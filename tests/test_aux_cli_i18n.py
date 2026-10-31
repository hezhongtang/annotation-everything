"""字体检查与技能扫描的语言选择：人读文案按语言变，字体名与路径保持原文。"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
CHECKER = ROOT / "skill" / "annotation-everything" / "scripts" / "check_pptx_fonts.py"
SCRIPTS = CHECKER.parent
sys.path.insert(0, str(SCRIPTS))

import ledger  # noqa: E402
import skill_sources  # noqa: E402


class FontCliLanguageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="font-i18n-test-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.bin = self.root / "bin"
        self.bin.mkdir()
        self.pptx = self.root / "synthetic.pptx"
        self.env = {k: v for k, v in os.environ.items()
                    if k not in ("AE_LOCALE", "LC_ALL", "LC_MESSAGES", "LANG")}
        self.env.update({"HOME": str(self.root / "home"), "PATH": str(self.bin) + os.pathsep + os.defpath,
                         "PYTHONDONTWRITEBYTECODE": "1"})

    def tool(self, name: str, script: str):
        path = self.bin / name
        path.write_text("#!/bin/sh\n" + script + "\n", encoding="utf-8")
        path.chmod(0o755)

    def make_pptx(self):
        with zipfile.ZipFile(self.pptx, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("ppt/slides/slide1.xml", '''<p:sld xmlns:p="urn:p" xmlns:a="urn:a">
              <a:r><a:rPr><a:latin typeface="Synthetic Sans"/><a:ea typeface="Missing Family"/></a:rPr></a:r>
            </p:sld>''')

    def run_check(self, *args: str, **env):
        merged = {**self.env, **env}
        return subprocess.run([sys.executable, str(CHECKER), str(self.pptx), *map(str, args)],
                              env=merged, capture_output=True, text=True, timeout=60)

    def test_default_stays_chinese(self):
        self.make_pptx()
        self.tool("fc-list", "printf '%s\\n' 'Synthetic Sans,Synthetic Sans Regular'")
        p = self.run_check()
        self.assertEqual(p.returncode, 1, p.stdout + p.stderr)
        self.assertIn("PPTX 字体声明：", p.stdout)
        self.assertIn("Synthetic Sans：本次枚举找到", p.stdout)
        self.assertIn("Missing Family：本次枚举未检出", p.stdout)

    def test_locale_flag_localizes_but_keeps_font_names(self):
        self.make_pptx()
        self.tool("fc-list", "printf '%s\\n' 'Synthetic Sans,Synthetic Sans Regular'")
        p = self.run_check("--locale", "en")
        self.assertEqual(p.returncode, 1, p.stdout + p.stderr)
        self.assertIn("Synthetic Sans", p.stdout)
        self.assertIn("Missing Family", p.stdout)
        self.assertIn("found in this listing", p.stdout)
        self.assertIn("not in this listing", p.stdout)
        p = self.run_check()
        self.assertEqual(p.returncode, 1, p.stdout + p.stderr)
        self.assertIn("Synthetic Sans", p.stdout)
        self.assertIn("Missing Family", p.stdout)
        self.assertIn("本次枚举找到", p.stdout)
        self.assertIn("本次枚举未检出", p.stdout)

    def test_locale_flag_position_and_env(self):
        self.make_pptx()
        self.tool("fc-list", "printf '%s\\n' 'Synthetic Sans'")
        p = self.run_check("--locale", "en")
        self.assertIn("Fonts declared in the PPTX:", p.stdout)
        p = self.run_check(AE_LOCALE="en")
        self.assertIn("Fonts declared in the PPTX:", p.stdout)
        p = self.run_check("--locale", "zh-CN", AE_LOCALE="en")
        self.assertIn("PPTX 字体声明：", p.stdout)

    def test_help_is_localized(self):
        p = subprocess.run([sys.executable, str(CHECKER), "--help", "--locale", "en"],
                           env=self.env, capture_output=True, text=True, timeout=60)
        self.assertEqual(p.returncode, 0)
        self.assertIn("Check the fonts declared in a PPTX", p.stdout)
        p = subprocess.run([sys.executable, str(CHECKER), "--help"],
                           env=self.env, capture_output=True, text=True, timeout=60)
        self.assertIn("检查 PPTX 声明字体", p.stdout)

    def test_unsupported_locale_is_command_error(self):
        p = subprocess.run([sys.executable, str(CHECKER), str(self.pptx), "--locale", "fr"],
                           env=self.env, capture_output=True, text=True, timeout=60)
        self.assertEqual(p.returncode, 2)
        self.assertTrue(p.stderr.strip())
        self.assertEqual(p.stdout, "")

    def test_unrecognized_argument_is_localized_and_exits_two(self):
        """R1 回归：这条路径曾经缺 detail 参数，抛 MessageFormatError 并返回 1。"""
        self.make_pptx()
        self.tool("fc-list", "printf '%s\\n' 'Synthetic Sans'")
        p = self.run_check("--nope")
        self.assertEqual(p.returncode, 2, p.stdout + p.stderr)
        self.assertIn("无法识别的参数", p.stderr)
        self.assertNotIn("Traceback", p.stderr)
        p = self.run_check("--nope", "--locale", "en")
        self.assertEqual(p.returncode, 2, p.stdout + p.stderr)
        self.assertIn("Unrecognized argument", p.stderr)
        self.assertNotIn("Traceback", p.stderr)

    def test_missing_tool_error_is_localized_and_keeps_command(self):
        """工具缺失时的本地化错误。PATH 只留临时目录：CI 的 Ubuntu 自带 fc-list
        （在 /usr/bin），不隔离就会走到「枚举成功」那条分支，断言随之落空。"""
        self.make_pptx()
        p = self.run_check("--locale", "en", PATH=str(self.bin))
        self.assertEqual(p.returncode, 2, p.stdout + p.stderr)
        self.assertIn("fc-list", p.stderr)
        self.assertIn("fontconfig", p.stderr)
        p = self.run_check(PATH=str(self.bin))
        self.assertIn("找不到 fc-list", p.stderr)


class SkillScanLanguageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="skill-i18n-test-")
        self.addCleanup(self.temp.cleanup)
        # 别的测试可能在进程内定过语言（set_locale 会定住）；先解开，AE_LOCALE 才说得上话
        self.addCleanup(skill_sources.set_locale, None)
        skill_sources.set_locale(None)
        self.root = Path(self.temp.name)
        self.source = self.root / "skills"
        (self.source / "broken").mkdir(parents=True)
        (self.source / "broken" / "SKILL.md").write_text("没有 frontmatter\n", encoding="utf-8")

    def warnings(self, **env):
        old = os.environ.get("AE_LOCALE")
        if "AE_LOCALE" in env:
            os.environ["AE_LOCALE"] = env["AE_LOCALE"]
        try:
            _results, warnings = skill_sources.scan([self.source])
        finally:
            if old is None:
                os.environ.pop("AE_LOCALE", None)
            else:
                os.environ["AE_LOCALE"] = old
        return warnings

    def test_warning_follows_env_locale(self):
        zh = self.warnings()
        self.assertTrue(zh and any("缺少 frontmatter" in w for w in zh), zh)
        en = self.warnings(AE_LOCALE="en")
        self.assertTrue(en and any("Missing frontmatter" in w for w in en), en)
        self.assertIn(str(self.source / "broken" / "SKILL.md"), en[0], "来源路径不翻译")

    def test_default_is_chinese(self):
        os.environ.pop("AE_LOCALE", None)
        _results, warnings = skill_sources.scan([self.source])
        self.assertTrue(any("保留" in w for w in warnings), warnings)

    def test_build_scan_uses_explicit_and_saved_page_language(self):
        for locale, fallback, message in (("en", "zh-CN", "Missing frontmatter"),
                                           ("zh-CN", "en", "缺少 frontmatter")):
            with self.subTest(locale=locale):
                out = self.root / locale
                env = {**os.environ, "AE_HOME": str(self.root / "ledger"), "AE_LOCALE": fallback}
                args = ["--report", str(ROOT / "fixtures/sample/report.html"),
                        "--script", str(ROOT / "fixtures/sample/script.md"),
                        "--out", str(out), "--locale", locale, "--skills-from", str(self.source)]
                for command, run_env in ((args, env), (["--rebuild", str(out)], {k:v for k,v in env.items() if k != "AE_LOCALE"})):
                    p = subprocess.run([sys.executable, str(SCRIPTS / "build_viewer.py"), *command],
                                       env=run_env, capture_output=True, text=True)
                    self.assertEqual(p.returncode, 0, p.stderr)
                    self.assertIn(message, p.stdout + p.stderr)
                    self.assertIn(str(self.source / "broken" / "SKILL.md"), p.stdout + p.stderr)
                overridden = subprocess.run([sys.executable, str(SCRIPTS / "build_viewer.py"), "--rebuild", str(out)],
                                            env=env, capture_output=True, text=True)
                self.assertEqual(overridden.returncode, 0, overridden.stderr)
                self.assertIn("Missing frontmatter" if fallback == "en" else "缺少 frontmatter",
                              overridden.stdout + overridden.stderr)
                self.assertEqual(json.loads((out / "review.json").read_text(encoding="utf-8"))["locale"], fallback)

    def test_not_skill_file_error_is_localized(self):
        other = self.source / "broken" / "README.md"
        other.write_text("x", encoding="utf-8")
        os.environ.pop("AE_LOCALE", None)
        with self.assertRaises(ValueError) as cm:
            skill_sources._skill_files(other)
        self.assertIn("来源文件须为 SKILL.md", str(cm.exception))
        os.environ["AE_LOCALE"] = "en"
        try:
            with self.assertRaises(ValueError) as cm:
                skill_sources._skill_files(other)
        finally:
            os.environ.pop("AE_LOCALE", None)
        self.assertIn("The source file must be SKILL.md", str(cm.exception))

    def test_set_locale_pins_the_language_over_ae_locale(self):
        """调用方已解析完语言时定住，AE_LOCALE 不再翻案——否则同一进程里中英混排。"""
        os.environ["AE_LOCALE"] = "en"
        try:
            skill_sources.set_locale("zh-CN")
            _results, warnings = skill_sources.scan([self.source])
            self.assertTrue(any("缺少 frontmatter" in w for w in warnings), warnings)
        finally:
            os.environ.pop("AE_LOCALE", None)
            skill_sources.set_locale(None)


class LedgerErrorLanguageTests(unittest.TestCase):
    """台账错误：原因与拒绝条件不变，人读文案按语言，路径与标识保持原文。"""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="ledger-i18n-test-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.home = self.root / "ledger"
        self.home.mkdir()
        self.saved = os.environ.get("AE_HOME")
        os.environ["AE_HOME"] = str(self.home)
        self.addCleanup(ledger.set_locale, None)
        ledger.set_locale(None)
        self.addCleanup(self._restore)

    def _restore(self):
        if self.saved is None:
            os.environ.pop("AE_HOME", None)
        else:
            os.environ["AE_HOME"] = self.saved
        os.environ.pop("AE_LOCALE", None)

    def message(self, **env):
        saved = os.environ.get("AE_LOCALE")
        os.environ.pop("AE_LOCALE", None)
        os.environ.update(env)
        try:
            with self.assertRaises(ledger.LedgerError) as cm:
                ledger.match("ffff")
            return str(cm.exception)
        finally:
            os.environ.pop("AE_LOCALE", None)
            if saved is not None:
                os.environ["AE_LOCALE"] = saved

    def test_error_message_follows_env_locale_and_keeps_the_key(self):
        zh = self.message()
        self.assertIn("审阅台账里没有标识以 ffff 开头的审阅", zh)
        self.assertIn("serve.py list", zh)
        en = self.message(AE_LOCALE="en")
        self.assertIn("No review in the ledger starts with ffff", en)
        self.assertIn("serve.py list", en)

    def test_unreadable_ledger_keeps_path_and_type(self):
        (self.home / "reviews.json").write_text("{坏的", encoding="utf-8")
        saved = os.environ.get("AE_LOCALE")
        os.environ.pop("AE_LOCALE", None)
        try:
            with self.assertRaises(ledger.LedgerError) as cm:
                ledger.entries()
            zh = str(cm.exception)
        finally:
            os.environ.pop("AE_LOCALE", None)
            if saved is not None:
                os.environ["AE_LOCALE"] = saved
        self.assertIn(str(self.home / "reviews.json"), zh, "台账路径不翻译")
        self.assertIn("JSONDecodeError", zh, "系统错误类型保持原文")

    def test_unsupported_env_locale_keeps_chinese_instead_of_failing(self):
        text = self.message(AE_LOCALE="ja")
        self.assertIn("审阅台账里没有标识以 ffff 开头的审阅", text)


if __name__ == "__main__":
    unittest.main()
