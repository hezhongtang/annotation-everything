"""技能更新的命令行行为；仓库、技能副本与审阅数据均为合成环境。"""
from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
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

    def test_failed_filesystem_replace_restores_previous_copy(self):
        self.publish("0.10.0")
        runner = """
import os, sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
import update
real_replace = os.replace
failed = False
def replace(source, destination):
    global failed
    if Path(destination) == Path(sys.argv[2]) and not failed:
        failed = True
        raise OSError('synthetic installation failure')
    return real_replace(source, destination)
os.replace = replace
sys.exit(update.main(['now', '--locale', 'en']))
"""
        result = subprocess.run([sys.executable, "-c", runner, str(self.copy / "scripts"), str(self.copy)],
                                env=self.env, capture_output=True, text=True, timeout=45)
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("did not complete", result.stderr)
        self.assertEqual((self.copy / "release-marker.txt").read_text(encoding="utf-8"), "0.9.0")
        self.assertTrue((self.home / ".codex" / "skills" / SKILL).is_symlink())

    def test_failed_restore_keeps_the_previous_copy_for_recovery(self):
        self.publish("0.10.0")
        runner = """
import os, sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
import update
real_replace = os.replace
def replace(source, destination):
    if Path(destination) == Path(sys.argv[2]):
        raise OSError('synthetic restore failure')
    return real_replace(source, destination)
os.replace = replace
sys.exit(update.main(['now', '--locale', 'en']))
"""
        result = subprocess.run([sys.executable, "-c", runner, str(self.copy / "scripts"), str(self.copy)],
                                env=self.env, capture_output=True, text=True, timeout=45)
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        backups = list(self.skills.glob(".ae-update-*/previous"))
        self.assertEqual(len(backups), 1, result.stderr)
        self.assertIn(str(backups[0]), result.stderr)
        self.assertEqual((backups[0] / "release-marker.txt").read_text(encoding="utf-8"), "0.9.0")

    def test_running_service_defers_update_until_it_stops(self):
        self.publish("0.10.0")
        review = self.root / "review"
        review.mkdir()
        (review / "index.html").write_text("<title>Synthetic</title>", encoding="utf-8")
        (review / "review.json").write_text('{"reviewId":"0123456789abcdef"}', encoding="utf-8")
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        server = subprocess.Popen([sys.executable, str(self.copy / "scripts" / "serve.py"), str(review),
                                   "--port", str(port), "--no-open"], env=self.env,
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


if __name__ == "__main__":
    unittest.main()
