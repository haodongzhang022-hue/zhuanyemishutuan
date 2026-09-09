"""M9+ 原文归档检索层（ContentArchive）：折叠后"不丢原文、编号可找回"

Issue #5 秘书机制核心承诺：原文不是丢弃，而是保留 + 编号索引可检索。
此前 FoldEngine.fold_content 仅为每个任务生成一个 content_ref 编号标记
（如 ``{session}-seg001``），并未真正持久化对应原文全文 —— 折叠后无法
按 content_ref 找回原文。本模块补齐这一关键缺口：

  - 将每段折叠原文全文持久化到独立归档存储（默认 data/state/content/）
  - 提供按 content_ref 精确取回 / 按会话批量取回原文
  - 完全解耦、可独立使用，供本仓库状态管理层与其他下游单元迁移复用
  - 路径穿越加固：session_id / content_ref 仅允许 [A-Za-z0-9_-]
"""
from __future__ import annotations

import json
import os
import re
import time
from typing import Dict, List, Optional

# 标识符白名单：仅允许字母/数字/下划线/短横线，防路径穿越
_IDENTIFIER_PATTERN = re.compile(r"^[A-Za-z0-9_-]+$")
# content_ref 常形如 {session}-seg001；保留 {session}-seg 前缀以兼容 FoldEngine
_REF_PATTERN = re.compile(r"^[A-Za-z0-9_-]+-seg[0-9]+$")
# 分隔符：session 与段号
_SEG_PREFIX = "seg"

_EXT = ".json"


class ContentArchive:
    """
    M9+ 原文归档检索层。

    每个归档条目形如：
        data/state/content/<session_id>/<content_ref>.json
        {
          "session_id": "demo-1",
          "content_ref": "demo-1-seg001",
          "content": "……折叠前的原文全文……",
          "title": "段标题",
          "meta": { ... },           # 可选扩展信息
          "archived_at": "2026-09-08 ..."
        }

    ContentArchive 与 StateManager 解耦，可独立于状态卡单独使用
    （对任何"要折叠但不可丢原文"的内容做归档+索引）。
    """

    def __init__(self, root_dir: Optional[str] = None):
        # 默认归档根：<cwd>/data/state/content
        self.root_dir = root_dir or os.path.join(
            os.getcwd(), "data", "state", "content")
        os.makedirs(self.root_dir, exist_ok=True)

    # ------------------------------------------------------------------
    # 路径解析（含防穿越校验）
    # ------------------------------------------------------------------
    @staticmethod
    def _validate_identifier(value: str, what: str = "标识符") -> None:
        if not _IDENTIFIER_PATTERN.match(value):
            raise ValueError(
                f"非法{what}: {value!r}。仅允许字母/数字/下划线/短横线 "
                f"([A-Za-z0-9_-])，防止路径穿越。"
            )

    def _session_dir(self, session_id: str) -> str:
        self._validate_identifier(session_id, "session_id")
        d = os.path.join(self.root_dir, session_id)
        os.makedirs(d, exist_ok=True)
        return d

    def _entry_path(self, session_id: str, content_ref: str) -> str:
        self._validate_identifier(session_id, "session_id")
        self._validate_identifier(content_ref, "content_ref")
        return os.path.join(self._session_dir(session_id),
                            f"{content_ref}{_EXT}")

    # ------------------------------------------------------------------
    # 写入
    # ------------------------------------------------------------------
    def store(self, session_id: str, content_ref: str,
              content: str, *, title: str = "",
              meta: Optional[dict] = None) -> str:
        """归档一段原文全文，按 content_ref 建立索引。返回 content_ref。"""
        if not content.strip():
            raise ValueError("content 不能为空，无可归档原文")
        path = self._entry_path(session_id, content_ref)
        entry = {
            "session_id": session_id,
            "content_ref": content_ref,
            "content": content,
            "title": title,
            "meta": meta or {},
            "archived_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        }
        self._atomic_write_json(path, entry)
        return content_ref

    def store_segments(self, session_id: str,
                       segments: List[str],
                       titles: Optional[List[str]] = None,
                       start_seq: int = 1) -> List[str]:
        """把多段原文（对应 fold 后的多个折叠任务）批量归档。

        每段生成形如 ``{session}-seg{NNN}`` 的 content_ref（与 FoldEngine
        的编号约定一致，保证 fold_content 产出的引用可真正取回原文）。

        Args:
            start_seq: 起始段号（默认 1）。多次归档同一会话时，调用方应传入
                既有最大段号 + 1 以**续号**，避免新一轮归档从 seg001 重启并
                覆盖既有归档文件、造成 content_ref 错位。

        返回归档生成的 content_ref 列表（与传入的非空 segments 一一对应）。
        """
        refs: List[str] = []
        for i, seg in enumerate(segments):
            seg = seg.strip()
            if not seg:
                continue
            seq = start_seq + len(refs)
            ref = f"{session_id}-{_SEG_PREFIX}{seq:03d}"
            title = (titles[i] if titles and i < len(titles) else "") or \
                self._first_line(seg)
            self.store(session_id, ref, seg, title=title)
            refs.append(ref)
        return refs

    @staticmethod
    def _first_line(text: str) -> str:
        first = text.split("\n")[0].strip()
        return first[:50] if first else "未命名段"

    # ------------------------------------------------------------------
    # 读取
    # ------------------------------------------------------------------
    def retrieve(self, session_id: str, content_ref: str) -> Optional[dict]:
        """按 content_ref 精确取回一条归档原文。不存在返回 None。"""
        path = self._entry_path(session_id, content_ref)
        if not os.path.exists(path):
            return None
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return None

    def retrieve_content(self, session_id: str,
                         content_ref: str) -> Optional[str]:
        """便捷取回原文纯文本（仅 content 字段）。"""
        entry = self.retrieve(session_id, content_ref)
        return entry["content"] if entry else None

    def list_refs(self, session_id: str) -> List[str]:
        """列出某会话已归档的全部 content_ref（按编号升序）。"""
        self._validate_identifier(session_id, "session_id")
        sdir = self._session_dir(session_id)
        if not os.path.isdir(sdir):
            return []
        refs = [f[: -len(_EXT)] for f in os.listdir(sdir)
                if f.endswith(_EXT)]
        refs.sort(key=lambda r: self._sort_key(r))
        return refs

    @staticmethod
    def _sort_key(content_ref: str):
        """按 {session}-seg{N} 的段号排序。"""
        m = _REF_PATTERN.match(content_ref)
        if m:
            seq = content_ref.rsplit("-", 1)[-1].replace(_SEG_PREFIX, "")
            try:
                return int(seq)
            except ValueError:
                return 0
        return 0

    def retrieve_session(self, session_id: str) -> List[dict]:
        """取回某会话全部归档原文（按段号升序）。"""
        return [
            self.retrieve(session_id, ref)
            for ref in self.list_refs(session_id)
            if self.retrieve(session_id, ref)
        ]

    def list_sessions(self) -> List[str]:
        """列出已归档的全部会话。"""
        return [d for d in os.listdir(self.root_dir)
                if os.path.isdir(os.path.join(self.root_dir, d))]

    # ------------------------------------------------------------------
    # 删除
    # ------------------------------------------------------------------
    def delete(self, session_id: str, content_ref: str) -> bool:
        path = self._entry_path(session_id, content_ref)
        if os.path.exists(path):
            os.remove(path)
            return True
        return False

    def delete_session(self, session_id: str) -> int:
        """删除某会话全部归档，返回删除条目数。

        归档目录删空后回收目录自身（<root>/<session>/），
        使 delete_session 不留"归档空目录残骸"。
        """
        self._validate_identifier(session_id, "session_id")
        sdir = self._session_dir(session_id)
        if not os.path.isdir(sdir):
            return 0
        n = 0
        for f in os.listdir(sdir):
            if f.endswith(_EXT):
                try:
                    os.remove(os.path.join(sdir, f))
                    n += 1
                except OSError:
                    pass
        # 回收会话子目录自身（<root>/<session>/ 已删空时）。
        # 注意：只删 session 子目录，不删除归档根（content/），
        # 同根其他会话仍复用该目录。
        try:
            if not os.listdir(sdir):
                os.rmdir(sdir)
        except OSError:
            pass
        return n

    # ------------------------------------------------------------------
    # 原子写（对齐 StateManager / arbiter 一致性）
    # ------------------------------------------------------------------
    def _atomic_write_json(self, path: str, data: dict) -> None:
        import tempfile
        dir_path = os.path.dirname(os.path.abspath(path))
        fd, tmp_path = tempfile.mkstemp(dir=dir_path, suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            os.replace(tmp_path, path)
        except Exception:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise
