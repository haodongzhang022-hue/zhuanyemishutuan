# 修复纳入测试清单（测试回归任务台账）

> 用途：将评审/测试过程中**修复过的东西**主动纳入本地回归测试。
> 本地拉取代码后，按本清单**逐项测试**，每个修复项明确"应该怎么测"。
> 维护：随每次修复/评审更新，测试完成后勾选。

## 关联

- 上游需求：ISSUE-001 状态管理秘书机制（Issue #1）
- 当前代码载体：PR #3 `SecretarySession` 会话门面（源分支 `auto/session-facade-9b95`）
- 测试基线：全量 pytest **133 项通过**（123 + 🔴#4 复评回归 10）

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
- **修复**：ho-* 卡删除前做**存活引用检查**：扫描同数据根下其他会话归属台账 `.session-<id>.owned`，仅当引用该卡的会话自身已删（引用已清）才物理删除；引用未清不得删。最后存活引用者删除时一并回收。
- **怎么测**：
  1. A/B 同源同目标同意图建交接 → 共享同一张 ho-* 卡；删 A 后 B 的 ho-* 卡/交接卡引用仍可读。
  2. A 先删、B 最后删 → 共享卡在 B 删除后才回收，无孤儿无残留。
  3. 不同意图的独立 ho-* 卡互不阻塞，随各会话删除即回收。
- **回归测试**：`TestSharedHoCardCrossSession`（3 项）

### 🔴 修复 #D — 共享 ho-* 卡复评三缺陷（Issue #4 复评打回：🔴#1–#3）
> 评审对象 `b0ac532`（修复 #C）。单实例顺序场景已过，但存活引用判定口径与执行顺序存 3 处 🔴 必修，验收要点 1/2 未闭环。
- **🔴#1 判定与撤销自身引用顺序颠倒（TOCTOU 孤儿）**：原顺序「删交接卡 → 判定存活引用 → 删自身状态卡+台账」。自身引用在判定**之后**才撤销，两个引用者并发/交错删除时各自判定都看到对方存活 → 双方都跳过删除 → 共享卡 + `.lock` 成孤儿。实测 30 轮随机并发泄漏 27 次、屏障对齐确定性复现。
  - **修复**：删除顺序调整为「**先撤销自身引用**（本会话交接卡 → 本会话状态卡 → 归属台账），**再**一次扫描建立存活引用索引并回收」。前置撤销后不存在双跳过：若某次判定仍见他人引用，对方撤销必然更晚、其判定必然更晚，由最后撤销者回收。
- **🔴#2 引用来源覆盖不全（独立 HandoffBus 引用不可见）**：扫描只认归属台账，P2-② 支持的独立 `HandoffBus(data_dir=…)` 路径**不落台账**，其未删除交接卡的 `state_card_id` 引用不被计入 → 秘书删除后共享卡被删、独立使用者引用失效（与原始缺陷同型）。
  - **修复**：存活引用源改为**并集** = 其他存活会话台账 ∪ 未删除交接卡（`handoffs/*.json` 非隐藏文件）的 `state_card_id`。
- **🔴#3 台账非法 JSON 结构（数组）→ 裸 `AttributeError` 中断删除并留残留**：`_scan_ho_referencing_sessions`/`_load_ledger` 对台账直接 `data.get(...)`，未校验 `dict`。同根出现合法 JSON 但非对象的 `.owned` 即抛 `AttributeError: 'list' object has no attribute 'get'`，且本会话状态卡与台账均残留、构造期同样崩。
  - **修复**：`isinstance(data, dict)` 守卫（`_load_ledger` 与新增 `_read_json_dict` 两处），非 dict 视为空台账；字段类型不符（`handoff_ids` 非数组）同样降级为空。
- **怎么测**：
  1. **屏障对齐并发删除**：用屏障把两次存活判定对齐，共享卡 + `.lock` + `.owner.json` 必须被最后撤销者回收；多轮泄漏计数为 0。
  2. **独立 HandoffBus 共享引用**：独立使用者与秘书会话共享同一 ho-* 卡，秘书 `delete_session()` 后共享卡保留；独立使用者删卡后引用清空、共享卡可回收。
  3. **非法台账鲁棒性**：同根其他会话台账为数组时 `delete_session` 不抛异常且回收完全；本会话自身台账非法时构造不崩、视为空台账、删除幂等。
- **回归测试**：`TestSharedHoCardConcurrentReclaim`（2 项）+ `TestSharedHoCardIndependentBusReference`（2 项）+ `TestLedgerMalformedRobustness`（4 项）

> 🟡 建议一并落地：删除路径原为「每张 ho-* 卡各扫一遍 `handoffs/`」，复杂度 O(卡数 × 台账数)；改为 `_build_ho_reference_index` **一次扫描**建 `state_card_id → 引用者集合` 索引，O(台账数 + 卡数)。实测同根 200 台账 + 50 卡 + 10 待回收：**32.7ms → 4.9ms**。
> 🟢 优化一并落地：`except Exception` 宽吞窄化为 `(OSError, ValueError)`；`_build_ho_reference_index` 引用者标签区分语义（会话 ID / `handoff:<card_id>`），`_scan_ho_referencing_sessions` 保留为兼容包装。

---

### 🔴 修复 #E — 异交接目录 + 同 state 根下共享卡引用静默失效（Issue #4 复评打回：🔴#4）
> 评审对象 `6a2e713`（修复 #D）。🔴#1 / 🔴#3 已确认清零；#D 对 🔴#2 的修复仍残留同类缺陷（🔴#4）。
- **问题**：存活引用索引仍**只扫本会话 `handoff_bus.data_dir`**，但删除目标 ho-* 卡在**共享 state 目录**。当独立 `HandoffBus` 的交接目录 ≠ 会话 `handoffs/`、而两者共享同一 state 根时，其未删除交接卡的 `state_card_id` **不被计入** → 秘书 `delete_session()` 仍删掉共享 ho-* 卡 → 存活交接卡引用静默失效。与 🔴#2 同型同根因。
  - 确定性复现（`queue/` 布局、显式 `state_root` 布局均 5/5 违反；co-located 0/5）。
- **修复（双保险）**：
  1. **扫描界提升**：`_build_ho_reference_index` 扫描界从 `handoff_bus.data_dir` 提升到**会话数据根 `self._session_root`（`os.walk` 递归）**，收集根下任意子目录的交接卡（经廉价预筛 + 交接卡形状校验）与归属台账；剪枝隐藏目录与原文归档根。
  2. **state 根级引用登记**：`HandoffBus.create_handoff(auto_state_card=True)` 在 `<state_root>/handoff-refs/<card_id>.ref` 登记「交接卡 → ho-* 卡」引用（记录 `card_path`），`delete()` 同步注销；索引并入该登记——覆盖**交接卡目录完全落在会话数据根之外**的布局（目录扫描不可达）。登记随卡回收，`card_path` 已消失的 stale 登记视为失效、不永久阻塞回收。
- **怎么测**：
  1. **四布局参数化保留**：`colocated` / `queue` / `explicit_state_root` / `out_of_root`，独立使用者与会话共享同一 ho-* 卡，秘书 `delete_session()` 后共享卡与独立交接卡均保留。
  2. **四布局参数化释放回收**：独立使用者删卡后引用清空，共享卡可回收（无孤儿）。
  3. **登记生命周期**：建卡登记存在、删卡登记注销；盘外删除卡（stale 登记）不阻塞回收；根外目录布局仅靠登记即可阻止误删。
- **回归测试**：`TestSharedHoCardIndependentBusReference`（2 项 × 4 布局 = 8）+ `TestSharedHoCardStateRootRegistry`（4 项）

> 复评 🟡 一并落地：
> - 🟡1 `test_malformed_field_types_treated_as_empty` 恒真断言 → 换为判别性断言（非法字段不产生任何存活引用）。
> - 🟡2 测试计数同步：README/设计文档 102 → **132**。
> - 🟡3 共享卡「后到者上下文静默丢弃」语义写入 `SecretarySession.create_handoff` / `HandoffBus.create_handoff` docstring。
> - 🟢 `Optional[set]` → `Optional[Set[str]]`。

## 三、可复现缺陷记录（P2 待办）

评审/测试接力中发现、**非阻塞合并**的 P2 边界问题，已建 Issue 跟踪。

| P2 | 问题 | 对应 Issue | 状态 |
|---|---|---|---|
| ① | 共享 ho-* 卡跨会话误删，另一会话引用失效 | Issue #4 | ✅ 已修复（#C + 复评 #D：顺序/引用源/类型守卫 + 复评 #E：扫描界提升到数据根 + state 根级引用登记，覆盖异交接目录/根外布局） |
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

- Python 3.13 + pytest 8.3（本地复现用 3.11）
- 运行：`cd state-secretary && pytest -q`
- 配置：`pytest.ini` 已配置 `pythonpath=src`

