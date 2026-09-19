#!/usr/bin/env python3
"""变异判别力矩阵：每个变异跑一次门禁，记录 rc 与 FAIL 条数。

判据：变异前 rc=0（基线全绿），变异后 rc≠0 且 FAIL>0（拦住）。
"""
import re
import subprocess
import sys

import os

#: 本自测脚本位于 <bundle>/selftest/，其余资产在上一级（共享仓布局）
BUNDLE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CHECK = os.path.join(BUNDLE, "checks/check_npc_cost_guard.py")
RULES = os.path.join(BUNDLE, "rules/npc-cost-guard.yml")

MIRROR = os.path.join(BUNDLE, "samples/repo-mirror/.cnb/npc.yml")
NPCGO = os.path.join(BUNDLE, "samples/repo-npcgo/.cnb.yml")
INC_OK = os.path.join(BUNDLE, "samples/repo-include-ok/.cnb.yml")
INC_DUP = os.path.join(BUNDLE, "samples/repo-include-dup/.cnb.yml")
MIRROR_BASE = os.path.join(BUNDLE, "baseline/npc-guard-env.yml")
NPCGO_BASE = os.path.join(BUNDLE, "baseline/npc-go-options.yml")
TEMPLATE = os.path.join(BUNDLE, "templates/npc-guard-job.yml")

#: 反例探针（本身就该 rc≠0，用于验证"能拦住"，不计入"基线必须全绿"）
EXPECT_RED = {"M12 引用方与共享模板重复写 stages"}

CASES = [
    ("M1 cpus 2 -> 8", MIRROR, MIRROR, [("cpus: 2", "cpus: 8")]),
    ("M2 timeout 2h -> 10h", MIRROR, MIRROR, [("timeout: 2h", "timeout: 10h")]),
    ("M3 基线模型名去点", MIRROR, MIRROR_BASE,
     [("deepseek-v4.1-flash", "deepseek-v4-flash")]),
    ("M4 基线去掉 DSH_MAX_STEPS", MIRROR, MIRROR_BASE, [('DSH_MAX_STEPS: "72"\n', "")]),
    ("M5 基线去掉 DSH_MODEL", MIRROR, MIRROR_BASE, [('DSH_MODEL: deepseek-v4.1-flash\n', "")]),
    ("M6 本地再写一处 cpus", MIRROR, MIRROR,
     [("  runner:\n    cpus: 2", "  runner:\n    cpus: 2\n  extra:\n    cpus: 2")]),
    ("M7 镜像路线混用 options.maxTurns", MIRROR, MIRROR,
     [("        - name: npc go\n          <<: *base-stage",
       "        - name: npc go\n          <<: *base-stage\n          options:\n"
       "            maxTurns: 72")]),
    ("M8 npc:go 基线去 maxTurns", NPCGO, NPCGO_BASE, [("maxTurns: 72\n", "")]),
    ("M9 npc:go 路线混用 DSH_MAX_STEPS", NPCGO, NPCGO,
     [("          timeout: 2h\n          optionsFrom:",
       "          timeout: 2h\n          env:\n            DSH_MAX_STEPS: \"72\"\n"
       "          optionsFrom:")]),
    ("M10 基线回合上限放宽 72 -> 200", MIRROR, MIRROR_BASE,
     [('DSH_MAX_STEPS: "72"', 'DSH_MAX_STEPS: "200"')]),
    ("M11 基线引用指向不存在的文件", MIRROR, MIRROR,
     [("../../baseline/npc-guard-env.yml", "../../baseline/missing-baseline.yml")]),
    ("M12 引用方与共享模板重复写 stages", INC_DUP, INC_DUP, []),
    ("M13 共享模板漏声明 allow_events", INC_OK, TEMPLATE,
     [('allow_events:\n  - issue.comment@npc\n  - pull_request.comment@npc\n'
       '  - pull_request\n  - push\n', "")]),
    ("M14 共享模板漏声明 allow_slugs", INC_OK, TEMPLATE,
     [('allow_slugs:\n  - "dongdongxiaozu/**"\n', "")]),
    ("M15 allow_events 漏 pull_request", INC_OK, TEMPLATE,
     [('  - pull_request\n  - push\n', '  - push\n')]),
    ("M16 基线文件缺鉴权声明", MIRROR, MIRROR_BASE,
     [('allow_slugs:\n  - "dongdongxiaozu/**"\nallow_events:\n'
       '  - issue.comment@npc\n  - pull_request.comment@npc\n'
       '  - pull_request\n  - push\n', "")]),
]


def run(config):
    p = subprocess.run([sys.executable, CHECK, "--config", config, "--rules", RULES],
                       capture_output=True, text=True)
    return p.returncode, len(re.findall(r"^\[FAIL\]", p.stdout, re.M))


def main():
    print(f"{'变异':<38}{'变异前':>8}{'变异后':>8}{'FAIL':>6}  判定")
    print("-" * 76)
    missed, dirty = 0, 0
    for name, probe, target, subs in CASES:
        before_rc, _ = run(probe)
        if name not in EXPECT_RED:
            dirty += 0 if before_rc == 0 else 1
        orig = open(target, encoding="utf-8").read()
        mutated = orig
        for old, new in subs:
            assert old in mutated, f"{name}: 找不到待变异片段 {old!r}"
            mutated = mutated.replace(old, new, 1)
        if subs:
            open(target, "w", encoding="utf-8").write(mutated)
        try:
            rc, fails = run(probe)
        finally:
            if subs:
                open(target, "w", encoding="utf-8").write(orig)
        ok = rc != 0 and fails > 0
        missed += 0 if ok else 1
        print(f"{name:<38}{before_rc:>8}{rc:>8}{fails:>6}  {'✓ 拦住' if ok else '✗ 漏网'}")
    print("-" * 76)
    print(f"变异 {len(CASES)} 个：漏网 {missed}，基线非绿 {dirty}")
    return 1 if (missed or dirty) else 0


if __name__ == "__main__":
    sys.exit(main())
