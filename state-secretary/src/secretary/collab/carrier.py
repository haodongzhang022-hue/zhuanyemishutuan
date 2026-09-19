"""判重台账的**可持久载体**（评论正文）——**生产侧接线**（🔴-3 复评第 5 轮）

## 为什么需要这一层

`RunScopeGuard.trail()/trails()` 只**渲染**留痕；`adopt_trails()` 只**解析**留痕。
但"把留痕从评论里取出来 / 把新留痕写回同一条评论"这两步此前**没有任何生产者**：

- `CNB_RUN_SCOPE_TRAIL` / `CNB_SESSION_ID` 全库只有**读**（`ci-incremental-test.sh`），
  `.cnb.yml` 的 `main.push` 段**没有 `env`/`imports`** → 生产链路上两个变量都无来源；
- 于是 CI 跑完 `guard.trails(session_id="")` 打印的留痕，写回评论时会把
  **所有会话**的归属整段重写成 `""` → 别的会话的台账被本会话继承（**漏跑**）、
  "红→修→再跑"被自己的判重堵死。

本模块把这两步落成**可执行的生产者**（`fetch` / `writeback`），并给出
**会话号的确定性来源**（同一 PR/Issue = 同一会话，跨容器稳定）：

| 步骤 | 做什么 | 为什么必须由流水线做 |
|---|---|---|
| `fetch` | 按载体定位符取回评论正文 → 写 `CNB_RUN_SCOPE_TRAIL_FILE` | 平台每次 push = 新容器，本地台账从空开始；**评论不会**被回收 |
| `writeback` | 把本次新留痕**追加**到同一条评论（保留旧留痕） | 不写回 → 下一棒又归零（"软约束换了个位置"） |

## 归属口径（与 🔴-3 一致）

- 会话号取 `CNB_SESSION_ID`；缺省由 `CNB_REPO_SLUG` + PR/Issue 号**推导**
  （同一 PR/Issue 同一会话，跨容器稳定）；
- **推导不出（无 PR/Issue 上下文，如主干 push）→ 不写回**（并告警）：
  宁可这次不落载体，也不把别人的归属抹成空串（那是漏跑方向）。
- 评论正文合并**只增不减**：新留痕追加，旧留痕原样保留（解析侧 `max()` + 归属过滤）。

## 用法（`.cnb.yml` 的 `main.push` 段）

    - ./scripts/collab-carrier.sh fetch      # 跑前：恢复台账
    - ./scripts/ci-dispatch.sh               # 跑增量
    - ./scripts/collab-carrier.sh writeback  # 跑后：追加写回同一条评论

只依赖标准库（`urllib`），不引入新依赖。
"""
from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.request
from typing import Any, Callable, Dict, List, Optional, Tuple

#: 载体留痕标记（与 `run_guard.RUN_SCOPE_TRAIL_MARKER` **同源**）
from .run_guard import RUN_SCOPE_TRAIL_MARKER

#: 载体评论的**识别标记**：正文里带该 HTML 注释即视为本机制的载体评论。
#: 用它把"载体评论"与普通讨论评论区分开（避免把讨论评论当载体反复改写）。
CARRIER_MARK = "<!-- collab-run-scope-carrier -->"

#: 默认文件（阶段之间用文件传递，不用 export——每个 stage 是独立 shell）
DEFAULT_TRAIL_FILE = "/tmp/cnb-run-scope-trail.txt"
DEFAULT_OUT_FILE = "/tmp/cnb-run-scope-new-trail.txt"

#: 留痕行正则（取回正文时只认这些行，避免把无关内容当台账）
_TRAIL_LINE_RE = re.compile(
    r'<!--\s*' + re.escape(RUN_SCOPE_TRAIL_MARKER) + r':\s*\{.*?\}\s*-->', re.S)


# ----------------------------------------------------------------------
# 纯函数：会话号 / 定位符 / 正文合并（可单测，不碰网络）
# ----------------------------------------------------------------------

def session_id_for(env: Optional[Dict[str, str]] = None) -> str:
    """本次执行的**会话号**（判重台账的归属）。

    取值优先级：

    1. 显式 `CNB_SESSION_ID`（运维/上游给了就用，尊重显式口径）；
    2. `CNB_REPO_SLUG` + PR 号 / Issue 号（**推导**：同一 PR/Issue = 同一会话，
       跨容器稳定——这正是"归属"要的语义）；
    3. 都没有 → ``""``（**不冒充**：见 `writeback()`，空归属一律不写回）。

    旧实现全库**没有生产者** → 归属恒为空串 → 写回时把别人归属整段抹平（🔴-3）。
    """
    env = os.environ if env is None else env
    explicit = (env.get("CNB_SESSION_ID") or "").strip()
    if explicit:
        return explicit
    repo = (env.get("CNB_REPO_SLUG") or env.get("CNB_REPO_SLUG_LOWERCASE") or "").strip()
    if not repo:
        return ""
    for key in ("CNB_PULL_REQUEST_IID", "CNB_ISSUE_IID", "CNB_PULL_REQUEST_ID"):
        num = (env.get(key) or "").strip()
        if num and num != "0":
            return f"{repo}#{num}"
    return ""


def carrier_target(env: Optional[Dict[str, str]] = None) -> Optional[Dict[str, str]]:
    """载体**定位符**：评论挂在哪个 PR/Issue 上（取不到 → ``None``）。

    显式 `CNB_COMMENT_ID`（评论触发）优先——那就是"同一条评论"本身。
    """
    env = os.environ if env is None else env
    repo = (env.get("CNB_REPO_SLUG") or env.get("CNB_REPO_SLUG_LOWERCASE") or "").strip()
    if not repo:
        return None
    comment_id = (env.get("CNB_COMMENT_ID") or "").strip()
    for kind, key in (("pull", "CNB_PULL_REQUEST_IID"), ("issue", "CNB_ISSUE_IID")):
        num = (env.get(key) or "").strip()
        if num and num != "0":
            out = {"repo": repo, "kind": kind, "number": num}
            if comment_id:
                out["comment_id"] = comment_id
            return out
    if comment_id:
        # 有评论号但不知挂在哪个对象上：仍可按评论号定位（调用方按 `note` 处理）
        return {"repo": repo, "kind": "note", "number": "", "comment_id": comment_id}
    return None


def merge_carrier_body(old: str, new_lines: str) -> str:
    """把新留痕**追加**到载体正文（旧留痕原样保留，重复行不叠加）。

    - 旧正文里的留痕行**按原样保留**（含各自的 `session_id` 归属）——
      这是 🔴-3 的核心：写回**不得**改写别人的归属；
    - 新留痕行去掉与旧正文重复的那些（同一条留痕反复追加会让评论线性膨胀）；
    - 旧正文没有载体标记 → 补上（首行），便于下一棒识别。
    """
    old = old if isinstance(old, str) else ""
    new_lines = new_lines if isinstance(new_lines, str) else ""
    seen = set(_TRAIL_LINE_RE.findall(old))
    add: List[str] = []
    for line in new_lines.splitlines():
        line = line.strip()
        if not line:
            continue
        if line in seen:
            continue
        seen.add(line)
        add.append(line)
    body = old.rstrip()
    if CARRIER_MARK not in body:
        body = (CARRIER_MARK + "\n" + body).strip()
    if not add:
        return body + "\n" if body else ""
    return (body + "\n" + "\n".join(add) + "\n") if body else ("\n".join(add) + "\n")


def trail_lines_of(body: Any) -> str:
    """从评论正文里取出**留痕行**（其余内容不带回，避免把讨论正文当台账）。"""
    if not isinstance(body, str):
        return ""
    return "\n".join(_TRAIL_LINE_RE.findall(body))


# ----------------------------------------------------------------------
# 网络侧（transport 可注入：单测不碰网络）
# ----------------------------------------------------------------------

Transport = Callable[[str, str, Dict[str, str], Optional[bytes]],
                     Tuple[int, str]]


def _urllib_transport(method: str, url: str, headers: Dict[str, str],
                      body: Optional[bytes]) -> Tuple[int, str]:
    req = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:   # noqa: S310
            return int(resp.status), resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:                       # pragma: no cover
        return int(exc.code), exc.read().decode("utf-8", "replace")


def _api_base(env: Dict[str, str]) -> str:
    return (env.get("CNB_API_ENDPOINT") or "https://api.cnb.cool").rstrip("/")


def _headers(env: Dict[str, str]) -> Dict[str, str]:
    token = env.get("CNB_TOKEN") or ""
    return {
        "Authorization": "Bearer " + token,
        "Accept": "application/vnd.cnb.api+json",
        "Content-Type": "application/json",
    }


def _comments_url(target: Dict[str, str], env: Dict[str, str],
                  comment_id: str = "") -> str:
    base = _api_base(env)
    repo = target["repo"]
    if target.get("kind") == "pull":
        path = f"/{repo}/-/pulls/{target['number']}/comments"
    elif target.get("kind") == "issue":
        path = f"/{repo}/-/issues/{target['number']}/comments"
    else:
        # 只有评论号：走"取单条评论"的通用入口（PR 侧优先）
        path = f"/{repo}/-/pulls/0/comments"
    return base + path + (f"/{comment_id}" if comment_id else "")


def find_carrier_comment(target: Dict[str, str], env: Dict[str, str],
                         transport: Optional[Transport] = None) -> Optional[str]:
    """在目标 PR/Issue 里找**载体评论**（带标记的最近一条）→ 返回其 id。"""
    tr = transport or _urllib_transport
    if target.get("comment_id"):
        return target["comment_id"]
    url = _comments_url(target, env) + "?page=1&page_size=100"
    status, text = tr("GET", url, _headers(env), None)
    if status != 200:
        return None
    try:
        rows = json.loads(text)
    except ValueError:
        return None
    if not isinstance(rows, list):
        return None
    for row in rows:
        if not isinstance(row, dict):
            continue
        body = row.get("body") or ""
        if CARRIER_MARK in body or RUN_SCOPE_TRAIL_MARKER in body:
            cid = row.get("id")
            if cid:
                return str(cid)
    return None


def run_fetch(env: Optional[Dict[str, str]] = None,
              transport: Optional[Transport] = None) -> Dict[str, Any]:
    """**跑前**：把载体评论正文落到 `CNB_RUN_SCOPE_TRAIL_FILE`。

    取不到（无 PR/Issue 上下文 / 无载体评论 / API 失败）→ 写空文件并告警，
    **不阻塞干活**（判重判不出时方向是保守放行，见 `RunScopeGuard.decide`）。
    """
    env = dict(os.environ if env is None else env)
    out_file = env.get("CNB_RUN_SCOPE_TRAIL_FILE") or DEFAULT_TRAIL_FILE
    target = carrier_target(env)
    sid = session_id_for(env)
    if target is None:
        _write(out_file, "")
        return {"ok": False, "reason": "无 PR/Issue 上下文 → 本次无载体（不误拦）",
                "session_id": sid, "file": out_file}
    cid = find_carrier_comment(target, env, transport)
    if not cid:
        _write(out_file, "")
        return {"ok": False, "reason": "未找到载体评论 → 本次按无历史处理",
                "session_id": sid, "file": out_file}
    tr = transport or _urllib_transport
    status, text = tr("GET", _comments_url(target, env, cid), _headers(env), None)
    if status != 200:
        _write(out_file, "")
        return {"ok": False, "reason": f"取载体失败（HTTP {status}）",
                "session_id": sid, "file": out_file}
    try:
        body = json.loads(text).get("body") or ""
    except (ValueError, AttributeError):
        body = ""
    lines = trail_lines_of(body)
    _write(out_file, lines)
    return {"ok": True, "reason": "已取回载体留痕",
            "session_id": sid, "file": out_file,
            "comment_id": cid, "lines": len(lines.splitlines())}


def run_writeback(env: Optional[Dict[str, str]] = None,
                  transport: Optional[Transport] = None) -> Dict[str, Any]:
    """**跑后**：把本次新留痕**追加**到同一条载体评论（保留旧留痕与各自归属）。

    **归属为空一律不写回**（🔴-3）：宁可这次不落载体，也不把别人的归属抹成空串
    （那是"别的会话台账被继承 → 漏跑"的方向）。
    """
    env = dict(os.environ if env is None else env)
    in_file = env.get("CNB_RUN_SCOPE_NEW_TRAIL_FILE") or DEFAULT_OUT_FILE
    sid = session_id_for(env)
    new_lines = _read(in_file)
    if not new_lines.strip():
        return {"ok": False, "reason": "本次无新留痕 → 不写回", "session_id": sid}
    if not sid:
        return {"ok": False,
                "reason": "**归属为空**（无 CNB_SESSION_ID 且推导不出）→ "
                          "不写回（否则会把别的会话归属抹平）",
                "session_id": sid}
    target = carrier_target(env)
    if target is None:
        return {"ok": False, "reason": "无 PR/Issue 上下文 → 无处写回",
                "session_id": sid}
    tr = transport or _urllib_transport
    cid = find_carrier_comment(target, env, transport)
    old = ""
    if cid:
        status, text = tr("GET", _comments_url(target, env, cid),
                          _headers(env), None)
        if status == 200:
            try:
                old = json.loads(text).get("body") or ""
            except (ValueError, AttributeError):
                old = ""
    merged = merge_carrier_body(old, new_lines)
    payload = json.dumps({"body": merged}).encode("utf-8")
    if cid:
        status, _ = tr("PATCH", _comments_url(target, env, cid),
                       _headers(env), payload)
        action = "patch"
    else:
        status, _ = tr("POST", _comments_url(target, env), _headers(env), payload)
        action = "post"
    return {"ok": status in (200, 201, 204), "action": action,
            "reason": f"写回载体（HTTP {status}）", "session_id": sid,
            "comment_id": cid or ""}


def _write(path: str, text: str) -> None:
    try:
        with open(path, "w", encoding="utf-8") as f:
            f.write(text)
    except OSError:                     # pragma: no cover - 落盘失败不阻塞
        pass


def _read(path: str) -> str:
    try:
        with open(path, encoding="utf-8") as f:
            return f.read()
    except OSError:
        return ""
