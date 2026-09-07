# M1/M2 最小POC (R4) · 技术栈验证

> 本 POC 拷贝自 `dongdongxiaozu/hanghangchuzhuangyuan` poc/ 目录，
> 用于验证秘书机制的意图路由基础能力。

## 运行

```bash
cd poc
./m1_router.sh industry-register.yaml "<一句话意图>"
```

## R5 复合路由 POC
- 见 `README-POC-R5.md` · 运行 `./m2_router_composite.sh industry-register.yaml "<复合意图>"`
- 补复合缺口: 多槽位二次扫描 + 会话编排判定

## R6 多行业单元路由 POC
- 见 `README-POC-R6.md` · 运行 `./m3_multiunit_router.sh unit-index.yaml "<意图>"`
- 意图→单元路由（不触碰源仓）
