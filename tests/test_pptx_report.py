"""PPTX 直接构建（#148）：渲染管线、对照页 parts、清单与格式路由。

fixture 由 fixtures/pptx/make_pptx.py 现场合成；渲染与字体工具输出全部受控注入
（伪造图像字节 / 假工具脚本），不依赖本机 LibreOffice、Poppler 或 fontconfig。
真实工具链的集成验证按 Testing Decision 6 留待 PR review 与 #153。
只用标准库：python3 -m unittest tests.test_pptx_report
"""
from __future__ import annotations

import io
import json
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "skill" / "annotation-everything" / "scripts"
sys.path.insert(0, str(SCRIPTS))
sys.path.insert(0, str(ROOT / "fixtures" / "pptx"))
os.environ.setdefault("AE_HOME", tempfile.mkdtemp(prefix="ae-test-home-"))
os.environ.setdefault("HOME", tempfile.mkdtemp(prefix="ae-test-user-"))

import build_viewer  # noqa: E402
import ledger  # noqa: E402
import make_pptx as mp  # noqa: E402
import pptx_reader as pr  # noqa: E402
import pptx_report as pp  # noqa: E402

PNG_1PX = (b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
           b"\x08\x06\x00\x00\x00\x1f\x15\xc4\x89\x00\x00\x00\nIDATx\x9cc\x00\x01"
           b"\x00\x00\x05\x00\x01\r\n-\xb4\x00\x00\x00\x00IEND\xaeB`\x82")


def tmp() -> Path:
    return Path(tempfile.mkdtemp(prefix="ae-pptx-report-"))


class ControlledRenderMixin:
    """受控渲染接缝：替换 pptx_report 的外部工具调用，注入合成 PNG 与字体证据。"""

    def controlled(self, slides: int = 2, fonts: dict | None = None) -> mock._patch:
        images = [PNG_1PX] * slides
        renderer = mock.patch.object(pp, "render_slides", lambda pptx, workdir: images)
        evidence = mock.patch.object(pp, "font_evidence", lambda pptx, pdf: fonts or {
            "declared": [], "missing": [], "unresolved": [], "fallbacks": [],
            "installed": "ok", "pdf": None, "review": False, "warning": False})
        renderer.start()
        evidence.start()
        self.addCleanup(renderer.stop)
        self.addCleanup(evidence.stop)
        return renderer


class PartsTests(ControlledRenderMixin, unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="ae-pptx-parts-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def test_landscape_pages_follow_slide_order_size_and_orientation(self):
        self.controlled(slides=2)
        result = pp.parts(mp.build_sample(self.root / "sample.pptx"))

        self.assertEqual(result["title"], "合成标题")
        self.assertEqual([(s["n"], s["key"]) for s in result["pages"]], [(1, "s260"), (2, "s257")],
                         "页面顺序跟随演示文稿的 sldIdLst，页面键用持久幻灯片标识")
        first = result["pages"][0]
        self.assertEqual((first["w"], first["h"], first["orient"]), (1280.0, 720.0, "landscape"),
                         "EMU 画布按 96dpi 折算成舞台像素")
        self.assertTrue(first["stable"])
        for s in result["pages"]:
            self.assertIn('class="page pptx-page"', s["html"])
            self.assertIn('src="data:image/png;base64,', s["html"], "每页以渲染图负责视觉")

    def test_portrait_canvas_keeps_original_orientation(self):
        self.controlled(slides=2)
        result = pp.parts(mp.build_sample(self.root / "portrait.pptx", portrait=True))
        self.assertEqual((result["pages"][0]["w"], result["pages"][0]["h"], result["pages"][0]["orient"]),
                         (720.0, 1280.0, "portrait"))

    def test_meta_records_identity_renderer_and_unverified_items(self):
        self.controlled(slides=2)
        path = mp.build_sample(self.root / "sample.pptx")
        result = pp.parts(path)

        meta = result["meta"]
        self.assertEqual(meta["fingerprint"], pr.read(path)["fingerprint"])
        self.assertEqual(meta["renderer"]["tool"], "libreoffice+poppler")
        self.assertEqual(meta["renderer"]["pages"], 2)
        self.assertEqual(meta["renderer"]["status"], "ok")
        self.assertIn("visual", meta["unverified"], "渲染成功也不自动标记视觉核验通过")
        self.assertEqual([s["key"] for s in meta["slides"]], ["s260", "s257"])
        self.assertTrue(meta["slides"][0]["notes"], "演讲者备注的存在性进入清单")

    def test_element_models_are_embedded_for_future_interaction(self):
        self.controlled(slides=2)
        result = pp.parts(mp.build_sample(self.root / "sample.pptx"))

        slide = result["meta"]["slides"][0]
        element = slide["elements"][0]
        self.assertEqual(element["key"], "e2")
        self.assertIn("capabilities", element, "元素带回写能力声明：渲染图不能成为回写依据")
        self.assertIn("geometry", element)
        self.assertIn(element["kind"], ("shape", "table", "picture", "group", "chart"))
        # 渲染图只是显示产物：页面 html 只含图像与幻灯片键，不携带源文件回写路径
        self.assertNotIn("writeback", result["pages"][0]["html"])
        self.assertNotIn(str(self.root), result["pages"][0]["html"])

    def test_reader_errors_surface_as_pptx_report_errors(self):
        self.controlled(slides=2)
        junk = self.root / "junk.pptx"
        junk.write_bytes(b"not a zip")
        with self.assertRaises(pp.PptxReportError) as caught:
            pp.parts(junk)
        self.assertEqual(caught.exception.code, "notZip")


class BuildIntegrationTests(ControlledRenderMixin, unittest.TestCase):
    """公开构建接缝：build_viewer.main 的退出码、诊断与产物（先例 test_docx_review）。"""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="ae-pptx-build-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def build(self, report: Path, out: Path, *args: str) -> tuple[int, str, str]:
        o, e = io.StringIO(), io.StringIO()
        with redirect_stdout(o), redirect_stderr(e):
            try:
                code = build_viewer.main(["--report", str(report), "--out", str(out),
                                          "--no-scan-skills", "--locale", "zh-CN", *args])
            except SystemExit as exc:
                code = exc.code if isinstance(exc.code, int) else 1
                e.write(str(exc.code))
        return code, o.getvalue(), e.getvalue()

    def test_pptx_builds_review_page_directly_case_insensitive(self):
        self.controlled(slides=2)
        for name in ("deck.pptx", "DECK.PPTX", "mixed.Pptx"):
            with self.subTest(name=name):
                out = self.root / ("rv-" + name)
                code, stdout, stderr = self.build(mp.build_sample(self.root / name), out)
                self.assertEqual(code, 0, stderr)
                self.assertIn("index.html", stdout)

    def test_manifest_and_page_data(self):
        self.controlled(slides=2)
        out = self.root / "rv"
        code, _o, stderr = self.build(mp.build_sample(self.root / "deck.pptx"), out)
        self.assertEqual(code, 0, stderr)
        m = json.loads((out / "review.json").read_text(encoding="utf-8"))
        self.assertEqual(m["format"], {"report": "pptx", "version": pp.FORMAT_VERSION})
        self.assertEqual([(p["n"], p["key"], p["orient"]) for p in m["pages"]],
                         [(1, "s260", "landscape"), (2, "s257", "landscape")])
        self.assertEqual(m["pptx"]["fingerprint"],
                         pr.read(self.root / "deck.pptx")["fingerprint"])
        self.assertIn("visual", m["pptx"]["unverified"])
        page = (out / "index.html").read_text(encoding="utf-8")
        self.assertIn('class=\\"page pptx-page\\"', page)
        self.assertNotIn("__PPTX__", page)

    def test_ppt_gets_conversion_guidance_not_html_reading(self):
        legacy = self.root / "old.ppt"
        legacy.write_bytes(b"\xd0\xcf\x11\xe0legacy binary")
        code, _o, stderr = self.build(legacy, self.root / "rv")
        self.assertNotEqual(code, 0)
        self.assertIn("转换为 .pptx", stderr)
        self.assertNotIn("UnicodeDecodeError", stderr, "不落入 UTF-8/HTML 分支")

    def test_html_report_still_builds_without_pptx_metadata(self):
        out = self.root / "rv"
        code, _o, stderr = self.build(ROOT / "fixtures" / "sample" / "report.html", out)
        self.assertEqual(code, 0, stderr)
        m = json.loads((out / "review.json").read_text(encoding="utf-8"))
        self.assertNotIn("format", m, "HTML 审阅保持没有 format 字段")

    def test_missing_renderer_is_an_executable_failure(self):
        with mock.patch.object(pp, "find_soffice", lambda: None), \
                mock.patch.object(pp, "font_evidence", lambda p, d: None):
            code, _o, stderr = self.build(mp.build_sample(self.root / "deck.pptx"), self.root / "rv")
        self.assertNotEqual(code, 0)
        self.assertIn("LibreOffice", stderr, "工具缺失给出安装指引")
        self.assertNotIn("Traceback", stderr)


if __name__ == "__main__":
    unittest.main()
