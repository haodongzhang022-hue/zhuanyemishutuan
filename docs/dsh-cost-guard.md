# NPC 成本护栏（CPU / 核时 / 模型路由）

> module: zhuanyemishutuan · docs
> 关联：Issue #24「优化 cpu 消耗的问题」
> 版本：v0.4（2026-09-19）
> 配置载体：`.cnb/npc.yml` ｜ 回归载体：`state-secretary/tests/test_npc_cost_guard.py`

## 1. 结论先行

本仓库 NPC 任务的**异常高消耗**不是"某个角色写错了"，而是**三个默认值叠加**的结果：

| # | 问题 | 实测代价 | 处置 |
| --- | --- | --- | --- |
| 🔴 A | 共享锚点 `runner.cpus: 2` 写的是 **8 核**（平台实测 labels `cpus=8`） | 190 次 NPC 构建 = **744 核时**（93 机时） | 收到 2 核 → 186 核时 |
| 🔴 B | `DSH_MODEL: deepseek-v4-flash`（**缺点号**）被路由到**思考模型** | 161 请求 **58 770 毫度**；对照组 17.0 M prompt / 260 请求仅 13 360 毫度 = **33×** | 改规范名 `deepseek-v4.1-flash` |
| 🟡 C | `stage timeout: 10h` 无回合上限 | 单次异常最多 **20 核时**（2h × 8 核） | timeout 收 2h + `DSH_MAX_STEPS=72` |
| 🟡 D | PR 评论 `@npc` 循环触发 | 107 次构建 / 19 次 error / **58.3 机时** | 护栏 + 评论节流（流程侧，见 §5） |

## 2. 取证口径（可复跑）

```bash
# 构建清单与事件分布
cnb build get-build-logs --repo dongdongxiaozu/zhuanyemishutuan --page-size 100
# 单次构建的 AI 用量
cnb build get-build-ai-audit  --repo ... --sn <sn> --pipelineId <sn>-001
# 单次构建的请求明细（模型名、思考令牌、响应耗时）
cnb build get-build-ai-request --repo ... --sn <sn> --pipelineId <sn>-001 --page 0
# Stage 耗时与日志
cnb pulls get-ci-timing --sn <sn>
cnb build get-build-stage --repo ... --sn <sn> --pipelineId <sn>-001 --stageId stage-0
```

### 2.1 事件分布（190 次 NPC 构建）

| 事件 | 次数 | error | 累计机时 | 核时 @8 |
| --- | --- | --- | --- | --- |
| `pull_request.comment@npc` | 107 | 19 | 58.3 h | 466.7 |
| `issue.comment@npc` | 83 | 1 | 34.6 h | 277.1 |
| 其它（push / issue.*） | 35 | 29 | 0.1 h | 0.8 |
| **合计** | **190** | | **93.0 h** | **744.6** |

峰值：`2026-09-10T06` 单小时 **111 核时**；`2026-09-18T21` **81 核时**。

### 2.2 单次极值

| 构建 | 回合 | 墙钟 | 核时 @8 | prompt tokens |
| --- | --- | --- | --- | --- |
| `cnb-p55-1k2sl2h3g` | 258 | 45 min | 6.0 | 35.0 M |
| `cnb-1fv-1k2qfhqvi` | 195 | 15 min | 2.0 | 28.1 M |
| `cnb-teg-1k2sj1s9b`（**error**） | 160 | 37 min | 5.1 | 17.0 M |

`cnb-p55` 单次即占全仓 NPC 总核时的 **0.8%**，且 258 回合全部在改**同一个文件**
（`trail.py`）的同一处扫描逻辑——`agent` 在"改-测-回滚"里空转，最终 3 行产出的
单核时成本是正常任务的 10 倍以上。

### 2.3 CPU 是不是真瓶颈？（这决定了"加核"没有意义）

`npc go` Stage 占总耗时 **99.8%**（45m30s / 45m37s），但其中：

- `Promise` 工具的**全部**内容都是纯文本（`read` / `grep` / `write`），近零 CPU；
- 唯一进容器的 `bash` 工具，实测 `docker pull python:3.11-slim` + `docker run`
  单次 **0.6–0.9 s**（镜像已本地命中），单次构建 23 次拉取合计 < 25 s；
- 耗时主体是**模型往返**：3.3–10.6 s/回合，258 回合 ≈ 45 min。

⇒ **墙钟由模型主导，给 8 核与给 2 核墙钟几乎不变，核时开销差 4×。**
这是本次最重要的结论：**CPU 高消耗的根因是"按核计费的核数给多了"，不是"CPU 真的忙"。**

### 2.4 模型路由异常（最隐蔽的一项）

同一角色、同一分支、不同时间的两组构建，模型名只差一个点：

| 构建 | 配置值 | 平台 `model` | 请求数 | 毫度 | 单请求 response_start 中位 |
| --- | --- | --- | --- | --- | --- |
| `cnb-teg-1k2sj1s9b` | `deepseek-v4-flash` | `deepseek-v4-flash` | 159 | **58 770** | **7 659 ms**（124/159 带 thinking） |
| `cnb-p55-1k2sl2h3g` | `deepseek-v4.1-flash` | `deepseek-v4.1-flash` | 258 | 16 870 | 1 500 ms |

折算到**单位请求**：`58 770 / 161 = 365` 对 `13 360 / 260 = 51` → **7.1×**；
折算到**单位 prompt token**（两组 token 量差 4×，摊平后）→ **33×**。

⇒ 缺点号的 `deepseek-v4-flash` 指向的是**思考档**，不是 flash 档。
**注意：`DSH_MODEL` 写错不会报错**，静默降级/升档，只能靠用量报表发现——
所以本次把这条写成了回归断言（`test_model_name_not_the_dotless_variant`）。

## 3. 已落地的改动

`.cnb/npc.yml`：

- `runner.cpus`：8 → **2**，并注明"按核计费、墙钟由模型主导"，且**只允许在共享锚点出现一处**
  （`test_anchor_is_the_single_source_of_core_count`）
- `stage timeout`：10h → **2h**，并注明"是挂死兜底不是目标值"
- 新增 `DSH_MAX_STEPS: "72"`：回合上限，触顶**带部分成果收尾**并在评论写明未完成项
- 全部 17 处 `DSH_MODEL: deepseek-v4-flash` → **`deepseek-v4.1-flash`**
- 头部补 §成本护栏说明（含实测数字，供下一棒判断）

`state-secretary/tests/test_npc_cost_guard.py`：**10 项**回归，分三层
（值断言 / 值来源断言 / 声明与实现同源断言）。变异判别力实测：

| 变异 | 触发 FAIL |
| --- | --- |
| M1 `cpus: 2` → `8` | 2 |
| M2 `timeout: 2h` → `10h` | 2 |
| M3 模型名去点 | 2 |

## 4. 收益估算

| 项 | 改动前 | 改动后 | 降幅 |
| --- | --- | --- | --- |
| 核时（同样 190 次任务） | 744.6 | **186.2** | **−75%** |
| 单次异常风险上限 | 20 核时（2h × 8） | **4 核时**（2h × 2） | −80% |
| 缺点号模型档折算 | 365 毫度/请求 | 51 毫度/请求 | **−86%** |
| 回合失控上限 | 无（跑满 timeout） | 72 回合 | 有界 |

## 5. 仍建议在流程侧处理（本次未改，避免越权）

- **PR 评论 `@npc` 自触发**：`@CodeBuddy` 的评论会被下次 push/评论再次拉起，
  形成接力放大。建议在编排层加：同一 PR 同一分钟内的 `@npc` 合并为一次；
  NPC 自身产出的评论不触发下一棒，改由人工或明确的接力卡驱动。
- **长尾 stall 判定**：`cnb-teg` 跑了 37 min 后 error，实际是 agent 自我回滚
  （`cp .scratch/trail_old.py trail.py`）导致零产出。建议 `DSH_MAX_STEPS` 之外，
  再加"连续 N 回合无文件产出即收尾"的软护栏。
- **根组织 CPU 配额**：`cnb-1po` 的失败根因是
  `Root Group's events CPU core-hours are insufficient for pre-freezing`——
  **配额已不够预冻结（5 min ≈ 0.67 核时）**。在配额补齐前，
  NPC 构建会持续在 `Prepare` 阶段以 4–7 s 快速失败（近 30 次/天），
  这部分"零产出烧配额"应优先处理。

## 6. 验证

- 配置校验：`.cnb/npc.yml` YAML 语法通过（语义/Schema 告警为**既有**：
  角色名顶层 key 是平台原生路由，校验器不识别，非本次引入）
- 回归：
`cd state-secretary && python3 -m pytest tests/test_npc_cost_guard.py -v` → **10 passed**
- 全量：见 PR 描述
