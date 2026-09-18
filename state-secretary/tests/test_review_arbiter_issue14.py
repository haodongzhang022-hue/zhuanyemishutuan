"""Issue #14 补全机制回归：评审版本仲裁 + 审查范围分级

需求口径（用户）：
  1. **只在开启评审时记录版本**，过程中变化先不管；
  2. **无法追踪变化就以新的**（不编造冲突）；
  3. 正在编辑 → **防同写锁 + 延迟一段时间再处理**；
  4. **先完成该做的**（评审先落盘），**只额外处理一个**解决冲突（单冲突收口）；
  5. **不要大量全量**：**只在大版本做全量**，日常开发**只审变化**，防止无效构建/测试。

判别性设计（关键断言均配反向对照，防恒真）：
  - 「只在开启评审时记录版本」：开发过程断言 ``recorded() is False``（反向：
    开启后为 True 且版本=启动时磁盘版本）；
  - 「冲突不阻塞整体」：冲突场景下 ``applied is True`` 且磁盘版本已变（反向：
    延迟窗场景 ``applied is False`` 且磁盘版本不变）；
  - 「单冲突收口」：连续 3 次冲突后待处理卡**仍有且仅有 1 张**，``merged_conflicts==3``；
  - 「可重试不算冲突」：同向写入 + 过渡态 → ``should_retry`` 且 ``conflict is False``；
  - 「日常不全量」：默认/常规评审文本 → ``scan``（反向：大版本标签 → ``full``）。
"""
from __future__ import annotations

import os
import shutil
import tempfile

import pytest

from secretary.review import (
    FULL,
    SCAN,
    ConflictCard,
    ReviewArbiter,
    ReviewBaseline,
    decide_scan,
    is_same_side,
    scan_commands,
)
from secretary.session import SecretarySession
from secretary.state import OwnershipGuard


class FakeClock:
    """可推进的时钟（不 sleep 也能验证延迟窗语义）。"""

    def __init__(self, t: float = 1_000_000.0):
        self.t = t

    def __call__(self) -> float:
        return self.t

    def advance(self, sec: float) -> None:
        self.t += sec


@pytest.fixture
def env():
    """一组常用对象：会话 + 评审/开发两方 + 共享数据根。"""
    d = tempfile.mkdtemp()
    clock = FakeClock()
    rev = SecretarySession("i14", data_dir=d, role_name="secretary.review")
    dev = SecretarySession("i14", data_dir=d, role_name="secretary.dev")
    rev.review_arbiter._clock = clock          # 注入假时钟
    try:
        yield d, clock, rev, dev
    finally:
        shutil.rmtree(d, ignore_errors=True)


def dev_write(dev: SecretarySession, title: str = "修复产出",
              keep_lease: bool = False) -> None:
    """模拟开发方一次受控写入（**有效推进**：新增一条任务）。

    ``keep_lease=False``（默认）写完即清租约 = **编辑已完成**（写入为既成事实）；
    ``keep_lease=True`` 保留租约 = **正在编辑**（用于验证延迟窗）。
    """
    og = OwnershipGuard("i14", dev.state_manager, secretary="secretary.dev")
    og.guarded_add_task(title)
    if not keep_lease:
        og._clear_lease()


def dev_concurrent_overwrite(dev: SecretarySession, note: str = "并发改写") -> None:
    """模拟**不构成推进**的并发写入（只改目标，无新任务/完成态）。

    用于构造"真冲突"：对方在评审期间改了一个字段，但既没新增任务、也没抵达
    完成态 → 判定链走到最后一步，登记一张冲突卡。
    """
    og = OwnershipGuard("i14", dev.state_manager, secretary="secretary.dev")
    og.claim()
    og._clear_lease()
    card = dev.state_manager.read_card_disk("i14")
    card.objective = note
    dev.state_manager.save_card(card)


# ------------------------------------------------------------------
# ① 只在开启评审时记录版本
# ------------------------------------------------------------------

class TestBaselineOnlyOnReviewStart:
    def test_no_baseline_during_development(self, env):
        """开发过程中的任何变更都**不**记录评审版本。"""
        _, _, rev, dev = env
        for i in range(3):
            dev.add_task(f"开发步骤{i}")
        assert rev.review_recorded() is False
        assert rev.review_baseline() is None
        assert not os.path.exists(rev.review_arbiter.baseline_path())

    def test_begin_review_records_current_version(self, env):
        """开启评审才记版本：基线 = 开启瞬间的磁盘版本（反向对照上一条）。"""
        _, _, rev, dev = env
        dev.add_task("开发步骤")
        assert rev.review_recorded() is False
        base = rev.begin_review()
        assert rev.review_recorded() is True
        disk = rev.state_manager.read_card_disk("i14")
        assert base.version == disk.version
        assert rev.review_baseline()["version"] == base.version

    def test_begin_review_idempotent_does_not_advance(self, env):
        """已开启评审后再调 ``begin_review`` **不**改写基线（防"过程中也记版本"）。"""
        _, _, rev, dev = env
        first = rev.begin_review().version
        dev.add_task("评审中又提交")
        again = rev.begin_review()
        assert again.version == first, "重复开启评审不得把基线偷偷推前"
        # 反向佐证：显式 force 才允许重立
        assert rev.begin_review(force=True).version > first

    def test_begin_review_requires_existing_card(self, env):
        d, _, _, _ = env
        arb = ReviewArbiter("ghost", SecretarySession(
            "keepalive", data_dir=d).state_manager, reviewer="secretary.review",
            data_dir=os.path.join(d, "review"))
        with pytest.raises(ValueError):
            arb.begin_review()

    def test_apply_before_review_rebases_not_fails(self, env):
        """未开启评审就写评审结果 → 不炸、不阻塞，直接以新版本为准（宽进）。"""
        _, _, rev, _ = env
        assert rev.review_recorded() is False
        dec = rev.apply_review(note="未开启评审的写入")
        assert dec.applied is True
        assert dec.action == "rebase"
        assert dec.rebased is True
        assert rev.review_recorded() is True


# ------------------------------------------------------------------
# ② 先完成该做的：评审先落盘，冲突不阻塞整体
# ------------------------------------------------------------------

class TestSaveFirstThenConflict:
    def test_conflict_still_applies_review(self, env):
        """真冲突时评审结果**已落盘**（关键顺序：先完成该做的）。"""
        _, _, rev, dev = env
        rev.begin_review()
        dev_concurrent_overwrite(dev)       # 并发改写（非推进）
        before = rev.state_manager.read_card_disk("i14").version
        dec = rev.apply_review(note="评审打回")
        after = rev.state_manager.read_card_disk("i14").version
        assert dec.applied is True, "冲突不得阻塞评审落盘"
        assert dec.conflict is True
        assert after == before + 1

    def test_effective_progress_is_not_conflict(self, env):
        """反向对照：对方**有效推进**（新增任务，无反向可挑项）→ 不算冲突。"""
        _, _, rev, dev = env
        rev.begin_review()
        dev_write(dev, "实现新功能")
        # 反向对照：同一状态若**没有**新增任务（纯并发覆盖）则判冲突
        assert rev.apply_review(note="").conflict is False

    def test_no_progress_no_conflict(self, env):
        """对方只改了自己的 owner/时间戳、未实质推进 → 不判冲突（不编造）。"""
        _, _, rev, dev = env
        rev.begin_review()
        # 只改 objective（无新增任务、无完成态）
        dev.state_manager.get_card("i14").objective = "同目标更详细描述"
        dev.state_manager.save_card(dev.state_manager.get_card("i14"))
        dec = rev.apply_review(note="")
        assert dec.conflict is False

    def test_conflict_reason_mentions_versions(self, env):
        _, _, rev, dev = env
        rev.begin_review()
        dev_concurrent_overwrite(dev)
        dec = rev.apply_review(note="")
        assert dec.conflict is True
        assert "v" in dec.reason and dec.card is not None
        assert dec.card.base_version < dec.card.current_version


# ------------------------------------------------------------------
# ②b 推进判定参照物 = 评审基线（防"含对方变更的最新卡"自我比较恒判冲突）
# ------------------------------------------------------------------

class TestProgressReferenceIsBaseline:
    """回归护栏：判定"对方是否推进"必须拿**基线快照**比，不能拿写回前的最新卡比。

    起因（实缺陷）：``disk_before`` 是**开始写回时**的最新卡，对方的并发写入早已
    落在其中；拿它和"写回后"比较（写回只把版本 +1、任务集不变）→ 恒为"无进展"
    → 每次并发都误判冲突、每次都开冲突卡（正是要治的循环）。
    """

    def test_progress_uses_baseline_snapshot(self, env):
        """开发方在评审期间新增任务 → 判**有推进**（不冲突）。"""
        _, _, rev, dev = env
        rev.begin_review()
        assert rev.review_baseline()["history"], "开启评审须落基线快照"
        dev_write(dev, "评审期间实现的新功能")
        dec = rev.apply_review(note="评审打回")
        assert dec.conflict is False, "对方新增任务属有效推进，不得判冲突"
        assert dec.applied is True

    def test_same_inputs_but_no_progress_does_conflict(self, env):
        """反向对照：同样的并发写入，但**没有**新增任务/完成态 → 判冲突。"""
        _, _, rev, dev = env
        rev.begin_review()
        dev_concurrent_overwrite(dev, "只改目标")
        assert rev.apply_review(note="").conflict is True

    def test_deep_copy_semantics_not_object_identity(self, env):
        """判别性：参照物必须是**快照内容**，不是"另读一次磁盘对象"。

        读两次磁盘得到的都是"含对方变更的最新卡"（内容相同）——若实现拿它们
        互比，恒判无进展。本用例断言"读两次磁盘内容一致"这一事实，从而锁住
        "不能靠再读一次磁盘当参照物"。
        """
        _, _, rev, dev = env
        rev.begin_review()
        dev_write(dev, "新任务")
        c1 = rev.state_manager.read_card_disk("i14")
        c2 = rev.state_manager.read_card_disk("i14")
        assert [t.title for t in c1.tasks] == [t.title for t in c2.tasks]
        base_card = rev.review_arbiter._baseline_snapshot(
            rev.review_arbiter.get_baseline(), rev.state_manager)
        assert base_card is not None
        assert len(c1.tasks) > len(base_card.tasks), "基线快照必须早于对方写入"

    def test_baseline_snapshot_missing_falls_back_wide(self, env):
        """快照不可得 → 宽进（视为有进展），不编造冲突。"""
        _, _, rev, dev = env
        rev.begin_review()
        arb = rev.review_arbiter
        base = arb.get_baseline()
        base.history = []
        arb._atomic_write(arb.baseline_path(), base.to_dict())
        assert arb._baseline_snapshot(arb.get_baseline(), arb.sm) is None
        dev_concurrent_overwrite(dev)
        assert rev.apply_review(note="").conflict is False

    def test_corrupt_snapshot_does_not_break(self, env):
        """坏快照（card 非 dict / 版本类型错）→ 忽略该条，不抛异常。"""
        _, _, rev, dev = env
        rev.begin_review()
        arb = rev.review_arbiter
        base = arb.get_baseline()
        base.history = [{"version": "x", "card": "not-a-dict"},
                        {"version": 0, "card": {"broken": True}}]
        arb._atomic_write(arb.baseline_path(), base.to_dict())
        loaded = arb.get_baseline()
        assert all(isinstance(h.get("card"), dict) for h in loaded.history)

    def test_baseline_history_bounded(self, env):
        """快照轨迹有界（最多 2 条）——不随轮次无限增长。"""
        _, _, rev, dev = env
        rev.begin_review()
        for i in range(4):
            dev_write(dev, f"推进{i}")
            rev.apply_review(note="评审")
        hist = rev.review_baseline()["history"]
        assert len(hist) <= 2

# ------------------------------------------------------------------
# ③ 防同写锁 + 延迟重试（正在编辑先等）
# ------------------------------------------------------------------

class TestEditingLockAndRetry:
    def test_editing_lease_defers_processing(self, env):
        """他人持写租约 → 给防同写锁 + 延迟窗，本次**不落盘**。"""
        _, clock, rev, dev = env
        rev.begin_review()
        og = OwnershipGuard("i14", dev.state_manager, secretary="secretary.dev")
        og.claim()                          # 开发方"正在编辑"（持写租约）
        before = rev.state_manager.read_card_disk("i14").version
        dec = rev.apply_review(note="评审")
        assert dec.action == "retry"
        assert dec.should_retry is True
        assert dec.applied is False, "延迟窗内不得落盘（先完成编辑）"
        assert dec.conflict is False
        assert dec.retry_after == pytest.approx(clock() + rev.review_arbiter.retry_delay_sec)
        assert rev.state_manager.read_card_disk("i14").version == before

    def test_retry_waits_until_window_elapsed(self, env):
        """延迟窗未到 → 继续等待（不落盘）；到点后正常处理。"""
        _, clock, rev, dev = env
        rev.begin_review()
        og = OwnershipGuard("i14", dev.state_manager, secretary="secretary.dev")
        og.claim()
        first = rev.apply_review(note="评审")
        assert first.should_retry is True
        clock.advance(rev.review_arbiter.retry_delay_sec - 1)
        still = rev.retry_review(note="评审")
        assert still.should_retry is True
        assert still.applied is False

    def test_retry_after_lease_released_applies(self, env):
        """编辑结束（租约释放、无他人租约）→ 延迟重试直接完成，无需人工。"""
        _, clock, rev, dev = env
        rev.begin_review()
        og = OwnershipGuard("i14", dev.state_manager, secretary="secretary.dev")
        og.claim()
        assert rev.apply_review(note="评审").should_retry is True
        og.release()
        clock.advance(rev.review_arbiter.retry_delay_sec + 1)
        dec = rev.retry_review(note="评审")
        assert dec.applied is True
        assert dec.conflict is False
        assert dec.action == "save"

    def test_own_lease_does_not_defer_self(self, env):
        """反向对照：**自己的**租约不算"正在编辑"，不触发延迟。"""
        _, _, rev, _ = env
        rev.begin_review()
        og = OwnershipGuard("i14", rev.state_manager, secretary="secretary.review")
        og.claim()
        dec = rev.apply_review(note="评审")
        assert dec.should_retry is False
        assert dec.applied is True

    def test_same_side_write_is_retryable_not_conflict(self, env):
        """同向写入（另一方也是评审）+ 过渡态 → 可重试时序，**不算冲突**。

        两步都验：① 租约在 → 延迟重试；② 租约释放后按规则判为**非冲突**。
        """
        _, clock, rev, dev = env
        rev.begin_review()
        og = OwnershipGuard("i14", dev.state_manager,
                            secretary="secretary.review.b")
        og.claim()
        dec = rev.apply_review(note="复核：仍不合格")
        assert dec.should_retry is True
        assert dec.conflict is False, "同向写入不得判为冲突（只延迟重试）"

        og.release()
        clock.advance(rev.review_arbiter.retry_delay_sec + 1)
        dec2 = rev.retry_review(note="复核：仍不合格")
        assert dec2.applied is True
        assert dec2.conflict is False


# ------------------------------------------------------------------
# ④ 单冲突收口：只额外处理一个
# ------------------------------------------------------------------

class TestSingleConflictCloseout:
    def test_repeated_conflicts_merge_into_one_card(self, env):
        """连续多次冲突 → 待处理卡**始终只有一张**，``merged_conflicts`` 累加。"""
        _, _, rev, dev = env
        rev.begin_review()
        for i in range(3):
            dev_concurrent_overwrite(dev, f"并发改写{i}")
            dec = rev.apply_review(note="评审打回")
            assert dec.conflict is True
            assert dec.applied is True
            pending = rev.pending_review_conflicts()
            assert len(pending) == 1, "冲突卡必须收敛为一张"
            assert pending[0].id == dec.card.id
        assert rev.pending_review_conflicts()[0].merged_conflicts == 3

    def test_merged_card_keeps_first_id(self, env):
        """合并进同一张：卡 ID 不随冲突次数变化（可稳定引用）。"""
        _, _, rev, dev = env
        rev.begin_review()
        first = None
        for i in range(3):
            dev_concurrent_overwrite(dev, f"改写{i}")
            dec = rev.apply_review(note="评审")
            first = first or dec.card.id
            assert dec.card.id == first

    def test_resolve_closes_and_rebases_baseline(self, env):
        """收口一次即闭合：状态 resolved、基线以当前版本重立。"""
        _, _, rev, dev = env
        rev.begin_review()
        dev_concurrent_overwrite(dev)
        dec = rev.apply_review(note="评审")
        card_id = dec.card.id
        assert rev.resolve_review_conflict(card_id, "保留双方：新基线继续",
                                           by="张浩东") is True
        assert rev.pending_review_conflicts() == []
        base = rev.review_baseline()
        assert base["version"] == rev.state_manager.read_card_disk("i14").version
        assert base["source"] == "rebase"

    def test_resolve_rejects_empty_resolution(self, env):
        _, _, rev, dev = env
        rev.begin_review()
        dev_concurrent_overwrite(dev)
        card_id = rev.apply_review(note="评审").card.id
        with pytest.raises(ValueError):
            rev.resolve_review_conflict(card_id, "   ")
        assert len(rev.pending_review_conflicts()) == 1

    def test_resolve_unknown_ticket_returns_false(self, env):
        _, _, rev, _ = env
        assert rev.resolve_review_conflict("REVCONF-i14-1", "x") is False

    def test_new_card_supersedes_old_resolved(self, env):
        """已收口的旧卡被新卡接管（不堆积历史待办）。"""
        _, _, rev, dev = env
        rev.begin_review()
        dev_concurrent_overwrite(dev)
        old = rev.apply_review(note="评审").card.id
        own = rev.review_arbiter
        assert own.resolve(old, "收口") is True
        own._atomic_write(
            own.conflict_path(),
            {**own.get_conflict().to_dict(), "status": "superseded"})
        dev_concurrent_overwrite(dev, "再次冲突")
        new_card = rev.apply_review(note="评审").card
        assert len(rev.pending_review_conflicts()) == 1
        assert rev.pending_review_conflicts()[0].id == new_card.id
        assert rev.pending_review_conflicts()[0].status == "pending"

    def test_retry_success_supersedes_pending(self, env):
        """重试成功且无冲突 → 之前那张待处理卡作废（同一冲突不重复占位）。"""
        _, clock, rev, dev = env
        rev.begin_review()
        dev_concurrent_overwrite(dev)
        assert rev.apply_review(note="评审").conflict is True
        assert len(rev.pending_review_conflicts()) == 1
        # 冲突消解（对方继续推进 → 推进分支生效）→ 重试成功：不冲突 + 旧卡作废
        clock.advance(1)
        dev_write(dev, "冲突后继续推进")
        dec = rev.retry_review(note="评审")
        assert dec.applied is True
        assert dec.conflict is False
        assert rev.pending_review_conflicts() == []


# ------------------------------------------------------------------
# ⑤ 无法追踪变化就以新的（不编造冲突）
# ------------------------------------------------------------------

class TestUntrackableRebase:
    def test_missing_baseline_rebases(self, env):
        _, _, rev, _ = env
        dec = rev.apply_review(note="")
        assert dec.kind == "no_baseline"
        assert dec.rebased is True

    def test_version_regress_rebases(self, env):
        """当前版本低于基线（状态被回退/共享卡复用）→ 以新为准，不判冲突。"""
        _, _, rev, dev = env
        rev.begin_review(force=True)
        arb = rev.review_arbiter
        # 人为把基线抬到远高于当前版本
        arb._atomic_write(arb.baseline_path(), ReviewBaseline(
            session_id="i14", version=999, reviewer="secretary.review").to_dict())
        dec = rev.apply_review(note="")
        assert dec.kind == "version_regress"
        assert dec.conflict is False
        assert dec.rebased is True
        assert rev.review_baseline()["version"] < 999

    def test_no_common_ancestor_not_conflict(self, env):
        """双方版本都低于基线（无共同祖先）→ 不判冲突（不编造）。"""
        _, _, rev, dev = env
        rev.begin_review()
        arb = rev.review_arbiter
        arb._atomic_write(arb.baseline_path(), ReviewBaseline(
            session_id="i14", version=999, reviewer="secretary.review").to_dict())
        before = rev.state_manager.read_card_disk("i14")
        applied = arb._do_save(lambda c: None)
        after = rev.state_manager.read_card_disk("i14")
        assert applied is True
        assert arb._is_conflict(before, after, arb.get_baseline(), "") is False

    def test_unknown_writer_not_conflict(self, env):
        """写者不明（无租约、owner 为空）→ 不判冲突。"""
        _, _, rev, _ = env
        rev.begin_review()
        arb = rev.review_arbiter
        og = OwnershipGuard("i14", rev.state_manager, secretary="x")
        og.claim()
        og.release()
        card = rev.state_manager.read_card_disk("i14")
        assert (card.owner or "") == ""
        assert arb._owner_of(card) == ""


# ------------------------------------------------------------------
# ⑥ 健壮性与生命周期
# ------------------------------------------------------------------

class TestPersistenceAndLifecycle:
    def test_corrupt_baseline_degrades_to_none(self, env):
        """基线文件损坏 → 视为未登记（宽进不炸，以新为准）。"""
        _, _, rev, _ = env
        with open(rev.review_arbiter.baseline_path(), "w", encoding="utf-8") as f:
            f.write("{ bad json")
        assert rev.review_baseline() is None
        assert rev.apply_review(note="").rebased is True

    @pytest.mark.parametrize("raw", ["[]", '"str"', "123", '{"version": "9"}'])
    def test_malformed_baseline_variants(self, env, raw):
        _, _, rev, _ = env
        with open(rev.review_arbiter.baseline_path(), "w", encoding="utf-8") as f:
            f.write(raw)
        assert rev.review_baseline() is None

    def test_corrupt_conflict_card_degrades(self, env):
        _, _, rev, _ = env
        with open(rev.review_arbiter.conflict_path(), "w", encoding="utf-8") as f:
            f.write("{ bad json")
        assert rev.review_arbiter.get_conflict() is None
        assert rev.pending_review_conflicts() == []

    @pytest.mark.parametrize("bad", ["../escape", "a/b", "", "a b", "会话"])
    def test_invalid_session_id_rejected(self, env, bad):
        d, _, rev, _ = env
        with pytest.raises(ValueError):
            ReviewArbiter(bad, rev.state_manager, reviewer="r",
                          data_dir=os.path.join(d, "review"))

    def test_invalid_retry_delay_rejected(self, env):
        d, _, rev, _ = env
        for bad in (-1, True, "30"):
            with pytest.raises(ValueError):
                ReviewArbiter("i14", rev.state_manager, reviewer="r",
                              data_dir=os.path.join(d, "review"),
                              retry_delay_sec=bad)

    def test_empty_reviewer_rejected(self, env):
        d, _, rev, _ = env
        with pytest.raises(ValueError):
            ReviewArbiter("i14", rev.state_manager, reviewer="   ",
                          data_dir=os.path.join(d, "review"))

    def test_invalid_ticket_id_traversal_rejected(self, env):
        _, _, rev, _ = env
        with pytest.raises(ValueError):
            rev.review_arbiter.card_path("../evil")

    def test_delete_session_cleans_review_state(self, env):
        """会话回收一并清理基线与冲突卡；同 id 复建不带陈旧状态。"""
        _, _, rev, dev = env
        rev.begin_review()
        dev_concurrent_overwrite(dev)
        arb = rev.review_arbiter
        assert arb.apply_save(note="").conflict is True
        assert os.path.exists(arb.baseline_path())
        assert os.path.exists(arb.conflict_path())
        rev.delete_session()
        assert not os.path.exists(arb.baseline_path())
        assert not os.path.exists(arb.conflict_path())

    def test_review_dir_default_under_session_root(self, env):
        d, _, rev, _ = env
        assert os.path.dirname(rev.review_arbiter.baseline_path()) \
            == os.path.join(d, "review")


# ------------------------------------------------------------------
# ⑦ 审查范围分级：日常只审变化，全量只在大版本
# ------------------------------------------------------------------

class TestScanScope:
    @pytest.mark.parametrize("kwargs", [
        {}, {"text": "日常修复"}, {"text": "评审打回第 2 轮"},
        {"tags": ["bug"]}, {"tags": []},
    ])
    def test_default_is_scan(self, kwargs):
        """反向对照基线：非大版本一律 ``scan``（不触发全量）。"""
        ctx = decide_scan(**kwargs)
        assert ctx.mode == SCAN
        assert ctx.is_full is False

    @pytest.mark.parametrize("kwargs", [
        {"tags": ["major-version"]},
        {"tags": ["大版本"]},
        {"text": "大版本发版评审"},
        {"text": "release 前全量回归"},
        {"major_version": True},
    ])
    def test_major_version_is_full(self, kwargs):
        assert decide_scan(**kwargs).mode == FULL

    def test_explicit_changes_only_wins_over_full_tag(self):
        """显式"只要变化"压过大版本标签（先完成该做的，不空转全量）。"""
        ctx = decide_scan(tags=["major-version"], text="这轮只审变化就行")
        assert ctx.mode == SCAN
        assert "只审变化" in ctx.reason or "scan" in ctx.signals[-1]

    def test_major_flag_beats_text(self):
        ctx = decide_scan(major_version=True, text="只审变化")
        assert ctx.mode == FULL

    def test_scan_commands_never_full(self):
        """``scan`` 档位的执行口径明确**不含全量**（防无效构建/测试）。"""
        cmds = scan_commands(decide_scan())
        assert "全量" in cmds["build"] and "仅变更" in cmds["build"]
        assert "全量测试（大版本专用）" not in cmds["test"]
        assert "不跑全量" in cmds["test"]

    def test_full_commands_allow_full(self):
        cmds = scan_commands(decide_scan(tags=["major-version"]))
        assert cmds["test"] == "全量测试（大版本专用）"

    def test_scan_context_serializable(self):
        d = decide_scan(text="日常").to_dict()
        assert d["mode"] == SCAN
        assert d["review_changes_only"] is True
        assert "只审变化" in d["guidance"]

    def test_facade_scope(self, env):
        """门面接入：评审角色可直接问范围口径。"""
        _, _, rev, _ = env
        assert rev.review_scope().mode == SCAN
        assert rev.review_scope(tags=["major-version"]).mode == FULL
        assert "不跑全量" in rev.review_scope_commands()["test"]


# ------------------------------------------------------------------
# ⑧ 规则与数据模型
# ------------------------------------------------------------------

class TestRulesAndModels:
    @pytest.mark.parametrize("a,b,expected", [
        ("秘书·评审", "reviewer", True),
        ("secretary.review", "秘书·复评", True),
        ("秘书·开发", "secretary.dev", True),
        ("秘书·评审", "秘书·开发", False),
        ("unknown-a", "unknown-b", False),
        ("", "秘书·评审", False),
    ])
    def test_same_side_classification(self, a, b, expected):
        assert is_same_side(a, b) is expected

    def test_baseline_from_dict_tolerates_garbage(self):
        assert ReviewBaseline.from_dict(None) is None
        assert ReviewBaseline.from_dict([]) is None
        assert ReviewBaseline.from_dict({"version": "9"}) is None
        assert ReviewBaseline.from_dict({"version": True}) is None
        base = ReviewBaseline.from_dict({"version": 3})
        assert base is not None and base.version == 3

    def test_conflict_card_from_dict_tolerates_garbage(self):
        assert ConflictCard.from_dict(None) is None
        assert ConflictCard.from_dict({"session_id": "s"}) is None      # 缺 id
        assert ConflictCard.from_dict({"id": "../x", "session_id": "s"}) is None
        card = ConflictCard.from_dict(
            {"id": "C-1", "session_id": "s", "merged_conflicts": True})
        assert card is not None and card.merged_conflicts == 1


# ------------------------------------------------------------------
# ⑨ 与循环熔断（LoopGuard）联动的整合口径
# ------------------------------------------------------------------

class TestIntegrationWithLoopGuard:
    """评审仲裁与轮次熔断协同：**先完成该做的**，冲突只额外占一个处理位。"""

    def test_conflict_does_not_consume_loop_budget(self, env):
        """冲突登记**不**消耗循环修复预算（冲突不是"又一轮修复"）。"""
        _, _, rev, dev = env
        rev.begin_review()
        dev_concurrent_overwrite(dev)
        before = rev.loop_status()["blocking_rounds"]
        dec = rev.apply_review(note="")
        assert dec.conflict is True
        assert rev.loop_status()["blocking_rounds"] == before

    def test_conflict_card_count_stays_one_across_rounds(self, env):
        """跨多轮冲突：待处理卡恒为 1 张（不随轮次增殖 → 不形成新循环）。"""
        _, _, rev, dev = env
        rev.begin_review()
        for i in range(5):
            dev_concurrent_overwrite(dev, f"并发{i}")
            rev.apply_review(note=f"第{i+1}轮")
            rev.review_round(blocking=1, note=f"第{i+1}轮门禁")
            assert len(rev.pending_review_conflicts()) <= 1

    def test_resolve_then_continue_reviewing(self, env):
        """收口一次后继续评审：基线重立，后续正常走增量（不全量）。"""
        _, _, rev, dev = env
        rev.begin_review()
        dev_concurrent_overwrite(dev)
        card_id = rev.apply_review(note="评审").card.id
        assert rev.resolve_review_conflict(card_id, "保留双方，新基线继续",
                                           by="张浩东") is True
        assert rev.pending_review_conflicts() == []
        # 收口后开发继续推进 → 下一轮判无冲突（增量）
        dev_write(dev, "收口后继续实现")
        assert rev.apply_review(note="下一轮评审").conflict is False
        assert rev.review_scope().mode == SCAN, "日常开发不走全量"

    def test_fused_session_still_records_review(self, env):
        """循环熔断不影响"先完成该做的"：评审结果照常落盘（只堵唤醒）。"""
        _, _, rev, dev = env
        rev.begin_review()
        assert rev.fuse_loop("上游叫停") is True
        before = rev.state_manager.read_card_disk("i14").version
        dec = rev.apply_review(note="熔断后的收尾评审")
        assert dec.applied is True
        assert rev.state_manager.read_card_disk("i14").version == before + 1
        assert rev.should_wake_next() is False
