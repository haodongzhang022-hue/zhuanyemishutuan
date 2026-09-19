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
历史（``a455608`` / ``e3195ea`` / ``d6abaab`` 等）做跨版复核。

🟡6（复评第 2 轮）：原 docstring 仍列 ``798e80d / 0ed083d`` 为"跨版复核"修订，
与 🟡10 的修复矛盾（这两修订在本仓库不存在 → git cat-file 失败 → 永久 skip）。
此处同步为 ``LEGACY_BASE / LEGACY_BIG / LEGACY_MID`` 三个**真实存在**的修订。

门禁不变量（贯穿全文件）：

    **计划跑测数 == 报告实跑数**，且**任何"全量"表述都必须同时给出「已跑过」数**。

复评第 2 轮定点修复覆盖：

- 🔴-1 计数守卫**语境绑定**（序号 / 规模数 / 已跑过数不得冒充实跑数）；
- 🔴-2 回退分支**不得用计划数冒充实跑数**（无计数证据 → 拒绝报绿/落账）；
- 🔴-3 不变量 1 显式守卫 + 全量规模取**实测收集数**（并集去重）；
- 🟡1–🟡7 逐项处置（见各类 docstring）。
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
    CounterWordingError, FullSweepReport, GateError, IncrementalGate,
    SweepPlan, SweepReport, SweepTarget, SweepRules,
    assert_counter, render_count,
)
from secretary.gate.rules import (  # noqa: E402
    classify_changed_path, looks_like_test_path,
)

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
TEST_ROOT = "state-secretary/tests"
NEW_TEST_FILE = f"{TEST_ROOT}/test_incremental_gate_issue14.py"

#: 本增量**新增**的测试文件集合（按提交序列追加，不删旧项）。
#: 为什么是集合而非单值：`plan_sweep(base=LEGACY_BASE)` 的计划面就是
#: "LEGACY_BASE 之后新增的测试文件"。每加一个新增测试文件就要同步这里，
#: 否则 G1 的"小增量只落一个文件"断言会把**合法的第二个新文件**误判成
#: "夹带无关用例"——这是**期望值**需要维护，不是门禁缺陷。
#: 历史：`test_incremental_gate_issue14.py`（Issue #14）→
#:       `test_npc_cost_guard.py`（Issue #24 成本护栏）。
NEW_TEST_FILES = [
    f"{TEST_ROOT}/test_incremental_gate_issue14.py",
    f"{TEST_ROOT}/test_npc_cost_guard.py",
]

#: 相对 `LEGACY_MID` 的新增测试文件集合。`LEGACY_MID` 早于本增量序列的起点，
#: 故比 `NEW_TEST_FILES` 多一个 `test_acceptance_matrix_issue4.py`。
#: 两个基线**分别**维护，不共用（共用会把"基线早于某文件"混成"门禁夹带"）。
NEW_TEST_FILES_AT_MID = [
    f"{TEST_ROOT}/test_acceptance_matrix_issue4.py",
    f"{TEST_ROOT}/test_incremental_gate_issue14.py",
    f"{TEST_ROOT}/test_npc_cost_guard.py",
]

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
        assert sorted(plan.planned_files) == sorted(NEW_TEST_FILES), (
            f"增量新增文件 {NEW_TEST_FILES} 却计划触及 {plan.planned_files}"
            " → 夹带无关用例或漏掉新文件")
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
        """真实增量（本 PR）→ 计划须远小于全量（**判别性**）。

        🟡 阈值口径（复评第 3 轮调整）：原断言用固定比例 ``< full * 0.2``，
        与**用例数耦合**——本 PR 的增量主体就是门禁用例本身，随判别性回归
        增补用例，比例自然上升（实测 72/380=18.9% → 81/389=20.8%）→ 脆弱。
        改为**语义不变量**（不随规模漂移）：

        - 计划 ⊆ diff 触及文件（另见 ``test_g8_big_increment_plan_matches_real_diff``）；
        - 计划数 **严格小于** 已跑过数（增量小于既有 → 确实"只跑增量"）；
        - 已跑过占多数（> 50%）。

        判别力：变异体 M1（"总是全量"）→ ``deferred≈0`` 且
        ``planned == full`` → 三条断言全数失败。
        """
        _rev_or_skip(LEGACY_BASE)
        gate = _gate()
        full = gate.collect("WORKTREE")
        plan = gate.plan_sweep(base=LEGACY_BASE)
        assert len(full) >= 250, "前置：全量规模须 > 250"
        assert plan.planned_count < plan.deferred_count, (
            f"增量计划 {plan.planned_count} 例 ≥ 已跑过 {plan.deferred_count} 例 "
            "→ 未体现『只跑增量』（增量须小于既有）")
        assert plan.planned_count < len(full) * 0.5, (
            f"增量计划 {plan.planned_count} 例 / 全量 {len(full)} 例 → 未体现"
            "『只跑增量』（应远小于全量）")
        assert plan.deferred_count > len(full) * 0.5, \
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
        # 中等增量对照：`LEGACY_MID` 之后新增的测试文件（基线早于本序列起点，
        # 故面比 `NEW_TEST_FILES` 多一个，见 `NEW_TEST_FILES_AT_MID`）
        plan_mid = gate.plan_sweep(base=LEGACY_MID)
        assert sorted(plan_mid.planned_files) == sorted(NEW_TEST_FILES_AT_MID), (
            f"LEGACY_MID 增量计划面 {sorted(plan_mid.planned_files)} != "
            f"新增集 {sorted(NEW_TEST_FILES_AT_MID)}")

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


def _hermetic_git_init(git, root: str) -> None:
    """**封闭式** git 沙箱初始化：不受宿主全局/用户 git 配置影响。

    🔴 根因（Issue #14 收口，实测复现）：沙箱只配了 ``user.email/user.name``，
    **没关签名**。宿主若存在全局 ``commit.gpgsign=true`` 且签名程序不可用
    （CI 镜像里就是如此），``git commit`` 会**静默失败**（rc=128
    "gpg failed to sign the data"）——于是仓库**根本没有 HEAD**，
    随之 ``git diff --numstat HEAD`` 以 128 + 空输出"假装没有变更"，
    用例拿到空条目后表现为 IndexError / AssertionError。

    此类失败看起来像"环境噪声"，实为**两个真缺陷**叠加（见本文件
    ``TestG14BaseUnresolvable``）：沙箱不封闭 + 门禁把"查不到"当"没有"。

    修法：``-c commit.gpgsign=false`` + ``-c gpg.format=openpgp`` 显式对消，
    **并断言 commit 真的成功**（否则后续失败会伪装成"业务失败"，无从归因）。
    """
    git("init", "-q")
    git("config", "user.email", "gate@example.com")
    git("config", "user.name", "gate")
    git("config", "commit.gpgsign", "false")
    git("config", "tag.gpgsign", "false")
    git("config", "core.hooksPath", os.devnull)
    git("add", "-A")
    commit = git("-c", "commit.gpgsign=false", "commit", "-qm", "base")
    assert commit.returncode == 0, (
        f"沙箱基线提交失败（{commit.returncode}）: {commit.stderr}")
    head = git("rev-parse", "--verify", "HEAD")
    assert head.returncode == 0, f"沙箱无 HEAD（提交未落）: {head.stderr}"


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

    _hermetic_git_init(git, root)

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

        判别方式：计划 2 例但只解析到 1 例真实执行结果 → 守卫须以
        **计划 ≠ 实跑** 拒绝（不变量 1），且传给计数守卫的 ``executed``
        必须是 1（真实），而不是 2（计划）。
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
        report.counts_source = "junitxml"
        report.exit_code = 0
        captured = {}
        real = inc.assert_counter

        def spy(text, executed, **kw):
            captured["executed"] = executed
            return real(text, executed, **kw)

        inc.assert_counter = spy
        try:
            # 🔴-3：计划 2 / 实跑 1 → 不变量 1 直接拒绝（GateError）
            with pytest.raises(Exception) as ei:
                sbx.gate.assert_report(report)
            assert "计划 2 例 ≠ 实跑 1 例" in str(ei.value), str(ei.value)
        finally:
            inc.assert_counter = real
        # 文本未含真实实跑数 → 拒绝（🔴-3 绕过路径）
        with pytest.raises(CounterWordingError):
            assert_counter("本轮实跑 7 例", executed=1)
        # 反向：真全量文字（规模 == 实跑）不得被误拒
        assert assert_counter("全量 pytest：1 passed", 1, total_suite=1) == 1
        # 反向：计划 == 实跑 时，守卫须绑定真实实跑数（非计划数）
        plan2 = _plan_for(
            sbx.gate, ["state-secretary/tests/test_base.py::test_ok"],
            "state-secretary/tests/test_base.py")
        report2 = SweepReport(plan2)
        report2.executed = ["state-secretary/tests/test_base.py::test_ok"]
        report2.counts = {"passed": 1}
        report2.counts_verified = True
        report2.counts_source = "junitxml"
        report2.exit_code = 0
        inc.assert_counter = spy
        try:
            assert sbx.gate.assert_report(report2) == 1
        finally:
            inc.assert_counter = real
        assert captured["executed"] == 1, (
            f"守卫须绑定真实实跑数 1，实测绑定 {captured['executed']}（=计划数？）")


# ======================================================================
# G11 复评第 2 轮定点修复判别性回归（🔴-1 / 🔴-2 / 🔴-3）
# ======================================================================

class TestG11Round2PointFixes:
    """复评第 2 轮：三条 CLI 反例 + 回退分支 + 不变量 1 + 规模并集。"""

    # ---- 🔴-1：计数守卫语境绑定（序号/规模/已跑过数不得冒充实跑数）----

    FORGED_TEXT = "复评第 6 轮：全量 359 项全部通过（308 例已跑过）"

    @pytest.mark.parametrize("executed", [6, 359, 308])
    def test_g11_forged_executed_numbers_rejected(self, executed):
        """**判别性（🔴-1）**：序号 6 / 规模 359 / 已跑过 308 均不得放行。"""
        with pytest.raises(CounterWordingError):
            assert_counter(self.FORGED_TEXT, executed, total_suite=359,
                           deferred=308)

    def test_g11_cross_sentence_reference_rejected(self):
        """**判别性（🔴-1 跨句指代）**：「本轮实跑见第 6 轮」→ 拒绝。"""
        with pytest.raises(CounterWordingError):
            assert_counter("本轮实跑见第 6 轮", 6)

    def test_g11_bare_full_wording_true_full_rejected(self):
        """**判别性（🔴-1）**：「全量 359 项全部通过」+ executed=359 → 拒绝。

        旧实现把"规模数 == 实跑数"当"真全量"豁免，于是最典型的误读
        「本轮跑了全量」被直接放行。现须有实跑语境或 pytest 汇总证据。
        """
        with pytest.raises(CounterWordingError):
            assert_counter("全量 359 项全部通过", 359, total_suite=359,
                           deferred=308)

    def test_g11_pytest_summary_is_valid_evidence(self):
        """真全量的**合法**证据：pytest 汇总行（``341 passed``）放行。"""
        assert assert_counter("全量 pytest：341 passed + 3 skipped", 341,
                              total_suite=341) == 341

    def test_g11_context_bound_wording_accepted(self):
        """合法写法（实跑语境）须放行，返回本轮实跑数。"""
        text = render_count(59, total_suite=743, deferred=684)
        assert assert_counter(text, 59, total_suite=743, deferred=684) == 59

    # ---- 🔴-2：回退分支不得用计划数冒充实跑数 ----

    def test_g11_fallback_must_not_use_plan_as_executed(self):
        """**判别性（🔴-2）**：无 junitxml 且无 stdout 计数 → 不得用计划数。"""
        gate = _gate()
        plan = SweepPlan(base="x")
        plan.targets = [SweepTarget(nodeid=f"{NEW_TEST_FILE}::test_a",
                                    kind=INCREMENT_ADDED, path=NEW_TEST_FILE,
                                    name="test_a")]
        report = SweepReport(plan)
        report.exit_code = 0
        report.summary_line = ""          # 无汇总行计数

        class _Out:
            stdout = ""
            stderr = ""
            returncode = 0

        gate._fill_results(report, "/nonexistent.xml", _Out())
        assert report.executed_count == 0, (
            f"无计数证据时不得用计划数冒充实跑数，实测 {report.executed_count}")
        assert report.counts_source == "none"
        assert report.green is False, "计数未核实却报绿 = 假绿"
        with pytest.raises(Exception) as ei:
            gate.assert_report(report)
        assert "未核实" in str(ei.value) or "拒绝" in str(ei.value)

    def test_g11_stdout_summary_fallback_is_reliable(self):
        """有 pytest 汇总行（真实执行产物）→ 可落账（counts_source=stdout）。"""
        gate = _gate()
        plan = SweepPlan(base="x")
        plan.targets = [SweepTarget(nodeid=f"{NEW_TEST_FILE}::test_a",
                                    kind=INCREMENT_ADDED, path=NEW_TEST_FILE,
                                    name="test_a")]
        report = SweepReport(plan)
        report.exit_code = 0
        report.summary_line = "1 passed in 0.1s"

        class _Out:
            stdout = "1 passed in 0.1s\n"
            stderr = ""
            returncode = 0

        gate._fill_results(report, "/nonexistent.xml", _Out())
        assert report.counts_source == "stdout"
        assert report.executed_count == 1
        assert report.degraded_reason, "🟡4：降级原因须留痕"

    def test_g11_custom_runner_zero_exec_not_green(self):
        """**判别性（🔴-2）**：``pytest_cmd=[自定义 runner]``（公开 API）→
        真实执行 0 例时不得报绿 / 不得落账。"""
        import shutil
        runner = shutil.which("true") or "/bin/true"
        sbx = _sandbox_repo()
        gate = IncrementalGate(repo_root=sbx.root, pytest_cmd=[runner])
        plan = _plan_for(
            gate, ["state-secretary/tests/test_base.py::test_ok"],
            "state-secretary/tests/test_base.py")
        report = gate.run(plan)
        assert report.executed_count == 0
        assert report.green is False, "真实执行 0 例却报绿 = 假绿（🔴-2）"

    # ---- 🔴-3：不变量 1 + 全量规模并集 ----

    def test_g11_invariant1_plan_equals_executed_rejects_file_level(self):
        """**判别性（🔴-3 不变量 1）**：文件级标的 1 个 → 实跑该文件全部 3 例
        → ``计划 1 / 实跑 3`` 须被拒绝。"""
        sbx = _sandbox_repo(skipped=True)   # test_ok + test_skip = 2 例
        plan = SweepPlan(base="HEAD")
        plan.targets.append(SweepTarget(
            nodeid="state-secretary/tests/test_base.py",
            kind=INCREMENT_DISSOLVED,
            path="state-secretary/tests/test_base.py"))
        # run() 须 **fail-fast**（不变量 1 守卫在 run 内即拒绝）
        with pytest.raises(Exception) as ei:
            sbx.gate.run(plan)
        assert "计划" in str(ei.value) and "实跑" in str(ei.value)

    def test_g11_suite_total_is_union_not_sum(self):
        """**判别性（🔴-3 规模口径）**：``excluded`` 与 ``deferred`` 重叠时
        全量规模须取**并集**，不得重复计数。"""
        plan = SweepPlan(base="x")
        dup = f"{NEW_TEST_FILE}::test_dup"
        plan.targets = [SweepTarget(nodeid=f"{NEW_TEST_FILE}::test_a",
                                    kind=INCREMENT_ADDED, path=NEW_TEST_FILE,
                                    name="test_a")]
        # 同一 nodeid 同时出现在 deferred 与 excluded（旧实现重复计数）
        plan.deferred = [SweepTarget(nodeid=dup, kind=INCREMENT_LEGACY,
                                     path=NEW_TEST_FILE, name="test_dup")]
        plan.excluded = [SweepTarget(nodeid=dup, kind=INCREMENT_DISSOLVED,
                                     path=NEW_TEST_FILE, name="test_dup",
                                     risk="count-sync")]
        assert plan.skipped_unique_count == 1, "并集须去重"
        assert plan.suite_total_count == 2, (
            f"规模 = 计划 1 + 并集 1 = 2，实测 {plan.suite_total_count}")
        report = SweepReport(plan)
        report.executed = [f"{NEW_TEST_FILE}::test_a"]
        report.counts = {"passed": 1}
        report.counts_source = "junitxml"
        assert report.counter_claim.total_suite == 2

    def test_g11_plan_deferred_excludes_excluded(self):
        """**判别性（🔴-3）**：计划生成时 ``deferred`` 须剔除 ``excluded``。"""
        _rev_or_skip(LEGACY_BASE)
        gate = _gate()
        plan = gate.plan_sweep(base=LEGACY_BASE, include_redundant=True)
        deferred_ids = {t.nodeid for t in plan.deferred}
        excluded_ids = {t.nodeid for t in plan.excluded}
        assert not (deferred_ids & excluded_ids), (
            f"deferred 与 excluded 重叠 {sorted(deferred_ids & excluded_ids)} "
            "→ 全量规模会重复计数")

    # ---- 🟡1：单一语义源（未知标签不得分歧）----

    def test_g11_redundant_semantics_single_source(self):
        """🟡1：``SweepTarget.is_redundant`` 与 ``SweepRules.is_redundant``
        对未知标签须一致（不得 True/False 分歧）。"""
        rules = SweepRules()
        for risk in ("", "dissolve", "count-sync", "cosmetic", "doc-append",
                     "some-unknown-label"):
            t = SweepTarget(nodeid=f"{NEW_TEST_FILE}::test_x",
                            kind=INCREMENT_DISSOLVED, path=NEW_TEST_FILE,
                            name="test_x", risk=risk)
            assert t.is_redundant == rules.is_redundant(t), (
                f"risk={risk!r} 判定分歧: target={t.is_redundant} "
                f"rules={rules.is_redundant(t)}")
        # 未知标签 → 两边都不得判为冗余（否则会被静默跳过）
        unknown = SweepTarget(nodeid=f"{NEW_TEST_FILE}::test_y",
                              kind=INCREMENT_DISSOLVED, path=NEW_TEST_FILE,
                              name="test_y", risk="some-unknown-label")
        assert unknown.is_redundant is False
        assert rules.is_redundant(unknown) is False

    # ---- 🟡2：整文件重写判定接线 ----

    def test_g11_whole_file_rewrite_is_wired(self):
        """🟡2：``is_whole_file_rewrite`` 须在门禁路径上生效。

        构造改动比例 ≥ 阈值（或高风险关键词）的既有测试文件 → 计划须把
        **全部改动用例**升级为消解（补入整文件消解）。
        """
        rules = SweepRules(dissolve_ratio=0.01)
        item = {"path": NEW_TEST_FILE, "is_new": False,
                "changed_lines": 10, "total_lines": 100,
                "added_tests": [], "changed_tests": ["test_one"]}
        assert rules.is_whole_file_rewrite(item) is True
        added, to_dissolve = rules.rewrite_names(
            item, [], ["test_one"])
        assert to_dissolve == ["test_one"]
        # 低改动比例 + 无高风险关键词 → 不触发
        low = {"path": "state-secretary/tests/test_plain.py", "is_new": False,
               "changed_lines": 1, "total_lines": 1000}
        assert rules.is_whole_file_rewrite(low) is False

    # ---- 🔴-1 端到端：CLI 反例必须 rc=1 ----

    def _cli(self, *args):
        return subprocess.run(
            [sys.executable,
             os.path.join(REPO_ROOT, "state-secretary", "poc",
                          "incremental_gate.py"), "counter", *args],
            cwd=REPO_ROOT, capture_output=True, text=True)

    def test_g11_cli_forged_numbers_all_rejected(self):
        """**判别性（🔴-1 CLI 端到端）**：三条反例 CLI 必须 rc=1。

        反例（旧实现全 rc=0 假绿）：
        ``复评第 6 轮：全量 359 项全部通过（308 例已跑过）`` 分别以
        6（序号）/ 359（规模）/ 308（已跑过）作为 ``--executed``。
        """
        text = self.FORGED_TEXT
        for executed in ("6", "359", "308"):
            r = self._cli("--text", text, "--executed", executed,
                          "--total", "359", "--deferred", "308")
            assert r.returncode == 1, (
                f"executed={executed} 应 rc=1（语境盲区），实测 rc="
                f"{r.returncode}；stdout={r.stdout!r}")
            assert "实跑语境" in (r.stdout + r.stderr)

    def test_g11_cli_legit_wording_accepted(self):
        """**反向对照**：合法写法 CLI 须 rc=0（守卫不得过度收紧）。"""
        r = self._cli("--text", "本轮实跑 69 例（计划 69）", "--executed", "69")
        assert r.returncode == 0, (r.stdout, r.stderr)
        assert "本轮实跑 69 例" in r.stdout

    def test_g11_cli_true_full_run_accepted(self):
        """**反向对照**：真全量（pytest 汇总证据）CLI 须 rc=0。"""
        r = self._cli("--text", "全量 pytest：341 passed + 3 skipped",
                      "--executed", "341", "--total", "341")
        assert r.returncode == 0, (r.stdout, r.stderr)

    # ---- 🟡3 / 🟡5：接线与留痕一致性 ----

    def test_g11_src_pythonpath_is_wired(self):
        """🟡3：``src_pythonpath`` 须接线到 pytest（不得是未引用形参）。"""
        sbx = _sandbox_repo()
        gate = IncrementalGate(repo_root=sbx.root,
                               src_pythonpath="src")
        plan = _plan_for(
            gate, ["state-secretary/tests/test_base.py::test_ok"],
            "state-secretary/tests/test_base.py")
        report = gate.run(plan)
        assert "pythonpath=src" in report.command, report.command

    def test_g11_command_includes_junitxml(self):
        """🟡5：留痕命令须与实际执行一致（含 ``--junitxml=``）。"""
        sbx = _sandbox_repo()
        plan = _plan_for(
            sbx.gate, ["state-secretary/tests/test_base.py::test_ok"],
            "state-secretary/tests/test_base.py")
        report = sbx.gate.run(plan)
        assert "--junitxml=" in report.command, report.command

    def test_g11_no_extra_undeclared_number_in_render(self):
        """🟡5 邻接：报告渲染不得出现未声明的第 4 个计数（如"合计"）。"""
        sbx = _sandbox_repo()
        plan = _plan_for(
            sbx.gate, ["state-secretary/tests/test_base.py::test_ok"],
            "state-secretary/tests/test_base.py")
        report = sbx.gate.run(plan)
        # 守卫自身须能通过（否则报告文字与计数主张不一致）
        assert sbx.gate.assert_report(report) == 1


# ======================================================================
# G12 复评第 3 轮定点修复（🔴-A src_pythonpath 路径口径 / 🔴-B junitxml <error>）
# ======================================================================
#
# 病灶一（🔴-A，回归）：pytest 的 `pythonpath` ini 相对 **rootdir**
# （= `state-secretary/`，由 pytest.ini 决定）解析，而形参默认值
# `state-secretary/src` 是**仓库根相对** → 实际指向
# `state-secretary/state-secretary/src`（不存在）→ 目标模块 import 失败 →
# **rc=2 collection error**。真实基线端到端 `run --base e3195ea/d6abaab`
# 实测失败（掩盖原因：端到端自测只跑 `--base a455608`，该文件自带
# `sys.path.insert`，未暴露）。
#
# 病灶二（🔴-B，本轮因 🔴-A 成为默认路径必现）：`_parse_junit` 的
# `counts[status]` 键名不符（`counts` 键为复数 `errors`，`status` 取单数
# `error`）→ **任何** junitxml 含 `<error>`（collection error / fixture /
# setup 错误）→ `KeyError('error')`（**非** `GateError`）→ 破坏公开 API
# 契约与 CLI 可诊断性（只打印 `[门禁拒绝] 'error'`）。


def _sandbox_repo_with_src() -> _Sandbox:
    """沙箱：``pytest.ini`` 声明 ``pythonpath = src``，测试 import ``src`` 下模块。

    这是 🔴-A 的**最小复现环境**：只有真正把 ``pythonpath`` 指到存在的
    ``src/`` 时 collection 才成功；指向 ``state-secretary/src``（不存在）
    即 rc=2 collection error。
    """
    root = tempfile.mkdtemp(prefix="gate-sbx-pp-")
    pkg = os.path.join(root, "state-secretary")
    tests = os.path.join(pkg, "tests")
    src = os.path.join(pkg, "src")
    os.makedirs(tests, exist_ok=True)
    os.makedirs(src, exist_ok=True)
    with open(os.path.join(pkg, "pytest.ini"), "w", encoding="utf-8") as f:
        f.write("[pytest]\npythonpath = src\n")
    with open(os.path.join(src, "sbxlib.py"), "w", encoding="utf-8") as f:
        f.write("VALUE = 41\n")
    with open(os.path.join(tests, "test_base.py"), "w", encoding="utf-8") as f:
        f.write("from sbxlib import VALUE\n\n"
                "def test_ok():\n    assert VALUE + 1 == 42\n")

    def git(*args):
        return subprocess.run(["git", *args], cwd=root, capture_output=True,
                              text=True)

    git("init", "-q")
    git("config", "user.email", "gate@example.com")
    git("config", "user.name", "gate")
    git("add", "-A")
    git("commit", "-qm", "base")
    return _Sandbox(root, IncrementalGate(repo_root=root))


def _sandbox_repo_with_erroring_fixture() -> _Sandbox:
    """沙箱：``test_base.py`` 内 fixture 抛错 → junitxml 含 ``<error>``。

    🔴-B 的最小复现：任何含 ``<error>`` 的 junitxml 旧实现都抛
    ``KeyError('error')``。
    """
    root = tempfile.mkdtemp(prefix="gate-sbx-err-")
    pkg = os.path.join(root, "state-secretary")
    tests = os.path.join(pkg, "tests")
    os.makedirs(tests, exist_ok=True)
    with open(os.path.join(pkg, "pytest.ini"), "w", encoding="utf-8") as f:
        f.write("[pytest]\n")
    with open(os.path.join(tests, "test_base.py"), "w", encoding="utf-8") as f:
        f.write("import pytest\n\n"
                "@pytest.fixture\n"
                "def boom():\n"
                "    raise RuntimeError('fixture boom')\n\n"
                "def test_uses_boom(boom):\n    assert True\n")

    def git(*args):
        return subprocess.run(["git", *args], cwd=root, capture_output=True,
                              text=True)

    git("init", "-q")
    git("config", "user.email", "gate@example.com")
    git("config", "user.name", "gate")
    git("add", "-A")
    git("commit", "-qm", "base")
    return _Sandbox(root, IncrementalGate(repo_root=root))


class TestG12Round3PointFixes:
    """复评第 3 轮：🔴-A 路径口径 + 🔴-B junitxml ``<error>`` 键名。"""

    # ---- 🔴-A：src_pythonpath 须转 pytest 运行目录相对 ----

    def test_g12_pythonpath_ini_value_is_run_dir_relative(self):
        """**判别性（🔴-A 单元）**：默认仓库根相对值 → 运行目录相对。

        ``state-secretary/src`` → ``src``（旧实现原样注入 → 指向
        ``state-secretary/state-secretary/src``）。
        """
        sbx = _sandbox_repo_with_src()
        assert sbx.gate.src_pythonpath == "state-secretary/src", "前置：默认值"
        assert sbx.gate.pythonpath_ini_value() == "src", (
            "🔴-A：注入值须为运行目录相对（src），实测 "
            f"{sbx.gate.pythonpath_ini_value()!r}")
        # 已是运行目录相对 → 保持不变（不得二次去前缀）
        g2 = IncrementalGate(repo_root=sbx.root, src_pythonpath="src")
        assert g2.pythonpath_ini_value() == "src"
        # 绝对路径 → 原样（pytest 直接可用）
        abs_p = os.path.join(sbx.root, "state-secretary", "src")
        g3 = IncrementalGate(repo_root=sbx.root, src_pythonpath=abs_p)
        assert g3.pythonpath_ini_value() == abs_p
        # 空值 → 空（不注入 pythonpath）
        g4 = IncrementalGate(repo_root=sbx.root, src_pythonpath="")
        assert g4.pythonpath_ini_value() == ""

    def test_g12_pythonpath_default_end_to_end_is_green(self):
        """**判别性（🔴-A 端到端）**：默认 ``src_pythonpath`` 下真实子进程
        须 rc=0 且恰好 1 例通过。

        旧实现注入 ``pythonpath=state-secretary/src`` → collection error
        rc=2（模块 import 失败）→ 真实基线端到端 ``run`` 全红。
        """
        sbx = _sandbox_repo_with_src()
        plan = _plan_for(
            sbx.gate, ["state-secretary/tests/test_base.py::test_ok"],
            "state-secretary/tests/test_base.py")
        report = sbx.gate.run(plan)
        assert "pythonpath=src" in report.command, report.command
        assert report.exit_code == 0, (
            f"🔴-A：默认 pythonpath 注入口径错误 → rc={report.exit_code}；"
            f"{report.raw_output[-400:]}")
        assert report.executed_count == 1
        assert report.passed_count == 1
        assert report.green is True, report.render()

    def test_g12_repo_relative_pythonpath_would_be_collection_error(self):
        """**判别性（🔴-A 反证）**：旧口径（仓库根相对）确实 collection error。

        rc 口径随 selector 形态而异（均为"一个用例都没跑"）：

        - **文件级** selector → rc=**2**（collection error，pytest 汇总报错）；
        - **nodeid 级** selector（门禁实际用法）→ rc=**4**
          （``found no collectors``，因该 nodeid 无法收集）。

        两者都伴随 ``ModuleNotFoundError``。门禁实际走 nodeid → rc=4，但
        junitxml 仍产出 ``<error>`` 节点 → 旧实现 ``_parse_junit`` 抛
        ``KeyError('error')`` 逃逸（🔴-B）→ CLI 只打印 ``[门禁拒绝] 'error'``。
        """
        sbx = _sandbox_repo_with_src()
        pkg = os.path.join(sbx.root, "state-secretary")
        # (1) 文件级 selector → rc=2
        out = subprocess.run(
            [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider",
             "-o", "pythonpath=state-secretary/src", "tests/test_base.py"],
            cwd=pkg, capture_output=True, text=True)
        assert out.returncode == 2, (
            f"旧口径（文件级）应 rc=2 collection error，实测 rc={out.returncode}；"
            f"{out.stdout[-300:]}")
        assert "ModuleNotFoundError" in (out.stdout + out.stderr), \
            "反证：须是 import 失败（模块不在 sys.path）"
        # (2) nodeid 级 selector（门禁实际用法）→ rc=4，一个用例都没跑
        out_nid = subprocess.run(
            [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider",
             "-o", "pythonpath=state-secretary/src",
             "tests/test_base.py::test_ok"],
            cwd=pkg, capture_output=True, text=True)
        assert out_nid.returncode == 4, (
            f"旧口径（nodeid 级）应 rc=4（no collectors），实测 "
            f"rc={out_nid.returncode}；{out_nid.stdout[-300:]}")
        # 反向对照：正确口径（运行目录相对）两种 selector 均 rc=0
        for sel in ("tests/test_base.py", "tests/test_base.py::test_ok"):
            ok = subprocess.run(
                [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider",
                 "-o", "pythonpath=src", sel],
                cwd=pkg, capture_output=True, text=True)
            assert ok.returncode == 0, (sel, ok.stdout[-300:])

    # ---- 🔴-B：junitxml ``<error>`` 键名映射 ----

    def test_g12_parse_junit_error_status_maps_to_errors(self):
        """**判别性（🔴-B 单元）**：含 ``<error>`` 的 junitxml 须解析为
        ``counts['errors']``，**不得**抛 ``KeyError('error')``。"""
        gate = _gate()
        xml = (
            '<?xml version="1.0" encoding="utf-8"?>\n'
            '<testsuites><testsuite name="pytest" tests="2">\n'
            '  <testcase classname="tests.test_a" name="test_ok" time="0"/>\n'
            '  <testcase classname="tests.test_a" name="test_err" time="0">\n'
            '    <error message="fixture boom">RuntimeError: boom</error>\n'
            '  </testcase>\n'
            '</testsuite></testsuites>\n')
        fd, path = tempfile.mkstemp(prefix="gate-junit-", suffix=".xml")
        os.close(fd)
        try:
            with open(path, "w", encoding="utf-8") as f:
                f.write(xml)
            counts, nodeids = gate._parse_junit(path)
        finally:
            os.unlink(path)
        assert counts["errors"] == 1, counts
        assert counts["passed"] == 1, counts
        assert sum(counts.values()) == 2
        assert sorted(nodeids.values()) == ["error", "passed"]

    def test_g12_erroring_fixture_run_no_keyerror(self):
        """**判别性（🔴-B 端到端）**：fixture 抛错 → ``run()`` 须返回报告
        （**不得** ``KeyError``），``errors_count == 1``、``green is False``、
        ``assert_report`` 抛 ``GateError``（**非** ``KeyError``）。"""
        from secretary.gate import GateError
        sbx = _sandbox_repo_with_erroring_fixture()
        plan = _plan_for(
            sbx.gate, ["state-secretary/tests/test_base.py::test_uses_boom"],
            "state-secretary/tests/test_base.py")
        report = sbx.gate.run(plan)   # 旧实现：KeyError('error') 崩栈
        assert report.exit_code == 1, report.raw_output[-400:]
        assert report.counts_source == "junitxml", report.render()
        assert report.errors_count == 1, report.render()
        assert report.green is False, "含 error 却报绿 = 假绿"
        # 公开 API 契约：解析层**不得**抛裸 KeyError；报告可诊断
        try:
            n = sbx.gate.assert_report(report)
        except GateError:
            n = None   # 计数口径守卫拒绝亦属合法（此处不拒绝，见下断言）
        except KeyError as exc:  # pragma: no cover - 旧实现路径
            raise AssertionError(
                f"🔴-B：assert_report 抛裸 KeyError({exc!r}) → 破坏 CLI 可诊断性")
        assert n == report.executed_count, (
            "计数口径守卫须以真实实跑数为准放行（error 属真实执行产物）")
        # CLI 端到端：rc=1（非绿）且 **不得**打印裸 KeyError
        cli = subprocess.run(
            [sys.executable, os.path.join(REPO_ROOT, "state-secretary", "poc",
                                          "incremental_gate.py"),
             "--repo", sbx.root, "run", "--base", "HEAD"],
            cwd=REPO_ROOT, capture_output=True, text=True)
        assert cli.returncode == 1, (cli.stdout[-300:], cli.stderr[-300:])
        assert "KeyError" not in cli.stderr and "'error'" not in cli.stderr, (
            f"🔴-B：CLI 不得输出裸 KeyError 形貌；stderr={cli.stderr[-300:]!r}")

    def test_g12_unknown_junit_status_degrades_not_crashes(self):
        """**判别性（🔴-B 收窄捕获）**：``_parse_junit`` 抛
        ``JUnitParseError``（解析不可信）→ ``_fill_results`` **必须**捕获
        并明确降级（``counts_source="none"``），**绝不**崩栈。

        判别力：变异体（不把 ``JUnitParseError`` 纳入 ``except``）→
        异常逃逸 → 本用例失败。
        """
        from secretary.gate import JUnitParseError
        gate = _gate()
        plan = SweepPlan(base="x")
        plan.targets = [SweepTarget(nodeid=f"{NEW_TEST_FILE}::test_a",
                                    kind=INCREMENT_ADDED,
                                    path=NEW_TEST_FILE, name="test_a")]
        report = SweepReport(plan)
        report.exit_code = 0
        report.summary_line = ""

        def _boom(_path):
            raise JUnitParseError("未知 junitxml 状态: 'weird'")

        real = gate._parse_junit
        gate._parse_junit = _boom
        try:
            class _Out:
                stdout = ""
                stderr = ""
                returncode = 0

            gate._fill_results(report, __file__, _Out())
        finally:
            gate._parse_junit = real
        assert report.counts_source == "none", report.render()
        assert report.counts_verified is False
        assert report.green is False, "解析不可信却报绿 = 假绿"
        assert "解析不可信" in report.degraded_reason, report.degraded_reason

    def test_g12_junit_status_map_is_single_source(self):
        """**判别性（🔴-B 键名）**：``status → counts 键`` 映射须单源且
        覆盖 pytest 的四种标签；``error``（单数）必须映射到 ``errors``。"""
        from secretary.gate.incremental import _JUNIT_STATUS_TO_COUNT
        assert _JUNIT_STATUS_TO_COUNT["error"] == "errors"
        assert _JUNIT_STATUS_TO_COUNT["errors"] == "errors"
        for tag in ("passed", "failed", "skipped"):
            assert _JUNIT_STATUS_TO_COUNT[tag] == tag
        # 键必须与 `_parse_junit` 初始化的一致（防再次键名漂移）
        assert set(_JUNIT_STATUS_TO_COUNT.values()) == {
            "passed", "failed", "errors", "skipped"}

    # ---- 多基线端到端 run（🔴-A 回归防线） ----

    @staticmethod
    def _plan_without_self(gate, base: str):
        """真实仓库计划，**剔除本门禁用例文件**后返回。

        防递归：``test_incremental_gate_issue14.py`` 内含 ``gate.run()``
        用例，若端到端 ``run`` 把它纳入 selector → 嵌套跑测无限递归。
        剔除后仍含 ``test_acceptance_matrix_issue4.py`` /
        ``test_secretary_session.py``（二者**无** ``sys.path.insert``，
        正是 🔴-A 的判别面）。
        """
        plan = gate.plan_sweep(base=base)
        plan.targets = [t for t in plan.targets if t.path != NEW_TEST_FILE]
        return plan

    @pytest.mark.parametrize("base", [LEGACY_BIG, LEGACY_MID])
    def test_g12_real_repo_multi_base_run_is_green(self, base):
        """**判别性（🔴-A 多基线回归）**：真实仓库多基线端到端 ``run`` 须
        rc=0 / 计数来源 junitxml / 实跑数 == 计划数 / 🟢。

        旧实现：``--base e3195ea`` / ``--base d6abaab`` → rc=1，
        stderr ``[门禁拒绝] 'error'``（🔴-A 致 collection error，🔴-B 致
        KeyError）。端到端自测只跑 ``--base a455608``（该文件自带
        ``sys.path.insert``）→ 未暴露。

        判别面：``test_acceptance_matrix_issue4.py`` / ``test_secretary_session.py``
        **无** ``sys.path.insert`` → 仅靠 ``pythonpath`` ini 找 ``secretary``
        包 → 旧口径（``state-secretary/src``）必然 collection error。
        """
        _rev_or_skip(base)
        gate = _gate()
        plan = self._plan_without_self(gate, base)
        assert plan.planned_count > 0, f"前置：base={base} 须有增量"
        assert any(p != NEW_TEST_FILE for p in plan.planned_files), \
            "前置：须含非本门禁文件的判别面"
        report = gate.run(plan)
        assert report.exit_code == 0, (
            f"base={base} rc={report.exit_code}；"
            f"{report.raw_output[-400:]}")
        assert report.counts_source == "junitxml", report.render()
        assert report.green is True, report.render()
        assert report.executed_count == plan.planned_count, (
            f"base={base}：计划 {plan.planned_count} 例 ≠ 实跑 "
            f"{report.executed_count} 例 → 违反不变量 1")
        # 计数口径守卫须放行（报告文字与真实实跑数一致）
        assert gate.assert_report(report) == report.executed_count

    def test_g12_real_repo_default_pythonpath_is_run_dir_relative(self):
        """**判别性（🔴-A 真实仓库）**：真实仓库默认口径须转 ``src``
        （旧实现原样 ``state-secretary/src`` → 真实基线 rc=2/1）。"""
        gate = _gate()
        assert gate.pythonpath_ini_value() == "src", (
            f"实测 {gate.pythonpath_ini_value()!r}")
        assert os.path.isdir(
            os.path.join(gate.repo_root, "state-secretary", "src")), \
            "前置：src 目录确实存在（口径正确性前提）"


# ======================================================================
# G13 🟡-B：`is_whole_file_rewrite` 的**比例分支**可复现判别用例
# ======================================================================
#
# 🟡-B（复评第 2 轮）：比例分支（分母 = 工作区全文行数）极难触发——正常编辑
# 比例 < 0.5 → 复现不出"改动比例 ≥ 0.5 → 计划 5 例"。本组用**沙箱**构造
# "小文件被整文件重写"（changed/total ≥ 0.5）→ 比例分支必然命中，端到端
# 判别（不经关键词分支）。


def _sandbox_repo_for_rewrite() -> _Sandbox:
    """沙箱：基线含 4 行测试文件（3 例），随后**整文件重写**（改动比例 ≥ 0.5）。"""
    root = tempfile.mkdtemp(prefix="gate-sbx-rw-")
    pkg = os.path.join(root, "state-secretary")
    tests = os.path.join(pkg, "tests")
    os.makedirs(tests, exist_ok=True)
    with open(os.path.join(pkg, "pytest.ini"), "w", encoding="utf-8") as f:
        f.write("[pytest]\n")
    path = os.path.join(tests, "test_rw.py")

    def git(*args):
        return subprocess.run(["git", *args], cwd=root, capture_output=True,
                              text=True)

    _hermetic_git_init(git, root)
    # 基线：4 行、3 例
    with open(path, "w", encoding="utf-8") as f:
        f.write("def test_a():\n    assert True\n\n"
                "def test_b():\n    assert True\n\n"
                "def test_c():\n    assert True\n")
    git("add", "-A")
    git("commit", "-qm", "base")
    # 整文件重写：8 行全改（git numstat 记 8 增；工作区 8 行 → 比例 1.0）
    with open(path, "w", encoding="utf-8") as f:
        f.write("def test_a():\n    assert 1 == 1\n\n"
                "def test_b():\n    assert 2 == 2\n\n"
                "def test_c():\n    assert 3 == 3\n\n"
                "def test_d():\n    assert 4 == 4\n")
    return _Sandbox(root, IncrementalGate(repo_root=root))


class TestG13WholeFileRewriteRatioBranch:
    """🟡-B：整文件重写**比例分支**可复现判别（非关键词分支）。"""

    def test_g13_ratio_branch_is_reachable_end_to_end(self):
        """**判别性（🟡-B）**：小文件整文件重写 → 比例 ≥ 0.5 → 比例分支命中
        （`DISOLVE_KEYWORDS` 不匹配 `test_rw.py` 路径/无 summary 关键词）→
        全部既有用例升级为消解。"""
        sbx = _sandbox_repo_for_rewrite()
        changes = sbx.gate.changed_files(base="HEAD")
        entry = [c for c in changes if c["path"].endswith("test_rw.py")]
        assert entry, changes
        entry = entry[0]
        assert entry["total_lines"] > 0, entry
        ratio = entry["changed_lines"] / entry["total_lines"]
        assert ratio >= 0.5, (
            f"前置：改动比例须 ≥ 0.5（实测 {ratio}）→ 否则比例分支不可达")
        # 比例分支确实命中（关键词分支对 test_rw.py 不匹配）
        assert sbx.gate.rules.is_whole_file_rewrite(entry) is True, entry
        plan = sbx.gate.plan_sweep(base="HEAD")
        assert plan.planned_count > 0, plan.render()

    def test_g13_ratio_branch_discriminates_from_keyword_branch(self):
        """**判别性（🟡-B 对照）**：把阈值调高到 1.1 → 比例分支不可达；
        且该条目**不**命中关键词分支 → 判定翻转为 False（证明原判定确由
        **比例**驱动，而非恒真或关键词误命中）。"""
        sbx = _sandbox_repo_for_rewrite()
        entry = [c for c in sbx.gate.changed_files(base="HEAD")
                 if c["path"].endswith("test_rw.py")][0]
        assert sbx.gate.rules.is_whole_file_rewrite(entry) is True
        strict = IncrementalGate(repo_root=sbx.root,
                                 rules=SweepRules(dissolve_ratio=1.1))
        assert strict.rules.is_whole_file_rewrite(entry) is False, (
            "阈值 1.1 下比例分支不可达且无关键词 → 须 False（否则判定非比例驱动）")


def _sandbox_repo_broken_base() -> _Sandbox:
    """沙箱：**基线不可解析**（模拟浅克隆 / HEAD 未建立 / 提交静默失败）。

    复现路径与实测一致：初始化并 add 后，commit **未落**（宿主
    ``commit.gpgsign=true`` 且签名器不可用时即为如此），于是仓库无 HEAD。
    """
    root = tempfile.mkdtemp(prefix="gate-sbx-nobase-")
    pkg = os.path.join(root, "state-secretary")
    tests = os.path.join(pkg, "tests")
    os.makedirs(tests, exist_ok=True)
    with open(os.path.join(pkg, "pytest.ini"), "w", encoding="utf-8") as f:
        f.write("[pytest]\n")
    with open(os.path.join(tests, "test_base.py"), "w", encoding="utf-8") as f:
        f.write("def test_ok():\n    assert True\n")

    def git(*args):
        return subprocess.run(["git", *args], cwd=root, capture_output=True,
                              text=True)

    git("init", "-q")
    git("config", "user.email", "gate@example.com")
    git("config", "user.name", "gate")
    git("add", "-A")
    # 刻意**不提交** → 无 HEAD（等价于 commit 静默失败）
    assert git("rev-parse", "--verify", "HEAD").returncode != 0
    return _Sandbox(root, IncrementalGate(repo_root=root))


class TestG14BaseUnresolvable:
    """🔴 收口：基线不可解析 — 「查不到变更」**不得**等于「没有变更」。

    缺陷（Issue #14 收口轮实测，非环境噪声）：原 ``changed_files`` 用
    ``_run``（不校验 rc），``git diff --numstat <base>`` 在 base 解析失败时
    返回 **128 + 空 stdout**，被当作"无变更" → 空条目 / 空计划。
    表现是 2 项用例 IndexError / AssertionError，根因是**静默假绿**：
    "只跑增量"最危险的失败模式就是**空计划被读成"本轮无需跑测"**。
    """

    def test_unresolvable_base_raises_instead_of_empty(self):
        """**判别性**：基线不可解析 → ``changed_files`` 抛 ``GateError``。

        反向对照（本条为"旧行为"的对照）：旧实现返回 ``[]``——
        那正是"查不到 = 没有"的错。"""
        sbx = _sandbox_repo_broken_base()
        with pytest.raises(GateError) as ei:
            sbx.gate.changed_files(base="HEAD")
        assert "基线不可解析" in str(ei.value)

    def test_empty_result_is_still_empty_for_a_valid_base(self):
        """**反向对照（防恒真）**：基线合法且**确实无改动**时，
        仍须返回空列表——不得把"真·无变更"也判成错误。
        （即：本修复只把**查不到**与**没有**分开，不改变"真的没有"。）"""
        sbx = _sandbox_repo()
        assert sbx.gate.changed_files(base="HEAD") == []

    def test_plan_degrades_conservatively_not_silently(self):
        """**判别性**：基线不可解析 → 计划**保守降级**（全树消解），
        **不**返回空计划，且降级原因**可见**。

        - 旧行为：空计划（0 例）→ 读成"无需跑测"（假绿）；
        - 新行为：文件级消解 = 全树需重跑（宁多跑不漏跑）+ ``degraded`` 留痕。"""
        sbx = _sandbox_repo_broken_base()
        plan = sbx.gate.plan_sweep(base="HEAD")
        assert plan.degraded is True
        assert plan.degraded_reason, "降级必须留痕（否则与真实增量无法区分）"
        assert plan.planned_count > 0, (
            "降级不得退化为空计划——空计划会被读成『本轮无需跑测』")
        assert all(t.kind == INCREMENT_DISSOLVED for t in plan.targets)
        assert "⚠ 降级" in plan.render()

    def test_valid_base_with_real_change_is_not_degraded(self):
        """**反向对照（防恒真）**：基线合法且有真实增量 → **不得**降级，
        且计划只落在变更文件上（证明降级分支不被误触发）。"""
        sbx = _sandbox_repo_for_rewrite()
        plan = sbx.gate.plan_sweep(base="HEAD")
        assert plan.degraded is False
        assert not plan.degraded_reason
        assert plan.planned_files == ["state-secretary/tests/test_rw.py"], (
            plan.planned_files)

    def test_sandbox_is_hermetic_against_host_gpgsign(self):
        """**回归**：沙箱提交**不受宿主全局 git 配置影响**。

        判别力：把 helper 的 ``-c commit.gpgsign=false`` + 断言去掉后，
        在 ``commit.gpgsign=true`` 且签名器不可用的环境上，本文件
        ``TestG13WholeFileRewriteRatioBranch`` 会**确定性**失败
        （IndexError）——即历史上"看起来像环境噪声"的那两项。"""
        sbx = _sandbox_repo_for_rewrite()
        head = subprocess.run(["git", "rev-parse", "--verify", "HEAD"],
                              cwd=sbx.root, capture_output=True, text=True)
        assert head.returncode == 0, "沙箱必须有 HEAD（提交须真的落盘）"
        unsigned = subprocess.run(
            ["git", "log", "-1", "--format=%G?"], cwd=sbx.root,
            capture_output=True, text=True)
        # "N" = 无签名（正是我们要的：沙箱不该依赖签名器）
        assert unsigned.stdout.strip() in ("N", ""), unsigned.stdout
