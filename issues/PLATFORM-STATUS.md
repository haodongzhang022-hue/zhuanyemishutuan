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
- CNB Issue #14（循环熔断 · 避免评审/修复无限循环）: https://cnb.cool/dongdongxiaozu/zhuanyemishutuan/-/issues/14
  - 落地：`state-secretary/src/secretary/loop/loop_guard.py`（轮次预算 + 非必要修复最多 1 次 + 停滞感知 + 静默交接）
  - 门禁接入点：`SecretarySession.review_round` / `silent_handoff` / `resume_loop`
  - 规范：`docs/secretary-gate-workflow.md` §2.1（v0.3）；角色提示词 `.cnb/settings.yml`（评审/测试/秘书长）
  - 补全机制（v0.4 · §2.2/§2.3）：`review/review_arbiter.py`（只在开启评审时记版本 + 防同写锁 + 延迟重试 + 单冲突收口）
    + `review/scope.py`（**日常只审变化，全量只在大版本**，防无效构建/测试）；
    设计：`docs/design/state-secretary-review-arbiter.md`；门面：`begin_review` / `apply_review` / `retry_review` / `resolve_review_conflict` / `review_scope`
  - 协同规则（v0.5 · §2.2.1）：`collab/backoff.py`（阶梯 10s→30s→60s→3min→6min→10min 到顶即停）
    + `collab/wait_policy.py`（等待交平台挂起，**不占 CPU**）；规则包 `shared/collab/collab-rules.yaml`
  - 检查点探测（v0.6 · §2.2.2）：`collab/probe.py`（**非轮询**：形成检查点 → 判断检查点 →
    状态变化/到点再触发；**最多 6 次**）；平台落点 `cnb:await`（零占用）/ `crontab`（≥5min，平台代跑）/
    `api_trigger`（外部 webhook 再跑）/ 交回人工；`.cnb.yml` 新增 `api_trigger_probe_check` /
    `api_trigger_probe_resume` 再触发入口与 `cnb:await` 等待片；
    门面：`create_checkpoint` / `checkpoint_plan` / `register_platform_wait` / `probe_checkpoint` /
    `dispatch_probe_event` / `checkpoints_overview` / `probe_status`

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
