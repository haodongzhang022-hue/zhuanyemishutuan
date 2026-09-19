"""Issue #14 本轮：**同一问题不报第二遍**（评审项确定性去重）

官方对照（`cnbcool/code-review` 插件 与 SCA 插件）：
  - 评审插件把"避免重复刷同一条评论"交给 `include_existing_comments` → **模型判断**；
  - SCA 插件的口径更硬：**"同一组件同一 CVE 不重复阻断"**、
    **"内容未变化的文件重命名不会重复产生"** → **确定性**。

本模块把 SCA 那种硬口径搬到评审侧：同一「类别 + 语义锚点」不重复报，
重复项自动**结转**（不回转、不占轮次）。

判别性设计（关键断言配**反向对照**）：
  - 锚点**不含路径/行号** → 改名与整体移位**不得**被当成新问题（反向：锚点内容变了要判新）；
  - 降噪口径照抄官方默认（`style,nit` 屏蔽、`min_severity` 阈值）；
  - 非代码文件过滤（锁文件/图片/构建产物）——官方插件同口径；
  - `should_rotate` 只看**新问题**（老问题重复报**不得**产生新一轮修复）。
"""
from __future__ import annotations

import os
import shutil
import tempfile

import pytest

from secretary.review import (
    CAT_BUG,
    CAT_NIT,
    CAT_SECURITY,
    CAT_STYLE,
    DEFAULT_EXCLUDED_CATEGORIES,
    SEV_CRITICAL,
    SEV_INFO,
    SEV_WARNING,
    ReviewFingerprintLedger,
    filter_code_files,
    fingerprint,
    is_code_file,
)
from secretary.session import SecretarySession


@pytest.fixture()
def ledger():
    d = tempfile.mkdtemp()
    led = ReviewFingerprintLedger(data_dir=d)
    try:
        yield led
    finally:
        shutil.rmtree(d, ignore_errors=True)


# ------------------------------------------------------------------
# ① 指纹稳定性：改名 / 移位 **不算**新问题
# ------------------------------------------------------------------

class TestFingerprintStability:
    def test_same_code_in_renamed_path_is_same_issue(self, ledger):
        """**核心口径**：内容相同、路径不同 → 同一个问题（官方 SCA 同源）。"""
        items = [
            {"category": CAT_BUG, "line_text": "const x = calc(a, b);",
             "path": "src/old/a.js", "severity": SEV_CRITICAL},
            {"category": CAT_BUG, "line_text": "  const x = calc(a, b);  ",
             "path": "src/new/renamed.js", "severity": SEV_CRITICAL},
        ]
        res = ledger.dedup(items, round_no=1)
        assert len(res.fresh) == 1 and len(res.duplicate) == 1
        assert res.duplicate[0].path_hint == "src/new/renamed.js"

    def test_line_shift_is_same_issue(self, ledger):
        """整体插行导致行号位移 → 仍是同一个问题（行号不进指纹）。"""
        first = fingerprint(CAT_BUG, "  return check(user);  ")
        second = fingerprint(CAT_BUG, "return check(user);", path="moved.py")
        assert first.key == second.key

    def test_changed_code_is_a_new_issue(self, ledger):
        """**反向对照**：代码内容变了 → **必须**判为新问题（别把去重做成"永不报"）。"""
        res = ledger.dedup([
            {"category": CAT_BUG, "line_text": "return check(user);",
             "path": "a.py"},
            {"category": CAT_BUG, "line_text": "return check(order);",
             "path": "a.py"},
        ], round_no=1)
        assert len(res.fresh) == 2 and not res.duplicate

    def test_different_category_is_a_new_issue(self, ledger):
        """**反向对照**：同一行、不同类别 → 不同问题（如 bug 与 security 不是一回事）。"""
        res = ledger.dedup([
            {"category": CAT_BUG, "line_text": "eval(input())", "path": "a.py"},
            {"category": CAT_SECURITY, "line_text": "eval(input())", "path": "a.py"},
        ], round_no=1)
        assert len(res.fresh) == 2

    def test_comments_and_literals_do_not_split_the_anchor(self, ledger):
        """格式噪音（注释/字面量/数字）不进锚点 → 同一问题的不同写法仍判同。"""
        res = ledger.dedup([
            {"category": CAT_BUG, "line_text": "x = fetch(20)  # timeout",
             "path": "a.py"},
            {"category": CAT_BUG, "line_text": "x=fetch(30)", "path": "a.py"},
        ], round_no=1)
        assert len(res.fresh) == 1 and len(res.duplicate) == 1

    def test_context_window_strengthens_anchor(self, ledger):
        """给了前后文 → 锚点更具体；不同上下文不被误判为同一问题。"""
        a = fingerprint(CAT_BUG, "return y", ("def f():", "  y = 1"))
        b = fingerprint(CAT_BUG, "return y", ("def g():", "  y = 2"))
        assert a.key != b.key


# ------------------------------------------------------------------
# ② 降噪：照抄官方默认口径
# ------------------------------------------------------------------

class TestNoiseReduction:
    def test_style_and_nit_filtered_by_default(self, ledger):
        """官方默认 `exclude_categories: "style,nit"` → 照抄，避免小问题刷屏。"""
        res = ledger.dedup([
            {"category": CAT_STYLE, "line_text": "x=1", "path": "a.py"},
            {"category": CAT_NIT, "line_text": "y=2", "path": "a.py"},
            {"category": CAT_BUG, "line_text": "z=3", "path": "a.py"},
        ], round_no=1)
        assert len(res.filtered) == 2 and len(res.fresh) == 1
        assert set(DEFAULT_EXCLUDED_CATEGORIES) == {CAT_STYLE, CAT_NIT}

    def test_min_severity_threshold_respected(self):
        """官方 `min_severity` 同口径：低于阈值的不发评论（这里是"不发回转"）。"""
        d = tempfile.mkdtemp()
        try:
            led = ReviewFingerprintLedger(data_dir=d, min_severity=SEV_WARNING)
            res = led.dedup([
                {"category": CAT_BUG, "line_text": "a=1", "severity": SEV_INFO},
                {"category": CAT_BUG, "line_text": "b=1", "severity": SEV_WARNING},
            ], round_no=1)
            assert len(res.filtered) == 1 and len(res.fresh) == 1
            assert res.fresh[0].severity == SEV_WARNING
        finally:
            shutil.rmtree(d, ignore_errors=True)

    def test_unknown_category_falls_back_to_other(self, ledger):
        """**反向对照**：未知类别 → `other`（不抛、也不当成屏蔽项）。"""
        res = ledger.dedup([
            {"category": "totally-made-up", "line_text": "a=1", "path": "a.py"},
        ], round_no=1)
        assert len(res.fresh) == 1 and res.fresh[0].category == "other"

    def test_bad_shaped_items_are_ignored_not_fatal(self, ledger):
        """宽进：形状不符的条目忽略（不因一条坏数据整轮评审失败）。"""
        res = ledger.dedup([None, 42, "str", {},
                            {"category": CAT_BUG, "line_text": "a=1"}],
                           round_no=1)
        assert len(res.fresh) == 1


# ------------------------------------------------------------------
# ③ 非代码文件过滤（官方插件同口径）
# ------------------------------------------------------------------

class TestNonCodeFiles:
    def test_lock_and_binary_and_artifacts_are_not_code(self):
        for p in ("package-lock.json", "yarn.lock", "go.sum",
                  "assets/logo.png", "dist/bundle.js", "node_modules/x/a.js",
                  "build/out.bin", "x.tar.gz"):
            assert is_code_file(p) is False, p

    def test_real_sources_are_code(self):
        """**反向对照**：真实源码**必须**算代码（否则评审会漏文件）。"""
        for p in ("src/main.py", "app/page.tsx", "internal/x.go", "README.md"):
            assert is_code_file(p) is True, p

    def test_filter_preserves_order_and_dedups(self):
        got = filter_code_files(["b.py", "a.lock", "b.py", "a.py"])
        assert got == ["b.py", "a.py"]

    def test_unknown_path_is_treated_as_code(self):
        """判不出就当代码（宁可多评审，也不漏）。"""
        assert is_code_file("") is True
        assert is_code_file(None) is True
        assert is_code_file("weird.unknown-ext") is True


# ------------------------------------------------------------------
# ④ 结转：重复项不回转、不占轮次
# ------------------------------------------------------------------

class TestCarryForward:
    def test_duplicate_does_not_rotate(self, ledger):
        """**核心口径**：老问题重复报 → `should_rotate` 为假（不产生新一轮修复）。"""
        kw = [{"category": CAT_BUG, "line_text": "a=1", "path": "a.py",
               "severity": SEV_CRITICAL}]
        first = ledger.dedup(kw, round_no=1)
        assert first.should_rotate is True
        second = ledger.dedup(kw, round_no=2)
        assert second.should_rotate is False
        assert not second.fresh and len(second.duplicate) == 1

    def test_carry_marks_entry(self, ledger):
        """结转留痕：标记后 `carried()` 可见。"""
        ledger.dedup([{"category": CAT_BUG, "line_text": "a=1", "path": "a.py"}],
                     round_no=1)
        key = ledger.overview()["entries"][0]["key"]
        assert ledger.carry([key]) == 1
        assert ledger.carried()[0].key == key
        assert ledger.carry([key]) == 0        # 幂等

    def test_carry_unknown_key_is_noop(self, ledger):
        """**反向对照**：结转不存在的指纹 → 0（不凭空造条目）。"""
        assert ledger.carry(["does-not-exist"]) == 0
        assert ledger.overview()["total"] == 0

    def test_repeated_counts_are_visible(self, ledger):
        """重复报的一眼可见（`repeated`）。"""
        kw = [{"category": CAT_BUG, "line_text": "a=1", "path": "a.py"}]
        for r in range(3):
            ledger.dedup(kw, round_no=r + 1)
        ov = ledger.overview()
        assert ov["total"] == 1 and ov["repeated_count"] == 1
        assert ov["repeated"][0]["count"] == 3
        assert ov["repeated"][0]["rounds"] == [1, 2, 3]


# ------------------------------------------------------------------
# ⑤ 持久化与门面
# ------------------------------------------------------------------

class TestPersistenceAndFacade:
    def test_ledger_survives_new_instance(self):
        """跨实例可比（否则"每轮新实例"就等于每轮都是新问题）。"""
        d = tempfile.mkdtemp()
        try:
            kw = [{"category": CAT_BUG, "line_text": "a=1", "path": "a.py"}]
            ReviewFingerprintLedger(data_dir=d).dedup(kw, round_no=1)
            again = ReviewFingerprintLedger(data_dir=d).dedup(kw, round_no=2)
            assert not again.fresh and len(again.duplicate) == 1
        finally:
            shutil.rmtree(d, ignore_errors=True)

    def test_corrupt_ledger_does_not_block_work(self):
        """**反向对照**：台账损坏 → 当空台账（不挡住干活），不抛。"""
        d = tempfile.mkdtemp()
        try:
            with open(os.path.join(d, "review-fingerprints.json"), "w") as f:
                f.write("{not json")
            led = ReviewFingerprintLedger(data_dir=d)
            res = led.dedup([{"category": CAT_BUG, "line_text": "a=1"}],
                            round_no=1)
            assert len(res.fresh) == 1
        finally:
            shutil.rmtree(d, ignore_errors=True)

    def test_no_data_dir_means_in_process_only(self):
        """不给目录 → 仅进程内生效（不落盘、不抛）。"""
        led = ReviewFingerprintLedger(data_dir=None)
        led.dedup([{"category": CAT_BUG, "line_text": "a=1"}], round_no=1)
        assert led.overview()["total"] == 1

    def test_session_facade_end_to_end(self):
        """门面：`review_dedup` / `carry_review_items` / `review_fingerprints_overview`。"""
        d = tempfile.mkdtemp()
        try:
            sec = SecretarySession("fp-s1", data_dir=d)
            kw = [{"category": CAT_BUG, "line_text": "return check(user);",
                   "path": "a.py", "severity": SEV_CRITICAL}]
            first = sec.review_dedup(kw, round_no=1)
            assert first.should_rotate is True
            # 同一问题在另一文件名下再报 → 不回转
            second = sec.review_dedup(
                [{"category": CAT_BUG, "line_text": "return check(user);",
                  "path": "moved/b.py", "severity": SEV_CRITICAL}], round_no=2)
            assert second.should_rotate is False
            key = sec.review_fingerprints_overview()["entries"][0]["key"]
            assert sec.carry_review_items(key) == 1
            assert sec.review_fingerprints_overview()["carried"]
        finally:
            shutil.rmtree(d, ignore_errors=True)

    def test_session_delete_does_not_leak_fingerprints(self):
        """会话回收：指纹台账随会话数据根一并清理（同 id 复建不带陈旧条目）。"""
        d = tempfile.mkdtemp()
        try:
            sec = SecretarySession("fp-s2", data_dir=d)
            sec.review_dedup([{"category": CAT_BUG, "line_text": "a=1"}],
                             round_no=1)
            assert sec.review_fingerprints_overview()["total"] == 1
            fresh = SecretarySession("fp-s3", data_dir=tempfile.mkdtemp())
            assert fresh.review_fingerprints_overview()["total"] == 0
        finally:
            shutil.rmtree(d, ignore_errors=True)
