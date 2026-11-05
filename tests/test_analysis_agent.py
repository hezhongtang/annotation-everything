"""Agent 侧契约：analysis 事件的投递、去重、跨队列领取顺序、旧等待进程不领取、命令行与 HTTP 接口形状。"""
from __future__ import annotations

import io
import json
import os
import shlex
import socket
import struct
import subprocess
import sys
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "skill/annotation-everything" / "scripts"
sys.path.insert(0, str(SCRIPTS))
sys.path.insert(0, str(ROOT))

import host_isolation

host_isolation.require_for_module(__name__)

import analysis_store  # noqa: E402
import notes as notes_cli  # noqa: E402
import notes_store as store  # noqa: E402
import serve  # noqa: E402
import wakeup  # noqa: E402

THREAD = "11111111-1111-4111-8111-111111111111"


class Desktop:
    """合成的桌面 IPC：只记录投递进来的提示。"""

    def __init__(self, path):
        self.path = path
        self.requests = []
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
                        result = {"result": {"turn": {"id": "fixture-turn", "status": "inProgress"}}}
                    reply = {"type": "response", "requestId": request["requestId"],
                             "resultType": "success", "result": result, "handledByClientId": "owner"}
                    raw = json.dumps(reply).encode()
                    client.sendall(struct.pack("<I", len(raw)) + raw)

    def close(self):
        self.stopped.set()
        self.worker.join(2)
        self.listener.close()

    def prompt(self, index: int = -1) -> str:
        return self.requests[index]["params"]["turnStart"]["request"]["input"][0]["text"]


def _cli_out(*args):
    buf = io.StringIO()
    with redirect_stdout(buf):
        code = notes_cli.main(list(args))
    assert code == 0, (code, buf.getvalue())
    return buf.getvalue()


def _cli_json_in(argv, payload):
    """跑一条从 stdin 读 JSON 的命令行。"""
    stdin = sys.stdin
    sys.stdin = io.StringIO(json.dumps(payload))
    buf = io.StringIO()
    try:
        with redirect_stdout(buf):
            code = notes_cli.main(list(argv))
    finally:
        sys.stdin = stdin
    assert code == 0, (code, buf.getvalue())
    return buf.getvalue()


class AnalysisAgentTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="ae-analysis-agent-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.env = patch.dict(os.environ, {"AE_HOME": str(self.root / "ledger"), "CODEX_THREAD_ID": THREAD})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.review = self.root / "review"
        self.review.mkdir(mode=0o700)
        (self.review / "review.json").write_text(
            json.dumps({"reviewId": "synthetic-analysis", "reviewDir": str(self.review)}))
        (self.review / "index.html").write_text("<!doctype html><title>Synthetic</title>")
        self.desktop = Desktop(self.root / "desktop.sock")
        self.addCleanup(self.desktop.close)

    def add_note(self, text="第 3 页「增长主要来自新客」站得住吗", page=3):
        _doc, note = store.add(self.review, {"page": page, "text": text})
        return note

    def test_analysis_event_registered_with_dedicated_prompt_and_dedup(self):
        """analysis 是登记的投递事件：专门提示（不改稿、不重建对照页、默认只查原始材料），同轮次不重复投递。"""
        wakeup.bind(self.review, THREAD, self.desktop.path)
        note = self.add_note()
        _state, round_no = analysis_store.start(self.review, note["id"])
        sent = wakeup.deliver(self.review, "analysis", round_no)
        self.assertEqual(sent["state"], "accepted")
        self.assertEqual(len(self.desktop.requests), 1)
        prompt = self.desktop.prompt()
        self.assertIn("analysis", prompt.lower())
        self.assertIn("notes.py analysis", prompt)               # 读一轮上下文的命令
        self.assertIn("analysis-writeback", prompt)              # 写回轮次的命令
        self.assertIn("--events analysis", prompt)               # 领取命令声明接收分析事件
        self.assertIn("source material", prompt)                 # 默认只查审阅的原始材料
        self.assertNotIn("rebuild the same review", prompt)      # 不要求重建对照页
        self.assertNotIn("resolve each annotation", prompt)      # 不要求改稿收尾
        # 同一轮次重复投递被去重键拦下
        again = wakeup.deliver(self.review, "analysis", round_no)
        self.assertEqual(again["state"], "accepted")
        self.assertEqual(len(self.desktop.requests), 1)
        # 轮次已被领取后不再投递
        claimed = analysis_store.claim(self.review, by="Synthetic")
        self.assertEqual(claimed["seq"], round_no)
        skipped = wakeup.deliver(self.review, "analysis", round_no)
        self.assertEqual(skipped["state"], "skipped")

    def test_acknowledge_accepts_analysis_events(self):
        wakeup.bind(self.review, THREAD, self.desktop.path)
        note = self.add_note()
        _state, round_no = analysis_store.start(self.review, note["id"])
        sent = wakeup.deliver(self.review, "analysis", round_no)
        wakeup.acknowledge(self.review, "analysis", round_no, sent["messageId"], THREAD)
        result = wakeup.status(self.review)
        self.assertEqual(result["events"]["analysis"]["state"], "received")

    def test_analysis_claim_requires_the_bound_session(self):
        """分析领取与批次领取共用接收方身份约束。"""
        wakeup.bind_wait(self.review, "Claude Code", "session-a")
        note = self.add_note()
        state, round_no = analysis_store.start(self.review, note["id"])
        command = ["claim", str(self.review), "--events", "analysis",
                   "--harness", "Claude Code", "--session", "session-b"]
        self.assertEqual(notes_cli.main(command), 2)
        self.assertEqual(analysis_store.load(self.review, state["id"])["rounds"][0]["status"], "submitted")
        command[-1] = "session-a"
        claimed = json.loads(_cli_out(*command))
        self.assertEqual((claimed["event"], claimed["round"]), ("analysis", round_no))

    def test_claim_prefers_earlier_batch_across_queues(self):
        """批次交出在先、轮次在后：合在一起领取时先领批次。"""
        first = self.add_note()
        second = self.add_note(text="第 5 页配色太浅", page=5)
        store.handoff_submit(self.review, "先交的批次")   # 批次 1：first + second
        analysis_store.start(self.review, first["id"])     # first 移出批次，开题轮次排队在后
        got = json.loads(_cli_out("claim", str(self.review), "--events", "analysis"))
        self.assertTrue(got["claimed"])
        self.assertEqual(got.get("event", "handoff"), "handoff")
        self.assertEqual(got["seq"], 1)

    def test_claim_prefers_earlier_round_across_queues(self):
        """轮次交出在先、批次在后：先领轮次；轮次在手上时一次只处理一项，领不到批次。"""
        note = self.add_note()
        analysis_store.start(self.review, note["id"])      # 开题轮次先排队
        other = self.add_note(text="第 5 页配色太浅", page=5)
        store.handoff_submit(self.review, "后交的批次")
        got = json.loads(_cli_out("claim", str(self.review), "--events", "analysis"))
        self.assertTrue(got["claimed"])
        self.assertEqual(got["event"], "analysis")
        self.assertEqual(got["round"], 1)
        # 轮次还在手上：同入口再领取（哪怕批次在等）被拒
        self.assertEqual(notes_cli.main(["claim", str(self.review), "--events", "analysis"]), 2)

    def test_exact_round_claim_does_not_claim_a_same_numbered_batch_or_jump_the_queue(self):
        """轮次与批次撞号时仍指向指定轮次；更早交出的批次必须先处理。"""
        first = self.add_note()
        self.add_note(text="另一条改稿意见")
        store.handoff_submit(self.review, "先交改稿")
        state, round_no = analysis_store.start(self.review, first["id"])
        command = ["claim", str(self.review), "--events", "analysis", "--round", str(round_no)]
        with redirect_stdout(io.StringIO()) as output:
            code = notes_cli.main(command)
        self.assertEqual(code, 3, output.getvalue())
        self.assertFalse(json.loads(output.getvalue())["claimed"])
        self.assertEqual(store.handoff_load(self.review)["items"][0]["status"], "pending")
        self.assertEqual(analysis_store.load(self.review, state["id"])["rounds"][0]["status"], "submitted")
        first_item = json.loads(_cli_out("claim", str(self.review), "--events", "analysis"))
        self.assertEqual(first_item["event"], "handoff")
        store.handoff_done(self.review, seq=first_item["seq"], accept_rest=True)
        result = json.loads(_cli_out(*command))
        self.assertEqual((result["event"], result["round"]), ("analysis", round_no))

    def test_review_request_queues_after_an_earlier_analysis_round(self):
        """复核按请求复核的时间排队，不能沿用最初改稿的顺序挤到分析前面。"""
        self.add_note()
        store.handoff_submit(self.review, target="review")
        store.handoff_claim(self.review)
        store.handoff_done(self.review, seq=1, mode="draft", accept_rest=True)
        _state, round_no = analysis_store.start(self.review, self.add_note(text="新的分析问题")["id"])
        store.handoff_request_review(self.review, 1)
        received = json.loads(_cli_out("claim", str(self.review), "--events", "analysis"))
        self.assertEqual((received["event"], received["round"]), ("analysis", round_no))

    def test_exact_batch_claim_does_not_skip_an_earlier_round(self):
        """--seq 只指批次；即便同号分析先交出，也不能误领分析或越序。"""
        state, round_no = analysis_store.start(self.review, self.add_note()["id"])
        self.add_note(text="后交改稿")
        store.handoff_submit(self.review)
        with redirect_stdout(io.StringIO()) as output:
            result = notes_cli.main(["claim", str(self.review), "--events", "analysis", "--seq", "1"])
        self.assertEqual(result, 3)
        self.assertEqual(json.loads(output.getvalue()), {"claimed": False, "reason": "earlier-item"})
        self.assertEqual(store.handoff_load(self.review)["items"][0]["status"], "pending")
        self.assertEqual(analysis_store.load(self.review, state["id"])["rounds"][0]["status"], "submitted")
        received = json.loads(_cli_out("claim", str(self.review), "--events", "analysis"))
        self.assertEqual((received["event"], received["round"]), ("analysis", round_no))

    def test_concurrent_claims_leave_only_one_item_in_progress(self):
        """两个并发领取者不能分别接下改稿和分析。"""
        note = self.add_note()
        self.add_note(text="同批改稿意见")
        store.handoff_submit(self.review)
        analysis_store.start(self.review, note["id"])
        barrier = threading.Barrier(2)

        def take():
            barrier.wait(timeout=2)
            try:
                event, _item = notes_cli.claim_next(self.review, by="Synthetic", include_analysis=True)
                return event
            except store.HandoffError as error:
                return error.code

        with ThreadPoolExecutor(max_workers=2) as executor:
            futures = [executor.submit(take) for _ in range(2)]
            results = [future.result(timeout=5) for future in futures]
        self.assertCountEqual(results, ["handoff", "agent-busy"])
        self.assertEqual(len(analysis_store.queue(self.review)), 1)

    def test_plain_claim_and_delivered_batch_command_cannot_skip_an_earlier_round(self):
        """普通命令和真实桌面提示里的批次命令都不能越过更早交出的分析。"""
        wakeup.bind(self.review, THREAD, self.desktop.path)
        state, _round_no = analysis_store.start(self.review, self.add_note()["id"])
        self.add_note(text="后交的改稿意见")
        store.handoff_submit(self.review)
        wakeup.deliver(self.review, "handoff", 1)
        with redirect_stdout(io.StringIO()) as output:
            code = notes_cli.main(["claim", str(self.review)])
        self.assertEqual(code, 3, output.getvalue())
        prefix = "Then claim this exact batch: "
        line = next(line for line in self.desktop.prompt().splitlines() if line.startswith(prefix))
        command = shlex.split(line[len(prefix):])
        received = subprocess.run(command, capture_output=True, text=True, timeout=10)
        self.assertEqual(received.returncode, 3, received.stdout + received.stderr)
        self.assertFalse(json.loads(received.stdout)["claimed"])
        self.assertEqual(store.handoff_load(self.review)["items"][0]["status"], "pending")
        self.assertEqual(analysis_store.load(self.review, state["id"])["rounds"][0]["status"], "submitted")

    def test_stage_filter_cannot_skip_an_earlier_batch(self):
        """复核再次交出后排在新批次后面，--stage review 不能越过新批次。"""
        self.add_note()
        store.handoff_submit(self.review, target="review")
        store.handoff_claim(self.review)
        store.handoff_done(self.review, seq=1, mode="draft")
        self.add_note(text="第二批新意见")
        store.handoff_submit(self.review)
        store.handoff_request_review(self.review, 1)
        with redirect_stdout(io.StringIO()) as output:
            code = notes_cli.main(["claim", str(self.review), "--events", "analysis", "--stage", "review"])
        self.assertEqual(code, 3, output.getvalue())
        self.assertEqual([item["status"] for item in store.handoff_load(self.review)["items"]],
                         ["review", "pending"])
        received = json.loads(_cli_out("claim", str(self.review), "--events", "analysis"))
        self.assertEqual((received["seq"], received["stage"]), (2, "draft"))

    def test_claim_refuses_a_second_item_while_a_batch_is_claimed(self):
        """已有初改批次时，无论另一批还是分析轮次都不能再领。"""
        self.add_note()
        store.handoff_submit(self.review, "第一批")
        store.handoff_claim(self.review)
        store.handoff_submit(self.review, "第二批")
        state, _round_no = analysis_store.start(self.review, self.add_note(text="分析问题")["id"])
        self.assertEqual(notes_cli.main(["claim", str(self.review), "--events", "analysis"]), 2)
        self.assertEqual(notes_cli.main(["claim", str(self.review)]), 2)
        self.assertEqual([item["status"] for item in store.handoff_load(self.review)["items"]],
                         ["claimed", "pending"])
        self.assertEqual(analysis_store.load(self.review, state["id"])["rounds"][0]["status"], "submitted")

    def test_old_waiter_and_plain_claim_cannot_take_analysis_round(self):
        """不带显式参数的等待与领取领不到分析轮次（旧进程不受影响）。"""
        note = self.add_note()
        state, round_no = analysis_store.start(self.review, note["id"])
        env = {k: v for k, v in os.environ.items()
               if k not in ("CODEX_THREAD_ID", "CLAUDECODE", "AE_HARNESS", "AE_SESSION_ID", "AE_LOCALE")}
        env.update(AE_HOME=str(self.root / "ledger"), PYTHONDONTWRITEBYTECODE="1")
        plain = subprocess.run([sys.executable, str(SCRIPTS / "notes.py"), "claim", str(self.review)],
                               env=env, capture_output=True, text=True, timeout=10)
        self.assertEqual(plain.returncode, 3, plain.stdout + plain.stderr)   # 没有可领的批次
        waited = subprocess.run([sys.executable, str(SCRIPTS / "notes.py"), "wait", str(self.review),
                                 "--timeout", "0.5", "--interval", "0.1"],
                                env=env, capture_output=True, text=True, timeout=10)
        self.assertEqual(waited.returncode, 3, waited.stdout + waited.stderr)
        rounds = analysis_store.load(self.review, state["id"])["rounds"]
        self.assertEqual([r["status"] for r in rounds], ["submitted"])

    def test_wait_with_events_analysis_takes_round_and_prints_context(self):
        """带 --events analysis 的等待领得到轮次，输出焦点问题与写回指引。"""
        note = self.add_note()
        state, round_no = analysis_store.start(self.review, note["id"])
        env = {k: v for k, v in os.environ.items()
               if k not in ("CODEX_THREAD_ID", "CLAUDECODE", "AE_HARNESS", "AE_SESSION_ID", "AE_LOCALE")}
        env.update(AE_HOME=str(self.root / "ledger"), PYTHONDONTWRITEBYTECODE="1")
        waited = subprocess.run([sys.executable, str(SCRIPTS / "notes.py"), "wait", str(self.review),
                                 "--timeout", "2", "--interval", "0.1", "--events", "analysis"],
                                env=env, capture_output=True, text=True, timeout=15)
        self.assertEqual(waited.returncode, 0, waited.stdout + waited.stderr)
        self.assertIn(note["text"], waited.stdout)   # 焦点问题
        self.assertIn("analysis-writeback", waited.stdout)
        rounds = analysis_store.load(self.review, state["id"])["rounds"]
        self.assertEqual(rounds[0]["status"], "claimed")
        self.assertEqual(rounds[0]["claimedBy"], "agent")

    def test_analysis_context_and_writeback_cli(self):
        """命令行读一轮的上下文、按基线版本写回结构提议。"""
        note = self.add_note()
        state, round_no = analysis_store.start(self.review, note["id"])
        claimed = analysis_store.claim(self.review, by="Synthetic")
        context = json.loads(_cli_out("analysis", str(self.review), "--round", str(round_no), "--json"))
        self.assertEqual(context["focus"], note["text"])
        self.assertEqual(context["round"], round_no)
        self.assertEqual(context["baseRev"], claimed["baseRev"])
        self.assertEqual(context["items"], [])
        proposals = [{"key": "p1", "text": "用年度还是季度口径验证？", "short": "验证口径",
                      "options": [{"id": "Y", "label": "年度"}, {"id": "S", "label": "季度"}],
                      "recommend": "Y", "parent": None}]
        out = json.loads(_cli_json_in(["analysis-writeback", str(self.review), "--round", str(round_no),
                                       "--base-rev", str(claimed["baseRev"]), "--proposals", "-"], proposals))
        self.assertEqual(out["nextRound"], round_no + 1)
        after = analysis_store.load(self.review, state["id"])
        self.assertEqual(after["rounds"][1]["items"][0]["key"], "S:p1")

    def test_transfer_blocked_while_round_claimed(self):
        """有处理中的分析轮次时，改绑 / 解除接收方被拒（TRANSFER_BLOCKS 登记）。"""
        wakeup.bind_wait(self.review, "Claude Code", "session-a")
        note = self.add_note()
        analysis_store.start(self.review, note["id"])
        analysis_store.claim(self.review, by="Synthetic")
        with self.assertRaises(ValueError) as ctx:
            wakeup.assert_transferable(self.review)
        self.assertIn("analysis-in-progress", str(ctx.exception))

    def test_http_api_shapes_errors_and_features(self):
        """HTTP 薄契约：能力标记、分析接口的形状与错误码。"""
        server = serve.ReviewServer(self.review, 0, log_to_file=False)
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()

        def close():
            server.shutdown()
            server.server_close()
            worker.join(2)
        self.addCleanup(close)
        headers = {"Host": f"127.0.0.1:{server.port}", "Origin": f"http://127.0.0.1:{server.port}",
                   "Content-Type": "application/json", "X-AE-Review": "synthetic-analysis"}

        code, _, raw = serve._req(server.port, "GET", "/api/health", {"Host": f"127.0.0.1:{server.port}"})
        self.assertEqual(code, 200)
        self.assertIn("analysis", json.loads(raw)["features"])

        code, _, raw = serve._req(server.port, "GET", "/api/analysis", {"Host": f"127.0.0.1:{server.port}"})
        self.assertEqual(code, 200)
        self.assertEqual(json.loads(raw)["analyses"], [])

        note = self.add_note()
        code, _, raw = serve._req(server.port, "POST", "/api/analysis/start", headers, {"noteId": note["id"]})
        self.assertEqual(code, 200, raw)
        started = json.loads(raw)
        self.assertEqual(started["round"], 1)
        self.assertEqual(started["analysis"]["sourceNote"], note["id"])
        self.assertEqual(started["note"]["status"], "held")

        code, _, raw = serve._req(server.port, "GET", "/api/analysis?analysis=" + started["analysis"]["id"],
                                 {"Host": f"127.0.0.1:{server.port}"})
        self.assertEqual(code, 200, raw)
        detail = json.loads(raw)
        self.assertEqual(detail["analysis"]["focus"], note["text"])
        self.assertEqual(detail["context"]["round"], 1)
        self.assertEqual(detail["queue"], {"submitted": 1, "claimed": 0})
        code, _, _ = serve._req(server.port, "GET", "/api/analysis?analysis=missing",
                               {"Host": f"127.0.0.1:{server.port}"})
        self.assertIn(code, (400, 404))

        code, _, raw = serve._req(server.port, "POST", "/api/analysis/start", headers, {"noteId": "missing"})
        self.assertEqual(code, 404)
        self.assertEqual(json.loads(raw)["code"], "no-note")
        code, _, raw = serve._req(server.port, "POST", "/api/analysis/start", headers, {})
        self.assertEqual(code, 400)

        aid = started["analysis"]["id"]
        code, _, raw = serve._req(server.port, "POST", "/api/analysis/answer", headers,
                                  {"analysis": aid, "round": 1, "key": "S:p1", "value": {"kind": "accept"}})
        self.assertEqual(code, 400, raw)   # 该轮还没有这一项
        code, _, raw = serve._req(server.port, "POST", "/api/analysis/answer", headers,
                                  {"analysis": aid, "round": 99, "key": "S:p1", "value": {"kind": "accept"}})
        self.assertEqual(code, 404)

    def test_http_analysis_submit_checks_the_receiver(self):
        """旧页面或旧服务不能向替换后的接收方交出分析。"""
        binding = wakeup.bind_wait(self.review, "Claude Code", "session-a")
        state, round_no = analysis_store.start(self.review, self.add_note()["id"])
        claimed = analysis_store.claim(self.review)
        result = analysis_store.writeback(self.review, state["id"], round_no, claimed["baseRev"],
                                         [{"key": "p1", "text": "采用哪个口径？", "short": "口径",
                                           "options": [{"id": "Y", "label": "年度"}], "recommend": "Y"}])
        server = serve.ReviewServer(self.review, 0, log_to_file=False)
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()

        def close():
            server.shutdown()
            server.server_close()
            worker.join(2)
        self.addCleanup(close)
        headers = {"Host": f"127.0.0.1:{server.port}", "Origin": f"http://127.0.0.1:{server.port}",
                   "Content-Type": "application/json", "X-AE-Review": "synthetic-analysis"}
        body = {"analysis": state["id"], "round": result["nextRound"], "receiverBindingId": "stale"}
        code, _, raw = serve._req(server.port, "POST", "/api/analysis/submit", headers, body)
        self.assertEqual((code, json.loads(raw).get("code")), (409, "receiver-mismatch"))
        self.assertEqual(analysis_store.queue(self.review), [])
        body["receiverBindingId"] = binding["bindingId"]
        wakeup.bind_wait(self.review, "Claude Code", "session-b", replace=True)
        code, _, raw = serve._req(server.port, "POST", "/api/analysis/submit", headers, body)
        self.assertEqual((code, json.loads(raw).get("code")), (409, "receiver-restart"))
        self.assertEqual(analysis_store.queue(self.review), [])

    def test_http_analysis_writes_share_origin_and_content_type_gates(self):
        """分析写操作与既有写接口同一套门：伪 Host、跨源 Origin、缺 Origin、非 JSON 一律拒绝。"""
        server = serve.ReviewServer(self.review, 0, log_to_file=False)
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()

        def close():
            server.shutdown()
            server.server_close()
            worker.join(2)
        self.addCleanup(close)
        good = {"Host": f"127.0.0.1:{server.port}"}
        same = {**good, "Origin": f"http://127.0.0.1:{server.port}", "Content-Type": "application/json"}
        note = self.add_note()
        guards = [({**same, "Host": "evil.example"}, 421),
                  ({**same, "Origin": "http://evil.example"}, 403),
                  ({k: v for k, v in same.items() if k != "Origin"}, 403),
                  ({**same, "Content-Type": "text/plain"}, 415)]
        for headers, expected in guards:
            code, _, _raw = serve._req(server.port, "POST", "/api/analysis/start", headers, {"noteId": note["id"]})
            self.assertEqual(code, expected, headers)
        code, _, _raw = serve._req(server.port, "POST", "/api/analysis/start", same, {"noteId": note["id"]})
        self.assertEqual(code, 200)
        self.assertEqual(store.load(self.review)["notes"][0]["status"], "held")


if __name__ == "__main__":
    unittest.main()
