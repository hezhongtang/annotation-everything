"""PPTX 文件层检查器契约测试（issue #180）：包结构与关键部件的有效性。

接缝：pptx_check.check_deck（.pptx 文件 → ae-pptx-check/1 结果）与它的 CLI。
反例由构建器产物现场变异合成，只用 Python 标准库；不承诺覆盖任意 OOXML 错误，
只覆盖本票生成范围内的最低要求。
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "skill" / "annotation-everything" / "scripts"
sys.path.insert(0, str(SCRIPTS))
os.environ.setdefault("AE_HOME", tempfile.mkdtemp(prefix="ae-test-home-"))

import pptx_check as pc  # noqa: E402
import pptx_build as pb  # noqa: E402


def deck_spec() -> dict:
    """最小成功规格（与 test_pptx_build 的合成规格同构，内联避免跨文件导入）。"""
    return {
        "schema": "ae-pptx-deck/1",
        "title": "合成演示文稿",
        "theme": {
            "colors": {"dk1": "1F2933", "lt1": "FFFFFF", "dk2": "3E4C59", "lt2": "F5F7FA",
                       "accent1": "2B6CB0", "accent2": "2C7A7B", "accent3": "B7791F",
                       "accent4": "6B46C1", "accent5": "C05621", "accent6": "975FA8",
                       "hlink": "2B6CB0", "folHlink": "805AD5"},
            "fonts": {"major": {"latin": "Synthetic Display", "ea": "合成标题字体"},
                      "minor": {"latin": "Synthetic Text", "ea": "合成正文字体"}},
        },
        "slides": [
            {"key": "cover", "layout": "title", "title": "合成封面", "subtitle": "副标题"},
            {"key": "sum", "layout": "titleAndContent", "title": "要点页",
             "body": ["第一条要点", {"text": "二级要点", "level": 1}, "第二条要点"]},
        ],
    }


def full_deck_spec() -> dict:
    """覆盖四类图表与备注的成功样例规格。"""
    spec = deck_spec()
    spec["slides"][0]["notes"] = "开场讲稿。"
    spec["slides"][1]["charts"] = [{
        "chart": "column", "name": "收入", "x": 600000, "y": 1800000,
        "cx": 6000000, "cy": 3600000, "categories": ["一季度", "二季度"],
        "series": [{"name": "收入", "values": [100, 112]}], "legend": "bottom"}]
    return spec


class PptxCheckTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="ae-pptx-check-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def _deck(self, name="deck.pptx") -> Path:
        out = self.root / name
        pb.build_deck(full_deck_spec(), out)
        return out

    def _mutated(self, mutations: dict[str, object], name="mutated.pptx") -> Path:
        """读 zip、按名替换或删除成员（None 表示删除）、重写新包。"""
        source = self._deck()
        target = self.root / name
        with zipfile.ZipFile(source) as archive:
            parts = {info.filename: archive.read(info.filename) for info in archive.infolist()}
        for member, value in mutations.items():
            if value is None:
                parts.pop(member, None)
            else:
                parts[member] = value if isinstance(value, bytes) else str(value).encode("utf-8")
        with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as archive:
            for member, data in parts.items():
                archive.writestr(member, data)
        return target

    def _codes(self, result: dict) -> list[str]:
        return [p["code"] for p in result["problems"]]

    def test_builder_output_passes_all_checks(self):
        """构建器产物通过全部文件检查，结果结构完整。"""
        deck = self._deck()
        result = pc.check_deck(deck)
        self.assertEqual(result["schema"], "ae-pptx-check/1")
        self.assertTrue(result["ok"])
        self.assertEqual(result["slides"], 2)
        self.assertEqual(result["problems"], [])
        ids = {c["id"] for c in result["checks"]}
        self.assertTrue({"package.required-parts", "package.relationships",
                         "package.content-types", "slide.layout-refs",
                         "slide.placeholders", "chart.structure",
                         "chart.theme-fill", "theme.parts",
                         "sections.coverage"} <= ids)
        self.assertTrue(all(c["status"] == "pass" for c in result["checks"]))
        # 图表与内嵌工作簿部件被识别并检查
        chart_check = next(c for c in result["checks"] if c["id"] == "chart.structure")
        self.assertIn("ppt/charts/chart1.xml", chart_check["scope"])

    def test_invalid_core_xml_returns_structured_cli_failure(self):
        for part in ("[Content_Types].xml", "_rels/.rels", "ppt/presentation.xml"):
            with self.subTest(part=part):
                deck = self._mutated({part: b"<broken>"})
                done = subprocess.run(
                    [sys.executable, str(SCRIPTS / "pptx_check.py"), str(deck), "--json"],
                    capture_output=True, text=True, timeout=60)
                self.assertEqual(done.returncode, 1, done.stderr)
                result = json.loads(done.stdout)
                self.assertFalse(result["ok"])
                self.assertEqual(result["slides"], 0)
                problem = next(p for p in result["problems"] if p["code"] == "badXml")
                self.assertEqual(problem["part"], part)
                self.assertNotIn("Traceback", done.stderr)

    def test_oversized_xml_returns_structured_cli_failure(self):
        part = "[Content_Types].xml"
        deck = self._mutated({part: b" " * (pc.MAX_XML_BYTES + 1)})
        done = subprocess.run(
            [sys.executable, str(SCRIPTS / "pptx_check.py"), str(deck), "--json"],
            capture_output=True, text=True, timeout=60)
        self.assertEqual(done.returncode, 1, done.stderr)
        result = json.loads(done.stdout)
        self.assertFalse(result["ok"])
        problem = next(p for p in result["problems"] if p["code"] == "partTooLarge")
        self.assertEqual(problem["part"], part)
        self.assertEqual(result["slides"], 0)
        self.assertNotIn("Traceback", done.stderr)

    def test_duplicate_layout_shape_ids_are_rejected(self):
        part = "ppt/slideLayouts/slideLayout1.xml"
        with zipfile.ZipFile(self._deck()) as archive:
            damaged = archive.read(part).replace(b'<p:cNvPr id="2"', b'<p:cNvPr id="1"')
        deck = self._mutated({part: damaged})
        result = pc.check_deck(deck)
        self.assertFalse(result["ok"])
        problem = next(p for p in result["problems"] if p["code"] == "shapeIdDuplicate")
        self.assertEqual(problem["part"], part)
        self.assertEqual(problem["slide"], 1)

    def test_empty_text_body_is_rejected_with_part(self):
        part = "ppt/slideMasters/slideMaster1.xml"
        with zipfile.ZipFile(self._deck()) as archive:
            damaged = archive.read(part).replace(b"<a:p/>", b"")
        result = pc.check_deck(self._mutated({part: damaged}))
        self.assertFalse(result["ok"])
        problem = next(p for p in result["problems"] if p["code"] == "textBodyEmpty")
        self.assertEqual(problem["part"], part)

    def test_invalid_notes_master_list_is_rejected(self):
        part = "ppt/presentation.xml"
        with zipfile.ZipFile(self._deck()) as archive:
            original = archive.read(part)
        for content in (b"", b'<p:notesMasterId id="2147483712" r:id="rIdNotesMaster"/>'):
            with self.subTest(content=content):
                damaged = re.sub(rb'<p:notesMasterIdLst>.*?</p:notesMasterIdLst>',
                                 b'<p:notesMasterIdLst>' + content + b'</p:notesMasterIdLst>', original)
                result = pc.check_deck(self._mutated({part: damaged}))
                self.assertIn("notesMasterListBad", self._codes(result))

    def test_shared_notes_theme_is_rejected(self):
        part = "ppt/notesMasters/_rels/notesMaster1.xml.rels"
        with zipfile.ZipFile(self._deck()) as archive:
            damaged = archive.read(part).replace(b"theme2.xml", b"theme1.xml")
        result = pc.check_deck(self._mutated({part: damaged}))
        self.assertFalse(result["ok"])
        self.assertIn("themeShared", self._codes(result))

    def test_missing_rel_target_is_reported_with_part_path(self):
        """关系目标缺失：报告检查项与部件路径。"""
        deck = self._mutated({
            "ppt/slides/_rels/slide1.xml.rels":
                '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
                '<Relationship Id="rIdLayout1" '
                'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/slideLayout" '
                'Target="../slideLayouts/slideLayoutMissing.xml"/></Relationships>'})
        result = pc.check_deck(deck)
        self.assertFalse(result["ok"])
        self.assertIn("relTargetMissing", self._codes(result))
        problem = next(p for p in result["problems"] if p["code"] == "relTargetMissing")
        self.assertTrue(problem["part"].endswith("slide1.xml.rels"))
        self.assertIn("slideLayoutMissing.xml", problem["reason"])

    def test_content_type_mismatch_is_reported(self):
        """部件内容类型与部件不符：幻灯片声明成版式类型要被抓到。"""
        deck = self._mutated({
            "[Content_Types].xml":
                self._deck_content_types().replace(
                    'PartName="/ppt/slides/slide2.xml" '
                    'ContentType="application/vnd.openxmlformats-officedocument.'
                    'presentationml.slide+xml"',
                    'PartName="/ppt/slides/slide2.xml" '
                    'ContentType="application/vnd.openxmlformats-officedocument.'
                    'presentationml.slideLayout+xml"')})
        result = pc.check_deck(deck)
        self.assertFalse(result["ok"])
        self.assertIn("contentTypeMismatch", self._codes(result))

    def _deck_content_types(self) -> str:
        with zipfile.ZipFile(self._deck("for-ct.pptx")) as archive:
            return archive.read("[Content_Types].xml").decode("utf-8")

    def test_slide_without_named_layout_is_reported(self):
        """版式缺命名（cSld@name 为空）或幻灯片缺版式引用都要失败。"""
        deck = self._mutated({
            "ppt/slideLayouts/slideLayout1.xml":
                self._part_text("ppt/slideLayouts/slideLayout1.xml").replace(
                    '<p:cSld name="标题幻灯片">', "<p:cSld>")})
        result = pc.check_deck(deck)
        self.assertIn("layoutUnnamed", self._codes(result))

        deck2 = self._mutated({
            "ppt/slides/_rels/slide1.xml.rels":
                '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"/>'})
        result2 = pc.check_deck(deck2)
        self.assertIn("slideLayoutMissing", self._codes(result2))

    def _part_text(self, name: str) -> str:
        with zipfile.ZipFile(self._deck("for-part.pptx")) as archive:
            return archive.read(name).decode("utf-8")

    def test_slide_placeholder_not_in_layout_is_reported(self):
        """幻灯片占位符在版式里不存在：可定位到幻灯片。"""
        slide = self._part_text("ppt/slides/slide1.xml").replace(
            '<p:ph type="ctrTitle"/>', '<p:ph type="ctrTitle" idx="9"/>')
        deck = self._mutated({"ppt/slides/slide1.xml": slide})
        result = pc.check_deck(deck)
        self.assertIn("placeholderUnknown", self._codes(result))
        problem = next(p for p in result["problems"] if p["code"] == "placeholderUnknown")
        self.assertEqual(problem.get("slide"), 1)

    def test_chart_structure_errors_are_reported(self):
        """图表关键结构：缺轴引用、数据缓存不齐、饼图带轴、缺可编辑数据关系。"""
        chart = self._part_text("ppt/charts/chart1.xml")
        cases = {
            # 删掉图表组引用的第一条 axId：轴引用不完整
            "chartAxisMissing": chart.replace(
                '<c:axId val="100000002"/>', "", 1),
            "chartCacheMismatch": chart.replace('<c:ptCount val="2"/>',
                                                '<c:ptCount val="1"/>'),
            "chartPieHasAxes": chart.replace('<c:barChart>', '<c:pieChart>').replace(
                '</c:barChart>', '</c:pieChart>'),
        }
        for code, mutated in cases.items():
            with self.subTest(code=code):
                deck = self._mutated({"ppt/charts/chart1.xml": mutated})
                result = pc.check_deck(deck)
                self.assertIn(code, self._codes(result))
                problem = next(p for p in result["problems"] if p["code"] == code)
                self.assertTrue(problem["part"].startswith("ppt/charts/chart"))

    def test_chart_without_editable_data_is_reported(self):
        """图表缺 externalData（内嵌工作簿）：不能宣称保留可编辑数据。"""
        deck = self._mutated({
            "ppt/charts/_rels/chart1.xml.rels":
                '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"/>',
            "ppt/charts/chart1.xml":
                self._part_text("ppt/charts/chart1.xml").replace(
                    '<c:externalData r:id="rId1" autoUpdate="0"/>', "")})
        result = pc.check_deck(deck)
        self.assertIn("chartDataMissing", self._codes(result))

    def test_chart_fixed_color_fill_is_reported(self):
        """图表系列落固定色（srgbClr）而不是引用主题：判失败。"""
        chart = self._part_text("ppt/charts/chart1.xml").replace(
            '<c:order val="0"/>',
            '<c:order val="0"/><c:spPr><a:solidFill>'
            '<a:srgbClr val="FF0000"/></a:solidFill></c:spPr>')
        deck = self._mutated({"ppt/charts/chart1.xml": chart})
        result = pc.check_deck(deck)
        self.assertIn("chartFixedColor", self._codes(result))

    def test_chart_content_type_mismatch_is_reported(self):
        """图表部件内容类型写错：按部件家族规则抓到。"""
        types = self._deck_content_types().replace(
            'PartName="/ppt/charts/chart1.xml" '
            'ContentType="application/vnd.openxmlformats-officedocument.drawingml.chart+xml"',
            'PartName="/ppt/charts/chart1.xml" '
            'ContentType="application/vnd.openxmlformats-officedocument.presentationml.slide+xml"')
        deck = self._mutated({"[Content_Types].xml": types})
        result = pc.check_deck(deck)
        self.assertIn("contentTypeMismatch", self._codes(result))

    def test_slide_missing_from_sections_is_reported(self):
        """章节漏页：可定位漏掉的幻灯片。"""
        presentation = self._part_text("ppt/presentation.xml")
        deck = self._mutated({"ppt/presentation.xml": presentation.replace(
            '<p14:sldId id="', '<p14:sldId id="X', 1)})
        result = pc.check_deck(deck)
        self.assertIn("sectionSlideMissing", self._codes(result))

    def test_corrupt_file_fails_cleanly(self):
        """坏文件（不是 zip / 缺核心部件）给出可读失败而不是异常。"""
        garbage = self.root / "garbage.pptx"
        garbage.write_bytes(b"not a zip at all")
        result = pc.check_deck(garbage)
        self.assertFalse(result["ok"])
        self.assertIn("notZip", self._codes(result))

        empty = self.root / "empty.pptx"
        with zipfile.ZipFile(empty, "w") as archive:
            archive.writestr("README.txt", "nothing here")
        result2 = pc.check_deck(empty)
        self.assertFalse(result2["ok"])
        self.assertIn("partMissing", self._codes(result2))

    def test_cli_contract(self):
        """CLI：成功退出 0；坏文件退出 1；--json 输出结构化结果。"""
        deck = self._deck("cli.pptx")
        script = SCRIPTS / "pptx_check.py"
        done = subprocess.run([sys.executable, str(script), str(deck), "--json"],
                              capture_output=True, text=True, timeout=60)
        self.assertEqual(done.returncode, 0, done.stderr)
        payload = json.loads(done.stdout)
        self.assertEqual(payload["schema"], "ae-pptx-check/1")
        self.assertTrue(payload["ok"])

        garbage = self.root / "bad.pptx"
        garbage.write_bytes(b"junk")
        done2 = subprocess.run([sys.executable, str(script), str(garbage)],
                               capture_output=True, text=True, timeout=60)
        self.assertEqual(done2.returncode, 1)


if __name__ == "__main__":
    unittest.main()
