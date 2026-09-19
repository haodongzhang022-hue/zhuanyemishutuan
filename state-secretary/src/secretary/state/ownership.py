"""M9 秘书并发归属与冲突仲裁（OwnershipGuard）

背景：Issue #5 秘书分工机制中，多 Agent / 多秘书需在"层级树"里协作推进同一任务，
但可能同时读写同一张 StateCard（如两个秘书都想把某任务标 done / 挂依赖）。

论文 SKILL.state 亦明示限制："多 Agent 并发写状态还需要确定性的冲突处理"。
本模块补齐秘书机制在多秘书协作场景的最后一块拼图：
- **归属声明**：每张状态卡 owner 为责任秘书坑位，claim/release 显式声明占用；
- **乐观并发仲裁**：写回须携带其"读到的 base_version"，若与最新不符(他人已改)，
  抛 StateConflict 拒绝覆盖——结果确定，不静默丢更新；
- **租约写锁**：可为某卡加临时写租约(持有者+过期)，防止两秘书同时改写；
- **原子 CAS 落盘**：版本校验 + 整卡写回合并为一次 flock 保护的原子提交，
  杜绝"校验与落盘之间的 TOCTOU 竞态"导致的静默覆盖；
- **隐式归属同步建租约**：首个写者自动成为 owner 的同时建立 lease，
  阻断"非 owner 秘书绕过越权检查改写他人卡"的旁路。
- 全程兼容既有 StateManager：仅新增受控写入口，不改变原有无并发参数调用路径。
"""
from __future__ import annotations

import json
import os
import tempfile
import time
from typing import List, Optional

from .models import StateCard, TaskRecord
from .manager import StateManager
from .lock_registry import KIND_CARD_WRITE


class StateConflict(Exception):
    """确定性并发冲突：同一张 StateCard 被并发秘书修改。

    携带冲突双方信息，供上层确定性处置（重读最新 / 回滚 / 人工裁决）。
    """

    def __init__(self, message: str, *, session_id: str = "",
                 expected_version: Optional[int] = None,
                 actual_version: Optional[int] = None,
                 current_owner: str = "", requester: str = ""):
        super().__init__(message)
        self.session_id = session_id
        self.expected_version = expected_version
        self.actual_version = actual_version
        self.current_owner = current_owner
        self.requester = requester


class OwnershipDenied(Exception):
    """归属越权：请求秘书非该卡 owner，且未获授权，拒绝写入。"""


class CardLease:
    """一张 StateCard 的临时写租约（占用声明）。"""

    def __init__(self, holder: str, ttl_sec: float = 300.0):
        self.holder = holder
        self.acquired_at = time.time()
        self.until = self.acquired_at + ttl_sec

    def is_expired(self) -> bool:
        return time.time() > self.until

    def to_dict(self) -> dict:
        return {
            "holder": self.holder,
            "acquired_at": self.acquired_at,
            "until": self.until,
        }

    @classmethod
    def from_dict(cls, data: dict) -> Optional["CardLease"]:
        if not isinstance(data, dict) or not data.get("holder"):
            return None
        try:
            lease = cls(holder=data["holder"])
            lease.acquired_at = float(data.get("acquired_at", lease.acquired_at))
            lease.until = float(data.get("until", lease.acquired_at + 300))
            return lease
        except (TypeError, ValueError):
            return None


class OwnershipGuard:
    """秘书并发写同一状态的确定性归属 + 冲突仲裁。

    用法（在秘书/执行 agent 侧）：
        guard = OwnershipGuard(session_id, state_manager, secretary="secre_a")
        guard.claim()                                  # 声明归属(占用卡)
        guard.guarded_add_task("写报告", base_version=guard.version())  # 携带读到的版本写
        guard.release()                                # 释放归属

    任一秘书（含非 owner）也可以只读；只有 owner(或获授权者)可写。
    """

    LOCK_SUFFIX = ".owner.json"

    # 卡片写租约在聚合台账里的锁键（"所有锁应该被聚合的管理"）
    REGISTRY_KEY_SUFFIX = ":card_write"

    def __init__(self, session_id: str, state_manager: StateManager,
                 secretary: str = "secretary",
                 lock_registry: Optional[object] = None):
        if not isinstance(session_id, str) or not session_id:
            raise ValueError("session_id 不能为空")
        self.session_id = session_id
        self.sm = state_manager
        self.secretary = secretary or "secretary"
        self._data_dir = state_manager.data_dir
        # 锁聚合台账（可选）：单锁文件仍是终态判据，台账只是镜像 + 聚合视图。
        # 台账读写异常**不得**影响单锁行为（宽进：锁机制强于台账机制）。
        self.lock_registry = lock_registry

    # ---- 只读 ----

    def get_card(self) -> Optional[StateCard]:
        return self.sm.get_card(self.session_id)

    def version(self) -> int:
        card = self.sm.get_card(self.session_id)
        return card.version if card else 0

    def current_owner(self) -> str:
        card = self.sm.get_card(self.session_id)
        return (card.owner or "") if card else ""

    # ---- 归属声明/释放 ----

    def claim(self, force: bool = False) -> str:
        """声明本秘书为该卡的 owner。
        force=True：强制抢占(覆盖原 owner)；默认若已被他人占用则拒绝。
        返回占用成功的秘书坑位名。

        注意：本方法内 lease 仅作**抢占前置检查**——若他人租约未过期则拒绝抢占；
        一旦通过（无租约/租约已过期/force），将直接覆盖 card.owner 为当前秘书
        并刷新租约。写权限的判定（_authorize）始终以 card.owner 为唯一权威。
        """
        # 获取排他锁保护整个 claim 流程
        lock_f = self.sm.lock_card(self.session_id)
        try:
            # 在锁内读取磁盘最新状态
            card = self.sm.read_card_disk(self.session_id)
            if card is None:
                raise ValueError(f"session 不存在，需先创建 StateCard: {self.session_id}")
            disk_version = card.version
            owner = card.owner or ""

            # 检查租约状态
            lease = self._load_lease()
            if lease and lease.holder != self.secretary and not lease.is_expired():
                if not force:
                    raise StateConflict(
                        f"卡 {self.session_id} 已被秘书 {lease.holder} 写租约占用，"
                        f"拒绝 {self.secretary} 抢占。可用 force=True 强制抢占或等待租约过期。",
                        session_id=self.session_id, current_owner=lease.holder,
                        requester=self.secretary)

            # 修改 owner 并写盘
            card.owner = self.secretary
            card.version += 1
            ok, _ = self.sm.cas_save_card(
                card, expected_version=disk_version, skip_lock=True)
            if not ok:
                raise StateConflict(
                    f"claim 版本冲突：卡 {self.session_id} 已被他人修改，请重试。",
                    session_id=self.session_id, requester=self.secretary)
            # 写租约
            self._store_lease(CardLease(self.secretary))
        finally:
            self.sm.release_lock(lock_f)
        return self.secretary

    def release(self) -> bool:
        """释放本秘书对卡的归属与写租约(仅 owner 或租约持有者可释放)。

        返回:
            True → 实际释放了（本秘书确实拥有该卡的所有权或租约）；
            False → 本秘书不是 owner/租约持有者，未做任何变更。
        """
        lock_f = self.sm.lock_card(self.session_id)
        try:
            card = self.sm.read_card_disk(self.session_id)
            if card is None:
                return False
            lease = self._load_lease()
            is_owner = card.owner == self.secretary
            is_lease_holder = lease is not None and lease.holder == self.secretary

            if not is_owner and not is_lease_holder:
                return False  # 非本秘书的卡，不释放

            disk_version = card.version

            if is_lease_holder:
                self._clear_lease()
            if is_owner:
                card.owner = ""
                card.version += 1
                ok, _ = self.sm.cas_save_card(
                    card, expected_version=disk_version, skip_lock=True)
                if not ok:
                    raise StateConflict(
                        f"release 版本冲突：卡 {self.session_id} 已被他人修改，请重试。",
                        session_id=self.session_id, requester=self.secretary)
            return True
        finally:
            self.sm.release_lock(lock_f)

    def is_owner(self) -> bool:
        return self.current_owner() == self.secretary

    # ---- 乐观并发受控写 ----

    def guarded_add_task(self, title: str, base_version: Optional[int] = None,
                         objective: str = "", cur_dir: str = "",
                         files: Optional[List[str]] = None,
                         depends_on: Optional[List[str]] = None,
                         tools: Optional[List[str]] = None,
                         task_type: str = "normal",
                         content_ref: Optional[str] = None,
                         **kwargs) -> TaskRecord:
        """归属 + 乐观版本受控的新增任务写（原子 CAS 落盘）。

        在 flock 排他锁内完成：授权检查 → 版本校验 → 卡修改 → 原子写回。
        """
        lock_f = self.sm.lock_card(self.session_id)
        try:
            card = self.sm.read_card_disk(self.session_id)
            if card is None:
                raise ValueError(f"session 不存在，需先创建 StateCard: {self.session_id}")
            disk_version = card.version

            # 写权限预检（含隐式归属）
            self._authorize(card)
            # 版本 CAS 检查（基于磁盘读取时的原始版本）
            if base_version is not None and base_version != disk_version:
                raise StateConflict(
                    f"乐观并发冲突：卡 {self.session_id} 版本已从 {base_version} 变为 {disk_version}，"
                    f"秘书 {self.secretary} 基于旧版本写入被拒，请重读最新后重试。",
                    session_id=self.session_id, expected_version=base_version,
                    actual_version=disk_version, requester=self.secretary)

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
            # CAS 落盘：以磁盘原始版本为基准
            ok, current_v = self.sm.cas_save_card(
                card, expected_version=disk_version, skip_lock=True)
            if not ok:
                raise StateConflict(
                    f"CAS 提交失败：卡 {self.session_id} 已被他人修改"
                    f"(磁盘版本 {current_v} ≠ 期望 {disk_version})。请重读重试。",
                    session_id=self.session_id, expected_version=disk_version,
                    actual_version=current_v, requester=self.secretary)
            return task
        finally:
            self.sm.release_lock(lock_f)

    def guarded_update_task_state(self, task_id: str, state: str,
                                  base_version: Optional[int] = None) -> bool:
        """归属 + 版本受控的任务状态更新（原子 CAS 落盘）。
        若任务不存在/迁移非法，抛 ValueError。
        """
        lock_f = self.sm.lock_card(self.session_id)
        try:
            card = self.sm.read_card_disk(self.session_id)
            if card is None:
                raise ValueError(f"session 不存在，需先创建 StateCard: {self.session_id}")
            disk_version = card.version

            self._authorize(card)
            if base_version is not None and base_version != disk_version:
                raise StateConflict(
                    f"乐观并发冲突：卡 {self.session_id} 版本已从 {base_version} "
                    f"变为 {disk_version}，秘书 {self.secretary} 写入被拒。",
                    session_id=self.session_id, expected_version=base_version,
                    actual_version=disk_version, requester=self.secretary)

            task = card.get_task(task_id)
            if task is None:
                raise ValueError(f"任务不存在: {task_id}")
            ok = card.update_task(task_id, state=state)
            if not ok:
                raise ValueError(
                    f"状态迁移非法: {task.state} → {state}（卡 {self.session_id} 任务 {task_id}）")
            cas_ok, current_v = self.sm.cas_save_card(
                card, expected_version=disk_version, skip_lock=True)
            if not cas_ok:
                raise StateConflict(
                    f"CAS 提交失败：卡 {self.session_id} 已被他人修改。请重读重试。",
                    session_id=self.session_id, expected_version=disk_version,
                    actual_version=current_v, requester=self.secretary)
            return True
        finally:
            self.sm.release_lock(lock_f)

    def guarded_update_task_fields(self, task_id: str, base_version: Optional[int] = None,
                                   **fields) -> bool:
        """归属 + 版本受控的任务字段更新（原子 CAS 落盘）。
        若任务不存在，抛 ValueError。
        """
        lock_f = self.sm.lock_card(self.session_id)
        try:
            card = self.sm.read_card_disk(self.session_id)
            if card is None:
                raise ValueError(f"session 不存在，需先创建 StateCard: {self.session_id}")
            disk_version = card.version

            self._authorize(card)
            if base_version is not None and base_version != disk_version:
                raise StateConflict(
                    f"乐观并发冲突：卡 {self.session_id} 版本已从 {base_version} "
                    f"变为 {disk_version}，秘书 {self.secretary} 写入被拒。",
                    session_id=self.session_id, expected_version=base_version,
                    actual_version=disk_version, requester=self.secretary)

            task = card.get_task(task_id)
            if task is None:
                raise ValueError(f"任务不存在: {task_id}")
            ok = card.update_task(task_id, **fields)
            if not ok:
                raise ValueError(f"任务字段更新失败: {task_id}")
            cas_ok, current_v = self.sm.cas_save_card(
                card, expected_version=disk_version, skip_lock=True)
            if not cas_ok:
                raise StateConflict(
                    f"CAS 提交失败：卡 {self.session_id} 已被他人修改。请重读重试。",
                    session_id=self.session_id, expected_version=disk_version,
                    actual_version=current_v, requester=self.secretary)
            return True
        finally:
            self.sm.release_lock(lock_f)

    # ---- 冲突仲裁（M9+ 版本化冲突仲裁与审核）----

    def arbitrate_conflict(self, base_version: Optional[int] = None,
                           proposed_card: Optional[StateCard] = None,
                           reason: str = "",
                           capture_base: Optional[StateCard] = None,
                           data_dir: Optional[str] = None) -> object:
        """版本冲突时生成 ReviewTicket，而非简单拒绝。

        参数:
        - proposed_card: 调用方尝试提交的 StateCard（应用了其修改后）
        - base_version: 调用方读到的版本号（可选）
        - capture_base: 调用方读取时的 StateCard 快照（可选，用于精确差异分析）
        - reason: 冲突原因描述
        - data_dir: 仲裁票据持久化目录（可选；若未指定，ConflictArbiter 将使用默认目录
        <cwd>/data/arbitration/）

        流程:
        1. 读取当前 StateCard（A 已提交后的版本）
        2. 若有 capture_base，则精确计算 A 与 B 的差异并检测重叠
        3. 若无 capture_base，标记冲突并生成待审核票据（不自动合并，避免误覆盖）
        4. 若 A 与 B 的修改确实不重叠，可自动合并

        返回: ReviewTicket
        """
        from .arbiter import ConflictArbiter

        current_card = self.sm.get_card(self.session_id)
        if current_card is None:
            raise StateConflict("卡不存在", session_id=self.session_id)

        if proposed_card is None:
            raise ValueError("需提供 proposed_card（冲突方尝试提交的版本）")

        arbiter = ConflictArbiter(state_manager=self.sm, data_dir=data_dir)

        # 若有精确的 base 快照 → 完整分析流程（自动合并或生成审核票）
        if capture_base is not None:
            ticket = arbiter.capture_conflict(
                session_id=self.session_id,
                base_card=capture_base,
                conflicting_card=proposed_card,
                current_card=current_card,
                arbiter_name=self.secretary,
                conflict_reason=reason or f"秘书 {self.secretary} 的写请求与当前状态冲突",
            )
            return ticket

        # 无 base 快照 → 无法判断是否精确重叠，保守生成待审核票据
        # （不自动合并，避免误覆盖他人的修改）
        from .arbiter import ReviewTicket as RT
        ticket = RT(
            session_id=self.session_id,
            version_a=current_card.version,
            version_b=proposed_card.version,
            card_a=current_card.to_dict(),
            card_b=proposed_card.to_dict(),
            base_card=current_card.to_dict(),
            arbiter=self.secretary,
            conflict_reason=reason or (
                f"版本冲突：agent 基于 v{base_version} 写入，当前 v{current_card.version}。"
                "无法确定是否与对方修改重叠，待人工/AI审核。"
            ),
            status="pending",
            auto_merge_blocked=True,  # 无 base 无法精确判断重叠 → 禁止 merge 自动合并
        )
        arbiter._save_ticket(ticket)
        return ticket

    def build_proposed(self, mutation_fn) -> StateCard:
        """在最新状态副本上应用修改，生成 proposed_card。
        用于仲裁流程：将修改应用到最新版本上，供仲裁分析。
        """
        current = self.sm.get_card(self.session_id)
        if current is None:
            raise StateConflict("卡不存在", session_id=self.session_id)
        proposed = StateCard.from_dict(current.to_dict())
        mutation_fn(proposed)
        return proposed

    def build_proposed_from_base(self, base_snapshot: StateCard,
                                 mutation_fn) -> StateCard:
        """在 base 快照上应用修改，生成 proposed_card。
        用于仲裁流程：将修改应用到 B 读取时的版本上，
        这样 proposed 只包含 B 自己的修改，不含 A 已提交的变更。

        参数:
        - base_snapshot: Agent B 读取时的 StateCard 快照
        - mutation_fn: B 要应用的修改函数，接收 StateCard 参数
        """
        proposed = StateCard.from_dict(base_snapshot.to_dict())
        mutation_fn(proposed)
        return proposed

    # ---- 内部 ----

    def _authorize(self, card: StateCard) -> None:
        """写权限预检（须在 flock 锁内调用，card 为磁盘最新读取的实例）。

        写权限仲裁以 **card.owner 字段为唯一权威**（owner 语义是责任秘书，
        长期有效直至显式 release 或被他方 claim 覆盖）。租约(.owner.json)
        仅作为 claim 的前置抢占检查，不参与写授权判定。

        规则：
        - 卡已被其他秘书 owner → 拒绝写入（OwnershipDenied）
        - 卡 owner 为空（尚未 claim）→ 首个写者隐式取得归属，
          并同步建立租约文件，阻断越权旁路
        - 卡 owner 是本秘书 → 允许写
        """
        card_owner = card.owner or ""
        if card_owner and card_owner != self.secretary:
            raise OwnershipDenied(
                f"秘书 {self.secretary} 无权写卡 {self.session_id}："
                f"owner={card_owner}。请先 claim 或等待 owner 释放。")

        # owner 为空 → 首个写者隐式取得归属（同步建租约）
        if not card_owner:
            card.owner = self.secretary
            self._store_lease(CardLease(self.secretary))

    # ---- 租约持久化到 data/state/<session>.owner.json ----

    def _lease_path(self) -> str:
        return os.path.join(self._data_dir, f"{self.session_id}{self.LOCK_SUFFIX}")

    def _load_lease(self) -> Optional[CardLease]:
        path = self._lease_path()
        if not os.path.exists(path):
            return None
        try:
            with open(path, "r", encoding="utf-8") as f:
                return CardLease.from_dict(json.load(f))
        except Exception:
            return None

    def registry_key(self) -> str:
        """本会话卡片写租约在聚合台账中的锁键。"""
        return f"{self.session_id}{self.REGISTRY_KEY_SUFFIX}"

    def _registry_call(self, action: str, **kwargs) -> None:
        """台账同步（失败静默：台账异常不得影响单锁语义）。"""
        reg = self.lock_registry
        if reg is None:
            return
        try:
            if action == "register":
                reg.register(
                    self.registry_key(), kind=KIND_CARD_WRITE,
                    holder=kwargs.get("holder", ""), scope="状态卡写入",
                    reason=kwargs.get("reason", ""))
            elif action == "release":
                reg.heartbeat(self.registry_key(), kwargs.get("holder", ""))
                reg.release(self.registry_key(), by=kwargs.get("by", ""),
                            reason=kwargs.get("reason", ""))
        except Exception:  # noqa: BLE001 - 台账是观测面，不参与写授权
            pass

    def _store_lease(self, lease: CardLease) -> None:
        # 原子写 lease 文件
        dir_path = os.path.dirname(os.path.abspath(self._lease_path()))
        os.makedirs(dir_path, exist_ok=True)
        fd, tmp_path = tempfile.mkstemp(dir=dir_path, suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(lease.to_dict(), f, ensure_ascii=False, indent=2)
            os.replace(tmp_path, self._lease_path())
        except Exception:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise
        # 台账镜像（聚合视图；写租约本体仍以 .owner.json 为准）
        self._registry_call("register", holder=lease.holder,
                            reason="卡片写租约（=正在编辑）")

    def _clear_lease(self) -> None:
        path = self._lease_path()
        if os.path.exists(path):
            try:
                os.remove(path)
            except OSError:
                pass
        # 台账同步释放（"别遗留锁一直不打开"）：过期/被清理的租约不再挂账
        self._registry_call("release", holder=self.secretary,
                            by=self.secretary, reason="写租约释放（编辑结束）")
