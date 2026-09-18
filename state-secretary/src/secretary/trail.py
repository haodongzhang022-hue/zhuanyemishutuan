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


def collect_marked(text: Any, marker: str) -> List[dict]:
    """收集正文里**全部**留痕（顺序保留；坏 JSON 跳过，不炸）。

    只做"收集"这一件事 —— 合并语义（`max()` / 取或）留给各调用方，
    因为熔断轮次与探测次数合并的字段不同。

    🟢-1（复评第 5 轮）：**删掉死形参 `max_depth`**。旧签名有
    ``max_depth: int = MAX_TRAIL_DEPTH``，但函数体从不读它 ——
    ``collect_marked(text, marker, 0)`` 与默认结果完全一致（形参存在却不生效，
    正是本仓库反复治的形态："读了不生效 / 声明了不接线"）。

    深度边界**不是**没有，而是**不在这里**：本函数只按标记正则取片段，
    片段自身的嵌套深度由 `json.loads` 处理（超界抛 `RecursionError`，
    已捕获 → 跳过该片段，不炸）；跨仓载荷的**语义**深度上界在消费侧
    （`probe._coerce_checks_used` / `loop_guard._coerce_budget_payload`
    的 `MAX_TRAIL_DEPTH`）。删形参而不是"补个假用"：后者会造出第二份
    语义源，与本仓库"单一语义源"的口径冲突。
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
# 🔴5（复评第 4 轮）：正文是不可信输入，扫描必须**线性**。旧实现
# `_balanced_object_end` 从**每个** `{` 重新扫到文末，未闭合时退化成 O(n²)
# （`"{" * 40000` → 63 s，基线 3 ms）。
#
# 复评第 5 轮（本文件的上界被证伪：两段式"线性主扫描 + 有界修复"会**丢卡**，
# 且丢卡方向是 fail-open）→ 按总师裁定**改实现路径**：
#
# * **构造性"不漏"**：每个候选 `{` 用 ``json.JSONDecoder().raw_decode`` **独立起算**
#   ——与旧实现"每个起点重扫"的语义等价（能捞到的都捞到），但解析交给 C，
#   且未闭合/非法即 fail-fast。**不再**依赖"预算够就捞到"。
# * **不变量 = 超集召回**：`a7847f9` 的 `extract_json_objects` 能取到的对象，
#   本实现**必须**全取到（差分 fuzz 须 `LOST=0`）。见 ``scan_json_spans`` docstring。
# * **上界只降级、不丢段**：触顶（``MAX_SCAN_WORK``）时保留**已取到的**片段，
#   只把**剩余区间**判为"不表态"，并**显式标记**（``..._report`` 的第二个返回值）。
#
#: 正文扫描的**默认上界**（字符数）：上界远大于任何真实交接卡/评论正文
#: （含历史留痕累积）；超出部分不再尝试解析（保守：判不出 → 不表态）。
DEFAULT_SCAN_BUDGET = 1_000_000

#: 候选解析的**工作量上界**（字符数，兜底）：`raw_decode` 每个候选是
#: O(候选长度)，"每个 `{` 都试一次"在**不可信正文**上仍可能退化
#: （如 `'{"a":' * n` 这类"形态像对象、但一路不闭合"的片段）。
#: 触顶即**降级**：保留已取到的片段 + **显式标记** `degraded`，
#: 绝**不**丢弃整段结果（上一轮"静默收摊"正是丢卡来源）。
#: 记账口径：成功/失败按 `raw_decode` **实际扫过**的位置计（成功 `end - p`、
#: 失败 `pos - p`），`RecursionError`（无位置）按**剩余长度**上限计——
#: 即"记账 ≥ 真实扫描量"，故本上界同时是**真实耗时**的上界。
#: 取值按实测留足余量：79 KB 台账正文（`issues/TEST-REGRESSION.md`）工作量
#: ≈ 18 万字符，本上界有 ~100× 余量；触顶只出现在恶意正文。
MAX_SCAN_WORK = 20_000_000

#: JSON 对象首字符（跳过空白后）：`{` 之后**只能**是 `"`（键）或 `}`（空对象）。
#: 这条 O(1) 预检把 `if (x) {` / `"{"` 类输入挡在 `raw_decode` 之外
#: （否则每个候选都要进一次 C 解析器）。
_JSON_WS = " \t\n\r"
_JSON_DECODER = json.JSONDecoder()


def _scan_report(text: str, limit: int) -> "tuple[List[str], bool]":
    """扫描 ``text[:limit]`` 里的顶层 ``{...}`` 片段（JSON 合法才取）。

    返回 ``(片段清单, degraded)``。``degraded=True`` 的含义**只有一条**：
    "``limit`` 之后（或工作量触顶之后）的区间**未表态**"——**已取到的片段
    一个不丢**（这正是与上一轮"静默收摊、整段丢卡"的区别）。消费侧据此把
    安全旗标（``fused`` / ``wake``）取**保守方向**，否则降级本身就是一条
    fail-open。

    实现（**构造性"不漏"**）：

    * **候选起算独立**：对每个候选 ``{`` 调 ``raw_decode``（**不共享**前一个候选
      的字符串状态）——等价旧实现"每个起点按括号配平重扫 + ``json.loads``"的
      语义，但解析在 C 里做，且未闭合/非法即 fail-fast。
    * **O(1) 预检**（短路 `"{"×n` / `if (x) {` 类输入）：
      ① 候选之后**必须还有** `}`（``rfind``，一次 O(n) 定全局上界）；
      ② ``{`` 之后跳过空白**必须**是 `"` 或 `}`（JSON 对象的唯一两种起头）。
    * **降级（只降级、不丢段）**：工作量超过 ``MAX_SCAN_WORK`` → 停止尝试、
      保留已取到的片段、``degraded=True``。
    """
    out: List[str] = []
    # 截断（``limit < len(text)``）同样是"该区间不表态" → 显式标记（不得静默）。
    degraded = limit < len(text)
    last_close = text.rfind("}", 0, limit)
    if last_close < 0:
        return out, degraded     # 区间内没有 `}`：不可能有配平对象
    work = 0
    i = 0
    while True:
        p = text.find("{", i, limit)
        if p < 0 or p > last_close:
            break
        j = p + 1
        while j < limit and text[j] in _JSON_WS:
            j += 1
        if j >= limit or (text[j] != '"' and text[j] != "}"):
            i = p + 1            # 形态不像对象：O(1) 跳过（不进 raw_decode）
            continue
        try:
            got, end = _JSON_DECODER.raw_decode(text, p)
        except (ValueError, RecursionError) as exc:
            # 失败代价 = ``raw_decode`` **实际扫过**的位置（错误位置 pos），
            # 而不是"剩余全文"——否则正常正文（每段一个对象）也会被误计为触顶。
            # ``RecursionError`` 无 pos（深嵌套）：按剩余长度上限计（记账 ≥ 真实）。
            pos = getattr(exc, "pos", None)
            cost = (pos - p) if isinstance(pos, int) and pos > p \
                else (limit - p)
            if work + cost > MAX_SCAN_WORK:
                degraded = True  # 只降级：已取到的片段照常返回
                break
            work += cost
            i = p + 1            # 失败候选：下一个起点（旧实现跳到配平末尾）
            continue
        if work + (end - p) > MAX_SCAN_WORK:
            degraded = True          # 同上：已取到的片段不丢
            break
        work += end - p
        if isinstance(got, dict):
            out.append(text[p:end])
        i = end if end > p else p + 1
    return out, degraded


def scan_json_spans_report(
        text: Any, budget: Optional[int] = None) -> "tuple[List[str], bool]":
    """``scan_json_spans`` 的**带降级标记**版本：``(片段清单, degraded)``。

    为什么要有显式标记（复评第 5 轮总师裁定）：上一轮的
    ``MAX_REPAIR_CHARS`` 触顶是**静默收摊**——调用方看不出"这一段没表态"，
    于是下一轮继续漏。降级必须**可观测**，故单列一个入口返回标记；
    语义仍是"降级只影响**剩余区间**，**已取到的片段一个不丢**"。
    """
    out: List[str] = []
    if not isinstance(text, str) or not text:
        return out, False
    n = len(text)
    limit = n if budget is None else min(n, max(0, budget))
    if limit <= 0:
        return out, False
    return _scan_report(text, limit)


def scan_json_spans(text: Any, budget: Optional[int] = None) -> List[str]:
    """正文里的顶层 ``{...}`` 片段（配平**且** JSON 合法；顺序保留）。

    **语义（与基线 `a7847f9` 的 `extract_json_objects` 同口径，且为超集）**：
    候选 = 正文里每个 ``{``（**独立起算**）；取其中"从该 ``{`` 起能 ``raw_decode``
    出合法 JSON 对象"的那些，按起点顺序输出片段文本。旧实现是"每个起点按括号
    配平扫一遍再 ``json.loads``"——合法对象两种口径结果一致，故

    * **超集召回（不变量）**：基线能捞到的对象，本实现**全捞到**（`LOST=0`）；
    * 失败候选按 ``p + 1`` 前进（旧实现跳到配平末尾）→ 可能多捞出失败候选
      **内部**的对象（超集方向；不产生丢卡，且不会重复输出已被解析片段覆盖的起点）。

    **为什么不再用"两段式（线性主扫描 + 有界修复）"**（复评第 5 轮裁定）：
    那条路径的**正确性依赖预算兜底**（"预算够就捞到"），本轮已在兜底路径上
    长出两个 fail-open：引号区间前缀 + 卡（88 B）、>60 KB 长正文 + 卡
    （`issues/TEST-REGRESSION.md`）——卡被整段丢掉 → ``fused=False`` /
    ``wake=True``（**放行**方向）。现在"不漏"是**构造性**的：
    候选独立起算，不看前面的引号/花括号状态。

    ``budget`` 给定时只扫描正文**前 ``budget`` 个字符**（上界远大于任何真实
    交接卡/评论正文；超出部分保守不表态），不改变正常输入的结果。
    """
    return scan_json_spans_report(text, budget)[0]


def json_objects_in_report(
        text: Any, budget: Optional[int] = None) -> "tuple[List[dict], bool]":
    """``json_objects_in`` 的**带降级标记**版本：``(对象清单, degraded)``。"""
    segs, degraded = scan_json_spans_report(
        text, DEFAULT_SCAN_BUDGET if budget is None else budget)
    out: List[dict] = []
    for seg in segs:
        try:
            got = json.loads(seg)
        except (ValueError, RecursionError):
            continue
        if isinstance(got, dict):
            out.append(got)
    return out, degraded


def json_objects_in(text: Any, budget: Optional[int] = None) -> List[dict]:
    """正文里全部顶层 JSON **对象**（坏片段跳过；不抛）。

    与 ``scan_json_spans`` 同一次扫描的结果，只多一层 ``json.loads`` 兜底；
    ``budget`` 缺省同样取 ``DEFAULT_SCAN_BUDGET``（上界**处处生效**，
    不因调用方忘了传就变成无界扫描）。降级标记见 ``json_objects_in_report``。
    """
    return json_objects_in_report(
        text, DEFAULT_SCAN_BUDGET if budget is None else budget)[0]
