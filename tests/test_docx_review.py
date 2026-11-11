"""DOCX 直接审阅（#135）：原分页渲染、分页可信度、支持矩阵诊断、对照页接入与兼容守卫。

fixture 全部由 fixtures/docx/make_sample.py 现场合成，不入库真实文档。
只用标准库：python3 -m unittest tests.test_docx_review
"""
from __future__ import annotations

import io
import json
import os
import re
import sys
import tempfile
import unittest
import zipfile
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
import ledger  # noqa: E402
import make_sample as ms  # noqa: E402
import notes as notes_cli  # noqa: E402
import notes_store as store  # noqa: E402
import report_check  # noqa: E402

LRPB = "<w:lastRenderedPageBreak/>"
PAGE_BR = '<w:r><w:br w:type="page"/></w:r>'


def tmp() -> Path:
    return Path(tempfile.mkdtemp(prefix="ae-docx-"))


def para(text: str = "", *, pid: str | None = None, runs: str = "", ppr: str = "") -> str:
    attr = f' w14:paraId="{pid}"' if pid else ""
    body = (f'<w:r><w:t xml:space="preserve">{text}</w:t></w:r>' if text else "") + runs
    return f'<w:p{attr}>{f"<w:pPr>{ppr}</w:pPr>" if ppr else ""}{body}</w:p>'


def mini(inner: str, *, sect: str = "", app_pages: int | None = None, extra: dict | None = None) -> Path:
    return ms.package(tmp() / "t.docx", ms.body_xml(inner, sect), app_pages=app_pages, extra=extra)


def codes(result: dict) -> dict[str, dict]:
    return {d["code"]: d for d in result["diagnostics"]}


def corrupt_member(path: Path, name: str = "word/document.xml") -> Path:
    """把 zip 里某个成员的压缩数据开头清零：zip 结构仍合法，解压必失败（模拟传输截断或位翻转）。"""
    raw = bytearray(path.read_bytes())
    i = raw.find(name.encode())
    hdr = i - 30   # 本地文件头 30 字节，其后是文件名
    assert bytes(raw[hdr:hdr + 4]) == b"PK\x03\x04", "没定位到数据区的本地文件头"
    nlen = int.from_bytes(raw[hdr + 26:hdr + 28], "little")
    elen = int.from_bytes(raw[hdr + 28:hdr + 30], "little")
    at = hdr + 30 + nlen + elen
    raw[at:at + 16] = bytes(16)
    out = path.with_name("corrupt-" + path.name)
    out.write_bytes(bytes(raw))
    return out


class SampleFixtureTests(unittest.TestCase):
    """合成多页样例：A4 纵向长条款、跨页表格、高亮、删除线、页眉页脚，加一节横向页。"""

    @classmethod
    def setUpClass(cls):
        cls.path = ms.build_docx(tmp() / "sample.docx")
        cls.r = dr.render(cls.path)

    def test_page_count_orientation_and_pagination_are_verified(self):
        pages = self.r["pages"]
        self.assertEqual(len(pages), ms.SAMPLE_PAGES)
        self.assertEqual(self.r["pagination"], "verified")
        self.assertEqual([p["orient"] for p in pages], ["portrait"] * 4 + ["landscape"])
        self.assertEqual((pages[0]["w"], pages[0]["h"]), (793.7, 1122.5), "A4 纵向 = 794×1123 px")
        self.assertEqual((pages[4]["w"], pages[4]["h"]), (1122.5, 793.7))
        for p in pages:
            self.assertIn(f'data-page="{p["n"]}"', p["html"])
            self.assertIn(f'width:{p["w"]:.1f}px;min-height:{p["h"]:.1f}px', p["html"], "页面按原尺寸，内容超出时向下延伸")

    def test_original_page_breaks_are_kept(self):
        pages = self.r["pages"]
        self.assertIn("第 4 条长条款", pages[0]["html"])
        self.assertIn("本条在第 2 页继续", pages[1]["html"], "段内渲染分页：续写落在第 2 页")
        self.assertNotIn("本条在第 2 页继续", pages[0]["html"])
        self.assertIn("docx-cont", pages[1]["html"])
        self.assertIn("示例货物 3", pages[1]["html"])
        self.assertNotIn("示例货物 4", pages[1]["html"], "表格第 4 行起在第 3 页")
        self.assertIn("示例货物 4", pages[2]["html"])
        self.assertIn("docx-repeat", pages[2]["html"], "续表重复表头")
        self.assertIn("三、签署", pages[3]["html"], "硬分页符后的内容在第 4 页")
        self.assertIn("附件：价格明细", pages[4]["html"])

    def test_marks_header_footer_and_table_structure(self):
        html1 = self.r["pages"][0]["html"]
        self.assertRegex(html1, r'class="docx-r docx-hl"[^>]*background-color:#ffff00[^>]*>关键条款已用黄色高亮')
        self.assertRegex(html1, r'class="docx-r docx-strike"[^>]*line-through[^>]*>删除线文字')
        for p in self.r["pages"]:
            self.assertIn("合成示例 · 仅供测试", p["html"], "每页有页眉")
            self.assertIn(f"第 {p['n']} 页 / 共 {ms.SAMPLE_PAGES} 页", dr.visible_text(p["html"]), "页脚的 PAGE / NUMPAGES 按实际页码")
        self.assertIn("<table", self.r["pages"][1]["html"])
        self.assertIn("background:#D9E2F3", self.r["pages"][1]["html"], "表头底纹")

    def test_paragraph_and_run_locators(self):
        html2 = self.r["pages"][1]["html"]
        self.assertRegex(html2, r'data-docx-p="\d+" data-docx-pid="1A[0-9A-F]{6}"')
        self.assertRegex(html2, r'data-docx-r="\d+"')
        header = re.search(r'<div class="docx-header".*?</div></div>', html2, re.S).group(0)
        self.assertNotIn("data-docx-p", header, "页眉段落不能当正文定位")
        repeat = re.search(r'<tr class="docx-th docx-repeat">.*?</tr>', self.r["pages"][2]["html"], re.S).group(0)
        self.assertNotIn("data-docx-p", repeat, "重复的表头不带定位")

    def test_stable_page_keys(self):
        keys = [p["key"] for p in self.r["pages"]]
        self.assertEqual(len(set(keys)), len(keys))
        self.assertTrue(keys[1].endswith("~c"), "从段落中间开始的页用续页键")
        self.assertTrue(all(build_viewer.stable_key(k) for k in keys))
        self.assertEqual(keys, [p["key"] for p in dr.render(self.path)["pages"]], "重建不变")

    def test_unsupported_structures_are_diagnosed_with_pages(self):
        c = codes(self.r)
        for code in ("revision", "comment", "textbox", "field"):
            self.assertIn(code, c)
            self.assertEqual(c[code]["pages"], [3])
        self.assertEqual(c["revision"]["count"], 2)
        self.assertEqual(c["comment"]["count"], 1, "一条 Word 批注跨多个运行也只计一次")
        self.assertEqual(c["header"]["count"], 1, "页眉每页都显示，但只计一次")
        self.assertNotIn("unknown", c)
        html3 = self.r["pages"][2]["html"]
        self.assertRegex(html3, r'data-docx-ro="[^"]*revision[^"]*"')
        self.assertRegex(html3, r'data-docx-ro="[^"]*field[^"]*"')
        self.assertIn('class="docx-txbx" data-docx-ro="textbox"', html3)
        self.assertIn("docx-comment", html3)
        for d in self.r["diagnostics"]:
            self.assertIn(d["code"], dr.SUPPORT, "诊断都在支持矩阵里")

    def test_estimated_and_unverified_pagination(self):
        est = dr.render(ms.build_docx(tmp() / "e.docx", rendered=False, para_ids=False))
        self.assertEqual(est["pagination"], "estimated")
        self.assertEqual(len(est["pages"]), 3, "只按硬分页与分节切")
        self.assertIn("estimatedPages", codes(est))
        self.assertTrue(all(p["key"].startswith("t") for p in est["pages"]), "没有 paraId 时用页首文字摘要")
        unv = dr.render(ms.build_docx(tmp() / "u.docx", app_pages=4))
        self.assertEqual(unv["pagination"], "unverified")


class PaginationEdgeTests(unittest.TestCase):
    def test_page_break_only_paragraph_stays_on_previous_page(self):
        r = dr.render(mini(para("一") + f"<w:p>{PAGE_BR}</w:p>" + f"<w:p><w:r>{LRPB}<w:t>二</w:t></w:r></w:p>"))
        self.assertEqual(len(r["pages"]), 2)
        self.assertIn("二", dr.visible_text(r["pages"][1]["html"]))

    def test_document_starting_with_page_break_keeps_blank_first_page(self):
        r = dr.render(mini(f"<w:p>{PAGE_BR}</w:p>" + para("正文")))
        self.assertEqual(len(r["pages"]), 2)
        self.assertNotIn("正文", r["pages"][0]["html"])

    def test_leading_inline_hard_breaks_keep_blank_pages(self):
        for count in (1, 2):
            with self.subTest(count=count):
                p = para(runs='<w:r>' + '<w:br w:type="page"/>' * count + '<w:t>正文</w:t></w:r>')
                r = dr.render(mini(p))
                self.assertEqual(len(r["pages"]), count + 1)
                self.assertTrue(all("正文" not in page["html"] for page in r["pages"][:-1]))
                self.assertIn("正文", r["pages"][-1]["html"])

    def test_initial_page_break_before_does_not_add_a_blank_page(self):
        r = dr.render(mini(para("正文", ppr='<w:pageBreakBefore/>')))
        self.assertEqual(len(r["pages"]), 1)

    def test_leading_hard_and_rendered_break_are_not_counted_twice(self):
        p = para(runs=f'<w:r><w:br w:type="page"/>{LRPB}<w:t>正文</w:t></w:r>')
        r = dr.render(mini(p))
        self.assertEqual(len(r["pages"]), 2)
        self.assertIn("正文", r["pages"][1]["html"])

    def test_rendered_marker_deduplicates_only_one_hard_break(self):
        p = para(runs=f'<w:r><w:t>首页</w:t>{LRPB}<w:br w:type="page"/><w:br w:type="page"/><w:t>第三页</w:t></w:r>')
        r = dr.render(mini(p))
        self.assertEqual(len(r["pages"]), 3)
        self.assertIn("第三页", r["pages"][2]["html"])

    def test_table_row_spanning_three_pages(self):
        cell = (f'<w:tc>{para("上")}<w:p><w:r>{LRPB}<w:t>中</w:t></w:r></w:p>'
                f'<w:p><w:r>{LRPB}<w:t>下</w:t></w:r></w:p></w:tc>')
        r = dr.render(mini(f"<w:tbl><w:tblGrid><w:gridCol w:w=\"3000\"/></w:tblGrid><w:tr>{cell}</w:tr></w:tbl>"))
        self.assertEqual(len(r["pages"]), 3)
        self.assertIn("rowSplit", codes(r))
        self.assertIn("下", r["pages"][2]["html"])

    def test_row_moving_to_next_page_with_marker_in_first_cell_only(self):
        row = (f'<w:tr><w:tc><w:p><w:r>{LRPB}<w:t>A2</w:t></w:r></w:p></w:tc><w:tc>{para("B2")}</w:tc></w:tr>')
        r = dr.render(mini(f'<w:tbl><w:tr><w:tc>{para("A1")}</w:tc><w:tc>{para("B1")}</w:tc></w:tr>{row}</w:tbl>'))
        self.assertEqual(len(r["pages"]), 2)
        self.assertIn("B2", r["pages"][1]["html"], "没有标记的格随行一起到新页")
        self.assertNotIn("rowSplit", codes(r))

    def test_continuous_section_changing_orientation_starts_new_page(self):
        cont = '<w:sectPr><w:type w:val="continuous"/><w:pgSz w:w="16838" w:h="11906" w:orient="landscape"/></w:sectPr>'
        first = '<w:sectPr><w:pgSz w:w="11906" w:h="16838"/></w:sectPr>'
        r = dr.render(mini(para("纵向", ppr=first) + para("横向"), sect=cont))
        self.assertEqual([p["orient"] for p in r["pages"]], ["portrait", "landscape"])

    def test_page_number_start_and_format(self):
        sect = ('<w:sectPr><w:footerReference w:type="default" r:id="rIdFooter"/><w:pgSz w:w="11906" w:h="16838"/>'
                '<w:pgNumType w:start="5" w:fmt="upperRoman"/></w:sectPr>')
        r = dr.render(mini(para("甲") + f"<w:p>{PAGE_BR}</w:p>" + para("乙"), sect=sect))
        self.assertEqual([p["label"] for p in r["pages"]], ["V", "VI"])
        self.assertIn("第 VI 页", dr.visible_text(r["pages"][1]["html"]))


class InlineStructureTests(unittest.TestCase):
    def test_revision_split_by_page_break_keeps_markup_and_readonly_on_both_fragments(self):
        p = (f'<w:p w14:paraId="0000AAAA"><w:r><w:t>前半</w:t></w:r><w:ins w:id="1" w:author="甲">'
             f'<w:r><w:t>插入一</w:t></w:r><w:r>{LRPB}<w:t>插入二</w:t></w:r></w:ins></w:p>')
        r = dr.render(mini(para("开头") + p))
        self.assertEqual(len(r["pages"]), 2)
        for page in r["pages"]:
            frag = re.search(r'<div class="docx-p[^"]*" data-docx-p="1"[^>]*>', page["html"]).group(0)
            self.assertIn('data-docx-ro="revision"', frag, "只读原因在分页之前就已知道")
        self.assertRegex(r["pages"][1]["html"], r'class="docx-r docx-ins"[^>]*>.*?插入二')
        self.assertEqual(r["pages"][1]["html"].count("<span"), r["pages"][1]["html"].count("</span>"))
        self.assertEqual(codes(r)["revision"]["pages"], [1, 2])
        self.assertEqual(codes(r)["revision"]["count"], 1)

    def test_diagnostics_follow_content_after_a_paragraph_break(self):
        p = para(runs=('<w:r><w:t>首页</w:t></w:r>' + PAGE_BR
                       + '<w:ins w:id="7" w:author="甲"><w:r><w:t>新增</w:t></w:r></w:ins>'))
        r = dr.render(mini(p))
        self.assertEqual(codes(r)["revision"]["pages"], [2])
        self.assertEqual(codes(r)["revision"]["count"], 1)

    def test_run_diagnostics_follow_content_after_a_break_in_the_same_run(self):
        p = para(runs=('<w:r><w:t>首页</w:t><w:br w:type="page"/>'
                       '<w:commentReference w:id="0"/></w:r>'))
        r = dr.render(mini(p))
        self.assertEqual(codes(r)["comment"]["pages"], [2])

    def test_table_diagnostics_follow_final_cell_and_row_pages(self):
        hdr = f'<w:tr><w:trPr><w:tblHeader/></w:trPr><w:tc>{para("表头")}</w:tc></w:tr>'
        field = '<w:fldSimple w:instr="DATE"><w:r><w:t>日期结果</w:t></w:r></w:fldSimple>'
        row = (f'<w:tr><w:tc>{para(runs=f"<w:r>{LRPB}<w:t>次页</w:t></w:r>")}</w:tc>'
               f'<w:tc>{para(runs=field)}</w:tc></w:tr>')
        comment = para(runs='<w:r><w:commentReference w:id="0"/></w:r>')
        final = f'<w:tr><w:tc>{comment}</w:tc></w:tr>'
        r = dr.render(mini(f'<w:tbl>{hdr}{row}{final}</w:tbl>'))
        self.assertEqual(len(r["pages"]), 2)
        self.assertEqual(codes(r)["field"]["pages"], [2], "没有分页标记的单元格随整行移动")
        self.assertEqual(codes(r)["comment"]["pages"], [2], "后续行沿用表格实际页码")
        self.assertEqual(codes(r)["table"]["pages"], [1, 2])

    def test_table_cell_diagnostics_follow_fragments(self):
        marked = (f'<w:commentRangeStart w:id="0"/><w:ins w:id="7">'
                  f'<w:r><w:t>上</w:t>{LRPB}<w:t>下</w:t></w:r></w:ins>'
                  '<w:commentRangeEnd w:id="0"/>')
        r = dr.render(mini(f'<w:tbl><w:tr><w:tc>{para(runs=marked)}</w:tc></w:tr></w:tbl>'))
        for code in ("revision", "comment", "rowSplit", "table"):
            self.assertEqual(codes(r)[code]["pages"], [1, 2], code)
            self.assertEqual(codes(r)[code]["count"], 1, code)

    def test_complex_field_diagnostics_follow_each_result_fragment(self):
        p = para(runs=('<w:r><w:t>前文</w:t><w:br w:type="page"/>'
                       '<w:fldChar w:fldCharType="begin"/><w:instrText> TOC </w:instrText>'
                       '<w:fldChar w:fldCharType="separate"/><w:t>目录一</w:t>'
                       '<w:br w:type="page"/><w:t>目录二</w:t><w:fldChar w:fldCharType="end"/></w:r>'))
        r = dr.render(mini(p))
        self.assertEqual(codes(r)["field"]["pages"], [2, 3])
        self.assertEqual(codes(r)["field"]["count"], 1)

    def test_repeated_header_diagnostics_follow_displayed_pages(self):
        marked = '<w:ins w:id="7"><w:r><w:t>修订表头</w:t></w:r></w:ins>'
        hdr = f'<w:tr><w:trPr><w:tblHeader/></w:trPr><w:tc>{para(runs=marked)}</w:tc></w:tr>'
        cell = para(runs=f'<w:r>{LRPB}<w:t>次页</w:t></w:r>')
        r = dr.render(mini(f'<w:tbl>{hdr}<w:tr><w:tc>{cell}</w:tc></w:tr></w:tbl>'))
        self.assertEqual(codes(r)["revision"]["pages"], [1, 2])
        self.assertEqual(codes(r)["revision"]["count"], 1)

    def test_table_property_revisions_are_visible_and_readonly(self):
        cases = (("trPr", "ins", "revision", "docx-ins"),
                 ("trPr", "del", "revision", "docx-del"),
                 ("tcPr", "cellIns", "revision", "docx-ins"),
                 ("tcPr", "cellDel", "revision", "docx-del"),
                 ("tcPr", "cellMerge", "revision", "docx-revision"),
                 ("tcPr", "tcPrChange", "formatRevision", "docx-revision"))
        for prop, tag, code, css in cases:
            with self.subTest(prop=prop, tag=tag):
                revision = f'<w:{prop}><w:{tag} w:id="9" w:author="审阅人"/></w:{prop}>'
                p = para(runs=f'<w:r><w:t>上</w:t>{LRPB}<w:t>下</w:t></w:r>')
                row = (f'<w:tr>{revision if prop == "trPr" else ""}'
                       f'<w:tc>{revision if prop == "tcPr" else ""}{p}</w:tc></w:tr>')
                r = dr.render(mini(f'<w:tbl>{row}</w:tbl>{para("普通正文")}'))
                self.assertEqual(codes(r)[code]["pages"], [1, 2])
                self.assertEqual(codes(r)[code]["count"], 1)
                for page in r["pages"]:
                    self.assertIn(f'data-docx-ro="{code}"', page["html"])
                    self.assertRegex(page["html"], rf'class="docx-r {css}"[^>]*title="[^"]*审阅人')
                    self.assertNotIn("\x00", page["html"], "内部诊断标记不能进入输出")
                plain = re.search(r'<div[^>]*data-docx-p="1"[^>]*>.*?</div>', r["pages"][-1]["html"]).group(0)
                self.assertNotIn("data-docx-ro", plain, "修订状态不能泄漏到表格之后")
                self.assertRegex(plain, r'<span class="docx-r"[^>]*>普通正文')

    def test_row_revision_reaches_all_cells_and_nested_tables(self):
        inner = f'<w:tbl><w:tr><w:tc>{para("嵌套内容")}</w:tc></w:tr></w:tbl>'
        row = ('<w:tr><w:trPr><w:ins w:id="9"/></w:trPr>'
               f'<w:tc>{para("第一格")}</w:tc><w:tc>{inner}{para("第二格")}</w:tc></w:tr>')
        r = dr.render(mini(f'<w:tbl>{row}</w:tbl>'))
        for text in ("第一格", "嵌套内容", "第二格"):
            self.assertRegex(r["pages"][0]["html"],
                             rf'data-docx-ro="revision"[^>]*><span class="docx-r docx-ins"[^>]*>{text}')
        self.assertEqual(codes(r)["revision"]["count"], 1)

    def test_multi_paragraph_field_marks_every_paragraph_readonly(self):
        toc = ('<w:p><w:r><w:fldChar w:fldCharType="begin"/></w:r><w:r><w:instrText> TOC \\o </w:instrText></w:r>'
               '<w:r><w:fldChar w:fldCharType="separate"/></w:r><w:r><w:t>目录一</w:t></w:r></w:p>'
               '<w:p><w:r><w:t>目录二</w:t></w:r></w:p>'
               '<w:p><w:r><w:t>目录三</w:t></w:r><w:r><w:fldChar w:fldCharType="end"/></w:r></w:p>')
        html_ = dr.render(mini(toc + para("正文")))["pages"][0]["html"]
        for text in ("目录一", "目录二", "目录三"):
            self.assertRegex(html_, rf'data-docx-ro="field"[^>]*><span[^>]*>{text}')
        self.assertRegex(html_, r'data-docx-p="3"[^>]*><span[^>]*>正文')
        self.assertNotRegex(html_, r'data-docx-p="3"[^>]*data-docx-ro')

    def test_nested_page_field_inside_instruction_is_hidden(self):
        nested = ('<w:p><w:r><w:fldChar w:fldCharType="begin"/></w:r><w:r><w:instrText>IF </w:instrText></w:r>'
                  '<w:r><w:fldChar w:fldCharType="begin"/></w:r><w:r><w:instrText>PAGE</w:instrText></w:r>'
                  '<w:r><w:fldChar w:fldCharType="separate"/></w:r><w:r><w:t>1</w:t></w:r>'
                  '<w:r><w:fldChar w:fldCharType="end"/></w:r><w:r><w:instrText> = 1 "a" "b"</w:instrText></w:r>'
                  '<w:r><w:fldChar w:fldCharType="separate"/></w:r><w:r><w:t>b</w:t></w:r>'
                  '<w:r><w:fldChar w:fldCharType="end"/></w:r></w:p>')
        self.assertEqual(dr.visible_text(dr.render(mini(nested))["pages"][0]["html"]), "b")

    def test_block_content_control_paragraphs_are_readonly(self):
        sdt = f"<w:sdt><w:sdtPr/><w:sdtContent>{para('控件里的段落')}</w:sdtContent></w:sdt>"
        html_ = dr.render(mini(sdt))["pages"][0]["html"]
        self.assertRegex(html_, r'data-docx-ro="contentControl"[^>]*><span[^>]*>控件里的段落')

    def test_body_level_comment_range_end_closes_comment(self):
        doc = ('<w:commentRangeStart w:id="0"/>' + para("被批注") + '<w:commentRangeEnd w:id="0"/>' + para("之后"))
        html_ = dr.render(mini(doc))["pages"][0]["html"]
        self.assertRegex(html_, r'docx-cmt[^>]*>被批注')
        self.assertNotRegex(html_, r'docx-cmt[^>]*>之后')

    def test_hidden_text_marks_paragraph_readonly(self):
        hidden = '<w:p><w:r><w:rPr><w:vanish/></w:rPr><w:t>看不见</w:t></w:r><w:r><w:t>看得见</w:t></w:r></w:p>'
        r = dr.render(mini(hidden))
        self.assertIn('data-docx-ro="hidden"', r["pages"][0]["html"])
        self.assertIn("hidden", codes(r))
        self.assertEqual(dr.visible_text(r["pages"][0]["html"]).replace(" ", ""), "看不见看得见",
                         "隐藏文字留在 DOM 里（hidden 属性），不在页面上显示")

    def test_alternate_content_falls_back_to_plain_text(self):
        alt = ('<w:p><w:r><mc:AlternateContent><mc:Choice Requires="w14"><w14:unknown/></mc:Choice>'
               '<mc:Fallback><w:t>退回文字</w:t></mc:Fallback></mc:AlternateContent></w:r></w:p>')
        self.assertIn("退回文字", dr.render(mini(alt))["pages"][0]["html"])

    def test_vmerge_after_repeated_header_is_not_merged_into_header(self):
        hdr = f'<w:tr><w:trPr><w:tblHeader/></w:trPr><w:tc>{para("表头")}</w:tc></w:tr>'
        r1 = f'<w:tr><w:tc><w:tcPr><w:vMerge w:val="restart"/></w:tcPr>{para("合并")}</w:tc></w:tr>'
        r2 = f'<w:tr><w:tc><w:tcPr><w:vMerge/></w:tcPr><w:p><w:r>{LRPB}</w:r></w:p></w:tc></w:tr>'
        r = dr.render(mini(f"<w:tbl>{hdr}{r1}{r2}</w:tbl>"))
        self.assertEqual(len(r["pages"]), 2)
        self.assertNotIn("rowspan", r["pages"][1]["html"])
        self.assertNotIn("unknown", codes(r), "tcPr / trPr 不算未识别结构")

    def test_numbering_shared_abstract_continues(self):
        num = ('<?xml version="1.0" encoding="UTF-8"?><w:numbering ' + ms.NS + '><w:abstractNum w:abstractNumId="0">'
               '<w:lvl w:ilvl="0"><w:start w:val="1"/><w:numFmt w:val="decimal"/><w:lvlText w:val="%1."/></w:lvl>'
               '</w:abstractNum><w:num w:numId="1"><w:abstractNumId w:val="0"/></w:num>'
               '<w:num w:numId="2"><w:abstractNumId w:val="0"/></w:num></w:numbering>')
        ppr = lambda n: f'<w:numPr><w:ilvl w:val="0"/><w:numId w:val="{n}"/></w:numPr>'  # noqa: E731
        r = dr.render(mini(para("甲", ppr=ppr(1)) + para("乙", ppr=ppr(2)), extra={"word/numbering.xml": num}))
        self.assertEqual(re.findall(r'class="docx-num" aria-hidden="true">([^<]*)<', r["pages"][0]["html"]), ["1.", "2."])

    def test_header_image_is_inlined_once(self):
        png = bytes.fromhex("89504e470d0a1a0a0000000d4948445200000001000000010806000000"
                            "1f15c4890000000d49444154789c6360000002000154a24f5d0000000049454e44ae426082")
        hdr = ('<?xml version="1.0" encoding="UTF-8"?><w:hdr ' + ms.NS + '>'
               '<w:p><w:r><w:pict><v:shape><v:imagedata r:id="rImg"/></v:shape></w:pict></w:r></w:p></w:hdr>')
        rels = ('<?xml version="1.0" encoding="UTF-8"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
                '<Relationship Id="rImg" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/image" Target="media/logo.png"/></Relationships>')
        sect = '<w:sectPr><w:headerReference w:type="default" r:id="rIdHeader"/><w:pgSz w:w="11906" w:h="16838"/></w:sectPr>'
        r = dr.render(mini(para("一") + f"<w:p>{PAGE_BR}</w:p>" + para("二") + f"<w:p>{PAGE_BR}</w:p>" + para("三"),
                           sect=sect, extra={"word/header1.xml": hdr, "word/_rels/header1.xml.rels": rels,
                                             "word/media/logo.png": png}))
        self.assertEqual(len(r["pages"]), 3)
        self.assertEqual(r["css"].count("data:image/png;base64,"), 1, "同一张图只内联一次")
        self.assertTrue(all("docx-m1" in p["html"] for p in r["pages"]))


class UntrustedInputTests(unittest.TestCase):
    def test_css_injection_in_colors_and_fonts_is_dropped(self):
        evil = ('<w:p><w:r><w:rPr><w:color w:val="000;position:fixed;inset:0"/>'
                '<w:rFonts w:ascii="x\';position:fixed;}" w:eastAsia="宋体"/>'
                '<w:shd w:fill="red;top:0"/></w:rPr><w:t>文字</w:t></w:r></w:p>')
        html_ = dr.render(mini(evil))["pages"][0]["html"]
        self.assertNotIn("position:fixed", html_)
        self.assertNotIn("top:0", html_)
        self.assertIn("&#x27;宋体&#x27;", html_, "合法的字体名照样保留")

    def test_bad_numbers_do_not_crash_and_are_bounded(self):
        num = ('<?xml version="1.0" encoding="UTF-8"?><w:numbering ' + ms.NS + '><w:abstractNum w:abstractNumId="0">'
               '<w:lvl w:ilvl="0"><w:start w:val="400000000"/><w:numFmt w:val="lowerLetter"/><w:lvlText w:val="%1)"/></w:lvl>'
               '</w:abstractNum><w:num w:numId="1"><w:abstractNumId w:val="0"/></w:num></w:numbering>')
        bad = ('<w:p><w:pPr><w:spacing w:before="nan" w:line="inf"/><w:ind w:left="1e400"/>'
               '<w:numPr><w:ilvl w:val="0"/><w:numId w:val="1"/></w:numPr></w:pPr>'
               '<w:r><w:rPr><w:sz w:val="nan"/></w:rPr><w:t>x</w:t></w:r></w:p>')
        r = dr.render(mini(bad, sect='<w:sectPr><w:pgSz w:w="inf" w:h="-5"/></w:sectPr>', extra={"word/numbering.xml": num}))
        self.assertLess(len(r["pages"][0]["html"]), 5000)

    def test_rejects_dtd_and_entity_declarations(self):
        bomb = ('<?xml version="1.0"?><!DOCTYPE x [<!ENTITY a "aaaa">]><w:document ' + ms.NS
                + '><w:body><w:p><w:r><w:t>&a;</w:t></w:r></w:p></w:body></w:document>')
        with self.assertRaises(dr.DocxError) as cm:
            dr.render(ms.package(tmp() / "b.docx", bomb))
        self.assertEqual(cm.exception.code, "dtd")

    def test_rejects_utf16_dtd_in_all_xml_parts(self):
        xml = ('<?xml version="1.0" encoding="UTF-16"?>'
               '<!DOCTYPE x [<!ENTITY a "合成实体">]><x>&a;</x>')
        for encoding, bom in (("utf-16-le", b"\xff\xfe"), ("utf-16-be", b"\xfe\xff")):
            for prefix in (b"", bom):
                for part in ("word/document.xml", "word/styles.xml", "_rels/.rels"):
                    with self.subTest(encoding=encoding, bom=bool(prefix), part=part):
                        source = mini(para("正文"), extra={part: prefix + xml.encode(encoding)})
                        with self.assertRaises(dr.DocxError) as cm:
                            dr.render(source)
                        self.assertEqual(cm.exception.code, "dtd")

    def test_valid_utf16_document_remains_supported(self):
        xml = ms.body_xml(para("合法正文")).replace('encoding="UTF-8"', 'encoding="UTF-16"')
        for encoding, bom in (("utf-16-le", b"\xff\xfe"), ("utf-16-be", b"\xfe\xff")):
            with self.subTest(encoding=encoding):
                source = mini("", extra={"word/document.xml": bom + xml.encode(encoding)})
                self.assertIn("合法正文", dr.render(source)["pages"][0]["html"])

    def test_oversized_part_is_refused(self):
        from unittest import mock
        with mock.patch.object(dr, "MAX_PART", 64):
            with self.assertRaises(dr.DocxError) as cm:
                dr.parse_xml(b"<a>x</a>" * 32, "word/document.xml")
        self.assertEqual(cm.exception.code, "tooLarge")

    def test_corrupt_zip_member_is_refused_as_not_zip(self):
        broken = corrupt_member(ms.build_docx(tmp() / "s.docx"))
        with self.assertRaises(dr.DocxError) as cm:
            dr.render(broken)
        self.assertEqual(cm.exception.code, "notZip", "压缩流损坏按 notZip 拒绝，不抛裸 zlib.error")

    def test_surrogate_sym_is_dropped_not_fatal(self):
        d = ms.Doc(False, False)
        doc = ms.body_xml(d.p(d.r("正文") + '<w:r><w:sym w:font="Wingdings" w:char="D800"/></w:r>'))
        r = dr.render(ms.package(tmp() / "sym.docx", doc))
        joined = "".join(p["html"] for p in r["pages"])
        self.assertNotIn("\ud800", joined, "孤立代理不出现在页面文字里")
        for p in r["pages"]:
            p["html"].encode("utf-8")   # 输出必须编码得进 UTF-8 文件

    def test_unreadable_formats_have_specific_codes(self):
        d = tmp()
        (d / "old.doc").write_bytes(dr.OLE_MAGIC + b"\0" * 600)
        (d / "enc.docx").write_bytes(dr.OLE_MAGIC + b"\0" * 64 + "EncryptedPackage".encode("utf-16-le"))
        (d / "junk.docx").write_bytes(b"not a zip")
        strict = ms.package(d / "strict.docx", '<?xml version="1.0"?><w:document xmlns:w="http://purl.oclc.org/ooxml/'
                            'wordprocessingml/main"><w:body/></w:document>')
        for name, code in (("old.doc", "legacyDoc"), ("enc.docx", "encrypted"), ("junk.docx", "notZip"),
                           (strict.name, "strict")):
            with self.subTest(name), self.assertRaises(dr.DocxError) as cm:
                dr.render(d / name)
            self.assertEqual(cm.exception.code, code)


class BuildIntegrationTests(unittest.TestCase):
    def build(self, report: Path, out: Path, *args: str) -> tuple[int, str, str]:
        o, e = io.StringIO(), io.StringIO()
        with redirect_stdout(o), redirect_stderr(e):
            try:
                code = build_viewer.main(["--report", str(report), "--out", str(out), "--no-scan-skills",
                                          "--locale", "zh-CN", *args])
            except SystemExit as exc:
                code = exc.code if isinstance(exc.code, int) else 1
                e.write(str(exc.code))
        return code, o.getvalue(), e.getvalue()

    def test_docx_builds_review_page_directly(self):
        out = tmp() / "rv"
        code, stdout, stderr = self.build(ms.build_docx(tmp() / "s.docx"), out)
        self.assertEqual(code, 0, stderr)
        self.assertIn("DOCX 分页：已校验", stdout)
        self.assertIn("DOCX 限制：Word 原生修订", stderr, "命令行列出限制")
        m = json.loads((out / "review.json").read_text(encoding="utf-8"))
        self.assertEqual(m["format"], {"report": "docx", "version": dr.FORMAT_VERSION})
        self.assertEqual(m["docx"]["pagination"], "verified")
        self.assertEqual([(p["w"], p["h"]) for p in m["pages"]][-1], (1122.5, 793.7))
        page = (out / "index.html").read_text(encoding="utf-8")
        self.assertIn('const DOCX={"v": 1, "pagination": "verified"', page)
        self.assertIn('"orient": "landscape"', page)
        self.assertNotIn("__DOCX__", page)

    def test_html_report_page_has_no_docx_metadata(self):
        out = tmp() / "rv"
        code, _o, stderr = self.build(ROOT / "fixtures" / "sample" / "report.html", out)
        self.assertEqual(code, 0, stderr)
        self.assertIn("const DOCX=null;", (out / "index.html").read_text(encoding="utf-8"))
        self.assertNotIn("format", json.loads((out / "review.json").read_text(encoding="utf-8")))

    def test_estimated_pagination_is_reported(self):
        out = tmp() / "rv"
        code, stdout, stderr = self.build(ms.build_docx(tmp() / "e.docx", rendered=False), out)
        self.assertEqual(code, 0, stderr)
        self.assertIn("分页为估算", stderr)
        self.assertIn("另存一次", stderr)
        self.assertEqual(json.loads((out / "review.json").read_text(encoding="utf-8"))["docx"]["pagination"], "estimated")

    def test_legacy_doc_is_refused_with_clear_message(self):
        d = tmp()
        (d / "old.doc").write_bytes(dr.OLE_MAGIC + b"\0" * 600)
        code, _o, stderr = self.build(d / "old.doc", d / "rv")
        self.assertNotEqual(code, 0)
        self.assertIn("另存为 .docx", stderr)
        self.assertNotIn("UnicodeDecodeError", stderr)

    def test_binary_with_other_extension_is_not_read_as_text_silently(self):
        d = tmp()
        (d / "report.html").write_bytes(b"\xff\xfe\x00bad")
        code, _o, stderr = self.build(d / "report.html", d / "rv")
        self.assertNotEqual(code, 0)
        self.assertIn("不是 .docx", stderr)

    def test_notes_follow_stable_page_key_when_pages_are_inserted(self):
        def doc(prefix: str) -> Path:
            body = (prefix + para("甲页", pid="0000A001") + f"<w:p>{PAGE_BR}</w:p>"
                    + f'<w:p w14:paraId="0000B001"><w:r>{LRPB}<w:t>乙页</w:t></w:r></w:p>')
            return mini(body, app_pages=None)
        out = tmp() / "rv"
        self.assertEqual(self.build(doc(""), out)[0], 0)
        _d, note = store.add(out, {"page": 2, "key": "w0000B001", "text": "乙页的批注"})
        rid = json.loads((out / "review.json").read_text(encoding="utf-8"))["reviewId"]
        src = json.loads((out / "review.json").read_text(encoding="utf-8"))["sourcesAbs"]["report"]
        # 前面插入一页：重建后批注跟着乙页挪到第 3 页
        Path(src).write_bytes(doc(para("新插入", pid="0000C001") + f"<w:p>{PAGE_BR}</w:p>").read_bytes())
        code, _o, stderr = self.build(Path(src), out)
        self.assertEqual(code, 0, stderr)
        self.assertEqual(json.loads((out / "review.json").read_text(encoding="utf-8"))["reviewId"], rid)
        moved = next(n for n in store.load(out)["notes"] if n["id"] == note["id"])
        self.assertEqual(moved["page"], 3)
        self.assertFalse(moved.get("orphan"))

    def test_rebuild_refuses_newer_format_version(self):
        out = tmp() / "rv"
        self.assertEqual(self.build(ms.build_docx(tmp() / "s.docx"), out)[0], 0)
        m = json.loads((out / "review.json").read_text(encoding="utf-8"))
        m["format"]["version"] = dr.FORMAT_VERSION + 1
        (out / "review.json").write_text(json.dumps(m), encoding="utf-8")
        self.assertIsNotNone(ledger.unsupported_format(m))
        e = io.StringIO()
        with redirect_stderr(e), self.assertRaises(SystemExit) as cm:
            build_viewer.main(["--rebuild", str(out), "--no-scan-skills"])
        self.assertIn("不支持", str(cm.exception.code))
        self.assertIsNone(ledger.unsupported_format({}), "旧审阅没有 format：按 HTML 处理")

    def test_notes_side_texts_and_batch_diff_read_docx(self):
        src = ms.build_docx(tmp() / "s.docx")
        texts = notes_cli.side_texts({"report": src, "script": None})
        self.assertIn("本条在第 2 页继续", texts["报告"][2])
        parts = notes_cli._load_parts("report", src)
        self.assertEqual([n for _k, n, _t in parts], [1, 2, 3, 4, 5])
        self.assertIn("违约金", parts[2][2])

    def test_build_reports_corrupt_zip_member_without_traceback(self):
        broken = corrupt_member(ms.build_docx(tmp() / "s.docx"))
        code, _o, e = self.build(broken, tmp() / "rv")
        self.assertNotEqual(code, 0)
        self.assertIn("不是有效的 DOCX", e, "损坏成员报本地化错误，不是 traceback")

    def test_docm_snapshot_keeps_bytes(self):
        out = tmp() / "rv"
        src = ms.build_docx(tmp() / "s.docm")
        self.assertEqual(self.build(src, out)[0], 0, ".docm 同样能建审阅")
        snap = notes_cli._snapshot(out, 1, "base")
        self.assertIsNotNone(snap, ".docm 报告的快照要成功，不能按文本读炸掉")
        saved = (snap / "report").read_bytes()
        self.assertEqual(saved, src.read_bytes(), "Word 家族后缀都按原字节保存")
        self.assertEqual(saved[:2], b"PK")

    def test_notes_refuse_newer_report_format(self):
        out = tmp() / "rv"
        self.assertEqual(self.build(ms.build_docx(tmp() / "s.docx"), out)[0], 0)
        m = json.loads((out / "review.json").read_text(encoding="utf-8"))
        m["format"]["version"] = dr.FORMAT_VERSION + 1
        (out / "review.json").write_text(json.dumps(m), encoding="utf-8")
        with self.assertRaises(notes_cli.BuildError) as cm:
            notes_cli._sources(out)   # show / wait / diff / snapshot 的源解析同样明确拒绝
        self.assertIn("不支持", str(cm.exception.code))

    def test_report_check_refuses_docx(self):
        with self.assertRaises(report_check.UsageError):
            report_check.check_report(ms.build_docx(tmp() / "s.docx"), locale="zh-CN")


if __name__ == "__main__":
    unittest.main()
