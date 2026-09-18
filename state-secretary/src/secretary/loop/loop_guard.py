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

## 跨容器 / 跨棒预算继承（**预算不归零**）

磁盘状态只在**同一数据根**（同容器/同卷）内有效。平台 `crontab` 代跑、跨仓
接力、换 agent 都会**开新容器、新工作区**——磁盘上的 ``round_no`` 读不到，
"最多 3 轮"就永远不会触发（这正是"软约束换了个位置"）。

因此预算必须**随载荷走**，落盘只是**同容器内的加速缓存**：

- ``budget_payload()`` / ``budget_trail()``：把轮次快照导出为 dict / 评论留痕标记
  （``<!-- loop-budget: {...} -->``），随交接卡 ``context``、探测事件载荷或
  评论正文传递；
- ``adopt_budget(payload)``：**``max()`` 合并**外部轮次（只增不减，不因新容器
  重置），并保守继承载荷里的熔断标记；载荷可以是 dict / JSON 串 / 含标记的评论正文；
- ``from_handoff()`` **不论 wake 真假都先合并预算**：静默交接继承熔断态，
  常规接力也把轮次带过来（否则"换一棒重置一次预算"照样能把 3 轮变成 N 轮）。

**边界（明示）**：若调用方**既不给载荷、又换数据根**，则预算只能从 0 开始——
这是平台执行模型的边界，不是"实现疏忽"。故门禁方必须在交接/再触发时**带上载荷**：
``SecretarySession.silent_handoff()`` / ``review_round(external_rounds=…)`` /
``inherit_budget(…)`` 三个入口已接线。

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

# 评论/正文里的预算留痕标记（跨容器传递轮次预算的**可持久载体**）：
# 交接卡 context、PR/Issue 评论正文里带这一行即可被 `adopt_budget()` 识别。
#
# **取全部留痕、按 `max()` 合并**（复评第 3 轮 🔴1）：评论正文是"每棒把留痕写回
# 评论"的**累积载体**，容器回收后正文里必然是多条留痕叠加。只取**第一条** =
# 拿最旧的那次轮次 → 上界被稀释（"最多 3 轮 / 6 次"永不触发）。故这里**收集
# 全部**留痕再逐字段 `max()`，与"留痕在前 / 在后"两种顺序都无关。
BUDGET_TRAIL_RE = re.compile(r'<!--\s*loop-budget:\s*(\{.*?\})\s*-->', re.S)

# 载荷解析的**深度上界**（🟡1）：跨仓/评论载荷不可信，深嵌套 JSON（~3000 层）
# 会让递归解析抛 `RecursionError` 并逃逸到门面。超界一律按"判不出"处理。
MAX_COERCE_DEPTH = 8

# `wake` 的**严格**判定口径（与 HandoffBus.create_handoff 的写出侧一致）：
#   显式静默（严格 False）→ 静默；显式唤醒（严格 True）→ 唤醒；
#   其余（"false"/0/"no"/缺失…）→ **判不出** → 按**静默**处理并留痕。
# 为什么判不出要取保守方向：读入侧放宽等于把熔断语义漏出去（写出侧严、读入侧松），
# 一个 `wake="false"` 就能复活循环——比"误静默"更糟（误静默只多等一次人工）。
WAKE_SILENT = "silent"
WAKE_AWAKE = "awake"
WAKE_UNKNOWN = "unknown"


def _balanced_object_end(text: str, start: int) -> Optional[int]:
    """从 ``text[start] == '{'`` 起按**括号配平**找出对象结束位置（跳过字符串内括号）。

    取不到（未闭合 / 空）→ ``None``。纯迭代实现，**不做递归**（深嵌套不炸栈）。
    """
    depth = 0
    in_str = False
    escaped = False
    for i in range(start, len(text)):
        ch = text[i]
        if in_str:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return i
    return None


def extract_json_objects(text: str) -> List[dict]:
    """从正文里取出**全部**顶层 JSON 对象（宽进：解析不出的片段跳过）。

    **为什么要"全部"**（复评第 3 轮 🔴2）：评论正文常见形态是
    「预算留痕 + 交接卡 context」拼接，而留痕在前/在后两种顺序都存在。
    只取**第一个**对象时，"留痕在前"就会把静默交接的 ``wake=False`` 读丢 →
    ``should_wake_next()=True``（熔断语义从新入口漏出去，且方向是**放行**）。
    """
    out: List[dict] = []
    if not isinstance(text, str):
        return out
    i = 0
    while i < len(text):
        start = text.find("{", i)
        if start < 0:
            break
        end = _balanced_object_end(text, start)
        if end is None:
            i = start + 1
            continue
        try:
            got = json.loads(text[start:end + 1])
        except (ValueError, RecursionError):
            got = None
        if isinstance(got, dict):
            out.append(got)
        i = end + 1
    return out


def extract_json_object(text: str) -> Optional[dict]:
    """从正文里取出**第一个** JSON 对象（宽进：取不到 → ``None``）。

    保留本入口供既有调用方使用；判定"是不是静默交接"请用
    ``extract_json_objects()`` + ``pick_wake_object()``——只认第一个对象会把
    "留痕在前"的静默交接读丢（见 🔴2）。
    """
    objs = extract_json_objects(text)
    return objs[0] if objs else None


def pick_wake_object(objects: List[dict]) -> Optional[dict]:
    """在多个顶层对象里挑**含 ``wake`` 键**的那个（找不到 → ``None``）。

    ``wake`` 是静默交接的**判据字段**，故解析时**优先找它**，而不是盲取第一个：
    "留痕在前"时第一个对象是预算留痕（不带 ``wake``），盲取 = 判不出静默交接。
    全都找不到 ``wake`` → 返回 ``None``，调用方按"非静默交接"处理（普通 context
    不误熔断）；而"带 ``wake`` 但值非严格 bool"仍走**保守静默**（见 ``parse_wake``）。
    """
    for obj in objects:
        if isinstance(obj, dict) and "wake" in obj:
            return obj
    return None


def parse_wake(value: Any) -> str:
    """把载荷里的 ``wake`` 解析成三态（**只认严格 bool**）。

    - 严格 ``False`` → ``WAKE_SILENT``（静默交接，不唤醒）；
    - 严格 ``True`` → ``WAKE_AWAKE``（常规接力）；
    - 缺失/字符串/数字/其它 → ``WAKE_UNKNOWN``（**判不出**，调用方取保守方向）。

    与 ``HandoffBus.create_handoff`` 的 ``wake`` 严格 bool 校验**同口径**：
    写出侧拒绝 ``"false"``，读入侧也不得把它当成真值。
    """
    if value is True:
        return WAKE_AWAKE
    if value is False:
        return WAKE_SILENT
    return WAKE_UNKNOWN

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
    # 本轮结束后的**预算留痕**（``<!-- loop-budget: … -->``）：调用方把它写进
    # 评论/交接卡 context，下一棒（可能是新容器）据此恢复轮次，不必共享磁盘。
    budget_trail: str = ""

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
            "budget_trail": self.budget_trail,
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
        """字段级容错恢复：非 dict / 字段类型不符 → 退化为默认值（不抛异常）。

        **例外**：``fused`` 是安全旗标，非严格 ``False`` 一律按**已熔断**（🔴4）
        ——"判不出"取保守方向，不得把熔断语义放宽掉。
        """
        if not isinstance(data, dict):
            return cls(session_id=session_id)

        def _int(key: str) -> int:
            v = data.get(key, 0)
            return v if isinstance(v, int) and not isinstance(v, bool) else 0

        def _str(key: str) -> str:
            v = data.get(key, "")
            return v if isinstance(v, str) else ""

        def _fused_flag() -> bool:
            """``fused`` 的**保守**读取（🔴4，方向与 🔴B 同族）。

            ``fused`` 是**安全旗标**：保守方向应是"判不出 → 按**已熔断**"，
            而不是"判不出 → 按未熔断"。旧实现 ``bool(...)`` 把 ``fused: 1``
            视为**已熔断**；若改成"非 bool → False"，一张坏卡（``fused: 1``）
            就能把熔断**清掉** → ``should_wake_next()=True`` → 循环复活
            （与同 PR 自己的判据"读入侧放宽等于把熔断语义漏出去"冲突）。

            故：严格 ``False`` → 未熔断；**其余任何形态**（严格 ``True`` /
            ``1`` / ``"yes"`` / 缺失 / 坏值）→ 一律按**已熔断**处理。
            """
            return data.get("fused") is not False

        hist = data.get("history", [])
        if not isinstance(hist, list):
            hist = []
        return cls(
            session_id=session_id,
            round_no=max(0, _int("round_no")),
            blocking_rounds=max(0, _int("blocking_rounds")),
            repair_rounds=max(0, _int("repair_rounds")),
            advisory_used=max(0, _int("advisory_used")),
            fused=_fused_flag(),
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
        """从交接卡**继承熔断态与轮次预算**（换 agent / 换容器不归零）。

        接收方（下一个 agent）用本方法起手即熔断，避免"换个 agent 就复活
        循环"：只登记事实、不再唤醒。``handoff_context`` 可为交接卡的
        ``context`` 字段（JSON 字符串/已解析 dict/含预算标记的正文）或交接卡的
        ``to_dict()``。

        判定口径（**只认严格 bool**，与 ``HandoffBus.create_handoff`` 的写出侧一致）：

        - 载荷带 ``wake`` 且严格 ``False`` → 落在熔断态，轮次计数一并继承；
        - 载荷带 ``wake`` 且严格 ``True`` → 常规接力（不熔断），但**轮次预算
          仍然合并**（否则"每棒从 0 开始"照样能把 3 轮稀释成 N 轮）；
        - 载荷带 ``wake`` 但**非严格 bool**（``"false"``/``0``/``"no"``…）
          → **判不出**，取**保守方向**：按静默处理（熔断 + 留痕）。
          读入侧放宽等于把熔断语义漏出去——一个 ``wake="false"`` 就能复活循环；
        - 载荷**不带** ``wake`` 键 / 无法解析 → 非静默交接（常规接力）；
          静默交接载荷**必带** ``wake``（见 ``silent_handoff_payload``），
          故"缺失"不构成"判不出"（不能对普通 context 误熔断）。

        **拼接顺序无关**（复评第 3 轮 🔴2）：``handoff_context`` 是评论正文时，
        常见形态是「留痕 + 交接卡 context」或「context + 留痕」两种顺序。解析按
        **全部顶层对象**进行并**优先取含 ``wake`` 的那个**，故两种顺序判定一致
        （旧实现只取第一个对象 → "留痕在前"时 ``wake=False`` 读不到 →
        ``should_wake_next()=True``，熔断语义从新入口漏出去，且方向是**放行**）。
        """
        raw: Any = handoff_context
        payload: Any = raw
        if isinstance(raw, str) and raw.strip():
            try:
                payload = json.loads(raw)
            except ValueError:
                # 常见形态：JSON 载荷 + `<!-- loop-budget: … -->` 留痕拼接。
                # 🔴2：**优先取含 `wake` 键的对象**——正文里"留痕在前"时第一个
                # 对象是预算留痕（不带 `wake`），盲取第一个 = 判不出静默交接。
                payload = pick_wake_object(extract_json_objects(raw))
        if not isinstance(payload, dict):
            payload = None

        guard = cls(session_id=session_id, data_dir=data_dir, **kwargs)
        # 轮次预算**无条件合并**（常规接力也要带，见上"跨容器/跨棒预算继承"）。
        # 预算来源取**整段正文**（🔴1：正文里留痕会累积多条，`adopt_budget` 会
        # 收集全部并 `max()` 合并），而不是只取 wake 对象那一个——否则"留痕段 +
        # 不带内嵌留痕的载荷"拼接形态会把预算丢掉。
        budget_src = raw if isinstance(raw, str) and raw.strip() else payload
        if budget_src is not None:
            guard.adopt_budget(budget_src, source="handoff", save=False)

        has_wake = payload is not None and "wake" in payload
        wake_state = parse_wake(payload.get("wake")) if has_wake else None
        if wake_state == WAKE_AWAKE or not has_wake:
            guard._save()
            return guard

        # 严格 False（静默）与**非 bool**（判不出）都取保守方向：不唤醒。
        st = guard._state
        if wake_state == WAKE_UNKNOWN:
            detail = (f"载荷 wake 非严格 bool（{payload.get('wake')!r}）→ "
                      "保守按静默处理（不得默认「可唤醒」）")
        else:
            detail = "继承静默交接（wake=False）"
        st.fused = True
        reason = payload.get("reason")
        st.fused_reason = (reason if isinstance(reason, str) and reason
                           else detail)
        st.fused_at = time.strftime("%Y-%m-%d %H:%M:%S")
        st.history.append({
            "round_no": st.round_no,
            "blocking": 0,
            "advisory": 0,
            "action": LoopAction.FUSE.value,
            "wake": False,
            "note": f"继承静默交接：{detail}｜{st.fused_reason}",
            "at": st.fused_at,
        })
        guard._save()
        return guard

    # ---- 预算载荷（跨容器 / 跨棒继承：预算不归零） ----

    def budget_payload(self) -> dict:
        """导出**轮次预算快照**（随交接卡/事件载荷/评论留痕走的那一份）。

        与 ``silent_handoff_payload()`` 的区别：本方法只带**预算与熔断事实**，
        不带静默交接的 ``wake=False`` 语义——常规接力也应当带上它，
        否则"换一棒重置一次预算"照样能把 3 轮稀释成 N 轮。
        """
        st = self._state
        return {
            "session_id": st.session_id,
            "rounds": {
                "total": st.round_no,
                "blocking_rounds": st.blocking_rounds,
                "repair_rounds": st.repair_rounds,
                "advisory_used": st.advisory_used,
                "stall_count": st.stall_count,
            },
            "fused": st.fused,
            "reason": st.fused_reason,
            "budget": {
                "max_rounds": self.max_rounds,
                "max_advisory_fix_rounds": self.max_advisory_fix_rounds,
                "max_total_rounds": self.max_total_rounds,
                "max_stall_repeats": self.max_stall_repeats,
            },
        }

    def budget_trail(self) -> str:
        """**评论/正文留痕**形态的预算载荷（``<!-- loop-budget: {...} -->``）。

        可持久载体 = 评论正文 / 交接卡 context：容器会被回收，评论不会。
        接收方（新容器）用 ``adopt_budget(该段文本)`` 即恢复轮次，不必共享磁盘。
        """
        return ("<!-- loop-budget: "
                + json.dumps(self.budget_payload(), ensure_ascii=False,
                             sort_keys=True)
                + " -->")

    @staticmethod
    def _fused_of(data: Any) -> bool:
        """载荷里的 ``fused`` 是否表示**已熔断**（**保守方向**，与 🔴4 同族）。

        - 键不存在 → 不表态（``False``，不得凭"没这个字段"就熔断）；
        - 严格 ``False`` → 未熔断；
        - **其余任何形态**（``True`` / ``1`` / ``"yes"`` / 坏值）→ **按已熔断**
          （"判不出 → 取保守方向"：读入侧放宽等于把熔断语义漏出去）。
        """
        if not isinstance(data, dict) or "fused" not in data:
            return False
        return data.get("fused") is not False

    @staticmethod
    def _merge_budget_dicts(dicts: Any, _depth: int = 0) -> Optional[dict]:
        """把**多份**预算 dict 合并成一份（逐字段 ``max()``，``fused`` 取或）。

        🔴1：评论正文里留痕会**累积多条**（每棒把留痕写回评论 = 必做项）。
        只取第一条 = 拿最旧的轮次 → 上界被稀释。故全部取、按 ``max()`` 合并，
        与"留痕在前/在后"的顺序无关。

        内嵌的 ``budget_trail``（``<!-- loop-budget: … -->``）**递归展开后再合并**
        ——交接卡 context 里的预算留痕是内嵌字段，不是单独一段正文。
        深度超过 ``MAX_COERCE_DEPTH`` 一律不展开（跨仓载荷不可信）。
        """
        if _depth > MAX_COERCE_DEPTH:
            return None
        merged: Dict[str, Any] = {}
        rounds_out: Dict[str, Any] = {}
        reason_out = ""
        nested: List[Any] = []
        for d in dicts:
            if not isinstance(d, dict):
                continue
            rounds = d.get("rounds")
            if isinstance(rounds, dict):
                for k, v in rounds.items():
                    if isinstance(v, int) and not isinstance(v, bool):
                        rounds_out[k] = max(rounds_out.get(k, 0), v)
            if LoopGuard._fused_of(d):
                merged["fused"] = True
                reason = d.get("reason")
                if isinstance(reason, str) and reason and not reason_out:
                    reason_out = reason
            budget = d.get("budget")
            if isinstance(budget, dict) and "budget" not in merged:
                merged["budget"] = dict(budget)
            trail = d.get("budget_trail")
            if isinstance(trail, str) and trail:
                nested.append(LoopGuard._coerce_budget_payload(
                    trail, _depth + 1))
        if nested:
            inner = LoopGuard._merge_budget_dicts(nested, _depth + 1)
            if inner is not None:
                for k, v in (inner.get("rounds") or {}).items():
                    rounds_out[k] = max(rounds_out.get(k, 0), v)
                if inner.get("fused") is True:
                    merged["fused"] = True
                    if not reason_out:
                        reason_out = inner.get("reason") or ""
                if isinstance(inner.get("budget"), dict) \
                        and "budget" not in merged:
                    merged["budget"] = dict(inner["budget"])
        if rounds_out:
            merged["rounds"] = rounds_out
        if reason_out:
            merged["reason"] = reason_out
        return merged or None

    @staticmethod
    def _coerce_budget_payload(payload: Any,
                               _depth: int = 0) -> Optional[dict]:
        """把任意形态的载荷解析成预算 dict（宽进：解析不出 → None，不炸）。

        接受：dict / JSON 串 / 含 ``<!-- loop-budget: … -->`` 标记的正文
        （评论、交接卡 context、事件 ``facts`` 都能直接喂进来）。

        **多条留痕 → ``max()`` 合并**（🔴1）：评论正文是"每棒写回留痕"的累积
        载体，正文里必然叠加多条留痕；只取第一条会让轮次回退到最旧那次，
        "最多 3 轮 / 6 次"永不触发。故**收集全部**再合并，顺序无关。

        **深度上界**（🟡1）：跨仓载荷不可信，深嵌套 JSON 会触发 ``RecursionError``
        并逃逸到门面；超过 ``MAX_COERCE_DEPTH`` 一律按"判不出"（``None``）处理。
        """
        if _depth > MAX_COERCE_DEPTH:
            return None
        if isinstance(payload, dict):
            # 内嵌 `budget_trail`（交接卡 context 的形态）也要展开并合并
            trail = payload.get("budget_trail")
            if isinstance(trail, str) and trail:
                inner = LoopGuard._coerce_budget_payload(trail, _depth + 1)
                if inner is not None:
                    return LoopGuard._merge_budget_dicts(
                        [payload, inner], _depth + 1) or payload
            return payload
        if not isinstance(payload, str) or not payload.strip():
            return None

        # ① 正文里的**全部** `<!-- loop-budget: … -->` 留痕 → 合并（🔴1）
        merged = LoopGuard._merge_budget_dicts(
            LoopGuard._coerce_budget_payload(m.group(1), _depth + 1)
            for m in BUDGET_TRAIL_RE.finditer(payload))
        if merged is not None and isinstance(merged.get("rounds"), dict):
            return merged

        # ② 整段就是一份 JSON
        try:
            got: Any = json.loads(payload)
        except (ValueError, RecursionError):
            got = None
        if not isinstance(got, dict):
            # ③ 正文里散落的多个顶层对象 → 合并（顺序无关；内嵌留痕递归展开）
            got = LoopGuard._merge_budget_dicts(
                extract_json_objects(payload), _depth + 1)
        if not isinstance(got, dict):
            return merged
        if not isinstance(got.get("rounds"), dict) and merged is not None:
            # ④ 合并结果与 ③ 互补（如"留痕段 + 不带 rounds 的载荷"）
            got = LoopGuard._merge_budget_dicts([got, merged], _depth + 1) or got
        # 载荷内嵌的 `budget_trail`（`<!-- loop-budget: … -->` 形态）：递归取内层，
        # 与载荷自身的 rounds **合并**（取 max）——不是替换（否则内层较小值时回退）。
        nested = got.get("budget_trail")
        if isinstance(nested, str):
            inner = LoopGuard._coerce_budget_payload(nested, _depth + 1)
            if inner is not None:
                got = LoopGuard._merge_budget_dicts(
                    [got, inner], _depth + 1) or got
        return got

    def adopt_budget(self, payload: Any, source: str = "",
                     save: bool = True) -> dict:
        """**合并外部轮次预算**（``max()`` 只增不减）→ 换容器/换棒不归零。

        Args:
            payload: ``budget_payload()`` 的 dict / JSON 串 / 含
                ``<!-- loop-budget: … -->`` 的评论正文；``None``/解析失败 → 空操作。
            source: 留痕来源（如 ``"handoff"`` / ``"review_round"`` / ``"comment"``）。
            save: 是否立即落盘（``from_handoff`` 内部合并时由调用方统一落盘）。

        Returns:
            合并结果摘要 ``{"merged": bool, "adopted": {...}, "reason": str}``。
            载荷里的 ``fused`` **非严格 ``False``**（``True``/``1``/``"yes"``/坏值）
            时**保守继承熔断**（宁多一次人工解锁，也不放一个可能已超预算的循环
            继续跑）；键缺失则不表态（不凭"没这个字段"就熔断）。
        """
        data = self._coerce_budget_payload(payload)
        if data is None:
            return {"merged": False, "adopted": {}, "reason": "载荷无法解析（无操作）"}

        st = self._state
        fields = ("round_no", "blocking_rounds", "repair_rounds",
                  "advisory_used", "stall_count")
        before = {k: getattr(st, k) for k in fields}
        rounds = data.get("rounds")
        if not isinstance(rounds, dict):
            rounds = {}

        def _n(key: str) -> int:
            v = rounds.get(key, 0)
            return max(0, v) if isinstance(v, int) and not isinstance(v, bool) else 0

        st.round_no = max(st.round_no, _n("total"))
        st.blocking_rounds = max(st.blocking_rounds, _n("blocking_rounds"))
        st.repair_rounds = max(st.repair_rounds, _n("repair_rounds"))
        st.advisory_used = max(st.advisory_used, _n("advisory_used"))
        st.stall_count = max(st.stall_count, _n("stall_count"))

        # 载荷里的 `fused` 取**保守方向**（🔴4 同族）：非严格 `False` 一律按已熔断。
        fused_from_payload = self._fused_of(data)
        if fused_from_payload and not st.fused:
            st.fused = True
            reason = data.get("reason")
            st.fused_reason = (reason if isinstance(reason, str) and reason
                               else "继承外部预算载荷（fused=True）")
            st.fused_at = time.strftime("%Y-%m-%d %H:%M:%S")

        adopted = {k: getattr(st, k) for k in fields}
        changed = {k: v for k, v in adopted.items() if v != before[k]}
        note = (f"合并外部预算（source={source or '未标注'}）："
                f"rounds={adopted}，fused={st.fused}")
        st.history.append({
            "round_no": st.round_no,
            "blocking": 0,
            "advisory": 0,
            "action": "adopt_budget",
            "wake": not st.fused,
            "note": note,
            "at": time.strftime("%Y-%m-%d %H:%M:%S"),
        })
        if save:
            self._save()
        return {"merged": True, "adopted": adopted, "changed": changed,
                "fused": st.fused, "reason": note}

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
                       note: str = "", signature: str = "",
                       external_rounds: Any = None) -> LoopDecision:
        """登记一轮评审/复评结果并提交状态（含落盘）。

        Args:
            external_rounds: **外部轮次预算载荷**（``budget_payload()`` /
                ``budget_trail()`` / 含 ``<!-- loop-budget: … -->`` 的评论正文）。
                登记前先 ``max()`` 合并——跨容器/跨棒接力时，本容器磁盘上的
                轮次从 0 开始，若不合并外部载荷，「最多 3 轮」永远不会触发。

        Returns:
            LoopDecision：``action`` 为 ``clear``/``fix``/``fix_last``/``fuse``；
            ``fused_now=True`` 表示本次调用**刚刚**熔断（调用方据此只建一次
            静默交接卡，避免重复建卡）；``budget_trail`` 为本轮结束后的
            **可持久留痕**（写进评论即完成跨容器继承）。
        """
        if external_rounds is not None:
            self.adopt_budget(external_rounds, source="review_round", save=False)
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
        decision.budget_trail = self.budget_trail()
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
