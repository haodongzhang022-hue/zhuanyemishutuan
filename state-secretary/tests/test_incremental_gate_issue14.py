"""Issue #14 增量门禁（只跑有效增量 · 反全量冗余）验收矩阵

用户口径（Issue #14 原文要点）：

1. **只跑增量**：可以漏（后续用时会重新提需求去改），但不得大量重复冗余；
2. 「用大量冗余保一个低质量测试」= **资源错配**，会挤占/阻断真正有效的审查与测试；
3. **权责一致**：跑多少写多少——「本轮跑 59 个就写 59 个，不要写全量，
   这会形成歧义，导致误导」；
4. **全量须标记**：全量跑测时哪些是「默认已跑过」的必须标出来，否则
   「全量 743 个」会被读成「本轮跑了 743 个」；
5. **过程可视化**。

本文件用例均为**判别性**（反向对照 + 危险模式下必须失败），并以真实仓库
历史（``a455608`` / ``798e80d`` / ``0ed083d`` 等）做跨版复核。

门禁不变量（贯穿全文件）：

    **计划跑测数 == 报告实跑数**，且**任何"全量"表述都必须同时给出「已跑过」数**。
"""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from secretary.gate import (  # noqa: E402
    INCREMENT_ADDED, INCREMENT_DISSOLVED, INCREMENT_LEGACY,
    CounterWordingError, FullSweepReport, IncrementalGate,
    SweepPlan, SweepTarget, SweepRules, assert_counter, render_count,
)
from secretary.gate.rules import (  # noqa: E402
    classify_changed_path, looks_like_test_path,
)

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
TEST_ROOT = "state-secretary/tests"
NEW_TEST_FILE = f"{TEST_ROOT}/test_incremental_gate_issue14.py"

# 历史增量节点（本仓库真实存在的提交）：用于增量 diff 判定的真实复核
LEGACY_BASE = "a455608"   # 前一已跑过的 head
LEGACY_SMALL = "798e80d"  # 增量 = 由本 PR 引入的测试文件（新增）
LEGACY_BIG = "0ed083d"    # 增量 = 新增测试文件 + 既有文件消解


def _gate() -> IncrementalGate:
    return IncrementalGate(repo_root=REPO_ROOT)


def _git(*args: str) -> str:
    out = subprocess.run(["git", *args], cwd=REPO_ROOT,
                         capture_output=True, text=True)
    assert out.returncode == 0, out.stderr
    return out.stdout


def _rev_or_skip(rev: str) -> str:
    out = subprocess.run(["git", "rev-parse", "--verify", f"{rev}^{{commit}}"],
                         cwd=REPO_ROOT, capture_output=True, text=True)
    if out.returncode != 0:
        pytest.skip(f"历史节点不可用（浅克隆）: {rev}")
    return rev


# ======================================================================
# G1 只跑增量：默认只跑"新增 + 消解"，无关既有项默认不重复
# ======================================================================

class TestG1IncrementalOnly:
    """🔴 门禁核心：默认计划不得包含与增量无关的既有用例。"""

    def test_g1_plan_excludes_unrelated_legacy(self):
        """新旧基线 diff 存在 → 计划内**不得**出现与 diff 无关的既有用例。"""
        _rev_or_skip(LEGACY_BASE)
        plan = _gate().plan_sweep(base=LEGACY_BASE)
        assert plan.planned_count > 0, "前置：本 PR 必有增量用例"
        planned_files = set(plan.planned_files)
        touched = {c["path"] for c in _gate().changed_files(LEGACY_BASE)
                   if looks_like_test_path(c["path"])}
        unrelated = planned_files - touched
        assert not unrelated, (
            f"计划内含与 diff 无关的既有文件（全量冗余回归）：{sorted(unrelated)}")
        # 无关既有项须落在 deferred（已跑过、默认不重复）
        assert plan.deferred_count > 0, \
            "既有用例须被标记为『已跑过、默认不重复』而非直接消失"

    def test_g1_plan_never_exceeds_touched_scope(self):
        """计划用例数 ≤ 触及文件内的用例总数（不得顺手全量）。"""
        _rev_or_skip(LEGACY_BASE)
        gate = _gate()
        plan = gate.plan_sweep(base=LEGACY_BASE)
        touched = [c["path"] for c in gate.changed_files(LEGACY_BASE)
                   if looks_like_test_path(c["path"])]
        scoped = gate.collect("WORKTREE", [
            os.path.relpath(os.path.join(REPO_ROOT, p),
                            os.path.join(REPO_ROOT, "state-secretary"))
            for p in touched])
        assert plan.planned_count <= len(scoped) + 1, (
            f"计划 {plan.planned_count} 例 > 触及范围 {len(scoped)} 例 → 夹带了无关用例")

    def test_g1_deferred_is_labelled_previously_run(self):
        """deferred 项须带 ``seen_at``（已跑过的基线）与可读理由。"""
        _rev_or_skip(LEGACY_BASE)
        plan = _gate().plan_sweep(base=LEGACY_BASE)
        assert plan.deferred, "前置：须有已跑过项"
        for t in plan.deferred:
            assert t.kind == INCREMENT_LEGACY
            assert t.seen_at == LEGACY_BASE
            assert "已跑过" in t.reason

    def test_g1_plan_minimal_when_nothing_changed(self):
        """**反向对照（判别性）**：base == 已跑 head 且**无任何改动** → 计划 0 例。

        判别力：若门禁退化为"总是全量"，本用例立即失败（计划 ≈308 例）。
        为避免未提交工作区干扰，用临时干净 worktree 模拟"增量已跑过且已提交"。
        """
        head_now = _git("rev-parse", "HEAD").strip()
        clean = os.path.join(tempfile.mkdtemp(prefix="gate-clean-"), "wt")
        try:
            subprocess.run(["git", "worktree", "add", "--detach", clean,
                            head_now], cwd=REPO_ROOT, capture_output=True,
                           text=True, check=True)
            gate = IncrementalGate(repo_root=clean)
            plan = gate.plan_sweep(base=head_now)
            assert plan.planned_count == 0, (
                f"base == 已跑 head 且无改动 → 计划须 0 例，实测 "
                f"{plan.planned_count}（全量冗余回归）")
            assert plan.deferred_count > 250, (
                f"既有用例须全部标为『已跑过』，实测 {plan.deferred_count}")
        finally:
            subprocess.run(["git", "worktree", "remove", "--force", clean],
                           cwd=REPO_ROOT, capture_output=True, text=True)

    def test_g1_small_increment_plan_is_small(self):
        """**判别性**：增量只含一个新增测试文件 → 计划须 ≈ 该文件用例数，
        不得是 308（全量）。"""
        _rev_or_skip(LEGACY_SMALL)
        plan = _gate().plan_sweep(base=LEGACY_SMALL)
        assert plan.planned_count < 40, (
            f"小增量（单文件）却计划 {plan.planned_count} 例 → 全量冗余回归")
        assert NEW_TEST_FILE in plan.planned_files, \
            "新增测试文件须在计划内（未跑过 → 必跑）"


# ======================================================================
# G2 三态判定：新增 / 消解 / 已跑过（含冗余剔除）
# ======================================================================

class TestG2TriState:
    def test_g2_new_file_is_added(self):
        """新增文件 → ``added``，不得归入 legacy/dissolved。"""
        t = classify_changed_path(NEW_TEST_FILE, is_new=True)
        assert t.kind == INCREMENT_ADDED
        assert "未跑过" in t.reason

    def test_g2_changed_file_is_dissolved_not_legacy(self):
        """**判别性**：既有文件改动 → ``dissolved``（已跑过但已失效），
        不得直接当作"已跑过就不用跑"。"""
        t = classify_changed_path("state-secretary/tests/test_secretary_session.py",
                                  is_new=False)
        assert t.kind == INCREMENT_DISSOLVED
        assert t.kind != INCREMENT_LEGACY

    def test_g2_test_path_recognised(self):
        assert looks_like_test_path(NEW_TEST_FILE)
        assert looks_like_test_path("tests/test_x.py")
        assert not looks_like_test_path("src/secretary/session/session.py")
        assert not looks_like_test_path("state-secretary/tests/fixture.json")

    def test_g2_file_level_fingerprint_loss_is_conservative(self):
        """指纹缺失（浅克隆/截断）→ 保守按**文件级消解**（宁多跑，不漏跑）。"""
        rules = SweepRules()
        targets = rules.classify([{
            "path": NEW_TEST_FILE, "is_new": False,
            "changed_lines": 3, "added_tests": [], "changed_tests": [],
        }])
        assert len(targets) == 1
        assert targets[0].kind == INCREMENT_DISSOLVED
        assert "保守" in targets[0].reason


# ======================================================================
# G3 反过渡测试：冗余剔除（低质量重复不得挤占有效测试）
# ======================================================================

class TestG3RedundantElimination:
    """🔴 用户口径：错误资源配置 = 大量冗余保一个低质量测试。"""

    @pytest.mark.parametrize("path,risk", [
        ("state-secretary/tests/test_a.py", ""),            # 普通测试 → 消解
        ("docs/design/state-secretary-fold.md", "doc-append"),
        ("issues/TEST-REGRESSION.md", "doc-append"),
    ])
    def test_g3_redundant_labels(self, path, risk):
        from secretary.gate.rules import is_redundant_label
        assert is_redundant_label(path) == risk

    def test_g3_count_sync_test_not_re_planned(self):
        """**判别性**：仅计数/留痕类改动的用例（如「测试计数同步」）默认不重复。"""
        rules = SweepRules()
        t = SweepTarget(
            nodeid=f"{NEW_TEST_FILE}::test_count_sync", kind=INCREMENT_DISSOLVED,
            path=NEW_TEST_FILE, name="test_count_sync", risk="count-sync")
        assert rules.is_redundant(t) is True
        # 非冗余（真实断言面改动）反向对照
        t2 = SweepTarget(
            nodeid=f"{NEW_TEST_FILE}::test_fix_race", kind=INCREMENT_DISSOLVED,
            path=NEW_TEST_FILE, name="test_fix_race", risk="dissolve")
        assert rules.is_redundant(t2) is False

    def test_g3_added_is_never_redundant(self):
        """新增用例永不算冗余——未跑过的断言面必须跑（否则漏了有效测试）。"""
        rules = SweepRules()
        t = SweepTarget(nodeid=f"{NEW_TEST_FILE}::test_new", kind=INCREMENT_ADDED,
                        path=NEW_TEST_FILE, name="test_new", risk="count-sync")
        assert rules.is_redundant(t) is False

    def test_g3_redundant_kept_when_explicitly_requested(self):
        """``include_redundant=True`` 时冗余项仍可跑（人工决定，不静默丢弃）。"""
        _rev_or_skip(LEGACY_BASE)
        gate = _gate()
        a = gate.plan_sweep(base=LEGACY_BASE, include_redundant=False)
        b = gate.plan_sweep(base=LEGACY_BASE, include_redundant=True)
        assert b.planned_count >= a.planned_count, \
            "显式要求时不得丢弃冗余项（须可人工复核）"


# ======================================================================
# G4 全量告警：已跑过项须被标记（防"全量 === 本轮实跑"歧义）
# ======================================================================

class TestG4FullSweepLabelling:
    def test_g4_full_view_labels_previously_run(self):
        """全量视图须逐项标注 ``previously_run``，并给出三数（全量/已跑过/实跑）。"""
        _rev_or_skip(LEGACY_BASE)
        gate = _gate()
        view = gate.full_sweep_view(base=LEGACY_BASE,
                                    executed_now=[f"{NEW_TEST_FILE}::test_x"])
        assert view.total > 0
        assert view.items, "全量视图须逐项列出（可视化）"
        assert all("previously_run" in it for it in view.items)
        assert view.previously_run_count > 0
        text = view.render()
        assert "已跑过" in text and "本轮实跑" in text
        assert "不得把上述全量数当作「本轮实跑数」上报" in text

    def test_g4_full_view_rejects_unlabelled_full_count(self):
        """**判别性**：全量视图若未标出「已跑过」，门禁须拒绝（防误导）。"""
        all_ids = ([f"{NEW_TEST_FILE}::test_a", f"{NEW_TEST_FILE}::test_b",
                    f"{TEST_ROOT}/test_state_tree.py::test_t"]
                   + [f"{TEST_ROOT}/test_state_arbiter.py::test_y{i}"
                      for i in range(5)])
        view = FullSweepReport(
            all_nodeids=all_ids,
            previously_run=[f"{TEST_ROOT}/test_state_tree.py::test_t"],
            executed_now=[f"{NEW_TEST_FILE}::test_a"])
        gate = _gate()
        # 正常视图（已标记）应通过
        gate.assert_full_view(view)
        # 人为抹掉"已跑过"标记 → 须拒绝
        evil = FullSweepReport(
            all_nodeids=all_ids,
            previously_run=[f"{TEST_ROOT}/test_state_tree.py::test_t"],
            executed_now=[f"{NEW_TEST_FILE}::test_a"]).mask_previously_run()
        with pytest.raises(Exception):
            gate.assert_full_view(evil)


# ======================================================================
# G5 计数口径：权责一致（跑多少写多少，不得写"全量"）
# ======================================================================

class TestG5CounterWording:
    """🔴 用户原话：「跑 59 个就写跑 59 个，不要写全量，这会形成歧义，导致误导。」"""

    def test_g5_render_count_reports_executed(self):
        """渲染须以**实跑数**为主语，全量只作括号内规模说明。"""
        text = render_count(59, total_suite=743, deferred=684)
        assert text.startswith("跑 59 个测试"), text
        assert "684" in text and "743" in text

    def test_g5_bare_full_wording_rejected(self):
        """**判别性**：『全量 743 个测试全部通过』（无已跑过标记）→ 拒绝。"""
        with pytest.raises(CounterWordingError) as ei:
            assert_counter("全量 743 个测试全部通过", 59, total_suite=743,
                           deferred=684)
        assert "已跑过" in str(ei.value) or "默认不重复" in str(ei.value)

    def test_g5_full_wording_without_deferred_count_rejected(self):
        """声明全量规模但未给出『已跑过』数 → 拒绝（读者仍会误读）。"""
        with pytest.raises(CounterWordingError):
            assert_counter("全量 743 个测试规模，本轮实跑 59 例", 59,
                           total_suite=743, deferred=0)

    def test_g5_proper_wording_accepted(self):
        """合法写法须通过，并返回本轮实跑数（供直接落账）。"""
        text = render_count(59, total_suite=743, deferred=684)
        assert assert_counter(text, 59, total_suite=743, deferred=684) == 59

    def test_g5_plain_incremental_wording_accepted(self):
        """纯增量口径（不含"全量"字样）直接通过——**只跑增量就该这么写**。"""
        assert assert_counter("本轮跑 59 个测试（增量）", 59) == 59

    def test_g5_unknown_third_number_rejected(self):
        """出现未声明的第三个计数 → 拒绝（防台账漂移/拼凑数字）。"""
        with pytest.raises(CounterWordingError):
            assert_counter("本轮跑 59 个测试，累计 12345 个用例", 59)

    def test_g5_ledger_drift_detected(self):
        """台账计数漂移（子项和 ≠ 实跑）→ 拒绝。

        判别性：历史台账曾出现 228/229 → 237 的漂移；本用例锁定该面。
        """
        from secretary.gate.counter import assert_ledger_counts
        assert_ledger_counts([("门面", 148), ("独立探针", 65)], 213)
        with pytest.raises(CounterWordingError):
            assert_ledger_counts([("门面", 148), ("独立探针", 65)], 308)

    def test_g5_report_counter_matches_plan(self):
        """报告计数主张须与计划一致（计划多少 = 跑多少）。"""
        plan = SweepPlan(base="x")
        plan.targets = [SweepTarget(nodeid=f"{NEW_TEST_FILE}::test_{i}",
                                    kind=INCREMENT_ADDED, path=NEW_TEST_FILE,
                                    name=f"test_{i}") for i in range(59)]
        for i in range(684):
            plan.deferred.append(SweepTarget(
                nodeid=f"{TEST_ROOT}/test_secretary_session.py::test_old_{i}",
                kind=INCREMENT_LEGACY, seen_at="a455608"))
        from secretary.gate import SweepReport
        report = SweepReport(plan)
        report.executed = [t.nodeid for t in plan.targets]
        c = report.counter_claim
        assert c.executed == 59, "实跑数须等于计划数"
        assert c.deferred == 684
        assert c.total_suite == 743
        assert report.executed_count != c.total_suite, \
            "实跑数不得被写成全量数（权责一致）"


# ======================================================================
# G6 过程可视化
# ======================================================================

class TestG6Visualisation:
    def test_g6_plan_render_shows_counts_and_reasons(self):
        plan = SweepPlan(base="a455608")
        plan.targets = [SweepTarget(nodeid=f"{NEW_TEST_FILE}::test_a",
                                    kind=INCREMENT_ADDED, path=NEW_TEST_FILE,
                                    name="test_a", reason="新增")]
        for i in range(3):
            plan.deferred.append(SweepTarget(
                nodeid=f"{TEST_ROOT}/test_state_tree.py::test_d{i}",
                kind=INCREMENT_LEGACY, seen_at="a455608"))
        text = plan.render()
        assert "计划跑测  : 1 例" in text
        assert "已跑过不重复: 3 例" in text
        assert NEW_TEST_FILE in text

    def test_g6_report_render_never_claims_full_run(self):
        from secretary.gate import SweepReport
        plan = SweepPlan(base="a455608")
        plan.targets = [SweepTarget(nodeid=f"{NEW_TEST_FILE}::test_a",
                                    kind=INCREMENT_ADDED, path=NEW_TEST_FILE,
                                    name="test_a")]
        for i in range(307):
            plan.deferred.append(SweepTarget(
                nodeid=f"{TEST_ROOT}/test_secretary_session.py::t{i}",
                kind=INCREMENT_LEGACY, seen_at="a455608"))
        report = SweepReport(plan)
        report.executed = [t.nodeid for t in plan.targets]
        report.passed = list(report.executed)
        text = report.render()
        assert "本轮实跑: 1 例" in text
        assert "308" not in text.split("全量规模")[0], \
            "实跑数位不得出现全量数"
        assert "计数口径: 本轮实跑 1 例" in text


# ======================================================================
# G7 有效覆盖面兜底：允许漏，但不得因剔除而漏掉有效面
# ======================================================================

class TestG7EffectiveCoverage:
    def test_g7_supplements_one_per_uncovered_face(self):
        """某有效面无计划用例 → 补 **1 例**（不是补全量）。"""
        rules = SweepRules()
        available = ([f"{TEST_ROOT}/test_state_tree.py::test_a",
                      f"{TEST_ROOT}/test_state_tree.py::test_b"]
                     + [f"{NEW_TEST_FILE}::test_x"])
        supplements = rules.effective_coverage(
            available, [f"{NEW_TEST_FILE}::test_x"],
            guards=["test_state_tree.py", NEW_TEST_FILE])
        assert len(supplements) == 1, f"每面只补 1 例，实测 {supplements}"
        assert "test_state_tree.py" in supplements[0]

    def test_g7_covered_face_not_supplemented(self):
        """已有覆盖的有效面不得重复补入（防又变成冗余）。"""
        rules = SweepRules()
        available = [f"{TEST_ROOT}/test_state_tree.py::test_a"]
        supplements = rules.effective_coverage(
            available, [f"{TEST_ROOT}/test_state_tree.py::test_a"],
            guards=["test_state_tree.py"])
        assert supplements == []

    def test_g7_plan_covers_all_effective_faces(self):
        """**判别性端到端**：小增量计划仍须覆盖全部有效面（9 个测试文件各 ≥1）。"""
        _rev_or_skip(LEGACY_SMALL)
        gate = _gate()
        plan = gate.plan_sweep(base=LEGACY_SMALL)
        from secretary.gate.incremental import DEFAULT_GUARDS
        planned = "\n".join(plan.planned_files)
        missing = [g for g in DEFAULT_GUARDS if g not in planned]
        assert not missing, f"有效面缺覆盖（兜底失效）：{missing}"


# ======================================================================
# G8 真实仓库端到端（跨版本复核 · 用真实历史节点）
# ======================================================================

class TestG8RealRepoEndToEnd:
    """用本仓库真实历史节点做增量判定，避免"只在合成数据上成立"。"""

    def test_g8_real_pr_increment_is_small_not_full(self):
        """真实增量（本 PR）→ 计划须远小于全量 308（**判别性**）。"""
        _rev_or_skip(LEGACY_BASE)
        gate = _gate()
        full = gate.collect("WORKTREE")
        plan = gate.plan_sweep(base=LEGACY_BASE)
        assert len(full) >= 250, "前置：全量规模须 > 250"
        assert plan.planned_count < len(full) * 0.2, (
            f"增量计划 {plan.planned_count} 例 / 全量 {len(full)} 例 → 未体现"
            "『只跑增量』（应远小于全量）")
        assert plan.deferred_count > len(full) * 0.8, \
            "绝大多数既有用例须被标为『已跑过、默认不重复』"

    def test_g8_big_increment_plan_matches_real_diff(self):
        """较大增量（新增测试文件 + 既有文件消解）→ 计划含 added 与 dissolved，
        且仍不含无关既有项。"""
        _rev_or_skip(LEGACY_BIG)
        gate = _gate()
        plan = gate.plan_sweep(base=LEGACY_BIG)
        kinds = {t.kind for t in plan.targets}
        assert INCREMENT_ADDED in kinds, "新增测试文件须判为 added（未跑过）"
        assert plan.count_by_kind(INCREMENT_ADDED) > 0
        touched = {c["path"] for c in gate.changed_files(LEGACY_BIG)
                   if looks_like_test_path(c["path"])}
        assert not (set(plan.planned_files) - touched), \
            "计划不得含与 diff 无关的既有文件"

    def test_g8_gate_module_self_consistent(self):
        """门禁自身：计划/报告/计数三处口径一致（不变量断言）。"""
        _rev_or_skip(LEGACY_BASE)
        gate = _gate()
        plan = gate.plan_sweep(base=LEGACY_BASE)
        assert plan.planned_count == len(plan.targets)
        assert plan.planned_count + plan.deferred_count + len(plan.excluded) \
            >= plan.planned_count
        # 计数不变量：计划数 == 目标数 == 各子类之和
        assert plan.planned_count == (
            plan.count_by_kind(INCREMENT_ADDED)
            + plan.count_by_kind(INCREMENT_DISSOLVED)
            + plan.count_by_kind(INCREMENT_LEGACY))
        # 计数口径守卫：以"实跑数"为唯一主语渲染的报文须通过
        text = render_count(plan.planned_count,
                            total_suite=plan.planned_count + plan.deferred_count,
                            deferred=plan.deferred_count)
        assert_counter(text, plan.planned_count,
                       total_suite=plan.planned_count + plan.deferred_count,
                       deferred=plan.deferred_count)


# ======================================================================
# G9 跨版判别力（危险模式下必须失败）
# ======================================================================

class TestG9DiscriminativePower:
    """门禁若被改坏（例如"总是全量"或"计数写全量"）→ 本组必须失败。"""

    def test_g9_full_always_plan_fails_scope_assertion(self):
        """变异体 M1：把计划改回"总是全量" → G1 范围断言必须失败。"""
        _rev_or_skip(LEGACY_BASE)
        gate = _gate()
        plan = gate.plan_sweep(base=LEGACY_BASE)
        full = gate.collect("WORKTREE")
        plan.targets = [SweepTarget(nodeid=n, kind=INCREMENT_DISSOLVED,
                                    path=n.split("::")[0])
                        for n in full]  # 变异：全量
        touched = {c["path"] for c in gate.changed_files(LEGACY_BASE)
                   if looks_like_test_path(c["path"])}
        unrelated = set(plan.planned_files) - touched
        assert unrelated, "变异体应被 G1 范围断言捕获（若此断言失败说明门禁无判别力）"

    def test_g9_counter_guard_catches_full_wording(self):
        """变异体 M2：把本轮实跑数写成全量 → 计数守卫必须失败。"""
        with pytest.raises(CounterWordingError):
            assert_counter("全量 308 项全部通过", 12, total_suite=308,
                           deferred=296)

    def test_g9_plan_count_equals_executed_count(self):
        """不变量：计划数 == 实跑数（若不等 → 权责不一致）。"""
        _rev_or_skip(LEGACY_BASE)
        gate = _gate()
        plan = gate.plan_sweep(base=LEGACY_BASE)
        from secretary.gate import SweepReport
        report = SweepReport(plan)
        report.executed = [t.nodeid for t in plan.targets]
        assert report.executed_count == plan.planned_count == \
            report.counter_claim.executed
