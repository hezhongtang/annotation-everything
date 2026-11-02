"""结构与视觉核验、可交付判定的契约测试（issue #73）。

report_check.py 的 `record.visual` / `record.length` 检查与 `structure.*` 清单钉住
#69 决策 11、12 的确定规则：结构核验覆盖叙述顺序、标题与内容、结论支撑关系与
重复缺口；视觉核验声明逐页检查通过必须有覆盖全部报告页的证据结构（每页
checked 与 issues），声明与证据不符要能暴露——缺页覆盖、未检查页、带未解决
问题却声明通过、篇幅与需求不一致都是程序检查失败；如实记录的 fail / not_run /
unavailable 不产生程序失败，但覆盖如实降级、不能形成可交付结论。无讲稿时讲稿
关联项（content.script-consistency、structure.length）按适用性规则不计入总数，
声明了 pass 就是虚假的已检查。程序、内容、结构与视觉三类结果共同决定「报告可
交付」；初稿生成完成与可交付分别输出。

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

CONTENT_CHECK_IDS = ("content.requirements", "content.claims", "content.fact-vs-inference",
                     "content.units-dates", "content.numbers-charts", "content.cross-page",
                     "content.script-consistency", "content.open-questions")
# #73 结构核验清单（决策 11）：叙述顺序、标题与内容、结论支撑、重复缺口、篇幅或时长。
STRUCTURE_CHECK_IDS = ("structure.flow", "structure.headings", "structure.claim-support",
                       "structure.dup-gaps", "structure.length")
VISUAL_CHECK_IDS = ("visual.pages",)
# 讲稿关联项：无讲稿时不计入覆盖总数，声明 pass 即虚假的已检查。
SCRIPT_SCOPED_IDS = ("content.script-consistency", "structure.length")

MATERIAL = "# 三季度笔记\n\n三季度收入 112 万元，上年同期 100 万元。\n"


def tmpdir() -> Path:
    return Path(tempfile.mkdtemp())


def sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


REPORT = """<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><title>合成季度回顾</title>
<style>.page{width:1280px;height:720px;padding:56px;box-sizing:border-box;font-family:sans-serif}
.kpi{font-size:48px;font-weight:700}</style></head><body>
<section class="page" data-page="1" data-key="sum" id="page-sum"><h2>结论</h2><p class="kpi">收入同比 +12%</p>
<p>新开 3 家门店贡献主要增量。</p></section>
<section class="page" data-page="2" data-key="rev" id="page-rev"><h2>收入结构</h2><p>外卖占比 38%。</p></section>
<section class="page" data-page="3" data-key="next" id="page-next"><h2>下一步</h2><p>两件事。</p></section>
</body></html>
"""

SCRIPT = """# 讲稿：合成季度回顾

### 第 1 页｜结论
**讲法**：
先说结论：收入同比增长 12%。

### 第 2 页｜收入结构
**讲法**：
外卖占比继续上升。

### 第 3 页｜下一步
**讲法**：
两件事。
"""


def visual_evidence(pages: list | None = None, *, status: str = "pass") -> list[dict]:
    """visual.pages 的 checks 声明；pages 为 None 表示不带逐页证据。"""
    item = {"id": "visual.pages", "kind": "visual", "status": status,
            "scope": "全部 3 页（1280×720 实际渲染）", "note": "逐页渲染检查"}
    if pages is not None:
        item["pages"] = pages
    return item


def full_pages(*, checked: bool = True, issues: list | None = None) -> list[dict]:
    """三页证据：可选把某页标为未检查或带问题。"""
    entries = []
    for n in (1, 2, 3):
        entry = {"page": n, "checked": checked, "viewport": "1280x720"}
        if issues:
            entry["issues"] = issues
        entries.append(entry)
    return entries


def all_checks(*, content: str = "pass", structure: str = "pass",
               visual: dict | None = None) -> list[dict]:
    """完整清单声明：内容八项 + 结构五项 + 视觉一项（默认全 pass、带证据）。"""
    items = [{"id": cid, "kind": "content", "status": content,
              "scope": "全部", "note": "合成契约数据"}
             for cid in CONTENT_CHECK_IDS]
    items += [{"id": cid, "kind": "structure", "status": structure,
               "scope": "全部页", "note": "合成契约数据"}
              for cid in STRUCTURE_CHECK_IDS]
    if visual is None:
        visual = visual_evidence(full_pages())
    items.append(visual)
    return items


def make_record(script_text: str = SCRIPT, **overrides) -> dict:
    """结构完整的最小核验记录；检查缺省全部 not_run、视觉不带证据。"""
    record = {
        "schema": RECORD_SCHEMA,
        "generatedAt": "2026-11-02T09:00:00+08:00",
        "request": {"purpose": "季度经营汇报", "audience": "管理层",
                    "coreQuestions": ["三季度收入为什么增长"], "length": "3 页",
                    "script": "yes"},
        "sources": [{"path": "materials/q3-notes.md", "status": "read",
                     "coverage": "全文", "sha256": sha(MATERIAL), "note": ""}],
        "method": {"kind": "builtin", "name": "annotation-everything 内置范式",
                   "version": "0.9.0", "unavailable": []},
        "links": {"report": "report.html", "script": "script.md",
                  "sha256": {"report": sha(REPORT), "script": sha(script_text)},
                  "sourceNote": "materials/q3-notes.md 及同目录讲稿"},
        "findings": [{"claim": "三季度收入同比增长 12%", "type": "calculation",
                      "sources": ["materials/q3-notes.md"], "pages": ["sum"],
                      "refs": [{"path": "materials/q3-notes.md", "locator": "§1 收入",
                                "quote": "去年同期为 100 万元", "found": True}],
                      "support": "supported",
                      "basis": "对比材料 §1 两期收入原文，并按声明输入复算 percent-change",
                      "calculation": {"op": "percent-change", "inputs": {"a": 100, "b": 112},
                                      "expected": 12,
                                      "unit": "万元", "period": "2025Q3 对 2024Q3",
                                      "definition": "对上年同期收入的增长百分比",
                                      "inputLocations": {
                                          "a": "materials/q3-notes.md §1：去年同期 100 万元",
                                          "b": "materials/q3-notes.md §1：三季度收入 112 万元"}},
                      "note": "收入 112 对上年 100，四舍五入到整数"}],
        "gaps": ["十月的周报缺失"],
        "checks": all_checks(content="not_run", structure="not_run",
                             visual=visual_evidence(status="not_run")),
        "openItems": [{"item": "十月周报待补", "blocking": False}],
        "verdict": {"draftComplete": True, "deliverable": None},
    }
    record.update(overrides)
    return record


def compliant_record(**overrides) -> dict:
    """程序、内容、结构、视觉全部齐备的可交付形态记录。"""
    record = make_record(checks=all_checks())
    record["verdict"] = {"draftComplete": True, "deliverable": True}
    record.update(overrides)
    return record


def write_case(d: Path, *, report: str = REPORT, script: str | None = SCRIPT,
               record: dict | None = None) -> dict:
    d.mkdir(parents=True, exist_ok=True)
    paths = {"report": d / "report.html"}
    paths["report"].write_text(report, encoding="utf-8")
    if script is not None:
        paths["script"] = d / "script.md"
        paths["script"].write_text(script, encoding="utf-8")
    if record is not None:
        paths["record"] = d / "report-record.json"
        paths["record"].write_text(json.dumps(record, ensure_ascii=False, indent=2) + "\n",
                                   encoding="utf-8")
    return paths


def run_check(paths: dict, *, locale: str | None = None,
              argv_extra: list[str] | None = None) -> tuple[int, dict | None, str, str]:
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
    payload = json.loads(text) if "--json" in (argv_extra or []) else None
    return code, payload, text, err.getvalue()


def check_by_id(payload: dict, cid: str) -> dict:
    for item in payload["checks"]:
        if item["id"] == cid:
            return item
    raise AssertionError(f"检查结果里没有 {cid}：{payload['checks']}")


class StructureChecklistTests(unittest.TestCase):
    """结构核验清单（决策 11）：五项内置、覆盖单独汇总、参与可交付判定。"""

    def test_structure_items_are_known_with_structure_kind(self):
        for cid in STRUCTURE_CHECK_IDS:
            self.assertIn(cid, rc.KNOWN_RECORD_CHECKS)
            self.assertEqual(rc.KNOWN_RECORD_CHECKS[cid], "structure")
        kinds = set(rc.KNOWN_RECORD_CHECKS.values())
        self.assertEqual(kinds, {"content", "structure", "visual"})

    def test_full_structure_pass_is_covered_and_deliverable(self):
        d = tmpdir()
        paths = write_case(d, record=compliant_record())
        code, payload, _, _ = run_check(paths, locale="zh-CN", argv_extra=["--json"])
        self.assertEqual(code, 0)
        self.assertEqual(payload["coverage"]["structure"], "covered")
        self.assertEqual(payload["coverageCounts"]["structure"],
                         {"done": len(STRUCTURE_CHECK_IDS),
                          "total": len(STRUCTURE_CHECK_IDS), "failed": 0})
        self.assertTrue(payload["verdict"]["deliverable"])

    def test_undeclared_structure_items_block_deliverable_without_program_failure(self):
        """#72 形态记录（只声明内容与视觉）：不引入程序失败，结构覆盖如实显示未执行。"""
        d = tmpdir()
        legacy_checks = [{"id": cid, "kind": "content", "status": "not_run",
                          "scope": "", "note": ""} for cid in CONTENT_CHECK_IDS]
        legacy_checks.append(visual_evidence(status="not_run"))
        paths = write_case(d, record=make_record(checks=legacy_checks,
                                                 request={"purpose": "季度经营汇报"}))
        code, payload, _, _ = run_check(paths, locale="zh-CN", argv_extra=["--json"])
        self.assertEqual(code, 0, "缺结构清单不引入新的程序检查失败")
        self.assertEqual(check_by_id(payload, "record.checks")["status"], "pass")
        self.assertEqual(check_by_id(payload, "record.visual")["status"], "not_applicable")
        self.assertEqual(payload["coverage"]["structure"], "not_run")
        self.assertFalse(payload["verdict"]["deliverable"])
        self.assertTrue(any("结构" in r for r in payload["verdict"]["reasons"]),
                        payload["verdict"]["reasons"])

    def test_failed_structure_item_blocks_deliverable(self):
        d = tmpdir()
        record = compliant_record()
        for item in record["checks"]:
            if item["id"] == "structure.headings":
                item.update({"status": "fail", "scope": "第 2 页",
                             "note": "标题写「渠道」，正文讲的是成本"})
        paths = write_case(d, record=record)
        code, payload, _, _ = run_check(paths, locale="zh-CN", argv_extra=["--json"])
        self.assertEqual(code, 0, "清单项如实失败是覆盖问题，不是程序检查失败")
        self.assertEqual(payload["coverage"]["structure"], "uncovered")
        self.assertEqual(payload["coverageCounts"]["structure"]["failed"], 1)
        self.assertFalse(payload["verdict"]["deliverable"])
        self.assertTrue(payload["verdict"]["draftComplete"])

    def test_partial_structure_checklist_cannot_cover(self):
        d = tmpdir()
        record = compliant_record(checks=all_checks(structure="not_run"))
        paths = write_case(d, record=record)
        _, payload, _, _ = run_check(paths, locale="zh-CN", argv_extra=["--json"])
        self.assertEqual(payload["coverage"]["structure"], "not_run")
        self.assertFalse(payload["verdict"]["deliverable"])


class ScriptScopeTests(unittest.TestCase):
    """讲稿关联项的适用性规则：无讲稿不计入总数，声明 pass 即虚假的已检查。"""

    def no_script_record(self, **overrides) -> dict:
        record = make_record(**overrides)
        record["links"]["script"] = None
        record["links"]["sha256"]["script"] = None
        record["request"]["script"] = "no"
        return record

    def test_script_scoped_items_excluded_from_totals_without_script(self):
        d = tmpdir()
        paths = write_case(d, script=None, record=self.no_script_record())
        code, payload, _, _ = run_check(paths, locale="zh-CN", argv_extra=["--json"])
        self.assertEqual(code, 0)
        n_content = len(CONTENT_CHECK_IDS) - 1     # script-consistency 不适用
        n_structure = len(STRUCTURE_CHECK_IDS) - 1  # structure.length 不适用
        self.assertEqual(payload["coverageCounts"]["content"]["total"], n_content)
        self.assertEqual(payload["coverageCounts"]["structure"]["total"], n_structure)

    def test_script_scoped_items_counted_with_script(self):
        d = tmpdir()
        paths = write_case(d, record=make_record())
        _, payload, _, _ = run_check(paths, locale="zh-CN", argv_extra=["--json"])
        self.assertEqual(payload["coverageCounts"]["content"]["total"],
                         len(CONTENT_CHECK_IDS))
        self.assertEqual(payload["coverageCounts"]["structure"]["total"],
                         len(STRUCTURE_CHECK_IDS))

    def test_script_scoped_pass_without_script_is_false_checked(self):
        """无讲稿却声明 script-consistency / structure.length 已通过：程序检查失败。"""
        for scoped in SCRIPT_SCOPED_IDS:
            with self.subTest(scoped=scoped):
                d = tmpdir()
                record = compliant_record(request={"purpose": "季度经营汇报",
                                                   "script": "no"})
                for item in record["checks"]:
                    if item["id"] == scoped:
                        item["status"] = "pass"
                paths = write_case(d, script=None, record=record)
                code, payload, _, _ = run_check(paths, locale="zh-CN", argv_extra=["--json"])
                self.assertEqual(code, 1, "无讲稿声明讲稿关联项通过是虚假的已检查")
                item = check_by_id(payload, "record.checks")
                self.assertEqual(item["status"], "fail")
                self.assertIn(scoped, item["detail"])
                self.assertFalse(payload["verdict"]["deliverable"])


class LengthConstraintTests(unittest.TestCase):
    """篇幅约束（record.length）：request.length 可解析为页数时与报告页数比对。"""

    def test_matching_page_count_passes(self):
        d = tmpdir()
        paths = write_case(d, record=make_record())
        code, payload, _, _ = run_check(paths, locale="zh-CN", argv_extra=["--json"])
        self.assertEqual(code, 0)
        self.assertEqual(check_by_id(payload, "record.length")["status"], "pass")

    def test_mismatched_page_count_fails_but_draft_stands(self):
        d = tmpdir()
        four_pages = REPORT.replace(
            "</body></html>",
            '<section class="page" data-page="4" data-key="extra" id="page-extra">'
            "<h2>附录</h2><p>补充页。</p></section></body></html>")
        four_script = SCRIPT + "\n### 第 4 页｜附录\n**讲法**：\n补充页讲一句。\n"
        paths = write_case(d, report=four_pages, script=four_script,
                           record=make_record(script_text=four_script,
                                              request={"purpose": "季度经营汇报",
                                                       "length": "3 页"}))
        code, payload, text, _ = run_check(paths, locale="zh-CN", argv_extra=["--json"])
        self.assertEqual(code, 1)
        item = check_by_id(payload, "record.length")
        self.assertEqual(item["status"], "fail")
        self.assertEqual(item["code"], "record.length-mismatch")
        self.assertFalse(payload["verdict"]["deliverable"])
        self.assertTrue(payload["verdict"]["draftComplete"],
                        "篇幅不符不阻塞初稿进入审阅")
        self.assertIn("篇幅", text)

    def test_range_constraint_passes_within_bounds(self):
        d = tmpdir()
        paths = write_case(d, record=make_record(request={"purpose": "季度经营汇报",
                                                          "length": "2–5 页"}))
        code, payload, _, _ = run_check(paths, locale="zh-CN", argv_extra=["--json"])
        self.assertEqual(code, 0)
        self.assertEqual(check_by_id(payload, "record.length")["status"], "pass")

    def test_range_constraint_fails_outside_bounds(self):
        d = tmpdir()
        paths = write_case(d, record=make_record(request={"purpose": "季度经营汇报",
                                                          "length": "5-8 页"}))
        code, payload, _, _ = run_check(paths, locale="zh-CN", argv_extra=["--json"])
        self.assertEqual(code, 1)
        self.assertEqual(check_by_id(payload, "record.length")["code"],
                         "record.length-mismatch")

    def test_english_pages_constraint(self):
        d = tmpdir()
        paths = write_case(d, record=make_record(request={"purpose": "Quarterly review",
                                                          "length": "3 pages"}))
        code, payload, _, _ = run_check(paths, locale="en", argv_extra=["--json"])
        self.assertEqual(code, 0)
        self.assertEqual(check_by_id(payload, "record.length")["status"], "pass")

    def test_duration_only_not_compared(self):
        """时长约束不折算页数：不可解析为页数时判不适用，不虚假比较。"""
        d = tmpdir()
        paths = write_case(d, record=make_record(request={"purpose": "季度经营汇报",
                                                          "length": "10 分钟讲稿"}))
        code, payload, _, _ = run_check(paths, locale="zh-CN", argv_extra=["--json"])
        self.assertEqual(code, 0)
        self.assertEqual(check_by_id(payload, "record.length")["status"], "not_applicable")

    def test_missing_length_or_record_not_applicable(self):
        d = tmpdir()
        paths = write_case(d, record=make_record(request={"purpose": "季度经营汇报"}))
        _, payload, _, _ = run_check(paths, locale="zh-CN", argv_extra=["--json"])
        self.assertEqual(check_by_id(payload, "record.length")["status"], "not_applicable")
        paths = write_case(tmpdir())
        _, payload, _, _ = run_check(paths, locale="zh-CN", argv_extra=["--json"])
        self.assertEqual(check_by_id(payload, "record.length")["status"], "not_applicable")


class VisualEvidenceTests(unittest.TestCase):
    """视觉逐页证据（record.visual）：声明与证据一致才可通过；不符要能暴露。"""

    def test_pass_without_evidence_fails(self):
        """声明已逐页检查通过但没有每页证据：程序检查失败。"""
        d = tmpdir()
        record = compliant_record(checks=all_checks(visual=visual_evidence(None)))
        paths = write_case(d, record=record)
        code, payload, text, _ = run_check(paths, locale="zh-CN", argv_extra=["--json"])
        self.assertEqual(code, 1)
        item = check_by_id(payload, "record.visual")
        self.assertEqual(item["status"], "fail")
        self.assertIn("证据", item["detail"])
        self.assertFalse(payload["verdict"]["deliverable"])

    def test_full_evidence_passes_and_covers_visual(self):
        d = tmpdir()
        paths = write_case(d, record=compliant_record())
        code, payload, _, _ = run_check(paths, locale="zh-CN", argv_extra=["--json"])
        self.assertEqual(code, 0)
        self.assertEqual(check_by_id(payload, "record.visual")["status"], "pass")
        self.assertEqual(payload["coverage"]["visual"], "covered")
        self.assertTrue(payload["verdict"]["deliverable"])

    def test_missing_page_coverage_fails(self):
        """声明已逐页检查但证据缺第 3 页：不能通过。"""
        d = tmpdir()
        record = compliant_record()
        for item in record["checks"]:
            if item["id"] == "visual.pages":
                item["pages"] = [{"page": 1, "checked": True},
                                 {"page": 2, "checked": True}]
        paths = write_case(d, record=record)
        code, payload, _, _ = run_check(paths, locale="zh-CN", argv_extra=["--json"])
        self.assertEqual(code, 1)
        item = check_by_id(payload, "record.visual")
        self.assertEqual(item["code"], "record.visual-coverage")
        self.assertIn("3", item["detail"])
        self.assertFalse(payload["verdict"]["deliverable"])

    def test_evidence_referencing_unknown_page_fails(self):
        d = tmpdir()
        record = compliant_record()
        for item in record["checks"]:
            if item["id"] == "visual.pages":
                item["pages"] = full_pages() + [{"page": 9, "checked": True}]
        paths = write_case(d, record=record)
        code, payload, _, _ = run_check(paths, locale="zh-CN", argv_extra=["--json"])
        self.assertEqual(code, 1)
        self.assertEqual(check_by_id(payload, "record.visual")["code"],
                         "record.visual-coverage")

    def test_unchecked_page_blocks_pass(self):
        """证据里有未检查的页却声明逐页通过：程序检查失败。"""
        d = tmpdir()
        record = compliant_record()
        for item in record["checks"]:
            if item["id"] == "visual.pages":
                item["pages"] = [{"page": 1, "checked": True},
                                 {"page": 2, "checked": True},
                                 {"page": 3, "checked": False,
                                  "note": "渲染中断，未检查"}]
        paths = write_case(d, record=record)
        code, payload, _, _ = run_check(paths, locale="zh-CN", argv_extra=["--json"])
        self.assertEqual(code, 1)
        self.assertEqual(check_by_id(payload, "record.visual")["code"],
                         "record.visual-unchecked")
        self.assertFalse(payload["verdict"]["deliverable"])

    def test_unresolved_issue_with_pass_fails(self):
        """带未解决的问题（溢出）却声明通过：程序检查失败。"""
        d = tmpdir()
        record = compliant_record()
        for item in record["checks"]:
            if item["id"] == "visual.pages":
                item["pages"] = [{"page": 1, "checked": True, "issues": []},
                                 {"page": 2, "checked": True,
                                  "issues": ["表格溢出画布右缘约 40px"]},
                                 {"page": 3, "checked": True, "issues": []}]
        paths = write_case(d, record=record)
        code, payload, text, _ = run_check(paths, locale="zh-CN", argv_extra=["--json"])
        self.assertEqual(code, 1)
        item = check_by_id(payload, "record.visual")
        self.assertEqual(item["code"], "record.visual-issue")
        self.assertIn("溢出", item["detail"])
        self.assertFalse(payload["verdict"]["deliverable"])

    def test_honest_fail_with_issue_is_consistent_and_blocks_deliverable(self):
        """如实记录失败：程序检查通过（声明与证据一致），覆盖如实降级，不可交付。"""
        d = tmpdir()
        record = compliant_record()
        record["verdict"] = {"draftComplete": True, "deliverable": False}
        for item in record["checks"]:
            if item["id"] == "visual.pages":
                item.update(visual_evidence(
                    [{"page": 1, "checked": True, "issues": []},
                     {"page": 2, "checked": True,
                      "issues": ["图表文字小于 12px，不可读"]},
                     {"page": 3, "checked": True, "issues": []}],
                    status="fail"))
        paths = write_case(d, record=record)
        code, payload, _, _ = run_check(paths, locale="zh-CN", argv_extra=["--json"])
        self.assertEqual(code, 0, "如实记录的视觉失败不产生程序检查失败")
        self.assertEqual(check_by_id(payload, "record.visual")["status"], "pass")
        self.assertEqual(payload["coverage"]["visual"], "uncovered")
        self.assertEqual(payload["coverageCounts"]["visual"]["failed"], 1)
        self.assertFalse(payload["verdict"]["deliverable"])

    def test_interrupted_check_recorded_not_run(self):
        """检查中断：未检查的页如实记录，整体声明 not_run，不算已检查。"""
        d = tmpdir()
        record = compliant_record()
        record["verdict"] = {"draftComplete": True, "deliverable": None}
        for item in record["checks"]:
            if item["id"] == "visual.pages":
                item.update(visual_evidence(
                    [{"page": 1, "checked": True, "issues": []},
                     {"page": 2, "checked": False, "note": "渲染中断"},
                     {"page": 3, "checked": False, "note": "渲染中断"}],
                    status="not_run"))
        paths = write_case(d, record=record)
        code, payload, _, _ = run_check(paths, locale="zh-CN", argv_extra=["--json"])
        self.assertEqual(code, 0)
        self.assertEqual(check_by_id(payload, "record.visual")["status"], "pass",
                         "声明 not_run 且如实记录中断，与证据一致")
        self.assertEqual(payload["coverage"]["visual"], "not_run")
        self.assertFalse(payload["verdict"]["deliverable"])
        self.assertTrue(any("视觉" in r for r in payload["verdict"]["reasons"]),
                        payload["verdict"]["reasons"])

    def test_unavailable_viewing_capability_recorded(self):
        """缺少查看能力：unavailable 如实登记，不能以程序检查通过代替视觉通过。"""
        d = tmpdir()
        record = compliant_record()
        record["verdict"] = {"draftComplete": True, "deliverable": None}
        for item in record["checks"]:
            if item["id"] == "visual.pages":
                item.update({"status": "unavailable",
                             "scope": "宿主无页面渲染能力",
                             "note": "无法渲染页面，未执行视觉检查"})
                item.pop("pages", None)
        paths = write_case(d, record=record)
        code, payload, _, _ = run_check(paths, locale="zh-CN", argv_extra=["--json"])
        self.assertEqual(code, 0)
        self.assertNotEqual(payload["coverage"]["visual"], "covered")
        self.assertFalse(payload["verdict"]["deliverable"])

    def test_malformed_evidence_fails(self):
        d = tmpdir()
        record = compliant_record()
        for item in record["checks"]:
            if item["id"] == "visual.pages":
                item["pages"] = "全部页面都看过了"
        paths = write_case(d, record=record)
        code, payload, _, _ = run_check(paths, locale="zh-CN", argv_extra=["--json"])
        self.assertEqual(code, 1)
        self.assertEqual(check_by_id(payload, "record.visual")["code"],
                         "record.visual-bad")

    def test_malformed_entry_fails(self):
        d = tmpdir()
        record = compliant_record()
        for item in record["checks"]:
            if item["id"] == "visual.pages":
                item["pages"] = [{"page": "第一页", "checked": "yes"}]
        paths = write_case(d, record=record)
        code, payload, _, _ = run_check(paths, locale="zh-CN", argv_extra=["--json"])
        self.assertEqual(code, 1)
        self.assertEqual(check_by_id(payload, "record.visual")["code"],
                         "record.visual-bad")

    def test_no_visual_declaration_not_applicable(self):
        """没有视觉声明（旧形态）：record.visual 不适用，覆盖走 not_run。"""
        d = tmpdir()
        record = make_record(checks=[{"id": "content.requirements", "kind": "content",
                                      "status": "not_run", "scope": "", "note": ""}])
        paths = write_case(d, record=record)
        code, payload, _, _ = run_check(paths, locale="zh-CN", argv_extra=["--json"])
        self.assertEqual(code, 1, "缺视觉类声明按 #72 契约仍是程序检查失败")
        self.assertEqual(check_by_id(payload, "record.checks")["code"],
                         "record.checks-empty")
        self.assertEqual(check_by_id(payload, "record.visual")["status"],
                         "not_applicable")


class DeliverableMergeTests(unittest.TestCase):
    """合并判定：程序、内容、结构、视觉共同决定可交付；初稿与可交付分别输出。"""

    def test_correct_case_is_deliverable(self):
        d = tmpdir()
        paths = write_case(d, record=compliant_record())
        code, payload, _, _ = run_check(paths, locale="zh-CN", argv_extra=["--json"])
        self.assertEqual(code, 0)
        self.assertTrue(payload["verdict"]["draftComplete"])
        self.assertTrue(payload["verdict"]["deliverable"])
        self.assertEqual(payload["coverage"]["content"], "covered")
        self.assertEqual(payload["coverage"]["structure"], "covered")
        self.assertEqual(payload["coverage"]["visual"], "covered")
        _, _, text, _ = run_check(paths, locale="zh-CN")
        self.assertIn("初稿生成：已完成", text)
        self.assertIn("报告可交付：是", text)

    def test_missing_page_example_not_deliverable(self):
        """反例：声明逐页检查但记录缺页覆盖，不能通过整体交付验收。"""
        d = tmpdir()
        record = compliant_record()
        for item in record["checks"]:
            if item["id"] == "visual.pages":
                item["pages"] = [{"page": 1, "checked": True},
                                 {"page": 2, "checked": True}]
        paths = write_case(d, record=record)
        code, payload, _, _ = run_check(paths, locale="zh-CN", argv_extra=["--json"])
        self.assertEqual(code, 1)
        self.assertFalse(payload["verdict"]["deliverable"])

    def test_unexecuted_required_check_not_deliverable(self):
        """反例：未执行必要检查（结构清单留 not_run），不能通过整体交付验收。"""
        d = tmpdir()
        paths = write_case(d, record=compliant_record(
            checks=all_checks(structure="not_run")))
        _, payload, _, _ = run_check(paths, locale="zh-CN", argv_extra=["--json"])
        self.assertFalse(payload["verdict"]["deliverable"])

    def test_claim_without_evidence_blocks_deliverable(self):
        """反例：核心结论缺依据（无 support / refs），不能通过整体交付验收。"""
        d = tmpdir()
        record = compliant_record()
        for key in ("refs", "support", "basis"):
            record["findings"][0].pop(key, None)
        paths = write_case(d, record=record)
        code, payload, _, _ = run_check(paths, locale="zh-CN", argv_extra=["--json"])
        self.assertEqual(code, 0, "缺声明是待核实，不是程序失败")
        self.assertEqual(payload["coverage"]["content"], "not_run")
        self.assertEqual(payload["coverage"]["structure"], "not_run",
                         "结论支撑证据不足时结构核验同样不能显示为已覆盖")
        self.assertTrue(payload["coverageCapped"]["structure"])
        self.assertFalse(payload["verdict"]["deliverable"])
        self.assertTrue(payload["verdict"]["draftComplete"])

    def test_nonblocking_items_disclosed_without_blocking_deliverable(self):
        d = tmpdir()
        record = compliant_record()
        record["openItems"] = [{"item": "附录页码样式", "blocking": False}]
        record["gaps"] = ["十月的周报缺失"]
        paths = write_case(d, record=record)
        code, payload, text, _ = run_check(paths, locale="zh-CN", argv_extra=["--json"])
        self.assertEqual(code, 0)
        self.assertTrue(payload["verdict"]["deliverable"])
        self.assertIn("十月的周报缺失", text)
        self.assertEqual(payload["record"]["openItems"], {"n": 1, "blocking": 0})

    def test_blocking_item_blocks_deliverable(self):
        d = tmpdir()
        record = compliant_record()
        record["openItems"] = [{"item": "核心口径二选一待用户拍板", "blocking": True}]
        paths = write_case(d, record=record)
        _, payload, _, _ = run_check(paths, locale="zh-CN", argv_extra=["--json"])
        self.assertFalse(payload["verdict"]["deliverable"])
        self.assertTrue(payload["verdict"]["draftComplete"])

    def test_stale_record_cannot_back_current_report(self):
        d = tmpdir()
        record = compliant_record()
        paths = write_case(d, report=REPORT.replace("两件事", "三件事"), record=record)
        code, payload, _, _ = run_check(paths, locale="zh-CN", argv_extra=["--json"])
        self.assertEqual(code, 1)
        self.assertFalse(payload["record"]["fresh"])
        for kind in ("content", "structure", "visual"):
            self.assertEqual(payload["coverage"][kind], "unverified",
                             f"过期记录的 {kind} 覆盖不能沿用")
        self.assertFalse(payload["verdict"]["deliverable"])

    def test_verdict_declared_false_not_overridden(self):
        """程序复核不支持记录里虚假的可交付声明：记录声明 false 时维持否。"""
        d = tmpdir()
        record = compliant_record()
        record["verdict"] = {"draftComplete": True, "deliverable": False}
        paths = write_case(d, record=record)
        _, payload, _, _ = run_check(paths, locale="zh-CN", argv_extra=["--json"])
        self.assertFalse(payload["verdict"]["deliverable"])


class DisclosureTests(unittest.TestCase):
    """摘要披露：三类覆盖、已检查范围、阻塞项出自同一份结构化结果。"""

    def test_summary_discloses_three_kinds_and_scope(self):
        d = tmpdir()
        record = compliant_record()
        for item in record["checks"]:
            if item["id"] == "visual.pages":
                item["scope"] = "全部 3 页（1280×720 实际渲染，逐页记录）"
        paths = write_case(d, record=record)
        _, payload, _, _ = run_check(paths, locale="zh-CN", argv_extra=["--json"])
        self.assertEqual(len(payload["record"]["scopes"]), 14)
        _, _, zh, _ = run_check(paths, locale="zh-CN")
        self.assertIn("结构核验", zh)
        self.assertIn("视觉核验", zh)
        self.assertIn("全部 3 页", zh)
        _, _, en, _ = run_check(paths, locale="en")
        self.assertIn("Structure", en)
        self.assertIn("Visual", en)

    def test_structured_fields_language_invariant(self):
        d = tmpdir()
        paths = write_case(d, record=compliant_record())
        _, zh, _, _ = run_check(paths, locale="zh-CN", argv_extra=["--json"])
        _, en, _, _ = run_check(paths, locale="en", argv_extra=["--json"])
        self.assertEqual([c["id"] for c in zh["checks"]], [c["id"] for c in en["checks"]])
        self.assertEqual(zh["coverage"], en["coverage"])
        self.assertEqual(zh["coverageCounts"], en["coverageCounts"])
        self.assertEqual(zh["coverageCapped"], en["coverageCapped"])
        self.assertEqual(zh["verdict"]["draftComplete"], en["verdict"]["draftComplete"])
        self.assertEqual(zh["verdict"]["deliverable"], en["verdict"]["deliverable"])

    def test_check_order_stays_stable(self):
        """新程序检查追加在尾部，既有检查项的顺序不回退（#74/#75 追加 method、revision）。"""
        d = tmpdir()
        paths = write_case(d, record=make_record())
        _, payload, _, _ = run_check(paths, locale="zh-CN", argv_extra=["--json"])
        ids = [c["id"] for c in payload["checks"]]
        self.assertEqual(len(ids), len(rc.Checks.ORDER))
        self.assertEqual(ids[-3:], ["record.length", "record.method", "record.revision"])


if __name__ == "__main__":
    unittest.main()
