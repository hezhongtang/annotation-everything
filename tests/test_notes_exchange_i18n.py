"""导入导出 CLI 的双语交付：参数、反馈、错误与原始数据分别保持契约。"""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "skill" / "annotation-everything" / "scripts"


class NotesExchangeLanguageTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix="ae-exchange-language-")
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.review = self.root / "review"
        self.env = {**os.environ, "AE_HOME": str(self.root / "ledger"), "AE_LOCALE": "zh-CN"}
        built = subprocess.run([sys.executable, str(SCRIPTS / "build_viewer.py"), "--report",
                                str(ROOT / "fixtures" / "sample" / "report.html"), "--out", str(self.review),
                                "--no-scan-skills"], env=self.env, capture_output=True, text=True, timeout=30)
        self.assertEqual(built.returncode, 0, built.stderr)
        self.input = self.root / "notes.json"
        self.input.write_text(json.dumps({"notes": [{"id": "example", "page": 1, "target": "报告",
                                                     "text": "原文 <tag> {count}"}]}), encoding="utf-8")

    def run_cli(self, *args):
        return subprocess.run([sys.executable, str(SCRIPTS / "notes.py"), *map(str, args)],
                              env=self.env, capture_output=True, text=True, timeout=30)

    def test_english_import_feedback_keeps_original_protocol(self):
        result = self.run_cli("import", self.review, self.input, "--locale", "en")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Import complete", result.stdout)
        self.assertIn("added 1", result.stdout)
        self.assertNotRegex(result.stdout, r"[一-鿿]")
        raw = json.loads(self.run_cli("show", self.review, "--json").stdout)["notes"][0]
        self.assertEqual((raw["text"], raw["target"], raw["id"]), ("原文 <tag> {count}", "报告", "example"))

    def test_invalid_import_locale_does_not_write_annotations(self):
        result = self.run_cli("import", self.review, self.input, "--locale", "fr")
        self.assertEqual(result.returncode, 2)
        self.assertFalse((self.review / "notes.json").exists())

    def test_import_identity_error_is_english_and_does_not_write(self):
        self.input.write_text(json.dumps({"reviewId": "0123456789abcdef", "notes": []}), encoding="utf-8")
        result = self.run_cli("--locale", "en", "import", self.review, self.input)
        self.assertEqual(result.returncode, 2)
        self.assertIn("another review", result.stderr)
        self.assertIn("--force", result.stderr)
        self.assertNotRegex(result.stderr, r"[一-鿿]")
        self.assertFalse((self.review / "notes.json").exists())

    def test_bad_markdown_gives_english_recovery_without_state_changes(self):
        bad = self.root / "annotations.md"
        bad.write_text("unrecognized text", encoding="utf-8")
        result = self.run_cli("import", self.review, bad, "--locale", "en")
        self.assertEqual(result.returncode, 2)
        self.assertIn("Markdown", result.stderr)
        self.assertNotRegex(result.stderr, r"[一-鿿]")
        self.assertFalse((self.review / "notes.json").exists())

    def test_explicit_language_localizes_help_and_export_feedback(self):
        for command in ("import", "export", "wait", "show", "reopen"):
            with self.subTest(command=command):
                result = self.run_cli(command, "--help", "--locale", "en")
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertNotRegex(result.stdout, r"[一-鿿]")
        target = self.root / "annotations.md"
        result = self.run_cli("export", self.review, "--lang", "en", "--out", target)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Exported", result.stdout)
        self.assertNotRegex(result.stdout, r"[一-鿿]")

    def test_english_generation_localizes_results_help_and_asset_warnings(self):
        report = self.root / "english.html"
        text = (ROOT / "fixtures" / "sample" / "report.en.html").read_text(encoding="utf-8")
        report.write_text(text.replace("<h2>Agenda</h2>",
                          '<h2>Agenda</h2><img src="https://example.invalid/demo.png">'), encoding="utf-8")
        command = [sys.executable, str(SCRIPTS / "build_viewer.py"), "--locale", "en"]
        help_result = subprocess.run([*command, "--help"], env=self.env, capture_output=True, text=True, timeout=30)
        self.assertEqual(help_result.returncode, 0)
        self.assertNotRegex(help_result.stdout, r"[一-鿿]")
        result = subprocess.run([*command, "--report", str(report), "--out", str(self.root / "english-review"),
                                 "--no-scan-skills"], env=self.env, capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("pages: 6", result.stdout)
        self.assertIn("example.invalid", result.stderr)
        self.assertNotRegex(result.stdout + result.stderr, r"[一-鿿]")


if __name__ == "__main__":
    unittest.main()
