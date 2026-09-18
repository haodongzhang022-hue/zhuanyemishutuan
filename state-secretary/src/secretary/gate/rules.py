"""跑测规则：只跑有效增量，杜绝低质量冗余。

判定顺序（三态，见 :mod:`secretary.gate.models`）：

1. **新增**（``added``）——文件/用例此前**不存在** → 必跑。
   新增用例天然是"有效测试"（它锁定一个尚无人跑过的断言面）。
2. **改动**（``dissolved``）——文件/用例此前存在、本轮被改 → 「已跑过但已
   失效」→ 复跑属**消解**（只在证据消解时跑）。**不得**当作"已跑过就不用跑"。
3. **无关**（``legacy``）——与增量 diff 无关 → 此前已跑过 → **默认不重复**。

**冗余剔除（反过度测试）**：以下路径即使被改动也不复跑，除非其断言面被消解：

- 纯注释/文档/空行改写（``cosmetic``）：断言面未变 → 复跑无增量信息；
- 计数/台账同步（``count-sync``）：如"测试计数同步 308 项"，只改数字；
- 文档章追加（``doc-append``）：如"复评第 N 轮独立复测留痕"。

细粒度失效（fail-closed）：以**用例主体文本哈希**为指纹。某用例被改 → 仅该
用例消解；同一文件内未改动的用例**默认不重复**。指纹缺失（浅克隆/diff 截断）
时保守回退为**文件级全量消解**，宁可多跑，不可漏跑。
"""
from __future__ import annotations

import os
import re
from typing import Dict, Iterable, List, Sequence, Tuple

from .models import (
    INCREMENT_ADDED, INCREMENT_DISSOLVED, INCREMENT_LEGACY,
    SweepTarget,
)

# ---- 「冗余」路径规则（默认不重复；除非消解阈值触发） ----
#
# 每条规则 = (名称, 正则, 风险标签)。命中即标记为冗余（非消解路径）。
DEFAULT_DISSOLVED_RATIO = 0.5  # 改动比例阈值（防"整文件重写"被当无关增量）

REDUNDANT_RULES: Tuple[Tuple[str, re.Pattern, str], ...] = (
    ("doc-append", re.compile(r"留痕|台账|复测|复评|口径|文档|docs/|\.md$"),
     "doc-append"),
    ("count-sync", re.compile(r"计数|count.*sync|计数同步"), "count-sync"),
    ("cosmetic", re.compile(r"注释|错别字|typo|格式化|flake8|E501|空白"), "cosmetic"),
    ("pure-markdown", re.compile(r"\.md$"), "doc-append"),
)

# 高风险（**非**冗余）：与审查/测试有效性直接相关，改动必须消解
DISOLVE_KEYWORDS = re.compile(
    r"fix|修复|bug|缺陷|回归|断言|不变量|守卫|校验|白名单|越权|穿越|"
    r"竞态|并发|锁|TOCTOU|fail-fast|安全|漏洞|逃逸",
    re.IGNORECASE)

# 测试路径判定
_TEST_DIR_PARTS = ("tests", "test")
_TEST_FILE_RE = re.compile(r"(^|/)test_[^/]+\.py$")


def looks_like_test_path(path: str) -> bool:
    """路径是否像测试文件（``tests/`` 目录内，或 ``test_*.py``）。"""
    if not path:
        return False
    norm = path.replace(os.sep, "/")
    base = os.path.basename(norm)
    if not base.endswith(".py"):
        return False          # 非 Python 文件不是测试（如 tests/fixture.json）
    if _TEST_FILE_RE.search(norm):
        return True
    parts = norm.split("/")
    # tests/ 目录内的 .py（含 conftest/helper）视为测试面
    return any(p in _TEST_DIR_PARTS for p in parts[:-1])


def is_redundant_label(text: str) -> str:
    """返回冗余风险标签（``""`` = 非冗余/须消解）。"""
    for _name, pattern, risk in REDUNDANT_RULES:
        if pattern.search(text or ""):
            return risk
    return ""


def classify_changed_path(path: str, is_new: bool,
                          changed_lines: int = 0,
                          depends_on: Sequence[str] = ()) -> SweepTarget:
    """把一个变更路径分类为增量的三态之一。

    Args:
        path: 变更文件路径（仓库相对）。
        is_new: 该文件在基线中**不存在**（git diff --diff-filter=A）。
        changed_lines: 该文件改动行数（用于冗余判定）。
        depends_on: 依赖的源文件（测试文件→被测模块）。

    Returns:
        :class:`SweepTarget`（``kind`` 为 applied 三态之一）。
    """
    if is_new:
        return SweepTarget(
            nodeid=path, kind=INCREMENT_ADDED, path=path,
            name=os.path.basename(path),
            reason="新增文件此前不存在 → 未跑过，必跑",
            risk="", refs=list(depends_on))

    risk = is_redundant_label(path)
    if risk and risk != "dissolve":
        return SweepTarget(
            nodeid=path, kind=INCREMENT_DISSOLVED, path=path,
            name=os.path.basename(path),
            reason=f"已跑过但内容已变（冗余标签: {risk}）→ 仅消解阈值触发才复跑",
            risk=risk)

    return SweepTarget(
        nodeid=path, kind=INCREMENT_DISSOLVED, path=path,
        name=os.path.basename(path),
        reason="已跑过但内容已变 → 复跑属消解（证据消解时执行）",
        risk="dissolve")


class SweepRules:
    """跑测规则集：新增/消解/冗余/兜底覆盖面。

    Attributes:
        test_root: 仓库内测试根（用于文件→用例展开）。
        dissolve_ratio: 改动比例阈值（默认 0.5）。
        redundant_labels: 冗余风险标签集合。
    """

    def __init__(self, test_root: str = "state-secretary/tests",
                 dissolve_ratio: float = DEFAULT_DISSOLVED_RATIO,
                 redundant_labels: Iterable[str] = ("doc-append", "count-sync",
                                                    "cosmetic")):
        self.test_root = test_root
        self.dissolve_ratio = float(dissolve_ratio)
        self.redundant_labels = set(redundant_labels)

    # ---- 增量分类 ----

    def classify(self, changed_files: Sequence[dict]) -> List[SweepTarget]:
        """把 git diff 变更清单分类为增量标的。

        Args:
            changed_files: ``[{"path":…, "is_new":bool, "changed_lines":int,
                              "added_tests":[name,…], "changed_tests":[name,…]}]``

        Returns:
            增量标的清单（已按"用例粒度"展开；含冗余剔除）。
        """
        out: List[SweepTarget] = []
        for item in changed_files:
            path = item.get("path", "")
            if not path:
                continue
            if not looks_like_test_path(path):
                continue  # 非测试文件不产生跑测标的（源改动由其测试覆盖兜底）
            base = classify_changed_path(
                path, bool(item.get("is_new")),
                int(item.get("changed_lines", 0)))
            added = list(item.get("added_tests") or [])
            changed = list(item.get("changed_tests") or [])
            if base.kind == INCREMENT_ADDED:
                names = added or [""]
                for n in names:
                    out.append(SweepTarget(
                        nodeid=self._nodeid(path, n),
                        kind=INCREMENT_ADDED, path=path, name=n,
                        reason=base.reason, risk=""))
                continue
            # 文件已存在：细粒度到用例
            if not added and not changed:
                # 指纹缺失 → 保守：整文件视为消解（宁多跑，不漏跑）
                out.append(SweepTarget(
                    nodeid=path, kind=INCREMENT_DISSOLVED, path=path,
                    name=os.path.basename(path),
                    reason="指纹缺失（浅克隆/截断）→ 文件级保守消解",
                    risk=base.risk))
                continue
            for n in added:
                out.append(SweepTarget(
                    nodeid=self._nodeid(path, n), kind=INCREMENT_ADDED,
                    path=path, name=n,
                    reason="用例此前不存在 → 新增，必跑", risk=""))
            for n in changed:
                out.append(SweepTarget(
                    nodeid=self._nodeid(path, n),
                    kind=INCREMENT_DISSOLVED, path=path, name=n,
                    reason="用例已跑过但其主体已改 → 消解", risk=base.risk))
        return out

    @staticmethod
    def _nodeid(path: str, name: str) -> str:
        return f"{path}::{name}" if name else path

    # ---- 冗余剔除 ----

    def is_redundant(self, target: SweepTarget) -> bool:
        """该标的是否属"冗余"（默认不重复跑）。

        仅对**消解候选**生效：新增永远不冗余（未跑过的断言面必须跑）。
        """
        if target.kind == INCREMENT_ADDED:
            return False
        return target.risk in self.redundant_labels

    # ---- 有效覆盖面兜底（可漏不漏，但不得因剔除而漏关键路径） ----

    def effective_coverage(self, available: Sequence[str],
                           chosen: Sequence[str],
                           guards: Sequence[str] = ()) -> List[str]:
        """有效覆盖面检查：剔除冗余后，须仍有覆盖各有效面的用例在计划内。

        Args:
            available: 可用用例 nodeid 全集。
            chosen: 本轮计划跑的 nodeid。
            guards: 必须被覆盖的有效面关键词（如 session/fold/handoff/scan）。

        Returns:
            需要补跑（兜底）的 nodeid 列表；若某有效面无任何 chosen 命中，
            则从 ``available`` 中取首个匹配该面的用例补入（**只补 1 个/面**，
            避免又变成全量冗余）。
        """
        supplements: List[str] = []
        chosen_set = set(chosen)
        for guard in guards:
            if any(guard in c for c in chosen_set):
                continue
            cand = next((a for a in available if guard in a), None)
            if cand and cand not in chosen_set:
                supplements.append(cand)
                chosen_set.add(cand)
        return supplements

    def merge_targets(self, *groups: Iterable[SweepTarget]
                      ) -> List[SweepTarget]:
        """合并多组标的（同 nodeid 去重；``added`` 优先于 ``dissolved``）。"""
        order = {INCREMENT_ADDED: 0, INCREMENT_DISSOLVED: 1,
                 INCREMENT_LEGACY: 2}
        merged: Dict[str, SweepTarget] = {}
        for group in groups:
            for t in group:
                old = merged.get(t.nodeid)
                if old is None or order[t.kind] < order[old.kind]:
                    merged[t.nodeid] = t
        return list(merged.values())


def changed_test_files(diff_entries: Sequence[dict],
                       test_root: str = "state-secretary/tests"
                       ) -> List[str]:
    """从 git diff --name-status 条目中挑出测试文件路径。"""
    out: List[str] = []
    for e in diff_entries:
        path = e.get("path", "")
        if not path:
            continue
        norm = path.replace(os.sep, "/")
        if norm.startswith(test_root.rstrip("/") + "/") or looks_like_test_path(norm):
            out.append(path)
    return out


DEFAULT_RULES = SweepRules()
