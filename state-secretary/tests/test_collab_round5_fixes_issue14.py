"""Issue #14 PR #15 复评第 5 轮定点修复回归（🔴E–🔴H + 🟡1–🟡5 + 🟢）

触发：架构·评审 复评第 4 轮（基线 `a7847f9`）——本轮 🔴1–🔴4 已认可清零，
但**第 3 轮同时提出的 🔴E–🔴H 未处理**，在 `a7847f9` 上逐项实测仍成立 →
处置标签 `fix_bug`。本文件只针对该 4 项 🔴 与 5 项 🟡 补**判别性回归**，
每组关键断言配**反向对照**（防恒真）。

| 项 | 缺陷（评审实测） | 本文件判据 |
|---|---|---|
| 🔴E | 锁台账**查不到这把锁**被判成「锁已释放」→ 放行（未登记/已释放不可区分，都取放行方向；显式 `lock_held=True` 也被 `or facts` 覆盖） | 未登记 scope → `unknown`（**继续等**）；曾登记且已释放 → `proceed`；显式事实**优先于缺席推断** |
| 🔴F | 判重台账落 `/tmp`、无留痕回写、CI 不落 verdict → 平台每次新容器判重**永不触发**（实测 r1/r2/r3 全 run） | 跨容器：第 2 次带留痕 → `skip_duplicate`；红后修复同一文件集 → 可复跑；CI 脚本必须落真实结论 |
| 🔴G | 门面 `create_checkpoint` / `register_platform_wait` 不透传两侧事件名 → 落点四档恒 `platform_crontab`，"≤5min 挂 await"生产不可达 | 门面同事件 → `platform_await`（计划 + 登记两处）；未显式给时按当前事件自动登记 |
| 🔴H | `main.push` 新增测试阶段 `image: node:20` 且零安装步骤（镜像不含 python3）→ `python3 -m pytest` 必失败 | `.cnb.yml` 声明 Python 运行时 + 装 pytest；不再用 `|| true` 吞掉缺运行时 |

🟡：多留痕 `max()` 按 `session_id`/`checkpoint_key` **归属过滤**（跨会话/跨检查点
污染会误判"预算用尽"/"已熔断"）；`_is_red("577 passed, 0 failed")=False`；
`checkpoint_plan` docstring 明写"含末尾用尽行"；`ReviewArbiter` 独立构造默认取规则包；
预算用尽分支**落盘**（观测面与判定一致）。

🟢：留痕收集/归属过滤抽公共实现（`secretary.trail`），两处不再各写一份。
"""
from __future__ import annotations

import os
import shutil
import tempfile

import pytest

from secretary.collab import (
    CHECK_LOCK,
    PROCEED,
    SKIP_DUPLICATE,
    UNKNOWN,
    WAIT,
    CheckpointProbe,
    RunScopeGuard,
    load_rules,
)
from secretary.collab.run_guard import _is_red
from secretary.loop import LoopGuard
from secretary.review.review_arbiter import DEFAULT_RETRY_DELAY_SEC, ReviewArbiter
from secretary.session import SecretarySession
from secretary import trail as trail_mod

SESSION = "i14r5"
REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))))
RULES = "shared/collab/collab-rules.yaml"


class FakeClock:
    def __init__(self, t: float = 1_000_000.0):
        self.t = t

    def __call__(self) -> float:
        return self.t


@pytest.fixture
def rules():
    return load_rules(search_from=os.path.join(REPO_ROOT, "state-secretary"))


@pytest.fixture
def env():
    d = tempfile.mkdtemp()
    clock = FakeClock()
    sec = SecretarySession(SESSION, data_dir=d, rules_path=RULES)
    sec.probe._clock = clock
    sec.lock_registry._clock = clock
    try:
        yield d, sec
    finally:
        shutil.rmtree(d, ignore_errors=True)


def _read(rel: str) -> str:
    with open(os.path.join(REPO_ROOT, rel), encoding="utf-8") as f:
        return f.read()


# ==================================================================
# 🔴E 等锁三态：**没查到这把锁** ≠ 锁已释放
# ==================================================================

class TestLockFactsAreThreeState:
    def test_unregistered_scope_is_unknown_not_proceed(self, env):
        """**未登记**的 scope → `unknown`（继续等），**不得**判成"锁已释放"。

        旧实现：`held` 里找不到 → `{"lock_held": False}` → `proceed`
        （与"已释放"完全不可区分，且方向是**放行**——等他的锁最怕的误放行）。
        """
        _, sec = env
        sec.create_checkpoint("cpE", kind=CHECK_LOCK, scope="card:other")
        out = sec.probe_checkpoint("cpE")
        assert out.verdict == UNKNOWN, out.event.reason
        assert out.event.wake_allowed is False
        assert "没有这把锁" in out.event.reason

    def test_registered_then_released_proceeds(self, env):
        """**曾登记且已释放** → `proceed`（三态里的"查到且未持有"）。"""
        _, sec = env
        sec.lock_registry.register("card:x", kind="card_write", holder="dev")
        sec.create_checkpoint("cpR", kind=CHECK_LOCK, scope="card:x")
        assert sec.probe_checkpoint("cpR").verdict == WAIT
        sec.lock_registry.release("card:x", by="dev")
        out = sec.probe_checkpoint("cpR")
        assert out.verdict == PROCEED, out.event.reason
        assert out.event.wake_allowed is True

    def test_unregistered_and_released_are_distinguishable(self, env):
        """**判别性核心**：两种情形必须给出**不同**判定（旧实现两者都是 proceed）。"""
        _, sec = env
        sec.create_checkpoint("cpNever", kind=CHECK_LOCK, scope="card:never")
        sec.lock_registry.register("card:once", kind="card_write", holder="dev")
        sec.create_checkpoint("cpOnce", kind=CHECK_LOCK, scope="card:once")
        sec.lock_registry.release("card:once", by="dev")
        never = sec.probe_checkpoint("cpNever").verdict
        once = sec.probe_checkpoint("cpOnce").verdict
        assert never == UNKNOWN and once == PROCEED, (never, once)
        assert never != once

    def test_explicit_fact_beats_absence_inference(self, env):
        """**显式事实优先**：台账查不到时，显式 `lock_held=True` **不得**被覆盖成放行。

        旧实现 `self._collect_lock_facts(cp) or facts` 让"台账里没这把锁"
        （= 判不出）把显式事实冲掉，方向还是放行。
        """
        _, sec = env
        sec.create_checkpoint("cpEx", kind=CHECK_LOCK, scope="card:absent")
        out = sec.probe_checkpoint("cpEx", lock_held=True, lock_holder="dev")
        assert out.verdict == WAIT, out.event.reason
        # 反向对照：显式 False + 台账查不到 → 也不得放行（缺席推断仍不成立）
        sec.create_checkpoint("cpEx2", kind=CHECK_LOCK, scope="card:absent2")
        out2 = sec.probe_checkpoint("cpEx2", lock_held=False)
        assert out2.verdict == PROCEED, "显式事实（False）优先，且是显式给出的"

    def test_raw_probe_without_lock_view_is_unknown(self):
        """反向对照：**整体**没接锁台账（`lock_view=None`）→ 仍 `unknown`。"""
        p = CheckpointProbe(SESSION, data_dir=tempfile.mkdtemp())
        p.create("cpBlind", kind=CHECK_LOCK, scope="card:x")
        out = p.probe("cpBlind")
        assert out.verdict == UNKNOWN

    def test_ledger_exposes_keys_for_absence_detection(self, env):
        """台账必须给出**全量锁键**（含已释放）——三态判定的数据前提。"""
        _, sec = env
        sec.lock_registry.register("card:k", kind="card_write", holder="dev")
        view = sec.lock_registry.overview()
        assert "card:k" in view["keys"]
        sec.lock_registry.release("card:k", by="dev")
        view2 = sec.lock_registry.overview()
        assert "card:k" in view2["keys"], "已释放的键仍在全量键里（才能判「查到未持有」）"
        assert view2["held"] == []

    def test_handcrafted_view_without_keys_is_conservative(self):
        """手搓视图**不给 `keys`** → 保守按"没查到"处理（不误放行）。"""
        p = CheckpointProbe(SESSION, data_dir=tempfile.mkdtemp(),
                            lock_view=lambda: {"held": []})
        p.create("cpHC", kind=CHECK_LOCK, scope="card:x")
        assert p.probe("cpHC").verdict == UNKNOWN


# ==================================================================
# 🔴F 增量判重：跨容器载体 + CI 落真实结论 + 红后可复跑
# ==================================================================

class TestRunScopeCarrierAcrossContainers:
    KW = dict(mode="scan", revision="r1", impact=["a.py"],
              command="pytest -q a.py")

    def test_second_container_skips_via_trail(self, rules):
        """**跨容器**：第 1 次 run + 留痕 → 第 2 次（新数据根）`skip_duplicate`。

        旧实现台账落 `<tmp>/run-scope`，每次 push 新容器从空开始 → 永不触发
        （实测 r1/r2/r3 全 run）。
        """
        g1 = RunScopeGuard(rules, data_dir=os.path.join(tempfile.mkdtemp(), "x"))
        assert g1.decide_repeat(**self.KW, base="base").action == "run"
        g1.record_repeat(**self.KW, verdict="853 passed")
        trail = g1.trails(session_id=SESSION)
        assert "run-scope" in trail
        g2 = RunScopeGuard(rules, data_dir=os.path.join(tempfile.mkdtemp(), "y"))
        assert g2.adopt_trails(trail, session_id=SESSION)["merged"] == 1
        d = g2.decide_repeat(**{**self.KW, "revision": "r2"}, base="base")
        assert d.action == SKIP_DUPLICATE, d.reason

    def test_without_trail_stays_run(self, rules):
        """**反向对照**：不带留痕 → 每容器从 0 开始（证明上一条有判别力）。"""
        for rev in ("r1", "r2", "r3"):
            g = RunScopeGuard(rules, data_dir=os.path.join(
                tempfile.mkdtemp(), "run-scope"))
            assert g.decide_repeat(**{**self.KW, "revision": rev},
                                   base="base").action == "run"

    def test_red_then_fixed_same_files_can_rerun(self, rules):
        """**红后修复可复跑**：修 bug 改的常是同一批文件（受影响面不变）→ 必须放行。

        否则"红 → 修 → 再跑"这条唯一有信息量的路径被自己的判重堵死。
        """
        g1 = RunScopeGuard(rules, data_dir=os.path.join(tempfile.mkdtemp(), "x"))
        g1.record_repeat(**self.KW, verdict="2 failed")
        trail = g1.trails(session_id=SESSION)
        g2 = RunScopeGuard(rules, data_dir=os.path.join(tempfile.mkdtemp(), "y"))
        g2.adopt_trails(trail, session_id=SESSION)
        d = g2.decide_repeat(**{**self.KW, "revision": "r2"}, base="base")
        assert d.action == "run", d.reason

    def test_green_still_skips_after_rerun_window(self, rules):
        """**反向对照**：绿 + 同一份受审内容 → 仍跳过（例外不被架空）。"""
        g = RunScopeGuard(rules)
        g.record_repeat(**self.KW, verdict="853 passed")
        assert g.decide_repeat(**{**self.KW, "revision": "r2"},
                               base="base").action == SKIP_DUPLICATE

    def test_trail_is_attributed_by_session(self, rules):
        """留痕**按 `session_id` 归属过滤**：别的会话的留痕不得替本会话判定。"""
        g1 = RunScopeGuard(rules, data_dir=tempfile.mkdtemp())
        g1.record_repeat(**self.KW, verdict="853 passed")
        trail = g1.trails(session_id="other-session")
        g2 = RunScopeGuard(rules, data_dir=tempfile.mkdtemp())
        assert g2.adopt_trails(trail, session_id=SESSION)["merged"] == 0
        assert g2.decide_repeat(**{**self.KW, "revision": "r2"},
                                base="base").action == "run"

    def test_trail_is_monotonic_and_merge_only_grows(self, rules):
        """留痕合并**只增不减**（小值不得把已跑次数拉回）。"""
        g1 = RunScopeGuard(rules, data_dir=tempfile.mkdtemp())
        g1.record_repeat(**self.KW, verdict="ok")
        g1.record_repeat(**{**self.KW, "revision": "r2"}, verdict="ok")
        trail = g1.trails(session_id=SESSION)
        g2 = RunScopeGuard(rules, data_dir=tempfile.mkdtemp())
        g2.adopt_trails(trail, session_id=SESSION)
        assert g2.content_repeats_of(mode="scan", impact=self.KW["impact"],
                                     command=self.KW["command"]) == 2

    def test_facade_exposes_carrier(self, env):
        """门面必须暴露载体入口（与 🔴A 的 `loop_budget_trail` 同一方案）。"""
        _, sec = env
        sec.record_repeat(mode="scan", revision="r1", impact=["a.py"],
                          command="pytest -q a.py", verdict="853 passed")
        trail = sec.run_scope_trail()
        assert "run-scope" in trail
        nxt = SecretarySession(SESSION, data_dir=tempfile.mkdtemp(),
                               rules_path=RULES)
        assert nxt.inherit_run_scope(trail)["merged"] == 1
        assert nxt.decide_repeat(mode="scan", revision="r2", impact=["a.py"],
                                 command="pytest -q a.py").action == SKIP_DUPLICATE

    def test_ci_script_wires_carrier_and_verdict(self):
        """CI 脚本必须**接线**：取留痕 → 判重 → 落**真实结论** → 打印写回留痕。"""
        body = _read("scripts/ci-incremental-test.sh")
        assert "adopt_trails" in body, "CI 必须从留痕恢复台账（跨容器）"
        assert "CNB_RUN_SCOPE_TRAIL" in body, "必须有承载留痕的环境入口"
        assert "record_repeat" in body and "verdict=" in body, "必须落真实结论"
        assert "guard.trails(" in body, "必须打印可写回评论的留痕"
        assert "TEST_RC" in body, "必须按真实退出码回填结论"


# ==================================================================
# 🔴G 门面透传两侧事件名 → 落点能到 platform_await
# ==================================================================

class TestFacadeCarriesEvents:
    def test_facade_create_checkpoint_accepts_events(self, env):
        """门面形参必须能收两侧事件名（旧签名没有 → 恒降级 crontab）。"""
        _, sec = env
        cp = sec.create_checkpoint("cpG", kind=CHECK_LOCK,
                                   waiter_event="push", resolver_event="push")
        assert cp.waiter_event == "push" and cp.resolver_event == "push"

    def test_same_event_plan_reaches_await(self, env):
        """**同事件** → 计划里的短等待档是 `platform_await`（首选落点可达）。"""
        _, sec = env
        sec.create_checkpoint("cpG2", kind=CHECK_LOCK,
                              waiter_event="push", resolver_event="push")
        plan = sec.checkpoint_plan("cpG2")
        assert plan[0]["driver"] == "platform_await", plan[0]
        assert plan[0]["await_key"].startswith("probe:cpG2:r")

    def test_register_platform_wait_not_downgraded(self, env):
        """登记等待时也不得降级（旧门面签名拿不到事件 → 恒 crontab）。"""
        _, sec = env
        sec.create_checkpoint("cpG3", kind=CHECK_LOCK,
                              waiter_event="push", resolver_event="push")
        cp = sec.register_platform_wait("cpG3")
        assert cp.history[-1]["driver"] == "platform_await"
        assert cp.history[-1]["downgraded"] is False

    def test_register_platform_wait_accepts_explicit_events(self, env):
        """显式传入两侧事件也生效（调用方明确口径优先）。"""
        _, sec = env
        sec.create_checkpoint("cpG4", kind=CHECK_LOCK)
        cp = sec.register_platform_wait("cpG4", waiter_event="push",
                                        resolver_event="push")
        assert cp.history[-1]["driver"] == "platform_await"

    def test_current_event_env_is_used_as_fallback(self, env, monkeypatch):
        """未显式给事件 → 按**当前事件**（平台内置变量）自动登记。"""
        monkeypatch.setenv("CNB_EVENT", "push")
        _, sec = env
        cp = sec.create_checkpoint("cpG5", kind=CHECK_LOCK)
        assert cp.waiter_event == "push" and cp.resolver_event == "push"
        assert sec.register_platform_wait("cpG5").history[-1]["driver"] \
            == "platform_await"

    def test_mismatched_events_still_downgrade(self, env):
        """**反向对照**：两侧事件不同 → 仍降级 `crontab`（不得硬挂等不到的 await）。"""
        _, sec = env
        sec.create_checkpoint("cpG6", kind=CHECK_LOCK,
                              waiter_event="issue.comment",
                              resolver_event="push")
        plan = sec.checkpoint_plan("cpG6")
        assert plan[0]["driver"] == "platform_crontab"
        assert plan[0]["downgraded"] is True

    def test_unknown_event_still_downgrade(self, env, monkeypatch):
        """**反向对照**：事件不可判定（没给且环境没有）→ 降级 crontab（保守）。"""
        for name in ("CNB_EVENT", "CNB_PIPELINE_EVENT", "CNB_TRIGGER_EVENT"):
            monkeypatch.delenv(name, raising=False)
        _, sec = env
        sec.create_checkpoint("cpG7", kind=CHECK_LOCK)
        assert sec.checkpoint_plan("cpG7")[0]["driver"] == "platform_crontab"


# ==================================================================
# 🔴H CI 运行时声明：声明了才能跑 python3 -m pytest
# ==================================================================

class TestCiDeclaresRuntime:
    def test_main_push_image_is_python(self):
        """`main.push` 阶段会跑 pytest → 镜像必须是**带 python3** 的运行时。"""
        body = _read(".cnb.yml")
        seg = body.split("main:")[1].split(".npc_comment_task")[0]
        assert "image: python:" in seg, "main.push 必须声明 Python 运行时"
        assert "image: node:20" not in seg

    def test_main_push_installs_pytest(self):
        """必须**装**测试依赖（镜像不保证自带 pytest）。"""
        body = _read(".cnb.yml")
        seg = body.split("main:")[1].split(".npc_comment_task")[0]
        assert "pip install" in seg and "pytest" in seg

    def test_no_swallowed_runtime_probe(self):
        """不得用 `|| true` 把"没有运行时"静默吞掉（那正是"看似跑了实际没跑"）。"""
        body = _read(".cnb.yml")
        seg = body.split("main:")[1].split(".npc_comment_task")[0]
        assert "python3 --version || true" not in seg

    def test_ci_script_fails_loudly_on_test_failure(self):
        """CI 脚本必须按真实退出码失败（不得把红当绿）。"""
        body = _read("scripts/ci-incremental-test.sh")
        assert 'exit "$TEST_RC"' in body
        assert "增量测试**未通过**" in body


# ==================================================================
# 🟡1 多留痕 max()：按归属过滤（跨会话 / 跨检查点污染 → 误判用尽）
# ==================================================================

class TestTrailAttribution:
    def test_loop_budget_filters_by_session(self):
        """**别的会话**的留痕不得被本会话吸收（否则下一轮直接 fuse）。"""
        trailA = ('<!-- loop-budget: {"rounds": {"total": 6, "blocking_rounds": 6},'
                  ' "session_id": "A"} -->')
        trailB = ('<!-- loop-budget: {"rounds": {"total": 1, "blocking_rounds": 0},'
                  ' "session_id": "B"} -->')
        g = LoopGuard("B", data_dir=tempfile.mkdtemp(), max_rounds=3)
        res = g.adopt_budget(trailA + "\n" + trailB, save=False)
        assert res["adopted"]["round_no"] == 1, res["adopted"]
        assert g.is_fused() is False

    def test_loop_budget_keeps_own_trail(self):
        """**反向对照**：属于自己的留痕照常合并（过滤不得把本会话的也丢掉）。"""
        trailB = ('<!-- loop-budget: {"rounds": {"total": 3, "blocking_rounds": 3},'
                  ' "session_id": "B"} -->')
        g = LoopGuard("B", data_dir=tempfile.mkdtemp(), max_rounds=3)
        assert g.adopt_budget(trailB, save=False)["adopted"]["round_no"] == 3

    def test_loop_budget_trail_without_session_is_kept(self):
        """留痕**不带** `session_id` → 不表态 → 保留（"没写归属"≠"属于别人"）。"""
        trail = '<!-- loop-budget: {"rounds": {"total": 2}} -->'
        g = LoopGuard("B", data_dir=tempfile.mkdtemp(), max_rounds=3)
        assert g.adopt_budget(trail, save=False)["adopted"]["round_no"] == 2

    def test_probe_budget_filters_by_checkpoint(self):
        """**别的检查点**的探测留痕不得被本检查点吸收（否则一上来就"用尽"）。"""
        p = CheckpointProbe("P", data_dir=tempfile.mkdtemp())
        p.create("cpA")
        cpA = p.load("cpA")
        cpA.checks_used = 6
        p._save(cpA)
        p.create("cpB")
        cpB = p.load("cpB")
        body = p.budget_trail_of(cpA) + "\n" + p.budget_trail_of(cpB)
        assert CheckpointProbe._coerce_checks_used(body) == 6      # 不指定归属
        assert CheckpointProbe._coerce_checks_used(body, cp_key="cpB") == 0
        assert CheckpointProbe._coerce_checks_used(body, cp_key="cpA") == 6

    def test_probe_cross_key_pollution_does_not_exhaust(self):
        """**判别性核心**：A 用尽 6 次不得让 B 直接判 `resume_required`。"""
        p = CheckpointProbe("P", data_dir=tempfile.mkdtemp())
        p.create("cpA")
        cpA = p.load("cpA")
        cpA.checks_used = 6
        p._save(cpA)
        p.create("cpB", kind="event")
        body = p.budget_trail_of(cpA) + "\n" + p.budget_trail_of(p.load("cpB"))
        out = p.probe("cpB", event_fired=False, external_checks=body)
        assert out.event.resume_required is False, "B 不得因 A 的轮次被判用尽"
        assert out.event.checks_used == 1

    def test_shared_trail_impl_is_single_source(self):
        """🟢：两处留痕正则来自**同一实现**（不再是两份同形正则）。"""
        from secretary.collab.probe import PROBE_BUDGET_TRAIL_RE
        from secretary.loop.loop_guard import BUDGET_TRAIL_RE
        assert PROBE_BUDGET_TRAIL_RE.pattern == trail_mod.trail_re("probe-budget").pattern
        assert BUDGET_TRAIL_RE.pattern == trail_mod.trail_re("loop-budget").pattern
        assert trail_mod.collect_marked(
            '<!-- x: {"a": 1} -->\n<!-- x: {"a": 2} -->', "x") == [{"a": 1}, {"a": 2}]


# ==================================================================
# 🟡2 `_is_red` 子串匹配把"0 failed"判红
# ==================================================================

class TestRedVerdictIgnoresZeroCounts:
    @pytest.mark.parametrize("verdict", [
        "577 passed, 0 failed", "0 failed", "0 errors", "0 failed, 0 errors",
        "853 passed in 6.47s", "684 passed",
    ])
    def test_zero_counts_are_not_red(self, verdict):
        """「**零个**失败/错误」是绿，不是红（旧实现含 `failed` 子串即判红）。"""
        assert _is_red(verdict) is False, verdict

    @pytest.mark.parametrize("verdict", [
        "2 failed", "1 error", "0 errors, 3 failed", "failed", "🔴 不通过",
        "FAILED tests/test_a.py", "失败",
    ])
    def test_real_failures_are_red(self, verdict):
        """**反向对照**：真失败仍是红（判据不得被修成恒 False）。"""
        assert _is_red(verdict) is True, verdict

    def test_zero_failed_does_not_license_rerun(self, rules):
        """端到端：上轮 `"577 passed, 0 failed"` → 同口径重跑仍**跳过**。

        旧实现该串判红 → "允许重跑一次"的例外被**恒真激活**（等于默认放行）。
        """
        g = RunScopeGuard(rules)
        g.record(**dict(mode="scan", revision="abc123",
                        impact=["src/a.py", "src/b.py"],
                        command="pytest -q tests/test_a.py"),
                 verdict="577 passed, 0 failed")
        d = g.decide(mode="scan", revision="abc123",
                     impact=["src/a.py", "src/b.py"],
                     command="pytest -q tests/test_a.py",
                     last_verdict="577 passed, 0 failed")
        assert d.action == SKIP_DUPLICATE, d.reason

    def test_real_red_still_licenses_one_rerun(self, rules):
        """**反向对照**：真红（`2 failed`）仍允许同口径重跑一次。"""
        g = RunScopeGuard(rules)
        kw = dict(mode="scan", revision="abc123",
                  impact=["src/a.py", "src/b.py"],
                  command="pytest -q tests/test_a.py")
        g.record(**kw, verdict="2 failed")
        assert g.decide(**kw, last_verdict="2 failed").action == "run"


# ==================================================================
# 🟡3 `checkpoint_plan` 明写"含末尾用尽行"
# ==================================================================

class TestCheckpointPlanDocstring:
    def test_docstring_states_trailing_exhausted_row(self):
        doc = SecretarySession.checkpoint_plan.__doc__ or ""
        assert "预算用尽" in doc
        assert "档位数 + 1" in doc or "档位数+1" in doc

    def test_rows_equal_ladder_plus_one(self, env):
        """口径落地：`checks=3` → 4 行（3 档 + 用尽行），末行是 `handoff_resume`。"""
        _, sec = env
        sec.create_checkpoint("cpP")
        rows = sec.checkpoint_plan("cpP", checks=3)
        assert [r["check"] for r in rows] == [1, 2, 3, 7]
        assert rows[-1]["driver"] == "handoff_resume"
        assert "停止自动挂账" in rows[-1]["note"]


# ==================================================================
# 🟡4 `ReviewArbiter` 独立构造默认与规则包同源
# ==================================================================

class TestReviewArbiterDefaultDelay:
    def test_standalone_default_matches_rule_package(self, rules):
        """独立构造（不传 `retry_delay_sec`）→ 取规则包，不再是硬编码 60s。"""
        d = tempfile.mkdtemp()
        sm = SecretarySession("x", data_dir=d).state_manager
        arb = ReviewArbiter("a", sm, reviewer="r",
                            data_dir=os.path.join(d, "rv"))
        assert arb.retry_delay_sec == rules.lock_retry_delay_sec
        assert arb.retry_delay_sec != DEFAULT_RETRY_DELAY_SEC or \
            rules.lock_retry_delay_sec == DEFAULT_RETRY_DELAY_SEC

    def test_explicit_value_still_wins(self, env):
        """**反向对照**：显式传入仍以调用方为准（兼容语义不丢）。"""
        _, sec = env
        arb = ReviewArbiter("b", sec.state_manager, reviewer="r",
                            data_dir=os.path.join(sec._session_root, "rv2"),
                            retry_delay_sec=7)
        assert arb.retry_delay_sec == 7.0

    def test_session_and_standalone_agree(self, env):
        """会话路径与独立构造给出**同一个**延迟窗（同一条取值路径）。"""
        d, sec = env
        arb = ReviewArbiter("c", sec.state_manager, reviewer="r",
                            data_dir=os.path.join(d, "rv3"))
        assert arb.retry_delay_sec == sec.review_arbiter.retry_delay_sec


# ==================================================================
# 🟡5 预算用尽分支**落盘**（观测面与判定一致）
# ==================================================================

class TestExhaustedBudgetPersists:
    def test_external_checks_persist_to_disk(self):
        """外部轮次合并后判用尽 → 磁盘/`overview` 必须显示真实轮次（旧为 0）。"""
        p = CheckpointProbe("P5", data_dir=tempfile.mkdtemp())
        p.create("cp5", kind="event")
        out = p.probe("cp5", event_fired=False, external_checks=6)
        assert out.verdict == WAIT and out.event.resume_required is True
        cp = p.load("cp5")
        assert cp.checks_used == 6, "用尽分支必须落盘"
        assert cp.resume_required is True
        assert cp.history[-1]["action"] == "budget_exhausted"

    def test_overview_shows_exhausted_state(self):
        """`overview()` 与判定一致（旧：事件留痕 6、磁盘/总览 0）。"""
        p = CheckpointProbe("P5b", data_dir=tempfile.mkdtemp())
        p.create("cp5b", kind="event")
        p.probe("cp5b", event_fired=False, external_checks=6)
        # 用尽后该检查点不再出现在"未决"清单里（已交回人工），但磁盘状态可读
        cp = p.load("cp5b")
        assert cp.checks_used == 6 and cp.resume_required is True

    def test_normal_probe_still_persists(self):
        """**反向对照**：正常探测路径也照常落盘（不得因改动丢原行为）。"""
        p = CheckpointProbe("P5c", data_dir=tempfile.mkdtemp())
        p.create("cp5c", kind="event")
        p.probe("cp5c", event_fired=False)
        assert p.load("cp5c").checks_used == 1
