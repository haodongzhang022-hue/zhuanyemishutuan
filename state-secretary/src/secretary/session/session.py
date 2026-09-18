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
from typing import Callable, Dict, List, Optional, Any

from ..state import (
    StateManager, StateCard, TaskRecord,
    TaskState, TaskType,
)
from ..state.fold_engine import FoldEngine
from ..state.content_archive import ContentArchive
from ..context.assembler import ContextAssembler
from ..handoff.handoff import HandoffBus
from ..handoff.models import HandoffCard
from ..loop import LoopDecision, LoopGuard
from ..collab import (
    TWO_WAY_SPLIT_NOTE,
    cron_expr_for,
    CollabRules,
    CheckpointProbe,
    ProbeEvent,
    ProbeOutcome,
    StepBackoff,
    WaitPolicy,
    RunScopeGuard,
    RunDecision,
    load_rules,
)
from ..review import (
    ArbiterDecision,
    ConflictCard,
    ReviewArbiter,
    ScanContext,
    decide_scan,
    scan_commands,
)
from ..state import LockRegistry, LegacyLockScanner

# 扫描界递归时**候选**剪枝的重型/非数据目录名（防 data_dir 指向工作区根时全树
# 遍历，亦防其内无关 JSON 被误判为交接卡引用 → 永久阻塞共享卡回收）。
# 🔴 P0-1 / M-1 修复：**不按裸目录名剪枝**——名单中的名字（build/dist/env/venv
# 等）**以及隐藏目录名**（.git/.cache/.data/.state 等）完全可能承载真实交接卡
# 或被合法选作 data_dir，按名剪枝会让落在其中的真实交接卡不可见 → 共享卡被误删。
# 故剪枝前一律先做「保护子树判定」+「形状判定」，见
# ``SecretarySession._keep_scan_dir`` / ``_dir_holds_handoff_data``。
#
# 有界遍历上界（**唯一语义源**，形状判定与 os.walk 剪枝共用，勿各写一套）。
# 语义：``_SKIP_DIR_PROBE_DEPTH`` = 形状判定的**递归深度上界（含文件层）**——
# 被保留目录自身的内容 = 深度 1，其下一层子目录的内容 = 深度 2，依此类推。
# 故「卡目录相对被保留目录的最大层级」= ``_SKIP_DIR_PROBE_DEPTH - 1``；
# os.walk 侧按同一语义派生（相对层级 > 该上界即止步），两侧常量不得各写一套。
# 实测口径（M-2，卡目录相对被保留目录的层级）：2 层**可见**
# （``<root>/node_modules/a/b/card.json``）；3 层**不可见**
# （``<root>/node_modules/a/b/c/card.json``）。
_SKIP_DIR_PROBE_DEPTH = 3
_KEPT_SKIP_MAX_CARD_DIR_DEPTH = _SKIP_DIR_PROBE_DEPTH - 1

_SKIP_SCAN_DIRS = frozenset({
    "node_modules", "__pycache__", "venv", ".venv", "env",
    "dist", "build", "target", "site-packages", "vendor", "coverage",
})


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

    **循环熔断（Issue #14）**：每个会话秘书附带一只 ``LoopGuard``，以
    「轮次预算 + 非必要修复最多 1 次 + 静默交接」三道闸控制评审/修复往返；
    熔断后 ``create_handoff`` 拒绝唤醒式交接（须走 ``silent_handoff``），
    且预算状态跨实例持久化——换 agent 不复活循环。

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
        loop_dir: Optional[str] = None,
        max_rounds: Optional[int] = None,
        max_advisory_fix_rounds: Optional[int] = None,
        max_total_rounds: Optional[int] = None,
        max_stall_repeats: Optional[int] = None,
        review_dir: Optional[str] = None,
        retry_delay_sec: Optional[float] = None,
        locks_dir: Optional[str] = None,
        persistent_grace_sec: Optional[float] = None,
        collab_rules: Optional[CollabRules] = None,
        run_guard_dir: Optional[str] = None,
        rules_path: Optional[str] = None,
        probe_dir: Optional[str] = None,
        max_checks: Optional[int] = None,
    ):
        """初始化一个会话秘书。

        Args:
            session_id: 会话唯一 ID（Issue/PR/对话编号）
            objective: 会话总体目标
            unit_id: 所属行业单元
            role_name: 秘书角色名（默认 "secretary"）
            data_dir: 会话数据根目录（默认 <cwd>/data）。同一会话的状态卡、
                原文归档、交接卡、交接引用登记均收敛在此根之下派生
                （state/ content/ handoffs/ handoff-refs/）。
            archive_root: 原文归档根目录（默认 <data_dir>/content）。
            static_prefix: 静态前缀（角色骨架，可传给 ContextAssembler）。
            loop_dir: 循环熔断状态目录（默认 ``<数据根>/loop``）。
            max_rounds: 门禁（🔴）修复轮次上界。不传取**跨仓库规则包**（默认 3）。
            max_advisory_fix_rounds: 非必要（🟡）修复轮次上界。不传取规则包（默认 1）。
            max_total_rounds: 修复总轮次硬上界。不传取规则包（默认 5）。
            max_stall_repeats: 同一整改项指纹连续出现上界。不传取规则包（默认 2）。
            review_dir: 评审仲裁数据目录（默认 ``<数据根>/review``）。
            retry_delay_sec: **兼容参数**。不传时按跨仓库协同规则取阶梯首档
                （默认 10s）；传入则固定用该值（旧语义，不做阶梯）。
            locks_dir: 锁聚合台账目录（默认 ``<数据根>/locks``）。
                **所有锁统一登记在此**：卡片写租约 / 评审读锁 / 延迟锁，
                `locks_overview()` 一次看全"哪里要避开"。
            persistent_grace_sec: **兼容参数**。不传时取规则包（默认 180s）。
            collab_rules: **跨仓库协同规则包**（`CollabRules`）。不传时按
                `rules_path` → 环境变量 `CNB_COLLAB_RULES` → 从 `data_dir`
                向上查找 `shared/collab/collab-rules.yaml` 的顺序装载。
                规则包承载"agent 协同时的全局规则"：等待阶梯（
                10s→30s→60s→3min→6min→10min 到顶即停）、锁/熔断/审查范围/计费口径。
            rules_path: 显式规则包路径（优先于自动查找）。
            probe_dir: 检查点探测器数据目录（默认 ``<数据根>/probe``）。
            max_checks: 检查点探测次数上界。不传取规则包（默认 6 = 阶梯档位数）；
                **用尽即停止自动挂账**，交回人工/上游。
        """
        # 合法性预检
        if not re.match(r'^[A-Za-z0-9_-]+\Z', session_id):  # 🟡-4：尾锚 \Z 防尾换行
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

        # 跨仓库协同规则（Issue #14 追问）：agent 协同的**全局参数**一处定义。
        # 装载顺序：显式 rules_path → 环境变量 → 从会话数据根逐级向上找规则包
        # → 内置默认。规则包决定等待阶梯 / 锁窗口 / 熔断预算 / 审查范围 / 计费口径。
        self.collab_rules = (collab_rules if collab_rules is not None
                             else load_rules(rules_path, search_from=self._session_root))
        # 等待阶梯：显式传了 retry_delay_sec 就不走阶梯（旧语义，向后兼容）；
        # 未传则用规则包阶梯——等待从少到多，到顶（10 分钟）即停。
        self.backoff: Optional[StepBackoff] = (
            None if retry_delay_sec is not None else self.collab_rules.backoff)
        # 等待策略：把"等多久"翻译成"怎么等"——**等待交平台挂起，本地不做 CPU 轮询**
        # （回答"长时间等待会不会占用构建 CPU 计费"）。显式固定窗口时也成立。
        self.wait_policy = WaitPolicy(
            backoff=self.backoff or self.collab_rules.backoff,
            platform=self.collab_rules.platform,
        )
        if retry_delay_sec is not None:
            effective_delay = float(retry_delay_sec)
        else:
            effective_delay = self.collab_rules.backoff.delay_for(0)
        effective_grace = (float(persistent_grace_sec)
                           if persistent_grace_sec is not None
                           else self.collab_rules.lock_persistent_grace_sec)

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
        # 循环熔断守卫（Issue #14）：状态落 <数据根>/loop/<session_id>.json，
        # 与会话数据同根，随 delete_session 一并回收。
        self.loop_guard = LoopGuard(
            session_id=session_id,
            data_dir=loop_dir or os.path.join(self._session_root, "loop"),
            max_rounds=(self.collab_rules.max_rounds if max_rounds is None
                        else max_rounds),
            max_advisory_fix_rounds=(
                self.collab_rules.max_advisory_fix_rounds
                if max_advisory_fix_rounds is None else max_advisory_fix_rounds),
            max_total_rounds=(self.collab_rules.max_total_rounds
                              if max_total_rounds is None
                              else max_total_rounds),
            max_stall_repeats=(self.collab_rules.max_stall_repeats
                               if max_stall_repeats is None
                               else max_stall_repeats),
        )
        # 评审版本仲裁（Issue #14 补全机制）：只在开启评审时记录版本 +
        # 防同写锁 + 延迟重试 + 单冲突收口。状态落 <数据根>/review/，
        # 随 delete_session 一并回收。
        self.review_arbiter = ReviewArbiter(
            session_id=session_id,
            state_manager=self.state_manager,
            reviewer=role_name,
            data_dir=review_dir or os.path.join(self._session_root, "review"),
            retry_delay_sec=effective_delay,
            lock_registry=LockRegistry(
                session_id=session_id,
                data_dir=locks_dir or os.path.join(self._session_root, "locks"),
                ttl_sec=effective_delay,
                persistent_grace_sec=effective_grace,
                backoff=self.backoff,
            ),
            persistent_grace_sec=effective_grace,
            backoff=self.backoff,
        )
        # 锁聚合台账（同一本，供总览/清扫/外部模块登记；卡片写租约也镜像到此）
        self.lock_registry = self.review_arbiter.lock_registry
        # 检查点探测器（Issue #14 追问）：**非轮询**的"形成检查点 → 判断检查点 →
        # 状态变化/到点再触发"。探测点一律落到**平台语义**（cnb:await / crontab /
        # api_trigger），本地绝不 sleep；最多 6 次（= 等待阶梯档位数），用尽交回人工。
        # 锁视图直接接**锁聚合台账**（探测只看可判定事实，不看 agent 自述）。
        self.probe = CheckpointProbe(
            session_id=session_id,
            data_dir=probe_dir or os.path.join(self._session_root, "probe"),
            backoff=self.backoff or self.collab_rules.backoff,
            max_checks=(self.collab_rules.probe_max_checks
                        if max_checks is None else max_checks),
            lock_view=self.lock_registry.overview,
            # 落点分工阈值 + 固定 cron 间隔都来自**规则包**（组织级共享同一份）：
            # ≤ 此值挂 cnb:await，> 此值交平台 crontab（固定 */5 一档扫）。
            await_max_sec=self.collab_rules.probe_await_max_sec,
            rules=self.collab_rules,
        )
        # 同口径重复执行守卫（Issue #14 本次追问）：**让"只跑增量、不连跑"成为机制**。
        # 日常默认 `scan`（增量）；同一口径（范围/版本/受影响面/命令）只跑一次，
        # 第 2 次起 `skip_duplicate`——跑法固定，重复跑不产生判别力（只重复烧构建/测试）。
        # 「连跑 N 次同一命令」当稳定性论证 → `blocked_stability` + 替代口径。
        self.run_guard = RunScopeGuard(
            rules=self.collab_rules,
            data_dir=run_guard_dir or os.path.join(self._session_root, "run-scope"),
        )
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
            m = re.match(r'^[A-Za-z0-9_-]+-seg(\d+)\Z', ref)
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
        if '"source_unit"' not in raw or '"target_unit"' not in raw:
            return False
        # 交接卡必有 id 字段；StateCard 有 session_id 无 source_unit/target_unit，
        # 归档/台账亦不含这些字段，故三者不会误入
        return '"id"' in raw

    # ------------------------------------------------------------------
    # 扫描剪枝（P0-1 修复：不按裸目录名判定）
    # ------------------------------------------------------------------

    def _protected_scan_dirs(self) -> List[str]:
        """返回**永不剪枝**的保护子树绝对路径清单。

        P0-1 根因：剪枝曾按**裸目录名**（``_SKIP_SCAN_DIRS``）判定，而名单中的
        名字（``build``/``dist``/``env``/``venv``/``target``/``vendor``/
        ``coverage`` 等）完全可能被合法选作 ``data_dir``——此时落在其中的真实
        交接卡被整片跳过 → 引用不可见 → 共享 ``ho-*`` 卡被误删、存活引用静默
        失效（实测 11/12 悬空）。故无论名字如何，**数据根本身及其结构性子目录
        （state / handoffs / content / handoff-refs）所在子树永不剪枝**。
        """
        dirs = [
            os.path.abspath(self._session_root),
            os.path.abspath(self.state_manager.data_dir),
            os.path.abspath(self.handoff_bus.data_dir),
            os.path.abspath(self.archive.root_dir),
        ]
        for reg_dir in self.handoff_bus.ref_registry_dirs():
            dirs.append(os.path.abspath(reg_dir))
        # 去重保序
        out: List[str] = []
        for d in dirs:
            if d not in out:
                out.append(d)
        return out

    def _keep_scan_dir(self, path: str, name: str,
                       protected_dirs: List[str],
                       kept_skip_dirs: set) -> bool:
        """判断递归扫描时是否**保留**该子目录（True=进入，False=剪枝）。

        判定顺序：
        1. 保护子树（或其祖先）：**永不剪枝**——即使名字命中名单；
        2. 名字命中 ``_SKIP_SCAN_DIRS``（``build``/``node_modules``/``.venv``
           等）**或为隐藏目录**（``.staging``/``.cache``/``.git`` 等）：**先做
           形状判定**（``_dir_holds_handoff_data``）——目录内实际存在交接卡
           形状的 JSON 则保留（引用必须可见），否则剪枝（保持原性能收益：
           真重型目录如 ``node_modules``、真无关目录如 ``.git`` 仍被跳过）。
           被保留的目录记入 ``kept_skip_dirs``——其内引用按"仅指向本根真实
           状态卡"过滤（见 ``_build_ho_reference_index``），从而既不误剪
           真实/遗留卡，又不让伪 JSON 阻塞回收；
        3. 常规数据目录：保留。

        🔴B 复评修复：隐藏目录原为**裸名直接剪枝**（``if name.startswith(".")``），
        与名单目录"按裸名判定"同根因——根内隐藏 ``data_dir``（``.staging``/
        ``.cache``/``.data``/``.build`` 等）内的真实卡不可见 → 共享卡被误删。
        现与名单目录同一口径（形状判定后再剪），不再按裸名判定。
        """
        abspath = os.path.abspath(path)
        # 1) 保护子树及其祖先不剪枝
        for p in protected_dirs:
            if abspath == p or p.startswith(abspath + os.sep):
                return True
        # 2) 名单目录（`build`/`node_modules`/`.venv` 等）**与全部隐藏目录**
        #    （`.staging`/`.cache`/`.git` 等）：同一「形状判定后再剪」口径。
        #    防"剪枝名 / 裸点前缀名承载真实/遗留交接卡"被误剪（P0-1；🔴B 复评：
        #    原实现对隐藏目录**直接按裸名剪枝**，同根因未清——根内隐藏 data_dir
        #    + 双通道登记不可用时，其内真实卡不可见 → 共享卡被误删、不变量破坏）。
        #    `.git` 等真无关目录仍被剪枝（形状判定有界且经廉价预筛，无交接卡
        #    形状 JSON 即剪），保留原性能收益。
        if name in _SKIP_SCAN_DIRS or name.startswith("."):
            if self._dir_holds_handoff_data(path):
                kept_skip_dirs.add(abspath)
                return True
            return False
        # 3) 常规目录保留
        return True

    def _exceeds_kept_skip_depth(self, dirpath: str, kept_skip_dirs: set) -> bool:
        """``dirpath`` 是否已超出"保留的名单目录"子树的有界遍历深度。

        防"保留目录内命中一张（伪）卡后，整棵 node_modules / 隐藏树被全树
        遍历"——`dirpath` 相对保留根的层级 > ``_KEPT_SKIP_MAX_CARD_DIR_DEPTH``
        （= ``_SKIP_DIR_PROBE_DEPTH - 1``）即止步。**上界与形状判定共用同一
        语义源**（M-2），两处常量不得各写一套。

        实测口径（M-2）：`<root>/node_modules/a/b/`（卡目录相对层级 2）**可见**；
        `<root>/node_modules/a/b/c/`（相对层级 3）**不可见**。

        **多根重叠（🔴A 复评修复）**：``dirpath`` 可同时落在多个保留根之下
        （嵌套名单目录/隐藏目录各自入列），此时取**最小深度**判定——不得
        取"首个前缀命中"（set 迭代顺序随哈希变化 → 非确定性破坏不变量）。
        """
        if not kept_skip_dirs:
            return False
        abspath = os.path.abspath(dirpath)
        # 🔴A 复评修复：`kept_skip_dirs` 为 set，原实现「首个前缀命中即 return」
        # 取的是**集合迭代顺序**的第一个前缀，而非最近根 → 当 `dirpath` 同时
        # 落在多个保留根之下（如 `<root>/build` 与 `<root>/build/dist` 双双
        # 因形状判定入列）时，深度上界判定随路径哈希（`PYTHONHASHSEED`）变化
        # → 同一布局时而截断、时而放行 → 不变量「存活交接卡 → 其状态卡必须
        # 存在」非确定性破坏。改为取**所有匹配根的最小深度**，去除顺序依赖。
        best = None
        for r in kept_skip_dirs:
            if abspath == r:
                return False
            if abspath.startswith(r + os.sep):
                depth = len(abspath[len(r) + 1:].split(os.sep))
                if best is None or depth < best:
                    best = depth
        # M-2：上界与形状判定共用同一语义源（`_SKIP_DIR_PROBE_DEPTH - 1`
        # = 可见卡目录的最大相对层级），避免两处常量各自漂移导致
        # 「形状判定保留、遍历却剪掉」的不自洽。
        return best is not None and best > _KEPT_SKIP_MAX_CARD_DIR_DEPTH

    @staticmethod
    def _under_any(path: str, roots: set) -> bool:
        """``path`` 是否等于或位于 ``roots`` 中任一目录之下。"""
        abspath = os.path.abspath(path)
        for r in roots:
            if abspath == r or abspath.startswith(r + os.sep):
                return True
        return False

    def _dir_holds_handoff_data(self, path: str,
                                max_depth: int = _SKIP_DIR_PROBE_DEPTH,
                                max_entries: int = 2000) -> bool:
        """浅扫：该目录子树内是否存在交接卡形状的 JSON（有界深度/预算）。

        用于 ``_SKIP_SCAN_DIRS`` 名单目录的剪枝前判定。**有界**（深度
        ``max_depth``、访问条目 ``max_entries``）以免对真正的重型目录
        （``node_modules`` 深层依赖树）做全树遍历而失去剪枝意义；命中即保留
        该子树，交由常规递归扫描按形状校验精确收集引用。

        残留边界：嵌套深度超过 ``max_depth`` 的卡仍不可见（见契约文档 §3）。
        """
        budget = [max_entries]

        def _scan(d: str, depth: int) -> bool:
            if depth > max_depth or budget[0] <= 0:
                return False
            try:
                with os.scandir(d) as it:
                    entries = list(it)
            except OSError:
                return False
            dirs = []
            for entry in entries:
                if budget[0] <= 0:
                    return False
                budget[0] -= 1
                try:
                    if entry.is_file(follow_symlinks=False):
                        if (entry.name.endswith(".json")
                                and not entry.name.startswith(".")
                                and self._maybe_handoff_card(entry.path)):
                            return True
                    elif entry.is_dir(follow_symlinks=False):
                        dirs.append(entry.path)
                except OSError:
                    continue
            for sub in dirs:
                if _scan(sub, depth + 1):
                    return True
            return False

        return _scan(path, 1)

    def _build_ho_reference_index(self) -> Dict[str, List[str]]:
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

        剪枝（🔴 P0-1 修复：**不再按裸目录名判定**）：
        - **保护子树永不剪枝**——会话数据根 / ``state`` / ``handoffs`` /
          ``content`` / ``handoff-refs`` 及其祖先（``_protected_scan_dirs``）；
        - 隐藏目录（``.git`` 等）剪枝；
        - 原文归档根剪枝（归档条目永不含 ``state_card_id``）；
        - 名字命中 ``_SKIP_SCAN_DIRS``（``node_modules``/``__pycache__``/
          ``venv``/``env``/``dist``/``build``/``target``/``site-packages``/
          ``vendor``/``coverage``）的目录**须先过形状判定**
          （``_dir_holds_handoff_data``，有界浅扫：深度 ≤ 3、条目 ≤ 2000）——其内实际存在交接卡形状
          JSON 则**不得剪枝**，否则落在该类名字下（含被合法选作 ``data_dir``）
          的真实/遗留交接卡不可见 → 共享卡被误删（实测 11/12 悬空）。

        **剪枝歧义名覆盖（P0-1）**：``build``/``dist``/``env``/``venv`` 等名字
        完全可能被合法选作 ``data_dir``。修复后此类布局下的交接卡引用**可见**
        （既包括 ``SecretarySession`` 注入的 ``handoffs/``，也包括独立
        ``HandoffBus(data_dir=<root>/build)`` 的手写/遗留卡）；仅当目录内确无
        交接卡形状 JSON 时才剪枝，保留原性能收益。

        形状校验（来源 2）：文件须同时含 ``state_card_id`` + ``source_unit`` +
        ``target_unit`` + ``id`` 才计入（交接卡必带 ``id``；StateCard 只有
        ``session_id``、归档/台账均无这些字段，故不会误入）。

        **可见性保证范围（P0-1 / P1-1 修复后更新）**：

        - **前置条件（原 🟡#1 收窄）**：经 ``HandoffBus`` API 写入的卡恒带
          ``id``；其引用可见性要求 ① ``state_root`` 与会话数据根**同根**
          （否则卡目录落在扫描界之外），**或** ② 登记写入成功。
        - **剪枝名单歧义名（原 🟡A）已修复**：不再按裸目录名剪枝，名单目录内
          实际存在交接卡即计入 → 手写/遗留卡落在 ``build``/``dist``/``env``
          等目录下同样可见。
        - **登记失败不再静默（原 🟡E / P1-1）已修复**：登记失败且为**唯一**
          可见性路径（``data_dir`` 在 ``state_root`` 子树之外）→ 建卡
          **fail-fast 回滚**；子树内由目录扫描兜底。故「API 写入路径保证」
          在登记失败场景下亦成立，🟡E 缺口闭环。
        - **仍属已知边界（非 API 路径）**：手写/外部遗留交接卡若**缺 ``id``
          字段**（形状校验必含 ``id``，🟡B）则不可见。改走 ``HandoffBus`` API
          即可覆盖；口径详见
          ``docs/design/state-secretary-handoff-visibility.md``（Issue #8）。

        **保守方向（不变量优先）**：凡扫描界内**实际存在**的交接卡/登记一律
        计入引用，**不做**"排除自身认领卡"处理。调用方（``delete_session``）
        在取索引前已先删除本会话交接卡（🔴#1），故自身卡通常已不在磁盘；若
        某张自身卡**删除失败**仍留在盘上，它就是一个**真实的存活引用**——
        此时排除它会让共享卡被误删、而该残卡仍持有失效引用，破坏不变量
        「存活交接卡 → 其 ``state_card_id`` 状态卡必须存在」。**宁可多保留
        （引用未清 → 不删共享卡），绝不误删**。

        **回收语义（🟡C 措辞更正，非行为变更）**：不存在"延迟回收"或"自动
        回收"路径——残卡留盘期间共享卡**永久留盘**，不会被任何后续会话自动
        触发回收；须**显式清理残卡**（删除该交接卡文件）后，由后续
        ``delete_session`` 再次判定引用已清，才能回收共享卡。故正确表述为
        「**不误删；残卡需显式清理**」，而非"延迟回收"。

        引用者标签：会话引用记会话 ID；交接卡引用记 ``handoff:<card_id>``，
        两类语义可区分。

        一次遍历同时建立全量索引，删除路径不再"每张卡各扫一遍目录"，
        复杂度由 O(卡数 × 台账数) 降为 O(台账数 + 卡数)。
        """
        index: Dict[str, List[str]] = {}
        state_dir = os.path.abspath(self.state_manager.data_dir)
        scan_root = os.path.abspath(self._session_root)
        archive_root = os.path.abspath(self.archive.root_dir)
        if not os.path.isdir(scan_root):
            return index
        protected_dirs = self._protected_scan_dirs()

        def _add(scid, label: str) -> None:
            if not isinstance(scid, str) or not scid:
                return
            bucket = index.setdefault(scid, [])
            if label not in bucket:
                bucket.append(label)

        prefix, suffix = ".session-", ".owned"
        kept_skip_dirs: set = set()
        for dirpath, dirnames, filenames in os.walk(scan_root):
            # 剪枝（P0-1 修复：**不按裸目录名判定**）：
            #   - 保护子树（会话数据根 / state / handoffs / content / handoff-refs）
            #     及其祖先永不剪枝；
            #   - 隐藏目录（.git 等）剪枝；
            #   - 名字命中 _SKIP_SCAN_DIRS 的目录**须先过形状判定**——其内
            #     实际存在交接卡（手写/遗留/独立 data_dir 落在此类名字下）则
            #     不得剪枝，否则引用不可见 → 共享卡被误删。
            # 保留的名单目录子树**有界遍历**（与形状判定同深度上界，防"命中
            # 一张卡后整棵 node_modules 被全树遍历"）：深度超上界即止步。
            if self._exceeds_kept_skip_depth(dirpath, kept_skip_dirs):
                dirnames[:] = []
                continue
            dirnames[:] = [
                d for d in dirnames
                if self._keep_scan_dir(
                    os.path.join(dirpath, d), d, protected_dirs, kept_skip_dirs)
            ]
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
                # 名单目录（本应剪枝、因形状判定而保留）内的引用：仅当它指向
                # **本根 state 目录下真实存在**的状态卡时才计入——既让真实/
                # 遗留卡可见（P0-1），又不让重型目录内无关 JSON 的伪引用
                # 永久阻塞共享卡回收（保留剪枝初衷）。
                if (self._under_any(dirpath, kept_skip_dirs)
                        and not os.path.exists(
                            os.path.join(state_dir, f"{scid}.json"))):
                    continue
                _add(scid, f"handoff:{card_id}")
        # 来源 3：state 根级引用登记（覆盖交接卡目录在会话根之外的独立使用者）
        #
        # 🔴 P0-2 修复：``isdir → listdir`` 之间存在 TOCTOU 窗口（并发的
        # ``HandoffBus.delete`` → ``_unregister_ref`` 会回收空登记目录）。
        # 原实现让 ``listdir`` 的 ``FileNotFoundError`` 逃逸出
        # ``delete_session``，中断于回收中途，留下 ho-*.json/.lock、归档、
        # 台账残骸（实测注入 100%、自然并发 3/2160）。此处整段以
        # ``try/except OSError`` 守卫：登记目录消失即视为"无登记引用"。
        # 双通道（主 handoff-refs/ + 备 .handoff-refs/，🟡E 闭环）：任一生效即引用可见。
        for reg_dir in self.handoff_bus.ref_registry_dirs():
            ref_names: List[str] = []
            try:
                ref_names = os.listdir(reg_dir)
            except OSError:
                # 登记目录在 isdir 与 listdir 之间被并发回收 → 该通道无登记引用
                ref_names = []
            for fname in ref_names:
                if not fname.endswith(".ref"):
                    continue
                data = self._read_json_dict(os.path.join(reg_dir, fname))
                if data is None:
                    continue
                card_id = data.get("card_id")
                if not isinstance(card_id, str) or not card_id:
                    continue
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
        """.. deprecated:: 使用 ``_build_ho_reference_index`` 直接取索引。

        兼容包装（保留历史调用点，勿在新代码使用）：返回仍存活引用该
        ho-* 卡的引用者标签列表。

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

        **扫描界（🔴#4 修复）**：引用扫描界不再只到 ``handoff_bus.data_dir``
        单目录，而是提升到**会话数据根**（递归，剪枝隐藏目录与原文归档根），
        并并入 state 根级引用登记 ``<state_root>/handoff-refs/*.ref``（覆盖
        交接卡目录完全在会话数据根之外的独立使用者）。否则"独立使用者交接
        目录 ≠ 会话 handoffs/、但 state 根相同"时，其交接卡引用不在扫描界内
        → 判定"无存活引用"→ 删共享卡 → 存活引用静默失效（🔴#4）。

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
        #    不再传"自身卡排除"：自身交接卡已在上一步撤销；若撤销失败而残卡
        #    仍在磁盘，它即真实存活引用，须计入以守住"存活卡 → 状态卡存在"不变量。
        #
        #    🔴 P0-2 / 🟡2 修复（第二道防线）：扫描本身若抛异常（**全异常族**，
        #    不止 OSError），一律**保守跳过回收**（宁可不删，绝不误删）——扫描
        #    不可用时无法证明"引用已清"，不得据此删除共享卡；且异常不得中断
        #    后续归档/台账回收（原实现异常逃逸 → delete_session 半途而废、
        #    留 ho-*.json/.lock、归档、台账残骸）。
        reference_index: Optional[Dict[str, List[str]]] = None
        try:
            reference_index = self._build_ho_reference_index()
        except Exception:  # noqa: BLE001
            # 🟡2 修复（异常族收口）：原实现只兜 ``OSError``，注入
            # ``TypeError``/``RuntimeError`` 等**非 OSError** 异常仍会逃逸
            # ``delete_session`` → 归档/台账清理被跳过（实测逃逸 + 归档残留）。
            # 契约 §2「扫描异常不得逃逸」此处收口为**兜全部异常**：扫描不可用
            # 一律保守跳过回收（宁可不删，绝不误删），且不中断后续归档/台账清理。
            reference_index = None
        if reference_index is not None:
            for sid in ho_cards:
                self._delete_ho_state_card_if_safe(sid, reference_index)
        self._owned_state_card_ids.clear()

        # 5) 清理本会话的原文归档（并回收 <content>/<session>/ 空子目录）
        try:
            self.archive.delete_session(self.session_id)
        except (OSError, ValueError):
            pass
        # 6) 清理本会话循环熔断状态（Issue #14）：会话生命周期结束时不留
        #    预算文件，避免同一 session_id 复用时带上陈旧熔断/轮次计数。
        try:
            self.loop_guard.delete_state()
        except OSError:
            pass
        # 7) 清理本会话评审基线/冲突卡（Issue #14 补全机制）：同上，避免同
        #    一 session_id 复用时带上陈旧基线与待处理冲突。
        try:
            self.review_arbiter.delete_state()
            self.lock_registry.delete_state()
        except OSError:
            pass
        # 8) 清理本会话检查点（Issue #14 追问）：同上，避免同一 session_id 复用时
        #    带上陈旧检查点（"最多 6 次"的预算不得被上一轮继承）。
        try:
            self.probe.delete_state()
        except OSError:
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
            card.add_task(
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
        summary = card.summarize_for_upstream()
        # 循环预算随汇总上行（Issue #14）：上级/审计据 ``wake_allowed`` 判定
        # 「是否还可能被自动唤醒继续修」，无需回读会话本地状态。
        summary["loop"] = self.loop_guard.status()
        summary["wake_allowed"] = self.loop_guard.should_wake_next()
        summary["locks"] = self.locks_overview()
        # 协同规则随汇总上行（Issue #14 追问）：上级一眼知道"下一档等多久、
        # 到没到顶、等待按不按 CPU 计费"，不必回读会话本地状态。
        summary["collab"] = self.collab_status()
        return summary

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
        wake: bool = True,
    ) -> HandoffCard:
        """创建一张交接卡（跨单元协作），自动关联本会话 StateCard。

        **共享卡语义后果（显式约定，🟡#3）**：ho-* 状态卡 ID =
        ``hash(source_unit, target_unit, intent)``——同源/同目标/同意图的多个
        会话**共享同一张 ho-* 卡**。共享卡的交接任务只按标题去重，因此**后到者
        传入的 ``context`` 不会写入该卡**（仅首到者的 context 落卡），后到者
        上下文**静默丢弃**。这是"共享卡"设计的既定语义（避免同一交接任务在卡内
        重复堆积）；需要保留各自上下文的场景请使用不同 ``intent``（→ 不同卡），
        或把上下文写入自己的任务卡/原文归档。

        **唤醒闸门（Issue #14）**：本会话已**熔断**时 ``wake=True`` 被拒绝
        （``RuntimeError``）——熔断语义就是"不再唤醒下一轮"，自动/误写的唤醒式
        交接会复活循环。熔断后须用 ``silent_handoff()``（``wake=False``）。
        """
        if wake and self.loop_guard.is_fused():
            raise RuntimeError(
                f"会话 {self.session_id} 已熔断"
                f"（{self.loop_guard.status()['fused_reason']}）："
                "禁止唤醒式交接（wake=True），请改用 silent_handoff()。")
        hc = self.handoff_bus.create_handoff(
            source_unit=source_unit,
            target_unit=target_unit,
            intent=intent,
            context=context,
            auto_state_card=True,
            wake=wake,
            fuse_reason=(self.loop_guard.status()["fused_reason"] if not wake else ""),
        )
        if hc and hc.id:
            # 认领本交接卡 + 其关联 ho-* StateCard（落盘归属台账，跨实例可清）
            self._record_ownership(hc.id, hc.state_card_id)
        return hc

    # ------------------------------------------------------------------
    # 循环熔断（Issue #14：轮次预算 + 非必要修复最多 1 次 + 静默交接）
    # ------------------------------------------------------------------

    def review_round(self, blocking: int = 0, advisory: int = 0,
                     note: str = "", signature: str = "") -> LoopDecision:
        """登记一轮评审/复评结果，返回本轮处置与是否允许唤醒下一轮。

        Args:
            blocking: 🔴/门禁必修项数
            advisory: 🟡/非门禁（无必要）项数
            note: 留痕说明（如打回方、修复摘要）
            signature: 本轮整改项**指纹**（如打回项集合的稳定摘要）。与上一轮
                相同且达 ``max_stall_repeats`` → 判**停滞循环**直接熔断
                （"感知循环"：同一问题反复报，不必等轮次预算耗尽）

        Returns:
            ``LoopDecision``：``action`` ∈ ``clear``（无需修复、不唤醒）/
            ``fix``（门禁，预算内）/ ``fix_last``（非必要，最多 1 次）/
            ``fuse``（熔断，不唤醒）；``fused_now=True`` 表示本次刚熔断，
            调用方据此只建**一次**静默交接卡。
        """
        return self.loop_guard.register_round(
            blocking=blocking, advisory=advisory, note=note,
            signature=signature)

    @staticmethod
    def review_signature(items) -> str:
        """由整改项清单生成稳定指纹（供 ``review_round(signature=…)`` 使用）。

        排序 + 归一化空白后哈希 → 同一批问题换顺序/换空白再报也判为**停滞**。
        """
        return LoopGuard.signature_of(items)

    def preview_round(self, blocking: int = 0, advisory: int = 0,
                      signature: str = "") -> LoopDecision:
        """预览下一轮处置（**纯函数**：不消耗预算、不落盘）。"""
        return self.loop_guard.preview(
            blocking=blocking, advisory=advisory, signature=signature)

    def loop_status(self) -> dict:
        """循环预算与熔断态快照。"""
        return self.loop_guard.status()

    def should_wake_next(self) -> bool:
        """当前是否允许唤醒下一轮（熔断后恒 False）。"""
        return self.loop_guard.should_wake_next()

    def fuse_loop(self, reason: str) -> bool:
        """显式叫停循环（人工/上游直接熔断）。"""
        return self.loop_guard.fuse(reason)

    def resume_loop(self, by: str, reason: str = "") -> bool:
        """人工解锁熔断（须留痕 ``by``；这是恢复修复预算的**唯一**途径）。"""
        return self.loop_guard.resume(by=by, reason=reason)

    def silent_handoff(
        self,
        source_unit: str,
        target_unit: str,
        intent: str,
        context: str = "",
        open_items: Optional[List[str]] = None,
    ) -> HandoffCard:
        """熔断后的**静默交接**：建 ``wake=False`` 交接卡，接收方只登记不唤醒。

        与 ``create_handoff(..., wake=False)`` 等价，额外把熔断原因与轮次快照
        一并写入卡面 ``context``（若调用方未提供 context），让下一 agent 一眼
        看到"为何不得再开一轮"，不依赖它自行推理。
        """
        payload = self.loop_guard.silent_handoff_payload(
            open_items=open_items, target_unit=target_unit)
        if not context:
            context = json.dumps(payload, ensure_ascii=False, indent=2)
        return self.create_handoff(
            source_unit=source_unit,
            target_unit=target_unit,
            intent=intent,
            context=context,
            wake=False,
        )

    def inherit_silent_handoff(self, handoff_context: Any) -> bool:
        """接收静默交接（``wake=False``）后**继承熔断态**：本会话起手即不唤醒。

        返回 True 表示确实继承到熔断态（载荷为静默交接）；False 表示载荷并非
        静默交接（常规接力，无变化）。载荷无法解析时不熔断（宽进不炸）。
        """
        fused = LoopGuard.from_handoff(
            session_id=self.session_id,
            data_dir=self.loop_guard.data_dir,
            handoff_context=handoff_context,
            max_rounds=self.loop_guard.max_rounds,
            max_advisory_fix_rounds=self.loop_guard.max_advisory_fix_rounds,
            max_total_rounds=self.loop_guard.max_total_rounds,
            max_stall_repeats=self.loop_guard.max_stall_repeats,
        )
        self.loop_guard = fused
        return fused.is_fused()

    # ------------------------------------------------------------------
    # 评审版本仲裁（Issue #14 补全机制）
    # 只在开启评审时记录版本 + 防同写锁 + 延迟重试 + 单冲突收口
    # ------------------------------------------------------------------

    def begin_review(self, force: bool = False) -> Any:
        """**开启评审**：把当前版本原子登记为评审基线（幂等）。

        开发过程中**不**记录任何版本；只有开启评审时才登记。已登记且
        ``force=False`` 时不改写基线（防止靠重新开启评审把基线偷偷推前）。
        """
        return self.review_arbiter.begin_review(force=force)

    def review_baseline(self) -> Optional[dict]:
        """当前评审基线（未开启评审 → ``None``）。"""
        base = self.review_arbiter.get_baseline()
        return base.to_dict() if base else None

    def review_recorded(self) -> bool:
        """是否已在开启评审时登记过版本。"""
        return self.review_arbiter.recorded()

    def rebase_review(self) -> dict:
        """以当前版本为新基线（"无法追踪变化就以新的"）。"""
        return self.review_arbiter.rebase().to_dict()

    def apply_review(
        self,
        mutate: Optional[Callable[[StateCard], None]] = None,
        note: str = "",
    ) -> ArbiterDecision:
        """写回评审结果（**先完成该做的**），再单独判冲突。

        Returns:
            ``ArbiterDecision``：``applied`` 为 True 表示评审已落盘；
            ``should_retry`` 为 True 表示卡正在被编辑，等 ``retry_delay_sec``
            后调 ``retry_review()``；``conflict`` 为 True 时 ``card`` 是
            **唯一**冲突卡（后续冲突合并进它）。
        """
        return self.review_arbiter.apply_save(mutate=mutate, note=note)

    def retry_review(
        self,
        mutate: Optional[Callable[[StateCard], None]] = None,
        note: str = "",
    ) -> ArbiterDecision:
        """延迟窗到点后重试评审写回（窗未到则继续等待，不落盘）。"""
        return self.review_arbiter.retry(mutate=mutate, note=note)

    def pending_review_conflicts(self) -> List[ConflictCard]:
        """待处理评审冲突卡（**至多一张**：单冲突收口）。"""
        return self.review_arbiter.pending_conflicts()

    def resolve_review_conflict(
        self, ticket_id: str, resolution: str, by: str = "",
    ) -> bool:
        """收口那张冲突卡（**只额外处理一个**），收口后以当前版本重立基线。"""
        return self.review_arbiter.resolve(ticket_id, resolution, by=by)

    def complete_deferred_review(
        self,
        mutate: Optional[Callable[[StateCard], None]] = None,
        note: str = "",
    ) -> ArbiterDecision:
        """「先做其他」做完后回来收尾：补做被延迟的那次评审，成功才打开锁。"""
        return self.review_arbiter.complete_deferred(mutate=mutate, note=note)

    def deferred_review_status(self) -> Optional[dict]:
        """当前「先做其他，并挂锁」项：锁还剩多久、对方动没动、是否已升级持续锁。"""
        return self.review_arbiter.deferred_status()

    # ------------------------------------------------------------------
    # 跨仓库协同规则（Issue #14 追问：agent 协同时的全局规则）
    # ------------------------------------------------------------------

    def collab_status(self) -> dict:
        """跨仓库协同规则的**生效快照**："这套数值从哪来、现在是多少"。

        一眼回答三个问题：
        - 等待阶梯是什么（从少到多、到顶即停）；
        - 锁/熔断/审查范围用的是哪套预算；
        - **等待算不算 CPU 计费**（平台口径）。
        """
        rules = self.collab_rules
        return {
            "source": rules.source,
            "waiting_plan": rules.waiting_plan(),
            "waiting_ladder": rules.waiting_ladder(),
            "waiting": (self.backoff.status(0).to_dict()
                        if self.backoff is not None else None),
            "locks": {
                "retry_delay_sec": self.review_arbiter.retry_delay_sec,
                "persistent_grace_sec": self.review_arbiter.persistent_grace_sec,
                "registry_shared": rules.lock_registry_shared,
            },
            "loop": {
                "max_rounds": self.loop_guard.max_rounds,
                "max_advisory_fix_rounds": self.loop_guard.max_advisory_fix_rounds,
                "max_total_rounds": self.loop_guard.max_total_rounds,
                "max_stall_repeats": self.loop_guard.max_stall_repeats,
            },
            "scan": {
                "major_version_tags": list(rules.major_version_tags),
            },
            "platform_cost": rules.platform.to_dict(),
        }

    def waiting_plan(self) -> str:
        """等待计划（一行）：``10s → 30s → … → 600s（到顶即停）``。"""
        if self.backoff is None:      # 显式固定窗口（旧语义）
            return f"固定延迟 {self.review_arbiter.retry_delay_sec:.0f}s（未启用阶梯）"
        return self.backoff.human()

    def waiting_status(self, retriggered: int = 0) -> Optional[dict]:
        """当前位置的等待状态：第几档、等多久、是否到顶/已停。"""
        return self.review_arbiter.waiting_status(retriggered)

    def cost_note(self) -> dict:
        """**等待是否占用 CPU 计费**：平台口径原话（供所有角色统一答复）。"""
        return self.collab_rules.platform.to_dict()

    def wait_decision(self, attempt: int = 0, *, editing: bool = True) -> dict:
        """该"怎么等"（第 ``attempt`` 次检测）。

        三选一，**永不选"本地空转"**：`platform_suspend`（挂平台等）/
        `deferred_review`（先做其他）/ `resume_required`（到顶交回人工）。
        """
        return self.wait_policy.decide(attempt, editing=editing).to_dict()

    def wait_plan(self, attempts: Optional[int] = None) -> List[dict]:
        """整条等待计划（每次到点怎么等），可直接贴进讨论/文档。"""
        return self.wait_policy.plan(attempts)

    # ------------------------------------------------------------------
    # 锁聚合管理（Issue #14：所有锁统一登记，触发即可查看，绝不遗留）
    # ------------------------------------------------------------------

    def locks_overview(self) -> dict:
        """**所有锁**的聚合总览：「哪里要避开」一眼可见（不必靠测试才知道）。

        汇总三处：
        - 锁聚合台账（卡片写租约 / 评审读锁 / 延迟锁 / 序列化锁）；
        - 历史遗留单锁文件（``<数据根>/state/*.owner.json``，只读上报）；
        - 「先做其他，并挂锁」当前项（若有）。
        `by_kind` 按类型计数，`where_to_avoid` 是给人看的避开清单。
        """
        view = self.lock_registry.overview()
        legacy = LegacyLockScanner.scan(self.lock_registry.data_dir)
        if not legacy:
            legacy = LegacyLockScanner.scan(self.state_manager.data_dir)
        view["legacy_locks"] = legacy
        view["deferred"] = self.review_arbiter.deferred_status()
        view["where_to_avoid"] = list(view.get("where_to_avoid", [])) + [
            f"{l.get('session_id')}（legacy 写租约｜持有者 "
            f"{l.get('holder') or '未标注'}｜{'已过期' if l.get('expired') else '仍有效'}）"
            for l in legacy
        ]
        return view

    def sweep_locks(self, by: str = "") -> List[dict]:
        """清理**遗留锁**（过期且超宽限仍未处理）→ 置 stale + 留痕。

        不误杀活跃锁与宽限内的持续锁；返回被回收的锁清单。
        """
        return self.review_arbiter.sweep_locks(by=by or self.role_name)

    def release_review_locks(self, by: str = "", reason: str = "评审结束") -> int:
        """释放本会话**评审相关**的锁（评审读锁 + 延迟锁），不动他人写租约。"""
        return self.review_arbiter.release_locks(by=by, reason=reason)

    def review_scope(
        self,
        tags: Any = None,
        text: str = "",
        major_version: bool = False,
    ) -> ScanContext:
        """审查范围分级：**日常开发只审变化，全量只在大版本**。

        用于防止"每轮往返都触发全量构建 + 全量测试"的无效作业：非大版本一律
        ``scan``（只审变更集与受影响面），仅大版本标签/显式标记才放 ``full``。
        """
        return decide_scan(tags=tags, text=text, major_version=major_version)

    def review_scope_commands(self, **kwargs) -> Dict[str, str]:
        """按范围档位给出构建/测试执行口径（``scan`` 档明确不跑全量）。"""
        return scan_commands(self.review_scope(**kwargs))

    # ------------------------------------------------------------------
    # 同口径重复执行守卫（Issue #14 本次追问：只跑增量、不连跑同一口径）
    # ------------------------------------------------------------------

    def run_decision(
        self,
        mode: str = "",
        revision: str = "",
        impact: Any = None,
        command: str = "",
        last_verdict: str = "",
        window_repeats: int = 1,
    ) -> RunDecision:
        """**跑不跑**：日常默认增量；同一口径（范围/版本/受影响面/命令）只跑一次。

        - 同一口径第 2 次起 → ``skip_duplicate``（跑法固定，重复跑不产生判别力）；
        - 想"连跑 N 次"当稳定性论证 → ``blocked_stability`` + 替代口径
          （定点/变异探针、独立场景）；
        - 例外：同口径已达上界但上一轮**不绿** → 允许重跑（红→修→再跑有新信息）。
        """
        return self.run_guard.decide(
            mode=mode, revision=revision, impact=impact, command=command,
            last_verdict=last_verdict, window_repeats=window_repeats)

    def record_run(
        self,
        mode: str = "",
        revision: str = "",
        impact: Any = None,
        command: str = "",
        verdict: str = "",
    ):
        """登记一次**实际执行**（只记真的跑了的；被跳过的重复不计数）。"""
        return self.run_guard.record(mode=mode, revision=revision, impact=impact,
                                     command=command, verdict=verdict)

    def run_repeats_of(self, mode: str = "", revision: str = "",
                       impact: Any = None, command: str = "") -> int:
        """某口径已执行几次（"连跑三次"这类行为会被一眼看见）。"""
        return self.run_guard.repeats_of(mode=mode, revision=revision,
                                         impact=impact, command=command)

    def run_scope_overview(self) -> Dict[str, Any]:
        """口径台账总览：跑了几次、有没有**超出同口径上界**的重复执行。"""
        return self.run_guard.overview()

    def run_scope_note(self) -> str:
        """一行口径（评论/文档/角色提示词共用）：**只跑增量、同口径只跑一次**。"""
        r = self.collab_rules
        return (f"日常只跑增量（默认 {r.scan_default_mode}）；同一口径"
                f"（范围/版本/受影响面/命令）最多跑 {r.max_identical_repeats} 次，"
                f"第 2 次起跳过；稳定性论证请换口径"
                f"（{'/'.join(r.stability_via)}），不连跑同一命令；"
                f"全量（full）只在大版本")

    # ------------------------------------------------------------------
    # 检查点探测器（Issue #14 追问：非轮询 → 检查点 → 状态变化再触发）
    # ------------------------------------------------------------------

    def create_checkpoint(
        self,
        key: str,
        kind: str = "lock",
        holder: str = "",
        scope: str = "",
        await_key: str = "",
        max_checks: Optional[int] = None,
        note: str = "",
    ):
        """建一个**检查点**：等什么（``kind``）、等谁（``holder``）、还能探几次。

        **幂等**：同键已存在即原样返回（不重置预算）——否则"每次遇到都重建"
        等于把"最多 6 次"变成无限次，正是要治的循环。
        """
        return self.probe.create(key=key, kind=kind, holder=holder, scope=scope,
                                 await_key=await_key, max_checks=max_checks,
                                 note=note)

    def checkpoint_plan(self, key: str = "", **kwargs) -> List[Dict[str, Any]]:
        """六次探测**怎么等**：等待阶梯 + 平台落点（await / crontab），非轮询。"""
        cp = self.probe.load(key) if key else None
        return self.probe.delay_plan(cp)

    def register_platform_wait(self, key: str, await_key: str = "", sn: str = "",
                               driver: str = "platform_await", note: str = ""):
        """登记"等待已挂到平台语义上"（``cnb:await``）→ 本地零占用，可被回收。"""
        return self.probe.register_platform_wait(
            key, await_key=await_key, sn=sn, driver=driver, note=note)

    def probe_checkpoint(self, key: str, **kwargs) -> ProbeOutcome:
        """**探测一次**（采集轻量事实 → 判断检查点 → 记预算 → 给下一步落点）。

        ``our_need_satisfied=False`` 表示"我方已不需要这个锁" → 直接判 ``release``
        （放锁、不再等），避免队头阻塞。
        """
        return self.probe.probe(key, **kwargs)

    def dispatch_probe_event(self, event: ProbeEvent) -> Dict[str, Any]:
        """把探测事件转成**给流水线的平台指令**（``cnb:await`` / ``cnb:apply`` /
        ``crontab`` / 交回人工），并标明**是否允许据此唤醒下一轮**。"""
        return self.probe.dispatch(event)

    def checkpoints_overview(self) -> Dict[str, Any]:
        """所有检查点总览（含"还剩几次探测、下一次等多久、挂在哪"）。"""
        return self.probe.overview()

    def probe_status(self) -> Dict[str, Any]:
        """检查点探测的**生效口径快照**：次数上界、平台落点、判别式、计费口径。

        含三点已确认口径：① 落点分工（`await_max_sec` 阈值 + 不可混用）；
        ② cron **固定 `*/5` 一档扫**；③ 规则来源层级（组织级 / 仓库级，见 `chain`）。
        """
        rules = self.collab_rules
        return {
            "max_checks": self.probe.max_checks,
            "waiting_plan": rules.waiting_plan(),
            "probe_plan": rules.probe_plan(),
            "drivers": list(rules.probe_drivers),
            "checks": list(rules.probe_checks),
            "await_max_sec": self.probe.await_max_sec,
            "crontab_min_interval_sec": rules.crontab_min_interval_sec,
            "crontab_interval": cron_expr_for(rules),
            "crontab_interval_note": ("固定一档扫：不随阶梯档位放大；"
                                      "与阶梯时间表解耦（平台多久代跑一次）"),
            "split_note": TWO_WAY_SPLIT_NOTE,
            "rules_scope": rules.scope,
            "rules_chain": [dict(c) for c in rules.chain],
            "local_cpu_cost": "0（探测点挂平台语义；本地不 sleep、不轮询）",
            "platform_cost": rules.platform.to_dict(),
        }

    def organization_probe_rules(self) -> Dict[str, Any]:
        """**组织级共享**的 probe 口径快照（跨仓同一份；已确认口径 #3）。"""
        from ..collab import organization_probe_rules as _org
        return _org(rules=self.collab_rules)

    def dispatch_probe_event_shared(self, event: Any) -> Dict[str, Any]:
        """**跨仓**落点判定：同一份组织级规则包 → 同一套动作（不依赖本地会话号）。"""
        from ..collab import dispatch_shared
        return dispatch_shared(event, self.collab_rules)

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
