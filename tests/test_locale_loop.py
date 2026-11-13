"""最小审阅闭环：生成时选择语言、页面内嵌词典、CLI 按优先级读取。"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "skill" / "annotation-everything" / "scripts"
FIXTURE = ROOT / "fixtures" / "sample"
sys.path.insert(0, str(SCRIPTS))

import build_viewer  # noqa: E402
import notes_store as store  # noqa: E402


def tmpdir() -> Path:
    return Path(tempfile.mkdtemp())


def show(review: Path, args: list[str], env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    base = {k: v for k, v in os.environ.items() if k not in ("AE_LOCALE", "LC_ALL", "LC_MESSAGES", "LANG")}
    base.update(env or {})
    base["CLAUDECODE"] = ""
    return subprocess.run([sys.executable, str(SCRIPTS / "notes.py"), "show", str(review), *args],
                          capture_output=True, text=True, env=base, timeout=30)


class BuildLocaleTests(unittest.TestCase):
    def test_new_review_without_locale_stays_chinese(self):
        out = tmpdir() / "review"
        build_viewer.build(FIXTURE / "report.html", FIXTURE / "script.md", out, no_scan_skills=True)
        manifest = json.loads((out / "review.json").read_text(encoding="utf-8"))
        self.assertNotIn("locale", manifest)
        self.assertNotIn("locale", manifest["build"])
        html = (out / "index.html").read_text(encoding="utf-8")
        self.assertIn("REVIEW_LOCALE=null;", html)
        self.assertIn("const CATALOGS=", html)
        self.assertIn('"Add annotation"', html)
        self.assertIn('"添加批注"', html)
        self.assertIn('id="localePick"', html)
        self.assertIn('value="报告"', html)
        self.assertIn("ae-locale:", html)

    def test_review_stop_is_available_from_the_generated_header(self):
        with tempfile.TemporaryDirectory() as directory:
            out = Path(directory) / "review"
            build_viewer.build(FIXTURE / "report.html", FIXTURE / "script.md", out, no_scan_skills=True)
            html = (out / "index.html").read_text(encoding="utf-8")

        header = html.split("<header>", 1)[1].split("</header>", 1)[0]
        history = html.split('id="batches"', 1)[1].split('id="toast"', 1)[0]
        self.assertRegex(header, r"<button[^>]*>停止审阅</button>")
        self.assertNotIn("停止审阅", history)
        self.assertIn('"Stop review"', html)

    def test_explicit_locale_is_stored_and_survives_rebuild(self):
        out = tmpdir() / "review"
        first = build_viewer.build(FIXTURE / "report.html", FIXTURE / "script.md", out,
                                    locale="en-GB", no_scan_skills=True)
        manifest = json.loads((out / "review.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["locale"], "en")
        self.assertEqual(manifest["build"]["locale"], "en")
        self.assertIn('REVIEW_LOCALE="en";', (out / "index.html").read_text(encoding="utf-8"))
        store.add(out, {"page": 1, "text": "请改标题", "target": "报告", "quote": "原句"})
        second = build_viewer.build(FIXTURE / "report.html", FIXTURE / "script.md", out, no_scan_skills=True)
        again = json.loads((out / "review.json").read_text(encoding="utf-8"))
        self.assertEqual(again["reviewId"], first["reviewId"])
        self.assertEqual(second["reviewId"], first["reviewId"])
        self.assertEqual(again["locale"], "en")
        self.assertEqual(store.load(out)["notes"][0]["text"], "请改标题")
        self.assertEqual(store.load(out)["notes"][0]["target"], "报告")

    def test_rebuild_can_override_locale_and_auto_is_kept_unresolved(self):
        out = tmpdir() / "review"
        build_viewer.build(FIXTURE / "report.html", FIXTURE / "script.md", out, locale="zh-CN", no_scan_skills=True)
        build_viewer.build(FIXTURE / "report.html", FIXTURE / "script.md", out, locale="auto", no_scan_skills=True)
        manifest = json.loads((out / "review.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["locale"], "auto")

    def test_unsupported_locale_does_not_write_or_drop_notes(self):
        missing = tmpdir() / "missing"
        with self.assertRaises(SystemExit) as cm:
            build_viewer.build(FIXTURE / "report.html", None, missing, locale="fr", no_scan_skills=True)
        self.assertIn("不支持的语言", str(cm.exception))
        self.assertFalse(missing.exists())

        out = tmpdir() / "review"
        build_viewer.build(FIXTURE / "report.html", FIXTURE / "script.md", out, no_scan_skills=True)
        store.add(out, {"page": 1, "text": "留下来", "target": "报告"})
        manifest = json.loads((out / "review.json").read_text(encoding="utf-8"))
        manifest["locale"] = "fr"
        (out / "review.json").write_text(json.dumps(manifest), encoding="utf-8")
        with self.assertRaises(SystemExit):
            build_viewer.build(FIXTURE / "report.html", FIXTURE / "script.md", out, no_scan_skills=True)
        self.assertEqual(store.load(out)["notes"][0]["text"], "留下来")


class ShowLocaleTests(unittest.TestCase):
    def setUp(self):
        self.out = tmpdir() / "review"
        build_viewer.build(FIXTURE / "report.html", FIXTURE / "script.md", self.out, no_scan_skills=True)
        store.add(self.out, {"page": 1, "text": "请改标题", "target": "报告", "quote": "原句"})
        store.add(self.out, {"page": 1, "text": "第二条", "target": "讲稿"})

    def test_default_show_stays_chinese_and_json_keeps_values(self):
        text = show(self.out, ["--all"]).stdout
        self.assertIn("# 批注 · ", text)
        self.assertIn("请改标题", text)
        self.assertIn("【报告】", text)
        self.assertNotIn("Annotations", text)
        raw = json.loads(show(self.out, ["--json", "--all"]).stdout)
        self.assertEqual(raw["notes"][0]["text"], "请改标题")
        self.assertEqual(raw["notes"][0]["target"], "报告")
        self.assertEqual(raw["notes"][0]["quote"], "原句")

    def test_explicit_locale_beats_environment_and_review(self):
        manifest = json.loads((self.out / "review.json").read_text(encoding="utf-8"))
        manifest["locale"] = "zh-CN"
        (self.out / "review.json").write_text(json.dumps(manifest), encoding="utf-8")
        text = show(self.out, ["--all", "--locale", "en"], {"AE_LOCALE": "zh-CN"}).stdout
        self.assertIn("Annotations · ", text)
        self.assertIn("2 annotations", text)
        self.assertIn("请改标题", text)
        self.assertIn("原句", text)
        self.assertIn("[Report]", text)
        self.assertIn("[Speaker notes]", text)
        self.assertNotIn("【报告】", text)
        raw = json.loads(show(self.out, ["--json", "--all", "--locale", "en"], {"AE_LOCALE": "zh-CN"}).stdout)
        self.assertEqual([n["target"] for n in raw["notes"]], ["报告", "讲稿"])

    def test_environment_beats_review_and_review_beats_default(self):
        manifest = json.loads((self.out / "review.json").read_text(encoding="utf-8"))
        manifest["locale"] = "en"
        (self.out / "review.json").write_text(json.dumps(manifest), encoding="utf-8")
        from_review = show(self.out, ["--all"]).stdout
        self.assertIn("Annotations", from_review)
        self.assertIn("2 annotations", from_review)
        from_env = show(self.out, ["--all"], {"AE_LOCALE": "zh-CN"}).stdout
        self.assertIn("# 批注 · ", from_env)
        self.assertNotIn("Annotations", from_env)

    def test_english_show_counts_drafted_notes(self):
        """一条未处理加一条已初改，不能显示成 (1 open, 0 resolved)。"""
        out = tmpdir() / "mixed"
        build_viewer.build(FIXTURE / "report.html", None, out, locale="en", no_scan_skills=True)
        store.add(out, {"page": 1, "text": "还没改", "target": "报告"})
        _doc, drafted = store.add(out, {"page": 1, "text": "初改过", "target": "讲稿"})
        store.set_status(out, drafted["id"], "drafted", by="agent")
        text = show(out, ["--all"]).stdout
        self.assertIn(
            "2 annotations (1 open, 1 drafted, waiting for review, 0 resolved)",
            text,
        )
        self.assertNotIn("(1 open, 0 resolved)", text)
        self.assertIn("初改过", text)
        chinese = show(out, ["--all", "--locale", "zh-CN"]).stdout
        self.assertIn("共 2 条（未处理 1，已初改·待复核 1，已处理 0）", chinese)
        self.assertNotIn("drafted", chinese)

    def test_one_annotation_uses_singular(self):
        out = tmpdir() / "one"
        build_viewer.build(FIXTURE / "report.html", None, out, locale="en", no_scan_skills=True)
        store.add(out, {"page": 1, "text": "只此一条", "target": "报告"})
        text = show(out, ["--all"]).stdout
        self.assertIn("1 annotation", text)
        self.assertNotIn("1 annotations", text)
        self.assertIn("只此一条", text)

    def test_english_show_keeps_braces_in_title_and_source_paths(self):
        src = tmpdir() / "{bar}"
        src.mkdir()
        report = src / "report.html"
        script = src / "script.md"
        report.write_text((FIXTURE / "report.html").read_text(encoding="utf-8"), encoding="utf-8")
        script.write_text((FIXTURE / "script.md").read_text(encoding="utf-8"), encoding="utf-8")
        out = tmpdir() / "review"
        build_viewer.build(report, script, out, title="Q3 {draft} 与 } 尾", locale="en", no_scan_skills=True)
        store.add(out, {"page": 1, "text": "正文 {keep}", "target": "报告", "quote": "引用 {q}"})
        text = show(out, ["--all"]).stdout
        self.assertIn("Annotations · Q3 {draft} 与 } 尾", text)
        self.assertNotIn("{{draft}}", text)
        self.assertNotIn("{{bar}}", text)
        self.assertIn(str(report.resolve()), text)
        self.assertIn("正文 {keep}", text)
        self.assertIn("引用 {q}", text)

    def test_bad_locale_is_diagnosed_and_keeps_notes(self):
        before = (self.out / "notes.json").read_text(encoding="utf-8")
        result = show(self.out, ["--locale", "fr"])
        self.assertEqual(result.returncode, 2)
        self.assertIn("不支持的语言", result.stderr)
        self.assertEqual((self.out / "notes.json").read_text(encoding="utf-8"), before)

    def test_auto_uses_system_languages_only_when_asked(self):
        manifest = json.loads((self.out / "review.json").read_text(encoding="utf-8"))
        manifest["locale"] = "auto"
        (self.out / "review.json").write_text(json.dumps(manifest), encoding="utf-8")
        english = show(self.out, [], {"LC_ALL": "en_US.UTF-8"}).stdout
        self.assertIn("Annotations", english)
        chinese = show(self.out, [], {"LC_ALL": "zh_CN.UTF-8"}).stdout
        self.assertIn("# 批注 · ", chinese)
        unmatched = show(self.out, [], {"LC_ALL": "fr_FR.UTF-8"}).stdout
        self.assertIn("Annotations", unmatched)
        untouched = show(self.out, [], {"LC_ALL": "en_US.UTF-8", "AE_LOCALE": ""})
        # 审阅是 auto，空的 AE_LOCALE 不算设置，仍然按系统语言
        self.assertIn("Annotations", untouched.stdout)


class InstalledCopyTests(unittest.TestCase):
    def test_skill_copy_includes_both_catalogs(self):
        sys.path.insert(0, str(ROOT))
        import ae
        dst = tmpdir() / "annotation-everything"
        ae.copy_skill(ROOT / "skill" / "annotation-everything", dst)
        self.assertTrue((dst / "locales" / "zh-CN.json").is_file())
        self.assertTrue((dst / "locales" / "en.json").is_file())
        self.assertTrue((dst / "scripts" / "i18n.py").is_file())


if __name__ == "__main__":
    unittest.main()
