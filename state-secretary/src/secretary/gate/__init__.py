"""增量门禁（IncrementalGate）：只跑有效增量，杜绝全量冗余。

用户口径（Issue #14）：
- 原则是**只跑增量**：可以漏（后续用时会重新提需求去改），但不得大量重复冗余；
- 「大量冗余保一个低质量测试」= 资源错配，会挤占/阻断真正有效的审查与测试；
- 计数**权责一致**：跑多少写多少（本轮跑 59 个就写 59，不得写"全量"）；
- 全量跑测须**标记**哪些是"此前已跑过的（默认不重复）"，避免 743 个被读成
  "本轮跑了 743 个"；
- 过程可视化。
"""
from .models import (
    INCREMENT_ADDED, INCREMENT_DISSOLVED, INCREMENT_LEGACY,
    SweepTarget, SweepPlan, SweepReport, FullSweepReport, CounterClaim,
    INCREMENT_KINDS,
)
from .counter import assert_counter, CounterWordingError, render_count
from .incremental import (
    IncrementalGate, GateError, JUnitParseError, looks_like_test_path,
)
from .rules import (
    SweepRules, DEFAULT_RULES, DEFAULT_DISSOLVED_RATIO,
    classify_changed_path, changed_test_files,
)

__all__ = [
    "INCREMENT_ADDED", "INCREMENT_DISSOLVED", "INCREMENT_LEGACY",
    "INCREMENT_KINDS",
    "SweepTarget", "SweepPlan", "SweepReport", "FullSweepReport",
    "CounterClaim",
    "assert_counter", "CounterWordingError", "render_count",
    "IncrementalGate", "GateError", "JUnitParseError", "looks_like_test_path",
    "SweepRules", "DEFAULT_RULES", "DEFAULT_DISSOLVED_RATIO",
    "classify_changed_path", "changed_test_files",
]
