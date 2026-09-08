"""SecretarySession：秘书会话门面（Facade）

将状态管理秘书机制的所有底层组件整合为一个可供 L3 会话秘书直接使用的
统一入口。每个 SecretarySession 实例 = 一个 L3 会话秘书，绑定一个 session_id。

整合组件：
  - StateManager    → 状态卡生命周期管理
  - FoldEngine      → 折叠/编号/检索
  - ContentArchive  → 原文归档（折叠后不丢原文）
  - ContextAssembler→ 三层上下文装配（稳定前缀 + 缓存命中）
  - HandoffBus      → M5 交接卡（跨单元协作）

核心设计原则：
  1. 一会话一秘书：session_id 即对话/Issue/PR 唯一标识，环境隔离。
  2. 自动化优先：任务折叠默认自动，无需手动干预。
  3. 原文不丢：折叠后原文可按 content_ref 精确找回。
  4. 稳定前缀：静态上下文层不随会话长度漂移，缓存命中率高。
"""
from __future__ import annotations

import json
import os
import re
import tempfile
import time
from typing import Dict, List, Optional, Any

from ..state import (
    StateManager, StateCard, TaskRecord,
    TaskState, TaskType,
)
from ..state.fold_engine import FoldEngine
from ..state.content_archive import ContentArchive
from ..context.assembler import ContextAssembler
from ..handoff.handoff import HandoffBus
from ..handoff.models import HandoffCard


class _StateCardProxy:
    """StateCard 的**装配视图代理**：暴露 compact_context() 供 ContextAssembler 层B使用。

    ContextAssembler._build_layer_b 优先调用 state_card.compact_context() 来生成
    "会话态上下文"（展开任务要素 + 折叠任务编号引用）。StateCard 本身是数据模型，
    不耦合 FoldEngine 折叠逻辑；由门面在此注入 FoldEngine 代理，使层B能真正携带
    任务标题/折叠引用，而非只落到"目标/进度/会话"的降级摘要。
    """

    def __init__(self, card: StateCard, fold_engine: FoldEngine):
        self._card = card
        self._fold_engine = fold_engine

    def compact_context(self) -> str:
        return self._fold_engine.compact_context(self._card)


class SecretarySession:
    """会话秘书门面：一个 L3 会话秘书的完整接口。

    用法：
        sec = SecretarySession("issue-42")
        # 新建任务
        sec.add_task("设计状态 Schema")
        # 折叠一段原始上下文并编号
        sec.fold("需求澄清：Q1 四层秘书定义…", meta={"objective": "澄清需求"})
        # 获取当前会话的压缩上下文（供模型驱动下一轮）
        ctx = sec.get_compact_context()
        # 更新任务状态
        sec.mark_task("issue-42-0001", "in_progress")
        # 向 L2 状态管理秘书提交汇总
        summary = sec.summarize_for_upstream()
    """

    def __init__(
        self,
        session_id: str,
        objective: str = "",
        unit_id: str = "",
        role_name: str = "secretary",
        data_dir: Optional[str] = None,
        archive_root: Optional[str] = None,
        static_prefix: Optional[str] = None,
    ):
        """初始化一个会话秘书。

        Args:
            session_id: 会话唯一 ID（Issue/PR/对话编号）
            objective: 会话总体目标
            unit_id: 所属行业单元
            role_name: 秘书角色名（默认 "secretary"）
            data_dir: 会话数据根目录（默认 <cwd>/data）。同一会话的状态卡、
                原文归档、交接卡均收敛在此根之下派生（state/ content/ handoffs/）。
            archive_root: 原文归档根目录（默认 <data_dir>/content）。
            static_prefix: 静态前缀（角色骨架，可传给 ContextAssembler）。
        """
        # 合法性预检
        if not re.match(r'^[A-Za-z0-9_-]+$', session_id):
            raise ValueError(
                f"非法 session_id: {session_id!r}。仅允许 [A-Za-z0-9_-]。")

        self.session_id = session_id
        self.unit_id = unit_id
        self.role_name = role_name
        self.objective = objective
        self.static_prefix = static_prefix

        # 统一数据根：同一会话的状态卡/原文归档/交接卡收敛到同一根之下，
        # 避免"数据根分裂"（状态卡在 A、归档在 B、交接卡在 C）。
        # 默认 <cwd>/data，传入 data_dir 时以之为根。
        self._session_root = data_dir or os.path.join(os.getcwd(), "data")
        state_dir = os.path.join(self._session_root, "state")
        # archive_root 显式给出则使用；否则从数据根派生 content
        self._archive_root = archive_root or os.path.join(
            self._session_root, "content")
        ho_dir = os.path.join(self._session_root, "handoffs")

        # 初始化底层组件
        self.state_manager = StateManager(data_dir=state_dir)
        self.fold_engine = FoldEngine(
            session_id=session_id,
            unit_id=unit_id,
            role_name=role_name,
            archive_root=self._archive_root,
        )
        self.archive = ContentArchive(root_dir=self._archive_root)
        self.context_assembler = ContextAssembler(
            static_prefix_provider=(
                lambda u, r: static_prefix if static_prefix else None
            ) if static_prefix else None,
        )
        self.handoff_bus = HandoffBus(data_dir=ho_dir, state_root=self._session_root)
        # 记录本会话创建/认领过的交接卡 ID，供 delete_session 一并清理。
        # 跨实例归属以 data_dir 下的归属台账（handoffs/.session-<id>.owned）
        # 持久化记录为准，实例内存清单仅为会话内缓存镜像。
        self._owned_handoff_ids: List[str] = []
        # 本会话经交接 auto_state_card 关联的 ho-* StateCard ID
        # （delete_session 一并回收这些状态卡，消除跨根孤儿）
        self._owned_state_card_ids: List[str] = []

        # 确保 StateCard 已创建（get_or_create）
        self._ensure_card()
        # 恢复本会话归属台账（跨实例：上一实例创建的交接卡/ho-* 卡纳入清理）
        self._load_ledger()

    # ------------------------------------------------------------------
    # 内部工具
    # ------------------------------------------------------------------

    def _ensure_card(self) -> StateCard:
        """确保 StateCard 存在，不存在则创建"""
        return self.state_manager.get_or_create(
            session_id=self.session_id,
            objective=self.objective,
            unit_id=self.unit_id,
            role_name=self.role_name,
        )

    def _card(self) -> Optional[StateCard]:
        """获取当前会话的 StateCard"""
        return self.state_manager.get_card(self.session_id)

    @staticmethod
    def _max_content_seq(card: Optional[StateCard]) -> int:
        """取任务卡中已有 content_ref 的最大段号（无则 0）。

        用于续号：{session}-seg{NNN} 中 NNN 的最大值。
        """
        max_seq = 0
        if card is None:
            return max_seq
        for t in card.tasks:
            ref = t.content_ref or ""
            m = re.match(r'^[A-Za-z0-9_-]+-seg(\d+)$', ref)
            if m:
                max_seq = max(max_seq, int(m.group(1)))
        return max_seq

    def _own_state_card(self, state_card_id: str) -> None:
        """登记一张本会话负责回收的 StateCard（去重保留）。"""
        if state_card_id and state_card_id not in self._owned_state_card_ids:
            self._owned_state_card_ids.append(state_card_id)

    def _ledger_path(self) -> str:
        """本会话交接卡归属台账路径。

        存放于本会话 handoffs/ 目录下（<root>/handoffs/.session-<id>.owned）。
        持久化记录"哪些交接卡 + 关联 ho-* 状态卡属于本会话"，供跨实例
        重开后 delete_session 仍能一次回收；不扫全部卡文件、不误认领
        其他会话的交接卡（多会话同数据根时互不越权）。

        注意：文件后缀刻意**不带 .json**——handoff 目录的 *.json 会被
        list_pending() 当作交接卡解析，台账不应被误读为 HandoffCard。
        """
        return os.path.join(
            self.handoff_bus.data_dir,
            f".session-{self.session_id}.owned")

    def _record_ownership(self, handoff_id: str,
                          state_card_id: Optional[str]) -> None:
        """登记一张交接卡（及其关联 ho-* 状态卡）属于本会话，落盘台账。"""
        if handoff_id and handoff_id not in self._owned_handoff_ids:
            self._owned_handoff_ids.append(handoff_id)
        if state_card_id:
            self._own_state_card(state_card_id)
        self._flush_ledger()

    def _flush_ledger(self) -> None:
        """把本会话交接卡归属台账写入磁盘（幂等、无锁单写者）。"""
        ledger = {
            "session_id": self.session_id,
            "handoff_ids": list(self._owned_handoff_ids),
            "state_card_ids": list(self._owned_state_card_ids),
            "updated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        }
        path = self._ledger_path()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        fd, tmp_path = tempfile.mkstemp(
            dir=os.path.dirname(path), suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(ledger, f, ensure_ascii=False, indent=2)
            os.replace(tmp_path, path)
        except Exception:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise

    def _load_ledger(self) -> None:
        """会话实例启动时读取本会话归属台账，恢复跨实例所有权。"""
        path = self._ledger_path()
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, ValueError):
            return
        for hid in data.get("handoff_ids", []) or []:
            if hid not in self._owned_handoff_ids:
                self._owned_handoff_ids.append(hid)
        for sid in data.get("state_card_ids", []) or []:
            self._own_state_card(sid)

    def _owned_handoffs(self) -> List[str]:
        """汇总本会话需要清理的交接卡 ID（内存清单 ∪ 台账权威清单）。"""
        self._load_ledger()
        return list(self._owned_handoff_ids)

    # ------------------------------------------------------------------
    # 会话生命周期
    # ------------------------------------------------------------------

    def get_card(self) -> Optional[StateCard]:
        """获取当前 StateCard"""
        return self._card()

    def delete_session(self) -> bool:
        """清理本会话的全部数据（状态卡 + 原文归档 + 交接卡 + 关联 ho-* 状态卡）。

        生命周期语义完整：一次 delete 即回收该会话秘书占用的全部存储，
        不残留 .lock / 空目录 / 跨实例交接卡 / 交接自动关联的孤儿状态卡。
        返回"本会话状态卡是否存在"（卡已不存在时返回 False，
        即使归档/交接等伴生数据已全部清理）。
        """
        # 1) 清理本会话"创建/认领过"的交接卡——含上一实例创建、本实例据台账
        #    恢复认领的卡（跨实例不留残余）
        for hid in self._owned_handoffs():
            try:
                self.handoff_bus.delete(hid)
            except Exception:
                pass
        self._owned_handoff_ids.clear()

        # 2) 清理交接卡自动关联的 ho-* StateCard（此前落 <cwd> 的孤儿卡根因；
        #    现随 data_dir/state 收敛，随卡删除连带 .lock 一并回收）
        for sid in list(self._owned_state_card_ids):
            try:
                self.state_manager.delete_card(sid)
            except Exception:
                pass
        self._owned_state_card_ids.clear()

        # 3) 清理本会话状态卡（连带 .lock / .owner.json）
        ok = self.state_manager.delete_card(self.session_id)

        # 4) 删除本会话归属台账（台账本身也是本会话数据的一部分）
        try:
            ledger_path = self._ledger_path()
            if os.path.exists(ledger_path):
                os.remove(ledger_path)
        except OSError:
            pass

        # 5) 清理本会话的原文归档（并回收 <content>/<session>/ 空子目录）
        try:
            self.archive.delete_session(self.session_id)
        except Exception:
            pass
        return ok

    # ------------------------------------------------------------------
    # 任务管理
    # ------------------------------------------------------------------

    def add_task(
        self,
        title: str,
        objective: str = "",
        task_type: str = "normal",
        depends_on: Optional[List[str]] = None,
        files: Optional[List[str]] = None,
        summary: str = "",
        **kwargs,
    ) -> Optional[TaskRecord]:
        """添加一个新任务到本会话。

        - normal（默认）→ 折叠态
        - deliverable → 展开态（末端交付任务）
        """
        return self.state_manager.add_task(
            session_id=self.session_id,
            title=title,
            objective=objective,
            task_type=task_type,
            depends_on=depends_on,
            files=files,
            summary=summary,
            **kwargs,
        )

    def mark_task(self, task_id: str, state: str) -> bool:
        """更新任务状态（todo / in_progress / done / blocked / stopped）"""
        return self.state_manager.update_task_state(
            self.session_id, task_id, state)

    def update_task(self, task_id: str, **fields) -> bool:
        """更新任务的任意字段（title/summary/objective 等）"""
        return self.state_manager.update_task_fields(
            self.session_id, task_id, **fields)

    def get_task(self, task_id: str) -> Optional[TaskRecord]:
        """按任务编号取任务"""
        card = self._card()
        return card.get_task(task_id) if card else None

    def list_tasks(self) -> List[TaskRecord]:
        """列出所有任务"""
        card = self._card()
        return list(card.tasks) if card else []

    def get_active_tasks(self) -> List[TaskRecord]:
        """获取当前 in_progress 的任务"""
        card = self._card()
        if not card:
            return []
        return [t for t in card.tasks
                if t.state == TaskState.IN_PROGRESS.value]

    def get_open_tasks(self) -> List[TaskRecord]:
        """获取所有未完成的任务（todo + in_progress + blocked）"""
        card = self._card()
        if not card:
            return []
        return [t for t in card.tasks
                if t.state in (TaskState.TODO.value,
                               TaskState.IN_PROGRESS.value,
                               TaskState.BLOCKED.value)]

    # ------------------------------------------------------------------
    # 折叠 / 编号 / 检索
    # ------------------------------------------------------------------

    def fold(
        self,
        content: str,
        meta: Optional[dict] = None,
    ) -> StateCard:
        """折叠一段原始上下文，编号归档。

        将原文按段落自动拆分，每段成为一个折叠任务并编号。
        原文全文归档到 ContentArchive，content_ref 作为索引可检索找回。

        Args:
            content: 要折叠的原始上下文全文
            meta: 附加元数据（objective/cur_dir/files 等）

        Returns:
            更新后的 StateCard（当前会话的完整状态卡）
        """
        meta = meta or {}
        card = self._ensure_card()

        # 若提供了 objective 但卡没有 objective，更新
        if meta.get("objective") and not card.objective:
            card.objective = meta["objective"]
            self.state_manager.save_card(card)

        # 调用 fold_engine 分段
        segments = self.fold_engine._split_segments(content)

        # 续号：基于既有任务卡最大 content_ref 段号 + 1，保证同一会话多次
        # 折叠编号连续、不覆盖既有归档文件，content_ref 与任务一一对应。
        start_seq = self._max_content_seq(card) + 1

        # 归档原文全文（从 start_seq 续号）
        refs = self.archive.store_segments(
            self.session_id, segments, start_seq=start_seq)
        # 去除空段后重新对齐
        segments = [s.strip() for s in segments if s.strip()]

        for i, seg in enumerate(segments):
            # 最后一段作为交付任务展开，其余折叠
            is_deliverable = (i == len(segments) - 1)
            task_type = (TaskType.DELIVERABLE.value if is_deliverable
                         else TaskType.NORMAL.value)

            # 确保 content_ref 有值
            content_ref = refs[i] if i < len(refs) else None

            # 添加到 StateManager 管理的现有 card
            task = card.add_task(
                title=seg.split("\n")[0][:80],
                objective=meta.get("objective", self.objective),
                summary=seg[:200] + ("..." if len(seg) > 200 else ""),
                task_type=task_type,
                content_ref=content_ref,
                files_touched=meta.get("files", []),
                tools_called=meta.get("tools", []),
                cur_dir=meta.get("cur_dir", ""),
            )

        self.state_manager.save_card(card)
        return card

    def get_compact_context(self) -> str:
        """获取当前会话的压缩上下文（驱动下一轮对话的 prompt 片段）。

        只携带展开任务要素 + 折叠任务编号引用，不含原文全文。
        """
        card = self._card()
        if card is None:
            return f"目标: {self.objective}" if self.objective else ""
        return self.fold_engine.compact_context(card)

    def get_navigation(self) -> str:
        """获取当前会话的任务导航视图（人类可读）"""
        card = self._card()
        if card is None:
            return f"(空会话 {self.session_id})"
        return self.fold_engine.render_navigation(card)

    def retrieve_original(self, content_ref: Optional[str]) -> Optional[str]:
        """按 content_ref 精确检索并取回原文全文。

        参数非法（None/空串/含非法字符）或归档不存在时返回 None；
        作为公共 API 不抛裸 TypeError/ValueError。
        """
        if not content_ref:
            return None
        try:
            return self.archive.retrieve_content(
                self.session_id, content_ref)
        except Exception:
            return None

    def search_tasks(self, keyword: str,
                     include_original: bool = False) -> List[dict]:
        """在本会话的所有任务中搜索关键词"""
        card = self._card()
        if card is None:
            return []
        return self.fold_engine.search_tasks(
            [card], keyword, include_original=include_original)

    def apply_fold_policy(self, current_task_id: str = None) -> Optional[StateCard]:
        """应用自动折叠策略：交付任务恒展开、当前执行任务展开、其余折叠。"""
        return self.state_manager.apply_fold_policy(
            self.session_id, current_task_id=current_task_id)

    # ------------------------------------------------------------------
    # 三层上下文装配（稳定前缀 + 缓存命中保障）
    # ------------------------------------------------------------------

    def assemble_context(
        self,
        user_text: str = "",
        static_prefix: Optional[str] = None,
    ) -> Dict[str, Any]:
        """对一次调用执行三层上下文装配。

        Returns dict 含 layer_a/layer_b/layer_c/full_context/cache_key 等。
        """
        card = self._card()
        # 传装配视图代理：让层B能经 FoldEngine 生成携带任务标题/折叠引用的
        # 会话态上下文（StateCard 本身无 compact_context 方法）。
        state_card = _StateCardProxy(card, self.fold_engine) if card else None
        result = self.context_assembler.assemble(
            session_id=self.session_id,
            role_name=self.role_name,
            unit_id=self.unit_id,
            user_text=user_text,
            static_prefix=static_prefix or self.static_prefix,
            state_card=state_card,
            objective=self.objective,
        )
        return result

    # ------------------------------------------------------------------
    # 层级向上提交
    # ------------------------------------------------------------------

    def summarize_for_upstream(self) -> dict:
        """生成向上级 L2 状态管理秘书提交的汇总状态。

        只携带结构化汇总字段（进度/决策/开放项），不含原始全文。
        """
        card = self._card()
        if card is None:
            return {
                "session_id": self.session_id,
                "objective": self.objective,
                "task_count": 0,
                "tasks": [],
            }
        return card.summarize_for_upstream()

    def set_upstream(self, upstream_session: str) -> bool:
        """设置上级秘书引用"""
        return self.state_manager.set_upstream(
            self.session_id, upstream_session)

    def add_downstream(self, downstream_session: str) -> bool:
        """添加下级秘书引用"""
        return self.state_manager.add_downstream(
            self.session_id, downstream_session)

    # ------------------------------------------------------------------
    # 交接（M5）
    # ------------------------------------------------------------------

    def create_handoff(
        self,
        source_unit: str,
        target_unit: str,
        intent: str,
        context: str = "",
    ) -> HandoffCard:
        """创建一张交接卡（跨单元协作），自动关联本会话 StateCard。"""
        hc = self.handoff_bus.create_handoff(
            source_unit=source_unit,
            target_unit=target_unit,
            intent=intent,
            context=context,
            auto_state_card=True,
        )
        if hc and hc.id:
            # 认领本交接卡 + 其关联 ho-* StateCard（落盘归属台账，跨实例可清）
            self._record_ownership(hc.id, hc.state_card_id)
        return hc

    # ------------------------------------------------------------------
    # 进度统计
    # ------------------------------------------------------------------

    def progress_stats(self) -> dict:
        """获取本会话的进度统计。"""
        card = self._card()
        if card is None:
            return {"session_id": self.session_id, "task_count": 0}
        summary = card.summarize_for_upstream()
        return {
            "session_id": self.session_id,
            "task_count": summary["task_count"],
            "todo": summary["todo_count"],
            "in_progress": summary["in_progress_count"],
            "done": summary["done_count"],
            "blocked": summary["blocked_count"],
            "stopped": summary["stopped_count"],
            "objective": card.objective or self.objective,
        }
