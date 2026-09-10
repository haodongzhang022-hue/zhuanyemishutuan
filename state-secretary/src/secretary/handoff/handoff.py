"""M5 跨单元交接总线"""
from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
import threading
import time
from typing import List, Optional

from .models import HandoffCard

# card_id 白名单（🔴 P1-3）：仅允许字母/数字/下划线/短横线。
# card_id 会参与 ``<data_dir>/<card_id>.json`` 与 ``<reg_dir>/<card_id>.ref``
# 路径拼接，未校验时 ``"../state/VICTIM"`` 之类的输入构成**路径穿越删除面**
# （可删 data_dir / handoff-refs 之外的任意文件，并可与 P0-1 连锁破坏不变量）。
# 尾锚用 ``\Z``（非 ``$``）：Python 的 ``$`` 匹配**尾换行之前**，会让
# ``"abc\n"`` 通过校验并进入路径拼接（🟡-4 收紧；同族 pattern 一并收紧）。
_CARD_ID_PATTERN = re.compile(r'^[A-Za-z0-9_-]+\Z')


# 引用登记目录（``handoff-refs/`` 与 ``.handoff-refs/``）的**建 / 写 / 删 /
# 空目录回收**互斥锁（🔴1）。模块级：不同 HandoffBus 实例共享同一 state_root
# （同一登记目录）时亦互斥，否则「_unregister_ref 空目录 rmdir」与
# 「_register_ref makedirs/mkstemp」交叠 → mkstemp 抛 FileNotFoundError →
# 双通道同时失败 → 登记静默丢失（或 fail-fast 误抛 OSError）。
_REF_REGISTRY_LOCK = threading.RLock()


def _is_valid_card_id(card_id: object) -> bool:
    """card_id 是否为合法标识符（拒绝路径分隔符/相对路径/空串）。"""
    return isinstance(card_id, str) and bool(_CARD_ID_PATTERN.match(card_id))


class HandoffBus:
    """
    M5 跨单元交接总线：
    管理跨行业/跨岗位交接卡的生命周期。
    交接卡采用 JSON 格式存储，便于后续扩展为异步消息。

    M9 集成：创建交接卡时若指定 auto_state_card=True，
    自动为交接创建/关联一张 StateCard（M9 状态卡）。

    数据根收敛：交接卡本体（handoffs/）与其关联的 StateCard 应同根——
    - data_dir:   交接卡 JSON 落盘目录
    - state_root: 关联 ho-* StateCard 的落盘根（显式传入时使用）
    - 默认推导：data_dir 显式传入且未指定 state_root 时，以 data_dir 父目录
      推导 state_root，使 ho-* 卡落 ``<parent(data_dir)>/state``，
      与 SecretarySession 数据根四子目录
      （state/ content/ handoffs/ handoff-refs/）收敛逻辑一致，
      不再落 `<cwd>/data/state` 孤儿；
      data_dir 亦未传时保持历史退化行为（<cwd>/data/handoffs + <cwd> 根）。
    SecretarySession 会同时注入本会话 data_dir 下的 state/ 与 handoffs/，
    使交接卡与其关联状态卡收敛到同一会话数据根，不再落 `<cwd>/data/state` 孤儿。

    引用登记（state 根级，P2-① 复评 🔴#4）：``auto_state_card=True`` 时，
    除落盘交接卡外，还在 ``<state_root>/handoff-refs/<card_id>.ref`` 登记
    「本交接卡引用了哪张 ho-* 状态卡」。该登记与 ho-* 卡同根、随卡回收，
    使**交接卡目录与状态根不同树**的独立使用者也能被存活引用检查发现
    （仅靠扫描会话数据根无法覆盖该布局）。删除交接卡时同步注销登记。

    **双通道登记（🟡E 闭环）**：登记同时写入**两个不同故障域**的目录——
    主 ``<state_root>/handoff-refs/`` 与备 ``<state_root>/.handoff-refs/``。
    单通道被同名文件占位 / 权限不足 / 单条 ``.ref`` 被外部删除时，另一通道
    仍让引用可见，避免"根外卡引用双路径皆不可见 → 共享卡误删"。仅当**双通道
    全部失败**且登记为该卡唯一可见性路径时，``create_handoff`` 才 fail-fast
    回滚（不再有静默悬空引用）。
    """

    def __init__(self, data_dir: Optional[str] = None,
                 state_root: Optional[str] = None):
        """初始化交接总线。

        Args:
            data_dir: 交接卡 JSON 落盘目录（默认 <cwd>/data/handoffs）
            state_root: 关联 ho-* StateCard 的状态根。默认推导规则——
                - data_dir 显式传入且未指定 state_root：以 ``data_dir`` 的
                  父目录作为状态根，ho-* 卡落 ``<parent(data_dir)>/state``，
                  与 SecretarySession 数据根四子目录
                  （state/ content/ handoffs/ handoff-refs/）收敛到同一数据根一致；
                - 仅 data_dir 缺省（用 <cwd>/data/handoffs）：状态根保持历史
                  行为（<cwd>），向后兼容；
                显式传入后 auto_state_card 生成的关联状态卡落盘于
                ``<state_root>/state``，与调用方 StateManager 收敛同根。
        """
        # data_dir 缺省时才退化为 <cwd>/data/handoffs（历史默认）
        self.data_dir = data_dir or os.path.join(os.getcwd(), "data", "handoffs")
        # 关联 ho-* StateCard 的状态根推导：
        # - 显式 state_root：直接使用（最高优先）；
        # - 未传 state_root 但 data_dir 显式传入：以 data_dir 父目录推导，
        #   使 ho-* 卡与交接卡收敛同一数据根（state/ handoffs/ 同根），
        #   不再落 <cwd>/data/state 孤儿；
        # - 二者均未传：保持历史退化行为，关联 StateCard 默认落 <cwd>。
        if state_root is not None:
            self.state_root = state_root
        elif data_dir is not None:
            self.state_root = os.path.dirname(
                os.path.normpath(os.path.abspath(data_dir)))
        else:
            self.state_root = None
        os.makedirs(self.data_dir, exist_ok=True)
        # 线程锁：Web server 以 ThreadingHTTPServer 多线程复用模块级单例交接总线，
        # 并发 create_handoff/list_pending/update_status 会并行读写数据目录，
        # 不加锁存在文件交错写/读到半写文件的竞态。加锁串行化保证线程安全。
        # 用 RLock（可重入）：get/_save 等会被 update_status/list_pending 在持锁下再调用。
        self._lock = threading.RLock()

    # ---- 引用登记（state 根级，供存活引用检查发现异目录使用者）----

    def ref_registry_dir(self) -> Optional[str]:
        """引用登记**主**目录 ``<state_root>/handoff-refs``；无 state_root 时 None。"""
        if not self.state_root:
            return None
        return os.path.join(self.state_root, "handoff-refs")

    def ref_registry_dirs(self) -> List[str]:
        """引用登记的**全部候选目录**（主 + 备，按优先级）。

        P1-4 修复（🟡E 闭环）：登记为**双通道**——
        1. 主：``<state_root>/handoff-refs/``（既有通道）；
        2. 备：``<state_root>/.handoff-refs/``（隐藏目录，与主通道**不同故障域**）。

        原实现单通道：主登记目录被同名普通文件占位 / 权限不足 / 外部删除单条
        ``.ref`` 时，根外交接卡的引用**双路径皆不可见**（登记未落 + 目录扫描不可达）
        → 会话侧误判"无引用"→ 误删共享卡（🟡E）。双通道后任一生效即引用可见，
        且不再依赖 fail-fast 阻断建卡（fail-fast 退化为**双通道全失败**时的兜底）。
        """
        if not self.state_root:
            return []
        return [
            os.path.join(self.state_root, "handoff-refs"),
            os.path.join(self.state_root, ".handoff-refs"),
        ]

    def _registry_required(self) -> bool:
        """登记是否为该交接卡引用的**唯一**可见性路径。

        - 交接卡目录落在 ``state_root`` 子树**内**：会话侧扫描界（数据根递归）
          以形状判定即可发现引用 → 登记失败不致命（另有目录扫描兜底）；
        - 交接卡目录落在 ``state_root`` 子树**外**（独立使用者 ``data_dir``
          在别处）：仅 ``<state_root>/handoff-refs/*.ref`` 能让引用可见 →
          登记失败必须 **fail-fast**，否则产生静默悬空引用（P1-1）。
        """
        if not self.state_root:
            return False
        root = os.path.abspath(self.state_root)
        data = os.path.abspath(self.data_dir)
        return not (data == root or data.startswith(root + os.sep))

    def _register_ref(self, card_id: str, state_card_id: str) -> bool:
        """登记一张交接卡对 ho-* 状态卡的引用（幂等、原子写）。

        记录 card_path 便于存活判定时核对"交接卡是否仍在"——卡被删除后
        登记即视为失效引用，可安全回收共享卡（无需依赖扫描其所在目录）。

        **返回是否登记成功（P1-1 修复：不再静默吞 OSError）**：登记是
        ``state_root`` 子树外交接卡的**唯一**可见性路径，静默失败会让会话侧
        误判"无存活引用"而删共享卡 → 存活引用悬空。调用方
        （``create_handoff``）据 ``_registry_required()`` 决定是否 fail-fast。
        """
        if not state_card_id:
            return False
        if not _is_valid_card_id(card_id):
            return False  # 🔴 P1-3：非法 card_id 不参与路径拼接
        dirs = self.ref_registry_dirs()
        if not dirs:
            return False
        payload = {
            "card_id": card_id,
            "state_card_id": state_card_id,
            "card_path": os.path.abspath(
                os.path.join(self.data_dir, f"{card_id}.json")),
        }
        written = False
        # 与 _unregister_ref 的「空目录 rmdir」互斥（🔴1）：否则 rmdir 与
        # makedirs/mkstemp 交叠 → mkstemp 抛 FileNotFoundError → 双通道同时
        # 失败 → 登记静默丢失 / fail-fast 误抛。临界区覆盖全部候选通道。
        with _REF_REGISTRY_LOCK:
            for reg_dir in dirs:
                # 双通道：任一生效即引用可见（🟡E 闭环）。单通道失败不阻断另一通道。
                try:
                    os.makedirs(reg_dir, exist_ok=True)
                    path = os.path.join(reg_dir, f"{card_id}.ref")
                    fd, tmp_path = tempfile.mkstemp(dir=reg_dir, suffix=".tmp")
                    try:
                        with os.fdopen(fd, "w", encoding="utf-8") as f:
                            json.dump(payload, f, ensure_ascii=False)
                        os.replace(tmp_path, path)
                        written = True
                    except OSError:
                        try:
                            os.unlink(tmp_path)
                        except OSError:
                            pass
                except OSError:
                    continue
        # 双通道全部失败：返回 False，由调用方按可见性唯一性决定是否 fail-fast
        return written

    def _unregister_ref(self, card_id: str) -> None:
        """注销一张交接卡的引用登记（删除交接卡时同步调用）。

        注销后若登记目录已空则回收该目录（与 delete_session「不留空目录
        残骸」的纪律一致）；下次登记会按需重建。
        """
        if not _is_valid_card_id(card_id):
            # 🔴 P1-3：非法 card_id 不得构成路径穿越删除面（如 "../handoff-refs/x"）
            return
        # 与 _register_ref 互斥（🔴1）：删 .ref 与「空目录 rmdir」须在同一
        # 临界区，防止 rmdir 把并发登记线程刚 makedirs 的目录删掉。
        with _REF_REGISTRY_LOCK:
            for reg_dir in self.ref_registry_dirs():
                try:
                    path = os.path.join(reg_dir, f"{card_id}.ref")
                    if os.path.exists(path):
                        os.remove(path)
                except OSError:
                    pass
                # 回收空登记目录（非空时 os.rmdir 抛 OSError，忽略即可）
                try:
                    if os.path.isdir(reg_dir) and not os.listdir(reg_dir):
                        os.rmdir(reg_dir)
                except OSError:
                    pass

    def create_handoff(self, source_unit: str, target_unit: str,
                       intent: str, context: str = "",
                       auto_state_card: bool = False) -> HandoffCard:
        """创建一张交接卡（线程安全）。

        auto_state_card=True 时自动创建关联的 M9 StateCard，
        交接卡获得唯一会话ID，供下游秘书状态管理使用。

        **共享卡语义后果**：关联 StateCard 的 ID 为
        ``hash(source_unit, target_unit, intent)``，同源/同目标/同意图的多次
        调用**复用同一张卡**；该卡内的交接任务仅按标题去重，故**后续调用的
        ``context`` 不落卡**（后到者上下文静默丢弃）。需要各自上下文时请改用
        不同 ``intent``。

        **引用登记（双通道，🟡E 闭环）**：``auto_state_card=True`` 时须登记
        主 ``<state_root>/handoff-refs/<card_id>.ref`` **与**备
        ``<state_root>/.handoff-refs/<card_id>.ref``（不同故障域）。判定：
        - 任一通道写入成功 → 引用可见，建卡成功；
        - **双通道全部失败**且 ``data_dir`` 在 ``state_root`` 子树**之外**
          （登记为唯一可见性路径）→ 回滚该交接卡并抛 ``OSError``（fail-fast）；
        - 双通道全失败但 ``data_dir`` 在子树**之内** → 目录扫描兜底，不抛。

        ``card_id`` 须为 ``[A-Za-z0-9_-]+``（🔴 P1-3 白名单）；非法值不参与
        路径拼接（防路径穿越删除面）。

        **入参校验（🔴 P1-3 补充）**：``card_id`` 由 ``source_unit`` /
        ``target_unit`` 拼接而成，故二者亦须为 ``[A-Za-z0-9_-]+``（非空）。
        否则 ``create_handoff("../state/VICTIM", …)`` 会让 ``_save`` 把卡写到
        ``data_dir`` **之外**（路径穿越写入面）。非法入参抛 ``ValueError``。
        """
        if not _is_valid_card_id(source_unit):
            raise ValueError(
                f"非法 source_unit: {source_unit!r}。仅允许 [A-Za-z0-9_-]。")
        if not _is_valid_card_id(target_unit):
            raise ValueError(
                f"非法 target_unit: {target_unit!r}。仅允许 [A-Za-z0-9_-]。")
        with self._lock:
            # 用纳秒时间戳 + 自增序号保证同秒同源唯一（秒级时间戳碰撞会覆盖丢卡）
            card_id = f"{source_unit}-{target_unit}-{time.time_ns()}"
            state_card_id = None

            # M9 集成：自动创建关联 StateCard
            if auto_state_card:
                try:
                    from secretary.state.manager import StateManager
                    # 关联 ho-* 状态卡落盘：状态根已按推导规则收敛
                    # （显式 state_root / data_dir 父目录 / 历史 <cwd>），
                    # 统一落 <state_root>/state 子目录。
                    sm = StateManager(
                        data_dir=os.path.join(self.state_root, "state")
                        if self.state_root else None)
                    # 会话ID = 交接卡意图的简单哈希，保证稳定可检索
                    hash_hex = hashlib.md5(
                        f"{source_unit}-{target_unit}-{intent}".encode()
                    ).hexdigest()[:12]
                    state_card_id = f"ho-{source_unit[:8]}-{target_unit[:8]}-{hash_hex}"
                    # 获取或创建 StateCard（同一稳定会话复用时沿用既有卡）
                    sm.get_or_create(
                        session_id=state_card_id,
                        objective=intent,
                        unit_id=target_unit,
                        role_name="secretary",
                    )
                    # 向 StateCard 添加交接任务，但同一 StateCard 已存在同名任务时不重复堆积
                    task_title = f"交接: {source_unit} -> {target_unit}"
                    card_state = sm.get_card(state_card_id)
                    exists = any(
                        t.title == task_title for t in card_state.tasks
                    ) if card_state else False
                    if not exists:
                        sm.add_task(
                            session_id=state_card_id,
                            title=task_title,
                            objective=intent,
                            task_type="deliverable",
                            summary=context,
                        )
                except Exception:
                    # StateCard 创建失败不阻断交接卡本身
                    state_card_id = None

            card = HandoffCard(
                id=card_id,
                source_unit=source_unit,
                target_unit=target_unit,
                intent=intent,
                context=context,
                state_card_id=state_card_id,
                created_at=time.strftime("%Y-%m-%d %H:%M:%S"),
            )
            self._save(card)
            # 引用登记：与 ho-* 卡同根，供存活引用检查发现"交接卡目录与
            # 状态根不同树"的独立使用者（🔴#4）。
            # P1-1 修复：登记失败不再静默——若登记是该卡的**唯一**可见性路径
            # （data_dir 在 state_root 子树之外），回滚刚落盘的交接卡并抛
            # OSError（fail-fast），避免"写入成功但引用静默悬空"。
            if card.state_card_id:
                if (not self._register_ref(card.id, card.state_card_id)
                        and self._registry_required()):
                    # 双通道登记**全部失败**且登记为该卡唯一可见性路径 →
                    # 回滚交接卡并 fail-fast（不产生"写入成功但引用静默悬空"）。
                    try:
                        os.remove(os.path.join(
                            self.data_dir, f"{card.id}.json"))
                    except OSError:
                        pass
                    raise OSError(
                        "交接卡引用登记双通道均失败，且登记为其唯一可见性路径"
                        f"（data_dir 在 state_root 子树之外）：card_id={card.id}；"
                        "已回滚该交接卡以避免悬空引用")
            return card

    def get(self, card_id: str) -> Optional[HandoffCard]:
        """按 ID 读取交接卡（线程安全）。

        🔴 P1-3：非法 card_id（含路径分隔符/相对路径）一律返回 ``None``，
        不参与路径拼接。
        """
        if not _is_valid_card_id(card_id):
            return None
        with self._lock:
            path = os.path.join(self.data_dir, f"{card_id}.json")
            if not os.path.exists(path):
                return None
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            return HandoffCard(**data)

    def update_status(self, card_id: str, status: str) -> bool:
        """更新交接卡状态（线程安全）"""
        with self._lock:
            card = self.get(card_id)
            if not card:
                return False
            card.status = status
            self._save(card)
            return True

    def list_pending(self) -> List[HandoffCard]:
        """列出所有待处理交接卡（线程安全）。

        P0-2 同形加固（既有代码，随 P0-2 一并收口）：``listdir`` 包
        ``try/except OSError``——数据目录在 ``isdir``/``listdir`` 之间被并发
        回收，或因权限/IO 异常不可读时，返回空列表而非向调用方抛裸异常。
        与 ``_build_ho_reference_index`` 来源 3 的 ``listdir`` 守卫同形
        （该处为本 PR 新增逃逸面，已修）；本处为 ``main`` 既有代码，一并加固。

        🟡-1 同函数口径统一（复评第 5 轮）：**逐文件读路径与目录级守卫同纪律**。
        ``get()`` 内部走 ``os.path.exists → open``，文件在该窗口内被并发删除
        （或半写 / 内容损坏）时，原实现会让 ``FileNotFoundError`` /
        ``JSONDecodeError`` 从本方法逃逸——与"目录不可读即返回空列表"的
        目录级声明口径不一致。现将单条读取包 ``try/except (OSError, ValueError)``，
        坏条**跳过**而不逃逸（与 ``session.py::_read_json_dict`` 同纪律）；
        单条异常不再影响其余待处理卡的列举。
        """
        with self._lock:
            try:
                names = os.listdir(self.data_dir)
            except OSError:
                # 数据目录不可读/已被并发回收 → 无待处理卡可列
                return []
            pending = []
            for f in names:
                if not f.endswith(".json"):
                    continue
                # 🟡-1：单条读取异常（exists→open 窗口被并发删除 / 半写 /
                # JSON 损坏）跳过该条，不向调用方抛裸异常。
                try:
                    card = self.get(f[:-5])
                except (OSError, ValueError):
                    continue
                if card and card.status == "created":
                    pending.append(card)
            return pending

    def delete(self, card_id: str) -> bool:
        """删除一张交接卡（线程安全）。返回是否删除成功。

        删除后同步注销 state 根级引用登记（``handoff-refs/<card_id>.ref``），
        使该交接卡对共享 ho-* 状态卡的引用不再阻塞回收（🔴#4）。
        """
        if not _is_valid_card_id(card_id):
            # 🔴 P1-3：非法 card_id 不得删除 data_dir 之外的任意文件
            # （如 "../state/VICTIM"）；也不注销登记（登记名同样受限）。
            return False
        with self._lock:
            path = os.path.join(self.data_dir, f"{card_id}.json")
            existed = os.path.exists(path)
            if existed:
                try:
                    os.remove(path)
                except OSError:
                    return False
            # 无论卡是否曾存在，登记都注销（幂等），避免残登记阻塞回收
            self._unregister_ref(card_id)
            return existed

    def _save(self, card: HandoffCard):
        if not card.id:
            card.id = f"{card.source_unit}-{card.target_unit}-{time.time_ns()}"
        # 🔴 P1-3（纵深防御）：card_id 参与路径拼接，非法则拒绝落盘——
        # 即便调用方绕过 create_handoff 的入参校验（如外部构造 HandoffCard
        # 后直接 _save），也不得写出 data_dir 之外。
        if not _is_valid_card_id(card.id):
            raise ValueError(
                f"非法 card_id: {card.id!r}。仅允许 [A-Za-z0-9_-]。")
        path = os.path.join(self.data_dir, f"{card.id}.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump({
                "id": card.id,
                "source_unit": card.source_unit,
                "target_unit": card.target_unit,
                "intent": card.intent,
                "context": card.context,
                "status": card.status,
                "state_card_id": card.state_card_id,
                "created_at": card.created_at,
            }, f, ensure_ascii=False, indent=2)

    def validate_card(self, card: HandoffCard) -> dict:
        """执行 M5 × M9 完整度校验（标准字段检查）"""
        return card.completeness_check()

    def create_standard_handoff(self, source_unit: str, target_unit: str,
                                intent: str, context: str = "") -> HandoffCard:
        """创建符合 M5 × M9 跨单元标准的交接卡（强制关联 StateCard）。

        与 create_handoff 区别：
        - 自动创建关联 StateCard
        - 返回前执行完整度校验
        """
        card = self.create_handoff(
            source_unit=source_unit,
            target_unit=target_unit,
            intent=intent,
            context=context,
            auto_state_card=True,
        )
        check = card.completeness_check()
        if not check["passed"]:
            raise ValueError(
                f"交接卡不完整: 缺少 {', '.join(check['missing'])}"
            )
        return card
