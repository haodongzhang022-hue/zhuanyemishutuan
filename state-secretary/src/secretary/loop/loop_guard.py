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

# 留痕收集 / 归属过滤的**公共实现**（🟢 复评第 5 轮）：与 `collab.probe` 共用
# 同一份，避免两处再次分叉——🔴1「只取第一条」正是分叉里长出来的缺陷。
from ..trail import (  # noqa: E402
    DEFAULT_SCAN_BUDGET,
    MAX_TRAIL_DEPTH,
    attributed,
    collect_marked,
    json_objects_in,
    json_objects_in_diag,
    json_objects_in_report,
    trail_re,
)

# 载荷解析的**深度上界**：跨仓/评论载荷不可信，深嵌套 JSON 会触发
# ``RecursionError`` 并逃逸到门面；超界一律按"判不出"处理（不炸）。
#
# 🟡（复评第 4 轮）**单一语义源**：这里不再 `as MAX_COERCE_DEPTH` 另起一个名字
# （旧写法在 `loop_guard` / `probe` 各留一个同值别名，看着像两套口径）。
# 上界的**唯一定义处**是 `secretary.trail.MAX_TRAIL_DEPTH`；兼容别名只在
# 包出口（`loop.__init__` / `collab.__init__`）保留，指向同一个对象。
MAX_COERCE_DEPTH = MAX_TRAIL_DEPTH

# 评论/正文里的预算留痕标记（跨容器传递轮次预算的**可持久载体**）：
# 交接卡 context、PR/Issue 评论正文里带这一行即可被 `adopt_budget()` 识别。
#
# **取全部留痕、按 `max()` 合并**（复评第 3 轮 🔴1）：评论正文是"每棒把留痕写回
# 评论"的**累积载体**，容器回收后正文里必然是多条留痕叠加。只取**第一条** =
# 拿最旧的那次轮次 → 上界被稀释（"最多 3 轮 / 6 次"永不触发）。故这里**收集
# 全部**留痕再逐字段 `max()`，与"留痕在前 / 在后"两种顺序都无关。
BUDGET_TRAIL_RE = trail_re("loop-budget")

# `wake` 的**严格**判定口径（与 HandoffBus.create_handoff 的写出侧一致）：
#   显式静默（严格 False）→ 静默；显式唤醒（严格 True）→ 唤醒；
#   其余（"false"/0/"no"/缺失…）→ **判不出** → 按**静默**处理并留痕。
# 为什么判不出要取保守方向：读入侧放宽等于把熔断语义漏出去（写出侧严、读入侧松），
# 一个 `wake="false"` 就能复活循环——比"误静默"更糟（误静默只多等一次人工）。
WAKE_SILENT = "silent"
WAKE_AWAKE = "awake"
WAKE_UNKNOWN = "unknown"


def extract_json_objects(text: str, budget: Optional[int] = None) -> List[dict]:
    """从正文里取出**全部**顶层 JSON 对象（宽进：解析不出的片段跳过）。

    **为什么要"全部"**（复评第 3 轮 🔴2）：评论正文常见形态是
    「预算留痕 + 交接卡 context」拼接，而留痕在前/在后两种顺序都存在。
    只取**第一个**对象时，"留痕在前"就会把静默交接的 ``wake=False`` 读丢 →
    ``should_wake_next()=True``（熔断语义从新入口漏出去，且方向是**放行**）。

    **复杂度**（🔴5 复评第 4 轮）：扫描实现收敛到 ``secretary.trail``
    （``scan_json_spans``）——候选 ``{`` **独立起算**（``raw_decode``），未闭合
    ``{`` 不再触发"每个起点重扫文末"的 O(n²)（``"{" * 40000`` 从 63 s 降到毫秒级，
    见该函数 docstring）。``budget`` 缺省用 ``DEFAULT_SCAN_BUDGET``
    （超长正文只扫前一段，保守不表态）。
    """
    if not isinstance(text, str) or not text:
        return []
    return json_objects_in(text, DEFAULT_SCAN_BUDGET if budget is None else budget)


def extract_json_objects_report(
        text: str, budget: Optional[int] = None) -> "tuple[List[dict], bool]":
    """``extract_json_objects`` 的**带降级标记**版本：``(对象清单, degraded)``。

    复评第 5 轮总师裁定"预算触顶**只允许降级为该区间不表态**，**不得**丢掉整段
    结果；降级须**显式标记**"。本入口即该标记的来源；判定侧（``from_handoff`` /
    ``pick_wake_object``）据此把安全旗标取**保守方向**——否则"降级"本身又是一条
    fail-open（上一轮 `MAX_REPAIR_CHARS` 静默收摊就是这么漏卡的）。
    """
    if not isinstance(text, str) or not text:
        return [], False
    return json_objects_in_report(
        text, DEFAULT_SCAN_BUDGET if budget is None else budget)


def extract_json_objects_diag(
        text: str, budget: Optional[int] = None) -> "tuple[List[dict], bool, str]":
    """``extract_json_objects`` 的**带降级原因**版本：``(对象清单, degraded, reason)``。

    ``reason`` 三态（``truncated`` / ``work_cap`` / ``rescan_len``，见
    ``secretary.trail.SCAN_REASON_*``）。判定侧据此把安全旗标取**保守方向**，
    并把**原因**写进 ``fused_reason``——只标记"降级了"而不说明"哪一段没表态"
    会让下一棒看不出该怎么复核（本仓库把"声明了不接线"当缺陷类）。
    """
    if not isinstance(text, str) or not text:
        return [], False, ""
    return json_objects_in_diag(
        text, DEFAULT_SCAN_BUDGET if budget is None else budget)


def extract_json_object(text: str) -> Optional[dict]:
    """从正文里取出**第一个** JSON 对象（宽进：取不到 → ``None``）。

    保留本入口的**原因**（🟢 复评第 4 轮点名"现已无调用方"）：
    它是本模块**对外稳定 API**（``secretary.loop.__all__`` 里导出），语义 =
    "取第一个顶层对象"，与 ``extract_json_objects()[0]`` **同源实现**
    （不另写一份扫描）。**内部判定**（静默交接）一律用
    ``extract_json_objects()`` + ``pick_wake_object()``：只认第一个对象会把
    "留痕在前"的静默交接读丢（🔴2）。
    """
    objs = extract_json_objects(text)
    return objs[0] if objs else None


def pick_wake_object(objects: List[dict]) -> Optional[dict]:
    """在多个顶层对象里挑出**决定唤醒语义**的那个（找不到 → ``None``）。

    🔴2（复评第 4 轮）**确定性且保守**的口径——不再"首个含 ``wake`` 者胜"：

    1. **保守优先**：只要有一个对象的 ``wake`` **不是严格 ``True``**
       （严格 ``False`` 或坏值 ``"false"``/``0``/``"no"``…）→ 判定**静默**。
       方向与 ``parse_wake`` 的"判不出 → 保守静默"一致：读入侧放宽等于把熔断
       语义漏出去。交接卡同样会**累积多条**（同一条评论里引用多张卡），
       "首个含 ``wake`` 者胜"会让**顺序决定结论**：
       ``[wake=True, wake=False]`` → 放行、``[wake=False, wake=True]`` → 熔断
       （**fail-open 且顺序相关**，与 docstring/文档宣称的"顺序无关"直接矛盾）。
    2. **"最新卡"判据（显式定义）**：需要返回具体对象时，取**追加顺序里最后
       出现**的那个（正文是"每棒追加写回同一条评论"的累积载体 → 最后出现的
       即更晚的那张卡）。故多个静默卡时返回**最后一张**（决定仍恒为静默，
       取哪张不影响结论；只是 ``reason`` 取最新那张的）。
    3. 所有带 ``wake`` 的对象都是严格 ``True`` → 返回**最后一个**（常规接力）。
    4. 全都**不带** ``wake`` → ``None``（普通 context，不误熔断）。
    """
    if not isinstance(objects, (list, tuple)):
        return None
    silent: Optional[dict] = None
    awake: Optional[dict] = None
    for obj in objects:
        if not isinstance(obj, dict) or "wake" not in obj:
            continue
        if obj.get("wake") is True:
            awake = obj            # 严格 True：常规接力（取最新）
        else:
            silent = obj           # False / 坏值：保守静默（取最新）
    if silent is not None:
        return silent
    return awake


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
        degraded = False
        scan_reason = ""
        if isinstance(raw, str) and raw.strip():
            try:
                payload = json.loads(raw)
            except ValueError:
                # 常见形态：JSON 载荷 + `<!-- loop-budget: … -->` 留痕拼接。
                # 🔴2：**优先取含 `wake` 键的对象**——正文里"留痕在前"时第一个
                # 对象是预算留痕（不带 `wake`），盲取第一个 = 判不出静默交接。
                objs, degraded, scan_reason = extract_json_objects_diag(raw)
                payload = pick_wake_object(objs)
        if not isinstance(payload, dict):
            payload = None
        if payload is None and degraded:
            # **降级不表态 → 安全旗标取保守方向**（复评第 5 轮裁定）：
            # 扫描降级（正文被上界截断 / 工作量触顶）意味着"后面那一段没看"，
            # 而静默交接卡**可能就在没看的那一段里**。此时按"判不出"处理
            # （→ 保守静默），**不得**因为"没找到 `wake` 键"就判"常规接力"——
            # 后者正是 N1/N2 的 fail-open 方向（卡被丢掉 → `wake=True` 放行）。
            payload = {"wake": None,
                       "reason": "扫描降级（%s）：未表态区间按判不出处理"
                                 "（保守静默）" % (scan_reason or "未标注")}

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
        if degraded and wake_state == WAKE_AWAKE:
            # 降级 = "后面那一段没看" → 那里**可能还有**一张静默卡。
            # 此时"找到了一张 `wake=True` 的卡"不足以判放行（可能是**更早**的
            # 那张）→ 按**判不出**处理（保守静默）。方向与上面同：降级本身
            # **不得**成为 fail-open 通道。
            wake_state = WAKE_UNKNOWN
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
    def _merge_budget_dicts(dicts: Any, _depth: int = 0,
                            session_id: str = "") -> Optional[dict]:
        """把**多份**预算 dict 合并成一份（**逐字段 ``max()`` / 顺序无关**）。

        🔴1：评论正文里留痕会**累积多条**（每棒把留痕写回评论 = 必做项）。
        只取第一条 = 拿最旧的轮次 → 上界被稀释。故全部取、按 ``max()`` 合并，
        与"留痕在前/在后"的顺序无关。

        **按 ``session_id`` 归属过滤**（🟡1 复评第 5 轮）：同一条评论里可能挂着
        **别的会话**的留痕（另一个 Issue 已经用掉 5 轮）。无脑 ``max()`` 会把
        别人的轮次吸收进来 → 本会话下一轮直接 ``fuse``（**误判用尽/误熔断**）。
        故 ``session_id`` 给出时只合并**属于该会话**的留痕；留痕不带
        ``session_id`` 字段时不表态（保留）——"没写归属"≠"属于别人"。

        **``reason`` / ``budget`` 也按顺序无关口径合并**（🟡 复评第 4 轮）：

        - ``reason``：旧实现"首个非空胜" → ``[d1, d2]`` 与 ``[d2, d1]`` 给出
          **不同原因**（同一次判定、换顺序换结论）。现取**已熔断载荷里最长**的
          那条（确定性；更长的通常信息更多），并列时取字典序较小者 → 完全顺序无关。
        - ``budget``：旧实现"首个非空胜" → ``[{"max_rounds": 3}, {"max_rounds": 9}]``
          读成 3（**上界被旧值盖小**，而 ``rounds`` 已 ``max()`` → 两个字段互相矛盾）。
          现**逐字段取 ``max()``**（数字取大、其余按顺序无关的"最大"定义取）。

        内嵌的 ``budget_trail``（``<!-- loop-budget: … -->``）**递归展开后再合并**
        ——交接卡 context 里的预算留痕是内嵌字段，不是单独一段正文。
        深度超过 ``MAX_COERCE_DEPTH`` 一律不展开（跨仓载荷不可信）。
        """
        if _depth > MAX_COERCE_DEPTH:
            return None
        merged: Dict[str, Any] = {}
        rounds_out: Dict[str, Any] = {}
        reasons: List[str] = []
        budgets: List[Dict[str, Any]] = []
        fused = False
        nested: List[Any] = []
        for d in dicts:
            if not isinstance(d, dict):
                continue
            if not attributed(d, "session_id", session_id):
                continue          # 别的会话的留痕：不合并（🟡1）
            rounds = d.get("rounds")
            if isinstance(rounds, dict):
                for k, v in rounds.items():
                    if isinstance(v, int) and not isinstance(v, bool):
                        rounds_out[k] = max(rounds_out.get(k, 0), v)
            if LoopGuard._fused_of(d):
                fused = True
                reason = d.get("reason")
                if isinstance(reason, str) and reason:
                    reasons.append(reason)
            budget = d.get("budget")
            if isinstance(budget, dict):
                budgets.append(budget)
            trail = d.get("budget_trail")
            if isinstance(trail, str) and trail:
                nested.append(LoopGuard._coerce_budget_diag_impl(
                    trail, _depth + 1, session_id=session_id)[0])
        if nested:
            inner = LoopGuard._merge_budget_dicts(nested, _depth + 1,
                                                  session_id=session_id)
            if inner is not None:
                for k, v in (inner.get("rounds") or {}).items():
                    if isinstance(v, int) and not isinstance(v, bool):
                        rounds_out[k] = max(rounds_out.get(k, 0), v)
                if inner.get("fused") is True:
                    fused = True
                    r = inner.get("reason")
                    if isinstance(r, str) and r:
                        reasons.append(r)
                if isinstance(inner.get("budget"), dict):
                    budgets.append(inner["budget"])
        if rounds_out:
            merged["rounds"] = rounds_out
        if fused:
            merged["fused"] = True
        if reasons:
            # 顺序无关：最长优先，并列取字典序最小（同一次判定的原因唯一）
            merged["reason"] = sorted(reasons, key=lambda x: (-len(x), x))[0]
        if budgets:
            # 🟡b（复评第 5 轮）：``budget`` 段**只作观测**，**无采纳路径**——
            # 外部载荷的 ``max_rounds`` 一旦被采纳就是 ``max()`` 抬高上界
            # = 削弱熔断（与 N1/N2 同属 fail-open），且归属可缺省、不可信正文
            # 可借此放大预算。消费侧（``adopt_budget``）只读 ``rounds`` / ``fused``。
            merged["budget"] = LoopGuard._merge_budget_values(budgets)
        return merged or None

    @staticmethod
    def _merge_budget_values(budgets: List[Dict[str, Any]]) -> Dict[str, Any]:
        """把多份 ``budget`` 段**逐字段**合并（**真顺序无关**）。

        ⚠️ **只作观测，不被消费**（复评第 5 轮 🟡b · 总师裁定）：本方法的返回值
        **没有任何采纳路径**——``adopt_budget()`` 只消费 ``rounds`` 与 ``fused``，
        **不**把外部载荷的 ``max_rounds`` 采纳成本会话的上界。原因：把外部载荷的
        ``max_rounds`` 采纳进来是 ``max()`` **抬高上界 = 削弱熔断**（方向与 N1/N2
        同属 fail-open），且归属字段可缺省（"不表态"即被合并），不可信正文可借此
        放大预算。故这里只保留**观测**语义（供留痕/排查），调用方**不得**据此改
        ``self.max_rounds`` 等上界。

        合并口径（**N3 修**：旧实现 ``None`` 参与比较 → 顺序相关）：

        - ``None`` = **缺席**，**不参与比较**（``[{"x": None}, {"x": 9}]`` 与
          ``[{"x": 9}, {"x": None}]`` 都必须读成 ``9``；旧实现在后者上返回
          ``None``——``max(9, None, key=repr)`` 比的是 ``repr``，``'None' > '9'``）；
        - 数字（排除 ``bool``）取 ``max()``；字符串取字典序最大；
        - 其余（混类型/怪值）按 ``(类型名, repr)`` 的**确定性**全序取大——
          同样与顺序无关，且不给"非数字上界"赋业务含义。
        """
        out: Dict[str, Any] = {}
        for b in budgets:
            if not isinstance(b, dict):
                continue
            for k, v in b.items():
                if v is None:
                    continue          # N3：None 视为缺席（不参与比较）
                if k not in out:
                    out[k] = v
                    continue
                cur = out[k]
                if isinstance(v, (int, float)) and not isinstance(v, bool) \
                        and isinstance(cur, (int, float)) \
                        and not isinstance(cur, bool):
                    out[k] = max(cur, v)
                elif isinstance(v, str) and isinstance(cur, str):
                    out[k] = max(cur, v)
                else:
                    out[k] = max(cur, v,
                                 key=lambda x: (type(x).__name__, repr(x)))
        return out

    @staticmethod
    def _coerce_budget_payload(payload: Any, _depth: int = 0,
                               session_id: str = "") -> Optional[dict]:
        """``_coerce_budget_diag`` 的**只取载荷**包装（既有调用方/回归按此签名）。

        需要知道"扫描是否降级、为什么降级"时用 ``_coerce_budget_diag``——
        降级 = 有区间**未表态**，消费侧据此取**保守方向**（见 ``adopt_budget``）。
        """
        return LoopGuard._coerce_budget_diag(payload, _depth, session_id)[0]

    @staticmethod
    def _coerce_budget_diag(payload: Any, _depth: int = 0,
                            session_id: str = "") -> "tuple[Optional[dict], bool, str]":
        """``(载荷, degraded, reason)``：解析预算载荷并**上报扫描降级**。

        为什么必须把降级**传到消费侧**（复评第 5 轮裁定 · G5）：扫描降级意味着
        "某一段没看"，而 `fused=true` 的卡 / 更大的 `rounds` **可能就在那一段里**。
        若消费侧只拿到"没解析出东西"就按空操作返回，降级本身就成了**新的
        fail-open 通道**（🔴-4：降级正文 → `merged=False`、`round_no=0`，卡静默丢）。

        ``reason`` 三态见 ``secretary.trail.SCAN_REASON_*``；嵌套 `budget_trail`
        递归展开时**逐层取或**（任一层降级 → 整体降级）。
        """
        return LoopGuard._coerce_budget_diag_impl(payload, _depth, session_id)

    @staticmethod
    def _coerce_budget_diag_impl(payload: Any, _depth: int = 0,
                                 session_id: str = "") -> "tuple[Optional[dict], bool, str]":
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
            return None, False, ""
        if isinstance(payload, dict):
            if not attributed(payload, "session_id", session_id):
                return None, False, ""   # 别的会话的载荷：不合并（🟡1）
            # 内嵌 `budget_trail`（交接卡 context 的形态）也要展开并合并
            trail = payload.get("budget_trail")
            if isinstance(trail, str) and trail:
                inner, degraded, reason = LoopGuard._coerce_budget_diag_impl(
                    trail, _depth + 1, session_id=session_id)
                if inner is not None:
                    return (LoopGuard._merge_budget_dicts(
                        [payload, inner], _depth + 1,
                        session_id=session_id) or payload), degraded, reason
                return payload, degraded, reason
            return payload, False, ""
        if not isinstance(payload, str) or not payload.strip():
            return None, False, ""

        # ① 正文里的**全部** `<!-- loop-budget: … -->` 留痕 → 合并（🔴1）
        #    归属过滤（🟡1）：只合并属于本会话的留痕（同一条评论可能有多个会话）。
        #    🔴1 残留（复评第 4 轮）：**这里不再"有 rounds 就提前 return"**。
        #    旧实现「`merged` 带 rounds → 直接返回」把 ②③④ 全跳过：正文形态
        #    「留痕段 + 不带内嵌留痕的卡」时，卡的轮次/`fused` 一律读不到——
        #    正是本轮声称"预算来源取整段正文"要修的那个形态（实测 trail(3)+card(6)
        #    → `round_no=3`，比"只给卡"的 6 还差）。现改为**参与合并**：
        #    `merged` 只作初始值，继续解析正文里的对象再一起 `max()`。
        merged = LoopGuard._merge_budget_dicts(
            (rec for rec in collect_marked(payload, "loop-budget")
             if attributed(rec, "session_id", session_id)),
            _depth + 1, session_id=session_id)

        # ② 整段就是一份 JSON
        degraded = False
        reason = ""
        try:
            got: Any = json.loads(payload)
        except (ValueError, RecursionError):
            got = None
        if not isinstance(got, dict):
            # ③ 正文里散落的多个顶层对象 → 合并（顺序无关；内嵌留痕递归展开）
            #    用 diag 版本：把"扫描降级"（截断 / 总量触顶 / 候选超长）
            #    一并上报给消费侧，否则降级会被静默当成"没解析出东西"。
            objs, degraded, reason = extract_json_objects_diag(payload)
            got = LoopGuard._merge_budget_dicts(
                objs, _depth + 1, session_id=session_id)
        if not isinstance(got, dict):
            return merged, degraded, reason
        # ④ 留痕段与正文对象**互补合并**（顺序无关、逐字段 max）：
        #    只要两处都有内容就合并——不再用"`got` 有没有 `rounds`"当条件
        #    （`fused` / `budget` / `reason` 也必须在两条通道间合并）。
        if merged is not None:
            got = LoopGuard._merge_budget_dicts([got, merged], _depth + 1,
                                                session_id=session_id) or got
        # 载荷内嵌的 `budget_trail`（`<!-- loop-budget: … -->` 形态）：递归取内层，
        # 与载荷自身的 rounds **合并**（取 max）——不是替换（否则内层较小值时回退）。
        nested = got.get("budget_trail")
        if isinstance(nested, str):
            inner, d2, r2 = LoopGuard._coerce_budget_diag_impl(
                nested, _depth + 1, session_id=session_id)
            if inner is not None:
                got = LoopGuard._merge_budget_dicts(
                    [got, inner], _depth + 1, session_id=session_id) or got
            degraded = degraded or d2
            reason = reason or r2
        return got, degraded, reason

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
        # 归属过滤（🟡1）：只合并**属于本会话**的留痕——同一条评论可能挂着
        # 多个会话/检查点的留痕，无脑 max() 会把别人的轮次吸收进来 → 本会话
        # 下一轮直接 fuse（误熔断）。
        data, degraded, scan_reason = self._coerce_budget_diag(
            payload, session_id=self.session_id)
        if data is None and not degraded:
            return {"merged": False, "adopted": {},
                    "degraded": False, "scan_reason": "",
                    "reason": "载荷无法解析（无操作）"}
        if data is None:
            # 降级且没解析出载荷：**不得**按空操作返回（🔴-4：卡静默丢）
            data = {}

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

        if degraded:
            # **扫描降级 → 安全旗标取保守方向**（复评第 5 轮裁定 · G5）：
            # 降级 = "某一段没表态"，而 `fused=true` 的卡 / 更大的 `rounds`
            # 可能就在那一段里。此时"没读到"**不得**当成"没有"——那正是
            # 降级变成 fail-open 通道的形态（🔴-4）。故保守继承熔断，
            # 并把**原因**写进 `fused_reason`（可观测，便于下一棒复核）。
            if not st.fused:
                st.fused = True
                st.fused_reason = (
                    f"扫描降级（{scan_reason or '未标注'}）：未表态区间按判不出"
                    "处理（保守继承熔断）")
                st.fused_at = time.strftime("%Y-%m-%d %H:%M:%S")

        adopted = {k: getattr(st, k) for k in fields}
        changed = {k: v for k, v in adopted.items() if v != before[k]}
        note = (f"合并外部预算（source={source or '未标注'}）："
                f"rounds={adopted}，fused={st.fused}"
                + (f"，扫描降级={scan_reason}" if degraded else ""))
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
                "fused": st.fused,
                # 扫描降级标记**外露**（G5 观测面）：消费侧据此取保守方向，
                # 复核者也据此看出"这次为什么判不出"（不吞异常/不静默）。
                "degraded": degraded, "scan_reason": scan_reason,
                "reason": note}

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
