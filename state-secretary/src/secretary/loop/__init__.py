"""loop: 循环熔断守卫（轮次预算 + 非必要修复熔断 + 静默交接）"""
from .loop_guard import (
    LoopAction,
    LoopDecision,
    LoopGuard,
    LoopState,
    DEFAULT_MAX_ROUNDS,
    DEFAULT_MAX_ADVISORY_FIX_ROUNDS,
    DEFAULT_MAX_STALL_REPEATS,
)

__all__ = [
    "LoopAction",
    "LoopDecision",
    "LoopGuard",
    "LoopState",
    "DEFAULT_MAX_ROUNDS",
    "DEFAULT_MAX_ADVISORY_FIX_ROUNDS",
    "DEFAULT_MAX_STALL_REPEATS",
]
