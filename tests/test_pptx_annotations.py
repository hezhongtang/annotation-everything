"""PPTX 批注定位（#149）：可选 pptx 批注字段的清洗、元素目标有序列表与页面元素模型。

fixture 全部现场合成，不入库真实材料。只用标准库：
python3 -m unittest tests.test_pptx_annotations
"""
from __future__ import annotations

import hashlib
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "skill" / "annotation-everything" / "scripts"
sys.path.insert(0, str(SCRIPTS))
sys.path.insert(0, str(ROOT / "fixtures" / "pptx"))
os.environ.setdefault("AE_HOME", tempfile.mkdtemp(prefix="ae-test-home-"))   # 测试不碰用户真实的审阅台账
os.environ.setdefault("HOME", tempfile.mkdtemp(prefix="ae-test-user-"))

import pptx_reader as pr  # noqa: E402
import notes_store as store  # noqa: E402
import make_pptx as mp  # noqa: E402

FP = "ab" * 32
D64 = "0f" * 32


def tmp() -> Path:
    return Path(tempfile.mkdtemp(prefix="ae-pptx-note-"))


def review_dir() -> Path:
    d = tmp()
    (d / "review.json").write_text("{}", encoding="utf-8")
    return d


def element(key: str = "e2", *, eid: str = "2", name: str = "合成标题", kind: str = "shape",
            z: int = 0, digest: str = D64, ro: list[str] | None = None, **more) -> dict:
    out = {"key": key, "id": eid, "name": name, "kind": kind, "z": z, "digest": digest, **more}
    if ro is not None:
        out["ro"] = ro
    return out


def anchor(kind: str, via: str, **more) -> dict:
    return {"v": 1, "slide": "s260", "kind": kind, "via": via, **more}


class PptxAnchorFieldTests(unittest.TestCase):
    """批注的 pptx 定位字段 v1：写入时按 kind 清洗；更高版本原样保留；坏数据拒绝。"""

    def test_elements_anchor_keeps_order_and_drops_unknown_keys(self):
        d = review_dir()
        a = anchor("elements", "click", fp=FP, elements=[
            element("e11", eid="11", name="合成图片", kind="picture", z=1, ro=["text", "geometry", "style"]),
            element("e12", eid="12", name="合成组合", kind="group", z=2, ro=["geometry", "style"], junk=1),
        ], junk="x")
        _doc, note = store.add(d, {"page": 1, "text": "成组调整", "pptx": a})
        self.assertEqual(note["pptx"], {
            "v": 1, "slide": "s260", "kind": "elements", "via": "click", "fp": FP,
            "elements": [
                {"key": "e11", "id": "11", "name": "合成图片", "kind": "picture", "z": 1,
                 "digest": D64, "ro": ["text", "geometry", "style"]},
                {"key": "e12", "id": "12", "name": "合成组合", "kind": "group", "z": 2,
                 "digest": D64, "ro": ["geometry", "style"]},
            ]})
        self.assertEqual([e["key"] for e in note["pptx"]["elements"]], ["e11", "e12"],
                         "多元素批注保存每个目标的稳定身份，顺序就是选择顺序")

    def test_text_anchor_keeps_paragraph_offsets_quote_and_run_boundaries(self):
        d = review_dir()
        a = anchor("text", "selection", fp=FP, elements=[element()],
                   text={"para": 0, "paraDigest": D64, "start": 1, "end": 4, "quote": "相同😀",
                         "runs": [{"start": 0, "end": 2}, {"start": 2, "end": 4}]})
        _doc, note = store.add(d, {"page": 1, "text": "改措辞", "pptx": a})
        self.assertEqual(note["pptx"]["text"], {
            "para": 0, "paraDigest": D64, "start": 1, "end": 4, "quote": "相同😀",
            "runs": [{"start": 0, "end": 2}, {"start": 2, "end": 4}]})

    def test_page_point_region_anchors_use_normalized_canvas_coordinates(self):
        d = review_dir()
        cases = (
            (anchor("page", "thumb", fp=FP), {"v": 1, "slide": "s260", "kind": "page", "via": "thumb", "fp": FP}),
            (anchor("point", "point", point={"x": 0.5, "y": 0.1}), None),
            (anchor("region", "region", region={"x": 0.25, "y": 0.18, "w": 0.3, "h": 0.1}), None),
        )
        for raw, want in cases:
            with self.subTest(kind=raw["kind"]):
                _doc, note = store.add(d, {"page": 1, "text": "x", "pptx": raw})
                self.assertEqual(note["pptx"], want if want else raw,
                                 "点与区域保存相对幻灯片画布的归一化坐标")

    def test_malformed_anchor_is_rejected(self):
        d = review_dir()
        bad = (
            "x", {"v": 1}, {"v": 0}, {"v": True},
            anchor("elements", "thumb"),                       # kind 与 via 不匹配
            anchor("elements", "click", elements=[]),          # 元素目标至少一个
            anchor("page", "thumb", elements=[element()]),     # 整页不带元素列表
            anchor("text", "click", elements=[element()]),     # 文本定位来源只能是选区或文本反查
            anchor("text", "selection", elements=[element(), element("e3", eid="3")]),  # 文本只落一个元素
            anchor("text", "selection", elements=[element()]),  # 缺文本选区
            anchor("point", "point"),                          # 缺点坐标
            anchor("region", "region"),                        # 缺区域坐标
            anchor("elements", "click", elements=[element(ro=["nope"])]),
            anchor("elements", "click", elements=[element(digest="zz")]),
            anchor("elements", "click", elements=[element(kind="strange")]),
            anchor("elements", "click", elements=[element(z=-1)]),
            anchor("text", "selection", elements=[element()],
                   text={"para": 0, "paraDigest": D64, "start": 4, "end": 2, "quote": "甲"}),
            anchor("point", "point", point={"x": 1.5, "y": 0.1}),
            anchor("region", "region", region={"x": 0.2, "y": 0.1, "w": 0.9, "h": 0.1}),
            anchor("point", "point", point={"x": 0.5, "y": 0.1, "h": 0.2}),
            {**anchor("elements", "click"), "slide": ""},
            anchor("elements", "click", fp="not-hex"),
        )
        for raw in bad:
            with self.subTest(bad=json.dumps(raw, ensure_ascii=False)[:90]), self.assertRaises(store.NoteError):
                store.add(d, {"page": 1, "text": "x", "pptx": raw})

    def test_text_anchor_without_offsets_keeps_element_and_paragraph_only(self):
        """段内重复文字的文本反查：只记元素与逻辑段落（含上下文摘要），不猜字符偏移。"""
        d = review_dir()
        a = anchor("text", "text", elements=[element()],
                   text={"para": 1, "paraDigest": D64, "quote": "相同"})
        _doc, note = store.add(d, {"page": 1, "text": "改措辞", "pptx": a})
        self.assertEqual(note["pptx"]["text"], {"para": 1, "paraDigest": D64, "quote": "相同"})

    def test_notes_without_anchor_stay_as_before(self):
        d = review_dir()
        _doc, note = store.add(d, {"page": 1, "text": "旧式批注"})
        self.assertNotIn("pptx", note)

    def test_newer_anchor_version_is_kept_verbatim(self):
        d = review_dir()
        future = {"v": 2, "slide": "s260", "shape": {"new": [1, 2]}}
        _doc, note = store.add(d, {"page": 1, "text": "x", "pptx": future})
        self.assertEqual(note["pptx"], future)
        with self.assertRaises(store.NoteError):
            store.add(d, {"page": 1, "text": "y", "pptx": {"v": 2, "blob": "x" * 9000}})

    def test_update_replaces_or_clears_the_anchor(self):
        d = review_dir()
        _doc, note = store.add(d, {"page": 1, "text": "x", "pptx": anchor("elements", "click",
                                                                          elements=[element()])})
        _doc, note = store.update(d, note["id"], {"pptx": anchor("region", "region",
                                                                  region={"x": 0.1, "y": 0.1, "w": 0.2, "h": 0.2})})
        self.assertEqual(note["pptx"]["kind"], "region")
        _doc, note = store.update(d, note["id"], {"pptx": None})
        self.assertNotIn("pptx", note)

    def test_manual_page_move_drops_the_anchor(self):
        """人工挪页以这次决定为准：读得懂的 pptx 定位不再保留（与 docx 同一条规则）。"""
        d = review_dir()
        _doc, note = store.add(d, {"page": 1, "key": "s260", "text": "x",
                                   "pptx": anchor("elements", "click", elements=[element()])})
        _doc, moved = store.update(d, note["id"], {"page": 2, "key": "s257", "orphan": False})
        self.assertNotIn("pptx", moved)

    def test_import_round_trips_the_anchor(self):
        d = review_dir()
        a = anchor("text", "selection", elements=[element()],
                   text={"para": 0, "paraDigest": D64, "start": 0, "end": 2, "quote": "相同",
                         "runs": [{"start": 0, "end": 2}]})
        store.import_notes(d, [{"id": "n1", "page": 1, "text": "x", "pptx": a}])
        self.assertEqual(store.load(d)["notes"][0]["pptx"], a)


PNG_1PX = b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x06" \
          b"\x00\x00\x00\x1f\x15\xc4\x89\x00\x00\x00\nIDATx\x9cc\x00\x01\x00\x00\x05\x00\x01" \
          b"\x0d\n-\xb4\x00\x00\x00\x00IEND\xaeB`\x82"


def build_page(out: Path, source: Path) -> dict:
    """真实 build_viewer 构建（渲染注入受控 PNG），返回 review.json 内容。"""
    import build_viewer
    with mock.patch.object(__import__("pptx_report"), "render_slides", lambda p, w: [PNG_1PX, PNG_1PX]):
        build_viewer.main(["--report", str(source), "--out", str(out), "--no-scan-skills"])
    return json.loads((out / "review.json").read_text(encoding="utf-8"))


def viewer_pptx(out: Path) -> dict:
    """构建出的 index.html 里 PPTX 常量的元素模型。"""
    text = (out / "index.html").read_text(encoding="utf-8")
    start = text.find("const PPTX=")
    end = text.find(";\n", start)
    assert start > 0 and end > start, "index.html 缺 PPTX 常量"
    return json.loads(text[start + len("const PPTX="):end])


class PptxViewModelTests(unittest.TestCase):
    """对照页的 PPTX 常量携带逐页元素模型：身份、几何、能力与文本 run 边界（#149 交互依据）。"""

    @classmethod
    def setUpClass(cls):
        cls.out = tmp() / "rv"
        cls.manifest = build_page(cls.out, mp.build_sample(tmp() / "deck.pptx"))

    def test_slides_carry_the_reader_element_model(self):
        pptx = viewer_pptx(self.out)
        slides = {s["key"]: s for s in pptx["slides"]}
        self.assertEqual(set(slides), {"s260", "s257"})
        title = slides["s260"]["elements"][0]
        self.assertEqual(title["key"], "e2")
        self.assertEqual(title["kind"], "shape")
        self.assertEqual(title["geo"], {"x": 914400 / 12192000, "y": 914400 / 6858000,
                                        "width": 5486400 / 12192000, "height": 914400 / 6858000})
        self.assertTrue(title["cap"]["textWrite"] and title["cap"]["geometryWrite"])

    def test_paragraphs_keep_logical_text_digest_and_run_boundaries(self):
        pptx = viewer_pptx(self.out)
        title = pptx["slides"][0]["elements"][0]
        self.assertEqual([p["n"] for p in title["paragraphs"]], [0, 1])
        first = title["paragraphs"][0]
        self.assertEqual(first["text"], "相同😀&<")
        self.assertEqual(first["digest"], hashlib.sha256("相同😀&<".encode()).hexdigest(),
                         "段落摘要保持读取层口径（上下文摘要）")
        self.assertEqual(first["runs"], [{"start": 0, "end": 2}, {"start": 2, "end": 5}],
                         "跨 run 选区所需的样式边界按 Unicode 码点保留")
        second = title["paragraphs"][1]
        self.assertEqual((second["text"], second["runs"]), ("相同", [{"start": 0, "end": 2}]))

    def test_capability_matrix_reaches_the_page_for_readonly_elements(self):
        pptx = viewer_pptx(self.out)
        kinds = {e["kind"]: e for e in pptx["slides"][1]["elements"]}
        self.assertIn("picture", kinds)
        self.assertFalse(kinds["picture"]["cap"]["textWrite"], "图片按能力矩阵不可文本回写")
        self.assertFalse(kinds["group"]["cap"]["geometryWrite"], "组合形状不可自动回写几何")
        self.assertTrue(kinds["picture"]["cap"]["select"], "只读元素仍可选择")
        self.assertIn("unknown", kinds)
        self.assertFalse(kinds["unknown"]["cap"]["styleWrite"], "未知结构默认只读")


def pptx_view_model(source: Path) -> dict:
    """与 build_viewer.pptx_view 同构的页面 PPTX 常量（真实读取层输出）。"""
    import build_viewer
    model = pr.read(source)
    return {"fp": model["fingerprint"],
            "slides": [{"key": s["key"], "n": s["n"], "notes": False,
                        "elements": [build_viewer._pptx_view_element(e) for e in s["elements"]]}
                       for s in model["slides"]]}


_PURE_START = "// ---------- PPT 幻灯片定位（pptx 字段 v1）"
_PURE_END = "// ---------- PPT 幻灯片定位结束 ----------"


def pure_js() -> str:
    text = (ROOT / "skill" / "annotation-everything" / "scripts" / "viewer_template.html").read_text(encoding="utf-8")
    start, end = text.find(_PURE_START), text.find(_PURE_END)
    assert start > 0 and end > start, "页面缺少 PPT 幻灯片定位纯函数块"
    return text[start:end]


def _js_engine() -> list[str] | None:
    import shutil
    node = shutil.which("node")
    if node:
        return [node]
    if sys.platform == "darwin" and shutil.which("osascript"):
        return ["osascript", "-l", "JavaScript"]
    return None


class ViewerPptxLogicTests(unittest.TestCase):
    """页面里 PPT 定位纯函数的确定性检查：文本反查消歧、命中测试、目标身份与只读限制。

    有 node 或 macOS 的 JavaScriptCore 时直接执行模板里的函数；没有 JS 引擎的环境跳过
    （本机和端到端测试会真正执行），先例见 tests/test_review_page_i18n.py。
    """

    @classmethod
    def setUpClass(cls):
        engine = _js_engine()
        if engine is None:
            raise unittest.SkipTest("没有 JS 引擎，跳过页面 PPT 函数执行")
        cls.source = mp.build_sample(tmp() / "logic.pptx")
        model = pptx_view_model(cls.source)
        prologue = (
            "const PPTX = " + json.dumps(model, ensure_ascii=False) + ";\n"
            "const P = " + json.dumps([{"n": s["n"], "key": s["key"]} for s in model["slides"]]) + ";\n"
            "let i = 0;\n"
            "const clip = (s, n) => Array.from(String(s)).slice(0, n).join('');\n"
            "const cpLen = s => { let n = 0; for (const _ of s) n++; return n; };\n"
            "const tMsg = (k, p) => k + (p ? ':' + JSON.stringify(p) : '');\n"
        )
        cases = r'''
var __out = {
  crossRun: pptxFromText('相同😀&<'),
  repeated: pptxFromText('相同'),
  absent: pptxFromText('本页没有的文字 zqxj'),
  secondParaOnly: (i = 0, pptxFromText('相同')),
  hitTable: (i = 1, pptxHit(pptxSlide(), 0.04, 0.07)),
  hitMedia: pptxHit(pptxSlide(), 0.45, 0.07),
  hitGroupOverPicture: pptxHit(pptxSlide(), 0.15, 0.12),
  targetShape: (i = 0, pptxTargetOf(pptxSlide().elements[0])),
  targetPicture: (i = 1, pptxTargetOf(pptxHit(pptxSlide(), 0.45, 0.07))),
  point: (i = 0, pptxAnchor('point', 'point', {point: {x: 0.5, y: 0.1}})),
  locText: pptxLocText({v: 1, kind: 'text', slide: 's260',
    elements: [{key: 'e2', name: '合成标题', ro: []}],
    text: {para: 0, paraDigest: 'x', start: 1, end: 4, quote: '相同😀', runs: []}}),
  locElements: pptxLocText({v: 1, kind: 'elements', slide: 's257',
    elements: [{key: 'e11', name: '合成图片', ro: ['text', 'geometry', 'style']},
               {key: 'e12', name: '合成组合', ro: []}]})
};
'''
        script = prologue + pure_js() + "\n" + cases + "console.log(JSON.stringify(__out));"
        path = tmp() / "pptx_logic.js"
        path.write_text(script, encoding="utf-8")
        import subprocess
        done = subprocess.run(engine + [str(path)], capture_output=True, text=True, timeout=30)
        assert done.returncode == 0, done.stderr[:2000]
        cls.out = json.loads(done.stdout.strip().splitlines()[-1])

    def test_cross_run_text_lookup_keeps_paragraph_offsets_and_style_boundaries(self):
        a = self.out["crossRun"]
        self.assertEqual((a["kind"], a["via"], a["slide"]), ("text", "text", "s260"))
        self.assertEqual(a["elements"][0]["key"], "e2")
        self.assertEqual(a["text"]["para"], 0)
        self.assertEqual((a["text"]["start"], a["text"]["end"]), (0, 5))
        self.assertEqual(a["text"]["runs"], [{"start": 0, "end": 2}, {"start": 2, "end": 5}],
                         "跨 run 反查保留样式边界所需的 run 边界")
        self.assertEqual(a["text"]["quote"], "相同😀&<")

    def test_repeated_text_across_paragraphs_is_not_anchored_by_text_alone(self):
        self.assertIsNone(self.out["repeated"],
                          "「相同」在两个段落都出现：只匹配文字会选错目标，不写定位")
        self.assertIsNone(self.out["absent"])

    def test_hit_test_prefers_the_topmost_selectable_element(self):
        self.assertEqual(self.out["hitTable"]["key"], "e18",
                         "原点一带表格/图表/SmartArt/OLE/未知结构叠放：命中 z 最高的未知结构")
        self.assertEqual(self.out["hitMedia"]["key"], "e16")
        self.assertEqual(self.out["hitGroupOverPicture"]["key"], "e12",
                         "组合形状盖在图片上方时命中组合（z 更高）")

    def test_targets_carry_identity_and_readonly_limits(self):
        shape = self.out["targetShape"]
        self.assertEqual((shape["key"], shape["kind"], shape["id"]), ("e2", "shape", "2"))
        self.assertNotIn("ro", shape, "普通文本形状不在只读限制里")
        picture = self.out["targetPicture"]
        self.assertEqual(picture["ro"], ["text", "geometry", "style"], "图片按能力矩阵整组只读")
        self.assertEqual(picture["digest"], self.out["targetShape"]["digest"] and picture["digest"])

    def test_point_anchor_is_normalized_with_slide_key_and_fingerprint(self):
        model = pptx_view_model(self.source)
        p = self.out["point"]
        self.assertEqual((p["kind"], p["via"], p["slide"], p["point"]), ("point", "point", "s260", {"x": 0.5, "y": 0.1}))
        self.assertEqual(p["fp"], model["fp"])

    def test_location_summaries_name_target_type_and_quote(self):
        self.assertIn("viewer.pptx.loc.text", self.out["locText"])
        self.assertIn("相同😀", self.out["locText"])
        self.assertIn("viewer.pptx.loc.elements", self.out["locElements"])
        self.assertIn("viewer.pptx.ro", self.out["locElements"], "含只读元素时摘要标只读")


if __name__ == "__main__":
    unittest.main()
