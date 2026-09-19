"""NPC 成本护栏回归（Issue #24）

把 `.cnb/npc.yml` 的**成本关键配置**变成可复跑断言，避免下一棒顺手改回高消耗档。

判据来源（全部为平台实测，非推测）：

* 190 次 NPC 构建（issue/PR `@npc` 事件）= **93.0 机时 / 744 核时**（runner 8 核）。
  单次峰值：258 回合 / 45 min / 8 核 = **6.0 核时**。
* `cpus` 按核计费；NPC 墙钟由模型往返主导（3.3–10.6 s/回合，工具以纯文本为主），
  给 8 核与给 2 核墙钟几乎不变，核时开销差 4×。
* `DSH_MODEL: deepseek-v4-flash`（**缺点号**）被路由到思考模型：161 次请求
  58 770 毫度 / 260 次 13 360 毫度 = **33×**，单请求 response_start 中位 7.7 s（对照 1.5 s）。

断言分两层，缺一不可：

* **值断言**（防配置被改坏）；
* **同源性断言**（防"改了实现没改声明"——本仓库反复治的形态）：
  注释里的实测数字必须与断言里的常量一致。
"""

import os
import re

import pytest

yaml = pytest.importorskip("yaml")

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
NPC_YML = os.path.join(REPO_ROOT, ".cnb", "npc.yml")

#: 成本护栏常量（改这里必须同步改 `npc.yml` 注释里的实测口径）
MAX_CPUS = 2
MAX_STAGE_TIMEOUT = "2h"
CANONICAL_MODEL = "deepseek-v4.1-flash"


@pytest.fixture(scope="module")
def npc_source() -> str:
    with open(NPC_YML, encoding="utf-8") as f:
        return f.read()


@pytest.fixture(scope="module")
def npc_doc(npc_source):
    return yaml.safe_load(npc_source)


def _iter_stages(doc):
    """遍历所有角色事件下的 stage 定义（跳过非角色顶层键与注释字段）。"""
    for role, events in doc.items():
        if not isinstance(events, dict):
            continue
        for event, jobs in events.items():
            for job in jobs if isinstance(jobs, list) else [jobs]:
                if not isinstance(job, dict):
                    continue
                for st in job.get("stages") or []:
                    if isinstance(st, dict):
                        yield role, event, st


def _iter_jobs(doc):
    for role, events in doc.items():
        if not isinstance(events, dict):
            continue
        for event, jobs in events.items():
            for job in jobs if isinstance(jobs, list) else [jobs]:
                if isinstance(job, dict):
                    yield role, event, job

class TestRunnerSizing:
    """核数是**按核计费**的成本主变量，不是性能旋钮。"""

    def test_shared_runtime_anchor_is_low_core(self, npc_doc):
        base = npc_doc[".npc-job-base"]
        assert base["runner"]["cpus"] <= MAX_CPUS, (
            f"共享运行时锚点 cpus={base['runner']['cpus']} > {MAX_CPUS}；"
            "NPC 墙钟由模型往返主导，加核不减时只加钱"
        )

    def test_no_role_overrides_core_count(self, npc_source):
        """角色层不得**单独**提核数——一处提核 = 7 角色全提。

        这里查「源码里有没有在角色段写死 cpus」，不查解析后的值：解析后的值
        必然带锚点继承的 2，那不是覆盖。判据落在**值来源**上。
        """
        offenders = []
        for line in npc_source.splitlines():
            stripped = line.strip()
            if stripped.startswith("#") or not stripped.startswith("cpus:"):
                continue
            value = stripped.split(":", 1)[1].strip().strip('"')
            if value.isdigit() and int(value) > MAX_CPUS:
                offenders.append(stripped)
        assert offenders == [], f"出现高核数配置（成本翻倍）：{offenders}"

    def test_anchor_is_the_single_source_of_core_count(self, npc_source):
        """`cpus:` 只允许出现在共享锚点一处，防止下游各写各的。"""
        hits = [
            line.strip() for line in npc_source.splitlines()
            if line.strip().startswith("cpus:") and not line.strip().startswith("#")
        ]
        assert len(hits) == 1, f"`cpus` 应只有一处（共享锚点），实际 {hits}"


class TestStageTimeout:
    """10 h 是挂死兜底，不是目标值；按 2 核计，10 h 单点风险 = 20 核时。"""

    def test_timeout_is_bounded(self, npc_doc):
        """锚点 `.base-stage`（被 `<<` 合并进各 stage）自带 timeout。"""
        base = npc_doc[".base-stage"]
        assert base.get("timeout") == MAX_STAGE_TIMEOUT, (
            f".base-stage timeout={base.get('timeout')}，期望 {MAX_STAGE_TIMEOUT}"
        )

    def test_every_stage_has_a_timeout(self, npc_doc):
        """镜像已删除进程级超时 → 漏配 stage timeout 即无墙钟防线。"""
        missing = [
            f"{role}/{event}/{st.get('name')}"
            for role, event, st in _iter_stages(npc_doc)
            if "timeout" not in st
        ]
        assert missing == [], f"缺 stage timeout：{missing}"


class TestModelRouting:
    """模型名必须写**带点**的规范名，否则被路由到思考档（实测 33× 折算）。"""

    def test_all_roles_use_canonical_model_name(self, npc_doc):
        seen = []
        for role, _event, st in _iter_stages(npc_doc):
            model = (st.get("env") or {}).get("DSH_MODEL")
            if model:
                seen.append((role, model))
        assert seen, "未找到任何 DSH_MODEL，防线失效"
        bad = [(r, m) for r, m in seen if m != CANONICAL_MODEL]
        assert bad == [], f"非规范模型名（会被路由到思考档）：{bad}"

    def test_model_name_not_the_dotless_variant(self, npc_source):
        """`deepseek-v4-flash` 出现在**可执行配置位**即失败（注释里作为反例可以）。"""
        for line in npc_source.splitlines():
            stripped = line.strip()
            if stripped.startswith("#"):
                continue
            assert "deepseek-v4-flash" not in stripped, (
                f"可执行配置位出现缺点号模型名：{stripped}"
            )


class TestDeclarationMatchesImplementation:
    """注释里的实测数字必须与断言常量同源（防口径漂移）。"""

    def test_core_count_rationale_is_documented(self, npc_source):
        assert "按核计费" in npc_source, "cpus 的成本依据（按核计费）未写入配置注释"

    def test_measured_baseline_is_present(self, npc_source):
        """把实测基数写进注释，下一棒才知道这些值是量出来的。"""
        for token in ("744 核时", "258 回合", "33×"):
            assert token in npc_source, f"配置注释缺少实测口径：{token}"

    def test_declared_timeout_matches_constant(self, npc_source):
        assert re.search(r"^\s*timeout:\s*2h\s*$", npc_source, re.M), (
            "可执行位 timeout 与 MAX_STAGE_TIMEOUT 不同源"
        )
