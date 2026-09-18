"""Issue #14 本次追问回归：**只跑增量、不连跑同一口径**（让口径变成机制）

用户原话：
  > 平时不需要跑全量，而且尤其是不适合连跑三次，这也没意义，我们平时只需要跑增量的部分…
  > 连续跑三次，有问题也可能查不出来，因为跑的方式是固定的。同一个口径几乎不会产生分歧。
  > 所以像这种连续跑三次全量是无效的。…为什么没事按那个规则呀，是规则不健全吗？还是配置有问题？

拆成两条**可执行**口径（不再是"提醒"，而是判定）：
  ① 默认增量：日常一律 ``scan``，``full`` 只在大版本；
  ② 同口径不重复：``(范围, 版本, 受影响面, 命令)`` 相同 → 第 2 次起 ``skip_duplicate``；
     「连跑 N 次同一命令」当稳定性论证 → ``blocked_stability`` + 替代口径。

判别性设计（关键断言配**反向对照**，防恒真）：
  - 判重**只认四元组**：改版本 / 改受影响面 / 改命令 任一 → 必须**放行**
    （反向：时间戳、执行人这类非口径字段**不得**进指纹）；
  - 「上一轮不绿可重跑」是**例外**，不是"允许连跑"的理由：
    红→重跑（1 次）放行，但 ``window_repeats=3`` 无论红绿都 ``blocked_stability``；
  - 口径信息不全（缺版本/缺命令）→ **不拦**（反向：台账损坏也当空历史，不挡住干活）；
  - 受影响面按**集合**语义：同一组元素换顺序 → **同一口径**（不得被当成新口径）。
"""
from __future__ import annotations

import os
import tempfile

import pytest

from secretary.collab import (
    BLOCKED_STABILITY,
    RUN,
    SKIP_DUPLICATE,
    CollabRules,
    RunScopeGuard,
    load_rules,
)
from secretary.session.session import SecretarySession

CMD = "pytest -q tests/test_a.py"
KW = dict(mode="scan", revision="abc123", impact=["src/a.py", "src/b.py"],
          command=CMD)


@pytest.fixture()
def rules() -> CollabRules:
    return load_rules(search_from=os.path.dirname(os.path.dirname(__file__)))


@pytest.fixture()
def guard(rules: CollabRules) -> RunScopeGuard:
    return RunScopeGuard(rules)


# ----------------------------------------------------------------------
# ① 规则包：默认增量 + 同口径上界（配置侧）
# ----------------------------------------------------------------------

class TestRulePackageCarriesScopePolicy:
    def test_default_mode_is_scan(self, rules: CollabRules) -> None:
        """默认档位必须是增量——不需要有人"记得"去选增量。"""
        assert rules.scan_default_mode == "scan"

    def test_max_identical_repeats_is_one(self, rules: CollabRules) -> None:
        """同一口径只跑一次（用户：'同一个口径几乎不会产生分歧'）。"""
        assert rules.max_identical_repeats == 1

    def test_stability_repeats_disallowed(self, rules: CollabRules) -> None:
        """连跑同一命令当稳定性论证：默认**禁止**。"""
        assert rules.allow_stability_repeats is False

    def test_stability_alternatives_not_empty(self, rules: CollabRules) -> None:
        """禁止之余必须给替代路径，否则规则只是"不许"，没法落地。"""
        assert rules.stability_via
        assert "targeted_probe" in rules.stability_via

    def test_scan_policy_surfaces_in_dict(self, rules: CollabRules) -> None:
        scan = rules.to_dict()["scan"]
        for k in ("default_mode", "max_identical_repeats",
                  "repeat_rerun_on_failure", "repeat_keys",
                  "allow_stability_repeats", "stability_via"):
            assert k in scan, f"规则包 to_dict 缺 {k}"

    def test_repeat_keys_are_scope_only(self, rules: CollabRules) -> None:
        """指纹字段必须**只有**口径相关项（不得混入时间戳/执行人）。"""
        assert set(rules.repeat_keys) == {"mode", "revision", "impact", "command"}


# ----------------------------------------------------------------------
# ② 同一口径：第 1 次放行、第 2 次起跳过
# ----------------------------------------------------------------------

class TestIdenticalScopeIsNotRerun:
    def test_first_run_allowed(self, guard: RunScopeGuard) -> None:
        d = guard.decide(**KW)
        assert d.action == RUN and d.should_run

    def test_second_run_skipped_after_green(self, guard: RunScopeGuard) -> None:
        """连跑第 2 次（上轮为绿）→ 跳过。这正是"连跑三次很无效"的机制化。"""
        guard.record(**KW, verdict="577 passed")
        d = guard.decide(**KW, last_verdict="577 passed")
        assert d.action == SKIP_DUPLICATE and d.skipped
        assert d.prior_count == 1

    def test_third_run_also_skipped(self, guard: RunScopeGuard) -> None:
        """第 3 次仍跳过（不是"允许再来一次"）。"""
        guard.record(**KW, verdict="577 passed")
        d = guard.decide(**KW, last_verdict="577 passed")
        assert d.action == SKIP_DUPLICATE

    def test_skipped_run_does_not_count(self, guard: RunScopeGuard) -> None:
        """被跳过的重复**不计数**——否则跳过也涨计数，台账失真。"""
        guard.record(**KW, verdict="green")
        guard.decide(**KW, last_verdict="green")
        guard.decide(**KW, last_verdict="green")
        assert guard.repeats_of(**KW) == 1

    # --- 反向对照：任一"口径字段"变了 → 必须放行 ---

    def test_new_revision_runs(self, guard: RunScopeGuard) -> None:
        """版本变了 = 新口径 → 放行（否则新提交跑不动，规则就成了死锁）。"""
        guard.record(**KW, verdict="green")
        assert guard.decide(revision="def456", mode="scan",
                            impact=KW["impact"], command=CMD,
                            last_verdict="green").action == RUN

    def test_new_command_runs(self, guard: RunScopeGuard) -> None:
        """命令变了（如只跑受影响用例）→ 新口径 → 放行。"""
        guard.record(**KW, verdict="green")
        assert guard.decide(mode="scan", revision="abc123", impact=KW["impact"],
                            command="pytest -q tests/test_b.py",
                            last_verdict="green").action == RUN

    def test_new_impact_runs(self, guard: RunScopeGuard) -> None:
        """受影响面变了 → 新口径 → 放行。"""
        guard.record(**KW, verdict="green")
        assert guard.decide(mode="scan", revision="abc123", impact=["src/c.py"],
                            command=CMD, last_verdict="green").action == RUN

    # --- 反向对照：受影响面是**集合**语义 ---

    def test_impact_order_irrelevant(self, guard: RunScopeGuard) -> None:
        """同一组受影响面换顺序 → **同一口径**（不得被当成新口径钻空子）。"""
        guard.record(mode="scan", revision="abc123",
                     impact=["src/a.py", "src/b.py"], command=CMD, verdict="green")
        d = guard.decide(mode="scan", revision="abc123",
                         impact=["src/b.py", "src/a.py"], command=CMD,
                         last_verdict="green")
        assert d.action == SKIP_DUPLICATE

    def test_impact_duplicates_collapse(self, guard: RunScopeGuard) -> None:
        """受影响面重复项折叠 → 仍是同一口径。"""
        guard.record(mode="scan", revision="abc123",
                     impact=["src/a.py"], command=CMD, verdict="green")
        d = guard.decide(mode="scan", revision="abc123",
                         impact=["src/a.py", "src/a.py"], command=CMD,
                         last_verdict="green")
        assert d.action == SKIP_DUPLICATE


# ----------------------------------------------------------------------
# ③ 「上一轮不绿」是例外，不是"允许连跑"的理由
# ----------------------------------------------------------------------

class TestRedIsExceptionNotLicenseToRepeat:
    def test_rerun_after_red_allowed(self, guard: RunScopeGuard) -> None:
        """红→修→再跑是**有新信息**的，允许一次重跑。"""
        guard.record(**KW, verdict="2 failed")
        assert guard.decide(**KW, last_verdict="2 failed").action == RUN

    def test_no_verdict_does_not_license_rerun(self, guard: RunScopeGuard) -> None:
        """**没给结论 ≠ 跑红了**：不得据此放行重跑（否则判重形同虚设）。

        这是本轮抓到的真缺陷：原实现用 `not _is_green(...)` 判"上一轮不绿"，
        把"未知/空"也当成红 → 台账里只要没结论，同口径就能无限重跑。
        """
        guard.record(**KW, verdict="")
        assert guard.decide(**KW, last_verdict="").action == SKIP_DUPLICATE

    def test_unknown_verdict_does_not_license_rerun(
            self, guard: RunScopeGuard) -> None:
        """**反向对照**：未知字符串同样不算红 → 跳过（不是"放行"）。"""
        guard.record(**KW, verdict="green")
        assert guard.decide(**KW, last_verdict="whatever").action == SKIP_DUPLICATE

    def test_explicit_red_verdict_licenses_one_rerun(
            self, guard: RunScopeGuard) -> None:
        """**正向对照**：**明确**红了才放行重跑（红→修→再跑才有信息）。"""
        guard.record(**KW, verdict="2 failed")
        assert guard.decide(**KW, last_verdict="2 failed").action == RUN
        assert guard.decide(**KW, last_verdict="🔴 不通过").action == RUN

    def test_no_history_still_runs(self, guard: RunScopeGuard) -> None:
        """反向：**没有任何历史**时当然要跑（不能因"判不出"就不跑）。"""
        assert guard.decide(**KW).action == RUN

    def test_window_repeats_three_blocked_even_when_green(
            self, guard: RunScopeGuard) -> None:
        """**连跑三次** → `blocked_stability`（上轮绿也不行）。"""
        guard.record(**KW, verdict="577 passed")
        d = guard.decide(**KW, last_verdict="577 passed", window_repeats=3)
        assert d.action == BLOCKED_STABILITY
        assert d.alternatives  # 必须给替代路径

    def test_window_repeats_three_blocked_even_when_red(
            self, guard: RunScopeGuard) -> None:
        """**反向对照**：上轮红**也**不得成为"连跑 3 次"的理由。

        红只允许"重跑一次"，不等于"允许连跑三次"——这两件事必须分开。
        """
        guard.record(**KW, verdict="2 failed")
        d = guard.decide(**KW, last_verdict="2 failed", window_repeats=3)
        assert d.action == BLOCKED_STABILITY

    def test_blocked_gives_stability_alternatives(self, guard: RunScopeGuard) -> None:
        d = guard.decide(**KW, window_repeats=2)
        assert d.action == BLOCKED_STABILITY
        assert d.alternatives == guard.rules.stability_via

    def test_window_repeats_one_is_normal(self, guard: RunScopeGuard) -> None:
        """`window_repeats=1` 是正常单次执行，不得被当成稳定性滥用。"""
        assert guard.decide(**KW, window_repeats=1).action == RUN


# ----------------------------------------------------------------------
# ④ 保守性：判不出就不拦（不因台账/信息不全挡住干活）
# ----------------------------------------------------------------------

class TestConservativeNeverBlocksWork:
    def test_missing_command_not_blocked(self, guard: RunScopeGuard) -> None:
        assert guard.decide(mode="scan", revision="abc", impact=["x"],
                            command="").action == RUN

    def test_missing_revision_not_blocked(self, guard: RunScopeGuard) -> None:
        assert guard.decide(mode="scan", revision="", impact=["x"],
                            command=CMD).action == RUN

    def test_corrupt_ledger_is_empty_history(self, rules: CollabRules) -> None:
        """台账损坏 → 当空历史（**不因台账坏了挡住干活**）。"""
        d = tempfile.mkdtemp()
        root = os.path.join(d, "run-scope")
        os.makedirs(root, exist_ok=True)
        with open(os.path.join(root, "run-scope.json"), "w", encoding="utf-8") as f:
            f.write("{ this is not json")
        g = RunScopeGuard(rules, data_dir=root)
        assert g.overview()["total"] == 0
        assert g.decide(**KW).action == RUN

    def test_ledger_persists_across_instances(self, rules: CollabRules) -> None:
        """留痕跨实例可读（否则"跳过"只在本次进程内有效，重启即失效）。"""
        d = tempfile.mkdtemp()
        g1 = RunScopeGuard(rules, data_dir=d)
        g1.record(**KW, verdict="green")
        g2 = RunScopeGuard(rules, data_dir=d)
        assert g2.repeats_of(**KW) == 1
        assert g2.decide(**KW, last_verdict="green").action == SKIP_DUPLICATE

    def test_individual_run_record_not_persisted_as_override(
            self, guard: RunScopeGuard) -> None:
        """反向：**未执行**的判定不得改变台账（判定是纯读）。"""
        before = guard.overview()["total"]
        guard.decide(**KW)
        assert guard.overview()["total"] == before


# ----------------------------------------------------------------------
# ⑤ 台账与门面
# ----------------------------------------------------------------------

class TestOverviewAndFacade:
    def test_overview_flags_over_budget(self, rules: CollabRules) -> None:
        """**重复加倍的会显出来**：超上界的口径进 `over_budget`。"""
        g = RunScopeGuard(rules)
        # 模拟"绕过守卫连跑"：直接写台账三次
        for _ in range(3):
            g.record(**KW, verdict="green")
        over = g.overview()["over_budget"]
        assert len(over) == 1 and over[0]["count"] == 3

    def test_overview_guidance_present(self, guard: RunScopeGuard) -> None:
        assert "只跑一次" in guard.overview()["guidance"]

    def test_session_facade_end_to_end(self) -> None:
        d = tempfile.mkdtemp()
        s = SecretarySession(session_id="t-scope", data_dir=d)
        kw = dict(mode="scan", revision="abc", impact=["src/x.py"],
                  command="pytest -q tests/test_x.py")
        # 首次：没有记录 → 放行
        assert s.run_decision(**kw).action == RUN
        s.record_run(**kw, verdict="green")
        assert s.run_decision(**kw, last_verdict="green").action == SKIP_DUPLICATE
        assert s.run_repeats_of(**kw) == 1
        assert s.run_scope_overview()["max_identical_repeats"] == 1

    def test_session_note_mentions_incremental_and_no_repeat(self) -> None:
        d = tempfile.mkdtemp()
        s = SecretarySession(session_id="t-note", data_dir=d)
        note = s.run_scope_note()
        assert "增量" in note and "只跑一次" in note or "最多跑 1 次" in note
        assert "大版本" in note

    def test_session_default_scope_is_scan(self) -> None:
        """**默认**就是增量——不传任何标签时不得落到 full。"""
        d = tempfile.mkdtemp()
        s = SecretarySession(session_id="t-default", data_dir=d)
        assert s.review_scope().mode == "scan"
        # 判别性断言：措辞必须明确"不跑全量"（"不跑全量回归"含"全量"二字，
        # 故不能只查字面，须查**否定 + 增量**语义；配正向对照防恒真）
        cmds = s.review_scope_commands()
        assert "增量" in cmds["test"] and "不跑全量" in cmds["test"]
        assert "增量" in cmds["build"]
        full_cmds = s.review_scope_commands(major_version=True)
        assert "全量" in full_cmds["test"] and "增量" not in full_cmds["test"]


# ----------------------------------------------------------------------
# ⑥ 配置侧根因：仓库级覆写**必须真的生效**（"规则写了却不生效"是本次真问题）
# ----------------------------------------------------------------------

class TestRepoOverrideActuallyApplies:
    """根因回归：`_OVERRIDABLE_FIELDS` 是**白名单**，漏登记 → 覆写被静默丢弃。

    这正是用户问的"是规则不健全吗？还是配置有问题？"——**配置白名单漏登记**，
    表现为"规则文件里明明写了，跑起来还是默认值"。
    """

    def test_new_scan_fields_are_overridable(self) -> None:
        from secretary.collab.rules import overridable_field_names
        names = overridable_field_names()
        for f in ("scan_default_mode", "max_identical_repeats",
                  "repeat_rerun_on_failure", "repeat_keys",
                  "allow_stability_repeats", "stability_via"):
            assert f in names, f"{f} 未登记进 _OVERRIDABLE_FIELDS → 覆写会被静默丢弃"

    def test_audit_reports_no_missing_fields(self) -> None:
        """自检必须为空：任何数值字段漏登记都会被这条断言挡住（防再犯）。"""
        from secretary.collab.rules import _audit_overridable_fields
        assert _audit_overridable_fields() == []

    def test_audit_is_discriminative(self) -> None:
        """**反向对照**：故意移除一个字段 → 自检必须报出来（防恒真）。"""
        import secretary.collab.rules as R
        orig = R._OVERRIDABLE_FIELDS
        try:
            R._OVERRIDABLE_FIELDS = tuple(
                x for x in orig if x != "max_identical_repeats")
            assert "max_identical_repeats" in R._audit_overridable_fields()
        finally:
            R._OVERRIDABLE_FIELDS = orig
        assert R._audit_overridable_fields() == []

    def test_repo_yaml_override_reaches_merged_rules(self, tmp_path) -> None:
        """仓库级改数值 → **合并结果**必须是仓库级的值（不是组织级/默认）。"""
        from secretary.collab import load_rules
        repo = tmp_path / "collab-rules.yaml"
        repo.write_text(
            "scan:\n"
            "  max_identical_repeats: 3\n"
            "  default_mode: \"scan\"\n"
            "  stability_via: [\"mutation_probe\"]\n",
            encoding="utf-8")
        rules = load_rules(repo_path=str(repo),
                           search_from=None, scope="repo")
        assert rules.max_identical_repeats == 3
        assert rules.stability_via == ["mutation_probe"]

    def test_org_value_survives_when_repo_silent(self) -> None:
        """反向：仓库级**没写**的域 → 沿用组织级（不得被默认值抹掉）。"""
        from secretary.collab import load_rules
        rules = load_rules(search_from=os.path.dirname(
            os.path.dirname(__file__)))
        assert rules.max_identical_repeats == 1
        assert rules.scan_default_mode == "scan"
        assert rules.allow_stability_repeats is False
        scopes = [c.get("scope") for c in rules.chain]
        assert "organization" in scopes  # 组织级确实参与了
