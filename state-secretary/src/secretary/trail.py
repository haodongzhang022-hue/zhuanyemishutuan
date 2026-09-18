"""留痕（trail）公共工具：**收集全部留痕 + 归属过滤**（单一实现，防两处再分叉）

## 为什么要有这一层（复评第 4 轮 🟢 / 第 5 轮 🟡1）

「预算留痕」在**两处**各自实现过一遍：`loop_guard.BUDGET_TRAIL_RE`（熔断轮次）
与 `probe.PROBE_BUDGET_TRAIL_RE`（探测次数）。两处逻辑同形却各写一份 —— 上一轮
"只取第一条留痕"的缺陷（🔴1）就是在这种分叉里长出来的；这一轮又长出同族的
第二个缺陷（🟡1）：**多留痕 `max()` 合并时没有按归属过滤**。

归属过滤为什么必须有：评论正文是**累积载体**（每棒把留痕写回同一条评论），
而一条评论里可能同时挂着**多个检查点 / 多个会话**的留痕。无脑 `max()` 会让
A 检查点用掉的 6 次被 B 检查点"继承"→ B 一上来就判「预算用尽」直接
`resume_required`（熔断侧同理：别的会话 `rounds.total=5` 被本会话吸收 →
下一轮直接 `fuse`）。这是**误判用尽**，方向是"多停一次"，与"永不触发上界"
一样都是判定失真。

故口径收敛为两条，且**只有一份实现**：

1. **收集全部**：正文里同一标记的多条留痕全部取出（不是只取第一条）；
2. **归属过滤**：留痕自带归属字段（`checkpoint_key` / `session_id`）时，
   只保留**属于本次判定对象**的那些；**不带该字段的载荷不表态**（保留）——
   "没写归属"不等于"属于别人"（否则老的/手写的留痕会被整段丢弃）。
"""
from __future__ import annotations

import json
import re
from typing import Any, List, Optional

#: 留痕解析的**深度上界**：跨仓/评论载荷不可信，深嵌套 JSON 会触发
#: `RecursionError` 并逃逸到门面；超界一律按"判不出"处理（不炸）。
MAX_TRAIL_DEPTH = 8


def trail_re(marker: str) -> "re.Pattern[str]":
    """某标记的留痕正则（`<!-- <marker>: {json} -->`，跨行）。"""
    return re.compile(r'<!--\s*' + re.escape(marker) + r':\s*(\{.*?\})\s*-->',
                      re.S)


def collect_marked(text: Any, marker: str,
                   max_depth: int = MAX_TRAIL_DEPTH) -> List[dict]:
    """收集正文里**全部**留痕（顺序保留；坏 JSON 跳过，不炸）。

    只做"收集"这一件事 —— 合并语义（`max()` / 取或）留给各调用方，
    因为熔断轮次与探测次数合并的字段不同。
    """
    if not isinstance(text, str) or not text.strip():
        return []
    out: List[dict] = []
    for m in trail_re(marker).finditer(text):
        try:
            got = json.loads(m.group(1))
        except (ValueError, RecursionError):
            continue
        if isinstance(got, dict):
            out.append(got)
    return out


def attributed(record: Any, field: str, value: Any) -> bool:
    """**归属过滤**：留痕是否属于本次判定对象。

    - `value` 未给出（空串/None）→ 不过滤（调用方没说要按什么归属）；
    - 留痕**不带**该字段 → 不表态 → 保留（"没写归属"≠"属于别人"）；
    - 留痕带该字段且**等于** `value` → 保留；
    - 留痕带该字段且**不等** → 丢弃（这正是 🟡1 的缺陷：跨检查点/跨会话污染）。
    """
    if not isinstance(record, dict):
        return False
    if value in ("", None):
        return True
    if field not in record:
        return True
    got = record.get(field)
    if got in ("", None):
        return True
    return str(got) == str(value)


# ---- 正文里的 JSON 片段扫描（**单一实现**：loop_guard 与 probe 共用） ----
# 🔴5（复评第 4 轮）：正文是不可信输入，扫描必须**单次线性**。旧实现
# `_balanced_object_end` 从**每个** `{` 重新扫到文末，未闭合时退化成 O(n²)
# （`"{" * 40000` → 63 s，基线 3 ms）。故把扫描收进这一层，两处共用一份，
# 避免再次分叉（与"留痕收集"同一理由）。
#
#: 正文扫描的**默认上界**（字符数）：上界远大于任何真实交接卡/评论正文
#: （含历史留痕累积）；超出部分不再尝试解析（保守：判不出 → 不表态）。
DEFAULT_SCAN_BUDGET = 1_000_000


def _stack_scan(text: str, a: int, b: int, pairs: dict,
                regions: List["tuple[int, int]"]) -> None:
    """扫描 ``[a, b)``（**以"字符串外"为初态**），记录 ``{``→``}`` 配对与字符串区间。

    - ``pairs``：``{起点下标: 结束下标}``，**只记配平的**（栈配对，线性）；
    - ``regions``：字符串区间 ``(开引号下标, 闭引号下标或 b)``——"在字符串里"
      的那些 ``{`` 不会被配对，正是旧实现"每个起点重扫"能捞到、而单次扫描
      捞不到的那些（见 ``scan_json_spans`` 的修复段）。
    """
    stack: List[int] = []
    in_str = False
    escaped = False
    q = -1
    for i in range(a, b):
        ch = text[i]
        if in_str:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_str = False
                regions.append((q, i))
        elif ch == '"':
            in_str = True
            q = i
        elif ch == "{":
            stack.append(i)
        elif ch == "}":
            if stack:
                s0 = stack.pop()
                pairs.setdefault(s0, i)
    if in_str:
        regions.append((q, b))


#: 修复扫描的**上界**：主扫描把"字符串里"的 ``{`` 视为待定，逐个字符串区间
#: 重扫一遍（等价于旧实现"每个起点重新起算"）。重扫次数与总字符量都设上界，
#: 使**不可信正文**（大量未闭合引号）不能把解析拖成平方复杂度。
MAX_REPAIR_REGIONS = 256
MAX_REPAIR_CHARS = 500_000
MAX_REPAIR_DEPTH = 3


def scan_json_spans(text: Any, budget: Optional[int] = None) -> List[str]:
    """**单次线性扫描**取出正文里的顶层 ``{...}`` 片段（配平的才算数）。

    🔴5（复评第 4 轮）**为什么不能"每个 ``{`` 重新扫到文末"**：评论正文是
    **不可信输入**，且本函数在 ``from_handoff`` / ``adopt_budget`` /
    ``_coerce_checks_used`` 的**主路径**上。旧实现从**每个** ``{`` 重新扫到
    文末，未配平时每个起点都白扫一遍 → O(n²)：``"{" * 40000`` 实测 **63 s**
    （基线 3 ms，4 个数量级回归）。典型触发输入：JS/JSON 差量片段
    （``"if (x) {" * 3000``）、jsonschema 片段。

    实现 = **一次线性主扫描 + 有界修复**，两段都确定性：

    1. **主扫描**（``_stack_scan``，一次从左到右，栈配对 + 字符串状态）：
       得到全部"字符串外" ``{`` 的配对，以及字符串区间清单。
       未配平的起点**记为不可用**（不产片段、也**不重扫**——重扫就是平方来源），
       但它**不影响它后面**能配平的起点：``"{" * 20000 + '{"wake": false}'``
       仍能取到那张卡（栈配对的正是后面那个 ``{``）——
       "前面有未闭合花括号"**不得**把后面的交接卡整段丢掉（那是 fail-open）。
    2. **有界修复**：主扫描把"字符串里"的 ``{`` 视为待定（旧实现对这些起点是
       **重新起算字符串状态**的，故仍可能捞出对象，例如未闭合引号后面的卡）。
       故对每个字符串区间 ``(q, c)`` 再从 ``q + 1`` 重扫一次（初态"字符串外"），
       只采纳**落在该区间内**的起点，补齐主扫描捞不到的配对。
       重扫次数与总字符量各有上界（``MAX_REPAIR_REGIONS`` /
       ``MAX_REPAIR_CHARS``）：超界即**收摊**（判不出 → 不表态），
       绝不让不可信正文把耗时拖成平方。

    ``budget`` 给定时只扫描正文**前 ``budget`` 个字符**（上界远大于任何真实
    交接卡/评论正文；超出部分保守不表态），不改变正常输入的结果。
    """
    out: List[str] = []
    if not isinstance(text, str) or not text:
        return out
    n = len(text)
    limit = n if budget is None else min(n, max(0, budget))
    pairs: dict = {}
    regions: List["tuple[int, int]"] = []
    _stack_scan(text, 0, limit, pairs, regions)
    repair_chars = 0
    pending = list(regions[:MAX_REPAIR_REGIONS])
    depth = 0
    while pending and depth < MAX_REPAIR_DEPTH:
        nxt: List["tuple[int, int]"] = []
        for q, c in pending:
            if repair_chars >= MAX_REPAIR_CHARS:
                break
            repair_chars += max(0, limit - (q + 1))
            sub: dict = {}
            sub_regions: List["tuple[int, int]"] = []
            _stack_scan(text, q + 1, limit, sub, sub_regions)
            for s0, e0 in sub.items():
                if q < s0 < c:      # 只补**该字符串区间内部**的起点
                    pairs.setdefault(s0, e0)
            # 重扫本身又可能落在"新的字符串"里（嵌套未闭合引号）→ 递归修复
            for rq, rc in sub_regions:
                if rq >= q and repair_chars < MAX_REPAIR_CHARS:
                    nxt.append((rq, rc))
        pending = nxt[:MAX_REPAIR_REGIONS]
        depth += 1
    cursor = 0
    for start in sorted(pairs):
        if start < cursor:
            continue                 # 已被上一个片段覆盖（嵌套在内部）
        end = pairs[start]
        out.append(text[start:end + 1])
        cursor = end + 1
    return out


def json_objects_in(text: Any, budget: Optional[int] = None) -> List[dict]:
    """正文里全部顶层 JSON **对象**（坏片段跳过；不抛）。

    与 ``scan_json_spans`` 同一次扫描的结果，只多一层 ``json.loads`` 兜底；
    ``budget`` 缺省同样取 ``DEFAULT_SCAN_BUDGET``（上界**处处生效**，
    不因调用方忘了传就变成无界扫描）。
    """
    out: List[dict] = []
    for seg in scan_json_spans(
            text, DEFAULT_SCAN_BUDGET if budget is None else budget):
        try:
            got = json.loads(seg)
        except (ValueError, RecursionError):
            continue
        if isinstance(got, dict):
            out.append(got)
    return out
