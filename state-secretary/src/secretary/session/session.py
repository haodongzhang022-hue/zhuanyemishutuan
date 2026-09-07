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

import os
import re
from typing import Dict, List, Optional, Any

from ..state import (
    StateManager, StateCard, TaskRecord,
    TaskState, TaskType, FoldState,
)
from ..state.fold_engine import FoldEngine
from ..state.content_archive import ContentArchive
from ..context.assembler import ContextAssembler
from ..handoff.handoff import HandoffBus
from ..handoff.models import HandoffCard


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
            data_dir: 状态数据目录（默认 <cwd>/data/state）
            archive_root: 原文归档根目录（默认 <cwd>/data/state/content）
            static_prefix: 静态前缀（角色骨架，可传给 ContextAssembler）
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

        # 初始化底层组件
        self.state_manager = StateManager(data_dir=data_dir)
        self.fold_engine = FoldEngine(
            session_id=session_id,
            unit_id=unit_id,
            role_name=role_name,
            archive_root=archive_root,
        )
        self.archive = ContentArchive(root_dir=archive_root)
        self.context_assembler = ContextAssembler(
            static_prefix_provider=(
                lambda u, r: static_prefix if static_prefix else None
            ) if static_prefix else None,
        )
        # handoff data_dir 与 state data_dir 同级但目录名独立
        ho_dir = os.path.join(
            os.path.dirname(data_dir) if data_dir else os.getcwd(),
            "data", "handoffs",
        )
        self.handoff_bus = HandoffBus(data_dir=ho_dir)

        # 确保 StateCard 已创建（get_or_create）
        self._ensure_card()

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

    # ------------------------------------------------------------------
    # 会话生命周期
    # ------------------------------------------------------------------

    def get_card(self) -> Optional[StateCard]:
        """获取当前 StateCard"""
        return self._card()

    def delete_session(self) -> bool:
        """删除本会话的状态卡"""
        return self.state_manager.delete_card(self.session_id)

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
        total = len(segments)

        # 归档原文全文
        refs = self.archive.store_segments(self.session_id, segments)
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

    def retrieve_original(self, content_ref: str) -> Optional[str]:
        """按 content_ref 精确检索并取回原文全文。"""
        return self.archive.retrieve_content(
            self.session_id, content_ref)

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
        result = self.context_assembler.assemble(
            session_id=self.session_id,
            role_name=self.role_name,
            unit_id=self.unit_id,
            user_text=user_text,
            static_prefix=static_prefix or self.static_prefix,
            state_card=card,
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
        return self.handoff_bus.create_handoff(
            source_unit=source_unit,
            target_unit=target_unit,
            intent=intent,
            context=context,
            auto_state_card=True,
        )

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
