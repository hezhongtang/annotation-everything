"""Synthetic HTTP -> persisted binding -> desktop IPC, including service restart."""
from __future__ import annotations

import contextlib
import json
import os
from pathlib import Path
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
import serve
import notes_store as store
import wakeup
import notes as notes_cli
sys.path.insert(0, str(ROOT))
import host_isolation
host_isolation.require_for_module(__name__)

THREAD = "11111111-1111-4111-8111-111111111111"


class Desktop:
    def __init__(self, path):
        self.path = path
        self.requests = []
        self.reject = False
        self.drop_reply = False
        self.listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.listener.bind(str(path))
        path.chmod(0o600)
        self.listener.listen()
        self.listener.settimeout(0.1)
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
                def exact(n):
                    data = b""
                    while len(data) < n:
                        part = client.recv(n - len(data))
                        if not part:
                            return None
                        data += part
                    return data
                while True:
                    head = exact(4)
                    if not head:
                        break
                    request = json.loads(exact(struct.unpack("<I", head)[0]))
                    method = request["method"]
                    result = {"clientId": "fixture"} if method == "initialize" else {}
                    if method == "thread-follower-start-turn":
                        self.requests.append(request)
                        if self.drop_reply:
                            break
                        result = {"result": {"turn": {"id": "fixture-turn", "status": "inProgress"}}}
                    reply = {"type": "response", "requestId": request["requestId"],
                             "resultType": "error" if self.reject and method.endswith("start-turn") else "success",
                             "error": "unsupported-protocol", "result": result,
                             "handledByClientId": "owner"}
                    raw = json.dumps(reply).encode()
                    client.sendall(struct.pack("<I", len(raw)) + raw)

    def close(self):
        self.stopped.set()
        self.worker.join(2)
        self.listener.close()


class DeliveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="ae-delivery-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.env = patch.dict(os.environ, {"AE_HOME": str(self.root / "ledger"), "CODEX_THREAD_ID": THREAD})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.review = self.root / "review"
        self.review.mkdir(mode=0o700)
        (self.review / "review.json").write_text(json.dumps({"reviewId": "synthetic-delivery", "reviewDir": str(self.review)}))
        (self.review / "index.html").write_text("<!doctype html><title>Synthetic</title>")
        self.desktop = Desktop(self.root / "desktop.sock")
        self.addCleanup(self.desktop.close)

    def bind(self):
        return wakeup.bind(self.review, THREAD, self.desktop.path)

    def start_server(self):
        server = serve.ReviewServer(self.review, 0, log_to_file=False)
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        def close():
            server.shutdown()
            server.server_close()
            worker.join(2)
            self.assertTrue(server.wait_for_hooks(5), "hook workers must finish before removing the review fixture")
        return server, close

    def submit(self, server):
        headers = {"Host": f"127.0.0.1:{server.port}", "Origin": f"http://127.0.0.1:{server.port}",
                   "Content-Type": "application/json", "X-AE-Review": "synthetic-delivery"}
        _, _, metadata = serve._req(server.port, "GET", "/api/notes", headers)
        receiver = json.loads(metadata)["receiver"]
        status, _, data = serve._req(server.port, "POST", "/api/handoff", headers,
                                     {"message": "synthetic", "allowSubagents": False,
                                      "receiverBindingId": receiver.get("bindingId")})
        self.assertEqual(status, 200)
        return json.loads(data)["seq"]

    def wait_status(self, state):
        deadline = time.monotonic() + 4
        while time.monotonic() < deadline:
            result = wakeup.status(self.review)
            if result.get("state") == state:
                return result
            time.sleep(.02)
        self.fail(wakeup.status(self.review))

    def test_binding_survives_plain_service_restart_and_ack_is_separate(self):
        binding = self.bind()
        first, close = self.start_server()
        try:
            self.assertEqual(self.submit(first), 1)
            sent = self.wait_status("accepted")
            self.assertEqual(store.handoff_state(self.review)["latest"]["status"], "pending")
            wakeup.acknowledge(self.review, "handoff", 1, sent["messageId"], THREAD)
            self.assertEqual(wakeup.status(self.review)["state"], "received")
            store.handoff_claim(self.review, by="Synthetic")
            row = serve.review_row("synthetic-delivery", {"dir": str(self.review), "port": None})
            self.assertTrue(row["receiptConfirmed"])
            self.assertFalse(row["unattended"])
        finally:
            close()
        second, close = self.start_server()
        try:
            self.assertEqual(self.submit(second), 2)
            self.wait_status("accepted")
            self.assertEqual(len(self.desktop.requests), 2)
            self.assertEqual(wakeup.load_binding(self.review)["bindingId"], binding["bindingId"])
            self.assertIsNotNone(second.on_handoff)
        finally:
            close()

    def test_duplicate_and_lost_ack_not_replayed(self):
        self.bind()
        store.handoff_submit(self.review, "synthetic")
        self.desktop.drop_reply = True
        with self.assertRaises(Exception):
            wakeup.deliver(self.review, "handoff", 1)
        self.assertEqual(wakeup.status(self.review)["state"], "unconfirmed")
        wakeup.deliver(self.review, "handoff", 1)
        self.assertEqual(len(self.desktop.requests), 1)

    def test_rejected_delivery_can_retry_on_restart_and_never_claims(self):
        self.bind()
        store.handoff_submit(self.review, "synthetic")
        self.desktop.reject = True
        with self.assertRaises(Exception):
            wakeup.deliver(self.review, "handoff", 1)
        self.assertEqual(wakeup.status(self.review)["state"], "failed")
        self.desktop.reject = False
        server, close = self.start_server()
        try:
            server.recover_deliveries()
            self.wait_status("accepted")
            self.assertEqual(store.handoff_state(self.review)["latest"]["status"], "pending")
            server.recover_deliveries()
            time.sleep(.1)
            self.assertEqual(len(self.desktop.requests), 2)
        finally:
            close()

    def test_binding_validation_and_custom_hook_conflict(self):
        self.bind()
        with self.assertRaises(ValueError):
            serve.ReviewServer(self.review, 0, on_handoff=[sys.executable, "-V"])
        with self.assertRaises(ValueError):
            wakeup.acknowledge(self.review, "handoff", 1, "wrong", "22222222-2222-4222-8222-222222222222")
        manifest = self.review / "review.json"
        manifest.write_text(json.dumps({"reviewId": "copied-review"}))
        with self.assertRaises(ValueError):
            wakeup.load_binding(self.review)

    def test_broken_delivery_link_is_an_error_and_is_not_overwritten(self):
        binding = self.bind()
        history = self.review / wakeup.DELIVERY_FILE
        history.symlink_to(self.review / "missing-delivery-history.json")
        key = wakeup._key(binding, "handoff", 1)
        with self.assertRaises((OSError, ValueError)):
            wakeup._record(self.review, key, {"state": "connecting"})
        self.assertTrue(history.is_symlink(), "损坏的历史不能被新回执静默覆盖")
        self.assertEqual(wakeup.public_status(self.review)["state"], "error")

    def test_handoff_and_review_are_different_deliveries(self):
        self.bind()
        store.handoff_submit(self.review, "synthetic", target="review")
        wakeup.deliver(self.review, "handoff", 1)
        wakeup.deliver(self.review, "handoff", 1)
        self.assertEqual(len(self.desktop.requests), 1)
        request = self.desktop.requests[0]["params"]["turnStart"]
        self.assertEqual(request["context"], {"inheritThreadSettings": True})
        self.assertNotIn("sandboxPolicy", request["request"])
        self.assertIn("--seq 1", request["request"]["input"][0]["text"])
        self.assertEqual((self.review / wakeup.BINDING_FILE).stat().st_mode & 0o777, 0o600)
        store.handoff_claim(self.review, by="Synthetic")
        store.handoff_done(self.review, seq=1, by="Synthetic", mode="draft")
        store.handoff_request_review(self.review, 1)
        wakeup.deliver(self.review, "review", 1)
        self.assertEqual(len(self.desktop.requests), 2)
        self.assertNotEqual(self.desktop.requests[0]["params"]["turnStart"]["request"]["clientUserMessageId"],
                            self.desktop.requests[1]["params"]["turnStart"]["request"]["clientUserMessageId"])

    def test_claim_without_chat_receipt_is_unknown_not_offline(self):
        store.handoff_submit(self.review, "synthetic")
        store.handoff_claim(self.review, by="Synthetic")
        row = serve.review_row("synthetic-delivery", {"dir": str(self.review), "port": None})
        self.assertIsNone(row["receiptConfirmed"])
        self.assertEqual(row["agentDelivery"]["state"], "unconfigured")
        self.assertFalse(row["unattended"])
        self.assertEqual(store.handoff_state(self.review)["latest"]["status"], "claimed")

    def test_bound_review_rejects_claiming_waiter_and_wrong_chat(self):
        self.bind()
        store.handoff_submit(self.review, "synthetic")
        self.assertEqual(notes_cli.main(["wait", str(self.review), "--timeout", "1"]), 2)
        with patch.dict(os.environ, {"CODEX_THREAD_ID": "different-chat"}):
            self.assertEqual(notes_cli.main(["claim", str(self.review), "--seq", "1"]), 2)
        self.assertEqual(store.handoff_state(self.review)["latest"]["status"], "pending")

    def test_plain_cli_restart_recovers_unsent_submission(self):
        self.bind()
        store.handoff_submit(self.review, "synthetic")
        process = subprocess.Popen([sys.executable, str(SCRIPTS / "serve.py"), str(self.review), "--no-open"],
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            self.wait_status("accepted")
            state = json.loads((self.review / serve.STATUS_FILE).read_text())
            self.assertTrue(serve.probe(state["port"])["hook"])
        finally:
            process.terminate()
            process.wait(timeout=5)

        process = subprocess.Popen([sys.executable, str(SCRIPTS / "serve.py"), str(self.review), "--no-open"],
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            deadline = time.monotonic() + 3
            state, health = None, None
            while time.monotonic() < deadline:
                state = serve.read_status(self.review)
                if state and state.get("pid") == process.pid:
                    health = serve.probe(state["port"])
                    if health and health.get("hook"):
                        break
                time.sleep(.02)
            self.assertIsNotNone(state, "重启服务须写出完整的状态 JSON")
            self.assertEqual(state["pid"], process.pid)
            self.assertTrue(health and health.get("hook"))
            time.sleep(.1)
            self.assertEqual(len(self.desktop.requests), 1)
        finally:
            process.terminate()
            process.wait(timeout=5)

    def test_wait_binding_reuses_the_same_running_service(self):
        wakeup.bind_wait(self.review, "Claude Code", "session-a")
        process = subprocess.Popen([sys.executable, str(SCRIPTS / "serve.py"), str(self.review), "--no-open"],
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            deadline = time.monotonic() + 4
            while not (self.review / serve.STATUS_FILE).exists() and time.monotonic() < deadline:
                time.sleep(.02)
            reused = subprocess.run([sys.executable, str(SCRIPTS / "serve.py"), str(self.review), "--no-open"],
                                    capture_output=True, text=True, timeout=10)
            self.assertEqual(reused.returncode, 0, reused.stdout + reused.stderr)
            self.assertEqual(json.loads((self.review / serve.STATUS_FILE).read_text())["pid"], process.pid)
        finally:
            process.terminate()
            process.wait(timeout=5)

    def test_registered_event_types_drive_dedup_and_status(self):
        """登记的事件类型参与去重与状态汇总；未登记的类型被拒，也不抢已登记类型的汇总。"""
        self.bind()
        fake = {"awaiting": "pending", "claimStage": "draft"}
        with patch.dict(wakeup.DELIVERY_EVENTS, {"fake": fake}):
            with self.assertRaises(ValueError):   # 未登记的事件类型：去重键拒绝
                wakeup.deliver(self.review, "future-analysis", 1)
            store.handoff_submit(self.review, "synthetic")
            handoff = wakeup.deliver(self.review, "handoff", 1)
            other = wakeup.deliver(self.review, "fake", 1)   # 同 seq 不同事件：不互相去重
            self.assertEqual(len(self.desktop.requests), 2)
            self.assertNotEqual(handoff["messageId"], other["messageId"])
            wakeup.acknowledge(self.review, "handoff", 1, handoff["messageId"], THREAD)
            result = wakeup.status(self.review)
            self.assertEqual(result["events"]["handoff"]["state"], "received")
            self.assertEqual(result["events"]["fake"]["state"], "accepted")
            self.assertEqual((result["event"], result["state"]), ("fake", "accepted"),
                             "同 seq 时登记顺序靠后的类型优先汇总")
            with self.assertRaises(ValueError):   # 确认命令接受已登记类型（过了参数解析才到绑定校验）
                wakeup.main(["ack", str(self.review), "--event", "fake", "--seq", "1", "--message-id", "wrong"])
            with self.assertRaises(SystemExit):   # 未登记类型过不了参数解析
                wakeup.main(["ack", str(self.review), "--event", "future-analysis", "--seq", "1", "--message-id", "x"])
        binding = wakeup.load_binding(self.review)
        history = json.loads((self.review / wakeup.DELIVERY_FILE).read_text())
        history["items"][f'{binding["bindingId"]}:future-analysis:99'] = {   # 将来版本写下的记录，本版本未登记
            "bindingId": binding["bindingId"], "seq": 99, "event": "future-analysis",
            "messageId": "future-version", "state": "received"}
        (self.review / wakeup.DELIVERY_FILE).write_text(json.dumps(history, ensure_ascii=False))
        result = wakeup.status(self.review)
        self.assertEqual((result["event"], result["seq"]), ("handoff", 1), "未登记类型的记录不抢汇总")
        self.assertEqual(list(result["events"]), ["handoff"])   # 解除登记后同样不参与统计

    def test_transfer_checks_consult_every_registered_source(self):
        """改绑 / 解除接收方的前置检查按登记的来源逐个判定；来源可登记多种。"""
        self.bind()
        store.handoff_submit(self.review, "synthetic")
        store.handoff_claim(self.review, by="Synthetic")
        with self.assertRaises(ValueError) as ctx:
            wakeup.assert_transferable(self.review)
        self.assertIn("batch-in-progress", str(ctx.exception))
        store.handoff_done(self.review, seq=1, by="Synthetic")
        wakeup.assert_transferable(self.review)   # 批次不在处理中：通过
        fake = ("fake-in-progress: synthetic source", lambda review: True)
        with patch.object(wakeup, "TRANSFER_BLOCKS", wakeup.TRANSFER_BLOCKS + (fake,)):
            with self.assertRaises(ValueError) as ctx:   # 批次之外登记的来源也能拦下
                wakeup.assert_transferable(self.review)
        self.assertIn("fake-in-progress", str(ctx.exception))
        wakeup.assert_transferable(self.review)   # 解除登记后恢复通过

    def test_old_delivery_event_cannot_route_to_the_replacement_chat(self):
        old = self.bind()
        replacement = "22222222-2222-4222-8222-222222222222"
        wakeup.bind(self.review, replacement, self.desktop.path, replace=True)
        store.handoff_submit(self.review, "Synthetic queued event")
        payload = {"app": "annotation-everything", "reviewId": "synthetic-delivery", "reviewDir": str(self.review),
                   "bindingId": old["bindingId"], "event": "handoff", "seq": 1}
        sent = subprocess.run([sys.executable, str(SCRIPTS / "wakeup.py"), "deliver", str(self.review)],
                              input=json.dumps(payload), capture_output=True, text=True, timeout=10)
        self.assertEqual(sent.returncode, 2, sent.stdout + sent.stderr)
        self.assertEqual(self.desktop.requests, [])


if __name__ == "__main__":
    unittest.main()
