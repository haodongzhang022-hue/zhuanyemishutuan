# 修复纳入测试清单（测试回归任务台账）

> 用途：将评审/测试过程中**修复过的东西**主动纳入本地回归测试。
> 本地拉取代码后，按本清单**逐项测试**，每个修复项明确"应该怎么测"。
> 维护：随每次修复/评审更新，测试完成后勾选。

## 关联

- 上游需求：ISSUE-001 状态管理秘书机制（Issue #1）
- 当前代码载体：PR #3 `SecretarySession` 会话门面（源分支 `auto/session-facade-9b95`）
- 测试基线：全量 pytest **295 项全部通过**（门面 **148 项** = 90 + 24 复评第 5 轮判别性
  回归 + 5 🟡 收口回归 + 29 M-1/M-2/🟡1–🟡5 补收口，独立探针 65 项；底层模块回归
  8 + 15 + 19 + 20 + 12 + 8）。
  历史口径：`28ca7f6` 228（门面 88）/ `60e32ec` 230（门面 90）/ `bca4193` 237（门面 90）
  / `e3195ea` 261（门面 114）/ `63345ec` 266（门面 119）→ 本轮 **295**（门面 148）。

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
  （后续 🔴#4 新增 state 根级引用登记 `handoff-refs/`，数据根子目录增至 4 个。）
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
> - 🟡2 测试计数同步：README/设计文档 102 → **137**。
> - 🟡3 共享卡「后到者上下文静默丢弃」语义写入 `SecretarySession.create_handoff` / `HandoffBus.create_handoff` docstring。
> - 🟢 `Optional[set]` → `Optional[Set[str]]`。

### 🔴 修复 #F — 🔴#4 跟进收口（Issue #4 复评 #E 的残差）
> 评审对象 `11dfdd9`（修复 #E）。四布局扫描界已过，但 #E 的实现仍留 2 处残差，
> 与本角色独立探针（不变量「存活交接卡 → 其 ``state_card_id`` 状态卡必须存在」）
> 的复现一致。
- **残差 1（同型同根因）**：`delete_session()` 以 `exclude_handoff_ids=set(owned_handoffs)`
  把**本会话认领的交接卡 ID 从索引中排除**，假定"自身卡一定已删"。若该卡
  **删除失败**（OSError / 权限 / 竞态）仍留盘，排除它会让共享卡被误删、而残卡
  仍持有失效引用——破坏不变量。修复：**取消自身卡排除**，凡磁盘上实际存在的
  交接卡一律计为存活引用（**不误删**；**无"延迟回收/自动回收"路径**——残卡留盘
  期间共享卡一并留盘，须**显式清理残卡**后由后续删除者再次判定方可回收，
  不产生永久孤儿）。
- **残差 2**：`os.walk` 递归扫描界只剪枝隐藏目录与归档根，未剪枝 `node_modules`
  等重型非数据目录。`data_dir` 指向工作区根时全树遍历代价高，且其内无关 JSON
  可能被形状校验误判为交接卡引用 → **永久阻塞共享卡回收**。修复：新增
  `_SKIP_SCAN_DIRS` 剪枝；形状校验收紧为须同时含 `state_card_id` +
  `source_unit` + `target_unit` + `id`。
- **怎么测**：
  1. **自身卡删除失败**：monkeypatch `os.remove` 令自身交接卡删除抛 `OSError` →
     共享卡不得被删、自身状态卡仍正常回收；残卡清除后共享卡可回收。
  2. **重型目录剪枝**：`node_modules` 内伪交接卡（含全部字段）不得产生任何引用。
  3. **形状校验**：`state/` 下 StateCard JSON 不得被误判为交接卡，真实交接卡引用仍计入。
- **回归测试**：`TestSharedHoCardSelfResidueInvariant`（2 项）+ `TestSharedHoCardScanPruning`（2 项）
  —— 其中 3 项在 `11dfdd9` 上失败（有判别力）。

> 🟡 复评 #E 收口：
> - 🟡1 `test_malformed_field_types_treated_as_empty` 的"判别性断言"仍偏弱（仅断言
>   非法值不作为 key）。**加强**：非法台账会话不得出现在**任何**引用者标签中 +
>   同目录正常交接卡引用必须仍被计入（证明索引非"空而恒真"）。
> - 🟡3 `_scan_ho_referencing_sessions` 补 `.. deprecated::` 标注（#E 遗漏）。
> - 测试计数同步 README/设计文档 → **137**。

### 🟡 修复 #G — 复评 #E 遗留收口（Issue #4 复评：🟡4 未落地 + 计数 55 + 文档漂移）
> 评审对象 `570c7ee`。评审确认 🔴 清零、放行，但指出 2 项 🟡 需下一提交收口。
- **🟡4 未落地且留痕不实**：上一轮声明「在 `test_batch_persist` 加断言固化后到者
  上下文未落卡」，但 `test_batch_persist.py` 实际 **0 行改动**，全仓亦无该断言
  （仅 `SecretarySession.create_handoff` / `HandoffBus.create_handoff` 两处
  docstring）。**本轮真实落地**：在 `test_handoff_same_state_card_no_dup_task`
  补 3 处判别性断言——
  1. 首到者 `context` 落卡（`card.tasks[0].summary == "首到者上下文"`）；
  2. 全量序列化（`json.dumps(card.to_dict())`）**不含**后到者 context 文本；
  3. 重开会话（新 `StateManager` 实例）读回仍一致（语义随卡落盘持久化）。
  - **判别力**：把实现改为「后到者另建任务」或「追加后到者 context」→ 断言 2
    失败；改为「首到者 context 不落卡」→ 断言 1 失败。非恒真。
- **门面计数 54 → 55**：门面文件 `tests/test_secretary_session.py` 实收 **55** 项
  （137 = 82 + 55）。已同步 `state-secretary/README.md`、`docs/design/
  state-secretary-index.md`、`docs/design/state-secretary-module.md`。
- **🟢 文档漂移**：`_build_ho_reference_index` docstring 剪枝行补 `_SKIP_SCAN_DIRS`
  全量清单 + 形状校验四字段口径 + **可见性保证范围**（仅覆盖 `HandoffBus` API
  写入路径；手写/外部遗留卡缺 `id` 或落剪枝目录时不可见——属已知覆盖边界，
  是否收窄为 API 路径保证待总师裁定）。
- **测试基线**：仍 **137 项**（仅加强既有断言、未新增用例）；`compileall` 通过。

### 🟡 修复 #H — 最终复核（`570c7ee`）🟡 随本 PR 小提交收口
> 评审对象 `570c7ee`（PR #7）。评审判定 🔴 清零、`merge` 放行；本轮随 PR 小提交
> 收口 3 项非阻塞 🟡，行为零变更（🟡A/🟡B 待总师裁定后另开 Issue）。
- **🟡C 措辞更正**（`_build_ho_reference_index` docstring + 测试 docstring）：
  原文「仅延迟回收」与事实不符——残卡留盘期间共享卡**永久留盘**，穷举会话侧
  API（同会话新实例 ×N、无关会话 ×N）后**无自动回收路径**，须**显式清理残卡**
  后由后续 `delete_session` 再次判定方可回收。更正为「**不误删；残卡需显式清理**」，
  并在 `test_own_residue_reclaims_after_card_removed` 加**判别性断言**：残卡留盘
  期间新会话 `delete_session` 后共享卡**必须仍留盘**。
  - **判别力实证（变异测试）**：在 `delete_session` 内注入「顺带回收孤儿 ho-* 卡」
    逻辑 → 新断言失败；还原 → 通过。
- **计数 54 → 55**：由上一提交 `042e7e6` 落地，本轮**独立复核**确认门面文件
  `tests/test_secretary_session.py` 实收 **55** 项（137 = 8+15+55+19+20+12+8），
  与 `README.md` / `state-secretary-index.md` / `state-secretary-module.md` 三处
  文档口径一致。
- **`.gitignore` 补 `data/handoff-refs/`**：`HandoffBus(auto_state_card=True)`
  的 state 根级引用登记目录此前未被忽略（`data/state/`、`data/handoffs/` 已忽略），
  易被误提交。补齐后与既有数据根子目录（state/ content/ handoffs/ handoff-refs/）
  忽略口径一致（`60b625d` 已同步「三子目录」措辞）。
- **测试基线**：仍 **137 项**（🟡C 仅加强既有断言、未新增用例）；`compileall` 通过。
  （**后续 🔴 修复 #J 后基线为 199 项**，见下节。）

### 🟡 修复 #I — 复评 `042e7e6` 三项结转项收口（PR #7 · 收口轮）
> 评审对象 `042e7e6`。评审判定 🔴 清零、`merge` 放行，但如实指出 3 项**结转未收口**
> （非本轮 diff、非阻塞）：🟡C 措辞（`bb9df61` 已收口）、`.gitignore`（`bb9df61` 已收口）、
> `handoff.py` 模块注释「三子目录」未含 `handoff-refs/`；另有 🟡A/🟡B/🟡D 契约边界待总师裁定。
- **`handoff.py` 模块注释同步**（`handoff.py:29/51`）：原写「SecretarySession **三子目录**
  （state/ content/ handoffs/）」，未含 🔴#4 引入的 `handoff-refs/` 登记目录 → 更正为
  「数据根**四子目录**（state/ content/ handoffs/ handoff-refs/）」；
  `SecretarySession.__init__` docstring 的 `data_dir` 说明同步补「交接引用登记」。
  **行为零变更**（纯注释/docstring）。
- **契约文档落地**：新增 `docs/design/state-secretary-handoff-visibility.md`——冻结核心契约
  （扫描界 / 引用源并集 / 登记生命周期 / 自身残卡不变量 / 回收语义），并把 🟡A（剪枝歧义名）/
  🟡B（形状校验收紧必含 `id`）/ 🟡D（旧版根外卡升级路径）的**待裁定口径 + 两选项**成文，
  供总师裁定；`_build_ho_reference_index` docstring 指向该文档。
- **🟡A/🟡B/🟡D 登记后续 Issue**：**Issue #8**（P2-④ 交接卡引用可见性契约边界）。
- **测试基线**：仍 **137 项**（本轮零新增用例，仅注释/文档）；`compileall` 通过。

### 🟡 修复 #J — 增量复评 `82807ff` 口径收窄（PR #7 · 收口轮 2）
> 评审对象 `bb9df61..82807ff`（增量 5 文件 +111/−4）。评审判定 🔴 清零、
> `proceed`（允许合并），要求把 🟡#1 口径收窄（+ Issue #8 补第三条 gap）并
> 顺带收口 🟡#2/#3 与 🟢#1/#2。本轮**仅改文档/注释/忽略项，行为零变更**。
- **🟡#1 口径收窄（过度声明 → 带前置条件）**：`docs/design/state-secretary-handoff-visibility.md`
  §3 与 `session.py::_build_ho_reference_index` docstring 原称「经 `HandoffBus` API
  写出的卡…歧义目录与无 `id` 场景**均不误删**」，作为**一般性保证**不成立。
  收窄为：登记兜底成立须 **① `state_root` 与会话数据根同根**（否则卡目录落在
  扫描界之外）**且 ② `handoff-refs/<card_id>.ref` 登记写入成功**（失败被
  `_register_ref` 静默吞、无重试无补偿）。**不带前置条件的表述属过度声明，不采用**。
- **🟡E 新增第三条 gap（与 🟡D 同族）**：登记写入失败 + 根外卡目录 → 引用**双路径
  皆不可见** → 共享 `ho-*` 卡被误删（违反 §2「宁可多保留，绝不误删」）。
  **纯 API 路径反例**已复现（`auto_state_card=True`、无手写卡）：
  `SecretarySession('sessA', data_dir=root).create_handoff(...)` → 登记目录置为不可写
  （同名文件占位）→ `HandoffBus(data_dir='external/queue', state_root=root).create_handoff(同三元组)`
  复用同一 `ho-*` 卡、登记未写出 → `A.delete_session()` → 卡已删、根外卡留盘。
  已在契约文档 §3 补 🟡E 小节，并登记 Issue #8（第三条 gap）。
- **🟡#2 措辞残留**：`issues/TEST-REGRESSION.md:198`「三子目录」已由 `60b625d` 同步，
  本轮再补 `handoff-refs/` 使与数据根四子目录口径完全一致。
- **🟡#3 `.gitignore` 口径**：忽略清单未含 `data/content/`，与「数据根子目录全忽略」
  口径不符——实测 `fold()` 后 `data/content/<sess>/*.json` 为未跟踪状态。补 `data/content/`。
- **🟢#1 影响面口径对齐**：契约文档 🟡A 计数由「8/8 目录复现」更正为对
  `_SKIP_SCAN_DIRS` **全量 11 名逐一复现、11/11 均误删**（计数取小会弱化影响面）。
- **🟢#2 注释换行**：`handoff.py:29-30 / 52-53` 把词组「`handoffs/ handoff-refs/`」
  劈开 → 改为整词组不跨行，便于检索。
- **测试基线**：仍 **137 项**（本轮零新增用例，仅文档/注释/忽略项）；`compileall` 通过；
  `handoff.py` / `session.py` 剥离 docstring 后 AST 等价（行为零变更）。

## 三、可复现缺陷记录（P2 待办）

评审/测试接力中发现、**非阻塞合并**的 P2 边界问题，已建 Issue 跟踪。

| P2 | 问题 | 对应 Issue | 状态 |
|---|---|---|---|
| ① | 共享 ho-* 卡跨会话误删，另一会话引用失效 | Issue #4 | ✅ 已修复（#C + 复评 #D：顺序/引用源/类型守卫 + 复评 #E：扫描界提升到数据根 + state 根级引用登记，覆盖异交接目录/根外布局） |
| ② | 独立 HandoffBus 未传 state_root 落 cwd 孤儿 | Issue #5 | ✅ 修复（本分支）：data_dir 显式时默认以父目录推导 state_root，ho-* 卡落 `<parent(data_dir)>/state`；未传 data_dir 保持历史退化兼容。回归测试见下（106 项全过） |
| ③ | delete_card 返回值 docstring 语义漂移 | Issue #6 | ✅ 修复（本分支）：统一返回「状态卡是否存在」（.lock/.owner.json 伴生残留不影响返回值，幂等清理），delete_card/delete_session 两处 docstring 对齐。回归测试见下（115 项全过） |
| ④ | 交接卡引用**可见性契约边界**（🟡A `_SKIP_SCAN_DIRS` 歧义名 / 🟡B 形状校验收紧必含 `id` / 🟡D+🟡E 登记静默失败） | Issue #8 | ✅ **已修复（#J）**：🟡A 剪枝歧义名 → 保护子树 + 形状判定（探针 0/12 悬空）；🟡D/🟡E 登记静默失败 → 唯一可见性路径 fail-fast 回滚；⚠️ 仅剩 🟡B（形状校验必含 `id`）为已知边界（改走 API 即覆盖）。不再依赖总师裁定即可放行。口径详见 `docs/design/state-secretary-handoff-visibility.md` |
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

### 🔴 修复 #J — 复评 `60b625d` 打回项收口（PR #7 · P0-1/P0-2 + P1-1/P1-2 + 🟡）

> 评审对象 head `60b625d`（复评 #3 维持打回）。评审判定 🔴 P0-1/P0-2 未修复、
> 不放行；另指出 🟡 台账口径与实现不符（`data/content/` 未忽略）。本轮按
> ①–⑤ 全部处置。

**① 🔴 P0-1 剪枝误伤存活引用 — 已修复**
- **根因**：`_build_ho_reference_index` 按**裸目录名**（`_SKIP_SCAN_DIRS`）整片
  跳过子目录；名单名（`node_modules`/`venv`/`.venv`/`env`/`dist`/`build`/`target`/
  `vendor`/`coverage` 等）可能被合法选作 `data_dir` → 其中真实/遗留交接卡不可见
  → 共享 `ho-*` 卡被误删（评审实测悬空 **11/12**，`11dfdd9` 对照 1/12）。
- **修复**：剪枝改为**形状判定 + 保护子树**（`_keep_scan_dir` / `_protected_scan_dirs`
  / `_dir_holds_handoff_data`）：
  1. 保护子树（数据根 / `state` / `handoffs` / `content` / `handoff-refs`）及其祖先
     **永不剪枝**（即使名字命中名单）；
  2. 名单目录（含 `.venv`）**先做形状判定**（有界浅扫：深度 ≤ 3、条目 ≤ 2000），
     内有交接卡形状 JSON 则保留，否则剪枝（保留原性能收益）；
  3. 被保留的名单目录记入 `kept_skip_dirs`，其内引用按"**仅指向本根真实状态卡**"
     过滤 → 既让真实/遗留卡可见，又不让伪 JSON 永久阻塞回收。
- **验证**：自写探针 12 个名单名目录 → **悬空 0/12**（修复前 11/12）。

**② 🔴 P0-2 registry `isdir→listdir` TOCTOU 异常逃逸 — 已修复**
- **根因**：`session.py` 来源 3 的 `os.listdir(reg_dir)` 无守卫；并发的
  `HandoffBus.delete → _unregister_ref` 会回收空登记目录 → `FileNotFoundError`
  逃逸出 `delete_session`，中断于回收中途（注入 100%；自然并发 3/2160）。
- **修复**（两道防线）：
  1. `listdir` 包 `try/except OSError`（目录消失 = 无登记引用）；
  2. 扫描整体 `try/except OSError` → **保守跳过回收**（扫描不可用则无法证明
     引用已清，宁可不删绝不误删），且异常**不得中断**后续归档/台账清理。
- **验证**：注入式探针 → 异常 None、残留 `[]`；自然并发 6 进程 × 360 次
  ×3 轮 = **0/2160**（修复前 3/2160）。

**③ 🟡 P1-1 登记失败 fail-fast / P1-2 迁移前提文档化 / 🔴 P1-3 路径穿越 / 🟡E 双通道**
- `HandoffBus._register_ref` 返回布尔；新增 `_registry_required()` 判定"登记是否
  为该卡唯一可见性路径"（`data_dir` 在 `state_root` 子树之外）。
- 唯一路径且登记失败 → **回滚交接卡并抛 `OSError`**（fail-fast，不产生悬空引用）；
  子树内 → 目录扫描兜底，不阻断。
- `state-secretary-handoff-visibility.md` 新增 §5「迁移前提」：旧版根外卡须补偿
  登记（重走 API 或手写 `.ref`）方可被会话侧发现。
- **🟡E 闭环（双通道登记）**：登记同时写主 `<state_root>/handoff-refs/` 与备
  `<state_root>/.handoff-refs/`（不同故障域）。单通道被同名文件占位 / 权限不足 /
  单条 `.ref` 被外部删除时另一通道仍让引用可见；仅双通道全失败且为唯一可见性路径时
  fail-fast 回滚。`.gitignore` 补 `data/.handoff-refs/`。
- **🔴 P1-3 `card_id` 路径穿越**：新增 `_is_valid_card_id`（`[A-Za-z0-9_-]+` 白名单），
  `get`/`delete`/`_register_ref`/`_unregister_ref` 全部前置校验；台账 `handoff_ids`
  注入经 `HandoffBus.delete` 亦被拦截，不再构成 `data_dir` 之外的删除面。
- **🔴 P1-3 补充（本角色独立探针新发现，随本轮补齐）**：`card_id` 由
  `source_unit` / `target_unit` **拼接**而成，但 `create_handoff` 原先只校验
  `card_id`（在 `get`/`delete` 等**读/删**路径），**未校验单元名**——故
  `create_handoff("../state/VICTIM", "t", …)`（含经 `SecretarySession` 门面）
  会让 `_save` 把卡写到 `data_dir` **之外**（路径穿越**写入**面，污染数据根之外）。
  补齐：`create_handoff` 对 `source_unit` / `target_unit` 做同一白名单校验
  （非法抛 `ValueError`）；`_save` 另做**纵深防御**（非法 `card.id` 拒绝落盘，
  防外部构造 `HandoffCard` 绕过）。回归：`TestHandoffUnitNameTraversalGuard`（8 项
  ——6 参数化 + 门面 + `_save` 纵深），在修复前 head 上 **8/8 FAIL**（有判别力）。

**④ 🟡 台账/忽略口径收口**
- `.gitignore` 补 `data/content/`（原文归档根）——原忽略清单缺 `content/`，默认
  路径运行后 `git status` 出现未跟踪归档文件（评审+测试双方复现）。现忽略清单为
  **6 条**数据根项：`state/ content/ bindings/ handoffs/ handoff-refs/
  .handoff-refs/`（后两项分别由 🔴#4 引用登记与 🟡E 双通道登记引入）。
  口径说明：`bindings/` 为数据根**结构性目录**（全仓无代码引用，仅忽略项），
  故「数据根**四子目录**」指会话门面实际派生的
  `state/ content/ handoffs/ handoff-refs/`；`bindings/`、`.handoff-refs/`
  为附加项，不与「四子目录」表述冲突。
- `docs/design/state-secretary-handoff-visibility.md` §3「API 路径保证」口径更正：
  P1-1 修复后该保证**无条件成立**（原文档承诺在登记静默失败时不成立）。

**⑤ 回归测试（新增 20 项，全部在 `60b625d` 上失败 → 有判别力）**
- `TestPruneNameDirVisibility`：11 项名单名目录参数化 + `test_standalone_bus_skip_named_dir_derived_state_root`（独立 bus + 未显式 state_root）（承载真实卡须可见）+
  `test_skip_named_dir_without_real_card_still_pruned`（判别性对照：伪引用仍剪枝）+
  `test_data_root_named_as_skip_dir_never_pruned`（保护子树）；
- `TestRegistryListdirToctouGuard`：`test_registry_dir_removed_between_isdir_and_listdir`
  （注入 TOCTOU）、`test_scan_failure_skips_reclaim_without_abort`（第二道防线）、
  `test_concurrent_create_fold_delete_no_exception`（线程并发）；
- `TestRegistryFailFast`：`_registry_required` 判定 + 唯一路径 fail-fast 回滚 +
  子树内容忍登记失败（3 项）。
- **测试基线**：137 → **228 项全部通过**（门面 **88 项**，独立探针 58 项；
  含本角色 P1-3 写入面补充 +8 与总师 🔴1 竞态收口 +3）。
- **架构·测试独立探针**（`tests/test_ho_ref_invariant_probes.py`；当时 41 项，
  后经该角色 `42ad15e`/`24d104f` 扩至 **58 项**）：
  修复前（`60b625d`）**40 项 FAIL**（P0-1 33 + P0-2 2 + P1-3 4 + 反向守卫 1），
  修复后 **41 项全部 PASS** → P0/P1 清零的可执行证据。
  **xfail 标注已移除**（原 `xfail(strict=False)` 修复后虽显示 XPASS，但日后回归会
  静默转为 xfail 而不报失败 → 门禁失效）；改为硬断言后任何回归直接 FAIL。

### 🟡 修复 #M — 评审 P2-5 归因更正 + 静态告警清零（收口轮）
> 评审复评 `60b625d` 指出台账对静态告警的**归因不实**，要求更正归因。

- **归因更正（评审 P2-5）**：`handoff.py` E303、`session.py` F841（`task`）
  **均非本 PR 新增**——基线 `de5eae8` / `60b625d` 同型（行号随改动漂移，同口径
  下基线 5 条告警）；本 PR 真正新增的静态告警**仅**
  `test_secretary_session.py` 的 E741（`l`）。此前台账未区分二者，现更正。
- **本轮清零（顺手收口，行为零变更）**：五处全部修掉——
  - `handoff.py` E303（`_save` 后多空行）；
  - `session.py` F841（`fold()` 内未使用的 `task` 赋值 → 改为直接调用）；
  - `test_secretary_session.py` E741（`for l in labels` → `lbl`）；
  - `test_secretary_session.py` E303/E302（类间空行）；
  - `test_secretary_session.py` F841（未用 `ho` 变量）。
- **核验**：本 PR 四个改动文件 `flake8 --max-line-length=100` **0 告警**
  （基线同口径 5 条）。

### 🔴 修复 #K — 复评 `ee4214e` 打回项独立核验 + 口径收口（PR #7 · 收口轮 3）

> 评审对象 head `ee4214e`（增量 `82807ff..ee4214e`，纯文档/注释/忽略项）。评审
> 维持 `fix_bug` + `blocked` 上报总师，要求：① 补 Issue #8 影响面同步；② P0-2
> 无争议先修；③ P0-1 待总师定方向；④ P1-3 删除面白名单。本角色**独立复跑**
> （自写探针，不复用作者/测试用例）+ 5 版本矩阵（`6393886` / `11dfdd9` /
> `570c7ee` / `ee4214e` / 现 head）逐项核验。

**一、P0-1 / P0-2 / P1-3 独立复现（`ee4214e`，确认未清零）**

| 项 | 我的独立探针结果（`ee4214e`） |
|---|---|
| P0-1 | in-root 名单名目录 × {登记缺失 / 登记失败} → **误删**（`11dfdd9` 同矩阵 OK ⇒ 确为 `570c7ee` 新增回归）；非名单名（`queue`）OK |
| P0-2 | 注入 `listdir` `OSError` → **逃逸出 `delete_session`**、返回丢失、`state/ho-*.json` 残骸 |
| P1-3 | `bus.delete('../../…')` → 根外文件被删（返回 `False` 但文件已删）；`_unregister_ref` 穿越删他人 `.ref`；台账 `handoff_ids` 注入经 `delete_session` 可达删除面 |

**二、修复核验（现 head）**

| 项 | 现状 | 证据 |
|---|---|---|
| P0-1 | ✅ 闭环 | in-root 名单名目录 12/12 全 OK（现 head 复跑） |
| P0-2 | ✅ 闭环 | 注入式异常 None、残骸 `[]` |
| P1-3 | ✅ 闭环 | `delete` 返 `False` 且 victim 完好；`_unregister_ref` 不删他人 `.ref`；台账注入被拦截；`8790e5f` 补 `source_unit/target_unit` 写入面白名单 |
| 全量 | ✅ | **228 passed**（门面 88 + 独立探针 58）；`compileall` 通过 |

**三、残留边界（本轮实测，非阻塞）**

- **名单目录内再嵌套 ≥ 3 层**：卡目录 `<root>/node_modules/a/b/`（深度 2）**可见**；
  `<root>/node_modules/a/b/c/`（深度 3）**不可见** → 登记不可用时误删。非名单名
  （`queue`）任意深度均 OK。已写入契约文档 §3/§4。
- **根外卡 + 双通道登记均不可用**（外部删 `.ref`）：误删仍在，但 `6393886`(main)
  同场景即破坏 ⇒ **baseline 既有面，非本 PR 引入**（归因措辞已按此更正）。

**四、本轮改动（仅文档/台账，行为零变更）**

- **Issue #8 正文同步**：🟡A 影响面 `8/8` → **11/11**；作废「仅影响非 API 路径」
  过度声明（实测 API 卡目录落剪枝名 + 登记不可用同样误删，影响面与写入路径无关）；
  待办更新为处置状态（🟡A/🟡D/🟡E 已闭环，仅剩 🟡B + 深度残留边界）。
- `docs/design/state-secretary-handoff-visibility.md` §3/§4：深度边界改为**实测口径**
  （深度 2 可见 / 深度 3 不可见）+ 补「根外卡 + 登记不可用 = baseline 既有面」。
- 本节 `.gitignore` 口径精确化（6 条数据根项；「四子目录」指门面派生项，`bindings/`
  与 `.handoff-refs/` 为附加项）。

**五、待总师裁定**

- P0-1 剪枝方向：现状采用评审倾向的「**保护子树 + 形状判定**」路线（= 评审 §五 ②
  的加强版，兼容剪枝性能收益）；若总师另定方向（回退剪枝 / 收窄契约），我按裁定返工。
- 深度残留边界是否需覆盖（会牺牲剪枝收益）。
### 🟡 修复 #N — P0-2 同形点加固（`list_pending`）+ 计数口径同步
> 评审回转卡 ① 建议项：`handoff.py:243` `list_pending()` 的
> `os.listdir(self.data_dir)` 属 **`main` 既有代码**（非本 PR 新增面），
> 建议随 P0-2 一并加固。
- **加固**：`os.listdir(self.data_dir)` 包 `try/except OSError` → 数据目录被并发
  回收 / 权限 / IO 异常不可读时返回**空列表**，不再向调用方抛裸异常。与
  `_build_ho_reference_index` 来源 3 的守卫同形（后者为本 PR 新增逃逸面，已由
  `a852574` 修复；本处为既有代码，一并收口）。
- **怎么测**：`TestListPendingListdirGuard`（2 项）——
  ① monkeypatch `os.listdir` 对该目录抛 `FileNotFoundError` → 返回 `[]` 不抛；
  ② 目录整体被外部删除 → 返回 `[]`（幂等）。
- **测试基线**：上游 `28ca7f6` 实测 **228 项**（门面 88 + 独立探针 58）→ 本角色
  +2 → **230 项全部通过**（门面 90，独立探针 58）；`compileall` 通过。

## 四、测试环境

- Python 3.13 + pytest 8.3（本地复现用 3.11）
- 运行：`cd state-secretary && pytest -q`
- 配置：`pytest.ini` 已配置 `pythonpath=src`

## 六、Issue #4 复评第 4 轮 · 总师裁定 + 🔴1 根因收口

> 裁定：**本 PR 内修复收口**（不开新 Issue、不接受口径收窄规避）；本轮为最终
> 收口轮（范围冻结）。详见 `docs/design/state-secretary-handoff-visibility.md` §7。

### 🔴1 登记目录 rmdir × 登记写 竞态（根因：临界区无互斥）
- **问题**：`_register_ref`（`makedirs`/`mkstemp`）与 `_unregister_ref`（空目录
  `rmdir`）无互斥 → `mkstemp` 抛 `FileNotFoundError`。开发方双通道 + fail-fast
  把后果转为「fail-fast 误抛 `OSError`（根外布局）+ 登记静默丢失（根内布局）」，
  **根因未消除**。
- **修复**：模块级 `_REF_REGISTRY_LOCK`（RLock）；登记临界区与注销临界区互斥。
- **怎么测**：6 线程 × 300 轮「登记→校验 `.ref` 存在→注销」，丢失须为 0
  （修复前 22–33/2400）；根外布局并发 `create_handoff` 不得误抛 `OSError`。
- **回归测试**：`TestRegistryRmdirWriteRace`（3 项，修复前基线确定性失败）

### 🔴2 索引来源 3 `listdir` 无守卫 → `delete_session` 抛未捕获异常
- 已由开发方 `a852574` 修复（`try/except OSError → []`），总师复跑确认。
- 回归测试：`TestRegistryListdirToctouGuard` + 测试方独立探针 `TestP0_2...`（硬断言）

### 🔴1 补强（复评第 4 轮 · 确定性窗口回归 + 计数同步）
> 总师 `73361ca` 已落地 🔴1 根因收口（模块级 `_REF_REGISTRY_LOCK`）。本角色在
> **范围冻结**约束下仅补两项**非侵入**增量（不引入新机制）：
- **确定性窗口回归** `TestRegistryRmdirWindowDeterministic`
  （`test_rmdir_between_makedirs_and_mkstemp_is_mutually_excluded`）：与既有
  概率型 `TestRegistryRmdirWriteRace` 互补——以「窗口内等待对端完成一轮
  `rmdir`」把 `makedirs → mkstemp` 竞态**确定性**对齐。判别力实证：临时移除
  模块级互斥（等价 `8790e5f`）→ **3/3 稳定 FAIL**（根外卡误判双通道全失败 →
  fail-fast 抛 `OSError`）；恢复锁 → 稳定 PASS。
- **计数同步**：README / 设计索引 / module / 台账 4 处 210 → **228**
  （门面 87 → **88**，独立探针 41 → **58**）。
- **基线**：`pytest -q` **228 passed**；`compileall` 通过。

---

## 七、Issue #4 复评第 4 轮 · 架构·测试独立复测（`60e32ec`）

> 角色：架构·测试。对象：PR #7 head `60e32ec`（源分支 `auto/session-facade-9b95`）。
> 触发：总师裁定回执要求「独立复跑并发矩阵 + 确认 210 passed 后放行；并请测试方
> 补来源 2 独立判别 / 形状校验 / 台账存活断言（非阻塞）」。
> 结论：🟢 **通过（放行合并）**，无 🔴 / 🟠；补 7 项判别性断言（含变异验证）。

### 7.1 独立复跑（未采信单方结论）

| 项 | 口径 | 基线 `cc7f4fd` | 收口 `73361ca` / `60e32ec` |
|---|---|---|---|
| 登记静默丢失 | 6 线程 × 300 轮 × 3 组 | **64 / 5400** | **0 / 5400** |
| 并发矩阵 误抛 | 根外+根内 × 6 × 250 × 3 | **5** | **0** |
| 并发矩阵 登记丢失 | 同上 | **234 / 9000** | **0 / 9000** |
| 并发矩阵 静默悬空 | 同上 | 0 | 0 |
| 全量 pytest | `cd state-secretary && pytest -q` | — | **237 passed** |
| `compileall src` | — | — | OK |

- 复跑脚本（本角色独立探针，**未随 PR 提交**以遵守「范围冻结」）：并发矩阵
  （根外 / 根内 × 6 线程 × 250 轮 × 3 组，指标 = 误抛 / 登记丢失 / 不变量悬空）
  与登记丢失探针（6 线程 × 300 轮 × 3 组）。
- 基线 `cc7f4fd` 用 `git archive` 单独检出 `src` 运行，**未污染工作树**。
- 总师表列「0–2/1500」与本角色实测「234/9000」同向（本角色轮数更大、复现率
  更高），结论一致：**基线有确定性竞态，收口后清零**。

### 7.2 补 7 项判别性断言（`tests/test_ho_ref_invariant_probes.py`，58 → 65 项）

| 用例 | 断言 | 变异判别力（已验证） |
|---|---|---|
| `test_source2_alone_blocks_shared_ho_delete` | 清登记后仅靠**来源 2**（卡本体）不得误删共享卡 | 跳过非隐藏 `*.json` → **FAIL** |
| `test_source2_released_then_card_reclaimed` | 反向对照：卡本体删除 → 可回收（防恒真） | — |
| `test_large_context_card_visible_full_read` | `context` 200 KB，`state_card_id` 位于 64 KB 之后仍须可见 | `f.read()`→`f.read(65536)` → **FAIL** |
| `test_ledger_dead_session_contributes_no_reference` | 死会话（状态卡已删）台账不得贡献引用 | 移除存活守卫 → **FAIL** |
| `test_ledger_alive_session_contributes_reference` | 反向对照：存活会话台账须贡献引用（防恒真） | — |
| `test_shape_requires_id_field` | 缺 `id` 的形状 JSON 不得计入（🟡B 边界固化） | `'"id"' in raw`→`True` → **FAIL** |
| `test_shape_complete_card_counted` | 反向对照：四字段齐备须计入（防恒真） | — |

- **变异测试矩阵**：4 个变异体（来源 2 移除 / 台账存活守卫移除 / 形状校解放宽 /
  截断读取）各触发 **1–27 项 FAIL**，本实现上 **7/7 PASS** → 断言非恒真、有判别力。
- 本组为**判别力补强**（覆盖既有用例未隔离的路径），非缺陷回补。

### 7.3 信号灯

- 🟢 **通过**：🔴1 / 🔴2 独立复跑清零；全量 237 passed；判别性断言补齐。
- 🟠 **无**（原 🟡A/🟡B/🟡D 按总师裁定随 Issue #8，非本 PR 门禁；
  已知深度边界见 §三 P2-④ 与契约文档 §3/§4）。
- 🔴 **无**。

### 7.4 测试计数同步

- 全量 **237 项** = 8 + 15 + 65 + 90 + 19 + 20 + 12 + 8
  （`batch_persist` / `content_archive` / 独立探针 / 门面 / `arbiter` /
  `state_manager` / `ownership` / `state_tree`）。
- 同步 `state-secretary/README.md`、`docs/design/state-secretary-index.md`、
  `docs/design/state-secretary-module.md` 三处（230 → 237；独立探针 58 → 65）。

---

## 八、Issue #4 复评第 5 轮 · 架构·测试独立复测（`bca4193`）

> 角色：架构·测试。对象：PR #7 源分支 head **`bca4193`**（`60e32ec` + test-only 提交）。
> 触发：架构·评审 `60e32ec` 增量复评（🔴 清零 / `proceed`）转交本角色复测。
> 范围：增量 `28ca7f6..60e32ec`（6 文件 +70/−6）+ 全量 `main..60e32ec`。
> 前置核对：`bca4193` 相对 `60e32ec` 的 `state-secretary/src` **逐字节不变**
> （`git diff --stat 60e32ec..bca4193 -- state-secretary/src` 空），故 src 复测结论
> 等价适用于两 head。
> 结论：🟢 **通过（放行合并）**，无 🔴 / 🟠；3 项 🟡 结转（均为 baseline 既有面，
> 非本 diff 引入、不在 `delete_session` 关键路径）。

### 8.1 独立复跑（探针脚本未随 PR 提交，遵守范围冻结；工作树已复原）

| 项 | 口径 | 结果 |
|---|---|---|
| 不变量（逐布局） | 11 布局 | 10 ok / 1 破坏（根外 + 双通道登记**全被外部删除**，见 8.3 🟡-3） |
| P0-2 注入式 TOCTOU | `listdir` 对登记目录抛 `FileNotFoundError` | **不逃逸**，残骸 `[]` ✅ |
| P0-2 自然并发 | 6 线程 × 120 轮（含 rmdir 竞态） | 逃逸 **0/720**，残骸轮次 **0/120** ✅ |
| P0-2 判别对照 | 基线 `a852574~1` 同探针 | `ESCAPED FileNotFoundError` + 残骸 `state/ho-*.json`/`.lock` ✅ 判别成立 |
| P0-1 根内剪枝名矩阵 | 11 名单名 × 3 触发 = 33 + `queue` 对照 3 | 违反 **0/36** ✅ |
| P1-3 读/删/写三面 | `delete`/`get`/`create_handoff` 穿越 | 全拦截，受害文件存活 ✅ |
| NEW-1 单元名白名单 | 非 ASCII / `../` | `ValueError` ✅；`_save` 纵深防御 `ValueError` ✅ |
| 🔴1 登记写 × `rmdir` 竞态 | 根内 + 根外 × 6 线程 × 150 轮 | 登记丢失 **0/1800**，误抛 **0** ✅ |
| 不变量并发保持 | 根内 / 根外 × 4 线程 × 60 轮 | 破坏 **0/120**，误抛 **0** ✅ |
| 全量 pytest | `cd state-secretary && pytest -q` | **237 passed** ✅ |
| `compileall src tests` | — | OK ✅ |
| `pyflakes` | `session.py` / `handoff.py` / 两测试文件 | **0 告警**（全仓仅既有未用导入，非本 PR） |

### 8.2 变异测试（判别力，非恒真实证）

在 `bca4193` 检出副本上逐个注入变异体，跑全量：

| 变异体 | 触发 FAIL |
|---|---|
| 形状校解放宽（`'"id"' in raw` → `True`） | 1 |
| 截断读取（`f.read()` → `f.read(65536)`） | 1 |
| 台账存活守卫移除 | 1 |
| 来源 2 整体失效（跳过非隐藏 `*.json`） | 38 |
| 来源 3 整体移除 | 17 |
| 来源 3 `listdir` 守卫移除 | 22 |
| 模块级互斥锁失效（`with _REF_REGISTRY_LOCK` → `if True`） | 3 |

基线恢复后 **237 passed** → 断言非恒真、门禁持续有效。

### 8.3 缺陷分级与结转

- 🔴 **P0：无**。P0-1 / P0-2 独立复跑清零，判别对照成立。
- 🟠 **P1：无**。P1-3 读/删/写三面全拦截；NEW-1 写侧穿越 `ValueError`。
- 🟡 **P2（结转，均非本 diff 引入、不阻断合并）**：
  - **🟡-1 `list_pending` 逐文件读路径逃逸（确认评审定级 🟡，不升级门禁）**：
    逃逸需**不同 `HandoffBus` 实例**（各自 `_lock` 互不互斥）或**外部非原子写者**。
    实测：独立实例 4 读 × 4 写 × 200 轮 → `JSONDecodeError` **14261**；
    外部 `open(w)` 截断写 → `JSONDecodeError` 9765 + `TypeError` 2372。
    同实例内不逃逸（`list_pending`/`get`/`_save` 共享 RLock）。
    基线 `main 6393886` 同口径逃逸 **3144** → **baseline 既有面、非本 PR 引入**，
    且不在 `delete_session` 关键路径 → **维持 🟡，不升级门禁**。
    建议（随 Issue #8 / 后续）：`list_pending`/`get` 读侧加 `json`/`OSError` 守卫，
    或统一走 `_save` 的原子写（`mkstemp` + `os.replace`）。
  - **🟡-3 根外卡 + 双通道登记**被外部删除** → 不变量破坏**：基线 `main` 同场景
    亦破坏（4/4 布局全破坏），本 PR 由双通道 + fail-fast 收窄至「仅双通道被外部
    删除」一态 → 属 🟡D/🟡E 同族既有面，契约文档 §4/§5 已记录并给出补偿登记口径。
  - **🟡-5 存量非 ASCII 单元名卡 → 永久残骸**（确认评审定级）：旧版（`main`）
    `create_handoff("单元","目标")` 实测**可产出**该卡。本 PR 白名单后：
    不变量**仍成立**（形状扫描计入引用、共享 `ho-*` 卡保留，实测 ok），
    但该卡 `get`→`None`、`delete`→`False` → **无法经 API 清理**，须手工删文件。
    属**迁移面**（存量数据），非本 PR 新增缺陷 → 维持 🟡。

### 8.4 计数口径一致性

- 实测 237 = 8 + 15 + 65 + 90 + 19 + 20 + 12 + 8
  （`batch_persist` / `content_archive` / 独立探针 / 门面 / `arbiter` /
  `state_manager` / `ownership` / `state_tree`）。
- `state-secretary/README.md`、`docs/design/state-secretary-index.md`、
  `docs/design/state-secretary-module.md` 三处均为 **237**（门面 90 + 独立探针 65）✅。
- 台账头部原漂移（遗留 `228` / `229` 两行并存）→ **本轮修正为 237**。

### 8.5 信号灯

- 🟢 **通过（放行合并）**：无 🔴 / 🟠；P0/P1 独立复跑清零；判别力经 7 变异体验证；
  全量 237 passed；`compileall` OK。
- 🟡 结转 3 项（🟡-1 / 🟡-3 / 🟡-5）：均 baseline 既有面或迁移面，**不阻断合并**，
  建议随 Issue #8 排期。

---

## 九、Issue #4 复评第 5 轮 · 🔴A / 🔴B 定点修复（head `7300b22` 打回）

> 评审（架构·评审）在 `7300b22` 增量复评判 **不通过**（核心不变量在根内布局仍可破坏、
> 且随 `PYTHONHASHSEED` 变化）。本角色按点定修法修复，遵守总师 §7.1 范围冻结
> （不重构、不引新机制）。契约文档 §8 同步。

### 🔴A 保留根多重重叠 → 深度截断顺序相关（`session.py::_exceeds_kept_skip_depth`）
- **问题**：`kept_skip_dirs` 为 `set`，原实现「首个前缀命中即 `return`」取**集合迭代
  顺序**的首个前缀而非最近根 → 嵌套保留根下深度上界判定随路径哈希漂移。
- **修复**：取**所有匹配根的最小深度**（`abspath == r` 仍立即返 `False`）。
- **怎么测**：固定布局（`<root>/build/decoy.json` 使 `build`/`build/dist` 双双入列 +
  受害者卡 `<root>/build/dist/x/y` + 双通道 `.ref` 删除）× 多 seed →
  不变量须**恒**保持（修复前 10 seed 中 6 破坏）。
- **回归测试**：`TestKeptSkipDepthOrderIndependence`（5 项，修复前 3 项 FAIL）。

### 🔴B 隐藏目录按裸名剪枝（`session.py::_keep_scan_dir`）
- **问题**：隐藏目录走 `if name.startswith("."): return False`（**纯裸名剪枝**），
  与名单目录"按裸名判定"同根因 → 根内隐藏 `data_dir`（`.staging`/`.cache`/`.data`/
  `.build`）+ 双通道登记不可用时，其内真实卡不可见 → 共享卡被误删（实测 4/4 破坏）。
- **修复**：隐藏目录并入名单分支，同一 `_dir_holds_handoff_data` 有界判定后再剪。
- **怎么测**：4 隐藏名 × 3 触发（`legacy_no_registry` / `registry_failed` /
  `ref_externally_removed`）+ 对照 `queue` + `.git` 剪枝收益 → 0 破坏、伪引用 0。
- **回归测试**：`TestHiddenDirVisibility`（14 项，修复前 8 项 FAIL）。

### 🟡C / 🟡D / 🟡E 口径更正（随本轮）
- **🟡C fail-fast 回滚面**：**已知边界、声明不改行为**（范围冻结）——回滚交接卡本体，
  已建 `ho-*` 卡留盘残骸、无台账可达 → 后续 `delete_session` 不回收。理由：该卡可能为
  **既有共享卡**（hash 复用），回滚前须判"是否本次新建"（行为变更）。
  回归：`TestFailFastRollbackBoundary`（2 项）锁定边界。
- **🟡D 口径过度声明**：撤回「API 路径引用恒可见**无条件成立**」→ 改为**带边界表述**；
  §5「根内卡无需迁移」补前置（深度 < 3 且条目预算未耗尽）。
- **🟡E 条目预算**：§3/§4 补「`max_entries=2000` 耗尽 → 不可见」+「`os.scandir` 顺序
  参与预算消耗、天然不确定」。回归：`TestScanBudgetBoundary`（3 项）。

### 自测
- 全量 `pytest -q`：**261 passed**（门面 114、独立探针 65）；`compileall` OK；
  `flake8 --max-line-length=100`（两改动文件）**0 告警**。
- 独立探针（本角色自写）：10-seed 矩阵 **0/10 破坏**；隐藏名 4×3 **0/12 破坏**；
  剪枝收益不退化（`.git`/`node_modules`/`.venv` 伪引用 0、2000 目录索引 1.2ms）。

---

## 十、🟡 结转小提交收口（评审终裁 🟡-2 / 🟡-5 · 非门禁）

> 触发：架构·评审终裁回执（head `bca4193`，`merge`）请确认 🟡-2（台账过期行）与
> 🟡-5（契约文档补注）随本 PR 小提交收口。范围**非门禁**，仅文档口径。

### 🟡-2 台账过期基线行（`229（门面 89）`）
- **现状：已收口**。该行由架构·测试 `7c4f50a` 并入单一实测口径，本角色 `e3195ea`
  进一步改为「实测 261 + 历史口径一行」（`28ca7f6` 228 / `60e32ec` 230 /
  `bca4193` 237 → 261）。全文已无并存的过期基线行（`228` 仅存于历史留痕 §节）。
- **复核**：`grep -n "^\s*- 测试基线.*229" issues/TEST-REGRESSION.md` → 无命中。

### 🟡-5 契约文档 §5 补注（单元名白名单为新增前提）
- **收口**：`docs/design/state-secretary-handoff-visibility.md` §5 新增第 4 条
  「存量非 ASCII 单元名卡」——点明 🔴 P1-3 单元名白名单 `[A-Za-z0-9_-]+` 为
  **本 PR 新增**约束；旧版（≤ `6a2e713`）经 `create_handoff("单元","目标")` 产出的
  存量卡 `card_id` 含非 ASCII → 白名单后 `get`→`None`、`delete`→`False`，
  **无法经 API 清理**，须**重建或改名**（重命名并改写卡内 `id`，或改走 API 重建）。
  同时注明**不变量仍成立**（形状扫描计入引用、共享 `ho-*` 卡保留）→ 属迁移面。
- 头部状态块 + §3 表 P1-3 行同步加注，指向 §5 第 4 条。

### 自测
- `pytest -q`：**261 passed**（本节为纯文档改动，计数不变）；`compileall src` OK。
- 无代码/测试改动 → 不改行为、不动门禁结论。
- **后续**：§十一 代码侧 🟡 收口 +5 用例 → 全量 **266 passed**；本节 🟡-2 / 🟡-5 结论不变。


---

## 十一、Issue #4 复评第 5 轮 · 架构·开发 代码侧 🟡 收口（`63345ec` 基线 · 非门禁）

> 角色：架构·开发。触发：架构·评审增量复评 `60e32ec` 处置 **`proceed`**（🔴 清零），
> 点名确认 🟡-2 台账口径 / 🟡-5 契约文档补注，并询问 🟡-1 是否一并收口。
> 本轮**不含 🔴 修复**，均为 🟡 建议收口 + 判别性回归。
> 基线：源分支 head 已由上游推进至 `63345ec`（`e3195ea` 🔴A/🔴B 定点修复 + §十
> 🟡-2/🟡-5 **文档口径**收口）。本节仅收**代码侧** 🟡（🟡-1 / 🟡-4）+ 🟡-3 报告。

### 10.1 收口清单

| 项 | 处置 | 落点 |
|---|---|---|
| 🟡-1 `list_pending` 逐文件读未守 | ✅ **一并收口**（同函数口径不一致，低风险） | `handoff/handoff.py::list_pending` |
| 🟡-2 台账基线并存 | ✅ **已由 §十 收口**（唯一口径 + 历史行）；本轮复核无残留 | `issues/TEST-REGRESSION.md` 头部 |
| 🟡-3 fail-fast 回滚未含 `ho-*` + `.lock` | ⏸ 本节不改行为（上游 §8.3 范围冻结下定为已知边界）；**已由 §十二 🟡1 落地**（回滚本次新建 `ho-*` 卡） | `handoff/handoff.py::create_handoff`（§十二 已落地） |
| 🟡-4 标识符尾锚 `$` 而非 `\Z` | ✅ 收紧为 `\Z`（**同族 6 处 pattern 一并收紧**） | handoff / manager / arbiter / content_archive / session |
| 🟡-5 单元名白名单为新增前提 | ✅ **已由 §十 收口**（契约文档 §5 第 4 条） | `docs/design/state-secretary-handoff-visibility.md` |

### 10.2 🟡-1 实现（逐文件读守卫）

- **问题**：`list_pending` 只守目录级 `listdir`；`get()` 内部 `exists → open`
  窗口被并发删除（或半写/损坏）时 `FileNotFoundError` / `JSONDecodeError` 逃逸，
  与 docstring「不可读即返回空列表」口径不一致。
- **改法**：单条读取包 `try/except (OSError, ValueError)` → **跳过该条**，不逃逸
  （与 `session.py::_read_json_dict` 同纪律）；单条异常不影响其余卡列举。
- **语义影响**：无新增行为面；仅把「裸异常逃逸」改为「跳过」。方法不在
  `delete_session` 关键路径（仅 docstring 提及），无数据破坏后果。

### 10.3 🟡-4 实现（尾锚 `$` → `\Z`）

Python `$` 匹配**尾换行之前**，`"abc\n"` 原被判合法。**同族 pattern 全部收紧**
（避免只改一处留同形缺口）：`handoff._CARD_ID_PATTERN`、`manager._SESSION_ID_PATTERN`、
`arbiter._IDENTIFIER_PATTERN`、`content_archive._IDENTIFIER_PATTERN` /
`_REF_PATTERN`、`session.py` 内联 `session_id` / `content_ref` 两处 match。

### 10.4 新增回归（5 项，含变异验证）

| 用例 | 断言 | 变异判别力（实测） |
|---|---|---|
| `TestListPendingPerFileReadGuard::test_file_removed_between_exists_and_open_is_skipped` | 单条被并发删除 → 跳过，其余仍列出 | 移除守卫 → **FAIL**（`FileNotFoundError`） |
| `...::test_corrupt_json_entry_is_skipped` | 半写 JSON → 跳过 | 移除守卫 → **FAIL**（`JSONDecodeError`） |
| `TestIdentifierTrailingNewlineRejected::test_card_id_trailing_newline_rejected` | 尾换行 `card_id` 拒绝（读/删均拒） | 回退 `$` → **FAIL** |
| `...::test_session_id_trailing_newline_rejected` | 尾换行 `session_id` 拒绝 | 同上 |
| `...::test_unit_name_trailing_newline_rejected` | 尾换行单元名 → `ValueError` | 同上 |

- 变异矩阵：2 个变异体分别触发 **2 / 1 项 FAIL**；本实现上 **5/5 PASS** → 非恒真。
- 全量 `pytest -q`：**266 passed ×3**（门面 119 = 114 + 5，独立探针 65）；`compileall` 通过。

### 10.5 🟡-1 自然并发实证（本角色独立探针，不复用作者/测试用例）

口径：**多实例同 `data_dir`**（3 写 + 3 读 × 8s，读线程每轮新建独立 `HandoffBus`
实例——同实例共享 `RLock` 不逃逸），统计 `list_pending` 逃逸异常数：

| 版本 | `list_pending` 调用数 | 逃逸 `FileNotFoundError` | 逃逸 `JSONDecodeError` |
|---|---|---|---|
| 基线 `e3195ea`（无逐文件守卫） | 34,063 | **6,976** | **174** |
| 收口后（本提交） | 37,353 | **0** | **0** |

### 10.6 🟡-3 就绪修法（**§十二 已落地**）

- **顾虑回应**：上游 §8.3 认为回滚须判「是否本次新建」属**行为变更**——已就绪
  且**不越界**的实现：`create_handoff` 在 `get_or_create` **之前**记录
  `ho_card_preexisted = sm.get_card(state_card_id) is not None`；fail-fast 回滚时
  仅当 `not ho_card_preexisted` 才 `delete_card`（连带 `.lock` / `.owner.json`）。
  **预存在共享卡绝不误删**，仅回收本次调用新建者。
- **落地时须同步**：改写 `TestFailFastRollbackBoundary::test_fail_fast_leaves_ho_card_known_boundary`
  （现断言"残骸留盘" → 转为"残骸已回收"）+ 契约文档 §4/§8.3 口径。
- **落地状态**：评审复评第 5 轮明确要求「🟡1 fail-fast 回滚 ho-*」为**最低要求**之一，
  故已在 **§十二** 按上述就绪修法落地（含反向对照回归）。

### 10.7 计数同步

- 全量 **266 项** = 8 + 15 + 65 + 119 + 19 + 20 + 12 + 8（门面 114 → **119**）。
- 同步 `state-secretary/README.md`、`docs/design/state-secretary-index.md`、
  `docs/design/state-secretary-module.md` 三处（261 → 266；门面 114 → 119）。

---

## 十二、Issue #4 复评第 5 轮 · M-1/M-2 验收口径补收口 + 🟡1–🟡5（PR #7）

> 对象：评审 `8790e5f` 增量复评（`fix_bug` 打回）。承接 §九（🔴A/🔴B）后，
> 按评审 M-1/M-2 验收口径与「🟡1–🟡5 请一并登记（🟡1/🟡4 为最低要求）」补收口。

### 🔴 M-1 验收口径（点前缀名 × 3 触发 in-root → 0 破坏）

- **本角色独立探针**（不复用评审/测试用例）：5 名（`.data`/`.cache`/`.secretary`/
  `.state`/`.queue`）× 3 触发（`legacy_no_registry`/`registry_failed`/
  `ref_externally_removed`）= 15 组。
- **基线 `8790e5f` 对照**（`git archive` 单独检出 src 运行）：**15/15 破坏**；
  修复后 **0/15**。
- **评审最小复现**（原样复跑）：双通道占位 + `HandoffBus(data_dir=root/".cache/handoffs",
  state_root=root)` → 交接卡存活=True、ho-* 存活=True → **不变量保持**。
- 回归：`TestHiddenDirVisibility::test_reviewer_acceptance_hidden_names`（15 项，
  已参数化评审口径 5 名）；`.git` 承载真实卡 3/3 可见；剪枝收益对照（隐藏目录内
  伪 JSON 不产生伪引用）通过。
- **变异判别力**：还原裸名剪枝分支 → 该组 **18/18 FAIL**（已验证）。

### 🔴 M-2 深度口径与常量单一语义源

- **常量**：新增 `_KEPT_SKIP_MAX_CARD_DIR_DEPTH = _SKIP_DIR_PROBE_DEPTH - 1`；
  `_exceeds_kept_skip_depth` 判定由 `best >= _SKIP_DIR_PROBE_DEPTH` 改为
  `best > _KEPT_SKIP_MAX_CARD_DIR_DEPTH`（语义等价、单一来源）。
- **文档口径按实测更正**：§3「嵌套 ≥ 3 层不可见」→「**卡目录**相对保留根层级
  **> 2 层**不可见」（原口径把形状判定的**含文件层**深度误当卡目录层级）。
  实测：`<root>/node_modules/a/b/handoffs`（相对层级 2）可见；
  `<root>/node_modules/a/b/c/handoffs`（层级 3）不可见。
- 回归：`TestM2DepthBoundConsistency`（7 项：常量派生 1 + 2 层可见 3 + 3 层边界 3，
  修复前 6 FAIL）。

### 🟡1–🟡5 处置

| # | 项 | 处置 | 回归 |
|---|---|---|---|
| 🟡1 | fail-fast 只回滚交接卡 → 累积孤儿 `ho-*` | **已修**：回滚**本次新建**的 `ho-*` 卡（`delete_card` 清 `.json`/`.lock`/`.owner.json`）；**先已存在的共享卡不动**（以「是否本次新建」判定） | `TestFailFastRollbackBoundary`（3：连续 3 次失败零残留 + 反向对照 + 交接卡回滚） |
| 🟡2 | 非 `OSError` 异常逃逸 `delete_session` | **已修**：扫描守卫 `except OSError` → `except Exception`（契约 §2「扫描异常不得逃逸」收口为全异常族） | `TestYellow2NonOserrorScanEscape`（3：`TypeError`/`RuntimeError`/`ValueError`） |
| 🟡3 | 保守跳过回收后不可"再判定" | **登记 + 口径固化**：确认无"新实例自动回收"路径（内存态 + 台账已撤）→ 契约明确为「不误删；须显式清理或本实例重试」 | `TestYellow3ConservativeSkipSemantics`（1） |
| 🟡4 | 白名单收紧兼容面未文档化（id 含 `.`/非 ASCII） | **登记 + 文档化**：契约 §5 第 4 条补「盘点 `handoffs/*.json` 文件名（= `card_id`），不符白名单者先清理/改名/补偿登记；**未完成盘点不得启用共享卡回收**」 | `TestYellow4LegacyCardIdCharset`（2） |
| 🟡5 | 回执与实现/历史不符（符号/类名） | **本轮回执按实际符号**；复核 `_ALWAYS_SKIP_SCAN_DIR_NAMES` / `_AMBIGUOUS_SCAN_DIR_NAMES` / `_contains_data_root` / `TestAmbiguousDirNameNoBarePruning` / `TestReferenceScanFailureContainment` / `TestRefRegistrationFailFast` / `TestHandoffCardIdTraversalGuard` 在**全历史**（`git log --all -S`）命中数均为 **0** → 旧回执为误记，此处更正 | — |

### 自测

- 全量 **295 passed** = 8 + 15 + 65 + 148 + 19 + 20 + 12 + 8
  （`batch_persist` / `content_archive` / 独立探针 / 门面 / `arbiter` /
  `state_manager` / `ownership` / `state_tree`）。
- 本轮新增（门面 119 → 148，+29）：M-1 验收 15 + M-2 7 + 🟡1 3（原 2 改 3）+
  🟡2 3 + 🟡3 1 + 🟡4 2（-2 旧边界用例改写）；`TestListPendingPerFileReadGuard`（2）/
  `TestIdentifierTrailingNewlineRejected`（3）为上游 §十一 既有项，合并后一并计入。
- `compileall src` OK；`pyflakes`（改动文件）0 告警。
- 性能：2000 目录 `.git` / `node_modules` 扫描 ~1.2–1.7 ms（非保留目录 ~66 ms）
  → **剪枝收益不退化**。

---

## 十三、Issue #4 复评第 6 轮 · 架构·测试独立复测（`e3195ea` / 增量至 `2a430e1`）

> 触发：架构·评审接力卡「复评 #6（对象 = 新 head `e3195ea`）— 🔴A/🔴B 清零，放行」。
> 本角色**不复用**评审/开发用例，全部以自写独立探针复跑（探针源码见回执附注），
> 并按「多 seed + 跨版对照 + 判别力」三口径复核。

### 13.1 测试矩阵（功能 / 性能 / 安全 / 联调 / E2E）

| 面 | 探针口径 | head `e3195ea` | 基线对照 | 结论 |
|---|---|---|---|---|
| **功能** 🔴A | 固定布局（`build/decoy.json` + 受害者卡 `build/dist/x/y` + 双通道 `.ref` 删除）× **12 seed** | **0/12 破坏** | `bca4193` **5/12 破坏**（seed 1/3/8/9/11） | ✅ 清零且跨 seed 稳定 |
| **功能** 🔴B | 5 隐藏名（`.staging`/`.cache`/`.data`/`.build`/`.git`）× 3 触发 | **0/15 破坏** | `bca4193` **10/15 破坏** | ✅ 清零 |
| **功能** 旧🔴 P0-1 | 10 非隐藏剪枝名 × 3 触发（in-root） | **0/30** | `bca4193` 0/30 | ✅ 无回归 |
| **功能** 旧🔴 P0-2 | `listdir` OSError 注入 + 12 线程 × 200 轮 | **0 逃逸 / 0 残骸 / 0 异常** | — | ✅ 无回归 |
| **安全** 旧🔴 P1-3 | 读/删/写/台账 四面（含台账 `handoff_ids` 注入） | **4/4 全拦截** | — | ✅ 无回归 |
| **联调** 混合嵌套 | `.git/build`、`build/.cache`、`node_modules/.venv`、`dist/.staging`、`.cache/target`、`build/dist/node_modules` × 6 seed | **0/36 破坏** | — | ✅ |
| **E2E** 随机压测 | 120 轮多会话/多布局交错（10 布局 × 4 触发） | **234 格 0 违反 / 0 异常** | — | ✅ |
| **性能** 剪枝收益 | `.git` 5000 对象目录×3 文件 / `node_modules` 2000 目录 / 20 万文件 monorepo | `0.0015s` / `0.0010s` / `0.79s`；伪引用 **0** | `bca4193` `0.0001s` / `0.0010s` / `0.80s` | ✅ 收益不退化（绝对量级 <2ms） |
| **性能** 规模有界 | `.git` 1k / 5k / 20k 对象目录 | `0.0030s` / `0.0015s` / `0.0012s`（**有界，不随规模增长**） | — | ✅ |
| **并发** | 16×400 / 8×800（模块级锁） | **0 异常 / 0 孤儿 / 0 锁残留** | — | ✅ |

### 13.2 判别力验证（防恒真）

- 将 head 的 `tests/test_secretary_session.py`（含新增 24 项判别性回归）**移植到旧 head `bca4193`** 运行：
  **9–11 FAIL**（`TestHiddenDirVisibility` 8 项 + `TestKeptSkipDepthOrderIndependence` 2 项 +
  多 seed 端到端 1 项，随 `PYTHONHASHSEED` 在 9–11 间波动）→ **非恒真、有判别力**。
- 移植 `2a430e1` 用例至 `bca4193`：**30 FAIL** → 后续增量同具判别力。

### 13.3 缺陷分级

- 🔴 **P0：无**。🔴A（12 seed 0 破坏）/ 🔴B（0/15）/ 旧 🔴 三项 独立复跑全清零。
- 🟠 **P1：无**。P1-3 四面全拦截；扫描异常族收口后 `delete_session` 不逃逸。
- 🟡 **P2（结转，均非本 diff 引入或属迁移面，不阻断合并）**：
  1. **🟡-3 非 ASCII / 含点单元名存量卡 → 永久残骸**（**独立复核属实，维持 🟡**）：
     - head 上 `create_handoff("架构·评审", "架构·测试", …)` 抛 `ValueError`（`main` 可正常建/读/删）；
     - 跨版矩阵：`main 建 → head 删` 时该卡**留盘**（`card_alive=True`），`get`→`None`、`delete`→`False`
       → **无法经 API 清理**；`main 建 → main 删` 则正常回收（`card_alive=False`）。
     - 该存量卡若引用有效 `ho-*` 卡，则**永久阻塞**共享卡回收（head/newhead 实测
       `ho_alive=True`、二次 `delete_session` 后仍 `True`）；**不变量仍保持**（未误删）。
     - **不升级为 🔴**：属**迁移面**（存量数据），非本 PR 新增缺陷；且 §5 第 4 条已文档化
       「盘点 `handoffs/*.json` 文件名，不符白名单者先清理/改名；未完成盘点不得启用共享卡回收」。
     - **建议 Issue 一并要求**：契约补「旧版非 ASCII 单元名卡迁移/清理脚本或清单」，
       并评估放宽为「仅拒绝路径分隔符/相对路径」而非全字符白名单（现白名单同时拒绝了
       业务上合法且本体系实际使用的 `架构·评审` / `秘书·开发` 等中文单元名）。
  2. **🟡-1 `get()` 直读路径未守卫 → `list_pending` 并发仍可抛 `TypeError`**（**独立复核，维持 🟡**）：
     - `2a430e1` 已将 `list_pending` 的单条读取包 `try/except (OSError, ValueError)`，
       损坏 JSON（`JSONDecodeError`）**已不逃逸**（head 上 3×400 轮 → 1200 次逃逸；`2a430e1` → 0–2 次）。
     - **残余**：合法 JSON 但**字段不全**（如并发截断写恰好落成 `{"id":…,"source_unit":…,
       "target_unit":…}` 缺 `intent`）→ `HandoffCard(**data)` 抛 `TypeError`，
       该异常**不在** `(OSError, ValueError)` 之列 → 仍逃逸（`get`/`list_pending` 均可达；
       `main`/`bca4193`/`head`/`2a430e1` 同形，**baseline 既有面**）。
     - **不升级为 🔴**：非本 PR 引入（`main` 同形）、不在 `delete_session` 关键路径、
       需外部非原子写者或跨实例并发。建议读侧统一 `except Exception` 或 `HandoffCard`
       构造加字段校验（`from_dict` 缺字段降级/跳过）。
  3. **🟡C fail-fast 回滚面**：`e3195ea` 上双通道全失败 + 根外布局 → 回滚交接卡，
     但 `ho-*.json` + `.lock` **留盘且不可回收**（我独立复验属实）；**`2a430e1` 已修复**
     （仅回滚本次新建的 `ho-*`，共享卡不动，实测零残留）→ 该 🟡 在 `2a430e1` 闭环。
  4. **🟡 深度 ≥3 / 条目预算 2000 边界**：独立实测 `build/n0/n1/n2`（卡目录相对层级 3）
     与 2100 兄弟目录仍**不可见** → 不变量破坏；文档 §3 已文档化 + 用例锁定 → 口径闭环，
     仅作已知边界留痕（`2a430e1` 已把深度口径按实测更正为「卡目录相对层级 > 2 不可见」）。
  5. **🟡-4 E501 口径不可复现**：仓库**无** `.flake8`/`setup.cfg`/`tox.ini`/`pyproject.toml`。
     按 flake8 默认 79：`e3195ea` 新增行 5 项 E501、`2a430e1` 新增行 4 项；
     按 `--max-line-length=100`：两 head 新增行 **0 告警**。→ 建议落 `.flake8` 固化行长，
     否则「静态告警清零」不可复现。

### 13.4 计数口径核对

- `e3195ea`：实测 **261 passed** = 8 + 15 + 65 + 114 + 19 + 20 + 12 + 8
  （门面 **114** / 独立探针 **65**）→ 与 README / index / module / 台账四处**一致** ✅
- 分支增量至 `2a430e1`：实测 **295 passed** = 8 + 15 + 65 + **148** + 19 + 20 + 12 + 8
  （门面 148 / 独立探针 65）→ 与四处**一致** ✅
- `PYTHONHASHSEED=0..3` 全量复跑：261 / 295 均全绿（无 seed 依赖）。
- `compileall src` OK；`flake8 --max-line-length=100`（改动文件）0 告警。

### 13.5 信号灯

- 🟢 **通过（放行合并）**：`e3195ea` 无 🔴 / 🟠；🔴A（12 seed）/ 🔴B（0/15）/ 旧 🔴 三项
  独立复跑全清零；判别力经跨版移植验证（10–11 FAIL）；E2E 234 格 0 违反；
  261 passed 四处计数一致。
- 🟢 **增量 `2a430e1` 同样放行**：🔴A/🔴B 0 破坏；🟡C / 🟡1（JSONDecodeError 面）/ 🟡2 已闭环；
  295 passed 计数一致。
- 🟡 结转 5 项（§13.3），**均非本 diff 引入或属迁移面 → 不阻断合并**，建议随 Issue #8 排期；
  其中「非 ASCII 单元名」建议在 Issue 内一并给出迁移清单与白名单放宽评估。
