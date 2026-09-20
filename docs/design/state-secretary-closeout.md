# ISSUE-001 收尾归档 · 状态管理秘书机制

> 归档时间：2026-09-18
> 关联 Issue：#1（状态管理秘书机制：层级状态管理 + 上下文折叠编号检索 + 缓存命中保障）
> 结论：**目标 Y 达成，验收通过，Issue 关闭**。衍生的持续项按各自 Issue 独立跟踪。

## 1. 交付物终态

| 交付物（Issue #1 要求） | 落点 | 状态 |
|---|---|---|
| 层级秘书组织模型（L0-L3 + 升降/边界） | `state-secretary-org.md` + `src/secretary/state/hierarchy.py` | ✅ |
| 状态 Schema 最小集 + 扩展机制 | `state-secretary-schema.md` + `src/secretary/state/models.py` | ✅ |
| 上下文编号/索引/折叠/检索协议 | `state-secretary-fold.md` + `fold_engine.py` / `content_archive.py` | ✅ |
| 折叠触发与停止追踪规则（自动化优先） | `state-secretary-fold.md` §2/§4 | ✅ |
| 缓存命中保障（稳定前缀 + 丢弃时机） | `state-secretary-cache.md` + `src/secretary/context/assembler.py` | ✅ |
| POC 验证（token / 缓存命中 A/B） | `state-secretary-poc.md` + `poc/state_m9_benchmark.py` | ✅ |
| 落库模块划分建议（新增纯净模块） | `state-secretary-module.md` + `state-secretary/` 独立模块 | ✅ |
| 产品 spec（通用层 vs 演进 vs 固化） | `docs/product/state-secretary-spec.md` | ✅ |

## 2. US-1..4 验收结论

| 用户故事 | 验收判据 | 证据 | 结论 |
|---|---|---|---|
| US-1 层级状态管理秘书 | 新会话自动分配独立 L3 会话秘书，不共享上下文 | `SecretarySession` 一实例=一会话；`TestSessionIsolation` 等 | ✅ |
| US-2 上下文折叠与编号检索 | 累积折叠为带 `context_id` 索引态；原文不常驻、按索引精确找回 | `FoldEngine.fold` + `retrieve_original`；`TestFoldContinuationRegression` / `TestRetrieveOriginalGuard` | ✅ |
| US-3 层级向上提交 | 下级只提交汇总态（progress/decisions/open_items），不携全文 | `summarize_for_upstream`；案例断言不含原文 | ✅ |
| US-4 缓存命中率保障 | 同会话前缀稳定不漂移，折叠不破坏前缀 | `ContextAssembler` 三层隔离（缓存键=层A哈希）；`TestCacheKeyStability` | ✅ |

论文三层改进对照表见 `state-secretary-index.md` §2。

## 3. 验证基线（关闭时）

- **pytest 全量 308 passed**（门面 148 / 独立探针 65 / Issue #4 验收矩阵 13 / 底层各模块回归）。
  ⚠️ 本地收尾环境未装 pytest，**未复跑**；该数据取仓库最后验证留痕
  （`issues/TEST-REGRESSION.md` §14.4，head `a455608`）。
- **POC benchmark 三硬阈值 PASS**（`poc/state_m9_benchmark.py`）。
- 跨版判别力矩阵、并发压测、性能有界性见 `issues/TEST-REGRESSION.md` §13–§14。

## 4. 合并留痕

| PR | 内容 | 状态 |
|---|---|---|
| #2 | 架构设计 + 产品 spec（8 文件） | 已合并 |
| #3 / #7 | `SecretarySession` 会话门面 + 评审修复循环收口（14 文件） | 已合并 |
| #15 | LoopGuard 循环熔断守卫 | 独立跟踪（Issue #14） |

## 5. 结转项（不在本 Issue 范围内，另开跟踪）

| 项 | 载体 | 说明 |
|---|---|---|
| P2-① 共享 ho-* 卡跨会话误删 | Issue #4 | 主干修复已并入 #7；Issue #4 作为专项验收载体关闭时需单独确认 |
| P2-② / ③ 独立 HandoffBus 落 cwd、docstring 语义漂移 | Issue #5 / #6 | 小项 |
| P2-④ 交接卡引用可见性契约边界（🟡B 形状校验必含 id） | Issue #8 | 已知边界，不阻断合并 |
| P2-⑤–⑨ fail-fast 回滚 / 可见性文档 / 静态口径 / 单元名白名单 / 读侧守卫 | Issue #9–#13 | 非本 diff 引入，排期处理 |
| 循环熔断 LoopGuard | Issue #14 / PR #15 | 评审修复无限循环控制，独立验证中 |
| DSH 新版生态适配 | Issue #16 | 用户新提需求，独立跟踪 |

## 6. 收尾说明

本 Issue 承载的需求（层级状态管理 + 折叠编号检索 + 缓存命中保障）已完成设计与可运行实现，
验收证据齐备；后续演化项与已知边界均已登记到独立 Issue，**不再占用本 Issue 上下文**。
按用户指示关闭本 Issue。

---

## Issue #14 收尾归档（循环熔断 + 协作等待 + 增量跑测门禁 · 2026-09-19）

> 关联 Issue：#14（感知循环并阻碍 / 只跑增量 · 反全量冗余）
> 结论：**目标达成，验收通过，Issue 关闭**。衍生的持续项按各自 Issue 独立跟踪。

### 1.14 交付物终态

| 交付物（Issue #14 要求） | 落点 | 状态 |
|---|---|---|
| 循环熔断（最大轮次 + 非必要修复最多 1 次 + 静默交接不唤醒） | `src/secretary/loop/loop_guard.py` + `LoopGuard` 门面 | ✅ |
| 评审版本基线 / 冲突收敛成单卡 / 审查范围分级 | `review/review_arbiter.py` | ✅ |
| 聚合一处的锁台账 + 避开清单 + 延迟后检测 | `state/lock_registry.py` | ✅ |
| 等待阶梯 10s→30s→60s→3min→6min→10min 到顶即停 | `src/secretary/collab/backoff.py` | ✅ |
| 检查点探测器（非轮询、事件驱动、探测上界 6） | `src/secretary/collab/probe.py` | ✅ |
| 全局/跨仓规则包（一处定义、只可覆写数值） | `shared/collab/`（组织级 + 仓库级） | ✅ |
| **只跑增量**门禁（反全量冗余 · 计数权责一致 · 全量须标记） | `src/secretary/gate/` + `poc/incremental_gate.py` | ✅ |
| 等待不占 CPU（平台挂起语义） | `.cnb.yml`（`cnb:await` + `crontab` 代跑） | ✅ |

### 2.14 验收结论（用户逐轮口径 → 落地）

| 用户口径 | 落地 | 证据 |
|---|---|---|
| 「最大轮次」「不必要修复最多 1 次」「传递后不额外唤醒」 | `max_rounds=3` / `max_advisory_fix_rounds=1` / 静默交接卡 `wake=False` | PR #15 |
| 「只在开启评审时记录版本」「延迟 1 分钟」「3 分钟持续到对方解决」 | `begin_review()` 幂等不推前 / 延迟窗 60s / 持续锁 | PR #15 |
| 「阶梯 10s…10 分钟到顶就停」「最多 6 次」 | 定值阶梯 + 到顶即停 + `resume_required` 交回人工 | PR #15 |
| 「非轮询、检查点 + webhook 触发、边缘探测器」 | `CheckpointProbe` + 平台 `cnb:await` / `crontab` | PR #15 |
| 「不要大量全量，只审查变化」「连跑三次无意义」 | 审查范围分级 + `RunScopeGuard` 同口径只跑一次 | PR #15 |
| 「只跑增量，跑多少写多少，全量须标记」 | `secretary.gate` 三态门禁 + 计数守卫 + 全量告警视图 | **PR #19（已合并）** |
| 「全量 743 例会被读成本轮 743 → 权责一致」 | `assert_counter` 拒绝未标记的"全量"措辞 | PR #19 |
| 收尾轮全量复跑暴露的 2 项失败 | 🔴-C 修复：沙箱封闭 + 「查不到」≠「没有」 | **PR #23** |

### 3.14 验证基线（关闭时）

- **pytest 全量 397 passed**——收尾轮实测；**#23 合并后二次复跑 397 passed**（本次实跑）。
  构成：8 + 15 + 65 + 148 + 13 + 19 + 20 + 12 + 8 + **89**（增量门禁）。
- 增量口径：`plan --base HEAD` → 计划 **8 例**（新增 5 / 消解 3）、
  已跑过不重复 **389 例**、全量规模 **397**。
- `compileall src tests poc` OK；`pyflakes` / `flake8 --max-line-length=100` 改动文件 0 告警。
- 变异矩阵 M1–M19 逐项实测判别力（见 `state-secretary-incremental-gate.md` §10）。

### 4.14 合并留痕

| PR | 内容 | 状态 |
|---|---|---|
| #15 | LoopGuard + 协作规则 + 检查点探测器（循环熔断与等待机制） | 已合并 |
| #17 | Issue 收尾归档段 | 已合并 |
| #18 | DSH 新版生态适配 | 已合并 |
| #19 | 增量跑测门禁（只跑有效增量 · 剔除冗余 · 计数权责一致） | 已合并 |
| #23 | 收口轮 🔴-C：「查不到变更」≠「没有变更」 | **已合并**（`b5e49e0`） |
| #27 | 关闭前复验（#23 合并后无回归 · 归档口径校正） | **已合并**（`46910b8`） |
| #26 | NPC 成本护栏（核数 / 超时 / 回合上限，Issue #24） | **已合并**（`a429a4c`），独立跟踪 |
| #28 | 组织级成本护栏 v1.2（共享规则库，Issue #24） | **已合并**（`292dc69`），独立跟踪 |

> **收尾轮二次确认（2026-09-19）**：#23 已合入 `main`（`b5e49e0`）。合并后**复跑验证**
> 与 "3.14 验证基线" 一致：全量 pytest **397 passed**（本次实跑），增量门禁 CLI
> `plan --base HEAD` → 计划 **0 例** / 已跑过 **397 例**；`plan --base deadbeef` →
> `⚠ 降级` 全树计划（397 例）；`counter` 对未绑定实跑数的文字**拒绝落账**。
> 即：收口轮修复（沙箱封闭 + 「查不到」≠「没有」）在合并后**行为未回归**。

> **关闭前最终确认（2026-09-20，PR #27 合并后）**：#27 已合入 `main`（`46910b8`）。
> 其后 `main` 又合入 **#28**（组织级成本护栏，`292dc69`，属 Issue #24）。
> **本 Issue 交付面复查**：`#27..HEAD` 的变更**未触碰** `state-secretary/src`、`state-secretary/tests`、
> 本仓 `.cnb/` 与 `shared/` —— 全部落在 `docs/` 与 `issues/TEST-REGRESSION.md`
> （`#28` 新增的 `.cnb.yml` 均在 `docs/npc-guard-org/samples/` 样例目录内，非本仓配置）。
> 故 #28 对本 Issue 交付物**零影响**，无回归面。
>
> **按「只跑增量」口径复验（本次实跑）**：`plan --base 46910b8` → 计划 **0 例**
> （新增 0 / 消解 0），已跑过不重复 **1254 例**。**本轮实跑 0 例**（无变更即无测试增量）。
> 可信性对照：`plan --base deadbeef` → `⚠ 降级` 全树计划 **1254 例（非空）**，
> 证明「0 例」是**真的没有增量**，不是「查不到就报空」；`counter --text "全量 1254 项全部通过"`
> → **拒绝落账**。**未跑全量、未连跑。**

### 5.14 结转项（不在本 Issue 范围内，另开跟踪）

| 项 | 载体 | 说明 |
|---|---|---|
| 增量门禁已知边界 ①–④ | 契约 §9 | 指纹粒度 / 新增文件内既有用例 / 浅克隆回退 / 仅识别 `*.py`，**均为"多跑"方向安全** |
| 跨仓规则推广到组织级 | 待开 Issue | 组织级包已就位（`shared/collab/organization.yaml`），跨仓接入按仓推进 |
| 非 ASCII 单元名存量卡迁移 | Issue #8 | 迁移面，需清单/改名脚本 |
| P2 系列边界 | Issue #5/#6/#9–#13 | 均非本 Issue 引入 |

### 6.14 收尾说明

Issue #14 承载的需求（循环熔断、等待与锁协同、检查点探测、**只跑增量**门禁）
已完成设计与可运行实现，验收证据齐备；收尾轮全量复跑暴露的 2 项失败已定位为
**真缺陷**（非环境噪声）并在 PR #23 修复+加护栏，**#23 已合并（`b5e49e0`）并复跑确认未回归**。
按用户指示关闭本 Issue，后续演化项与已知边界均已登记，不再占用本 Issue 上下文。

**本 Issue 无遗留待开发项**：PR #15（Issue #21 的 `scan_json_spans` 重做）是**独立评审链**，
仍在评审中、与本 Issue 需求交付范围**不重叠**（本 Issue 的交付物已由
PR #17 / #18 / #19 / #23 全部合入）；其结论见 PR #15 评论区，不影响本 Issue 关闭。
