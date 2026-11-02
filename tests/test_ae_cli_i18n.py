"""ae.py 命令行的语言选择、本地化消息与稳定的机器判定字段。

技能副本、台账与技能目录全是合成环境；只用标准库。中英文两轮跑同一批断言，
保证「人读文案按语言变、机器判定字段不变」。
"""
from __future__ import annotations

import contextlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
import unittest.mock
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
AE = ROOT / "ae.py"
SERVE_PY = ROOT / "skill" / "annotation-everything" / "scripts" / "serve.py"
SCRIPTS = ROOT / "skill" / "annotation-everything" / "scripts"
sys.path.insert(0, str(ROOT))
import ae  # noqa: E402
sys.path.insert(0, str(SCRIPTS))

import i18n  # noqa: E402

# 既有中文判定字段：旧机器消费者照旧读取，任何语言下都不得变值
LEGACY_LEVELS = ("通过", "警告", "失败")
LEGACY_CATEGORIES = ("安装状态", "可选工具", "开发环境", "运行状态")
# 新增的语言无关判定字段
STABLE_LEVELS = ("pass", "warn", "fail")
STABLE_CATEGORIES = ("install", "tools", "dev", "runtime")


class AeLocaleTestCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="ae-i18n-test-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.home = self.root / "home"
        self.home.mkdir()
        self.skills = self.home / ".claude" / "skills"
        self.ae_script = AE
        # 本模块验证语言与机器字段；使用确定的合成空进程表，避免遍历宿主进程。
        # 真实盘点与清理边界仍由 test_ae_checks_runtime / test_host_interference 覆盖。
        self.host_bin = self.root / "host-bin"
        self.host_bin.mkdir()
        ps = self.host_bin / "ps"
        ps.write_text('#!/bin/sh\nif [ "$1 $2" = "-ww -eo" ]; then\n  exit 0\n'
                      'else\n  exec /bin/ps "$@"\nfi\n', encoding="utf-8")
        ps.chmod(0o755)

    def env(self, **extra) -> dict:
        base = {k: v for k, v in os.environ.items()
                if k not in ("AE_LOCALE", "LC_ALL", "LC_MESSAGES", "LANG")}
        base.update({"HOME": str(self.home), "CLAUDE_SKILLS_DIR": str(self.skills),
                     "AE_HOME": str(self.root / "ledger"), "PYTHONDONTWRITEBYTECODE": "1",
                     "PATH": str(self.host_bin) + os.pathsep + os.environ.get("PATH", os.defpath)})
        base.update(extra)
        return base

    def run_ae(self, *args: str, **extra):
        return subprocess.run([sys.executable, str(self.ae_script), *args], env=self.env(**extra),
                              capture_output=True, text=True, timeout=120)

    def install(self, *args: str, **extra):
        p = self.run_ae("install", *args, **extra)
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        return p


class LocaleSelectionTests(AeLocaleTestCase):
    def test_locale_flag_before_and_after_subcommand(self):
        for argv in (["--locale", "en", "doctor"], ["doctor", "--locale", "en"]):
            p = self.run_ae(*argv)
            self.assertIn(p.returncode, (0, 1), p.stdout + p.stderr)
            self.assertIn("Installation", p.stdout, argv)
            self.assertNotIn("安装状态", p.stdout, argv)

    def test_env_locale_used_without_flag(self):
        p = self.run_ae("doctor", AE_LOCALE="en")
        self.assertIn("Summary:", p.stdout)
        self.assertNotIn("汇总", p.stdout)

    def test_flag_beats_env(self):
        p = self.run_ae("doctor", "--locale", "zh-CN", AE_LOCALE="en")
        self.assertIn("汇总", p.stdout)
        self.assertNotIn("Summary:", p.stdout)

    def test_env_beats_default_but_not_flag(self):
        p = self.run_ae("doctor", AE_LOCALE="zh-CN")
        self.assertIn("汇总", p.stdout)

    def test_auto_reads_system_languages_only_when_asked(self):
        p = self.run_ae("doctor", "--locale", "auto", LANG="zh_CN.UTF-8")
        self.assertIn("汇总", p.stdout)
        p = self.run_ae("doctor", "--locale", "auto", LANG="en_US.UTF-8")
        self.assertIn("Summary:", p.stdout)

    def test_no_choice_keeps_chinese_even_on_english_terminal(self):
        p = self.run_ae("doctor", LANG="en_US.UTF-8", LC_ALL="en_US.UTF-8")
        self.assertIn("汇总", p.stdout, "没有显式选择时不跟随终端语言")

    def test_locale_flag_accepts_alias_and_env_alias(self):
        for value in ("en", "en-US", "en_GB"):
            p = self.run_ae("doctor", "--locale", value)
            self.assertIn("Summary:", p.stdout, value)
        for value in ("zh", "zh-Hans", "zh_CN.UTF-8"):
            p = self.run_ae("doctor", "--locale", value)
            self.assertIn("汇总", p.stdout, value)

    def test_unsupported_locale_is_a_command_error(self):
        for argv in (["doctor", "--locale", "fr"], ["doctor", "--locale", "zh-TW"]):
            p = self.run_ae(*argv)
            self.assertEqual(p.returncode, 2, p.stdout + p.stderr)
            self.assertTrue(p.stderr.strip(), argv)
            self.assertEqual(p.stdout, "", "拒绝信息只走 stderr，不污染 stdout")

    def test_unsupported_env_locale_is_a_command_error(self):
        p = self.run_ae("doctor", AE_LOCALE="ja")
        self.assertEqual(p.returncode, 2)
        self.assertNotIn("Traceback", p.stderr)

    def test_locale_override_reaches_skill_copy_scripts(self):
        """从技能副本运行 serve.py 时，--locale 与 AE_LOCALE 同样生效。"""
        self.install()
        copy = self.skills / "annotation-everything"
        copied = copy / "scripts" / "serve.py"
        self.assertTrue(copied.is_file())
        base = self.env()
        for argv, want in ((["list", "--locale", "en"], "No reviews in the review ledger"),
                           (["list"], "审阅台账里没有审阅")):
            p = subprocess.run([sys.executable, str(copied), *argv], env=base,
                               capture_output=True, text=True, timeout=60)
            self.assertIn(p.returncode, (0, 2), p.stdout + p.stderr)
            self.assertIn(want, p.stdout, argv)
        p = subprocess.run([sys.executable, str(copied), "list"], env=self.env(AE_LOCALE="en"),
                           capture_output=True, text=True, timeout=60)
        self.assertIn("No reviews in the review ledger", p.stdout)


class HelpAndErrorLanguageTests(AeLocaleTestCase):
    def test_help_is_localized(self):
        p = self.run_ae("--help", "--locale", "en")
        self.assertEqual(p.returncode, 0)
        self.assertIn("management command", p.stdout)
        self.assertIn("Copy the skill bundle into a skill copy", p.stdout)
        p = self.run_ae("--help")
        self.assertEqual(p.returncode, 0)
        self.assertIn("统一管理命令", p.stdout)
        self.assertIn("把技能本体复制为技能副本", p.stdout)

    def test_subcommand_help_is_localized(self):
        p = self.run_ae("repair", "--help", "--locale", "en")
        self.assertEqual(p.returncode, 0)
        self.assertIn("destructive", p.stdout)
        self.assertIn("--forget-stale", p.stdout, "参数名不翻译")
        p = self.run_ae("uninstall", "--help", "--locale", "en")
        self.assertIn("--stop-servers", p.stdout)
        self.assertIn("never\n                  touched", p.stdout)

    def test_missing_subcommand_is_localized(self):
        p = self.run_ae("--locale", "en")
        self.assertEqual(p.returncode, 2)
        self.assertIn("Missing subcommand", p.stderr)
        self.assertNotIn("Traceback", p.stderr)
        p = self.run_ae()
        self.assertEqual(p.returncode, 2)
        self.assertIn("缺少子命令", p.stderr)

    def test_unknown_subcommand_is_localized(self):
        p = self.run_ae("--locale", "en", "no-such-subcommand")
        self.assertEqual(p.returncode, 2)
        self.assertIn("Unknown subcommand: no-such-subcommand", p.stderr)
        p = self.run_ae("no-such-subcommand")
        self.assertEqual(p.returncode, 2)
        self.assertIn("未知的子命令：no-such-subcommand", p.stderr)

    def test_missing_option_value_is_localized(self):
        p = self.run_ae("--locale", "en", "install", "--link")
        self.assertEqual(p.returncode, 2)
        self.assertIn("needs a value", p.stderr)
        p = self.run_ae("install", "--link")
        self.assertEqual(p.returncode, 2)
        self.assertIn("需要一个值", p.stderr)

    def test_unrecognized_argument_is_localized(self):
        p = self.run_ae("--locale", "en", "doctor", "--nope")
        self.assertEqual(p.returncode, 2)
        self.assertIn("Unrecognized argument", p.stderr)

    def test_help_option_is_localized(self):
        p = self.run_ae("--locale", "en", "doctor", "--help")
        self.assertEqual(p.returncode, 0)
        self.assertIn("Show this help message and exit", p.stdout)


class DoctorMachineOutputTests(AeLocaleTestCase):
    def doctor_json(self, *args: str, **extra):
        p = self.run_ae("doctor", "--json", *args, **extra)
        try:
            return p, json.loads(p.stdout)
        except ValueError:
            self.fail(f"stdout 不是合法 JSON：{p.stdout!r}\nstderr={p.stderr!r}")

    def test_json_stdout_is_pure_json(self):
        self.install()
        for locale in (None, "en"):
            p, doc = self.doctor_json(*(["--locale", locale] if locale else []))
            self.assertIn(p.returncode, (0, 1))
            self.assertIsInstance(doc["results"], list)
            self.assertTrue(doc["results"])
            self.assertEqual(doc["summary"]["pass"] + doc["summary"]["warn"] + doc["summary"]["fail"],
                             len(doc["results"]))

    def test_legacy_fields_stay_chinese_and_identical_across_locales(self):
        self.install()
        _, zh = self.doctor_json()
        _, en = self.doctor_json("--locale", "en")
        self.assertEqual([r["code"] for r in zh["results"]], [r["code"] for r in en["results"]])
        for a, b in zip(zh["results"], en["results"]):
            self.assertEqual(a["category"], b["category"], a["code"])
            self.assertEqual(a["level"], b["level"], a["code"])
            self.assertEqual(a["fix_command"], b["fix_command"], a["code"])
            self.assertIn(a["category"], LEGACY_CATEGORIES, a["code"])
            self.assertIn(a["level"], LEGACY_LEVELS, a["code"])

    def test_new_fields_are_stable_and_language_independent(self):
        self.install()
        _, zh = self.doctor_json()
        _, en = self.doctor_json("--locale", "en")
        for a, b in zip(zh["results"], en["results"]):
            self.assertEqual(a["status"], b["status"], a["code"])
            self.assertEqual(a["category_id"], b["category_id"], a["code"])
            self.assertIn(a["status"], STABLE_LEVELS, a["code"])
            self.assertIn(a["category_id"], STABLE_CATEGORIES, a["code"])
        self.assertEqual(set(zh["summary"]), {"pass", "warn", "fail"}, "汇总键名本身与语言无关")
        self.assertTrue({r["level"] for r in en["results"]} <= set(LEGACY_LEVELS))

    def test_human_message_follows_language_but_judgement_does_not(self):
        self.install()
        _, zh = self.doctor_json()
        _, en = self.doctor_json("--locale", "en")
        by_code_zh = {r["code"]: r for r in zh["results"]}
        by_code_en = {r["code"]: r for r in en["results"]}
        self.assertEqual(by_code_zh["fc-list"]["level"], by_code_en["fc-list"]["level"])
        self.assertEqual(by_code_zh["fc-list"]["status"], by_code_en["fc-list"]["status"])
        self.assertNotEqual(by_code_zh["fc-list"]["message"], by_code_en["fc-list"]["message"])
        self.assertTrue(any(ord(ch) > 127 for ch in by_code_zh["fc-list"]["message"]))
        self.assertFalse(any(ord(ch) > 127 for ch in by_code_en["fc-list"]["message"]))

    def test_json_declares_which_language_the_messages_are_in(self):
        self.install()
        _, zh = self.doctor_json()
        _, en = self.doctor_json("--locale", "en")
        self.assertEqual(zh["locale"], "zh-CN")
        self.assertEqual(en["locale"], "en")

    def test_schema_version_is_stable_across_locales(self):
        """schema 让消费者认出拿到的是哪一版结构；它本身与语言无关。"""
        self.install()
        _, zh = self.doctor_json()
        _, en = self.doctor_json("--locale", "en")
        self.assertEqual(zh["schema"], en["schema"])
        self.assertEqual(zh["schema"], ae.DOCTOR_SCHEMA)

    def test_failure_machine_fields_and_exit_code_survive_localization(self):
        """副本被改坏时中英文的判定、失败项与退出码完全一致。"""
        self.install()
        (self.skills / "annotation-everything" / "手工改动.txt").write_text("x", encoding="utf-8")
        p_zh, zh = self.doctor_json()
        p_en, en = self.doctor_json("--locale", "en")
        self.assertEqual(p_zh.returncode, 1)
        self.assertEqual(p_en.returncode, 1)
        self.assertEqual([r["code"] for r in zh["results"] if r["status"] == "fail"],
                         [r["code"] for r in en["results"] if r["status"] == "fail"])
        self.assertIn("skill-copy-sync", {r["code"] for r in en["results"] if r["status"] == "fail"})

    def test_json_error_path_is_valid_json(self):
        self.install()
        record = self.skills / "annotation-everything" / ".install-record.json"
        record.write_text("{坏的", encoding="utf-8")
        p, doc = self.doctor_json()
        self.assertEqual(p.returncode, 2)
        self.assertIn("error", doc)
        self.assertTrue(doc["error"])
        p, doc = self.doctor_json("--locale", "en")
        self.assertEqual(p.returncode, 2)
        self.assertIn("install record", doc["error"])

    def test_text_output_keeps_grouping_and_localizes_labels(self):
        self.install()
        p = self.run_ae("doctor", "--locale", "en")
        self.assertIn("Installation", p.stdout)
        self.assertIn("Runtime", p.stdout)
        self.assertIn("[PASS] python-version", p.stdout)
        self.assertNotIn("安装状态", p.stdout)
        p = self.run_ae("doctor")
        self.assertIn("安装状态", p.stdout)
        self.assertIn("[通过] python-version", p.stdout)

    def test_exit_codes_are_unchanged(self):
        # 独立来源路径避免把并行 e2e 的同仓库服务识别为本测试空台账的残留服务。
        repo = self.root / "repo"
        repo.mkdir()
        self.ae_script = repo / "ae.py"
        shutil.copyfile(AE, self.ae_script)
        shutil.copytree(ROOT / "skill", repo / "skill",
                        ignore=shutil.ignore_patterns("__pycache__", ".DS_Store"))
        self.assertEqual(self.run_ae("doctor", "--json").returncode, 1, "没安装时安装状态失败")
        self.install()
        p = self.run_ae("doctor")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        p = self.run_ae("repair", "--dry-run")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)


class CommandMessageTests(AeLocaleTestCase):
    def test_install_and_uninstall_messages_follow_language(self):
        p = self.install("--locale", "en")
        self.assertIn("Installed the skill copy at", p.stdout)
        p = self.run_ae("uninstall", "--locale", "en")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertIn("Removed the skill copy", p.stdout)
        self.assertIn("Kept the review ledger", p.stdout)
        p = self.install()
        self.assertIn("已安装技能副本到", p.stdout)
        p = self.run_ae("uninstall")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertIn("已删除技能副本", p.stdout)
        self.assertIn("已保留审阅台账", p.stdout)

    def test_uninstall_not_installed_message_is_localized(self):
        p = self.run_ae("uninstall", "--locale", "en")
        self.assertEqual(p.returncode, 0)
        self.assertIn("nothing to uninstall", p.stdout)

    def test_unknown_link_agent_still_refused_and_localized(self):
        p = self.run_ae("install", "--link", "bogus", "--locale", "en")
        self.assertEqual(p.returncode, 2)
        self.assertIn("Unknown agent name: bogus", p.stderr)
        self.assertIn("codex", p.stderr, "合法取值不翻译")
        p = self.run_ae("install", "--link", "bogus")
        self.assertEqual(p.returncode, 2)
        self.assertIn("未知的 Agent 名称：bogus", p.stderr)

    def test_occupied_link_position_still_refused_and_localized(self):
        # 与其他 ae 测试一样把 CODEX_HOME 指到临时目录：否则 --link 会写到本机真实的 ~/.codex
        target = self.home / ".codex" / "skills" / "annotation-everything"
        target.mkdir(parents=True)
        p = self.run_ae("install", "--link", "codex", "--locale", "en",
                        CODEX_HOME=str(self.home / ".codex"))
        self.assertEqual(p.returncode, 2, p.stdout + p.stderr)
        self.assertIn("left untouched", p.stderr)
        self.assertIn(str(target), p.stderr)
        self.assertTrue(target.is_dir(), "非链接内容不被覆盖")

    def test_repair_plan_and_dry_run_are_localized(self):
        self.install()
        (self.skills / "annotation-everything" / "手工改动.txt").write_text("x", encoding="utf-8")
        p = self.run_ae("repair", "--dry-run", "--locale", "en")
        self.assertEqual(p.returncode, 1, p.stdout + p.stderr)
        self.assertIn("Repair plan:", p.stdout)
        self.assertIn("print only", p.stdout)
        self.assertIn("Re-sync the skill copy", p.stdout)
        p = self.run_ae("repair", "--dry-run")
        self.assertEqual(p.returncode, 1)
        self.assertIn("修复计划：", p.stdout)
        self.assertIn("重新同步技能副本", p.stdout)
        self.assertIn("只打印不执行", p.stdout)

    def test_destructive_flag_note_is_not_weakened_in_english(self):
        """翻译后的破坏性提示必须仍然说明未执行，不能暗示已经动手。"""
        ledger_home = self.root / "ledger"
        ledger_home.mkdir(parents=True, exist_ok=True)
        review = self.root / "review"
        review.mkdir()
        (ledger_home / "reviews.json").write_text(json.dumps(
            {"version": 1, "reviews": {"a" * 16: {"dir": str(self.root / "gone"), "port": None}}}),
            encoding="utf-8")
        p = self.run_ae("repair", "--dry-run", "--locale", "en")
        self.assertEqual(p.returncode, 1, p.stdout + p.stderr)
        self.assertIn("not executed this time", p.stdout)
        self.assertIn("--forget-stale", p.stdout)
        p = self.run_ae("repair", "--dry-run", "--locale", "en", "--forget-stale")
        self.assertIn("Remove stale ledger entries", p.stdout)
        self.assertEqual(p.returncode, 1, "--dry-run 仍然不动台账")

    def test_purge_refusals_stay_localized_and_specific(self):
        home = self.root / "ledger"
        home.mkdir(parents=True, exist_ok=True)
        (home / "reviews.json").write_text(json.dumps(
            {"version": 1, "reviews": {"b" * 16: {"dir": str(home / "inside"), "port": None}}}),
            encoding="utf-8")
        self.install()
        p = self.run_ae("uninstall", "--purge", "--locale", "en")
        self.assertEqual(p.returncode, 2, p.stdout + p.stderr)
        self.assertIn("refusing to delete", p.stderr)
        self.assertIn("never touches review directories", p.stderr)
        self.assertTrue((self.skills / "annotation-everything" / "SKILL.md").is_file(),
                        "安全检查不过时什么都不删")
        p = self.run_ae("uninstall", "--purge")
        self.assertEqual(p.returncode, 2)
        self.assertIn("拒绝删除", p.stderr)

    def test_uninstall_refuses_running_servers_in_both_languages(self):
        """检测到 serve.py 运行时拒绝卸载；两种语言都拒绝，且都不暗示已停止。"""
        self.install()
        copy = self.skills / "annotation-everything"
        # 假 ps：全表查询只上报这一个合成 serve.py 进程，绝不枚举本机真实进程
        self.bin = self.root / "bin"
        self.bin.mkdir()
        self.ps_table = self.root / "ps-table.txt"
        self.ps_table.write_text(f"4242 {sys.executable} {copy / 'scripts' / 'serve.py'} review\n",
                                 encoding="utf-8")
        real_ps = shutil.which("ps") or "/bin/ps"
        shim = self.bin / "ps"
        shim.write_text(f'#!/bin/sh\nif [ "$1 $2" = "-ww -eo" ]; then\n  cat "{self.ps_table}"\n'
                        f'else\n  exec "{real_ps}" "$@"\nfi\n', encoding="utf-8")
        shim.chmod(0o755)
        env = {**self.env(), "PATH": str(self.bin) + os.pathsep + os.environ.get("PATH", os.defpath)}
        for locale, needle in ((None, "请先停止服务"), ("en", "Stop the services first")):
            p = subprocess.run([sys.executable, str(AE), "uninstall",
                                *(["--locale", locale] if locale else [])],
                               env=env, capture_output=True, text=True, timeout=60)
            self.assertEqual(p.returncode, 1, p.stdout + p.stderr)
            self.assertIn(needle, p.stderr)
            self.assertIn("--stop-servers", p.stderr)
            self.assertTrue((copy / "SKILL.md").is_file(), "拒绝卸载时不动技能副本")

    def test_ledger_error_follows_the_chosen_language(self):
        """R2 回归：台账错误曾只读 AE_LOCALE，英文会话里会混出中文。"""
        ledger_home = self.root / "ledger"
        ledger_home.mkdir(parents=True, exist_ok=True)
        (ledger_home / "reviews.json").write_text("{坏的", encoding="utf-8")
        self.install()
        for argv, needle, absent in (
                (["--locale", "en"], "Cannot read the review ledger", "审阅台账"),
                ([], "审阅台账", "Cannot read the review ledger")):
            p = self.run_ae("doctor", *argv)
            self.assertEqual(p.returncode, 1, p.stdout + p.stderr)
            self.assertIn(needle, p.stdout, argv)
            self.assertNotIn(absent, p.stdout, argv)
        # 显式参数压过 AE_LOCALE：不能一种是中文、另一种是英文
        p = self.run_ae("doctor", "--locale", "zh-CN", AE_LOCALE="en")
        self.assertEqual(p.returncode, 1)
        self.assertIn("审阅台账", p.stdout)
        self.assertNotIn("Cannot read the review ledger", p.stdout)

    def test_unexpected_failure_is_localized_and_traces_stay_out(self):
        """未预期的异常也走本地化的一句话，不把堆栈抛给用户。"""
        sys.path.insert(0, str(ROOT))
        import ae
        previous = ae.current_locale()
        self.addCleanup(ae.set_locale, previous)
        out, err = io.StringIO(), io.StringIO()
        with unittest.mock.patch.object(ae, "read_record", side_effect=RuntimeError("boom")):
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                code = ae.main(["--locale", "en", "doctor", "--json"])
        self.assertEqual(code, 2)
        self.assertIn("Command failed (RuntimeError)", err.getvalue())
        self.assertNotIn("Traceback", err.getvalue())
        self.assertEqual(json.loads(out.getvalue())["error"].count("RuntimeError"), 1)
        out, err = io.StringIO(), io.StringIO()
        with unittest.mock.patch.object(ae, "read_record", side_effect=RuntimeError("boom")):
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                code = ae.main(["doctor", "--json"])
        self.assertEqual(code, 2)
        self.assertIn("命令执行失败（RuntimeError）", err.getvalue())


if __name__ == "__main__":
    unittest.main()
