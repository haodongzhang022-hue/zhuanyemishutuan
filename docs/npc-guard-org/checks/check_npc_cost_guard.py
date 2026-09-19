#!/usr/bin/env python3
"""组织级 NPC 成本护栏校验（共享仓 npc-guard/checks/ 维护，引用方 CI 调用）。

四层判据，**互不替代**：

1. **值断言** —— 引用方本地的核数 / timeout / 回合上限 / 模型名必须在组织基线上界内。
2. **值来源断言** —— 本地 cpus 声明 ≤1 处；0 处时必须由共享文件提供；且可注入型护栏值
   必须**经正确通道**来自**共享仓**（`imports`→env / `optionsFrom`→options）。
3. **被引用文件自身也过同一套值断言** —— 兼容两种形态：平铺基线（`baseline/*.yml`）与
   模板嵌套（`templates/*.yml` 的 `$.*[].stages[].env` 与 `runner.cpus`）。
   否则"共享值被改坏"是校验盲区（模板 cpus 8 + 回合 500 + 模型名去点三处同改仍全绿）。
4. **路线分派 + 通道分派** —— 镜像路线与 npc:go 路线的字段名不同且不可互换；
   且 env 字段只能走 `env`/`imports`、options 字段只能走 `options`/`optionsFrom`。
   字段混用或通道错配**都是静默失效**，一律判 FAIL。

退出码：0 通过；1 断言失败；2 依赖或入参错误（大声失败，不静默跳过）。
"""
import argparse
import os
import re
import sys
import urllib.request
from collections import namedtuple

try:
    import yaml
except ImportError:  # 依赖必须显式声明，缺 yaml 时以 rc=2 大声失败
    sys.stderr.write("FATAL: 需要 pyyaml（引用方 CI 镜像请预装或 pip install pyyaml）\n")
    sys.exit(2)

MIRROR_ENTRY = "sh /app/start.sh"          # 镜像路线入口
MIRROR_FIELD = "DSH_MAX_STEPS"             # 镜像路线回合上限字段名（env 通道）
MIRROR_MODEL = "DSH_MODEL"                 # 镜像路线模型字段名（env 通道）
NPCGO_FIELD = "maxTurns"                   # npc:go 路线回合上限字段名（options 通道）
NPCGO_MODEL = "model"                      # npc:go 路线模型字段名（options 通道）
GUARD_REPO_MARK = "npc-guard"              # 共享仓标识（值来源可追溯）

#: 路线 → 该路线的护栏字段（通道即字段名决定：env 字段 vs options 字段）
ROUTE_FIELDS = {
    "mirror": (MIRROR_FIELD, MIRROR_MODEL),
    "npc:go": (NPCGO_FIELD, NPCGO_MODEL),
}
#: 通道 → 该通道能喂到的字段集合（喂错通道 = 静默失效）
CHANNEL_FIELDS = {
    "imports": (MIRROR_FIELD, MIRROR_MODEL),
    "optionsFrom": (NPCGO_FIELD, NPCGO_MODEL),
}
#: 路线 → 能承载该路线字段的**通道**（include 可承载两者：路线 B 的 stage 级字段由合并而来）
ROUTE_CHANNELS = {
    "mirror": ("imports", "include"),
    "npc:go": ("optionsFrom", "include"),
}
ALL_TURNS_FIELDS = (MIRROR_FIELD, NPCGO_FIELD)
ALL_MODEL_FIELDS = (MIRROR_MODEL, NPCGO_MODEL)

Ref = namedtuple("Ref", "raw resolved channel ignore_error")


def repo_root_of(config_path):
    """本地配置的**项目根**：`.cnb/npc.yml` → 上一级；否则取文件所在目录。

    与平台语义一致（include / imports 的本地路径相对项目根，不是相对 CI cwd）。
    """
    abs_path = os.path.abspath(config_path)
    parent = os.path.dirname(abs_path)
    if os.path.basename(parent) == ".cnb":
        return os.path.dirname(parent)
    return parent


def load(path, base_dir=None):
    """路径或 URL 均可（URL 供 CI 直读基线，免额外 curl 步骤）。

    相对路径按 `base_dir`（项目根）解析——与流水线语义一致。
    """
    if str(path).startswith("http"):
        # 平台配置引用用 /-/blob/…（平台内部解析）；CI 里 curl/urllib 拉原文须用 /-/git/raw/…
        # 两种 URL 同文档混用是常见照抄错误，这里统一转 raw，两种写法都能跑。
        url = re.sub(r"/-/blob/", "/-/git/raw/", str(path))
        try:
            with urllib.request.urlopen(urllib.request.Request(url), timeout=30) as resp:
                return yaml.safe_load(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            token = os.environ.get("CNB_TOKEN")
            if exc.code not in (401, 403) or not token:
                raise
            # 私有共享仓：匿名拉不动时再带 token 重试（实测带 token 打公开端点会 400，
            # 故先匿名、失败才回退，不做无条件加头）
            req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
            with urllib.request.urlopen(req, timeout=30) as resp:
                return yaml.safe_load(resp.read().decode("utf-8"))
    resolved = path
    if base_dir and not os.path.isabs(str(path)):
        resolved = os.path.join(base_dir, str(path))
    with open(resolved, encoding="utf-8") as fh:
        return yaml.safe_load(fh)


def leaves(node, path=""):
    if isinstance(node, dict):
        for key, val in node.items():
            yield from leaves(val, f"{path}.{key}")
    elif isinstance(node, list):
        for idx, val in enumerate(node):
            yield from leaves(val, f"{path}[{idx}]")
    else:
        yield path, node


def iter_jobs(node, path=""):
    """遍历所有 **job**（dict，含 `stages` 或 `runner` 者），跳过顶层 `.` 开头的锚点定义。"""
    if isinstance(node, dict):
        if isinstance(node.get("stages"), list):
            yield path, node
        for key, val in node.items():
            if path == "" and str(key).startswith("."):
                continue
            yield from iter_jobs(val, f"{path}.{key}")
    elif isinstance(node, list):
        for idx, val in enumerate(node):
            yield from iter_jobs(val, f"{path}[{idx}]")


def find_stages(node, path=""):
    """遍历所有 stage。

    **跳过锚点定义**（顶层 key 以 `.` 开头，如 `.npc` / `.base-stage`）：
    它们不是可执行的 stage，只是给角色 `<<` 复用的模板；模型名等角色级字段
    由引用它的角色段声明，对锚点断言必然误报。
    """
    for jpath, job in iter_jobs(node, path):
        for idx, st in enumerate(job["stages"]):
            if isinstance(st, dict):
                yield f"{jpath}.stages[{idx}]", st


def minutes(text):
    """'72' / '2h' / '90m' / '30s' -> 分钟（float）；不合法返回 None。"""
    m = re.fullmatch(r"\s*(\d+)\s*([smh]?)\s*", str(text))
    if not m:
        return None
    return int(m.group(1)) * {"": 1, "s": 1 / 60, "m": 1, "h": 60}[m.group(2)]


def route_of(stage):
    if stage.get("type") == "npc:go":
        return "npc:go"
    script = stage.get("script") or ""
    if isinstance(script, list):
        script = "\n".join(str(x) for x in script)
    return "mirror" if MIRROR_ENTRY in str(script) else None


def merge_config(base, over):
    """按官方 include 合并规则模拟合并（用于"会不会重复 stage"的可验证判定）。

    * 数组 + 数组 → 追加元素
    * 对象 + 对象 → 同名键覆盖、新键添加（递归）
    * 数组 + 对象 / 对象 + 数组 → 仅保留数组
    """
    if isinstance(base, list) and isinstance(over, list):
        return base + over
    if isinstance(base, dict) and isinstance(over, dict):
        out = dict(base)
        for key, val in over.items():
            out[key] = merge_config(out[key], val) if key in out else val
        return out
    if isinstance(over, list):
        return over
    return over if not isinstance(base, list) else base


def resolve_includes(path, seen=None, depth=0):
    """递归解析 include（本地相对路径优先），返回合并后的配置。

    `include` 是**配置级合并**，不做文本拼接，故锚点无法跨文件；这里正是为了把
    "引用方再写一份 stages 会不会重复执行"变成可跑的判定。相对路径按**引用它的
    文件所在目录**解析（与流水线语义一致：include 的本地路径相对项目根，
    嵌套时按所在文件就近解析）。
    """
    seen = seen or set()
    if depth > 64 or path in seen:  # 与平台一致：嵌套上限 64
        return {}
    seen.add(path)
    try:
        doc = load(path)
    except Exception:
        return {}
    if not isinstance(doc, dict):
        return {}
    base_dir = repo_root_of(path)
    merged = {}
    for item in doc.get("include") or []:
        if isinstance(item, str):
            ref = item if str(item).startswith("http") else os.path.join(base_dir, item)
            merged = merge_config(merged, resolve_includes(ref, seen, depth + 1))
        elif isinstance(item, dict):
            if item.get("config"):          # 内联对象：不是文件，无需加载
                merged = merge_config(merged, item["config"])
            elif item.get("path"):
                ref = item["path"]
                if not str(ref).startswith("http"):
                    ref = os.path.join(base_dir, ref)
                merged = merge_config(merged, resolve_includes(ref, seen, depth + 1))
    merged = merge_config(merged, {k: v for k, v in doc.items() if k != "include"})
    return merged


def duplicate_npc_stages(merged):
    """合并后同一事件键下出现 >1 个 npc go stage = 重复执行（数组追加的后果）。"""
    dupes = []
    if not isinstance(merged, dict):
        return dupes
    for top, events in merged.items():
        if not isinstance(events, dict):
            continue
        for event, jobs in events.items():
            if not isinstance(jobs, list):
                continue
            hits = []
            for idx, job in enumerate(jobs):
                if not isinstance(job, dict):
                    continue
                for st in job.get("stages") or []:
                    if isinstance(st, dict) and "npc" in str(st.get("name", "")).lower():
                        hits.append(f"{top}/{event}[{idx}].stages:{st.get('name')}")
            if len(hits) > 1:
                dupes.append((top, event, hits))
    return dupes


def _dict_nodes(node):
    """遍历配置里的所有 dict 节点（job 与 stage 均可承载 imports / optionsFrom）。"""
    if isinstance(node, dict):
        yield node
        for val in node.values():
            yield from _dict_nodes(val)
    elif isinstance(node, list):
        for val in node:
            yield from _dict_nodes(val)


def collect_refs(cfg, base_dir):
    """所有被引用的**外部文件**，带**通道**信息（imports / optionsFrom / include）。

    通道是判据的一部分：`imports` 只喂 env、`optionsFrom` 只喂 options，
    走错通道即静默失效。内联 `- config: {...}` 不是文件，不收。
    """
    refs, seen = [], set()

    def add(raw, channel, ignore_error=False):
        if not isinstance(raw, str):
            return
        resolved = raw if raw.startswith("http") else os.path.join(base_dir, raw)
        key = (resolved, channel)
        if key not in seen:
            seen.add(key)
            refs.append(Ref(raw=raw, resolved=resolved, channel=channel,
                            ignore_error=ignore_error))

    # imports / optionsFrom 可出现在 job 或 stage 上（单 job stage 可省略层级，字段同级）
    for node in _dict_nodes(cfg):
        for channel in ("imports", "optionsFrom"):
            val = node.get(channel)
            if val is None:
                continue
            for item in (val if isinstance(val, list) else [val]):
                add(item, channel)
    for item in (cfg.get("include") or []):
        if isinstance(item, str):
            add(item, "include")
        elif isinstance(item, dict) and item.get("path"):
            add(item["path"], "include", bool(item.get("ignoreError")))
    return refs


#: 共享资产包里**允许承载护栏值**的目录（值来源判据只认这三个目录）
SHARED_DIRS = ("rules", "baseline", "templates")


def shared_roots(rules_path):
    """与 `--rules` 同源的**共享资产包根目录**（本地路径形态）。

    生产接入用共享仓 URL（`npc-guard`），离线/自测用同一份资产包的相对路径——两者等价。
    引用方**仓内自建**的基线不在共享资产包内，不算共享：否则 D1/D2 名存实亡。
    """
    if str(rules_path).startswith("http"):
        return []
    rules_dir = os.path.dirname(os.path.abspath(rules_path))
    root = os.path.dirname(rules_dir) if os.path.basename(rules_dir) == "rules" else rules_dir
    return [os.path.join(root, d) for d in SHARED_DIRS]


def make_is_shared(rules_path, shared_mark=GUARD_REPO_MARK):
    roots = shared_roots(rules_path)

    def is_shared(ref):
        raw = ref.raw if isinstance(ref, Ref) else str(ref)
        if shared_mark in raw:
            return True
        resolved = ref.resolved if isinstance(ref, Ref) else str(ref)
        if str(resolved).startswith("http"):
            return False
        abs_ref = os.path.abspath(resolved)
        return any(abs_ref == root or abs_ref.startswith(root + os.sep) for root in roots)

    return is_shared


def guard_fields_of(data):
    """被引用文件里声明的**护栏字段**集合（兼容平铺基线与模板嵌套两种形态）。"""
    if not isinstance(data, dict):
        return set()
    found = set()
    for path, _ in leaves(data):
        leaf = path.rsplit(".", 1)[-1]
        if leaf in ALL_TURNS_FIELDS + ALL_MODEL_FIELDS:
            found.add(leaf)
    return found


def guard_values_of(data):
    """被引用文件里的**护栏值**（路径 → 值），供"文件自身也过值断言"使用。"""
    out = {"cpus": [], "timeout": [], "turns": [], "models": []}
    if not isinstance(data, dict):
        return out
    for path, val in leaves(data):
        leaf = path.rsplit(".", 1)[-1]
        if leaf == "cpus":
            out["cpus"].append((path, val))
        elif leaf in ALL_TURNS_FIELDS:
            out["turns"].append((path, val))
        elif leaf in ALL_MODEL_FIELDS:
            out["models"].append((path, val))
        elif leaf == "timeout" and ".stages[" in path:
            out["timeout"].append((path, val))
    return out


def assert_guard_values(label, data, rules):
    """同一套值断言：对**任何**被引用文件（基线或模板）都跑一遍。"""
    fails, notes = [], []
    max_cpus = rules["max_cpus"]
    base_timeout = rules["max_stage_timeout"]
    max_turns = rules["max_turns"]
    allow, deny = rules.get("models_allow", []), rules.get("models_deny", [])
    vals = guard_values_of(data)
    for path, val in vals["cpus"]:
        if not isinstance(val, int) or val > max_cpus:
            fails.append(f"{label}{path} = {val!r} 超出组织上限 {max_cpus}"
                         "（按核计费，加核不减时只加钱）")
    for path, val in vals["timeout"]:
        got = minutes(val)
        if got is None or got > minutes(base_timeout):
            fails.append(f"{label}{path} timeout={val!r} 超出基线 {base_timeout}"
                         "（挂死兜底，不是目标值）")
    for path, val in vals["turns"]:
        turns = minutes(val)
        if turns is None:
            fails.append(f"{label}{path} 的回合上限 {val!r} 不是合法数值")
        elif turns > minutes(max_turns):
            fails.append(f"{label}{path} 的回合上限 {val!r} 超出组织上限 {max_turns}"
                         "（基线是全组织共同上界，只能由共享仓 PR 调整）")
    for path, val in vals["models"]:
        if val in deny:
            fails.append(f"{label}{path} 的模型 {val!r} 在黑名单"
                         "（实测被路由到思考档，折算 33×）")
        elif val not in allow:
            fails.append(f"{label}{path} 的模型 {val!r} 不在白名单 {allow}"
                         "（扩展须走共享仓 PR）")
    if not fails and (vals["cpus"] or vals["turns"] or vals["models"]):
        notes.append(f"{label} 值断言通过（cpus {len(vals['cpus'])} / 回合 "
                     f"{len(vals['turns'])} / 模型 {len(vals['models'])} 处）")
    return fails, notes


def check_auth(ref):
    """被引用文件必须自带鉴权声明（跨仓私有引用，否则被权限检查拦下）。

    返回 (问题列表, 声明的 allow_events)。注意：鉴权字段要写在**被引用文件**上，
    写在别的文件上无效——这正是"最常见的接不上原因"。
    """
    problems = []
    try:
        doc = load(ref.resolved)
    except Exception as exc:
        return [f"被引用文件不可读：{ref.raw}（{type(exc).__name__}: {exc}）"], []
    if not isinstance(doc, dict):
        return [f"被引用文件顶层不是对象：{ref.raw}"], []
    if not doc.get("allow_slugs"):
        problems.append(f"{ref.raw}: 缺 allow_slugs（私有且非同源仓库的文件必须显式授权仓库范围）")
    events = doc.get("allow_events") or []
    if isinstance(events, str):
        events = [e.strip() for e in re.split(r"[,|]", events) if e.strip()]
    if not events:
        problems.append(f"{ref.raw}: 缺 allow_events"
                        "（不可信事件引用外部文件时，被引用文件必须显式声明）")
    return problems, events


def cpus_decl_nodes(cfg):
    """值来源判据：**解析结果**里声明了 cpus 的节点，按对象身份去重。

    按源码文本数 `cpus:` 行会被 flow-style（`runner: {cpus: 2}`）绕过；按解析结果数又会被
    YAML 锚点展开重复计数（锚点 + 继承是同一份值）。故按**对象身份**去重：
    锚点复用的多处 runner 是同一个对象（计 1），各角色各写一份则是不同对象（计 N）。
    """
    out = {}

    def walk(node, path=""):
        if isinstance(node, dict):
            if "cpus" in node:
                out.setdefault(id(node), (f"{path}.cpus", node["cpus"]))
            for key, val in node.items():
                walk(val, f"{path}.{key}")
        elif isinstance(node, list):
            for idx, val in enumerate(node):
                walk(val, f"{path}[{idx}]")

    walk(cfg)
    return list(out.values())


def check(config_path, rules_path, shared_mark=GUARD_REPO_MARK, auth_scope="guard"):
    """四层判据全量校验。返回 (fails, notes)。"""
    fails, notes = [], []
    is_shared = make_is_shared(rules_path, shared_mark)
    base_dir = None if str(config_path).startswith("http") else repo_root_of(config_path)
    cfg, rules = load(config_path), load(rules_path)
    if not isinstance(rules, dict) or "max_cpus" not in rules:
        fails.append(f"基线文件缺 max_cpus（--rules 应指向 rules/npc-cost-guard.yml）：{rules_path}")
        return fails, notes
    max_cpus = rules["max_cpus"]
    base_timeout = rules["max_stage_timeout"]
    max_turns = rules["max_turns"]
    allow, deny = rules.get("models_allow", []), rules.get("models_deny", [])

    # 合并后的配置（include 是配置级合并）：路线 B 的 stage 只存在于合并结果里，
    # 故**所有 stage 判定都走 merged**，否则"只 include 模板、不写本地角色 key"会被误判。
    merged_cfg = {}
    if not str(config_path).startswith("http"):
        merged_cfg = resolve_includes(os.path.abspath(config_path))
    effective = merged_cfg if isinstance(merged_cfg, dict) and merged_cfg else cfg
    cfg_stage_paths = {p for p, _ in find_stages(cfg)}

    refs = collect_refs(cfg, base_dir or ".")

    # ① 值来源断言：本地 cpus 声明 ≤1 处（防各角色各写各的）；0 处必须由共享文件提供
    decls = cpus_decl_nodes(cfg)
    merged_cpus = [v for p, v in leaves(effective) if p.endswith(".cpus")]
    if len(decls) > 1:
        fails.append(f"本地 cpus 声明 {len(decls)} 处（应 ≤1 处，防各角色各写各的）："
                     f"{[p for p, _ in decls]}")
    elif len(decls) == 1:
        notes.append(f"值来源：cpus 本地自持（{decls[0][0]} = {decls[0][1]!r}，唯一来源）")
    elif merged_cpus:
        notes.append(f"值来源：cpus 由共享文件提供（本地 0 处声明，合并后 {merged_cpus[0]!r}）")
    else:
        fails.append("本地无 cpus 声明，且 include/imports 也没提供 —— 值来源不可判"
                     "（要么本地锚点内写一处，要么 include 共享模板）")

    # ② 被引用文件：可读性 + 通道↔字段匹配 + 鉴权 + **文件自身也过同一套值断言**
    ref_docs = {}
    for ref in refs:
        try:
            ref_docs[ref] = load(ref.resolved, base_dir)
        except Exception as exc:
            if ref.ignore_error:
                notes.append(f"被引用文件 {ref.raw} 不可读但声明 ignoreError，按平台语义跳过")
                continue
            fails.append(f"基线引用不可读：{ref.raw}（{type(exc).__name__}: {exc}）")
    for ref, data in ref_docs.items():
        # 值断言只跑**护栏提供方**：共享资产包文件，或经 imports/optionsFrom 显式声明的基线。
        # 与护栏无关的本地 include（lint 配置等）不在此列，避免误报（🟡-2）。
        if not (is_shared(ref) or ref.channel in CHANNEL_FIELDS):
            continue
        label = f"被引用文件 {ref.raw}"
        sub_fails, sub_notes = assert_guard_values(f"{label} 的", data, rules)
        fails.extend(sub_fails)
        notes.extend(sub_notes)
        fields = guard_fields_of(data)
        allowed = set(CHANNEL_FIELDS.get(ref.channel, fields))
        if ref.channel in CHANNEL_FIELDS and fields - allowed:
            fails.append(f"{label}：经 {ref.channel} 引入，却声明了该通道喂不到的字段 "
                         f"{sorted(fields - allowed)}"
                         "（imports 只注入 env、optionsFrom 只注入 options，走错通道静默失效）")

    # ②b 鉴权声明：必须落在**被引用文件**上，且覆盖用到的事件
    used_events = set()
    for top, events in cfg.items():
        if isinstance(events, dict):
            used_events |= {e for e in events if isinstance(e, str) and "@npc" in e}
    required = set(rules.get("required_allow_events") or []) | used_events
    for ref in refs:
        data = ref_docs.get(ref)
        if data is None or not audit_scope_ok(ref, data, auth_scope, is_shared):
            continue
        problems, declared = check_auth(ref)
        fails.extend(problems)
        if problems:
            continue
        missing = sorted(e for e in required
                         if e not in declared and not any(
                             re.fullmatch(d.replace("*", ".*"), e) for d in declared))
        if missing:
            fails.append(f"{ref.raw}: allow_events 未覆盖 {missing}"
                         "（PR 类事件属不可信事件，被引用文件必须显式声明；"
                         "漏 push 会让门禁自身引用失败）")
        else:
            notes.append(f"鉴权：{ref.raw} 声明 allow_events={declared}")

    # ②c 重复 stage 判定（include 是"数组追加"，片段带 stages 会与本地的并存）
    if merged_cfg:
        dupes = duplicate_npc_stages(merged_cfg)
        for top, event, hits in dupes:
            fails.append(f"include 合并后 {top}/{event} 出现 {len(hits)} 个 npc go stage "
                         f"{hits}——数组追加会重复执行；引用方勿为模板已覆盖的事件再写 stages")
        if not dupes and any(True for _ in iter_jobs(merged_cfg)):
            notes.append("include 合并：每个事件键下恰好 1 个 npc go stage（无重复执行）")

    # ③ 值断言 + 路线分派 + **通道分派**（字段名与通道都必须与路线一致）
    npc_stages = [(p, s) for p, s in find_stages(effective)
                  if "npc" in str(s.get("name", "")).lower()]
    if not npc_stages:
        fails.append("未找到 npc go stage（--config 应指向本仓 .cnb/npc.yml，"
                     "或 include 的共享模板里应含 npc go stage）")
    for path, st in npc_stages:
        route = route_of(st)
        if route is None:
            fails.append(f"{path}: 既非镜像入口（{MIRROR_ENTRY}）也非 type: npc:go，路线不可判")
            continue
        jpath = path.rsplit(".stages[", 1)[0]
        from_include = path not in cfg_stage_paths
        # 该 stage 的**提供方**：stage / 所在 job 上的 imports|optionsFrom；
        # 来自 include 的 stage（路线 B）另加 include 引用
        raws = set(ref_raws_of(st, job_of(effective, jpath), job_of(cfg, jpath)))
        providers = [r for r in refs
                     if (r.channel in CHANNEL_FIELDS and r.raw in raws)
                     or (from_include and r.channel == "include")]

        turns_field, model_field = ROUTE_FIELDS[route]
        other_route = "npc:go" if route == "mirror" else "mirror"
        env = st.get("env") or {}
        opts = st.get("options") or {}
        # ③a 路线混用：另一条路线的字段出现在本 stage
        for field in ROUTE_FIELDS[other_route]:
            if field in env or field in opts:
                why = ("镜像私有变量，npc:go 不读" if field.startswith("DSH_")
                       else "仅 type: npc:go 生效，镜像不读")
                fails.append(f"{path}: {route} 路线不得混用 {field}（{why}）")
        # ③b 通道分派：本路线字段须经**正确通道**取自**共享仓**（否则静默失效 / 来源不单一）
        ok_channels = ROUTE_CHANNELS[route]
        for field, channel, holder_name in ((turns_field, ok_channels[0],
                                             "env" if route == "mirror" else "options"),):
            holder = env if holder_name == "env" else opts
            if field in holder:
                continue
            declaring = [r for r in providers if field in guard_fields_of(ref_docs.get(r, {}))]
            declared_by = [r for r in declaring if r.channel in ok_channels]
            for r in [x for x in declaring if x.channel not in ok_channels]:
                fails.append(f"{path}: {route} 路线的 {field} 由 {r.raw} 经 {r.channel} 引入，"
                             f"**通道错配**（{r.channel} 只喂 "
                             f"{'env' if r.channel == 'imports' else 'options'}，"
                             f"{holder_name}.{field} 永远拿不到 → 静默失效）")
            shared = [r for r in declared_by if is_shared(r)]
            if not declared_by:
                fails.append(f"{path}: {route} 路线缺 {holder_name}.{field}，"
                             f"且未被任何引用文件经 {'/'.join(ok_channels)} 提供"
                             "（回合上限缺省即失控，必须显式声明）")
            elif not shared:
                fails.append(f"{path}: {route} 路线的 {field} 来自 "
                             f"{[r.raw for r in declared_by]}，**不在共享资产包内**"
                             f"（共享仓 {shared_mark} 或与 --rules 同源）——"
                             "D1/D2 防线要求单一来源，仓内自建基线不算共享（改坏无人评审）")
            else:
                notes.append(f"{path}: {field} 经 {shared[0].channel} 取自共享仓 "
                             f"{shared[0].raw}")
        got = minutes(st.get("timeout"))
        if got is None or got > minutes(base_timeout):
            fails.append(f"{path}: timeout={st.get('timeout')!r}，须声明且 ≤ 基线 {base_timeout}"
                         "（挂死兜底，不是目标值）")
        raw = env.get(turns_field) or opts.get(turns_field)
        if raw is not None:
            turns = minutes(raw)
            if turns is None:
                fails.append(f"{path}: {turns_field}={raw!r} 不是合法数值")
            elif turns > minutes(max_turns):
                fails.append(f"{path}: {turns_field}={raw!r} 超出基线 {max_turns}"
                             "（例外须写实测理由，且只在该角色上放宽）")
            elif turns < minutes(max_turns):
                notes.append(f"{path}: {turns_field}={raw!r} 比基线更紧（允许的向下收紧）")

        # ③c 模型白名单（含"缺点号"黑名单；未声明时须有共享基线兜底）
        model = env.get(MIRROR_MODEL) or opts.get(NPCGO_MODEL)
        if model is None:
            fallback = [r for r in providers if is_shared(r)
                        and guard_fields_of(ref_docs.get(r, {})) & set(ALL_MODEL_FIELDS)]
            if not fallback:
                fails.append(f"{path}: 未声明模型名，且共享仓未提供（缺省档位不可控，"
                             "写错模型名会静默升档）")
            else:
                notes.append(f"{path}: 模型取共享基线默认值（{fallback[0].raw}）")
        elif model in deny:
            fails.append(f"{path}: 模型 {model!r} 在黑名单（实测被路由到思考档，折算 33×）")
        elif model not in allow:
            fails.append(f"{path}: 模型 {model!r} 不在白名单 {allow}（扩展须走共享仓 PR）")

    # ④ 合并后（含共享模板）的核数与 timeout 也过同一套值断言
    for path, val in ((p, v) for p, v in leaves(effective) if p.endswith(".cpus")):
        if not isinstance(val, int) or val > max_cpus:
            fails.append(f"{path} = {val!r} 超出组织上限 {max_cpus}"
                         "（按核计费，加核不减时只加钱）")
    for path, val in ((p, v) for p, v in leaves(effective) if p.endswith(".timeout")):
        got = minutes(val)
        if got is None or got > minutes(base_timeout):
            fails.append(f"{path} timeout={val!r} 超出基线 {base_timeout}")
    return fails, notes


def job_of(cfg, jpath):
    """按路径取 job 节点（`path.rsplit(".stages[", 1)[0]` 的结果）。"""
    for p, job in iter_jobs(cfg):
        if p == jpath:
            return job
    return None


def ref_raws_of(*nodes):
    """取若干节点（stage / 所在 job）上的 imports / optionsFrom 引用原文。

    通道分派的判据来源：字段由**哪个通道**提供，必须与路线匹配。
    """
    out = []
    for node in nodes:
        if not isinstance(node, dict):
            continue
        for channel in ("imports", "optionsFrom"):
            val = node.get(channel)
            for item in (val if isinstance(val, list) else ([val] if val else [])):
                if isinstance(item, str):
                    out.append(item)
    return out


def audit_scope_ok(ref, data, scope, is_shared):
    """鉴权审计范围：默认只审"与护栏有关"的被引用文件，避免无关本地 include 误报。

    * `guard`（默认）：共享资产包文件 ∪ 声明了护栏字段的文件
    * `shared`：仅共享资产包文件；`all`：全部；`none`：不审
    """
    if scope == "none":
        return False
    if scope == "all":
        return True
    if scope == "shared":
        return is_shared(ref)
    return is_shared(ref) or bool(guard_fields_of(data))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", required=True, help="引用方 .cnb/npc.yml（路径或 URL）")
    ap.add_argument("--rules", required=True, help="共享仓 rules/npc-cost-guard.yml（路径或 URL）")
    ap.add_argument("--shared-mark", default=GUARD_REPO_MARK,
                    help=f"共享仓标识（默认 {GUARD_REPO_MARK}）：值来源须指向它")
    ap.add_argument("--auth-scope", default="guard", choices=("guard", "shared", "all", "none"),
                    help="鉴权审计范围（默认 guard：只审共享仓文件与声明护栏字段的文件）")
    args = ap.parse_args()
    fails, notes = check(args.config, args.rules, args.shared_mark, args.auth_scope)
    for line in notes:
        print(f"[note] {line}")
    for line in fails:
        print(f"[FAIL] {line}")
    if fails:
        print(f"\n断言失败 {len(fails)} 项 —— 门禁不通过")
        return 1
    print("\n成本护栏校验通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
