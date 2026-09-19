"""secretary: 状态管理秘书模块

对应 ISSUE-001 状态管理秘书机制（层级状态管理 + 上下文折叠编号检索 + 缓存命中保障）。

核心子模块：
- secretary.state:   M9 状态管理层 — StateManager/FoldEngine/Hierarchy/ContentArchive
- secretary.context: M10 上下文装配层 — 三层物理隔离 + 稳定前缀
- secretary.handoff: M5 交接总线 — 与状态管理秘书协同
- secretary.session: 会话门面层 — SecretarySession（L3 会话秘书统一入口）
- secretary.gate:    增量跑测门禁 — 只跑有效增量 / 反全量冗余 / 计数权责一致
                    （Issue #14）

使用示例：
    from secretary.session import SecretarySession

    sec = SecretarySession("issue-42", objective="实现状态管理秘书机制")
    sec.add_task("设计数据模型", task_type="deliverable")
    sec.fold("需求澄清内容…", meta={"objective": "澄清需求"})
    ctx = sec.get_compact_context()
"""

from . import state
from . import context
from . import handoff
from . import session
from . import gate

__all__ = ["state", "context", "handoff", "session", "gate"]
