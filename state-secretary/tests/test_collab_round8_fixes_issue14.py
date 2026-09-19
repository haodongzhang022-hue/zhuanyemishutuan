"""Issue #14 PR #15 **最终收口轮**（N1 / N2 / N3 + 🟡a / 🟡b）判别性回归

触发：架构·评审 blocked 上报（第 4 次打回）→ 架构·总师裁定
**改实现路径 + 本 PR 内收口 + 范围冻结（最终收口轮）**。

本文件只覆盖裁定冻结的 5 项，**不引入新机制、不做重构**：

| 项 | 缺陷（总师/评审独立复跑） | 本文件判据 |
|---|---|---|
| **N1** | 引号区间前缀 + 卡（88 B）：旧两段式扫描把卡**整段丢掉** → `fused=False` / `wake=True`（**单向 fail-open**） | 卡恢复可见且 `fused=True` / `wake=False`（两序都测） |
| **N2** | `issues/TEST-REGRESSION.md`(79 KB) + 卡：卡被丢（`n_objs` 3→1），机制 = 修复段上界触顶**整段收摊** | 卡恢复；`n_objs` 不少于基线；**超集召回**（基线对象全在） |
| **N3** | `_merge_budget_values` 的 `None` **参与比较**（`max(9, None, key=repr)` → `'None' > '9'`）→ `[{"x":None},{"x":9}]` 与 `[{"x":9},{"x":None}]` **结论不同** | 两序**同结论**；`None` 视为缺席；真顺序无关 |
| **🟡a** | 反向对照（判别性用例） | 引号区间前缀 + 卡 / >60 KB 长正文 + 卡：**基线能捞到的对象新实现全捞到** |
| **🟡b** | `merged["budget"]` 若被采纳 = `max()` **抬高上界 = 削弱熔断**（fail-open） | 载荷 `budget.max_rounds=99` **不得**改 `self.max_rounds`；`budget` 段只作观测 |

**不变量（本轮的硬判据）**：超集召回 —— `a7847f9` 的 `extract_json_objects`
能取到的对象，新实现**必须全取到**（差分 fuzz `LOST=0`；本文件用
`_baseline_objects()` 把基线实现**内联复刻**，故回归不依赖旧提交可检出）。

**触顶只降级、不丢卡**：降级（正文超 `DEFAULT_SCAN_BUDGET` / 工作量触顶）
时安全旗标取**保守方向**，且降级**显式标记**（`*_report()`）。
"""
from __future__ import annotations

import json
import os
import random
import tempfile
import time

import pytest

from secretary.loop import LoopGuard, extract_json_objects

# ---- 判别性验证用的**取用层**（只为让本文件在**旧基线**上也能收集） ----
# 本轮的判别性自证要求"把新测试放到基线 `a7847f9` 上跑 → 失败"。基线**没有**
# `secretary.trail`、也没有 `*_report` / `MAX_SCAN_WORK` 这些新符号；若直接
# `import` 会在**收集期**报错（那是"跑不起来"，不是"判据失败"）。故这里逐个
# 取用并给出**旧行为**兜底：兜底值使相关断言**失败**（而不是 error），
# 判别力因此可读、可计数。
try:                                    # 基线无 trail 模块
    from secretary import trail as trail_mod
except ImportError:                     # pragma: no cover - 仅基线
    trail_mod = None

DEFAULT_SCAN_BUDGET = getattr(trail_mod, "DEFAULT_SCAN_BUDGET", 1_000_000)
MAX_SCAN_WORK = getattr(trail_mod, "MAX_SCAN_WORK", None)
scan_json_spans = getattr(trail_mod, "scan_json_spans", None)
scan_json_spans_report = getattr(trail_mod, "scan_json_spans_report", None)
json_objects_in_report = getattr(trail_mod, "json_objects_in_report", None)
extract_json_objects_report = getattr(
    __import__("secretary.loop", fromlist=["x"]),
    "extract_json_objects_report", None)


def _objects(text, budget=None):
    """对象清单（新旧实现同入口）：走 `loop_guard.extract_json_objects`。"""
    return list(extract_json_objects(text))


def _objects_report(text, budget=None):
    """``(对象清单, degraded)``；旧实现无标记 → 兜底 ``False``（断言因此失败）。"""
    if extract_json_objects_report is None:
        return _objects(text), False
    return extract_json_objects_report(text, budget)


def _spans_report(text, budget=None):
    if scan_json_spans_report is None:
        return [], False
    return scan_json_spans_report(text, budget)


json_objects_in = _objects

SESSION = "i14r8"
REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))))
LEDGER = os.path.join(REPO_ROOT, "issues", "TEST-REGRESSION.md")

#: 判别性用例的卡：静默交接（`fused=True` / `wake=False`）。
#: 卡被丢掉时 `from_handoff` 会判"常规接力" → `should_wake_next()=True`
#: （**放行**方向），故"卡是否可见"就是判别性判据本身。
CARD = json.dumps({"session_id": SESSION, "fused": True, "wake": False,
                   "rounds": {"total": 4}}, sort_keys=True)
CARD_OBJ = json.loads(CARD)

#: N1 的判别性前缀（88 B 正文）：`"\"` × 7 + `"` —— 未闭合引号 + 转义序列，
#: 旧"线性主扫描 + 有界修复"在这里把后续的卡整段丢掉。
N1_PREFIX = '"\\' * 7 + '"'


def _baseline_objects(text: str):
    """**基线 `a7847f9` 的扫描实现**（内联复刻，用于超集召回对照）。

    原样复刻 `_balanced_object_end` + "从左到右取第一个配平对象"：
    这是 `LOST=0` 判据的被比较方，**不是**新实现的一部分。
    """
    out = []
    i = 0
    while i < len(text):
        start = text.find("{", i)
        if start < 0:
            break
        depth = 0
        in_str = False
        escaped = False
        end = None
        for k in range(start, len(text)):
            ch = text[k]
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
                    end = k
                    break
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


def _canon(objs):
    return [json.dumps(o, sort_keys=True, ensure_ascii=False) for o in objs]


def _guard(body: str) -> LoopGuard:
    return LoopGuard.from_handoff(
        SESSION, data_dir=tempfile.mkdtemp(), handoff_context=body)


# ==================================================================
# N1 引号区间前缀 + 卡（88 B）：卡必须可见，且方向**不得** fail-open
# ==================================================================

class TestN1QuotedPrefixKeepsCard:
    def test_card_visible_and_fused(self):
        """判别性用例：88 B 正文 = 未闭合引号/转义前缀 + 静默卡。

        旧实现 `objs=[{'total': 4}]`（只剩卡的内层对象）→ 卡不可见 →
        `fused=False / wake=True`（**单向 fail-open**）。现必须恢复。
        """
        body = N1_PREFIX + CARD
        # 判别性用例规模：总师复核时是"88 B 正文"= 15 B 前缀 + 73 B 卡
        # （卡用 `session_id="S"`）。本文件会话号更长，故断言**前缀长度**（同形）。
        assert len(N1_PREFIX) == 15
        objs = json_objects_in(body)
        assert CARD_OBJ in objs, objs
        g = _guard(body)
        assert g.is_fused() is True
        assert g.should_wake_next() is False

    def test_same_conclusion_both_orders(self):
        """**反向对照（🟡a）**：卡在前/在后两序结论一致（不得顺序相关）。"""
        for body in (N1_PREFIX + CARD, CARD + N1_PREFIX):
            g = _guard(body)
            assert g.is_fused() is True, body
            assert g.should_wake_next() is False

    def test_superset_recall_holds(self):
        """**超集召回**：基线能捞到的对象，新实现一个不落。"""
        body = N1_PREFIX + CARD
        base = _canon(_baseline_objects(body))
        new = _canon(json_objects_in(body))
        assert set(base) <= set(new), (base, new)
        assert base, "基线在该用例上必须能捞到对象（否则用例无判别力）"

    @pytest.mark.parametrize("n", [1, 3, 7, 15, 31])
    def test_prefix_lengths_all_keep_card(self, n):
        """前缀长度参数化：不得只在某一个长度上碰巧通过。"""
        body = '"\\' * n + '"' + CARD
        assert CARD_OBJ in json_objects_in(body)
        assert _guard(body).is_fused() is True

    def test_reverse_control_plain_card_still_awake(self):
        """**反向对照**：只有卡、无前缀 → 卡是静默卡，仍 `fused=True`；
        而 `wake=True` 的卡**不得**被误判静默（防"恒熔断"的恒真断言）。"""
        awake = json.dumps({"session_id": SESSION, "wake": True}, sort_keys=True)
        g = _guard(awake)
        assert g.is_fused() is False
        assert g.should_wake_next() is True


# ==================================================================
# N2 长正文（>60 KB 台账）+ 卡：卡不得被"上界收摊"丢掉
# ==================================================================

class TestN2LongBodyKeepsCard:
    @pytest.fixture
    def ledger(self):
        with open(LEDGER, encoding="utf-8") as f:
            return f.read()

    def test_ledger_is_the_discriminative_size(self, ledger):
        """用例前提：台账正文 > 60 KB（判别性用例的规模条件）。"""
        assert len(ledger) > 60_000

    def test_card_visible_and_fused(self, ledger):
        """`issues/TEST-REGRESSION.md` + 卡：旧实现 `n_objs` 3→1（卡被丢）。"""
        for body in (ledger + "\n" + CARD, CARD + "\n" + ledger):
            objs = json_objects_in(body)
            assert CARD_OBJ in objs, f"卡被丢掉：n_objs={len(objs)}"
            g = _guard(body)
            assert g.is_fused() is True
            assert g.should_wake_next() is False

    def test_superset_recall_on_ledger(self, ledger):
        """**超集召回**（🟡a 反向对照）：台账正文上基线对象全在，且不少于基线数。"""
        body = ledger + "\n" + CARD
        base = _baseline_objects(body)
        new = json_objects_in(body)
        assert set(_canon(base)) <= set(_canon(new))
        assert len(new) >= len(base)
        assert len(base) > 1, "台账里应有多个对象（判别性前提）"

    def test_mechanism_is_not_the_repair_cap(self, ledger):
        """**机制断言**：旧上界（修复段字符量）触顶不再决定结果。

        旧实现把 `MAX_REPAIR_CHARS` 抬高后卡才恢复 → 说明卡是被**上界**丢的。
        新实现里"每个候选独立起算"，故正文规模翻倍仍稳定可见（无上界敏感）。
        """
        for k in (1, 2, 3):
            body = ledger * k + CARD
            assert CARD_OBJ in json_objects_in(body), f"k={k} 丢卡"
            assert not json_objects_in_report(body)[1], f"k={k} 不该降级"

    def test_scan_work_cap_is_generous(self, ledger):
        """上界余量：台账 ×3 的实际工作量必须远低于 `MAX_SCAN_WORK`（否则降级常态化）。"""
        assert MAX_SCAN_WORK is not None
        assert MAX_SCAN_WORK >= 10_000_000
        assert len(ledger) * 3 < MAX_SCAN_WORK


# ==================================================================
# N3 预算合并**真顺序无关**（`None` 视为缺席）
# ==================================================================

class TestN3MergeIsOrderIndependent:
    def test_none_is_absence_not_a_value(self):
        """判别性用例：`[{"x": None}, {"x": 9}]` 两序都必须读成 `9`。

        旧实现 `max(cur, v, key=repr)` 拿 `repr` 比：`'None' > '9'` → 后序读成
        `None`（同一次判定、换顺序换结论）。
        """
        a, b = {"x": None}, {"x": 9}
        assert LoopGuard._merge_budget_values([a, b]) == {"x": 9}
        assert LoopGuard._merge_budget_values([b, a]) == {"x": 9}

    def test_all_none_yields_empty(self):
        """全 `None` → 该字段**不出现**（缺席），不是 `{"x": None}`。"""
        got = LoopGuard._merge_budget_values([{"x": None}, {"x": None}])
        assert "x" not in got

    @pytest.mark.parametrize("order", [
        [{"x": 1}, {"x": None}, {"x": 5}],
        [{"x": None}, {"x": 5}, {"x": 1}],
        [{"x": 5}, {"x": 1}, {"x": None}],
    ])
    def test_permutations_agree(self, order):
        assert LoopGuard._merge_budget_values(order) == {"x": 5}

    def test_random_pairs_are_order_independent(self):
        """随机化：`None`/数字/字符串/怪值混排，两序结论必须一致（真顺序无关）。"""
        random.seed(20260919)
        pool = [None, 0, 9, -3, 2.5, "b", "a", True, [1], {"k": 1}]
        for _ in range(4000):
            k = random.randint(2, 4)
            pairs = [{f"f{i}": random.choice(pool) for i in range(2)}
                     for _ in range(k)]
            fwd = LoopGuard._merge_budget_values(pairs)
            rev = LoopGuard._merge_budget_values(list(reversed(pairs)))
            assert fwd == rev, (pairs, fwd, rev)

    def test_numbers_still_take_max(self):
        """**反向对照**：数字仍取 `max()`（不得为了顺序无关改成"首个胜"）。"""
        assert LoopGuard._merge_budget_values(
            [{"max_rounds": 3}, {"max_rounds": 9}]) == {"max_rounds": 9}
        assert LoopGuard._merge_budget_values(
            [{"max_rounds": 9}, {"max_rounds": 3}]) == {"max_rounds": 9}


# ==================================================================
# 🟡b `budget` 段**只作观测**：不得被采纳成上界（采纳 = 削弱熔断）
# ==================================================================

class TestBudgetSegmentIsObservationOnly:
    def test_external_max_rounds_is_not_adopted(self):
        """判别性用例：载荷 `budget.max_rounds=99` **不得**改会话上界。

        采纳外部 `max_rounds` 是 `max()` **抬高上界 = 削弱熔断**（方向与 N1/N2
        同属 fail-open），且归属字段可缺省（"不表态"即被合并），不可信正文可借此
        放大预算。
        """
        g = LoopGuard(SESSION, data_dir=tempfile.mkdtemp(), max_rounds=3)
        g.adopt_budget({"session_id": SESSION, "rounds": {"total": 1},
                        "budget": {"max_rounds": 99, "max_total_rounds": 99}})
        assert g.max_rounds == 3
        assert g.max_total_rounds == 5
        assert g.status()["max_rounds"] == 3

    def test_rounds_are_still_adopted(self):
        """**反向对照**：`rounds` 仍必须被采纳（只增不减），否则预算不跨容器。"""
        g = LoopGuard(SESSION, data_dir=tempfile.mkdtemp(), max_rounds=3)
        g.adopt_budget({"session_id": SESSION, "rounds": {"total": 2}})
        assert g.status()["round_no"] == 2

    def test_budget_is_observable_but_not_consumed(self):
        """观测面仍可见（留痕/排查用），但**消费路径**只有 `rounds` / `fused`。"""
        merged = LoopGuard._merge_budget_dicts([
            {"budget": {"max_rounds": 3}}, {"budget": {"max_rounds": 9}}])
        assert merged["budget"] == {"max_rounds": 9}      # 观测口径（顺序无关）
        g = LoopGuard(SESSION, data_dir=tempfile.mkdtemp(), max_rounds=3)
        g.adopt_budget(merged)
        assert g.max_rounds == 3                          # 采纳口径：不变

    def test_source_has_no_adoption_path(self):
        """源码护栏：`adopt_budget` 体内**不得**出现对 `budget` 段的消费。

        （"读了不生效 / 声明了不接线"的反面：这里要求"观测字段不接线"，
        故用护栏把口径钉住，避免下一轮又被接上。）
        """
        path = os.path.join(REPO_ROOT, "state-secretary", "src", "secretary",
                            "loop", "loop_guard.py")
        with open(path, encoding="utf-8") as f:
            src = f.read()
        body = src.split("def adopt_budget(", 1)[1].split("def reload(", 1)[0]
        assert 'data.get("budget")' not in body
        assert 'data["budget"]' not in body


# ==================================================================
# 超集召回不变量（差分 fuzz：`LOST=0`）
# ==================================================================

class TestSupersetRecallInvariant:
    """**不变量**：基线 `a7847f9` 能捞到的对象，新实现**必须全捞到**。

    差分对照的"被比较方"用 `_baseline_objects()`（内联复刻），故本回归
    **不依赖**旧提交可检出。`EXTRA`（新实现多捞到）**允许**——那是超集方向，
    不产生丢卡（这正是"构造性不漏"的代价与收益）。
    """

    _FRAGS = ['{"a":1}', '{"wake": false}', '"if (x) {"', '{"a": "b"}', '\\"',
              '{"a":', '{', '}', '"', '\\', '{}', '{"x": [1,2]}',
              '<!-- loop-budget: {"rounds":{"total":2}} -->']
    _ALPHA = ['{', '}', '"', '\\', 'x', ' ', ':', ',', '[', ']', '1',
              'n', 't', 'f', 'a', '\n', '\t', '*', '/', '(']

    def _gen(self, rnd, kind, n):
        if kind == "random":
            return "".join(rnd.choice(self._ALPHA) for _ in range(n))
        if kind == "frag":
            return "".join(rnd.choice(self._FRAGS) for _ in range(n))
        if kind == "quoteish":
            return "".join(rnd.choice(['"', '\\', '{', '}', 'x'])
                           for _ in range(n))
        parts = []
        for _ in range(n):
            r = rnd.random()
            parts.append('{"a":1}' if r < 0.3 else '{"a":' if r < 0.5
                         else '"s"' if r < 0.6 else '1' if r < 0.7
                         else ',' if r < 0.8 else '[' if r < 0.9 else ']')
        return "".join(parts)

    @pytest.mark.parametrize("seed", [20260919, 7, 42])
    def test_fuzz_no_lost_card(self, seed):
        """差分 fuzz：`LOST=0`（且卡在含卡用例里必须可见）。"""
        rnd = random.Random(seed)
        kinds = ["random", "frag", "quoteish", "jsonish"]
        lost = 0
        checked = 0
        for i in range(6000):
            kind = kinds[i % len(kinds)]
            body = (self._gen(rnd, kind, rnd.randint(0, 40)) + CARD
                    + self._gen(rnd, kind, rnd.randint(0, 20)))
            if rnd.random() < 0.5:
                body = self._gen(rnd, kind, rnd.randint(0, 20)) + body
            base = set(_canon(_baseline_objects(body)))
            new = set(_canon(json_objects_in(body)))
            checked += 1
            if base - new:
                lost += 1
        assert checked == 6000
        assert lost == 0, f"超集召回被破坏：{lost} 组丢对象（seed={seed}）"

    def test_card_in_random_prefix_bodies(self):
        """随机前缀 + 卡：卡必须**始终**可见（N1/N2 的同族泛化）。"""
        rnd = random.Random(11)
        for _ in range(2000):
            pfx = self._gen(rnd, "quoteish", rnd.randint(0, 60))
            body = pfx + CARD
            assert CARD_OBJ in json_objects_in(body), repr(pfx)


# ==================================================================
# 上界：只降级、不丢段，且**显式标记**（禁止"静默收摊"）
# ==================================================================

class TestDegradeNeverDropsCardSilently:
    def test_truncation_is_flagged(self):
        """正文超 `DEFAULT_SCAN_BUDGET` → 显式 `degraded=True`（不得静默）。"""
        big = "x" * (DEFAULT_SCAN_BUDGET + 10) + CARD
        objs, degraded = json_objects_in_report(big)
        assert objs == []
        assert degraded is True

    def test_truncation_keeps_fused_direction(self):
        """降级时安全旗标取**保守方向**（不得因"没看到卡"就判放行）。"""
        big = "x" * (DEFAULT_SCAN_BUDGET + 10) + CARD
        g = _guard(big)
        assert g.is_fused() is True
        assert g.should_wake_next() is False

    def test_explicit_budget_scan_flags_too(self):
        """显式给 `budget` 截断时同样标记（口径处处生效）。"""
        body = "y" * 100 + CARD
        _, degraded = _spans_report(body, budget=50)
        assert degraded is True
        _, full = _spans_report(body, budget=None)
        assert full is False

    def test_work_cap_degrades_but_keeps_earlier_spans(self):
        """工作量触顶 → 降级，但**已取到的片段一个不丢**（只降级、不丢段）。"""
        head = '{"keep": 1}' * 5
        # 大量"形态像对象但一路不闭合"的候选，把工作量推过 `MAX_SCAN_WORK`
        filler = '{"a":' * 200000
        objs, degraded = json_objects_in_report(head + filler)
        assert degraded is True
        assert {"keep": 1} in objs, "触顶前已取到的片段被丢了"

    def test_degraded_plus_awake_card_still_conservative(self):
        """**降级 + `wake=True` 卡**：仍按判不出处理（降级不得成为 fail-open 通道）。

        降级意味着"后面那一段没看"，那里**可能还有**一张静默卡 → 只看"找到一张
        `wake=True`"不足以判放行（可能是**更早**的那张）。
        """
        body = "x" * (DEFAULT_SCAN_BUDGET + 10) + json.dumps(
            {"session_id": SESSION, "wake": True})
        g = _guard(body)
        assert g.is_fused() is True
        assert g.should_wake_next() is False

    def test_normal_body_is_not_degraded(self):
        """**反向对照**：正常正文（对象 + 留痕）不降级（防"恒降级"恒真）。"""
        body = '{"a": 1}\n<!-- loop-budget: {"rounds": {"total": 2}} -->\n' \
               '{"wake": false}\nif (x) { y }'
        objs, degraded = json_objects_in_report(body)
        assert degraded is False
        assert objs == [{"a": 1}, {"rounds": {"total": 2}}, {"wake": False}]


# ==================================================================
# 复杂度：构造性"不漏"不得把耗时拖回平方（总师原型口径）
# ==================================================================

class TestComplexityStaysBounded:
    """性能判据取**宽松上界**（1 s）——只为抓"平方级回归"。"""

    def _t(self, text):
        t0 = time.perf_counter()
        got = json_objects_in(text)
        return time.perf_counter() - t0, got

    # 🟡💥 argv 上限：**必须**给显式短 `ids`。默认 id 会把整个 100 KB 参数
    # 拼进 nodeid（最长实测 **140 KB**），而单个 argv 参数受 `MAX_ARG_STRLEN`
    # （Linux 128 KB）限制 → 门禁把计划 nodeid 喂给 `subprocess` 时抛
    # `OSError: [Errno 7] Argument list too long`（`--base e3195ea` 实测必现）。
    # 显式 `ids` 同时让 nodeid **稳定**（不随参数内容漂移）→ 缓存/留痕可比对。
    @pytest.mark.parametrize("text", [
        "{" * 20000, "{" * 40000, "if (x) {" * 3000, '"' * 20000,
        '"{' * 50000, '{"a":' * 20000, '{"a": "' * 20000,
    ], ids=[
        "braces-20k", "braces-40k", "ifx-3k", "quotes-20k",
        "quote-brace-50k", "obj-colon-20k", "obj-colon-str-20k",
    ])
    def test_adversarial_inputs_are_fast(self, text):
        dt, _ = self._t(text)
        assert dt < 1.0, f"len={len(text)} 用了 {dt:.2f}s（平方回归）"

    def test_scales_linearly(self):
        t1, _ = self._t("{" * 10000)
        t2, _ = self._t("{" * 100000)
        assert t2 < max(t1 * 25, 1.0), (t1, t2)

    def test_two_hundred_kb_body(self):
        """200 KB 正文（`DEFAULT_SCAN_BUDGET` 之内）→ 逐段对象全取到、不降级。"""
        body = ("x" * 100 + '{"a":1}') * 2000
        assert len(body) < DEFAULT_SCAN_BUDGET
        dt, objs = self._t(body)
        assert len(objs) == 2000
        assert dt < 1.0, f"200 KB 正文用了 {dt:.2f}s"

    def test_scan_json_spans_is_the_single_entry(self):
        """扫描入口仍**单一实现**（`trail`），`loop_guard` 不再自带一份。"""
        import inspect
        assert scan_json_spans is not None, "基线无 trail 扫描入口"
        from secretary.loop import loop_guard as lg
        src = inspect.getsource(lg)
        assert "_balanced_object_end" not in src
        assert "_stack_scan" not in src
