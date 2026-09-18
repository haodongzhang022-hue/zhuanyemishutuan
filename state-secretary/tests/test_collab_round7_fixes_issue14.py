"""Issue #14 PR #15 复评第 5 轮回转卡（🔴-1…🔴-4）定点修复回归

触发：架构·评审 第 5 轮复评（对象 `a7847f9..d22f6d7`）→ `fix_bug`，
🔴 未清零、达打回上界（标 blocked）。本文件针对回转卡的 4 项 🔴 补**判别性回归**，
每组关键断言配**反向对照**（防恒真）。

| 项 | 缺陷（评审实测） | 本文件判据 |
|---|---|---|
| 🔴-1 | `_lock_key_known(known, scope)` 拿 **scope** 去比 **keys**（keys 只含锁键）→ 生产形态（`S:review` / `scope="评审读锁（…）"`）"已释放"**恒判不出** → 等他的锁**永不放行** | 已释放 → `proceed`（基线 `a7847f9` 同场景亦 `proceed`）；**未登记 scope 仍 `unknown`**（不得放宽成放行）；`overview()` 给出 `scopes` |
| 🔴-2 | `_coerce_checks_used` 的 **dict 分支无归属过滤**（字符串留痕分支有）→ 别人的 `checkpoint_key` 载荷被吸收 → 本检查点直接 `resume_required` | dict 载荷按 `checkpoint_key` 过滤；**不带归属的载荷仍保留**（不表态 ≠ 属于别人）；字符串分支口径不变 |
| 🔴-3 | `CNB_SESSION_ID` / `CNB_RUN_SCOPE_TRAIL` **全库无生产者**；`trails(session_id="")` 把评论里**所有会话**归属重写成 `""` → 别的会话台账被继承（漏跑）/ 红后复跑被堵死 | `trails()` **保留原归属**；CI 侧无 sid **不写回**；`.cnb.yml` `main.push` 真有载体生产者（fetch/writeback）；会话号可推导 |
| 🔴-4 | `_ZERO_ISSUE_RE` 只匹配**数字 0 开头** → `"no errors"` / `"errors: 0"` / `"1 xfailed"` / `"no failures"` 仍判红 → 例外**恒真激活** | 上述变体 → 绿；**真失败仍红**（反向对照） |

🟡 结转（本轮不回转，仅登记）：🟡4 显式规则包路径不同源；🟢-1 死形参 `max_depth`
（本轮**已回退删除**）；🔴F 红后复跑**上界**（本轮**已落上界** `max_rerun_on_failure`）。
"""
from __future__ import annotations

import json
import os
import shutil
import tempfile

import pytest

from secretary.collab import (
    CHECK_LOCK,
    PROCEED,
    SKIP_DUPLICATE,
    UNKNOWN,
    CheckpointProbe,
    RunScopeGuard,
    load_rules,
)
from secretary.collab import carrier
from secretary.collab.run_guard import (
    RUN_SCOPE_TRAIL_MARKER,
    _is_red,
)
from secretary.session import SecretarySession

SESSION = "i14r7"
REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))))
RULES = "shared/collab/collab-rules.yaml"


@pytest.fixture
def rules():
    return load_rules(search_from=os.path.join(REPO_ROOT, "state-secretary"))


@pytest.fixture
def env():
    d = tempfile.mkdtemp()
    sec = SecretarySession(SESSION, data_dir=d, rules_path=RULES)
    try:
        yield d, sec
    finally:
        shutil.rmtree(d, ignore_errors=True)


def _read(rel: str) -> str:
    with open(os.path.join(REPO_ROOT, rel), encoding="utf-8") as f:
        return f.read()


# ==================================================================
# 🔴-1 锁判据与"命中"**同口径**（scope 是自然语言、key 才是锁键）
# ==================================================================

class TestLockScopeSymmetry:
    def test_released_review_lock_proceeds(self, env):
        """**生产形态**：`ReviewArbiter` 的锁键是 `S:review`、`scope` 是自然语言。

        旧实现拿 `scope` 去比 `keys`（只含锁键）→ "已释放"**恒判不出** →
        `unknown` → **永不放行**（原 🔴E 的同族，从"误放行"翻成"永不放行"）。
        """
        _, sec = env
        scope = "评审读锁（评审已有基线，勿重复开启）"
        sec.lock_registry.register("S:review", kind="review", holder="rev",
                                   scope=scope)
        sec.create_checkpoint("cpS", kind=CHECK_LOCK, scope=scope)
        assert sec.probe_checkpoint("cpS").verdict == "wait"
        sec.lock_registry.release("S:review", by="rev")
        out = sec.probe_checkpoint("cpS")
        assert out.verdict == PROCEED, out.event.reason

    def test_ownership_scope_also_proceeds(self, env):
        """同族第二处：`OwnershipManager` 的 `scope="状态卡写入"`（键是 `<sess>#card`）。"""
        _, sec = env
        sec.lock_registry.register(f"{SESSION}#card", kind="card_write",
                                   holder="dev", scope="状态卡写入")
        sec.create_checkpoint("cpO", kind=CHECK_LOCK, scope="状态卡写入")
        assert sec.probe_checkpoint("cpO").verdict == "wait"
        sec.lock_registry.release(f"{SESSION}#card", by="dev")
        assert sec.probe_checkpoint("cpO").verdict == PROCEED

    def test_unregistered_scope_still_unknown(self, env):
        """**反向对照**：未登记的 scope **仍须** `unknown`（不得放宽成放行）。

        判据放宽的方向只应是"能判出已释放"，绝不能变成"判不出就放行"。
        """
        _, sec = env
        sec.lock_registry.register("S:review", kind="review", holder="rev",
                                   scope="评审读锁（评审已有基线，勿重复开启）")
        sec.create_checkpoint("cpU", kind=CHECK_LOCK, scope="从未登记过的锁")
        assert sec.probe_checkpoint("cpU").verdict == UNKNOWN
        # 反向：把同一把锁登记过 → 立刻能判（证明上一条不是因为"判据恒 unknown"）
        sec.create_checkpoint("cpK", kind=CHECK_LOCK, scope="评审读锁（评审已有基线，勿重复开启）")
        sec.lock_registry.release("S:review", by="rev")
        assert sec.probe_checkpoint("cpK").verdict == PROCEED

    def test_overview_exposes_scopes(self, env):
        """台账必须给出**全量 scope 名**（与 `keys` 同源）——同口径判定的数据前提。"""
        _, sec = env
        sec.lock_registry.register("S:review", kind="review", holder="rev",
                                   scope="评审读锁（评审已有基线，勿重复开启）")
        view = sec.lock_registry.overview()
        assert "S:review" in view["keys"]
        assert "评审读锁（评审已有基线，勿重复开启）" in view["scopes"]
        sec.lock_registry.release("S:review", by="rev")
        view2 = sec.lock_registry.overview()
        assert view2["scopes"] == ["评审读锁（评审已有基线，勿重复开启）"], \
            "已释放的 scope 仍在全量清单里（才能判「查到未持有」）"

    def test_handcrafted_mapping_view_also_accepted(self):
        """手搓视图给 `{scope: key}` 映射形态时同样认（同口径的第三种数据源）。"""
        p = CheckpointProbe(SESSION, data_dir=tempfile.mkdtemp(),
                            lock_view=lambda: {"held": [],
                                               "keys": {"S:review": "评审读锁（x）"}})
        p.create("cpM", kind=CHECK_LOCK, scope="评审读锁（x）")
        assert p.probe("cpM").verdict == PROCEED


# ==================================================================
# 🔴-2 dict 载荷**按归属过滤**（与字符串分支同口径）
# ==================================================================

class TestDictPayloadAttribution:
    def test_other_checkpoint_dict_payload_is_ignored(self):
        """别人的检查点 dict 载荷（`ProbeEvent.to_dict()` 形态）不得被吸收。"""
        got = CheckpointProbe._coerce_checks_used(
            {"checkpoint_key": "cpA", "checks_used": 6, "kind": "lock"},
            0, cp_key="cpB")
        assert got is None, "别的检查点的 dict 载荷不得被本检查点吸收（误判用尽）"

    def test_own_checkpoint_dict_payload_is_taken(self):
        """**正向对照**：自己的 dict 载荷照常读取（判据不得被修成恒 None）。"""
        got = CheckpointProbe._coerce_checks_used(
            {"checkpoint_key": "cpB", "checks_used": 2}, 0, cp_key="cpB")
        assert got == 2

    def test_dict_without_attribution_is_kept(self):
        """**不表态 ≠ 属于别人**：不带 `checkpoint_key` 的载荷仍保留。"""
        assert CheckpointProbe._coerce_checks_used(
            {"checks_used": 6}, 0, cp_key="cpB") == 6
        assert CheckpointProbe._coerce_checks_used(
            {"checks_used": 6}, 0) == 6

    def test_dict_and_string_branches_agree(self):
        """**同族口径对称**：同一条载荷的 dict 形态与留痕形态给**同一结论**。"""
        payload = {"checkpoint_key": "cpA", "checks_used": 6}
        as_trail = "<!-- probe-budget: %s -->" % json.dumps(payload)
        assert (CheckpointProbe._coerce_checks_used(payload, 0, cp_key="cpB")
                == CheckpointProbe._coerce_checks_used(as_trail, 0, cp_key="cpB")
                is None)

    def test_other_checkpoint_event_payload_does_not_exhaust(self):
        """**判别性核心**（端到端）：别人的 `ProbeEvent.to_dict()`（必带
        `checkpoint_key`）不得让本检查点直接 `resume_required`。"""
        p = CheckpointProbe(SESSION, data_dir=tempfile.mkdtemp())
        p.create("cpB", kind="event")
        other_event = {
            "checkpoint_key": "cpA", "kind": "lock", "checks_used": 6,
            "verdict": "wait", "action": "keep_waiting",
        }
        out = p.probe("cpB", event_fired=False, external_checks=other_event)
        assert out.event.resume_required is False, "B 不得因 A 的轮次被判用尽"
        assert out.event.checks_used == 1

    def test_own_event_payload_does_merge(self):
        """**正向对照**：本检查点自己的事件载荷照常合并（判据不得恒 None）。"""
        p = CheckpointProbe(SESSION, data_dir=tempfile.mkdtemp())
        p.create("cpB", kind="event")
        own = {"checkpoint_key": "cpB", "kind": "event", "checks_used": 5,
               "verdict": "wait"}
        out = p.probe("cpB", event_fired=False, external_checks=own)
        assert out.event.checks_used == 6, out.event.reason


# ==================================================================
# 🔴-3 载体归属**不得被抹平** + 生产者真接线
# ==================================================================

class TestCarrierAttributionIsPreserved:
    def _comment(self):
        return "\n".join([
            '<!-- run-scope: {"session_id": "sessionA", "mode": "scan", '
            '"impact": "tests/a.py", "command": "pytest -q tests/a.py", '
            '"revision": "rA", "count": 1, "verdict": "passed", '
            '"revisions": ["rA"]} -->',
            '<!-- run-scope: {"session_id": "sessionB", "mode": "scan", '
            '"impact": "tests/b.py", "command": "pytest -q tests/b.py", '
            '"revision": "rB", "count": 1, "verdict": "passed", '
            '"revisions": ["rB"]} -->',
        ])

    def test_ci_writeback_keeps_original_owners(self, rules):
        """CI 侧（无会话号）写回**不得**把别人的归属改写成空串。"""
        g = RunScopeGuard(rules)
        g.adopt_trails(self._comment())
        out = g.trails()
        assert '"session_id": "sessionA"' in out
        assert '"session_id": "sessionB"' in out
        assert '"session_id": ""' not in out, "归属被抹平（漏跑方向）"

    def test_third_session_does_not_inherit_others(self, rules):
        """**判别性核心**：第三个会话不得继承别人的台账（旧实现实测 `skip_duplicate`）。"""
        g = RunScopeGuard(rules)
        g.adopt_trails(self._comment())
        rewritten = g.trails()
        g3 = RunScopeGuard(rules)
        assert g3.adopt_trails(rewritten, session_id="sessionC")["merged"] == 0
        d = g3.decide_repeat(mode="scan", revision="rC",
                             impact="tests/a.py",
                             command="pytest -q tests/a.py")
        assert d.action == "run", "别的会话的台账不得替本会话判定（漏跑）"

    def test_owner_can_still_recover_own_records(self, rules):
        """**正向对照**：归属仍在 → 原会话照常恢复自己的台账。"""
        g = RunScopeGuard(rules)
        g.adopt_trails(self._comment())
        rewritten = g.trails()
        gA = RunScopeGuard(rules)
        assert gA.adopt_trails(rewritten, session_id="sessionA")["merged"] == 1

    def test_explicit_session_id_still_wins(self, rules):
        """显式传参仍以调用方为准（本会话自己的记录写回时带上本会话号）。"""
        g = RunScopeGuard(rules, session_id="sessionX")
        g.record_repeat(mode="scan", revision="r1", impact=["a.py"],
                        command="pytest -q a.py", verdict="ok")
        assert '"session_id": "sessionX"' in g.trails()
        # 显式覆盖
        assert '"session_id": "sessionY"' in g.trails(session_id="sessionY")

    def test_record_repeat_carries_owner(self, rules):
        """登记时归属落进记录（否则写回时无从"原样带回"）。"""
        g = RunScopeGuard(rules, session_id="sessionZ")
        g.record_repeat(mode="scan", revision="r1", impact=["a.py"],
                        command="pytest -q a.py", verdict="ok")
        assert '"session_id": "sessionZ"' in g.trails()


class TestCarrierProducersExist:
    def test_env_vars_have_producers(self):
        """`CNB_SESSION_ID` / `CNB_RUN_SCOPE_TRAIL` 必须有**写侧**（不再只有读）。"""
        body = _read("scripts/ci-incremental-test.sh")
        assert "carrier.session_id_for" in body, "会话号必须有推导（生产者）"
        assert "carrier._write" in body, "新留痕必须落文件供写回"
        yml = _read(".cnb.yml")
        assert "collab-carrier.sh fetch" in yml, "跑前取载体必须接线"
        assert "collab-carrier.sh writeback" in yml, "跑后写回必须接线"

    def test_session_id_derivable_without_env(self):
        """会话号可由 `CNB_REPO_SLUG` + PR 号**推导**（同一 PR = 同一会话，跨容器稳定）。"""
        assert carrier.session_id_for(
            {"CNB_REPO_SLUG": "org/repo", "CNB_PULL_REQUEST_IID": "15"}) \
            == "org/repo#15"
        assert carrier.session_id_for(
            {"CNB_REPO_SLUG": "org/repo", "CNB_ISSUE_IID": "14"}) == "org/repo#14"
        assert carrier.session_id_for({"CNB_SESSION_ID": "explicit"}) == "explicit"

    def test_no_context_means_no_session(self):
        """**反向对照**：无 PR/Issue 上下文 → **空归属**（不得瞎猜一个会话号）。"""
        assert carrier.session_id_for({"CNB_REPO_SLUG": "org/repo"}) == ""

    def test_writeback_refuses_empty_owner(self, tmp_path):
        """归属为空 → **不写回**（宁可这次不落载体，也不抹平别人的归属）。"""
        f = tmp_path / "new.txt"
        f.write_text('<!-- run-scope: {"session_id": "x"} -->\n', encoding="utf-8")
        res = carrier.run_writeback(
            {"CNB_RUN_SCOPE_NEW_TRAIL_FILE": str(f),
             "CNB_REPO_SLUG": "org/repo"},   # 无 PR/Issue 号 → 推导不出
            transport=lambda *a, **k: (200, "{}"))
        assert res["ok"] is False
        assert "归属为空" in res["reason"]

    def test_merge_keeps_old_lines_and_owners(self):
        """合并**只增不减**：旧留痕（含各自归属）原样保留，新行追加。"""
        old = ('<!-- collab-run-scope-carrier -->\n'
               '<!-- run-scope: {"session_id": "A", "count": 1} -->\n')
        new = '<!-- run-scope: {"session_id": "B", "count": 2} -->'
        merged = carrier.merge_carrier_body(old, new)
        assert '"session_id": "A"' in merged and '"session_id": "B"' in merged
        # 重复追加不膨胀
        assert carrier.merge_carrier_body(merged, new).count('"B"') == 1

    def test_fetch_writes_trail_file(self, tmp_path):
        """`fetch` 把载体正文里的留痕落文件（跑前恢复的**真实**入口）。"""
        out = tmp_path / "trail.txt"
        comment = ('<!-- collab-run-scope-carrier -->\n'
                   '<!-- run-scope: {"session_id": "A", "count": 1} -->\n')
        calls = []

        def tr(method, url, headers, body):
            calls.append((method, url))
            if method == "GET" and url.endswith("/comments?page=1&page_size=100"):
                return 200, json.dumps([{"id": "77", "body": comment}])
            if method == "GET" and url.endswith("/comments/77"):
                return 200, json.dumps({"id": "77", "body": comment})
            return 404, "{}"

        res = carrier.run_fetch(
            {"CNB_REPO_SLUG": "org/repo", "CNB_PULL_REQUEST_IID": "15",
             "CNB_RUN_SCOPE_TRAIL_FILE": str(out)}, transport=tr)
        assert res["ok"] is True and res["comment_id"] == "77"
        assert RUN_SCOPE_TRAIL_MARKER in out.read_text(encoding="utf-8")

    def test_fetch_without_context_is_safe(self, tmp_path):
        """**反向对照**：无 PR/Issue 上下文 → 空文件 + 不误拦（不炸、不阻塞）。"""
        out = tmp_path / "trail.txt"
        out.write_text("stale", encoding="utf-8")
        res = carrier.run_fetch({"CNB_REPO_SLUG": "org/repo",
                                 "CNB_RUN_SCOPE_TRAIL_FILE": str(out)},
                                transport=lambda *a, **k: (200, "[]"))
        assert res["ok"] is False
        assert out.read_text(encoding="utf-8") == ""

    def test_writeback_patches_same_comment(self, tmp_path):
        """写回**追加到同一条评论**（PATCH，不是新建评论 → 评论数不膨胀）。"""
        f = tmp_path / "new.txt"
        f.write_text('<!-- run-scope: {"session_id": "A", "count": 3} -->\n',
                     encoding="utf-8")
        seen = []

        def tr(method, url, headers, body):
            seen.append((method, url))
            if method == "GET" and url.endswith("/comments?page=1&page_size=100"):
                return 200, json.dumps([{"id": "77", "body": "<!-- collab-run-scope-carrier -->"}])
            if method == "GET":
                return 200, json.dumps({"id": "77", "body": "<!-- collab-run-scope-carrier -->"})
            return 200, "{}"

        res = carrier.run_writeback(
            {"CNB_REPO_SLUG": "org/repo", "CNB_PULL_REQUEST_IID": "15",
             "CNB_RUN_SCOPE_NEW_TRAIL_FILE": str(f)}, transport=tr)
        assert res["ok"] is True and res["action"] == "patch"
        assert any(m == "PATCH" and u.endswith("/comments/77") for m, u in seen)


# ==================================================================
# 🔴-4 零计数**变体**不再判红（例外不被恒真激活）
# ==================================================================

class TestZeroCountVariants:
    @pytest.mark.parametrize("verdict", [
        "577 passed, no errors", "errors: 0", "1 xfailed", "no failures",
        "no errors, no failures", "failures = 0", "zero errors",
        "577 passed, 0 failed", "0 failed", "0 errors", "853 passed in 6.47s",
    ])
    def test_zero_variants_are_not_red(self, verdict):
        """零计数的**其它常见写法**同样是绿（旧实现只认"数字 0 开头"）。"""
        assert _is_red(verdict) is False, verdict

    @pytest.mark.parametrize("verdict", [
        "2 failed", "1 error", "0 errors, 3 failed", "failed", "🔴 不通过",
        "FAILED tests/test_a.py", "失败", "1 failed, 577 passed",
        "errors: 1", "3 xfailed, 1 failed",
    ])
    def test_real_failures_still_red(self, verdict):
        """**反向对照**：真失败仍是红（判据不得被修成恒 False）。"""
        assert _is_red(verdict) is True, verdict

    def test_no_errors_does_not_license_rerun(self, rules):
        """端到端：`"577 passed, no errors"` → 同口径重跑仍**跳过**。"""
        g = RunScopeGuard(rules)
        kw = dict(mode="scan", revision="abc123",
                  impact=["src/a.py"], command="pytest -q tests/test_a.py")
        g.record(**kw, verdict="577 passed, no errors")
        d = g.decide(**kw, last_verdict="577 passed, no errors")
        assert d.action == SKIP_DUPLICATE, d.reason


# ==================================================================
# 结转项：🟢-1 死形参**已回退删除** + 红后复跑**已落上界**
# ==================================================================

class TestCarriedOverItems:
    def test_collect_marked_has_no_dead_param(self):
        """🟢-1：死形参 `max_depth` **删除**（形参存在却不生效是同一类缺陷）。"""
        import inspect
        from secretary import trail as trail_mod
        params = list(inspect.signature(trail_mod.collect_marked).parameters)
        assert params == ["text", "marker"], params

    def test_red_rerun_is_bounded(self, rules):
        """结转第 3 条：文档写"复跑**一次**" → 必须有上界（实测旧实现无上界）。"""
        assert rules.max_rerun_on_failure == 1
        g = RunScopeGuard(rules)
        kw = dict(mode="scan", revision="r1", impact=["a.py"],
                  command="pytest -q a.py")
        actions = []
        for _ in range(5):
            actions.append(g.decide(**kw, last_verdict="2 failed").action)
            g.record(**kw, verdict="2 failed")
        assert actions[0] == "run" and actions[1] == "run", actions
        assert all(a == SKIP_DUPLICATE for a in actions[2:]), \
            "连续'修了仍红'必须有上界（旧实现全 run）"

    def test_red_rerun_cap_is_discriminative(self, rules):
        """**反向对照**：把上界设 0 → 不绿也不复跑（证明上一条不是恒真）。"""
        import dataclasses
        capped = dataclasses.replace(rules, max_rerun_on_failure=0)
        g = RunScopeGuard(capped)
        kw = dict(mode="scan", revision="r1", impact=["a.py"],
                  command="pytest -q a.py")
        g.record(**kw, verdict="2 failed")
        assert g.decide(**kw, last_verdict="2 failed").action == SKIP_DUPLICATE

    def test_green_path_unaffected(self, rules):
        """反向：绿 + 同口径 → 仍跳过（上界不得把判重放宽）。"""
        g = RunScopeGuard(rules)
        kw = dict(mode="scan", revision="r1", impact=["a.py"],
                  command="pytest -q a.py")
        g.record(**kw, verdict="577 passed")
        assert g.decide(**kw, last_verdict="577 passed").action == SKIP_DUPLICATE
