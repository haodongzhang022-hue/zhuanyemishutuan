# agent 协同规则（全局 · 跨仓库）

> version: v0.2 · Ref: #14 追问（三点确认）
> 载体：`shared/collab/collab-rules.yaml`（规则包）＋ `state-secretary/src/secretary/collab/`（解析执行）
> 定位：**agent ↔ agent 的协同礼仪**，与具体仓库业务无关 → **一处定义、全域生效**

## 1. 为什么要"全局"

「等多久」「最多往返几轮」「什么算大版本才全量」这类参数**不属于任何单个仓库**。
放在单仓里定义，结果是：

| 单仓定义的问题 | 后果 |
|---|---|
| 每仓各写一套数值 | 数值漂移：换仓/换 agent 行为不一致 |
| 数值散落在角色提示词里 | 不可执行、不可校验、改名即失联 |
| 没有权威来源 | 评审各报一个数 → 争执 → 往返（正是要治的循环） |

所以整包收敛为 **`shared/collab/collab-rules.yaml`**：跨仓库共用一份，
代码从任意子目录向上查找都能拿到**同一份**；各仓库**只允许覆写数值，不允许改语义**。

## 2. 规则包内容

| 域 | 参数 | 口径 |
|---|---|---|
| 等待/退避 | `backoff.ladder` = `[10, 30, 60, 180, 360, 600]`、`max_attempts` | 「从少到多，持续增加到 10 分钟就停止」（§3） |
| 锁 | `locks.retry_delay_sec` / `persistent_grace_sec` / `registry_shared` | 延迟首档 10s；重触发转持续锁；所有锁进一本台账 |
| 循环熔断 | `loop.max_rounds`=3 / `max_advisory_fix_rounds`=1 / `max_total_rounds`=5 / `max_stall_repeats`=2 | §2.1 |
| 审查范围 | `scan.major_version_tags` / `full_scan_keywords` / `scan_keywords` | 日常只审变化，全量只在大版本 |
| 探测 | `probe.*` | §4.1（非轮询检查点、固定 `*/5`、落点分工） |
| 计费口径 | `platform.*` | §4（等待不计 CPU） |

**装载层级**（**已确认口径 #3**，先组织后仓库，都找不到则内置默认）：

| 层级 | 载体 | 权限 |
|---|---|---|
| **组织级**（跨仓库一份） | `shared/collab/organization.yaml` / `CNB_ORG_COLLAB_RULES` / `CNB_ORG_SHARED_DIR` | 定义**基线与语义** |
| 仓库级 | `shared/collab/collab-rules.yaml` / `CNB_COLLAB_RULES` | **只覆写数值**，语义不可改 |
| 内置默认 | 代码兜底 | 不抛、不静默乱读 |

`load_rules()` 返回的 `scope` 标明生效层级、`chain` 留痕"哪一级给了哪些域"——
一眼看出"这套数值是全组织共用同一份，还是被本仓覆写过"。

> 显式传参（如 `SecretarySession(retry_delay_sec=…)`）**优先于**规则包——
> 调用方有明确口径时以调用方为准；不传才取规则包。

## 3. 等待阶梯：从少到多，到顶即停

> 从少到多符合逻辑，因为**大部分都是很快就没问题了**，所以要不第一次的设计到
> **10 秒**，然后 **30 秒、60 秒、3 分钟、6 分钟**，持续增加到 **10 分钟就停止**。

| 第几次检测 | 1 | 2 | 3 | 4 | 5 | 6 及以后 |
|---|---|---|---|---|---|---|
| 等待 | **10s** | 30s | 60s | 3min | 6min | **10min（顶，停）** |

四条硬约束（都由 `StepBackoff` 保证，测试逐条判别）：

1. **从少到多**：阶梯是**定值**，不是倍率推算（倍率 ×2/×3 一步就跨过 10 分钟，落不到这串数字上）；
2. **大部分很快就没问题**：前两档 ≤30s —— 多数冲突在第 1 次检测就消失，
   **不替少数长尾付等待成本**；
3. **10 分钟就停止**：到顶后**不再增长**（`stopped=True`），且到顶次数用尽后
   转 `resume_required`——**停止自动挂账，交回人工/上游**；
4. **不缩短承诺**：重触发**只增不减**——已承诺的长等待绝不能被下一轮改回 10 秒。

> 到顶后**不会**把检测点无限往后推（那样"到顶"就成了无限等待）：
> 顶档只重复有限次（`max_attempts`，默认 6），然后把主动权交回人。

## 4. 等待会不会占用构建 CPU？（重要）

> 另外这个等待会不会占用 **CPU 构建 CPU 计算时间**呀，平台会根据这个收费，
> 如果一个 agent 是这样长时间等待会不会消耗，还是**被挂起**的等待？

**答：等待本身不计 CPU；被计费的是「任务实际运行时长」与「模型 Token」，不是等待。**

平台计费项（据 CNB 官方文档 [定价](/zh/pricing.md)）：

| 计费项 | 口径 |
|---|---|
| 云原生构建-CPU | 按任务**实际运行时长**，每 5min 冻结 `5min × 节点规格`（免费额度 160 核时/月） |
| AI Credits | 按**模型调用的 Token** 消耗（NPC / AI 评审 / 内置 CodeBuddy 等） |

**没有「等待」这一计费项。** 但有个前提：**等待必须走平台的等待/挂起语义**。
- 走平台等待（如内置任务 [`cnb:await`](/zh/build/internal-steps/cnb/await-resolve.md)）→ 等待期间**不产生 CPU 核时**；
- 反过来，**本地 `sleep` / 空转轮询**是在烧核时：8 核空转 10 分钟 ≈ **1.33 核时（约 ¥0.17）**。

> 平台文档佐证：`npc:go` 任务的 `timeout` 作用于**每一次工具执行**，
> **Agent 会话本身不设整体超时**，整体时长受流水线超时（20h）约束。
> 也就是说"等待"是平台层面的事，本地没有理由自己 `sleep`。

因此本策略把"怎么等"固化成三种模式，**禁止第四种（本地循环）**：

| 模式 | 含义 | 本地 CPU |
|---|---|---|
| `platform_suspend` | 挂到平台等待语义上，到点再检测 | **0（不占构建核时）** |
| `deferred_review` | 「先做其他，并挂锁」：不等，去干别的 | 0（在干真活） |
| `resume_required` | 阶梯到顶且用尽 → 交回人工/上游，不再自动挂账 | 0 |

**这也解释了为什么"阶梯到顶即停"是安全的**：到顶不等于"再等 10 分钟 ×N 次"，
而是**停止自动挂账**，把主动权交回人——既不烧 CPU，也不拖着一个永不结束的会话。

代码口径：`WaitPolicy.explain()` 直接给出上述结论（所有角色共用一段话，避免各说一套）。

## 4.1 探测器（probe）：非轮询的检查点

`probe` 段固化"最多 6 次 + 非轮询 + 等锁 + 判断检查点"的全局口径：

```yaml
probe:
  max_checks: 6                 # = 等待阶梯档位数；用尽即停止自动挂账
  drivers: [platform_await, platform_crontab, external_webhook,
            delegate_event, handoff_resume]
  await_max_sec: 300            # ≤5min 挂 cnb:await（零占用，状态一变即醒）
  crontab_min_interval_sec: 300 # 平台定时任务最小间隔（官方文档下限）
  checks: [confirm, lock, version, event, resource]
```

- **落点全是平台语义**，没有"本地 sleep / 空转轮询"这一种——那条路才按 CPU 核时计费；
- **等锁**：判别式 `lock` 读锁聚合台账快照；锁表不可见 → `unknown`（继续等，不误判）；
- **判断检查点**：只看可判定事实（`confirmed` / 锁快照 / 版本号 / 事件信号），
  **不看 agent 自述**；
- **反队头阻塞**：`our_need_satisfied=False` → 直接放锁、不再等。

平台依据（best practice）：`cnb:await`/`cnb:resolve`（零占用唤醒）、`crontab`（≥5min，
平台代跑）、`cnb:apply`/`cnb:trigger` 与 OPENAPI `StartBuild`（外部 webhook 再跑）。
详见 `docs/secretary-gate-workflow.md` §2.2.2。

**接线**：长等待由 `.cnb.yml` 顶层的 `crontab` 配置真正接到平台
（`cron: "*/5 * * * *"` + `event: api_trigger_probe_check`；平台约束：仅支持明确
单一分支，故 `branch: main`）。**固定 `*/5` 一档扫**，与阶梯时间表解耦。

### 4.2 三点确认（v0.2 · 已固化）

| # | 确认项 | 固化口径 |
|---|---|---|
| 1 | 落点分工「**≤5min 挂 await，>5min 交 crontab**」 | **硬口径**：按 `probe.await_max_sec`（默认 300s）自动分工；**不存在「一律挂 await」的回退路径**——长等待挂单个 await 就是"把到点必看押在对端一定会 resolve 上"，对端不 resolve 即死等。两个落点**互斥不混用**（await 行不带 cron，crontab 行必带） |
| 2 | `cron` 间隔「**固定 `*/5` 一档扫**」 | 所有平台定时探测点统一 `*/5 * * * *`，**不随阶梯档位放大**（旧口径 `*/6`、`*/10` 作废）。与阶梯时间表**解耦**：阶梯回答"我们打算什么时候再看"，cron 只回答"平台多久代跑一次"——否则换阶梯就改了平台的代跑节奏 |
| 3 | `probe` 规则「**推到组织级共享**」 | 装载层级 **组织级 → 仓库级（只覆写数值）→ 内置默认**；仓库级改不动语义（落点成员资格、判别式清单）；跨仓事件按**判定字符串**走同一份规则包出同一套动作 |

**一句话**：**短等待挂 `await`、长等待交 `crontab`；`crontab` 固定 `*/5` 一档扫；
这套口径放组织级、全组织共用一份，仓库只能改数值。**

代码口径：`probe.TWO_WAY_SPLIT_NOTE` / `probe.CRON_FIXED_EXPR` /
`organization_probe_rules()`（组织级快照）/ `dispatch_shared()`（跨仓落点判定）。

## 5. 怎么用

```python
from secretary.collab import load_rules, WaitPolicy

rules = load_rules()                     # 从任意子目录向上找规则包
rules.waiting_ladder()                   # [10, 30, 60, 180, 360, 600]
rules.waiting_plan()                     # 一行人类可读计划
rules.platform.waiting_billed            # False：等待不计 CPU

wp = WaitPolicy(rules.backoff, rules.platform)
wp.decide(0).to_dict()                   # 第 1 次该怎么等（platform_suspend, 10s）
wp.decide(5).mode                        # 到顶用尽 → 'resume_required'
wp.explain()                             # "等待是否占 CPU"的完整口径
```

会话门面（`SecretarySession`）：

| 方法 | 作用 |
|---|---|
| `collab_status()` | 生效快照：数值从哪来、等待计划、锁/熔断/审查范围、计费口径 |
| `waiting_plan()` / `waiting_status(n)` | 一行等待计划 / 当前位置的等待状态（含到顶） |
| `wait_decision(attempt)` / `wait_plan()` | "怎么等"的决策 / 整条等待计划 |
| `cost_note()` | 「等待是否占 CPU」的平台口径原话 |
| `probe_status()` | 探测口径快照（含分工阈值、固定 cron、规则来源层级 `chain`） |
| `organization_probe_rules()` | **组织级共享**口径快照（跨仓同一份） |
| `dispatch_probe_event_shared()` | **跨仓**落点判定（同一份规则 → 同一套动作） |

## 6. 覆写与校验

- **覆写数值**：设置环境变量 `CNB_COLLAB_RULES=/path/to/collab-rules.yaml`，
  或按 `load_rules(path)` 显式装载；仓库内差异**只改数值**。
- **非法阶梯会被拒绝**：出现回退档位（如 `[60, 30]`）直接 `ValueError`——
  "等待被缩短"在入口就挡掉，而不是等到运行时才发现承诺被回退。
- **规则包缺失/损坏**：一律回落内置默认（不抛、不静默乱读）。
