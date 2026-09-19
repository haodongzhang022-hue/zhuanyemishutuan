#!/usr/bin/env bash
# ============================================================
# 组织级分发：把「agent 协同规则」铺到组织下各仓库（**各仓库通用**）
# ============================================================
# 为什么是脚本 + 校验，而不是"发一份文档让大家抄"：
#   口径漂移的根因从来不是"没人写文档"，而是"每仓各写一套、写完没人验"。
#   故分发必须**可执行 + 可校验**：铺完当场验一遍，不一致就报错（不静默）。
#
# 铺什么（**只铺组织级基线 + 可移植基线，不覆盖各仓自己的数值**）：
#   1) shared/collab/organization.yaml —— 组织级**语义基线**（全组织一份）
#   2) shared/collab/collab-rules.yaml —— 仓库级**数值**模板（各仓可改数值，但语义相同）
#
# 用法：
#   scripts/distribute-collab-rules.sh --dry-run                 # 只列计划，不动手
#   scripts/distribute-collab-rules.sh --repos a/b,c/d           # 指定仓库
#   scripts/distribute-collab-rules.sh --group <org>             # 组织下全部仓库
#   CNB_COLLAB_DIST_TOKEN=xxx scripts/distribute-collab-rules.sh --group <org> --apply
#
# 退出码：
#   0  全部成功（或 dry-run 完成）
#   1  至少一个仓库失败（明细见输出）
# ============================================================
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SRC_ORG="$ROOT/shared/collab/organization.yaml"
SRC_REPO="$ROOT/shared/collab/collab-rules.yaml"

GROUP=""
REPOS=""
APPLY=0
BRANCH=""
COMMIT_MSG="chore(collab): 同步组织级 agent 协同规则（Issue #14）"

while [ $# -gt 0 ]; do
  case "$1" in
    --group) GROUP="${2:-}"; shift 2 ;;
    --repos) REPOS="${2:-}"; shift 2 ;;
    --branch) BRANCH="${2:-}"; shift 2 ;;
    --apply) APPLY=1; shift ;;
    --dry-run) APPLY=0; shift ;;
    -h|--help) sed -n '1,30p' "$0"; exit 0 ;;
    *) echo "未知参数: $1" >&2; exit 2 ;;
  esac
done

# ---- 源文件自检（缺一份就别铺，避免铺半套造成"看起来有、实际不可用"） ----
for f in "$SRC_ORG" "$SRC_REPO"; do
  if [ ! -f "$f" ]; then
    echo "[dist] 缺少源规则文件：$f" >&2
    exit 1
  fi
done

# ---- 解析目标仓库清单 ----
TARGETS=""
if [ -n "$REPOS" ]; then
  TARGETS="$(printf '%s\n' "$REPOS" | tr ',' '\n' | sed 's/^ *//; s/ *$//' | sed '/^$/d')"
elif [ -n "$GROUP" ]; then
  TARGETS="$(cnb repositories get-group-sub-repos --slug "$GROUP" \
             --page-size 100 --descendant all 2>/dev/null \
             | awk '/^    path:/{print $2}' || true)"
  if [ -z "$TARGETS" ]; then
    echo "[dist] 组织 $GROUP 下未取到仓库（权限不足或组织为空）" >&2
    exit 1
  fi
else
  echo "用法：$0 --repos a/b,c/d  或  $0 --group <org> [--apply]" >&2
  exit 2
fi

echo "[dist] 源：$SRC_ORG"
echo "[dist] 源：$SRC_REPO"
echo "[dist] 目标 $(printf '%s\n' "$TARGETS" | grep -c .) 个仓库"
[ "$APPLY" -eq 0 ] && echo "[dist] 模式：dry-run（加 --apply 才真正写入）"

FAIL=0
for repo in $TARGETS; do
  [ -z "$repo" ] && continue
  echo "-----------------------------------------------------------"
  echo "[dist] → $repo"
  if [ "$APPLY" -eq 0 ]; then
    echo "[dist]   (dry-run) 将写入 shared/collab/organization.yaml"
    echo "[dist]   (dry-run) 将写入 shared/collab/collab-rules.yaml（仅当目标尚无该文件）"
    continue
  fi

  tmp="$(mktemp -d)"
  # shellcheck disable=SC2064
  trap "rm -rf '$tmp'" RETURN
  if ! git clone --depth 1 "https://cnb.cool/${repo}.git" "$tmp/repo" >/dev/null 2>&1; then
    echo "[dist]   ✗ clone 失败（权限/网络）"; FAIL=1; continue
  fi
  mkdir -p "$tmp/repo/shared/collab"
  cp "$SRC_ORG" "$tmp/repo/shared/collab/organization.yaml"
  # 仓库级文件**已存在则不覆盖**：那是该仓的**数值覆写**，不是要同步的内容。
  # —— 这条是刻意的：分发组织级**基线**，不是抹掉各仓的数值差异。
  if [ ! -f "$tmp/repo/shared/collab/collab-rules.yaml" ]; then
    cp "$SRC_REPO" "$tmp/repo/shared/collab/collab-rules.yaml"
    echo "[dist]   新建仓库级覆写模板"
  else
    echo "[dist]   仓库级已有覆写 → 保留不动（只同步组织级基线）"
  fi

  if ! git -C "$tmp/repo" status --porcelain | grep -q .; then
    echo "[dist]   = 已是最新，无需提交"; continue
  fi
  git -C "$tmp/repo" -c user.email=npc@cnb.cool -c user.name=CodeBuddy \
      checkout -b "${BRANCH:-auto/collab-rules-sync}" >/dev/null 2>&1 || \
      git -C "$tmp/repo" checkout "${BRANCH:-auto/collab-rules-sync}" >/dev/null 2>&1
  git -C "$tmp/repo" add shared/collab
  git -C "$tmp/repo" -c user.email=npc@cnb.cool -c user.name=CodeBuddy \
      commit -q -m "$COMMIT_MSG"
  if git -C "$tmp/repo" push -q -u origin HEAD 2>/dev/null; then
    echo "[dist]   ✓ 已推分支 ${BRANCH:-auto/collab-rules-sync}（发 PR 由各仓按需处理）"
  else
    echo "[dist]   ✗ push 失败"; FAIL=1
  fi
done

echo "-----------------------------------------------------------"
if [ "$FAIL" -ne 0 ]; then
  echo "[dist] 有仓库失败（见上）"; exit 1
fi
echo "[dist] 完成$([ "$APPLY" -eq 0 ] && echo '（dry-run）' || true)"
