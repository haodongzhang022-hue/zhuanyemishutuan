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

from ..collab.backoff import StepBackoff
from ..state.lock_registry import (
    KIND_DEFER,
    KIND_REVIEW,
    LOCK_HELD,
    LegacyLockScanner,
    LockRegistry,
)
from ..state.manager import StateManager
from ..state.models import StateCard

# 会话标识白名单（与 StateManager / LoopGuard / HandoffBus 同族口径，尾锚 \Z 防尾换行）
_SAFE_ID = re.compile(r'^[A-Za-z0-9_-]+\Z')

# 冲突卡 ID 前缀（仅 [A-Za-z0-9_-]，可直接参与路径拼接，无需再转义）
_CONFLICT_ID_PREFIX = "REVCONF"

# 延迟窗：首次触发「正在编辑」→ **阶梯第 1 档**再检测（不用立刻抢写）。
# 阶梯（Issue #14 追问）：10s → 30s → 60s → 3min → 6min → 10min 到顶即停；
# 前两档刻意很短（"大部分很快就没问题"），后段拉长收敛长尾。
# 本常量保留为**兼容默认**（会话未装载规则包时的固定延迟窗）。
DEFAULT_RETRY_DELAY_SEC = 60.0
# 「到顶即停」的上界 = 阶梯顶（10 分钟）：重触发的等待**不再超过它**
DEFAULT_MAX_WAIT_SEC = 600.0
# 再次触发（第 2 次及以后）→ 升级为**持续锁**；等待档位继续沿阶梯上行
# （3 分钟是阶梯中的一档），一直持续到对方处理完为止（不自动打开）
DEFAULT_PERSISTENT_GRACE_SEC = 180.0

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
    deferred: bool = False         # 「先做其他，并挂锁」：评审已先做完，锁挂着待检测
    deferred_persistent: bool = False  # 锁已升级为持续锁（持续到对方处理完）
    retriggered: int = 0           # 重触发次数（>=1 → 锁升级为持续锁）
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
            "deferred": self.deferred,
            "deferred_persistent": self.deferred_persistent,
            "retriggered": self.retriggered,
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
            deferred=bool(data.get("deferred", False)),
            deferred_persistent=bool(data.get("deferred_persistent", False)),
            retriggered=max(0, _int("retriggered")),
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
    deferred: bool = False     # 「先做其他，并挂锁」：已把本次评审做完，锁挂着待检测
    lock: Optional[dict] = None  # 本次涉及锁的聚合台账快照（触发即可查看）
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
            "deferred": self.deferred,
            "lock": dict(self.lock) if self.lock else None,
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
        retry_delay_sec: float = DEFAULT_RETRY_DELAY_SEC,
        clock: Optional[Callable[[], float]] = None,
        lock_registry: Optional[LockRegistry] = None,
        persistent_grace_sec: float = DEFAULT_PERSISTENT_GRACE_SEC,
        backoff: Optional[StepBackoff] = None,
    ):
        """
        Args:
            session_id: 会话 ID（须为 ``[A-Za-z0-9_-]+``）
            state_manager: 状态卡管理器（写回与版本读数均以磁盘为准）
            reviewer: 评审方标识（用于"同向可重试 / 反向算冲突"判定与留痕）
            data_dir: 仲裁数据目录（默认 ``<state 数据根>/../review``，
                即与会话数据同根，随会话生命周期一并回收）
            retry_delay_sec: 疑似"正在编辑"时的延迟窗（默认 **60s**），
                窗内不落盘、先把其他做完；**再次触发**则该锁升级为持续锁
            clock: 时间源（默认 ``time.time``，测试可注入）
            lock_registry: 锁聚合台账（默认在 ``<state 数据根>/locks/`` 自建）。
                **所有锁统一登记**：评审读锁 / 延迟锁 / 卡片写租约都在一本台账里，
                触发即可 `locks_overview()` 一眼看清"哪里要避开"
            persistent_grace_sec: 重触发后持续锁的宽限（默认 **3 分钟**），
                持续到对方处理完（不自动打开）
            backoff: **阶梯退避**（Issue #14 追问）。给定后，延迟后检测的等待
                时长走阶梯：``10s → 30s → 60s → 3min → 6min → 10min``，
                **到顶即停**（不再涨、不再重挂）；未给出则退化为固定
                ``retry_delay_sec``（旧语义，向后兼容）。
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
        if not isinstance(persistent_grace_sec, (int, float)) \
                or isinstance(persistent_grace_sec, bool) or persistent_grace_sec < 0:
            raise ValueError(
                f"非法 persistent_grace_sec: {persistent_grace_sec!r}（须为非负数）。")
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
        self.persistent_grace_sec = float(persistent_grace_sec)
        # 阶梯退避（Issue #14 追问）：等待从少到多、到顶（10 分钟）即停
        self.backoff = backoff
        # 锁聚合台账（"所有锁应该被聚合的管理"）：与会话数据同根，随会话回收
        self.lock_registry = lock_registry or LockRegistry(
            session_id=session_id,
            data_dir=os.path.join(self._state_dir, "locks"),
            clock=self._clock,
            ttl_sec=self.retry_delay_sec,
            persistent_grace_sec=self.persistent_grace_sec,
            backoff=backoff,
        )

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

    # ---- 锁键 / 台账留痕 ----

    def lock_key(self, purpose: str) -> str:
        """聚合台账里的锁键（``<session>:<用途>``；用途仅 [A-Za-z0-9_-]）。"""
        if not isinstance(purpose, str) or not re.match(r'^[A-Za-z0-9_-]+\Z', purpose):
            raise ValueError(f"非法锁用途: {purpose!r}（仅允许 [A-Za-z0-9_-]）。")
        return f"{self.session_id}:{purpose}"

    def review_lock_key(self) -> str:
        """评审读锁键（**保证有基线才评审**）。"""
        return self.lock_key("review")

    def defer_lock_key(self) -> str:
        """延迟锁键（"正在编辑"→ 先做其他，并挂锁）。"""
        return self.lock_key("review_defer")

    def locks_overview(self) -> dict:
        """本会话**全部锁**的聚合总览（触发了就去看一眼情况）。"""
        try:
            return self.lock_registry.overview()
        except Exception:  # noqa: BLE001 - 观测面异常不得影响评审
            return {"session_id": self.session_id, "count": 0, "held": [],
                    "where_to_avoid": [], "by_kind": {}, "stale_candidates": []}

    def _registry_register(self, key: str, kind: str, holder: str,
                           scope: str, reason: str = "",
                           persist: bool = False,
                           retry_delay_sec: float = 0.0,
                           increment: bool = False,
                           delay_step: Optional[int] = None) -> Optional[dict]:
        """台账登记（失败静默：台账是观测面，不得阻塞评审落盘）。"""
        try:
            rec = self.lock_registry.register(
                key, kind=kind, holder=holder, scope=scope,
                persist=persist, retry_delay_sec=retry_delay_sec,
                increment=increment, reason=reason, delay_step=delay_step)
            return rec.to_dict()
        except Exception:  # noqa: BLE001
            return None

    def _registry_release(self, key: str, by: str = "",
                          reason: str = "") -> None:
        try:
            self.lock_registry.release(key, by=by, reason=reason)
        except Exception:  # noqa: BLE001
            pass

    def _legacy_locks(self) -> List[Dict[str, Any]]:
        """历史遗留单锁文件（<state 根>/*.owner.json）——聚合视图里一并列出。"""
        return LegacyLockScanner.scan(self._state_dir)

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
        # 评审读锁（聚合登记）：「已开启评审」这件事本身也是一把锁——
        # 别人一眼能看见"评审开着呢"，不必靠测试去猜哪里要避开。
        self._registry_register(
            self.review_lock_key(), KIND_REVIEW, self.reviewer,
            scope="评审读锁（评审已有基线，勿重复开启）",
            reason="开启评审：登记基线 + 挂评审读锁")
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

        # 情形 B：疑似"正在编辑"（存在他人租约）→ 先做其他，并挂锁；延迟后检测
        editing = self._editing()
        if editing:
            lock = self._defer_while_editing(note)
            after = float(lock.get("retry_at") or (self._clock()
                                                   + self.retry_delay_sec))
            return ArbiterDecision(
                action="retry", applied=False, conflict=False,
                should_retry=True, retry_after=after,
                retry_delay_sec=self.retry_delay_sec,
                deferred=True, lock=lock,
                baseline=baseline,
                reason=("卡正在被编辑（存在写租约）→ **先做其他，并挂锁**；"
                        f"延迟后检测（首次 {self.retry_delay_sec:.0f}s，"
                        f"重触发升级 {self.persistent_grace_sec:.0f}s 持续到对方处理完）"),
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
                lock = self._defer_while_editing(
                    note, scope=f"同向写入（{other}）时序冲突", holder=other)
                after = float(lock.get("retry_at") or (self._clock()
                                                       + self.retry_delay_sec))
                return ArbiterDecision(
                    action="retry", applied=False, conflict=False,
                    should_retry=True, retry_after=after,
                    retry_delay_sec=self.retry_delay_sec,
                    deferred=True, lock=lock,
                    baseline=baseline,
                    reason=(f"同向写入（{other}）已推进 v{disk_before.version}"
                            "→ 可重试的时序冲突，**先做其他、并挂锁**，延迟后检测"),
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

    # ------------------------------------------------------------------
    # 阶梯退避（Issue #14 追问：从少到多，10 分钟到顶即停）
    # ------------------------------------------------------------------

    def _step_for(self, retriggered: int) -> int:
        """本次该用阶梯的哪一档（0 起算）。无阶梯 → 0（固定窗口旧语义）。"""
        return max(0, int(retriggered))

    def waiting_status(self, retriggered: int = 0) -> Optional[dict]:
        """当前位置的等待计划（给角色/人看：下一档等多久、是否到顶）。"""
        if self.backoff is None:
            return None
        return self.backoff.status(self._step_for(retriggered)).to_dict()

    def _next_step(self, lock: Optional[Any]) -> int:
        """下一次检测该用阶梯的哪一档（**只进不退**，到顶后停在顶）。"""
        if self.backoff is None:
            return 0
        cur = 0
        if lock is not None and lock.retry_delay_sec:
            # 由"当前窗口时长"反查档位：窗口被拉长过就不能再退回短档。
            for idx, sec in enumerate(self.backoff.ladder):
                if sec <= lock.retry_delay_sec + 1e-9:
                    cur = idx
        return min(cur + 1, len(self.backoff.ladder) - 1)

    def _defer_while_editing(self, note: str, scope: str = "状态卡正在被编辑",
                             holder: str = "", persistent: bool = False) -> dict:
        """「**先做其他，并挂锁**」：挂一把锁等对方处理完，锁上带延迟后检测点。

        **等待时长走阶梯**（Issue #14 追问）：第 1 次 ≈ 10 秒，其后 30s → 60s →
        3min → 6min → **10min 到顶即停**（不再增长、不再重挂）。理由：
        "大部分很快就没问题"，前两档短一点，多数冲突在第 1 次检测就消失；
        长尾才逐步拉长，最后停在 10 分钟。

        **再次触发**：台账里同一把锁升级为**持续锁**（持久，不自动打开），
        等待档位继续沿阶梯上行（同一档不允许回退）。
        """
        key = self.defer_lock_key()
        now = self._clock()
        existing = self.lock_registry.get(key)
        is_retrigger = existing is not None
        # 重触发 = 本会话此前已挂过同一把锁（第 2 次及以后触发同一问题）
        triggers = int(existing.retry_count) + 1 if existing is not None else 0
        want_persistent = bool(is_retrigger or persistent)
        step = self._step_for(triggers)
        # 未装载规则包时退化为固定窗口（旧语义）；装载后等待由阶梯推出。
        requested = 0.0 if self.backoff is not None else self.retry_delay_sec
        # 一次登记同时带上「持续锁」与「延迟后检测点」两项语义
        # （不许分两次登记，否则会被当成"重触发"而互相抬高计数）
        lock = self._registry_register(
            key, KIND_DEFER, self.reviewer,
            scope=f"{scope}（先做其他，勿抢写）",
            persist=want_persistent, retry_delay_sec=requested,
            increment=is_retrigger, delay_step=step,
            reason=note or "评审遇到他人写租约：先做其他，并挂锁")
        if lock is None:      # 台账不可用时的最小降级（内存语义仍正确）
            fallback = (self.backoff.delay_for(step) if self.backoff is not None
                        else self.retry_delay_sec)
            return {"key": key, "kind": KIND_DEFER, "holder": self.reviewer,
                    "scope": scope,
                    "retry_at": now + fallback,
                    "retry_delay_sec": fallback,
                    "step": step,
                    "persistent": want_persistent,
                    "retriggered": triggers}
        lock["persistent"] = bool(lock.get("persistent") or want_persistent)
        lock["retriggered"] = triggers
        lock["step"] = step
        self._update_deferred(
            scope=scope, reason=note,
            persistent=bool(lock.get("persistent")),
            retriggered=triggers,
            retry_after=float(lock.get("retry_at") or 0.0),
            step=step)
        return lock

    def retry(
        self,
        mutate: Optional[Callable[[StateCard], None]] = None,
        proposed: Optional[StateCard] = None,
        note: str = "",
    ) -> ArbiterDecision:
        """**延迟后检测**：到点重试一次。

        - 锁未到点（或对方仍在编辑）→ 续期等待（``should_retry=True``，**不释放锁**）；
          持续锁在宽限期内永不提前打开（"别遗留锁一直不打开"的反面：也不许早开）；
        - 对方已处理完 → 正常走 `apply_save()`，成功后释放锁（编辑真的结束了）。
        """
        lock = self.lock_registry.get(self.defer_lock_key())
        if lock is not None and lock.status == LOCK_HELD \
                and lock.retry_at > self._clock():
            return ArbiterDecision(
                action="retry", applied=False, conflict=False,
                should_retry=True, retry_after=lock.retry_at,
                retry_delay_sec=self.retry_delay_sec, deferred=True,
                lock=lock.to_dict(),
                reason=("延迟窗未到：**先做其他**，到点再来检测"
                        f"（剩余 {max(0.0, lock.retry_at - self._clock()):.0f}s）"),
                detail=note,
            )

        if self._editing():
            # 还在编辑：续期，绝不释放。**等待档位沿阶梯上行**（Issue #14 追问）：
            # 10s → 30s → 60s → 3min → 6min → 10min 到顶即停；到顶后**不再重挂**，
            # 只做 heartbeat 续期把锁挂在原地等对方处理完（不遗留、也不重复空转）。
            step = self._next_step(lock)
            grant = (self.backoff.delay_for(step) if self.backoff is not None
                     else (self.persistent_grace_sec
                           if (lock is not None and lock.persistent)
                           else self.retry_delay_sec))
            self.lock_registry.heartbeat(
                self.defer_lock_key(), self.reviewer, extend_sec=grant)
            prev_retry = lock.retry_at if lock is not None else 0.0
            if prev_retry <= self._clock():
                if self.backoff is not None and self.backoff.is_top(step) \
                        and (lock is not None and lock.retry_delay_sec >= self.backoff.top):
                    # **到顶即停**：不把窗口继续往后推（否则"到顶"就成了无限等待），
                    # 只保留心跳续期与 10 分钟检测窗；锁状态由台账/票据继续可见。
                    pass
                else:
                    # 只重排**已过期**的检测点：本轮新窗口 = 阶梯下一档；
                    # 仍在未来的窗口绝不缩短（"持续到对方处理完"的承诺不可回退）。
                    self.lock_registry.register(
                        self.defer_lock_key(), kind=KIND_DEFER, holder=self.reviewer,
                        scope=(lock.scope if lock is not None
                               else "状态卡正在被编辑"),
                        reason="延迟后检测：对方仍在编辑 → 续期等待（锁不打开）",
                        delay_step=step)
            new_lock = self.lock_registry.get(self.defer_lock_key())
            return ArbiterDecision(
                action="retry", applied=False, conflict=False,
                should_retry=True,
                retry_after=(new_lock.retry_at if new_lock is not None
                             else self._clock() + self.retry_delay_sec),
                retry_delay_sec=self.retry_delay_sec, deferred=True,
                lock=new_lock.to_dict() if new_lock is not None else None,
                reason="延迟后检测：对方仍在编辑 → 锁续期等待，先做其他",
                detail=note,
            )

        decision = self.apply_save(mutate=mutate, proposed=proposed, note=note)
        # 重试成功且无冲突 → 之前的冲突卡作废 + 释放延迟锁（编辑已结束）
        if decision.applied and not decision.conflict:
            self.supersede_pending()
            self._registry_release(self.defer_lock_key(), by=self.reviewer,
                                   reason="延迟后检测成功：对方已处理完，锁打开")
        return decision

    def complete_deferred(
        self,
        mutate: Optional[Callable[[StateCard], None]] = None,
        note: str = "",
    ) -> ArbiterDecision:
        """「**先做其他**」做完之后的收尾：补做被延迟的那次评审。

        与 `retry()` 的差别：`retry()` 是"到点自动检测"，本方法是"我把其他事
        做完了，主动回来收尾"。锁打开是**人在回路**的：只有真的落盘成功
        （对方处理完）才释放；否则继续挂着并返回 `should_retry`。
        """
        lock = self.lock_registry.get(self.defer_lock_key())
        if lock is not None and lock.status == LOCK_HELD and self._editing():
            self.lock_registry.heartbeat(self.defer_lock_key(), self.reviewer)
            return ArbiterDecision(
                action="retry", applied=False, conflict=False,
                should_retry=True,
                retry_after=float(lock.retry_at
                                  or (self._clock() + self.retry_delay_sec)),
                retry_delay_sec=self.retry_delay_sec, deferred=True,
                lock=lock.to_dict(),
                reason="收尾时对方仍在编辑 → 锁保持挂着（不打开），先做其他",
                detail=note,
            )
        decision = self.retry(mutate=mutate, note=note)
        if decision.applied and not decision.conflict:
            self._registry_release(self.defer_lock_key(), by=self.reviewer,
                                   reason="先做其他的那次评审已补做完成，锁打开")
        return decision

    def release_locks(self, by: str = "", reason: str = "评审结束") -> int:
        """释放本会话评审相关的全部锁（评审读锁 + 延迟锁）。

        **只释放本评审方持有的锁**，不动卡片写租约（那是别人的编辑锁）；
        返回实际释放数量。
        """
        n = 0
        for key in (self.defer_lock_key(), self.review_lock_key()):
            try:
                if self.lock_registry.release(key, by=by or self.reviewer,
                                              reason=reason):
                    n += 1
            except Exception:  # noqa: BLE001
                pass
        return n

    def sweep_locks(self, by: str = "") -> List[dict]:
        """清理本会话的**遗留锁**（过期且超宽限仍未处理）→ 聚合视图里留痕。

        「注意别遗留锁一直不打开」：被扫成 stale 的若是延迟锁，对应的"待处理"
        挂账项同步关闭（避免锁没了、票还挂着 → 又成新的遗留）。
        """
        try:
            swept = self.lock_registry.sweep(by=by or self.reviewer)
        except Exception:  # noqa: BLE001
            return []
        if any(r.key == self.defer_lock_key() for r in swept):
            card = self.get_conflict()
            if card is not None and card.status == "pending" and card.deferred:
                card.status = "superseded"
                card.deferred = False
                card.deferred_persistent = False
                card.history.append({
                    "at": time.strftime("%Y-%m-%d %H:%M:%S"),
                    "by": by or self.reviewer,
                    "action": "sweep-stale-lock",
                    "resolution": "延迟锁过期未续期 → 挂账项一并关闭",
                })
                self._atomic_write(self.conflict_path(), card.to_dict())
        return [r.to_dict() for r in swept]

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
        card.deferred = False
        card.deferred_persistent = False
        card.editing = False
        card.history.append({
            "at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "by": (by or "unknown").strip(),
            "action": "resolve",
            "resolution": card.resolution,
        })
        self._atomic_write(self.conflict_path(), card.to_dict())
        # 收口 = 这件事处理完了 → 一并打开挂着的延迟锁（不遗留锁一直不打开）
        self._registry_release(self.defer_lock_key(), by=by or self.reviewer,
                               reason="冲突已收口，延迟锁打开")
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
        card.deferred = False
        card.deferred_persistent = False
        card.editing = False
        card.history.append({
            "at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "by": (by or "auto").strip(),
            "action": "supersede",
            "resolution": "冲突已消解（重试成功/已被新票据接管）",
        })
        self._atomic_write(self.conflict_path(), card.to_dict())
        # 待处理项消解 → 延迟锁一并打开（别遗留锁一直不打开）
        self._registry_release(self.defer_lock_key(), by=by or self.reviewer,
                               reason="冲突已消解，延迟锁打开")
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

    def _update_deferred(self, scope: str = "", reason: str = "",
                         persistent: bool = False, retriggered: int = 0,
                         retry_after: float = 0.0,
                         step: Optional[int] = None) -> ConflictCard:
        """把「延迟 + 挂锁」也登记成一张**冲突卡**（与人看的 `overview()` 对齐）。

        口径：冲突卡**至多一张**——延迟与真冲突都是"同一件待处理的事"，
        因此都收敛到同一张卡特；这样「触发了就去看一眼情况」只需看**一个**地方
        （聚合总览 + 唯一冲突卡），不会散落成多张票（那正是要治的循环特征）。
        """
        existing = self.get_conflict()
        now = self._clock()
        if existing is not None and existing.status == "pending":
            existing.deferred = True
            existing.retriggered = max(existing.retriggered, int(retriggered))
            existing.deferred_persistent = bool(existing.deferred_persistent
                                                or persistent)
            existing.retry_after = retry_after or (now + self._next_wait(step))
            existing.editing = True
            wait_plan = self._wait_plan_text(step)
            existing.reason = reason or (
                "先做其他，并挂锁：等对方处理完再检测"
                + ("（持续锁，一直挂到对方处理完）" if existing.deferred_persistent
                   else f"（{wait_plan}）"))
            existing.history.append({
                "at": time.strftime("%Y-%m-%d %H:%M:%S"), "by": self.reviewer,
                "action": "defer", "scope": scope, "reason": reason,
                "persistent": existing.deferred_persistent,
                "retriggered": existing.retriggered,
            })
            self._atomic_write(self.conflict_path(), existing.to_dict())
            return existing

        if existing is not None:
            existing.status = "superseded"
            existing.history.append({
                "at": time.strftime("%Y-%m-%d %H:%M:%S"), "by": self.reviewer,
                "action": "superseded_by_new",
            })
            self._atomic_write(self.conflict_path(), existing.to_dict())

        card = ConflictCard(
            id=f"{_CONFLICT_ID_PREFIX}-{self.session_id}-{int(self._clock() * 1000)}",
            session_id=self.session_id,
            base_version=0,
            current_version=0,
            kind=NO_CONFLICT,
            reason=reason or (
                "先做其他，并挂锁：等对方处理完再检测"
                + ("（持续锁，一直挂到对方处理完）" if persistent
                   else f"（{self._wait_plan_text(step)}）")),
            detected_at=time.strftime("%Y-%m-%d %H:%M:%S"),
            editing=True,
            retry_after=retry_after or (now + self._next_wait(step)),
            deferred=True,
            deferred_persistent=bool(persistent),
            retriggered=int(retriggered),
            history=[{
                "at": time.strftime("%Y-%m-%d %H:%M:%S"), "by": self.reviewer,
                "action": "defer", "scope": scope, "reason": reason,
                "persistent": bool(persistent), "retriggered": int(retriggered),
            }],
        )
        self._atomic_write(self.conflict_path(), card.to_dict())
        return card

    def _next_wait(self, step: Optional[int] = None) -> float:
        """下一档等待时长（无阶梯 → 固定 ``retry_delay_sec`` 旧语义）。"""
        if self.backoff is None:
            return self.retry_delay_sec
        return self.backoff.delay_for(0 if step is None else int(step))

    def _wait_plan_text(self, step: Optional[int] = None) -> str:
        """等待计划的人类可读说明（"下一档等多久 + 整条阶梯"）。"""
        if self.backoff is None:
            return f"延迟 {self.retry_delay_sec:.0f}s 后检测"
        st = self.backoff.status(0 if step is None else int(step))
        return f"阶梯第 {st.attempt + 1} 档：{st.label} 后检测（{self.backoff.human()}）"

    def deferred_status(self) -> Optional[dict]:
        """当前「先做其他，并挂锁」项的状态（无 → None）。"""
        card = self.get_conflict()
        if card is None or not card.deferred or card.status != "pending":
            return None
        lock = self.lock_registry.get(self.defer_lock_key())
        now = self._clock()
        return {
            "ticket": card.id,
            "reason": card.reason,
            "retriggered": card.retriggered,
            "persistent": card.deferred_persistent,
            "retry_at": card.retry_after,
            "retry_in_sec": max(0.0, card.retry_after - now),
            "lock": lock.to_dict() if lock is not None else None,
            "lock_should_release": self.lock_registry.should_release(
                self.defer_lock_key()),
            "editing": self._editing(),
            "waiting": self.waiting_status(card.retriggered),
        }

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
