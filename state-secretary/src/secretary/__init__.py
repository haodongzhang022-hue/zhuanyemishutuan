"""secretary: 状态管理秘书模块

对应 ISSUE-001 状态管理秘书机制（层级状态管理 + 上下文折叠编号检索 + 缓存命中保障）。

核心子模块：
- secretary.state: M9 状态管理层 — StateManager/FoldEngine/Hierarchy/ContentArchive
- secretary.context: M10 上下文装配层 — 三层物理隔离 + 稳定前缀
- secretary.handoff: M5 交接总线 — 与状态管理秘书协同
"""

from . import state
from . import context
from . import handoff

__all__ = ["state", "context", "handoff"]
