# 产品规格：状态管理秘书机制（通用层 vs 演进 vs 固化）

> module: zhuanyemishutuan · docs/product
> 对照 dongjiagoushi 既有秘书协作机制，判定：哪些进通用层、哪些持续演进、哪些固化到角色/领域词；并给出最小 Schema 草案。
> Ref: #1

## 1. 目的

架构·产品 承接 ISSUE-001 交付物「状态 Schema 最小集草案 + 通用层 vs 持续演进 vs 固化归属判定」，供开发阶段对齐实现。

## 2. 通用层 vs 持续演进 vs 固化 归属判定

| 机制/要素 | 判定 | 依据 |
|---|---|---|
| 四层秘书组织（L0-L3） | **通用层** | 全行业会话编排通用骨架 |
| 层级向上提交汇总协议 | **通用层** | 隔离噪声、跨域通用 |
| 状态最小 Schema（meta+state） | **通用层** | 所有秘书遵守的最小集 |
| context_id 编号 + 折叠 + 检索 | **通用层** | 全库通用索引机制 |
| 稳定前缀 + 缓存保障 | **通用层** | 成本与命中全场景受益 |
| 丢弃时机规则 | **通用层** | 防常驻 agent 无限增长，通用 |
| 行业扩展字段 | **持续演进/固化** | 各行业在 ext 区演进，成熟后固化 |
| 折叠触发阈值（何时归档） | **持续演进** | 依实测调优，初期走自动化默认 |
| 接力卡/双版本/入口强制（dongjiagoushi） | **不纳入** | 属架构师协作既有机制，由该仓库维护；仅借鉴可进通用层的部分 |

## 3. 最小 Schema 草案（产品视角）

产品建议的对外契约字段（对齐 schema.md §2 实现）：

```
状态记录:
  state_id / secretary_level / domain / conversation_id / parent_id
  task_id / progress(纯数值[0,1]) / status(todo|in_progress|done|blocked|stopped)
  decisions[] / open_items[] / context_ids[]
  ext: { 行业命名空间 }
```

- 字段语义稳定、命名直白，任何层级秘书可无歧义读写。
- `ext` 区命名空间隔离，避免行业互踩。

## 4. 验收对接

- **US-1**：自动分配独立 L3 秘书 → org L3 + 事件接线。
- **US-2**：折叠编号检索 → fold。
- **US-3**：层级向上汇总提交 → org + schema 最小集。
- **US-4**：稳定前缀缓存命中 → cache。
- 论文三层改进对照表见 design/state-secretary-index.md §2。

## 5. 本期范围

- 本期交付**设计文档 + 骨架 + POC**，做通用层纯抽象，不落地具体行业字段（Out of Scope）。
- 不处理多 agent 并发写冲突（Out of Scope，以每会话独立秘书规避）。
