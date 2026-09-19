#!/usr/bin/env bash
# ============================================================
# 增量测试入口（日常唯一跑法）——**只跑变更相关用例**
# ============================================================
# 为什么是脚本而不是把命令写死在 .cnb.yml：
#   判定逻辑（同口径/同版本 → 跳过；连跑 → 拒绝）必须被**真正执行**，
#   而不是散在流水线里靠"记得这么写"。脚本是机制的唯一落点。
#
# 口径（Issue #14 追问 / 本轮对齐官方）：
#   - 日常只跑增量（`scan`）；全量（`full`）只在**大版本**；
#   - **全量门禁（本轮加固）**：`CNB_SCOPE=full` **不再等于可以跑**——
#     必须同时满足"大版本 + 可采信来源 + 代码推进类事件"，否则**按增量改跑**
#     （拦住空烧全量；用户口径："他不然我们新开发一步，他就要跑一次 600 多次的验证"）；
#   - 同一**受审内容**（范围/受影响面/命令）只跑一次，**跨版本**也算同一口径
#     （修掉"每改一行 revision 就变 → 每次都放行"）；
#   - "连跑 N 次同一命令"当稳定性论证 → 拒绝（不受任何口径字段影响）；
#   - **能力降级**：算不出受影响面时**保守放行**（判不出就不增量；
#     漏跑比多跑危险——官方 SCA 增量规则同源）。
#   - **跨容器判重（🔴F，本轮）**：平台每次 push = **新容器、新工作区**，
#     `<tmp>/run-scope` 每次都从空开始 → 判重永不触发（实测 r1/r2/r3 全 run）。
#     故台账随**可持久载体**走（PR/Issue 评论正文，与 🔴A 同一方案）：
#       ① 跑之前 `adopt_trails()` 从 `CNB_RUN_SCOPE_TRAIL`（评论正文）恢复——
#          **生产者**是 `scripts/collab-carrier.sh fetch`（落
#          `CNB_RUN_SCOPE_TRAIL_FILE`，🔴-3：此前该变量全库只有读、没有生产者）；
#       ② 跑之后 `record_repeat(..., verdict=<真实结论>)` + 打印
#          `<!-- run-scope: … -->` 并落 `CNB_RUN_SCOPE_NEW_TRAIL_FILE`，
#          由 `scripts/collab-carrier.sh writeback` **追加写回同一条评论**
#          （保留旧留痕与各自 `session_id` 归属）。
#     CI 必须落**真实结论**（verdict）：否则"红→修→再跑"这条唯一有信息量的
#     路径会被判重自己堵死（内容没变=同一份受审内容，结论为空=不绿不红）。
#
# 用法：
#   ./scripts/ci-incremental-test.sh                      # 增量（默认）
#   CNB_MAJOR_VERSION=1 CNB_SCOPE=full CNB_TRIGGER_SOURCE=operator \
#     CNB_EVENT=push ./scripts/ci-incremental-test.sh     # 全量（仅大版本 + 凭据）
#   CNB_WINDOW_REPEATS=3 ./scripts/ci-incremental-test.sh # 会被拒绝
# ============================================================
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PKG="$ROOT/state-secretary"

SCOPE="${CNB_SCOPE:-scan}"
WINDOW_REPEATS="${CNB_WINDOW_REPEATS:-1}"
# 全量**凭据**（Issue #14 本轮）：只有可采信来源 + 大版本事实 + 代码推进事件
# 三者同时成立，`full` 才真的跑；否则**自动改跑增量**（拦了但给路）。
MAJOR_VERSION="${CNB_MAJOR_VERSION:-}"      # 非空且非 0 → 大版本（可判定事实）
TRIGGER_SOURCE="${CNB_TRIGGER_SOURCE:-agent_claim}"
# 触发事件：平台内置变量优先（CI 里由平台给出），否则退化为"不可判定"
EVENT="${CNB_EVENT:-${CNB_PIPELINE_EVENT:-}}"

# 版本 = 被测提交（判"同一口径"的关键字段之一）
REVISION="$(git -C "$ROOT" rev-parse --short HEAD 2>/dev/null || echo unknown)"

# 受影响面 = **相对基线、针对被测提交**的变更文件清单。
#
# 关键（判重的正确性）：受影响面必须**由 revision 决定**，不能读"当前工作区"——
# 工作区内容会随文件创建/删除而变，同一 revision 会算出不同 impact →
# 每次都当成"新口径"→ 判重形同虚设（"机制看似有、实际拦不住"的典型形态）。
# 故：一律用 `git diff <base>...<REVISION>`（三点，只看该提交的变更）。
BASE_REF="${CNB_BASE_REF:-origin/main}"
# 基线是否**真的可解析**：解析不出（新建分支 / 无该 ref）时不要把空变更集
# 当成"没有变更"——那会让不同变更集撞成同一指纹而被**跳过**（漏跑，危险方向）。
BASE_SHA="$(git -C "$ROOT" rev-parse --verify -q "$BASE_REF" 2>/dev/null || true)"
CHANGED="$(git -C "$ROOT" diff --name-only "$BASE_REF"..."$REVISION" 2>/dev/null \
  || git -C "$ROOT" show --name-only --pretty=format: "$REVISION" 2>/dev/null \
  || true)"

# ---- 全量门禁（**跑之前先过闸**）：不满足大版本条件 → 自动改跑增量 ----
# 这道闸放在最前面：`CNB_SCOPE=full` 只是"想跑全量"，**不等于**可以跑。
if [ "$SCOPE" = "full" ]; then
  GATE_OUT="$(python3 - "$MAJOR_VERSION" "$TRIGGER_SOURCE" "$EVENT" "$ROOT" <<'PYEOF'
import os, sys
sys.path.insert(0, os.path.join(sys.argv[4], "state-secretary", "src"))
try:
    from secretary.collab import decide_full, full_run_eligible
    from secretary.collab.rules import load_rules
except Exception as exc:
    print("ALLOW")
    print("[full-gate] 无法装载门禁（%s）→ 不因门禁坏了挡住干活" % exc)
    raise SystemExit(0)
mv_raw, src, event = sys.argv[1:4]
rules = load_rules(search_from=os.path.join(sys.argv[4], "state-secretary"))
mv = mv_raw.strip() not in ("", "0", "false", "False", "no") if mv_raw.strip() else None
d = decide_full(rules, major_version=mv, source=src or "agent_claim",
                event=event)
if d.should_run and d.mode == "full":
    ev = full_run_eligible(event, blocked_events=rules.blocked_events)
    if ev["blocked"]:
        d = decide_full(rules, major_version=False, source=src or "agent_claim",
                        event=event)
        d.reason = "%s；%s" % (ev["reason"], d.reason)
if d.should_run and d.mode == "full":
    print("ALLOW")
    print("[full-gate] 大版本 + 可采信来源 + 代码推进事件 → 允许全量")
else:
    print("BLOCK")
    print("[full-gate] action=%s(blocked_full) :: %s" % (d.action, d.reason))
    print("[full-gate] 改跑增量 → %s" % d.scan_command)
PYEOF
)"
  GATE_VERDICT="$(printf '%s\n' "$GATE_OUT" | head -1)"
  printf '%s\n' "$GATE_OUT" | tail -n +2
  if [ "$GATE_VERDICT" != "ALLOW" ]; then
    echo "[scope=full→scan] 全量门禁拦截：本次**改跑增量**（不跑全量回归）"
    SCOPE="scan"
  fi
fi

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
             "$PKG" "${BASE_SHA:-}" "${CNB_EVENT:-}" <<'PYEOF'
import os, sys
pkg = sys.argv[5]
base_sha = sys.argv[6] if len(sys.argv) > 6 else ""
event = sys.argv[7] if len(sys.argv) > 7 else ""
sys.path.insert(0, os.path.join(pkg, "src"))
try:
    from secretary.collab import RunScopeGuard, load_rules
    from secretary.collab import carrier
except Exception as exc:            # 依赖缺失 → 不因守卫坏了挡住干活
    print("[guard] 无法装载守卫（%s）→ 直接执行" % exc)
    sys.exit(0)

revision, scope, window, targets = sys.argv[1:5]
rules = load_rules(search_from=pkg)
guard = RunScopeGuard(rules, data_dir=os.path.join(
    os.environ.get("CNB_RUN_SCOPE_DIR", "/tmp"), "run-scope"))
cmd = "pytest -q %s" % targets

# 🔴F ①：从**可持久载体**（评论正文）恢复判重台账。平台每次 push 都是新容器，
# 本地台账从空开始 → 判重永不触发；容器会被回收，**评论不会**。
#
# 🔴-3：载体入口有**两个生产者**（`scripts/collab-carrier.sh fetch` 落文件；
# 调用方也可直接给 `CNB_RUN_SCOPE_TRAIL`）。会话号**必须有生产者**——
# 旧实现只读 `CNB_SESSION_ID`（全库无生产者）→ 归属恒空 → 写回时把评论里
# 所有会话的归属抹平（漏跑方向）。现按 `carrier.session_id_for()` 推导：
# 显式 `CNB_SESSION_ID` → `CNB_REPO_SLUG` + PR/Issue 号 → 空（**不冒充**）。
trail = os.environ.get("CNB_RUN_SCOPE_TRAIL", "")
if not trail:
    trail = carrier._read(os.environ.get("CNB_RUN_SCOPE_TRAIL_FILE")
                          or carrier.DEFAULT_TRAIL_FILE)
sid = carrier.session_id_for(os.environ)
if not sid:
    print("[guard] **告警**：归属推导不出（无 CNB_SESSION_ID 且无 PR/Issue 号）"
          "→ 本次不落载体归属，跑后**不写回**（不得抹平别人的归属）")
if trail:
    got = guard.adopt_trails(trail, session_id=sid)
    print("[guard] 从留痕恢复（跨容器继承）：merged=%s session_id=%r"
          % (got.get("merged"), sid))

try:
    wr = int(window)
except ValueError:
    wr = 1
# **去升级口径**（本轮加固）：按"受审内容"判重——同一份变更集在**新版本**下
# 重跑同样算重复（修掉"每改一行 revision 就变 → 每次都放行"）。
# 🔴F：上一次结论取**台账里的真实结论**（跨容器从留痕恢复），不靠调用方传参。
d = guard.decide_repeat(mode=scope, revision=revision, impact=targets,
                        command=cmd, window_repeats=wr,
                        base=(base_sha or None), event=event)
print("[guard] action=%s :: %s" % (d.action, d.reason))
print("[guard] 口径去升级=%s（同一受审内容历史版本数=%s）"
      % (getattr(d, "repeat_scope", "revision"),
         getattr(d, "content_revisions", 0)))
if getattr(d, "degraded", False):
    print("[guard] 能力降级 → 保守放行（%s）" % d.degrade_reason)
if not d.should_run:
    if d.action == "blocked_stability":
        print("[guard] **拒绝连跑**：跑法固定，重复跑不产生判别力")
    if d.alternatives:
        print("[guard] 稳定性论证请改用：%s" % ", ".join(d.alternatives))
    print("[guard] 跳过执行（同一受审内容重复跑无判别力）")
    sys.exit(10)
# **不在这里登记**：先跑，拿到**真实结论**再登记（见脚本末尾的 record 步骤）——
# 登记时若不给 verdict，"上一轮不绿才允许复跑"的例外永远不成立（🔴F）。
print("[guard] 判定=执行；跑完再按**真实结论**登记（verdict 由本脚本回填）")
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
set +e
if [ -n "${TEST_TARGETS:-}" ]; then
  # shellcheck disable=SC2086
  python3 -m pytest -q $TEST_TARGETS
else
  python3 -m pytest -q
fi
TEST_RC=$?
set -e

# 🔴F ②：把**真实结论**写进台账，并打印可持久留痕（流水线追加写回同一条评论）。
# 没有这一步：① 台账里没有 verdict → "上一轮不绿才允许复跑"的例外永不成立；
# ② 下一棒（新容器）读不到"这份内容跑过几遍" → 判重永不触发。
VERDICT="$([ "$TEST_RC" -eq 0 ] && echo "passed" || echo "failed(rc=$TEST_RC)")"
python3 - "$REVISION" "$SCOPE" "$CANON_TARGETS" "$PKG" "$VERDICT" <<'PYEOF'
import os, sys
rev, scope, targets, pkg, verdict = sys.argv[1:6]
sys.path.insert(0, os.path.join(pkg, "src"))
try:
    from secretary.collab import RunScopeGuard, load_rules
except Exception as exc:                       # 依赖缺失 → 不挡住干活
    print("[guard] 无法装载守卫（%s）→ 跳过登记" % exc)
    raise SystemExit(0)
sys.path.insert(0, os.path.join(pkg, "src"))
from secretary.collab import carrier          # noqa: E402  （上面已装载过）
guard = RunScopeGuard(load_rules(search_from=pkg), data_dir=os.path.join(
    os.environ.get("CNB_RUN_SCOPE_DIR", "/tmp"), "run-scope"))
trail_in = os.environ.get("CNB_RUN_SCOPE_TRAIL", "")
if not trail_in:
    trail_in = carrier._read(os.environ.get("CNB_RUN_SCOPE_TRAIL_FILE")
                             or carrier.DEFAULT_TRAIL_FILE)
# 🔴-3：会话号**必须推导**（旧实现只读无生产者的 `CNB_SESSION_ID`）
sid = carrier.session_id_for(os.environ)
if trail_in:
    guard.adopt_trails(trail_in, session_id=sid)
guard.record_repeat(mode=scope, revision=rev, impact=targets,
                    command="pytest -q %s" % targets, verdict=verdict,
                    session_id=sid)
print("[guard] 已登记本次执行：verdict=%s session_id=%r" % (verdict, sid))
# 可持久留痕（写回评论用；保留旧留痕与**各自归属**，解析侧取 max + 按归属过滤）
out = guard.trails(session_id=sid)
if out:
    # 🔴-3：落文件供 `scripts/collab-carrier.sh writeback` 追加写回**同一条评论**。
    # 归属为空时**不写回**（`run_writeback` 会拒绝）——宁可这次不落载体，
    # 也不把评论里别的会话的归属抹成空串（那是漏跑方向）。
    carrier._write(os.environ.get("CNB_RUN_SCOPE_NEW_TRAIL_FILE")
                   or carrier.DEFAULT_OUT_FILE, out)
    print("[guard] 写回评论的留痕（追加，勿删旧的）：")
    print(out)
PYEOF

if [ "$TEST_RC" -ne 0 ]; then
  echo "[ci] 增量测试**未通过**（rc=$TEST_RC）——结论已登记，修复后同一份受审内容允许复跑"
  exit "$TEST_RC"
fi
echo "[ci] 增量测试通过"
