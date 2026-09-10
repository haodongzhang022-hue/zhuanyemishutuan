# 状态管理秘书机制 · 架构设计索引

> module: zhuanyemishutuan · docs/design
> 目标：将 SKILL.state 论文（arxiv 2608.26263）改进为「层级秘书状态管理 + 上下文折叠编号检索 + 缓存命中保障」的通用层会话编排机制。
> Ref: #1

## 1. 设计文件一览

| 文件 | 覆盖交付物 |
|------|-----------|
| `state-secretary-org.md` | 层级秘书组织模型（L0-L3 + 升降/边界 + 与交接总线关系） |
| `state-secretary-schema.md` | 状态 Schema 最小集 + ext 领域扩展机制 |
| `state-secretary-fold.md` | 上下文编号/索引/折叠/检索协议（自动化优先 + 停止追踪） |
| `state-secretary-cache.md` | 缓存命中保障 + 稳定前缀 + 丢弃时机机制 |
| `state-secretary-poc.md` | POC 验证方案（token 消耗与缓存命中 A/B） |
| `state-secretary-module.md` | 落库新增独立模块划分建议（Q5） |
| `state-secretary-handoff-visibility.md` | 交接卡引用可见性契约边界（P0-1/P0-2/P1-1 修复后更新；仅 🟡B 边界随 Issue #8） |

## 2. 对 SKILL.state 论文的三层改进对照

| SKILL.state（论文） | 本方案改进 | 目的 | 对应文档 |
|---|---|---|---|
| 校验后丢弃本步推理 | 保留原文、按任务折叠 + 编号 | 缺原文时可按索引找回，不丢上下文 | fold.md |
| 状态由单一 agent 管理 | 层级秘书层层向上提交 | 每层只掌握本层有效上下文，隔离噪声 | org.md |
| 状态 Schema 预先设计固定字段 | 通用最小集 + 按对话动态分配秘书 | 每个 Issue/对话独享环境，提示词稳定 | schema.md / org.md |

设计原则：**不是「减少信息量」，而是「结构化信息 + 索引化存储 + 分层隔离」**。

## 3. US-1..4 逐条验收对照

| 用户故事 | 验收判据 | 设计落点 | 状态 |
|---|---|---|---|
| US-1 层级状态管理秘书 | 新 Issue/对话自动分配独立 L3 会话秘书，独立环境、不共享上下文 | org.md §3 L0-L3 分层 + §4 自动分配 | ✅ |
| US-2 上下文折叠与编号检索 | 多轮累积折叠为带 `context_id` 索引态；原文不常驻 prompt，缺时按索引精确找回 | fold.md §3 编号/§4 折叠/§5 检索 | ✅ |
| US-3 层级向上提交 | 下级仅提交汇总状态（progress/decisions/open_items），不携全文 | org.md §5 + schema.md §3 | ✅ |
| US-4 缓存命中率保障 | 同一对话前缀稳定不漂移；折叠只索引不常驻、不破坏前缀稳定 | cache.md §3 稳定前缀 | ✅ |

## 4. Q1-Q5 需求澄清落地对照

| 澄清结论 | 落地文档 |
|---|---|
| Q1 四层秘书定义 | org.md §3 |
| Q2 折叠自动化优先、一开始按任务折叠 | fold.md §2/§4 |
| Q3 原文不落地额外存储，只存 todolist 精简层 | fold.md §3.3 + cache.md §4 丢弃时机 |
| Q4 状态最小 Schema + 行业扩展 | schema.md |
| Q5 新增独立模块 | module.md |

## 5. 实现状态（已落地代码）

> **2026-09-08**：状态管理秘书模块已落地可运行代码（`state-secretary/`），
> 代码源自 `hanghangchuzhuangyuan` M9/M10 状态管理层拷贝移植，包名适配为 `secretary.*`。
> 157 项 pytest 测试全部通过（SecretarySession 门面 75 项，含数据生命周期/评审回归），POC benchmark 可运行。
> **2026-09-08 补**：新增 `SecretarySession` 会话门面层——将 state/context/handoff
> 整合为 L3 会话秘书的统一完整接口，一个实例 = 一个会话秘书，开箱即用。

| 设计文档 | 已落地代码 |
|---------|-----------|
| schema.md | `state-secretary/src/secretary/state/models.py` |
| fold.md | `state-secretary/src/secretary/state/fold_engine.py` + `content_archive.py` |
| org.md | `state-secretary/src/secretary/state/hierarchy.py` |
| cache.md | `state-secretary/src/secretary/context/assembler.py` |
| poc.md | `state-secretary/poc/state_m9_benchmark.py` |
| org.md + schema.md + cache.md | `state-secretary/src/secretary/session/session.py` — SecretarySession 门面 |

## 5.1 契约边界

交接卡引用的**可见性保证范围**见 `state-secretary-handoff-visibility.md`：
- 🔴 **P0-1 已修复**：剪枝不再按裸目录名判定（保护子树永不剪枝 + 名单目录形状判定）
  → 名单名目录（`build`/`dist`/`env`/`venv` 等）内真实/遗留交接卡引用可见；
- 🔴 **P0-2 已修复**：`registry isdir→listdir` TOCTOU 加 `OSError` 守卫；扫描异常
  保守跳过回收且不中断清理；
- 🔴 **P1-3 已修复**：`card_id` 白名单校验，`delete`/`_unregister_ref`/台账
  `handoff_ids` 不再构成路径穿越删除面；
- 🟡 **P1-1 已修复**：**双通道登记**（主 `handoff-refs/` + 备 `.handoff-refs/`）
  任一生效即引用可见；双通道全失败且为唯一可见性路径时 fail-fast（原 🟡D/🟡E 闭环）；
- ⚠️ 仅剩 🟡B（形状校验必含 `id`）为已知边界，随 **Issue #8** 跟踪，**不阻断合并**。
核心契约（扫描界 / 引用源并集 / 登记生命周期 / 自身残卡不变量 / 回收语义）已冻结。

## 6. 后续待开发项

| 项 | 状态 | 说明 |
|---|------|------|
| R7 跨行业复合交接 POC | ⏳ 待开发 | R6 标记的下一步：跨行业复合诉求 = 拆子任务 → 各路由到对应单元 → 交接卡汇合 |
| 秘书角色×状态机集成验证 | ⏳ 待验证 | 将 SecretarySession 与 CNB NPC/角色实际使用场景对接验证 |
| CP2 人工确认 | ⏳ 待确认 | 对 POC benchmark 结果做人工验收放行 |
