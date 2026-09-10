"""SecretarySession 门面接口集成测试

覆盖 ISSUE-001 核心验收：
  US-1: 一会话一秘书（独立环境）
  US-2: 折叠 + 编号 + 可检索原文
  US-3: 向上提交汇总态（不含全文）
  US-4: 稳定前缀 + 缓存键恒定
"""
from __future__ import annotations

import os
import shutil
import tempfile
import threading

import pytest

from secretary.session import SecretarySession
from secretary.handoff import HandoffBus


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


# ------------------------------------------------------------------
# 评审回归：fold 续号不覆盖（🔴 #1）
# ------------------------------------------------------------------

class TestFoldContinuationRegression:
    def test_multi_fold_refs_do_not_overwrite(self, tmp_dir):
        """同一会话连续两次 fold，第二次的 content_ref 续号且不覆盖第一次。

        评审指出的数据正确性缺陷：重复 fold 导致 content_ref 错位覆盖，
        第二轮任务拿到第一轮的引用。本用例回归验证。
        """
        sec = SecretarySession("fold-reg1", objective="续号回归",
                               data_dir=tmp_dir)
        sec.fold("第一轮任务A\n\n第一轮任务B")
        first_tasks = sec.list_tasks()
        first_refs = {t.task_id: t.content_ref for t in first_tasks}

        # 再折叠一轮
        sec.fold("第二轮任务C\n\n第二轮任务D")
        all_tasks = sec.list_tasks()

        # 两次任务的 content_ref 均唯一且不重复
        refs = [t.content_ref for t in all_tasks]
        assert len(refs) == len(set(refs)), "content_ref 不应重复"

        # 第一次的任务 ref 仍是第一轮的 seg001/seg002
        first_tasks_after = sec.list_tasks()
        for t in first_tasks_after[:2]:
            assert t.content_ref == first_refs[t.task_id]

        # 每条任务的 ref 可精确找回各自原文
        for t in all_tasks:
            orig = sec.retrieve_original(t.content_ref)
            assert orig is not None
        # 第一轮 ref 找回第一轮原文
        assert "第一轮任务A" in sec.retrieve_original(first_tasks[0].content_ref)
        assert "第二轮任务C" in sec.retrieve_original(all_tasks[2].content_ref)
        sec.delete_session()


# ------------------------------------------------------------------
# 评审建议回归：add_task summary 透传（🟡 #2）
# ------------------------------------------------------------------

class TestAddTaskSummary:
    def test_summary_passthrough(self, tmp_dir):
        """add_task 的 summary 参数应透传到 TaskRecord，不被静默吞掉。"""
        sec = SecretarySession("sum-reg1", data_dir=tmp_dir)
        t = sec.add_task("任务A", summary="这是折叠摘要")
        assert t.summary == "这是折叠摘要"
        sec.delete_session()


# ------------------------------------------------------------------
# 评审建议回归：层B携带任务标题/折叠引用（🟡 #3）
# ------------------------------------------------------------------

class TestLayerBNavigationContent:
    def test_layer_b_carries_task_navigation(self, tmp_dir):
        """assemble_context 的 layer_b 应携带任务标题/折叠引用，
        而非只落到"目标/进度/会话"降级摘要。"""
        sec = SecretarySession("layerb-1", objective="导航测试",
                               data_dir=tmp_dir)
        sec.add_task("做开发任务", task_type="deliverable", objective="开发")
        sec.add_task("设计缓存任务", objective="缓存")
        res = sec.assemble_context(user_text="查询")
        layer_b = res["layer_b"]
        # 展开任务标题应出现
        assert "做开发任务" in layer_b
        # 任务编号/折叠引用应出现（可驱动导航）
        assert "layerb-1-0001" in layer_b
        assert "layerb-1-0002" in layer_b
        sec.delete_session()


# ------------------------------------------------------------------
# 评审建议回归：retrieve_original 非法入参不抛裸异常（🟡 #4）
# ------------------------------------------------------------------

class TestRetrieveOriginalGuard:
    def test_invalid_ref_returns_none(self, tmp_dir):
        """retrieve_original 对 None/空串/非法字符/不存在引用返回 None，
        不抛裸 TypeError/ValueError。"""
        sec = SecretarySession("retr-reg1", data_dir=tmp_dir)
        assert sec.retrieve_original(None) is None
        assert sec.retrieve_original("") is None
        assert sec.retrieve_original("bad/../path") is None
        assert sec.retrieve_original("nonexist-seg001") is None
        sec.delete_session()


# ------------------------------------------------------------------
# 评审建议回归：数据根收敛（🟡 #5）+ 删除清理（🟡 #6）
# ------------------------------------------------------------------

class TestDataRootAndCleanup:
    def test_single_data_root(self, tmp_dir):
        """状态卡/原文归档/交接卡应收敛到同一数据根下，不再三处割裂。"""
        sec = SecretarySession("root-reg1", objective="根收敛",
                               data_dir=tmp_dir)
        root = os.path.abspath(tmp_dir)
        assert os.path.abspath(sec.state_manager.data_dir).startswith(root)
        assert os.path.abspath(sec.archive.root_dir).startswith(root)
        assert os.path.abspath(sec.handoff_bus.data_dir).startswith(root)
        sec.delete_session()

    def test_data_root_has_no_double_data(self, tmp_dir):
        """显式传 data_dir 时，不应产生 <cwd>/data/data/… 双嵌套。"""
        sec = SecretarySession("root-reg2", data_dir=tmp_dir)
        # 三个数据目录都直接在根下派生，无 "data/data" 嵌套
        for p in (sec.state_manager.data_dir,
                  sec.archive.root_dir, sec.handoff_bus.data_dir):
            norm = os.path.normpath(p)
            assert "data/data" not in norm, f"出现双 data 嵌套: {norm}"
        sec.delete_session()

    def test_delete_session_cleans_archive_and_handoff(self, tmp_dir):
        """delete_session 应清理状态卡 + 原文归档 + 本会话创建的交接卡。"""
        sec = SecretarySession("clean-reg1", data_dir=tmp_dir)
        sec.add_task("任务A", task_type="deliverable")
        sec.fold("要归档的原文\n\n第二段")
        sec.create_handoff("team-a", "team-b", "交接意图", "内容")

        # 删除前应存在归档与交接文件
        assert sec.archive.list_refs("clean-reg1"), "归档应有条目"
        assert os.path.exists(sec.handoff_bus.data_dir)

        sec.delete_session()

        # 状态卡、归档已清空
        assert sec.get_card() is None
        assert sec.archive.list_refs("clean-reg1") == []
        # 本会话创建的交接卡文件已删除
        assert not os.listdir(sec.handoff_bus.data_dir) or all(
            not f.endswith(".json") for f in os.listdir(sec.handoff_bus.data_dir))




# ------------------------------------------------------------------
# 评审回归：数据根收敛到交接关联卡（🔴 #A）+ delete_session 无残留（🔴 #B）
# ------------------------------------------------------------------

class TestHandoffStateCardConvergence:
    """🔴 #A：交接 auto_state_card 关联的 ho-* 状态卡应收敛到本会话数据根，
    且 delete_session 应一并回收，不再落 <cwd> 孤儿。"""

    def test_ho_state_card_lands_in_session_root(self, tmp_dir):
        """create_handoff(auto_state_card) 的 ho-* 卡落 <data_root>/state 而非 <cwd>。"""
        sec = SecretarySession("ho-root1", data_dir=tmp_dir)
        ho = sec.create_handoff("team-a", "team-b", "收敛意图", "上下文")
        assert ho is not None and ho.state_card_id

        state_dir = os.path.abspath(sec.state_manager.data_dir)
        # ho-* 卡在会话数据根下（而非 cwd/data/state 孤儿）
        assert os.path.exists(os.path.join(state_dir, f"{ho.state_card_id}.json"))
        cwd_state = os.path.abspath(os.path.join(os.getcwd(), "data", "state"))
        assert not os.path.exists(
            os.path.join(cwd_state, f"{ho.state_card_id}.json")), \
            "ho-* 状态卡不应落 <cwd>/data/state 孤儿"
        sec.delete_session()

    def test_delete_session_removes_ho_state_card(self, tmp_dir):
        """delete_session 应删除本会话关联的 ho-* 状态卡（含其 .lock）。"""
        sec = SecretarySession("ho-del1", data_dir=tmp_dir)
        ho = sec.create_handoff("unit-a", "unit-b", "待清理意图", "内容")
        sid = ho.state_card_id
        state_dir = sec.state_manager.data_dir
        assert os.path.exists(os.path.join(state_dir, f"{sid}.json"))

        sec.delete_session()
        assert not os.path.exists(os.path.join(state_dir, f"{sid}.json"))
        assert not os.path.exists(os.path.join(state_dir, f"{sid}.lock"))
        assert not os.path.exists(os.path.join(state_dir, f"{sid}.owner.json"))


class TestDeleteSessionNoResidue:
    """🔴 #B：delete_session 不应残留 .lock / 归档空目录 / 跨实例交接卡。"""

    def test_no_lock_and_no_empty_archive_dir_residue(self, tmp_dir):
        """删除后 <session>.lock 移除、content/<session>/ 空目录回收。"""
        sec = SecretarySession("residue-1", data_dir=tmp_dir)
        sec.add_task("任务", task_type="deliverable")
        sec.fold("归档原文一\n\n归档原文二")

        state_dir = sec.state_manager.data_dir
        archive_sdir = os.path.join(tmp_dir, "content", "residue-1")
        assert os.path.exists(os.path.join(state_dir, "residue-1.lock"))
        assert os.path.isdir(archive_sdir)

        sec.delete_session()
        assert not os.path.exists(os.path.join(state_dir, "residue-1.lock")), \
            ".lock 不应残留"
        assert not os.path.isdir(archive_sdir), "归档空目录不应残留"

    def test_cross_instance_delete_cleans_previous_handoff(self, tmp_dir):
        """上一实例创建的交接卡/ho-* 卡，本实例据台账重开后 delete_session 应能回收。"""
        # 实例1：创建交接卡后不删除即释放
        s1 = SecretarySession("xinst-1", data_dir=tmp_dir)
        ho1 = s1.create_handoff("src-x", "dst-x", "跨实例意图", "内容")
        handoff_id = ho1.id
        state_card_id = ho1.state_card_id
        ho_dir = s1.handoff_bus.data_dir
        state_dir = s1.state_manager.data_dir
        # 释放实例1（不调 delete_session，模拟中途退出/超时未清理）
        del s1

        # 文件确实存在于磁盘
        assert os.path.exists(os.path.join(ho_dir, f"{handoff_id}.json"))
        assert os.path.exists(os.path.join(state_dir, f"{state_card_id}.json"))

        # 实例2：以同一 session_id + 同一 data_dir 重开 → 应接管上一实例的归属
        s2 = SecretarySession("xinst-1", data_dir=tmp_dir)
        s2.delete_session()
        assert not os.path.exists(os.path.join(ho_dir, f"{handoff_id}.json")), \
            "跨实例交接卡不应残留"
        assert not os.path.exists(os.path.join(state_dir, f"{state_card_id}.json")), \
            "跨实例 ho-* 状态卡不应残留"

    def test_multi_session_no_cross_claim(self, tmp_dir):
        """同数据根下两个会话：删除 B 不得误删 A 的交接卡/ho-* 卡。"""
        sec_a = SecretarySession("multi-a", data_dir=tmp_dir)
        ho_a = sec_a.create_handoff("src-a", "dst-a", "A的意图", "ctx")
        sec_b = SecretarySession("multi-b", data_dir=tmp_dir)
        ho_dir = sec_b.handoff_bus.data_dir
        state_dir = sec_b.state_manager.data_dir

        # 删除 B → A 的数据不受影响
        sec_b.delete_session()
        assert os.path.exists(os.path.join(ho_dir, f"{ho_a.id}.json")), \
            "删除 B 不应误删 A 的交接卡"
        assert os.path.exists(os.path.join(state_dir, f"{ho_a.state_card_id}.json")), \
            "删除 B 不应误删 A 的 ho-* 状态卡"
        # B 自身状态卡已删
        assert not os.path.exists(os.path.join(state_dir, "multi-b.json"))
        sec_a.delete_session()


# ------------------------------------------------------------------
# P2-① 回归：共享 ho-* 卡跨会话误删（Issue #4）
# 共享 ho-* 卡 ID = hash(source,target,intent)，同源/同目标/同意图的多个
# 会话共享同一张卡。删 A 会话不得影响 B 会话持有的 ho-* 卡引用。
# ------------------------------------------------------------------

class TestSharedHoCardCrossSession:
    """P2-①：共享 ho-* 卡删除前须做存活引用检查，引用未清不得删。"""

    def _same_shared_ho(self, tmp_dir, sess_a, sess_b,
                        source="team-x", target="team-y",
                        intent="同一交接意图"):
        """两个会话创建同源/同目标/同意图的交接 → 共享同一张 ho-* 卡。"""
        sec_a = SecretarySession(sess_a, data_dir=tmp_dir)
        ho_a = sec_a.create_handoff(source, target, intent, "A上下文")
        sec_b = SecretarySession(sess_b, data_dir=tmp_dir)
        ho_b = sec_b.create_handoff(source, target, intent, "B上下文")
        assert ho_a.state_card_id == ho_b.state_card_id, \
            "同源/同目标/同意图应共享同一张 ho-* 卡"
        assert ho_a.id != ho_b.id, "交接卡本身各自独立"
        return sec_a, sec_b, ho_a, ho_b

    def test_delete_a_keeps_b_shared_ho_reference(self, tmp_dir):
        """删 A 会话：B 持有的共享 ho-* 卡与 handoff 卡引用均不受影响。"""
        sec_a, sec_b, ho_a, ho_b = self._same_shared_ho(
            tmp_dir, "sh-a1", "sh-b1")
        sid = ho_b.state_card_id
        state_dir = sec_b.state_manager.data_dir
        ho_dir = sec_b.handoff_bus.data_dir

        sec_a.delete_session()

        # B 持有的共享 ho-* 卡仍在，可读且任务仍在
        ho_path = os.path.join(state_dir, f"{sid}.json")
        assert os.path.exists(ho_path), \
            "删 A 后共享 ho-* 卡不应被删除（B 仍存活引用）"
        card = sec_b.state_manager.get_card(sid)
        assert card is not None, "B 仍能读到共享 ho-* 卡"
        assert card.tasks, "共享 ho-* 卡内交接任务应保留"
        # B 自身 handoff 卡仍在
        assert os.path.exists(os.path.join(ho_dir, f"{ho_b.id}.json")), \
            "B 持有的 handoff 卡不应被删"
        sec_b.delete_session()

    def test_last_referrer_delete_reclaims_ho_card(self, tmp_dir):
        """共享卡最后存活引用者删除后，ho-* 卡与交接卡一并回收（无孤儿）。"""
        sec_a, sec_b, ho_a, ho_b = self._same_shared_ho(
            tmp_dir, "sh-a2", "sh-b2")
        sid = ho_b.state_card_id
        state_dir = sec_b.state_manager.data_dir

        sec_a.delete_session()
        assert os.path.exists(os.path.join(state_dir, f"{sid}.json")), \
            "前置：A 删除后共享卡仍保留（B 引用中）"

        sec_b.delete_session()
        assert not os.path.exists(os.path.join(state_dir, f"{sid}.json")), \
            "最后引用者删除后共享 ho-* 卡应一并回收"
        assert not os.path.exists(os.path.join(state_dir, f"{sid}.lock"))
        assert not os.path.exists(os.path.join(state_dir, f"{sid}.owner.json"))

    def test_independent_ho_cards_not_blocked(self, tmp_dir):
        """各自独立意图（不同源）的 ho-* 卡互不干扰，删除仍回收。"""
        sec_a = SecretarySession("sh-a3", data_dir=tmp_dir)
        ho_a = sec_a.create_handoff("team-a1", "team-y", "意图A", "ctx")
        sec_b = SecretarySession("sh-b3", data_dir=tmp_dir)
        ho_b = sec_b.create_handoff("team-b1", "team-y", "意图B", "ctx")
        assert ho_a.state_card_id != ho_b.state_card_id

        state_dir = sec_b.state_manager.data_dir
        sec_a.delete_session()
        # A 的独立 ho-* 卡已回收；B 的卡不受影响
        assert not os.path.exists(
            os.path.join(state_dir, f"{ho_a.state_card_id}.json")), \
            "A 独占的 ho-* 卡应随 A 删除回收"
        assert os.path.exists(
            os.path.join(state_dir, f"{ho_b.state_card_id}.json")), \
            "B 的 ho-* 卡不受 A 删除影响"
        sec_b.delete_session()


# ------------------------------------------------------------------
# P2-③ 回归：delete_session 返回值语义统一（Issue #6）
# 与 StateManager.delete_card 对齐：返回「清理前本会话状态卡是否存在」。
# 状态卡缺失、仅剩伴生残留时同样清理完全但返回 False。
# ------------------------------------------------------------------

class TestDeleteSessionReturnSemantics:
    def test_delete_session_returns_true_when_card_existed(self, tmp_dir):
        """会话有卡：delete_session 返回 True，且全部伴生数据清理完成。"""
        sec = SecretarySession("delret-1", data_dir=tmp_dir)
        assert sec.get_card() is not None
        sec.add_task("任务A", task_type="deliverable")
        sec.fold("归档原文一\n\n归档原文二")
        sec.create_handoff("team-a", "team-b", "删除语义", "内容")

        ret = sec.delete_session()
        assert ret is True, "清理前状态卡存在时应返回 True"
        assert sec.get_card() is None
        assert sec.archive.list_refs("delret-1") == []

    def test_delete_session_returns_false_when_card_missing_residue(self, tmp_dir):
        """状态卡已缺、仅剩 .lock/.owner 残留时 delete_session 返回 False，
        且残留一并清理（与 delete_card 语义对齐，消除漂移）。"""
        sec = SecretarySession("delret-2", data_dir=tmp_dir)
        state_dir = sec.state_manager.data_dir
        # 保留 .lock/.owner.json 伴生残留，物理移除状态卡本体 → 卡已缺
        lock_path = os.path.join(state_dir, "delret-2.lock")
        owner_path = os.path.join(state_dir, "delret-2.owner.json")
        with open(lock_path, "w", encoding="utf-8") as f:
            f.write("")
        with open(owner_path, "w", encoding="utf-8") as f:
            f.write('{"owner": "secretary"}')
        assert os.path.exists(os.path.join(state_dir, "delret-2.json"))
        os.remove(os.path.join(state_dir, "delret-2.json"))

        ret = sec.delete_session()
        assert ret is False, "状态卡已缺失时应返回 False（不得因伴生残留返 True）"
        assert not os.path.exists(lock_path), ".lock 残留应被一并清理"
        assert not os.path.exists(owner_path), ".owner.json 残留应被一并清理"
        assert sec.get_card() is None


# ------------------------------------------------------------------
# P2-① 复评回归（Issue #4 / b0ac532 打回）：🔴#1–#3
# 🔴#1 判定与撤销自身引用顺序颠倒 → 并发下双跳过 → 共享卡孤儿（TOCTOU）
# 🔴#2 引用来源仅认台账 → 独立 HandoffBus 使用者的引用对扫描不可见
# 🔴#3 台账非 dict（如数组）→ 裸 AttributeError 中断 delete_session 并留残留
# ------------------------------------------------------------------

class TestSharedHoCardConcurrentReclaim:
    """🔴#1：撤销自身引用须前置到存活判定之前，并发/交错删除不留孤儿。"""

    @staticmethod
    def _barrier_aligned_round(tmp_root):
        """把两次存活判定用屏障对齐，令"判定先于撤销"的旧顺序必然双跳过。

        返回 (共享 ho-* 卡 ID, 状态卡目录, 删除线程异常列表)。
        """
        import threading

        sec_a = SecretarySession("cc-a", data_dir=tmp_root)
        ho_a = sec_a.create_handoff("team-x", "team-y", "并发同意图", "A ctx")
        sec_b = SecretarySession("cc-b", data_dir=tmp_root)
        ho_b = sec_b.create_handoff("team-x", "team-y", "并发同意图", "B ctx")
        assert ho_a.state_card_id == ho_b.state_card_id, "前置：应共享同一 ho-* 卡"

        state_dir = sec_a.state_manager.data_dir
        barrier = threading.Barrier(2, timeout=5)
        # 版本无关：新实现走一次扫描索引，旧实现走逐卡扫描包装——两者都在
        # "判定存活引用"的时机被屏障对齐，旧顺序下必然双跳过（TOCTOU）。
        hook = ("_build_ho_reference_index"
                if hasattr(SecretarySession, "_build_ho_reference_index")
                else "_scan_ho_referencing_sessions")
        original = getattr(SecretarySession, hook)

        def aligned(self, *args, **kwargs):
            result = original(self, *args, **kwargs)
            # 两次判定在此对齐：旧顺序下双方各自仍看到对方存活 → 双跳过
            try:
                barrier.wait()
            except Exception:
                pass
            return result

        setattr(SecretarySession, hook, aligned)
        errors = []

        def _run(sec):
            try:
                sec.delete_session()
            except Exception as exc:  # noqa: BLE001 - 测试记录后断言
                errors.append(exc)

        t1 = threading.Thread(target=_run, args=(sec_a,))
        t2 = threading.Thread(target=_run, args=(sec_b,))
        t1.start()
        t2.start()
        t1.join()
        t2.join()
        setattr(SecretarySession, hook, original)
        return ho_a.state_card_id, state_dir, errors

    def test_barrier_aligned_concurrent_delete_no_orphan(self, tmp_dir):
        """屏障对齐的并发删除：共享 ho-* 卡 + .lock 必须被最后撤销者回收。"""
        sid, state_dir, errors = self._barrier_aligned_round(tmp_dir)
        assert errors == [], f"并发删除不应抛异常: {errors}"
        assert not os.path.exists(os.path.join(state_dir, f"{sid}.json")), \
            "并发删除后共享 ho-* 卡不应成为无人回收的孤儿"
        assert not os.path.exists(os.path.join(state_dir, f"{sid}.lock")), \
            "共享 ho-* 卡的 .lock 不应残留"
        assert not os.path.exists(
            os.path.join(state_dir, f"{sid}.owner.json")), \
            "共享 ho-* 卡的 .owner.json 不应残留"
        assert not os.path.exists(os.path.join(state_dir, "cc-a.json"))
        assert not os.path.exists(os.path.join(state_dir, "cc-b.json"))

    def test_repeated_concurrent_delete_no_leak(self, tmp_dir):
        """多轮屏障对齐并发删除，泄漏计数必须为 0（原实现 27/30 泄漏）。"""
        leaks = 0
        rounds = 12
        for i in range(rounds):
            root = os.path.join(tmp_dir, f"round-{i}")
            os.makedirs(root)
            sid, state_dir, errors = self._barrier_aligned_round(root)
            assert errors == [], f"第 {i} 轮并发删除抛异常: {errors}"
            if os.path.exists(os.path.join(state_dir, f"{sid}.json")):
                leaks += 1
        assert leaks == 0, f"{rounds} 轮并发删除泄漏 {leaks} 次，应为 0"


def _standalone_layout(tmp_dir, layout):
    """按布局构造「独立 HandoffBus 使用者 + 会话共享同一 ho-* 卡」的场景。

    返回 ``(bus, session_data_root)``。四类布局均属受支持路径：

    - ``colocated``：独立 bus 与会话同用 ``<root>/handoffs``（原覆盖）；
    - ``queue``：独立 bus 落 ``<root>/queue``，``state_root`` 由父目录推导
      → 与会话共享 state 根（P2-② 推导规则，Issue #4 原始描述路径）；
    - ``explicit_state_root``：独立 bus 显式 ``state_root=<root>``；
    - ``out_of_root``：交接卡目录完全在会话数据根**之外**，仅共享 state 根
      （仅靠目录扫描不可达，须由 state 根级引用登记覆盖）。
    """
    from secretary.handoff import HandoffBus

    if layout == "colocated":
        return HandoffBus(data_dir=os.path.join(tmp_dir, "handoffs")), tmp_dir
    if layout == "queue":
        return HandoffBus(data_dir=os.path.join(tmp_dir, "queue")), tmp_dir
    if layout == "explicit_state_root":
        return HandoffBus(
            data_dir=os.path.join(tmp_dir, "queue"), state_root=tmp_dir), tmp_dir
    if layout == "out_of_root":
        root = os.path.join(tmp_dir, "sess-root")
        os.makedirs(root, exist_ok=True)
        return HandoffBus(
            data_dir=os.path.join(tmp_dir, "elsewhere"), state_root=root), root
    raise AssertionError(f"未知布局: {layout}")


class TestSharedHoCardIndependentBusReference:
    """🔴#2 / 🔴#4：独立 HandoffBus 使用者（不落台账）的引用须被计入存活引用源。

    参数化覆盖四类布局——异交接目录（``queue/``、显式 ``state_root``、会话根
    之外）此前漏检，导致秘书 ``delete_session()`` 误删共享卡、存活交接卡引用
    静默失效（🔴#4）。
    """

    @pytest.mark.parametrize(
        "layout", ["colocated", "queue", "explicit_state_root", "out_of_root"])
    def test_standalone_bus_reference_blocks_shared_ho_delete(
            self, tmp_dir, layout):
        """独立 HandoffBus 与秘书会话共享同一 ho-* 卡：
        秘书 delete_session 后共享卡须保留（独立使用者仍引用）。"""
        bus, root = _standalone_layout(tmp_dir, layout)
        ho_std = bus.create_handoff(
            "team-x", "team-y", "同一意图", "独立上下文", auto_state_card=True)

        sec = SecretarySession("ind-a", data_dir=root)
        ho_sec = sec.create_handoff("team-x", "team-y", "同一意图", "秘书上下文")
        assert ho_std.state_card_id == ho_sec.state_card_id, \
            "前置：同源/同目标/同意图应共享同一 ho-* 卡"
        sid = ho_sec.state_card_id
        state_dir = sec.state_manager.data_dir

        sec.delete_session()

        assert os.path.exists(os.path.join(state_dir, f"{sid}.json")), \
            "独立 HandoffBus 使用者仍引用 → 共享卡不得被删（引用失效）"
        assert os.path.exists(
            os.path.join(bus.data_dir, f"{ho_std.id}.json")), \
            "独立使用者的交接卡不得被秘书删除"
        # 秘书自身数据已清
        assert not os.path.exists(os.path.join(state_dir, "ind-a.json"))
        assert not os.path.exists(
            os.path.join(sec.handoff_bus.data_dir, ".session-ind-a.owned"))

    @pytest.mark.parametrize(
        "layout", ["colocated", "queue", "explicit_state_root", "out_of_root"])
    def test_standalone_reference_released_then_card_reclaimed(
            self, tmp_dir, layout):
        """独立使用者删除自己的交接卡后引用清空，共享卡可被回收（无孤儿）。"""
        bus, root = _standalone_layout(tmp_dir, layout)
        ho_std = bus.create_handoff(
            "team-x", "team-y", "释放意图", "独立上下文", auto_state_card=True)
        sec = SecretarySession("ind-b", data_dir=root)
        ho_sec = sec.create_handoff("team-x", "team-y", "释放意图", "秘书上下文")
        sid = ho_sec.state_card_id
        state_dir = sec.state_manager.data_dir

        sec.delete_session()
        assert os.path.exists(os.path.join(state_dir, f"{sid}.json")), \
            "前置：独立引用未清，共享卡保留"

        # 独立使用者删除自己的交接卡 → 引用清空
        bus.delete(ho_std.id)

        # 最后一个秘书侧引用者（新会话实例）回收共享卡
        reclaim = SecretarySession("ind-b2", data_dir=root)
        index = reclaim._build_ho_reference_index()
        assert not index.get(sid), \
            "独立交接卡删除后不应再有存活引用"
        assert reclaim._delete_ho_state_card_if_safe(sid, index) is True
        assert not os.path.exists(os.path.join(state_dir, f"{sid}.json")), \
            "引用清空后共享卡应可回收"
        reclaim.delete_session()


class TestSharedHoCardSelfResidueInvariant:
    """🔴#4 跟进：不变量「存活交接卡 → 其 state_card_id 状态卡必须存在」在
    **自身交接卡删除失败**时同样成立。

    原实现把本会话认领的交接卡 ID 作为 ``exclude_handoff_ids`` 从索引中排除，
    假定"自身卡一定已删"。若删除失败（OSError/权限/竞态）卡仍在盘上，排除它
    会让共享卡被误删，而残卡仍持有失效引用 —— 与 🔴#4 同型。修复：不再排除，
    磁盘上仍在的卡即真实存活引用（**不误删**；残卡留盘期间共享卡一并留盘，
    **无自动回收路径**，须显式清理残卡后由后续删除者再次判定方可回收）。
    """

    def test_own_handoff_delete_failure_keeps_shared_card(self, tmp_dir, monkeypatch):
        """自身交接卡删除失败仍留盘 → 共享卡不得被删（引用仍有效）。"""
        sec = SecretarySession("sr-a", data_dir=tmp_dir)
        ho = sec.create_handoff("s-x", "t-y", "删除失败意图", "ctx")
        sid = ho.state_card_id
        state_dir = sec.state_manager.data_dir
        handoff_path = os.path.join(sec.handoff_bus.data_dir, f"{ho.id}.json")

        # 模拟删除失败：仅对该交接卡文件的 os.remove 抛 OSError
        real_remove = os.remove

        def flaky_remove(path, *args, **kwargs):
            if os.path.abspath(path) == os.path.abspath(handoff_path):
                raise OSError("simulated removal failure")
            return real_remove(path, *args, **kwargs)

        monkeypatch.setattr(os, "remove", flaky_remove)
        sec.delete_session()
        monkeypatch.undo()

        assert os.path.exists(handoff_path), "前置：自身交接卡仍留盘"
        assert os.path.exists(os.path.join(state_dir, f"{sid}.json")), \
            "留盘的交接卡仍持有有效引用 → 共享卡不得被删（否则引用静默失效）"
        assert not os.path.exists(os.path.join(state_dir, "sr-a.json")), \
            "秘书自身状态卡仍应正常回收"

    def test_own_residue_reclaims_after_card_removed(self, tmp_dir):
        """残卡清除后引用清空 → 共享卡可由后续删除者回收（不永久孤儿）。"""
        sec_a = SecretarySession("sr-b", data_dir=tmp_dir)
        ho = sec_a.create_handoff("s-x", "t-y", "残卡回收意图", "ctx")
        sid = ho.state_card_id
        state_dir = sec_a.state_manager.data_dir
        handoff_path = os.path.join(sec_a.handoff_bus.data_dir, f"{ho.id}.json")

        # 模拟"删除失败"：自身卡留盘、台账与状态卡已撤（delete_session 顺序）
        real_remove = os.remove

        def flaky_remove(path, *args, **kwargs):
            if os.path.abspath(path) == os.path.abspath(handoff_path):
                raise OSError("simulated removal failure")
            return real_remove(path, *args, **kwargs)

        os.remove = flaky_remove
        try:
            sec_a.delete_session()
        finally:
            os.remove = real_remove
        assert os.path.exists(handoff_path), "前置：残卡仍留盘"

        other = SecretarySession("sr-c", data_dir=tmp_dir)
        assert f"handoff:{ho.id}" in other._build_ho_reference_index().get(sid, []), \
            "残卡留盘期间仍应计为存活引用（不误删）"

        # 🟡C 措辞固化（判别性）：残卡留盘期间共享卡**无延迟/自动回收路径**——
        # 任何新会话的 delete_session 都不会触发回收；须**显式清理残卡**后由
        # 后续删除者再次判定方可回收。若实现改为"新会话顺带回收孤儿 ho-* 卡"，
        # 本断言失败。
        other.delete_session()
        assert os.path.exists(os.path.join(state_dir, f"{sid}.json")), \
            "残卡留盘期间共享卡必须留盘（不存在延迟/自动回收路径）"

        # 残卡清除 → 引用清空 → 共享卡可回收（不永久孤儿）
        os.remove(handoff_path)
        other2 = SecretarySession("sr-c2", data_dir=tmp_dir)
        index = other2._build_ho_reference_index()
        assert not index.get(sid), "残卡清除后不应再有存活引用"
        assert other2._delete_ho_state_card_if_safe(sid, index) is True
        assert not os.path.exists(os.path.join(state_dir, f"{sid}.json")), \
            "引用清空后共享卡应可回收（无永久孤儿）"
        other2.delete_session()


class TestSharedHoCardScanPruning:
    """🔴#4 跟进：扫描界须剪枝重型非数据目录，且形状校验须能排除伪交接卡。"""

    def test_heavy_dirs_pruned_and_no_pseudo_reference(self, tmp_dir):
        """node_modules 内伪交接卡（含 state_card_id/source_unit/target_unit/id）
        不应被计入引用——否则会永久阻塞共享卡回收，且拖慢删除路径。"""
        import json

        nm = os.path.join(tmp_dir, "node_modules", "pkg")
        os.makedirs(nm)
        for i in range(20):
            with open(os.path.join(nm, f"f{i}.json"), "w", encoding="utf-8") as f:
                json.dump({"id": f"p{i}", "source_unit": "x", "target_unit": "y",
                           "state_card_id": f"fake-{i}"}, f)

        sec = SecretarySession("sp-a", data_dir=tmp_dir)
        index = sec._build_ho_reference_index()
        assert not any(k.startswith("fake-") for k in index), \
            "重型非数据目录应被剪枝，其 JSON 不得产生伪引用"
        sec.delete_session()

    def test_state_card_files_not_mistaken_as_handoff(self, tmp_dir):
        """state/ 下的 StateCard JSON 不得被误判为交接卡引用。"""
        sec_a = SecretarySession("sp-b", data_dir=tmp_dir)
        sec_b = SecretarySession("sp-c", data_dir=tmp_dir)
        ho = sec_a.create_handoff("s-x", "t-y", "形状校验意图", "ctx")

        index = sec_b._build_ho_reference_index()
        # StateCard 无 source_unit/target_unit → 不应贡献 handoff: 标签
        for labels in index.values():
            assert not any(l.startswith("handoff:sp-b") for l in labels), \
                "StateCard JSON 不得被误判为交接卡"
        assert f"handoff:{ho.id}" in index.get(ho.state_card_id, []), \
            "真实交接卡引用仍应被计入（判别性）"
        sec_a.delete_session()
        sec_b.delete_session()


class TestPruneNameDirVisibility:
    """🔴 P0-1 复评回归（Issue #4 复评 #3 / head 60b625d 打回）：剪枝不得按
    **裸目录名**判定。

    原实现按 ``_SKIP_SCAN_DIRS`` 名单名整片跳过，而名单中的名字
    （``build``/``dist``/``env``/``venv``/``.venv``/``target``/``vendor``/
    ``coverage`` 等）完全可能被合法选作 ``data_dir``。此时落在其中的真实/
    遗留交接卡不可见 → 共享 ``ho-*`` 卡被误删、存活引用静默失效（评审实测
    悬空 11/12）。修复后：名单目录须先过形状判定；保护子树（数据根 /
    state / handoffs / content / handoff-refs）永不剪枝。
    """

    @pytest.mark.parametrize(
        "name", ["node_modules", "__pycache__", "venv", ".venv", "env",
                 "dist", "build", "target", "site-packages", "vendor",
                 "coverage"])
    def test_skip_named_dir_holding_real_card_not_pruned(self, tmp_dir, name):
        """剪枝名单名目录内承载**真实**交接卡（无 .ref 登记的遗留/手写卡）：
        引用必须可见 → 秘书 delete_session 不得删共享卡（否则引用悬空）。"""
        import json

        sec = SecretarySession("pn-a", data_dir=tmp_dir)
        ho = sec.create_handoff("team-a", "team-b", "名单目录意图", "ctx")
        sid = ho.state_card_id
        state_dir = sec.state_manager.data_dir

        d = os.path.join(tmp_dir, name)
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, "legacy-card.json"), "w", encoding="utf-8") as f:
            json.dump({"id": "legacy-card", "source_unit": "team-a",
                       "target_unit": "team-b", "intent": "名单目录意图",
                       "state_card_id": sid, "status": "created"}, f)

        sec.delete_session()
        assert os.path.exists(os.path.join(state_dir, f"{sid}.json")), \
            f"剪枝名单名目录 {name!r} 内的真实交接卡引用必须可见（不得误删共享卡）"

    def test_skip_named_dir_without_real_card_still_pruned(self, tmp_dir):
        """判别性对照：名单目录内**无**交接卡（伪 JSON 指向不存在的状态卡）
        → 剪枝仍生效，伪引用不得阻塞共享卡回收（保留原性能收益与防误判）。"""
        import json

        nm = os.path.join(tmp_dir, "node_modules", "pkg")
        os.makedirs(nm)
        for i in range(20):
            with open(os.path.join(nm, f"f{i}.json"), "w", encoding="utf-8") as f:
                json.dump({"id": f"p{i}", "source_unit": "x", "target_unit": "y",
                           "state_card_id": f"fake-{i}"}, f)

        sec = SecretarySession("pn-b", data_dir=tmp_dir)
        index = sec._build_ho_reference_index()
        assert not any(k.startswith("fake-") for k in index), \
            "名单目录内伪引用（指向不存在的状态卡）不得计入"
        sec.delete_session()

    def test_standalone_bus_skip_named_dir_derived_state_root(self, tmp_dir):
        """独立 ``HandoffBus(data_dir=<root>/build)`` **未显式 state_root**
        （由父目录推导）→ 落在名单名目录内的交接卡引用必须可见。

        覆盖评审要求的两类组合：① 剪枝名单名承载真实卡；② 未显式 state_root
        （P2-② 推导规则下 state_root 由 ``data_dir`` 父目录得到）。
        """
        from secretary.handoff import HandoffBus

        bus = HandoffBus(data_dir=os.path.join(tmp_dir, "build"))  # 无 state_root
        assert bus.state_root == tmp_dir, "前置：state_root 应由父目录推导"
        ho = bus.create_handoff("team-a", "team-b", "名单目录独立意图", "ctx",
                                auto_state_card=True)

        sec = SecretarySession("pn-d", data_dir=tmp_dir)
        ho_sec = sec.create_handoff("team-a", "team-b", "名单目录独立意图", "ctx")
        sid = ho_sec.state_card_id
        state_dir = sec.state_manager.data_dir

        sec.delete_session()
        assert os.path.exists(os.path.join(state_dir, f"{sid}.json")), \
            "名单名目录内的独立使用者引用必须可见 → 共享卡不得被误删"

    def test_data_root_named_as_skip_dir_never_pruned(self, tmp_dir):
        """data_dir **本身**取名单名（如 build/）：保护子树永不剪枝——
        其内交接卡引用必须可见。"""
        root = os.path.join(tmp_dir, "build")
        os.makedirs(root)
        sec = SecretarySession("pn-c", data_dir=root)
        ho = sec.create_handoff("team-a", "team-b", "数据根名单名意图", "ctx")
        sid = ho.state_card_id
        state_dir = sec.state_manager.data_dir

        other = SecretarySession("pn-c2", data_dir=root)
        index = other._build_ho_reference_index()
        assert f"handoff:{ho.id}" in index.get(sid, []), \
            "data_dir 为名单名时，其内交接卡引用必须可见（保护子树）"

        sec.delete_session()
        assert not os.path.exists(os.path.join(state_dir, f"{sid}.json")), \
            "自身引用撤销后无他引 → 共享卡应正常回收（保护子树下扫描正常，无孤儿）"
        other.delete_session()


class TestRegistryListdirToctouGuard:
    """🔴 P0-2 复评回归：``isdir → listdir`` TOCTOU 异常不得逃逸。

    并发的 ``HandoffBus.delete`` → ``_unregister_ref`` 会回收空登记目录，
    使 ``_build_ho_reference_index`` 来源 3 的 ``os.listdir`` 抛
    ``FileNotFoundError``。原实现让异常逃逸出 ``delete_session`` → 中断于
    回收中途，留 ho-*.json/.lock、归档、台账残骸（评审注入 100% 复现）。
    """

    def test_registry_dir_removed_between_isdir_and_listdir(self, tmp_dir,
                                                            monkeypatch):
        """注入：``isdir(reg_dir)`` 返回 True 后、``listdir`` 前删目录 →
        delete_session 不得抛异常，且回收完整（无 ho-*/.lock/归档/台账残骸）。"""
        sec = SecretarySession("tg-a", data_dir=tmp_dir)
        ho = sec.create_handoff("ua", "ub", "注入意图", "ctx")
        sid = ho.state_card_id
        sec.fold("归档段落一\n归档段落二")
        state_dir = sec.state_manager.data_dir
        ho_dir = sec.handoff_bus.data_dir
        reg_dir = sec.handoff_bus.ref_registry_dir()
        assert os.path.isdir(reg_dir), "前置：登记目录存在"

        real_listdir = os.listdir

        def flaky_listdir(path="."):
            # 模拟 isdir 判定后、listdir 前该目录被并发回收（TOCTOU 窗口）
            if os.path.abspath(str(path)) == os.path.abspath(reg_dir):
                shutil.rmtree(reg_dir, ignore_errors=True)
                raise FileNotFoundError(2, "No such file or directory",
                                        str(path))
            return real_listdir(path)

        monkeypatch.setattr(os, "listdir", flaky_listdir)
        ret = sec.delete_session()  # 旧实现此处抛 FileNotFoundError
        monkeypatch.undo()

        assert ret is True
        assert not os.path.exists(os.path.join(state_dir, f"{sid}.json")), \
            "注入竞态下共享 ho-* 卡仍须被回收（无残骸）"
        assert not os.path.exists(os.path.join(state_dir, f"{sid}.lock")), \
            "ho-* 卡的 .lock 不得残留"
        assert not os.path.exists(os.path.join(state_dir, "tg-a.json"))
        assert not os.path.exists(os.path.join(ho_dir, ".session-tg-a.owned")), \
            "归属台账不得残留"
        content = os.path.join(tmp_dir, "content", "tg-a")
        assert not os.path.isdir(content), "归档会话目录不得残留"

    def test_scan_failure_skips_reclaim_without_abort(self, tmp_dir,
                                                      monkeypatch):
        """第二道防线：扫描本身抛 OSError → 保守跳过共享卡回收（不误删），
        但后续归档/台账清理不得中断（delete_session 全路径不半途而废）。"""
        sec = SecretarySession("tg-b", data_dir=tmp_dir)
        ho = sec.create_handoff("ua", "ub", "扫描失败意图", "ctx")
        sid = ho.state_card_id
        sec.fold("段落一\n段落二")
        state_dir = sec.state_manager.data_dir
        ho_dir = sec.handoff_bus.data_dir

        def boom(self, *a, **k):
            raise OSError("simulated scan failure")

        monkeypatch.setattr(SecretarySession, "_build_ho_reference_index", boom)
        ret = sec.delete_session()
        monkeypatch.undo()

        assert ret is True
        assert os.path.exists(os.path.join(state_dir, f"{sid}.json")), \
            "扫描不可用 → 无法证明引用已清 → 保守不删共享卡（宁可不删）"
        assert not os.path.exists(os.path.join(state_dir, "tg-b.json")), \
            "本会话状态卡仍须回收"
        assert not os.path.exists(os.path.join(ho_dir, ".session-tg-b.owned")), \
            "台账不得残留（异常不得中断后续清理）"
        assert not os.path.isdir(os.path.join(tmp_dir, "content", "tg-b")), \
            "归档不得残留"

    def test_concurrent_create_fold_delete_no_exception(self, tmp_dir):
        """自然并发（线程 × 多轮 create/fold/delete 同根）：delete_session
        不得因 registry 目录并发回收而抛异常（原实现实测 3/2160）。"""
        import threading

        errors = []

        def _run(tag):
            try:
                for i in range(12):
                    s = SecretarySession(f"cc-{tag}-{i}", data_dir=tmp_dir)
                    s.fold("并发段落一\n并发段落二")
                    s.create_handoff("ua", "ub", "并发同意图", f"ctx{i}")
                    s.delete_session()
            except BaseException as exc:  # noqa: BLE001 - 测试记录后断言
                errors.append(exc)

        threads = [threading.Thread(target=_run, args=(t,)) for t in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert errors == [], f"并发 create/fold/delete 不应抛异常: {errors}"


class TestHandoffUnitNameTraversalGuard:
    """🔴 P1-3 补充：``create_handoff`` 的 ``source_unit`` / ``target_unit``
    亦参与 ``card_id`` 拼接 → 必须同受白名单校验。

    反例（本角色独立探针）：``create_handoff("../state/VICTIM", "t", …)`` 会让
    ``_save`` 把卡写到 ``data_dir`` **之外**（路径穿越**写入**面）——虽不直接删除
    文件，但可污染数据根之外（且 ``SecretarySession`` 门面同样可达）。
    修复：非法 ``source_unit``/``target_unit`` 抛 ``ValueError``；``_save`` 另做
    纵深防御（非法 ``card.id`` 拒绝落盘）。
    """

    @pytest.mark.parametrize("bad", ["../state/VICTIM", "a/b", "..", "", "a\\b", "/abs"])
    def test_create_handoff_rejects_traversal_units(self, tmp_dir, bad):
        from secretary.handoff import HandoffBus

        bus = HandoffBus(data_dir=os.path.join(tmp_dir, "data", "handoffs"))
        with pytest.raises(ValueError):
            bus.create_handoff(bad, "t", "意图", "ctx")

    def test_facade_create_handoff_rejects_traversal_unit(self, tmp_dir):
        """门面公共 API 同样不可被穿越单元名绕过（不写出数据根之外）。"""
        sec = SecretarySession("un-a", data_dir=tmp_dir)
        victim = os.path.join(tmp_dir, "state", "VICTIM.json")
        os.makedirs(os.path.dirname(victim), exist_ok=True)
        with open(victim, "w", encoding="utf-8") as f:
            f.write("{}")

        with pytest.raises(ValueError):
            sec.create_handoff("../state/VICTIM", "t", "意图", "ctx")

        escaped = [os.path.join(dp, f)
                   for dp, _, fs in os.walk(tmp_dir) for f in fs
                   if f.startswith("..")]
        assert escaped == [], f"不得写出数据根之外（穿越卡）: {escaped}"

    def test_save_rejects_illegal_card_id_defense_in_depth(self, tmp_dir):
        """纵深防御：绕过 ``create_handoff`` 直接构造非法 ``id`` 的 HandoffCard
        → ``_save`` 拒绝落盘（不写出 data_dir 之外）。"""
        from secretary.handoff import HandoffBus
        from secretary.handoff.models import HandoffCard

        bus = HandoffBus(data_dir=os.path.join(tmp_dir, "data", "handoffs"))
        card = HandoffCard(id="../ESCAPED", source_unit="a", target_unit="b",
                           intent="i", state_card_id="ho-x")
        with pytest.raises(ValueError):
            bus._save(card)


class TestRegistryFailFast:
    """🟡 P1-1 复评修复：登记失败不得静默（唯一可见性路径时 fail-fast）。"""

    def test_registry_required_only_when_outside_state_root(self, tmp_dir):
        from secretary.handoff import HandoffBus

        root = os.path.join(tmp_dir, "sess-root")
        os.makedirs(root)
        outside = HandoffBus(data_dir=os.path.join(tmp_dir, "elsewhere"),
                             state_root=root)
        inside = HandoffBus(data_dir=os.path.join(root, "handoffs"),
                            state_root=root)
        assert outside._registry_required() is True, \
            "data_dir 在 state_root 子树之外 → 登记为唯一可见性路径"
        assert inside._registry_required() is False, \
            "data_dir 在 state_root 子树内 → 目录扫描兜底，登记非唯一路径"

    def test_registry_dual_channel_backup_survives_primary_blocked(self, tmp_dir):
        """🟡E 闭环（双通道）：主登记目录被同名普通文件占位 → **备通道**
        ``.handoff-refs/`` 生效 → 建卡不失败，且根外引用仍可见。"""
        from secretary.handoff import HandoffBus

        root = os.path.join(tmp_dir, "sess-root")
        os.makedirs(root)
        elsewhere = os.path.join(tmp_dir, "elsewhere")
        with open(os.path.join(root, "handoff-refs"), "w", encoding="utf-8") as f:
            f.write("occupied")  # 主通道不可用

        bus = HandoffBus(data_dir=elsewhere, state_root=root)
        ho = bus.create_handoff("ua", "ub", "双通道意图", "ctx",
                                auto_state_card=True)
        assert os.path.isfile(os.path.join(
            root, ".handoff-refs", f"{ho.id}.ref")), "备通道登记应写入"

        sec = SecretarySession("dc-a", data_dir=root)
        index = sec._build_ho_reference_index()
        assert f"handoff:{ho.id}" in index.get(ho.state_card_id, []), \
            "主通道被占位时，备通道登记须让根外引用仍可见（🟡E 闭环）"

    def test_registry_failure_fail_fast_when_only_path(self, tmp_dir):
        """**双通道全部失败**（主 + 备登记目录均被同名普通文件占位）+
        data_dir 在 state_root 之外 → 建卡 fail-fast（抛 OSError）且回滚交接卡，
        不产生"写入成功但引用静默悬空"的卡。"""
        from secretary.handoff import HandoffBus

        root = os.path.join(tmp_dir, "sess-root")
        os.makedirs(root)
        elsewhere = os.path.join(tmp_dir, "elsewhere")
        # 两条通道均以普通文件占位 → makedirs 必抛 FileExistsError(OSError)
        for name in ("handoff-refs", ".handoff-refs"):
            with open(os.path.join(root, name), "w", encoding="utf-8") as f:
                f.write("occupied")

        bus = HandoffBus(data_dir=elsewhere, state_root=root)
        with pytest.raises(OSError):
            bus.create_handoff("ua", "ub", "唯一可见性意图", "ctx",
                               auto_state_card=True)
        leftover = ([f for f in os.listdir(elsewhere) if f.endswith(".json")]
                    if os.path.isdir(elsewhere) else [])
        assert leftover == [], "fail-fast 应回滚刚落盘的交接卡（不留悬空引用）"

    def test_registry_failure_tolerated_when_dir_scan_covers(self, tmp_dir):
        """registry 被占位但 data_dir 在 state_root 子树内 → 目录扫描兜底，
        建卡不失败，且引用仍可见（登记失败不致命）。"""
        from secretary.handoff import HandoffBus

        root = os.path.join(tmp_dir, "sess-root")
        os.makedirs(root)
        with open(os.path.join(root, "handoff-refs"), "w", encoding="utf-8") as f:
            f.write("occupied")

        bus = HandoffBus(data_dir=os.path.join(root, "handoffs"),
                         state_root=root)
        ho = bus.create_handoff("ua", "ub", "目录兜底意图", "ctx",
                                auto_state_card=True)
        sec = SecretarySession("ff-a", data_dir=root)
        index = sec._build_ho_reference_index()
        assert f"handoff:{ho.id}" in index.get(ho.state_card_id, []), \
            "登记失败但目录扫描可达 → 引用仍须可见"


class TestSharedHoCardStateRootRegistry:
    """🔴#4：state 根级引用登记——覆盖"交接卡目录在会话数据根之外"的布局，
    且登记随卡回收、stale 登记不得永久阻塞共享卡回收。"""

    def test_registry_entry_created_and_removed_with_card(self, tmp_dir):
        from secretary.handoff import HandoffBus

        bus = HandoffBus(data_dir=os.path.join(tmp_dir, "queue"),
                         state_root=tmp_dir)
        ho = bus.create_handoff("ua", "ub", "登记意图", "ctx",
                                auto_state_card=True)
        reg = os.path.join(tmp_dir, "handoff-refs", f"{ho.id}.ref")
        assert os.path.exists(reg), "auto_state_card 应登记 state 根级引用"
        bus.delete(ho.id)
        assert not os.path.exists(reg), "删除交接卡应同步注销引用登记"

    def test_stale_registry_does_not_block_reclaim(self, tmp_dir):
        """交接卡在盘外被移除（未经 HandoffBus.delete）→ 登记视为失效，
        不得永久阻塞共享 ho-* 卡回收。"""
        from secretary.handoff import HandoffBus

        bus = HandoffBus(data_dir=os.path.join(tmp_dir, "queue"),
                         state_root=tmp_dir)
        ho = bus.create_handoff("ua", "ub", "失效登记意图", "ctx",
                                auto_state_card=True)
        sec = SecretarySession("stale-1", data_dir=tmp_dir)
        ho_sec = sec.create_handoff("ua", "ub", "失效登记意图", "ctx")
        sid = ho_sec.state_card_id
        state_dir = sec.state_manager.data_dir
        # 盘外移除交接卡本体，保留 .ref（模拟异常退出/外部删除）
        os.remove(os.path.join(bus.data_dir, f"{ho.id}.json"))

        sec.delete_session()
        assert not os.path.exists(os.path.join(state_dir, f"{sid}.json")), \
            "失效登记不得阻塞共享卡回收"

    def test_registry_dir_no_empty_residue_after_delete(self, tmp_dir):
        """会话自身交接卡删除后，其引用登记与空登记目录一并回收（不留残骸）。"""
        sec = SecretarySession("reg-res-1", data_dir=tmp_dir)
        sec.create_handoff("ua", "ub", "残骸意图", "ctx")
        reg_dir = os.path.join(tmp_dir, "handoff-refs")
        assert os.path.isdir(reg_dir) and os.listdir(reg_dir), \
            "前置：建卡后应有引用登记"

        sec.delete_session()
        assert not os.path.exists(reg_dir), "空登记目录不应残留"

    def test_registry_reference_blocks_delete_when_dir_out_of_root(self, tmp_dir):
        """交接卡目录在会话数据根之外：目录扫描不可达，仅登记可见 →
        仍须阻止误删共享卡。"""
        from secretary.handoff import HandoffBus

        root = os.path.join(tmp_dir, "sess-root")
        os.makedirs(root)
        bus = HandoffBus(data_dir=os.path.join(tmp_dir, "elsewhere"),
                         state_root=root)
        bus.create_handoff("ua", "ub", "根外意图", "ctx",
                           auto_state_card=True)
        sec = SecretarySession("oor-1", data_dir=root)
        ho_sec = sec.create_handoff("ua", "ub", "根外意图", "ctx")
        sid = ho_sec.state_card_id
        state_dir = sec.state_manager.data_dir

        sec.delete_session()
        assert os.path.exists(os.path.join(state_dir, f"{sid}.json")), \
            "根外独立使用者的引用应经登记被计入 → 共享卡不得被删"


class TestLedgerMalformedRobustness:
    """🔴#3：台账非 dict（合法 JSON 但非对象）不得中断 delete_session 或构造。"""

    def test_other_session_malformed_ledger_does_not_break_delete(self, tmp_dir):
        """同根其他会话台账为数组：delete_session 不抛裸异常且回收完全。"""
        import json

        SecretarySession("bad", data_dir=tmp_dir)  # 存活会话（有状态卡）
        sec = SecretarySession("mal-a", data_dir=tmp_dir)
        ho_dir = sec.handoff_bus.data_dir
        state_dir = sec.state_manager.data_dir
        with open(os.path.join(ho_dir, ".session-bad.owned"),
                  "w", encoding="utf-8") as f:
            json.dump(["x"], f)  # 合法 JSON、非对象

        ho = sec.create_handoff("t-a", "t-b", "非法台账意图", "ctx")
        ret = sec.delete_session()  # 旧实现此处抛 AttributeError

        assert ret is True, "状态卡存在时应返回 True"
        assert not os.path.exists(os.path.join(state_dir, "mal-a.json")), \
            "本会话状态卡须回收"
        assert not os.path.exists(
            os.path.join(ho_dir, ".session-mal-a.owned")), "本会话台账须回收"
        assert not os.path.exists(
            os.path.join(state_dir, f"{ho.state_card_id}.json")), \
            "无存活引用的 ho-* 卡须回收（不得因非法台账中断而残留）"

    def test_own_malformed_ledger_constructor_and_delete_robust(self, tmp_dir):
        """本会话自身台账为数组：构造不崩、delete_session 幂等清理完全。"""
        import json

        ho_dir = os.path.join(tmp_dir, "handoffs")
        os.makedirs(ho_dir, exist_ok=True)
        with open(os.path.join(ho_dir, ".session-crash.owned"),
                  "w", encoding="utf-8") as f:
            json.dump(["y"], f)  # 旧实现在 _load_ledger 抛 AttributeError

        sec = SecretarySession("crash", data_dir=tmp_dir)  # 不抛异常
        assert sec._owned_handoffs() == [], "非法台账应视为空台账"
        state_dir = sec.state_manager.data_dir
        sec.delete_session()
        assert not os.path.exists(os.path.join(state_dir, "crash.json"))

    def test_malformed_field_types_treated_as_empty(self, tmp_dir):
        """handoff_ids/state_card_ids 字段类型不符（字符串/数字/对象）视为空台账，不崩。

        判别性断言（替换原恒真断言 ``index == {} or all(v ...)``——索引值由
        ``_add`` 构造必为非空列表，该式恒真、无判别力）：
        ① 字段类型不符的**其他会话**台账不得贡献任何引用者标签；
        ② 同目录正常交接卡的引用必须**仍被计入**——证明索引非"空而恒真"，
        而是确实扫到并区分了合法/非法台账。
        """
        import json

        from secretary.handoff import HandoffBus

        ho_dir = os.path.join(tmp_dir, "handoffs")
        os.makedirs(ho_dir, exist_ok=True)
        # 本会话自身台账字段类型不符
        with open(os.path.join(ho_dir, ".session-weird.owned"),
                  "w", encoding="utf-8") as f:
            json.dump({"handoff_ids": "not-a-list",
                       "state_card_ids": 42}, f)

        sec = SecretarySession("weird", data_dir=tmp_dir)
        assert sec._owned_handoffs() == [], "字段类型不符应视为空台账"

        # 同根另一存活会话台账字段类型同样不符（对象/字符串）
        sec_other = SecretarySession("weird2", data_dir=tmp_dir)
        with open(os.path.join(ho_dir, ".session-weird2.owned"),
                  "w", encoding="utf-8") as f:
            json.dump({"handoff_ids": {"a": 1}, "state_card_ids": "42"}, f)

        # 同目录一张正常交接卡：其引用必须仍被计入（判别性）
        bus = HandoffBus(data_dir=ho_dir)
        ho = bus.create_handoff("s-x", "t-x", "类型不符意图", "ctx",
                                auto_state_card=True)

        index = sec._build_ho_reference_index()
        labels = index.get(ho.state_card_id, [])
        assert f"handoff:{ho.id}" in labels, \
            "正常交接卡的引用应被计入（否则断言无判别力）"
        assert "weird2" not in labels, \
            "字段类型不符的台账不得贡献引用者标签"
        assert all("weird2" not in v for v in index.values()), \
            "非法台账会话不得出现在任何引用者标签中"

        sec.delete_session()
        sec_other.delete_session()

    def test_reference_index_single_scan_multi_card(self, tmp_dir):
        """一次扫描建立 state_card_id → 引用者集合索引（O(台账+卡) 而非逐卡扫）。"""
        sec_a = SecretarySession("idx-a", data_dir=tmp_dir)
        ho_a = sec_a.create_handoff("s-a", "t-a", "意图A", "ctx")
        sec_b = SecretarySession("idx-b", data_dir=tmp_dir)
        ho_b = sec_b.create_handoff("s-b", "t-b", "意图B", "ctx")

        sec_c = SecretarySession("idx-c", data_dir=tmp_dir)
        index = sec_c._build_ho_reference_index()
        # 索引同时覆盖两张卡，且标签为会话 ID / handoff:<id> 两类语义
        assert ho_a.state_card_id in index
        assert ho_b.state_card_id in index
        assert "idx-a" in index[ho_a.state_card_id]
        assert f"handoff:{ho_a.id}" in index[ho_a.state_card_id]
        sec_c.delete_session()
        sec_a.delete_session()
        sec_b.delete_session()


# ------------------------------------------------------------------
# 🔴1 复评第 4 轮：登记目录 rmdir × 登记写 竞态（模块级互斥锁）
# _register_ref 的「建目录 + mkstemp 原子写」与 _unregister_ref 的「删 .ref +
# 空目录 rmdir」无互斥 → rmdir 与 mkstemp 交叠 → mkstemp 抛 FileNotFoundError →
# 双通道同时失败 → .ref 不落地（根外卡引用唯一可见通道失效）→ 共享 ho-* 卡误删。
# ------------------------------------------------------------------

class TestRegistryRmdirWriteRace:
    """🔴1：登记/注销临界区互斥，.ref 不得因空目录 rmdir 竞态静默丢失。"""

    def test_concurrent_register_unregister_no_ref_loss(self, tmp_dir):
        """6 线程 × 300 轮并发「登记→校验 .ref 存在→注销」，丢失须为 0
        （修复前 22–33/2400）。"""
        root = os.path.join(tmp_dir, "root")
        os.makedirs(root, exist_ok=True)
        outside = os.path.join(tmp_dir, "outside")
        os.makedirs(outside, exist_ok=True)
        threads = 6
        iters = 300
        lost = []
        lock = threading.Lock()

        def worker(k):
            bus = HandoffBus(data_dir=os.path.join(outside, f"q{k}"),
                             state_root=root)
            for i in range(iters):
                cid = f"c{k}-{i}"
                bus._register_ref(cid, "ho-shared")
                reg = os.path.join(root, "handoff-refs", f"{cid}.ref")
                if not os.path.exists(reg):
                    with lock:
                        lost.append(cid)
                bus._unregister_ref(cid)

        ts = [threading.Thread(target=worker, args=(k,))
              for k in range(threads)]
        for t in ts:
            t.start()
        for t in ts:
            t.join()
        assert lost == [], (
            f"🔴1 回归：并发下 .ref 静默丢失 {len(lost)}/{threads*iters}"
            "（登记目录 rmdir 与登记写无互斥）")

    def test_register_after_unregister_rmdir_lands_ref(self, tmp_dir):
        """确定性：注销回收空登记目录后，随后登记须按需重建并落地 .ref。"""
        root = os.path.join(tmp_dir, "root")
        os.makedirs(root, exist_ok=True)
        bus = HandoffBus(data_dir=os.path.join(tmp_dir, "queue"),
                         state_root=root)
        bus._unregister_ref("first")
        bus._register_ref("second", "ho-shared")
        assert os.path.isfile(
            os.path.join(root, "handoff-refs", "second.ref")),             "登记目录被 rmdir 后登记须按需重建并落地"

    def test_concurrent_out_of_root_create_no_spurious_failure(self, tmp_dir):
        """根外布局并发 create_handoff：登记不得因 rmdir 竞态误判为
        「双通道全失败」而 fail-fast 抛 OSError（修复前 2/1200）。"""
        root = os.path.join(tmp_dir, "root")
        os.makedirs(root, exist_ok=True)
        outside = os.path.join(tmp_dir, "outside")
        os.makedirs(outside, exist_ok=True)
        errors = []
        lock = threading.Lock()

        def worker(k):
            bus = HandoffBus(data_dir=os.path.join(outside, f"q{k}"),
                             state_root=root)
            for i in range(150):
                try:
                    card = bus.create_handoff(
                        "s", "t", f"intent-{k}-{i}", "ctx",
                        auto_state_card=True)
                    bus.delete(card.id)
                except Exception as exc:  # noqa: BLE001 - 测试记录后断言
                    with lock:
                        errors.append(exc)

        ts = [threading.Thread(target=worker, args=(k,)) for k in range(6)]
        for t in ts:
            t.start()
        for t in ts:
            t.join()
        assert errors == [], (
            f"🔴1 回归：并发根外建卡误抛 {len(errors)} 次：{errors[:3]}")
