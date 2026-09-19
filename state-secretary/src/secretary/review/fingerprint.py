"""评审项指纹（ReviewFingerprint）：**同一问题不报第二遍**

## 需求与官方对照（Issue #14：检索"别人/官方有没有做得更好"）

官方 AI 评审插件 `cnbcool/code-review` 已经做了**按内容的去重**：
`include_existing_comments` 会把 PR 上已有评论/评审拉给模型**参考**，
并只丢弃"挂在基线旧代码上"的过期讨论（baseline + merge-base 祖先判定）。
方向正确，但它本质是**把去重交给模型判断**——模型这次的判断与上次不一定一致。

官方 SCA 插件的口径更硬一点，值得抄：**"已存在的同一组件同一 CVE 不会重复阻断"**、
**"内容未变化的文件重命名不会重复产生"**。这两条是**确定性**的（不依赖模型心情）。

本模块把那两条硬口径搬到评审侧：

| 官方口径（SCA） | 评审侧落地 |
|---|---|
| 同一组件 + 同一 CVE 不重复报 | 同一「文件 + 问题类别 + 语义锚点」不重复报（`fingerprint()`） |
| 内容未变化的改名不重复报 | 判定只用**文件内容锚点**（所在行文本 / 附近代码片段），不掺路径（`_anchor_of`） |
| 新增组件 / 新增 CVE 才算新增 | 只有**新指纹**才唤醒修复；老指纹再次出现 → `duplicate`（不回转） |

## 为什么"锚点"不能掺文件和行号

行号会因插入代码整体位移；文件会被重命名/移动。若指纹掺了它们，
"同一个问题"在新一轮就变成了"新问题" → 再报一遍 → 修复往返多一轮
（正是 Issue #14 要治的循环）。故锚点取**语义**：类别 + 归一化后的关键代码文本
（含前后文窗口），路径只作**次要提示**留存、不进指纹。

## 与既有机制的分工

| 机制 | 作用域 |
|---|---|
| `LoopGuard` | 往返**轮次**上界（3 轮 / 1 次 / 总 5 / 停滞 2） |
| `RunScopeGuard` | 同一**执行口径**（范围+版本+受影响面+命令）不重复跑 |
| **`ReviewFingerprintLedger`** | 同一**评审项**不重复报（问题粒度），重复项自动转为结转 |
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import threading
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

# ---- 问题类别（与官方 code-review 的 category 对齐，便于互认） ----
CAT_SECURITY = "security"
CAT_BUG = "bug"
CAT_PERF = "perf"
CAT_MAINTAINABILITY = "maintainability"
CAT_STYLE = "style"
CAT_NIT = "nit"
CAT_OTHER = "other"

ALL_CATEGORIES = (CAT_SECURITY, CAT_BUG, CAT_PERF, CAT_MAINTAINABILITY,
                  CAT_STYLE, CAT_NIT, CAT_OTHER)

# ---- 严重程度（与官方 code-review 对齐） ----
SEV_CRITICAL = "critical"
SEV_WARNING = "warning"
SEV_INFO = "info"

ALL_SEVERITIES = (SEV_CRITICAL, SEV_WARNING, SEV_INFO)

#: 官方默认屏蔽的类别（`exclude_categories: "style,nit"`）——直接照抄，
#: 因为"风格/吹毛求疵不刷屏"是官方已经验证过的降噪口径。
DEFAULT_EXCLUDED_CATEGORIES: Tuple[str, ...] = (CAT_STYLE, CAT_NIT)

#: 非代码文件（官方插件会过滤）：lock 文件、图片、构建产物、压缩包等
_NON_CODE_SUFFIXES = (
    ".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".ico", ".svg",
    ".pdf", ".zip", ".tar", ".gz", ".tgz", ".bz2", ".xz", ".7z", ".rar",
    ".jar", ".war", ".whl", ".so", ".dylib", ".dll", ".exe", ".bin",
    ".lock", ".woff", ".woff2", ".ttf", ".eot", ".mp3", ".mp4", ".mov",
)
_NON_CODE_NAMES = (
    "package-lock.json", "yarn.lock", "pnpm-lock.yaml", "poetry.lock",
    "pipfile.lock", "cargo.lock", "composer.lock", "gemfile.lock", "go.sum",
)
#: 构建产物目录（官方同样过滤）
_NON_CODE_DIRS = (
    "node_modules/", "dist/", "build/", "target/", "__pycache__/",
    ".venv/", "venv/", "vendor/", "coverage/", "site-packages/",
)

_WS = re.compile(r"\s+")
# 操作符/分隔符周围空白**一律抹平**：`x = f(a)` 与 `x=f(a)` 是同一段逻辑，
# 只因格式化工具不同就判成两个问题，会让"同一问题"在每轮都被当成新问题。
_AROUND_OP = re.compile(r"\s*([=+\-*/%<>!&|^~?:;,(){}\[\]])\s*")
_IDENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_NUM = re.compile(r"\b\d+\b")
_STR = re.compile(r"(['\"])(?:\\.|(?!\1).)*\1")
_COMMENT = re.compile(r"(#|//|/\*|\*).*")


def is_code_file(path: Any) -> bool:
    """是否**代码文件**（官方插件会过滤掉锁文件/图片/构建产物 → 这里同口径）。

    判定是保守的（判不出就当代码，宁可多评审也不漏）：
    只有明确命中非代码后缀/文件名/目录才排除。
    """
    if not isinstance(path, str) or not path.strip():
        return True
    p = path.strip().replace("\\", "/").lower()
    if p.endswith(_NON_CODE_SUFFIXES):
        return False
    base = p.rsplit("/", 1)[-1]
    if base in _NON_CODE_NAMES:
        return False
    for d in _NON_CODE_DIRS:
        if d in p:
            return False
    return True


def filter_code_files(paths: Iterable[Any]) -> List[str]:
    """过滤出代码文件（保持输入顺序、去重）。"""
    out: List[str] = []
    seen = set()
    for raw in paths or []:
        if not isinstance(raw, str):
            continue
        p = raw.strip()
        if not p or p in seen:
            continue
        seen.add(p)
        if is_code_file(p):
            out.append(p)
    return out


def _norm_line(text: Any) -> str:
    """把一行代码归一化成**语义锚点**（去掉格式噪音，保留可辨识内容）。

    去掉：注释、行尾空白、空白串、字面量内容、数字。
    保留：标识符与操作符序列（这才是"这是哪一段逻辑"的判别信息）。
    """
    if not isinstance(text, str):
        return ""
    line = text.strip()
    line = _COMMENT.sub("", line)
    line = _STR.sub("'S'", line)
    line = _NUM.sub("N", line)
    line = _WS.sub(" ", line)
    line = _AROUND_OP.sub(r"\1", line)
    return line.strip(" \t;{},")


def _anchor_of(line_text: Any, context: Sequence[Any] = ()) -> str:
    """语义锚点 = 命中行 + 前后文窗口的归一化文本（哈希前）。

    刻意**不含路径/行号**：改名与整体移位不该被当成新问题
    （对应官方 SCA "内容未变化的文件重命名不重复产生"）。
    """
    parts: List[str] = []
    for ctx in context:
        norm = _norm_line(ctx)
        if norm:
            parts.append(norm)
    own = _norm_line(line_text)
    if own:
        parts.append(own)
    if not parts:
        # 全空（如纯注释行）→ 退化为"空锚点"，同类同文件同空锚点仍要判同
        return ""
    return "|".join(parts)


@dataclass
class ReviewFingerprint:
    """一个评审项的**稳定指纹**（跨轮次可比、抗震）。"""

    key: str = ""                    # sha1 前 16 位
    category: str = CAT_OTHER
    severity: str = SEV_INFO
    anchor: str = ""                 # 语义锚点（哈希前原文，便于人工核对）
    path_hint: str = ""              # **不进指纹**，仅留痕（改名/移位时提示"
    #                                  可能是同一个问题"）

    def to_dict(self) -> dict:
        return {
            "key": self.key,
            "category": self.category,
            "severity": self.severity,
            "anchor": self.anchor,
            "path_hint": self.path_hint,
        }


def _norm_cat(cat: Any) -> str:
    c = cat.strip().lower() if isinstance(cat, str) else ""
    return c if c in ALL_CATEGORIES else CAT_OTHER


def _norm_sev(sev: Any) -> str:
    s = sev.strip().lower() if isinstance(sev, str) else ""
    if s in ALL_SEVERITIES:
        return s
    return SEV_INFO


def fingerprint(
    category: Any = CAT_OTHER,
    line_text: Any = "",
    context: Sequence[Any] = (),
    *,
    path: Any = "",
) -> ReviewFingerprint:
    """计算一个评审项的指纹（**类别 + 语义锚点** → sha1）。

    Args:
        category: 问题类别（security/bug/perf/…，未知 → other）
        line_text: 问题所在行的代码文本
        context: 前后文行（可选，增强锚点稳定性）
        path: 文件路径（**只留痕、不进指纹**）
    """
    cat = _norm_cat(category)
    anchor = _anchor_of(line_text, context)
    blob = f"{cat}\x1f{anchor}"
    key = hashlib.sha1(blob.encode("utf-8")).hexdigest()[:16]
    return ReviewFingerprint(
        key=key, category=cat, severity=SEV_INFO, anchor=anchor,
        path_hint=path.strip() if isinstance(path, str) else "",
    )


@dataclass
class LedgerEntry:
    """台账里的一条评审项（首次出现时登记）。"""

    key: str = ""
    category: str = CAT_OTHER
    severity: str = SEV_INFO
    path_hint: str = ""
    anchor: str = ""
    rounds: List[int] = field(default_factory=list)   # 出现在第几轮
    count: int = 1
    carried: bool = False            # 是否已**结转**（不再回转）
    first_round: int = 0
    last_round: int = 0

    def to_dict(self) -> dict:
        return {
            "key": self.key, "category": self.category, "severity": self.severity,
            "path_hint": self.path_hint, "anchor": self.anchor,
            "rounds": list(self.rounds), "count": self.count,
            "carried": self.carried, "first_round": self.first_round,
            "last_round": self.last_round,
        }

    @classmethod
    def from_dict(cls, data: Any) -> Optional["LedgerEntry"]:
        if not isinstance(data, dict) or not data.get("key"):
            return None
        e = cls(key=str(data["key"]))
        e.category = _norm_cat(data.get("category"))
        e.severity = _norm_sev(data.get("severity"))
        e.path_hint = str(data.get("path_hint") or "")
        e.anchor = str(data.get("anchor") or "")
        rounds = data.get("rounds")
        e.rounds = [int(r) for r in rounds if isinstance(r, (int, float))] \
            if isinstance(rounds, list) else []
        try:
            e.count = max(1, int(data.get("count") or 1))
        except (TypeError, ValueError):
            e.count = 1
        e.carried = bool(data.get("carried") or False)
        try:
            e.first_round = int(data.get("first_round") or 0)
        except (TypeError, ValueError):
            e.first_round = 0
        try:
            e.last_round = int(data.get("last_round") or 0)
        except (TypeError, ValueError):
            e.last_round = 0
        return e


@dataclass
class DedupResult:
    """一轮评审的**去重结果**（给角色/流水线执行用）。"""

    fresh: List[ReviewFingerprint] = field(default_factory=list)      # 新问题 → 值得回转
    duplicate: List[ReviewFingerprint] = field(default_factory=list)  # 老问题 → 只结转
    filtered: List[ReviewFingerprint] = field(default_factory=list)   # 被降噪口径滤掉

    @property
    def should_rotate(self) -> bool:
        """是否还有"值得回转"的新问题（**只看新问题**，老问题重复报不产生回转）。"""
        return bool(self.fresh)

    def to_dict(self) -> dict:
        return {
            "fresh": [f.to_dict() for f in self.fresh],
            "duplicate": [f.to_dict() for f in self.duplicate],
            "filtered": [f.to_dict() for f in self.filtered],
            "fresh_count": len(self.fresh),
            "duplicate_count": len(self.duplicate),
            "filtered_count": len(self.filtered),
            "should_rotate": self.should_rotate,
        }


class ReviewFingerprintLedger:
    """评审项指纹台账（**同一问题不报第二遍**，重复项自动结转）。

    Args:
        data_dir: 落盘目录（缺省时不落盘，仅进程内生效——不因台账缺失挡住干活）
        excluded_categories: 降噪屏蔽的类别（默认照抄官方 `style,nit`）
        min_severity: 触发评论的最低严重程度（官方 `min_severity` 同口径）
    """

    def __init__(
        self,
        data_dir: Optional[str] = None,
        excluded_categories: Optional[Sequence[str]] = None,
        min_severity: str = SEV_INFO,
    ):
        self.data_dir = data_dir
        self.excluded_categories = tuple(
            _norm_cat(c) for c in (DEFAULT_EXCLUDED_CATEGORIES
                                   if excluded_categories is None
                                   else excluded_categories))
        self.min_severity = _norm_sev(min_severity)
        self._lock = threading.Lock()
        self._entries: Dict[str, LedgerEntry] = {}
        self._load()

    # ------------------------------------------------------------------
    # 判定
    # ------------------------------------------------------------------

    def _severity_passes(self, sev: str) -> bool:
        order = {SEV_CRITICAL: 3, SEV_WARNING: 2, SEV_INFO: 1}
        return order.get(sev, 1) >= order.get(self.min_severity, 1)

    def dedup(
        self,
        items: Iterable[Any],
        round_no: int = 0,
        *,
        record: bool = True,
    ) -> DedupResult:
        """对一轮评审结果去重：**新问题保留、老问题结转、噪音滤掉**。

        `items` 支持两种形态，便于与官方插件输出互认：

        - 结构化 dict：``{"category": ..., "line_text": ..., "context": [...],
          "path": ..., "severity": ...}``（缺字段一律走默认，不抛）；
        - 或 ``ReviewFingerprint`` / 已算好的指纹字典。
        """
        res = DedupResult()
        for raw in items or []:
            fp = self._as_fingerprint(raw)
            if fp is None:
                continue
            if fp.category in self.excluded_categories \
                    or not self._severity_passes(fp.severity):
                res.filtered.append(fp)
                continue
            with self._lock:
                prior = self._entries.get(fp.key)
                if prior is not None:
                    prior.count += 1
                    if round_no not in prior.rounds:
                        prior.rounds.append(int(round_no))
                    prior.last_round = int(round_no)
                    if prior.carried:
                        pass   # 已结转：保持结转，不再回转
                    res.duplicate.append(fp)
                    continue
                if record:
                    entry = LedgerEntry(
                        key=fp.key, category=fp.category, severity=fp.severity,
                        path_hint=fp.path_hint, anchor=fp.anchor,
                        rounds=[int(round_no)], count=1,
                        first_round=int(round_no), last_round=int(round_no),
                    )
                    self._entries[fp.key] = entry
            res.fresh.append(fp)
        if record:
            self._persist()
        return res

    def _as_fingerprint(self, raw: Any) -> Optional[ReviewFingerprint]:
        """把入参归一化成指纹（形态不符 → None，宽进不炸）。"""
        if isinstance(raw, ReviewFingerprint):
            fp = raw
        elif isinstance(raw, dict):
            # 形状不符的条目**直接忽略**：没有任何可用字段（无 line_text/problem
            # 也无 key）的 dict 不该生成"空锚点"条目——否则所有空白条目会撞成
            # 同一个指纹，凭空造出一个"反复报到死"的假问题。
            if not raw.get("key") and not raw.get(
                    "line_text") and not raw.get("problem"):
                return None
            if raw.get("key") and "anchor" in raw:
                # 已是算好的指纹（可能来自跨仓事件）
                fp = ReviewFingerprint(
                    key=str(raw.get("key")),
                    category=_norm_cat(raw.get("category")),
                    severity=_norm_sev(raw.get("severity")),
                    anchor=str(raw.get("anchor") or ""),
                    path_hint=str(raw.get("path_hint") or raw.get("path") or ""),
                )
            else:
                fp = fingerprint(
                    raw.get("category", CAT_OTHER),
                    raw.get("line_text", raw.get("problem", "")),
                    raw.get("context") or (),
                    path=raw.get("path", raw.get("file", "")),
                )
                fp.severity = _norm_sev(raw.get("severity"))
        else:
            return None
        if not fp.key:
            return None
        return fp

    # ------------------------------------------------------------------
    # 结转（重复项不再回转）
    # ------------------------------------------------------------------

    def carry(self, keys: Iterable[Any]) -> int:
        """把指定指纹标记为**已结转**（再次出现只登记，不回转、不占轮次）。"""
        n = 0
        with self._lock:
            for k in keys or []:
                key = str(k or "").strip()
                entry = self._entries.get(key)
                if entry is None or entry.carried:
                    continue
                entry.carried = True
                n += 1
        if n:
            self._persist()
        return n

    def carried(self) -> List[LedgerEntry]:
        """已结转的评审项（Issue 台账里"登记但不回转"的那一份）。"""
        with self._lock:
            return [e for e in self._entries.values() if e.carried]

    # ------------------------------------------------------------------
    # 视图
    # ------------------------------------------------------------------

    def overview(self) -> Dict[str, Any]:
        """台账总览：几项、几项重复、几项结转（重复报的一眼可见）。"""
        with self._lock:
            entries = [e.to_dict() for e in self._entries.values()]
        entries.sort(key=lambda e: (-e["count"], e["key"]))
        repeated = [e for e in entries if e["count"] > 1]
        return {
            "total": len(entries),
            "repeated": repeated,
            "repeated_count": len(repeated),
            "carried": [e for e in entries if e["carried"]],
            "excluded_categories": list(self.excluded_categories),
            "min_severity": self.min_severity,
            "entries": entries,
            "guidance": ("同一问题（类别 + 语义锚点）只报一次；再出现只结转、不回转——"
                         "锚点不含路径/行号，改名与整体移位不算新问题"),
        }

    # ------------------------------------------------------------------
    # 持久化（损坏/缺失一律当空台账，不挡住干活）
    # ------------------------------------------------------------------

    def _path(self) -> Optional[str]:
        if not self.data_dir:
            return None
        return os.path.join(self.data_dir, "review-fingerprints.json")

    def _load(self) -> None:
        path = self._path()
        if not path or not os.path.isfile(path):
            return
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, ValueError):
            return
        items = data.get("entries") if isinstance(data, dict) else None
        if not isinstance(items, list):
            return
        with self._lock:
            for item in items:
                e = LedgerEntry.from_dict(item)
                if e is not None:
                    self._entries[e.key] = e

    def _persist(self) -> None:
        path = self._path()
        if not path:
            return
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with self._lock:
                payload = {"entries": [e.to_dict() for e in self._entries.values()]}
            tmp = path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False, indent=2)
            os.replace(tmp, path)
        except OSError:
            return
