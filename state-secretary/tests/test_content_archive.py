"""M9+ 原文归档检索层测试（ContentArchive）

覆盖：
- store / retrieve 原文全文闭环（content_ref → 原文）
- retrieve_content / list_refs / retrieve_session / list_sessions
- store_segments 批量归档与编号（含空段跳过）
- FoldEngine.fold_content 自动归档 → content_ref 可找回原文
- FoldEngine.retrieve_original 按引用取回原文
- FoldEngine.search_tasks(include_original=True) 命中返回原文
- 路径穿越加固（非法 session_id / content_ref 拒绝）
- 空 content 拒绝 / 删除 / delete_session
- 原子写临时文件不残留

本文件遵循本仓库测试约定：不依赖 pytest，采用纯函数式 +
if __name__ == "__main__" 显式调用（与 scripts/change-aware-test.sh
的 python3 直接执行方式兼容）。
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../src"))

from secretary.state.content_archive import ContentArchive
from secretary.state.fold_engine import FoldEngine
from secretary.state.models import StateCard


def _make_ca(tmpdir, root="content"):
    return ContentArchive(root_dir=os.path.join(tmpdir, root))


def _make_env(tmpdir):
    """返回 (ca, fe)，二者共享同一归档根（对齐 FoldEngine.archive_root）。"""
    root = os.path.join(tmpdir, "content")
    ca = ContentArchive(root_dir=root)
    fe = FoldEngine("sess-full", archive_root=root)
    return ca, fe


# ------------------------------------------------------------------
# store / retrieve
# ------------------------------------------------------------------
def test_store_retrieve_roundtrip():
    with tempfile.TemporaryDirectory() as tmpdir:
        ca, _ = _make_env(tmpdir)
        ref = ca.store("sess-1", "sess-1-seg001",
                       "这是折叠前的一段完整原文。")
        assert ref == "sess-1-seg001"

        entry = ca.retrieve("sess-1", "sess-1-seg001")
        assert entry["content"] == "这是折叠前的一段完整原文。"
        assert entry["session_id"] == "sess-1"
        assert entry["content_ref"] == "sess-1-seg001"

        # 纯文本便捷读取
        assert ca.retrieve_content("sess-1", "sess-1-seg001") == \
            "这是折叠前的一段完整原文。"


def test_store_retrieve_nonexistent():
    with tempfile.TemporaryDirectory() as tmpdir:
        ca, _ = _make_env(tmpdir)
        assert ca.retrieve("sess-x", "nope-999") is None
        assert ca.retrieve_content("sess-x", "nope-999") is None


def test_store_rejects_empty_content():
    with tempfile.TemporaryDirectory() as tmpdir:
        ca, _ = _make_env(tmpdir)
        raised = False
        try:
            ca.store("sess-1", "sess-1-seg001", "   ")
        except ValueError:
            raised = True
        assert raised, "空 content 应抛出 ValueError"


# ------------------------------------------------------------------
# store_segments / list
# ------------------------------------------------------------------
def test_store_segments_numbering():
    with tempfile.TemporaryDirectory() as tmpdir:
        ca, _ = _make_env(tmpdir)
        refs = ca.store_segments("sess-multi",
                                 ["段A原文", "段B原文", "段C交付"])
        assert refs == ["sess-multi-seg001", "sess-multi-seg002",
                        "sess-multi-seg003"]

        assert ca.list_refs("sess-multi") == refs
        assert ca.list_sessions() == ["sess-multi"]

        session = ca.retrieve_session("sess-multi")
        assert [e["content"] for e in session] == \
            ["段A原文", "段B原文", "段C交付"]


def test_store_segments_skip_empty():
    with tempfile.TemporaryDirectory() as tmpdir:
        ca, _ = _make_env(tmpdir)
        refs = ca.store_segments("sess-e", ["  ", "段A", "段B"])
        assert refs == ["sess-e-seg001", "sess-e-seg002"]
        assert [e["content"] for e in ca.retrieve_session("sess-e")] == \
            ["段A", "段B"]


# ------------------------------------------------------------------
# FoldEngine 联动
# ------------------------------------------------------------------
def test_fold_content_auto_archives():
    with tempfile.TemporaryDirectory() as tmpdir:
        ca, fe = _make_env(tmpdir)
        content = """第一步：需求分析。

第二步：核心引擎实现。

第三步：交付文档。"""
        card = fe.fold_content(content)
        assert len(card.tasks) == 3
        # 每个任务 content_ref 应能取回原文
        for i, t in enumerate(card.tasks):
            assert t.content_ref == f"sess-full-seg{i + 1:03d}"
            assert ca.retrieve_content("sess-full", t.content_ref) is not None
        # 交付段(最后)展开、其余折叠
        assert card.tasks[-1].fold == "expanded"
        assert all(t.fold == "collapsed" for t in card.tasks[:-1])


def test_fold_content_archive_off_no_archive():
    """archive=False 时不写归档（兼容只需折叠、不需要归档的场景）。"""
    with tempfile.TemporaryDirectory() as tmpdir:
        ca, fe = _make_env(tmpdir)
        fe.fold_content("仅一段，不需要归档。", archive=False)
        assert ca.list_refs("sess-full") == []


def test_fold_engine_retrieve_original():
    with tempfile.TemporaryDirectory() as tmpdir:
        ca, fe = _make_env(tmpdir)
        ca.store("sess-full", "sess-full-seg001", "原文第一段内容")
        assert fe.retrieve_original("sess-full", "sess-full-seg001") == \
            "原文第一段内容"
        assert fe.retrieve_original("sess-full", "nope-000") is None


def test_fold_content_segments_match_original():
    """fold 生成的任务 content_ref 与归档原文一一对应（多空行边界）。"""
    with tempfile.TemporaryDirectory() as tmpdir:
        _, fe = _make_env(tmpdir)
        content = """段A内容。



段B内容。


段C交付。"""
        card = fe.fold_content(content)
        assert len(card.tasks) == 3
        originals = [fe.retrieve_original("sess-full", t.content_ref)
                     for t in card.tasks]
        assert originals == ["段A内容。", "段B内容。", "段C交付。"]


def test_search_tasks_include_original():
    with tempfile.TemporaryDirectory() as tmpdir:
        _, fe = _make_env(tmpdir)
        ca = ContentArchive(root_dir=fe.archive_root)
        card = StateCard(session_id="sess-full", objective="测试")
        card.add_task(title="重构折叠引擎", content_ref="sess-full-seg001",
                      summary="重构折叠引擎模块", fold="collapsed")
        card.add_task(title="交付方案", content_ref="sess-full-seg002",
                      summary="交付完整方案", fold="expanded")
        # 提前归档，供 include_original 取回
        ca.store_segments(
            "sess-full", ["本次重构折叠引擎模块", "交付完整方案"])

        res = fe.search_tasks([card], "重构", include_original=True)
        assert len(res) == 1
        assert res[0]["original"] == "本次重构折叠引擎模块"

        # 不 include_original 时无 original 键
        res2 = fe.search_tasks([card], "重构")
        assert "original" not in res2[0]


# ------------------------------------------------------------------
# 路径穿越 / 校验
# ------------------------------------------------------------------
def test_invalid_session_id_rejected():
    with tempfile.TemporaryDirectory() as tmpdir:
        ca, _ = _make_env(tmpdir)
        raised = False
        try:
            ca.store("../etc", "x-seg001", "content")
        except ValueError:
            raised = True
        assert raised, "非法 session_id 应被拒绝"
        raised = False
        try:
            ca.list_refs("../../x")
        except ValueError:
            raised = True
        assert raised, "非法 session_id 应被拒绝"


def test_invalid_content_ref_rejected():
    with tempfile.TemporaryDirectory() as tmpdir:
        ca, _ = _make_env(tmpdir)
        raised = False
        try:
            ca.store("sess-safe", "../../evil", "content")
        except ValueError:
            raised = True
        assert raised, "非法 content_ref 应被拒绝"


# ------------------------------------------------------------------
# 删除
# ------------------------------------------------------------------
def test_delete():
    with tempfile.TemporaryDirectory() as tmpdir:
        ca, _ = _make_env(tmpdir)
        ca.store("sess-d", "sess-d-seg001", "要删的原文")
        assert ca.delete("sess-d", "sess-d-seg001") is True
        assert ca.retrieve_content("sess-d", "sess-d-seg001") is None
        # 重复删除返回 False
        assert ca.delete("sess-d", "sess-d-seg001") is False


def test_delete_session():
    with tempfile.TemporaryDirectory() as tmpdir:
        ca, _ = _make_env(tmpdir)
        ca.store_segments("sess-del", ["一", "二", "三"])
        assert ca.delete_session("sess-del") == 3
        assert ca.list_refs("sess-del") == []


def test_no_tmp_residue():
    """原子写后不应残留 .tmp 文件"""
    with tempfile.TemporaryDirectory() as tmpdir:
        ca, _ = _make_env(tmpdir)
        ca.store("sess-t", "sess-t-seg001", "原文")
        base = os.path.join(ca.root_dir, "sess-t")
        residue = [f for f in os.listdir(base) if f.endswith(".tmp")]
        assert residue == []


if __name__ == "__main__":
    test_store_retrieve_roundtrip()
    test_store_retrieve_nonexistent()
    test_store_rejects_empty_content()
    test_store_segments_numbering()
    test_store_segments_skip_empty()
    test_fold_content_auto_archives()
    test_fold_content_archive_off_no_archive()
    test_fold_engine_retrieve_original()
    test_fold_content_segments_match_original()
    test_search_tasks_include_original()
    test_invalid_session_id_rejected()
    test_invalid_content_ref_rejected()
    test_delete()
    test_delete_session()
    test_no_tmp_residue()
    print("✅ tests/test_content_archive.py 全部通过")
