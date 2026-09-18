"""评审版本仲裁（ReviewArbiter）：只在开启评审时记录版本 + 防同写锁 + 延迟重试

## 需求（Issue #14 补充，用户口径）

> 补全机制，**只在开启评审时候记录版本**，过程中变化先不管，如果无法追踪变化就以
> 新的就行，或者**正在编辑的给他一个防止同写的锁**，**延迟一段时间再处理**，防止
> 出现冲突，这样即使真有冲突，量也不大，**先完成该做的之后，只额外处理一个解决
> 冲突就行**，不要让一个小冲突影响整体。

拆成四条可执行口径：

1. **只在开启评审时记录版本**：会话创建/开发过程中**不落**任何评审基线（`recorded=False`）；
   首次开启评审（`begin_review()`）时才把当前 StateCard 版本原子登记为基线。
2. **无法追踪变化就以新的**：基线与当前状态不可比（跨实例基线丢失 / 双方无公共祖先 /
   版本号退化）→ **不编造冲突**，直接以当前版本为新基线继续评审（`REBASE`）。
3. **正在编辑 → 防同写锁 + 延迟**：冲突带 `editing` 标记 + `retry_after` 秒的**延迟窗**，
   本次不落盘、不作废评审；过了延迟窗再重试（`retry()`），编辑自然结束即无冲突。
4. **小冲突不阻塞整体**：评审结果**先**落盘（不因冲突丢弃本次评审），冲突只排队登记成
   **至多一张**待处理冲突卡；同一会话后续冲突合并进同一张卡（`merged_conflicts` 计数），
   旧票据一并作废（`superseded_by`），避免"每轮冲突各开一张票"再次形成循环。

## 与既有 ConflictArbiter 的分工

`state.arbiter.ConflictArbiter` 是**写入方**冲突的通用仲裁（版本化保存 + 人工/AI 审核）；
本模块是**评审方**的轻量上界：不做全量 diff 分析，只回答三个问题——
「要不要等（延迟）」「能不能算新的（REBASE）」「是不是真的只值一张冲突卡（只处理一个）」。

## 调用顺序（关键：先完成该做的）

`apply_save()`（或 `apply()`）**无论是否冲突都先写回**评审状态；冲突只影响
「是否额外产生一张冲突卡」，不影响评审结果落盘。
"""
from __future__ import annotations

import json
import os
import re
import tempfile
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

from ..state.manager import StateManager
from ..state.models import StateCard

# 会话标识白名单（与 StateManager / LoopGuard / HandoffBus 同族口径，尾锚 \Z 防尾换行）
_SAFE_ID = re.compile(r'^[A-Za-z0-9_-]+\Z')

# 冲突卡 ID 前缀（仅 [A-Za-z0-9_-]，可直接参与路径拼接，无需再转义）
_CONFLICT_ID_PREFIX = "REVCONF"

# 冲突类别
NO_CONFLICT = "none"
STALE_BASE = "stale_base"        # 基线版本落后于磁盘（有他人写入）
VERSION_REGRESS = "version_regress"  # 当前版本低于登记基线（状态被回退/共享卡复用）
NO_BASELINE = "no_baseline"      # 库中查不到该会话基线（跨实例/未登记）→ 以新为准


@dataclass
class ReviewBaseline:
    """评审基线（**只在开启评审时**登记）。"""

    session_id: str
    version: int
    reviewer: str = ""
    started_at: str = ""
    source: str = "begin_review"   # begin_review | rebase
    # 基线快照轨迹（只保留**开启评审时**及退化重立时登记的版本，最多 2 条）：
    # 供"对方是否有效推进"判定拿"基线时刻的卡"做参照，避免拿"含对方变更的最新
    # 卡"自我比较导致恒判冲突（见 ``ReviewArbiter._is_progress`` docstring）。
    history: List[Dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "session_id": self.session_id,
            "version": self.version,
            "reviewer": self.reviewer,
            "started_at": self.started_at,
            "source": self.source,
            "history": [dict(h) for h in self.history],
        }

    def with_snapshot(self, card: Optional[StateCard]) -> "ReviewBaseline":
        """附加基线时刻的卡快照（``history`` 最多保留 2 条）。"""
        if card is None:
            return self
        self.history.append({
            "version": int(card.version or 0),
            "at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "card": card.to_dict(),
        })
        del self.history[:-2]
        return self

    @classmethod
    def from_dict(cls, data: Any, session_id: str = "") -> Optional["ReviewBaseline"]:
        """字段级容错：非 dict 返回 None；version 非整数 → 视为不可用（None）。"""
        if not isinstance(data, dict):
            return None
        sid = data.get("session_id", session_id)
        if not isinstance(sid, str) or not sid:
            sid = session_id
        ver = data.get("version", 0)
        if not isinstance(ver, int) or isinstance(ver, bool) or ver < 0:
            return None
        hist = data.get("history", [])
        if not isinstance(hist, list):
            hist = []
        clean_hist = []
        for h in hist:
            if not isinstance(h, dict):
                continue
            if not isinstance(h.get("card"), dict):
                continue
            clean_hist.append(h)
        return cls(
            session_id=sid,
            version=ver,
            reviewer=str(data.get("reviewer") or ""),
            started_at=str(data.get("started_at") or ""),
            source=str(data.get("source") or "begin_review"),
            history=clean_hist[-2:],
        )


@dataclass
class ConflictCard:
    """一张（且每会话至多一张）评审冲突卡：待人工/AI 单次收口。"""

    id: str
    session_id: str
    base_version: int
    current_version: int
    reason: str = ""
    kind: str = NO_CONFLICT
    detected_at: str = ""
    editing: bool = False          # 疑似"正在编辑"（有租约/延迟窗内）→ 先等
    retry_after: float = 0.0       # 重试时间戳（0 = 无需等待）
    status: str = "pending"        # pending | resolved | superseded
    merged_conflicts: int = 1      # 合并进本卡的冲突次数（>1 说明已折叠）
    superseded_by: Optional[str] = None
    resolution: str = ""           # resolved 时的处置说明
    history: List[Dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "session_id": self.session_id,
            "base_version": self.base_version,
            "current_version": self.current_version,
            "reason": self.reason,
            "kind": self.kind,
            "detected_at": self.detected_at,
            "editing": self.editing,
            "retry_after": self.retry_after,
            "status": self.status,
            "merged_conflicts": self.merged_conflicts,
            "superseded_by": self.superseded_by,
            "resolution": self.resolution,
            "history": [dict(h) for h in self.history],
        }

    @classmethod
    def from_dict(cls, data: Any) -> Optional["ConflictCard"]:
        """字段级容错：非 dict / 缺 id / 非法 id → None（宽进不炸）。"""
        if not isinstance(data, dict):
            return None
        cid = data.get("id")
        if not isinstance(cid, str) or not _SAFE_ID.match(cid):
            return None
        sid = data.get("session_id", "")
        if not isinstance(sid, str) or not _SAFE_ID.match(sid):
            return None

        def _int(key: str) -> int:
            v = data.get(key, 0)
            return v if isinstance(v, int) and not isinstance(v, bool) else 0

        def _str(key: str) -> str:
            v = data.get(key, "")
            return v if isinstance(v, str) else ""

        retry = data.get("retry_after", 0.0)
        if isinstance(retry, bool) or not isinstance(retry, (int, float)):
            retry = 0.0
        hist = data.get("history", [])
        if not isinstance(hist, list):
            hist = []
        sup = data.get("superseded_by")
        return cls(
            id=cid,
            session_id=sid,
            base_version=max(0, _int("base_version")),
            current_version=max(0, _int("current_version")),
            reason=_str("reason"),
            kind=_str("kind") or NO_CONFLICT,
            detected_at=_str("detected_at"),
            editing=bool(data.get("editing", False)),
            retry_after=float(retry),
            status=_str("status") or "pending",
            merged_conflicts=max(1, _int("merged_conflicts")),
            superseded_by=sup if isinstance(sup, str) and sup else None,
            resolution=_str("resolution"),
            history=[h for h in hist if isinstance(h, dict)],
        )


@dataclass
class ArbiterDecision:
    """一次评审写的处置结果（**先落盘**，冲突只决定要不要额外排队）。"""

    action: str                # save | retry | rebase | conflict
    applied: bool              # 评审结果是否已落盘
    conflict: bool             # 是否构成（真实）冲突
    kind: str = NO_CONFLICT
    should_retry: bool = False
    retry_after: float = 0.0
    retry_delay_sec: float = 0.0
    rebased: bool = False      # 是否"以新版本为准"重立基线
    card: Optional[ConflictCard] = None
    baseline: Optional[ReviewBaseline] = None
    reason: str = ""
    detail: str = ""

    def to_dict(self) -> dict:
        return {
            "action": self.action,
            "applied": self.applied,
            "conflict": self.conflict,
            "kind": self.kind,
            "should_retry": self.should_retry,
            "retry_after": self.retry_after,
            "retry_delay_sec": self.retry_delay_sec,
            "rebased": self.rebased,
            "card": self.card.to_dict() if self.card else None,
            "baseline": self.baseline.to_dict() if self.baseline else None,
            "reason": self.reason,
            "detail": self.detail,
        }


class ReviewArbiter:
    """评审版本仲裁：只在开启评审时记版本 + 防同写锁 + 延迟重试 + 单冲突收口。

    方向约定：**评审方**在同一张状态卡上写入「评审状态」（如把任务从
    `in_progress` 置为 `blocked`），**开发方**写入修复产出。同向写入（都是评审）
    视为可重试的时序冲突（延迟后再处理）；反向写入（他人已推进）视为真冲突
    （只登记一张冲突卡，不阻塞评审落盘）。

    用法::

        arb = ReviewArbiter(session_id="issue-14", state_manager=sm,
                            reviewer="secretary.review", data_dir=...)
        arb.begin_review()                       # ① 只在开启评审时记录版本
        d = arb.apply_save(mutate=fn)            # ② 先完成该做的（评审落盘）
        if d.should_retry:
            time.sleep(d.retry_delay_sec); d = arb.retry(mutate=fn)
        if d.conflict:
            ...  # ③ 只额外处理一个：d.card 即唯一冲突卡（后续冲突会合并进它）
    """

    def __init__(
        self,
        session_id: str,
        state_manager: StateManager,
        reviewer: str = "reviewer",
        data_dir: Optional[str] = None,
        retry_delay_sec: float = 30.0,
        clock: Optional[Callable[[], float]] = None,
    ):
        """
        Args:
            session_id: 会话 ID（须为 ``[A-Za-z0-9_-]+``）
            state_manager: 状态卡管理器（写回与版本读数均以磁盘为准）
            reviewer: 评审方标识（用于"同向可重试 / 反向算冲突"判定与留痕）
            data_dir: 仲裁数据目录（默认 ``<state 数据根>/../review``，
                即与会话数据同根，随会话生命周期一并回收）
            retry_delay_sec: 疑似"正在编辑"时的延迟窗（默认 30s），窗内不落盘、先等
            clock: 时间源（默认 ``time.time``，测试可注入）
        """
        if not isinstance(session_id, str) or not _SAFE_ID.match(session_id):
            raise ValueError(
                f"非法 session_id: {session_id!r}。仅允许 [A-Za-z0-9_-]。")
        if state_manager is None or not hasattr(state_manager, "cas_save_card"):
            raise ValueError("state_manager 须为 StateManager 实例（提供 cas_save_card）。")
        if not isinstance(retry_delay_sec, (int, float)) \
                or isinstance(retry_delay_sec, bool) or retry_delay_sec < 0:
            raise ValueError(
                f"非法 retry_delay_sec: {retry_delay_sec!r}（须为非负数）。")
        if not isinstance(reviewer, str) or not reviewer.strip():
            raise ValueError("reviewer 不可为空（评审留痕需要）。")

        self.session_id = session_id
        self.sm = state_manager
        self.reviewer = reviewer.strip()
        self.retry_delay_sec = float(retry_delay_sec)
        self._clock = clock or time.time
        self._state_dir = os.path.dirname(
            os.path.abspath(state_manager.data_dir.rstrip(os.sep))) or os.getcwd()
        self.data_dir = data_dir or os.path.join(self._state_dir, "review")
        os.makedirs(self.data_dir, exist_ok=True)

    # ------------------------------------------------------------------
    # 路径与落盘
    # ------------------------------------------------------------------

    def baseline_path(self) -> str:
        """基线文件路径（每会话一个）。"""
        return os.path.join(self.data_dir, f"{self.session_id}.baseline.json")

    def conflict_path(self) -> str:
        """冲突卡路径（每会话**至多一张**）。"""
        return os.path.join(self.data_dir, f"{self.session_id}.conflict.json")

    def card_path(self, ticket_id: str) -> str:
        """按票据 ID 定位冲突卡（须为合法标识符，防路径穿越）。"""
        if not isinstance(ticket_id, str) or not _SAFE_ID.match(ticket_id):
            raise ValueError(f"非法票据 ID: {ticket_id!r}。仅允许 [A-Za-z0-9_-]。")
        return os.path.join(self.data_dir, f"{ticket_id}.conflict.json")

    def _atomic_write(self, path: str, data: dict) -> None:
        fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), prefix=".tmp-",
                                   suffix=".json")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            os.replace(tmp, path)
        except Exception:
            try:
                os.remove(tmp)
            except OSError:
                pass
            raise

    def _read_json(self, path: str) -> Any:
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except (OSError, ValueError):
            return None

    # ------------------------------------------------------------------
    # ① 只在开启评审时记录版本
    # ------------------------------------------------------------------

    def recorded(self) -> bool:
        """是否**已**在开启评审时登记过版本（开发过程中恒为 False）。"""
        return os.path.exists(self.baseline_path())

    def get_baseline(self) -> Optional[ReviewBaseline]:
        """读回评审基线（未开启评审 / 文件损坏 → None）。"""
        data = self._read_json(self.baseline_path())
        return ReviewBaseline.from_dict(data, self.session_id)

    def begin_review(self, force: bool = False) -> ReviewBaseline:
        """开启评审：把**当前磁盘版本**原子登记为基线（幂等）。

        幂等语义（"只在开启评审时候记录版本"）：已登记且 ``force=False`` 时
        **不改写**基线——评审过程中开发者又提交了修改，不能靠重新 `begin_review`
        把基线偷偷推前（那等于"过程中变化也记版本"）。要重立基线须显式
        ``force=True``（或在冲突判定中走 ``REBASE``：以新版本为准）。

        Raises:
            ValueError: 会话 StateCard 不存在。
        """
        if self.recorded() and not force:
            existing = self.get_baseline()
            if existing is not None:
                return existing
        card = self.sm.read_card_disk(self.session_id)
        if card is None:
            raise ValueError(f"session 不存在，无法开启评审: {self.session_id}")
        base = ReviewBaseline(
            session_id=self.session_id,
            version=int(card.version or 0),
            reviewer=self.reviewer,
            started_at=time.strftime("%Y-%m-%d %H:%M:%S"),
            source="begin_review",
        ).with_snapshot(card)
        self._atomic_write(self.baseline_path(), base.to_dict())
        return base

    def rebase(self, current_card: Optional[StateCard] = None) -> ReviewBaseline:
        """以**当前版本为新基线**（"无法追踪变化就以新的"）。"""
        existing = self.get_baseline()
        card = current_card or self.sm.read_card_disk(self.session_id)
        if card is None:
            raise ValueError(f"session 不存在: {self.session_id}")
        # 退化重立（rebase）沿用同一份快照轨迹：保留更早的"开启评审"快照
        base = ReviewBaseline(
            session_id=self.session_id,
            version=int(card.version or 0),
            reviewer=self.reviewer,
            started_at=time.strftime("%Y-%m-%d %H:%M:%S"),
            source="rebase",
            history=list(existing.history) if existing is not None else [],
        ).with_snapshot(card)
        self._atomic_write(self.baseline_path(), base.to_dict())
        return base

    # ------------------------------------------------------------------
    # ② 评审写回：先完成该做的，再做冲突判定
    # ------------------------------------------------------------------

    def apply_save(
        self,
        mutate: Optional[Callable[[StateCard], None]] = None,
        proposed: Optional[StateCard] = None,
        note: str = "",
    ) -> ArbiterDecision:
        """把评审结果**先**写回，再单独判冲突（关键顺序）。

        Args:
            mutate: 在**磁盘最新卡**上应用的评审修改（推荐：天然以最新版本为基，
                小量变更即使与开发方并发也不丢）
            proposed: 直接给出的目标卡（与 ``mutate`` 二选一；缺失时直接拒绝）
            note: 留痕说明

        Returns:
            ``ArbiterDecision``：``applied=True`` 表示评审结果已落盘
            （**冲突不阻塞落盘**）；``should_retry=True`` 表示疑似"正在编辑"，
            应等 ``retry_delay_sec`` 后调 ``retry()``；``conflict=True`` 时
            ``card`` 为**唯一**冲突卡（后续冲突会合并进它）。
        """
        if mutate is None and proposed is not None:
            def mutate(card: StateCard) -> None:  # noqa: E306
                target = proposed.to_dict()
                for k in ("tasks", "owner", "objective", "upstream",
                          "downstream", "updated_at"):
                    if k in target:
                        setattr(card, k, target[k])
        if mutate is not None and not callable(mutate):
            raise ValueError(f"非法 mutate: {mutate!r}（须为可调用对象或 None）。")

        baseline = self.get_baseline()
        disk_before = self.sm.read_card_disk(self.session_id)

        # 情形 A：尚未开启评审（无基线）→ 不因评审而阻塞，落盘 + 以新为准重立基线
        if baseline is None or disk_before is None:
            applied = self._do_save(mutate)
            new_base = self.rebase() if applied else None
            return ArbiterDecision(
                action="rebase", applied=applied, conflict=False,
                kind=NO_BASELINE, rebased=bool(new_base),
                baseline=new_base,
                reason="库中无评审基线（未开启评审/跨实例）→ 直接以当前版本为准",
                detail=note,
            )

        # 情形 B：疑似"正在编辑"（存在他人租约）→ 给防同写锁 + 延迟，稍后再处理
        editing = self._editing()
        if editing:
            after = self._clock() + self.retry_delay_sec
            return ArbiterDecision(
                action="retry", applied=False, conflict=False,
                should_retry=True, retry_after=after,
                retry_delay_sec=self.retry_delay_sec,
                baseline=baseline, reason="卡正在被编辑（存在写租约）→ 延迟后重试",
                detail=note,
            )

        # 情形 C：版本退化（当前版本低于基线）→ 不可追踪变化，以新为准
        if int(disk_before.version or 0) < baseline.version:
            applied = self._do_save(mutate)
            new_base = self.rebase() if applied else None
            return ArbiterDecision(
                action="rebase", applied=applied, conflict=False,
                kind=VERSION_REGRESS, rebased=bool(new_base),
                baseline=new_base or baseline,
                reason=(f"当前版本 v{disk_before.version} < 基线 v{baseline.version}"
                        "（状态被回退/共享卡复用）→ 以新为准"),
                detail=note,
            )

        # 情形 D：同向已推进（他人也是评审、写完仍是同一侧）→ 时序冲突，延迟重试
        if int(disk_before.version or 0) > baseline.version:
            other = self._owner_of(disk_before)
            if other and other != self.reviewer and self._same_side(other):
                after = self._clock() + self.retry_delay_sec
                return ArbiterDecision(
                    action="retry", applied=False, conflict=False,
                    should_retry=True, retry_after=after,
                    retry_delay_sec=self.retry_delay_sec,
                    baseline=baseline,
                    reason=(f"同向写入（{other}）已推进 v{disk_before.version}"
                            "→ 可重试的时序冲突，延迟后重试"),
                    detail=note,
                )

        # 情形 E：常规路径——以最新版本为基落盘（先完成该做的）
        applied = self._do_save(mutate)
        if not applied:
            return ArbiterDecision(
                action="save", applied=False, conflict=True,
                kind=STALE_BASE, baseline=baseline,
                reason="落盘失败：会话状态卡不存在",
                detail=note,
            )

        disk_after = self.sm.read_card_disk(self.session_id)
        version_after = int(disk_after.version or 0) if disk_after else 0
        if not self._is_conflict(disk_before, disk_after, baseline, note):
            # 评审轮推进了版本 → 基线随之前移（仍只在开启评审时登记）
            self._advance_baseline(version_after)
            return ArbiterDecision(
                action="save", applied=True, conflict=False,
                baseline=self.get_baseline(),
                reason="评审已落盘，无冲突",
                detail=note,
            )

        # 情形 F：真冲突——评审结果**已落盘**，只额外登记**一张**冲突卡
        card = self._enqueue_conflict(disk_before, disk_after, baseline, note)
        return ArbiterDecision(
            action="conflict", applied=True, conflict=True,
            kind=STALE_BASE, card=card, baseline=baseline,
            reason=card.reason, detail=note,
        )

    def retry(
        self,
        mutate: Optional[Callable[[StateCard], None]] = None,
        proposed: Optional[StateCard] = None,
        note: str = "",
    ) -> ArbiterDecision:
        """过了延迟窗后的重试（延迟窗未到则继续 `should_retry`，不落盘）。"""
        card = self.get_conflict()
        if card is not None and card.status == "pending" \
                and card.retry_after > self._clock():
            return ArbiterDecision(
                action="retry", applied=False, conflict=False,
                should_retry=True, retry_after=card.retry_after,
                retry_delay_sec=self.retry_delay_sec, card=card,
                reason="延迟窗未到，继续等待编辑结束",
                detail=note,
            )
        decision = self.apply_save(mutate=mutate, proposed=proposed, note=note)
        # 重试成功且无冲突 → 之前的冲突卡作废（同一冲突不重复占用处理位）
        if decision.applied and not decision.conflict:
            self.supersede_pending()
        return decision

    def apply(
        self,
        mutate: Optional[Callable[[StateCard], None]] = None,
        proposed: Optional[StateCard] = None,
        note: str = "",
    ) -> ArbiterDecision:
        """`apply_save` 的便捷别名（同步语义）。"""
        return self.apply_save(mutate=mutate, proposed=proposed, note=note)

    # ------------------------------------------------------------------
    # ③ 冲突卡：至多一张，后续冲突合并进同一张
    # ------------------------------------------------------------------

    def get_conflict(self) -> Optional[ConflictCard]:
        """读回本会话的冲突卡（无/损坏 → None）。"""
        return ConflictCard.from_dict(self._read_json(self.conflict_path()))

    def pending_conflicts(self) -> List[ConflictCard]:
        """待处理冲突卡清单（**长度至多 1**：单冲突收口）。"""
        card = self.get_conflict()
        if card is None or card.status != "pending":
            return []
        return [card]

    def resolve(
        self,
        ticket_id: str,
        resolution: str,
        by: str = "",
        rebase_baseline: bool = True,
    ) -> bool:
        """人工/AI 收口那张冲突卡（**只额外处理一个**）。

        收口后基线以当前版本重立，后续评审从新版本继续——不因一次冲突重开全量。
        非法/不存在/非待处理的票据 → ``False``（幂等，不炸）。
        """
        card = self.get_conflict()
        if card is None or card.id != ticket_id or card.status != "pending":
            return False
        if not isinstance(resolution, str) or not resolution.strip():
            raise ValueError("resolve 须提供非空 resolution（收口留痕）。")
        card.status = "resolved"
        card.resolution = resolution.strip()
        card.history.append({
            "at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "by": (by or "unknown").strip(),
            "action": "resolve",
            "resolution": card.resolution,
        })
        self._atomic_write(self.conflict_path(), card.to_dict())
        if rebase_baseline:
            try:
                self.rebase()
            except ValueError:
                pass
        return True

    def supersede_pending(self, by: str = "") -> int:
        """作废当前待处理冲突卡（冲突已自行消解/已有更新票据接管）。

        Returns:
            被作废的卡数（0 或 1）。
        """
        card = self.get_conflict()
        if card is None or card.status != "pending":
            return 0
        card.status = "superseded"
        card.history.append({
            "at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "by": (by or "auto").strip(),
            "action": "supersede",
            "resolution": "冲突已消解（重试成功/已被新票据接管）",
        })
        self._atomic_write(self.conflict_path(), card.to_dict())
        return 1

    def _enqueue_conflict(
        self,
        disk_before: StateCard,
        disk_after: Optional[StateCard],
        baseline: ReviewBaseline,
        note: str,
    ) -> ConflictCard:
        """登记冲突：已有待处理卡 → **合并进同一张**（不新开票）。"""
        current_version = int(disk_after.version or 0) if disk_after else 0
        existing = self.get_conflict()
        if existing is not None and existing.status == "pending":
            existing.merged_conflicts += 1
            existing.current_version = current_version
            existing.reason = (
                f"累计 {existing.merged_conflicts} 次冲突已合并至本卡（只额外处理一个）"
            )
            existing.retry_after = 0.0
            existing.history.append({
                "at": time.strftime("%Y-%m-%d %H:%M:%S"),
                "by": self.reviewer,
                "action": "merge",
                "note": note,
                "current_version": current_version,
            })
            self._atomic_write(self.conflict_path(), existing.to_dict())
            return existing

        # 新开一张：先把旧的非 pending 卡（resolved/superseded）标记为被接管
        if existing is not None:
            existing.status = "superseded"
            existing.history.append({
                "at": time.strftime("%Y-%m-%d %H:%M:%S"),
                "by": self.reviewer,
                "action": "superseded_by_new",
            })
            self._atomic_write(self.conflict_path(), existing.to_dict())

        ts = int(self._clock() * 1000)
        card = ConflictCard(
            id=f"{_CONFLICT_ID_PREFIX}-{self.session_id}-{ts}",
            session_id=self.session_id,
            base_version=baseline.version,
            current_version=current_version,
            kind=STALE_BASE,
            reason=(
                f"基线 v{baseline.version} ≠ 当前 v{current_version}"
                "（评审期间状态卡被并发写入）→ 已先落盘本次评审，冲突只登记一张卡"
            ),
            detected_at=time.strftime("%Y-%m-%d %H:%M:%S"),
            history=[{
                "at": time.strftime("%Y-%m-%d %H:%M:%S"),
                "by": self.reviewer,
                "action": "create",
                "note": note,
                "base_version": baseline.version,
                "current_version": current_version,
                "disk_version": int(disk_before.version or 0),
            }],
        )
        self._atomic_write(self.conflict_path(), card.to_dict())
        return card

    # ------------------------------------------------------------------
    # 冲突判定（宽进：判不出就"以新为准"，不编造冲突）
    # ------------------------------------------------------------------

    def _is_conflict(
        self,
        disk_before: Optional[StateCard],
        disk_after: Optional[StateCard],
        baseline: ReviewBaseline,
        note: str,
    ) -> bool:
        """本次评审是否构成冲突。

        判定链（任一不满足即视为"可追踪、无冲突"，交由"以新为准"推进）：

        1. **无法追踪变化 → 不算冲突**：基线/前后快照缺失、无清晰共同祖先
           （双方版本都低于基线，比如基线来自另一实例的更高版本）→ 以新为准；
        2. **写者足够清晰**：拿到写者标识（租约或卡 owner）才继续判方向；
        3. **同向且带过渡态 → 不算冲突**：对方与本次评审写同侧（评审状态），
           且本次写入落在一个"过渡态"上（未越过门禁完成态）→ 可重试时序，不算冲突；
        4. **有效推进 → 不算冲突**：对方把任务推进（引入新的任务/完成态）超过基线；
        5. 其余 → 冲突（登记唯一冲突卡）。
        """
        base_version = int(baseline.version or 0)
        before_v = int(disk_before.version or 0)
        after_v = int(disk_after.version or 0)

        # 1) 无法追踪变化 → 以新为准，不编造冲突
        if before_v < base_version and after_v < base_version:
            return False

        # 2) 写者不明/就是自己 → 不判冲突
        other = self._owner_of(disk_before)
        if not other or other == self.reviewer:
            return False

        # 3) 同向写入且未越过门禁完成态 → 可重试的时序冲突，不算冲突
        if self._same_side(other) and not self._crossed_finish(disk_after):
            return False

        # 4) 对方有效推进（新增任务 / 抵达完成态）→ 不阻断
        if self._is_progress(disk_before, disk_after, baseline, self.sm):
            return False

        return True

    def _crossed_finish(self, card: Optional[StateCard]) -> bool:
        """本次写入是否越过门禁完成态（存在 done 任务）——过渡态不算越界。"""
        if card is None:
            return False
        return any(getattr(t, "state", "") == "done" for t in card.tasks)

    @staticmethod
    def _is_progress(before: Optional[StateCard],
                     after: Optional[StateCard],
                     baseline: Optional[ReviewBaseline] = None,
                     state_manager: Optional[StateManager] = None) -> bool:
        """对方是否**有效推进**（自评审基线以来）。

        **参照物为什么是基线而不是"写回前一瞬"**：``apply_save`` 里的
        ``disk_before`` 是**开始评审写回时**的最新卡——对方的并发写入早已落在
        其中。拿它与"写回后"比较，等于拿「含对方变更的卡」比自己（写回只把版本
        +1、任务集不变），结果恒为"无进展"→ 每次并发都误判冲突。故参照物取
        **评审开启时登记的基线**（``ReviewBaseline``），与当前磁盘卡比较：

        - 任务数增加 → 对方/己方引入了新任务（推进）；
        - 出现基线中不存在的完成态（done）→ 有实质结论（推进）。

        只看门禁语义（任务数 / 完成态），不看结论措辞；快照不可得（缺基线、
        读不到卡、坏文件）→ 视为"有进展"（宽进：不编造冲突，以新为准）。
        """
        if before is None or after is None:
            return True
        if len(after.tasks) > len(before.tasks):
            return True
        before_done = {t.task_id for t in before.tasks
                       if getattr(t, "state", "") == "done"}
        after_done = {t.task_id for t in after.tasks
                      if getattr(t, "state", "") == "done"}
        if after_done - before_done:
            return True

        # 兜底：以评审基线快照为参照（见 docstring 的「参照物」说明）
        base_card = ReviewArbiter._baseline_snapshot(baseline, state_manager)
        if base_card is None:
            return True
        if len(after.tasks) > len(base_card.tasks):
            return True
        base_done = {t.task_id for t in base_card.tasks
                     if getattr(t, "state", "") == "done"}
        return bool(after_done - base_done)

    @staticmethod
    def _baseline_snapshot(
        baseline: Optional[ReviewBaseline],
        state_manager: Optional[StateManager],
    ) -> Optional[StateCard]:
        """基线时刻的卡快照：从 ``history`` 里取"版本 ≤ 基线版本"的最后一条。

        返回 None 表示快照不可得（无基线/无 history/类型不符）——调用方据此
        走"宽进"分支（视为有进展），不编造冲突。
        """
        if baseline is None or state_manager is None:
            return None
        hist = baseline.history
        if not isinstance(hist, list):
            return None
        best = None
        for entry in hist:
            if not isinstance(entry, dict):
                continue
            ver = entry.get("version")
            snap = entry.get("card")
            if not isinstance(ver, int) or isinstance(ver, bool):
                continue
            if not isinstance(snap, dict):
                continue
            if ver > baseline.version:
                continue
            if best is None or ver >= best[0]:
                best = (ver, snap)
        if best is None:
            return None
        try:
            return StateCard.from_dict(best[1])
        except Exception:  # noqa: BLE001 - 坏快照不得拖垮判定
            return None

    def _same_side(self, writer: str) -> bool:
        """写者与评审方是否同侧（命名约定 / 类别判定；判不出 → False）。"""
        try:
            from .rules import is_same_side
            return is_same_side(writer, self.reviewer)
        except Exception:  # noqa: BLE001 - 判定规则异常不得影响主流程
            return False

    def _owner_of(self, card: Optional[StateCard]) -> str:
        """卡的当前写者标识：优先租约持有者，其次卡 owner 字段（判不出 → ""）。"""
        if card is None:
            return ""
        try:
            from ..state.ownership import OwnershipGuard
            lease = OwnershipGuard(
                self.session_id, self.sm, secretary=self.reviewer)._load_lease()
            if lease is not None and not lease.is_expired():
                return str(lease.holder or "")
        except Exception:  # noqa: BLE001
            pass
        return str(getattr(card, "owner", "") or "")

    def _editing(self) -> bool:
        """是否有**他人**持有效写租约（"正在编辑"→ 先给防同写锁 + 延迟）。"""
        try:
            from ..state.ownership import OwnershipGuard
            lease = OwnershipGuard(
                self.session_id, self.sm, secretary=self.reviewer)._load_lease()
        except Exception:  # noqa: BLE001
            return False
        if lease is None or lease.is_expired():
            return False
        return str(lease.holder or "") != self.reviewer

    # ------------------------------------------------------------------
    # 内部写盘
    # ------------------------------------------------------------------

    def _do_save(self, mutate: Optional[Callable[[StateCard], None]]) -> bool:
        """在最新磁盘卡上应用评审修改并 CAS 落盘（失败返回 False，不抛）。"""
        lock_f = None
        try:
            lock_f = self.sm.lock_card(self.session_id)
            card = self.sm.read_card_disk(self.session_id)
            if card is None:
                return False
            version = int(card.version or 0)
            if mutate is not None:
                mutate(card)
            card.updated_at = time.strftime("%Y-%m-%d %H:%M:%S")
            card.version = version + 1
            ok, _ = self.sm.cas_save_card(
                card, expected_version=version, skip_lock=True)
            return bool(ok)
        except (OSError, ValueError):
            return False
        finally:
            if lock_f is not None:
                self.sm.release_lock(lock_f)

    def _advance_baseline(self, version: int) -> None:
        """评审轮推进 → 基线随之前移（仍属"开启评审时登记的版本"演进）。

        注意：**只在"无冲突"路径推进**，快照同步刷新，避免下一轮把上一轮的
        并发变更误算成本轮"对方推进"。
        """
        existing = self.get_baseline()
        card = self.sm.read_card_disk(self.session_id)
        base = ReviewBaseline(
            session_id=self.session_id,
            version=version,
            reviewer=self.reviewer,
            started_at=time.strftime("%Y-%m-%d %H:%M:%S"),
            source="begin_review",
            history=list(existing.history) if existing is not None else [],
        ).with_snapshot(card)
        self._atomic_write(self.baseline_path(), base.to_dict())

    def delete_state(self) -> bool:
        """会话生命周期回收：删除基线与冲突卡（同 id 复建不带陈旧状态）。"""
        removed = False
        for path in (self.baseline_path(), self.conflict_path()):
            try:
                if os.path.exists(path):
                    os.remove(path)
                    removed = True
            except OSError:
                pass
        return removed
