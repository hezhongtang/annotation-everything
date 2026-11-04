"""serve.py 的语言选择、本地化诊断与稳定的安全拒绝代码。

拒绝的 HTTP 状态与原因代码是给机器和脚本判定的，任何语言下都不能变；
给人看的说明按语言变，且不回显请求内容。服务只用合成审阅目录，不起后台常驻进程。
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
import threading
import unittest
import unittest.mock
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SERVE_PY = ROOT / "skill" / "annotation-everything" / "scripts" / "serve.py"
SCRIPTS = SERVE_PY.parent
sys.path.insert(0, str(SCRIPTS))

import i18n  # noqa: E402
import serve  # noqa: E402
sys.path.insert(0, str(ROOT))
import host_isolation  # noqa: E402
host_isolation.require_for_module(__name__)


def tmpdir() -> Path:
    return Path(tempfile.mkdtemp())


class RejectLanguageTests(unittest.TestCase):
    """安全拒绝：代码与状态稳定，说明按语言，不回显请求内容。"""

    def start(self, review_dir: Path, **kwargs):
        srv = serve.ReviewServer(review_dir, 0, log_to_file=False, **kwargs)
        threading.Thread(target=srv.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True).start()
        self.addCleanup(srv.shutdown)
        self.addCleanup(srv.server_close)
        return srv

    def host(self, srv) -> dict:
        return {"Host": f"127.0.0.1:{srv.port}"}

    def test_default_stays_chinese(self):
        d = tmpdir()
        (d / "index.html").write_text("x")
        srv = self.start(d)
        status, _headers, body = serve._req(srv.port, "GET", "/", {"Host": "evil.example"})
        self.assertEqual((status, json.loads(body)["code"]), (421, "bad-host"))
        self.assertIn("Host 不是本服务", json.loads(body)["error"])

    def test_reject_keeps_code_and_status_in_english(self):
        d = tmpdir()
        (d / "index.html").write_text("x")
        srv = self.start(d, locale="en")
        cases = [
            ({"Host": "evil.example"}, "GET", "/", None, 421, "bad-host",
             "This Host is not this service"),
            ({"Host": "PLACEHOLDER", "Sec-Fetch-Site": "cross-site"}, "GET", "/api/notes", None,
             403, "cross-site", "another site or port"),
        ]
        for headers, method, path, body, want_status, want_code, needle in cases:
            headers = {k: (f"127.0.0.1:{srv.port}" if v == "PLACEHOLDER" else v)
                       for k, v in headers.items()}
            status, _h, raw = serve._req(srv.port, method, path, headers, body)
            self.assertEqual((status, json.loads(raw)["code"]), (want_status, want_code))
            self.assertIn(needle, json.loads(raw)["error"])

    def test_write_rejections_keep_their_codes_in_english(self):
        d = tmpdir()
        (d / "index.html").write_text("x")
        srv = self.start(d, locale="en")
        host = self.host(srv)
        origin = {"Origin": f"http://127.0.0.1:{srv.port}"}
        status, _h, raw = serve._req(srv.port, "POST", "/api/notes", host, {"page": 1, "text": "x"})
        self.assertEqual((status, json.loads(raw)["code"]), (403, "no-origin"))
        self.assertIn("missing Origin", json.loads(raw)["error"])
        status, _h, raw = serve._req(srv.port, "POST", "/api/notes",
                                     {**host, **origin, "Content-Type": "text/plain"},
                                     raw=b"{}")
        self.assertEqual((status, json.loads(raw)["code"]), (415, "bad-content-type"))
        self.assertIn("application/json", json.loads(raw)["error"])
        status, _h, raw = serve._req(srv.port, "POST", "/api/notes",
                                     {**host, **origin, "Content-Type": "application/json"},
                                     raw='{"page": 1'.encode("utf-8"))
        self.assertEqual((status, json.loads(raw)["code"]), (400, "bad-json"))
        status, _h, raw = serve._req(srv.port, "OPTIONS", "/", {**host, **origin})
        self.assertEqual((status, json.loads(raw)["code"]), (405, "preflight"))
        status, _h, raw = serve._req(srv.port, "GET", "/nope", host)
        self.assertEqual((status, json.loads(raw)["code"]), (404, "not-found"))

    def test_unknown_error_is_safe_and_does_not_echo_request(self):
        d = tmpdir()
        (d / "index.html").write_text("x")
        srv = self.start(d, locale="en")
        secret = "NOTE-TO-CLAUDE:<script>secret</script>"
        with unittest.mock.patch.object(serve.Handler, "_index",
                                        side_effect=ValueError(secret)):
            status, _h, raw = serve._req(srv.port, "GET", "/?x=" + secret, self.host(srv))
        self.assertEqual(status, 500)
        payload = json.loads(raw)
        self.assertEqual(payload["code"], "server-error")
        self.assertNotIn(secret, payload["error"])
        self.assertIn("retried later", payload["error"])

    def test_text_limit_keeps_bad_note_code_and_exposes_localizable_details(self):
        for language, expected in (("en", "maximum 20000 characters"), ("zh-CN", "上限 20000 字")):
            d = tmpdir()
            self.addCleanup(shutil.rmtree, d)
            (d / "index.html").write_text("synthetic review")
            srv = self.start(d, locale=language)
            headers = {**self.host(srv), "Origin": f"http://127.0.0.1:{srv.port}",
                       "Content-Type": "application/json"}
            for field in ("text", "edit.to"):
                note = {"page": 1, "text": "synthetic annotation"}
                oversized = "X" * 20001
                if field == "text":
                    note["text"] = oversized
                else:
                    note["edit"] = {"op": "replace", "from": "old", "to": oversized}
                with self.subTest(language=language, field=field):
                    status, _h, raw = serve._req(srv.port, "POST", "/api/notes", headers, note)
                    payload = json.loads(raw)
                    self.assertEqual((status, payload["code"]), (400, "bad-note"))
                    self.assertEqual(payload["reason"], "text-too-long")
                    self.assertEqual(payload["params"], {"field": field, "limit": 20000})
                    self.assertIn(expected, payload["error"])
                    self.assertIn(field, payload["error"])
                    self.assertNotIn(oversized, payload["error"])
                    self.assertFalse((d / "notes.json").exists())

    def test_unknown_error_message_is_localized(self):
        d = tmpdir()
        (d / "index.html").write_text("x")
        srv = self.start(d)
        with unittest.mock.patch.object(serve.Handler, "_index", side_effect=ValueError("boom")):
            _status, _h, raw = serve._req(srv.port, "GET", "/", self.host(srv))
        self.assertIn("服务端写入失败", json.loads(raw)["error"])

    def test_no_index_message_is_localized(self):
        d = tmpdir()
        srv = self.start(d, locale="en")
        status, _h, raw = serve._req(srv.port, "GET", "/", self.host(srv))
        self.assertEqual((status, json.loads(raw)["code"]), (404, "no-index"))
        self.assertIn("build_viewer.py", json.loads(raw)["error"])


class ServeCliLanguageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="serve-i18n-test-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.home = self.root / "home"
        self.home.mkdir()
        self.ledger = self.root / "ledger"
        self.ledger.mkdir()
        self.env = {k: v for k, v in os.environ.items()
                    if k not in ("AE_LOCALE", "LC_ALL", "LC_MESSAGES", "LANG")}
        self.env.update({"HOME": str(self.home), "AE_HOME": str(self.ledger),
                         "PYTHONDONTWRITEBYTECODE": "1"})

    def run_serve(self, *args: str, script: Path = SERVE_PY, **env):
        merged = {**self.env, **env}
        return subprocess.run([sys.executable, str(script), *args], env=merged,
                              capture_output=True, text=True, timeout=60)

    def write_ledger(self, *review_dirs: Path):
        reviews = {f"{i:016x}": {"dir": str(d), "port": None} for i, d in enumerate(review_dirs)}
        (self.ledger / "reviews.json").write_text(
            json.dumps({"version": 1, "reviews": reviews}, ensure_ascii=False), encoding="utf-8")

    def make_review(self, name: str = "review") -> Path:
        d = self.root / name
        d.mkdir()
        (d / "index.html").write_text("<!doctype html><title>合成对照页</title>", encoding="utf-8")
        (d / "review.json").write_text(json.dumps({"reviewId": "0" * 16, "name": "合成审阅"}),
                                       encoding="utf-8")
        return d

    def test_list_language_flag_and_env(self):
        d = self.make_review()
        self.write_ledger(d)
        for argv, want in ((["list", "--locale", "en"], "Review ledger:"),
                           (["list"], "审阅台账："),
                           (["--locale", "en", "list"], "Review ledger:")):
            p = self.run_serve(*argv)
            self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
            self.assertIn(want, p.stdout, argv)
            self.assertIn(str(d), p.stdout, argv)
        p = self.run_serve("list", AE_LOCALE="en")
        self.assertIn("Review ledger:", p.stdout)

    def test_list_empty_message_is_localized(self):
        p = self.run_serve("list", "--locale", "en")
        self.assertIn("No reviews in the review ledger", p.stdout)
        p = self.run_serve("list")
        self.assertIn("审阅台账里没有审阅", p.stdout)

    def test_list_state_field_stays_machine_readable_and_text_is_localized(self):
        d = self.make_review()
        self.write_ledger(d)
        p = self.run_serve("list", "--json", "--locale", "en")
        row = json.loads(p.stdout)["reviews"][0]
        self.assertEqual(row["state"], "已停", "既有判定字段不随语言改值")
        self.assertEqual(row["state_id"], "stopped")
        p = self.run_serve("list", "--json")
        row = json.loads(p.stdout)["reviews"][0]
        self.assertEqual((row["state"], row["state_id"]), ("已停", "stopped"), "中英文等价")
        p = self.run_serve("list", "--locale", "en")
        self.assertIn("stopped", p.stdout)

    def test_state_id_covers_every_branch_of_the_label_helper(self):
        """state_id 有五个取值，_state_label 对每个各有一条分支；逐个钉住。"""
        running = self.make_review("running")
        temp = self.make_review("temp")
        mismatch = self.make_review("mismatch")
        # 台账按 0000…0 / 0000…1 / 0000…2 登记；前两份标识对上，第三份故意对不上
        for directory, rid in ((running, "0" * 16), (temp, "1" * 16), (mismatch, "f" * 16)):
            (directory / "review.json").write_text(
                json.dumps({"reviewId": rid, "name": "合成审阅"}), encoding="utf-8")
        # running：状态文件的端口与台账一致；temp：端口与台账里的不一致（本次临时端口）
        import serve as srv
        (running / srv.STATUS_FILE).write_text(json.dumps(
            {"pid": os.getpid(), "port": 8765, "url": "http://127.0.0.1:8765/"}), encoding="utf-8")
        (temp / srv.STATUS_FILE).write_text(json.dumps(
            {"pid": os.getpid(), "port": 8766, "url": "http://127.0.0.1:8766/"}), encoding="utf-8")
        (self.ledger / "reviews.json").write_text(json.dumps({"version": 1, "reviews": {
            "0" * 16: {"dir": str(running), "port": 8765},
            "1" * 16: {"dir": str(temp), "port": 9999},
            "2" * 16: {"dir": str(mismatch), "port": None}}}), encoding="utf-8")
        p = self.run_serve("list", "--json", "--locale", "en")
        rows = {Path(r["dir"]).name: (r["state"], r["state_id"]) for r in json.loads(p.stdout)["reviews"]}
        self.assertEqual(rows["running"], ("运行中", "running"))
        self.assertEqual(rows["temp"], ("运行中（临时端口 8766）", "running-temp"))
        self.assertEqual(rows["mismatch"][1], "mismatch")
        p = self.run_serve("list", "--json")
        rows = {Path(r["dir"]).name: (r["state"], r["state_id"]) for r in json.loads(p.stdout)["reviews"]}
        self.assertEqual(rows["running"], ("运行中", "running"), "state 恒为中文，state_id 与语言无关")

    def test_list_reports_missing_directory_in_both_languages(self):
        d = self.make_review()
        self.write_ledger(d)
        import shutil
        shutil.rmtree(d)
        p = self.run_serve("list", "--json", "--locale", "en")
        row = json.loads(p.stdout)["reviews"][0]
        self.assertEqual((row["state"], row["state_id"]), ("目录不在", "missing"))
        p = self.run_serve("list", "--locale", "en")
        self.assertIn("directory gone", p.stdout)
        self.assertIn("serve.py forget", p.stdout)

    def test_status_language(self):
        d = self.make_review()
        p = self.run_serve("status", str(d), "--locale", "en")
        self.assertEqual(p.returncode, 3, p.stdout + p.stderr)
        self.assertEqual(json.loads(p.stdout)["server"], "Not running")
        p = self.run_serve("status", str(d))
        self.assertEqual(json.loads(p.stdout)["server"], "未运行")

    def test_forget_language_and_release_unchanged(self):
        d = self.make_review()
        self.write_ledger(d)
        p = self.run_serve("forget", "0" * 4, "--locale", "en")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertIn("Removed " + "0" * 16, p.stdout)
        self.assertIn("untouched", p.stdout)
        self.assertTrue(d.is_dir(), "forget 不动审阅目录")
        p = self.run_serve("forget", "0" * 4)
        self.assertEqual(p.returncode, 2)
        self.assertIn("错误：", p.stderr)

    def test_forget_error_is_localized(self):
        p = self.run_serve("forget", "ffff", "--locale", "en")
        self.assertEqual(p.returncode, 2)
        self.assertIn("Error:", p.stderr)
        self.assertIn("serve.py list", p.stderr)
        p = self.run_serve("forget", "zzzz")
        self.assertEqual(p.returncode, 2)
        self.assertIn("不是审阅标识", p.stderr)

    def test_help_is_localized(self):
        p = self.run_serve("--help", "--locale", "en")
        self.assertEqual(p.returncode, 0)
        self.assertIn("listens on 127.0.0.1 only", p.stdout)
        p = self.run_serve("list", "--help", "--locale", "en")
        self.assertIn("Only list reviews under this directory", p.stdout)
        p = self.run_serve("forget", "--help", "--locale", "en")
        self.assertIn("Remove a review from the ledger", p.stdout)

    def test_unsupported_locale_is_command_error(self):
        p = self.run_serve("list", "--locale", "de")
        self.assertEqual(p.returncode, 2)
        self.assertNotIn("Traceback", p.stderr)
        self.assertEqual(p.stdout, "")

    def test_check_command_language(self):
        p = self.run_serve("check", "--locale", "en")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertTrue(p.stdout.strip())
        p = self.run_serve("check")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertTrue(p.stdout.strip())

    def test_review_locale_is_the_last_fallback(self):
        """R3 回归：命令行没定语言时，用审阅里保存的 locale。"""
        d = self.make_review()
        (d / "review.json").write_text(json.dumps({"reviewId": "0" * 16, "locale": "en"}),
                                       encoding="utf-8")
        p = self.run_serve("status", str(d))
        self.assertEqual(json.loads(p.stdout)["server"], "Not running", p.stdout + p.stderr)
        p = self.run_serve("status", str(d), AE_LOCALE="zh-CN")
        self.assertEqual(json.loads(p.stdout)["server"], "未运行")
        p = self.run_serve("status", str(d), "--locale", "zh-CN", AE_LOCALE="en")
        self.assertEqual(json.loads(p.stdout)["server"], "未运行", "显式参数最优先")

    def test_start_keeps_explicit_locale_before_directory_or_subcommand(self):
        d = self.make_review()
        manifest = d / "review.json"
        # 界面语言是进程级状态：本用例定过语言，结束后解除定住，别污染后面的用例
        self.addCleanup(i18n.set_locale, None)
        for explicit, fallback in (("en", "zh-CN"), ("zh-CN", "en")):
            manifest.write_text(json.dumps({"reviewId": "0" * 16, "locale": fallback}),
                                encoding="utf-8")
            for args in (["--locale", explicit, str(d)],
                         ["--locale=" + explicit, str(d)],
                         ["--locale", explicit, "serve", str(d)],
                         ["--locale=" + explicit, "serve", str(d)],
                         ["serve", str(d), "--locale", explicit]):
                with self.subTest(args=args), \
                        unittest.mock.patch.dict(os.environ, {"AE_LOCALE": fallback}), \
                        unittest.mock.patch.object(serve, "serve", return_value=0) as start:
                    self.assertEqual(serve.main([*args, "--no-open"]), 0)
                    self.assertEqual(start.call_args.kwargs["locale"], explicit)

    def test_locale_reaches_skill_copy(self):
        """从技能副本运行 serve.py 时，--locale 与 AE_LOCALE 同样生效（词典随副本走）。"""
        copy = self.home / ".claude" / "skills" / "annotation-everything"
        copy.mkdir(parents=True)
        shutil.copytree(SCRIPTS, copy / "scripts",
                        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        shutil.copytree(SCRIPTS.parent / "locales", copy / "locales")
        p = self.run_serve("list", "--locale", "en", script=copy / "scripts" / "serve.py")
        self.assertIn("No reviews in the review ledger", p.stdout)
        p = self.run_serve("list", script=copy / "scripts" / "serve.py", AE_LOCALE="en")
        self.assertIn("No reviews in the review ledger", p.stdout)
        p = self.run_serve("list", script=copy / "scripts" / "serve.py")
        self.assertIn("审阅台账里没有审阅", p.stdout)


class StderrPurityTests(unittest.TestCase):
    def test_json_status_output_is_not_mixed_with_terminal_hints(self):
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp)
            (d / "index.html").write_text("x")
            env = {k: v for k, v in os.environ.items()
                   if k not in ("AE_LOCALE", "LC_ALL", "LC_MESSAGES", "LANG")}
            env["AE_HOME"] = str(Path(tmp) / "ledger")
            p = subprocess.run([sys.executable, str(SERVE_PY), "status", str(d), "--locale", "en"],
                               env=env, capture_output=True, text=True, timeout=60)
            self.assertEqual(p.returncode, 3)
            self.assertEqual(json.loads(p.stdout)["dir"], str(d.resolve()))
