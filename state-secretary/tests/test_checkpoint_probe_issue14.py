"""Issue #14 追问回归：检查点探测器（**非轮询**探测点，最多 6 次）

需求口径（用户原话）：
  > **最多 6 次就行**，再多就不适合等待了，不是这里的问题了，或者我们是否可以以
  > **非轮询**而是**形成检查点然后 webhook 触发再跑**，**等他的锁**，然后形成一个
  > 机制**判断检查点**，类似于**边缘计算的探测器**这样的，会不会好点。怎么处理
  > 比较好，平台对这个有最佳实践吗

判别性设计（关键断言配**反向对照**，防恒真）：
  - 「最多 6 次」：预算用尽→`resume_required`（反向：第 6 次之前**不**判用尽）；
  - 「非轮询」：平台落点**永不**是本地空转；短等待挂 `await`、长等待交 `crontab`
    （反向：`crontab` 间隔**不低于平台 5 分钟下限**）；
  - 「等他的锁」：锁在→`wait`，锁释放→`proceed`（反向：**锁表不可见时判 unknown**，
    不误判成"已释放"）；
  - 「判断检查点」：判定只看可判定事实；**我方不再需要 → release**（反向：不会傻等到对方放锁）；
  - 「检查点幂等」：同键重建**不重置预算**（反向：重置即把"最多 6 次"变成无限次）；
  - 「不占 CPU」：探测落点全部零占用（反向对照：本地空转 10min ≈ 1.33 核时）。
"""
from __future__ import annotations

import os
import shutil
import tempfile

import pytest

from secretary.collab import (
    ALL_DRIVERS,
    CHECK_CONFIRM,
    CHECK_EVENT,
    CHECK_LOCK,
    CHECK_VERSION,
    CRONTAB_MIN_INTERVAL_SEC,
    DRIVER_AWAIT,
    DRIVER_CRONTAB,
    DRIVER_HANDOFF,
    PROCEED,
    RELEASE,
    UNKNOWN,
    WAIT,
    CheckpointProbe,
    StepBackoff,
    cpu_cost_of_sleep,
    load_rules,
)
from secretary.session import SecretarySession

SESSION = "i14probe"
RULES = "shared/collab/collab-rules.yaml"


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
    sec = SecretarySession(SESSION, data_dir=d, role_name="secretary.session",
                           rules_path=RULES)
    sec.probe._clock = clock
    sec.lock_registry._clock = clock
    try:
        yield d, clock, sec
    finally:
        shutil.rmtree(d, ignore_errors=True)


@pytest.fixture
def tmp_probe():
    d = tempfile.mkdtemp()
    p = CheckpointProbe("i14probe", data_dir=d, clock=FakeClock())
    try:
        yield p
    finally:
        shutil.rmtree(d, ignore_errors=True)


# ------------------------------------------------------------------
# ① 最多 6 次：次数上界 = 阶梯档位数，用尽即停止自动挂账
# ------------------------------------------------------------------

class TestBudget:
    def test_max_checks_default_is_six(self, tmp_probe):
        """「最多 6 次就行」：默认探测上界 = 等待阶梯档位数 = 6。"""
        assert tmp_probe.max_checks == 6
        assert len(tmp_probe.backoff.ladder) == 6

    def test_plan_has_six_plus_handoff(self, tmp_probe):
        """计划 = 6 次探测 + 1 条"预算用尽 → 交回人工"（不是第 7 次等待）。"""
        plan = tmp_probe.delay_plan()
        assert [r["check"] for r in plan] == [1, 2, 3, 4, 5, 6, 7]
        assert plan[-1]["driver"] == DRIVER_HANDOFF
        assert plan[-1]["delay_sec"] == 0.0
        assert "停止自动挂账" in plan[-1]["note"]

    def test_exhausted_only_after_six(self, tmp_probe):
        """反向对照：第 1..5 次**不**判用尽；第 6 次才判 → resume_required。"""
        tmp_probe.create("c1", kind=CHECK_EVENT)
        for i in range(5):
            out = tmp_probe.probe("c1", event_fired=False)
            assert out.verdict == WAIT
            assert out.event.resume_required is False, f"第 {i+1} 次不该用尽"
            assert out.event.checks_left == 5 - i
        out = tmp_probe.probe("c1", event_fired=False)
        assert out.verdict == WAIT
        assert out.event.resume_required is True
        assert out.event.checks_left == 0
        assert out.event.action == "human_resume"

    def test_probe_after_exhaustion_does_not_wake(self, tmp_probe):
        """用尽后再探：仍不唤醒、仍交回人工（不得据此再开一轮）。"""
        tmp_probe.create("c1", kind=CHECK_EVENT, max_checks=1)
        tmp_probe.probe("c1", event_fired=False)
        out = tmp_probe.probe("c1", event_fired=False)
        assert out.event.resume_required is True
        assert out.event.wake_allowed is False
        assert out.event.checks_used == 1, "用尽后不得继续消耗预算"

    def test_recreate_does_not_reset_budget(self, tmp_probe):
        """**幂等**：同键重建不重置预算（反向：重置 = 把"最多 6 次"变成无限次）。"""
        tmp_probe.create("c1", kind=CHECK_EVENT)
        tmp_probe.probe("c1", event_fired=False)
        again = tmp_probe.create("c1", kind=CHECK_EVENT, note="又遇到一次")
        assert again.checks_used == 1, "重建不得清零已用次数"
        assert again.max_checks == 6


# ------------------------------------------------------------------
# ② 非轮询：落点是平台语义（await / crontab / api_trigger），永不本地空转
# ------------------------------------------------------------------

class TestNonPolling:
    def test_short_waits_use_platform_await(self, tmp_probe):
        """短等待（≤5 分钟）挂 `cnb:await`：零占用，状态一变就醒。"""
        plan = tmp_probe.delay_plan()
        short = [r for r in plan if r["delay_sec"] and r["delay_sec"] <= 300]
        assert short, "应有短等待档"
        assert all(r["driver"] == DRIVER_AWAIT for r in short)
        assert all(r["crontab"] == "" for r in short)

    def test_long_waits_use_platform_crontab(self, tmp_probe):
        """长等待（>5 分钟）交**平台定时任务**到点代跑——平台是"不睡觉的进程"。"""
        plan = tmp_probe.delay_plan()
        long_rows = [r for r in plan if r["delay_sec"] > 300]
        assert [r["delay_sec"] for r in long_rows] == [360.0, 600.0]
        assert all(r["driver"] == DRIVER_CRONTAB for r in long_rows)
        # 已确认口径 #2：cron 间隔**固定 */5 一档扫**（不随档位放大）
        assert long_rows[-1]["crontab"] == "*/5 * * * *"

    def test_crontab_never_below_platform_floor(self, tmp_probe):
        """反向对照：cron 间隔不低于平台下限 5 分钟（低于下限根本提交不了）。"""
        plan = tmp_probe.delay_plan()
        for row in plan:
            if not row["crontab"]:
                continue
            minutes = int(row["crontab"].split("*/")[1].split(" ")[0])
            assert minutes * 60 >= CRONTAB_MIN_INTERVAL_SEC, row

    def test_top_rc6_is_ten_minutes(self, tmp_probe):
        """第 6 次（顶）= 10 分钟；平台定时任务仍按**固定 */5** 一档扫。"""
        plan = tmp_probe.delay_plan()
        row6 = [r for r in plan if r["check"] == 6][0]
        assert row6["delay_sec"] == 600.0 and row6["at_top"] is True
        assert row6["crontab"] == "*/5 * * * *"

    def test_no_local_loop_driver_exists(self, tmp_probe):
        """**永不**本地空转：全部落点里没有"本地 sleep/轮询"这一种。"""
        assert "local_loop" not in ALL_DRIVERS
        assert "local_sleep" not in ALL_DRIVERS
        plan = tmp_probe.delay_plan()
        assert all(r["local_cpu"].startswith("0") for r in plan)

    def test_waiting_probe_reports_platform_instruction(self, tmp_probe):
        """等待中的探测 → 给出**平台指令**（await/crontab），不是"再睡一会"。"""
        tmp_probe.create("c2", kind=CHECK_EVENT)
        out = tmp_probe.probe("c2", event_fired=False)     # 第 1 次 → 10s
        dis = tmp_probe.dispatch(out.event)
        assert dis["action"] == "platform_await"
        assert dis["trigger"]["type"] == "cnb:await"
        assert "不 sleep" in dis["trigger"]["note"]
        # 走到长等待档（第 5 次 = 6 分钟）→ 交平台定时任务（反向：不是本地循环）
        for _ in range(4):
            out = tmp_probe.probe("c2", event_fired=False)
        dis = tmp_probe.dispatch(out.event)
        assert dis["action"] == "platform_crontab"
        assert dis["trigger"]["type"] == "crontab"
        assert dis["trigger"]["event"] == "api_trigger_probe_check"

    def test_platform_wait_registration_is_idempotent_and_recorded(self, tmp_probe):
        """登记"已挂平台等待"：留痕 + 标记 awaiting（这才是"非轮询"的落点）。"""
        tmp_probe.create("c3", kind=CHECK_LOCK)
        cp = tmp_probe.register_platform_wait("c3", await_key="lock-released",
                                              sn="cnb-abc")
        assert cp.resolve_status == "awaiting"
        assert cp.await_key == "lock-released" and cp.sn == "cnb-abc"
        assert any(h["action"] == "platform_wait" for h in cp.history)

    def test_rejects_local_handoff_as_platform_wait(self, tmp_probe):
        """反例：把"交回人工"当平台等待挂上去 → 入口就拒绝。"""
        tmp_probe.create("c4")
        with pytest.raises(ValueError):
            tmp_probe.register_platform_wait("c4", driver="local_loop")


# ------------------------------------------------------------------
# ③ 等他的锁 + 判断检查点（只看可判定事实）
# ------------------------------------------------------------------

class TestJudge:
    def test_lock_held_waits_then_release_proceeds(self, env):
        """「等他的锁」：锁在→wait；锁释放→proceed（一轮一判，不猜）。"""
        _, _, sec = env
        sec.lock_registry.register("card:baseline", kind="card_write", holder="开发")
        sec.create_checkpoint("cp-lock", kind=CHECK_LOCK,
                              scope="card:baseline", holder="开发")
        out = sec.probe_checkpoint("cp-lock")
        assert out.verdict == WAIT
        assert "开发" in out.event.reason
        sec.lock_registry.release("card:baseline", by="开发", reason="编辑完成")
        out = sec.probe_checkpoint("cp-lock")
        assert out.verdict == PROCEED
        assert out.event.wake_allowed is True

    def test_lock_view_not_wired_judges_unknown(self, tmp_probe):
        """反向对照：锁表不可见 → `unknown`（**不得**误判成"锁已释放"）。"""
        tmp_probe.create("cp-blind", kind=CHECK_LOCK, scope="card:x")
        out = tmp_probe.probe("cp-blind")
        assert out.verdict == UNKNOWN
        assert out.event.wake_allowed is False
        assert "不误判" in out.event.reason

    def test_our_need_satisfied_releases_lock(self, env):
        """「我方已不需要」→ release（放锁、不再等）：**反队头阻塞**，不傻等对方。"""
        _, _, sec = env
        sec.lock_registry.register("card:baseline", kind="card_write", holder="开发")
        sec.create_checkpoint("cp-ours", kind=CHECK_LOCK, scope="card:baseline")
        out = sec.probe_checkpoint("cp-ours", our_need_satisfied=False)
        assert out.verdict == RELEASE
        assert tmp_probe_dispatch(sec, out)["action"] == "release_lock"
        assert tmp_probe_dispatch(sec, out)["wake_allowed"] is False

    def test_confirm_check_takes_human_word_only(self, tmp_probe):
        """人工确认型：`confirmed=True` 才 proceed；缺事实 → unknown（宽进）。"""
        tmp_probe.create("cp-confirm", kind=CHECK_CONFIRM)
        assert tmp_probe.probe("cp-confirm", confirmed=False).verdict == WAIT
        assert tmp_probe.probe("cp-confirm", confirmed=True).verdict == PROCEED

    def test_confirm_missing_fact_is_unknown(self, tmp_probe):
        """反向对照：没给 `confirmed` → unknown，**不得**默认"已确认"或"未确认"。"""
        tmp_probe.create("cp-confirm2", kind=CHECK_CONFIRM)
        out = tmp_probe.probe("cp-confirm2")
        assert out.verdict == UNKNOWN
        assert out.verdict not in (PROCEED, WAIT)

    def test_version_check_detects_change(self, tmp_probe):
        """版本判别式：版本号变化 → proceed；未变 → wait（同样输入给反向对照）。"""
        tmp_probe.create("cp-ver", kind=CHECK_VERSION)
        assert tmp_probe.probe("cp-ver", version=7).verdict == UNKNOWN  # 首次无基线
        assert tmp_probe.probe("cp-ver", version=7).verdict == WAIT, "版本未变不得判完成"
        assert tmp_probe.probe("cp-ver", version=7).verdict == WAIT, "仍未变仍不得判完成"
        assert tmp_probe.probe("cp-ver", version=8).verdict == PROCEED

    def test_version_reference_is_pre_probe_snapshot(self, tmp_probe):
        """**防退化护栏**：版本判别必须拿"探测前"快照做参照。

        若实现先写入本次版本再比较，prev == cur 恒成立 → "版本变了"永远判不出
        （恒为"未变"）。本用例把"变了/没变"两种同样输入放一起做反向对照。
        """
        tmp_probe.create("cp-a", kind=CHECK_VERSION)
        tmp_probe.probe("cp-a", version=1)              # 立基线
        changed = tmp_probe.probe("cp-a", version=2)    # 变了 → 必须 proceed
        tmp_probe.create("cp-b", kind=CHECK_VERSION)
        tmp_probe.probe("cp-b", version=1)
        same = tmp_probe.probe("cp-b", version=1)       # 没变 → 必须 wait
        assert changed.verdict == PROCEED, "版本变化必须能判出来（不得恒为未变）"
        assert same.verdict == WAIT
        assert changed.verdict != same.verdict, "同样形状的输入必须能被区分开"

    def test_event_check_only_proceeds_on_signal(self, tmp_probe):
        """事件判别式：只有明确事件到达才 proceed（反向：无信号 → wait/unknown）。"""
        tmp_probe.create("cp-ev", kind=CHECK_EVENT)
        assert tmp_probe.probe("cp-ev", event_fired=False).verdict == WAIT
        assert tmp_probe.probe("cp-ev", event_fired=True).verdict == PROCEED

    def test_platform_resolve_counts_as_event(self, tmp_probe):
        """平台侧 `cnb:resolve` 到达 = 状态变化已确认 → proceed（事件驱动闭环）。"""
        tmp_probe.create("cp-res", kind=CHECK_EVENT)
        tmp_probe.register_platform_wait("cp-res", await_key="k1")
        tmp_probe.resolve("cp-res", status="resolved")
        out = tmp_probe.probe("cp-res")
        assert out.verdict == PROCEED

    def test_judgement_ignores_agent_self_report(self, tmp_probe):
        """**不看 agent 自述**：自报"已完成"但不给可判定事实 → 仍 unknown/wait。"""
        tmp_probe.create("cp-say", kind=CHECK_LOCK, scope="card:x")
        out = tmp_probe.probe("cp-say", **{"agent_says_done": True})
        assert out.verdict == UNKNOWN, "自述不得被采信"
        assert "agent_says_done" not in out.event.facts

    def test_next_wait_uses_next_ladder_step(self, tmp_probe):
        """**防退化护栏**：第 N 次探测后的"下一次等待"取阶梯下一档（不得回落档位）。

        反例：用 ladder[N-1] 会让下一次等待与刚等过的档重复（甚至看起来"变短"）。
        """
        tmp_probe.create("cp-step", kind=CHECK_EVENT)
        seen = []
        for _ in range(6):
            out = tmp_probe.probe("cp-step", event_fired=False)
            seen.append(out.event.next_delay_sec)
        assert seen[:5] == [30.0, 60.0, 180.0, 360.0, 600.0], seen
        seq = [x for x in seen if x > 0]
        assert seq == sorted(seq), f"下一次等待只增不减: {seq}"

    def test_unknown_keeps_waiting_not_proceed(self, tmp_probe):
        """unknown 的处置是"继续等"（挂检查点），**不是**放行。"""
        tmp_probe.create("cp-u", kind=CHECK_LOCK, scope="card:y")
        out = tmp_probe.probe("cp-u")
        assert out.verdict == UNKNOWN
        assert out.event.next_delay_sec == 30.0, "已等过 10s 档，下一次取 30s 档"
        assert tmp_probe.dispatch(out.event)["action"] == "platform_await"


def tmp_probe_dispatch(sec, out):
    """小工具：把探测产出交给门面分发（保持用例可读）。"""
    return sec.dispatch_probe_event(out.event)


# ------------------------------------------------------------------
# ④ 平台最佳实践 + 计费口径
# ------------------------------------------------------------------

class TestPlatformPractice:
    def test_best_practice_drivers_are_platform_native(self, tmp_probe):
        """平台最佳实践落点：await / crontab / 外部 webhook / 委托事件 / 人工。"""
        rules = load_rules(RULES)
        assert rules.probe_drivers == [
            "platform_await", "platform_crontab", "external_webhook",
            "delegate_event", "handoff_resume"]
        assert tmp_probe.max_checks == rules.probe_max_checks == 6

    def test_crontab_floor_comes_from_platform_docs(self):
        """平台定时任务最小间隔 5 分钟（官方文档口径），规则包不得低于它。"""
        rules = load_rules(RULES)
        assert rules.crontab_min_interval_sec >= CRONTAB_MIN_INTERVAL_SEC
        assert rules.probe_await_max_sec == 300.0

    def test_probe_plan_is_one_liner_for_docs(self):
        """一行人类可读探测计划（文档/角色提示词共用同一句话）。"""
        plan = load_rules(RULES).probe_plan()
        assert "最多 6 次" in plan and "本地绝不 sleep" in plan

    def test_proceeding_verdict_triggers_real_work_not_another_wait(self, tmp_probe):
        """条件满足 → 触发**真正干活**的流水线（cnb:apply api_trigger），不是又一轮等待。"""
        tmp_probe.create("cp-work", kind=CHECK_EVENT)
        out = tmp_probe.probe("cp-work", event_fired=True)
        dis = tmp_probe.dispatch(out.event)
        assert dis["action"] == "resume"
        assert dis["trigger"]["type"] == "cnb:apply"
        assert dis["trigger"]["event"] == "api_trigger_probe_resume"
        assert dis["wake_allowed"] is True

    def test_exhausted_verdict_never_wakes(self, tmp_probe):
        """预算用尽 → **不得**据此唤醒（否则探测自己变成新循环）。"""
        tmp_probe.create("cp-ex", kind=CHECK_EVENT, max_checks=1)
        out = tmp_probe.probe("cp-ex", event_fired=False)
        dis = tmp_probe.dispatch(out.event)
        assert dis["action"] == "human_resume"
        assert dis["wake_allowed"] is False

    def test_waiting_costs_zero_cpu_counterfactual(self, tmp_probe):
        """探测/等待零占用（反向对照：本地空转 10 分钟要烧 ~1.33 核时）。"""
        plan = tmp_probe.delay_plan()
        assert all(r["local_cpu"].startswith("0") for r in plan)
        cost = cpu_cost_of_sleep(600, cores=8)
        assert cost["core_hours"] > 1.3, "本地空转确实在烧核时——所以才必须挂平台"

    def test_probe_status_exposes_contract(self, env):
        """门面给出探测口径快照（角色/文档照它执行，不各写一套）。"""
        _, _, sec = env
        st = sec.probe_status()
        assert st["max_checks"] == 6
        assert st["drivers"][0] == "platform_await"
        assert st["crontab_min_interval_sec"] >= 300
        assert "不 sleep" in st["local_cpu_cost"]


# ------------------------------------------------------------------
# ⑤ 观测与生命周期
# ------------------------------------------------------------------

class TestOverviewAndLifecycle:
    def test_overview_shows_remaining_checks_and_next_wait(self, env):
        """「触发了一眼就能看」：还剩几次、下一次等多久、挂在哪。"""
        _, _, sec = env
        sec.lock_registry.register("card:a", kind="card_write", holder="开发")
        sec.create_checkpoint("cp-1", kind=CHECK_LOCK, scope="card:a", holder="开发")
        sec.probe_checkpoint("cp-1")
        view = sec.checkpoints_overview()
        assert view["count"] == 1
        row = view["checkpoints"][0]
        assert row["checks_used"] == 1 and row["checks_left"] == 5
        assert row["next_delay_sec"] == 30.0
        assert any("card:a" in s for s in view["where_to_avoid"])

    def test_closed_checkpoints_leave_the_open_list(self, env):
        """已放行/已放弃/已交回人工的检查点不在"未决"清单里（不占位）。"""
        _, _, sec = env
        sec.create_checkpoint("cp-done", kind=CHECK_EVENT)
        sec.probe_checkpoint("cp-done", event_fired=True)
        assert sec.checkpoints_overview()["count"] == 0

    def test_delete_session_clears_checkpoints(self, env):
        """会话回收时检查点一并清理（同 id 复建不带陈旧预算）。"""
        d, _, _ = env
        sec = SecretarySession(SESSION, data_dir=d, rules_path=RULES)
        sec.create_checkpoint("cp-x", kind=CHECK_EVENT)
        assert sec.checkpoints_overview()["count"] == 1
        sec.delete_session()
        sec2 = SecretarySession(SESSION, data_dir=d, rules_path=RULES)
        assert sec2.checkpoints_overview()["count"] == 0

    def test_illegal_session_id_rejected(self):
        """非法会话标识拒绝构造（**只挡危险形态**，不挡"仓/号"这类正常标识）。

        `bad/id` 这类"仓/号"写法是**跨仓协作正常标识**，不得误伤（见
        `test_collab_confirm_issue14.py`）；真正该挡的是路径穿越与控制字符。
        """
        for bad in ("", "a/../b", "../evil", "/abs", "a\\b", "has space",
                    "line\nbreak", "x" * 200):
            with pytest.raises(ValueError):
                CheckpointProbe(bad)
        assert CheckpointProbe("repo-b/issue-7").session_id == "repo-b/issue-7"

    def test_corrupt_checkpoint_file_is_tolerated(self, tmp_probe):
        """损坏的检查点文件 → 视为不存在（宽进不炸），不阻塞探测。"""
        with open(os.path.join(tmp_probe.data_dir, "broken.json"), "w") as f:
            f.write("{not json")
        assert tmp_probe.list_open() == []
        cp = tmp_probe.create("broken")     # 可正常重建
        assert cp.key == "broken"

    def test_backoff_and_probe_share_the_same_budget_source(self, tmp_probe):
        """唯一语义源：探测次数上界与等待阶梯档位数同源（改阶梯即改预算口径）。"""
        bo = StepBackoff()
        assert tmp_probe.max_checks == len(bo.ladder) == 6
        assert [r["delay_sec"] for r in tmp_probe.delay_plan()[:6]] == \
            [bo.delay_for(i) for i in range(6)]
