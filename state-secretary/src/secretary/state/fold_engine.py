"""M9 折叠渲染引擎：默认恒折叠 + 编号索引 + 非破坏性渲染层"""
from __future__ import annotations

import re
import time
from typing import Dict, List, Optional, Tuple

from .models import (
    StateCard, TaskRecord, TaskState, TaskType, FoldState,
)


class FoldEngine:
    """
    M9 折叠渲染引擎：
    将一段对话/任务原文按逻辑任务拆分、编号、折叠。
    - 默认所有上层任务为 collapsed（恒折叠，不是运行中折叠）
    - 仅末端交付任务(deliverable) + 当前执行态任务 展开
    - 原文不删除，仅生成可插拔的"渲染层/导航层"
    """

    def __init__(self, session_id: str, unit_id: str = "",
                 role_name: str = "secretary",
                 archive_root: Optional[str] = None):
        self.session_id = session_id
        self.unit_id = unit_id
        self.role_name = role_name
        # ContentArchive 归档根目录（默认 data/state/content），供测试/复用注入
        self.archive_root = archive_root

    # ---- 原文折叠 ----

    def fold_content(self, content: str,
                     content_meta: Optional[dict] = None,
                     archive: bool = True) -> StateCard:
        """
        将一段对话原文按逻辑块拆分并生成 StateCard。
        采用启发式拆分：按换行/编号分割任务片段。
        每段 → 一个 TaskRecord，内容默认折叠，原文引用保留在 content_ref。

        archive=True（默认）时，将每段**原文全文**持久化到 ContentArchive
        （data/state/content/），使折叠后的 content_ref 真正可按索引取回原文，
        落实秘书机制"原文不丢、编号可找回"的核心承诺。
        """
        from .content_archive import ContentArchive

        meta = content_meta or {}
        objective = meta.get("objective", "")
        cur_dir = meta.get("cur_dir", "")
        files = meta.get("files", [])
        tools = meta.get("tools", [])

        card = StateCard(
            session_id=self.session_id,
            unit_id=self.unit_id,
            role_name=self.role_name,
            objective=objective,
        )

        segments = self._split_segments(content)
        total = len(segments)

        # 若开启归档，先批量持久化每段原文全文，得到真正可寻回的 content_ref
        archive_refs: List[str] = []
        if archive:
            archive_refs = ContentArchive(
                root_dir=self.archive_root).store_segments(
                self.session_id, segments)
            # 保持与段号一一对应（跳过空段后可能出现错位，这里重建对应关系）
            segments = [s.strip() for s in segments if s.strip()]
            total = len(segments)

        for i, seg in enumerate(segments):
            is_deliverable = (i == total - 1)  # 最后一段为交付段
            task_type = TaskType.DELIVERABLE.value if is_deliverable else TaskType.NORMAL.value
            # 非交付任务恒折叠；交付任务展开
            fold = FoldState.EXPANDED.value if is_deliverable else FoldState.COLLAPSED.value

            content_ref = (
                archive_refs[i] if archive and i < len(archive_refs)
                else self._gen_content_ref(i + 1)
            )
            task = card.add_task(
                title=self._extract_title(seg),
                summary=seg[:200] + ("..." if len(seg) > 200 else ""),
                task_type=task_type,
                fold=fold,
                objective=objective,
                cur_dir=cur_dir,
                files_touched=files,
                tools_called=tools,
                content_ref=content_ref,
            )
        return card

    def _split_segments(self, content: str) -> List[str]:
        """将原文按逻辑任务切分。
        策略：按行切割，连续行合并为段。
        实际场景中秘书会显式传任务列表；此处的启发式切分用于最小POC。
        """
        content = content.strip()
        if not content:
            return []
        # 按空行切分，每段视为一个任务
        parts = re.split(r'\n\s*\n', content)
        return [p.strip() for p in parts if p.strip()]

    def _extract_title(self, segment: str) -> str:
        """从段落提取标题（取首行前30字符）"""
        first_line = segment.split("\n")[0].strip()
        return first_line[:50] if first_line else "未命名任务"

    def _gen_content_ref(self, seq: int) -> str:
        """生成原文引用编号"""
        return f"{self.session_id}-seg{seq:03d}"

    # ---- 渲染层 ----

    def render_navigation(self, card: StateCard) -> str:
        """
        生成折叠导航/任务索引（渲染层视图）。
        这是状态机的可视化投影，让人类一眼看全局，让AI精确到任务。
        不修改原文。
        """
        lines = []
        lines.append(f"📋 StateCard: {card.session_id}")
        if card.objective:
            lines.append(f"🎯 目标: {card.objective}")
        lines.append("")

        for task in card.tasks:
            state_icon = {
                TaskState.TODO.value: "⬜",
                TaskState.IN_PROGRESS.value: "🔄",
                TaskState.DONE.value: "✅",
                TaskState.BLOCKED.value: "⛔",
                TaskState.FAILED.value: "❌",
            }.get(task.state, "⬜")

            fold_mark = "" if task.fold == FoldState.EXPANDED.value else " [折叠]"
            deliver_mark = " [交付]" if task.task_type == TaskType.DELIVERABLE.value else ""
            ref = f" (ref: {task.content_ref})" if task.content_ref else ""

            lines.append(f"  {state_icon} #{task.task_id} {task.title}{fold_mark}{deliver_mark}{ref}")

            # 展开的任务显示摘要/要素
            if task.fold == FoldState.EXPANDED.value:
                if task.objective:
                    lines.append(f"      目标: {task.objective}")
                if task.cur_dir:
                    lines.append(f"      目录: {task.cur_dir}")
                if task.files_touched:
                    lines.append(f"      文件: {', '.join(task.files_touched)}")
                if task.tools_called:
                    lines.append(f"      工具: {', '.join(task.tools_called)}")
                if task.depends_on:
                    lines.append(f"      依赖: {', '.join(task.depends_on)}")
                if task.summary:
                    # 只显示摘要前120字符
                    brief = task.summary[:120] + ("..." if len(task.summary) > 120 else "")
                    lines.append(f"      摘要: {brief}")

        # 统计
        states = [t.state for t in card.tasks]
        done = states.count(TaskState.DONE.value)
        in_prog = states.count(TaskState.IN_PROGRESS.value)
        blocked = states.count(TaskState.BLOCKED.value)
        total = len(card.tasks)
        lines.append("")
        lines.append(f"📊 进度: {done}/{total} 完成, {in_prog} 进行中, {blocked} 阻塞")

        return "\n".join(lines)

    def compact_context(self, card: StateCard) -> str:
        """
        生成"压缩上下文"：只携带展开任务的要素 + 折叠任务的编号引用。
        这是执行 agent 真正看到的 prompt 片段——不含全量原文。
        """
        parts = []
        expanded = [t for t in card.tasks if t.fold == FoldState.EXPANDED.value]
        collapsed = [t for t in card.tasks if t.fold == FoldState.COLLAPSED.value]

        if card.objective:
            parts.append(f"总体目标: {card.objective}")

        if expanded:
            parts.append("— 当前执行态 —")
            for t in expanded:
                info = [f"#{t.task_id} {t.title} ({t.state})"]
                if t.objective:
                    info.append(f"目标: {t.objective}")
                if t.cur_dir:
                    info.append(f"目录: {t.cur_dir}")
                if t.files_touched:
                    info.append(f"文件: {', '.join(t.files_touched)}")
                if t.tools_called:
                    info.append(f"工具: {', '.join(t.tools_called)}")
                if t.depends_on:
                    info.append(f"依赖: {', '.join(t.depends_on)}")
                parts.append("; ".join(info))

        if collapsed:
            refs = ", ".join(
                f"#{t.task_id}" + (f"(ref:{t.content_ref})" if t.content_ref else "")
                for t in collapsed
            )
            parts.append(f"— 已折叠任务(按需检索) —")
            parts.append(refs)

        return "\n".join(parts)

    def resolve_ref(self, card: StateCard, task_id: str) -> Optional[TaskRecord]:
        """按任务编号精确检索任务（索引机制）"""
        return card.get_task(task_id)

    def retrieve_original(self, session_id: str,
                          content_ref: str) -> Optional[str]:
        """按 content_ref 从归档取回**原文全文**（ContentArchive）。

        这是"折叠后原文仍可通过索引找回"的关键入口：
        content_ref 不再只是编号标记，而是真正能解析回原文的归档索引。
        归档不存在 / 引用非法时返回 None。
        """
        from .content_archive import ContentArchive
        try:
            return ContentArchive(
                root_dir=self.archive_root).retrieve_content(
                session_id, content_ref)
        except Exception:
            return None

    # ---- M8 跨任务检索集成 ----

    def search_tasks(self, cards: list, keyword: str,
                     fields=None, include_original: bool = False) -> List[dict]:
        """在多个 StateCard 的折叠任务中搜索关键词。
        用于 M8 跨会话检索：让已折叠的内容也能被发现。

        参数:
        - cards: StateCard 列表（从 StateManager.list_cards() 获取）
        - keyword: 搜索关键词
        - fields: 搜索字段列表，默认 title/summary/objective/content_ref
        - include_original: True 时对每个命中任务额外尝试从 ContentArchive
          取回原文全文，放入返回的 "original" 字段（找不到则为 None）。

        返回: [{session_id, task_id, title, state, fold, field_hit,
                content_ref, original?}]
        """
        search_fields = fields or ["title", "summary", "objective",
                                   "content_ref", "files_touched"]
        results = []
        kw = keyword.lower()
        for card in cards:
            for task in card.tasks:
                hits = []
                for f in search_fields:
                    value = getattr(task, f, None)
                    if value is None:
                        continue
                    if isinstance(value, str) and kw in value.lower():
                        hits.append(f)
                    elif isinstance(value, list):
                        if any(kw in str(v).lower() for v in value):
                            hits.append(f)
                if hits:
                    item = {
                        "session_id": card.session_id,
                        "task_id": task.task_id,
                        "title": task.title,
                        "state": task.state,
                        "fold": task.fold,
                        "field_hit": hits,
                        "content_ref": task.content_ref,
                    }
                    if include_original:
                        item["original"] = (
                            self.retrieve_original(card.session_id,
                                                   task.content_ref)
                            if task.content_ref else None
                        )
                    results.append(item)
        return results

    def search_all_states(self, keyword: str, data_dir: Optional[str] = None) -> List[dict]:
        """在所有 StateCard 中搜索任务内容（M8 集成便捷入口）。

        参数:
        - keyword: 搜索关键词
        - data_dir: 可选的状态数据目录。缺省时使用当前工作目录下的
          默认 data/state 目录（与 CLI state search 一致）。

        返回: [{session_id, task_id, title, state, fold, field_hit}]
        """
        from .manager import StateManager
        sm = StateManager(data_dir=data_dir)
        cards = sm.list_cards()
        return self.search_tasks(cards, keyword)

    def auto_fold_policy(self, card: StateCard, current_task_id: str = None) -> StateCard:
        """
        应用默认折叠策略：
        - 交付任务(deliverable) → 恒展开
        - 当前执行任务（in_progress 或显式指定）→ 展开
        - 其他普通任务 → 恒折叠（可编号检索）
        不删除原文，仅更新渲染层的 fold 状态。
        """
        # 找出当前执行任务（未显式指定时自动找第一个 in_progress）
        active_id = current_task_id
        if not active_id:
            for task in card.tasks:
                if task.state == TaskState.IN_PROGRESS.value:
                    active_id = task.task_id
                    break

        for task in card.tasks:
            if task.task_type == TaskType.DELIVERABLE.value:
                # 交付任务恒展开
                task.fold = FoldState.EXPANDED.value
            elif active_id and task.task_id == active_id:
                # 当前执行任务展开
                task.fold = FoldState.EXPANDED.value
            else:
                # 其余默认折叠
                task.fold = FoldState.COLLAPSED.value
            task.updated_at = time.strftime("%Y-%m-%d %H:%M:%S")
        card.updated_at = time.strftime("%Y-%m-%d %H:%M:%S")
        return card
