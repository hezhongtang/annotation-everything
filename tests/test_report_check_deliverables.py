"""核验记录额外交付（PPTX）判定契约测试（issue #180）。

接缝：report_check.check_report 对 `ae-report-record/1` 可选 deliverables[] 的
程序判定——产物存在、哈希新鲜、调用依据、页面覆盖、图片证据绑定、跨格式
数字一致性、PowerPoint 实测披露，以及必需而未完成时整项请求的缺口披露。
全部材料由标准库现场合成（deck 用内置制作器构建）。
"""
from __future__ import annotations

import hashlib
import json
import os
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "skill" / "annotation-everything" / "scripts"
sys.path.insert(0, str(SCRIPTS))
os.environ.setdefault("AE_HOME", tempfile.mkdtemp(prefix="ae-test-home-"))

import report_check as rc  # noqa: E402
import pptx_build as pb  # noqa: E402

RECORD_SCHEMA = "ae-report-record/1"
REPORT = """<!DOCTYPE html><html><head><meta charset="utf-8"><title>合成报告</title></head>
<body><section class="page" data-page="1" data-key="sum"><h2>三季度收入</h2>
<p>三季度收入同比增长 12%。<span class="ae-source">materials/q3-notes.md §1</span></p></section>
<section class="page" data-page="2" data-key="rev"><h2>要点</h2><p>第二页。</p></section></body></html>"""
SCRIPT = "# 讲稿\n### 第 1 页｜三季度收入\n### 第 2 页｜要点\n"
MATERIAL = "三季度收入 112 万元，去年同期 100 万元。"


def sha(data) -> str:
    if isinstance(data, str):
        data = data.encode("utf-8")
    return hashlib.sha256(data).hexdigest()


def make_record(**overrides) -> dict:
    """最小结构完整记录；deliverables 由各用例自行添加。"""
    record = {
        "schema": RECORD_SCHEMA,
        "generatedAt": "2026-11-02T09:00:00+08:00",
        "request": {"purpose": "季度经营汇报", "audience": "管理层",
                    "coreQuestions": ["三季度收入为什么增长"], "length": "2 页",
                    "script": "yes"},
        "sources": [{"path": "materials/q3-notes.md", "status": "read",
                     "coverage": "全文", "sha256": sha(MATERIAL), "note": ""}],
        "method": {"kind": "builtin", "name": "annotation-everything 内置范式",
                   "version": "0.12.1", "unavailable": []},
        "links": {"report": "report.html", "script": "script.md",
                  "sha256": {"report": sha(REPORT), "script": sha(SCRIPT)},
                  "sourceNote": "材料旁"},
        "findings": [{"claim": "三季度收入同比增长 12%", "type": "calculation",
                      "sources": ["materials/q3-notes.md"], "pages": ["sum"],
                      "refs": [{"path": "materials/q3-notes.md", "locator": "§1",
                                "quote": "去年同期为 100 万元", "found": True}],
                      "support": "supported",
                      "basis": "对比两期原文并复算",
                      "calculation": {"op": "percent-change", "inputs": {"a": 100, "b": 112},
                                      "expected": 12, "unit": "万元",
                                      "period": "2025Q3 对 2024Q3",
                                      "definition": "对上年同期收入的增长百分比",
                                      "inputLocations": {"a": "§1", "b": "§1"}}}],
        "gaps": [], "openItems": [],
        "checks": [{"id": "content.requirements", "status": "not_run", "scope": "", "note": ""},
                   {"id": "content.claims", "status": "not_run", "scope": "", "note": ""},
                   {"id": "content.fact-vs-inference", "status": "not_run", "scope": "", "note": ""},
                   {"id": "content.units-dates", "status": "not_run", "scope": "", "note": ""},
                   {"id": "content.numbers-charts", "status": "not_run", "scope": "", "note": ""},
                   {"id": "content.cross-page", "status": "not_run", "scope": "", "note": ""},
                   {"id": "content.script-consistency", "status": "not_run", "scope": "", "note": ""},
                   {"id": "content.open-questions", "status": "not_run", "scope": "", "note": ""},
                   {"id": "structure.flow", "status": "not_run", "scope": "", "note": ""},
                   {"id": "structure.headings", "status": "not_run", "scope": "", "note": ""},
                   {"id": "structure.claim-support", "status": "not_run", "scope": "", "note": ""},
                   {"id": "structure.dup-gaps", "status": "not_run", "scope": "", "note": ""},
                   {"id": "structure.length", "status": "not_run", "scope": "", "note": ""},
                   {"id": "visual.pages", "status": "not_run", "scope": "", "note": ""}],
        "verdict": {"draftComplete": True, "deliverable": None},
    }
    record.update(overrides)
    return record


def deck_spec(claim_text: str = "三季度收入同比增长 12%") -> dict:
    return {
        "schema": "ae-pptx-deck/1",
        "title": "合成演示文稿",
        "theme": {"colors": {"dk1": "1F2933", "lt1": "FFFFFF", "dk2": "3E4C59",
                             "lt2": "F5F7FA", "accent1": "2B6CB0", "accent2": "2C7A7B",
                             "accent3": "B7791F", "accent4": "6B46C1", "accent5": "C05621",
                             "accent6": "975FA8", "hlink": "2B6CB0", "folHlink": "805AD5"},
                  "fonts": {"major": {"latin": "Synthetic Display", "ea": "合成标题字体"},
                            "minor": {"latin": "Synthetic Text", "ea": "合成正文字体"}}},
        "slides": [
            {"key": "cover", "layout": "title", "title": "合成封面"},
            {"key": "sum", "layout": "titleAndContent", "title": "收入结论",
             "body": [claim_text]},
        ],
    }


FAKE_PNG = (b"\x89PNG\r\n\x1a\n" + b"\x00\x00\x00\x0dIHDR"
            + (4).to_bytes(4, "big") + (4).to_bytes(4, "big") + b"rest")


class DeliverablesCheckTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="ae-deliverables-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.deck_path, self.deck_sha = self._deck()
        self.images = self._pages([1, 2])

    # ── 现场产物 ────────────────────────────────────────────────────

    def _deck(self, claim_text: str = "三季度收入同比增长 12%", name="deck.pptx"):
        out = self.root / name
        summary = pb.build_deck(deck_spec(claim_text), out)
        return out, summary["sha256"]

    def _pages(self, numbers, prefix="pages"):
        directory = self.root / prefix
        directory.mkdir(exist_ok=True)
        entries = []
        for number in numbers:
            image = directory / f"page-{number:02d}.png"
            image.write_bytes(FAKE_PNG)
            entries.append({"page": number, "image": f"{prefix}/page-{number:02d}.png",
                            "sha256": sha(FAKE_PNG), "checked": True, "issues": []})
        return entries

    def deliverable(self, **overrides) -> dict:
        item = {
            "kind": "pptx", "required": True,
            "file": "deck.pptx", "sha256": self.deck_sha, "pages": 2,
            "build": {"tool": "annotation-everything 内置 pptx_build.py",
                      "version": "0.12.1",
                      "invocation": "python3 $S/pptx_build.py --deck deck.json --out deck.pptx",
                      "deckSpec": "deck.json", "deckSpecSha256": sha("{}")},
            "render": {"tool": "LibreOffice soffice", "version": "26.07",
                       "pptxSha256": self.deck_sha,
                       "notes": "pdftoppm 24.08"},
            "checks": {
                "file": {"id": "pptx.file", "status": "pass", "note": "pptx_check 全部通过",
                         "issues": []},
                "visual": {"id": "pptx.visual", "status": "pass", "note": "逐页看图完成",
                           "pages": self.images},
                "consistency": {"id": "pptx.consistency", "status": "pass",
                                "note": "关键数字逐条核对",
                                "findings": [{"claim": "三季度收入同比增长 12%",
                                              "pages": [2]}]},
            },
            "powerpoint": {"status": "not_run", "note": "本机无 PowerPoint，待 PR review 实测"},
            "openIssues": [], "note": "",
        }
        item.update(overrides)
        return item

    def write_case(self, record: dict, *, deck=True) -> Path:
        (self.root / "report.html").write_text(REPORT, encoding="utf-8")
        (self.root / "script.md").write_text(SCRIPT, encoding="utf-8")
        (self.root / "materials").mkdir(exist_ok=True)
        (self.root / "materials" / "q3-notes.md").write_text(MATERIAL, encoding="utf-8")
        (self.root / "report-record.json").write_text(
            json.dumps(record, ensure_ascii=False), encoding="utf-8")
        if deck and not self.deck_path.exists():
            self._deck()
        return self.root / "report-record.json"

    def run_check(self, record: dict) -> dict:
        self.write_case(record)
        return rc.check_report(self.root / "report.html",
                               self.root / "script.md",
                               self.root / "report-record.json",
                               locale="zh-CN")

    def item(self, result: dict) -> dict | None:
        found = result.get("deliverables") or []
        return found[0] if found else None

    def status(self, result: dict) -> str:
        return next(c["status"] for c in result["checks"] if c["id"] == "record.deliverables")

    # ── 用例 ────────────────────────────────────────────────────────

    def test_record_without_deliverables_stays_untouched(self):
        """旧记录没有 deliverables：判定不适用，不误读为已检查或失败。"""
        result = self.run_check(make_record())
        self.assertEqual(self.status(result), "not_applicable")
        self.assertEqual(result["deliverables"], [])

    def test_valid_deliverable_passes_and_is_deliverable(self):
        """成功形态：文件检查、逐页看图、内容一致性齐备，判定可交付并披露实测状态。"""
        record = make_record(deliverables=[self.deliverable()])
        result = self.run_check(record)
        self.assertEqual(self.status(result), "pass")
        item = self.item(result)
        self.assertTrue(item["deliverable"])
        self.assertEqual(item["powerpoint"], "not_run")
        # HTML 与 PPTX 各自判定：整项 deliverable 不因 PPTX 可交付而改变语义
        self.assertIn(result["verdict"]["deliverable"], (True, False, None))

    def test_missing_file_and_stale_hash_fail(self):
        """产物不存在与哈希陈旧（改动文件沿用旧证据）都判失败。"""
        record = make_record(deliverables=[self.deliverable(file="不存在的deck.pptx")])
        result = self.run_check(record)
        self.assertEqual(self.status(result), "fail")
        codes = self._codes(result)
        self.assertIn("record.deliverable-missing", codes)
        self.assertFalse(self.item(result)["deliverable"])

        record2 = make_record(deliverables=[self.deliverable(sha256="0" * 64)])
        result2 = self.run_check(record2)
        self.assertEqual(self.status(result2), "fail")
        self.assertIn("record.deliverable-stale", self._codes(result2))
        self.assertFalse(self.item(result2)["deliverable"])

    def _codes(self, result: dict) -> list[str]:
        entry = next(c for c in result["checks"] if c["id"] == "record.deliverables")
        return [p["code"] for p in entry.get("problems", [])]

    def test_missing_invocation_fails(self):
        """缺调用依据：制作方式无从证明，判失败。"""
        item = self.deliverable()
        item["build"] = {"tool": "", "version": None, "invocation": ""}
        result = self.run_check(make_record(deliverables=[item]))
        self.assertEqual(self.status(result), "fail")
        self.assertFalse(self.item(result)["deliverable"])

    def test_page_coverage_gaps_fail(self):
        """漏页、checked=false 声 pass、页数与实际幻灯片不符都判失败。"""
        short = self.deliverable()
        short["checks"]["visual"]["pages"] = self.images[:1]
        result = self.run_check(make_record(deliverables=[short]))
        self.assertEqual(self.status(result), "fail")

        unchecked = self.deliverable()
        unchecked["checks"]["visual"]["pages"][1]["checked"] = False
        result2 = self.run_check(make_record(deliverables=[unchecked]))
        self.assertEqual(self.status(result2), "fail")

        wrong_count = self.deliverable(pages=5)
        result3 = self.run_check(make_record(deliverables=[wrong_count]))
        self.assertEqual(self.status(result3), "fail")

    def test_unbound_image_evidence_fails(self):
        """图片文件缺失或哈希不匹配：证据没有绑定到被检查的文件版本，判失败。"""
        missing = self.deliverable()
        missing["checks"]["visual"]["pages"][0]["image"] = "pages/没有这张图.png"
        result = self.run_check(make_record(deliverables=[missing]))
        self.assertEqual(self.status(result), "fail")

        stale_image = self.deliverable()
        stale_image["checks"]["visual"]["pages"][0]["sha256"] = "f" * 64
        result2 = self.run_check(make_record(deliverables=[stale_image]))
        self.assertEqual(self.status(result2), "fail")

    def test_open_issues_with_pass_claims_fail(self):
        """带着未解决问题声明通过：可确定的矛盾，判失败。"""
        item = self.deliverable(openIssues=["图表配色待用户确认"])
        result = self.run_check(make_record(deliverables=[item]))
        self.assertEqual(self.status(result), "fail")
        self.assertFalse(self.item(result)["deliverable"])

    def test_page_issues_with_pass_claim_fail(self):
        """合成视觉缺陷反例：页面带 issues 却声明 pass，不能通过。"""
        item = self.deliverable()
        item["checks"]["visual"]["pages"][0]["issues"] = ["标题溢出画布"]
        result = self.run_check(make_record(deliverables=[item]))
        self.assertEqual(self.status(result), "fail")
        self.assertIn("record.deliverable-visual", self._codes(result))
        self.assertFalse(self.item(result)["deliverable"])

    def test_powerpoint_declaration_needs_evidence(self):
        """实测声明 pass 必须带系统与版本；要求逐份实测而缺实测阻止可交付。"""
        bare = self.deliverable(powerpoint={"status": "pass"})
        result = self.run_check(make_record(deliverables=[bare]))
        self.assertEqual(self.status(result), "fail")
        self.assertFalse(self.item(result)["deliverable"])

        bare["requirePowerpoint"] = True
        result = self.run_check(make_record(deliverables=[bare]))
        self.assertFalse(self.item(result)["deliverable"])

        grounded = self.deliverable(
            powerpoint={"status": "pass", "os": "Windows 11 23H2",
                        "version": "PowerPoint 2024 17932.20100", "build": "17932.20100",
                        "note": "合成样例实测"})
        result2 = self.run_check(make_record(deliverables=[grounded]))
        self.assertEqual(self.status(result2), "pass")
        self.assertTrue(self.item(result2)["deliverable"])

        required_but_unverified = self.deliverable(requirePowerpoint=True)
        result3 = self.run_check(make_record(deliverables=[required_but_unverified]))
        self.assertEqual(self.status(result3), "pass")  # 诚实 not_run 不是矛盾
        self.assertFalse(self.item(result3)["deliverable"])
        self.assertTrue(any("PPTX" in reason for reason in result3["verdict"]["reasons"]))

    def test_cross_format_number_mismatch_fails_with_location(self):
        """合成的跨格式数字不一致反例：一致性声明 pass 不能通过，并给出页码。"""
        # 重写同名文件：数字换成 21%，并同步记录哈希——只有数字不一致这一处矛盾
        _, self.deck_sha = self._deck(claim_text="三季度收入同比增长 21%")
        item = self.deliverable()
        result = self.run_check(make_record(deliverables=[item]))
        self.assertEqual(self.status(result), "fail")
        problems = [p for c in result["checks"] if c["id"] == "record.deliverables"
                    for p in c.get("problems", [])]
        self.assertTrue(any("consistency" in p["code"] for p in problems))
        self.assertFalse(self.item(result)["deliverable"])

    def test_unmapped_findings_fail(self):
        """结论没有保留对应关系（consistency.findings 漏映射）判失败。"""
        item = self.deliverable()
        item["checks"]["consistency"]["findings"] = []
        result = self.run_check(make_record(deliverables=[item]))
        self.assertEqual(self.status(result), "fail")

    def test_required_but_not_completed_discloses_gap(self):
        """必需而未完成（无有效产物）：程序不判矛盾，但保留整项请求缺口。"""
        item = {"kind": "pptx", "required": True, "file": None, "status": "unavailable",
                "note": "宿主缺 LibreOffice，暂无法渲染", "resume": "安装后可继续"}
        result = self.run_check(make_record(deliverables=[item]))
        self.assertEqual(self.status(result), "pass")
        entry = self.item(result)
        self.assertFalse(entry["deliverable"])
        self.assertTrue(any("PPTX" in reason for reason in result["verdict"]["reasons"]))

    def test_render_evidence_must_match_rebuilt_pptx(self):
        item = self.deliverable()
        spec = deck_spec()
        spec["theme"]["colors"]["accent1"] = "FF0000"
        rebuilt = pb.build_deck(spec, self.deck_path)
        item["sha256"] = rebuilt["sha256"]
        self.assertNotEqual(item["sha256"], item["render"]["pptxSha256"])
        result = self.run_check(make_record(deliverables=[item]))
        self.assertIn("record.deliverable-stale", self._codes(result))
        self.assertFalse(self.item(result)["deliverable"])

    def test_visual_pass_requires_render_source_hash(self):
        for render in (None, {}, {"pptxSha256": "0" * 64}):
            with self.subTest(render=render):
                result = self.run_check(make_record(deliverables=[self.deliverable(render=render)]))
                self.assertIn("record.deliverable-stale", self._codes(result))
                self.assertFalse(self.item(result)["deliverable"])

    def test_extra_deliverable_failure_preserves_html_verdict(self):
        record = make_record()
        record["verdict"]["deliverable"] = True
        for check in record["checks"]:
            check.update(status="pass", scope="全部页", note="合成核验记录")
            if check["id"] == "visual.pages":
                check["pages"] = [{"page": n, "checked": True, "issues": []}
                                  for n in (1, 2)]
        baseline = self.run_check(record)
        self.assertTrue(baseline["verdict"]["deliverable"], baseline)
        for required in (False, True):
            with self.subTest(required=required):
                record["deliverables"] = [self.deliverable(required=required, file="missing.pptx")]
                result = self.run_check(record)
                self.assertEqual(self.status(result), "fail")
                self.assertTrue(result["verdict"]["deliverable"])
                self.assertFalse(self.item(result)["deliverable"])
                has_gap = any("PPTX" in reason for reason in result["verdict"]["reasons"])
                self.assertEqual(has_gap, required)

    def test_errors_are_scoped_to_the_deliverable_item(self):
        invalid = self.deliverable(sha256="0" * 64)
        valid = self.deliverable()
        result = self.run_check(make_record(deliverables=[invalid, valid]))
        self.assertEqual([item["deliverable"] for item in result["deliverables"]], [False, True])

    def test_chart_only_numbers_are_extracted_from_relationships(self):
        for chart_type in ("column", "bar", "line", "pie"):
            for absolute in (False, True):
                with self.subTest(chart_type=chart_type, absolute=absolute):
                    spec = deck_spec()
                    spec["slides"][1].update(layout="titleOnly", body=[], charts=[{
                        "chart": chart_type, "name": "同比", "x": 600000, "y": 1800000,
                        "cx": 6000000, "cy": 3600000, "categories": ["本期"],
                        "series": [{"name": "增长率", "values": [12]}]}])
                    pb.build_deck(spec, self.deck_path)
                    if absolute:
                        with zipfile.ZipFile(self.deck_path) as archive:
                            parts = {name: archive.read(name) for name in archive.namelist()}
                        part = "ppt/_rels/presentation.xml.rels"
                        parts[part] = parts[part].replace(b'Target="slides/', b'Target="/ppt/slides/')
                        part = "ppt/slides/_rels/slide2.xml.rels"
                        parts[part] = parts[part].replace(b'Target="../charts/', b'Target="/ppt/charts/')
                        with zipfile.ZipFile(self.deck_path, "w") as archive:
                            for name, data in parts.items():
                                archive.writestr(name, data)
                    self.deck_sha = sha(self.deck_path.read_bytes())
                    result = self.run_check(make_record(deliverables=[self.deliverable()]))
                    self.assertEqual(self.status(result), "pass", result)
                    self.assertTrue(self.item(result)["deliverable"])
                    record = make_record(deliverables=[self.deliverable()])
                    record["findings"][0]["calculation"]["expected"] = 21
                    self.assertIn("record.deliverable-consistency", self._codes(self.run_check(record)))

    def test_negative_percent_change_matches_signed_slide_text(self):
        claim = "三季度收入同比变化 -12%"
        for rendered in (claim, claim.replace("-", "−")):
            with self.subTest(rendered=rendered):
                _, self.deck_sha = self._deck(rendered)
                item = self.deliverable()
                item["checks"]["consistency"]["findings"][0]["claim"] = claim
                record = make_record(deliverables=[item])
                record["findings"][0]["claim"] = claim
                record["findings"][0]["calculation"].update(inputs={"a": 100, "b": 88}, expected=-12)
                result = self.run_check(record)
                self.assertEqual(self.status(result), "pass", result)
                self.assertTrue(self.item(result)["deliverable"])

    def test_opposite_sign_without_calculation_is_rejected(self):
        claim = "三季度收入同比变化 -12%"
        _, self.deck_sha = self._deck(claim.replace("-", "+"))
        item = self.deliverable()
        item["checks"]["consistency"]["findings"][0]["claim"] = claim
        record = make_record(deliverables=[item])
        record["findings"][0].update(claim=claim, type="fact")
        record["findings"][0].pop("calculation")
        result = self.run_check(record)
        self.assertIn("record.deliverable-consistency", self._codes(result))
        self.assertFalse(self.item(result)["deliverable"])

    def test_unreadable_pptx_cannot_pass_with_no_findings(self):
        self.deck_path.write_bytes(b"broken pptx")
        item = self.deliverable(sha256=sha(self.deck_path.read_bytes()))
        item["render"]["pptxSha256"] = item["sha256"]
        item["checks"]["consistency"]["findings"] = []
        result = self.run_check(make_record(findings=[], deliverables=[item]))
        self.assertIn("record.deliverable-pages", self._codes(result))
        self.assertFalse(self.item(result)["deliverable"])

if __name__ == "__main__":
    unittest.main()
