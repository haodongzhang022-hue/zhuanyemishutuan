"""M9 状态管理层测试"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../src"))

from secretary.state import (
    StateManager, FoldEngine, HierarchySubmitter,
    StateCard, TaskRecord, TaskState, TaskType, FoldState,
)


def test_create_and_get_state_card():
    """创建并读取 StateCard"""
    with tempfile.TemporaryDirectory() as tmpdir:
        mgr = StateManager(data_dir=tmpdir)
        card = mgr.create_card(
            session_id="sess-1",
            objective="实现M9状态管理",
            unit_id="dongjiagoushi",
            role_name="secretary",
        )
        assert card is not None
        assert card.session_id == "sess-1"
        assert card.objective == "实现M9状态管理"
        assert card.id is not None

        # 重新读取
        fetched = mgr.get_card("sess-1")
        assert fetched is not None
        assert fetched.session_id == "sess-1"
        assert fetched.objective == "实现M9状态管理"


def test_add_and_update_task():
    """添加和更新任务"""
    with tempfile.TemporaryDirectory() as tmpdir:
        mgr = StateManager(data_dir=tmpdir)
        card = mgr.create_card("sess-2", objective="测试任务管理")

        # 添加普通任务
        task = mgr.add_task(
            session_id="sess-2",
            title="实现状态模型",
            files=["models.py"],
            tools=["python3"],
        )
        assert task is not None
        assert task.task_id == "sess-2-0001"
        assert task.fold == FoldState.COLLAPSED.value  # 普通任务默认折叠

        # 添加交付任务
        task2 = mgr.add_task(
            session_id="sess-2",
            title="交付完整模块",
            task_type=TaskType.DELIVERABLE.value,
            files=["__init__.py"],
        )
        assert task2 is not None
        assert task2.fold == FoldState.EXPANDED.value  # 交付任务默认展开

        # 更新状态
        ok = mgr.update_task_state("sess-2", task.task_id, TaskState.DONE.value)
        assert ok is True

        # 非法状态迁移（done → in_progress 不允许）
        ok2 = mgr.update_task_state("sess-2", task.task_id, TaskState.IN_PROGRESS.value)
        assert ok2 is False

        # 验证更新结果
        card2 = mgr.get_card("sess-2")
        assert card2 is not None
        assert card2.get_task(task.task_id).state == TaskState.DONE.value


def test_fold_engine_default_folding():
    """折叠引擎：默认恒折叠非交付任务"""
    with tempfile.TemporaryDirectory() as tmpdir:
        mgr = StateManager(data_dir=tmpdir)
        mgr.create_card("sess-3", objective="折叠测试")

        # 添加三个任务：两个普通 + 一个交付
        t1 = mgr.add_task("sess-3", "任务A", task_type=TaskType.NORMAL.value)
        t2 = mgr.add_task("sess-3", "任务B", task_type=TaskType.NORMAL.value)
        t3 = mgr.add_task("sess-3", "交付C", task_type=TaskType.DELIVERABLE.value)

        # 手动更新 t1 为 in_progress
        mgr.update_task_state("sess-3", t2.task_id, TaskState.IN_PROGRESS.value)

        # 执行折叠策略
        engine = FoldEngine("sess-3")
        card = mgr.get_card("sess-3")
        card = engine.auto_fold_policy(card)
        mgr.save_card(card)

        # 验证：交付任务 + 当前执行任务展开，其他折叠
        expanded = mgr.get_expanded_tasks("sess-3")
        collapsed = mgr.get_collapsed_tasks("sess-3")

        expanded_ids = {t.task_id for t in expanded}
        collapsed_ids = {t.task_id for t in collapsed}

        assert t3.task_id in expanded_ids  # 交付任务展开
        assert t2.task_id in expanded_ids  # 当前执行任务展开
        assert t1.task_id in collapsed_ids  # 普通任务折叠

        assert len(expanded) == 2
        assert len(collapsed) == 1


def test_compact_context_no_full_content():
    """压缩上下文不含原文全量"""
    with tempfile.TemporaryDirectory() as tmpdir:
        mgr = StateManager(data_dir=tmpdir)
        mgr.create_card("sess-4", objective="上下文压缩")

        t1 = mgr.add_task("sess-4", "调研任务", files=["research.md"],
                          content_ref="sess-4-seg001")
        t2 = mgr.add_task("sess-4", "执行任务", files=["impl.py"])
        mgr.update_task_state("sess-4", t2.task_id, TaskState.IN_PROGRESS.value)
        t3 = mgr.add_task("sess-4", "交付任务", task_type=TaskType.DELIVERABLE.value)

        # 执行折叠策略
        engine = FoldEngine("sess-4")
        card = mgr.get_card("sess-4")
        card = engine.auto_fold_policy(card)
        mgr.save_card(card)

        ctx = engine.compact_context(card)
        # 展开的任务应携带要素信息
        assert "sess-4-0002" in ctx  # 当前执行任务
        assert "sess-4-0003" in ctx  # 交付任务

        # 折叠任务只显示编号和引用，不含正文
        assert "sess-4-0001" in ctx
        assert "调研任务" not in ctx  # 折叠任务不携带标题正文（只带编号ref）

        # 引用编号应存在（可检索）
        assert "sess-4-seg001" in ctx or "ref" in ctx


def test_hierarchy_submit():
    """层级向上提交：只传汇总不传全量"""
    with tempfile.TemporaryDirectory() as tmpdir:
        mgr = StateManager(data_dir=tmpdir)
        mgr.create_card("sess-5", objective="向下级提交测试")
        mgr.add_task("sess-5", "任务1")
        mgr.add_task("sess-5", "任务2")

        mgr.update_task_state("sess-5", "sess-5-0001", TaskState.DONE.value)

        submitter = HierarchySubmitter(data_dir=tmpdir)
        card = mgr.get_card("sess-5")  # 重新获取最新状态
        sub = submitter.submit_up(
            card, upstream_session="parent-session"
        )
        assert sub["from_session"] == "sess-5"
        assert sub["to_session"] == "parent-session"
        payload = sub["payload"]
        assert payload["task_count"] == 2
        assert payload["done_count"] == 1

        # 检查提交记录
        records = submitter.list_submissions()
        assert len(records) == 1


def test_fold_content_with_content_meta():
    """折叠原文并带元数据"""
    content = """第一段任务内容

第二段任务内容

最终交付内容"""

    engine = FoldEngine("sess-6", unit_id="dongjiagoushi")
    card = engine.fold_content(
        content,
        content_meta={
            "objective": "将内容折叠为任务",
            "files": ["docs/file.md"],
            "tools": ["python3"],
        },
    )
    assert len(card.tasks) == 3
    # 最后一段是交付任务且展开
    last = card.tasks[-1]
    assert last.task_type == TaskType.DELIVERABLE.value
    assert last.fold == FoldState.EXPANDED.value
    # 前面是普通任务且折叠
    assert card.tasks[0].fold == FoldState.COLLAPSED.value
    # 每个任务都有内容引用编号
    assert card.tasks[0].content_ref is not None




def test_session_id_path_traversal_guard():
    """安全：非法 session_id 不得越出 data/state/ 目录"""
    with tempfile.TemporaryDirectory() as tmpdir:
        mgr = StateManager(data_dir=tmpdir)

        # 非法字符应被拒绝
        for bad in ["../evil", "../../etc/passwd", "a/b", "a\\b", ".", ".."]:
            # create_card 应抛 ValueError
            try:
                mgr.create_card(session_id=bad, objective="x")
                assert False, f"session_id={bad!r} 应被拒绝"
            except ValueError:
                pass  # 预期异常

        # get_card 对非法 session_id 返回 None（不抛异常）
        assert mgr.get_card("../evil") is None
        assert mgr.delete_card("../evil") is False

        # 验证 tmpdir 中没有越界文件
        files = os.listdir(tmpdir)
        assert all(f.endswith(".json") for f in files if f != "submissions.jsonl")
        # 且没有写入到父目录
        parent_files = os.listdir(os.path.dirname(tmpdir))
        for f in parent_files:
            assert "evil" not in f and "passwd" not in f


def test_from_dict_forward_compatibility():
    """前向兼容：from_dict 遇到未知字段/缺字段不应崩溃"""
    # 缺少字段（旧数据没有新字段）
    card = StateCard.from_dict({
        "session_id": "old-card",
        "objective": "旧卡",
    })
    assert card.session_id == "old-card"
    assert card.objective == "旧卡"
    assert card.tasks == []

    # 包含未知新字段（未来版本字段）也应能解析
    card2 = StateCard.from_dict({
        "session_id": "future-card",
        "objective": "未来卡",
        "some_future_field": "value",
        "tasks": [
            {"task_id": "t1", "title": "任务1", "some_new_field": "x"},
            {"task_id": "t2"},  # 缺少 title，应使用默认值
            "not-a-dict",  # 损坏数据，应跳过
        ],
    })
    assert card2.session_id == "future-card"
    assert len(card2.tasks) == 2  # 两条有效任务被恢复
    assert card2.tasks[0].title == "任务1"
    assert card2.tasks[1].title == ""  # 缺失 title 用默认值
    assert card2.tasks[1].task_id == "t2"

    # 非 dict 输入也不应崩溃
    task = TaskRecord.from_dict(None)
    assert task.task_id == ""
    assert task.title == ""


def test_save_card_public_api():
    """公开 save_card 方法可用"""
    with tempfile.TemporaryDirectory() as tmpdir:
        mgr = StateManager(data_dir=tmpdir)
        card = mgr.create_card("sess-7", objective="公开API测试")
        # 直接再保存一次
        mgr.save_card(card)
        fetched = mgr.get_card("sess-7")
        assert fetched is not None
        assert fetched.objective == "公开API测试"


def test_fold_search_tasks():
    """M8 跨会话任务检索: FoldEngine.search_tasks 跨卡搜索"""
    with tempfile.TemporaryDirectory() as tmpdir:
        mgr = StateManager(data_dir=tmpdir)
        # 创建两张卡
        mgr.create_card("sess-search-1", objective="搜索测试行业架构")
        mgr.add_task("sess-search-1", "处理用户输入需求", task_type="normal")
        mgr.create_card("sess-search-2", objective="游戏开发单元")
        mgr.add_task("sess-search-2", "生成游戏逻辑代码", task_type="normal")

        engine = FoldEngine("")
        cards = mgr.list_cards()

        # 按 keyword 搜索
        results = engine.search_tasks(cards, "用户")
        assert len(results) >= 1
        assert any(r["session_id"] == "sess-search-1" for r in results)
        assert any(r["session_id"] == "sess-search-2" for r in results) is False

        results2 = engine.search_tasks(cards, "游戏")
        assert len(results2) >= 1
        assert any(r["session_id"] == "sess-search-2" for r in results2)

        # 跨会话搜索所有状态卡（便捷入口，指向同一数据目录）
        results3 = engine.search_all_states("游戏", data_dir=tmpdir)
        assert len(results3) >= 1
        assert any(r["session_id"] == "sess-search-2" for r in results3)


def test_handoff_completeness_and_standard():
    """M5xM9 交接卡标准字段完整度: completeness_check / create_standard_handoff"""
    from secretary.handoff import HandoffBus
    from secretary.handoff.models import HandoffCard

    with tempfile.TemporaryDirectory() as tmpdir:
        bus = HandoffBus(data_dir=tmpdir)

        # 1. 不完整卡 → completeness_check 应报缺失字段
        incomplete = HandoffCard(
            source_unit="", target_unit="target-x", intent="",
        )
        check = incomplete.completeness_check()
        assert check["passed"] is False
        assert "source_unit" in check["missing"]
        assert "intent" in check["missing"]
        assert "state_card_id" in check["missing"]

        # 2. create_standard_handoff 自动补全 → 校验通过
        std = bus.create_standard_handoff(
            source_unit="dongjiagoushi",
            target_unit="game-dev-unit",
            intent="需要为游戏设计架构",
            context="完整上下文",
        )
        assert std is not None
        assert std.state_card_id is not None, "standard_handoff 应自动关联 StateCard"
        check2 = std.completeness_check()
        assert check2["passed"] is True, f"standard_handoff 应完整: {check2}"

        # 3. validate_card 委托给 completeness_check
        result = bus.validate_card(std)
        assert result["passed"] is True

        # 4. validate() 对不完整卡应抛 ValueError
        try:
            incomplete.validate()
            assert False, "validate() 对不完整卡应抛 ValueError"
        except ValueError:
            pass


def test_api_state_search():
    """API: FoldEngine.search_tasks 关键词检索验证"""
    from secretary.state.fold_engine import FoldEngine

    with tempfile.TemporaryDirectory() as tmpdir:
        mgr = StateManager(data_dir=tmpdir)
        mgr.create_card(
            session_id="api-srch-test",
            objective="测试搜索用行业目标",
            unit_id="dongjiagoushi",
            role_name="secretary",
        )
        mgr.add_task(
            session_id="api-srch-test",
            title="需要实现一个搜索功能",
            summary="包含关键词搜索的折叠任务",
        )
        card = mgr.get_card("api-srch-test")
        engine = FoldEngine(card.session_id)

        # 搜索关键词
        results = engine.search_tasks([card], "搜索")
        assert len(results) >= 1
        assert results[0]["title"] == "需要实现一个搜索功能"
if __name__ == "__main__":
    test_create_and_get_state_card()
    test_add_and_update_task()
    test_fold_engine_default_folding()
    test_compact_context_no_full_content()
    test_hierarchy_submit()
    test_fold_content_with_content_meta()
    test_session_id_path_traversal_guard()
    test_from_dict_forward_compatibility()
    test_save_card_public_api()
    test_fold_search_tasks()
    test_handoff_completeness_and_standard()
    test_cli_arb_and_state_search()
    print("✅ M9 状态管理层测试全部通过")
