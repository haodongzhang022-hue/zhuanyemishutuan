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
    SweepPlan, SweepReport, SweepTarget, SweepRules,
    assert_counter, render_count,
)
from secretary.gate.rules import (  # noqa: E402
    classify_changed_path, looks_like_test_path,
)

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
TEST_ROOT = "state-secretary/tests"
NEW_TEST_FILE = f"{TEST_ROOT}/test_incremental_gate_issue14.py"

# 历史增量节点（本仓库真实存在的提交）：用于增量 diff 判定的真实复核
# 🟡10：原契约写的 798e80d / 0ed083d 在本仓库不存在（git cat-file 失败）→
# 3 项永久 skip、"跨版复核"不可复现。此处改用**真实存在**的修订。
LEGACY_BASE = "a455608"    # 前一已跑过的 head（本 PR 的基线）
LEGACY_BIG = "e3195ea"     # 增量 = 既有测试文件新增 + 消解（真实大增量）
LEGACY_MID = "d6abaab"     # 增量 = 两个测试文件新增（真实中等增量）


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
        """**判别性**：增量只含一个新增测试文件 → 计划须**只**落在该文件，
        不得夹带既有用例（全量冗余回归）。"""
        gate = _gate()
        plan = gate.plan_sweep(base=LEGACY_BASE)
        full = gate.collect("WORKTREE")
        assert plan.planned_files == [NEW_TEST_FILE], (
            f"单文件增量却计划触及 {plan.planned_files} → 夹带无关用例")
        assert plan.planned_count < len(full) * 0.25, (
            f"小增量计划 {plan.planned_count} 例 / 全量 {len(full)} 例 → "
            "未体现『只跑增量』")
        assert plan.deferred_count > len(full) * 0.7, \
            "绝大多数既有用例须被标为『已跑过、默认不重复』"


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
        """**判别性端到端**：兜底只在**本轮触及范围**内生效（不夹带无关用例）。

        口径收敛（复评第 1 轮登记）：契约 §5 原写「9 个测试文件各 ≥1 例」，
        与不变量 5「计划只落在 diff 触及的测试文件内」互斥。正解是：
        **兜底不得把与增量无关的既有文件拉进计划**（允许漏，允许后续重提）。
        本用例锁定该口径：触及范围内的有效面须有覆盖，触及范围外**不得**补入。
        """
        from secretary.gate.incremental import DEFAULT_GUARDS
        gate = _gate()
        plan = gate.plan_sweep(base=LEGACY_BASE)
        touched = {c["path"] for c in gate.changed_files(LEGACY_BASE)
                   if looks_like_test_path(c["path"])}
        # 计划不得触及范围外文件（兜底不得夹带无关用例）
        assert not (set(plan.planned_files) - touched), \
            f"兜底夹带了无关文件：{sorted(set(plan.planned_files) - touched)}"
        # 触及范围内的有效面须有覆盖
        in_scope = [g for g in DEFAULT_GUARDS
                    if any(g in p for p in touched)]
        planned = "\n".join(plan.planned_files)
        missing = [g for g in in_scope if g not in planned]
        assert not missing, f"触及范围内的有效面缺覆盖：{missing}"


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
        """较大增量（新增用例 + 既有用例消解）→ 计划含 added 与 dissolved，
        且仍不含无关既有项。"""
        _rev_or_skip(LEGACY_BIG)
        gate = _gate()
        plan = gate.plan_sweep(base=LEGACY_BIG)
        kinds = {t.kind for t in plan.targets}
        assert INCREMENT_ADDED in kinds, "新增用例须判为 added（未跑过）"
        assert INCREMENT_DISSOLVED in kinds, "既有用例改动须判为 dissolved（消解）"
        assert plan.count_by_kind(INCREMENT_ADDED) > 0
        assert plan.count_by_kind(INCREMENT_DISSOLVED) > 0
        touched = {c["path"] for c in gate.changed_files(LEGACY_BIG)
                   if looks_like_test_path(c["path"])}
        assert not (set(plan.planned_files) - touched), \
            "计划不得含与 diff 无关的既有文件"
        # 中等增量对照：两个文件新增
        plan_mid = gate.plan_sweep(base=LEGACY_MID)
        assert len(plan_mid.planned_files) == 2, plan_mid.planned_files

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
        """不变量：计划数 == 实跑数 —— **真实子进程**判别（🔴-2）。

        旧实现为自证式恒真（手工 ``report.executed = [t.nodeid …]`` 再断言），
        与真实执行无关。本用例改为沙箱真实 pytest 子进程：新增测试文件 →
        计划 N 例 → 实跑必须恰为 N 例（既非 0 也非全量）。
        """
        sandbox = _sandbox_repo(with_new_file=True)
        plan = sandbox.gate.plan_sweep(base="HEAD")
        assert plan.planned_count > 0, "前置：新增文件须产生计划"
        report = sandbox.gate.run(plan)
        assert report.exit_code == 0, report.raw_output[-400:]
        assert report.executed_count == plan.planned_count, (
            f"计划 {plan.planned_count} 例 ≠ 实跑 {report.executed_count} 例 "
            "→ 权责不一致（🔴-2 判别点）")
        assert report.counter_claim.executed == report.executed_count

# ======================================================================
# G10 端到端判别性回归（🔴-2：核心路径 run() 真实覆盖）
# ======================================================================
#
# 旧 34 项用例中 `gate.run(` 调用数 = 0 —— 全部手工构造 SweepReport 后自填
# executed/passed 再断言，故 🔴-1 的「假绿 + 空计划退化为全量」能全绿通过。
# 本组用**真实 pytest 子进程 + rc/汇总/失败注入**做判别，四类必备：
#   (a) 失败注入下报告不得 green；
#   (b) selector 路径正确性（错误口径须 rc=4，正确口径须命中）；
#   (c) 空计划不得退化为全量；
#   (d) 守卫拒绝未含实跑数的文本。


class _Sandbox:
    """沙箱仓库：临时 git repo + 真实可跑的 pytest 用例（隔离真实仓库）。"""

    def __init__(self, root: str, gate):
        self.root = root
        self.gate = gate


def _sandbox_repo(with_new_file: bool = False, failing: bool = False,
                  skipped: bool = False) -> _Sandbox:
    """建一个临时 git 仓库（含 state-secretary/tests），返回可用门禁。

    Args:
        with_new_file: 额外新增一个**未提交**测试文件（增量 = added）。
        failing: 基础测试文件内含一个失败用例。
        skipped: 基础测试文件内含一个跳过用例。
    """
    root = tempfile.mkdtemp(prefix="gate-sbx-")
    pkg = os.path.join(root, "state-secretary")
    tests = os.path.join(pkg, "tests")
    os.makedirs(tests, exist_ok=True)
    with open(os.path.join(pkg, "pytest.ini"), "w", encoding="utf-8") as f:
        f.write("[pytest]\n")
    body = ["def test_ok():", "    assert True", ""]
    if failing:
        body += ["def test_bad():", "    assert False", ""]
    if skipped:
        body += ["import pytest", "", "@pytest.mark.skip(reason='x')",
                 "def test_skip():", "    assert True", ""]
    with open(os.path.join(tests, "test_base.py"), "w", encoding="utf-8") as f:
        f.write("\n".join(body) + "\n")

    def git(*args):
        return subprocess.run(["git", *args], cwd=root, capture_output=True,
                              text=True)

    git("init", "-q")
    git("config", "user.email", "gate@example.com")
    git("config", "user.name", "gate")
    git("add", "-A")
    git("commit", "-qm", "base")

    if with_new_file:
        with open(os.path.join(tests, "test_new.py"), "w",
                  encoding="utf-8") as f:
            f.write("def test_new_a():\n    assert True\n\n"
                    "def test_new_b():\n    assert True\n")

    return _Sandbox(root, IncrementalGate(repo_root=root))


def _plan_for(gate, nodeids, path):
    plan = SweepPlan(base="HEAD")
    for n in nodeids:
        plan.targets.append(SweepTarget(nodeid=n, kind=INCREMENT_ADDED,
                                        path=path, name=n.split("::")[-1]))
    return plan


class TestG10EndToEndDiscriminative:
    """🔴-2：run() 核心路径真实覆盖（四类判别性场景）。"""

    # ---- (b) selector 路径正确性 ----

    def test_g10_selector_path_is_pytest_relative(self):
        """**判别性（b）**：selector 须为 pytest 运行目录相对路径 → 命中 1 例。

        反例（旧口径）：把仓库相对 nodeid 直接当 selector 喂给
        ``cwd=state-secretary/`` 的子进程 → pytest rc=4（not found）。
        """
        sbx = _sandbox_repo()
        nodeid = "state-secretary/tests/test_base.py::test_ok"
        plan = _plan_for(sbx.gate, [nodeid], "state-secretary/tests/test_base.py")
        report = sbx.gate.run(plan)
        assert report.exit_code == 0, (
            f"selector 路径口径错误（rc={report.exit_code}）: "
            f"{report.raw_output[-300:]}")
        assert report.executed_count == 1, "须恰好命中 1 例"
        assert report.passed_count == 1

    def test_g10_repo_relative_selector_would_be_rc4(self):
        """**判别性（b）反证**：旧口径（仓库相对 selector）确实 rc=4。

        本断言证明 🔴-1 的 bug 类别真实存在，并锁定新口径的必要性。
        """
        sbx = _sandbox_repo()
        pkg = os.path.join(sbx.root, "state-secretary")
        out = subprocess.run(
            [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider",
             "state-secretary/tests/test_base.py::test_ok"],
            cwd=pkg, capture_output=True, text=True)
        assert out.returncode == 4, (
            "仓库相对 selector 在 cwd=state-secretary/ 下应 rc=4（路径未找到）")
        assert "not found" in (out.stdout + out.stderr)

    def test_g10_file_level_target_also_relative(self):
        """**判别性（b）**：文件级标的（无 ``::``）同口径 → 不得 rc=4。"""
        sbx = _sandbox_repo()
        plan = SweepPlan(base="HEAD")
        plan.targets.append(SweepTarget(
            nodeid="state-secretary/tests/test_base.py",
            kind=INCREMENT_DISSOLVED, path="state-secretary/tests/test_base.py"))
        report = sbx.gate.run(plan)
        assert report.exit_code == 0, report.raw_output[-300:]
        assert report.executed_count == 1

    # ---- (a) 失败注入下不得 green ----

    def test_g10_failure_injection_must_not_be_green(self):
        """**判别性（a）**：真实失败注入 → green 必须 False（rc 参与判定）。"""
        sbx = _sandbox_repo(failing=True)
        plan = _plan_for(
            sbx.gate,
            ["state-secretary/tests/test_base.py::test_ok",
             "state-secretary/tests/test_base.py::test_bad"],
            "state-secretary/tests/test_base.py")
        report = sbx.gate.run(plan)
        assert report.exit_code == 1, report.raw_output[-300:]
        assert report.failed_count == 1, "失败须被真实解析出来"
        assert report.green is False, "有失败却报绿 = 假绿（🔴-1 判别点）"

    def test_g10_rc_nonzero_never_green(self):
        """**判别性（a）**：rc≠0 一律不得 green（即使失败列表为空）。"""
        sbx = _sandbox_repo()
        plan = _plan_for(
            sbx.gate, ["state-secretary/tests/test_base.py::test_ok"],
            "state-secretary/tests/test_base.py")
        report = sbx.gate.run(plan)
        report.failed, report.errors = [], []
        report.exit_code = 4
        assert report.green is False, "rc=4 却报绿 = 假绿"

    # ---- (c) 空计划不得退化为全量 ----

    def test_g10_empty_plan_does_not_run_full_suite(self):
        """**判别性（c）**：空计划 → 0 例（不得裸跑全量）。"""
        sbx = _sandbox_repo()
        plan = SweepPlan(base="HEAD")   # 无 targets
        report = sbx.gate.run(plan)
        assert report.executed_count == 0, (
            f"空计划却实跑 {report.executed_count} 例 → 退化为全量")
        assert report.exit_code == 0
        assert report.command == "", "空计划不得生成任何 pytest 命令"

    def test_g10_unresolvable_selector_fails_fast_not_full(self):
        """**判别性（c）**：selector/files 皆空 → fail-fast，绝不裸跑全量。"""
        sbx = _sandbox_repo()
        plan = SweepPlan(base="HEAD")
        plan.targets.append(SweepTarget(nodeid="ghost/path.py::test_ghost",
                                        kind=INCREMENT_ADDED, path=""))
        with pytest.raises(Exception) as ei:
            sbx.gate.run(plan)
        msg = str(ei.value)
        assert "不存在" in msg or "退化为全量" in msg, msg
        # 反证：裸跑会跑全量（沙箱里 1 例）
        pkg = os.path.join(sbx.root, "state-secretary")
        bare = subprocess.run(
            [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider"],
            cwd=pkg, capture_output=True, text=True)
        assert bare.returncode == 0 and "1 passed" in bare.stdout

    def test_g10_executed_comes_from_real_run_not_plan(self):
        """**判别性**：``executed`` 取真实执行结果，而非计划构造式赋值。"""
        sbx = _sandbox_repo()
        plan = _plan_for(
            sbx.gate, ["state-secretary/tests/test_base.py::test_ok"],
            "state-secretary/tests/test_base.py")
        report = sbx.gate.run(plan)
        assert report.executed_count == 1
        assert report.counts_verified is True, "计数须来自 junitxml（可复核）"
        assert report.summary_line, "须留 pytest 汇总行"
        assert report.raw_output, "🟡3：原始输出不得丢失（事后可复核）"
        assert report.command and "test_base.py::test_ok" in report.command

    def test_g10_skipped_count_is_real_not_inferred(self):
        """🟡2：``skipped`` 须真实填充（不再由 passed = 计划 − 失败 推断）。"""
        sbx = _sandbox_repo(skipped=True)
        plan = _plan_for(
            sbx.gate,
            ["state-secretary/tests/test_base.py::test_ok",
             "state-secretary/tests/test_base.py::test_skip"],
            "state-secretary/tests/test_base.py")
        report = sbx.gate.run(plan)
        assert report.skipped_count == 1, (
            f"skip 须被真实统计，实测 skipped={report.skipped_count}")
        assert report.passed_count == 1
        assert report.executed_count == 2, "实跑 = 通过 + 跳过（真实口径）"

    def test_g10_silent_drop_of_targets_must_fail_fast(self):
        """**判别性（c）**：若标的被静默丢弃（selectors/files 皆空）→ fail-fast。

        用 monkeypatch 模拟"静默丢弃缺失标的"的变异体（旧实现即如此：
        路径不存在就跳过），证明第二道防线确实会拒绝裸跑全量。
        """
        sbx = _sandbox_repo()
        plan = _plan_for(
            sbx.gate, ["state-secretary/tests/test_base.py::test_ok"],
            "state-secretary/tests/test_base.py")
        monkey = sbx.gate._runnable
        sbx.gate._runnable = lambda p: ([], [])   # 变异：静默丢弃全部标的
        try:
            with pytest.raises(Exception) as ei:
                sbx.gate.run(plan)
            assert "退化为全量" in str(ei.value) or "皆为空" in str(ei.value)
        finally:
            sbx.gate._runnable = monkey

    def test_g10_plan_count_matches_pytest_collection(self):
        """**判别性（🟡5）**：计划口径须 == pytest 收集口径（参数化逐项展开）。

        变异体（计划回退 def 级、不展开参数化）→ 计划数 < 收集数 → 本用例失败。
        """
        gate = _gate()
        plan = gate.plan_sweep(base=LEGACY_BASE)
        rels = [gate._pytest_rel(f) for f in plan.planned_files]
        collected = gate.collect("WORKTREE", rels)
        assert plan.planned_count == len(collected), (
            f"计划 {plan.planned_count} 例 ≠ pytest 收集 {len(collected)} 例 "
            "→ 计划口径与收集口径不一致（🟡5 判别点）")
        assert any("[" in c for c in collected), \
            "前置：触及文件须含参数化用例（用于验证展开口径）"

    # ---- (d) 守卫拒绝未含实跑数的文本 ----

    def test_g10_guard_rejects_text_without_executed_count(self):
        """**判别性（d）**：文本未含实跑数 → 拒绝（旧实现放行并返回 59）。"""
        with pytest.raises(CounterWordingError):
            assert_counter("本轮实跑 3 例", executed=59)

    def test_g10_guard_rejects_forged_full_text(self):
        """**判别性（d）**：伪造「全量 308 项全部通过（296 例已跑过）」
        而实跑 12 → 拒绝（实跑数根本不在文本里）。"""
        with pytest.raises(CounterWordingError):
            assert_counter("全量 308 项全部通过（296 例已跑过）", 12,
                           total_suite=308, deferred=296)

    def test_g10_guard_accepts_true_full_run_text(self):
        """反向失准修复：契约 15.6 的真全量文字按 executed=341 须通过。"""
        assert assert_counter("全量 pytest：341 passed + 3 skipped", 341,
                              total_suite=341) == 341

    def test_g10_assert_report_binds_real_executed(self):
        """🔴-3：``assert_report`` 须绑定**真实实跑数**（非计划数）。

        判别方式：计划 2 例但只解析到 1 例真实执行结果时，传给守卫的
        ``executed`` 必须是 1（真实），而不是 2（计划）。
        """
        import secretary.gate.incremental as inc
        sbx = _sandbox_repo()
        plan = _plan_for(
            sbx.gate,
            ["state-secretary/tests/test_base.py::test_ok",
             "state-secretary/tests/test_base.py::test_ghost"],
            "state-secretary/tests/test_base.py")
        report = SweepReport(plan)
        report.executed = ["state-secretary/tests/test_base.py::test_ok"]
        report.counts = {"passed": 1}
        report.counts_verified = True
        report.exit_code = 0
        captured = {}
        real = inc.assert_counter

        def spy(text, executed, **kw):
            captured["executed"] = executed
            return real(text, executed, **kw)

        inc.assert_counter = spy
        try:
            try:
                sbx.gate.assert_report(report)
            except CounterWordingError:
                pass   # 计划 2 / 实跑 1 的不一致由守卫正常拒绝
        finally:
            inc.assert_counter = real
        assert captured["executed"] == 1, (
            f"守卫须绑定真实实跑数 1，实测绑定 {captured['executed']}（=计划数？）")
        # 文本未含真实实跑数 → 拒绝（🔴-3 绕过路径）
        with pytest.raises(CounterWordingError):
            assert_counter("本轮实跑 7 例", executed=1)
        # 反向：真全量文字（规模 == 实跑）不得被误拒
        assert assert_counter("全量 pytest：1 passed", 1, total_suite=1) == 1
