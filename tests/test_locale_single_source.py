"""界面语言只有一个进程级来源：入口设置一次，各模块共同遵循。

对应 issue #95。词典适配层（i18n）持进程级的「定住语言 / 按 AE_LOCALE 刷新」状态，
ledger、skill_sources、serve、check_pptx_fonts 的 T 都从它取；ae.py 的 set_locale 不再
逐个模块传递。两层断言：

- 进程内：定住后不被 AE_LOCALE 覆盖、没定住时按 AE_LOCALE 刷新、非法时退回中文；
  任何模块调 set_locale(None) 都解除整个进程的定住状态（运行时代码不传 None，只有测试会）。
- 进程外：五个入口各自只设置一次语言，来自底层模块（台账、技能发现、词典适配层）的
  报错都用同一种语言。

进程内的探针都取用户能看到的输出：台账抛出的 LedgerError 文案、技能扫描警告、字体检查
抛出的 CheckError 文案、serve 解析 --on-handoff 时的退出说明。台账与技能扫描的入口会按
AE_LOCALE 刷新共享状态，其余模块随之同语言——这正是「只有一个来源」要保证的。

只用标准库与合成数据：python3 -m unittest discover tests
"""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "skill" / "annotation-everything" / "scripts"
SAMPLE = ROOT / "fixtures" / "sample"
AE = ROOT / "ae.py"
sys.path.insert(0, str(SCRIPTS))

import check_pptx_fonts  # noqa: E402
import i18n  # noqa: E402
import ledger  # noqa: E402
import serve  # noqa: E402
import skill_sources  # noqa: E402

# 各探针的中英文措辞：只取能区分语言的那一小段，路径与系统错误类型不列入
LEDGER_RID = {"zh-CN": "审阅标识不合法", "en": "Illegal review id"}
SKILL_FRONTMATTER = {"zh-CN": "缺少 frontmatter", "en": "Missing frontmatter"}
FONTS_PPTX = {"zh-CN": "无法读取 PPTX", "en": "Cannot read the PPTX"}
HOOK_SYNTAX = {"zh-CN": "--on-handoff 的命令写法不对", "en": "The --on-handoff command is malformed"}
# 入口报错里来自底层模块的那段
LEDGER_MISSING = {"zh-CN": "审阅台账里没有标识以 ffff 开头的审阅",
                  "en": "No review in the ledger starts with ffff"}
LEDGER_UNREADABLE = {"zh-CN": "读不了（JSONDecodeError）", "en": "Cannot read the review ledger"}
LEDGER_REGISTER = {"zh-CN": "没能登记到审阅台账", "en": "Could not register in the review ledger"}


class LocaleStateTestCase(unittest.TestCase):
    """进程内语言状态：每个用例结束都解除定住，用例之间不互相污染。"""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="locale-source-test-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.home = self.root / "home"
        self.home.mkdir()
        self.ledger_home = self.root / "ledger"
        self.ledger_home.mkdir()
        self.skills = self.root / "skills"
        (self.skills / "broken").mkdir(parents=True)
        (self.skills / "broken" / "SKILL.md").write_text("没有 frontmatter\n", encoding="utf-8")
        self.saved = {name: os.environ.get(name)
                      for name in ("AE_LOCALE", "AE_HOME", "LC_ALL", "LC_MESSAGES", "LANG")}
        for name in ("AE_LOCALE", "LC_ALL", "LC_MESSAGES", "LANG"):
            os.environ.pop(name, None)
        os.environ["AE_HOME"] = str(self.ledger_home)
        self.addCleanup(self._restore_env)
        # 界面语言是进程级状态，别的模块可能在本进程里定过语言：每条用例都先从「未定住」
        # 开始，用例之间才不互相污染。运行时代码不传 None，只有测试会传
        i18n.set_locale(None)
        self.addCleanup(i18n.set_locale, None)

    def _restore_env(self):
        for name, value in self.saved.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value

    def ledger_error(self) -> str:
        with self.assertRaises(ledger.LedgerError) as cm:
            ledger.register("不是标识", self.root)
        return str(cm.exception)

    def skill_warnings(self) -> list:
        _results, warnings = skill_sources.scan([self.skills])
        return warnings

    def font_error(self) -> str:
        with self.assertRaises(check_pptx_fonts.CheckError) as cm:
            check_pptx_fonts.declared_fonts(self.root / "missing.pptx")
        return str(cm.exception)

    def hook_error(self) -> str:
        with self.assertRaises(SystemExit) as cm:
            serve.parse_hook("a 'b")
        return str(cm.exception)


class ProcessLocalePinTests(LocaleStateTestCase):
    """pin 三条语义：定住不翻案、没定住按 AE_LOCALE 刷新、非法退回中文。"""

    def test_pinned_language_is_not_overwritten_by_ae_locale(self):
        """入口定住语言后，AE_LOCALE 不再翻案——否则同一进程里中英混排。"""
        os.environ["AE_LOCALE"] = "en"
        serve.set_locale("zh-CN")
        self.assertIn(LEDGER_RID["zh-CN"], self.ledger_error())
        self.assertTrue(any(SKILL_FRONTMATTER["zh-CN"] in w for w in self.skill_warnings()))
        self.assertIn(FONTS_PPTX["zh-CN"], self.font_error())
        self.assertIn(HOOK_SYNTAX["zh-CN"], self.hook_error())

    def test_any_module_pinning_holds_for_the_whole_process(self):
        """任何一个模块定住语言，全进程各模块都用它；按模块分开保存并没有实际隔离。"""
        os.environ["AE_LOCALE"] = "en"
        for name, pin in (("ledger", ledger.set_locale), ("skill_sources", skill_sources.set_locale),
                          ("serve", serve.set_locale), ("check_pptx_fonts", check_pptx_fonts.set_locale)):
            with self.subTest(pinned_by=name):
                pin("zh-CN")
                self.assertIn(LEDGER_RID["zh-CN"], self.ledger_error())
                self.assertTrue(any(SKILL_FRONTMATTER["zh-CN"] in w for w in self.skill_warnings()))
                self.assertIn(FONTS_PPTX["zh-CN"], self.font_error())
                self.assertIn(HOOK_SYNTAX["zh-CN"], self.hook_error())

    def test_unpinned_language_follows_ae_locale(self):
        os.environ["AE_LOCALE"] = "en"
        # 台账入口先按 AE_LOCALE 刷新共享状态，其余模块随之同语言
        self.assertIn(LEDGER_RID["en"], self.ledger_error())
        self.assertTrue(any(SKILL_FRONTMATTER["en"] in w for w in self.skill_warnings()))
        self.assertIn(FONTS_PPTX["en"], self.font_error())
        self.assertIn(HOOK_SYNTAX["en"], self.hook_error())

    def test_unsupported_ae_locale_falls_back_to_chinese(self):
        os.environ["AE_LOCALE"] = "ja"
        self.assertIn(LEDGER_RID["zh-CN"], self.ledger_error())
        self.assertTrue(any(SKILL_FRONTMATTER["zh-CN"] in w for w in self.skill_warnings()))
        self.assertIn(FONTS_PPTX["zh-CN"], self.font_error())
        self.assertIn(HOOK_SYNTAX["zh-CN"], self.hook_error())

    def test_set_locale_none_releases_the_pin_for_the_whole_process(self):
        """新语义：任何模块 set_locale(None) 都解除整个进程的定住状态，不只是它自己。"""
        os.environ["AE_LOCALE"] = "en"
        serve.set_locale("zh-CN")
        self.assertIn(LEDGER_RID["zh-CN"], self.ledger_error())
        skill_sources.set_locale(None)
        self.assertIn(LEDGER_RID["en"], self.ledger_error())
        self.assertIn(FONTS_PPTX["en"], self.font_error())
        self.assertIn(HOOK_SYNTAX["en"], self.hook_error())


class EntryLanguageTests(unittest.TestCase):
    """五个入口各自只设置一次语言，来自底层模块的报错都用同一种语言。"""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="entry-locale-test-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.home = self.root / "home"
        self.home.mkdir()
        # 空台账（合法、没有审阅）：serve / notes 按标识找不到审阅时报台账的错
        self.ledger_home = self.root / "ledger"
        self.ledger_home.mkdir()
        # 台账读不了：登记与体检时报「读不了」的错，两种台账各覆盖一种底层报错
        self.broken_home = self.root / "broken-ledger"
        self.broken_home.mkdir()
        (self.broken_home / "reviews.json").write_text("{坏的", encoding="utf-8")
        self.skills = self.root / "skills"
        (self.skills / "broken").mkdir(parents=True)
        (self.skills / "broken" / "SKILL.md").write_text("没有 frontmatter\n", encoding="utf-8")
        # PATH 只留一个空目录：字体检查与 ps 的输出不随机器变
        self.bin = self.root / "bin"
        self.bin.mkdir()
        ps = self.bin / "ps"
        ps.write_text('#!/bin/sh\nif [ "$1 $2" = "-ww -eo" ]; then\n  exit 0\n'
                      'else\n  exec /bin/ps "$@"\nfi\n', encoding="utf-8")
        ps.chmod(0o755)
        self.env = {k: v for k, v in os.environ.items()
                    if k not in ("AE_LOCALE", "LC_ALL", "LC_MESSAGES", "LANG")}
        self.env.update({"HOME": str(self.home), "AE_HOME": str(self.ledger_home),
                         "CLAUDE_SKILLS_DIR": str(self.home / ".claude" / "skills"),
                         "CODEX_HOME": str(self.home / ".codex"),
                         "PATH": str(self.bin) + os.pathsep + os.defpath,
                         "PYTHONDONTWRITEBYTECODE": "1"})

    def run_entry(self, script: Path, *args: str, broken_ledger: bool = False, **extra):
        merged = {**self.env, **extra}
        if broken_ledger:
            merged["AE_HOME"] = str(self.broken_home)
        return subprocess.run([sys.executable, str(script), *map(str, args)], env=merged,
                              capture_output=True, text=True, timeout=180)

    def out_dir(self, name: str) -> Path:
        d = self.root / name
        d.mkdir(parents=True, exist_ok=True)
        return d

    def test_serve_ledger_error_follows_the_single_language(self):
        for locale, needle in (("zh-CN", LEDGER_MISSING["zh-CN"]), ("en", LEDGER_MISSING["en"])):
            with self.subTest(locale=locale):
                p = self.run_entry(SCRIPTS / "serve.py", "forget", "ffff", "--locale", locale)
                self.assertEqual(p.returncode, 2, p.stdout + p.stderr)
                self.assertIn(needle, p.stderr)

    def test_notes_ledger_error_follows_the_single_language(self):
        for locale, needle in (("zh-CN", LEDGER_MISSING["zh-CN"]), ("en", LEDGER_MISSING["en"])):
            with self.subTest(locale=locale):
                p = self.run_entry(SCRIPTS / "notes.py", "show", "ffff", "--locale", locale)
                self.assertEqual(p.returncode, 2, p.stdout + p.stderr)
                self.assertIn(needle, p.stderr)

    def test_build_viewer_ledger_and_skill_errors_follow_the_single_language(self):
        for locale, ledger_needle, skill_needle in (
                ("zh-CN", LEDGER_REGISTER["zh-CN"], SKILL_FRONTMATTER["zh-CN"]),
                ("en", LEDGER_REGISTER["en"], SKILL_FRONTMATTER["en"])):
            with self.subTest(locale=locale):
                out = self.out_dir("out-" + locale)
                p = self.run_entry(SCRIPTS / "build_viewer.py", "--report", SAMPLE / "report.html",
                                   "--script", SAMPLE / "script.md", "--out", out,
                                   "--skills-from", self.skills, "--locale", locale,
                                   broken_ledger=True)
                self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
                self.assertIn(ledger_needle, p.stderr, "台账登记失败也用同一语言")
                self.assertIn(skill_needle, p.stderr, "技能扫描警告也用同一语言")
                self.assertIn(str(self.skills / "broken" / "SKILL.md"), p.stderr, "来源路径不翻译")

    def test_ae_ledger_error_follows_the_single_language(self):
        for locale, needle in (("zh-CN", LEDGER_UNREADABLE["zh-CN"]), ("en", LEDGER_UNREADABLE["en"])):
            with self.subTest(locale=locale):
                p = self.run_entry(AE, "doctor", "--locale", locale, broken_ledger=True)
                self.assertIn(p.returncode, (0, 1), p.stdout + p.stderr)
                self.assertIn(needle, p.stdout)

    def test_check_pptx_fonts_error_follows_the_single_language(self):
        """字体检查没有台账与技能发现依赖，它的报错经词典适配层（进程级状态）出来。"""
        for locale, needle in (("zh-CN", FONTS_PPTX["zh-CN"]), ("en", FONTS_PPTX["en"])):
            with self.subTest(locale=locale):
                p = self.run_entry(SCRIPTS / "check_pptx_fonts.py",
                                   self.root / "missing.pptx", "--locale", locale)
                self.assertEqual(p.returncode, 2, p.stdout + p.stderr)
                self.assertIn(needle, p.stderr)


if __name__ == "__main__":
    unittest.main()
