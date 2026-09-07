"""M9 秘书并发归属与冲突仲裁测试（OwnershipGuard）

覆盖：
- 归属声明/释放(claim/release)，owner 与 version 演进
- 他人占用时抢占被拒(StateConflict)，force 可强抢
- 越权写被拒(OwnershipDenied)
- 乐观并发冲突：基于旧 base_version 写入被拒(StateConflict)，不静默覆盖
- 未带版本号写向后兼容
- 卡归属为空的"首个写者隐式取得维护权"
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../src"))

from secretary.state import (
    StateManager, OwnershipGuard, StateConflict, OwnershipDenied,
)


def test_claim_and_release_owner():
    """归属声明/释放：owner 字段随 claim/release 演进，version 单调递增"""
    with tempfile.TemporaryDirectory() as tmpdir:
        mgr = StateManager(data_dir=tmpdir)
        mgr.create_card("sess-own-1", objective="并发测试")
        g = OwnershipGuard("sess-own-1", mgr, secretary="secre_a")

        assert g.current_owner() == ""      # 初始未归属
        v0 = g.version()
        g.claim()
        assert g.current_owner() == "secre_a"
        assert g.version() > v0             # 归属使版本递增

        g.release()
        assert g.current_owner() == ""      # 释放后无归属


def test_conflict_on_occupied_by_others():
    """他人占用未过期：非 owner 秘书 claim 被拒(StateConflict)，force 可强抢"""
    with tempfile.TemporaryDirectory() as tmpdir:
        mgr = StateManager(data_dir=tmpdir)
        mgr.create_card("sess-own-2", objective="并发")
        ga = OwnershipGuard("sess-own-2", mgr, secretary="secre_a")
        ga.claim()
        # B 想抢占 A 占用中的卡
        gb = OwnershipGuard("sess-own-2", mgr, secretary="secre_b")
        try:
            gb.claim()
            assert False, "应抛 StateConflict"
        except StateConflict as e:
            assert e.current_owner == "secre_a"
            assert e.requester == "secre_b"
        # 仍归 A
        assert ga.current_owner() == "secre_a"
        # force 可强抢
        gb.claim(force=True)
        assert gb.current_owner() == "secre_b"


def test_lease_expired_allow_reclaim():
    """租约过期后，其他秘书可重新 claim（确定性处置）"""
    with tempfile.TemporaryDirectory() as tmpdir:
        mgr = StateManager(data_dir=tmpdir)
        mgr.create_card("sess-own-3", objective="过期")
        ga = OwnershipGuard("sess-own-3", mgr, secretary="secre_a")
        ga.claim()
        # 手动把 A 的租约改为已过期
        import time as _t
        lease_path = os.path.join(tmpdir, "sess-own-3.owner.json")
        import json
        with open(lease_path, encoding="utf-8") as f:
            lease = json.load(f)
        lease["until"] = _t.time() - 10   # 已过期
        with open(lease_path, "w", encoding="utf-8") as f:
            json.dump(lease, f)
        # B 可接管（租约过期）
        gb = OwnershipGuard("sess-own-3", mgr, secretary="secre_b")
        gb.claim()   # 不抛异常
        assert gb.current_owner() == "secre_b"


def test_optimistic_version_conflict():
    """乐观并发：基于旧 base_version 写入被拒(StateConflict)，不静默覆盖"""
    with tempfile.TemporaryDirectory() as tmpdir:
        mgr = StateManager(data_dir=tmpdir)
        mgr.create_card("sess-own-4", objective="乐观锁")
        ga = OwnershipGuard("sess-own-4", mgr, secretary="secre_a")
        ga.claim()
        v0 = ga.version()
        # A 先加一个任务 → 版本变为 v0+1
        ga.guarded_add_task("任务A", base_version=v0)
        v1 = ga.version()
        assert v1 == v0 + 1

        # B(同一 owner 视为获授权) 基于旧版本 v0 再写 → 冲突
        # 注：secretary 用 A 表示同一维护者不同执行，验证版本仲裁本身
        g2 = OwnershipGuard("sess-own-4", mgr, secretary="secre_a")
        try:
            g2.guarded_add_task("任务B-旧版本", base_version=v0)
            assert False, "应抛 StateConflict(基于旧版本)"
        except StateConflict as e:
            assert e.expected_version == v0
            assert e.actual_version == v1
        # 用最新版本写成功
        g2.guarded_add_task("任务B-最新", base_version=v1)
        assert len(mgr.get_card("sess-own-4").tasks) == 2


def test_ownership_denied_for_unprivileged_writer():
    """越权写：卡被 A 占用，B(非 owner、无授权)写被拒(OwnershipDenied)"""
    with tempfile.TemporaryDirectory() as tmpdir:
        mgr = StateManager(data_dir=tmpdir)
        mgr.create_card("sess-own-5", objective="越权")
        ga = OwnershipGuard("sess-own-5", mgr, secretary="secre_a")
        ga.claim()
        gb = OwnershipGuard("sess-own-5", mgr, secretary="secre_b")
        try:
            gb.guarded_add_task("B想加任务")
            assert False, "应抛 OwnershipDenied"
        except OwnershipDenied:
            pass
        # A 正常写
        ga.guarded_add_task("A加任务", base_version=ga.version())
        assert len(mgr.get_card("sess-own-5").tasks) == 1


def test_first_writer_implicit_owner():
    """卡归属为空时，首个写者隐式取得维护权"""
    with tempfile.TemporaryDirectory() as tmpdir:
        mgr = StateManager(data_dir=tmpdir)
        mgr.create_card("sess-own-6", objective="隐式归属")
        g = OwnershipGuard("sess-own-6", mgr, secretary="secre_x")
        # 未 claim 直接写
        g.guarded_add_task("首个任务")
        assert g.current_owner() == "secre_x"  # 隐式归属
        card = mgr.get_card("sess-own-6")
        assert len(card.tasks) == 1


def test_base_version_none_backward_compat():
    """不携带 base_version → 放弃乐观检测，向后兼容原用法"""
    with tempfile.TemporaryDirectory() as tmpdir:
        mgr = StateManager(data_dir=tmpdir)
        mgr.create_card("sess-own-7", objective="兼容")
        g = OwnershipGuard("sess-own-7", mgr, secretary="secre_a")
        g.claim()
        # 不传 base_version 也正常写
        t = g.guarded_add_task("任务", base_version=None)
        assert t is not None
        assert len(mgr.get_card("sess-own-7").tasks) == 1


def test_implicit_owner_blocks_other_writer():
    """隐式归属后，另一秘书写应被拒(OwnershipDenied)——锁定修复回归。"""
    with tempfile.TemporaryDirectory() as tmpdir:
        mgr = StateManager(data_dir=tmpdir)
        mgr.create_card("sess-own-8", objective="隐式归属越权")
        # A 未 claim 直接写 → 隐式取得 owner
        ga = OwnershipGuard("sess-own-8", mgr, secretary="secre_a")
        ga.guarded_add_task("A首写")
        assert ga.current_owner() == "secre_a"

        # B 也想写 → 因 owner 已是 A，应被拒
        gb = OwnershipGuard("sess-own-8", mgr, secretary="secre_b")
        try:
            gb.guarded_add_task("B想写")
            assert False, "B 不应能写 A 隐式归属的卡"
        except OwnershipDenied:
            pass

        # A 仍可正常写
        ga.guarded_add_task("A再写", base_version=ga.version())
        card = mgr.get_card("sess-own-8")
        assert len(card.tasks) == 2


def test_release_returns_false_for_non_owner():
    """非 owner 秘书调用 release 应返回 False 且不改动卡。"""
    with tempfile.TemporaryDirectory() as tmpdir:
        mgr = StateManager(data_dir=tmpdir)
        mgr.create_card("sess-own-9", objective="释放判定")
        ga = OwnershipGuard("sess-own-9", mgr, secretary="secre_a")
        ga.claim()

        gb = OwnershipGuard("sess-own-9", mgr, secretary="secre_b")
        released = gb.release()
        assert released is False
        # owner 仍是 A
        assert ga.current_owner() == "secre_a"

        # A release 返回 True
        released_a = ga.release()
        assert released_a is True
        assert ga.current_owner() == ""


def test_concurrent_writers_no_loss():
    """真实并发回归：两个独立 StateManager(同 owner 秘书)并发写同一卡，
    通过 flock+CAS 原子提交保证两任务均不丢失。
    """
    import subprocess
    import sys as _sys

    with tempfile.TemporaryDirectory() as tmpdir:
        # 预创建卡片并让秘书 A claim 为 owner
        mgr = StateManager(data_dir=tmpdir)
        mgr.create_card("sess-conc", objective="并发不丢")
        ga = OwnershipGuard("sess-conc", mgr, secretary="secre_a")
        ga.claim()

        src_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src")

        # 每个进程模拟一个独立 StateManager 实例，owner 同为 secre_a
        # 但用不同进程（各读各的版本），验证 flock+CAS 防丢
        script_template = """
import sys
sys.path.insert(0, {src!r})
from secretary.state.manager import StateManager
from secretary.state.ownership import OwnershipGuard, StateConflict, OwnershipDenied

def worker(data_dir, session, secretary, title):
    mgr = StateManager(data_dir=data_dir)
    g = OwnershipGuard(session, mgr, secretary=secretary)
    for attempt in range(10):
        try:
            v = g.version()
            g.guarded_add_task(title, base_version=v)
            return 0
        except (StateConflict, OwnershipDenied):
            continue  # 冲突/越权则重试
    return 1

rc = worker({dir!r}, {session!r}, {sec!r}, {title!r})
sys.exit(rc)
"""

        # 两个子进程各自调用 guarded_add_task 加不同标题任务
        # 由于 owner 都是 secre_a，两者都不会抛 OwnershipDenied
        # 并发时 flock+CAS 确保不会相互覆盖
        code_a = script_template.format(
            src=src_path, dir=tmpdir, session='sess-conc',
            sec='secre_a', title='并发-A任务')
        code_b = script_template.format(
            src=src_path, dir=tmpdir, session='sess-conc',
            sec='secre_a', title='并发-B任务')

        p1 = subprocess.Popen([_sys.executable, "-c", code_a],
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        p2 = subprocess.Popen([_sys.executable, "-c", code_b],
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        out1, err1 = p1.communicate(timeout=10)
        out2, err2 = p2.communicate(timeout=10)

        assert p1.returncode == 0, f"进程1失败: {err1.decode()}"
        assert p2.returncode == 0, f"进程2失败: {err2.decode()}"

        # 读取最终状态，验证两个任务都在（没有丢失覆盖）
        final = mgr.get_card("sess-conc")
        titles = [t.title for t in final.tasks]
        assert "并发-A任务" in titles, f"任务A丢失，当前任务: {titles}"
        assert "并发-B任务" in titles, f"任务B丢失，当前任务: {titles}"


def test_update_state_silent_failure_fixed():
    """guarded_update_task_state 对不存在的任务抛 ValueError，不再静默失败。"""
    with tempfile.TemporaryDirectory() as tmpdir:
        mgr = StateManager(data_dir=tmpdir)
        mgr.create_card("sess-own-10", objective="更新失败")
        g = OwnershipGuard("sess-own-10", mgr, secretary="secre_a")
        g.claim()
        t = g.guarded_add_task("任务1", base_version=g.version())
        # 更新不存在任务应抛 ValueError
        try:
            g.guarded_update_task_state("NOT-EXIST", "done", base_version=g.version())
            assert False, "应抛 ValueError(任务不存在)"
        except ValueError as e:
            assert "任务不存在" in str(e)
        # 不存在的非法迁移也应报错
        try:
            g.guarded_update_task_state(t.task_id, "nonexistent_state", base_version=g.version())
            assert False, "应抛 ValueError(非法迁移)"
        except ValueError:
            pass


def test_cas_save_card_conflict_no_cache_pollution():
    """cas_save_card 版本冲突时不污染 _mem_cache：冲突卡不应被读到。

    评审指出：冲突(不落盘)时若将调用方卡片写入 _mem_cache 且不清理，
    后续 get_card 会读到仅存在于内存、从未落盘的幻影态。
    """
    with tempfile.TemporaryDirectory() as tmpdir:
        mgr = StateManager(data_dir=tmpdir)
        mgr.create_card("sess-cache", objective="缓存冲突")

        # 先用另一 guard 把版本推进到 v1
        g = OwnershipGuard("sess-cache", mgr, secretary="secre_a")
        g.claim()
        v_after_claim = g.version()
        g.guarded_add_task("已提交任务", base_version=v_after_claim)
        v1 = mgr.get_card("sess-cache").version
        assert v1 > v_after_claim

        # 模拟另一调用方基于旧版本(v0) 调用 cas_save_card → 版本冲突
        stale_card = mgr.get_card("sess-cache")   # 从磁盘读到最新 v1
        stale_card.version = 0                      # 改为模拟旧版本读取结果
        ok, current_v = mgr.cas_save_card(stale_card, expected_version=0)
        assert not ok, f"应版本冲突，ok={ok}"
        assert current_v == v1

        # 关键验证：冲突后 _mem_cache 不应有幻影条目。
        # 若污染则 get_card 返回 stale_card(version=0)，而非磁盘上的 v1。
        after = mgr.get_card("sess-cache")
        assert after is not None
        assert after.version == v1, f"缓存被污染: 读到 version={after.version}，期望磁盘 version={v1}"
        # 磁盘内容不受冲突调用影响
        titles = [t.title for t in after.tasks]
        assert "已提交任务" in titles


if __name__ == "__main__":
    test_claim_and_release_owner()
    test_conflict_on_occupied_by_others()
    test_lease_expired_allow_reclaim()
    test_optimistic_version_conflict()
    test_ownership_denied_for_unprivileged_writer()
    test_first_writer_implicit_owner()
    test_base_version_none_backward_compat()
    test_implicit_owner_blocks_other_writer()
    test_release_returns_false_for_non_owner()
    test_concurrent_writers_no_loss()
    test_update_state_silent_failure_fixed()
    test_cas_save_card_conflict_no_cache_pollution()
    print("✅ M9 秘书并发归属与冲突仲裁测试全部通过")
