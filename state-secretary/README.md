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
| 会话统一门面 | `SecretarySession` — L3 会话秘书完整驱动（状态+折叠+装配+交接） | org.md |
| 增量跑测门禁 | `secretary.gate.IncrementalGate` — 只跑有效增量 / 反全量冗余 / 计数权责一致 | incremental-gate.md |

## 增量跑测门禁（Issue #14）

原则：**只跑增量**（允许漏，后续用时会重新提需求去改），不得大量重复冗余；
**跑多少写多少**，不得用"全量"造成歧义。

```bash
# 本轮要跑什么（过程可视化；只列 diff 触及的用例）
python3 poc/incremental_gate.py plan --base <上次已跑 head>
# 按计划实跑（精确到 nodeid）
python3 poc/incremental_gate.py run  --base <上次已跑 head>
# 全量告警视图：把「已跑过、默认不重复」逐项标出来
python3 poc/incremental_gate.py full --base <上次已跑 head>
```

契约见 `docs/design/state-secretary-incremental-gate.md`。

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
# 底层 API（M9/M10）
from secretary.state import StateManager, FoldEngine, TaskState

mgr = StateManager(data_dir="/tmp/state-data")
card = mgr.create_card(
    session_id="my-session",
    objective="实现状态管理秘书机制",
    unit_id="zhuanyemishutuan",
    role_name="secretary",
)
task = mgr.add_task(session_id="my-session", title="设计数据模型",
                    task_type="deliverable")
engine = FoldEngine("my-session")
card = mgr.apply_fold_policy("my-session")
compact = engine.compact_context(card)

# ── 推荐用法：SecretarySession 会话门面 ─────────────────────────
# 一个 L3 会话秘书 = 一个 SecretarySession 实例
from secretary.session import SecretarySession

sec = SecretarySession("issue-42", objective="实现状态管理秘书机制")
sec.add_task("设计数据模型", task_type="deliverable")   # 交付任务恒展开
sec.fold("需求澄清：Q1 四层秘书定义…", meta={"objective": "澄清"})

ctx = sec.get_compact_context()        # 压缩上下文（驱动下一轮）
nav = sec.get_navigation()             # 任务导航视图
summary = sec.summarize_for_upstream() # 向上级提交汇总态

# 三层上下文装配（缓存键 = layer_a 哈希）
result = sec.assemble_context(user_text="查询进度")
cache_key = result["cache_key"]        # 同一会话稳定不变
# ⚠ 若未配置 static_prefix，所有会话的 layer_a 相同 → 缓存键共享；
#   需要隔离时请在 SecretarySession 传入独立的 static_prefix。
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
│       ├── handoff/             # 交接总线（M5）
│       │   ├── models.py        # HandoffCard
│       │   └── handoff.py       # HandoffBus
│       └── session/             # 会话门面层（L3 秘书统一入口）
│           ├── __init__.py
│           └── session.py       # SecretarySession（推荐使用）
├── tests/                       # 342 项测试（门面 148 + 独立探针 65 + 验收矩阵 13
│                                #   + 增量门禁 34，含评审回归）
├── src/secretary/gate/          # 增量跑测门禁（Issue #14）
│   ├── models.py                # 三态模型 SweepTarget/Plan/Report + 全量告警视图
│   ├── rules.py                 # 增量分类 + 冗余剔除 + 有效覆盖面兜底
│   ├── counter.py               # 计数口径守卫（跑多少写多少）
│   └── incremental.py           # IncrementalGate（git 增量识别 + 采集 + 实跑）
├── poc/                         # POC 脚本 + benchmark
│   ├── incremental_gate.py      # 增量跑测门禁 CLI（plan/run/full/counter）
│   ├── m1_router.sh             # 单单元路由 POC
│   ├── m2_router_composite.sh   # 复合路由 POC
│   ├── m3_multiunit_router.sh   # 多单元路由 POC
│   ├── state_m9_benchmark.py    # token/命中量化验证
│   └── ...
└── data/                        # 运行期数据（JSON 落盘）
```
