"""SecretarySession 门面接口集成测试

覆盖 ISSUE-001 核心验收：
  US-1: 一会话一秘书（独立环境）
  US-2: 折叠 + 编号 + 可检索原文
  US-3: 向上提交汇总态（不含全文）
  US-4: 稳定前缀 + 缓存键恒定
"""
from __future__ import annotations

import os
import sys
import shutil
import tempfile

import pytest

sys.path.insert(0, os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from secretary.session import SecretarySession
from secretary.state import StateManager, TaskState


@pytest.fixture
def tmp_dir():
    """每个测试独立的临时数据目录"""
    d = tempfile.mkdtemp()
    yield d
    shutil.rmtree(d, ignore_errors=True)


# ------------------------------------------------------------------
# 会话生命周期
# ------------------------------------------------------------------

class TestSessionLifecycle:
    def test_create_and_get_card(self, tmp_dir):
        sec = SecretarySession("sess-lc-1", objective="生命周期测试",
                               data_dir=tmp_dir)
        card = sec.get_card()
        assert card is not None
        assert card.session_id == "sess-lc-1"
        assert card.objective == "生命周期测试"
        sec.delete_session()

    def test_illegal_session_id_rejected(self, tmp_dir):
        with pytest.raises(ValueError):
            SecretarySession("bad/path/name", data_dir=tmp_dir)

    def test_delete_session(self, tmp_dir):
        sec = SecretarySession("sess-lc-2", data_dir=tmp_dir)
        assert sec.get_card() is not None
        assert sec.delete_session() is True
        assert sec.get_card() is None


# ------------------------------------------------------------------
# US-1: 一会话一秘书（独立环境）
# ------------------------------------------------------------------

class TestSessionIsolation:
    def test_independent_cards_no_share(self, tmp_dir):
        """两个不同会话秘书互不干扰"""
        sec_a = SecretarySession("isolation-a", objective="会话A",
                                 data_dir=tmp_dir)
        sec_b = SecretarySession("isolation-b", objective="会话B",
                                 data_dir=tmp_dir)

        sec_a.add_task("任务A1", task_type="deliverable")
        sec_a.add_task("任务A2")
        sec_b.add_task("任务B1", task_type="deliverable")

        assert len(sec_a.list_tasks()) == 2
        assert len(sec_b.list_tasks()) == 1
        assert sec_a.list_tasks()[0].title == "任务A1"
        assert sec_b.list_tasks()[0].title == "任务B1"

        # 清理
        sec_a.delete_session()
        sec_b.delete_session()


# ------------------------------------------------------------------
# US-2: 折叠 + 编号 + 可检索原文
# ------------------------------------------------------------------

class TestFoldAndRetrieve:
    def test_fold_content_generates_numbered_tasks(self, tmp_dir):
        sec = SecretarySession("fold-t1", objective="折叠测试",
                               data_dir=tmp_dir)
        content = "第一段内容说明。\n\n第二段内容说明。"
        sec.fold(content, meta={"objective": "折叠目标"})

        tasks = sec.list_tasks()
        assert len(tasks) == 2
        # 任务都有唯一编号
        ids = {t.task_id for t in tasks}
        assert len(ids) == 2
        # 折叠任务可检索（有 content_ref）
        refs = [t.content_ref for t in tasks if t.content_ref]
        assert len(refs) == 2
        # 编号格式正确
        assert tasks[0].task_id.startswith("fold-t1-")
        sec.delete_session()

    def test_retrieve_original_by_ref(self, tmp_dir):
        sec = SecretarySession("fold-r1", objective="检索测试",
                               data_dir=tmp_dir)
        content = "这是需要被折叠的原始文本。\n\n另一段要归档的内容。"
        sec.fold(content)

        tasks = sec.list_tasks()
        for t in tasks:
            if t.content_ref:
                original = sec.retrieve_original(t.content_ref)
                assert original is not None
                assert len(original) > 0
                # 原文与折叠前内容匹配
                assert "原始文本" in original or "另一段" in original
        sec.delete_session()

    def test_search_tasks(self, tmp_dir):
        sec = SecretarySession("fold-s1", objective="搜索测试",
                               data_dir=tmp_dir)
        sec.add_task("实现状态管理秘书", objective="状态管理开发")
        sec.add_task("设计缓存策略", objective="缓存优化")

        results = sec.search_tasks("状态管理")
        assert len(results) == 1
        assert results[0]["title"] == "实现状态管理秘书"
        sec.delete_session()

    def test_compact_context_excludes_stopped(self, tmp_dir):
        """归档任务不进入活动 prompt"""
        sec = SecretarySession("fold-c1", objective="压缩测试",
                               data_dir=tmp_dir)
        sec.add_task("已完成任务", task_type="deliverable")
        sec.add_task("未完成任务")

        tasks = sec.list_tasks()
        # 完成第一个任务
        sec.mark_task(tasks[0].task_id, "done")
        # 归档第一个任务
        sec.mark_task(tasks[0].task_id, "stopped")

        ctx = sec.get_compact_context()
        # stopped 任务不携带在 prompt 中
        assert "已完成任务" not in ctx
        sec.delete_session()


# ------------------------------------------------------------------
# US-3: 向上提交汇总态（不含全文）
# ------------------------------------------------------------------

class TestUpstreamSubmission:
    def test_summarize_for_upstream(self, tmp_dir):
        sec = SecretarySession("sub-u1", objective="提交测试",
                               data_dir=tmp_dir)
        sec.add_task("任务1", task_type="deliverable")
        sec.add_task("任务2")

        summary = sec.summarize_for_upstream()
        assert summary["task_count"] == 2
        assert summary["done_count"] == 0
        assert summary["todo_count"] == 2
        # 不含原文全文（只含结构化摘要字段）
        assert "content_refs" in summary  # 索引而非原文
        assert "tasks" in summary
        for t in summary["tasks"]:
            assert "content_ref" in t  # 携带索引引用
            assert "title" in t        # 携带标题
        assert isinstance(summary["content_refs"], list)
        sec.delete_session()

    def test_progress_stats(self, tmp_dir):
        sec = SecretarySession("sub-s1", objective="进度测试",
                               data_dir=tmp_dir)
        sec.add_task("A", task_type="deliverable")
        sec.add_task("B")
        sec.add_task("C")

        tasks = sec.list_tasks()
        sec.mark_task(tasks[0].task_id, "done")
        sec.mark_task(tasks[1].task_id, "in_progress")
        sec.mark_task(tasks[2].task_id, "blocked")

        stats = sec.progress_stats()
        assert stats["done"] == 1
        assert stats["in_progress"] == 1
        assert stats["blocked"] == 1
        assert stats["todo"] == 0
        sec.delete_session()


# ------------------------------------------------------------------
# US-4: 稳定前缀 + 缓存命中保障
# ------------------------------------------------------------------

class TestCacheStability:
    def test_cache_key_stable_across_turns(self, tmp_dir):
        """同一会话多次装配 → 缓存键不变"""
        sec = SecretarySession("cache-s1", objective="缓存测试",
                               data_dir=tmp_dir,
                               static_prefix="[秘书] 你是会话秘书，管理状态。")
        sec.add_task("任务A", task_type="deliverable")

        # 多次装配，缓存键应当一致
        r1 = sec.assemble_context(user_text="查询进度")
        r2 = sec.assemble_context(user_text="继续开发")
        r3 = sec.assemble_context(user_text="帮我看看任务")

        assert r1["cache_key"] == r2["cache_key"]
        assert r2["cache_key"] == r3["cache_key"]

        # layer_a 应当稳定不变（不含动态内容）
        assert r1["layer_a"] == r2["layer_a"]
        assert r2["layer_a"] == r3["layer_a"]
        sec.delete_session()

    def test_different_sessions_different_cache_keys(self, tmp_dir):
        """不同会话秘书 → 缓存键不同（环境隔离）"""
        sec_a = SecretarySession("cache-a", objective="A",
                                 data_dir=tmp_dir,
                                 static_prefix="[秘书A]")
        sec_b = SecretarySession("cache-b", objective="B",
                                 data_dir=tmp_dir,
                                 static_prefix="[秘书B]")

        r_a = sec_a.assemble_context(user_text="你好")
        r_b = sec_b.assemble_context(user_text="你好")

        assert r_a["cache_key"] != r_b["cache_key"]
        sec_a.delete_session()
        sec_b.delete_session()

    def test_static_prefix_never_mixed_with_dynamic(self, tmp_dir):
        """层A不含动态内容标记，保证静态隔离"""
        sec = SecretarySession("cache-s2", objective="隔离测试",
                               data_dir=tmp_dir,
                               static_prefix="[秘书] 角色定义")
        sec.add_task("做开发", task_type="deliverable")

        result = sec.assemble_context(user_text="今天写代码")
        layer_a = result["layer_a"]

        # 层A不应包含动态层标记
        assert "会话态上下文" not in layer_a
        assert "规则注入" not in layer_a
        sec.delete_session()


# ------------------------------------------------------------------
# M5 交接集成
# ------------------------------------------------------------------

class TestHandoffIntegration:
    def test_create_handoff_with_state_card(self, tmp_dir):
        sec = SecretarySession("handoff-1", objective="交接测试",
                               data_dir=tmp_dir)
        ho = sec.create_handoff(
            source_unit="team-a",
            target_unit="team-b",
            intent="完成跨团队任务",
            context="需要接力开发",
        )
        assert ho is not None
        assert ho.source_unit == "team-a"
        assert ho.target_unit == "team-b"
        # 自动关联 StateCard
        assert ho.state_card_id is not None
        sec.delete_session()


# ------------------------------------------------------------------
# 任务状态流转
# ------------------------------------------------------------------

class TestTaskStateMachine:
    def test_valid_state_transitions(self, tmp_dir):
        sec = SecretarySession("state-1", objective="状态机测试",
                               data_dir=tmp_dir)
        sec.add_task("开发功能", task_type="deliverable")
        task = sec.list_tasks()[0]

        # todo → in_progress → done
        assert sec.mark_task(task.task_id, "in_progress") is True
        assert sec.mark_task(task.task_id, "done") is True
        # done → stopped (归档)
        assert sec.mark_task(task.task_id, "stopped") is True
        sec.delete_session()

    def test_invalid_transition_rejected(self, tmp_dir):
        sec = SecretarySession("state-2", objective="状态测试",
                               data_dir=tmp_dir)
        sec.add_task("开发", task_type="deliverable")
        task = sec.list_tasks()[0]

        # in_progress → todo 不合法（需阻塞/完成/停止）
        assert sec.mark_task(task.task_id, "in_progress") is True
        # todo 不能从 in_progress 回退（非法迁移应被拒绝）
        assert sec.mark_task(task.task_id, "todo") is False
        sec.delete_session()
