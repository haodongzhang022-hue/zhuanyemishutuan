"""M9 状态管理层：任务状态卡 + 折叠索引 + 原文归档 + 层级提交 + 秘书长多级树 + 秘书并发仲裁"""
from .models import (
    StateCard, TaskRecord, TaskState, TaskType, FoldState,
)
from .manager import StateManager
from .fold_engine import FoldEngine
from .content_archive import ContentArchive
from .persist import FlushQueue, DeferredWriter
from .hierarchy import HierarchySubmitter, SecretaryTree, SecretaryTreeNode
from .ownership import OwnershipGuard, CardLease, StateConflict, OwnershipDenied
from .arbiter import (
    ConflictArbiter, ReviewTicket, StateDiff, FieldChange,
    diff_state, diffs_overlap,
)
__all__ = [
    "StateCard", "TaskRecord", "TaskState", "TaskType", "FoldState",
    "StateManager", "FoldEngine", "ContentArchive", "HierarchySubmitter",
    "SecretaryTree", "SecretaryTreeNode",
    "OwnershipGuard", "CardLease", "StateConflict", "OwnershipDenied",
    "ConflictArbiter", "ReviewTicket", "StateDiff", "FieldChange",
    "diff_state", "diffs_overlap",
    "FlushQueue", "DeferredWriter",
]
