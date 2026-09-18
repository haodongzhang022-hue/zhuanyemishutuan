"""Issue #14 PR #15 复评第 4 轮定点修复回归（🔴1–🔴4 + 🟡1–🟡5）

触发：架构·评审 复评第 3 轮（基线 `ed62088`）——**🔴 未清零**（`fix_bug`）。
本文件只针对该轮 4 项 🔴 与 5 项 🟡 补**判别性回归**；每组关键断言配**反向对照**。

| 项 | 缺陷（评审实测） | 本文件判据 |
|---|---|---|
| 🔴1 | 预算通道只取**第一条**留痕 → 评论正文多留痕叠加时轮次**回退**，「最多 3 轮 / 6 次」永不触发 | 评论正文累积多条留痕（每棒写回）→ 序列**必须出现 fuse**（第 4 次即拦）；探测 `checks_used` 必须递增到 6；**两种顺序**（留痕在前/在后）结果一致；反向：不传载荷仍从 0 开始 |
| 🔴2 | `extract_json_object` 只取**第一个**对象 → 留痕在前时静默交接 `wake=False` 读不到 → `should_wake_next()=True`（熔断语义漏出，方向是**放行**） | 正文「留痕 + 交接卡」两种顺序都必须继承熔断；门面 `inherit_silent_handoff` 同结论；反向：`wake=True` 载荷仍不熔断 |
| 🔴3 | 新 `_mini_yaml` 值行含引号即跳过注释剥离 → 带引号注释的**覆写**被读成字符串 → 静默落回默认（生产无 PyYAML 路径） | 带引号注释的覆写文件在 `locks/loop/probe/scan` **四域**都读到生效值；真实规则包的两个 bool 字段读成 **bool**；反向：注释不带引号仍正确 |
| 🔴4 | `LoopState.from_dict` 的 `fused` 改严格 bool → `fused=1` 的坏卡把熔断**清掉**（安全旗标 fail-open） | `fused` 非严格 `False`（`1`/`"yes"`/`0`/`None`…）一律按**已熔断**；反向：严格 `False` 仍未熔断 |

🟡：递归上界（深嵌套不逃逸）、`api_trigger_probe_wait` 真被产出、坏 `key` → `None`、
`wake_allowed` 保守默认、`checkpoint_plan(checks>6)` 不产重复行。
"""
from __future__ import annotations

import json
import os
import tempfile

import pytest

from secretary.collab import (
    CRON_FIXED_EXPR,
    WAIT,
    CheckpointProbe,
    dispatch_shared,
)
from secretary.collab.probe import Checkpoint, ProbeEvent
from secretary.collab.rules import CollabRules, _mini_yaml
from secretary.loop import (
    LoopAction,
    LoopGuard,
    MAX_COERCE_DEPTH,
    extract_json_objects,
    pick_wake_object,
)
from secretary.session import SecretarySession

SESSION = "i14r4"
REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))))
RULES = "shared/collab/collab-rules.yaml"


class FakeClock:
    def __init__(self, t: float = 1_000_000.0):
        self.t = t

    def __call__(self) -> float:
        return self.t


@pytest.fixture
def tmp_dir():
    d = tempfile.mkdtemp()
    yield d


# ==================================================================
# 🔴1 预算通道：取**全部**留痕（`max()` 合并），不是第一条
# ==================================================================

class TestBudgetTrailTakesAll:
    def test_comment_body_with_many_trails_still_fuses(self):
        """**评论正文累积多条留痕**（每棒把留痕写回评论 = 必做项）→ 必须熔断。

        旧实现 `BUDGET_TRAIL_RE.search()` 只取**第一条**（= 最旧那次轮次）
        → 轮次回退 → 8 棒序列恒为 `fix×8`，**从不 fuse**。
        """
        seq, trails = [], []
        for _ in range(8):
            g = LoopGuard(SESSION, data_dir=tempfile.mkdtemp())
            body = "\n".join(trails)          # 正文 = 历史留痕累积
            if body:
                g.adopt_budget(body, source="comment", save=False)
            dec = g.register_round(blocking=1)
            seq.append(dec.action)
            trails.append(g.budget_trail())
        assert LoopAction.FUSE.value in seq, seq
        assert seq[:3] == [LoopAction.FIX.value] * 3, seq
        assert seq[3] == LoopAction.FUSE.value, seq

    def test_last_only_payload_agrees(self):
        """**同口径**：只喂**最新一条**留痕时序列一致（说明合并没改语义）。"""
        seq, last = [], None
        for _ in range(8):
            g = LoopGuard(SESSION, data_dir=tempfile.mkdtemp())
            dec = g.register_round(blocking=1, external_rounds=last)
            seq.append(dec.action)
            last = dec.budget_trail
        assert seq[:4] == [LoopAction.FIX.value] * 3 \
            + [LoopAction.FUSE.value], seq

    def test_order_of_trails_does_not_matter(self):
        """**两种顺序**（留痕在前 / 在后）判出的轮次一致（判别性核心）。"""
        g = LoopGuard(SESSION, data_dir=tempfile.mkdtemp())
        trails = []
        for _ in range(3):
            trails.append(g.budget_trail())
            g.register_round(blocking=1)
        got = []
        for body in ("\n".join(trails), "\n".join(reversed(trails)),
                     trails[-1]):
            other = LoopGuard(SESSION, data_dir=tempfile.mkdtemp())
            other.adopt_budget(body, save=False)
            got.append(other.status()["round_no"])
        assert got == [2, 2, 2], got

    def test_probe_budget_from_comment_body_with_many_trails(self):
        """探测预算同族：正文累积多条 `probe-budget` 留痕 → `checks_used` 递增到 6。"""
        trails, seq = [], []
        for _ in range(8):
            p = CheckpointProbe(SESSION, data_dir=tempfile.mkdtemp(),
                                clock=FakeClock())
            body = "\n".join(trails)
            out = p.probe("cp1", confirmed=False,
                          external_checks=(body or None))
            seq.append(out.event.checks_used)
            trails.append(p.budget_trail_of(p.load("cp1")))
        assert seq[:6] == [1, 2, 3, 4, 5, 6], seq
        assert seq[-1] == 6, seq

    def test_probe_without_payload_stays_at_one(self):
        """**反向对照**：不传载荷 → 每棒从 0 开始（证明上一条有判别力）。"""
        seq = []
        for _ in range(8):
            p = CheckpointProbe(SESSION, data_dir=tempfile.mkdtemp(),
                                clock=FakeClock())
            seq.append(p.probe("cp1", confirmed=False).event.checks_used)
        assert set(seq) == {1}, seq

    def test_trail_segment_plus_plain_card_still_merges(self):
        """「留痕段 + **不带内嵌留痕**的交接卡」两种顺序都恢复轮次。

        预算来源取**整段正文**（不是只取 wake 对象那一个）——否则这种拼接形态
        会把留痕段整个丢掉（轮次归零）。
        """
        g = LoopGuard(SESSION, data_dir=tempfile.mkdtemp(), max_rounds=3)
        for _ in range(3):
            g.register_round(blocking=1)
        trail = g.budget_trail()
        card = json.dumps({"wake": True, "rounds": {"total": 0}})
        for body in (f"{trail}\n{card}", f"{card}\n{trail}"):
            other = LoopGuard.from_handoff(SESSION, data_dir=tempfile.mkdtemp(),
                                           handoff_context=body)
            assert other.status()["round_no"] == 3, body
            assert other.is_fused() is False

    def test_handoff_context_embedded_trail_merges(self):
        """交接卡 `context` 内嵌 `budget_trail` 字段 → 也按 `max()` 合并（不替换）。"""
        sec = SecretarySession(SESSION, data_dir=tempfile.mkdtemp(), max_rounds=3)
        for _ in range(3):
            sec.review_round(blocking=1)
        payload = json.dumps({
            "wake": True,
            "rounds": {"total": 1},              # 载荷自身偏小
            "budget_trail": sec.loop_budget_trail(),   # 内嵌留痕偏大
        })
        nxt = SecretarySession(SESSION, data_dir=tempfile.mkdtemp())
        nxt.inherit_budget(payload)
        assert nxt.loop_status()["round_no"] == 3, "内嵌留痕不得被小值载荷盖掉"


# ==================================================================
# 🔴2 静默交接：正文拼接顺序无关（优先取含 `wake` 的对象）
# ==================================================================

class TestSilentHandoffOrderInsensitive:
    @staticmethod
    def _fused_payload(tmp: str) -> str:
        sec = SecretarySession(SESSION, data_dir=tmp, max_rounds=3)
        for _ in range(4):
            sec.review_round(blocking=1)
        card = sec.silent_handoff("a-unit", "b-unit", "熔断交接")
        return sec.loop_budget_trail(), card.context

    @pytest.mark.parametrize("trail_first", [True, False])
    def test_both_orders_inherit_silence(self, trail_first):
        """**两种拼接顺序**都必须继承熔断（旧实现只取第一个对象 → 留痕在前时漏掉）。"""
        tmp = tempfile.mkdtemp()
        trail, card = self._fused_payload(tmp)
        body = f"{trail}\n{card}" if trail_first else f"{card}\n{trail}"
        g = LoopGuard.from_handoff(SESSION, data_dir=tempfile.mkdtemp(),
                                   handoff_context=body)
        assert g.is_fused() is True, body[:200]
        assert g.should_wake_next() is False
        assert g.status()["round_no"] == 4, "轮次一并继承"

    @pytest.mark.parametrize("trail_first", [True, False])
    def test_facade_inherit_silent_handoff_both_orders(self, trail_first):
        """门面级同结论（`inherit_silent_handoff` 不得因顺序漏掉熔断）。"""
        tmp = tempfile.mkdtemp()
        trail, card = self._fused_payload(tmp)
        body = f"{trail}\n{card}" if trail_first else f"{card}\n{trail}"
        nxt = SecretarySession(SESSION, data_dir=tempfile.mkdtemp())
        assert nxt.inherit_silent_handoff(body) is True
        assert nxt.should_wake_next() is False

    def test_probe_trail_first_reads_event(self):
        """探测留痕在前：`external_checks` 仍要读到载荷里的轮次。"""
        p = CheckpointProbe(SESSION, data_dir=tempfile.mkdtemp(), clock=FakeClock())
        cp = p.create("ck")
        cp.checks_used = 5
        trail = p.budget_trail_of(cp)
        ev = json.dumps(ProbeEvent(checkpoint_key="ck", verdict=WAIT,
                                   next_delay_sec=600.0).to_dict())
        assert p._coerce_checks_used(f"{trail}\n{ev}") == 5
        assert p._coerce_checks_used(f"{ev}\n{trail}") == 5

    def test_wake_true_payload_is_not_silent(self):
        """**反向对照**：`wake=True` 的常规接力（留痕在前）不得被误熔断。"""
        g = LoopGuard(SESSION, data_dir=tempfile.mkdtemp())
        g.register_round(blocking=1)
        body = f"{g.budget_trail()}\n" + json.dumps({"wake": True, "rounds": {"total": 1}})
        other = LoopGuard.from_handoff(SESSION, data_dir=tempfile.mkdtemp(),
                                       handoff_context=body)
        assert other.is_fused() is False
        assert other.should_wake_next() is True

    def test_pick_wake_object_skips_trailless_objects(self):
        """口径函数本身：多个对象里挑**含 `wake`** 的那个；都没有 → `None`。"""
        objs = extract_json_objects(
            '{"rounds": {"total": 1}}\n{"wake": false, "rounds": {"total": 2}}')
        assert pick_wake_object(objs) == {"wake": False, "rounds": {"total": 2}}
        assert pick_wake_object([{"rounds": {"total": 1}}]) is None


# ==================================================================
# 🔴3 `_mini_yaml` 行内注释按 YAML 语义剥离（引号配对后再找 `#`）
# ==================================================================

OVERRIDE_WITH_QUOTED_COMMENTS = '''
locks:
  retry_delay_sec: 30   # 覆写"锁窗口"为 30s
loop:
  max_rounds: 7 # 最多"7 轮"
probe:
  crontab_interval_minutes: 15 # 疏化"扫描"
scan:
  allow_stability_repeats: true # 允许"连跑"
'''


class TestMiniYamlQuotedComments:
    def test_override_with_quoted_comments_takes_effect(self, monkeypatch):
        """**带引号注释**的覆写 → `locks/loop/probe/scan` **四域**都要读到生效值。

        旧实现"见引号就整行跳过"→ 值读成字符串 → 静默落回默认
        （文件写 30、生效 10），正是 🔴D 那一类"读了不生效"。
        """
        import secretary.collab.rules as R
        monkeypatch.setattr(R, "_yaml", None)     # 生产默认：无 PyYAML 走兜底
        path = os.path.join(tempfile.mkdtemp(), "ovr.yaml")
        with open(path, "w", encoding="utf-8") as f:
            f.write(OVERRIDE_WITH_QUOTED_COMMENTS)
        rules = CollabRules.from_yaml(path)
        assert rules.lock_retry_delay_sec == 30.0
        assert rules.max_rounds == 7
        assert rules.crontab_interval_minutes == 15
        assert rules.allow_stability_repeats is True
        assert rules.source == path and rules.scope == "repo"

    def test_parsed_values_are_typed_not_strings(self, monkeypatch):
        """解析结果必须是**数值/布尔**，不是半截字符串（判据本体）。"""
        import secretary.collab.rules as R
        monkeypatch.setattr(R, "_yaml", None)
        got = _mini_yaml(OVERRIDE_WITH_QUOTED_COMMENTS)
        assert got["locks"]["retry_delay_sec"] == 30
        assert got["loop"]["max_rounds"] == 7
        assert got["probe"]["crontab_interval_minutes"] == 15
        assert got["scan"]["allow_stability_repeats"] is True

    def test_unquoted_comments_still_stripped(self):
        """**反向对照**：注释不带引号时同样正确（没把剥离规则改坏）。"""
        got = _mini_yaml('locks:\n  retry_delay_sec: 30   # 覆写锁窗口\n')
        assert got == {"locks": {"retry_delay_sec": 30}}

    def test_hash_inside_quotes_is_not_a_comment(self):
        """**反向对照**：引号**内**的 `#` 不是注释（YAML 语义）。"""
        got = _mini_yaml('a: "x # y"\nb: \'p # q\'\n')
        assert got == {"a": "x # y", "b": "p # q"}

    def test_real_rule_package_bools_are_bools(self, monkeypatch):
        """真实规则包（本 PR 新增行 :77/:79）在无 PyYAML 路径下读成 **bool**。

        旧实现把它们读成字符串 → `_as_bool` 判不出 → 落回默认；"恰好与文件一致"
        纯属巧合（一旦覆写成反的，同样不生效）。
        """
        import secretary.collab.rules as R
        monkeypatch.setattr(R, "_yaml", None)
        with open(os.path.join(REPO_ROOT, "shared", "collab",
                               "collab-rules.yaml"), encoding="utf-8") as f:
            got = _mini_yaml(f.read())
        assert got["scan"]["repeat_rerun_on_failure"] is True
        assert got["scan"]["allow_stability_repeats"] is False
        rules = CollabRules.from_dict(got)
        assert rules.repeat_rerun_on_failure is True
        assert rules.allow_stability_repeats is False


# ==================================================================
# 🔴4 `fused` 安全旗标：非严格 `False` 一律按**已熔断**
# ==================================================================

class TestFusedFlagConservative:
    @pytest.mark.parametrize("bad", [1, "yes", "true", 0, "false", None, [], {}])
    def test_non_bool_fused_means_fused(self, tmp_dir, bad):
        """坏卡里的 `fused`（真值/非 bool）→ **已熔断**，不得被清掉。"""
        with open(os.path.join(tmp_dir, f"{SESSION}.json"), "w",
                  encoding="utf-8") as f:
            json.dump({"session_id": SESSION, "fused": bad}, f)
        g = LoopGuard(SESSION, data_dir=tmp_dir)
        assert g.is_fused() is True, bad
        assert g.should_wake_next() is False
        # 熔断态不得被后续轮次"跑活"：序列必须恒为 fuse
        assert g.register_round(blocking=1).action == LoopAction.FUSE.value

    def test_strict_false_is_not_fused(self, tmp_dir):
        """**反向对照**：严格 `False` → 未熔断（不得把正常状态也熔断掉）。"""
        with open(os.path.join(tmp_dir, f"{SESSION}.json"), "w",
                  encoding="utf-8") as f:
            json.dump({"session_id": SESSION, "fused": False}, f)
        g = LoopGuard(SESSION, data_dir=tmp_dir)
        assert g.is_fused() is False
        assert g.register_round(blocking=1).action == LoopAction.FIX.value

    @pytest.mark.parametrize("bad", [True, 1, "yes", "false", None])
    def test_adopt_budget_fused_is_conservative_too(self, tmp_dir, bad):
        """同族：`adopt_budget` 载荷里的 `fused` 非严格 `False` → 也按已熔断。"""
        g = LoopGuard(SESSION, data_dir=tmp_dir)
        assert g.is_fused() is False
        g.adopt_budget({"fused": bad, "rounds": {"total": 1}}, save=False)
        assert g.is_fused() is True, bad

    def test_adopt_budget_without_fused_key_does_not_fuse(self, tmp_dir):
        """**反向对照**：键缺失 → 不表态（不凭"没这个字段"就熔断）。"""
        g = LoopGuard(SESSION, data_dir=tmp_dir)
        g.adopt_budget({"rounds": {"total": 2}}, save=False)
        assert g.is_fused() is False
        assert g.status()["round_no"] == 2

    def test_explicit_fuse_survives_bad_card(self, tmp_dir):
        """显式 `fuse()` 后落盘的坏卡（`fused: 1`）不得把熔断读没。"""
        g = LoopGuard(SESSION, data_dir=tmp_dir)
        assert g.fuse("上游叫停") is True
        g2 = LoopGuard(SESSION, data_dir=tmp_dir)
        assert g2.is_fused() is True and g2.should_wake_next() is False


# ==================================================================
# 🟡1 载荷解析深度上界（深嵌套不得逃逸 `RecursionError`）
# ==================================================================

class TestCoercionDepthBound:
    DEEP = "{" + '"a":{' * 3000 + "}" * 3000

    def test_adopt_budget_does_not_raise(self, tmp_dir):
        g = LoopGuard(SESSION, data_dir=tmp_dir)
        assert g.adopt_budget(self.DEEP)["merged"] is False
        assert g.adopt_budget('{"budget_trail": "' + self.DEEP + '"}')["merged"] is False

    def test_facade_and_shared_do_not_raise(self, tmp_dir):
        sec = SecretarySession(SESSION, data_dir=tmp_dir)
        assert sec.inherit_budget(self.DEEP)["merged"] is False
        dis = dispatch_shared({"verdict": WAIT, "next_delay_sec": self.DEEP})
        assert dis["action"] in ("platform_await", "platform_crontab")
        assert dis["wake_allowed"] is False

    def test_checks_used_deep_payload_is_none(self, tmp_dir):
        p = CheckpointProbe(SESSION, data_dir=tmp_dir, clock=FakeClock())
        assert p._coerce_checks_used(self.DEEP) is None
        assert MAX_COERCE_DEPTH >= 1

    def test_reverse_shallow_payload_still_read(self, tmp_dir):
        """**反向对照**：正常浅载荷仍被读到（上界没把正常路径挡掉）。"""
        g = LoopGuard(SESSION, data_dir=tmp_dir)
        assert g.adopt_budget({"rounds": {"total": 3}})["adopted"]["round_no"] == 3


# ==================================================================
# 🟡2 `api_trigger_probe_wait` 真被产出（接线，不只是文档）
# ==================================================================

class TestAwaitEventWired:
    def test_await_trigger_names_the_wait_event(self, tmp_dir):
        """≤5min 的 await 落点必须产出承载它的事件名（此前只写在文档里）。"""
        p = CheckpointProbe(SESSION, data_dir=tmp_dir, clock=FakeClock())
        p.create("c2", kind="event", waiter_event="push", resolver_event="push")
        dis = p.dispatch(p.probe("c2", event_fired=False).event)
        assert dis["action"] == "platform_await"
        assert dis["trigger"]["type"] == "cnb:await"
        assert dis["trigger"]["event"] == "api_trigger_probe_wait"

    def test_crontab_trigger_keeps_check_event(self):
        """**反向对照**：长等待仍走 `crontab` + `api_trigger_probe_check`（不混用）。"""
        dis = dispatch_shared({"checkpoint_key": "cp", "verdict": WAIT,
                               "next_delay_sec": 600.0})
        assert dis["action"] == "platform_crontab"
        assert dis["trigger"]["type"] == "crontab"
        assert dis["trigger"]["event"] == "api_trigger_probe_check"
        assert dis["trigger"]["cron"] == CRON_FIXED_EXPR

    def test_cnb_yml_definition_matches_produced_event(self):
        """`.cnb.yml` 里 `api_trigger_probe_wait` 的定义与代码产出的名字同源。"""
        with open(os.path.join(REPO_ROOT, ".cnb.yml"), encoding="utf-8") as f:
            text = f.read()
        assert "api_trigger_probe_wait:" in text
        assert "*await_lock_task" in text
        assert "api_trigger_probe_wait" in text


# ==================================================================
# 🟡3 坏 `key` → `None`（不得退化成空标识互相覆盖）
# ==================================================================

class TestBadCheckpointKey:
    @pytest.mark.parametrize("bad", [5, "", "   ", None, [], {}])
    def test_bad_key_is_none(self, bad):
        assert Checkpoint.from_dict({"key": bad}) is None, bad

    def test_good_key_still_parses(self):
        """**反向对照**：合法 `key` 正常解析（没把好 key 也挡掉）。"""
        cp = Checkpoint.from_dict({"key": "cp-1"})
        assert cp is not None and cp.key == "cp-1"

    def test_bad_keys_do_not_collapse_into_one_file(self, tmp_dir):
        """坏 key 不再退化成**同一个空标识** → 不互相覆盖（旧实现落同一文件）。"""
        p = CheckpointProbe(SESSION, data_dir=tmp_dir, clock=FakeClock())
        assert Checkpoint.from_dict({"key": 5}) is None
        assert Checkpoint.from_dict({"key": 6}) is None
        # 只有"视为不存在"才安全：坏 key 不得被 `load()` 当成合法检查点
        with open(os.path.join(tmp_dir, "bad.json"), "w", encoding="utf-8") as f:
            json.dump({"key": 5, "checks_used": 3}, f)
        assert p.load("bad") is None


# ==================================================================
# 🟡4 `wake_allowed` 保守默认（判不出 → 不放行）
# ==================================================================

class TestWakeAllowedConservative:
    @pytest.mark.parametrize("bad", ["yes", "true", 1, 0, "false", None, [], {}])
    def test_non_bool_defaults_to_false(self, bad):
        ev = ProbeEvent.from_dict({"wake_allowed": bad})
        assert ev.wake_allowed is False, bad

    @pytest.mark.parametrize("good", [True, False])
    def test_strict_bool_is_honoured(self, good):
        """**反向对照**：严格 bool 原样读取（保守默认不得盖掉显式值）。"""
        assert ProbeEvent.from_dict({"wake_allowed": good}).wake_allowed is good

    def test_default_construction_is_conservative(self):
        """默认构造也是保守方向（不得凭空"允许唤醒"）。"""
        assert ProbeEvent().wake_allowed is False
        assert ProbeEvent(checkpoint_key="k").to_dict()["wake_allowed"] is False


# ==================================================================
# 🟡5 `checkpoint_plan(checks>档位数)` 不产重复行
# ==================================================================

class TestCheckpointPlanNoDuplicateRows:
    def test_checks_beyond_ladder_is_clamped(self, tmp_dir):
        """`checks=7`（> 6 档）→ 只到 6 档 + 用尽行，**不得**出现重复的 `check`。"""
        sec = SecretarySession(SESSION, data_dir=tmp_dir, rules_path=RULES)
        sec.create_checkpoint("cp-1")
        ids = [r["check"] for r in sec.checkpoint_plan("cp-1", checks=7)]
        assert ids == [1, 2, 3, 4, 5, 6, 7], ids
        assert len(set(ids)) == len(ids), "重复行（同一档报两遍）"

    def test_partial_checks_and_default(self, tmp_dir):
        """**反向对照**：`checks=2` → 3 行；不传 → 全档 + 用尽行。"""
        sec = SecretarySession(SESSION, data_dir=tmp_dir, rules_path=RULES)
        sec.create_checkpoint("cp-2")
        assert [r["check"] for r in sec.checkpoint_plan("cp-2", checks=2)] \
            == [1, 2, 7]
        assert len(sec.checkpoint_plan("cp-2")) == 7

    def test_invalid_checks_still_rejected(self, tmp_dir):
        sec = SecretarySession(SESSION, data_dir=tmp_dir, rules_path=RULES)
        sec.create_checkpoint("cp-3")
        with pytest.raises(ValueError):
            sec.checkpoint_plan("cp-3", checks=0)
        with pytest.raises(TypeError):
            sec.checkpoint_plan("cp-3", max_checks=9)
