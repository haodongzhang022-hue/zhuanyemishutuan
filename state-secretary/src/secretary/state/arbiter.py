"""M9+ 版本化冲突仲裁与审核模块（ConflictArbiter）

设计目标：弥补纯"锁/版本校验"在语义冲突场景的不足。

核心场景：
  - Agent A 基于版本 N 读取 StateCard，提交修改 → 版本变为 N+1
  - Agent B 基于版本 N 读取 → 尝试以 base_version=N 提交修改
  - 纯版本校验只能抛冲突拒绝 B（丢失 B 的工作成果）
  - 即使 B 重读后以新版本写入，仍可能基于旧理解覆盖 A 的修改

本模块提供 **"版本化保存 + 审核仲裁"** 机制：
  1. 检测到冲突时，保留双方修改（不静默拒绝也不直接覆盖）
  2. 字段级 Diff 分析：区分"修改了不同位置"和"修改了同一位置"
  3. 非重叠修改 → 自动合并（无损合并，双方变更都保留）
  4. 重叠修改 → 生成 ReviewTicket，保存双方版本，标记待审核
  5. 审核环节：审阅者决定保留 版本A / 版本B / 执行合并

完全独立解耦：不依赖 StateManager/OwnershipGuard 之外模块。
输入输出用 dict（JSON 兼容），可在任何项目复用。
"""
from __future__ import annotations

import json
import os
import re
import time
from dataclasses import dataclass, field as dc_field
from typing import Dict, List, Optional, Set, Union

from .models import StateCard, TaskRecord
from .manager import StateManager

# 标识符（session_id / ticket_id）白名单：仅允许字母/数字/下划线/短横线，防路径穿越
_IDENTIFIER_PATTERN = re.compile(r'^[A-Za-z0-9_-]+$')

# TaskRecord 中参与冲突仲裁的"语义字段"（排除 meta/时间戳/自动编号）
_SEMANTIC_FIELDS = [
    "title", "state", "task_type", "fold", "objective",
    "cur_dir", "files_touched", "depends_on", "tools_called",
    "summary", "content_ref",
]


# ============================================================================
# 一、字段级状态差异分析
# ============================================================================

@dataclass
class FieldChange:
    """一个字段级变更：在哪个任务的哪个字段，从什么值变成什么值"""
    task_id: str
    field: str
    old_value: object = None
    new_value: object = None

    def to_dict(self) -> dict:
        return {
            "task_id": self.task_id,
            "field": self.field,
            "old_value": self.old_value,
            "new_value": self.new_value,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "FieldChange":
        return cls(
            task_id=data.get("task_id", ""),
            field=data.get("field", ""),
            old_value=data.get("old_value"),
            new_value=data.get("new_value"),
        )


@dataclass
class StateDiff:
    """两个 StateCard 版本之间的字段级差异"""
    from_version: int = 0
    to_version: int = 0
    task_added: List[dict] = dc_field(default_factory=list)
    task_removed: List[str] = dc_field(default_factory=list)
    task_changes: List[FieldChange] = dc_field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "from_version": self.from_version,
            "to_version": self.to_version,
            "task_added": self.task_added,
            "task_removed": self.task_removed,
            "task_changes": [c.to_dict() for c in self.task_changes],
        }

    @classmethod
    def from_dict(cls, data: dict) -> "StateDiff":
        d = cls(
            from_version=data.get("from_version", 0),
            to_version=data.get("to_version", 0),
            task_removed=list(data.get("task_removed", [])),
        )
        d.task_added = list(data.get("task_added", []))
        d.task_changes = [FieldChange.from_dict(c)
                          for c in data.get("task_changes", [])]
        return d

    @property
    def is_empty(self) -> bool:
        return (not self.task_added and not self.task_removed
                and not self.task_changes)

    def task_ids_touched(self) -> Set[str]:
        """本差异触及的所有任务ID"""
        ids = set()
        for a in self.task_added:
            ids.add(a.get("task_id", ""))
        ids.update(self.task_removed)
        for c in self.task_changes:
            ids.add(c.task_id)
        return ids


def diff_state(base: Union[StateCard, dict],
               target: Union[StateCard, dict]) -> StateDiff:
    """计算从 base 到 target 的状态差异（字段级）。

    输入可以是 StateCard 对象或 dict。忽略 meta/时间戳/版本等字段。
    """
    if isinstance(base, dict):
        base = StateCard.from_dict(base)
    if isinstance(target, dict):
        target = StateCard.from_dict(target)

    d = StateDiff(from_version=base.version, to_version=target.version)

    base_tasks = {t.task_id: t for t in base.tasks}
    target_tasks = {t.task_id: t for t in target.tasks}

    for tid, t in target_tasks.items():
        if tid not in base_tasks:
            d.task_added.append(t.to_dict())

    for tid in base_tasks:
        if tid not in target_tasks:
            d.task_removed.append(tid)

    for tid in base_tasks.keys() & target_tasks.keys():
        bt = base_tasks[tid]
        tt = target_tasks[tid]
        for f in _SEMANTIC_FIELDS:
            bv = getattr(bt, f, None)
            tv = getattr(tt, f, None)
            if bv != tv:
                d.task_changes.append(FieldChange(
                    task_id=tid, field=f, old_value=bv, new_value=tv,
                ))

    return d


def diffs_overlap(diff_a: StateDiff, diff_b: StateDiff) -> List[dict]:
    """检测两个 diff 是否触及相同位置（任务级×字段级）。

    返回重叠项列表（空 = 无重叠，可安全自动合并）。
    """
    overlaps: List[dict] = []
    a_added = {t.get("task_id", "") for t in diff_a.task_added}
    b_added = {t.get("task_id", "") for t in diff_b.task_added}
    a_removed = set(diff_a.task_removed)
    b_removed = set(diff_b.task_removed)

    # 1. 双方新增同一 ID 任务
    for tid in a_added & b_added:
        overlaps.append({
            "task_id": tid, "field": "*",
            "diff_a_value": "added", "diff_b_value": "added",
            "change_kind": "same_task_new_added",
        })

    # 2. 一方新增另一方删除
    for tid in (a_added & b_removed) | (b_added & a_removed):
        overlaps.append({
            "task_id": tid, "field": "*",
            "diff_a_value": "added" if tid in a_added else "removed",
            "diff_b_value": "removed" if tid in a_removed else "added",
            "change_kind": "added_removed_same_task",
        })

    # 3. 双方删除同一任务
    for tid in a_removed & b_removed:
        overlaps.append({
            "task_id": tid, "field": "*",
            "diff_a_value": "removed", "diff_b_value": "removed",
            "change_kind": "both_removed_same_task",
        })

    # 4. 一方修改某任务的字段，另一方删除该同一任务（含对称情形）。
    #    删除会整体抹去对方对该任务的字段修改 → 语义冲突，禁止自动合并，转审核。
    a_changed_ids = {c.task_id for c in diff_a.task_changes}
    b_changed_ids = {c.task_id for c in diff_b.task_changes}
    for tid in (a_changed_ids & b_removed) | (b_changed_ids & a_removed):
        a_is_changer = tid in a_changed_ids
        overlaps.append({
            "task_id": tid, "field": "*",
            "diff_a_value": "changed" if a_is_changer else "removed",
            "diff_b_value": "removed" if a_is_changer else "changed",
            "change_kind": "changed_removed_same_task",
        })

    # 5. 同一任务上修改同一字段
    a_changes: Dict[str, Dict[str, FieldChange]] = {}
    b_changes: Dict[str, Dict[str, FieldChange]] = {}
    for c in diff_a.task_changes:
        a_changes.setdefault(c.task_id, {})[c.field] = c
    for c in diff_b.task_changes:
        b_changes.setdefault(c.task_id, {})[c.field] = c

    for tid in a_changes.keys() & b_changes.keys():
        for field in a_changes[tid].keys() & b_changes[tid].keys():
            overlaps.append({
                "task_id": tid, "field": field,
                "diff_a_value": a_changes[tid][field].new_value,
                "diff_b_value": b_changes[tid][field].new_value,
                "change_kind": "same_field_conflict",
            })

    return overlaps


# ============================================================================
# 二、审核票据 ReviewTicket
# ============================================================================

class ConflictResolution:
    """审核决策常量"""
    KEEP_A = "keep_a"
    KEEP_B = "keep_b"
    MERGE = "merge"
    REJECT = "reject"


class TicketStatus:
    """票据生命周期"""
    PENDING = "pending"
    UNDER_REVIEW = "under_review"
    RESOLVED = "resolved"
    CANCELLED = "cancelled"


@dataclass
class ReviewTicket:
    """
    一次冲突审核的完整票据。

    保存冲突发生时双方版本的完整 StateCard 快照，
    供审阅者（人类/AI reviewer）在"保留版本A"和"保留版本B"之间做出决策。
    """
    session_id: str
    version_a: int                        # 已提交方 A 的版本号
    version_b: int                        # 冲突方 B 的版本号
    card_a: dict                          # A 提交后的 StateCard 快照
    card_b: dict                          # B 尝试提交的 StateCard 快照
    base_card: dict                       # 冲突前的公共 StateCard（如可得）
    arbiter: str = ""                     # 发起方/秘书名
    conflict_reason: str = ""             # 触发原因描述
    overlaps: List[dict] = dc_field(default_factory=list)
    status: str = "pending"
    resolution: str = ""                  # keep_a | keep_b | merge | reject
    resolver: str = ""                    # 审核执行者
    reason: str = ""                      # 审核决策理由
    resolution_note: str = ""             # 处理详情
    created_at: str = ""
    updated_at: str = ""
    auto_merge_blocked: bool = False      # True=禁止自动合并(无base无法精确分析时)
    id: Optional[str] = None

    def __post_init__(self):
        if not _IDENTIFIER_PATTERN.match(self.session_id):
            raise ValueError(
                f"非法 session_id: {self.session_id!r}。仅允许字母/数字/下划线/短横线 "
                f"([A-Za-z0-9_-])，防止路径穿越。"
            )
        if self.id and not _IDENTIFIER_PATTERN.match(self.id):
            raise ValueError(
                f"非法 ticket_id: {self.id!r}。仅允许字母/数字/下划线/短横线 "
                f"([A-Za-z0-9_-])，防止路径穿越。"
            )
        if not self.created_at:
            self.created_at = time.strftime("%Y-%m-%d %H:%M:%S")
        if not self.updated_at:
            self.updated_at = self.created_at
        if not self.id:
            ts = int(time.time() * 1000)
            self.id = f"ARB-{self.session_id}-{ts}"

    @property
    def is_auto_mergeable(self) -> bool:
        """是否可自动合并（无重叠项且未被显式阻止）"""
        return len(self.overlaps) == 0 and not self.auto_merge_blocked

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "session_id": self.session_id,
            "version_a": self.version_a,
            "version_b": self.version_b,
            "card_a": self.card_a,
            "card_b": self.card_b,
            "base_card": self.base_card,
            "arbiter": self.arbiter,
            "conflict_reason": self.conflict_reason,
            "overlaps": self.overlaps,
            "status": self.status,
            "resolution": self.resolution,
            "resolver": self.resolver,
            "reason": self.reason,
            "resolution_note": self.resolution_note,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "auto_merge_blocked": self.auto_merge_blocked,
            "is_auto_mergeable": self.is_auto_mergeable,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "ReviewTicket":
        return cls(
            session_id=data.get("session_id", ""),
            version_a=data.get("version_a", 0),
            version_b=data.get("version_b", 0),
            card_a=data.get("card_a", {}),
            card_b=data.get("card_b", {}),
            base_card=data.get("base_card", {}),
            arbiter=data.get("arbiter", ""),
            conflict_reason=data.get("conflict_reason", ""),
            overlaps=data.get("overlaps", []),
            status=data.get("status", "pending"),
            resolution=data.get("resolution", ""),
            resolver=data.get("resolver", ""),
            reason=data.get("reason", ""),
            resolution_note=data.get("resolution_note", ""),
            created_at=data.get("created_at", ""),
            updated_at=data.get("updated_at", ""),
            auto_merge_blocked=data.get("auto_merge_blocked", False),
            id=data.get("id"),
        )

    def summarize(self) -> str:
        """生成审核者可读的冲突摘要"""
        lines = [
            f"📋 冲突票据: {self.id}",
            f"  会话: {self.session_id} (A_v={self.version_a}, B_v={self.version_b})",
            f"  状态: {self.status}",
            f"  发起方: {self.arbiter or '(未指定)'}",
            f"  原因: {self.conflict_reason or '版本冲突'}",
        ]
        # 统计双方差异
        da = diff_state(self.base_card, self.card_a) if self.base_card else StateDiff()
        db = diff_state(self.base_card, self.card_b) if self.base_card else StateDiff()
        if da.task_added:
            lines.append(f"  A 新增任务: {len(da.task_added)}")
        if da.task_changes:
            lines.append(f"  A 修改字段: {len(da.task_changes)} 处")
        if db.task_added:
            lines.append(f"  B 新增任务: {len(db.task_added)}")
        if db.task_changes:
            lines.append(f"  B 修改字段: {len(db.task_changes)} 处")
        if self.overlaps:
            lines.append(f"  ⚠️ 重叠冲突: {len(self.overlaps)} 处")
            for o in self.overlaps[:10]:
                lines.append(
                    f"    - {o.get('task_id', '?')}.{o.get('field', '?')}: "
                    f"A={o.get('diff_a_value')} vs B={o.get('diff_b_value')} "
                    f"[{o.get('change_kind', '?')}]"
                )
        else:
            lines.append("  ✅ 无重叠（可自动合并）")
        return "\n".join(lines)


# ============================================================================
# 三、冲突仲裁引擎
# ============================================================================

class ConflictArbiter:
    """
    版本化冲突仲裁引擎。

    核心能力：
    1. capture_conflict: 接收双方版本，分析重叠，保存票据
    2. auto_merge: 无重叠时自动合并双方修改
    3. review: 审核入口，决定保留哪个版本
    4. list_pending / get_ticket: 票据查询
    5. 持久化到 data/arbitration/ 目录

    完全独立于 StateManager/OwnershipGuard，可用任意 StateCard 快照驱动。
    """

    TICKET_SUFFIX = ".ticket.json"

    def __init__(self, data_dir: Optional[str] = None,
                 state_manager: Optional[StateManager] = None):
        self.data_dir = data_dir or os.path.join(os.getcwd(), "data", "arbitration")
        os.makedirs(self.data_dir, exist_ok=True)
        self.sm = state_manager

    # ---- 核心入口：捕获冲突 ----

    def capture_conflict(self,
                         session_id: str,
                         base_card: Union[StateCard, dict],
                         conflicting_card: Union[StateCard, dict],
                         current_card: Union[StateCard, dict],
                         arbiter_name: str = "",
                         conflict_reason: str = "") -> ReviewTicket:
        """
        捕获一次版本化冲突。

        参数：
        - session_id: 会话ID
        - base_card: 冲突前双方共同基于的版本（如无历史快照，传当前卡近似）
        - conflicting_card: 冲突方 B 尝试产生的版本
        - current_card: 已提交成功的版本 A（通常为当前最新版本）

        流程：
        1. 计算 base→A 和 base→B 的字段级差异
        2. 检测重叠
        3. 无重叠 → 自动合并并标记 resolved
        4. 有重叠 → 保存票据，标记 pending，等待审核
        """
        # 统一转为 dict 存储（JSON 兼容，完全解耦）
        if isinstance(base_card, StateCard):
            base_dict = base_card.to_dict()
        else:
            base_dict = base_card
        if isinstance(conflicting_card, StateCard):
            conflicting_dict = conflicting_card.to_dict()
        else:
            conflicting_dict = conflicting_card
        if isinstance(current_card, StateCard):
            current_dict = current_card.to_dict()
        else:
            current_dict = current_card

        # 计算双方 diff
        diff_a = diff_state(base_dict, current_dict)
        diff_b = diff_state(base_dict, conflicting_dict)

        # 检测重叠
        overlaps = diffs_overlap(diff_a, diff_b)

        ticket = ReviewTicket(
            session_id=session_id,
            version_a=diff_a.to_version,
            version_b=diff_b.to_version,
            card_a=current_dict,
            card_b=conflicting_dict,
            base_card=base_dict,
            arbiter=arbiter_name,
            conflict_reason=conflict_reason or "版本冲突(基于旧版本提交)",
            overlaps=overlaps,
        )

        if not overlaps:
            # 无重叠 → 自动合并
            merged = self._auto_merge(base_dict, current_dict, conflicting_dict)
            # 合并后复核：确认 A、B 双方的非重叠变更均被保留，才允许"自动合并/宣称保留"；
            # 若复核不通过（异常兜底），降级为待审核，绝不静默丢失任一方修改。
            if merged is not None and self._verify_merge_retains(
                    base_dict, current_dict, conflicting_dict, merged):
                self._apply_merged(session_id, merged)
                ticket.status = "resolved"
                ticket.resolution = ConflictResolution.MERGE
                ticket.resolver = "auto-arbiter"
                ticket.resolution_note = "自动合并成功：A与B的修改不重叠，复核确认双方变更均已保留。"
                ticket.updated_at = time.strftime("%Y-%m-%d %H:%M:%S")
            elif merged is not None:
                # 复核未通过 → 保守转审核，避免"宣称保留实则丢失"
                ticket.status = "pending"
                ticket.conflict_reason = (
                    "自动合并复核未通过：存在边界重叠，转为人工/AI审核裁决。"
                )
                ticket.resolution = ""
                ticket.overlaps = ticket.overlaps or [{
                    "task_id": "*", "field": "*",
                    "diff_a_value": "auto", "diff_b_value": "auto",
                    "change_kind": "auto_merge_verify_failed",
                }]
                ticket.updated_at = time.strftime("%Y-%m-%d %H:%M:%S")

        # 持久化票据
        self._save_ticket(ticket)
        return ticket

    # ---- 审核入口 ----

    def review(self, ticket_id: str, decision: str,
               resolver: str = "", reason: str = "") -> ReviewTicket:
        """
        审核者决策入口。
        decision:
          keep_a = 保留版本 A（已提交方）
          keep_b = 保留版本 B（冲突方）
          merge  = 尝试自动合并（仅无重叠时）
          reject = 拒绝冲突方(B)修改，保留已提交版本
        """
        ticket = self.get_ticket(ticket_id)
        if ticket is None:
            raise ValueError(f"票据不存在: {ticket_id}")
        if ticket.status not in ("pending", "under_review"):
            raise ValueError(f"票据状态不是待审核: {ticket.status} (当前={ticket.status})")

        # 版本回退防护：若冲突发生后会话又产生过更新（当前版本已高于票据快照），
        # 直接覆写旧快照会回退版本并静默丢失中间变更。此时拒绝静默落盘，
        # 提示重新基于最新状态捕获冲突（生成新票据）后再审核。
        if decision in (ConflictResolution.KEEP_A,
                        ConflictResolution.KEEP_B,
                        ConflictResolution.REJECT,
                        ConflictResolution.MERGE):
            self._guard_stale_review(ticket)

        if decision == ConflictResolution.KEEP_A:
            self._apply_card(session_id=ticket.session_id,
                             card=ticket.card_a)
            ticket.resolution = ConflictResolution.KEEP_A
            ticket.resolution_note = "审核决策：保留版本 A（已提交方的修改）"
        elif decision == ConflictResolution.KEEP_B:
            self._apply_card(session_id=ticket.session_id,
                             card=ticket.card_b)
            ticket.resolution = ConflictResolution.KEEP_B
            ticket.resolution_note = "审核决策：保留版本 B（冲突方的修改）"
        elif decision == ConflictResolution.MERGE:
            # 门禁：仅无重叠的票据才可执行自动合并。
            # 若票据存在重叠(overlaps 非空)，_auto_merge 会对冲突字段直接以 B 覆盖 A，
            # 造成"审核决策 merge"静默丢弃 A 的修改，违背"绝不静默丢一方修改"的约定。
            if not ticket.is_auto_mergeable:
                raise ValueError(
                    "无法自动合并：票据存在重叠冲突("
                    f"{len(ticket.overlaps)} 处)，直接 merge 会静默丢弃一方修改。"
                    "请使用 keep_a / keep_b 显式选择保留哪一方，"
                    "或先处理冲突后再重新捕获。"
                )
            merged = self._auto_merge(ticket.base_card,
                                      ticket.card_a,
                                      ticket.card_b)
            if merged is not None and self._verify_merge_retains(
                    ticket.base_card, ticket.card_a, ticket.card_b, merged):
                self._apply_card(session_id=ticket.session_id, card=merged)
                ticket.resolution = ConflictResolution.MERGE
                ticket.resolution_note = "审核决策：执行自动合并，复核确认双方变更均已保留"
            else:
                raise ValueError(
                    "自动合并复核未通过或无法合并，请使用 keep_a / keep_b 决策。"
                )
        elif decision == ConflictResolution.REJECT:
            # 拒绝 B，恢复 A
            self._apply_card(session_id=ticket.session_id,
                             card=ticket.card_a)
            ticket.resolution = ConflictResolution.REJECT
            ticket.resolution_note = "审核决策：拒绝冲突方(B)的修改"
        else:
            raise ValueError(f"非法决策: {decision}")

        ticket.status = "resolved"
        ticket.resolver = resolver
        ticket.reason = reason
        ticket.updated_at = time.strftime("%Y-%m-%d %H:%M:%S")
        self._save_ticket(ticket)
        return ticket

    # ---- 查询 ----

    def get_ticket(self, ticket_id: str) -> Optional[ReviewTicket]:
        path = self._ticket_path(ticket_id)
        if not os.path.exists(path):
            return None
        try:
            with open(path, "r", encoding="utf-8") as f:
                return ReviewTicket.from_dict(json.load(f))
        except Exception:
            return None

    def list_tickets(self, session_id: Optional[str] = None,
                     status: Optional[str] = None) -> List[ReviewTicket]:
        """列出票据"""
        tickets = []
        if not os.path.exists(self.data_dir):
            return tickets
        for f in os.listdir(self.data_dir):
            if not f.endswith(self.TICKET_SUFFIX):
                continue
            path = os.path.join(self.data_dir, f)
            try:
                with open(path, "r", encoding="utf-8") as fh:
                    data = json.load(fh)
                t = ReviewTicket.from_dict(data)
                if session_id and t.session_id != session_id:
                    continue
                if status and t.status != status:
                    continue
                tickets.append(t)
            except Exception:
                continue
        return tickets

    def list_pending(self, session_id: Optional[str] = None) -> List[ReviewTicket]:
        """列出待审核票据"""
        return self.list_tickets(session_id=session_id,
                                 status=TicketStatus.PENDING)

    # ---- 内部工具 ----

    def _auto_merge(self, base: dict, card_a: dict,
                    card_b: dict) -> Optional[dict]:
        """自动合并：将 B 的新增/变更叠加到 A 的结果上。

        前提：双方差异不重叠（由 diffs_overlap 保证）。
        返回合并后的 StateCard dict，失败返回 None。
        """
        # 以 A 的版本为基础
        merged = StateCard.from_dict(card_a)
        diff_b = diff_state(base, card_b)

        # 1. B 新增的任务 → 追加（若 A 未添加同 ID 任务）
        existing_ids = {t.task_id for t in merged.tasks}
        for added in diff_b.task_added:
            tid = added.get("task_id", "")
            if tid and tid not in existing_ids:
                try:
                    merged.tasks.append(TaskRecord.from_dict(added))
                except Exception:
                    pass

        # 2. B 删除的任务 → 从合并版删除
        for removed_id in diff_b.task_removed:
            merged.tasks = [t for t in merged.tasks
                            if t.task_id != removed_id]

        # 3. B 修改的字段 → 应用（前提是这些字段不在 overlaps 中）
        for change in diff_b.task_changes:
            task = merged.get_task(change.task_id)
            if task is None:
                continue
            if hasattr(task, change.field):
                setattr(task, change.field, change.new_value)

        merged.version = max(int(card_a.get("version", 0) or 0),
                             int(card_b.get("version", 0) or 0)) + 1
        merged.updated_at = time.strftime("%Y-%m-%d %H:%M:%S")
        return merged.to_dict()

    def _verify_merge_retains(self, base: dict, card_a: dict,
                              card_b: dict, merged: dict) -> bool:
        """复核自动合并结果：确认 A、B 双方的非重叠变更均被保留。

        防止"宣称双方均已保留、实则某方修改被静默丢弃"的边界情况。
        校验维度（相对 base）：
          - A 新增的任务在合并结果中仍存在
          - A 删除的任务在合并结果中仍被删除
          - A 修改的字段在合并结果中保持 A 的新值
        对 B 同样校验一遍。任一不满足即返回 False。
        """
        for diff in (diff_state(base, card_a), diff_state(base, card_b)):
            merged_card = StateCard.from_dict(merged)
            merged_tasks = {t.task_id: t for t in merged_card.tasks}
            base_tasks = {t.task_id: t for t in StateCard.from_dict(base).tasks}
            # 新增任务应仍在
            for added in diff.task_added:
                tid = added.get("task_id", "")
                if tid and tid not in merged_tasks:
                    return False
            # 删除的任务应已被移除
            for removed_id in diff.task_removed:
                if removed_id in merged_tasks:
                    return False
            # 字段修改应保留新值（需任务仍存在）
            for change in diff.task_changes:
                task = merged_tasks.get(change.task_id)
                if task is None:
                    return False
                cur = getattr(task, change.field, None)
                if cur != change.new_value:
                    return False
        return True

    def _apply_merged(self, session_id: str, merged: dict) -> None:
        """将合并结果写回 StateManager"""
        self._apply_card(session_id, merged)

    def _guard_stale_review(self, ticket: ReviewTicket) -> None:
        """版本回退防护：审核落盘前检查当前会话是否已越过票据快照。

        若当前实时状态版本高于票据内快照版本（冲突后又有提交），直接以旧快照
        覆写会回退版本号并静默丢弃中间变更 → 抛错提示重新捕获冲突，不静默落盘。

        无论是否注入了 StateManager，均尝试从磁盘读取当前最新状态进行比对。
        """
        # 优先从 StateManager 读取；无 StateManager 时直接从磁盘读
        live = None
        if self.sm:
            live = self.sm.get_card(ticket.session_id)
        else:
            data_dir = os.path.join(os.getcwd(), "data", "state")
            path = os.path.join(data_dir, f"{ticket.session_id}.json")
            if os.path.exists(path):
                try:
                    with open(path, "r", encoding="utf-8") as f:
                        live = StateCard.from_dict(json.load(f))
                except Exception:
                    pass

        if live is None:
            return
        ticket_top = max(int(ticket.version_a or 0),
                         int(ticket.version_b or 0))
        if int(live.version or 0) > ticket_top:
            raise ValueError(
                f"票据 {ticket.id} 冲突后会话 {ticket.session_id} 已有更新 "
                f"(当前 v{live.version} > 票据快照 v{ticket_top})。"
                "直接落盘会回退版本并丢失中间变更，请基于最新状态重新捕获冲突"
                "（生成新票据）后再审核。"
            )

    def _apply_card(self, session_id: str, card: dict) -> None:
        """将一张卡的状态写入 StateManager"""
        if self.sm:
            sc = StateCard.from_dict(card)
            self.sm.save_card(sc)
        else:
            # 无 StateManager 时直接写文件
            if not _IDENTIFIER_PATTERN.match(session_id):
                raise ValueError(
                    f"非法 session_id: {session_id!r}。仅允许字母/数字/下划线/短横线 "
                    f"([A-Za-z0-9_-])，防止路径穿越。"
                )
            data_dir = os.path.join(os.getcwd(), "data", "state")
            os.makedirs(data_dir, exist_ok=True)
            path = os.path.join(data_dir, f"{session_id}.json")
            with open(path, "w", encoding="utf-8") as f:
                json.dump(card, f, ensure_ascii=False, indent=2)

    # ---- 持久化 ----

    def _ticket_path(self, ticket_id: str) -> str:
        if not _IDENTIFIER_PATTERN.match(ticket_id):
            raise ValueError(
                f"非法 ticket_id: {ticket_id!r}。仅允许字母/数字/下划线/短横线 "
                f"([A-Za-z0-9_-])，防止路径穿越。"
            )
        return os.path.join(self.data_dir, f"{ticket_id}{self.TICKET_SUFFIX}")

    def _save_ticket(self, ticket: ReviewTicket) -> None:
        path = self._ticket_path(ticket.id)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(ticket.to_dict(), f, ensure_ascii=False, indent=2)
