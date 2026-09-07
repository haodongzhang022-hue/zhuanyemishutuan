"""M9 延迟/批量落盘队列（FlushQueue）

用途：
把"需要落盘"的写动作统一放进一个队列，按批次一次性写出，
避免规模增大时每次 register/link/unlink / add_task 都触发一次全量磁盘 I/O。

特性：
- 同一资源（path）的多次脏写会合并，flush 时只写最后一次（增量取最终态）
- flush() 把当前 pending 全部批量写出（一批只写一轮磁盘）
- 支持 write-through（默认即时落盘，保持既有行为）与 deferred（延迟批量）两种模式
- 提供 auto_flush 阈值：pending 达到 batch_size 时自动批量落盘
"""
from __future__ import annotations

import time
from typing import Callable, Dict, List, Optional


class FlushQueue:
    """
    落盘队列。

    use_deferred = False  → write-through：enqueue 即立即执行 write_fn（兼容旧行为）
    use_deferred = True   → 延迟批量：仅标记 pending，flush() 时才真正批量落盘
    """

    def __init__(self, batch_size: int = 0, use_deferred: bool = False,
                 on_flush: Optional[Callable[[int], None]] = None):
        self._pending: Dict[str, dict] = {}   # path -> {payload, write_fn, ts}
        self.batch_size = max(batch_size, 0)
        self.use_deferred = use_deferred
        self.on_flush = on_flush              # 可注入回调，便于观测/flush 统计
        self._write_count = 0                 # 实际落盘次数
        self._flush_count = 0

    @property
    def pending_count(self) -> int:
        return len(self._pending)

    @property
    def write_count(self) -> int:
        """实际执行的磁盘写入次数（含 write-through 与 flush 批量写）"""
        return self._write_count

    @property
    def flush_count(self) -> int:
        """flush 批次次数"""
        return self._flush_count

    def enqueue(self, key: str, write_fn: Callable[[], None],
                payload: Optional[dict] = None) -> None:
        """
        登记一次待落盘操作。
        key: 唯一资源标识（如同一 session/树文件），同 key 多次登记会被合并。
        write_fn: 真正写盘的零参函数。
        payload: 可选，用于去重/观测（保留最近一次）。
        """
        self._pending[key] = {
            "write_fn": write_fn,
            "payload": payload,
            "ts": time.time(),
        }
        if not self.use_deferred:
            # write-through：立即执行
            self.flush()
            return
        if self.batch_size > 0 and self.pending_count >= self.batch_size:
            self.flush()

    def flush(self) -> int:
        """
        批量落盘所有 pending 项，返回本次写入的资源数。
        每项只写一次（同 key 只保留最后一次登记），一轮 flush 即完成一批。
        """
        items = list(self._pending.values())
        self._pending.clear()
        for item in items:
            try:
                item["write_fn"]()
                self._write_count += 1
            except Exception:
                # 单资源写失败不阻断同批其他项
                continue
        if items:
            self._flush_count += 1
            if self.on_flush:
                self.on_flush(len(items))
        return len(items)

    def flush_all(self) -> int:
        """语义同 flush()：清空并批量落盘当前 pending"""
        return self.flush()

    def clear(self) -> None:
        """丢弃所有 pending（不落盘），用于异常回滚场景"""
        self._pending.clear()


class DeferredWriter:
    """
    便捷上下文管理器：把一段需要多次变更的操作包成"延迟批量落盘"批次。

    with DeferredWriter(queue) as q:
        q.enqueue(...)   # 只登记
    # 退出时自动 flush() 一次性批量落盘
    """

    def __init__(self, queue: FlushQueue):
        self.queue = queue
        self._prev = None

    def __enter__(self) -> FlushQueue:
        self._prev = self.queue.use_deferred
        self.queue.use_deferred = True
        return self.queue

    def __exit__(self, exc_type, exc, tb) -> None:
        try:
            if exc_type is None:
                self.queue.flush()
        finally:
            self.queue.use_deferred = self._prev
        return None  # 不吞异常
