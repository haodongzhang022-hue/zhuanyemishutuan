# 评审版本仲裁 · 设计（只在开启评审时记版本 + 防同写锁 + 延迟重试 + 单冲突收口）

> module: zhuanyemishutuan · docs/design
> Ref: #14（补全机制）
> 载体：`review/review_arbiter.py`（`ReviewArbiter`）
> ＋ `review/scope.py`（审查范围分级）＋ `session/session.py`（门面集成）

## 1. 需求口径

> 补全机制，**只在开启评审时候记录版本**，过程中变化先不管，如果无法追踪变化就以
> 新的就行，或者**正在编辑的给他一个防止同写的锁**，**延迟一段时间再处理**，防止
> 出现冲突，这样即使真有冲突，量也不大，**先完成该做的之后，只额外处理一个解决
> 冲突就行**，不要让一个小冲突影响整体。而且注意不要太频繁**全量**——**只在**大版本
> 可以进行全量，日常开发不进行全量，**只审查变化**就行，防止无效构建和测试。

拆成五条可执行口径（每条都有代码与用例对应，见 §7）：

| # | 口径 | 落地 |
|---|---|---|
| 1 | 只在开启评审时记录版本 | `begin_review()`；过程中 `recorded()` 恒 False |
| 2 | 无法追踪变化就以新的 | `REBASE`（不编造冲突） |
| 3 | 正在编辑 → 防同写锁 + 延迟 | `retry_after` / `retry_delay_sec` / `retry()` |
| 4 | 先完成该做的，只额外处理一个 | `apply_save()` **先落盘**；冲突收敛为**一张**卡 |
| 5 | 日常只审变化，全量只在大版本 | `decide_scan()` / `scan_commands()` |

## 2. 口径 1：只在开启评审时记录版本

+ **开发过程不记**：`add_task` / 任务推进 / 字段修改**不落**任何评审基线，
  `recorded()` 恒为 `False`（用例 `test_no_baseline_during_development`）。
+ **开启评审才记**：`begin_review()` 把**当前磁盘版本**原子登记为基线。
+ **幂等不推前**：已开启评审后再调 `begin_review()` **不改写**基线
  （`force=False`）——否则等于"过程中变化也记版本"，把基线偷偷推前、
  悄悄吞掉并发变更（用例 `test_begin_review_idempotent_does_not_advance`）。
+ **评审轮推进**：无冲突路径上基线随版本前移（`_advance_baseline`），
  仍属"评审期间登记的版本"演进。

## 3. 口径 2：无法追踪变化就以新的

判定链（任一命中即 `REBASE`，**不编造冲突**）：

| 情形 | 处置 |
|---|---|
| 库中查不到基线（未开启评审 / 跨实例） | 落盘 + 以当前版本重立基线 |
| 当前版本 **<** 基线版本（状态被回退 / 共享卡复用） | 以新为准 |
| 双方版本都低于基线（**无清晰共同祖先**） | 不判冲突 |

> 宽进原则：判不出"是否可追踪"时一律**以新为准**，不因判定能力不足而误开冲突。

## 4. 口径 3：防同写锁 + 延迟重试

+ **锁**：复用既有 `OwnershipGuard` 的写租约（`<session>.owner.json`）。
  有**他人**有效租约 = "正在编辑" → 给防同写锁：本次**不落盘**，返回
  `should_retry=True` + `retry_after`。
+ **延迟**：`retry_delay_sec`（默认 30s）。窗内调 `retry()` 继续等待；
  到点后编辑多半已结束 → 直接完成，无需人工（用例
  `test_retry_after_lease_released_applies`）。
+ **同向可重试**：另一方也是评审（`rules.is_same_side`）且写入落在过渡态
  → 判**可重试的时序冲突**（`should_retry`，**不是** conflict）。
+ **自己的租约不延迟自己**（反向对照用例 `test_own_lease_does_not_defer_self`）。

## 5. 口径 4：先完成该做的 + 只额外处理一个

```text
apply_save(mutate)
  ├─ ① 冲突判定（读基线 + 最新卡 + 编辑锁 + 方向/推进）
  └─ ② _do_save()   ← **无论是否冲突都先写回评审结果**
        └─ 冲突 → _enqueue_conflict() 只登记**一张**卡（后续冲突合并进同一张）
```

+ **冲突不阻塞评审**：`applied=True`（用例
  `test_conflict_still_applies_review` 断言磁盘版本确实 +1）。
+ **只额外处理一个**：`pending_conflicts()` 长度**至多 1**；连续 3 次冲突后
  仍是 1 张，`merged_conflicts==3`（用例
  `test_repeated_conflicts_merge_into_one_card`）。
+ **收口即闭合**：`resolve_review_conflict(id, resolution, by=…)` → 基线以当前
  版本重立，后续从新版本继续（**不因一次冲突重开全量**）。
+ **不占循环预算**：冲突登记**不**消耗 `LoopGuard` 的修复轮次
  （用例 `test_conflict_does_not_consume_loop_budget`）——冲突不是"又一轮修复"。

### 冲突判定参照物（实缺陷修复，务必别退回去）

判定"对方是否**有效推进**"的参照物**必须是评审基线快照**，不能拿
`disk_before`（开始写回时的最新卡）比：后者**已含对方变更**，与写回后相比
（写回只把版本 +1、任务集不变）→ 恒为"无进展" → **每次并发都误判冲突、
每次都开冲突卡**（正是要治的循环）。

`ReviewBaseline.history` 保存开启评审（及退化重立）时的卡快照（最多 2 条），
用例组 `TestProgressReferenceIsBaseline` 是这条的回归护栏。

## 6. 口径 5：日常只审变化，全量只在大版本

| 档位 | 触发 | 审查范围 |
|---|---|---|
| `full` | `major-version` 等大版本标签 / 大版本文本信号 / `major_version=True` | 全量（含全量测试） |
| `scan` | **其余全部情形**（日常开发 / 常规评审 / 修复往返） | 只审变化（变更集 + 受影响面） |

+ **判定顺序（保守）**：显式"只要变化" → 大版本标记/标签/文本 → 默认降级 `scan`。
+ `scan_commands()` 给出执行口径：`scan` 档明确 **"不跑全量"**，`full` 档才允许
  全量构建 + 全量测试（防往返轮次里的无效作业）。
+ 门面：`SecretarySession.review_scope(...)` / `review_scope_commands(...)`。

## 7. 健壮性与边界

| 场景 | 行为 |
|---|---|
| 基线文件损坏 / 非对象 JSON / `version` 类型不符 | 视为未登记（`None`），以新为准，不抛 |
| 冲突卡文件损坏 / ID 非法（含 `../`） | `get_conflict()` 返回 None；`card_path()` 抛 `ValueError` |
| `session_id` 非 `[A-Za-z0-9_-]+` | `ValueError`（不参与路径拼接，防穿越） |
| `retry_delay_sec` 为负/布尔/字符串、`reviewer` 为空 | `ValueError`（拒绝静默取整/匿名留痕） |
| `resolve` 的 `resolution` 为空 | `ValueError`；票据不存在/非 pending → `False`（幂等） |
| 快照 `history` 条目坏 | 逐条丢弃，不拖垮判定 |
| 快照轨迹长度 | 最多 2 条（不随轮次无限增长） |
| `delete_session()` | 一并清理 `<数据根>/review/` 基线与冲突卡，同 id 复建不带陈旧状态 |

## 8. 与门禁规范 / 循环熔断的关系

+ `docs/secretary-gate-workflow.md` §2.2：评审**先落盘**，冲突**只登记一张**，
  收口后以新基线继续；
+ `LoopGuard` 管"**要不要开新一轮**"，`ReviewArbiter` 管"**这一轮怎么写不丢东西**"
  ——两者正交：熔断后评审仍可落盘（`test_fused_session_still_records_review`），
  只是不得再唤醒新一轮。

## 9. 验证

`state-secretary/tests/test_review_arbiter_issue14.py`（**75 项**，全量 **445 项**）：

| 组 | 用例数 | 判据 |
|---|---|---|
| ① 只在开启评审时记版本 | 5 | 开发期无基线 / 开启即记 / 幂等不推前 / 卡不存在拒绝 / 未开启即写 → REBASE |
| ② 先完成该做的 | 4 | 冲突仍落盘（断言版本+1）/ 有效推进不冲突 / 非推进不误判进展 / 冲突卡带版本 |
| ②b 参照物=基线 | 6 | 推进判据用基线快照 / 反向对照（同样输入无进展则冲突）/ 快照缺失宽进 / 坏快照不炸 / 轨迹有界 |
| ③ 防同写锁 + 延迟 | 5 | 他人租约延迟不落盘 / 窗内继续等 / 放租约后重试成功 / 自己租约不延迟自己 / 同向可重试不算冲突 |
| ④ 单冲突收口 | 6 | 3 次冲突仍 1 张 + merged=3 / 卡 ID 稳定 / 收口重立基线 / 空 resolution 拒绝 / 未知票据 False / 重试成功作废旧卡 |
| ⑤ 以新为准 | 4 | 无基线 REBASE / 版本退化 REBASE / 无共同祖先不冲突 / 写者不明不冲突 |
| ⑥ 健壮性与生命周期 | 8 | 坏基线/坏卡退化 / 非法 session_id / 非法延迟 / 空 reviewer / 路径穿越票据 / 生命周期回收 / 默认目录 |
| ⑦ 审查范围分级 | 13 | 默认 scan（4 组）/ 大版本 full（5 组）/ 显式"只要变化"压过大版本 / `scan` 口径不含全量 / 门面接入 |
| ⑧ 规则与数据模型 | 8 | 同侧判定 6 组参数化 / 模型容错 |
| ⑨ 与循环熔断联动 | 4 | 冲突不占轮次预算 / 跨轮冲突卡恒 1 张 / 收口后增量继续 / 熔断后评审仍落盘 |

判别力：每个关键断言都配反向对照或边界佐证（见文件头 docstring）。
