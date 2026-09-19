"""阶梯退避（StepBackoff）：从少到多的等待阶梯，封顶即停

## 需求口径（Issue #14 追问，用户）

> 从少到多符合逻辑，因为大部分都是很快就没问题了，所以要不**第一次的设计到 10 秒，
> 然后 30 秒，60 秒，3 分钟，6 分钟，持续增加到 10 分钟就停止**。怎么样

拆成四条可执行口径：

| # | 口径 | 落地 |
|---|---|---|
| 1 | **从少到多** | 阶梯**定值**（10/30/60/180/360/600），不是"每次 +N"的线性/倍率推算 |
| 2 | **大部分很快就没问题** | 前两档很短（10s → 30s）：多数冲突在第 1 次检测就消失，**不为少数长尾付等待成本** |
| 3 | **持续增加到 10 分钟就停止** | 到顶后**停在 10 分钟**重复（不是"再涨"），并给出 `stopped=True`；本地挂账次数仍有硬上界（`max_attempts`） |
| 4 | **不许缩短承诺** | 单调不减：重触发不得把已承诺的等待改小（"等 3 分钟"不能被下一轮降回 10 秒） |

**为什么不用倍率**：倍率（×2 或 ×3）是"指数爆炸"，3 分钟档之后一步就跨过 10 分钟，
落不到用户给的这串数字上；定值阶梯才能既"从少到多"又"精确收在 10 分钟"。

## 与"没有 CPU 轮询"的关系

阶梯一旦到底（10 分钟）就**停止**：本模块只负责算出"下一次什么时候看"，
**不负责等**。等待由平台承担（不在本地 sleep/轮询）——让出资源，不占构建 CPU。

## 用法

    bo = StepBackoff()
    bo.delay_for(0)      # 10.0
    bo.delay_for(1)      # 30.0
    bo.delay_for(5)      # 600.0（顶）
    bo.delay_for(99)     # 600.0（仍停在顶）
    bo.schedule()        # [{step, delay_sec, stopped}, ...] 全阶梯（给人看/可上报）
    bo.status(3)         # {attempt, delay_sec, stopped, remaining_attempts, ladder}
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple

# 默认阶梯：10s → 30s → 60s → 3min → 6min → 10min（到顶即停）
DEFAULT_LADDER: Tuple[float, ...] = (10.0, 30.0, 60.0, 180.0, 360.0, 600.0)

# 到顶后仍要重复的硬上界（本地挂账次数；到顶"停止"由阶梯语义保证，
# 本上界只防"同一把锁被无限次重新挂起"导致台账/票据常驻）
DEFAULT_MAX_ATTEMPTS = 6

# 阶梯语义标签（供人读，避免只看到裸秒数）
_STEP_LABEL = {
    10.0: "10s（首次，极短）",
    30.0: "30s",
    60.0: "60s（1 分钟）",
    180.0: "180s（3 分钟）",
    360.0: "360s（6 分钟）",
    600.0: "600s（10 分钟，顶）",
}


def _label(sec: float) -> str:
    """给一档等待加人类可读标签（未在预设内的档位用秒数标注）。"""
    return _STEP_LABEL.get(float(sec), f"{float(sec):.0f}s")


@dataclass
class BackoffStatus:
    """一次退避的状态快照（可上报、可留痕）。"""

    attempt: int = 0                # 第几次检测（0 = 首次）
    delay_sec: float = 0.0          # 本次等待时长（= `delay_for(attempt)`）
    next_delay_sec: float = 0.0     # 下一次等待时长（若还有下一次）
    at_top: bool = False            # 是否已到阶梯顶
    stopped: bool = False           # 是否"到顶即停"（不再涨、不再重新挂起）
    exhausted: bool = False         # 本地挂账次数是否用尽（到顶后重复上界）
    remaining_attempts: int = 0     # 到顶后还剩几次检测（未到顶 = 无意义的大数）
    ladder: List[float] = field(default_factory=list)
    label: str = ""
    reason: str = ""

    def to_dict(self) -> dict:
        return {
            "attempt": self.attempt,
            "delay_sec": self.delay_sec,
            "next_delay_sec": self.next_delay_sec,
            "at_top": self.at_top,
            "stopped": self.stopped,
            "exhausted": self.exhausted,
            "remaining_attempts": self.remaining_attempts,
            "ladder": list(self.ladder),
            "label": self.label,
            "reason": self.reason,
        }


class StepBackoff:
    """阶梯退避计算器（纯计算，**不 sleep、不轮询**）。

    Args:
        ladder: 阶梯（秒），默认 ``10/30/60/180/360/600``；
            必须**单调不减**且每档 > 0（否则"等待可能被缩短"，拒绝构造）
        max_attempts: 本地挂账次数硬上界（默认 6，恰好覆盖整条默认阶梯）
    """

    def __init__(
        self,
        ladder: Optional[Sequence[float]] = None,
        max_attempts: int = DEFAULT_MAX_ATTEMPTS,
    ):
        raw = tuple(float(x) for x in (DEFAULT_LADDER if ladder is None else ladder))
        if not raw:
            raise ValueError("阶梯不可为空。")
        for sec in raw:
            if sec <= 0:
                raise ValueError(f"非法阶梯档位: {sec!r}（须为正数秒）。")
        for prev, cur in zip(raw, raw[1:]):
            if cur < prev:
                raise ValueError(
                    f"阶梯必须单调不减（从少到多）: {prev} → {cur}；"
                    "缩短等待会破坏「已承诺的等待不可回退」口径。")
        if not isinstance(max_attempts, int) or isinstance(max_attempts, bool) \
                or max_attempts < 1:
            raise ValueError(f"非法 max_attempts: {max_attempts!r}（须为 ≥1 的整数）。")
        self.ladder: Tuple[float, ...] = raw
        self.max_attempts = int(max_attempts)

    # ------------------------------------------------------------------
    # 核心：第 N 次检测该等多久
    # ------------------------------------------------------------------

    @property
    def top(self) -> float:
        """阶梯顶（最后一次等待时长）= "持续增加到 10 分钟就停止"的那 10 分钟。"""
        return self.ladder[-1]

    def delay_for(self, attempt: int = 0) -> float:
        """第 ``attempt`` 次检测（0 起算）应等待的秒数；到顶后**停在顶**。"""
        idx = max(0, int(attempt))
        if idx >= len(self.ladder):
            return self.top
        return self.ladder[idx]

    def is_top(self, attempt: int = 0) -> bool:
        """该次是否已到阶梯顶（含之后的所有重复档）。"""
        return max(0, int(attempt)) >= len(self.ladder) - 1

    def status(self, attempt: int = 0) -> BackoffStatus:
        """该次退避的完整状态（含 `stopped` / 剩余次数 / 下次时长）。"""
        idx = max(0, int(attempt))
        delay = self.delay_for(idx)
        at_top = self.is_top(idx)
        nxt = self.delay_for(idx + 1)
        exhausted = idx + 1 >= self.max_attempts
        stopped = at_top            # "到顶即停"：不再增长仍可重复，但不再重挂
        reason = (
            "已达阶梯顶（10 分钟）→ 停止增长；到顶后的重复受 max_attempts 约束"
            if at_top else
            f"阶梯第 {idx + 1}/{len(self.ladder)} 档：{_label(delay)}"
        )
        return BackoffStatus(
            attempt=idx,
            delay_sec=delay,
            next_delay_sec=nxt,
            at_top=at_top,
            stopped=stopped,
            exhausted=exhausted,
            remaining_attempts=(max(0, self.max_attempts - (idx + 1))
                                if at_top else self.max_attempts),
            ladder=[float(x) for x in self.ladder],
            label=_label(delay),
            reason=reason,
        )

    # ------------------------------------------------------------------
    # 重触发：等待只增不减（已承诺的等待不可回退）
    # ------------------------------------------------------------------

    def retrigger(self, attempt: int = 0, previous_wait_sec: float = 0.0) -> float:
        """**再次触发**同一把锁 → 返回应等待的秒数（阶梯下一档，且**不短于**已承诺）。

        "大部分很快就没问题"的代价不能由少数长尾场景转移给"重触发者"：
        重触发一律往**更长**的档位走；即便调用方给了一个很长的上一档，
        也不会被拉回短档（单调不减）。
        """
        idx = max(0, int(attempt))
        if idx + 1 < len(self.ladder):
            wanted = self.ladder[idx + 1]
        else:
            wanted = self.top
        return max(float(previous_wait_sec or 0.0), wanted)

    def capped(self, delay_sec: float) -> float:
        """把任意等待**收敛到阶梯顶以内**（不允许超过 10 分钟）。"""
        sec = float(delay_sec)
        return self.top if sec > self.top else sec

    # ------------------------------------------------------------------
    # 视图
    # ------------------------------------------------------------------

    def schedule(self, attempts: Optional[int] = None) -> List[Dict[str, Any]]:
        """全阶梯时间表（给人看/可上报）：第几次、等多久、是否到顶/停止。"""
        n = self.max_attempts if attempts is None else max(1, int(attempts))
        return [self.status(i).to_dict() for i in range(n)]

    def human(self) -> str:
        """一行人类可读阶梯：``10s → 30s → … → 600s（顶后停止）``。"""
        body = " → ".join(_label(x) for x in self.ladder)
        return f"{body}（到顶即停，最多检测 {self.max_attempts} 次）"

    def to_dict(self) -> dict:
        return {
            "ladder": [float(x) for x in self.ladder],
            "top_sec": self.top,
            "max_attempts": self.max_attempts,
            "human": self.human(),
        }

    @classmethod
    def from_dict(cls, data: Any) -> Optional["StepBackoff"]:
        """从配置字典构造（形状不符 → None，宽进不炸）。"""
        if not isinstance(data, dict):
            return None
        ladder = data.get("ladder")
        max_attempts = data.get("max_attempts", DEFAULT_MAX_ATTEMPTS)
        if not isinstance(max_attempts, int) or isinstance(max_attempts, bool):
            max_attempts = DEFAULT_MAX_ATTEMPTS
        try:
            return cls(ladder=ladder if isinstance(ladder, (list, tuple)) else None,
                       max_attempts=max_attempts)
        except ValueError:
            return None
