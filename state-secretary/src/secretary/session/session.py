"""SecretarySession：秘书会话门面（Facade）

将状态管理秘书机制的所有底层组件整合为一个可供 L3 会话秘书直接使用的
统一入口。每个 SecretarySession 实例 = 一个 L3 会话秘书，绑定一个 session_id。

整合组件：
  - StateManager    → 状态卡生命周期管理
  - FoldEngine      → 折叠/编号/检索
  - ContentArchive  → 原文归档（折叠后不丢原文）
  - ContextAssembler→ 三层上下文装配（稳定前缀 + 缓存命中）
  - HandoffBus      → M5 交接卡（跨单元协作）

核心设计原则：
  1. 一会话一秘书：session_id 即对话/Issue/PR 唯一标识，环境隔离。
  2. 自动化优先：任务折叠默认自动，无需手动干预。
  3. 原文不丢：折叠后原文可按 content_ref 精确找回。
  4. 稳定前缀：静态上下文层不随会话长度漂移，缓存命中率高。
"""
from __future__ import annotations

import json
import os
import re
import tempfile
import time
from typing import Dict, List, Optional, Set, Any

from ..state import (
    StateManager, StateCard, TaskRecord,
    TaskState, TaskType,
)
from ..state.fold_engine import FoldEngine
from ..state.content_archive import ContentArchive
from ..context.assembler import ContextAssembler
from ..handoff.handoff import HandoffBus
from ..handoff.models import HandoffCard


class _StateCardProxy:
    """StateCard 的**装配视图代理**：暴露 compact_context() 供 ContextAssembler 层B使用。

    ContextAssembler._build_layer_b 优先调用 state_card.compact_context() 来生成
    "会话态上下文"（展开任务要素 + 折叠任务编号引用）。StateCard 本身是数据模型，
    不耦合 FoldEngine 折叠逻辑；由门面在此注入 FoldEngine 代理，使层B能真正携带
    任务标题/折叠引用，而非只落到"目标/进度/会话"的降级摘要。
    """

    def __init__(self, card: StateCard, fold_engine: FoldEngine):
        self._card = card
        self._fold_engine = fold_engine

    def compact_context(self) -> str:
        return self._fold_engine.compact_context(self._card)


class SecretarySession:
    """会话秘书门面：一个 L3 会话秘书的完整接口。

    用法：
        sec = SecretarySession("issue-42")
        # 新建任务
        sec.add_task("设计状态 Schema")
        # 折叠一段原始上下文并编号
        sec.fold("需求澄清：Q1 四层秘书定义…", meta={"objective": "澄清需求"})
        # 获取当前会话的压缩上下文（供模型驱动下一轮）
        ctx = sec.get_compact_context()
        # 更新任务状态
        sec.mark_task("issue-42-0001", "in_progress")
        # 向 L2 状态管理秘书提交汇总
        summary = sec.summarize_for_upstream()
    """

    def __init__(
        self,
        session_id: str,
        objective: str = "",
        unit_id: str = "",
        role_name: str = "secretary",
        data_dir: Optional[str] = None,
        archive_root: Optional[str] = None,
        static_prefix: Optional[str] = None,
    ):
        """初始化一个会话秘书。

        Args:
            session_id: 会话唯一 ID（Issue/PR/对话编号）
            objective: 会话总体目标
            unit_id: 所属行业单元
            role_name: 秘书角色名（默认 "secretary"）
            data_dir: 会话数据根目录（默认 <cwd>/data）。同一会话的状态卡、
                原文归档、交接卡均收敛在此根之下派生（state/ content/ handoffs/）。
            archive_root: 原文归档根目录（默认 <data_dir>/content）。
            static_prefix: 静态前缀（角色骨架，可传给 ContextAssembler）。
        """
        # 合法性预检
        if not re.match(r'^[A-Za-z0-9_-]+$', session_id):
            raise ValueError(
                f"非法 session_id: {session_id!r}。仅允许 [A-Za-z0-9_-]。")

        self.session_id = session_id
        self.unit_id = unit_id
        self.role_name = role_name
        self.objective = objective
        self.static_prefix = static_prefix

        # 统一数据根：同一会话的状态卡/原文归档/交接卡收敛到同一根之下，
        # 避免"数据根分裂"（状态卡在 A、归档在 B、交接卡在 C）。
        # 默认 <cwd>/data，传入 data_dir 时以之为根。
        self._session_root = data_dir or os.path.join(os.getcwd(), "data")
        state_dir = os.path.join(self._session_root, "state")
        # archive_root 显式给出则使用；否则从数据根派生 content
        self._archive_root = archive_root or os.path.join(
            self._session_root, "content")
        ho_dir = os.path.join(self._session_root, "handoffs")

        # 初始化底层组件
        self.state_manager = StateManager(data_dir=state_dir)
        self.fold_engine = FoldEngine(
            session_id=session_id,
            unit_id=unit_id,
            role_name=role_name,
            archive_root=self._archive_root,
        )
        self.archive = ContentArchive(root_dir=self._archive_root)
        self.context_assembler = ContextAssembler(
            static_prefix_provider=(
                lambda u, r: static_prefix if static_prefix else None
            ) if static_prefix else None,
        )
        self.handoff_bus = HandoffBus(data_dir=ho_dir, state_root=self._session_root)
        # 记录本会话创建/认领过的交接卡 ID，供 delete_session 一并清理。
        # 跨实例归属以 data_dir 下的归属台账（handoffs/.session-<id>.owned）
        # 持久化记录为准，实例内存清单仅为会话内缓存镜像。
        self._owned_handoff_ids: List[str] = []
        # 本会话经交接 auto_state_card 关联的 ho-* StateCard ID
        # （delete_session 一并回收这些状态卡，消除跨根孤儿）
        self._owned_state_card_ids: List[str] = []

        # 确保 StateCard 已创建（get_or_create）
        self._ensure_card()
        # 恢复本会话归属台账（跨实例：上一实例创建的交接卡/ho-* 卡纳入清理）
        self._load_ledger()

    # ------------------------------------------------------------------
    # 内部工具
    # ------------------------------------------------------------------

    def _ensure_card(self) -> StateCard:
        """确保 StateCard 存在，不存在则创建"""
        return self.state_manager.get_or_create(
            session_id=self.session_id,
            objective=self.objective,
            unit_id=self.unit_id,
            role_name=self.role_name,
        )

    def _card(self) -> Optional[StateCard]:
        """获取当前会话的 StateCard"""
        return self.state_manager.get_card(self.session_id)

    @staticmethod
    def _max_content_seq(card: Optional[StateCard]) -> int:
        """取任务卡中已有 content_ref 的最大段号（无则 0）。

        用于续号：{session}-seg{NNN} 中 NNN 的最大值。
        """
        max_seq = 0
        if card is None:
            return max_seq
        for t in card.tasks:
            ref = t.content_ref or ""
            m = re.match(r'^[A-Za-z0-9_-]+-seg(\d+)$', ref)
            if m:
                max_seq = max(max_seq, int(m.group(1)))
        return max_seq

    def _own_state_card(self, state_card_id: str) -> None:
        """登记一张本会话负责回收的 StateCard（去重保留）。"""
        if state_card_id and state_card_id not in self._owned_state_card_ids:
            self._owned_state_card_ids.append(state_card_id)

    def _ledger_path(self) -> str:
        """本会话交接卡归属台账路径。

        存放于本会话 handoffs/ 目录下（<root>/handoffs/.session-<id>.owned）。
        持久化记录"哪些交接卡 + 关联 ho-* 状态卡属于本会话"，供跨实例
        重开后 delete_session 仍能一次回收；不扫全部卡文件、不误认领
        其他会话的交接卡（多会话同数据根时互不越权）。

        注意：文件后缀刻意**不带 .json**——handoff 目录的 *.json 会被
        list_pending() 当作交接卡解析，台账不应被误读为 HandoffCard。
        """
        return os.path.join(
            self.handoff_bus.data_dir,
            f".session-{self.session_id}.owned")

    def _record_ownership(self, handoff_id: str,
                          state_card_id: Optional[str]) -> None:
        """登记一张交接卡（及其关联 ho-* 状态卡）属于本会话，落盘台账。"""
        if handoff_id and handoff_id not in self._owned_handoff_ids:
            self._owned_handoff_ids.append(handoff_id)
        if state_card_id:
            self._own_state_card(state_card_id)
        self._flush_ledger()

    def _flush_ledger(self) -> None:
        """把本会话交接卡归属台账写入磁盘（幂等、无锁单写者）。"""
        ledger = {
            "session_id": self.session_id,
            "handoff_ids": list(self._owned_handoff_ids),
            "state_card_ids": list(self._owned_state_card_ids),
            "updated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        }
        path = self._ledger_path()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        fd, tmp_path = tempfile.mkstemp(
            dir=os.path.dirname(path), suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(ledger, f, ensure_ascii=False, indent=2)
            os.replace(tmp_path, path)
        except Exception:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise

    def _load_ledger(self) -> None:
        """会话实例启动时读取本会话归属台账，恢复跨实例所有权。

        🔴#3 类型守卫：台账文件可能是**合法 JSON 但顶层非对象**（如 ``["x"]``）
        或字段类型不符（``handoff_ids`` 非数组）。此类文件一律视为**空台账**，
        不得因 ``data.get`` 触发裸 ``AttributeError`` 中断 ``delete_session``。
        """
        path = self._ledger_path()
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, ValueError):
            return
        if not isinstance(data, dict):
            # 非对象 JSON（数组/字符串/数字/布尔/null）→ 视为空台账，不崩
            return
        handoff_ids = data.get("handoff_ids")
        for hid in handoff_ids if isinstance(handoff_ids, list) else []:
            if isinstance(hid, str) and hid not in self._owned_handoff_ids:
                self._owned_handoff_ids.append(hid)
        state_card_ids = data.get("state_card_ids")
        for sid in state_card_ids if isinstance(state_card_ids, list) else []:
            if isinstance(sid, str):
                self._own_state_card(sid)

    def _owned_handoffs(self) -> List[str]:
        """汇总本会话需要清理的交接卡 ID（内存清单 ∪ 台账权威清单）。"""
        self._load_ledger()
        return list(self._owned_handoff_ids)

    @staticmethod
    def _read_json_dict(path: str) -> Optional[dict]:
        """读取 JSON 文件，仅当顶层为**对象**时返回，否则 None（🔴#3 守卫）。"""
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, ValueError):
            return None
        return data if isinstance(data, dict) else None

    @staticmethod
    def _maybe_handoff_card(path: str) -> bool:
        """廉价预筛：仅当文件文本同时出现 ``state_card_id`` 与
        ``source_unit``/``target_unit`` 字段名时才值得 ``json.loads``。

        🔴#4 修复后扫描界提升到会话数据根，根下同时存在大量 StateCard
        （``state/*.json``，其字段名是 ``session_id``、**不含**
        ``state_card_id``）。预筛令这些文件只做一次文本包含检查、不做 JSON
        解析，保持删除路径的近线性开销。

        全文读取（不截断）：``HandoffCard`` 的 ``state_card_id`` 字段排在
        ``context`` 之后，截断读取会在 context 很大时漏判，故必须读全文。
        """
        try:
            with open(path, "r", encoding="utf-8") as f:
                raw = f.read()
        except (OSError, ValueError):
            return False
        if '"state_card_id"' not in raw:
            return False
        return '"source_unit"' in raw or '"target_unit"' in raw

    def _build_ho_reference_index(
        self,
        exclude_handoff_ids: Optional[Set[str]] = None,
    ) -> Dict[str, List[str]]:
        """一次扫描建立 ``state_card_id -> 存活引用者标签`` 索引。

        **扫描界 = 本会话数据根 ``self._session_root``（递归）**，而非仅本会话
        ``handoff_bus.data_dir``（🔴#4 修复）。原因：删除目标 ho-* 卡落在
        **共享 state 目录**（``<root>/state``），而独立 ``HandoffBus`` 使用者的
        交接卡目录可为其任意 ``data_dir``（如 ``<root>/queue``）。若只扫本会话
        ``handoffs/``，异目录交接卡的 ``state_card_id`` 不被计入 → 秘书
        ``delete_session()`` 误删共享卡、存活交接卡引用静默失效。故扫描界提升到
        会话数据根，递归收集根下任意子目录的交接卡与归属台账。

        存活引用来源 = **并集**（🔴#2 修复：不再只认归属台账）：

        1. 数据根下**其他存活会话**的归属台账 ``.session-<id>.owned``：仅当该
           会话自身 StateCard（``state/<id>.json``）仍存在（尚未 ``delete_session``）
           时计入；
        2. 数据根下**未删除交接卡**（非隐藏 ``*.json``，且具交接卡形状：含
           ``state_card_id`` 与 ``source_unit``/``target_unit``，经廉价预筛）的
           ``state_card_id`` 字段——覆盖不经 ``SecretarySession`` 门面、
           因而**不落归属台账**、且**目录可与本会话不同**的独立
           ``HandoffBus(data_dir=…)`` 使用者；
        3. **state 根级引用登记** ``<state_root>/handoff-refs/*.ref``：交接卡
           目录可完全落在会话数据根**之外**（如 ``data_dir=/else/queue`` 且
           ``state_root=<root>``），此时来源 2 的目录扫描不可达。登记与 ho-* 卡
           同根、随卡回收，且记录 ``card_path``——仅当该交接卡仍在盘上时才计入，
           卡删除即引用失效。

        剪枝：跳过隐藏目录、跳过原文归档根（归档条目永不含 ``state_card_id``）。

        引用者标签：会话引用记会话 ID；交接卡引用记 ``handoff:<card_id>``，
        两类语义可区分。本会话自身台账/交接卡不计入——调用方
        （``delete_session``）须**先撤销自身引用**再取本索引（🔴#1）。

        一次遍历同时建立全量索引，删除路径不再"每张卡各扫一遍目录"，
        复杂度由 O(卡数 × 台账数) 降为 O(台账数 + 卡数)。

        Args:
            exclude_handoff_ids: 额外排除的交接卡 ID（如本会话已认领但删除
                失败的卡，避免自身残卡被误判为"他人的存活引用"）。
        """
        index: Dict[str, List[str]] = {}
        state_dir = os.path.abspath(self.state_manager.data_dir)
        scan_root = os.path.abspath(self._session_root)
        archive_root = os.path.abspath(self.archive.root_dir)
        if not os.path.isdir(scan_root):
            return index

        def _add(scid, label: str) -> None:
            if not isinstance(scid, str) or not scid:
                return
            bucket = index.setdefault(scid, [])
            if label not in bucket:
                bucket.append(label)

        own_handoff_ids = set(self._owned_handoff_ids)
        own_handoff_ids |= set(exclude_handoff_ids or ())
        prefix, suffix = ".session-", ".owned"
        for dirpath, dirnames, filenames in os.walk(scan_root):
            # 剪枝：隐藏目录（如 .git）与原文归档根（归档条目不含引用字段）
            dirnames[:] = [d for d in dirnames if not d.startswith(".")]
            if os.path.abspath(dirpath) == archive_root:
                dirnames[:] = []
                continue
            for fname in filenames:
                path = os.path.join(dirpath, fname)
                if not os.path.isfile(path):
                    continue
                # 来源 1：其他会话归属台账（隐藏文件，后缀刻意非 .json）
                if fname.startswith(prefix) and fname.endswith(suffix):
                    other_sid = fname[len(prefix):-len(suffix)]
                    if not other_sid or other_sid == self.session_id:
                        continue
                    # 会话自身已删除 → 其台账引用失效，不阻塞共享卡回收
                    if not os.path.exists(
                            os.path.join(state_dir, f"{other_sid}.json")):
                        continue
                    data = self._read_json_dict(path)
                    if data is None:
                        continue  # 🔴#3：非对象/损坏台账视为空台账
                    sids = data.get("state_card_ids")
                    if not isinstance(sids, list):
                        continue
                    for scid in sids:
                        _add(scid, other_sid)
                    continue
                # 来源 2：未删除交接卡（非隐藏 .json）
                if fname.startswith(".") or not fname.endswith(".json"):
                    continue
                card_id = fname[:-5]
                if card_id in own_handoff_ids:
                    continue  # 本会话自身引用，不计入
                # 预筛 + 形状校验：仅"交接卡"（含 state_card_id 且具
                # source_unit/target_unit）计入，避免把 StateCard / 租约文件
                # 等误判为引用者，也不为其做无谓 JSON 解析。
                if not self._maybe_handoff_card(path):
                    continue
                data = self._read_json_dict(path)
                if data is None:
                    continue
                scid = data.get("state_card_id")
                if not isinstance(scid, str) or not scid:
                    continue
                _add(scid, f"handoff:{card_id}")
        # 来源 3：state 根级引用登记（覆盖交接卡目录在会话根之外的独立使用者）
        reg_dir = self.handoff_bus.ref_registry_dir()
        if reg_dir and os.path.isdir(reg_dir):
            for fname in os.listdir(reg_dir):
                if not fname.endswith(".ref"):
                    continue
                data = self._read_json_dict(os.path.join(reg_dir, fname))
                if data is None:
                    continue
                card_id = data.get("card_id")
                if not isinstance(card_id, str) or not card_id:
                    continue
                if card_id in own_handoff_ids:
                    continue  # 本会话自身引用，不计入
                scid = data.get("state_card_id")
                if not isinstance(scid, str) or not scid:
                    continue
                # 卡已删除 → 登记失效（stale），不计入，避免永久阻塞回收
                card_path = data.get("card_path")
                if not isinstance(card_path, str) or not os.path.exists(card_path):
                    continue
                _add(scid, f"handoff:{card_id}")
        return index

    def _ho_has_live_references(
        self,
        state_card_id: str,
        reference_index: Optional[Dict[str, List[str]]] = None,
    ) -> bool:
        """共享 ho-* 卡是否仍被其他存活引用者引用（引用未清 → 不得删）。"""
        if not state_card_id:
            return False
        index = (reference_index if reference_index is not None
                 else self._build_ho_reference_index())
        return bool(index.get(state_card_id))

    def _scan_ho_referencing_sessions(self, state_card_id: str) -> List[str]:
        """兼容包装：返回仍存活引用该 ho-* 卡的引用者标签列表。

        标签语义与 ``_build_ho_reference_index`` 一致——会话引用为会话 ID，
        交接卡引用为 ``handoff:<card_id>``（两类可区分）。空列表 = 引用已清、
        可回收；调用方须**先撤销自身引用**再取（🔴#1）。
        """
        return list(self._build_ho_reference_index().get(state_card_id, []))

    def _delete_ho_state_card_if_safe(
        self,
        state_card_id: str,
        reference_index: Optional[Dict[str, List[str]]] = None,
    ) -> bool:
        """删除一张 ho-* StateCard，删除前执行"存活引用检查"。

        🔴#1 前提：调用方须**先撤销本会话自身引用**（本会话交接卡 + 归属台账
        + 本会话状态卡）再调用本方法；🔴#2 引用源为「其他存活会话台账 ∪
        未删除交接卡 state_card_id」。仅当索引中无任何存活引用者（引用已清）
        才物理删除；引用未清不得删——否则另一会话持有的引用会失效（P2-①）。
        """
        if not state_card_id:
            return False
        if self._ho_has_live_references(state_card_id, reference_index):
            # 仍有其他存活引用者引用该共享卡 → 保留，供其继续使用
            return False
        try:
            return self.state_manager.delete_card(state_card_id)
        except (OSError, ValueError):
            return False

    # ------------------------------------------------------------------
    # 会话生命周期
    # ------------------------------------------------------------------

    def get_card(self) -> Optional[StateCard]:
        """获取当前 StateCard"""
        return self._card()

    def delete_session(self) -> bool:
        """清理本会话的全部数据（状态卡 + 原文归档 + 交接卡 + 关联 ho-* 状态卡）。

        生命周期语义完整：一次 delete 即回收该会话秘书占用的全部存储，
        不残留 .lock / .owner.json / 空目录 / 跨实例交接卡 / 交接自动关联的
        孤儿状态卡。

        **删除顺序（🔴#1 修复）**：先**撤销本会话自身引用**（本会话交接卡 →
        本会话状态卡 → 归属台账），**再**判定共享 ho-* 卡的存活引用并回收。
        原顺序（先判定、后撤销）在并发/交错删除下，两个引用者都会在各自判定
        时看到对方"仍存活"而双双跳过删除，使共享卡连同 ``.lock`` 成为无人
        回收的孤儿（TOCTOU）。前置撤销后，任一次判定都发生在自身引用清空
        之后——若某次判定仍看到他人引用，则对方撤销必然更晚、其判定必然更晚，
        由最后撤销者完成回收，不存在双跳过。

        **引用源（🔴#2 修复）**：存活引用 = 其他存活会话归属台账 ∪ 未删除
        交接卡的 ``state_card_id``（覆盖不落台账的独立 ``HandoffBus`` 使用者）。

        **返回值语义（P2-③ 统一）**：返回「清理前本会话状态卡是否存在」，
        与 ``StateManager.delete_card`` 完全一致——
        - 状态卡存在 → 已删除，返回 ``True``（清理完成）；
        - 状态卡本已缺失（仅剩 .lock / .owner.json 等伴生残留）→ 仍会幂等
          清理全部伴生残留，但返回 ``False``（本会话已不存在）。
        返回值不代表"清理是否发生"，只代表"清理前会话是否仍存在"。
        """
        # 0) 先快照待回收清单（后续清空前取用）。注意 _owned_handoffs() 会
        #    顺带 _load_ledger() 补齐跨实例台账，须先调用再快照 ho-* 卡清单。
        owned_handoffs = list(self._owned_handoffs())
        ho_cards = list(self._owned_state_card_ids)

        # 1) 撤销自身引用之一：删除本会话"创建/认领过"的交接卡——含上一实例
        #    创建、本实例据台账恢复认领的卡（跨实例不留残余）
        for hid in owned_handoffs:
            try:
                self.handoff_bus.delete(hid)
            except OSError:
                pass
        self._owned_handoff_ids.clear()

        # 2) 撤销自身引用之二：清理本会话状态卡（连带 .lock / .owner.json
        #    伴生残留）。**须在存活判定之前完成**（🔴#1）。ok = delete_card
        #    返回"清理前状态卡是否存在"，即本 delete_session 的返回值。
        ok = self.state_manager.delete_card(self.session_id)

        # 3) 撤销自身引用之三：删除本会话归属台账（台账亦属本会话数据）
        try:
            ledger_path = self._ledger_path()
            if os.path.exists(ledger_path):
                os.remove(ledger_path)
        except OSError:
            pass

        # 4) 自身引用已全部撤销 → 一次扫描建立存活引用索引，回收已无引用的
        #    共享 ho-* 卡（引用未清不得删：P2-①；顺序 🔴#1 / 引用源 🔴#2）
        reference_index = self._build_ho_reference_index(
            exclude_handoff_ids=set(owned_handoffs))
        for sid in ho_cards:
            self._delete_ho_state_card_if_safe(sid, reference_index)
        self._owned_state_card_ids.clear()

        # 5) 清理本会话的原文归档（并回收 <content>/<session>/ 空子目录）
        try:
            self.archive.delete_session(self.session_id)
        except (OSError, ValueError):
            pass
        return ok

    # ------------------------------------------------------------------
    # 任务管理
    # ------------------------------------------------------------------

    def add_task(
        self,
        title: str,
        objective: str = "",
        task_type: str = "normal",
        depends_on: Optional[List[str]] = None,
        files: Optional[List[str]] = None,
        summary: str = "",
        **kwargs,
    ) -> Optional[TaskRecord]:
        """添加一个新任务到本会话。

        - normal（默认）→ 折叠态
        - deliverable → 展开态（末端交付任务）
        """
        return self.state_manager.add_task(
            session_id=self.session_id,
            title=title,
            objective=objective,
            task_type=task_type,
            depends_on=depends_on,
            files=files,
            summary=summary,
            **kwargs,
        )

    def mark_task(self, task_id: str, state: str) -> bool:
        """更新任务状态（todo / in_progress / done / blocked / stopped）"""
        return self.state_manager.update_task_state(
            self.session_id, task_id, state)

    def update_task(self, task_id: str, **fields) -> bool:
        """更新任务的任意字段（title/summary/objective 等）"""
        return self.state_manager.update_task_fields(
            self.session_id, task_id, **fields)

    def get_task(self, task_id: str) -> Optional[TaskRecord]:
        """按任务编号取任务"""
        card = self._card()
        return card.get_task(task_id) if card else None

    def list_tasks(self) -> List[TaskRecord]:
        """列出所有任务"""
        card = self._card()
        return list(card.tasks) if card else []

    def get_active_tasks(self) -> List[TaskRecord]:
        """获取当前 in_progress 的任务"""
        card = self._card()
        if not card:
            return []
        return [t for t in card.tasks
                if t.state == TaskState.IN_PROGRESS.value]

    def get_open_tasks(self) -> List[TaskRecord]:
        """获取所有未完成的任务（todo + in_progress + blocked）"""
        card = self._card()
        if not card:
            return []
        return [t for t in card.tasks
                if t.state in (TaskState.TODO.value,
                               TaskState.IN_PROGRESS.value,
                               TaskState.BLOCKED.value)]

    # ------------------------------------------------------------------
    # 折叠 / 编号 / 检索
    # ------------------------------------------------------------------

    def fold(
        self,
        content: str,
        meta: Optional[dict] = None,
    ) -> StateCard:
        """折叠一段原始上下文，编号归档。

        将原文按段落自动拆分，每段成为一个折叠任务并编号。
        原文全文归档到 ContentArchive，content_ref 作为索引可检索找回。

        Args:
            content: 要折叠的原始上下文全文
            meta: 附加元数据（objective/cur_dir/files 等）

        Returns:
            更新后的 StateCard（当前会话的完整状态卡）
        """
        meta = meta or {}
        card = self._ensure_card()

        # 若提供了 objective 但卡没有 objective，更新
        if meta.get("objective") and not card.objective:
            card.objective = meta["objective"]
            self.state_manager.save_card(card)

        # 调用 fold_engine 分段
        segments = self.fold_engine._split_segments(content)

        # 续号：基于既有任务卡最大 content_ref 段号 + 1，保证同一会话多次
        # 折叠编号连续、不覆盖既有归档文件，content_ref 与任务一一对应。
        start_seq = self._max_content_seq(card) + 1

        # 归档原文全文（从 start_seq 续号）
        refs = self.archive.store_segments(
            self.session_id, segments, start_seq=start_seq)
        # 去除空段后重新对齐
        segments = [s.strip() for s in segments if s.strip()]

        for i, seg in enumerate(segments):
            # 最后一段作为交付任务展开，其余折叠
            is_deliverable = (i == len(segments) - 1)
            task_type = (TaskType.DELIVERABLE.value if is_deliverable
                         else TaskType.NORMAL.value)

            # 确保 content_ref 有值
            content_ref = refs[i] if i < len(refs) else None

            # 添加到 StateManager 管理的现有 card
            task = card.add_task(
                title=seg.split("\n")[0][:80],
                objective=meta.get("objective", self.objective),
                summary=seg[:200] + ("..." if len(seg) > 200 else ""),
                task_type=task_type,
                content_ref=content_ref,
                files_touched=meta.get("files", []),
                tools_called=meta.get("tools", []),
                cur_dir=meta.get("cur_dir", ""),
            )

        self.state_manager.save_card(card)
        return card

    def get_compact_context(self) -> str:
        """获取当前会话的压缩上下文（驱动下一轮对话的 prompt 片段）。

        只携带展开任务要素 + 折叠任务编号引用，不含原文全文。
        """
        card = self._card()
        if card is None:
            return f"目标: {self.objective}" if self.objective else ""
        return self.fold_engine.compact_context(card)

    def get_navigation(self) -> str:
        """获取当前会话的任务导航视图（人类可读）"""
        card = self._card()
        if card is None:
            return f"(空会话 {self.session_id})"
        return self.fold_engine.render_navigation(card)

    def retrieve_original(self, content_ref: Optional[str]) -> Optional[str]:
        """按 content_ref 精确检索并取回原文全文。

        参数非法（None/空串/含非法字符）或归档不存在时返回 None；
        作为公共 API 不抛裸 TypeError/ValueError。
        """
        if not content_ref:
            return None
        try:
            return self.archive.retrieve_content(
                self.session_id, content_ref)
        except Exception:
            return None

    def search_tasks(self, keyword: str,
                     include_original: bool = False) -> List[dict]:
        """在本会话的所有任务中搜索关键词"""
        card = self._card()
        if card is None:
            return []
        return self.fold_engine.search_tasks(
            [card], keyword, include_original=include_original)

    def apply_fold_policy(self, current_task_id: str = None) -> Optional[StateCard]:
        """应用自动折叠策略：交付任务恒展开、当前执行任务展开、其余折叠。"""
        return self.state_manager.apply_fold_policy(
            self.session_id, current_task_id=current_task_id)

    # ------------------------------------------------------------------
    # 三层上下文装配（稳定前缀 + 缓存命中保障）
    # ------------------------------------------------------------------

    def assemble_context(
        self,
        user_text: str = "",
        static_prefix: Optional[str] = None,
    ) -> Dict[str, Any]:
        """对一次调用执行三层上下文装配。

        Returns dict 含 layer_a/layer_b/layer_c/full_context/cache_key 等。
        """
        card = self._card()
        # 传装配视图代理：让层B能经 FoldEngine 生成携带任务标题/折叠引用的
        # 会话态上下文（StateCard 本身无 compact_context 方法）。
        state_card = _StateCardProxy(card, self.fold_engine) if card else None
        result = self.context_assembler.assemble(
            session_id=self.session_id,
            role_name=self.role_name,
            unit_id=self.unit_id,
            user_text=user_text,
            static_prefix=static_prefix or self.static_prefix,
            state_card=state_card,
            objective=self.objective,
        )
        return result

    # ------------------------------------------------------------------
    # 层级向上提交
    # ------------------------------------------------------------------

    def summarize_for_upstream(self) -> dict:
        """生成向上级 L2 状态管理秘书提交的汇总状态。

        只携带结构化汇总字段（进度/决策/开放项），不含原始全文。
        """
        card = self._card()
        if card is None:
            return {
                "session_id": self.session_id,
                "objective": self.objective,
                "task_count": 0,
                "tasks": [],
            }
        return card.summarize_for_upstream()

    def set_upstream(self, upstream_session: str) -> bool:
        """设置上级秘书引用"""
        return self.state_manager.set_upstream(
            self.session_id, upstream_session)

    def add_downstream(self, downstream_session: str) -> bool:
        """添加下级秘书引用"""
        return self.state_manager.add_downstream(
            self.session_id, downstream_session)

    # ------------------------------------------------------------------
    # 交接（M5）
    # ------------------------------------------------------------------

    def create_handoff(
        self,
        source_unit: str,
        target_unit: str,
        intent: str,
        context: str = "",
    ) -> HandoffCard:
        """创建一张交接卡（跨单元协作），自动关联本会话 StateCard。

        **共享卡语义后果（显式约定，🟡#3）**：ho-* 状态卡 ID =
        ``hash(source_unit, target_unit, intent)``——同源/同目标/同意图的多个
        会话**共享同一张 ho-* 卡**。共享卡的交接任务只按标题去重，因此**后到者
        传入的 ``context`` 不会写入该卡**（仅首到者的 context 落卡），后到者
        上下文**静默丢弃**。这是"共享卡"设计的既定语义（避免同一交接任务在卡内
        重复堆积）；需要保留各自上下文的场景请使用不同 ``intent``（→ 不同卡），
        或把上下文写入自己的任务卡/原文归档。
        """
        hc = self.handoff_bus.create_handoff(
            source_unit=source_unit,
            target_unit=target_unit,
            intent=intent,
            context=context,
            auto_state_card=True,
        )
        if hc and hc.id:
            # 认领本交接卡 + 其关联 ho-* StateCard（落盘归属台账，跨实例可清）
            self._record_ownership(hc.id, hc.state_card_id)
        return hc

    # ------------------------------------------------------------------
    # 进度统计
    # ------------------------------------------------------------------

    def progress_stats(self) -> dict:
        """获取本会话的进度统计。"""
        card = self._card()
        if card is None:
            return {"session_id": self.session_id, "task_count": 0}
        summary = card.summarize_for_upstream()
        return {
            "session_id": self.session_id,
            "task_count": summary["task_count"],
            "todo": summary["todo_count"],
            "in_progress": summary["in_progress_count"],
            "done": summary["done_count"],
            "blocked": summary["blocked_count"],
            "stopped": summary["stopped_count"],
            "objective": card.objective or self.objective,
        }
