"""计数口径守卫：跑多少写多少（权责一致），禁止用"全量"模糊本轮实跑数。

用户口径（Issue #14）：
    "如果本轮跑 59 个，你就要写：跑 59 个测试，不要写全量。
     这会形成歧义，导致误导。跑多少就是多少。"

本模块把该口径做成**可执行守卫**——报表/留痕在任何时候都能调用
:func:`assert_counter`，一旦出现「全量」措辞与本轮实跑数不符即拒绝。

复评第 2 轮定点修复（🔴-1：守卫「语境盲区」清零）：

旧实现只做**裸数字出现**判定（``_appears``），于是实跑数可被
**序号 / 规模数 / 已跑过数**冒充，例如：

- ``"复评第 6 轮：全量 359 项全部通过（308 例已跑过）"`` + ``executed=6``
  → 6 只是「第 6 轮」序号，却曾被放行；
- 同句 + ``executed=359`` → 359 是全量规模，走"真全量豁免"被放行；
- 同句 + ``executed=308`` → 308 是「已跑过」数，被放行。

现改为**绑定语境的实跑数匹配**（:func:`_appears_as_executed`）：
``executed`` 必须命中**实跑数专属标记**（``本轮实跑 N`` / ``本轮跑 N`` /
``实跑 N`` / ``executed: N`` / 规范式 ``跑 N 个测试``），且该命中不得
紧后接规模 / 已跑过语境；「真全量豁免」也必须与实跑语境或 pytest
结构化汇总证据（``N passed``）绑定后才生效。
"""
from __future__ import annotations

import re
from typing import Iterable, Optional

# 与"全量"同义的误导性措辞（会把本轮实跑数掩盖成全量规模）
FULL_WORDING_TOKENS = ("全量", "全部通过", "全绿", "全跑", "all pass", "full suite")

# 允许的例外：明确把"全量"用于描述**规模**且同时标出已跑过数的句子，如
# "全量 743 例（其中 684 例已跑过，本轮实跑 59 例）"。
_SCALE_MARKER = re.compile(r"已跑过|此前已跑|默认不重复|规模")

_NUM_PATTERN = r"\d[\d,]*"

# 分项状态词：其后/其前的数字是本轮实跑数的**分解**，不参与"未声明计数"判定。
_CN_STATUS = r"通过|失败|错误|跳过|消解|剔除"
_EN_STATUS = (r"passed|failed|errors?|skipped|xfailed|xpassed|deselected")

# 计数单位：数字 + 单位才视为"计数"（如 "59 例" / "59 个测试"）。
_COUNT_UNIT = r"例|个|项|条|tokens?|tests?"

_COUNT_CONTEXT_RE = re.compile(rf"({_NUM_PATTERN})\s*(?:{_COUNT_UNIT})")
_THOUSANDS_RE = re.compile(r"\d{1,3}(?:,\d{3})+")

# 分项子计数：中文「通过 34」/ 英文「33 passed」两种词序。
_SUBCOUNT_RE = re.compile(
    rf"(?:{_CN_STATUS})\s*[：:]?\s*({_NUM_PATTERN})"
    rf"|({_NUM_PATTERN})\s*(?:{_EN_STATUS})")

# 序号（第 N 轮 / 第 N 次 / 第 N 版）——非计数。
_ORDINAL_RE = re.compile(r"第\s*(\d+)\s*(?:轮|次|版|步|条|点)")

# 裸长数字（commit sha 等，无计数单位）——非计数（后随计数单位者仍算计数）。
_COMMITISH_RE = re.compile(
    rf"(?<![\w,])\d{{7,}}(?![\w,])(?!(?:\s*)(?:{_COUNT_UNIT}))")

# ----------------------------------------------------------------------
# 🔴-1：实跑数**专属标记**（语境绑定）
# ----------------------------------------------------------------------
# 只有这些标记旁的数字才是「本轮真实实跑数」；序号 / 规模数 / 已跑过数
# 即使数值相等也不得冒充。
_CN_EXECUTED_MARKERS = ("本轮实跑", "本轮跑", "本次跑", "本次实跑",
                        "本轮执行", "本轮实际跑", "实跑")
_EN_EXECUTED_MARKERS = (r"executed", r"ran\s+now", r"run\s+now")

# 「本轮实跑 N」：中文标记 + 数字（允许 "本轮实跑: 6 例" / "本轮实跑 6"）
_EXECUTED_CN_RE = re.compile(
    rf"(?:{'|'.join(_CN_EXECUTED_MARKERS)})\s*[：:]?\s*({_NUM_PATTERN})")
# 「executed: N」/「executed N」
_EXECUTED_EN_RE = re.compile(
    rf"(?:{'|'.join(_EN_EXECUTED_MARKERS)})\s*[：:]?\s*({_NUM_PATTERN})",
    re.IGNORECASE)
# 规范渲染式「跑 N 个测试」（:func:`render_count` 的主语写法）
_EXECUTED_CANON_RE = re.compile(
    rf"跑\s*({_NUM_PATTERN})\s*(?:个|例|项|条)(?:测试|用例|case|tests?)?")

# 命中实跑标记的数字若**紧后**接这些语境，说明它其实不是本轮实跑数：
#   「本轮实跑 308 例已跑过」 → 该数字是已跑过数（自相矛盾写法）
#   「本轮实跑 359 规模」     → 该数字是规模数
_EXECUTED_NEG_SUFFIX_RE = re.compile(
    rf"^\s*(?:{_COUNT_UNIT})?\s*(?:已跑过|此前已跑|默认不重复)"
    rf"|^\s*(?:规模|全量)")


class CounterWordingError(ValueError):
    """计数措辞违规：用"全量"表述掩盖本轮实跑数（误导性歧义）。"""


def _as_int(value) -> int:
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        return int(value.replace(",", "").strip())
    raise TypeError(f"无法解析计数: {value!r}")


def render_count(executed: int, total_suite: int = 0,
                 deferred: int = 0, planned: Optional[int] = None,
                 redundant: int = 0) -> str:
    """渲染合法计数措辞（**跑多少写多少**）。

    示例::

        >>> render_count(59, total_suite=743, deferred=684)
        '跑 59 个测试（全量 743 例中 684 例已跑过、默认不重复；本轮实跑 59 例）'
    """
    executed = _as_int(executed)
    text = f"跑 {executed} 个测试"
    if total_suite:
        total_suite = _as_int(total_suite)
        deferred = _as_int(deferred)
        suffix = f"（全量 {total_suite} 例"
        if deferred:
            suffix += f"中 {deferred} 例已跑过、默认不重复"
        suffix += f"；本轮实跑 {executed} 例）"
        text += suffix
    if planned is not None and _as_int(planned) != executed:
        text += f"（计划 {_as_int(planned)} 例）"
    if redundant:
        text += f"（另剔除冗余 {_as_int(redundant)} 例）"
    return text


def _appears(text: str, value: int) -> bool:
    """文本中是否出现该数字（支持千分位；须为独立数字）。

    仅用于**规模数 / 已跑过数**的松散存在性校验；实跑数请用
    :func:`_appears_as_executed`（语境绑定）。
    """
    for form in (str(value), f"{value:,}"):
        if re.search(rf"(?<![\d,]){re.escape(form)}(?![\d,])", text or ""):
            return True
    return False


def _iter_executed_matches(text: str):
    """产出文本中所有「实跑数专属标记 + 数字」的匹配（语境绑定）。"""
    for regex in (_EXECUTED_CN_RE, _EXECUTED_EN_RE, _EXECUTED_CANON_RE):
        for m in regex.finditer(text or ""):
            yield m.group(1), m.start(1), m.end(1)


def _appears_as_executed(text: str, value: int) -> bool:
    """``value`` 是否以**实跑数语境**出现在文本中（🔴-1 核心守卫）。

    判定要点（根治三条 CLI 反例）：

    1. 必须命中「本轮实跑 N / 本轮跑 N / 实跑 N / executed: N / 跑 N 个测试」
       这类**实跑专属标记**；裸数字一律不算——序号「第 6 轮」、规模数
       「全量 359」、已跑过数「308 例已跑过」均不得冒充实跑数；
    2. 命中数字的**紧后**上下文不得是规模 / 已跑过语境
       （如「本轮实跑 308 例已跑过」这类自相矛盾写法）→ 拒绝；
    3. 跨句指代（「本轮实跑见第 6 轮」）因标记后无数字而**天然不命中**。

    这三点合起来即「绑定语境的实跑数匹配」，替代旧实现的裸数字出现判定。
    """
    text = text or ""
    target = str(value).replace(",", "")
    for raw, _start, end in _iter_executed_matches(text):
        if raw.replace(",", "") != target:
            continue
        if _EXECUTED_NEG_SUFFIX_RE.match(text[end:end + 12]):
            continue
        return True
    return False


def _has_pytest_summary_for(text: str, value: int) -> bool:
    """文本是否含该数的 **pytest 结构化汇总**证据（如 ``341 passed``）。

    真全量豁免须有真实执行证据：裸写「全量 N 项全部通过」不构成证据
    （无 ``N passed``），故不得借此把规模数冒充实跑数。
    """
    pattern = re.compile(
        rf"(?<![\d,]){value}(?![\d,])\s+"
        r"(?:passed|failed|errors?|skipped|xfailed|xpassed)",
        re.IGNORECASE)
    return bool(pattern.search(text or ""))


def _strip_non_counts(text: str) -> str:
    """抹掉非计数语境（序号 / commit sha），避免误报。"""
    text = _ORDINAL_RE.sub("第 N ", text or "")
    return _COMMITISH_RE.sub(" ", text)


def _extract_numbers(text: str) -> list:
    """抽取文本中的**计数**（数字 + 计数单位 / 千分位），排除分项子计数。"""
    text = _strip_non_counts(text)
    sub = set()
    for m in _SUBCOUNT_RE.finditer(text):
        raw = m.group(1) or m.group(2)
        if raw:
            sub.add(int(raw.replace(",", "")))
    nums = set()
    for m in _COUNT_CONTEXT_RE.finditer(text):
        nums.add(int(m.group(1).replace(",", "")))
    for m in _THOUSANDS_RE.finditer(text):
        nums.add(int(m.group(0).replace(",", "")))
    return sorted(nums - sub)


def assert_counter(text: str, executed: int, total_suite: int = 0,
                   deferred: int = 0, planned: Optional[int] = None,
                   redundant: Optional[int] = None,
                   remaining: Optional[int] = None) -> int:
    """校验一段留痕文字的计数口径，返回本轮实跑数。

    规则（复评第 2 轮加固，🔴-1）：

    1. **实跑数须以实跑语境出现**：``executed`` 必须命中「本轮实跑 N /
       实跑 N / executed: N / 跑 N 个测试」这类专属标记，**不得**由序号
       （第 N 轮）、规模数（全量 N）、已跑过数（N 例已跑过）冒充；
    2. **「全量」措辞**：出现「全量/全部通过/全绿」等措辞时——
       - 规模数 ≠ 实跑数 → 必须标出「已跑过/默认不重复」数，否则拒绝；
       - 规模数 == 实跑数（真全量）→ **须与实跑语境或 pytest 汇总证据
         绑定**才放行；
       - 未声明规模 → 须有规模标记（已跑过/默认不重复/规模），否则拒绝；
    3. **未声明计数**：文本中的计数须落在合法集合
       {executed, planned, total_suite, deferred, redundant, remaining} 内；
       分项子计数（通过/失败/错误/跳过 N）、序号（第 N 轮）、commit sha 除外。

    Args:
        text: 待校验的留痕/报表文字。
        executed: 本轮**真实实跑**用例数（pytest 汇总口径）。
        total_suite: 全量用例数（0 表示未声明）。
        deferred: 已跑过（默认不重复）用例数。
        planned: 本轮计划用例数（可选，参与合法计数集合）。
        redundant: 冗余剔除用例数（可选）。
        remaining: 未消解差异用例数（可选，全量视图用）。

    Returns:
        int: 本轮实跑数（供调用方直接落账）。
    """
    executed = _as_int(executed)
    total_suite = _as_int(total_suite)
    deferred = _as_int(deferred)
    if not isinstance(text, str):
        raise CounterWordingError(f"计数文本须为字符串，实际 {type(text)!r}")

    # 实跑数须有**语境绑定证据**：实跑专属标记，或 pytest 结构化汇总
    # （``N passed`` = 真实执行产物，非裸措辞）。
    if not (_appears_as_executed(text, executed)
            or _has_pytest_summary_for(text, executed)):
        raise CounterWordingError(
            f"文本未以**实跑语境**出现本轮实跑数 {executed} → 无法核对"
            "「跑多少写多少」，拒绝对未绑定真实实跑数的文字落账"
            "（序号 / 规模数 / 已跑过数不得冒充实跑数；pytest 汇总行 "
            "`N passed` 亦可作为证据）。合法写法："
            f"{render_count(executed, total_suite, deferred)}")

    lowered = (text or "").lower()
    has_full_wording = any(
        tok in text or tok in lowered for tok in FULL_WORDING_TOKENS)

    if has_full_wording:
        if total_suite and total_suite != executed:
            if not deferred or not _appears(text, deferred):
                raise CounterWordingError(
                    f"声明了全量规模 {total_suite} 例（≠ 本轮实跑 {executed}）"
                    "却未标出『已跑过/默认不重复』数 → 读者会把全量数当作"
                    "本轮实跑数。请改写为："
                    f"{render_count(executed, total_suite, deferred)}")
        elif total_suite and total_suite == executed:
            # 真全量：规模 == 实跑。**须与实跑语境 / pytest 汇总证据绑定**
            # 才放行（🔴-1：旧实现仅凭"裸数字出现"即走本豁免，被规模数
            # 「全量 359 项」冒充）。
            if not (_appears_as_executed(text, executed)
                    or _has_pytest_summary_for(text, executed)):
                raise CounterWordingError(
                    f"声明真全量（规模 == 实跑 {executed}）但既无实跑语境"
                    "『本轮实跑 N』也无 pytest 汇总证据（如 `N passed`）→ "
                    "无法确认本轮真的跑了全量。请改写为："
                    f"{render_count(executed, total_suite)}")
        elif not _SCALE_MARKER.search(text):
            raise CounterWordingError(
                "使用「全量/全部通过」等措辞却未标记『已跑过/默认不重复』→ "
                f"会把本轮实跑 {executed} 例误读为全量。请改写为："
                f"{render_count(executed, total_suite, deferred)}")

    allowed = {executed, total_suite, deferred}
    for extra in (planned, redundant, remaining):
        if extra is not None:
            allowed.add(_as_int(extra))
    allowed.discard(0)
    for num in _extract_numbers(text):
        if num in allowed or num <= 0:
            continue
        raise CounterWordingError(
            f"出现未声明的计数 {num}（合法集合: 实跑 {executed} / 计划 "
            f"{planned if planned is not None else '-'} / 全量 {total_suite} / "
            f"已跑过 {deferred} / 冗余 "
            f"{redundant if redundant is not None else '-'} / 未消解 "
            f"{remaining if remaining is not None else '-'}）→ 权责不一致，"
            "拒绝落账。")
    return executed


def assert_ledger_counts(pairs: Iterable, executed: int) -> None:
    """台账计数一致性：``(名称, 值)`` 序列中的总值须等于本轮实跑数。

    用于"台账写 295、实际跑 148"这类漂移（历史台账已多次发生）的即时拦截。
    """
    executed = _as_int(executed)
    total = 0
    names = []
    for name, value in pairs:
        total += _as_int(value)
        names.append(name)
    if total != executed:
        raise CounterWordingError(
            f"计数口径漂移：{' + '.join(names)} = {total} ≠ 本轮实跑 {executed}。"
            "跑多少写多少（权责一致）。")
