"""秘书会话门面：一个 L3 会话秘书的完整统一入口

将 StateManager / FoldEngine / ContentArchive / ContextAssembler / HandoffBus
整合为一个可直接使用的会话管理接口。

一个 SecretarySession 实例 = 一个 L3 会话秘书 = 一个会话的完整状态管理者。
"""
from .session import SecretarySession

__all__ = ["SecretarySession"]
