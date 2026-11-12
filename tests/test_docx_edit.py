"""#137：用合成 DOCX 从公开回写入口核验字节保留、定位、防护及失败后的原件。"""
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
from xml.etree import ElementTree as ET

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "skill/annotation-everything/scripts"))
sys.path.insert(0, str(ROOT / "fixtures/docx"))
import docx_edit as de
import docx_review as dr
import make_sample as ms
import notes as notes_cli
import notes_store as store


def para(text, pid="0000000A"):
    return f'<w:p w14:paraId="{pid}"><w:r><w:t>{text}</w:t></w:r></w:p>'


def member(raw, name="word/document.xml"):
    with zipfile.ZipFile(io.BytesIO(raw)) as zf:
        return zf.read(name)


def local_records(raw):
    """独立读取各成员的 local header + 压缩流 + descriptor，比较未改成员原字节。"""
    with zipfile.ZipFile(io.BytesIO(raw)) as zf:
        infos = sorted(zf.infolist(), key=lambda i: i.header_offset)
        return {i.filename: raw[i.header_offset:infos[n + 1].header_offset if n + 1 < len(infos) else zf.start_dir]
                for n, i in enumerate(infos)}


class WritebackTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="ae-docx-edit-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.review = self.root / "review"
        self.review.mkdir()
        self.source = self.root / "synthetic.docx"

    def document(self, inner=None, *, raw=None, extra=None):
        raw = raw if raw is not None else ms.body_xml(inner or para("甲乙丙"))
        ms.package(self.source, raw, extra=extra)
        self.original = self.source.read_bytes()
        self.fp = hashlib.sha256(self.original).hexdigest()
        self.manifest = {"format": {"report": "docx", "version": 1}, "sources": {"report": "../synthetic.docx"},
                         "docx": {"fingerprint": self.fp}}
        (self.review / "review.json").write_text(json.dumps(self.manifest), encoding="utf-8")
        return self.source

    def replace(self, before="乙", after="新", **kwargs):
        return de.writeback(self.review, replacement={"paragraph": 0, "from": before, "to": after}, **kwargs)

    def note(self, before="乙", after="新", *, n=0, full="甲乙丙", pid="0000000A", **location):
        a = {"v": 1, "kind": "quote", "via": "selection", "fp": self.fp,
             "para": {"id": pid, "n": n, "digest": dr.text_digest(full)}, **location}
        _doc, note = store.add(self.review, {"page": 1, "target": "报告", "quoteFrom": "报告", "quote": before,
            "text": "改为：" + after if after else "删除", "docx": a,
            "edit": {"op": "replace", "from": before, "to": after} if after else {"op": "delete", "from": before}})
        return note["id"]

    def assert_refused(self, code, action=None):
        with self.assertRaises(de.EditError) as caught:
            (action or self.replace)()
        errors = caught.exception.errors if isinstance(caught.exception, de.SelectionError) else [caught.exception]
        self.assertIn(code, [e.code for e in errors])
        self.assertEqual(self.source.read_bytes(), self.original)
        return errors

    def test_batch_deletion_and_replacement_preserve_untouched_bytes_and_notes(self):
        self.document(para("甲乙丙") + para("第二段", "0000000B") + para("旁观段", "0000000C"))
        one = self.note(after="")
        two = self.note(before="第二", after="新", n=1, full="第二段", pid="0000000B")
        _doc, batch, _merged = store.handoff_submit(self.review)
        notes_before = (self.review / "notes.json").read_bytes()
        result = de.writeback(self.review, batch=batch["seq"])
        self.assertEqual(result["ids"], [one, two])
        expected = member(self.original).replace(b"<w:t>" + "甲乙丙".encode() + b"</w:t>", b'<w:t xml:space="preserve">' + "甲丙".encode() + b"</w:t>")
        expected = expected.replace(b"<w:t>" + "第二段".encode() + b"</w:t>", b'<w:t xml:space="preserve">' + "新段".encode() + b"</w:t>")
        self.assertEqual(member(self.source.read_bytes()), expected)
        self.assertEqual((self.review / "notes.json").read_bytes(), notes_before)
        before, after = local_records(self.original), local_records(self.source.read_bytes())
        for name in before:
            if name != "word/document.xml":
                self.assertEqual(after[name], before[name], name)
        backup = Path(result["backup"])
        self.assertEqual(backup.read_bytes(), self.original)
        self.assertEqual(stat.S_IMODE(backup.stat().st_mode), 0o600)
        self.assertIn("batch-1/docx-writeback", str(backup))

    def test_cross_run_replacement_keeps_run_properties_and_paragraphs(self):
        self.document('<w:p><w:r><w:rPr><w:b/></w:rPr><w:t>合同金</w:t></w:r>'
                      '<w:r><w:rPr><w:i/></w:rPr><w:t>额为100元。</w:t></w:r></w:p>')
        self.replace("金额为100", "合计200")
        xml = member(self.source.read_bytes())
        self.assertIn('<w:t xml:space="preserve">合同合计200</w:t>'.encode(), xml)
        self.assertIn('<w:t xml:space="preserve">元。</w:t>'.encode(), xml)
        self.assertEqual(xml.count(b"<w:r>"), 2)
        self.assertIn(b"<w:rPr><w:i/></w:rPr>", xml)

    def test_utf8_bom_alt_prefix_entities_cdata_and_empty_elements(self):
        raw = ('\ufeff' + ms.body_xml('<w:p><w:r><w:t/><w:t></w:t><w:t>甲&amp;<![CDATA[<乙>]]>&#x4E19;</w:t></w:r></w:p>')).replace("w:", "x:").replace("xmlns:w=", "xmlns:x=")
        self.document(raw=raw)
        self.replace("&<乙>", " <&新> ")
        xml = member(self.source.read_bytes())
        expected = member(self.original).replace('<x:t>甲&amp;<![CDATA[<乙>]]>&#x4E19;</x:t>'.encode(),
                    '<x:t xml:space="preserve">甲 &lt;&amp;新&gt; 丙</x:t>'.encode())
        self.assertEqual(xml, expected)
        self.assertTrue(xml.startswith(b"\xef\xbb\xbf"))
        self.assertIn(b"<x:t/><x:t></x:t>", xml)

    def test_space_default_single_quotes_and_attribute_greater_than(self):
        self.document('<w:p><w:r><w:t xml:space=\'default\' custom="a>b">甲乙丙</w:t></w:r></w:p>')
        self.replace("甲乙丙", " 新 \r\n\t")
        xml = member(self.source.read_bytes())
        self.assertIn(b'xml:space="preserve" custom="a>b"', xml)
        self.assertIn(b"&#13;", xml)
        self.assertEqual(ET.fromstring(xml).find('.//' + dr.W + 't').text, " 新 \r\n\t")

    def test_whitespace_fallback_and_non_bmp_characters(self):
        self.document(para("甲 😀  乙 丙"))
        self.replace("😀乙", "新")
        self.assertIn("甲 新 丙".encode(), member(self.source.read_bytes()))

    def test_precise_match_wins_over_whitespace_folded_match(self):
        self.document(para("甲 乙；甲乙"))
        self.replace("甲乙", "新")
        self.assertIn("甲 乙；新".encode(), member(self.source.read_bytes()))

    def test_repeated_text_requires_offsets(self):
        self.document(para("乙乙"))
        nid = self.note(before="乙", full="乙乙", start=1, end=2)
        de.writeback(self.review, ids=[nid])
        self.assertIn("乙新".encode(), member(self.source.read_bytes()))

    def test_ambiguous_text_is_refused_without_offsets(self):
        self.document(para("乙乙"))
        self.assert_refused("ambiguous")

    def test_selection_with_collapsed_whitespace_does_not_edit_another_exact_match(self):
        self.document(para("甲 乙；甲  乙"))
        nid = self.note(before="甲 乙", full="甲 乙；甲  乙", start=4, end=8)
        de.writeback(self.review, ids=[nid])
        self.assertIn("甲 乙；新".encode(), member(self.source.read_bytes()))

    def test_selection_offsets_and_quote_must_agree(self):
        self.document()
        nid = self.note(before="乙", start=0, end=1)
        self.assert_refused("notFound", lambda: de.writeback(self.review, ids=[nid]))

    def test_unique_id_still_needs_the_original_text(self):
        self.document(para("甲乙丙") + para("需要修改", "0000000B"))
        nid = self.note(before="需要", full="甲乙丙")
        self.assert_refused("notFound", lambda: de.writeback(self.review, ids=[nid]))

    def test_duplicate_id_uses_ordinal_and_digest(self):
        self.document(para("甲乙丙") + para("甲乙丙"))
        nid = self.note(n=1)
        de.writeback(self.review, ids=[nid])
        texts = [p.text for p in de.parse_document(member(self.source.read_bytes()))]
        self.assertEqual(texts, ["甲乙丙", "甲新丙"])

    def test_no_id_unique_digest_survives_shifted_ordinal(self):
        self.document(para("前置", "00000001") + '<w:p><w:r><w:t>甲乙丙</w:t></w:r></w:p>')
        nid = self.note(n=0, pid=None)
        de.writeback(self.review, ids=[nid])
        self.assertIn("甲新丙".encode(), member(self.source.read_bytes()))

    def test_stale_or_future_anchor_is_refused(self):
        for location in ({"fp": "0" * 64}, {"changed": True}, {"v": 2}):
            with self.subTest(location=location):
                self.document()
                nid = self.note(**location)
                self.assert_refused("anchor", lambda: de.writeback(self.review, ids=[nid]))

    def test_overlapping_edits_refuse_the_entire_selection(self):
        self.document()
        one, two = self.note(before="甲乙"), self.note(before="乙丙")
        self.assert_refused("overlap", lambda: de.writeback(self.review, ids=[one, two]))

    def test_disjoint_edits_in_one_text_element_use_original_offsets(self):
        self.document()
        one, two = self.note(before="甲", after="长的替换"), self.note(before="丙", after="")
        de.writeback(self.review, ids=[one, two])
        self.assertIn("长的替换乙".encode(), member(self.source.read_bytes()))

    def test_cross_paragraph_and_nontext_ranges_are_refused(self):
        self.document()
        nid = self.note(endPara={"id": None, "n": 1, "digest": "a" * 12})
        self.assert_refused("range", lambda: de.writeback(self.review, ids=[nid]))
        for special in ("<w:tab/>", "<w:br/>", "<w:noBreakHyphen/>"):
            with self.subTest(special=special):
                self.document(f'<w:p><w:r><w:t>甲</w:t>{special}<w:t>乙</w:t></w:r></w:p>')
                text = {"<w:tab/>": "甲\t乙", "<w:br/>": "甲乙", "<w:noBreakHyphen/>": "甲‑乙"}[special]
                self.assert_refused("range", lambda: self.replace(text))

    def test_utf16_and_invalid_xml_characters_are_refused(self):
        self.document(raw=ms.body_xml(para("甲乙丙")).replace("UTF-8", "UTF-16").encode("utf-16"))
        self.assert_refused("encoding")
        self.document()
        for bad in ("a\x00b", "a\x01b", "a\ud800b", "\ufffe"):
            self.assert_refused("characters", lambda: self.replace(after=bad))

    def test_dtd_and_nested_text_elements_are_refused(self):
        for raw in (ms.body_xml(para("甲乙丙")).replace('<w:document', '<!DOCTYPE w:document [<!ENTITY x "乙">]><w:document'),
                    ms.body_xml('<w:p><w:r><w:t>甲<unexpected/>乙</w:t></w:r></w:p>')):
            self.document(raw=raw)
            self.assert_refused("xml")

    def test_zip_comments_extra_fields_and_streaming_descriptors_are_preserved(self):
        self.document()

        class Stream(io.BytesIO):
            def seek(self, *_args):
                raise OSError("stream cannot seek")

        stream = Stream()
        with zipfile.ZipFile(io.BytesIO(self.original)) as old, zipfile.ZipFile(stream, "w") as out:
            out.comment = b"synthetic ZIP comment"
            for info in old.infolist():
                info.extra = b"\xfe\xca\x03\x00xyz"
                info.comment = b"synthetic member"
                info.external_attr = 0o600 << 16
                out.writestr(info, old.read(info.filename))
        self.source.write_bytes(stream.getvalue())
        self.original = self.source.read_bytes()
        self.manifest["docx"]["fingerprint"] = hashlib.sha256(self.original).hexdigest()
        (self.review / "review.json").write_text(json.dumps(self.manifest))
        self.replace()
        new_raw = self.source.read_bytes()
        before, after = local_records(self.original), local_records(new_raw)
        for name in before:
            if name != "word/document.xml":
                self.assertEqual(before[name], after[name])
        with zipfile.ZipFile(io.BytesIO(self.original)) as old, zipfile.ZipFile(io.BytesIO(new_raw)) as new:
            self.assertEqual(old.comment, new.comment)
            for name in old.namelist():
                a, b = old.getinfo(name), new.getinfo(name)
                self.assertEqual((a.extra, a.comment, a.external_attr, a.date_time), (b.extra, b.comment, b.external_attr, b.date_time))

    def test_readonly_math_note_explains_structure_even_when_digest_contains_math_text(self):
        self.document(f'<w:p><w:r><w:t>甲乙丙</w:t></w:r><m:oMath xmlns:m="{dr.M[1:-1]}"><m:r><m:t>x</m:t></m:r></m:oMath></w:p>')
        nid = self.note(full="甲乙丙x", pid=None)
        errors = self.assert_refused("readonly", lambda: de.writeback(self.review, ids=[nid]))
        self.assertIn("math", errors[0].detail)

    def test_table_cell_revisions_and_unknown_run_content_are_readonly(self):
        for kind in ("cellIns", "cellDel", "cellMerge", "tcPrChange"):
            self.document(f'<w:tbl><w:tr><w:tc><w:tcPr><w:{kind}/></w:tcPr>{para("甲乙丙")}</w:tc></w:tr></w:tbl>')
            self.assert_refused("readonly")
        self.document('<w:p><w:r><w:t>甲乙丙</w:t><w:unknownContent/></w:r></w:p>')
        self.assert_refused("readonly")

    def test_tracking_needs_explicit_untracked_flag_and_warns(self):
        self.document(extra={"word/settings.xml": f'<w:settings xmlns:w="{dr.W_NS}"><w:trackRevisions/></w:settings>'})
        self.assert_refused("tracked")
        result = self.replace(allow_untracked=True)
        self.assertEqual(result["warnings"], ["untracked"])
        self.assertEqual(member(self.original, "word/settings.xml"), member(self.source.read_bytes(), "word/settings.xml"))

    def test_disabled_tracking_and_protection_allow_editing(self):
        self.document(extra={"word/settings.xml": f'<w:settings xmlns:w="{dr.W_NS}"><w:trackRevisions w:val="false"/><w:documentProtection w:enforcement="0"/></w:settings>'})
        self.replace()

    def test_editing_protection_and_signatures_cannot_be_overridden(self):
        for code, extra in (("protected", {"word/settings.xml": f'<w:settings xmlns:w="{dr.W_NS}"><w:documentProtection w:enforcement="1"/></w:settings>'}),
                            ("signed", {"_xmlsignatures/sig1.xml": "signature"}),
                            ("signed", {"_rels/signature.rels": '<Relationships><Relationship Type="http://example/digital-signature/signature"/></Relationships>'})):
            with self.subTest(code=code):
                self.document(extra=extra)
                self.assert_refused(code, lambda: self.replace(allow_untracked=True))

    def test_word_owner_file_blocks_write_and_preview(self):
        self.document()
        lock = self.source.with_name("~$" + self.source.name)
        lock.write_bytes(b"word")
        self.assert_refused("locked")
        self.assert_refused("locked", lambda: self.replace(dry_run=True))

    def test_fingerprint_mismatch_has_no_force_override(self):
        self.document()
        self.source.write_bytes(self.original + b"changed")
        self.original = self.source.read_bytes()
        self.assert_refused("fingerprint", lambda: self.replace(allow_untracked=True))

    def test_readonly_structures_report_each_note_without_partial_write(self):
        self.document(para("甲乙丙") + '<w:p><w:ins><w:r><w:t>需要修改</w:t></w:r></w:ins></w:p>')
        one = self.note()
        two = self.note(before="需要", full="需要修改", n=1, pid=None)
        errors = self.assert_refused("readonly", lambda: de.writeback(self.review, ids=[one, two]))
        self.assertEqual(errors[0].item, two)
        self.assertEqual(errors[0].detail, "revision")

    def test_all_unsupported_structures_are_refused_in_page_and_writer(self):
        variants = {
            "revision": '<w:p><w:ins><w:r><w:t>甲乙丙</w:t></w:r></w:ins></w:p>',
            "field": '<w:p><w:fldSimple w:instr="TOC"><w:r><w:t>甲乙丙</w:t></w:r></w:fldSimple></w:p>',
            "contentControl": '<w:sdt><w:sdtContent>' + para("甲乙丙") + '</w:sdtContent></w:sdt>',
            "textbox": '<w:p><w:r><w:t>甲乙丙</w:t><w:pict><w:txbxContent>' + para("内文") + '</w:txbxContent></w:pict></w:r></w:p>',
            "math": f'<w:p><w:r><w:t>甲乙丙</w:t></w:r><m:oMath xmlns:m="{dr.M[1:-1]}"><m:r><m:t>x</m:t></m:r></m:oMath></w:p>',
            "hidden": '<w:p><w:r><w:rPr><w:vanish/></w:rPr><w:t>甲乙丙</w:t></w:r></w:p>',
        }
        for reason, inner in variants.items():
            with self.subTest(reason=reason):
                self.document(inner)
                errors = self.assert_refused("readonly")
                self.assertIn(reason, errors[0].detail)
                page = dr.render(self.source)["pages"][0]["html"]
                self.assertRegex(page, 'data-docx-ro="[^"]*' + reason)

    def test_cross_paragraph_field_and_inherited_hidden_style_are_readonly(self):
        self.document('<w:p><w:r><w:fldChar w:fldCharType="begin"/></w:r></w:p>' + para("甲乙丙") +
                      '<w:p><w:r><w:fldChar w:fldCharType="end"/></w:r></w:p>')
        self.assert_refused("readonly", lambda: de.writeback(self.review, replacement={"paragraph": 1, "from": "乙", "to": "新"}))
        styles = f'<w:styles xmlns:w="{dr.W_NS}"><w:style w:styleId="Hide"><w:rPr><w:vanish/></w:rPr></w:style></w:styles>'
        self.document('<w:p><w:pPr><w:pStyle w:val="Hide"/></w:pPr><w:r><w:t>甲乙丙</w:t></w:r></w:p>', extra={"word/styles.xml": styles})
        self.assert_refused("readonly")
        self.assertIn('data-docx-ro="hidden"', dr.render(self.source)["pages"][0]["html"])

    def test_character_style_false_preserves_inherited_hidden_text(self):
        styles = (f'<w:styles xmlns:w="{dr.W_NS}">'
                  '<w:style w:type="character" w:styleId="BaseChar"><w:rPr><w:vanish/></w:rPr></w:style>'
                  '<w:style w:type="character" w:styleId="DerivedChar"><w:basedOn w:val="BaseChar"/>'
                  '<w:rPr><w:vanish w:val="0"/></w:rPr></w:style></w:styles>')
        self.document('<w:p><w:r><w:rPr><w:rStyle w:val="DerivedChar"/></w:rPr>'
                      '<w:t>甲乙丙</w:t></w:r></w:p>', extra={"word/styles.xml": styles})
        nid = self.note(pid=None)
        self.assert_refused("readonly")
        self.assert_refused("readonly", lambda: de.writeback(self.review, ids=[nid]))
        rendered = dr.render(self.source)
        self.assertIn('data-docx-ro="hidden"', rendered["pages"][0]["html"])
        self.assertRegex(rendered["pages"][0]["html"], r'<span [^>]*\bhidden[^>]*>甲乙丙</span>')
        self.assertIn("hidden", [item["code"] for item in rendered["diagnostics"]])

    def test_character_style_toggle_and_direct_format_can_unhide_text(self):
        for derived, direct in (("1", ""), ("0", '<w:vanish w:val="0"/>')):
            with self.subTest(derived=derived, direct=direct):
                styles = (f'<w:styles xmlns:w="{dr.W_NS}">'
                          '<w:style w:type="character" w:styleId="BaseChar"><w:rPr><w:vanish/></w:rPr></w:style>'
                          '<w:style w:type="character" w:styleId="DerivedChar"><w:basedOn w:val="BaseChar"/>'
                          f'<w:rPr><w:vanish w:val="{derived}"/></w:rPr></w:style></w:styles>')
                self.document('<w:p><w:r><w:rPr><w:rStyle w:val="DerivedChar"/>' + direct
                              + '</w:rPr><w:t>甲乙丙</w:t></w:r></w:p>', extra={"word/styles.xml": styles})
                self.assertNotIn('data-docx-ro="hidden"', dr.render(self.source)["pages"][0]["html"])
                self.replace()
                self.assertIn("甲新丙".encode(), member(self.source.read_bytes()))

    def test_table_styles_including_inheritance_and_conditions_are_readonly(self):
        for body in ('<w:rPr><w:vanish/></w:rPr>', '<w:tblStylePr w:type="firstRow"><w:rPr><w:vanish/></w:rPr></w:tblStylePr>'):
            for explicit in (True, False):
                with self.subTest(body=body, explicit=explicit):
                    styles = (f'<w:styles xmlns:w="{dr.W_NS}"><w:style w:type="table" w:styleId="Base">{body}</w:style>'
                              '<w:style w:type="table" w:styleId="HiddenTable" w:default="1"><w:basedOn w:val="Base"/></w:style></w:styles>')
                    props = '<w:tblPr><w:tblStyle w:val="HiddenTable"/></w:tblPr>' if explicit else ''
                    self.document(f'<w:tbl>{props}<w:tr><w:tc>{para("甲乙丙")}</w:tc></w:tr></w:tbl>', extra={"word/styles.xml": styles})
                    errors = self.assert_refused("readonly")
                    self.assertEqual(errors[0].detail, "hidden")
                    self.assertIn('data-docx-ro="hidden"', dr.render(self.source)["pages"][0]["html"])

    def test_nontext_symbols_are_exposed_to_the_page_without_changing_text_offsets(self):
        for token, visible in (("<w:tab/>", "\t"), ("<w:noBreakHyphen/>", "‑"), ("<w:softHyphen/>", "­"), ('<w:sym w:char="0041"/>', "A")):
            with self.subTest(token=token):
                self.document(f'<w:p><w:r><w:t>甲</w:t>{token}<w:t>乙</w:t></w:r></w:p>')
                result = dr.render(self.source)
                self.assertEqual(result["paragraphs"][0]["digest"], dr.text_digest("甲" + visible + "乙"))
                self.assertRegex(result["pages"][0]["html"], r'class="docx-(?:tab|nontext)"')
                self.assert_refused("range", lambda: self.replace("甲" + visible + "乙"))

    def test_dry_run_has_no_output_or_backup(self):
        self.document()
        output = self.root / "preview.docx"
        result = self.replace(output=output, dry_run=True)
        self.assertTrue(result["dryRun"])
        self.assertEqual(self.source.read_bytes(), self.original)
        self.assertFalse(output.exists())
        self.assertFalse((self.review / ".snapshots").exists())

    def test_output_keeps_source_and_existing_output_is_not_overwritten(self):
        self.document()
        output = self.root / "copy.docx"
        self.replace(output=output)
        self.assertEqual(self.source.read_bytes(), self.original)
        changed = output.read_bytes()
        self.assertIn("甲新丙".encode(), member(changed))
        self.assert_refused("output", lambda: self.replace(output=output))
        self.assertEqual(output.read_bytes(), changed)

    def test_fsync_or_replace_failure_leaves_original_and_usable_backup(self):
        self.document()
        for method in ("fsync", "replace"):
            with self.subTest(method=method):
                if method == "fsync":
                    # 第一次 fsync 是原件备份，第二次是待提交文档。
                    patcher = patch.object(de.os, method, side_effect=[None, OSError("disk full")])
                else:
                    patcher = patch.object(de.os, method, side_effect=OSError("rename denied"))
                with patcher, self.assertRaises(OSError):
                    self.replace()
                self.assertEqual(self.source.read_bytes(), self.original)
                backups = list((self.review / ".snapshots").rglob("*.docx"))
                self.assertTrue(backups)
                self.assertTrue(all(p.read_bytes() == self.original for p in backups))
                self.assertFalse(list(self.root.glob("*.tmp")))

    def test_postwrite_corruption_does_not_replace_original(self):
        self.document()
        real_fsync = os.fsync
        count = 0

        def corrupt_pending(fd):
            nonlocal count
            real_fsync(fd)
            count += 1
            if count == 2:
                os.lseek(fd, 0, os.SEEK_SET)
                os.write(fd, b"broken zip")

        with patch.object(de.os, "fsync", side_effect=corrupt_pending), self.assertRaises((de.EditError, zipfile.BadZipFile)):
            self.replace()
        self.assertEqual(self.source.read_bytes(), self.original)
        self.assertEqual(next((self.review / ".snapshots").rglob("*.docx")).read_bytes(), self.original)

    def test_source_changed_during_write_is_not_overwritten(self):
        self.document()
        real_fsync = os.fsync
        count = 0

        def change_source(fd):
            nonlocal count
            real_fsync(fd)
            count += 1
            if count == 2:
                self.source.write_bytes(self.original + b"external change")

        with patch.object(de.os, "fsync", side_effect=change_source), self.assertRaises(de.EditError) as caught:
            self.replace()
        self.assertEqual(caught.exception.code, "fingerprint")
        self.assertEqual(self.source.read_bytes(), self.original + b"external change")
        self.assertEqual(next((self.review / ".snapshots").rglob("*.docx")).read_bytes(), self.original)

    def test_binary_snapshot_and_page_diff_work_after_writeback(self):
        self.document()
        notes_cli.snapshot(self.review, {"seq": 1, "target": "review"}, "base")
        self.replace()
        notes_cli.snapshot(self.review, {"seq": 1, "target": "review"}, "draft")
        snap = self.review / ".snapshots/batch-1/base/report"
        self.assertEqual(snap.read_bytes(), self.original)
        out = io.StringIO()
        with redirect_stdout(out):
            result = notes_cli.diff_cmd(self.review, 1, "draft", None, "zh-CN")
        self.assertEqual(result, 0)
        self.assertIn("甲乙丙", out.getvalue())
        self.assertIn("甲新丙", out.getvalue())

    def test_cli_locales_dry_run_and_tracking_warning(self):
        self.document(extra={"word/settings.xml": f'<w:settings xmlns:w="{dr.W_NS}"><w:trackRevisions/></w:settings>'})
        for lang, phrase in (("zh-CN", "AE 写入的是不留痕改动"), ("en", "AE writes untracked changes")):
            out, err = io.StringIO(), io.StringIO()
            with redirect_stdout(out), redirect_stderr(err):
                code = de.main(["replace", str(self.review), "--paragraph", "0", "--from", "乙", "--to", "新", "--dry-run", "--json", "--allow-untracked", "--locale", lang])
            self.assertEqual(code, 0, err.getvalue())
            self.assertTrue(json.loads(out.getvalue())["dryRun"])
            self.assertIn(phrase, err.getvalue())
        self.assertEqual(self.source.read_bytes(), self.original)

    def test_cli_argument_errors_use_the_shared_language_contract(self):
        self.document()
        cases = [(["--locale"], {}, "参数 --locale 需要一个值"),
                 (["--locale", "en", "apply", str(self.review), "--id"], {}, "Option --id needs a value"),
                 (["apply", str(self.review), "--id", "missing"], {"AE_LOCALE": "fr"}, "不支持的语言：fr"),
                 (["apply", str(self.review), "--unknown", "--locale", "en"], {}, "Unrecognized argument: --unknown")]
        for args, env, expected in cases:
            with self.subTest(args=args), patch.dict(os.environ, {"AE_LOCALE": "", **env}):
                out, err = io.StringIO(), io.StringIO()
                with redirect_stdout(out), redirect_stderr(err):
                    try:
                        code = de.main(args)
                    except SystemExit as exc:
                        code = exc.code
                self.assertEqual(code, 2)
                self.assertIn(expected, err.getvalue())
                self.assertNotIn("None", err.getvalue())
        self.assertEqual(self.source.read_bytes(), self.original)

    def test_invalid_stored_locale_reports_its_actual_value(self):
        self.document()
        self.manifest["locale"] = "fr"
        (self.review / "review.json").write_text(json.dumps(self.manifest))
        err = io.StringIO()
        with patch.dict(os.environ, {"AE_LOCALE": ""}), redirect_stderr(err):
            code = de.main(["apply", str(self.review), "--id", "missing"])
        self.assertEqual(code, 2)
        self.assertIn("不支持的语言：fr", err.getvalue())


if __name__ == "__main__":
    unittest.main()
