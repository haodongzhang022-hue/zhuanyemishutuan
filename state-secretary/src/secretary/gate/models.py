"""增量门禁数据模型。

术语（三态，权责一致的最小口径）：

- ``ADDED``       本轮**新增**的用例/文件 → **未跑过**，必须跑；
- ``DISSOLVED``   本轮**改动（非新增）**的用例/文件 → **已跑过但已失效**，
                  复跑属「消解」，只在证据消解时跑（不是"已跑过就不用跑"）；
- ``LEGACY``      与增量 diff **无关**的既有用例 → **此前已跑过**，默认不重复。

``LEGACY`` 即用户所说"全量时哪一些是默认已经跑过的，要给他标记出来"。
"""
from __future__ import annotations

import hashlib
from typing import Any, Dict, List, Optional

# 增量种类（枚举值即字符串，便于 JSON 落盘）
INCREMENT_ADDED = "added"
INCREMENT_DISSOLVED = "dissolved"
INCREMENT_LEGACY = "legacy"

INCREMENT_KINDS = (INCREMENT_ADDED, INCREMENT_DISSOLVED, INCREMENT_LEGACY)

# 用户口径：高重复/无消解风险的跑测标记为冗余（默认跳过）
REDUNDANT_BACKLOG = ("redundant", "low-quality-redundant")


def _stable_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


class SweepTarget:
    """一个跑测标的（= 一个测试用例节点，粒度到 nodeid）。

    Attributes:
        nodeid: pytest nodeid（``tests/x.py::Cls::test_y``）或文件路径。
        kind: added / dissolved / legacy。
        path: 文件相对路径。
        name: 用例名。
        seen_at: 消解基线（历史 head / 已跑标记提交）。
        reason: 判定理由（可读）。
        risk: 失效风险标签；命中冗余后援即"默认已跑过、不重复"。
        refs: 相关历史落痕（台账章节、提交）等引用。
    """

    __slots__ = ("nodeid", "kind", "path", "name", "seen_at", "reason",
                 "risk", "refs", "digest")

    def __init__(self, nodeid: str, kind: str, path: str = "",
                 name: str = "", seen_at: str = "", reason: str = "",
                 risk: str = "", refs: Optional[List[str]] = None):
        if kind not in INCREMENT_KINDS:
            raise ValueError(f"非法增量种类: {kind!r}（应为 {INCREMENT_KINDS}）")
        if not nodeid:
            raise ValueError("nodeid 不能为空")
        self.nodeid = nodeid
        self.kind = kind
        self.path = path
        self.name = name
        self.seen_at = seen_at
        self.reason = reason
        self.risk = risk
        self.refs = list(refs or [])
        self.digest = _stable_hash(nodeid)

    @property
    def is_redundant(self) -> bool:
        """是否属「冗余」：非消解路径（risk 未标记消解）→ 不重复跑。

        🟡9：唯一语义源为 :attr:`SweepRules.redundant_labels`（见
        :meth:`SweepRules.is_redundant`）；此处为**无规则集时**的便捷判定，
        供外部/CLI 单点使用，不再是第二套独立实现。
        """
        return self.risk not in ("", "dissolve")

    def to_dict(self) -> Dict[str, Any]:
        return {
            "nodeid": self.nodeid,
            "kind": self.kind,
            "path": self.path,
            "name": self.name,
            "seen_at": self.seen_at,
            "reason": self.reason,
            "risk": self.risk,
            "refs": list(self.refs),
            "digest": self.digest,
        }

    def __repr__(self) -> str:  # pragma: no cover - 调试用
        return f"<SweepTarget {self.kind} {self.nodeid}>"


class SweepPlan:
    """一次跑测计划（本轮**要跑什么**）。

    ``targets`` 只承载「要跑」的标的；被排除的既有项落在
    ``deferred`` / ``excluded``，供"全量告警"标记出来（默认不重复）。
    """

    def __init__(self, base: str = "", head: str = "WORKTREE",
                 test_root: str = "state-secretary/tests"):
        self.base = base
        self.head = head
        self.test_root = test_root
        self.targets: List[SweepTarget] = []
        self.deferred: List[SweepTarget] = []   # 已跑过、默认不重复（增量）
        self.excluded: List[SweepTarget] = []   # 标记为冗余，不跑
        self.note: List[str] = []

    # ---- 统计（权责一致口径） ----

    @property
    def planned_count(self) -> int:
        """本轮计划的用例数（= 真实要跑的数量）。"""
        return len(self.targets)

    @property
    def deferred_count(self) -> int:
        return len(self.deferred)

    @property
    def excluded_count(self) -> int:
        return len(self.excluded)

    @property
    def planned_files(self) -> List[str]:
        out: List[str] = []
        for t in self.targets:
            if t.path and t.path not in out:
                out.append(t.path)
        return out

    def count_by_kind(self, kind: str) -> int:
        return sum(1 for t in self.targets if t.kind == kind)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "base": self.base,
            "head": self.head,
            "test_root": self.test_root,
            "planned_count": self.planned_count,
            "deferred_count": self.deferred_count,
            "excluded_count": self.excluded_count,
            "planned_files": self.planned_files,
            "by_kind": {k: self.count_by_kind(k) for k in INCREMENT_KINDS},
            "targets": [t.to_dict() for t in self.targets],
            "deferred": [t.to_dict() for t in self.deferred],
            "excluded": [t.to_dict() for t in self.excluded],
            "note": list(self.note),
        }

    def render(self) -> str:
        """过程可视化：人可读的计划书（本轮要跑多少 / 为什么）。"""
        lines = [
            "== 增量跑测计划（权责一致：计划多少 = 跑多少）==",
            f"  基线 base : {self.base or '(无)'}",
            f"  目标 head : {self.head}",
            f"  计划跑测  : {self.planned_count} 例"
            f"（新增 {self.count_by_kind(INCREMENT_ADDED)}"
            f" / 消解 {self.count_by_kind(INCREMENT_DISSOLVED)}）",
            f"  已跑过不重复: {self.deferred_count} 例（默认跳过，见全量告警）",
            f"  冗余剔除  : {self.excluded_count} 例",
        ]
        if self.planned_files:
            lines.append("  计划文件:")
            for p in self.planned_files:
                n = sum(1 for t in self.targets if t.path == p)
                lines.append(f"    - {p}（{n} 例）")
        for n in self.note:
            lines.append(f"  备注: {n}")
        return "\n".join(lines)


class SweepReport:
    """一次增量跑测的结果（本轮**实际跑了什么**）。

    复评第 1 轮定点修复（🔴-1）：``executed`` 与各项计数**全部来自 pytest
    真实执行**（junitxml 结构化结果；缺失时回退汇总行计数），不再由计划
    构造式赋值；``green`` 纳入 ``exit_code``（rc≠0 不得为绿）。
    """

    def __init__(self, plan: SweepPlan):
        self.plan = plan
        self.executed: List[str] = []      # 真实执行的 nodeid（junitxml 口径）
        self.passed: List[str] = []
        self.failed: List[str] = []
        self.errors: List[str] = []
        self.skipped: List[str] = []
        self.counts: Dict[str, int] = {}   # 结构化汇总（权威计数）
        self.counts_verified = False       # 是否来自 junitxml（可复核）
        self.duration_sec: float = 0.0
        self.exit_code: Optional[int] = None
        self.raw_output: str = ""          # pytest 原始输出（事后可复核）
        self.command: str = ""             # 实际执行的命令行
        self.summary_line: str = ""        # pytest 汇总行（如 "36 passed in 1.2s"）
        self.suite_total: int = 0          # 本轮 pytest 实收用例数（汇总口径）

    # ---- 权威计数（真实实跑，非计划推断） ----

    @property
    def executed_count(self) -> int:
        """本轮**真实实跑**用例数（通过+失败+错误+跳过）。"""
        if self.counts_verified:
            return sum(self.counts.values())
        return len(self.executed)

    @property
    def passed_count(self) -> int:
        return self.counts.get("passed", len(self.passed))

    @property
    def failed_count(self) -> int:
        return self.counts.get("failed", len(self.failed))

    @property
    def errors_count(self) -> int:
        return self.counts.get("errors", len(self.errors))

    @property
    def skipped_count(self) -> int:
        return self.counts.get("skipped", len(self.skipped))

    @property
    def collected_count(self) -> int:
        """本轮 pytest 实收用例数（含 skip；无结构化结果时退化为实跑数）。"""
        return self.suite_total or self.executed_count

    @property
    def redundant_saved(self) -> int:
        """本轮省下的重复跑测数（= 已跑过 + 冗余剔除）。"""
        return self.plan.deferred_count + len(self.plan.excluded)

    @property
    def counter_claim(self) -> "CounterClaim":
        """计数主张：**跑多少写多少**（不得写"全量"）。"""
        return CounterClaim(
            executed=self.executed_count,
            planned=self.plan.planned_count,
            deferred=self.plan.deferred_count,
            redundant=len(self.plan.excluded),
            total_suite=self.plan.planned_count + self.plan.deferred_count
            + len(self.plan.excluded),
        )

    @property
    def green(self) -> bool:
        """通过信号灯：**须同时**无失败/无错误 **且** ``exit_code == 0``。

        🔴-1：旧实现只看 failed/errors 列表 → pytest rc=4（not found）时
        列表为空即报 green=True（假绿）。rc 必须参与判定。
        """
        return (self.failed_count == 0 and self.errors_count == 0
                and self.exit_code == 0)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "plan": self.plan.to_dict(),
            "executed": list(self.executed),
            "executed_count": self.executed_count,
            "counts": dict(self.counts),
            "counts_verified": self.counts_verified,
            "suite_total": self.suite_total,
            "passed": self.passed_count,
            "failed": list(self.failed),
            "errors": list(self.errors),
            "skipped": list(self.skipped),
            "duration_sec": round(self.duration_sec, 3),
            "exit_code": self.exit_code,
            "summary_line": self.summary_line,
            "command": self.command,
            "redundant_saved": self.redundant_saved,
            "counter": self.counter_claim.to_dict(),
            "green": self.green,
        }

    def render(self) -> str:
        """过程可视化：本轮实际跑了多少（**不写"全量"**）。"""
        c = self.counter_claim
        lines = [
            "== 增量跑测结果 ==",
            f"  本轮实跑: {c.executed} 例"
            f"（通过 {self.passed_count} / 失败 {self.failed_count}"
            f" / 错误 {self.errors_count} / 跳过 {self.skipped_count}）",
            f"  跳过重复: {self.plan.deferred_count} 例已跑过 + "
            f"{len(self.plan.excluded)} 例冗余（合计 {self.redundant_saved} 例省下）",
            f"  全量规模: {c.total_suite} 例"
            f"（如写全量须标记 {self.plan.deferred_count} 例已跑过）",
            f"  计数口径: {c.render()}",
            f"  退出码  : {self.exit_code}（rc≠0 不得报绿）",
            f"  pytest  : {self.summary_line or '(无汇总)'}",
            f"  信号灯  : {'🟢 通过' if self.green else '🔴 未通过'}",
        ]
        return "\n".join(lines)


class FullSweepReport:
    """全量跑测告警视图：把「此前已跑过的（默认不重复）」显式标记出来。

    用户口径：不许把"全量 743 个"读成"本轮跑了 743 个"。故全量视图必须
    逐项标注 ``previously_run``，并按本轮实际跑过的集合与全量集合做差，
    给出「已跑过 / 本轮实跑 / 未消解差异」三个数。
    """

    def __init__(self, all_nodeids: List[str], previously_run: List[str],
                 executed_now: List[str], base: str = ""):
        all_set = list(dict.fromkeys(all_nodeids))
        run_set = set(previously_run)
        now_set = set(executed_now)
        self.base = base
        self.total = len(all_set)
        self._unlabelled = False
        self.previously_run = [n for n in all_set if n in run_set]
        self.executed_now = [n for n in all_set if n in now_set]
        self.remaining = [n for n in all_set
                          if n not in run_set and n not in now_set]
        self.items = [
            {
                "nodeid": n,
                "previously_run": n in run_set,
                "executed_now": n in now_set,
                "undissolved_gap": (n not in run_set and n not in now_set),
            }
            for n in all_set
        ]

    @property
    def previously_run_count(self) -> int:
        """已跑过（默认不重复）用例数——须被显式标注（用户口径 #4）。"""
        return 0 if self._unlabelled else len(self.previously_run)

    @property
    def is_first_full_run(self) -> bool:
        """首轮全量（无历史基线）：已跑过 0 且本轮实跑 == 全量。

        🟡7：首次接入门禁（``previously_run == 0``）不得被误拒。
        """
        return (not self._unlabelled and self.previously_run_count == 0
                and self.total > 0 and self.executed_now_count == self.total)

    def mask_previously_run(self) -> "FullSweepReport":
        """**反例构造用**：抹掉"已跑过"标记（模拟未标记的全量上报）。

        门禁须拒绝此类视图（``IncrementalGate.assert_full_view``）。
        """
        self._unlabelled = True
        return self

    @property
    def executed_now_count(self) -> int:
        """本轮实跑用例数（**不得**被写成全量数）。"""
        return len(self.executed_now)

    def counter_claim(self) -> "CounterClaim":
        return CounterClaim(
            executed=self.executed_now_count,
            planned=self.executed_now_count,
            deferred=self.previously_run_count,
            redundant=len(self.remaining),
            total_suite=self.total,
        )

    def render(self) -> str:
        c = self.counter_claim()
        lines = [
            "== 全量跑测告警（已跑过项须标记）==",
            f"  全量用例: {self.total} 例",
        ]
        if self._unlabelled:
            lines.append("  已跑过（默认不重复）: **未标记** ← 违规，须拒绝上报")
        elif self.is_first_full_run:
            lines.append("  已跑过（默认不重复）: 0 例（**首轮全量**：无历史基线）")
        else:
            lines.append(
                f"  已跑过（默认不重复）: {self.previously_run_count} 例 ← 须标记")
        lines += [
            f"  本轮实跑: {self.executed_now_count} 例",
            f"  未消解差异: {len(self.remaining)} 例",
            f"  计数口径: {c.render()}",
            "  ⚠ 不得把上述全量数当作「本轮实跑数」上报。",
        ]
        return "\n".join(lines)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "base": self.base,
            "total": self.total,
            "previously_run_count": self.previously_run_count,
            "executed_now_count": self.executed_now_count,
            "remaining_count": len(self.remaining),
            "items": self.items,
            "counter": self.counter_claim().to_dict(),
        }


class CounterClaim:
    """计数主张：把「跑了多少」钉死成可校验的一个数（权责一致）。

    - ``executed``  本轮实跑；
    - ``planned``   本轮计划跑；
    - ``deferred``  已跑过、默认不重复；
    - ``redundant`` 冗余剔除；
    - ``total_suite`` 全量规模。
    """

    def __init__(self, executed: int, planned: Optional[int] = None,
                 deferred: int = 0, redundant: int = 0,
                 total_suite: int = 0):
        self.executed = int(executed)
        # 🟡4：planned 显式传 0 不得被 `or` 静默改成 executed；
        # 缺省（None）才回退为 executed。
        self.planned = int(executed) if planned is None else int(planned)
        self.deferred = int(deferred)
        self.redundant = int(redundant)
        self.total_suite = int(total_suite) or (
            self.executed + self.deferred + self.redundant)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "executed": self.executed,
            "planned": self.planned,
            "deferred": self.deferred,
            "redundant": self.redundant,
            "total_suite": self.total_suite,
        }

    def render(self) -> str:
        return (f"本轮实跑 {self.executed} 例"
                f"（计划 {self.planned}）"
                f"· 已跑过不重复 {self.deferred}"
                f"· 冗余剔除 {self.redundant}"
                f"· 全量规模 {self.total_suite}")

    def __eq__(self, other: Any) -> bool:
        return isinstance(other, CounterClaim) and self.to_dict() == other.to_dict()
