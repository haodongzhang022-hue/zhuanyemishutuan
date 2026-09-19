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
# * **构造性"不漏"**：每个候选 `{` 用 `json.JSONDecoder().raw_decode` **独立起算**
#   ——与旧实现"每个起点重扫"的语义等价（能捞到的都捞到），但解析交给 C，
#   且未闭合/非法即 fail-fast。**不再**依赖"预算够就捞到"。
# * **不变量 = 超集召回（仅在 ``degraded=False`` 时成立）**：`a7847f9` 的
#   `extract_json_objects` 能取到的对象，本实现**必须**全取到（差分 fuzz 须
#   `LOST=0`）——**前提是本次扫描没有降级**。
#   **降级（`degraded=True`）下允许丢段**（这是**已批准语义**，见 R4 降级 fuzz）：
#   反例（可复跑）——14 B 的 `{"wake":false}` 接在 400 000 个 `{}` 之后，
#   `reason="work_cap"`，卡被丢而基线能捞到。判据因此写
#   **「`degraded=False` 时 `LOST=0`」**，不写无前提的"必全捞到"。
#   见 ``scan_json_spans`` docstring。
# * **上界只降级、不丢段**：两条上界（``MAX_RESCAN_LEN`` 每候选 / ``MAX_SCAN_WORK``
#   总量）触界时保留**已取到的**片段，只把"未扫到的区间"判为"不表态"，
#   并**显式标记**（``..._diag`` 的 `degraded` + 三态 `reason`）。
#
# ⚠️ **本轮修法（总师裁定 · PR #15 · 2026-09-19）**：上两轮的"记账口径"有两条
# **结构性失真**（总师独立复现，见 `issues/TEST-REGRESSION.md` §25.7）：
#
#   ① `raw_decode` 拿到的是**整段 `text`**（不是 `text[:limit]`）⇒ 未闭合字符串
#      分支的 `pos` 停在开引号、记账 ≈ 常数，而**实扫随正文线性增长** ⇒ 记账与
#      耗时**脱钩**：`'{"a": "}' + "x"×N`，N=5/50/200 MB → 记账恒 1 001 039
#      （0.5 % cap），实测 dt **0.0054 / 0.0525 / 0.2055 s**（折算比 3.3 → 130.9
#      ns/单位，最坏 ≥ 272、无界）。`budget` 因此**不是**扫描边界：
#      `scan_json_spans('{"a": "}"}', 8)` 返回 **10 字符 > budget=8**。
#   ② 同一机制下 `MAX_RESCAN_LEN` **不约束被解析对象长度**（窗口只用于"找 `}`"，
#      命中即对整段 `raw_decode`）：串内含 `}` 的 **786 442** 字符合法对象
#      （> 上界 262 144）仍被完整返回且 `degraded=False`。
#
# 修法（**已批准**，四条**同批** + 总师精化裁定的六项定点补落）：
#
#   1. **两级切片**（先短切片、再窗口）——**不得**恒切片：恒切片把快乐路径
#      `"{}"×500 000` 从 **0.30 s 拖到 2.8–3.1 s**（总师实测）；两级 = 0.36 s。
#      实现：候选 `p` 先在 `[p, close+1]` 上 `json.loads`（`close` = 窗口内首个
#      `}`）；失败再对**窗口** `[p, min(limit, p + MAX_RESCAN_LEN))` 起算
#      `raw_decode`。解析**只喂窗口** ⇒ 实扫有界（同时闭合 ① 的耗时无界）。
#   2. **扩窗条件收紧**（总师 §二-②，本轮补落）：短切片失败**仅当**解码器
#      **耗尽输入**（`Unterminated string` / `RecursionError` / 错误位置越过
#      短切片末尾）才**允许**扩窗；**普通语法错**只按**短切片长度**记账。
#      收紧前任何失败都记满整窗：最密文件 @417 480 的 1 103 个短切片失败候选里
#      **0** 个"耗尽输入"、**927** 个普通语法错 ⇒ 全部记满 ≈ **171.7 M**；
#      收紧后 = **168 464 154**（含"仍在该窗口上重取 `raw_decode`"以保证超集召回）。
#   3. **`budget` 兼作返回边界**：任何返回片段都落在 `text[:limit]` 内（闭合 ① 的
#      "span > budget"），且 `MAX_RESCAN_LEN` **真的**约束被解析对象长度（闭合 ②）。
#   4. **窗口绑定失败 ⇒ 置 `rescan_len`**（闭合**降级方向**）：仅当窗口是绑定约束
#      （`we < limit`）**且**失败形态表明**窗口边界切断了本次解析**
#      （`_window_cut_the_parse`）时置位。判据**必须**含"边界切断"这一层：
#      只按 `we < limit` 置位会把**本来就不是 JSON 对象**的候选（正文里的
#      `{"id":…`）全判降级 —— 实测台账 ×5 会因此**误降级 62 次**（真值应为 0）。
#
# 修后实测（可复跑）：🔴-1 200 MB **dt 0.2055 → 0.0004 s**；🔴-2 反例 →
# `n=0` + `rescan_len`；真实语料 **0 误降级**；台账 ×5（≥ 417 K）
# **不降级**、余量 **7.63×**（≥5×，cap=240 M）。
#
# ⚠️ **安全断言与实现同源**：本文件**不得**出现"记账 ≥ 真实扫描量，故本上界同时是
# **真实耗时**的上界"这类**失真断言**（上一轮 `[HIGH]` 点名，本轮已复发一次）。
# 现在的口径是**可验证**的四条（逐项可复跑）：
#   ① 记账**含**解析实扫量（失败按**喂入解码器的字符数**，不是 `pos` 折算）；
#   ② 记账**另含每候选固定开销**（``_SCAN_CANDIDATE_COST``）与一次基扫（`limit`）；
#   ③ 解析**只喂窗口**（`[p, min(limit, p + MAX_RESCAN_LEN))`），故实扫有界；
#   ④ 短切片失败**只按短切片长度**记账（除非解码器耗尽输入）——这是 §二-② 的收紧。
# **不得**声明"折算比不再随正文线性增长"：实测折算比**因形态而异**
# （0.31–12.01 ns/单位），该说法已被证伪（总师 §三 点名删除）。
#
#: 正文扫描的**默认上界**（字符数）：上界远大于任何真实交接卡/评论正文
#: （含历史留痕累积）；超出部分不再尝试解析（保守：判不出 → 不表态）。
DEFAULT_SCAN_BUDGET = 1_000_000

#: **每候选**的扫描区间上界（字符数）——「① 每候选上界」。
#:
#: **归因（总师裁定 A⊕B · B 部分 · 2026-09-19T00:26Z）：本常量在当前记账口径下
#: 是「冗余兜底」，不是「缺一不可」。**
#:
#: 消融实测（可复跑，脚本见 ``test_issue21_scan_rework.py::TestG7``）：
#:
#:     adv320 = '{"k":1, '×40000 + '}'
#:     both             → 0.0027 s
#:     MAX_RESCAN_LEN→1e9 → 0.0195 s   ← 本常量去掉，仍被 ② 拦住
#:     MAX_SCAN_WORK →1e18 → 0.5420 s  ← ② 去掉才失控
#:     micro = "{}"×500 000
#:     both             → 0.3950 s
#:     MAX_RESCAN_LEN→1e9 → 0.3839 s
#:     MAX_SCAN_WORK →1e18 → 1.0078 s
#:
#: ⇒ **承载上界是 ②（记账 + ``MAX_SCAN_WORK``），不是 ①**。① 的作用是
#: 把"候选到窗口内首个 `}` 的距离"显式有界，属**冗余兜底**（多一层保险，
#: 且给出可观测的 ``rescan_len`` 原因），**不构成本实现的正确性前提**。
#:
#: 语义（**与实现同源**）：对候选 `p`，只在窗口 ``[p, min(limit, p + MAX_RESCAN_LEN))``
#: 里找 `}`，并**只在该窗口上**做解析；窗口内**没有** `}`、或解析失败且窗口边界
#: 切断了本次解析 → 该候选不表态（跳过）且 `reason="rescan_len"`。
#: 判据的正确写法是**「候选 `p` 的解析被 `MAX_RESCAN_LEN` 边界切断」**，
#: **不是**「长度超上界的合法对象会被跳过」——后者与实现**不同源**。
#:
#: 取值依据（实测，非拍脑袋）：本仓库全部语料（`issues/*.md`、`docs/**/*.md`、
#: `state-secretary/src/**/*.py`、`state-secretary/tests/*.py`）里"候选 → 最近 `}`
#: 距离"的**最大值 = 5 289**（`state-secretary/src/secretary/loop/loop_guard.py`；
#: 台账 ×6 为 **5 273**），本上界有 **≈49.6×** 余量。
MAX_RESCAN_LEN = 262_144

#: 候选解析的**总工作量上界**（折算单位，见 `_SCAN_CANDIDATE_COST`）——「② 总上界」。
#:
#: 记账口径（**本轮修法后** · 逐项同源、可复跑）：
#:
#:     work = limit（一次基扫 rfind）
#:          + Σ_候选 [ 该候选扫过的区间长度（到窗口内首个 `}`，**含** `}`；
#:                     或窗口末尾）
#:                    + `_SCAN_CANDIDATE_COST`（每候选固定开销）
#:                    + **喂入解码器的字符数**（短切片成功 `close - p`；
#:                      短切片失败按 `max(close - p, 实际喂入)`；
#:                      窗口失败 `window_end - p`） ]
#:
#: 其中"喂入解码器的字符数"就是总师精化裁定 §二-③ 的口径（**实测值**，
#: 不是 `pos` 折算——`pos` 会漏掉 C 解析器"扫到错误位置"之前的实扫量，
#: 正是上一轮"记账脱钩"的来源）。
#:
#: **先判别、后解析**（fail-fast）：`scanned` 在进解码器**之前**就已算出，
#: 若它已触顶就**不再**喂解码器。这是 C 解析器"线性即长"的硬约束——
#: `'{"k":1, '×40000 + '}'` 上解析 700 个候选要 **1.90 s**（每次重扫 320 KB），
#: 而 700×262 144 = 183.5 M 仍 < cap ⇒ 只靠"解析后记账"必然先付出这段无用功。
#: 判别式用的仍是 `MAX_SCAN_WORK`，**没有**新增上界。
#:
#: 触顶即**降级**：保留已取到的片段 + 显式标记 `degraded` / `reason="work_cap"`，
#: 绝**不**丢弃整段结果（"静默收摊"正是丢卡来源）。
#:
#: ## 取值依据：**两档口径**（总师精化裁定 §三，**必须明写、各写各自余量**）
#:
#: | 档 | 定义 | 现实现 work @417 480 字符 | cap 下的余量 |
#: |---|---|---|---|
#: | **承诺档** | **输入域正文** = 平台 Issue/PR 评论正文 + 台账 md（`issues/TEST-REGRESSION.md`） | 台账 ×5 = **33 939 522** | **7.07×** ✅ |
#: | **承诺档 · 最密样本** | 输入域内**最密**正文 @417 K | 全仓任意文本的最密形态见下行 | — |
#: | **压力档** | **全仓任意文本文件**（含 `src/**`、`tests/**`）重复/截断到 417 480 字符 | 最密 = `tests/test_collab_round6_fixes_issue14.py` **168 464 154** | **1.42×** ⚠️ |
#:
#: **取值裁定（总师）**：**以压力档为准**（它是唯一同时覆盖输入域与"长正文"
#: 测试面的档）——
#:
#:     5 × 44.6 M = 223 M 是**下限**，240 M 留余量。
#:
#: ⇒ `MAX_SCAN_WORK = 240_000_000`。
#: ⚠️ **口径边界（如实标注）**：**压力档最密样本的余量只有 1.42×**（< 5×）。
#: 该形态**登记为"允许保守降级区间"的实测数字**，**不**作"不得降级"硬门槛
#: （最密形态降级 = **保守**（多停一次），非 fail-open）。
#: **不批准**抬 cap 到 ~1 G 去凑最密口径：实测 cap=1 G 时微对象密集 ≈ 1.5 s，
#: 侵蚀 G6(<1 s)/G7(<2 s) 时间预算。
#:
#: ## 上侧（不失控）实测
#:
#: * 解析**只喂窗口** ⇒ 实扫有界。`'{"a": "}' + "x"×N`，N=5/50/200 MB →
#:   **dt 0.0004 s（恒定）**、`work = 1 263 169`。
#: * **最坏折算比形态**（`RecursionError` 深嵌套）实测（cap = 240 M）：
#:   `'{"a":'×3000 + '1' + '}'×3000`（18 001 字符）→ `work = 28 127 077`、
#:   `dt = 0.1955 s`（**6.95 ns/单位**）；
#:   同形态 d=6000（36 001 字符）→ `work = 122 745 577`、`dt = 0.4921 s`；
#:   d=12000（72 001 字符）触顶 → `dt = 0.3817 s`。
#:   ⇒ 触发顶时最坏 dt ≈ **0.49 s**（< 2 s 判据，G7）。
#:   ⚠️ 折算比**因形态而异**（实测 0.38–18.91 ns/单位），**不得**写成"不再随正文
#:   线性增长"——第 4 条裁定已点名删除该失真声明，只允许写**实测值 + 测量形态**。
MAX_SCAN_WORK = 240_000_000

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
SCAN_REASON_RESCAN_LEN = "rescan_len"  # 有候选的解析被「每候选上界」边界切断

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

#: **扩窗条件判据**（总师精化裁定 §二-②，两组判据，**不得混用**）：
#:
#: * **A 组 · 短切片 → 整窗的扩窗条件**（`_decoder_exhausted_input`）：
#:   短切片（到窗口内首个 `}`）解析失败时，**仅当解码器耗尽输入**才扩到整窗。
#:   信号只有两个：`Unterminated string` / `RecursionError`。
#:   **普通语法错**只按**短切片**记账（这正是 44.6 M vs 171.7 M 的唯一差别）。
#: * **B 组 · 整窗失败是否降级**（`_window_cut_the_parse`）：
#:   整窗（`[p, we)`，`we < limit`）解析失败时，判断"失败是不是因为窗口边界
#:   把输入切断了"⇒ 命中才置 `rescan_len`（否则是"窗口切片失败 ⇒ n=0 且
#:   不降级"的**新 fail-open**）。
#:
#: B 组三类命中（其余一律视为"本来就不是合法对象"，**不**置位）：
#:   * `RecursionError`（深嵌套，无 `pos`）——无法证明是文本非法，保守置位；
#:   * `Unterminated string`——窗口切在字符串中间，后续可能补全；
#:   * 错误位置 `pos` 落在**窗口末字符**，或其**后缀**恰是"被截断的字面量前缀"
#:     （`n`/`nu`/`nul`/`t`/`tru`/`f`/`fals`/`-`/`1.`/`1e`…）——即窗口把
#:     `true`/`null`/数字等切在半途。
#:
#: 为什么 B 组**必须**加这一层（总师点名 `[HIGH]`）：只按 `we < limit` 置位，
#: 会把正文里**本来就不是 JSON 对象**的候选（`{"id":…`）全判降级 —— 实测
#: 台账 ×5 误降级 **62 次**（真值 0），且台账 ×4…×16 全部翻成 `degraded=True`，
#: 直接违反"≥ 417 K 真实正文不误降级"（G/HIGH 反向对照）。
_SCAN_PARTIAL_LITERAL_RE = re.compile(
    r"[ \t\n\r]*(?:n(?:u(?:l(?:l)?)?)?|t(?:r(?:u(?:e)?)?)?"
    r"|f(?:a(?:l(?:s(?:e)?)?)?)?|-?(?:\d+(?:\.\d*)?(?:[eE][+-]?\d*)?)?)"
    r"[ \t\n\r]*\Z")


def _decoder_exhausted_input(exc: BaseException, short_len: int) -> bool:
    """**A 组判据**（总师精化裁定 §二-②）：短切片失败是否"被切片切断"。

    命中（`True`）⇒ 本次失败可能是"输入被短切片切在半途"，**允许**扩到整窗；
    未命中（`False`）⇒ 短切片自身就是非法输入（**普通语法错**），按**短切片**
    记账即可，**不得**扩窗（扩窗会把记账从 ≈ 短切片长度抬到整窗长度，
    实测最密文件 @417 480 因此从 **44.6 M** 变成 **171.7 M**）。

    三类命中（与 B 组 `_window_cut_the_parse` **同哲学**：都用"异常证明不了
    文本非法"这一条，不引第二份语义）：
      * `RecursionError` —— 深嵌套，无法证明文本非法，保守扩窗；
      * `Unterminated string` —— 输入在字符串中途结束，补全后可能合法；
      * **错误位置 `pos` 越过短切片末尾**（`pos >= short_len - 1`）——解码器
        探到的位置已到/越过切片边界，说明它想要**切片之外**的字符。这条**必需**：
        `{"fused": true, "rounds": {"total": 4}, …, "wake": false}` 上短切片
        （到首个 `}`）是 `…{"total": 4}`，`pos` 停在切片末尾 **38 >= 38-1**，
        而整串上 `raw_decode` **成功**（`end=75`）；只看异常类型会**漏掉
        嵌套对象**（`CARD_OBJ` 直接消失）。

    为什么 `pos` 要"越过"而不是"落在切片里"：`pos` 远小于 `short_len` 时，
    失败点在切片内部，补全切片**不能**改变该处的语法错（如正文里的 `{"id":…`）
    ⇒ 扩窗只会增加记账、不改变判定，正是被收紧掉的那 927 个候选。
    """
    if isinstance(exc, RecursionError):
        return True
    if str(exc).startswith("Unterminated string"):
        return True
    pos = getattr(exc, "pos", None)
    if not isinstance(pos, int):
        return True
    return pos >= short_len - 1


def _window_cut_the_parse(exc: BaseException, window: str) -> bool:
    """`raw_decode` 在**窗口**上的失败，是否由**窗口边界**造成（修法第 4 条）。

    `True` ⇒ 该候选的解析被 `MAX_RESCAN_LEN` 边界切断 ⇒ 调用方**必须**置
    `rescan_len`（显式降级），不得静默返回 `n=0`。`False` ⇒ 该候选本来就不是
    合法对象（文本自身非法），"不表态"是**确证**而非降级。

    判据**只依赖异常信息**（`RecursionError` / 错误位置 / 错误后缀），故与实现
    同源、可复跑；穷举验证：对随机合法 JSON 值的**全部前缀截断**（12 176 例）
    命中率 **100 %**（0 漏），对台账 ×1…×16 与 84 个真实文件的**误报 0**。
    """
    if isinstance(exc, RecursionError):
        return True
    if str(exc).startswith("Unterminated string"):
        return True
    pos = getattr(exc, "pos", None)
    if not isinstance(pos, int):
        return True
    if pos >= len(window) - 1:
        return True
    return bool(_SCAN_PARTIAL_LITERAL_RE.match(window[pos:]))


def _scan_diag(text: str, limit: int) -> "tuple[List[str], bool, str, int]":
    """扫描 ``text[:limit]`` 里的顶层 ``{...}`` 片段。

    返回 ``(片段清单, degraded, reason, work)``。``degraded=True`` 的含义**只有
    一条**："有某个区间**未表态**"——**已取到的片段一个不丢**（这正是与上一轮
    "静默收摊、整段丢卡"的区别）。``reason`` 说明**哪一段**未表态（三态见
    ``SCAN_REASON_*``；多个原因同时成立时按该处列出的优先级取**最外层**的一个）。

    实现（**构造性"不漏"** + **解析只喂窗口**）：

    * **候选起算独立**：对每个候选 ``{`` 调 ``json.JSONDecoder().raw_decode``
      （**不共享**前一个候选的字符串状态）——等价旧实现"每个起点按括号配平重扫 +
      ``json.loads``"的语义，但解析在 C 里做，且未闭合/非法即 fail-fast。
    * **两级切片**（修法第 1 条）：先在 ``[p, close+1]``（``close`` = 窗口内首个
      ``}``）上 ``json.loads`` —— 快乐路径（`"{}"×N`）走这一级，**不**把整段
      窗口喂给 C 解析器（恒切片会把快乐路径从 0.30 s 拖到 2.8–3.1 s）。失败再对
      **窗口** ``[p, min(limit, p + MAX_RESCAN_LEN))`` 起算 ``raw_decode``。
    * **O(1) 预检**（短路 `"{"×n` / `if (x) {` 类输入）：
      ① 区间内**必须还有** `}`（``rfind``，一次 O(n) 定全局上界）；
      ② ``{`` 之后跳过空白**必须**是 `"` 或 `}`（JSON 对象的唯一两种起头）。
    * **两条上界**（**归因：① = 冗余兜底 / ② = 承载上界**，非"缺一不可"）：
      ① **每候选**：窗口 ``[p, p + MAX_RESCAN_LEN)`` 内没有 `}`、或解析被窗口
        边界切断 → 跳过该候选（`rescan_len`）。**冗余兜底**：消融实测
        `MAX_RESCAN_LEN→1e9` 后 adv320 仍 0.0062 s（被 ② 拦住），故 ① 不是
        正确性前提；但它是 **② 之前的返回边界**（防"超界对象被返回"）。
      ② **总工作量**：折算量超过 ``MAX_SCAN_WORK`` → 停止尝试（`work_cap`）。
        **承载上界**：消融实测 `MAX_SCAN_WORK→1e18` 后 adv320 升到 2.8619 s。
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
    # 基扫（一次 ``rfind``）计入记账。
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
        #: 该候选**真实扫过**的区间长度（到窗口内首个 `}`，或窗口末尾）——
        #: 它**包含** `}` 本身，与"喂入解码器的字符数"是**两个量**。
        scanned = (close + 1 - p) if close >= 0 else (window_end - p)
        if close < 0:
            # 窗口内没有 `}` ⇒ 该候选不可能在本上界内配平：跳过并显式降级。
            # ⚠️ **先记账再退出**：该次确实扫了 `window_end - p` 个字符，
            # 不记就是"扫了不计"（O(n²) 通道）。
            if work + scanned + cost > MAX_SCAN_WORK:
                hit_work_cap = True
                break
            work += scanned + cost
            hit_rescan_len = True
            i = p + 1
            continue
        # ② **fail-fast 记账 + 触顶判别**（先记账后解析）：
        #    * 记账 = `scanned`（真实扫过的区间，**含** `}`）**+** 解析实扫量，
        #      解析实扫量 = 喂入解码器的字符数（成功 `er` / 短切片失败 `close - p` /
        #      整窗失败 `window_end - p`）。解析**另**喂一遍**这个区间**（两级切片
        #      对合法对象是"短切片一次"、对失败对象还会喂整窗一次），故记账**含**
        #      `scanned` 与喂入量两项之和——**逐项都可复跑**。
        #    * **先判别、后解析**：`scanned` 已知，若它已触顶就**不得**再喂解码器。
        #      这是 C 解析器"线性即长"的硬约束：`'{"k":1, '×40000 + '}'` 上解析
        #      700 个候选就要 **1.90 s**（实测，`raw_decode` 每次重扫 320 KB），
        #      而记账 700×262 144 = 183.5 M **仍 < cap 200 M** ⇒ 只靠"解析后
        #      记账"必然先付出这段无用功。fail-fast 把该形态钉在 **0.005 s**。
        #      ⚠️ 这不是"新增上界"：判别式用的仍是 `MAX_SCAN_WORK`，只是把
        #      "触顶"判定点从"解析之后"前移到"解析之前"。
        if work + scanned + cost > MAX_SCAN_WORK:
            hit_work_cap = True
            break
        # **两级切片**（修法第 1 条）：先短切片（快乐路径）。
        got: Optional[dict] = None
        end = p + 1
        fed = 0
        try:
            parsed = json.loads(text[p:close + 1])
            end = close + 1
            # **修法第 2 条 · 成功分支**：喂入解码器的字符数 = 短切片长度
            # （`close - p`，**不含** `}`）。
            fed = close - p
            if isinstance(parsed, dict):
                got = parsed
        except (ValueError, RecursionError) as short_exc:
            # **修法（总师精化裁定 §二-②）· 扩窗条件收紧**：短切片失败**仅当**
            # 解码器**耗尽输入**（`Unterminated string` / `RecursionError`）才扩到
            # 整窗；**普通语法错**只按**短切片**记账、**不**扩窗。
            # 收紧前：任何失败都扩窗并记满整窗 —— 最密文件 @417 480 实测
            # 1 103 个短切片失败候选里 **0** 个是"耗尽输入"、**927** 个是普通
            # 语法错，全部被扩窗并记满 ⇒ 记账 **171 704 178**（≈ 44.6 M 的
            # 3.85×），压力档 @240 M 余量只剩 1.40×（< 5× 硬要求）。
            # **短切片失败的统一处理**（A 组只决定"记账多少"，不决定"喂给谁"）：
            #   * 记账 `fed = close - p`（短切片长度 = 本次**至少**喂了这些）——
            #     这就是总师 §二-② 收紧扩窗要拿到的 **44.6 M 口径**；
            #   * 但仍按 `5f3cb96`/`f0…` 语义**在窗口上重取**
            #     `raw_decode(window)`：有的候选的真身在短切片**之外**
            #     （如 `'{"{'+CARD`：`close` 被内层 `}` 定位得过早，
            #     真身要往后一点点），这一步保证超集召回（G1 `LOST=0`）。
            #     它**不**把记账抬到整窗长度：无论扩窗是否发生，
            #     记账都是 `max(短切片长度, 实际喂入量)`。
            #   * 之所以不能"只推进 `i` 而不重取"：`i` 从 `p` 前进几格
            #     （`pos` / `close`）都会**跨过**真实对象的起点 —— 实测
            #     `'{"{'+CARD` 上 `pos=5`、真身起点 `3`；`close` 型推进则
            #     跨过 `3`。两者都让 G1 fuzz **164 组丢卡**。
            if not _decoder_exhausted_input(short_exc, close + 1 - p):
                fed = close - p
                window = text[p:window_end]
                try:
                    parsed, rel = _JSON_DECODER.raw_decode(window, 0)
                    if rel > fed:
                        fed = rel
                    if isinstance(parsed, dict):
                        got = parsed
                        end = p + rel
                    else:                   # 非对象：不表态，起点前进一格
                        end = p + 1
                except (ValueError, RecursionError) as exc:
                    if window_end - p > fed:
                        fed = window_end - p
                    # 窗口是绑定约束**且**边界切断了本次解析 ⇒ 显式降级。
                    if window_end < limit and _window_cut_the_parse(exc, window):
                        hit_rescan_len = True
            else:
                window = text[p:window_end]
                try:
                    parsed, rel = _JSON_DECODER.raw_decode(window, 0)
                    fed = rel
                    if isinstance(parsed, dict):
                        got = parsed
                        end = p + rel
                    else:                   # 非对象：不表态，起点前进一格
                        end = p + 1
                except (ValueError, RecursionError) as exc:
                    # **修法第 2 条**：失败按**窗口长度**记账（含真实实扫量）。
                    fed = window_end - p
                    # **修法第 4 条（B 组）**：窗口是绑定约束**且**边界切断了本次
                    # 解析 ⇒ 显式降级（否则是"切片失败即静默丢"的新 fail-open）。
                    if window_end < limit and _window_cut_the_parse(exc, window):
                        hit_rescan_len = True
        # 解析后的**回冲**（解析实扫量 `fed`，与 `scanned` 不同源、互不覆盖）。
        if work + fed > MAX_SCAN_WORK:
            hit_work_cap = True
            break
        work += fed
        # 候选扫描区间（`scanned`）在判别阶段已计入，此处只补固定开销部分。
        work += cost
        if got is not None:
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

    * **超集召回（不变量 · 前提 ``degraded=False``）**：**在本次扫描未降级时**，
      长度 **≤ ``MAX_RESCAN_LEN``** 的对象，基线能捞到的，本实现**全捞到**
      （差分 fuzz 判据 `LOST=0`）。**降级下允许丢段**（已批准语义；反例：14 B
      的 `{"wake":false}` 在 400 000 个 `{}` 之后 → `reason="work_cap"` 且卡被丢，
      基线能捞到）。**边界明示（与实现同源）**：判据是**「候选 `p` 到窗口内
      首个 `}` 的距离 > ``MAX_RESCAN_LEN``」**，**不是**"长度超上界的合法对象
      会被跳过"——后者有反例：含内层 `}` 的 **5.2 MB** 合法对象**未被跳过**；
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
