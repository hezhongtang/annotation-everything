"""双语导出与跨语言导入：带版本与语言标记的 Markdown 交换格式，以及 JSON 结构化备份。

对应 issue #35 的验收标准。页面（viewer_template.html）与命令行（notes.py）各有一份实现，
这里用同一批合成用例钉住命令行那一份的规范；页面那一份由 tests/e2e_browser.py 的
export_import_checks 用同一批用例核对。只用标准库：python3 -m unittest discover tests
"""
from __future__ import annotations

import io
import importlib.util
import json
import os
import sys
import tempfile
import unittest
from unittest import mock
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
import export_format as ef  # noqa: E402
import i18n  # noqa: E402
import notes as notes_cli  # noqa: E402
import notes_store as store  # noqa: E402

AT = "2026-10-31 09:34:00"


def tmpdir() -> Path:
    return Path(tempfile.mkdtemp())


def review(tmp: Path, name: str = "rv", locale: str | None = None) -> Path:
    """按合成样例建一份审阅，返回审阅目录。"""
    out = tmp / name
    out.mkdir(parents=True, exist_ok=True)
    build_viewer.build(FIXTURE / "report.html", FIXTURE / "script.md", out,
                       **({"locale": locale} if locale else {}))
    return out


def rich_notes() -> list[dict]:
    """一条把 Markdown 支持字段都用上的批注，加一条只有正文的批注。"""
    return [
        {"id": "n1rich00000000a", "page": 3, "title": "结论：收入增长 12%", "target": "报告",
         "text": "把 +12% 改成「同比 +12%」\n第二行也要改", "quote": "+12%", "quoteFrom": "报告",
         "pin": {"x": 38.8, "y": 59.8}, "status": "open", "at": "2026-10-27T10:19:14Z"},
        {"id": "n2rich00000000b", "page": 7, "title": "成本", "target": "讲稿",
         "text": "改为：删掉整段", "quote": "原句甲乙", "quoteFrom": "讲稿", "status": "drafted",
         "edit": {"op": "replace", "from": "原句甲乙", "to": "删掉整段"},
         "at": "2026-10-27T11:00:00Z", "resolution": "已缩短", "region": {"x": 5, "y": 20, "w": 60, "h": 30},
         "style": {"scope": "region"}, "anchor": "table「渠道」", "skills": ["humanizer-zh", "dataviz"],
         "pair": {"side": "报告", "match": "exact", "text": "对应句子"}},
    ]


def add_notes(d: Path, notes: list[dict] | None = None) -> None:
    """写进审阅目录。store.add 不接受 status / resolution，drafted 要用 set_status 标。"""
    for n in (notes if notes is not None else rich_notes()):
        status = n.get("status")
        _doc, saved = store.add(d, {k: v for k, v in n.items() if k not in ("status", "resolution")})
        if status in ("drafted", "resolved"):
            # drafted 只能由代理标（by 不能是 page）；没有在途批次时 seq 留空
            store.set_status(d, saved["id"], status, by="claude", resolution=n.get("resolution", ""))


def run_cli(argv: list[str]) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = notes_cli.main(argv)
    return code, out.getvalue(), err.getvalue()


class MarkerTests(unittest.TestCase):
    """验收 2：新 Markdown 能识别格式版本与内容语言，解析不依赖导入界面 locale。"""

    def test_marker_declares_version_and_language(self):
        for lang in ("zh-CN", "en"):
            md = ef.render(rich_notes(), title="示例报告", lang=lang, exported_at=AT)
            self.assertEqual(md.splitlines()[0], f"<!-- annotation-everything export v1 lang={lang} -->")
            self.assertEqual(ef.read_marker(md), (1, lang))

    def test_marker_survives_leading_blank_lines_and_bom(self):
        md = ef.render(rich_notes(), title="x", lang="en", exported_at=AT)
        self.assertEqual(ef.read_marker("\n\n" + md), (1, "en"))
        self.assertEqual(ef.read_marker("﻿" + md), (1, "en"))

    def test_unknown_version_is_refused_with_a_reason(self):
        with self.assertRaises(ef.FormatError) as cm:
            ef.parse_markdown("<!-- annotation-everything export v2 lang=en -->\n# Annotations · x\n")
        self.assertIn("v2", str(cm.exception))
        self.assertIn("v1", str(cm.exception))

    def test_unknown_language_is_refused_with_a_reason(self):
        with self.assertRaises(ef.FormatError) as cm:
            ef.parse_markdown("<!-- annotation-everything export v1 lang=ja -->\n")
        self.assertIn("ja", str(cm.exception))

    def test_parse_does_not_depend_on_the_importing_ui_locale(self):
        """同一份英文导出，在中文界面和英文界面下解析结果必须一致。"""
        md = ef.render(rich_notes(), title="示例报告", lang="en", exported_at=AT)
        for env in ({}, {"AE_LOCALE": "zh-CN"}, {"AE_LOCALE": "en"}):
            saved = os.environ.get("AE_LOCALE")
            os.environ.pop("AE_LOCALE", None)
            os.environ.update(env)
            try:
                items, _always = ef.parse_markdown(md)
            finally:
                os.environ.pop("AE_LOCALE", None)
                if saved is not None:
                    os.environ["AE_LOCALE"] = saved
            self.assertEqual([i["target"] for i in items], ["报告", "讲稿"])
            self.assertEqual([i["status"] for i in items], ["open", "drafted"])

    GRAMMAR_LITERALS = ("[Report · Pin", "[Script · Region", "> Quote (", "> Quote: ",
                        "Persistent skills (", "Position hint: ", "Draft note: ", "Style: Whole page",
                        "↔ Other side (", "Skills: ", "Resolution: ",
                        # 标记本身是两边各自写死的常量，由 test_marker_declares_version_and_language 直接钉；
                        # 报错文案里可以引用它（那正是要告诉用户的写法），所以不列入这里。
                        "## 第 3 页｜", "【已处理】", "【已初改·待复核】")

    ZH_ONLY = ("## 第 3 页｜", "【已处理】", "【已初改·待复核】")

    def test_version_one_labels_stay_fixed(self):
        """交换格式保持 v1 标签；显示文案可以恰好使用相同的词，不代表文法依赖词典。"""
        with_resolved = rich_notes() + [{"id": "r" * 16, "page": 9, "title": "结尾", "target": "报告",
                                          "text": "已处理的意见", "status": "resolved",
                                          "at": "2026-10-27T14:00:00Z"}]
        zh = ef.render(with_resolved, title="示例报告", lang="zh-CN", exported_at=AT)
        en = ef.render(with_resolved, title="示例报告", lang="en", exported_at=AT)
        for literal in self.GRAMMAR_LITERALS + self.ZH_ONLY:
            if literal in self.ZH_ONLY:
                self.assertIn(literal, zh)
            elif literal not in ("[Script · Region", "> Quote: ", "Persistent skills (",
                                 "Style: Whole page", "Resolution: "):
                self.assertIn(literal, en)   # 其余几个这批合成数据里没有对应字段

    def test_ui_copy_change_does_not_change_the_grammar(self):
        """从模块加载到导出和解析都不读取 UI 词典，普通文案变化不会影响交换格式。"""
        before = {lang: ef.render(rich_notes(), title="t", lang=lang, exported_at=AT)
                  for lang in ("zh-CN", "en")}
        spec = importlib.util.spec_from_file_location("isolated_export_format", SCRIPTS / "export_format.py")
        isolated = importlib.util.module_from_spec(spec)
        with mock.patch.object(i18n, "load_catalog", side_effect=AssertionError("交换格式不得读取 UI 词典")) as load:
            spec.loader.exec_module(isolated)
            for lang, text in before.items():
                self.assertEqual(isolated.render(rich_notes(), title="t", lang=lang, exported_at=AT), text)
                self.assertEqual(isolated.parse_markdown(text), ef.parse_markdown(text))
            load.assert_not_called()


class RoundTripTests(unittest.TestCase):
    """验收 1、3：两种语言各自往返，以及跨语言往返；解析结果与语言无关。"""

    def _round_trip(self, lang: str) -> list[dict]:
        md = ef.render(rich_notes(), title="示例报告", lang=lang, exported_at=AT)
        items, always = ef.parse_markdown(md)
        self.assertIsNone(always)
        return items

    def test_chinese_export_imports_back_identically(self):
        items = self._round_trip("zh-CN")
        self.assertEqual(items[0], {"page": 3, "title": "结论：收入增长 12%", "target": "报告",
                                    "text": "把 +12% 改成「同比 +12%」\n第二行也要改", "status": "open",
                                    "pin": {"x": 38.8, "y": 59.8}, "quoteFrom": "报告", "quote": "+12%"})

    def test_english_export_imports_back_identically(self):
        items = self._round_trip("en")
        self.assertEqual(items, self._round_trip("zh-CN"), "英文导出与中文导出落库结果必须一致")

    def test_english_export_imports_into_a_chinese_review(self):
        md = ef.render(rich_notes(), title="示例报告", lang="en", exported_at=AT)
        items, _ = ef.parse_markdown(md)
        d = review(tmpdir())
        with self.subTest(lang="en"):
            _doc, added, skipped, updated = store.import_notes(d, items, by="test")
            self.assertEqual((added, skipped, updated), (2, 0, 0))
            got = store.load(d)["notes"]
            self.assertEqual([n["target"] for n in got], ["报告", "讲稿"])
            self.assertEqual([n["status"] for n in got], ["open", "drafted"])
            self.assertEqual(got[1]["style"], {"scope": "region"})
            self.assertEqual(got[1]["edit"], {"op": "replace", "from": "原句甲乙", "to": "删掉整段"})
            self.assertEqual(got[1]["pair"], {"side": "报告", "match": "exact", "text": "对应句子"})

    def test_chinese_export_imports_into_an_english_review(self):
        """中文导出进英文审阅：界面语言只影响显示，落库的协议值仍是中文。"""
        md = ef.render(rich_notes(), title="示例报告", lang="zh-CN", exported_at=AT)
        items, _ = ef.parse_markdown(md)
        d = review(tmpdir(), locale="en")
        store.import_notes(d, items, by="test")
        got = store.load(d)["notes"]
        self.assertEqual([n["target"] for n in got], ["报告", "讲稿"])
        self.assertEqual(got[1]["resolution"], "已缩短")

    def test_cli_export_then_cli_import_both_languages(self):
        for lang in ("zh-CN", "en"):
            with self.subTest(lang=lang):
                d = review(tmpdir())
                add_notes(d)
                code, out, err = run_cli(["export", str(d), "--lang", lang])
                self.assertEqual(code, 0, err)
                target = review(tmpdir(), name="target")
                f = target / "in.md"
                f.write_text(out, encoding="utf-8")
                code, out, err = run_cli(["import", str(target), str(f)])
                self.assertEqual(code, 0, err)
                self.assertIn("新增 2 条", out)
                got = store.load(target)["notes"]
                self.assertEqual([n["target"] for n in got], ["报告", "讲稿"])
                self.assertEqual(got[1]["edit"]["to"], "删掉整段")

    def test_cli_export_defaults_to_the_current_language(self):
        # 优先级按 i18n.py 的共同契约：--lang > AE_LOCALE > 审阅 locale > 中文；
        # 只有 auto 读 LC_ALL/LC_MESSAGES/LANG。全部语言环境变量在用例内给定，
        # 断言不随开发机的系统语言漂移。
        d = review(tmpdir())
        add_notes(d, [rich_notes()[0]])
        saved = {name: os.environ.get(name)
                 for name in ("AE_LOCALE", "LC_ALL", "LC_MESSAGES", "LANG")}

        def restore() -> None:
            for name, value in saved.items():
                if value is None:
                    os.environ.pop(name, None)
                else:
                    os.environ[name] = value

        for name in saved:
            os.environ.pop(name, None)
        try:
            _code, out, _err = run_cli(["export", str(d)])
            self.assertIn("lang=zh-CN", out.splitlines()[0], "没有 --lang 也没有审阅 locale 时默认中文")
            _code, out, _err = run_cli(["export", str(d), "--lang", "en"])
            self.assertIn("lang=en", out.splitlines()[0])
            # --lang auto 是显式选择，压过 AE_LOCALE；auto 只跟随系统语言（LC_ALL 优先）
            os.environ["AE_LOCALE"] = "en"
            os.environ["LC_ALL"] = "zh_CN.UTF-8"
            _code, out, _err = run_cli(["export", str(d), "--lang", "auto", "--format", "json"])
            self.assertIn('"lang": "zh-CN"', out, "显式 --lang auto 只跟随系统语言，不被 AE_LOCALE 改写")
            # 不带 --lang 时才轮到 AE_LOCALE，且优先于审阅里保存的 locale
            localized = review(tmpdir(), locale="zh-CN")
            add_notes(localized, [rich_notes()[0]])
            _code, out, _err = run_cli(["export", str(localized), "--format", "json"])
            self.assertIn('"lang": "en"', out, "AE_LOCALE 优先于审阅设置")
        finally:
            restore()

    def test_export_language_follows_the_review_locale(self):
        d = review(tmpdir(), locale="en")
        add_notes(d, [rich_notes()[0]])
        _code, out, _err = run_cli(["export", str(d)])
        self.assertIn("lang=en", out.splitlines()[0], "审阅里存了 locale=en 时默认英文")

    def test_illegal_language_choice_stops_without_touching_notes(self):
        d = review(tmpdir())
        add_notes(d, [rich_notes()[0]])
        code, out, err = run_cli(["export", str(d), "--lang", "fr"])
        self.assertEqual(code, 2)
        self.assertIn("fr", err)
        self.assertEqual(len(store.load(d)["notes"]), 1)

    def test_export_json_keeps_structured_semantics(self):
        d = review(tmpdir())
        add_notes(d)
        store.always_save(d, [{"name": "humanizer-zh", "scope": "讲稿"}])
        _code, out, _err = run_cli(["export", str(d), "--format", "json"])
        data = json.loads(out)
        self.assertEqual(data["version"], 1)
        self.assertEqual([a["scope"] for a in data["alwaysSkills"]], ["讲稿"])
        note = data["notes"][1]
        for key in ("id", "page", "target", "text", "quote", "status", "region", "style", "edit", "pair", "skills"):
            self.assertIn(key, note, f"JSON 备份要保留 {key}")
        self.assertEqual(note["target"], "讲稿", "协议值不改写")


class LegacyTests(unittest.TestCase):
    """验收 4：旧中文 Markdown 继续可读，并逐项声明往返范围。"""

    LEGACY = ("# 批注 · 示例报告\n导出时间：2026-10-30 08:00:00；共 1 条（未处理 1），涉及 1 页\n"
              "报告上的标注：标点是一个点，框选是一块区域；坐标为相对页面左上角的百分比（横向 x、纵向 y，宽、高）。\n"
              "## 第 4 页｜收入\n"
              "1. 【报告·标点 2（x 12.3%，y 45.6%）】意见一\n"
              "   > 引用（报告）：甲乙丙\n"
              "   ↔ 另一侧（讲稿·相近）：丁戊己\n"
              "2. 【报告和讲稿】删除引用的这段文字\n"
              "   > 引用（报告）：戊己庚\n"
              "3. 【讲稿·框选 3（x 1%，y 2%，宽 30%，高 4%）】风格意见\n"
              "   风格优化：框选处\n"
              "   位置线索：table「渠道」\n"
              "   技能：humanizer-zh（常驻·讲稿）、dataviz（这条指定）——处理这条前先调用这些技能\n")

    def test_legacy_chinese_markdown_still_parses(self):
        items, always = ef.parse_markdown(self.LEGACY)
        self.assertIsNone(always)
        self.assertEqual([i["page"] for i in items], [4, 4, 4])
        self.assertEqual([i["target"] for i in items], ["报告", "报告和讲稿", "讲稿"])
        self.assertEqual(items[0]["pin"], {"x": 12.3, "y": 45.6})
        self.assertEqual(items[0]["pair"], {"side": "讲稿", "match": "similar", "text": "丁戊己"})
        self.assertEqual(items[1]["edit"], {"op": "delete", "from": "戊己庚"})
        self.assertEqual(items[2]["region"], {"x": 1.0, "y": 2.0, "w": 30.0, "h": 4.0})
        self.assertEqual(items[2]["style"], {"scope": "region"})
        self.assertEqual(items[2]["anchor"], "table「渠道」")
        self.assertEqual(items[2]["skills"], ["dataviz"], "导入时常驻技能不写进单条（与既有行为一致）")

    def test_legacy_round_trip_through_the_cli(self):
        d = review(tmpdir())
        f = d / "legacy.md"
        f.write_text(self.LEGACY, encoding="utf-8")
        code, out, err = run_cli(["import", str(d), str(f)])
        self.assertEqual(code, 0, err)
        self.assertIn("新增 3 条", out)

    def test_declared_round_trip_scope(self):
        """逐项声明 Markdown 支持什么：这些字段往返后必须还在。"""
        note = rich_notes()[1]
        for lang in ("zh-CN", "en"):
            with self.subTest(lang=lang):
                md = ef.render([note], title="t", lang=lang, exported_at=AT)
                items, _ = ef.parse_markdown(md)
                self.assertEqual(items[0]["page"], 7, "页码")
                self.assertEqual(items[0]["title"], "成本", "页标题")
                self.assertEqual(items[0]["target"], "讲稿", "对象")
                self.assertEqual(items[0]["status"], "drafted", "状态")
                self.assertEqual(items[0]["text"], "改为：删掉整段", "正文")
                self.assertEqual(items[0]["quote"], "原句甲乙", "引用")
                self.assertEqual(items[0]["quoteFrom"], "讲稿", "引用来源")
                self.assertEqual(items[0]["region"], {"x": 5.0, "y": 20.0, "w": 60.0, "h": 30.0}, "框选坐标")
                self.assertEqual(items[0]["style"], {"scope": "region"}, "风格优化范围")
                self.assertEqual(items[0]["resolution"], "已缩短", "处理说明")
                self.assertEqual(items[0]["anchor"], "table「渠道」", "位置线索")
                self.assertEqual(items[0]["skills"], ["humanizer-zh", "dataviz"], "本条指定的技能")
                self.assertEqual(items[0]["pair"], {"side": "报告", "match": "exact", "text": "对应句子"}, "另一侧对应句")
                self.assertEqual(items[0]["edit"], {"op": "replace", "from": "原句甲乙", "to": "删掉整段"}, "快捷修改")

    def test_multiline_body_survives(self):
        note = rich_notes()[0]
        for lang in ("zh-CN", "en"):
            items, _ = ef.parse_markdown(ef.render([note], title="t", lang=lang, exported_at=AT))
            self.assertEqual(items[0]["text"], "把 +12% 改成「同比 +12%」\n第二行也要改")

    def test_status_markers_round_trip(self):
        for status, zh, en in (("open", "", ""), ("drafted", "【已初改·待复核】", "[Drafted] "),
                               ("resolved", "【已处理】", "[Resolved] ")):
            note = {"id": "x" * 16, "page": 2, "title": "t", "target": "报告", "text": "意见",
                    "status": status, "at": "2026-10-27T10:00:00Z"}
            for lang, prefix in (("zh-CN", zh), ("en", en)):
                md = ef.render([note], title="t", lang=lang, exported_at=AT)
                if prefix:
                    self.assertIn(prefix, md)
                items, _ = ef.parse_markdown(md)
                self.assertEqual(items[0]["status"], status)

    def test_always_skills_header_round_trips_in_both_languages(self):
        always = [{"name": "humanizer-zh", "scope": "讲稿"}, {"name": "dataviz", "scope": "all"}]
        for lang in ("zh-CN", "en"):
            md = ef.render(rich_notes(), title="t", always=always, lang=lang, exported_at=AT)
            items, got = ef.parse_markdown(md)
            self.assertEqual(got, always, f"{lang} 的常驻技能一行要能还原")
            self.assertEqual(len(items), 2)

    def test_resolution_and_draft_note_labels_differ(self):
        note = {"id": "y" * 16, "page": 2, "title": "t", "target": "报告", "text": "意见",
                "status": "drafted", "at": "2026-10-27T10:00:00Z", "resolution": "初改说明"}
        zh = ef.render([note], title="t", lang="zh-CN", exported_at=AT)
        en = ef.render([note], title="t", lang="en", exported_at=AT)
        self.assertIn("初改说明：", zh)
        self.assertIn("Draft note: ", en)
        self.assertEqual(ef.parse_markdown(zh)[0][0]["resolution"], "初改说明")
        self.assertEqual(ef.parse_markdown(en)[0][0]["resolution"], "初改说明")

    def test_unspecified_target_uses_the_fixed_literal(self):
        """对象为空时写固定字面量 `未指定`（文法的一部分，与 0.3 以来一致），导入时原样成为 target。"""
        note = {"id": "u" * 16, "page": 2, "title": "t", "target": "", "text": "意见",
                "at": "2026-10-27T10:00:00Z"}
        for lang in ("zh-CN", "en"):
            md = ef.render([note], title="t", lang=lang, exported_at=AT)
            self.assertIn("未指定", md, f"{lang} 也要写同一个字面量")
            items, _ = ef.parse_markdown(md)
            self.assertEqual(items[0]["target"], "未指定")

    def test_inherited_limits_are_pinned(self):
        """formats.md「从 0.3 继承下来的三条限制」：正文行读起来像元信息行时按元信息读。"""
        note = {"id": "v" * 16, "page": 2, "title": "t", "target": "报告",
                "text": "第一行\n   风格优化：整页", "at": "2026-10-27T10:00:00Z"}
        items, _ = ef.parse_markdown(ef.render([note], title="t", lang="zh-CN", exported_at=AT))
        self.assertEqual(items[0]["text"], "第一行", "像元信息行的正文行按元信息读（与 0.3 一致）")
        self.assertEqual(items[0]["style"], {"scope": "page"})

    def test_blank_line_inside_a_body_is_dropped(self):
        note = {"id": "w" * 16, "page": 2, "title": "t", "target": "报告",
                "text": "第一行\n\n第三行", "at": "2026-10-27T10:00:00Z"}
        items, _ = ef.parse_markdown(ef.render([note], title="t", lang="zh-CN", exported_at=AT))
        self.assertEqual(items[0]["text"], "第一行\n第三行", "空行被丢掉，要原样保留请用 JSON")

    def test_body_starting_with_a_status_marker_is_read_as_that_status(self):
        note = {"id": "x" * 16, "page": 2, "title": "t", "target": "报告",
                "text": "【已处理】其实是正文", "at": "2026-10-27T10:00:00Z"}
        items, _ = ef.parse_markdown(ef.render([note], title="t", lang="zh-CN", exported_at=AT))
        self.assertEqual(items[0]["status"], "resolved")
        self.assertEqual(items[0]["text"], "其实是正文")

    def test_zero_three_region_written_on_the_style_line_still_reads(self):
        md = ("## 第 4 页｜收入\n1. 【报告】表格太挤\n   风格优化：区域（x 5%，y 20%，宽 60%，高 30%）\n")
        items, _ = ef.parse_markdown(md)
        self.assertEqual(items[0]["region"], {"x": 5.0, "y": 20.0, "w": 60.0, "h": 30.0})
        self.assertEqual(items[0]["style"], {"scope": "region"})


class EditedBodyTests(unittest.TestCase):
    """验收 6：编辑可见正文后再导入，采用编辑后的内容。"""

    def test_edited_markdown_body_wins(self):
        md = ef.render(rich_notes(), title="t", lang="en", exported_at=AT)
        edited = md.replace("把 +12% 改成「同比 +12%」", "把 +12% 改成「同比增长 12 个百分点」")
        self.assertNotIn(edited, md)
        items, _ = ef.parse_markdown(edited)
        self.assertEqual(items[0]["text"], "把 +12% 改成「同比增长 12 个百分点」\n第二行也要改")

    def test_edited_quick_edit_body_wins_over_the_old_structure(self):
        md = ef.render(rich_notes(), title="t", lang="zh-CN", exported_at=AT)
        edited = md.replace("改为：删掉整段", "改为：只删最后一句")
        items, _ = ef.parse_markdown(edited)
        self.assertEqual(items[1]["edit"], {"op": "replace", "from": "原句甲乙", "to": "只删最后一句"})

    def test_json_import_uses_the_edited_body(self):
        """JSON 里只改正文：结构化的快捷修改要跟着正文走，不能写回旧文字。"""
        d = review(tmpdir())
        # 原批注与导入编辑都使用合成时钟，避免真实日期超过固定编辑日期后误判为旧版本。
        with mock.patch.object(store, "now_iso", return_value="2026-10-31T00:00:00Z"):
            add_notes(d)
        _code, out, _err = run_cli(["export", str(d), "--format", "json"])
        data = json.loads(out)
        edited = 0
        for note in data["notes"]:
            if note.get("edit", {}).get("op") == "replace":
                note["text"] = "改为：改成另一句"
                note["updatedAt"] = "2026-11-01T00:00:00Z"
                edited += 1
        self.assertEqual(edited, 1, "合成数据里要有一条带快捷修改的批注")
        f = d / "in.json"
        f.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        code, out, err = run_cli(["import", str(d), str(f)])
        self.assertEqual(code, 0, err)
        self.assertIn("更新 1 条", out)
        got = {n["id"]: n for n in store.load(d)["notes"]}
        changed = next(n for n in got.values() if n.get("edit"))
        self.assertEqual(changed["edit"]["to"], "改成另一句", "采用编辑后的正文，不写回旧内容")

    def test_json_visible_body_can_switch_quick_edit_operation(self):
        d = review(tmpdir())
        for original, text, expected in (
            ({"op": "delete", "from": "原句"}, "改为：新句", {"op": "replace", "from": "原句", "to": "新句"}),
            ({"op": "replace", "from": "原句", "to": "旧句"}, store.DELETE_TEXT, {"op": "delete", "from": "原句"}),
        ):
            with self.subTest(original=original):
                f = d / "switch.json"
                f.write_text(json.dumps({"notes": [{"page": 1, "target": "报告", "text": text,
                                                    "quote": "原句", "edit": original}]}), encoding="utf-8")
                code, _, err = run_cli(["import", str(d), str(f)])
                self.assertEqual(code, 0, err)
                saved = next(n for n in store.load(d)["notes"] if n["text"] == text)
                self.assertEqual(saved["edit"], expected)

    def test_unrelated_markdown_is_refused_with_a_rule(self):
        for text in ("随便一段文字，没有页头也没有条目。", "# 只是个标题\n\n一些段落。",
                     "<!-- 别的注释 -->\n## 第 1 页｜x\n", "## Page 3 | x\n1. [Report] 意见\n"):
            with self.subTest(text=text[:20]):
                with self.assertRaises(ef.FormatError) as cm:
                    ef.parse_markdown(text)
                self.assertIn("annotation-everything export v1", str(cm.exception))

    def test_marker_present_but_no_items_is_not_an_error(self):
        md = ef.render([], title="空审阅", lang="en", exported_at=AT)
        items, always = ef.parse_markdown(md)
        self.assertEqual(items, [])
        self.assertIsNone(always)

    def test_cli_reports_unrecognizable_files_clearly(self):
        d = review(tmpdir())
        f = d / "junk.md"
        f.write_text("完全不是批注导出的文字", encoding="utf-8")
        with self.assertRaises(SystemExit) as cm:
            notes_cli.read_import(f)
        self.assertIn("annotation-everything export v1", str(cm.exception))
        self.assertIn("第 N 页", str(cm.exception))


class SpecialCharTests(unittest.TestCase):
    """验收 8：多行、标点、花括号、特殊 HTML 字符与残缺格式。"""

    CASES = [
        ("多行正文", {"text": "第一行\n第二行\n\t第三行带制表符"}),
        ("中文标点", {"text": "改一下：「a，b；c」——d（e）"}),
        ("英文标点", {"text": 'Fix: "quoted", (paren) [bracket] {brace} — dash'}),
        ("花括号", {"text": "模板里的 {name} 与 {{literal}} 都要原样"}),
        ("HTML 字符", {"text": "<script>alert(1)</script> & <b>粗体</b> > 小于号"}),
        ("反引号与竖线", {"text": "`code` | pipe \\ backslash"}),
        ("空正文之外的空白", {"text": "   前后空格   "}),
        ("超长一行", {"text": "长" * 500}),
    ]

    # formats.md「从 0.3 继承下来的三条限制」第 1 条：正文续行读起来像元信息行时按元信息读。
    # 每种语言给对应形状的那一行（中文行在英文文法里就是普通正文，反之亦然）。
    META_LINES = {
        "zh-CN": ["风格优化：整页", "处理说明：改好了", "技能：humanizer-zh", "位置线索：table",
                  "↔ 另一侧（报告·相同）：对应句", "> 引用（报告）：别的东西"],
        "en": ["Style: Whole page", "Resolution: done", "Skills: humanizer-zh", "Position hint: table",
               "↔ Other side (Report · identical): a sentence", "> Quote (Report): something else"],
    }

    def test_special_bodies_round_trip_in_both_languages(self):
        for label, extra in self.CASES:
            for lang in ("zh-CN", "en"):
                with self.subTest(label=label, lang=lang):
                    note = {"id": "z" * 16, "page": 2, "title": "标题｜带分隔符", "target": "报告",
                            "at": "2026-10-27T10:00:00Z", **extra}
                    md = ef.render([note], title="t", lang=lang, exported_at=AT)
                    items, _ = ef.parse_markdown(md)
                    self.assertEqual(items[0]["text"], extra["text"])
                    self.assertEqual(items[0]["title"], "标题｜带分隔符")

    def test_metadata_shaped_body_line_is_read_as_metadata(self):
        for lang, lines in self.META_LINES.items():
            for line in lines:
                with self.subTest(lang=lang, line=line):
                    note = {"id": "m" * 16, "page": 2, "title": "t", "target": "报告",
                            "text": "意见\n" + line, "at": "2026-10-27T10:00:00Z"}
                    items, _ = ef.parse_markdown(ef.render([note], title="t", lang=lang, exported_at=AT))
                    self.assertEqual(items[0]["text"], "意见", "这一行按元信息读，不算正文")

    def test_metadata_shaped_body_line_fabricates_a_missing_field(self):
        """限制第 1 条的实际后果：这一行不算正文；批注本来没有该字段时，还会凭正文造出一个来
        （引用被造出来会改掉重复判定，快捷修改的 edit.from 也会跟着变）。"""
        for lang, line in (("zh-CN", "> 引用（讲稿）：别的东西"), ("en", "> Quote (Script): something else")):
            with self.subTest(lang=lang):
                note = {"id": "y" * 16, "page": 2, "title": "t", "target": "报告",
                        "text": "结论\n" + line, "at": "2026-10-27T10:00:00Z"}
                items, _ = ef.parse_markdown(ef.render([note], title="t", lang=lang, exported_at=AT))
                self.assertEqual(items[0]["text"], "结论")
                self.assertEqual(items[0]["quote"], "别的东西" if lang == "zh-CN" else "something else")
                self.assertEqual(items[0]["quoteFrom"], "讲稿", "来源还原成中文存储值")

    def test_real_metadata_still_wins_over_a_metadata_shaped_body_line(self):
        """导出的元信息行排在正文续行之后，所以批注真有的字段不会被正文里的同形行改掉。"""
        for lang, body_line, real in (("zh-CN", "技能：假的", "真的"), ("en", "Skills: fake", "real")):
            with self.subTest(lang=lang):
                note = {"id": "w" * 16, "page": 2, "title": "t", "target": "报告", "skills": [real],
                        "text": "意见\n" + body_line, "at": "2026-10-27T10:00:00Z"}
                items, _ = ef.parse_markdown(ef.render([note], title="t", lang=lang, exported_at=AT))
                self.assertEqual(items[0]["text"], "意见")
                self.assertEqual(items[0]["skills"], [real])

    def test_quote_with_html_and_newline_is_flattened(self):
        note = {"id": "q" * 16, "page": 2, "title": "t", "target": "报告", "text": "意见",
                "quote": "第一行\n<script>x</script>", "quoteFrom": "报告", "at": "2026-10-27T10:00:00Z"}
        for lang in ("zh-CN", "en"):
            md = ef.render([note], title="t", lang=lang, exported_at=AT)
            self.assertNotIn("\n<script>", md, "引用里的换行要折成一行，否则会变成续行")
            items, _ = ef.parse_markdown(md)
            self.assertEqual(items[0]["quote"], "第一行 <script>x</script>")

    def test_english_item_survives_a_stripped_trailing_space(self):
        """英文条目靠方括号后那个空格分隔正文；行尾空白被工具去掉时也不能把整条读丢。"""
        note = {"id": "s" * 16, "page": 2, "title": "t", "target": "报告", "text": "意见",
                "at": "2026-10-27T10:00:00Z"}
        md = ef.render([note], title="t", lang="en", exported_at=AT)
        stripped = "\n".join(line.rstrip() for line in md.splitlines())
        items, _ = ef.parse_markdown(stripped)
        self.assertEqual([i["text"] for i in items], ["意见"])
        for line in stripped.splitlines():
            if line.startswith("1. ["):
                self.assertRegex(line, r"^1\. \[Report\]\s?意见$")

    def test_broken_formats_are_handled_without_crashing(self):
        broken = [
            "<!-- annotation-everything export v1 lang=en -->\n# Annotations · t\n## Page 3 | x\n1. [Report\n",
            "<!-- annotation-everything export v1 lang=en -->\n## Page 3 | x\n1. [Report · Pin 1 (x abc%, y 1%)] 意见\n",
            "<!-- annotation-everything export v1 lang=zh-CN -->\n## 第 3 页｜x\n1. 【报告·标点 1（x 1%，y 2%）】\n",
            "<!-- annotation-everything export v1 lang=zh-CN -->\n## 第 x 页｜标题\n1. 【报告】意见\n",
            "<!-- annotation-everything export v1 lang=zh-CN -->\n1. 【报告】没有页头的条目\n",
            "<!-- annotation-everything export v1 lang=zh-CN -->\n## 第 3 页｜x\n   > 引用（报告）：孤立引用\n",
        ]
        for text in broken:
            with self.subTest(text=text.splitlines()[-1][:30]):
                items, _always = ef.parse_markdown(text)   # 不抛异常，也不产出半条批注
                self.assertTrue(all(i.get("text", "").strip() or i.get("status") for i in items))

    def test_export_is_safe_to_render(self):
        """导出内容按不可信数据处理：HTML 原样写出，由使用方转义。"""
        note = {"id": "h" * 16, "page": 2, "title": "t", "target": "报告",
                "text": "<img src=x onerror=alert(1)>", "at": "2026-10-27T10:00:00Z"}
        md = ef.render([note], title="t", lang="en", exported_at=AT)
        self.assertIn("<img src=x onerror=alert(1)>", md, "导出不做转义，也不解释 HTML")


class WrapperTests(unittest.TestCase):
    """验收 7：文件名与导出时间可按语言显示，机器语义不受影响。"""

    def test_filename_keeps_review_identity_and_a_machine_stamp(self):
        for lang in ("zh-CN", "en"):
            name = ef.default_filename("示例报告_ab12", lang, stamp="202610310934")
            self.assertTrue(name.endswith(".md"))
            self.assertIn("示例报告_ab12", name, "文件名保留审阅识别")
            self.assertIn("202610310934", name, "时间戳紧凑、可按字典序排序")
        self.assertTrue(ef.default_filename("ns", "en", stamp="202610310934").startswith("annotations_"))
        self.assertTrue(ef.default_filename("ns", "zh-CN", stamp="202610310934").startswith("批注_"))

    def test_exported_timestamp_is_locale_independent(self):
        note = {"id": "t" * 16, "page": 2, "title": "t", "target": "报告", "text": "意见",
                "at": "2026-10-27T10:00:00Z"}
        stamps = {ef.render([note], title="t", lang=lang, exported_at=AT).splitlines()[2] for lang in ("zh-CN", "en")}
        self.assertEqual(len(stamps), 2)
        for line in stamps:
            self.assertIn("2026-10-31 09:34:00", line)

    def test_coordinates_and_numbers_keep_their_machine_meaning(self):
        note = {"id": "c" * 16, "page": 2, "title": "t", "target": "报告", "text": "意见",
                "pin": {"x": 1234.5, "y": 0.125}, "region": {"x": 100000, "y": 0.5, "w": 33.333, "h": 1},
                "at": "2026-10-27T10:00:00Z"}
        for lang in ("zh-CN", "en"):
            items, _ = ef.parse_markdown(ef.render([note], title="t", lang=lang, exported_at=AT))
            self.assertEqual(items[0]["pin"], {"x": 1234.5, "y": 0.125})
            self.assertEqual(items[0]["region"], {"x": 100000.0, "y": 0.5, "w": 33.333, "h": 1.0})

    def test_file_stamp_matches_the_page_stamp12_format(self):
        self.assertRegex(ef.file_stamp(), r"^\d{12}$")


class JsonConsumerTests(unittest.TestCase):
    """验收 5：JSON 保留协议值；locale 元信息能被旧消费者安全忽略。"""

    def test_new_lang_key_is_ignorable_by_old_consumers(self):
        payload = ef.json_export(review_id="r" * 16, name="n", namespace="ns", notes=[], always=[], lang="en")
        self.assertEqual(payload["lang"], "en")
        old = {k: v for k, v in payload.items() if k != "lang"}   # 旧消费者只认这几个键
        self.assertEqual(old["notes"], [])
        self.assertEqual(old["alwaysSkills"], [])
        self.assertEqual(old["reviewId"], "r" * 16)

    def test_import_identity_check_still_works(self):
        src = review(tmpdir(), name="src")
        target = review(tmpdir(), name="target")
        rid_src = src.joinpath("review.json")
        rid_target = target.joinpath("review.json")
        self.assertNotEqual(json.loads(rid_src.read_text())["reviewId"],
                            json.loads(rid_target.read_text())["reviewId"])
        add_notes(src, [rich_notes()[0]])
        _code, out, _err = run_cli(["export", str(src), "--format", "json"])
        f = target / "other.json"
        f.write_text(out, encoding="utf-8")
        code, _out, err = run_cli(["import", str(target), str(f)])
        self.assertEqual(code, 2)
        self.assertIn("另一份审阅", err)
        self.assertEqual(store.load(target)["notes"], [])
        code, out, _err = run_cli(["import", str(target), str(f), "--force"])
        self.assertEqual(code, 0)
        self.assertEqual(len(store.load(target)["notes"]), 1)

    def test_dedup_and_update_still_work_after_markdown_import(self):
        d = review(tmpdir())
        md = ef.render(rich_notes(), title="t", lang="en", exported_at=AT)
        f = d / "in.md"
        f.write_text(md, encoding="utf-8")
        run_cli(["import", str(d), str(f)])
        _code, out, _err = run_cli(["import", str(d), str(f)])
        self.assertIn("跳过重复 2 条", out, "同一份导出再导入一次不应重复")

    def test_export_only_open_notes_by_default(self):
        d = review(tmpdir())
        add_notes(d, [rich_notes()[0]])
        store.set_status(d, store.load(d)["notes"][0]["id"], "resolved", resolution="已改")
        _code, out, _err = run_cli(["export", str(d)])
        self.assertNotIn("第 3 页", out, "默认只导未处理：已处理的不导")
        self.assertIn("共 1 条（未处理 0）", out)
        _code, out, _err = run_cli(["export", str(d), "--all"])
        self.assertIn("【已处理】", out)
        self.assertIn("第 3 页", out)

    def test_open_count_in_the_header_counts_drafted_as_unfinished(self):
        """「未处理」与页面一致：已初改·待复核也还没定稿，算在里面（这行只给人看，不参与解析）。"""
        d = review(tmpdir())
        add_notes(d)
        _code, out, _err = run_cli(["export", str(d)])
        self.assertIn("共 2 条（未处理 2）", out)
        self.assertIn("涉及 2 页", out)

    def test_drafted_notes_are_exported_by_default(self):
        d = review(tmpdir())
        add_notes(d)
        _code, out, _err = run_cli(["export", str(d)])
        self.assertIn("【已初改·待复核】改为：删掉整段", out)
        self.assertIn("   初改说明：已缩短", out)

    def test_cli_markdown_export_matches_the_page_grammar(self):
        """与页面 exportText() 逐行同构（时间戳那行除外）。"""
        d = review(tmpdir())
        add_notes(d)
        store.always_save(d, [{"name": "humanizer-zh", "scope": "讲稿"}])
        _code, out, _err = run_cli(["export", str(d)])
        self.assertEqual(out.splitlines()[0], "<!-- annotation-everything export v1 lang=zh-CN -->")
        self.assertEqual(out.splitlines()[1], "# 批注 · " + json.loads((d / "review.json").read_text())["name"])
        self.assertIn("报告上的标注：标点是一个点", out)
        self.assertIn("常驻技能（每条批注按范围处理前都先调用）：humanizer-zh（讲稿）", out)
        self.assertIn("## 第 3 页｜", out)
        self.assertIn("1. 【报告·标点 1（x 38.8%，y 59.8%）】把 +12%", out)
        self.assertIn("   > 引用（报告）：+12%", out)
        self.assertIn("## 第 7 页｜", out)
        self.assertIn("【已初改·待复核】改为：删掉整段", out)
        self.assertIn("   风格优化：框选处", out)
        self.assertIn("   位置线索：table「渠道」", out)
        self.assertIn("   技能：humanizer-zh、dataviz", out)
        self.assertIn("   ↔ 另一侧（报告·相同）：对应句子", out)

    def test_export_to_a_file(self):
        d = review(tmpdir())
        add_notes(d, [rich_notes()[0]])
        out_file = tmpdir() / "sub" / "out.md"
        code, out, err = run_cli(["export", str(d), "--out", str(out_file)])
        self.assertEqual(code, 0, err)
        self.assertTrue(out_file.is_file())
        self.assertIn("已导出 1 条", out)


if __name__ == "__main__":
    unittest.main()
