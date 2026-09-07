"""M5 跨单元交接总线"""
from __future__ import annotations

import hashlib
import json
import os
import threading
import time
from typing import List, Optional

from .models import HandoffCard


class HandoffBus:
    """
    M5 跨单元交接总线：
    管理跨行业/跨岗位交接卡的生命周期。
    交接卡采用 JSON 格式存储，便于后续扩展为异步消息。

    M9 集成：创建交接卡时若指定 auto_state_card=True，
    自动为交接创建/关联一张 StateCard（M9 状态卡）。
    """

    def __init__(self, data_dir: Optional[str] = None):
        self.data_dir = data_dir or os.path.join(os.getcwd(), "data", "handoffs")
        os.makedirs(self.data_dir, exist_ok=True)
        # 线程锁：Web server 以 ThreadingHTTPServer 多线程复用模块级单例交接总线，
        # 并发 create_handoff/list_pending/update_status 会并行读写数据目录，
        # 不加锁存在文件交错写/读到半写文件的竞态。加锁串行化保证线程安全。
        # 用 RLock（可重入）：get/_save 等会被 update_status/list_pending 在持锁下再调用。
        self._lock = threading.RLock()

    def create_handoff(self, source_unit: str, target_unit: str,
                       intent: str, context: str = "",
                       auto_state_card: bool = False) -> HandoffCard:
        """创建一张交接卡（线程安全）。

        auto_state_card=True 时自动创建关联的 M9 StateCard，
        交接卡获得唯一会话ID，供下游秘书状态管理使用。
        """
        with self._lock:
            # 用纳秒时间戳 + 自增序号保证同秒同源唯一（秒级时间戳碰撞会覆盖丢卡）
            card_id = f"{source_unit}-{target_unit}-{time.time_ns()}"
            state_card_id = None

            # M9 集成：自动创建关联 StateCard
            if auto_state_card:
                try:
                    from secretary.state.manager import StateManager
                    sm = StateManager()
                    # 会话ID = 交接卡意图的简单哈希，保证稳定可检索
                    hash_hex = hashlib.md5(
                        f"{source_unit}-{target_unit}-{intent}".encode()
                    ).hexdigest()[:12]
                    state_card_id = f"ho-{source_unit[:8]}-{target_unit[:8]}-{hash_hex}"
                    # 获取或创建 StateCard（同一稳定会话复用时沿用既有卡）
                    sm.get_or_create(
                        session_id=state_card_id,
                        objective=intent,
                        unit_id=target_unit,
                        role_name="secretary",
                    )
                    # 向 StateCard 添加交接任务，但同一 StateCard 已存在同名任务时不重复堆积
                    task_title = f"交接: {source_unit} -> {target_unit}"
                    card_state = sm.get_card(state_card_id)
                    exists = any(
                        t.title == task_title for t in card_state.tasks
                    ) if card_state else False
                    if not exists:
                        sm.add_task(
                            session_id=state_card_id,
                            title=task_title,
                            objective=intent,
                            task_type="deliverable",
                            summary=context,
                        )
                except Exception:
                    # StateCard 创建失败不阻断交接卡本身
                    state_card_id = None

            card = HandoffCard(
                id=card_id,
                source_unit=source_unit,
                target_unit=target_unit,
                intent=intent,
                context=context,
                state_card_id=state_card_id,
                created_at=time.strftime("%Y-%m-%d %H:%M:%S"),
            )
            self._save(card)
            return card

    def get(self, card_id: str) -> Optional[HandoffCard]:
        """按 ID 读取交接卡（线程安全）"""
        with self._lock:
            path = os.path.join(self.data_dir, f"{card_id}.json")
            if not os.path.exists(path):
                return None
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            return HandoffCard(**data)

    def update_status(self, card_id: str, status: str) -> bool:
        """更新交接卡状态（线程安全）"""
        with self._lock:
            card = self.get(card_id)
            if not card:
                return False
            card.status = status
            self._save(card)
            return True

    def list_pending(self) -> List[HandoffCard]:
        """列出所有待处理交接卡（线程安全）"""
        with self._lock:
            pending = []
            for f in os.listdir(self.data_dir):
                if f.endswith(".json"):
                    card = self.get(f[:-5])
                    if card and card.status == "created":
                        pending.append(card)
            return pending

    def _save(self, card: HandoffCard):
        if not card.id:
            card.id = f"{card.source_unit}-{card.target_unit}-{time.time_ns()}"
        path = os.path.join(self.data_dir, f"{card.id}.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump({
                "id": card.id,
                "source_unit": card.source_unit,
                "target_unit": card.target_unit,
                "intent": card.intent,
                "context": card.context,
                "status": card.status,
                "state_card_id": card.state_card_id,
                "created_at": card.created_at,
            }, f, ensure_ascii=False, indent=2)


    def validate_card(self, card: HandoffCard) -> dict:
        """执行 M5 × M9 完整度校验（标准字段检查）"""
        return card.completeness_check()

    def create_standard_handoff(self, source_unit: str, target_unit: str,
                                intent: str, context: str = "") -> HandoffCard:
        """创建符合 M5 × M9 跨单元标准的交接卡（强制关联 StateCard）。

        与 create_handoff 区别：
        - 自动创建关联 StateCard
        - 返回前执行完整度校验
        """
        card = self.create_handoff(
            source_unit=source_unit,
            target_unit=target_unit,
            intent=intent,
            context=context,
            auto_state_card=True,
        )
        check = card.completeness_check()
        if not check["passed"]:
            raise ValueError(
                f"交接卡不完整: 缺少 {', '.join(check['missing'])}"
            )
        return card
