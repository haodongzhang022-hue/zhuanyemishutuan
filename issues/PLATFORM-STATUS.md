# 平台对接状态（2026-09-05）

- CNB Issue #1（open / P1 / labels: feature,design,state-secretary）: https://cnb.cool/dongdongxiaozu/zhuanyemishutuan/-/issues/1
- 承接触发：Issue 评论 @CodeBuddy（结构化 mentions 实体）→ 事件 `issue.comment@npc`（cnb-0rg-1k1nvvbo4）已生成
- 事件接线：.cnb.yml `$: issue.comment@npc → npc:go 秘书·接待`；角色骨架 .cnb/settings.yml（秘书·命名空间）
- DSH 接力卡：RL-000003（架构·总师）/ RL-000004（架构·产品）—— 已绑定 Issue #1，状态 in_progress
- 本地 issues/ISSUE-001-*.md 为需求源文档；平台 Issue 为其正式承接载体

## 触发机制备忘（踩坑记录）
- 仅 push 文件 ≠ 创建 Issue：须用 `cnb issues create-issue` 在平台创建正式 Issue
- 纯文本评论中的 @CodeBuddy 不触发 NPC：须带结构化 mentions（--data JSON）或 UI 编辑器 @ 提及
- npc:go 承接需要 .cnb/settings.yml 角色定义 + .cnb.yml `$` 事件绑定（二者缺一不触发）
- CNB Issue #14（循环熔断 · 避免评审/修复无限循环）: https://cnb.cool/dongdongxiaozu/zhuanyemishutuan/-/issues/14
  - 落地：`state-secretary/src/secretary/loop/loop_guard.py`（轮次预算 + 非必要修复最多 1 次 + 停滞感知 + 静默交接）
  - 门禁接入点：`SecretarySession.review_round` / `silent_handoff` / `resume_loop`
  - 规范：`docs/secretary-gate-workflow.md` §2.1（v0.3）；角色提示词 `.cnb/settings.yml`（评审/测试/秘书长）
  - 补全机制（v0.4 · §2.2/§2.3）：`review/review_arbiter.py`（只在开启评审时记版本 + 防同写锁 + 延迟重试 + 单冲突收口）
    + `review/scope.py`（**日常只审变化，全量只在大版本**，防无效构建/测试）；
    设计：`docs/design/state-secretary-review-arbiter.md`；门面：`begin_review` / `apply_review` / `retry_review` / `resolve_review_conflict` / `review_scope`
