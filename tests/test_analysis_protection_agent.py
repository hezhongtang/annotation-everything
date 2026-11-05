"""#113 CLI、投递提示与 HTTP 薄契约，全部使用合成审阅。"""
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
import notes_store as store
import serve
import wakeup


class ProtectionAgentTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="ae-protect-agent-")
        self.addCleanup(self.tmp.cleanup)
        self.d = Path(self.tmp.name) / "review"
        self.d.mkdir()
        (self.d / "review.json").write_text(json.dumps({"reviewId": "synthetic-protection", "pages": [
            {"n": 1, "key": "summary", "title": "合成概要", "stable": True}]}))
        (self.d / "index.html").write_text("<!doctype html><title>Synthetic</title>")
        self.env = patch.dict(os.environ, {"AE_HOME": str(Path(self.tmp.name) / "ledger")})
        self.env.start()
        self.addCleanup(self.env.stop)

    def question(self):
        state, seq = analysis.start_question(self.d, "采用哪个合成口径？", page=1)
        aid = state["id"]
        claimed = analysis.claim(self.d, seq=seq)
        seq = analysis.writeback(self.d, aid, seq, claimed["baseRev"], [{
            "key": "caliber", "text": "采用哪个口径？", "short": "口径", "recommend": "year",
            "options": [{"id": "year", "label": "全年"}, {"id": "quarter", "label": "季度"}]}])["nextRound"]
        analysis.answer(self.d, aid, seq, "S:caliber", {"kind": "accept"})
        analysis.answer(self.d, aid, seq, "Q:n1", {"kind": "accept"})
        return aid, seq

    def changed_note(self):
        aid, seq = self.question()
        draft = analysis.convert(self.d, aid, "n1", text="按全年口径核对合成数字")
        note = analysis.confirm_convert(self.d, aid, draft["id"])
        analysis.redecide(self.d, aid, "n1", "quarter")
        return aid, note

    def command(self, *args, payload=None):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err), patch.object(
                sys, "stdin", io.StringIO(json.dumps(payload) if payload is not None else "")):
            code = cli.main([args[0], str(self.d), *args[1:]])
        return code, out.getvalue(), err.getvalue()

    def server(self, *, locale="en"):
        server = serve.ReviewServer(self.d, 0, log_to_file=False, locale=locale)
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        def close():
            server.shutdown()
            server.server_close()
            worker.join(2)
        self.addCleanup(close)
        headers = {"Host": f"127.0.0.1:{server.port}", "Origin": f"http://127.0.0.1:{server.port}",
                   "Content-Type": "application/json", "X-AE-Review": "synthetic-protection"}
        return server, headers

    def test_read_current_conclusions_without_an_open_round_is_read_only(self):
        aid, seq = self.question()
        analysis.submit_round(self.d, aid, seq)
        claimed = analysis.claim(self.d, seq=seq)
        self.assertIsNone(analysis.writeback(self.d, aid, seq, claimed["baseRev"], [])["nextRound"])
        before = {p: p.read_bytes() for p in (self.d / "analysis").glob("*/events.jsonl")}
        code, out, err = self.command("analysis-check", "--json")
        self.assertEqual(code, 0, err)
        context = json.loads(out)
        self.assertEqual(context["analyses"][0]["conclusions"][0]["answer"], "全年")
        self.assertEqual({item["kind"] for item in context["checks"]}, {
            "changed-notes", "resolved-changed-basis", "report-conflicts", "analysis-conflicts"})
        self.assertEqual(before, {p: p.read_bytes() for p in before})

    def test_cli_redecide_request_and_update_envelope_keep_original_until_answered(self):
        aid, seq = self.question()
        draft = analysis.convert(self.d, aid, "n1", text="旧合成批注")
        note = analysis.confirm_convert(self.d, aid, draft["id"])
        code, out, err = self.command("analysis-redecide", "--analysis", aid, "--node", "n1", "--option", "quarter")
        self.assertEqual(code, 0, err)
        self.assertTrue(json.loads(out)["ok"])
        code, _, err = self.command("analysis-note", "--analysis", aid, "--note-id", note["id"], "--action", "update")
        self.assertEqual(code, 0, err)
        seq = analysis.queue(self.d)[0]["seq"]
        claimed = analysis.claim(self.d, seq=seq)
        code, out, err = self.command("analysis-writeback", "--analysis", aid, "--round", str(seq),
                                      "--base-rev", str(claimed["baseRev"]), "--proposals", "-", payload={
                                          "updates": [{"noteId": note["id"], "text": "按季度口径核对合成数字"}],
                                          "proposals": [], "assumptions": [], "facts": []})
        self.assertEqual(code, 0, err)
        seq = json.loads(out)["nextRound"]
        code, out, err = self.command("analysis", "--round", str(seq), "--locale", "en")
        self.assertEqual(code, 0, err)
        self.assertIn("旧合成批注", out)
        self.assertIn("按季度口径核对合成数字", out)
        stored = next(n for n in store.load(self.d)["notes"] if n["id"] == note["id"])
        self.assertEqual(stored["text"], "旧合成批注")
        analysis.answer(self.d, aid, seq, "U:u1", {"kind": "accept"})
        stored = next(n for n in store.load(self.d)["notes"] if n["id"] == note["id"])
        self.assertEqual(stored["text"], "按季度口径核对合成数字")
        self.assertEqual(stored["analysisHistory"][0]["text"], "旧合成批注")

    def test_full_check_prompts_four_confirmation_lists_without_changing_notes(self):
        aid, note = self.changed_note()
        _, plain = store.add(self.d, {"page": 1, "text": "合成待改批注"})
        _, batch, _ = store.handoff_submit(self.d, full_check=True)
        binding = {"reviewDir": str(self.d), "reviewId": "synthetic-protection"}
        before = store.load(self.d)
        prompt = wakeup.prompt(binding, "handoff", batch["seq"], "synthetic-message")
        for expected in ("analysis-check", "changed-notes", "resolved-changed-basis", "report-conflicts", "analysis-conflicts"):
            self.assertIn(expected, prompt)
        self.assertIn("Do not rewrite annotation text", prompt)
        code, out, err = self.command("wait", "--no-claim", "--timeout", "0.01", "--locale", "en")
        self.assertEqual(code, 0, err)
        self.assertIn("analysis-check", out)
        self.assertIn("changed conclusions", out)
        self.assertEqual(before, store.load(self.d))
        _, normal, _ = store.handoff_submit(self.d, full_check=False)
        normal_prompt = wakeup.prompt(binding, "handoff", normal["seq"], "normal")
        self.assertNotIn("analysis-check", normal_prompt)

    def test_http_protection_commands_check_receiver_and_return_linked_notes(self):
        aid, seq = self.question()
        draft = analysis.convert(self.d, aid, "n1", text="合成旧文本")
        note = analysis.confirm_convert(self.d, aid, draft["id"])
        binding = wakeup.bind_wait(self.d, "Claude Code", "synthetic-session")
        server, headers = self.server()
        body = {"analysis": aid, "node": "n1", "option": "quarter", "receiverBindingId": "old"}
        code, _, raw = serve._req(server.port, "POST", "/api/analysis/redecide", headers, body)
        self.assertEqual((code, json.loads(raw).get("code")), (409, "receiver-mismatch"))
        body["receiverBindingId"] = binding["bindingId"]
        with patch.object(server, "run_analysis_hook") as hook:
            code, _, raw = serve._req(server.port, "POST", "/api/analysis/redecide", headers, body)
        self.assertEqual(code, 200, raw)
        self.assertTrue(hook.called)
        result = json.loads(raw)
        linked = result["analysis"]["linkedNotes"][0]
        self.assertEqual((linked["id"], linked["status"], linked["text"]), (note["id"], "held", "合成旧文本"))
        self.assertEqual(result["doc"]["notes"][0]["status"], "held")
        payload = {"analysis": aid, "noteId": note["id"], "receiverBindingId": binding["bindingId"]}
        code, _, raw = serve._req(server.port, "POST", "/api/analysis/update-note", headers, payload)
        self.assertEqual(code, 200, raw)
        self.assertEqual(json.loads(raw)["analysis"]["updateRequests"][note["id"]]["status"], "requested")
        code, _, raw = serve._req(server.port, "GET", "/api/analysis?analysis=" + aid, headers)
        self.assertEqual(json.loads(raw)["analysis"]["linkedNotes"][0]["id"], note["id"])
        seq = analysis.queue(self.d)[0]["seq"]
        analysis.claim(self.d, seq=seq)
        code, _, raw = serve._req(server.port, "POST", "/api/analysis/keep-note", headers, payload)
        self.assertEqual((code, json.loads(raw).get("code")), (400, "analysis-locked"))
        self.assertIn("being processed", json.loads(raw)["error"])

    def test_http_keep_delete_and_old_round_errors_are_natural(self):
        aid, note = self.changed_note()
        server, headers = self.server()
        for action in ("keep-note", "delete-note"):
            code, _, raw = serve._req(server.port, "POST", "/api/analysis/" + action, headers, {"analysis": aid})
            self.assertEqual(code, 400, raw)
            self.assertIn("annotation", json.loads(raw)["error"].lower())
        code, _, raw = serve._req(server.port, "POST", "/api/analysis/keep-note", headers,
                                  {"analysis": aid, "noteId": note["id"]})
        self.assertEqual(code, 200, raw)
        self.assertFalse(json.loads(raw)["analysis"]["linkedNotes"][0]["tracking"])
        old = analysis.load(self.d, aid)["rounds"][1]["seq"]
        code, _, raw = serve._req(server.port, "POST", "/api/analysis/answer", headers,
                                  {"analysis": aid, "round": old, "key": "Q:n1", "value": {"kind": "accept"}})
        self.assertEqual(code, 400, raw)
        self.assertEqual(json.loads(raw)["code"], "round-not-answerable")
        self.assertIn("refresh", json.loads(raw)["error"].lower())
        other, discarded = self.changed_note()
        code, _, raw = serve._req(server.port, "POST", "/api/analysis/delete-note", headers,
                                  {"analysis": other, "noteId": discarded["id"]})
        self.assertEqual(code, 200, raw)
        self.assertEqual(json.loads(raw)["analysis"]["linkedNotes"], [])
        self.assertNotIn(discarded["id"], [n["id"] for n in json.loads(raw)["doc"]["notes"]])


if __name__ == "__main__":
    unittest.main()
