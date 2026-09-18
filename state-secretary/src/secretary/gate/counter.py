"""计数口径守卫：跑多少写多少（权责一致），禁止用"全量"模糊本轮实跑数。

用户口径（Issue #14）：
    "如果本轮跑 59 个，你就要写：跑 59 个测试，不要写全量。
     这会形成歧义，导致误导。跑多少就是多少。"

本模块把该口径做成**可执行守卫**——报表/留痕在任何时候都能调用
:func:`assert_counter`，一旦出现「全量」措辞与本轮实跑数不符即拒绝。
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


# 计数语境：数字后紧跟计数单位（例/个/项/条/次/轮/通过/失败/错误），
# 或为带千分位逗号的数（如 1,234）。裸增长串（commit sha）不视为计数。
_COUNT_CONTEXT_RE = re.compile(
    rf"({_NUM_PATTERN})\s*(?:例|个|项|条|通过|失败|错误|tokens?|tests?)")
_THOUSANDS_RE = re.compile(r"\d{1,3}(?:,\d{3})+")
_COMMITISH_RE = re.compile(r"^\d{6,}$")


def _extract_numbers(text: str) -> list:
    nums = set()
    for m in _COUNT_CONTEXT_RE.finditer(text):
        nums.add(int(m.group(1).replace(",", "")))
    for m in _THOUSANDS_RE.finditer(text):
        nums.add(int(m.group(0).replace(",", "")))
    return sorted(nums)


def assert_counter(text: str, executed: int, total_suite: int = 0,
                   deferred: int = 0) -> int:
    """校验一段留痕文字的计数口径，返回本轮实跑数。

    规则：
    1. 出现「全量」类措辞时，必须**同时**具备规模标记（已跑过/默认不重复/
       规模）——否则视为"把本轮实跑数说成全量"→ 抛 :class:`CounterWordingError`；
    2. 出现「全量」类措辞且报了全量数时，若未同时标出已跑过数 → 抛错
       （用户原话：全量 743 个会被读成本轮跑了 743 个）；
    3. 该文字中的数字须落在 {executed, total_suite, deferred} 合法集合内，
       出现"凭空的第三个数"（如未消解差异未声明）→ 抛错。

    Args:
        text: 待校验的留痕/报表文字。
        executed: 本轮实跑用例数。
        total_suite: 全量用例数（0 表示未声明）。
        deferred: 已跑过（默认不重复）用例数。

    Returns:
        int: 本轮实跑数（供调用方直接落账）。
    """
    executed = _as_int(executed)
    total_suite = _as_int(total_suite)
    deferred = _as_int(deferred)
    if not isinstance(text, str):
        raise CounterWordingError(f"计数文本须为字符串，实际 {type(text)!r}")

    lowered = text.lower()
    has_full_wording = any(
        tok in text or tok in lowered for tok in FULL_WORDING_TOKENS)

    if has_full_wording:
        if not _SCALE_MARKER.search(text):
            raise CounterWordingError(
                "使用「全量/全部通过」等措辞却未标记『已跑过/默认不重复』→ "
                f"会把本轮实跑 {executed} 例误读为全量。请改写为："
                f"{render_count(executed, total_suite, deferred)}")
        if total_suite and not deferred:
            raise CounterWordingError(
                "声明了全量规模但**未标出『已跑过』数量** → 读者仍会把全量数"
                f"当作本轮实跑数（误导性歧义）。请改写为："
                f"{render_count(executed, total_suite, deferred)}")
        if total_suite and deferred and str(deferred) not in text:
            raise CounterWordingError(
                "声明了全量规模但未标出『已跑过』数量 → 读者仍会把全量数"
                f"当作本轮实跑数。请改写为："
                f"{render_count(executed, total_suite, deferred)}")

    allowed = {executed, total_suite, deferred}
    allowed.discard(0)
    for num in _extract_numbers(text):
        if num in allowed:
            continue
        if num <= 4:
            # 1-4 之类的小数字常为条目序号/轮次（如「复评第 5 轮」例外），
            # 仅当它等于任一合法计数时才计入；此处放行极小数字避免误报。
            continue
        raise CounterWordingError(
            f"出现未声明的计数 {num}（合法集合: 实跑 {executed} / "
            f"全量 {total_suite} / 已跑过 {deferred}）→ 权责不一致，拒绝落账。")
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
