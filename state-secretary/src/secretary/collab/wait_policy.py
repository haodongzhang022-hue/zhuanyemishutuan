"""等待策略（WaitPolicy）：等待**交给平台挂起**，本地不做 CPU 轮询

## 需求（Issue #14 追问）

> 另外这个等待会不会占用 CPU 构建 CPU 计算时间呀，平台会根据这个收费，
> 如果一个 agent 是这样长时间等待会不会消耗，还是被挂起的等待

平台口径（见 `shared/collab/collab-rules.yaml` 的 ``platform`` 段，来源为
CNB 官方文档 `/zh/pricing.md`、`/zh/build/timeout.md`、
`/zh/build/internal-steps/cnb/await-resolve.md`）：

- 计费项只有 **云原生构建/开发 CPU 核时**（按任务实际运行时长，每 5min 冻结
  `5min × 节点规格`）与 **AI Credits**（按模型调用 Token）；
- **没有"等待"这一计费项**：走平台等待/挂起语义（如 `cnb:await`）不产生核时；
- 真正烧钱的是**本地 `sleep` 与空转轮询**：8 核空转 10 分钟 ≈ 1.33 核时（约 ¥0.17）。

因此本模块把"怎么等"固化成三种**明确**模式，禁止第四种（本地循环）：

| 模式 | 含义 | 本地 CPU |
|---|---|---|
| `platform_suspend` | 挂到平台等待语义上（`cnb:await` / 等下一次事件），**进程可被回收** | 0（不占构建核时） |
| `deferred_review` | 「先做其他，并挂锁」：不等，放下这件事去做别的 | 0（在干真活） |
| `resume_required` | 阶梯到顶且到顶次数用尽 → **交回人工/上游**，不再自动挂账 | 0 |

三种模式的共同点：**都不在本地空转**。这也解释了为什么"阶梯到顶即停"是安全的——
到顶不是"继续等 10 分钟 ×N"，而是"停止自动挂账，把主动权交回人"。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from .backoff import StepBackoff
from .rules import PlatformCostNote

# 等待模式
PLATFORM_SUSPEND = "platform_suspend"   # 平台挂起（cnb:await 等），不占 CPU
DEFERRED_REVIEW = "deferred_review"     # 先做其他，并挂锁（不等，去干别的）
RESUME_REQUIRED = "resume_required"     # 到顶且用尽 → 交回人工/上游

ALL_WAIT_MODES = (PLATFORM_SUSPEND, DEFERRED_REVIEW, RESUME_REQUIRED)

# 本地空转的代价示例（写进口径，便于人一眼看到"为什么不能 sleep"）
CPU_PER_CORE_HOUR_YUAN = 0.125


@dataclass
class WaitDecision:
    """一次"该怎么等"的结论（给角色/流水线执行用）。"""

    mode: str = DEFERRED_REVIEW
    delay_sec: float = 0.0
    step: int = 0
    stopped: bool = False
    resume_required: bool = False
    local_cpu_cost: str = "0（不占构建核时）"
    guidance: str = ""
    suspend_hint: str = ""
    counterfactual: str = ""     # 反向对照：若本地空转，要花多少

    def to_dict(self) -> dict:
        return {
            "mode": self.mode,
            "delay_sec": self.delay_sec,
            "step": self.step,
            "stopped": self.stopped,
            "resume_required": self.resume_required,
            "local_cpu_cost": self.local_cpu_cost,
            "guidance": self.guidance,
            "suspend_hint": self.suspend_hint,
            "counterfactual": self.counterfactual,
        }


class WaitPolicy:
    """把"等多久"翻译成"怎么等"（**永远不翻译成"本地 sleep"**）。

    Args:
        backoff: 阶梯退避（决定等待时长与是否到顶）
        platform: 平台计费口径（用于生成"等待不计 CPU"的说明）
        cores: 构建节点核数（仅用于算**反例**"若本地空转要花多少"，不参与决策）
    """

    def __init__(
        self,
        backoff: Optional[StepBackoff] = None,
        platform: Optional[PlatformCostNote] = None,
        cores: int = 8,
    ):
        self.backoff = backoff or StepBackoff()
        self.platform = platform or PlatformCostNote()
        self.cores = int(cores) if isinstance(cores, int) and cores > 0 else 8

    # ------------------------------------------------------------------
    # 决策
    # ------------------------------------------------------------------

    def decide(self, attempt: int = 0, *, editing: bool = True) -> WaitDecision:
        """给第 ``attempt`` 次检测决定"怎么等"。

        - 对方已处理完（``editing=False``）→ 无需等待（延迟 0，去做评审）；
        - 未到顶 → **平台挂起**到下一档到点（不占 CPU）；
        - 到顶且到顶次数用尽 → ``resume_required``：停止自动挂账，交回人工/上游。
        """
        st = self.backoff.status(attempt)
        if not editing:
            return WaitDecision(
                mode=DEFERRED_REVIEW,
                delay_sec=0.0,
                step=st.attempt,
                stopped=False,
                resume_required=False,
                local_cpu_cost="0（不占构建核时）",
                guidance="对方已处理完：直接做评审，无需等待",
                suspend_hint=self.platform.pause_hint,
                counterfactual=self._counterfactual(0.0),
            )
        if st.exhausted and st.at_top:
            return WaitDecision(
                mode=RESUME_REQUIRED,
                delay_sec=0.0,
                step=st.attempt,
                stopped=True,
                resume_required=True,
                local_cpu_cost="0（不占构建核时）",
                guidance=("阶梯已到顶（10 分钟）且到顶次数用尽 → **停止自动挂账**，"
                          "把此事交回人工/上游处置；不得据此再开一轮空等"),
                suspend_hint=self.platform.pause_hint,
                counterfactual=self._counterfactual(0.0),
            )
        return WaitDecision(
            mode=PLATFORM_SUSPEND,
            delay_sec=st.delay_sec,
            step=st.attempt,
            stopped=st.stopped,
            resume_required=False,
            local_cpu_cost="0（不占构建核时）",
            guidance=(f"挂到平台等待语义上，{st.label} 后检测；"
                      "本地不做 sleep、不做轮询（那才在烧核时）"),
            suspend_hint=self.platform.pause_hint,
            counterfactual=self._counterfactual(st.delay_sec),
        )

    def plan(self, attempts: Optional[int] = None) -> List[Dict[str, Any]]:
        """整条等待计划（每次到点"怎么等"），可直接给文档/角色提示词引用。"""
        n = self.backoff.max_attempts if attempts is None else max(1, int(attempts))
        return [self.decide(i).to_dict() for i in range(n)]

    # ------------------------------------------------------------------
    # 口径说明（所有角色共用一段话，避免各说一套）
    # ------------------------------------------------------------------

    def _counterfactual(self, delay_sec: float) -> str:
        """反例：同样时长若在本地空转，要烧多少核时／多少钱。"""
        if delay_sec <= 0:
            return "等价本地空转会白烧核时；本策略不做本地空转"
        hours = float(delay_sec) / 3600.0
        core_hours = self.cores * hours
        cost = core_hours * CPU_PER_CORE_HOUR_YUAN
        return (f"若改成本地空转 {delay_sec:.0f}s：{self.cores} 核 ×"
                f"{hours * 60:.0f}min ≈ {core_hours:.2f} 核时（约 ¥{cost:.3f}）；"
                "平台挂起为 0")

    def explain(self) -> Dict[str, Any]:
        """回答"等待会不会占 CPU 计费"的完整口径（可直接回评论）。"""
        return {
            "question": "长时间等待会不会消耗构建 CPU / 被平台计费？",
            "answer": ("**等待本身不计 CPU**：平台计费项只有「云原生构建/开发 CPU 核时」"
                       "（按任务实际运行时长）与「AI Credits」（按模型调用 Token），"
                       "没有「等待」这一项。前提是等待走了平台等待/挂起语义，"
                       "没有在本地 sleep、空转轮询或持续产出日志。"),
            "platform": self.platform.to_dict(),
            "modes": {
                PLATFORM_SUSPEND: "挂到平台等待语义（如 cnb:await），不占构建核时",
                DEFERRED_REVIEW: "先做其他，并挂锁：不空等，去干别的",
                RESUME_REQUIRED: "到顶即停：交回人工/上游，不再自动挂账",
            },
            "forbidden": "本地 sleep / 空转轮询 / 无意义心跳保活（这些在烧核时）",
            "ladder": self.backoff.human(),
        }

    def to_dict(self) -> dict:
        return {
            "ladder": self.backoff.to_dict(),
            "cores": self.cores,
            "explain": self.explain(),
        }


def cpu_cost_of_sleep(seconds: float, cores: int = 8,
                      yuan_per_core_hour: float = CPU_PER_CORE_HOUR_YUAN) -> dict:
    """算"若本地空转 N 秒"的 CPU 代价（用于口径对照，不用于决策）。"""
    sec = max(0.0, float(seconds))
    core_hours = float(cores) * (sec / 3600.0)
    return {
        "seconds": sec,
        "cores": int(cores),
        "core_hours": round(core_hours, 4),
        "yuan": round(core_hours * float(yuan_per_core_hour), 4),
        "note": "仅作反例对照：本策略不做本地空转，等待走平台挂起（不计 CPU）",
    }
