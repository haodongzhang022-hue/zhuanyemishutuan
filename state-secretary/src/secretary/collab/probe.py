"""检查点探测器（CheckpointProbe）：**事件驱动的探测点**，替代本地轮询

## 需求（Issue #14 追问，用户原话）

> 最多 6 次就行，再多就不适合等待了，不是这里的问题了，或者我们是否可以以
> **非轮询**而是**形成检查点然后 webhook 触发再跑**，**等他的锁**，然后形成一个
> 机制**判断检查点**，类似于**边缘计算的探测器**这样的，会不会好点。怎么处理比较好，
> 平台对这个有最佳实践吗

拆成四条可执行口径：

| # | 口径 | 落地 |
|---|---|---|
| 1 | **最多 6 次** | 探测次数上界 = 等待阶梯档位数（6 档：10s→30s→60s→3min→6min→10min）；用尽即**停止自动挂账** |
| 2 | **非轮询，检查点 + 再触发** | 探测点**不自己等**：要么挂到**平台等待语义**（`cnb:await`，触发方在状态变化时 `resolve`），要么**委托给平台定时任务**（`crontab`）到点代跑；本地**永不 sleep/轮询** |
| 3 | **形成检查点，判断检查点** | 检查点 = `key` + **判别式**（`kind`）。判别只看**可判定事实**（锁在不在 / 版本变没变 / 我方还等不等），**不看 agent 自述**；判不出 → `UNKNOWN`（宽进，不误判） |
| 4 | **边缘计算的探测器** | 采集**轻量本地快照**（锁台账 + 版本台账），产出**结构化事件**（`ProbeEvent`）上报；重活（评审、构建、模型调用）只在"值得唤醒"时才做，符合边缘计算"本地轻量感知、云端重处理"的算力分层 |

## 平台最佳实践（官方文档口径，已查证）

| 能力 | 平台机制 | 文档 |
|---|---|---|
| **一次探测 = 一次事件，零占用** | `cnb:await` / `cnb:resolve`：pipeline 执行到 await 即**挂起**，由配对的 resolve 在**状态变化时**唤醒；await 期间不跑任务 | `/zh/build/internal-steps/cnb/await-resolve.md` |
| **跨流水线/跨仓再触发** | `cnb:apply`（同仓子流水线）/ `cnb:trigger`（跨仓）触发 `api_trigger_*` 事件 | `/zh/build/internal-steps/cnb/apply.md`、`/zh/build/internal-steps/cnb/trigger.md` |
| **外部 webhook / 调度系统驱动** | OPENAPI `StartBuild` + 外部 token 触发 `api_trigger` 流水线（官方推荐给**跨根组织**场景） | `/zh/build/trigger-rule.md`、`/zh/build/crontab.md` |
| **延迟兜底（队头阻塞时）** | `crontab` 定时任务：**最小间隔 5 分钟**，执行身份 = 最后修改该配置的用户；仅支持**明确单一分支** | `/zh/build/crontab.md` |
| **不计等待成本** | 计费只有「CPU 核时」（按任务**实际运行**时长）与「AI Credits」（按模型调用 Token），**没有"等待"项** | `/zh/pricing.md`、`/zh/build/timeout.md` |

平台**最佳实践结论**（本模块据此固化）：

1. **首选「等锁」= `cnb:await` 平台挂起**——锁释放方 `cnb:resolve`，等待方**零占用**唤醒；
   这才是"webhook 触发再跑"在 CNB 上的原生形态（事件驱动、无常驻进程）。
2. **次选「到点再跑」= `crontab` + `api_trigger`**——锁由**仓库外**持有者
   （如本地编辑中的真人）时，平台内没有"锁释放事件"可等，就用**平台定时任务**
   到点代跑一次探测；**平台是那个"不睡觉的进程"**，不是本会话。
3. **严禁本地轮询**：本地 `sleep`/空转轮询才是在烧核时（8 核空转 10 分钟 ≈ 1.33 核时）。

## 与"等待阶梯"的关系

阶梯（`StepBackoff`）只回答**"下一次什么时候看"**；本模块回答**"到点后怎么再看、
以及看什么"**。两者共用同一个次数上界（6 次），到顶即停 → 交回人工（`resume_required`）。

## 用法

    probe = CheckpointProbe("i14", data_dir="<数据根>/probe")
    cp = probe.create(key="card:review-baseline", kind=CHECK_LOCK, holder="开发",
                      await_key="i14-lock-released", max_checks=6)
    plan = probe.delay_plan(cp)              # 六次怎么等（含平台指令，非轮询）
    probe.register_platform_wait(cp, sn="cnb-xxx")   # 已挂到平台等待语义（零占用）
    probe.note_lock(cp, held=True, holder="开发")     # 采集锁快照（轻量）
    ev = probe.judge(cp, our_need_satisfied=False)   # 判断检查点（判定式）
    probe.dispatch(ev)                       # 上报结构化事件（跨 agent 传递）
"""
from __future__ import annotations

import json
import os
import re
import tempfile
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

from .backoff import DEFAULT_MAX_ATTEMPTS, StepBackoff

# 会话/检查点标识白名单（与 LockRegistry / LoopGuard 同族口径，尾锚 \Z）
_SAFE_ID = re.compile(r'^[A-Za-z0-9_.:-]+\Z')

# ---- 探测维度（判别式）：只看"可判定事实"，不看 agent 自述 ----
CHECK_CONFIRM = "confirm"      # 人工/上游确认（人说的话才算数）
CHECK_LOCK = "lock"            # 等锁：锁表视图 / 租约释放
CHECK_VERSION = "version"      # 等版本：版本台账（提交/台账版本）变化
CHECK_EVENT = "event"          # 等事件：外部 webhook 打进来（api_trigger / cnb:resolve）
CHECK_RESOURCE = "resource"    # 等资源：外部资源就绪（设备/算力/制品）

ALL_CHECKS = (CHECK_CONFIRM, CHECK_LOCK, CHECK_VERSION, CHECK_EVENT, CHECK_RESOURCE)

# ---- 判定结果（三态 + 未确定） ----
PROCEED = "proceed"                    # 条件已满足 → 继续干活（唤醒）
WAIT = "wait"                          # 仍未满足 → 挂检查点，走到点再探测
RELEASE = "release"                    # **我方不再需要** → 主动放锁，不再等（反队头阻塞）
UNKNOWN = "unknown"                    # 判不出（缺数据）→ 宽进：继续等，不误判

ALL_VERDICTS = (PROCEED, WAIT, RELEASE, UNKNOWN)

# ---- 平台侧探测落点（**永不选 "local_loop"**） ----
DRIVER_AWAIT = "platform_await"        # 挂 cnb:await：状态变化时由 resolve 唤醒（首选）
DRIVER_CRONTAB = "platform_crontab"    # 平台定时任务（crontab）到点代跑探测
DRIVER_EXTERNAL = "external_webhook"   # 外部 webhook / OPENAPI 触发 api_trigger 再跑
DRIVER_DELEGATE = "delegate_event"     # 委托持有者完成时回调 api_trigger_* 事件
DRIVER_HANDOFF = "handoff_resume"      # 交回人工/上游，由人解锁后再触发

ALL_DRIVERS = (DRIVER_AWAIT, DRIVER_CRONTAB, DRIVER_EXTERNAL,
               DRIVER_DELEGATE, DRIVER_HANDOFF)

# 平台定时任务最小间隔（官方文档：低于此间隔无法提交）
CRONTAB_MIN_INTERVAL_SEC = 300.0


def _driver_for_delay(delay_sec: float) -> str:
    """按等待时长挑平台落点：**短等待挂 await；长等待交平台定时任务**。

    - ≤ 平台定时任务最小间隔（5 分钟）→ `platform_await`（零占用，状态一变就醒）；
    - > 5 分钟 → `platform_crontab`（平台那个"不睡觉的进程"到点代跑一次探测）。
    """
    return DRIVER_AWAIT if float(delay_sec) <= CRONTAB_MIN_INTERVAL_SEC else DRIVER_CRONTAB


def _cron_every(delay_sec: float, base_minutes: int = 5) -> str:
    """把等待时长换算成 cron 表达式（向上取整到分钟；**不低于平台 5 分钟下限**）。

    例：``600s → "*/10 * * * *"``（每 10 分钟一次的探测点）。
    """
    minutes = max(int(base_minutes), int(-(-float(delay_sec) // 60.0)))
    return f"*/{minutes} * * * *"


@dataclass
class ProbeEvent:
    """一次探测产出的**结构化事件**（可跨 agent / 跨仓传递、可留痕）。

    「边缘计算的探测器」口径：本地只做**轻量采集 + 判定**，产出事件；
    重处理（评审/构建/模型调用）由事件驱动，**只在值得唤醒时才发生**。
    """

    checkpoint_key: str = ""
    kind: str = CHECK_EVENT
    verdict: str = UNKNOWN
    # 采集到的**轻量**事实（锁表视图 / 版本号 / 外部信号），不塞原始数据
    facts: Dict[str, Any] = field(default_factory=dict)
    driver: str = DRIVER_AWAIT
    next_delay_sec: float = 0.0
    checks_used: int = 0
    checks_left: int = 0
    resume_required: bool = False
    reason: str = ""
    action: str = ""            # 给接收方的**明确指令**（resume / keep_waiting / sweep_lock / human）
    wake_allowed: bool = True   # 是否允许据此唤醒下一轮（防"探测自己变成新循环"）
    at: float = 0.0

    def to_dict(self) -> dict:
        return {
            "checkpoint_key": self.checkpoint_key,
            "kind": self.kind,
            "verdict": self.verdict,
            "facts": dict(self.facts),
            "driver": self.driver,
            "next_delay_sec": self.next_delay_sec,
            "checks_used": self.checks_used,
            "checks_left": self.checks_left,
            "resume_required": self.resume_required,
            "reason": self.reason,
            "action": self.action,
            "wake_allowed": self.wake_allowed,
            "at": self.at,
        }

    @classmethod
    def from_dict(cls, data: Any) -> Optional["ProbeEvent"]:
        if not isinstance(data, dict):
            return None
        ev = cls()
        ev.checkpoint_key = str(data.get("checkpoint_key") or "")
        ev.kind = str(data.get("kind") or CHECK_EVENT)
        ev.verdict = str(data.get("verdict") or UNKNOWN)
        ev.facts = data.get("facts") if isinstance(data.get("facts"), dict) else {}
        ev.driver = str(data.get("driver") or DRIVER_AWAIT)
        ev.next_delay_sec = float(data.get("next_delay_sec") or 0.0)
        ev.checks_used = int(data.get("checks_used") or 0)
        ev.checks_left = int(data.get("checks_left") or 0)
        ev.resume_required = bool(data.get("resume_required") or False)
        ev.reason = str(data.get("reason") or "")
        ev.action = str(data.get("action") or "")
        ev.wake_allowed = bool(data.get("wake_allowed", True))
        ev.at = float(data.get("at") or 0.0)
        return ev


@dataclass
class Checkpoint:
    """一个**检查点**：等什么、怎么判、还能探几次、下一步挂到平台哪儿。"""

    key: str
    session_id: str = ""
    kind: str = CHECK_EVENT
    holder: str = ""                 # 我们在等谁（人/agent/外部系统）
    scope: str = ""                  # 等什么（锁键 / 版本对象 / 资源名）
    # 平台侧：等待语义的配对键 + 已挂流水线号（挂上了 = 零占用等待）
    await_key: str = ""
    resolve_status: str = ""         # ""(未挂) / "awaiting" / "resolved" / "timeout"
    sn: str = ""
    checks_used: int = 0             # 已探测次数（**最多 6 次**）
    max_checks: int = DEFAULT_MAX_ATTEMPTS
    next_delay_sec: float = 0.0      # 下一次等待时长（取阶梯）
    last_verdict: str = ""
    last_facts: Dict[str, Any] = field(default_factory=dict)
    created_at: float = 0.0
    updated_at: float = 0.0
    released: bool = False           # 我方主动放弃等待 → 放锁，不再占位
    resume_required: bool = False    # 到顶即停 → 交回人工
    history: List[Dict[str, Any]] = field(default_factory=list)

    def used_up(self) -> bool:
        """探测预算是否用尽（用尽 → **停止自动挂账**，交回人工/上游）。"""
        return self.checks_used >= self.max_checks

    def checks_left(self) -> int:
        return max(0, self.max_checks - self.checks_used)

    def to_dict(self) -> dict:
        return {
            "key": self.key,
            "session_id": self.session_id,
            "kind": self.kind,
            "holder": self.holder,
            "scope": self.scope,
            "await_key": self.await_key,
            "resolve_status": self.resolve_status,
            "sn": self.sn,
            "checks_used": self.checks_used,
            "max_checks": self.max_checks,
            "checks_left": self.checks_left(),
            "next_delay_sec": self.next_delay_sec,
            "last_verdict": self.last_verdict,
            "last_facts": dict(self.last_facts),
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "released": self.released,
            "resume_required": self.resume_required,
            "history": list(self.history),
        }

    @classmethod
    def from_dict(cls, data: Any) -> Optional["Checkpoint"]:
        if not isinstance(data, dict) or not data.get("key"):
            return None
        cp = cls(key=str(data["key"]))
        cp.session_id = str(data.get("session_id") or "")
        cp.kind = str(data.get("kind") or CHECK_EVENT)
        cp.holder = str(data.get("holder") or "")
        cp.scope = str(data.get("scope") or "")
        cp.await_key = str(data.get("await_key") or "")
        cp.resolve_status = str(data.get("resolve_status") or "")
        cp.sn = str(data.get("sn") or "")
        cp.checks_used = int(data.get("checks_used") or 0)
        cp.max_checks = int(data.get("max_checks") or DEFAULT_MAX_ATTEMPTS)
        cp.next_delay_sec = float(data.get("next_delay_sec") or 0.0)
        cp.last_verdict = str(data.get("last_verdict") or "")
        cp.last_facts = (data.get("last_facts")
                         if isinstance(data.get("last_facts"), dict) else {})
        cp.created_at = float(data.get("created_at") or 0.0)
        cp.updated_at = float(data.get("updated_at") or 0.0)
        cp.released = bool(data.get("released") or False)
        cp.resume_required = bool(data.get("resume_required") or False)
        cp.history = data.get("history") if isinstance(data.get("history"), list) else []
        return cp


@dataclass
class ProbeOutcome:
    """一次探测的完整产出：判定 + 事件 + **给流水线的平台指令**。"""

    verdict: str = UNKNOWN
    event: ProbeEvent = field(default_factory=ProbeEvent)
    checkpoint: Optional[Checkpoint] = None

    def to_dict(self) -> dict:
        return {
            "verdict": self.verdict,
            "event": self.event.to_dict(),
            "checkpoint": self.checkpoint.to_dict() if self.checkpoint else None,
        }


class CheckpointProbe:
    """检查点探测器注册表（**纯判定 + 平台落点编排，永不本地 sleep**）。

    Args:
        session_id: 会话标识（一个 Issue/PR 一个）
        data_dir: 检查点持久化目录（默认 ``<cwd>/data/probe``）
        backoff: 等待阶梯（决定"下一次什么时候看"；与探测次数上界共用语义）
        max_checks: 探测次数上界（**默认 6**，随阶梯档位数）
        clock: 注入时钟（默认 ``time.time``；测试可注入假时钟）
        lock_view: 采集**锁表视图**的函数（返回 ``{key: {...}}`` 或 ``None``）
        version_view: 采集**版本台账**的函数（返回当前版本号）
    """

    def __init__(
        self,
        session_id: str,
        data_dir: Optional[str] = None,
        backoff: Optional[StepBackoff] = None,
        max_checks: int = DEFAULT_MAX_ATTEMPTS,
        clock: Optional[Callable[[], float]] = None,
        lock_view: Optional[Callable[[], Optional[Dict[str, Any]]]] = None,
        version_view: Optional[Callable[[], Optional[int]]] = None,
        crontab_base_minutes: int = 5,
    ):
        if not isinstance(session_id, str) or not _SAFE_ID.match(session_id):
            raise ValueError(
                f"非法 session_id: {session_id!r}。仅允许 [A-Za-z0-9_.:-]。")
        import time as _time
        self.session_id = session_id
        self.data_dir = data_dir or os.path.join(os.getcwd(), "data", "probe")
        self.backoff = backoff or StepBackoff()
        self.max_checks = (int(max_checks)
                           if isinstance(max_checks, int) and not isinstance(max_checks, bool)
                           and max_checks >= 1 else DEFAULT_MAX_ATTEMPTS)
        self._clock: Callable[[], float] = clock or _time.time
        self._lock_view = lock_view
        self._version_view = version_view
        self.crontab_base_minutes = max(1, int(crontab_base_minutes))
        os.makedirs(self.data_dir, exist_ok=True)

    # ------------------------------------------------------------------
    # 落盘（一个检查点一个文件；损坏/缺失 → 视为不存在，不炸）
    # ------------------------------------------------------------------

    def _path(self, key: str) -> str:
        safe = re.sub(r'[^A-Za-z0-9_.:-]', '_', str(key))[:120] or "checkpoint"
        return os.path.join(self.data_dir, f"{safe}.json")

    def load(self, key: str) -> Optional[Checkpoint]:
        path = self._path(key)
        if not os.path.isfile(path):
            return None
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, ValueError):
            return None
        return Checkpoint.from_dict(data)

    def _save(self, cp: Checkpoint) -> None:
        cp.updated_at = self._clock()
        data = json.dumps(cp.to_dict(), ensure_ascii=False, indent=2)
        fd, tmp = tempfile.mkstemp(dir=self.data_dir, suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(data)
            os.replace(tmp, self._path(cp.key))
        finally:
            if os.path.exists(tmp):
                try:
                    os.remove(tmp)
                except OSError:
                    pass

    # ------------------------------------------------------------------
    # 建 / 查 / 列
    # ------------------------------------------------------------------

    def create(
        self,
        key: str,
        kind: str = CHECK_EVENT,
        holder: str = "",
        scope: str = "",
        await_key: str = "",
        max_checks: Optional[int] = None,
        note: str = "",
    ) -> Checkpoint:
        """登记一个检查点（**幂等**：同键已存在则原样返回，不重置预算）。

        幂等是刻意的：若"每次遇到都重建检查点"，就等于把"最多 6 次"变成无限次
        ——正是要治的那种循环。
        """
        existed = self.load(key)
        if existed is not None:
            return existed
        cp = Checkpoint(
            key=str(key),
            session_id=self.session_id,
            kind=str(kind or CHECK_EVENT),
            holder=str(holder or ""),
            scope=str(scope or ""),
            await_key=str(await_key or ""),
            max_checks=(self.max_checks if max_checks is None
                        else max(1, int(max_checks))),
            created_at=self._clock(),
            updated_at=self._clock(),
        )
        cp.history.append({
            "at": self._clock(), "action": "create", "kind": cp.kind,
            "holder": cp.holder, "note": note,
            "waiting_plan": self.backoff.human(),
        })
        self._save(cp)
        return cp

    def list_open(self) -> List[Checkpoint]:
        """所有**未结束**的检查点（未放行、未放弃、未交回人工）。"""
        out: List[Checkpoint] = []
        if not os.path.isdir(self.data_dir):
            return out
        for name in sorted(os.listdir(self.data_dir)):
            if not name.endswith(".json"):
                continue
            cp = self.load(name[:-5])
            if cp is None:
                continue
            if cp.released or cp.resume_required:
                continue
            if cp.last_verdict == PROCEED:
                continue
            out.append(cp)
        return out

    # ------------------------------------------------------------------
    # 等待计划（**六次，每次一个平台落点；没有任何一次是本地空转**）
    # ------------------------------------------------------------------

    def delay_plan(self, cp: Optional[Checkpoint] = None) -> List[Dict[str, Any]]:
        """六次探测怎么等：等待阶梯 + **平台落点** + cron 表达式（非轮询）。

        「最多 6 次」= 阶梯档位数；第 6 次（10 分钟档）之后预算用尽 →
        `resume_required`（停止自动挂账，交回人工/上游）。
        """
        n = cp.max_checks if cp is not None else self.max_checks
        rows: List[Dict[str, Any]] = []
        for i in range(n):
            st = self.backoff.status(i)
            driver = _driver_for_delay(st.delay_sec)
            rows.append({
                "check": i + 1,
                "delay_sec": st.delay_sec,
                "label": st.label,
                "at_top": st.at_top,
                "driver": driver,
                "crontab": (_cron_every(st.delay_sec, self.crontab_base_minutes)
                            if driver == DRIVER_CRONTAB else ""),
                "local_cpu": "0（不占构建核时）",
                "note": ("挂 cnb:await，锁释放方 resolve 唤醒（零占用）"
                         if driver == DRIVER_AWAIT else
                         "交平台定时任务（crontab）到点代跑一次探测；"
                         "平台最小间隔 5 分钟"),
            })
        rows.append({
            "check": n + 1,
            "delay_sec": 0.0,
            "label": "预算用尽",
            "at_top": True,
            "driver": DRIVER_HANDOFF,
            "crontab": "",
            "local_cpu": "0（不占构建核时）",
            "note": "停止自动挂账 → 交回人工/上游（resume_required），不再自己等",
        })
        return rows

    # ------------------------------------------------------------------
    # 触发机制：登记"已把等待挂到平台语义上"（这才是"非轮询"的落点）
    # ------------------------------------------------------------------

    def register_platform_wait(
        self,
        key: str,
        await_key: str = "",
        sn: str = "",
        driver: str = DRIVER_AWAIT,
        note: str = "",
    ) -> Optional[Checkpoint]:
        """登记"等待已挂到平台语义上"：挂上了 → 本地**零占用**（可被回收）。

        这对应 `.cnb.yml` 里的 `cnb:await`（配对键 `await_key`）——
        锁/状态一旦变化，由对端 `cnb:resolve` 唤醒；也可由外部 webhook
        触发 `api_trigger` 再跑（`external_webhook`）。
        """
        cp = self.load(key)
        if cp is None:
            return None
        if driver not in ALL_DRIVERS or driver == DRIVER_HANDOFF:
            raise ValueError(
                f"非法平台落点: {driver!r}（不得为本地空转/人工落点）。"
                f"允许: {ALL_DRIVERS}")
        if await_key:
            cp.await_key = str(await_key)
        if sn:
            cp.sn = str(sn)
        cp.resolve_status = "awaiting"
        cp.history.append({
            "at": self._clock(), "action": "platform_wait", "driver": driver,
            "await_key": cp.await_key, "sn": cp.sn, "note": note,
        })
        self._save(cp)
        return cp

    def resolve(
        self,
        key: str,
        status: str = "resolved",
        facts: Optional[Dict[str, Any]] = None,
    ) -> Optional[Checkpoint]:
        """平台侧 `cnb:resolve` 到达（对端状态变化）→ 标记检查点已被唤醒。"""
        cp = self.load(key)
        if cp is None:
            return None
        cp.resolve_status = str(status or "resolved")
        if facts:
            cp.last_facts.update(facts)
        cp.history.append({"at": self._clock(), "action": "resolve",
                           "status": cp.resolve_status,
                           "facts": dict(facts or {})})
        self._save(cp)
        return cp

    # ------------------------------------------------------------------
    # 探测器：轻量采集 + 判定检查点（**只看可判定事实**）
    # ------------------------------------------------------------------

    def _collect_lock_facts(self, cp: Checkpoint) -> Optional[Dict[str, Any]]:
        """采集**锁快照**（边缘探测器口径：只取相关那一把，不搬全量）。"""
        view = None
        if self._lock_view is not None:
            try:
                view = self._lock_view()
            except Exception:      # pragma: no cover - 采集失败按"判不出"处理
                view = None
        if view is None:
            return None
        scope = cp.scope or cp.key
        held = view.get("held") if isinstance(view, dict) else None
        if isinstance(held, list):
            for row in held:
                if not isinstance(row, dict):
                    continue
                if scope and scope in (row.get("key"), row.get("scope")):
                    return {
                        "lock_held": True,
                        "holder": row.get("holder") or "",
                        "persistent": bool(row.get("persistent")),
                        "ttl_left_sec": row.get("ttl_left_sec"),
                    }
            return {"lock_held": False, "holder": ""}
        # 兼容 `{key: {...}}` 形态
        if isinstance(held, dict):
            row = held.get(scope)
            if row is None:
                return {"lock_held": False, "holder": ""}
            return {"lock_held": True,
                    "holder": (row or {}).get("holder", "")
                    if isinstance(row, dict) else ""}
        return None

    def _collect_version_fact(self, cp: Checkpoint) -> Optional[Dict[str, Any]]:
        """采集**版本号**（不搬内容——边缘探测器只回传"变了没"）。"""
        if self._version_view is None:
            return None
        try:
            cur = self._version_view()
        except Exception:          # pragma: no cover - 采集失败按"判不出"处理
            return None
        if not isinstance(cur, int) or isinstance(cur, bool):
            return None
        return {"version": cur}

    def probe(self, key: str, **kwargs) -> ProbeOutcome:
        """探测一次（第 N+1 次）：**采集 → 判定 → 记预算 → 给出下一步落点**。

        Args:
            our_need_satisfied: **我方是否还等这件事**。False 表示"我已不需要这个锁/
                这个版本了" → 直接 `release`（放锁、不再等），**避免队头阻塞**。
            confirmed / version_changed / event_fired / resource_ready: 各判别式的事实
        """
        cp = self.load(key)
        if cp is None:
            cp = self.create(key)
        if cp.used_up():
            return self._outcome(cp, WAIT,
                                 reason="探测预算已用尽（最多 %d 次）" % cp.max_checks,
                                 resume_required=True,
                                 action="human_resume")
        # ① 采集（轻量）。**先留一份探测前快照**：判定必须拿"探测前"的事实做参照，
        #    否则先写入新事实再比较，永远等于自身 → "版本变了"这类判别恒为"未变"
        #    （与 ReviewArbiter 的"进度参照取基线快照"是同一类缺陷，务必防退化）。
        prev_facts = dict(cp.last_facts)
        facts = self._collect(cp, kwargs)
        cp.checks_used += 1
        # ② 判定（确定性顺序，先"我方是否还需要"）。用 **prev_facts** 作参照。
        verdict, reason = self._judge(cp, facts, kwargs, prev=prev_facts)
        cp.last_facts.update(facts)
        cp.last_verdict = verdict
        # ③ 记预算 / 下一步落点
        resume_required = False
        next_delay = 0.0
        if verdict in (PROCEED, RELEASE):
            cp.next_delay_sec = 0.0
            cp.released = (verdict == RELEASE)
        elif cp.used_up():
            resume_required = True
            cp.resume_required = True
            cp.next_delay_sec = 0.0
            reason = (f"{reason}；探测次数已达上界（{cp.max_checks} 次/阶梯顶）"
                      "→ 停止自动挂账，交回人工/上游")
        else:
            # 第 N 次探测做完后，"下一次等待" 是阶梯的**下一档**（ladder[N]）——
            # 反例：用 ladder[N-1] 会把降级当成下一次等待时长（等过一次的档不得重复）。
            st = self.backoff.status(cp.checks_used)
            next_delay = st.delay_sec
            cp.next_delay_sec = next_delay
        cp.history.append({
            "at": self._clock(), "action": "probe", "check": cp.checks_used,
            "verdict": verdict, "facts": dict(facts), "reason": reason,
            "next_delay_sec": next_delay, "resume_required": resume_required,
        })
        self._save(cp)
        return self._outcome(cp, verdict, reason=reason,
                             resume_required=resume_required, facts=facts)

    @staticmethod
    def _collect(cp: Checkpoint, kw: Dict[str, Any]) -> Dict[str, Any]:
        """把调用方给的**事实**整理成采集快照（只留可判定项，不塞原始数据）。"""
        out: Dict[str, Any] = {}
        for name in ("confirmed", "version_changed", "event_fired",
                     "resource_ready", "lock_held", "our_need_satisfied"):
            if name in kw and kw[name] is not None:
                out[name] = bool(kw[name])
        if "version" in kw and isinstance(kw["version"], int):
            out["version"] = int(kw["version"])
        return out

    def _judge(self, cp: Checkpoint, facts: Dict[str, Any], kw: Dict[str, Any],
               prev: Optional[Dict[str, Any]] = None) -> Any:
        """**判断检查点**：按判别式给三态 + unknown（不看 agent 自述）。

        Args:
            facts: 本次采集到的事实
            prev: **探测前**的事实快照（版本类判别式的参照；缺省回落 ``cp.last_facts``）
        """
        # 0) 我方不再需要 → 立即放锁（反队头阻塞；比"等对方放锁"更高优先）
        if kw.get("our_need_satisfied") is False:
            return RELEASE, "我方已不再需要该检查点 → 主动放锁，不再占用等待"
        kind = cp.kind
        if kind == CHECK_CONFIRM:
            if isinstance(kw.get("confirmed"), bool):
                return ((PROCEED, "已获得人工/上游确认") if kw["confirmed"]
                        else (WAIT, "尚未获得确认"))
            return UNKNOWN, "缺少确认事实（未给 confirmed）"
        prev = cp.last_facts if prev is None else prev
        if kind == CHECK_LOCK:
            facts = self._collect_lock_facts(cp) or facts
            if "lock_held" in facts:
                if facts["lock_held"]:
                    return WAIT, (f"锁仍在 {facts.get('holder') or '未知持有者'} 手上"
                                  "（持续锁：等对方处理完）")
                return PROCEED, "锁已释放 → 条件满足，可以继续"
            return UNKNOWN, "锁表不可见（未接入锁台账）→ 不误判，继续等"
        if kind == CHECK_VERSION:
            facts2 = self._collect_version_fact(cp) or facts
            if "version_changed" in facts2:
                return ((PROCEED, "版本已变化") if facts2["version_changed"]
                        else (WAIT, "版本未变（无新进展）"))
            if "version" in facts2:
                before = prev.get("version")
                cur = facts2["version"]
                if isinstance(before, int) and before != cur:
                    return PROCEED, f"版本号变化 {before} → {cur}"
                if isinstance(before, int) and before == cur:
                    return WAIT, f"版本号未变（仍为 {cur}）"
                return UNKNOWN, f"首次采集版本 {cur}，无基线可比（下轮再判）"
            return UNKNOWN, "版本台账不可见 → 不误判，继续等"
        if kind == CHECK_EVENT:
            if "event_fired" in facts:
                return ((PROCEED, "外部事件已到达（webhook / resolve）")
                        if facts["event_fired"] else (WAIT, "外部事件尚未到达"))
            if cp.resolve_status == "resolved":
                return PROCEED, "平台侧 resolve 已到达（状态变化已确认）"
            return UNKNOWN, "尚未收到外部事件（等待 webhook / cnb:resolve）"
        if kind == CHECK_RESOURCE:
            if "resource_ready" in facts:
                return ((PROCEED, "外部资源已就绪") if facts["resource_ready"]
                        else (WAIT, "外部资源尚未就绪"))
            return UNKNOWN, "资源就绪信号缺失 → 不误判，继续等"
        return UNKNOWN, f"未知判别式 {kind!r}"

    def _outcome(self, cp: Checkpoint, verdict: str, reason: str = "",
                 resume_required: bool = False,
                 facts: Optional[Dict[str, Any]] = None,
                 action: str = "") -> ProbeOutcome:
        """组装产出：判定 + **结构化事件** + 平台指令。"""
        if verdict == PROCEED:
            driver, act = DRIVER_HANDOFF, "resume"
        elif verdict == RELEASE:
            driver, act = DRIVER_HANDOFF, "release_lock"
        elif resume_required:
            driver, act = DRIVER_HANDOFF, "human_resume"
        else:
            driver = _driver_for_delay(cp.next_delay_sec)
            act = "keep_waiting"
        if action:
            act = action
        event = ProbeEvent(
            checkpoint_key=cp.key,
            kind=cp.kind,
            verdict=verdict,
            facts=dict(facts or cp.last_facts),
            driver=driver,
            next_delay_sec=cp.next_delay_sec,
            checks_used=cp.checks_used,
            checks_left=cp.checks_left(),
            resume_required=resume_required or cp.resume_required,
            reason=reason,
            action=act,
            wake_allowed=(verdict == PROCEED) and not resume_required,
            at=self._clock(),
        )
        return ProbeOutcome(verdict=verdict, event=event, checkpoint=cp)

    # ------------------------------------------------------------------
    # 事件分发：结构化事件交给下一棒（跨 agent/跨仓），**不靠自述**
    # ------------------------------------------------------------------

    def dispatch(self, event: ProbeEvent) -> Dict[str, Any]:
        """把探测事件转成**给流水线/下一棒的明确指令**（平台落点 + 触发方式）。

        「webhook 触发再跑」在 CNB 上的三种原生写法都在这里给出，按判定结果选：
        - 条件满足 → `cnb:apply`/`cnb:trigger` 触发 `api_trigger_*` 再跑（真正干活）；
        - 仍未满足 → `cnb:await` 挂起等待（零占用），或交 `crontab` 到点代跑；
        - 预算用尽 → 交回人工（`resume_required`，不得据此再开一轮）。
        """
        if event.verdict == PROCEED:
            return {
                "action": "resume",
                "driver": DRIVER_DELEGATE,
                "trigger": {
                    "type": "cnb:apply",
                    "event": "api_trigger_probe_resume",
                    "env": {"CHECKPOINT_KEY": event.checkpoint_key,
                            "PROBE_VERDICT": event.verdict},
                    "note": "条件已满足：触发一次真正干活的流水线（评审/构建），"
                            "不是又开一轮等待",
                },
                "wake_allowed": True,
                "reason": event.reason,
            }
        if event.verdict == RELEASE:
            return {
                "action": "release_lock",
                "driver": DRIVER_HANDOFF,
                "trigger": None,
                "wake_allowed": False,
                "reason": event.reason + "｜放锁后不再挂检查点（反队头阻塞）",
            }
        if event.resume_required:
            return {
                "action": "human_resume",
                "driver": DRIVER_HANDOFF,
                "trigger": None,
                "wake_allowed": False,
                "reason": event.reason + "｜需人工 resume(by=…, reason=…) 留痕解锁",
            }
        if event.driver == DRIVER_AWAIT:
            return {
                "action": "platform_await",
                "driver": DRIVER_AWAIT,
                "trigger": {
                    "type": "cnb:await",
                    "key": f"{self.session_id}-{event.checkpoint_key}",
                    "note": "零占用挂起；锁/状态变化时由对端 cnb:resolve 唤醒。"
                            "**本地不 sleep、不轮询**",
                },
                "wake_allowed": False,
                "reason": event.reason,
            }
        return {
            "action": "platform_crontab",
            "driver": DRIVER_CRONTAB,
            "trigger": {
                "type": "crontab",
                "cron": _cron_every(event.next_delay_sec, self.crontab_base_minutes),
                "event": "api_trigger_probe_check",
                "note": "长等待交**平台**定时任务到点代跑一次探测；平台最小间隔 5 分钟",
            },
            "wake_allowed": False,
            "reason": event.reason,
        }

    # ------------------------------------------------------------------
    # 视图
    # ------------------------------------------------------------------

    def overview(self) -> Dict[str, Any]:
        """所有检查点总览（"触发了一眼就能看"）。"""
        cps = self.list_open()
        return {
            "session_id": self.session_id,
            "count": len(cps),
            "max_checks": self.max_checks,
            "waiting_plan": self.backoff.human(),
            "checkpoints": [{
                "key": c.key,
                "kind": c.kind,
                "holder": c.holder,
                "scope": c.scope,
                "checks_used": c.checks_used,
                "checks_left": c.checks_left(),
                "next_delay_sec": c.next_delay_sec,
                "driver": _driver_for_delay(c.next_delay_sec)
                if c.next_delay_sec else DRIVER_HANDOFF,
                "last_verdict": c.last_verdict,
                "platform_wait": c.resolve_status or "未挂",
            } for c in cps],
            "where_to_avoid": [
                f"{c.scope or c.key}（等 {c.holder or '未知'}｜还剩 "
                f"{c.checks_left()} 次探测｜下一次 "
                f"{c.next_delay_sec:.0f}s）" for c in cps
            ],
        }

    def delete_state(self) -> bool:
        """会话生命周期回收（同 id 复建不带陈旧检查点）。"""
        removed = False
        if not os.path.isdir(self.data_dir):
            return removed
        for name in os.listdir(self.data_dir):
            if not name.endswith(".json"):
                continue
            try:
                os.remove(os.path.join(self.data_dir, name))
                removed = True
            except OSError:
                pass
        return removed
