"""报告产物核验入口的表驱动契约测试（issue #70）。

公开入口是技能副本里的 `scripts/report_check.py`：接收报告、可选讲稿与可选核验记录，
输出结构化结果与可读摘要。这里钉住它的对外契约：检查项 id 与状态、核验记录
`ae-report-record/1` 的结构校验、损坏 / 未知版本 / 讲稿不对应 / 过期记录 / 计算
不一致 / 空检查集合的明确报错，以及「程序检查通过不自动形成可交付结论」的判定语义。
结构化字段（id、status、code、verdict 字段）语言无关；detail 与摘要按 locale 本地化。

只用标准库与内联合成数据：python3 -m unittest discover tests
"""
from __future__ import annotations

import hashlib
import io
import json
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "skill" / "annotation-everything" / "scripts"
sys.path.insert(0, str(SCRIPTS))
os.environ.setdefault("AE_HOME", tempfile.mkdtemp(prefix="ae-test-home-"))
os.environ.pop("AE_LOCALE", None)   # 语言由各用例显式给定

import report_check as rc  # noqa: E402

RECORD_SCHEMA = "ae-report-record/1"
CHECK_SCHEMA = "ae-report-check/1"


def tmpdir() -> Path:
    return Path(tempfile.mkdtemp())


REPORT = """<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><title>合成季度回顾</title>
<style>.page{width:1280px;height:720px;padding:56px;box-sizing:border-box;font-family:sans-serif}
.kpi{font-size:48px;font-weight:700}</style></head><body>
<section class="page" data-page="1" data-key="sum" id="page-sum"><h2>结论</h2><p class="kpi">收入同比 +12%</p>
<p>新开 3 家门店贡献主要增量。</p></section>
<section class="page" data-page="2" data-key="next" id="page-next"><h2>下一步</h2><p>两件事。</p></section>
</body></html>
"""

REPORT_EN = REPORT.replace("合成季度回顾", "Synthetic Quarterly Review").replace("收入同比 +12%", "Revenue +12% YoY")

SCRIPT = """# 讲稿：合成季度回顾

### 第 1 页｜结论
**讲法**：
先说结论：收入同比增长 12%。

### 第 2 页｜下一步
**讲法**：
两件事。
"""

SCRIPT_EN = """# Script: Synthetic Quarterly Review

### Page 1｜Conclusion
**Talking points**:
Conclusion first: revenue up 12% YoY.

### Page 2｜Next steps
**Talking points**:
Two things.
"""


def sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def make_record(report_text: str = REPORT, script_text: str | None = SCRIPT,
                report_name: str = "report.html", **overrides) -> dict:
    """一份结构完整的最小核验记录；检查结果缺省全部 not_run。"""
    links = {
        "report": report_name,
        "script": "script.md" if script_text is not None else None,
        "sha256": {"report": sha(report_text),
                   "script": sha(script_text) if script_text is not None else None},
        "sourceNote": "materials/q3-notes.md 及同目录讲稿",
    }
    record = {
        "schema": RECORD_SCHEMA,
        "generatedAt": "2026-11-02T09:00:00+08:00",
        "request": {"purpose": "季度经营汇报", "audience": "管理层",
                    "coreQuestions": ["三季度收入为什么增长"], "length": "2 页", "script": "yes"},
        "sources": [{"path": "materials/q3-notes.md", "status": "read",
                     "coverage": "全文", "note": ""}],
        "method": {"kind": "builtin", "name": "annotation-everything 内置范式",
                   "version": "0.9.0", "unavailable": []},
        "links": links,
        "findings": [{"claim": "三季度收入同比增长 12%", "type": "calculation",
                      "sources": ["materials/q3-notes.md"], "pages": ["sum"],
                      "calculation": {"op": "percent-change", "inputs": {"a": 100, "b": 112},
                                      "expected": 12},
                      "note": "收入 112 对上年 100，四舍五入到整数"}],
        "gaps": ["十月的周报缺失"],
        "checks": [
            {"id": "content.requirements", "kind": "content", "status": "not_run",
             "scope": "全部需求", "note": ""},
            {"id": "visual.pages", "kind": "visual", "status": "not_run",
             "scope": "全部 2 页", "note": ""},
        ],
        "openItems": [{"item": "十月周报待补", "blocking": False}],
        "verdict": {"draftComplete": True, "deliverable": None},
    }
    record.update(overrides)
    return record


def write_case(d: Path, *, report: str = REPORT, script: str | None = SCRIPT,
               record: dict | None = None, record_text: str | None = None,
               report_name: str = "report.html") -> dict:
    """把合成产物写进临时目录；返回各文件路径。"""
    d.mkdir(parents=True, exist_ok=True)
    paths = {"report": d / report_name}
    paths["report"].write_text(report, encoding="utf-8")
    if script is not None:
        paths["script"] = d / "script.md"
        paths["script"].write_text(script, encoding="utf-8")
    if record_text is not None:
        paths["record"] = d / "report-record.json"
        paths["record"].write_text(record_text, encoding="utf-8")
    elif record is not None:
        paths["record"] = d / "report-record.json"
        paths["record"].write_text(json.dumps(record, ensure_ascii=False, indent=2) + "\n",
                                   encoding="utf-8")
    return paths


def run_check(paths: dict, *, locale: str | None = None, argv_extra: list[str] | None = None
              ) -> tuple[int, dict | None, str, str]:
    """在进程内调用公开入口：返回 (退出码, JSON 结果或 None, 摘要 stdout, stderr)。"""
    argv = [str(paths["report"])]
    if paths.get("script"):
        argv += ["--script", str(paths["script"])]
    if paths.get("record"):
        argv += ["--record", str(paths["record"])]
    if locale:
        argv += ["--locale", locale]
    if argv_extra:
        argv += argv_extra
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = rc.main(argv)
    text = out.getvalue()
    payload = None
    if "--json" in (argv_extra or []):
        payload = json.loads(text)
    return code, payload, text, err.getvalue()


def check_by_id(payload: dict, cid: str) -> dict:
    for item in payload["checks"]:
        if item["id"] == cid:
            return item
    raise AssertionError(f"检查结果里没有 {cid}：{payload['checks']}")


class ValidInputsTests(unittest.TestCase):
    """有效输入：程序检查通过、初稿成立；内容与视觉核验未执行不能算通过。"""

    def test_valid_full_set(self):
        d = tmpdir()
        paths = write_case(d, record=make_record())
        code, payload, text, _ = run_check(paths, locale="zh-CN", argv_extra=["--json"])
        self.assertEqual(code, 0)
        self.assertEqual(payload["schema"], CHECK_SCHEMA)
        self.assertTrue(payload["verdict"]["draftComplete"])
        self.assertFalse(payload["verdict"]["deliverable"])
        self.assertEqual(payload["record"]["version"], RECORD_SCHEMA)
        self.assertTrue(payload["record"]["fresh"])
        self.assertEqual(payload["coverage"]["content"], "not_run")
        self.assertEqual(payload["coverage"]["visual"], "not_run")
        self.assertEqual(check_by_id(payload, "report.parse")["status"], "pass")
        self.assertEqual(check_by_id(payload, "report.pageKeys")["status"], "pass")
        self.assertEqual(check_by_id(payload, "script.pages")["status"], "pass")
        self.assertEqual(check_by_id(payload, "record.calculations")["status"], "pass")
        # 交付被挡下的原因要写明内容与视觉核验未执行
        self.assertTrue(any("视觉" in r or "visual" in r for r in payload["verdict"]["reasons"]),
                        payload["verdict"]["reasons"])
        # 未执行的检查不能出现在任何「通过」的展示里
        self.assertNotIn("content.requirements", [c["id"] for c in payload["checks"]])

    def test_summary_is_localized(self):
        d = tmpdir()
        paths = write_case(d, record=make_record())
        _, _, zh, _ = run_check(paths, locale="zh-CN")
        self.assertIn("初稿", zh)
        self.assertIn("核验", zh)
        _, _, en, _ = run_check(paths, locale="en")
        self.assertIn("Draft", en)
        self.assertIn("not", en.lower() or en)   # 英文摘要说明未执行项

    def test_report_only_no_record(self):
        d = tmpdir()
        paths = write_case(d, script=None)
        code, payload, text, _ = run_check(paths, locale="zh-CN", argv_extra=["--json"])
        self.assertEqual(code, 0)
        self.assertEqual(check_by_id(payload, "script.pages")["status"], "not_applicable")
        self.assertTrue(payload["verdict"]["draftComplete"])
        self.assertFalse(payload["verdict"]["deliverable"])
        self.assertIsNone(payload["record"]["version"])
        self.assertFalse(payload["record"]["fresh"])
        self.assertEqual(payload["coverage"]["content"], "not_run")
        # 没有记录时，交付结论必须说明内容与视觉核验没有执行依据
        self.assertTrue(any("记录" in r for r in payload["verdict"]["reasons"]),
                        payload["verdict"]["reasons"])

    def test_english_materials(self):
        d = tmpdir()
        paths = write_case(d, report=REPORT_EN, script=SCRIPT_EN,
                           record=make_record(report_text=REPORT_EN, script_text=SCRIPT_EN))
        code, payload, _, _ = run_check(paths, locale="en", argv_extra=["--json"])
        self.assertEqual(code, 0)
        self.assertTrue(payload["verdict"]["draftComplete"])


class ScriptCorrespondenceTests(unittest.TestCase):
    """讲稿对应：页码与报告页序一一对应；不对应时明确列出。"""

    def test_missing_script_page_fails(self):
        d = tmpdir()
        short = SCRIPT.split("### 第 2 页")[0]
        paths = write_case(d, script=short, record=make_record(script_text=short))
        code, payload, text, _ = run_check(paths, locale="zh-CN", argv_extra=["--json"])
        self.assertEqual(code, 1)
        item = check_by_id(payload, "script.pages")
        self.assertEqual(item["status"], "fail")
        self.assertEqual(item["code"], "script.page-mismatch")
        self.assertIn("2", item["detail"])
        self.assertFalse(payload["verdict"]["draftComplete"])

    def test_extra_script_page_fails(self):
        d = tmpdir()
        extra = SCRIPT + "\n### 第 3 页｜多出来的页\n**讲法**：\n不该有。\n"
        paths = write_case(d, script=extra, record=make_record(script_text=extra))
        code, payload, _, _ = run_check(paths, locale="zh-CN", argv_extra=["--json"])
        self.assertEqual(code, 1)
        self.assertEqual(check_by_id(payload, "script.pages")["status"], "fail")


class RecordStructureTests(unittest.TestCase):
    """损坏记录、未知版本与缺失字段的明确报错。"""

    def test_broken_record(self):
        d = tmpdir()
        paths = write_case(d, record_text="{ not json")
        code, payload, text, _ = run_check(paths, locale="zh-CN", argv_extra=["--json"])
        self.assertEqual(code, 1)
        item = check_by_id(payload, "record.structure")
        self.assertEqual(item["status"], "fail")
        self.assertEqual(item["code"], "record.broken")
        self.assertTrue(item["detail"].strip())

    def test_unknown_record_version(self):
        d = tmpdir()
        record = make_record()
        record["schema"] = "ae-report-record/2"
        paths = write_case(d, record=record)
        code, payload, _, _ = run_check(paths, locale="zh-CN", argv_extra=["--json"])
        self.assertEqual(code, 1)
        item = check_by_id(payload, "record.structure")
        self.assertEqual(item["code"], "record.version-unknown")
        self.assertIn("2", item["detail"])
        self.assertFalse(payload["verdict"]["deliverable"])

    def test_missing_required_field(self):
        d = tmpdir()
        record = make_record()
        del record["links"]
        paths = write_case(d, record=record)
        code, payload, _, _ = run_check(paths, locale="zh-CN", argv_extra=["--json"])
        self.assertEqual(code, 1)
        self.assertEqual(check_by_id(payload, "record.structure")["code"], "record.field-missing")
        self.assertIn("links", check_by_id(payload, "record.structure")["detail"])

    def test_references_missing_report_page(self):
        d = tmpdir()
        record = make_record()
        record["findings"][0]["pages"] = ["ghost"]
        paths = write_case(d, record=record)
        code, payload, _, _ = run_check(paths, locale="zh-CN", argv_extra=["--json"])
        self.assertEqual(code, 1)
        item = check_by_id(payload, "record.pageRefs")
        self.assertEqual(item["status"], "fail")
        self.assertEqual(item["code"], "record.page-missing")
        self.assertIn("ghost", item["detail"])
        self.assertFalse(payload["verdict"]["draftComplete"])


class FreshnessTests(unittest.TestCase):
    """证据新鲜度：记录对应旧产物时不沿用其结论。"""

    def test_stale_record(self):
        d = tmpdir()
        record = make_record()          # 哈希对应 REPORT
        paths = write_case(d, report=REPORT.replace("两件事", "三件事"), record=record)
        code, payload, _, _ = run_check(paths, locale="zh-CN", argv_extra=["--json"])
        self.assertEqual(code, 1)
        self.assertFalse(payload["record"]["fresh"])
        item = check_by_id(payload, "record.freshness")
        self.assertEqual(item["status"], "fail")
        self.assertEqual(item["code"], "record.stale")
        # 过期记录的检查结果不能为当前产物背书
        self.assertEqual(payload["coverage"]["content"], "unverified")
        self.assertEqual(payload["coverage"]["visual"], "unverified")
        self.assertFalse(payload["verdict"]["deliverable"])


class CalculationTests(unittest.TestCase):
    """明确声明输入与口径的可复算数值：复算一致通过，不一致明确报错。"""

    def test_matching_calculation_passes(self):
        d = tmpdir()
        paths = write_case(d, record=make_record())
        code, payload, _, _ = run_check(paths, locale="zh-CN", argv_extra=["--json"])
        self.assertEqual(code, 0)
        self.assertEqual(check_by_id(payload, "record.calculations")["status"], "pass")

    def test_mismatched_calculation_fails(self):
        d = tmpdir()
        record = make_record()
        record["findings"][0]["calculation"]["expected"] = 15
        paths = write_case(d, record=record)
        code, payload, _, _ = run_check(paths, locale="zh-CN", argv_extra=["--json"])
        self.assertEqual(code, 1)
        item = check_by_id(payload, "record.calculations")
        self.assertEqual(item["code"], "record.calc-mismatch")
        self.assertIn("15", item["detail"])
        self.assertFalse(payload["verdict"]["deliverable"])

    def test_unsupported_operator_fails(self):
        d = tmpdir()
        record = make_record()
        record["findings"][0]["calculation"] = {"op": "pow", "inputs": {"a": 2, "b": 10},
                                                "expected": 1024}
        paths = write_case(d, record=record)
        code, payload, _, _ = run_check(paths, locale="zh-CN", argv_extra=["--json"])
        self.assertEqual(code, 1)
        self.assertEqual(check_by_id(payload, "record.calculations")["code"],
                         "record.calc-unsupported-op")


class RecordChecksTests(unittest.TestCase):
    """记录的检查集合：非空、id 可识别、内容与视觉两类都有声明。"""

    def test_empty_checks_fail(self):
        d = tmpdir()
        paths = write_case(d, record=make_record(checks=[]))
        code, payload, _, _ = run_check(paths, locale="zh-CN", argv_extra=["--json"])
        self.assertEqual(code, 1)
        item = check_by_id(payload, "record.checks")
        self.assertEqual(item["status"], "fail")
        self.assertEqual(item["code"], "record.checks-empty")

    def test_unknown_check_id_fails(self):
        d = tmpdir()
        record = make_record(checks=[
            {"id": "magic.auto-review", "kind": "content", "status": "pass", "scope": "", "note": ""},
            {"id": "visual.pages", "kind": "visual", "status": "not_run", "scope": "", "note": ""},
        ])
        paths = write_case(d, record=record)
        code, payload, _, _ = run_check(paths, locale="zh-CN", argv_extra=["--json"])
        self.assertEqual(code, 1)
        item = check_by_id(payload, "record.checks")
        self.assertEqual(item["code"], "record.check-unknown")
        self.assertIn("magic.auto-review", item["detail"])

    def test_missing_visual_declaration_fails(self):
        d = tmpdir()
        record = make_record(checks=[
            {"id": "content.requirements", "kind": "content", "status": "not_run",
             "scope": "", "note": ""},
        ])
        paths = write_case(d, record=record)
        code, payload, _, _ = run_check(paths, locale="zh-CN", argv_extra=["--json"])
        self.assertEqual(code, 1)
        self.assertEqual(check_by_id(payload, "record.checks")["code"], "record.checks-empty")

    def test_bad_check_status_fails(self):
        d = tmpdir()
        record = make_record(checks=[
            {"id": "content.requirements", "kind": "content", "status": "done",
             "scope": "", "note": ""},
            {"id": "visual.pages", "kind": "visual", "status": "not_run", "scope": "", "note": ""},
        ])
        paths = write_case(d, record=record)
        code, payload, _, _ = run_check(paths, locale="zh-CN", argv_extra=["--json"])
        self.assertEqual(code, 1)
        self.assertEqual(check_by_id(payload, "record.checks")["code"], "record.check-status")


class DeliverableVerdictTests(unittest.TestCase):
    """可交付结论：记录完整覆盖且声明可交付时才被复核支持；缺任何一环都不支持。"""

    @staticmethod
    def passed_record(**overrides) -> dict:
        record = make_record(
            checks=[
                {"id": "content.requirements", "kind": "content", "status": "pass",
                 "scope": "全部需求", "note": "逐项核对，见记录"},
                {"id": "visual.pages", "kind": "visual", "status": "pass",
                 "scope": "全部 2 页", "note": "已逐页查看"},
            ],
            **overrides)
        record["verdict"] = {"draftComplete": True, "deliverable": True}
        return record

    def test_full_coverage_supports_declared_deliverable(self):
        d = tmpdir()
        paths = write_case(d, record=self.passed_record())
        code, payload, _, _ = run_check(paths, locale="zh-CN", argv_extra=["--json"])
        self.assertEqual(code, 0)
        self.assertEqual(payload["coverage"]["content"], "covered")
        self.assertEqual(payload["coverage"]["visual"], "covered")
        self.assertTrue(payload["verdict"]["deliverable"])

    def test_deliverable_claim_without_visual_not_supported(self):
        d = tmpdir()
        record = self.passed_record()
        record["checks"][1]["status"] = "not_run"
        record["verdict"] = {"draftComplete": True, "deliverable": True}
        paths = write_case(d, record=record)
        code, payload, _, _ = run_check(paths, locale="zh-CN", argv_extra=["--json"])
        self.assertEqual(code, 0)
        self.assertEqual(payload["coverage"]["visual"], "not_run")
        self.assertFalse(payload["verdict"]["deliverable"])
        self.assertTrue(any("视觉" in r for r in payload["verdict"]["reasons"]),
                        payload["verdict"]["reasons"])

    def test_blocking_open_item_prevents_deliverable(self):
        d = tmpdir()
        record = self.passed_record()
        record["openItems"] = [{"item": "核心口径二选一待用户拍板", "blocking": True}]
        paths = write_case(d, record=record)
        code, payload, _, _ = run_check(paths, locale="zh-CN", argv_extra=["--json"])
        self.assertEqual(code, 0)
        self.assertFalse(payload["verdict"]["deliverable"])

    def test_failed_content_check_blocks(self):
        d = tmpdir()
        record = self.passed_record()
        record["checks"][0] = {"id": "content.requirements", "kind": "content", "status": "fail",
                               "scope": "核心问题 2", "note": "报告未回应第二个核心问题"}
        record["verdict"] = {"draftComplete": True, "deliverable": False}
        paths = write_case(d, record=record)
        code, payload, _, _ = run_check(paths, locale="zh-CN", argv_extra=["--json"])
        self.assertEqual(code, 0)
        self.assertEqual(payload["coverage"]["content"], "uncovered")
        self.assertFalse(payload["verdict"]["deliverable"])
        self.assertFalse(payload["record"]["declaredDeliverable"])

    def test_record_not_run_shown_as_not_run(self):
        d = tmpdir()
        paths = write_case(d, record=make_record())
        _, payload, _, _ = run_check(paths, locale="en", argv_extra=["--json"])
        # 未执行就是未执行：coverage 不是 covered，摘要不能把 not_run 说成通过
        self.assertNotEqual(payload["coverage"]["content"], "covered")
        self.assertNotEqual(payload["coverage"]["visual"], "covered")


class ReportFormatTests(unittest.TestCase):
    """报告本身的格式检查。"""

    def test_duplicate_page_numbers_fail(self):
        d = tmpdir()
        dup = REPORT.replace('data-page="2" data-key="next"', 'data-page="1" data-key="next"')
        paths = write_case(d, report=dup, script=None)
        code, payload, _, _ = run_check(paths, locale="zh-CN", argv_extra=["--json"])
        self.assertEqual(code, 1)
        self.assertEqual(check_by_id(payload, "report.parse")["status"], "fail")
        self.assertFalse(payload["verdict"]["draftComplete"])

    def test_derived_page_key_warns_but_draft_stands(self):
        d = tmpdir()
        derived = REPORT.replace('data-key="sum" id="page-sum"', 'id="page-sum"') \
                        .replace('data-key="next" id="page-next"', 'id="page-next"')
        paths = write_case(d, report=derived, script=None)
        code, payload, _, _ = run_check(paths, locale="zh-CN", argv_extra=["--json"])
        self.assertEqual(code, 0)
        self.assertTrue(payload["warnings"])
        self.assertTrue(payload["verdict"]["draftComplete"])
        # 由页码生成的键参与不了重排校正：不阻塞初稿，但写进交付阻塞之外的说明
        self.assertFalse(payload["verdict"]["deliverable"])

    def test_no_pages_at_all_fails(self):
        d = tmpdir()
        paths = write_case(d, report="<html><body><p>不是逐页报告</p></body></html>", script=None)
        code, payload, _, _ = run_check(paths, locale="zh-CN", argv_extra=["--json"])
        self.assertEqual(code, 1)
        self.assertEqual(check_by_id(payload, "report.parse")["status"], "fail")
        self.assertFalse(payload["verdict"]["draftComplete"])


class CliContractTests(unittest.TestCase):
    """命令行契约：JSON schema、退出码、语言无关字段、摘要本地化。"""

    def test_missing_report_exit_2(self):
        d = tmpdir()
        code, _, _, err = run_check({"report": d / "nope.html"}, locale="zh-CN")
        self.assertEqual(code, 2)
        self.assertTrue(err.strip())

    def test_bad_locale_exit_2(self):
        d = tmpdir()
        paths = write_case(d)
        code, _, _, err = run_check(paths, locale="fr")
        self.assertEqual(code, 2)

    def test_structured_fields_language_invariant(self):
        d = tmpdir()
        paths = write_case(d, record=make_record())
        _, zh, _, _ = run_check(paths, locale="zh-CN", argv_extra=["--json"])
        _, en, _, _ = run_check(paths, locale="en", argv_extra=["--json"])
        self.assertEqual([c["id"] for c in zh["checks"]], [c["id"] for c in en["checks"]])
        self.assertEqual([c["status"] for c in zh["checks"]], [c["status"] for c in en["checks"]])
        self.assertEqual(zh["coverage"], en["coverage"])
        self.assertEqual(zh["verdict"]["draftComplete"], en["verdict"]["draftComplete"])
        self.assertEqual(zh["verdict"]["deliverable"], en["verdict"]["deliverable"])
        # reason 的具体条数可以随语言一致：两条路径都有理由且非空
        self.assertTrue(zh["verdict"]["reasons"] and en["verdict"]["reasons"])

    def test_summary_mentions_record_and_next_step(self):
        d = tmpdir()
        paths = write_case(d, record=make_record())
        _, _, zh, _ = run_check(paths, locale="zh-CN")
        self.assertIn("初稿", zh)
        self.assertIn("报告可交付", zh)      # 明确区分两个判定
        self.assertIn("未执行", zh)


if __name__ == "__main__":
    unittest.main()
