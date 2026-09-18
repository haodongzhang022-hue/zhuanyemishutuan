# 设计：检查点探测器（CheckpointProbe）

> Ref: #14 追问 · 版本 v0.1 · 模块 `state-secretary/src/secretary/collab/probe.py`

## 1. 需求

> 最多 6 次就行，再多就不适合等待了，不是这里的问题了，或者我们是否可以以非轮询
> 而是形成检查点然后 webhook 触发再跑，等他的锁，然后形成一个机制判断检查点，
> 类似于边缘计算的探测器这样的，会不会好点。怎么处理比较好，平台对这个有最佳实践吗。

## 2. 拆解：一句话一个机制

| 用户的话 | 机制 | 实现 |
|---|---|---|
| 最多 6 次就行 | **探测预算** = 阶梯档位数；用尽即停 | `Checkpoint.max_checks` / `used_up()` |
| 非轮询 | 探测点**不自己等**，落点全是平台语义 | `_driver_for_delay()` / `dispatch()` |
| 形成检查点 | 登记"等什么、等谁、还能探几次"（**幂等**） | `create()/load()/list_open()` |
| webhook 触发再跑 | `cnb:apply` / `cnb:trigger` / OPENAPI `StartBuild` → `api_trigger_*` | `dispatch()` 的 `trigger` 指令 |
| 等他的锁 | 判别式 `lock`：读**锁聚合台账**快照 | `_collect_lock_facts()` |
| 判断检查点 | 判别式（confirm/lock/version/event/resource）→ 三态 + unknown | `_judge()` |
| 边缘计算的探测器 | 本地**轻量采集 + 判定**，产出结构化事件；重活只在 `proceed` 时做 | `ProbeEvent` |

## 3. 结构

```
CheckpointProbe（注册表：纯计算 + 平台落点编排，永不 sleep）
├── create / load / list_open / delete_state      # 检查点生命周期（幂等，落盘）
├── delay_plan()                                  # 六次怎么等（阶梯 + 平台落点 + cron）
├── register_platform_wait() / resolve()          # 挂 cnb:await；对端 resolve 唤醒
├── probe() → ProbeOutcome{verdict, event, cp}    # 采集 → 判定 → 记预算 → 下一步
│   ├── _collect()          # 只留可判定事实（不含自述）
│   ├── _collect_lock_facts() / _collect_version_fact()   # 轻量采集
│   └── _judge()            # 判定式（确定性顺序）
└── dispatch(event)         # 结构化事件 → 平台指令（await/crontab/apply/人工）
```

## 4. 关键设计决策

### 4.1 判定顺序：先"我方是否还需要"

`our_need_satisfied=False` → 立即 `release`（放锁、不再等）。理由：**队头阻塞**里
最贵的一种就是"对方放不放无所谓了，但我们还在占着检查点等"。这条优先级高于所有判别式。

### 4.2 `unknown` 不等于 `proceed`

判不出（锁表没接、没给确认事实、首次无版本基线）一律 `unknown`，处置是**继续等**。
反例（必须避免）：把"看不见锁"当成"锁已释放" → 抢写 → 真冲突。

### 4.3 版本判别取**探测前快照**

`prev = cp.last_facts` 必须在**写入本次采集结果之前**取。否则 `prev == cur` 恒成立，
"版本变了"永远判不出（本批实测抓到的缺陷，已加 `TestJudge` 反向对照护栏）。

### 4.4 落点选择：短等待挂 await，长等待交 crontab

- ≤ `await_max_sec`（300s）→ `cnb:await`：**零占用**，状态一变由对端 `resolve` 唤醒；
- > 300s → `crontab`：平台定时任务到点代跑一次探测（**平台最小间隔 5 分钟**，
  低于该值配置无法提交，故 cron 间隔**向上取整且不低于 5 分钟**）。

### 4.5 预算幂等

同键重建**不重置** `checks_used`。否则"每次遇到都重建检查点"就把"最多 6 次"
变成无限次——正是 Issue #14 要治的循环，不能在新机制里复现。

### 4.6 唤醒闸门

`ProbeEvent.wake_allowed` 只在 `proceed` 且非预算用尽时为 `True`。
"探测"本身不得变成新的循环源：等待中/用尽的探测**永不**唤醒下一轮。

## 5. 与既有机制的关系

| 机制 | 分工 |
|---|---|
| `StepBackoff` | 回答"**下一次什么时候看**"（阶梯） |
| `CheckpointProbe` | 回答"**到点后怎么再看、看什么、判出什么**"（探测点 + 判定） |
| `WaitPolicy` | 回答"**怎么等**"（平台挂起 / 先做其他 / 交回人工） |
| `LockRegistry` | 提供**锁快照**（探测的判别事实来源之一） |
| `LoopGuard` | 上界在"往返轮次"；探测上界在"探测次数"，二者互不占用预算 |

## 6. 平台依据

`cnb:await`/`cnb:resolve`、`crontab`（≥5min）、`cnb:apply`/`cnb:trigger`、
OPENAPI `StartBuild`、`timeout`/计费口径——出处见
`docs/secretary-gate-workflow.md` §2.2.2 与 `shared/collab/collab-rules.yaml` 的 `probe` 段。

## 7. 测试

`tests/test_checkpoint_probe_issue14.py`（38 项）：预算上界与幂等（反向：第 5 次不用尽、
重建不清零）、非轮询落点（反向：cron 不低 5 分钟、无 local_loop 落点）、
判定三态（反向：锁表不可见 → unknown、自述不采信、版本快照靶向护栏）、
平台实践与计费对照、观测与生命周期。
