#!/usr/bin/env bash
# ============================================================
# 判重台账的**可持久载体**接线（🔴-3 复评第 5 轮）
# ============================================================
# 平台每次 push = **新容器、新工作区**，本地判重台账从空开始 → 判重永不触发。
# 载体必须是"不会被回收"的东西 —— PR/Issue 评论正文（与 🔴A 同一方案）。
#
# 为什么需要本脚本（而不是"提示词要求"）：
#   `CNB_RUN_SCOPE_TRAIL` / `CNB_SESSION_ID` 此前**全库只有读、没有生产者**，
#   `.cnb.yml` 的 `main.push` 段也没有 `env`/`imports` → 载体停在文档里。
#   本脚本把两步落成**可执行的生产者**：
#     ① `fetch`     跑前：取回载体评论正文里的 `<!-- run-scope: … -->` 留痕
#     ② `writeback` 跑后：把本次新留痕**追加**回同一条评论（保留旧留痕）
#
# 用法（.cnb.yml 的 main.push 段，三段依次）：
#   ./scripts/collab-carrier.sh fetch
#   ./scripts/ci-dispatch.sh
#   ./scripts/collab-carrier.sh writeback
#
# 归属口径（🔴-3）：会话号取 `CNB_SESSION_ID`，缺省由 `CNB_REPO_SLUG` +
# PR/Issue 号推导（同一 PR/Issue = 同一会话，跨容器稳定）。
# **推导不出 → 不写回**（宁可这次不落载体，也不把别人的归属抹成空串）。
# ============================================================
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ACTION="${1:-}"
PKG="$ROOT/state-secretary"

if [ "$ACTION" != "fetch" ] && [ "$ACTION" != "writeback" ]; then
  echo "用法: $0 {fetch|writeback}" >&2
  exit 2
fi

python3 - "$PKG" "$ACTION" <<'PYEOF'
import os, sys
pkg, action = sys.argv[1], sys.argv[2]
sys.path.insert(0, os.path.join(pkg, "src"))
try:
    from secretary.collab import carrier
except Exception as exc:            # 依赖缺失 → 不因载体坏了挡住干活
    print("[carrier] 无法装载（%s）→ 跳过" % exc)
    raise SystemExit(0)

res = carrier.run_fetch() if action == "fetch" else carrier.run_writeback()
print("[carrier] %s: ok=%s session_id=%r :: %s"
      % (action, res.get("ok"), res.get("session_id"), res.get("reason")))
# 跑前取回：把留痕暴露给后续 stage（同 stage 内已写文件；此处仅做可观测打印）
if action == "fetch":
    path = os.environ.get("CNB_RUN_SCOPE_TRAIL_FILE") or carrier.DEFAULT_TRAIL_FILE
    try:
        with open(path, encoding="utf-8") as f:
            got = f.read().strip()
    except OSError:
        got = ""
    if got:
        os.environ["CNB_RUN_SCOPE_TRAIL"] = got
        print("[carrier] 已恢复留痕行数=%d（供 ci-incremental-test.sh 读取）"
              % len(got.splitlines()))
    else:
        print("[carrier] 无历史留痕（本次按无历史处理，不误拦）")
PYEOF
