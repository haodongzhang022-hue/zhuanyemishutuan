"""SLIB 上下文装配守卫（Session-Level Industry Binding · Assembly Guard）

会话级行业绑定装配守卫：
- 一个会话只装配"被锁定行业"的上下文。
- 本模块在 state-secretary 中作为可选装配约束存在。
- 若无 session_binding 后端（未集成路由引擎），守卫自动降级不拦截。

说明：ISSUE-001 状态管理秘书机制中，缓存命中核心依赖「稳定前缀 + 静态层隔离」，
SLIB 是路由层的额外行业隔离约束。本模块保留了接口但允许无后端降级。
"""
from __future__ import annotations

from typing import Optional


class SLIBBoundMismatch(Exception):
    """装配行业与会话绑定行业不一致时抛出的守卫异常"""

    def __init__(self, session_id: str, bound_unit: str,
                 bound_industry: str, attempt_unit: str):
        self.session_id = session_id
        self.bound_unit = bound_unit
        self.bound_industry = bound_industry
        self.attempt_unit = attempt_unit
        super().__init__(
            f"SLIB 装配守卫：会话 {session_id} 已绑定行业"
            f"「{bound_industry}」({bound_unit})，禁止装配非绑定行业"
            f"上下文 {attempt_unit}。跨行业请开新会话。"
        )


class SLIBAssemblyGuard:
    """基于 SessionBindingStore 的装配守卫。

    当未接入路由引擎的 session_binding 后端时，守卫自动降级
    （仅保留接口，不做实际拦截），保证 state-secretary 模块可独立运行。
    """

    def __init__(self, binding_store=None):
        self._binding_store = binding_store
        self._store_available = False
        try:
            from secretary.session_binding import SessionBindingStore
            self._binding_store = self._binding_store or SessionBindingStore()
            self._store_available = True
        except (ImportError, AttributeError):
            # 未接入 session_binding → 降级为不拦截（模块可独立运行）
            pass

    def resolve_unit(self, session_id: str, unit_id: str,
                     industry: str = "", role_name: str = "",
                     auto_bind: bool = True) -> str:
        """把"待装配的 unit_id"与会话绑定对齐，返回最终应装配的 unit_id。

        无 session_binding 后端时直接返回原 unit_id（不约束）。
        """
        if not self._store_available or not session_id:
            return unit_id

        binding = self._binding_store.get_binding(session_id)
        if binding is not None:
            # 已绑定行业
            if unit_id and unit_id != binding.unit_id:
                raise SLIBBoundMismatch(
                    session_id, binding.unit_id, binding.industry, unit_id)
            return binding.unit_id

        # 未绑定
        if not unit_id:
            raise ValueError(
                f"SLIB 装配守卫：会话 {session_id} 尚未绑定行业，"
                f"且未提供 unit_id，无法确定装配哪个行业上下文。"
            )
        if auto_bind:
            try:
                self._binding_store.bind(session_id, unit_id, industry or unit_id,
                                         role_name)
            except ValueError as exc:
                binding = self._binding_store.get_binding(session_id)
                if binding is None:
                    raise exc
                if binding.unit_id != unit_id:
                    raise SLIBBoundMismatch(
                        session_id, binding.unit_id, binding.industry, unit_id)
        return unit_id
