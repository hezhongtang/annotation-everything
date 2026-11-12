"""#152：白名单几何与样式回写——经公开入口（writeback(ops=…) 与 CLI）断言契约。

几何：归一化坐标 → 源文件 EMU 的换算在不同画布尺寸下正确；越界、非法值与不支持
单位拒绝。样式：白名单内的文本属性/填充/线条直接格式可回写，未知属性保守拒绝。
多元素逐目标预检，任一不安全整组不写入；自动重叠/缩小警告与视觉核验分开记录。
"""
from __future__ import annotations

import hashlib
import io
import json
import os
import sys
import tempfile
import unittest
import zipfile
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "skill/annotation-everything/scripts"))
sys.path.insert(0, str(ROOT / "fixtures/pptx"))
import make_pptx as mp
import pptx_edit as pe
import pptx_reader as pr

CANVAS_W, CANVAS_H = 12192000, 6858000


def member(raw, name):
    with zipfile.ZipFile(io.BytesIO(raw)) as zf:
        return zf.read(name)


def local_records(raw):
    with zipfile.ZipFile(io.BytesIO(raw)) as zf:
        infos = sorted(zf.infolist(), key=lambda i: i.header_offset)
        return {i.filename: raw[i.header_offset:infos[n + 1].header_offset if n + 1 < len(infos) else zf.start_dir]
                for n, i in enumerate(infos)}


def styled_parts(portrait: bool = False) -> dict[str, str | bytes]:
    parts = mp.sample_parts(portrait=portrait)
    parts["ppt/slides/slide10.xml"] = mp.styled_slide_xml()
    return parts


def geometry(**set_values):
    return {"unit": "normalized", "set": set_values}


def text_style(**props):
    return {"scope": "text", "props": props}


def fill_style(**props):
    return {"scope": "fill", "props": props}


def line_style(**props):
    return {"scope": "line", "props": props}


class PptxOpsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="ae-pptx-ops-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.review = self.root / "review"
        self.review.mkdir()
        self.source = self.root / "synthetic.pptx"

    def build(self, parts=None, portrait=False):
        parts = parts if parts is not None else styled_parts(portrait=portrait)
        mp.write_package(self.source, parts)
        self.original = self.source.read_bytes()
        self.fp = hashlib.sha256(self.original).hexdigest()
        model = pr.read(self.source)
        self.model = model
        self.manifest = {"format": {"report": "pptx", "version": 1},
                         "sources": {"report": "../synthetic.pptx"},
                         "pptx": {"fingerprint": self.fp},
                         "pages": [{"n": s["n"], "key": s["key"], "title": "", "stable": True}
                                   for s in model["slides"]]}
        (self.review / "review.json").write_text(json.dumps(self.manifest), encoding="utf-8")
        return self.source

    def element(self, slide_n=1, elem_id="2"):
        slide = self.model["slides"][slide_n - 1]
        el = next(e for e in slide["elements"] if str(e["id"]) == str(elem_id))
        return slide, el

    def target(self, slide_n=1, elem_id="2", **over):
        _slide, el = self.element(slide_n, elem_id)
        target = {"key": el["key"], "id": str(el["id"] or ""), "name": el.get("name", ""),
                  "kind": el["kind"], "z": el.get("z", 0), "digest": el["evidenceDigest"]}
        target.update(over)
        return target

    def op(self, detail=None, *, aspect="geometry", slide_n=1, elem_id="2", **over):
        o = {"slide": self.model["slides"][slide_n - 1]["key"], "target": self.target(slide_n, elem_id),
             "aspect": aspect}
        if detail:
            o.update(detail)
        o.update(over)
        return o

    def geometry(self, unit="normalized", **set_values):
        return {"unit": unit, "set": set_values}

    def refresh_fp(self):
        """真实写入后源文件变化：按新字节同步 manifest 指纹（等价重建对照页后的重锚定）。"""
        self.fp = hashlib.sha256(self.source.read_bytes()).hexdigest()
        self.manifest["pptx"]["fingerprint"] = self.fp
        (self.review / "review.json").write_text(json.dumps(self.manifest), encoding="utf-8")

    def writeback(self, ops, **kwargs):
        if isinstance(ops, list):
            ops = {"v": pe.OPS_VERSION, "ops": ops}
        return pe.writeback(self.review, ops=ops, **kwargs)

    def assert_refused(self, code, ops, **kwargs):
        with self.assertRaises(pe.EditError) as caught:
            self.writeback(ops, **kwargs)
        errors = caught.exception.errors if isinstance(caught.exception, pe.SelectionError) else [caught.exception]
        self.assertIn(code, [e.code for e in errors])
        self.assertEqual(self.source.read_bytes(), self.original)
        return errors

    # ---------- 切片1：几何回写公开入口与归一化换算 ----------
    def test_geometry_move_and_resize_roundtrip_in_emu(self):
        self.build()
        ops = [self.op(geometry(x=0.25, y=0.125, width=0.5, height=0.25))]
        result = self.writeback(ops, dry_run=True)
        self.assertTrue(result["dryRun"])
        self.assertIsNone(result["backup"])
        change = result["changes"][0]
        self.assertEqual(change["aspect"], "geometry")
        props = {p["name"]: p for p in change["props"]}
        self.assertEqual(props["x"], {"name": "x", "unit": "emu", "from": 914400, "to": 3048000})
        self.assertEqual(props["y"], {"name": "y", "unit": "emu", "from": 914400, "to": 857250})
        self.assertEqual(props["cx"], {"name": "cx", "unit": "emu", "from": 5486400, "to": 6096000})
        self.assertEqual(props["cy"], {"name": "cy", "unit": "emu", "from": 914400, "to": 1714500})
        self.assertEqual(self.source.read_bytes(), self.original)
        self.assertFalse((self.review / ".snapshots").exists())
        result = self.writeback(ops)
        self.assertEqual(result["changes"][0]["props"], change["props"])
        self.assertEqual(result["pages"], [1])
        xml = member(self.source.read_bytes(), "ppt/slides/slide10.xml")
        self.assertIn(b'<a:off x="3048000" y="857250"/>', xml)
        self.assertIn(b'<a:ext cx="6096000" cy="1714500"/>', xml)
        model = pr.read(self.source)
        el = next(e for e in model["slides"][0]["elements"] if str(e["id"]) == "2")
        self.assertAlmostEqual(el["geometry"]["normalized"]["x"], 0.25, places=6)
        self.assertAlmostEqual(el["geometry"]["normalized"]["height"], 0.25, places=6)
        self.assertEqual([p["text"] for p in el["paragraphs"]], ["标题甲标题乙"])
        backup = Path(result["backup"])
        self.assertEqual(backup.read_bytes(), self.original)
        self.assertIn("batch-0/pptx-writeback", str(backup))

    def test_geometry_position_only_keeps_size_bytes(self):
        self.build()
        self.writeback([self.op(geometry(y=0.5))])
        xml = member(self.source.read_bytes(), "ppt/slides/slide10.xml")
        self.assertIn(b'<a:off x="914400" y="3429000"/>', xml)
        self.assertIn(b'<a:ext cx="5486400" cy="914400"/>', xml, "未设置尺寸时 ext 字节原样保留")

    def test_geometry_size_only_keeps_position_bytes(self):
        self.build()
        self.writeback([self.op(geometry(width=0.25, height=0.125))])
        xml = member(self.source.read_bytes(), "ppt/slides/slide10.xml")
        self.assertIn(b'<a:off x="914400" y="914400"/>', xml, "未设置位置时 off 字节原样保留")
        self.assertIn(b'<a:ext cx="3048000" cy="857250"/>', xml)

    def test_geometry_conversion_follows_canvas_size(self):
        self.build(portrait=True)
        self.writeback([self.op(geometry(x=0.25, y=0.125, width=0.5, height=0.25))])
        xml = member(self.source.read_bytes(), "ppt/slides/slide10.xml")
        self.assertIn(b'<a:off x="1714500" y="1524000"/>', xml)
        self.assertIn(b'<a:ext cx="3429000" cy="3048000"/>', xml)

    # ---------- 切片3：几何拒绝矩阵 ----------
    def test_geometry_out_of_bounds_and_illegal_values_are_refused(self):
        self.build()
        cases = [
            ({"set": {"x": -0.1, "y": 0.1}}, "bounds"),
            ({"set": {"x": 0.9, "y": 0.1, "width": 0.2}}, "bounds"),
            ({"set": {"x": 0.1, "y": 0.9, "height": 0.2}}, "bounds"),
            ({"set": {"x": 0.1, "y": 0.1, "width": 1.0002 - 0.1}}, "bounds"),
            ({"set": {"x": 0.1, "y": 0.1, "width": 0, "height": 0.1}}, "value"),
            ({"set": {"x": 0.1, "y": 0.1, "height": -1}}, "value"),
            ({"set": {"x": 0.1, "y": 0.1, "width": 1e-9, "height": 0.1}}, "value"),
            ({"set": {"x": "0.1", "y": 0.1, "width": 0.2, "height": 0.1}}, "value"),
            ({"set": {"x": float("nan"), "width": 0.2, "height": 0.1}}, "value"),
        ]
        for over, code in cases:
            with self.subTest(over=over, code=code):
                set_values = over.pop("set")
                self.assert_refused(code, [self.op(geometry(**set_values), **over)])

    def test_geometry_unit_is_explicit_and_normalized_only(self):
        self.build()
        set_values = {"x": 0.25, "y": 0.1}
        self.assert_refused("unit", [self.op({"set": set_values})])   # 缺 unit
        self.assert_refused("unit", [self.op({"unit": "emu", "set": set_values})])
        self.assert_refused("unit", [self.op({"unit": "px", "set": set_values})])

    def test_geometry_unknown_fields_and_properties_are_refused(self):
        self.build()
        self.assert_refused("property", [self.op(geometry(x=0.2, y=0.2, left=0.1))])
        self.assert_refused("ops", [self.op(geometry(x=0.2), extra=True)])
        self.assert_refused("ops", [{"slide": self.model["slides"][0]["key"], "aspect": "geometry",
                                     "unit": "normalized", "target": self.target()}])
        self.assert_refused("version", {"v": pe.OPS_VERSION + 1,
                                        "ops": [self.op(geometry(x=0.2, y=0.2))]})

    def test_geometry_targets_outside_the_whitelist_are_refused(self):
        self.build()
        for slide_n, elem_id, kind in ((1, "4", "shape"), (2, "13", "shape"),
                                       (2, "11", "picture"), (2, "10", "table")):
            with self.subTest(elem_id=elem_id, kind=kind):
                errors = self.assert_refused(
                    "readonlyGeometry",
                    [self.op(geometry(x=0.1, y=0.1, width=0.2, height=0.1), slide_n=slide_n, elem_id=elem_id)])
                self.assertEqual(errors[0].detail, kind)

    def test_geometry_duplicate_shape_id_stays_readonly(self):
        parts = styled_parts()
        shape = ('<p:sp><p:nvSpPr><p:cNvPr id="9" name="重复"/><p:cNvSpPr txBox="1"/><p:nvPr/></p:nvSpPr>'
                 '<p:spPr><a:xfrm><a:off x="0" y="0"/><a:ext cx="1000000" cy="1000000"/></a:xfrm></p:spPr>'
                 '<p:txBody><a:bodyPr/><a:lstStyle/><a:p><a:r><a:t>重复</a:t></a:r></a:p></p:txBody></p:sp>')
        parts["ppt/slides/slide10.xml"] = parts["ppt/slides/slide10.xml"].replace(
            "</p:spTree>", shape + shape.replace('x="0"', 'x="2000000"') + "</p:spTree>")
        self.build(parts)
        model = pr.read(self.source)
        el = next(e for e in model["slides"][0]["elements"] if str(e["id"]) == "9")
        target = {"key": el["key"], "id": "9", "name": el.get("name", ""), "kind": el["kind"],
                  "z": el.get("z", 0), "digest": el["evidenceDigest"]}
        # 标识重复但证据可消歧：目标能按证据找回，可重复标识的能力已被清空，保持只读
        self.assert_refused("readonlyGeometry", [{"slide": model["slides"][0]["key"], "target": target,
                                                  "aspect": "geometry", "unit": "normalized",
                                                  "set": {"x": 0.1, "y": 0.1}}])

    def test_geometry_duplicate_shape_id_with_conflicting_evidence_is_ambiguous(self):
        parts = styled_parts()
        parts["ppt/slides/slide10.xml"] = parts["ppt/slides/slide10.xml"].replace(
            "</p:spTree>",
            '<p:sp><p:nvSpPr><p:cNvPr id="9" name="重复"/><p:cNvSpPr txBox="1"/><p:nvPr/></p:nvSpPr>'
            '<p:spPr><a:xfrm><a:off x="0" y="0"/><a:ext cx="1000000" cy="1000000"/></a:xfrm></p:spPr>'
            '<p:txBody><a:bodyPr/><a:lstStyle/><a:p><a:r><a:t>重复</a:t></a:r></a:p></p:txBody></p:sp>'
            '<p:sp><p:nvSpPr><p:cNvPr id="9" name="重复"/><p:cNvSpPr txBox="1"/><p:nvPr/></p:nvSpPr>'
            '<p:spPr><a:xfrm><a:off x="0" y="0"/><a:ext cx="1000000" cy="1000000"/></a:xfrm></p:spPr>'
            '<p:txBody><a:bodyPr/><a:lstStyle/><a:p><a:r><a:t>重复</a:t></a:r></a:p></p:txBody></p:sp></p:spTree>')
        self.build(parts)
        model = pr.read(self.source)
        el = next(e for e in model["slides"][0]["elements"] if str(e["id"]) == "9")
        target = {"key": el["key"], "id": "9", "name": el.get("name", ""), "kind": el["kind"],
                  "z": el.get("z", 0), "digest": el["evidenceDigest"]}
        self.assert_refused("ambiguous", [{"slide": model["slides"][0]["key"], "target": target,
                                           "aspect": "geometry", "unit": "normalized",
                                           "set": {"x": 0.1, "y": 0.1}}])

    def test_same_element_claimed_by_two_ops_refuses_the_whole_group(self):
        self.build()
        one = self.op(geometry(x=0.2, y=0.2), id="one")
        two = self.op(geometry(width=0.3, height=0.3), id="two")
        errors = self.assert_refused("conflict", [one, two])
        self.assertEqual(errors[0].item, "two")
        self.assertEqual(errors[0].detail, "one")

    def test_geometry_no_change_is_refused(self):
        self.build()
        self.assert_refused("noChange", [self.op(geometry(x=0.075))])

    def test_bad_target_identity_is_refused(self):
        self.build()
        self.assert_refused("ops", [self.op(geometry(x=0.2, y=0.2),
                                            target=self.target(digest="not-a-digest"))])
        self.assert_refused("anchor", [self.op(geometry(x=0.2, y=0.2),
                                               slide="s0000000000000000000000000000000000000000000000000000000000000000")])

    # ---------- 切片4：文本样式回写 ----------
    def test_text_style_updates_existing_and_missing_rpr(self):
        self.build()
        result = self.writeback([self.op(text_style(bold=True, size=24, color="00AA00", underline=True),
                                         aspect="style")], dry_run=True)
        props = {p["name"]: p for p in result["changes"][0]["props"]}
        self.assertEqual(props["bold"], {"name": "bold", "unit": None, "from": "mixed", "to": True})
        self.assertEqual(props["size"], {"name": "size", "unit": "pt", "from": "mixed", "to": 24.0})
        self.assertEqual(props["color"], {"name": "color", "unit": "srgb", "from": "mixed", "to": "00AA00"})
        self.assertEqual(self.source.read_bytes(), self.original)
        result = self.writeback([self.op(text_style(bold=True, size=24, color="00AA00", underline=True),
                                         aspect="style")])
        xml = member(self.source.read_bytes(), "ppt/slides/slide10.xml")
        self.assertIn(b'<a:rPr b="1" sz="2400" u="sng"><a:solidFill><a:srgbClr val="00AA00"/></a:solidFill>'
                      b'<a:latin typeface="Synthetic Sans"/></a:rPr>', xml,
                      "已有 rPr 保留未触及属性，仅改白名单字段")
        self.assertIn('<a:r><a:rPr b="1" u="sng" sz="2400">'
                      '<a:solidFill><a:srgbClr val="00AA00"/></a:solidFill></a:rPr>'
                      "<a:t>标题乙</a:t></a:r>".encode("utf-8"), xml, "无 rPr 的 run 合成最小 rPr")
        model = pr.read(self.source)
        runs = model["slides"][0]["elements"][0]["paragraphs"][0]["runs"]
        self.assertEqual(runs[0]["style"]["color"], "00AA00")
        self.assertTrue(runs[0]["style"]["bold"] and runs[1]["style"]["bold"])
        self.assertEqual(runs[0]["text"] + runs[1]["text"], "标题甲标题乙", "文字内容不变")

    def test_text_style_false_values_write_explicit_off(self):
        self.build()
        self.writeback([self.op(text_style(bold=False, underline=False), aspect="style")])
        xml = member(self.source.read_bytes(), "ppt/slides/slide10.xml")
        self.assertIn(b'<a:rPr b="0" u="none"', xml)
        model = pr.read(self.source)
        runs = model["slides"][0]["elements"][0]["paragraphs"][0]["runs"]
        self.assertFalse(runs[1]["style"]["bold"])
        self.assertEqual(runs[1]["style"]["underline"], "none")

    def test_text_style_value_and_property_guards(self):
        self.build()
        self.assert_refused("property", [self.op(text_style(fontname="Synthetic Sans"), aspect="style")])
        for props, code in (({"size": 0.5}, "value"), ({"size": 4000.5}, "value"),
                            ({"color": "XYZ123"}, "value"), ({"bold": "yes"}, "value"),
                            ({"width": 2}, "property")):
            with self.subTest(props=props):
                self.assert_refused(code, [self.op(text_style(**props), aspect="style")])

    def test_text_style_no_change_is_refused(self):
        parts = styled_parts()
        parts["ppt/slides/slide10.xml"] = parts["ppt/slides/slide10.xml"].replace(
            "<a:rPr b=\"1\" sz=\"1800\">", "<a:rPr b=\"1\" sz=\"1800\" i=\"0\" u=\"none\">")
        parts["ppt/slides/slide10.xml"] = parts["ppt/slides/slide10.xml"].replace(
            "<a:r><a:t>标题乙</a:t></a:r>",
            "<a:r><a:rPr b=\"1\" sz=\"1800\" i=\"0\" u=\"none\"/><a:t>标题乙</a:t></a:r>")
        self.build(parts)
        self.assert_refused("noChange", [self.op(text_style(bold=True, italic=False, underline=False),
                                                 aspect="style")])

    # ---------- 切片5：填充与线条回写 ----------
    def test_fill_replaces_existing_and_inserts_when_absent(self):
        self.build()
        result = self.writeback([self.op(fill_style(color="FF8800"), aspect="style")], dry_run=True)
        self.assertEqual(result["changes"][0]["props"],
                         [{"name": "color", "unit": "srgb", "from": "4477AA", "to": "FF8800"}])
        self.writeback([self.op(fill_style(color="FF8800"), aspect="style")])
        xml = member(self.source.read_bytes(), "ppt/slides/slide10.xml")
        self.assertIn(b'<a:solidFill><a:srgbClr val="FF8800"/></a:solidFill>'
                      b'<a:ln w="19050">', xml, "替换原填充，a:ln 原样保留")
        self.refresh_fp()
        self.writeback([self.op(fill_style(color="00BB00"), aspect="style", slide_n=1, elem_id="3")])
        xml = member(self.source.read_bytes(), "ppt/slides/slide10.xml")
        self.assertIn(b'<a:ext cx="3000000" cy="600000"/></a:xfrm><a:solidFill>'
                      b'<a:srgbClr val="00BB00"/></a:solidFill></p:spPr>', xml,
                      "无填充的形状按子元素顺序插入")

    def test_fill_no_change_is_refused(self):
        self.build()
        self.assert_refused("noChange", [self.op(fill_style(color="4477AA"), aspect="style")])

    def test_line_updates_width_and_color_keeping_other_children(self):
        self.build()
        result = self.writeback([self.op(line_style(width=2, color="00FF00"), aspect="style")], dry_run=True)
        props = {p["name"]: p for p in result["changes"][0]["props"]}
        self.assertEqual(props["width"]["from"], 1.5)
        self.assertEqual(props["width"]["to"], 2.0)
        self.assertEqual(props["color"], {"name": "color", "unit": "srgb", "from": "112233", "to": "00FF00"})
        self.writeback([self.op(line_style(width=2, color="00FF00"), aspect="style")])
        xml = member(self.source.read_bytes(), "ppt/slides/slide10.xml")
        self.assertIn(b'<a:ln w="25400"><a:solidFill><a:srgbClr val="00FF00"/></a:solidFill>'
                      b'<a:prstDash val="dash"/></a:ln>', xml, "prstDash 等未触及子元素保留")
        self.refresh_fp()
        self.writeback([self.op(line_style(color="123456"), aspect="style", slide_n=1, elem_id="3")])
        xml = member(self.source.read_bytes(), "ppt/slides/slide10.xml")
        self.assertIn(b'</a:xfrm><a:ln><a:solidFill><a:srgbClr val="123456"/></a:solidFill></a:ln></p:spPr>',
                      xml, "无线条的形状按子元素顺序插入 a:ln")
        self.refresh_fp()
        self.writeback([self.op(line_style(width=0.5), aspect="style", slide_n=1, elem_id="3")])
        xml = member(self.source.read_bytes(), "ppt/slides/slide10.xml")
        self.assertIn(b'<a:ln w="6350"><a:solidFill><a:srgbClr val="123456"/></a:solidFill></a:ln>', xml)

    def test_line_no_change_is_refused(self):
        self.build()
        self.assert_refused("noChange", [self.op(line_style(width=1.5, color="112233"), aspect="style")])

    def test_structure_outside_whitelist_is_refused(self):
        parts = styled_parts()
        parts["ppt/slides/slide10.xml"] = parts["ppt/slides/slide10.xml"].replace(
            '<a:ln w="19050">', '<a:weird/><a:ln w="19050">')
        self.build(parts)
        self.assert_refused("structure", [self.op(fill_style(color="FF8800"), aspect="style")])
        parts = styled_parts()
        parts["ppt/slides/slide10.xml"] = parts["ppt/slides/slide10.xml"].replace(
            "<a:prstDash val=\"dash\"/></a:ln>", "<a:prstDash val=\"dash\"/><a:mystery/></a:ln>")
        self.build(parts)
        self.assert_refused("structure", [self.op(line_style(color="00FF00"), aspect="style")])
        parts = styled_parts()
        parts["ppt/slides/slide10.xml"] = parts["ppt/slides/slide10.xml"].replace(
            '<a:latin typeface="Synthetic Sans"/>', '<a:latin typeface="Synthetic Sans"/><a:mystery/>')
        self.build(parts)
        self.assert_refused("structure", [self.op(text_style(color="00AA00"), aspect="style")])

    # ---------- 切片6：占位符与多元素整组预检 ----------
    def test_placeholder_geometry_and_style_are_writable_with_explicit_xfrm(self):
        self.build()
        self.writeback([self.op(geometry(x=0.5, y=0.06), slide_n=1, elem_id="3")])
        self.refresh_fp()
        # 同一元素的多类修改按契约拆成多次操作：一次回写里同一元素只允许一个操作
        current = self.source.read_bytes()
        with self.assertRaises(pe.EditError) as caught:
            self.writeback([self.op(geometry(x=0.4, y=0.06), slide_n=1, elem_id="3"),
                            self.op(text_style(bold=True), aspect="style", slide_n=1, elem_id="3")])
        self.assertIn("conflict", [e.code for e in caught.exception.errors])
        self.assertEqual(self.source.read_bytes(), current, "冲突时整组不写入")
        self.writeback([self.op(text_style(bold=True), aspect="style", slide_n=1, elem_id="3")])
        xml = member(self.source.read_bytes(), "ppt/slides/slide10.xml")
        self.assertIn(b'<a:off x="6096000" y="411480"/>', xml)
        self.assertIn('<a:r><a:rPr b="1"/><a:t>占位</a:t></a:r>'.encode("utf-8"), xml)
        model = pr.read(self.source)
        el = next(e for e in model["slides"][0]["elements"] if str(e["id"]) == "3")
        self.assertEqual(el["kind"], "shape", "占位符仍按普通形状参与能力矩阵")

    def test_mixed_group_refuses_everything_when_one_target_is_unsafe(self):
        self.build()
        safe = self.op(geometry(x=0.2, y=0.2), id="safe")
        unsafe = self.op(geometry(x=0.5, y=0.5), slide_n=2, elem_id="13", id="unsafe")
        errors = self.assert_refused("readonlyGeometry", [safe, unsafe])
        self.assertEqual([e.item for e in errors], ["unsafe"])
        xml = member(self.source.read_bytes(), "ppt/slides/slide10.xml")
        self.assertIn(b'<a:off x="914400" y="914400"/>', xml, "安全目标的字节同样不写入")

    def test_multi_element_group_writes_each_target(self):
        self.build()
        one = self.op(geometry(x=0.2, y=0.2), id="one")
        two = self.op(geometry(x=0.6, y=0.6), slide_n=1, elem_id="3", id="two")
        three = self.op(text_style(bold=True), aspect="style", slide_n=1, elem_id="4", id="three")
        result = self.writeback([one, two, three])
        self.assertEqual(result["ids"], ["one", "two", "three"])
        self.assertEqual(result["pages"], [1])
        xml = member(self.source.read_bytes(), "ppt/slides/slide10.xml")
        self.assertIn(b'<a:off x="2438400" y="1371600"/>', xml)
        self.assertIn(b'<a:off x="7315200" y="4114800"/>', xml)
        self.assertIn('<a:r><a:rPr b="1"/><a:t>没有变换</a:t></a:r>'.encode("utf-8"), xml)

    # ---------- 切片7：警告与视觉核验分离 ----------
    def test_automatic_warnings_are_recorded_separately_from_visual(self):
        self.build()
        result = self.writeback([self.op(geometry(x=0.05, y=0.02, width=0.2, height=0.06))])
        codes = [w["code"] for w in result["warnings"]]
        self.assertIn("overlap", codes)
        self.assertIn("shrink", codes)
        overlap = next(w for w in result["warnings"] if w["code"] == "overlap")
        self.assertEqual(overlap["with"], [k for k in
                        (e["key"] for e in self.model["slides"][0]["elements"]
                         if str(e["id"]) == "3")])
        self.assertEqual(result["visual"], {"status": "notRun", "pages": [1]})
        self.assertEqual(result["warnings"] == [], False)

    def test_out_of_canvas_tolerance_warns_instead_of_refusing(self):
        self.build()
        result = self.writeback([self.op(geometry(x=0.99999, y=0.1, width=0.00002, height=0.1))])
        self.assertIn("outOfCanvas", [w["code"] for w in result["warnings"]])
        xml = member(self.source.read_bytes(), "ppt/slides/slide10.xml")
        self.assertIn(b'<a:off x="12191878" y="685800"/>', xml)

    def test_text_apply_result_has_no_visual_field(self):
        # 既有文本回写契约保持不变（无 ops 时 result 不新增 visual 字段）
        self.build()
        import notes_store as store
        _slide, el = self.element(1, "3")
        para = el["paragraphs"][0]
        doc, note = store.add(self.review, {
            "page": 1, "target": "报告", "quoteFrom": "报告", "quote": "占位",
            "text": "改为：新文",
            "pptx": {"v": 1, "slide": _slide["key"], "kind": "text", "via": "selection",
                     "fp": self.fp, "elements": [self.target(slide_n=1, elem_id="3")],
                     "text": {"para": 0, "paraDigest": para["digest"], "quote": "占位"}},
            "edit": {"op": "replace", "from": "占位", "to": "新文"}})
        result = pe.writeback(self.review, ids=[note["id"]])
        self.assertNotIn("visual", result)
        self.assertEqual(result["warnings"], [])

    # ---------- 切片8：未触及保留、回滚与 CLI ----------
    def test_untouched_members_and_slices_stay_byte_identical(self):
        self.build()
        self.writeback([self.op(geometry(x=0.3, y=0.3)),
                        self.op(text_style(color="00AA00"), aspect="style", slide_n=1, elem_id="3")])
        before, after = local_records(self.original), local_records(self.source.read_bytes())
        self.assertEqual(sorted(before), sorted(after))
        for name in before:
            if name != "ppt/slides/slide10.xml":
                self.assertEqual(after[name], before[name], name)
        old_xml = member(self.original, "ppt/slides/slide10.xml").decode("utf-8")
        new_xml = member(self.source.read_bytes(), "ppt/slides/slide10.xml").decode("utf-8")
        for fragment in ('<p:sp><p:nvSpPr><p:cNvPr id="3"', '<a:t>标题乙</a:t>'):
            self.assertEqual(old_xml.count(fragment), new_xml.count(fragment), fragment)
        untouched_before = old_xml[old_xml.index('<p:cNvPr id="3"'):]
        self.assertIn(untouched_before[:200], new_xml, "未触及形状起始片段不变")

    def test_write_failure_rolls_back_and_keeps_backup(self):
        self.build()
        with patch.object(pe.os, "fsync", side_effect=[None, OSError("disk full")]), \
                self.assertRaises(OSError):
            self.writeback([self.op(geometry(x=0.3, y=0.3))])
        self.assertEqual(self.source.read_bytes(), self.original)
        backups = list((self.review / ".snapshots").rglob("*.pptx"))
        self.assertTrue(backups and all(p.read_bytes() == self.original for p in backups))
        self.assertFalse(list(self.root.glob("*.tmp")))

    def test_cli_ops_json_locales_and_argument_guards(self):
        self.build()
        ops_file = self.root / "ops.json"
        ops_file.write_text(json.dumps({"v": 1, "ops": [self.op(geometry(x=0.25, y=0.25), id="move")]},
                                       ensure_ascii=False), encoding="utf-8")
        for lang, phrase in (("zh-CN", "预演通过"), ("en", "Preview passed")):
            out, err = io.StringIO(), io.StringIO()
            with redirect_stdout(out), redirect_stderr(err):
                code = pe.main(["ops", str(self.review), "--ops", str(ops_file),
                                "--dry-run", "--json", "--locale", lang])
            self.assertEqual(code, 0, err.getvalue())
            self.assertTrue(json.loads(out.getvalue())["dryRun"])
            out, err = io.StringIO(), io.StringIO()
            with redirect_stdout(out), redirect_stderr(err):
                code = pe.main(["ops", str(self.review), "--ops", str(ops_file), "--dry-run",
                                "--locale", lang])
            self.assertEqual(code, 0, err.getvalue())
            self.assertIn(phrase, out.getvalue())
            self.assertIn("move", out.getvalue())
        self.assertEqual(self.source.read_bytes(), self.original)
        # apply 与 ops 的参数互斥
        for args in (["apply", str(self.review), "--ops", str(ops_file)],
                     ["ops", str(self.review)],
                     ["ops", str(self.review), "--ops", str(ops_file), "--batch", "1"],
                     ["ops", str(self.review), "--ops", str(ops_file), "--id", "x"]):
            with self.subTest(args=args), patch.dict(os.environ, {"AE_LOCALE": "zh-CN"}):
                out, err = io.StringIO(), io.StringIO()
                with redirect_stdout(out), redirect_stderr(err):
                    try:
                        code = pe.main(args)
                    except SystemExit as exc:
                        code = exc.code
                self.assertEqual(code, 2)
                self.assertIn("apply", err.getvalue())
        self.assertEqual(self.source.read_bytes(), self.original)


if __name__ == "__main__":
    unittest.main()
