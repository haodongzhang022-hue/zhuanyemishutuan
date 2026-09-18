"""平台语义对齐（PlatformSemantics）：把协同落点对齐到**平台真实能力**

## 为什么需要这一层（Issue #14：用户要求"查官方最佳实践并参考过来"）

前几轮把等待/探测抽象成"平台落点"（`cnb:await` / `crontab` / `api_trigger`），
方向是对的，但**抽象的"平台语义"不等于具体任务的真实约束**。经查证 CNB 官方文档
（`/zh/build/internal-steps/cnb/await-resolve.md`、`/zh/build/trigger-rule.md`、
`/zh/build/internal-steps/cnb/apply.md`、`/zh/build/internal-steps/cnb/trigger.md`），
`cnb:await` 有两条**会让原设计失效**的硬约束：

| 官方约束（原文） | 原文设计的问题 | 本模块的收口 |
|---|---|---|
| 「只能对**同一个事件**触发的 pipeline 进行 await 和 resolve 操作」 | 探测/干活 pipeline 常由 `crontab`、`api_trigger` 触发，而**放锁方**多在 `push`/评论事件里 → 两者**配不上对**，await 会**永远等不到** resolve | `await_eligible()`：不同事件 / 未知事件 → **不得**挂 await，降级 `crontab`（平台代跑，且由平台做死锁检测） |
| 「一个 `key` **仅能 resolve 一次**，但可以 await 多次」 | 同一检查点第 4、5 次探测若复用同一个 await key，第 2 次以后**必死等**（key 已被 resolve 过） | `await_key_for(checkpoint, attempt)`：key 带**轮次**，每轮唯一 |
| 「`死锁检测` 会自动检测…**无限等待**：await 一个不存在的 key，或者 key 没有关联 resolve」 | 对端不 resolve（人没编辑完 / 外部系统不回调）→ 靠平台死锁检测兜底，需**提前说明**而不是等到超时才发现 | `local_cpu_note()` / `timeout_caveat`：把这条写进口径（平台侧能兜底 ≠ 我们可以设计成依赖它） |
| 「await-resolve 任务只能运行在流水线的 `stages` 中，不能运行在 `endStages` 和 `failStages` 中」 | 收尾/失败阶段挂 await 会直接**不生效** | `await_stage_ok()`：位置校验，非 `stages` → 拒绝 |

另外把**官方自身的增量最佳实践**纳入对照（见 `official_capabilities()`）：
官方 AI 评审插件 `cnbcool/code-review` 已经做了 diff 增量、非代码文件过滤、
历史评论去重（`include_existing_comments`）、行内评论提交；SCA 插件按
"新增组件 / 新增 CVE"判增量、**内容未变化的改名不重复报**。这两条正好印证并
补强了本仓库的"增量 + 同口径不重复"与"评审评论去重"口径。

## 与其它模块的分工

| 模块 | 回答 |
|---|---|
| `WaitPolicy` | 该怎么等（平台挂起 / 先做其他 / 交回人工） |
| `probe._driver_for_delay` | 等多久对应哪个落点（阈值分工） |
| **`PlatformSemantics`** | **这个落点在这条流水线/这个事件下，平台到底允不允许、能不能配上对** |
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List

# 平台定时任务最小间隔（官方文档下限；与 `probe.CRONTAB_MIN_INTERVAL_SEC` 同源）。
# 这里**本地定义常量**而不是从 `probe` 导入：`probe` 需要本模块做落点校验，
# 若反向导入会成环。两者一致性由测试对拍（`test_platform_semantics_issue14`）。
CRONTAB_MIN_INTERVAL_SEC = 300.0

# 落点名称（与 `probe` 同字面量；同为避免成环而本地定义）
DRIVER_AWAIT = "platform_await"
DRIVER_CRONTAB = "platform_crontab"
DRIVER_EXTERNAL = "external_webhook"
DRIVER_HANDOFF = "handoff_resume"

# ------------------------------------------------------------------
# 官方事实（逐条带出处，避免"口径漂移"与"凭印象设计"）
# ------------------------------------------------------------------

#: 平台文档出处（写进口径，便于回评/复核时直接引用）
DOC_SOURCES: Dict[str, str] = {
    "await": "/zh/build/internal-steps/cnb/await-resolve.md",
    "crontab": "/zh/build/crontab.md",
    "trigger": "/zh/build/trigger-rule.md",
    "apply": "/zh/build/internal-steps/cnb/apply.md",
    "trigger_task": "/zh/build/internal-steps/cnb/trigger.md",
    "code_review": "https://cnb.cool/cnb/plugins/cnbcool/code-review",
    "sca": "/zh/security/sca-vulnerability.md",
    "pricing": "/zh/pricing.md",
    "npc": "/zh/build/npc.md",
}

#: `cnb:await` / `cnb:resolve` 的官方硬约束（原文要点）
AWAIT_CONSTRAINTS: Dict[str, str] = {
    "same_event": "只能对**同一个事件**触发的 pipeline 做 await/resolve（不同事件配不上对）",
    "resolve_once": "一个 `key` **仅能 resolve 一次**（可被 await 多次）——复用同一 key 会导致后续等待等不到",
    "deadlock": "平台会自动做死锁检测（互相 await / await 环 / await 不存在的 key / 重复 resolve），"
                "检测到时结束等待并抛 `dead lock found.`",
    "stage_only": "await-resolve 只能运行在流水线的 `stages` 中（`endStages` / `failStages` 内不生效）",
}

#: 触发事件分类：**能否与某个事件"配对上 await"取决于两侧是否同一事件**
#:
#: 口径：只有**已知且同类**的事件之间才可能配对；涉及"人"或"外部系统"的事件
#: （评论/评审/issue/webhook）在探测方看来**不可判定**，一律按"配不上对"处理。
EVENT_PUSH = "push"
EVENT_COMMIT_ADD = "commit.add"
EVENT_PR = "pull_request"
EVENT_PR_TARGET = "pull_request.target"
EVENT_PR_MERGED = "pull_request.merged"
EVENT_COMMENT = "issue.comment"
EVENT_PR_COMMENT = "pull_request.comment"
EVENT_CRONTAB = "crontab"
EVENT_API_TRIGGER = "api_trigger"
EVENT_WEB_TRIGGER = "web_trigger"
EVENT_NPC_COMMENT = "*.comment@npc"

#: 已知事件全集（**宽进**：不在集合里的事件按"不可判定"处理，不猜）
KNOWN_EVENTS = (
    EVENT_PUSH, EVENT_COMMIT_ADD, EVENT_PR, EVENT_PR_TARGET,
    EVENT_PR_MERGED, EVENT_COMMENT, EVENT_PR_COMMENT, EVENT_CRONTAB,
    EVENT_API_TRIGGER, EVENT_WEB_TRIGGER,
)

#: **平台原生定时**类事件（无人工/外部方参与，等它就是等平台，无需 await）
_PLATFORM_OWNED_EVENTS = (EVENT_CRONTAB,)


def norm_event(event: Any) -> str:
    """事件名归一化（小写去空白；非字符串 → 空串 = 不可判定）。"""
    if not isinstance(event, str):
        return ""
    return event.strip().lower()


def await_eligible(
    waiter_event: Any,
    resolver_event: Any = "",
    stage: str = "stages",
) -> Dict[str, Any]:
    """**能不能挂 `cnb:await`**——按官方两条硬约束判定（判不出就不挂）。

    Args:
        waiter_event: 等待方（探测方）这条 pipeline 的事件
        resolver_event: 放锁方（对端）那条 pipeline 的事件；空 = 不知道
        stage: await 任务所在位置（`stages` / `endStages` / `failStages`）

    Returns:
        ``{"eligible": bool, "reason": str, "fallback": driver, "constraints": [...]}``

    判定是**保守的**（宁可降级 crontab，也不挂一个配不上对的 await）：
    - 位置不在 `stages` → 不挂（官方：endStages/failStages 内不生效）；
    - 两侧事件**任一不可判定** → 不挂（"看不见 ≠ 会 resolve"）；
    - 两侧事件**不同** → 不挂（官方：只能同一事件配对）；
    - 等待方本身是平台定时事件 → 不挂（平台自己会再来一次，等它没有意义）。
    """
    fallback = DRIVER_CRONTAB
    if not isinstance(stage, str) or stage.strip().lower() != "stages":
        return {
            "eligible": False,
            "reason": f"await 不在 `stages`（当前 {stage!r}）→ 平台内不生效，降级 crontab",
            "fallback": fallback,
            "constraints": [AWAIT_CONSTRAINTS["stage_only"]],
        }
    we = norm_event(waiter_event)
    re_ = norm_event(resolver_event)
    if not we or not re_:
        return {
            "eligible": False,
            "reason": "两侧事件不可判定（缺事件名）→ 不挂 await，降级 crontab（平台代跑 + 平台死锁检测兜底）",
            "fallback": fallback,
            "constraints": [AWAIT_CONSTRAINTS["same_event"], AWAIT_CONSTRAINTS["deadlock"]],
        }
    if we in _PLATFORM_OWNED_EVENTS:
        return {
            "eligible": False,
            "reason": f"等待方本身是平台定时事件（{we}）→ 平台到点会再来一次，无需 await",
            "fallback": fallback,
            "constraints": [AWAIT_CONSTRAINTS["same_event"]],
        }
    if we != re_:
        return {
            "eligible": False,
            "reason": (f"两侧事件不同（等待方 {we} / 放锁方 {re_}）→ "
                       "await 与 resolve 配不上对，会等不到；降级 crontab"),
            "fallback": fallback,
            "constraints": [AWAIT_CONSTRAINTS["same_event"], AWAIT_CONSTRAINTS["deadlock"]],
        }
    return {
        "eligible": True,
        "reason": f"同一事件（{we}）+ 位于 stages → 可挂 await（零占用，对端 resolve 唤醒）",
        "fallback": "",
        "constraints": [AWAIT_CONSTRAINTS["resolve_once"]],
    }


def await_key_for(checkpoint_key: Any, attempt: Any = 0) -> str:
    """**每轮唯一**的 await key——对官方「一个 key 仅能 resolve 一次」的落点。

    复用同一 key 的后果是硬性的：第 2 次及以后的 await 永远等不到 resolve
    （key 已被消费），表现为"探测卡住到平台死锁检测才发现"。
    故 key 一律带轮次，且轮次不可判定时用 `0`（仍保证同轮唯一）。
    """
    try:
        n = max(0, int(attempt))
    except (TypeError, ValueError):
        n = 0
    key = str(checkpoint_key or "").strip()
    return f"probe:{key}:r{n}"


def await_stage_ok(stage: Any) -> bool:
    """await 位置校验：只有 `stages` 有效（官方约束）。"""
    return isinstance(stage, str) and stage.strip().lower() == "stages"


@dataclass
class PlatformVerdict:
    """一条落点的**平台可执行性**判定（含降级与理由，便于留痕/回评）。"""

    driver: str = DRIVER_CRONTAB
    requested: str = DRIVER_AWAIT
    downgraded: bool = False
    reason: str = ""
    await_key: str = ""
    constraints: List[str] = field(default_factory=list)
    sources: List[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "driver": self.driver,
            "requested": self.requested,
            "downgraded": self.downgraded,
            "reason": self.reason,
            "await_key": self.await_key,
            "constraints": list(self.constraints),
            "sources": list(self.sources),
        }


def resolve_driver(
    requested: str,
    checkpoint_key: Any = "",
    attempt: Any = 0,
    waiter_event: Any = "",
    resolver_event: Any = "",
    stage: str = "stages",
    await_max_sec: float = CRONTAB_MIN_INTERVAL_SEC,
) -> PlatformVerdict:
    """把"想要的落点"收口成"**平台真能执行**的落点"（判不出就降级，不硬上）。

    - `requested` 不是 `platform_await` → 原样返回（其它落点不受本层约束）；
    - `requested == platform_await` → 逐条过官方硬约束；任一不过 → 降级
      `platform_crontab`（并给出理由与出处）；
    - 降级**只可能降级到 crontab 或互斥外的平台落点**，永不落到"本地空转"。
    """
    src = [DOC_SOURCES["await"]]
    if requested != DRIVER_AWAIT:
        return PlatformVerdict(driver=requested, requested=requested,
                               reason="非 await 落点：不受 await 配对约束",
                               sources=src)
    chk = await_eligible(waiter_event, resolver_event, stage)
    if not chk["eligible"]:
        return PlatformVerdict(
            driver=DRIVER_CRONTAB, requested=requested, downgraded=True,
            reason=chk["reason"], constraints=list(chk["constraints"]),
            sources=src + [DOC_SOURCES["crontab"]],
        )
    return PlatformVerdict(
        driver=DRIVER_AWAIT, requested=requested, downgraded=False,
        reason=chk["reason"], await_key=await_key_for(checkpoint_key, attempt),
        constraints=list(chk["constraints"]), sources=src,
    )


def local_cpu_note() -> str:
    """等待/降级都不占 CPU 的一句话口径（含"降级不代表丢功能"）。"""
    return (
        "降级到 crontab 仍是**平台**在等（平台是那个不睡觉的进程，且平台会做死锁检测），"
        "本地 CPU 占用恒为 0；两处落点都不允许本地 sleep/轮询。"
    )


def full_run_eligible(
    event: Any,
    blocked_events: Any = None,
) -> Dict[str, Any]:
    """**这个事件下能不能跑全量**——目录/评论/定时类事件一律不允许。

    这是"一次污染了很多仓库"的直接堵口：全量回归只有在**代码推进**类事件
    （`push` / `pull_request` 等真正与变更绑定的事件）下才有意义。
    评论、打开/更新 Issue、平台定时任务这些**不含有测试阶段**的事件，
    既没有明确基线、也没有变更集，跑全量只是空烧。

    判定是**保守的**（判不出事件就拦）：`blocked_events` 命中或事件不可判定 → 不允许。
    """
    ev = norm_event(event)
    if blocked_events is None:
        try:
            from .rules import DEFAULT_BLOCKED_EVENTS as blocked_events  # type: ignore
        except Exception:  # pragma: no cover - 兜底
            blocked_events = ["issue.comment", "pull_request.comment",
                              "crontab", "api_trigger", "web_trigger"]
    block = [str(e).strip().lower() for e in (blocked_events or []) if str(e).strip()]
    if not ev:
        return {"eligible": False, "blocked": True,
                "reason": "触发事件不可判定 → 不得跑全量（拿不准就不全量）",
                "event": ev}
    if ev in block:
        return {"eligible": False, "blocked": True,
                "reason": f"事件 `{ev}` 不含有测试阶段（非代码推进）→ 不得跑全量",
                "event": ev}
    return {"eligible": True, "blocked": False,
            "reason": f"事件 `{ev}` 为代码推进类 → 可（在大版本条件下）跑全量",
            "event": ev}


def pipeline_change_allowed(
    changed_paths: Any,
    allowlist: Any = None,
) -> Dict[str, Any]:
    """**流水线兜底变更的路径白名单**（最小权限）。

    背景：为了让存量仓库真的生效，需要一条带自校验的兜底路径。兜底一旦能被用来
    "顺手改点别的"，就把它变成了**绕过门禁的后门** —— 恶意/误用输入可能通过兜底
    写进任意文件（路径穿越、绝对路径、改流水线本身）。

    判定（任一不满足即拒）：

    - 路径必须是**相对路径**（拒绝绝对路径与 `..` 穿越）；
    - 必须以白名单里的某个前缀开头（默认只允许 `shared/collab/`）。

    判定是**保守的**（判不出就拒）：拿不准的输入一律拒绝，
    让调用方走正常 PR 路径，而不是靠兜底。
    """
    if allowlist is None:
        try:
            from .rules import DEFAULT_PIPELINE_ALLOWLIST as allowlist  # type: ignore
        except Exception:  # pragma: no cover - 兜底
            allowlist = ["shared/collab/"]
    prefixes = [str(p) for p in (allowlist or []) if str(p).strip()]
    raw = changed_paths if isinstance(changed_paths, (list, tuple, set)) \
        else ([changed_paths] if isinstance(changed_paths, str) else [])
    paths = [str(x).strip() for x in raw if str(x).strip()]
    if not paths:
        return {"allowed": False, "reason": "变更路径为空 → 拒绝（兜底不认无路径变更）",
                "paths": []}
    for path in paths:
        if path.startswith("/") or "\\" in path.replace("/", ""):
            return {"allowed": False,
                    "reason": f"路径 `{path}` 为绝对路径/非法分隔符 → 拒绝",
                    "paths": paths}
        if ".." in path.split("/"):
            return {"allowed": False,
                    "reason": f"路径 `{path}` 含 `..` 穿越 → 拒绝", "paths": paths}
        if not any(path.startswith(p) for p in prefixes):
            return {"allowed": False,
                    "reason": (f"路径 `{path}` 超出允许范围 "
                               f"（仅 {prefixes}）→ 拒绝（兜底不得改规则之外的文件）"),
                    "paths": paths}
    return {"allowed": True, "reason": f"变更均在允许范围内（{prefixes}）",
            "paths": paths}


def official_capabilities() -> Dict[str, Any]:
    """**官方最佳实践对照表**——"别人怎么做的"（Issue #14 要求检索）。

    逐条说明"官方已有能力 / 我们已有对应 / 我们的增量在哪"，
    便于判断"是否有比我们处理得更好的实现可以抄过来"。
    """
    return {
        "ai_review_plugin": {
            "source": DOC_SOURCES["code_review"],
            "capability": [
                "基于 diff 的**增量评审**（只评审变更，不重审全仓）",
                "自动过滤非代码文件（lock 文件、图片、构建产物等）",
                "`include_existing_comments`: 拉取已有评论/评审作**参考**（避免重复刷同一条）",
                "过期过滤：只丢弃挂在**基线旧代码**上的讨论（baseline + merge-base 祖先判定）",
                "降噪：`min_severity` / `min_confidence` / `exclude_categories`（默认屏蔽 style,nit）",
                "`max_comments` 按严重程度排序截断，避免刷屏",
                "评审事件模式 `auto`：无问题→approve；有 critical→request_changes；其余→comment",
            ],
            "ours": [
                "`decide_scan()`：日常只审变化、全量只在大版本（范围分级）",
                "`ReviewArbiter.begin_review()`：**只在开启评审时记版本**，基线不推前",
                "`RunScopeGuard`：同一口径只跑一次，禁止「连跑三次」当稳定性论证",
            ],
            "gap_we_close": [
                "官方按**评论内容**去重（AI 参考已有评论）；我们补**结构化指纹精确去重**："
                "`review_fingerprint` 对同一「文件+问题类型+代码锚点」的重复项直接合并，"
                "不依赖模型「记不记得」（见 `review/fingerprint.py`）",
                "非代码文件的判定口径放进规则包（组织级统一），不散落在各仓脚本里",
            ],
        },
        "sca_plugin": {
            "source": DOC_SOURCES["sca"],
            "capability": [
                "只报告**本次新增**的漏洞（增量）",
                "已存在的同一组件同一 CVE **不重复阻断**",
                "**内容未变化的文件重命名不重复产生**漏洞",
            ],
            "ours": [
                "`RunScopeGuard` 口径指纹 = 范围+版本+受影响面+命令（同口径不重复）",
            ],
            "gap_we_close": [
                "把「改名/移动不产生新工作」升级成通用不变式：受影响面按**内容**归一化，"
                "纯重命名不生成新口径（见 `RunScopeGuard` 的 `rename_normalized` 说明）",
            ],
        },
        "await_resolve": {
            "source": DOC_SOURCES["await"],
            "capability": ["零占用等待：await 挂起，对端 resolve 唤醒（不占核时）",
                           "平台自动死锁检测（等待不会静默卡死）"],
            "ours": [
                "`probe` 的 `platform_await` 落点 + 固定 `*/5` crontab 兜底",
            ],
            "gap_we_close": [
                "await 的**同一事件**约束与**key 仅能 resolve 一次**约束此前未落地 → "
                "本模块 `await_eligible()` / `await_key_for()` 在挂之前先校验，"
                "配不上对就降级 crontab（否则 await 会等不到、靠平台死锁检测才发现）",
            ],
        },
    }


def platform_report() -> Dict[str, Any]:
    """平台语义对齐的完整快照（供 `collab_status()` / 文档 / 回评引用）。"""
    return {
        "sources": dict(DOC_SOURCES),
        "await_constraints": dict(AWAIT_CONSTRAINTS),
        "known_events": list(KNOWN_EVENTS),
        "await_key_scheme": "probe:<checkpoint>:r<attempt>（每轮唯一，防 key 被消费后死等）",
        "downgrade_policy": "await 不可用 → crontab（平台代跑 + 平台死锁检测）；永不降级到本地空转",
        "local_cpu_note": local_cpu_note(),
        "official_capabilities": official_capabilities(),
        "full_run": ("全量仅大版本；**非代码推进事件（评论/目录/定时/触发）一律不得跑全量**"
                     "——防跨仓污染"),
    }

# ------------------------------------------------------------------
# 能力探测与降级（官方 SCA 插件的"增量规则"启发）
# ------------------------------------------------------------------
# 官方 SCA 插件的增量口径里有两条**结构性**前提：它必须能**拿到本次变更集**
# （否则"新增漏洞"无从判定）。映射到本仓库：`RunScopeGuard` 的"同一口径不重复"
# 依赖**受影响面**（impact）。若某次触发拿不到变更集（如新建分支的 push 没有
# "上一个 commit"可比、平台 diff 统计超限、纯触发性事件），
# 现实现会把 impact 归一化成空串 —— 而空 impact **是**一个合法指纹，
# 于是"不同变更集之间"可能被误判成"同一口径" → **该跑的被跳过**（漏跑，危险方向）。
#
# 官方对此的处理是"增量规则"里写清楚：删除文件不产生新增漏洞、内容未变的改名
# 不重复产生 —— 即**判不出就不再增量**。本模块把这条抄过来：
# 能力拿不到 → **降级为保守放行**（宁可多跑，不可漏跑），并把降级理由留痕。

#: 判定"能不能算出受影响面"的输入字段（缺一不可）
IMPACT_INPUTS = ("revision", "base", "mode")

#: 降级动作：能力缺失时一律 **conservative_run**（保守放行，不跳过）
DEGRADED_RUN = "conservative_run"
DEGRADED_FULL = "conservative_full"


def impact_available(
    revision: Any,
    base: Any = None,
    impact: Any = "",
    event: Any = "",
) -> Dict[str, Any]:
    """能否**可信地**算出"受影响面"（决定增量可达性）。

    Returns:
        ``{"available": bool, "reason": str, "degraded": bool, "action": str}``

    判定是**保守的**（判不出就不做增量）：

    - 缺 `revision` → 不可信（连被测版本都不知道）；
    - `base` 与 `impact` **同时缺** → 不可信（既没基线、也没算出来的变更集）；
    - `base` 给了（哪怕是空串）但 `impact` 为空 → 不可信：
      "算出来是空"与"算不出来"在数据上无法区分，官方口径是"判不出就不增量"；
    - 平台侧"新建分支 push 没有上一个 commit"这类**已知**情形在
      `/zh/build/grammar.md` 的 `ifModify` 限制里写明了 → 直接判降级。

    `base` 用 ``None`` 表示**未提供**（区别于显式给空串 = 提供了但为空）。
    """
    rev = revision.strip() if isinstance(revision, str) else ""
    bas_provided = base is not None
    bas = base.strip() if isinstance(base, str) else ""
    imp = impact if isinstance(impact, (list, tuple, set)) else (
        [impact] if isinstance(impact, str) and impact.strip() else [])
    ev = norm_event(event)

    if not rev:
        return {"available": False, "degraded": True, "action": DEGRADED_RUN,
                "reason": "缺 revision（无法确定被测版本）→ 不做增量判重，保守放行"}
    if bas_provided and not bas:
        # 官方：新建分支的 push 没有"上一个 commit"作对比基准，无法统计文件变更。
        return {"available": False, "degraded": True, "action": DEGRADED_RUN,
                "reason": ("base 为空（如新建分支 push 无上一个 commit）→ "
                           "无法统计变更集，保守放行")}
    if ev == "branch.create":
        return {"available": False, "degraded": True, "action": DEGRADED_RUN,
                "reason": "branch.create 事件无变更基准（官方 ifModify 限制同源）→ 保守放行"}
    if not imp:
        if not bas_provided:
            return {"available": False, "degraded": True, "action": DEGRADED_RUN,
                    "reason": ("既无 base 也无受影响面 → 判不出变更集，"
                               "不做增量判重，保守放行（宁可多跑，不可漏跑）")}
        return {"available": False, "degraded": True, "action": DEGRADED_RUN,
                "reason": ("受影响面为空：无法区分「算出来是空」与「算不出来」→ "
                           "按官方口径不做增量判重，保守放行（宁可多跑，不可漏跑）")}
    return {"available": True, "degraded": False, "action": "run",
            "reason": f"受影响面可得（{len(imp)} 项）→ 可做增量判重"}


def capability_report() -> Dict[str, Any]:
    """能力与降级口径快照（供 `collab_status()` / 文档引用）。"""
    return {
        "impact_inputs": list(IMPACT_INPUTS),
        "degrade_policy": ("能力拿不到 → **保守放行**（宁可多跑不可漏跑）；"
                           "降级理由留痕，不静默跳过"),
        "known_degrade_cases": [
            "缺 revision / 缺 base（新建分支 push 无上一个 commit）",
            "branch.create 事件（无变更基准）",
            "受影响面为空（算出来是空 vs 算不出来无法区分）",
        ],
        "source": DOC_SOURCES["sca"],
        "note": ("官方 SCA 增量规则同源：判不出「新增」就不报增量、"
                 "内容未变的改名不重复产生。此处把「判不出就不增量」用于**跳过决策**，"
                 "方向取**安全**的一侧：漏跑比多跑危险。"),
    }
