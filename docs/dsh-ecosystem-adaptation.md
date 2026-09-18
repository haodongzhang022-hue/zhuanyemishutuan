# 新版 dsh 生态适配说明

> module: zhuanyemishutuan · docs
> 关联：Issue #16「升级适配」
> 版本：v0.3（2026-09-18）

## 1. 背景

秘书团早期（v0.2）使用 CNB 平台**内置 `npc:go` 执行器**，角色路由依赖评论文本中
内嵌的 `[触发:<角色>]` 标记，由 `.cnb/settings.yml` 的 `npc.roles[].prompt` 驱动。

CNB 平台已上线新版 dsh 生态 —— [`cnpc/dsh`](https://cnb.cool/cnpc/dsh)：
基于 [deepseek-harness（dsh）](https://deepseek-harness.github.io/deepseek-harness/)
官方 CLI headless 模式封装的 CNB NPC 运行镜像。相较内置执行器，新版具备：

- **平台原生角色路由**：评论 `@<仓库路径>(<角色名>)` 按**同名顶层 key** 路由，无需文本标记命中
- **完整 agent 能力**：读写文件、执行命令、提交 PR、加载 skills、MCP 工具
- **多模型可切**：经 `DSH_MODEL` 按角色指定模型（`deepseek-v4-flash` / `glm-5.3` / `kimi-k3` 等）
- **过程日志透传**：构建期补丁把 agent 执行过程实时写流水线日志
- **vision 识图**：纯文本主模型经 `describe_image` 副模型识图
- **项目级指令自动加载**：仓库根 `AGENTS.md` / `CLAUDE.md` 由 dsh 自动加载

## 2. 适配内容

| 项 | v0.2（旧） | v0.3（新） |
| --- | --- | --- |
| 执行器 | 平台内置 `type: npc:go` | 镜像 `docker.cnb.cool/xqitw/docker/npc-dsh:latest` |
| 入口 | 平台接管 | `sh /app/start.sh` |
| 角色路由 | 评论文本 `[触发:<角色>]` 命中 | 顶层 key = 角色名（平台原生） |
| 事件绑定 | `.cnb.yml` 的 `$` 段直接挂 `*npc_task` | `.cnb/npc.yml` 内每角色挂 `issue.comment@npc` / `pull_request.comment@npc` |
| 模型 | 平台默认 | `DSH_MODEL` 显式注入 |
| 时间兜底 | 平台默认 | `stage timeout: 10h`（**必配**，见 §4） |
| 项目级指令 | 无 | 新增根目录 `AGENTS.md`（dsh 自动加载） |
| 角色定义 | `.cnb/settings.yml` | 不变（7 个秘书角色保留，仅路由方式升级） |

### 文件变更

```
.cnb.yml                  → 精简为 include + push + vscode，NPC 事件移入 npc.yml
.cnb/npc.yml              → 新增：镜像/资源/环境锚点 + 7 角色顶层 key 路由
.cnb/settings.yml         → 不变（角色 prompt 继续作为 CNB_NPC_PROMPT 为空时的回落源）
AGENTS.md                 → 新增：项目级指令（仓库定位/角色体系/门禁规则/开发约定）
docs/dsh-ecosystem-adaptation.md → 本文件
```

## 3. 角色路由机制

`.cnb/npc.yml` 顶层以角色名为 key（与 `.cnb/settings.yml` 的 `npc.roles[].name` **精确一致**）：

```yaml
"秘书·评审":
  issue.comment@npc:
    - <<: *npc
      stages:
        - name: npc go
          <<: *base-stage
          env:
            <<: *npc-env
            DSH_MODEL: deepseek-v4-flash
```

触发：评论中 `@dongdongxiaozu/zhuanyemishutuan(秘书·评审)` → 平台按 key 路由到该 pipeline。
未带角色名时由 `$` 段兜底走默认角色。`$` 段显式注入 `CNB_NPC_NAME: 秘书·接待`
与 `npc.defaultRole` 对齐——`@npc` 事件由平台注入 `CNB_NPC_PROMPT`，但兜底路径需
自报角色名，镜像才能从 `.cnb/settings.yml` 回落到对应 prompt。

7 个角色：`秘书·接待` / `秘书·秘书长` / `秘书·状态管理` / `秘书·会话` / `秘书·开发` / `秘书·评审` / `秘书·测试`。

## 4. 关键约束（踩坑记录）

1. **必须配 `stage timeout`**：镜像已删除业务层进程级超时，时间兜底完全依赖 stage `timeout`。
   CNB Job 未声明 timeout 默认仅 2h，漏配则 dsh 挂死无任何墙钟防线。本仓库设 `10h`。
2. **角色名须与 settings.yml 完全一致**：`@<路径>(<role-name>)` 的 role-name 必须等于
   `npc.roles[].name`，否则回落默认角色。
3. **非 `@npc` 事件不注入提示词**：`pull_request` 等普通事件平台不注入 `CNB_NPC_PROMPT` /
   `CNB_NPC_TRIGGER_CONTENT`，需手动补齐。
4. **顶层 key 含非 ASCII 需加引号**：角色名含 `·`，YAML 中以 `"秘书·评审"` 双引号包裹。
5. **`DSH_TOOLS_MODE` 旧值 `code` 已废弃**：dsh 0.1.2-rc.1 起合法值为 `native`/`ptc`/`both`。
   本仓库不覆盖该变量，走镜像默认 `ptc`。

## 5. 验证

- 配置校验：`python3 -c "import yaml; yaml.safe_load(open('.cnb/npc.yml'))"` — 通过
- 角色对齐：`npc.yml` 顶层 key 集合 == `settings.yml` 角色名集合（7/7，见 §3）
- 代码回归：`state-secretary` 全量 pytest **308 passed**（适配未触及模块代码）
- 平台实测：Issue 评论 `@dongdongxiaozu/zhuanyemishutuan(秘书·接待)` 触发 npc-dsh pipeline
