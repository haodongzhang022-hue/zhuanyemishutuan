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
| 探测 | `probe.max_checks`、`probe.drivers`、`probe.crontab_min_interval_sec` | 「最多 6 次；非轮询，检查点 + webhook 再触发；等锁」 |
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
DEFAULT_PROBE_MAX_CHECKS = 6                 # 「最多 6 次就行」= 阶梯档位数
DEFAULT_PROBE_DRIVERS = [
    "platform_await",        # 首选：挂 cnb:await，状态变化时 resolve 唤醒（零占用）
    "platform_crontab",      # 次选：平台定时任务到点代跑（长等待；≥5 分钟）
    "external_webhook",      # 外部 webhook / OPENAPI 触发 api_trigger 再跑
    "delegate_event",        # 委托持有者完成时回调 api_trigger_* 事件
    "handoff_resume",        # 预算用尽 → 交回人工/上游
]
DEFAULT_PROBE_AWAIT_MAX_SEC = 300.0          # ≤5 分钟挂 await，超过交 crontab
DEFAULT_CRONTAB_MIN_INTERVAL_SEC = 300.0     # 平台定时任务最小间隔（官方文档）
DEFAULT_CRONTAB_INTERVAL_MINUTES = 5         # 平台定时任务间隔：固定一档（已确认口径 #2）
DEFAULT_PROBE_CHECKS = ["confirm", "lock", "version", "event", "resource"]

DEFAULT_LOCK_RETRY_DELAY_SEC = 60.0
DEFAULT_LOCK_PERSISTENT_GRACE_SEC = 180.0
DEFAULT_MAX_ROUNDS = 3
DEFAULT_MAX_ADVISORY_FIX_ROUNDS = 1
DEFAULT_MAX_TOTAL_ROUNDS = 5
DEFAULT_MAX_STALL_REPEATS = 2

# 规则包默认路径（仓库根/shared/collab/collab-rules.yaml，可被显式路径覆写）
DEFAULT_RULES_RELPATH = os.path.join("shared", "collab", "collab-rules.yaml")

# 组织级（**跨仓库**）规则包路径——已确认口径 #3：「推到组织级共享」
# 组织级是**更高一层**的口径：放组织维度的共享目录，所有仓库取到同一份；
# 仓库级的 `shared/collab/collab-rules.yaml` 只作**数值覆写**，语义不变。
ORGANIZATION_RULES_RELPATH = os.path.join("shared", "collab", "organization.yaml")

# 作用域（写进 `source`，一眼看出"这套数值是哪一级给的"）
RULES_SCOPE_ORGANIZATION = "organization"     # 组织级：跨仓库，最高优先级
RULES_SCOPE_REPO = "repo"                     # 仓库级：只允许覆写数值
RULES_SCOPE_DEFAULT = "default"               # 内置默认

# 组织级共享的环境变量（指向组织级规则包/共享目录）
ENV_ORG_RULES = "CNB_ORG_COLLAB_RULES"
ENV_ORG_DIR = "CNB_ORG_SHARED_DIR"


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
    probe_max_checks: int = DEFAULT_PROBE_MAX_CHECKS
    probe_drivers: List[str] = field(
        default_factory=lambda: list(DEFAULT_PROBE_DRIVERS))
    probe_await_max_sec: float = DEFAULT_PROBE_AWAIT_MAX_SEC
    crontab_min_interval_sec: float = DEFAULT_CRONTAB_MIN_INTERVAL_SEC
    # 平台定时任务间隔（分钟）：**固定一档**（已确认口径 #2）。
    # 只读不"放大"——低于平台下限会被夹到下限；调用方给更大值即认为是有意疏化扫描。
    crontab_interval_minutes: int = DEFAULT_CRONTAB_INTERVAL_MINUTES
    probe_checks: List[str] = field(default_factory=lambda: list(DEFAULT_PROBE_CHECKS))
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
    # 规则作用域（organization / repo / default）——一眼看出数值来自哪一级
    scope: str = RULES_SCOPE_DEFAULT
    # 组织级与仓库级的来源（跨仓共享的**留痕**：同一份组织级口径被谁覆写过）
    chain: List[Dict[str, Any]] = field(default_factory=list)
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

    def probe_plan(self) -> str:
        """一行人类可读的探测计划（文档/评论/角色提示词共用一句话）。

        落点分工与 cron **固定档位**都在这一句话里——所有角色引用同一句，
        不再各写一套（口径漂移往往就是从"文档里这句和代码里那句不一样"开始的）。
        """
        every = self.crontab_every_minutes()
        return (f"检查点探测：最多 {self.probe_max_checks} 次（= 等待阶梯档位数）；"
                f"落点 {self.probe_drivers[0]}（≤{self.probe_await_max_sec:.0f}s）→ "
                f"{self.probe_drivers[1]}（>{self.probe_await_max_sec:.0f}s，"
                f"固定 */{every} 一档扫）；本地绝不 sleep/轮询")

    def crontab_every_minutes(self) -> int:
        """平台定时任务的实际间隔（分钟）= `max(平台下限, 配置值)`。

        下限夹取是刻意的：**配不出比平台允许更密的扫描**（低于下限根本提交不了）。
        """
        return max(int(self.crontab_min_interval_sec // 60),
                   int(self.crontab_interval_minutes))

    def to_dict(self) -> dict:
        return {
            "source": self.source,
            "scope": self.scope,
            "chain": [dict(c) for c in self.chain],
            "backoff": self.backoff.to_dict(),
            "probe": {
                "max_checks": self.probe_max_checks,
                "drivers": list(self.probe_drivers),
                "await_max_sec": self.probe_await_max_sec,
                "crontab_min_interval_sec": self.crontab_min_interval_sec,
                "crontab_interval_minutes": self.crontab_interval_minutes,
                "checks": list(self.probe_checks),
            },
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
    def from_dict(cls, data: Any, source: str = "dict",
                  scope: str = RULES_SCOPE_DEFAULT,
                  chain: Optional[List[Dict[str, Any]]] = None) -> "CollabRules":
        """从（已解析的）配置字典构造；形状不符的字段一律回落默认（宽进不炸）。"""
        if not isinstance(data, dict):
            return cls(source=source, scope=scope, chain=list(chain or []))

        bo_raw = data.get("backoff")
        ladder = _as_ladder(bo_raw.get("ladder")) if isinstance(bo_raw, dict) else None
        max_attempts = (_as_int(bo_raw.get("max_attempts"), DEFAULT_MAX_ATTEMPTS)
                        if isinstance(bo_raw, dict) else DEFAULT_MAX_ATTEMPTS)
        try:
            backoff = StepBackoff(ladder=ladder or DEFAULT_LADDER,
                                  max_attempts=max_attempts)
        except ValueError:
            backoff = StepBackoff()

        probe = data.get("probe") if isinstance(data.get("probe"), dict) else {}
        locks = data.get("locks") if isinstance(data.get("locks"), dict) else {}
        loop = data.get("loop") if isinstance(data.get("loop"), dict) else {}
        scan = data.get("scan") if isinstance(data.get("scan"), dict) else {}

        def _strs(key: str, default: Sequence[str]) -> List[str]:
            v = scan.get(key)
            if not isinstance(v, (list, tuple)):
                return list(default)
            out = [x for x in v if isinstance(x, str) and x]
            return out or list(default)

        def _str_list(src: Dict[str, Any], key: str,
                      default: Sequence[str]) -> List[str]:
            v = src.get(key)
            if not isinstance(v, (list, tuple)):
                return list(default)
            out = [x for x in v if isinstance(x, str) and x]
            return out or list(default)

        # 过滤未知落点（**语义域**：只接受平台原生落点），但**保留调用方的顺序**
        # ——顺序是仓库级的"优先级表达"，属可覆写范围。
        _raw_drivers = _str_list(probe, "drivers", DEFAULT_PROBE_DRIVERS)
        probe_drivers = [d for d in _raw_drivers
                         if d in DEFAULT_PROBE_DRIVERS] or list(DEFAULT_PROBE_DRIVERS)
        # 若调用方显式给的清单里**混入了未知落点**，说明想改语义 → 整份回落默认
        # （宁可回落，也不"过滤掉未知项后留下半个语义"）。
        if any(d not in DEFAULT_PROBE_DRIVERS for d in _raw_drivers):
            probe_drivers = list(DEFAULT_PROBE_DRIVERS)
        probe_checks = _str_list(probe, "checks", DEFAULT_PROBE_CHECKS)
        crontab_min = max(
            DEFAULT_CRONTAB_MIN_INTERVAL_SEC,
            _as_float(probe.get("crontab_min_interval_sec"),
                      DEFAULT_CRONTAB_MIN_INTERVAL_SEC))
        registry_shared = locks.get("registry_shared", True)
        return cls(
            backoff=backoff,
            probe_max_checks=max(1, _as_int(probe.get("max_checks"),
                                            DEFAULT_PROBE_MAX_CHECKS)),
            probe_drivers=probe_drivers,
            probe_await_max_sec=_as_float(probe.get("await_max_sec"),
                                          DEFAULT_PROBE_AWAIT_MAX_SEC),
            crontab_min_interval_sec=crontab_min,
            crontab_interval_minutes=max(
                1, _as_int(probe.get("crontab_interval_minutes"),
                           DEFAULT_CRONTAB_INTERVAL_MINUTES)),
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
            probe_checks=probe_checks,
            platform=PlatformCostNote.from_dict(data.get("platform")),
            source=source,
            scope=scope if scope in (RULES_SCOPE_ORGANIZATION, RULES_SCOPE_REPO)
            else RULES_SCOPE_DEFAULT,
            chain=[dict(c) for c in (chain or []) if isinstance(c, dict)],
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
    def from_yaml(cls, path: str, scope: str = RULES_SCOPE_REPO,
                  chain: Optional[List[Dict[str, Any]]] = None) -> "CollabRules":
        """从 YAML 文件装载；文件缺失/损坏/无 PyYAML → 返回默认（不抛）。"""
        if not isinstance(path, str) or not os.path.isfile(path):
            return cls(source=f"default（未找到 {path}）", scope=RULES_SCOPE_DEFAULT,
                       chain=list(chain or []))
        try:
            with open(path, "r", encoding="utf-8") as f:
                text = f.read()
        except OSError:
            return cls(source=f"default（读取失败 {path}）", scope=RULES_SCOPE_DEFAULT,
                       chain=list(chain or []))
        data = _parse_yaml(text)
        if not isinstance(data, dict):
            return cls(source=f"default（解析失败 {path}）", scope=RULES_SCOPE_DEFAULT,
                       chain=list(chain or []))
        return cls.from_dict(data, source=path, scope=scope, chain=chain)


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


def find_rules_file(
    scope: str = RULES_SCOPE_REPO,
    search_from: Optional[str] = None,
) -> str:
    """按**作用域**查找规则包文件（找不到返回空串）。

    - ``organization``：先看 ``CNB_ORG_COLLAB_RULES`` / ``CNB_COLLAB_RULES``；
      再看 ``CNB_ORG_SHARED_DIR`` 下的 ``collab/organization.yaml``；
      最后从 ``search_from`` 逐级向上找 ``shared/collab/organization.yaml``
      ——**支撑"一份口径、全域生效"**（同一份被所有仓库取到）。
    - ``repo``：环境变量 ``CNB_COLLAB_RULES`` → 逐级向上找
      ``shared/collab/collab-rules.yaml``。
    """
    if scope == RULES_SCOPE_ORGANIZATION:
        # 环境变量既可直接给文件，也可给共享目录
        direct = os.environ.get(ENV_ORG_RULES, "")
        if direct and os.path.isfile(direct):
            return direct
        org_dir = os.environ.get(ENV_ORG_DIR, "")
        if org_dir:
            cand = os.path.join(org_dir, "collab", "organization.yaml")
            if os.path.isfile(cand):
                return cand
            cand2 = os.path.join(org_dir, "organization.yaml")
            if os.path.isfile(cand2):
                return cand2
    else:
        direct = os.environ.get("CNB_COLLAB_RULES", "")
        if direct and os.path.isfile(direct):
            return direct
    relpath = (ORGANIZATION_RULES_RELPATH
               if scope == RULES_SCOPE_ORGANIZATION else DEFAULT_RULES_RELPATH)
    cur = os.path.abspath(search_from or os.getcwd())
    for _ in range(64):        # 有界向上（防符号链接成环）
        candidate = os.path.join(cur, relpath)
        if os.path.isfile(candidate):
            return candidate
        parent = os.path.dirname(cur)
        if parent == cur:
            break
        cur = parent
    return ""


def load_rules_chain(
    search_from: Optional[str] = None,
    org_path: Optional[str] = None,
    repo_path: Optional[str] = None,
) -> CollabRules:
    """**组织级 → 仓库级**逐层装载（已确认口径 #3：probe 规则推到组织级共享）。

    语义：

    1. **组织级**（跨仓库同一份）是基线：`shared/collab/organization.yaml`
       或 `CNB_ORG_COLLAB_RULES` / `CNB_ORG_SHARED_DIR`；
    2. **仓库级**只做**数值覆写**（`shared/collab/collab-rules.yaml` /
       `CNB_COLLAB_RULES`），缺省即沿用组织级；
    3. 两处都没有 → 内置默认（不抛、不静默乱读）。

    返回值里的 `chain` 记录"哪一级给了哪些域"，`source` 标明生效层级——
    一眼看出"这套数值全组织共用同一份，还是被本仓覆写过"。
    """
    chain: List[Dict[str, Any]] = []
    org_file = (org_path if isinstance(org_path, str) and os.path.isfile(org_path)
                else "")
    if not org_file:
        org_file = find_rules_file(RULES_SCOPE_ORGANIZATION,
                                   search_from=search_from)
    org_rules = (CollabRules.from_yaml(org_file, scope=RULES_SCOPE_ORGANIZATION)
                 if org_file else None)
    if org_rules is not None and org_file:
        chain.append({"scope": RULES_SCOPE_ORGANIZATION, "path": org_file})
    elif isinstance(org_path, str) and org_path and not os.path.isfile(org_path):
        chain.append({"scope": RULES_SCOPE_ORGANIZATION, "path": "",
                      "note": f"未找到组织级规则包 {org_path}（回落默认）"})

    # 注意：`repo_path`/`org_path` 给的是**候选路径**——文件不存在时按"没给"处理，
    # 不能把它当成"仓库级来源"（否则会把组织级正确的口径覆盖成默认值）。
    repo_file = (repo_path if isinstance(repo_path, str)
                 and os.path.isfile(repo_path) else "")
    if not repo_file:
        repo_file = find_rules_file(RULES_SCOPE_REPO, search_from=search_from)
    repo_rules = (CollabRules.from_yaml(repo_file, scope=RULES_SCOPE_REPO)
                  if repo_file else None)
    if repo_rules is not None and repo_file:
        chain.append({"scope": RULES_SCOPE_REPO, "path": repo_file})

    base = org_rules or CollabRules(source="default（未找到组织级/仓库级规则包）")
    if repo_rules is None:
        base.chain = chain
        return base
    # 仓库级覆写：**数值级合并**（组织级没给的域沿用组织级；给了的以仓库级为准）
    merged = _merge_rules(base, repo_rules)
    merged.scope = RULES_SCOPE_REPO
    merged.source = f"{base.source} + {repo_rules.source}"
    merged.chain = chain
    return merged


# 允许仓库级覆写的**数值域**（语义域不可改：判别式清单、落点清单的**成员资格**不可改）
_OVERRIDABLE_FIELDS = (
    "probe_max_checks", "probe_await_max_sec", "crontab_min_interval_sec",
    "lock_retry_delay_sec", "lock_persistent_grace_sec", "lock_registry_shared",
    "max_rounds", "max_advisory_fix_rounds", "max_total_rounds",
    "max_stall_repeats", "major_version_tags", "full_scan_keywords",
    "scan_keywords", "probe_checks",
)


def _merge_rules(base: "CollabRules", overlay: "CollabRules") -> "CollabRules":
    """组织级基线 + 仓库级覆写（**只合数值，不合语义**）。

    - 数值/清单：以仓库级为准（仓库级未显式给出的域沿用组织级——通过
      "与内置默认相同即视为未覆写"判定，避免把默认值当成覆写）；
    - **落点清单（drivers）的成员资格不可改**：仓库级只能排序不能增删，
      否则换个仓库就能把"落点"改成非平台语义（那正是要防的漏口）；
    - 阶梯：仓库级显式给了合法阶梯才替换，否则沿用组织级的。
    """
    out = CollabRules.from_dict(base.to_dict(), source=base.source, scope=base.scope)
    # 阶梯：仓库级与内置默认不同（= 显式覆写）才采用，否则沿用组织级
    if list(overlay.backoff.ladder) != list(StepBackoff().ladder):
        out.backoff = overlay.backoff
    defaults = CollabRules()
    for field_name in _OVERRIDABLE_FIELDS:
        val = getattr(overlay, field_name)
        if val != getattr(defaults, field_name):
            setattr(out, field_name, val)
    # drivers：**成员资格不可改**（只允许重排）——增删一律忽略（语义域）。
    # 判据是**与组织级基线的集合是否一致**：
    #   - 集合被改过（增/删落点）→ 语义域，忽略，沿用组织级；
    #   - 集合一致但顺序不同 → 纯重排，允许（便于仓库按本地流程调优先级）；
    #   - 与组织级完全相同 → 视为"没动过"，沿用组织级（不做无意义覆写）。
    if set(overlay.probe_drivers) == set(base.probe_drivers):
        out.probe_drivers = (list(overlay.probe_drivers)
                            if list(overlay.probe_drivers)
                            != list(base.probe_drivers)
                            else list(base.probe_drivers))
    else:
        out.probe_drivers = list(base.probe_drivers)
    # 计费口径：仓库级与默认不同才采用（避免覆盖组织级的自定义说明）
    out.platform = (overlay.platform if overlay.platform != defaults.platform
                    else base.platform)
    return out


def load_rules(
    path: Optional[str] = None,
    *,
    search_from: Optional[str] = None,
    scope: str = "",
    org_path: Optional[str] = None,
    repo_path: Optional[str] = None,
) -> CollabRules:
    """装载跨仓库协同规则（**组织级 → 仓库级 → 内置默认**）。

    查找顺序：
    1. 显式 ``path``（最高优先级，调用方明确口径时以它为准）；
    2. ``scope="organization"`` → 只取**组织级**（跨仓库同一份）；
       ``scope="repo"`` → 只取**仓库级**；
       不传 ``scope`` → 走 ``load_rules_chain()``（组织级打底 + 仓库级覆写）。

    组织级查找（三处，先到先得）：
    ``CNB_ORG_COLLAB_RULES`` → ``CNB_ORG_SHARED_DIR/collab/organization.yaml``
    → 从 ``search_from`` 逐级向上 ``shared/collab/organization.yaml``。

    仓库级查找：``CNB_COLLAB_RULES`` → 逐级向上 ``shared/collab/collab-rules.yaml``。
    """
    if isinstance(path, str) and path:
        return CollabRules.from_yaml(path, scope=RULES_SCOPE_REPO)
    scope = scope if scope in (RULES_SCOPE_ORGANIZATION, RULES_SCOPE_REPO) else ""
    if scope == RULES_SCOPE_ORGANIZATION:
        found = org_path or find_rules_file(RULES_SCOPE_ORGANIZATION,
                                            search_from=search_from)
        if found:
            return CollabRules.from_yaml(found, scope=RULES_SCOPE_ORGANIZATION)
        return CollabRules(source="default（未找到组织级规则包）")
    if scope == RULES_SCOPE_REPO:
        found = repo_path or find_rules_file(RULES_SCOPE_REPO,
                                             search_from=search_from)
        if found:
            return CollabRules.from_yaml(found, scope=RULES_SCOPE_REPO)
        return CollabRules(source="default（未找到仓库级规则包）")
    return load_rules_chain(search_from=search_from, org_path=org_path,
                            repo_path=repo_path)
