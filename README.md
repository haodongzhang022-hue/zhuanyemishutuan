# 专业秘书团（zhuanyemishutuan）

秘书团项目：团队协作、任务接力与知识沉淀的协作项目。

## 仓库信息
- 代码托管：腾讯云 CNB（https://cnb.cool/dongdongxiaozu/zhuanyemishutuan）
- 协作模式：本地优先 + 云端协同（云原生开发 + 云原生构建）
- 默认分支：`main`

## 目录规划
- `docs/` —— 项目文档与规范
- `.cnb.yml` —— CNB 流水线与云原生开发环境声明（Everything as code）
- `issues/` —— 任务卡片与接力记录（待建设）
- `state-secretary/` —— 状态管理秘书机制（`src/secretary/` + `tests/`）
- `shared/collab/` —— **agent 协同规则（全局 · 跨仓库）**：组织级基线 + 仓库级数值覆写
- `scripts/` —— 可执行入口（增量测试 / 组织级分发）

## 协同规则（组织级 · 各仓库通用）

`agent ↔ agent 的协同礼仪`与具体业务无关，故**一处定义、全域生效**：

| 层级 | 载体 | 权限 |
|---|---|---|
| 组织级（全组织一份） | `shared/collab/organization.yaml` | 定义**基线与语义** |
| 仓库级 | `shared/collab/collab-rules.yaml` | **只覆写数值**，语义不可改 |
| 内置默认 | 代码兜底 | 不抛、不静默乱读 |

覆盖：等待阶梯（10s→30s→60s→3min→6min→10min 到顶即停）、锁窗口与聚合台账、
循环熔断预算、审查范围（日常只审变化）、探测预算（最多 6 次）、平台计费口径。

**分发到组织下各仓库**（默认 dry-run，不会误写别人仓库）：

```bash
scripts/distribute-collab-rules.sh --group dongdongxiaozu          # 先看计划
scripts/distribute-collab-rules.sh --group dongdongxiaozu --apply  # 真正写入（每仓建分支）
```

口径全文见 `docs/secretary-collab-rules.md`；平台语义对齐与官方最佳实践对照见
`docs/design/state-secretary-platform-alignment.md`。

## 快速开始
（待补充：秘书团章程、任务接力协议、门禁规范）
