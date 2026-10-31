"""讲稿格式：英文页头与字段、中英等价、与界面语言无关、诊断与字段处理规则。

对应 issue #34 的验收标准。references/formats.md「讲稿 Markdown」一节的每条约定
在这里都有对得上的合成用例（中英格式、混合语言正文、缺页与代表性格式错误）。
只用标准库：python3 -m unittest discover tests
"""
from __future__ import annotations

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
FIXTURE = ROOT / "fixtures" / "sample"
sys.path.insert(0, str(SCRIPTS))
os.environ["AE_HOME"] = tempfile.mkdtemp(prefix="ae-test-home-")   # 测试不碰用户真实的审阅台账
os.environ["HOME"] = tempfile.mkdtemp(prefix="ae-test-user-")      # 技能扫描只使用合成目录
os.environ["CODEX_HOME"] = str(Path(os.environ["HOME"]) / ".codex")
os.environ["CLAUDE_SKILLS_DIR"] = str(Path(os.environ["HOME"]) / ".claude" / "skills")

import build_viewer  # noqa: E402
import i18n  # noqa: E402
import notes as notes_cli  # noqa: E402
import notes_store as store  # noqa: E402


def tmpdir() -> Path:
    return Path(tempfile.mkdtemp())


def write(dirpath: Path, name: str, text: str) -> Path:
    dirpath.mkdir(parents=True, exist_ok=True)
    f = dirpath / name
    f.write_text(text, encoding="utf-8")
    return f


def parse(text: str):
    return build_viewer.parse_script(text)


def semantics(pages: list[dict]) -> list[dict]:
    """页面语义：页码、标题、正文、问答、过渡与秒数；不含时长字段的原文写法。"""
    return [{k: v for k, v in p.items() if k != "dur"} for p in pages]


# 同一份内容的中英两种写法：除了页头与字段标记，正文一字不差
SAME_CONTENT_ZH = """# 讲稿：样例

### 第 1 页｜封面
**时长**：20 秒
**讲法**：
各位好，今天汇报三季度情况。
**过渡**：先看目录。

### 第 2 页｜结论
**时长**：1.5 分钟
**讲法**：
收入同比增长 12%。
老店同店增长 4%。
**过渡**：再看成本。
**可能被问**：
- 问：新店贡献多少？答：约占三分之二。
"""

SAME_CONTENT_EN = """# 讲稿：样例

### Page 1｜封面
**Duration**: 20 秒
**Talking points**：
各位好，今天汇报三季度情况。
**Transition**: 先看目录。

### Page 2｜结论
**Duration**: 1.5 分钟
**Talking points**:
收入同比增长 12%。
老店同店增长 4%。
**Transition**: 再看成本。
**Likely questions**:
- Q: 新店贡献多少？ A: 约占三分之二。
"""


class EnglishFormatTests(unittest.TestCase):
    """验收 1：规范的英文页头、时长、讲法、过渡和问答写法及合成样例。"""

    def test_english_page_head_fields_and_qa_parse(self):
        h1, _intro, pages, warns = parse(SAME_CONTENT_EN)
        self.assertEqual(h1, "讲稿：样例")
        self.assertEqual(warns, [])
        self.assertEqual([p["n"] for p in pages], [1, 2])
        self.assertEqual(pages[0]["title"], "封面")
        self.assertEqual(pages[0]["sec"], 20)
        self.assertEqual(pages[0]["dur"], "20 秒")
        self.assertEqual(pages[0]["talk"], "<p>各位好，今天汇报三季度情况。</p>")
        self.assertEqual(pages[0]["next"], "先看目录。")
        self.assertEqual(pages[1]["sec"], 90)
        self.assertEqual(pages[1]["talk"], "<p>收入同比增长 12%。</p><p>老店同店增长 4%。</p>")
        self.assertEqual(pages[1]["next"], "再看成本。")
        self.assertEqual(pages[1]["qa"], [{"q": "新店贡献多少？", "a": "约占三分之二。"}])

    def test_english_head_accepts_half_width_separator_and_spacing(self):
        _h1, _i, pages, warns = parse("### Page 3 | 结论：收入增长 12%\n**Duration**: 90 s\n**Talking points**：\n正文\n")
        self.assertEqual(warns, [])
        self.assertEqual(pages[0]["n"], 3)
        self.assertEqual(pages[0]["title"], "结论：收入增长 12%")
        self.assertEqual(pages[0]["sec"], 90)
        self.assertEqual(pages[0]["talk"], "<p>正文</p>")

    def test_english_head_without_space_after_page(self):
        """「### Page2｜标题」与中文「### 第2页｜标题」同样宽容，不会把整页并进上一页。"""
        _h1, _i, pages, warns = parse("### Page1｜封面\n**Duration**: 20 s\n**Talking points**:\n一\n"
                                      "### page2｜目录\n**Duration**: 30 s\n**Talking points**:\n二\n")
        self.assertEqual(warns, [])
        self.assertEqual([(p["n"], p["title"], p["sec"], p["talk"]) for p in pages],
                         [(1, "封面", 20, "<p>一</p>"), (2, "目录", 30, "<p>二</p>")])

    def test_head_without_separator_is_diagnosed_even_without_space(self):
        bad = write(tmpdir(), "s.md", "### 第 1 页｜封面\n**时长**：20 秒\n### page2 目录\n")
        with self.assertRaises(SystemExit) as cm:
            build_viewer.build(FIXTURE / "report.html", bad, tmpdir() / "a", locale="zh-CN", no_scan_skills=True)
        message = str(cm.exception)
        self.assertIn("第 3 行", message)
        self.assertIn("### page2 目录", message)
        self.assertNotIn("页序不一致", message, "先报页头写法，不要让作者从页码差异里猜")

    def test_a_colon_inside_chinese_text_is_not_an_answer_marker(self):
        """「套餐A：」「评级A：」是正文，不能当成英文问答的答标记把问题截断。"""
        for line, want in (("- 问：套餐A：能退吗？答：可以。", ("套餐A：能退吗？", "可以。")),
                           ("- 问：评级A：代表什么？", ("评级A：代表什么？", "")),
                           ("- 问：方案A：能改吗？答：在谈。", ("方案A：能改吗？", "在谈。"))):
            _h1, _i, pages, warns = parse(f"### 第 1 页｜问答\n**可能被问**：\n{line}\n")
            self.assertEqual(warns, [])
            self.assertEqual(pages[0]["qa"], [{"q": want[0], "a": want[1]}], line)

    def test_english_answer_marker_still_splits(self):
        for line, want in (("- Q: 新店贡献多少？ A: 约占三分之二。", ("新店贡献多少？", "约占三分之二。")),
                           ("- Q: Why did margin fall? A: Commission.", ("Why did margin fall?", "Commission.")),
                           ("- Q:q?A:a.", ("q?", "a.")),
                           # 问题里自己也带了「A:」时取第一个标记，与中文「答：」取第一个一致
                           ("- Q: Is plan A: cut costs feasible? A: Yes.", ("Is plan", "cut costs feasible? A: Yes."))):
            _h1, _i, pages, warns = parse(f"### Page 1｜QA\n**Likely questions**:\n{line}\n")
            self.assertEqual(warns, [])
            self.assertEqual(pages[0]["qa"], [{"q": want[0], "a": want[1]}], line)

    def test_unreadable_duration_warns_instead_of_crashing(self):
        self.assertEqual(build_viewer.seconds("1.2.3 分钟"), 0, "写错的数字不能让生成器崩掉")
        _h1, _i, pages, warns = parse("### 第 1 页｜时长写错\n**时长**：1.2.3 分钟\n**讲法**：\n正文\n")
        self.assertEqual(pages[0]["sec"], 0)
        self.assertEqual(pages[0]["dur"], "1.2.3 分钟")
        self.assertEqual(len(warns), 1)
        self.assertIn("看不懂", warns[0])
        self.assertIn("1.2.3 分钟", warns[0])
        _h1, _i, pages, warns = build_viewer.parse_script(
            "### Page 1｜Bad duration\n**Duration**: 1.2.3 min\n**Talking points**:\nBody\n", "en")
        self.assertEqual(len(warns), 1)
        self.assertIn("cannot be read", warns[0])

    def test_zero_and_wordless_durations_do_not_warn(self):
        for dur in ("0 秒", "半分钟", "30 秒", "2 min", "1 分 30 秒"):
            _h1, _i, pages, warns = parse(f"### 第 1 页｜时长\n**时长**：{dur}\n**讲法**：\n正文\n")
            self.assertEqual(warns, [], dur)

    def test_english_time_units(self):
        for text, want in (("2 minutes", 120), ("45 seconds", 45), ("30 s", 30), ("1 min 30 s", 90),
                           ("1.5 min", 90), ("2 mins", 120), ("20 秒", 20), ("1.5 分钟", 90),
                           ("1 分 30 秒", 90), ("90 sec", 90), ("2 MINUTES", 120)):
            self.assertEqual(build_viewer.seconds(text), want, text)

    def test_english_qa_without_bullets_and_half_width_colons(self):
        _h1, _i, pages, _w = parse("### Page 1｜问答\n**Likely questions**:\nQ: 甲？ A: 乙。\n")
        self.assertEqual(pages[0]["qa"], [{"q": "甲？", "a": "乙。"}])

    def test_english_head_and_field_names_ignore_case_and_spacing(self):
        text = "### PAGE 1｜封面\n**DURATION**: 20 s\n**talking  points**:\n正文\n**Transition**：收尾\n"
        _h1, _i, pages, warns = parse(text)
        self.assertEqual(warns, [])
        self.assertEqual(pages[0]["n"], 1)
        self.assertEqual(pages[0]["sec"], 20)
        self.assertEqual(pages[0]["talk"], "<p>正文</p>")
        self.assertEqual(pages[0]["next"], "收尾")

    def test_documented_english_aliases(self):
        text = ("### Page 1｜别名\n**Time**: 20 s\n**Talk**:\n正文\n"
                "### Page 2｜别名二\n**Time**: 30 s\n**Talking point**:\n正文二\n**Q&A**:\nQ: 甲？ A: 乙。\n"
                "### Page 3｜别名三\n**Questions**:\nQ: 丙？ A: 丁。\n")
        _h1, _i, pages, warns = parse(text)
        self.assertEqual(warns, [])
        self.assertEqual([p["sec"] for p in pages], [20, 30, 0])
        self.assertEqual(pages[0]["talk"], "<p>正文</p>")
        self.assertEqual(pages[1]["talk"], "<p>正文二</p>")
        self.assertEqual(pages[1]["qa"], [{"q": "甲？", "a": "乙。"}])
        self.assertEqual(pages[2]["qa"], [{"q": "丙？", "a": "丁。"}])

    def test_text_before_the_first_field_is_kept(self):
        text = "### Page 1｜开头几句\n开场白不算字段。\n**Duration**: 20 s\n**Talking points**:\n正文\n"
        _h1, _i, pages, warns = parse(text)
        self.assertEqual(warns, [])
        self.assertEqual(pages[0]["talk"], "<p>开场白不算字段。</p><p>正文</p>")
        self.assertEqual(pages[0]["sec"], 20)

    def test_english_sample_fixture_matches_chinese_fixture_structure(self):
        zh = parse((FIXTURE / "script.md").read_text(encoding="utf-8"))
        en = parse((FIXTURE / "script.en.md").read_text(encoding="utf-8"))
        self.assertEqual(zh[3], [])
        self.assertEqual(en[3], [])
        self.assertEqual(len(zh[2]), len(en[2]))
        for a, b in zip(zh[2], en[2]):
            self.assertEqual((a["n"], a["sec"], len(a["qa"]), bool(a["next"]), bool(a["talk"])),
                             (b["n"], b["sec"], len(b["qa"]), bool(b["next"]), bool(b["talk"])), f"第 {a['n']} 页")
        self.assertEqual([p["n"] for p in en[2]], [1, 2, 3, 4, 5, 6])
        self.assertEqual([p["sec"] for p in en[2]], [20, 30, 90, 120, 120, 90])
        self.assertEqual(len(en[2][2]["qa"]), 2)
        self.assertEqual(len(en[2][4]["qa"]), 1)
        self.assertIn("<p>", en[2][2]["talk"])


class EquivalenceTests(unittest.TestCase):
    """验收 2：相同内容的中英格式映射到等价的内部页语义。"""

    def test_same_content_maps_to_identical_pages(self):
        _h1, _i, zh, zh_warns = parse(SAME_CONTENT_ZH)
        _h1, _i, en, en_warns = parse(SAME_CONTENT_EN)
        self.assertEqual(zh_warns, [])
        self.assertEqual(en_warns, [])
        self.assertEqual(semantics(zh), semantics(en))

    def test_chinese_format_and_no_field_body_still_work(self):
        _h1, intro, pages, warns = parse((FIXTURE / "script.md").read_text(encoding="utf-8"))
        self.assertEqual(warns, [])
        self.assertIn("<h3>使用说明</h3>", intro)
        self.assertEqual([p["n"] for p in pages], [1, 2, 3, 4, 5, 6])
        self.assertEqual(sum(p["sec"] for p in pages), 20 + 30 + 90 + 120 + 120 + 90)
        self.assertEqual(pages[2]["qa"], [{"q": "新店贡献具体多少？", "a": "约占增量的三分之二。"},
                                          {"q": "老店为什么放缓？", "a": "主要是商圈客流下降。"}])
        # 受支持的无字段正文：整页按「讲法」显示（中英都一样）
        for text, talk in (("### 第 1 页｜无字段\n第一句\n第二句\n", "<p>第一句</p><p>第二句</p>"),
                           ("### Page 1｜No fields\nFirst line.\nSecond line.\n", "<p>First line.</p><p>Second line.</p>")):
            _h1, _i, only, warns = parse(text)
            self.assertEqual(warns, [])
            self.assertEqual(only[0]["talk"], talk)

    def test_mixed_head_and_field_languages_in_one_script(self):
        text = ("### 第 1 页｜中文页头\n**Duration**: 20 s\n**讲法**：\n中文界面也认英文时长。\n"
                "### Page 2｜English head\n**时长**：30 秒\n**Talking points**:\n英文界面也认中文字段。\n")
        _h1, _i, pages, warns = parse(text)
        self.assertEqual(warns, [])
        self.assertEqual([p["n"] for p in pages], [1, 2])
        self.assertEqual(pages[0]["sec"], 20)
        self.assertEqual(pages[0]["talk"], "<p>中文界面也认英文时长。</p>")
        self.assertEqual(pages[1]["sec"], 30)
        self.assertEqual(pages[1]["talk"], "<p>英文界面也认中文字段。</p>")


class LocaleIndependenceTests(unittest.TestCase):
    """验收 3：输入识别不依赖当前 locale，不翻译标题和正文来完成解析。"""

    def test_english_script_builds_in_chinese_ui_without_translation(self):
        out = tmpdir() / "review"
        r = build_viewer.build(FIXTURE / "report.html", FIXTURE / "script.en.md", out, locale="zh-CN", no_scan_skills=True)
        self.assertEqual(r["pages"], 6)
        self.assertEqual(r["seconds"], 20 + 30 + 90 + 120 + 120 + 90)
        html = (out / "index.html").read_text(encoding="utf-8")
        self.assertIn("Revenue grew 12% year on year", html, "正文按原文进页面，不翻译")
        self.assertIn("Conclusion: Q3 revenue up 12% year on year", html, "标题按原文进页面，不翻译")
        self.assertIn("About two thirds of the increase", html)
        self.assertNotIn("各位好", html, "讲稿按英文原样进页面，不翻译成中文")
        self.assertNotIn("今天分两部分", html)
        manifest = json.loads((out / "review.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["pages"][2]["title"], "Conclusion: Q3 revenue up 12% year on year")

    def test_chinese_script_builds_in_english_ui(self):
        out = tmpdir() / "review"
        r = build_viewer.build(FIXTURE / "report.html", FIXTURE / "script.md", out, locale="en", no_scan_skills=True)
        self.assertEqual(r["pages"], 6)
        self.assertEqual(r["seconds"], 20 + 30 + 90 + 120 + 120 + 90)
        html = (out / "index.html").read_text(encoding="utf-8")
        self.assertIn("三季度收入同比增长 12%", html)
        self.assertIn('REVIEW_LOCALE="en";', html)

    def test_diagnostics_follow_the_current_language(self):
        bad = write(tmpdir(), "bad.md", "### 第 1 页｜封面\n**时长**：20 秒\n### Page 2 - 结论\n**时长**：30 秒\n")
        with self.assertRaises(SystemExit) as cm:
            build_viewer.build(FIXTURE / "report.html", bad, tmpdir() / "a", locale="en", no_scan_skills=True)
        message = str(cm.exception)
        self.assertIn("is not a recognizable script page heading", message)
        self.assertIn("### Page N｜Title", message)
        self.assertNotIn("无法识别", message)
        with self.assertRaises(SystemExit) as cm:
            build_viewer.build(FIXTURE / "report.html", bad, tmpdir() / "b", locale="zh-CN", no_scan_skills=True)
        self.assertIn("不是能识别的讲稿页头", str(cm.exception))
        self.assertIn("### Page N｜Title", str(cm.exception))

    def test_english_diagnostics_name_the_english_field_forms(self):
        text = "### Page 1｜Typo\n**Duraion**: 20 s\n"
        _h1, _i, pages, warns = build_viewer.parse_script(text, "en")
        self.assertEqual(len(warns), 1)
        self.assertIn("Duraion", warns[0])
        self.assertIn("Duration", warns[0])
        self.assertIn("Talking points", warns[0])
        self.assertNotIn("讲法", warns[0], "英文诊断只写英文字段名")


class RebuildAndPerPageTests(unittest.TestCase):
    """验收 4：新生成与重建保留来源、审阅身份、稳定页面键与批注关联；按页读取、快照与对比同样有效。"""

    def test_rebuild_keeps_identity_keys_and_note_links(self):
        tree = tmpdir()
        report = tree / "report.html"
        script = tree / "script.md"
        report.write_bytes((FIXTURE / "report.html").read_bytes())
        script.write_text((FIXTURE / "script.en.md").read_text(encoding="utf-8"), encoding="utf-8")
        out = tree / "review"
        first = build_viewer.build(report, script, out, locale="en", no_scan_skills=True)
        manifest = json.loads((out / "review.json").read_text(encoding="utf-8"))
        self.assertEqual([p["key"] for p in manifest["pages"]], ["cover", "toc", "sum", "rev", "cost", "next"])
        self.assertTrue(all(p["stable"] for p in manifest["pages"]))
        self.assertEqual(manifest["sources"]["script"], os.path.relpath(script, out))
        store.add(out, {"page": 3, "text": "Shorten this sentence", "target": "讲稿", "key": "sum"})
        second = build_viewer.build(report, script, out, no_scan_skills=True)
        self.assertEqual(second["reviewId"], first["reviewId"])
        notes = store.load(out)["notes"]
        self.assertEqual([(n["page"], n["key"], n["text"]) for n in notes], [(3, "sum", "Shorten this sentence")])
        again = json.loads((out / "review.json").read_text(encoding="utf-8"))
        self.assertEqual(again["locale"], "en", "重建沿用已保存的界面语言")
        self.assertEqual(again["pages"][2]["title"], "Conclusion: Q3 revenue up 12% year on year")

    def test_per_page_texts_and_diff_cover_english_script(self):
        tree = tmpdir()
        report = tree / "report.html"
        script = tree / "script.md"
        report.write_bytes((FIXTURE / "report.html").read_bytes())
        script.write_text((FIXTURE / "script.en.md").read_text(encoding="utf-8"), encoding="utf-8")
        out = tree / "review"
        build_viewer.build(report, script, out, no_scan_skills=True)
        texts = notes_cli.side_texts(build_viewer.resolve_sources(out))
        self.assertIn("Revenue grew 12% year on year", texts["讲稿"][3])
        self.assertIn("About two thirds of the increase", texts["讲稿"][3])
        self.assertNotIn("Talking points", texts["讲稿"][3], "字段名不进纯文本")
        self.assertNotIn("Duration", texts["讲稿"][3], "字段名不进纯文本")

        parts = notes_cli._parts("script", script.read_text(encoding="utf-8"))
        self.assertEqual([(k, n) for k, n, _t in parts], [(f"n:{i}", i) for i in range(1, 7)])
        self.assertIn("### Page 3｜", parts[2][2])

        notes_cli._snapshot(out, 1, "base")
        script.write_text((FIXTURE / "script.en.md").read_text(encoding="utf-8").replace(
            "Revenue grew 12% year on year", "Revenue grew 13% year on year"), encoding="utf-8")
        buf = io.StringIO()
        with redirect_stdout(buf):
            code = notes_cli.diff_cmd(out, 1, "current", None)
        self.assertEqual(code, 0)
        diff = buf.getvalue()
        self.assertIn("讲稿 · 第 3 页", diff)
        self.assertIn("+Revenue grew 13% year on year", diff)
        self.assertIn("-Revenue grew 12% year on year", diff)
        self.assertNotIn("第 4 页", diff, "只列出改动过的页")


    def test_side_texts_keep_unrecognized_field_labels(self):
        """对照页把认不出的字段名当正文显示，匹配用的纯文本也要一样，否则「另一侧也要改」的提示会对不上。"""
        tree = tmpdir()
        (tree / "report.html").write_bytes((FIXTURE / "report.html").read_bytes())
        script = write(tree, "s.md", "".join(
            f"### 第 {i} 页｜第 {i} 页\n**时长**：20 秒\n**讲法**：\n**关键数据**：第 {i} 页的关键数据。\n"
            for i in range(1, 7)))
        build_viewer.build(tree / "report.html", script, tree / "review", no_scan_skills=True)
        texts = notes_cli.side_texts(build_viewer.resolve_sources(tree / "review"))
        self.assertIn("关键数据", texts["讲稿"][3])
        self.assertIn("第 3 页的关键数据。", texts["讲稿"][3])
        self.assertNotIn("讲法", texts["讲稿"][3], "认得的字段名不进纯文本")
        self.assertNotIn("时长", texts["讲稿"][3], "认得的字段名不进纯文本")


class DiagnosticTests(unittest.TestCase):
    """验收 5：页码不一致、无法识别页头及有歧义的新增格式给出现有语言的明确诊断。"""

    def test_no_page_head_names_both_formats(self):
        empty = write(tmpdir(), "s.md", "# 只有标题\n## 说明\n一些内容\n")
        with self.assertRaises(SystemExit) as cm:
            build_viewer.build(FIXTURE / "report.html", empty, tmpdir() / "a", locale="zh-CN", no_scan_skills=True)
        message = str(cm.exception)
        self.assertIn("### 第 N 页｜标题", message)
        self.assertIn("### Page N｜Title", message)
        with self.assertRaises(SystemExit) as cm:
            build_viewer.build(FIXTURE / "report.html", empty, tmpdir() / "b", locale="en", no_scan_skills=True)
        # 英文诊断只写英文写法（英文消息不混中文）；中文写法见中文消息与 references/formats.md
        self.assertIn("### Page N｜Title", str(cm.exception))
        self.assertNotIn("第 N 页", str(cm.exception))

    def test_unrecognized_head_reports_line_and_expected_form(self):
        cases = ("### Page 2 - 结论\n", "### Page 2：结论\n", "#### Page 2｜结论\n", "### 第 2 页 结论\n",
                 "### Page 2 结论\n")
        for text in cases:
            bad = write(tmpdir(), "s.md", "### 第 1 页｜封面\n**时长**：20 秒\n" + text)
            with self.assertRaises(SystemExit) as cm:
                build_viewer.build(FIXTURE / "report.html", bad, tmpdir() / "a", locale="zh-CN", no_scan_skills=True)
            message = str(cm.exception)
            self.assertIn("第 3 行", message, text)
            self.assertIn(text.strip(), message, text)
            self.assertIn("### 第 N 页｜标题", message, text)

    def test_duplicate_page_number_is_rejected(self):
        bad = write(tmpdir(), "s.md", "### 第 1 页｜甲\n**时长**：20 秒\n### 第 1 页｜乙\n**时长**：30 秒\n")
        with self.assertRaises(SystemExit) as cm:
            build_viewer.build(FIXTURE / "report.html", bad, tmpdir() / "a", locale="zh-CN", no_scan_skills=True)
        self.assertIn("第 1 页有多个页头", str(cm.exception))
        self.assertIn("第 1、3 行", str(cm.exception))

    def test_page_mismatch_is_localized_and_lenient_still_works(self):
        bad = write(tmpdir(), "s.md", (FIXTURE / "script.en.md").read_text(encoding="utf-8").split("### Page 6")[0])
        with self.assertRaises(SystemExit) as cm:
            build_viewer.build(FIXTURE / "report.html", bad, tmpdir() / "a", locale="en", no_scan_skills=True)
        message = str(cm.exception)
        self.assertIn("Script page numbers do not match the report", message)
        self.assertIn("missing from the script: [6]", message)
        self.assertIn("--lenient", message)
        self.assertNotIn("讲稿页码与报告页序不一致", message)
        with self.assertRaises(SystemExit) as cm:
            build_viewer.build(FIXTURE / "report.html", bad, tmpdir() / "b", locale="zh-CN", no_scan_skills=True)
        self.assertIn("讲稿页码与报告页序不一致", str(cm.exception))
        self.assertIn("报告有、讲稿缺：[6]", str(cm.exception))
        with redirect_stderr(io.StringIO()):
            r = build_viewer.build(FIXTURE / "report.html", bad, tmpdir() / "c", locale="en", lenient=True, no_scan_skills=True)
        self.assertEqual(r["pages"], 6)
        self.assertEqual(r["seconds"], 20 + 30 + 90 + 120 + 120)

    def test_report_only_and_missing_pages_unchanged(self):
        r = build_viewer.build(FIXTURE / "report.html", None, tmpdir() / "r", no_scan_skills=True)
        self.assertEqual(r["pages"], 6)
        self.assertEqual(r["seconds"], 0)


class FieldRuleTests(unittest.TestCase):
    """验收 6：重复或未知字段的处理规则：不丢内容、给诊断、不静默按空讲稿交付。"""

    def test_duplicate_field_warns_and_keeps_last_value(self):
        text = ("### 第 1 页｜重复\n**时长**：20 秒\n**讲法**：\n第一版。\n"
                "**时长**：40 秒\n**过渡**：收尾。\n")
        _h1, _i, pages, warns = parse(text)
        self.assertEqual(pages[0]["sec"], 40, "重复字段采用最后一次的值（与既有行为一致）")
        self.assertEqual(pages[0]["dur"], "40 秒")
        self.assertEqual(pages[0]["talk"], "<p>第一版。</p>", "重复字段不吞掉正文")
        self.assertEqual(pages[0]["next"], "收尾。")
        self.assertEqual(len(warns), 1)
        self.assertIn("字段「时长」重复", warns[0])
        self.assertIn("第 2、5 行", warns[0])

    def test_unknown_field_is_kept_as_body_and_warned(self):
        text = "### 第 1 页｜未知字段\n**讲法**：\n**备注**：这是加粗引导句，不是字段。\n正文照常。\n"
        _h1, _i, pages, warns = parse(text)
        self.assertIn("<b>备注</b>：这是加粗引导句，不是字段。", pages[0]["talk"])
        self.assertIn("正文照常。", pages[0]["talk"])
        self.assertEqual(len(warns), 1)
        self.assertIn("「备注」不是可识别的字段名", warns[0])
        self.assertIn("按正文保留", warns[0])
        self.assertIn("Talking points", warns[0])

    def test_english_field_typo_is_warned_not_dropped(self):
        text = "### Page 1｜Typo\n**Talking points**:\nBody line.\n**Duraion**: 20 s\n"
        _h1, _i, pages, warns = parse(text)
        self.assertIn("<b>Duraion</b>: 20 s", pages[0]["talk"], "认不出的字段名按正文保留，不丢内容")
        self.assertIn("Body line.", pages[0]["talk"])
        self.assertEqual(pages[0]["sec"], 0, "认不出的字段不计时长")
        self.assertEqual(len(warns), 1)
        self.assertIn("Duraion", warns[0])

    def test_page_with_only_unknown_fields_shows_whole_body(self):
        text = "### Page 1｜Only typo\n**Duraion**: 20 s\n**Transiton**: 收尾\n"
        _h1, _i, pages, warns = parse(text)
        self.assertEqual(pages[0]["talk"], "<p><b>Duraion</b>: 20 s</p><p><b>Transiton</b>: 收尾</p>")
        self.assertEqual(len(warns), 2)

    def test_old_chinese_input_with_extra_field_survives(self):
        text = "### 第 1 页｜旧稿\n**时长**：1 分钟\n**讲法**：\n正文。\n**备注**：补充说明\n**过渡**：下一段\n"
        _h1, _i, pages, warns = parse(text)
        self.assertEqual(pages[0]["sec"], 60)
        self.assertEqual(pages[0]["talk"], "<p>正文。</p><p><b>备注</b>：补充说明</p>")
        self.assertEqual(pages[0]["next"], "下一段")
        self.assertEqual(len(warns), 1)


class MixedLanguageTests(unittest.TestCase):
    """验收 7：混合语言正文按原文保留，界面语言不改写材料。"""

    def test_mixed_language_body_is_kept_verbatim(self):
        text = ("### Page 1｜混合\n**Duration**: 30 s\n**Talking points**:\n"
                "Revenue grew 12%（收入同比增长 12%）。\n第二行 mixed language。\n"
                "**Transition**: 过渡也用中英混排 next slide\n"
                "**Likely questions**:\n- Q: 新店贡献多少？ A: About two thirds.\n")
        _h1, _i, pages, warns = parse(text)
        self.assertEqual(warns, [])
        self.assertEqual(pages[0]["sec"], 30)
        self.assertEqual(pages[0]["talk"], "<p>Revenue grew 12%（收入同比增长 12%）。</p><p>第二行 mixed language。</p>")
        self.assertEqual(pages[0]["next"], "过渡也用中英混排 next slide")
        self.assertEqual(pages[0]["qa"], [{"q": "新店贡献多少？", "a": "About two thirds."}])

    def test_build_mixed_material_in_both_ui_languages(self):
        tree = tmpdir()
        pages = (("封面", "Hello 大家好。"), ("目录", "Agenda 目录。"), ("结论", "结论 +12%。"),
                 ("收入", "收入结构。"), ("成本", "成本端。"), ("下一步", "三件事。"))
        secs = (20, 30, 90, 120, 120, 90)
        script = write(tree, "s.md", "".join(
            f"### Page {i}｜{title}\n**Duration**: {sec} s\n**Talking points**:\n{body}\n"
            for i, (title, body), sec in zip(range(1, 7), pages, secs)))
        for locale in ("zh-CN", "en"):
            out = tmpdir() / ("r" + locale)
            r = build_viewer.build(FIXTURE / "report.html", script, out, locale=locale, no_scan_skills=True)
            self.assertEqual(r["pages"], 6)
            self.assertEqual(r["seconds"], sum(secs))
            html = (out / "index.html").read_text(encoding="utf-8")
            self.assertIn("Hello 大家好。", html)
            self.assertIn("结论 +12%。", html)


class CatalogTests(unittest.TestCase):
    """新增的 build.* / formats.* 诊断文案在中英词典里一致、占位符对齐。"""

    def test_shipped_catalogs_stay_consistent(self):
        self.assertEqual(i18n.check_catalogs(), [], "中英词典 key、占位符与复数分支必须一致")

    def test_new_keys_exist_in_both_languages(self):
        keys = ["build.scriptNoPages", "build.scriptBadHead", "build.scriptDuplicatePage", "build.pageMismatch",
                "build.pageMismatchLenient", "build.diff.onlyReport", "build.diff.onlyScript", "build.diff.order",
                "build.scriptDuplicateField", "build.scriptUnknownField", "formats.pageHead", "formats.fields"]
        for tag in i18n.SUPPORTED:
            messages = i18n.load_catalog(tag)["messages"]
            for key in keys:
                self.assertIn(key, messages, f"{tag} 缺少 {key}")

    def test_auto_locale_without_intro_uses_resolved_language(self):
        """auto 时简介兜底句按系统语言取，不因为语言选择解析不出而崩。"""
        out = tmpdir() / "review"
        text = (FIXTURE / "script.md").read_text(encoding="utf-8").split("### 第 1 页", 1)[1]
        script = write(tmpdir(), "s.md", "### 第 1 页" + text)
        r = build_viewer.build(FIXTURE / "report.html", script, out, locale="auto", no_scan_skills=True)
        self.assertEqual(r["pages"], 6)
        html = (out / "index.html").read_text(encoding="utf-8")
        self.assertIn('data-msg="viewer.introDefault"', html)


if __name__ == "__main__":
    unittest.main()
