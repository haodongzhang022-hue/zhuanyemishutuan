# state-secretary · 状态管理秘书模块

对应 **ISSUE-001** 状态管理秘书机制（层级状态管理 + 上下文折叠编号检索 + 缓存命中保障）。

> 本模块代码源自 `dongdongxiaozu/hanghangchuzhuangyuan`（行行出状元）M9 状态管理层
> + M10 上下文装配层的**拷贝移植**（复制而非迁移，原仓不受影响）。
> 独立为本仓库的通用层模块，供秘书团体系使用。

## 模块定位

- **本模块** = 状态管理秘书机制的**实际可运行实现**
- **docs/design/ 文档** = 该实现的设计依据（见 `docs/`）
- **dongjiagoushi** = 秘书角色的行为/交互层（入口、路由、澄清）
- 两者功能互补，不重复

## 核心能力

| 能力 | 实现 | 对应设计文档 |
|------|------|------------|
| 状态卡生命周期 | `StateManager` — 每会话一张 StateCard | schema.md |
| 折叠/编号/检索 | `FoldEngine` — 默认恒折叠 + 编号索引 | fold.md |
| 原文归档 | `ContentArchive` — 折叠后原文可精确找回 | fold.md §3.3 |
| 层级向上提交 | `HierarchySubmitter` + `SecretaryTree` — L0-L3 树 | org.md |
| 并发安全 | `OwnershipGuard` + `ConflictArbiter` — 归属/写锁/仲裁 | schema.md |
| 批量落盘 | `FlushQueue` / `DeferredWriter` | 架构保障 |
| 上下文装配 | `ContextAssembler` — 三层物理隔离 + 稳定前缀 | cache.md |
| POC benchmark | `poc/state_m9_benchmark.py` — token/命中 A/B | poc.md |

## 快速开始

```bash
# 安装
cd state-secretary
pip install -e .

# 运行 POC benchmark
python3 poc/state_m9_benchmark.py --steps 20

# 运行测试
cd state-secretary
python3 -m pytest tests/ -v
```

## Python API 示例

```python
from secretary.state import StateManager, FoldEngine, TaskState

# 创建一张状态卡
mgr = StateManager(data_dir="/tmp/state-data")
card = mgr.create_card(
    session_id="my-session",
    objective="实现状态管理秘书机制",
    unit_id="zhuanyemishutuan",
    role_name="secretary",
)

# 添加任务
task = mgr.add_task(
    session_id="my-session",
    title="设计数据模型",
    task_type="deliverable",  # 交付任务恒展开
)

# 折叠并生成压缩上下文
engine = FoldEngine("my-session")
card = mgr.apply_fold_policy("my-session")
compact = engine.compact_context(card)
print(compact)  # 只含折叠引用 + 展开任务要素
```

## 目录结构

```
state-secretary/
├── README.md                    # 模块说明
├── src/
│   └── secretary/               # Python 源码包
│       ├── __init__.py
│       ├── state/               # 状态管理层（M9）
│       │   ├── models.py        # 数据模型 + Schema
│       │   ├── manager.py       # StateManager
│       │   ├── fold_engine.py   # FoldEngine
│       │   ├── content_archive.py  # 原文归档
│       │   ├── hierarchy.py     # SecretaryTree / HierarchySubmitter
│       │   ├── ownership.py     # OwnershipGuard
│       │   ├── arbiter.py       # ConflictArbiter
│       │   └── persist.py       # FlushQueue
│       ├── context/             # 上下文装配层（M10）
│       │   ├── assembler.py     # 三层上下文装配
│       │   ├── cache_guard.py   # 缓存守卫
│       │   ├── rules.py         # 规则引擎
│       │   └── slib_guard.py    # SLIB 装配守卫
│       └── handoff/             # 交接总线（M5）
│           ├── models.py        # HandoffCard
│           └── handoff.py       # HandoffBus
├── tests/                       # 74 项测试
├── poc/                         # POC 脚本 + benchmark
│   ├── m1_router.sh             # 单单元路由 POC
│   ├── m2_router_composite.sh   # 复合路由 POC
│   ├── m3_multiunit_router.sh   # 多单元路由 POC
│   ├── state_m9_benchmark.py    # token/命中量化验证
│   └── ...
└── data/                        # 运行期数据（JSON 落盘）
```
