"""#114 历史与分析线的 CLI/HTTP 薄契约。"""
from __future__ import annotations

import io
import json
import os
import sys
import tempfile
import threading
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "skill/annotation-everything/scripts"))
sys.path.insert(0, str(ROOT))
import host_isolation
host_isolation.require_for_module(__name__)
import analysis_store as analysis
import notes as cli
import serve
import wakeup


class AnalysisHistoryAgentTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="ae-analysis-history-agent-")
        self.addCleanup(temporary.cleanup)
        self.review = Path(temporary.name)
        (self.review / "review.json").write_text(json.dumps({"reviewId": "synthetic-history"}))
        (self.review / "index.html").write_text("<!doctype html><title>Synthetic</title>")
        env = patch.dict(os.environ, {"AE_HOME": str(self.review / "ledger")})
        env.start()
        self.addCleanup(env.stop)
        state, opening = analysis.start_question(self.review, "合成增长结论是否成立？")
        self.aid = state["id"]
        claimed = analysis.claim(self.review, seq=opening)
        self.number = analysis.writeback(self.review, self.aid, opening, claimed["baseRev"], [{
            "key": "scope", "text": "合成统计口径是什么？", "short": "统计口径", "recommend": "year",
            "options": [{"id": "year", "label": "全年"}, {"id": "quarter", "label": "季度"}]}])["nextRound"]
        analysis.answer(self.review, self.aid, self.number, "S:scope", {"kind": "accept"})
        analysis.answer(self.review, self.aid, self.number, "Q:n1", {"kind": "accept"})

    def command(self, command, *args):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            result = cli.main([command, str(self.review), *args])
        return result, out.getvalue(), err.getvalue()

    def test_cli_history_and_line_actions_have_localized_output_and_json_state(self):
        code, out, err = self.command("analysis-line", "--analysis", self.aid, "--action", "seal")
        self.assertEqual(code, 0, err)
        checkpoint = json.loads(out)["analysis"]["checkpoints"][-1]["id"]
        code, out, err = self.command("analysis-line", "--analysis", self.aid, "--action", "fork", "--checkpoint", checkpoint)
        self.assertEqual(code, 0, err)
        self.assertNotEqual(json.loads(out)["analysis"]["line"], "main")
        code, out, err = self.command("analysis-history", "--analysis", self.aid, "--json")
        self.assertEqual(code, 0, err)
        self.assertEqual(len(json.loads(out)["lines"]), 2)
        code, out, err = self.command("analysis-history", "--analysis", self.aid, "--locale", "en")
        self.assertEqual(code, 0, err)
        self.assertIn("Main line", out)
        self.assertIn("Second line", out)
        self.assertIn("Checkpoint", out)
        code, out, err = self.command("analysis-line", "--analysis", self.aid, "--action", "fork",
                                      "--checkpoint", "missing-internal-checkpoint", "--locale", "en")
        self.assertEqual(code, 1)
        self.assertIn("checkpoint", err.lower())
        self.assertNotIn("missing-internal-checkpoint", err)

    def server(self):
        binding = wakeup.bind_wait(self.review, "Claude Code", "synthetic-history-session")
        server = serve.ReviewServer(self.review, 0, log_to_file=False, locale="en")
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        def close():
            server.shutdown()
            server.server_close()
            worker.join(2)
        self.addCleanup(close)
        headers = {"Host": f"127.0.0.1:{server.port}", "Origin": f"http://127.0.0.1:{server.port}",
                   "Content-Type": "application/json", "X-AE-Review": "synthetic-history"}
        return server, headers, binding

    def test_http_history_operations_preserve_current_line_and_receiver_gates(self):
        server, headers, binding = self.server()
        payload = {"analysis": self.aid, "receiverBindingId": binding["bindingId"]}
        bad = {**payload, "receiverBindingId": "stale"}
        code, _, raw = serve._req(server.port, "POST", "/api/analysis/seal", headers, bad)
        self.assertEqual((code, json.loads(raw).get("code")), (409, "receiver-mismatch"))
        code, response_headers, _ = serve._req(server.port, "POST", "/api/analysis/seal",
                                               {**headers, "Origin": "http://evil.example"}, payload)
        self.assertEqual(code, 403)
        self.assertFalse(any(k.lower().startswith("access-control-") for k in response_headers))
        def post(action, **extra):
            code, _, raw = serve._req(server.port, "POST", "/api/analysis/" + action, headers, {**payload, **extra})
            self.assertEqual(code, 200, raw)
            return json.loads(raw)
        sealed = post("seal")
        checkpoint = sealed["analysis"]["checkpoints"][-1]["id"]
        self.assertIn("doc", sealed)
        with patch.object(server, "run_analysis_hook") as hook:
            forked = post("fork", checkpoint=checkpoint)
        self.assertTrue(hook.called)
        other = forked["analysis"]["line"]
        number = next(r["seq"] for r in forked["analysis"]["rounds"] if r["line"] == other and r["status"] == "submitted")
        post("switch-line", line="main")
        code, _, raw = serve._req(server.port, "GET", "/api/analysis?analysis=" + self.aid, headers)
        self.assertEqual(code, 200, raw)
        self.assertIsNone(json.loads(raw)["context"])
        code, _, raw = serve._req(server.port, "GET", f"/api/analysis?analysis={self.aid}&round={number}", headers)
        result = json.loads(raw)
        self.assertEqual((result["analysis"]["line"], result["context"]["line"]), ("main", other))
        code, _, raw = serve._req(server.port, "POST", "/api/analysis/close", headers, payload)
        self.assertEqual((code, json.loads(raw).get("code")), (400, "analysis-pending-rounds"))
        self.assertIn("Submitted", json.loads(raw)["error"])
        claimed = analysis.claim(self.review, seq=number)
        analysis.writeback(self.review, self.aid, number, claimed["baseRev"], [])
        self.assertEqual(post("close")["analysis"]["status"], "closed")
        with patch.object(server, "run_analysis_hook") as hook:
            reopened = post("reopen")
        self.assertEqual(reopened["analysis"]["status"], "active")
        self.assertTrue(hook.called)
        code, _, raw = serve._req(server.port, "GET", f"/api/analysis?analysis={self.aid}&round=bad", headers)
        self.assertEqual((code, json.loads(raw).get("code")), (400, "bad-request"))

    def test_historical_cli_read_keeps_the_agent_note_and_has_no_writeback_instruction(self):
        analysis.submit_round(self.review, self.aid, self.number)
        claimed = analysis.claim(self.review, seq=self.number)
        analysis.writeback(self.review, self.aid, self.number, claimed["baseRev"], [], note="完整合成说明。")
        code, out, err = self.command("analysis", "--round", str(self.number), "--locale", "en")
        self.assertEqual(code, 0, err)
        self.assertIn("完整合成说明。", out)
        self.assertIn("read-only", out)
        self.assertNotIn("analysis-writeback", out)
