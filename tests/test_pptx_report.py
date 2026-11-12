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


class CompatibilityTests(ControlledRenderMixin, unittest.TestCase):
    """格式版本契约与服务能力：旧版本机制面对 pptx 明确拒绝，本版本声明能力。"""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="ae-pptx-compat-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def build_one(self, out: Path) -> Path:
        self.controlled(slides=2)
        code = 0
        o, e = io.StringIO(), io.StringIO()
        with redirect_stdout(o), redirect_stderr(e):
            try:
                code = build_viewer.main(["--report", str(mp.build_sample(self.root / "deck.pptx")),
                                          "--out", str(out), "--no-scan-skills", "--locale", "zh-CN"])
            except SystemExit as exc:
                code = exc.code if isinstance(exc.code, int) else 1
        assert code == 0, e.getvalue()
        return out / "review.json"

    def test_ledger_accepts_pptx_v1_and_refuses_future_versions(self):
        manifest_path = self.build_one(self.root / "rv")
        m = json.loads(manifest_path.read_text(encoding="utf-8"))
        self.assertIsNone(ledger.unsupported_format(m), "本版本认得 pptx v1")
        m["format"]["version"] = pp.FORMAT_VERSION + 1
        manifest_path.write_text(json.dumps(m, ensure_ascii=False), encoding="utf-8")
        self.assertIn("不支持", ledger.unsupported_format(m), "更高版本元数据明确拒绝")

        old_style = dict(m)
        old_style.pop("format")
        old_style.pop("pptx")
        self.assertIsNone(ledger.unsupported_format(old_style), "旧 HTML 审阅继续按无 format 处理")

    def test_legacy_manifest_without_pptx_support_is_refused_not_read_as_html(self):
        # 旧版本的 REPORT_FORMATS 没有 pptx：模拟旧代码视角，未知格式必须明确拒绝
        with mock.patch.object(ledger, "REPORT_FORMATS", {"html": 0, "docx": 1}):
            problem = ledger.unsupported_format({"format": {"report": "pptx", "version": 1}})
        self.assertIsNotNone(problem)
        self.assertIn("pptx", problem)

    def test_service_declares_pptx_feature(self):
        import serve
        self.assertIn("pptx", serve.FEATURES)

    def test_page_embeds_pptx_summary_and_old_service_block(self):
        self.build_one(self.root / "rv")
        page = (self.root / "rv" / "index.html").read_text(encoding="utf-8")
        self.assertIn('const PPTX={"v": 1,', page)
        self.assertIn("viewer.pptx.oldService", page, "页面带旧服务阻断提示的词典 key")
        self.assertIn("pptxBlocked", page, "旧服务写入阻断逻辑进入页面")


class RenderPipelineTests(unittest.TestCase):
    """真实工具管线的错误路径：伪造 soffice / pdftoppm 脚本注入 PATH（先例 test_pptx_fonts）。

    不依赖本机是否装了 LibreOffice；成功路径的集成验证留待 PR review 与 #153 的真实渲染。
    """

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="ae-pptx-render-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.bin = self.root / "bin"
        self.bin.mkdir()
        self.pptx = mp.build_sample(self.root / "deck.pptx")
        self.workdir = self.root / "work"
        self.workdir.mkdir()

    def tool(self, name: str, script: str):
        path = self.bin / name
        path.write_text("#!/bin/sh\n" + script + "\n", encoding="utf-8")
        path.chmod(0o755)

    def isolated(self):
        """PATH 只保留伪造 bin 与系统 defpath（cp 等可用）；LibreOffice 的候选安装路径一并屏蔽。"""
        patcher = mock.patch.dict(os.environ, {"PATH": str(self.bin) + os.pathsep + os.defpath})
        candidates = mock.patch.object(pp, "SOFFICE_CANDIDATES", ())
        patcher.start()
        candidates.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(candidates.stop)

    def fake_soffice(self):
        """伪造 soffice：按 --outdir 参数（第 6 个）把合成 PDF 复制到输出目录。"""
        self.tool("soffice", 'cp "%s" "$6/deck.pdf"' % self.fake_pdf())

    def fake_pdf(self, pages: int = 2) -> Path:
        objects = [b"<< /Type /Catalog /Pages 2 0 R >>", b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
                   b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 960 540] >>"]
        raw = bytearray(b"%PDF-1.4\n")
        for number, obj in enumerate(objects, 1):
            raw.extend(f"{number} 0 obj\n".encode() + obj + b"\nendobj\n")
        raw.extend(b"trailer\n<< /Size 4 /Root 1 0 R >>\n%%EOF\n")
        pdf = self.root / f"synthetic-{pages}.pdf"
        pdf.write_bytes(bytes(raw))
        return pdf

    def test_missing_tools_have_actionable_codes(self):
        self.isolated()
        for code in ("noRenderer", "noRasterizer"):
            with self.subTest(code=code):
                if code == "noRasterizer":
                    self.fake_soffice()
                with self.assertRaises(pp.PptxReportError) as caught:
                    pp.render_slides(self.pptx, self.workdir)
                self.assertEqual(caught.exception.code, code)

    def test_conversion_failure_and_empty_output_are_distinct(self):
        self.isolated()
        self.tool("soffice", "echo boom >&2; exit 1")
        with self.assertRaises(pp.PptxReportError) as caught:
            pp.render_slides(self.pptx, self.workdir)
        self.assertEqual(caught.exception.code, "convertFailed")
        self.assertIn("boom", caught.exception.detail)

        self.tool("soffice", "exit 0")   # 成功但不产出 PDF：同样按转换失败报
        with self.assertRaises(pp.PptxReportError) as caught:
            pp.render_slides(self.pptx, self.workdir)
        self.assertEqual(caught.exception.code, "convertFailed")

        self.fake_soffice()
        self.tool("pdftoppm", "exit 0")
        with self.assertRaises(pp.PptxReportError) as caught:
            pp.render_slides(self.pptx, self.workdir)
        self.assertEqual(caught.exception.code, "emptyOutput", "pdftoppm 没产出页面图像时报空输出")

    def test_raster_failure_reports_tool_stderr(self):
        self.isolated()
        self.fake_soffice()
        self.tool("pdftoppm", "echo raster-boom >&2; exit 3")
        with self.assertRaises(pp.PptxReportError) as caught:
            pp.render_slides(self.pptx, self.workdir)
        self.assertEqual(caught.exception.code, "rasterFailed")
        self.assertIn("raster-boom", caught.exception.detail)

    def test_timeout_is_reported_not_hung(self):
        self.isolated()
        self.tool("soffice", "sleep 5")
        with mock.patch.object(pp, "SOFFICE_TIMEOUT", 0.3):
            with self.assertRaises(pp.PptxReportError) as caught:
                pp.render_slides(self.pptx, self.workdir)
        self.assertEqual(caught.exception.code, "timeout")

    def test_pipeline_returns_rasterized_pages_in_order(self):
        self.isolated()
        self.fake_soffice()
        self.tool("pdftoppm", 'out=""; n=1; for a in "$@"; do case "$a" in -*) ;; *) out="$a";; esac; done; '
                              'prefix="${out%.pdf}"; while [ $n -le 2 ]; do printf PDFPAGE > "$prefix-$n.png"; n=$((n+1)); done')
        images = pp.render_slides(self.pptx, self.workdir)
        self.assertEqual(images, [b"PDFPAGE", b"PDFPAGE"])

    def test_page_count_mismatch_stops_the_build(self):
        images = [PNG_1PX]   # 只有 1 张渲染图，源里有 2 张幻灯片
        with mock.patch.object(pp, "render_slides", lambda p, w: images), \
                mock.patch.object(pp, "font_evidence", lambda p, d: None):
            with self.assertRaises(pp.PptxReportError) as caught:
                pp.parts(self.pptx)
        self.assertEqual(caught.exception.code, "pageCount")


class FontEvidenceTests(unittest.TestCase):
    """转换前声明字体与转换后 PDF 字体证据：受控 fc-list / pdffonts 输出（先例 test_pptx_fonts）。"""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="ae-pptx-fonts-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.bin = self.root / "bin"
        self.bin.mkdir()
        self.pptx = mp.build_sample(self.root / "deck.pptx")
        self.pdf = self.root / "rendered.pdf"
        self.pdf.write_bytes(b"%PDF synthetic")
        patcher = mock.patch.dict(os.environ, {"PATH": str(self.bin) + os.pathsep + os.defpath})
        patcher.start()
        self.addCleanup(patcher.stop)

    def tool(self, name: str, script: str):
        path = self.bin / name
        path.write_text("#!/bin/sh\n" + script + "\n", encoding="utf-8")
        path.chmod(0o755)

    def evidence(self, pdf: Path | None | object = ...) -> dict:
        # Ellipsis 表示默认用渲染 PDF；显式传 None 才是不做转换后对照
        return pp.font_evidence(self.pptx, self.pdf if pdf is ... else pdf)

    def explicit_font_deck(self, name: str = "explicit.pptx") -> Path:
        """单页 deck：全部 run 显式声明同一字体，unresolved 与主题备选为空。"""
        run = ('<a:r><a:rPr lang="en-US"><a:latin typeface="Synthetic Sans"/></a:rPr>'
               '<a:t>explicit text</a:t></a:r>')
        parts = {
            "[Content_Types].xml": '<?xml version="1.0" encoding="UTF-8"?>\n'
                f'<Types xmlns="{mp.CT}"><Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
                '<Default Extension="xml" ContentType="application/xml"/>'
                '<Override PartName="/ppt/presentation.xml" ContentType="application/vnd.openxmlformats-officedocument.presentationml.presentation.main+xml"/>'
                '<Override PartName="/ppt/slides/slide1.xml" ContentType="application/vnd.openxmlformats-officedocument.presentationml.slide+xml"/></Types>',
            "_rels/.rels": '<?xml version="1.0" encoding="UTF-8"?>\n'
                f'<Relationships xmlns="{mp.PKG_REL}"><Relationship Id="rIdDeck" Type="{mp.R}/officeDocument" Target="ppt/presentation.xml"/></Relationships>',
            "ppt/presentation.xml": '<?xml version="1.0" encoding="UTF-8"?>\n'
                f'<p:presentation xmlns:p="{mp.P}" xmlns:r="{mp.R}"><p:sldIdLst><p:sldId id="257" r:id="rIdFirst"/></p:sldIdLst>'
                '<p:sldSz cx="12192000" cy="6858000"/></p:presentation>',
            "ppt/_rels/presentation.xml.rels": '<?xml version="1.0" encoding="UTF-8"?>\n'
                f'<Relationships xmlns="{mp.PKG_REL}"><Relationship Id="rIdFirst" Type="{mp.R}/slide" Target="slides/slide1.xml"/></Relationships>',
            "ppt/slides/slide1.xml": '<?xml version="1.0" encoding="UTF-8"?>\n'
                f'<p:sld xmlns:p="{mp.P}" xmlns:a="{mp.A}" xmlns:r="{mp.R}"><p:cSld><p:spTree>'
                '<p:nvGrpSpPr><p:cNvPr id="1" name=""/><p:cNvGrpSpPr/><p:nvPr/></p:nvGrpSpPr><p:grpSpPr/>'
                '<p:sp><p:nvSpPr><p:cNvPr id="2" name="显式字体页"/><p:cNvSpPr txBox="1"/><p:nvPr/></p:nvSpPr>'
                '<p:spPr><a:xfrm><a:off x="914400" y="914400"/><a:ext cx="5486400" cy="914400"/></a:xfrm></p:spPr>'
                f'<p:txBody><a:bodyPr/><a:lstStyle/><a:p>{run}</a:p></p:txBody></p:sp>'
                '</p:spTree></p:cSld></p:sld>',
        }
        return mp.write_package(self.root / name, parts)

    def test_missing_and_found_declared_fonts_are_recorded(self):
        self.tool("fc-list", "printf '%s\\n' '完全无关的字体'")
        evidence = self.evidence(pdf=None)
        self.assertEqual([d["font"] for d in evidence["declared"]], ["Synthetic Sans"])
        self.assertEqual(evidence["missing"], ["Synthetic Sans"], "声明字体未检出按缺失记录")
        self.assertEqual(evidence["installed"], "ok")
        self.assertTrue(evidence["review"], "有缺失就要人工核查")

        self.tool("fc-list", "printf '%s\\n' 'Synthetic Sans, Synthetic Sans Regular'")
        self.pptx = self.explicit_font_deck()
        evidence = self.evidence(pdf=None)
        self.assertEqual(evidence["missing"], [])
        self.assertFalse(evidence["review"], "声明齐全、全部显式且枚举正常时清单层面不触发核查")

    def test_fc_list_failure_keeps_declarations_and_marks_unknown(self):
        self.tool("fc-list", "exit 7")
        evidence = self.evidence(pdf=None)
        self.assertEqual(evidence["installed"], "failed")
        self.assertEqual([d["font"] for d in evidence["declared"]], ["Synthetic Sans"],
                         "枚举失败时声明清单仍然可见")
        self.assertEqual(evidence["missing"], [], "枚举失败不误报缺失：是无法确认，不是缺失")
        self.assertTrue(evidence["warning"])

    def test_unresolved_theme_references_stay_unknown(self):
        parts = mp.sample_parts()
        parts["ppt/slides/slide10.xml"] = parts["ppt/slides/slide10.xml"].replace(
            '<a:rPr b="1"><a:latin typeface="Synthetic Sans"/></a:rPr>',
            '<a:rPr b="1"><a:latin typeface="+mn-lt"/></a:rPr>')
        self.pptx = mp.write_package(self.root / "theme.pptx", parts)
        self.tool("fc-list", "printf '%s\\n' 'Synthetic Sans'")
        evidence = self.evidence(pdf=None)
        self.assertTrue(evidence["unresolved"], "主题引用无法解析：按未知记录")
        self.assertTrue(evidence["review"])

    def test_pdf_evidence_with_unembedded_font_requires_review(self):
        self.tool("fc-list", "printf '%s\\n' 'Synthetic Sans'")
        self.tool("pdffonts", "printf '%s\\n' "
                  "'name                                 type              encoding         emb sub uni object ID' "
                  "'------------------------------------ ----------------- ---------------- --- --- --- ---------' "
                  "'Synthetic Sans                       TrueType          WinAnsi          no  no  no       5  0'")
        evidence = self.evidence()
        self.assertEqual(evidence["pdf"]["rows"][0]["name"], "Synthetic Sans")
        self.assertEqual(evidence["pdf"]["rows"][0]["emb"], "no")
        self.assertTrue(evidence["review"], "渲染 PDF 里字体未嵌入：需要人工核查")

    def test_matching_names_never_mark_visual_verified(self):
        self.tool("fc-list", "printf '%s\\n' 'Synthetic Sans'")
        self.tool("pdffonts", "printf '%s\\n' "
                  "'name                                 type              encoding         emb sub uni object ID' "
                  "'------------------------------------ ----------------- ---------------- --- --- --- ---------' "
                  "'Synthetic Sans                       TrueType          WinAnsi          yes yes yes      5  0'")
        self.pptx = self.explicit_font_deck()
        evidence = self.evidence()
        self.assertFalse(evidence["review"], "名称一致且嵌入：字体清单层面不触发核查")
        with mock.patch.object(pp, "render_slides", lambda p, w: [PNG_1PX]), \
                mock.patch.object(pp, "font_evidence", lambda p, d: evidence):
            result = pp.parts(self.pptx)
        self.assertIn("visual", result["meta"]["unverified"],
                      "字体名称一致也不自动标记视觉核验通过")


if __name__ == "__main__":
    unittest.main()
