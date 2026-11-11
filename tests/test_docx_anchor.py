"""DOCX 批注定位（#136）：段落索引、定位字段、重建时按段落重映射、show 与导出。

fixture 全部现场合成，不入库真实文档。只用标准库：python3 -m unittest tests.test_docx_anchor
"""
from __future__ import annotations

import hashlib
import io
import json
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "skill" / "annotation-everything" / "scripts"
sys.path.insert(0, str(SCRIPTS))
sys.path.insert(0, str(ROOT / "fixtures" / "docx"))
os.environ["AE_HOME"] = tempfile.mkdtemp(prefix="ae-test-home-")   # 测试不碰用户真实的审阅台账
os.environ["HOME"] = tempfile.mkdtemp(prefix="ae-test-user-")

import build_viewer  # noqa: E402
import docx_review as dr  # noqa: E402
import export_format as ef  # noqa: E402
import make_sample as ms  # noqa: E402
import notes as notes_cli  # noqa: E402
import notes_store as store  # noqa: E402

LRPB = "<w:lastRenderedPageBreak/>"
PAGE_BR = '<w:p><w:r><w:br w:type="page"/></w:r></w:p>'   # 只有分页符的段落：占一个段落序号


def tmp() -> Path:
    return Path(tempfile.mkdtemp(prefix="ae-docx-anchor-"))


def para(text: str = "", *, pid: str | None = None, runs: str = "") -> str:
    attr = f' w14:paraId="{pid}"' if pid else ""
    body = (f'<w:r><w:t xml:space="preserve">{text}</w:t></w:r>' if text else "") + runs
    return f"<w:p{attr}>{body}</w:p>"


def docx(inner: str, path: Path | None = None) -> Path:
    return ms.package(path or tmp() / "t.docx", ms.body_xml(inner, ""))


def digest(text: str) -> str:
    """规范里的段落文本摘要：去掉全部空白后取 SHA-1 的前 12 位十六进制。"""
    return hashlib.sha1("".join(text.split()).encode("utf-8")).hexdigest()[:12]


def anchor(text: str, n: int, *, pid: str | None = None, **more) -> dict:
    return {"v": 1, "kind": "quote", "via": "selection", "para": {"id": pid, "n": n, "digest": digest(text)}, **more}


class ParagraphIndexTests(unittest.TestCase):
    def test_index_lists_digest_and_fragment_offsets(self):
        split = f'<w:p w14:paraId="0000000B"><w:r><w:t>前半</w:t></w:r><w:r>{LRPB}<w:t>后 半</w:t></w:r></w:p>'
        r = dr.render(docx(para("第一段", pid="0000000A") + split))
        self.assertEqual(r["paragraphs"], [
            {"n": 0, "id": "0000000A", "digest": digest("第一段"), "frags": [{"page": 1, "start": 0}]},
            {"n": 1, "id": "0000000B", "digest": digest("前半后半"),
             "frags": [{"page": 1, "start": 0}, {"page": 2, "start": 2}]},
        ])

    def test_pages_expose_digest_and_continuation_offset(self):
        split = f'<w:p><w:r><w:t>前半</w:t></w:r><w:r>{LRPB}<w:t>后半</w:t></w:r></w:p>'
        pages = dr.render(docx(split))["pages"]
        d = digest("前半后半")
        self.assertIn(f'data-docx-p="0" data-docx-d="{d}"', pages[0]["html"])
        self.assertNotIn("data-docx-o=", pages[0]["html"], "首片从 0 开始，不写偏移")
        self.assertIn(f'data-docx-p="0" data-docx-d="{d}" data-docx-o="2"', pages[1]["html"])

    def test_list_numbers_are_not_part_of_the_digest(self):
        numbered = ('<w:p><w:pPr><w:numPr><w:ilvl w:val="0"/><w:numId w:val="1"/></w:numPr></w:pPr>'
                    "<w:r><w:t>条款正文</w:t></w:r></w:p>")
        r = dr.render(docx(numbered))
        self.assertIn("docx-num", r["pages"][0]["html"], "编号已画出")
        self.assertEqual(r["paragraphs"][0]["digest"], digest("条款正文"))

    def test_fingerprint_is_the_source_sha256(self):
        path = docx(para("甲"))
        self.assertEqual(dr.render(path)["fingerprint"], hashlib.sha256(path.read_bytes()).hexdigest())


class RelocateTests(unittest.TestCase):
    """重建后按段落找回批注：模拟 Word 重排后的新文档，用旧版写下的定位去找。"""

    def locate(self, inner: str, a: dict) -> dict:
        return dr.relocate(a, dr.render(docx(inner))["paragraphs"])

    def test_para_id_follows_paragraph_when_page_start_changes(self):
        # 旧版：目标段是第 2 页页首；新版：前面多了一段，目标段成了第 2 页的第二段
        new = para("甲", pid="0000000A") + PAGE_BR + para("新增", pid="0000000C") + para("目标段", pid="0000000B")
        got = self.locate(new, anchor("目标段", 2, pid="0000000B"))
        self.assertEqual((got["status"], got["how"], got["page"], got["para"]["n"], got["changed"]),
                         ("ok", "paraId", 2, 3, False))

    def test_para_id_match_with_changed_text_is_flagged(self):
        got = self.locate(para("目标段改过了", pid="0000000B"), anchor("目标段", 0, pid="0000000B"))
        self.assertEqual((got["status"], got["how"], got["changed"]), ("ok", "paraId", True))

    def test_without_para_id_unique_digest_survives_inserted_paragraphs_and_pages(self):
        new = para("插入一") + PAGE_BR + para("插入二") + PAGE_BR + para("甲") + para("目标段")
        got = self.locate(new, anchor("目标段", 1))
        self.assertEqual((got["status"], got["how"], got["why"], got["page"]), ("ok", "digest", "noId", 3))

    def test_duplicate_text_needs_matching_ordinal(self):
        new = para("同上") + para("甲") + para("同上")
        same = self.locate(new, anchor("同上", 2))
        self.assertEqual((same["status"], same["how"], same["para"]["n"]), ("ok", "ordinal", 2))
        moved = self.locate(para("插入") + new, anchor("同上", 2))
        self.assertEqual((moved["status"], moved["why"]), ("ambiguous", "noId"))

    def test_duplicate_para_id_falls_back_to_digest(self):
        new = para("甲", pid="0000000A") + para("目标段", pid="0000000A")
        got = self.locate(new, anchor("目标段", 0, pid="0000000A"))
        self.assertEqual((got["status"], got["how"], got["why"], got["para"]["n"]), ("ok", "digest", "duplicateId", 1))

    def test_regenerated_para_ids_fall_back_to_digest(self):
        got = self.locate(para("目标段", pid="000000FF"), anchor("目标段", 5, pid="0000000B"))
        self.assertEqual((got["status"], got["how"], got["why"]), ("ok", "digest", "idGone"))

    def test_deleted_paragraph_is_missing(self):
        got = self.locate(para("别的内容", pid="0000000A"), anchor("目标段", 0, pid="0000000B"))
        self.assertEqual((got["status"], got["page"]), ("missing", None))

    def test_offset_picks_the_page_of_a_split_paragraph(self):
        split = f'<w:p w14:paraId="0000000B"><w:r><w:t>前半</w:t></w:r><w:r>{LRPB}<w:t>后半</w:t></w:r></w:p>'
        self.assertEqual(self.locate(split, anchor("前半后半", 0, pid="0000000B", start=1, end=2))["page"], 1)
        self.assertEqual(self.locate(split, anchor("前半后半", 0, pid="0000000B", start=2, end=4))["page"], 2)


def review_dir() -> Path:
    d = tmp()
    (d / "review.json").write_text("{}", encoding="utf-8")
    return d


class AnchorFieldTests(unittest.TestCase):
    """批注的 docx 定位字段：写入时清洗，旧批注与更高版本的字段都不受影响。"""

    def test_add_keeps_a_valid_anchor_and_drops_unknown_keys(self):
        d = review_dir()
        a = {**anchor("目标段", 3, pid="0000000b", start=2, end=5, run=1), "fp": "ab" * 32, "junk": "x",
             "endPara": {"id": None, "n": 4, "digest": digest("下一段"), "extra": 1}}
        _doc, note = store.add(d, {"page": 2, "text": "改一下", "docx": a})
        self.assertEqual(note["docx"], {
            "v": 1, "kind": "quote", "via": "selection", "fp": "ab" * 32,
            "para": {"id": "0000000B", "n": 3, "digest": digest("目标段")}, "start": 2, "end": 5,
            "endPara": {"id": None, "n": 4, "digest": digest("下一段")}, "run": 1})

    def test_malformed_anchor_is_rejected(self):
        d = review_dir()
        for bad in ("x", {"v": 1}, {**anchor("甲", 0), "kind": "nope"}, {**anchor("甲", 0), "para": {"n": -1, "digest": "zz"}},
                    {**anchor("甲", 0), "start": 5, "end": 2}, {"v": 0}, {"v": True}):
            with self.subTest(bad=bad), self.assertRaises(store.NoteError):
                store.add(d, {"page": 1, "text": "x", "docx": bad})

    def test_notes_without_anchor_stay_as_before(self):
        d = review_dir()
        _doc, note = store.add(d, {"page": 1, "text": "旧式批注"})
        self.assertNotIn("docx", note)

    def test_newer_anchor_version_is_kept_verbatim(self):
        d = review_dir()
        future = {"v": 2, "shape": {"new": [1, 2]}, "para": "不认识的结构"}
        _doc, note = store.add(d, {"page": 1, "text": "x", "docx": future})
        self.assertEqual(note["docx"], future)
        with self.assertRaises(store.NoteError):
            store.add(d, {"page": 1, "text": "y", "docx": {"v": 2, "blob": "x" * 5000}})

    def test_update_replaces_or_clears_the_anchor(self):
        d = review_dir()
        _doc, note = store.add(d, {"page": 1, "text": "x", "docx": {**anchor("甲", 0), "kind": "pin", "via": "point"}})
        _doc, note = store.update(d, note["id"], {"docx": {**anchor("乙", 1), "kind": "region", "via": "point"}})
        self.assertEqual((note["docx"]["kind"], note["docx"]["para"]["n"]), ("region", 1))
        _doc, note = store.update(d, note["id"], {"docx": None})
        self.assertNotIn("docx", note)

    def test_import_round_trips_the_anchor(self):
        d = review_dir()
        a = anchor("甲", 0, pid="0000000A")
        store.import_notes(d, [{"id": "n1", "page": 1, "text": "x", "docx": a}])
        self.assertEqual(store.load(d)["notes"][0]["docx"], a)


class RemapTests(unittest.TestCase):
    """重建对照页：先按段落把批注归到现在所在的页，页面键只管没有段落定位的批注。"""

    OLD_FP, NEW_FP = "0" * 64, "1" * 64

    def remap(self, d: Path, inner: str, old_keys: dict[int, str] | None = None) -> dict:
        r = dr.render(docx(inner))
        pages = r["pages"]
        return store.remap_pages(d, old_keys or {}, {pg["key"]: pg["n"] for pg in pages},
                                 {pg["n"]: pg["title"] for pg in pages},
                                 docx={"paragraphs": r["paragraphs"], "fingerprint": self.NEW_FP,
                                       "keys": {pg["n"]: pg["key"] for pg in pages}})

    def add(self, d: Path, page: int, key: str, a: dict | None) -> str:
        data = {"page": page, "key": key, "title": "旧标题", "text": "意见"}
        if a:
            data["docx"] = {**a, "fp": self.OLD_FP}
        return store.add(d, data)[1]["id"]

    def note(self, d: Path, nid: str) -> dict:
        return next(n for n in store.load(d)["notes"] if n["id"] == nid)

    def test_note_follows_its_paragraph_when_the_page_start_changes(self):
        # 旧版第 2 页以目标段开头（键 w0000000B）；新版前面插了一段，页首换成新段，旧键已不存在
        d = review_dir()
        nid = self.add(d, 2, "w0000000B", anchor("目标段", 2, pid="0000000B"))
        result = self.remap(d, para("甲", pid="0000000A") + PAGE_BR + para("新页首", pid="0000000C")
                            + para("目标段", pid="0000000B"), {2: "w0000000B"})
        n = self.note(d, nid)
        self.assertEqual((n["page"], n["key"], n["title"]), (2, "w0000000C", "新页首 目标段"))
        self.assertNotIn("orphan", n, "段落还在，不算孤立")
        self.assertEqual(n["docx"]["para"]["n"], 3)
        self.assertEqual(n["docx"]["fp"], self.NEW_FP, "段落文字没变：定位对当前版本有效")
        self.assertEqual(result["orphans"], [])

    def test_note_moves_with_pages_inserted_and_removed(self):
        d = review_dir()
        nid = self.add(d, 3, "w0000000D", anchor("目标段", 6, pid="0000000B"))
        result = self.remap(d, para("甲", pid="0000000A") + para("目标段", pid="0000000B"))
        n = self.note(d, nid)
        self.assertEqual(n["page"], 1)
        self.assertEqual([(m["from"], m["to"]) for m in result["moved"]], [(3, 1)])

    def test_missing_para_id_uses_digest_and_reports_it(self):
        d = review_dir()
        nid = self.add(d, 1, "tabc", anchor("目标段", 0))
        result = self.remap(d, para("前面插入") + PAGE_BR + para("目标段"))
        self.assertEqual(self.note(d, nid)["page"], 2)
        self.assertEqual([(x["id"], x["status"], x["how"], x["why"]) for x in result["docx"]],
                         [(nid, "ok", "digest", "noId")])

    def test_unprovable_paragraph_is_orphaned_without_moving(self):
        d = review_dir()
        nid = self.add(d, 2, "tabc", anchor("同上", 1))
        result = self.remap(d, para("同上") + para("插入") + para("甲") + para("同上"))
        n = self.note(d, nid)
        self.assertEqual((n["page"], n["orphan"], n["docx"]["fp"]), (2, True, self.OLD_FP))
        self.assertEqual([(x["status"], x["why"]) for x in result["docx"]], [("ambiguous", "noId")])
        self.assertEqual([o["id"] for o in result["orphans"]], [nid])

    def test_changed_paragraph_keeps_old_fingerprint_and_is_flagged(self):
        d = review_dir()
        nid = self.add(d, 1, "w0000000B", anchor("目标段", 0, pid="0000000B", start=0, end=2))
        self.remap(d, para("目标段已改写", pid="0000000B"))
        a = self.note(d, nid)["docx"]
        self.assertEqual((a["fp"], a["changed"]), (self.OLD_FP, True))

    def test_whole_page_note_still_uses_the_page_key(self):
        d = review_dir()
        nid = self.add(d, 1, "w0000000B", None)
        self.remap(d, para("甲", pid="0000000A") + PAGE_BR + para("目标段", pid="0000000B"), {1: "w0000000B"})
        self.assertEqual(self.note(d, nid)["page"], 2)

    def test_newer_anchor_version_is_left_alone_and_falls_back_to_page_key(self):
        d = review_dir()
        future = {"v": 2, "para": {"id": "0000000A", "n": 0, "digest": digest("甲")}}
        nid = store.add(d, {"page": 1, "key": "w0000000B", "text": "x", "docx": future})[1]["id"]
        self.remap(d, para("甲", pid="0000000A") + PAGE_BR + para("目标段", pid="0000000B"), {1: "w0000000B"})
        n = self.note(d, nid)
        self.assertEqual((n["page"], n["docx"]), (2, future))

    def test_second_remap_is_a_no_op(self):
        d = review_dir()
        self.add(d, 1, "tabc", anchor("目标段", 0))
        inner = para("前面插入") + PAGE_BR + para("目标段")
        self.remap(d, inner)
        rev = store.load(d)["rev"]
        self.remap(d, inner)
        self.assertEqual(store.load(d)["rev"], rev)


def build(report: Path, out: Path, locale: str = "zh-CN") -> tuple[int, str, str]:
    o, e = io.StringIO(), io.StringIO()
    with redirect_stdout(o), redirect_stderr(e):
        try:
            code = build_viewer.main(["--report", str(report), "--out", str(out), "--no-scan-skills", "--locale", locale])
        except SystemExit as exc:
            code = exc.code if isinstance(exc.code, int) else 1
            e.write(str(exc.code))
    return code, o.getvalue(), e.getvalue()


def manifest(out: Path) -> dict:
    return json.loads((out / "review.json").read_text(encoding="utf-8"))


class BuildRemapTests(unittest.TestCase):
    def test_review_json_and_page_record_the_source_fingerprint(self):
        src, out = docx(para("甲", pid="0000000A")), tmp() / "rv"
        self.assertEqual(build(src, out)[0], 0)
        fp = hashlib.sha256(src.read_bytes()).hexdigest()
        self.assertEqual(manifest(out)["docx"]["fingerprint"], fp)
        page = (out / "index.html").read_text(encoding="utf-8")
        self.assertIn(f'"fp": "{fp}"', page)
        self.assertIn('"anchor": 1', page, "页面知道自己写的定位字段版本")

    def test_rebuild_moves_anchored_note_with_its_paragraph_and_explains_digest_matching(self):
        src, out = docx(para("甲") + para("目标段")), tmp() / "rv"
        self.assertEqual(build(src, out)[0], 0)
        old = manifest(out)
        _doc, note = store.add(out, {"page": 1, "key": old["pages"][0]["key"], "text": "意见", "quote": "目标",
                                     "docx": {**anchor("目标段", 1, start=0, end=2), "fp": old["docx"]["fingerprint"]}})
        docx(para("新增的第一页") + PAGE_BR + para("甲") + para("目标段"), src)   # Word 里在前面加了一页
        code, _o, err = build(src, out)
        self.assertEqual(code, 0, err)
        new = manifest(out)
        got = next(n for n in store.load(out)["notes"] if n["id"] == note["id"])
        self.assertEqual((got["page"], got["key"], got.get("orphan")), (2, new["pages"][1]["key"], None))
        self.assertEqual((got["docx"]["para"]["n"], got["docx"]["fp"]), (3, new["docx"]["fingerprint"]))
        self.assertIn(note["id"], err)
        self.assertIn("没有 paraId", err, "缺 paraId 时在诊断里说明按序号与摘要互证")

    def test_rebuild_reports_paragraph_that_cannot_be_found(self):
        src, out = docx(para("甲") + para("目标段")), tmp() / "rv"
        self.assertEqual(build(src, out)[0], 0)
        _doc, note = store.add(out, {"page": 1, "text": "意见", "docx": anchor("目标段", 1)})
        docx(para("甲") + para("完全不同"), src)
        _code, _o, err = build(src, out, "en")
        got = next(n for n in store.load(out)["notes"] if n["id"] == note["id"])
        self.assertTrue(got["orphan"])
        self.assertIn("paragraph", err)
        self.assertIn(note["id"], err)


FULL = {"v": 1, "kind": "quote", "via": "text", "fp": "ab" * 32,
        "para": {"id": "0000000B", "n": 12, "digest": "0123456789ab"}, "start": 5, "end": 18,
        "endPara": {"id": None, "n": 13, "digest": "ba9876543210"}, "run": 2, "changed": True}


class ExportTests(unittest.TestCase):
    """导出 Markdown 中英两种文法都带 DOCX 定位，导入后字段还原。"""

    def notes(self, a: dict) -> list[dict]:
        return [{"id": "n1", "page": 3, "title": "条款", "target": "报告", "text": "改措辞", "status": "open",
                 "at": "2026-01-01T00:00:00+08:00", "quote": "原文", "quoteFrom": "报告", "pin": None, "docx": a}]

    def test_both_languages_carry_the_location(self):
        zh = ef.render(self.notes(FULL), title="合成", lang="zh-CN")
        en = ef.render(self.notes(FULL), title="合成", lang="en")
        self.assertIn("   DOCX 定位：v1 引用 · 段落 12（paraId 0000000B，摘要 0123456789ab） · 字符 5–18"
                      " · 止于段落 13（paraId -，摘要 ba9876543210） · 运行 2 · 来源 文本匹配 · 指纹 " + "ab" * 32
                      + " · 段落已改动", zh)
        self.assertIn("   DOCX location: v1 quote · paragraph 12 (paraId 0000000B, digest 0123456789ab) · chars 5–18"
                      " · ends in paragraph 13 (paraId -, digest ba9876543210) · run 2 · source text match"
                      " · fingerprint " + "ab" * 32 + " · paragraph changed", en)

    def test_round_trip_restores_the_anchor(self):
        pin = {"v": 1, "kind": "pin", "via": "point", "para": {"id": None, "n": 0, "digest": "0123456789ab"}}
        for lang in ("zh-CN", "en"):
            for a in (FULL, pin):
                with self.subTest(lang=lang, kind=a["kind"]):
                    items, _always = ef.parse_markdown(ef.render(self.notes(a), title="合成", lang=lang))
                    self.assertEqual(items[0]["docx"], a)
                    self.assertEqual(items[0]["text"], "改措辞", "定位行不并进正文")

    def test_newer_version_is_named_but_not_parsed_back(self):
        for lang, word in (("zh-CN", "DOCX 定位：v2"), ("en", "DOCX location: v2")):
            text = ef.render(self.notes({"v": 2, "x": 1}), title="合成", lang=lang)
            self.assertIn(word, text)
            items, _always = ef.parse_markdown(text)
            self.assertNotIn("docx", items[0])
            self.assertEqual(items[0]["text"], "改措辞")

    def test_notes_without_anchor_export_as_before(self):
        note = self.notes(FULL)[0]
        del note["docx"]
        self.assertNotIn("DOCX", ef.render([note], title="合成", lang="zh-CN"))


class ShowTests(unittest.TestCase):
    """notes.py show / wait 的阅读输出带 DOCX 定位，并说明定位是否对着当前版本。"""

    MANIFEST = {"title": "合成", "pages": [{"n": 3, "key": "w0000000B", "title": "条款"}],
                "docx": {"fingerprint": "cd" * 32}}

    def show(self, a: dict, lang: str) -> str:
        doc = {"rev": 1, "notes": [{"id": "n1", "page": 3, "key": "w0000000B", "target": "报告", "text": "改措辞",
                                    "status": "open", "at": "2026-01-01T00:00:00+08:00", "docx": a}]}
        return notes_cli.to_markdown(doc, self.MANIFEST, include_resolved=True, page=None, locale=lang)

    def test_location_line_in_both_languages(self):
        current = {k: v for k, v in FULL.items() if k not in ("changed", "endPara")} | {"fp": "cd" * 32}
        zh, en = self.show(current, "zh-CN"), self.show(current, "en")
        self.assertIn("DOCX 定位：段落序号 12（paraId 0000000B）", zh)
        self.assertIn("字符 5–18", zh)
        self.assertIn("按文本唯一匹配反查", zh)
        self.assertIn("DOCX location: paragraph 12 (paraId 0000000B)", en)
        self.assertIn("characters 5–18", en)
        self.assertNotIn("⚠", zh, "对着当前版本写的定位不带警告")

    def test_changed_and_outdated_anchors_are_flagged(self):
        self.assertIn("段落文字已改动", self.show(FULL, "zh-CN"))
        self.assertIn("paragraph text has changed", self.show(FULL, "en"))
        old = {k: v for k, v in FULL.items() if k != "changed"}
        self.assertIn("另一个版本", self.show(old, "zh-CN"))
        self.assertIn("another version", self.show(old, "en"))

    def test_newer_version_is_marked_unusable_for_writeback(self):
        self.assertIn("不可用于回写", self.show({"v": 2, "x": 1}, "zh-CN"))
        self.assertIn("cannot be used for write-back", self.show({"v": 2, "x": 1}, "en"))


if __name__ == "__main__":
    unittest.main()
