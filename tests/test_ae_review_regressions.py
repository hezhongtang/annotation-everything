"""PR #25 审查缺陷的回归测试；仅使用合成文件与注入的进程表。"""
from __future__ import annotations

import argparse
import contextlib
import io
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import ae

SERVE = ROOT / ae.SERVE_PY


class ReviewRegressionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="ae-review-regression-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.home = self.root / "home"
        self.home.mkdir()
        self.ledger = self.root / "ledger"
        self.copy = self.root / "skills" / ae.SKILL_NAME
        self.copy.mkdir(parents=True)
        self.env = {"HOME": str(self.home), "AE_HOME": str(self.ledger),
                    "CLAUDE_SKILLS_DIR": str(self.copy.parent),
                    "CODEX_HOME": str(self.home / ".codex"), "PYTHONDONTWRITEBYTECODE": "1"}
        self.env_patch = patch.dict(os.environ, self.env)
        self.env_patch.start()
        self.addCleanup(self.env_patch.stop)

    def ctx(self, **overrides):
        values = dict(skill_copy=self.copy, repo=ROOT, record=None, ledger_home=self.ledger,
                      process_lister=lambda: [], port_probe=lambda port: None)
        values.update(overrides)
        return ae.DoctorContext(**values)

    def run_ae(self, *args, cwd=None):
        return subprocess.run([sys.executable, str(ROOT / "ae.py"), *args],
                              cwd=cwd or self.root, capture_output=True, text=True, timeout=40)

    def uninstall(self):
        with patch.object(ae, "running_servers", return_value=[]), \
                contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            return ae.main(["uninstall", "--purge"])

    def test_failed_checks_always_include_a_diagnostic_command(self):
        self.ledger.mkdir()
        (self.ledger / "reviews.json").write_text("{invalid", encoding="utf-8")
        cmd = shlex.join([sys.executable, str(SERVE), str(self.root / "gone"), "--port", "18900"])
        ctx = self.ctx(port_probe=lambda port: {"app": "other"} if port == 8765 else None,
                       process_lister=lambda: [(4242, cmd)])
        with patch.object(ae.sys, "version_info", (3, 8, 0)):
            results = ae.run_doctor(ctx)
        self.assertGreaterEqual(sum(r.level == ae.LEVEL_FAIL for r in results), 4)
        for result in results:
            if result.level == ae.LEVEL_FAIL:
                with self.subTest(code=result.code):
                    self.assertTrue(result.fix_command.strip())

    def test_deleted_review_reports_port_from_command_line(self):
        cmd = shlex.join([sys.executable, str(SERVE), str(self.root / "gone"), "--port=18900"])
        result = ae.check_orphan_servers(self.ctx(process_lister=lambda: [(4242, cmd)]))
        self.assertEqual(result.level, ae.LEVEL_FAIL)
        self.assertIn("4242", result.message)
        self.assertIn("18900", result.message)

    def test_absolute_script_invocation_outside_repo_omits_dev_checks(self):
        p = self.run_ae("doctor", "--json")
        self.assertIn(p.returncode, (0, 1), p.stderr)
        results = json.loads(p.stdout)["results"]
        self.assertNotIn(ae.CATEGORY_DEV, {r["category"] for r in results})

    def test_malformed_install_record_exits_two_without_traceback(self):
        for data in ([1], "record", {"repo": []}, {"links": "bad"}, {"links": [{"path": 1}]}):
            with self.subTest(data=data):
                (self.copy / ae.RECORD_NAME).write_text(json.dumps(data), encoding="utf-8")
                p = self.run_ae("doctor", "--json")
                self.assertEqual(p.returncode, 2, p.stdout + p.stderr)
                self.assertNotIn("Traceback", p.stderr)

    def test_unexpected_command_exception_exits_two(self):
        with patch.object(ae, "cmd_doctor", side_effect=RuntimeError("synthetic failure")), \
                contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(ae.main(["doctor", "--json"]), 2)

    def test_install_recovers_from_invalid_record(self):
        (self.copy / ae.RECORD_NAME).write_text("[1]", encoding="utf-8")
        p = self.run_ae("install")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertIsInstance(json.loads((self.copy / ae.RECORD_NAME).read_text()), dict)

    def test_purge_rejects_corrupt_ledger_before_removing_copy(self):
        self.ledger.mkdir()
        path = self.ledger / "reviews.json"
        path.write_text("{invalid", encoding="utf-8")
        self.assertEqual(self.uninstall(), 2)
        self.assertEqual(path.read_text(), "{invalid")
        self.assertTrue(self.copy.is_dir())

    def test_purge_preserves_unregistered_review_and_unknown_files(self):
        self.ledger.mkdir()
        (self.ledger / "reviews.json").write_text('{"version":1,"reviews":{}}', encoding="utf-8")
        for name, is_dir in (("unregistered-review", True), ("material.txt", False)):
            with self.subTest(name=name):
                path = self.ledger / name
                if is_dir:
                    path.mkdir()
                    marker = path / "index.html"
                else:
                    marker = path
                marker.write_text("synthetic review", encoding="utf-8")
                self.assertEqual(self.uninstall(), 2)
                self.assertEqual(marker.read_text(), "synthetic review")
                self.assertTrue(self.copy.exists())
                marker.unlink()
                if is_dir:
                    path.rmdir()

    def test_purge_rejects_invalid_ledger_structure(self):
        self.ledger.mkdir()
        for data in ([], {"reviews": []}, {"reviews": {"a" * 16: {"dir": []}}}):
            with self.subTest(data=data):
                path = self.ledger / "reviews.json"
                path.write_text(json.dumps(data), encoding="utf-8")
                self.assertEqual(self.uninstall(), 2)
                self.assertTrue(path.exists())

    def test_service_detection_ignores_mentions_and_unrelated_scripts(self):
        own = shlex.join([sys.executable, str(SERVE), str(self.root / "review")])
        fake = [(41001, "grep serve.py notes.txt"),
                (41002, f"python3 {self.root}/other/serve.py /review"),
                (41003, shlex.join([sys.executable, "-c", "pass", str(SERVE), "/review"])),
                (41004, own)]
        self.assertEqual([pid for pid, _ in ae.running_servers(lister=lambda: fake)], [41004])

    def test_stop_servers_rejects_unrelated_live_process(self):
        proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
        self.addCleanup(self.stop, proc)
        command = f"{sys.executable} {SERVE} {self.root}/review"
        with patch.object(ae.os, "kill") as kill:
            stuck = ae.stop_servers([(proc.pid, command)], wait_seconds=0)
        kill.assert_not_called()
        self.assertEqual([pid for pid, _ in stuck], [proc.pid])
        self.assertIsNone(proc.poll())

    @staticmethod
    def stop(proc):
        if proc.poll() is None:
            proc.terminate()
        proc.communicate(timeout=10)


if __name__ == "__main__":
    unittest.main()
