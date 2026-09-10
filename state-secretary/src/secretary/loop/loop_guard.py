"""循环熔断守卫（LoopGuard）：轮次预算 + 非必要修复熔断 + 静默交接

## 背景（Issue #14）

评审/开发往返缺乏上界时，Issue 会持续「打回 → 修正 → 复评」无限轮次；
其中相当比例的往复并非门禁必修（🔴/P0），而是 🟡 类**无必要修复项**，
每轮都产生新的可挑项 → 永动循环。

本模块给会话秘书（L3）一个**确定性、可持久化**的熔断判据，四道上界：

1. **门禁修复轮次上界** ``max_rounds``：出现 🔴/门禁必修项时，允许的修复往返
   次数（默认 3，与「秘书·评审 不合格最多 3 次」一致）；耗尽即熔断。
2. **非必要修复上界** ``max_advisory_fix_rounds``：仅剩 🟡/非门禁项时，
   **最多修 1 次**（默认 1）；再报同类项即熔断，不再产生新一轮。
3. **修复总轮次上界** ``max_total_rounds``：任意混合形态下的硬上界
   （默认 ``max_rounds + max_advisory_fix_rounds + 1``），防「门禁项与非必要项
   交替出现」绕过前两道上界。
4. **停滞感知** ``max_stall_repeats``（默认 2）：打回方带 ``signature``
   （如「整改项指纹」）重复出现同一签名且无进展 → 判定**停滞循环**立即熔断，
   不必等轮次预算耗尽（"感知循环并阻碍"——循环的典型特征是"同一问题反复报、
   每轮都算新的一轮"）。不带 ``signature`` 的轮次不参与停滞判定（显式选用）。

## 熔断语义（静默交接 · 不唤醒）

熔断 = ``LoopAction.FUSE`` + ``wake=False``：

- **不产生新一轮修复**，``should_wake_next()`` 为 False；
- 交接给下一个 agent 时携带 **``wake=False``** 的静默交接卡
  （``SecretarySession.create_handoff(..., wake=False)``），接收方**只登记、
  不唤醒**——即不得据此再开一轮「修复 → 复评」；
- 只有**人工**显式 ``resume(by=..., reason=...)``（留痕解锁）才恢复修复预算，
  避免下一 agent 自行复活循环。

## 状态与持久化

- 状态文件 ``<data_dir>/<session_id>.json``（``session_id`` 须过
  ``[A-Za-z0-9_-]+`` 白名单，不参与路径穿越）；
- 原子落盘（``tmp + os.replace``）；文件损坏/非对象 JSON → 视为全新状态
  （不抛异常，宁可从宽开始也不因坏文件卡死会话）；
- 状态**跨实例可读**：同一 ``session_id`` 的新秘书实例读回熔断态后仍熔断
  → 「会话换 agent 即复活循环」被堵死。

## 判定口径（确定性，与调用顺序无关）

``preview()`` 为**纯函数**（不改状态、不落盘），``register_round()`` 才提交；
判定顺序：已熔断 → **停滞**（指纹重复）→ 总轮次上界 → 门禁上界 → 非必要上界 →
无需修复（``CLEAR``，不消耗任何预算、不唤醒）。

``CLEAR``（无 🔴 无 🟡）不消耗预算：门禁放行路径不得被"空转轮次"挤掉预算。
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional

# 会话标识白名单（与 SecretarySession / HandoffBus 同族口径，尾锚 \Z 防尾换行）
_SAFE_ID = re.compile(r'^[A-Za-z0-9_-]+\Z')

DEFAULT_MAX_ROUNDS = 3
DEFAULT_MAX_ADVISORY_FIX_ROUNDS = 1
# 同一整改项指纹连续出现次数上界：达到即判定停滞循环（Issue #14「感知循环」）
DEFAULT_MAX_STALL_REPEATS = 2


class LoopAction(str, Enum):
    """一轮的处置动作（由预算确定，不依赖模型自述）。"""

    CLEAR = "clear"          # 无必修/无建议项：无需修复，**不唤醒**（门禁放行）
    FIX = "fix"              # 门禁必修项，预算内 → 允许修复并唤醒下一轮复评
    FIX_LAST = "fix_last"    # 仅非必要项，**最多 1 次** → 允许修复且仅此一次唤醒
    FUSE = "fuse"            # 熔断：不再修复、不再唤醒，转静默交接


@dataclass
class LoopDecision:
    """一次轮次判定结果（``register_round`` 的返回值 / ``preview`` 的预览）。"""

    action: str
    round_no: int
    blocking: int
    advisory: int
    wake: bool
    budget_left: int
    advisory_left: int
    total_left: int
    reason: str = ""
    fused_now: bool = False   # 本次调用**刚刚**触发熔断（供调用方只建一次静默交接卡）
    signature: str = ""       # 本轮整改项指纹（停滞感知）
    stall_count: int = 0      # 同一指纹连续出现次数

    def to_dict(self) -> dict:
        return {
            "action": self.action,
            "round_no": self.round_no,
            "blocking": self.blocking,
            "advisory": self.advisory,
            "wake": self.wake,
            "budget_left": self.budget_left,
            "advisory_left": self.advisory_left,
            "total_left": self.total_left,
            "reason": self.reason,
            "fused_now": self.fused_now,
            "signature": self.signature,
            "stall_count": self.stall_count,
        }


@dataclass
class LoopState:
    """会话循环状态（持久化单元）。"""

    session_id: str
    round_no: int = 0             # 已登记轮次总数（含 CLEAR）
    blocking_rounds: int = 0      # 已消耗的门禁修复轮次
    repair_rounds: int = 0        # 已消耗的修复轮次（门禁 ∪ 非必要，总上界用）
    advisory_used: int = 0        # 自上次门禁轮以来的非必要修复次数
    fused: bool = False
    fused_reason: str = ""
    fused_at: str = ""
    resume_count: int = 0
    last_signature: str = ""      # 上一轮整改项指纹（停滞感知用）
    stall_count: int = 0          # 同一指纹连续出现次数
    history: List[Dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "session_id": self.session_id,
            "round_no": self.round_no,
            "blocking_rounds": self.blocking_rounds,
            "repair_rounds": self.repair_rounds,
            "advisory_used": self.advisory_used,
            "fused": self.fused,
            "fused_reason": self.fused_reason,
            "fused_at": self.fused_at,
            "resume_count": self.resume_count,
            "last_signature": self.last_signature,
            "stall_count": self.stall_count,
            "history": self.history,
        }

    @classmethod
    def from_dict(cls, data: Any, session_id: str) -> "LoopState":
        """字段级容错恢复：非 dict / 字段类型不符 → 退化为默认值（不抛异常）。"""
        if not isinstance(data, dict):
            return cls(session_id=session_id)

        def _int(key: str) -> int:
            v = data.get(key, 0)
            return v if isinstance(v, int) and not isinstance(v, bool) else 0

        def _str(key: str) -> str:
            v = data.get(key, "")
            return v if isinstance(v, str) else ""

        hist = data.get("history", [])
        if not isinstance(hist, list):
            hist = []
        return cls(
            session_id=session_id,
            round_no=max(0, _int("round_no")),
            blocking_rounds=max(0, _int("blocking_rounds")),
            repair_rounds=max(0, _int("repair_rounds")),
            advisory_used=max(0, _int("advisory_used")),
            fused=bool(data.get("fused", False)),
            fused_reason=_str("fused_reason"),
            fused_at=_str("fused_at"),
            resume_count=max(0, _int("resume_count")),
            last_signature=_str("last_signature"),
            stall_count=max(0, _int("stall_count")),
            history=[h for h in hist if isinstance(h, dict)],
        )


class LoopGuard:
    """会话级循环熔断守卫（线程内使用；跨实例以磁盘状态为准）。

    用法::

        g = LoopGuard(data_dir="/data/loop", session_id="issue-14")
        d = g.register_round(blocking=1, advisory=3)   # 门禁必修
        if d.action == LoopAction.FUSE.value:
            ...  # 静默交接：create_handoff(..., wake=d.wake) 即 wake=False
    """

    def __init__(
        self,
        session_id: str,
        data_dir: Optional[str] = None,
        max_rounds: int = DEFAULT_MAX_ROUNDS,
        max_advisory_fix_rounds: int = DEFAULT_MAX_ADVISORY_FIX_ROUNDS,
        max_total_rounds: Optional[int] = None,
        max_stall_repeats: int = DEFAULT_MAX_STALL_REPEATS,
    ):
        if not isinstance(session_id, str) or not _SAFE_ID.match(session_id):
            raise ValueError(
                f"非法 session_id: {session_id!r}。仅允许 [A-Za-z0-9_-]。")
        if not isinstance(max_rounds, int) or isinstance(max_rounds, bool) \
                or max_rounds < 0:
            raise ValueError(f"非法 max_rounds: {max_rounds!r}（须为非负整数）。")
        if not isinstance(max_advisory_fix_rounds, int) \
                or isinstance(max_advisory_fix_rounds, bool) \
                or max_advisory_fix_rounds < 0:
            raise ValueError(
                f"非法 max_advisory_fix_rounds: {max_advisory_fix_rounds!r}"
                "（须为非负整数）。")
        if max_total_rounds is None:
            max_total_rounds = max_rounds + max_advisory_fix_rounds + 1
        if not isinstance(max_total_rounds, int) \
                or isinstance(max_total_rounds, bool) or max_total_rounds < 0:
            raise ValueError(
                f"非法 max_total_rounds: {max_total_rounds!r}（须为非负整数）。")

        self.session_id = session_id
        self.data_dir = data_dir or os.path.join(os.getcwd(), "data", "loop")
        if not isinstance(max_stall_repeats, int) \
                or isinstance(max_stall_repeats, bool) or max_stall_repeats < 1:
            raise ValueError(
                f"非法 max_stall_repeats: {max_stall_repeats!r}"
                "（须为 >=1 的整数）。")
        self.max_rounds = max_rounds
        self.max_advisory_fix_rounds = max_advisory_fix_rounds
        self.max_total_rounds = max_total_rounds
        self.max_stall_repeats = max_stall_repeats
        os.makedirs(self.data_dir, exist_ok=True)
        self._state = self._load()

    # ---- 持久化 ----

    def state_path(self) -> str:
        """状态文件路径（``<data_dir>/<session_id>.json``）。"""
        return os.path.join(self.data_dir, f"{self.session_id}.json")

    def _load(self) -> LoopState:
        """读取磁盘状态（损坏/缺失/非对象 → 全新状态）。"""
        path = self.state_path()
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, ValueError):
            return LoopState(session_id=self.session_id)
        return LoopState.from_dict(data, self.session_id)

    def _save(self) -> None:
        """原子落盘（同目录 tmp + os.replace）。"""
        path = self.state_path()
        fd, tmp = tempfile.mkstemp(dir=self.data_dir, prefix=".tmp-", suffix=".json")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(self._state.to_dict(), f, ensure_ascii=False, indent=2)
            os.replace(tmp, path)
        except Exception:
            try:
                os.remove(tmp)
            except OSError:
                pass
            raise

    @classmethod
    def from_handoff(
        cls,
        session_id: str,
        data_dir: Optional[str] = None,
        handoff_context: Any = "",
        **kwargs,
    ) -> "LoopGuard":
        """从交接卡（``wake=False`` 的静默交接）**继承熔断态**。

        接收方（下一个 agent）用本方法起手即熔断，避免"换个 agent 就复活
        循环"：只登记事实、不再唤醒。``handoff_context`` 可为交接卡的
        ``context`` 字段（JSON 字符串/已解析 dict）或交接卡的 ``to_dict()``。

        - 载荷 ``wake`` 为 False → 新守卫直接落在熔断态（轮次计数继承）；
        - 载荷缺失/非静默（``wake`` 为 True）→ 返回未熔断的新守卫（常规接力）；
        - 载荷无法解析（坏 JSON/非 dict）→ 同样返回未熔断新守卫（宽进不炸）。
        """
        payload: Any = handoff_context
        if isinstance(payload, str) and payload.strip():
            try:
                payload = json.loads(payload)
            except ValueError:
                payload = None
        if not isinstance(payload, dict):
            payload = None

        guard = cls(session_id=session_id, data_dir=data_dir, **kwargs)
        if payload is None or payload.get("wake", True) is not False:
            return guard

        st = guard._state
        rounds = payload.get("rounds", {})
        if isinstance(rounds, dict):
            def _n(key: str) -> int:
                v = rounds.get(key, 0)
                return v if isinstance(v, int) and not isinstance(v, bool) else 0
            st.round_no = max(st.round_no, _n("total"))
            st.blocking_rounds = max(st.blocking_rounds, _n("blocking_rounds"))
            st.repair_rounds = max(st.repair_rounds, _n("repair_rounds"))
            st.advisory_used = max(st.advisory_used, _n("advisory_used"))
            st.stall_count = max(st.stall_count, _n("stall_count"))
        st.fused = True
        st.fused_reason = str(payload.get("reason") or "继承静默交接（wake=False）")
        st.fused_at = time.strftime("%Y-%m-%d %H:%M:%S")
        st.history.append({
            "round_no": st.round_no,
            "blocking": 0,
            "advisory": 0,
            "action": LoopAction.FUSE.value,
            "wake": False,
            "note": f"继承静默交接：{st.fused_reason}",
            "at": st.fused_at,
        })
        guard._save()
        return guard

    # ---- 状态读取 ----

    def reload(self) -> LoopState:
        """重新从磁盘加载状态（跨实例/跨进程变更后同步）。"""
        self._state = self._load()
        return self._state

    def is_fused(self) -> bool:
        """是否已熔断（不再允许任何修复轮）。"""
        return self._state.fused

    def should_wake_next(self) -> bool:
        """当前是否允许"唤醒下一轮"（熔断后恒为 False）。"""
        return not self._state.fused

    def status(self) -> dict:
        """当前预算与熔断态快照（供向上汇总/留痕）。"""
        st = self._state
        return {
            "session_id": st.session_id,
            "round_no": st.round_no,
            "blocking_rounds": st.blocking_rounds,
            "repair_rounds": st.repair_rounds,
            "advisory_used": st.advisory_used,
            "fused": st.fused,
            "fused_reason": st.fused_reason,
            "fused_at": st.fused_at,
            "resume_count": st.resume_count,
            "max_rounds": self.max_rounds,
            "max_advisory_fix_rounds": self.max_advisory_fix_rounds,
            "max_total_rounds": self.max_total_rounds,
            "max_stall_repeats": self.max_stall_repeats,
            "stall_count": st.stall_count,
            "last_signature": st.last_signature,
            "wake_allowed": self.should_wake_next(),
            "history": [dict(h) for h in st.history],
        }

    def history(self) -> List[Dict[str, Any]]:
        """轮次处置留痕（round_no/blocking/advisory/action/wake/note/at）。"""
        return [dict(h) for h in self._state.history]

    @staticmethod
    def signature_of(items) -> str:
        """由整改项清单生成**稳定指纹**（用于停滞感知）。

        排序 + 归一化空白后取 SHA-256 前 12 位：与条目顺序无关、与首尾空白无关
        → 同一批问题在两轮之间"换个顺序再报一遍"也会被判为停滞。
        空清单/非可迭代 → 返回空串（不启用停滞判定）。
        """
        try:
            norm = sorted(
                " ".join(str(x).split()) for x in items if str(x).strip()
            )
        except TypeError:
            return ""
        if not norm:
            return ""
        digest = hashlib.sha256("\n".join(norm).encode("utf-8")).hexdigest()
        return digest[:12]

    # ---- 判定 ----

    def preview(self, blocking: int = 0, advisory: int = 0,
                signature: str = "") -> LoopDecision:
        """**纯函数**预览本轮处置（不改状态、不落盘）。

        Args:
            blocking: 本轮 🔴/门禁必修项数
            advisory: 本轮 🟡/非门禁（无必要）项数
            signature: 本轮整改项**指纹**（可选，如"打回项集合的稳定摘要"）。
                与上一轮相同且已达 ``max_stall_repeats`` → 判定**停滞循环**
                直接熔断（不必等轮次预算耗尽）。
        """
        if not isinstance(blocking, int) or isinstance(blocking, bool) or blocking < 0:
            raise ValueError(f"非法 blocking: {blocking!r}（须为非负整数）。")
        if not isinstance(advisory, int) or isinstance(advisory, bool) or advisory < 0:
            raise ValueError(f"非法 advisory: {advisory!r}（须为非负整数）。")
        if not isinstance(signature, str):
            raise ValueError(f"非法 signature: {signature!r}（须为 str）。")

        st = self._state
        next_round = st.round_no + 1
        blocking_left = max(0, self.max_rounds - st.blocking_rounds)
        advisory_left = max(0, self.max_advisory_fix_rounds - st.advisory_used)
        total_left = max(0, self.max_total_rounds - st.repair_rounds)

        # 停滞感知（Issue #14「感知循环」）：本轮签名 == 上轮签名 → 计数 +1
        # （仅当本轮确有修复项、且签名非空时参与判定）
        repair_required_preview = blocking > 0 or advisory > 0
        if signature and repair_required_preview and signature == st.last_signature:
            next_stall = st.stall_count + 1
        elif signature:
            next_stall = 1
        else:
            next_stall = st.stall_count

        def _d(action: str, wake: bool, reason: str,
               bl: Optional[int] = None, al: Optional[int] = None,
               tl: Optional[int] = None) -> LoopDecision:
            return LoopDecision(
                action=action, round_no=next_round, blocking=blocking,
                advisory=advisory, wake=wake,
                budget_left=blocking_left if bl is None else bl,
                advisory_left=advisory_left if al is None else al,
                total_left=total_left if tl is None else tl,
                reason=reason, signature=signature, stall_count=next_stall,
            )

        # 1) 已熔断：任何轮次都不再唤醒（须人工 resume）
        if st.fused:
            return _d(LoopAction.FUSE.value, False,
                      f"已熔断（{st.fused_reason}），须人工 resume 才恢复")

        repair_required = blocking > 0 or advisory > 0

        # 1.5) 停滞感知：同一整改项指纹反复出现（无进展）→ 立即熔断
        #      （"感知循环"：同一问题反复报，每轮都算新的一轮 → 不靠轮次也能挡住）
        if repair_required and signature and next_stall >= self.max_stall_repeats:
            return _d(LoopAction.FUSE.value, False,
                      f"停滞循环：同一整改项指纹连续出现 {next_stall} 次"
                      f"（上界 {self.max_stall_repeats}），无实质进展")

        # 2) 修复总轮次硬上界（防门禁项/非必要项交替绕过前两道上界）
        if repair_required and total_left <= 0:
            return _d(LoopAction.FUSE.value, False,
                      f"修复总轮次已达上界 {self.max_total_rounds}",
                      tl=0)

        # 3) 门禁修复上界：🔴 出现即占门禁预算，耗尽则熔断
        if blocking > 0:
            if blocking_left <= 0:
                return _d(LoopAction.FUSE.value, False,
                          f"门禁修复轮次已达上界 {self.max_rounds}", bl=0)
            return _d(LoopAction.FIX.value, True,
                      "门禁必修项，预算内允许修复并唤醒复评",
                      bl=blocking_left - 1, tl=max(0, total_left - 1))

        # 4) 非必要修复上界：仅剩 🟡 时**最多修 1 次**
        if advisory > 0:
            if advisory_left <= 0:
                return _d(LoopAction.FUSE.value, False,
                          f"非必要修复已达上界 {self.max_advisory_fix_rounds} 次"
                          "（无必要修复项不再产生新一轮）", al=0)
            return _d(LoopAction.FIX_LAST.value, True,
                      "仅非必要项，允许最后一次修复",
                      al=advisory_left - 1, tl=max(0, total_left - 1))

        # 5) 无必修无建议：无需修复，不唤醒、不消耗预算
        return _d(LoopAction.CLEAR.value, False, "无必修/建议项：无需修复")

    def register_round(self, blocking: int = 0, advisory: int = 0,
                       note: str = "", signature: str = "") -> LoopDecision:
        """登记一轮评审/复评结果并提交状态（含落盘）。

        Returns:
            LoopDecision：``action`` 为 ``clear``/``fix``/``fix_last``/``fuse``；
            ``fused_now=True`` 表示本次调用**刚刚**熔断（调用方据此只建一次
            静默交接卡，避免重复建卡）。
        """
        decision = self.preview(blocking, advisory, signature=signature)
        st = self._state
        st.round_no += 1
        # 停滞计数与指纹提交（仅在确有修复项时推进，CLEAR 不污染指纹）
        if blocking > 0 or advisory > 0:
            if signature:
                st.stall_count = decision.stall_count
                st.last_signature = signature

        if decision.action == LoopAction.FIX.value:
            st.blocking_rounds += 1
            st.repair_rounds += 1
            # 门禁轮重置非必要计数（上下文已变，前次非必要修复不再累计）
            st.advisory_used = 0
        elif decision.action == LoopAction.FIX_LAST.value:
            st.advisory_used += 1
            st.repair_rounds += 1
        elif decision.action == LoopAction.FUSE.value:
            fused_now = not st.fused
            st.fused = True
            if fused_now:
                st.fused_reason = decision.reason
                st.fused_at = time.strftime("%Y-%m-%d %H:%M:%S")
            decision.fused_now = fused_now

        st.history.append({
            "round_no": st.round_no,
            "blocking": blocking,
            "advisory": advisory,
            "action": decision.action,
            "wake": decision.wake,
            "note": note if isinstance(note, str) else "",
            "signature": signature,
            "stall_count": decision.stall_count,
            "at": time.strftime("%Y-%m-%d %H:%M:%S"),
        })
        self._save()
        return decision

    def fuse(self, reason: str) -> bool:
        """**显式**熔断（人工/上游直接叫停循环）。已熔断返回 False。"""
        st = self._state
        if st.fused:
            return False
        st.fused = True
        st.fused_reason = reason if isinstance(reason, str) and reason \
            else "显式熔断"
        st.fused_at = time.strftime("%Y-%m-%d %H:%M:%S")
        st.history.append({
            "round_no": st.round_no,
            "blocking": 0,
            "advisory": 0,
            "action": LoopAction.FUSE.value,
            "wake": False,
            "note": f"显式熔断: {st.fused_reason}",
            "at": st.fused_at,
        })
        self._save()
        return True

    def resume(self, by: str, reason: str = "") -> bool:
        """**人工解锁**熔断（须留痕：``by`` 为人/角色标识，不可为空）。

        解锁语义 = **重置修复预算**（``blocking_rounds``/``advisory_used``/
        ``repair_rounds`` 归零）：预算本已耗尽，只翻 ``fused`` 标志位的话下一轮
        立即再次熔断，解锁无实际意义。``round_no`` 与 ``history`` **保留**，
        并累计 ``resume_count``，供审计"循环被人工放开过几次、以何理由"。
        """
        if not isinstance(by, str) or not by.strip():
            raise ValueError("resume 须提供非空 by（人工解锁留痕，禁止匿名复活）。")
        st = self._state
        if not st.fused:
            return False
        st.fused = False
        st.fused_reason = ""
        st.fused_at = ""
        st.resume_count += 1
        # 重置修复预算（保留 round_no/history 作审计轨迹）
        st.blocking_rounds = 0
        st.advisory_used = 0
        st.repair_rounds = 0
        st.stall_count = 0
        st.last_signature = ""
        st.history.append({
            "round_no": st.round_no,
            "blocking": 0,
            "advisory": 0,
            "action": "resume",
            "wake": True,
            "note": f"人工解锁 by={by.strip()}: {reason or '(未填原因)'}",
            "at": time.strftime("%Y-%m-%d %H:%M:%S"),
        })
        self._save()
        return True

    def delete_state(self) -> bool:
        """删除本会话循环状态文件（会话生命周期回收）。"""
        path = self.state_path()
        try:
            os.remove(path)
            return True
        except OSError:
            return False

    def silent_handoff_payload(self, open_items: Optional[List[str]] = None,
                               target_unit: str = "") -> dict:
        """构造**静默交接**（不唤醒）载荷，供交接卡/评论留痕使用。

        ``wake=False`` 为核心字段：接收方只登记、不唤醒，不得据此再开一轮。
        """
        st = self._state
        return {
            "session_id": st.session_id,
            "wake": False,
            "fused": st.fused,
            "reason": st.fused_reason or "未熔断（按当前预算仍可修复）",
            "rounds": {
                "total": st.round_no,
                "blocking_rounds": st.blocking_rounds,
                "repair_rounds": st.repair_rounds,
                "advisory_used": st.advisory_used,
                "stall_count": st.stall_count,
            },
            "budget": {
                "max_rounds": self.max_rounds,
                "max_advisory_fix_rounds": self.max_advisory_fix_rounds,
                "max_total_rounds": self.max_total_rounds,
                "max_stall_repeats": self.max_stall_repeats,
            },
            "target_unit": target_unit,
            "open_items": list(open_items or []),
            "resume_policy": "须人工 resume(by=..., reason=...) 才恢复修复预算",
        }
