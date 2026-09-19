"""Issue #21 · `scan_json_spans` 重做（CP1 已批准）—— G1–G7 门禁判别性回归

触发：架构·评审 第 6 次 `blocked` 上报 → 架构·总师裁定（PR #15 @ `9382552`）：
① 确认 `9382552` 属**超范围提交**（scan 重做移出本 PR，由 **Issue #21** 承载）；
② 批准收敛路径 = **`9382552` 的语义 + 两条上界 + `reason` + 三处接线**；
**不批准纯回退**（`a7847f9` 语义 320 KB → 384 s，O(n²)）。

本文件即 Issue #21 的 **G1–G7** 门禁判据（CP1 已由总师 spike 验证，开发直接落地）。

| # | 门禁 | 本文件判据 |
|---|---|---|
| G1 | 语义 = 超集召回（**前提 `degraded=False`**） | 差分 fuzz **42 000**（12 seed × 3 500）：`LOST=0` **且** `REV=0` |
| G2 | ① `MAX_RESCAN_LEN` **显式** | 触界 → `reason="rescan_len"`；**归因 = 冗余兜底**（非"缺一不可"，见 R2） |
| G3 | ② `MAX_SCAN_WORK` **显式**且记账 ≥ 真实扫描量 | 触界 → `reason="work_cap"`；须覆盖**异常构造代价**；**归因 = 承载上界**（见 R2） |
| G4 | 降级契约 `degraded=True` + `reason` | 三态各一条；已配平起点**不得静默丢** |
| G5 | 消费侧接线 **3 处** | `from_handoff` / `adopt_budget` / `probe._coerce_checks_used` |
| **G6** | **含 `}`** 的对抗输入 **< 1 s** | `'{"k":1, '×40000 + '}'`；**禁止**只测无 `}` 输入 |
| **G7** | 记账覆盖异常构造代价 / 每候选限长 | 见 G3/G2 的**机制**断言（非仅结论）；**R5 起改为消融敏感** |
| **R1** | 断言与实现**同源** | 删两条失真断言（超上界必跳过 / ≤ 上界必全捞到），改可复跑判据 + 反例 |
| **R2** | 归因 = ①冗余兜底 / ②承载上界 | 删「缺一不可」，附**消融**（cap→1e9 仍被 ② 拦住 / work→1e18 失控） |
| **R3** | 数字可复跑 | 5 289 / 42 000 / 12 000 / 1.6 MB 标注口径；折算比**只写实测值 + 测量形态**（⚠️ **不是上界**，见 §25.7） |
| **R4** | **降级区间 fuzz** | 三态各 ≥ 3 000 组；判据 = **方向保守**（非"不丢卡"） |
| **R5** | 判别力 | `test_g7_...` 改造为**消融敏感**（记账量差 > 10×） |

**不变量（硬判据 · 前提 `degraded=False`）**：超集召回 —— `a7847f9` 的
`extract_json_objects` 能取到的**长度 ≤ `MAX_RESCAN_LEN`** 的对象，
**在本次扫描未降级时**本实现**必须全取到**（`_baseline_objects()`
内联复刻基线实现，故回归**不依赖**旧提交可检出）。
**降级（`degraded=True`）下允许丢段** —— 这是**已批准语义**，不是缺陷
（反例：14 B 的 `{"wake":false}` 接在 400 000 个 `{}` 之后 → `work_cap` 丢卡，
基线能捞到；见 `TestR1AssertionsMatchImplementation`）。

**边界明示（与实现同源 · 总师裁定 R1）**：判据是**「候选 `p` 到窗口内首个
`}` 的距离 > `MAX_RESCAN_LEN`」**——**不是**"长度超过 `MAX_RESCAN_LEN` 的
合法对象会被跳过"（后者有反例：含内层 `}` 的 **5.2 MB** 合法对象**未被跳过**，
见 `TestR1AssertionsMatchImplementation`）。

**R4 降级 fuzz**：本文件的 G1 fuzz 覆盖不到降级区间（实测 42 000 组里 body
最大 962 B、`degraded` 计数 **0**）——降级区间的判据见
`TestR4DegradedFuzz`（三态各 ≥ 数千组，判据 = **方向保守**，不是"不丢卡"）。
"""
from __future__ import annotations

import inspect
import json
import os
import random
import tempfile
import time

import pytest

from secretary.loop import LoopGuard, extract_json_objects

# ---- 判别性验证用的**取用层**（只为让本文件在**旧基线**上也能收集） ----
try:
    from secretary import trail as trail_mod
except ImportError:                      # pragma: no cover - 仅旧基线
    trail_mod = None

DEFAULT_SCAN_BUDGET = getattr(trail_mod, "DEFAULT_SCAN_BUDGET", 1_000_000)
# 旧基线**没有**这两个新常量：兜底值取 `None`，且**每个用到它们的用例先断言
# "常量存在"**——这样在基线上失败的是**断言**（可读、可计数），而不是
# `TypeError`（那是"跑不起来"，不是"判据失败"）。
MAX_RESCAN_LEN = getattr(trail_mod, "MAX_RESCAN_LEN", None)
MAX_SCAN_WORK = getattr(trail_mod, "MAX_SCAN_WORK", None)
SCAN_REASON_TRUNCATED = getattr(trail_mod, "SCAN_REASON_TRUNCATED", "truncated")
SCAN_REASON_WORK_CAP = getattr(trail_mod, "SCAN_REASON_WORK_CAP", "work_cap")
SCAN_REASON_RESCAN_LEN = getattr(trail_mod, "SCAN_REASON_RESCAN_LEN",
                                 "rescan_len")
scan_json_spans_diag = getattr(trail_mod, "scan_json_spans_diag", None)
json_objects_in_diag = getattr(trail_mod, "json_objects_in_diag", None)
json_objects_in_report = getattr(trail_mod, "json_objects_in_report", None)
extract_json_objects_diag = getattr(
    __import__("secretary.loop", fromlist=["x"]),
    "extract_json_objects_diag", None)

SESSION = "i21"
REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))))
LEDGER = os.path.join(REPO_ROOT, "issues", "TEST-REGRESSION.md")

#: 判别性用例的卡：静默交接（`fused=True` / `wake=False`）。
CARD = json.dumps({"session_id": SESSION, "fused": True, "wake": False,
                   "rounds": {"total": 4}}, sort_keys=True)
CARD_OBJ = json.loads(CARD)
#: 常规接力卡（`wake=True`）——**反向对照**用（不得被误判静默）。
AWAKE_CARD = json.dumps({"session_id": SESSION, "wake": True}, sort_keys=True)


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


def _objects_diag(text, budget=None):
    """``(对象清单, degraded, reason)``；旧基线无 diag → 兜底 ``(…, False, "")``。"""
    if json_objects_in_diag is None:
        return list(extract_json_objects(text)), False, ""
    return json_objects_in_diag(text, budget)


def _spans_diag(text, budget=None):
    if scan_json_spans_diag is None:
        return [], False, ""
    return scan_json_spans_diag(text, budget)


#: G8 判据用：**旧二元组**入口（`(片段清单, degraded)`）。
scan_json_spans_report = getattr(trail_mod, "scan_json_spans_report", None)


def _scan_diag_probe(text, limit=None):
    """内部四元组（含 `work`）：判"记账余量"用；旧基线无 → 断言失败。

    `limit` 缺省 = `len(text)`（全量）；显式给 `limit` 用于"`budget` 是扫描边界"
    的定点判据（🔴-1）。
    """
    fn = getattr(trail_mod, "_scan_diag", None)
    assert fn is not None, "缺 trail._scan_diag（G3/G8 硬要求）"
    return fn(text, len(text) if limit is None else limit)


def _need(name, value):
    """取新常量：缺失（旧基线）→ **断言失败**（而非 TypeError）。"""
    assert value is not None, f"缺 {name}（Issue #21 硬要求）"
    return value


def _guard(body: str) -> LoopGuard:
    return LoopGuard.from_handoff(
        SESSION, data_dir=tempfile.mkdtemp(), handoff_context=body)


# ==================================================================
# G6 含 `}` 的对抗输入 < 1 s（**禁止**只测无 `}` 输入）
# ==================================================================

class TestG6AdversarialWithCloseBraceIsFast:
    """`'{"k":1, '×40000 + '}'` —— 每个候选都要扫到**文末那个 `}`**。

    这是上一轮"只给总上界不给每候选上界"的**判别性输入**：实测 320 KB → 2.26 s、
    1 MB → 21.7 s，且 `degraded` 恒 `False`（记账口径 `pos - p` 恒 8 ⇒ 永不触顶）。
    """

    def _t(self, text):
        t0 = time.perf_counter()
        got = extract_json_objects(text)
        return time.perf_counter() - t0, got

    @pytest.mark.parametrize("n,limit_s", [(40000, 1.0), (125000, 1.0)])
    def test_g6_320kb_and_1mb_under_one_second(self, n, limit_s):
        text = '{"k":1, ' * n + '}'
        dt, got = self._t(text)
        assert dt < limit_s, f"len={len(text)} 用了 {dt:.2f}s（> {limit_s}s）"

    def test_g6_scales_linearly_not_quadratically(self):
        """规模 ×3 → 耗时不得到平方级（2.26 s → 21.7 s 是 ×9.6）。"""
        t1, _ = self._t('{"k":1, ' * 40000 + '}')
        t2, _ = self._t('{"k":1, ' * 125000 + '}')
        assert t2 < max(t1 * 6, 1.0), (t1, t2)

    def test_g6_degraded_is_reported_not_silent(self):
        """**禁止静默**：该输入必须 `degraded=True` + 可读 `reason`。

        上一轮该输入的 `degraded` 恒 `False`（"声明了不生效"），故"跑得快"
        必须与"显式降级"同时成立——否则下一轮无从判断"没表态的区间在哪"。
        两个上界都会在这类输入上生效（每候选限长 + 总量触顶），`reason`
        按优先级取**最外层**的一个（确定性，与出现顺序无关）。
        """
        text = '{"k":1, ' * 40000 + '}'
        objs, degraded, reason = _objects_diag(text)
        assert degraded is True
        assert reason in (SCAN_REASON_RESCAN_LEN, SCAN_REASON_WORK_CAP)

    def test_g6_card_after_adversarial_prefix_still_conservative(self):
        """对抗前缀 + 静默卡：**不得** fail-open（降级 → 保守静默）。"""
        body = '{"k":1, ' * 40000 + '}' + CARD
        g = _guard(body)
        assert g.is_fused() is True
        assert g.should_wake_next() is False

    def test_g6_under_one_second_end_to_end(self):
        """端到端（`from_handoff`）同样 < 1 s（总师实测旧实现 4.54 s）。"""
        body = '{"k":1, ' * 40000 + '}' + CARD
        t0 = time.perf_counter()
        _guard(body)
        assert time.perf_counter() - t0 < 1.0


# ==================================================================
# G2 ① `MAX_RESCAN_LEN` **显式**（每候选上界）
# ==================================================================

class TestG2PerCandidateBoundIsExplicit:
    def test_g2_constant_exists_and_is_explicit(self):
        """① 每候选上界必须**单独显式**（不可用总上界顶替）。"""
        assert MAX_RESCAN_LEN is not None, "缺 MAX_RESCAN_LEN（G2 硬要求）"
        assert isinstance(MAX_RESCAN_LEN, int) and MAX_RESCAN_LEN > 0

    def test_g2_is_not_derived_from_total_work_cap(self):
        """两条上界**不同源**：每候选上界不是总上界的函数（防"用总顶替"）。"""
        cap = _need("MAX_RESCAN_LEN", MAX_RESCAN_LEN)
        total = _need("MAX_SCAN_WORK", MAX_SCAN_WORK)
        assert cap != total
        assert cap < total

    def test_g2_boundary_triggers_rescan_len_reason(self):
        """触界 → `reason="rescan_len"`（**判别性**：改大到不触界即不降级）。"""
        cap = _need("MAX_RESCAN_LEN", MAX_RESCAN_LEN)
        body = json.dumps({"pad": "y" * (cap + 1000)})
        objs, degraded, reason = _objects_diag(body)
        assert degraded is True
        assert reason == SCAN_REASON_RESCAN_LEN

    def test_g2_reverse_under_bound_is_not_degraded(self):
        """**反向对照**：对象长度**在上界之内** → 正常取到、不降级。"""
        cap = _need("MAX_RESCAN_LEN", MAX_RESCAN_LEN)
        body = json.dumps({"pad": "y" * (cap // 4)})
        objs, degraded, reason = _objects_diag(body)
        assert degraded is False
        assert objs == [json.loads(body)]

    def test_g2_calibration_has_measured_margin(self):
        """取值依据（实测）：本仓库**全部语料**里"候选 → 最近 `}` 距离"的最大值
        必须远小于上界（否则真实正文会常态化降级）。"""
        import glob
        corpus = []
        for pat in ("issues/*.md", "docs/**/*.md",
                    "state-secretary/src/**/*.py", "state-secretary/tests/*.py"):
            for f in glob.glob(os.path.join(REPO_ROOT, pat), recursive=True):
                try:
                    with open(f, encoding="utf-8") as fh:
                        corpus.append(fh.read())
                except OSError:
                    continue
        assert corpus, "语料为空（判据无意义）"
        worst = 0
        for text in corpus:
            last_close = text.rfind("}")
            i = 0
            while True:
                p = text.find("{", i)
                if p < 0 or p > last_close:
                    break
                close = text.find("}", p)
                worst = max(worst, (close + 1 - p) if close >= 0
                            else (len(text) - p))
                i = p + 1
        cap = _need("MAX_RESCAN_LEN", MAX_RESCAN_LEN)
        assert worst < cap, (worst, cap)
        assert cap >= worst * 8, (worst, cap)

    def test_g2_long_legal_object_is_skipped_but_flagged(self):
        """**边界明示（同源判据 · 总师裁定 R1）**：判据是
        **「候选 `p` 到窗口内首个 `}` 的距离 > `MAX_RESCAN_LEN`」**。

        ⚠️ 旧写法（"长度超上界的合法对象会被跳过"）**与实现不同源**，已被
        反例推翻 —— 见 `TestR1AssertionsMatchImplementation::
        test_r1a_long_object_with_inner_close_brace_is_not_skipped`。
        本用例的输入**恰好**满足同源判据（单个候选、`}` 在 cap+10 处）。
        """
        cap = _need("MAX_RESCAN_LEN", MAX_RESCAN_LEN)
        obj = json.dumps({"pad": "z" * (cap + 10)})
        # 前提断言：该输入确实满足**同源**判据（距离 > cap）
        assert obj.index("}", 1) + 1 > cap
        objs, degraded, reason = _objects_diag(obj)
        assert objs == []                  # 该候选不表态（跳过）
        assert degraded is True            # 但**显式**（可观测、可复核）
        assert reason == SCAN_REASON_RESCAN_LEN


# ==================================================================
# G3 ② `MAX_SCAN_WORK` **显式**且记账 ≥ 真实扫描量（含异常构造代价）
# ==================================================================

class TestG3WorkAccountingCoversRealCost:
    def test_g3_constant_exists_and_is_explicit(self):
        assert MAX_SCAN_WORK is not None, "缺 MAX_SCAN_WORK（G3 硬要求）"
        assert isinstance(MAX_SCAN_WORK, int) and MAX_SCAN_WORK > 0

    def test_g3_failure_charge_is_window_length_not_pos_minus_p(self):
        """**机制断言（G7 · 本轮 🔴-1/🔴-2 随修法改口径）**：喂入量**相加不取 max**。

        修法前口径 = 错误位置 `pos`（`pos - p` 恒 8 ⇒ 永不触顶）；上一轮改为
        `max(短切片长度, 实际喂入)`（**漏记**短切片那次喂入，实测少记
        20 682 304 / 10.93 %，方向 fail-open）。本轮口径（🔴-1）：
        **各次喂入相加** —— A 组未命中（普通语法错）只喂短切片 ⇒ `fed = short_fed`；
        A 组命中（扩窗）两次都喂 ⇒ `fed = short_fed + window_fed`。

        源码护栏把**新**口径钉住（旧 `pos - p` / `max(...)` 仍不得出现）。
        """
        path = os.path.join(REPO_ROOT, "state-secretary", "src", "secretary",
                            "trail.py")
        with open(path, encoding="utf-8") as f:
            src = f.read()
        body = src.split("def _scan_diag(", 1)[1]
        code = "\n".join(ln for ln in body.splitlines()
                         if not ln.lstrip().startswith("#"))
        # 短切片喂入量（含 `}`）+ 窗口喂入量（含窗口末字符）
        assert "short_fed = close + 1 - p" in code, \
            "短切片喂入量口径缺失（应含 `}`：`close + 1 - p`）"
        assert "window_fed = window_end - p" in code, \
            "窗口喂入量口径缺失（应含窗口末字符：`window_end - p`）"
        assert "fed = short_fed" in code, \
            "A 组未命中（普通语法错）未按『只喂短切片』记账"
        assert "fed = short_fed + window_fed" in code, \
            "扩窗时两次喂入未**相加**（🔴-1：不得取 max）"
        assert "pos - p" not in code, "失败记账回退到 pos - p（永不触顶）"
        assert "pos if" not in code, "失败记账仍取 pos（漏实扫量，记账脱钩）"
        assert "if rel > fed" not in code, "喂入量仍取 max（漏记短切片那次喂入）"

    def test_g3_work_cap_reports_reason(self):
        """触顶 → `reason="work_cap"`（可观测；不得静默）。"""
        body = '{"k":1, ' * 40000 + '}'
        objs, degraded, reason = _objects_diag(body)
        assert degraded is True
        assert reason in (SCAN_REASON_WORK_CAP, SCAN_REASON_RESCAN_LEN)

    def test_g3_earlier_spans_survive_the_cap(self):
        """**只降级、不丢段**：触顶时**已取到的片段一个不丢**。"""
        head = '{"keep": 1}'
        filler = "{}" * 400000            # 密集微对象 → 折算量快速触顶
        objs, degraded, reason = _objects_diag(head + filler)
        assert degraded is True
        assert {"keep": 1} in objs, "触顶前已取到的片段被丢了"
        assert reason == SCAN_REASON_WORK_CAP

    def test_g3_candidate_cost_is_charged(self):
        """**机制断言（G7）**：每候选固定开销**计入**记账。

        只按字符量记账时，"大量微对象"形态（`"{}"×N`：字符量小、耗时大）
        会记成很小的量 ⇒ 记账**不覆盖真实耗时**。故 `_SCAN_CANDIDATE_COST`
        必须存在且被计入（源码护栏 + 行为对照）。
        """
        path = os.path.join(REPO_ROOT, "state-secretary", "src", "secretary",
                            "trail.py")
        with open(path, encoding="utf-8") as f:
            src = f.read()
        assert "_SCAN_CANDIDATE_COST" in src
        # 行为对照：微对象密集正文的耗时被"记账口径"覆盖（触顶即停）
        t0 = time.perf_counter()
        objs, degraded, _ = _objects_diag("{}" * 500000)
        assert time.perf_counter() - t0 < 2.0
        assert degraded is True

    def test_g3_real_corpus_is_not_degraded(self):
        """**反向对照**：真实台账正文（×4，334 KB）**不降级**（余量充分）。"""
        with open(LEDGER, encoding="utf-8") as f:
            ledger = f.read()
        body = ledger * 4 + "\n" + CARD
        objs, degraded, reason = _objects_diag(body)
        assert degraded is False, reason
        assert CARD_OBJ in objs

    def test_g3_recursion_error_is_charged_by_remaining_length(self):
        """`RecursionError`（无 `pos`）按**剩余长度上限**计（记账 ≥ 真实）。"""
        deep = "{" + '{"a":{' * 3000 + "}" * 3000
        t0 = time.perf_counter()
        _objects_diag(deep)
        assert time.perf_counter() - t0 < 1.0


# ==================================================================
# G4 降级契约：`degraded=True` + `reason` 三态（不得静默丢）
# ==================================================================

class TestG4DegradeContractThreeReasons:
    def test_g4_truncated_reason(self):
        body = "x" * (DEFAULT_SCAN_BUDGET + 10) + CARD
        objs, degraded, reason = _objects_diag(body)
        assert degraded is True
        assert reason == SCAN_REASON_TRUNCATED

    def test_g4_rescan_len_reason(self):
        cap = _need("MAX_RESCAN_LEN", MAX_RESCAN_LEN)
        body = json.dumps({"pad": "y" * (cap + 1000)})
        _, degraded, reason = _objects_diag(body)
        assert degraded is True and reason == SCAN_REASON_RESCAN_LEN

    def test_g4_work_cap_reason(self):
        body = '{"k":1, ' * 40000 + '}'
        _, degraded, reason = _objects_diag(body)
        assert degraded is True
        assert reason in (SCAN_REASON_WORK_CAP, SCAN_REASON_RESCAN_LEN)

    def test_g4_normal_body_reports_no_reason(self):
        """**反向对照**：正常正文不降级、`reason` 为空（防"恒降级"恒真）。"""
        body = ('{"a": 1}\n<!-- loop-budget: {"rounds": {"total": 2}} -->\n'
                '{"wake": false}\nif (x) { y }')
        objs, degraded, reason = _objects_diag(body)
        assert degraded is False and reason == ""
        assert objs == [{"a": 1}, {"rounds": {"total": 2}}, {"wake": False}]

    def test_g4_already_parsed_spans_are_never_dropped(self):
        """**已配平起点不得静默丢**：降级只影响"未表态区间"。"""
        cap = _need("MAX_RESCAN_LEN", MAX_RESCAN_LEN)
        head = '{"keep": 1} {"also": 2}'
        body = head + json.dumps({"pad": "y" * (cap + 1000)})
        objs, degraded, reason = _objects_diag(body)
        assert {"keep": 1} in objs and {"also": 2} in objs
        assert degraded is True and reason == SCAN_REASON_RESCAN_LEN

    def test_g4_truncated_card_is_conservative(self):
        """截断时静默卡在未表态区间 → 保守静默（不得 fail-open）。"""
        g = _guard("x" * (DEFAULT_SCAN_BUDGET + 10) + CARD)
        assert g.is_fused() is True
        assert g.should_wake_next() is False

    def test_g4_truncated_awake_card_is_still_conservative(self):
        """截断 + `wake=True` 卡 → 仍保守（未表态区间可能还有静默卡）。"""
        g = _guard("x" * (DEFAULT_SCAN_BUDGET + 10) + AWAKE_CARD)
        assert g.is_fused() is True
        assert g.should_wake_next() is False

    def test_g4_reason_is_deterministic(self):
        """`reason` 与出现顺序无关（多个原因同时成立 → 取最外层，确定性）。"""
        body = '{"k":1, ' * 40000 + '}'
        assert {_objects_diag(body)[2] for _ in range(5)} == {
            _objects_diag(body)[2]}


# ==================================================================
# G5 消费侧接线 **3 处**
# ==================================================================

class TestG5ConsumersAreWired:
    def test_g5_from_handoff_uses_degrade_reason(self):
        """① `from_handoff`：降级 → 保守方向，且 `fused_reason` **带原因**。"""
        for body, expect in (
            ("x" * (DEFAULT_SCAN_BUDGET + 10) + CARD, SCAN_REASON_TRUNCATED),
            ('{"k":1, ' * 40000 + '}' + CARD, None),
        ):
            g = _guard(body)
            assert g.is_fused() is True
            assert g.should_wake_next() is False
            reason = g.status()["fused_reason"]
            assert "扫描降级" in reason
            if expect:
                assert expect in reason

    def test_g5_from_handoff_source_uses_diag_entry(self):
        """源码护栏：`from_handoff` 必须走 `extract_json_objects_diag`（带原因）。"""
        path = os.path.join(REPO_ROOT, "state-secretary", "src", "secretary",
                            "loop", "loop_guard.py")
        with open(path, encoding="utf-8") as f:
            src = f.read()
        body = src.split("def from_handoff(", 1)[1].split("    def ", 1)[0]
        assert "extract_json_objects_diag" in body

    def test_g5_adopt_budget_degraded_body_does_not_silently_drop_card(self):
        """② `adopt_budget`：降级正文**不得**变成"无操作"（卡静默丢）。"""
        g = LoopGuard(SESSION, data_dir=tempfile.mkdtemp())
        res = g.adopt_budget("x" * (DEFAULT_SCAN_BUDGET + 10) + CARD)
        assert res["merged"] is True, "降级被当成空操作（fail-open 方向）"
        assert g.is_fused() is True
        assert g.should_wake_next() is False

    def test_g5_adopt_budget_reverse_normal_payload_unchanged(self):
        """**反向对照**：正常载荷仍按 `max()` 合并（改动不误伤正常路径）。"""
        g = LoopGuard(SESSION, data_dir=tempfile.mkdtemp())
        res = g.adopt_budget(CARD)
        assert res["merged"] is True
        assert g.status()["round_no"] == 4

    def test_g5_probe_coerce_used_reports_degrade(self):
        """③ `probe._coerce_checks_used`：降级必须**上报**（不再静默判不出）。"""
        from secretary.collab.probe import CheckpointProbe
        trail = ("<!-- probe-budget: "
                 + json.dumps({"checkpoint_key": "cp", "checks_used": 6,
                               "max_checks": 6})
                 + " -->")
        diag = getattr(CheckpointProbe, "_coerce_checks_used_diag", None)
        assert diag is not None, "缺 _coerce_checks_used_diag（G5 硬要求）"
        body = "x" * (DEFAULT_SCAN_BUDGET + 10) + trail
        got, degraded, reason = diag(body, 0, "cp")
        assert degraded is True
        assert reason == SCAN_REASON_TRUNCATED
        # 降级时**不得**只判"取不到"（那会让上界被稀释 → 永不触发）
        assert got is None or got >= 0

    def test_g5_probe_adopt_budget_degrades_to_ceiling(self):
        """降级 → 保守按**上界**继承（不再自动挂账），并留痕原因。"""
        from secretary.collab.probe import CheckpointProbe
        p = CheckpointProbe("P21", data_dir=tempfile.mkdtemp())
        cp = p.create("cp21", kind="event", max_checks=6)
        trail = ("<!-- probe-budget: "
                 + json.dumps({"checkpoint_key": "cp21", "checks_used": 1,
                               "max_checks": 6})
                 + " -->")
        res = p.adopt_checks_budget(
            cp, "x" * (DEFAULT_SCAN_BUDGET + 10) + trail)
        assert res["degraded"] is True
        assert res["checks_used"] == 6, "降级未取保守方向（上界被稀释）"
        out = p.probe("cp21", event_fired=False)
        assert out.event.resume_required is True

    def test_g5_probe_reverse_normal_payload(self):
        """**反向对照**：正常留痕载荷仍按原口径合并（只增不减）。"""
        from secretary.collab.probe import CheckpointProbe
        p = CheckpointProbe("P21b", data_dir=tempfile.mkdtemp())
        cp = p.create("cp21b", kind="event", max_checks=6)
        trail = ("<!-- probe-budget: "
                 + json.dumps({"checkpoint_key": "cp21b", "checks_used": 3,
                               "max_checks": 6})
                 + " -->")
        res = p.adopt_checks_budget(cp, trail)
        assert res["merged"] is True and res["checks_used"] == 3
        assert res["degraded"] is False


# ==================================================================
# G1 语义 = 超集召回（差分 fuzz：`LOST=0` **且** `REV=0`）
# ==================================================================

class TestG1SupersetRecallInvariant:
    """不变量：基线能捞到的对象（长度 ≤ 上界）新实现**必须全捞到**。

    `EXTRA`（新实现多捞到）**允许** —— 那是超集方向，不产生丢卡。
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
            parts.append('{"a":1}' if r < 0.3 else '{"k":1, ' if r < 0.5
                         else '{' if r < 0.7 else '}' if r < 0.85 else '[1]')
        return "".join(parts)

    @pytest.mark.parametrize("seed", [20260919, 7, 42, 1, 2, 3, 5, 11, 13,
                                      17, 23, 29])
    def test_g1_no_lost_objects(self, seed):
        """12 seed × 3500 组 = 42 000 组差分 fuzz：`LOST=0`。"""
        rnd = random.Random(seed)
        kinds = ["random", "frag", "quoteish", "jsonish"]
        lost = 0
        for i in range(3500):
            kind = kinds[i % 4]
            body = (self._gen(rnd, kind, rnd.randint(0, 40)) + CARD
                    + self._gen(rnd, kind, rnd.randint(0, 20)))
            if rnd.random() < 0.5:
                body = self._gen(rnd, kind, rnd.randint(0, 20)) + body
            base = set(_canon(_baseline_objects(body)))
            new = set(_canon(_objects_diag(body)[0]))
            if base - new:
                lost += 1
        assert lost == 0, f"超集召回被破坏：{lost} 组丢对象（seed={seed}）"

    @pytest.mark.parametrize("seed", [20260919, 7, 42, 1, 2, 3])
    def test_g1_no_release_direction_flip(self, seed):
        """`REV`（放行方向翻转）= 0：基线熔断而新实现放行 **不得**出现。"""
        rnd = random.Random(seed)
        kinds = ["random", "frag", "quoteish", "jsonish"]
        rev = 0
        for i in range(2000):
            kind = kinds[i % 4]
            body = (self._gen(rnd, kind, rnd.randint(0, 40)) + CARD
                    + self._gen(rnd, kind, rnd.randint(0, 20)))
            new_wake = _guard(body).should_wake_next()
            if new_wake:
                # 基线判定（同一判据、同一卡）
                import secretary.loop.loop_guard as lg
                orig = getattr(lg, "extract_json_objects_diag", None)
                if orig is None:
                    # 旧基线：无 diag 入口 → 该判据在基线上**不成立**（记为翻转）
                    rev += 1
                    continue
                lg.extract_json_objects_diag = (
                    lambda t, b=None: (_baseline_objects(t), False, ""))
                try:
                    base_wake = _guard(body).should_wake_next()
                finally:
                    lg.extract_json_objects_diag = orig
                if not base_wake:
                    rev += 1
        assert rev == 0, f"放行方向翻转 {rev} 次（seed={seed}）"

    def test_g1_card_visible_in_random_prefix_bodies(self):
        """随机前缀 + 卡：卡必须**始终**可见（N1/N2 同族泛化）。"""
        rnd = random.Random(11)
        for _ in range(2000):
            pfx = self._gen(rnd, "quoteish", rnd.randint(0, 60))
            body = pfx + CARD
            assert CARD_OBJ in _objects_diag(body)[0], repr(pfx)

    def test_g1_superset_allows_extra(self):
        """**超集方向**：多捞到（`EXTRA`）允许；少捞到（`LOST`）不允许。"""
        body = '{"a": 1} {"b": 2}'
        base = set(_canon(_baseline_objects(body)))
        new = set(_canon(_objects_diag(body)[0]))
        assert base <= new

    def test_g1_ledger_card_visible_and_fused(self):
        """台账正文 + 卡（>60 KB 判别性用例）：卡可见、方向保守。"""
        with open(LEDGER, encoding="utf-8") as f:
            ledger = f.read()
        for body in (ledger + "\n" + CARD, CARD + "\n" + ledger):
            assert CARD_OBJ in _objects_diag(body)[0]
            g = _guard(body)
            assert g.is_fused() is True
            assert g.should_wake_next() is False


# ==================================================================
# G7 记账覆盖异常构造代价 / 每候选限长（机制层，非仅结论）
# ==================================================================

class TestG7AccountingMechanism:
    def test_g7_single_entry_single_implementation(self):
        """扫描入口仍**单一实现**（`trail`），`loop_guard` 不自带一份。"""
        from secretary.loop import loop_guard as lg
        src = inspect.getsource(lg)
        assert "_balanced_object_end" not in src
        assert "_stack_scan" not in src
        assert "scan_json_spans" in src or "json_objects_in" in src

    def test_g7_reason_three_states_are_distinct(self):
        """三态取值互不相同（可判别，不得同值冒充）。"""
        assert len({SCAN_REASON_TRUNCATED, SCAN_REASON_WORK_CAP,
                    SCAN_REASON_RESCAN_LEN}) == 3

    def test_g7_accounting_covers_measured_worst_case(self):
        """**R5 改造**：原判据（"最坏耗时 < 2 s"）**恒真、无判别力**（实测：
        原样 0.41 s / cap→1e9 0.40 s / candcost→0 1.07 s，三者均 < 2.0）。

        现改为**消融敏感**：绑 `_SCAN_CANDIDATE_COST` 计入/不计入的**记账量差**
        （确定性、不受机器速度影响），见
        `TestR5DiscriminatingPower::test_r5_candidate_cost_is_bound_into_accounting`。
        本用例保留"耗时 < 2 s"作为**辅助**判据，并给出可复跑**折算比**
        （测量形态：`"{}"×500 000`，`dt / work`）。
        ⚠️ **折算比仍是实测值，不写成上界**：修法（解析只喂窗口）后，未闭合
        字符串 + 巨尾形态的**记账不再与实扫脱钩**（5/50/200 MB → dt 恒定
        0.0003 s、work 恒 1 263 177），但折算比仍**因形态而异**（实测微对象
        密集 2.87 ns/单位、未闭合巨尾 0.23–0.28 ns/单位），故只作观测不作证明。
        """
        worst_dt = 0.0
        for body in ("{}" * 500000, '{"k":1, ' * 40000 + '}',
                     "{" * 999000 + "}", "if (x) {" * 50000 + "}"):
            t0 = time.perf_counter()
            _objects_diag(body)
            worst_dt = max(worst_dt, time.perf_counter() - t0)
        assert worst_dt < 2.0, f"最坏对抗输入耗时 {worst_dt:.2f}s"

    def test_g7_per_candidate_bound_limits_single_scan(self):
        """每候选限长**真的**生效：单候选扫描区间不超过 `MAX_RESCAN_LEN`。

        机制判据：正文长度远超上界、但 `}` 在极远处 → 该候选被跳过
        （`rescan_len`），而不是"扫到文末"（那正是 O(n²) 的来源）。
        """
        # 候选必须过 O(1) 预检（`{` 后跳过空白是 `"` 或 `}`）才进"每候选限长"，
        # 故用 `'{"a": '` 起头（形态像对象）+ 超长正文 + 远端 `}`。
        cap = _need("MAX_RESCAN_LEN", MAX_RESCAN_LEN)
        body = '{"a": ' + "x" * (cap * 3) + "}"
        _, degraded, reason = _objects_diag(body)
        assert degraded is True
        assert reason == SCAN_REASON_RESCAN_LEN

    def test_g7_docstring_assertion_matches_implementation(self):
        """**同源断言**：docstring 不得再宣称"记账 ≥ 真实扫描量故同时是耗时上界"
        而未说明**每候选上界**（总师点名：此类失真断言下一轮仍会误信）。"""
        path = os.path.join(REPO_ROOT, "state-secretary", "src", "secretary",
                            "trail.py")
        with open(path, encoding="utf-8") as f:
            src = f.read()
        assert "MAX_RESCAN_LEN" in src
        assert "SCAN_REASON_RESCAN_LEN" in src
        # 记账口径的说明必须点明**修法后的三条**（解析只喂窗口 / 失败按窗口长度 /
        # 记账含每候选固定开销）——旧口径"错误位置 pos（整段前缀）"已被证伪
        # （记账与实扫脱钩），不得再作为记账口径出现。
        assert "只喂窗口" in src or "只在该窗口上" in src
        # 本轮 🔴-1/🔴-2 口径：**各次喂入相加**（`short_fed` / `window_fed`）
        assert "short_fed" in src and "window_fed" in src
        assert "相加" in src, "未写明『各次喂入相加』（🔴-1 口径）"
        assert "_SCAN_CANDIDATE_COST" in src
        assert "记账脱钩" in src, "未记录被证伪的旧口径（下一轮会误信）"


# ==================================================================
# G8 `budget <= 0` 不得绕过降级规则（总师 Issue #21 评论
#    `2101068933304893440` @ 2026-09-18T22:00:44Z 新增验收项）
# ==================================================================

class TestG8ZeroBudgetIsTheSameRuleAsTruncation:
    """**G8**：`budget <= 0` 与截断是**同一条规则**，不得"提前返回、静默不标记"。

    缺陷（总师独立复现）：旧实现在 ``scan_json_spans_diag`` 里
    ``if limit <= 0: return out, False``，**位于** ``degraded = limit < len(text)``
    **之前** ⇒ 非空正文被整段截断却**不标记**（实测 `([], False)`）。

    修法（本文件判据）：该分支**与截断同规则**（`limit=0 < len(text)` 本应为 `True`），
    属"提前返回绕过已定规则"，**不是**新语义 ⇒ 现在 `limit <= 0` **不再自己判定**，
    一律交给 ``_scan_diag`` 的唯一那条规则。

    归属：与 **G4（降级契约）同批**落地（总师明示"不得单独"）。
    """

    def test_g8_zero_budget_on_nonempty_body_is_degraded(self):
        """判别性用例（总师原文口径）：`scan_json_spans_report(text, 0)` → `degraded=True`。"""
        text = '{"a":1}'
        spans, degraded = scan_json_spans_report(text, 0)
        assert spans == []
        assert degraded is True, "非空正文 + budget=0 被静默成未降级（G8）"

    def test_g8_negative_budget_on_nonempty_body_is_degraded(self):
        """判别性用例：`json_objects_in_report(text, -5)` → `degraded=True`。"""
        objs, degraded = json_objects_in_report('{"a":1}', -5)
        assert objs == []
        assert degraded is True, "非空正文 + budget<0 被静默成未降级（G8）"

    def test_g8_reason_is_truncated_same_as_rule(self):
        """`reason` 必须与**截断**同一条：`truncated`（不是新语义、不是空串）。"""
        for budget in (0, -1, -5, -1000):
            _, degraded, reason = _spans_diag('{"a":1}', budget)
            assert degraded is True
            assert reason == SCAN_REASON_TRUNCATED, (budget, reason)

    def test_g8_same_rule_as_truncation_is_the_shared_source(self):
        """**机制断言（单一语义源）**：`budget=0` 与"正文超 budget"走**同一判定处**。

        判据 = 两种输入的 `(degraded, reason)` 完全一致；且 `trail.py` 里
        `scan_json_spans_diag` **不再**有"`limit <= 0` 提前 return"的绕过分支
        （源码护栏：该函数体内不得出现 `limit <= 0` / `limit < 1` 的早退）。
        """
        small = '{"a":1}'
        big = "x" * 100 + small
        a = _spans_diag(small, 0)
        b = _spans_diag(big, 50)          # 截断：limit=50 < len(text)
        assert a[1:] == b[1:] == (True, SCAN_REASON_TRUNCATED), (a, b)
        path = os.path.join(REPO_ROOT, "state-secretary", "src", "secretary",
                            "trail.py")
        with open(path, encoding="utf-8") as f:
            src = f.read()
        body = src.split("def scan_json_spans_diag(", 1)[1].split(
            "\ndef ", 1)[0]
        code = "\n".join(ln for ln in body.splitlines()
                         if not ln.lstrip().startswith("#"))
        assert "limit <= 0" not in code, "仍有绕过已定规则的提前返回（G8）"
        assert "limit < 1" not in code
        # 仍必须把 `limit` 交给唯一判定处
        assert "_scan_diag(text, limit)" in code

    def test_g8_empty_body_is_not_degraded(self):
        """**反向对照**：空正文**不**降级（没有内容可表态，防"恒降级"恒真）。"""
        for budget in (None, 0, -5, 10):
            assert _spans_diag("", budget) == ([], False, "")
            assert _objects_diag("", budget) == ([], False, "")

    def test_g8_reverse_normal_budget_unchanged(self):
        """**反向对照**：`budget > 0` 且不截断 → 结果与不传 `budget` 一致。"""
        text = '{"a":1} {"b":2}'
        assert _spans_diag(text, len(text)) == _spans_diag(text)
        assert _spans_diag(text, 10_000) == _spans_diag(text)

    def test_g8_end_to_end_conservative_on_zero_budget(self):
        """端到端：`budget=0` 的降级不得成为 fail-open 通道（卡在未表态区间）。"""
        from secretary.loop import extract_json_objects_diag
        assert extract_json_objects_diag is not None, "缺 diag 入口（G4/G5）"
        objs, degraded, reason = extract_json_objects_diag(CARD, 0)
        assert objs == [] and degraded is True
        assert reason == SCAN_REASON_TRUNCATED


# ==================================================================
# ≥ 417 K 正文**反误降级**对照（总师：只修记账 = 新 fail-open，
#   触发于 417 K **非对抗**正文；故必须有"不误降级"的对照）
# ==================================================================

class TestReverseControlRealBodyBeyond417K:
    """**反向对照**：真实台账正文 **≥ 417 K** 不得误降级。

    为什么必须有这条（总师 `[HIGH]`）：`cost = pos` 单独落地时，台账 ×5
    （**417 480 字符**）即触旧上界、卡被吞 —— 且落在**非对抗正文**上，
    于是"修记账"反而把 CP1 变成**新的 fail-open 通道**（`adopt_budget` →
    `wake=True`）。故判据不只要"对抗输入 < 1 s"，还要"**真实正文不降级**"。

    本条用**实读台账 ×N**（非构造串）作为"真实正文"，并给出规模阶梯。
    """

    @staticmethod
    def _ledger():
        with open(LEDGER, encoding="utf-8") as f:
            return f.read()

    def test_ledger_size_meets_the_417k_threshold(self):
        """用例前提：台账 ×5 **≥ 417 480 字符**（总师实测的误降级触发规模）。"""
        led = self._ledger()
        assert len(led) * 5 >= 417_480, (len(led), len(led) * 5)

    #: 规模阶梯取到 **×8**（≈ 742 K，实测余量仍 ≥ 2.5×）：判据要**稳定**，
    #: 不取"贴在上界附近"的档位（那是**允许**保守降级的区间，见下一条用例）。
    @pytest.mark.parametrize("k", [1, 2, 3, 4, 5, 6, 8])
    def test_reverse_real_ledger_is_not_degraded(self, k):
        """台账 ×1…×8（92 K → 742 K，含 **≥ 417 K** 档）**不降级**，静默卡可见。"""
        body = self._ledger() * k + "\n" + CARD
        objs, degraded, reason = _objects_diag(body)
        assert degraded is False, f"台账 ×{k}（{len(body)} 字符）误降级：{reason}"
        assert reason == ""
        assert CARD_OBJ in objs, f"台账 ×{k} 丢卡"

    def test_reverse_beyond_margin_degrades_conservatively(self):
        """**边界明示**：规模继续放大（远超 417 K）时**允许**保守降级。

        判据不是"永不降级"（那等于无上界 = O(n²) 回归），而是**降级方向保守**：
        `degraded=True` + `reason` 可读，且 `from_handoff` **不得**因此 fail-open。
        本条把"上界附近是保守区间"写死，避免下一轮把它误读成"误降级缺陷"。
        """
        body = self._ledger() * 16 + "\n" + CARD     # ~1.5 MB > `DEFAULT_SCAN_BUDGET`
        objs, degraded, reason = _objects_diag(body)
        assert degraded is True, "超 `DEFAULT_SCAN_BUDGET` 却未标记（静默收摊）"
        assert reason == SCAN_REASON_TRUNCATED
        g = _guard(body)
        assert g.is_fused() is True and g.should_wake_next() is False

    def test_reverse_ledger_x5_end_to_end_still_fused(self):
        """端到端（≥417 K 真实正文 + 静默卡）：`fused=True` / `wake=False`。

        这正是总师实测的**误降级触发规模**（417 480 字符）：若上界取值过小，
        此处的卡会被吞 → `wake=True`（fail-open）。本判据把它钉住。
        """
        body = self._ledger() * 5 + "\n" + CARD
        assert len(body) >= 417_480
        g = _guard(body)
        assert g.is_fused() is True, "≥417 K 真实正文被误降级（fail-open 方向）"
        assert g.should_wake_next() is False

    def test_reverse_ledger_x5_adopt_budget_not_degraded(self):
        """端到端（`adopt_budget` 通道）：≥417 K 正文**不得**被保守继承熔断。"""
        body = self._ledger() * 5 + "\n" + CARD
        g = LoopGuard(SESSION, data_dir=tempfile.mkdtemp())
        res = g.adopt_budget(body)
        assert res["merged"] is True
        assert res.get("degraded") is False, res
        assert g.status()["round_no"] == 4

    def test_reverse_calibration_has_margin_beyond_417k(self):
        """取值依据（实测 · **裁定口径 = 台账代理** · 余量 ≥ 5×）：417 K 反向
        对照必须留足余量。

        判据 = 台账 ×5（**≥ 417 480 字符**，总师裁定的反向对照规模）的实际记账量
        **≤ `MAX_SCAN_WORK` / 5**（余量 **≥ 5×**）。

        ⚠️ **口径单源（总师裁定 · 2026-09-19）**：`≥5×` **绑定一个显式口径**
        = **真实交接正文的代理 = 台账（`issues/TEST-REGRESSION.md`）**。
        修前 39.49 M（5.06×，且随留痕增长会翻转）；修后（记账口径已变）
        **25.35 M → 实测 28 302 658 ⇒ 7.07×**（本用例钉住 ≥5×）。
        **最密真实文件**（`tests/test_collab_round6_fixes_issue14.py` 重复到
        417 480 字符，修后 **1.16×、未降级**）**登记为"允许保守降级区间"的实测
        数字**，**不**作"不得降级"硬门槛（见 `test_reverse_densest_file_is_logged_
        conservative_band`）。**不批准**抬 cap 去凑最密口径（会侵蚀 G6/G7 时间预算）。
        """
        cap = _need("MAX_SCAN_WORK", MAX_SCAN_WORK)
        body = self._ledger() * 5 + "\n" + CARD
        assert len(body) >= 417_480, len(body)
        _, _, _, work = _scan_diag_probe(body)
        assert work < cap, (work, cap)
        assert work * 5 <= cap, f"417 K 正文余量不足 5×：work={work} cap={cap}"

    def test_reverse_densest_file_is_logged_conservative_band(self):
        """**口径边界登记（总师裁定）**：最密真实文件的余量**登记为实测数字**，
        **不**作"不得降级"硬门槛。

        枚举口径 = 把**每个真实文件**重复到 ≥ 417 480 字符；修后最差
        **1.16×**（`tests/test_collab_round6_fixes_issue14.py`，
        `work = 171 704 178`），**未降级**（保守方向 = 多停一次，非 fail-open）。
        本用例只**钉住"该形态必须被登记"**（可复跑数字 + 方向），
        不把它当"≥5×"判据（否则会逼出"抬 cap 到 1 G"的反向修法）。
        """
        cap = _need("MAX_SCAN_WORK", MAX_SCAN_WORK)
        path = os.path.join(REPO_ROOT, "state-secretary", "tests",
                            "test_collab_round6_fixes_issue14.py")
        with open(path, encoding="utf-8") as f:
            src = f.read()
        reps = max(1, 417_480 // len(src) + 1)
        body = src * reps
        assert len(body) >= 417_480, len(body)
        _, _, _, work = _scan_diag_probe(body)
        margin = cap / work
        # 登记区间：该形态余量 < 5×（实测 ≈1.16×），但**远未触顶**（> 1×）
        assert margin > 1.0, f"最密文件形态已触顶（该形态必须仍不降级）：{margin}"
        objs, degraded, reason = _objects_diag(body)
        assert degraded is False, (degraded, reason)
        assert len(objs) >= 1, "最密文件形态丢对象"

    def test_reverse_worst_adversarial_still_bounded(self):
        """**同批对照的另一半**：真实正文不降级，同时对抗输入**仍被上界拦住**。"""
        body = '{"k":1, ' * 40000 + '}'
        _, degraded, reason = _objects_diag(body)
        assert degraded is True
        assert reason in (SCAN_REASON_WORK_CAP, SCAN_REASON_RESCAN_LEN)


# ==================================================================
# R1–R5（总师裁定 A⊕B · 2026-09-19T00:26:34Z）
#   范围冻结：只改文档/断言/数字与实现同源 + 补降级区间 fuzz；
#   **不改** `_scan_diag` 逻辑 / 常量取值 / 消费侧接线。
# ==================================================================

class TestR1AssertionsMatchImplementation:
    """**R1 断言同源**：把两条失真断言换成**可复跑**判据 + 反例用例。

    失真断言（总师独立复现 · 均成立）：
    ① "长度超 `MAX_RESCAN_LEN` 的合法对象会被跳过" —— **反例**：含内层 `}`
       的 5.2 MB 对象 → `n=1`（未跳过）。
    ② "长度 ≤ `MAX_RESCAN_LEN` 必全捞到" —— **反例**：14 B 卡在微对象密集
       正文后 → `work_cap` 丢卡（基线捞得到）。
    """

    def test_r1a_source_guard_does_not_claim_length_over_cap_is_skipped(self):
        """**源码护栏**：`trail.py` 不得再出现"长度超上界的合法对象会被跳过"。"""
        path = os.path.join(REPO_ROOT, "state-secretary", "src", "secretary",
                            "trail.py")
        with open(path, encoding="utf-8") as f:
            src = f.read()
        # 失真断言①的**断言式**写法（"会被跳过——但那是显式降级"）不得再现；
        # 出现在**否定语境**里的引用（"不是……会被跳过"）不算断言。
        for bad in ("的合法对象会被跳过——但那是",
                    "的合法对象会被**跳过**——但那是"):
            assert bad not in src, f"失真断言①仍在：{bad}"
        # 同源写法必须出现
        assert "到窗口内首个" in src

    def test_r1a_object_over_cap_is_rejected_and_flagged(self):
        """**🔴-2 定点判据（本轮修法后 · 判据同源）**：长度 > `MAX_RESCAN_LEN`
        的合法对象**必须**被 `MAX_RESCAN_LEN` 拒绝**且显式降级**（`rescan_len`）。

        修法前（🔴-2）：`MAX_RESCAN_LEN` 只用于"找 `}`"，命中即对**整段** `raw_decode`
        ⇒ 长度 **786 442**（> cap 262 144）的合法对象**被完整返回**且
        `degraded=False`（上界形同虚设）。
        修法后：解析**只喂窗口** `[p, p + MAX_RESCAN_LEN)` ⇒ 该对象被窗口边界切断
        ⇒ `n=0` **且** `reason="rescan_len"`（**不得**静默丢）。

        同源判据 = **「候选 `p` 的解析被 `MAX_RESCAN_LEN` 边界切断」**，
        不是「长度超上界的合法对象会被跳过」——后者与实现**不同源**（本用例
        断言的是**行为**：超界 → 拒绝 + 显式降级）。
        """
        cap = _need("MAX_RESCAN_LEN", MAX_RESCAN_LEN)
        inner_close = '{"a":"}"' + ',"b":"' + "x" * (5 * 1024 * 1024) + '"}'
        assert len(inner_close) > cap, "前提：对象长度 > 上界"
        assert isinstance(json.loads(inner_close), dict), "前提：合法 JSON 对象"
        # 显式 `budget=len` ⇒ 排除 `DEFAULT_SCAN_BUDGET` 截断，让 `rescan_len`
        # 成为**唯一**可能的降级原因（否则 `truncated` 按优先级抢先）。
        objs, degraded, reason = _objects_diag(inner_close, len(inner_close))
        assert objs == [], (
            "超 `MAX_RESCAN_LEN` 的合法对象**被返回**（🔴-2 回归）")
        assert degraded is True and reason == SCAN_REASON_RESCAN_LEN, (degraded,
                                                                       reason)

    def test_r1a_window_bound_failure_is_flagged_not_silently_dropped(self):
        """**修法第 4 条判据（降级方向）**：窗口切片失败**不得**静默丢。

        字面修法（只喂窗口 + 按窗口长度记账）会把 🔴-2 从"超界返回"变成
        "**静默丢**"：窗口 `raw_decode` 失败 ⇒ `n=0` **且** `degraded=False`
        （新 fail-open）。本用例把"必须置 `rescan_len`"钉住。
        """
        cap = _need("MAX_RESCAN_LEN", MAX_RESCAN_LEN)
        # 窗口把字符串切在半途（`Unterminated string`）→ 必须降级
        body = '{"a": "}' + "y" * (cap * 2) + '"}'
        objs, degraded, reason = _objects_diag(body, len(body))
        assert objs == [] and degraded is True
        assert reason == SCAN_REASON_RESCAN_LEN, (degraded, reason)
        # 反向对照：**本来就不是 JSON 对象**的候选（正文里的 `{"id":…`）不得被
        # 误判降级——否则"≥417 K 真实正文不误降级"直接失效。
        for plain in ('{"id":1, "name":"x"}', 'if (x) { y }'):
            objs2, deg2, reason2 = _objects_diag(plain)
            assert deg2 is False, (plain, reason2)

    def test_r1a_budget_is_a_scan_boundary(self):
        """**🔴-1 定点判据**：`budget` **兼作返回边界**（修法第 3 条）。

        修法前：`scan_json_spans('{"a": "}"}', 8)` 返回 **10 字符 > budget=8**
        （`raw_decode` 拿到整段 `text`）。修法后：解析只喂
        `[p, min(limit, p + MAX_RESCAN_LEN))` ⇒ 任何返回片段都落在 `text[:limit]` 内。
        """
        text = '{"a": "}"}'
        spans = _spans_diag(text, 8)[0]
        assert all(len(sp) <= 8 for sp in spans), (spans, 8)
        assert spans == [], spans

    def test_r1a_charge_does_not_decouple_from_actual_scan(self):
        """**🔴-1 机制判据**：记账**不再与实扫脱钩**（修法第 1/2 条）。

        修法前：`'{"a": "}' + "x"×N`，N=5/50/200 MB → 记账恒 **1 001 039**
        （0.5 % cap），实扫 ≈ N，折算比 3.3 → 130.9 ns/单位（无界）。
        修法后：解析只喂窗口 ⇒ 记账**恒定**且**远大于**旧值（含窗口实扫量），
        耗时**不随正文线性增长**。
        """
        works = []
        dts = []
        for mb in (5, 50):
            body = '{"a": "}' + "x" * (mb * 1024 * 1024)
            t0 = time.perf_counter()
            _, _, _, work = _scan_diag_probe(body, limit=1_000_000)
            dts.append(time.perf_counter() - t0)
            works.append(work)
        assert works[0] == works[1], f"记账仍随正文变化：{works}"
        assert works[0] > 1_001_039 * 1.2, (
            f"记账未含窗口实扫量（旧值 1 001 039）：{works[0]}")
        assert max(dts) < 0.05, f"耗时仍随正文增长：{dts}"

    def test_r1b_source_guard_invariant_has_degraded_false_premise(self):
        """**源码护栏**：超集召回不变量必须带前提 `degraded=False`。"""
        for rel in ("state-secretary/src/secretary/trail.py",
                    "docs/secretary-collab-rules.md",
                    "issues/TEST-REGRESSION.md"):
            with open(os.path.join(REPO_ROOT, rel), encoding="utf-8") as f:
                src = f.read()
            assert "degraded=False" in src, f"{rel} 缺 `degraded=False` 前提"
            assert "降级" in src and "允许丢段" in src, f"{rel} 未明示降级允许丢段"

    def test_r1b_short_card_lost_under_degradation(self):
        """**反例②（可复跑）**：长度 **≤** `MAX_RESCAN_LEN` 的卡在降级下**被丢**。

        `{"wake":false}` 仅 14 B（远小于上界），接在 400 000 个 `{}` 之后
        → `work_cap` 丢卡，而基线 `_baseline_objects()` **捞得到**。
        ⇒ 无前提的"长度 ≤ 上界必全捞到"**不成立**；判据必须写成
        "`degraded=False` 时 `LOST=0`"。
        """
        cap = _need("MAX_RESCAN_LEN", MAX_RESCAN_LEN)
        small_card = '{"wake":false}'
        assert len(small_card) <= cap, "前提：卡长度 ≤ 上界"
        body = "{}" * 400000 + small_card
        base = set(_canon(_baseline_objects(body)))
        objs, degraded, reason = _objects_diag(body)
        new = set(_canon(objs))
        assert json.dumps({"wake": False}) in base, "前提：基线能捞到该卡"
        assert degraded is True and reason == SCAN_REASON_WORK_CAP, (degraded,
                                                                    reason)
        assert base - new, "长度 ≤ 上界的卡在降级下**被丢** ⇒ 无前提断言被推翻"

    def test_r1b_invariant_holds_when_not_degraded(self):
        """**同源判据的正向一半**：`degraded=False` 时 `LOST=0` 确实成立。"""
        rnd = random.Random(20260919)
        for _ in range(300):
            body = "".join(rnd.choice(['{"a":1}', '{"k":', '{', '}', '"', 'x'])
                           for _ in range(rnd.randint(0, 30))) + CARD
            base = set(_canon(_baseline_objects(body)))
            objs, degraded, _ = _objects_diag(body)
            if degraded:
                continue
            assert not (base - set(_canon(objs))), "未降级却丢对象"


class TestR2AttributionIsRedundantFallback:
    """**R2 归因（B 落地）**：① = 冗余兜底 / ② = 承载上界；删除"缺一不可"。

    判据 = **消融敏感**：去掉 ① 不改变量级（仍被 ② 拦住），去掉 ② 才失控。
    """

    def test_r2_source_guard_no_mutual_necessity_claim(self):
        """**源码护栏**：三处不得再写"缺一不可"（scan 上界语境）。"""
        # 判据 = **断言式**写法不得再现（"两条上界（缺一不可）"）；出现在
        # **否定语境**里的引用（"不是「缺一不可」"）是允许的——那正是本轮改写。
        targets = {
            "state-secretary/src/secretary/trail.py":
                "两条上界**（缺一不可）",
            "docs/secretary-collab-rules.md": "两条上界（缺一不可",
            "issues/TEST-REGRESSION.md": "两条上界（**缺一不可**）",
        }
        for rel, needle in targets.items():
            with open(os.path.join(REPO_ROOT, rel), encoding="utf-8") as f:
                src = f.read()
            assert needle not in src, f"{rel} 仍有「{needle}」"
        # 归因结论必须写明
        with open(os.path.join(REPO_ROOT, "state-secretary", "src",
                               "secretary", "trail.py"), encoding="utf-8") as f:
            src = f.read()
        assert "冗余兜底" in src and "承载上界" in src

    def test_r2_ablation_rescan_len_is_redundant(self):
        """**消融 ①**：`MAX_RESCAN_LEN→1e9` 后对抗输入**仍被 ② 拦住**（同量级）。"""
        adv = '{"k":1, ' * 40000 + '}'
        t0 = time.perf_counter()
        _objects_diag(adv)
        both = time.perf_counter() - t0
        orig = trail_mod.MAX_RESCAN_LEN
        try:
            trail_mod.MAX_RESCAN_LEN = 10 ** 9
            t0 = time.perf_counter()
            _, degraded, reason = _objects_diag(adv)
            cap_off = time.perf_counter() - t0
        finally:
            trail_mod.MAX_RESCAN_LEN = orig
        assert degraded is True, "去掉 ① 后仍须被 ② 拦住（否则 ① 才是承载上界）"
        assert reason == SCAN_REASON_WORK_CAP, reason
        assert cap_off < 0.1, (both, cap_off)
        assert cap_off < max(both, 1e-6) * 20, (
            f"去掉 ① 后耗时应同量级：both={both:.4f}s cap_off={cap_off:.4f}s")

    def test_r2_ablation_work_cap_is_the_bearing_bound(self):
        """**消融 ②**：`MAX_SCAN_WORK→1e18` 后对抗输入**失控**。

        ⚠️ **判据改用记账量（确定性）**：旧判据用墙钟 `work_off > 0.5 s` ——
        该阈值**随解释器/机器翻转**，且本轮 🔴-1/🔴-2 定点修后 A_false 不再重取
        窗口，同一消融的 dt 从 **0.5420 s 降到 0.39 s**（真实改善，非退化）⇒
        硬 0.5 s 阈值**反向失败**。现以**记账量**为主判据（与速度解耦）：
        去掉 ② 后 `work` 必须**远超** cap（实测 **6 232 276 553 ≈ 26× cap**），
        而 dt 只作**相对**判据（`≫ both`）。
        """
        cap = _need("MAX_SCAN_WORK", MAX_SCAN_WORK)
        adv = '{"k":1, ' * 40000 + '}'
        t0 = time.perf_counter()
        _objects_diag(adv)
        both = time.perf_counter() - t0
        work_both = _scan_diag_probe(adv)[3]
        orig = trail_mod.MAX_SCAN_WORK
        try:
            trail_mod.MAX_SCAN_WORK = 10 ** 18
            t0 = time.perf_counter()
            _objects_diag(adv)
            work_off_dt = time.perf_counter() - t0
            work_off = _scan_diag_probe(adv)[3]
        finally:
            trail_mod.MAX_SCAN_WORK = orig
        # 主判据（确定性、与机器速度无关）：记账量失控 ⇒ ② 是承载上界
        assert work_off > cap * 10, (
            f"去掉 ② 后记账量未失控：{work_off} vs cap={cap}")
        assert work_off > work_both * 10, (
            f"② 才是承载上界：both={work_both} work_off={work_off}")
        # 辅助判据（相对，不设绝对阈值）：dt 必须远慢于 both
        assert work_off_dt > max(both, 1e-6) * 20, (
            f"② 才是承载上界：both={both:.4f}s work_off={work_off_dt:.4f}s")


class TestR4DegradedFuzz:
    """**R4 降级 fuzz（本轮核心缺口）**：强制触发三态，判据 = **方向保守**。

    为什么必须补：G1 fuzz（42 000 组）实测 body 最大 962 B、`degraded` 计数
    **0** ⇒ **降级路径零覆盖**。降级下**允许丢段**（已批准语义），故判据
    **不是**"不丢卡"，而是**「消费侧方向保守」**（`fused=True` / `wake=False`）。

    ⚠️ 三态覆盖规模（**实测计数**）：
    `truncated` **7 200 组** / `work_cap` **3 000 组** / `rescan_len` **3 000 组**
    （合计 **13 200 组**；每组都走 `from_handoff` 端到端）。
    `work_cap` 组用**测试内缩小的** `MAX_SCAN_WORK` 以把墙钟压到可跑范围
    （**代码路径不变**，只是更快触顶；另有 `test_r4_work_cap_at_production_cap`
    用**生产取值**单独钉住）。
    """

    _SEEDS_CHEAP = [20260919, 7, 42, 1, 2, 3]
    _SEEDS_CAP = [20260919, 7, 42]

    # ---- 三态构造器：**强制**触发降级（不依赖随机撞上） ----
    @staticmethod
    def _body_truncated(rnd):
        """`truncated`：正文 > `DEFAULT_SCAN_BUDGET`（`limit < len(text)`）。"""
        pad = "".join(rnd.choice('{}"x') for _ in range(rnd.randint(0, 200)))
        return '{"a":1}\n' + "x" * (DEFAULT_SCAN_BUDGET + rnd.randint(1, 5000)) \
            + pad + CARD

    @staticmethod
    def _body_work_cap(rnd, n=None):
        """`work_cap`：微对象密集串把折算量顶过 `MAX_SCAN_WORK`。"""
        return '{"keep":1}' + "{}" * (n or rnd.randint(2000, 2600)) + CARD

    @staticmethod
    def _body_rescan_len(rnd):
        """`rescan_len`：候选到窗口内首个 `}` 的距离 > `MAX_RESCAN_LEN`。"""
        cap = _need("MAX_RESCAN_LEN", MAX_RESCAN_LEN)
        pad = "x" * (cap + rnd.randint(1, 5000))
        return '{"a": ' + pad + "}" + CARD

    @pytest.mark.parametrize("seed", _SEEDS_CHEAP)
    def test_r4_truncated_fuzz_is_conservative(self, seed):
        """>= 数千组 `truncated`：消费侧方向**保守**（`fused=True`/`wake=False`）。"""
        rnd = random.Random(seed)
        n = 0
        for _ in range(1200):
            body = self._body_truncated(rnd)
            objs, degraded, reason = _objects_diag(body)
            assert degraded is True, "构造的输入未降级"
            assert reason == SCAN_REASON_TRUNCATED, reason
            n += 1
            g = _guard(body)
            assert g.is_fused() is True, "降级方向不保守（fused）"
            assert g.should_wake_next() is False, "降级方向不保守（wake）"
        assert n >= 1200, f"truncated 覆盖不足：{n}"

    @pytest.mark.parametrize("seed", _SEEDS_CAP)
    def test_r4_work_cap_fuzz_is_conservative(self, seed):
        """>= 数千组 `work_cap`：**允许丢段**，但方向必须保守。"""
        rnd = random.Random(seed)
        orig = trail_mod.MAX_SCAN_WORK
        n = 0
        lost_any = 0
        try:
            trail_mod.MAX_SCAN_WORK = 1_500_000     # 更快触顶（路径不变）
            for _ in range(1000):
                body = self._body_work_cap(rnd)
                objs, degraded, reason = _objects_diag(body)
                assert degraded is True, "构造的输入未降级"
                assert reason == SCAN_REASON_WORK_CAP, reason
                n += 1
                if CARD_OBJ not in objs:
                    lost_any += 1          # **允许**（已批准语义）
                g = _guard(body)
                assert g.is_fused() is True, "降级方向不保守（fused）"
                assert g.should_wake_next() is False, "降级方向不保守（wake）"
        finally:
            trail_mod.MAX_SCAN_WORK = orig
        assert n >= 1000, f"work_cap 覆盖不足：{n}"
        assert lost_any > 0, "work_cap 下应当**允许**丢段（本用例覆盖该语义）"

    def test_r4_work_cap_at_production_cap(self):
        """**生产取值钉住**：不缩小上界时 `work_cap` 仍可达且方向保守。"""
        cap = _need("MAX_SCAN_WORK", MAX_SCAN_WORK)
        body = '{"keep":1}' + "{}" * 240000 + CARD
        _, _, _, work = _scan_diag_probe(body)
        assert work <= cap, (work, cap)
        objs, degraded, reason = _objects_diag(body)
        assert degraded is True and reason == SCAN_REASON_WORK_CAP, (degraded,
                                                                    reason)
        g = _guard(body)
        assert g.is_fused() is True and g.should_wake_next() is False

    @pytest.mark.parametrize("seed", _SEEDS_CAP)
    def test_r4_rescan_len_fuzz_is_conservative(self, seed):
        """>= 数千组 `rescan_len`：方向保守 + 已取片段不丢。"""
        rnd = random.Random(seed)
        n = 0
        for _ in range(1000):
            body = self._body_rescan_len(rnd)
            objs, degraded, reason = _objects_diag(body)
            assert degraded is True, "强制构造的输入未降级"
            assert reason == SCAN_REASON_RESCAN_LEN, reason
            n += 1
            g = _guard(body)
            assert g.is_fused() is True, "降级方向不保守（fused）"
            assert g.should_wake_next() is False, "降级方向不保守（wake）"
        assert n >= 1000, f"rescan_len 覆盖不足：{n}"

    def test_r4_three_states_all_covered_by_thousands(self):
        """**覆盖度自证**：三态在 fuzz 里各 ≥ 数千组（防"零覆盖"复发）。"""
        rnd = random.Random(20260919)
        counts = {SCAN_REASON_TRUNCATED: 0, SCAN_REASON_WORK_CAP: 0,
                  SCAN_REASON_RESCAN_LEN: 0}
        orig = trail_mod.MAX_SCAN_WORK
        try:
            trail_mod.MAX_SCAN_WORK = 1_500_000
            for _ in range(1200):
                for body in (self._body_truncated(rnd),
                             self._body_work_cap(rnd),
                             self._body_rescan_len(rnd)):
                    _, degraded, reason = _objects_diag(body)
                    assert degraded is True, "构造的输入未降级"
                    counts[reason] += 1
        finally:
            trail_mod.MAX_SCAN_WORK = orig
        for state, c in counts.items():
            assert c >= 1200, f"{state} 覆盖不足：{c}"

    def test_r4_g1_fuzz_covers_no_degradation_reproduced(self):
        """**缺口复现（把 R4 的必要性钉住）**：G1 fuzz 形态确实**零降级**。"""
        rnd = random.Random(20260919)
        frags = TestG1SupersetRecallInvariant._FRAGS
        alpha = TestG1SupersetRecallInvariant._ALPHA
        deg = 0
        maxlen = 0
        for _ in range(2000):
            kind = rnd.choice(["random", "frag", "quoteish"])
            if kind == "random":
                body = "".join(rnd.choice(alpha)
                               for _ in range(rnd.randint(0, 40)))
            elif kind == "frag":
                body = "".join(rnd.choice(frags)
                               for _ in range(rnd.randint(0, 40)))
            else:
                body = "".join(rnd.choice(['"', '\\', '{', '}', 'x'])
                               for _ in range(rnd.randint(0, 40)))
            body = body + CARD
            maxlen = max(maxlen, len(body))
            if _objects_diag(body)[1]:
                deg += 1
        assert deg == 0, f"G1 形态意外覆盖了降级：{deg}"
        assert maxlen < 5000, f"G1 形态规模很小（{maxlen} B）⇒ 覆盖不到降级"


class TestR5DiscriminatingPower:
    """**R5 判别力**：`test_g7_accounting_covers_measured_worst_case` 改造为
    **消融敏感**（绑 `_SCAN_CANDIDATE_COST` 计入/不计入的记账量差）。

    旧用例（实测）：原 0.406 s / cap→1e9 0.397 s / candcost→0 1.021 s，
    三者均 < 2.0 ⇒ **恒真**（无判别力）。现在改为**记账量**判据（确定性、
    不受机器速度影响）。
    """

    def test_r5_candidate_cost_is_bound_into_accounting(self):
        """**消融敏感**：`_SCAN_CANDIDATE_COST` 计入/不计入 → 记账量差 **≫ 1×**。

        微对象密集形态下，每候选固定开销主导记账；去掉后记账量掉一个数量级
        ⇒ 判据能把"记账口径被改坏"抓出来（旧用例抓不出）。
        """
        body = "{}" * 500000
        _, _, _, work_full = _scan_diag_probe(body)
        orig = trail_mod._SCAN_CANDIDATE_COST
        try:
            trail_mod._SCAN_CANDIDATE_COST = 0
            _, _, _, work_zero = _scan_diag_probe(body)
        finally:
            trail_mod._SCAN_CANDIDATE_COST = orig
        assert work_zero > 0
        ratio = work_full / work_zero
        assert ratio > 10, (
            f"记账量对 `_SCAN_CANDIDATE_COST` 不敏感（ratio={ratio:.2f}）"
            "⇒ 用例无判别力")
        # 源码护栏：常量存在且被计入
        with open(os.path.join(REPO_ROOT, "state-secretary", "src", "secretary",
                               "trail.py"), encoding="utf-8") as f:
            src = f.read()
        assert "_SCAN_CANDIDATE_COST" in src
        assert "cost = (j - p) + _SCAN_CANDIDATE_COST" in src

    def test_r5_worst_case_is_measured_and_bounded(self):
        """最坏耗时判据保留，但**同时**给出可复跑的**折算比**（口径标注）。

        修法后（解析只喂窗口）折算比**不再随正文线性增长**：未闭合字符串 +
        巨尾形态 5/50/200 MB → `dt` **恒定 0.0003 s**、`work` 恒 **1 263 177**。
        本用例仍只写**实测值**（不写成"必然上界"）——折算比因输入形态而异。
        """
        body = "{}" * 500000
        _, _, _, work = _scan_diag_probe(body)
        t0 = time.perf_counter()
        _objects_diag(body)
        dt = time.perf_counter() - t0
        ratio_ns = dt / work * 1e9 if work else float("inf")
        assert dt < 2.0, f"最坏耗时 {dt:.4f}s"
        # 折算比 × 上界 < 2 s（**可复跑**形式，非"必然上界"）
        assert MAX_SCAN_WORK * ratio_ns / 1e9 < 2.0, (ratio_ns, dt, work)
        # 修法第 1/2 条：未闭合巨尾形态的**记账恒定**（不再与实扫脱钩）
        works = [_scan_diag_probe('{"a": "}' + "x" * (mb * 1024 * 1024),
                                  limit=1_000_000)[3] for mb in (5, 50)]
        assert works[0] == works[1], f"记账仍随正文变化（脱钩）：{works}"

    def test_r5_ablation_cap_off_does_not_change_worst_case(self):
        """**判别力对照**：cap→1e9 不改变最坏耗时（⇒ 旧用例据此恒真）。"""
        bodies = ("{}" * 500000, '{"k":1, ' * 40000 + '}', "{" * 999000 + "}",
                  "if (x) {" * 50000 + "}")
        def worst():
            w = 0.0
            for b in bodies:
                t0 = time.perf_counter()
                _objects_diag(b)
                w = max(w, time.perf_counter() - t0)
            return w
        base = worst()
        orig = trail_mod.MAX_RESCAN_LEN
        try:
            trail_mod.MAX_RESCAN_LEN = 10 ** 9
            off = worst()
        finally:
            trail_mod.MAX_RESCAN_LEN = orig
        assert off < 2.0 and base < 2.0, (base, off)
        assert off < max(base, 1e-6) * 10, (
            f"cap→1e9 不应改变量级：base={base:.4f}s off={off:.4f}s"
            "（⇒ 旧用例的『< 2 s』判据在此消融下恒真）")


# ==================================================================
# CP3（总师精化裁定 §四-4 · 六项同批 · 2026-09-19）
#   行为级回归 ①–⑤ + 扩窗收紧判别力 + 两档口径余量
#   ——**行为级**判据（不是源码护栏）：断言的是 `_scan_diag` / `..._diag`
#   的**观测量**（记账、`rescan_len`、`degraded`、`dt`），可复跑、可证伪。
# ==================================================================

class TestCP3RefinedRulingBehaviours:
    """总师精化裁定（PR #15 评论 `2101139351265976320`）的定点回归。

    六项必做里，第 1/2/3/4 项的**行为级**判据落在本类；第 6 项（精化版上重跑
    fuzz）由 `TestG1SupersetRecallInvariant` 承担（本轮在精化版上重跑，`LOST=0`）。

    | 判据 | 断言 |
    |---|---|
    | ① | 未闭合串 + 巨尾（含 `budget=1000`）记账 ≥ 实扫 |
    | ② | 串内含 `}` 的 >L 对象 → `rescan_len`（行为级） |
    | ③ | 台账 ×1…×8 `deg=False`（防朴素版回归） |
    | ④ | G6 1 MB 臂改 `budget=len(text)`（去掉 `DEFAULT_SCAN_BUDGET` 截断干扰） |
    | ⑤ | 数字口径修正：实测记账量 = 新口径值（不是旧 171.7 M / 摊派口径） |
    """

    # ---- ① 未闭合串 + 巨尾：记账 ≥ 实扫 -------------------------------
    def test_1_unterminated_string_with_giant_tail_accounts_ge_real_scan(self):
        """**①** 未闭合串 + 巨尾：记账 **≥** 该次**实际扫描量**（不得再脱钩）。

        旧实现（`pos` 折算）在此恒记 **1 001 039**（cap 的 0.5 %），而实扫随
        正文线性增长 ⇒ 记账与耗时脱钩。修后解析**只喂窗口**，记账必含窗口实扫量。
        """
        budget = 1_000_000
        works = []
        for mb in (5, 50):
            body = '{"a": "}' + "x" * (mb * 1024 * 1024)
            _, deg, _, work = _scan_diag_probe(body, limit=budget)
            works.append(work)
            # 记账 ≥ 喂入解码器的字符数（= 窗口实扫量）+ 基扫
            assert work >= budget, (mb, work, budget)
            assert deg is True, "未闭合巨尾未显式降级"
        # **记账与实扫不再脱钩**：解析只喂窗口 ⇒ 记账**恒定**（旧实现恒 1 001 039
        # 看似也恒定，但其实扫随正文线性增长 ⇒ 脱钩；修后记账 = 窗口实扫量）。
        assert works[0] == works[1], f"记账仍随正文变化（脱钩）：{works}"
        assert works[0] > 1_001_039, (
            f"记账未含窗口实扫量（旧值 1 001 039）：{works[0]}")

    def test_1_budget_1000_unterminated_string_accounts_ge_real_scan(self):
        """**①（含 `budget=1000`）**：短 budget 下记账仍 ≥ 实扫（= `limit`）。"""
        body = '{"a": "' + "x" * 5_000_000 + '"}'
        _, deg, reason, work = _scan_diag_probe(body, limit=1000)
        assert work >= 1000, (work,)
        assert deg is True and reason == SCAN_REASON_TRUNCATED, (deg, reason)

    # ---- ② 串内含 `}` 的 >L 对象 → rescan_len ---------------------------
    def test_2_long_object_with_inner_close_brace_flags_rescan_len(self):
        """**②（行为级）**：串内含 `}` 的 > `MAX_RESCAN_LEN` 对象 → `rescan_len`。

        该对象**合法**却超过每候选上界 ⇒ 必须**不返回**且**显式降级**
        （不是"超界返回"，也不是"静默丢"）。
        """
        cap = _need("MAX_RESCAN_LEN", MAX_RESCAN_LEN)
        obj = '{"a":"}"' + ',"b":"' + "x" * (cap * 2) + '"}'
        assert len(obj) > cap and isinstance(json.loads(obj), dict)
        objs, degraded, reason = _objects_diag(obj, len(obj))
        assert objs == [], "超过每候选上界的合法对象被返回"
        assert degraded is True and reason == SCAN_REASON_RESCAN_LEN, (degraded,
                                                                       reason)
        # 反向对照：**未超过**上界的含内层 `}` 对象必须正常返回、不降级。
        small = '{"a":{"b":1},"c":"}"}'
        objs2, deg2, reason2 = _objects_diag(small, len(small))
        assert objs2 == [json.loads(small)], (objs2,)
        assert deg2 is False and reason2 == "", (deg2, reason2)

    # ---- ③ 台账 ×1…×8 deg=False（防朴素版回归） -----------------------
    @pytest.mark.parametrize("k", [1, 2, 3, 4, 5, 6, 8])
    def test_3_ledger_x1_to_x8_not_degraded(self, k):
        """**③** 台账 ×1…×8 `deg=False`（朴素版口径下这些档会全翻 `rescan_len`）。"""
        with open(LEDGER, encoding="utf-8") as f:
            led = f.read()
        body = led * k + "\n" + CARD
        objs, degraded, reason = _objects_diag(body)
        assert degraded is False, f"台账 ×{k}（{len(body)}）误降级：{reason}"
        assert reason == ""
        assert CARD_OBJ in objs, f"台账 ×{k} 丢卡"

    # ---- ④ G6 1 MB 臂改 budget=len(text) ------------------------------
    def test_4_g6_1mb_arm_uses_full_budget(self):
        """**④** G6 的 1 MB 臂必须用 `budget=len(text)`（不被 `DEFAULT_SCAN_BUDGET`
        截断抢先），否则判据测到的是 `truncated` 而不是**每候选上界**的行为。
        """
        n = 125000
        text = '{"k":1, ' * n + '}'
        assert len(text) > DEFAULT_SCAN_BUDGET, "该臂确实超过默认上界"
        objs, degraded, reason = _objects_diag(text, len(text))
        assert degraded is True
        assert reason == SCAN_REASON_WORK_CAP, reason
        # 耗时仍 < 1 s（G6 的时间判据在**满 budget** 下也要成立）
        t0 = time.perf_counter()
        _objects_diag(text, len(text))
        assert time.perf_counter() - t0 < 1.0
        # 反向：用默认上界会被截断抢先（`truncated`），这正是要排除的干扰
        _, deg2, reason2 = _objects_diag(text)
        assert deg2 is True and reason2 == SCAN_REASON_TRUNCATED, reason2

    # ---- ⑤ 数字口径修正（实测记账量） ---------------------------------
    def test_5_accounting_numbers_match_implementation(self):
        """**⑤** 记账数字必须与**实现同源**（本轮 🔴-1/🔴-2 修后的实测值）。

        口径：最密文件 @417 480（截断形态），**CPython 3.11.16**。
        旧读数 **168 464 154**（"取 `max` 漏记 + A_false 仍重取窗口"）⇒ 本轮
        实测 **29 946 906**（A_true **202** / A_false **901**；A_false 不再扩窗）。
        判据取"明显小于『失败即记满整窗』的摊派下界"（1 103 × 262 144 ≈ 289 M）。
        """
        cap = _need("MAX_SCAN_WORK", MAX_SCAN_WORK)
        path = os.path.join(REPO_ROOT, "state-secretary", "tests",
                            "test_collab_round6_fixes_issue14.py")
        with open(path, encoding="utf-8") as f:
            src = f.read()
        body = (src * (417_480 // len(src) + 1))[:417_480]
        _, deg, reason, work = _scan_diag_probe(body)
        assert deg is False, (deg, reason)
        assert work < cap, (work, cap)
        # 记账量**小于**"每个失败候选都记满整窗"的上界 ⇒ 证明收紧生效。
        # 失败候选数 1 103（实测）、整窗 262 144 ⇒ 摊派口径下界 289 M > cap；
        # 本轮实测 29 946 906。判据取"明显小于摊派下界"。
        assert work < 1_103 * MAX_RESCAN_LEN * 0.8, (
            f"记账仍接近『失败即记满整窗』的摊派口径：{work}")

    # ---- 扩窗收紧的**判别力**（消融：把 A 组判据放回"任何失败都扩窗"） ----
    def test_6_widening_tightening_is_discriminating(self):
        """**判别力（A 组收紧的落地 · 本轮 🔴-2 改为消融敏感）**：短切片
        **普通语法错**必须**不**扩窗，且该判据必须**真的**改变记账。

        ⚠️ **旧写法无判别力**（本轮 🔴-2 点名）：旧实现里 A_false **仍然重取
        窗口**（记账取 `max`）⇒ 强制 `_decoder_exhausted_input → True` 的消融在
        3 个语料 + 3 000 组 fuzz 上**记账/降级/原因 delta 全 0**（"读了不生效"）。
        本用例现在**直接断言消融敏感**：强制 `True` 后记账必须**显著变大**
        （A_false 从"只喂短切片"变成"短切片 + 整窗"）。

        实测（CPython 3.11.16）：最密 @417 480 记账 **29 946 906 → 191 483 814**
        （**6.39×**，delta 161 536 908）；台账 ×5 **8 959 428 → 35 828 982**
        （**4.00×**）。
        """
        from secretary import trail as tm
        path = os.path.join(REPO_ROOT, "state-secretary", "tests",
                            "test_collab_round6_fixes_issue14.py")
        with open(path, encoding="utf-8") as f:
            src = f.read()
        body = (src * (417_480 // len(src) + 1))[:417_480]
        limit = len(body)
        short_fail = widened = 0
        i = 0
        last_close = body.rfind("}", 0, limit)
        while True:
            p = body.find("{", i, limit)
            if p < 0 or p > last_close:
                break
            j = tm._JSON_WS_RE.match(body, p + 1, limit).end()
            if j >= limit or (body[j] != '"' and body[j] != "}"):
                i = p + 1
                continue
            we = min(limit, p + tm.MAX_RESCAN_LEN)
            close = body.find("}", p, we)
            if close < 0:
                i = p + 1
                continue
            try:
                json.loads(body[p:close + 1])
                i = close + 1
                continue
            except (ValueError, RecursionError) as exc:
                short_fail += 1
                if tm._decoder_exhausted_input(exc, close + 1 - p):
                    widened += 1
            i = p + 1
        assert short_fail >= 500, f"该语料短切片失败候选过少：{short_fail}"
        # 同源计数（与实现同口径）：A_true = 扩窗、A_false = 普通语法错。
        # 实测 1 103 = A_true **202** + A_false **901**（旧注释写"0 / 927"失真）。
        assert widened == 202, (
            f"A_true 计数与实测不同源：{widened}（应为 202；总失败 {short_fail}）")
        assert short_fail - widened == 901, (
            f"A_false 计数与实测不同源：{short_fail - widened}（应为 901）")
        # 记账量据此明显低于"失败即记满整窗"的摊派口径
        _, deg, reason, work = _scan_diag_probe(body)
        assert deg is False, (deg, reason)
        assert work < short_fail * tm.MAX_RESCAN_LEN, (
            f"记账仍达『失败即记满整窗』口径：{work} vs "
            f"{short_fail * tm.MAX_RESCAN_LEN}")
        # ---- 消融敏感（本轮 🔴-2 的**判别性**判据）----
        # 强制 A 组判据恒真（= 收紧前"任何失败都扩窗"）：记账必须显著变大。
        orig = tm._decoder_exhausted_input
        try:
            tm._decoder_exhausted_input = lambda exc, n: True
            _, deg_abl, reason_abl, work_abl = _scan_diag_probe(body)
        finally:
            tm._decoder_exhausted_input = orig
        assert work_abl > work * 3, (
            f"A 组判据**未生效**（消融后记账几乎不变）：base={work} "
            f"abl={work_abl}（应 ≥3×；旧实现 delta 恒 0）")
        # 消融必须**只**改记账（不改变降级/召回判定）——否则说明判据越权。
        assert (deg_abl, reason_abl) == (deg, reason), (deg_abl, reason_abl)

    # ---- 两档口径余量（§三） ------------------------------------------
    def test_7_commitment_tier_margin_at_least_5x(self):
        """**两档口径 · 承诺档**（输入域正文 = 台账 md）：台账 ×5 ≥ **5×**。"""
        cap = _need("MAX_SCAN_WORK", MAX_SCAN_WORK)
        with open(LEDGER, encoding="utf-8") as f:
            led = f.read()
        body = led * 5 + "\n" + CARD
        assert len(body) >= 417_480, len(body)
        _, _, _, work = _scan_diag_probe(body)
        assert work * 5 <= cap, f"承诺档余量不足 5×：work={work} cap={cap}"

    def test_8_pressure_tier_is_logged_conservative(self):
        """**两档口径 · 压力档**（全仓任意文本 @417 480）**登记 + 不触顶**。

        口径：最密 = `tests/test_collab_round6_fixes_issue14.py` @417 480（截断），
        **CPython 3.11.16**，记账 = `_scan_diag` 的 `work`。
        旧读数 **168 464 154**（余量 1.42×，"取 `max` 漏记 + A_false 仍重取窗口"
        的失真值）⇒ 本轮实测 **29 946 906**、余量 **8.01×**（≥5×）。

        ⚠️ **旧断言 `margin < 5.0` 是反向失败点**（🟡5）：修好 🔴-1 后余量必然
        > 5×，该硬断言会把**修好的实现**判失败。现改为"**登记 + 不触顶**"：
        只要求**实测值被登记**（可复跑、写进台账）且**未触顶**（不降级）。
        """
        cap = _need("MAX_SCAN_WORK", MAX_SCAN_WORK)
        path = os.path.join(REPO_ROOT, "state-secretary", "tests",
                            "test_collab_round6_fixes_issue14.py")
        with open(path, encoding="utf-8") as f:
            src = f.read()
        body = (src * (417_480 // len(src) + 1))[:417_480]
        _, deg, reason, work = _scan_diag_probe(body)
        assert work < cap, f"压力档最密形态已触顶（不该）：{work} / {cap}"
        assert deg is False, (deg, reason)
        margin = cap / work
        assert margin > 1.0, margin
        # **登记**：该实测值必须写进常量注释（口径可复跑、防数字漂移）。
        with open(os.path.join(REPO_ROOT, "state-secretary", "src", "secretary",
                               "trail.py"), encoding="utf-8") as f:
            src_trail = f.read()
        assert "29 946 906" in src_trail, (
            "压力档实测记账量未登记在 trail.py 常量注释里（防口径漂移）")
        assert "8.01×" in src_trail, "压力档余量未登记"

    def test_9_worst_case_dt_is_remeasured_and_bounded(self):
        """**§三 上侧重测**（`RecursionError` 深嵌套）：**形态无关**判据。

        ⚠️ **旧判据无判别力且随环境翻转**（本轮 🔴-3 点名）：旧用例断言
        "最坏 dt < 2 s"，实测 **3.11 通过 / 3.13 失败** ——
        3.11（本机）d=6000 **未触顶** dt **0.6164 s**、d=8000 起触顶；
        3.13（总师实测）d=12000 **未触顶** dt **2.21 s**、d=14000 **3.65 s**。
        同一判据在两个解释器上结论相反 ⇒ 属"结论随环境翻转"的用例。

        现改为**形态无关**的两条（与机器速度、解释器版本解耦）：
          ① **未触顶档位**：记账随 `d` **单调增**（结构性：候选越深，喂入越多）；
          ② **触顶档位**：**必须降级**且 `reason="work_cap"`（不是"静默返回"）。
        这两条在 3.11 / 3.13 上都成立（它们断言的是**结构**，不是**耗时**）。
        另附**实测 dt + 测量形态 + 解释器**（只作观测，**不作断言**）。
        """
        cap = _need("MAX_SCAN_WORK", MAX_SCAN_WORK)
        works = {}
        degs = {}
        dts = {}
        for d in (1000, 2000, 3000, 4000, 5000, 6000, 8000, 12000, 14000):
            body = '{"a":' * d + '1' + '}' * d
            _, deg, reason, work = _scan_diag_probe(body)
            t0 = time.perf_counter()
            _objects_diag(body)
            dts[d] = time.perf_counter() - t0
            works[d] = work
            degs[d] = deg
            if deg:
                # ② 触顶档位：**必须**显式降级，且原因正确（不得静默）
                assert reason == SCAN_REASON_WORK_CAP, (d, deg, reason)
            else:
                # 未触顶档位不得报触顶
                assert work < cap, (d, work, cap)
        # ① 未触顶档位（`deg=False`）记账随 `d` **单调增**
        #    （形态无关：记账只由算法决定，与解释器速度**无关**，跨 3.11/3.13 一致）
        # ⚠️ 判据用 `deg` 分区，**不得**用 `work < cap` —— 触顶档位**提前停**，
        #    其 `work` 可能**低于** cap（实测 d=12000 → 239 906 061 < 240 M）。
        untripped = sorted(d for d in works if not degs[d])
        assert len(untripped) >= 4, (
            f"未触顶档位过少，判据无意义：{untripped}（cap={cap}）")
        for lo, hi in zip(untripped, untripped[1:]):
            assert works[lo] < works[hi], (
                f"未触顶档位记账非单调：d={lo} → {works[lo]}，"
                f"d={hi} → {works[hi]}")
        # ② **触顶档位必须降级**（`work_cap`）——判据**与解释器解耦**：
        #    自然档位在 3.11 上 d≥8000 触顶、在 3.13 上可能整列都不触顶
        #    （总师实测 3.13 d=14000 未触顶），故"必触顶"这一半改用
        #    **确定性缩 cap** 验证（同一形态、把 cap 压到必然越界），
        #    而不是要求自然档位里**必然**出现触顶档（那会随解释器翻转）。
        body_deep = '{"a":' * 2000 + '1' + '}' * 2000
        work_deep = _scan_diag_probe(body_deep)[3]
        assert work_deep < cap, (work_deep, cap)
        small_cap = work_deep // 2
        orig_cap = trail_mod.MAX_SCAN_WORK
        try:
            trail_mod.MAX_SCAN_WORK = small_cap
            _, deg_small, reason_small, work_small = _scan_diag_probe(body_deep)
        finally:
            trail_mod.MAX_SCAN_WORK = orig_cap
        assert deg_small is True and reason_small == SCAN_REASON_WORK_CAP, (
            deg_small, reason_small)
        # `work` 是**触顶前**的累计值（fail-fast：越界那个候选不计入）⇒
        # 判据取"已跑到 cap 附近"，**不得**断言 `>= cap`（那会因差一个候选而翻车）。
        assert work_small > small_cap * 0.9, (work_small, small_cap)
        # 附：实测 dt（**只观测、不断言**——它随解释器/机器翻转）
        # 3.11.16 本机：d=6000 → 0.6164 s（未触顶）；d≥8000 → 0.15–0.41 s（已停）
        # 3.13（总师实测）：d=12000 → 2.21 s；d=14000 → 3.65 s（均未触顶）
        assert all(isinstance(v, float) and v >= 0 for v in dts.values())
        # cap 抬到 240 M 后（时间预算不被抬 cap 侵蚀：触顶档位先停，dt 有界）
        assert cap == 240_000_000, cap
