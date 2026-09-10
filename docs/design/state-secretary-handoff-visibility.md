# 交接卡引用可见性 · 契约边界

> module: zhuanyemishutuan · docs/design
> 关联：Issue #4（P2-① 共享 `ho-*` 卡跨会话误删）· Issue #8（P2-④ 可见性契约边界）
> 载体：`state-secretary/src/secretary/session/session.py` → `_build_ho_reference_index`
> 状态：**已更新（🔴 P0-1 / P0-2 / P1-1 / P1-3 修复后）**。核心契约已冻结（见 §2）。
> §3 的 🟡A（剪枝歧义名）/ 🟡D+🟡E（登记静默失败，**双通道登记**闭环）**已修复**；
> 仅剩 🟡B（形状校验必含 `id`）为已知边界，**不再依赖总师裁定即可放行**。

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
| 🔴 P1-3 补充 单元名路径穿越 | `card_id` 由 `source_unit`/`target_unit` 拼接，但 `create_handoff` 未校验单元名 → `create_handoff("../state/X",…)` 把卡写到 `data_dir` **之外**（路径穿越**写入**面，门面同样可达） | ✅ **已修复**：`create_handoff` 对 `source_unit`/`target_unit` 做同一白名单校验（非法抛 `ValueError`）；`_save` 纵深防御（非法 `card.id` 拒绝落盘） |

### 剪枝规则（P0-1 修复后）

```
进入子目录判定（_keep_scan_dir）：
  1) 保护子树（数据根 / state / handoffs / content / handoff-refs）及其祖先 → 保留（永不剪枝）
  2) 名字 ∈ _SKIP_SCAN_DIRS（含 .venv）→ 形状判定：
       目录子树内有交接卡形状 JSON → 保留（kept_skip_dirs，引用按"指向本根真实状态卡"过滤）
       否则 → 剪枝（保留原性能收益：真重型目录如 node_modules 仍被跳过）
  3) 其他隐藏目录（.git 等）→ 剪枝
  4) 常规数据目录 → 保留
```

- **有界**：形状判定与保留子树遍历深度 ≤ 3、访问条目 ≤ 2000，避免对 `node_modules`
  深层树全树遍历（实测 2000 目录扫描 0.002s）。
- **残留边界**：名单目录内**再嵌套 ≥ 3 层**的交接卡仍不可见（有界浅扫的代价）。
  实测边界（卡目录相对名单目录的层级深度，`_SKIP_DIR_PROBE_DEPTH = 3`）：
  `<root>/node_modules/a/b/` **可见**（深度 2）、`<root>/node_modules/a/b/c/`
  **不可见**（深度 3）。规避方式：改走 `HandoffBus` API（登记通道）或调整
  `data_dir` 布局（把卡目录置于数据根下非名单目录、或减少嵌套）。

### API 路径保证（P1-1 + 双通道登记后成立）

经 `HandoffBus` API（`create_handoff(auto_state_card=True)` / `delete`）写出的卡：
- 恒带 `id` → 形状校验通过；
- 登记**双通道**（主 `handoff-refs/` + 备 `.handoff-refs/`）任一成功 → 来源 3 可见；
- 双通道全失败且为**唯一**可见性路径 → **建卡即失败**（不产生悬空卡）；
- 双通道全失败但目录扫描可达（卡目录在 `state_root` 子树内）→ 来源 1/2 可见。

即：**「API 写入路径的引用恒可见」无条件成立**（原 🟡#1 的「`state_root` 同根 +
登记成功」前置条件已由双通道 + fail-fast 兜住），不存在"写入成功但引用静默悬空"。
**不带前置条件的原「API 路径均不误删」表述曾属过度声明，现已由实现闭环。**

## 4. 风险与影响

- **手写卡落在名单目录**：P0-1 修复后可见（形状判定保留子树）；仅当**再嵌套 ≥ 3 层**
  时仍不可见（有界浅扫的代价，避免 `node_modules` 全树遍历）。深度边界实测见 §3。
- **根外卡 + 登记不可用**：`data_dir` 在 `state_root` 子树**之外**且双通道登记均
  不可用（外部删 `.ref` / 磁盘异常）时，引用不可见 → 共享卡可能被误删。此为
  **baseline 既有面**（`6393886` main 同场景即破坏，非本 PR 引入），属 🟡D 同族，
  见 §5 迁移前提；双通道**写入失败**路径已由 fail-fast 兜住（不产生悬空卡）。
- **无 `id` 的手写卡**：仍不可见（🟡B）。改走 `HandoffBus` API 即覆盖。
- **登记失败**：双通道（主 + 备）任一生效即引用可见；双通道全失败时，子树外布局
  fail-fast（不产生悬空卡）、子树内布局由目录扫描兜底。
- **扫描异常（P0-2）**：扫描抛 `OSError` 时保守**跳过回收**（不误删），且不中断
  归档/台账清理；`registry listdir` 竞态（TOCTOU）已加守卫。
- **路径穿越（P1-3）**：参与路径拼接的标识（`card_id` **及** `source_unit`/
  `target_unit`）均已加白名单校验——`HandoffBus.delete` / `_unregister_ref` /
  台账 `handoff_ids` 不再构成 `data_dir` 之外的**删除**面；`create_handoff` /
  `_save` 亦不再构成**写入**面。

## 5. 迁移前提（🟡 P1-2 文档化）

旧版（≤ `6a2e713`）的**根外**交接卡（`data_dir` 在 `state_root` 子树之外）无
`handoff-refs/*.ref` 登记 → 在扫描界内不可达、无登记可见 → 引用仍可能悬空。

**升级/迁移前提（运维须知）**：

1. **根内卡**：`data_dir` 在 `state_root` 子树内（含名单目录名）→ P0-1 修复后
   目录扫描可达，**无需迁移**；
2. **根外卡**：`data_dir` 在 `state_root` 子树之外 → 须**补偿登记**方可被会话侧
   发现。补偿方式（二选一）：
   - 由该卡的持有者改用 `HandoffBus(data_dir=…, state_root=…)` 重新创建/重写该卡
     （`create_handoff` 会自动登记）；或
   - 手工在 `<state_root>/handoff-refs/<card_id>.ref` 写入
     `{"card_id":…, "state_card_id":…, "card_path":<绝对路径>}`。
3. **不变量**：迁移期间若共享 `ho-*` 卡被误删，其根外引用会悬空——建议在迁移
   完成前**冻结**跨根交接卡的删除操作（或先执行补偿登记再删除）。

**判定口径**：`HandoffBus._registry_required()` 即"该卡是否属根外、登记是否为
唯一可见性路径"的实现判据。

## 6. 留痕

- 登记：Issue #8（本草案关联；含 🟡E 第三条 gap —— **已闭环**）
- 代码口径标注：`session.py::_build_ho_reference_index` docstring「可见性保证范围」
- 台账：`issues/TEST-REGRESSION.md` §三 P2-④ + §二 修复 #J
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
