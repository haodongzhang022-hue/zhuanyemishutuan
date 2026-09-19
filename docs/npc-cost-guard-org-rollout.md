# NPC 成本护栏 · 组织级通用化方案

> module: 组织级（根组织 `dongdongxiaozu`） · docs/platform
> 关联：Issue #24「优化 cpu 消耗的问题」（本仓成本护栏已落地）
> 版本：v1.2（2026-09-19，按复评 🔴-1/🔴-2/🔴-3 二次返工）
> 目标：把「NPC 成本护栏」从**单仓经验**升级为**组织级共享规则**，一次维护、多仓受益

## 0. 结论先行

| 问题 | 结论 |
| --- | --- |
| 放通用仓库还是组织共享规则？ | **两段式**：共享**规则与基线文件**放通用仓库；**约束落点**仍在各使用方仓库 |
| 组织有没有"共享流水线配置"机制？ | **没有全局开关**；`include`/`imports`/`optionsFrom` 是**引用式**共享 |
| 那"组织级"怎么落地才稳？ | **可复制的基线文件 + 一条可跑校验 + 一处配额开关**，把"共享"做成可验证的约束 |

一句话：**共享的是文件与校验，不是平台开关。** 期望"组织设置里配一次、
所有仓库自动生效"，在 CNB 当前机制下不成立；任何宣称"组织级自动继承"的方案
都会退化成"各写各的"。

## 1. 平台机制事实（先校正预期，再谈落地）

以下均为官方文档口径，**v1.1 按评审校正了三条**（标 ⚠️ 者为 v1.0 的错误假设）：

| 能力 | 支持 | 事实 | 文档 |
| --- | --- | --- | --- |
| 组织级全局流水线配置 | ❌ | 无此入口；配置只在仓库的 `.cnb.yml` / `.cnb/settings.yml` 生效 | [触发规则][tr] |
| `include` 跨仓引用 YAML | ✅ | 可引用其他仓库的文件；是**配置级合并**，不是文本拼接 | [语法手册][gr] |
| ⚠️ 跨文件 YAML 锚点 | ❌ | `include` 注意事项第 4 条：**不支持**跨文件锚点（`&` / `*`） | 同上 |
| ⚠️ `include` 合并规则 | — | 数组 + 数组 = **追加**；对象 + 对象 = 同名覆盖；数组 + 对象 = 仅取数组 | 同上 |
| `imports` 跨仓引入环境变量 | ✅ | 可引用其他仓库文件，YAML 对象 → 每个 key 即一个环境变量 | [语法手册][gr] |
| `optionsFrom` 跨仓引入任务参数 | ✅ | 内置任务参数文件（`npc:go` 的 `options` 走这条路） | [语法手册][gr] |
| `npc:go` 原生成本参数 | ✅ | `model` / `maxTurns` / `contextWindow` / `maxTokens` / `thinkingLevel` | [npc:go][ng] |
| ⚠️ 引用文件的跨仓鉴权 | ✅ | 字段声明在**被引用的那个文件**上；不可信事件下未声明即被拦 | [文件引用][fr] |
| 根组织 CPU 配额 | ✅ | 根组织维度计费；不足时 NPC 在 `Prepare` 快速失败（零产出烧配额） | [定价][pr] |

**关键推论**：组织级能"管"的只有两件事——**根组织配额**（唯一全局开关）与
**被引用的共享文件**（唯一单点维护）。其余全是使用方的自愿引用，必须配校验才稳定。

## 2. 分层设计：哪些通用，哪些必须留在仓库

不要把 `.cnb/npc.yml` 整份抽走——它是**仓库行为**（角色路由、镜像、事件绑定）。
按**归属**分三层：

| 层 | 内容 | 归属 | 理由 |
| --- | --- | --- | --- |
| L1 通用成本基线 | 核数上限、stage timeout 上限、回合上限、模型规范名、禁项 | **通用仓库** | 与业务无关，纯平台经济性 |
| L2 共享校验 | 值断言 + **值来源**断言 + 路线/通道分派 + 鉴权断言 | **通用仓库** | 校验须与规则同源，否则规则升级校验不跟 |
| L3 仓库特有 | 角色清单、镜像地址、事件绑定、提示词、角色级放宽 | **各仓库** | 通用仓库不该知道"秘书·评审"是谁 |

判定口诀：**"换个仓库还成立"的进 L1/L2，"换个仓库就不成立"的留 L3。**

## 3. 放哪里

| 方案 | 载体 | 优点 | 代价 | 适用 |
| --- | --- | --- | --- | --- |
| A 共享规则仓库 | 新建 `dongdongxiaozu/npc-guard` | 单一来源；多仓可引用；留痕 | 需建仓；接入一次 | **推荐** |
| B 并入系统公共仓 | `cnb/...` | 零新建 | 不属于自己的仓，改动不受控 | 不推荐 |
| C 组织设置 | 期望"组织级配置项" | 心智简单 | 平台**无此机制** | 不可行 |

**本方案的全部交付物已随本 PR 入库**（`docs/npc-guard-org/`，建共享仓时整目录平移）：

```text
docs/npc-guard-org/                 # → 平移为 <org>/npc-guard 仓库根
├── rules/npc-cost-guard.yml        # L1 基线常量 + 鉴权 + 组织标准事件集
├── baseline/npc-guard-env.yml      # 路线 A：镜像路线 env 基线（imports 引入）
├── baseline/npc-go-options.yml     # 路线 A'：npc:go 路线 options 基线（optionsFrom 引入）
├── templates/npc-guard-job.yml     # 路线 B：挂 `$` 的完整 job 段模板（include 引入，含鉴权）
├── checks/check_npc_cost_guard.py  # L2 校验脚本（引用方 CI 调用）
├── samples/repo-mirror/            # 样例①镜像路线 · imports（应 rc=0）
├── samples/repo-npcgo/             # 样例②npc:go 路线 · optionsFrom（应 rc=0）
├── samples/repo-include-min/       # 样例③路线 B 最小用法：只 include、不写本地 key（应 rc=0）
├── samples/repo-include-override/  # 样例④路线 B + 角色级向下收紧（应 rc=0）
├── samples/repo-misc-include/      # 样例⑤无关本地 include + 内联 config（应 rc=0）
├── samples/repo-include-dup/       # 反例⑥同 key 同事件重复写 stages（应 rc=1）
├── samples/repo-local-baseline/    # 反例⑦基线指向仓内自建文件（应 rc=1）
├── samples/repo-flow-cpus/         # 反例⑧flow-style 两角色各写 cpus（应 rc=1）
└── selftest/mutation_matrix.py     # 判别力矩阵（样例 8 + 变异 23，实测 0 漏网）
```

样例里的共享文件路径用**相对资产包根**（`../../baseline/…`）以便离线可跑；
生产接入时换成共享仓 URL（见 §5），校验脚本对两者一视同仁。

## 4. 三条必修项的处置（v1.0 → v1.1）

### 4.1 🔴-1 跨文件锚点：方案核心机制改为"值注入 + 模板"，锚点只在本文件内

v1.0 让引用方 `<<: *npc-guard-base` 复用共享仓锚点，**平台不支持**：`include` 是配置级合并，
`&npc-guard-base` 定义在共享仓文件里，引用方文件里的 `*npc-guard-base` 是未定义别名，
解析即失败（实测见 §4.4）。`<<: *anchor.stages[0]` 更是**非法 YAML**（别名不能带下标路径）。

**两条可选路线，均已提供可跑样例并纳入校验：**

| 路线 | 共享仓提供 | 引用方怎么接 | 适用 |
| --- | --- | --- | --- |
| **A（推荐）** | **基线值文件** | `imports`（env）/`optionsFrom`（options） | 值型护栏（核数/回合/模型） |
| **B（备选）** | **完整 job 段模板**（挂 `$`） | `include` 引入；只为要覆盖的角色写 key | 想复用 job 结构 |

选 A 的理由：值型护栏用官方支持跨仓 + 鉴权的 `imports` / `optionsFrom` 即可承载，
引用方的 `stages` 保持本地可见（可读性、排障都更好），且**天然避开数组追加的重复 stage 陷阱**。

### 4.2 🔴-2 鉴权声明移到被引用文件，`allow_events` 补齐四类事件

v1.0 把 `allow_slugs` / `allow_events` 写进了 `rules/npc-cost-guard.yml`（规则常量文件），
但**真正被引用的是 `baseline/*.yml` 与 `templates/*.yml`**——它们没有鉴权声明，一 include 就被拦下。

v1.1：鉴权字段声明在**每一个被引用文件**上（`baseline/npc-guard-env.yml`、
`baseline/npc-go-options.yml`、`templates/npc-guard-job.yml`），且 `allow_events` 补齐：

```yaml
allow_slugs:
  - "dongdongxiaozu/**"
allow_events:
  - issue.comment@npc
  - pull_request.comment@npc
  - pull_request        # PR 类属不可信事件；门禁本身跑在这个事件上
  - push                # 引用方 CI 门禁的常规触发事件
```

`rules/npc-cost-guard.yml` 另设 `required_allow_events`，把这份集合固化为**组织标准**，
由校验脚本对每个被引用文件逐一核对（漏 `pull_request` / `push` 都会被判 FAIL）。

### 4.3 🔴-3 字段名统一 + 两条路线分列 + 值来源判据重写

**(1) 一套字段名，按路线分派，不混用。** v1.0 在同一份方案里混用了镜像私有变量与官方参数。
v1.1 明确**两条路线不可混用**，校验脚本按 stage 入口自动分派：

| 路线 | 判定入口 | 回合上限字段 | 模型字段 | 生效条件 |
| --- | --- | --- | --- | --- |
| 镜像（本仓） | `script: sh /app/start.sh` | `env.DSH_MAX_STEPS` | `env.DSH_MODEL` | 镜像 |
| `npc:go` | `type: npc:go` | `options.maxTurns` | `options.model` | 平台内置 |

镜像路线写 `options.maxTurns`、或 `npc:go` 路线写 `DSH_MAX_STEPS`，**都是静默失效**——
所以脚本把"混用"直接判 FAIL（变异 M7 / M9 实测各拦 1 条）。

**(2) 基线文件按路线分列。** 两份基线互不替代，引用方按自己的路线选一份：

```yaml
# baseline/npc-guard-env.yml（镜像路线，imports 引入 → 每个 key 即一个环境变量）
DSH_MAX_STEPS: "72"
DSH_MODEL: deepseek-v4.1-flash

# baseline/npc-go-options.yml（npc:go 路线，optionsFrom 引入 → 每个 key 即一个 options 字段）
maxTurns: 72
model: deepseek-v4.1-flash
```

注意 `imports` 只注入 **env**，喂不到 `options`——`npc:go` 路线的基线必须走
`optionsFrom`（通道错配即静默失效，脚本判 FAIL，见 §4.5.2）。

**(3) "值来源"断言在 `include` 场景下重写。** v1.0 的判据是"`cpus:` 只出现一处"，
在 `include` 之后**语义已变**：解析后的配置里锚点会展开成多份（锚点 + 继承），
引用方原文里也可能一处都没有（值来自被引用文件），断言会退化成"0 处"或指向共享仓。

v1.1 改为**两条独立判据，互不替代**（v1.2 再收敛，见 §4.5）：

- **本地声明计数**（防下游各写各的）：数**解析结果**里声明了 `cpus` 的节点，
  按**对象身份去重**——锚点复用是同一对象（计 1 处），各角色各写一份是不同对象（计 N 处）。
  恰好 1 处 = 自持（唯一来源）；0 处 = 必须由共享文件提供；**≥2 处 = FAIL**。
  （v1.1 按源码行首 `cpus:` 计数，被 flow-style `runner: {cpus: 2}` 绕过 → §4.5.4 🟡-1）
- **引用可读 + 通道正确 + 来源是共享仓**（防"看着像引用"）：把每个 `imports` /
  `optionsFrom` / `include` 指向的文件**实际读出来**，字段缺失、读不到（404 / 路径错）、
  **通道喂不到该字段**、或**不在共享资产包内**，都判 FAIL，不静默放过。
- 附加：**被引用文件自身也过同一套值断言**（含路线 B 模板的嵌套字段）——否则
  "共享值被改坏"是校验盲区（变异 M3 基线模型名去点、M10 回合上限放宽到 200、
  M17 模板三值同改坏，v1.0/v1.1 判据全绿；v1.2 各拦 1–6 条）。

### 4.4 可复现的证据（三个机制事实的实测）

```bash
# ① 跨文件锚点：引用方文件单独解析即失败（ComposerError: undefined alias）
#    ② 别名带下标路径：ScannerError（不是合法 YAML）
#    ③ include 数组追加：同一事件合并出两个 npc go stage
cd state-secretary && python3 -m pytest tests/ -q     # 全量基线（rebase 到 main 后重跑）
python3 docs/npc-guard-org/selftest/mutation_matrix.py # 样例 8 + 变异 23：0 漏网
```

校验脚本内建了**按官方合并规则模拟 `include`** 的判定（`merge_config` + `resolve_includes`，
嵌套上限 64 与平台一致），因此"引用方再写一份 `stages` 会不会重复执行"是**可跑断言**，
不靠人眼推演（反例样例 `samples/repo-include-dup/` 实测被拦）。

## 4.5 复评返工（v1.1 → v1.2）：校验契约三项 🔴 与 🟡 收敛

首轮返工把**方案机制**修对了，但复评指出**校验契约**自身有三处必修——即"文档说会拦、
脚本实际不拦"。v1.2 全部定点修，并补变异覆盖。

### 4.5.1 🔴-1 路线 B 的模板值纳入**同一套**值断言

v1.1 的"基线自身也过值断言"取的是**顶层** `data.get("DSH_MAX_STEPS")`，而路线 B 的护栏值
嵌在 `$.<event>[].stages[].env` 内 → 全部取到 `None` → **静默跳过**。
实测：模板内 `cpus: 2→8`、`DSH_MAX_STEPS: "72"→"500"`、模型名去点（33× 事故签名）
**三处同改**，路线 B 样例仍 rc=0。

v1.2 把断言抽成 `assert_guard_values()`，对**每一个被引用文件**都跑一遍，取值走
`guard_values_of()` 的**全树遍历**（`leaves()`）——平铺基线（`baseline/*.yml`）与
模板嵌套（`templates/*.yml` 的 `runner.cpus` / `stages[].env.*` / `stages[].timeout`）
**同一套判据**。同一变异现在触发 **6 条 FAIL**（变异 M17）。

### 4.5.2 🔴-2 值注入**通道**错配判 FAIL（接上 v1.1 的死代码）

v1.1 里 `shared_ref` / `opts_shared` 算出来却从未使用（pyflakes 报 2 处 unused），
判据退化成"字段名出现在**任意**被引用文件里即可"，与"经哪条通道引入"无关：

- `npc:go` 路线用 `imports` 指向 options 基线 → `maxTurns` 永远进不了 `options` → v1.1 rc=0
- 镜像路线用 `optionsFrom` 指向 env 基线 → `DSH_MAX_STEPS` 永远进不了 `env` → v1.1 rc=0

v1.2 引入 `collect_refs()`：每个被引用文件都带**通道**（`imports` / `optionsFrom` / `include`），
判据是 `字段 ⊆ 通道能喂的字段集`：

| 通道 | 能喂到 | 字段 | 结论 |
| --- | --- | --- | --- |
| `imports` | `env` | `DSH_MAX_STEPS` / `DSH_MODEL` | ✅ |
| `optionsFrom` | `options` | `maxTurns` / `model` | ✅ |
| `include` | 合并进配置 | 两者皆可（路线 B 的 stage 级字段由合并而来） | ✅ |

通道错配**两层都拦**：被引用文件侧（"经 imports 引入却声明 options 字段"）与
stage 侧（"镜像路线的 `DSH_MAX_STEPS` 由 optionsFrom 引入"）。变异 M18 / M19 各拦 3 条。

### 4.5.3 🔴-3 值来源必须**指向共享仓**，仓内自建基线不算共享

v1.1 的 `traceable` 只看字段名，不看引用是否指向共享仓。实测：引用方
`imports: [./local-baseline.yml]`（**仓内自建**、与共享仓无关）→ rc=0。
后果：每个仓自己维护一份"基线"即可全绿，§0 的核心命题与 D1「规则单一来源」在判据上不成立。

v1.2 引入 `make_is_shared()`，共享 = **共享仓 URL（含 `npc-guard`）** 或
**与 `--rules` 同源的资产包目录**（`rules/` / `baseline/` / `templates/` 三者之一）。
离线自测用相对路径指向同一份资产包——与共享仓 URL 等价，校验一视同仁；
仓内自建文件**不在**这三个目录内，一律判 FAIL（反例样例 `samples/repo-local-baseline/`，变异 M20）。

附带修：鉴权审计不再被 `npc-guard` 白名单短路（改为 `--auth-scope`，默认
`guard` = 共享资产包文件 ∪ 声明了护栏字段的文件），跨仓 URL 路径不含 `npc-guard` 也会审。

### 4.5.4 🟡 同批收敛

- **🟡-1 源码计数可绕过**：v1.1 按源码行首数 `cpus:`，被 flow-style
  （`runner: {cpus: 2}`）绕过。改按**解析结果 + 对象身份去重**计数——
  锚点复用是同一对象（计 1 处），各角色各写是不同对象（计 N 处）。
  变异 M21 + 反例样例 `samples/repo-flow-cpus/`。
- **🟡-2 无关本地 include 误报**：v1.1 把**所有** include 的本地文件拉进鉴权审计。
  改为默认只审"共享资产包文件 ∪ 声明了护栏字段的文件"（`--auth-scope` 可调）；
  样例 `samples/repo-misc-include/` 现 rc=0。
- **🟡-3 内联 `config` 误报**：官方 `include` 第 3 种写法 `- config: {...}` 在 v1.1 被收进
  `None` → 报"不可读"。`collect_refs()` 不再把内联对象当文件；`path` + `ignoreError`
  也按平台语义尊重（`ignoreError: true` 时不可读不判 FAIL）。
- **🟡-4 文档与实现不符**：§5.2 的"最小用法"（只 include 模板、不写本地角色 key）
  在 v1.1 实测 rc=1——`find_stages()` 查的是**未合并**原文。v1.2 所有 stage 判定改走
  **合并后配置**；新增样例 `samples/repo-include-min/`（rc=0）与 `repo-include-override/`。
- **🟡-5 口径打架**：PR 正文与台账数字互斥。统一为**实测口径**：
  v1.0 **9** error → v1.1 **6** → v1.2 **2**（均 MD013 行长，全仓既有口径），台账同步。
- **🟡-6 基线口径**：分支落后 `main`。已 rebase 到 `main`（`46910b8`）后重跑全量；
  失败集与 `main` **逐项相同**（`git worktree` 对照实跑）。
- **🟡-7 死代码**：`shared_ref` / `opts_shared` 未使用；`any("stages" in str(j) …)` 语义混乱。
  已删除，pyflakes **0 告警**；重复 stage 判定改走 `iter_jobs()`。

### 4.5.5 判据收敛（🟢）

v1.1 里 `merged_cfg` / `source` / `decls` 三套"值来源"路径并存且未合取。v1.2 收敛为：

1. `cpus_decl_nodes()` —— 本地声明计数（对象身份去重）
2. `collect_refs()` + `make_is_shared()` —— 每个引用的**通道**与**来源**
3. `assert_guard_values()` —— 任何被引用文件都跑同一套值断言

三条合取才是"通过"：**声明数合法 ∧ 通道正确 ∧ 来源是共享仓**。

## 5. 引用方接入：两条路线，各有可跑样例

### 5.1 路线 A（推荐）：基线值注入

```yaml
# .cnb/npc.yml（镜像路线；样例见 samples/repo-mirror/）
.npc-job-base: &npc-job-base          # 锚点仅在本文件内使用
  docker:
    image: docker.cnb.cool/xqitw/docker/npc-dsh:latest
  runner:
    cpus: 2                            # 全仓唯一一处（值来源断言）
  services:
    - docker

.base-stage: &base-stage
  script: sh /app/start.sh
  timeout: 2h

"秘书·评审":
  issue.comment@npc:
    - <<: *npc-job-base
      imports:
        - https://cnb.cool/dongdongxiaozu/npc-guard/-/blob/main/baseline/npc-guard-env.yml
      stages:
        - name: npc go
          <<: *base-stage
          env:
            DSH_MODEL: deepseek-v4.1-flash   # 或省略，取基线默认值
```

`npc:go` 路线同构，只把 `imports` 换成 `optionsFrom`（样例见 `samples/repo-npcgo/`）。

### 5.2 路线 B（备选）：完整 job 段模板

模板挂在 **`$`（所有角色共用的默认配置）** 下——这是避开"数组追加"陷阱的关键：

```yaml
# .cnb.yml（样例见 samples/repo-include-override/）
include:
  - https://cnb.cool/dongdongxiaozu/npc-guard/-/blob/main/templates/npc-guard-job.yml

# 本仓只为**个别角色**写顶层 key：角色 key 下的同名事件**覆盖** `$` 下的同名事件
# （官方 NPC 文档口径），因此不会重复执行。只写要覆盖的字段即可。
"秘书·评审":
  pull_request.comment@npc:
    - docker:
        image: docker.cnb.cool/xqitw/docker/npc-dsh:latest
      runner:
        cpus: 1                      # 允许向下收紧
      stages:
        - name: npc go
          script: sh /app/start.sh
          timeout: 1h                # 允许向下收紧
          env:
            DSH_MAX_STEPS: "48"      # 允许向下收紧
            DSH_MODEL: deepseek-v4.1-flash
```

**最小用法**：只 `include` 模板、**不写任何本地角色 key**——全部沿用 `$` 的组织默认护栏。
v1.1 因 `find_stages()` 查的是未合并原文而误判 rc=1；v1.2 所有 stage 判定改走**合并后配置**，
该用法实测 rc=0（样例 `samples/repo-include-min/`）。

⚠️ 唯一的踩坑写法：**在模板已覆盖的同一顶层 key 的同一事件下再写一份 `stages`**。
`include` 的数组语义是"追加"，会合并出第二个 `npc go` stage（重复执行）。
校验脚本按官方合并规则模拟 `include` 后判定，实测拦住（`samples/repo-include-dup/`，变异 M12）。

### 5.3 门禁：在引用方 CI 跑（配置属于该仓，门禁必须在该仓执行）

```yaml
main:
  pull_request:
    - stages:
        - name: NPC 成本护栏
          script: |
            python3 -m pip install --quiet pyyaml   # 依赖显式声明，缺 yaml 会 rc=2 大声失败
            python3 checks/check_npc_cost_guard.py \
              --config .cnb/npc.yml \
              --rules https://cnb.cool/dongdongxiaozu/npc-guard/-/blob/main/rules/npc-cost-guard.yml
```

- **依赖**：脚本需要 `pyyaml`（本仓测试用 `pytest.importorskip("yaml")` 兜底，
  但 CI 门禁必须显式安装——缺 yaml 时脚本以 **rc=2** 失败，不静默跳过）。
- **URL 形式**：平台配置引用（`include` / `imports` / `optionsFrom`）用 `/-/blob/…`；
  CI 里用 `curl` / `urllib` 拉原文要用 `/-/git/raw/…`。两者都可读（实测均 200），
  但混用容易照抄错，**校验脚本对两种写法都做了归一**（内部转 raw 再读）。
- 脚本可直读 `--rules` 的 URL，**无需额外 curl 一步**。

## 6. 稳定运行：四条硬防线

| # | 防线 | 落点 | 被拦住的样子 |
| --- | --- | --- | --- |
| D1 | 规则单一来源 | 共享仓 `rules/`/`baseline/`/`templates/` | 调核数只能改共享仓；仓内自建 → FAIL |
| D2 | 值来源 + 通道断言 | 校验脚本 | 某角色偷写 `cpus: 8` → FAIL（3 条）；通道错配 → FAIL（M18/M19） |
| D3 | 引用方 CI 门禁 | 各仓 `pull_request` 事件 | 改坏配置的 PR 直接不绿，不靠 reviewer 眼力 |
| D4 | 根组织配额 | 组织设置 | 配额不足导致的 `Prepare` 快失败（零产出烧配额）被提前暴露 |

D1–D3 是"人改不错"的机制，D4 是"环境不挖坑"的机制。**缺 D4，D1–D3 省下来的量会被配额失败吃掉**：
本仓台账 §32.6 已实测配额不足导致**近 30 次/天零产出快失败**。

## 7. 落地顺序（配额提到第 1 步）

1. **补根组织 events CPU 配额**——唯一全局开关；配额不补，后面每一步都可能被 `Prepare` 吃掉
2. 建 `npc-guard` 共享仓，把 `docs/npc-guard-org/` 整目录平移（L1/L2 入库，自测可跑）
3. 本仓（zhuanyemishutuan）作为**第一个引用方**接入，验证"引用 + 门禁"闭环
4. 组织内逐个仓库接入；接入前不强制，接入后由 CI 保证不回退

## 8. 例外与豁免

- 通用基线是**上限**不是**目标值**，引用方只允许**向下收紧**（`cpus` 更低、回合上限更小）。
- 确需放宽（如大仓全量构建）：**只在该角色上放宽**，并在配置注释写明实测理由；
  放宽不得触碰共享基线（一处加核 = 该仓全部角色加核）。
- 模型白名单扩展走**共享仓 PR**，禁止在引用方就地新增模型名——那正是 33× 事故的成因。

## 9. 自测与判别力（可复验）

```bash
python3 docs/npc-guard-org/selftest/mutation_matrix.py
```

**样例矩阵**（期望 rc 逐个核对，不符即判失败）：

| 样例 | 形态 | 期望 | 实测 |
| --- | --- | --- | --- |
| `repo-mirror` | 镜像路线 · `imports` | 0 | 0 |
| `repo-npcgo` | `npc:go` 路线 · `optionsFrom` | 0 | 0 |
| `repo-include-min` | 路线 B 最小用法（只 include） | 0 | 0 |
| `repo-include-override` | 路线 B + 角色级向下收紧 | 0 | 0 |
| `repo-misc-include` | 无关本地 include + 内联 `config` | 0 | 0 |
| `repo-include-dup` | 反例：同 key 同事件重复 `stages` | 1 | 1 |
| `repo-local-baseline` | 反例：基线指向仓内自建文件 | 1 | 1 |
| `repo-flow-cpus` | 反例：flow-style 两角色各写 `cpus` | 1 | 1 |

**变异矩阵**（每个变异实测触发 FAIL 条数）：

| 变异 | FAIL | 变异 | FAIL |
| --- | --- | --- | --- |
| M1 `cpus: 2` → `8` | 3 | M11 基线引用指向不存在文件 | 3 |
| M2 `timeout: 2h` → `10h` | 5 | M12 与模板重复写 `stages` | 1 |
| M3 基线模型名去点 | 1 | M13 模板漏 `allow_events` | 1 |
| M4 基线去掉 `DSH_MAX_STEPS` | 2 | M14 模板漏 `allow_slugs` | 1 |
| M5 基线去掉 `DSH_MODEL` | 2 | M15 `allow_events` 漏 `pull_request` | 1 |
| M6 本地再写一处 `cpus` | 1 | M16 基线文件缺鉴权声明 | 2 |
| M7 镜像路线混用 `maxTurns` | 1 | **M17 模板值三处同改坏** | **6** |
| M8 `npc:go` 基线去 `maxTurns` | 2 | **M18 `npc:go` 走 `imports` 指 options** | 3 |
| M9 `npc:go` 混用 `DSH_MAX_STEPS` | 1 | **M19 镜像走 `optionsFrom` 指 env** | **3** |
| M10 基线回合上限 72 → 200 | 1 | **M20 基线指向仓内自建文件** | **2** |
| | | **M21 flow-style 两角色各写 `cpus`** | **1** |

**23 个变异、0 漏网**（M20b / M21b 为自带红的反例样例探针，不计入"基线必须全绿"）。
8 个样例期望 rc 全部相符；**本仓现状**（`--config .cnb/npc.yml`）rc=0。

判别力对照本仓既有口径（M1/M2/M3 各触发 2 FAIL）：同三个变异在本方案下触发 3 / 5 / 1 条。
v1.1 的三处盲区（模板值零断言 / 通道错配 / 仓内自建基线）在 v1.1 下**全绿**，
v1.2 分别拦 6 / 3+3 / 2 条——**新增变异正是为这三处盲区准备的判别力证据**。

## 10. 已知边界（用之前先知道）

- **`imports` 文件里的一切 key 都会变成环境变量**（官方口径：文件内容解析为对象，
  属性名为环境变量名）。因此基线文件里的 `allow_slugs` / `allow_events` 会顺带注入成
  两个环境变量。实测无害（只是两串 glob 文本，不覆盖任何护栏变量），但**不要在基线文件里
  放与护栏同名的 key**，否则会覆盖真值。
- **`imports` 的本地相对路径会拼成"文件页面地址"加载**（本地有、远端无 → 不生效）。
  故路线 A 的样例即使写相对路径，**也必须先提交入库**；否则请在 CI 里用共享仓 URL。
- **`include` 的本地路径相对项目根**，且嵌套上限 64 个文件；`include` **不能**引用密钥仓库文件
  （合并后的完整配置会展示在构建详情页）。
- **路线 B 依赖"角色 key 覆盖 `$`"这一官方合并语义**：同一顶层 key 的同一事件写两份 `stages`
  仍会重复执行（校验脚本拦得住，但别指望平台报错）。
- **不可信事件下 `CNB_TOKEN` 权限被封顶**（非 NPC 的不可信事件最大 `Reporter`）：
  门禁若需要跨仓写权限，请放到 `push` / `pull_request.target` 等可信事件上。
- **门禁脚本依赖 `pyyaml`**：缺依赖时以 rc=2 失败（不静默跳过），引用方 CI 需显式安装。

## 11. 参考

- [NPC 任务][np]
- [npc:go 内置任务][ng]
- [语法手册][gr]（`include` / `imports` / `optionsFrom`）
- [文件引用鉴权][fr]
- [触发规则][tr]
- [定价][pr]
- 本仓实测口径：[`dsh-cost-guard.md`](./dsh-cost-guard.md)
- 交付物目录：[`npc-guard-org/`](./npc-guard-org/)

[np]: https://docs.cnb.cool/zh/build/npc.md
[ng]: https://docs.cnb.cool/zh/build/internal-steps/npc/go.md
[gr]: https://docs.cnb.cool/zh/build/grammar.md
[fr]: https://docs.cnb.cool/zh/build/file-reference.md#权限检查
[tr]: https://docs.cnb.cool/zh/build/trigger-rule.md
[pr]: https://docs.cnb.cool/zh/pricing.md
