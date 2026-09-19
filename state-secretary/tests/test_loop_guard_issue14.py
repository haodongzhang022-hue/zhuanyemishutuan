"""Issue #14 循环熔断回归（轮次预算 + 非必要修复最多 1 次 + 静默交接不唤醒）

判据（本文件全部用例围绕的三条上界）：
  1. 门禁（🔴）修复轮次上界 ``max_rounds``（默认 3）；
  2. 非必要（🟡）修复上界 ``max_advisory_fix_rounds``（默认 1）——
     “无必要修复项，最多修 1 次”；
  3. 修复总轮次硬上界 ``max_total_rounds``——防 1/2 交替绕过。

熔断语义：``wake=False``，交接给下一个 agent 时**只登记、不唤醒**；
仅人工 ``resume(by=…)`` 可恢复修复预算。

**判别性设计**：每个关键断言都配反向对照或边界佐证，避免恒真断言——
- ``preview`` 纯函数性断言：预览前后 ``status()`` 完全一致（非仅比轮次）；
- 总轮次上界断言同时佐证**门禁预算仍在**（``budget_left>=1``），
  证明拦住循环的是总上界本身，而非被门禁上界顺带拦下（否则断言无判别力）；
- ``inherit_silent_handoff`` 配 3 组非静默载荷反向对照（wake=True / 空 /
  坏 JSON）——确保"熔断继承"来自载荷语义而非该 API 恒真返回 True。
"""
from __future__ import annotations

import json
import os
import shutil
import tempfile

import pytest

from secretary.handoff import HandoffBus
from secretary.loop import LoopAction, LoopGuard
from secretary.session import SecretarySession


@pytest.fixture
def tmp_dir():
    d = tempfile.mkdtemp()
    yield d
    shutil.rmtree(d, ignore_errors=True)


# ------------------------------------------------------------------
# ① 门禁修复轮次上界
# ------------------------------------------------------------------

class TestRoundBudget:
    def test_gate_rounds_within_budget_wake(self, tmp_dir):
        """🔴 在 3 轮预算内：允许修复且允许唤醒下一轮复评。"""
        g = LoopGuard("s1", data_dir=tmp_dir, max_rounds=3)
        for i in range(3):
            d = g.register_round(blocking=1, advisory=0, note=f"第{i+1}轮")
            assert d.action == LoopAction.FIX.value
            assert d.wake is True
            assert d.round_no == i + 1
        assert g.is_fused() is False
        assert g.should_wake_next() is True

    def test_gate_round_four_fuses_no_wake(self, tmp_dir):
        """预算耗尽（第 4 次 🔴）→ 熔断、不唤醒（反向对照上一条）。"""
        g = LoopGuard("s2", data_dir=tmp_dir, max_rounds=3)
        for _ in range(3):
            g.register_round(blocking=1)
        d = g.register_round(blocking=1)
        assert d.action == LoopAction.FUSE.value
        assert d.wake is False
        assert d.fused_now is True
        assert g.is_fused() is True
        assert g.should_wake_next() is False
        assert "门禁修复轮次" in d.reason

    def test_fused_stays_fused_on_subsequent_rounds(self, tmp_dir):
        """熔断后**任何**后续轮次仍熔断，且 ``fused_now`` 只在首爆为 True。"""
        g = LoopGuard("s3", data_dir=tmp_dir, max_rounds=0)
        first = g.register_round(blocking=1)
        assert first.fused_now is True
        again = g.register_round(blocking=1)
        assert again.action == LoopAction.FUSE.value
        assert again.fused_now is False

    def test_preview_is_pure_function(self, tmp_dir):
        """``preview`` 不改状态、不落盘（前后 ``status()`` 全等）。"""
        g = LoopGuard("s4", data_dir=tmp_dir, max_rounds=3)
        before = dict(g.status())
        g.preview(blocking=1, advisory=1)
        g.preview(blocking=99, advisory=99)
        assert g.status() == before
        assert not os.path.exists(g.state_path())

    def test_clear_does_not_consume_budget(self, tmp_dir):
        """``CLEAR``（无 🔴 无 🟡）不消耗预算、不唤醒——门禁放行不被空转挤掉。"""
        g = LoopGuard("s5", data_dir=tmp_dir, max_rounds=1)
        for _ in range(5):
            d = g.register_round(blocking=0, advisory=0)
            assert d.action == LoopAction.CLEAR.value
            assert d.wake is False
        assert g.status()["blocking_rounds"] == 0
        # 反向佐证：预算未被空转消耗，门禁轮仍可修
        assert g.register_round(blocking=1).action == LoopAction.FIX.value

    def test_fused_rejects_clear_action(self, tmp_dir):
        """熔断为终态：即便「本轮无新项」也不得降级为 CLEAR（防熔断被放行绕开）。"""
        g = LoopGuard("s6", data_dir=tmp_dir, max_rounds=0)
        g.register_round(blocking=1)
        d = g.register_round(blocking=0, advisory=0)
        assert d.action == LoopAction.FUSE.value
        assert d.wake is False


# ------------------------------------------------------------------
# ② 非必要修复「最多 1 次」
# ------------------------------------------------------------------

class TestAdvisoryMaxOneFix:
    def test_advisory_allows_exactly_one_fix(self, tmp_dir):
        """仅剩 🟡：第 1 次允许修（fix_last），第 2 次同类即熔断。"""
        g = LoopGuard("a1", data_dir=tmp_dir)
        first = g.register_round(blocking=0, advisory=3)
        assert first.action == LoopAction.FIX_LAST.value
        assert first.wake is True
        second = g.register_round(blocking=0, advisory=1)
        assert second.action == LoopAction.FUSE.value
        assert second.wake is False
        assert second.advisory_left == 0
        assert "非必要修复" in second.reason

    def test_gate_round_resets_advisory_quota(self, tmp_dir):
        """反向对照：门禁轮重置非必要计数——🟡 → 🔴 → 🟡 仍允许修 1 次。

        若实现改成"非必要只算一次全局"，本断言会失败（判别性）。
        """
        g = LoopGuard("a2", data_dir=tmp_dir, max_rounds=3)
        assert g.register_round(blocking=0, advisory=1).action == LoopAction.FIX_LAST.value
        assert g.register_round(blocking=1, advisory=0).action == LoopAction.FIX.value
        again = g.register_round(blocking=0, advisory=2)
        assert again.action == LoopAction.FIX_LAST.value
        assert again.wake is True

    def test_advisory_zero_budget_fuses_immediately(self, tmp_dir):
        """``max_advisory_fix_rounds=0``（零容忍档）→ 首报 🟡 即熔断。"""
        g = LoopGuard("a3", data_dir=tmp_dir, max_advisory_fix_rounds=0)
        d = g.register_round(blocking=0, advisory=1)
        assert d.action == LoopAction.FUSE.value
        assert d.wake is False


# ------------------------------------------------------------------
# ③ 修复总轮次硬上界（防交替绕过）
# ------------------------------------------------------------------

class TestTotalRoundHardCap:
    def test_alternating_rounds_hit_total_cap_not_gate_cap(self, tmp_dir):
        """🔴/🟡 交替出现：由**总轮次上界**拦住，而非门禁上界顺带拦住。

        两套预算**解耦**：``max_rounds=5``（门禁充足）、
        ``max_advisory_fix_rounds=1``（非必要仅 1 次），总上界显式压到 **2**
        （等价于"🔴 与 🟡 交替各修 1 次就收口"的严格档）。序列
        🔴 🟡 用尽总预算后，第 3 次 🔴 时门禁预算**仍剩 4**（``budget_left>=1``），
        却因 ``total_left==0`` 熔断——断言同时锁定「挡住的是总上界」。
        判别力：把 ``max_total_rounds`` 放到默认（=7）时该轮为 FIX，本用例失败。
        """
        g = LoopGuard("t1", data_dir=tmp_dir, max_rounds=5,
                      max_advisory_fix_rounds=1, max_total_rounds=2)
        seq = [1, 0]  # blocking 计数；advisory = 1-blocking
        actions = []
        for b in seq:
            actions.append(g.register_round(blocking=b, advisory=1 - b).action)
        assert actions == [LoopAction.FIX.value, LoopAction.FIX_LAST.value]
        d = g.register_round(blocking=1, advisory=0)
        assert d.action == LoopAction.FUSE.value
        assert d.total_left == 0
        assert d.budget_left >= 1, "门禁预算仍在 → 拦住循环的是总轮次上界"
        assert d.advisory_left >= 0
        assert "总轮次" in d.reason

    def test_explicit_total_cap_override(self, tmp_dir):
        """显式 ``max_total_rounds`` 生效（此处压到 1）。"""
        g = LoopGuard("t2", data_dir=tmp_dir, max_rounds=99,
                      max_advisory_fix_rounds=99, max_total_rounds=1)
        assert g.register_round(blocking=1).action == LoopAction.FIX.value
        d = g.register_round(blocking=1)
        assert d.action == LoopAction.FUSE.value
        assert d.total_left == 0

    def test_clear_rounds_do_not_consume_total_budget(self, tmp_dir):
        """总上界只统计**修复**轮：CLEAR 轮再多也不触发熔断。"""
        g = LoopGuard("t3", data_dir=tmp_dir, max_rounds=2,
                      max_advisory_fix_rounds=1, max_total_rounds=2)
        for _ in range(4):
            assert g.register_round(blocking=0, advisory=0).action \
                == LoopAction.CLEAR.value
        assert g.is_fused() is False
        assert g.register_round(blocking=1).action == LoopAction.FIX.value


# ------------------------------------------------------------------
# ④ 静默交接（wake=False）：不唤醒
# ------------------------------------------------------------------

class TestSilentHandoff:
    def test_silent_handoff_card_not_waking(self, tmp_dir):
        sec = SecretarySession("h1", data_dir=tmp_dir)
        sec.review_round(blocking=0, advisory=1)
        sec.review_round(blocking=0, advisory=1)  # → 熔断
        assert sec.should_wake_next() is False
        card = sec.silent_handoff("a-unit", "b-unit", "循环熔断交接",
                                  open_items=["🟡 结转项"])
        assert card.wake is False
        assert card.fuse_reason
        payload = json.loads(card.context)
        assert payload["wake"] is False
        assert payload["fused"] is True
        assert payload["open_items"] == ["🟡 结转项"]
        assert "resume" in payload["resume_policy"]

    def test_wake_false_survives_reload(self, tmp_dir):
        """落盘后重新读取仍为 wake=False（否则跨实例复活循环）。"""
        bus = HandoffBus(data_dir=os.path.join(tmp_dir, "handoffs"),
                         state_root=tmp_dir)
        c = bus.create_handoff("a", "b", "静默意图", "ctx",
                               auto_state_card=True, wake=False)
        assert c.wake is False
        # 新 bus 实例（模拟下一个 agent 进程）读回
        bus2 = HandoffBus(data_dir=os.path.join(tmp_dir, "handoffs"),
                          state_root=tmp_dir)
        assert bus2.get(c.id).wake is False
        assert bus2.get(c.id).fuse_reason == c.fuse_reason

    def test_fused_session_rejects_waking_handoff(self, tmp_dir):
        sec = SecretarySession("h2", data_dir=tmp_dir, max_rounds=0)
        sec.review_round(blocking=1)  # 立即熔断
        with pytest.raises(RuntimeError):
            sec.create_handoff("a-unit", "b-unit", "常规接力")
        # 反向对照：静默通道仍可用（熔断后只允许不唤醒的交接）
        assert sec.silent_handoff("a-unit", "b-unit", "静默").wake is False

    def test_unfused_session_still_allows_waking_handoff(self, tmp_dir):
        """反向对照：未熔断会话的常规交接不受影响（wake 默认 True）。"""
        sec = SecretarySession("h3", data_dir=tmp_dir)
        card = sec.create_handoff("a-unit", "b-unit", "常规接力")
        assert card.wake is True
        assert card.fuse_reason == ""

    def test_wake_must_be_bool(self, tmp_dir):
        """``wake`` 严格 bool：``"false"`` 这类真值字符串不得静默绕过静默语义。"""
        bus = HandoffBus(data_dir=os.path.join(tmp_dir, "handoffs"),
                         state_root=tmp_dir)
        with pytest.raises(TypeError):
            bus.create_handoff("a", "b", "i", wake="false")
        assert bus.list_pending() == []

    def test_legacy_card_without_wake_defaults_true(self, tmp_dir):
        """向后兼容：缺 ``wake`` 字段的旧交接卡读回为 True（常规接力）。"""
        ho_dir = os.path.join(tmp_dir, "handoffs")
        os.makedirs(ho_dir, exist_ok=True)
        path = os.path.join(ho_dir, "legacy-card.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump({"id": "legacy-card", "source_unit": "a",
                       "target_unit": "b", "intent": "i", "context": "",
                       "status": "created", "state_card_id": None,
                       "created_at": ""}, f)
        bus = HandoffBus(data_dir=ho_dir, state_root=tmp_dir)
        assert bus.get("legacy-card").wake is True

    def test_unknown_field_ignored_on_load(self, tmp_dir):
        """字段级容错：卡面出现未知字段（未来版本写入）不得导致读取失败。"""
        ho_dir = os.path.join(tmp_dir, "handoffs")
        os.makedirs(ho_dir, exist_ok=True)
        with open(os.path.join(ho_dir, "future-card.json"), "w",
                  encoding="utf-8") as f:
            json.dump({"id": "future-card", "source_unit": "a",
                       "target_unit": "b", "intent": "i",
                       "wake": False, "future_field": {"x": 1}}, f)
        bus = HandoffBus(data_dir=ho_dir, state_root=tmp_dir)
        assert bus.get("future-card").wake is False


# ------------------------------------------------------------------
# ⑤ 跨 agent / 跨实例不复活
# ------------------------------------------------------------------

class TestCrossAgentNoRevive:
    def test_fused_state_survives_new_instance(self, tmp_dir):
        """换实例（模拟换 agent）后仍熔断——预算状态跨实例持久化。"""
        g = LoopGuard("x1", data_dir=tmp_dir, max_rounds=0)
        g.register_round(blocking=1)
        g2 = LoopGuard("x1", data_dir=tmp_dir, max_rounds=0)
        assert g2.is_fused() is True
        assert g2.should_wake_next() is False
        assert g2.register_round(blocking=0, advisory=0).action \
            == LoopAction.FUSE.value

    def test_inherit_silent_handoff_payload(self, tmp_dir):
        sec = SecretarySession("x2", data_dir=tmp_dir, max_rounds=3,
                               max_advisory_fix_rounds=1)
        sec.review_round(blocking=1)
        sec.review_round(blocking=1)
        sec.review_round(blocking=1)
        sec.review_round(blocking=1)  # 熔断
        payload = sec.loop_guard.silent_handoff_payload(open_items=["残留"])

        # 下一个 agent：新数据根、同 session_id
        other = tempfile.mkdtemp()
        try:
            nxt = SecretarySession("x2", data_dir=other)
            assert nxt.should_wake_next() is True  # 起手未熔断
            assert nxt.inherit_silent_handoff(json.dumps(payload)) is True
            assert nxt.should_wake_next() is False
            st = nxt.loop_status()
            assert st["fused"] is True
            assert st["round_no"] == 4        # 轮次计数一并继承
            assert st["blocking_rounds"] == 3
            with pytest.raises(RuntimeError):
                nxt.create_handoff("a-unit", "b-unit", "复活尝试")
        finally:
            shutil.rmtree(other, ignore_errors=True)

    @pytest.mark.parametrize("payload", [
        '{"wake": true, "fused": false}',
        '',
        '{ this is not json',
        '["not", "an", "object"]',
    ])
    def test_inherit_non_silent_payload_does_not_fuse(self, tmp_dir, payload):
        """反向对照：非静默载荷（wake=True / 空 / 坏 JSON / 数组）不熔断。"""
        sec = SecretarySession("x3", data_dir=tmp_dir)
        assert sec.inherit_silent_handoff(payload) is False
        assert sec.should_wake_next() is True


# ------------------------------------------------------------------
# ⑤b 停滞感知（同一整改项指纹反复出现 → 立即熔断，不靠轮次）
# ------------------------------------------------------------------

class TestStallDetection:
    def test_same_signature_twice_fuses_before_budget(self, tmp_dir):
        """同一指纹第 2 次出现即熔断——预算**远未耗尽**（判别性）。"""
        g = LoopGuard("st1", data_dir=tmp_dir, max_rounds=5,
                      max_advisory_fix_rounds=5)
        d1 = g.register_round(blocking=0, advisory=1, signature="🟡|台账口径")
        assert d1.action == LoopAction.FIX_LAST.value
        d2 = g.register_round(blocking=0, advisory=1, signature="🟡|台账口径")
        assert d2.action == LoopAction.FUSE.value
        assert d2.wake is False
        assert "停滞" in d2.reason
        assert d2.budget_left >= 1 and d2.advisory_left >= 1, \
            "预算充足 → 拦住的是停滞感知，而非轮次上界"

    def test_different_signature_does_not_fuse(self, tmp_dir):
        """反向对照：每轮指纹不同（问题在推进）→ 不触发停滞熔断。"""
        g = LoopGuard("st2", data_dir=tmp_dir, max_rounds=5,
                      max_advisory_fix_rounds=5)
        for i in range(4):
            d = g.register_round(blocking=0, advisory=1, signature=f"🟡|项{i}")
            assert d.action == LoopAction.FIX_LAST.value, f"第 {i} 轮不应熔断"
        assert g.is_fused() is False

    def test_signature_optional(self, tmp_dir):
        """不带签名（不启用停滞感知）→ 只按轮次预算判定。"""
        g = LoopGuard("st3", data_dir=tmp_dir, max_rounds=5,
                      max_advisory_fix_rounds=5)
        for _ in range(4):
            assert g.register_round(blocking=0, advisory=1).wake is True
        assert g.is_fused() is False

    def test_clear_rounds_do_not_advance_stall(self, tmp_dir):
        """CLEAR 轮不推进停滞计数（否则"无项放行"会误累积成停滞）。"""
        g = LoopGuard("st4", data_dir=tmp_dir, max_rounds=5,
                      max_advisory_fix_rounds=5)
        g.register_round(blocking=0, advisory=0, signature="🟡|x")
        d = g.register_round(blocking=0, advisory=1, signature="🟡|x")
        assert d.action == LoopAction.FIX_LAST.value  # 首次计入，而非第 2 次
        assert d.stall_count == 1

    def test_stall_threshold_configurable(self, tmp_dir):
        """``max_stall_repeats=3`` → 前两次允许，第三次熔断。"""
        g = LoopGuard("st5", data_dir=tmp_dir, max_stall_repeats=3,
                      max_rounds=9, max_advisory_fix_rounds=9)
        assert g.register_round(blocking=1, signature="s").action == LoopAction.FIX.value
        assert g.register_round(blocking=1, signature="s").action == LoopAction.FIX.value
        assert g.register_round(blocking=1, signature="s").action == LoopAction.FUSE.value

    def test_signature_of_is_order_and_space_insensitive(self, tmp_dir):
        """指纹由清单生成：顺序/空白无关；空清单返回空串（不启用停滞）。"""
        a = LoopGuard.signature_of(["🟡 台账口径", "🟡 文档补注"])
        b = LoopGuard.signature_of(["  🟡   文档补注 ", "🟡 台账口径"])
        assert a == b and a
        assert LoopGuard.signature_of([]) == ""
        assert LoopGuard.signature_of([], ) != a

    def test_facade_signature_helper_detects_reordered_repeat(self, tmp_dir):
        """端到端：两轮报同一批 🟡（换顺序）→ 第 2 轮即判停滞熔断。"""
        sec = SecretarySession("st8", data_dir=tmp_dir, max_rounds=9,
                               max_advisory_fix_rounds=9)
        items = ["🟡 台账口径", "🟡 文档补注"]
        d1 = sec.review_round(advisory=len(items),
                              signature=sec.review_signature(items))
        assert d1.action == LoopAction.FIX_LAST.value
        d2 = sec.review_round(advisory=len(items),
                              signature=sec.review_signature(list(reversed(items))))
        assert d2.action == LoopAction.FUSE.value
        assert "停滞" in d2.reason

    def test_stall_threshold_rejects_zero(self, tmp_dir):
        with pytest.raises(ValueError):
            LoopGuard("st6", data_dir=tmp_dir, max_stall_repeats=0)

    def test_resume_clears_stall(self, tmp_dir):
        """人工解锁同时清停滞指纹（否则解锁后下一轮立刻再熔断）。"""
        g = LoopGuard("st7", data_dir=tmp_dir, max_stall_repeats=2,
                      max_rounds=5, max_advisory_fix_rounds=5)
        g.register_round(blocking=0, advisory=1, signature="s")
        assert g.register_round(blocking=0, advisory=1, signature="s").action \
            == LoopAction.FUSE.value
        assert g.resume(by="张浩东", reason="确认") is True
        assert g.register_round(blocking=0, advisory=1, signature="s").action \
            == LoopAction.FIX_LAST.value


# ------------------------------------------------------------------
# ⑥ 人工解锁（唯一恢复途径）
# ------------------------------------------------------------------

class TestResumeRequiresHuman:
    def test_resume_requires_identity(self, tmp_dir):
        g = LoopGuard("r1", data_dir=tmp_dir, max_rounds=0)
        g.register_round(blocking=1)
        for bad in ("", "   ", None):
            with pytest.raises(ValueError):
                g.resume(by=bad)
        assert g.is_fused() is True, "匿名/空 by 不得解锁熔断"

    def test_resume_restores_budget(self, tmp_dir):
        g = LoopGuard("r2", data_dir=tmp_dir, max_rounds=1)
        assert g.register_round(blocking=1).action == LoopAction.FIX.value
        assert g.register_round(blocking=1).action == LoopAction.FUSE.value
        assert g.resume(by="张浩东", reason="人工确认收口") is True
        assert g.should_wake_next() is True
        # 反向佐证：解锁后确实恢复了一轮修复预算
        assert g.register_round(blocking=1).action == LoopAction.FIX.value
        assert g.status()["resume_count"] == 1
        assert any(h["action"] == "resume" and "张浩东" in h["note"]
                   for h in g.history())

    def test_resume_returns_false_when_not_fused(self, tmp_dir):
        g = LoopGuard("r3", data_dir=tmp_dir)
        assert g.resume(by="张浩东") is False

    def test_fuse_explicit_and_idempotent(self, tmp_dir):
        g = LoopGuard("r4", data_dir=tmp_dir)
        assert g.fuse("上游叫停") is True
        assert g.fuse("再叫停") is False
        assert g.should_wake_next() is False
        assert g.status()["fused_reason"] == "上游叫停"


# ------------------------------------------------------------------
# ⑦ 状态持久化健壮性与生命周期
# ------------------------------------------------------------------

class TestPersistenceAndLifecycle:
    def test_state_file_location_and_reload(self, tmp_dir):
        g = LoopGuard("p1", data_dir=tmp_dir)
        g.register_round(blocking=1, advisory=2, note="第1轮")
        assert g.state_path() == os.path.join(tmp_dir, "p1.json")
        g.reload()
        assert g.status()["round_no"] == 1
        assert g.status()["history"][0]["note"] == "第1轮"

    @pytest.mark.parametrize("raw", ["[]", '"str"', "{ bad json", "123"])
    def test_corrupted_state_degrades_to_fresh(self, tmp_dir, raw):
        """损坏/非对象状态文件 → 视为全新状态（不抛异常，宽进不炸）。"""
        with open(os.path.join(tmp_dir, "p2.json"), "w", encoding="utf-8") as f:
            f.write(raw)
        g = LoopGuard("p2", data_dir=tmp_dir)
        assert g.is_fused() is False
        assert g.register_round(blocking=1).action == LoopAction.FIX.value

    def test_malformed_field_types_degrade(self, tmp_dir):
        """字段类型不符（轮次为字符串/布尔、history 非数组）不得炸。"""
        with open(os.path.join(tmp_dir, "p3.json"), "w", encoding="utf-8") as f:
            json.dump({"session_id": "p3", "round_no": "9",
                       "blocking_rounds": True, "repair_rounds": None,
                       "advisory_used": 2, "history": {"not": "list"}}, f)
        g = LoopGuard("p3", data_dir=tmp_dir)
        st = g.status()
        assert st["round_no"] == 0 and st["blocking_rounds"] == 0
        assert st["advisory_used"] == 2
        assert g._state.history == []

    @pytest.mark.parametrize("bad", ["../escape", "a/b", "", "a b", "会话"])
    def test_invalid_session_id_rejected(self, tmp_dir, bad):
        """session_id 白名单：不参与路径穿越/空格/非 ASCII。"""
        with pytest.raises(ValueError):
            LoopGuard(bad, data_dir=tmp_dir)

    @pytest.mark.parametrize("kwargs", [
        {"max_rounds": -1}, {"max_rounds": True},
        {"max_advisory_fix_rounds": -1},
        {"max_total_rounds": -2},
    ])
    def test_invalid_budget_rejected(self, tmp_dir, kwargs):
        with pytest.raises(ValueError):
            LoopGuard("p4", data_dir=tmp_dir, **kwargs)

    @pytest.mark.parametrize("kwargs", [
        {"blocking": -1}, {"advisory": -1}, {"blocking": True},
        {"advisory": 1.5},
    ])
    def test_invalid_counts_rejected(self, tmp_dir, kwargs):
        g = LoopGuard("p5", data_dir=tmp_dir)
        base = {"blocking": 0, "advisory": 0}
        base.update(kwargs)
        with pytest.raises(ValueError):
            g.preview(**base)

    def test_delete_session_cleans_loop_state(self, tmp_dir):
        sec = SecretarySession("p6", data_dir=tmp_dir, max_rounds=0)
        sec.review_round(blocking=1)
        assert sec.should_wake_next() is False
        path = sec.loop_guard.state_path()
        assert os.path.exists(path)
        sec.delete_session()
        assert not os.path.exists(path), "会话回收须一并清理循环熔断状态"
        # 反向对照：会话重建后不带陈旧熔断
        sec2 = SecretarySession("p6", data_dir=tmp_dir, max_rounds=0)
        assert sec2.should_wake_next() is True

    def test_loop_dir_default_under_session_root(self, tmp_dir):
        """默认状态目录落 ``<数据根>/loop``（与会话数据同根，无 cwd 孤儿）。"""
        sec = SecretarySession("p7", data_dir=tmp_dir)
        assert os.path.dirname(sec.loop_guard.state_path()) \
            == os.path.join(tmp_dir, "loop")


# ------------------------------------------------------------------
# ⑧ 会话门面集成（预算随汇总上行 / 显式熔断）
# ------------------------------------------------------------------

class TestSessionFacadeIntegration:
    def test_summary_carries_loop_budget(self, tmp_dir):
        sec = SecretarySession("f1", data_dir=tmp_dir, max_rounds=1)
        summary = sec.summarize_for_upstream()
        assert summary["wake_allowed"] is True
        assert summary["loop"]["max_rounds"] == 1
        sec.review_round(blocking=1)
        sec.review_round(blocking=1)  # 熔断
        summary2 = sec.summarize_for_upstream()
        assert summary2["wake_allowed"] is False
        assert summary2["loop"]["fused"] is True

    def test_preview_does_not_consume_session_budget(self, tmp_dir):
        sec = SecretarySession("f2", data_dir=tmp_dir, max_rounds=1)
        for _ in range(3):
            assert sec.preview_round(blocking=1).action == LoopAction.FIX.value
        assert sec.loop_status()["blocking_rounds"] == 0
        assert sec.review_round(blocking=1).action == LoopAction.FIX.value

    def test_facade_fuse_and_resume(self, tmp_dir):
        sec = SecretarySession("f3", data_dir=tmp_dir)
        assert sec.fuse_loop("人工叫停") is True
        assert sec.should_wake_next() is False
        assert sec.resume_loop(by="张浩东", reason="确认收口") is True
        assert sec.should_wake_next() is True
