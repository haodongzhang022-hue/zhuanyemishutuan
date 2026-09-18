"""Issue #21 · `scan_json_spans` 重做（CP1 已批准）—— G1–G7 门禁判别性回归

触发：架构·评审 第 6 次 `blocked` 上报 → 架构·总师裁定（PR #15 @ `9382552`）：
① 确认 `9382552` 属**超范围提交**（scan 重做移出本 PR，由 **Issue #21** 承载）；
② 批准收敛路径 = **`9382552` 的语义 + 两条上界 + `reason` + 三处接线**；
**不批准纯回退**（`a7847f9` 语义 320 KB → 384 s，O(n²)）。

本文件即 Issue #21 的 **G1–G7** 门禁判据（CP1 已由总师 spike 验证，开发直接落地）。

| # | 门禁 | 本文件判据 |
|---|---|---|
| G1 | 语义 = 超集召回 | 差分 fuzz ≥40 000 × ≥12 seed：`LOST=0` **且** `REV=0` |
| G2 | ① `MAX_RESCAN_LEN` **显式** | 触界 → `reason="rescan_len"`；**不可**用总上界顶替 |
| G3 | ② `MAX_SCAN_WORK` **显式**且记账 ≥ 真实扫描量 | 触界 → `reason="work_cap"`；须覆盖**异常构造代价** |
| G4 | 降级契约 `degraded=True` + `reason` | 三态各一条；已配平起点**不得静默丢** |
| G5 | 消费侧接线 **3 处** | `from_handoff` / `adopt_budget` / `probe._coerce_checks_used` |
| **G6** | **含 `}`** 的对抗输入 **< 1 s** | `'{"k":1, '×40000 + '}'`；**禁止**只测无 `}` 输入 |
| **G7** | 记账覆盖异常构造代价 / 每候选限长 | 见 G3/G2 的**机制**断言（非仅结论） |

**不变量（硬判据）**：超集召回 —— `a7847f9` 的 `extract_json_objects` 能取到的
**长度 ≤ `MAX_RESCAN_LEN`** 的对象，本实现**必须全取到**（`_baseline_objects()`
内联复刻基线实现，故回归**不依赖**旧提交可检出）。

**边界明示**：长度超过 `MAX_RESCAN_LEN` 的合法对象会被**跳过**——但那是
**显式降级**（`degraded=True` + `reason="rescan_len"`），消费侧取保守方向，
**不是**静默丢卡。
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
        """**边界明示**：超上界的合法对象被跳过 —— 但**显式降级**（不是静默丢）。"""
        cap = _need("MAX_RESCAN_LEN", MAX_RESCAN_LEN)
        obj = json.dumps({"pad": "z" * (cap + 10)})
        objs, degraded, reason = _objects_diag(obj)
        assert objs == []                  # 被跳过
        assert degraded is True            # 但**显式**（可观测、可复核）
        assert reason == SCAN_REASON_RESCAN_LEN


# ==================================================================
# G3 ② `MAX_SCAN_WORK` **显式**且记账 ≥ 真实扫描量（含异常构造代价）
# ==================================================================

class TestG3WorkAccountingCoversRealCost:
    def test_g3_constant_exists_and_is_explicit(self):
        assert MAX_SCAN_WORK is not None, "缺 MAX_SCAN_WORK（G3 硬要求）"
        assert isinstance(MAX_SCAN_WORK, int) and MAX_SCAN_WORK > 0

    def test_g3_failure_charge_is_not_pos_minus_p(self):
        """**机制断言（G7）**：失败记账取**错误位置 `pos`**，**不是** `pos - p`。

        旧口径 `cost = pos - p` 恒为 8（非法 JSON 的错误位置紧贴候选）⇒
        总记账 800 k ≪ 20 M ⇒ **永不触顶**（320 KB → 2.26 s、`degraded=False`）。
        这里用**源码护栏**把口径钉住：`trail.py` 的失败分支不得出现 `pos - p`。
        """
        path = os.path.join(REPO_ROOT, "state-secretary", "src", "secretary",
                            "trail.py")
        with open(path, encoding="utf-8") as f:
            src = f.read()
        body = src.split("def _scan_diag(", 1)[1]
        code = "\n".join(ln for ln in body.splitlines()
                         if not ln.lstrip().startswith("#"))
        assert "charge = pos if" in code, "失败记账未取错误位置 pos"
        assert "pos - p" not in code, "失败记账回退到 pos - p（永不触顶）"

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
        """记账折算比（实测最坏 ≈ 1.2 ns/单位）→ 上界对应耗时 < 2 s。

        判据取**宽松**上界（只为抓"记账口径失真"这类回归）：若把每候选固定
        开销从记账里去掉，"微对象密集"形态会记成极小量 → 实际耗时远超
        `MAX_SCAN_WORK × 折算比`。
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
        # 记账口径的说明必须点明"错误位置 pos（整段前缀）"
        assert "错误位置" in src and "pos" in src
