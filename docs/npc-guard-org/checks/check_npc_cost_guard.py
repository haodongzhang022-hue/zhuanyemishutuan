#!/usr/bin/env python3
"""组织级 NPC 成本护栏校验（共享仓 npc-guard/checks/ 维护，引用方 CI 调用）。

三层判据：值断言 / **值来源断言** / **路线分派**（镜像路线与 npc:go 路线不可混用）。

两条路线的字段名**不同且不可互换**，因此校验按 stage 入口自动分派：

* 镜像路线（`script: sh /app/start.sh`）：回合上限字段 = `DSH_MAX_STEPS`（镜像私有变量）
* npc:go 路线（`type: npc:go`）：回合上限字段 = `options.maxTurns`（官方内置）

退出码：0 通过；1 断言失败；2 依赖或入参错误（大声失败，不静默跳过）。
"""
import argparse
import os
import re
import sys
import urllib.request

try:
    import yaml
except ImportError:  # 依赖必须显式声明，缺 yaml 时以 rc=2 大声失败
    sys.stderr.write("FATAL: 需要 pyyaml（引用方 CI 镜像请预装或 pip install pyyaml）\n")
    sys.exit(2)

MIRROR_ENTRY = "sh /app/start.sh"   # 镜像路线入口
MIRROR_FIELD = "DSH_MAX_STEPS"      # 镜像路线回合上限字段名
NPCGO_FIELD = "maxTurns"            # npc:go 路线回合上限字段名
GUARD_REPO_MARK = "npc-guard"       # 共享仓标识（值来源可追溯）


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


def find_stages(node, path=""):
    """遍历所有 stage。

    **跳过锚点定义**（顶层 key 以 `.` 开头，如 `.npc` / `.base-stage`）：
    它们不是可执行的 stage，只是给角色 `<<` 复用的模板；模型名等角色级字段
    由引用它的角色段声明，对锚点断言必然误报。
    """
    if isinstance(node, dict):
        for key, val in node.items():
            if key == "stages" and isinstance(val, list):
                for idx, st in enumerate(val):
                    if isinstance(st, dict):
                        yield f"{path}.stages[{idx}]", st
            else:
                if path == "" and str(key).startswith("."):
                    continue
                yield from find_stages(val, f"{path}.{key}")
    elif isinstance(node, list):
        for idx, val in enumerate(node):
            yield from find_stages(val, f"{path}[{idx}]")


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
            if item.get("config"):
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


def referenced_files(cfg, base_dir):
    """所有被引用的**外部文件**（include / imports / optionsFrom），按顺序去重。"""
    refs = []
    for path, val in leaves(cfg):
        if ".imports" in path or ".optionsFrom" in path:
            refs.append(val)
    for item in (cfg.get("include") or []):
        if isinstance(item, str):
            refs.append(item)
        elif isinstance(item, dict) and item.get("path"):
            refs.append(item["path"])
    out = []
    for ref in refs:
        if not isinstance(ref, str):
            continue
        resolved = ref if ref.startswith("http") else os.path.join(base_dir, ref)
        if resolved not in out:
            out.append(resolved)
    return out


def check_auth(path):
    """被引用文件必须自带鉴权声明（跨仓私有引用，否则被权限检查拦下）。

    返回 (问题列表, 声明的 allow_events)。注意：鉴权字段要写在**被引用文件**上，
    写在别的文件上无效——这正是"最常见的接不上原因"。
    """
    problems = []
    try:
        doc = load(path)
    except Exception as exc:
        return [f"被引用文件不可读：{exc}"], []
    if not isinstance(doc, dict):
        return [f"被引用文件顶层不是对象：{path}"], []
    slugs = doc.get("allow_slugs")
    if not slugs:
        problems.append(f"{path}: 缺 allow_slugs（私有且非同源仓库的文件必须显式授权仓库范围）")
    events = doc.get("allow_events") or []
    if isinstance(events, str):
        events = [e.strip() for e in re.split(r"[,|]", events) if e.strip()]
    if not events:
        problems.append(f"{path}: 缺 allow_events（不可信事件引用外部文件时，被引用文件必须显式声明）")
    return problems, events


def count_cpus_decls(source):
    """值来源判据（include 场景重写版）：按**源码声明**计数，不按解析后的展开值。

    YAML 锚点展开会让解析结果里出现多份 cpus（锚点 + 继承），那不是"各写各的"。
    判据因此落在源码：本地 `cpus:` 声明必须 **0 或 1 处**。
    """
    hits = []
    for line in source.splitlines():
        stripped = line.strip()
        if not stripped.startswith("cpus:") or stripped.startswith("#"):
            continue
        hits.append(re.split(r"\s+#", stripped, maxsplit=1)[0].strip())  # 去掉行尾注释
    return hits


def check(config_path, rules_path):
    fails, notes = [], []
    base_dir = None if str(config_path).startswith("http") else repo_root_of(config_path)
    cfg, rules = load(config_path), load(rules_path)
    source = config_path
    if not str(config_path).startswith("http"):
        with open(config_path, encoding="utf-8") as fh:
            source = fh.read()
    if not isinstance(rules, dict) or "max_cpus" not in rules:
        fails.append(f"基线文件缺 max_cpus（--rules 应指向 rules/npc-cost-guard.yml）：{rules_path}")
        return fails, notes
    merged_cfg = {}
    if not str(config_path).startswith("http"):
        merged_cfg = resolve_includes(os.path.abspath(config_path))
    max_cpus = rules["max_cpus"]
    base_timeout = rules["max_stage_timeout"]
    max_turns = rules["max_turns"]
    allow, deny = rules.get("models_allow", []), rules.get("models_deny", [])

    # ① 值来源断言（include 场景重写版）：按**源码声明**计数，不按解析后的展开值。
    #    YAML 锚点展开会让解析结果里出现多份 cpus（锚点 + 继承），那不是"各写各的"。
    #    三种合法形态，判定顺序：本地声明 >1 处直接失败；
    #    本地 1 处 = 自持（锚点内唯一来源）；本地 0 处 = 必须由 include/imports 提供。
    decls = count_cpus_decls(source) if isinstance(source, str) else []
    for path, val in ((p, v) for p, v in leaves(cfg) if p.endswith(".runner.cpus")):
        if not isinstance(val, int) or val > max_cpus:
            fails.append(f"{path} = {val!r} 超出组织上限 {max_cpus}（按核计费，加核不减时只加钱）")

    # ② 值来源断言：可注入型基线值必须可追溯到共享仓（imports 支持跨仓 + 鉴权）
    # 值来源引用：imports / optionsFrom（引用方显式写）+ include（合并进来的基线字段）
    refs = [v for p, v in leaves(cfg) if ".imports" in p or ".optionsFrom" in p]
    if not str(config_path).startswith("http"):
        for item in (cfg.get("include") or []):
            refs.append(item.get("path") if isinstance(item, dict) else item)
    shared_ref = any(GUARD_REPO_MARK in str(r) for r in refs)
    # 基线文件实际声明了什么：值来源断言必须落到"字段真的在基线里"，否则只是"看着像"
    # （生产用法是共享仓 URL；本地/离线场景用相对路径指向同一份基线文件，等价成立）
    baseline_fields, unreadable = set(), []
    for ref in refs:
        try:
            data = load(ref, base_dir)
        except Exception as exc:  # 读不到即大声失败，不静默放过
            unreadable.append(f"{ref}（{type(exc).__name__}: {exc}）")
            continue
        if isinstance(data, dict):
            baseline_fields |= {k for k in data if isinstance(k, str)}
    if unreadable:
        fails.append(f"基线引用不可读：{unreadable}")
    traceable = bool(baseline_fields & {MIRROR_FIELD, NPCGO_FIELD})
    mode = "shared" if traceable else "local"
    merged_cpus = [v for p, v in leaves(merged_cfg) if p.endswith(".runner.cpus")] if merged_cfg else []
    if len(decls) > 1:
        fails.append(f"本地 cpus 声明 {len(decls)} 处（应 ≤1 处，防各角色各写各的）：{decls}")
    elif len(decls) == 1:
        notes.append(f"值来源：cpus 本地自持（{decls[0]}，唯一来源）")
    elif merged_cpus:
        notes.append("值来源：cpus 由 include 的共享模板提供，本地 0 处声明")
    else:
        fails.append("本地无 cpus 声明，且 include/imports 也没提供 —— 值来源不可判"
                     "（要么本地锚点内写一处，要么 include 共享模板）")
    if mode == "shared":
        notes.append(f"值来源：{len(refs)} 处基线引用，字段 {sorted(baseline_fields)} 可读")

    # ②b 基线自身也要过同一套值断言——否则"共享值被改坏"是校验盲区
    #     （变异 M3/M10：基线里模型名去点、回合上限放宽，引用方却依然全绿）
    for ref in refs:
        try:
            data = load(ref, base_dir)
        except Exception:
            continue  # 不可读已在 ② 报 FAIL
        if not isinstance(data, dict):
            continue
        for label, raw in ((MIRROR_FIELD, data.get(MIRROR_FIELD)),
                           (NPCGO_FIELD, data.get(NPCGO_FIELD))):
            if raw is None:
                continue
            turns = minutes(raw)
            if turns is None:
                fails.append(f"基线 {ref} 的 {label}={raw!r} 不是合法数值")
            elif turns > minutes(max_turns):
                fails.append(f"基线 {ref} 的 {label}={raw!r} 超出组织上限 {max_turns}"
                             "（基线是全组织共同上界，只能由共享仓 PR 调整）")
        model = data.get("DSH_MODEL") or data.get("model")
        if model in deny:
            fails.append(f"基线 {ref} 的模型 {model!r} 在黑名单（实测被路由到思考档，33×）")
        elif model is not None and model not in allow:
            fails.append(f"基线 {ref} 的模型 {model!r} 不在白名单 {allow}")

    # ②c 重复 stage 判定（include 是"数组追加"，片段带 stages 会与本地的并存）
    if merged_cfg:
        dupes = duplicate_npc_stages(merged_cfg)
        for top, event, hits in dupes:
            fails.append(f"include 合并后 {top}/{event} 出现 {len(hits)} 个 npc go stage "
                         f"{hits}——数组追加会重复执行；引用方勿为模板已覆盖的事件再写 stages")
        if not dupes and any("stages" in str(j) for j in [merged_cfg]):
            notes.append("include 合并：每个事件键下恰好 1 个 npc go stage（无重复执行）")

    # ②d 鉴权声明（🔴-2）：必须落在**被引用文件**上，且覆盖用到的事件
    used_events = set()
    for top, events in cfg.items():
        if isinstance(events, dict):
            used_events |= {e for e in events if isinstance(e, str) and "@npc" in e}
    for ref in referenced_files(cfg, base_dir or "."):
        if str(ref).startswith("http") and GUARD_REPO_MARK not in str(ref):
            continue  # 只审计共享护栏文件的鉴权（其余引用不在本门禁职责内）
        problems, declared = check_auth(ref)
        fails.extend(problems)
        if not problems:
            # 组织标准事件集（rules.required_allow_events）∪ 本仓实际用到的事件
            required = set(rules.get("required_allow_events") or []) | used_events
            missing = sorted(e for e in required
                             if e not in declared and not any(
                                 re.fullmatch(d.replace("*", ".*"), e) for d in declared))
            if missing:
                fails.append(f"{ref}: allow_events 未覆盖 {missing}"
                             "（PR 类事件属不可信事件，被引用文件必须显式声明；"
                             "漏 push 会让门禁自身引用失败）")
        if not problems:
            notes.append(f"鉴权：{ref} 声明 allow_events={declared}")

    # ③ 值断言 + 路线分派：字段名必须与路线一致，混用即失败
    npc_stages = [(p, s) for p, s in find_stages(cfg) if "npc" in str(s.get("name", "")).lower()]
    if not npc_stages:
        fails.append("未找到 npc go stage（--config 应指向本仓 .cnb/npc.yml）")
    for path, st in npc_stages:
        route = route_of(st)
        if route is None:
            fails.append(f"{path}: 既非镜像入口（{MIRROR_ENTRY}）也非 type: npc:go，路线不可判")
            continue
        got = minutes(st.get("timeout"))
        if got is None or got > minutes(base_timeout):
            fails.append(f"{path}: timeout={st.get('timeout')!r}，须声明且 ≤ 基线 {base_timeout}"
                         "（挂死兜底，不是目标值）")
        env = st.get("env") or {}
        opts = st.get("options") or {}
        if route == "mirror":
            if MIRROR_FIELD not in env and MIRROR_FIELD not in baseline_fields:
                fails.append(f"{path}: 镜像路线缺 {MIRROR_FIELD}，且共享基线里也没有该字段"
                             "（回合上限缺省即失控，必须显式声明）")
            if NPCGO_FIELD in opts:
                fails.append(f"{path}: 镜像路线不得混用 options.{NPCGO_FIELD}"
                             "（仅 type: npc:go 生效，写了会静默失效）")
        else:
            # npc:go 路线的基线必须走 options / optionsFrom（imports 只注入 env，喂不到 options）
            opts_from = st.get("optionsFrom") or []
            if isinstance(opts_from, str):
                opts_from = [opts_from]
            opts_shared = any(GUARD_REPO_MARK in str(o) for o in opts_from)
            if NPCGO_FIELD not in opts and NPCGO_FIELD not in baseline_fields:
                fails.append(f"{path}: npc:go 路线缺 options.{NPCGO_FIELD}，"
                             f"且 optionsFrom 未指向共享仓 {GUARD_REPO_MARK}"
                             "（imports 只注入 env，喂不到 options）")
            if MIRROR_FIELD in env:
                fails.append(f"{path}: npc:go 路线不得混用 {MIRROR_FIELD}"
                             "（镜像私有变量，npc:go 不读）")
        for label, raw in ((MIRROR_FIELD, env.get(MIRROR_FIELD)),
                           (NPCGO_FIELD, opts.get(NPCGO_FIELD))):
            if raw is None:
                continue
            turns = minutes(raw)
            if turns is None:
                fails.append(f"{path}: {label}={raw!r} 不是合法数值")
            elif turns > minutes(max_turns):
                fails.append(f"{path}: {label}={raw!r} 超出基线 {max_turns}"
                             "（例外须写实测理由，且只在该角色上放宽）")
            elif turns < minutes(max_turns):
                notes.append(f"{path}: {label}={raw!r} 比基线更紧（允许的向下收紧）")

        # ④ 模型白名单（含"缺点号"黑名单；未声明时须有共享基线兜底）
        model = env.get("DSH_MODEL") or opts.get("model")
        if model is None:
            if not (baseline_fields & {"DSH_MODEL", "model"}):
                fails.append(f"{path}: 未声明模型名，基线也未提供（缺省档位不可控，"
                             "写错模型名会静默升档）")
            else:
                notes.append(f"{path}: 模型取共享基线默认值")
        elif model in deny:
            fails.append(f"{path}: 模型 {model!r} 在黑名单（实测被路由到思考档，折算 33×）")
        elif model not in allow:
            fails.append(f"{path}: 模型 {model!r} 不在白名单 {allow}（扩展须走共享仓 PR）")
    return fails, notes


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", required=True, help="引用方 .cnb/npc.yml")
    ap.add_argument("--rules", required=True, help="共享仓 rules/npc-cost-guard.yml（路径或 URL）")
    args = ap.parse_args()
    fails, notes = check(args.config, args.rules)
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
