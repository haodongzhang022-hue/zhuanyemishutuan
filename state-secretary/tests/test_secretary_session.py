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

import pytest

from secretary.session import SecretarySession


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


class TestSharedHoCardIndependentBusReference:
    """🔴#2：独立 HandoffBus 使用者（不落台账）的引用须被计入存活引用源。"""

    def test_standalone_bus_reference_blocks_shared_ho_delete(self, tmp_dir):
        """独立 HandoffBus 与秘书会话共享同源/同目标/同意图 ho-* 卡：
        秘书 delete_session 后共享卡须保留（独立使用者仍引用）。"""
        from secretary.handoff import HandoffBus

        # 独立使用者：不经 SecretarySession，故不落归属台账
        bus = HandoffBus(data_dir=os.path.join(tmp_dir, "handoffs"))
        ho_std = bus.create_handoff(
            "team-x", "team-y", "同一意图", "独立上下文", auto_state_card=True)

        sec = SecretarySession("ind-a", data_dir=tmp_dir)
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

    def test_standalone_reference_released_then_card_reclaimed(self, tmp_dir):
        """独立使用者删除自己的交接卡后引用清空，共享卡可被回收（无孤儿）。"""
        from secretary.handoff import HandoffBus

        bus = HandoffBus(data_dir=os.path.join(tmp_dir, "handoffs"))
        ho_std = bus.create_handoff(
            "team-x", "team-y", "释放意图", "独立上下文", auto_state_card=True)
        sec = SecretarySession("ind-b", data_dir=tmp_dir)
        ho_sec = sec.create_handoff("team-x", "team-y", "释放意图", "秘书上下文")
        sid = ho_sec.state_card_id
        state_dir = sec.state_manager.data_dir

        sec.delete_session()
        assert os.path.exists(os.path.join(state_dir, f"{sid}.json")), \
            "前置：独立引用未清，共享卡保留"

        # 独立使用者删除自己的交接卡 → 引用清空
        bus.delete(ho_std.id)

        # 最后一个秘书侧引用者（新会话实例）回收共享卡
        reclaim = SecretarySession("ind-b2", data_dir=tmp_dir)
        index = reclaim._build_ho_reference_index()
        assert not index.get(sid), \
            "独立交接卡删除后不应再有存活引用"
        assert reclaim._delete_ho_state_card_if_safe(sid, index) is True
        assert not os.path.exists(os.path.join(state_dir, f"{sid}.json")), \
            "引用清空后共享卡应可回收"
        reclaim.delete_session()


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
        """handoff_ids/state_card_ids 字段类型不符（字符串/数字）视为空，不崩。"""
        import json

        ho_dir = os.path.join(tmp_dir, "handoffs")
        os.makedirs(ho_dir, exist_ok=True)
        with open(os.path.join(ho_dir, ".session-weird.owned"),
                  "w", encoding="utf-8") as f:
            json.dump({"handoff_ids": "not-a-list",
                       "state_card_ids": 42}, f)

        sec = SecretarySession("weird", data_dir=tmp_dir)
        assert sec._owned_handoffs() == []
        index = sec._build_ho_reference_index()
        assert index == {} or all(v for v in index.values())
        sec.delete_session()

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
