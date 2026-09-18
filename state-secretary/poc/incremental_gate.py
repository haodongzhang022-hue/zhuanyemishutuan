#!/usr/bin/env python3
"""增量跑测门禁 CLI（Issue #14）。

用法（仓库根执行，或任意目录传 --repo）::

    # 1) 看本轮要跑什么（过程可视化）
    python3 state-secretary/poc/incremental_gate.py plan --base a455608

    # 2) 按计划实跑（只跑增量，精确到 nodeid）
    python3 state-secretary/poc/incremental_gate.py run --base a455608

    # 3) 全量告警视图：把「此前已跑过」标出来（防"全量 === 本轮实跑"歧义）
    python3 state-secretary/poc/incremental_gate.py full --base a455608

    # 4) 校验一段留痕文字的计数口径（权责一致）
    python3 state-secretary/poc/incremental_gate.py counter \
        --text "全量 308 项全部通过" --executed 12 --total 308 --deferred 296

退出码：0=通过 / 1=失败（测试红 或 计数口径违规）。
"""
from __future__ import annotations

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                "..", "src"))

from secretary.gate import (  # noqa: E402
    CounterWordingError, IncrementalGate, assert_counter,
)


def cmd_plan(args) -> int:
    gate = IncrementalGate(repo_root=args.repo)
    plan = gate.plan_sweep(base=args.base, head=args.head,
                           include_redundant=args.include_redundant)
    if args.json:
        print(json.dumps(plan.to_dict(), ensure_ascii=False, indent=2))
    else:
        print(plan.render())
    return 0


def cmd_full(args) -> int:
    gate = IncrementalGate(repo_root=args.repo)
    executed = args.executed.split(",") if args.executed else []
    view = gate.full_sweep_view(base=args.base, head=args.head,
                                executed_now=executed)
    if args.json:
        print(json.dumps(view.to_dict(), ensure_ascii=False, indent=2)[:20000])
    else:
        print(view.render())
    try:
        gate.assert_full_view(view)
    except Exception as exc:  # noqa: BLE001
        print(f"[门禁拒绝] {exc}", file=sys.stderr)
        return 1
    return 0


def cmd_run(args) -> int:
    gate = IncrementalGate(repo_root=args.repo)
    plan = gate.plan_sweep(base=args.base, head=args.head,
                           include_redundant=args.include_redundant)
    print(plan.render())
    report = gate.run(plan)
    print()
    print(report.render())
    try:
        gate.assert_report(report)
    except CounterWordingError as exc:
        print(f"[计数口径违规] {exc}", file=sys.stderr)
        return 1
    return 0 if report.green else 1


def cmd_counter(args) -> int:
    try:
        n = assert_counter(args.text, args.executed, total_suite=args.total,
                           deferred=args.deferred)
    except CounterWordingError as exc:
        print(f"[计数口径违规] {exc}", file=sys.stderr)
        return 1
    print(f"[计数口径通过] 本轮实跑 {n} 例")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="增量跑测门禁（Issue #14）")
    ap.add_argument("--repo", default=os.getcwd(), help="仓库根（含 .git）")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("plan", help="生成本轮增量跑测计划")
    p.add_argument("--base", required=True, help="基线修订（上次已跑 head）")
    p.add_argument("--head", default="WORKTREE")
    p.add_argument("--include-redundant", action="store_true")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_plan)

    f = sub.add_parser("full", help="全量告警视图（标记已跑过项）")
    f.add_argument("--base", required=True)
    f.add_argument("--head", default="WORKTREE")
    f.add_argument("--executed", default="", help="本轮实跑 nodeid，逗号分隔")
    f.add_argument("--json", action="store_true")
    f.set_defaults(func=cmd_full)

    r = sub.add_parser("run", help="按增量计划实跑")
    r.add_argument("--base", required=True)
    r.add_argument("--head", default="WORKTREE")
    r.add_argument("--include-redundant", action="store_true")
    r.set_defaults(func=cmd_run)

    c = sub.add_parser("counter", help="校验留痕文字的计数口径")
    c.add_argument("--text", required=True)
    c.add_argument("--executed", type=int, required=True)
    c.add_argument("--total", type=int, default=0)
    c.add_argument("--deferred", type=int, default=0)
    c.set_defaults(func=cmd_counter)

    args = ap.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
