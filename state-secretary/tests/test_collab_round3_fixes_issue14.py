"""Issue #14 复评第 3 轮定点修复回归（🔴A–🔴D + 🟡 结转）

本文件**只针对本轮评审提出的 4 项 🔴 与 6 项 🟡** 补判别性回归；
每组关键断言都配**反向对照**（防恒真、防"改了个位置但没治"）。

| 项 | 缺陷（评审实测） | 本文件判据 |
|---|---|---|
| 🔴A | 熔断/探测预算不跨容器、不跨棒继承 → 「最多 3 轮 / 6 次」永不触发 | 跨容器接力序列**必须出现 fuse**；跨容器探测 `checks_used` **必须递增到上界**；`external_rounds` / `external_checks` / 评论留痕三种载荷都要生效（反向：**不**传载荷时本地仍从 0 开始） |
| 🔴B | `payload.get("wake", True) is not False` → `"false"`/`0`/`"no"` 被判「可唤醒」 | 三组非 bool 一律**熔断**（反向：严格 `True` 仍可唤醒、无 `wake` 键的普通 context 不误熔断） |
| 🔴C | 合法 JSON 但类型不符 → `int()` 裸抛，逃逸到门面与跨仓入口 | `load`/`list_open`/`overview`/`create`/`probe` 与 `dispatch_shared` 全部**不抛**（反向：坏字段确实退化为默认值，不是被静默当成有效值） |
| 🔴D | 规则包数值"被读但不生效" / 同一份规则出两套动作 | 覆写 `locks.retry_delay_sec` **即生效**（快照同步）；覆写 `crontab_interval_minutes` 后**本地与跨仓落点一致**（反向：显式传参仍优先） |

🟡：恒真断言、`dispatch_shared` 副作用、`.cnb.yml` 锚点接线、文档口径、
无 PyYAML 兜底解析、死代码。
"""
from __future__ import annotations

import json
import os
import shutil
import tempfile

import pytest

from secretary.collab import (
    CRON_FIXED_EXPR,
    DRIVER_AWAIT,
    DRIVER_CRONTAB,
    UNKNOWN,
    WAIT,
    CheckpointProbe,
    dispatch_shared,
    load_rules,
)
from secretary.collab.probe import ProbeEvent
from secretary.collab.rules import _mini_yaml
from secretary.loop import LoopAction, LoopGuard, WAKE_UNKNOWN, parse_wake
from secretary.session import SecretarySession

SESSION = "i14r3"
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
    shutil.rmtree(d, ignore_errors=True)


def _fresh_probe(name: str = SESSION) -> CheckpointProbe:
    """每次调用给**新数据根**（模拟平台 crontab 的"新容器、新工作区"）。"""
    return CheckpointProbe(name, data_dir=tempfile.mkdtemp(), clock=FakeClock())


# ==================================================================
# 🔴A 预算跨容器 / 跨棒继承（「最多 3 轮 / 6 次」必须真能触发）
# ==================================================================

class TestCrossContainerBudget:
    def test_eight_relays_in_new_containers_eventually_fuse(self):
        """**跨容器接力 8 次**：预算随载荷走 → 序列里必须出现 `fuse`。

        反向对照（旧实现）：各自新数据根、不传载荷 → 处置序列恒为
        ``fix×8``，**从不熔断**（"最多 3 轮"在平台执行模型下永不触发）。
        """
        seq = []
        trail = None
        for _ in range(8):
            g = LoopGuard(SESSION, data_dir=tempfile.mkdtemp())
            dec = g.register_round(blocking=1, advisory=0,
                                   external_rounds=trail)
            trail = dec.budget_trail
            seq.append(dec.action)
        assert LoopAction.FUSE.value in seq, seq
        # 门禁预算 3：前 3 次 fix，第 4 次起 fuse（**不是**靠第 8 次才拦）
        assert seq[:3] == [LoopAction.FIX.value] * 3, seq
        assert seq[3] == LoopAction.FUSE.value, seq

    def test_without_payload_budget_resets_per_container(self):
        """**反向对照**：不传载荷时，每容器确实从 0 开始（证明上面那条判别性）。

        这不是"想要的"行为，而是平台执行模型的边界——故载荷是**必带项**。
        """
        seq = []
        for _ in range(8):
            g = LoopGuard(SESSION, data_dir=tempfile.mkdtemp())
            seq.append(g.register_round(blocking=1).action)
        assert set(seq) == {LoopAction.FIX.value}, seq

    def test_seven_probes_in_new_containers_reach_the_ceiling(self):
        """**跨容器探测 7 次**：`checks_used` 递增到上界并 `resume_required`。

        反向对照（旧实现）：`checks_used` 恒为 1、`checks_left` 恒 5、
        `resume_required` 永远 False。
        """
        used, out = 0, None
        for _ in range(7):
            p = _fresh_probe()
            out = p.probe("cp1", confirmed=False, external_checks=used)
            used = out.event.checks_used
        assert used == 6, used
        assert out.event.checks_left == 0
        assert out.event.resume_required is True

    def test_probe_accepts_budget_payload_forms(self):
        """三种载荷形态都要生效：int / ``{"checks_used": n}`` / 评论留痕标记。"""
        trail = ("<!-- probe-budget: "
                 + json.dumps({"checkpoint_key": "cp1", "checks_used": 5,
                               "max_checks": 6})
                 + " -->")
        for payload in (5, {"checks_used": 5},
                        ProbeEvent(checks_used=5).to_dict(), trail):
            p = _fresh_probe()
            out = p.probe("cp1", confirmed=False, external_checks=payload)
            assert out.event.checks_used == 6, payload

    def test_budget_merge_is_monotonic(self):
        """``max()`` 合并：外部轮次**只能抬高**，不能被小值拉回（防"改小即绕过"）。"""
        g = LoopGuard(SESSION, data_dir=tempfile.mkdtemp())
        g.register_round(blocking=1)
        g.register_round(blocking=1)
        res = g.adopt_budget({"rounds": {"total": 1, "blocking_rounds": 0}})
        assert res["adopted"]["round_no"] == 2, "小值不得把已消耗轮次拉回"
        res2 = g.adopt_budget({"rounds": {"total": 9, "blocking_rounds": 9}})
        assert res2["adopted"]["round_no"] == 9

    def test_handoff_payload_carries_budget_across_container(self, tmp_dir):
        """端到端：熔断会话的**静默交接卡 context** 即可被**新容器**继承。

        反向对照：不带 `loop-budget` 留痕的普通 context → 不继承熔断
        （否则等于对任意交接都误熔断）。
        """
        sec = SecretarySession("x9", data_dir=tmp_dir, max_rounds=3)
        for _ in range(4):
            sec.review_round(blocking=1)
        card = sec.silent_handoff("a-unit", "b-unit", "熔断交接",
                                  open_items=["残留"])
        payload = json.loads(card.context)
        assert payload["wake"] is False
        assert "loop-budget" in payload["budget_trail"]

        other = tempfile.mkdtemp()          # **新容器**
        nxt = SecretarySession("x9", data_dir=other)
        assert nxt.loop_status()["round_no"] == 0    # 起手读不到上一容器磁盘
        assert nxt.inherit_silent_handoff(card.context) is True
        assert nxt.loop_status()["round_no"] == 4
        assert nxt.loop_status()["blocking_rounds"] == 3
        assert nxt.should_wake_next() is False

    def test_regular_relay_also_carries_budget(self, tmp_dir):
        """**常规接力也要带预算**：否则"每棒从 0 开始"照样稀释 3 轮上界。"""
        g = LoopGuard(SESSION, data_dir=tmp_dir, max_rounds=3)
        g.register_round(blocking=1)
        g.register_round(blocking=1)
        trail = g.budget_trail()
        other = LoopGuard(SESSION, data_dir=tempfile.mkdtemp(), max_rounds=3)
        other.adopt_budget(trail, source="relay")
        assert other.status()["round_no"] == 2
        # 新容器里只还能修 1 轮（不是从 0 重新给 3 轮）
        assert other.register_round(blocking=1).action == LoopAction.FIX.value
        assert other.register_round(blocking=1).action == LoopAction.FUSE.value

    def test_budget_trail_is_comment_embeddable(self):
        """留痕形态可直接贴进评论正文并被解析（可持久载体 = 评论，不是容器）。"""
        g = LoopGuard(SESSION, data_dir=tempfile.mkdtemp())
        g.register_round(blocking=1)
        text = f"复评结论：仍有 🔴。\n\n{g.budget_trail()}\n\n@下一棒"
        other = LoopGuard(SESSION, data_dir=tempfile.mkdtemp())
        res = other.adopt_budget(text, source="comment")
        assert res["merged"] is True
        assert res["adopted"]["round_no"] == 1


# ==================================================================
# 🔴B 静默交接：`wake` 严格 bool（读入侧与写出侧同口径）
# ==================================================================

class TestStrictWakeOnInherit:
    @pytest.mark.parametrize("bad", ["false", 0, "no", "0", 1, None, []])
    def test_non_bool_wake_is_conservatively_silent(self, bad):
        """**三组以上反向对照**：非 bool 的 `wake` → 判不出 → **保守按静默**。

        旧实现 `is not False`：``"false"``/``0``/``"no"`` 全被判「可唤醒」，
        熔断语义直接漏出去（写出侧 `create_handoff` 严、读入侧松，自相矛盾）。
        """
        g = LoopGuard.from_handoff(SESSION, data_dir=tempfile.mkdtemp(),
                                   handoff_context={"wake": bad,
                                                    "rounds": {"total": 2}})
        assert g.is_fused() is True, bad
        assert g.should_wake_next() is False, bad

    def test_strict_true_still_awake(self):
        """反向对照：严格 `True` 仍走常规接力（不得对正常载荷误熔断）。"""
        g = LoopGuard.from_handoff(SESSION, data_dir=tempfile.mkdtemp(),
                                   handoff_context={"wake": True})
        assert g.is_fused() is False
        assert g.should_wake_next() is True

    def test_missing_wake_key_is_not_unknown(self):
        """反向对照：**不带** `wake` 键的普通 context → 常规接力（不熔断）。

        静默交接载荷**必带** ``wake``（见 ``silent_handoff_payload``），
        故"缺失"不构成"判不出"；否则任意交接 context 都会被误熔断。
        """
        g = LoopGuard.from_handoff(SESSION, data_dir=tempfile.mkdtemp(),
                                   handoff_context={"intent": "常规接力"})
        assert g.is_fused() is False

    def test_parse_wake_three_states(self):
        """口径函数本身：只认严格 bool，其余一律 `unknown`。"""
        assert parse_wake(True) == "awake"
        assert parse_wake(False) == "silent"
        for bad in ("false", "true", 0, 1, None, {}, []):
            assert parse_wake(bad) == WAKE_UNKNOWN, bad

    def test_write_side_and_read_side_same_bar(self, tmp_dir):
        """**口径自洽**：写出侧拒绝 `wake="false"`，读入侧也不得把它当真值。"""
        from secretary.handoff import HandoffBus
        bus = HandoffBus(data_dir=os.path.join(tmp_dir, "handoffs"),
                         state_root=tmp_dir)
        with pytest.raises(TypeError):
            bus.create_handoff("a", "b", "i", wake="false")
        # 磁盘上被写成字符串的卡（历史/外部写入）读回 → 继承侧保守熔断
        ho_dir = os.path.join(tmp_dir, "handoffs")
        with open(os.path.join(ho_dir, "bad-wake.json"), "w",
                  encoding="utf-8") as f:
            json.dump({"id": "bad-wake", "source_unit": "a",
                       "target_unit": "b", "intent": "i",
                       "context": json.dumps({"wake": "false"})}, f)
        ctx = bus.get("bad-wake").context
        g = LoopGuard.from_handoff(SESSION, data_dir=tempfile.mkdtemp(),
                                   handoff_context=ctx)
        assert g.is_fused() is True

    def test_non_bool_wake_keeps_budget_too(self):
        """保守熔断时**轮次预算一并继承**（不能只翻标志位、把计数丢掉）。"""
        g = LoopGuard.from_handoff(SESSION, data_dir=tempfile.mkdtemp(),
                                   handoff_context={"wake": "no",
                                                    "rounds": {"total": 3,
                                                               "blocking_rounds": 3}})
        st = g.status()
        assert st["fused"] is True
        assert st["round_no"] == 3 and st["blocking_rounds"] == 3


# ==================================================================
# 🔴C 字段级容错：合法 JSON + 类型不符**不得裸抛**
# ==================================================================

BAD_CHECKPOINT = {
    "key": "cp", "checks_used": "NaN", "max_checks": "x",
    "next_delay_sec": "soon", "released": "yes",
    "last_facts": "not-a-dict", "history": "nope",
    "session_id": 42, "kind": None,
}


class TestFieldLevelTolerance:
    @pytest.fixture
    def probe_with_bad_cp(self, tmp_dir):
        p = CheckpointProbe(SESSION, data_dir=tmp_dir, clock=FakeClock())
        with open(os.path.join(tmp_dir, "cp.json"), "w", encoding="utf-8") as f:
            json.dump(BAD_CHECKPOINT, f)
        return p

    def test_read_paths_do_not_raise(self, probe_with_bad_cp):
        """``load`` / ``list_open`` / ``overview`` / ``create``（幂等分支）全不抛。"""
        p = probe_with_bad_cp
        assert p.load("cp") is not None
        assert len(p.list_open()) == 1
        assert p.overview()["count"] == 1
        assert p.create("cp").key == "cp"

    def test_probe_does_not_raise(self, probe_with_bad_cp):
        """探测路径同样不抛（判定退化为 `unknown` → 继续等，不误放行）。"""
        out = probe_with_bad_cp.probe("cp", confirmed=False)
        assert out.verdict in (UNKNOWN, WAIT)

    def test_bad_fields_degrade_to_defaults(self, probe_with_bad_cp):
        """**反向对照**：坏字段确实**退化默认**，不是被静默当成有效值。"""
        cp = probe_with_bad_cp.load("cp")
        assert cp.checks_used == 0 and cp.max_checks == 6
        assert cp.next_delay_sec == 0.0
        assert cp.released is False and cp.last_facts == {} and cp.history == []
        assert cp.session_id == "" and cp.kind == "event"

    def test_facade_does_not_raise(self, tmp_dir):
        """门面四个入口（`checkpoints_overview` / `create_checkpoint` /
        `probe_checkpoint` / `checkpoint_plan`）不裸抛。"""
        sec = SecretarySession(SESSION, data_dir=tmp_dir, rules_path=RULES)
        with open(os.path.join(sec.probe.data_dir, "cp.json"), "w",
                  encoding="utf-8") as f:
            json.dump(BAD_CHECKPOINT, f)
        assert sec.checkpoints_overview()["count"] == 1
        assert sec.create_checkpoint("cp").key == "cp"
        assert sec.probe_checkpoint("cp").verdict in (UNKNOWN, WAIT)
        assert sec.checkpoint_plan("cp")

    def test_probe_event_from_dict_tolerates_bad_types(self):
        """跨仓载荷 `ProbeEvent.from_dict` 类型不符 → 退化默认（不抛）。"""
        ev = ProbeEvent.from_dict({"next_delay_sec": "soon", "checks_used": "NaN",
                                   "facts": "nope", "wake_allowed": "yes"})
        assert ev.next_delay_sec == 0.0 and ev.checks_used == 0
        assert ev.facts == {} and ev.wake_allowed is True

    def test_dispatch_shared_tolerates_bad_types(self):
        """跨仓入口 `dispatch_shared` 类型不符 → **不抛**，判不出即继续等。"""
        dis = dispatch_shared({"verdict": "wait", "next_delay_sec": "soon"})
        assert dis["action"] in ("platform_await", "platform_crontab")
        assert dis["wake_allowed"] is False

    def test_dispatch_shared_survives_garbage(self):
        """**反向对照**：整份载荷不可解析也不得炸（→ `unknown`，绝不误放行）。"""
        for garbage in (None, 42, "not json", [1, 2], {"verdict": object()}):
            dis = dispatch_shared(garbage)
            assert dis["wake_allowed"] is False
            assert dis["verdict"] == UNKNOWN

    def test_same_bar_as_loop_guard_family(self, tmp_dir):
        """**口径一致性**：与 `LoopGuard` / `LockRegistry` 同族（坏字段退化不炸）。"""
        g = LoopGuard(SESSION, data_dir=tmp_dir)
        g._state.round_no = 2
        bad = {"round_no": "NaN", "fused": "yes", "history": "x"}
        st = type(g._state).from_dict(bad, SESSION)
        assert st.round_no == 0 and st.fused is False and st.history == []


# ==================================================================
# 🔴D 规则包数值：消费点补全（覆写即生效 + 本地/跨仓一致）
# ==================================================================

class TestRulesConsumptionPoints:
    def test_lock_retry_delay_override_takes_effect(self, tmp_dir):
        """🔴D-1：覆写 `locks.retry_delay_sec` → 锁窗口**真的变**（不是读了不生效）。"""
        path = os.path.join(tmp_dir, "r.yaml")
        with open(path, "w", encoding="utf-8") as f:
            f.write("locks:\n  retry_delay_sec: 30\n")
        sec = SecretarySession(SESSION, data_dir=tmp_dir, rules_path=path)
        assert sec.collab_rules.lock_retry_delay_sec == 30.0
        assert sec.review_arbiter.retry_delay_sec == 30.0, "覆写必须生效"
        assert sec.lock_registry.ttl_sec == 30.0
        assert sec.collab_status()["locks"]["retry_delay_sec"] == 30.0

    def test_default_lock_delay_is_ladder_first_step(self, tmp_dir):
        """默认首档 = 阶梯第一档（10s）——与 §3 阶梯口径同一处定义。"""
        sec = SecretarySession(SESSION, data_dir=tmp_dir, rules_path=RULES)
        assert sec.collab_rules.lock_retry_delay_sec == 10.0
        assert sec.collab_rules.waiting_ladder()[0] == 10.0
        assert sec.review_arbiter.retry_delay_sec == 10.0

    def test_explicit_param_still_wins(self, tmp_dir):
        """**反向对照**：显式传参优先于规则包（调用方有明确口径时以调用方为准）。"""
        path = os.path.join(tmp_dir, "r.yaml")
        with open(path, "w", encoding="utf-8") as f:
            f.write("locks:\n  retry_delay_sec: 30\n")
        sec = SecretarySession(SESSION, data_dir=tmp_dir, rules_path=path,
                               retry_delay_sec=45.0)
        assert sec.review_arbiter.retry_delay_sec == 45.0
        assert sec.collab_status()["locks"]["explicit_override"] is True

    def test_crontab_interval_local_and_cross_repo_agree(self, tmp_dir):
        """🔴D-2：覆写 cron 间隔 → **本地与跨仓落点一致**（同一份规则出同一套动作）。

        旧实现：`cron_expr_for(rules) = */15`，而 `dispatch_shared(...)`
        仍硬编码 `*/5` —— 同一份规则包出两套落点，正是"跨仓漂移"。
        """
        path = os.path.join(tmp_dir, "r.yaml")
        with open(path, "w", encoding="utf-8") as f:
            f.write("probe:\n  crontab_interval_minutes: 15\n")
        rules = load_rules(path)
        from secretary.collab.probe import cron_expr_for
        assert cron_expr_for(rules) == "*/15 * * * *"
        dis = dispatch_shared({"checkpoint_key": "cp", "verdict": WAIT,
                               "driver": DRIVER_CRONTAB,
                               "next_delay_sec": 600.0}, rules)
        assert dis["trigger"]["cron"] == "*/15 * * * *", dis["trigger"]

    def test_crontab_interval_explicit_param_still_wins(self, tmp_dir):
        """**反向对照**：显式传 `crontab_base_minutes` 仍优先（兼容旧调用点）。"""
        path = os.path.join(tmp_dir, "r.yaml")
        with open(path, "w", encoding="utf-8") as f:
            f.write("probe:\n  crontab_interval_minutes: 15\n")
        rules = load_rules(path)
        dis = dispatch_shared({"verdict": WAIT, "driver": DRIVER_CRONTAB,
                               "next_delay_sec": 600.0}, rules,
                              crontab_base_minutes=5)
        assert dis["trigger"]["cron"] == CRON_FIXED_EXPR

    def test_crontab_floor_still_clamped(self, tmp_dir):
        """**反向对照**：调小仍被平台 5 分钟下限夹住（配不出更密的扫描）。"""
        path = os.path.join(tmp_dir, "r.yaml")
        with open(path, "w", encoding="utf-8") as f:
            f.write("probe:\n  crontab_interval_minutes: 1\n")
        rules = load_rules(path)
        dis = dispatch_shared({"verdict": WAIT, "driver": DRIVER_CRONTAB,
                               "next_delay_sec": 600.0}, rules)
        assert dis["trigger"]["cron"] == CRON_FIXED_EXPR

    def test_split_threshold_from_rules_applies_cross_repo(self, tmp_dir):
        """分工阈值也来自规则包：跨仓落点与本地**同阈值**（不各判一套）。"""
        path = os.path.join(tmp_dir, "r.yaml")
        with open(path, "w", encoding="utf-8") as f:
            f.write("probe:\n  await_max_sec: 120\n")
        rules = load_rules(path)
        # 180s > 120s → 交 crontab（本地同一份规则也必须这么判）
        dis = dispatch_shared({"verdict": WAIT, "driver": DRIVER_AWAIT,
                               "next_delay_sec": 180.0}, rules)
        assert dis["action"] == "platform_crontab", dis


# ==================================================================
# 🟡1–🟡6 结转建议
# ==================================================================

class TestAdvisoryItems:
    def test_y1_no_always_true_assertion_in_confirm_suite(self):
        """🟡1：`test_collab_confirm_issue14.py` 里不得再有 `... or True` 恒真断言。"""
        path = os.path.join(REPO_ROOT, "state-secretary", "tests",
                            "test_collab_confirm_issue14.py")
        with open(path, "r", encoding="utf-8") as f:
            text = f.read()
        code = "\n".join(ln.split("#", 1)[0] for ln in text.splitlines())
        assert "or True" not in code, "恒真断言（护栏失效）未清理"
        # 反向对照：断言本身可失败（不是"扫了个不存在的模式"）
        assert "or True" in "x = 1 or True"

    def test_y2_dispatch_shared_has_no_io_side_effect(self, tmp_dir, monkeypatch):
        """🟡2：`dispatch_shared` 声明"纯函数" → **不得**在 cwd 下造目录。"""
        work = tempfile.mkdtemp()
        monkeypatch.chdir(work)
        dispatch_shared({"checkpoint_key": "cp", "verdict": WAIT,
                         "driver": DRIVER_CRONTAB, "next_delay_sec": 600.0})
        assert not os.path.exists(os.path.join(work, "data")), \
            "落点计算不得污染工作区"

    def test_y3_await_anchor_is_referenced(self):
        """🟡3：`.cnb.yml` 的 `&await_lock_task` 必须有引用（否则口径未接线）。"""
        path = os.path.join(REPO_ROOT, ".cnb.yml")
        with open(path, "r", encoding="utf-8") as f:
            text = f.read()
        assert "&await_lock_task" in text
        assert "*await_lock_task" in text, "锚点定义后无引用（口径未接线）"
        assert "api_trigger_probe_wait" in text

    def test_y4_docs_single_lock_delay_claim(self):
        """🟡4：文档口径统一——「延迟 1 分钟」不再是**首档**主张。"""
        for rel in ("docs/secretary-gate-workflow.md",
                    "docs/design/state-secretary-lock-registry.md",
                    "docs/secretary-collab-rules.md"):
            with open(os.path.join(REPO_ROOT, rel), "r", encoding="utf-8") as f:
                text = f.read()
            assert "延迟 **1 分钟**后检测" not in text, rel
            assert "延迟首档 10s" in text or "阶梯第一档" in text, rel

    def test_y5_mini_yaml_reads_multiline_forms(self):
        """🟡5：无 PyYAML 时兜底解析器能读**多行 inline list** 与**折叠块**。

        旧实现：`waiting_note: >-` 读成字符串 `">-"`；多行列表读成半截字符串
        → 组织级自定义值被**静默丢弃**，而 `chain` 仍把该文件标为来源。
        """
        text = (
            "probe:\n"
            "  drivers: [\"platform_await\", \"platform_crontab\",\n"
            "            \"external_webhook\"]\n"
            "  max_checks: 4\n"
            "platform:\n"
            "  waiting_note: >-\n"
            "    平台没有等待计费项，\n"
            "    本地 sleep 才烧核时。\n"
            "  waiting_billed: false\n"
        )
        got = _mini_yaml(text)
        assert got["probe"]["drivers"] == ["platform_await", "platform_crontab",
                                          "external_webhook"]
        assert got["probe"]["max_checks"] == 4
        assert got["platform"]["waiting_note"] == \
            "平台没有等待计费项， 本地 sleep 才烧核时。"
        assert got["platform"]["waiting_billed"] is False

    def test_y5_real_rule_packages_parse_without_pyyaml(self):
        """真实规则包（含折叠块 + 多行列表）在**无 PyYAML** 兜底路径下也读全。"""
        for rel in ("shared/collab/organization.yaml",
                    "shared/collab/collab-rules.yaml"):
            with open(os.path.join(REPO_ROOT, rel), "r", encoding="utf-8") as f:
                got = _mini_yaml(f.read())
            assert got["probe"]["max_checks"] == 6, rel
            assert len(got["probe"]["drivers"]) == 5, rel
            assert got["platform"]["waiting_billed"] is False, rel
            assert got["platform"]["waiting_note"] != ">-", rel

    def test_y6_dead_code_removed(self):
        """🟡6：`probe.py` 的死代码（未使用的 `_SAFE_ID`、`_cron_every` 别名）已清理。"""
        path = os.path.join(REPO_ROOT, "state-secretary", "src", "secretary",
                            "collab", "probe.py")
        with open(path, "r", encoding="utf-8") as f:
            text = f.read()
        assert "_cron_every = " not in text
        assert "_SAFE_ID = re.compile" not in text


# ==================================================================
# 🟢 优化项
# ==================================================================

class TestMinorOptimizations:
    def test_checkpoint_plan_kwargs_now_named_and_real(self, tmp_dir):
        """🟢：`checkpoint_plan` 的形参不再**静默忽略**（改具名且真透传）。"""
        sec = SecretarySession(SESSION, data_dir=tmp_dir, rules_path=RULES)
        sec.create_checkpoint("cp-1")
        # 误用（未知关键字）直接暴露，而不是被悄悄丢掉
        with pytest.raises(TypeError):
            sec.checkpoint_plan("cp-1", max_checks=99)
        # 具名形参**真的生效**（反向对照：不是"换了个名字还是没用"）
        assert len(sec.checkpoint_plan("cp-1")) == 7      # 6 档 + 用尽行
        assert len(sec.checkpoint_plan("cp-1", checks=2)) == 3
        with pytest.raises(ValueError):
            sec.checkpoint_plan("cp-1", checks=0)

    def test_y5_nested_map_inside_sequence_item(self):
        """🟢：`- key: value` 之后的**同级兄弟键**（嵌套映射）不再丢字段。"""
        got = _mini_yaml(
            "list:\n"
            "  - k: v\n"
            "    m: n\n"
            "  - k2: v2\n"
            "after: 9\n")
        assert got["list"] == [{"k": "v", "m": "n"}, {"k2": "v2"}]
        assert got["after"] == 9

    def test_dispatch_recomputes_driver_from_local_rules(self):
        """**反向对照**（安全加固）：事件自带的 `driver` 不得被轻信。

        否则跨仓发送方只要写 `driver=platform_await`，就能把 10 分钟的等待
        也挂成单个 await —— 即"押注对端一定会 resolve"的死等。
        """
        # 事件谎报 driver=await + 长等待 → 按本地规则重算 → 交 crontab
        dis = dispatch_shared({"checkpoint_key": "cp", "verdict": WAIT,
                               "driver": DRIVER_AWAIT,
                               "next_delay_sec": 600.0,
                               "waiter_event": "push", "resolver_event": "push"})
        assert dis["action"] == "platform_crontab", dis
        # 正向：短等待 + 配对成立 → 仍按 await（分工本身没被改坏）
        short = dispatch_shared({"checkpoint_key": "cp", "verdict": WAIT,
                                 "driver": DRIVER_AWAIT, "next_delay_sec": 10.0,
                                 "waiter_event": "push",
                                 "resolver_event": "push"})
        assert short["action"] == "platform_await", short

    def test_driver_for_delay_tolerates_bad_threshold(self):
        """宽进：坏阈值/坏时长不得炸；判不出取**保守落点**（crontab）。"""
        from secretary.collab.probe import _driver_for_delay
        assert _driver_for_delay(600.0, "bad") == DRIVER_CRONTAB
        assert _driver_for_delay("soon", 300.0) == DRIVER_CRONTAB
        assert _driver_for_delay(600.0, -1) == DRIVER_CRONTAB
        assert _driver_for_delay(10.0, 300.0) == DRIVER_AWAIT
