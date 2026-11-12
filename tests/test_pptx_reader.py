"""PPTX 安全读取契约测试；全部材料由标准库现场合成。"""
from __future__ import annotations

import hashlib
from pathlib import Path
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "skill" / "annotation-everything" / "scripts"))
sys.path.insert(0, str(ROOT / "fixtures" / "pptx"))

import make_pptx as mp  # noqa: E402
import pptx_reader as pr  # noqa: E402


class PptxReaderTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="ae-pptx-reader-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def test_slide_order_identity_canvas_and_part_relationships(self):
        path = mp.build_sample(self.root / "sample.pptx")
        result = pr.read(path)

        self.assertEqual(result["formatVersion"], 1)
        self.assertEqual(result["capabilityVersion"], 1)
        self.assertEqual(result["fingerprint"], hashlib.sha256(path.read_bytes()).hexdigest())
        self.assertEqual(result["canvas"], {
            "width": 12192000, "height": 6858000, "unit": "emu", "orientation": "landscape",
        })
        self.assertEqual(
            [(s["n"], s["id"], s["key"], s["relationshipId"], s["part"]) for s in result["slides"]],
            [
                (1, "260", "s260", "rIdSecond", "ppt/slides/slide10.xml"),
                (2, "257", "s257", "rIdFirst", "ppt/slides/slide2.xml"),
            ],
        )

    def test_text_shape_preserves_paragraphs_unicode_offsets_and_run_styles(self):
        result = pr.read(mp.build_sample(self.root / "sample.pptx"))
        element = result["slides"][0]["elements"][0]

        self.assertEqual((element["id"], element["key"], element["name"], element["kind"]),
                         ("2", "e2", "合成标题", "shape"))
        self.assertEqual((element["z"], element["parent"]), (0, None))
        self.assertEqual(element["geometry"]["source"], {
            "x": 914400, "y": 914400, "cx": 5486400, "cy": 914400, "unit": "emu",
        })
        self.assertAlmostEqual(element["geometry"]["normalized"]["x"], 0.075)
        self.assertAlmostEqual(element["geometry"]["normalized"]["width"], 0.45)
        self.assertEqual(element["text"], "相同😀&<\n相同")
        self.assertEqual(element["textDigest"], hashlib.sha256(element["text"].encode()).hexdigest())
        first, second = element["paragraphs"]
        self.assertEqual((first["index"], first["text"]), (0, "相同😀&<"))
        self.assertEqual(first["digest"], hashlib.sha256("相同😀&<".encode()).hexdigest())
        self.assertEqual([(r["start"], r["end"], r["text"]) for r in first["runs"]], [(0, 2, "相同"), (2, 5, "😀&<")])
        self.assertEqual(first["runs"][0]["style"]["bold"], True)
        self.assertEqual(first["runs"][0]["style"]["typeface"], "Synthetic Sans")
        self.assertEqual(first["runs"][1]["style"]["italic"], True)
        self.assertNotEqual(first["runs"][0]["styleDigest"], first["runs"][1]["styleDigest"])
        self.assertEqual((second["index"], second["text"]), (1, "相同"))
        self.assertEqual(element["capabilities"], {
            "display": True, "select": True, "locate": True,
            "textWrite": True, "geometryWrite": True, "styleWrite": True,
        })

    def test_line_break_counts_in_paragraph_text_and_unicode_offsets(self):
        parts = mp.sample_parts()
        parts["ppt/slides/slide10.xml"] = parts["ppt/slides/slide10.xml"].replace(
            "</a:r><a:r><a:rPr i=", "</a:r><a:br/><a:r><a:rPr i=", 1)
        result = pr.read(mp.write_package(self.root / "line-break.pptx", parts))
        paragraph = result["slides"][0]["elements"][0]["paragraphs"][0]
        self.assertEqual(paragraph["text"], "相同\n😀&<")
        self.assertEqual([(r["start"], r["end"], r["text"]) for r in paragraph["runs"]],
                         [(0, 2, "相同"), (2, 3, "\n"), (3, 6, "😀&<")])

    def test_portrait_canvas_and_reproducible_fixture(self):
        first = mp.build_sample(self.root / "first.pptx")
        second = mp.build_sample(self.root / "second.pptx")
        self.assertEqual(first.read_bytes(), second.read_bytes())

        portrait = mp.build_sample(self.root / "portrait.pptx", portrait=True)
        result = pr.read(portrait)
        self.assertEqual(result["canvas"], {
            "width": 6858000, "height": 12192000, "unit": "emu", "orientation": "portrait",
        })
        self.assertEqual(result["fingerprint"], hashlib.sha256(portrait.read_bytes()).hexdigest())
        self.assertFalse((self.root / "ppt").exists())

    def test_complex_elements_expose_geometry_text_relationships_and_read_only_capabilities(self):
        result = pr.read(mp.build_sample(self.root / "sample.pptx"))
        elements = result["slides"][1]["elements"]
        self.assertEqual([e["kind"] for e in elements], [
            "table", "picture", "group", "shape", "chart", "smartArt", "media", "ole", "unknown",
        ])
        by_name = {e["name"]: e for e in elements}

        table = by_name["合成表格"]
        cell = table["cells"][0]
        self.assertEqual((cell["row"], cell["column"], cell["text"]), (0, 0, "合成单元格"))
        self.assertEqual(cell["paragraphs"][0]["runs"][0]["end"], 5)
        self.assertEqual([(c["row"], c["column"], c["text"]) for c in table["cells"]],
                         [(0, 0, "合成单元格"), (0, 1, "第二列"), (1, 0, "第二行")])
        self.assertEqual(by_name["合成图片"]["description"], "合成图片替代文本")
        child = by_name["组合内文本"]
        self.assertEqual((child["parent"], child["depth"]), ("e12", 1))
        self.assertAlmostEqual(child["geometry"]["normalized"]["x"], 0.35)
        self.assertAlmostEqual(child["geometry"]["normalized"]["y"], 0.35)
        self.assertAlmostEqual(child["geometry"]["normalized"]["width"], 0.25)
        self.assertAlmostEqual(child["geometry"]["normalized"]["height"], 0.25)
        self.assertEqual(by_name["合成图表"]["references"][0]["target"], "ppt/charts/chart1.xml")
        self.assertEqual(by_name["合成流程图"]["references"][0]["target"], "ppt/diagrams/data1.xml")
        self.assertEqual(by_name["合成媒体"]["references"][0]["target"], "ppt/media/synthetic.mp4")
        self.assertEqual(by_name["未知扩展"]["text"], "未知合成文本")

        for name in ("合成表格", "合成图片", "合成组合", "组合内文本", "合成图表", "合成流程图", "合成媒体", "合成OLE", "未知扩展"):
            with self.subTest(name=name):
                capabilities = by_name[name]["capabilities"]
                self.assertTrue(all(capabilities[key] for key in ("display", "select", "locate")))
                self.assertFalse(any(capabilities[key] for key in ("textWrite", "geometryWrite", "styleWrite")))
        self.assertIn({"code": "unknownStructure", "part": "ppt/slides/slide2.xml",
                       "slide": "s257", "element": "e18", "detail": "urn:synthetic:unknown"},
                      result["diagnostics"])

    def test_unknown_uri_containing_ole_substring_is_not_misread_as_ole(self):
        parts = mp.sample_parts()
        parts["ppt/slides/slide2.xml"] = parts["ppt/slides/slide2.xml"].replace(
            'uri="urn:synthetic:unknown"', 'uri="urn:synthetic:console-like"')
        result = pr.read(mp.write_package(self.root / "ole-substring.pptx", parts))

        kinds = {e["name"]: e["kind"] for e in result["slides"][1]["elements"]}
        self.assertEqual(kinds["未知扩展"], "unknown")
        self.assertIn({"code": "unknownStructure", "part": "ppt/slides/slide2.xml",
                       "slide": "s257", "element": "e18", "detail": "urn:synthetic:console-like"},
                      result["diagnostics"])

    def test_notes_available_missing_and_damaged_are_distinct(self):
        result = pr.read(mp.build_sample(self.root / "sample.pptx"))
        notes = result["slides"][0]["notes"]
        self.assertEqual(notes["status"], "available")
        self.assertEqual((notes["relationshipId"], notes["part"]),
                         ("rIdNotes", "ppt/notesSlides/notesSlide1.xml"))
        self.assertEqual(notes["text"], "合成备注😀\n第二段")
        self.assertEqual(notes["digest"], hashlib.sha256(notes["text"].encode()).hexdigest())
        self.assertEqual(notes["paragraphs"][0]["runs"][0]["end"], 5)
        self.assertEqual(notes["capabilities"], {
            "display": True, "select": True, "locate": True,
            "textWrite": True, "geometryWrite": False, "styleWrite": False,
        })
        self.assertIsNone(result["slides"][1]["notes"])

        parts = mp.sample_parts()
        parts["ppt/notesSlides/notesSlide1.xml"] = "<p:notes>"
        broken = pr.read(mp.write_package(self.root / "broken-notes.pptx", parts))
        self.assertEqual(broken["slides"][0]["notes"]["status"], "error")
        self.assertIn({"code": "badXml", "part": "ppt/notesSlides/notesSlide1.xml",
                       "slide": "s260", "element": "", "detail": broken["slides"][0]["notes"]["detail"]},
                      broken["diagnostics"])
        self.assertIsNone(broken["slides"][1]["notes"])

    def test_duplicate_shape_ids_have_unique_keys_but_ambiguous_write_and_locate_capabilities(self):
        parts = mp.sample_parts()
        slide = parts["ppt/slides/slide10.xml"]
        start = slide.index("<p:sp>")
        end = slide.index("</p:sp>", start) + len("</p:sp>")
        duplicate = slide[start:end].replace('name="合成标题"', 'name="重复形状"').replace(
            '<a:off x="914400" y="914400"/>', '<a:off x="2000000" y="914400"/>')
        parts["ppt/slides/slide10.xml"] = slide[:end] + duplicate + slide[end:]
        path = mp.write_package(self.root / "duplicate-shapes.pptx", parts)

        result = pr.read(path)
        first, second = result["slides"][0]["elements"]
        self.assertEqual(first["key"], "e2")
        self.assertTrue(second["key"].startswith("e2~"))
        self.assertNotEqual(first["evidenceDigest"], second["evidenceDigest"])
        self.assertEqual([e["key"] for e in result["slides"][0]["elements"]],
                         [e["key"] for e in pr.read(path)["slides"][0]["elements"]])
        for element in (first, second):
            self.assertTrue(element["capabilities"]["select"])
            self.assertFalse(element["capabilities"]["locate"])
            self.assertFalse(any(element["capabilities"][key] for key in ("textWrite", "geometryWrite", "styleWrite")))
        self.assertIn({"code": "duplicateShapeId", "part": "ppt/slides/slide10.xml",
                       "slide": "s260", "element": second["key"], "detail": "2"}, result["diagnostics"])


class PptxSecurityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="ae-pptx-security-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        # 读取层承诺不解压到磁盘：观察进程工作目录的顶层条目在读取前后不变。
        self.cwd_entries = {entry.name for entry in Path.cwd().iterdir()}

    def assert_error(self, path: Path, code: str, part: str | None = None):
        before = path.read_bytes()
        with self.assertRaises(pr.PptxError) as caught:
            pr.read(path)
        self.assertEqual(caught.exception.code, code)
        if part is not None:
            self.assertEqual(caught.exception.part, part)
        self.assertEqual(path.read_bytes(), before)
        self.assertEqual({entry.name for entry in Path.cwd().iterdir()}, self.cwd_entries)
        self.assertFalse((self.root / "ppt").exists())

    def test_non_zip_missing_core_bad_xml_and_dtd_have_controlled_errors(self):
        junk = self.root / "junk.pptx"
        junk.write_bytes(b"not a zip")
        self.assert_error(junk, "notZip")

        for name in ("[Content_Types].xml", "ppt/presentation.xml", "ppt/slides/slide10.xml"):
            with self.subTest(missing=name):
                parts = mp.sample_parts()
                del parts[name]
                self.assert_error(mp.write_package(self.root / ("missing-" + name.replace("/", "-")), parts),
                                  "missingPart", name)

        parts = mp.sample_parts()
        parts["ppt/slides/slide10.xml"] = "<p:sld>"
        self.assert_error(mp.write_package(self.root / "bad-xml.pptx", parts), "badXml", "ppt/slides/slide10.xml")

        parts = mp.sample_parts()
        parts["ppt/presentation.xml"] = ('<?xml version="1.0"?><!DOCTYPE presentation [<!ENTITY a "boom">]>'
                                         f'<p:presentation xmlns:p="{mp.P}"><p:sldSz cx="1" cy="1"/>&a;</p:presentation>')
        self.assert_error(mp.write_package(self.root / "dtd.pptx", parts), "dtd", "ppt/presentation.xml")

    def test_unsafe_and_duplicate_member_names_are_rejected_before_xml_reads(self):
        import warnings
        import zipfile

        for index, name in enumerate(("../escape.xml", "/absolute.xml", "..\\escape.xml",
                                      "ppt/../../escape.xml", "C:/escape.xml")):
            with self.subTest(path=name):
                parts = mp.sample_parts()
                parts[name] = "<unused/>"
                self.assert_error(mp.write_package(self.root / f"unsafe-{index}.pptx", parts), "unsafePath", name)

        duplicate = self.root / "duplicate.pptx"
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", UserWarning)
            with zipfile.ZipFile(duplicate, "w") as archive:
                for name, content in mp.sample_parts().items():
                    archive.writestr(name, content)
                archive.writestr("ppt/slides/slide10.xml", "<p:sld/>")
        self.assert_error(duplicate, "duplicateMember", "ppt/slides/slide10.xml")

    def test_oversized_xml_members_and_compression_ratio_are_refused(self):
        parts = mp.sample_parts()
        parts["ppt/slides/slide10.xml"] = b"<" + b" " * (8 * 1024 * 1024 + 1) + b"/>"
        self.assert_error(mp.write_package(self.root / "large-xml.pptx", parts),
                          "tooLarge", "ppt/slides/slide10.xml")

        parts = mp.sample_parts()
        parts["ppt/media/oversize.bin"] = b"\0" * (64 * 1024 * 1024 + 1)
        self.assert_error(mp.write_package(self.root / "large-member.pptx", parts),
                          "tooLarge", "ppt/media/oversize.bin")

        parts = mp.sample_parts()
        parts["ppt/media/compression-bomb.bin"] = b"\0" * (4 * 1024 * 1024)
        self.assert_error(mp.write_package(self.root / "compression-ratio.pptx", parts),
                          "compressionRatio", "ppt/media/compression-bomb.bin")

    def test_non_slide_root_and_missing_sptree_get_controlled_errors(self):
        parts = mp.sample_parts()
        parts["ppt/slides/slide10.xml"] = f'<p:notes xmlns:p="{mp.P}"/>'
        self.assert_error(mp.write_package(self.root / "not-a-slide.pptx", parts),
                          "invalidPackage", "ppt/slides/slide10.xml")

        parts = mp.sample_parts()
        parts["ppt/slides/slide10.xml"] = f'<p:sld xmlns:p="{mp.P}" xmlns:a="{mp.A}"><p:cSld/></p:sld>'
        self.assert_error(mp.write_package(self.root / "no-sptree.pptx", parts),
                          "invalidPackage", "ppt/slides/slide10.xml")

    def test_corrupt_streams_and_overlapping_member_data_are_refused(self):
        import struct
        import zipfile

        source = mp.build_sample(self.root / "source.pptx")
        with zipfile.ZipFile(source) as archive:
            info = archive.getinfo("ppt/presentation.xml")
            header_offset = info.header_offset
            compressed_size = info.compress_size
        raw = bytearray(source.read_bytes())
        name_length, extra_length = struct.unpack_from("<HH", raw, header_offset + 26)
        data_offset = header_offset + 30 + name_length + extra_length
        raw[data_offset + compressed_size // 2] ^= 0xFF
        corrupted = self.root / "corrupt-stream.pptx"
        corrupted.write_bytes(raw)
        self.assert_error(corrupted, "notZip", "ppt/presentation.xml")

        with zipfile.ZipFile(source) as archive:
            shared_offset = archive.getinfo("_rels/.rels").header_offset
        raw = bytearray(source.read_bytes())
        position = raw.find(b"PK\x01\x02")
        patched = False
        while position >= 0:
            name_length, extra_length, comment_length = struct.unpack_from("<HHH", raw, position + 28)
            name = bytes(raw[position + 46:position + 46 + name_length]).decode("utf-8")
            if name == "ppt/presentation.xml":
                struct.pack_into("<I", raw, position + 42, shared_offset)
                patched = True
                break
            position = raw.find(b"PK\x01\x02", position + 46 + name_length + extra_length + comment_length)
        self.assertTrue(patched)
        overlap = self.root / "overlap.pptx"
        overlap.write_bytes(raw)
        self.assert_error(overlap, "badCompression", "ppt/presentation.xml")


if __name__ == "__main__":
    unittest.main()
