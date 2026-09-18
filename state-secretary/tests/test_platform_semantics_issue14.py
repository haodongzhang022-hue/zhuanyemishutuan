"""Issue #14 本轮：**对齐官方平台语义 + 官方最佳实践对照**

用户要求（原话）：
  > 检查一下这个功能是否正常了，然后这个功能可能也有其他用户开发了，可以检索一下有没有。
  > 有可能开发的比我们处理的要好，或者**官方有没有对此有最佳实践可以参考过来**。
  > 帮助我们把这个功能开发到完善，然后**分配到组织下，实现各个仓库的通用**。

本轮查证官方文档后**发现并修掉**的真问题（都有判别性断言 + 反向对照）：

① `cnb:await` 只能对**同一个事件**触发的 pipeline 配对。原实现在"两边事件不同"
   时仍挂 await → **永远等不到**（要靠平台死锁检测才发现）。现挂之前先校验，
   配不上对就降级 crontab。
② `cnb:await` 的 key **仅能 resolve 一次**。原实现复用同一个 key → 第 2 次起必死等。
   现 key 带**轮次**（`probe:<key>:r<n>`）。
③ await 只能跑在 `stages`（endStages/failStages 内不生效）→ 加位置校验。
④ 跨仓事件此前按**本地会话号**读检查点（对方的检查点不在本地磁盘上）→
   改为"事件自带上下文"，没带就保守降级。
⑤ `RunScopeGuard` 在**算不出受影响面**时会把不同变更集撞成同一指纹 → **该跑的被跳过**
   （漏跑，危险方向）。照官方 SCA 增量口径改为**保守放行**（判不出就不增量）。

官方最佳实践对照（`official_capabilities()`）：官方 AI 评审插件已做 diff 增量、
非代码文件过滤、历史评论去重、降噪阈值；官方 SCA 插件按"新增组件/新增 CVE"判增量、
内容未变的改名不重复报——本仓据此补了 `ReviewFingerprintLedger`（确定性指纹去重）。
"""
from __future__ import annotations

import os
import shutil
import tempfile

import pytest

from secretary.collab import (
    AWAIT_CONSTRAINTS,
    DOC_SOURCES,
    await_eligible,
    await_key_for,
    await_stage_ok,
    capability_report,
    impact_available,
    local_cpu_note,
    official_capabilities,
    platform_report,
    resolve_driver,
)
from secretary.collab.platform import CRONTAB_MIN_INTERVAL_SEC as PLN_MIN
from secretary.collab.probe import (
    AWAIT_MAX_SEC,
    CRONTAB_MIN_INTERVAL_SEC as PROBE_MIN,
    CHECK_LOCK,
    DRIVER_AWAIT,
    DRIVER_CRONTAB,
    WAIT,
    CheckpointProbe,
    dispatch_shared,
)
from secretary.session import SecretarySession


#: 仓库根（本测试文件位于 <root>/state-secretary/tests/）
REPO_ROOT = os.path.dirname(os.path.dirname(
    os.path.dirname(os.path.abspath(__file__))))


@pytest.fixture()
def probe():
    d = tempfile.mkdtemp()
    p = CheckpointProbe("i14plat", data_dir=d)
    try:
        yield p
    finally:
        shutil.rmtree(d, ignore_errors=True)


# ------------------------------------------------------------------
# ① await 只能**同一事件**配对（官方硬约束）
# ------------------------------------------------------------------

class TestAwaitSameEventConstraint:
    def test_same_event_is_eligible(self):
        """两侧同一事件 + 位于 stages → 可挂 await（零占用唤醒）。"""
        chk = await_eligible("push", "push")
        assert chk["eligible"] is True and chk["fallback"] == ""

    def test_different_event_is_not_eligible(self):
        """**反向对照**：两侧事件不同 → 配不上对，**不得**挂 await。"""
        chk = await_eligible("push", "issue.comment")
        assert chk["eligible"] is False
        assert chk["fallback"] == DRIVER_CRONTAB
        assert AWAIT_CONSTRAINTS["same_event"] in chk["constraints"]

    def test_unknown_event_is_not_eligible(self):
        """**反向对照**：事件不可判定 → 不猜、不挂（"看不见 ≠ 会 resolve"）。"""
        assert await_eligible("", "push")["eligible"] is False
        assert await_eligible("push", "")["eligible"] is False
        assert await_eligible("", "")["eligible"] is False
        assert await_eligible(None, None)["eligible"] is False

    def test_platform_owned_event_needs_no_await(self):
        """等待方本身是平台定时事件 → 平台到点会再来一次，等它没有意义。"""
        assert await_eligible("crontab", "crontab")["eligible"] is False

    def test_stage_position_matters(self):
        """官方：await-resolve 只能跑在 `stages`（endStages/failStages 不生效）。"""
        assert await_stage_ok("stages") is True
        assert await_stage_ok("endStages") is False
        assert await_stage_ok("failStages") is False
        assert await_eligible("push", "push", stage="endStages")["eligible"] is False

    def test_case_insensitive_event_matching(self):
        """事件名大小写不该改变判定（"Push" 与 "push" 是同一事件）。"""
        assert await_eligible(" Push ", "push")["eligible"] is True


# ------------------------------------------------------------------
# ② key 仅能 resolve 一次 → key 必须带轮次
# ------------------------------------------------------------------

class TestAwaitKeyIsPerRound:
    def test_key_includes_attempt(self):
        """每轮 key 唯一——否则第 2 次及以后的 await 永远等不到 resolve。"""
        keys = [await_key_for("cp", i) for i in range(6)]
        assert len(set(keys)) == 6, "各轮 key 必须互不相同"
        assert keys[0] == "probe:cp:r0" and keys[3] == "probe:cp:r3"

    def test_bad_attempt_falls_back_to_zero(self):
        """轮次不可判定 → 用 0（仍保证同轮唯一，不抛）。"""
        assert await_key_for("cp", None) == "probe:cp:r0"
        assert await_key_for("cp", "not-a-number") == "probe:cp:r0"
        assert await_key_for("cp", -5) == "probe:cp:r0"

    def test_resolve_driver_generates_key_only_when_eligible(self):
        """可挂时给 key；不可挂时**不给** key（避免"挂不上还留个 key 看着像挂了"）。"""
        ok = resolve_driver(DRIVER_AWAIT, "cp", 2, "push", "push")
        assert ok.driver == DRIVER_AWAIT and ok.await_key == "probe:cp:r2"
        bad = resolve_driver(DRIVER_AWAIT, "cp", 2, "push", "issue.comment")
        assert bad.driver == DRIVER_CRONTAB and bad.await_key == ""


# ------------------------------------------------------------------
# ③ 降级：配不上对 → crontab（平台代跑 + 平台死锁检测），**永不**本地空转
# ------------------------------------------------------------------

class TestDowngradeRatherThanDeadWait:
    def test_downgrade_driver_and_reason(self):
        v = resolve_driver(DRIVER_AWAIT, "cp", 0, "push", "issue.comment")
        assert v.downgraded is True and v.driver == DRIVER_CRONTAB
        assert AWAIT_CONSTRAINTS["same_event"] in v.constraints
        assert DOC_SOURCES["crontab"] in v.sources

    def test_non_await_driver_untouched(self):
        """**反向对照**：非 await 落点不受配对约束（别把 crontab 也降级了）。"""
        v = resolve_driver(DRIVER_CRONTAB, "cp", 0, "push", "issue.comment")
        assert v.driver == DRIVER_CRONTAB and v.downgraded is False

    def test_downgrade_never_lands_on_local_loop(self):
        """降级只可能落到**平台**落点：永不出现"本地空转"这种驱动。"""
        for we in ("", "push", "crontab", "unknown-event"):
            for re_ in ("", "push", "issue.comment"):
                v = resolve_driver(DRIVER_AWAIT, "cp", 0, we, re_)
                assert v.driver in (DRIVER_AWAIT, DRIVER_CRONTAB), (we, re_)
                assert "local" not in v.driver and "sleep" not in v.driver

    def test_local_cpu_note_says_platform_still_waits(self):
        """降级**不代表丢功能**：仍是平台在等（且平台做死锁检测）。"""
        note = local_cpu_note()
        assert "平台" in note and "0" in note and "sleep" in note

    def test_probe_registration_records_downgrade(self, probe):
        """登记等待时降级要**留痕**（理由 + 出处），便于事后复核。"""
        probe.create("ck", kind=CHECK_LOCK,
                     waiter_event="issue.comment", resolver_event="push")
        cp = probe.register_platform_wait("ck")
        assert cp.resolve_status == "awaiting"
        last = cp.history[-1]
        assert last["driver"] == DRIVER_CRONTAB and last["downgraded"] is True
        assert last["requested_driver"] == DRIVER_AWAIT
        assert last["downgrade_reason"] and last["platform_sources"]

    def test_probe_registration_keeps_await_when_paired(self, probe):
        """**反向对照**：配得上对就**不降级**，并按轮次生成 key。"""
        probe.create("ck2", kind=CHECK_LOCK,
                     waiter_event="push", resolver_event="push")
        cp = probe.register_platform_wait("ck2")
        assert cp.await_key == "probe:ck2:r0"
        assert cp.history[-1]["downgraded"] is False

    def test_delay_plan_shows_real_driver(self, probe):
        """计划表显示的是**校验后**的真实落点（不是"计划一套、执行一套"）。"""
        cp = probe.create("plan-ok", kind=CHECK_LOCK,
                          waiter_event="push", resolver_event="push")
        rows = probe.delay_plan(cp)[:4]
        assert all(r["driver"] == DRIVER_AWAIT for r in rows)
        assert all(r["await_key"].startswith("probe:plan-ok:r") for r in rows)

    def test_dispatch_downgrades_and_never_wakes(self, probe):
        """dispatch 同样过校验：降级后 `wake_allowed` 仍为 False（等待不唤醒）。"""
        probe.create("d1", kind=CHECK_LOCK,
                     waiter_event="issue.comment", resolver_event="push")
        out = probe.probe("d1", lock_held=True, lock_holder="dev")
        dis = probe.dispatch(out.event)
        assert dis["action"] == "platform_crontab"
        assert dis["downgraded"] is True and dis["wake_allowed"] is False


# ------------------------------------------------------------------
# ④ 跨仓事件：上下文只能由事件自带（别读本地检查点）
# ------------------------------------------------------------------

class TestCrossRepoCarriesOwnContext:
    def test_shared_event_with_events_keeps_await(self):
        """事件自带两侧同一事件 → 接收方按同一份规则挂 await。"""
        dis = dispatch_shared({"checkpoint_key": "cp", "verdict": WAIT,
                               "waiter_event": "push", "resolver_event": "push"})
        assert dis["action"] == "platform_await"
        assert dis["trigger"]["key"] == "probe:cp:r0"

    def test_shared_event_without_events_degrades(self):
        """**反向对照**：没带事件名 → 不猜（对方的检查点不在本地磁盘上）→ 降级。"""
        dis = dispatch_shared({"checkpoint_key": "cp", "verdict": WAIT})
        assert dis["action"] == "platform_crontab" and dis["downgraded"] is True

    def test_shared_event_reports_owning_session(self):
        """归属会话随事件流转（接收方据此知道"这是谁的检查点"）。"""
        dis = dispatch_shared({"checkpoint_key": "cp", "verdict": WAIT,
                               "waiter_event": "push", "resolver_event": "push",
                               "facts": {"session_id": "repo-b/issue-7"}})
        assert dis["session_id"] == "repo-b/issue-7"

    def test_shared_event_does_not_read_local_checkpoint(self):
        """**反向对照**：本地存在同名检查点也**不得**影响跨仓判定。

        跨仓读本地 = 读到"别人的/不相干的"状态，正是"误放行 → 抢写 → 真冲突"的来源。
        """
        d = tempfile.mkdtemp()
        try:
            p = CheckpointProbe("repo-b", data_dir=d)
            p.create("cp", kind=CHECK_LOCK,
                     waiter_event="push", resolver_event="push",
                     note="本地同名检查点（配对成立）")
            # 跨仓事件**未带**事件名 → 必须降级（若"偷读本地"就会挂上 await）
            dis = dispatch_shared({"checkpoint_key": "cp", "verdict": WAIT})
            assert dis["action"] == "platform_crontab"
        finally:
            shutil.rmtree(d, ignore_errors=True)


# ------------------------------------------------------------------
# ⑤ 能力探测与降级：判不出就不增量（漏跑比多跑危险）
# ------------------------------------------------------------------

class TestCapabilityDegradation:
    def test_impact_available_when_concrete(self):
        """有版本 + 有具体受影响面 → 增量判重成立。"""
        got = impact_available("abc", base="def", impact=["a.py", "b.py"])
        assert got["available"] is True and got["degraded"] is False

    def test_missing_revision_degrades(self):
        """**反向对照**：连被测版本都不知道 → 降级（保守放行）。"""
        got = impact_available("", base="def", impact=["a.py"])
        assert got["available"] is False and got["degraded"] is True

    def test_missing_base_and_impact_degrades(self):
        """既没基线、也没算出变更集 → 判不出 → 降级。"""
        got = impact_available("abc")
        assert got["available"] is False and got["degraded"] is True
        assert "保守放行" in got["reason"]

    def test_empty_base_string_degrades_but_impact_alone_is_enough(self):
        """`base=""`（提供了但为空，如新建分支推送）= 无变更基准 → 降级。

        **反向对照**：`base=None`（未提供该字段）时，只要受影响面具体
        （调用方自己算好了），就不该被"没给 base"一票否决——否则日常全在降级。
        """
        assert impact_available("abc", base="", impact=["a.py"])["available"] is False
        assert impact_available("abc", base=None,
                                impact=["a.py"])["available"] is True

    def test_branch_create_degrades(self):
        """官方 ifModify 限制同源：新建分支无变更基准 → 降级。"""
        got = impact_available("abc", base="def", impact=["a.py"],
                               event="branch.create")
        assert got["available"] is False and got["degraded"] is True

    def test_empty_impact_with_base_degrades(self):
        """有基线但受影响面为空 → "算出来是空"与"算不出来"无法区分 → 降级。"""
        got = impact_available("abc", base="def", impact=[])
        assert got["available"] is False and got["degraded"] is True

    def test_guard_degrades_instead_of_skipping(self, probe):
        """`RunScopeGuard`：判不出影响面时**保守放行**（不能因判重而漏跑）。"""
        from secretary.collab import RunScopeGuard
        g = RunScopeGuard()
        kw = dict(mode="scan", revision="abc", impact=[], command="pytest -q tests/")
        first = g.decide(**kw)
        assert first.action == "run" and first.degraded is True
        assert first.degrade_reason
        g.record(**kw, verdict="passed")
        # **关键**：即便已登记过，判不出时仍放行（不 skip）
        again = g.decide(**kw, last_verdict="passed")
        assert again.action == "run" and again.degraded is True

    def test_guard_still_skips_when_impact_concrete(self):
        """**反向对照**：受影响面具体时，"同口径不重复"照常生效（降级别误伤判重）。"""
        from secretary.collab import RunScopeGuard, SKIP_DUPLICATE
        g = RunScopeGuard()
        kw = dict(mode="scan", revision="abc",
                  impact=["tests/test_a.py"], command="pytest -q tests/test_a.py")
        g.record(**kw, verdict="passed")
        assert g.decide(**kw, last_verdict="passed").action == SKIP_DUPLICATE

    def test_capability_report_names_known_cases(self):
        rep = capability_report()
        assert rep["degrade_policy"] and rep["known_degrade_cases"]
        assert "保守放行" in rep["degrade_policy"]


# ------------------------------------------------------------------
# ⑥ 官方最佳实践对照（"别人/官方怎么做的"）
# ------------------------------------------------------------------

class TestOfficialBestPracticeAlignment:
    def test_report_contains_official_capabilities(self):
        """对照表必须给出**官方能力 + 我们的对应 + 我们补的缺口**三层。"""
        caps = official_capabilities()
        for key in ("ai_review_plugin", "sca_plugin", "await_resolve"):
            blk = caps[key]
            assert blk["source"] and blk["capability"] and blk["ours"]
            assert blk["gap_we_close"], key

    def test_sca_increment_rules_are_referenced(self):
        """官方 SCA 的两条硬口径（同 CVE 不重复 / 内容未变的改名不重复）要在对照里。"""
        blob = str(official_capabilities())
        assert "同一组件同一 CVE" in blob or "CVE" in blob
        assert "重命名" in blob or "改名" in blob

    def test_await_constraints_are_documented_with_sources(self):
        """每条 await 硬约束都在册，且出处可追溯（不凭印象设计）。"""
        for key in ("same_event", "resolve_once", "deadlock", "stage_only"):
            assert key in AWAIT_CONSTRAINTS
        assert DOC_SOURCES["await"].startswith("/zh/build/")
        assert "crontab" in DOC_SOURCES and "code_review" in DOC_SOURCES

    def test_platform_report_is_self_consistent(self):
        """报告自洽：降级策略、key 方案、CPU 口径都在，且**没有**本地空转选项。"""
        rep = platform_report()
        assert "crontab" in rep["downgrade_policy"]
        assert "r<attempt>" in rep["await_key_scheme"]
        assert "sleep" in rep["local_cpu_note"]
        assert "local_loop" not in rep["downgrade_policy"]

    def test_min_interval_constants_agree_across_modules(self):
        """**单一语义源对拍**：两个模块的平台下限常量必须一致。"""
        assert PLN_MIN == PROBE_MIN == 300.0
        assert AWAIT_MAX_SEC == PROBE_MIN

    def test_no_local_sleep_anywhere_in_plan(self, probe):
        """**反向对照**：计划里的每一次等待都在平台侧，没有任何"本地等"。"""
        cp = probe.create("no-local", kind=CHECK_LOCK,
                          waiter_event="push", resolver_event="push")
        for row in probe.delay_plan(cp):
            assert row["local_cpu"].startswith("0")
            assert "local" not in row["driver"]
            assert "sleep" not in row["driver"]


# ------------------------------------------------------------------
# ⑦ 门面接线（角色/脚本只需调门面）
# ------------------------------------------------------------------

class TestFacadeWiring:
    def test_session_exposes_platform_and_capability(self):
        d = tempfile.mkdtemp()
        try:
            sec = SecretarySession("plat-s1", data_dir=d)
            rep = sec.platform_report()
            assert rep["official_capabilities"]["await_resolve"]["gap_we_close"]
            assert sec.capability_report()["degrade_policy"]
            assert sec.await_eligibility("push", "push")["eligible"] is True
            assert sec.await_eligibility("push", "issue.comment")["eligible"] is False
            assert sec.await_key_of("ck", 3) == "probe:ck:r3"
            assert sec.impact_availability("abc")["degraded"] is True
        finally:
            shutil.rmtree(d, ignore_errors=True)

    def test_session_await_plan_uses_checkpoint_context(self):
        """门面按检查点上下文给落点（配得上对 → await；否则 crontab）。"""
        d = tempfile.mkdtemp()
        try:
            sec = SecretarySession("plat-s2", data_dir=d)
            sec.probe.create("ok", kind=CHECK_LOCK,
                             waiter_event="push", resolver_event="push")
            assert sec.await_plan("ok")["driver"] == DRIVER_AWAIT
            sec.probe.create("bad", kind=CHECK_LOCK,
                             waiter_event="push", resolver_event="issue.comment")
            plan = sec.await_plan("bad")
            assert plan["driver"] == DRIVER_CRONTAB and plan["downgraded"] is True
        finally:
            shutil.rmtree(d, ignore_errors=True)

    def test_collab_status_still_consistent_after_alignment(self):
        """对齐后协同状态快照仍然自洽（没把既有口径改坏）。"""
        d = tempfile.mkdtemp()
        try:
            sec = SecretarySession("plat-s3", data_dir=d)
            st = sec.collab_status()
            assert st["waiting_plan"] and st["waiting_ladder"]
            assert st["source"]
            assert st["platform_cost"]["waiting_billed"] is False
            # 探测口径快照（含固定 cron 与规则来源层级）仍然可读
            ps = sec.probe_status()
            assert ps["crontab_interval"] and ps["split_note"]
            assert ps["local_cpu_cost"].startswith("0")
        finally:
            shutil.rmtree(d, ignore_errors=True)


# ------------------------------------------------------------------
# ⑧ 分发到组织（各仓库通用）的**可移植性**
# ------------------------------------------------------------------

class TestDistributionPortability:
    def test_org_rules_alone_are_enough(self):
        """只放一份组织级规则包（各仓分发的最小内容），从任意子目录都能取到同一份。"""
        from secretary.collab import load_rules
        src = os.path.join(REPO_ROOT, "shared", "collab", "organization.yaml")
        assert os.path.isfile(src), src
        d = tempfile.mkdtemp()
        try:
            nested = os.path.join(d, "a", "b", "c")
            os.makedirs(os.path.join(d, "shared", "collab"))
            os.makedirs(nested)
            shutil.copy(src, os.path.join(d, "shared", "collab",
                                          "organization.yaml"))
            rules = load_rules(search_from=nested)
            assert rules.scope == "organization"
            assert rules.waiting_ladder() == [10, 30, 60, 180, 360, 600]
            assert rules.probe_max_checks == 6
            assert rules.crontab_every_minutes() == 5
        finally:
            shutil.rmtree(d, ignore_errors=True)

    def test_distribution_script_exists_and_is_valid_bash(self):
        """分发脚本必须在仓里、可执行，且**语法合法**（分发本身也要可执行）。"""
        import subprocess
        script = os.path.join(REPO_ROOT, "scripts", "distribute-collab-rules.sh")
        assert os.path.isfile(script)
        assert os.access(script, os.X_OK), "分发脚本需可执行"
        proc = subprocess.run(["bash", "-n", script], capture_output=True)
        assert proc.returncode == 0, proc.stderr.decode()

    def test_distribution_defaults_to_dry_run(self):
        """**安全默认**：不带 `--apply` 只 dry-run（不会误写别人仓库）。"""
        import subprocess
        script = os.path.join(REPO_ROOT, "scripts", "distribute-collab-rules.sh")
        proc = subprocess.run(
            ["bash", script, "--repos", "dongdongxiaozu/zhuanyemishutuan"],
            capture_output=True, text=True)
        assert proc.returncode == 0
        assert "dry-run" in proc.stdout
        assert "将写入" in proc.stdout
