"""🔴 P0/P1 独立探针（架构·测试）——不变量：「存活交接卡 → 其 state_card_id 状态卡必须存在」

运行：``pytest -q tests/test_ho_ref_invariant_probes.py``

本文件由「架构·测试」在 Issue #4 复评阶段独立编写，与评审（架构·评审）新增的
探针互为交叉验证。**已转为硬断言（🔴 修复后）**：原 ``xfail`` 在修复后显示
``XPASS``，但日后回归会静默转为 ``xfail`` 而不报失败、门禁失效；改为硬断言后
任何回归直接 FAIL，作为 P0/P1 清零的**持续**可执行证据。

口径更正（评审复评 §二，2026-09-10；本文件据此重写，旧稿两处口径已撤）：

- **更正 ①**：``handoff.py:_unregister_ref`` 的 ``os.listdir`` **位于
  ``try/except OSError`` 之内，有守卫、不逃逸**（旧稿"同形无守卫"系误报，已撤）。
  本 PR 新增逃逸面**只有 ``session.py`` ``_build_ho_reference_index`` 来源 3**
  （``isdir`` → ``listdir`` 无 try）。同形点 ``handoff.py`` ``list_pending`` 属
  ``main`` 既有代码、非本 PR diff，不计入本 PR 新增面。
- **更正 ②**：旧稿"``11dfdd9`` 基线 3/33（仅 ``.venv``）"**与实测不符**。旧稿
  30 组把受害者卡目录放在**会话数据根之外**的独立 tempdir，而 ``11dfdd9`` 的
  扫描界只到数据根 + 登记 → 根外卡双路径皆不可见 → **同样误删**；故该 30 组在
  ``11dfdd9`` 与 ``570c7ee`` 上**都失败**，**无跨版判别力**（实测 ``11dfdd9``
  用 ``--runxfail``：41 failed）。
  **正确判别对照 = 卡目录放在会话数据根「之内」、仅改目录名**
  （``TestP0_1DiscriminatingInsideRoot``）：``queue``（非剪枝名）两版皆不违反；
  剪枝名 ``11dfdd9`` 不违反、``570c7ee`` 起违反。实测该面 11dfdd9 → 33 passed、
  570c7ee/60b625d/ee4214e/d7c4607 → 30 failed（修复前，硬断言下即失败）。故
  「``570c7ee`` 相对 ``11dfdd9`` 新引入 P0-1」结论**仍成立**，判别证据即本类。
  旧稿 30 组降级为**覆盖面**（``TestP0_1RootOutsideCoverage``，归 🟡E / Issue #8，
  修复后由双通道登记闭环）。

覆盖四组（均在当前 head 通过；括号内为修复前破坏面，供跨版复核）：

- **P0-1 判别面**（``570c7ee`` 引入）：卡目录在数据根**之内** + 剪枝名 → 存活引用
  不可见 → 共享 ``ho-*`` 卡被误删。3 触发 × 10 非隐藏剪枝名 = 30 + 对照 3。
- **P0-1 根外契约面**（🟡E / Issue #8 闭环后）：卡目录在数据根**之外** →
  双通道登记任一生效即引用可见、双通道全失败则 fail-fast。10 + 1 + 1 + 1 = 13 组。
- **P0-2**：来源 3 ``listdir`` 无 try → ``OSError`` 逃逸 → ``delete_session``
  中断 → 归档残留 + ``ho-*``/``.lock`` 永久孤儿（3 项）。
- **P1-3**：``card_id`` 未校验 → ``.ref`` 通道路径穿越；台账 ``handoff_ids`` 注入
  经 ``delete_session`` 可达删除面；与 P0-1 连锁（4 项）。

口径：剪枝名单共 **11** 个，其中非隐藏 **10** 个（``.venv`` 为隐藏 → 单列）。
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

# 实现侧剪枝名单（漂移断言用）。≤11dfdd9 无此常量 → None。
try:
    from secretary.session.session import _SKIP_SCAN_DIRS as _IMPL_SKIP_SCAN_DIRS
except ImportError:  # pragma: no cover - 旧版本兼容
    _IMPL_SKIP_SCAN_DIRS = None

# 是否具备剪枝名单：决定反向守卫是否适用（保证本文件跨版可跑）。
_HAS_PRUNE_LIST = _IMPL_SKIP_SCAN_DIRS is not None

# 探针侧副本（参数化用）。与实现侧的漂移由 test_prune_dirs_no_drift 断言兜底，
# 避免实现改名/增删剪枝项时探针静默失配。
PRUNE_DIRS = [
    "node_modules", "__pycache__", "venv", ".venv", "env",
    "dist", "build", "target", "site-packages", "vendor", "coverage",
]
NON_HIDDEN_PRUNE = [d for d in PRUNE_DIRS if not d.startswith(".")]
TRIGGERS = ("legacy_no_registry", "registry_failed", "ref_externally_removed")


@pytest.fixture
def roots():
    root = tempfile.mkdtemp()
    outside = tempfile.mkdtemp()
    yield root, outside
    shutil.rmtree(root, ignore_errors=True)
    shutil.rmtree(outside, ignore_errors=True)


def _make_victim_bus(root, card_base, dirname, trigger):
    """构造独立使用者：其交接卡引用共享 ho-* 卡，但引用对秘书不可见。

    ``card_base`` = 卡目录的父目录（数据根之内 = 判别面；根外 = 覆盖面）。
    """
    if trigger == "registry_failed":
        # 占位主登记通道（备通道仍可用 → 引用经备份通道可见，P1-4 双通道）
        with open(os.path.join(root, "handoff-refs"), "w", encoding="utf-8") as f:
            f.write("x")
    bus = HandoffBus(data_dir=os.path.join(card_base, dirname), state_root=root)
    card = bus.create_handoff("s", "t", "shared-intent", "ctx",
                              auto_state_card=True)
    if trigger in ("legacy_no_registry", "ref_externally_removed"):
        # 外部删除双通道登记 → 该引用确已不可见（模拟外部/遗留动作，非缺陷）
        for name in ("handoff-refs", ".handoff-refs"):
            reg = os.path.join(root, name, f"{card.id}.ref")
            if os.path.isfile(reg):
                os.remove(reg)
    return bus, card


def _invariant_broken(root, bus, card, ho_id):
    """返回 (是否破坏不变量, 交接卡是否存活, ho-* 卡是否存活)。

    判定前须已执行 ``secretary.delete_session()``——不变量语义为
    「delete 之后，仍存活的交接卡所引用的状态卡必须仍存在」。
    """
    card_alive = os.path.exists(os.path.join(bus.data_dir, f"{card.id}.json"))
    ho_alive = os.path.exists(os.path.join(root, "state", f"{ho_id}.json"))
    return (card_alive and not ho_alive), card_alive, ho_alive


def _run_shared_card_case(root, card_base, dirname, trigger):
    bus, victim = _make_victim_bus(root, card_base, dirname, trigger)
    sec = SecretarySession("A", data_dir=root)
    own = sec.create_handoff("s", "t", "shared-intent", "ctx")
    assert own.state_card_id == victim.state_card_id, \
        "前置：同源/同目标/同意图应共享同一 ho-* 卡"
    sec.delete_session()
    return _invariant_broken(root, bus, victim, victim.state_card_id)


# ----------------------------------------------------------------------
# P0-1 判别面：数据根「之内」+ 剪枝名 → 引用不可见（570c7ee 新引入）
# 判别对照：queue（非剪枝名）两版皆不违反；剪枝名 11dfdd9 不违反、570c7ee 起违反
# 本类为「570c7ee 相对 11dfdd9 新引入」的判别证据（评审复评 §二 更正②）
# ----------------------------------------------------------------------

class TestP0_1DiscriminatingInsideRoot:
    @pytest.mark.parametrize("dirname", NON_HIDDEN_PRUNE)
    @pytest.mark.parametrize("trigger", TRIGGERS)
    def test_pruned_dir_inside_root_keeps_live_reference(
            self, roots, dirname, trigger):
        root, _ = roots
        broken, card_alive, ho_alive = _run_shared_card_case(
            root, root, dirname, trigger)
        assert card_alive, "前置：受害者交接卡须在盘上（探针不变量前提）"
        assert not broken, (
            f"不变量破坏：数据根内 {dirname}/{trigger} 下存活交接卡引用未计入"
            f"索引 → 共享 ho-* 卡被误删（card_alive={card_alive}, ho_alive={ho_alive}）")

    @pytest.mark.parametrize("trigger", TRIGGERS)
    def test_non_pruned_dir_inside_root_control(self, roots, trigger):
        """判别对照（须通过，两版皆然）：非剪枝名 ``queue`` 不得违反。
        证明判别变量为「目录名是否命中剪枝名单」，而非目录位于根内。"""
        root, _ = roots
        broken, card_alive, ho_alive = _run_shared_card_case(
            root, root, "queue", trigger)
        assert card_alive, "前置：受害者交接卡须在盘上"
        assert not broken, (
            f"对照破坏：非剪枝名 queue/{trigger} 亦违反 → 判别变量未隔离"
            f"（card_alive={card_alive}, ho_alive={ho_alive}）")


# ----------------------------------------------------------------------
# P0-1 覆盖面 / 根外契约（🟡E / Issue #8 闭环后）：卡目录在数据根「之外」
# 修复后契约：双通道登记（主 handoff-refs/ + 备 .handoff-refs/）任一生效即
# 引用可见；双通道全失败且为唯一可见性路径 → 建卡 fail-fast（不产生悬空卡）。
# 注：外部**删除已写登记**（stale 登记）会让根外引用确不可见——属外部动作、
# 非实现缺陷，见契约 §5 迁移前提，故不作为失败面。
# ----------------------------------------------------------------------

class TestP0_1RootOutsideCoverage:
    @pytest.mark.parametrize("dirname", NON_HIDDEN_PRUNE)
    def test_root_outside_card_visible_via_backup_channel(self, roots, dirname):
        """主登记通道失败、备通道正常 → 根外卡引用仍可见（🟡E 双通道闭环）。"""
        root, outside = roots
        broken, card_alive, ho_alive = _run_shared_card_case(
            root, outside, dirname, "registry_failed")
        assert card_alive, "前置：受害者交接卡须在盘上"
        assert not broken, (
            f"🟡E 回归：根外 {dirname} 下主通道失败、备通道正常，引用应可见"
            f"（card_alive={card_alive}, ho_alive={ho_alive}）")

    def test_root_outside_card_visible_via_main_channel(self, roots):
        """根外 + 双通道均正常 → 引用可见（基线，无外部删登记）。"""
        root, outside = roots
        broken, card_alive, ho_alive = _run_shared_card_case(
            root, outside, "queue", "none")
        assert card_alive, "前置：受害者交接卡须在盘上"
        assert not broken, (
            f"根外 queue 双通道正常时引用应可见（card_alive={card_alive}, "
            f"ho_alive={ho_alive}）")

    def test_root_outside_double_channel_failure_fails_fast(self, roots):
        """根外 + 双通道全失败 → 建卡 fail-fast（OSError），不产生悬空卡。
        证明「写入成功但引用静默悬空」不存在（P1-1 + 🟡E 闭环）。"""
        root, outside = roots
        for name in ("handoff-refs", ".handoff-refs"):
            with open(os.path.join(root, name), "w", encoding="utf-8") as f:
                f.write("x")
        bus = HandoffBus(data_dir=os.path.join(outside, "queue"),
                         state_root=root)
        with pytest.raises(OSError):
            bus.create_handoff("s", "t", "shared-intent", "ctx",
                               auto_state_card=True)

    def test_pure_api_path_registry_failure_pruned_dir(self, roots):
        """纯 API 路径（受害者仅用 HandoffBus，不经门面登记）：根外 + 剪枝名 +
        主通道失败 → 备通道兜底，引用可见。"""
        root, outside = roots
        broken, card_alive, ho_alive = _run_shared_card_case(
            root, outside, "node_modules", "registry_failed")
        assert card_alive, "前置：受害者交接卡须在盘上"
        assert not broken, (
            f"纯 API 路径根外 + 剪枝 + 主通道失败，备通道应兜底"
            f"（card_alive={card_alive}, ho_alive={ho_alive}）")


class TestP0_1HiddenVenvBaseline:
    @pytest.mark.parametrize("trigger", TRIGGERS)
    def test_hidden_venv_baseline_surface(self, roots, trigger):
        """``.venv`` 属隐藏目录剪枝（全版本既有），单列不计入新增面，
        但同根因，建议随 P0-1 一并覆盖。"""
        root, _ = roots
        broken, _, _ = _run_shared_card_case(root, root, ".venv", trigger)
        assert not broken, "基线既有面：.venv 剪枝同样破坏不变量"


# ----------------------------------------------------------------------
# P0-2：来源 3 引用登记 listdir 无 try → delete_session 逃逸
# ----------------------------------------------------------------------

def _install_listdir_fault(reg_dir, exc_factory):
    """注入 listdir 故障；返回 restore 回调。"""
    real_listdir = os.listdir

    def boom(path):
        if os.path.abspath(str(path)) == os.path.abspath(reg_dir):
            raise exc_factory()
        return real_listdir(path)

    os.listdir = boom
    return lambda: setattr(os, "listdir", real_listdir)


class TestP0_2RegistryListdirEscape:
    def test_listdir_oserror_does_not_escape_delete_session(self, roots):
        root, _ = roots
        sec = SecretarySession("esc-1", data_dir=root)
        sec.create_handoff("s", "t", "i", "c")
        reg_dir = os.path.join(root, "handoff-refs")
        assert os.path.isdir(reg_dir), "前置：登记目录应存在"

        restore = _install_listdir_fault(
            reg_dir, lambda: OSError(13, "Permission denied"))
        try:
            sec.delete_session()  # 不应抛 OSError
        finally:
            restore()

    def test_isdir_listdir_toctou_does_not_escape(self, roots):
        """``isdir`` 通过后目录消失 → ``listdir`` 抛 FileNotFoundError
        不得逃逸。"""
        root, _ = roots
        sec = SecretarySession("esc-2", data_dir=root)
        reg_dir = os.path.join(root, "handoff-refs")
        os.makedirs(reg_dir, exist_ok=True)

        restore = _install_listdir_fault(
            reg_dir, lambda: FileNotFoundError(2, "No such file or directory"))
        try:
            sec.delete_session()
        finally:
            restore()

    def test_escape_leaves_permanent_orphan_and_archive_residue(self, roots):
        """逃逸后果：步骤 5（归档清理）不执行 → 归档残留；台账已于步骤 3
        删除 → 重试实例无从认领 → ``ho-*.json`` + ``ho-*.lock`` 永久孤儿。"""
        root, _ = roots
        sec = SecretarySession("esc-3", data_dir=root)
        sec.add_task("t1", "obj")
        sec.fold("alpha\n\nbeta")               # 产生 content/esc-3/ 归档
        sec.create_handoff("s", "t", "i", "c")
        ho_ids = list(sec._owned_state_card_ids)
        assert ho_ids, "前置：须认领 ho-* 状态卡"
        ho_id = ho_ids[0]
        reg_dir = sec.handoff_bus.ref_registry_dir()
        archive_dir = os.path.join(root, "content", "esc-3")

        restore = _install_listdir_fault(
            reg_dir, lambda: FileNotFoundError(2, "No such file or directory"))
        try:
            sec.delete_session()
        finally:
            restore()

        assert not os.path.isdir(archive_dir), "逃逸不得跳过步骤 5 归档清理"
        assert not os.path.exists(os.path.join(root, "state", f"{ho_id}.json")), \
            "逃逸不得遗留 ho-* 卡"
        assert not os.path.exists(os.path.join(root, "state", f"{ho_id}.lock")), \
            "逃逸不得遗留 ho-*.lock"
        # 重试一次（不再注入）：台账已删 → 新实例无从认领 → 孤儿永久留存
        SecretarySession("esc-3", data_dir=root).delete_session()
        assert not os.path.exists(os.path.join(root, "state", f"{ho_id}.json")), \
            "重试后 ho-* 卡仍残留 → 永久孤儿"


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
        """连锁升级：台账注入 ``../handoff-refs/<victim.id>`` 试图穿越删他人
        ``.ref`` → 其引用不可见 → 共享卡误删。修复后穿越被 ``card_id`` 白名单
        拦截：victim 的 ``.ref`` 须存活，且共享卡不得被误删。"""
        root, outside = roots
        # victim：根外独立使用者（登记为其唯一可见性路径），双通道均正常
        bus, victim = _make_victim_bus(root, outside, "queue", "none")
        reg = os.path.join(root, "handoff-refs", f"{victim.id}.ref")
        assert os.path.isfile(reg), "前置：victim 主通道登记须存在"
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
        assert os.path.isfile(reg), \
            "穿越面不得删除他人 .ref（card_id 白名单应拦截）"
        broken, _, _ = _invariant_broken(
            root, bus, victim, victim.state_card_id)
        assert not broken, "穿越面与 P0-1 连锁不得破坏「存活卡 → 状态卡存在」不变量"


# ----------------------------------------------------------------------
# 反向守卫（当前应通过）：剪枝不得被"删掉了事"——重型目录仍须剪枝且不产生伪引用
# ≤11dfdd9 无剪枝名单 → 不适用（按版本能力 skip）
# ----------------------------------------------------------------------

@pytest.mark.skipif(not _HAS_PRUNE_LIST,
                    reason="≤11dfdd9 无剪枝名单，反向守卫不适用")
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

    def test_prune_dirs_no_drift(self):
        """探针侧 ``PRUNE_DIRS`` 不得与实现侧 ``_SKIP_SCAN_DIRS`` 漂移：
        实现改名/增删剪枝项时此处即报错，避免探针覆盖面静默失配。"""
        assert set(PRUNE_DIRS) == set(_IMPL_SKIP_SCAN_DIRS), (
            f"剪枝名单漂移：探针 {sorted(PRUNE_DIRS)} vs 实现 "
            f"{sorted(_IMPL_SKIP_SCAN_DIRS)}")


# ----------------------------------------------------------------------
# 判别力补强（架构·测试，Issue #4 复评第 4 轮 · 总师裁定回转要求）：
# 「来源 2 独立判别 / 形状校验 / 台账存活」断言 + 截断读取。
#
# 既有用例多用「秘书门面建卡」（同时落台账 + 登记），来源 1/3 会掩盖来源 2
# 是否真的生效；台账存活校验亦无独立判别。本组把每个来源**隔离为唯一可见
# 路径**，并配反向对照证明断言非恒真。
#
# 四项断言均经**变异测试**验证判别力（每项 docstring 注明变异体）：
# 在对应实现被"顺手删掉/放宽"的变异体上必然 FAIL，在本实现上 PASS。
# ----------------------------------------------------------------------

class TestReferenceSourceDiscriminative:
    """来源 2（交接卡本体）与台账存活校验的**独立判别**。"""

    def test_source2_alone_blocks_shared_ho_delete(self, roots):
        """来源 2 独立可见：清掉 state 根级登记（来源 3）后，仅靠数据根下
        未删除交接卡的 ``state_card_id`` 亦不得误删共享卡。

        变异判别力：注入「跳过非隐藏 ``*.json``」（来源 2 失效）→ 本断言 FAIL。
        """
        root, _ = roots
        bus = HandoffBus(data_dir=os.path.join(root, "queue"))  # state_root 推导=root
        std = bus.create_handoff("src2-x", "src2-y", "来源2独立意图", "ctx",
                                 auto_state_card=True)
        # 清掉来源 3（主 + 备登记通道），使来源 2 成为唯一可见路径
        for rd in (os.path.join(root, "handoff-refs"),
                   os.path.join(root, ".handoff-refs")):
            ref = os.path.join(rd, f"{std.id}.ref")
            if os.path.exists(ref):
                os.remove(ref)

        sec = SecretarySession("src2-a", data_dir=root)
        own = sec.create_handoff("src2-x", "src2-y", "来源2独立意图", "秘书ctx")
        assert own.state_card_id == std.state_card_id, \
            "前置：同源/同目标/同意图共享同一 ho-* 卡"
        sid = own.state_card_id
        state_dir = sec.state_manager.data_dir

        sec.delete_session()

        assert os.path.exists(os.path.join(state_dir, f"{sid}.json")), \
            "来源 2（数据根下未删除交接卡）应独立可见 → 共享卡不得被删"
        assert os.path.exists(os.path.join(bus.data_dir, f"{std.id}.json")), \
            "独立使用者的交接卡不得被秘书删除"

    def test_source2_released_then_card_reclaimed(self, roots):
        """反向对照（防恒真）：来源 2 的交接卡本体删除后引用清空 → 共享卡可回收。

        与上一用例构成判别对：若索引对任何输入都"保留共享卡"，本断言 FAIL。
        """
        root, _ = roots
        bus = HandoffBus(data_dir=os.path.join(root, "queue"))
        std = bus.create_handoff("src2b-x", "src2b-y", "来源2回收意图", "ctx",
                                 auto_state_card=True)
        sec = SecretarySession("src2-b", data_dir=root)
        own = sec.create_handoff("src2b-x", "src2b-y", "来源2回收意图", "秘书ctx")
        sid = own.state_card_id
        state_dir = sec.state_manager.data_dir

        bus.delete(std.id)          # 卡本体 + 登记一并清 → 引用清空
        sec.delete_session()

        assert not os.path.exists(os.path.join(state_dir, f"{sid}.json")), \
            "引用清空后共享卡应可回收（无孤儿）"

    def test_large_context_card_visible_full_read(self, roots):
        """截断读取判别：``context`` 远大于常见读缓冲（200 KB）时，
        ``state_card_id`` 排在 context 之后——若实现截断读取（如
        ``f.read(65536)``）则引用不可见 → 共享卡误删。

        变异判别力：把 ``_maybe_handoff_card`` 的 ``raw = f.read()`` 改为
        ``f.read(65536)`` → 本断言 FAIL（已验证）。
        """
        root, _ = roots
        bus = HandoffBus(data_dir=os.path.join(root, "queue"))
        std = bus.create_handoff("tr-x", "tr-y", "截断意图", "A" * 200000,
                                 auto_state_card=True)
        card_path = os.path.join(bus.data_dir, f"{std.id}.json")
        with open(card_path, encoding="utf-8") as f:
            raw = f.read()
        assert raw.index('"state_card_id"') > 65536, \
            "前置：state_card_id 须位于 64 KB 读缓冲之后（否则本断言无判别力）"
        for rd in (os.path.join(root, "handoff-refs"),
                   os.path.join(root, ".handoff-refs")):
            ref = os.path.join(rd, f"{std.id}.ref")
            if os.path.exists(ref):
                os.remove(ref)

        sec = SecretarySession("tr-a", data_dir=root)
        own = sec.create_handoff("tr-x", "tr-y", "截断意图", "秘书ctx")
        sid = own.state_card_id
        state_dir = sec.state_manager.data_dir
        sec.delete_session()

        assert os.path.exists(os.path.join(state_dir, f"{sid}.json")), \
            "大 context 交接卡须经**全文读取**可见 → 共享卡不得被删"

    def test_ledger_dead_session_contributes_no_reference(self, roots):
        """台账**存活校验**：归属台账对应的会话状态卡已不存在（会话已删）
        → 其 ``state_card_ids`` 不得贡献引用（否则永久阻塞共享卡回收）。

        变异判别力：移除 ``_build_ho_reference_index`` 中「该会话状态卡仍
        存在」守卫 → 本断言 FAIL（已验证）。
        """
        root, _ = roots
        sec = SecretarySession("live-b", data_dir=root)
        ho = sec.create_handoff("liver-x", "liver-y", "台账存活意图", "ctx")
        sid = ho.state_card_id
        # 构造"死会话"台账：state/<dead>.json 不存在
        with open(os.path.join(root, "handoffs", ".session-dead.owned"),
                  "w", encoding="utf-8") as f:
            json.dump({"session_id": "dead", "handoff_ids": [],
                       "state_card_ids": [sid]}, f)

        index = sec._build_ho_reference_index()
        assert "dead" not in index.get(sid, []), \
            "已删除会话的台账不得贡献存活引用标签"

        sec.delete_session()
        assert not os.path.exists(
            os.path.join(root, "state", f"{sid}.json")), \
            "死会话台账不得永久阻塞共享卡回收"

    def test_ledger_alive_session_contributes_reference(self, roots):
        """反向对照（防恒真）：台账对应会话**仍存活**（状态卡存在）时必须
        贡献引用——与上一用例构成判别对。

        变异判别力：若把台账来源整体删除（只认交接卡），本断言 FAIL。
        """
        root, _ = roots
        sec_alive = SecretarySession("alive", data_dir=root)  # 先建状态卡
        sec = SecretarySession("live-c", data_dir=root)
        ho = sec.create_handoff("livec-x", "livec-y", "台账存活意图2", "ctx")
        sid = ho.state_card_id
        with open(os.path.join(root, "handoffs", ".session-alive.owned"),
                  "w", encoding="utf-8") as f:
            json.dump({"session_id": "alive", "handoff_ids": [],
                       "state_card_ids": [sid]}, f)

        index = sec._build_ho_reference_index()
        assert "alive" in index.get(sid, []), \
            "存活会话台账须贡献引用（否则断言无判别力）"
        sec.delete_session()
        sec_alive.delete_session()

    def test_shape_requires_id_field(self, roots):
        """形状校验（🟡B 边界固化）：含 ``state_card_id`` + ``source_unit`` +
        ``target_unit`` 但**缺 ``id``** 的 JSON 不得被计入引用。

        变异判别力：把 ``_maybe_handoff_card`` 的 ``return '"id"' in raw``
        放宽为 ``return True`` → 本断言 FAIL（已验证）。
        """
        root, _ = roots
        sec = SecretarySession("shape-a", data_dir=root)
        os.makedirs(os.path.join(root, "misc"), exist_ok=True)
        with open(os.path.join(root, "misc", "pseudo.json"), "w",
                  encoding="utf-8") as f:
            json.dump({"source_unit": "a", "target_unit": "b",
                       "state_card_id": "ho-pseudo"}, f)

        index = sec._build_ho_reference_index()
        assert "ho-pseudo" not in index, \
            "缺 id 的 JSON 不得被误判为交接卡引用（形状校验收紧口径）"
        sec.delete_session()

    def test_shape_complete_card_counted(self, roots):
        """反向对照（防恒真）：含四字段的交接卡形状 JSON 必须计入。"""
        root, _ = roots
        sec = SecretarySession("shape-b", data_dir=root)
        os.makedirs(os.path.join(root, "misc"), exist_ok=True)
        with open(os.path.join(root, "misc", "real.json"), "w",
                  encoding="utf-8") as f:
            json.dump({"id": "misc-real", "source_unit": "a",
                       "target_unit": "b", "state_card_id": "ho-real"}, f)

        index = sec._build_ho_reference_index()
        assert "handoff:real" in index.get("ho-real", []), \
            "形状完整的交接卡引用必须计入（否则断言无判别力）"
        sec.delete_session()
