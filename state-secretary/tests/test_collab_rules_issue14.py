"""Issue #14 追问回归：阶梯退避（10s→30s→60s→3min→6min→10min 到顶即停）
+ 跨仓库全局协同规则包 + 等待不占构建 CPU（平台挂起语义）

需求口径（用户）：
  1. **从少到多符合逻辑**，大部分很快就没问题 → 第一次 **10 秒**，然后 **30s、60s、
     3 分钟、6 分钟**，**持续增加到 10 分钟就停止**；
  2. 这部分算 **agent 协同时的规则**，**适合全局、跨仓库**；
  3. **等待会不会占用构建 CPU 计算时间**（平台据此收费）——会不会消耗，
     还是被**挂起**的等待。

判别性设计（关键断言配反向对照，防恒真）：
  - 「从少到多」：阶梯**定值**且单调不减（反向：倍率推算落不到这串数字上）；
  - 「10 分钟就停止」：到顶后**不再增长**、`stopped=True`、到顶次数用尽→`resume_required`；
  - 「大部分很快就没问题」：前两档 ≤30s（反向：不是一上来就 3 分钟）；
  - 「不缩短承诺」：重触发等待**只增不减**（反向：给一个更长的上一档也不会被拉回）；
  - 「全局跨仓」：规则包从任意子目录向上都取到**同一份**（反向：环境变量可覆写）；
  - 「不占 CPU」：等待模式**永不**是"本地空转"（反向对照：空转 10min 要烧 1.33 核时）。
"""
from __future__ import annotations

import os
import shutil
import tempfile

import pytest

from secretary.collab import (
    DEFERRED_REVIEW,
    PLATFORM_SUSPEND,
    RESUME_REQUIRED,
    CollabRules,
    StepBackoff,
    WaitPolicy,
    cpu_cost_of_sleep,
    load_rules,
)
from secretary.collab.backoff import DEFAULT_LADDER
from secretary.session import SecretarySession

SESSION = "i14"


class FakeClock:
    def __init__(self, t: float = 1_000_000.0):
        self.t = t

    def __call__(self) -> float:
        return self.t

    def advance(self, sec: float) -> None:
        self.t += sec


@pytest.fixture
def env():
    d = tempfile.mkdtemp()
    clock = FakeClock()
    rev = SecretarySession(SESSION, data_dir=d, role_name="secretary.review")
    rev.review_arbiter._clock = clock
    rev.lock_registry._clock = clock
    try:
        yield d, clock, rev
    finally:
        shutil.rmtree(d, ignore_errors=True)


# ------------------------------------------------------------------
# ① 阶梯本身：从少到多，到顶即停
# ------------------------------------------------------------------

class TestLadder:
    def test_ladder_is_exactly_the_agreed_values(self):
        """阶梯就是约定的六档：10/30/60/180/360/600（秒）。"""
        assert list(DEFAULT_LADDER) == [10.0, 30.0, 60.0, 180.0, 360.0, 600.0]
        bo = StepBackoff()
        assert [bo.delay_for(i) for i in range(6)] == [10.0, 30.0, 60.0,
                                                      180.0, 360.0, 600.0]

    def test_from_less_to_more(self):
        """从少到多：严格单调不减（反向：出现回退即失败）。"""
        bo = StepBackoff()
        seq = [bo.delay_for(i) for i in range(len(DEFAULT_LADDER) + 3)]
        assert seq == sorted(seq), f"必须从少到多: {seq}"

    def test_early_steps_are_short(self):
        """「大部分很快就没问题」：前两档都很短（≤30s）。"""
        bo = StepBackoff()
        assert bo.delay_for(0) <= 10.0
        assert bo.delay_for(1) <= 30.0

    def test_stops_at_ten_minutes(self):
        """「持续增加到 10 分钟就停止」：到顶后**不再增长**并给 stopped=True。"""
        bo = StepBackoff()
        top_step = len(DEFAULT_LADDER) - 1
        assert bo.delay_for(top_step) == 600.0
        assert bo.delay_for(top_step + 5) == 600.0, "到顶后不得继续增长"
        st = bo.status(top_step)
        assert st.at_top is True and st.stopped is True
        assert st.delay_sec == 600.0

    def test_not_top_before_top(self):
        """反向对照：未到顶时**不**判 stopped（防恒真）。"""
        bo = StepBackoff()
        st = bo.status(1)
        assert st.at_top is False and st.stopped is False
        assert st.delay_sec == 30.0

    def test_ten_minutes_is_the_hard_ceiling(self):
        """10 分钟是硬顶：任何等待都不会超过它（反向：不是 15 分钟）。"""
        bo = StepBackoff()
        assert bo.capped(99999) == 600.0
        assert max(r["delay_sec"] for r in bo.schedule()) == 600.0

    def test_retrigger_never_shortens(self):
        """重触发：只增不减（给一个比下一档更长的上一档，也不会被拉回短档）。"""
        bo = StepBackoff()
        assert bo.retrigger(0, previous_wait_sec=0) == 30.0
        assert bo.retrigger(0, previous_wait_sec=999) == 999.0, "不得缩短已有承诺"
        assert bo.retrigger(99, previous_wait_sec=0) == 600.0

    def test_rejects_non_monotonic_ladder(self):
        """非法阶梯（回退档位）拒绝构造——"等待被缩短"在入口就挡掉。"""
        with pytest.raises(ValueError):
            StepBackoff(ladder=[60, 30, 180])
        with pytest.raises(ValueError):
            StepBackoff(ladder=[])
        with pytest.raises(ValueError):
            StepBackoff(ladder=[10, 0, 30])

    def test_schedule_and_human_view(self):
        """可视化：全阶梯时间表 + 一行人类可读计划（供文档/角色提示词引用）。"""
        bo = StepBackoff()
        rows = bo.schedule()
        assert [r["delay_sec"] for r in rows] == [10.0, 30.0, 60.0,
                                                  180.0, 360.0, 600.0]
        assert rows[-1]["stopped"] is True
        human = bo.human()
        assert "10s" in human and "600s" in human and "到顶即停" in human


# ------------------------------------------------------------------
# ② 跨仓库全局规则包
# ------------------------------------------------------------------

class TestGlobalRules:
    def test_rule_package_found_from_subdir(self):
        """跨仓库规则包：从**任意子目录**向上都能取到同一份（全局生效）。"""
        repo_root = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
        deep = os.path.join(repo_root, "state-secretary", "src", "secretary")
        rules = load_rules(search_from=deep)
        assert rules.source.endswith("collab-rules.yaml"), rules.source
        assert rules.waiting_ladder() == list(DEFAULT_LADDER)

    def test_rule_package_not_found_falls_back(self):
        """反向对照：找不到规则包 → 回落内置默认（不抛、不静默乱读）。"""
        d = tempfile.mkdtemp()
        try:
            rules = load_rules(search_from=os.path.join(d, "nowhere"))
            assert "default" in rules.source
            assert rules.waiting_ladder() == list(DEFAULT_LADDER)
        finally:
            shutil.rmtree(d, ignore_errors=True)

    def test_rules_carry_loop_and_scan(self):
        """规则包同时收敛熔断预算与审查范围口径（一处定义，全域生效）。"""
        rules = CollabRules.from_dict({
            "loop": {"max_rounds": 2, "max_advisory_fix_rounds": 0,
                     "max_total_rounds": 4, "max_stall_repeats": 1},
            "scan": {"major_version_tags": ["release-major"]},
        })
        assert rules.max_rounds == 2
        assert rules.max_advisory_fix_rounds == 0
        assert rules.max_total_rounds == 4
        assert rules.major_version_tags == ["release-major"]

    def test_bad_rules_shape_degrades(self):
        """宽进：形状不符的字段一律回落默认（不因一个坏字段整包失败）。"""
        rules = CollabRules.from_dict({
            "backoff": {"ladder": "not-a-list", "max_attempts": "x"},
            "locks": {"retry_delay_sec": True},
            "loop": None,
        })
        assert rules.waiting_ladder() == list(DEFAULT_LADDER)
        assert rules.lock_retry_delay_sec == 60.0
        assert rules.max_rounds == 3

    def test_session_uses_rule_package(self, env):
        """会话默认装载规则包：等待阶梯 / 熔断预算都来自规则包（非硬编码）。"""
        _, _, rev = env
        assert rev.collab_rules.waiting_ladder() == list(DEFAULT_LADDER)
        assert rev.review_arbiter.retry_delay_sec == 10.0, "首个延迟窗 = 阶梯首档"
        assert rev.review_arbiter.persistent_grace_sec == 180.0
        assert rev.loop_guard.max_rounds == 3

    def test_explicit_params_still_win(self):
        """反向对照：显式传参**优先于**规则包（调用方有明确口径时以调用方为准）。"""
        d = tempfile.mkdtemp()
        try:
            s = SecretarySession(SESSION, data_dir=d, retry_delay_sec=45.0,
                                 max_rounds=7)
            assert s.review_arbiter.retry_delay_sec == 45.0
            assert s.backoff is None, "显式固定窗口时不启用阶梯"
            assert s.loop_guard.max_rounds == 7
        finally:
            shutil.rmtree(d, ignore_errors=True)

    def test_collab_status_snapshot(self, env):
        """生效快照：数值从哪来、等待计划是什么、等待算不算计费，一眼可见。"""
        _, _, rev = env
        st = rev.collab_status()
        assert st["waiting_ladder"] == list(DEFAULT_LADDER)
        assert st["waiting_plan"] == rev.waiting_plan()
        assert st["platform_cost"]["waiting_billed"] is False

    def test_summary_carries_collab(self, env):
        """向上汇总带协同规则快照：上级不必回读会话本地状态。"""
        _, _, rev = env
        assert rev.summarize_for_upstream()["collab"]["waiting_ladder"]


# ------------------------------------------------------------------
# ③ 等待不占构建 CPU（挂起 vs 空转）
# ------------------------------------------------------------------

class TestWaitDoesNotBurnCpu:
    def test_waiting_is_not_billed(self):
        """平台口径：**没有"等待"这一计费项**（反向：waiting_billed 断言为 False）。"""
        rules = CollabRules()
        assert rules.platform.waiting_billed is False
        assert "等待" in rules.platform.waiting_note

    def test_decision_never_says_local_spin(self):
        """决策永不产出"本地空转"：全部落在这三种模式之一。"""
        wp = WaitPolicy()
        modes = [wp.decide(i).mode for i in range(8)]
        assert set(modes) <= {PLATFORM_SUSPEND, DEFERRED_REVIEW, RESUME_REQUIRED}
        assert modes[0] == PLATFORM_SUSPEND

    def test_suspend_then_resume_required(self):
        """未到顶 → 平台挂起；到顶且用尽 → 交回人工（不空等）。"""
        wp = WaitPolicy()
        assert wp.decide(0).mode == PLATFORM_SUSPEND
        assert wp.decide(0).delay_sec == 10.0
        last = wp.decide(5)
        assert last.mode == RESUME_REQUIRED
        assert last.resume_required is True and last.delay_sec == 0.0

    def test_editing_false_needs_no_wait(self):
        """对方已处理完 → 无需等待，直接去做评审（反向对照：延迟为 0）。"""
        wp = WaitPolicy()
        d = wp.decide(3, editing=False)
        assert d.delay_sec == 0.0
        assert d.mode == DEFERRED_REVIEW
        assert d.resume_required is False

    def test_local_cpu_cost_is_zero(self):
        """三种模式的本地 CPU 代价都是 0（等待不占构建核时）。"""
        wp = WaitPolicy()
        for i in (0, 3, 5):
            assert "0" in wp.decide(i).local_cpu_cost

    def test_counterfactual_quantifies_spin(self):
        """反例对照：本地空转确有其代价（8 核 10 分钟 ≈ 1.33 核时）。"""
        cost = cpu_cost_of_sleep(600, cores=8)
        assert cost["core_hours"] == pytest.approx(1.3333, abs=1e-3)
        assert cost["yuan"] == pytest.approx(0.1667, abs=1e-3)
        assert "空转" in cost["note"]

    def test_explain_answers_the_question(self):
        """口径说明直接回答"等待会不会占 CPU"（供所有角色统一答复）。"""
        info = WaitPolicy().explain()
        assert "不计 CPU" in info["answer"]
        assert info["platform"]["waiting_billed"] is False
        assert "sleep" in info["forbidden"]

    def test_session_exposes_wait_plan(self, env):
        """会话门面可直接给出等待计划与"怎么等"的决策。"""
        _, _, rev = env
        assert [p["mode"] for p in rev.wait_plan()] == [
            PLATFORM_SUSPEND] * 5 + [RESUME_REQUIRED]
        assert rev.wait_decision(0)["delay_sec"] == 10.0
        assert rev.cost_note()["waiting_billed"] is False
        assert "waiting" in rev.collab_status()


# ------------------------------------------------------------------
# ④ 端到端：会话里的等待真的按阶梯走（含到顶即停）
# ------------------------------------------------------------------

class TestLadderEndToEnd:
    def _edit(self, rev):
        from secretary.state import CardLease, OwnershipGuard
        og = OwnershipGuard(SESSION, rev.state_manager, secretary="dev")
        og.guarded_add_task("开发产出")
        og.lock_registry = rev.lock_registry
        og._store_lease(CardLease("dev"))
        return og

    def test_defer_windows_follow_ladder(self, env):
        """连续触发同一把锁：等待依次 10 → 30 → 60 → 180 → 360 → 600 封顶。"""
        _, clock, rev = env
        rev.begin_review()
        self._edit(rev)
        waits = []
        for i in range(7):
            dec = rev.apply_review(note=f"评审{i}")
            waits.append(round(dec.retry_after - clock()))
        assert waits[:6] == [10, 30, 60, 180, 360, 600]
        assert waits[6] == 600, "到顶后停在 10 分钟（不再增长）"

    def test_top_step_does_not_rearm_forever(self, env):
        """到顶即停：顶档不会把检测点无限往后推（反向：detect 点不前进）。"""
        _, clock, rev = env
        rev.begin_review()
        self._edit(rev)
        for i in range(6):
            rev.apply_review(note=f"评审{i}")
        lock_before = rev.lock_registry.get(rev.review_arbiter.defer_lock_key())
        clock.advance(601)                      # 顶档到点
        dec = rev.retry_review(note="到点检测")
        lock_after = rev.lock_registry.get(rev.review_arbiter.defer_lock_key())
        assert dec.should_retry is True
        assert lock_after.retry_delay_sec == lock_before.retry_delay_sec == 600.0

    def test_waiting_status_reports_top(self, env):
        """一眼看清当前档位：到顶后 `stopped=True`。"""
        _, _, rev = env
        rev.begin_review()
        self._edit(rev)
        for i in range(6):
            rev.apply_review(note=f"评审{i}")
        st = rev.waiting_status(5)
        assert st["at_top"] is True and st["stopped"] is True
        assert st["label"].startswith("600s")

    def test_first_wait_is_short(self, env):
        """「大部分很快就没问题」：第一次只等 10 秒（反向：不是 1 分钟）。"""
        _, clock, rev = env
        rev.begin_review()
        self._edit(rev)
        dec = rev.apply_review(note="评审")
        assert dec.retry_after == pytest.approx(clock() + 10.0)
