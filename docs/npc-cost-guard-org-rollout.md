# NPC 成本护栏 · 组织级通用化方案

> module: 组织级（根组织 `dongdongxiaozu`） · docs/platform
> 关联：Issue #24「优化 cpu 消耗的问题」（本仓成本护栏已落地）
> 版本：v1.0（2026-09-19）
> 目标：把「NPC 成本护栏」从**单仓经验**升级为**组织级共享规则**，一次维护、多仓受益

## 0. 结论先行

| 问题 | 结论 |
| --- | --- |
| 放通用仓库还是组织共享规则？ | **两段式**：共享**规则文件**放一个通用仓库；**约束落点**仍在每个使用方仓库 |
| 组织有没有"共享流水线配置"这种机制？ | **没有全局开关**。`include` / `imports` / `optionsFrom` / `settingsFrom` 是**引用式配置共享**，必须由使用方仓库显式引用 |
| 那"组织级"怎么落地才稳？ | 用**一套可复制的 canonical 片段 + 一条强校验 + 一处配额开关**，把"共享"做成可验证的约束，而不是靠自觉 |

一句话：**共享的是文件与校验，不是平台开关。** 期望"组织设置里配一次、所有仓库自动生效"在 CNB 当前机制下不成立，
任何宣称"组织级自动继承"的方案都会在真实仓库里退化成"各写各的"。

## 1. 平台机制事实（决定方案形态）

以下均为官方文档口径，先校正预期再谈落地。

| 能力 | 是否支持 | 事实 | 文档 |
| --- | --- | --- | --- |
| 组织级全局流水线配置 | ❌ | 无此入口；配置只在仓库的 `.cnb.yml` / `.cnb/settings.yml` 生效 | [触发规则](https://docs.cnb.cool/zh/build/trigger-rule.md) |
| `include` 跨仓引用 YAML | ✅ | 可引用当前仓库或**其他仓库**的文件 URL | [语法手册](https://docs.cnb.cool/zh/build/grammar.md#include) |
| `npc.imports.list` 引入他仓知识库/角色 | ✅ | `.cnb/settings.yml` 中声明要引入的仓库路径 | [NPC 配置](https://docs.cnb.cool/zh/repo/settings-npc.md) |
| NPC 事件流水线来源 | ✅ | 跑在**当前 Issue/PR 所属仓库**下，加载的是 **NPC 所属仓库**的 `.cnb.yml`；未定义的事件走系统默认 | [NPC](https://docs.cnb.cool/zh/build/npc.md#事件执行) |
| `npc:go` 原生成本参数 | ✅ | `model` / `maxTurns` / `contextWindow` / `maxTokens` / `thinkingLevel` / `supportImage`，官方内置 | [npc:go](https://docs.cnb.cool/zh/build/internal-steps/npc/go.md) |
| 引用文件的跨仓权限 | ✅ | 私有且非同源仓库的文件由 `allow_slugs` / `allow_events` / `allow_branches` / `allow_images` 控制 | [文件引用](https://docs.cnb.cool/zh/build/file-reference.md) |
| 根组织范围内的 NPC 跨仓操作 | ✅ | 组织设置开关「允许 NPC 跨仓操作」，放宽**限地**（仍限根组织内），不改限权/限时 | [NPC](https://docs.cnb.cool/zh/build/npc.md#允许-npc-跨仓操作) |
| 组织级 CPU 配额 | ✅ | 在**根组织**维度计费与限额；配额不足时 NPC 会在 `Prepare` 阶段快速失败（`events CPU core-hours are insufficient`） | [定价](https://docs.cnb.cool/zh/pricing.md) |

**关键推论**：组织级能"管"的只有两件事——**根组织配额**（真正的全局开关）和
**被引用的共享文件**（真正的单点维护）。其余全是使用方仓库的自愿引用，必须配校验才稳定。

## 2. 分层设计：哪些是通用规则，哪些必须留在仓库

不要把 `.cnb/npc.yml` 整份抽走。它是**仓库行为**（角色路由、镜像、事件绑定），抽走会让角色语义和成本口径耦合。
正确切法是按**归属**分三层：

| 层 | 内容 | 归属 | 理由 |
| --- | --- | --- | --- |
| L1 通用成本基线 | 核数上限、stage timeout 上限、回合/上下文/token 上限、模型**规范名**、禁项（缺点号模型名、`thinkingLevel: max` 无理由使用） | **通用仓库** | 与业务无关，纯平台经济性，全组织同一口径 |
| L2 共享校验 | 一份校验脚本/测试（值断言 + **值来源**断言 + 声明与实现同源断言） | **通用仓库** | 校验必须与规则同源，否则规则升级校验不跟 |
| L3 仓库特有 | 角色清单、镜像地址、事件绑定、提示词、额外的角色级放宽（需写理由） | **各仓库** | 属于仓库行为，通用仓库不该知道"秘书·评审"是谁 |

判定口诀：**"换个仓库还成立"的进 L1/L2，"换个仓库就不成立"的留 L3。**

## 3. 放哪里：三种载体对比

| 方案 | 载体 | 优点 | 代价 | 适用 |
| --- | --- | --- | --- | --- |
| A 共享规则仓库 | 新建 `dongdongxiaozu/npc-guard`（或复用现有组织级公共仓） | 单一来源；`include` + 校验脚本 + 模板可被多仓引用；改动有 PR 留痕 | 需建仓；引用方要显式接入一次 | **推荐** |
| B 复用系统 NPC 仓库 | 把规则并入已有公共仓（如 `cnb/...`） | 零新建 | 不属于自己的仓，改动不受控 | 不推荐 |
| C 组织设置 | 期望"组织级配置项" | 心智简单 | 平台**无此机制**，不可实现 | 不可行 |

**推荐 A**：建一个组织级通用仓（名字建议 `npc-guard` 或 `platform-templates`），放三样东西：

```text
<org>/npc-guard
├── rules/npc-cost-guard.yml      # L1 基线（机器可读，含 allow_events/allow_slugs）
├── templates/npc-guard-snippet.yml  # 可直接 include 的 job 片段（锚点 + 环境变量）
├── checks/check_npc_cost_guard.py   # L2 校验脚本（各仓 --config 传入自己的 npc.yml）
└── docs/why.md                    # 为什么是这些值（把本仓的实测口径搬过来）
```

## 4. 通用仓库侧：可直接复用的两份文件

### 4.1 L1 基线 `rules/npc-cost-guard.yml`

```yaml
# 组织级 NPC 成本基线（通用仓库维护，引用方只读）
# ⚠️ 权限：私有且非同源仓库的文件必须声明 allow_events / allow_images 才能被引用
allow_slugs: "dongdongxiaozu/**"      # 仅本根组织下的仓库可读
allow_events:
  - "issue.comment@npc"
  - "pull_request.comment@npc"

# ── 成本基线（单位口径见 docs/why.md）────────────────────────────
# cpus 按核计费；NPC 墙钟由模型往返主导，加核不减时只加钱
max_cpus: 2
max_stage_timeout: "2h"
max_turns: 72
max_context_window: "128k"
max_tokens: "64k"
thinking_level_max: "medium"

# ── 模型白名单：只允许规范名（带点）─────────────────────────────
# ⚠️ `deepseek-v4-flash`（缺点号）会被路由到思考档，实测折算 33×
models_allow:
  - deepseek-v4.1-flash
models_deny:
  - deepseek-v4-flash
```

### 4.2 可 `include` 的片段 `templates/npc-guard-snippet.yml`

引用方在自己的 `.cnb.yml` 里 `include` 后，用 `<<:` 合并即可复用锚点，
**不再各自复制 cpus / timeout / maxTurns**——这是"一处改、多仓跟"的关键。

```yaml
# 引用方用法：
# include:
#   - https://cnb.cool/dongdongxiaozu/npc-guard/-/blob/main/templates/npc-guard-snippet.yml
# 然后在角色事件下 <<: *npc-guard-job
.npc-guard-base: &npc-guard-base
  runner:
    cpus: 2              # 组织基线：不按角色重复声明
  stages:
    - name: npc go
      timeout: 2h        # 组织基线：挂死兜底，非目标值
      env:
        DSH_MAX_STEPS: "72"
```

> 想彻底用官方参数而非镜像私有变量时，可把回合上限改成 `npc:go` 的 `options.maxTurns`：
> 官方内置、语义更清晰，且**显式传入时以你的取值为准**（不随模型默认值漂移）。

### 4.3 校验脚本 `checks/check_npc_cost_guard.py`（思路）

把本仓已验证过的三层断言**泛化**成通用校验（本仓实现见
`state-secretary/tests/test_npc_cost_guard.py`，10 项、变异各触发 2 FAIL）：

1. **值断言**：`cpus ≤ max_cpus`、`timeout == max_stage_timeout`、`maxTurns ≤ max_turns`、模型名 ∈ `models_allow`
2. **值来源断言**：`cpus:` 只允许出现在共享锚点**一处**（防下游各写各的）
3. **同源断言**：注释里的实测数字与常量一致（防"改了实现没改声明"）

用法：`python3 check_npc_cost_guard.py --config .cnb/npc.yml --rules rules/npc-cost-guard.yml`，
在**引用方仓库**的 CI 里跑（配置属于该仓，门禁必须在该仓执行）。

## 5. 引用方仓库侧：稳定接入三步

```yaml
# ① .cnb.yml —— 引用共享片段（不做整份搬运）
include:
  - https://cnb.cool/dongdongxiaozu/npc-guard/-/blob/main/templates/npc-guard-snippet.yml

# ② 角色事件下合并锚点，只写本仓特有的东西
"秘书·评审":
  issue.comment@npc:
    - <<: *npc-guard-base
      docker:
        image: docker.cnb.cool/xqitw/docker/npc-dsh:latest
      stages:
        - name: npc go
          <<: *npc-guard-base.stages[0]
          env:
            DSH_MODEL: deepseek-v4.1-flash   # 必须是规范名

# ③ CI 里加门禁（引用方自己的 .cnb.yml，push/pull_request 事件）
main:
  pull_request:
    - stages:
        - name: NPC 成本护栏
          script: |
            curl -fsSL https://cnb.cool/dongdongxiaozu/npc-guard/-/git/raw/main/checks/check_npc_cost_guard.py -o /tmp/check.py
            python3 /tmp/check.py --config .cnb/npc.yml --rules <rules-url>
```

> 跨仓引用私有仓库文件时，务必在共享仓的规则文件里声明 `allow_slugs` / `allow_events`，
> 否则会被权限检查拦下（这是最常见的"接不上"原因）。

## 6. 怎么保证"稳定"：四条硬防线

| # | 防线 | 落点 | 失败模式被拦住的样子 |
| --- | --- | --- | --- |
| D1 | 规则单一来源 | 通用仓库 `rules/` | 有人想调核数 → 只能改基线并被所有人评审 |
| D2 | 值来源断言 | 校验脚本 | 某角色偷偷写 `cpus: 8` → 断言失败（本仓已实测：变异触发 2 FAIL） |
| D3 | 引用方 CI 门禁 | 各仓 `pull_request` 事件 | 改坏配置的 PR 直接不绿，不靠 reviewer 眼力 |
| D4 | 根组织配额 | 组织设置 | 配额不足导致的 `Prepare` 阶段快失败（零产出烧配额）被提前暴露 |

D1–D3 是"人改不错"的机制，D4 是"环境不挖坑"的机制。**缺 D4，D1–D3 省下来的量会被配额失败吃掉。**

## 7. 例外与豁免

- 通用基线是**上限**不是**目标值**，引用方只允许**向下收紧**（`cpus` 更低、`maxTurns` 更小）。
- 确需放宽（如大仓全量构建）时：**只在该角色上放宽**，并在配置注释写明实测理由；
  放宽不得触碰共享锚点（一处加核 = 该仓全部角色加核）。
- 模型白名单的扩展走**共享仓 PR**，禁止在引用方就地新增模型名——那正是 33× 事故的成因。

## 8. 落地顺序（建议）

1. 建 `npc-guard` 共享仓，把本仓已验证的三层校验泛化为通用版（L1/L2 入库）
2. 本仓（zhuanyemishutuan）作为**第一个引用方**接入，验证"引用 + 门禁"闭环真的能跑
3. 补根组织 **events CPU 配额**（唯一全局开关，优先级最高）
4. 组织内逐个仓库接入；接入前不强制，接入后由 CI 保证不回退

## 9. 参考

- [NPC](https://docs.cnb.cool/zh/build/npc.md)｜[npc:go](https://docs.cnb.cool/zh/build/internal-steps/npc/go.md)｜[触发规则](https://docs.cnb.cool/zh/build/trigger-rule.md)
- [文件引用](https://docs.cnb.cool/zh/build/file-reference.md)｜[语法手册 include](https://docs.cnb.cool/zh/build/grammar.md#include)｜[NPC 配置](https://docs.cnb.cool/zh/repo/settings-npc.md)
- [定价](https://docs.cnb.cool/zh/pricing.md)｜[角色权限](https://docs.cnb.cool/zh/guide/role-permissions.md)
- 本仓实测口径：[`dsh-cost-guard.md`](./dsh-cost-guard.md)
