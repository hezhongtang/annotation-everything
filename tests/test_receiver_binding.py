"""Receiver identity through the CLI and HTTP boundaries; synthetic reviews only."""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "skill/annotation-everything/scripts"
sys.path.insert(0, str(SCRIPTS))
import notes_store as store
import serve


class ReceiverCliTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="ae-receiver-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.review = self.root / "review"
        self.review.mkdir()
        (self.review / "review.json").write_text(json.dumps({"reviewId": "1" * 16}), encoding="utf-8")
        self.env = {k: v for k, v in os.environ.items()
                    if k not in ("CODEX_THREAD_ID", "CODEX_SESSION_ID", "CLAUDECODE", "AE_HARNESS", "AE_SESSION_ID", "AE_LOCALE")}
        self.env.update(AE_HOME=str(self.root / "ledger"), PYTHONDONTWRITEBYTECODE="1")

    def cli(self, script, *args, **env):
        return subprocess.run([sys.executable, str(SCRIPTS / script), *args],
                              env={**self.env, **env}, capture_output=True, text=True, timeout=10)

    def bind(self, harness="Claude Code", session="session-a", *extra):
        result = self.cli("wakeup.py", "bind", str(self.review), "--adapter", "wait",
                          "--harness", harness, "--session", session, *extra)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return json.loads(result.stdout)

    def server(self):
        server = serve.ReviewServer(self.review, 0, log_to_file=False)
        worker = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
        worker.start()
        def close():
            server.shutdown()
            server.server_close()
            worker.join(2)
        self.addCleanup(close)
        return server

    def request(self, server, method="GET", path="/api/notes", body=None):
        headers = {"Host": f"127.0.0.1:{server.port}", "Origin": f"http://127.0.0.1:{server.port}",
                   "Content-Type": "application/json", "X-AE-Review": "1" * 16}
        code, _, raw = serve._req(server.port, method, path, headers, body)
        return code, json.loads(raw)

    def test_only_bound_session_can_claim_even_when_by_matches(self):
        self.bind()
        store.handoff_submit(self.review, "Synthetic annotation batch")
        self.assertEqual(self.cli("notes.py", "claim", str(self.review)).returncode, 2)
        wrong = self.cli("notes.py", "claim", str(self.review), "--by", "Claude Code",
                         AE_HARNESS="Codex", AE_SESSION_ID="session-b")
        self.assertEqual(wrong.returncode, 2, wrong.stdout + wrong.stderr)
        same_id_other_harness = self.cli("notes.py", "claim", str(self.review),
                                         AE_HARNESS="Codex", AE_SESSION_ID="session-a")
        self.assertEqual(same_id_other_harness.returncode, 2)
        correct = self.cli("notes.py", "claim", str(self.review),
                           AE_HARNESS="Claude Code", AE_SESSION_ID="session-a")
        self.assertEqual(correct.returncode, 0, correct.stdout + correct.stderr)
        self.assertEqual(json.loads(correct.stdout)["seq"], 1)

    def test_status_identifies_the_bound_harness_and_session(self):
        self.bind()
        result = self.cli("wakeup.py", "status", str(self.review))
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        receiver = json.loads(result.stdout)["receiver"]
        self.assertEqual((receiver["harness"], receiver["sessionId"], receiver["state"]),
                         ("Claude Code", "session-a", "idle"))

    def test_check_supports_a_wait_binding_without_a_desktop_socket(self):
        self.bind()
        checked = self.cli("wakeup.py", "check", str(self.review))
        self.assertEqual(checked.returncode, 0, checked.stdout + checked.stderr)
        self.assertEqual(json.loads(checked.stdout)["receiver"]["sessionId"], "session-a")

    def test_unbinding_running_service_requires_restart(self):
        self.bind()
        server = self.server()
        self.assertEqual(self.cli("wakeup.py", "unbind", str(self.review)).returncode, 0)
        self.assertEqual(self.request(server)[1]["receiver"]["state"], "restart-required")

    def test_invalid_binding_is_reported_and_can_be_explicitly_repaired(self):
        self.bind()
        server = self.server()
        path = self.review / ".agent-binding.json"
        value = json.loads(path.read_text())
        value["bindingId"] = 123
        path.write_text(json.dumps(value))
        code, data = self.request(server)
        self.assertEqual(code, 200, data)
        self.assertEqual(data["receiver"], {"state": "error"})
        self.assertEqual(self.cli("notes.py", "claim", str(self.review)).returncode, 2)
        self.bind("Claude Code", "session-a", "--replace")
        self.assertEqual(self.request(server)[1]["receiver"]["state"], "restart-required")

    def test_transfer_requires_explicit_replace_and_finished_batch(self):
        self.bind()
        other = ("bind", str(self.review), "--adapter", "wait", "--harness", "Codex", "--session", "session-b")
        self.assertEqual(self.cli("wakeup.py", *other).returncode, 2)
        store.handoff_submit(self.review, "Synthetic first batch")
        owner = {"AE_HARNESS": "Claude Code", "AE_SESSION_ID": "session-a"}
        self.assertEqual(self.cli("notes.py", "claim", str(self.review), **owner).returncode, 0)
        self.assertEqual(self.cli("wakeup.py", *other, "--replace").returncode, 2)
        self.assertEqual(self.cli("wakeup.py", "unbind", str(self.review)).returncode, 2)
        finished = self.cli("notes.py", "done", str(self.review), "--seq", "1", **owner)
        self.assertEqual(finished.returncode, 0, finished.stdout + finished.stderr)
        transferred = self.cli("wakeup.py", *other, "--replace")
        self.assertEqual(transferred.returncode, 0, transferred.stdout + transferred.stderr)
        store.handoff_submit(self.review, "Synthetic second batch")
        self.assertEqual(self.cli("notes.py", "claim", str(self.review), **owner).returncode, 2)
        new_owner = self.cli("notes.py", "claim", str(self.review), AE_HARNESS="Codex", AE_SESSION_ID="session-b")
        self.assertEqual(new_owner.returncode, 0, new_owner.stdout + new_owner.stderr)

    def test_bound_waiter_can_receive_with_explicit_identity(self):
        self.bind()
        store.handoff_submit(self.review, "Synthetic waiting batch")
        waited = self.cli("notes.py", "wait", str(self.review), "--timeout", "1",
                          "--harness", "Claude Code", "--session", "session-a")
        self.assertEqual(waited.returncode, 0, waited.stdout + waited.stderr)
        self.assertIn("第 1 批", waited.stdout)

    def test_another_session_cannot_finish_or_resolve_the_bound_batch(self):
        self.bind()
        _, note = store.add(self.review, {"page": 1, "text": "Synthetic annotation"})
        store.handoff_submit(self.review, "Synthetic batch")
        owner = {"AE_HARNESS": "Claude Code", "AE_SESSION_ID": "session-a"}
        self.assertEqual(self.cli("notes.py", "claim", str(self.review), **owner).returncode, 0)
        other = {"AE_HARNESS": "Codex", "AE_SESSION_ID": "session-b"}
        self.assertEqual(self.cli("notes.py", "resolve", str(self.review), note["id"], **other).returncode, 2)
        self.assertEqual(self.cli("notes.py", "done", str(self.review), "--seq", "1", **other).returncode, 2)
        resolved = self.cli("notes.py", "resolve", str(self.review), note["id"], **owner)
        self.assertEqual(resolved.returncode, 0, resolved.stdout + resolved.stderr)

    def test_http_receiver_reports_waiting_processing_and_idle_without_losing_identity(self):
        self.bind()
        server = self.server()
        code, data = self.request(server)
        self.assertEqual(code, 200)
        receiver = data["receiver"]
        self.assertEqual((receiver["harness"], receiver["sessionId"], receiver["state"]),
                         ("Claude Code", "session-a", "idle"))
        self.assertNotIn(str(self.review), json.dumps(receiver))
        self.assertNotIn("socketPath", receiver)
        process = subprocess.Popen([sys.executable, str(SCRIPTS / "notes.py"), "wait", str(self.review),
                                    "--timeout", "5", "--interval", ".1"],
                                   env={**self.env, "AE_HARNESS": "Claude Code", "AE_SESSION_ID": "session-a"},
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        def stop():
            if process.poll() is None:
                process.terminate()
            process.communicate(timeout=5)
        self.addCleanup(stop)
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            _, data = self.request(server)
            if data["receiver"]["state"] == "waiting":
                break
            time.sleep(.03)
        self.assertEqual(data["receiver"]["state"], "waiting")
        code, result = self.request(server, "POST", "/api/handoff",
                                    {"message": "Synthetic", "receiverBindingId": receiver["bindingId"]})
        self.assertEqual(code, 200, result)
        stdout, stderr = process.communicate(timeout=5)
        self.assertEqual(process.returncode, 0, stdout + stderr)
        self.assertEqual(self.request(server)[1]["receiver"]["state"], "processing")
        done = self.cli("notes.py", "done", str(self.review), "--seq", "1",
                        AE_HARNESS="Claude Code", AE_SESSION_ID="session-a")
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        after = self.request(server)[1]["receiver"]
        self.assertEqual((after["bindingId"], after["state"]), (receiver["bindingId"], "idle"))

    def test_waiter_exits_after_transfer_and_cannot_claim_for_its_old_session(self):
        self.bind()
        server = self.server()
        process = subprocess.Popen([sys.executable, str(SCRIPTS / "notes.py"), "wait", str(self.review),
                                    "--timeout", "5", "--interval", ".1"],
                                   env={**self.env, "AE_HARNESS": "Claude Code", "AE_SESSION_ID": "session-a"},
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        def stop():
            if process.poll() is None:
                process.terminate()
            process.communicate(timeout=5)
        self.addCleanup(stop)
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            if self.request(server)[1]["receiver"]["state"] == "waiting":
                break
            time.sleep(.03)
        self.assertEqual(self.request(server)[1]["receiver"]["state"], "waiting")
        self.bind("Codex", "session-b", "--replace")
        out, err = process.communicate(timeout=3)
        self.assertEqual(process.returncode, 2, out + err)
        store.handoff_submit(self.review, "Synthetic batch after transfer")
        claimed = self.cli("notes.py", "claim", str(self.review), AE_HARNESS="Codex", AE_SESSION_ID="session-b")
        self.assertEqual(claimed.returncode, 0, claimed.stdout + claimed.stderr)

    def test_http_handoff_rejects_stale_destination_and_requires_service_restart(self):
        self.bind()
        first = self.server()
        old = self.request(first)[1]["receiver"]
        self.bind("Codex", "session-b", "--replace")
        changed = self.request(first)[1]["receiver"]
        self.assertEqual(changed["state"], "restart-required")
        code, _ = self.request(first, "POST", "/api/handoff", {"receiverBindingId": old["bindingId"]})
        self.assertEqual(code, 409)
        self.assertEqual(self.request(first, path="/api/handoff")[1]["total"], 0)
        restarted = self.server()
        current = self.request(restarted)[1]["receiver"]
        code, _ = self.request(restarted, "POST", "/api/handoff", {"receiverBindingId": old["bindingId"]})
        self.assertEqual(code, 409)
        code, _ = self.request(restarted, "POST", "/api/handoff", {})
        self.assertEqual(code, 409, "A bound review must not silently accept an unidentified destination")
        code, data = self.request(restarted, "POST", "/api/handoff", {"receiverBindingId": current["bindingId"]})
        self.assertEqual(code, 200, data)

    def test_review_handoff_also_checks_receiver_after_explicit_transfer(self):
        self.bind()
        initial = self.server()
        old = self.request(initial)[1]["receiver"]
        store.handoff_submit(self.review, "Synthetic review batch", target="review")
        owner = {"AE_HARNESS": "Claude Code", "AE_SESSION_ID": "session-a"}
        self.assertEqual(self.cli("notes.py", "claim", str(self.review), **owner).returncode, 0)
        self.assertEqual(self.cli("notes.py", "done", str(self.review), "--draft", **owner).returncode, 0)
        self.bind("Codex", "session-b", "--replace")
        restarted = self.server()
        current = self.request(restarted)[1]["receiver"]
        code, _ = self.request(restarted, "POST", "/api/handoff/review", {"seq": 1, "receiverBindingId": old["bindingId"]})
        self.assertEqual(code, 409)
        code, data = self.request(restarted, "POST", "/api/handoff/review", {"seq": 1, "receiverBindingId": current["bindingId"]})
        self.assertEqual(code, 200, data)
        self.assertEqual(self.cli("notes.py", "claim", str(self.review), **owner).returncode, 2)
        self.assertEqual(self.cli("notes.py", "claim", str(self.review), AE_HARNESS="Codex", AE_SESSION_ID="session-b").returncode, 0)
        self.assertEqual(self.cli("wakeup.py", "unbind", str(self.review)).returncode, 2)


if __name__ == "__main__":
    unittest.main()
