"""#152：白名单几何与样式回写——经公开入口（writeback(ops=…) 与 CLI）断言契约。

几何：归一化坐标 → 源文件 EMU 的换算在不同画布尺寸下正确；越界、非法值与不支持
单位拒绝。样式：白名单内的文本属性/填充/线条直接格式可回写，未知属性保守拒绝。
多元素逐目标预检，任一不安全整组不写入；自动重叠/缩小警告与视觉核验分开记录。
"""
from __future__ import annotations

import hashlib
import io
import json
import sys
import tempfile
import unittest
import zipfile
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

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


if __name__ == "__main__":
    unittest.main()
