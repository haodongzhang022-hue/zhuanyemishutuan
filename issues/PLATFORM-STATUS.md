# 平台对接状态（2026-09-05 · 更新 2026-09-18 收尾归档）

- CNB Issue #1（**2026-09-18 收尾关闭** / P1 / labels: feature,design,state-secretary）: https://cnb.cool/dongdongxiaozu/zhuanyemishutuan/-/issues/1
- 承接触发：Issue 评论 @CodeBuddy（结构化 mentions 实体）→ 事件 `issue.comment@npc`（cnb-0rg-1k1nvvbo4）已生成
- 事件接线：.cnb.yml `$: issue.comment@npc → npc:go 秘书·接待`；角色骨架 .cnb/settings.yml（秘书·命名空间）
- DSH 接力卡：RL-000003（架构·总师）/ RL-000004（架构·产品）—— 已绑定 Issue #1，状态 in_progress
- 本地 issues/ISSUE-001-*.md 为需求源文档；平台 Issue 为其正式承接载体

## 触发机制备忘（踩坑记录）
- 仅 push 文件 ≠ 创建 Issue：须用 `cnb issues create-issue` 在平台创建正式 Issue
- 纯文本评论中的 @CodeBuddy 不触发 NPC：须带结构化 mentions（--data JSON）或 UI 编辑器 @ 提及
- npc:go 承接需要 .cnb/settings.yml 角色定义 + .cnb.yml `$` 事件绑定（二者缺一不触发）
## 收尾归档（2026-09-18）

- Issue #1 需求已达成：设计 + 可运行实现（`state-secretary/`）+ 验收证据齐备 → 关闭
- 归档文档：`docs/design/state-secretary-closeout.md`（交付物终态 / US-1..4 结论 / 验证基线 / 结转项）
- 结转项载体：Issue #4、#5、#6、#8–#13（P2 边界与小项）、#14（LoopGuard）、#16（DSH 新版适配）

## 新版 dsh 生态适配（2026-09-18 · Issue #16）

- 旧版：平台内置 `npc:go` 执行器 + 评论 `[触发:<角色>]` 文本命中路由
- 新版：接入 `cnpc/dsh` 镜像（`docker.cnb.cool/xqitw/docker/npc-dsh:latest`），
  角色路由升级为平台原生「顶层 key = 角色名」
- 落点：`.cnb/npc.yml`（新增，7 角色 × issue/PR 事件）、`.cnb.yml`（精简为 include）、
  `AGENTS.md`（新增，项目级指令）、`docs/dsh-ecosystem-adaptation.md`（适配说明）
- 触发：`@dongdongxiaozu/zhuanyemishutuan(<角色名>)`，未带角色名走 `$` 段兜底（秘书·接待）
- 关键约束：`stage timeout: 10h` 必配（镜像无进程级超时兜底）
