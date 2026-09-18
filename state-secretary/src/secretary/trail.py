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
# * **不变量 = 超集召回**：`a7847f9` 的 `extract_json_objects` 能取到的、且长度
#   **≤ ``MAX_RESCAN_LEN``** 的对象，本实现**必须**全取到（差分 fuzz 须 `LOST=0`）。
#   见 ``scan_json_spans`` docstring。
# * **上界只降级、不丢段**：两条上界（``MAX_RESCAN_LEN`` 每候选 / ``MAX_SCAN_WORK``
#   总量）触界时保留**已取到的**片段，只把"未扫到的区间"判为"不表态"，
#   并**显式标记**（``..._diag`` 的 `degraded` + 三态 `reason`）。
#
# ⚠️ **安全断言与实现同源**（复评第 5 轮总师点名 · `[HIGH]`）：本文件**不得**
# 再出现"记账 ≥ 真实扫描量，故本上界同时是**真实耗时**的上界"这类**失真断言**。
# 现在的口径是**可验证**的两条：
#   ① 记账**覆盖** `raw_decode` 的实扫量与**异常构造代价**（失败取错误位置 `pos`，
#      不是 `pos - p`——后者恒为 8，会让总上界**永不触顶**）；
#   ② 记账**另含每候选固定开销**（``_SCAN_CANDIDATE_COST``）与一次基扫（`limit`），
#      故"记账 → 耗时"的折算比有**实测上界**（最坏 ≈ 1.2 ns/单位，见
#      `test_issue21_scan_rework.py::TestG7AccountingMechanism`）。
# 断言写成"**实测折算比 × 上界** < 2 s"这种**可复跑**形式，不写"必然上界"。
#
#: 正文扫描的**默认上界**（字符数）：上界远大于任何真实交接卡/评论正文
#: （含历史留痕累积）；超出部分不再尝试解析（保守：判不出 → 不表态）。
DEFAULT_SCAN_BUDGET = 1_000_000

#: **每候选**的扫描区间上界（字符数）——「① 每候选上界」。
#:
#: 为什么必须**单独显式**给出（复评第 5 轮裁定 · G2 硬要求）：只给"总工作量上界"
#: 而不给每候选上界，总上界会被**单个候选**吃掉——`'{"k":1, '×40000 + '}'`
#: （320 KB）里每个候选都要扫到文末，总上界再大也只是"能容忍几个候选"，
#: 实测 320 KB → 2.26 s、1 MB → 21.7 s（`degraded` 恒 `False`）。
#:
#: 语义：对候选 `p`，只在窗口 ``[p, min(limit, p + MAX_RESCAN_LEN))`` 里找 `}`；
#: 窗口内**没有** `}` → 该候选不表态（跳过）且 `reason="rescan_len"`。
#: 等价于"从该候选起能配平的对象，长度不得超过本上界"。
#:
#: 取值依据（实测，非拍脑袋）：本仓库全部语料（`issues/*.md`、`docs/**/*.md`、
#: `src/**/*.py`、`tests/*.py` + 台账 ×1…×6 + 卡片拼接）里"候选 → 最近 `}`
#: 距离"的**最大值 = 4 842**（台账 ×6），本上界有 **54×** 余量。
#: 反向对照见 ``test_issue21_scan_rework.py``：**超过本上界的合法长对象会被
#: 跳过**——但这是**显式降级**（`degraded=True` + `reason="rescan_len"`），
#: 消费侧据此取保守方向，**不是**静默丢卡。
MAX_RESCAN_LEN = 262_144

#: 候选解析的**总工作量上界**（折算单位，见 `_SCAN_CANDIDATE_COST`）——「② 总上界」。
#:
#: 记账口径（复评第 5 轮裁定 · G3：**记账 ≥ 真实扫描量**，且须覆盖**异常构造代价**）：
#:
#:     work += 候选扫描区间长度（窗口内到 `}` 的字符数）
#:           + 每候选固定开销折算（`_SCAN_CANDIDATE_COST`）
#:           + raw_decode 实扫量：成功 `end - p`；失败取**错误位置 `pos`**
#:             （**不是** `pos - p`——异常消息的构造代价随 `pos` 线性增长，
#:             `pos - p` 恒为 8 ⇒ 总记账 800 k ≪ 上界 ⇒ **永不触顶**，
#:             正是 2.26 s / 21.7 s 的机制）；无 `pos`（`RecursionError`）
#:             取剩余长度上限。
#:
#: 触顶即**降级**：保留已取到的片段 + 显式标记 `degraded` / `reason="work_cap"`，
#: 绝**不**丢弃整段结果（"静默收摊"正是丢卡来源）。
#:
#: 取值依据（实测，两条都要满足）：
#:
#: * **上侧（耗时上界）**：最坏折算比 ≈ **1.1 ns/单位**（`"{}"×N` 微对象密集形态，
#:   每候选固定开销主导）⇒ 本上界对应最坏耗时 ≈ **0.22 s**（实测最坏对抗输入
#:   **0.22 s**，远低于 G6 的 1 s 判据）。
#: * **下侧（不误降级，裁定明示 ≥ 5×）**：总师裁定的反向对照规模 = 台账 ×5
#:   （**≥ 417 480 字符**）→ 实测折算量 **32.3 M** ⇒ 本上界留 **6.2×** 余量
#:   （≥ 5× 硬要求，见 `test_reverse_calibration_has_margin_beyond_417k`）。
#:   该余量同时把"真实正文不误降级"的规模上限抬到 ≈ 927 K 字符（台账 ×10）。
#:
#: ⇒ 触顶只出现在**恶意/极端密集**正文（含 `}` 的 320 KB 对抗输入、微对象密集串）。
MAX_SCAN_WORK = 200_000_000

#: **每候选固定开销**的折算当量（单位：字符）。为什么总上界里必须含它：
#: 扫描循环的每次迭代（`find` + 空白跳过 + 形态预检 + 循环开销）实测 ≈
#: **0.4–0.9 µs/候选**，与候选长度**无关**；只按字符量记账时，
#: "大量微对象"形态（如 `"{}"×500 000`：字符量仅 1 M，耗时 0.47 s）会
#: 记成很小的量 ⇒ 记账**不覆盖真实耗时**（上界失真）。
#: 取值 = 实测每候选开销的**上界**（0.9 µs）折算，留余量取 1 024。
_SCAN_CANDIDATE_COST = 1_024

#: 降级原因三态（`reason` 的取值域）。消费侧按 `reason` 把安全旗标取保守方向，
#: 并把原因写进留痕。
SCAN_REASON_TRUNCATED = "truncated"    # 正文被 `budget` 截断（尾部未表态）
SCAN_REASON_WORK_CAP = "work_cap"      # 总工作量触顶而提前停止（尾部未表态）
SCAN_REASON_RESCAN_LEN = "rescan_len"  # 有候选因「每候选上界」被跳过（该候选未表态）

#: `reason` 的**优先级**（越靠前越"外层"：未表态的区间越靠后、越"整段"）。
#: 多个原因同时成立时取最外层的一个——确定性（与出现顺序无关）。
_SCAN_REASON_PRIORITY = (
    SCAN_REASON_TRUNCATED,
    SCAN_REASON_WORK_CAP,
    SCAN_REASON_RESCAN_LEN,
)

#: JSON 对象首字符（跳过空白后）：`{` 之后**只能**是 `"`（键）或 `}`（空对象）。
#: 这条 O(1) 预检把 `if (x) {` / `"{"` 类输入挡在 `raw_decode` 之外
#: （否则每个候选都要进一次 C 解析器）。
_JSON_WS = " \t\n\r"
_JSON_WS_RE = re.compile(r"[ \t\n\r]*")
_JSON_DECODER = json.JSONDecoder()


def _scan_diag(text: str, limit: int) -> "tuple[List[str], bool, str, int]":
    """扫描 ``text[:limit]`` 里的顶层 ``{...}`` 片段。

    返回 ``(片段清单, degraded, reason, work)``。``degraded=True`` 的含义**只有
    一条**："有某个区间**未表态**"——**已取到的片段一个不丢**（这正是与上一轮
    "静默收摊、整段丢卡"的区别）。``reason`` 说明**哪一段**未表态（三态见
    ``SCAN_REASON_*``；多个原因同时成立时按该处列出的优先级取**最外层**的一个）。

    实现（**构造性"不漏"**）：

    * **候选起算独立**：对每个候选 ``{`` 调 ``raw_decode``（**不共享**前一个候选
      的字符串状态）——等价旧实现"每个起点按括号配平重扫 + ``json.loads``"的
      语义，但解析在 C 里做，且未闭合/非法即 fail-fast。
    * **O(1) 预检**（短路 `"{"×n` / `if (x) {` 类输入）：
      ① 区间内**必须还有** `}`（``rfind``，一次 O(n) 定全局上界）；
      ② ``{`` 之后跳过空白**必须**是 `"` 或 `}`（JSON 对象的唯一两种起头）。
    * **两条上界**（缺一不可）：
      ① **每候选**：窗口 ``[p, p + MAX_RESCAN_LEN)`` 内没有 `}` → 跳过该候选
        （`rescan_len`）；
      ② **总工作量**：折算量超过 ``MAX_SCAN_WORK`` → 停止尝试（`work_cap`）。
    * **降级（只降级、不丢段）**：两条上界都**只**让"未扫到的区间"不表态，
      已取到的片段照常返回。
    * **G8（`budget <= 0` 与截断同一条规则）**：``limit < len(text)`` 即
      `truncated` —— `limit = 0` 时本式自然成立，故调用方（``..._diag``）
      **不得**再为 `budget <= 0` 另设"提前返回"分支（那正是"绕过已定规则"，
      会把"非空正文被整段截断"静默成"未降级"）。**降级只有一个判定处**
      （本函数这一行），这是本文件的口径。
    """
    out: List[str] = []
    # 截断（``limit < len(text)``）同样是"该区间不表态" → 显式标记（不得静默）。
    # **G8**：`limit = 0`（`budget <= 0`）也走这一行 —— 不再有绕过它的分支。
    truncated = limit < len(text)
    # 基扫（一次 ``rfind``）计入记账：总上界因此也是"含基扫"的真实耗时上界。
    work = limit
    hit_work_cap = False
    hit_rescan_len = False
    last_close = text.rfind("}", 0, limit)
    if last_close < 0:
        # 区间内没有 `}`：不可能有配平对象（此时"跳过"不是降级，是确证）。
        return out, truncated, SCAN_REASON_TRUNCATED if truncated else "", work
    i = 0
    while True:
        p = text.find("{", i, limit)
        if p < 0 or p > last_close:
            break
        j = _JSON_WS_RE.match(text, p + 1, limit).end()
        cost = (j - p) + _SCAN_CANDIDATE_COST
        if j >= limit or (text[j] != '"' and text[j] != "}"):
            # 形态不像对象：O(1) 跳过（不进 raw_decode）
            if work + cost > MAX_SCAN_WORK:
                hit_work_cap = True
                break
            work += cost
            i = p + 1
            continue
        # ① 每候选上界：只在窗口内找 `}`（找不到 ⇒ 该候选不表态）
        window_end = min(limit, p + MAX_RESCAN_LEN)
        close = text.find("}", p, window_end)
        scanned = (close + 1 - p) if close >= 0 else (window_end - p)
        if work + scanned + cost > MAX_SCAN_WORK:
            hit_work_cap = True
            break
        work += scanned + cost
        if close < 0:
            # 窗口内没有 `}` ⇒ 该候选不可能在本上界内配平：跳过并显式降级。
            hit_rescan_len = True
            i = p + 1
            continue
        try:
            got, end = _JSON_DECODER.raw_decode(text, p)
        except (ValueError, RecursionError) as exc:
            # 失败代价 = ``raw_decode`` 的**错误位置 ``pos``**（整段前缀）——
            # 异常消息的构造代价随 ``pos`` 线性，取 ``pos - p``（恒 8）会让
            # 记账**永不触顶**（复评第 5 轮实测：320 KB → 2.26 s 而 `degraded`
            # 恒 `False`）。``RecursionError`` 无 pos（深嵌套）：按剩余长度上限计。
            pos = getattr(exc, "pos", None)
            charge = pos if isinstance(pos, int) and pos > p else (limit - p)
            if work + charge > MAX_SCAN_WORK:
                hit_work_cap = True
                break
            work += charge
            i = p + 1            # 失败候选：下一个起点（旧实现跳到配平末尾）
            continue
        if work + (end - p) > MAX_SCAN_WORK:
            hit_work_cap = True
            break
        work += end - p
        if isinstance(got, dict):
            out.append(text[p:end])
        i = end if end > p else p + 1
    flags = {
        SCAN_REASON_TRUNCATED: truncated,
        SCAN_REASON_WORK_CAP: hit_work_cap,
        SCAN_REASON_RESCAN_LEN: hit_rescan_len,
    }
    # 多个原因同时成立 → 取**最外层**（未表态区间最靠后）的一个，确定性。
    reason = next((r for r in _SCAN_REASON_PRIORITY if flags[r]), "")
    return out, bool(reason), reason, work


def scan_json_spans_diag(
        text: Any, budget: Optional[int] = None) -> "tuple[List[str], bool, str]":
    """``scan_json_spans`` 的**带降级原因**版本：``(片段清单, degraded, reason)``。

    为什么降级必须**可观测且带原因**（复评第 5 轮总师裁定 · G4）：上一轮的
    ``MAX_REPAIR_CHARS`` 触顶是**静默收摊**——调用方看不出"这一段没表态"，
    于是下一轮继续漏。三态 ``reason``（``truncated`` / ``work_cap`` /
    ``rescan_len``）让消费侧能区分"正文被截断" / "总量触顶" / "某候选超长"，
    并据此把安全旗标（``fused`` / ``wake``）取**保守方向**、把原因写进留痕。
    """
    if not isinstance(text, str) or not text:
        return [], False, ""
    n = len(text)
    limit = n if budget is None else min(n, max(0, budget))
    # **G8：`budget <= 0` 与截断是同一条规则**（`limit < len(text)`）。
    # 旧实现在此**提前返回** `(out, False)`，把"非空正文被整段截断"静默成
    # "未降级"（总师实测 `scan_json_spans_report(text, 0) → ([], False)`）。
    # 现在**不再**自己判定，一律交给 `_scan_diag` 的唯一那条规则 ——
    # `limit = 0` 时它自然给出 `degraded=True` / `reason="truncated"`。
    # 这样"降级"只有一个判定处（单一语义源），不会再长出"绕过已定规则"的分支。
    spans, degraded, reason, _work = _scan_diag(text, limit)
    return spans, degraded, reason


def scan_json_spans_report(
        text: Any, budget: Optional[int] = None) -> "tuple[List[str], bool]":
    """``scan_json_spans`` 的**带降级标记**版本：``(片段清单, degraded)``。

    保留二元组签名（既有调用方/回归都按二元组解包）；需要知道**为什么**降级时
    用 ``scan_json_spans_diag``（三态 ``reason``）。
    """
    spans, degraded, _ = scan_json_spans_diag(text, budget)
    return spans, degraded


def scan_json_spans(text: Any, budget: Optional[int] = None) -> List[str]:
    """正文里的顶层 ``{...}`` 片段（配平**且** JSON 合法；顺序保留）。

    **语义（与基线 `a7847f9` 的 `extract_json_objects` 同口径，且为超集）**：
    候选 = 正文里每个 ``{``（**独立起算**）；取其中"从该 ``{`` 起能 ``raw_decode``
    出合法 JSON 对象"的那些，按起点顺序输出片段文本。旧实现是"每个起点按括号
    配平扫一遍再 ``json.loads``"——合法对象两种口径结果一致，故

    * **超集召回（不变量）**：长度 **≤ ``MAX_RESCAN_LEN``** 的对象，基线能捞到的，
      本实现**全捞到**（差分 fuzz 判据 `LOST=0`）。**边界明示**：长度超过
      ``MAX_RESCAN_LEN`` 的合法对象会被跳过——但那是**显式降级**
      （`degraded=True` + `reason="rescan_len"`），消费侧取保守方向，
      **不是**静默丢卡；
    * 失败候选按 ``p + 1`` 前进（旧实现跳到配平末尾）→ 可能多捞出失败候选
      **内部**的对象（超集方向；不产生丢卡）。

    **为什么不再用"两段式（线性主扫描 + 有界修复）"**（复评第 5 轮裁定）：
    那条路径的**正确性依赖预算兜底**（"预算够就捞到"），本轮已在兜底路径上
    长出两个 fail-open：引号区间前缀 + 卡（88 B）、>60 KB 长正文 + 卡
    （`issues/TEST-REGRESSION.md`）——卡被整段丢掉 → ``fused=False`` /
    ``wake=True``（**放行**方向）。现在"不漏"是**构造性**的：
    候选独立起算，不看前面的引号/花括号状态。

    ``budget`` 给定时只扫描正文**前 ``budget`` 个字符**（上界远大于任何真实
    交接卡/评论正文；超出部分保守不表态），不改变正常输入的结果。
    """
    return scan_json_spans_diag(text, budget)[0]


def json_objects_in_diag(
        text: Any, budget: Optional[int] = None) -> "tuple[List[dict], bool, str]":
    """``json_objects_in`` 的**带降级原因**版本：``(对象清单, degraded, reason)``。"""
    segs, degraded, reason = scan_json_spans_diag(
        text, DEFAULT_SCAN_BUDGET if budget is None else budget)
    out: List[dict] = []
    for seg in segs:
        try:
            got = json.loads(seg)
        except (ValueError, RecursionError):
            continue
        if isinstance(got, dict):
            out.append(got)
    return out, degraded, reason


def json_objects_in_report(
        text: Any, budget: Optional[int] = None) -> "tuple[List[dict], bool]":
    """``json_objects_in`` 的**带降级标记**版本：``(对象清单, degraded)``。"""
    objs, degraded, _ = json_objects_in_diag(text, budget)
    return objs, degraded


def json_objects_in(text: Any, budget: Optional[int] = None) -> List[dict]:
    """正文里全部顶层 JSON **对象**（坏片段跳过；不抛）。

    与 ``scan_json_spans`` 同一次扫描的结果，只多一层 ``json.loads`` 兜底；
    ``budget`` 缺省同样取 ``DEFAULT_SCAN_BUDGET``（上界**处处生效**，
    不因调用方忘了传就变成无界扫描）。降级标记见 ``json_objects_in_report`` /
    ``json_objects_in_diag``。
    """
    return json_objects_in_diag(
        text, DEFAULT_SCAN_BUDGET if budget is None else budget)[0]
