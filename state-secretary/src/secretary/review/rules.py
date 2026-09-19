"""评审仲裁判定规则（可替换、纯函数式）

把「谁和谁同侧」和「哪些状态算过渡态」从仲裁主流程里剥离出来，便于单测与按团队
口径替换（不同团队的评审/开发命名习惯不同，判定规则属于**策略**而非机制）。

判定原则（与 Issue #14 口径一致）：
  - 能明确判出同侧才判同侧（保守），判不出 → 不判冲突（宽进，以新为准）；
  - 只依赖**确定性关键词**，不猜语义、不看自然语言结论。
"""
from __future__ import annotations

from typing import Optional

# 评审侧关键词：命中即视为"评审/复核"方向
REVIEW_KEYWORDS = ("review", "reviewer", "评审", "复评", "复核", "审查")

# 开发侧关键词：命中即视为"开发/实现"方向
DEV_KEYWORDS = ("dev", "developer", "开发", "实现", "修复", "fix")

# 过渡态：写入落在这里不算"越过门禁完成态"（同向写入仍可延迟重试）
TRANSITION_STATES = ("todo", "in_progress", "blocked", "stopped", "failed", "cancelled")


def _norm(name: str) -> str:
    """归一化标识：小写、去空白、去分隔符（`秘书·评审` → `秘书评审`）。"""
    if not isinstance(name, str):
        return ""
    out = name.strip().lower()
    for ch in (" ", "\t", "_", "-", ".", "·", "/", ":"):
        out = out.replace(ch, "")
    return out


def classify(name: str) -> str:
    """把写者标识归类为 ``review`` / ``dev`` / ``""``（判不出）。"""
    n = _norm(name)
    if not n:
        return ""
    for kw in REVIEW_KEYWORDS:
        if _norm(kw) in n:
            return "review"
    for kw in DEV_KEYWORDS:
        if _norm(kw) in n:
            return "dev"
    return ""


def is_same_side(a: str, b: str) -> bool:
    """两个写者是否同侧（同判别不出 → ``False``，保守不判冲突）。"""
    ca, cb = classify(a), classify(b)
    return bool(ca) and ca == cb


def is_transition_state(state: Optional[str]) -> bool:
    """该状态是否属于过渡态（未越过门禁完成态）。"""
    return isinstance(state, str) and state in TRANSITION_STATES
