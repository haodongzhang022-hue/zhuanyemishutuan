"""锁聚合台账（LockRegistry）：**所有锁统一登记**，触发即可查、不遗留

## 需求（Issue #14 追问，用户口径）

> 延迟可以到 1 分钟，再次触发就按 3 分钟持续到对方解决完，**注意别遗留锁一直不打开**，
> **所有锁应该被聚合的管理**，这样哪里要避开不用测试才知道，而是**触发了就可以去看
> 一眼情况**；并且建立在这里的触发机制——**延迟后检测**，以及**先做其他，并挂锁**，
> 实现稳定协作。

拆成四条可执行口径：

1. **所有锁聚合管理**：一份台账（`<state 数据根>/locks/registry.json`）登记全部锁
   （卡片写租约 / 评审读锁 / 评审延迟锁 / 写入序列化锁），带 `session_id`、`kind`、
   `holder`、`scope`、`acquired_at`、`expires_at`、`heartbeat_at`、`last_reason`。
   **触发即可一眼看清**：`overview()` 一次给出「谁锁了、锁什么、还有多久、动了没」，
   不必靠测试或试错去猜哪里要避开。
2. **别遗留锁一直不打开**：每把锁都必须有 `expires_at`（`persistent` 锁 =
   等对方处理完自动续期，**已过宽限仍未处理 → 判 `stale`**，由 `sweep()` 回收并留痕）。
3. **延迟后检测**：锁可挂「延迟后自动重试」（`retry_after`），到点由
   `due_for_retry()` 取出、逐把 `retry()` 检测；未能推进则续期等待，且**不会遗弃**。
4. **先做其他，并挂锁**：拿不到锁就**先把别的做完**，把这件事挂上锁留待后续——
   `acquire_blocked()` 返回「先做其他」指令；同一键再次触发时锁**升级为持续锁**
   （默认 3 分钟宽限），一直持续到对方处理完为止（`should_release()` 为 False 时
   **绝不释放**）。

## 锁的层次

| 层次 | 机制 | 能防什么 |
|---|---|---|
| L1 会话内/flock | `StateManager.lock_card` 文件锁 | 同机多进程/线程的写入互斥 |
| L2 账本锁 | `LockRegistry`（本模块） | 卡片写租约标识「正在编辑」、**评审读锁** |
| L3 版本 CAS | `cas_save_card(expected_version=…)` | 跨机并发（无共享文件锁时）的最后一道 |

**单锁兼容**：<session>.owner.json 仍是终态判据（`OwnershipGuard` 只认它），
台账只是它的**镜像 + 聚合视图**；台账被外部删除也不影响单锁行为，仍可自愈重建。

## 用法

    reg = LockRegistry("i14", data_dir="<state 根>/locks")
    lk = reg.acquire("review-baseline", kind=KIND_REVIEW, scope="snapshot")
    if lk is None:
        reg.acquire_blocked("review-baseline")   # 先做其他，并挂锁
    reg.heartbeat(lk)                            # 处理完 → 心跳/释放
    reg.release(lk)

    print(reg.overview())            # 触发了就去看一眼
    reg.sweep()                      # 清理遗留锁（不误杀活跃/持续锁）
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

# 会话标识白名单（与 StateManager / LoopGuard / ReviewArbiter 同族口径，尾锚 \Z）
_SAFE_ID = re.compile(r'^[A-Za-z0-9_-]+\Z')

# 锁类型：持有语义不同，但统一登记、统一回收
KIND_CARD_WRITE = "card_write"      # 卡片写租约（=「正在编辑」判据）
KIND_REVIEW = "review"              # 评审读锁（保证有基线才评审）
KIND_DEFER = "defer"                # 延迟锁（先做其他，并挂锁）
KIND_SERIALIZE = "serialize"        # 写入序列化锁

ALL_KINDS = (KIND_CARD_WRITE, KIND_REVIEW, KIND_DEFER, KIND_SERIALIZE)

# 锁状态
LOCK_HELD = "held"        # 持有中（未过期）
LOCK_STALE = "stale"      # 过期未处理 → 遗留嫌疑（待回收）
LOCK_RELEASED = "released"  # 已正常释放
LOCK_REVOKED = "revoked"  # 人工/清理回收（留痕）

# 默认 TTL
DEFAULT_TTL_SEC = 60.0          # 「延迟可以到 1 分钟」（= 阶梯第 3 档）
DEFAULT_PERSISTENT_GRACE_SEC = 180.0  # 「再次触发就按 3 分钟持续到对方解决完」
DEFAULT_SWEEP_GRACE_SEC = 30.0  # 过期后仍给一段宽限，避免误杀"刚好在续期"的锁


def _now_str(at: Optional[float] = None) -> str:
    """格式化时间戳（跟随注入时钟，避免跨实例/假时钟下日志与到期时间自相矛盾）。"""
    return time.strftime("%Y-%m-%d %H:%M:%S",
                         time.localtime(time.time() if at is None else at))


@dataclass
class LockRecord:
    """一把锁的登记项（聚合视图的最小单元）。"""

    key: str
    session_id: str
    kind: str = KIND_SERIALIZE
    scope: str = ""
    holder: str = ""
    acquired_at: float = 0.0
    expires_at: float = 0.0
    ttl_sec: float = 0.0           # 首次登记的锁寿命（续期沿用，防"越续越长"）
    heartbeat_at: float = 0.0
    persistent: bool = False       # True：持续到对方处理完（不自动过期，靠宽限判 stale）
    retry_at: float = 0.0          # 延迟后检测的时间点（0 = 无）
    retry_delay_sec: float = 0.0
    retry_count: int = 0           # 重触发/重试次数（第 2 次起升级为持续锁）
    status: str = LOCK_HELD
    last_reason: str = ""
    releases: int = 0              # 被释放/回收次数（>1 说明重触发过）
    history: List[Dict[str, Any]] = field(default_factory=list)

    # ---- 视图 ----

    def is_expired(self, now: Optional[float] = None) -> bool:
        now = time.time() if now is None else now
        return self.expires_at > 0 and now >= self.expires_at

    def ttl_left(self, now: Optional[float] = None) -> float:
        now = time.time() if now is None else now
        return max(0.0, self.expires_at - now) if self.expires_at > 0 else 0.0

    def to_dict(self) -> dict:
        return {
            "key": self.key,
            "session_id": self.session_id,
            "kind": self.kind,
            "scope": self.scope,
            "holder": self.holder,
            "acquired_at": self.acquired_at,
            "expires_at": self.expires_at,
            "ttl_sec": self.ttl_sec,
            "heartbeat_at": self.heartbeat_at,
            "persistent": self.persistent,
            "retry_at": self.retry_at,
            "retry_delay_sec": self.retry_delay_sec,
            "retry_count": self.retry_count,
            "status": self.status,
            "last_reason": self.last_reason,
            "releases": self.releases,
            "history": [dict(h) for h in self.history],
        }

    @classmethod
    def from_dict(cls, data: Any) -> Optional["LockRecord"]:
        """字段级容错：非 dict / key 缺失 / 非法 key → None（宽进不炸）。"""
        if not isinstance(data, dict):
            return None
        key = data.get("key")
        if not isinstance(key, str) or not key:
            return None
        sid = data.get("session_id", "")
        if not isinstance(sid, str):
            sid = ""

        def _f(k: str, dflt: float = 0.0) -> float:
            v = data.get(k, dflt)
            if isinstance(v, bool) or not isinstance(v, (int, float)):
                return dflt
            return float(v)

        def _i(k: str, dflt: int = 0) -> int:
            v = data.get(k, dflt)
            if isinstance(v, bool) or not isinstance(v, int):
                return dflt
            return v

        hist = data.get("history", [])
        if not isinstance(hist, list):
            hist = []
        return cls(
            key=key,
            session_id=sid,
            kind=str(data.get("kind") or KIND_SERIALIZE),
            scope=str(data.get("scope") or ""),
            holder=str(data.get("holder") or ""),
            acquired_at=_f("acquired_at"),
            expires_at=_f("expires_at"),
            ttl_sec=_f("ttl_sec"),
            heartbeat_at=_f("heartbeat_at"),
            persistent=bool(data.get("persistent", False)),
            retry_at=_f("retry_at"),
            retry_delay_sec=_f("retry_delay_sec"),
            retry_count=max(0, _i("retry_count")),
            status=str(data.get("status") or LOCK_HELD),
            last_reason=str(data.get("last_reason") or ""),
            releases=max(0, _i("releases")),
            history=[h for h in hist if isinstance(h, dict)],
        )


class LockRegistry:
    """会话锁的**聚合台账**（thread-safe 靠 flock 串行化台账文件读写）。

    一个实例 = 一份台账文件（``<data_dir>/registry.json``）；同一台机器上多个
    实例/进程共享同一份文件，读改写全程持 ``<data_dir>/.registry.lock`` 排他锁。
    """

    LOCK_FILENAME = ".registry.lock"
    REGISTRY_FILENAME = "registry.json"

    def __init__(
        self,
        session_id: str,
        data_dir: Optional[str] = None,
        ttl_sec: float = DEFAULT_TTL_SEC,
        persistent_grace_sec: float = DEFAULT_PERSISTENT_GRACE_SEC,
        sweep_grace_sec: float = DEFAULT_SWEEP_GRACE_SEC,
        clock: Optional[Callable[[], float]] = None,
        backoff: Optional[StepBackoff] = None,
    ):
        if not isinstance(session_id, str) or not _SAFE_ID.match(session_id):
            raise ValueError(
                f"非法 session_id: {session_id!r}。仅允许 [A-Za-z0-9_-]。")
        for name, val in (("ttl_sec", ttl_sec),
                          ("persistent_grace_sec", persistent_grace_sec),
                          ("sweep_grace_sec", sweep_grace_sec)):
            if not isinstance(val, (int, float)) or isinstance(val, bool) or val < 0:
                raise ValueError(f"非法 {name}: {val!r}（须为非负数）。")
        self.session_id = session_id
        self.data_dir = data_dir or os.path.join(os.getcwd(), "data", "state", "locks")
        self.ttl_sec = float(ttl_sec)
        self.persistent_grace_sec = float(persistent_grace_sec)
        self.sweep_grace_sec = float(sweep_grace_sec)
        self._clock = clock or time.time
        # 阶梯退避（Issue #14 追问）：延迟后检测的**等待时长**从少到多逐步拉长，
        # 10s → 30s → 60s → 3min → 6min → 10min 到顶即停。默认为空 = 保持
        # 「每次都等固定 ttl」的旧语义（向后兼容；会话层默认装载跨仓库规则包）。
        self.backoff = backoff
        os.makedirs(self.data_dir, exist_ok=True)

    # ------------------------------------------------------------------
    # 路径 / 台账读写
    # ------------------------------------------------------------------

    def registry_path(self) -> str:
        """台账文件路径。"""
        return os.path.join(self.data_dir, self.REGISTRY_FILENAME)

    def _lock_path(self) -> str:
        return os.path.join(self.data_dir, self.LOCK_FILENAME)

    def _with_lock(self, fn):
        """持台账排他锁执行 fn()（跨进程串行化；无 fcntl 时退化为直接执行）。"""
        path = self._lock_path()
        try:
            import fcntl
        except ImportError:  # pragma: no cover - 非 POSIX 平台
            return fn()
        with open(path, "a+", encoding="utf-8") as f:
            try:
                fcntl.flock(f.fileno(), fcntl.LOCK_EX)
            except OSError:  # pragma: no cover - 平台不支持文件锁
                return fn()
            try:
                return fn()
            finally:
                try:
                    fcntl.flock(f.fileno(), fcntl.LOCK_UN)
                except OSError:  # pragma: no cover
                    pass

    def _load_all(self) -> Dict[str, LockRecord]:
        """读回全部锁记录（损坏/非对象 → 空台账，不抛）。"""
        try:
            with open(self.registry_path(), "r", encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, ValueError):
            return {}
        if not isinstance(data, dict):
            return {}
        raw = data.get("locks", [])
        if not isinstance(raw, list):
            return {}
        out: Dict[str, LockRecord] = {}
        for item in raw:
            rec = LockRecord.from_dict(item)
            if rec is not None:
                out[rec.key] = rec
        return out

    def _write_all(self, locks: Dict[str, LockRecord]) -> None:
        """原子落盘台账（同目录 tmp + os.replace）。"""
        payload = {
            "session_id": self.session_id,
            "updated_at": _now_str(),
            "count": len(locks),
            "locks": [locks[k].to_dict() for k in sorted(locks)],
        }
        fd, tmp = tempfile.mkstemp(
            dir=self.data_dir, prefix=".tmp-registry-", suffix=".json")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False, indent=2)
            os.replace(tmp, self.registry_path())
        except Exception:
            try:
                os.remove(tmp)
            except OSError:
                pass
            raise

    # ------------------------------------------------------------------
    # 1) 聚合登记：所有锁都写进同一本台账
    # ------------------------------------------------------------------

    def register(
        self,
        key: str,
        kind: str = KIND_SERIALIZE,
        holder: str = "",
        scope: str = "",
        ttl_sec: Optional[float] = None,
        persistent: bool = False,
        retry_delay_sec: float = 0.0,
        persist: bool = False,
        increment: bool = False,
        reason: str = "",
        delay_step: Optional[int] = None,
    ) -> LockRecord:
        """登记（或**再次触发**升级）一把锁。

        Args:
            key: 锁键（全局唯一，建议 ``<session>:<用途>``）
            kind: 锁类型（见 ``ALL_KINDS``）
            holder: 持有者标识（秘书/角色名）
            scope: 保护范围说明（"哪里要避开"）
            ttl_sec: 本次 TTL（默认 ``ttl_sec``，即 1 分钟延迟窗）
            persistent: True → 持续到对方处理完（不自动过期）
            retry_delay_sec: 本次「延迟后检测」的等待时长（退避可长可短；
                0 = 无检测点）
            persist: 本次起**升级为持续锁**（持久 + 3 分钟宽限），
                持续到对方处理完；一旦置持续，后续触发不会把它降回普通锁
            increment: 是否把本次登记计为「**再次触发**」（调用方显式声明；
                内部续期/心跳不得计入，否则计数会被悄悄抬高）
            reason: 触发原因（留痕）
            increment: 是否把本次登记计为「**再次触发**」（调用方显式声明）
            delay_step: **阶梯档位序号**（0 起算）。给定且构造时带 ``backoff`` 时，
                本次等待时长**由阶梯推出**（``10s→30s→60s→3min→6min→10min``），
                且**只增不减**：已承诺的等待窗口绝不会被缩短；显式 ``retry_delay_sec``
                优先于阶梯（调用方有明确口径时以调用方为准）。

        Returns:
            最新 ``LockRecord``；同一 key **再次触发** → 升级为持续锁
            （``persistent=True`` + 3 分钟宽限），不断累积重试计数。
        """
        if not isinstance(key, str) or not key.strip():
            raise ValueError("锁 key 不可为空。")
        key = key.strip()
        if key not in (kind,) and not isinstance(key, str):
            raise ValueError(f"非法锁 key: {key!r}")  # pragma: no cover
        if kind not in ALL_KINDS:
            raise ValueError(f"非法锁类型: {kind!r}（须为 {ALL_KINDS} 之一）。")

        def _op() -> LockRecord:
            now = self._clock()
            locks = self._load_all()
            existing = locks.get(key)
            ttl = self.ttl_sec if ttl_sec is None else float(ttl_sec)
            delay = float(retry_delay_sec) if retry_delay_sec else ttl
            # 阶梯退避（Issue #14 追问）：档位由调用方给出（0 起算），
            # 等待时长从少到多；显式 retry_delay_sec 优先（口径冲突时以调用方为准）。
            stair = None
            if self.backoff is not None and delay_step is not None \
                    and not retry_delay_sec:
                stair = self.backoff.delay_for(delay_step)
                delay = stair
            if existing is not None and existing.status == LOCK_HELD:
                # 再次触发的**计数只由调用方显式声明**（increment=True）——
                # 否则内部续期/心跳也会被当成"重触发"，把计数越抬越高。
                if increment:
                    existing.retry_count += 1
                existing.persistent = existing.persistent or persistent or persist
                existing.holder = holder or existing.holder
                existing.scope = scope or existing.scope
                # 续期语义：本次 TTL 显式给出 → 用本次；未给出 → 沿用首次 TTL
                # （不悄悄改锁寿命：读「now 与到期」的差值会随续期不断变长）
                lock_ttl = ttl if ttl_sec is not None else existing.ttl_sec
                if lock_ttl <= 0:
                    lock_ttl = ttl
                existing.expires_at = now + lock_ttl
                existing.ttl_sec = lock_ttl
                existing.heartbeat_at = now
                if retry_delay_sec or stair is not None:
                    # 退避**由调用方定值**（已含"上一档 + 一档"），本层只承接；
                    # 新窗口若比原窗口更早，也要保持"不缩短"的最小语义——
                    # 阶梯"从少到多"绝不意味着可以把已承诺的长等待改回短等待。
                    earliest = max(existing.retry_at, now + delay)
                    existing.retry_at = earliest
                    existing.retry_delay_sec = max(0.0, earliest - now)
                    if stair is not None:
                        existing.history.append({
                            "at": _now_str(now), "action": "backoff-step",
                            "by": holder, "step": int(delay_step),
                            "delay_sec": stair,
                            "label": self.backoff.status(int(delay_step)).label,
                        })
                else:
                    existing.retry_at = 0.0
                    existing.retry_delay_sec = 0.0
                existing.last_reason = reason or existing.last_reason
                existing.history.append({
                    "at": _now_str(now), "action": "re-acquire",
                    "by": holder, "reason": reason,
                    "persistent": existing.persistent,
                    "retry_count": existing.retry_count,
                })
                locks[key] = existing
                self._write_all(locks)
                return existing

            is_persistent = bool(persistent or persist)
            rec = LockRecord(
                key=key,
                session_id=self.session_id,
                kind=kind,
                scope=scope,
                holder=holder,
                acquired_at=now,
                expires_at=now + (self.persistent_grace_sec if is_persistent else ttl),
                ttl_sec=(self.persistent_grace_sec if is_persistent else ttl),
                heartbeat_at=now,
                persistent=is_persistent,
                retry_at=(now + delay) if delay else 0.0,
                retry_delay_sec=delay,
                retry_count=0,
                status=LOCK_HELD,
                last_reason=reason,
                releases=(existing.releases if existing is not None else 0),
                history=[{
                    "at": _now_str(now), "action": "register",
                    "by": holder, "reason": reason, "kind": kind,
                    "persistent": is_persistent,
                    "ttl_sec": ttl,
                }],
            )
            locks[key] = rec
            self._write_all(locks)
            return rec

        return self._with_lock(_op)

    def acquire(
        self,
        key: str,
        kind: str = KIND_SERIALIZE,
        holder: str = "",
        scope: str = "",
        ttl_sec: Optional[float] = None,
        retry_delay_sec: float = 0.0,
        reason: str = "",
    ) -> Optional[LockRecord]:
        """尝试获取一把锁；**已被他人持有时返回 None**（调用方「先做其他，并挂锁」）。

        本方法只登记**普通锁**（会自动过期 = 延迟锁：延迟后检测再处理）。
        若希望「挂住不放、持续到对方处理完」，用 ``hold_until_resolved()``。
        """
        existing = self.get(key)
        now = self._clock()
        if existing is not None and existing.status == LOCK_HELD \
                and existing.holder and holder and existing.holder != holder \
                and not existing.is_expired(now):
            return None
        return self.register(
            key, kind=kind, holder=holder, scope=scope, ttl_sec=ttl_sec,
            retry_delay_sec=retry_delay_sec, reason=reason)

    def hold_until_resolved(
        self,
        key: str,
        kind: str = KIND_DEFER,
        holder: str = "",
        scope: str = "",
        reason: str = "",
        grace_sec: Optional[float] = None,
    ) -> LockRecord:
        """**先做其他，并挂锁**：挂一把持续锁（默认 3 分钟宽限，不自动过期）。

        语义（用户口径）："再次触发就按 3 分钟持续到对方解决完"——
        锁挂上后不再自动打开，直到 `release()` / 对方处理完 / `sweep()` 判 stale。
        每次 `heartbeat()` 会续期，故只要还有人在处理，锁就一直有效。
        """
        rec = self.register(
            key, kind=kind, holder=holder, scope=scope,
            ttl_sec=grace_sec if grace_sec is not None else self.persistent_grace_sec,
            persistent=True, retry_delay_sec=0.0, reason=reason)
        return self.heartbeat(rec.key, holder=holder) or rec

    def acquire_blocked(self, key: str, holder: str = "",
                        reason: str = "") -> Dict[str, Any]:
        """挂锁等待 + 返回「先做其他」指令（拿不到锁时不空等）。

        Returns:
            ``{"action": "do_other_first", "waited": …, "lock": …}``
            —— ``lock`` 为挂在那把锁上的持续锁记录（供后续 `overview()` 查看）。
        """
        stuck = self.get(key)
        lock = self.hold_until_resolved(
            key, kind=KIND_DEFER, holder=holder,
            scope=(stuck.scope if stuck is not None else ""),
            reason=reason or "锁被占用：先做其他，并挂锁等待对方处理完")
        now = self._clock()
        return {
            "action": "do_other_first",
            "key": key,
            "holder": stuck.holder if stuck is not None else "",
            "waited_sec": max(0.0, now - (stuck.acquired_at if stuck is not None else now)),
            "retry_at": lock.retry_at,
            "lock": lock.to_dict(),
            "guidance": ("先做其他任务，不要空等；锁已挂住，"
                         "对方处理完（锁释放/过期）后会自动重试"),
        }

    # ------------------------------------------------------------------
    # 2) 续期 / 释放 / 回收（别遗留锁一直不打开）
    # ------------------------------------------------------------------

    def get(self, key: str) -> Optional[LockRecord]:
        """读一把锁（无/坏 → None）。"""
        return self._load_all().get(key)

    def heartbeat(self, key: str, holder: str = "",
                  extend_sec: Optional[float] = None) -> Optional[LockRecord]:
        """续期（"还在处理中"）：刷新心跳与到期时间；已释放/回收 → None。"""
        def _op() -> Optional[LockRecord]:
            locks = self._load_all()
            rec = locks.get(key)
            if rec is None or rec.status != LOCK_HELD:
                return None
            now = self._clock()
            if extend_sec is not None:
                ext = float(extend_sec)
            elif rec.persistent:
                ext = max(self.persistent_grace_sec, rec.ttl_sec)
            else:
                ext = rec.ttl_sec or self.ttl_sec
            rec.expires_at = now + ext
            rec.ttl_sec = ext
            rec.heartbeat_at = now
            if holder:
                rec.holder = holder
            rec.history.append({"at": _now_str(now), "action": "heartbeat",
                                "by": holder, "extend_sec": ext})
            locks[key] = rec
            self._write_all(locks)
            return rec

        return self._with_lock(_op)

    def should_release(self, key: str) -> bool:
        """**该不该打开这把锁**：持续锁在宽限期内一律 False（不许提前打开）。

        只有过期且超过宽限（判 stale）或非持续锁已过期时才 True。
        """
        rec = self.get(key)
        if rec is None or rec.status != LOCK_HELD:
            return True
        now = self._clock()
        if not rec.is_expired(now):
            return False
        grace = self.sweep_grace_sec + (self.persistent_grace_sec if rec.persistent else 0)
        return now >= rec.expires_at + grace

    def release(self, key: str, by: str = "", reason: str = "") -> bool:
        """正常释放锁（处理完了）→ 状态置 ``released`` 并留痕。

        Returns:
            True 实际释放；False 锁不存在或已释放（幂等）。
        """
        def _op() -> bool:
            locks = self._load_all()
            rec = locks.get(key)
            if rec is None or rec.status != LOCK_HELD:
                return False
            rec.status = LOCK_RELEASED
            rec.releases += 1
            rec.heartbeat_at = self._clock()
            rec.history.append({"at": _now_str(self._clock()), "action": "release",
                                "by": by, "reason": reason})
            locks[key] = rec
            self._write_all(locks)
            return True

        return self._with_lock(_op)

    def sweep(self, by: str = "", grace_sec: Optional[float] = None) -> List[LockRecord]:
        """清理**遗留锁**（过期且超宽限仍未处理）→ 置 ``stale`` 并留痕。

        「注意别遗留锁一直不打开」：本方法给出确定的回收路径；**不误杀**
        活跃锁与宽限内的持续锁（反向对照见测试）。

        Returns:
            被判 stale 回收的锁清单（空 = 无遗留）。
        """
        grace = self.sweep_grace_sec if grace_sec is None else float(grace_sec)

        def _op() -> List[LockRecord]:
            locks = self._load_all()
            now = self._clock()
            out: List[LockRecord] = []
            changed = False
            for key, rec in list(locks.items()):
                if rec.status != LOCK_HELD:
                    continue
                if rec.expires_at <= 0:
                    continue
                if now < rec.expires_at + grace:
                    continue
                rec.status = LOCK_STALE
                rec.releases += 1
                rec.history.append({
                    "at": _now_str(now), "action": "sweep-stale", "by": by,
                    "reason": f"过期 {(now - rec.expires_at):.0f}s 未续期/未处理 → 回收",
                })
                locks[key] = rec
                out.append(rec)
                changed = True
            if changed:
                self._write_all(locks)
            return out

        return self._with_lock(_op)

    def revoke(self, key: str, by: str = "", reason: str = "") -> bool:
        """人工/上游强制回收一把锁（留痕 `by`；锁不存在 → False）。"""
        def _op() -> bool:
            locks = self._load_all()
            rec = locks.get(key)
            if rec is None or rec.status != LOCK_HELD:
                return False
            rec.status = LOCK_REVOKED
            rec.releases += 1
            rec.history.append({"at": _now_str(self._clock()), "action": "revoke",
                                "by": by or "unknown", "reason": reason})
            locks[key] = rec
            self._write_all(locks)
            return True

        return self._with_lock(_op)

    # ------------------------------------------------------------------
    # 3) 触发即可查：聚合视图
    # ------------------------------------------------------------------

    def list_held(self, kind: Optional[str] = None) -> List[LockRecord]:
        """当前持有中的锁（可按类型过滤）。"""
        out = [r for r in self._load_all().values() if r.status == LOCK_HELD]
        if kind:
            out = [r for r in out if r.kind == kind]
        return sorted(out, key=lambda r: r.acquired_at)

    def due_for_retry(self) -> List[LockRecord]:
        """**延迟后检测**：到点该重试的锁（持有中 + 有 retry_at + 已到点）。"""
        now = self._clock()
        return [r for r in self.list_held()
                if r.retry_at > 0 and now >= r.retry_at]

    def overview(self) -> Dict[str, Any]:
        """锁聚合总览：**触发了就去看一眼情况**（一眼知道哪里要避开）。

        Returns:
            ``{"count", "by_kind", "where_to_avoid", "held", "stale_candidates"}``
            —— ``where_to_avoid`` 是给人看的"哪里要避开"清单。
        """
        now = self._clock()
        all_locks = self._load_all()
        held = [r for r in all_locks.values() if r.status == LOCK_HELD]
        by_kind: Dict[str, int] = {}
        for r in held:
            by_kind[r.kind] = by_kind.get(r.kind, 0) + 1
        stale_candidates = [
            r for r in held
            if r.expires_at > 0
            and now >= r.expires_at + (self.sweep_grace_sec
                                       + (self.persistent_grace_sec if r.persistent else 0))
        ]
        return {
            "session_id": self.session_id,
            "count": len(held),
            "by_kind": by_kind,
            "held": [{
                "key": r.key,
                "kind": r.kind,
                "holder": r.holder,
                "scope": r.scope,
                "persistent": r.persistent,
                "ttl_left_sec": round(r.ttl_left(now), 1),
                "retry_in_sec": (round(max(0.0, r.retry_at - now), 1)
                                 if r.retry_at > 0 else None),
                "retry_count": r.retry_count,
                "reason": r.last_reason,
            } for r in sorted(held, key=lambda x: x.acquired_at)],
            "where_to_avoid": [
                f"{r.scope or r.key}（{r.kind}｜持有者 {r.holder or '未标注'}"
                f"｜剩余 {round(r.ttl_left(now), 1)}s"
                f"{'｜持续到处理完' if r.persistent else ''}）"
                for r in sorted(held, key=lambda x: x.acquired_at)
            ],
            "stale_candidates": [r.key for r in stale_candidates],
            "legacy_hint": ("历史遗留的单锁文件（<session>.owner.json）见 "
                            "LegacyLockScanner；本台账为新锁的唯一权威来源"),
        }

    def history(self, key: Optional[str] = None, limit: int = 50) -> List[Dict[str, Any]]:
        """留痕（可按锁键过滤；返回最近 `limit` 条）。"""
        all_locks = self._load_all()
        recs = [all_locks[key]] if key and key in all_locks else list(all_locks.values())
        rows: List[Dict[str, Any]] = []
        for rec in recs:
            for h in rec.history:
                rows.append({"key": rec.key, "kind": rec.kind, **h})
        rows.sort(key=lambda r: str(r.get("at", "")))
        return rows[-limit:] if limit and limit > 0 else rows

    def delete_state(self) -> bool:
        """会话生命周期回收：删除台账（同 id 复建不带陈旧锁）。"""
        removed = False
        for path in (self.registry_path(), self._lock_path()):
            try:
                if os.path.exists(path):
                    os.remove(path)
                    removed = True
            except OSError:
                pass
        return removed


class LegacyLockScanner:
    """历史遗留的**单锁文件**扫描（<session>.owner.json）。

    「所有锁应该被聚合的管理」——老的单锁不强行迁移，但必须能被**看见**：
    本扫描器把它们以 ``legacy`` 名义报出来，供 `SecretarySession` 汇总时提醒
    「这里还有一把没人管的锁」。扫描只读、不删（避免误伤他人租约）。
    """

    LOCK_SUFFIX = ".owner.json"

    @staticmethod
    def scan(state_dir: str) -> List[Dict[str, Any]]:
        """扫描目录下的单锁文件（非目录/不存在 → 空清单；坏文件只报不抛）。"""
        out: List[Dict[str, Any]] = []
        if not isinstance(state_dir, str) or not os.path.isdir(state_dir):
            return out
        try:
            names = sorted(os.listdir(state_dir))
        except OSError:
            return out
        for name in names:
            if not name.endswith(LegacyLockScanner.LOCK_SUFFIX):
                continue
            path = os.path.join(state_dir, name)
            entry: Dict[str, Any] = {
                "path": path,
                "session_id": name[:-len(LegacyLockScanner.LOCK_SUFFIX)],
                "kind": "legacy_card_write",
                "status": "held",
            }
            try:
                with open(path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                if isinstance(data, dict):
                    entry["holder"] = str(data.get("holder") or "")
                    entry["until"] = data.get("until")
                    until = data.get("until")
                    if isinstance(until, (int, float)) and not isinstance(until, bool):
                        entry["expired"] = float(until) <= time.time()
                        entry["status"] = "stale" if entry["expired"] else "held"
            except (OSError, ValueError):
                entry["status"] = "unreadable"
            out.append(entry)
        return out
