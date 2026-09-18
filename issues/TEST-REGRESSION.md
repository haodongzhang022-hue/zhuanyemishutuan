# 修复纳入测试清单（测试回归任务台账）

> 用途：将评审/测试过程中**修复过的东西**主动纳入本地回归测试。
> 本地拉取代码后，按本清单**逐项测试**，每个修复项明确"应该怎么测"。
> 维护：随每次修复/评审更新，测试完成后勾选。

## 关联

- 上游需求：ISSUE-001 状态管理秘书机制（Issue #1）
- 当前代码载体：PR #3 `SecretarySession` 会话门面（源分支 `auto/session-facade-9b95`）
- 测试基线：全量 pytest **445 项全部通过**（门面 **148 项** = 90 + 24 复评第 5 轮判别性
  回归 + 5 🟡 收口回归 + 29 M-1/M-2/🟡1–🟡5 补收口，独立探针 65 项，Issue #4 验收矩阵 13 项，
  **Issue #14 循环熔断 62 项** + **Issue #14 评审仲裁/审查范围 75 项**；底层模块回归 8 + 15 + 19 + 20 + 12 + 8）。
  历史口径：`28ca7f6` 228（门面 88）/ `60e32ec` 230（门面 90）/ `bca4193` 237（门面 90）
  / `e3195ea` 261（门面 114）/ `63345ec` 266（门面 119）→ `2a430e1` **308**（含验收矩阵 13）
  → `60499d8` **370**（新增 Issue #14 循环熔断 62）→ **本轮 445**（新增评审仲裁/审查范围 75）。

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
## 十四、Issue #4 验收矩阵（架构·测试 门禁固化 · PR #7 `2a430e1` 基线）

> 由「架构·测试」接手复评转交（`73361ca` 收口轮）后新增：**独立检出 head +
> 自写探针（不复用作者/评审用例）+ 跨版对照**，并把探针固化为可执行门禁。

### 14.1 新增门禁文件

- `state-secretary/tests/test_acceptance_matrix_issue4.py`（**13 项**）
  - `TestG1RegistryRmdirWriteRace`（2）：🔴1 登记/注销互斥（丢失须 0）+ 根外并发建卡不误抛
  - `TestG2Source3ListdirGuard`（2）：🔴2 来源 3 `listdir` OSError + `isdir→listdir` TOCTOU 均不逃逸、残骸 0
  - `TestG3Source2Discriminative`（2）：来源 2 独立可见（清空双通道登记后仍保留）+ 反向对照（卡删后可回收）
  - `TestG4ShapeReverseAssertions`（4）：缺 `source_unit`/`target_unit`/`id` 不得计入（3 参数化）+ 完整形状对照
  - `TestG5LedgerLiveness`（2）：死会话台账不计入 + 存活会话台账对照
  - `TestG6KnownBoundaryWindowRace`（1）：🟡1 窗口竞态已知边界锚点（闸门置于 `_save` 之后）

### 14.2 跨版判别力（同文件回放到历史 head，实测失败面）

| head | 失败项 | 说明 |
|---|---|---|
| `de5eae8`(main) | G3 / G4×4 / G5×2（7） | 无 `_build_ho_reference_index` → 来源 2/形状/台账存活机制尚不存在 |
| `cc7f4fd` | G1×1 | 无模块级互斥 → 并发登记丢失 25/1200 |
| `8790e5f` | G1×2 | 丢失 17/1200 + 根外并发误抛 OSError |
| `a852574` | G1×2 | 丢失 8/1200 + 误抛 2 次 |
| `11dfdd9` | G1 / G2×2 / G4×3（6） | 来源 3 `listdir` 无守卫（抛 + 残骸）；形状未收紧 |
| `570c7ee` / `82807ff` | G1 / G2×2（3） | 来源 3 `listdir` 无守卫 |
| `6a2e713` | G3×1 / G4×1（2） | 扫描界不足 / 形状对照 |
| `73361ca` / `be400e6` / `2a430e1` | **0** | 全绿 |

### 14.3 本角色独立探针（自写，不复用用例）

| 探针 | `de5eae8`(main) | `8790e5f` | `cc7f4fd` | `73361ca` / `2a430e1` |
|---|---|---|---|---|
| A 登记静默丢失（6×300×3） | 无该机制 | **64/5400** | **66/5400** | **0/5400** |
| B 根外并发误抛（6×250×3） | 无该机制 | **2/4500** | **3/4500** | **0/4500** |
| C 来源 3 `listdir` 注入 | 无该机制 | 抛 + `ho-*.json/.lock` 残骸 | 抛 + 残骸 | **不抛、残骸 0** |
| D `isdir→listdir` TOCTOU | 无该机制 | 抛 + 残骸 | 抛 + 残骸 | **不抛、残骸 0** |
| E 来源 2 独立判别 | 无该机制 | 保留✅/回收✅ | 同 | 同 |
| F 形状缺字段（3 类） | 无该机制 | 不计入✅ | 同 | 同 |
| G 死会话台账 | 无该机制 | 不计入✅ | 同 | 同 |
| H 🟡1 窗口竞态（闸门 `_save` 后） | **3/3 破坏** | — | — | **3/3 破坏（已知边界）** |
| L 跨进程共享 state_root | — | — | — | **29–55/1800 丢失（进程内锁不覆盖）** |
| M 性能（8 线程 9600 ops / delete_session） | — | — | — | 1.43–2.08s / 30–52ms（同量级，无退化） |

**变异矩阵**（验证断言非恒真）：注入 M1 放宽形状 `id` 校验 → G4 失败；
M2 移除台账存活守卫 → G5 失败；M3 跳过来源 2 → G3 失败。本实现 7/7 PASS。

### 14.4 自测

- 全量 **308 passed** = 8 + 15 + 65 + 148 + 13 + 19 + 20 + 12 + 8
  （新增验收矩阵 **13** 项；连跑 3 次稳定）。
- `compileall src tests` OK；`pyflakes`（新增文件）0 告警。

---

## 十五、Issue #14 循环熔断（轮次预算 + 非必要修复最多 1 次 + 静默交接）

> 需求（Issue #14）：避免评审/修复无限循环——(a) 最大轮次；(b) **无必要修复的内容最多修 1 次**；
> (c) 熔断后交接给下一个 agent 时**不进行额外唤醒**。

### 15.1 新增门禁文件

- `state-secretary/tests/test_loop_guard_issue14.py`（**62 项**）

| 组 | 用例数 | 判据 |
|---|---|---|
| ① 门禁轮次上界 | 6 | 预算内 `fix`+唤醒；耗尽 `fuse`+不唤醒；熔断后恒熔断（`fused_now` 仅首爆）；`preview` 纯函数；`clear` 不耗预算；熔断终态不得降级为 `clear` |
| ② 非必要最多 1 次 | 3 | 首报 🟡 → `fix_last`（唯一一次唤醒）；再报 → `fuse`；门禁轮**重置**配额（反向对照：🟡→🔴→🟡 仍可修 1 次）；零容忍档 |
| ③ 修复总轮次硬上界 | 3 | 🔴/🟡 交替：**门禁预算仍在**却由 `total_left==0` 熔断（判别「挡住的是总上界」）；显式压上界；`clear` 不计入总预算 |
| ④ 静默交接 | 7 | `wake=False` 落盘跨实例保真（新 bus 读回仍 False）；熔断会话拒绝 `wake=True`（`RuntimeError`）+ 反向对照未熔断可唤醒；`wake="false"` 抛 `TypeError`；旧卡缺 `wake` 默认 True；未知字段忽略 |
| ⑤ 跨 agent 不复活 | 6 | 熔断态跨实例持久化；`inherit_silent_handoff(payload)` 继承轮次计数（4 轮 → `round_no=4`/`blocking_rounds=3`）并拒绝复活；**4 组反向对照**（`wake=true` 载荷 / 空串 / 坏 JSON / 数组 → 不熔断） |
| ⑤b 停滞感知 | 9 | 同一指纹第 2 次即熔断且**预算充足**（判别停滞而非轮次）；不同指纹不熔断；签名可选；`clear` 不推进停滞；阈值可配；阈值 0 拒绝；`resume` 清指纹 |
| ⑥ 人工解锁 | 4 | 空/匿名 `by` 抛 `ValueError`（禁止匿名复活）；解锁**重置预算**并恢复一轮修复能力；未熔断 `resume` 返回 False；显式 `fuse` 幂等 |
| ⑦ 持久化健壮性与生命周期 | 12 | 状态文件位置/`reload`；4 组损坏状态退化（`[]`/`"str"`/坏 JSON/`123`）；字段类型不符降级；5 组非法 `session_id` 拒绝；4 组非法预算拒绝；4 组非法计数拒绝；`delete_session` 清理熔断状态 + 复建不带陈旧熔断；默认状态目录在数据根下 |
| ⑧ 门面集成 | 3 | `summarize_for_upstream` 携带 `wake_allowed`/`loop` 快照；`preview_round` 不耗预算；门面 `fuse_loop`/`resume_loop` |

### 15.2 判别力（反向对照/边界佐证，防恒真断言）

| 断言 | 反向对照 / 佐证 |
|---|---|
| 总轮次上界拦住循环 | 同轮 `budget_left>=1`（门禁未耗尽）→ 放默认总上界时该轮为 `fix` |
| 停滞熔断（同指纹） | `advisory_left>=1` 且 `budget_left>=1`；不同指纹连续 4 轮**不**熔断 |
| 静默交接继承熔断 | 4 组非静默载荷（`wake=true`/空/坏 JSON/数组）均**不**熔断 |
| 门禁轮重置 🟡 配额 | 🟡→🔴→🟡 仍 `fix_last`（若改「全局只 1 次」则失败） |
| `wake` 严格 bool | `wake="false"` 抛 `TypeError` 且**不落卡**（`list_pending()==[]`） |
| 熔断不唤醒 | 熔断会话 `create_handoff(wake=True)` 抛错、`silent_handoff()` 仍可用 |

### 15.3 自测

- 全量 **370 passed** = 8 + 15 + 65 + 148 + 13 + **62** + 19 + 20 + 12 + 8（连跑 3 次稳定）。
- `python3 -m pytest tests/ -q` → 370 passed。
- `pyflakes`（新增/改动文件）0 告警；`compileall` OK。

---

## 十六、Issue #14 补全机制：评审版本仲裁 + 审查范围分级

> 需求（Issue #14 补充）：只在开启评审时记录版本；无法追踪变化就以新的；正在编辑给防同写锁
> 并延迟重试；**先完成该做的**、**只额外处理一个**解决冲突；**日常只审变化**，**全量只在大版本**。

### 16.1 新增门禁文件

- `state-secretary/tests/test_review_arbiter_issue14.py`（**75 项**）

| 组 | 用例数 | 判据 |
|---|---|---|
| ① 只在开启评审时记版本 | 5 | 开发期 `recorded()=False`；`begin_review` 记当前版本；**幂等不推前**（反向：`force=True` 才重立）；卡不存在拒绝；未开启即写 → REBASE |
| ② 先完成该做的 | 4 | 冲突时 `applied=True` 且**磁盘版本 +1**（反向：延迟窗 `applied=False` 且版本不变）；有效推进不冲突；非推进不误判 |
| ②b 推进判据参照物=基线 | 6 | 正常并发推进**不冲突**（回归护栏）；反向对照（同样输入无进展则冲突）；快照缺失宽进；坏快照不炸；轨迹 ≤2 条 |
| ③ 防同写锁 + 延迟 | 5 | 他人租约 → `should_retry` 且不落盘；窗内继续等；放租约后重试成功；**自己租约不延迟自己**；同向写入可重试不算冲突 |
| ④ 单冲突收口 | 6 | 连续 3 次冲突**仍 1 张卡**且 `merged_conflicts=3`；卡 ID 稳定；收口重立基线；空 resolution 拒绝；未知票据 False；重试成功作废旧卡 |
| ⑤ 以新为准 | 4 | 无基线 REBASE；版本退化 REBASE；无共同祖先不冲突；写者不明不冲突 |
| ⑥ 健壮性与生命周期 | 8 | 坏基线/坏卡退化；非法 session_id/延迟/reviewer；路径穿越票据拒绝；生命周期回收；默认目录 |
| ⑦ 审查范围分级 | 13 | 默认 `scan`（4 组参数化）；大版本 `full`（5 组）；显式"只要变化"**压过**大版本标签；`scan` 执行口径**不含全量**；门面接入 |
| ⑧ 规则与数据模型 | 8 | 同侧判定 6 组参数化；`ReviewBaseline`/`ConflictCard` 容错 |
| ⑨ 与循环熔断联动 | 4 | 冲突**不占**轮次预算；跨轮冲突卡恒 1 张；收口后走增量；熔断后评审仍落盘 |

### 16.2 判别力（反向对照 / 边界佐证）

| 断言 | 反向对照 / 佐证 |
|---|---|
| 开发过程不记版本 | 开启评审后 `recorded()` 为 True 且版本=启动时磁盘版本 |
| 幂等不推前 | 评审中追加提交后 `begin_review()` 版本不变；`force=True` 才变 |
| 冲突不阻塞落盘 | 冲突场景 `applied=True` **且**磁盘版本 +1；延迟场景 `applied=False` **且**版本不变 |
| 单冲突收口 | 3 次冲突后待处理卡**仍有且仅有 1 张**，`merged_conflicts==3` |
| 推进判据参照物 | 同样输入下"有新增任务 → 不冲突 / 无新增任务 → 冲突"，锁住参照物必须是基线快照 |
| 日常不全量 | 默认/常规文本 → `scan`；大版本标签 → `full`；"只审变化"文本 → `scan`（压过标签） |
| 冲突不占轮次 | 冲突前后 `blocking_rounds` 不变 |

### 16.3 实缺陷修复留痕（本轮）

判定"对方是否有效推进"的参照物原为 `disk_before`（**开始写回时**的最新卡）——它已含
对方并发写入，与写回后相比（写回只把版本 +1、任务集不变）→ **恒为"无进展" → 每次并发
都误判冲突、每次都开冲突卡**（恰是要治的循环）。改为以 `ReviewBaseline.history` 的
**基线快照**为参照；回归护栏见用例组 `TestProgressReferenceIsBaseline`。

### 16.4 自测

- 全量 **445 passed** = 8 + 15 + 65 + 148 + 13 + 62 + **75** + 19 + 20 + 12 + 8（连跑 3 次稳定）。
- `python3 -m pytest tests/ -q` → 445 passed。
- `pyflakes`（新增/改动文件）0 告警；`compileall src tests` OK。

---

## 十七、Issue #14 追问：检查点探测（非轮询探测点，最多 6 次）

> 需求（Issue #14 追问）：最多 6 次；**非轮询**而是**形成检查点 + webhook 触发再跑**；
> **等他的锁**；**判断检查点**；类似**边缘计算的探测器**；平台有无最佳实践。

### 17.1 新增门禁文件

- `state-secretary/tests/test_checkpoint_probe_issue14.py`（**38 项**）

| 组 | 用例数 | 判据 |
|---|---|---|
| ① 最多 6 次预算 | 5 | 默认上界 = 阶梯档位数 = 6；计划 = 6 次 + 1 条交回人工；**反向：第 1..5 次不判用尽**；用尽后再探不唤醒；**重建不清零预算** |
| ② 非轮询落点 | 7 | 短等待挂 `cnb:await`（零占用）；长等待交 `crontab`（平台代跑）；**反向：cron 间隔不低于平台 5 分钟下限**；第 6 次 = 10min/**/10**；落点无 `local_loop`；等待中给的是**平台指令**；非法落点入口拒绝 |
| ③ 等锁 + 判断检查点 | 9 | 锁在→`wait`、锁释放→`proceed`；**反向：锁表不可见→`unknown`，不得当"已释放"**；我方不需要→`release`（反队头阻塞）；确认型缺事实→`unknown`；版本变化→`proceed`（反向：未变→`wait`）；事件信号；**自述不采信**；`unknown` 处置 = 继续等 |
| ④ 平台实践 + 计费 | 6 | 落点均为平台原生；crontab 下限来自平台文档；一行探测计划；`proceed` 触发**真正干活**（`cnb:apply api_trigger_probe_resume`）而非再等一轮；用尽不唤醒；等待零占用（反向：本地空转 10min ≈ 1.33 核时） |
| ⑤ 观测与生命周期 | 7 | `checkpoints_overview` 给出剩余次数/下一次等待/避开清单；已决检查点退出未决清单；**`delete_session` 连带清理**；非法 session_id 拒绝；坏文件容错；预算与阶梯同源 |

### 17.2 判别力（反向对照 / 边界佐证）

| 断言 | 反向对照 / 佐证 |
|---|---|
| 最多 6 次 | 第 1..5 次 `resume_required=False`；第 6 次才 True 且 `checks_left=0` |
| 幂等不重置预算 | 重建后 `checks_used` 保持 1（重置即把"6 次"变成无限次） |
| 非轮询 | 计划里 `local_cpu` 全为 0；`local_loop`/`local_sleep` 都不在落点集里 |
| cron 不低于下限 | 逐行解析 `*/N` 并断言 `N*60 ≥ 300` |
| 等锁判定 | 锁表可见且持有→`wait`；不可见→`unknown`（**不得**判 `proceed`） |
| 版本快照参照物 | 同样形状输入必须被区分：`1→2` 判 `proceed`，`1→1` 判 `wait` |
| 下一次等待 | 第 N 次后的下一次取**下一档**（防回落重复档） |
| 自述不入判定 | 传 `agent_says_done` 不进 `facts`、不改判定 |
| 唤醒闸门 | `proceed` 才 `wake_allowed=True`；等待中/用尽恒 False |

### 17.3 实缺陷修复留痕（本轮）

1. **版本判别恒为"未变"**：原实现先把本次采集结果写进 `last_facts` 再比较，
   `prev == cur` 恒成立 → "版本变了"永远判不出。改为判定使用**探测前快照**
   （`prev_facts` 在采集前取），并加护栏用例 `test_version_reference_is_pre_probe_snapshot`
   （同样输入 `1→2` 与 `1→1` 必须判出不同结果）。
2. **下一次等待回落一档**：第 N 次探测后取 `ladder[N-1]`，等于把"刚等过的档"当下一次
   （看起来像降级/重复）。改为取 `ladder[N]`，护栏用例 `test_next_wait_uses_next_ladder_step`
   断言 `[30,60,180,360,600]` 且单调不减。

### 17.4 平台依据（已查证官方文档）

- `cnb:await` / `cnb:resolve`：**零占用挂起**，由状态变化唤醒（同一事件内配对）
  —— `/zh/build/internal-steps/cnb/await-resolve.md`
- `crontab`：最小间隔 **5 分钟**，执行身份 = 最后修改该配置的用户 —— `/zh/build/crontab.md`
- `cnb:apply` / `cnb:trigger` / OPENAPI `StartBuild`：触发 `api_trigger` 流水线（外部 webhook 再跑）
  —— `/zh/build/internal-steps/cnb/trigger.md`、`/zh/build/trigger-rule.md`
- 计费：只有 CPU 核时（实际运行时长）+ AI Credits（Token），**无"等待"项**
  —— `/zh/pricing.md`、`/zh/build/timeout.md`

### 17.5 自测

- 全量 **547 passed** = 445 + 62 + 35 + 29 分项（连跑 3 次稳定）；本轮新增 **38** 项。
- `python3 -m pytest tests/ -q` → 547 passed。
- `pyflakes`（新增/改动文件）0 告警；`compileall src tests` OK。
- `.cnb.yml`：YAML 语法 ✅ / 语义 ✅（Schema 告警为 HEAD 既有，未新增）。


---

## 十八、Issue #14 追问 · 只跑增量 / 同口径不重复（PR #15 分支）

> 触发：用户「平时不需要跑全量，而且尤其是不适合连跑三次…同一个口径几乎不会产生分歧…
> 我们已经把这一套只跑增量、不跑全量的建立过了呀，但可能在其他仓库没有形成，组织下通用的…
> 现在为什么没事按那个规则呀，是规则不健全吗？还是配置有问题？检查一下这个吧。」

### 18.1 问题定位：**四个缺口叠加**（规则不全 + 机制缺失 + 配置漏登记 + 流水线没接）

| # | 缺口 | 证据 | 修法 |
|---|---|---|---|
| 1 | 规则**只到"范围"没到"重复"** | `scan` 段只有 `major_version_tags` / `*_keywords`，无任何"同一口径别重复跑"的参数 | 规则包补 `default_mode` / `max_identical_repeats` / `repeat_keys` / `allow_stability_repeats` / `stability_via`（组织级 + 仓库级都补） |
| 2 | 规则是**口径不是机制** | `decide_scan` 只返回 `scan`/`full` 字符串；没有任何代码**拦**重复执行 | 新增 `collab/run_guard.py`（`RunScopeGuard`）：`skip_duplicate` / `blocked_stability` |
| 3 | 配置**白名单漏登记** | `_OVERRIDABLE_FIELDS` 是硬编码元组；实测仓库级写 `max_identical_repeats: 3` → 合并结果**仍是 1**（静默丢弃） | 补齐白名单（含漏掉的 `crontab_interval_minutes`）+ 自检 `_audit_overridable_fields()` |
| 4 | 流水线**没有测试阶段** | `.cnb.yml` 只有 `echo` + `node -v`；"只跑增量"**无处落地** | 新增 `scripts/ci-incremental-test.sh`，接入 `main.push` |

> 第 3 条正是用户问的"规则不健全还是配置有问题"：**两者都有**——
> 规则缺"重复"这一维，且新增字段被**静默丢弃**（写了不生效），
> 再叠加"没人真正执行它"。三层都不拦住，才会"没事就不按那个规则"。

### 18.2 本轮修掉的真缺陷（都配反向对照护栏）

| # | 缺陷 | 后果 | 护栏 |
|---|---|---|---|
| 1 | "上一轮不绿"用 `not _is_green(...)` 判 | **未知/空也算红** → 台账里只要没结论，同口径**无限重跑**，判重形同虚设 | `test_no_verdict_does_not_license_rerun` / `test_unknown_verdict_does_not_license_rerun`（反向）+ `test_explicit_red_verdict_licenses_one_rerun`（正向） |
| 2 | `window_repeats>1` 原按"上轮绿"才拒 | 上轮红时可"连跑三次" → 例外被当默认路径 | `test_window_repeats_three_blocked_even_when_red` |
| 3 | 仓库级覆写漏登记（见 18.1 #3） | "规则写了不生效" | `test_audit_is_discriminative`（故意移除字段 → 自检必须报出） |
| 4 | CI 受影响面读**工作区**而非 revision | 同一 revision 算出不同 impact → 每次当"新口径" → 判重失效（实测：同命令连跑 3 次全部放行） | 改 `git diff <base>...<REVISION>`；`test_impact_order_irrelevant` / `test_impact_duplicates_collapse` |

### 18.3 判定表（`RunScopeGuard.decide`）

口径指纹 = `mode + revision + impact + command`（**只有口径字段**进指纹）。

| 情形 | 判定 |
|---|---|
| `window_repeats > 1`（想连跑 N 次） | `blocked_stability`（**红/绿都拒**）+ 替代口径 |
| 缺版本 / 缺命令 | `run`（判不出就不拦） |
| 同口径次数 < 上界（1） | `run` |
| 达上界 + 上一轮**明确红** | `run` |
| 达上界 + 其余（含**没给结论**） | `skip_duplicate` |

### 18.4 端到端实测（`scripts/ci-incremental-test.sh`）

| 调用 | 结果 |
|---|---|
| 第 1 次（同口径） | `run` → 533 passed（增量目标，非全量） |
| 第 2 次（同口径） | `skip_duplicate` → `[ci] 本次跳过（不消耗构建/测试）` |
| 第 3 次（同口径） | `skip_duplicate` → 跳过 |
| `CNB_WINDOW_REPEATS=3` | `blocked_stability` + `targeted_probe/mutation_probe/independent_scenario` → 跳过 |
| 换基线（受影响面变） | `run` → 420 passed（新口径，正常放行） |

### 18.5 自测

- `pytest -q`：**616 passed**（基线 577 + 新增 `test_run_scope_issue14.py` **39** 项）。
- `pyflakes`（改动文件）0 告警；`compileall src tests` OK。
- 四个 YAML（`.cnb.yml` / `.cnb/settings.yml` / `shared/collab/*.yaml`）语法 + 语义 ✅。
- **本轮不连跑三次**：按新口径，同一口径第 2 次即跳过（自证口径生效）。

---

## 十九、PR #15 评审第 3 轮定点修复（🔴A–🔴D + 🟡1–🟡6）

> 触发：架构·评审 复评「4 项 🔴 未清零 → `fix_bug`，不转测试」。
> 评审基线：`2be6a3d`；修复提交见本 PR 最新 commit。

### 19.1 四项 🔴（独立探针复现 → 定点修 → 判别性回归）

| 项 | 缺陷（评审实测） | 修法 | 回归护栏（含反向对照） |
|---|---|---|---|
| **🔴A** 预算不跨容器/不跨棒 → 「最多 3 轮 / 6 次」永不触发 | ① 8 次接力各自新数据根 → 序列恒为 `fix×8`，**从不 fuse**；② 7 次平台探测 → `checks_used` **恒为 1**；③ `probe(checks_used=5)` 入参预算**不被消费** | 预算**随载荷走**：`LoopGuard.budget_payload()/budget_trail()/adopt_budget()`、`probe(key, external_checks=…)`/`adopt_checks_budget()`，`max()` 合并（只增不减）；`from_handoff` **不论 wake 真假都先合并轮次**；`ProbeEvent.budget_trail` 随评论/交接卡/事件持久传递；`.cnb.yml` 的 `probe_task` 明列"取留痕 → 合并 → 写回评论"三步 | `test_eight_relays_in_new_containers_eventually_fuse`（序列**必须出现 fuse**，且第 4 次即拦）/ `test_seven_probes_in_new_containers_reach_the_ceiling`（`checks_used`→6 且 `resume_required`）/ **反向**：`test_without_payload_budget_resets_per_container`（不传载荷确实归零 → 证明前者有判别力）/ 三种载荷形态 / 单调性 / 端到端静默交接 → 新容器继承 |
| **🔴B** 静默交接 `is not False` → `"false"/0/"no"` 被判「可唤醒」 | 三组非 bool 实测 `is_fused()=False`（未熔断），与 `create_handoff` 严格 bool **写出侧严、读入侧松**自相矛盾 | 新增 `parse_wake()` 三态（只认严格 bool）；**判不出 → 保守按静默**并留痕；无 `wake` 键的普通 context 仍走常规接力（不误熔断） | `test_non_bool_wake_is_conservatively_silent`（7 组参数化）/ **反向**：`test_strict_true_still_awake` / `test_missing_wake_key_is_not_unknown` / `test_write_side_and_read_side_same_bar` |
| **🔴C** `Checkpoint`/`ProbeEvent` 缺字段级容错 → 合法 JSON 类型不符即裸抛并逃逸 | `{"checks_used":"NaN"}` 使 `load/list_open/overview/create` 与门面四入口、`dispatch_shared` **全部抛 `ValueError`** | 两个 `from_dict` **逐字段守卫**（`_as_int/_as_float/_as_str/_as_bool/_as_dict/_as_list`，与 `LoopGuard`/`LockRegistry` 同族）；`dispatch_shared` 包守卫 → `unknown`（继续等，绝不误放行） | `test_read_paths_do_not_raise` / `test_facade_does_not_raise` / `test_dispatch_shared_tolerates_bad_types` / **反向**：`test_bad_fields_degrade_to_defaults`（确实退化默认，不是被静默当有效值）/ `test_dispatch_shared_survives_garbage` / `test_same_bar_as_loop_guard_family` |
| **🔴D** 规则包数值"被读但不生效" / 同一份规则出两套动作 | D-1 `locks.retry_delay_sec` 被读但**不生效**（`effective_delay` 取 `backoff.delay_for(0)`）；D-2 跨仓 `dispatch_shared` **不读** `crontab_interval_minutes`（硬编码 5）→ 本地 `*/15` / 跨仓 `*/5` | D-1：锁窗口/评审延迟窗改取 `rules.lock_retry_delay_sec`（默认 10s = 阶梯第一档）；D-2：`dispatch_shared` 缺省从 `rules` 取 cron 间隔（`crontab_minutes_from_rules`），并统一 `_driver_for_delay` 的默认值来源；`collab_status()["locks"]` 增 `rules_retry_delay_sec` / `explicit_override` | `test_lock_retry_delay_override_takes_effect`（30 → 生效值 30 且快照同步）/ `test_crontab_interval_local_and_cross_repo_agree`（本地与跨仓**同为 `*/15`**）/ **反向**：`test_explicit_param_still_wins` / `test_crontab_floor_still_clamped` / `test_split_threshold_from_rules_applies_cross_repo` |

### 19.2 🟡1–🟡6 + 🟢

| 项 | 修法 |
|---|---|
| 🟡1 恒真断言 | `assert got.probe_drivers[0] == "platform_await" or True` → 可失败断言（逐字段比对 + 长度 >1）；并加"源码里不得再有 `or True`"的护栏（**只扫代码行**，跳过注释） |
| 🟡2 `dispatch_shared` 有 IO 副作用 | `CheckpointProbe(mkdir=False)` 惰性建目录：落点计算不再在 cwd 造 `data/probe/`（`_save` 前才 `_ensure_dir()`）；回归 `test_y2_dispatch_shared_has_no_io_side_effect` |
| 🟡3 `.await_lock_task` 锚点无引用 | 新增 `api_trigger_probe_wait` 事件承载 `*await_lock_task`（await 行不带 cron，与 crontab 行**互斥不混用**）；回归 `test_y3_await_anchor_is_referenced` |
| 🟡4 文档「延迟 1 分钟」口径冲突 | 三份文档 + 两份规则包 + 角色提示词统一为「**延迟首档 10s**（阶梯第一档，可达 1 分钟）」；回归 `test_y4_docs_single_lock_delay_claim` |
| 🟡5 无 PyYAML 兜底解析读不全 | `_mini_yaml` 重写：支持**多行 inline list**（括号配平）、**折叠/字面块标量**（`>-`/`\|`）、块序列里的**嵌套映射兄弟键**、空值自动判 list/dict；回归用**真实规则包**验证（`waiting_note != ">-"`、drivers 5 项） |
| 🟡6 死代码 | 删除 `probe._SAFE_ID`（未使用）与 `_cron_every` 别名；回归 `test_y6_dead_code_removed` |
| 🟢 `checkpoint_plan(**kwargs)` 静默忽略 | 改具名形参 `checks=` 并**真透传**（未知关键字直接 `TypeError`）；回归 `test_checkpoint_plan_kwargs_now_named_and_real` |
| 🟢 嵌套映射丢字段 | 见 🟡5（`test_y5_nested_map_inside_sequence_item`） |
| 附带加固 | `dispatch_core` **按本地规则重算落点**（不轻信事件自带 `driver`，否则跨仓写 `driver=platform_await` 就能把 10 分钟等待挂成单 await = 死等）；`LoopGuard.from_dict` 的 `fused` 改严格 bool（**此改动在第 4 轮被判定为方向反了 → 已改为保守读取，见 §二十 🔴4**） |

### 19.3 自测

- `pytest -q`：**789 passed**（rebase 到 `89d48bd` 后：上游新增 127 项 + 本轮新增
  `test_collab_round3_fixes_issue14.py` **46** 项），连跑 3 次稳定。
- `compileall src tests` OK；`pyflakes`（本轮改动文件）**0 告警**。
- 四个 YAML（`.cnb.yml` / `.cnb/settings.yml` / `shared/collab/*.yaml`）语法 + 语义 ✅。
- 独立探针（**非复用作者测试**）复现 → 修复后复测：🔴A 序列 `fix,fix,fix,fuse,...`、
  `checks_used`→6 且 `resume_required=True`；🔴B `"false"/0/"no"` 全熔断；🔴C 全路径不抛；
  🔴D 覆写 30 → 生效 30、cron 本地/跨仓同为 `*/15`。

---

## 二十、PR #15 复评第 4 轮定点修复（🔴1–🔴4 + 🟡1–🟡5）

> ⚠️ **本节 🔴1 / 🔴2 两行结论已被实测推翻，请看 §22.5 的更正**（复评第 4 轮实测）：
> 🔴1 的 ①「有 `rounds` 就提前 return」**仍在**（`trail(3)+card(6)` → `round_no=3`）；
> 🔴2 当时是「首个含 `wake` 者胜」（**卡+卡顺序决定结论**，一个方向 fail-open）。
> 两行原文的「已清零」只表示当轮做了修复动作，**不等于判据成立**。

> 触发：架构·评审 复评第 3 轮（基线 `ed62088`）判 `fix_bug`——**🔴 未清零，不转测试**。
> 本轮只动 `89d48bd..ed62088` 的**新增/改动行**，修复提交见本 PR 最新 commit。

### 20.1 四项 🔴（独立探针复现 → 定点修 → 判别性回归）

| 项 | 缺陷（评审实测） | 修法 | 回归护栏（含反向对照） |
|---|---|---|---|
| **🔴1** 预算通道只取**第一条**留痕 → 评论正文多留痕叠加时轮次**回退**，「最多 3 轮 / 6 次」永不触发 | `BUDGET_TRAIL_RE.search()` / `PROBE_BUDGET_TRAIL_RE.search()` 只取第一条（= 最旧轮次）→ 8 棒正文累积形态恒 `fix×8`；探测 `checks_used` 卡在 1 | 两处都改**取全部留痕、逐字段 `max()` 合并**（`_merge_budget_dicts` / `_coerce_checks_used` 收集全部匹配）；交接卡 `context` 内嵌的 `budget_trail` **递归展开后合并**（不是替换）；`probe` 侧同样展开 `budget_trail` 字段 | `test_comment_body_with_many_trails_still_fuses`（8 棒必须出现 fuse 且第 4 次即拦）/ `test_order_of_trails_does_not_matter`（留痕在前/在后 → 轮次一致）/ `test_probe_budget_from_comment_body_with_many_trails`（`checks_used`→6）/ `test_handoff_context_embedded_trail_merges` / **反向**：`test_last_only_payload_agrees`、`test_probe_without_payload_stays_at_one` |
| **🔴2** `extract_json_object` 只取**第一个**对象 → 留痕在前时静默交接 `wake=False` 读不到 → `should_wake_next()=True`（熔断语义从新入口漏出，方向是**放行**） | `text.find("{")` 起只解析第一个配平对象；门面 `inherit_silent_handoff(trail + context)` 同结论 | 新增 `extract_json_objects()`（全部顶层对象）+ `pick_wake_object()`（**优先取含 `wake` 的对象**）；`from_handoff` 用它解析；找不到 `wake` 仍按"非静默"（普通 context 不误熔断），带 `wake` 但非严格 bool 仍走**保守静默** | `test_both_orders_inherit_silence`（参数化两种顺序）/ `test_facade_inherit_silent_handoff_both_orders` / `test_probe_trail_first_reads_event` / `test_pick_wake_object_skips_trailless_objects` / **反向**：`test_wake_true_payload_is_not_silent` |
| **🔴3** 新 `_mini_yaml` 值行含引号即跳过注释剥离 → 带引号注释的**覆写**被读成字符串 → 静默落回默认（生产无 PyYAML 路径；旧实现正确；本 PR 真实规则包 :77/:79 已命中） | `if '"' not in line and "'" not in line:` 才剥注释 → `retry_delay_sec: 30  # 覆写"锁窗口"为 30s` 整行当值 → 生效 10（文件写 30） | 新增 `_strip_inline_comment()`：**按 YAML 语义**（引号配对后再找 `#`，`#` 须在行首或前有空白）剥行内注释；`_mini_yaml` 三处调用点统一换用（主行 / 多行 inline list / 空值探测行） | `test_override_with_quoted_comments_takes_effect`（`locks/loop/probe/scan` **四域**都读到生效值）/ `test_parsed_values_are_typed_not_strings` / `test_real_rule_package_bools_are_bools`（真实规则包 :77/:79 读成 **bool**）/ **反向**：`test_unquoted_comments_still_stripped`、`test_hash_inside_quotes_is_not_a_comment` |
| **🔴4** `LoopState.from_dict` 的 `fused` 改严格 bool → 安全旗标 fail-open（`fused=1` 的坏卡把熔断**清掉** → 循环复活） | 本轮新增 `_bool("fused")`：非 bool → `False`（= 未熔断），与同 PR 判据"读入侧放宽等于把熔断语义漏出去"冲突 | `fused` 改**保守读取**：严格 `False` → 未熔断；**其余任何形态**（`1`/`"yes"`/`"true"`/`0`/`None`/缺失/坏值）→ 一律按**已熔断**（`data.get("fused") is not False`） | `test_non_bool_fused_means_fused`（8 组参数化：`is_fused()=True`、`should_wake_next()=False`、后续轮恒 `fuse`）/ `test_explicit_fuse_survives_bad_card` / **反向**：`test_strict_false_is_not_fused` |

### 20.2 🟡1–🟡5（同轮清掉）

| 项 | 修法 | 回归 |
|---|---|---|
| 🟡1 `adopt_budget` 嵌套递归无上界 → 深嵌套（~3000 层）`RecursionError` 逃逸到门面 | 新增 `MAX_COERCE_DEPTH = 8` 深度上界（`loop_guard` / `probe` 两处），超界按"判不出"（`None` / 不合并）处理；`json.loads` 的 `RecursionError` 一并兜住 | `TestCoercionDepthBound`（`adopt_budget` / 门面 `inherit_budget` / `dispatch_shared` / `_coerce_checks_used` 全不抛）/ **反向**：浅载荷仍被读到 |
| 🟡2 `api_trigger_probe_wait` 只有引用、**代码从不产出** | `dispatch_core` 的 await 分支补 `trigger["event"] = "api_trigger_probe_wait"`（crontab 分支仍是 `api_trigger_probe_check`）→ `.cnb.yml` 的口径与代码产出**同名同源** | `TestAwaitEventWired`（await 落点产出该事件名；**反向**：crontab 落点仍是 check 事件且带 `*/5`） |
| 🟡3 `Checkpoint.from_dict({"key": 5})` → `key=""`（坏 key 退化成**空标识**，互相覆盖） | 坏 `key`（非 str / 空白）→ 返回 `None`（**视为不存在**） | `TestBadCheckpointKey`（6 组坏 key → `None`；`load()` 不把坏卡当合法检查点）/ **反向**：合法 key 正常解析 |
| 🟡4 `ProbeEvent.from_dict` 的 `wake_allowed` 默认方向偏宽（`"yes"` → `True`） | 读入侧非严格 bool → **`False`**（保守：不得据此唤醒）；数据类默认值同改 | `TestWakeAllowedConservative`（8 组坏值 → `False`；默认构造亦 `False`）/ **反向**：严格 bool 原样读取 |
| 🟡5 `checkpoint_plan(checks=7)` 产出重复行（`[1..7, 7]`） | `keep = min(checks, len(rows) - 1)` 先夹到实际档位数再拼"用尽行" | `TestCheckpointPlanNoDuplicateRows`（`checks=7` → `[1..7]` 无重复；`checks=2` → 3 行）/ **反向**：非法 `checks` 仍 `ValueError`、未知关键字仍 `TypeError` |

### 20.3 附带收口

- `.cnb.yml` / `.cnb/settings.yml` / `docs/secretary-collab-rules.md` 写回口径补全：
  **追加写回同一条评论**（保留旧留痕，解析侧按 `max()` 合并 → 评论数不膨胀）；
  并明写"取全部留痕的 max()，不是第一条"、"留痕在前/在后两种顺序都认"。
- `tests/test_collab_confirm_issue14.py` 的 `import json`（`89d48bd` 起未使用，
  但它在本 PR 改动清单内）顺手清零 → 本轮改动文件 `pyflakes` **0 告警**（口径不含糊）。

### 20.4 自测

- `pytest -q`：**853 passed**（基线 `ed62088` 789 + 本轮新增
  `test_collab_round4_fixes_issue14.py` **64** 项），连跑 3 次稳定。
- `compileall src tests` OK；`pyflakes`（本轮改动文件）**0 告警**。
- 四个 YAML（`.cnb.yml` / `.cnb/settings.yml` / `shared/collab/*.yaml`）语法 + 语义 ✅。
- 独立探针（**非复用作者测试**）复现 → 修复后复测：
  🔴1 8 棒正文累积序列 `fix,fix,fix,fuse,…`、`checks_used`→6；
  🔴2 两种拼接顺序都 `fused=True / wake=False`；
  🔴3 带引号注释覆写四域生效（30 / 7 / 15 / true）；
  🔴4 `fused=1/"yes"/0/None` 一律 `is_fused()=True`；
  🟡 深嵌套不抛、await 事件名同源、坏 key→None、`wake_allowed` 保守、plan 无重复行。
- 兜底解析器 vs PyYAML **逐字段比对**（两份真实规则包）：差异 **0**。

---

## 21. 复评第 5 轮定点修复（PR #15 · 基线 `a7847f9`）

> 触发：架构·评审 复评第 4 轮 —— 本轮 🔴1–🔴4 已认可清零，但**第 3 轮同时提出的
> 🔴E–🔴H 本轮未处理、在 `a7847f9` 上逐项实测仍成立** → 处置标签 `fix_bug`。
> 本节只记该 4 项 🔴 与 5 项 🟡（+ 2 项 🟢）的修法与判别性回归。

### 21.1 🔴 四项（未清零项）

| 项 | 缺陷（评审实测） | 修法 | 回归 |
|---|---|---|---|
| 🔴E | 等锁判定把「台账**查不到这把锁**」当「锁已释放」→ 放行。未登记 / 已释放**不可区分且都取放行方向**；显式 `lock_held=True` 也被 `_collect_lock_facts(cp) or facts` 覆盖成放行 | `_collect_lock_facts` 改**三态返回**（`True` / `False` / `None`=没查到）；台账 `overview()` 增**全量锁键** `keys`（含已释放/已回收）作为"查到过"的判据；判定改为**显式事实优先**（`if "lock_held" in facts` 先判），缺席推断**永不覆盖**显式事实 | `TestLockFactsAreThreeState`（未登记 → `unknown`；曾登记已释放 → `proceed`；**两者可区分**；显式 `lock_held=True` + 台账查不到 → `wait`；手搓视图不给 `keys` → 保守 `unknown`）/ **反向**：整体未接台账仍 `unknown` |
| 🔴F | 判重台账落 `<tmp>/run-scope`、无留痕回写、CI 不落 verdict → 平台每次 push = **新容器** → 判重**永不触发**（实测 r1/r2/r3 全 `run`）；`record_repeat(...)` 不传 `verdict` → "修完同一文件集的新提交仍 `skip_duplicate`" | 与 🔴A **同一载体方案**：`RunScopeGuard.trail()/trails()` = `<!-- run-scope: {…} -->`，`adopt_trails()` 恢复（`max()` + 按 `session_id` 归属过滤）；门面 `run_scope_trail()` / `inherit_run_scope()`；CI 脚本接 `CNB_RUN_SCOPE_TRAIL`、跑完 `record_repeat(..., verdict=<rc 回填>)` 并打印写回留痕；**红后（同一受审内容）允许复跑**（`decide_repeat` 从台账取真实结论） | `TestRunScopeCarrierAcrossContainers`（跨容器第 2 次 → `skip_duplicate`；红后同一文件集 → 可复跑；绿仍跳过；归属过滤；只增不减；门面载体入口；CI 接线断言）/ **反向**：不带留痕每容器仍从 0 开始 |
| 🔴G | 门面 `create_checkpoint` / `register_platform_wait` 形参**没有**两侧事件名 → 落点四档恒 `platform_crontab`，"≤5min 挂 `cnb:await`"这条**首选落点在生产路径不可达**（照角色提示词用门面调用永远走不到） | 门面两处补 `waiter_event` / `resolver_event`（+ `stage`）并透传；未显式给出时按**当前事件**（`CNB_EVENT` / `CNB_PIPELINE_EVENT` / `CNB_TRIGGER_EVENT`）自动登记 | `TestFacadeCarriesEvents`（同事件 → 计划与登记两处都 `platform_await`；显式事件生效；环境事件回落生效）/ **反向**：两侧不同 / 事件不可判定 → 仍降级 `crontab` |
| 🔴H | `main.push` 新增的增量测试阶段 `image: node:20` 且**零安装步骤**（该镜像不含 python3）→ `python3 -m pytest` 在干净镜像里**必失败**（`set -euo pipefail`），且 `main` 此前无测试阶段 → 本 PR 新引入 | `.cnb.yml` 的 `main.push` 改 `image: python:3.11` + `pip install pytest`（+ 运行时自检）；删掉 `python3 --version || true`（`|| true` 把"没有运行时"静默吞掉） | `TestCiDeclaresRuntime`（镜像声明 Python 运行时；装 pytest；无 `|| true` 吞错；CI 脚本按真实 rc 失败） |

### 21.2 🟡 五项

| 项 | 缺陷 | 修法 | 回归 |
|---|---|---|---|
| 🟡1 | 多留痕 `max()` **未按归属过滤**：把 A 检查点的 `checks_used=6` / 别的会话的 `rounds.total=5` 合并进来 → **误判"预算用尽"/"已熔断"** | `loop_guard._merge_budget_dicts` / `_coerce_budget_payload` 按 `session_id` 过滤；`probe._coerce_checks_used` 按 `checkpoint_key` 过滤（`adopt_checks_budget` 传 `cp.key`）；留痕**不带该字段时不表态**（保留） | `TestTrailAttribution`（跨会话不吸收、跨检查点不吸收、本会话/本检查点照常合并、无归属字段保留）/ **反向**：A 用尽 6 次不再让 B 判 `resume_required` |
| 🟡2 | `_is_red` **子串**匹配把 `"577 passed, 0 failed"` 判红 → "允许重跑一次"的例外**恒真激活** | 匹配前先把 `0 failed` / `0 errors` 这类**零个失败**整段抹掉（`_ZERO_ISSUE_RE`） | `TestRedVerdictIgnoresZeroCounts`（6 组"零失败"→ 非红；7 组真失败 → 红；端到端：上轮 `"577 passed, 0 failed"` → 仍 `skip_duplicate`）/ **反向**：`2 failed` 仍允许重跑一次 |
| 🟡3 | `checkpoint_plan(checks=3)` 返回 4 行但 docstring 未说明"含末尾用尽行" | docstring 明写"返回行数 = 档位数 + 1，末尾恒有「预算用尽」行" | `TestCheckpointPlanDocstring`（docstring 含"预算用尽"/"档位数 + 1"；`checks=3` → `[1,2,3,7]` 末行 `handoff_resume`） |
| 🟡4 | `ReviewArbiter` **独立构造**默认硬编码 60s，与规则包 / 会话路径的 10s 不一致 | `retry_delay_sec` 改 `Optional[float]`：不传 → 取规则包 `locks.retry_delay_sec`（与会话同一取值路径）；规则包缺失才回落 `DEFAULT_RETRY_DELAY_SEC` | `TestReviewArbiterDefaultDelay`（独立构造 = 规则包值；会话与独立构造一致）/ **反向**：显式传入仍以调用方为准 |
| 🟡5 | 预算用尽分支**不落盘**：外部 `checks_used=6` 时磁盘 / `overview` 仍显示 0，而事件留痕显示 6 → **观测面与判定不一致** | 用尽分支补 `_save(cp)` + `history` 留痕（`action="budget_exhausted"`）+ 置 `resume_required` | `TestExhaustedBudgetPersists`（磁盘/总览显示 6、`resume_required=True`、留痕可见）/ **反向**：正常探测路径照常落盘 |

### 21.3 🟢 两项

| 项 | 修法 | 回归 |
|---|---|---|
| 留痕收集/归属过滤抽公共实现 | 新增 `secretary/trail.py`（`trail_re` / `collect_marked` / `attributed` / `MAX_TRAIL_DEPTH`）；`loop_guard.BUDGET_TRAIL_RE` 与 `probe.PROBE_BUDGET_TRAIL_RE` 均改为 `trail_re(...)`，两处不再各写一份同形正则 | `TestTrailAttribution::test_shared_trail_impl_is_single_source`（两处正则来自同一实现；`collect_marked` 收集**全部**留痕） |
| 锁采集三态返回 | 见 🔴E（`_collect_lock_facts` 从"缺席即推断"改为"缺席即 `unknown`"） | 见 🔴E 回归 |

### 21.4 自测

- `pytest -q`：**909 passed**（基线 `a7847f9` 853 + 本轮新增
  `test_collab_round5_fixes_issue14.py` **55** 项 + 上一轮改判的
  `test_full_gate_issue14.py` 净 +1），连跑 3 次稳定。
- `compileall src tests` OK；`pyflakes`（本轮改动文件）**0 告警**。
- 四个 YAML（`.cnb.yml` / `.cnb/settings.yml` / `shared/collab/*.yaml`）语法 ✅。
- **独立探针**（非复用作者测试）复现 → 修复后复测：
  🔴E 未登记 → `unknown`（旧 `proceed`）、已释放 → `proceed`、两者可区分；
  🔴F 跨容器序列 `run → skip_duplicate`（旧恒 `run`）、红后同内容可复跑；
  🔴G 门面同事件落点 = `platform_await`（旧恒 `platform_crontab`）；
  🔴H `.cnb.yml` 声明 `python:3.11` + 装 pytest；
  🟡1 跨检查点 `checks_used` 不再污染（`coerce(body, cp_key="cpB")=0`）；
  🟡2 `_is_red("577 passed, 0 failed")=False`；
  🟡5 用尽后磁盘 `checks_used=6`。
- CI 脚本**端到端模拟**（同一受审内容连跑 3 个"新容器"）：容器 1 `run` +
  留痕 → 容器 2/3 `skip_duplicate`（旧实现三个容器全 `run`）。

---

## 22. PR #15 复评第 4 轮定点修复（🔴1 / 🔴2 / 🔴5 · 基线 `a7847f9`）

> 触发：架构·评审 复评第 4 轮（对象 `a7847f9`）判 `fix_bug`——🔴3/🔴4 认可清零，
> **🔴1 / 🔴2 有可达残留**，并新增 **🔴5 性能回归**。本轮只动这三项 + 5 项 🟡 收口。
> 复评口径（评审指定）：`inherit_budget(trail+card)` → `6/True/False`、
> `_coerce_checks_used(trail2+载荷5)` → `5`、卡+卡两序同结论、`"{"×20000` 耗时。

### 22.1 🔴 三项

| 项 | 缺陷（评审实测） | 修法 | 判别性回归（+ 反向对照） |
|---|---|---|---|
| **🔴1a** 留痕 ↔ 载荷**不合并**：`_coerce_budget_payload` ① 读到 `rounds` 就 `return merged` → ②③④ 全跳过；「留痕段 + 不带内嵌留痕的卡」形态下**卡的轮次读不到**（实测 `trail(3)+card(6)` → `round_no=3`，**比"只给卡"的 6 还差**） | `loop_guard.py`：① 的 early return 删除，`merged` 只作**初始值**参与合并；④ 不再以"`got` 有没有 `rounds`"为条件，两通道一律 `_merge_budget_dicts([got, merged])`（`fused`/`budget`/`reason` 同） | `TestBudgetTrailMergesWithPayload`（`inherit_budget` → `6/True/False`；`from_handoff` 同结论；**两序一致**）/ **反向**：只给留痕 → 3、只给卡 → 6（加留痕不得更差） |
| **🔴1b** `_coerce_checks_used` dict 分支**首个键命中即 return** → `trail(2)+{"checks_used":5}` 读成 2；`{"checks_used":2,"budget_trail":trail(5)}` 读成 2（新增的 `budget_trail` 展开在真实路径上是**死代码**：`ProbeEvent.to_dict()` 必带 `checks_used`） | `probe.py`：三条键 + `rounds` + 内嵌 `budget_trail` **各自取值后统一 `max()`**；**正文串分支**同样合并（原先只扫留痕 → `trail(2)+{"checks_used":5}` 丢载荷），散落对象走 `trail.json_objects_in()`（与 `loop_guard` 同一份扫描） | `TestChecksUsedMergesAllChannels`（3 组 → 5；`ProbeEvent.to_dict()` 形态 → 4；`probe(external=trail2+载荷5)` → **6**）/ **反向**：只给留痕 → 2 |
| **🔴2** `pick_wake_object` 取**第一个**含 `wake` 的对象 → **卡+卡顺序决定结论**：`[True,False]` → 放行（fail-open）、`[False,True]` → 熔断；基线 `ed62088` 同结论 → docstring/文档宣称的"顺序无关"被证伪 | `loop_guard.py`：口径改**保守 + 确定**——**任一对象的 `wake` 非严格 `True` → 判静默**（与 `parse_wake` 的"判不出 → 保守静默"同向）；需要具体对象时取**追加顺序里最后一张卡**（正文是追加写的 → 后者更晚，即显式定义的"最新"判据） | `TestPickWakeObjectIsConservative`（卡+卡两序**同结论**＝熔断；坏值 `"false"` + `True` 两序也同结论；多静默卡取最后一张）/ **反向**：单张 `wake=True` 不熔断、多张 `True` 不熔断、不带 `wake` → `None` |
| **🔴5** `extract_json_objects` 在未闭合 `{` 上 **O(n²)**（`_balanced_object_end` 从每个 `{` 重扫文末）：`"{"×20000` → 15.8 s、`×40000` → 63.3 s、`"if (x) {"×3000` → 3.0 s；**基线 `ed62088` 同路径 3 ms**（本轮引入的回归，4 个数量级）；该函数在 `from_handoff` / `adopt_budget` / `_coerce_checks_used` **主路径**上，评论正文是不可信输入 | 扫描实现收敛到 `secretary/trail.py`（**单一实现**）：`scan_json_spans()` = **一次线性主扫描（栈配对 + 字符串状态）** + **有界修复**（对"字符串区间"重扫以补齐旧实现能捞到的起点；区间数与字符量各有上界，超界收摊）。未配平的起点**不产片段、也不重扫**（平方来源），但**不影响其后**能配平的起点 | `TestScanIsLinear`（`"{"×20000 / ×40000`、`"if (x) {"×3000` 均 **< 1 s**；×10 规模不平方）/ **反向**：未闭合前缀**不得**把其后的静默卡整段丢掉（fail-open 方向），正常正文（对象 + 留痕 + 括号字符串）逐字不变 |

### 22.2 🟡 五项收口

| 项 | 修法 | 回归 |
|---|---|---|
| 🟡1 `DEEP` 用例**无判别力**（非法 JSON → 走 `ValueError` 分支，基线同样"判不出"；合法 3000 层 JSON 两边都 `RecursionError`） | 用例换成**合法**深嵌套（内层 3000 层 dict，基线 `RecursionError` **逃逸到门面** → 本轮按判不出）+ **真正递归**形态（内嵌 `budget_trail` 链逐层解析） | `TestCoercionDepthBound::test_legal_deep_json_does_not_escape`（先断言 `json.loads` 真抛 `RecursionError`，再断言门面/`adopt_budget` 不抛）/ `test_nested_trail_chain_parses_layer_by_layer`（深度 ≤ 上界时逐层合并到 `round_no=9`）/ **反向**：浅载荷仍被读到 |
| 🟡2 `MAX_COERCE_DEPTH` **两处各写一份**（`loop_guard` / `probe`），`loop` 包导出了、`probe` 的没导出 | 上界**唯一定义处** = `secretary.trail.MAX_TRAIL_DEPTH`；两处只保留**指向它**的兼容别名（`MAX_COERCE_DEPTH = MAX_TRAIL_DEPTH`，不再 `as` 改名）；`collab` 包补齐导出 | `TestDepthBoundSingleSource`（`is` 同一对象；两个包都能取到同一个） |
| 🟡3 `_merge_budget_dicts` 的 `reason` / `budget` 是"**首个非空胜**" → 顺序相关；`budget` 取首个会把 `max_rounds` **盖小**（`[3,9]` → 3）而 `rounds` 已 `max()`（两字段互相矛盾） | `reason` 取**已熔断载荷里最长**的一条（并列取字典序最小 → 完全顺序无关）；`budget` **逐字段 `max()`**（新增 `_merge_budget_values`） | `TestReasonAndBudgetMergeAreOrderIndependent`（两序 `reason` 同、`budget.max_rounds` → 9、`rounds.total` 仍 `max`） |
| 🟡4 await 落点**只带 `key`、无 `env`**：`.cnb.yml` 的 await 片读 `$CHECKPOINT_AWAIT_KEY`，而落点产出的 key（`probe:<k>:r<n>`）**没人注入**那个变量 → "接线"停在事件名层面 | 落点 `trigger.env` 显式给出**键名映射**：`CHECKPOINT_AWAIT_KEY`（= 本次轮次 key，与 `.cnb.yml` **同名同源**）、`CHECKPOINT_KEY`、`PROBE_VERDICT`；另加 `trigger.key_env` 标注变量名 | `TestAwaitKeyIsWiredToPipeline`（`env.CHECKPOINT_AWAIT_KEY == trigger.key` 且以 `probe:c2:r` 开头；`.cnb.yml` 确实读该变量）/ **反向**：crontab 落点**不带** await 片的环境注入（落点互斥不混用） |
| 🟢 `extract_json_object` 无调用方（仅 re-export） | **保留并写明原因**：对外稳定 API（`secretary.loop.__all__` 导出），语义 = "取第一个顶层对象"，与 `extract_json_objects()[0]` **同源实现**（不另写一份扫描）；内部判定一律走"取全部 + `pick_wake_object`" | `TestExtractJsonObjectRetention`（与 `extract_json_objects()[0]` 同值；空/非串 → `None`） |

### 22.3 与旧实现的**差分对照**（防"修一个坏一个"）

- `scan_json_spans` vs `a7847f9` 的 `extract_json_objects`：**4518 组**用例
  （手写边界 + 4000 组随机花括号/引号/转义串 + 500 组"卡 + 留痕 + 未闭合 `{`"混合）
  逐组比对对象清单 → **差异 0**。
- `scan_json_spans` vs **基线 `ed62088`** 的 `extract_json_object`（按旧口径
  "从左到右反复取第一个"复现）：差异仅出现在**未配平起点自身**（旧实现从
  `start+1` 重扫，新实现不重扫）——正是平方复杂度来源；真实正文形态（对象 +
  留痕 + 差量片段）**逐字节一致**。

### 22.4 自测

- `pytest -q`：**944 passed**（基线 `a7847f9` 909 + 本轮新增
  `test_collab_round6_fixes_issue14.py` **33** 项 + 第 4 轮文件改判净 +2），连跑 3 次稳定。
- **判别力验证**（把本轮新测试放到**上一轮 `d22f6d7`** 上跑）：**19 failed / 12 passed**
  —— 19 项失败正是本轮要修的 🔴1a/🔴1b/🔴2/🔴5 + 🟡 各项（不是恒真断言）。
- `compileall src tests` OK；`pyflakes`（本轮改动文件）**0 告警**。
- 四个 YAML（`.cnb.yml` / `.cnb/settings.yml` / `shared/collab/*.yaml`）：
  PyYAML 语法 ✅ + 仓库兜底解析器（`_mini_yaml`）✅。
- **性能对照**（评审表格口径，同一容器）：

  | 输入 | `ed62088`（基线） | `a7847f9`（回归） | 本轮 |
  |---|---|---|---|
  | `"{" × 20000` | — | 15.8 s | **1.4 ms** |
  | `"{" × 40000` | 3 ms | 63.3 s | **2.6 ms** |
  | `"if (x) {" × 3000`（24 KB） | — | 3.0 s | **1.6 ms** |
  | `"{" × 200000` | — | — | **14.9 ms** |
  | 引号密集（`'"'×20000` / `'"{'×50000` / 2 MB 正文） | — | — | **29–81 ms** |

- 复评口径四项（评审指定，独立探针）：
  `inherit_budget(trail+card)` → `6 / True / False`；
  `_coerce_checks_used(trail2+载荷5)` → `5`；
  卡+卡两序 → `fused=True / should_wake_next=False`（两序同结论）；
  `"{"×20000` → 1.4 ms（基线 3 ms 量级）。

### 22.5 台账更正（按实测）

- §20 的 🔴1 行：**"已清零"不成立**——本轮实测其 ① early return 仍在，
  「留痕 + 不带内嵌留痕的卡」形态下轮次**回退**（`trail(3)+card(6)` → 3）。
  已在 §22.1 🔴1a 记录并修复；🔴1 的"trail↔trail 通道"部分（第 3 轮）确实已修好。
- §20 的 🔴2 行：**"已清零"不成立**——`pick_wake_object` 当时是"首个含 `wake` 者胜"，
  卡+卡顺序决定结论（且一个方向 fail-open）。已在 §22.1 🔴2 记录并改为保守口径。
- §21（第 5 轮）未涉及本三项；本轮为**第 3 次打回后的定点修**（§19 `2be6a3d`、
  §20 `ed62088`、§21 `a7847f9`、本轮）——按协作纪律，若下轮仍有 🔴 残留，
  由评审按 `blocked` 上报总师裁定。
