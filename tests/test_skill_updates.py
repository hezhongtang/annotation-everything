"""技能更新的命令行行为；仓库、技能副本与审阅数据均为合成环境。"""
from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
import unittest


ROOT = Path(__file__).resolve().parents[1]
SKILL = "annotation-everything"
sys.path.insert(0, str(ROOT))
import host_isolation  # noqa: E402
host_isolation.require_for_module(__name__)


class SkillUpdateTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="ae-update-test-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.home = self.root / "home"
        self.home.mkdir()
        self.repo = self.root / "source"
        self.remote = self.root / "remote.git"
        self.skills = self.root / "custom-skills"
        self.copy = self.skills / SKILL
        self.env = {**os.environ, "HOME": str(self.home),
                    "AE_HOME": str(self.home / "ledger"),
                    "XDG_DATA_HOME": str(self.home / "data"),
                    "CODEX_HOME": str(self.home / ".codex"),
                    "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull,
                    "GIT_TERMINAL_PROMPT": "0", "PYTHONDONTWRITEBYTECODE": "1"}
        self.git("init", "--bare", str(self.remote))
        self.repo.mkdir()
        self.git("init", str(self.repo))
        self.git("-C", str(self.repo), "config", "user.name", "Synthetic Test")
        self.git("-C", str(self.repo), "config", "user.email", "test@example.invalid")
        shutil.copy2(ROOT / "ae.py", self.repo / "ae.py")
        (self.repo / ".gitignore").write_text("ignored-data.txt\n", encoding="utf-8")
        shutil.copytree(ROOT / "skill" / SKILL, self.repo / "skill" / SKILL,
                        ignore=shutil.ignore_patterns("__pycache__", "*.pyc", ".DS_Store"))
        self.git("-C", str(self.repo), "remote", "add", "origin", str(self.remote))
        self.publish("0.9.0")
        installed = self.run_command(self.repo / "ae.py", "install", "--target", str(self.skills),
                                     "--link", "codex")
        self.assertEqual(installed.returncode, 0, installed.stdout + installed.stderr)

    def git(self, *args):
        return subprocess.run(["git", "-c", "core.hooksPath=/dev/null", *args],
                              env=self.env, capture_output=True, text=True, check=True)

    def run_command(self, script, *args):
        return subprocess.run([sys.executable, str(script), *args], env=self.env,
                              capture_output=True, text=True, timeout=45)

    def publish(self, version):
        serve = self.repo / "skill" / SKILL / "scripts" / "serve.py"
        content = serve.read_text(encoding="utf-8")
        import re
        content = re.sub(r'^VERSION = "[^"]+"', f'VERSION = "{version}"', content, flags=re.M)
        serve.write_text(content, encoding="utf-8")
        (self.repo / "skill" / SKILL / "release-marker.txt").write_text(version, encoding="utf-8")
        self.git("-C", str(self.repo), "add", ".")
        self.git("-C", str(self.repo), "commit", "-m", f"Release {version}")
        self.git("-C", str(self.repo), "tag", "-a", f"v{version}", "-m", f"Release {version}")
        self.git("-C", str(self.repo), "push", "origin", "HEAD", "--tags")

    def test_auto_installs_new_release_and_preserves_link_and_review_data(self):
        review = self.home / "ledger" / "reviews.json"
        review.parent.mkdir(parents=True)
        review.write_text('{"reviews": {"synthetic": "keep"}}', encoding="utf-8")
        self.publish("0.10.0")
        work = self.repo / "local-work.txt"
        work.write_text("unfinished work", encoding="utf-8")
        source_head = self.git("-C", str(self.repo), "rev-parse", "HEAD").stdout

        result = self.run_command(self.copy / "scripts" / "update.py", "auto")

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("0.10.0", result.stdout)
        self.assertEqual((self.copy / "release-marker.txt").read_text(encoding="utf-8"), "0.10.0")
        record = json.loads((self.copy / ".install-record.json").read_text(encoding="utf-8"))
        self.assertEqual(record["version"], "0.10.0")
        link = self.home / ".codex" / "skills" / SKILL
        self.assertTrue(link.is_symlink())
        self.assertEqual(link.resolve(), self.copy.resolve())
        self.assertEqual(json.loads(review.read_text(encoding="utf-8")),
                         {"reviews": {"synthetic": "keep"}})
        self.assertEqual(work.read_text(encoding="utf-8"), "unfinished work")
        self.assertEqual(self.git("-C", str(self.repo), "rev-parse", "HEAD").stdout, source_head)
        doctor = self.run_command(self.repo / "ae.py", "doctor", "--target", str(self.skills), "--json")
        checked = json.loads(doctor.stdout)["results"]
        self.assertTrue(all(item["status"] == "pass" for item in checked
                            if item["code"] in ("skill-copy-sync", "skill-copy-version", "skill-links")),
                        doctor.stdout + doctor.stderr)

    def test_check_reports_a_release_without_installing_it(self):
        self.publish("0.10.0")
        result = self.run_command(self.copy / "scripts" / "update.py", "check", "--locale", "en")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("0.10.0", result.stdout)
        self.assertIn("0.9.0", result.stdout)
        self.assertEqual((self.copy / "release-marker.txt").read_text(encoding="utf-8"), "0.9.0")

    def test_installed_skill_entry_runs_auto_check_and_explains_reload(self):
        self.publish("0.10.0")
        entry = (self.copy / "SKILL.md").read_text(encoding="utf-8")
        self.assertIn('python3 "$S/update.py" auto --locale LANG', entry)
        self.assertIn("reload this `SKILL.md`", entry)
        result = subprocess.run(["sh", "-c", 'python3 "$S/update.py" auto --locale en'],
                                env={**self.env, "S": str(self.copy / "scripts")},
                                capture_output=True, text=True, timeout=45)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("0.10.0", result.stdout)
        self.assertEqual((self.copy / "release-marker.txt").read_text(encoding="utf-8"), "0.10.0")

    def test_auto_preserves_local_changes_and_reports_available_update(self):
        self.publish("0.10.0")
        local = self.copy / "local-notes.txt"
        local.write_text("keep local changes", encoding="utf-8")
        result = self.run_command(self.copy / "scripts" / "update.py", "auto", "--locale", "en")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("local changes", result.stderr)
        self.assertEqual(local.read_text(encoding="utf-8"), "keep local changes")
        self.assertEqual((self.copy / "release-marker.txt").read_text(encoding="utf-8"), "0.9.0")

    def test_auto_offline_keeps_installed_version_and_does_not_block_work(self):
        self.git("-C", str(self.repo), "remote", "set-url", "origin", str(self.root / "missing.git"))
        result = self.run_command(self.copy / "scripts" / "update.py", "auto", "--locale", "en")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("keeping the current version", result.stderr)
        self.assertEqual((self.copy / "release-marker.txt").read_text(encoding="utf-8"), "0.9.0")

    def test_auto_rejects_a_tag_with_the_wrong_bundled_version(self):
        self.publish("0.9.1")
        self.git("-C", str(self.repo), "tag", "-a", "v9.0.0", "-m", "invalid release")
        self.git("-C", str(self.repo), "push", "origin", "v9.0.0")
        result = self.run_command(self.copy / "scripts" / "update.py", "auto", "--locale", "en")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("do not match", result.stderr)
        self.assertEqual((self.copy / "release-marker.txt").read_text(encoding="utf-8"), "0.9.0")

    def test_auto_rejects_a_release_missing_a_module_update_imports(self):
        self.git("-C", str(self.repo), "rm", "--quiet", f"skill/{SKILL}/scripts/fsutil.py")
        self.publish("0.10.0")
        result = self.run_command(self.copy / "scripts" / "update.py", "auto", "--locale", "en")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("do not match", result.stderr)
        self.assertEqual((self.copy / "release-marker.txt").read_text(encoding="utf-8"), "0.9.0")
        self.assertTrue((self.copy / "scripts" / "fsutil.py").is_file())

    def test_auto_rejects_a_release_missing_the_overview_runtime(self):
        self.git("-C", str(self.repo), "rm", "--quiet", f"skill/{SKILL}/scripts/review_overview.py")
        self.publish("0.10.0")
        result = self.run_command(self.copy / "scripts" / "update.py", "auto", "--locale", "en")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("do not match", result.stderr)
        self.assertEqual((self.copy / "release-marker.txt").read_text(encoding="utf-8"), "0.9.0")
        self.assertTrue((self.copy / "scripts" / "review_overview.py").is_file())

    def test_daily_limit_does_not_prevent_manual_update(self):
        first = self.run_command(self.copy / "scripts" / "update.py", "auto")
        self.assertEqual(first.returncode, 0, first.stdout + first.stderr)
        self.publish("0.10.0")
        second = self.run_command(self.copy / "scripts" / "update.py", "auto")
        self.assertEqual(second.returncode, 0, second.stdout + second.stderr)
        self.assertEqual((self.copy / "release-marker.txt").read_text(encoding="utf-8"), "0.9.0")
        manual = self.run_command(self.copy / "scripts" / "update.py", "now")
        self.assertEqual(manual.returncode, 0, manual.stdout + manual.stderr)
        self.assertEqual((self.copy / "release-marker.txt").read_text(encoding="utf-8"), "0.10.0")

    def test_auto_does_not_downgrade_or_install_prereleases(self):
        self.git("-C", str(self.repo), "tag", "v9.0.0-rc.1")
        self.git("-C", str(self.repo), "push", "origin", "v9.0.0-rc.1")
        result = self.run_command(self.copy / "scripts" / "update.py", "auto")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual((self.copy / "release-marker.txt").read_text(encoding="utf-8"), "0.9.0")

    def test_opt_out_disables_auto_without_disabling_manual_check(self):
        self.publish("0.10.0")
        self.env["AE_AUTO_UPDATE"] = "0"
        auto = self.run_command(self.copy / "scripts" / "update.py", "auto")
        check = self.run_command(self.copy / "scripts" / "update.py", "check", "--locale", "en")
        self.assertEqual(auto.returncode, 0, auto.stdout + auto.stderr)
        self.assertEqual((self.copy / "release-marker.txt").read_text(encoding="utf-8"), "0.9.0")
        self.assertEqual(check.returncode, 0, check.stdout + check.stderr)
        self.assertIn("0.10.0", check.stdout)

    def test_update_help_and_errors_follow_the_chosen_language(self):
        script = self.copy / "scripts" / "update.py"
        chinese = self.run_command(script, "--help", "--locale", "zh-CN")
        english = self.run_command(script, "--help", "--locale", "en")
        invalid = self.run_command(script, "unknown", "--locale", "zh-CN")
        self.assertEqual(chinese.returncode, 0, chinese.stdout + chinese.stderr)
        self.assertIn("检查并更新", chinese.stdout)
        self.assertEqual(english.returncode, 0, english.stdout + english.stderr)
        self.assertIn("Check and update", english.stdout)
        self.assertEqual(invalid.returncode, 2)
        self.assertIn("未知的更新模式", invalid.stderr)

    def test_notes_restart_preserves_python_isolation_options(self):
        site = self.root / "untrusted-site"
        site.mkdir()
        marker = self.root / "unexpected-sitecustomize"
        (site / "sitecustomize.py").write_text(
            "import os, pathlib\npathlib.Path(os.environ['SITE_MARKER']).write_text('ran')\n")
        env = {**self.env, "PYTHONPATH": str(site), "SITE_MARKER": str(marker)}
        script = str(self.copy / "scripts" / "notes.py")
        fallback = "import runpy, sys; sys.orig_argv = None; sys.argv = [sys.argv[1], '--help']; runpy.run_path(sys.argv[0], run_name='__main__')"
        for command in ([sys.executable, "-I", script, "--help"],
                        [sys.executable, "-I", "-c", fallback, script]):
            with self.subTest(command=command[:3]):
                result = subprocess.run(command, env=env, capture_output=True, text=True, timeout=45)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertFalse(marker.exists(), "restarting notes.py must preserve -I")

    def test_failed_atomic_exchange_keeps_previous_copy(self):
        self.publish("0.10.0")
        runner = """
import sys
sys.path.insert(0, sys.argv[1])
import update
def exchange(*_):
    raise OSError('synthetic filesystem failure')
update.exchange_directories = exchange
sys.exit(update.main(['now', '--locale', 'en']))
"""
        result = subprocess.run([sys.executable, "-c", runner, str(self.copy / "scripts")],
                                env=self.env, capture_output=True, text=True, timeout=45)
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("did not complete", result.stderr)
        self.assertEqual((self.copy / "release-marker.txt").read_text(encoding="utf-8"), "0.9.0")
        self.assertTrue((self.home / ".codex" / "skills" / SKILL).is_symlink())

    def test_process_killed_after_atomic_exchange_leaves_new_copy_available(self):
        self.publish("0.10.0")
        runner = """
import os, signal, sys
sys.path.insert(0, sys.argv[1])
import update
exchange = update.exchange_directories
def exchange_then_stop(*args):
    exchange(*args)
    os.kill(os.getpid(), signal.SIGKILL)
update.exchange_directories = exchange_then_stop
sys.exit(update.main(['now', '--locale', 'en']))
"""
        result = subprocess.run([sys.executable, "-c", runner, str(self.copy / "scripts")],
                                env=self.env, capture_output=True, text=True, timeout=45)
        self.assertEqual(result.returncode, -signal.SIGKILL, result.stdout + result.stderr)
        self.assertEqual((self.copy / "release-marker.txt").read_text(encoding="utf-8"), "0.10.0")
        self.assertTrue((self.home / ".codex" / "skills" / SKILL / "SKILL.md").is_file())

    def test_local_directories_links_and_nested_records_are_not_deleted(self):
        self.publish("0.10.0")
        (self.copy / "empty-dir").mkdir()
        (self.copy / "custom").mkdir()
        (self.copy / "custom" / ".install-record.json").write_text("keep", encoding="utf-8")
        os.symlink(str(self.root), self.copy / "directory-link")
        result = self.run_command(self.copy / "scripts" / "update.py", "auto", "--locale", "en")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("local changes", result.stderr)
        self.assertTrue((self.copy / "empty-dir").is_dir())
        self.assertTrue((self.copy / "directory-link").is_symlink())
        self.assertEqual((self.copy / "custom" / ".install-record.json").read_text(), "keep")

    def test_local_permission_change_is_not_overwritten(self):
        self.publish("0.10.0")
        marker = self.copy / "release-marker.txt"
        marker.chmod(marker.stat().st_mode ^ 0o100)
        mode = marker.stat().st_mode
        result = self.run_command(self.copy / "scripts" / "update.py", "auto", "--locale", "en")
        self.assertIn("local changes", result.stderr)
        self.assertEqual(marker.stat().st_mode, mode)
        self.assertEqual(marker.read_text(encoding="utf-8"), "0.9.0")

    def test_skill_root_permission_change_is_not_overwritten(self):
        self.publish("0.10.0")
        self.copy.chmod(self.copy.stat().st_mode ^ 0o010)
        mode = self.copy.stat().st_mode
        result = self.run_command(self.copy / "scripts" / "update.py", "auto", "--locale", "en")
        self.assertIn("local changes", result.stderr)
        self.assertEqual(self.copy.stat().st_mode, mode)
        self.assertEqual((self.copy / "release-marker.txt").read_text(encoding="utf-8"), "0.9.0")

    def test_update_keeps_a_private_skill_root_private(self):
        (self.repo / "skill" / SKILL).chmod(0o700)
        installed = self.run_command(self.repo / "ae.py", "install", "--target", str(self.skills))
        self.assertEqual(installed.returncode, 0, installed.stdout + installed.stderr)
        self.publish("0.10.0")
        result = self.run_command(self.copy / "scripts" / "update.py", "now", "--locale", "en")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.copy.stat().st_mode & 0o777, 0o700)

    def test_concurrent_edit_during_exchange_is_backed_up_and_reported(self):
        self.publish("0.10.0")
        runner = """
import sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
import update
exchange = update.exchange_directories
def exchange_with_edit(old, new):
    (Path(old) / 'late-change.txt').write_text('keep this change')
    exchange(old, new)
update.exchange_directories = exchange_with_edit
sys.exit(update.main(['now', '--locale', 'en']))
"""
        result = subprocess.run([sys.executable, "-c", runner, str(self.copy / "scripts")],
                                env=self.env, capture_output=True, text=True, timeout=45)
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("Updated annotation-everything", result.stdout)
        self.assertIn("changed while updating", result.stderr)
        backup = list(self.skills.glob(".ae-update-*/annotation-everything/late-change.txt"))
        self.assertEqual(len(backup), 1, result.stderr)
        self.assertEqual(backup[0].read_text(), "keep this change")

    def test_unreadable_previous_copy_after_exchange_still_reports_update(self):
        self.publish("0.10.0")
        runner = """
import sys
sys.path.insert(0, sys.argv[1])
import update
exchange = update.exchange_directories
snapshot = update.content_manifest.snapshot
swapped = False
def exchange_then_mark(*args):
    global swapped
    exchange(*args)
    swapped = True
def read_tree(path):
    if swapped:
        raise PermissionError('synthetic read failure')
    return snapshot(path)
update.exchange_directories = exchange_then_mark
update.content_manifest.snapshot = read_tree
sys.exit(update.main(['now', '--locale', 'en']))
"""
        result = subprocess.run([sys.executable, "-c", runner, str(self.copy / "scripts")],
                                env=self.env, capture_output=True, text=True, timeout=45)
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("Updated annotation-everything", result.stdout)
        self.assertIn("previous copy changed", result.stderr)
        self.assertEqual((self.copy / "release-marker.txt").read_text(encoding="utf-8"), "0.10.0")
        self.assertTrue(list(self.skills.glob(".ae-update-*/annotation-everything/release-marker.txt")))

    def test_install_target_can_equal_xdg_data_home(self):
        target = Path(self.env["XDG_DATA_HOME"])
        installed = self.run_command(self.repo / "ae.py", "install", "--target", str(target))
        self.assertEqual(installed.returncode, 0, installed.stdout + installed.stderr)
        other_copy = target / SKILL
        self.publish("0.10.0")
        result = self.run_command(other_copy / "scripts" / "update.py", "now", "--locale", "en")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual((other_copy / "release-marker.txt").read_text(encoding="utf-8"), "0.10.0")
        doctor = self.run_command(self.repo / "ae.py", "doctor", "--target", str(target), "--json")
        checks = json.loads(doctor.stdout)["results"]
        self.assertEqual(next(c["status"] for c in checks if c["code"] == "skill-copy-sync"), "pass")

    def test_legacy_process_in_a_path_with_spaces_defers_update(self):
        target = self.root / "custom skills with spaces"
        installed = self.run_command(self.repo / "ae.py", "install", "--target", str(target))
        self.assertEqual(installed.returncode, 0, installed.stdout + installed.stderr)
        copy = target / SKILL
        self.publish("0.10.0")
        script = copy / "scripts" / "serve.py"
        original = script.read_bytes()
        marker = self.root / "legacy-ready"
        script.write_text("import pathlib, sys, time\npathlib.Path(sys.argv[1]).write_text('ready')\ntime.sleep(30)\n")
        process = subprocess.Popen([sys.executable, str(script), str(marker)], env=self.env,
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.addCleanup(lambda: process.poll() is None and process.kill())
        deadline = time.monotonic() + 10
        while not marker.exists() and time.monotonic() < deadline:
            if process.poll() is not None:
                self.fail("synthetic legacy service exited before it was ready")
            time.sleep(0.05)
        self.assertTrue(marker.exists())
        script.write_bytes(original)
        result = self.run_command(copy / "scripts" / "update.py", "auto", "--locale", "en")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("running", result.stderr)
        self.assertEqual((copy / "release-marker.txt").read_text(encoding="utf-8"), "0.9.0")
        process.terminate()
        process.wait(timeout=10)

    def test_unrelated_relative_serve_script_does_not_block_update(self):
        self.publish("0.10.0")
        other = self.root / "unrelated"
        other.mkdir()
        marker = self.root / "unrelated-ready"
        (other / "serve.py").write_text(
            "import pathlib, sys, time\npathlib.Path(sys.argv[1]).write_text('ready')\ntime.sleep(30)\n")
        process = subprocess.Popen([sys.executable, "serve.py", str(marker)], cwd=other, env=self.env,
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.addCleanup(lambda: process.poll() is None and process.kill())
        deadline = time.monotonic() + 10
        while not marker.exists() and time.monotonic() < deadline:
            if process.poll() is not None:
                self.fail("synthetic unrelated service exited before it was ready")
            time.sleep(0.05)
        self.assertTrue(marker.exists())
        result = self.run_command(self.copy / "scripts" / "update.py", "now", "--locale", "en")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual((self.copy / "release-marker.txt").read_text(encoding="utf-8"), "0.10.0")
        process.terminate()
        process.wait(timeout=10)

    def test_ignored_files_in_previous_cache_are_not_installed_or_deleted(self):
        self.publish("0.10.0")
        local = self.copy / "local.txt"
        local.write_text("local", encoding="utf-8")
        first = self.run_command(self.copy / "scripts" / "update.py", "auto", "--locale", "en")
        self.assertIn("local changes", first.stderr)
        cache_root = Path(self.env["XDG_DATA_HOME"]) / SKILL / "updates"
        old_cache = next(cache_root.glob("*/release-*/skill/annotation-everything"))
        ignored = old_cache / "ignored-data.txt"
        ignored.write_text("keep", encoding="utf-8")
        local.unlink()
        result = self.run_command(self.copy / "scripts" / "update.py", "now", "--locale", "en")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertFalse((self.copy / "ignored-data.txt").exists())
        self.assertEqual(ignored.read_text(encoding="utf-8"), "keep")

    def test_running_service_defers_update_until_it_stops(self):
        self.publish("0.10.0")
        review = self.root / "review"
        review.mkdir()
        (review / "index.html").write_text("<title>Synthetic</title>", encoding="utf-8")
        (review / "review.json").write_text('{}', encoding="utf-8")  # 无标识：不会登记到台账
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        server_env = {**self.env, "AE_HOME": str(self.home / "other-ledger")}
        server = subprocess.Popen([sys.executable, str(self.copy / "scripts" / "serve.py"), str(review),
                                   "--port", str(port), "--no-open"], env=server_env,
                                  stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.addCleanup(lambda: server.poll() is None and server.kill())
        deadline = time.monotonic() + 10
        while not (review / ".annotation-server.json").exists() and time.monotonic() < deadline:
            if server.poll() is not None:
                self.fail("synthetic review server exited before it was ready")
            time.sleep(0.05)
        self.assertTrue((review / ".annotation-server.json").is_file())
        result = self.run_command(self.copy / "scripts" / "update.py", "auto", "--locale", "en")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("running", result.stderr)
        self.assertIsNone(server.poll(), "the existing review server stays running")
        self.assertEqual((self.copy / "release-marker.txt").read_text(encoding="utf-8"), "0.9.0")
        server.terminate()
        server.wait(timeout=10)
        manual = self.run_command(self.copy / "scripts" / "update.py", "now", "--locale", "en")
        self.assertEqual(manual.returncode, 0, manual.stdout + manual.stderr)
        self.assertEqual((self.copy / "release-marker.txt").read_text(encoding="utf-8"), "0.10.0")

    def test_waiter_with_different_ledger_defers_update(self):
        self.publish("0.10.0")
        review = self.root / "waiting-review"
        review.mkdir()
        (review / "review.json").write_text('{"reviewId":"0123456789abcdef"}', encoding="utf-8")
        wait_env = {**self.env, "AE_HOME": str(self.home / "other-ledger")}
        waiter = subprocess.Popen([sys.executable, str(self.copy / "scripts" / "notes.py"), "wait", str(review),
                                   "--timeout", "30"], env=wait_env,
                                  stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.addCleanup(lambda: waiter.poll() is None and waiter.kill())
        deadline = time.monotonic() + 10
        while not list(review.glob(".annotation-wait.*.json")) and time.monotonic() < deadline:
            if waiter.poll() is not None:
                self.fail("synthetic waiter exited before it was ready")
            time.sleep(0.05)
        self.assertTrue(list(review.glob(".annotation-wait.*.json")))
        result = self.run_command(self.copy / "scripts" / "update.py", "auto", "--locale", "en")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("annotation process is running", result.stderr)
        self.assertEqual((self.copy / "release-marker.txt").read_text(encoding="utf-8"), "0.9.0")
        waiter.terminate()
        waiter.wait(timeout=10)

    def test_service_started_during_exchange_refuses_stale_code(self):
        self.publish("0.10.0")
        ready = self.root / "exchange-ready"
        resume = self.root / "exchange-resume"
        server_ready = self.root / "server-imported"
        review = self.root / "review-during-update"
        review.mkdir()
        (review / "index.html").write_text("<title>Synthetic</title>", encoding="utf-8")
        (review / "review.json").write_text('{}', encoding="utf-8")
        update_runner = """
import sys, time
from pathlib import Path
sys.path.insert(0, sys.argv[1])
import update
exchange = update.exchange_directories
def delayed_exchange(*args):
    Path(sys.argv[2]).write_text('ready')
    deadline = time.monotonic() + 10
    while not Path(sys.argv[3]).exists() and time.monotonic() < deadline:
        time.sleep(0.02)
    exchange(*args)
update.exchange_directories = delayed_exchange
sys.exit(update.main(['now', '--locale', 'en']))
"""
        updater = subprocess.Popen([sys.executable, "-c", update_runner, str(self.copy / "scripts"),
                                    str(ready), str(resume)], env=self.env,
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        self.addCleanup(lambda: updater.poll() is None and updater.kill())
        deadline = time.monotonic() + 10
        while not ready.exists() and time.monotonic() < deadline:
            if updater.poll() is not None:
                self.fail("updater exited before the exchange boundary")
            time.sleep(0.05)
        self.assertTrue(ready.exists())
        serve_runner = """
import sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
import serve
Path(sys.argv[2]).write_text('ready')
sys.exit(serve.serve(Path(sys.argv[3]), None, False, False, locale='en'))
"""
        server = subprocess.Popen([sys.executable, "-c", serve_runner, str(self.copy / "scripts"),
                                   str(server_ready), str(review)], env={**self.env, "AE_HOME": str(self.root / "other-home")},
                                  stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        self.addCleanup(lambda: server.poll() is None and server.kill())
        deadline = time.monotonic() + 10
        while not server_ready.exists() and time.monotonic() < deadline:
            if server.poll() is not None:
                self.fail("synthetic server exited before it imported its code")
            time.sleep(0.05)
        self.assertTrue(server_ready.exists())
        resume.write_text("go", encoding="utf-8")
        out, err = updater.communicate(timeout=15)
        self.assertEqual(updater.returncode, 0, out + err)
        out, err = server.communicate(timeout=15)
        self.assertEqual(server.returncode, 6, out + err)
        self.assertIn("skill was just updated", err)

    def test_notes_started_during_exchange_reloads_the_new_code(self):
        notes_source = self.repo / "skill" / SKILL / "scripts" / "notes.py"
        source = notes_source.read_text(encoding="utf-8")
        notes_source.write_text(source.replace("def main(argv:",
                                              "print('NEW-NOTES-CODE-LOADED', file=sys.stderr)\ndef main(argv:", 1),
                                encoding="utf-8")
        self.publish("0.10.0")
        exchange_ready = self.root / "notes-exchange-ready"
        exchange_resume = self.root / "notes-exchange-resume"
        compiled = self.root / "old-notes-compiled"
        updater_code = """
import sys, time
from pathlib import Path
sys.path.insert(0, sys.argv[1])
import update
exchange = update.exchange_directories
def delayed_exchange(*args):
    Path(sys.argv[2]).write_text('ready')
    deadline = time.monotonic() + 10
    while not Path(sys.argv[3]).exists() and time.monotonic() < deadline:
        time.sleep(0.02)
    exchange(*args)
update.exchange_directories = delayed_exchange
sys.exit(update.main(['now', '--locale', 'en']))
"""
        updater = subprocess.Popen([sys.executable, "-c", updater_code, str(self.copy / "scripts"),
                                    str(exchange_ready), str(exchange_resume)], env=self.env,
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        self.addCleanup(lambda: updater.poll() is None and updater.kill())
        deadline = time.monotonic() + 10
        while not exchange_ready.exists() and time.monotonic() < deadline:
            if updater.poll() is not None:
                self.fail("updater exited before the exchange boundary")
            time.sleep(0.05)
        self.assertTrue(exchange_ready.exists())
        notes_code = """
import sys
from pathlib import Path
script, ready = sys.argv[1:]
code = compile(Path(script).read_text(), script, 'exec')
Path(ready).write_text('compiled')
sys.argv = [script, '--help']
exec(code, {'__name__': '__main__', '__file__': script})
"""
        notes = subprocess.Popen([sys.executable, "-c", notes_code, str(self.copy / "scripts" / "notes.py"),
                                  str(compiled)], env=self.env,
                                 stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        self.addCleanup(lambda: notes.poll() is None and notes.kill())
        deadline = time.monotonic() + 10
        while not compiled.exists() and time.monotonic() < deadline:
            if notes.poll() is not None:
                self.fail("synthetic notes command exited before compiling old code")
            time.sleep(0.05)
        self.assertTrue(compiled.exists())
        exchange_resume.write_text("go", encoding="utf-8")
        out, err = updater.communicate(timeout=15)
        self.assertEqual(updater.returncode, 0, out + err)
        out, err = notes.communicate(timeout=15)
        self.assertEqual(notes.returncode, 0, out + err)
        self.assertIn("NEW-NOTES-CODE-LOADED", err)


if __name__ == "__main__":
    unittest.main()
