"""Issue #14 PR #15 复评第 5 轮（`a7847f9` 打回）定点修复回归 —— 🔴1 / 🔴2 / 🔴5

触发：架构·评审 复评第 4 轮判 `fix_bug`（**🔴 未清零**）。🔴3/🔴4 已认可清零，
但 🔴1 / 🔴2 有**可达残留**，并新增 1 项**性能回归**（🔴5）。本文件逐项补
**判别性回归 + 反向对照**（每组都配"若退回旧实现即失败"的断言）。

| 项 | 缺陷（评审实测） | 本文件判据 |
|---|---|---|
| 🔴1a | `_coerce_budget_payload` ① **提前 return**：正文里只要有一条带 `rounds` 的留痕，就 `return merged`，②③④ 全跳过 → 「留痕段 + 不带内嵌留痕的卡」形态里**卡的轮次读不到**（实测 `trail(3)+card(6)` → `round_no=3`，比"只给卡"的 6 还差） | `inherit_budget(trail+card)` → `6 / True / False`；`from_handoff` 同结论；**两序一致**；反向：只给留痕 → 3 |
| 🔴1b | `_coerce_checks_used` dict 分支**首个键命中即 return** → `trail(2)+{"checks_used":5}` 读成 2；`{"checks_used":2,"budget_trail":trail(5)}` 读成 2（新增的 `budget_trail` 展开是**死代码**，`ProbeEvent.to_dict()` 必带 `checks_used`） | 3 组 → 全部 5；`probe(external=trail2+载荷5)` → `checks_used=6`；反向：只给留痕 → 2 |
| 🔴2 | `pick_wake_object` 取**第一个**含 `wake` 的对象 → 卡+卡 顺序决定结论（`[True,False]` 放行 / `[False,True]` 熔断，**fail-open 且顺序相关**） | 卡+卡两序**同结论**（都熔断）；坏值 + `True` 两序也同结论；反向：单张 `wake=True` 仍不熔断 |
| 🔴5 | `_balanced_object_end` 从**每个** `{` 重扫到文末，未闭合时 O(n²)（`"{" * 40000` → 63 s，基线 3 ms） | `"{"×20000 / ×40000` 耗时 < 1 s（基线量级）；`"if (x) {"×3000` 同量级；**语义不变**：未闭合前缀**不影响**其后的卡（fail-open 反向对照） |
| 🟡 | `DEEP` 用例无判别力（非法 JSON，基线同样"判不出"）；`MAX_COERCE_DEPTH` 两处各写一份；`reason`/`budget` 合并是"首个非空胜"（顺序相关）；await 落点只带 `key`、**没把 key 接到 pipeline**（await 片读 `$CHECKPOINT_AWAIT_KEY`）；`extract_json_object` 无调用方 | 见各 `Test*` 类 |

🟢：`extract_json_object` 保留原因已写明（对外稳定 API，与"取全部"**同源实现**）。
"""
from __future__ import annotations

import json
import os
import tempfile
import time

import pytest

from secretary.collab import (
    WAIT,
    CheckpointProbe,
    MAX_COERCE_DEPTH as COLLAB_DEPTH,
)
from secretary.collab.probe import Checkpoint
from secretary.loop import (
    LoopGuard,
    MAX_COERCE_DEPTH,
    extract_json_object,
    extract_json_objects,
    pick_wake_object,
)
from secretary.session import SecretarySession
from secretary import trail as trail_mod

SESSION = "i14r6"


class FakeClock:
    def __init__(self, t: float = 1_000_000.0):
        self.t = t

    def __call__(self) -> float:
        return self.t


@pytest.fixture
def tmp_dir():
    d = tempfile.mkdtemp()
    yield d


def _guard(sid, total, fused, tmp=None):
    """造一个带指定轮次/熔断态的守卫（直接摆状态，不消耗预算）。"""
    g = LoopGuard(sid, data_dir=tmp or tempfile.mkdtemp())
    g._state.round_no = total
    g._state.fused = fused
    if fused:
        g._state.fused_reason = "x"
    return g


def _probe_trail(key: str, n: int) -> str:
    return CheckpointProbe.budget_trail_of(Checkpoint(key=key, checks_used=n))


# ==================================================================
# 🔴1a 留痕 ↔ 载荷**必须合并**（不得"有 rounds 就提前 return"）
# ==================================================================

class TestBudgetTrailMergesWithPayload:
    def test_trail_plus_card_takes_max(self, tmp_dir):
        """评审口径：`inherit_budget(trail(3) + card(6,fused))` → `6 / True / False`。"""
        trail = _guard(SESSION, 3, False).budget_trail()
        card = {"session_id": SESSION, "wake": False, "reason": "r",
                "rounds": {"total": 6}, "fused": True}
        sec = SecretarySession(SESSION, data_dir=tmp_dir)
        sec.inherit_budget(trail + "\n" + json.dumps(card))
        assert sec.loop_status()["round_no"] == 6
        assert sec.loop_status()["fused"] is True
        assert sec.should_wake_next() is False

    def test_from_handoff_same_conclusion(self, tmp_dir):
        trail = _guard(SESSION, 3, False).budget_trail()
        card = {"session_id": SESSION, "wake": False, "reason": "r",
                "rounds": {"total": 6}, "fused": True}
        g = LoopGuard.from_handoff(
            SESSION, data_dir=tmp_dir,
            handoff_context=trail + "\n" + json.dumps(card))
        assert g.status()["round_no"] == 6
        assert g.is_fused() is True
        assert g.should_wake_next() is False

    def test_both_orders_agree(self, tmp_dir):
        """**顺序无关**：卡在前 / 留痕在前 → 同结论（旧实现只认"有 rounds 就返回"）。"""
        trail = _guard(SESSION, 3, False).budget_trail()
        card = json.dumps({"session_id": SESSION, "wake": False,
                           "rounds": {"total": 6}, "fused": True})
        for body in (trail + "\n" + card, card + "\n" + trail):
            sec = SecretarySession(SESSION, data_dir=tempfile.mkdtemp())
            sec.inherit_budget(body)
            assert sec.loop_status()["round_no"] == 6, body[:40]
            assert sec.loop_status()["fused"] is True
            assert sec.should_wake_next() is False

    def test_trail_only_is_the_reverse_control(self, tmp_dir):
        """**反向对照**：只给留痕 → 就是留痕自己的 3（不是被"合并"抬成 6）。"""
        trail = _guard(SESSION, 3, False).budget_trail()
        sec = SecretarySession(SESSION, data_dir=tmp_dir)
        sec.inherit_budget(trail)
        assert sec.loop_status()["round_no"] == 3
        assert sec.loop_status()["fused"] is False
        assert sec.should_wake_next() is True

    def test_card_only_is_not_regressed(self, tmp_dir):
        """**反向对照**：只给卡 → 6 / 熔断（加留痕不得让它**变差**）。"""
        card = {"session_id": SESSION, "wake": False, "rounds": {"total": 6},
                "fused": True}
        sec = SecretarySession(SESSION, data_dir=tmp_dir)
        sec.inherit_budget(json.dumps(card))
        assert sec.loop_status()["round_no"] == 6
        assert sec.should_wake_next() is False


# ==================================================================
# 🔴1b `_coerce_checks_used`：**所有通道取 max**（不再"首个键命中即 return"）
# ==================================================================

class TestChecksUsedMergesAllChannels:
    def test_trail_then_payload(self):
        """评审口径：`trail(2) + {"checks_used": 5}` → **5**（旧实现 → 2）。"""
        body = _probe_trail("k1", 2) + ' {"checks_used": 5}'
        assert CheckpointProbe._coerce_checks_used(body) == 5

    def test_payload_then_trail(self):
        body = '{"checks_used": 5} ' + _probe_trail("k1", 2)
        assert CheckpointProbe._coerce_checks_used(body) == 5

    def test_payload_own_key_plus_embedded_trail(self):
        """`{"checks_used": 2, "budget_trail": trail(5)}` → **5**。

        旧实现：dict 分支命中 `checks_used` 即 `return` → 新增的
        `budget_trail` 展开在**真实路径上是死代码**（`ProbeEvent.to_dict()`
        必带 `checks_used`）。
        """
        got = CheckpointProbe._coerce_checks_used(
            {"checks_used": 2, "budget_trail": _probe_trail("k1", 5)})
        assert got == 5

    def test_event_dict_with_embedded_trail(self):
        """`ProbeEvent.to_dict()` 形态（必带 `checks_used`）同样要看到内嵌留痕。"""
        got = CheckpointProbe._coerce_checks_used(
            {"checkpoint_key": "k1", "checks_used": 1, "verdict": WAIT,
             "budget_trail": _probe_trail("k1", 4)})
        assert got == 4

    def test_probe_end_to_end(self, tmp_dir):
        """评审口径：`probe(external_checks=trail(2)+载荷(5))` → 本轮后 **6**。"""
        p = CheckpointProbe(SESSION, data_dir=tmp_dir, clock=FakeClock())
        p.create("k1")
        body = _probe_trail("k1", 2) + ' {"checks_used": 5}'
        out = p.probe("k1", external_checks=body, confirmed=False)
        assert out.checkpoint.checks_used == 6

    def test_trail_only_is_the_reverse_control(self):
        """**反向对照**：只给留痕 → 2（合并不得凭空抬高）。"""
        assert CheckpointProbe._coerce_checks_used(_probe_trail("k1", 2)) == 2


# ==================================================================
# 🔴2 `pick_wake_object`：**保守 + 确定性**（任一非严格 True → 静默）
# ==================================================================

class TestPickWakeObjectIsConservative:
    def test_two_cards_same_conclusion_both_orders(self, tmp_dir):
        """卡+卡两序 → **同结论**（旧实现顺序决定结论，且一个方向是 fail-open）。"""
        old = json.dumps({"session_id": SESSION, "wake": True})
        new = json.dumps({"session_id": SESSION, "wake": False,
                          "reason": "silent"})
        for body in (old + "\n" + new, new + "\n" + old):
            g = LoopGuard.from_handoff(SESSION, data_dir=tempfile.mkdtemp(),
                                       handoff_context=body)
            assert g.is_fused() is True, body[:30]
            assert g.should_wake_next() is False, body[:30]

    def test_bad_value_card_is_silent_regardless_of_order(self, tmp_dir):
        """坏值（`"false"`）与 `True` 同现 → 两序都**保守静默**（判不出不放行）。"""
        good = json.dumps({"session_id": SESSION, "wake": True})
        bad = json.dumps({"session_id": SESSION, "wake": "false"})
        for body in (good + "\n" + bad, bad + "\n" + good):
            g = LoopGuard.from_handoff(SESSION, data_dir=tempfile.mkdtemp(),
                                       handoff_context=body)
            assert g.is_fused() is True
            assert g.should_wake_next() is False

    def test_single_true_card_is_not_fused(self, tmp_dir):
        """**反向对照**：单张 `wake=True`（常规接力）**不得**被误熔断。"""
        g = LoopGuard.from_handoff(
            SESSION, data_dir=tempfile.mkdtemp(),
            handoff_context=json.dumps({"session_id": SESSION, "wake": True}))
        assert g.is_fused() is False
        assert g.should_wake_next() is True

    def test_all_true_cards_not_fused(self):
        """**反向对照**：多张卡都是严格 `True` → 仍不熔断（不是"有卡就静默"）。"""
        objs = [{"wake": True}, {"wake": True}]
        assert pick_wake_object(objs) == {"wake": True}

    def test_trailless_objects_are_none(self):
        """**反向对照**：不带 `wake` 的对象 → `None`（普通 context 不误熔断）。"""
        assert pick_wake_object([{"rounds": {"total": 1}}]) is None
        assert pick_wake_object([]) is None
        assert pick_wake_object("not a list") is None

    def test_silent_takes_the_last_card(self):
        """"最新卡"判据显式化：多张静默卡 → 取**最后**一张（追加写 → 后者更晚）。"""
        first = {"wake": False, "reason": "旧"}
        last = {"wake": False, "reason": "新"}
        assert pick_wake_object([first, last])["reason"] == "新"
        assert pick_wake_object([last, first])["reason"] == "旧"


# ==================================================================
# 🔴5 扫描复杂度：单次线性（未闭合 `{` 不再 O(n²)）
# ==================================================================

class TestScanIsLinear:
    """性能判据用**宽松上界**（1 s）而非绝对耗时——只为抓"平方级回归"。"""

    def _t(self, text):
        t0 = time.perf_counter()
        got = extract_json_objects(text)
        return time.perf_counter() - t0, got

    def test_unclosed_braces_are_not_quadratic(self):
        """`"{"×20000` / `×40000`：基线 63 s 量级 → 现在必须毫秒级（< 1 s）。"""
        for n in (20000, 40000):
            dt, got = self._t("{" * n)
            assert got == []
            assert dt < 1.0, f'"{{" x {n} 用了 {dt:.2f}s（平方回归）'

    def test_js_fragment_not_quadratic(self):
        """差量片段 `"if (x) {"×3000`（27 KB）同样是主路径输入。"""
        dt, got = self._t("if (x) {" * 3000)
        assert got == []
        assert dt < 1.0, f"if(x){{ x3000 用了 {dt:.2f}s"

    def test_quote_heavy_input_not_quadratic(self):
        """引号密集（未闭合引号 → 修复扫描路径）同样不得平方。"""
        for text in ('"' * 20000, '"{' * 50000, ('"' + "{" * 10) * 20000):
            dt, _ = self._t(text)
            assert dt < 1.0, f"len={len(text)} 用了 {dt:.2f}s"

    def test_scan_budget_is_an_upper_bound(self):
        """**边界明示**：超过扫描上界 → 保守不表态（不是无限扫下去）。"""
        from secretary.trail import DEFAULT_SCAN_BUDGET
        big = "x" * (DEFAULT_SCAN_BUDGET + 10) + '{"wake": false}'
        dt, got = self._t(big)
        assert got == []
        assert dt < 1.0

    def test_scan_scales_linearly(self):
        """规模 ×10 → 耗时不得到平方级（允许常数波动，判据取 25 倍宽松上界）。"""
        t1, _ = self._t("{" * 10000)
        t2, _ = self._t("{" * 100000)
        assert t2 < max(t1 * 25, 1.0), (t1, t2)

    def test_unclosed_prefix_does_not_hide_later_card(self):
        """**保守方向反向对照**：未闭合前缀**不得**把其后的交接卡整段丢掉。

        若为"提速"把未配平段之后的正文全放弃 → `wake=False` 读不到 →
        `should_wake_next()=True`（fail-open），比慢更危险。
        """
        body = "{" * 5000 + json.dumps({"session_id": SESSION, "wake": False})
        objs = extract_json_objects(body)
        assert {"session_id": SESSION, "wake": False} in objs
        g = LoopGuard.from_handoff(SESSION, data_dir=tempfile.mkdtemp(),
                                   handoff_context=body)
        assert g.is_fused() is True

    def test_normal_content_still_parsed(self):
        """**反向对照**：正常正文（对象 + 留痕 + 括号字符串）逐字不变。"""
        body = ('{"a": 1}\n<!-- loop-budget: {"rounds": {"total": 2}} -->\n'
                '{"wake": false}\nif (x) { y }')
        objs = extract_json_objects(body)
        assert objs == [{"a": 1}, {"rounds": {"total": 2}}, {"wake": False}]
        assert extract_json_object('{"a": {"b": "{not a brace}"}} {"c": 2}') \
            == {"a": {"b": "{not a brace}"}}


# ==================================================================
# 🟡 收口（本轮不阻塞，但同轮收掉）
# ==================================================================

class TestDepthBoundSingleSource:
    def test_one_semantic_source(self):
        """深度上界**只有一个定义处**（`secretary.trail.MAX_TRAIL_DEPTH`）。"""
        assert MAX_COERCE_DEPTH is trail_mod.MAX_TRAIL_DEPTH
        assert COLLAB_DEPTH is trail_mod.MAX_TRAIL_DEPTH

    def test_collab_package_exports_it_too(self):
        """`collab` 包此前**没导出**（`loop` 导出了）→ 现在两边都可取到同一个。"""
        from secretary import collab, loop
        assert collab.MAX_COERCE_DEPTH is loop.MAX_COERCE_DEPTH


class TestReasonAndBudgetMergeAreOrderIndependent:
    """`_merge_budget_dicts` 的 `reason` / `budget` 不再是"首个非空胜"。"""

    A = {"fused": True, "reason": "原因A", "rounds": {"total": 1},
         "budget": {"max_rounds": 3}}
    B = {"fused": True, "reason": "原因B（更长一些）", "rounds": {"total": 2},
         "budget": {"max_rounds": 9}}

    def test_reason_is_order_independent(self):
        m1 = LoopGuard._merge_budget_dicts([self.A, self.B])
        m2 = LoopGuard._merge_budget_dicts([self.B, self.A])
        assert m1["reason"] == m2["reason"] == "原因B（更长一些）"

    def test_budget_fields_take_max_not_first(self):
        """`budget.max_rounds`：`[3, 9]` → **9**（旧实现取首个 → 3，**上界被盖小**）。"""
        for order in ([self.A, self.B], [self.B, self.A]):
            m = LoopGuard._merge_budget_dicts(order)
            assert m["budget"]["max_rounds"] == 9

    def test_rounds_still_max(self):
        for order in ([self.A, self.B], [self.B, self.A]):
            assert LoopGuard._merge_budget_dicts(order)["rounds"]["total"] == 2


class TestAwaitKeyIsWiredToPipeline:
    """🟡2：await 落点必须把**轮次 key 注入** await 片读的变量名。"""

    def test_await_trigger_carries_env_key_mapping(self, tmp_dir):
        p = CheckpointProbe(SESSION, data_dir=tmp_dir, clock=FakeClock())
        p.create("c2", kind="event", waiter_event="push", resolver_event="push")
        dis = p.dispatch(p.probe("c2", event_fired=False).event)
        assert dis["action"] == "platform_await"
        trg = dis["trigger"]
        assert trg["type"] == "cnb:await"
        assert trg["event"] == "api_trigger_probe_wait"
        # 轮次 key 与 `.cnb.yml` 的 `cnb:await.options.key` 变量名**同名同源**
        assert trg["key_env"] == "CHECKPOINT_AWAIT_KEY"
        assert trg["env"]["CHECKPOINT_AWAIT_KEY"] == trg["key"]
        assert trg["env"]["CHECKPOINT_KEY"] == "c2"
        assert trg["key"].startswith("probe:c2:r")

    def test_cnb_yml_reads_that_variable(self):
        """`.cnb.yml` 的 await 片确实读 `$CHECKPOINT_AWAIT_KEY`（同名同源）。"""
        root = os.path.dirname(os.path.dirname(os.path.dirname(
            os.path.abspath(__file__))))
        with open(os.path.join(root, ".cnb.yml"), encoding="utf-8") as f:
            cfg = f.read()
        assert "key: $CHECKPOINT_AWAIT_KEY" in cfg

    def test_crontab_branch_has_no_await_env(self):
        """**反向对照**：crontab 落点不带 await 片的环境注入（落点互斥不混用）。"""
        from secretary.collab import dispatch_shared
        dis = dispatch_shared({"checkpoint_key": "cp", "verdict": WAIT,
                               "next_delay_sec": 600.0})
        assert dis["action"] == "platform_crontab"
        assert "CHECKPOINT_AWAIT_KEY" not in json.dumps(dis["trigger"])


class TestExtractJsonObjectRetention:
    def test_kept_and_same_implementation(self):
        """🟢 保留原因：对外稳定 API，且与"取全部"**同源实现**（不是第二份扫描）。"""
        body = '{"a": 1}\n{"b": 2}'
        assert extract_json_object(body) == extract_json_objects(body)[0]
        assert extract_json_object("no json") is None
        assert extract_json_object(None) is None
