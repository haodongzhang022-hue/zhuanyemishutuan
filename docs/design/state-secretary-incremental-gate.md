# 增量跑测门禁契约（Incremental Gate）

> version: v1.0 · Issue #14
> 模块：`state-secretary/src/secretary/gate/`
> 用例：`state-secretary/tests/test_incremental_gate_issue14.py`（72 项）
> CLI：`state-secretary/poc/incremental_gate.py`

## 1. 用户口径（原文要点）

1. **只跑增量**是原则：可以漏（后续用时会重新提需求去改），但**不得大量重复冗余**；
2. 「用大量冗余去保一个低质量测试」= **资源错配**——看似形成无限安全，实际不可能；
   它导致**疯狂重复、低质量审查与测试**，把真正有效的测试**阻断或占用**，效率降低；
3. **权责一致**：跑多少就写多少。「本轮跑 59 个就写跑 59 个，**不要写全量**——
   这会形成歧义，导致误导」；
4. **全量须标记**：全量跑测时，哪些是「**默认已跑过**」的必须标出来，否则
   「全量 743 个」会被读成「本轮跑了 743 个」；
5. **过程可视化**：虚拟的多或少都不好，跑测过程与计数须可视化。

## 2. 三态口径（唯一语义源）

| 状态 | 含义 | 处理 |
|---|---|---|
| `added`（新增） | 文件/用例此前**不存在** | **必跑**（未跑过的断言面） |
| `dissolved`（消解） | 文件/用例**改动**（非新增） | 已跑过但**已失效** → 计为消解；仅"证据消解"时复跑 |
| `legacy`（已跑过） | 与增量 diff **无关** | **默认不重复**（允许漏） |

> ⚠️ 关键纠偏：**"改动" ≠ "不用跑"**。改动项属**消解**（旧证据失效），
> 与"已跑过的既有项"是**两种东西**。本契约把两者显式分流，避免"全都不跑"。

## 3. 不变量（Invariants）

1. **计划跑测数 == 报告实跑数**（计划多少 = 跑多少）；
2. **任何"全量"表述必须同时给出「已跑过」数**，否则拒绝落账；
3. **新增用例永不算冗余**（未跑过的断言面必须跑）；
4. **无增量 → 计划为 0**（不得兜底补跑，纯属冗余）；
5. **计划用例只落在 diff 触及的测试文件内**（不得夹带无关用例）；
6. **指纹缺失（浅克隆/diff 截断）→ 保守回退文件级消解**（宁多跑，不漏跑）。

## 4. 冗余剔除（反过渡测试）

命中以下标签的改动**默认不重复跑**（可用 `include_redundant=True` 人工恢复）：

- `doc-append`：文档/留痕/台账/复测/复评/口径类（如 `复评第 N 轮独立复测留痕`）；
- `count-sync`：口径/计数同步类（如 `测试计数同步 308 项`）；
- `cosmetic`：注释/typo/格式化/E501 类。

判定为**非冗余（必须消解）**的关键面：`fix/修复/缺陷/回归/断言/不变量/守卫/
校验/白名单/越权/穿越/竞态/并发/锁/TOCTOU/fail-fast/安全/漏洞/逃逸`。

## 5. 有效覆盖面兜底（允许漏，但不漏有效面）

剔除冗余后，须仍有用例覆盖各**有效面**。
某面无覆盖时**只补 1 例**（`SweepRules.effective_coverage`），
避免"兜底"再次退化为全量冗余。

> **口径收敛（复评第 1 轮）**：兜底**只在 diff 触及的测试文件范围内**生效，
> 不得把与增量无关的既有文件拉进计划——否则与不变量 5 互斥。
> 原表述「9 个测试文件各 ≥1 例」作废（那是全量口径，非增量口径）。
>
> 无增量（计划 0 例）时**不兜底**——此时补跑属纯冗余（不变量 4）。

## 6. 计数口径守卫（权责一致）

`assert_counter(text, executed, total_suite, deferred)` 拒绝：

- **文本未以实跑语境出现本轮实跑数** → 拒绝。实跑数须命中专属标记
  （`本轮实跑 N` / `本轮跑 N` / `实跑 N` / `executed: N` / `跑 N 个测试`），
  **裸数字不算**——序号（`第 6 轮`）、规模数（`全量 359`）、已跑过数
  （`308 例已跑过`）均不得冒充实跑数（🔴-1 定点修复）；
- 「全量/全部通过」等措辞 + 规模数 ≠ 实跑数却**未标记**『已跑过/默认不重复』；
- 声明全量规模但**未给出**已跑过数（读者仍会误读）；
- 出现**未声明**的计数（防台账漂移 / 拼凑数字）；
- 例外：**真全量**（规模数 == 实跑数）放行，但**须绑定实跑语境或 pytest
  汇总证据**（`N passed`）——如「全量 pytest：359 passed」按 `executed=359`
  通过；而裸写「全量 359 项全部通过」不构成证据 → 拒绝（🔴-1）。

合法写法由 `render_count()` 生成：

```
跑 59 个测试（全量 743 例中 684 例已跑过、默认不重复；本轮实跑 59 例）
```

纯增量口径（**推荐**，无歧义）直接写：`本轮跑 59 个测试（增量）`。

`assert_ledger_counts([(名称, 值), …], executed)` 拦截台账漂移
（历史曾发生 228/229 → 237 的口径漂移）。

## 7. 全量告警视图

`IncrementalGate.full_sweep_view(base, executed_now)` 输出：

```
== 全量跑测告警（已跑过项须标记）==
  全量用例: 359 例
  已跑过（默认不重复）: 308 例 ← 须标记
  本轮实跑: 0 例（未提供 --executed 时不冒充；旧实现用计划冒充实跑）
  未消解差异: 69 例
  计数口径: 本轮实跑 0 例（计划 0）· 已跑过不重复 308 · 冗余剔除 69 · 全量规模 377
  ⚠ 不得把上述全量数当作「本轮实跑数」上报。
```

逐项 `previously_run` 标记见 `to_dict()["items"]`。
`assert_full_view()` 在全量数 > 1 却**未标记**已跑过项时拒绝；
**首轮全量**（无历史基线、已跑过 0 且实跑 == 全量）放行（复评第 1 轮修 🟡7）。

## 8. CLI

```bash
# 本轮要跑什么（可视化）
python3 state-secretary/poc/incremental_gate.py plan --base a455608
# 按计划实跑（精确到 nodeid，杜绝"顺手全量"）
python3 state-secretary/poc/incremental_gate.py run  --base a455608
# 全量告警视图（标记已跑过项）
python3 state-secretary/poc/incremental_gate.py full --base a455608
# 合法写法（rc=0）：文本中的实跑数须与 --executed 一致
python3 state-secretary/poc/incremental_gate.py counter \
    --text "跑 51 个测试（全量 359 例中 308 例已跑过、默认不重复；本轮实跑 51 例）" \
    --executed 51 --total 359 --deferred 308

# 反例（会被拒绝，rc=1）：
#   (a) 文本实跑数(69) ≠ --executed(51) → 无法核对"跑多少写多少"
python3 state-secretary/poc/incremental_gate.py counter \
    --text "跑 69 个测试（全量 377 例中 308 例已跑过、默认不重复；本轮实跑 69 例）" \
    --executed 51 --total 359 --deferred 308
#   (b) 文本未含实跑数 51（只有规模/已跑过数）
python3 state-secretary/poc/incremental_gate.py counter \
    --text "全量 308 项全部通过" --executed 51 --total 359 --deferred 308
```

> 🟡-A（复评第 3 轮）：上例 (a) 原被误标为"合法写法"，与实测 rc=1 矛盾；
> 现已改一致（合法例的文本实跑数 = `--executed`），并把原例降为反例 (a)。

退出码：`0` 通过 / `1` 测试红或计数口径违规。

## 9. 已知边界（不阻断，显式登记）

1. **指纹粒度**：以用例主体文本哈希判定失效；同一用例内**非断言面**
   （docstring 措辞）改动也会判为消解 → 属"多跑"，非"漏跑"，方向安全；
2. **新增文件内既有用例**：文件级新增时全部用例计 `added`（含从别处搬来的
   用例）→ 属"多跑"，方向安全；
3. **浅克隆**：`git archive` 不可用时跨版采集回退为文件级消解（保守）；
   基线**完全不可解析**（HEAD 未建立 / 拼写错误）时同理走 `_degraded_plan`
    全树降级（**不**静默空计划），降级原因在 `plan.degraded_reason` 可见；
4. **非 Python 测试**：仅识别 `*.py` 测试面；其他语言测试面需扩展
   `looks_like_test_path`。

## 10. 跨版判别力（防恒真）

| 场景 | 结果（复评第 2 轮实测） |
|---|---|
| 当前 head（门禁实现 + 用例） | **84 passed**（0 skipped；门禁文件，复评第 3 轮） |
| 移植到旧 head（未解决 Issue #14） | 收集期即失败（无 `secretary.gate` 模块） |
| 变异体 **M1**：`plan_sweep` 退化为"总是全量" | **10 FAIL** |
| 变异体 **M2**：计数守卫去掉"全量须标已跑过" | **11 FAIL** |
| 变异体 **M4**：`green` 不纳入 `exit_code` / 计数可信度 | **12 FAIL** |
| 变异体 **M5**：空计划裸跑全量 | **11 FAIL** |
| 变异体 **M6**：`run()` selector 回退仓库相对口径 | **22 FAIL**（rc=4 / 假绿 / 路径口径） |
| 变异体 **M7**：静默丢弃标的（both-empty 不 fail-fast） | **11 FAIL** |
| 变异体 **M8**：`_appears` 退化为"含任意数字" | **16 FAIL**（复评第 1 轮仅 1 FAIL → 判别性已加厚）<br>注：🟡-C 复评第 3 轮澄清——该变异体定义须为**同时**退化 `_appears` 与 `_appears_as_executed`（`_appears` 已不在守卫主路径，单独退化不触发 FAIL） |
| 变异体 **M9**：回退分支用计划数冒充实跑数 | **11 FAIL** |
| 变异体 **M10**：去掉不变量 1（计划 ≠ 实跑不拦） | **11 FAIL** |
| 变异体 **M11**：`deferred` 不剔除 `excluded`（规模重复计数） | **10 FAIL** |
| 变异体 **M12**：全量规模回退为 `planned+deferred+excluded` | **11 FAIL** |
| 变异体 **M13**：未核实计数仍放行落账 | **10 FAIL** |
| 变异体 **M14**：`pythonpath` 回退仓库根相对口径（🔴-A） | **5 FAIL**（多基线端到端 rc=4 + 口径单元） |
| 变异体 **M15**：`counts[status]` 键名不符（🔴-B） | **2 FAIL**（`KeyError('error')` 崩栈） |
| 变异体 **M16**：`_fill_results` 不收窄捕获 `JUnitParseError` | **1 FAIL**（解析异常逃逸） |
| 变异体 **M17**：基线不可解析改为放行（还原旧行为，🔴-C） | **2 FAIL** |
| 变异体 **M18**：降级分支退化为空计划（还原假绿，🔴-C） | **1 FAIL** |
| 变异体 **M19**：沙箱去掉 gpgsign 对消（还原"不封闭沙箱"，🔴-C） | **5 FAIL** |

## 10.1 复评第 2 轮定点修复（🔴-1 / 🔴-2 / 🔴-3 清零）

| 项 | 旧病灶 | 修复 |
|---|---|---|
| 🔴-1 | `_appears()` 只做裸数字出现判定 → 序号（第 6 轮）/ 规模数（全量 359）/ 已跑过数（308）可冒充实跑数；"真全量豁免"凭裸数字直接放行 | `_appears_as_executed()` **语境绑定**：必须命中「本轮实跑 N / 实跑 N / executed: N / 跑 N 个测试」专属标记，且紧后不得接规模/已跑过语境；真全量豁免须绑定实跑语境或 pytest 汇总证据（`N passed`） |
| 🔴-2 | junitxml 解析失败回退时用 `plan.targets` 填充 `executed`，并清空 `passed` → 计划数冒充实跑数、`green=True`、`assert_report` 放行 | 回退分支**不得**用计划数：`counts_source ∈ {junitxml, stdout, none}`；`none` 时 `executed=0`、`green=False`、`assert_report` 抛错；`except Exception` 收窄为 `ET.ParseError/OSError` 且记录降级原因 |
| 🔴-3 | 计划数 ≠ 实跑数无守卫（文件级标的 1 个 → 实跑 3 例放行）；全量规模 `planned+deferred+excluded` 重复计数；`cmd_full` 用计划冒充实跑 | ① `run()`/`assert_report` 显式校验**不变量 1**（计划 == 实跑）；② 规模口径优先取 `collect(head)` **实测收集数**（并集去重仅作回退）；③ `cmd_full` 未给 `--executed` 时 `executed_now=[]`，不冒充 |

顺带处置：🟡1 冗余判定单一语义源（`REDUNDANT_LABELS`）、🟡2 整文件重写判定接线、
🟡3 `src_pythonpath` 接线为 pytest `pythonpath` ini、🟡4 异常收窄 + 降级原因留痕、
🟡5 留痕命令在拼接 `--junitxml=` **之后**生成、🟡6 docstring 修订同步、🟡7 跨句指代反例。

## 10.2 复评第 3 轮定点修复（🔴-A / 🔴-B 清零）

| 项 | 旧病灶 | 修复 |
|---|---|---|
| 🔴-A | `src_pythonpath` 默认值 `state-secretary/src` 是**仓库根相对**，而 pytest 的 `pythonpath` ini 相对 **rootdir**（=`state-secretary/`）解析 → 实际指向 `state-secretary/state-secretary/src`（不存在）→ 目标模块 import 失败 → **rc=2/4 collection error**。真实基线 `run --base e3195ea/d6abaab` 端到端失败（掩盖原因：端到端自测只跑 `--base a455608`，该文件自带 `sys.path.insert`） | 新增 `pythonpath_ini_value()`：注入前一律转**运行目录相对**（`state-secretary/src` → `src`；绝对路径原样）。口径与 `_pytest_rel()` 同源 |
| 🔴-B | `_parse_junit` 的 `counts[status]` 键名不符（`counts` 键为复数 `errors`，`status` 取单数 `error`）→ **任何** junitxml 含 `<error>`（collection error / fixture / setup 错误）→ `KeyError('error')`（**非** `GateError`）→ 破坏公开 API 契约与 CLI 可诊断性（只打印 `[门禁拒绝] 'error'`） | ① 显式状态映射 `_JUNIT_STATUS_TO_COUNT`（`error → errors`）；② 未知状态抛 `JUnitParseError`（`GateError` 子类，已导出）；③ `_fill_results` 收窄捕获纳入 `JUnitParseError` → 明确降级（`counts_source="none"` + 降级原因留痕），**绝不**崩栈 |

**回归防线**：新增 `TestG12Round3PointFixes`（🔴-A/🔴-B 定点 + **多基线端到端
`run`** `e3195ea`/`d6abaab`）+ `TestG13WholeFileRewriteRatioBranch`（🟡-B 比例分支
可复现判别）。多基线用例剔除本门禁文件防递归（其判别面
`test_acceptance_matrix_issue4.py` / `test_secretary_session.py` **无**
`sys.path.insert`，正是 🔴-A 的暴露面）。

## 10.3 收口轮定点修复（🔴-C 清零 · 「查不到」≠「没有」）

> 触发：Issue #14 收口轮全量复跑实测 **2 项失败**，初判像"环境噪声"，
> 追查后确认是**两个真缺陷叠加**——且其中一个正是本门禁最危险的失败模式。

| 项 | 病灶 | 修复 |
|---|---|---|
| 🔴-C1 **沙箱不封闭** | `_sandbox_repo_for_rewrite` 只配 `user.email/name`，**未对消宿主全局 `commit.gpgsign`**。CI 镜像全局 `commit.gpgsign=true` 而签名器不可用 → `git commit` **静默失败（rc=128 *"gpg failed to sign the data"*）** → 沙箱**根本没有 HEAD** → 表现为 `IndexError / AssertionError`。**判别力为零**（无论实现对不对都失败） | 新增 `_hermetic_git_init()`：`commit.gpgsign=false` + `tag.gpgsign=false` + `core.hooksPath=/dev/null`，**并断言 commit 成功 + HEAD 存在**；断言把"沙箱坏了"显式暴露，不再伪装成业务失败 |
| 🔴-C2 **把「查不到」当「没有」** | `changed_files` 用 `_run`（**不校验 rc**）：`git diff --numstat <base>` 在 base 解析失败时返回 **128 + 空 stdout**，与"无变更"**无从区分** → 空条目 / **空计划**。空计划会被读成"本轮无需跑测" = **假绿**（"只跑增量"最危险的失败模式） | ① 新增 `_run_checked()`：git 非零 rc → 抛 `GateError`；② 新增 `_assert_base_usable()` 基线预检；③ `plan_sweep` 对不可解析基线走 **`_degraded_plan()` 保守降级**（文件级消解 = 全树重跑，宁多跑不漏跑）+ `plan.degraded/degraded_reason` **留痕可见**，**绝不返回空计划** |

**为什么不是 fail-fast 一种**：`changed_files`（底层 API）对不可解析基线**抛错**
（调用方自负其责）；生产入口 `plan_sweep` 走**保守降级**——因为这里存在一条
*安全* 的保守路径（不变量 6 的"指纹缺失 → 保守回退文件级消解"），
没必要为"基线缺失"卡住干活。两种处置的共同底线是：**绝不静默给空计划**。

**回归防线**：新增 `TestG14BaseUnresolvable`（5 项，含 2 项反向对照）——
① 不可解析 → 抛错；② **反向对照**：基线合法且确无改动 → 仍返回空列表
（证明只把"查不到"与"没有"分开，没改"真的没有"）；③ 降级**非空**且原因可见；
④ **反向对照**：合法基线 + 真增量 → **不**降级；⑤ 沙箱对宿主 gpgsign 封闭。

**变异矩阵（判别力实证）**：M17/M18/M19 分别触发 **2 / 1 / 5 FAIL**，
本实现上 **5/5 PASS** → 断言非恒真。

## 11. 与既有门禁的关系

- **不改变** `SecretarySession` / `HandoffBus` / `StateManager` 任何行为；
- 复用 `state-secretary/tests/` 既有 308 项（`a455608` 基线）作为"已跑过"基线；
- 与 Issue #4 验收矩阵（`test_acceptance_matrix_issue4.py`）互补：前者锁**实现
  不变量**，本门禁锁**跑测策略与计数口径**。
