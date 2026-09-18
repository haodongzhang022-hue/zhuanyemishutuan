"""检查点探测器（CheckpointProbe）：**事件驱动的探测点**，替代本地轮询

## 需求（Issue #14 追问，用户原话）

> 最多 6 次就行，再多就不适合等待了，不是这里的问题了，或者我们是否可以以
> **非轮询**而是**形成检查点然后 webhook 触发再跑**，**等他的锁**，然后形成一个
> 机制**判断检查点**，类似于**边缘计算的探测器**这样的，会不会好点。怎么处理比较好，
> 平台对这个有最佳实践吗

拆成四条可执行口径：

| # | 口径 | 落地 |
|---|---|---|
| 1 | **最多 6 次** | 探测次数上界 = 等待阶梯档位数（6 档：10s→30s→60s→3min→6min→10min）；用尽即**停止自动挂账** |
| 2 | **非轮询，检查点 + 再触发** | 探测点**不自己等**：要么挂到**平台等待语义**（`cnb:await`，触发方在状态变化时 `resolve`），要么**委托给平台定时任务**（`crontab`）到点代跑；本地**永不 sleep/轮询** |
| 3 | **形成检查点，判断检查点** | 检查点 = `key` + **判别式**（`kind`）。判别只看**可判定事实**（锁在不在 / 版本变没变 / 我方还等不等），**不看 agent 自述**；判不出 → `UNKNOWN`（宽进，不误判） |
| 4 | **边缘计算的探测器** | 采集**轻量本地快照**（锁台账 + 版本台账），产出**结构化事件**（`ProbeEvent`）上报；重活（评审、构建、模型调用）只在"值得唤醒"时才做，符合边缘计算"本地轻量感知、云端重处理"的算力分层 |

## 三条已确认口径（Issue #14 追问 · 用户三点确认）

| # | 确认项 | 固化 |
|---|---|---|
| 1 | 落点分工「**≤5min 挂 await，>5min 交 crontab**」 | 固化为**硬口径**：`_driver_for_delay()` 按 `await_max_sec`（默认 300s）自动分工，**不提供"一律挂 await"的回退口**——默认即为唯一落点策略 |
| 2 | `cron` 间隔「**固定 `*/5` 一档扫**」 | `_cron_fixed_5min()`：所有平台定时探测点统一 `*/5 * * * *`（**= 平台 5 分钟下限**，不随阶梯档位放大），与阶梯时间表**解耦** |
| 3 | `probe` 规则推到「**组织级共享**」 | 组织级包 `shared/collab/organization.yaml`（全组织一份）+ 仓库级 `shared/collab/collab-rules.yaml`（**只覆写数值**）；装载顺序见 `rules.load_rules`：**组织级 → 仓库级 → 内置默认**，跨仓接力用 `dispatch_shared()` |

## 平台最佳实践（官方文档口径，已查证）

| 能力 | 平台机制 | 文档 |
|---|---|---|
| **一次探测 = 一次事件，零占用** | `cnb:await` / `cnb:resolve`：pipeline 执行到 await 即**挂起**，由配对的 resolve 在**状态变化时**唤醒；await 期间不跑任务 | `/zh/build/internal-steps/cnb/await-resolve.md` |
| **跨流水线/跨仓再触发** | `cnb:apply`（同仓子流水线）/ `cnb:trigger`（跨仓）触发 `api_trigger_*` 事件 | `/zh/build/internal-steps/cnb/apply.md`、`/zh/build/internal-steps/cnb/trigger.md` |
| **外部 webhook / 调度系统驱动** | OPENAPI `StartBuild` + 外部 token 触发 `api_trigger` 流水线（官方推荐给**跨根组织**场景） | `/zh/build/trigger-rule.md`、`/zh/build/crontab.md` |
| **延迟兜底（队头阻塞时）** | `crontab` 定时任务：**最小间隔 5 分钟**，执行身份 = 最后修改该配置的用户；仅支持**明确单一分支** | `/zh/build/crontab.md` |
| **不计等待成本** | 计费只有「CPU 核时」（按任务**实际运行**时长）与「AI Credits」（按模型调用 Token），**没有"等待"项** | `/zh/pricing.md`、`/zh/build/timeout.md` |

平台**最佳实践结论**（本模块据此固化）：

1. **首选「等锁」= `cnb:await` 平台挂起**——锁释放方 `cnb:resolve`，等待方**零占用**唤醒；
   这才是"webhook 触发再跑"在 CNB 上的原生形态（事件驱动、无常驻进程）。
2. **次选「到点再跑」= `crontab` + `api_trigger`**——锁由**仓库外**持有者
   （如本地编辑中的真人）时，平台内没有"锁释放事件"可等，就用**平台定时任务**
   到点代跑一次探测；**平台是那个"不睡觉的进程"**，不是本会话。
3. **严禁本地轮询**：本地 `sleep`/空转轮询才是在烧核时（8 核空转 10 分钟 ≈ 1.33 核时）。

## 与"等待阶梯"的关系

阶梯（`StepBackoff`）只回答**"下一次什么时候看"**；本模块回答**"到点后怎么再看、
以及看什么"**。两者共用同一个次数上界（6 次），到顶即停 → 交回人工（`resume_required`）。

## 探测预算的**跨容器继承**（🔴A：预算不归零）

「最多 6 次」不能只落在 ``<数据根>/probe/*.json``：平台 `crontab` 代跑是
**新容器、新工作区**，磁盘上的 ``checks_used`` 从 0 开始 → 上界永不触发
（等于"软约束换了个位置"）。

故预算必须**随载荷走**：

- ``probe(key, external_checks=…)`` / ``adopt_checks_budget()``：``max()`` 合并
  外部轮次（只增不减），在"是否用尽"判定**之前**合并；
- ``ProbeEvent.budget_trail`` / ``budget_trail_of()``：``<!-- probe-budget: … -->``
  留痕形态，随**评论正文 / 交接卡 context / 事件载荷**持久传递
  （容器会被回收，评论不会）；
- ``dispatch()`` 的返回值统一带 ``budget_trail``，接收方**写回评论**即完成继承。

**边界（明示）**：调用方既不给载荷、又换数据根 → 预算只能从 0 开始。
这是平台执行模型的边界，不是"实现疏忽"；故 ``.cnb.yml`` 的探测任务里
"取上次留痕 → 合并 → 写回评论"三步是**必做项**（见该文件 `probe_task` 注释）。

## 用法

    probe = CheckpointProbe("i14", data_dir="<数据根>/probe")
    cp = probe.create(key="card:review-baseline", kind=CHECK_LOCK, holder="开发",
                      await_key="i14-lock-released", max_checks=6)
    plan = probe.delay_plan(cp)              # 六次怎么等（含平台指令，非轮询）
    probe.register_platform_wait(cp, sn="cnb-xxx")   # 已挂到平台等待语义（零占用）
    probe.note_lock(cp, held=True, holder="开发")     # 采集锁快照（轻量）
    ev = probe.judge(cp, our_need_satisfied=False)   # 判断检查点（判定式）
    probe.dispatch(ev)                       # 上报结构化事件（跨 agent 传递）
"""
from __future__ import annotations

import json
import os
import re
import tempfile
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

from .backoff import DEFAULT_MAX_ATTEMPTS, StepBackoff

# 平台语义对齐（**具体任务的真实约束**）：`cnb:await` 只能在**同一事件**配对，
# 且一个 key 仅能 resolve 一次。挂之前先校验，配不上对就降级 crontab——
# 否则 await 会静默等不到（要靠平台死锁检测才发现）。见 `collab/platform.py`。
from .platform import (  # noqa: E402  （同包内相对导入，放这里保持常量可见）
    await_key_for,
    resolve_driver,
)

# 会话/检查点标识的**安全校验**统一走 `_safe_shared_id()`（见文件末尾）：
# 跨仓标识（`org/repo#14`）与本地标识共用同一条判定，不再保留第二份正则
# （🟡6：原先的 `_SAFE_ID` 定义后无人使用，死代码会被误读为"已校验"）。

# ---- 探测维度（判别式）：只看"可判定事实"，不看 agent 自述 ----
CHECK_CONFIRM = "confirm"      # 人工/上游确认（人说的话才算数）
CHECK_LOCK = "lock"            # 等锁：锁表视图 / 租约释放
CHECK_VERSION = "version"      # 等版本：版本台账（提交/台账版本）变化
CHECK_EVENT = "event"          # 等事件：外部 webhook 打进来（api_trigger / cnb:resolve）
CHECK_RESOURCE = "resource"    # 等资源：外部资源就绪（设备/算力/制品）

ALL_CHECKS = (CHECK_CONFIRM, CHECK_LOCK, CHECK_VERSION, CHECK_EVENT, CHECK_RESOURCE)

# ---- 判定结果（三态 + 未确定） ----
PROCEED = "proceed"                    # 条件已满足 → 继续干活（唤醒）
WAIT = "wait"                          # 仍未满足 → 挂检查点，走到点再探测
RELEASE = "release"                    # **我方不再需要** → 主动放锁，不再等（反队头阻塞）
UNKNOWN = "unknown"                    # 判不出（缺数据）→ 宽进：继续等，不误判

ALL_VERDICTS = (PROCEED, WAIT, RELEASE, UNKNOWN)

# ---- 平台侧探测落点（**永不选 "local_loop"**） ----
DRIVER_AWAIT = "platform_await"        # 挂 cnb:await：状态变化时由 resolve 唤醒（首选）
DRIVER_CRONTAB = "platform_crontab"    # 平台定时任务（crontab）到点代跑探测
DRIVER_EXTERNAL = "external_webhook"   # 外部 webhook / OPENAPI 触发 api_trigger 再跑
DRIVER_DELEGATE = "delegate_event"     # 委托持有者完成时回调 api_trigger_* 事件
DRIVER_HANDOFF = "handoff_resume"      # 交回人工/上游，由人解锁后再触发

ALL_DRIVERS = (DRIVER_AWAIT, DRIVER_CRONTAB, DRIVER_EXTERNAL,
               DRIVER_DELEGATE, DRIVER_HANDOFF)

# 平台定时任务最小间隔（官方文档：低于此间隔无法提交）
CRONTAB_MIN_INTERVAL_SEC = 300.0


# ---- 字段级容错守卫（与 LoopGuard / LockRegistry 同族口径） ----
# 「损坏/缺失 → 视为不存在，不炸」只覆盖**非法 JSON**是不够的：合法 JSON 但
# **类型不符**（如 ``{"checks_used": "NaN"}``）在 ``int()`` 处直接裸抛，且会
# 一路逃逸到门面（``checkpoints_overview()``）与跨仓入口（``dispatch_shared()``）。
# 故逐字段守卫：类型不符 → 退化默认（宽进），绝不把坏字段变成异常。

def _as_int(value: Any, default: int) -> int:
    """宽进读整数（bool / 字符串 / 非数字 → 默认值）。"""
    return value if isinstance(value, int) and not isinstance(value, bool) \
        else default


def _as_float(value: Any, default: float) -> float:
    """宽进读数字（bool / 字符串 / 非数字 → 默认值）。"""
    return float(value) if isinstance(value, (int, float)) \
        and not isinstance(value, bool) else default


def _as_str(value: Any, default: str = "") -> str:
    """宽进读字符串（非 str → 默认值；不用 ``str()`` 强转，避免把坏值当内容）。"""
    return value if isinstance(value, str) else default


def _as_bool(value: Any, default: bool) -> bool:
    """宽进读布尔（**只认严格 bool**；``"false"``/``0`` 一律回落默认值）。"""
    return value if isinstance(value, bool) else default


def _as_dict(value: Any) -> Dict[str, Any]:
    """宽进读映射（非 dict → 空 dict）。"""
    return dict(value) if isinstance(value, dict) else {}


def _as_list(value: Any) -> List[Any]:
    """宽进读列表（非 list → 空列表）。"""
    return list(value) if isinstance(value, list) else []


# 探测预算的**可持久留痕**标记（评论正文 / 交接卡 context / 事件载荷共用）：
# 容器会被回收，评论不会 —— 跨容器"最多 6 次"只能靠载荷走，不能靠本地磁盘。
PROBE_BUDGET_TRAIL_RE = re.compile(r'<!--\s*probe-budget:\s*(\{.*?\})\s*-->', re.S)


def crontab_minutes_from_rules(rules: Optional[Any]) -> int:
    """从规则包取平台定时任务间隔（分钟）——**跨仓与本地共用同一条取值路径**。

    规则包缺失/形状不符 → 回落平台下限 5 分钟（宽进，不炸）。
    """
    every = getattr(rules, "crontab_every_minutes", None)
    if callable(every):
        try:
            return max(1, int(every()))
        except Exception:      # pragma: no cover - 宽进：异常回落默认档
            return CRON_EVERY_MINUTES
    return CRON_EVERY_MINUTES


# 等待时长 → 平台落点的分工阈值（= 平台定时任务下限 5 分钟）
AWAIT_MAX_SEC = CRONTAB_MIN_INTERVAL_SEC


def _driver_for_delay(delay_sec: float,
                      await_max_sec: float = AWAIT_MAX_SEC) -> str:
    """按等待时长挑平台落点：**短等待挂 await；长等待交平台定时任务**。

    - ≤ ``await_max_sec``（默认 = 平台下限 5 分钟）→ `platform_await`：
      **零占用**挂起，状态一变由对端 `cnb:resolve` 唤醒；
    - > ``await_max_sec`` → `platform_crontab`：交**平台那个"不睡觉的进程"**
      到点代跑一次探测。

    **这是一条硬口径（已确认）**：不存在"一律挂 await"的回退口——
    超过平台定时下限的等待若仍挂在单个 await 上，就等于把"到点必看"押在
    对端一定会 resolve 上；对端不 resolve（人没编辑完 / 外部系统不回调）就是死等。
    故分工按阈值自动进行，且**两个落点不得混用**（见 `TWO_WAY_SPLIT_NOTE`）。

    **宽进**：阈值或时长不是数字（坏载荷/坏规则包）→ 回落平台下限 300s；
    仍判不出时取**保守落点** `crontab`（"到点必看"由平台兜底，不押注对端一定 resolve）。
    """
    try:
        limit = float(await_max_sec)
    except (TypeError, ValueError):
        limit = AWAIT_MAX_SEC
    if limit <= 0:
        limit = AWAIT_MAX_SEC
    try:
        delay = float(delay_sec)
    except (TypeError, ValueError):
        return DRIVER_CRONTAB
    return DRIVER_AWAIT if delay <= limit else DRIVER_CRONTAB


# 分工口径一句话（文档/评论/角色提示词共用，避免各说一套）
TWO_WAY_SPLIT_NOTE = (
    "落点分工（固定，不可混用）：≤5 分钟挂 `cnb:await`（零占用、事件唤醒）；"
    ">5 分钟交平台 `crontab`（固定 `*/5` 一档扫，平台代跑）。"
    "不存在「一律挂 await」这条回退路径。"
)


# 平台定时任务（crontab）固定间隔：**一档 `*/5` 扫**（已确认口径 #2）
#
# 为什么固定 `*/5` 而不是随阶梯档位放大：
# - `*/5` 就是**平台下限**本身，长等待（6min/10min 档）只需"到点时被平台看一眼"，
#   无需更稀疏；更稀疏反而让收尾更慢；
# - 与**阶梯时间表解耦**：阶梯回答"我们打算什么时候再看"，cron 只回答"平台多久代跑一次"。
#   若把 cron 跟着阶梯放大（`*/6`、`*/10`），换阶梯就会改变平台的代跑节奏——
#   两套节奏耦合，正是"换仓/换 agent 行为漂移"的来源。
CRON_EVERY_MINUTES = 5
CRON_FIXED_EXPR = "*/5 * * * *"      # 固定表达式（单一语义源，测试直接断言它）


def _cron_fixed_5min(_delay_sec: float = 0.0,
                     base_minutes: int = CRON_EVERY_MINUTES) -> str:
    """**固定一档扫**：所有平台定时探测点用**同一个** cron 表达式。

    第一个参数（原等待时长）**保留但不使用**——签名兼容旧调用点，语义上
    "间隔与等待时长无关"（这正是与阶梯**解耦**的落点）。

    间隔 = `max(平台下限 5min, base_minutes)`：传小于 5 的 `base_minutes`
    会被平台下限夹住（**不可能**配出比平台允许更密的扫描）。
    """
    minutes = max(int(CRONTAB_MIN_INTERVAL_SEC // 60), int(base_minutes))
    return f"*/{minutes} * * * *"


def cron_expr_for(rules: Optional[Any] = None) -> str:
    """按规则包给出**唯一**的 cron 表达式（单一语义源，供门面/文档引用）。

    规则包里的 `probe.crontab_interval_minutes` **是真读的**（不是装饰性文档）：
    调大即有意疏化扫描，调小会被平台下限夹住。
    """
    minutes = crontab_minutes_from_rules(rules)
    return _cron_fixed_5min(0.0, minutes)


@dataclass
class ProbeEvent:
    """一次探测产出的**结构化事件**（可跨 agent / 跨仓传递、可留痕）。

    「边缘计算的探测器」口径：本地只做**轻量采集 + 判定**，产出事件；
    重处理（评审/构建/模型调用）由事件驱动，**只在值得唤醒时才发生**。
    """

    checkpoint_key: str = ""
    kind: str = CHECK_EVENT
    # 平台语义对齐（Issue #14）：await 只能**同一事件**配对。
    # 跨仓传递时必须带上这两项，否则接收方无从校验、只能保守降级 crontab。
    waiter_event: str = ""
    resolver_event: str = ""
    verdict: str = UNKNOWN
    # 采集到的**轻量**事实（锁表视图 / 版本号 / 外部信号），不塞原始数据
    facts: Dict[str, Any] = field(default_factory=dict)
    driver: str = DRIVER_AWAIT
    next_delay_sec: float = 0.0
    checks_used: int = 0
    checks_left: int = 0
    resume_required: bool = False
    reason: str = ""
    action: str = ""            # 给接收方的**明确指令**（resume / keep_waiting / sweep_lock / human）
    wake_allowed: bool = True   # 是否允许据此唤醒下一轮（防"探测自己变成新循环"）
    # **探测预算留痕**（``<!-- probe-budget: … -->``）：随评论/交接卡/事件载荷走，
    # 下一棒（可能是新容器）据此恢复 ``checks_used``，不必共享磁盘。
    budget_trail: str = ""
    at: float = 0.0

    def to_dict(self) -> dict:
        return {
            "checkpoint_key": self.checkpoint_key,
            "kind": self.kind,
            "waiter_event": self.waiter_event,
            "resolver_event": self.resolver_event,
            "verdict": self.verdict,
            "facts": dict(self.facts),
            "driver": self.driver,
            "next_delay_sec": self.next_delay_sec,
            "checks_used": self.checks_used,
            "checks_left": self.checks_left,
            "resume_required": self.resume_required,
            "reason": self.reason,
            "action": self.action,
            "wake_allowed": self.wake_allowed,
            "budget_trail": self.budget_trail,
            "at": self.at,
        }

    @classmethod
    def from_dict(cls, data: Any) -> Optional["ProbeEvent"]:
        """字段级容错恢复（**合法 JSON + 类型不符也不炸**）。

        与 ``LoopGuard.from_dict`` / ``LockRegistry.from_dict`` **同族口径**：
        坏字段退化为默认值，只有"根本不是对象"才返回 ``None``。
        跨仓载荷（``dispatch_shared``）走的正是这条路径，不得裸抛。
        """
        if not isinstance(data, dict):
            return None
        ev = cls()
        ev.checkpoint_key = _as_str(data.get("checkpoint_key"))
        ev.kind = _as_str(data.get("kind"), CHECK_EVENT) or CHECK_EVENT
        ev.waiter_event = _as_str(data.get("waiter_event"))
        ev.resolver_event = _as_str(data.get("resolver_event"))
        ev.verdict = _as_str(data.get("verdict"), UNKNOWN) or UNKNOWN
        ev.facts = _as_dict(data.get("facts"))
        ev.driver = _as_str(data.get("driver"), DRIVER_AWAIT) or DRIVER_AWAIT
        ev.next_delay_sec = _as_float(data.get("next_delay_sec"), 0.0)
        ev.checks_used = max(0, _as_int(data.get("checks_used"), 0))
        ev.checks_left = max(0, _as_int(data.get("checks_left"), 0))
        ev.resume_required = _as_bool(data.get("resume_required"), False)
        ev.reason = _as_str(data.get("reason"))
        ev.action = _as_str(data.get("action"))
        ev.wake_allowed = _as_bool(data.get("wake_allowed"), True)
        ev.budget_trail = _as_str(data.get("budget_trail"))
        ev.at = _as_float(data.get("at"), 0.0)
        return ev


@dataclass
class Checkpoint:
    """一个**检查点**：等什么、怎么判、还能探几次、下一步挂到平台哪儿。"""

    key: str
    session_id: str = ""
    kind: str = CHECK_EVENT
    holder: str = ""                 # 我们在等谁（人/agent/外部系统）
    scope: str = ""                  # 等什么（锁键 / 版本对象 / 资源名）
    # 平台侧：等待语义的配对键 + 已挂流水线号（挂上了 = 零占用等待）
    await_key: str = ""
    resolve_status: str = ""         # ""(未挂) / "awaiting" / "resolved" / "timeout"
    sn: str = ""
    # 平台语义对齐（Issue #14）：await 只能**同一事件**配对。
    # 两侧事件留痕，便于事后复核"为什么这次降级成了 crontab"。
    waiter_event: str = ""           # 探测方（等待方）这条 pipeline 的事件
    resolver_event: str = ""         # 放锁方（对端）那条 pipeline 的事件
    checks_used: int = 0             # 已探测次数（**最多 6 次**）
    max_checks: int = DEFAULT_MAX_ATTEMPTS
    next_delay_sec: float = 0.0      # 下一次等待时长（取阶梯）
    last_verdict: str = ""
    last_facts: Dict[str, Any] = field(default_factory=dict)
    created_at: float = 0.0
    updated_at: float = 0.0
    released: bool = False           # 我方主动放弃等待 → 放锁，不再占位
    resume_required: bool = False    # 到顶即停 → 交回人工
    history: List[Dict[str, Any]] = field(default_factory=list)

    def used_up(self) -> bool:
        """探测预算是否用尽（用尽 → **停止自动挂账**，交回人工/上游）。"""
        return self.checks_used >= self.max_checks

    def checks_left(self) -> int:
        return max(0, self.max_checks - self.checks_used)

    def to_dict(self) -> dict:
        return {
            "key": self.key,
            "session_id": self.session_id,
            "kind": self.kind,
            "holder": self.holder,
            "scope": self.scope,
            "await_key": self.await_key,
            "resolve_status": self.resolve_status,
            "sn": self.sn,
            "waiter_event": self.waiter_event,
            "resolver_event": self.resolver_event,
            "checks_used": self.checks_used,
            "max_checks": self.max_checks,
            "checks_left": self.checks_left(),
            "next_delay_sec": self.next_delay_sec,
            "last_verdict": self.last_verdict,
            "last_facts": dict(self.last_facts),
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "released": self.released,
            "resume_required": self.resume_required,
            "history": list(self.history),
        }

    @classmethod
    def from_dict(cls, data: Any) -> Optional["Checkpoint"]:
        """字段级容错恢复（**合法 JSON + 类型不符也不炸**）。

        坏字段退化默认值（同 ``LoopGuard`` / ``LockRegistry`` 族口径）；
        只有"不是对象"或"缺 key"才返回 ``None``（= 视为不存在）。
        """
        if not isinstance(data, dict) or not data.get("key"):
            return None
        cp = cls(key=_as_str(data.get("key")))
        cp.session_id = _as_str(data.get("session_id"))
        cp.kind = _as_str(data.get("kind"), CHECK_EVENT) or CHECK_EVENT
        cp.holder = _as_str(data.get("holder"))
        cp.scope = _as_str(data.get("scope"))
        cp.await_key = _as_str(data.get("await_key"))
        cp.resolve_status = _as_str(data.get("resolve_status"))
        cp.sn = _as_str(data.get("sn"))
        cp.waiter_event = _as_str(data.get("waiter_event"))
        cp.resolver_event = _as_str(data.get("resolver_event"))
        cp.checks_used = max(0, _as_int(data.get("checks_used"), 0))
        cp.max_checks = max(1, _as_int(data.get("max_checks"),
                                       DEFAULT_MAX_ATTEMPTS))
        cp.next_delay_sec = max(0.0, _as_float(data.get("next_delay_sec"), 0.0))
        cp.last_verdict = _as_str(data.get("last_verdict"))
        cp.last_facts = _as_dict(data.get("last_facts"))
        cp.created_at = _as_float(data.get("created_at"), 0.0)
        cp.updated_at = _as_float(data.get("updated_at"), 0.0)
        cp.released = _as_bool(data.get("released"), False)
        cp.resume_required = _as_bool(data.get("resume_required"), False)
        cp.history = [h for h in _as_list(data.get("history"))
                      if isinstance(h, dict)]
        return cp


@dataclass
class ProbeOutcome:
    """一次探测的完整产出：判定 + 事件 + **给流水线的平台指令**。"""

    verdict: str = UNKNOWN
    event: ProbeEvent = field(default_factory=ProbeEvent)
    checkpoint: Optional[Checkpoint] = None

    def to_dict(self) -> dict:
        return {
            "verdict": self.verdict,
            "event": self.event.to_dict(),
            "checkpoint": self.checkpoint.to_dict() if self.checkpoint else None,
        }


class CheckpointProbe:
    """检查点探测器注册表（**纯判定 + 平台落点编排，永不本地 sleep**）。

    Args:
        session_id: 会话标识（一个 Issue/PR 一个）
        data_dir: 检查点持久化目录（默认 ``<cwd>/data/probe``）
        backoff: 等待阶梯（决定"下一次什么时候看"；与探测次数上界共用语义）
        max_checks: 探测次数上界（**默认 6**，随阶梯档位数）
        clock: 注入时钟（默认 ``time.time``；测试可注入假时钟）
        lock_view: 采集**锁表视图**的函数（返回 ``{key: {...}}`` 或 ``None``）
        version_view: 采集**版本台账**的函数（返回当前版本号）
    """

    def __init__(
        self,
        session_id: str,
        data_dir: Optional[str] = None,
        backoff: Optional[StepBackoff] = None,
        max_checks: int = DEFAULT_MAX_ATTEMPTS,
        clock: Optional[Callable[[], float]] = None,
        lock_view: Optional[Callable[[], Optional[Dict[str, Any]]]] = None,
        version_view: Optional[Callable[[], Optional[int]]] = None,
        crontab_base_minutes: int = CRON_EVERY_MINUTES,
        await_max_sec: float = AWAIT_MAX_SEC,
        rules: Optional[Any] = None,
        mkdir: bool = True,
    ):
        # 会话号可能来自**跨仓事件**（如 `dongdongxiaozu/zhuanyemishutuan#14`）——
        # 因此这里接受"仓/号"这类正常写法；危险形态（路径穿越/控制字符）仍拒绝。
        # 落盘文件名另经 `_slug()` 转义，路径安全**不**依赖会话号本身。
        if not _safe_shared_id(session_id):
            raise ValueError(
                f"非法 session_id: {session_id!r}。"
                "拒绝路径穿越/控制字符/空白；允许 [A-Za-z0-9_.:/-#] 等标识写法。")
        import time as _time
        self.session_id = session_id
        self.data_dir = data_dir or os.path.join(os.getcwd(), "data", "probe")
        self.backoff = backoff or StepBackoff()
        self.max_checks = (int(max_checks)
                           if isinstance(max_checks, int) and not isinstance(max_checks, bool)
                           and max_checks >= 1 else DEFAULT_MAX_ATTEMPTS)
        self._clock: Callable[[], float] = clock or _time.time
        self._lock_view = lock_view
        self._version_view = version_view
        # 规则包里的固定间隔（若给了规则包且未显式传 base_minutes，则以规则包为准）
        self.crontab_base_minutes = int(crontab_base_minutes)
        if rules is not None and int(crontab_base_minutes) == CRON_EVERY_MINUTES:
            every = getattr(rules, "crontab_every_minutes", None)
            if callable(every):
                try:
                    self.crontab_base_minutes = int(every())
                except Exception:      # pragma: no cover - 宽进
                    pass
        self.crontab_base_minutes = max(1, int(self.crontab_base_minutes))
        # 落点分工阈值（≤ 此值挂 await，> 此值交 crontab）；**用户确认的硬口径**，
        # 可通过规则包调"时分界线"，但**不能取消分工**（不存在"一律 await"）。
        self.await_max_sec = (
            float(await_max_sec)
            if isinstance(await_max_sec, (int, float))
            and not isinstance(await_max_sec, bool) and float(await_max_sec) > 0
            else AWAIT_MAX_SEC)
        # 目录**惰性创建**（🟡2）：`dispatch()` 是"只算落点"的纯函数，
        # 不该在 cwd 下凭空造出 `data/probe/`（污染新容器/流水线工作区）。
        # 需要落盘的路径（create/_save）会自行 `_ensure_dir()`。
        self._mkdir = bool(mkdir)
        if self._mkdir:
            self._ensure_dir()

    def _ensure_dir(self) -> None:
        """确保数据目录存在（落盘前调用；纯计算路径不调用）。"""
        os.makedirs(self.data_dir, exist_ok=True)

    # ------------------------------------------------------------------
    # 落盘（一个检查点一个文件；损坏/缺失 → 视为不存在，不炸）
    # ------------------------------------------------------------------

    @staticmethod
    def _slug(value: Any) -> str:
        """把任意标识转成**文件系统安全**的片段（`/`、`#`、`..` 一并转掉）。

        路径安全由这里保证，**不**依赖标识本身有多"规矩"——所以会话号可以是
        `org/repo#14` 这种人类可读写法，落盘仍然平铺在本会话目录里。
        """
        safe = re.sub(r'[^A-Za-z0-9_.:-]', '_', str(value))[:120]
        safe = safe.replace("..", "__")
        return safe or "checkpoint"

    def _path(self, key: str) -> str:
        return os.path.join(self.data_dir, f"{self._slug(key)}.json")

    def load(self, key: str) -> Optional[Checkpoint]:
        path = self._path(key)
        if not os.path.isfile(path):
            return None
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, ValueError):
            return None
        return Checkpoint.from_dict(data)

    def _save(self, cp: Checkpoint) -> None:
        self._ensure_dir()
        cp.updated_at = self._clock()
        data = json.dumps(cp.to_dict(), ensure_ascii=False, indent=2)
        fd, tmp = tempfile.mkstemp(dir=self.data_dir, suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(data)
            os.replace(tmp, self._path(cp.key))
        finally:
            if os.path.exists(tmp):
                try:
                    os.remove(tmp)
                except OSError:
                    pass

    # ------------------------------------------------------------------
    # 建 / 查 / 列
    # ------------------------------------------------------------------

    # ------------------------------------------------------------------
    # 跨容器 / 跨棒预算继承（**探测预算不归零**）
    # ------------------------------------------------------------------

    @staticmethod
    def budget_trail_of(cp: Checkpoint) -> str:
        """检查点预算的**留痕**形态（写进评论/交接卡 context 即可跨容器继承）。"""
        return ("<!-- probe-budget: "
                + json.dumps({"checkpoint_key": cp.key,
                              "checks_used": cp.checks_used,
                              "max_checks": cp.max_checks},
                             ensure_ascii=False, sort_keys=True)
                + " -->")

    @staticmethod
    def _coerce_checks_used(value: Any) -> Optional[int]:
        """从任意形态的载荷里取外部探测轮次（宽进：取不到 → ``None``）。

        接受：int / 数字串 / 字典（``checks_used`` 或 ``rounds.checks_used``）/
        含 ``<!-- probe-budget: … -->`` 的正文 / ``ProbeEvent.to_dict()``。
        """
        if isinstance(value, bool):
            return None
        if isinstance(value, int):
            return max(0, value)
        if isinstance(value, float):
            return max(0, int(value))
        if isinstance(value, str):
            text = value.strip()
            m = PROBE_BUDGET_TRAIL_RE.search(text)
            if m:
                try:
                    got = json.loads(m.group(1))
                except ValueError:
                    got = None
                if isinstance(got, dict):
                    return CheckpointProbe._coerce_checks_used(got)
            try:
                return max(0, int(text))
            except ValueError:
                return None
        if isinstance(value, dict):
            for key in ("checks_used", "external_checks", "used"):
                if key in value:
                    got = CheckpointProbe._coerce_checks_used(value[key])
                    if got is not None:
                        return got
            rounds = value.get("rounds")
            if isinstance(rounds, dict):
                return CheckpointProbe._coerce_checks_used(rounds)
        return None

    def adopt_checks_budget(self, cp: Checkpoint, payload: Any = None,
                            save: bool = True) -> dict:
        """**合并外部探测轮次**（``max()`` 只增不减）→ 换容器不归零。

        Args:
            cp: 目标检查点（原地更新）。
            payload: 外部轮次（int / 数字串 / ``{"checks_used": n}`` /
                ``ProbeEvent.to_dict()`` / 含 ``<!-- probe-budget: … -->`` 的正文）；
                也可直接传探测 kwargs（含 ``checks_used``/``external_checks``）。
            save: 是否落盘。

        Returns:
            ``{"merged": bool, "checks_used": int, "max_checks": int}``。
        """
        external = self._coerce_checks_used(payload)
        if external is None or external <= cp.checks_used:
            return {"merged": False, "checks_used": cp.checks_used,
                    "max_checks": cp.max_checks}
        cp.checks_used = external
        cp.history.append({
            "at": self._clock(), "action": "adopt_budget",
            "checks_used": external,
            "note": "合并外部探测轮次（跨容器/跨棒继承，只增不减）",
        })
        if save:
            self._save(cp)
        return {"merged": True, "checks_used": cp.checks_used,
                "max_checks": cp.max_checks}

    def create(
        self,
        key: str,
        kind: str = CHECK_EVENT,
        holder: str = "",
        scope: str = "",
        await_key: str = "",
        max_checks: Optional[int] = None,
        note: str = "",
        waiter_event: str = "",
        resolver_event: str = "",
    ) -> Checkpoint:
        """登记一个检查点（**幂等**：同键已存在则原样返回，不重置预算）。

        幂等是刻意的：若"每次遇到都重建检查点"，就等于把"最多 6 次"变成无限次
        ——正是要治的那种循环。
        """
        existed = self.load(key)
        if existed is not None:
            return existed
        cp = Checkpoint(
            key=str(key),
            session_id=self.session_id,
            kind=str(kind or CHECK_EVENT),
            holder=str(holder or ""),
            scope=str(scope or ""),
            await_key=str(await_key or ""),
            waiter_event=str(waiter_event or ""),
            resolver_event=str(resolver_event or ""),
            max_checks=(self.max_checks if max_checks is None
                        else max(1, int(max_checks))),
            created_at=self._clock(),
            updated_at=self._clock(),
        )
        cp.history.append({
            "at": self._clock(), "action": "create", "kind": cp.kind,
            "holder": cp.holder, "note": note,
            "waiting_plan": self.backoff.human(),
        })
        self._save(cp)
        return cp

    def list_open(self) -> List[Checkpoint]:
        """所有**未结束**的检查点（未放行、未放弃、未交回人工）。"""
        out: List[Checkpoint] = []
        if not os.path.isdir(self.data_dir):
            return out
        for name in sorted(os.listdir(self.data_dir)):
            if not name.endswith(".json"):
                continue
            cp = self.load(name[:-5])
            if cp is None:
                continue
            if cp.released or cp.resume_required:
                continue
            if cp.last_verdict == PROCEED:
                continue
            out.append(cp)
        return out

    # ------------------------------------------------------------------
    # 等待计划（**六次，每次一个平台落点；没有任何一次是本地空转**）
    # ------------------------------------------------------------------

    def delay_plan(self, cp: Optional[Checkpoint] = None) -> List[Dict[str, Any]]:
        """六次探测怎么等：等待阶梯 + **平台落点** + cron 表达式（非轮询）。

        「最多 6 次」= 阶梯档位数；第 6 次（10 分钟档）之后预算用尽 →
        `resume_required`（停止自动挂账，交回人工/上游）。
        """
        n = cp.max_checks if cp is not None else self.max_checks
        we = cp.waiter_event if cp is not None else ""
        re_ = cp.resolver_event if cp is not None else ""
        rows: List[Dict[str, Any]] = []
        for i in range(n):
            st = self.backoff.status(i)
            wanted = _driver_for_delay(st.delay_sec, self.await_max_sec)
            # 平台语义校验：await 要求**同一事件**配对且 key 仅能 resolve 一次。
            # 配不上对就**在计划里就**显示降级后的真实落点（不是"计划一套、执行一套"）。
            v = resolve_driver(wanted, checkpoint_key=(cp.key if cp else ""),
                               attempt=i, waiter_event=we, resolver_event=re_,
                               await_max_sec=self.await_max_sec)
            driver = v.driver
            rows.append({
                "check": i + 1,
                "delay_sec": st.delay_sec,
                "label": st.label,
                "at_top": st.at_top,
                "driver": driver,
                "requested_driver": wanted,
                "downgraded": v.downgraded,
                "downgrade_reason": v.reason if v.downgraded else "",
                "await_key": v.await_key,
                "crontab": (_cron_fixed_5min(st.delay_sec, self.crontab_base_minutes)
                            if driver == DRIVER_CRONTAB else ""),
                "local_cpu": "0（不占构建核时）",
                "note": ("挂 cnb:await，锁释放方 resolve 唤醒（零占用）"
                         if driver == DRIVER_AWAIT else
                         "交平台定时任务（crontab）到点代跑一次探测；"
                         "固定 */5 一档扫（= 平台 5 分钟下限）"),
            })
        rows.append({
            "check": n + 1,
            "delay_sec": 0.0,
            "label": "预算用尽",
            "at_top": True,
            "driver": DRIVER_HANDOFF,
            "crontab": "",
            "local_cpu": "0（不占构建核时）",
            "note": "停止自动挂账 → 交回人工/上游（resume_required），不再自己等",
        })
        return rows

    # ------------------------------------------------------------------
    # 触发机制：登记"已把等待挂到平台语义上"（这才是"非轮询"的落点）
    # ------------------------------------------------------------------

    def register_platform_wait(
        self,
        key: str,
        await_key: str = "",
        sn: str = "",
        driver: str = DRIVER_AWAIT,
        note: str = "",
        waiter_event: str = "",
        resolver_event: str = "",
        stage: str = "stages",
    ) -> Optional[Checkpoint]:
        """登记"等待已挂到平台语义上"：挂上了 → 本地**零占用**（可被回收）。

        这对应 `.cnb.yml` 里的 `cnb:await`（配对键 `await_key`）——
        锁/状态一旦变化，由对端 `cnb:resolve` 唤醒；也可由外部 webhook
        触发 `api_trigger` 再跑（`external_webhook`）。

        **平台语义校验（Issue #14 对齐官方文档）**：`cnb:await` 要求
        「只能对**同一个事件**触发的 pipeline 做 await/resolve」且
        「一个 key **仅能 resolve 一次**」。因此这里在真正挂之前过一道
        `resolve_driver()`：

        - 两侧事件不同 / 不可判定 / 不在 `stages` → **自动降级 `crontab`**
          （平台代跑 + 平台死锁检测兜底），并在 `history` 里留下降级理由；
        - `await_key` **按轮次自动生成**（`probe:<key>:r<checks_used>`）——
          复用同一 key 会让第 2 次及以后的 await 永远等不到 resolve；
        - 显式传入的 `await_key` 仍尊重（调用方明确口径优先）。
        """
        cp = self.load(key)
        if cp is None:
            return None
        if driver not in ALL_DRIVERS or driver == DRIVER_HANDOFF:
            raise ValueError(
                f"非法平台落点: {driver!r}（不得为本地空转/人工落点）。"
                f"允许: {ALL_DRIVERS}")
        # 两侧事件：显式传入优先，否则用检查点登记时留痕的事件
        we = waiter_event or cp.waiter_event
        re_ = resolver_event or cp.resolver_event
        verdict = resolve_driver(
            driver, checkpoint_key=key, attempt=cp.checks_used,
            waiter_event=we, resolver_event=re_, stage=stage,
            await_max_sec=self.await_max_sec,
        )
        if we:
            cp.waiter_event = str(we)
        if re_:
            cp.resolver_event = str(re_)
        if verdict.downgraded:
            driver = verdict.driver
        if driver == DRIVER_AWAIT:
            cp.await_key = str(await_key) if await_key else verdict.await_key
        elif await_key:
            cp.await_key = str(await_key)
        if sn:
            cp.sn = str(sn)
        cp.resolve_status = "awaiting"
        cp.history.append({
            "at": self._clock(), "action": "platform_wait", "driver": driver,
            "requested_driver": verdict.requested,
            "downgraded": verdict.downgraded,
            "downgrade_reason": verdict.reason if verdict.downgraded else "",
            "await_key": cp.await_key, "sn": cp.sn, "note": note,
            "waiter_event": cp.waiter_event, "resolver_event": cp.resolver_event,
            "platform_sources": list(verdict.sources),
        })
        self._save(cp)
        return cp

    def resolve(
        self,
        key: str,
        status: str = "resolved",
        facts: Optional[Dict[str, Any]] = None,
    ) -> Optional[Checkpoint]:
        """平台侧 `cnb:resolve` 到达（对端状态变化）→ 标记检查点已被唤醒。"""
        cp = self.load(key)
        if cp is None:
            return None
        cp.resolve_status = str(status or "resolved")
        if facts:
            cp.last_facts.update(facts)
        cp.history.append({"at": self._clock(), "action": "resolve",
                           "status": cp.resolve_status,
                           "facts": dict(facts or {})})
        self._save(cp)
        return cp

    # ------------------------------------------------------------------
    # 探测器：轻量采集 + 判定检查点（**只看可判定事实**）
    # ------------------------------------------------------------------

    def _collect_lock_facts(self, cp: Checkpoint) -> Optional[Dict[str, Any]]:
        """采集**锁快照**（边缘探测器口径：只取相关那一把，不搬全量）。"""
        view = None
        if self._lock_view is not None:
            try:
                view = self._lock_view()
            except Exception:      # pragma: no cover - 采集失败按"判不出"处理
                view = None
        if view is None:
            return None
        scope = cp.scope or cp.key
        held = view.get("held") if isinstance(view, dict) else None
        if isinstance(held, list):
            for row in held:
                if not isinstance(row, dict):
                    continue
                if scope and scope in (row.get("key"), row.get("scope")):
                    return {
                        "lock_held": True,
                        "holder": row.get("holder") or "",
                        "persistent": bool(row.get("persistent")),
                        "ttl_left_sec": row.get("ttl_left_sec"),
                    }
            return {"lock_held": False, "holder": ""}
        # 兼容 `{key: {...}}` 形态
        if isinstance(held, dict):
            row = held.get(scope)
            if row is None:
                return {"lock_held": False, "holder": ""}
            return {"lock_held": True,
                    "holder": (row or {}).get("holder", "")
                    if isinstance(row, dict) else ""}
        return None

    def _collect_version_fact(self, cp: Checkpoint) -> Optional[Dict[str, Any]]:
        """采集**版本号**（不搬内容——边缘探测器只回传"变了没"）。"""
        if self._version_view is None:
            return None
        try:
            cur = self._version_view()
        except Exception:          # pragma: no cover - 采集失败按"判不出"处理
            return None
        if not isinstance(cur, int) or isinstance(cur, bool):
            return None
        return {"version": cur}

    def probe(self, key: str, **kwargs) -> ProbeOutcome:
        """探测一次（第 N+1 次）：**采集 → 判定 → 记预算 → 给出下一步落点**。

        Args:
            our_need_satisfied: **我方是否还等这件事**。False 表示"我已不需要这个锁/
                这个版本了" → 直接 `release`（放锁、不再等），**避免队头阻塞**。
            confirmed / version_changed / event_fired / resource_ready: 各判别式的事实
            checks_used / external_checks: **外部探测轮次**（跨容器/跨棒继承用）。
                平台 `crontab` 代跑是**新容器、新数据根**，本地磁盘上
                ``checks_used`` 从 0 开始 → "最多 6 次"永不触发。故此处接受
                外部轮次并 ``max()`` 合并（只增不减），使预算**随载荷走**。
        """
        cp = self.load(key)
        if cp is None:
            cp = self.create(key)
        # 跨容器预算继承（**在"是否用尽"判定之前**合并，否则永远判不出用尽）。
        self.adopt_checks_budget(cp, kwargs, save=False)
        if cp.used_up():
            return self._outcome(cp, WAIT,
                                 reason="探测预算已用尽（最多 %d 次）" % cp.max_checks,
                                 resume_required=True,
                                 action="human_resume")
        # ① 采集（轻量）。**先留一份探测前快照**：判定必须拿"探测前"的事实做参照，
        #    否则先写入新事实再比较，永远等于自身 → "版本变了"这类判别恒为"未变"
        #    （与 ReviewArbiter 的"进度参照取基线快照"是同一类缺陷，务必防退化）。
        prev_facts = dict(cp.last_facts)
        facts = self._collect(cp, kwargs)
        cp.checks_used += 1
        # ② 判定（确定性顺序，先"我方是否还需要"）。用 **prev_facts** 作参照。
        verdict, reason = self._judge(cp, facts, kwargs, prev=prev_facts)
        cp.last_facts.update(facts)
        cp.last_verdict = verdict
        # ③ 记预算 / 下一步落点
        resume_required = False
        next_delay = 0.0
        if verdict in (PROCEED, RELEASE):
            cp.next_delay_sec = 0.0
            cp.released = (verdict == RELEASE)
        elif cp.used_up():
            resume_required = True
            cp.resume_required = True
            cp.next_delay_sec = 0.0
            reason = (f"{reason}；探测次数已达上界（{cp.max_checks} 次/阶梯顶）"
                      "→ 停止自动挂账，交回人工/上游")
        else:
            # 第 N 次探测做完后，"下一次等待" 是阶梯的**下一档**（ladder[N]）——
            # 反例：用 ladder[N-1] 会把降级当成下一次等待时长（等过一次的档不得重复）。
            st = self.backoff.status(cp.checks_used)
            next_delay = st.delay_sec
            cp.next_delay_sec = next_delay
        cp.history.append({
            "at": self._clock(), "action": "probe", "check": cp.checks_used,
            "verdict": verdict, "facts": dict(facts), "reason": reason,
            "next_delay_sec": next_delay, "resume_required": resume_required,
        })
        self._save(cp)
        return self._outcome(cp, verdict, reason=reason,
                             resume_required=resume_required, facts=facts)

    @staticmethod
    def _collect(cp: Checkpoint, kw: Dict[str, Any]) -> Dict[str, Any]:
        """把调用方给的**事实**整理成采集快照（只留可判定项，不塞原始数据）。"""
        out: Dict[str, Any] = {}
        for name in ("confirmed", "version_changed", "event_fired",
                     "resource_ready", "lock_held", "our_need_satisfied"):
            if name in kw and kw[name] is not None:
                out[name] = bool(kw[name])
        if "version" in kw and isinstance(kw["version"], int):
            out["version"] = int(kw["version"])
        return out

    def _judge(self, cp: Checkpoint, facts: Dict[str, Any], kw: Dict[str, Any],
               prev: Optional[Dict[str, Any]] = None) -> Any:
        """**判断检查点**：按判别式给三态 + unknown（不看 agent 自述）。

        Args:
            facts: 本次采集到的事实
            prev: **探测前**的事实快照（版本类判别式的参照；缺省回落 ``cp.last_facts``）
        """
        # 0) 我方不再需要 → 立即放锁（反队头阻塞；比"等对方放锁"更高优先）
        if kw.get("our_need_satisfied") is False:
            return RELEASE, "我方已不再需要该检查点 → 主动放锁，不再占用等待"
        kind = cp.kind
        if kind == CHECK_CONFIRM:
            if isinstance(kw.get("confirmed"), bool):
                return ((PROCEED, "已获得人工/上游确认") if kw["confirmed"]
                        else (WAIT, "尚未获得确认"))
            return UNKNOWN, "缺少确认事实（未给 confirmed）"
        prev = cp.last_facts if prev is None else prev
        if kind == CHECK_LOCK:
            facts = self._collect_lock_facts(cp) or facts
            if "lock_held" in facts:
                if facts["lock_held"]:
                    return WAIT, (f"锁仍在 {facts.get('holder') or '未知持有者'} 手上"
                                  "（持续锁：等对方处理完）")
                return PROCEED, "锁已释放 → 条件满足，可以继续"
            return UNKNOWN, "锁表不可见（未接入锁台账）→ 不误判，继续等"
        if kind == CHECK_VERSION:
            facts2 = self._collect_version_fact(cp) or facts
            if "version_changed" in facts2:
                return ((PROCEED, "版本已变化") if facts2["version_changed"]
                        else (WAIT, "版本未变（无新进展）"))
            if "version" in facts2:
                before = prev.get("version")
                cur = facts2["version"]
                if isinstance(before, int) and before != cur:
                    return PROCEED, f"版本号变化 {before} → {cur}"
                if isinstance(before, int) and before == cur:
                    return WAIT, f"版本号未变（仍为 {cur}）"
                return UNKNOWN, f"首次采集版本 {cur}，无基线可比（下轮再判）"
            return UNKNOWN, "版本台账不可见 → 不误判，继续等"
        if kind == CHECK_EVENT:
            if "event_fired" in facts:
                return ((PROCEED, "外部事件已到达（webhook / resolve）")
                        if facts["event_fired"] else (WAIT, "外部事件尚未到达"))
            if cp.resolve_status == "resolved":
                return PROCEED, "平台侧 resolve 已到达（状态变化已确认）"
            return UNKNOWN, "尚未收到外部事件（等待 webhook / cnb:resolve）"
        if kind == CHECK_RESOURCE:
            if "resource_ready" in facts:
                return ((PROCEED, "外部资源已就绪") if facts["resource_ready"]
                        else (WAIT, "外部资源尚未就绪"))
            return UNKNOWN, "资源就绪信号缺失 → 不误判，继续等"
        return UNKNOWN, f"未知判别式 {kind!r}"

    def _outcome(self, cp: Checkpoint, verdict: str, reason: str = "",
                 resume_required: bool = False,
                 facts: Optional[Dict[str, Any]] = None,
                 action: str = "") -> ProbeOutcome:
        """组装产出：判定 + **结构化事件** + 平台指令。"""
        if verdict == PROCEED:
            driver, act = DRIVER_HANDOFF, "resume"
        elif verdict == RELEASE:
            driver, act = DRIVER_HANDOFF, "release_lock"
        elif resume_required:
            driver, act = DRIVER_HANDOFF, "human_resume"
        else:
            # 与本实例的分工阈值同源（否则同一实例会在 delay_plan/overview 与
            # 探测产出里给出两套落点——那正是"同一份规则出两套动作"的形态）。
            driver = _driver_for_delay(cp.next_delay_sec, self.await_max_sec)
            act = "keep_waiting"
        if action:
            act = action
        event = ProbeEvent(
            checkpoint_key=cp.key,
            kind=cp.kind,
            waiter_event=cp.waiter_event,
            resolver_event=cp.resolver_event,
            verdict=verdict,
            facts=dict(facts or cp.last_facts),
            driver=driver,
            next_delay_sec=cp.next_delay_sec,
            checks_used=cp.checks_used,
            checks_left=cp.checks_left(),
            resume_required=resume_required or cp.resume_required,
            reason=reason,
            action=act,
            wake_allowed=(verdict == PROCEED) and not resume_required,
            budget_trail=self.budget_trail_of(cp),
            at=self._clock(),
        )
        return ProbeOutcome(verdict=verdict, event=event, checkpoint=cp)

    # ------------------------------------------------------------------
    # 事件分发：结构化事件交给下一棒（跨 agent/跨仓），**不靠自述**
    # ------------------------------------------------------------------

    def dispatch(self, event: ProbeEvent) -> Dict[str, Any]:
        """把探测事件转成**给流水线/下一棒的明确指令**（平台落点 + 触发方式）。

        「webhook 触发再跑」在 CNB 上的三种原生写法都在这里给出，按判定结果选：
        - 条件满足 → `cnb:apply`/`cnb:trigger` 触发 `api_trigger_*` 再跑（真正干活）；
        - 仍未满足 → `cnb:await` 挂起等待（零占用），或交 `crontab` 到点代跑；
        - 预算用尽 → 交回人工（`resume_required`，不得据此再开一轮）。

        事件带 `session_id`（跨仓/跨 agent 传递后）时，落点按**它归属的会话**算，
        不再误用本地实例的会话号。
        """
        sid = self.session_id
        raw = event.facts.get("session_id") if isinstance(event.facts, dict) else None
        if _safe_shared_id(raw):
            sid = raw
        if sid == self.session_id:
            return self.dispatch_core(event)
        # 跨会话/跨仓：落点只依赖**会话号 + 规则**，不需要另一个实例。
        return _dispatch_for_session(
            event, sid, self._clock, self.crontab_base_minutes)

    def dispatch_core(
        self,
        event: ProbeEvent,
        checkpoint: Optional[Checkpoint] = None,
    ) -> Dict[str, Any]:
        """dispatch 的**核心实现**（实例内直接调用；跨仓共享走模块级函数）。

        Args:
            event: 探测产出的结构化事件（跨仓时由对端序列化而来）
            checkpoint: 可选上下文（跨仓纯函数路径用；给了就不读本地磁盘）

        返回值统一带 ``budget_trail``（探测预算留痕，🔴A）：接收方把它写进
        评论/交接卡，下一棒（可能是**新容器**）据此 ``max()`` 恢复
        ``checks_used``——否则平台 crontab 每次新容器都从 0 开始，
        "最多 6 次"永不触发。
        """
        out = self._dispatch_core(event, checkpoint=checkpoint)
        trail = event.budget_trail
        if not trail:
            cp = checkpoint
            if cp is None and event.checkpoint_key:
                cp = self.load(event.checkpoint_key)
            trail = self.budget_trail_of(cp) if cp is not None else ""
        if trail:
            out["budget_trail"] = trail
        return out

    def _dispatch_core(
        self,
        event: ProbeEvent,
        checkpoint: Optional[Checkpoint] = None,
    ) -> Dict[str, Any]:
        """落点判定的**纯逻辑**（``dispatch_core`` 负责附加预算留痕）。"""
        if event.verdict == PROCEED:
            return {
                "action": "resume",
                "driver": DRIVER_DELEGATE,
                "trigger": {
                    "type": "cnb:apply",
                    "event": "api_trigger_probe_resume",
                    "env": {"CHECKPOINT_KEY": event.checkpoint_key,
                            "PROBE_VERDICT": event.verdict},
                    "note": "条件已满足：触发一次真正干活的流水线（评审/构建），"
                            "不是又开一轮等待",
                },
                "wake_allowed": True,
                "reason": event.reason,
            }
        if event.verdict == RELEASE:
            return {
                "action": "release_lock",
                "driver": DRIVER_HANDOFF,
                "trigger": None,
                "wake_allowed": False,
                "reason": event.reason + "｜放锁后不再挂检查点（反队头阻塞）",
            }
        if event.resume_required:
            return {
                "action": "human_resume",
                "driver": DRIVER_HANDOFF,
                "trigger": None,
                "wake_allowed": False,
                "reason": event.reason + "｜需人工 resume(by=…, reason=…) 留痕解锁",
            }
        # 落点**按本地规则重算**（同一份规则 → 同一套动作，🔴D 同族加固）：
        # 不轻信事件自带的 `driver` 字段——否则跨仓发送方只要写
        # `driver=platform_await`，就能把 10 分钟的等待也挂成单个 await
        # （= 押注对端一定会 resolve 的死等）。
        driver = _driver_for_delay(event.next_delay_sec, self.await_max_sec)
        v = None
        if driver == DRIVER_AWAIT:
            # 平台语义校验：await 必须**同一事件**配对，且 key 仅能 resolve 一次。
            # 配不上对就降级 crontab——不硬上一个"一定会等不到"的 await。
            # 上下文来源：显式传入（跨仓）→ 本地检查点（本地）。
            cp = checkpoint if checkpoint is not None else self.load(
                event.checkpoint_key)
            v = resolve_driver(
                DRIVER_AWAIT, checkpoint_key=event.checkpoint_key,
                attempt=(cp.checks_used if cp else 0),
                waiter_event=(cp.waiter_event if cp else ""),
                resolver_event=(cp.resolver_event if cp else ""),
                await_max_sec=self.await_max_sec,
            )
            if not v.downgraded:
                return {
                    "action": "platform_await",
                    "driver": DRIVER_AWAIT,
                    "trigger": {
                        "type": "cnb:await",
                        # key 带**轮次**：官方「一个 key 仅能 resolve 一次」，
                        # 复用同一 key 会让第 2 次起永远等不到。
                        "key": v.await_key or await_key_for(
                            event.checkpoint_key,
                            (cp.checks_used if cp else 0)),
                        "note": "零占用挂起；锁/状态变化时由对端 cnb:resolve 唤醒。"
                                "**本地不 sleep、不轮询**",
                    },
                    "wake_allowed": False,
                    "reason": event.reason,
                }
        downgrade_reason = (
            v.reason if v is not None and v.downgraded else event.reason)
        return {
            "action": "platform_crontab",
            "driver": DRIVER_CRONTAB,
            "trigger": {
                "type": "crontab",
                "cron": _cron_fixed_5min(event.next_delay_sec,
                                         self.crontab_base_minutes),
                "event": "api_trigger_probe_check",
                "note": "长等待交**平台**定时任务到点代跑一次探测；平台最小间隔 5 分钟",
            },
            "wake_allowed": False,
            "downgraded": bool(v is not None and v.downgraded),
            "downgrade_reason": downgrade_reason if v is not None
            and v.downgraded else "",
            "reason": event.reason,
        }

    # ------------------------------------------------------------------
    # 视图
    # ------------------------------------------------------------------

    def overview(self) -> Dict[str, Any]:
        """所有检查点总览（"触发了一眼就能看"）。"""
        cps = self.list_open()
        return {
            "session_id": self.session_id,
            "count": len(cps),
            "max_checks": self.max_checks,
            "waiting_plan": self.backoff.human(),
            "checkpoints": [{
                "key": c.key,
                "kind": c.kind,
                "holder": c.holder,
                "scope": c.scope,
                "checks_used": c.checks_used,
                "checks_left": c.checks_left(),
                "next_delay_sec": c.next_delay_sec,
                "driver": _driver_for_delay(c.next_delay_sec, self.await_max_sec)
                if c.next_delay_sec else DRIVER_HANDOFF,
                "last_verdict": c.last_verdict,
                "platform_wait": c.resolve_status or "未挂",
            } for c in cps],
            "where_to_avoid": [
                f"{c.scope or c.key}（等 {c.holder or '未知'}｜还剩 "
                f"{c.checks_left()} 次探测｜下一次 "
                f"{c.next_delay_sec:.0f}s）" for c in cps
            ],
        }

    def delete_state(self) -> bool:
        """会话生命周期回收（同 id 复建不带陈旧检查点）。"""
        removed = False
        if not os.path.isdir(self.data_dir):
            return removed
        for name in os.listdir(self.data_dir):
            if not name.endswith(".json"):
                continue
            try:
                os.remove(os.path.join(self.data_dir, name))
                removed = True
            except OSError:
                pass
        return removed


# ======================================================================
# 跨仓共享（已确认口径 #3）：probe 规则推到组织级
# ======================================================================
# 「探什么、最多探几次、怎么等、判出来怎么走」是 **agent ↔ agent 的协同礼仪**，
# 与任何单个仓库无关——同一份口径要在**所有仓库**生效，否则 A 仓探 6 次、B 仓探 9 次，
# 换仓就行为漂移（正是"循环"的温床）。
#
# 因此本模块提供**不依赖任何实例**的入口：
#   - `probe_verdict_of(event)`：跨仓事件里带的是**判定字符串**（不是本地对象），
#     直接给出对外口径（不再"按本地会话号编造落点"）；
#   - `dispatch_shared(event, rules)`：按**全局规则包**（可来自组织级 YAML）给平台指令。
# 键名白名单与本地同源，跨仓事件不会被不同实现解读成不同动作。

def _safe_shared_id(value: Any) -> bool:
    """跨仓标识是否可用作落点键的**安全**前缀。

    与本地各模块的 ``_SAFE_ID`` 白名单（``[A-Za-z0-9_-]+``，禁 ``/``）**不同**：跨仓协作里
    `dongdongxiaozu/zhuanyemishutuan#14` 这类"仓/号"写法是**正常标识**，
    不能因为带 `/` 就丢掉它（丢了就等于把落点错标成本地会话）。
    这里只挡**真正危险**的形态：路径穿越、路径分隔、空白与控制字符、超长。
    """
    if not isinstance(value, str):
        return False
    if not value or len(value) > 120:
        return False
    if any(ord(ch) < 32 or ch == "\x7f" for ch in value):
        return False
    if any(ch.isspace() for ch in value):
        return False
    if ".." in value or "\\" in value or value.startswith("/"):
        return False
    return True


# 落点视图（给人看/文档引用/角色提示词共用）
DRIVER_ROLE = {
    DRIVER_AWAIT: "首选：状态变化会被对端 resolve → 零占用唤醒（≤ await_max_sec）",
    DRIVER_CRONTAB: "兜底：平台「不睡觉的进程」到点代跑（> await_max_sec，固定 */5）",
    DRIVER_EXTERNAL: "外部 webhook / OPENAPI StartBuild 触发 api_trigger 再跑",
    DRIVER_DELEGATE: "委托持有者完成时回调 api_trigger_* 事件",
    DRIVER_HANDOFF: "用尽/到顶 → 交回人工/上游（不据此再开一轮）",
}


def probe_verdict_of(event: Any) -> str:
    """跨仓口径：从探测事件取**判定字符串**（宽进：非法/缺失 → unknown）。

    跨仓传递的是数据不是对象——对方仓的 `ProbeEvent` 与本仓同名同义，
    这里只认判定值本身，遇到不认识的值一律当 `unknown`（**继续等，不误判放行**）。
    """
    if isinstance(event, ProbeEvent):
        verdict = event.verdict
    elif isinstance(event, dict):
        verdict = event.get("verdict")
    else:
        verdict = None
    return verdict if verdict in ALL_VERDICTS else UNKNOWN


def _dispatch_for_session(
    event: ProbeEvent,
    session_id: str,
    clock: Optional[Callable[[], float]] = None,
    crontab_base_minutes: int = CRON_EVERY_MINUTES,
    await_max_sec: float = AWAIT_MAX_SEC,
    checkpoint: Optional[Checkpoint] = None,
) -> Dict[str, Any]:
    """按**指定会话号**算落点（纯函数：不读不写任何检查点状态）。

    `CheckpointProbe.dispatch()` 与模块级 `dispatch_shared()` **共用这一条**算法——
    两处各写一遍就会分叉（"本地这么判、跨仓那么判"，正是协同漂移的来源）。

    `checkpoint` 为**跨仓/纯函数**路径下的可选上下文：跨仓事件里带的事件名与轮次
    不可能从本地检查点读出来（那是**别人的**会话），只能由事件自己携带。
    """
    if session_id == "":
        session_id = "shared"
    # `mkdir=False`：本函数**只算落点**（声明为纯函数），不得在 cwd 下造目录
    # ——新容器/流水线里凭空出现 `data/probe/` 是污染工作区的副作用。
    probe = CheckpointProbe(
        session_id, backoff=None, clock=clock,
        crontab_base_minutes=crontab_base_minutes, await_max_sec=await_max_sec,
        mkdir=False)
    return probe.dispatch_core(event, checkpoint=checkpoint)


def dispatch_shared(
    event: Any,
    rules: Optional[Any] = None,
    session_id: str = "shared",
    crontab_base_minutes: Optional[int] = None,
) -> Dict[str, Any]:
    """**跨仓共享**的落点判定：同一份规则包 → 同一套动作（不依赖本地实例）。

    Args:
        event: `ProbeEvent`（本仓）或跨仓传来的**字典**（键名口径同源）
        rules: `CollabRules`（组织级/仓库级规则包）；缺省用内置默认口径
        session_id: 兜底会话标识（事件里带 `session_id` 时以事件为准）
        crontab_base_minutes: cron 间隔（分钟）。**缺省从 `rules` 取**
            （`probe.crontab_interval_minutes`）——否则同一份规则包会在
            「本地 `cron_expr_for(rules)`」与「跨仓 `dispatch_shared`」出**两套落点**，
            正是要消灭的跨仓漂移。

    Returns:
        `{"verdict", "action", "driver", "trigger", "wake_allowed", "reason",
        "budget_trail"}`——跨仓接收方据此执行即可，**不必**、也**不该**再按
        自己的本地会话号重算。

    **宽进不炸**：载荷是合法 JSON 但字段类型不符（如
    ``{"next_delay_sec": "soon"}``）时，逐字段守卫已把它退化为默认；
    极端情况下（守卫之外仍抛）一律回落 ``unknown`` → 继续等，**绝不误放行**。
    """
    try:
        ev = (event if isinstance(event, ProbeEvent)
              else ProbeEvent.from_dict(event) or ProbeEvent())
        sid = session_id
        raw = ev.facts.get("session_id") if isinstance(ev.facts, dict) else None
        if _safe_shared_id(raw):
            sid = raw
        await_max = getattr(rules, "probe_await_max_sec", AWAIT_MAX_SEC)
        if not isinstance(await_max, (int, float)) or isinstance(await_max, bool):
            await_max = AWAIT_MAX_SEC
        # 🔴D-2：cron 间隔**缺省从规则包取**（不再硬编码 5）——
        # 否则同一份规则包会在「本地 cron_expr_for(rules)」与「跨仓 dispatch_shared」
        # 出两套落点，正是要消灭的跨仓漂移。
        minutes = (crontab_minutes_from_rules(rules)
                   if crontab_base_minutes is None else int(crontab_base_minutes))
        # 跨仓上下文：**只能由事件自带**（对方的检查点不在本地磁盘上，
        # 也不能按本地会话号去读——那会读到别人的/读不到）。
        # 事件没带侧边事件名时 → 两侧均不可判定 → 落点保守降级 crontab（平台兜底）。
        ctx = None
        facts = ev.facts if isinstance(ev.facts, dict) else {}
        if ev.waiter_event or ev.resolver_event or facts.get("waiter_event") \
                or facts.get("resolver_event") or facts.get("checks_used"):
            ctx = Checkpoint(
                key=str(event.get("checkpoint_key") if isinstance(event, dict)
                        else ev.checkpoint_key or "shared"),
                waiter_event=str(ev.waiter_event or facts.get("waiter_event") or ""),
                resolver_event=str(ev.resolver_event
                                   or facts.get("resolver_event") or ""),
            )
            try:
                ctx.checks_used = int(facts.get("checks_used") or 0)
            except (TypeError, ValueError):
                ctx.checks_used = 0
        out = _dispatch_for_session(ev, sid, None, minutes, await_max,
                                    checkpoint=ctx)
    except Exception:          # pragma: no cover - 兜底：坏载荷不得炸跨仓入口
        ev = ProbeEvent()
        sid = session_id
        out = {
            "action": "platform_await",
            "driver": DRIVER_AWAIT,
            "trigger": {
                "type": "cnb:await",
                "key": f"{session_id}-unknown",
                "note": "载荷无法解析 → 保守继续等（绝不误放行）",
            },
            "wake_allowed": False,
            "reason": "载荷解析失败：判不出 → unknown → 继续等",
        }
    out["verdict"] = probe_verdict_of(ev)
    out["session_id"] = sid          # 归属会话随事件流转（接收方据此知道是谁的检查点）
    out["shared"] = True
    return out


def organization_probe_rules(
    path: Optional[str] = None,
    rules: Optional[Any] = None,
) -> Dict[str, Any]:
    """组织级共享的 probe 口径**快照**（"这套数值全组织共用同一份"的证据）。

    装入口径见 `rules.load_rules`：**组织级 → 仓库级 → 内置默认**。
    `rules` 已给（或 `path` 已给）时以它为口径，否则现取组织级那份。
    """
    from .rules import load_rules     # 局部导入：避免与 rules → probe 的依赖成环
    loaded = rules if rules is not None else load_rules(path, scope="organization")
    return {
        "source": loaded.source,
        "scope": loaded.scope,
        "max_checks": loaded.probe_max_checks,
        "await_max_sec": loaded.probe_await_max_sec,
        "crontab_min_interval_sec": loaded.crontab_min_interval_sec,
        "crontab_interval": cron_expr_for(loaded),
        "drivers": list(loaded.probe_drivers),
        "checks": list(loaded.probe_checks),
        "split_note": TWO_WAY_SPLIT_NOTE,
        "local_cpu": "0（探测点全部落平台语义；本地不 sleep、不轮询）",
    }
