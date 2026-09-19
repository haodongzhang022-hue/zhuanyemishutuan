# 锁聚合台账 · 设计（所有锁统一登记 + 延迟后检测 + 先做其他并挂锁 + 绝不遗留）

> module: zhuanyemishutuan · docs/design
> Ref: #14（追问：锁语义与遗留治理）
> 载体：`state/lock_registry.py`（`LockRegistry` / `LockRecord` / `LegacyLockScanner`）
> ＋ `review/review_arbiter.py`（延迟锁 / 评审读锁）＋ `state/ownership.py`（写租约镜像）
> ＋ `session/session.py`（门面）

## 1. 需求口径

> 延迟可以到 **1 分钟**，**再次触发就按 3 分钟持续到对方解决完**，注意**别遗留锁一直
> 不打开**，**所有锁应该被聚合的管理**，这样**哪里要避开不用测试才知道，而是触发了
> 就可以去看一眼情况**，并且建立在这里的触发机制——**延迟后检测**，以及**先做其他，
> 并挂锁**，实现稳定协作。

拆成四条可执行口径：

| # | 口径 | 落地 |
|---|---|---|
| 1 | 延迟**阶梯**从少到多、10 分钟到顶即停；重触发持续到对方解决完 | `StepBackoff`：10s→30s→60s→3min→6min→**600s 顶**；到顶即停（不重挂）；重触发即 `persistent`。**首档数值源 = 规则包 `locks.retry_delay_sec`**（默认 10s，🔴D-1：覆写即生效） |
| 2 | 别遗留锁一直不打开 | 每把锁都有 `expires_at`；`sweep()` 回收过期+超宽限锁；收口/消解即释放 |
| 3 | 所有锁聚合管理，触发即可查看 | 一本台账 `registry.json`；`locks_overview()` 给出「哪里要避开」 |
| 4 | 延迟后检测 + 先做其他并挂锁 | `retry()` 到点检测；`acquire_blocked()` / `_defer_while_editing()` |
| 5 | 等待**不计 CPU**（平台挂起语义） | `WaitPolicy`：只产出 `platform_suspend` / `deferred_review` / `resume_required`，**永不选本地空转** |

> **v0.2 口径更正**：原「延迟 1 分钟 + 退避 60→120→180 封顶」作废。
> 用户原话「延迟可以到 1 分钟」= **阶梯可达 1 分钟**（60s 档），
> 不等于首档 1 分钟；**延迟首档 10s**（= 阶梯第一档，规则包
> `locks.retry_delay_sec`，覆写即生效）。
> 现为 **10s → 30s → 60s → 3min → 6min → 10min（顶，停）**：前两档刻意很短
> （"大部分很快就没问题"），到顶**不再增长也不无限重挂**（到顶次数用尽 →
> `resume_required`，交回人工/上游）。详见 `docs/secretary-collab-rules.md`。

## 2. 锁的层次与聚合关系

| 层次 | 机制 | 防什么 |
|---|---|---|
| L1 会话内/flock | `StateManager.lock_card` 文件锁 | 同机多进程/线程写入互斥 |
| L2 账本锁 | `LockRegistry`（本模块） | 写租约标识「正在编辑」、评审读锁、延迟锁 |
| L3 版本 CAS | `cas_save_card(expected_version=…)` | 跨机并发（无共享文件锁时）的最后一道 |

**单锁兼容（关键约束）**：`<session>.owner.json` 仍是**终态判据**
（`OwnershipGuard._authorize` 只认它），台账只是它的**镜像 + 聚合视图**。
台账文件被删/损坏 → 单锁行为完全不变，台账自愈重建
（用例 `test_registry_missing_lock_registry_does_not_break_lease`、`test_bad_registry_file_degrades`）。

## 3. 锁类型与语义

| kind | 语义 | 到期 | 释放时机 |
|---|---|---|---|
| `card_write` | 卡片写租约 = **「正在编辑」判据** | 短 TTL（默认 60s，随租约续期） | 编辑结束 `OwnershipGuard.release()` / `_clear_lease()` |
| `review` | 评审读锁（**开了评审才记版本**的可见化） | 短 TTL | 评审结束 `release_review_locks()` |
| `defer` | 延迟锁（**先做其他，并挂锁**） | 首次 60s；重触发转持续 | 对方处理完落盘成功 / 收口 / sweep |
| `serialize` | 通用写入序列化锁 | 短 TTL | 调用方释放 |

**持续锁（`persistent`）**：重触发（同一问题再次撞上同一把锁）即升级，
到期时间按宽限（默认 **3 分钟**）续期，`should_release()` 在宽限内恒 `False`
——**不许提前打开**，一直持续到对方处理完。

## 4. 退避口径（只增不减）

```text
第 1 次触发：等 60s        （延迟后检测）
第 2 次触发：等 120s       （升级为持续锁，锁不打开）
第 3 次及以后：等 180s      （封顶 = 持续锁宽限）
```

- 上限 = `persistent_grace_sec`（默认 180s）；**永不缩短**已承诺的等待
  （用例 `test_backoff_extends_never_shortens` 断言序列单调且 ≤ 上限）；
- 退避**由调用方定值**，台账层只承接（`retry_at = max(原窗口, now + 本次时长)`），
  避免"续期即翻倍"的内部歧义；
- 到点后仍未处理（对方仍在编辑）→ 续期再检测，**不放弃、不释放**。

## 5. 触发机制

```text
评审写回 apply_save()
  ├─ 有他人写租约？ ── 是 ─▶ _defer_while_editing()  ← 先做其他，并挂锁
  │                             ├─ 台账登记 defer 锁（persistent / retry_at）
  │                             └─ 收敛成**同一张**冲突卡（至多一张）
  └─ 否 ─▶ 先落盘（先完成该做的）→ 再判冲突
```

- **延迟后检测**：`retry()` 比较 `retry_at`；未到点 → 返回 `should_retry` + 剩余秒；
  到点且对方已处理完 → 正常落盘并**打开锁**；
- **先做其他**：`acquire_blocked()` 返回 `{"action": "do_other_first", …}`
  ——不空等，锁已挂在自己名下（用例 `test_acquire_blocked_returns_do_other_first`）；
- **收尾补做**：`complete_deferred_review()` / `complete_deferred()`
  供"其他事做完回来收尾"，成功才开锁。

## 6. 绝不遗留（回收路径）

| 路径 | 触发 | 行为 |
|---|---|---|
| 正常释放 | 对方处理完落盘成功 / `release_review_locks()` | 状态置 `released` + 留痕 |
| 收口释放 | `resolve_review_conflict()` / `supersede_pending()` | 冲突处理完 → 延迟锁一并打开 |
| 兜底回收 | `sweep()` / `sweep_locks()` | 过期**且超宽限** → 置 `stale` + 留痕；同步关闭挂账项 |
| 强制回收 | `revoke(key, by=…)` | 人工/上游显式回收（须留痕 `by`） |

**不误杀**（反向对照用例）：活跃锁（未过期）与宽限内的持续锁
**绝不被 sweep 回收**（`test_sweep_does_not_kill_active_lock`、
`test_sweep_does_not_kill_grace_persistent_lock`）。

## 7. 触发即可查看（聚合视图）

`locks_overview()` 一次给出：

- `count` / `by_kind`：按类型计数；
- `held[]`：每把锁的 `key/kind/holder/scope/persistent/ttl_left_sec/retry_in_sec/
  retry_count/reason`；
- `where_to_avoid[]`：**给人看的"哪里要避开"清单**（含持有者与剩余时间）；
- `stale_candidates[]`：已该回收的锁；
- `legacy_locks[]`：历史遗留单锁文件（`LegacyLockScanner` 只读上报，不删）；
- `deferred`：当前「先做其他，并挂锁」项（谁在编辑、锁多久、是否持续锁）。

会话汇总 `summarize_for_upstream()` 也带 `locks`，上级不必问、不必测就能看到。

## 8. 健壮性与边界

| 场景 | 行为 |
|---|---|
| 台账文件损坏 / 非对象 JSON | 视为空台账（不抛），后续登记自愈重建 |
| `session_id` / 锁 key / 锁用途非法 | `ValueError`（不参与路径拼接，防穿越） |
| 非法 `kind` / 负 TTL / 负宽限 | `ValueError`（拒绝静默降级） |
| 台账读写异常 | 静默降级（台账是观测面，**不参与写授权**，不得阻塞评审落盘） |
| 并发登记 / 并发重触发 | flock 串行化，**一把不丢、计数精确**（并发用例 2 项） |
| 会话回收 `delete_session()` | 台账一并删除（同 id 复建不带陈旧锁） |

## 9. 验证

`state-secretary/tests/test_lock_registry_issue14.py`（**35 项**）：默认口径 2 /
延迟后检测与先做其他 6 / 退避与持续锁 5 / 绝不遗留 6 / 聚合可见 6 /
拿不到锁先做其他 4 / 台账健壮性 4 / 并发聚合 3；
另有 `test_review_arbiter_issue14.py` 中原有 4 项延迟窗用例同步对齐（默认 60s）。
全量 **480 项通过**（连跑 3 次稳定），pyflakes 0 告警。
