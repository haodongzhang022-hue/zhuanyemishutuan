"""M10-a 三层上下文装配器：静态层 + 会话态层 + 规则注入层

设计原则：
  层A(静态·缓存键): 角色定义 + 岗位前缀 + 行为/输出契约
    ⇒ 缓存命中键只看这一层，session 内恒定
  层B(会话态): StateCard 展开要素（compact_context）
    ⇒ 一个 session 一份，按需刷新
  层C(注入·按规则): 规则命中才拼装的本次动作相关片段
    ⇒ 独立注入，与 A 层完全隔离

核心铁律：静态层永不拼接动态文本；动态上下文一律走独立注入通道。
"""
from __future__ import annotations

import hashlib
from typing import Dict, List, Optional, Any, Callable

from .rules import RuleEngine, ContextRule
from .slib_guard import SLIBAssemblyGuard, SLIBBoundMismatch


# 分层标记
LAYER_A_MARK = "### 静态上下文（缓存键域）###"
LAYER_B_MARK = "### 会话态上下文 ###"
LAYER_C_MARK = "### 规则注入上下文 ###"


class ContextAssembler:
    """M10 上下文装配器：把一份"会话上下文"按三层物理隔离拼装。

    典型用法：
      assembler = ContextAssembler(prefix_archive, rule_engine)
      result = assembler.assemble(
          session_id="sess-1",
          role_name="secretary",
          unit_id="dongjiagoushi",
          user_text="帮我修改 src/secretary/context/assembler.py",
          state_card=card,           # 或 state_card_provider 回调
          static_prefix="[秘书]",
          extra_sources={},          # 注入数据源 provider 回调字典
      )
    """

    def __init__(
        self,
        rule_engine: Optional[RuleEngine] = None,
        static_prefix_provider: Optional[Callable] = None,
        state_card_provider: Optional[Callable] = None,
        source_providers: Optional[Dict[str, Callable]] = None,
        slib_guard: Optional[SLIBAssemblyGuard] = None,
    ):
        """
        Args:
            rule_engine: 规则引擎实例（M10-b）
            static_prefix_provider: 回调 (unit_id, role_name) -> str 静态前缀/角色骨架
            state_card_provider: 回调 (session_id) -> StateCard 或 None
            source_providers: 注入数据源 provider 映射，
                {data_source标识: callable(ctx) -> str}
                例：{"file.diff": lambda ctx: "...", "handoff.card": ...}
        """
        self.rule_engine = rule_engine or RuleEngine()
        self.static_prefix_provider = static_prefix_provider
        self.state_card_provider = state_card_provider
        self.source_providers = source_providers or {}
        self.slib_guard = slib_guard or SLIBAssemblyGuard()

    # ---- 三层拼装 ----

    def assemble(
        self,
        session_id: str,
        role_name: str = "secretary",
        unit_id: str = "",
        user_text: str = "",
        static_prefix: Optional[str] = None,
        state_card: Any = None,
        objective: str = "",
    ) -> Dict[str, Any]:
        """
        对一次调用执行三层装配。

        Returns:
            {
                "layer_a": str,          # 静态层(缓存键域)
                "layer_b": str,          # 会话态层
                "layer_c": str,          # 规则注入层
                "full_context": str,     # 完整分层拼装
                "hit_rules": [...],      # 命中规则列表
                "cache_key": str,        # 仅层A的哈希(缓存签名)
                "layer_sizes": {...},    # 各层字节数
            }
        """
        # ---- 层A：静态上下文（缓存键域） ----
        layer_a = self._build_layer_a(static_prefix, unit_id, role_name)

        # ---- 层B：会话态上下文 ----
        layer_b = self._build_layer_b(session_id, state_card, objective)

        # ---- 层C：规则注入上下文 ----
        hit_rules = self.rule_engine.match(user_text) if user_text else []
        layer_c_parts = []
        layer_c_meta = []

        for rule in hit_rules:
            if rule.target_layer != "C":
                continue
            # 调用对应的数据源 provider 获取片段
            if rule.data_source in self.source_providers:
                try:
                    fragment = self.source_providers[rule.data_source]({
                        "session_id": session_id,
                        "rule": rule,
                        "user_text": user_text,
                        "unit_id": unit_id,
                        "role_name": role_name,
                    })
                    if fragment:
                        layer_c_parts.append(fragment)
                        layer_c_meta.append({
                            "rule_id": rule.rule_id,
                            "name": rule.name,
                            "data_source": rule.data_source,
                        })
                except Exception:
                    continue  # provider 异常不影响整体装配

        layer_c = "\n".join(layer_c_parts) if layer_c_parts else ""

        # ---- 完整拼装（三层物理隔离，非文本拼接） ----
        full_parts = []
        full_parts.append(f"{LAYER_A_MARK}\n{layer_a}")
        if layer_b:
            full_parts.append(f"{LAYER_B_MARK}\n{layer_b}")
        if layer_c:
            full_parts.append(f"{LAYER_C_MARK}\n{layer_c}")
        full_context = "\n\n".join(full_parts)

        return {
            "layer_a": layer_a,
            "layer_b": layer_b,
            "layer_c": layer_c,
            "full_context": full_context,
            "hit_rules": [r.to_dict() for r in hit_rules],
            "hit_rule_ids": [r.rule_id for r in hit_rules],
            "cache_key": self._gen_cache_key(layer_a),
            "layer_sizes": {
                "A": len(layer_a.encode("utf-8")),
                "B": len(layer_b.encode("utf-8")) if layer_b else 0,
                "C": len(layer_c.encode("utf-8")) if layer_c else 0,
            },
        }

    def assemble_bound(
        self,
        session_id: str,
        role_name: str = "secretary",
        unit_id: str = "",
        industry: str = "",
        user_text: str = "",
        static_prefix: Optional[str] = None,
        state_card: Any = None,
        objective: str = "",
        auto_bind: bool = True,
    ) -> Dict[str, Any]:
        """SLIB 会话级装配：在"被锁定行业"内执行三层装配。

        在调用 assemble 之前先用 SLIB 装配守卫把 unit_id 与会话绑定对齐：
          - 会话已绑定行业 X：若请求装配 unit_id != X → 抛 SLIBBoundMismatch；
          - 会话未绑定：按 auto_bind 将首装行业写入绑定后放行；
          - 无 session_id：退化为普通 assemble（静态单发）。

        由此保证一次会话调用只装配"被锁定行业"的上下文，杜绝跨/全量注入。
        """
        resolved_unit = self.slib_guard.resolve_unit(
            session_id=session_id,
            unit_id=unit_id,
            industry=industry,
            role_name=role_name,
            auto_bind=auto_bind,
        )
        return self.assemble(
            session_id=session_id,
            role_name=role_name,
            unit_id=resolved_unit,
            user_text=user_text,
            static_prefix=static_prefix,
            state_card=state_card,
            objective=objective,
        )

    def _build_layer_a(
        self,
        static_prefix: Optional[str],
        unit_id: str,
        role_name: str,
    ) -> str:
        """构建层A：静态上下文（缓存键域）

        内容：角色定义 + 岗位前缀 + 行为/输出契约。
        来源：prefix_provider 回调或直接传参。
        """
        parts = []
        # 静态前缀
        prefix = static_prefix
        if not prefix and self.static_prefix_provider and unit_id:
            try:
                prefix = self.static_prefix_provider(unit_id, role_name)
            except Exception:
                pass
        if prefix:
            parts.append(prefix)

        # 角色标识
        role_line = f"[{role_name}]" if role_name else ""
        if role_line:
            parts.append(role_line)
        if unit_id:
            parts.append(f"unit={unit_id}")
        parts.append("session_context_static=true")

        return " | ".join(parts)

    def _build_layer_b(
        self,
        session_id: str,
        state_card: Any,
        objective: str,
    ) -> str:
        """构建层B：会话态上下文

        内容：StateCard compact_context 展开要素 + 折叠引用。
        """
        parts = []
        # 尝试从 state_card 对象直接提取
        if state_card is not None:
            # 如果有 compact_context 方法（StateCard 或 FoldEngine）
            if hasattr(state_card, "compact_context"):
                try:
                    ctx = state_card.compact_context()
                    if ctx:
                        parts.append(ctx)
                except Exception:
                    pass
            elif hasattr(state_card, "summarize_for_upstream"):
                try:
                    summ = state_card.summarize_for_upstream()
                    if isinstance(summ, dict):
                        parts.append(
                            f"目标: {summ.get('objective', '')}\n"
                            f"进度: {summ.get('done_count', 0)}/{summ.get('task_count', 0)} 完成\n"
                            f"会话: {session_id}"
                        )
                except Exception:
                    pass
            elif hasattr(state_card, "objective"):
                try:
                    obj = state_card.objective or ""
                    if obj:
                        parts.append(f"目标: {obj}")
                except Exception:
                    pass

        # 如果没提供 state_card，但配置了 state_card_provider，尝试回调
        if not parts and self.state_card_provider:
            try:
                card = self.state_card_provider(session_id)
                if card is not None:
                    return self._build_layer_b(session_id, card, objective)
            except Exception:
                pass

        # 兜底：至少给 objective
        if objective:
            parts.append(f"目标: {objective}")

        return "\n".join(parts)

    @staticmethod
    def _gen_cache_key(layer_a: str) -> str:
        """生成缓存键：仅对层A（静态上下文）做哈希。

        保证：动态上下文(层B/C)变化不影响缓存键。
        """
        if not layer_a:
            return ""
        return hashlib.sha256(layer_a.encode("utf-8")).hexdigest()[:16]

    def get_fragment_size_stats(self, result: Dict[str, Any]) -> Dict[str, int]:
        """获取装配结果各层字节统计"""
        return result.get("layer_sizes", {})


# 便捷函数：快速构造默认装配器
def default_assembler(
    rules_file: Optional[str] = None,
    slib_guard: Optional["SLIBAssemblyGuard"] = None,
    **source_providers,
) -> ContextAssembler:
    """构造带默认规则引擎和自定义数据源 provider 的装配器

    slib_guard: 可选 SLIB 装配守卫实例（用于测试注入自定义 binding_store）
    """
    rule_engine = RuleEngine(rules_file) if rules_file else RuleEngine()
    return ContextAssembler(
        rule_engine=rule_engine,
        source_providers=source_providers,
        slib_guard=slib_guard,
    )
