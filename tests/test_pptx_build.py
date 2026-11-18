"""内置 PPTX 制作器契约测试（issue #180）：deck 规格 → 可编辑演示文稿。

接缝：pptx_build.build_deck（deck 规格 dict → .pptx 文件与构建摘要）与它的
CLI 包装。全部材料由标准库现场合成，只用 Python 标准库。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from xml.etree import ElementTree as ET

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "skill" / "annotation-everything" / "scripts"
sys.path.insert(0, str(SCRIPTS))
os.environ.setdefault("AE_HOME", tempfile.mkdtemp(prefix="ae-test-home-"))

import zipfile

import pptx_reader as pr  # noqa: E402
import pptx_build as pb  # noqa: E402

# 1×1 合成 PNG（签名 + IHDR 宽高），只用于母版标志的尺寸探测。
PNG_1X1 = b"\x89PNG\r\n\x1a\n" + b"\x00\x00\x00\x0dIHDR" + (4).to_bytes(4, "big") + (4).to_bytes(4, "big")


def deck_spec() -> dict:
    """合成最小 deck 规格：封面 + 内容页，主题色与字体齐全。"""
    return {
        "schema": "ae-pptx-deck/1",
        "title": "合成演示文稿",
        "theme": {
            "colors": {
                "dk1": "1F2933",
                "lt1": "FFFFFF",
                "dk2": "3E4C59",
                "lt2": "F5F7FA",
                "accent1": "2B6CB0",
                "accent2": "2C7A7B",
                "accent3": "B7791F",
                "accent4": "6B46C1",
                "accent5": "C05621",
                "accent6": "975FA8",
                "hlink": "2B6CB0",
                "folHlink": "805AD5",
            },
            "fonts": {
                "major": {"latin": "Synthetic Display", "ea": "合成标题字体"},
                "minor": {"latin": "Synthetic Text", "ea": "合成正文字体"},
            },
        },
        "slides": [
            {"key": "cover", "layout": "title", "title": "合成封面", "subtitle": "副标题"},
            {
                "key": "sum",
                "layout": "titleAndContent",
                "title": "要点页",
                "body": ["第一条要点", {"text": "二级要点", "level": 1}, "第二条要点"],
            },
        ],
    }


class PptxBuildTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="ae-pptx-build-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def _build(self, spec=None, name="deck.pptx") -> tuple[Path, dict]:
        out = self.root / name
        summary = pb.build_deck(spec if spec is not None else deck_spec(), out)
        return out, summary

    def _part(self, out: Path, name: str) -> str:
        with zipfile.ZipFile(out) as archive:
            return archive.read(name).decode("utf-8")

    def test_minimal_deck_builds_readable_package(self):
        """最小规格构建成功，产物能被审阅读取链路原样回读。"""
        out = self.root / "deck.pptx"
        summary = pb.build_deck(deck_spec(), out)

        self.assertEqual(summary["schema"], "ae-pptx-build/1")
        self.assertEqual(summary["pages"], 2)
        self.assertEqual(summary["title"], "合成演示文稿")
        self.assertTrue(out.exists())
        model = pr.read(out)
        self.assertEqual([s["n"] for s in model["slides"]], [1, 2])
        self.assertEqual(
            model["canvas"], {"width": 12192000, "height": 6858000, "unit": "emu", "orientation": "landscape"}
        )

    def test_theme_carries_confirmed_colors_and_fonts(self):
        """已确认的主题色与标题/正文字体写进主题部件，受控元素实际引用主题。"""
        out, _ = self._build()
        theme = self._part(out, "ppt/theme/theme1.xml")

        self.assertIn('<a:accent1><a:srgbClr val="2B6CB0"/></a:accent1>', theme)
        self.assertIn('<a:dk1><a:srgbClr val="1F2933"/></a:dk1>', theme)
        self.assertIn('typeface="合成标题字体"', theme)
        self.assertIn('typeface="合成正文字体"', theme)
        self.assertIn('<a:latin typeface="Synthetic Display"/>', theme)
        self.assertIn('<a:latin typeface="Synthetic Text"/>', theme)
        # 受控元素不落固定色：幻灯片正文 XML 里不出现 srgbClr
        for index in (1, 2):
            slide = self._part(out, f"ppt/slides/slide{index}.xml")
            self.assertNotIn("srgbClr", slide)
        # 母版文本样式引用主题字体槽位
        master = self._part(out, "ppt/slideMasters/slideMaster1.xml")
        self.assertIn('typeface="+mj-lt"', master)
        self.assertIn('typeface="+mn-lt"', master)

    def test_named_layouts_and_placeholder_usage(self):
        """使用中的版式有命名与类型，幻灯片占位符实际对应版式占位符。"""
        out, _ = self._build()
        layout1 = self._part(out, "ppt/slideLayouts/slideLayout1.xml")
        layout2 = self._part(out, "ppt/slideLayouts/slideLayout2.xml")

        self.assertIn('name="标题幻灯片"', layout1)
        self.assertIn('type="title"', layout1)
        self.assertIn('<p:ph type="ctrTitle"/>', layout1)
        self.assertIn('name="标题和内容"', layout2)
        self.assertIn('type="obj"', layout2)
        self.assertIn('<p:ph type="title"/>', layout2)
        self.assertIn('<p:ph type="body" idx="1"/>', layout2)
        slide2_rels = self._part(out, "ppt/slides/_rels/slide2.xml.rels")
        self.assertIn('Target="../slideLayouts/slideLayout2.xml"', slide2_rels)
        slide2 = self._part(out, "ppt/slides/slide2.xml")
        self.assertIn('<p:ph type="title"/>', slide2)
        self.assertIn('<p:ph type="body" idx="1"/>', slide2)

    def test_theme_swap_only_touches_theme_part(self):
        """更换主题样例：受控引用不变，主题部件随之变化（验收用的对照构造）。"""
        spec_b = deck_spec()
        spec_b["theme"]["colors"]["accent1"] = "C53030"
        spec_b["theme"]["fonts"]["minor"]["ea"] = "另一正文字体"
        out_a, _ = self._build(name="a.pptx")
        out_b, _ = self._build(spec_b, name="b.pptx")
        theme_a = self._part(out_a, "ppt/theme/theme1.xml")
        theme_b = self._part(out_b, "ppt/theme/theme1.xml")

        self.assertNotEqual(theme_a, theme_b)
        self.assertIn("C53030", theme_b)
        # 幻灯片 XML 只引用主题槽位，与主题取值无关，换主题前后应逐字节相同
        self.assertEqual(self._part(out_a, "ppt/slides/slide2.xml"), self._part(out_b, "ppt/slides/slide2.xml"))

    def test_master_carries_footer_number_and_logo(self):
        """页脚、页码与标志按规格落在母版，不逐页复制；内容类型登记图片格式。"""
        logo_path = self.root / "logo.png"
        logo_path.write_bytes(PNG_1X1)
        spec = deck_spec()
        spec["master"] = {
            "footer": True,
            "footerText": "内部资料",
            "pageNumber": True,
            "logo": {"path": str(logo_path), "name": "合成标志"},
        }
        out, _ = self._build(spec)
        master = self._part(out, "ppt/slideMasters/slideMaster1.xml")

        self.assertIn('<p:ph type="ftr"/>', master)
        self.assertIn('<p:ph type="sldNum"/>', master)
        self.assertIn('type="slidenum"', master)
        self.assertIn("<p:hf ftr=", master)
        self.assertIn("内部资料", master)
        self.assertIn('r:embed="rIdLogo"', master)
        master_rels = self._part(out, "ppt/slideMasters/_rels/slideMaster1.xml.rels")
        self.assertIn('Target="../media/logo.png"', master_rels)
        with zipfile.ZipFile(out) as archive:
            self.assertIn("ppt/media/logo.png", archive.namelist())
            types = archive.read("[Content_Types].xml").decode("utf-8")
        self.assertIn('Extension="png"', types)

    def test_cover_slide_can_hide_master_shapes(self):
        """封面等页面按约定隐藏母版公共元素，其余页面保留。"""
        spec = deck_spec()
        spec["master"] = {"footer": True, "pageNumber": True, "logo": None}
        spec["slides"][0]["hideMasterShapes"] = True
        out, _ = self._build(spec)

        self.assertIn('showMasterSp="0"', self._part(out, "ppt/slides/slide1.xml"))
        self.assertNotIn("showMasterSp", self._part(out, "ppt/slides/slide2.xml"))

    def test_layout_shape_ids_are_unique_with_footer_and_page_numbers(self):
        for footer, page_number in ((True, False), (False, True), (True, True)):
            with self.subTest(footer=footer, page_number=page_number):
                spec = deck_spec()
                spec["master"] = {"footer": footer, "pageNumber": page_number}
                spec["slides"].extend(
                    {"key": layout, "layout": layout, "title": "合成页"} for layout in ("sectionHeader", "titleOnly")
                )
                spec["slides"].append({"key": "blank", "layout": "blank"})
                out, _ = self._build(spec)
                with zipfile.ZipFile(out) as archive:
                    layouts = [
                        name
                        for name in archive.namelist()
                        if name.startswith("ppt/slideLayouts/") and name.endswith(".xml")
                    ]
                    self.assertEqual(len(layouts), 5)
                    for name in layouts:
                        root = ET.fromstring(archive.read(name))
                        ids = [node.get("id") for node in root.iter(f"{{{pb.P}}}cNvPr")]
                        self.assertEqual(len(ids), len(set(ids)), name)

    def test_empty_text_bodies_still_have_a_paragraph(self):
        spec = deck_spec()
        spec["slides"][0]["notes"] = "合成讲稿"
        spec["slides"][1]["shapes"] = [
            {"shape": "rect", "x": 600000, "y": 1800000, "cx": 1000000, "cy": 1000000, "fill": "accent1"}
        ]
        out, _ = self._build(spec)
        with zipfile.ZipFile(out) as archive:
            for name in archive.namelist():
                if not name.endswith(".xml"):
                    continue
                root = ET.fromstring(archive.read(name))
                for body in root.iter(f"{{{pb.P}}}txBody"):
                    self.assertIsNotNone(body.find(f"{{{pb.A}}}p"), name)

    def test_notes_master_list_is_present_only_with_notes(self):
        for notes in (False, True):
            with self.subTest(notes=notes):
                spec = deck_spec()
                if notes:
                    spec["slides"][0]["notes"] = "合成讲稿"
                out, _ = self._build(spec)
                root = ET.fromstring(self._part(out, "ppt/presentation.xml"))
                entries = root.find(f"{{{pb.P}}}notesMasterIdLst")
                if notes:
                    self.assertEqual(len(entries), 1)
                    self.assertEqual(set(entries[0].attrib), {f"{{{pb.R}}}id"})
                else:
                    self.assertIsNone(entries)

    def test_notes_master_uses_an_independent_theme_part(self):
        spec = deck_spec()
        spec["slides"][0]["notes"] = "合成讲稿"
        out, _ = self._build(spec)
        with zipfile.ZipFile(out) as archive:
            root = ET.fromstring(archive.read("ppt/notesMasters/_rels/notesMaster1.xml.rels"))
            themes = [rel.get("Target") for rel in root if rel.get("Type") == pb.R + "/theme"]
            self.assertEqual(themes, ["../theme/theme2.xml"])
            self.assertEqual(archive.read("ppt/theme/theme1.xml"), archive.read("ppt/theme/theme2.xml"))

    def test_sections_group_all_slides_exactly_once(self):
        """章节覆盖全部幻灯片且不重复归组；引用的幻灯片 ID 有效。"""
        spec = deck_spec()
        spec["slides"].append({"key": "trend", "layout": "titleOnly", "title": "趋势页"})
        spec["sections"] = [{"name": "开场", "keys": ["cover"]}, {"name": "结论", "keys": ["sum", "trend"]}]
        out, _ = self._build(spec)
        presentation = self._part(out, "ppt/presentation.xml")

        self.assertIn('name="开场"', presentation)
        self.assertIn('name="结论"', presentation)
        self.assertIn("<p14:sldId id=", presentation)
        # 全覆盖反例：漏掉一页时构建前拒绝且不落盘
        bad = dict(spec)
        bad["sections"] = [{"name": "开场", "keys": ["cover"]}]
        out_path = self.root / "bad.pptx"
        with self.assertRaises(pb.DeckSpecError) as ctx:
            pb.build_deck(bad, out_path)
        self.assertEqual(ctx.exception.code, "sectionMissingSlides")
        self.assertFalse(out_path.exists())

    def test_rebuild_is_deterministic_and_identity_survives_reorder(self):
        """同规格重建同字节；幻灯片身份只由 key 决定，重排页序不挪身份。"""
        out_a, summary_a = self._build(name="a.pptx")
        out_b, summary_b = self._build(name="b.pptx")
        self.assertEqual(summary_a["sha256"], summary_b["sha256"])
        self.assertEqual(out_a.read_bytes(), out_b.read_bytes())

        spec = deck_spec()
        spec["slides"] = [spec["slides"][1], spec["slides"][0]]
        out_c, _ = self._build(spec, name="c.pptx")
        ids_a = [s["id"] for s in pr.read(out_a)["slides"]]
        ids_c = [s["id"] for s in pr.read(out_c)["slides"]]
        self.assertEqual(sorted(ids_a), sorted(ids_c))
        self.assertNotEqual(ids_a, ids_c)  # 页序换了，身份跟着 key 走

    def test_spec_rejections_fail_before_write(self):
        """规格反例逐个拒绝，且失败时不产出文件。"""
        cases = [
            ("slideKeyDup", lambda s: s["slides"].append(dict(s["slides"][0]))),
            ("hexColor", lambda s: s["theme"]["colors"].update({"accent1": "红色"})),
            ("layoutUnknown", lambda s: s["slides"][0].update({"layout": "fancy"})),
            (
                "shapeOutside",
                lambda s: s["slides"][1].update(
                    {"shapes": [{"shape": "rect", "x": 0, "y": 0, "cx": 999999999, "cy": 100000}]}
                ),
            ),
            ("notesEmpty", lambda s: s["slides"][0].update({"notes": " "})),
            (
                "bodyLayoutMismatch",
                lambda s: s["slides"][1].update({"layout": "titleOnly", "body": ["只读标题页不放正文"]}),
            ),
        ]
        for code, mutate in cases:
            with self.subTest(code=code):
                spec = deck_spec()
                mutate(spec)
                out = self.root / f"bad-{code}.pptx"
                with self.assertRaises(pb.DeckSpecError) as ctx:
                    pb.build_deck(spec, out)
                self.assertEqual(ctx.exception.code, code)
                self.assertFalse(out.exists())

    def test_native_charts_with_editable_data(self):
        """四类首版图表都是原生图表部件：轴引用、数据缓存与内嵌工作簿齐全。"""
        spec = deck_spec()
        charts = [
            {
                "chart": "column",
                "name": "季度收入",
                "x": 600000,
                "y": 1800000,
                "cx": 6000000,
                "cy": 3800000,
                "categories": ["一季度", "二季度", "三季度"],
                "series": [{"name": "收入", "values": [100, 112, 121]}],
                "legend": "bottom",
            },
            {
                "chart": "bar",
                "name": "渠道对比",
                "x": 600000,
                "y": 1800000,
                "cx": 6000000,
                "cy": 3800000,
                "categories": ["直营", "加盟"],
                "series": [{"name": "销售额", "values": [80, 60]}],
            },
            {
                "chart": "line",
                "name": "趋势",
                "x": 600000,
                "y": 1800000,
                "cx": 6000000,
                "cy": 3800000,
                "categories": ["1月", "2月", "3月"],
                "series": [{"name": "活跃", "values": [5, 7, 6]}],
            },
            {
                "chart": "pie",
                "name": "占比",
                "x": 600000,
                "y": 1800000,
                "cx": 6000000,
                "cy": 3800000,
                "categories": ["线上", "线下"],
                "series": [{"name": "订单", "values": [62, 38]}],
                "labels": "percent",
                "legend": "right",
            },
        ]
        spec["slides"] = [
            {"key": f"chart{i + 1}", "layout": "titleOnly", "title": f"图表 {i + 1}", "charts": [chart]}
            for i, chart in enumerate(charts)
        ]
        out, _ = self._build(spec)
        with zipfile.ZipFile(out) as archive:
            names = archive.namelist()
        for index in range(1, 5):
            self.assertIn(f"ppt/charts/chart{index}.xml", names)
            self.assertIn(f"ppt/embeddings/data{index}.xlsx", names)
            chart = self._part(out, f"ppt/charts/chart{index}.xml")
            self.assertNotIn("srgbClr", chart)  # 图表配色走主题引用，不落固定色
            rels = self._part(out, f"ppt/charts/_rels/chart{index}.xml.rels")
            self.assertIn(f'Target="../embeddings/data{index}.xlsx"', rels)
            slide = self._part(out, f"ppt/slides/slide{index}.xml")
            self.assertIn("graphicFrame", slide)
            self.assertIn('x="600000"', slide)

        column = self._part(out, "ppt/charts/chart1.xml")
        self.assertIn('<c:barChart><c:barDir val="col"/>', column)
        self.assertIn("<c:catAx>", column)
        self.assertIn("<c:valAx>", column)
        self.assertIn('<c:tickLblPos val="nextTo"/>', column)
        self.assertIn('<c:legendPos val="b"/>', column)
        self.assertIn("<c:v>112</c:v>", column)
        self.assertIn("<c:v>三季度</c:v>", column)
        # 轴的 crossAx 互相引用，数据缓存条数与规格一致
        self.assertRegex(column, r"<c:axId val=\"(\d+)\"/><c:scaling>")
        self.assertIn('<c:ptCount val="3"/>', column)

        bar = self._part(out, "ppt/charts/chart2.xml")
        self.assertIn('<c:barDir val="bar"/>', bar)
        line = self._part(out, "ppt/charts/chart3.xml")
        self.assertIn("<c:lineChart>", line)
        self.assertIn('<c:smooth val="0"/>', line)
        pie = self._part(out, "ppt/charts/chart4.xml")
        self.assertIn("<c:pieChart>", pie)
        self.assertIn('<c:showPercent val="1"/>', pie)
        self.assertNotIn("<c:catAx>", pie)  # 饼图没有轴

        # 内嵌工作簿可打开，数据与图表缓存一致（可编辑数据的载体）
        import io

        with zipfile.ZipFile(out) as outer:
            workbook_bytes = outer.read("ppt/embeddings/data1.xlsx")
        with zipfile.ZipFile(io.BytesIO(workbook_bytes)) as workbook:
            sheet = workbook.read("xl/worksheets/sheet1.xml").decode("utf-8")
        self.assertIn("<v>112</v>", sheet)
        self.assertIn("三季度", sheet)
        types = self._part(out, "[Content_Types].xml")
        self.assertIn('Extension="xlsx"', types)
        self.assertIn("/ppt/charts/chart1.xml", types)

    def test_chart_data_validation(self):
        """图表反例在写入前拒绝：类目与数值等长、类型封闭、饼图限定标签。"""
        base = {
            "chart": "column",
            "x": 0,
            "y": 0,
            "cx": 4000000,
            "cy": 3000000,
            "categories": ["甲", "乙"],
            "series": [{"name": "系列", "values": [1, 2]}],
        }
        cases = [
            ("chartUnknown", {**base, "chart": "scatter"}),
            ("chartCountMismatch", {**base, "series": [{"name": "系列", "values": [1]}]}),
            ("labelsPercentNeedsPie", {**base, "chart": "column", "labels": "percent"}),
        ]
        for code, chart in cases:
            with self.subTest(code=code):
                spec = deck_spec()
                spec["slides"] = [{"key": "c", "layout": "titleOnly", "title": "图", "charts": [chart]}]
                out = self.root / f"bad-{code}.pptx"
                with self.assertRaises(pb.DeckSpecError) as ctx:
                    pb.build_deck(spec, out)
                self.assertEqual(ctx.exception.code, code)
                self.assertFalse(out.exists())

    def test_speaker_notes_go_to_notes_parts(self):
        """讲稿写入对应幻灯片的备注部件；无讲稿的页面不补造空备注。"""
        spec = deck_spec()
        spec["slides"][0]["notes"] = "开场强调结论。\n过渡：先看收入。"
        out, _ = self._build(spec)
        with zipfile.ZipFile(out) as archive:
            names = archive.namelist()
        self.assertIn("ppt/notesSlides/notesSlide1.xml", names)
        self.assertNotIn("ppt/notesSlides/notesSlide2.xml", names)
        self.assertIn("ppt/notesMasters/notesMaster1.xml", names)
        notes = self._part(out, "ppt/notesSlides/notesSlide1.xml")
        self.assertIn("开场强调结论。", notes)
        self.assertIn("过渡：先看收入。", notes)
        notes_rels = self._part(out, "ppt/notesSlides/_rels/notesSlide1.xml.rels")
        self.assertIn("../notesMasters/notesMaster1.xml", notes_rels)
        self.assertIn("../slides/slide1.xml", notes_rels)
        slide_rels = self._part(out, "ppt/slides/_rels/slide1.xml.rels")
        self.assertIn(
            'Type="%s/notesSlide"' % "http://schemas.openxmlformats.org/officeDocument/2006/relationships", slide_rels
        )
        presentation = self._part(out, "ppt/presentation.xml")
        self.assertIn("notesMasterIdLst", presentation)

    def test_no_notes_when_unwanted(self):
        """无讲稿时不产生备注部件，也不写空讲稿。"""
        out, _ = self._build()
        with zipfile.ZipFile(out) as archive:
            names = [n for n in archive.namelist() if "notes" in n.lower()]
        self.assertEqual(names, [])

    def test_master_element_order_follows_ecma_sequence(self):
        """母版子元素按 ECMA-376 序列：hf 必须在 txStyles 之前（无修复提示的前提）。"""
        spec = deck_spec()
        spec["master"] = {"footer": True, "pageNumber": True, "logo": None}
        out, _ = self._build(spec)
        master = self._part(out, "ppt/slideMasters/slideMaster1.xml")
        hf_index = master.find("<p:hf")
        styles_index = master.find("<p:txStyles")
        self.assertGreater(hf_index, 0)
        self.assertGreater(styles_index, hf_index)
        # 关闭公共元素时没有 hf 元素，顺序约束自然满足
        spec["master"] = {"footer": False, "pageNumber": False, "logo": None}
        out2, _ = self._build(spec, name="nohf.pptx")
        self.assertNotIn("<p:hf", self._part(out2, "ppt/slideMasters/slideMaster1.xml"))

    def test_cli_reports_chart_spec_errors_exit_two(self):
        """CLI 级反例：不支持的图表类型按规格错误退出码 2，打印可读文案而非 traceback。"""
        spec = deck_spec()
        spec["slides"][1]["charts"] = [
            {
                "chart": "scatter",
                "x": 0,
                "y": 0,
                "cx": 1000000,
                "cy": 1000000,
                "categories": ["甲"],
                "series": [{"name": "系列", "values": [1]}],
            }
        ]
        spec_path = self.root / "bad-chart.json"
        spec_path.write_text(json.dumps(spec, ensure_ascii=False), encoding="utf-8")
        out = self.root / "bad-chart.pptx"
        script = ROOT / "skill" / "annotation-everything" / "scripts" / "pptx_build.py"
        done = subprocess.run(
            [sys.executable, str(script), "--deck", str(spec_path), "--out", str(out)],
            capture_output=True,
            text=True,
            timeout=60,
        )
        self.assertEqual(done.returncode, 2, done.stdout + done.stderr)
        self.assertNotIn("Traceback", done.stderr)
        self.assertIn("scatter", done.stderr)
        self.assertFalse(out.exists())

    def test_cli_reports_build_summary(self):
        """CLI 包装：--json 输出构建摘要，退出码 0。"""
        import json
        import subprocess

        spec_path = self.root / "deck.json"
        spec_path.write_text(json.dumps(deck_spec(), ensure_ascii=False), encoding="utf-8")
        out = self.root / "cli.pptx"
        script = ROOT / "skill" / "annotation-everything" / "scripts" / "pptx_build.py"
        done = subprocess.run(
            [sys.executable, str(script), "--deck", str(spec_path), "--out", str(out), "--json"],
            capture_output=True,
            text=True,
            timeout=60,
        )
        self.assertEqual(done.returncode, 0, done.stderr)
        summary = json.loads(done.stdout)
        self.assertEqual(summary["schema"], "ae-pptx-build/1")
        self.assertEqual(summary["pages"], 2)
        self.assertTrue(out.exists())


if __name__ == "__main__":
    unittest.main()
