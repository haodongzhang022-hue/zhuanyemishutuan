# 状态 Schema 最小集 + 扩展机制

> 目标：定义所有秘书遵守的通用最小状态集，并提供行业/领域扩展（ext）机制，对齐 Q4。

## 1. 设计动机

论文要求按领域预定义固定 Schema；本方案需更通用——通用最小集全库遵守，允许行业自定义扩展（金融/游戏/视频/小说/电商等）。

## 2. 通用最小集（所有层级秘书共同遵守）

采用 JSON 单条状态记录，字段分两类：**元数据（meta）** 与 **会话状态（state）**。

```jsonc
{
  // ---- meta：索引 / 追踪层 ----
  "state_id": "uuid",          // 状态唯一 id
  "secretary_level": "L3",     // L0..L3 层级标识
  "domain": "state-secretary", // 所属功能域/模块
  "conversation_id": "...",    // 绑定会话 / Issue / PR
  "parent_id": "...",          // 上级状态 id（用于向上提交链）
  "created_at": "ts",
  "updated_at": "ts",

  // ---- state：最小会话状态集 ----
  "task_id": "t-001",          // 当前任务/小目标标识（对齐 Q2 todolist）
  "progress": 0.5,             // 进度 [0,1] 或完成枚举
  "status": "in_progress",     // todo | in_progress | done | blocked
  "decisions": [               // 关键决策（折叠要点，非全文）
    { "id": "d-1", "summary": "采信 Q1 四层秘书定义", "ts": "..." }
  ],
  "open_items": [              // 开放项 / 阻塞点
    { "id": "o-1", "summary": "待架构定义升降阈值", "type": "blocked" }
  ],
  "context_ids": []            // 本状态引用的上下文折叠索引（见 fold.md）

  // ---- ext：领域扩展区（可选）----
}
```

字段命名对齐 Q4 澄清建议：`task_id` / `progress` / `decisions` / `open_items` 等作为通用最小集。

## 3. 最小集字段约束（US-3 依赖）

- 向上提交时只携带 `state` 中的**汇总字段**（progress/decisions/open_items）+ `context_ids` 索引，不携带原始全文。
- 保证任一上级拿到即可做宏观判断与调度，无需感知下级细节。

## 4. 扩展机制（ext）

- 在通用集之后追加 `ext` 对象，命名空间按行业划分，**互不冲突**：
```jsonc
"ext": {
  "financial": { /* 金融行业扩展字段 */ },
  "gaming":     { /* 游戏行业扩展字段 */ },
  "secretary":  { /* 秘书/协作域扩展字段 */ }
}
```
- 扩展规则：
  - 通用演化：跨域共识的新字段先放通用最小集候选，评审后固化进通用层。
  - 领域固化：某行业频繁使用的字段，沉淀到该行业领域 schema 或角色/领域词。
  - 持续演进：低共识字段保留在 `ext` 区演进，不污染通用集。

## 5. 确定性

- 每个状态记录以 `state_id` 唯一标识，以 `task_id` 关联任务，避免同名任务歧义。
- 当前以「每会话独立秘书」规避多 agent 并发写同一状态的冲突（论文标注限制，Out of Scope）。
