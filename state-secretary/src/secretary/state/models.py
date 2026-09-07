"""M9 状态管理层 · 数据模型定义"""
from __future__ import annotations

import time
from dataclasses import dataclass, field, fields as dc_fields
from enum import Enum
from typing import List, Optional


class TaskState(str, Enum):
    """任务状态机：todo → in_progress → done / blocked / stopped
    - stopped = 归档/停止追踪（设计 schema 语义态，fold.md §4 归档态）
    - failed / cancelled = 实现层额外状态，保留兼容"""
    TODO = "todo"
    IN_PROGRESS = "in_progress"
    DONE = "done"
    BLOCKED = "blocked"
    STOPPED = "stopped"      # 归档/停止追踪（对齐 schema 枚举）
    FAILED = "failed"        # 实现层：执行失败
    CANCELLED = "cancelled"  # 实现层：取消

    @classmethod
    def is_valid_transition(cls, from_state: str, to_state: str) -> bool:
        """校验合法状态迁移"""
        VALID = {
            "todo": {"in_progress", "blocked", "cancelled", "done", "stopped"},
            "in_progress": {"done", "blocked", "failed", "stopped"},
            "blocked": {"in_progress", "done", "cancelled", "failed", "stopped"},
            "failed": {"in_progress", "todo", "cancelled", "stopped"},
            "done": {"stopped"},            # done 后可归档停止追踪
            "cancelled": {"stopped"},       # cancelled 后可归档
            "stopped": set(),               # stopped 归档终态，不可再迁移
        }
        return to_state in VALID.get(from_state, set())


class TaskType(str, Enum):
    """任务类型：
    - normal: 普通任务（默认折叠）
    - deliverable: 最终交付给人类的末端任务（恒展开）
    """
    NORMAL = "normal"
    DELIVERABLE = "deliverable"


class FoldState(str, Enum):
    """折叠状态：
    - collapsed: 默认折叠（恒折叠，非运行中折叠）
    - expanded: 展开（仅当前执行态/交付任务展开）
    """
    COLLAPSED = "collapsed"
    EXPANDED = "expanded"


@dataclass
class TaskRecord:
    """一个已编号的折叠任务单元"""
    task_id: str                  # 唯一任务编号，如 HH-001
    title: str                    # 任务标题/摘要
    state: str = TaskState.TODO.value
    task_type: str = TaskType.NORMAL.value
    fold: str = FoldState.COLLAPSED.value
    objective: str = ""           # 本次主要目标
    cur_dir: str = ""             # 本次主要目录
    files_touched: List[str] = field(default_factory=list)
    depends_on: List[str] = field(default_factory=list)
    tools_called: List[str] = field(default_factory=list)
    content_ref: Optional[str] = None   # 原文引用（编号索引，不携带全量）
    summary: str = ""             # 折叠摘要
    created_at: str = ""
    updated_at: str = ""

    def to_dict(self) -> dict:
        return {
            "task_id": self.task_id,
            "title": self.title,
            "state": self.state,
            "task_type": self.task_type,
            "fold": self.fold,
            "objective": self.objective,
            "cur_dir": self.cur_dir,
            "files_touched": self.files_touched,
            "depends_on": self.depends_on,
            "tools_called": self.tools_called,
            "content_ref": self.content_ref,
            "summary": self.summary,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "TaskRecord":
        """从字典重建，字段级容错：
        - 缺少字段 → 使用默认值
        - 多余未知字段 → 忽略
        """
        if not isinstance(data, dict):
            return cls(task_id="", title="")
        known = {f.name for f in dc_fields(cls)}
        clean = {k: v for k, v in data.items() if k in known}
        # 保证必填字段有值
        clean.setdefault("task_id", "")
        clean.setdefault("title", "")
        return cls(**clean)


@dataclass
class StateCard:
    """M9 任务状态卡（StateCard）：
    一份可恢复的任务状态机 + 要素清单，作为对话/任务的上下文。
    秘书(坑位) = 该状态卡的唯一维护者/读者。
    """
    session_id: str               # 会话ID（对话唯一）
    unit_id: str = ""             # 所属行业单元
    role_name: str = "secretary"  # 维护者坑位
    owner: str = ""                  # 责任秘书坑位（卡归属/占用者，空=未占）
    objective: str = ""           # 总体目标
    tasks: List[TaskRecord] = field(default_factory=list)
    upstream: Optional[str] = None    # 上级秘书引用（层级向上提交）
    downstream: List[str] = field(default_factory=list)  # 下级秘书列表
    created_at: str = ""
    updated_at: str = ""
    version: int = 0                # 乐观并发版本号：每次写变更 +1，供冲突仲裁
    id: Optional[str] = None

    def __post_init__(self):
        if not self.created_at:
            self.created_at = time.strftime("%Y-%m-%d %H:%M:%S")
        if not self.updated_at:
            self.updated_at = self.created_at
        if not self.id:
            self.id = f"SC-{self.session_id}-{int(time.time())}"

    def get_task(self, task_id: str) -> Optional[TaskRecord]:
        """按任务编号取任务"""
        for t in self.tasks:
            if t.task_id == task_id:
                return t
        return None

    def next_task_seq(self) -> int:
        """下一个任务序号"""
        return len(self.tasks) + 1

    def add_task(self, title: str, **kwargs) -> TaskRecord:
        """新增任务（自动编号）。
        默认折叠策略：
        - deliverable(交付任务) → 展开
        - normal(普通任务) → 默认折叠
        """
        seq = self.next_task_seq()
        task_type = kwargs.pop("task_type", TaskType.NORMAL.value)
        # 交付任务默认展开
        if task_type == TaskType.DELIVERABLE.value:
            kwargs.setdefault("fold", FoldState.EXPANDED.value)
        else:
            kwargs.setdefault("fold", FoldState.COLLAPSED.value)

        task = TaskRecord(
            task_id=f"{self.session_id}-{seq:04d}",
            title=title,
            task_type=task_type,
            created_at=time.strftime("%Y-%m-%d %H:%M:%S"),
            updated_at=time.strftime("%Y-%m-%d %H:%M:%S"),
            **kwargs,
        )
        self.tasks.append(task)
        self.updated_at = time.strftime("%Y-%m-%d %H:%M:%S")
        self.version += 1
        return task

    def update_task(self, task_id: str, **fields) -> bool:
        """更新任务字段（含状态迁移校验）"""
        task = self.get_task(task_id)
        if not task:
            return False

        # 校验状态迁移合法性
        if "state" in fields and fields["state"] != task.state:
            if not TaskState.is_valid_transition(task.state, fields["state"]):
                return False

        for k, v in fields.items():
            if hasattr(task, k):
                setattr(task, k, v)
        task.updated_at = time.strftime("%Y-%m-%d %H:%M:%S")
        self.updated_at = time.strftime("%Y-%m-%d %H:%M:%S")
        self.version += 1
        return True

    def summarize_for_upstream(self) -> dict:
        """生成向上级提交的汇总状态（不含原文全量）。
        只携带结构化要素：进度、关键决策、开放项。
        """
        tasks_summary = []
        for t in self.tasks:
            tasks_summary.append({
                "task_id": t.task_id,
                "title": t.title,
                "state": t.state,
                "fold": t.fold,
                "content_ref": t.content_ref,
                "summary": t.summary or t.title,
            })

        states = [t.state for t in self.tasks]
        return {
            "session_id": self.session_id,
            "unit_id": self.unit_id,
            "role_name": self.role_name,
            "owner": self.owner,
            "version": self.version,
            "objective": self.objective,
            "task_count": len(self.tasks),
            "todo_count": states.count(TaskState.TODO.value),
            "done_count": states.count(TaskState.DONE.value),
            "in_progress_count": states.count(TaskState.IN_PROGRESS.value),
            "blocked_count": states.count(TaskState.BLOCKED.value),
            "failed_count": states.count(TaskState.FAILED.value),
            "cancelled_count": states.count(TaskState.CANCELLED.value),
            "stopped_count": states.count(TaskState.STOPPED.value),
            "tasks": tasks_summary,
            "content_refs": [t.content_ref for t in self.tasks if t.content_ref],
            "upstream": self.upstream,
            "downstream": self.downstream,
        }

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "session_id": self.session_id,
            "unit_id": self.unit_id,
            "role_name": self.role_name,
            "owner": self.owner,
            "version": self.version,
            "objective": self.objective,
            "tasks": [t.to_dict() for t in self.tasks],
            "upstream": self.upstream,
            "downstream": self.downstream,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "StateCard":
        """从字典重建，字段级容错：
        - 缺少字段 → 使用默认值
        - 多余未知字段 → 忽略
        - 损坏的任务数据 → 跳过该任务而非整卡静默丢失
        """
        if not isinstance(data, dict):
            return cls(session_id="")

        # 已知字段白名单
        known = {f.name for f in dc_fields(cls)}
        clean = {}
        for k, v in data.items():
            if k in known:
                clean[k] = v

        # tasks 字段单独处理：逐条容错恢复
        raw_tasks = data.get("tasks", [])
        task_list = []
        if isinstance(raw_tasks, list):
            for raw_t in raw_tasks:
                # 仅处理 dict 类型；损坏数据直接跳过
                if not isinstance(raw_t, dict):
                    continue
                try:
                    task_list.append(TaskRecord.from_dict(raw_t))
                except Exception:
                    continue  # 损坏的任务记录跳过，不拖垮整卡

        clean["tasks"] = task_list
        # session_id 必填，缺失则置空串（上层由 StateManager 负责校验）
        clean.setdefault("session_id", "")
        return cls(**clean)
