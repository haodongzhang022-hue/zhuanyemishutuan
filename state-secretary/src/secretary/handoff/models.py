"""数据模型定义"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


@dataclass
class Role:
    """行业单元内的岗位定义"""
    name: str
    prefix: str
    handoff: str = "relay"      # relay | direct
    composite: bool = True
    description: str = ""
    keywords: List[str] = field(default_factory=list)  # 岗位触发关键词（单元自带，数据驱动）

    def get_keywords(self) -> List[str]:
        """
        获取该岗位的全部触发关键词。
        优先级：显式 keywords > description 拆分 > name 本身。
        """
        kws = list(self.keywords)
        if self.description and not kws:
            # description 中的空格分词作为隐式关键词
            kws.extend([w.strip() for w in self.description.replace(",", " ")
                        .replace("，", " ").split() if w.strip()])
        if not kws:
            kws.append(self.name)
        return kws


@dataclass
class IndustryUnit:
    """一个行业单元（如 dongjiagoushi = 软件研发·架构团队）"""
    unit_id: str
    industry: str
    industry_alias: List[str]
    roles: List[Role]
    entry_ref: str = ""
    capability_tags: List[str] = field(default_factory=list)
    register_version: str = "1.0"

    def role_by_name(self, name: str) -> Optional[Role]:
        for r in self.roles:
            if r.name == name:
                return r
        return None


@dataclass
class RouteResult:
    """路由判定结果"""
    unit_id: str
    industry: str
    roles: List[str]          # 命中的岗位列表（可能多个=复合）
    confidence: float         # 0-1
    matched_keywords: Dict[str, List[str]] = field(default_factory=dict)
    is_ambiguous: bool = False
    needs_manual: bool = False  # 需回落L2人工确认
    fallback_reason: str = ""


@dataclass
class CompositePlan:
    """M6 复合装配计划"""
    unit_id: str
    session_industry: str
    roles: List[Role]
    session_prefix_set: List[str]  # 稳定前缀集合
    execution_order: List[str]     # 岗位执行顺序（依赖序）


@dataclass
class HandoffCard:
    """M5 跨单元交接卡"""
    source_unit: str
    target_unit: str
    intent: str
    context: str = ""
    status: str = "created"  # created | in_progress | done | failed
    state_card_id: Optional[str] = None  # 关联 M9 StateCard 会话ID（M9 接入主链路）
    created_at: Optional[str] = None
    id: Optional[str] = None  # 运行时生成的唯一ID

    def completeness_check(self) -> dict:
        """完整性校验（M5 × M9 跨单元标准字段）：
        校验交接卡是否包含执行所需的标准字段。
        - source_unit / target_unit / intent: 必填
        - state_card_id: M9 接入后为标准字段（创建时自动关联）
        返回 {passed: bool, missing: [str]}
        """
        missing = []
        if not self.source_unit:
            missing.append("source_unit")
        if not self.target_unit:
            missing.append("target_unit")
        if not self.intent:
            missing.append("intent")
        if not self.state_card_id:
            missing.append("state_card_id")
        return {
            "passed": len(missing) == 0,
            "missing": missing,
            "card_id": self.id or "",
        }

    def validate(self) -> bool:
        """严格校验：缺任何标准字段则抛 ValueError"""
        result = self.completeness_check()
        if not result["passed"]:
            raise ValueError(
                f"HandoffCard 缺少标准字段: {', '.join(result['missing'])}"
            )
        return True
