"""M10-b 声明式规则表：本地触发上下文装配规则引擎

核心原则（R3）：
  把"何时需要哪些信息"写成声明式触发规则表，
  按规则命中才去本地拉取/装配对应片段。
  没命中规则的信息，一律不进本次调用。
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Any

# ---------- 数据模型 ----------

@dataclass
class ContextRule:
    """一条上下文装配触发规则"""
    rule_id: str              # 规则唯一编号，如 R3-FILE-01
    name: str                 # 规则名称
    description: str = ""     # 规则描述
    triggers: List[str] = field(default_factory=list)   # 触发关键词/模式列表
    target_layer: str = "C"   # 目标层：A=静态, B=会话态, C=注入层
    data_source: str = ""     # 数据来源标识（如 "file.diff", "state.card", "handoff"）
    enabled: bool = True      # 是否启用
    priority: int = 10        # 优先级（数字小先评估）
    match_mode: str = "keyword"  # keyword(关键词包含) / regex(正则) / both

    def match(self, text: str) -> bool:
        """判断文本是否命中此规则"""
        if not self.enabled or not text:
            return False
        if self.match_mode in ("keyword", "both"):
            for trig in self.triggers:
                if trig and trig.lower() in text.lower():
                    return True
        if self.match_mode in ("regex", "both"):
            for pat in self.triggers:
                try:
                    if re.search(pat, text, re.IGNORECASE):
                        return True
                except re.error:
                    continue
        return False

    def to_dict(self) -> dict:
        return {
            "rule_id": self.rule_id,
            "name": self.name,
            "description": self.description,
            "triggers": self.triggers,
            "target_layer": self.target_layer,
            "data_source": self.data_source,
            "enabled": self.enabled,
            "priority": self.priority,
            "match_mode": self.match_mode,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "ContextRule":
        """从字典构造，容错缺失字段"""
        known = {"rule_id", "name", "description", "triggers",
                 "target_layer", "data_source", "enabled",
                 "priority", "match_mode"}
        clean = {k: v for k, v in data.items() if k in known}
        return cls(**clean)


# ---------- 默认规则集（内置兜底） ----------

def default_rules() -> List[ContextRule]:
    """内置默认触发规则表。

    覆盖常见上下文装配场景：
    1. 文件/代码相关操作 → 装配 file.diff
    2. 状态管理相关操作 → 装配 state.card
    3. 交接相关 → 装配 handoff.card
    4. 架构/设计 → 装配 architecture
    5. 测试 → 装配 test.code
    """
    return [
        ContextRule(
            rule_id="R3-FILE-01",
            name="文件/代码操作触发",
            description="当用户提及具体文件或代码修改时，触发装配文件diff/代码上下文",
            triggers=["修改", "编辑", "改动", "修复", "重构", "删除", "新增文件",
                      ".py", ".js", ".ts", ".json", ".yaml", ".yml", ".md",
                      "代码", "code", "文件", "file", "diff", "merge"],
            target_layer="C",
            data_source="file.diff",
            priority=5,
        ),
        ContextRule(
            rule_id="R3-STAT-01",
            name="状态管理触发",
            description="当涉及任务状态/进度/StateCard操作时，装配会话状态上下文",
            triggers=["状态", "进度", "任务", "StateCard", "state", "session",
                      "折叠", "fold", "提交状态", "更新状态", "查看进度"],
            target_layer="B",
            data_source="state.card",
            priority=3,
        ),
        ContextRule(
            rule_id="R3-HAND-01",
            name="交接/接力触发",
            description="当涉及跨单元交接/接力时，装配交接卡上下文",
            triggers=["交接", "接力", "转交", "handoff", "relay",
                      "跨单元", "跨行业", "传递"],
            target_layer="C",
            data_source="handoff.card",
            priority=5,
        ),
        ContextRule(
            rule_id="R3-ARCH-01",
            name="架构/设计触发",
            description="当涉及架构/技术方案/设计时，装配架构文档上下文",
            triggers=["架构", "设计", "方案", "架构图", "技术方案",
                      "系统设计", "architecture", "design"],
            target_layer="C",
            data_source="architecture.docs",
            priority=7,
        ),
        ContextRule(
            rule_id="R3-TEST-01",
            name="测试触发",
            description="当涉及测试/回归/验证时，装配测试代码上下文",
            triggers=["测试", "回归", "验证", "test", "pytest",
                      "冒烟", "用例", "断言", "通过"],
            target_layer="C",
            data_source="test.code",
            priority=7,
        ),
        ContextRule(
            rule_id="R3-DATA-01",
            name="数据/规则配置触发",
            description="当涉及数据/配置文件时，装配数据驱动上下文",
            triggers=["注册表", "注册", "yaml", "规则表", "声明式",
                      "registry", "config", "配置", "数据驱动"],
            target_layer="C",
            data_source="data.files",
            priority=9,
        ),
    ]


# ---------- 规则引擎 ----------

class RuleEngine:
    """M10-b 规则引擎：加载规则表 + 触发匹配。

    职责：
    1. 从声明式 YAML / 内置默认规则集加载规则
    2. 对输入意图文本做规则匹配，返回命中的规则列表
    3. 支持规则按优先级排序
    4. 规则表可数据驱动扩展（改动不加代码）
    """

    def __init__(self, rules_file: Optional[str] = None):
        self.rules_file = rules_file
        self._rules: List[ContextRule] = []
        if rules_file and os.path.exists(rules_file):
            self._load_from_file(rules_file)
        else:
            # 默认使用内置规则集
            self._rules = default_rules()

    def _load_from_file(self, rules_file: str):
        """从 YAML 文件加载规则"""
        try:
            import yaml
            with open(rules_file, "r", encoding="utf-8") as f:
                data = yaml.safe_load(f)
            if data and "rules" in data:
                for item in data["rules"]:
                    rule = ContextRule.from_dict(item)
                    self._rules.append(rule)
        except Exception:
            # 加载失败回落内置默认规则
            self._rules = default_rules()

    def add_rule(self, rule: ContextRule):
        """动态添加一条规则"""
        self._rules.append(rule)

    def remove_rule(self, rule_id: str) -> bool:
        """按ID移除规则"""
        before = len(self._rules)
        self._rules = [r for r in self._rules if r.rule_id != rule_id]
        return len(self._rules) < before

    def list_rules(self) -> List[ContextRule]:
        """列出全部启用规则"""
        return [r for r in self._rules if r.enabled]

    def match(self, text: str) -> List[ContextRule]:
        """对输入文本做规则匹配，返回命中的启用规则（按优先级排序）。

        这是 R3 的核心入口：按文本信号判断需要装配哪些上下文片段。
        """
        if not text:
            return []
        hits = []
        for rule in self._rules:
            if not rule.enabled:
                continue
            if rule.match(text):
                hits.append(rule)
        hits.sort(key=lambda r: r.priority)
        return hits

    def match_by_layer(self, text: str, layer: str) -> List[ContextRule]:
        """仅匹配目标层命中规则（如只看层C注入规则）"""
        return [r for r in self.match(text) if r.target_layer == layer]

    def summary(self) -> str:
        """规则表摘要"""
        lines = []
        for r in self._rules:
            status = "✅" if r.enabled else "⏸️"
            lines.append(f"  {status} {r.rule_id} {r.name} [层{r.target_layer}] "
                         f"src={r.data_source} triggers={len(r.triggers)}")
        return "\n".join(lines) if lines else "  (空规则表)"
