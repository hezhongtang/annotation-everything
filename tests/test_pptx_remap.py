"""PPTX 重建重映射、演讲者备注与批注交换（#150）。

fixture 全部现场合成（fixtures/pptx/make_pptx.py 的变体），不入库真实材料。只用标准库：
python3 -m unittest tests.test_pptx_remap
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
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "skill" / "annotation-everything" / "scripts"
sys.path.insert(0, str(SCRIPTS))
sys.path.insert(0, str(ROOT / "fixtures" / "pptx"))
os.environ.setdefault("AE_HOME", tempfile.mkdtemp(prefix="ae-test-home-"))   # 测试不碰用户真实的审阅台账
os.environ.setdefault("HOME", tempfile.mkdtemp(prefix="ae-test-user-"))

import build_viewer  # noqa: E402
import export_format as ef  # noqa: E402
import make_pptx as mp  # noqa: E402
import notes as notes_cli  # noqa: E402
import notes_store as store  # noqa: E402
import pptx_reader as pr  # noqa: E402

FP = "ab" * 32
OLD_FP, NEW_FP = "0" * 64, "1" * 64
PNG_1PX = (b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x06"
           b"\x00\x00\x00\x1f\x15\xc4\x89\x00\x00\x00\nIDATx\x9cc\x00\x01\x00\x00\x05\x00\x01"
           b"\x0d\n-\xb4\x00\x00\x00\x00IEND\xaeB`\x82")


def tmp() -> Path:
    return Path(tempfile.mkdtemp(prefix="ae-pptx-remap-"))


def deck(parts: dict, name: str = "deck.pptx") -> Path:
    return mp.write_package(tmp() / name, parts)


# ---------- 合成变体：重排、重命名、文本变化、run 重拆、删除、重复形状标识 ----------
SldLST = '<p:sldIdLst><p:sldId id="260" r:id="rIdSecond"/><p:sldId id="257" r:id="rIdFirst"/></p:sldIdLst>'


def reorder(parts: dict) -> dict:
    out = dict(parts)
    swapped = SldLST.replace('id="260" r:id="rIdSecond"/><p:sldId id="257" r:id="rIdFirst"',
                             'id="257" r:id="rIdFirst"/><p:sldId id="260" r:id="rIdSecond"')
    assert swapped != SldLST
    out["ppt/presentation.xml"] = parts["ppt/presentation.xml"].replace(SldLST, swapped)
    return out


def drop_slide(parts: dict, slide_id: str = "260") -> dict:
    out = dict(parts)
    entry = next(s for s in ('<p:sldId id="260" r:id="rIdSecond"/>', '<p:sldId id="257" r:id="rIdFirst"/>')
                 if f'id="{slide_id}"' in s)
    out["ppt/presentation.xml"] = parts["ppt/presentation.xml"].replace(entry, "")
    return out


def rename_element(parts: dict, old: str, new: str) -> dict:
    out = dict(parts)
    hit = [k for k, v in parts.items() if isinstance(v, str) and f'name="{old}"' in v]
    assert len(hit) == 1, hit
    out[hit[0]] = parts[hit[0]].replace(f'name="{old}"', f'name="{new}"')
    return out


def drop_notes_rel(parts: dict) -> dict:
    out = dict(parts)
    out["ppt/slides/_rels/slide10.xml.rels"] = parts["ppt/slides/_rels/slide10.xml.rels"].replace(
        '<Relationship Id="rIdNotes" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships'
        '/notesSlide" Target="../notesSlides/notesSlide1.xml"/>', "")
    return out


ORIGINAL_PIC = ('<p:pic><p:nvPicPr><p:cNvPr id="11" name="合成图片" descr="合成图片替代文本"/><p:cNvPicPr/><p:nvPr/></p:nvPicPr>'
                '<p:blipFill><a:blip r:embed="rIdImage"/></p:blipFill>'
                '<p:spPr><a:xfrm><a:off x="1000000" y="0"/><a:ext cx="1000000" cy="1000000"/></a:xfrm></p:spPr></p:pic>')


def duplicate_picture_id(parts: dict) -> dict:
    """在元素页再放一个同 id 的图片（名称与几何不同）：形状标识撞车，只能靠证据消歧。"""
    out = dict(parts)
    clone = ('<p:pic><p:nvPicPr><p:cNvPr id="11" name="重复图片"/><p:cNvPicPr/><p:nvPr/></p:nvPicPr>'
             '<p:blipFill><a:blip r:embed="rIdImage"/></p:blipFill>'
             '<p:spPr><a:xfrm><a:off x="3000000" y="0"/><a:ext cx="500000" cy="500000"/></a:xfrm></p:spPr></p:pic>')
    assert ORIGINAL_PIC in parts["ppt/slides/slide2.xml"]
    out["ppt/slides/slide2.xml"] = parts["ppt/slides/slide2.xml"].replace(ORIGINAL_PIC, ORIGINAL_PIC + clone)
    return out


def para_digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def slide_of(model: dict, key: str) -> dict:
    return next(s for s in model["slides"] if s["key"] == key)


def element_of(model: dict, slide_key: str, key: str) -> dict:
    return next(e for e in slide_of(model, slide_key)["elements"] if e["key"] == key)


def target_of(e: dict) -> dict:
    """页面写入批注时的元素目标（与 viewer_template 的 pptxTargetOf 同一套语义）。"""
    out = {"key": e["key"], "id": str(e.get("id") or ""), "name": e.get("name") or "", "kind": e["kind"],
           "z": e.get("z", 0), "digest": e.get("evidenceDigest") or ""}
    ro = [k for k in ("text", "geometry", "style") if not e["capabilities"][k + "Write"]]
    if ro:
        out["ro"] = ro
    return out


def anchor(kind: str, via: str, slide: str = "s260", *, elements: list | None = None,
           text: dict | None = None, point: dict | None = None, region: dict | None = None,
           fp: str | None = OLD_FP) -> dict:
    out = {"v": 1, "slide": slide, "kind": kind, "via": via}
    if fp:
        out["fp"] = fp
    if elements is not None:
        out["elements"] = elements
    if text is not None:
        out["text"] = text
    if point is not None:
        out["point"] = point
    if region is not None:
        out["region"] = region
    return out


class RelocateTests(unittest.TestCase):
    """重建后按幻灯片定位找回目标：读取层的公开重定位入口（与 docx_review.relocate 并列）。"""

    def read(self, parts: dict) -> dict:
        return pr.read(deck(parts))

    def test_slide_reorder_follows_the_stable_slide_key(self):
        got = pr.relocate(anchor("page", "thumb"), self.read(reorder(mp.sample_parts()))["slides"])
        self.assertEqual((got["status"], got["page"]), ("ok", 2))

    def test_point_and_region_anchors_follow_the_slide_only(self):
        model = self.read(reorder(mp.sample_parts()))
        for a in (anchor("point", "point", point={"x": 0.5, "y": 0.1}),
                  anchor("region", "region", region={"x": 0.1, "y": 0.1, "w": 0.2, "h": 0.2})):
            with self.subTest(kind=a["kind"]):
                got = pr.relocate(a, model["slides"])
                self.assertEqual((got["status"], got["page"]), ("ok", 2))

    def test_deleted_slide_is_missing(self):
        got = pr.relocate(anchor("page", "thumb"), self.read(drop_slide(mp.sample_parts()))["slides"])
        self.assertEqual(got["status"], "missing")
        self.assertIsNone(got["page"])

    def element_anchor(self, parts: dict, slide_key: str = "s257", keys: tuple[str, ...] = ("e11",)) -> tuple[dict, dict]:
        """从原版模型取元素目标写成定位，返回 (定位, 新版模型)。"""
        original = pr.read(deck(mp.sample_parts(), "origin.pptx"))
        elements = [target_of(element_of(original, slide_key, k)) for k in keys]
        return anchor("elements", "click", slide_key, elements=elements), self.read(parts)

    def test_rename_and_zorder_change_follow_by_shape_id(self):
        parts = reorder(rename_element(mp.sample_parts(), "合成图片", "重命名的图片"))
        a, model = self.element_anchor(parts)
        got = pr.relocate(a, model["slides"])
        self.assertEqual((got["status"], got["how"]), ("ok", "shapeId"))
        self.assertEqual(got["targets"][0]["key"], "e11")
        self.assertEqual(got["targets"][0]["name"], "重命名的图片", "身份不变，目标信息刷新成当前版本")
        self.assertEqual(got["targets"][0]["digest"],
                         element_of(model, "s257", "e11")["evidenceDigest"])

    def test_multi_element_targets_keep_order(self):
        a, model = self.element_anchor(reorder(mp.sample_parts()), keys=("e11", "e12"))
        got = pr.relocate(a, model["slides"])
        self.assertEqual([t["key"] for t in got["targets"]], ["e11", "e12"], "多元素批注保持选择顺序")

    def test_duplicate_shape_id_is_disambiguated_by_evidence(self):
        a, model = self.element_anchor(duplicate_picture_id(mp.sample_parts()))
        got = pr.relocate(a, model["slides"])
        self.assertEqual((got["status"], got["how"], got["why"]), ("ok", "evidence", "duplicateShapeId"))
        self.assertEqual(got["targets"][0]["key"], "e11")

    def test_duplicate_shape_id_with_conflicting_evidence_is_ambiguous(self):
        parts = rename_element(duplicate_picture_id(mp.sample_parts()), "合成图片", "同名重命名后证据对不上")
        a, model = self.element_anchor(parts)
        got = pr.relocate(a, model["slides"])
        self.assertEqual((got["status"], got["why"]), ("ambiguous", "duplicateShapeId"),
                         "标识重复且证据对不上：宁可标需确认，不吸附相近对象")

    def test_deleted_element_is_missing(self):
        parts = mp.sample_parts()
        removed = parts["ppt/slides/slide2.xml"].replace(ORIGINAL_PIC, "")
        assert removed != parts["ppt/slides/slide2.xml"]
        parts["ppt/slides/slide2.xml"] = removed
        a, model = self.element_anchor(parts)
        got = pr.relocate(a, model["slides"])
        self.assertEqual((got["status"], got["page"]), ("missing", None), "原对象已删除：不猜别的元素")

    def text_anchor(self, parts: dict, first_para: str | None = None) -> tuple[dict, dict]:
        """对着原版标题第一段「相同😀&<」的选区定位；first_para 给出新版第一段文字。"""
        original = pr.read(deck(mp.sample_parts(), "origin.pptx"))
        el = element_of(original, "s260", "e2")
        p = el["paragraphs"][0]
        body_parts = mp.sample_parts()
        if first_para is not None:
            body_parts["ppt/slides/slide10.xml"] = mp.slide_xml(
                paragraphs=f'<a:p>{mp.text_run(first_para)}</a:p><a:p>{mp.text_run("相同")}</a:p>')
            parts = {**body_parts, **{k: v for k, v in parts.items() if k != "ppt/slides/slide10.xml"}}
        a = anchor("text", "selection", "s260", elements=[target_of(el)],
                   text={"para": 0, "paraDigest": p["digest"], "quote": "相同😀", "start": 0, "end": 3,
                         "runs": [{"start": r["start"], "end": r["end"]} for r in p["runs"]]})
        return a, self.read(parts)

    def test_run_resplit_keeps_the_selection(self):
        """run 重新拆分只改样式边界：逻辑段落文字不变，定位原样有效。"""
        parts = mp.sample_parts()
        parts["ppt/slides/slide10.xml"] = mp.slide_xml(
            paragraphs=f'<a:p>{mp.text_run("相同😀&<")}</a:p><a:p>{mp.text_run("相同")}</a:p>')
        a, model = self.text_anchor(parts)
        got = pr.relocate(a, model["slides"])
        self.assertEqual((got["status"], got["changed"]), ("ok", False))
        self.assertEqual(got["text"], {"para": 0, "changed": False})

    def test_minor_text_change_follows_the_quote_and_flags_changed(self):
        a, model = self.text_anchor(mp.sample_parts(), first_para="相同😀&<，补充一句")
        got = pr.relocate(a, model["slides"])
        self.assertEqual((got["status"], got["changed"]), ("ok", True), "元素身份不变，引用仍在：跟随并标记文字已变")
        self.assertEqual(got["text"], {"para": 0, "changed": True})

    def test_quote_gone_is_missing_not_attached_to_similar_text(self):
        a, model = self.text_anchor(mp.sample_parts(), first_para="完全不同的文字")
        got = pr.relocate(a, model["slides"])
        self.assertEqual((got["status"], got["why"], got["page"]), ("missing", "textGone", None),
                         "引用原文消失：选区已删，不猜相近文字")

    def test_paragraph_inserted_before_moves_the_index(self):
        parts = mp.sample_parts()
        # run 属性提到 f-string 外定义：3.9 及更早不允许表达式里出现反斜杠或同种引号
        marked_rpr = '<a:rPr b="1"/>'
        parts["ppt/slides/slide10.xml"] = mp.slide_xml(
            paragraphs=f'<a:p>{mp.text_run("新插入段")}</a:p><a:p>{mp.text_run("相同", marked_rpr)}'
                       + mp.text_run("😀&<", "<a:rPr i=\"1\"/>") + f'</a:p><a:p>{mp.text_run("相同")}</a:p>')
        a, model = self.text_anchor(parts)
        got = pr.relocate(a, model["slides"])
        self.assertEqual((got["status"], got["changed"]), ("ok", False))
        self.assertEqual(got["text"]["para"], 1, "段落摘要唯一命中：逻辑段落序号跟上")


def review_dir() -> Path:
    d = tmp()
    (d / "review.json").write_text("{}", encoding="utf-8")
    return d


class PptxRemapTests(unittest.TestCase):
    """重建对照页：带幻灯片定位的批注先按定位找回，页面键只管没有定位的批注（与 DOCX 同层并列）。"""

    TITLES = {1: "标题一", 2: "标题二"}

    def remap(self, d: Path, parts: dict, old_keys: dict[int, str] | None = None) -> dict:
        model = pr.read(deck(parts, "rebuilt.pptx"))
        keys = {s["n"]: s["key"] for s in model["slides"]}
        return store.remap_pages(d, old_keys or {}, {k: n for n, k in keys.items()}, dict(self.TITLES),
                                 pptx={"slides": model["slides"], "fingerprint": NEW_FP, "keys": keys})

    def add(self, d: Path, page: int, a: dict | None, key: str = "s260") -> str:
        data = {"page": page, "key": key, "title": "旧标题", "text": "意见"}
        if a is not None:
            data["pptx"] = a
        return store.add(d, data)[1]["id"]

    def note(self, d: Path, nid: str) -> dict:
        return next(n for n in store.load(d)["notes"] if n["id"] == nid)

    def test_note_follows_slide_reorder_with_page_key_and_title(self):
        d = review_dir()
        nid = self.add(d, 1, anchor("page", "thumb"))
        result = self.remap(d, reorder(mp.sample_parts()), {1: "s260"})
        n = self.note(d, nid)
        self.assertEqual((n["page"], n["key"], n["title"]), (2, "s260", "标题二"))
        self.assertNotIn("orphan", n, "幻灯片还在，不算孤立")
        self.assertEqual(n["pptx"]["fp"], NEW_FP, "源文件换了版本：定位对当前版本有效")
        self.assertEqual([(m["from"], m["to"]) for m in result["moved"]], [(1, 2)])

    def test_fingerprint_change_alone_refreshes_the_anchor(self):
        d = review_dir()
        a = anchor("elements", "click", "s257", elements=[target_of(
            element_of(pr.read(deck(mp.sample_parts(), "o.pptx")), "s257", "e11"))])
        nid = self.add(d, 2, a, key="s257")
        self.remap(d, mp.sample_parts(), {2: "s257"})
        n = self.note(d, nid)
        self.assertEqual((n["page"], n["pptx"]["fp"]), (2, NEW_FP), "只有指纹变化：跟随并刷新指纹")

    def test_deleted_slide_orphans_without_moving(self):
        d = review_dir()
        nid = self.add(d, 1, anchor("page", "thumb"))
        result = self.remap(d, drop_slide(mp.sample_parts()), {1: "s260"})
        n = self.note(d, nid)
        self.assertEqual((n["page"], n.get("orphan"), n["pptx"]["fp"]), (1, True, OLD_FP),
                         "幻灯片已删：页码不动、定位保持旧版本，等人工确认")
        self.assertEqual([(o["id"], o.get("reason")) for o in result["orphans"]], [(nid, "slideGone")])

    def test_deleted_element_orphans_with_reason(self):
        original = pr.read(deck(mp.sample_parts(), "o.pptx"))
        a = anchor("elements", "click", "s257", elements=[target_of(element_of(original, "s257", "e11"))])
        d = review_dir()
        nid = self.add(d, 2, a, key="s257")
        parts = mp.sample_parts()
        parts["ppt/slides/slide2.xml"] = parts["ppt/slides/slide2.xml"].replace(ORIGINAL_PIC, "")
        result = self.remap(d, parts, {2: "s257"})
        n = self.note(d, nid)
        self.assertTrue(n.get("orphan"))
        self.assertEqual([(o["id"], o.get("reason")) for o in result["orphans"]], [(nid, "elementGone")])

    def test_changed_text_anchor_keeps_old_fingerprint_and_is_flagged(self):
        original = pr.read(deck(mp.sample_parts(), "o.pptx"))
        el = element_of(original, "s260", "e2")
        p = el["paragraphs"][0]
        a = anchor("text", "selection", "s260", elements=[target_of(el)],
                   text={"para": 0, "paraDigest": p["digest"], "quote": "相同😀", "start": 0, "end": 3,
                         "runs": [{"start": r["start"], "end": r["end"]} for r in p["runs"]]})
        d = review_dir()
        nid = self.add(d, 1, a)
        parts = mp.sample_parts()
        parts["ppt/slides/slide10.xml"] = mp.slide_xml(
            paragraphs=f'<a:p>{mp.text_run("相同😀&<，补充一句")}</a:p><a:p>{mp.text_run("相同")}</a:p>')
        result = self.remap(d, parts)
        n = self.note(d, nid)
        self.assertEqual((n["pptx"]["fp"], n["pptx"].get("changed")), (OLD_FP, True),
                         "文字已变：偏移与指纹仍是旧版本的，不冒充当前版本")
        self.assertEqual([x["changed"] for x in result["pptx"]], [True])

    def test_ambiguous_element_is_orphaned_not_attached(self):
        original = pr.read(deck(mp.sample_parts(), "o.pptx"))
        a = anchor("elements", "click", "s257", elements=[target_of(element_of(original, "s257", "e11"))])
        d = review_dir()
        nid = self.add(d, 2, a, key="s257")
        result = self.remap(d, rename_element(duplicate_picture_id(mp.sample_parts()),
                                              "合成图片", "重命名后证据对不上"), {2: "s257"})
        self.assertEqual([(o["id"], o.get("reason")) for o in result["orphans"]], [(nid, "duplicateShapeId")])

    def test_newer_anchor_version_falls_back_to_the_page_key(self):
        d = review_dir()
        future = {"v": 2, "slide": "s260", "deck": {"new": [1]}}
        nid = store.add(d, {"page": 1, "key": "s260", "text": "x", "pptx": future})[1]["id"]
        self.remap(d, reorder(mp.sample_parts()), {1: "s260"})
        n = self.note(d, nid)
        self.assertEqual((n["page"], n["pptx"]), (2, future), "读不懂的版本原样保留，按页面键找回")

    def test_note_without_anchor_still_uses_the_page_key(self):
        d = review_dir()
        nid = self.add(d, 1, None)
        self.remap(d, reorder(mp.sample_parts()), {1: "s260"})
        self.assertEqual(self.note(d, nid)["page"], 2)

    def test_user_confirmation_survives_the_next_rebuild(self):
        d = review_dir()
        nid = self.add(d, 1, anchor("page", "thumb"))
        parts = drop_slide(mp.sample_parts())
        self.remap(d, parts, {1: "s260"})
        self.assertTrue(self.note(d, nid)["orphan"])
        store.update(d, nid, {"page": 1, "key": "s257", "orphan": False})
        result = self.remap(d, parts, {1: "s257"})
        n = self.note(d, nid)
        self.assertEqual((n["page"], result["orphans"]), (1, []), "用户已确认归属，不再作为孤立批注处理")

    def test_second_remap_is_a_no_op(self):
        d = review_dir()
        self.add(d, 1, anchor("page", "thumb"))
        parts = reorder(mp.sample_parts())
        self.remap(d, parts, {1: "s260"})
        rev = store.load(d)["rev"]
        self.remap(d, parts, {1: "s260"})
        self.assertEqual(store.load(d)["rev"], rev)


def build(source: Path, out: Path, *extra: str) -> tuple[int, str, str]:
    """真实 build_viewer 构建（渲染注入受控 PNG，页数随源文件）。"""
    import pptx_report
    o, e = io.StringIO(), io.StringIO()
    pages = [PNG_1PX] * len(pr.read(source)["slides"])
    with redirect_stdout(o), redirect_stderr(e), \
            mock.patch.object(pptx_report, "render_slides", lambda p, w: pages):
        try:
            code = build_viewer.main(["--report", str(source), "--out", str(out), "--no-scan-skills",
                                      "--locale", "zh-CN", *extra])
        except SystemExit as exc:
            code = exc.code if isinstance(exc.code, int) else 1
            e.write(str(exc.code))
    return code, o.getvalue(), e.getvalue()


def manifest(out: Path) -> dict:
    return json.loads((out / "review.json").read_text(encoding="utf-8"))


def viewer_pages(out: Path) -> list[dict]:
    """构建出的 index.html 里页面常量 P（讲稿侧内容也在其中）。"""
    text = (out / "index.html").read_text(encoding="utf-8")
    start = text.find("const P=")
    assert start > 0, "index.html 缺页面常量"
    return json.JSONDecoder().raw_decode(text, start + len("const P="))[0]


class PptxBuildRemapTests(unittest.TestCase):
    """公开构建入口的重建重映射：构建 → 批注 → 改源文件 → 重建。"""

    def test_rebuild_moves_anchored_note_and_keeps_the_review_id(self):
        source, out = deck(mp.sample_parts(), "src.pptx"), tmp() / "rv"
        self.assertEqual(build(source, out)[0], 0)
        rid = manifest(out)["reviewId"]
        _doc, note = store.add(out, {"page": 1, "key": "s260", "text": "整页意见",
                                     "pptx": anchor("page", "thumb")})
        mp.write_package(source, reorder(mp.sample_parts()))
        code, _o, err = build(source, out)
        self.assertEqual(code, 0, err)
        got = next(n for n in store.load(out)["notes"] if n["id"] == note["id"])
        self.assertEqual((got["page"], got.get("orphan")), (2, None))
        self.assertEqual(got["pptx"]["fp"], manifest(out)["pptx"]["fingerprint"])
        self.assertEqual(manifest(out)["reviewId"], rid, "同一目录原地重建：审阅标识保留")
        self.assertIn(note["id"], err)
        self.assertIn("从第 1 页移到第 2 页", err)

    def test_rebuild_orphans_note_on_deleted_slide(self):
        source, out = deck(mp.sample_parts(), "src.pptx"), tmp() / "rv"
        self.assertEqual(build(source, out)[0], 0)
        _doc, note = store.add(out, {"page": 1, "key": "s260", "text": "意见",
                                     "pptx": anchor("page", "thumb")})
        mp.write_package(source, drop_slide(mp.sample_parts()))
        code, _o, err = build(source, out)
        self.assertEqual(code, 0, err)
        got = next(n for n in store.load(out)["notes"] if n["id"] == note["id"])
        self.assertEqual((got["page"], got.get("orphan")), (1, True))
        self.assertIn(note["id"], err)
        self.assertIn("幻灯片定位", err)


class SpeakerNotesTests(unittest.TestCase):
    """演讲者备注与讲稿侧：无外部讲稿逐页进讲稿侧；显式外部讲稿优先且不静默合并。"""

    def test_notes_fill_the_script_side_per_slide(self):
        source, out = deck(mp.sample_parts(), "src.pptx"), tmp() / "rv"
        code, _o, err = build(source, out)
        self.assertEqual(code, 0, err)
        pages = viewer_pages(out)
        self.assertEqual([p["n"] for p in pages], [1, 2])
        self.assertIn("合成备注😀", pages[0]["talk"])
        self.assertIn("第二段", pages[0]["talk"])
        self.assertNotIn("合成备注", pages[1]["talk"], "第二张幻灯片没有备注，讲稿侧为空")
        self.assertIn("演讲者备注", err, "构建时说明备注进了讲稿侧")

    def test_deck_without_notes_stays_report_only(self):
        source, out = deck(drop_notes_rel(mp.sample_parts()), "src.pptx"), tmp() / "rv"
        code, _o, err = build(source, out)
        self.assertEqual(code, 0, err)
        self.assertEqual(viewer_pages(out)[0]["talk"], "", "没有备注的 PPT 只审阅报告，讲稿侧为空")
        self.assertNotIn("演讲者备注", err)

    def test_external_script_takes_precedence_and_warns_notes_not_merged(self):
        script = tmp() / "讲稿.md"
        script.write_text("### 第 1 页｜甲\n**讲法**\n外部讲稿一句\n\n### 第 2 页｜乙\n**讲法**\n第二页讲法\n",
                          encoding="utf-8")
        source, out = deck(mp.sample_parts(), "src.pptx"), tmp() / "rv"
        code, _o, err = build(source, out, "--script", str(script))
        self.assertEqual(code, 0, err)
        pages = viewer_pages(out)
        self.assertIn("外部讲稿一句", pages[0]["talk"])
        self.assertNotIn("合成备注😀", pages[0]["talk"], "PPT 备注不与外部讲稿合并")
        self.assertIn("未合并", err)
        self.assertIn("不是回写目标", err)


SHOW_EL = anchor("elements", "click", "s257", fp="cd" * 32, elements=[
    {"key": "e11", "id": "11", "name": "合成图片", "kind": "picture", "z": 1, "digest": "d" * 64,
     "ro": ["text", "geometry", "style"]},
    {"key": "e12", "id": "12", "name": "合成组合", "kind": "group", "z": 2, "digest": "e" * 64}])
SHOW_TEXT = anchor("text", "selection", "s260", fp="cd" * 32, elements=[
    {"key": "e2", "id": "2", "name": "合成标题", "kind": "shape", "z": 0, "digest": "f" * 64}],
    text={"para": 0, "paraDigest": "a" * 64, "start": 1, "end": 4, "quote": "相同😀",
          "runs": [{"start": 0, "end": 2}, {"start": 2, "end": 4}]})


class ShowTests(unittest.TestCase):
    """notes.py show / wait 的阅读输出带幻灯片定位：版本、身份、文本或区域、指纹与限制。"""

    MANIFEST = {"title": "合成", "pages": [{"n": 2, "key": "s257", "title": "元素页"}],
                "pptx": {"fingerprint": "cd" * 32}}

    def show(self, a: dict, lang: str, **extra: object) -> str:
        note = {"id": "n1", "page": 2, "key": "s257", "target": "报告", "text": "改措辞", "status": "open",
                "at": "2026-01-01T00:00:00+08:00", "pptx": a}
        note.update(extra)
        doc = {"rev": 1, "notes": [note]}
        return notes_cli.to_markdown(doc, self.MANIFEST, include_resolved=True, page=None, locale=lang)

    def test_location_lines_in_both_languages(self):
        zh, en = self.show(SHOW_EL, "zh-CN"), self.show(SHOW_EL, "en")
        self.assertIn("幻灯片定位 v1", zh)
        self.assertIn("幻灯片 s257", zh)
        self.assertIn("合成图片", zh)
        self.assertIn("标识 11", zh)
        self.assertIn("只读限制", zh)
        self.assertIn("Slide location v1", en)
        self.assertIn("slide s257", en)
        self.assertIn("shape id 11", en)
        self.assertIn("read-only", en)
        self.assertNotIn("⚠", zh, "对着当前版本写的定位不带警告")

    def test_text_selection_point_and_region_are_readable(self):
        text_zh, text_en = self.show(SHOW_TEXT, "zh-CN"), self.show(SHOW_TEXT, "en")
        self.assertIn("第 1 段", text_zh)
        self.assertIn("字符 1–4", text_zh)
        self.assertIn("相同😀", text_zh)
        self.assertIn("paragraph 1", text_en)
        self.assertIn("chars 1–4", text_en)
        region = anchor("region", "region", fp="cd" * 32, region={"x": 0.25, "y": 0.1, "w": 0.3, "h": 0.2})
        point = anchor("point", "point", fp="cd" * 32, point={"x": 0.5, "y": 0.1})
        self.assertIn("x 25%", self.show(region, "zh-CN"))
        self.assertIn("宽 30%", self.show(region, "zh-CN"))
        self.assertIn("x 25%", self.show(region, "en"))
        self.assertIn("width 30%", self.show(region, "en"))
        self.assertIn("x 50%", self.show(point, "en"))

    def test_changed_outdated_and_orphaned_locations_are_flagged(self):
        changed = dict(SHOW_TEXT, changed=True)
        self.assertIn("文字已改动", self.show(changed, "zh-CN"))
        self.assertIn("text has changed", self.show(changed, "en"))
        stale = {k: v for k, v in SHOW_TEXT.items() if k not in ("changed", "fp")}
        self.assertIn("另一个版本", self.show(stale, "zh-CN"))
        self.assertIn("another version", self.show(stale, "en"))
        orphaned = self.show(SHOW_EL, "zh-CN", orphan=True)
        self.assertIn("先向用户确认", orphaned)

    def test_newer_version_is_marked_unusable_for_writeback(self):
        self.assertIn("不可用于回写", self.show({"v": 2, "slide": "s260", "x": 1}, "zh-CN"))
        self.assertIn("cannot be used for write-back", self.show({"v": 2, "slide": "s260", "x": 1}, "en"))


class ExchangeTests(unittest.TestCase):
    """批注交换的 PPT 定位：导出与导入同一版本化格式，中英文法都完整还原；同文不同元素不误去重。"""

    PAGE = anchor("page", "thumb", "s260", fp="ab" * 32)
    ELEMENTS = anchor("elements", "click", "s257", fp="ab" * 32, elements=[
        {"key": "e11", "id": "11", "name": "合成图片", "kind": "picture", "z": 1, "digest": "d" * 64,
         "ro": ["text", "geometry", "style"]},
        {"key": "e12", "id": "12", "name": "合成组合", "kind": "group", "z": 2, "digest": "e" * 64}])
    TEXT = anchor("text", "selection", "s260", fp="ab" * 32, elements=[
        {"key": "e2", "id": "2", "name": "合成标题", "kind": "shape", "z": 0, "digest": "f" * 64}],
        text={"para": 0, "paraDigest": "a" * 64, "start": 1, "end": 4, "quote": "相同😀",
              "runs": [{"start": 0, "end": 2}, {"start": 2, "end": 4}]})
    POINT = anchor("point", "point", "s260", fp="ab" * 32, point={"x": 0.5, "y": 0.1})
    REGION = anchor("region", "region", "s260", fp="ab" * 32, region={"x": 0.25, "y": 0.1, "w": 0.3, "h": 0.2})
    CHANGED = dict(TEXT, changed=True)

    def notes(self, a: dict) -> list[dict]:
        return [{"id": "n1", "page": 2, "title": "元素页", "target": "报告", "text": "改措辞", "status": "open",
                 "at": "2026-01-01T00:00:00+08:00", "pptx": a}]

    @staticmethod
    def portable(a: dict) -> dict:
        """交换行不携带展示名（元素名会变、不是身份，重定位时刷新成当前版本；与 DOCX 段落定位
        不携带段落文本同理）：比对往返结果前把 name 清成空串。"""
        out = dict(a)
        if a.get("elements"):
            out["elements"] = [{**e, "name": ""} for e in a["elements"]]
        return out

    def test_both_languages_carry_the_location(self):
        zh = ef.render(self.notes(self.ELEMENTS), title="合成", lang="zh-CN")
        en = ef.render(self.notes(self.ELEMENTS), title="合成", lang="en")
        self.assertIn("   PPT 定位：v1 元素 · 幻灯片 s257"
                      " · 目标 e11（标识 11，picture，第 1 层，摘要 " + "d" * 64 + "，只读 文本、几何、样式）"
                      "、e12（标识 12，group，第 2 层，摘要 " + "e" * 64 + "）"
                      " · 来源 点选 · 指纹 " + "ab" * 32, zh)
        self.assertIn("   PPT location: v1 elements · slide s257"
                      " · targets e11 (id 11, picture, z 1, digest " + "d" * 64 + ", read-only text, geometry, style)"
                      "; e12 (id 12, group, z 2, digest " + "e" * 64 + ")"
                      " · source click · fingerprint " + "ab" * 32, en)
        text = ef.render(self.notes(self.TEXT), title="合成", lang="zh-CN")
        self.assertIn(" · 段落 0（摘要 " + "a" * 64 + "） · 字符 1–4 · 运行 0–2、2–4 · 来源 选区"
                      " · 指纹 " + "ab" * 32 + " · 引用 相同😀", text)

    def test_page_point_region_and_changed_round_trip(self):
        for lang in ("zh-CN", "en"):
            for a in (self.PAGE, self.POINT, self.REGION, self.CHANGED):
                with self.subTest(lang=lang, kind=a["kind"]):
                    items, _always = ef.parse_markdown(ef.render(self.notes(a), title="合成", lang=lang))
                    self.assertEqual(items[0]["pptx"], self.portable(a))
                    self.assertEqual(items[0]["text"], "改措辞", "定位行不并进正文")

    def test_elements_and_text_round_trip_in_both_languages(self):
        for lang in ("zh-CN", "en"):
            for a in (self.ELEMENTS, self.TEXT):
                with self.subTest(lang=lang, kind=a["kind"]):
                    items, _always = ef.parse_markdown(ef.render(self.notes(a), title="合成", lang=lang))
                    self.assertEqual(items[0]["pptx"], self.portable(a))

    def test_smartart_and_mixed_targets_round_trip_without_loss(self):
        smartart = {"key": "e15", "id": "15", "name": "合成流程图", "kind": "smartArt",
                    "z": 4, "digest": "c" * 64, "ro": ["text", "geometry", "style"]}
        for lang in ("zh-CN", "en"):
            for targets in ([smartart], [self.ELEMENTS["elements"][0], smartart]):
                with self.subTest(lang=lang, count=len(targets)):
                    a = dict(self.ELEMENTS, elements=targets)
                    items, _ = ef.parse_markdown(ef.render(self.notes(a), title="合成", lang=lang))
                    self.assertEqual(items[0]["pptx"], self.portable(a))
                    d = review_dir()
                    store.import_notes(d, items)
                    self.assertEqual(store.load(d)["notes"][0]["pptx"], self.portable(a))

    def test_point_and_thin_region_keep_storage_precision(self):
        for lang in ("zh-CN", "en"):
            for a in (dict(self.POINT, point={"x": 0.1234, "y": 0.9999}),
                      dict(self.REGION, region={"x": 0.1234, "y": 0.5678, "w": 0.004, "h": 0.0001})):
                with self.subTest(lang=lang, kind=a["kind"]):
                    items, _ = ef.parse_markdown(ef.render(self.notes(a), title="合成", lang=lang))
                    self.assertEqual(items[0]["pptx"], a)
                    d = review_dir()
                    store.import_notes(d, items)
                    self.assertEqual(store.load(d)["notes"][0]["pptx"], a)

    def test_newer_version_keeps_version_info_and_warns_on_import(self):
        """未知的新版本定位不当作无定位静默导入（Decision 17）：md 行只带版本号，
        保留版本信息（完整内容在 JSON 备份里），导入存储后 show 列出需升级提示。"""
        for lang, word in (("zh-CN", "PPT 定位：v2"), ("en", "PPT location: v2")):
            text = ef.render(self.notes({"v": 2, "slide": "s260", "x": 1}), title="合成", lang=lang)
            self.assertIn(word, text)
            items, _always = ef.parse_markdown(text)
            self.assertEqual(items[0]["pptx"], {"v": 2}, "保留原始版本信息，不静默丢弃")
            self.assertEqual(items[0]["text"], "改措辞", "定位行不并进正文")
            d = review_dir()
            store.import_notes(d, items)
            saved = store.load(d)["notes"][0]
            self.assertEqual(saved.get("pptx"), {"v": 2}, "存储层高版本原样保留，导入不丢")
            shown = notes_cli.to_markdown(store.load(d), {"title": "合成", "pages": [{"n": 2, "key": "s260", "title": "元素页"}]},
                                          include_resolved=True, page=None, locale=lang)
            self.assertIn("不可用于回写" if lang == "zh-CN" else "cannot be used for write-back", shown,
                          "需升级提示可见，不是无定位文本")

    def test_named_targets_reimport_into_the_same_review_without_duplicates(self):
        for lang in ("zh-CN", "en"):
            for a in (self.ELEMENTS, self.TEXT):
                with self.subTest(lang=lang, kind=a["kind"]):
                    d = review_dir()
                    store.import_notes(d, self.notes(a))
                    original = store.load(d)["notes"]
                    items, _ = ef.parse_markdown(ef.render(original, title="合成", lang=lang))
                    _doc, added, skipped, updated = store.import_notes(d, items)
                    self.assertEqual((added, skipped, updated), (0, 1, 0))
                    self.assertEqual(store.load(d)["notes"], original, "去重不改已有批注的展示名")

    def test_future_location_names_are_not_discarded_from_dedup(self):
        d = review_dir()
        notes = [dict(self.notes({"v": 2, "elements": [{"name": name}]})[0], id="future" + str(i))
                 for i, name in enumerate(("甲", "乙"))]
        _doc, added, skipped, _updated = store.import_notes(d, notes)
        self.assertEqual((added, skipped), (2, 0), "未知版本的字段含义未知，保持完整比较")

    def test_notes_without_anchor_export_as_before(self):
        note = self.notes(self.PAGE)[0]
        del note["pptx"]
        self.assertNotIn("PPT 定位", ef.render([note], title="合成", lang="zh-CN"))

    def test_same_text_on_different_elements_is_not_deduped(self):
        """同页、同文、同引用、元素目标不同：不是同一条批注，导入不去重（引用相同不等于目标相同）。"""
        d = review_dir()

        def anchor_for(digest: str) -> dict:
            return {"v": 1, "slide": "s257", "kind": "elements", "via": "click", "fp": OLD_FP,
                    "elements": [{"key": "e11", "id": "11", "name": "", "kind": "picture", "z": 1,
                                  "digest": digest}]}

        first = {"id": "n1", "page": 2, "key": "s257", "target": "报告", "text": "同样的话",
                 "quote": "相同", "pptx": anchor_for("d" * 64)}
        second = {**first, "id": "n2", "pptx": anchor_for("e" * 64)}
        _doc, added, skipped, _updated = store.import_notes(d, [first, second])
        self.assertEqual((added, skipped), (2, 0), "同文不同元素：两条都留下")


if __name__ == "__main__":
    unittest.main()
