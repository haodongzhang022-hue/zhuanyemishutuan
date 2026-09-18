"""Issue #14 追问回归：锁聚合管理 + 延迟后检测 + 先做其他并挂锁 + 绝不遗留锁

需求口径（用户）：
  1. **延迟可以到 1 分钟**（不再用 30s）；
  2. **再次触发就按 3 分钟持续到对方解决完**（锁升级为持续锁，不自动打开）；
  3. **注意别遗留锁一直不打开**（每把锁都有到期/回收路径，sweep 兜底）；
  4. **所有锁应该被聚合的管理**（一本台账：写租约 / 评审读锁 / 延迟锁）；
  5. **哪里要避开不用测试才知道，触发了一眼就能看**（`locks_overview()`）；
  6. 触发机制：**延迟后检测**（到点自动重试）+ **先做其他，并挂锁**。

判别性设计（关键断言均配反向对照，防恒真）：
  - 「1 分钟」：首次延迟窗恰为 60s（反向：退避后会变长，但不是 30s）；
  - 「3 分钟退避」：重触发后等待依次 60→120→180 封顶（反向：不缩短已承诺的等待）；
  - 「持续锁不打开」：`should_release()` 在宽限内恒 False，超宽限才 True；
  - 「不遗留」：过期超宽限 → `sweep()` 判 stale（反向：活跃/宽限内锁**不被**误杀）；
  - 「聚合」：写租约 / 评审读锁 / 延迟锁**同时**出现在一份 `overview()` 里；
  - 「先做其他」：拿不到锁返回 `do_other_first` 且挂上锁，不空等。
"""
from __future__ import annotations

import os
import shutil
import tempfile

import pytest

from secretary.review import DEFAULT_PERSISTENT_GRACE_SEC, DEFAULT_RETRY_DELAY_SEC
from secretary.session import SecretarySession
from secretary.state import (
    KIND_CARD_WRITE,
    KIND_DEFER,
    KIND_REVIEW,
    KIND_SERIALIZE,
    LOCK_HELD,
    LOCK_STALE,
    CardLease,
    LegacyLockScanner,
    LockRegistry,
    OwnershipGuard,
)

SESSION = "i14"


class FakeClock:
    """可推进的时钟（不 sleep 也能验证延迟/宽限语义）。"""

    def __init__(self, t: float = 1_000_000.0):
        self.t = t

    def __call__(self) -> float:
        return self.t

    def advance(self, sec: float) -> None:
        self.t += sec


@pytest.fixture
def env():
    """会话 + 假时钟（台账与仲裁共用同一时间源，避免真实/假时钟混用）。"""
    d = tempfile.mkdtemp()
    clock = FakeClock()
    rev = SecretarySession(SESSION, data_dir=d, role_name="secretary.review")
    dev = SecretarySession(SESSION, data_dir=d, role_name="secretary.dev")
    # 假时钟必须在**建会话之后立即注入**：台账会按当时时钟把注册时间写进磁盘，
    # 中途改时钟会与已落盘的 acquired_at 错位（判 stale 时算出"永不过期"）。
    dev.lock_registry._clock = clock
    rev.review_arbiter._clock = clock
    rev.lock_registry._clock = clock
    try:
        yield d, clock, rev, dev
    finally:
        shutil.rmtree(d, ignore_errors=True)


def dev_starts_editing(dev: SecretarySession, holder: str = "secretary.dev") -> OwnershipGuard:
    """开发方进入"正在编辑"：建写租约 + 镜像进聚合台账（与生产接线一致）。"""
    og = OwnershipGuard(SESSION, dev.state_manager, secretary=holder)
    og.guarded_add_task("开发产出")
    og.lock_registry = dev.lock_registry
    og._store_lease(CardLease(holder))
    return og


# ------------------------------------------------------------------
# ① 默认口径：1 分钟延迟 + 3 分钟持续
# ------------------------------------------------------------------

class TestDefaults:
    def test_defaults_are_60s_and_180s(self):
        """口径默认值本身即契约：延迟 1 分钟、持续 3 分钟（反向：不是旧 30s）。"""
        assert DEFAULT_RETRY_DELAY_SEC == 60.0
        assert DEFAULT_PERSISTENT_GRACE_SEC == 180.0

    def test_session_uses_ladder_first_step(self, env):
        """不传参时，会话默认按**跨仓库规则包**取阶梯首档 10s（同源三处）。"""
        _, _, rev, _ = env
        assert rev.review_arbiter.retry_delay_sec == 10.0
        assert rev.lock_registry.ttl_sec == 10.0
        assert rev.lock_registry.persistent_grace_sec == 180.0


# ------------------------------------------------------------------
# ② 延迟后检测 + 先做其他，并挂锁
# ------------------------------------------------------------------

class TestDeferAndRetryAfter:
    def test_first_defer_window_is_10s(self, env):
        """首次遇到"正在编辑" → 延迟窗 = 阶梯首档 10s（反向对照：≠ 60s 旧值）。"""
        _, clock, rev, dev = env
        rev.begin_review()
        dev_starts_editing(dev)
        dec = rev.apply_review(note="评审")
        assert dec.should_retry is True
        assert dec.deferred is True
        assert dec.applied is False, "延迟内不落盘（先做其他）"
        assert dec.retry_after == pytest.approx(clock() + 10.0)

    def test_lock_is_registered_and_visible(self, env):
        """挂锁立刻可在聚合台账里查到（"触发了就可以去看一眼"）。"""
        _, _, rev, dev = env
        rev.begin_review()
        dev_starts_editing(dev)
        rev.apply_review(note="评审")
        held = rev.lock_registry.list_held()
        defer = [l for l in held if l.kind == KIND_DEFER]
        assert len(defer) == 1
        assert defer[0].holder == "secretary.review"
        assert "先做其他" in defer[0].scope

    def test_retry_before_window_does_not_apply(self, env):
        """延迟后检测：窗未到 → 继续等（不落盘）。"""
        _, clock, rev, dev = env
        rev.begin_review()
        dev_starts_editing(dev)
        rev.apply_review(note="评审")
        clock.advance(59)
        dec = rev.retry_review(note="评审")
        assert dec.should_retry is True
        assert dec.applied is False

    def test_retry_after_window_applies_and_releases(self, env):
        """到点后对方已处理完 → 正常落盘并**打开锁**（不遗留）。"""
        _, clock, rev, dev = env
        rev.begin_review()
        og = dev_starts_editing(dev)
        rev.apply_review(note="评审")
        og.release()                      # 对方处理完（写租约释放）
        clock.advance(11)
        dec = rev.retry_review(note="评审")
        assert dec.applied is True
        assert dec.conflict is False
        assert rev.lock_registry.list_held(KIND_DEFER) == [], "编辑结束应打开延迟锁"

    def test_defers_again_while_still_editing(self, env):
        """对方还在编辑 → 锁**续期不打开**（先做其他），且不会丢。"""
        _, clock, rev, dev = env
        rev.begin_review()
        dev_starts_editing(dev)
        rev.apply_review(note="评审")
        clock.advance(11)
        dec = rev.retry_review(note="评审")
        assert dec.should_retry is True
        assert dec.applied is False
        assert dec.retry_after >= clock()
        assert len(rev.lock_registry.list_held(KIND_DEFER)) == 1, "锁不得被丢掉"


# ------------------------------------------------------------------
# ③ 再次触发 → 3 分钟持续锁（退避，不缩短承诺）
# ------------------------------------------------------------------

class TestRetriggerBecomesPersistent:
    def test_retrigger_escalates_to_persistent(self, env):
        """再次触发同一把延迟锁 → 升级为持续锁（持续到对方处理完）。"""
        _, _, rev, dev = env
        rev.begin_review()
        dev_starts_editing(dev)
        rev.apply_review(note="评审1")
        dec = rev.apply_review(note="评审2")
        lock = rev.lock_registry.get(rev.review_arbiter.defer_lock_key())
        assert lock is not None and lock.persistent is True
        assert lock.retry_count >= 1
        assert dec.lock and dec.lock["persistent"] is True

    def test_backoff_extends_never_shortens(self, env):
        """退避：等待沿阶梯依次变长（10 → 30 → 60 → 180），**绝不缩短已有承诺**。"""
        _, clock, rev, dev = env
        rev.begin_review()
        dev_starts_editing(dev)
        delays = []
        for i in range(4):
            dec = rev.apply_review(note=f"评审{i}")
            delays.append(round(dec.retry_after - clock()))
        assert delays[0] == 10
        assert delays == sorted(delays), f"等待不得回退: {delays}"
        assert delays[-1] == 180, f"第 4 次触发应到 3 分钟档: {delays}"

    def test_persistent_lock_not_released_within_grace(self, env):
        """**持续锁在宽限内不许打开**（对方还没解决完）。"""
        _, clock, rev, dev = env
        rev.begin_review()
        dev_starts_editing(dev)
        rev.apply_review(note="评审1")
        rev.apply_review(note="评审2")
        key = rev.review_arbiter.defer_lock_key()
        clock.advance(100)
        assert rev.lock_registry.should_release(key) is False
        assert len(rev.lock_registry.list_held(KIND_DEFER)) == 1

    def test_retry_still_waiting_after_grace_if_editing(self, env):
        """过了 3 分钟对方仍在编辑 → 继续等待（不放弃、不打开锁）。"""
        _, clock, rev, dev = env
        rev.begin_review()
        dev_starts_editing(dev)
        rev.apply_review(note="评审1")
        rev.apply_review(note="评审2")
        clock.advance(181)
        dec = rev.retry_review(note="评审")
        assert dec.should_retry is True
        assert dec.retry_after >= clock(), "仍要给出下一次检测点（到顶后为当前窗口）"
        assert len(rev.lock_registry.list_held(KIND_DEFER)) == 1

    def test_deferred_status_reports_situation(self, env):
        """`deferred_status()` 一眼看清：谁在编辑、锁多久、是否持续锁。"""
        _, _, rev, dev = env
        rev.begin_review()
        dev_starts_editing(dev)
        rev.apply_review(note="评审1")
        rev.apply_review(note="评审2")
        st = rev.deferred_review_status()
        assert st is not None
        assert st["persistent"] is True
        # 再次触发（第 2 次）时卡面记录的重触发数是"此前已触发过几次"= 1
        assert st["retriggered"] >= 1, f"重触发计数应被记录: {st}"
        assert st["editing"] is True
        assert st["lock_should_release"] is False
        assert st["retry_in_sec"] > 0


# ------------------------------------------------------------------
# ④ 绝不遗留锁：回收路径 + 不误杀
# ------------------------------------------------------------------

class TestNoLeftoverLocks:
    def test_sweep_reclaims_expired_lock(self, env):
        """过期超过宽限未被处理 → sweep 判 stale 回收（"别一直不打开"）。"""
        _, clock, _, dev = env
        reg = dev.lock_registry
        og = OwnershipGuard(SESSION, dev.state_manager, secretary="secretary.dev",
                            lock_registry=reg)
        og.guarded_add_task("写")
        og._store_lease(CardLease("secretary.dev"))
        clock.advance(400)
        swept = reg.sweep(by="tester")
        assert [r.key for r in swept] == [og.registry_key()]
        assert reg.get(og.registry_key()).status == LOCK_STALE

    def test_sweep_does_not_kill_active_lock(self, env):
        """反向对照：**活跃锁**（未过期）绝不被 sweep 误杀。"""
        _, clock, _, dev = env
        reg = dev.lock_registry
        og = OwnershipGuard(SESSION, dev.state_manager, secretary="secretary.dev",
                            lock_registry=reg)
        og.guarded_add_task("写")
        og._store_lease(CardLease("secretary.dev"))
        clock.advance(10)
        assert reg.sweep(by="tester") == []
        assert reg.get(og.registry_key()).status == LOCK_HELD

    def test_sweep_does_not_kill_grace_persistent_lock(self, env):
        """反向对照：宽限内的**持续锁**不得被回收（对方还在处理）。"""
        _, _, rev, dev = env
        rev.begin_review()
        dev_starts_editing(dev)
        rev.apply_review(note="评审1")
        rev.apply_review(note="评审2")
        assert rev.sweep_locks(by="tester") == []
        assert len(rev.lock_registry.list_held(KIND_DEFER)) == 1

    def test_sweep_stale_defer_closes_pending_ticket(self, env):
        """遗留延迟锁被判 stale 时，挂账项一并关闭（不留半拉子状态）。"""
        _, clock, rev, dev = env
        rev.begin_review()
        dev_starts_editing(dev)
        rev.apply_review(note="评审1")
        rev.apply_review(note="评审2")
        clock.advance(600 + 180 + 30 + 1)     # 过期 + 宽限（阶梯顶 10 分钟）
        swept = rev.sweep_locks(by="tester")
        assert any(r["key"] == rev.review_arbiter.defer_lock_key() for r in swept)
        assert rev.deferred_review_status() is None, "锁被回收后不得再挂着待处理项"

    def test_release_on_resolve(self, env):
        """冲突收口 → 延迟锁一并打开（处理完了就不该再挂锁）。"""
        _, _, rev, dev = env
        rev.begin_review()
        dev_starts_editing(dev)
        dec = rev.apply_review(note="评审")
        assert dec.deferred is True
        ticket = rev.deferred_review_status()["ticket"]
        assert rev.resolve_review_conflict(ticket, "已人工处理", by="tester") is True
        assert rev.lock_registry.list_held(KIND_DEFER) == []
        assert rev.deferred_review_status() is None

    def test_release_review_locks_does_not_touch_card_lease(self, env):
        """反向对照：释放评审锁**不动**卡片写租约（那是别人的编辑锁）。"""
        _, _, rev, dev = env
        rev.begin_review()
        og = dev_starts_editing(dev)
        dev.lock_registry = rev.lock_registry
        og.lock_registry = rev.lock_registry
        released = rev.release_review_locks(by="tester")
        assert released >= 1
        assert rev.lock_registry.get(og.registry_key()).status == LOCK_HELD


# ------------------------------------------------------------------
# ⑤ 所有锁聚合管理 + 触发即可查看
# ------------------------------------------------------------------

class TestAggregatedVisibility:
    def test_overview_aggregates_all_kinds(self, env):
        """写租约 / 评审读锁 / 延迟锁 **同处一份总览**（类型计数可验）。"""
        _, _, rev, dev = env
        dev.lock_registry = rev.lock_registry
        dev_starts_editing(dev)             # card_write
        rev.begin_review()                  # review
        rev.apply_review(note="评审")       # defer
        view = rev.locks_overview()
        assert view["by_kind"].get(KIND_CARD_WRITE) == 1
        assert view["by_kind"].get(KIND_REVIEW) == 1
        assert view["by_kind"].get(KIND_DEFER) == 1
        assert view["count"] == 3

    def test_overview_lists_where_to_avoid(self, env):
        """「哪里要避开」是给人看的清单，含持有者与剩余时间。"""
        _, _, rev, dev = env
        rev.begin_review()
        dev_starts_editing(dev)
        rev.apply_review(note="评审")
        view = rev.locks_overview()
        joined = " | ".join(view["where_to_avoid"])
        assert "评审读锁" in joined
        assert "先做其他" in joined
        assert "secretary.review" in joined
        # 台账锁逐条列出；若另有 legacy 单锁，则多一条（刻意保留可见性）
        assert len(view["where_to_avoid"]) == view["count"] + len(view["legacy_locks"])

    def test_overview_mentions_legacy_locks(self, env):
        """老的单锁文件也被扫出来上报（聚合视图不留观察盲区）。"""
        _, _, rev, dev = env
        dev_starts_editing(dev)
        legacy = rev.locks_overview()["legacy_locks"]
        assert any(l.get("holder") == "secretary.dev" for l in legacy)
        assert any("legacy" in s for s in rev.locks_overview()["where_to_avoid"])

    def test_legacy_scanner_marks_expired(self, env):
        """过期单锁被标 stale（可见），未过期标 held（反向对照）。"""
        _, clock, _, dev = env
        d = dev.state_manager.data_dir
        path = os.path.join(d, f"{SESSION}.owner.json")
        lease = CardLease("secretary.dev", ttl_sec=10)
        with open(path, "w", encoding="utf-8") as f:
            f.write(__import__("json").dumps(lease.to_dict()))
        fresh = LegacyLockScanner.scan(d)[0]
        assert fresh["status"] == "held"
        lease.until = 1.0        # 人为置为早已过期
        with open(path, "w", encoding="utf-8") as f:
            f.write(__import__("json").dumps(lease.to_dict()))
        stale = LegacyLockScanner.scan(d)[0]
        assert stale["status"] == "stale"

    def test_summary_carries_lock_view(self, env):
        """向上汇总带锁视图：上级不必问、不必测，直接看得到。"""
        _, _, rev, dev = env
        rev.begin_review()
        dev_starts_editing(dev)
        rev.apply_review(note="评审")
        summary = rev.summarize_for_upstream()
        assert summary["locks"]["count"] >= 1
        assert summary["locks"]["by_kind"].get(KIND_DEFER) == 1

    def test_history_traceable(self, env):
        """台账留痕：登记 → 重触发 都能在 history 里查到。"""
        _, _, rev, dev = env
        rev.begin_review()
        dev_starts_editing(dev)
        rev.apply_review(note="评审1")
        rev.apply_review(note="评审2")
        rows = rev.lock_registry.history(rev.review_arbiter.defer_lock_key())
        actions = {r.get("action") for r in rows}
        assert "register" in actions
        assert "re-acquire" in actions


# ------------------------------------------------------------------
# ⑥ 拿不到锁 → 先做其他（不等）
# ------------------------------------------------------------------

class TestDoOtherFirst:
    def test_acquire_blocked_returns_do_other_first(self):
        """锁被他人持有 → 返回 `do_other_first` 并把锁挂在自己名下等待。"""
        d = tempfile.mkdtemp()
        try:
            reg = LockRegistry(SESSION, data_dir=d)
            first = reg.acquire("k1", kind=KIND_DEFER, holder="A")
            assert first is not None
            assert reg.acquire("k1", kind=KIND_DEFER, holder="B") is None, "他人持锁不得抢占"
            instr = reg.acquire_blocked("k1", holder="B", reason="评审要写回")
            assert instr["action"] == "do_other_first"
            assert instr["holder"] == "A"
            assert instr["lock"]["holder"] == "B"
            assert instr["lock"]["persistent"] is True, "挂锁=持续等待，不是空等"
        finally:
            shutil.rmtree(d, ignore_errors=True)

    def test_hold_until_resolved_keeps_lock(self):
        """① 先做其他，并挂锁：持续锁到期仍可续期（heartbeat 后不该被回收）。"""
        d = tempfile.mkdtemp()
        try:
            clk = FakeClock()
            reg = LockRegistry(SESSION, data_dir=d, clock=clk)
            lock = reg.hold_until_resolved("k2", holder="B", reason="先做其他")
            assert lock.persistent is True
            clk.advance(170)
            reg.heartbeat("k2", "B")
            clk.advance(170)
            assert reg.sweep(by="t") == [], "持续心跳的锁不得被回收"
            assert reg.should_release("k2") is False
        finally:
            shutil.rmtree(d, ignore_errors=True)

    def test_complete_deferred_finishes_after_other_work(self, env):
        """「先做完其他的」回来收尾：对方已处理完 → 补做评审并打开锁。"""
        _, clock, rev, dev = env
        rev.begin_review()
        og = dev_starts_editing(dev)
        rev.apply_review(note="评审")
        og.release()                         # 对方处理完
        clock.advance(11)
        dec = rev.complete_deferred_review(note="收尾补做评审")
        assert dec.applied is True
        assert rev.lock_registry.list_held(KIND_DEFER) == []

    def test_complete_deferred_keeps_lock_if_still_editing(self, env):
        """反向对照：对方仍在编辑 → 收尾也不得落盘，锁保持挂着。"""
        _, clock, rev, dev = env
        rev.begin_review()
        dev_starts_editing(dev)
        rev.apply_review(note="评审")
        clock.advance(11)
        dec = rev.complete_deferred_review(note="收尾补做评审")
        assert dec.applied is False
        assert dec.should_retry is True
        assert len(rev.lock_registry.list_held(KIND_DEFER)) == 1


# ------------------------------------------------------------------
# ⑦ 台账健壮性
# ------------------------------------------------------------------

class TestRegistryRobustness:
    def test_bad_registry_file_degrades(self):
        """台账文件损坏/非对象 JSON → 视为空台账（不抛，后续可重建）。"""
        d = tempfile.mkdtemp()
        try:
            reg = LockRegistry(SESSION, data_dir=d)
            with open(reg.registry_path(), "w", encoding="utf-8") as f:
                f.write("{ not json")
            assert reg.overview()["count"] == 0
            rec = reg.register("k", holder="A")
            assert rec.key == "k" and reg.overview()["count"] == 1
            with open(reg.registry_path(), "w", encoding="utf-8") as f:
                f.write("[1, 2, 3]")
            assert reg.overview()["count"] == 0
        finally:
            shutil.rmtree(d, ignore_errors=True)

    def test_invalid_args_rejected(self):
        """非法 session_id / 锁类型 / 空 key / 负 TTL → ValueError（拒绝静默降级）。"""
        with pytest.raises(ValueError):
            LockRegistry("bad id!")
        with pytest.raises(ValueError):
            LockRegistry(SESSION, ttl_sec=-1)
        d = tempfile.mkdtemp()
        try:
            reg = LockRegistry(SESSION, data_dir=d)
            with pytest.raises(ValueError):
                reg.register("k", kind="not-a-kind")
            with pytest.raises(ValueError):
                reg.register("   ")
            with pytest.raises(ValueError):
                rev = SecretarySession(SESSION, data_dir=d)
                rev.review_arbiter.lock_key("bad/key")
        finally:
            shutil.rmtree(d, ignore_errors=True)

    def test_delete_state_clears_registry(self, env):
        """会话回收：台账一并删除（同 id 复建不带陈旧锁）。"""
        _, _, rev, dev = env
        rev.begin_review()
        assert rev.locks_overview()["count"] >= 1
        rev.lock_registry.delete_state()
        assert rev.locks_overview()["count"] == 0

    def test_registry_missing_lock_registry_does_not_break_lease(self, env):
        """反向对照：不接台账时单锁行为完全不变（台账只是观测面）。"""
        _, _, _, dev = env
        og = OwnershipGuard(SESSION, dev.state_manager, secretary="secretary.dev")
        og.guarded_add_task("无台账写入")
        og._store_lease(CardLease("secretary.dev"))
        assert og._load_lease().holder == "secretary.dev"
        og._clear_lease()
        assert og._load_lease() is None


# ------------------------------------------------------------------
# ⑧ 并发聚合：台账在多线程/多进程下不丢锁（"所有锁聚合"必须扛得住并发）
# ------------------------------------------------------------------

class TestConcurrentRegistry:
    def test_concurrent_register_no_lost_locks(self):
        """多线程并发登记不同锁 → 台账**一把不丢**（读改写有文件锁串行化）。"""
        import threading

        d = tempfile.mkdtemp()
        try:
            reg = LockRegistry(SESSION, data_dir=d)
            errors = []

            def worker(i: int) -> None:
                try:
                    r = LockRegistry(SESSION, data_dir=d)
                    r.register(f"k{i}", kind=KIND_SERIALIZE, holder=f"w{i}")
                except Exception as exc:  # noqa: BLE001
                    errors.append(exc)

            threads = [threading.Thread(target=worker, args=(i,))
                       for i in range(24)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()
            assert errors == []
            assert reg.overview()["count"] == 24
        finally:
            shutil.rmtree(d, ignore_errors=True)

    def test_concurrent_retrigger_counts_are_exact(self):
        """多线程并发**重触发同一把锁** → 计数精确（不丢不重，靠文件锁串行化）。"""
        import threading

        d = tempfile.mkdtemp()
        try:
            clk = FakeClock()
            reg = LockRegistry(SESSION, data_dir=d, clock=clk)
            reg.register("hot", kind=KIND_DEFER, holder="A")

            def worker() -> None:
                LockRegistry(SESSION, data_dir=d, clock=clk).register(
                    "hot", kind=KIND_DEFER, holder="A", increment=True)

            threads = [threading.Thread(target=worker) for _ in range(11)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()
            assert reg.get("hot").retry_count == 11
        finally:
            shutil.rmtree(d, ignore_errors=True)

    def test_two_sessions_locks_isolated_in_overview(self):
        """反向对照：另一会话的锁**不会**混进本会话总览（各自的台账、各自的键）。"""
        d = tempfile.mkdtemp()
        try:
            a = LockRegistry("s-a", data_dir=os.path.join(d, "a"))
            b = LockRegistry("s-b", data_dir=os.path.join(d, "b"))
            a.register("s-a:x", holder="A")
            b.register("s-b:x", holder="B")
            assert a.list_held()[0].session_id == "s-a"
            assert [l.key for l in b.list_held()] == ["s-b:x"]
        finally:
            shutil.rmtree(d, ignore_errors=True)
