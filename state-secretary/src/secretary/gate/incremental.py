"""增量门禁引擎：把"只跑增量"变成可执行、可留痕、可校验的门禁。

用法（本地/CI 一致）::

    from secretary.gate import IncrementalGate

    gate = IncrementalGate(repo_root="/workspace")
    plan = gate.plan_sweep(base="a455608", head="WORKTREE")
    print(plan.render())                     # 过程可视化：本轮要跑多少
    report = gate.run(plan)                  # 实跑（pytest 子进程）
    print(report.render())                   # 本轮实跑多少（不写"全量"）
    gate.assert_report(report)               # 计数口径守卫（跑多少写多少）

    full = gate.full_sweep_view(base="a455608")   # 全量告警视图
    print(full.render())                     # 已跑过项须被标记出来

设计要点（对齐用户口径）::

- **只跑增量**：只跑 `added`（未跑过）+ `dissolved`（已跑过但失效）。
- **允许漏**：默认不重复；漏掉的后续用时会重新提需求去改（用户明确允许）。
- **反冗余**：低质量重复（文档/计数/注释类）即使改动也剔除，防挤占有效测试。
- **全量须标记**：`full_sweep_view()` 逐项标注 `previously_run`。
- **权责一致**：`assert_report()` 拒绝用"全量"表述掩盖本轮实跑数。
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
import time
import xml.etree.ElementTree as ET
from typing import Dict, List, Optional, Sequence, Tuple

from .counter import assert_counter
from .models import (
    INCREMENT_ADDED, INCREMENT_DISSOLVED, INCREMENT_LEGACY,
    FullSweepReport, SweepPlan, SweepReport, SweepTarget,
)
from .rules import (
    DEFAULT_RULES, SweepRules, changed_test_files, looks_like_test_path,
)

__all__ = ["IncrementalGate", "GateError", "looks_like_test_path",
           "parse_pytest_nodeids"]


class GateError(RuntimeError):
    """门禁执行错误（仓库/路径/pytest 不可用等）。"""


# 顶层用例（缩进 0）与类内方法（缩进 4）：pytest 只收集这两层。
# 嵌套函数（缩进 ≥ 8，如测试内部 helper）**不是**用例，若被误收会产生
# 不存在的 nodeid → pytest rc=4（🔴-3 实测暴露）。
_DEF_RE = re.compile(
    r"^(?P<indent>[ \t]*)(?:async\s+)?def\s+(test_[A-Za-z0-9_]+)\s*\(")
_CLASS_RE = re.compile(r"^(?P<indent>[ \t]*)class\s+([A-Za-z0-9_]+)\s*[:(]")
_TOP_INDENT = ("", "    ")   # 顶层 / 类内一层
_NODEID_RE = re.compile(
    r"^(?P<path>[^:]+\.py)(?:::?(?P<rest>.*))?$")


def _run(cmd: Sequence[str], cwd: str,
         timeout: Optional[int] = None) -> subprocess.CompletedProcess:
    """执行子进程；``timeout`` 为 None 时不设上限（git 短命令用）。"""
    try:
        return subprocess.run(list(cmd), cwd=cwd, capture_output=True,
                              text=True, timeout=timeout)
    except subprocess.TimeoutExpired as exc:  # pragma: no cover - 需真实长跑
        raise GateError(
            f"子进程超时（{timeout}s）: {' '.join(list(cmd))}") from exc


def _is_git_repo(path: str) -> bool:
    try:
        out = _run(["git", "rev-parse", "--git-dir"], path)
    except (OSError, FileNotFoundError) as exc:  # pragma: no cover
        raise GateError(f"git 不可用: {exc}") from exc
    return out.returncode == 0


def parse_pytest_nodeids(text: str) -> List[str]:
    """从 ``pytest --collect-only -q`` 输出解析 nodeid 清单。

    输出形如 ``state-secretary/tests/test_a.py::TestX::test_y``。
    兼容 collect-only 的 q 模式（末尾带 "N tests collected" 汇总行）。
    """
    out: List[str] = []
    for line in (text or "").splitlines():
        line = line.strip()
        if not line or "::" not in line:
            continue
        if line.endswith("tests collected") or " error" in line:
            continue
        out.append(line)
    return out


class IncrementalGate:
    """增量跑测门禁。

    Args:
        repo_root: 仓库根（含 .git）。
        test_root: 测试根（仓库相对），默认 ``state-secretary/tests``。
        src_pythonpath: pytest 的 ``pythonpath``（默认 ``state-secretary/src``）。
        rules: 跑测规则集。
    """

    def __init__(self, repo_root: Optional[str] = None,
                 test_root: str = "state-secretary/tests",
                 src_pythonpath: str = "state-secretary/src",
                 rules: Optional[SweepRules] = None,
                 pytest_cmd: Optional[Sequence[str]] = None):
        self.repo_root = os.path.abspath(repo_root or os.getcwd())
        if not os.path.isdir(self.repo_root):
            raise GateError(f"仓库根不存在: {self.repo_root}")
        if not _is_git_repo(self.repo_root):
            raise GateError(f"不是 git 仓库（缺 .git）: {self.repo_root}")
        self.test_root = test_root.replace("\\", "/").strip("/")
        # 🟡3：形参真正接线——以 pytest 的 `pythonpath` ini 选项注入
        # （不再"赋值后无任何引用"）。
        self.src_pythonpath = src_pythonpath
        self.rules = rules or DEFAULT_RULES
        self._pytest = list(pytest_cmd or [sys.executable, "-m", "pytest"])
        self._cache: Dict[str, List[str]] = {}
        # 🟢 优化：`git show rev:path` 结果缓存（同一路径在 defs/指纹两处复用），
        # 把 O(变更文件) 次子进程降为每 (rev, path) 一次。
        self._show_cache: Dict[Tuple[str, str], Optional[str]] = {}

    # ------------------------------------------------------------------
    # git 增量识别
    # ------------------------------------------------------------------

    def resolve(self, rev: str) -> Optional[str]:
        """把 ``WORKTREE``/``HEAD``/sha 解析为 sha；不存在返回 None。"""
        if not rev or rev == "WORKTREE":
            return None
        out = _run(["git", "rev-parse", "--verify", f"{rev}^{{commit}}"],
                   self.repo_root)
        return out.stdout.strip() if out.returncode == 0 else None

    def changed_files(self, base: str, head: str = "WORKTREE") -> List[dict]:
        """列出 base→head 的变更文件（含新增/改动行数）。

        Returns:
            ``[{"path", "is_new", "changed_lines"}]``
        """
        if head == "WORKTREE":
            # 工作区：已跟踪改动 + 未跟踪新增
            tracked = _run(["git", "diff", "--numstat", base], self.repo_root)
            untracked = _run(
                ["git", "ls-files", "--others", "--exclude-standard"],
                self.repo_root)
            entries: Dict[str, dict] = {}
            for line in tracked.stdout.splitlines():
                parts = line.split("\t")
                if len(parts) < 3:
                    continue
                add, path = parts[0], parts[2]
                entries[path] = {
                    "path": path,
                    "is_new": not self._tracked_in_base(base, path),
                    "changed_lines": int(add) if add.isdigit() else 0,
                    "total_lines": self._total_lines(path),
                }
            for path in untracked.stdout.splitlines():
                path = path.strip()
                if path:
                    entries[path] = {"path": path, "is_new": True,
                                     "changed_lines": 0,
                                     "total_lines": self._total_lines(path)}
            return list(entries.values())

        out = _run(["git", "diff", "--numstat", base, head], self.repo_root)
        added_out = _run(["git", "diff", "--name-only", "--diff-filter=A",
                          base, head], self.repo_root)
        new_paths = {p.strip() for p in added_out.stdout.splitlines() if p.strip()}
        entries = {}
        for line in out.stdout.splitlines():
            parts = line.split("\t")
            if len(parts) < 3:
                continue
            add, path = parts[0], parts[2]
            entries[path] = {
                "path": path,
                "is_new": path in new_paths,
                "changed_lines": int(add) if add.isdigit() else 0,
                "total_lines": self._total_lines(path),
            }
        return list(entries.values())

    def _total_lines(self, path: str) -> int:
        """工作区该文件总行数（🟡2：整文件重写比例判定的分母）。"""
        full = os.path.join(self.repo_root, path)
        try:
            with open(full, "r", encoding="utf-8", errors="replace") as f:
                return sum(1 for _ in f)
        except OSError:
            return 0

    def _tracked_in_base(self, base: str, path: str) -> bool:
        out = _run(["git", "cat-file", "-e", f"{base}:{path}"], self.repo_root)
        return out.returncode == 0

    # ------------------------------------------------------------------
    # 用例展开（基线 / 当前）
    # ------------------------------------------------------------------

    def collect(self, head: str = "WORKTREE",
                test_files: Optional[Sequence[str]] = None) -> List[str]:
        """采集 nodeid 清单。

        Args:
            head: ``WORKTREE`` = 当前工作区实采；否则为 git 修订号，
                按修订号导出测试树到临时目录后采集（跨版判别力验证用）。
            test_files: 仅采集这些文件（增量跑测时限定范围，省采集开销）。
        """
        cache_key = f"{head}|{','.join(sorted(test_files or []))}"
        if cache_key in self._cache:
            return list(self._cache[cache_key])

        if head == "WORKTREE":
            cmd = self._pytest + ["--collect-only", "-q"]
            if test_files:
                # 只采集**当前工作区实际存在**的文件：pytest 对不存在的路径会
                # 直接报错退出（rc=4）导致整批采集失败（跨版复核时基线文件
                # 可能尚不存在）。缺失文件在本轮即"无可用用例"，跳过即可。
                pkg = os.path.join(self.repo_root, "state-secretary")
                present = [f for f in test_files
                           if os.path.isfile(os.path.join(pkg, f))]
                if not present:
                    self._cache[cache_key] = []
                    return []
                cmd += present
            out = _run(cmd, os.path.join(self.repo_root, "state-secretary"))
            ids = parse_pytest_nodeids(out.stdout)
            ids = [self._relativize(i) for i in ids]
        else:
            ids = self._collect_at_rev(head, test_files)
        self._cache[cache_key] = ids
        return list(ids)

    def _relativize(self, nodeid: str) -> str:
        """把 pytest 输出的 ``tests/x.py::t`` 规范为仓库相对路径。"""
        m = _NODEID_RE.match(nodeid)
        if not m:
            return nodeid
        path, rest = m.group("path"), m.group("rest")
        norm = path.replace("\\", "/")
        if not norm.startswith(self.test_root) and \
                not norm.startswith("state-secretary/"):
            norm = f"{self.test_root}/{norm.rsplit('tests/', 1)[-1]}" \
                if "tests/" in norm else norm
        return f"{norm}::{rest}" if rest else norm

    def _collect_at_rev(self, rev: str,
                        test_files: Optional[Sequence[str]]) -> List[str]:
        """在历史修订上采集 nodeid（用 git archive 导出到临时目录）。"""
        import shutil
        import tempfile
        import tarfile

        sha = self.resolve(rev)
        if sha is None:
            raise GateError(f"修订不存在: {rev!r}")
        tmp = tempfile.mkdtemp(prefix="gate-rev-")
        try:
            archive = _run(["git", "archive", sha], self.repo_root)
            if archive.returncode != 0:
                raise GateError(f"git archive {rev} 失败: {archive.stderr[:200]}")
            tar_path = os.path.join(tmp, "rev.tar")
            with open(tar_path, "wb") as f:
                f.write(archive.stdout.encode("utf-8", "surrogateescape"))
            with tarfile.open(tar_path) as tf:
                tf.extractall(tmp)
            pkg = os.path.join(tmp, "state-secretary")
            if not os.path.isdir(pkg):
                return []
            cmd = self._pytest + ["--collect-only", "-q"]
            if test_files:
                present = [f for f in test_files
                           if os.path.isfile(os.path.join(pkg, f))]
                if not present:
                    return []
                cmd += present
            out = _run(cmd, pkg)
            ids = parse_pytest_nodeids(out.stdout)
            return [self._relativize(i) for i in ids]
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    @staticmethod
    def parse_test_defs(source_text: str) -> List[str]:
        """从测试文件源码解析 ``类::用例`` / 顶层用例名清单。"""
        out: List[str] = []
        cls = ""
        for line in (source_text or "").splitlines():
            mc = _CLASS_RE.match(line)
            if mc:
                # 仅**顶层类**（缩进 0）切换当前类；方法内嵌套类（如测试内
                # 部的 `class _Out`）须被忽略，不得重置当前类上下文
                # （否则其后的类内用例会退化为顶层 nodeid → rc=4）。
                if mc.group("indent") == "":
                    cls = mc.group(2)
                continue
            md = _DEF_RE.match(line)
            if md and md.group("indent") in _TOP_INDENT:
                name = md.group(2)
                out.append(f"{cls}::{name}" if cls else name)
        return out

    def _show(self, rev: str, path: str) -> Optional[str]:
        """取某修订下文件内容（缓存；不存在返回 None）。"""
        key = (rev, path)
        if key not in self._show_cache:
            out = _run(["git", "show", f"{rev}:{path}"], self.repo_root)
            self._show_cache[key] = out.stdout if out.returncode == 0 else None
        return self._show_cache[key]

    def test_defs_at(self, rev: str, path: str) -> Optional[List[str]]:
        """取某修订下测试文件的用例名清单（不存在返回 None = 新增文件）。"""
        if not rev or rev == "WORKTREE":
            full = os.path.join(self.repo_root, path)
        else:
            full = None
        if full and os.path.isfile(full):
            with open(full, "r", encoding="utf-8") as f:
                return self.parse_test_defs(f.read())
        if rev and rev != "WORKTREE":
            text = self._show(rev, path)
            return None if text is None else self.parse_test_defs(text)
        return None

    def test_fingerprints_at(self, rev: str, path: str) -> Dict[str, str]:
        """取某修订下"用例名 → 主体文本哈希"（细粒度失效判定用）。"""
        text = None
        if rev and rev != "WORKTREE":
            text = self._show(rev, path)
            if text is None:
                return {}
        else:
            full = os.path.join(self.repo_root, path)
            if os.path.isfile(full):
                with open(full, "r", encoding="utf-8") as f:
                    text = f.read()
        if text is None:
            return {}
        return self._fingerprints(text)

    @staticmethod
    def _fingerprints(source: str) -> Dict[str, str]:
        import hashlib

        out: Dict[str, str] = {}
        cls = ""
        cur_name: Optional[str] = None
        buf: List[str] = []

        def flush():
            if cur_name is not None:
                body = "\n".join(buf).strip()
                key = f"{cls}::{cur_name}" if cls else cur_name
                if body:
                    out[key] = hashlib.sha256(
                        body.encode("utf-8")).hexdigest()[:16]

        for line in (source or "").splitlines():
            mc = _CLASS_RE.match(line)
            if mc:
                # 嵌套类忽略（不重置 cls）；仅顶层类切换类上下文
                if mc.group("indent") == "":
                    flush()
                    cur_name, buf = None, []
                    cls = mc.group(2)
                continue
            md = _DEF_RE.match(line)
            if md and md.group("indent") in _TOP_INDENT:
                flush()
                cur_name, buf = md.group(2), [line]
                continue
            if cur_name is not None:
                buf.append(line)
        flush()
        return out

    # ------------------------------------------------------------------
    # 计划（本轮要跑什么）
    # ------------------------------------------------------------------

    def plan_sweep(self, base: str, head: str = "WORKTREE",
                   guards: Optional[Sequence[str]] = None,
                   include_redundant: bool = False) -> SweepPlan:
        """生成增量跑测计划（只跑有效增量，剔除冗余）。

        Args:
            base: 基线修订（上一次已跑过的 head）。
            head: ``WORKTREE`` 或修订号。
            guards: 有效覆盖面关键词（缺省取默认关键面）。
            include_redundant: 是否把冗余项也纳入计划（默认 False = 剔除）。
        """
        plan = SweepPlan(base=base, head=head, test_root=self.test_root)
        changed = self.changed_files(base, head)
        # 🟡9：统一走 `changed_test_files`（与 `looks_like_test_path` 单一判定源），
        # 再把 diff 元数据（is_new / changed_lines）贴回。
        by_path = {c["path"]: c for c in changed}
        test_changes = [by_path[p] for p in changed_test_files(changed)]

        # 🔴-3：实测全量规模（``collect(head)`` 真实收集数）——规模口径的
        # 权威来源，避免并集口径把陈旧基线 nodeid 计入（规模虚高）。
        # 注：须在 ``changed_files`` **之后**采集——pytest 会写 ``__pycache__``，
        # 提前采集会把它当成未跟踪新增文件。
        plan.measured_suite_total = len(self.collect(head))

        # 采集当前用例全集（限定到变更文件，避免全树采集开销）
        scope = [self._pytest_rel(c["path"]) for c in test_changes]
        available = self.collect(head, scope or None)

        # 采集基线用例全集（已跑过的集合）
        baseline = self.collect(base) if self.resolve(base) else []

        targets: List[SweepTarget] = []
        for change in test_changes:
            path = change["path"]
            rel = self._pytest_rel(path)
            now_defs = self.test_defs_at(head, path) or []
            old_defs = self.test_defs_at(base, path)
            old_fp = self.test_fingerprints_at(base, path)
            new_fp = self.test_fingerprints_at(head, path)
            if old_defs is None:
                # 新增文件：全部用例未跑过（展开到**真实 nodeid**，
                # 参数化用例按 pytest 收集口径逐项列出，避免计划数 ≠ 实跑数）
                for nodeid in self._expand_nodeids(head, rel, now_defs):
                    targets.append(SweepTarget(
                        nodeid=nodeid,
                        kind=INCREMENT_ADDED, path=path,
                        name=nodeid.split("::")[-1],
                        reason="新增文件此前不存在 → 未跑过，必跑"))
                continue
            added = [n for n in now_defs if n not in set(old_defs)]
            changed_names = [
                n for n in now_defs
                if n in set(old_defs) and n in new_fp
                and old_fp.get(n) != new_fp.get(n)]
            # 🟡2 接线：整文件重写（改动比例 ≥ dissolve_ratio / 高风险关键词）
            # → 除新增外**全部用例**升级为消解（宁多跑，不漏跑）。此前
            # `is_whole_file_rewrite` 不在门禁路径上（仅测试调用）→ 判定不生效。
            if self.rules.is_whole_file_rewrite(change):
                extra = [n for n in now_defs
                         if n not in set(added) and n not in set(changed_names)]
                if extra:
                    changed_names = changed_names + extra
                    plan.note.append(
                        f"{rel}: 整文件重写（改动比例 ≥ "
                        f"{self.rules.dissolve_ratio} 或高风险关键词）→ "
                        f"全文件消解（补入 {len(extra)} 例）")
            for name in added:
                for nodeid in self._expand_nodeids(head, rel, [name]):
                    targets.append(SweepTarget(
                        nodeid=nodeid, kind=INCREMENT_ADDED, path=path,
                        name=nodeid.split("::")[-1],
                        reason="用例此前不存在 → 未跑过，必跑"))
            for name in changed_names:
                # 🟡11：_risk_for 只算一次，不再构造一次即弃的 SweepTarget
                risk_label = self._risk_for(path, name)
                redundant = self.rules.is_redundant_label(risk_label)
                for nodeid in self._expand_nodeids(head, rel, [name]):
                    targets.append(SweepTarget(
                        nodeid=nodeid,
                        kind=INCREMENT_DISSOLVED, path=path,
                        name=nodeid.split("::")[-1],
                        reason=("用例已跑过但其主体已改 → 消解"
                                if not redundant else
                                f"用例已改但属冗余类（{risk_label}）"
                                "→ 默认不重复"),
                        risk=risk_label))
            if not added and not changed_names:
                plan.note.append(
                    f"{rel}: 无用例级失效（仅非断言面改动）→ 不重复跑")

        # 拆分：要跑（非冗余） / 剔除（冗余） / 已跑过（基线中与本 diff 无关）
        for t in self.rules.merge_targets(targets):
            if t.kind == INCREMENT_ADDED or not self.rules.is_redundant(t):
                plan.targets.append(t)
            elif include_redundant:
                plan.targets.append(t)
            else:
                plan.excluded.append(t)

        # 已跑过（基线全集 - 计划内集合 - 冗余剔除集合）= 默认不重复项
        # 🔴-3：**必须剔除 excluded**，否则同一用例会同时落在 deferred 与
        # excluded → 全量规模重复计数（实测 362 vs 真实 361）。
        planned_ids = {t.nodeid for t in plan.targets}
        excluded_ids = {t.nodeid for t in plan.excluded}
        for nodeid in baseline:
            if nodeid in planned_ids or nodeid in excluded_ids:
                continue
            plan.deferred.append(SweepTarget(
                nodeid=nodeid, kind=INCREMENT_LEGACY, path=nodeid.split("::")[0],
                name=nodeid.split("::")[-1],
                seen_at=base,
                reason="与增量无关 → 此前已跑过，默认不重复（允许漏）"))

        # 有效覆盖面兜底：剔除冗余后仍须覆盖关键面（每面最多补 1 例）。
        # **仅当本轮确有增量时**才兜底——无增量（计划 0 例）时补跑属纯冗余。
        guard_keys = list(guards or DEFAULT_GUARDS) if plan.targets else []
        supplements = self.rules.effective_coverage(
            available, [t.nodeid for t in plan.targets], guard_keys)
        for nodeid in supplements:
            plan.targets.append(SweepTarget(
                nodeid=nodeid, kind=INCREMENT_DISSOLVED,
                path=nodeid.split("::")[0], name=nodeid.split("::")[-1],
                reason="有效覆盖面兜底（该有效面前缀无计划用例，补 1 例）",
                risk="dissolve"))
            plan.note.append(f"覆盖面兜底补入: {nodeid}")
        for t in plan.deferred:
            if t.nodeid in {s for s in supplements}:
                plan.deferred.remove(t)
        return plan

    def _pytest_rel(self, path: str) -> str:
        """仓库相对路径 → pytest 运行目录（state-secretary/）相对路径。"""
        norm = path.replace("\\", "/")
        prefix = "state-secretary/"
        return norm[len(prefix):] if norm.startswith(prefix) else norm

    def _expand_nodeids(self, head: str, rel: str,
                        names: Sequence[str]) -> List[str]:
        """把「类::用例名」展开为 pytest 收集口径的真实 nodeid 清单。

        🟡5：计划口径须与 pytest 收集口径一致——参数化用例按参数逐项展开
        （如 ``test_g3_redundant_labels[path0-risk0]``）。收集结果不可用时
        保守回退为 def 级 nodeid（多跑/少跑均以 pytest 实跑汇总为准）。
        """
        full = [self._nodeid_for(f"state-secretary/{rel}", rel, n)
                for n in names]
        try:
            collected = self.collect(head, [rel])
        except Exception:  # pragma: no cover - 收集失败时回退
            collected = []
        out: List[str] = []
        for base in full:
            hits = [c for c in collected
                    if c == base or c.startswith(base + "[")]
            out.extend(hits or [base])
        return out

    def _nodeid_for(self, path: str, rel: str, name: str) -> str:
        """统一 nodeid 口径：仓库相对路径（``state-secretary/tests/...``）。"""
        norm = path.replace("\\", "/")
        if not norm.startswith("state-secretary/"):
            norm = f"state-secretary/{norm}"
        return f"{norm}::{name}" if name else norm

    def _risk_for(self, path: str, name: str) -> str:
        from .rules import is_redundant_label
        risk = is_redundant_label(path)
        if risk:
            return risk
        import re
        if re.search(r"计数|留痕|台账|口径|复测|复评", name or ""):
            return "count-sync"
        if re.search(r"注释|typo|格式", name or ""):
            return "cosmetic"
        return "dissolve"

    # ------------------------------------------------------------------
    # 执行与报告
    # ------------------------------------------------------------------

    def _runnable(self, plan: SweepPlan) -> Tuple[List[str], List[str]]:
        """把计划标的解析为 pytest 运行目录相对的 selectors / files。

        🔴-1 定点修复：selectors 与 files **一律** ``_pytest_rel()``
        （子进程 ``cwd=state-secretary/``，仓库相对路径会 rc=4 not found）。
        """
        pkg = os.path.join(self.repo_root, "state-secretary")
        selectors: List[str] = []
        files: List[str] = []
        for t in plan.targets:
            if "::" in t.nodeid:
                rel_path, _, rest = t.nodeid.partition("::")
                rel = self._pytest_rel(rel_path)
                if not os.path.isfile(os.path.join(pkg, rel)):
                    raise GateError(
                        f"计划标的文件不存在（selector 无法解析）: {t.nodeid} "
                        f"→ pytest 运行目录相对路径 {rel!r} 不存在，"
                        "拒绝继续（否则会假绿或退化为全量）。")
                node = f"{rel}::{rest}" if rest else rel
                if node not in selectors:
                    selectors.append(node)
            else:
                rel = self._pytest_rel(t.nodeid)
                if not os.path.isfile(os.path.join(pkg, rel)):
                    raise GateError(
                        f"计划标的文件不存在（文件级标的无法解析）: {t.nodeid} "
                        f"→ pytest 运行目录相对路径 {rel!r} 不存在，拒绝继续。")
                if rel not in files:
                    files.append(rel)
        return selectors, files

    def run(self, plan: SweepPlan, extra_args: Sequence[str] = (),
            timeout: int = 3600) -> SweepReport:
        """按计划实跑（选择器精确到 nodeid，杜绝"顺手全量"）。

        🔴-1 定点修复要点：

        1. ``selectors`` / ``files`` 一律 ``_pytest_rel()``（运行目录相对），
           文件不存在即 fail-fast，绝不把仓库相对路径直接喂给 pytest；
        2. 两者皆空且计划非空 → **fail-fast**（``GateError``），
           **不得**退化为裸 ``pytest -q`` 全量；
        3. ``executed`` 与各计数取 **pytest 真实执行结果**
           （junitxml 结构化汇总，缺失时回退汇总行），并校验 ``rc`` 与汇总一致；
        4. ``green`` 纳入 ``exit_code``（rc≠0 一律非绿）。

        Args:
            plan: 跑测计划。
            extra_args: 追加给 pytest 的参数。
            timeout: 子进程超时秒数（🟡1：形参须真正生效）。
        """
        report = SweepReport(plan)
        if not plan.targets:
            # 无增量 → 计划 0 例：不跑、不报绿歧义（rc=0，实跑 0）
            report.exit_code = 0
            report.summary_line = "无增量 → 本轮 0 例（不跑）"
            # 空计划无计数需求 → counts_source="empty"（视为已核实，可落账）
            report.counts_source = "empty"
            return report

        selectors, files = self._runnable(plan)
        if not selectors and not files:
            raise GateError(
                f"计划 {plan.planned_count} 例，但 selectors 与 files 皆为空 → "
                "拒绝裸跑 pytest（会退化为全量，违反『只跑增量』）。"
                "请检查计划标的路径口径。")

        pkg = os.path.join(self.repo_root, "state-secretary")
        cmd = self._pytest + ["-q", "-p", "no:cacheprovider"]
        # 🟡3：src_pythonpath 接线为 pytest `pythonpath` ini（子进程 cwd 为
        # state-secretary/，此处传相对路径即可）。
        if self.src_pythonpath:
            cmd += ["-o", f"pythonpath={self.src_pythonpath}"]
        cmd += list(extra_args)
        cmd += selectors + files

        import tempfile
        fd, xml_path = tempfile.mkstemp(prefix="gate-junit-", suffix=".xml")
        os.close(fd)
        cmd += [f"--junitxml={xml_path}"]
        # 🟡5：留痕命令须与**实际执行**一致 → 在拼接 `--junitxml=` 之后生成。
        report.command = " ".join(cmd)
        start = time.time()
        out = _run(cmd, pkg, timeout=timeout)
        report.duration_sec = time.time() - start
        report.exit_code = out.returncode
        report.raw_output = (out.stdout or "") + (out.stderr or "")
        report.summary_line = self._summary_line(out.stdout or "")

        self._fill_results(report, xml_path, out)
        try:
            os.unlink(xml_path)
        except OSError:  # pragma: no cover
            pass

        self._verify_consistency(report, selectors + files)
        return report

    @staticmethod
    def _summary_line(stdout: str) -> str:
        """取 pytest 汇总行（含 passed/failed/skipped 计数的那行）。"""
        for line in reversed((stdout or "").splitlines()):
            if re.search(r"\d+\s+(passed|failed|error|skipped|no tests)",
                         line) and "=" not in line:
                return line.strip()
        return ""

    @staticmethod
    def _parse_summary(summary: str) -> Dict[str, int]:
        """解析汇总行的分项计数（passed/failed/errors/skipped）。"""
        counts: Dict[str, int] = {}
        mapping = {"passed": "passed", "failed": "failed", "error": "errors",
                   "errors": "errors", "skipped": "skipped"}
        for num, word in re.findall(r"(\d+)\s+(\w+)", summary or ""):
            key = mapping.get(word.lower())
            if key:
                counts[key] = counts.get(key, 0) + int(num)
        return counts

    def _fill_results(self, report: SweepReport, xml_path: str,
                      out: subprocess.CompletedProcess) -> None:
        """用 pytest 真实执行结果填充报告。

        🔴-2 定点修复（回退分支不得用计划数冒充实跑数）：

        - **junitxml 可用** → 结构化计数（``counts_source="junitxml"``，
          最权威，可复核）；
        - **junitxml 不可用但汇总行有计数** → 汇总行计数
          （``counts_source="stdout"``，真实执行产物，可落账）；
        - **两者皆无** → ``counts_source="none"``，``executed`` 保持已解析到
          的真实结果（通常 0），**绝不**用 ``plan.targets`` 填充；
          该报告 ``green=False`` 且 ``assert_report`` 一律抛错（拒绝落账）。
        """
        counts: Optional[Dict[str, int]] = None
        if not os.path.isfile(xml_path):
            report.degraded_reason = (
                "无 junitxml 产物（runner 未产出 / 被替换）→ 回退 pytest 汇总行计数")
        if os.path.isfile(xml_path):
            try:
                counts, nodeids = self._parse_junit(xml_path)
            except (ET.ParseError, OSError) as exc:
                # 🟡4：收窄异常范围（原 `except Exception` 吞掉全部解析异常），
                # 并**记录降级原因**（否则回退路径静默）。
                counts, nodeids = None, None
                report.degraded_reason = (
                    f"junitxml 解析失败（{type(exc).__name__}: {exc}）→ "
                    "回退 pytest 汇总行计数")
            else:
                for nodeid, kind in nodeids.items():
                    report.executed.append(nodeid)
                    if kind == "passed":
                        report.passed.append(nodeid)
                    elif kind == "failed":
                        report.failed.append(nodeid)
                    elif kind == "error":
                        report.errors.append(nodeid)
                    elif kind == "skipped":
                        report.skipped.append(nodeid)
                report.counts_verified = True
                report.counts_source = "junitxml"

        if counts is None:
            # 回退：汇总行计数 + FAILED/ERROR 行（**真实执行产物**）
            counts = self._parse_summary(report.summary_line)
            for line in (out.stdout or "").splitlines():
                t = line.strip()
                if t.startswith("FAILED ") or t.startswith("ERROR "):
                    (report.failed if t.startswith("FAILED")
                     else report.errors).append(t.split(" ", 1)[-1])
            report.executed = (list(report.passed) + list(report.failed)
                               + list(report.errors) + list(report.skipped))
            if counts:
                report.counts_verified = False
                report.counts_source = "stdout"
            else:
                # 🔴-2：无结构化结果且**无 stdout 计数** → 不得用计划数冒充。
                # executed 保持已解析到的真实 nodeid（通常 0），计数标为未核实，
                # green 自动为 False，assert_report 拒绝落账。
                report.counts_verified = False
                report.counts_source = "none"
                report.degraded_reason = report.degraded_reason or (
                    "无 junitxml 且 pytest 汇总行无计数 → 本轮实跑数不可核实"
                    "（拒绝报绿 / 拒绝落账）")
            report.counts = dict(counts)
            report.suite_total = sum(counts.values())
            return

        report.counts = dict(counts)
        report.suite_total = sum(counts.values())

    def _parse_junit(self, xml_path: str) -> Tuple[Dict[str, int], Dict[str, str]]:
        """解析 junitxml：返回（分项计数, nodeid → 状态）。

        nodeid 重建口径：``classname`` 的点分路径还原为 ``tests/x.py::Cls``，
        再拼接 ``name``（含参数化 ``[..]``），最后 ``_relativize`` 到仓库相对。
        """
        counts = {"passed": 0, "failed": 0, "errors": 0, "skipped": 0}
        nodeids: Dict[str, str] = {}
        tree = ET.parse(xml_path)
        for case in tree.iter("testcase"):
            status = "passed"
            if case.find("failure") is not None:
                status = "failed"
            elif case.find("error") is not None:
                status = "error"
            elif case.find("skipped") is not None:
                status = "skipped"
            counts[status] += 1
            nodeid = self._nodeid_from_junit(
                case.get("classname") or "", case.get("name") or "")
            nodeids[nodeid] = status
        return counts, nodeids

    def _nodeid_from_junit(self, classname: str, name: str) -> str:
        file_parts: List[str] = []
        cls_parts: List[str] = []
        for part in (classname or "").split("."):
            if not part:
                continue
            if not cls_parts and not part[:1].isupper():
                file_parts.append(part)
            else:
                cls_parts.append(part)
        path = "/".join(file_parts) + ".py" if file_parts else ""
        node = "::".join([path] + cls_parts + ([name] if name else []))
        return self._relativize(node)

    def _verify_consistency(self, report: SweepReport,
                            requested: Sequence[str]) -> None:
        """校验 rc / 汇总 / 计划三者一致。

        🔴-2：``counts_source == "none"``（无任何真实计数证据）→ 拒绝落账；
        🔴-3：**不变量 1「计划跑测数 == 报告实跑数」**须显式校验
        （文件级标的 1 个 → pytest 实跑该文件全部 3 例 → 必须拦截）。
        """
        rc = report.exit_code
        counts = report.counts
        n_fail = report.failed_count
        n_err = report.errors_count
        total = report.executed_count

        if rc == 4:
            raise GateError(
                "pytest rc=4（用法错误 / 路径未找到）→ selector 路径口径错误，"
                f"一个用例都没跑。原始输出尾部: {report.raw_output[-300:]}")
        if rc == 5 and total:
            raise GateError("rc=5（未收集到用例）却解析出实跑计数 → 结果不可信。")
        if not report.counts_reliable:
            # 🔴-2：无任何真实执行计数证据 → 实跑数不可核实。此处**不**用
            # 计划数填充、也不误报 rc/汇总不一致；报告保持 green=False，
            # 落账由 assert_report 明确拒绝（run() 仍返回可诊断的报告）。
            return
        if rc == 0 and (n_fail or n_err):
            raise GateError(
                f"rc=0 但汇总含失败 {n_fail} / 错误 {n_err} → rc 与汇总不一致，"
                "报告不可信，拒绝落账。")
        if rc == 1 and not (n_fail or n_err):
            raise GateError(
                "rc=1 但汇总无失败/错误 → rc 与汇总不一致（可能选择器未命中或"
                f"结果解析失败）。pytest: {report.summary_line or report.raw_output[-200:]}")
        if rc == 0 and requested and total == 0:
            raise GateError(
                f"rc=0 但实跑 0 例（请求 {len(requested)} 个 selector）→ "
                "计划非空却未跑任何用例，拒绝报绿。")
        if counts and sum(counts.values()) != total:
            raise GateError(
                f"分项计数之和 {sum(counts.values())} ≠ 实跑 {total} → 计数不自洽。")
        # 🔴-3 不变量 1：计划跑测数 == 报告实跑数
        planned = report.plan.planned_count
        if report.counts_reliable and rc == 0 and planned and total != planned:
            raise GateError(
                f"计划 {planned} 例 ≠ 实跑 {total} 例 → 违反不变量 1"
                "「计划跑测数 == 报告实跑数」（文件级标的须展开到 nodeid，"
                "或修正计划口径），拒绝落账。"
                f"pytest: {report.summary_line or '(无汇总)'}")

    # ------------------------------------------------------------------
    # 全量告警视图
    # ------------------------------------------------------------------

    def full_sweep_view(self, base: str, head: str = "WORKTREE",
                        executed_now: Optional[Sequence[str]] = None
                        ) -> FullSweepReport:
        """全量视图：把**此前已跑过**的用例逐项标记出来（防"全量"歧义）。"""
        all_ids = self.collect(head)
        previously = self.collect(base) if self.resolve(base) else []
        return FullSweepReport(all_ids, previously,
                               list(executed_now or []), base=base)

    # ------------------------------------------------------------------
    # 计数口径守卫（权责一致）
    # ------------------------------------------------------------------

    def assert_report(self, report: SweepReport) -> int:
        """校验报告文字的计数口径（跑多少写多少，不得写"全量"）。

        🔴-2：``counts_reliable is False``（计数未核实）→ **必须抛错**，
        不得对未核实计数报绿 / 落账。

        🔴-3：绑定**真实实跑数**（``report.executed_count`` = pytest 汇总口径），
        并校验其实跑数确实出现在报告文本中。
        """
        if not report.counts_reliable:
            raise GateError(
                "计数未经核实（counts_source="
                f"{report.counts_source!r}）→ 拒绝报绿 / 拒绝落账。"
                f"降级原因: {report.degraded_reason or '(未记录)'}")
        # 🔴-3 不变量 1：计划跑测数 == 报告实跑数（显式拒绝，不靠"计划构造得对"）
        planned = report.plan.planned_count
        executed = report.executed_count
        if planned and executed and planned != executed:
            raise GateError(
                f"计划 {planned} 例 ≠ 实跑 {executed} 例 → 违反不变量 1"
                "「计划跑测数 == 报告实跑数」，拒绝落账（文件级标的须展开到 "
                "nodeid 或修正计划口径）。")
        return assert_counter(
            report.render(),
            executed=report.executed_count,
            total_suite=report.counter_claim.total_suite,
            deferred=report.counter_claim.deferred,
            planned=report.plan.planned_count,
            redundant=len(report.plan.excluded))

    def assert_full_view(self, view: FullSweepReport) -> int:
        """校验全量告警视图：须标出"已跑过"数，否则拒绝。

        用户口径 #4：全量跑测时「默认已跑过」的必须标出来，否则「全量 743 个」
        会被读成本轮跑了 743 个。故此处只做**标记存在性 + 计数关系**校验。
        """
        text = view.render()
        # 🟡7：首轮全量（无历史基线，previously_run == 0 且实跑 == 全量）放行
        if (view.previously_run_count <= 0 and view.total > 1
                and not view.is_first_full_run):
            raise GateError(
                "全量跑测视图未标记『已跑过/默认不重复』项 → 会造成"
                "『本轮跑了全量』的误导。请先标记已跑过项。")
        if "已跑过" not in text:
            raise GateError("全量跑测视图未标记『已跑过』项 → 误导性歧义。")
        if not (view.executed_now_count <= view.total):
            raise GateError(
                f"实跑数 {view.executed_now_count} > 全量 {view.total} → 计数不自洽。")
        # 计数守卫：绑定全量视图的真实三数（实跑/已跑过/未消解）
        assert_counter(
            text,
            executed=view.executed_now_count,
            total_suite=view.total,
            deferred=view.previously_run_count,
            remaining=len(view.remaining),
            redundant=len(view.remaining))
        return view.executed_now_count


# 有效覆盖面兜底的关键面（每个有效面至少 1 例在计划内；不足则补 1 例）
DEFAULT_GUARDS: Tuple[str, ...] = (
    "test_secretary_session.py",
    "test_ho_ref_invariant_probes.py",
    "test_acceptance_matrix_issue4.py",
    "test_state_manager.py",
    "test_content_archive.py",
    "test_state_ownership.py",
    "test_state_arbiter.py",
    "test_state_tree.py",
    "test_batch_persist.py",
)
