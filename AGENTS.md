# 专业秘书团 · 项目级 Agent 指令

> 本文件由 dsh 运行时自动加载（`/workspace/AGENTS.md`）。
> 全局规则（skill 强绑定、评论发布、安全要求）由镜像内置，此处只写本仓库特有约定。

## 仓库定位

「专业秘书团」（zhuanyemishutuan）：团队协作、任务接力与知识沉淀的协作项目。
- 代码载体：`state-secretary/` —— 状态管理秘书模块（层级状态管理 + 上下文折叠编号检索 + 缓存命中保障）
- 设计文档：`docs/design/`，产品规格：`docs/product/`
- 任务台账：`issues/`（含 `TEST-REGRESSION.md` 回归清单）

## 秘书团角色体系（NPC 角色）

角色定义在 `.cnb/settings.yml`，NPC 事件映射在 `.cnb/npc.yml`。
触发方式：Issue/PR 评论中 `@dongdongxiaozu/zhuanyemishutuan(<角色名>)`。

| 角色 | 职责 |
| --- | --- |
| 秘书·接待 | 强制入口：接收诉求、翻译意图、路由判定，需求不清先反问 |
| 秘书·秘书长 | 管理层：接收汇报、分析安排，不进入具体对话 |
| 秘书·状态管理 | 小组长秘书：维护域内折叠状态，层层向上提交汇总 |
| 秘书·会话 | 一 Issue 一秘书：独立状态机、提示词前缀稳定、缓存命中优先 |
| 秘书·开发 | 按拆解任务实现，提 PR 前过标准检查 |
| 秘书·评审 | 评审/复评，输出处置标签，发现问题 `[回转] 退回:秘书·开发` |
| 秘书·测试 | 仅接「开发自评无问题」的任务，输出信号灯与缺陷分级 |

## 门禁流转规则（v0.2，详见 docs/secretary-gate-workflow.md）

1. 评审发现问题 → 回「秘书·开发」修正，不放行、不接测试
2. 开发修完自评无问题 → 才接「秘书·测试」
3. 明知还有问题 → 不接测试，回开发调整
4. 准备合并之前 → 走最终评审放行

状态机映射：`todo` → `in_progress` → `done`（开发自评通过）→ `blocked`（回转）/ `stopped`（归档）。

## 开发约定

- 测试基线以 `issues/TEST-REGRESSION.md` 台账为准，全量 pytest 必须通过
- 测试运行：`cd state-secretary && python3 -m pytest tests/ -v`
- 模块运行时**无第三方依赖**（仅 Python 标准库），勿引入运行时依赖
- 修改后同步更新对应设计文档与台账计数，避免口径漂移
- Markdown 提交前过 `markdownlint-cli2`（镜像已预装）
