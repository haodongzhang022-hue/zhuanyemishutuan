# 落库模块划分（已落地）

> 目标：ISSUE-001 的落库位置，对齐 Q5（新增模块，纯净可用）。

## 1. 结论（Q5）

- **已新增独立模块** `state-secretary/` 承载状态管理秘书机制。
- 代码源自 `hanghangchuzhuangyuan`（行行出状元）M9 状态管理层 + M10 上下文装配层的**拷贝移植**。
- 本仓库 `zhuanyemishutuan` 定位秘书/协作领域通用层抽象；dongjiagoushi 承载架构师既有协作规则。

## 2. 模块结构（已落地）

```
zhuanyemishutuan/            # 通用层
├── docs/design/             # 设计文档（7 篇 + index）
├── docs/product/            # 产品规格（1 篇）
└── state-secretary/         # 【已落地】状态管理秘书模块
    ├── README.md            # 模块说明 + 入口
    ├── setup.py             # Python 包配置
    ├── src/
    │   └── secretary/       # Python 源码包
    │       ├── state/       # 状态管理层（M9）
    │       │   ├── models.py          # 状态 Schema 数据模型
    │       │   ├── manager.py         # StateManager 卡生命周期
    │       │   ├── fold_engine.py     # FoldEngine 折叠/编号/检索
    │       │   ├── content_archive.py # ContentArchive 原文归档
    │       │   ├── hierarchy.py       # SecretaryTree + HierarchySubmitter
    │       │   ├── ownership.py       # OwnershipGuard 并发归属
    │       │   ├── arbiter.py         # ConflictArbiter 字段级仲裁
    │       │   └── persist.py         # FlushQueue 批量落盘
    │       ├── context/     # 上下文装配层（M10）
    │       │   ├── assembler.py       # 三层物理隔离装配器
    │       │   ├── cache_guard.py     # 缓存守卫
    │       │   ├── rules.py           # 规则引擎
    │       │   └── slib_guard.py      # SLIB 装配守卫
    │       ├── handoff/     # 交接总线（M5）
    │       │   ├── models.py          # HandoffCard
    │       │   └── handoff.py         # HandoffBus
    │       └── session/     # 会话门面层（L3 秘书统一入口）
    │           ├── __init__.py
    │           └── session.py         # SecretarySession
    │           └── review/          # 评审写回仲裁 + 审查范围分级（Issue #14 补全机制）
    ├── tests/               # 445 项 pytest 测试（全部通过；含 Issue #14 循环熔断 62 + 评审仲裁 75）
    └── poc/                 # POC 脚本 + benchmark
        ├── m1_router.sh             # 单单元路由
        ├── m2_router_composite.sh   # 复合路由
        ├── m3_multiunit_router.sh   # 多单元路由
        ├── state_m9_benchmark.py    # token/命中量化验证
        └── industry-register.yaml   # 路由注册表
```

## 3. 代码映射（设计文档 ↔ 实现）

| 设计文档 | 实现代码 | 说明 |
|---------|---------|------|
| state-secretary-schema.md | `state/models.py` | StateCard/TaskRecord/状态机 |
| state-secretary-fold.md | `state/fold_engine.py` + `content_archive.py` | 折叠 + 原文归档可回查 |
| state-secretary-org.md | `state/hierarchy.py` | SecretaryTree + 向上提交 |
| state-secretary-cache.md | `context/assembler.py` + `cache_guard.py` | 三层装配 + 稳定前缀 |
| state-secretary-poc.md | `poc/state_m9_benchmark.py` | 已可运行的量化对比 |
| org.md + schema.md + cache.md | `session/session.py` | SecretarySession 会话门面统一入口 |
| loop-guard.md（Issue #14） | `loop/loop_guard.py` | 轮次预算熔断 + 非必要修复上限 + 静默交接不唤醒 |
| review-arbiter.md（Issue #14） | `review/review_arbiter.py` + `review/scope.py` | 只在开启评审时记版本 + 防同写锁 + 延迟重试 + 单冲突收口；日常只审变化，全量只在大版本 |

## 4. 落地状态

1. ✅ schema/fold/org 最小可运行代码 + 测试（445 项通过，门面 148 项 + 独立探针 65 项 + 验收矩阵 13 项
   + Issue #14 循环熔断 62 项 + Issue #14 评审仲裁/审查范围 75 项（数据生命周期/评审回归））
1. ✅ SecretarySession 会话门面：L3 会话秘书统一入口（state+fold+context+handoff 整合）
2. ✅ cache 稳定前缀策略（context/assembler.py 三层装配）
3. ✅ poc/benchmark 可运行
4. ✅ 独立可部署，不依赖现有模块内部实现
5. ✅ 仅依赖 Python 标准库，无第三方包

## 5. 来源说明

- 代码源自 `hanghangchuzhuangyuan`（dongdongxiaozu/hanghangchuzhuangyuan）M9 + M10。
- 采用「复制而非迁移」原则，原仓不受影响，两端可并行演进。
- 包名 `hanghang.*` → `secretary.*` 适配，去除对原仓路由引擎的耦合。
