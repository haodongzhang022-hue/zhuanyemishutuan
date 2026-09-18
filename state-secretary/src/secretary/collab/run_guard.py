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
import threading
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from .rules import CollabRules

RUN = "run"                          # 放行执行
SKIP_DUPLICATE = "skip_duplicate"    # 同口径已跑过 → 跳过（不消耗构建/测试）
BLOCKED_STABILITY = "blocked_stability"   # 想"连跑 N 次"当稳定性论证 → 拒绝并给替代

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


def _is_red(verdict: Any) -> bool:
    """是否**明确**为不绿（红）。

    与 ``_is_green`` 的区别很关键：``False``/空串/未知字符串**都不是**红——
    因为"没给结论"不等于"跑红了"。把两者混同会让"允许重跑"的例外变成默认路径。
    """
    if isinstance(verdict, bool):
        return verdict is False
    if not isinstance(verdict, str):
        return False
    v = verdict.strip().lower()
    if not v:
        return False
    return any(r in v for r in _RED)


def _is_green(verdict: Any) -> bool:
    if isinstance(verdict, bool):
        return verdict
    if not isinstance(verdict, str):
        return False
    v = verdict.strip().lower()
    return any(g in v for g in _GREEN)


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
               last_verdict: Any = "", window_repeats: int = 1) -> RunDecision:
        """判定本次是否该跑。

        Args:
            mode: 范围档位（``scan`` / ``full``）；空 → 规则包默认档位
            revision: 版本（commit/PR head）
            impact: 受影响面（列表或字符串）
            command: 实际命令（构建/测试）
            last_verdict: 上一次同口径的结论（用于"上一轮不绿可重跑"例外）
            window_repeats: 想连跑几次（> 上界 → 稳定性滥用）
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
        # ② 四元组任一缺失 → 判不出"是不是同一口径" → 不拦（保守）
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
        recs.sort(key=lambda r: (-r["count"], r["key"]))
        over = [r for r in recs if r["count"] > self.rules.max_identical_repeats]
        return {
            "max_identical_repeats": self.rules.max_identical_repeats,
            "allow_stability_repeats": self.rules.allow_stability_repeats,
            "total": len(recs),
            "over_budget": over,
            "records": recs,
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
        with self._lock:
            for item in items:
                rec = RunRecord.from_dict(item)
                if rec is not None:
                    self._records[rec.key] = rec

    def _persist(self) -> None:
        path = self._path()
        if not path:
            return
        try:
            import json
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with self._lock:
                payload = {"records": [r.to_dict() for r in self._records.values()]}
            tmp = path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False, indent=2)
            os.replace(tmp, path)
        except OSError:
            return
