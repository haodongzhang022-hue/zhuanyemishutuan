"""全量门禁（FullScanGate）：**默认全量根本不跑**，而不是"默认不跑"

## 需求（Issue #14 本轮追问，用户口径）

> 这里我要求过不跑全量，他怎么还在一直连续跑三次稳定这种事情？是我们新配置的，
> 避免跑全量的那个规则现在有问题吗？…他不然我们新开发一步，他就要跑一次 600 多次的验证。

## 上一轮为什么没拦住（值得写清楚）

上一轮做了两件事，方向都对，但**都不够**：

| 上一轮的做法 | 为什么没拦住 |
|---|---|
| 规则包补 `scan.default_mode: scan`（默认增量） | 这是**默认值**，不是**门禁**——调用方显式给 `mode="full"`（例如"跑全量回归验证一下"）就直接绕过了 |
| 新增 `RunScopeGuard` 判重（同一口径只跑一次） | 判重的比较键含 `revision`。**每写一行代码 revision 就变**，指纹变 → 每次都算"新口径" → 每次都放行 → "每开发一步跑一次 600 多项"、"连跑三次稳定" |

所以本次不是"再提醒一遍"，而是补两道**硬闸**：

1. **`decide_full()` 门禁**：`full` **只有两种情况允许**——显式大版本标记，
   或调用方给出"通过官方 action 判断的大版本"的**凭据**（`allow_full=`）。
   其余一切情形（含"拿不准"）一律 **`blocked_full`**，并给出增量的可直接跑命令。
2. **去升级语义（`repeat_scope`）**：同一份变更集在不同 `revision` 下重复执行，
   按**受审内容**归并（`content` 档），而不是按 revision 各算一次。
   `window_repeats > 1`（"连跑三次求稳定"）**红绿都拒**，且**不受口径字段影响**——
   它压根不是一个可以靠"换口径"绕过的判定。

## 判定的方向性（重要）

「该不该跑全量」与「该不该跳过重复」**方向相反**，不能共用一套保守性：

- **跳过重复**：判不出 → **保守放行**（宁可多跑，不可漏跑）；
- **全量门禁**：判不出 → **保守拦截**（宁可少跑全量，不可空烧全量构建/测试）。

两者混用过的后果，正是用户看到的"每开发一步就跑一次 600 多项全量验证"。

## 与官方口径的关系

官方 AI 评审插件默认 `review_event: auto` + **基于 diff 的增量评审**；
官方 SCA 插件只报**本次新增**。即：平台自身的最佳实践就是**默认增量**。
本模块把这条从"插件默认值"提升为**本项目内可执行的门禁**，并把
"全量"这件事收敛到**唯一入口**（大版本 action）。

出处：`/zh/build/internal-steps/cnb/await-resolve.md`（等待语义）、
`https://cnb.cool/cnb/plugins/cnbcool/code-review`（官方增量评审默认值）、
`/zh/security/sca-vulnerability.md`（官方增量漏洞口径）。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from .rules import CollabRules
from .run_guard import _is_red, _norm_impact, _norm_revision


def _norm_event(event: Any) -> str:
    """事件名归一化（与 `platform.norm_event` 同一口径，导入延迟以避成环）。"""
    from .platform import norm_event
    return norm_event(event)

# 判定动作
RUN = "run"                      # 放行（增量，或大版本显式的全量）
BLOCKED_FULL = "blocked_full"    # 想跑全量但**不满足大版本条件** → 拦截，改跑增量
SKIP_DUPLICATE = "skip_duplicate"    # 同一份受审内容、同一命令已跑过 → 跳过
BLOCKED_STABILITY = "blocked_stability"   # 想"连跑 N 次"当稳定性论证 → 拒绝

# 触发来源（**凭据**：只有非"agent 自述"的来源才算数）
SOURCE_ACTION = "action"          # 平台/仓库 action（如 major-version 发布流程）
SOURCE_TAG = "tag"                # 大版本标签
SOURCE_OPERATOR = "operator"      # 人工显式指令
SOURCE_AGENT_CLAIM = "agent_claim"    # **agent 自己说"这是大版本" → 不算凭据**

#: 可被采信的来源（"不是你自己说了算"）
CREDIBLE_SOURCES = (SOURCE_ACTION, SOURCE_TAG, SOURCE_OPERATOR)

#: 去升级模式
SCOPE_REVISION = "revision"      # 口径含 revision（新提交 = 新口径）
SCOPE_CONTENT = "content"        # 按**受审内容**归一，不同 revision 的同一份内容 = 同一口径

#: 默认是否对"重复决策"启用去升级
DEFAULT_REPEAT_SCOPE = SCOPE_CONTENT


@dataclass
class FullGateDecision:
    """全量门禁的判定结果（可直接渲染成给 agent 的一句话处置）。"""

    action: str = RUN
    mode: str = "scan"
    reason: str = ""
    signals: List[str] = field(default_factory=list)
    # 拦截后给出的**可直接跑**的增量口径（不能只说"不许"）
    scan_command: str = ""
    scan_targets: str = ""
    # 判定所依据的可判定事实（不看 agent 自述）
    source: str = SOURCE_AGENT_CLAIM
    tags: List[str] = field(default_factory=list)
    major_version: Optional[bool] = None

    @property
    def should_run(self) -> bool:
        return self.action == RUN

    @property
    def is_full(self) -> bool:
        return self.should_run and self.mode == "full"

    @property
    def blocked(self) -> bool:
        return self.action in (BLOCKED_FULL, BLOCKED_STABILITY)

    def to_dict(self) -> dict:
        return {
            "action": self.action,
            "mode": self.mode,
            "should_run": self.should_run,
            "is_full": self.is_full,
            "blocked": self.blocked,
            "reason": self.reason,
            "signals": list(self.signals),
            "scan_command": self.scan_command,
            "scan_targets": self.scan_targets,
            "source": self.source,
            "tags": list(self.tags),
            "major_version": self.major_version,
            "guidance": self.guidance(),
        }

    def guidance(self) -> str:
        """一句话处置（给 agent 直接照做，不需要再解读）。"""
        if self.is_full:
            return "大版本：允许全量构建 + 全量测试"
        if self.action == BLOCKED_FULL:
            return (f"**不跑全量**：{self.reason}；"
                    f"改跑增量 → `{self.scan_command}`（{self.scan_targets}）")
        if self.action == BLOCKED_STABILITY:
            return "**不连跑**：跑法固定，重复跑不产生判别力"
        return "日常：只跑变更相关增量（不跑全量回归）"


def _norm_tags(tags: Any) -> List[str]:
    """标签归一化（与 `review.scope` 同一口径，避免两处各写一套）。"""
    if tags is None:
        return []
    if isinstance(tags, str):
        raw: Any = tags.replace("，", ",").split(",")
    elif isinstance(tags, (list, tuple, set)):
        raw = tags
    else:
        return []
    out: List[str] = []
    for t in raw:
        if not isinstance(t, str):
            continue
        t = t.strip().lower()
        if t:
            out.append(t)
    return out


def decide_full(
    rules: Optional[CollabRules] = None,
    *,
    major_version: Optional[bool] = None,
    tags: Any = None,
    source: str = SOURCE_AGENT_CLAIM,
    impact: Any = "",
    command: str = "",
    event: Any = "",
    tests_dir: str = "tests/",
) -> FullGateDecision:
    """**能不能跑全量**：只有大版本（且来源可采信）才放行，其余一律拦截。

    Args:
        rules: 协同规则包（大版本标签口径 = 唯一语义源）
        major_version: **可判定事实**——是否大版本。
            ``True`` = 是；``False`` = 明确不是；
            ``None`` = **不知道**（默认）。
            ⚠️ 绝不能把 agent 的自述当 `True`（自述走 `source="agent_claim"`）。
        tags: 版本标签（可判定事实）
        source: 判定来源（`action` / `tag` / `operator` / `agent_claim`）
        impact: 受影响面（用于给出**可直接跑**的增量口径）
        command: 原命令（用于把 `pytest -q` 换成增量命令）
        event: 触发事件（评论/目录/定时类事件**不得**跑全量——防跨仓污染）。
            留空 = **不可判定** → 保守拦截（拿不准就不全量）。
        tests_dir: 增量退化目录（解析不出具体变更测试时用）

    Returns:
        `FullGateDecision`。**判不出（`major_version=None` 且无大版本标签）
        一律拦截**——与"跳过重复"的保守方向相反，这里取安全的一侧：
        宁可少跑全量，不可空烧全量构建/测试。
    """
    r = rules if rules is not None else CollabRules.defaults()
    tag_list = _norm_tags(tags)
    src = source if isinstance(source, str) and source else SOURCE_AGENT_CLAIM
    signals: List[str] = [f"source:{src}"]

    # 来源不可采信（agent 自述）→ 直接拦，连"是不是大版本"都不用看：
    # 否则等于"谁喊一声大版本就能解锁全量"，门禁形同虚设。
    credible = src in CREDIBLE_SOURCES
    if not credible:
        signals.append("agent_claim:rejected")
        return _blocked_full(
            r, signals, tag_list, major_version, impact, command, tests_dir,
            reason=("全量是**大版本专用**，且必须由可采信来源（大版本 action / "
                    "大版本标签 / 人工指令）判定；agent 自述不构成凭据"),
            source=src)

    # 事件侧：不含有测试阶段的事件（评论/目录/定时/触发）**不得**跑全量。
    # 这是"一次污染很多仓库"的直接堵口——这类事件没有基线、也没有变更集。
    ev = full_run_eligible(event, blocked_events=r.blocked_events)
    if ev["blocked"]:
        signals.append(f"event:{ev.get('event', '')}")
        return _blocked_full(
            r, signals, tag_list, major_version, impact, command, tests_dir,
            reason=f"{ev['reason']} → 不得跑全量",
            source=src)

    if major_version is False:
        signals.append("major_version=False")
        return _blocked_full(
            r, signals, tag_list, major_version, impact, command, tests_dir,
            reason="明确判定为**非大版本** → 不跑全量",
            source=src)

    hit = next((t for t in tag_list if t in
                [x.lower() for x in r.major_version_tags]), "")
    if hit:
        signals.append(f"tag:{hit}")
        return FullGateDecision(
            action=RUN, mode="full", source=src, tags=tag_list,
            major_version=major_version,
            reason=f"大版本标签 `{hit}` → 允许全量（大版本专用）",
            signals=signals)

    if major_version is True:
        signals.append("major_version=True")
        return FullGateDecision(
            action=RUN, mode="full", source=src, tags=tag_list,
            major_version=major_version,
            reason="大版本（可判定事实）→ 允许全量",
            signals=signals)

    # 判不出 → **保守拦截**（与 skip 的保守方向相反）
    signals.append("undecidable")
    return _blocked_full(
        r, signals, tag_list, major_version, impact, command, tests_dir,
        reason=("判不出是不是大版本（无大版本标签、无大版本 action）→ "
                "**按非大版本处理，不跑全量**"),
        source=src)


def full_run_eligible(
    event: Any,
    blocked_events: Any = None,
) -> Dict[str, Any]:
    """**这个事件下能不能跑全量**——薄封装（实现见 `platform.full_run_eligible`）。

    放在本模块作**本地副本**：`platform` 需要 `full_gate` 的判定、`full_gate`
    需要 `platform` 的事件口径，互相导入会成环；故此处延迟转发，语义**只有一份**。
    """
    from .platform import full_run_eligible as _impl
    return _impl(event, blocked_events=blocked_events)


def _blocked_full(rules: CollabRules, signals: List[str], tags: List[str],
                  major_version: Optional[bool], impact: Any, command: str,
                  tests_dir: str, reason: str,
                  source: str) -> FullGateDecision:
    """构造"拦截全量 + 给出改跑增量口径"的判定（不能只说"不许"）。"""
    targets, cmd = incremental_command(impact, command, tests_dir=tests_dir)
    return FullGateDecision(
        action=BLOCKED_FULL, mode="scan", reason=reason, signals=signals,
        scan_targets=targets, scan_command=cmd, source=source, tags=tags,
        major_version=major_version)


def incremental_command(impact: Any, command: str = "",
                        tests_dir: str = "tests/") -> tuple:
    """把"想跑的全量命令"改写成**可直接跑**的增量命令。

    Returns:
        ``(targets, command)``。受影响面为空 → 退化到 `tests/`
        （仍是**变更相关**口径，不是 `pytest -q` 全量回归）。

    这样拦截之后 agent 不需要"自己想个增量跑法"——照抄这一行即可，
    避免"拦了但没给路"导致下一步又绕回全量。
    """
    canonical = _norm_impact(impact)
    targets = canonical.replace("|", " ") if canonical else tests_dir
    base = command.strip() if isinstance(command, str) else ""
    if not base:
        base = "pytest -q"
    if "pytest" in base:
        cmd = f"pytest -q {targets}".strip()
    else:
        cmd = base
    return targets, cmd


@dataclass
class RepeatDecision:
    """重复执行判定的**结论部分**（口径由 `RunScopeGuard.decide` 赋）。"""

    action: str = RUN
    reason: str = ""
    key: str = ""
    prior_count: int = 0
    max_identical_repeats: int = 1
    alternatives: List[str] = field(default_factory=list)
    degraded: bool = False
    degrade_reason: str = ""
    #: 本次判定用的去升级口（`revision` / `content`）——留痕"为什么这次算同一口径"
    scope: str = SCOPE_REVISION
    #: 同一受审内容下的**历史 revision 数**（>1 即说明"改一行代码就重跑一遍"）
    content_revisions: int = 0

    def to_dict(self) -> dict:
        return {
            "action": self.action,
            "reason": self.reason,
            "key": self.key,
            "prior_count": self.prior_count,
            "max_identical_repeats": self.max_identical_repeats,
            "alternatives": list(self.alternatives),
            "degraded": self.degraded,
            "degrade_reason": self.degrade_reason,
            "scope": self.scope,
            "content_revisions": self.content_revisions,
        }


def repeat_scope_of(rules: Optional[CollabRules] = None) -> str:
    """当前生效的去升级口（`content` / `revision`）。"""
    r = rules if rules is not None else CollabRules.defaults()
    scope = getattr(r, "repeat_scope", DEFAULT_REPEAT_SCOPE)
    return scope if scope in (SCOPE_REVISION, SCOPE_CONTENT) else \
        DEFAULT_REPEAT_SCOPE


def content_key_of(mode: str, impact: Any, command: str) -> str:
    """**受审内容指纹**：`(范围, 受影响面, 命令)` —— **不含 revision**。

    这是给"重复决策去升级"用的键：同一份变更集在新提交下重跑，
    按内容看就是**同一件事**（用户："同一个口径几乎不会产生分歧"）。
    """
    import hashlib
    parts = {
        "mode": mode or "scan",
        "impact": _norm_impact(impact),
        "command": command.strip() if isinstance(command, str) else "",
    }
    blob = "\x1f".join(f"{k}={parts[k]}" for k in ("mode", "impact", "command"))
    return hashlib.sha1(blob.encode("utf-8")).hexdigest()[:16]


def verdict_is_red(verdict: Any) -> bool:
    """结论是否**明确红**（复用 `run_guard` 的判据，不另立一套）。"""
    return _is_red(verdict)


def norm_revision(rev: Any) -> str:
    """版本归一化（复用 `run_guard`，避免两处口径漂移）。"""
    return _norm_revision(rev)
