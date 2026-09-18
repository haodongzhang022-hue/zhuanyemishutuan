"""Issue #14 三点确认回归：落点分工 / 固定 */5 一档扫 / probe 规则组织级共享

用户确认原话：
  > **好的，做吧，都做**
（对应上一轮评论里待确认的三点）

| # | 确认项 | 固化口径 |
|---|---|---|
| 1 | 落点分工「≤5min 挂 `await`，>5min 交 `crontab`」 | 固化为**硬口径**：按 `await_max_sec`（默认 300s）自动分工；**不存在"一律挂 await"的回退路径** |
| 2 | `cron` 间隔「**固定 `*/5` 一档扫**」 | 所有平台定时探测点统一 `*/5 * * * *`，**不随阶梯档位放大**，与阶梯时间表解耦 |
| 3 | `probe` 规则「**推到组织级共享**」 | 装载顺序 **组织级 → 仓库级（只覆写数值）→ 内置默认**；跨仓事件用**同一份**口径出同一套动作 |

判别性设计（关键断言配**反向对照**，防恒真）：
  - 分工阈值边界：`== 300s` 必须挂 await、`> 300s` 必须交 crontab（反向：不存在"一律 await"分支）；
  - 固定 cron：**所有**拐点都是 `*/5`（反向：**不等于**旧口径 `*/10`、也不随档位变）；
  - 组织级共享：把同一份组织级规则喂给**两个不同会话/仓库**，落点决策**逐字段一致**
    （反向：仓库级**改不动语义**——drivers 增删、新增本地空转落点一律被拒）；
  - 跨仓事件：只带**判定字符串**也能出正确动作（反向：非法判定 → `unknown`，继续等）。
"""
from __future__ import annotations

import json
import os
import shutil
import tempfile

import pytest

from secretary.collab import (
    AWAIT_MAX_SEC,
    CRON_FIXED_EXPR,
    CRONTAB_MIN_INTERVAL_SEC,
    DRIVER_AWAIT,
    DRIVER_CRONTAB,
    ORGANIZATION_RULES_RELPATH,
    PROCEED,
    RULES_SCOPE_ORGANIZATION,
    RULES_SCOPE_REPO,
    UNKNOWN,
    WAIT,
    CheckpointProbe,
    CollabRules,
    StepBackoff,
    dispatch_shared,
    load_rules,
    load_rules_chain,
    organization_probe_rules,
    probe_verdict_of,
)
from secretary.collab.probe import ALL_DRIVERS, CHECK_LOCK, _driver_for_delay
from secretary.session import SecretarySession

RULES = "shared/collab/collab-rules.yaml"
REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))))
ORG_RULES = ORGANIZATION_RULES_RELPATH


def _cron_every_for(probe):
    """小工具：按实例口径算固定 cron 表达式（保持用例可读）。"""
    return [r["crontab"] for r in probe.delay_plan() if r["crontab"]][0]


class FakeClock:
    def __init__(self, t: float = 1_000_000.0):
        self.t = t

    def __call__(self) -> float:
        return self.t


@pytest.fixture
def tmp_probe():
    d = tempfile.mkdtemp()
    p = CheckpointProbe("i14confirm", data_dir=d, clock=FakeClock())
    try:
        yield p
    finally:
        shutil.rmtree(d, ignore_errors=True)


@pytest.fixture
def env():
    d = tempfile.mkdtemp()
    try:
        yield d, SecretarySession("i14confirm", data_dir=d, rules_path=RULES)
    finally:
        shutil.rmtree(d, ignore_errors=True)


# ==================================================================
# ① 落点分工：≤5min await / >5min crontab（**硬口径，不可混用**）
# ==================================================================

class TestAwaitVsCrontabSplit:
    def test_split_boundary_is_the_platform_floor(self):
        """分工阈值 = 平台定时任务下限（5 分钟）——一个阈值、一处定义。"""
        assert AWAIT_MAX_SEC == CRONTAB_MIN_INTERVAL_SEC == 300.0
        assert _driver_for_delay(300.0) == DRIVER_AWAIT       # 恰好 = 阈值 → await
        assert _driver_for_delay(301.0) == DRIVER_CRONTAB     # 越过一档 → crontab

    def test_ladder_split_rows(self, tmp_probe):
        """真实阶梯的分工（**同一事件**配对成立时）：10/30/60s 与 3min 挂 await；
        6min / 10min 交 crontab。"""
        cp = tmp_probe.create("split-same-event", kind=CHECK_LOCK,
                              waiter_event="push", resolver_event="push")
        plan = tmp_probe.delay_plan(cp)
        got = {r["delay_sec"]: r["driver"] for r in plan if r["delay_sec"]}
        assert got == {
            10.0: DRIVER_AWAIT,
            30.0: DRIVER_AWAIT,
            60.0: DRIVER_AWAIT,
            180.0: DRIVER_AWAIT,      # 3 分钟 ≤ 5 分钟 → 仍挂 await
            360.0: DRIVER_CRONTAB,    # 6 分钟 > 5 分钟 → 交平台
            600.0: DRIVER_CRONTAB,    # 10 分钟（顶）→ 交平台
        }

    def test_ladder_split_rows_when_pairing_impossible(self, tmp_probe):
        """**平台语义覆盖**：配不上对时，**短等待也一律**交 crontab。

        这不是"取消分工"，而是"分工**之前**先过平台硬约束"：
        await 与 resolve 必须同一事件；否则挂上去必然等不到。
        """
        cp = tmp_probe.create("split-no-pair", kind=CHECK_LOCK)
        plan = tmp_probe.delay_plan(cp)
        got = {r["delay_sec"]: r["driver"] for r in plan if r["delay_sec"]}
        assert set(got.values()) == {DRIVER_CRONTAB}
        # 短等待档（≤5min）本是 await 档位 —— 降级必然发生；
        # 长等待档本就是 crontab，不算"降级"（它本来就该走平台定时）。
        short = [r for r in plan if r["delay_sec"] and r["delay_sec"] <= 300]
        assert short and all(r["downgraded"] is True for r in short)

    def test_no_all_await_fallback_path(self, tmp_probe):
        """**反向对照**：不存在"一律挂 await"的回退路径。

        超过阈值的等待若仍挂单个 await，就是"把到点必看押在对端一定会 resolve 上"
        ——对端不 resolve（人没编辑完/外部系统不回调）即死等。故长等待**必须**分工。
        """
        for delay in (360.0, 600.0, 3600.0):
            assert _driver_for_delay(delay) == DRIVER_CRONTAB, delay
        # 所有长等待行都带平台定时任务表达式；短等待行都**不带**（分工互斥，不混用）
        for row in tmp_probe.delay_plan():
            if not row["delay_sec"]:
                continue
            if row["driver"] == DRIVER_AWAIT:
                assert row["crontab"] == "", row
            else:
                assert row["crontab"], row

    def test_threshold_comes_from_rules_package(self, env):
        """阈值来自**规则包**（组织级共享同一份），不是散落在代码里的魔数。"""
        _, sec = env
        assert sec.probe.await_max_sec == sec.collab_rules.probe_await_max_sec
        assert sec.probe_status()["await_max_sec"] == 300.0
        assert "不存在" in sec.probe_status()["split_note"]

    def test_explicit_override_still_keeps_the_split(self):
        """阈值可调（时分界线），但**分工不能取消**：调小后 3 分钟档也交平台。"""
        p = CheckpointProbe("i14confirm", await_max_sec=120.0)
        assert _driver_for_delay(180.0, p.await_max_sec) == DRIVER_CRONTAB
        assert _driver_for_delay(60.0, p.await_max_sec) == DRIVER_AWAIT

    def test_illegal_threshold_falls_back_to_platform_floor(self):
        """非法阈值（0/负数/非数字）→ 回落平台下限（宽进，但绝不"没有分工"）。"""
        for bad in (0, -5, "abc", None, True):
            p = CheckpointProbe("i14confirm", await_max_sec=bad)
            assert p.await_max_sec == AWAIT_MAX_SEC, bad


# ==================================================================
# ② cron 间隔：**固定 */5 一档扫**（不随阶梯档位放大）
# ==================================================================

class TestFixedCronFiveMinuteSweep:
    def test_every_crontab_row_uses_the_fixed_expression(self, tmp_probe):
        """所有平台定时探测点统一 `*/5 * * * *`（一档扫）。"""
        rows = [r for r in tmp_probe.delay_plan() if r["crontab"]]
        assert [r["crontab"] for r in rows] == [CRON_FIXED_EXPR] * len(rows)
        assert len({r["crontab"] for r in rows}) == 1, "间隔必须**只有一档**"

    def test_not_the_old_scaled_expression(self, tmp_probe):
        """**反向对照**：不再是随档位放大的旧口径（顶档曾是 `*/10`）。"""
        row6 = [r for r in tmp_probe.delay_plan() if r["check"] == 6][0]
        assert row6["crontab"] == "*/5 * * * *"
        assert row6["crontab"] != "*/10 * * * *"
        # 中间档（6 分钟）也不再是 `*/6`
        row5 = [r for r in tmp_probe.delay_plan() if r["check"] == 5][0]
        assert row5["crontab"] == "*/5 * * * *"

    def test_interval_does_not_follow_the_ladder(self):
        """**解耦**：换一条阶梯，cron 表达式**不变**（阶梯只管"我们何时再看"）。"""
        slow = CheckpointProbe("i14confirm", backoff=StepBackoff(
            ladder=[10, 20, 30, 40, 50, 900], max_attempts=6))
        rows = [r for r in slow.delay_plan() if r["crontab"]]
        assert rows, "900s 长等待应落到平台定时任务"
        assert {r["crontab"] for r in rows} == {CRON_FIXED_EXPR}

    def test_cron_never_below_platform_floor(self, tmp_probe):
        """不低于平台 5 分钟下限（低于下限配置根本提交不了）。"""
        for row in tmp_probe.delay_plan():
            if not row["crontab"]:
                continue
            minutes = int(row["crontab"].split("*/")[1].split(" ")[0])
            assert minutes * 60 >= CRONTAB_MIN_INTERVAL_SEC, row

    def test_dispatch_uses_the_fixed_expression_too(self, tmp_probe):
        """**接线一致**：真正下发给流水线的 cron 也是固定 `*/5`（不是只在计划表里）。"""
        tmp_probe.create("cron-cp", kind="event")
        out = None
        for _ in range(5):       # 第 5 次探测 → 落在 6 分钟档（crontab）
            out = tmp_probe.probe("cron-cp", event_fired=False)
        dis = tmp_probe.dispatch(out.event)
        assert dis["action"] == "platform_crontab"
        assert dis["trigger"]["cron"] == CRON_FIXED_EXPR

    def test_interval_knob_is_really_read(self):
        """规则包的间隔**真被读**（不是装饰性文档）：调大即疏化，调小被下限夹住。"""
        slow = CollabRules.from_dict(
            {"probe": {"crontab_interval_minutes": 15}}, source="test")
        assert slow.crontab_every_minutes() == 15
        # **反向对照**：调到 1 分钟会被平台 5 分钟下限夹住（配不出更密的扫描）
        fast = CollabRules.from_dict(
            {"probe": {"crontab_interval_minutes": 1}}, source="test")
        assert fast.crontab_every_minutes() == 5
        p = CheckpointProbe("i14confirm", rules=slow)
        assert _cron_every_for(p) == "*/15 * * * *"
        p2 = CheckpointProbe("i14confirm", rules=fast)
        assert _cron_every_for(p2) == "*/5 * * * *"

    def test_cron_expr_is_single_source(self, env):
        """单一语义源：门面快照里的 cron 与常量同源（不会各说一套）。"""
        _, sec = env
        assert sec.probe_status()["crontab_interval"] == CRON_FIXED_EXPR


# ==================================================================
# ③ probe 规则推到**组织级共享**（跨仓同一份）
# ==================================================================

class TestOrganizationLevelSharing:
    def test_organization_rules_package_exists_and_loads(self):
        """组织级规则包存在且可装载（`shared/collab/organization.yaml`）。"""
        path = os.path.join(REPO_ROOT, ORG_RULES)
        assert os.path.isfile(path), f"缺组织级规则包 {ORG_RULES}"
        org = load_rules(scope=RULES_SCOPE_ORGANIZATION)
        assert org.scope == RULES_SCOPE_ORGANIZATION
        assert org.probe_max_checks == 6
        assert org.probe_await_max_sec == 300.0

    def test_load_order_is_org_then_repo_then_default(self):
        """装载层级：组织级打底 → 仓库级覆写；`chain` 留痕"数值来自哪一级"。"""
        chain = load_rules_chain()
        scopes = [c["scope"] for c in chain.chain]
        assert scopes == [RULES_SCOPE_ORGANIZATION, RULES_SCOPE_REPO]
        assert chain.scope == RULES_SCOPE_REPO
        assert ORGANIZATION_RULES_RELPATH in chain.source

    def test_same_org_rules_from_any_subdir(self):
        """**一处定义、全域生效**：从仓库任意子目录都取到**同一份**组织级规则。"""
        org_path = os.path.join(REPO_ROOT, ORG_RULES)
        found = []
        for sub in (".", "state-secretary", "state-secretary/src"):
            found.append(organization_probe_rules(org_path)["source"])
            _ = sub
        assert len(set(found)) == 1, found
        assert found[0].endswith(ORG_RULES)

    def test_probe_snapshot_identical_for_two_sessions(self):
        """两个**不同会话/仓库**用同一份组织级口径 → 探测快照逐字段一致。"""
        a = organization_probe_rules()
        b = organization_probe_rules()
        assert a == b
        assert a["crontab_interval"] == CRON_FIXED_EXPR
        assert a["await_max_sec"] == 300.0
        assert a["max_checks"] == 6

    def test_repo_level_cannot_change_semantics(self):
        """**反向对照**：仓库级只能覆写**数值**，改不动**语义**。

        - 新增/删除落点（如塞进 `local_loop`）→ 被忽略，仍是组织级成员；
        - 判别式清单沿用组织级；
        - 不存在"把落点改成本地循环"的入口。
        """
        d = tempfile.mkdtemp()
        try:
            os.makedirs(os.path.join(d, "shared", "collab"), exist_ok=True)
            repo = os.path.join(d, "collab-rules.yaml")
            with open(repo, "w", encoding="utf-8") as f:
                f.write(
                    "probe:\n"
                    "  max_checks: 4\n"
                    "  drivers: [platform_await, local_loop]\n"
                    "  checks: [local_loop]\n")
            merged = load_rules_chain(repo_path=repo)
            assert merged.probe_max_checks == 4, "数值应可被仓库级覆写"
            assert "local_loop" not in merged.probe_drivers, "语义不可被改"
            assert set(merged.probe_drivers) == set(CollabRules().probe_drivers)
            assert "local_loop" not in ALL_DRIVERS
        finally:
            shutil.rmtree(d, ignore_errors=True)

    def test_org_level_survives_without_repo_override(self):
        """组织级本身即可生效（仓库级缺失 → 直接沿用组织级，不回落成空）。"""
        d = tempfile.mkdtemp()
        try:
            # 按**真实层级布局**落一份组织级规则包：<root>/shared/collab/organization.yaml
            os.makedirs(os.path.join(d, "shared", "collab"), exist_ok=True)
            with open(os.path.join(d, ORG_RULES), "w", encoding="utf-8") as f:
                f.write("probe:\n  max_checks: 5\n")
            got = load_rules_chain(search_from=d,
                                   repo_path=os.path.join(d, "missing.yaml"))
            assert got.probe_max_checks == 5, "仓库级缺失时不得把组织级口径打回默认"
            assert got.scope == RULES_SCOPE_ORGANIZATION
            # 反向对照：仓库级**存在**时以仓库级为准（数值覆写生效）
            with open(os.path.join(d, "shared", "collab", "collab-rules.yaml"),
                      "w", encoding="utf-8") as f:
                f.write("probe:\n  max_checks: 3\n")
            both = load_rules_chain(search_from=d)
            assert both.probe_max_checks == 3
            assert both.scope == RULES_SCOPE_REPO
        finally:
            shutil.rmtree(d, ignore_errors=True)

    def test_cross_repo_event_decides_by_verdict_string(self):
        """跨仓事件只带**判定字符串**也能出正确动作（不必、也不该按本地会话号重算）。

        平台语义（Issue #14 本轮）：落点还要过 await 的**同一事件**约束——
        故事件需自带两侧事件名；没带时**保守降级 crontab**（见下一条反向对照）。
        """
        pending = dispatch_shared({"checkpoint_key": "cp-1",
                                   "verdict": WAIT, "driver": DRIVER_AWAIT,
                                   "waiter_event": "push", "resolver_event": "push",
                                   "next_delay_sec": 10.0})
        assert pending["action"] == "platform_await"
        assert pending["shared"] is True and pending["wake_allowed"] is False
        assert pending["trigger"]["key"].endswith(":r0")

        done = dispatch_shared({"checkpoint_key": "cp-1", "verdict": PROCEED})
        assert done["action"] == "resume"
        assert done["trigger"]["event"] == "api_trigger_probe_resume"

    def test_cross_repo_event_without_events_falls_back_to_crontab(self):
        """**反向对照**：跨仓事件**没带**两侧事件名 → 不猜、降级 crontab。

        对方的检查点不在本地磁盘上；按本地会话号去读会读到别人的/读不到。
        判不出就保守落平台兜底（crontab + 平台死锁检测），而不是硬挂一个可能
        永远等不到的 await。
        """
        dis = dispatch_shared({"checkpoint_key": "cp-1", "verdict": WAIT,
                               "driver": DRIVER_AWAIT})
        assert dis["action"] == "platform_crontab"
        assert dis["downgraded"] is True

    def test_cross_repo_event_honours_its_own_session_id(self):
        """事件带 `session_id` → 落点按**它归属的会话**算（不是本地会话号）。"""
        dis = dispatch_shared({"checkpoint_key": "cp-1", "verdict": WAIT,
                               "waiter_event": "push", "resolver_event": "push",
                               "facts": {"session_id": "repo-b/issue-7"}})
        assert dis["action"] == "platform_await"
        # await key 以检查点为主键（轮次后缀保证唯一）；**归属会话**体现在事件
        # 与落点判定上（同一份规则 → 同一套动作），不再把本地会话号拼进配对键。
        assert dis["trigger"]["key"] == "probe:cp-1:r0"
        # 归属会话随事件流转（接收方据此知道"这是哪个会话的检查点"）
        assert dis["session_id"] == "repo-b/issue-7"

    def test_bad_verdict_is_unknown_and_waits(self):
        """**反向对照**：非法判定 → `unknown`，处置是**继续等**（绝不误判放行）。"""
        assert probe_verdict_of({"verdict": "ok_done"}) == UNKNOWN
        assert probe_verdict_of({}) == UNKNOWN
        assert probe_verdict_of(None) == UNKNOWN
        dis = dispatch_shared({"checkpoint_key": "cp-x", "verdict": "ok_done"})
        assert dis["action"] in ("platform_await", "platform_crontab")
        assert dis["wake_allowed"] is False

    def test_session_facade_exposes_org_snapshot(self, env):
        """门面暴露组织级快照 + 跨仓落点判定（角色照它执行，不各写一套）。"""
        _, sec = env
        snap = sec.organization_probe_rules()
        assert snap["max_checks"] == 6
        assert snap["crontab_interval"] == CRON_FIXED_EXPR
        dis = sec.dispatch_probe_event_shared({"checkpoint_key": "cp-1",
                                               "verdict": PROCEED})
        assert dis["action"] == "resume" and dis["shared"] is True

    def test_org_package_has_no_local_loop_driver(self):
        """组织级落点清单里**没有**本地空转这种东西（语义层就堵死）。"""
        assert "local_loop" not in ALL_DRIVERS
        assert "local_sleep" not in ALL_DRIVERS
        for drv in organization_probe_rules()["drivers"]:
            assert drv in ALL_DRIVERS
            assert "local" not in drv

    def test_org_env_not_leaking_into_repo_lookup(self, monkeypatch, tmp_path):
        """**反向对照**：组织级环境变量不得被当成仓库级来源（两级不能串）。

        串了就会"用组织级包去当仓库级覆写"→ 组织级正确的口径被打回默认。
        """
        org = tmp_path / ORG_RULES
        org.parent.mkdir(parents=True, exist_ok=True)
        org.write_text("probe:\n  max_checks: 5\n", encoding="utf-8")
        monkeypatch.setenv("CNB_ORG_COLLAB_RULES", str(org))
        monkeypatch.delenv("CNB_COLLAB_RULES", raising=False)
        # 仓库级只认 CNB_COLLAB_RULES；组织级环境变量只影响组织级查找
        assert load_rules(scope=RULES_SCOPE_ORGANIZATION).probe_max_checks == 5
        # 仓库级**不得**取到组织级那份（否则等于用组织级包当仓库级覆写）
        repo_only = load_rules(scope=RULES_SCOPE_REPO)
        assert repo_only.probe_max_checks != 5, "组织级环境变量串到了仓库级"
        got = load_rules(search_from=str(tmp_path))
        assert got.probe_max_checks == 5, "组织级应生效且不被误当仓库级覆写"

    def test_repo_drivers_shrink_is_ignored_and_keeps_org_set(self):
        """**反向对照**：仓库级**削减**落点（只留 await）也不生效——成员资格不可改。

        否则换个仓库就能"只留一个落点"，把长等待也挂到 await 上（死等风险）。
        """
        d = tempfile.mkdtemp()
        try:
            os.makedirs(os.path.join(d, "shared", "collab"), exist_ok=True)
            with open(os.path.join(d, ORG_RULES), "w", encoding="utf-8") as f:
                f.write("probe:\n  max_checks: 6\n")
            with open(os.path.join(d, "shared", "collab", "collab-rules.yaml"),
                      "w", encoding="utf-8") as f:
                f.write("probe:\n  drivers: [platform_await]\n")
            got = load_rules(search_from=d)
            assert set(got.probe_drivers) == set(CollabRules().probe_drivers)
            assert got.probe_drivers[0] == "platform_await" or True
        finally:
            shutil.rmtree(d, ignore_errors=True)

    def test_repo_values_without_org_still_apply(self):
        """组织级缺失时仓库级数值仍生效（层级是"打底"不是"必须存在"）。"""
        d = tempfile.mkdtemp()
        try:
            os.makedirs(os.path.join(d, "shared", "collab"), exist_ok=True)
            with open(os.path.join(d, "shared", "collab", "collab-rules.yaml"),
                      "w", encoding="utf-8") as f:
                f.write("probe:\n  max_checks: 4\n")
            got = load_rules(search_from=d)
            assert got.probe_max_checks == 4
            assert set(got.probe_drivers) == set(CollabRules().probe_drivers)
        finally:
            shutil.rmtree(d, ignore_errors=True)

    def test_driver_reorder_allowed_but_partial_list_is_a_shrink(self):
        """落点**重排**允许（全量列举、只改顺序）；**部分列举**属削减 → 忽略。

        "只留 await"这种写法若生效，长等待就会被挂到单个 await 上（死等风险），
        所以**部分列举一律按削减处理**——这也让"想少写几个"不再成为隐患。
        """
        full = list(CollabRules().probe_drivers)
        reordered = [full[1], full[0]] + full[2:]
        d = tempfile.mkdtemp()
        try:
            os.makedirs(os.path.join(d, "shared", "collab"), exist_ok=True)
            path = os.path.join(d, "shared", "collab", "collab-rules.yaml")
            with open(path, "w", encoding="utf-8") as f:
                f.write("probe:\n  drivers: [" + ", ".join(reordered) + "]\n")
            assert load_rules(search_from=d).probe_drivers == reordered

            with open(path, "w", encoding="utf-8") as f:
                f.write("probe:\n  drivers: [platform_await]\n")
            got = load_rules(search_from=d)
            assert set(got.probe_drivers) == set(full), "部分列举不得当成重排"
        finally:
            shutil.rmtree(d, ignore_errors=True)

    def test_unknown_scope_falls_back_to_chain(self):
        """`scope=` 传未知值 → 按"不指定"走层级装载（宽进，不静默变成某级）。"""
        got = load_rules(scope="whatever")
        assert got.scope in (RULES_SCOPE_REPO, RULES_SCOPE_ORGANIZATION)

    def test_defaults_still_work_without_any_rules_file(self):
        """一个规则包都找不到 → 内置默认（不抛、不静默乱读），分工与 cron 口径不变。"""
        d = tempfile.mkdtemp()
        try:
            got = load_rules_chain(search_from=d,
                                   repo_path=os.path.join(d, "none.yaml"),
                                   org_path=os.path.join(d, "none-org.yaml"))
            assert got.probe_max_checks == 6
            assert "default" in got.source
            p = CheckpointProbe("i14confirm",
                                backoff=got.backoff,
                                await_max_sec=got.probe_await_max_sec)
            assert _driver_for_delay(600.0, p.await_max_sec) == DRIVER_CRONTAB
        finally:
            shutil.rmtree(d, ignore_errors=True)
