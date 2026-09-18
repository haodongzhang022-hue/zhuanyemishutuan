"""同口径重复执行守卫（RunScopeGuard）：**让"只跑增量、不连跑"成为机制，而非提醒**

## 需求（Issue #14 本次追问，用户口径）

> 平时不需要跑全量，而且尤其是不适合连跑三次，这也没意义，我们平时只需要跑增量的部分…
> 连续跑三次，有问题也可能查不出来，因为跑的方式是固定的。同一个口径几乎不会产生分歧。
> 所以像这种连续跑三次全量是无效的。

拆成两条**可执行**口径：

1. **默认增量**：日常一律 ``scan``（只审变更集 + 受影响面），``full`` 只在大版本；
2. **同口径不重复**：``(范围, 版本, 受影响面, 命令)`` 四元组相同 → 第 2 次起
   直接判**重复**、跳过执行。理由正是用户说的：跑法固定 → 同一口径几乎不出分歧 →
   重复跑只是重复烧构建/测试，**不增加判别力**。

## 为什么要有"守卫"而不只是"规则"

既有规则包（``shared/collab/collab-rules.yaml``）里已经写了"日常只审变化"，
但那是**口径**（说给人/agent 听），不是**机制**（拦得住的手）。
只要某一轮没人"记得"去看档位，全量 + 连跑就会照旧发生。
故本模块把它变成一道**判定**：``RunScopeGuard.decide()`` 返回
``run`` / ``skip_duplicate`` / ``blocked_stability`` —— 跳过不需要"自觉"。

## 允许的例外（不是"一刀切禁重跑"）

- **上一轮不绿**（``last_verdict`` 非绿）+ ``repeat_rerun_on_failure`` →
  允许同口径重跑：红→修→再跑是**有新信息**的（修完该绿），与"连跑三次全绿"不同；
- **稳定性论证要换口径**：跑一次增量后，对可疑项做**定点探针 / 变异探针 /
  独立场景**（``stability_via``），而不是把同一命令再跑两遍。
  ``blocked_stability`` 正是对"想连跑 N 次同一命令"的显式拒绝，并给出替代路径。

## 保守性（判不出就不拦）

- 四元组任一字段缺失/非法 → 视为**新口径**（``run``），不误拦；
- 历史记录损坏/不可读 → 视为**空历史**（``run``），不因台账坏了挡住干活。
"""
from __future__ import annotations

import hashlib
import os
import re
import threading
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

# `full_gate` 需要复用本模块的归一化/判红工具（单一语义源），
# 故此处**延迟导入**避免成环（与 `platform`/`probe` 的既有处理一致）。
from .platform import impact_available
from .rules import CollabRules
from ..trail import attributed, collect_marked

RUN = "run"                          # 放行执行
SKIP_DUPLICATE = "skip_duplicate"    # 同口径已跑过 → 跳过（不消耗构建/测试）
BLOCKED_STABILITY = "blocked_stability"   # 想"连跑 N 次"当稳定性论证 → 拒绝并给替代

# 判重台账的**可持久留痕**标记（🔴F：与 🔴A 用同一载体方案）。
# 平台每次 push = **新容器、新工作区** → `<tmp>/run-scope` 每次都从空开始，
# 判重**永不触发**（实测 r1/r2/r3 全 run）。故台账必须随**可持久载体**走：
# PR/Issue 评论正文（容器会被回收，评论不会）。
RUN_SCOPE_TRAIL_MARKER = "run-scope"

# 结论"绿"的判定：只有明确绿/通过才算绿；其余（含空/未知）一律按"不绿"
_GREEN = ("green", "pass", "passed", "ok", "success", "succeeded", "🟢")


def _norm_revision(rev: Any) -> str:
    """版本归一化：字符串去空白；其余类型判不出 → 空串（→ 视为新口径）。"""
    if not isinstance(rev, str):
        return ""
    return rev.strip()


def _norm_impact(impact: Any) -> str:
    """受影响面归一化：可迭代 → 排序去重后拼接；字符串 → 去空白。

    排序是刻意的：**集合表示**的受影响面，其列举顺序不应改变"是不是同一口径"。
    """
    if impact is None:
        return ""
    if isinstance(impact, str):
        return impact.strip()
    if isinstance(impact, (list, tuple, set, frozenset)):
        items = sorted({str(x).strip() for x in impact if str(x).strip()})
        return "|".join(items)
    return ""


# 结论"红"的判定：**只认已知的非绿结论**；空/未知**不算红**（否则等于默认放行）
_RED = ("red", "fail", "failed", "failure", "error", "🔴", "不通过", "失败")

# 「**零个**失败/错误」的正则（🟡2 复评第 5 轮）。
#
# 旧实现是**子串**匹配：`"577 passed, 0 failed"` 里含 `"failed"` → 判红。
# 而"577 passed, 0 failed"恰恰是**最典型的绿**（pytest/测试报告常见写法）——
# 于是"上一轮不绿才允许重跑一次"这条**例外被恒真激活**：判重形同虚设
# （叠加 🔴F 的"CI 不落 verdict"后，任何一次同口径重跑都会被放行）。
#
# 口径：`0 failed` / `0 errors` / `0 failures` 里的 `failed` 是**计数词**，
# 不是结论。故匹配前先把"零个 X"整段抹掉；只抹 `0`（含 `0.0`/`0%` 这类写法）。
_ZERO_ISSUE_RE = re.compile(
    r'\b0(?:\.0+)?\s*%?\s*'
    r'(?:failed|failures?|failing|errors?|errored|broken|缺陷|失败|错误)\b',
    re.I)


def _is_red(verdict: Any) -> bool:
    """是否**明确**为不绿（红）。

    与 ``_is_green`` 的区别很关键：``False``/空串/未知字符串**都不是**红——
    因为"没给结论"不等于"跑红了"。把两者混同会让"允许重跑"的例外变成默认路径。

    **"零个失败"不算红**（🟡2）：`"577 passed, 0 failed"` 是绿，不是红。
    判据先把 `0 failed` / `0 errors` 这类**计数词**抹掉再匹配关键词，
    否则"允许重跑一次"的例外会被恒真激活（等于默认放行）。
    """
    if isinstance(verdict, bool):
        return verdict is False
    if not isinstance(verdict, str):
        return False
    v = verdict.strip().lower()
    if not v:
        return False
    v = _ZERO_ISSUE_RE.sub(" ", v)
    if not v.strip():
        return False
    return any(r in v for r in _RED)


def _is_green(verdict: Any) -> bool:
    if isinstance(verdict, bool):
        return verdict
    if not isinstance(verdict, str):
        return False
    v = verdict.strip().lower()
    return any(g in v for g in _GREEN)


def _trail_of(payload: Dict[str, Any]) -> str:
    """把一份判重台账记录渲染成**可持久留痕**（评论正文里的一行）。"""
    import json
    return ("<!-- %s: %s -->"
            % (RUN_SCOPE_TRAIL_MARKER,
               json.dumps(payload, ensure_ascii=False, sort_keys=True)))


def _repeat_scope_of(rules: "CollabRules") -> str:
    """当前生效的**去升级口**（`content` / `revision`）；导入延迟以避成环。"""
    from .full_gate import repeat_scope_of
    return repeat_scope_of(rules)


@dataclass
class RunRecord:
    """一次执行的留痕（用于后续判重）。"""

    key: str = ""
    mode: str = "scan"
    revision: str = ""
    impact: str = ""
    command: str = ""
    verdict: str = ""
    count: int = 1

    def to_dict(self) -> dict:
        return {
            "key": self.key, "mode": self.mode, "revision": self.revision,
            "impact": self.impact, "command": self.command,
            "verdict": self.verdict, "count": self.count,
        }

    @classmethod
    def from_dict(cls, data: Any) -> Optional["RunRecord"]:
        if not isinstance(data, dict):
            return None
        key = data.get("key")
        if not isinstance(key, str) or not key:
            return None

        def _s(k: str) -> str:
            v = data.get(k)
            return v if isinstance(v, str) else ""

        try:
            count = int(data.get("count", 1))
        except (TypeError, ValueError):
            count = 1
        return cls(key=key, mode=_s("mode"), revision=_s("revision"),
                   impact=_s("impact"), command=_s("command"),
                   verdict=_s("verdict"), count=max(1, count))


@dataclass
class RunDecision:
    """一次执行请求的判定结果。"""

    action: str = RUN
    reason: str = ""
    key: str = ""
    prior_count: int = 0
    max_identical_repeats: int = 1
    alternatives: List[str] = field(default_factory=list)
    # 能力降级留痕（Issue #14 本轮）：判不出"能不能增量"时**保守放行**并说明原因，
    # 不静默跳过（漏跑比多跑危险）。
    degraded: bool = False
    degrade_reason: str = ""
    # 去升级口径留痕（Issue #14 本轮追问）：`content` / `revision`，
    # 以及"同一受审内容下出现过的历史 revision 数"（>1 = 改一行就重跑过一次）。
    repeat_scope: str = "revision"
    content_revisions: int = 0

    @property
    def should_run(self) -> bool:
        return self.action == RUN

    @property
    def skipped(self) -> bool:
        return self.action == SKIP_DUPLICATE

    def to_dict(self) -> dict:
        return {
            "action": self.action,
            "should_run": self.should_run,
            "skipped": self.skipped,
            "reason": self.reason,
            "key": self.key,
            "prior_count": self.prior_count,
            "max_identical_repeats": self.max_identical_repeats,
            "alternatives": list(self.alternatives),
            "degraded": self.degraded,
            "degrade_reason": self.degrade_reason,
            "repeat_scope": self.repeat_scope,
            "content_revisions": self.content_revisions,
        }


class RunScopeGuard:
    """同口径重复执行守卫（判定 + 轻量留痕）。

    Args:
        rules: 协同规则包（读 ``max_identical_repeats`` 等）
        data_dir: 留痕目录（缺省时不落盘，仅进程内判重）
        window_repeats: 调用方声明"想连跑几次"（> 上界即判稳定性滥用）
    """

    def __init__(self, rules: Optional[CollabRules] = None,
                 data_dir: Optional[str] = None) -> None:
        self.rules = rules if rules is not None else CollabRules.defaults()
        self.data_dir = data_dir
        self._lock = threading.Lock()
        self._records: Dict[str, RunRecord] = {}
        # 受审内容台账（Issue #14 本轮）：**跨 revision** 统计"同一份变更集跑了几遍"。
        # 与 `_records` 并存是刻意的——`_records` 回答"这个版本跑过几次"，
        # `_content_records` 回答"这份**内容**被跑了几遍"（后者才是"每开发一步
        # 重跑一遍"的真面目）。
        self._content_records: Dict[str, RunRecord] = {}
        self._content_revisions: Dict[str, set] = {}
        self._load()

    # ------------------------------------------------------------------
    # 判定
    # ------------------------------------------------------------------

    def key_of(self, mode: str, revision: Any, impact: Any,
               command: str) -> str:
        """计算口径指纹：按 ``repeat_keys`` 选字段拼串后取 sha1。

        **只取口径相关的字段**（范围/版本/受影响面/命令）——其余一律不进指纹，
        否则"时间戳/执行人"变了就会被当成新口径，判重形同虚设。
        """
        parts = {
            "mode": (mode or self.rules.scan_default_mode),
            "revision": _norm_revision(revision),
            "impact": _norm_impact(impact),
            "command": command.strip() if isinstance(command, str) else "",
        }
        keys = [k for k in self.rules.repeat_keys if k in parts] or list(parts)
        blob = "\x1f".join(f"{k}={parts[k]}" for k in keys)
        return hashlib.sha1(blob.encode("utf-8")).hexdigest()[:16]

    def decide(self, mode: str = "", revision: Any = "",
               impact: Any = "", command: str = "",
               last_verdict: Any = "", window_repeats: int = 1,
               base: Optional[str] = None, event: str = "") -> RunDecision:
        """判定本次是否该跑。

        Args:
            mode: 范围档位（``scan`` / ``full``）；空 → 规则包默认档位
            revision: 版本（commit/PR head）
            impact: 受影响面（列表或字符串）
            command: 实际命令（构建/测试）
            last_verdict: 上一次同口径的结论（用于"上一轮不绿可重跑"例外）
            window_repeats: 想连跑几次（> 上界 → 稳定性滥用）
            base: 基线版本（判"能否算受影响面"用）；``None`` = 未提供
            event: 触发事件名（判"能否算受影响面"用，如 `branch.create`）
        """
        rules = self.rules
        eff_mode = mode if mode in ("scan", "full") else rules.scan_default_mode
        cmd = command.strip() if isinstance(command, str) else ""

        # ① 稳定性滥用：显式想"连跑 N 次同一命令"——直接拒，给替代路径
        try:
            wr = int(window_repeats)
        except (TypeError, ValueError):
            wr = 1
        # 判据只看"想连跑同一口径"本身，**不因上轮不绿而放行**——
        # "上一轮不绿"是"允许重跑一次同口径"的例外（见 ③），不是"允许连跑 3 次"的理由。
        #
        # 本轮加固（Issue #14）：这条判定**不受任何口径字段影响**——
        # 它压根不是一个"可以靠换口径绕过"的东西，所以放在最前面、只做**参数合法性**
        # 的宽松处理（非法值按 1 处理，不因参数写错就拦），其余一律以 `wr` 为准。
        if wr > 1 and not rules.allow_stability_repeats:
            return RunDecision(
                action=BLOCKED_STABILITY,
                reason=(f"同一口径想连跑 {wr} 次：跑法固定，重复跑不产生判别力"
                        f"（不新增信息，只重复消耗构建/测试）"),
                key=self.key_of(eff_mode, revision, impact, cmd),
                prior_count=0,
                max_identical_repeats=rules.max_identical_repeats,
                alternatives=list(rules.stability_via),
            )

        key = self.key_of(eff_mode, revision, impact, cmd)
        # ② 口径信息不全 / 能力缺失 → 判不出"是不是同一口径" → **保守放行**。
        #
        # 能力探测（Issue #14 本轮，官方 SCA 增量规则同源）：判重的可信前提是
        # **能算受影响面**。算不出（新建分支 push 无基准 / 受影响面为空）时，
        # 空 impact 会被归一化成同一个合法指纹 → 不同变更集可能被误判成"同一口径"
        # → **该跑的被跳过**（漏跑，方向危险）。故此处取安全一侧：宁可多跑，不可漏跑。
        cap = impact_available(revision, base=base, impact=impact, event=event)
        if not cap["available"] and cap["degraded"]:
            return RunDecision(
                action=RUN,
                reason=f"降级放行（{cap['action']}）：{cap['reason']}",
                key=key, prior_count=0,
                max_identical_repeats=rules.max_identical_repeats,
                degraded=True, degrade_reason=cap["reason"],
            )
        if not _norm_revision(revision) or not cmd:
            return RunDecision(
                action=RUN,
                reason="口径信息不全（缺版本或命令）→ 不判重，直接放行",
                key=key, prior_count=0,
                max_identical_repeats=rules.max_identical_repeats,
            )

        with self._lock:
            prior = self._records.get(key)
        prior_count = prior.count if prior else 0

        if prior_count < rules.max_identical_repeats:
            return RunDecision(
                action=RUN,
                reason=f"同口径已跑 {prior_count}/{rules.max_identical_repeats} 次 → 放行",
                key=key, prior_count=prior_count,
                max_identical_repeats=rules.max_identical_repeats,
            )

        # ③ 已达上界：仅"上一轮**明确**不绿"才允许重跑（红→修→再跑是有信息的）。
        #
        # 判据是 `_is_red(last_verdict)` 而**不是** `not _is_green(...)`——
        # 后者把"没给结论/未知"也当成红，等于**默认放行**：台账里只要没有结论，
        # 同一口径就能无限重跑，判重形同虚设（"规则看似有、实际拦不住"）。
        # 故：只有**带过已知的非绿结论**（failed/红/false…）才放行一次重跑；
        # 未知一律按"已达上界"处理 → 跳过。
        if rules.repeat_rerun_on_failure and _is_red(last_verdict):
            return RunDecision(
                action=RUN,
                reason="同口径已达上界，但上一轮**不绿**且未给绿结论 → 允许重跑",
                key=key, prior_count=prior_count,
                max_identical_repeats=rules.max_identical_repeats,
            )

        return RunDecision(
            action=SKIP_DUPLICATE,
            reason=(f"同口径（范围 {eff_mode} / 版本 / 受影响面 / 命令）已跑 "
                    f"{prior_count} 次达上界，且上轮为绿 → 跳过"
                    f"（同一口径不产生分歧，重复跑无效）"),
            key=key, prior_count=prior_count,
            max_identical_repeats=rules.max_identical_repeats,
            alternatives=list(rules.stability_via),
        )

    # ------------------------------------------------------------------
    # 重复执行（**去升级**口径：Issue #14 本轮）
    # ------------------------------------------------------------------

    def decide_repeat(self, mode: str = "", revision: Any = "",
                      impact: Any = "", command: str = "",
                      last_verdict: Any = "", window_repeats: int = 1,
                      base: Optional[str] = None, event: str = "") -> RunDecision:
        """**该不该跑**——按"受审内容"而非"每次 revision"判重（去升级）。

        与 `decide()` 的差别只有一处，但正是用户看到的现象的根因：

        | 口径 | 比较键 | 后果 |
        |---|---|---|
        | `decide()`（旧） | 范围 + **版本** + 受影响面 + 命令 | 每改一行 → revision 变 → 每次都算新口径 → **每开发一步跑一次** |
        | `decide_repeat()`（新） | 范围 + 受影响面 + 命令 + **受审内容** | 同一份变更集在新提交下重跑 → 判为重复 → **跳过** |

        去升级由规则包 `scan.repeat_scope` 控制（默认 `content`；设成 `revision`
        即退回旧行为——保留这条是因为**方向性**：若某仓的业务确实"每个提交都必须跑一次"，
        它可以显式选择，但**默认不这样**）。
        """
        from .full_gate import content_key_of, repeat_scope_of
        eff_mode = mode if mode in ("scan", "full") else self.rules.scan_default_mode
        ckey = content_key_of(eff_mode, impact, command)
        with self._lock:
            revisions = sorted(self._content_revisions.get(ckey, set()))
            crec = self._content_records.get(ckey)
        # 🔴F：**上一次该内容的真实结论**优先作为例外依据。跨容器时调用方
        # （CI）拿不到上一次结论、传参恒为空 → "上一轮不绿才允许复跑"的例外
        # 永远不成立，修复后的代码得不到验证。故这里从台账（可由留痕恢复）
        # 取真实结论补上，再交给 `decide()` 统一判定。
        if crec is not None and crec.verdict and not _norm_revision(last_verdict):
            last_verdict = crec.verdict
        decision = self.decide(
            mode=mode, revision=revision, impact=impact, command=command,
            last_verdict=last_verdict, window_repeats=window_repeats,
            base=base, event=event)
        scope = repeat_scope_of(self.rules)
        decision.repeat_scope = scope
        if scope != "content":
            return decision
        decision.content_revisions = len(revisions)
        if decision.action != RUN:
            return decision
        # 同一份受审内容**已跑过**且这次换了个 revision → 正是"改一行重跑一遍"
        if crec is not None and _norm_revision(revision) != crec.revision:
            # **红后可复跑**（🔴F 复评第 5 轮）：内容键只由"受影响面 + 命令"构成，
            # 而**修 bug 常常改的就是同一批文件**（受影响面不变）→ 若一律跳过，
            # "红 → 修 → 再跑"这条唯一有信息量的路径就被自己的判重堵死了。
            # 故：上一次该内容**明确不绿**时允许复跑（这正是
            # `repeat_rerun_on_failure` 的语义，与 `decide()` 的同口径例外一致）。
            # 结论取**台账里的真实结论**（CI 落盘的那条），而不是只看调用方传参——
            # 跨容器时调用方拿不到上一次结论，传参恒为空 → 例外恒不生效。
            last = last_verdict if _norm_revision(last_verdict) else ""
            prior_verdict = last or (crec.verdict if crec else "")
            if self.rules.repeat_rerun_on_failure and _is_red(prior_verdict):
                return RunDecision(
                    action=RUN,
                    reason=(f"受审内容未变但版本变了（{crec.revision} → "
                            f"{_norm_revision(revision)}），且上一次该内容**不绿**"
                            f"（{prior_verdict!r}）→ 允许复跑（红→修→再跑有新信息）"),
                    key=decision.key, prior_count=crec.count,
                    max_identical_repeats=1,
                    repeat_scope=scope,
                    content_revisions=max(1, len(revisions)),
                )
            return RunDecision(
                action=SKIP_DUPLICATE,
                reason=(f"受审内容未变（受影响面/命令一致）但版本变了（{crec.revision} → "
                        f"{_norm_revision(revision)}）：跑法固定，新版本不构成新口径"
                        f"（同一受审内容只跑一次；上次结论为绿）"),
                key=decision.key, prior_count=crec.count + 1,
                max_identical_repeats=1,
                alternatives=list(self.rules.stability_via),
                repeat_scope=scope, content_revisions=max(1, len(revisions)),
            )
        return decision

    def record_repeat(self, mode: str = "", revision: Any = "",
                      impact: Any = "", command: str = "",
                      verdict: str = "") -> RunRecord:
        """登记一次**实际执行**（同时更新"受审内容 → 历史 revision 数"台账）。"""
        from .full_gate import content_key_of
        rec = self.record(mode=mode, revision=revision, impact=impact,
                          command=command, verdict=verdict)
        eff_mode = mode if mode in ("scan", "full") else self.rules.scan_default_mode
        ckey = content_key_of(eff_mode, impact, command)
        rev = _norm_revision(revision)
        with self._lock:
            self._content_revisions.setdefault(ckey, set())
            if rev:
                self._content_revisions[ckey].add(rev)
            prior = self._content_records.get(ckey)
            self._content_records[ckey] = RunRecord(
                key=ckey, mode=eff_mode, revision=rev,
                impact=rec.impact, command=rec.command, verdict=rec.verdict,
                count=(prior.count + 1) if prior else 1)
        self._persist()
        return rec

    def content_repeats_of(self, mode: str = "", impact: Any = "",
                           command: str = "") -> int:
        """同一份**受审内容**已跑了几遍（跨 revision 累计）。"""
        from .full_gate import content_key_of
        eff_mode = mode if mode in ("scan", "full") else self.rules.scan_default_mode
        ckey = content_key_of(eff_mode, impact, command)
        with self._lock:
            rec = self._content_records.get(ckey)
            return rec.count if rec else 0

    # ------------------------------------------------------------------
    # 留痕（🔴F：**可持久载体** —— 评论正文；与 🔴A 同一方案）
    # ------------------------------------------------------------------

    def trail(self, mode: str = "", impact: Any = "", command: str = "",
              session_id: str = "") -> str:
        """某份**受审内容**的留痕（`<!-- run-scope: {…} -->`），可贴进评论正文。

        为什么必须有这一步（🔴F）：平台每次 push = **新容器、新工作区**，
        `<tmp>/run-scope/run-scope.json` 每次都从空开始 → 判重永不触发
        （实测 r1/r2/r3 全 `run`）。容器会被回收，**评论不会** —— 故台账必须
        随评论走：接收方把本留痕写回 PR/Issue 评论，下一棒（新容器）
        `adopt_trails(该评论正文)` 即恢复"这份内容跑过几遍、上轮结论是什么"。
        """
        from .full_gate import content_key_of
        eff_mode = mode if mode in ("scan", "full") else self.rules.scan_default_mode
        ckey = content_key_of(eff_mode, impact, command)
        with self._lock:
            rec = self._content_records.get(ckey)
            revs = sorted(self._content_revisions.get(ckey, set()))
        if rec is None:
            return ""
        return _trail_of({
            "session_id": session_id or "",
            "mode": eff_mode, "impact": rec.impact, "command": rec.command,
            "revision": rec.revision, "count": rec.count,
            "verdict": rec.verdict, "revisions": revs,
        })

    def trails(self, session_id: str = "") -> str:
        """**全部**受审内容的留痕（多行；直接追加写回评论即完成跨容器继承）。"""
        out: List[str] = []
        with self._lock:
            items = list(self._content_records.values())
            revs = {k: sorted(v) for k, v in self._content_revisions.items()}
        for rec in sorted(items, key=lambda r: r.key):
            out.append(_trail_of({
                "session_id": session_id or "",
                "mode": rec.mode, "impact": rec.impact, "command": rec.command,
                "revision": rec.revision, "count": rec.count,
                "verdict": rec.verdict, "revisions": revs.get(rec.key, []),
            }))
        return "\n".join(out)

    def adopt_trails(self, payload: Any, session_id: str = "") -> dict:
        """从**可持久载体**（评论正文 / 交接卡 context）恢复判重台账。

        **只增不减**（`max()` 合并），故历史留痕叠加不影响正确性；
        **按 `session_id` 归属过滤**（留痕不带该字段时不表态 = 保留）——
        同一条评论可能挂着多个会话的留痕，无脑合并会让别的会话"替"本会话
        判定（🟡1 同族）。

        Returns:
            ``{"merged": int, "records": int, "reason": str}``
        """
        if isinstance(payload, dict):
            recs = [payload]
        elif isinstance(payload, str) and payload.strip():
            recs = collect_marked(payload, RUN_SCOPE_TRAIL_MARKER)
        else:
            recs = []
        from .full_gate import content_key_of
        merged = 0
        with self._lock:
            for rec in recs:
                if not attributed(rec, "session_id", session_id):
                    continue
                mode = rec.get("mode")
                mode = mode if mode in ("scan", "full") else self.rules.scan_default_mode
                impact = _norm_impact(rec.get("impact"))
                cmd = rec.get("command") if isinstance(rec.get("command"), str) else ""
                ckey = content_key_of(mode, impact, cmd)
                try:
                    count = max(1, int(rec.get("count", 1)))
                except (TypeError, ValueError):
                    count = 1
                rev = _norm_revision(rec.get("revision"))
                verdict = rec.get("verdict") if isinstance(rec.get("verdict"), str) else ""
                prior = self._content_records.get(ckey)
                if prior is None or count > prior.count:
                    self._content_records[ckey] = RunRecord(
                        key=ckey, mode=mode, revision=rev, impact=impact,
                        command=cmd, verdict=verdict, count=count)
                    merged += 1
                elif verdict and not prior.verdict:
                    prior.verdict = verdict
                revs = rec.get("revisions")
                if isinstance(revs, list):
                    self._content_revisions.setdefault(ckey, set())
                    for r in revs:
                        if isinstance(r, str) and r.strip():
                            self._content_revisions[ckey].add(r.strip())
                if rev:
                    self._content_revisions.setdefault(ckey, set()).add(rev)
                # 同时恢复"按版本判重"的台账（`decide()` 路径同源）
                if rev:
                    rkey = self.key_of(mode, rev, impact, cmd)
                    prior_r = self._records.get(rkey)
                    if prior_r is None or count > prior_r.count:
                        self._records[rkey] = RunRecord(
                            key=rkey, mode=mode, revision=rev, impact=impact,
                            command=cmd, verdict=verdict, count=count)
        if merged:
            self._persist()
        return {"merged": merged, "records": len(self._content_records),
                "reason": ("从留痕恢复判重台账（跨容器继承；只增不减）"
                           if merged else "留痕无新增（或无法解析）")}

    # ------------------------------------------------------------------
    # 留痕
    # ------------------------------------------------------------------

    def record(self, mode: str = "", revision: Any = "", impact: Any = "",
               command: str = "", verdict: str = "") -> RunRecord:
        """登记一次**实际执行**（只记真的跑了的；被跳过的重复不计数）。"""
        eff_mode = mode if mode in ("scan", "full") else self.rules.scan_default_mode
        cmd = command.strip() if isinstance(command, str) else ""
        key = self.key_of(eff_mode, revision, impact, cmd)
        with self._lock:
            prior = self._records.get(key)
            last = verdict if isinstance(verdict, str) else ""
            if not last and isinstance(verdict, bool):
                last = "green" if verdict else "failed"
            rec = RunRecord(
                key=key, mode=eff_mode, revision=_norm_revision(revision),
                impact=_norm_impact(impact), command=cmd,
                verdict=last,
                count=(prior.count + 1) if prior else 1,
            )
            self._records[key] = rec
        self._persist()
        return rec

    def repeats_of(self, mode: str = "", revision: Any = "",
                   impact: Any = "", command: str = "") -> int:
        """查询某口径已执行次数（供"连跑三次"这类行为被一眼看见）。"""
        eff_mode = mode if mode in ("scan", "full") else self.rules.scan_default_mode
        cmd = command.strip() if isinstance(command, str) else ""
        key = self.key_of(eff_mode, revision, impact, cmd)
        with self._lock:
            rec = self._records.get(key)
            return rec.count if rec else 0

    def overview(self) -> Dict[str, Any]:
        """口径台账总览：哪些口径跑了几次（**重复加倍的会显出来**）。"""
        with self._lock:
            recs = [r.to_dict() for r in self._records.values()]
        with self._lock:
            content = [r.to_dict() for r in self._content_records.values()]
            revs = {k: sorted(v) for k, v in self._content_revisions.items()}
        recs.sort(key=lambda r: (-r["count"], r["key"]))
        over = [r for r in recs if r["count"] > self.rules.max_identical_repeats]
        for row in content:
            row["revisions"] = revs.get(row["key"], [])
            row["revision_count"] = len(row["revisions"])
        content.sort(key=lambda r: (-r["count"], r["key"]))
        return {
            "max_identical_repeats": self.rules.max_identical_repeats,
            "allow_stability_repeats": self.rules.allow_stability_repeats,
            "repeat_scope": _repeat_scope_of(self.rules),
            "total": len(recs),
            "over_budget": over,
            "records": recs,
            # 跨 revision 的"同一受审内容"视图：**改一行重跑一遍**会在这里显形
            "content_records": content,
            "guidance": (
                "同一口径（范围/版本/受影响面/命令）只跑一次；"
                "想论证稳定性请换口径（定点/变异探针、独立场景），不要连跑同一命令"
            ),
        }

    # ------------------------------------------------------------------
    # 持久化（损坏/缺失一律当空历史，**不因台账坏了挡住干活**）
    # ------------------------------------------------------------------

    def _path(self) -> Optional[str]:
        if not self.data_dir:
            return None
        return os.path.join(self.data_dir, "run-scope.json")

    def _load(self) -> None:
        path = self._path()
        if not path or not os.path.isfile(path):
            return
        try:
            import json
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception:
            return
        if not isinstance(data, dict):
            return
        raw = data.get("records")
        items = raw if isinstance(raw, list) else []
        citems = data.get("content_records")
        crevs = data.get("content_revisions")
        with self._lock:
            for item in items:
                rec = RunRecord.from_dict(item)
                if rec is not None:
                    self._records[rec.key] = rec
            for item in (citems if isinstance(citems, list) else []):
                crec = RunRecord.from_dict(item)
                if crec is not None:
                    self._content_records[crec.key] = crec
            if isinstance(crevs, dict):
                for k, v in crevs.items():
                    if isinstance(k, str) and isinstance(v, list):
                        self._content_revisions[k] = {str(x) for x in v}

    def _persist(self) -> None:
        path = self._path()
        if not path:
            return
        try:
            import json
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with self._lock:
                payload = {
                    "records": [r.to_dict() for r in self._records.values()],
                    "content_records": [r.to_dict()
                                        for r in self._content_records.values()],
                    "content_revisions": {k: sorted(v) for k, v
                                          in self._content_revisions.items()},
                }
            tmp = path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False, indent=2)
            os.replace(tmp, path)
        except OSError:
            return
