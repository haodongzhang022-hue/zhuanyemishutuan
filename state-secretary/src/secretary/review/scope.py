"""审查范围分级（SCAN vs FULL）：**日常只审变化，全量只在大版本**

## 需求（Issue #14 补充，用户口径）

> 注意不要这样大量全量，只在大版本可以进行全量，日常开发不进行全量，只审查变化就行，
> 防止无效构建和测试。

配套上界的由来：循环里真正拖垮效率的往往不是"有没有上界"，而是**每一轮都触发全量**
——全量构建 + 全量测试在往返轮次里被反复执行，产生大量无效作业。故除了轮次上界，
还需要一道**范围上界**：

| 档位 | 触发条件 | 审查范围 |
|---|---|---|
| `full` | 大版本（`major-version` 标签 / `--全量` 显式请求 / `major_version=True`） | 全量（含全量测试） |
| `scan` | 其余全部情形（日常开发 / 常规评审 / 修复往返） | **只审变化**（变更集 + 受影响面） |

**判定顺序（保守）**：显式请求全量 → 大版本标签 → 默认降级为 `scan`。
即"拿不准就不全量"——宁可少跑一次全量，也不在日常开发里空烧构建与测试。

## 判定入口

- ``decide_scan(tags=…, text=…, major_version=…)`` → ``ScanContext``
- ``SecretarySession.review_scope(...)`` 门面调用
- 文本信号：大版本关键词（`大版本` / `major` / `全量` / `发版` / `release`）
  + 显式"只要变化"关键词（`增量` / `只审变化` / `scan`）——后者优先级最高。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional

SCAN = "scan"      # 只审变化（日常开发默认）
FULL = "full"      # 全量（仅大版本）

# 大版本（全量）标签
FULL_TAGS = ("major-version", "major", "release", "大版本", "全量")

# 文本信号：全量
FULL_TEXT_PATTERNS = (
    r"大版本", r"发版", r"全量", r"全量审", r"全量回归", r"全量测试",
    r"major[\s_-]?version", r"\brelease\b", r"full[\s_-]?scan",
)

# 文本信号：明确只要变化（最高优先，压过全量信号——"先完成该做的"）
SCAN_TEXT_PATTERNS = (
    r"只审变化", r"仅审变化", r"只看变化", r"只审查变化",
    r"增量审", r"增量审查", r"不要全量", r"不用全量", r"别全量",
    r"\bscan\b", r"\bincremental\b",
)


@dataclass
class ScanContext:
    """一次审查的范围判定结果（给评审/测试角色的执行口径）。"""

    mode: str = SCAN
    reason: str = ""
    signals: List[str] = field(default_factory=list)
    tags: List[str] = field(default_factory=list)

    @property
    def is_full(self) -> bool:
        return self.mode == FULL

    @property
    def is_scan(self) -> bool:
        return self.mode == SCAN

    def to_dict(self) -> dict:
        return {
            "mode": self.mode,
            "reason": self.reason,
            "signals": list(self.signals),
            "tags": list(self.tags),
            "is_full": self.is_full,
            "review_changes_only": self.is_scan,
            "guidance": (
                "全量：可跑全量构建 + 全量测试（大版本专用）"
                if self.is_full else
                "只审变化：仅审变更集与受影响面，不触发全量构建/测试"
            ),
        }


def _norm_tags(tags: Any) -> List[str]:
    """标签归一化（字符串 / 可迭代 → 小写去空白清单）。"""
    if tags is None:
        return []
    if isinstance(tags, str):
        raw: Iterable[Any] = tags.replace("，", ",").split(",")
    elif isinstance(tags, (list, tuple, set)):
        raw = tags
    else:
        return []
    out = []
    for t in raw:
        if not isinstance(t, str):
            continue
        t = t.strip().lower()
        if t:
            out.append(t)
    return out


def _has_pattern(text: str, patterns) -> Optional[str]:
    for pat in patterns:
        if re.search(pat, text, re.IGNORECASE):
            return pat
    return None


def decide_scan(
    tags: Any = None,
    text: str = "",
    major_version: bool = False,
) -> ScanContext:
    """判定审查范围：**只在大版本做全量**，其余一律只审变化。

    Args:
        tags: 版本标签（如 ``["major-version"]``）
        text: 触发文本（Issue/PR 评论、变更说明等）
        major_version: 显式大版本标记（最高优先）

    Returns:
        ``ScanContext``：``mode`` 为 ``scan`` / ``full``。
    """
    tag_list = _norm_tags(tags)
    body = text if isinstance(text, str) else ""
    signals: List[str] = []

    if major_version is True:
        signals.append("major_version=True")
        return ScanContext(FULL, "显式大版本标记 → 全量", signals, tag_list)

    # 明确"只要变化"优先：即便带了大版本标签，也尊重显式请求
    only_changes = _has_pattern(body, SCAN_TEXT_PATTERNS)
    if only_changes:
        signals.append(f"text:{only_changes}")
        return ScanContext(SCAN, "显式要求只审变化 → 增量", signals, tag_list)

    hit = next((t for t in tag_list if t in FULL_TAGS), "")
    if hit:
        signals.append(f"tag:{hit}")
        return ScanContext(FULL, f"大版本标签 {hit} → 全量", signals, tag_list)

    hit = _has_pattern(body, FULL_TEXT_PATTERNS)
    if hit:
        signals.append(f"text:{hit}")
        return ScanContext(FULL, f"大版本文本信号（{hit}）→ 全量", signals, tag_list)

    signals.append("default")
    return ScanContext(
        SCAN, "非大版本（日常开发）→ 只审变化，不触发全量构建/测试",
        signals, tag_list)


def scan_commands(ctx: ScanContext) -> Dict[str, str]:
    """按范围档位给出构建/测试执行口径（供评审/测试角色遵循）。

    Returns:
        ``{"build": …, "test": …}`` —— ``scan`` 档位明确**不跑全量**。
    """
    if ctx.is_full:
        return {
            "build": "全量构建（大版本专用）",
            "test": "全量测试（大版本专用）",
        }
    return {
        "build": "增量构建：仅变更包/目标，不做全仓库全量",
        "test": "增量测试：仅变更相关用例 + 受影响面，不跑全量回归",
    }
