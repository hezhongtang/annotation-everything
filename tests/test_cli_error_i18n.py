"""四个入口的 CLI 语言样板：argparse 报错、语言选择与 --locale 位置的中英输出。

对应 issue #94：报错分类、本地化解析器和「语言选择为空或非法」报告只有一份实现
（i18n），serve、update、check_pptx_fonts、ae.py 只传自己的消息 key 表。这里用
子进程跑真实脚本，逐字钉住中英两种语言下的 stderr 与退出码；合成的旧版技能副本
用来验证 ae.py 在副本还没有公共实现时退回内置最小实现。

只用标准库：python3 -m unittest discover tests
"""
from __future__ import annotations

import ast
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
AE = ROOT / "ae.py"
SCRIPTS = ROOT / "skill" / "annotation-everything" / "scripts"
SKILL_DIR = SCRIPTS.parent
SERVE_PY = SCRIPTS / "serve.py"
UPDATE_PY = SCRIPTS / "update.py"
FONTS_PY = SCRIPTS / "check_pptx_fonts.py"
SCRIPTS_BY_ENTRY = {"serve": SERVE_PY, "update": UPDATE_PY, "fonts": FONTS_PY, "ae": AE}

# 运行时脚本：技能 scripts 下的模块与仓库根的 ae.py
RUNTIME_SCRIPTS = sorted([*SCRIPTS.glob("*.py"), AE])

# argparse 按 CPython 的英文原文报错；这四段匹配规则只允许留在一个文件里
ARGPARSE_PATTERNS = (
    "invalid choice: '",
    "expected one argument",
    "unrecognized arguments:",
    "the following arguments are required:",
)

# 合成 PPTX 在 CASES 里的占位符； setUp 换成临时目录下的真实文件
PPTX = "@pptx@"

# (入口, 场景, 语言, argv, 额外环境, 退出码, 期望 stderr, stdout 须含)
# 语言列说明期望的消息语言：argparse 报错靠写在子命令前的 --locale en 取英文，
# 语言选择本身出错的场景用 AE_LOCALE=en（显式参数优先，报错仍按默认中文说明）。
CASES = [
    ("serve", "missing-value", "zh", ['--port'], {}, 2, 'usage: serve.py [-h] [--locale LANG] [--port PORT] [--claim-port N]\n                [--no-open] [--debug] [--on-handoff 命令]\n                dir\n参数 --port 需要一个值\n', None),
    ("serve", "missing-value", "en", ['--locale', 'en', '--port'], {}, 2, 'usage: serve.py [-h] [--locale LANG] [--port PORT] [--claim-port N]\n                [--no-open] [--debug] [--on-handoff COMMAND]\n                dir\nOption --port needs a value\n', None),
    ("serve", "unrecognized", "zh", ['--nope', 'synthetic-dir'], {}, 2, 'usage: serve.py [-h] [--locale LANG] [--port PORT] [--claim-port N]\n                [--no-open] [--debug] [--on-handoff 命令]\n                dir\n无法识别的参数：--nope\n', None),
    ("serve", "unrecognized", "en", ['--locale', 'en', '--nope', 'synthetic-dir'], {}, 2, 'usage: serve.py [-h] [--locale LANG] [--port PORT] [--claim-port N]\n                [--no-open] [--debug] [--on-handoff COMMAND]\n                dir\nUnrecognized argument: --nope\n', None),
    ("serve", "missing-required", "zh", [], {}, 2, 'usage: serve.py [-h] [--locale LANG] [--port PORT] [--claim-port N]\n                [--no-open] [--debug] [--on-handoff 命令]\n                dir\n缺少必需的参数\n', None),
    ("serve", "missing-required", "en", ['--locale', 'en'], {}, 2, 'usage: serve.py [-h] [--locale LANG] [--port PORT] [--claim-port N]\n                [--no-open] [--debug] [--on-handoff COMMAND]\n                dir\nA required argument is missing\n', None),
    ("serve", "empty-locale", "zh", ['--locale', ''], {}, 2, 'usage: serve.py [-h] [--locale LANG] [--port PORT] [--claim-port N]\n                [--no-open] [--debug] [--on-handoff 命令]\n                dir\n缺少必需的参数\n', None),
    ("serve", "empty-locale", "en", ['--locale', ''], {'AE_LOCALE': 'en'}, 2, 'usage: serve.py [-h] [--locale LANG] [--port PORT] [--claim-port N]\n                [--no-open] [--debug] [--on-handoff COMMAND]\n                dir\nA required argument is missing\n', None),
    ("serve", "missing-value", "zh", ['status', '--locale'], {}, 2, 'usage: serve.py status [-h] [--locale LANG] dir\n参数 --locale 需要一个值\n', None),
    ("serve", "missing-value", "en", ['--locale', 'en', 'status', '--locale'], {}, 2, 'usage: serve.py status [-h] [--locale LANG] dir\nOption --locale needs a value\n', None),
    ("serve", "unrecognized", "zh", ['status', '--nope', 'd'], {}, 2, 'usage: serve.py status [-h] [--locale LANG] dir\n无法识别的参数：--nope\n', None),
    ("serve", "unrecognized", "en", ['--locale', 'en', 'status', '--nope', 'd'], {}, 2, 'usage: serve.py status [-h] [--locale LANG] dir\nUnrecognized argument: --nope\n', None),
    ("serve", "missing-required", "zh", ['status'], {}, 2, 'usage: serve.py status [-h] [--locale LANG] dir\n缺少必需的参数\n', None),
    ("serve", "missing-required", "en", ['--locale', 'en', 'status'], {}, 2, 'usage: serve.py status [-h] [--locale LANG] dir\nA required argument is missing\n', None),
    ("serve", "missing-value", "zh", ['list', '--under'], {}, 2, 'usage: serve.py list [-h] [--locale LANG] [--under 目录] [--json]\n参数 --under 需要一个值\n', None),
    ("serve", "missing-value", "en", ['--locale', 'en', 'list', '--under'], {}, 2, 'usage: serve.py list [-h] [--locale LANG] [--under DIR] [--json]\nOption --under needs a value\n', None),
    ("serve", "unrecognized", "zh", ['list', '--nope'], {}, 2, 'usage: serve.py list [-h] [--locale LANG] [--under 目录] [--json]\n无法识别的参数：--nope\n', None),
    ("serve", "unrecognized", "en", ['--locale', 'en', 'list', '--nope'], {}, 2, 'usage: serve.py list [-h] [--locale LANG] [--under DIR] [--json]\nUnrecognized argument: --nope\n', None),
    ("serve", "empty-locale", "zh", ['list', '--locale', ''], {}, 0, '', None),
    ("serve", "empty-locale", "en", ['list', '--locale', ''], {'AE_LOCALE': 'en'}, 0, '', None),
    ("serve", "invalid-locale", "zh", ['list', '--locale', 'fr'], {}, 2, '不支持的语言：fr（可选：auto、zh-CN 或 en）\n', None),
    ("serve", "invalid-locale", "en", ['list', '--locale', 'fr'], {'AE_LOCALE': 'en'}, 2, '不支持的语言：fr（可选：auto、zh-CN 或 en）\n', None),
    ("serve", "after-dashes", "zh", ['list', '--', '--locale', 'en'], {}, 2, 'usage: serve.py list [-h] [--locale LANG] [--under 目录] [--json]\n无法识别的参数：-- --locale en\n', None),
    ("serve", "after-dashes", "en", ['list', '--', '--locale', 'en'], {'AE_LOCALE': 'en'}, 2, 'usage: serve.py list [-h] [--locale LANG] [--under DIR] [--json]\nUnrecognized argument: -- --locale en\n', None),
    ("serve", "missing-value", "zh", ['forget', '--locale'], {}, 2, 'usage: serve.py forget [-h] [--locale LANG] key\n参数 --locale 需要一个值\n', None),
    ("serve", "missing-value", "en", ['--locale', 'en', 'forget', '--locale'], {}, 2, 'usage: serve.py forget [-h] [--locale LANG] key\nOption --locale needs a value\n', None),
    ("serve", "unrecognized", "zh", ['forget', '--nope', 'k'], {}, 2, 'usage: serve.py forget [-h] [--locale LANG] key\n无法识别的参数：--nope\n', None),
    ("serve", "unrecognized", "en", ['--locale', 'en', 'forget', '--nope', 'k'], {}, 2, 'usage: serve.py forget [-h] [--locale LANG] key\nUnrecognized argument: --nope\n', None),
    ("serve", "missing-required", "zh", ['forget'], {}, 2, 'usage: serve.py forget [-h] [--locale LANG] key\n缺少必需的参数\n', None),
    ("serve", "missing-required", "en", ['--locale', 'en', 'forget'], {}, 2, 'usage: serve.py forget [-h] [--locale LANG] key\nA required argument is missing\n', None),
    ("update", "missing-value", "zh", ['--locale'], {}, 2, 'usage: update.py [-h] [--locale LANG] MODE\n参数 --locale 需要一个值\n', None),
    ("update", "missing-value", "en", ['--locale', 'en', '--locale'], {}, 2, 'usage: update.py [-h] [--locale LANG] MODE\nOption --locale needs a value\n', None),
    ("update", "unrecognized", "zh", ['now', '--nope'], {}, 2, 'usage: update.py [-h] [--locale LANG] MODE\n无法识别的参数：--nope\n', None),
    ("update", "unrecognized", "en", ['--locale', 'en', 'now', '--nope'], {}, 2, 'usage: update.py [-h] [--locale LANG] MODE\nUnrecognized argument: --nope\n', None),
    ("update", "missing-required", "zh", [], {}, 2, 'usage: update.py [-h] [--locale LANG] MODE\n缺少更新模式（可选：auto、check、now）\n', None),
    ("update", "missing-required", "en", ['--locale', 'en'], {}, 2, 'usage: update.py [-h] [--locale LANG] MODE\nMissing update mode (choose auto, check, or now)\n', None),
    ("update", "invalid-choice", "zh", ['bogus'], {}, 2, 'usage: update.py [-h] [--locale LANG] MODE\n未知的更新模式：bogus（可选：auto、check、now）\n', None),
    ("update", "invalid-choice", "en", ['--locale', 'en', 'bogus'], {}, 2, 'usage: update.py [-h] [--locale LANG] MODE\nUnknown update mode: bogus (choose auto, check, or now)\n', None),
    ("update", "empty-locale", "zh", ['--locale', '', 'now'], {}, 1, '缺少有效的安装记录或来源仓库，无法检查更新；请从来源仓库重新安装。\n', None),
    ("update", "empty-locale", "en", ['--locale', '', 'now'], {'AE_LOCALE': 'en'}, 1, 'The install record or source repository is unavailable; reinstall from the source repository to check for updates.\n', None),
    ("update", "invalid-locale", "zh", ['--locale', 'fr', 'now'], {}, 2, '不支持的语言：fr（可选：auto、zh-CN 或 en）\n', None),
    ("update", "invalid-locale", "en", ['--locale', 'fr', 'now'], {'AE_LOCALE': 'en'}, 2, '不支持的语言：fr（可选：auto、zh-CN 或 en）\n', None),
    ("update", "after-dashes", "zh", ['--', '--locale', 'en'], {}, 2, 'usage: update.py [-h] [--locale LANG] MODE\n未知的更新模式：--locale（可选：auto、check、now）\n', None),
    ("update", "after-dashes", "en", ['--', '--locale', 'en'], {'AE_LOCALE': 'en'}, 2, 'usage: update.py [-h] [--locale LANG] MODE\nUnknown update mode: --locale (choose auto, check, or now)\n', None),
    ("fonts", "missing-value", "zh", ['--locale'], {}, 2, 'usage: check_pptx_fonts.py [-h] [--locale LANG] [--pdf PDF] pptx\n参数 --locale 需要一个值\n', None),
    ("fonts", "missing-value", "en", ['--locale', 'en', '--locale'], {}, 2, 'usage: check_pptx_fonts.py [-h] [--locale LANG] [--pdf PDF] pptx\nOption --locale needs a value\n', None),
    ("fonts", "unrecognized", "zh", [PPTX, '--nope'], {}, 2, 'usage: check_pptx_fonts.py [-h] [--locale LANG] [--pdf PDF] pptx\n无法识别的参数：--nope\n', None),
    ("fonts", "unrecognized", "en", ['--locale', 'en', PPTX, '--nope'], {}, 2, 'usage: check_pptx_fonts.py [-h] [--locale LANG] [--pdf PDF] pptx\nUnrecognized argument: --nope\n', None),
    ("fonts", "missing-required", "zh", [], {}, 2, 'usage: check_pptx_fonts.py [-h] [--locale LANG] [--pdf PDF] pptx\n缺少必需的参数\n', None),
    ("fonts", "missing-required", "en", ['--locale', 'en'], {}, 2, 'usage: check_pptx_fonts.py [-h] [--locale LANG] [--pdf PDF] pptx\nA required argument is missing\n', None),
    ("fonts", "empty-locale", "zh", ['--locale', '', PPTX], {}, 2, '错误：找不到 fc-list，无法枚举字体；请安装 fontconfig 后重试\n', None),
    ("fonts", "empty-locale", "en", ['--locale', '', PPTX], {'AE_LOCALE': 'en'}, 2, 'Error: fc-list not found, cannot list fonts; install fontconfig and retry\n', None),
    ("fonts", "invalid-locale", "zh", ['--locale', 'fr', PPTX], {}, 2, '不支持的语言：fr（可选：auto、zh-CN 或 en）\n', None),
    ("fonts", "invalid-locale", "en", ['--locale', 'fr', PPTX], {'AE_LOCALE': 'en'}, 2, '不支持的语言：fr（可选：auto、zh-CN 或 en）\n', None),
    ("fonts", "after-dashes", "zh", ['--', '--locale', 'en'], {}, 2, 'usage: check_pptx_fonts.py [-h] [--locale LANG] [--pdf PDF] pptx\n无法识别的参数：en\n', None),
    ("fonts", "after-dashes", "en", ['--', '--locale', 'en'], {'AE_LOCALE': 'en'}, 2, 'usage: check_pptx_fonts.py [-h] [--locale LANG] [--pdf PDF] pptx\nUnrecognized argument: en\n', None),
    ("ae", "missing-value", "zh", ['install', '--link'], {}, 2, 'usage: ae.py install [-h] [--locale LANG] [--target DIR] [--link AGENTS]\n参数 --link 需要一个值\n', None),
    ("ae", "missing-value", "en", ['--locale', 'en', 'install', '--link'], {}, 2, 'usage: ae.py install [-h] [--locale LANG] [--target DIR] [--link AGENTS]\nOption --link needs a value\n', None),
    ("ae", "unrecognized", "zh", ['doctor', '--nope'], {}, 2, 'usage: ae.py [-h] [--locale LANG] 子命令 ...\n无法识别的参数：--nope\n', None),
    ("ae", "unrecognized", "en", ['--locale', 'en', 'doctor', '--nope'], {}, 2, 'usage: ae.py [-h] [--locale LANG] COMMAND ...\nUnrecognized argument: --nope\n', None),
    ("ae", "missing-required", "zh", [], {}, 2, 'usage: ae.py [-h] [--locale LANG] 子命令 ...\n缺少子命令（可选：install、doctor、repair、uninstall）\n', None),
    ("ae", "missing-required", "en", ['--locale', 'en'], {}, 2, 'usage: ae.py [-h] [--locale LANG] COMMAND ...\nMissing subcommand (choose from: install, doctor, repair, uninstall)\n', None),
    ("ae", "invalid-choice", "zh", ['no-such-subcommand'], {}, 2, 'usage: ae.py [-h] [--locale LANG] 子命令 ...\n未知的子命令：no-such-subcommand（可选：install、doctor、repair、uninstall）\n', None),
    ("ae", "invalid-choice", "en", ['--locale', 'en', 'no-such-subcommand'], {}, 2, 'usage: ae.py [-h] [--locale LANG] COMMAND ...\nUnknown subcommand: no-such-subcommand (choose from: install, doctor, repair, uninstall)\n', None),
    ("ae", "empty-locale", "zh", ['--locale', '', 'doctor'], {}, 1, '', None),
    ("ae", "empty-locale", "en", ['--locale', '', 'doctor'], {'AE_LOCALE': 'en'}, 1, '', None),
    ("ae", "invalid-locale", "zh", ['--locale', 'fr', 'doctor'], {}, 2, '不支持的语言：fr（可选：auto、zh-CN 或 en）\n', None),
    ("ae", "invalid-locale", "en", ['--locale', 'fr', 'doctor'], {'AE_LOCALE': 'en'}, 2, '不支持的语言：fr（可选：auto、zh-CN 或 en）\n', None),
    ("ae", "after-dashes", "zh", ['doctor', '--', '--locale', 'en'], {}, 2, 'usage: ae.py [-h] [--locale LANG] 子命令 ...\n无法识别的参数：-- --locale en\n', None),
    ("ae", "after-dashes", "en", ['doctor', '--', '--locale', 'en'], {'AE_LOCALE': 'en'}, 2, 'usage: ae.py [-h] [--locale LANG] COMMAND ...\nUnrecognized argument: -- --locale en\n', None),
    ("serve", "invalid-locale-padded", "zh", ['list', '--locale', ' fr '], {}, 2, '不支持的语言：fr（可选：auto、zh-CN 或 en）\n', None),
    ("serve", "invalid-locale-padded", "en", ['list', '--locale', ' fr '], {'AE_LOCALE': 'en'}, 2, '不支持的语言：fr（可选：auto、zh-CN 或 en）\n', None),
    ("update", "invalid-locale-padded", "zh", ['--locale', ' fr ', 'now'], {}, 2, '不支持的语言： fr （可选：auto、zh-CN 或 en）\n', None),
    ("update", "invalid-locale-padded", "en", ['--locale', ' fr ', 'now'], {'AE_LOCALE': 'en'}, 2, '不支持的语言： fr （可选：auto、zh-CN 或 en）\n', None),
    ("fonts", "invalid-locale-padded", "zh", ['--locale', ' fr ', '@pptx@'], {}, 2, '不支持的语言：fr（可选：auto、zh-CN 或 en）\n', None),
    ("fonts", "invalid-locale-padded", "en", ['--locale', ' fr ', '@pptx@'], {'AE_LOCALE': 'en'}, 2, '不支持的语言：fr（可选：auto、zh-CN 或 en）\n', None),
    ("ae", "invalid-locale-padded", "zh", ['--locale', ' fr ', 'doctor'], {}, 2, '不支持的语言：fr（可选：auto、zh-CN 或 en）\n', None),
    ("ae", "invalid-locale-padded", "en", ['--locale', ' fr ', 'doctor'], {'AE_LOCALE': 'en'}, 2, '不支持的语言：fr（可选：auto、zh-CN 或 en）\n', None),
    ("serve", "locale-position", "argv", ['list'], {}, 0, '', '审阅台账里没有审阅'),
    ("serve", "locale-position", "argv", ['--locale', 'en', 'list'], {}, 0, '', 'No reviews in the review ledger'),
    ("serve", "locale-position", "argv", ['list', '--locale', 'en'], {}, 0, '', 'No reviews in the review ledger'),
    ("serve", "locale-position", "argv", ['--locale=en', 'list'], {}, 0, '', 'No reviews in the review ledger'),
    ("update", "locale-position", "argv", ['--help'], {}, 0, '', '检查并更新'),
    ("update", "locale-position", "argv", ['--locale', 'en', '--help'], {}, 0, '', 'Check and update'),
    ("update", "locale-position", "argv", ['--help', '--locale', 'en'], {}, 0, '', 'Check and update'),
    ("update", "locale-position", "argv", ['--locale=en', '--help'], {}, 0, '', 'Check and update'),
    ("fonts", "locale-position", "argv", ['--help'], {}, 0, '', '检查 PPTX 声明字体'),
    ("fonts", "locale-position", "argv", ['--locale', 'en', '--help'], {}, 0, '', 'Check the fonts declared in a PPTX'),
    ("fonts", "locale-position", "argv", ['--help', '--locale', 'en'], {}, 0, '', 'Check the fonts declared in a PPTX'),
    ("fonts", "locale-position", "argv", ['--locale=en', '--help'], {}, 0, '', 'Check the fonts declared in a PPTX'),
    ("ae", "locale-position", "argv", ['doctor'], {}, 1, '', '安装状态'),
    ("ae", "locale-position", "argv", ['--locale', 'en', 'doctor'], {}, 1, '', 'Installation'),
    ("ae", "locale-position", "argv", ['doctor', '--locale', 'en'], {}, 1, '', 'Installation'),
    ("ae", "locale-position", "argv", ['--locale=en', 'doctor'], {}, 1, '', 'Installation'),
]


class CliErrorMatrixTests(unittest.TestCase):
    """四个入口 × 六种情况 × 中英两种语言：stderr 与退出码逐字不变。"""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="cli-i18n-test-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.home = self.root / "home"
        self.home.mkdir()
        self.ledger = self.root / "ledger"
        self.ledger.mkdir()
        # PATH 只留一个空目录：fc-list 一定找不到，字体检查的输出不随机器变
        self.bin = self.root / "bin"
        self.bin.mkdir()
        self.pptx = self.root / "synthetic.pptx"
        with zipfile.ZipFile(self.pptx, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("ppt/slides/slide1.xml", '''<p:sld xmlns:p="urn:p" xmlns:a="urn:a">
              <a:r><a:rPr><a:latin typeface="Synthetic Sans"/></a:rPr></a:r>
            </p:sld>''')

    def env(self, **extra) -> dict:
        base = {k: v for k, v in os.environ.items()
                if k not in ("AE_LOCALE", "LC_ALL", "LC_MESSAGES", "LANG")}
        base.update({"HOME": str(self.home), "AE_HOME": str(self.ledger),
                     "PATH": str(self.bin), "PYTHONDONTWRITEBYTECODE": "1"})
        base.update(extra)
        return base

    def run_entry(self, entry: str, argv, **extra):
        return subprocess.run([sys.executable, str(SCRIPTS_BY_ENTRY[entry]), *argv],
                              env=self.env(**extra), capture_output=True, text=True,
                              timeout=120, cwd=str(self.root))

    def test_matrix(self):
        for entry, case, lang, argv, extra, code, stderr, marker in CASES:
            resolved = [str(self.pptx) if token == PPTX else token for token in argv]
            with self.subTest(entry=entry, case=case, lang=lang, argv=resolved):
                p = self.run_entry(entry, resolved, **extra)
                self.assertNotIn("Traceback", p.stderr)
                self.assertEqual(p.returncode, code, p.stdout + p.stderr)
                self.assertEqual(p.stderr, stderr)
                if marker:
                    self.assertIn(marker, p.stdout, p.stdout)


class SingleImplementationTests(unittest.TestCase):
    """argparse 报错的匹配规则只保留一份，ae.py 不再保留自己的副本。"""

    def test_patterns_live_only_in_the_adapter(self):
        for pattern in ARGPARSE_PATTERNS:
            with self.subTest(pattern=pattern):
                holders = [path.name for path in RUNTIME_SCRIPTS
                           if pattern in path.read_text(encoding="utf-8")]
                self.assertEqual(holders, ["i18n.py"],
                                 "匹配 argparse 英文报错的正则只能在 i18n.py 里出现一次")

    def test_locale_extraction_lives_only_in_the_adapter(self):
        """取 --locale 的逻辑也只在 i18n.py 里；ae.py 不再有 explicit_locale_from_argv。"""
        holders = [path.name for path in RUNTIME_SCRIPTS
                   if "def explicit_locale(" in path.read_text(encoding="utf-8")]
        self.assertEqual(holders, ["i18n.py"])
        self.assertNotIn("explicit_locale_from_argv", AE.read_text(encoding="utf-8"))


class OldSkillCopyFallbackTests(unittest.TestCase):
    """技能副本是还没有公共实现的旧版本时，ae.py 退回内置最小实现照常工作。"""

    # #94 才加进 i18n.py 的顶层定义；合成的旧版副本把它们全部去掉
    NEW_IN_I18N = (
        "classify_cli_error", "translate_cli_error", "LocalizedParser", "locale_error_message",
        "CLI_ERROR_GENERIC", "CLI_ERROR_MISSING_VALUE", "CLI_ERROR_UNKNOWN_ARGUMENT",
        "CLI_ERROR_MISSING_ARGUMENT", "CLI_ERROR_INVALID_CHOICE",
        "CLI_ERROR_EMPTY_LOCALE", "CLI_ERROR_INVALID_LOCALE",
        "_INVALID_CHOICE", "_MISSING_VALUE", "_UNKNOWN_ARGUMENT",
    )

    @staticmethod
    def _top_level_names(node) -> set:
        if isinstance(node, (ast.FunctionDef, ast.ClassDef)):
            return {node.name}
        if isinstance(node, ast.Assign):
            return {t.id for t in node.targets if isinstance(t, ast.Name)}
        return set()

    def synthesize_old_i18n(self) -> str:
        """把当前的 i18n.py 退成旧版：删掉 #94 新增的顶层定义，其余原样保留。"""
        tree = ast.parse((SCRIPTS / "i18n.py").read_text(encoding="utf-8"))
        kept = [node for node in tree.body
                if not self._top_level_names(node) & set(self.NEW_IN_I18N)]
        return ast.unparse(ast.Module(body=kept, type_ignores=[]))

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="ae-old-copy-test-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.home = self.root / "home"
        self.home.mkdir()
        self.ledger = self.root / "ledger"
        self.ledger.mkdir()
        self.skills = self.root / "skills"
        # 从仓库外运行：ae.py 单独放在别处，skill_module 只能退回技能副本
        self.outside = self.root / "outside"
        self.outside.mkdir()
        shutil.copy2(AE, self.outside / "ae.py")
        # 合成的旧版技能副本：内容与当前一致，只有 i18n.py 退回没有公共实现的版本
        copy = self.skills / "annotation-everything"
        shutil.copytree(SKILL_DIR, copy,
                        ignore=shutil.ignore_patterns("__pycache__", "*.pyc", ".DS_Store"))
        old_i18n = self.synthesize_old_i18n()
        (copy / "scripts" / "i18n.py").write_text(old_i18n, encoding="utf-8")
        self.assertNotIn("LocalizedParser", old_i18n, "合成的旧版副本不该有公共实现")

    def run_ae(self, *args: str):
        env = {k: v for k, v in os.environ.items()
               if k not in ("AE_LOCALE", "LC_ALL", "LC_MESSAGES", "LANG")}
        env.update({"HOME": str(self.home), "AE_HOME": str(self.ledger),
                    "CLAUDE_SKILLS_DIR": str(self.skills),
                    "PYTHONDONTWRITEBYTECODE": "1"})
        return subprocess.run([sys.executable, str(self.outside / "ae.py"), *args],
                              env=env, capture_output=True, text=True, timeout=120)

    def test_doctor_works_with_an_old_skill_copy(self):
        p = self.run_ae("doctor")
        self.assertIn(p.returncode, (0, 1), p.stdout + p.stderr)
        self.assertNotIn("Traceback", p.stderr)
        self.assertTrue(p.stdout.strip(), "体检要有输出")
        self.assertIn("安装状态", p.stdout)
        self.assertNotIn("Installation", p.stdout)

    def test_explicit_locale_is_still_parsed_with_an_old_skill_copy(self):
        p = self.run_ae("--locale", "en", "doctor")
        self.assertIn(p.returncode, (0, 1), p.stdout + p.stderr)
        self.assertNotIn("Traceback", p.stderr)
        self.assertIn("Installation", p.stdout)
        self.assertNotIn("安装状态", p.stdout)

    def test_invalid_locale_is_still_reported_with_an_old_skill_copy(self):
        p = self.run_ae("--locale", "fr", "doctor")
        self.assertEqual(p.returncode, 2, p.stdout + p.stderr)
        self.assertNotIn("Traceback", p.stderr)
        self.assertEqual(p.stdout, "", "拒绝信息只走 stderr，不污染 stdout")
        self.assertIn("不支持的语言：fr", p.stderr)

    def test_argparse_errors_stay_localized_with_an_old_skill_copy(self):
        """兜底实现不区分类别，但仍要本地化、退出码仍要是 2。"""
        p = self.run_ae("no-such-subcommand")
        self.assertEqual(p.returncode, 2, p.stdout + p.stderr)
        self.assertNotIn("Traceback", p.stderr)
        self.assertIn("命令行用法不对", p.stderr)
        self.assertIn("invalid choice", p.stderr, "兜底消息带上原始报错补上下文")


if __name__ == "__main__":
    unittest.main()
