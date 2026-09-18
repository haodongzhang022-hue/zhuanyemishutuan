"""collab: **全局跨仓库协同规则**的单一实现（不绑定任何单一仓库）

本包承载"agent 协同时的规则"——适用于**全部仓库**、与具体业务无关的协作参数：
等待/退避节奏、协同预算口径、平台计费口径（等待不等于占 CPU）。

- `backoff`（`StepBackoff`）：阶梯退避（从少到多，到顶即停）
- `rules`（`CollabRules`）：整包协同参数 + YAML 装载（仓库可覆写）
- `wait_policy`（`WaitPolicy`）：把"等多久"翻译成"怎么等"——**等待交平台挂起，
  本地不做 CPU 轮询**（回答"等待会不会占用构建 CPU 计费"）

口径固化在仓库根 `shared/collab/*.yaml`（跨仓库共用一份），
本包只做**解析与执行**，不重复定义数值（单一语义源）。
"""
from .backoff import (
    DEFAULT_LADDER,
    DEFAULT_MAX_ATTEMPTS,
    BackoffStatus,
    StepBackoff,
)
from .rules import CollabRules, PlatformCostNote, load_rules
from .wait_policy import (
    ALL_WAIT_MODES,
    DEFERRED_REVIEW,
    PLATFORM_SUSPEND,
    RESUME_REQUIRED,
    WaitDecision,
    WaitPolicy,
    cpu_cost_of_sleep,
)

__all__ = [
    "StepBackoff",
    "BackoffStatus",
    "DEFAULT_LADDER",
    "DEFAULT_MAX_ATTEMPTS",
    "CollabRules",
    "PlatformCostNote",
    "load_rules",
    "WaitPolicy",
    "WaitDecision",
    "PLATFORM_SUSPEND",
    "DEFERRED_REVIEW",
    "RESUME_REQUIRED",
    "ALL_WAIT_MODES",
    "cpu_cost_of_sleep",
]
