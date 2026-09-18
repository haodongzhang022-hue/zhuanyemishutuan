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
| 循环熔断 | `LoopGuard` — 轮次预算 + 非必要修复最多 1 次 + 停滞感知 + 静默交接不唤醒 | loop-guard.md |
| 评审写回仲裁 | `ReviewArbiter` — 只在开启评审时记版本 + 防同写锁 + 延迟重试 + 单冲突收口 | review-arbiter.md |
| 审查范围分级 | `decide_scan` / `ScanContext` — 日常只审变化，全量只在大版本 | review-arbiter.md §6 |
| 锁聚合台账 | `LockRegistry` — 所有锁统一登记 + 延迟后检测 + 先做其他并挂锁 + 绝不遗留 | lock-registry.md |

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
summary = sec.summarize_for_upstream() # 向上级提交汇总态（含循环预算快照）

# 循环熔断：轮次预算 + 非必要修复最多 1 次 + 静默交接不唤醒
from secretary.loop import LoopAction
d = sec.review_round(blocking=0, advisory=2)   # 仅 🟡 → fix_last（仅此一次唤醒）
d = sec.review_round(blocking=0, advisory=1)   # 再报 🟡 → fuse（不唤醒）
if d.action == LoopAction.FUSE.value:
    sec.silent_handoff("秘书·评审", "秘书·开发", "循环熔断交接")  # wake=False
sec.resume_loop(by="人工", reason="确认收口")    # 恢复预算须人工留痕

# 评审写回：只在开启评审时记版本 + 冲突只额外处理一个
sec.begin_review()                            # ① 开启评审才登记基线
dec = sec.apply_review(mutate=lambda c: None) # ② 先落盘（冲突不阻塞评审）
# 锁聚合：所有锁（写租约/评审读锁/延迟锁）统一登记，触发即可查看
view = sec.locks_overview()                   # where_to_avoid = 哪里要避开
sec.deferred_review_status()                  # 当前"先做其他，并挂锁"项
sec.sweep_locks(by="秘书·会话")                # 兜底回收遗留锁（不误杀活跃锁）
sec.release_review_locks(by="秘书·评审")        # 评审结束放锁（别遗留）

if dec.should_retry:
    dec = sec.retry_review()                  # ③ 正在被编辑 → 延迟后重试
for conf in sec.pending_review_conflicts():   # ④ 待处理冲突至多一张
    sec.resolve_review_conflict(conf.id, "保留双方，新基线继续", by="人工")

# 审查范围：日常只审变化，全量只在大版本
assert sec.review_scope().mode == "scan"
sec.review_scope_commands(tags=["major-version"])   # 大版本才允许全量

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
│       ├── session/             # 会话门面层（L3 秘书统一入口）
│       │   ├── __init__.py
│       │   └── session.py       # SecretarySession（推荐使用）
│       └── loop/                # 循环熔断守卫（Issue #14）
│           ├── __init__.py
│           └── loop_guard.py    # LoopGuard
│       └── review/             # 评审版本仲裁 + 审查范围分级（Issue #14 补全机制）
│           ├── review_arbiter.py  # ReviewArbiter
│           ├── scope.py           # decide_scan / scan_commands
│           └── rules.py           # 同侧/过渡态判定规则
├── tests/                       # 445 项测试（门面 148 + 独立探针 65 + 验收矩阵 13 + 循环熔断 62 + 评审仲裁 75，含评审回归）
├── poc/                         # POC 脚本 + benchmark
│   ├── m1_router.sh             # 单单元路由 POC
│   ├── m2_router_composite.sh   # 复合路由 POC
│   ├── m3_multiunit_router.sh   # 多单元路由 POC
│   ├── state_m9_benchmark.py    # token/命中量化验证
│   └── ...
└── data/                        # 运行期数据（JSON 落盘）
```
