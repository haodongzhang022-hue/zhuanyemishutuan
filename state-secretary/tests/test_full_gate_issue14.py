"""Issue #14 本轮追问回归：**默认全量根本不跑**（让门禁从"默认值"变成"硬闸"）

用户原话：
  > 这里我要求过不跑全量，他怎么还在一直连续跑三次稳定这种事情？是我们新配置的，
  > 避免跑全量的那个规则现在有问题吗？…他不然我们新开发一步，他就要跑一次 600 多次的验证。
  > 甚至还要跑三倍，这太过分了。…处理完这个让他合理起来，我们再通用，
  > 不然相当于一次污染了很多仓库，那不更完蛋了。

## 上一轮的规则为什么没拦住（根因，必须留在用例里防复发）

| 上一轮的做法 | 为什么没拦住 | 本轮的硬闸 |
|---|---|---|
| `scan.default_mode: scan` | 只是**默认值**：调用方显式给 `mode="full"` 就绕过 | `decide_full()`：`full` 必须过三重条件，否则 `blocked_full` |
| `RunScopeGuard` 判重 | 判重键含 **`revision`**：每改一行代码 revision 就变 → 每次都算"新口径" → 每次都放行 | 去升级口径 `repeat_scope: content`：按**受审内容**判重（跨版本） |
| `.cnb.yml` 各事件都跑同一套 | 评论/目录/定时类事件**也**触发 → 没有基线、没有变更集，纯空烧 | `full_run_eligible()`：这些事件一律不得跑全量 |

## 判别性设计（关键断言配**反向对照**，防恒真）

- 门禁的**方向**与判重相反，两者必须能被区分开：
  同一份输入，`run_decision` 判"放行"（保守多跑），`full_decision` 判"拦截"（保守少跑）；
- **"agent 自述"不构成凭据**：`major_version=True` + `source="agent_claim"` 必须被拒
  （反向对照：换成 `source="operator"` 必须放行）——否则"谁喊一声大版本就能解锁全量"；
- **判不出就拦**（反向对照：判重那边判不出是**放行**）；
- **拦截必须给路径**：`blocked_full` 的判定必须带可直接跑的增量命令（不能只说"不许"）；
- **去升级必须真生效**：同内容换 revision → 跳过（反向对照：换受影响面/命令 → 放行）；
- **白名单是白名单**：`shared/collab/` 放行，`src/`、绝对路径、`..` 穿越必须拒。
"""
from __future__ import annotations

import os
import shutil
import tempfile

import pytest

from secretary.collab import (
    BLOCKED_FULL,
    BLOCKED_STABILITY,
    RUN,
    SKIP_DUPLICATE,
    SOURCE_ACTION,
    SOURCE_AGENT_CLAIM,
    SOURCE_OPERATOR,
    SOURCE_TAG,
    CollabRules,
    RunScopeGuard,
    content_key_of,
    decide_full,
    full_run_eligible,
    incremental_command,
    load_rules,
    pipeline_change_allowed,
    repeat_scope_of,
)
from secretary.session import SecretarySession

RULES_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SESSION = "i14full"

CMD = "pytest -q tests/"
KW = dict(mode="scan", revision="abc123", impact=["src/a.py", "src/b.py"],
          command=CMD)


@pytest.fixture()
def rules() -> CollabRules:
    return load_rules(search_from=RULES_DIR)


@pytest.fixture()
def env():
    d = tempfile.mkdtemp()
    sec = SecretarySession(SESSION, data_dir=d, role_name="secretary.review")
    try:
        yield d, sec
    finally:
        shutil.rmtree(d, ignore_errors=True)


# ----------------------------------------------------------------------
# ① 规则包：去升级口 + 事件黑名单 + 兜底白名单（配置侧）
# ----------------------------------------------------------------------

class TestRulePackageCarriesFullGatePolicy:
    def test_repeat_scope_defaults_to_content(self, rules: CollabRules) -> None:
        """去升级口默认 `content`：按受审内容判重，而不是按 revision。"""
        assert rules.repeat_scope == "content"
        assert repeat_scope_of(rules) == "content"

    def test_comment_events_blocked(self, rules: CollabRules) -> None:
        """评论类事件必须在不允许跑全量的名单里（防跨仓污染）。"""
        for ev in ("issue.comment", "pull_request.comment", "issuE.OPEN"):
            assert ev.lower() in rules.blocked_events

    def test_pipeline_allowlist_is_minimal(self, rules: CollabRules) -> None:
        """兜底白名单必须**最小**（只允许改规则文件），否则兜底就是后门。"""
        assert rules.pipeline_allowlist == ["shared/collab/"]

    def test_new_fields_are_overridable(self, rules: CollabRules) -> None:
        """新字段必须进可覆写白名单——否则仓库级写了会被**静默丢弃**。"""
        from secretary.collab.rules import overridable_field_names
        names = overridable_field_names()
        for k in ("repeat_scope", "blocked_events", "pipeline_allowlist"):
            assert k in names, f"{k} 未登记进可覆写白名单 → 覆写会被静默丢弃"

    def test_audit_reports_no_missing_fields(self) -> None:
        """白名单自检：没有"漏登记"的数值字段（防"规则写了不生效"复发）。"""
        from secretary.collab.rules import _audit_overridable_fields
        assert _audit_overridable_fields() == []

    def test_fields_surface_in_dict(self, rules: CollabRules) -> None:
        scan = rules.to_dict()["scan"]
        for k in ("repeat_scope", "blocked_events", "pipeline_allowlist"):
            assert k in scan


# ----------------------------------------------------------------------
# ② 全量门禁：三重条件 + **判不出就拦**
# ----------------------------------------------------------------------

class TestFullGateRequiresAllThreeConditions:
    def test_major_version_with_action_allows(self) -> None:
        """正向：大版本事实 + 可采信来源 + 代码推进事件 → 允许全量。"""
        d = decide_full(major_version=True, source=SOURCE_ACTION, event="push")
        assert d.action == RUN and d.mode == "full" and d.is_full

    def test_major_tag_with_operator_allows(self, rules: CollabRules) -> None:
        """大版本**标签** + 人工指令 → 允许（标签是可判定事实）。"""
        d = decide_full(rules, tags=["major-version"], source=SOURCE_OPERATOR,
                        event="push")
        assert d.action == RUN and d.is_full

    def test_agent_claim_is_not_credible(self, rules: CollabRules) -> None:
        """**agent 自述不算凭据**：喊一声大版本不能解锁全量。"""
        d = decide_full(rules, major_version=True, source=SOURCE_AGENT_CLAIM,
                        event="push")
        assert d.action == BLOCKED_FULL
        assert "自述" in d.reason or "凭据" in d.reason

    def test_agent_claim_rejected_even_with_tag(self, rules: CollabRules) -> None:
        """**反向对照**：来源不可采信时，**标签也不作数**（不能靠标签"补票"）。"""
        d = decide_full(rules, tags=["major-version"], source=SOURCE_AGENT_CLAIM,
                        event="push")
        assert d.action == BLOCKED_FULL

    def test_credible_source_without_major_is_blocked(
            self, rules: CollabRules) -> None:
        """**反向对照**：来源可采信但没有大版本事实 → 同样拦。"""
        d = decide_full(rules, major_version=False, source=SOURCE_OPERATOR,
                        event="push")
        assert d.action == BLOCKED_FULL

    def test_undecidable_is_blocked(self, rules: CollabRules) -> None:
        """**判不出就拦**（与判重的"判不出就放行"方向相反）。"""
        d = decide_full(rules, source=SOURCE_OPERATOR, event="push")
        assert d.action == BLOCKED_FULL
        assert "判不出" in d.reason

    def test_undecidable_direction_is_opposite_to_skip(
            self, rules: CollabRules) -> None:
        """**方向性对照**（本组核心）：同一份"信息不全"的输入，
        `decide_full` 必须**拦**，`RunScopeGuard.decide` 必须**放行**。"""
        full = decide_full(rules, source=SOURCE_OPERATOR, event="push")
        guard = RunScopeGuard(rules)
        skip = guard.decide(mode="scan", revision="", impact=["x"],
                            command=CMD)
        assert full.action == BLOCKED_FULL, "全量：判不出 → 保守拦截"
        assert skip.action == RUN, "判重：判不出 → 保守放行（不可混用两套方向）"
        assert full.action != skip.action

    def test_comment_event_blocks_even_when_major(self, rules: CollabRules) -> None:
        """**评论事件 + 大版本** → 仍拦（没有测试阶段的事件不得跑全量）。"""
        d = decide_full(rules, major_version=True, source=SOURCE_ACTION,
                        event="issue.comment")
        assert d.action == BLOCKED_FULL
        assert "不含" in d.reason or "测试阶段" in d.reason

    def test_unknown_event_blocks(self, rules: CollabRules) -> None:
        """事件不可判定 → 拦（拿不准就不全量）。"""
        d = decide_full(rules, major_version=True, source=SOURCE_ACTION)
        assert d.action == BLOCKED_FULL

    def test_all_three_required_matrix(self, rules: CollabRules) -> None:
        """三条件矩阵：任一缺失都不得放行（反向：三者齐全才放行）。"""
        cases = [
            dict(major_version=True, source=SOURCE_ACTION, event="issue.comment"),
            dict(major_version=False, source=SOURCE_ACTION, event="push"),
            dict(major_version=True, source=SOURCE_AGENT_CLAIM, event="push"),
            dict(major_version=None, source=SOURCE_ACTION, event="push"),
            dict(major_version=True, source=SOURCE_ACTION, event=""),
        ]
        for case in cases:
            d = decide_full(rules, **case)
            assert d.action == BLOCKED_FULL, f"应拦截: {case} → {d.action}"
        ok = decide_full(rules, major_version=True, source=SOURCE_ACTION,
                         event="push")
        assert ok.action == RUN, "三者齐全必须放行（防恒真：不能什么都拦）"


# ----------------------------------------------------------------------
# ③ 拦截必须**给出可直接跑的增量命令**（拦了但给路）
# ----------------------------------------------------------------------

class TestBlockedFullGivesRunnablePath:
    def test_blocked_carries_scan_command(self, rules: CollabRules) -> None:
        d = decide_full(rules, source=SOURCE_AGENT_CLAIM, event="push",
                        impact=["tests/test_a.py"], command="pytest -q")
        assert d.action == BLOCKED_FULL
        assert d.scan_command == "pytest -q tests/test_a.py"
        assert d.scan_targets == "tests/test_a.py"
        assert "不跑全量" in d.guidance()

    def test_empty_impact_degrades_to_tests_dir(self) -> None:
        """受影响面为空 → 退化到 `tests/`（仍是变更口径，**不是** `pytest -q` 全量）。"""
        targets, cmd = incremental_command("", "pytest -q")
        assert targets == "tests/"
        assert cmd == "pytest -q tests/"
        assert cmd != "pytest -q", "退化不得退回全量回归"

    def test_impact_set_semantics_ordering(self) -> None:
        """**反向对照**：受影响面换顺序 = 同一口径（不得被当成新口径）。"""
        a, _ = incremental_command(["b.py", "a.py"])
        b, _ = incremental_command(["a.py", "b.py"])
        assert a == b

    def test_guidance_is_actionable(self, rules: CollabRules) -> None:
        d = decide_full(rules, source=SOURCE_AGENT_CLAIM, event="push")
        assert d.scan_command in d.guidance(), "口径里必须含可直接跑的命令"


# ----------------------------------------------------------------------
# ④ 去升级：**改一行代码不再算新口径**（本轮核心缺陷）
# ----------------------------------------------------------------------

class TestRepeatScopeDeEscalation:
    def test_content_key_ignores_revision(self) -> None:
        """受审内容指纹**不含 revision**（含了就退回旧行为）。"""
        a = content_key_of("scan", ["src/a.py"], CMD)
        b = content_key_of("scan", ["src/a.py"], CMD)
        assert a == b

    def test_new_revision_same_content_is_skipped(
            self, rules: CollabRules) -> None:
        """**本轮核心**：同一份受审内容，换了 revision → 必须**跳过**。

        旧实现（比较键含 revision）在这里会放行 → 就是用户看到的"每开发一步
        就跑一次 600 多项验证"。
        """
        g = RunScopeGuard(rules)
        g.record_repeat(**KW, verdict="684 passed")
        d = g.decide_repeat(**{**KW, "revision": "deadbeef"})
        assert d.action == SKIP_DUPLICATE
        assert d.repeat_scope == "content"
        assert d.content_revisions >= 1

    def test_same_revision_same_content_also_skipped(
            self, rules: CollabRules) -> None:
        """同一 revision 同内容同样跳过（两档合并，不留"换版本就放行"的口子）。"""
        g = RunScopeGuard(rules)
        g.record_repeat(**KW, verdict="684 passed")
        assert g.decide_repeat(**KW).action == SKIP_DUPLICATE

    def test_changed_impact_still_runs(self, rules: CollabRules) -> None:
        """**反向对照**：受影响面真变了 → 必须放行（不能因为去升级就拦住新工作）。"""
        g = RunScopeGuard(rules)
        g.record_repeat(**KW, verdict="684 passed")
        d = g.decide_repeat(**{**KW, "revision": "new", "impact": ["src/c.py"]})
        assert d.action == RUN

    def test_changed_command_still_runs(self, rules: CollabRules) -> None:
        """**反向对照**：命令变了（换了跑法）→ 放行（换口径是真新信息）。"""
        g = RunScopeGuard(rules)
        g.record_repeat(**KW, verdict="684 passed")
        d = g.decide_repeat(**{**KW, "revision": "new",
                               "command": "pytest -q tests/test_z.py"})
        assert d.action == RUN

    def test_red_verdict_does_not_license_content_repeat(
            self, rules: CollabRules) -> None:
        """**反向对照**：上一轮红**也**不得成为"换个版本重跑同一份内容"的理由。

        "红→修→再跑"的前提是**修了**（内容会变）；内容没变就说明没修，
        再跑一遍不会有新信息。
        """
        g = RunScopeGuard(rules)
        g.record_repeat(**KW, verdict="2 failed")
        d = g.decide_repeat(**{**KW, "revision": "v2", "last_verdict": "2 failed"})
        assert d.action == SKIP_DUPLICATE

    def test_revision_scope_still_available(self) -> None:
        """`repeat_scope: revision` 仍可选（特殊仓"每个提交都要跑"）——但非默认。"""
        rules = CollabRules.from_dict({"scan": {"repeat_scope": "revision"}})
        assert repeat_scope_of(rules) == "revision"
        g = RunScopeGuard(rules)
        g.record_repeat(**KW, verdict="ok")
        d = g.decide_repeat(**{**KW, "revision": "other"})
        assert d.action == RUN, "revision 档下换版本确实算新口径"

    def test_invalid_repeat_scope_falls_back_to_content(self) -> None:
        """非法取值 → 回落 `content`（宽进；不得因此退化成"什么都放行"）。"""
        rules = CollabRules.from_dict({"scan": {"repeat_scope": "whatever"}})
        assert repeat_scope_of(rules) == "content"

    def test_content_ledger_persists(self, rules: CollabRules) -> None:
        """受审内容台账跨实例持久化（换 agent 不复活"重跑一遍"）。"""
        d = tempfile.mkdtemp()
        try:
            g1 = RunScopeGuard(rules, data_dir=d)
            g1.record_repeat(**KW, verdict="ok")
            g2 = RunScopeGuard(rules, data_dir=d)
            assert g2.content_repeats_of(mode="scan", impact=KW["impact"],
                                         command=CMD) == 1
            assert g2.decide_repeat(**{**KW, "revision": "v9"}).action == \
                SKIP_DUPLICATE
        finally:
            shutil.rmtree(d, ignore_errors=True)

    def test_overview_exposes_content_view(self, rules: CollabRules) -> None:
        """总览里能直接看到"同一受审内容被跑了几遍、跨几个版本"。"""
        g = RunScopeGuard(rules)
        g.record_repeat(**KW, verdict="ok")
        g.record_repeat(**{**KW, "revision": "v2"}, verdict="ok")
        ov = g.overview()
        assert ov["repeat_scope"] == "content"
        row = ov["content_records"][0]
        assert row["count"] == 2
        assert row["revision_count"] == 2


# ----------------------------------------------------------------------
# ⑤ 稳定性滥用：**不受任何口径字段影响**
# ----------------------------------------------------------------------

class TestStabilityAbuseIsNotBypassable:
    def test_window_three_blocked_under_content_scope(
            self, rules: CollabRules) -> None:
        """想连跑三次 → `blocked_stability`（去升级档下同样拦）。"""
        g = RunScopeGuard(rules)
        d = g.decide_repeat(**{**KW, "window_repeats": 3})
        assert d.action == BLOCKED_STABILITY

    def test_window_three_blocked_even_with_new_revision(
            self, rules: CollabRules) -> None:
        """**反向对照**：换个 revision 也**不能**绕过"连跑三次"的拒绝。"""
        g = RunScopeGuard(rules)
        g.record_repeat(**KW, verdict="ok")
        d = g.decide_repeat(**{**KW, "revision": "completely-new",
                               "window_repeats": 3})
        assert d.action == BLOCKED_STABILITY

    def test_window_three_blocked_with_empty_fields(
            self, rules: CollabRules) -> None:
        """**反向对照**：口径字段全空也**不能**绕（这条判定不看那些字段）。"""
        g = RunScopeGuard(rules)
        d = g.decide_repeat(mode="", revision="", impact="", command="",
                            window_repeats=3)
        assert d.action == BLOCKED_STABILITY

    def test_window_one_is_normal(self, rules: CollabRules) -> None:
        """`window_repeats=1` 是正常单次执行，不得被当成滥用（防恒真）。"""
        g = RunScopeGuard(rules)
        assert g.decide_repeat(**KW, window_repeats=1).action == RUN

    def test_invalid_window_value_is_lenient(self, rules: CollabRules) -> None:
        """非法 `window_repeats` → 按 1 处理（不因参数写错就拦）。"""
        g = RunScopeGuard(rules)
        assert g.decide_repeat(**KW, window_repeats="not-a-number").action == RUN


# ----------------------------------------------------------------------
# ⑥ 事件侧：不含有测试阶段的事件不得跑全量
# ----------------------------------------------------------------------

class TestEventSideGate:
    def test_comment_and_cron_blocked(self, rules: CollabRules) -> None:
        for ev in ("issue.comment", "pull_request.comment", "crontab",
                   "api_trigger", "web_trigger", "issue.open", "issue.update"):
            res = full_run_eligible(ev, blocked_events=rules.blocked_events)
            assert res["blocked"] is True, f"{ev} 不该允许全量"

    def test_push_eligible(self, rules: CollabRules) -> None:
        """**反向对照**：代码推进类事件可（在大版本条件下）跑全量。"""
        res = full_run_eligible("push", blocked_events=rules.blocked_events)
        assert res["blocked"] is False

    def test_unknown_event_blocks(self, rules: CollabRules) -> None:
        assert full_run_eligible("", blocked_events=rules.blocked_events)["blocked"]

    def test_case_insensitive(self, rules: CollabRules) -> None:
        assert full_run_eligible("ISSUE.COMMENT",
                                 blocked_events=rules.blocked_events)["blocked"]


# ----------------------------------------------------------------------
# ⑦ 兜底白名单：最小权限，别让"兜底"变成后门
# ----------------------------------------------------------------------

class TestPipelineChangeAllowlist:
    def test_rules_path_allowed(self, rules: CollabRules) -> None:
        res = pipeline_change_allowed(["shared/collab/collab-rules.yaml"])
        assert res["allowed"] is True

    def test_source_path_rejected(self) -> None:
        """**反向对照**：改源码必须拒（兜底不得"顺手改别的"）。"""
        assert pipeline_change_allowed(["state-secretary/src/x.py"])["allowed"] \
            is False

    def test_absolute_path_rejected(self) -> None:
        assert pipeline_change_allowed(["/etc/passwd"])["allowed"] is False

    def test_traversal_rejected(self) -> None:
        assert pipeline_change_allowed(["shared/collab/../../etc/passwd"])["allowed"] \
            is False

    def test_mixed_paths_rejected(self) -> None:
        """**反向对照**：只要有一条越界，整批拒（不允许"夹带"）。"""
        res = pipeline_change_allowed(
            ["shared/collab/organization.yaml", "state-secretary/src/x.py"])
        assert res["allowed"] is False

    def test_empty_paths_rejected(self) -> None:
        assert pipeline_change_allowed([])["allowed"] is False

    def test_custom_allowlist_respected(self) -> None:
        """白名单可覆写（数值/清单级），但**默认为最小**。"""
        res = pipeline_change_allowed(["docs/x.md"],
                                      allowlist=["docs/"])
        assert res["allowed"] is True


# ----------------------------------------------------------------------
# ⑧ 会话门面端到端
# ----------------------------------------------------------------------

class TestSessionFacade:
    def test_full_decision_default_is_blocked(self, env) -> None:
        """默认（无任何凭据）→ 拦。这是"默认全量根本不跑"的门面层证据。"""
        _, sec = env
        d = sec.full_decision(event="push")
        assert d.action == BLOCKED_FULL
        assert d.scan_command

    def test_full_decision_with_credentials(self, env) -> None:
        """大版本 + 人工指令 + push → 放行。"""
        _, sec = env
        d = sec.full_decision(major_version=True, source=SOURCE_OPERATOR,
                              event="push")
        assert d.is_full

    def test_full_decision_comment_event_blocks(self, env) -> None:
        """评论触发（本例正是这种）下**即使是大版本也不得跑全量**。"""
        _, sec = env
        d = sec.full_decision(major_version=True, source=SOURCE_OPERATOR,
                              event="issue.comment")
        assert d.action == BLOCKED_FULL

    def test_full_gate_note_mentions_default_and_gate(self, env) -> None:
        _, sec = env
        note = sec.full_gate_note()
        assert "全量只在大版本" in note
        assert "不得跑全量" in note or "不得" in note

    def test_decide_repeat_end_to_end(self, env) -> None:
        """门面层：同一受审内容换版本 → 跳过（旧行为会放行）。"""
        _, sec = env
        sec.record_repeat(mode="scan", revision="v1",
                          impact=["tests/test_a.py"], command="pytest -q tests/",
                          verdict="ok")
        d = sec.decide_repeat(mode="scan", revision="v2",
                              impact=["tests/test_a.py"],
                              command="pytest -q tests/")
        assert d.action == SKIP_DUPLICATE

    def test_session_uses_rule_package_values(self, env) -> None:
        _, sec = env
        assert sec.collab_rules.repeat_scope == "content"
        assert sec.collab_rules.pipeline_allowlist == ["shared/collab/"]

    def test_pipeline_change_allowed_facade(self, env) -> None:
        _, sec = env
        assert sec.pipeline_change_allowed(["shared/collab/x.yaml"])["allowed"]
        assert not sec.pipeline_change_allowed(["src/x.py"])["allowed"]


# ----------------------------------------------------------------------
# ⑨ 流水线配置侧：无"传参跑全量"的面
# ----------------------------------------------------------------------

class TestPipelineConfigHasNoFullInput:
    def _read(self, rel: str) -> str:
        root = os.path.dirname(RULES_DIR)
        with open(os.path.join(root, rel), encoding="utf-8") as f:
            return f.read()

    def test_dispatch_entry_exists_and_has_no_scope_input(self) -> None:
        """唯一入口不接收"全量"参数（有的话门禁就只是建议）。"""
        body = self._read("scripts/ci-dispatch.sh")
        assert "ci-incremental-test.sh" in body
        for bad in ("CNB_SCOPE", "scope=", "mode=full"):
            assert bad not in body, f"入口不应存在 {bad} 这类输入面"

    def test_main_push_uses_dispatch(self) -> None:
        body = self._read(".cnb.yml")
        assert "ci-dispatch.sh" in body

    def test_comment_events_use_comment_task(self) -> None:
        """评论/目录类事件必须走**接待**任务（不是会跑测试的那条）。"""
        body = self._read(".cnb.yml")
        assert "npc_comment_task" in body
        seg = body.split("issue.comment@npc:")[1]
        assert "npc_comment_task" in seg[:40]

    def test_comment_prompt_forbids_full(self) -> None:
        body = self._read(".cnb.yml")
        assert "不得跑全量" in body

    def test_ci_script_runs_full_gate(self) -> None:
        """CI 必须真的过门禁（有实际调用，不是只写注释）。"""
        body = self._read("scripts/ci-incremental-test.sh")
        assert "decide_full(" in body, "CI 脚本必须真的调用全量门禁"
        assert "full-gate" in body, "门禁判定必须留痕可见"

    def test_ci_script_uses_content_de_escalation(self) -> None:
        """CI 必须用去升级口径（否则又回到"改一行算新口径"）。"""
        body = self._read("scripts/ci-incremental-test.sh")
        assert "decide_repeat" in body
        assert "record_repeat" in body
