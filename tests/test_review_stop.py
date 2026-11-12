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
import analysis_store
import fsutil
import ledger
import serve
import wakeup

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
        self.cancel_hold = None
        self.rejected_turns = set()
        self.start_hold = None
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
                        if self.start_hold is not None:
                            self.start_hold.wait(3)
                        message_id = request["params"]["turnStart"]["request"]["clientUserMessageId"]
                        turn_id = "turn-" + message_id
                        self.turns[message_id] = turn_id
                        result = {"result": {"turn": {"id": turn_id, "status": "inProgress"}}}
                    elif method == "thread-follower-cancel-turn":
                        turn_id = request["params"]["turnId"]
                        self.cancelled.append(request["params"])
                        if self.cancel_hold is not None:
                            self.cancel_hold.wait(3)
                        if self.cancel_mode == "accept":
                            result = {"accepted": True}
                        elif self.cancel_mode == "timeout":
                            time.sleep(.5)
                            continue
                        else:
                            result = {"result": {"turn": {"id": turn_id, "status": "cancelled"}}}
                    rejected = method == "thread-follower-cancel-turn" and (
                        self.cancel_mode == "reject" or request["params"]["turnId"] in self.rejected_turns)
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
        self.temp = tempfile.TemporaryDirectory(prefix="ae-stop-")
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

    def start_server(self, directory, on_handoff=None, *, program=None, args=()):
        command = ([sys.executable, "-c", program, str(SCRIPTS), str(directory), *map(str, args)]
                   if program else [sys.executable, str(SCRIPTS / "serve.py"), str(directory), "--no-open"])
        if on_handoff:
            command += ["--on-handoff", on_handoff]
        process = subprocess.Popen(command, env=self.env, stdin=subprocess.DEVNULL,
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

    def wait_until(self, predicate, timeout=5):
        deadline = time.monotonic() + timeout
        while not predicate() and time.monotonic() < deadline:
            time.sleep(.02)
        return predicate()

    def bind_desktop(self, directory, desktop):
        result = self.cli("wakeup.py", "bind", str(directory), "--adapter", "codex-desktop",
                          "--thread", THREAD, "--socket", str(desktop.path))
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def deliver_analysis(self, directory, focus):
        _, seq = analysis_store.start_question(directory, focus, by="fixture")
        return wakeup.deliver(directory, "analysis", seq)

    def test_cli_stop_without_background_work_preserves_review_and_other_service(self):
        rid, other_rid = "1a" * 8, "2b" * 8
        directory, other = self.review("target", rid), self.review("other", other_rid)
        bound = self.cli("wakeup.py", "bind", str(directory), "--adapter", "wait",
                         "--harness", "Claude Code", "--session", "shared-session")
        self.assertEqual(bound.returncode, 0, bound.stdout + bound.stderr)
        binding_before = (directory / ".agent-binding.json").read_bytes()
        info, _ = self.start_server(directory)
        other_info, _ = self.start_server(other)
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
        info, _ = self.start_server(directory)
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

        restarted_info, restarted_health = self.start_server(directory)
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
        target_info, target_health = self.start_server(target)
        other_info, other_health = self.start_server(other)
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

        restarted_info, restarted_health = self.start_server(target)
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
        info, health = self.start_server(directory)
        status, _, created = self.request(info, "POST", "/api/notes",
                                          {"page": 1, "text": "keep this saved note"}, rid)
        self.assertEqual(status, 201, created)
        note = created["note"]
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
        note_path = "/api/notes/" + note["id"]
        for method, path, body in (
            ("POST", "/api/notes", {"page": 1, "text": "late save"}),
            ("POST", "/api/notes/import", {"notes": [{"page": 1, "text": "late import"}]}),
            ("PATCH", note_path, {"text": "late edit"}),
            ("DELETE", note_path, {}),
        ):
            status, _, late_note = self.request(info, method, path, body, rid)
            self.assertEqual((status, late_note.get("code")), (409, "review-stopped"), late_note)
        self.assertEqual(self.request(info, "GET", "/api/notes")[2]["notes"], [note])
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

    def test_concurrent_stops_share_a_single_cancellation(self):
        desktop = Desktop(self.root / "desktop-concurrent.sock")
        self.addCleanup(desktop.close)
        desktop.cancel_hold = threading.Event()
        rid = "71" * 8
        directory = self.review("concurrent", rid)
        self.bind_desktop(directory, desktop)
        self.deliver_analysis(directory, "Synthetic pending turn")
        command = [sys.executable, str(SCRIPTS / "serve.py"), "stop", rid, "--timeout", "3"]
        first = subprocess.Popen(command, env=self.env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        self.processes.append(first)
        try:
            self.assertTrue(self.wait_until(lambda: len(desktop.cancelled) == 1))
            state_file = directory / ".annotation-stop.json"
            second = subprocess.Popen(command, env=self.env, stdout=subprocess.PIPE,
                                      stderr=subprocess.PIPE, text=True)
            self.processes.append(second)
            # The original race starts the second attempt before the first confirmation.
            self.wait_until(lambda: json.loads(state_file.read_text())["attempts"] > 1, timeout=.6)
        finally:
            desktop.cancel_hold.set()
        results = []
        for process in (first, second):
            stdout, stderr = process.communicate(timeout=5)
            self.assertEqual(process.returncode, 0, stdout + stderr)
            results.append(json.loads(stdout))
        self.assertEqual(results[0]["stopId"], results[1]["stopId"])
        self.assertEqual(len(desktop.cancelled), 1)

    def test_transfer_waits_for_confirmation_of_unclaimed_desktop_turn(self):
        desktop = Desktop(self.root / "desktop-transfer.sock")
        self.addCleanup(desktop.close)
        rid = "72" * 8
        directory = self.review("transfer", rid)
        self.bind_desktop(directory, desktop)
        delivered = self.deliver_analysis(directory, "Accepted, still unclaimed")
        command = ("wakeup.py", "bind", str(directory), "--adapter", "wait", "--replace",
                   "--harness", "Claude Code", "--session", "replacement")

        early = self.cli(*command)
        self.assertEqual(early.returncode, 2, early.stdout + early.stderr)
        self.assertIn("delivery-in-progress", early.stderr)
        self.assertEqual(self.cli("wakeup.py", "unbind", str(directory)).returncode, 2)
        stopped = self.cli("serve.py", "stop", rid)
        self.assertEqual(stopped.returncode, 0, stopped.stdout + stopped.stderr)
        self.assertEqual([item["turnId"] for item in desktop.cancelled], [delivered["turnId"]])
        replaced = self.cli(*command)
        self.assertEqual(replaced.returncode, 0, replaced.stdout + replaced.stderr)

    def test_explicit_reuse_recovers_a_cancelled_unclaimed_turn_after_partial_stop(self):
        desktop = Desktop(self.root / "desktop-reuse.sock")
        self.addCleanup(desktop.close)
        rid = "73" * 8
        directory = self.review("reuse-delivery", rid)
        self.bind_desktop(directory, desktop)
        first = self.deliver_analysis(directory, "Cancel this turn")
        second = self.deliver_analysis(directory, "Reject cancellation of this turn")
        desktop.rejected_turns.add(second["turnId"])
        info, _ = self.start_server(directory)
        stopped = self.cli("serve.py", "stop", rid)
        self.assertEqual(stopped.returncode, 1, stopped.stdout + stopped.stderr)
        self.assertEqual(json.loads(stopped.stdout)["components"]["agent"]["confirmedTurns"], [first["turnId"]])

        reused = self.cli("serve.py", str(directory), "--no-open")
        self.assertEqual(reused.returncode, 0, reused.stdout + reused.stderr)
        self.assertEqual(serve.read_status(directory)["pid"], info["pid"])
        self.assertTrue(self.wait_until(lambda: len(desktop.turns) == 3),
                        "reusing the service should redeliver the cancelled turn")

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
                info, health = self.start_server(directory)
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
        info, health = self.start_server(directory)
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
        _, restarted_health = self.start_server(directory)
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
        info, _ = self.start_server(directory)
        with fsutil.flocked(directory / ".notes.lock", exclusive=True):
            stopped = self.cli("serve.py", "stop", rid, "--timeout", ".1")
            self.assertEqual(stopped.returncode, 1, stopped.stdout + stopped.stderr)
            result = json.loads(stopped.stdout)
            self.assertEqual(result["state"], "incomplete")
            self.assertEqual(result["components"]["save"]["error"], "save-protection-timeout")
            status, _, _ = self.request(info, "GET", "/api/health")
            self.assertEqual(status, 200)
        status, _, rejected = self.request(info, "POST", "/api/notes",
                                           {"page": 1, "text": "late save"}, rid)
        self.assertEqual((status, rejected.get("code")), (409, "review-stopped"), rejected)
        retried = self.cli("serve.py", "stop", rid, "--timeout", "2")
        self.assertEqual(retried.returncode, 0, retried.stdout + retried.stderr)
        self.assertEqual(json.loads(retried.stdout)["stopId"], result["stopId"])

    def test_unresponsive_live_http_process_is_not_reported_stopped(self):
        rid = "cd" * 8
        directory = self.review("unresponsive", rid)
        info, _ = self.start_server(directory)
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

    def test_explicit_start_reopens_incomplete_review_even_when_reusing_service(self):
        for index, mode in enumerate(("cli", "overview")):
            with self.subTest(mode=mode):
                rid = ("e" + str(index)) * 8
                directory = self.review("restart-" + mode, rid)
                info, _ = self.start_server(directory)
                with fsutil.flocked(directory / ".notes.lock", exclusive=True):
                    stopped = self.cli("serve.py", "stop", rid, "--timeout", ".1")
                    self.assertEqual(stopped.returncode, 1, stopped.stdout + stopped.stderr)
                self.assertEqual(self.request(info, "POST", "/api/server/resume", {})[0], 409)
                port = info["port"]
                headers = {"Host": f"127.0.0.1:{port}", "X-AE-Review": rid, "Content-Type": "application/json"}
                self.assertEqual(serve._req(port, "POST", "/api/server/resume", headers, {})[0], 403)
                headers.update(Origin=f"http://127.0.0.1:{port}")
                headers["Content-Type"] = "text/plain"
                self.assertEqual(serve._req(port, "POST", "/api/server/resume", headers, {})[0], 415)
                self.assertEqual(json.loads(self.cli("serve.py", "stop", rid, "--status").stdout)["state"],
                                 "incomplete")
                if mode == "cli":
                    started = self.cli("serve.py", str(directory), "--no-open")
                    self.assertEqual(started.returncode, 0, started.stdout + started.stderr)
                else:
                    host_rid = "f0" * 8
                    host = self.review("overview-host", host_rid)
                    host_info, _ = self.start_server(host)
                    status, _, started = self.request(host_info, "POST", "/api/overview/start",
                                                      {"reviewId": rid}, host_rid)
                    self.assertEqual(status, 200, started)
                state = json.loads(self.cli("serve.py", "stop", rid, "--status").stdout)
                self.assertEqual(state["state"], "active")
                status, _, submitted = self.request(info, "POST", "/api/handoff",
                                                    {"message": "explicit reuse start"}, rid)
                self.assertEqual(status, 200, submitted)
                final = self.cli("serve.py", "stop", rid, "--timeout", "2")
                self.assertEqual(final.returncode, 0, final.stdout + final.stderr)

    def test_http_is_not_confirmed_stopped_until_server_close_finishes(self):
        rid = "a0" * 8
        directory = self.review("slow-close", rid)
        closing, release = self.root / "closing", self.root / "release-close"
        program = (
            "import sys,time;from pathlib import Path;sys.path.insert(0,sys.argv[1]);"
            "import serve\n"
            "class SlowClose(serve.ReviewServer):\n"
            "    def server_close(self):\n"
            "        Path(sys.argv[2]).write_text('closing')\n"
            "        deadline=time.monotonic()+5\n"
            "        while not Path(sys.argv[3]).exists() and time.monotonic()<deadline: time.sleep(.02)\n"
            "        super().server_close()\n"
            "serve.ReviewServer=SlowClose\n"
            "sys.exit(serve.serve(Path(sys.argv[4]),None,False,False))\n"
        )
        process = subprocess.Popen([sys.executable, "-c", program, str(SCRIPTS), str(closing),
                                    str(release), str(directory)], env=self.env,
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.processes.append(process)
        info = None
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            candidate = serve.read_status(directory)
            if candidate and candidate.get("pid") == process.pid and serve.probe(candidate["port"], timeout=.2):
                info = candidate
                break
            time.sleep(.03)
        self.assertIsNotNone(info)
        stopper = subprocess.Popen([sys.executable, str(SCRIPTS / "serve.py"), "stop", rid, "--timeout", "3"],
                                   env=self.env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        deadline = time.monotonic() + 5
        while not closing.exists() and time.monotonic() < deadline:
            time.sleep(.03)
        self.assertTrue(closing.exists(), "server did not enter server_close")
        try:
            state = json.loads(self.cli("serve.py", "stop", rid, "--status").stdout)
            self.assertNotEqual(state["state"], "stopped")
            self.assertIsNone(process.poll())
            with socket.socket() as probe:
                probe.settimeout(.2)
                probe.connect(("127.0.0.1", info["port"]))
        finally:
            release.write_text("close")
        stdout, stderr = stopper.communicate(timeout=8)
        self.assertEqual(stopper.returncode, 0, stdout + stderr)
        process.wait(timeout=5)
        final = json.loads(self.cli("serve.py", "stop", rid, "--status").stdout)
        self.assertEqual(final["state"], "stopped")

    def test_start_waits_for_shutdown_even_when_the_stop_client_disconnects(self):
        rid = "a1" * 8
        directory = self.review("disconnect", rid)
        replying, release = self.root / "replying", self.root / "release"
        program = """
import sys, time
from pathlib import Path
sys.path.insert(0, sys.argv[1])
import serve

class DisconnectOnStop(serve.Handler):
    def _json(self, status, body):
        if self.path == "/api/server/stop" and body.get("stopping"):
            Path(sys.argv[3]).touch()
            deadline = time.monotonic() + 5
            while not Path(sys.argv[4]).exists() and time.monotonic() < deadline:
                time.sleep(.02)
            raise BrokenPipeError("synthetic disconnected client")
        super()._json(status, body)

serve.Handler = DisconnectOnStop
sys.exit(serve.serve(Path(sys.argv[2]), None, False, False))
"""
        info, _ = self.start_server(directory, program=program, args=(replying, release))
        original = self.processes[-1]
        stopper = subprocess.Popen([sys.executable, str(SCRIPTS / "serve.py"), "stop", rid, "--timeout", "3"],
                                   env=self.env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.processes.append(stopper)
        try:
            self.assertTrue(self.wait_until(replying.exists), "server did not reach the stop response")
            stopping = json.loads(self.cli("serve.py", "stop", rid, "--status").stdout)
            self.assertEqual(stopping["state"], "stopping")
            starter = subprocess.Popen([sys.executable, str(SCRIPTS / "serve.py"), str(directory), "--no-open"],
                                       env=self.env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            self.processes.append(starter)
            self.assertFalse(self.wait_until(lambda: starter.poll() is not None, timeout=.6),
                             "start reused a service with shutdown pending")
        finally:
            release.touch()

        self.assertEqual(original.wait(timeout=5), 0)
        stopper.wait(timeout=5)
        self.assertTrue(self.wait_until(lambda: (serve.probe(info["port"], timeout=.1) or {}).get("pid") == starter.pid))
        restarted = json.loads(self.cli("serve.py", "stop", rid, "--status").stdout)
        self.assertEqual((restarted["state"], restarted["previousStopId"]), ("active", stopping["stopId"]))

    def test_independent_hook_must_exit_before_no_service_stop_can_complete(self):
        rid = "b0" * 8
        directory = self.review("orphan-hook", rid)
        pid_file, release = self.root / "hook.pid", self.root / "release-hook"
        hook = self.root / "hook.py"
        hook.write_text(
            "import os,time\nfrom pathlib import Path\n"
            f"Path({str(pid_file)!r}).write_text(str(os.getpid()))\n"
            "deadline=time.monotonic()+5\n"
            f"while not Path({str(release)!r}).exists() and time.monotonic()<deadline: time.sleep(.02)\n",
            encoding="utf-8")
        info, _ = self.start_server(directory, f"{sys.executable} {hook}")
        status, _, submitted = self.request(info, "POST", "/api/handoff", {"message": "long hook"}, rid)
        self.assertEqual(status, 200, submitted)
        deadline = time.monotonic() + 5
        while not pid_file.exists() and time.monotonic() < deadline:
            time.sleep(.03)
        self.assertTrue(pid_file.exists(), "hook did not start")
        hook_pid = int(pid_file.read_text())
        server_process = next(process for process in self.processes if process.pid == info["pid"])
        server_process.terminate()
        server_process.wait(timeout=5)
        self.assertTrue(ledger.pid_alive(hook_pid))
        try:
            stopped = self.cli("serve.py", "stop", rid, "--timeout", ".1")
            self.assertEqual(stopped.returncode, 1, stopped.stdout + stopped.stderr)
            result = json.loads(stopped.stdout)
            self.assertEqual(result["state"], "incomplete")
            self.assertEqual(result["components"]["hooks"]["error"], "background-hook-running")
            self.assertIn(hook_pid, result["components"]["hooks"]["pids"])
            restarted_info, _ = self.start_server(directory)
            stopped = self.cli("serve.py", "stop", rid, "--timeout", ".1")
            self.assertEqual(stopped.returncode, 1, stopped.stdout + stopped.stderr)
            result = json.loads(stopped.stdout)
            self.assertEqual(result["components"]["hooks"]["error"], "background-hook-running")
            self.assertIn(hook_pid, result["components"]["hooks"]["pids"])
            self.assertEqual(self.request(restarted_info, "GET", "/api/health")[0], 200)
        finally:
            release.write_text("release")
        deadline = time.monotonic() + 6
        final = None
        while time.monotonic() < deadline:
            final = self.cli("serve.py", "stop", rid, "--timeout", ".2")
            if final.returncode == 0:
                break
            time.sleep(.05)
        self.assertIsNotNone(final)
        self.assertEqual(final.returncode, 0, final.stdout + final.stderr)
        self.assertEqual(json.loads(final.stdout)["state"], "stopped")

    def test_zero_timeout_stops_idle_review(self):
        rid = "b1" * 8
        self.review("idle", rid)
        stopped = self.cli("serve.py", "stop", rid, "--timeout", "0")
        self.assertEqual(stopped.returncode, 0, stopped.stdout + stopped.stderr)
        self.assertEqual(json.loads(stopped.stdout)["state"], "stopped")

    def test_stop_times_out_while_a_delivery_owns_the_dispatch_lock(self):
        rid = "b2" * 8
        directory = self.review("dispatch-timeout", rid)
        with wakeup.dispatch_lock(directory):
            stopped = self.cli("serve.py", "stop", rid, "--timeout", ".1", timeout=2)
        self.assertEqual(stopped.returncode, 1, stopped.stdout + stopped.stderr)
        state = json.loads(stopped.stdout)
        self.assertEqual(state["state"], "incomplete")
        self.assertEqual(state["components"]["agent"]["error"], "agent-dispatch-timeout")
        retried = self.cli("serve.py", "stop", rid, "--timeout", "2")
        self.assertEqual(retried.returncode, 0, retried.stdout + retried.stderr)
        self.assertEqual(json.loads(retried.stdout)["stopId"], state["stopId"])

    def test_stopped_review_rejects_analysis_fork_before_committing_a_round(self):
        rid = "b3" * 8
        directory = self.review("analysis-fork", rid)
        bound = self.cli("wakeup.py", "bind", str(directory), "--adapter", "wait",
                         "--harness", "Claude Code", "--session", "owner")
        self.assertEqual(bound.returncode, 0, bound.stdout + bound.stderr)
        state, _ = analysis_store.start_question(directory, "Synthetic focus", by="fixture")
        aid = state["id"]
        checkpoint = analysis_store.seal_checkpoint(directory, aid)["checkpoints"][-1]["id"]
        stopped = self.cli("serve.py", "stop", rid)
        self.assertEqual(stopped.returncode, 0, stopped.stdout + stopped.stderr)
        history_before = analysis_store.load(directory, aid)

        result = self.cli("notes.py", "analysis-line", str(directory), "--analysis", aid,
                          "--action", "fork", "--checkpoint", checkpoint,
                          "--harness", "Claude Code", "--session", "owner")

        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertIn("review-stopped", result.stderr)
        self.assertEqual(analysis_store.load(directory, aid), history_before)

    def test_inflight_delivery_is_settled_and_cancelled_before_stop_completes(self):
        desktop = Desktop(self.root / "desktop-inflight.sock")
        desktop.start_hold = threading.Event()
        self.addCleanup(desktop.close)
        rid = "c0" * 8
        directory = self.review("inflight", rid)
        bound = self.cli("wakeup.py", "bind", str(directory), "--adapter", "codex-desktop",
                         "--thread", THREAD, "--socket", str(desktop.path))
        self.assertEqual(bound.returncode, 0, bound.stdout + bound.stderr)
        _state, round_no = analysis_store.start_question(directory, "inflight delivery", by="synthetic")
        binding = json.loads((directory / ".agent-binding.json").read_text())
        payload = {"app": "annotation-everything", "reviewId": rid, "reviewDir": str(directory),
                   "bindingId": binding["bindingId"], "event": "analysis", "seq": round_no}
        delivery = subprocess.Popen([sys.executable, str(SCRIPTS / "wakeup.py"), "deliver", str(directory)],
                                    env=self.env, stdin=subprocess.PIPE,
                                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        self.processes.append(delivery)
        delivery.stdin.write(json.dumps(payload))
        delivery.stdin.close()
        history_path = directory / ".agent-deliveries.json"
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            history = json.loads(history_path.read_text()) if history_path.exists() else {"items": {}}
            if any(item.get("state") == "sending" for item in history["items"].values()):
                break
            time.sleep(.03)
        self.assertTrue(any(item.get("state") == "sending" for item in history["items"].values()))
        stopper = subprocess.Popen([sys.executable, str(SCRIPTS / "serve.py"), "stop", rid, "--timeout", "3"],
                                   env=self.env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        self.processes.append(stopper)
        try:
            deadline = time.monotonic() + 1
            while stopper.poll() is None and time.monotonic() < deadline:
                time.sleep(.02)
            self.assertIsNone(stopper.poll(), "stop completed while start-turn was still in flight")
        finally:
            desktop.start_hold.set()
        stdout, stderr = stopper.communicate(timeout=8)
        self.assertEqual(stopper.returncode, 0, stdout + stderr)
        self.assertEqual(json.loads(stdout)["state"], "stopped")
        self.assertEqual(len(desktop.cancelled), 1)

    def test_direct_delivery_cannot_start_after_cancellation_snapshot(self):
        desktop = Desktop(self.root / "desktop-race.sock")
        self.addCleanup(desktop.close)
        rid = "d0" * 8
        directory = self.review("delivery-race", rid)
        bound = self.cli("wakeup.py", "bind", str(directory), "--adapter", "codex-desktop",
                         "--thread", THREAD, "--socket", str(desktop.path))
        self.assertEqual(bound.returncode, 0, bound.stdout + bound.stderr)
        _state, round_no = analysis_store.start_question(directory, "delivery race", by="synthetic")
        program = ("import sys;from pathlib import Path;sys.path.insert(0,sys.argv[1]);"
                   "import notes;notes.notify_analysis(Path(sys.argv[2]),"
                   "{'rounds':[{'seq':int(sys.argv[3]),'status':'submitted'}]})")
        with wakeup.dispatch_lock(directory):
            delivery = subprocess.Popen([sys.executable, "-c", program, str(SCRIPTS), str(directory), str(round_no)],
                                        env=self.env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            self.processes.append(delivery)
            time.sleep(.2)
            stopper = subprocess.Popen([sys.executable, str(SCRIPTS / "serve.py"), "stop", rid,
                                        "--timeout", "3"], env=self.env,
                                       stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            self.processes.append(stopper)
            deadline = time.monotonic() + 1
            while stopper.poll() is None and time.monotonic() < deadline:
                time.sleep(.02)
            self.assertIsNone(stopper.poll(), "stop completed before the admitted delivery was settled")
        delivery_out, delivery_err = delivery.communicate(timeout=8)
        stdout, stderr = stopper.communicate(timeout=8)
        self.assertIn(delivery.returncode, (0, 1, 2), delivery_out + delivery_err)
        if delivery.returncode != 0:
            self.assertIn("review-stopped", delivery_err)
        self.assertEqual(stopper.returncode, 0, stdout + stderr)
        self.assertEqual(json.loads(stdout)["state"], "stopped")
        starts = [request for request in desktop.requests if request["method"] == "thread-follower-start-turn"]
        self.assertEqual(len(desktop.cancelled), len(starts),
                         "every direct delivery admitted before cancellation must be confirmed stopped")


if __name__ == "__main__":
    unittest.main()
