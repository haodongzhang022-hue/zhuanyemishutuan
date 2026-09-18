#!/usr/bin/env bash
# ============================================================
# 唯一执行入口（**不接收"全量"参数**）
# ============================================================
# 为什么要有这一层：
#   只要有人能"传个参数就让流水线跑全量"，全量门禁就永远只是个建议。
#   故本脚本**没有** `scope` / `mode` 之类的输入：它只会调
#   `ci-incremental-test.sh`（日常唯一跑法 = 增量），
#   全量只能由**大版本流程**在仓库内显式改这个入口来触发（代码化、走 PR、可审）。
#
# 也因此不需要"把不可信输入当命令执行"这类兜底——那正是注入口。
#
# 用法：
#   ./scripts/ci-dispatch.sh          # 只有这一种用法
# ============================================================
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

# 观测（**不作为执行路径**）：把本次拿到的平台参数打印出来，便于排障。
# 注意：这些值**不参与**命令拼接，只做展示；避免"拿输入拼命令"的注入面。
if [ -n "${CNB_TASK_PARAMS:-}" ]; then
  echo "[dispatch] 平台参数（仅展示，不参与执行）：${CNB_TASK_PARAMS}"
fi

# 兜底路径白名单（最小权限）：若本次变更触及规则文件之外的东西，直接拒绝——
# 兜底一旦能被用来"顺手改别的"，就等于把门禁重新放开（见
# `platform.pipeline_change_allowed()` 的同名口径）。
if [ -n "${CNB_PIPELINE_CHANGED_PATHS:-}" ]; then
  echo "[dispatch] 变更路径：${CNB_PIPELINE_CHANGED_PATHS}"
  python3 - "$ROOT" "$CNB_PIPELINE_CHANGED_PATHS" <<'PYEOF'
import os, sys
sys.path.insert(0, os.path.join(sys.argv[1], "state-secretary", "src"))
paths = [p for p in sys.argv[2].replace(",", " ").split() if p]
try:
    from secretary.collab import pipeline_change_allowed
except Exception as exc:                       # 依赖缺失 → 不因检查坏了挡住干活
    print("[dispatch] 无法装载白名单校验（%s）" % exc, file=sys.stderr)
    raise SystemExit(0)
res = pipeline_change_allowed(paths)
print("[dispatch] 白名单校验：%s（%s）" % (res["allowed"], res["reason"]))
if not res["allowed"]:
    raise SystemExit(3)
PYEOF
fi

# 日常唯一跑法：增量（`full` 不在这里的输入面上）
exec "$ROOT/scripts/ci-incremental-test.sh"
