"""review: 评审版本仲裁 + 审查范围分级

- 版本仲裁（`review_arbiter`）：只在开启评审时记录版本 + 防同写锁 + 延迟重试 + 单冲突收口
- 范围分级（`scope`）：**日常只审变化，全量只在大版本**（防止无效构建与测试）
"""
from .rules import classify, is_same_side, is_transition_state
from .review_arbiter import (
    NO_BASELINE,
    NO_CONFLICT,
    STALE_BASE,
    VERSION_REGRESS,
    ArbiterDecision,
    ConflictCard,
    ReviewArbiter,
    ReviewBaseline,
)
from .scope import FULL, SCAN, ScanContext, decide_scan, scan_commands

__all__ = [
    "ReviewArbiter",
    "ReviewBaseline",
    "ConflictCard",
    "ArbiterDecision",
    "NO_CONFLICT",
    "STALE_BASE",
    "VERSION_REGRESS",
    "NO_BASELINE",
    "classify",
    "is_same_side",
    "is_transition_state",
    "ScanContext",
    "decide_scan",
    "scan_commands",
    "SCAN",
    "FULL",
]
