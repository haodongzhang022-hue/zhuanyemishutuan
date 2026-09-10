# 交接卡引用可见性 · 契约边界

> module: zhuanyemishutuan · docs/design
> 关联：Issue #4（P2-① 共享 `ho-*` 卡跨会话误删）· Issue #8（P2-④ 可见性契约边界）
> 载体：`state-secretary/src/secretary/session/session.py` → `_build_ho_reference_index`
> 状态：**已更新（🔴 P0-1 / P0-2 / P1-1 / P1-3 修复后）**。核心契约已冻结（见 §2）。
> §3 的 🟡A（剪枝歧义名）/ 🟡D+🟡E（登记静默失败，**双通道登记**闭环）**已修复**；
> 仅剩 🟡B（形状校验必含 `id`）为已知边界，**不再依赖总师裁定即可放行**。
> **迁移前提**见 §5：含 🔴 P1-3 单元名白名单**新增**后的存量非 ASCII 单元名卡
> （须重建/改名，§5 第 4 条）。

## 1. 背景

`SecretarySession.delete_session()` 回收共享 `ho-*` 状态卡前，须先判定「是否仍有
存活引用者」。判定由 `_build_ho_reference_index()` 一次扫描建立
`state_card_id → 引用者标签` 索引完成。索引的三个来源：

1. **归属台账**（数据根下 `.session-<id>.owned`，且该会话状态卡仍存在）；
2. **未删除交接卡**（数据根下任意子目录，经剪枝 + 形状校验的 `*.json`）；
3. **state 根级引用登记**（`<state_root>/handoff-refs/<card_id>.ref`）。

不变量（🔴#1–#4 修复链路的核心）：**存活交接卡 → 其 `state_card_id` 状态卡必须存在**。
宁可多保留（不误删），绝不误删。

## 2. 已冻结的核心契约（本 PR 内，不随本草案变更）

| 契约 | 内容 |
|---|---|
| 扫描界 | 会话数据根 `self._session_root`（递归），非仅 `handoff_bus.data_dir` |
| 引用源 | 并集 = 台账 ∪ 未删除交接卡 ∪ state 根级登记 |
| 登记生命周期 | `auto_state_card=True` 建卡登记、`delete()` 注销；`card_path` 消失的 stale 登记视为失效、不阻塞回收 |
| 自身残卡 | **不排除**自身认领卡：留盘残卡即真实存活引用（不误删） |
| 回收语义 | 无「延迟/自动回收」路径——残卡留盘期间共享卡**永久留盘**，须**显式清理残卡**后由后续 `delete_session` 再判定方可回收（🟡C 措辞更正） |
| 删除顺序 | 先撤销自身引用（交接卡 → 自身状态卡 → 台账），再建索引回收（🔴#1，防 TOCTOU） |
| 异常守卫 | 扫描异常（含 `isdir→listdir` TOCTOU）**不得逃逸**：登记目录消失视为无登记；扫描失败保守跳过回收、且不中断归档/台账清理（🔴 P0-2） |
| 标识符校验 | 参与路径拼接的标识（`card_id` **及** `source_unit`/`target_unit`）须过 `[A-Za-z0-9_-]+` 白名单，拒绝路径穿越（🔴 P1-3） |

## 3. 可见性保证范围（🔴 P0-1 / P0-2 / P1-1 / P1-3 修复后更新）

| 项 | 原状 | 现状 |
|---|---|---|
| 🟡A 剪枝黑名单含歧义目录名 | 名单名（`build`/`dist`/`env`/`venv`/`.venv` 等）目录内交接卡整片不可见 → 悬空 11/12（评审/测试对全量 11 名逐一复现，11/11 误删） | ✅ **已修复**：剪枝**不再按裸目录名判定**——保护子树（数据根 / `state` / `handoffs` / `content` / `handoff-refs`）永不剪枝；名单目录须先过**形状判定**（`_dir_holds_handoff_data`，有界浅扫）才剪枝 |
| 🟡B 形状校验收紧必含 `id` | 外部/遗留交接卡缺 `id` → 不可见 | ⚠️ **维持**（已知边界）：改走 `HandoffBus` API 即覆盖；如需覆盖手写卡请报 Issue #8 |
| 🟡D 旧版根外卡无 `.ref` 登记 | 登记静默失败（`except OSError: pass`）→ 会话侧误判"无引用" | ✅ **已修复**：登记失败不再静默——登记为该卡**唯一**可见性路径（`data_dir` 在 `state_root` 子树外）时 **fail-fast** 回滚建卡；子树内则由目录扫描兜底（见下 🟡E） |
| 🟡E 登记写入失败 + 根外卡目录 → 无兜底 | 登记静默吞 + 卡目录在根外 → 引用双路径皆不可见 → 误删（纯 API 路径反例） | ✅ **已修复**（与 🟡D 同族）：**双通道登记**（主 `handoff-refs/` + 备 `.handoff-refs/`，不同故障域）任一生效即引用可见；仅**双通道全失败**且登记为唯一可见性路径时 `_registry_required()` 触发 fail-fast 回滚 |
| 🔴 P1-3 `card_id` 路径穿越 | 未校验 `card_id` → `.ref` / `.json` 路径穿越删除面，可与 P0-1 连锁 | ✅ **已修复**：`card_id` 须匹配 `[A-Za-z0-9_-]+`（`_is_valid_card_id`），`get`/`delete`/`_register_ref`/`_unregister_ref` 全部前置校验；台账 `handoff_ids` 经 `HandoffBus.delete` 亦被拦截 |
| 🔴 P1-3 补充 单元名路径穿越 | `card_id` 由 `source_unit`/`target_unit` 拼接，但 `create_handoff` 未校验单元名 → `create_handoff("../state/X",…)` 把卡写到 `data_dir` **之外**（路径穿越**写入**面，门面同样可达） | ✅ **已修复**：`create_handoff` 对 `source_unit`/`target_unit` 做同一白名单校验（非法抛 `ValueError`）；`_save` 纵深防御（非法 `card.id` 拒绝落盘）。**迁移前提**：白名单为**新增**约束，存量非 ASCII 单元名卡须重建/改名（§5 第 4 条） |

### 剪枝规则（P0-1 修复后）

```
进入子目录判定（_keep_scan_dir）：
  1) 保护子树（数据根 / state / handoffs / content / handoff-refs）及其祖先 → 保留（永不剪枝）
  2) 名字 ∈ _SKIP_SCAN_DIRS（build / node_modules / .venv 等）
     **或为隐藏目录**（.staging / .cache / .git 等）→ 形状判定：
       目录子树内有交接卡形状 JSON → 保留（kept_skip_dirs，引用按"指向本根真实状态卡"过滤）
       否则 → 剪枝（保留原性能收益：真重型目录 node_modules、真无关目录 .git 仍被跳过）
  3) 常规数据目录 → 保留
```

> **🔴B 复评修复（`7300b22` 打回）**：隐藏目录原为**裸名直接剪枝**
> （`if name.startswith(".")`），与名单目录"按裸名判定"同根因未清——根内隐藏
> `data_dir`（`.staging`/`.cache`/`.data`/`.build`）内真实卡不可见 → 共享卡被误删
> （实测 4/4 破坏）。现与名单目录同一口径（形状判定后再剪）。

- **有界**：形状判定与保留子树遍历深度 ≤ `_SKIP_DIR_PROBE_DEPTH`（= 3，**含文件
  层**）、访问条目 ≤ 2000，避免对 `node_modules` 深层树全树遍历（实测 2000 目录
  扫描 **1.2ms**）。
- **深度上界单一语义源（🔴 M-2 更正）**：`_KEPT_SKIP_MAX_CARD_DIR_DEPTH =
  _SKIP_DIR_PROBE_DEPTH - 1` —— 形状判定（递归，含文件层）与 `os.walk` 剪枝
  （相对保留根层级）**共用同一语义源**，不再各写一套（原两处常量语义不同 →
  「形状判定保留、遍历却剪掉」不自洽）。
- **🔴A 复评修复（`7300b22` 打回）——保留根多重重叠时取最小深度**：`kept_skip_dirs`
  为 `set`，当 `dirpath` 同时落在**多个**保留根之下（嵌套保留目录各自入列，如
  `<root>/build` 与 `<root>/build/dist`）时，原实现取「首个前缀命中即 return」→
  取的是**集合迭代顺序**的第一个前缀，而非最近根 → 深度上界判定随 `PYTHONHASHSEED`
  变化 → 同一布局时而截断、时而放行（实测 10 seed 中 6 破坏不变量）。现改为取
  **所有匹配根的最小深度**，判定与迭代顺序无关。
- **残留边界（有界浅扫的代价，已知）**：
  1. **深度**：**卡目录**相对保留根（名单名 / 隐藏名）的层级 >
     `_KEPT_SKIP_MAX_CARD_DIR_DEPTH`（= 2）时不可见。**实测口径（M-2 更正）**：
     `<root>/node_modules/a/b/handoffs`（卡目录相对层级 **2**）**可见**；
     `<root>/node_modules/a/b/c/handoffs`（相对层级 **3**）**不可见**。
     （原文档写「嵌套 ≥ 3 层不可见」，把形状判定的**含文件层**深度直接当作卡目录
     层级 → 口径错误；现按实测更正为「卡目录相对层级 > 2 层」。）
  2. **条目预算**：形状判定为**有界**浅扫，`max_entries = 2000` 耗尽即判定 False
     → 该子树内真实卡不可见（🟡E 文档化）。实测 `node_modules/` 下 2500 个兄弟
     目录时其内真实卡不可见；且 `os.scandir` 顺序参与预算消耗，**天然不确定**。
     规避方式：改走 `HandoffBus` API（登记通道）或调整 `data_dir` 布局（把卡目录
     置于数据根下非保留目录、或减少嵌套 / 减少兄弟条目）。

### API 路径保证（P1-1 + 双通道登记后成立）

经 `HandoffBus` API（`create_handoff(auto_state_card=True)` / `delete`）写出的卡：
- 恒带 `id` → 形状校验通过；
- 登记**双通道**（主 `handoff-refs/` + 备 `.handoff-refs/`）任一成功 → 来源 3 可见；
- 双通道全失败且为**唯一**可见性路径 → **建卡即失败**（不产生悬空卡）；
- 双通道全失败但目录扫描可达（卡目录在 `state_root` 子树内）→ 来源 1/2 可见。

即：**「API 写入路径的引用恒可见」在下列边界内成立**（🟡D 口径更正：原「无条件
成立」表述属**过度声明**，已撤回——🔴A/🔴B 修复后仍存在超深、条目预算耗尽两类
已知边界，见上「残留边界」）：

| 条件 | 引用是否可见 |
|---|---|
| 登记双通道任一生效（含卡目录在 `state_root` 子树**外**） | ✅ 可见（来源 3） |
| 登记双通道全失败 + 唯一可见性路径 | ✅ 建卡即失败（fail-fast，**不产生**悬空卡） |
| 登记双通道全失败 + 卡目录在 `state_root` 子树**内** | ✅ 目录扫描兜底（来源 1/2） |
| 卡目录在**保留目录**（名单名 / 隐藏名）内，卡目录相对层级 > 2 或条目预算耗尽 | ⚠️ **不可见**（已知边界，见上） |

**结论**：不存在"写入成功但引用静默悬空"（fail-fast 兜住）；但**不声称**"任何
布局下引用恒可见"——超深 / 预算耗尽两类边界下，若同时**双通道登记被外部删除**，
则引用不可见 → 共享卡可能被误删。此类须显式声明（见 §5）。

## 4. 风险与影响

- **手写卡落在保留目录（名单名 / 隐藏名）**：P0-1 + 🔴B 修复后可见（形状判定保留
  子树）；仅当**卡目录相对层级 > 2** 或**条目预算耗尽**时仍不可见（有界浅扫的代价，
  避免 `node_modules` 全树遍历）。边界实测见 §3（M-2 口径更正）。
- **保留根多重重叠（🔴A）**：嵌套保留目录下深度截断**取最小深度**（与迭代顺序无关），
  不再随 `PYTHONHASHSEED` 漂移。
- **fail-fast 回滚面（🟡1，复评第 5 轮已修）**：双通道登记全失败 + 根外布局时，
  回滚**本次调用产生的全部落盘物**——交接卡本体 **+ 本次新建的 `ho-*` 卡**
  （`delete_card` 幂等清 `.json`/`.lock`/`.owner.json`）。**先已存在的共享 `ho-*` 卡
  不动**（hash 复用场景，误删会破坏他处引用）——回滚前以「该卡是否本次新建」判定。
  回归：`TestFailFastRollbackBoundary`（3 项，含反向对照）。
- **根外卡 + 登记不可用**：`data_dir` 在 `state_root` 子树**之外**且双通道登记均
  不可用（外部删 `.ref` / 磁盘异常）时，引用不可见 → 共享卡可能被误删。此为
  **baseline 既有面**（`6393886` main 同场景即破坏，非本 PR 引入），属 🟡D 同族，
  见 §5 迁移前提；双通道**写入失败**路径已由 fail-fast 兜住（不产生悬空卡）。
- **无 `id` 的手写卡**：仍不可见（🟡B）。改走 `HandoffBus` API 即覆盖。
- **登记失败**：双通道（主 + 备）任一生效即引用可见；双通道全失败时，子树外布局
  fail-fast（不产生悬空卡）、子树内布局由目录扫描兜底。
- **扫描异常（P0-2 / 🟡2）**：扫描抛**任意异常**（不止 `OSError`，复评第 5 轮
  收口为全异常族）时保守**跳过回收**（不误删），且不中断归档/台账清理；
  `registry listdir` 竞态（TOCTOU）已加守卫。
- **保守跳过后的可回收性（🟡3，已知边界）**：扫描不可用时本次认领的 `ho-*` 卡
  留盘且**不再由任何新实例自动回收**（`_owned_state_card_ids` 为实例内存态、归属
  台账已随本会话撤销）→ 须**显式清理**或**本实例重试**再判定方可回收。
  口径 = 「不误删；须显式清理」，**不存在**"新实例顺带自动回收"路径。
- **历史卡 id 字符集（🟡4，迁移面）**：`card_id`/`source_unit`/`target_unit`
  白名单 `[A-Za-z0-9_-]+` 为**本 PR 新增**约束。旧版遗留卡 id 含 `.` 或非 ASCII
  （如 `unit.a-unit.b-*`、`架构·开发-架构·评审-*`）→ `get`→`None`、`delete`→`False`
  → 卡不可读不可删、其引用永久阻塞共享卡回收。**迁移前提见 §5 第 4 条**。
- **路径穿越（P1-3）**：参与路径拼接的标识（`card_id` **及** `source_unit`/
  `target_unit`）均已加白名单校验——`HandoffBus.delete` / `_unregister_ref` /
  台账 `handoff_ids` 不再构成 `data_dir` 之外的**删除**面；`create_handoff` /
  `_save` 亦不再构成**写入**面。

## 5. 迁移前提（🟡 P1-2 文档化）

旧版（≤ `6a2e713`）的**根外**交接卡（`data_dir` 在 `state_root` 子树之外）无
`handoff-refs/*.ref` 登记 → 在扫描界内不可达、无登记可见 → 引用仍可能悬空。

**升级/迁移前提（运维须知）**：

1. **根内卡**：`data_dir` 在 `state_root` 子树内 → P0-1 + 🔴B 修复后目录扫描可达，
   **无需迁移**——含**名单目录名**（`build`/`dist`/`env`…）与**隐藏目录名**
   （`.staging`/`.cache`/`.data`/`.build`…，🔴B 修复后同口径）。
   **但须满足**：卡目录相对保留目录的嵌套深度 < 3，且其所在保留子树未被条目预算
   耗尽（超深 / 预算耗尽时须补偿登记或调整布局，见 §3「残留边界」）；
2. **根外卡**：`data_dir` 在 `state_root` 子树之外 → 须**补偿登记**方可被会话侧
   发现。补偿方式（二选一）：
   - 由该卡的持有者改用 `HandoffBus(data_dir=…, state_root=…)` 重新创建/重写该卡
     （`create_handoff` 会自动登记）；或
   - 手工在 `<state_root>/handoff-refs/<card_id>.ref` 写入
     `{"card_id":…, "state_card_id":…, "card_path":<绝对路径>}`。
3. **不变量**：迁移期间若共享 `ho-*` 卡被误删，其根外引用会悬空——建议在迁移
   完成前**冻结**跨根交接卡的删除操作（或先执行补偿登记再删除）。
4. **存量非 ASCII / 含点单元名卡**（🔴 P1-3 单元名白名单为**新增前提**）：
   `source_unit`/`target_unit`/`card_id` 白名单 `[A-Za-z0-9_-]+` 为**本 PR 新增**
   约束。旧版（≤ `6a2e713`）产出的**存量卡**其 `card_id` 含**非 ASCII**
   （`架构·开发-架构·评审-*`）**或含 `.`**（`unit.a-unit.b-*`）→ 白名单后
   `get`→`None`、`delete`→`False`，**无法经 API 清理**，须**重建或改名**：
   - 重命名 `card_id`（去非 ASCII）并同步改写卡内 `id`；或
   - 改走 `HandoffBus` API 以合规单元名重建该卡（旧卡手工删文件回收）。
   **盘点要求（最低要求）**：升级前须盘点 `handoffs/*.json` 的**文件名**（= `card_id`），
   凡不符白名单者先行清理/改名/补偿登记；**未完成此盘点不得启用共享卡回收**
   （否则该卡引用永久阻塞共享卡回收，形成"永久孤儿"）。
   **不变量仍成立**：形状扫描仍计入其引用、共享 `ho-*` 卡保留（不误删）；该面属
   **迁移面**（存量数据），非本 PR 新增缺陷。

**判定口径**：`HandoffBus._registry_required()` 即"该卡是否属根外、登记是否为
唯一可见性路径"的实现判据。

## 6. 留痕

- 登记：Issue #8（本草案关联；含 🟡E 第三条 gap —— **已闭环**）
- 代码口径标注：`session.py::_build_ho_reference_index` docstring「可见性保证范围」
- 🔴 M-1/M-2 + 🟡1–🟡5（复评第 5 轮）：`_keep_scan_dir` 隐藏名取消裸名剪枝；
  深度上界常量单一语义源（`_KEPT_SKIP_MAX_CARD_DIR_DEPTH = _SKIP_DIR_PROBE_DEPTH - 1`）
  + 本文档深度口径按实测更正；fail-fast 回滚补本次新建 `ho-*` 卡；扫描异常族收口。
  详见 §8 / `issues/TEST-REGRESSION.md` §九。
- 台账：`issues/TEST-REGRESSION.md` §三 P2-④ + §二 修复 #J + §九 M-1/M-2/🟡1–🟡5
- 独立探针（架构·测试）：`state-secretary/tests/test_ho_ref_invariant_probes.py`

## 7. 总师裁定 + 🔴1 根因收口（Issue #4 复评第 4 轮 · 2026-09-10）

### 7.1 收口路径裁定

- **裁定：本 PR 内修复收口**。不开新 Issue、不接受「API 路径 + 单线程」口径
  收窄以规避 🔴。依据：两条 🔴 均**由本 PR 引入**（`11dfdd9` 新增登记机制），
  合并即向 `main` 引入并发缺陷；打回计数超限是**流程信号**，不构成放行理由。
- **熔断约束（本轮为最终收口轮）**：范围**冻结**为 🔴1/🔴2 定点修复 + 判别性
  回归；**禁止**借修复引入新机制/重构。若再现第 5 次打回 → 改判 `blocked` +
  另开 Issue 排期，PR 回退到 `11dfdd9` 之前的「扫描兜底」口径。

### 7.2 🔴1 根因判定（与开发方 `a852574` 方案的差异）

| | 开发方 `cc7f4fd`（双通道 + fail-fast） | 总师收口（模块级互斥锁） |
|---|---|---|
| 静默悬空（不变量破坏） | 0（fail-fast 回滚兜住） | 0 |
| **登记静默丢失** | **0–2 / 1500**（根内布局） | **0 / 1500** |
| **误抛 `OSError`（假失败）** | **0–2 / 1500**（根外布局） | **0 / 1500** |

**根因未消除**：`_register_ref`（`makedirs`/`mkstemp`）与 `_unregister_ref`
（空目录 `rmdir`）仍**无互斥**——`rmdir` 与 `mkstemp` 交叠 → `mkstemp` 抛
`FileNotFoundError` → 双通道同时失败。开发方方案把该竞态的**后果**从「静默
悬空」转为「fail-fast 误抛 `OSError` + 根内布局登记丢失」，**根因（临界区
无互斥）仍在**，表现为：
- 根外布局：并发下 `create_handoff` **误抛 `OSError`**（假失败，非真故障）；
- 根内布局：登记静默丢失（目录扫描兜底掩盖，不破坏不变量但属冗余路径失效）。

**收口**：新增模块级 `_REF_REGISTRY_LOCK`（RLock），使 `_register_ref` 的
「建目录 + 原子写」与 `_unregister_ref` 的「删 `.ref` + 空目录 `rmdir`」**同
临界区互斥**（跨 `HandoffBus` 实例共享同一 `state_root` 时亦互斥）。
保留开发方的双通道 + fail-fast（对「单通道占位/权限」类故障仍有效），二者互补。

### 7.3 验证（总师独立复跑）

- 登记静默丢失：**0/2400**（修复前 22–33/2400）。
- 严格并发矩阵（根外 + 根内布局，6 线程 × 250 轮 × 3 组）：误抛 **0**、
  静默悬空 **0**、登记丢失 **0**。
- 全量 `pytest`：**202 passed**（+3 判别性用例，修复前基线确定性失败）。
- `compileall` OK；`pyflakes` 仅 1 处既有未用变量（`test_secretary_session.py:1035`，
  `main` 已存在，非本轮）。

## 8. 复评第 5 轮收口：🔴A / 🔴B 定点修复（Issue #4 · head `7300b22` 打回）

> 评审（架构·评审）在 `7300b22` 增量复评中判 **不通过**：核心不变量在**根内布局**
> 下仍可被破坏，且结果随 `PYTHONHASHSEED` 变化。本角色按评审给出的**点定修法**
> 修复（遵守总师 §7.1 范围冻结：不重构、不引新机制）。

### 8.1 🔴A — 保留根多重重叠 → 深度截断顺序相关（非确定性）

- **根因**：`_exceeds_kept_skip_depth` 遍历 `kept_skip_dirs`（`set`）时「首个前缀
  命中即 `return`」——取的是**集合迭代顺序**的首个前缀而非最近根。当 `dirpath`
  同时落在多个保留根之下（`<root>/build` 与 `<root>/build/dist` 双双因形状判定
  入列），深度上界判定随路径哈希变化。
- **复现**（固定路径，仅变 seed）：`<root>/build/decoy.json` 使 `build`/`build/dist`
  双双入列；受害者卡在 `<root>/build/dist/x/y` 且双通道 `.ref` 删除 →
  **10 seed 中 6 破坏**（`card_alive=True, ho_alive=False`）。
- **修法（点定）**：取**所有匹配根的最小深度**，去除顺序依赖；`abspath == r`
  仍立即返回 `False`（根自身不截断）。
- **验证**：同布局 10 seed → **0 破坏**；`_OrderedRoots` 替身两顺序断言一致。

### 8.2 🔴B — 隐藏目录按裸名剪枝（同根因未清）

- **根因**：`_keep_scan_dir` 中名单目录已改为"形状判定后再剪"，但隐藏目录走
  `if name.startswith("."): return False`——**纯裸名剪枝**。根内隐藏 `data_dir`
  （`.staging`/`.cache`/`.data`/`.build`）+ 双通道登记不可用 → 其内真实卡不可见
  → 共享卡被误删（实测 4/4 破坏）。
- **修法**：隐藏目录并入名单分支，同一 `_dir_holds_handoff_data` 有界判定后再剪。
- **验证**：4 隐藏名 × 3 触发 = **12/12 保持不变量**（修复前 12/12 破坏）；
  `queue` 对照 ✅；`.git`/`node_modules`/`.venv` 剪枝收益**不退化**（伪引用 0、
  2000 目录索引 1.2ms）。
- **如实归因**：该行为面在基线 `60b625d` 同样破坏（**非本 PR 引入**）；但本 PR
  新增的 `if name.startswith(".")` 行**延续**了该判定，且文档曾宣称「剪枝不再按
  裸目录名判定」「API 路径的引用恒可见无条件成立」——与实测不符，故一并更正口径。

### 8.3 🟡C / 🟡D / 🟡E 口径与前提同步更正

| 项 | 处置 |
|---|---|
| 🟡C → 🟡1 fail-fast 回滚面 | **复评第 5 轮已修**（原"范围冻结不改行为"已解除）：回滚**本次新建**的 `ho-*` 卡（`delete_card` 清 `.json`/`.lock`/`.owner.json`），先已存在的共享卡不动（以"是否本次新建"判定）。`TestFailFastRollbackBoundary`（3 项，含反向对照） |
| 🟡D 口径过度声明 | **撤回**「API 写入路径引用恒可见**无条件成立**」→ 改为**带边界表述**（见 §3 表）；§5「根内卡无需迁移」补前置（卡目录相对层级 ≤ 2 且预算未耗尽，M-2 口径） |
| 🟡E 条目预算未文档化 | §3/§4 补「条目预算 `max_entries=2000` 耗尽 → 不可见」边界，并点明 `os.scandir` 顺序参与预算消耗（**天然不确定**）；`TestScanBudgetBoundary` 锁定预算语义 |

### 8.4 判别性回归（🟡F 四类缺口补齐）

新增 5 类 / **24 项**（`test_secretary_session.py`），**在修复前 head 上 11 项 FAIL**
（有真实判别力，非恒真）：

| 类 | 覆盖 | 修复前 |
|---|---|---|
| `TestKeptSkipDepthOrderIndependence`（5） | ① 嵌套保留根顺序（含 `_OrderedRoots` 两顺序 + 端到端 + **多 seed 子进程**） | 3 FAIL |
| `TestHiddenDirVisibility`（14） | ② 隐藏名（非 `.venv`）data_dir × 4 名 × 3 触发 + 对照 + `.git` 剪枝收益 | 8 FAIL |
| `TestScanBudgetBoundary`（3） | ③ 条目预算耗尽 / 充足 / 大规模有界 | — |
| `TestFailFastRollbackBoundary`（3） | ④ fail-fast 回滚（本次新建 `ho-*` 一并回收 + 反向对照） | 🟡1 修复前 2 FAIL |
| `TestYellow2NonOserrorScanEscape`（3） | ⑤ 非 `OSError` 扫描异常不逃逸 | 🟡2 修复前 3 FAIL |
| `TestYellow3ConservativeSkipSemantics`（1） | ⑥ 保守跳过无"新实例自动回收"路径 | — |
| `TestYellow4LegacyCardIdCharset`（2） | ⑦ 历史卡 id 字符集迁移面固化 | — |
| `TestM2DepthBoundConsistency`（7） | ⑧ 深度上界单一语义源 + 实测 2/3 层边界 | M-2 修复前 6 FAIL |
| `TestHiddenDirVisibility`（+15） | ⑨ **评审 M-1 验收口径**：点前缀名 × 3 触发 0 破坏 | M-1 修复前 15 FAIL |

### 8.5 自测（本角色）

- 全量 `pytest -q`：**295 passed**（门面 **148**，独立探针 65）。
- `compileall src` OK；`pyflakes`（改动文件）**0 告警**。
- 独立探针（本角色自写，不复用作者/测试用例）：10-seed 矩阵 **0/10 破坏**、
  隐藏名 4 名 × 3 触发 **0/12 破坏**、评审 M-1 口径 5 名 × 3 触发 **0/15 破坏**、
  剪枝收益不退化（2000 目录 `.git`/`node_modules` 索引 ~1.2–1.7ms）。

## 9. 复评第 5 轮补收口：M-2 深度口径 + 🟡1–🟡5（2026-09-10）

> 在 §8（🔴A/🔴B）之上，按评审 M-1/M-2 验收口径与「🟡1–🟡5 请一并登记」要求补收口。

| 项 | 处置 | 回归 |
|---|---|---|
| 🔴 M-1（=🔴B）验收口径 | 点前缀名（`.data`/`.cache`/`.secretary`/`.state`/`.queue`）× 3 触发（in-root）**0/15 破坏**；基线 `8790e5f` 独立探针实测 **15/15 破坏** | `TestHiddenDirVisibility::test_reviewer_acceptance_hidden_names`（15） |
| 🔴 M-2 深度口径 | 文档 §3「嵌套 ≥ 3 层」→ 实测「**卡目录**相对层级 > 2 层」；新增 `_KEPT_SKIP_MAX_CARD_DIR_DEPTH = _SKIP_DIR_PROBE_DEPTH - 1`（单一语义源） | `TestM2DepthBoundConsistency`（7，修复前 6 FAIL） |
| 🟡1 fail-fast 回滚 | 回滚本次新建 `ho-*` 卡（先已存在共享卡不动） | `TestFailFastRollbackBoundary`（3） |
| 🟡2 异常族收口 | `delete_session` 扫描守卫由 `except OSError` → `except Exception` | `TestYellow2NonOserrorScanEscape`（3） |
| 🟡3 保守跳过语义 | 固化「不误删；无新实例自动回收路径」 | `TestYellow3ConservativeSkipSemantics`（1） |
| 🟡4 id 字符集迁移 | 契约 §5 第 4 条补「盘点 `handoffs/*.json` 文件名」最低要求 | `TestYellow4LegacyCardIdCharset`（2） |
| 🟡5 回执符号准确性 | 复核 `_ALWAYS_SKIP_SCAN_DIR_NAMES`/`_AMBIGUOUS_SCAN_DIR_NAMES`/`_contains_data_root` 及 4 个测试类名在全历史命中数为 **0**（旧回执误记）；本轮回执按实际符号 | — |

## 9. 🟡 收口轮（Issue #4 复评第 5 轮 · 架构·评审 `proceed` 后 · 非门禁）

> 触发：架构·评审增量复评 `60e32ec` 处置 **`proceed`**（🔴 清零），点名确认
> 🟡-2 台账口径 / 🟡-5 契约文档补注，并询问 🟡-1 是否一并收口。本轮**不含 🔴 修复**。

| 项 | 处置 |
|---|---|
| 🟡-1 `list_pending` 逐文件读未守 | ✅ 单条读异常（`exists→open` 窗口 / 半写 / 坏 JSON）**跳过**不逃逸（`except (OSError, ValueError)`），与目录级守卫同纪律 |
| 🟡-2 台账基线并存 | ✅ 已由上游 `e3195ea` 收口为**唯一口径 + 历史行**；本轮复核一致（无残留） |
| 🟡-3 fail-fast 回滚残留 `ho-*` + `.lock` | ⏸ **本轮不改行为**：上游 §8.3 已在**总师范围冻结**下将其定为「已知边界 + `TestFailFastRollbackBoundary` 锁定」。**修法已就绪**（见下），待总师解冻/授权后落地 |
| 🟡-4 标识符尾锚 `$` | ✅ 收紧为 `\Z`（同族 6 处 pattern 一并收紧） |
| 🟡-5 单元名白名单为新增前提 | ✅ §5 迁移前提补注第 4 条 |

**🟡-3 就绪修法（未落地，等授权）**：`create_handoff` 在 `get_or_create` **之前**
记录 `ho_card_preexisted = sm.get_card(state_card_id) is not None`；fail-fast 回滚
时仅当 `not ho_card_preexisted` 才 `delete_card`（连带 `.lock` / `.owner.json`）。
如此**预存在的共享 `ho-*` 卡绝不误删**（回应 §8.3 的顾虑），仅回收本次调用新建者。
落地时须同步改写 `TestFailFastRollbackBoundary::test_fail_fast_leaves_ho_card_known_boundary`
（其当前断言"残骸留盘"将转为"残骸已回收"）。

