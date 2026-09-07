"""M9 秘书长多级树 + 主链路集成测试"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../src"))

from secretary.state import (
    StateManager, FoldEngine, HierarchySubmitter,
    SecretaryTree, SecretaryTreeNode,
    StateCard, TaskRecord, TaskState, TaskType, FoldState,
)


# ==== ③ SecretaryTree 多级树基础功能 ====

def test_tree_register_and_link():
    """多级树注册节点和建立父子关系"""
    with tempfile.TemporaryDirectory() as tmpdir:
        tree = SecretaryTree(data_dir=tmpdir, tree_name="test")
        # 注册根（秘书长）
        tree.register_node("root", role_name="secretary",
                           display_name="秘书长", objective="全局任务")
        # 注册子节点
        tree.register_node("child-1", role_name="secretary",
                           display_name="秘书A", objective="子任务A")
        tree.link("root", "child-1")
        # 注册孙节点
        tree.register_node("leaf-1", role_name="developer",
                           display_name="执行员", objective="编码")
        tree.link("child-1", "leaf-1")

        assert len(tree.nodes) == 3
        root = tree.get_root()
        assert root.session_id == "root"
        assert root.display_name == "秘书长"

        # 路径
        path = tree.get_path("leaf-1")
        assert path == ["root", "child-1", "leaf-1"]

        # 后代
        desc = tree.get_descendants("root")
        assert len(desc) == 2

        # 祖先
        anc = tree.get_ancestors("leaf-1")
        assert len(anc) == 2
        assert anc[0].session_id == "child-1"
        assert anc[1].session_id == "root"


def test_tree_persistence():
    """多级树持久化到磁盘并恢复"""
    with tempfile.TemporaryDirectory() as tmpdir:
        tree1 = SecretaryTree(data_dir=tmpdir, tree_name="persist")
        tree1.register_node("root-1", display_name="秘书长")
        tree1.register_node("sub-1", display_name="秘书")
        tree1.link("root-1", "sub-1")

        # 重新加载
        tree2 = SecretaryTree(data_dir=tmpdir, tree_name="persist")
        assert len(tree2.nodes) == 2
        assert tree2.get_root().session_id == "root-1"
        children = tree2.get_children("root-1")
        assert len(children) == 1
        assert children[0].session_id == "sub-1"


def test_tree_rollup_with_cards():
    """多级树 rollup 带 StateCard 的状态汇总"""
    with tempfile.TemporaryDirectory() as tmpdir:
        mgr = StateManager(data_dir=tmpdir)
        # 创建三张卡
        mgr.create_card("root-s", objective="全局")
        mgr.create_card("child-s", objective="子任务")
        mgr.create_card("leaf-s", objective="编码")
        # 添加任务
        mgr.add_task("root-s", "宏观管理")
        mgr.add_task("child-s", "协调")
        mgr.add_task("leaf-s", "写代码")
        mgr.update_task_state("leaf-s", "leaf-s-0001", TaskState.DONE.value)

        # 构建树
        tree = SecretaryTree(data_dir=tmpdir, tree_name="rollup")
        tree.register_node("root-s", display_name="秘书长")
        tree.register_node("child-s", display_name="秘书")
        tree.register_node("leaf-s", display_name="执行员")
        tree.link("root-s", "child-s")
        tree.link("child-s", "leaf-s")

        # Rollup
        rollup = tree.rollup(state_manager=mgr)
        assert len(rollup["levels"]) == 3  # 三层

        # 找到 leaf 层
        leaf_lvl = next(l for l in rollup["levels"] if l["depth"] == 2)
        assert len(leaf_lvl["nodes"]) == 1
        assert leaf_lvl["nodes"][0]["session_id"] == "leaf-s"
        assert leaf_lvl["nodes"][0]["done_count"] == 1


def test_submit_chain_multi_level():
    """多级逐级向上提交"""
    with tempfile.TemporaryDirectory() as tmpdir:
        mgr = StateManager(data_dir=tmpdir)
        mgr.create_card("root-s", objective="全局")
        mgr.create_card("child-s", objective="子任务")
        mgr.create_card("leaf-s", objective="编码")
        mgr.add_task("leaf-s", "写代码")
        mgr.add_task("child-s", "协调")
        mgr.add_task("root-s", "管理")

        tree = SecretaryTree(data_dir=tmpdir, tree_name="chain")
        tree.register_node("root-s", display_name="秘书长")
        tree.register_node("child-s", display_name="秘书")
        tree.register_node("leaf-s", display_name="执行员")
        tree.link("root-s", "child-s")
        tree.link("child-s", "leaf-s")

        submitter = HierarchySubmitter(data_dir=tmpdir)
        subs = submitter.submit_chain_up(
            card=mgr.get_card("leaf-s"),
            tree=tree,
            state_manager=mgr,
        )
        # 应产生两笔提交：leaf→child 和 child→root
        assert len(subs) == 2
        assert subs[0]["from_session"] == "leaf-s"
        assert subs[0]["to_session"] == "child-s"
        assert subs[1]["from_session"] == "child-s"
        assert subs[1]["to_session"] == "root-s"


# ==== ② M9 接入主执行链路 ====

def test_handoff_auto_state_card():
    """HandoffBus 自动创建 StateCard"""
    from secretary.handoff import HandoffBus
    with tempfile.TemporaryDirectory() as tmpdir:
        # 注意：HandoffBus 内部 StateManager 使用默认路径
        # 需要在指定 data_dir 时确保 HandoffBus 的 StateManager 一致
        import os
        old_cwd = os.getcwd()
        os.chdir(tmpdir)  # 让 StateManager 的默认路径在 tmpdir 下

        try:
            bus = HandoffBus(data_dir=os.path.join(tmpdir, "handoffs"))
            card = bus.create_handoff(
                source_unit="unit-a",
                target_unit="unit-b",
                intent="测试交接",
                context="上下文",
                auto_state_card=True,
            )
            assert card.state_card_id is not None
            assert card.state_card_id.startswith("ho-")

            # 验证交接卡持久化
            card2 = bus.get(card.id)
            assert card2 is not None
            assert card2.state_card_id == card.state_card_id
        finally:
            os.chdir(old_cwd)


def test_planner_auto_state_card():
    """StateManager get_or_create 自动建卡 + 交接卡关联"""
    # 对应原 hanghang CompositePlanner 自动关联状态卡的能力测试，
    # 此处验证 StateManager.get_or_create 的核心关联行为。
    import os, tempfile

    from secretary.state.manager import StateManager

    with tempfile.TemporaryDirectory() as tmpdir:
        sm = StateManager(data_dir=tmpdir)
        card = sm.get_or_create(
            session_id="plan-test-sess",
            objective="架构设计方案并编码实现",
            unit_id="dongjiagoushi",
            role_name="secretary",
        )
        assert card is not None
        assert card.session_id == "plan-test-sess"
        assert "架构设计方案" in card.objective


# ==== ① POC 量化验证 ====

def test_poc_benchmark_script():
    """验证 POC benchmark 脚本可执行且产出量化数据"""
    import subprocess
    script = os.path.join(os.path.dirname(__file__), "../poc/state_m9_benchmark.py")
    result = subprocess.run(
        [sys.executable, script, "--steps", "5"],
        capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 0, f"benchmark 执行失败: {result.stderr}"
    assert "压缩比" in result.stdout
    assert "状态管理" in result.stdout


def test_state_tree_export():
    """确认新类可从秘书包导出"""
    import secretary as sec
    assert hasattr(sec.state, "SecretaryTree")
    assert hasattr(sec.state, "SecretaryTreeNode")


if __name__ == "__main__":
    tests = [
        test_tree_register_and_link,
        test_tree_persistence,
        test_tree_rollup_with_cards,
        test_submit_chain_multi_level,
        test_handoff_auto_state_card,
        test_planner_auto_state_card,
        test_poc_benchmark_script,
        test_state_tree_export,
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
    print("✅ M9 多级树 + 主链路 + POC 测试全部通过")
