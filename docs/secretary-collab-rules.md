# agent 协同规则（全局 · 跨仓库）

> version: v0.3 · Ref: #14 追问（三点确认 + **全量门禁**）
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
| 锁 | `locks.retry_delay_sec` / `persistent_grace_sec` / `registry_shared` | 延迟首档 10s（**覆写即生效**，🔴D-1）；重触发转持续锁；所有锁进一本台账 |
| 循环熔断 | `loop.max_rounds`=3 / `max_advisory_fix_rounds`=1 / `max_total_rounds`=5 / `max_stall_repeats`=2 | §2.1 |
| 审查范围 | `scan.major_version_tags` / `full_scan_keywords` / `scan_keywords` | 日常只审变化，全量只在大版本 |
| 增量执行 | `scan.default_mode` / `max_identical_repeats` / `repeat_keys` / `allow_stability_repeats` / `stability_via` | §2.4（**同一口径只跑一次**；禁连跑当稳定性论证） |
| **全量门禁** | `scan.repeat_scope` / `blocked_events` / `pipeline_allowlist` | §2.5（**默认全量根本不跑**；去升级口径；事件侧禁跑；兜底白名单） |
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

### 2.4 增量执行：只跑变化，同一口径不重复

> 平时不需要跑全量，而且**尤其是**不适合连跑三次…连续跑三次，有问题也可能查不出来，
> 因为跑的方式是固定的。**同一个口径几乎不会产生分歧**。所以像这种连续跑三次全量是无效的。

两条**机制**（不是提醒；由 `RunScopeGuard` 判定，跳过不需要"自觉"）：

| 口径 | 数值 | 说明 |
|---|---|---|
| 日常档位 | `default_mode: scan` | **默认就是增量**（不需要有人记得选） |
| 同一口径上界 | `max_identical_repeats: 1` | 第 2 次起 `skip_duplicate` |
| 口径指纹字段 | `repeat_keys: [mode, revision, impact, command]` | **只有口径字段**进指纹（时间戳/执行人不得进） |
| 连跑当稳定性论证 | `allow_stability_repeats: false` | 显式想连跑 → `blocked_stability` |
| 稳定性替代路径 | `stability_via: [targeted_probe, mutation_probe, independent_scenario]` | 换口径，而不是重跑同一命令 |

判定优先级（`RunScopeGuard.decide`）：

1. **连跑**（`window_repeats > 1`）→ `blocked_stability`（**红/绿都拒**：
   "上轮红"只允许"重跑一次"，不等于"允许连跑三次"）；
2. 口径信息不全（缺版本/缺命令）→ `run`（**判不出就不拦**，不挡住干活）；
3. 同口径次数 < 上界 → `run`；
4. 达上界 + 上一轮**明确红** → `run`（红→修→再跑有新信息）；
5. 达上界 + 其余（含**没给结论**）→ `skip_duplicate`。

**"明确红"的判定必须排除计数词**（🟡2）：`"577 passed, 0 failed"` 是**绿**，
不是红 —— 旧实现按**子串**匹配，含 `failed` 就判红 → "允许重跑一次"的例外被
**恒真激活**（等于默认放行）。现口径先把 `0 failed` / `0 errors` 这类
**零个失败**整段抹掉再匹配关键词（`_ZERO_ISSUE_RE`）。口径覆盖四类写法
（🔴-4 复评第 5 轮：只认"数字 0 开头"时 `no errors` / `errors: 0` / `1 xfailed` /
`no failures` 仍判红 → "允许重跑一次"的例外被**恒真激活**）：

| 形态 | 例 |
|---|---|
| 前置零计数 | `0 failed` / `0.0 errors` / `0% failures` |
| 后置零计数 | `errors: 0` / `failures = 0` / `failed 0` |
| 显式否定 | `no errors` / `no failures` / `without failures` / `zero errors` |
| 预期失败 | `xfailed` / `xfail`（pytest 的**预期失败**不是不绿） |

#### 2.4.1 判重台账的**跨容器载体**（🔴F：与 🔴A 同一方案）

「同一份受审内容只跑一次」不能只落在 `<tmp>/run-scope/run-scope.json`：
平台每次 push = **新容器、新工作区**，台账每次从空开始 → 判重**永不触发**
（实测 r1/r2/r3 全 `run`）。这与 🔴A（熔断/探测预算）**同族**，只是 A 有
"写回评论"这个载体，F 连载体都没有。

| 环节 | 机制 |
|---|---|
| 留痕 | `RunScopeGuard.trail()/trails()` = `<!-- run-scope: {…} -->`，可贴进**评论正文** / 交接卡 `context`（容器会被回收，评论不会） |
| 合并 | `adopt_trails(payload)`：`max()` 只增不减；**按 `session_id` 归属过滤**（留痕不带该字段时不表态） |
| 回写 | CI 跑完把 `guard.trails(...)` **追加**写回同一条 PR/Issue 评论（保留旧留痕**与各自归属**） |
| 入口 | 跑前 `carrier.run_fetch()`（`scripts/collab-carrier.sh fetch`）/ 跑后 `carrier.run_writeback()`（`... writeback`）；门面 `run_scope_trail()` / `inherit_run_scope(payload)` |
| 归属 | 会话号 = `CNB_SESSION_ID` → `CNB_REPO_SLUG` + PR/Issue 号（同一 PR = 同一会话，跨容器稳定）；**推导不出 → 不写回** |
| 结论 | CI 必须落**真实结论**（`record_repeat(..., verdict=<rc 回填>)`）：不落结论 → "上一轮不绿才允许复跑"的例外永不成立 |

**载体必须有生产者**（🔴-3 复评第 5 轮）：`CNB_RUN_SCOPE_TRAIL` / `CNB_SESSION_ID`
此前**全库只有读**（`.cnb.yml` 的 `main.push` 段也没有 `env`/`imports`）→ 载体停在
"提示词要求"，且跑完写回会把评论里**所有会话**的归属整段重写成 `""`
（别的会话台账被继承 = **漏跑**；"红→修→再跑"被堵死）。现由
`scripts/collab-carrier.sh` 在 `main.push` 里接成**可执行的生产者**（fetch / writeback），
`trails()` 也**不得**改写未指定归属的记录（保留原 `session_id`）。

**红后可复跑**（🔴F）：内容键只由"受影响面 + 命令"构成，而**修 bug 常常改的就是
同一批文件**（受影响面不变）→ 若一律跳过，"红 → 修 → 再跑"这条**唯一有信息量**
的路径就被自己的判重堵死。故上一次该内容**明确不绿**时允许复跑——
**但复跑有上界**（`scan.max_rerun_on_failure`，默认 1 = 本节口径"一次"）：
无上界时连续"修了仍红"会全 `run`（实测 5 轮），例外把判重自己架空
（与 §2.4 判定优先级第 4 条同源）。

### 2.5 全量门禁：**默认全量根本不跑**（v0.3 新增）

> 这里我要求过不跑全量，他怎么还在一直连续跑三次稳定这种事情？…他不然我们新开发一步，
> 他就要跑一次 600 多次的验证。甚至还要跑三倍，这太过分了。…处理完这个让他合理起来，
> 我们再通用，**不然相当于一次污染了很多仓库**，那不更完蛋了。

**先回答"是我们新配置的那个规则现在有问题吗"**：规则本身没错，但**只做了一半**——
是"默认值"，不是"门禁"；而且判重的比较键**含 revision**：

| 上一轮的做法 | 为什么没拦住 | 本轮硬闸 |
|---|---|---|
| `scan.default_mode: scan` | 只是**默认值**：调用方显式给 `mode="full"`（"跑个全量回归验证一下"）就直接绕过 | `decide_full()`：`full` 必须过**三重条件**，否则 `blocked_full` |
| `RunScopeGuard` 判重 | 比较键含 **`revision`**：**每改一行代码 revision 就变** → 每次都算"新口径" → 每次都放行 | 去升级口径 `repeat_scope: content`：按**受审内容**判重（**跨版本**） |
| `.cnb.yml` 各事件共用一套 | 评论/目录/定时类事件**也**触发测试 → 没有基线、没有变更集，纯空烧 | `full_run_eligible()`：这些事件**一律不得跑全量** |

#### 三道硬闸

**① 全量三重条件**（`decide_full`，缺一不可）

| 条件 | 判定依据 | 说明 |
|---|---|---|
| **大版本事实** | `major_version=True` **或** 大版本标签 | `None`（不知道）→ **拦** |
| **可采信来源** | `action` / `tag` / `operator` | `agent_claim`（**agent 自述**）→ **拦**：否则"谁喊一声大版本就能解锁全量" |
| **代码推进类事件** | 不在 `blocked_events`（评论/目录/定时/触发） | 事件不可判定 → **拦** |

拦截时**必须给出可直接跑的增量命令**（`scan_command`）——拦了但给路，
避免"被拦住之后又绕回全量"。

**② 去升级口径**（`repeat_scope`，默认 `content`）

| 档 | 比较键 | 后果 |
|---|---|---|
| `content`（默认） | 范围 + **受影响面** + 命令 | 同一份变更集在**新版本**下重跑 → **判为重复 → 跳过** |
| `revision`（旧） | 范围 + **版本** + 受影响面 + 命令 | 改一行代码 = 新口径 → 每次放行（就是用户看到的现象） |

保留 `revision` 档是因为**方向性**：若某仓业务确实"每个提交都必须跑一次"，可以显式选它，
但**默认不这样**。

**③ 事件侧 + 单入口 + 兜底白名单**

- 评论/目录/定时/触发事件 → `.cnb.yml` 里走**秘书接待**任务（**不做测试**）；
- **唯一执行入口** `scripts/ci-dispatch.sh`：**不接收"全量"参数**，
  因此不存在"通过传参绕过门禁"的路径（也没有"拿输入拼命令"的注入面）；
- 流水线兜底变更只允许碰 `pipeline_allowlist`（默认 `shared/collab/`），
  绝对路径 / `..` 穿越 / 改源码一律拒——**兜底不是后门**。

#### ⚠️ 方向性：门禁与判重**方向相反**，不可混用

| 判定 | 判不出时 | 取的安全侧 |
|---|---|---|
| 跳过重复（`run_decision`） | **放行** | 宁可多跑，不可漏跑 |
| 全量门禁（`full_decision`） | **拦截** | 宁可少跑全量，不可空烧全量回归 |

两者混用过的后果，正是"每开发一步就跑一次 600 多项全量验证"。

#### 落地

- 代码：`collab/full_gate.py`（`FullGateDecision` / `decide_full` / `incremental_command`）、
  `collab/platform.py`（`full_run_eligible` / `pipeline_change_allowed`）、
  `collab/run_guard.py`（`decide_repeat` / `record_repeat` / `content_repeats_of`）
- 门面：`full_decision` / `full_run_eligible` / `pipeline_change_allowed` /
  `full_gate_note` / `decide_repeat` / `record_repeat` / `content_repeats_of`
- 执行入口：`scripts/ci-dispatch.sh` → `scripts/ci-incremental-test.sh`
- 规则：`shared/collab/*.yaml` 的 `scan.repeat_scope / blocked_events / pipeline_allowlist`

#### 端到端实测

| 调用 | 结果 |
|---|---|
| `./scripts/ci-dispatch.sh`（默认） | `run` → **601 passed**（只跑变更相关，非全量） |
| 再调一次（同受审内容） | `skip_duplicate` → **跳过** |
| `CNB_SCOPE=full`（无凭据） | `blocked_full` → **改跑增量** 601 passed |
| `CNB_SCOPE=full` + 大版本 + `operator` + `push` | 允许 → 684 passed（全量，大版本专用） |
| `CNB_WINDOW_REPEATS=3`（想连跑三次） | `blocked_stability` → **跳过** |
| 换受影响面（真新工作） | `run` → 正常放行 |

**"没给结论 ≠ 跑红了"**：这是实现中抓到的真缺陷——原用 `not _is_green(...)`
把"未知/空"也当成红，等于**默认放行**（台账里只要没结论，同口径就能无限重跑）。
现改为只认**明确的红**（`_is_red`）才走例外。

**配置侧同类缺陷（本轮根因之一）**：`_OVERRIDABLE_FIELDS` 是**白名单**，
新增数值字段若忘登记，仓库级覆写会被**静默丢弃** —— 表现为"规则文件里明明写了，
跑起来还是默认值"。现已加自检 `_audit_overridable_fields()`（漏登记即断言失败）。

### 2.5 规则包数值的**消费点**（"一处定义"必须真被读）

> 🔴D 修复：数值**被读**不等于**生效**。本轮补全两处消费点，并加回归用例。

| 数值 | 消费点 | 覆写效果 |
|---|---|---|
| `locks.retry_delay_sec` | `SecretarySession` 的锁窗口 / 评审延迟窗（`review_arbiter.retry_delay_sec`、`lock_registry.ttl_sec`） | 组织级/仓库级改 30 → 生效值即 30（`collab_status()["locks"]` 同步）；显式传参仍优先 |
| `probe.crontab_interval_minutes` | `cron_expr_for(rules)` **与** `dispatch_shared(event, rules)` | 改 15 → **本地与跨仓**同为 `*/15 * * * *`（旧实现跨仓硬编码 `*/5` → 同一份规则出两套动作） |
| `probe.await_max_sec` | `_driver_for_delay()`（本地/跨仓共用） | 改 120 → 180s 等待在**两侧**都判 `crontab` |

`collab_status()["locks"]` 额外给出 `rules_retry_delay_sec`（规则包原值）与
`explicit_override`（是否被显式传参覆写）——"生效值从哪来"一眼可见。

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
- **等锁（三态，🔴E）**：判别式 `lock` 读锁聚合台账快照 —— **查到且被持有 → `wait`**；
  **查到且未持有（= 已释放）→ `proceed`**；**台账里没有这把锁**（对方尚未登记 /
  跨仓不可见）→ `unknown`（继续等，**不误判为"已释放"**）。台账 `overview()` 给出
  **全量锁键**（`keys`，含已释放/已回收）——三态判定的数据前提；
  "没查到"与"已释放"必须可区分，否则"等他的锁"会误放行（最危险的方向）；
- **判断检查点**：只看可判定事实（`confirmed` / 锁快照 / 版本号 / 事件信号），
  **不看 agent 自述**；
- **反队头阻塞**：`our_need_satisfied=False` → 直接放锁、不再等。

平台依据（best practice）：`cnb:await`/`cnb:resolve`（零占用唤醒）、`crontab`（≥5min，
平台代跑）、`cnb:apply`/`cnb:trigger` 与 OPENAPI `StartBuild`（外部 webhook 再跑）。
详见 `docs/secretary-gate-workflow.md` §2.2.2。

### 4.1.1 探测预算的**跨容器继承**（🔴A：不归零）

「最多 6 次」不能只落在 `<数据根>/probe/*.json`：平台 `crontab` 代跑是
**新容器、新工作区**，磁盘上的 `checks_used` 从 0 开始 → 上界**永不触发**
（等于"软约束换了个位置"）。故预算随**载荷**走：

| 环节 | 机制 |
|---|---|
| 合并 | `probe(key, external_checks=…)` / `adopt_checks_budget()`：`max()` 只增不减，且在"是否用尽"判定**之前**合并 |
| 留痕 | `ProbeEvent.budget_trail` = `<!-- probe-budget: {…} -->`，可贴进**评论正文** / 交接卡 `context` / 事件载荷（容器会被回收，评论不会） |
| **取全部** | 正文里留痕会**累积多条**（每棒写回）→ 解析取**全部留痕的 `max()`**，**不是第一条**；"留痕在前/在后"两种顺序结果一致（🔴1） |
| **通道合并** | 留痕通道与载荷通道（`checks_used` / `external_checks` / `used` / `rounds` / 内嵌 `budget_trail`）**一起取 `max()`**——**任一通道都不得"命中即 return"**（🔴1 残留：`trail(2)+{"checks_used":5}` 曾读成 2，`budget_trail` 展开曾是死代码） |
| 回写 | `dispatch()` 返回值统一带 `budget_trail`；接收方**追加写回同一条评论**（口径见下），下一棒据此恢复 |
| 接线 | `.cnb.yml` 的 `probe_task` 明列三步：取上次留痕 → 合并 → 写回评论 |

**写回哪一条**（口径，防"多留痕叠加"再次踩坑）：**追加到本检查点的同一条评论**
（即"取留痕的那条"，不是每次新建一条）。追加时**保留旧留痕**——解析侧按
`max()` 合并，故历史留痕不影响正确性，且评论条数不随探测次数膨胀。
新建评论（如首轮还没有承载评论）时才新建一条，并在正文里标明检查点 KEY。

**边界（明示）**：调用方既不给载荷、又换数据根 → 预算只能从 0 开始。
这是平台执行模型的边界，不是"实现疏忽"；故上表"回写"一步是**必做项**。

**同族（熔断轮次）**：`loop-budget` 留痕同样**取全部、`max()` 合并**，
且交接卡 `context` 内嵌的 `budget_trail` 字段会**递归展开后合并**（不是替换）。

同族口径见 §2.1（熔断轮次）：`LoopGuard.budget_payload()` / `budget_trail()` /
`adopt_budget()`，门面入口 `review_round(external_rounds=…)` /
`inherit_budget(…)` / `loop_budget_trail()`。解析侧口径函数
`loop_guard.extract_json_objects()` / `pick_wake_object()`：

- **留痕与载荷必须合并**（🔴1 残留）：正文里"留痕段 + 不带内嵌留痕的卡"是
  常见形态，故留痕解析**不得**在"读到 `rounds` 就 return"——两条通道
  一律 `max()` 互补合并（`fused` / `budget` / `reason` 同）；
- **静默判定保守且确定**（🔴2）：`pick_wake_object()` 不再"首个含 `wake` 者胜"
  （那会让**卡+卡顺序决定结论**，一个方向是 fail-open）。现口径：
  **任一对象的 `wake` 非严格 `True` → 判静默**；需要具体对象时取
  **追加顺序里最后一张卡**（正文是追加写的 → 后者更晚）；
- **正文扫描：构造性"不漏"**（🔴5 → 复评第 5 轮**改实现路径**）：
  `extract_json_objects()` 走 `secretary.trail.scan_json_spans()`——
  **每个候选 `{` 独立起算** `json.JSONDecoder().raw_decode`（等价旧实现
  "每个起点重扫"的语义，解析在 C 里做，未闭合/非法即 fail-fast），
  外加 O(1) 预检（剩余正文必须还有 `}`；`{` 后跳过空白必须是 `"` 或 `}`）。
  故"前面有未闭合引号/花括号"**不可能**把后面的静默卡整段丢掉。
  旧的"一次线性主扫描 + 有界修复"路径**已废弃**：其正确性依赖预算兜底，
  实测在兜底路径上产出两条 **fail-open**（引号区间前缀 + 卡 88 B；
  >60 KB 长正文 + 卡 → 卡被丢 → `fused=False` / `wake=True`）。
- **不变量 = 超集召回（前提 `degraded=False`）**：基线 `a7847f9` 能捞到的、
  且长度 **≤ `MAX_RESCAN_LEN`** 的对象，**在本次扫描未降级时**新实现
  **必须全捞到**（差分 fuzz 判据 `LOST=0` **且** `REV=0`）。
  **降级（`degraded=True`）下允许丢段**（**已批准语义**，见 R4 降级 fuzz）：
  反例（可复跑）——14 B 的 `{"wake":false}` 接在 400 000 个 `{}` 之后 →
  `reason="work_cap"`、卡被丢（基线能捞到）。故判据**必须带前提**，不写无前提的"必全捞到"。
- **两条上界（归因：① = 冗余兜底 / ② = 承载上界 · 非"缺一不可" · 总师裁定 A⊕B）**：
  - ① **每候选** `MAX_RESCAN_LEN`（默认 `262_144`）：对候选 `p` 只在窗口
    `[p, p + MAX_RESCAN_LEN)` 里找 `}`；窗口内没有 → 该候选**不表态**
    （跳过）且 `reason="rescan_len"`。
    **归因（B 落地 · 消融实测）**：本常量在当前记账口径下是**冗余兜底**，
    **不是**"缺一不可"。消融（`adv320 = '{"k":1, '×40000 + '}'`）：
    `both` **0.0034 s** / `MAX_RESCAN_LEN→1e9` **0.0062 s**（仍被 ② 拦住）/
    `MAX_SCAN_WORK→1e18` **2.8619 s**（② 去掉才失控）；`micro = "{}"×500 000`：
    0.3950 / 0.3839 / 1.0078 s。⇒ **承载上界是 ②**。
    语义判据（**与实现同源**）：**「候选 `p` 到窗口内首个 `}` 的距离 >
    `MAX_RESCAN_LEN`」**——**不是**"长度超上界的合法对象会被跳过"（反例：
    含内层 `}` 的 **5.2 MB** 合法对象**未被跳过**）。
    取值依据：全仓语料里"候选 → 最近 `}` 距离"最大 **5 289**
    （`state-secretary/src/secretary/loop/loop_guard.py`；台账 ×6 = 5 273），
    本上界 **≈49.6×** 余量。
  - ② **总工作量** `MAX_SCAN_WORK`（默认 `200_000_000` 折算单位）：记账 =
    候选扫描区间长度 + **每候选固定开销折算**（`_SCAN_CANDIDATE_COST`，实测
    每次迭代 0.4–0.9 µs、与长度无关）+ `raw_decode` 实扫量（成功 `end - p`；
    **失败取错误位置 `pos`**，**不是** `pos - p`——异常消息构造代价随 `pos`
    线性，取 `pos - p`（恒 8）会让总上界**永不触顶**）+ 一次基扫（`limit`）。
    触界 → `reason="work_cap"`。
    ⚠️ **该口径已被证伪（2026-09-19，待总师裁定修法）**：`raw_decode` 拿到的是
    **整段 `text`**（不是 `text[:limit]`）⇒ 未闭合字符串分支的 `pos` 停在开引号、
    记账 ≈ 常数，而**实扫随正文线性增长** ⇒ 记账与耗时**脱钩**，
    `MAX_SCAN_WORK` **不是耗时上界**，折算比**无界**（实测 3.3 / 32.7 / 130.9
    ns/单位 @ 5 / 50 / 200 MB，最坏 ≥ **272 ns/单位**）。
    同一机制下 `MAX_RESCAN_LEN` 也**不约束被解析对象长度**（串内含 `}` 的
    786 442 字符对象 > 上界 262 144，仍完整返回且 `degraded=False`）。
    **下侧余量亦只在台账样本成立**：台账 ×5（491 655 字符）work = **39 493 168**
    ⇒ **5.06×**；但把**每个真实文件**重复到 417 480 字符后，**84 个文件里 19 个
    余量 < 5×**，最差 **1.00×**。修法（已上报总师）：`raw_decode` 只喂
    `[p, min(limit, p + MAX_RESCAN_LEN))` 窗口 + 失败按窗口长度记账 +
    `budget` 兼作返回边界，修后两侧重标定。
- **上界只降级、不丢段**（且**显式标记 + 三态原因**）：两条上界触界或正文被
  `DEFAULT_SCAN_BUDGET` 截断 → **保留已取到的片段**，把"未扫到的区间"判为
  "不表态"，并显式给出 `degraded=True` + `reason ∈ {truncated, work_cap,
  rescan_len}`（`*_diag()`；`*_report()` 保留二元组签名）。判定侧据此把安全旗标
  （`fused` / `wake`）取**保守方向**。**禁止**再出现"静默收摊"（上一轮
  `MAX_REPAIR_CHARS` 触顶即整段丢卡，正是 N1/N2 的来源）。
- **消费侧 3 处接线（Issue #21 G5）**：`from_handoff`（降级 → 保守静默，
  原因写进 `fused_reason`）/ `adopt_budget`（降级正文**不得**当空操作，否则
  卡静默丢）/ `probe._coerce_checks_used`（降级 → 保守按 `max_checks` 继承，
  不再自动挂账）。三处都走 `*_diag` 入口拿 `reason`，**不得**只标记不接线。
- **`budget <= 0` 与截断是同一规则（Issue #21 G8）**：`limit < len(text)` 即
  `truncated` —— `limit = 0` 时该式**自然成立**，故 `..._diag()` **不得**再为
  `budget <= 0` 另设"提前返回"分支（旧实现在 `degraded = limit < len(text)`
  **之前** `return out, False` ⇒ 非空正文被整段截断却**不标记**，实测
  `scan_json_spans_report(text, 0) → ([], False)`）。降级**只有一个判定处**
  （`_scan_diag` 那一行），这是"单一语义源"在降级上的落地。
  反向对照：**空正文**（`""`）任何 `budget` 都**不**降级（没有内容可表态）。
- **≥ 417 K 真实正文**反误降级对照（总师 `[HIGH]`）：`cost = pos` 单独落地时，
  台账 ×5（**417 480 字符**）即触旧上界、卡被吞——且落在**非对抗正文**上，
  "修记账"于是把 CP1 变成**新的 fail-open 通道**。故判据必须**同时**有两条：
  ① 含 `}` 对抗输入 **< 1 s**；② **真实正文 ≥ 417 K 不降级**（台账 ×1…×8 全绿、
  静默卡可见、`fused=True` / `wake=False`）。
- **⚠️ 安全断言与实现同源**（复评第 5 轮总师点名 `[HIGH]`）：本仓库**不得**
  再出现"记账 ≥ 真实扫描量，故本上界同时是**真实耗时**的上界"这类失真断言
  （实测不成立）。**本条已被复发证伪**（2026-09-19）：曾把"3 个构造里测得的最坏值"
  写成"实测上界 2.0 ns/单位"，而真实最坏 **≥ 272 ns/单位且无界** ⇒ 该写法**同样
  是失真断言**。现口径：**只写实测值与测量形态，并明确声明"不是上界"**；
  `MAX_SCAN_WORK` 是否构成耗时上界**待修法落地后重测**。
- **`budget` 段只作观测**（🟡b）：合并结果**无采纳路径**——外部载荷的
  `max_rounds` 一旦被采纳就是 `max()` **抬高上界 = 削弱熔断**（同属 fail-open），
  且归属可缺省、不可信正文可借此放大预算。消费侧只读 `rounds` / `fused`。

**await 片的键名映射**（🟡2 收口）：≤5min 落点的 `trigger.env` 显式给出
`CHECKPOINT_AWAIT_KEY`（= 本次轮次 key `probe:<key>:r<n>`，与 `.cnb.yml` 的
`cnb:await.options.key: $CHECKPOINT_AWAIT_KEY` **同名同源**）与 `CHECKPOINT_KEY`；
即"平台侧按 key 注入"这条不再只是事件名层面的口径——**键名映射写在落点里**，
await 片读到的 key 与本次探测生成的 key 是同一个（`trigger.key_env` 标注变量名）。

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
| `review_round(..., external_rounds=…)` | 登记一轮并**合并外部轮次预算**（跨容器/跨棒不归零） |
| `loop_budget_trail()` / `inherit_budget(payload)` | 轮次预算的**评论留痕** / 从留痕**恢复**轮次（🔴A） |
| `probe_checkpoint(key, external_checks=…)` | 探测一次并**合并外部探测轮次**（同上） |
| `create_checkpoint(..., waiter_event=…, resolver_event=…)` | 建检查点并**透传两侧事件名**（🔴G：不透传 → 落点恒降级 `crontab`，"≤5min 挂 await"生产不可达）；未显式给出时按当前事件（`CNB_EVENT`）自动登记 |
| `register_platform_wait(..., waiter_event=…, resolver_event=…)` | 登记"已挂平台语义"并透传事件（同上） |
| `run_scope_trail()` / `inherit_run_scope(payload)` | 判重台账的**评论留痕** / 从留痕**恢复**（🔴F，与 🔴A 同一载体方案） |

## 6. 覆写与校验

- **覆写数值**：设置环境变量 `CNB_COLLAB_RULES=/path/to/collab-rules.yaml`，
  或按 `load_rules(path)` 显式装载；仓库内差异**只改数值**。
- **非法阶梯会被拒绝**：出现回退档位（如 `[60, 30]`）直接 `ValueError`——
  "等待被缩短"在入口就挡掉，而不是等到运行时才发现承诺被回退。
- **规则包缺失/损坏**：一律回落内置默认（不抛、不静默乱读）。

## 7. 平台语义对齐（v0.3 · Issue #14 本轮）

> 检查一下这个功能是否正常了…另外这个功能可能也有其他用户开发了，可以检索一下有没有。
> 有可能开发的比我们处理的要好，或者**官方有没有对此有最佳实践可以参考过来**。
> 帮助我们把这个功能开发到完善，然后**分配到组织下，实现各个仓库的通用**。

### 7.1 查证结果：官方有最佳实践，且**推翻了我们两处设计**

方向（事件驱动 + 零占用挂起）是对的，但**抽象的"平台语义"≠ 具体任务的真实约束**。
逐条查证官方文档后，`cnb:await` 有两条**会让落点失效**的硬约束：

| 官方约束（原文） | 原实现的问题 | 收口 |
|---|---|---|
| 「只能对**同一个事件**触发的 pipeline 进行 await 和 resolve 操作」 | 探测/干活 pipeline 常由 `crontab`/`api_trigger` 触发，而放锁方多在 `push`/评论事件 → **配不上对，await 永远等不到** | `await_eligible()`：不同事件/不可判定 → **降级 `crontab`**（平台代跑，**且平台会做死锁检测**） |
| 「一个 `key` **仅能 resolve 一次**（可 await 多次）」 | 同一检查点第 4、5 次探测复用同一 key → 第 2 次起**必死等** | `await_key_for(ck, attempt)`：key 带**轮次**，每轮唯一 |
| 「await-resolve 任务只能运行在流水线的 `stages` 中」（`endStages`/`failStages` 内不生效） | 收尾/失败阶段挂 await 会**直接不生效** | `await_stage_ok()`：位置校验 |

出处：`/zh/build/internal-steps/cnb/await-resolve.md`。

**降级不等于丢功能**：`crontab` 仍是**平台**在等（平台是那个不睡觉的进程），
本地 CPU 占用恒为 **0**；两处落点都**不允许**本地 sleep/轮询。

代码口径：`secretary/collab/platform.py`
（`await_eligible` / `await_key_for` / `resolve_driver` / `platform_report`）。

### 7.2 官方最佳实践对照（"别人/官方怎么做的"）

| 能力 | 官方已有（出处） | 我们已有 | 本轮补的缺口 |
|---|---|---|---|
| PR 代码评审 | `cnbcool/code-review`：**diff 增量**、过滤非代码文件、`include_existing_comments` 拉历史评论**避免重复**、`min_severity`/`min_confidence`/`exclude_categories` 降噪、`max_comments` 截断、`auto` 评审事件 | `decide_scan()` 日常只审变化；`begin_review()` 只在开启评审时记版本；`RunScopeGuard` 同口径只跑一次 | **确定性**去重：官方把"别重复刷评论"交给**模型判断**；我们补 `review_fingerprint`（类别 + 语义锚点，**不含路径/行号**）→ 同一问题不报第二遍，重复项**结转** |
| 增量扫描 | SCA 插件：「同一组件同一 CVE **不重复阻断**」「**内容未变化的文件重命名不重复产生**」 | 同口径指纹（范围+版本+受影响面+命令） | 把"改名不产生新工作"升级为通用不变式（锚点不含路径）；并补**能力探测降级** |
| 零占用等待 | `cnb:await` / `cnb:resolve`（挂起不占核时）+ 平台**死锁检测** | `probe` 的 `platform_await` 落点 + 固定 `*/5` crontab 兜底 | 挂之前先过**同一事件**与 **key 一次性**约束（否则等不到才发现） |

**结论（回"有没有比我们做得更好的"）**：官方在**评审内容层**做得更成熟
（评审插件的降噪/去重/事件表态），我们在**协同调度层**做得更细（多仓一份规则、
熔断预算、探测预算、能力降级）。两者是**不同层**，故本轮取"官方做对的点"补齐
我们的确定性去重与能力降级，而不是重复实现一个评审插件。

### 7.3 能力探测与降级（判不出就不增量）

`RunScopeGuard` 的"同一口径不重复"依赖**受影响面**。若某次触发**拿不到变更集**
（新建分支 push 没有上一个 commit / 受影响面为空），空 impact **本身是一个合法指纹**
→ 不同变更集可能撞成同一个 → **该跑的被跳过**（漏跑，方向危险）。

按官方 SCA 增量口径（"判不出新增就不报增量"）改为**保守放行**：

| 情形 | 判定 |
|---|---|
| 有版本 + 有具体受影响面 | 正常增量判重 |
| 缺 `revision`（不知道被测版本） | **降级放行**（`conservative_run`）+ 留痕 |
| `base` 给空 / `branch.create`（无变更基准） | **降级放行** |
| 有 base 但受影响面为空（算出来是空 vs 算不出来无法区分） | **降级放行** |

口径：**漏跑比多跑危险**，故一律取安全一侧。代码：`platform.impact_available()`。

### 7.4 分发到组织（各仓库通用）

**分发的是内容，不是文档**：

| 分发物 | 作用 | 说明 |
|---|---|---|
| `shared/collab/organization.yaml` | **组织级语义基线（全组织一份）** | 各仓只需有这一份，`load_rules()` 从任意子目录向上都能取到**同一份** |
| `shared/collab/collab-rules.yaml` | 仓库级**数值覆写**模板 | **已存在则不覆盖**（那是该仓的数值差异，不该被分发抹掉） |
| `scripts/distribute-collab-rules.sh` | 分发**并校验** | 默认 dry-run；`--apply` 才写；铺完当轮可校验 |

```bash
# 先看会动哪些仓（安全默认）
scripts/distribute-collab-rules.sh --group dongdongxiaozu

# 真正写入（每仓建分支，不直推 main）
scripts/distribute-collab-rules.sh --group dongdongxiaozu --apply
```

**为什么是脚本 + 校验**：口径漂移的根因从来不是"没人写文档"，而是"每仓各写一套、
写完没人验"。分发必须**可执行**（铺得下去）且**可校验**（不一致会报出来）。

### 7.5 本轮新增门面

| 方法 | 作用 |
|---|---|
| `platform_report()` | 平台语义对齐快照（await 硬约束 / 官方能力对照 / 降级策略 + 出处） |
| `await_eligibility(waiter_event, resolver_event, stage)` | 只回答"能不能挂 await" |
| `await_plan(key)` | 按检查点上下文给**校验后**的落点（配不上对 → crontab） |
| `await_key_of(ck, attempt)` | 按轮次生成 await 配对键 |
| `capability_report()` / `impact_availability(...)` | 能力与降级口径 / 能不能可信地算受影响面 |
| `review_dedup(items, round_no)` | 一轮评审**去重**（新问题→回转；老问题→结转） |
| `carry_review_items(keys)` / `review_fingerprints_overview()` | 结转留痕 / 台账总览 |
