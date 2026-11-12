"""Review-scoped stop lifecycle through real CLI, process and HTTP boundaries."""
from __future__ import annotations

import json
import os
from pathlib import Path
import signal
import socket
import struct
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "skill/annotation-everything/scripts"
sys.path.insert(0, str(SCRIPTS))
import fsutil
import ledger
import serve

sys.path.insert(0, str(ROOT))
import host_isolation
host_isolation.require_for_module(__name__)

THREAD = "11111111-1111-4111-8111-111111111111"


class Desktop:
    def __init__(self, path):
        self.path = path
        self.requests = []
        self.cancelled = []
        self.turns = {}
        self.cancel_mode = "confirm"
        self.listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.listener.bind(str(path))
        path.chmod(0o600)
        self.listener.listen()
        self.listener.settimeout(.1)
        self.stopped = threading.Event()
        self.worker = threading.Thread(target=self.run, daemon=True)
        self.worker.start()

    def run(self):
        while not self.stopped.is_set():
            try:
                client, _ = self.listener.accept()
            except socket.timeout:
                continue
            with client:
                def exact(count):
                    data = b""
                    while len(data) < count:
                        part = client.recv(count - len(data))
                        if not part:
                            return None
                        data += part
                    return data
                while True:
                    head = exact(4)
                    if not head:
                        break
                    request = json.loads(exact(struct.unpack("<I", head)[0]))
                    self.requests.append(request)
                    method = request["method"]
                    result = {"clientId": "fixture"} if method == "initialize" else {}
                    if method == "thread-follower-start-turn":
                        message_id = request["params"]["turnStart"]["request"]["clientUserMessageId"]
                        turn_id = "turn-" + message_id
                        self.turns[message_id] = turn_id
                        result = {"result": {"turn": {"id": turn_id, "status": "inProgress"}}}
                    elif method == "thread-follower-cancel-turn":
                        turn_id = request["params"]["turnId"]
                        self.cancelled.append(request["params"])
                        if self.cancel_mode == "accept":
                            result = {"accepted": True}
                        elif self.cancel_mode == "timeout":
                            time.sleep(.5)
                            continue
                        else:
                            result = {"result": {"turn": {"id": turn_id, "status": "cancelled"}}}
                    rejected = method == "thread-follower-cancel-turn" and self.cancel_mode == "reject"
                    reply = {"type": "response", "requestId": request["requestId"],
                             "resultType": "error" if rejected else "success",
                             "error": "cancel-rejected" if rejected else None,
                             "result": result, "handledByClientId": "owner"}
                    raw = json.dumps(reply).encode()
                    client.sendall(struct.pack("<I", len(raw)) + raw)

    def close(self):
        self.stopped.set()
        self.worker.join(2)
        self.listener.close()


class ReviewStopTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="ae-review-stop-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.env_patch = patch.dict(os.environ, {"AE_HOME": str(self.root / "ledger")})
        self.env_patch.start()
        self.addCleanup(self.env_patch.stop)
        self.env = {k: v for k, v in os.environ.items()
                    if k not in ("CODEX_THREAD_ID", "AE_HARNESS", "AE_SESSION_ID", "AE_LOCALE")}
        self.env.update(AE_HOME=str(self.root / "ledger"), PYTHONDONTWRITEBYTECODE="1")
        self.processes = []
        self.addCleanup(self.stop_processes)

    def stop_processes(self):
        for process in self.processes:
            if process.poll() is None:
                process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)

    def review(self, name, rid):
        directory = self.root / name
        directory.mkdir(mode=0o700)
        (directory / "review.json").write_text(json.dumps({"reviewId": rid, "reviewDir": str(directory)}),
                                                encoding="utf-8")
        (directory / "index.html").write_text("<!doctype html><title>Synthetic review</title>", encoding="utf-8")
        ledger.register(rid, directory)
        return directory

    def cli(self, script, *args, timeout=15):
        return subprocess.run([sys.executable, str(SCRIPTS / script), *args], env=self.env,
                              capture_output=True, text=True, timeout=timeout)

    def start(self, directory):
        process = subprocess.Popen([sys.executable, str(SCRIPTS / "serve.py"), str(directory), "--no-open"],
                                   env=self.env, stdin=subprocess.DEVNULL,
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.processes.append(process)
        deadline = time.monotonic() + 8
        health = None
        while time.monotonic() < deadline:
            info = serve.read_status(directory)
            if info and info.get("pid") == process.pid:
                health = serve.probe(info["port"], timeout=.2)
                if health and health.get("reviewId"):
                    return info, health
            if process.poll() is not None:
                self.fail(f"server exited early: {process.returncode}")
            time.sleep(.03)
        self.fail(f"server did not become ready: {health}")

    def request(self, info, method, path, body=None, rid=None):
        port = info["port"]
        headers = {"Host": f"127.0.0.1:{port}", "Origin": f"http://127.0.0.1:{port}",
                   "Content-Type": "application/json"}
        if rid:
            headers["X-AE-Review"] = rid
        status, response_headers, raw = serve._req(port, method, path, headers, body)
        return status, response_headers, json.loads(raw)

    def test_cli_stop_without_background_work_preserves_review_and_other_service(self):
        rid, other_rid = "1a" * 8, "2b" * 8
        directory, other = self.review("target", rid), self.review("other", other_rid)
        bound = self.cli("wakeup.py", "bind", str(directory), "--adapter", "wait",
                         "--harness", "Claude Code", "--session", "shared-session")
        self.assertEqual(bound.returncode, 0, bound.stdout + bound.stderr)
        binding_before = (directory / ".agent-binding.json").read_bytes()
        info, _ = self.start(directory)
        other_info, _ = self.start(other)
        owned_port = ledger.get(rid)["port"]
        status, _, saved = self.request(info, "POST", "/api/notes",
                                        {"page": 1, "text": "Synthetic annotation"}, rid)
        self.assertEqual(status, 201, saved)

        stopped = self.cli("serve.py", "stop", rid[:8])

        self.assertEqual(stopped.returncode, 0, stopped.stdout + stopped.stderr)
        result = json.loads(stopped.stdout)
        self.assertEqual((result["reviewId"], result["state"]), (rid, "stopped"))
        self.assertEqual(result["components"]["http"]["state"], "stopped")
        self.assertEqual(result["components"]["waiters"]["state"], "stopped")
        self.assertEqual(result["components"]["agent"]["state"], "stopped")
        self.assertIsNotNone(result.get("stopId"))
        self.assertEqual(info["pid"], result["components"]["http"]["pid"])
        self.assertIsNotNone(self.processes[0].wait(timeout=5))
        status, _, health = self.request(other_info, "GET", "/api/health")
        self.assertEqual(status, 200, health)
        self.assertEqual(ledger.get(rid)["port"], owned_port)
        self.assertEqual((directory / ".agent-binding.json").read_bytes(), binding_before)
        shown = self.cli("notes.py", "show", str(directory), "--json")
        self.assertEqual(shown.returncode, 0, shown.stdout + shown.stderr)
        self.assertEqual([n["text"] for n in json.loads(shown.stdout)["notes"]], ["Synthetic annotation"])

        queried = self.cli("serve.py", "stop", rid, "--status")
        repeated = self.cli("serve.py", "stop", rid)
        self.assertEqual(queried.returncode, 0, queried.stdout + queried.stderr)
        self.assertEqual(repeated.returncode, 0, repeated.stdout + repeated.stderr)
        self.assertEqual(json.loads(queried.stdout)["state"], "stopped")
        self.assertEqual(json.loads(repeated.stdout)["stopId"], result["stopId"])
        state_path = directory / ".annotation-stop.json"
        self.assertEqual(state_path.stat().st_mode & 0o777, 0o600)
        self.assertNotIn(rid, ledger.load()["reviews"].get("stop", {}))

    def test_stop_exits_waiter_and_only_explicit_start_reopens_the_review(self):
        rid = "3c" * 8
        directory = self.review("waiting", rid)
        bound = self.cli("wakeup.py", "bind", str(directory), "--adapter", "wait",
                         "--harness", "Claude Code", "--session", "shared-session")
        self.assertEqual(bound.returncode, 0, bound.stdout + bound.stderr)
        info, _ = self.start(directory)
        waiter_env = {**self.env, "AE_HARNESS": "Claude Code", "AE_SESSION_ID": "shared-session"}
        waiter = subprocess.Popen([sys.executable, str(SCRIPTS / "notes.py"), "wait", str(directory),
                                   "--timeout", "30", "--interval", ".05"],
                                  env=waiter_env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        self.processes.append(waiter)
        marker = ledger.wait_file(directory, waiter.pid)
        deadline = time.monotonic() + 5
        while not marker.exists() and time.monotonic() < deadline:
            time.sleep(.03)
        self.assertTrue(marker.exists(), "waiter did not publish its marker")

        stopped = self.cli("serve.py", "stop", rid, "--timeout", "3")

        self.assertEqual(stopped.returncode, 0, stopped.stdout + stopped.stderr)
        stdout, stderr = waiter.communicate(timeout=5)
        self.assertEqual(waiter.returncode, 4, stdout + stderr)
        self.assertFalse(marker.exists())
        rewait = subprocess.run([sys.executable, str(SCRIPTS / "notes.py"), "wait", str(directory),
                                 "--timeout", ".2", "--interval", ".05"],
                                env=waiter_env, capture_output=True, text=True, timeout=5)
        self.assertEqual(rewait.returncode, 4, rewait.stdout + rewait.stderr)
        self.assertFalse(marker.exists())

        restarted_info, restarted_health = self.start(directory)
        state = self.cli("serve.py", "stop", rid, "--status")
        self.assertEqual(json.loads(state.stdout)["state"], "active")
        status, _, submitted = self.request(restarted_info, "POST", "/api/handoff",
                                            {"message": "after explicit start",
                                             "receiverBindingId": restarted_health["receiver"]["bindingId"]}, rid)
        self.assertEqual(status, 200, submitted)
        self.assertEqual(submitted["handoff"]["latest"]["status"], "pending")
        final_stop = self.cli("serve.py", "stop", rid, "--timeout", "3")
        self.assertEqual(final_stop.returncode, 0, final_stop.stdout + final_stop.stderr)

    def test_event_stop_cancels_only_this_reviews_turn_and_requires_confirmation(self):
        desktop = Desktop(self.root / "desktop.sock")
        self.addCleanup(desktop.close)
        target_rid, other_rid = "4d" * 8, "5e" * 8
        target, other = self.review("event-target", target_rid), self.review("event-other", other_rid)
        for directory in (target, other):
            bound = self.cli("wakeup.py", "bind", str(directory), "--adapter", "codex-desktop",
                             "--thread", THREAD, "--socket", str(desktop.path))
            self.assertEqual(bound.returncode, 0, bound.stdout + bound.stderr)
        target_info, target_health = self.start(target)
        other_info, other_health = self.start(other)
        for rid, info, health in ((target_rid, target_info, target_health),
                                  (other_rid, other_info, other_health)):
            status, _, submitted = self.request(info, "POST", "/api/handoff",
                                                {"message": "synthetic event work",
                                                 "receiverBindingId": health["receiver"]["bindingId"]}, rid)
            self.assertEqual(status, 200, submitted)
        deadline = time.monotonic() + 5
        while len([r for r in desktop.requests if r["method"] == "thread-follower-start-turn"]) < 2 \
                and time.monotonic() < deadline:
            time.sleep(.03)
        starts = [r for r in desktop.requests if r["method"] == "thread-follower-start-turn"]
        self.assertEqual(len(starts), 2)

        stopped = self.cli("serve.py", "stop", target_rid, "--timeout", "3")

        self.assertEqual(stopped.returncode, 0, stopped.stdout + stopped.stderr)
        result = json.loads(stopped.stdout)
        self.assertEqual(result["state"], "stopped")
        self.assertTrue(result["components"]["agent"]["confirmed"])
        self.assertEqual(len(desktop.cancelled), 1)
        cancellation = desktop.cancelled[0]
        self.assertEqual(cancellation["conversationId"], THREAD)
        self.assertEqual(cancellation["reviewId"], target_rid)
        self.assertEqual(cancellation["bindingId"], target_health["receiver"]["bindingId"])
        self.assertNotEqual(cancellation["bindingId"], other_health["receiver"]["bindingId"])
        self.assertTrue(cancellation["turnId"].startswith("turn-"))
        status, _, health = self.request(other_info, "GET", "/api/health")
        self.assertEqual(status, 200, health)
        repeated = self.cli("serve.py", "stop", target_rid)
        self.assertEqual(repeated.returncode, 0, repeated.stdout + repeated.stderr)
        self.assertEqual(len(desktop.cancelled), 1, "an already confirmed turn must not be cancelled again")

        restarted_info, restarted_health = self.start(target)
        self.assertEqual(restarted_health["receiver"]["bindingId"], target_health["receiver"]["bindingId"])
        deadline = time.monotonic() + 5
        while len([r for r in desktop.requests if r["method"] == "thread-follower-start-turn"]) < 3 \
                and time.monotonic() < deadline:
            time.sleep(.03)
        starts = [r for r in desktop.requests if r["method"] == "thread-follower-start-turn"]
        self.assertEqual(len(starts), 3, "explicit start should recover a cancelled, still unclaimed delivery")
        self.assertNotEqual(starts[0]["params"]["turnStart"]["request"]["clientUserMessageId"],
                            starts[-1]["params"]["turnStart"]["request"]["clientUserMessageId"])
        final_stop = self.cli("serve.py", "stop", target_rid, "--timeout", "2")
        self.assertEqual(final_stop.returncode, 0, final_stop.stdout + final_stop.stderr)
        self.assertEqual(len(desktop.cancelled), 2)

    def test_rejected_cancel_is_incomplete_blocks_new_work_and_retries_same_stop(self):
        desktop = Desktop(self.root / "desktop.sock")
        self.addCleanup(desktop.close)
        rid = "6f" * 8
        directory = self.review("reject-cancel", rid)
        bound = self.cli("wakeup.py", "bind", str(directory), "--adapter", "codex-desktop",
                         "--thread", THREAD, "--socket", str(desktop.path))
        self.assertEqual(bound.returncode, 0, bound.stdout + bound.stderr)
        info, health = self.start(directory)
        status, _, submitted = self.request(info, "POST", "/api/handoff",
                                            {"message": "work to reject cancellation",
                                             "receiverBindingId": health["receiver"]["bindingId"]}, rid)
        self.assertEqual(status, 200, submitted)
        deadline = time.monotonic() + 5
        while not any(r["method"] == "thread-follower-start-turn" for r in desktop.requests) \
                and time.monotonic() < deadline:
            time.sleep(.03)
        self.assertTrue(any(r["method"] == "thread-follower-start-turn" for r in desktop.requests))
        desktop.cancel_mode = "reject"

        first = self.cli("serve.py", "stop", rid, "--timeout", "2")

        self.assertEqual(first.returncode, 1, first.stdout + first.stderr)
        initial = json.loads(first.stdout)
        self.assertEqual(initial["state"], "incomplete")
        self.assertEqual(initial["components"]["agent"]["failures"][0]["code"], "agent-cancel-rejected")
        status, _, current_health = self.request(info, "GET", "/api/health")
        self.assertEqual(status, 200, current_health)
        status, _, rejected = self.request(info, "POST", "/api/handoff",
                                           {"message": "must not start during stop",
                                            "receiverBindingId": health["receiver"]["bindingId"]}, rid)
        self.assertEqual(status, 409, rejected)
        self.assertEqual(rejected["code"], "review-stopped")
        _, _, batches = self.request(info, "GET", "/api/handoff")
        self.assertEqual(batches["total"], 1)
        queried = self.cli("serve.py", "stop", rid, "--status")
        self.assertEqual(queried.returncode, 0, queried.stdout + queried.stderr)
        self.assertEqual(json.loads(queried.stdout)["state"], "incomplete")

        desktop.cancel_mode = "confirm"
        retried = self.cli("serve.py", "stop", rid, "--timeout", "2")
        self.assertEqual(retried.returncode, 0, retried.stdout + retried.stderr)
        final = json.loads(retried.stdout)
        self.assertEqual(final["state"], "stopped")
        self.assertEqual(final["stopId"], initial["stopId"])
        self.assertEqual(batches["latest"]["status"], "pending")
        self.assertEqual(len(desktop.cancelled), 2)

    def test_accept_only_and_timeout_are_capability_gaps_not_stop_confirmations(self):
        for rid, mode, expected in (("7a" * 8, "accept", "agent-cancel-confirmation-required"),
                                    ("8a" * 8, "timeout", "agent-cancel-timeout")):
            with self.subTest(mode=mode):
                desktop = Desktop(self.root / ("desktop-" + mode + ".sock"))
                self.addCleanup(desktop.close)
                directory = self.review("cancel-" + mode, rid)
                bound = self.cli("wakeup.py", "bind", str(directory), "--adapter", "codex-desktop",
                                 "--thread", THREAD, "--socket", str(desktop.path))
                self.assertEqual(bound.returncode, 0, bound.stdout + bound.stderr)
                info, health = self.start(directory)
                status, _, submitted = self.request(info, "POST", "/api/handoff",
                                                    {"message": "cancellation " + mode,
                                                     "receiverBindingId": health["receiver"]["bindingId"]}, rid)
                self.assertEqual(status, 200, submitted)
                deadline = time.monotonic() + 5
                while not any(r["method"] == "thread-follower-start-turn" for r in desktop.requests) \
                        and time.monotonic() < deadline:
                    time.sleep(.03)
                desktop.cancel_mode = mode

                stopped = self.cli("serve.py", "stop", rid, "--timeout", ".2")

                self.assertEqual(stopped.returncode, 1, stopped.stdout + stopped.stderr)
                result = json.loads(stopped.stdout)
                self.assertEqual(result["state"], "incomplete")
                self.assertEqual(result["components"]["agent"]["failures"][0]["code"], expected)
                self.assertIn("capabilityGap", result["components"]["agent"])
                status, _, _ = self.request(info, "GET", "/api/health")
                self.assertEqual(status, 200)
                desktop.cancel_mode = "confirm"
                if mode == "timeout":
                    time.sleep(.6)
                retried = self.cli("serve.py", "stop", rid, "--timeout", "2")
                self.assertEqual(retried.returncode, 0, retried.stdout + retried.stderr)
                self.assertEqual(json.loads(retried.stdout)["stopId"], result["stopId"])

    def test_processing_work_survives_interruption_and_requires_explicit_completion(self):
        rid = "9b" * 8
        directory = self.review("processing", rid)
        bound = self.cli("wakeup.py", "bind", str(directory), "--adapter", "wait",
                         "--harness", "Claude Code", "--session", "shared-session")
        self.assertEqual(bound.returncode, 0, bound.stdout + bound.stderr)
        info, health = self.start(directory)
        status, _, submitted = self.request(info, "POST", "/api/handoff",
                                            {"message": "claimed work",
                                             "receiverBindingId": health["receiver"]["bindingId"]}, rid)
        self.assertEqual(status, 200, submitted)
        owner_env = {**self.env, "AE_HARNESS": "Claude Code", "AE_SESSION_ID": "shared-session"}
        claimed = subprocess.run([sys.executable, str(SCRIPTS / "notes.py"), "claim", str(directory)],
                                 env=owner_env, capture_output=True, text=True, timeout=10)
        self.assertEqual(claimed.returncode, 0, claimed.stdout + claimed.stderr)

        first = self.cli("serve.py", "stop", rid, "--timeout", "1")
        self.assertEqual(first.returncode, 1, first.stdout + first.stderr)
        initial = json.loads(first.stdout)
        self.assertEqual(initial["components"]["agent"]["error"], "agent-cancellation-unsupported")
        self.assertIn("capabilityGap", initial["components"]["agent"])
        status, _, _ = self.request(info, "GET", "/api/health")
        self.assertEqual(status, 200)
        server_process = next(process for process in self.processes if process.pid == info["pid"])
        server_process.terminate()
        server_process.wait(timeout=5)

        resumed = self.cli("serve.py", "stop", rid, "--timeout", "1")
        self.assertEqual(resumed.returncode, 1, resumed.stdout + resumed.stderr)
        recovered = json.loads(resumed.stdout)
        self.assertEqual(recovered["stopId"], initial["stopId"])
        self.assertEqual(recovered["components"]["http"]["state"], "stopped")
        self.assertEqual(recovered["components"]["agent"]["state"], "incomplete")
        _, restarted_health = self.start(directory)
        self.assertEqual(restarted_health["receiver"]["bindingId"], health["receiver"]["bindingId"])
        self.assertEqual(self.cli("notes.py", "batches", str(directory), "--json").returncode, 0)
        batches = json.loads(self.cli("notes.py", "batches", str(directory), "--json").stdout)
        self.assertEqual(batches["latest"]["status"], "claimed")
        self.assertEqual(json.loads(self.cli("serve.py", "stop", rid, "--status").stdout)["state"], "active")
        done = subprocess.run([sys.executable, str(SCRIPTS / "notes.py"), "done", str(directory),
                               "--seq", "1"], env=owner_env, capture_output=True, text=True, timeout=10)
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        final = self.cli("serve.py", "stop", rid, "--timeout", "2")
        self.assertEqual(final.returncode, 0, final.stdout + final.stderr)

    def test_stop_waits_for_the_review_write_lock_before_http_shutdown(self):
        rid = "ab" * 8
        directory = self.review("save-protection", rid)
        info, _ = self.start(directory)
        with fsutil.flocked(directory / ".notes.lock", exclusive=True):
            stopped = self.cli("serve.py", "stop", rid, "--timeout", ".1")
            self.assertEqual(stopped.returncode, 1, stopped.stdout + stopped.stderr)
            result = json.loads(stopped.stdout)
            self.assertEqual(result["state"], "incomplete")
            self.assertEqual(result["components"]["save"]["error"], "save-protection-timeout")
            status, _, _ = self.request(info, "GET", "/api/health")
            self.assertEqual(status, 200)
        retried = self.cli("serve.py", "stop", rid, "--timeout", "2")
        self.assertEqual(retried.returncode, 0, retried.stdout + retried.stderr)
        self.assertEqual(json.loads(retried.stdout)["stopId"], result["stopId"])

    def test_unresponsive_live_http_process_is_not_reported_stopped(self):
        rid = "cd" * 8
        directory = self.review("unresponsive", rid)
        info, _ = self.start(directory)
        os.kill(info["pid"], signal.SIGSTOP)
        try:
            stopped = self.cli("serve.py", "stop", rid, "--timeout", ".1")
            self.assertEqual(stopped.returncode, 1, stopped.stdout + stopped.stderr)
            result = json.loads(stopped.stdout)
            self.assertEqual(result["state"], "incomplete")
            self.assertEqual(result["components"]["http"]["error"], "http-status-unconfirmed")
            self.assertIsNone(next(process for process in self.processes
                                   if process.pid == info["pid"]).poll())
        finally:
            os.kill(info["pid"], signal.SIGCONT)
        retried = self.cli("serve.py", "stop", rid, "--timeout", "2")
        self.assertEqual(retried.returncode, 0, retried.stdout + retried.stderr)
        self.assertEqual(json.loads(retried.stdout)["stopId"], result["stopId"])

    def test_stop_rejects_ambiguous_unknown_and_invalid_timeout_without_creating_state(self):
        first_rid = "abcd" + "0" * 12
        second_rid = "abcd" + "1" * 12
        first = self.review("ambiguous-first", first_rid)
        self.review("ambiguous-second", second_rid)
        for target in ("a", "dead", "abcd"):
            result = self.cli("serve.py", "stop", target)
            self.assertEqual(result.returncode, 2, target + result.stdout + result.stderr)
        invalid_timeout = self.cli("serve.py", "stop", first_rid, "--timeout", "-1")
        self.assertEqual(invalid_timeout.returncode, 2, invalid_timeout.stdout + invalid_timeout.stderr)
        self.assertFalse((first / ".annotation-stop.json").exists())
        stopped = self.cli("serve.py", "stop", first_rid)
        self.assertEqual(stopped.returncode, 0, stopped.stdout + stopped.stderr)
        queried = self.cli("serve.py", "stop-status", first_rid)
        self.assertEqual(queried.returncode, 0, queried.stdout + queried.stderr)
        self.assertEqual(json.loads(queried.stdout)["state"], "stopped")

    def test_legacy_service_returns_upgrade_required_without_stopping_http(self):
        rid = "de" * 8
        directory = self.review("legacy-stop", rid)
        program = (
            "import sys;from pathlib import Path;sys.path.insert(0,sys.argv[1]);"
            "import serve;serve.FEATURES=[f for f in serve.FEATURES if f!='stop-review'];"
            "server=serve.ReviewServer(Path(sys.argv[2]),0,log_to_file=False);"
            "serve.write_status(server);server.serve_forever()"
        )
        process = subprocess.Popen([sys.executable, "-c", program, str(SCRIPTS), str(directory)],
                                   env=self.env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.processes.append(process)
        deadline = time.monotonic() + 5
        health = None
        while time.monotonic() < deadline:
            info = serve.read_status(directory)
            if info and info.get("pid") == process.pid:
                health = serve.probe(info["port"], timeout=.2)
                if health and health.get("reviewId") == rid:
                    break
            time.sleep(.03)
        self.assertIsNotNone(health)
        self.assertNotIn("stop-review", health["features"])

        stopped = self.cli("serve.py", "stop", rid, "--timeout", ".2")

        self.assertEqual(stopped.returncode, 1, stopped.stdout + stopped.stderr)
        result = json.loads(stopped.stdout)
        self.assertEqual(result["state"], "incomplete")
        self.assertEqual(result["components"]["http"]["error"], "upgrade-required")
        self.assertIsNone(process.poll())
        status, _, current = self.request(info, "GET", "/api/health")
        self.assertEqual(status, 200, current)


if __name__ == "__main__":
    unittest.main()
