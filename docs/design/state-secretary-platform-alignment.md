# 设计：平台语义对齐 + 官方最佳实践对照

> Ref: #14 本轮 · 版本 v0.1 · 模块 `state-secretary/src/secretary/collab/platform.py`
> ＋ `state-secretary/src/secretary/review/fingerprint.py`

## 1. 需求（用户原话）

> 检查一下这个功能是否正常了，然后这个功能可能也有其他用户开发了，可以检索一下有没有。
> 有可能开发的比我们处理的要好，或者**官方有没有对此有最佳实践可以参考过来**。
> 帮助我们把这个功能开发到完善，然后**分配到组织下，实现各个仓库的通用**。

三件事：① 验证现状是否正常；② 检索同类实现/官方最佳实践；③ 完善并分发到组织。

## 2. ① 现状验证（结论：跑得通，且机制真的拦得住）

| 验证项 | 结果 |
|---|---|
| 增量跑法端到端（`scripts/ci-incremental-test.sh`） | 第 1 次 `run` → **533 passed**（只跑变更相关） |
| 同口径第 2 次 | `skip_duplicate` → **本次跳过（不消耗构建/测试）** |
| `CNB_WINDOW_REPEATS=3` | `blocked_stability` + 替代口径 → 跳过 |
| 全量回归 | **619 passed**（本轮前基线） |

## 3. ② 检索结果：官方有最佳实践，且**推翻了我们两处设计**

### 3.1 `cnb:await` 的两条硬约束（此前未落地）

出处 `/zh/build/internal-steps/cnb/await-resolve.md`：

| 官方原文 | 原实现 | 后果 | 收口 |
|---|---|---|---|
| 「只能对**同一个事件**触发的 pipeline 进行 await 和 resolve 操作」 | 不校验两侧事件 | 探测/干活 pipeline 常由 `crontab`/`api_trigger` 触发，放锁方多在 `push`/评论事件 → **永远等不到 resolve**，靠平台死锁检测才发现 | `await_eligible()`：不同事件/不可判定 → **降级 `crontab`** |
| 「一个 `key` **仅能 resolve 一次**，但可以 await 多次」 | 同一检查点复用同一 key | 第 2 次及以后的探测**必死等**（key 已被消费） | `await_key_for()`：key 带**轮次**，每轮唯一 |
| 「await-resolve 只能运行在流水线的 `stages` 中」（`endStages`/`failStages` 不生效） | 不校验位置 | 收尾/失败阶段挂 await **直接不生效** | `await_stage_ok()` |

补充：平台**自带死锁检测**（互相 await / await 环 / await 不存在的 key / 重复 resolve）
→ 会结束等待并抛 `dead lock found.`。这条是**兜底**，不是"可以设计成依赖它"的理由。

### 3.2 官方最佳实践对照（同类实现）

| 场景 | 官方能力（出处） | 我们已有 | 本轮补的缺口 |
|---|---|---|---|
| PR 评审 | `cnbcool/code-review`：diff 增量、过滤非代码文件、`include_existing_comments`（拉历史评论避免重复）、`min_severity`/`min_confidence`/`exclude_categories` 降噪、`max_comments` 截断、`review_event: auto` 自动表态 | `decide_scan()` 范围分级；`begin_review()` 只在开启评审时记版本；`RunScopeGuard` 同口径只跑一次 | **确定性**去重：官方把"别重复刷评论"交给**模型判断**（本次与上次不一定一致）；我们补 `ReviewFingerprintLedger`（类别 + 语义锚点，**不含路径/行号**），重复项**结转**不回转 |
| 增量扫描 | `sca-vulnerability`：「同一组件同一 CVE **不重复阻断**」「**内容未变化的文件重命名不重复产生**」 | 同口径指纹（范围+版本+受影响面+命令） | 把"改名不产生新工作"升级为**通用不变式**（锚点不含路径）；并补**能力降级保守放行** |
| 零占用等待 | `cnb:await`/`cnb:resolve` 挂起不占核时 + 平台死锁检测 | `probe` 的 `platform_await` 落点 + 固定 `*/5` crontab 兜底 | 挂之前先过**同一事件**与 **key 一次性**约束 |
| 计费 | 只有 CPU 核时（按实际运行时长）与 AI Credits（按 Token），**无"等待"项** | `cost_note()` / `PlatformCostNote` | 降级口径补一句：`crontab` 仍是**平台**在等，本地 CPU 恒为 0 |

**结论**：官方在**评审内容层**更成熟（降噪/去重/表态），我们在**协同调度层**更细
（多仓一份规则、熔断预算、探测预算、能力降级）。两层互补，故本轮**取官方做对的点
补齐我们**，而不是重复实现一个评审插件。

## 4. 判定与降级口径

```
落点判定：_driver_for_delay(delay)   # 阈值分工：≤300s → await；>300s → crontab
      ↓
平台校验：resolve_driver(...)        # await 硬约束（同一事件 / stages）
      ↓
  满足 → platform_await（key = probe:<ck>:r<attempt>）
  不满足 → platform_crontab（固定 */5）+ 降级理由留痕
```

| 情形 | 落点 |
|---|---|
| 两侧事件**相同** 且 `stage == stages` | `platform_await` |
| 两侧事件**不同** / **任一不可判定** | 降级 `platform_crontab` |
| 等待方本身是 `crontab` 事件 | 降级（平台到点会再来一次） |
| `stage` 为 `endStages`/`failStages` | 降级（官方不生效） |

**永不落到"本地空转"**（降级目标只有平台落点）。

## 5. 能力探测与降级（判不出就不增量）

```
RunScopeGuard.decide(...)
      ↓
impact_available(revision, base, impact, event)     # 能不能可信地算受影响面
      ↓
  不可信 → conservative_run（degraded=True + degrade_reason）→ **放行**
  可信   → 正常判重（同口径第 2 次起 skip_duplicate）
```

| 情形 | 判定 |
|---|---|
| 有 `revision` + 具体受影响面 | 正常增量判重 |
| 缺 `revision` | 降级放行 |
| `base=""`（新建分支）或 `event=branch.create` | 降级放行 |
| 有 `base` 但受影响面为空 | 降级放行（"算出来是空"vs"算不出来"无法区分） |

理由：空 impact **本身是合法指纹** → 不同变更集撞成同一个 → **该跑的被跳过**。
**漏跑比多跑危险**，故取安全一侧。

## 6. 评审项指纹（同一问题不报第二遍）

```
fingerprint(category, line_text, context, path)
  = sha1( category ⧺ anchor(line_text, context) )
anchor: 去注释/字面量/数字/操作符空白后的归一化代码文本
        **不含路径、不含行号**  ← 改名与整体移位不算新问题
```

| 行为 | 结果 |
|---|---|
| 内容相同、路径不同（改名） | **同一问题**（`duplicate`） |
| 行号位移 | **同一问题** |
| 代码内容变了 | **新问题**（`fresh`） |
| 类别不同（bug vs security） | **不同问题** |
| `style` / `nit` | **过滤**（官方默认屏蔽） |
| 低于 `min_severity` | **过滤** |
| 锁文件/图片/构建产物 | **非代码，不评审** |
| 老问题重复报 | **结转**，`should_rotate=False`（不产生新一轮修复） |
| 形状不符的条目 | 忽略（不因一条坏数据整轮失败） |
| 台账损坏 | 当空台账（不挡住干活） |

## 7. 分发到组织（各仓库通用）

| 分发物 | 作用 | 关键约束 |
|---|---|---|
| `shared/collab/organization.yaml` | 组织级**语义基线**（全组织一份） | 各仓只需这一份；`load_rules()` 从任意子目录向上取到**同一份** |
| `shared/collab/collab-rules.yaml` | 仓库级**数值**覆写模板 | **已存在则不覆盖**（那是该仓的数值差异） |
| `scripts/distribute-collab-rules.sh` | 分发**并校验** | 默认 **dry-run**；`--apply` 才写；每仓建分支、不直推 main |

**为什么是脚本不是文档**：口径漂移的根因从来不是"没人写文档"，而是"每仓各写一套、
写完没人验"。分发必须**可执行**且**可校验**。

## 8. 测试

- `tests/test_platform_semantics_issue14.py`（42 项）：同一事件约束（反向：不同事件/
  不可判定不得挂）、key 按轮次唯一（反向：不可判定回落 0）、降级只落平台落点
  （反向：非 await 落点不被降级）、跨仓不自读本地检查点（反向：本地有同名检查点也
  不得影响）、能力降级（反向：受影响面具体时判重照常生效）、官方对照表三层齐全、
  分发可移植性（组织级单文件足够 / 脚本可执行且语法合法 / 默认 dry-run）。
- `tests/test_review_fingerprint_issue14.py`（23 项）：改名/移位不算新问题
  （反向：代码变了必须判新、类别不同必须判新）、降噪照抄官方默认、非代码文件过滤
  （反向：真实源码必须算代码）、重复项不回转、结转幂等、跨实例可比、损坏台账不阻塞。
- 既有 `test_checkpoint_probe_issue14.py` / `test_collab_confirm_issue14.py` 中
  6 项断言按**新（正确）口径**更新，并各自补了反向对照用例。
