# 修复纳入测试清单（测试回归任务台账）

> 用途：将评审/测试过程中**修复过的东西**主动纳入本地回归测试。
> 本地拉取代码后，按本清单**逐项测试**，每个修复项明确"应该怎么测"。
> 维护：随每次修复/评审更新，测试完成后勾选。

## 关联

- 上游需求：ISSUE-001 状态管理秘书机制（Issue #1）
- 当前代码载体：PR #3 `SecretarySession` 会话门面（源分支 `auto/session-facade-9b95`）
- 测试基线：全量 pytest 102 项通过（原 74 + 门面 16 + 评审回归）

---

## 一、PR #3 功能主体回归

| 编号 | 测试点 | 怎么测 | 状态 |
|---|---|---|---|
| F-1 | US-1 会话隔离 | 不同会话各自独立，不共享上下文 | ✅ |
| F-2 | US-2 折叠编号+原文按索引可检索 | fold 后按 content_ref 精确找回原文 | ✅ |
| F-3 | US-3 向上提交汇总态 | summarize_for_upstream 不含原文全文 | ✅ |
| F-4 | US-4 缓存键稳定 | 同一会话多次装配 key 不变 | ✅ |
| F-5 | M5 交接卡自动关联 StateCard | create_handoff 自动建关联状态卡 | ✅ |

## 二、评审修复回归（本地必测）

### 🔴 修复 #1 — fold() 重复调用 content_ref 错位覆盖
- **问题**：连续两次 fold，第二轮任务挂第一轮 content_ref；归档文件被覆盖。
- **修复**：fold() 基于任务卡既有最大 content_ref 段号**续号**；`store_segments` 新增 `start_seq`。
- **怎么测**：
  1. 同一实例连续两次 fold，确认两轮各任务 ref 互不重复。
  2. 第一/二轮各自 `retrieve_original` 都能精确找回对应原文。
  3. **跨实例续号**：同 session_id 新开实例再 fold，ref 从 segNNN 续号不与前轮重复。
- **回归测试**：`TestFoldContinuationRegression`

### 🟡 修复 #2 — add_task(summary) 被静默吞掉
- **问题**：`summary` 参数未透传底层，折叠摘要字段形同虚设。
- **修复**：`add_task` 显式透传 `summary` 到 TaskRecord。
- **怎么测**：调用 `add_task(summary=...)` 后读取任务记录，确认 `t.summary` 非空且与传入一致；重开会话读回仍一致。
- **回归测试**：`TestAddTaskSummary`

### 🟡 修复 #3 — 层B拿不到任务明细
- **问题**：StateCard 缺 `compact_context`，装配层B只含目标/进度/会话，无任务标题/折叠引用。
- **修复**：新增 `_StateCardProxy`，装配入口注入 FoldEngine 代理生成压缩上下文。
- **怎么测**：`assemble_context` 返回的层B应携带展开任务标题 + 折叠任务编号引用，可驱动任务导航；层A仍保持静态隔离。
- **回归测试**：`TestLayerBNavigationContent`

### 🟡 修复 #4 — retrieve_original(None) 抛裸 TypeError
- **问题**：`content_ref=None` 抛裸异常。
- **修复**：公共 API 对 None/空串/非法字符/不存在引用统一返回 `None`。
- **怎么测**：传 None/空串/非法字符/跨会话引用/不存在引用，均应返回 None 不抛异常。
- **回归测试**：`TestRetrieveOriginalGuard`

### 🟡 修复 #5 — 数据根分裂
- **问题**：归档/交接默认落 `<cwd>/data/…`，出现双 `data` 嵌套，随 cwd 漂移。
- **修复**：`data_dir` 统一收敛 `state/ content/ handoffs/` 三子目录，消除双嵌套。
- **怎么测**：显式传 `data_dir=…/state` 时，归档落 `…/state/content`、交接落 `…/state/handoffs`，无 cwd 孤儿、无双 `data`。
- **回归测试**：`TestDataRootAndCleanup.test_single_data_root` / `test_data_root_has_no_double_data`

### 🟡 修复 #6 — delete_session 数据残留
- **问题**：删状态卡后归档目录、交接卡等仍残留。
- **修复**：`delete_session` 清理状态卡+原文归档+本会话交接卡；`HandoffBus` 新增 `delete(card_id)`。
- **怎么测**：delete_session 后确认归档/交接卡均清，state|content|handoffs 根保留可复用。
- **回归测试**：`TestDataRootAndCleanup.test_delete_session_cleans_archive_and_handoff`

### 🔴 修复 #A — 交接卡数据根分裂 / ho-* 孤儿状态卡
- **问题**：`create_handoff(auto_state_card=True)` 用默认 StateManager 建 ho-* 卡，落 cwd 根且永不清理。
- **修复**：`HandoffBus` 新增 `state_root` 参数，ho-* 卡收敛到 `<data_dir>/state`；SecretarySession 注入本会话根。
- **怎么测**：create_handoff 后 ho-*.json 落在 `<data_dir>/state/`，cwd 无孤儿。
- **回归测试**：`TestHandoffStateCardConvergence`

### 🔴 修复 #B — delete_session 三类残骸
- **问题**：删除后残留 .lock / 归档空目录 / 跨实例交接卡。
- **修复**：删 .lock/.owner.json；回收归档空目录；交接卡归属**落盘台账** `.session-<id>.owned`，跨实例可回收。
- **怎么测**：
  1. delete_session 后无 `.lock`、无归档空目录、无交接卡/ho-* 卡/台账残留。
  2. 跨实例重开（上一实例未清理）再 delete，回收上一实例交接卡。
  3. 多会话同根互不误删。
- **回归测试**：`TestDeleteSessionNoResidue`

### 🔴 修复 #C — 共享 ho-* 卡跨会话误删（P2-① / Issue #4）
- **问题**：ho-* 卡 ID = hash(source,target,intent)，同源/同目标/同意图的多个会话共享同一张卡；任一会话 `delete_session()` 会把它连带删除，另一存活会话引用失效。
- **修复**：ho-* 卡删除前做**存活引用检查**（`_scan_ho_referencing_sessions`）：扫描同数据根下其他会话归属台账 `.session-<id>.owned`，仅当引用该卡的会话自身已删（引用已清）才物理删除；引用未清不得删。最后存活引用者删除时一并回收。
- **怎么测**：
  1. A/B 同源同目标同意图建交接 → 共享同一张 ho-* 卡；删 A 后 B 的 ho-* 卡/交接卡引用仍可读。
  2. A 先删、B 最后删 → 共享卡在 B 删除后才回收，无孤儿无残留。
  3. 不同意图的独立 ho-* 卡互不阻塞，随各会话删除即回收。
- **回归测试**：`TestSharedHoCardCrossSession`（3 项）

---

## 三、可复现缺陷记录（P2 待办）

评审/测试接力中发现、**非阻塞合并**的 P2 边界问题，已建 Issue 跟踪。

| P2 | 问题 | 对应 Issue | 状态 |
|---|---|---|---|
| ① | 共享 ho-* 卡跨会话误删，另一会话引用失效 | Issue #4 | ✅ 已修复（#C） |
| ② | 独立 HandoffBus 未传 state_root 落 cwd 孤儿 | Issue #5 | ✅ 修复（本分支）：data_dir 显式时默认以父目录推导 state_root，ho-* 卡落 `<parent(data_dir)>/state`；未传 data_dir 保持历史退化兼容。回归测试见下（106 项全过） |
| ③ | delete_card 返回值 docstring 语义漂移 | Issue #6 | ✅ 修复（本分支）：统一返回「状态卡是否存在」（.lock/.owner.json 伴生残留不影响返回值，幂等清理），delete_card/delete_session 两处 docstring 对齐。回归测试见下（115 项全过） |

> P2-② 新增回归测试：
> - `test_standalone_handoff_explicit_data_dir_derives_state_root` — data_dir 显式 → ho-* 卡落父目录推导的 state/，无 cwd 孤儿
> - `test_standalone_handoff_data_dir_is_state_root_directly` — data_dir 即根时推导父目录 state/，不自嵌套
> - `test_standalone_handoff_explicit_state_root_still_wins` — 显式 state_root 最高优先
> - `test_standalone_handoff_no_data_dir_backward_compatible` — 未传 data_dir 保持历史退化行为（向后兼容）


> P2-③ 新增回归测试（返回值语义统一）：
> - `test_delete_card_card_exists_returns_true` — 状态卡存在 → delete_card 返回 True
> - `test_delete_card_card_missing_residue_only_returns_false` — 状态卡缺、仅剩 .lock/.owner 残留 → 返回 False（漂移边界），伴生残留仍幂等清理
> - `test_delete_card_nothing_exists_returns_false` — 卡与残留均无 → False（幂等）
> - `test_delete_card_idempotent_second_call_returns_false` — 二次删除返回 False
> - `TestDeleteSessionReturnSemantics` — delete_session 有卡返 True；卡缺仅残留时返 False 且清理完全

## 四、测试环境

- Python 3.13 + pytest 8.3
- 运行：`cd state-secretary && pytest -q`
- 配置：`pytest.ini` 已配置 `pythonpath=src`

