"""#151：用合成 PPTX 从公开回写入口核验字节保留、幻灯片定位、防护及失败后的原件。"""
from __future__ import annotations

import hashlib
import io
import json
import os
import stat
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
import notes as notes_cli
import notes_store as store
import pptx_edit as pe
import pptx_reader as pr


def member(raw, name):
    with zipfile.ZipFile(io.BytesIO(raw)) as zf:
        return zf.read(name)


def local_records(raw):
    """独立读取各成员的 local header + 压缩流 + descriptor，比较未改成员原字节。"""
    with zipfile.ZipFile(io.BytesIO(raw)) as zf:
        infos = sorted(zf.infolist(), key=lambda i: i.header_offset)
        return {i.filename: raw[i.header_offset:infos[n + 1].header_offset if n + 1 < len(infos) else zf.start_dir]
                for n, i in enumerate(infos)}


class PptxEditTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="ae-pptx-edit-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.review = self.root / "review"
        self.review.mkdir()
        self.source = self.root / "synthetic.pptx"
        self.ids = []

    # ---------- 夹具 ----------
    def build(self, parts=None, script=None, pages=True):
        parts = parts if parts is not None else mp.sample_parts()
        mp.write_package(self.source, parts)
        self.original = self.source.read_bytes()
        self.fp = hashlib.sha256(self.original).hexdigest()
        self.manifest = {"format": {"report": "pptx", "version": 1},
                         "sources": {"report": "../synthetic.pptx"},
                         "pptx": {"fingerprint": self.fp}}
        if script:
            self.manifest["sources"]["script"] = script
        if pages:
            model = pr.read(self.source)
            self.manifest["pages"] = [{"n": s["n"], "key": s["key"], "title": "", "stable": True}
                                      for s in model["slides"]]
        (self.review / "review.json").write_text(json.dumps(self.manifest), encoding="utf-8")
        return self.source

    def refresh_source(self, parts=None):
        """重写源文件并同步指纹：锚点不带 fp（fp 是可选项）时不影响预检次序。"""
        parts = parts if parts is not None else mp.sample_parts()
        mp.write_package(self.source, parts)
        self.original = self.source.read_bytes()
        self.fp = hashlib.sha256(self.original).hexdigest()
        self.manifest["pptx"]["fingerprint"] = self.fp
        (self.review / "review.json").write_text(json.dumps(self.manifest), encoding="utf-8")

    def slide_key(self, n=1):
        return pr.read(self.source)["slides"][n - 1]["key"]

    def shape_anchor(self, slide_n=1, elem_id="2", para=0, quote=None, start=None, end=None, **over):
        model = pr.read(self.source)
        slide = model["slides"][slide_n - 1]
        el = next(e for e in slide["elements"] if str(e["id"]) == str(elem_id))
        target = {"key": el["key"], "id": str(el["id"] or ""), "name": el.get("name", ""),
                  "kind": el["kind"], "z": el.get("z", 0), "digest": el["evidenceDigest"]}
        ro = [k for k in ("text", "geometry", "style") if not el["capabilities"][k + "Write"]]
        if ro:
            target["ro"] = ro
        p = el["paragraphs"][para]
        text = {"para": para, "paraDigest": p["digest"],
                "quote": p["text"] if quote is None else quote}
        if start is not None:
            text["start"], text["end"] = start, end
            text["runs"] = [{"start": r["start"], "end": r["end"]}
                            for r in p["runs"] if r["start"] < end and r["end"] > start]
        a = {"v": 1, "slide": slide["key"], "kind": "text", "via": "selection", "fp": self.fp,
             "elements": [target], "text": text}
        a.update(over)
        return a

    def shape_note(self, before, after="新", *, slide_n=1, elem_id="2", para=0,
                   start=None, end=None, quote=None, anchor_over=None):
        a = self.shape_anchor(slide_n=slide_n, elem_id=elem_id, para=para, quote=quote,
                              start=start, end=end, **(anchor_over or {}))
        _doc, note = store.add(self.review, {
            "page": slide_n, "target": "报告", "quoteFrom": "报告", "quote": before,
            "text": ("改为：" + after) if after else "删除引用的这段文字", "pptx": a,
            "edit": {"op": "replace", "from": before, "to": after} if after else {"op": "delete", "from": before}})
        self.ids.append(note["id"])
        return note["id"]

    def notes_note(self, before, after="新讲法", *, page=1, key=True, target="讲稿", quote_from="讲稿"):
        data = {"page": page, "target": target, "quoteFrom": quote_from, "quote": before,
                "text": "改为：" + after if after else "删除引用的这段文字",
                "edit": {"op": "replace", "from": before, "to": after} if after else {"op": "delete", "from": before}}
        if key:
            data["key"] = self.slide_key(page)
        _doc, note = store.add(self.review, data)
        self.ids.append(note["id"])
        return note["id"]

    def assert_refused(self, code, action=None):
        with self.assertRaises(pe.EditError) as caught:
            (action or self.writeback)()
        errors = caught.exception.errors if isinstance(caught.exception, pe.SelectionError) else [caught.exception]
        self.assertIn(code, [e.code for e in errors])
        self.assertEqual(self.source.read_bytes(), self.original)
        return errors

    def writeback(self, **kwargs):
        kwargs.setdefault("ids", list(self.ids))
        return pe.writeback(self.review, **kwargs)

    # ---------- 落实修改 ----------
    def test_batch_replacement_deletion_and_notes_update_preserve_untouched_bytes(self):
        self.build()
        one = self.shape_note("相同", "第二段新文", para=1)
        two = self.shape_note("相同", "", para=0, start=0, end=2)
        three = self.notes_note("第二段", "讲稿改后")
        _doc, batch, _merged = store.handoff_submit(self.review)
        notes_before = (self.review / "notes.json").read_bytes()
        handoff_before = (self.review / "handoff.json").read_bytes()
        result = self.writeback(batch=batch["seq"])
        self.assertEqual(result["ids"], [one, two, three])
        self.assertEqual(result["pages"], [1])
        self.assertEqual([(c["kind"], c["part"], c["para"]) for c in result["changes"]],
                         [("shape", "ppt/slides/slide10.xml", 1),
                          ("shape", "ppt/slides/slide10.xml", 0),
                          ("notes", "ppt/notesSlides/notesSlide1.xml", 1)])
        self.assertEqual(result["skipped"], [])
        xml = member(self.source.read_bytes(), "ppt/slides/slide10.xml")
        self.assertIn('<a:rPr i="1" sz="1800"/><a:t xml:space="preserve">😀&amp;&lt;</a:t>'.encode(),
                      xml, "未触及 run 的文字与属性原样保留")
        self.assertIn('<a:t xml:space="preserve">第二段新文</a:t>'.encode(), xml)
        self.assertIn(b'<a:t xml:space="preserve"></a:t>', xml)
        self.assertEqual(xml.count(b"<a:rPr"), 3, "未触及 run 的属性全部保留")
        notes_xml = member(self.source.read_bytes(), "ppt/notesSlides/notesSlide1.xml")
        self.assertIn("讲稿改后".encode(), notes_xml)
        self.assertIn("合成备注😀".encode(), notes_xml, "备注未触及段落保留")
        self.assertEqual((self.review / "notes.json").read_bytes(), notes_before, "回写不改批注状态")
        self.assertEqual((self.review / "handoff.json").read_bytes(), handoff_before)
        before, after = local_records(self.original), local_records(self.source.read_bytes())
        self.assertEqual(sorted(before), sorted(after))
        for name in before:
            if name not in ("ppt/slides/slide10.xml", "ppt/notesSlides/notesSlide1.xml"):
                self.assertEqual(after[name], before[name], name)
        model = pr.read(self.source)
        el = model["slides"][0]["elements"][0]
        self.assertEqual([p["text"] for p in el["paragraphs"]], ["😀&<", "第二段新文"])
        notes_model = model["slides"][0]["notes"]
        self.assertEqual([p["text"] for p in notes_model["paragraphs"]], ["合成备注😀", "讲稿改后"])
        backup = Path(result["backup"])
        self.assertEqual(backup.read_bytes(), self.original)
        self.assertEqual(stat.S_IMODE(backup.stat().st_mode), 0o600)
        self.assertIn("batch-1/pptx-writeback", str(backup))
        # 快照目录逐级建并锁 0700：.snapshots、batch-N 与 writeback 层都要断言
        self.assertEqual(backup.parent.name, "pptx-writeback")
        self.assertEqual(backup.parent.parent.name, "batch-1")
        self.assertEqual(backup.parent.parent.parent.name, ".snapshots")
        for directory in (backup.parent.parent.parent, backup.parent.parent, backup.parent):
            self.assertEqual(stat.S_IMODE(directory.stat().st_mode), 0o700, str(directory))

    def test_cross_run_replacement_keeps_run_properties(self):
        self.build()
        self.writeback(ids=[self.shape_note("相同😀", "新", para=0, start=0, end=3)])
        model = pr.read(self.source)
        runs = model["slides"][0]["elements"][0]["paragraphs"][0]["runs"]
        self.assertEqual([r["text"] for r in runs], ["新", "&<"])
        self.assertTrue(runs[0]["style"]["bold"], "替换文本沿用起始 run 的加粗")
        self.assertTrue(runs[1]["style"]["italic"], "未触及文本保留原有斜体")
        xml = member(self.source.read_bytes(), "ppt/slides/slide10.xml")
        self.assertIn(b'<a:rPr b="1"><a:latin typeface="Synthetic Sans"/></a:rPr>'
                      b'<a:t xml:space="preserve">\xe6\x96\xb0</a:t>', xml)
        self.assertIn(b'<a:rPr i="1" sz="1800"/>', xml)

    def test_repeated_text_requires_offsets_and_uses_them(self):
        self.build()
        nid = self.shape_note("相同", "新", para=0, start=0, end=2)
        self.writeback(ids=[nid])
        model = pr.read(self.source)
        texts = [p["text"] for p in model["slides"][0]["elements"][0]["paragraphs"]]
        self.assertEqual(texts, ["新😀&<", "相同"], "重复文字按偏移落在正确段落")

    def test_repeated_text_without_offsets_is_refused(self):
        parts = mp.sample_parts()
        parts["ppt/slides/slide10.xml"] = mp.slide_xml(
            paragraphs='<a:p><a:r><a:rPr lang="zh-CN"/><a:t xml:space="preserve">相同相同</a:t></a:r></a:p>')
        self.build(parts)
        self.shape_note("相同", "新", para=0)
        self.assert_refused("ambiguous")

    def test_shape_text_across_slides_and_disjoint_edits(self):
        self.build()
        one = self.shape_note("相同", "新", para=1)
        two = self.notes_note("合成备注😀", "")
        result = self.writeback(ids=[one, two])
        self.assertEqual(result["pages"], [1])
        model = pr.read(self.source)
        self.assertEqual(model["slides"][0]["elements"][0]["paragraphs"][1]["text"], "新")
        self.assertEqual(model["slides"][0]["notes"]["paragraphs"][0]["text"], "")

    def test_whitespace_fallback_matches_folded_quote(self):
        self.build()
        self.writeback(ids=[self.shape_note("相同 ", "新", para=1)])
        model = pr.read(self.source)
        self.assertEqual(model["slides"][0]["elements"][0]["paragraphs"][1]["text"], "新")

    def test_unlocatable_text_reference_is_refused(self):
        self.build()
        gone = {"text": {"para": 1, "paraDigest": "0" * 64, "quote": "没有这句话"}}
        self.assert_refused("anchor", lambda: self.writeback(
            ids=[self.shape_note("没有这句话", "新", para=1, anchor_over=gone)]))
        multi = {"text": {"para": 1, "paraDigest": "0" * 64, "quote": "相同"}}
        self.assert_refused("ambiguous", lambda: self.writeback(
            ids=[self.shape_note("相同", "新", para=1, anchor_over=multi)]))

    def test_overlapping_edits_refuse_the_entire_selection(self):
        self.build()
        one = self.shape_note("相同", "甲", para=0, start=0, end=2)
        two = self.shape_note("同😀", "乙", para=0, start=1, end=3)
        errors = self.assert_refused("overlap", lambda: self.writeback(ids=[one, two]))
        self.assertEqual(errors[0].item, two)

    def test_range_covering_line_break_is_refused(self):
        parts = mp.sample_parts()
        parts["ppt/slides/slide10.xml"] = mp.slide_xml(
            paragraphs='<a:p><a:r><a:rPr lang="zh-CN"/><a:t xml:space="preserve">甲</a:t></a:r>'
                       "<a:br/><a:r><a:rPr lang=\"zh-CN\"/><a:t xml:space=\"preserve\">乙</a:t></a:r></a:p>")
        self.build(parts)
        self.assert_refused("range", lambda: self.writeback(
            ids=[self.shape_note("甲\n乙", "新", quote="甲\n乙")]))

    def test_readonly_structures_are_refused(self):
        self.build()
        for elem_id, kind in (("18", "unknown"), ("13", "shape")):
            with self.subTest(elem_id=elem_id):
                errors = self.assert_refused("readonly", lambda: self.writeback(
                    ids=[self.shape_note("未知合成文本" if elem_id == "18" else "组合内文本", "新",
                                          slide_n=2, elem_id=elem_id, quote="未知合成文本" if elem_id == "18" else "组合内文本")]))
                self.assertEqual(errors[0].detail, kind)

    def test_stale_or_future_anchor_is_refused(self):
        for over in ({"fp": "0" * 64}, {"changed": True}, {"v": 2}):
            with self.subTest(over=over):
                self.build()
                nid = self.shape_note("相同", "新", para=1, anchor_over=over)
                self.assert_refused("anchor", lambda: self.writeback(ids=[nid]))

    def test_fingerprint_mismatch_has_no_force_override(self):
        self.build()
        self.shape_note("相同", "新", para=1)
        self.source.write_bytes(self.original + b"changed")
        self.original = self.source.read_bytes()
        self.assert_refused("fingerprint")

    def test_powerpoint_owner_file_blocks_write_and_preview(self):
        self.build()
        self.shape_note("相同", "新", para=1)
        lock = self.source.with_name("~$" + self.source.name)
        lock.write_bytes(b"ppt")
        self.assert_refused("locked")
        self.assert_refused("locked", lambda: self.writeback(dry_run=True))

    # ---------- 讲稿侧（已有备注） ----------
    def test_notes_writeback_requires_no_external_script(self):
        self.build(script="讲稿.md")
        (self.review / "讲稿.md").write_text("### 第 1 页｜甲\n**讲法**\n外部讲稿\n", encoding="utf-8")
        self.assert_refused("notes", lambda: self.writeback(
            ids=[self.notes_note("第二段", "新讲法")]))

    def test_notes_writeback_requires_existing_notes_part(self):
        self.build()
        self.assert_refused("notes", lambda: self.writeback(
            ids=[self.notes_note("第二段", "新讲法", page=2)]))

    def test_ambiguous_notes_quote_is_refused(self):
        parts = mp.sample_parts()
        para = '<a:p><a:r><a:rPr lang="zh-CN"/><a:t xml:space="preserve">第二段</a:t></a:r></a:p>'
        parts["ppt/notesSlides/notesSlide1.xml"] = parts["ppt/notesSlides/notesSlide1.xml"].replace(
            para, para + para)
        self.build(parts)
        self.assert_refused("ambiguous", lambda: self.writeback(
            ids=[self.notes_note("第二段", "新讲法")]))

    def test_missing_notes_quote_is_refused(self):
        self.build()
        self.assert_refused("notFound", lambda: self.writeback(
            ids=[self.notes_note("没有这句话", "新")]))

    def test_notes_writeback_uses_page_key_fallback_without_note_key(self):
        self.build()
        nid = self.notes_note("第二段", "讲稿改后", key=False)
        self.writeback(ids=[nid])
        model = pr.read(self.source)
        self.assertEqual(model["slides"][0]["notes"]["paragraphs"][1]["text"], "讲稿改后")

    # ---------- 包与预检闸门 ----------
    def test_dtd_is_refused(self):
        self.build()
        self.shape_note("相同", "新", para=1, anchor_over={"fp": None})
        parts = mp.sample_parts()
        parts["ppt/slides/slide10.xml"] = parts["ppt/slides/slide10.xml"].replace(
            "<p:sld", '<!DOCTYPE p:sld [<!ENTITY x "乙">]><p:sld', 1)
        self.refresh_source(parts)
        errors = self.assert_refused("xml")
        self.assertIn("DTD", errors[0].detail)

    def test_invalid_replacement_characters_are_refused(self):
        for bad in ("a\x00b", "a\x01b", "\ufffe"):
            with self.subTest(bad=bad):
                self.build()
                nid = self.shape_note("相同", "新" + bad, para=1)
                self.assert_refused("characters", lambda: self.writeback(ids=[nid]))
        # 孤立代理字符会在 notes 存储层被就地净化；手改 notes.json 里的转义才能到达写入闸门
        self.build()
        nid = self.shape_note("相同", "新MARK", para=1)
        data = (self.review / "notes.json").read_text(encoding="utf-8")
        data = data.replace("新MARK", json.dumps("新a\ud800b")[1:-1])
        (self.review / "notes.json").write_text(data, encoding="utf-8")
        self.assert_refused("characters", lambda: self.writeback(ids=[nid]))

    def test_broken_and_duplicate_zip_members_are_refused(self):
        self.build()
        nid = self.shape_note("相同", "新", para=1, anchor_over={"fp": None})
        self.source.write_bytes(self.original[: len(self.original) // 2])
        self.original = self.source.read_bytes()
        self.manifest["pptx"]["fingerprint"] = hashlib.sha256(self.original).hexdigest()
        (self.review / "review.json").write_text(json.dumps(self.manifest), encoding="utf-8")
        self.assert_refused("package", lambda: self.writeback(ids=[nid]))
        self.refresh_source()
        with zipfile.ZipFile(self.source, "a") as zf:
            zf.writestr("ppt/slides/slide10.xml", "second member")
        self.original = self.source.read_bytes()
        self.manifest["pptx"]["fingerprint"] = hashlib.sha256(self.original).hexdigest()
        (self.review / "review.json").write_text(json.dumps(self.manifest), encoding="utf-8")
        self.assert_refused("package", lambda: self.writeback(ids=[nid]))

    def test_not_a_pptx_review_is_refused(self):
        self.build()
        self.manifest["format"] = {"report": "pptx", "version": 99}
        (self.review / "review.json").write_text(json.dumps(self.manifest), encoding="utf-8")
        self.assert_refused("review")
        self.build()
        self.manifest["format"] = {"report": "docx", "version": 1}
        (self.review / "review.json").write_text(json.dumps(self.manifest), encoding="utf-8")
        self.assert_refused("review")

    def test_batch_and_selection_errors_are_reported(self):
        self.build()
        _doc, batch, _merged = store.handoff_submit(self.review)
        self.assert_refused("batch", lambda: self.writeback(batch=batch["seq"] + 7))
        self.assert_refused("empty", lambda: self.writeback(batch=batch["seq"]))
        self.assert_refused("note", lambda: self.writeback(ids=["missing"]))

    # ---------- dry-run 与另存 ----------
    def test_dry_run_lists_changes_without_output_or_backup(self):
        self.build()
        output = self.root / "preview.pptx"
        result = self.writeback(ids=[self.shape_note("相同", "新", para=1),
                                     self.notes_note("第二段", "讲稿改后")],
                                output=output, dry_run=True)
        self.assertTrue(result["dryRun"])
        self.assertEqual(result["pages"], [1])
        self.assertIsNone(result["backup"])
        self.assertEqual(self.source.read_bytes(), self.original)
        self.assertFalse(output.exists())
        self.assertFalse((self.review / ".snapshots").exists())

    def test_output_keeps_source_and_existing_output_is_not_overwritten(self):
        self.build()
        output = self.root / "copy.pptx"
        self.writeback(ids=[self.shape_note("相同", "新", para=1)], output=output)
        self.assertEqual(self.source.read_bytes(), self.original)
        self.assertIn("新".encode(), member(output.read_bytes(), "ppt/slides/slide10.xml"))
        self.assert_refused("output", lambda: self.writeback(
            ids=[self.shape_note("相同", "新", para=1)], output=output))
        link = self.root / "link.pptx"
        link.symlink_to(output)
        self.assert_refused("output", lambda: self.writeback(
            ids=[self.shape_note("相同", "新", para=1)], output=link))

    def test_zip_comments_extra_fields_and_attributes_are_preserved(self):
        self.build()
        with zipfile.ZipFile(io.BytesIO(self.original)) as old, zipfile.ZipFile(self.source, "w") as out:
            out.comment = b"synthetic ZIP comment"
            for info in old.infolist():
                info.extra = b"\xfe\xca\x03\x00xyz"
                info.comment = b"synthetic member"
                info.external_attr = 0o600 << 16
                out.writestr(info, old.read(info.filename))
        self.original = self.source.read_bytes()
        self.fp = hashlib.sha256(self.original).hexdigest()
        self.manifest["pptx"]["fingerprint"] = self.fp
        (self.review / "review.json").write_text(json.dumps(self.manifest), encoding="utf-8")
        self.writeback(ids=[self.shape_note("相同", "新", para=1)])
        new_raw = self.source.read_bytes()
        before, after = local_records(self.original), local_records(new_raw)
        for name in before:
            if name != "ppt/slides/slide10.xml":
                self.assertEqual(before[name], after[name])
        with zipfile.ZipFile(io.BytesIO(self.original)) as old, zipfile.ZipFile(io.BytesIO(new_raw)) as new:
            self.assertEqual(old.comment, new.comment)
            for name in old.namelist():
                a, b = old.getinfo(name), new.getinfo(name)
                self.assertEqual((a.extra, a.comment, a.external_attr, a.date_time),
                                 (b.extra, b.comment, b.external_attr, b.date_time))

    def test_data_descriptor_members_survive_writeback_byte_identical(self):
        # 第三方工具（Keynote / LibreOffice）导出的包可能带 data descriptor（flag 0x08）。
        # repack 按「成员头到下一成员头」切片，descriptor 应随未触及成员原样保留；
        # 此前所有回写测试的源包都不含 descriptor，字节保留结论在该输入上未证实过。
        self.build()
        sink = io.BytesIO()
        with zipfile.ZipFile(sink, "w") as archive:
            archive._seekable = False   # 模拟流式目标：zipfile 转入 descriptor 写法
            with zipfile.ZipFile(io.BytesIO(self.original)) as old:
                for info in old.infolist():
                    archive.writestr(info, old.read(info.filename))
        descriptor_raw = sink.getvalue()
        with zipfile.ZipFile(io.BytesIO(descriptor_raw)) as check:
            self.assertTrue(all(info.flag_bits & 0x8 for info in check.infolist()),
                            "夹具必须真的构造出 data descriptor")
        self.assertGreater(descriptor_raw.count(b"PK\x07\x08"), 0)
        self.source.write_bytes(descriptor_raw)
        self.original = descriptor_raw
        self.fp = hashlib.sha256(descriptor_raw).hexdigest()
        self.manifest["pptx"]["fingerprint"] = self.fp
        (self.review / "review.json").write_text(json.dumps(self.manifest), encoding="utf-8")

        self.writeback(ids=[self.shape_note("相同", "新", para=1)])
        before, after = local_records(self.original), local_records(self.source.read_bytes())
        for name in before:
            if name != "ppt/slides/slide10.xml":
                self.assertEqual(before[name], after[name])

    # ---------- 失败回滚 ----------
    def test_fsync_or_replace_failure_leaves_original_and_usable_backup(self):
        for method in ("fsync", "replace"):
            with self.subTest(method=method):
                self.build()
                nid = self.shape_note("相同", "新", para=1)
                if method == "fsync":
                    patcher = patch.object(pe.os, method, side_effect=[None, OSError("disk full")])
                else:
                    patcher = patch.object(pe.os, method, side_effect=OSError("rename denied"))
                with patcher, self.assertRaises(OSError):
                    self.writeback(ids=[nid])
                self.assertEqual(self.source.read_bytes(), self.original)
                backups = list((self.review / ".snapshots").rglob("*.pptx"))
                self.assertTrue(backups)
                self.assertTrue(all(p.read_bytes() == self.original for p in backups))
                self.assertFalse(list(self.root.glob("*.tmp")))

    def test_postwrite_corruption_does_not_replace_original(self):
        self.build()
        nid = self.shape_note("相同", "新", para=1)
        real_fsync = os.fsync
        count = 0

        def corrupt_pending(fd):
            nonlocal count
            real_fsync(fd)
            count += 1
            if count == 2:
                os.lseek(fd, 0, os.SEEK_SET)
                os.write(fd, b"broken zip")

        with patch.object(pe.os, "fsync", side_effect=corrupt_pending), \
                self.assertRaises((pe.EditError, zipfile.BadZipFile)):
            self.writeback(ids=[nid])
        self.assertEqual(self.source.read_bytes(), self.original)
        self.assertEqual(next((self.review / ".snapshots").rglob("*.pptx")).read_bytes(), self.original)

    def test_source_changed_during_write_is_not_overwritten(self):
        self.build()
        nid = self.shape_note("相同", "新", para=1)
        real_fsync = os.fsync
        count = 0

        def change_source(fd):
            nonlocal count
            real_fsync(fd)
            count += 1
            if count == 2:
                self.source.write_bytes(self.original + b"external change")

        with patch.object(pe.os, "fsync", side_effect=change_source), \
                self.assertRaises(pe.EditError) as caught:
            self.writeback(ids=[nid])
            self.assertEqual(caught.exception.code, "fingerprint")
            self.assertEqual(self.source.read_bytes(), self.original + b"external change")
            self.assertEqual(next((self.review / ".snapshots").rglob("*.pptx")).read_bytes(), self.original)

    def test_tampered_repack_output_is_rejected_by_postwrite_validation(self):
        # 写后校验此前只见过「正常通过」的对称输入：repack 输出里未触及成员的目录 CRC
        # 被改动时必须整组拒绝、不留任何写入——防线的拒绝分支首次被反向触发。
        import struct

        self.build()
        nid = self.shape_note("相同", "新", para=1)
        real_repack = pe.de.repack

        def tamper_crc(raw, main, document):
            out = bytearray(real_repack(raw, main, document))
            position = out.find(b"PK\x01\x02")
            while position >= 0:
                name_length, extra_length, comment_length = struct.unpack_from("<HHH", out, position + 28)
                if bytes(out[position + 46:position + 46 + name_length]).decode("utf-8") == "ppt/presentation.xml":
                    struct.pack_into("<I", out, position + 16, 0xDEADBEEF)   # 未触及成员的目录 CRC
                    break
                position = out.find(b"PK\x01\x02", position + 46 + name_length + extra_length + comment_length)
            return bytes(out)

        with patch.object(pe.de, "repack", side_effect=tamper_crc), \
                self.assertRaises(pe.EditError) as caught:
            self.writeback(ids=[nid])
        self.assertEqual(caught.exception.code, "validation")
        self.assertEqual(caught.exception.detail, "ppt/presentation.xml")
        self.assertEqual(self.source.read_bytes(), self.original)
        self.assertEqual(next((self.review / ".snapshots").rglob("*.pptx")).read_bytes(), self.original)
        self.assertFalse(list(self.root.glob("*.tmp")))

    # ---------- 改后闭环 ----------
    def test_binary_snapshot_and_page_diff_work_after_writeback(self):
        self.build()
        notes_cli.snapshot(self.review, {"seq": 1, "target": "review"}, "base")
        self.writeback(ids=[self.shape_note("相同", "新", para=1)])
        notes_cli.snapshot(self.review, {"seq": 1, "target": "review"}, "draft")
        snap = self.review / ".snapshots/batch-1/base/report"
        self.assertEqual(snap.read_bytes(), self.original)
        out = io.StringIO()
        with redirect_stdout(out):
            result = notes_cli.diff_cmd(self.review, 1, "draft", None, "zh-CN")
        self.assertEqual(result, 0)
        self.assertIn("相同", out.getvalue())
        self.assertIn("新", out.getvalue())

    # ---------- CLI 契约 ----------
    def test_cli_locales_dry_run_and_json(self):
        self.build()
        nid = self.shape_note("相同", "新", para=1)
        for lang, phrase in (("zh-CN", "预演通过"), ("en", "Preview passed")):
            out, err = io.StringIO(), io.StringIO()
            with redirect_stdout(out), redirect_stderr(err):
                code = pe.main(["apply", str(self.review), "--id", nid, "--dry-run", "--json", "--locale", lang])
            self.assertEqual(code, 0, err.getvalue())
            self.assertTrue(json.loads(out.getvalue())["dryRun"])
            out, err = io.StringIO(), io.StringIO()
            with redirect_stdout(out), redirect_stderr(err):
                code = pe.main(["apply", str(self.review), "--id", nid, "--dry-run", "--locale", lang])
            self.assertEqual(code, 0, err.getvalue())
            self.assertIn(phrase, out.getvalue())
        self.assertEqual(self.source.read_bytes(), self.original)

    def test_cli_argument_errors_use_the_shared_language_contract(self):
        self.build()
        cases = [(["--locale"], {}, "参数 --locale 需要一个值"),
                 (["--locale", "en", "apply", str(self.review), "--id"], {}, "Option --id needs a value"),
                 (["apply", str(self.review), "--id", "missing"], {"AE_LOCALE": "fr"}, "不支持的语言：fr"),
                 (["apply", str(self.review), "--unknown", "--locale", "en"], {}, "Unrecognized argument: --unknown"),
                 (["apply", str(self.review), "--batch", "1", "--id", "x"], {}, "apply"),
                 (["apply", str(self.review), "--batch", "0"], {}, "apply")]
        for args, env, expected in cases:
            with self.subTest(args=args), patch.dict(os.environ, {"AE_LOCALE": "", **env}):
                out, err = io.StringIO(), io.StringIO()
                with redirect_stdout(out), redirect_stderr(err):
                    try:
                        code = pe.main(args)
                    except SystemExit as exc:
                        code = exc.code
                self.assertEqual(code, 2)
                self.assertIn(expected, err.getvalue())
                self.assertNotIn("None", err.getvalue())
        self.assertEqual(self.source.read_bytes(), self.original)

    def test_invalid_stored_locale_reports_its_actual_value(self):
        self.build()
        self.manifest["locale"] = "fr"
        (self.review / "review.json").write_text(json.dumps(self.manifest))
        err = io.StringIO()
        with patch.dict(os.environ, {"AE_LOCALE": ""}), redirect_stderr(err):
            code = pe.main(["apply", str(self.review), "--id", "missing"])
        self.assertEqual(code, 2)
        self.assertIn("不支持的语言：fr", err.getvalue())


if __name__ == "__main__":
    unittest.main()
