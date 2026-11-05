"""#115 推翻假设与死局的 CLI/HTTP 薄契约。"""
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


class AnalysisResolutionAgentTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="ae-analysis-resolution-agent-")
        self.addCleanup(temporary.cleanup)
        self.review = Path(temporary.name)
        (self.review / "review.json").write_text(json.dumps({"reviewId": "synthetic-resolution"}))
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
        state = analysis.answer(self.review, self.aid, self.number, "Q:n1", {"kind": "accept"})
        analysis.submit_round(self.review, self.aid, self.number)
        claimed = analysis.claim(self.review, seq=self.number)
        self.number = analysis.writeback(self.review, self.aid, self.number, claimed["baseRev"], [],
                                        assumptions=[{"key": "source", "text": "合成全年数据口径可比",
                                                      "decisions": [state["nodes"]["n1"]["decision"]]}])["nextRound"]
        analysis.answer(self.review, self.aid, self.number, "A:a1", {"kind": "accept"})

    def command(self, command, *args):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            result = cli.main([command, str(self.review), *args])
        return result, out.getvalue(), err.getvalue()

    def test_cli_dead_end_writeback_read_and_refutation_are_localized(self):
        analysis.submit_round(self.review, self.aid, self.number)
        claimed = analysis.claim(self.review, seq=self.number)
        payload = self.review / "writeback.json"
        payload.write_text(json.dumps({"proposals": [], "deadEnds": [{
            "key": "quarter", "kind": "option", "node": "n1", "option": "quarter",
            "reason": "合成季度路径缺少依据", "assumptions": ["a1"]}]}), encoding="utf-8")
        code, out, err = self.command("analysis-writeback", "--round", str(self.number), "--base-rev",
                                      str(claimed["baseRev"]), "--proposals", str(payload), "--locale", "en")
        self.assertEqual(code, 0, err)
        number = json.loads(out)["nextRound"]
        self.assertIsNotNone(number)
        code, out, err = self.command("analysis", "--round", str(number), "--locale", "en")
        self.assertEqual(code, 0, err)
        self.assertIn("Dead end", out)
        self.assertIn("合成季度路径缺少依据", out)
        analysis.answer(self.review, self.aid, number, "D:e1", {"kind": "accept"})
        code, out, err = self.command("analysis-redecide", "--analysis", self.aid,
                                      "--node", "n1", "--option", "quarter", "--locale", "en")
        self.assertEqual(code, 1)
        self.assertIn("dead end", err.lower())
        self.assertIn("合成季度路径缺少依据", err)
        self.assertIn("合成全年数据口径可比", err)
        code, out, err = self.command("analysis-refute", "--analysis", self.aid,
                                      "--assumption", "missing-internal-assumption", "--locale", "en")
        self.assertEqual(code, 1)
        self.assertIn("assumption", err.lower())
        self.assertNotIn("missing-internal-assumption", err)
        with patch.object(cli, "notify_analysis") as notify:
            code, out, err = self.command("analysis-refute", "--analysis", self.aid,
                                          "--assumption", "a1", "--locale", "en")
        self.assertEqual(code, 0, err)
        self.assertTrue(notify.called)
        state = json.loads(out)["analysis"]
        self.assertTrue(state["assumptions"]["a1"]["refuted"])
        self.assertEqual(state["deadEnds"]["e1"]["status"], "review")

    def server(self):
        binding = wakeup.bind_wait(self.review, "Claude Code", "synthetic-resolution-session")
        server = serve.ReviewServer(self.review, 0, log_to_file=False, locale="en")
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        def close():
            server.shutdown()
            server.server_close()
            worker.join(2)
        self.addCleanup(close)
        headers = {"Host": f"127.0.0.1:{server.port}", "Origin": f"http://127.0.0.1:{server.port}",
                   "Content-Type": "application/json", "X-AE-Review": "synthetic-resolution"}
        return server, headers, binding

    def test_http_refutation_has_thin_shape_security_receiver_and_claimed_contracts(self):
        server, headers, binding = self.server()
        payload = {"analysis": self.aid, "assumption": "a1", "receiverBindingId": binding["bindingId"]}
        before = analysis.load(self.review, self.aid)
        for changed, expected in (({"Host": "evil.example"}, 421), ({"Origin": "http://evil.example"}, 403),
                                  ({"Origin": ""}, 403), ({"Content-Type": "text/plain"}, 415)):
            code, response_headers, _ = serve._req(server.port, "POST", "/api/analysis/refute",
                                                   {**headers, **changed}, payload)
            self.assertEqual(code, expected)
            self.assertFalse(any(k.lower().startswith("access-control-") for k in response_headers))
        code, _, raw = serve._req(server.port, "POST", "/api/analysis/refute", headers,
                                  {**payload, "receiverBindingId": "stale"})
        self.assertEqual((code, json.loads(raw).get("code")), (409, "receiver-mismatch"))
        self.assertEqual(analysis.load(self.review, self.aid), before)
        with patch.object(server, "run_analysis_hook") as hook:
            code, _, raw = serve._req(server.port, "POST", "/api/analysis/refute", headers, payload)
        self.assertEqual(code, 200, raw)
        result = json.loads(raw)
        self.assertTrue(result["analysis"]["assumptions"]["a1"]["refuted"])
        self.assertNotEqual(result["analysis"]["line"], "main")
        self.assertIn("linkedNotes", result["analysis"])
        self.assertIn("doc", result)
        self.assertEqual(result["reviewId"], "synthetic-resolution")
        self.assertTrue(hook.called)
        number = analysis.queue(self.review)[0]["seq"]
        analysis.claim(self.review, seq=number)
        code, _, raw = serve._req(server.port, "POST", "/api/analysis/refute", headers, payload)
        self.assertEqual((code, json.loads(raw).get("code")), (400, "analysis-locked"))

    def test_http_dead_end_refusal_returns_localizable_reason_and_assumption_data(self):
        analysis.submit_round(self.review, self.aid, self.number)
        claimed = analysis.claim(self.review, seq=self.number)
        number = analysis.writeback(self.review, self.aid, self.number, claimed["baseRev"], [], dead_ends=[{
            "key": "quarter", "kind": "option", "node": "n1", "option": "quarter",
            "reason": "合成季度路径缺少依据", "assumptions": ["a1"]}])["nextRound"]
        analysis.answer(self.review, self.aid, number, "D:e1", {"kind": "accept"})
        server, headers, binding = self.server()
        code, _, raw = serve._req(server.port, "POST", "/api/analysis/redecide", headers,
                                  {"analysis": self.aid, "node": "n1", "option": "quarter",
                                   "receiverBindingId": binding["bindingId"]})
        result = json.loads(raw)
        self.assertEqual((code, result["code"]), (400, "dead-end-option"))
        self.assertEqual(result["params"], {"reason": "合成季度路径缺少依据", "assumptions": ["合成全年数据口径可比"]})
        self.assertIn("dead end", result["error"])

    def test_analysis_delivery_describes_dead_end_proposals_and_reviewer_confirmation(self):
        analysis.submit_round(self.review, self.aid, self.number)
        prompt = wakeup.prompt({"reviewDir": str(self.review), "reviewId": "synthetic-resolution"},
                               "analysis", self.number, "synthetic-resolution-message")
        for required in ("deadEnds", "targetLine", "reason", "assumptions", "only when the reviewer confirms",
                         "do not confirm or refute", "own source material only"):
            self.assertIn(required, prompt)
        self.assertIn("do not edit source files", prompt)


if __name__ == "__main__":
    unittest.main()
