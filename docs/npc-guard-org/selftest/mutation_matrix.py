#!/usr/bin/env python3
"""变异判别力矩阵：每个变异跑一次门禁，记录 rc 与 FAIL 条数。

判据：变异前 rc=0（基线全绿），变异后 rc≠0 且 FAIL>0（拦住）。

覆盖四类判据（缺一类就是"该维度无判别力证据"）：

* 值断言（M1/M2/M3/M10/M17）
* 值来源断言（M4/M5/M6/M11/M20/M21）
* 通道分派 + 路线分派（M7/M8/M9/M18/M19）
* 鉴权（M13–M16）与重复 stage（M12）

用法：`python3 selftest/mutation_matrix.py`（退出码 0 = 0 漏网且基线全绿）。
"""
import os
import re
import subprocess
import sys

#: 本自测脚本位于 <bundle>/selftest/，其余资产在上一级（共享仓布局）
BUNDLE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CHECK = os.path.join(BUNDLE, "checks/check_npc_cost_guard.py")
RULES = os.path.join(BUNDLE, "rules/npc-cost-guard.yml")

MIRROR = os.path.join(BUNDLE, "samples/repo-mirror/.cnb/npc.yml")
NPCGO = os.path.join(BUNDLE, "samples/repo-npcgo/.cnb.yml")
INC_MIN = os.path.join(BUNDLE, "samples/repo-include-min/.cnb.yml")
INC_OVR = os.path.join(BUNDLE, "samples/repo-include-override/.cnb.yml")
INC_DUP = os.path.join(BUNDLE, "samples/repo-include-dup/.cnb.yml")
LOCAL_BASE = os.path.join(BUNDLE, "samples/repo-local-baseline/.cnb/npc.yml")
FLOW_CPUS = os.path.join(BUNDLE, "samples/repo-flow-cpus/.cnb.yml")
MISC_INC = os.path.join(BUNDLE, "samples/repo-misc-include/.cnb.yml")
MIRROR_BASE = os.path.join(BUNDLE, "baseline/npc-guard-env.yml")
NPCGO_BASE = os.path.join(BUNDLE, "baseline/npc-go-options.yml")
TEMPLATE = os.path.join(BUNDLE, "templates/npc-guard-job.yml")

#: 反例探针（本身就该 rc≠0，用于验证"能拦住"，不计入"基线必须全绿"）
EXPECT_RED = {
    "M12 引用方与共享模板重复写 stages",
    "M20b 仓内自建基线样例（自带红）",
    "M21b flow-style 两角色各写 cpus（自带红）",
}

#: (名称, 探针配置, 变异目标文件, [(旧片段, 新片段), ...])
CASES = [
    # ── 值断言 ───────────────────────────────────────────────────────
    ("M1 cpus 2 -> 8", MIRROR, MIRROR, [("cpus: 2", "cpus: 8")]),
    ("M2 timeout 2h -> 10h", MIRROR, MIRROR, [("timeout: 2h", "timeout: 10h")]),
    ("M3 基线模型名去点", MIRROR, MIRROR_BASE,
     [("deepseek-v4.1-flash", "deepseek-v4-flash")]),
    ("M10 基线回合上限放宽 72 -> 200", MIRROR, MIRROR_BASE,
     [('DSH_MAX_STEPS: "72"', 'DSH_MAX_STEPS: "200"')]),
    # 🔴-1 定点：路线 B 的模板值三处同改坏（v1.1 全绿 → v1.2 须拦）
    ("M17 模板值三处同改坏（cpus/回合/模型）", INC_MIN, TEMPLATE,
     [("cpus: 2", "cpus: 8"), ('DSH_MAX_STEPS: "72"', 'DSH_MAX_STEPS: "500"'),
      ("deepseek-v4.1-flash", "deepseek-v4-flash")]),
    # ── 值来源断言 ───────────────────────────────────────────────────
    ("M4 基线去掉 DSH_MAX_STEPS", MIRROR, MIRROR_BASE, [('DSH_MAX_STEPS: "72"\n', "")]),
    ("M5 基线去掉 DSH_MODEL", MIRROR, MIRROR_BASE, [('DSH_MODEL: deepseek-v4.1-flash\n', "")]),
    ("M6 本地再写一处 cpus", MIRROR, MIRROR,
     [("  runner:\n    cpus: 2", "  runner:\n    cpus: 2\n  extra:\n    cpus: 2")]),
    ("M11 基线引用指向不存在的文件", MIRROR, MIRROR,
     [("../../baseline/npc-guard-env.yml", "../../baseline/missing-baseline.yml")]),
    # 🔴-3 定点：基线来源不是共享仓（仓内自建）
    ("M20 基线指向仓内自建文件（非共享仓）", MIRROR, MIRROR,
     [("../../baseline/npc-guard-env.yml",
       "../../samples/repo-local-baseline/local-baseline.yml")]),
    ("M20b 仓内自建基线样例（自带红）", LOCAL_BASE, LOCAL_BASE, []),
    # 🟡-1 定点：flow-style 计数绕过
    ("M21 flow-style 两角色各写 cpus", MIRROR, MIRROR,
     [("    - <<: *npc-job-base\n      imports:",
       "    - <<: *npc-job-base\n      runner: {cpus: 2}\n      imports:"),
      ("    - <<: *npc-job-base\n      imports:",
       "    - <<: *npc-job-base\n      runner: {cpus: 2}\n      imports:")]),
    ("M21b flow-style 两角色各写 cpus（自带红）", FLOW_CPUS, FLOW_CPUS, []),
    # ── 通道分派 / 路线分派 ───────────────────────────────────────────
    ("M7 镜像路线混用 options.maxTurns", MIRROR, MIRROR,
     [("        - name: npc go\n          <<: *base-stage",
       "        - name: npc go\n          <<: *base-stage\n          options:\n"
       "            maxTurns: 72")]),
    ("M8 npc:go 基线去 maxTurns", NPCGO, NPCGO_BASE, [("maxTurns: 72\n", "")]),
    ("M9 npc:go 路线混用 DSH_MAX_STEPS", NPCGO, NPCGO,
     [("          timeout: 2h\n          optionsFrom:",
       "          timeout: 2h\n          env:\n            DSH_MAX_STEPS: \"72\"\n"
       "          optionsFrom:")]),
    # 🔴-2 定点：通道错配（走错通道 = 静默失效）
    ("M18 npc:go 用 imports 指向 options 基线", NPCGO, NPCGO,
     [("optionsFrom:", "imports:")]),
    ("M19 镜像路线用 optionsFrom 指向 env 基线", MIRROR, MIRROR,
     [("      imports:", "      optionsFrom:")]),
    # ── 重复 stage / 鉴权 ────────────────────────────────────────────
    ("M12 引用方与共享模板重复写 stages", INC_DUP, INC_DUP, []),
    ("M13 共享模板漏声明 allow_events", INC_OVR, TEMPLATE,
     [('allow_events:\n  - issue.comment@npc\n  - pull_request.comment@npc\n'
       '  - pull_request\n  - push\n', "")]),
    ("M14 共享模板漏声明 allow_slugs", INC_OVR, TEMPLATE,
     [('allow_slugs:\n  - "dongdongxiaozu/**"\n', "")]),
    ("M15 allow_events 漏 pull_request", INC_OVR, TEMPLATE,
     [('  - pull_request\n  - push\n', '  - push\n')]),
    ("M16 基线文件缺鉴权声明", MIRROR, MIRROR_BASE,
     [('allow_slugs:\n  - "dongdongxiaozu/**"\nallow_events:\n'
       '  - issue.comment@npc\n  - pull_request.comment@npc\n'
       '  - pull_request\n  - push\n', "")]),
]

#: 样例矩阵：期望 rc（正确样例 0 / 反例 1），逐个实跑核对
SAMPLES = [
    (MIRROR, 0), (NPCGO, 0), (INC_MIN, 0), (INC_OVR, 0), (MISC_INC, 0),
    (INC_DUP, 1), (LOCAL_BASE, 1), (FLOW_CPUS, 1),
]


def run(config):
    p = subprocess.run([sys.executable, CHECK, "--config", config, "--rules", RULES],
                       capture_output=True, text=True)
    return p.returncode, len(re.findall(r"^\[FAIL\]", p.stdout, re.M))


def main():
    bad = 0
    print("== 样例矩阵（期望 rc 逐个核对）")
    for path, want in SAMPLES:
        rc, fails = run(path)
        ok = rc == want
        bad += 0 if ok else 1
        print(f"  {os.path.relpath(path, BUNDLE):<48} rc={rc} (期望 {want}) "
              f"FAIL={fails}  {'✓' if ok else '✗'}")
    print("\n== 变异矩阵")
    print(f"{'变异':<40}{'变异前':>8}{'变异后':>8}{'FAIL':>6}  判定")
    print("-" * 78)
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
        print(f"{name:<40}{before_rc:>8}{rc:>8}{fails:>6}  {'✓ 拦住' if ok else '✗ 漏网'}")
    print("-" * 78)
    print(f"样例 {len(SAMPLES)} 个：不符期望 {bad}")
    print(f"变异 {len(CASES)} 个：漏网 {missed}，基线非绿 {dirty}")
    return 1 if (missed or dirty or bad) else 0


if __name__ == "__main__":
    sys.exit(main())
