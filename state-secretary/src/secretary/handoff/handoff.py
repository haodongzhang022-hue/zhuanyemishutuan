"""M5 跨单元交接总线"""
from __future__ import annotations

import hashlib
import json
import os
import tempfile
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

    数据根收敛：交接卡本体（handoffs/）与其关联的 StateCard 应同根——
    - data_dir:   交接卡 JSON 落盘目录
    - state_root: 关联 ho-* StateCard 的落盘根（显式传入时使用）
    - 默认推导：data_dir 显式传入且未指定 state_root 时，以 data_dir 父目录
      推导 state_root，使 ho-* 卡落 ``<parent(data_dir)>/state``，
      与 SecretarySession 三子目录（state/ content/ handoffs/）收敛逻辑一致，
      不再落 `<cwd>/data/state` 孤儿；
      data_dir 亦未传时保持历史退化行为（<cwd>/data/handoffs + <cwd> 根）。
    SecretarySession 会同时注入本会话 data_dir 下的 state/ 与 handoffs/，
    使交接卡与其关联状态卡收敛到同一会话数据根，不再落 `<cwd>/data/state` 孤儿。

    引用登记（state 根级，P2-① 复评 🔴#4）：``auto_state_card=True`` 时，
    除落盘交接卡外，还在 ``<state_root>/handoff-refs/<card_id>.ref`` 登记
    「本交接卡引用了哪张 ho-* 状态卡」。该登记与 ho-* 卡同根、随卡回收，
    使**交接卡目录与状态根不同树**的独立使用者也能被存活引用检查发现
    （仅靠扫描会话数据根无法覆盖该布局）。删除交接卡时同步注销登记。
    """

    def __init__(self, data_dir: Optional[str] = None,
                 state_root: Optional[str] = None):
        """初始化交接总线。

        Args:
            data_dir: 交接卡 JSON 落盘目录（默认 <cwd>/data/handoffs）
            state_root: 关联 ho-* StateCard 的状态根。默认推导规则——
                - data_dir 显式传入且未指定 state_root：以 ``data_dir`` 的
                  父目录作为状态根，ho-* 卡落 ``<parent(data_dir)>/state``，
                  与 SecretarySession 三子目录收敛到同一数据根一致；
                - 仅 data_dir 缺省（用 <cwd>/data/handoffs）：状态根保持历史
                  行为（<cwd>），向后兼容；
                显式传入后 auto_state_card 生成的关联状态卡落盘于
                ``<state_root>/state``，与调用方 StateManager 收敛同根。
        """
        # data_dir 缺省时才退化为 <cwd>/data/handoffs（历史默认）
        self.data_dir = data_dir or os.path.join(os.getcwd(), "data", "handoffs")
        # 关联 ho-* StateCard 的状态根推导：
        # - 显式 state_root：直接使用（最高优先）；
        # - 未传 state_root 但 data_dir 显式传入：以 data_dir 父目录推导，
        #   使 ho-* 卡与交接卡收敛同一数据根（state/ handoffs/ 同根），
        #   不再落 <cwd>/data/state 孤儿；
        # - 二者均未传：保持历史退化行为，关联 StateCard 默认落 <cwd>。
        if state_root is not None:
            self.state_root = state_root
        elif data_dir is not None:
            self.state_root = os.path.dirname(
                os.path.normpath(os.path.abspath(data_dir)))
        else:
            self.state_root = None
        os.makedirs(self.data_dir, exist_ok=True)
        # 线程锁：Web server 以 ThreadingHTTPServer 多线程复用模块级单例交接总线，
        # 并发 create_handoff/list_pending/update_status 会并行读写数据目录，
        # 不加锁存在文件交错写/读到半写文件的竞态。加锁串行化保证线程安全。
        # 用 RLock（可重入）：get/_save 等会被 update_status/list_pending 在持锁下再调用。
        self._lock = threading.RLock()

    # ---- 引用登记（state 根级，供存活引用检查发现异目录使用者）----

    def ref_registry_dir(self) -> Optional[str]:
        """引用登记目录 ``<state_root>/handoff-refs``；无 state_root 时 None。"""
        if not self.state_root:
            return None
        return os.path.join(self.state_root, "handoff-refs")

    def _register_ref(self, card_id: str, state_card_id: str) -> None:
        """登记一张交接卡对 ho-* 状态卡的引用（幂等、原子写）。

        记录 card_path 便于存活判定时核对"交接卡是否仍在"——卡被删除后
        登记即视为失效引用，可安全回收共享卡（无需依赖扫描其所在目录）。
        """
        reg_dir = self.ref_registry_dir()
        if not reg_dir or not card_id or not state_card_id:
            return
        try:
            os.makedirs(reg_dir, exist_ok=True)
            path = os.path.join(reg_dir, f"{card_id}.ref")
            fd, tmp_path = tempfile.mkstemp(dir=reg_dir, suffix=".tmp")
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    json.dump({
                        "card_id": card_id,
                        "state_card_id": state_card_id,
                        "card_path": os.path.abspath(
                            os.path.join(self.data_dir, f"{card_id}.json")),
                    }, f, ensure_ascii=False)
                os.replace(tmp_path, path)
            except Exception:
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass
        except OSError:
            # 登记失败不阻断交接卡本身（存活检查另有目录扫描兜底）
            pass

    def _unregister_ref(self, card_id: str) -> None:
        """注销一张交接卡的引用登记（删除交接卡时同步调用）。

        注销后若登记目录已空则回收该目录（与 delete_session「不留空目录
        残骸」的纪律一致）；下次登记会按需重建。
        """
        reg_dir = self.ref_registry_dir()
        if not reg_dir or not card_id:
            return
        try:
            path = os.path.join(reg_dir, f"{card_id}.ref")
            if os.path.exists(path):
                os.remove(path)
        except OSError:
            pass
        # 回收空登记目录（非空时 os.rmdir 抛 OSError，忽略即可）
        try:
            if os.path.isdir(reg_dir) and not os.listdir(reg_dir):
                os.rmdir(reg_dir)
        except OSError:
            pass

    def create_handoff(self, source_unit: str, target_unit: str,
                       intent: str, context: str = "",
                       auto_state_card: bool = False) -> HandoffCard:
        """创建一张交接卡（线程安全）。

        auto_state_card=True 时自动创建关联的 M9 StateCard，
        交接卡获得唯一会话ID，供下游秘书状态管理使用。

        **共享卡语义后果**：关联 StateCard 的 ID 为
        ``hash(source_unit, target_unit, intent)``，同源/同目标/同意图的多次
        调用**复用同一张卡**；该卡内的交接任务仅按标题去重，故**后续调用的
        ``context`` 不落卡**（后到者上下文静默丢弃）。需要各自上下文时请改用
        不同 ``intent``。
        """
        with self._lock:
            # 用纳秒时间戳 + 自增序号保证同秒同源唯一（秒级时间戳碰撞会覆盖丢卡）
            card_id = f"{source_unit}-{target_unit}-{time.time_ns()}"
            state_card_id = None

            # M9 集成：自动创建关联 StateCard
            if auto_state_card:
                try:
                    from secretary.state.manager import StateManager
                    # 关联 ho-* 状态卡落盘：状态根已按推导规则收敛
                    # （显式 state_root / data_dir 父目录 / 历史 <cwd>），
                    # 统一落 <state_root>/state 子目录。
                    sm = StateManager(
                        data_dir=os.path.join(self.state_root, "state")
                        if self.state_root else None)
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
            # 引用登记：与 ho-* 卡同根，供存活引用检查发现"交接卡目录与
            # 状态根不同树"的独立使用者（🔴#4）。
            if card.state_card_id:
                self._register_ref(card.id, card.state_card_id)
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

    def delete(self, card_id: str) -> bool:
        """删除一张交接卡（线程安全）。返回是否删除成功。

        删除后同步注销 state 根级引用登记（``handoff-refs/<card_id>.ref``），
        使该交接卡对共享 ho-* 状态卡的引用不再阻塞回收（🔴#4）。
        """
        with self._lock:
            path = os.path.join(self.data_dir, f"{card_id}.json")
            existed = os.path.exists(path)
            if existed:
                try:
                    os.remove(path)
                except OSError:
                    return False
            # 无论卡是否曾存在，登记都注销（幂等），避免残登记阻塞回收
            self._unregister_ref(card_id)
            return existed

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
