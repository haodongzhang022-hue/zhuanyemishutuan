#!/usr/bin/env bash
# ============================================================
# 增量测试入口（日常唯一跑法）——**只跑变更相关用例**
# ============================================================
# 为什么是脚本而不是把命令写死在 .cnb.yml：
#   判定逻辑（同口径/同版本 → 跳过；连跑 → 拒绝）必须被**真正执行**，
#   而不是散在流水线里靠"记得这么写"。脚本是机制的唯一落点。
#
# 口径（Issue #14 追问）：
#   - 日常只跑增量（`scan`）；全量（`full`）只在**大版本**；
#   - 同一口径（范围/版本/受影响面/命令）只跑一次，第 2 次起跳过；
#   - "连跑 N 次同一命令"当稳定性论证 → 拒绝，改走定点/变异探针/独立场景。
#
# 用法：
#   ./scripts/ci-incremental-test.sh                      # 增量（默认）
#   CNB_SCOPE=full ./scripts/ci-incremental-test.sh       # 全量（仅大版本）
#   CNB_WINDOW_REPEATS=3 ./scripts/ci-incremental-test.sh # 会被拒绝
# ============================================================
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PKG="$ROOT/state-secretary"

SCOPE="${CNB_SCOPE:-scan}"
WINDOW_REPEATS="${CNB_WINDOW_REPEATS:-1}"

# 版本 = 被测提交（判"同一口径"的关键字段之一）
REVISION="$(git -C "$ROOT" rev-parse --short HEAD 2>/dev/null || echo unknown)"

# 受影响面 = **相对基线、针对被测提交**的变更文件清单。
#
# 关键（判重的正确性）：受影响面必须**由 revision 决定**，不能读"当前工作区"——
# 工作区内容会随文件创建/删除而变，同一 revision 会算出不同 impact →
# 每次都当成"新口径"→ 判重形同虚设（"机制看似有、实际拦不住"的典型形态）。
# 故：一律用 `git diff <base>...<REVISION>`（三点，只看该提交的变更）。
BASE_REF="${CNB_BASE_REF:-origin/main}"
CHANGED="$(git -C "$ROOT" diff --name-only "$BASE_REF"..."$REVISION" 2>/dev/null \
  || git -C "$ROOT" show --name-only --pretty=format: "$REVISION" 2>/dev/null \
  || true)"

if [ "$SCOPE" = "full" ]; then
  echo "[scope=full] 大版本：跑全量（非日常路径）"
  TEST_TARGETS=""
else
  echo "[scope=scan] 日常：**只跑增量**（不跑全量）"
  # 只取变更相关的测试文件（路径相对 state-secretary/，pytest 在该目录下运行）：
  #   - 变更的测试文件本身；
  #   - 变更源码文件对应的 test_*.py（按模块名匹配）。
  TEST_TARGETS="$(printf '%s\n' "$CHANGED" | while read -r f; do
      [ -z "$f" ] && continue
      case "$f" in
        state-secretary/tests/*.py) echo "${f#state-secretary/}" ;;
        state-secretary/src/*)
          mod="$(basename "$f" .py)"
          ls "state-secretary/tests/" 2>/dev/null \
            | grep -E "test_.*${mod}.*\.py$" | sed 's|^|tests/|' || true
          ;;
      esac
    done | sort -u || true)"
  # 变更面解析不出**具体**测试目标 → 退化为仅跑 tests/（仍**不是**全量回归：
  # 全量回归只在 SCOPE=full，日常绝不走那条路）
  if [ -z "$TEST_TARGETS" ]; then
    echo "[scope=scan] 未能解析具体变更测试 → 退化为 tests/（非 full 全量路径）"
    TEST_TARGETS="tests/"
  fi
fi

# 受影响面按**规范形式**（去重排序）参与指纹，避免"顺序/重复"制造假新口径
CANON_TARGETS="$(printf '%s\n' ${TEST_TARGETS:-full} \
                 | tr ' ' '\n' | sed '/^$/d' | sort -u | paste -sd, -)"

# ---- 机制：同口径重复执行守卫（**跳过**而不是"提醒别跑"）----
# 守卫只做"判定 + 登记"，退出码告诉本脚本该不该真的跑 pytest：
#   0  = 该跑
#   10 = 跳过（同口径重复 / 稳定性滥用）
set +e
GUARD_OUT="$(python3 - "$REVISION" "$SCOPE" "$WINDOW_REPEATS" "$CANON_TARGETS" \
             "$PKG" <<'PYEOF'
import os, sys
pkg = sys.argv[5]
sys.path.insert(0, os.path.join(pkg, "src"))
try:
    from secretary.collab import RunScopeGuard, load_rules
except Exception as exc:            # 依赖缺失 → 不因守卫坏了挡住干活
    print("[guard] 无法装载守卫（%s）→ 直接执行" % exc)
    sys.exit(0)

revision, scope, window, targets = sys.argv[1:5]
rules = load_rules(search_from=pkg)
guard = RunScopeGuard(rules, data_dir=os.path.join(
    os.environ.get("CNB_RUN_SCOPE_DIR", "/tmp"), "run-scope"))
cmd = "pytest -q %s" % targets
try:
    wr = int(window)
except ValueError:
    wr = 1
d = guard.decide(mode=scope, revision=revision, impact=targets,
                 command=cmd, window_repeats=wr)
print("[guard] action=%s :: %s" % (d.action, d.reason))
if not d.should_run:
    if d.alternatives:
        print("[guard] 稳定性论证请改用：%s" % ", ".join(d.alternatives))
    print("[guard] 跳过执行（同一口径重复跑无判别力）")
    sys.exit(10)
guard.record(mode=scope, revision=revision, impact=targets, command=cmd)
print("[guard] 已登记本次执行")
PYEOF
)"
GUARD_RC=$?
set -e
echo "$GUARD_OUT"
if [ "$GUARD_RC" -eq 10 ]; then
  echo "[ci] 本次跳过（不消耗构建/测试）"
  exit 0
fi

cd "$PKG"
if [ -n "${TEST_TARGETS:-}" ]; then
  # shellcheck disable=SC2086
  python3 -m pytest -q $TEST_TARGETS
else
  python3 -m pytest -q
fi
echo "[ci] 增量测试通过"
