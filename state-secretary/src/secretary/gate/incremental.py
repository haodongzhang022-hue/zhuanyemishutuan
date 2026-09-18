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
from typing import Dict, List, Optional, Sequence, Tuple

from .counter import assert_counter
from .models import (
    INCREMENT_ADDED, INCREMENT_DISSOLVED, INCREMENT_LEGACY,
    FullSweepReport, SweepPlan, SweepReport, SweepTarget,
)
from .rules import DEFAULT_RULES, SweepRules, looks_like_test_path


class GateError(RuntimeError):
    """门禁执行错误（仓库/路径/pytest 不可用等）。"""


_DEF_RE = re.compile(r"^\s*(?:async\s+)?def\s+(test_[A-Za-z0-9_]+)\s*\(")
_CLASS_RE = re.compile(r"^\s*class\s+([A-Za-z0-9_]+)\s*[:(]")
_NODEID_RE = re.compile(
    r"^(?P<path>[^:]+\.py)(?:::?(?P<rest>.*))?$")


def _run(cmd: Sequence[str], cwd: str) -> subprocess.CompletedProcess:
    return subprocess.run(list(cmd), cwd=cwd, capture_output=True, text=True)


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
        self.test_root = test_root.replace("\\", "/").strip("/")
        self.src_pythonpath = src_pythonpath
        self.rules = rules or DEFAULT_RULES
        self._pytest = list(pytest_cmd or [sys.executable, "-m", "pytest"])
        self._cache: Dict[str, List[str]] = {}

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
                }
            for path in untracked.stdout.splitlines():
                path = path.strip()
                if path:
                    entries[path] = {"path": path, "is_new": True,
                                     "changed_lines": 0}
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
            }
        return list(entries.values())

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
                cls = mc.group(1)
                continue
            md = _DEF_RE.match(line)
            if md:
                name = md.group(1)
                out.append(f"{cls}::{name}" if cls else name)
        return out

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
            out = _run(["git", "show", f"{rev}:{path}"], self.repo_root)
            if out.returncode != 0:
                return None
            return self.parse_test_defs(out.stdout)
        return None

    def test_fingerprints_at(self, rev: str, path: str) -> Dict[str, str]:
        """取某修订下"用例名 → 主体文本哈希"（细粒度失效判定用）。"""
        text = None
        if rev and rev != "WORKTREE":
            out = _run(["git", "show", f"{rev}:{path}"], self.repo_root)
            if out.returncode != 0:
                return {}
            text = out.stdout
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
                flush()
                cur_name, buf = None, []
                cls = mc.group(1)
                continue
            md = _DEF_RE.match(line)
            if md:
                flush()
                cur_name, buf = md.group(1), [line]
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
        test_changes = [c for c in changed if looks_like_test_path(c["path"])]

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
                # 新增文件：全部用例未跑过
                for name in now_defs:
                    targets.append(SweepTarget(
                        nodeid=self._nodeid_for(path, rel, name),
                        kind=INCREMENT_ADDED, path=path, name=name,
                        reason="新增文件此前不存在 → 未跑过，必跑"))
                continue
            added = [n for n in now_defs if n not in set(old_defs)]
            changed_names = [
                n for n in now_defs
                if n in set(old_defs) and n in new_fp
                and old_fp.get(n) != new_fp.get(n)]
            for name in added:
                targets.append(SweepTarget(
                    nodeid=self._nodeid_for(path, rel, name),
                    kind=INCREMENT_ADDED, path=path, name=name,
                    reason="用例此前不存在 → 未跑过，必跑"))
            for name in changed_names:
                risk = self.rules.is_redundant(
                    SweepTarget(nodeid=name, kind=INCREMENT_DISSOLVED,
                                risk=self._risk_for(path, name)))
                risk_label = self._risk_for(path, name)
                targets.append(SweepTarget(
                    nodeid=self._nodeid_for(path, rel, name),
                    kind=INCREMENT_DISSOLVED, path=path, name=name,
                    reason=("用例已跑过但其主体已改 → 消解"
                            if not risk else
                            f"用例已改但属冗余类（{risk_label}）→ 默认不重复"),
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

        # 已跑过（基线全集 - 计划内集合）= 默认不重复项
        planned_ids = {t.nodeid for t in plan.targets}
        for nodeid in baseline:
            if nodeid in planned_ids:
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

    def run(self, plan: SweepPlan, extra_args: Sequence[str] = (),
            timeout: int = 3600) -> SweepReport:
        """按计划实跑（选择器精确到 nodeid，杜绝"顺手全量"）。"""
        report = SweepReport(plan)
        if not plan.targets:
            report.exit_code = 0
            return report
        pkg = os.path.join(self.repo_root, "state-secretary")
        files = [f for f in plan.planned_files
                 if os.path.isfile(os.path.join(pkg, self._pytest_rel(f)))]
        selectors = [t.nodeid for t in plan.targets
                     if "::" in t.nodeid
                     and os.path.isfile(os.path.join(pkg, self._pytest_rel(
                         t.nodeid.split("::")[0])))]
        cmd = self._pytest + ["-q", "-p", "no:cacheprovider"]
        cmd += list(extra_args)
        cmd += selectors if selectors else files
        start = time.time()
        out = _run(cmd, os.path.join(self.repo_root, "state-secretary"))
        report.duration_sec = time.time() - start
        report.exit_code = out.returncode
        report.executed = [t.nodeid for t in plan.targets]
        report.raw_output = getattr(report, "raw_output", "")
        for line in (out.stdout or "").splitlines():
            s = line.strip()
            if s.startswith("FAILED ") or s.startswith("ERROR "):
                (report.failed if s.startswith("FAILED")
                 else report.errors).append(s.split(" ", 1)[-1])
        passed = len(report.executed) - len(report.failed) - len(report.errors)
        report.passed = report.executed[:max(passed, 0)]
        return report

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
        """校验报告文字的计数口径（跑多少写多少，不得写"全量"）。"""
        return assert_counter(
            report.render(),
            executed=report.executed_count,
            total_suite=report.counter_claim.total_suite,
            deferred=report.counter_claim.deferred)

    def assert_full_view(self, view: FullSweepReport) -> int:
        """校验全量告警视图：须标出"已跑过"数，否则拒绝。

        用户口径 #4：全量跑测时「默认已跑过」的必须标出来，否则「全量 743 个」
        会被读成本轮跑了 743 个。故此处只做**标记存在性 + 计数关系**校验。
        """
        if view.previously_run_count <= 0 and view.total > 1:
            raise GateError(
                "全量跑测视图未标记『已跑过/默认不重复』项 → 会造成"
                "『本轮跑了全量』的误导。请先标记已跑过项。")
        text = view.render()
        if "已跑过" not in text:
            raise GateError("全量跑测视图未标记『已跑过』项 → 误导性歧义。")
        if not (view.executed_now_count <= view.total):
            raise GateError(
                f"实跑数 {view.executed_now_count} > 全量 {view.total} → 计数不自洽。")
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
