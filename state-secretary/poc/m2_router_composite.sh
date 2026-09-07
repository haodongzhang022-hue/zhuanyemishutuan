#!/usr/bin/env bash
# ============================================================
# M1/M4/M6 复合路由POC (R5) · 多槽位扫描 + 会话编排判定
# 在 R4(m1_router.sh 单直连) 基础上叠加复合装配能力
# 定位: 暴露并最小验证「复合 agent ≥2 岗位」的编排判定
# ============================================================
REG="${1:-industry-register.yaml}"; INPUT="${2:-}"
[ -f "$REG" ] || { echo "ERR: register not found"; exit 1; }
[ -n "$INPUT" ] || { echo "ERR: no intent"; exit 1; }

UNIT=$(awk -F': ' '/^unit_id:/{gsub(/ /,"",$2);print $2}' "$REG")
IND=$(awk -F': ' '/^industry:/{sub(/^ +/,"",$2);print $2}' "$REG")
ALIASES=$(awk '/^industry_alias:/{sub(/^industry_alias: *[[]/,""); sub(/[]].*/,""); gsub(/[," ]/," "); print}' "$REG")

# 岗位词典(岗位词 → 匹配关键词) 与 R4 对齐, 保持单岗位命中兼容
declare -A DICT=(
  [reviewer]="评审 审查 复核 走查 检视"
  [product]="产品 需求 规划 排期"
  [architect]="架构设计 架构方案 架构师 做架构 总体设计 技术方案"
  [developer]="开发 编码 实现 写代码 落地 实现代码"
  [secretary]="编排 记录 秘书 汇总 会议纪要"
)
ROLE_ORDER=(secretary architect developer reviewer product)

# 岗位优先级(复合冲突仲裁): 依赖序 product->architect->developer->reviewer->secretary
declare -A RPRI=([secretary]=1 [product]=2 [architect]=3 [developer]=4 [reviewer]=5)

# 从 yaml 读前缀/复合能力
declare -A RPREFIX RCOMP
while IFS= read -r line; do
   nm=$(echo "$line" | sed -n 's/.*name: *\([a-z]*\).*/\1/p')
   px=$(echo "$line" | sed -n 's/.*prefix: *"\([^"]*\)".*/\1/p')
   cp=$(echo "$line" | sed -n 's/.*composite: *\(true\|false\).*/\1/p')
   [ -n "$nm" ] && RPREFIX[$nm]="$px"; [ -n "$cp" ] && RCOMP[$nm]="$cp"
done <<< "$(grep '^  - {name:' "$REG")"

# ---- 多槽位二次扫描: 记录每个岗位的命中关键词(可能多岗位) ----
declare -A HIT_KW
for r in "${!DICT[@]}"; do
   for kw in ${DICT[$r]}; do
      case "$INPUT" in *"$kw"*)
         # 同岗位去重, 累计命中数
         HIT_KW[$r]="${HIT_KW[$r]:-} $kw";;
      esac
   done
done

# 收集命中岗位并按依赖序稳定排列
hits=(); for r in "${ROLE_ORDER[@]}"; do [ -n "${HIT_KW[$r]}" ] && hits+=("$r"); done

# 行业命中(仅作归属判别)
ind_hit=""
for a in $ALIASES "$IND"; do case "$INPUT" in *"$a"*) ind_hit="$a"; break;; esac; done

echo "======================================"
echo "M4/M6 复合路由POC 结果  (R5)"
echo "--------------------------------------"
echo "  unit     : $UNIT"
echo "  industry : $IND  (命中信号: ${ind_hit:-无})"

if [ "${#hits[@]}" -eq 0 ] && [ -z "$ind_hit" ]; then
   echo "  识别     : 行业/岗位均未命中 (置信低)"
   echo "  => 回落  : L2 入口人工一次意图确认"
   echo "======================================"
   exit 0
fi

if [ "${#hits[@]}" -eq 0 ]; then
   echo "  识别     : 仅行业命中(岗位词未命中) -> ${UNIT} 默认入口"
   echo "======================================"
   exit 0
fi

# ---- M4 编排判定: 单岗位 or 复合 ----
if [ "${#hits[@]}" -eq 1 ]; then
   r="${hits[0]}"
   echo "  识别岗位 : ${r} (单岗位直连)"
   echo "  => 路由  : 单直连 -> ${UNIT}@${r}  前缀=${RPREFIX[$r]}"
   echo "  M4判定   : 单直连(无复合诉求)"
else
   # 多岗位 => 复合候选
   printf "  识别岗位 : %s (共%d岗)\n" "${hits[*]}" "${#hits[@]}"
   # 校验可复合性: 全部命中岗位须 composite=true, 否则需交接而非同session复合
   allcomp=1; for r in "${hits[@]}"; do [ "${RCOMP[$r]:-false}" = "true" ] || { allcomp=0; break; }; done
   # 会话前缀策略: 同行业同session, 取该行业固定集合(此处取岗位序首岗位前缀稳定), 命中岗位全在该集合内
   echo "  复合性   : 全部命中岗位 composite=true? $([ $allcomp -eq 1 ] && echo '是' || echo '否(需拆为交接)')"

   # 复合 agent 前缀: 同一行业稳定前缀集合 = 各岗位前缀的固定并集, 会话按行业固化不突变
   echo "  前缀稳定 : 一会话一行业=${IND}, 岗位前缀并集=${RPREFIX[*]} 固化零突变"
   echo "  --------------------------------"
   if [ $allcomp -eq 1 ]; then
      echo "  => M6装配 : 复合agent = ${UNIT} 同session并装岗位:"
      for r in "${hits[@]}"; do
         echo "       - ${r}  前缀=${RPREFIX[$r]}  职责关键词=${HIT_KW[$r]}"
      done
      echo "  达标信号 : 复合≥2岗位同会话运行 (量化信号② ✅)"
   else
      echo "  => 判定  : 命中岗位含不可复合项, 需拆新session经 M5 交接卡接力"
      for r in "${hits[@]}"; do
         echo "       - ${r}  composite=${RCOMP[$r]:-false}"
      done
   fi
fi
echo "======================================"
