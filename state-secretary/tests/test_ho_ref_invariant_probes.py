"""🔴 P0/P1 独立探针（架构·测试）——不变量：「存活交接卡 → 其 state_card_id 状态卡必须存在」

运行：``pytest -q tests/test_ho_ref_invariant_probes.py``

本文件由「架构·测试」在 Issue #4 复评阶段独立编写，与评审（架构·评审）新增的三组
探针互为交叉验证。探针在 ``60b625d``/``570c7ee``（及此前）上**全部失败**，
记录了 P0-1（33 项）/P0-2（2 项）/P1-3（4 项）的破坏面。

**已转为硬断言（2026-09-10，🔴 修复后）**：上述三项已由「架构·开发」在
``a852574`` 修复，本文件同步**移除 ``xfail`` 标注**——原 ``xfail(strict=False)``
在修复后虽显示 XPASS，但若日后回归会**静默转为 xfail 而不报失败**，门禁失效。
改为硬断言后，任何回归都会直接 FAIL，作为 P0/P1 清零的**持续**可执行证据。

覆盖三组（对应评审复评回执的确认项）：

- **P0-1** 扫描界剪枝（``_SKIP_SCAN_DIRS`` / 隐藏目录）使**存活交接卡引用不可见**
  → 共享 ``ho-*`` 卡被误删 → 存活交接卡引用静默失效。
  3 条触发路径（旧版遗留无登记 / 登记失败 / 外部删 ``.ref``）× 剪枝名。
- **P0-2** ``_build_ho_reference_index`` 来源 3（``isdir`` → ``listdir`` 无 try）
  在 ``listdir`` 抛 ``OSError`` 时**逃逸**，``delete_session`` 中断、清理不完整。
- **P1-3** ``_unregister_ref`` / ``HandoffBus.delete`` 未校验 ``card_id`` →
  ``.ref`` 通道路径穿越；台账 ``handoff_ids`` 注入后经 ``delete_session`` 可达删除面，
  并可与 P0-1 连锁（穿越删他人 ``.ref`` → 其引用不可见 → 共享卡误删）。

口径（与评审一致，本文件不改变实现口径）：
- 剪枝名单共 **11** 个，其中非隐藏 **10** 个（``.venv`` 为隐藏 → 基线既有面，单列）；
- P0-1 新增回归面 = 10 × 3 = **30/30**；``11dfdd9`` 基线为 **3/33**（仅 ``.venv``）；
- P0-2 引入点 ``11dfdd9``（state 根级引用登记随该提交引入）。
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from secretary.handoff import HandoffBus  # noqa: E402
from secretary.session import SecretarySession  # noqa: E402

# 与 session.py 的 _SKIP_SCAN_DIRS 逐字对齐（11 个）
PRUNE_DIRS = [
    "node_modules", "__pycache__", "venv", ".venv", "env",
    "dist", "build", "target", "site-packages", "vendor", "coverage",
]
NON_HIDDEN_PRUNE = [d for d in PRUNE_DIRS if not d.startswith(".")]
TRIGGERS = ("legacy_no_registry", "registry_failed", "ref_externally_removed")

# 🔴 修复后（a852574）移除 xfail 标注 → 硬断言。
# 回归即 FAIL（原 xfail(strict=False) 会让回归静默通过，门禁失效）。
# P0-1 剪枝致存活引用不可见 → 共享 ho-* 卡误删
# P0-2 引用登记 listdir 无 try → delete_session 逃逸
# P1-3 card_id 未校验 → 路径穿越 / 删除面可达


@pytest.fixture
def roots():
    root = tempfile.mkdtemp()
    outside = tempfile.mkdtemp()
    yield root, outside
    shutil.rmtree(root, ignore_errors=True)
    shutil.rmtree(outside, ignore_errors=True)


def _make_victim_bus(root, outside, dirname, trigger):
    """构造异目录独立使用者：其交接卡引用共享 ho-* 卡，但引用对秘书不可见。"""
    if trigger == "registry_failed":
        # 以文件占位 handoff-refs → 登记目录不可创建（登记静默失败）
        with open(os.path.join(root, "handoff-refs"), "w", encoding="utf-8") as f:
            f.write("x")
    bus = HandoffBus(data_dir=os.path.join(outside, dirname), state_root=root)
    card = bus.create_handoff("s", "t", "shared-intent", "ctx",
                              auto_state_card=True)
    if trigger in ("legacy_no_registry", "ref_externally_removed"):
        reg = os.path.join(root, "handoff-refs", f"{card.id}.ref")
        if os.path.isfile(reg):
            os.remove(reg)
    return bus, card


def _invariant_broken(root, bus, card, ho_id):
    """返回 (是否破坏不变量, 交接卡是否存活, ho-* 卡是否存活)。

    判定前须已执行 ``secretary.delete_session()``——不变量语义为
    「delete 之后，仍存活的交接卡所引用的状态卡必须仍存在」。
    """
    card_alive = os.path.exists(os.path.join(bus.data_dir, f"{card.id}.json"))
    ho_alive = os.path.exists(
        os.path.join(root, "state", f"{ho_id}.json"))
    return (card_alive and not ho_alive), card_alive, ho_alive


# ----------------------------------------------------------------------
# P0-1：剪枝致存活引用不可见 → 共享 ho-* 卡误删（30/30 新增面 + .venv 基线面）
# ----------------------------------------------------------------------

class TestP0_1PrunedDirReferenceVisibility:
    @pytest.mark.parametrize("dirname", NON_HIDDEN_PRUNE)
    @pytest.mark.parametrize("trigger", TRIGGERS)
    def test_non_hidden_prune_dir_keeps_live_handoff_reference(
            self, roots, dirname, trigger):
        root, outside = roots
        bus, victim = _make_victim_bus(root, outside, dirname, trigger)
        sec = SecretarySession("A", data_dir=root)
        own = sec.create_handoff("s", "t", "shared-intent", "ctx")
        assert own.state_card_id == victim.state_card_id, \
            "前置：同源/同目标/同意图应共享同一 ho-* 卡"

        sec.delete_session()          # 触发存活引用判定与共享卡回收
        broken, card_alive, ho_alive = _invariant_broken(
            root, bus, victim, victim.state_card_id)
        assert card_alive, "前置：受害者交接卡须在盘上（探针不变量前提）"
        assert not broken, (
            f"不变量破坏：{dirname}/{trigger} 下存活交接卡引用未被计入索引 → "
            f"共享 ho-* 卡被误删（card_alive={card_alive}, ho_alive={ho_alive}）")

    @pytest.mark.parametrize("trigger", TRIGGERS)
    def test_hidden_venv_baseline_surface(self, roots, trigger):
        """``.venv`` 属隐藏目录剪枝（``11dfdd9`` 基线既有），单列不计入
        新增面，但同根因，建议随 P0-1 一并覆盖。"""
        root, outside = roots
        bus, victim = _make_victim_bus(root, outside, ".venv", trigger)
        sec = SecretarySession("A", data_dir=root)
        own = sec.create_handoff("s", "t", "shared-intent", "ctx")
        assert own.state_card_id == victim.state_card_id
        sec.delete_session()
        broken, _, _ = _invariant_broken(root, bus, victim, victim.state_card_id)
        assert not broken, "基线既有面：.venv 剪枝同样破坏不变量"

    def test_pure_api_path_registry_failure_pruned_dir(self, roots):
        """纯 API 路径（受害者仅用 HandoffBus，不经门面登记）：登记失败 +
        剪枝目录同样破坏——秘书侧只要认领同一共享 ho-* 卡即可触发误删。"""
        root, outside = roots
        bus, victim = _make_victim_bus(
            root, outside, "node_modules", "registry_failed")
        sec = SecretarySession("B", data_dir=root)
        own = sec.create_handoff("s", "t", "shared-intent", "ctx")
        assert own.state_card_id == victim.state_card_id, \
            "前置：秘书侧须认领同一共享 ho-* 卡"
        sec.delete_session()
        broken, card_alive, ho_alive = _invariant_broken(
            root, bus, victim, victim.state_card_id)
        assert card_alive, "前置：受害者交接卡须在盘上"
        assert not broken, "纯 API 路径下剪枝 + 登记失败同样不得误删共享卡"


# ----------------------------------------------------------------------
# P0-2：引用登记 listdir 无 try → delete_session 逃逸
# ----------------------------------------------------------------------

class TestP0_2RegistryListdirEscape:
    def test_listdir_oserror_does_not_escape_delete_session(self, roots):
        root, _ = roots
        sec = SecretarySession("esc-1", data_dir=root)
        sec.create_handoff("s", "t", "i", "c")
        reg_dir = os.path.join(root, "handoff-refs")
        assert os.path.isdir(reg_dir), "前置：登记目录应存在"

        real = os.listdir

        def boom(path):
            if os.path.abspath(str(path)) == os.path.abspath(reg_dir):
                raise OSError(13, "Permission denied", str(path))
            return real(path)

        os.listdir = boom
        try:
            sec.delete_session()  # 不应抛 OSError
        finally:
            os.listdir = real

    def test_isdir_listdir_toctou_does_not_escape(self, roots):
        """``isdir`` 通过后目录消失 → ``listdir`` 抛 FileNotFoundError
        不得逃逸。"""
        root, _ = roots
        sec = SecretarySession("esc-2", data_dir=root)
        reg_dir = os.path.join(root, "handoff-refs")
        os.makedirs(reg_dir, exist_ok=True)
        real_isdir, real_listdir = os.path.isdir, os.listdir

        def isdir(path):
            if os.path.abspath(str(path)) == os.path.abspath(reg_dir):
                return True
            return real_isdir(path)

        def listdir(path):
            if os.path.abspath(str(path)) == os.path.abspath(reg_dir):
                raise FileNotFoundError(2, "No such file or directory")
            return real_listdir(path)

        os.path.isdir, os.listdir = isdir, listdir
        try:
            sec.delete_session()
        finally:
            os.path.isdir, os.listdir = real_isdir, real_listdir


# ----------------------------------------------------------------------
# P1-3：card_id 未校验 → 路径穿越 / 删除面可达（含与 P0-1 的连锁升级）
# ----------------------------------------------------------------------

class TestP1_3CardIdPathTraversal:
    def test_unregister_ref_rejects_traversal(self, roots):
        root, _ = roots
        bus = HandoffBus(data_dir=os.path.join(root, "handoffs"),
                         state_root=root)
        victim = os.path.join(root, "handoff-refs", "victim.ref")
        os.makedirs(os.path.dirname(victim), exist_ok=True)
        with open(victim, "w", encoding="utf-8") as f:
            f.write("{}")
        bus._unregister_ref("../handoff-refs/victim")
        assert os.path.exists(victim), \
            "_unregister_ref 不得因 card_id 路径穿越删除登记目录之外的 .ref"

    def test_handoffbus_delete_rejects_traversal(self, roots):
        root, _ = roots
        bus = HandoffBus(data_dir=os.path.join(root, "handoffs"),
                         state_root=root)
        victim = os.path.join(root, "state", "VICTIM.json")
        os.makedirs(os.path.dirname(victim), exist_ok=True)
        with open(victim, "w", encoding="utf-8") as f:
            f.write("{}")
        bus.delete("../state/VICTIM")
        assert os.path.exists(victim), \
            "HandoffBus.delete 不得删除 data_dir 之外的任意文件"

    def test_ledger_handoff_ids_traversal_not_reachable(self, roots):
        """台账 ``handoff_ids`` 注入后经 ``delete_session`` 可达删除面。"""
        root, _ = roots
        sec = SecretarySession("A", data_dir=root)
        victim = os.path.join(root, "state", "OTHER.json")
        os.makedirs(os.path.dirname(victim), exist_ok=True)
        with open(victim, "w", encoding="utf-8") as f:
            f.write("{}")
        os.makedirs(os.path.join(root, "handoffs"), exist_ok=True)
        with open(os.path.join(root, "handoffs", ".session-A.owned"),
                  "w", encoding="utf-8") as f:
            json.dump({"session_id": "A",
                       "handoff_ids": ["../state/OTHER"],
                       "state_card_ids": []}, f)
        sec.delete_session()
        assert os.path.exists(victim), \
            "台账 handoff_ids 不得构成路径穿越删除面"

    def test_traversal_chain_does_not_break_ho_invariant(self, roots):
        """连锁升级：穿越删他人 ``.ref`` → 其异目录引用不可见 → 共享卡误删。"""
        root, outside = roots
        bus, victim = _make_victim_bus(
            root, outside, "queue", "legacy_no_registry")
        sec = SecretarySession("A", data_dir=root)
        own = sec.create_handoff("s", "t", "shared-intent", "ctx")
        assert own.state_card_id == victim.state_card_id
        os.makedirs(os.path.join(root, "handoffs"), exist_ok=True)
        with open(os.path.join(root, "handoffs", ".session-A.owned"),
                  "w", encoding="utf-8") as f:
            json.dump({"session_id": "A",
                       "handoff_ids": [f"../handoff-refs/{victim.id}"],
                       "state_card_ids": []}, f)
        sec.delete_session()
        broken, _, _ = _invariant_broken(
            root, bus, victim, victim.state_card_id)
        assert not broken, "穿越面与 P0-1 连锁不得破坏「存活卡 → 状态卡存在」不变量"


# ----------------------------------------------------------------------
# 反向守卫（当前应通过）：剪枝不得被"删掉了事"——重型目录仍须剪枝且不产生伪引用
# ----------------------------------------------------------------------

class TestScanGuardRegression:
    def test_heavy_dir_pseudo_refs_not_indexed(self, roots):
        root, _ = roots
        heavy = os.path.join(root, "node_modules", "pkg")
        os.makedirs(heavy)
        for i in range(20):
            with open(os.path.join(heavy, f"f{i}.json"), "w",
                      encoding="utf-8") as f:
                json.dump({"id": f"p{i}", "source_unit": "x",
                           "target_unit": "y", "state_card_id": f"fake-{i}"}, f)
        sec = SecretarySession("guard-1", data_dir=root)
        index = sec._build_ho_reference_index()
        assert not any(k.startswith("fake-") for k in index), \
            "重型目录内伪交接卡不得产生伪引用（修复 P0-1 时须保留该剪枝语义）"
