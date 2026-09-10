# 交接卡引用可见性 · 契约边界（草案 · 待总师裁定）

> module: zhuanyemishutuan · docs/design
> 关联：Issue #4（P2-① 共享 `ho-*` 卡跨会话误删）· Issue #8（P2-④ 可见性契约边界）
> 载体：`state-secretary/src/secretary/session/session.py` → `_build_ho_reference_index`
> 状态：**草案**。核心契约已冻结（见 §2），**可见性保证范围（§3）待总师裁定**。

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

## 3. 待裁定：可见性保证范围（🟡A / 🟡B / 🟡D）

现状（`570c7ee` 起）为性能/鲁棒收紧了两处启发式，**收窄了可见性覆盖**：

### 🟡A 剪枝黑名单含歧义目录名
`_SKIP_SCAN_DIRS` 含 `env / build / dist / target / vendor / coverage` 等
**可能被合法选作 `data_dir`** 的名字。落这些目录的**手写（非 API）**交接卡
引用不可见 → 共享卡被误删（评审/测试双方 8/8 目录复现）。

### 🟡B 形状校验收紧必含 `id`
来源 2 要求 `state_card_id` + `source_unit` + `target_unit` + `id` 四字段齐备。
外部/遗留交接卡缺 `id` → 不可见 → 误删（探针实证；含 `id` 对照卡正常保留）。

### 🟡D 旧版（≤ `6a2e713`）根外卡升级路径未覆盖
旧版创建的**根外**交接卡无 `.ref` 登记 → 在 `570c7ee` 下仍悬空。非本 PR 引入，
但修复未覆盖历史数据。

### 关键口径（影响裁定）

🟡A / 🟡B **仅影响非 API 写入路径**（手写 / 遗留 / 外部交接卡）。经
`HandoffBus` API（`create_handoff(auto_state_card=True)`）写出的卡恒有 `id`
且有 `handoff-refs/*.ref` 登记兜底 → 歧义目录与无 `id` 场景**均不误删**。

即：覆盖范围由「全盘可见」收窄为「**API 路径保证**」——**覆盖收窄，非核心契约破坏**，
故定为 🟡（不阻断合并），但**须总师裁定**是否接受该口径。

### 待裁定选项

- **选项 A（接受收窄）**：显式声明「引用可见性仅保证 `HandoffBus` API 写入路径」，
  本草案 §3 转正，🟡A/🟡B/🟡D 维持 🟡 并随 Issue #8 排期。
- **选项 B（要求全盘可见）**：不变量对**手写/遗留卡同样适用** → 🟡A/🟡B
  **升级为 🔴**，需回退复评：移除歧义名剪枝（或改白名单）、放宽 `id` 必含校验、
  并提供旧版根外卡的补偿登记路径。

## 4. 风险与影响

- 选 **A**：手写/外部写入者需改走 `HandoffBus` API，否则引用可能悬空（静默误删）。
  缓解：docstring 已标注口径 + 本草案落地。
- 选 **B**：扫描界放宽 → 全树遍历代价与伪引用误判面回升（🟡A/🟡B 收紧的初衷），
  需重新评估性能与误判（参见 `_SKIP_SCAN_DIRS` 引入时的实测）。

## 5. 留痕

- 登记：Issue #8（本草案关联）
- 代码口径标注：`session.py::_build_ho_reference_index` docstring「可见性保证范围」
- 台账：`issues/TEST-REGRESSION.md` §三 P2-④
