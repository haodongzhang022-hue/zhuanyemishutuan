#!/usr/bin/env bash
# ============================================================
# M1/M2/M8 多行业单元路由POC (R6) · 跨单元索引 + 行业级路由
# 在 R4(单单元)+R5(单单元复合) 基础上, 把单元提升为可索引对象,
# 验证「一个意图路由到多个行业单元之一」的收编能力。
# 不触碰任何源仓: 本层仅持【路由索引】unit-index.yaml, 描述单元能力入口。
# ============================================================
IDX="${1:-unit-index.yaml}"; INPUT="${2:-}"
[ -f "$IDX" ] || { echo "ERR: index not found"; exit 1; }
[ -n "$INPUT" ] || { echo "ERR: no intent"; exit 1; }

# ---- 用 awk 把 yaml 单元块按行序解析为 段列表 ----
# 每个单元块起始: '  - unit_id: xxx'
# 读单元个数
NU=$(grep -c '^  - unit_id:' "$IDX")
[ "$NU" -eq 0 ] && { echo "ERR: no unit in index"; exit 1; }

# 存储每单元字段 (用 index 对齐)
declare -a U_NAME U_IND U_REPO U_ALIAS U_TAGS
while IFS= read -r u; do :; done <<< ""
# 逐单元解析: 拆成多个临时判定(awk per block)
block=0
cur=""
while IFS= read -r line; do
  case "$line" in
    "  - unit_id:"*) block=$((block+1)); cur="${line#*- unit_id: }"; cur="${cur// /}"; U_NAME[$block]="$cur";;
    *"repo_ref:"*)     U_REPO[$block]="${line#*repo_ref: }";;
    *"industry:"*)     U_IND[$block]="$(echo "$line"|sed 's/.*industry: *//;s/["'"'"']//g')";;
    *"industry_alias:"*) U_ALIAS[$block]="$(echo "$line"|sed 's/.*industry_alias: *//;s/[][,]/ /g;s/["'"'"']//g;s/  */ /g')";;
    *"capability_tags:"*) U_TAGS[$block]="$(echo "$line"|sed 's/.*capability_tags: *//;s/[][,]/ /g;s/["'"'"']//g;s/  */ /g')";;
  esac
done < "$IDX"

echo "======================================"
echo "M1/M2/M8 多行业单元路由POC  (R6)"
echo "--------------------------------------"
echo "  索引单元数 : $NU"

# ---- 行业级多级识别: 先判单元, 再判(可选岗位) ----
best=""; bestkw=""
for b in $(seq 1 $NU); do
   for kw in ${U_ALIAS[$b]} ${U_TAGS[$b]}; do
      case "$INPUT" in *"$kw"*)
         # 保留最"长/特异"命中? 简单取首个, 记录
         if [ -z "$best" ]; then best="$b"; bestkw="$kw"; fi
         ;;
      esac
   done
done

if [ -z "$best" ]; then
   echo "  识别     : 未命中任何行业单元 (置信低)"
   echo "  => 回落  : L2 入口人工一次意图确认"
   echo "======================================"
   exit 0
fi

echo "  unit     : ${U_NAME[$best]}"
echo "  repo_ref : $(echo "${U_REPO[$best]}"|tr -d '"')"
echo "  industry : ${U_IND[$best]}   (命中信号: $bestkw)"
echo "  岗位      : 见该单元自身 role 词典(R4/R5单单元可路由) "
echo "  => 路由  : 单直连 -> ${U_NAME[$best]}@(unit内岗位) "
echo "  缓存索引 : M8 索引命中 capability_tags, 供编排检索"
echo "======================================"
