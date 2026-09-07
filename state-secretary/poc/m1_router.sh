#!/usr/bin/env bash
# ============================================================
# M1 最小POC · 行业识别 + 意图路由 (R4)  v3
# 轻量脚本式, 无第三方依赖
# ============================================================
REG="${1:-industry-register.yaml}"; INPUT="${2:-}"
[ -f "$REG" ] || { echo "ERR: register not found"; exit 1; }
[ -n "$INPUT" ] || { echo "ERR: no intent"; exit 1; }

UNIT=$(awk -F': ' '/^unit_id:/{gsub(/ /,"",$2);print $2}' "$REG")
IND=$(awk -F': ' '/^industry:/{sub(/^ +/,"",$2);print $2}' "$REG")
ALIASES=$(awk '/^industry_alias:/{sub(/^industry_alias: *[[]/,""); sub(/[]].*/,""); gsub(/[," ]/," "); print}' "$REG")

declare -A DICT=(
  [reviewer]="评审 审查 复核 走查 检视"
  [product]="产品 需求 规划 排期"
  [architect]="架构设计 架构方案 架构师 做架构 总体设计 技术方案"
  [developer]="开发 编码 实现 写代码 落地 实现代码"
  [secretary]="编排 记录 秘书 汇总 会议纪要"
)
ROLE_KEYMAP=(reviewer product architect developer secretary)
declare -A RPREFIX RCOMP
while IFS= read -r line; do
   nm=$(echo "$line" | sed -n 's/.*name: *\([a-z]*\).*/\1/p')
   px=$(echo "$line" | sed -n 's/.*prefix: *"\([^"]*\)".*/\1/p')
   cp=$(echo "$line" | sed -n 's/.*composite: *\(true\|false\).*/\1/p')
   [ -n "$nm" ] && RPREFIX[$nm]="$px"; [ -n "$cp" ] && RCOMP[$nm]="$cp"
done <<< "$(grep '^  - {name:' "$REG")"

# ---- 岗位命中 ----
route=""; px=""; conf=0
for r in "${ROLE_KEYMAP[@]}"; do
   for kw in ${DICT[$r]}; do
      case "$INPUT" in *"$kw"*) route="$r"; px="${RPREFIX[$r]}"; conf=$((conf+1)); break;; esac
   done
done

# ---- 行业命中(不参与岗位, 仅作归属/多单元判别) ----
ind_hit=""
for a in $ALIASES "$IND"; do case "$INPUT" in *"$a"*) ind_hit="$a"; break;; esac; done

# ---- 置信闸口: 岗位命中或行业别名/标签命中才算达标; 否则回落 L2 人工一次确认 ----
if [ -z "$route" ] && [ -z "$ind_hit" ]; then
   echo "======================================"
   echo "RESULT: 行业/岗位均未识别 (置信低)"
   echo "  => 回落 L2 入口: 人工一次意图确认"
   echo "  输入: $INPUT"
   echo "======================================"
   exit 0
fi
# 单单元POC下, 岗位命中但行业别名未命中 => 归属本单元(默认行业)
if [ -z "$ind_hit" ]; then ind_hit="岗位词触发(单单元归属)"; fi

echo "======================================"
echo "M1/M2 最小POC 路由结果"
echo "--------------------------------------"
echo "  unit     : $UNIT"
echo "  industry : $IND  (命中信号: $ind_hit)"
if [ -n "$route" ]; then
   echo "  role     : $route  前缀=${px}  可复合=${RCOMP[$route]}"
   echo "  => 路由  : 单直连 -> ${UNIT}@${route}  会话前缀=${px}"
else
   echo "  role     : 未明确(仅行业) -> ${UNIT} 默认入口"
fi
echo "  前缀稳定 : 一会话一行业=${IND}, 前缀集合固化零突变"
echo "======================================"
