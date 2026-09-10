"""M9 批量/延迟落盘 + 第2轮评审修复回归测试

覆盖：
- FlushQueue 批量合并：多次登记同一资源只写最后一次（增量取最终态）
- SecretaryTree/StateManager 延迟落盘：defer() 上下文内多次变更只触发一轮落盘
- 修复回归：HandoffBus 复用稳定 StateCard 时交接任务不重复堆积，且后到者 context 静默丢弃（🟡4 断言固化）
- 修复回归：submit_chain_up 断链(level_skipped)记录可由 CLI 展示逻辑安全消费(不抛 KeyError)
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../src"))

from secretary.state import (
    StateManager, SecretaryTree, HierarchySubmitter,
    FlushQueue, DeferredWriter,
)
from secretary.state.persist import FlushQueue as FQ


# ==== FlushQueue 批量合并 ====

def test_flush_queue_coalesce_same_key():
    """同一资源多次登记，flush 只写最后一次（增量取最终态）"""
    writes = []
    q = FQ(use_deferred=True)

    def mk_write(val):
        return (lambda: writes.append(val))

    q.enqueue("res-a", mk_write(1))
    q.enqueue("res-a", mk_write(2))   # 覆盖，应只剩 2
    q.enqueue("res-a", mk_write(3))
    assert q.pending_count == 1       # 同 key 合并为 1 项
    q.flush()
    assert writes == [3], f"同 key 未合并，应只写最终态，实际 {writes}"
    assert q.pending_count == 0


def test_flush_queue_batch_multi_resource():
    """多资源批量落盘一轮完成"""
    writes = []
    q = FQ(use_deferred=True)
    q.enqueue("a", lambda: writes.append("a"))
    q.enqueue("b", lambda: writes.append("b"))
    q.enqueue("c", lambda: writes.append("c"))
    assert q.pending_count == 3
    n = q.flush_all()
    assert n == 3
    assert sorted(writes) == ["a", "b", "c"]


def test_deferred_writer_context():
    """DeferredWriter 上下文退出时自动批量落盘"""
    writes = []
    q = FQ(use_deferred=False)  # 默认 write-through
    with DeferredWriter(q) as dq:
        dq.enqueue("x", lambda: writes.append("x"))
        # 延迟模式：未立即写
        assert writes == []
    # 退出后已批量落盘一次
    assert writes == ["x"]
    assert q.flush_count == 1


# ==== SecretaryTree / StateManager 延迟批量落盘 ====

def test_tree_deferred_batch_single_write():
    """defer() 上下文内多次变更仅触发一轮磁盘落盘"""
    with tempfile.TemporaryDirectory() as tmpdir:
        tree = SecretaryTree(data_dir=tmpdir, tree_name="batch")
        q = tree._flush_queue
        before = q.write_count
        with tree.defer():
            tree.register_node("root", display_name="秘书长")
            for i in range(10):
                tree.register_node(f"child-{i}", parent_session="root")
        # 20 多次变更 → 延迟模式下整批仅落盘 1 次
        assert q.write_count == before + 1, \
            f"期望 defer 内只落盘 1 次，实际 {q.write_count - before}"

        # 退出 defer 后数据已可被新实例加载
        tree2 = SecretaryTree(data_dir=tmpdir, tree_name="batch")
        assert len(tree2.nodes) == 11


def test_state_manager_deferred_batch():
    """StateManager defer() 内对同一会话多次加任务合并为一次落盘"""
    with tempfile.TemporaryDirectory() as tmpdir:
        mgr = StateManager(data_dir=tmpdir)
        q = mgr._flush_queue
        # 先建卡并落盘
        mgr.create_card("s1", objective="obj")
        before = q.write_count
        with mgr.defer():
            # 同一张卡连续加 8 个任务：write-through 需 8 次写盘，defer 合并为 1 次
            for i in range(8):
                mgr.add_task("s1", f"task-{i}", task_type="normal")
        # 同一 session 的多次写被合并：只落盘 1 次
        assert q.write_count == before + 1, \
            f"期望 defer 内同卡多任务合并为 1 次落盘，实际 {q.write_count - before}"
        # 数据可读且任务齐全
        card = mgr.get_card("s1")
        assert len(card.tasks) == 8


# ==== 修复回归：HandoffBus 重复交接不堆积 ====

def test_handoff_same_state_card_no_dup_task():
    """复用同一稳定 StateCard 时，交接任务不重复堆积，
    且**后到者 context 静默丢弃**（共享卡既定语义，🟡4 断言固化）。"""
    import json

    from secretary.handoff import HandoffBus
    with tempfile.TemporaryDirectory() as tmpdir:
        # 独立 HandoffBus 显式 data_dir 时，ho-* 状态卡收敛到
        # <parent(data_dir)>/state（此处即 <tmpdir>/state），无需 chdir。
        bus = HandoffBus(data_dir=os.path.join(tmpdir, "handoffs"))
        c1 = bus.create_handoff("ua", "ub", "同意图", "首到者上下文",
                                auto_state_card=True)
        c2 = bus.create_handoff("ua", "ub", "同意图", "后到者上下文",
                                auto_state_card=True)
        assert c2.state_card_id == c1.state_card_id  # 复用同一会话卡
        # ho-* 状态卡随 data_dir 父目录推导的 state_root 落盘
        mgr = StateManager(data_dir=os.path.join(bus.state_root, "state"))
        card = mgr.get_card(c1.state_card_id)
        assert card is not None, "ho-* 状态卡应落 state_root/state 可读"
        titles = [t.title for t in card.tasks]
        assert len(card.tasks) == 1, f"重复 add_task 未去重: {titles}"
        assert titles[0].startswith("交接:")

        # 🟡4 断言固化：共享卡只按标题去重，后到者 context 不落卡。
        # 判别性：若实现改为"追加 context"或"为后到者另建任务"，
        # 首到者摘要断言与全量序列化断言都会失败。
        assert card.tasks[0].summary == "首到者上下文", \
            f"首到者 context 应落卡，实际 {card.tasks[0].summary!r}"
        blob = json.dumps(card.to_dict(), ensure_ascii=False)
        assert "后到者上下文" not in blob, \
            "后到者 context 不得落共享卡（静默丢弃语义）"
        # 重开会话读回仍一致（语义随卡落盘持久化，而非仅内存态）
        card2 = StateManager(
            data_dir=os.path.join(bus.state_root, "state")
        ).get_card(c1.state_card_id)
        assert len(card2.tasks) == 1
        assert "后到者上下文" not in json.dumps(
            card2.to_dict(), ensure_ascii=False)


# ==== 修复回归：submit_chain_up 断链记录可安全展示 ====

def test_submit_chain_level_skipped_displayable():
    """中间层缺卡时返回 level_skipped 记录，展示逻辑(取 from_session)不抛 KeyError"""
    with tempfile.TemporaryDirectory() as tmpdir:
        mgr = StateManager(data_dir=tmpdir)
        mgr.create_card("root-s", objective="全局")
        mgr.create_card("leaf-s", objective="编码")   # child-s 无卡 → 触发断链
        tree = SecretaryTree(data_dir=tmpdir, tree_name="chain")
        tree.register_node("root-s")
        tree.register_node("child-s")
        tree.register_node("leaf-s")
        tree.link("root-s", "child-s")
        tree.link("child-s", "leaf-s")

        submitter = HierarchySubmitter(data_dir=tmpdir)
        subs = submitter.submit_chain_up(mgr.get_card("leaf-s"), tree,
                                         state_manager=mgr)
        # 至少含一笔 level_skipped
        skipped = [s for s in subs if s.get("level_skipped")]
        assert skipped, "应产生 level_skipped 断链记录"

        # 模拟 CLI 展示逻辑：断链走 .get 分支，正常提交走 from_session
        def render(s):
            if s.get("level_skipped"):
                return f"skip {s.get('session')}->{s.get('parent_session')}"
            return f"ok {s['from_session']}->{s['to_session']}"

        for s in subs:
            render(s)  # 不抛 KeyError


# ==== 导出 ====

def test_flush_queue_exported():
    import secretary.state as st
    assert hasattr(st, "FlushQueue")
    assert hasattr(st, "DeferredWriter")


if __name__ == "__main__":
    tests = [
        test_flush_queue_coalesce_same_key,
        test_flush_queue_batch_multi_resource,
        test_deferred_writer_context,
        test_tree_deferred_batch_single_write,
        test_state_manager_deferred_batch,
        test_handoff_same_state_card_no_dup_task,
        test_submit_chain_level_skipped_displayable,
        test_flush_queue_exported,
    ]
    passed = 0
    failed = []
    for t in tests:
        try:
            t()
            print(f"  ✅ {t.__name__}")
            passed += 1
        except Exception as e:
            print(f"  ❌ {t.__name__}: {e}")
            import traceback
            traceback.print_exc()
            failed.append(t.__name__)
    print(f"\n通过 {passed}/{len(tests)} 个测试")
    if failed:
        print(f"失败: {failed}")
        sys.exit(1)
    print("✅ 批量/延迟落盘 + 评审修复回归测试全部通过")
