"""跨仓库协同规则（CollabRules）：agent 协同的**全局参数**，一处定义、全域生效

## 为什么是"全局"

"等多长时间""最多往返几轮""什么算大版本要全量"这类参数与**具体仓库的业务**无关，
它们是 **agent 与 agent 之间的协同礼仪**。放在单个仓库里定义，就会：
每个仓库各写一套 → 数值漂移 → 换仓库/换 agent 行为不一致（正是"循环"的温床）。
故本模块把整包协同参数收敛成一个**可移植对象**：
`shared/collab/collab-rules.yaml`（跨仓库共用一份）→ 各仓库可**覆写**但不能各写一套语义。

## 覆盖的规则域

| 域 | 参数 | 口径来源 |
|---|---|---|
| 等待/退避 | `backoff.ladder`、`backoff.max_attempts` | 「10s → 30s → 60s → 3min → 6min → 10min 顶后停止」 |
| 锁 | `locks.retry_delay_sec`、`locks.persistent_grace_sec`、是否聚合台账 | 「延迟 1 分钟、重触发持续到对方解决完、所有锁聚合管理」 |
| 循环熔断 | `loop.max_rounds` 等 | 「🔴 最多 3 轮 / 🟡 最多 1 次 / 总 5 / 停滞 2」 |
| 审查范围 | `scan.major_version_tags` | 「日常只审变化，只有大版本全量」 |
| 计费口径 | `platform.*` | 平台文档（等待是**挂起**，不按等待时长计 CPU） |

## 计费口径（回答"长时间等待会不会占 CPU 计费"）

`PlatformCostNote` 把平台规则写进规则包，便于所有角色引用同一句话：

- **云原生构建 CPU** 按**任务实际运行**时长计（每 5 min 冻结 `5min × 节点规格`）；
- **AI Credits** 按**模型调用的 Token** 计；
- 二者都**没有"等待"这一计费项**：等待期间若走平台等待/挂起语义
  （`cnb:await` 等），**不产生 CPU 核时**；本地 `sleep` / 空转轮询才是在烧核时。

结论口径：**等待应该交给平台挂起，不能靠本地空转**——这也是本规则包把
"阶梯到顶即停 + 不做本地轮询"写成硬口径的原因。
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence

try:  # PyYAML 属可选依赖：缺失时退化为"内置默认 + 极简解析"
    import yaml as _yaml
except Exception:  # pragma: no cover - 运行环境无 PyYAML
    _yaml = None

from .backoff import (
    DEFAULT_LADDER,
    DEFAULT_MAX_ATTEMPTS,
    StepBackoff,
)

# 默认数（与既有实现同源，避免"规则包与代码各说一套"）
DEFAULT_LOCK_RETRY_DELAY_SEC = 60.0
DEFAULT_LOCK_PERSISTENT_GRACE_SEC = 180.0
DEFAULT_MAX_ROUNDS = 3
DEFAULT_MAX_ADVISORY_FIX_ROUNDS = 1
DEFAULT_MAX_TOTAL_ROUNDS = 5
DEFAULT_MAX_STALL_REPEATS = 2

# 规则包默认路径（仓库根/shared/collab/collab-rules.yaml，可被显式路径覆写）
DEFAULT_RULES_RELPATH = os.path.join("shared", "collab", "collab-rules.yaml")


def _as_float(value: Any, default: float) -> float:
    """宽进读数字段（bool / 非数字 → 默认值）。"""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return default
    return float(value)


def _as_int(value: Any, default: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        return default
    return value


def _as_ladder(value: Any) -> Optional[List[float]]:
    """读阶梯字段（非列表/含非法项 → None，交由调用方回落默认）。"""
    if not isinstance(value, (list, tuple)) or not value:
        return None
    out: List[float] = []
    for item in value:
        if isinstance(item, bool) or not isinstance(item, (int, float)):
            return None
        if float(item) <= 0:
            return None
        out.append(float(item))
    return out


@dataclass
class PlatformCostNote:
    """平台计费口径（写进规则包，供所有角色/文档引用同一句话）。"""

    cpu_unit: str = "核时"
    cpu_formula: str = "自然月内累积用量 − 免费额度；每 5min 冻结 5min × 节点规格"
    ai_unit: str = "AI Credits"
    ai_formula: str = "每次模型调用按实际 Token 消耗计（NPC / AI 评审 / 内置 CodeBuddy）"
    waiting_billed: bool = False       # 等待本身**不计** CPU
    waiting_note: str = (
        "等待没有独立计费项：走平台等待/挂起（如 cnb:await）不产生 CPU 核时；"
        "本地 sleep 或空转轮询才在烧核时。故等待一律交给平台挂起，本地不做轮询。"
    )
    pause_hint: str = "阶梯到顶（10 分钟）即停，不做本地死循环；长等待交给平台等待/挂起语义"

    def to_dict(self) -> dict:
        return {
            "cpu_unit": self.cpu_unit,
            "cpu_formula": self.cpu_formula,
            "ai_unit": self.ai_unit,
            "ai_formula": self.ai_formula,
            "waiting_billed": self.waiting_billed,
            "waiting_note": self.waiting_note,
            "pause_hint": self.pause_hint,
        }

    @classmethod
    def from_dict(cls, data: Any) -> "PlatformCostNote":
        if not isinstance(data, dict):
            return cls()

        def _s(k: str, d: str) -> str:
            v = data.get(k)
            return v if isinstance(v, str) and v else d

        wb = data.get("waiting_billed", False)
        return cls(
            cpu_unit=_s("cpu_unit", cls.cpu_unit),
            cpu_formula=_s("cpu_formula", cls.cpu_formula),
            ai_unit=_s("ai_unit", cls.ai_unit),
            ai_formula=_s("ai_formula", cls.ai_formula),
            waiting_billed=bool(wb) if isinstance(wb, bool) else False,
            waiting_note=_s("waiting_note", cls.waiting_note),
            pause_hint=_s("pause_hint", cls.pause_hint),
        )


@dataclass
class CollabRules:
    """跨仓库协同规则包（一处定义，全域生效）。

    Args:
        source: 规则来源（文件路径 / "default"），便于留痕"这套数值从哪来"
    """

    backoff: StepBackoff = field(default_factory=StepBackoff)
    lock_retry_delay_sec: float = DEFAULT_LOCK_RETRY_DELAY_SEC
    lock_persistent_grace_sec: float = DEFAULT_LOCK_PERSISTENT_GRACE_SEC
    lock_registry_shared: bool = True
    max_rounds: int = DEFAULT_MAX_ROUNDS
    max_advisory_fix_rounds: int = DEFAULT_MAX_ADVISORY_FIX_ROUNDS
    max_total_rounds: int = DEFAULT_MAX_TOTAL_ROUNDS
    max_stall_repeats: int = DEFAULT_MAX_STALL_REPEATS
    major_version_tags: List[str] = field(
        default_factory=lambda: ["major-version", "大版本", "release-major"])
    full_scan_keywords: List[str] = field(
        default_factory=lambda: ["大版本", "全量", "major version", "full scan"])
    scan_keywords: List[str] = field(
        default_factory=lambda: ["只审变化", "增量", "只跑增量", "scan only"])
    platform: PlatformCostNote = field(default_factory=PlatformCostNote)
    source: str = "default"
    extras: Dict[str, Any] = field(default_factory=dict)

    # ------------------------------------------------------------------
    # 视图
    # ------------------------------------------------------------------

    def waiting_ladder(self) -> List[float]:
        """等待阶梯（秒）——「从少到多，到顶即停」的唯一数值源。"""
        return [float(x) for x in self.backoff.ladder]

    def waiting_plan(self) -> str:
        """一行人类可读的等待计划（文档/评论/角色提示词共用一句话）。"""
        return self.backoff.human()

    def to_dict(self) -> dict:
        return {
            "source": self.source,
            "backoff": self.backoff.to_dict(),
            "locks": {
                "retry_delay_sec": self.lock_retry_delay_sec,
                "persistent_grace_sec": self.lock_persistent_grace_sec,
                "registry_shared": self.lock_registry_shared,
            },
            "loop": {
                "max_rounds": self.max_rounds,
                "max_advisory_fix_rounds": self.max_advisory_fix_rounds,
                "max_total_rounds": self.max_total_rounds,
                "max_stall_repeats": self.max_stall_repeats,
            },
            "scan": {
                "major_version_tags": list(self.major_version_tags),
                "full_scan_keywords": list(self.full_scan_keywords),
                "scan_keywords": list(self.scan_keywords),
            },
            "platform": self.platform.to_dict(),
        }

    # ------------------------------------------------------------------
    # 装载
    # ------------------------------------------------------------------

    @classmethod
    def defaults(cls) -> "CollabRules":
        """内置默认（无规则文件时的兜底；与既有默认完全一致）。"""
        return cls()

    @classmethod
    def from_dict(cls, data: Any, source: str = "dict") -> "CollabRules":
        """从（已解析的）配置字典构造；形状不符的字段一律回落默认（宽进不炸）。"""
        if not isinstance(data, dict):
            return cls(source=source)

        bo_raw = data.get("backoff")
        ladder = _as_ladder(bo_raw.get("ladder")) if isinstance(bo_raw, dict) else None
        max_attempts = (_as_int(bo_raw.get("max_attempts"), DEFAULT_MAX_ATTEMPTS)
                        if isinstance(bo_raw, dict) else DEFAULT_MAX_ATTEMPTS)
        try:
            backoff = StepBackoff(ladder=ladder or DEFAULT_LADDER,
                                  max_attempts=max_attempts)
        except ValueError:
            backoff = StepBackoff()

        locks = data.get("locks") if isinstance(data.get("locks"), dict) else {}
        loop = data.get("loop") if isinstance(data.get("loop"), dict) else {}
        scan = data.get("scan") if isinstance(data.get("scan"), dict) else {}

        def _strs(key: str, default: Sequence[str]) -> List[str]:
            v = scan.get(key)
            if not isinstance(v, (list, tuple)):
                return list(default)
            out = [x for x in v if isinstance(x, str) and x]
            return out or list(default)

        registry_shared = locks.get("registry_shared", True)
        return cls(
            backoff=backoff,
            lock_retry_delay_sec=_as_float(
                locks.get("retry_delay_sec"), DEFAULT_LOCK_RETRY_DELAY_SEC),
            lock_persistent_grace_sec=_as_float(
                locks.get("persistent_grace_sec"), DEFAULT_LOCK_PERSISTENT_GRACE_SEC),
            lock_registry_shared=(bool(registry_shared)
                                  if isinstance(registry_shared, bool) else True),
            max_rounds=_as_int(loop.get("max_rounds"), DEFAULT_MAX_ROUNDS),
            max_advisory_fix_rounds=_as_int(
                loop.get("max_advisory_fix_rounds"), DEFAULT_MAX_ADVISORY_FIX_ROUNDS),
            max_total_rounds=_as_int(
                loop.get("max_total_rounds"), DEFAULT_MAX_TOTAL_ROUNDS),
            max_stall_repeats=_as_int(
                loop.get("max_stall_repeats"), DEFAULT_MAX_STALL_REPEATS),
            major_version_tags=_strs("major_version_tags", cls.major_version_tags_default()),
            full_scan_keywords=_strs("full_scan_keywords", cls.full_scan_keywords_default()),
            scan_keywords=_strs("scan_keywords", cls.scan_keywords_default()),
            platform=PlatformCostNote.from_dict(data.get("platform")),
            source=source,
        )

    # 默认清单（类级常量，供 from_dict 回落；写成方法避免 dataclass 字段默认共享）
    @staticmethod
    def major_version_tags_default() -> List[str]:
        return ["major-version", "大版本", "release-major"]

    @staticmethod
    def full_scan_keywords_default() -> List[str]:
        return ["大版本", "全量", "major version", "full scan"]

    @staticmethod
    def scan_keywords_default() -> List[str]:
        return ["只审变化", "增量", "只跑增量", "scan only"]

    @classmethod
    def from_yaml(cls, path: str) -> "CollabRules":
        """从 YAML 文件装载；文件缺失/损坏/无 PyYAML → 返回默认（不抛）。"""
        if not isinstance(path, str) or not os.path.isfile(path):
            return cls(source=f"default（未找到 {path}）")
        try:
            with open(path, "r", encoding="utf-8") as f:
                text = f.read()
        except OSError:
            return cls(source=f"default（读取失败 {path}）")
        data = _parse_yaml(text)
        if not isinstance(data, dict):
            return cls(source=f"default（解析失败 {path}）")
        return cls.from_dict(data, source=path)


def _parse_yaml(text: str) -> Any:
    """解析 YAML：优先 PyYAML；无 PyYAML 时用**极小**子集解析器兜底。

    子集覆盖规则包所需的形态：嵌套映射、简单列表（含内联 ``[a, b]``）、
    标量、注释与空行。复杂 YAML 特性（锚点/多文档）不在子集内——
    此时若无 PyYAML，宁可回落默认，也不静默读错数值。
    """
    if _yaml is not None:
        try:
            return _yaml.safe_load(text)
        except Exception:  # pragma: no cover - 交由兜底解析器
            pass
    return _mini_yaml(text)


def _mini_yaml(text: str) -> Dict[str, Any]:
    """极简 YAML 子集解析（无 PyYAML 时的兜底；失败返回空 dict）。"""
    root: Dict[str, Any] = {}
    stack: List[tuple] = [(-1, root)]

    def _scalar(raw: str) -> Any:
        s = raw.strip()
        if not s:
            return ""
        if (s[0] == s[-1]) and s[0] in ('"', "'") and len(s) >= 2:
            return s[1:-1]
        if s.startswith("[") and s.endswith("]"):
            items = [x.strip() for x in s[1:-1].split(",")]
            out: List[Any] = []
            for it in items:
                if not it:
                    continue
                val = _scalar(it)
                out.append(val)
            return out
        low = s.lower()
        if low in ("true", "false"):
            return low == "true"
        try:
            return int(s)
        except ValueError:
            pass
        try:
            return float(s)
        except ValueError:
            return s

    for raw_line in text.splitlines():
        line = raw_line.split("#", 1)[0].rstrip()
        if not line.strip():
            continue
        indent = len(line) - len(line.lstrip(" "))
        body = line.strip()
        while stack and indent <= stack[-1][0]:
            stack.pop()
        if not stack:
            stack = [(-1, root)]
        parent = stack[-1][1]
        if body.startswith("- "):
            item = body[2:].strip()
            if isinstance(parent, list):
                if ":" in item:
                    k, _, v = item.partition(":")
                    d: Dict[str, Any] = {k.strip(): _scalar(v)}
                    parent.append(d)
                    stack.append((indent, d))
                else:
                    parent.append(_scalar(item))
            continue
        if ":" not in body:
            continue
        key, _, val = body.partition(":")
        key = key.strip()
        val = val.strip()
        if val:
            parent[key] = _scalar(val)
        else:
            child: Any = {}
            parent[key] = child
            stack.append((indent, child))
    return root


def load_rules(
    path: Optional[str] = None,
    *,
    search_from: Optional[str] = None,
) -> CollabRules:
    """装载跨仓库协同规则。

    查找顺序（先近后远，都找不到则内置默认）：
    1. 显式 ``path``；
    2. 环境变量 ``CNB_COLLAB_RULES``；
    3. 从 ``search_from``（默认当前工作目录）逐级向上找
       ``shared/collab/collab-rules.yaml`` —— 支撑"仓库内任何子目录都能取到同一份"。
    """
    if isinstance(path, str) and path:
        return CollabRules.from_yaml(path)
    env_path = os.environ.get("CNB_COLLAB_RULES", "")
    if env_path:
        return CollabRules.from_yaml(env_path)
    start = os.path.abspath(search_from or os.getcwd())
    cur = start
    for _ in range(64):        # 有界向上（防符号链接成环）
        candidate = os.path.join(cur, DEFAULT_RULES_RELPATH)
        if os.path.isfile(candidate):
            return CollabRules.from_yaml(candidate)
        parent = os.path.dirname(cur)
        if parent == cur:
            break
        cur = parent
    return CollabRules(source="default（未找到跨仓库规则包）")
