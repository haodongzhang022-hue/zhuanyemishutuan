"""Issue #4 验收矩阵（架构·测试 自写 · 门禁可执行证据）

不变量（本文件全部用例的共同判据）：
    **存活交接卡 → 其 ``state_card_id`` 状态卡必须存在**

覆盖：
- G1 登记目录 ``rmdir`` × 登记写竞态（🔴1）：并发登记不得静默丢失；根外布局
  并发 ``create_handoff`` 不得误抛 ``OSError``（fail-fast 误判）。
- G2 来源 3 ``listdir`` 守卫（🔴2）：注入 ``listdir`` ``OSError``（含
  ``isdir → listdir`` TOCTOU）→ ``delete_session`` 不抛异常且本会话残骸为 0。
- G3 来源 2 独立判别：构造「仅来源 2 可见」（无台账、无登记）的交接卡 →
  共享卡须保留；移除该卡后须可回收。
- G4 形状**反向**断言：``state_card_id`` 存在但缺 ``source_unit`` /
  ``target_unit`` / ``id`` 的 JSON 不得计入（防形状校验被放宽后误计入）。
- G5 台账存活校验：台账 ``state_card_ids`` 含目标卡但该会话状态卡已删 →
  不得计入（防死会话永久阻塞回收）。
- G6 🟡1 已知边界锚点：``create_handoff`` 与 ``delete_session`` 之间的窗口
  （闸门置于 ``_save`` 之后：卡已落盘、``.ref`` 未落地）→ 不变量被破坏。
  该锚点**锁定已知边界**：若此断言失败，说明窗口已收口，须同步更新契约文档。

本文件由「架构·测试」独立编写（不复用作者/评审用例），用例均带**反向对照**，
避免恒真断言。跨版判别力见各用例 docstring。
"""
from __future__ import annotations

import json
import os
import shutil
import tempfile
import threading

import pytest

from secretary.handoff import HandoffBus
from secretary.session import SecretarySession


@pytest.fixture
def tmp_dir():
    d = tempfile.mkdtemp()
    yield d
    shutil.rmtree(d, ignore_errors=True)


def _residue(root: str, session_id: str, ho_id: str = "") -> list:
    """本会话（含其认领 ho-* 卡）在数据根下的残骸清单。"""
    out = []
    for dirpath, _dirnames, filenames in os.walk(root):
        for f in filenames:
            if (f == f"{session_id}.json" or f == f"{session_id}.lock"
                    or f == f"{session_id}.owner.json"
                    or f.startswith(f".session-{session_id}")):
                out.append(os.path.relpath(os.path.join(dirpath, f), root))
            if ho_id and f.startswith(ho_id):
                out.append(os.path.relpath(os.path.join(dirpath, f), root))
    return sorted(out)


# ----------------------------------------------------------------------
# G1 登记目录 rmdir × 登记写竞态（🔴1）
# ----------------------------------------------------------------------

class TestG1RegistryRmdirWriteRace:
    """🔴1：登记/注销临界区须互斥，``.ref`` 不得因空目录 ``rmdir`` 竞态丢失。

    跨版判别力：无模块级互斥（``8790e5f`` / ``cc7f4fd``）→ 本组稳定失败
    （本角色探针：丢失 64/5400、误抛 2/4500）；有 ``_REF_REGISTRY_LOCK``
    （``73361ca`` 起）→ 0/5400、0/4500。
    """

    def test_g1_concurrent_register_unregister_no_ref_loss(self, tmp_dir):
        """6 线程 × 200 轮「登记 → 校验 ``.ref`` 存在 → 注销」：丢失须为 0。"""
        root = os.path.join(tmp_dir, "root")
        outside = os.path.join(tmp_dir, "outside")
        os.makedirs(root, exist_ok=True)
        os.makedirs(outside, exist_ok=True)
        threads, iters = 6, 200
        lost, lock = [], threading.Lock()

        def worker(k):
            bus = HandoffBus(data_dir=os.path.join(outside, f"q{k}"),
                             state_root=root)
            for i in range(iters):
                cid = f"g1-{k}-{i}"
                bus._register_ref(cid, "ho-shared")
                if not os.path.exists(
                        os.path.join(root, "handoff-refs", f"{cid}.ref")):
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
            f"G1：并发登记静默丢失 {len(lost)}/{threads * iters}"
            "（登记目录 rmdir 与登记写未互斥）")

    def test_g1_out_of_root_concurrent_create_no_spurious_oserror(self, tmp_dir):
        """根外布局并发 ``create_handoff``：不得误判「双通道全失败」抛 ``OSError``。"""
        root = os.path.join(tmp_dir, "root")
        outside = os.path.join(tmp_dir, "outside")
        os.makedirs(root, exist_ok=True)
        os.makedirs(outside, exist_ok=True)
        errors, lock = [], threading.Lock()

        def worker(k):
            bus = HandoffBus(data_dir=os.path.join(outside, f"q{k}"),
                             state_root=root)
            for i in range(120):
                try:
                    card = bus.create_handoff(
                        "s", "t", f"g1-intent-{k}-{i}", "ctx",
                        auto_state_card=True)
                    bus.delete(card.id)
                except Exception as exc:  # noqa: BLE001 - 记录后统一断言
                    with lock:
                        errors.append(f"{type(exc).__name__}: {exc}")

        ts = [threading.Thread(target=worker, args=(k,)) for k in range(6)]
        for t in ts:
            t.start()
        for t in ts:
            t.join()
        assert errors == [], f"G1：并发根外建卡误抛 {len(errors)} 次：{errors[:3]}"


# ----------------------------------------------------------------------
# G2 来源 3 listdir 守卫（🔴2）
# ----------------------------------------------------------------------

class TestG2Source3ListdirGuard:
    """🔴2：``_build_ho_reference_index`` 来源 3 的 ``listdir`` 须被守卫。

    跨版判别力：``11dfdd9`` / ``570c7ee`` / ``82807ff``（守卫前）→ 注入即抛
    ``FileNotFoundError`` 且留 ``state/ho-*.json`` + ``.lock`` 残骸（本角色
    探针复现）；``a852574`` 起（守卫后）→ 不抛、残骸 0。
    """

    def test_g2_listdir_oserror_does_not_escape_delete_session(
            self, tmp_dir, monkeypatch):
        root = os.path.join(tmp_dir, "data")
        sec = SecretarySession("g2-a", data_dir=root)
        card = sec.create_handoff("unitA", "unitB", "g2-intent", "ctx")
        ho_id = card.state_card_id
        reg_dirs = {os.path.join(root, "handoff-refs"),
                    os.path.join(root, ".handoff-refs")}
        real_listdir = os.listdir

        def fake_listdir(path):
            if os.path.abspath(path) in reg_dirs:
                raise FileNotFoundError(2, "injected", path)
            return real_listdir(path)

        monkeypatch.setattr(os, "listdir", fake_listdir)
        sec.delete_session()          # 旧实现：此处抛 FileNotFoundError
        monkeypatch.undo()

        assert _residue(root, "g2-a", ho_id) == [], \
            "G2：listdir OSError 不得逃逸，且本会话残骸须为 0"

    def test_g2_isdir_listdir_toctou_does_not_escape(
            self, tmp_dir, monkeypatch):
        """``isdir`` 通过、``listdir`` 前目录被并发回收（TOCTOU）→ 同样不得逃逸。"""
        root = os.path.join(tmp_dir, "data")
        sec = SecretarySession("g2-b", data_dir=root)
        card = sec.create_handoff("unitA", "unitB", "g2b-intent", "ctx")
        ho_id = card.state_card_id
        reg_dirs = {os.path.join(root, "handoff-refs"),
                    os.path.join(root, ".handoff-refs")}
        real_listdir, real_isdir = os.listdir, os.path.isdir

        def fake_isdir(path):
            if os.path.abspath(path) in reg_dirs:
                return True           # 判定"目录在"
            return real_isdir(path)

        def fake_listdir(path):
            if os.path.abspath(path) in reg_dirs:
                raise FileNotFoundError(2, "toctou", path)   # 随即消失
            return real_listdir(path)

        monkeypatch.setattr(os.path, "isdir", fake_isdir)
        monkeypatch.setattr(os, "listdir", fake_listdir)
        sec.delete_session()
        monkeypatch.undo()

        assert _residue(root, "g2-b", ho_id) == [], \
            "G2：isdir→listdir TOCTOU 不得逃逸，残骸须为 0"


# ----------------------------------------------------------------------
# G3 来源 2 独立判别
# ----------------------------------------------------------------------

class TestG3Source2Discriminative:
    """来源 2（数据根下未删除交接卡）须**独立**计入，而非被来源 1/3 兜住。"""

    def test_g3_source2_alone_keeps_shared_card(self, tmp_dir):
        """无台账、无登记（双通道清空）→ 仅来源 2 可见 → 共享卡须保留。"""
        root = os.path.join(tmp_dir, "data")
        os.makedirs(root, exist_ok=True)
        bus = HandoffBus(data_dir=os.path.join(root, "queue"))  # state_root=root
        std = bus.create_handoff("g3-x", "g3-y", "g3-intent", "ctx",
                                 auto_state_card=True)
        # 清掉来源 3（主 + 备通道），使来源 2 成为唯一可见路径
        for name in ("handoff-refs", ".handoff-refs"):
            ref = os.path.join(root, name, f"{std.id}.ref")
            if os.path.exists(ref):
                os.remove(ref)
        assert not os.path.exists(
            os.path.join(root, "handoff-refs", f"{std.id}.ref")), \
            "前置：来源 3 已清空"

        sec = SecretarySession("g3-a", data_dir=root)
        own = sec.create_handoff("g3-x", "g3-y", "g3-intent", "ctx")
        assert own.state_card_id == std.state_card_id, \
            "前置：同源/同目标/同意图共享同一 ho-* 卡"
        sid = own.state_card_id
        state_dir = sec.state_manager.data_dir

        sec.delete_session()

        assert os.path.exists(os.path.join(state_dir, f"{sid}.json")), \
            "G3：来源 2 独立可见 → 共享卡不得被删"
        assert os.path.exists(os.path.join(bus.data_dir, f"{std.id}.json")), \
            "G3：独立使用者的交接卡不得被秘书删除"

    def test_g3_source2_released_then_reclaimed(self, tmp_dir):
        """反向对照（防恒真）：来源 2 卡删除后引用清空 → 共享卡须可回收。"""
        root = os.path.join(tmp_dir, "data")
        os.makedirs(root, exist_ok=True)
        bus = HandoffBus(data_dir=os.path.join(root, "queue"))
        std = bus.create_handoff("g3b-x", "g3b-y", "g3b-intent", "ctx",
                                 auto_state_card=True)
        sec = SecretarySession("g3-b", data_dir=root)
        own = sec.create_handoff("g3b-x", "g3b-y", "g3b-intent", "ctx")
        sid = own.state_card_id
        state_dir = sec.state_manager.data_dir

        bus.delete(std.id)            # 卡本体 + 登记一并清
        sec.delete_session()

        assert not os.path.exists(os.path.join(state_dir, f"{sid}.json")), \
            "G3：引用清空后共享卡须可回收（无孤儿）"


# ----------------------------------------------------------------------
# G4 形状反向断言（缺字段不得计入）
# ----------------------------------------------------------------------

class TestG4ShapeReverseAssertions:
    """形状校验须**收紧**：缺 ``source_unit`` / ``target_unit`` / ``id`` 任一
    字段的 JSON 不得计入引用（防形状校验被放宽后误计入 → 死引用永久阻塞回收）。
    """

    @pytest.mark.parametrize("missing", ["source_unit", "target_unit", "id"])
    def test_g4_incomplete_shape_not_counted(self, tmp_dir, missing):
        root = os.path.join(tmp_dir, "data")
        os.makedirs(os.path.join(root, "misc"), exist_ok=True)
        payload = {"id": "misc-card", "source_unit": "a", "target_unit": "b",
                   "state_card_id": "ho-shape-probe"}
        payload.pop(missing)
        with open(os.path.join(root, "misc", "pseudo.json"), "w",
                  encoding="utf-8") as f:
            json.dump(payload, f)

        sec = SecretarySession("g4-a", data_dir=root)
        index = sec._build_ho_reference_index()
        assert "ho-shape-probe" not in index, (
            f"G4：缺 {missing} 的 JSON 不得计入引用（形状校验收紧口径）")
        sec.delete_session()

    def test_g4_complete_shape_counted_control(self, tmp_dir):
        """反向对照（防恒真）：四字段齐备的交接卡形状 JSON 必须计入。"""
        root = os.path.join(tmp_dir, "data")
        os.makedirs(os.path.join(root, "misc"), exist_ok=True)
        with open(os.path.join(root, "misc", "misc-real.json"), "w",
                  encoding="utf-8") as f:
            json.dump({"id": "misc-real", "source_unit": "a",
                       "target_unit": "b", "state_card_id": "ho-shape-real"},
                      f)

        sec = SecretarySession("g4-b", data_dir=root)
        index = sec._build_ho_reference_index()
        assert "handoff:misc-real" in index.get("ho-shape-real", []), \
            "G4：形状完整的交接卡引用必须计入（否则断言无判别力）"
        sec.delete_session()


# ----------------------------------------------------------------------
# G5 台账存活校验
# ----------------------------------------------------------------------

class TestG5LedgerLiveness:
    """归属台账仅在**其会话仍存活**（状态卡存在）时贡献引用。"""

    def test_g5_dead_session_ledger_not_counted(self, tmp_dir):
        """死会话台账（``state/<sid>.json`` 不存在）→ 不得计入，共享卡可回收。"""
        root = os.path.join(tmp_dir, "data")
        sec = SecretarySession("g5-live", data_dir=root)
        ho = sec.create_handoff("g5-x", "g5-y", "g5-intent", "ctx")
        sid = ho.state_card_id
        os.makedirs(os.path.join(root, "handoffs"), exist_ok=True)
        with open(os.path.join(root, "handoffs", ".session-g5dead.owned"),
                  "w", encoding="utf-8") as f:
            json.dump({"session_id": "g5dead", "handoff_ids": [],
                       "state_card_ids": [sid]}, f)
        assert not os.path.exists(os.path.join(root, "state", "g5dead.json")), \
            "前置：死会话状态卡不存在"

        index = sec._build_ho_reference_index()
        assert "g5dead" not in index.get(sid, []), \
            "G5：死会话台账不得贡献引用"
        sec.delete_session()
        assert not os.path.exists(os.path.join(root, "state", f"{sid}.json")), \
            "G5：死会话台账不得永久阻塞共享卡回收"

    def test_g5_alive_session_ledger_counted_control(self, tmp_dir):
        """反向对照（防恒真）：存活会话台账必须贡献引用。"""
        root = os.path.join(tmp_dir, "data")
        alive = SecretarySession("g5-alive", data_dir=root)   # 建出状态卡
        sec = SecretarySession("g5-b", data_dir=root)
        ho = sec.create_handoff("g5b-x", "g5b-y", "g5b-intent", "ctx")
        sid = ho.state_card_id
        with open(os.path.join(root, "handoffs", ".session-g5-alive.owned"),
                  "w", encoding="utf-8") as f:
            json.dump({"session_id": "g5-alive", "handoff_ids": [],
                       "state_card_ids": [sid]}, f)

        index = sec._build_ho_reference_index()
        assert "g5-alive" in index.get(sid, []), \
            "G5：存活会话台账须贡献引用（否则断言无判别力）"
        sec.delete_session()
        alive.delete_session()


# ----------------------------------------------------------------------
# G6 🟡1 已知边界锚点（窗口竞态）
# ----------------------------------------------------------------------

class TestG6KnownBoundaryWindowRace:
    """🟡1 已知边界锚点：``create_handoff`` 落卡与登记落地之间的窗口。

    构造（确定性，非概率）：把闸门置于 ``HandoffBus._save`` **之后**——此刻
    交接卡已落盘、``.ref`` 登记尚未落地；删除者（``delete_session``）在此期间
    撤销自身引用并回收共享 ``ho-*`` 卡 → 不变量被破坏
    （``card_on_disk=True`` 且 ``state_card_exists=False``）。

    实测：``de5eae8`` / ``73361ca`` / ``2a430e1`` 均 **3/3 破坏**——该窗口在
    ``main`` 已存在，非本 PR 引入，故定级 🟡（契约文档已登记为已知边界）。

    **锚点语义**：本用例断言"窗口仍存在"。若断言失败 → 窗口已被收口
    （如「先登记后落卡」或 ``.pending`` 占位落地），须同步更新契约文档口径，
    不得静默放宽。
    """

    @staticmethod
    def _round(tmp_root):
        root = os.path.join(tmp_root, "data")
        outside = os.path.join(tmp_root, "elsewhere")
        os.makedirs(outside, exist_ok=True)
        dele = SecretarySession("g6-dele", data_dir=root)
        seed = dele.create_handoff("uA", "uB", "g6-intent", "ctx")
        ho_id = seed.state_card_id
        prod = HandoffBus(data_dir=outside, state_root=root)

        gate, done = threading.Event(), threading.Event()
        orig_save = HandoffBus._save

        def patched_save(self, card):
            orig_save(self, card)
            gate.set()
            done.wait(5)

        HandoffBus._save = patched_save
        try:
            t = threading.Thread(target=lambda: prod.create_handoff(
                "uA", "uB", "g6-intent", "ctx2", auto_state_card=True))
            t.start()
            assert gate.wait(5), "前置：闸门未被触发（生产线程未落卡）"
            dele.delete_session()      # 自身引用已撤销；.ref 尚未落地
            done.set()
            t.join(5)
        finally:
            HandoffBus._save = orig_save

        card_on_disk = any(
            f.endswith(".json") for f in os.listdir(outside))
        state_card_exists = os.path.exists(
            os.path.join(root, "state", f"{ho_id}.json"))
        return card_on_disk, state_card_exists

    def test_g6_window_race_boundary_is_still_open(self, tmp_dir):
        results = [self._round(os.path.join(tmp_dir, f"r{i}"))
                   for i in range(3)]
        violations = [c and not s for c, s in results]
        assert all(violations), (
            "G6 已知边界锚点失效：窗口似已收口（实测 "
            f"{results}）→ 须更新契约文档「窗口竞态」口径，"
            "不得静默放宽本锚点")
