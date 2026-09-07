"""M9 层级向上提交协议 + 秘书长多级树拓扑

功能扩展（超越 SKILL.state 单层状态）：
1. 原有单级父子提交（submit_up / aggregate_from_downstreams）
2. 新增 SecretaryTree：支持多级树拓扑管理（秘书长 → 秘书 → 执行员）
3. 新增每层折叠汇总（仅携带该层有效上下文，不含下层的全量细节）
4. 新增树导航渲染视图（人类全局一眼看 + AI 精确检索）
"""
from __future__ import annotations

import json
import os
import time
from collections import deque
from typing import Dict, List, Optional

from .persist import FlushQueue
from .models import StateCard, TaskRecord, TaskState


class SecretaryTreeNode:
    """
    秘书长多级树的一个节点：
    - 每个节点关联一张 StateCard（由对应秘书维护）
    - 通过 parent/children 建立树形拓扑
    - 节点含"职责摘要"，用于上层宏观管理
    """

    def __init__(self,
                 session_id: str,
                 role_name: str = "secretary",
                 display_name: str = "",
                 objective: str = ""):
        self.session_id = session_id          # 关联 StateCard 的会话ID
        self.role_name = role_name            # 坑位/岗位名
        self.display_name = display_name or role_name
        self.objective = objective            # 本节点负责的目标
        self.parent_session: Optional[str] = None
        self.children: List[str] = []         # 子节点 session_id 列表

    def to_dict(self) -> dict:
        return {
            "session_id": self.session_id,
            "role_name": self.role_name,
            "display_name": self.display_name,
            "objective": self.objective,
            "parent_session": self.parent_session,
            "children": self.children,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "SecretaryTreeNode":
        node = cls(
            session_id=data.get("session_id", ""),
            role_name=data.get("role_name", "secretary"),
            display_name=data.get("display_name", ""),
            objective=data.get("objective", ""),
        )
        node.parent_session = data.get("parent_session")
        node.children = list(data.get("children", []))
        return node


class SecretaryTree:
    """
    秘书长多级树管理器：
    - 维护多级树拓扑（根=秘书长 → 中间=秘书 → 叶子=执行员）
    - 支持节点注册/关联/查询
    - 支持整棵树持久化到 data/state/tree-<name>.json
    - 不依赖 StateCard 已存在，节点可独立注册后由 StateManager 关联
    """

    def __init__(self, data_dir: Optional[str] = None, tree_name: str = "default",
                 flush_queue: Optional[FlushQueue] = None):
        self.data_dir = data_dir or os.path.join(os.getcwd(), "data", "state")
        os.makedirs(self.data_dir, exist_ok=True)
        self.tree_name = tree_name
        self.nodes: Dict[str, SecretaryTreeNode] = {}
        self.root_session: Optional[str] = None
        # 落盘走队列：默认 write-through（保持既有即时落盘行为），
        # 规模大时可用 defer() 切到"延迟批量"，多次变更仅一次落盘。
        self._flush_queue = flush_queue or FlushQueue()
        self._load()

    def _tree_path(self) -> str:
        """树持久化文件路径"""
        # 防御：tree_name 仅允许安全字符
        safe = "".join(c for c in self.tree_name if c.isalnum() or c in "-_")
        return os.path.join(self.data_dir, f"tree-{safe}.json")

    def _load(self):
        """从磁盘加载树（存在则恢复）"""
        path = self._tree_path()
        if not os.path.exists(path):
            return
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            self.root_session = data.get("root_session")
            raw_nodes = data.get("nodes", [])
            for raw in raw_nodes:
                node = SecretaryTreeNode.from_dict(raw)
                if node.session_id:
                    self.nodes[node.session_id] = node
        except Exception:
            pass  # 损坏的树数据静默忽略，不阻断

    def _save(self):
        """登记一次整棵树落盘（写入统一走队列，支持批量/延迟落盘）"""
        def _write():
            path = self._tree_path()
            payload = {
                "tree_name": self.tree_name,
                "root_session": self.root_session,
                "nodes": [n.to_dict() for n in self.nodes.values()],
                "updated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            }
            with open(path, "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False, indent=2)
        self._flush_queue.enqueue(self._tree_path(), _write)

    # ---- 批量/延迟落盘 ----

    def flush(self) -> int:
        """强制批量落盘（把 pending 的全部一次写出），返回写盘资源数"""
        return self._flush_queue.flush()

    def defer(self, batch_size: int = 0):
        """
        进入"延迟批量落盘"上下文：上下文内多次变更仅登记、统一到退出时批量落盘。
        with tree.defer():
            tree.register_node(...)   # 多次变更
        # 退出时 flush 一次
        """
        from .persist import DeferredWriter
        self._flush_queue.batch_size = max(batch_size, self._flush_queue.batch_size)
        return DeferredWriter(self._flush_queue)

    @property
    def pending_write_count(self) -> int:
        """尚未落盘的待写登记数（延迟模式下观测用）"""
        return self._flush_queue.pending_count

    # ---- 节点管理 ----

    def register_node(self, session_id: str,
                      role_name: str = "secretary",
                      display_name: str = "",
                      objective: str = "",
                      parent_session: Optional[str] = None) -> SecretaryTreeNode:
        """注册一个新秘书节点（若已存在则更新）"""
        if session_id in self.nodes:
            node = self.nodes[session_id]
            if display_name:
                node.display_name = display_name
            if objective:
                node.objective = objective
            if role_name:
                node.role_name = role_name
        else:
            node = SecretaryTreeNode(
                session_id=session_id,
                role_name=role_name,
                display_name=display_name,
                objective=objective,
            )
            self.nodes[session_id] = node

        # 根节点尚未设置 → 首节点即根（秘书长）
        if self.root_session is None:
            self.root_session = session_id

        if parent_session:
            self.link(parent_session, session_id)
        self._save()
        return node

    def set_root(self, session_id: str) -> bool:
        """指定根节点（秘书长所在节点）"""
        if session_id not in self.nodes:
            return False
        # 若该节点已有父 → 从原父的子列表移除
        old_parent = self.nodes[session_id].parent_session
        if old_parent and old_parent in self.nodes:
            p = self.nodes[old_parent]
            if session_id in p.children:
                p.children.remove(session_id)
        self.nodes[session_id].parent_session = None
        self.root_session = session_id
        self._save()
        return True

    def link(self, parent_session: str, child_session: str) -> bool:
        """建立父子关系（parent → child）"""
        if parent_session not in self.nodes or child_session not in self.nodes:
            return False
        parent = self.nodes[parent_session]
        child = self.nodes[child_session]

        # 从旧父移除
        if child.parent_session and child.parent_session != parent_session:
            old_p = self.nodes.get(child.parent_session)
            if old_p and child_session in old_p.children:
                old_p.children.remove(child_session)

        # 添加到新父
        child.parent_session = parent_session
        if child_session not in parent.children:
            parent.children.append(child_session)

        # 若 child 曾为根 → 根升级为 parent
        if self.root_session == child_session:
            self.root_session = parent_session

        self._save()
        return True

    def unlink(self, child_session: str) -> bool:
        """断开子节点与其父的关系（子变为独立根）"""
        if child_session not in self.nodes:
            return False
        node = self.nodes[child_session]
        if node.parent_session and node.parent_session in self.nodes:
            parent = self.nodes[node.parent_session]
            if child_session in parent.children:
                parent.children.remove(child_session)
        node.parent_session = None
        self._save()
        return True

    def remove_node(self, session_id: str) -> bool:
        """移除节点及其子树"""
        if session_id not in self.nodes:
            return False
        node = self.nodes[session_id]

        # 从其父的子列表移除
        if node.parent_session and node.parent_session in self.nodes:
            parent = self.nodes[node.parent_session]
            if session_id in parent.children:
                parent.children.remove(session_id)

        # 递归移除子树
        for child_id in list(node.children):
            self.remove_node(child_id)

        # 若为根 → 清除根标记
        if self.root_session == session_id:
            self.root_session = None
            # 若还有节点，优先选"无父的顶层节点"作为新根，
            # 保证单根落在某连通分量的顶部，其余孤立分量节点不会被
            # rollup/render_tree 从根误判不可达。
            remaining = [s for s in self.nodes if s != session_id]
            root_candidates = [
                s for s in remaining
                if self.nodes[s].parent_session is None
            ]
            if root_candidates:
                self.root_session = root_candidates[0]
            elif remaining:
                self.root_session = remaining[0]

        del self.nodes[session_id]
        self._save()
        return True

    # ---- 查询 ----

    def get_node(self, session_id: str) -> Optional[SecretaryTreeNode]:
        return self.nodes.get(session_id)

    def get_root(self) -> Optional[SecretaryTreeNode]:
        if not self.root_session:
            return None
        return self.nodes.get(self.root_session)

    def get_children(self, session_id: str) -> List[SecretaryTreeNode]:
        node = self.nodes.get(session_id)
        if not node:
            return []
        return [self.nodes[c] for c in node.children if c in self.nodes]

    def get_descendants(self, session_id: str) -> List[SecretaryTreeNode]:
        """获取某节点的所有后代（BFS）"""
        if session_id not in self.nodes:
            return []
        result: List[SecretaryTreeNode] = []
        visited = set()
        queue = deque(self.nodes[session_id].children)
        while queue:
            sid = queue.popleft()
            if sid in visited or sid not in self.nodes:
                continue
            visited.add(sid)
            result.append(self.nodes[sid])
            queue.extend(self.nodes[sid].children)
        return result

    def get_ancestors(self, session_id: str) -> List[SecretaryTreeNode]:
        """获取某节点的所有祖先（从近到远）"""
        result = []
        node = self.nodes.get(session_id)
        if not node:
            return []
        cur = node.parent_session
        while cur and cur in self.nodes:
            result.append(self.nodes[cur])
            cur = self.nodes[cur].parent_session
        return result

    def get_path(self, session_id: str) -> List[str]:
        """从根到节点的 session 路径"""
        node = self.nodes.get(session_id)
        if not node:
            return []
        path = [session_id]
        cur = node.parent_session
        while cur:
            path.insert(0, cur)
            if cur not in self.nodes:
                break
            cur = self.nodes[cur].parent_session
        return path

    # ---- 汇总 ----

    def summarize_node(self, node: SecretaryTreeNode,
                       card: Optional[StateCard] = None) -> dict:
        """
        生成单个节点的宏观汇总（供上层使用）。
        - 若提供了 StateCard，则用卡的实际任务进度
        - 否则仅用节点自身信息（无状态卡时仍可汇总）
        """
        summary = {
            "session_id": node.session_id,
            "role_name": node.role_name,
            "display_name": node.display_name,
            "objective": node.objective or (card.objective if card else ""),
        }
        if card:
            # 用 StateCard 任务统计
            states = [t.state for t in card.tasks]
            summary.update({
                "task_count": len(states),
                "done_count": states.count(TaskState.DONE.value),
                "in_progress_count": states.count(TaskState.IN_PROGRESS.value),
                "blocked_count": states.count(TaskState.BLOCKED.value),
                "stopped_count": states.count(TaskState.STOPPED.value),
                "content_refs": [t.content_ref for t in card.tasks if t.content_ref],
            })
        else:
            summary.update({
                "task_count": len(node.children),
                "done_count": 0,
                "in_progress_count": 0,
                "blocked_count": 0,
                "stopped_count": 0,
                "content_refs": [],
            })
        return summary

    def rollup(self, state_manager=None) -> dict:
        """
        整棵树的逐级向上汇总（rollup）：
        从叶子节点向上，每层只保留该层的结构化摘要。
        根节点(秘书长)看到的是全局宏观状态，而非每层全量。
        """
        # 获取所有卡
        cards: Dict[str, StateCard] = {}
        if state_manager:
            for sid, node in self.nodes.items():
                card = state_manager.get_card(sid)
                if card:
                    cards[sid] = card

        result = {
            "tree_name": self.tree_name,
            "root_session": self.root_session,
            "rollup_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "levels": [],
        }

        # 用 DFS 层级收集
        root = self.get_root()
        if not root:
            return result

        level_map: Dict[int, List[str]] = {}
        self._collect_levels(root.session_id, 0, level_map)
        max_depth = max(level_map.keys()) if level_map else 0

        # 自底向上汇总每层
        for depth in range(max_depth, -1, -1):
            sessions = level_map.get(depth, [])
            level_nodes = []
            for sid in sessions:
                node = self.nodes[sid]
                card = cards.get(sid)
                level_nodes.append(self.summarize_node(node, card))
            result["levels"].append({
                "depth": depth,
                "node_count": len(sessions),
                "nodes": level_nodes,
            })

        return result

    def _collect_levels(self, session_id: str, depth: int,
                        level_map: Dict[int, List[str]]):
        """DFS 收集每层的 session 列表"""
        if session_id not in self.nodes:
            return
        if depth not in level_map:
            level_map[depth] = []
        level_map[depth].append(session_id)
        node = self.nodes[session_id]
        for child_id in node.children:
            self._collect_levels(child_id, depth + 1, level_map)

    # ---- 渲染 ----

    def render_tree(self, state_manager=None) -> str:
        """
        渲染整棵秘书树的导航视图。
        每行显示：层级缩进 + 角色名 + 会话ID + 目标摘要 + 任务状态。
        """
        root = self.get_root()
        if not root:
            return f"(树 {self.tree_name} 为空)"

        cards: Dict[str, StateCard] = {}
        if state_manager:
            for sid, node in self.nodes.items():
                card = state_manager.get_card(sid)
                if card:
                    cards[sid] = card

        lines = []
        lines.append(f"🌳 秘书树: {self.tree_name}")
        lines.append(f"    根节点(秘书长): {root.session_id} ({root.display_name})")
        lines.append("")

        self._render_recursive(root, 0, cards, lines)
        return "\n".join(lines)

    def _render_recursive(self, node: SecretaryTreeNode, depth: int,
                          cards: Dict[str, StateCard], lines: List[str]):
        """递归渲染树节点"""
        indent = "  " * depth
        card = cards.get(node.session_id)
        state_str = ""
        if card:
            done = sum(1 for t in card.tasks if t.state == TaskState.DONE.value)
            total = len(card.tasks)
            state_str = f" [tasks:{done}/{total}]"
        icon = "🏛" if depth == 0 else ("📋" if node.children else "⚙")
        display = node.display_name or node.role_name
        obj = node.objective[:40] + "..." if len(node.objective) > 40 else node.objective
        lines.append(f"{indent}{icon} {display} [{node.session_id}]{state_str} {obj}")

        # 若有卡，列出展开任务
        if card:
            for t in card.tasks:
                if t.fold == "expanded":
                    lines.append(f"{indent}  ├─ ▶ {t.task_id} {t.title} ({t.state})")
                elif t.state in ("in_progress", "blocked"):
                    lines.append(f"{indent}  ├─ ● {t.task_id} {t.title} ({t.state})")

        for child in node.children:
            if child in self.nodes:
                self._render_recursive(self.nodes[child], depth + 1, cards, lines)


class HierarchySubmitter:
    """
    M9 层级向上提交：
    下级秘书维护局部状态卡（StateCard），
    需要向上级提交时，只传"结构化汇总"而非全量原文。

    提交内容 = 下级 StateCard 的 summarize_for_upstream() 结果：
    - 目标、进度、任务编号清单、content_refs
    - 不含原文正文

    增强：支持多级树拓扑（配合 SecretaryTree / SecretaryTreeNode 使用）。
    """

    def __init__(self, data_dir: Optional[str] = None):
        self.data_dir = data_dir or os.path.join(os.getcwd(), "data", "state")
        os.makedirs(self.data_dir, exist_ok=True)

    def submit_up(self, card: StateCard,
                  upstream_session: str = None) -> dict:
        """
        将下级秘书的 StateCard 向上级提交汇总。
        返回提交记录（含已提交的汇总 + 时间戳），并写落盘。
        """
        summary = card.summarize_for_upstream()
        if upstream_session:
            card.upstream = upstream_session
        else:
            upstream_session = card.upstream or "root"

        submission = {
            "from_session": card.session_id,
            "from_role": card.role_name,
            "to_session": upstream_session,
            "submitted_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "payload": summary,
        }
        self._record_submission(submission)
        return submission

    def aggregate_from_downstreams(self, parent_card: StateCard,
                                   children: List[StateCard]) -> dict:
        """
        上级秘书聚合多个下级的提交。
        上级看到的是各下级的"汇总快照"，不携带下级全量上下文。
        """
        aggregated = {
            "parent_session": parent_card.session_id,
            "parent_objective": parent_card.objective,
            "aggregated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "children": [],
        }
        for child in children:
            summary = child.summarize_for_upstream()
            aggregated["children"].append({
                "session_id": child.session_id,
                "role": child.role_name,
                "objective": child.objective,
                "task_count": summary["task_count"],
                "done_count": summary["done_count"],
                "blocked_count": summary["blocked_count"],
                "in_progress_count": summary["in_progress_count"],
                "stopped_count": summary.get("stopped_count", 0),
                "content_refs": summary["content_refs"],
            })
            # 记录上下游关系
            if child.session_id not in parent_card.downstream:
                parent_card.downstream.append(child.session_id)
            child.upstream = parent_card.session_id

        return aggregated

    # ---- 多级树增强 ----

    def submit_chain_up(self, card: StateCard,
                        tree: SecretaryTree,
                        state_manager=None) -> List[dict]:
        """
        沿树路径逐级向上提交：
        叶子 → 父 → 祖 → ... → 根（秘书长）
        每级只传该级的汇总，不携带下级全量。
        路径由 get_path 返回（根→叶方向），本方法反向从叶→根逐级提交。
        """
        # 获取叶子节点到根的路径（root → ... → leaf）
        path = tree.get_path(card.session_id)
        if len(path) <= 1:
            # 叶子即是根，只提交一次到根
            submission = self.submit_up(card, upstream_session=card.upstream or "root")
            return [submission]

        submissions = []
        # 路径是 root→...→leaf，从叶端向根端反向提交
        # 找到 card.session_id 在路径中的位置
        try:
            idx = path.index(card.session_id)
        except ValueError:
            # card 不在树中 → 无法沿路径提交，仅向上级单跳
            submission = self.submit_up(card,
                upstream_session=tree.get_node(card.session_id).parent_session if
                tree.get_node(card.session_id) else card.upstream)
            return [submission]

        # 从当前节点沿 path 逐级向父提交
        cur_idx = idx
        while cur_idx > 0:
            child_sid = path[cur_idx]          # 当前节点
            parent_sid = path[cur_idx - 1]     # 父节点
            # 获取 child 的 card（首次用传入的 card，后续用 state_manager 读取）
            if child_sid == card.session_id:
                child_card = card
            elif state_manager:
                child_card = state_manager.get_card(child_sid)
            else:
                child_card = None

            if child_card:
                submission = self.submit_up(child_card, upstream_session=parent_sid)
                submissions.append(submission)
                # 更新父子关系记录在 StateCard
                child_card.upstream = parent_sid
                if state_manager:
                    state_manager.save_card(child_card)
                    parent_card = state_manager.get_card(parent_sid)
                    if parent_card and child_sid not in parent_card.downstream:
                        parent_card.downstream.append(child_sid)
                        state_manager.save_card(parent_card)
            else:
                # 中间层缺卡/state_manager 缺失：显式记录断链，避免静默漏提交
                submissions.append({
                    "level_skipped": True,
                    "session": child_sid,
                    "parent_session": parent_sid,
                    "reason": "child_state_card_missing",
                    "note": "该层无 StateCard 或 state_manager 为空，已跳过本层向上提交",
                })
            cur_idx -= 1
        return submissions

    def aggregate_level(self, parent_card: StateCard,
                        child_cards: List[StateCard],
                        level_name: str = "") -> dict:
        """
        多级树某层的聚合：
        只保留每个 child 的结构化摘要 + 该层各自的折叠任务引用。
        不携带任何层的原始正文。
        """
        base = self.aggregate_from_downstreams(parent_card, child_cards)
        base["level_name"] = level_name or "default"
        # 为该层补充 content_refs 汇总
        refs = []
        for c in child_cards:
            refs.extend([t.content_ref for t in c.tasks if t.content_ref])
        base["all_content_refs"] = refs
        return base

    def rollup_full_tree(self, tree: SecretaryTree,
                         state_manager=None) -> dict:
        """完整的多级树汇总：从根到叶逐级递归聚合。
        返回层级结构，供上级/秘书长查看全局宏观状态。
        """
        return tree.rollup(state_manager)

    # ---- 持久化 ----

    def _record_submission(self, submission: dict):
        """记录一次提交到 data/state/submissions.log"""
        log_path = os.path.join(self.data_dir, "submissions.jsonl")
        with open(log_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(submission, ensure_ascii=False) + "\n")

    def list_submissions(self, session_id: str = None) -> List[dict]:
        """查看提交记录"""
        log_path = os.path.join(self.data_dir, "submissions.jsonl")
        if not os.path.exists(log_path):
            return []
        records = []
        with open(log_path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except Exception:
                    continue
                if session_id and rec.get("from_session") != session_id:
                    continue
                records.append(rec)
        return records
