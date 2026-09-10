"""M9 状态管理器：StateCard 的生命周期管理"""
from __future__ import annotations

import fcntl
import json
import os
import re
import tempfile
from typing import Dict, List, Optional

from .persist import FlushQueue
from .models import StateCard, TaskRecord, TaskState, FoldState

# session_id 白名单：仅允许字母/数字/下划线/短横线
# 尾锚 ``\Z``（非 ``$``，后者会放行尾换行）——🟡-4 同族收紧。
_SESSION_ID_PATTERN = re.compile(r'^[A-Za-z0-9_-]+\Z')


class _CardLockContext:
    """card_lock() 的上下文管理器包装。"""

    def __init__(self, lock_f):
        self._lock_f = lock_f

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        if self._lock_f is not None:
            try:
                import fcntl
                fcntl.flock(self._lock_f.fileno(), fcntl.LOCK_UN)
            except (OSError, ValueError):
                pass
            finally:
                try:
                    self._lock_f.close()
                except (OSError, ValueError):
                    pass
                self._lock_f = None
        return None  # 不吞异常


class StateManager:
    """
    M9 状态管理器（StateManager）：
    - 每个 session/对话 分配一张 StateCard
    - 管理 StateCard 的创建/读取/更新/删除
    - 落盘到 data/state/ 目录（JSON）
    - 提供折叠/展开查询
    """

    def __init__(self, data_dir: Optional[str] = None,
                 flush_queue: Optional[FlushQueue] = None):
        self.data_dir = data_dir or os.path.join(os.getcwd(), "data", "state")
        os.makedirs(self.data_dir, exist_ok=True)
        # 落盘统一走队列：默认 write-through 即时落盘（保持既有行为），
        # 高频/规模场景可用 defer() 批量延迟落盘。
        self._flush_queue = flush_queue or FlushQueue()
        # 内存态缓存：defer() 延迟落盘期间，同一会话的多次变更在内存累计，
        # 读操作(如 add_task 前 get_card)从缓存取最新态，保证批内正确。
        self._mem_cache: Dict[str, StateCard] = {}

    # ---- 生命周期管理 ----

    def create_card(self, session_id: str, objective: str = "",
                    unit_id: str = "", role_name: str = "secretary",
                    **kwargs) -> StateCard:
        """创建一张新的 StateCard"""
        self._validate_session_id(session_id)
        card = StateCard(
            session_id=session_id,
            unit_id=unit_id,
            role_name=role_name,
            objective=objective,
            **kwargs,
        )
        self.save_card(card)
        return card

    def get_card(self, session_id: str) -> Optional[StateCard]:
        """按会话ID读取 StateCard（延迟落盘期间从内存缓存取最新态）"""
        if not self._is_valid_session_id(session_id):
            return None
        if session_id in self._mem_cache:
            return self._mem_cache[session_id]
        path = self._card_path(session_id)
        if not os.path.exists(path):
            return None
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            card = StateCard.from_dict(data)
            return card
        except Exception:
            return None

    def get_or_create(self, session_id: str, objective: str = "",
                      unit_id: str = "", role_name: str = "secretary") -> StateCard:
        """获取已有卡，不存在则创建"""
        card = self.get_card(session_id)
        if card is None:
            card = self.create_card(session_id, objective, unit_id, role_name)
        return card

    def delete_card(self, session_id: str) -> bool:
        """删除一张 StateCard（同步清理内存缓存与全部伴生残留）。

        一次回收该会话在状态卡目录内的**全部**本会话数据：
        - 卡文件 ``<session>.json``
        - 卡 flock 锁文件 ``<session>.lock``（card_lock 产生的写锁残骸）
        - 卡 owner 租约文件 ``<session>.owner.json``（OwnershipGuard 占用声明）

        **返回值语义（P2-③ 统一）**：返回「调用前本会话状态卡是否存在」——
        ``<session>.json`` 存在且已删除 → ``True``；状态卡本已缺失 → ``False``。
        .lock / .owner.json 等**伴生文件残留不影响返回值**：无论返回 True 或
        False，伴生残留都会被一并清理（幂等删除）。非法 session_id 返回
        ``False`` 且不触碰任何文件。

        生命周期语义完整：delete_card 后磁盘不残留本会话 .json / .lock /
        .owner.json。状态卡数据根（state/）目录自身保留——同根可容纳其他
        会话卡且供实例复用重建，不属于"本会话残骸"。
        """
        if not self._is_valid_session_id(session_id):
            return False
        self._mem_cache.pop(session_id, None)
        card_path = self._card_path(session_id)
        lock_path = os.path.join(self.data_dir, f"{session_id}.lock")
        # 注意：须与 ownership.OwnershipGuard.LOCK_SUFFIX(".owner.json") 保持一致
        # （ownership 模块 import 本模块，此处避免循环引用故用字面量并留注释对齐）
        lease_path = os.path.join(self.data_dir, f"{session_id}.owner.json")
        # 返回值语义：仅反映"调用前状态卡是否存在"，与伴生文件是否残留无关
        card_existed = os.path.exists(card_path)
        # 删除卡文件 / 锁文件 / 租约文件（全部本会话伴生文件，幂等清理）
        for path in (card_path, lock_path, lease_path):
            try:
                if os.path.exists(path):
                    os.remove(path)
            except OSError:
                pass
        # 数据根目录（state/）保留：同根可容纳其他会话卡，且供实例复用重建，
        # 不属于本会话的"残骸"；delete_card 只需清掉本会话自身文件。
        return card_existed

    def list_cards(self) -> List[StateCard]:
        """列出所有 StateCard"""
        cards = []
        for f in os.listdir(self.data_dir):
            # 跳过租约文件(.owner.json)、锁文件(.lock)、临时文件(.json.tmp)
            if (f.endswith(".json") and not f.endswith(".owner.json")
                    and not f.endswith(".lock") and not f.endswith(".tmp")):
                path = os.path.join(self.data_dir, f)
                try:
                    with open(path, "r", encoding="utf-8") as fh:
                        data = json.load(fh)
                    cards.append(StateCard.from_dict(data))
                except Exception:
                    continue
        return cards

    # ---- 任务操作 ----

    def add_task(self, session_id: str, title: str,
                 objective: str = "", cur_dir: str = "",
                 files: Optional[List[str]] = None,
                 depends_on: Optional[List[str]] = None,
                 tools: Optional[List[str]] = None,
                 task_type: str = "normal",
                 content_ref: Optional[str] = None,
                 **kwargs) -> Optional[TaskRecord]:
        """新增一个任务到 StateCard"""
        card = self.get_card(session_id)
        if card is None:
            return None
        task = card.add_task(
            title=title,
            objective=objective,
            cur_dir=cur_dir,
            files_touched=files or [],
            depends_on=depends_on or [],
            tools_called=tools or [],
            task_type=task_type,
            content_ref=content_ref,
            **kwargs,
        )
        self.save_card(card)
        return task

    def update_task_state(self, session_id: str, task_id: str,
                          state: str) -> bool:
        """更新任务状态（带迁移校验）"""
        card = self.get_card(session_id)
        if card is None:
            return False
        ok = card.update_task(task_id, state=state)
        if ok:
            self.save_card(card)
        return ok

    def update_task_fields(self, session_id: str, task_id: str,
                           **fields) -> bool:
        """更新任务的任意字段"""
        card = self.get_card(session_id)
        if card is None:
            return False
        ok = card.update_task(task_id, **fields)
        if ok:
            self.save_card(card)
        return ok

    def fold_task(self, session_id: str, task_id: str) -> bool:
        """折叠指定任务（默认所有任务都是折叠态，此处用于显式折叠）"""
        return self.update_task_fields(session_id, task_id, fold=FoldState.COLLAPSED.value)

    def expand_task(self, session_id: str, task_id: str) -> bool:
        """展开指定任务（仅当前执行态/交付任务展开）"""
        return self.update_task_fields(session_id, task_id, fold=FoldState.EXPANDED.value)

    # ---- 查询 ----

    def get_expanded_tasks(self, session_id: str) -> List[TaskRecord]:
        """获取当前展开的任务列表"""
        card = self.get_card(session_id)
        if card is None:
            return []
        return [t for t in card.tasks if t.fold == FoldState.EXPANDED.value]

    def get_collapsed_tasks(self, session_id: str) -> List[TaskRecord]:
        """获取当前折叠的任务列表"""
        card = self.get_card(session_id)
        if card is None:
            return []
        return [t for t in card.tasks if t.fold == FoldState.COLLAPSED.value]

    def get_active_task(self, session_id: str) -> Optional[TaskRecord]:
        """获取当前正在执行的任务（第一个 in_progress）"""
        card = self.get_card(session_id)
        if card is None:
            return None
        for t in card.tasks:
            if t.state == TaskState.IN_PROGRESS.value:
                return t
        return None

    def get_deliverable_tasks(self, session_id: str) -> List[TaskRecord]:
        """获取末端交付任务（task_type=deliverable 且 fold=expanded）"""
        card = self.get_card(session_id)
        if card is None:
            return []
        return [t for t in card.tasks
                if t.task_type == "deliverable"
                and t.fold == FoldState.EXPANDED.value]

    # ---- 层级 ----

    def set_upstream(self, session_id: str, upstream_session: str) -> bool:
        """设置上级秘书引用"""
        card = self.get_card(session_id)
        if card is None:
            return False
        card.upstream = upstream_session
        self.save_card(card)
        return True

    def add_downstream(self, session_id: str, downstream_session: str) -> bool:
        """添加下级秘书引用"""
        card = self.get_card(session_id)
        if card is None:
            return False
        if downstream_session not in card.downstream:
            card.downstream.append(downstream_session)
        self.save_card(card)
        return True

    # ---- 公开持久化接口 ----

    def save_card(self, card: StateCard) -> None:
        """将 StateCard 持久化到磁盘（供 CLI 等外部调用）"""
        self._validate_session_id(card.session_id)
        self._save(card)

    def apply_fold_policy(self, session_id: str,
                          current_task_id: str = None) -> Optional[StateCard]:
        """公开的折叠策略入口：对指定 session 执行自动折叠并持久化"""
        from .fold_engine import FoldEngine
        card = self.get_card(session_id)
        if card is None:
            return None
        engine = FoldEngine(card.session_id)
        card = engine.auto_fold_policy(card, current_task_id=current_task_id)
        self.save_card(card)
        return card

    # ---- 并发安全原语（供 OwnershipGuard 使用） ----

    def card_lock(self, session_id: str):
        """获取 session 状态卡的 flock 排他锁（上下文管理器）。

        用独立的 .lock 文件承载 flock，避免 os.replace 替换 .json 导致锁失效。
        跨进程/线程同一文件写操作互斥。
        使用方法：
            with sm.card_lock(sid):
                ...
        """
        self._validate_session_id(session_id)
        lock_path = os.path.join(self.data_dir, f"{session_id}.lock")
        lock_f = open(lock_path, "w", encoding="utf-8")
        try:
            fcntl.flock(lock_f.fileno(), fcntl.LOCK_EX)
        except Exception:
            lock_f.close()
            raise
        return _CardLockContext(lock_f)

    def lock_card(self, session_id: str):
        """兼容入口：获取 flock 排他写锁，返回文件对象（须配合 release_lock 释放）。"""
        self._validate_session_id(session_id)
        lock_path = os.path.join(self.data_dir, f"{session_id}.lock")
        lock_f = open(lock_path, "w", encoding="utf-8")
        try:
            fcntl.flock(lock_f.fileno(), fcntl.LOCK_EX)
        except Exception:
            lock_f.close()
            raise
        return lock_f

    @staticmethod
    def release_lock(lock_f) -> None:
        """释放由 lock_card 获得的文件锁。"""
        if lock_f is None:
            return
        try:
            fcntl.flock(lock_f.fileno(), fcntl.LOCK_UN)
        except (OSError, ValueError):
            pass
        finally:
            try:
                lock_f.close()
            except (OSError, ValueError):
                pass

    def read_card_disk(self, session_id: str) -> Optional[StateCard]:
        """从磁盘强制读取最新卡（绕过内存缓存），供 CAS 校验用。"""
        if not self._is_valid_session_id(session_id):
            return None
        self._mem_cache.pop(session_id, None)  # 确保读到的是磁盘最新值
        path = self._card_path(session_id)
        if not os.path.exists(path):
            return None
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            return StateCard.from_dict(data)
        except Exception:
            return None

    def cas_save_card(self, card: StateCard,
                      expected_version: Optional[int] = None,
                      skip_lock: bool = False) -> tuple:
        """带 flock 排他锁 + CAS 版本校验的原子落盘。

        参数:
            card: 待保存的最新 StateCard（调用方已在内存中完成修改）
            expected_version: 调用方"读到的版本"。若不提供则不做版本校验（兼容旧用法）。
            skip_lock: 调用方已在 card_lock 上下文内时传 True，避免重复 flock 死锁。
        返回:
            (ok: bool, current_version: int)
            ok=True → CAS 成功且已落盘；ok=False → 版本冲突(需重读重试)。
        """
        session_id = card.session_id
        self._validate_session_id(session_id)
        lock_f = None
        if not skip_lock:
            lock_f = self.lock_card(session_id)
        try:
            # 重新读磁盘最新版本（绕过缓存）
            disk_card = self.read_card_disk(session_id)
            if disk_card is not None and expected_version is not None:
                if disk_card.version != expected_version:
                    # 版本冲突 → 不落盘，返回 False。
                    # 不写入 _mem_cache：该卡从未成功落盘，写入缓存会形成幻影态，
                    # 后续 get_card 读到与磁盘分叉的数据。与成功路径一致，直接清理缓存。
                    self._mem_cache.pop(session_id, None)
                    return (False, disk_card.version)

            # CAS 通过 → 原子写盘
            self._mem_cache[session_id] = card
            path = self._card_path(session_id)
            self._atomic_write_json(path, card.to_dict())
            self._mem_cache.pop(session_id, None)
            return (True, card.version)
        finally:
            if not skip_lock:
                self.release_lock(lock_f)

    # ---- 内部 ----

    @staticmethod
    def _is_valid_session_id(session_id: str) -> bool:
        """校验 session_id 是否合法（仅 [A-Za-z0-9_-]）"""
        if not isinstance(session_id, str) or not session_id:
            return False
        return bool(_SESSION_ID_PATTERN.match(session_id))

    @classmethod
    def _validate_session_id(cls, session_id: str) -> None:
        """校验 session_id，非法则抛 ValueError"""
        if not cls._is_valid_session_id(session_id):
            raise ValueError(
                f"非法 session_id: {session_id!r}。仅允许字母/数字/下划线/短横线 "
                f"([A-Za-z0-9_-])，防止路径穿越。"
            )

    def _card_path(self, session_id: str) -> str:
        self._validate_session_id(session_id)
        return os.path.join(self.data_dir, f"{session_id}.json")

    def _atomic_write_json(self, path: str, data: dict) -> None:
        """原子写 JSON：写临时文件 + os.replace，避免半写文件被并发读到。"""
        dir_path = os.path.dirname(os.path.abspath(path))
        fd, tmp_path = tempfile.mkstemp(dir=dir_path, suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            os.replace(tmp_path, path)
        except Exception:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise

    def _save(self, card: StateCard):
        """登记一次卡落盘（统一走队列，支持批量/延迟落盘）。

        写入统一经 _flush_queue：
        - write-through：登记即写盘（保持既有行为）
        - defer() 延迟批量：仅登记并缓存内存态，flush() 时写最新态一次
        """
        card_sid = card.session_id
        self._mem_cache[card_sid] = card   # 保存最新内存态

        def _write():
            # flush 时取最新内存态序列化，保证同一 session 多次变更只写最终态
            latest = self._mem_cache.get(card_sid, card)
            path = self._card_path(card_sid)
            lock_f = None
            try:
                # flock 排他锁保护：跨进程同 session 并发写时不互相打断
                lock_f = self.lock_card(card_sid)
                self._atomic_write_json(path, latest.to_dict())
            finally:
                self.release_lock(lock_f)
            # 已落盘，可从缓存移除（下次读回磁盘最新值）
            if self._mem_cache.get(card_sid) is latest:
                self._mem_cache.pop(card_sid, None)
        self._flush_queue.enqueue(self._card_path(card_sid), _write)

    # ---- 批量/延迟落盘 ----

    def flush(self) -> int:
        """强制批量落盘当前 pending 的卡写入，返回写盘资源数"""
        return self._flush_queue.flush()

    def defer(self, batch_size: int = 0):
        """
        进入"延迟批量落盘"上下文：上下文内多次建卡/加任务仅登记，
        退出时统一批量落盘。
        """
        from .persist import DeferredWriter
        self._flush_queue.batch_size = max(batch_size, self._flush_queue.batch_size)
        return DeferredWriter(self._flush_queue)

    @property
    def pending_write_count(self) -> int:
        """尚未落盘的待写登记数（延迟模式下观测用）"""
        return self._flush_queue.pending_count
