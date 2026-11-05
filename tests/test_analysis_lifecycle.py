"""#112 的公开行为：焦点、五种作答、假设及查证事实。仅用合成审阅。"""
from __future__ import annotations

import json
import io
import sys
import tempfile
import threading
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "skill/annotation-everything/scripts"))
sys.path.insert(0, str(ROOT))
import host_isolation

host_isolation.require_for_module(__name__)
import analysis_store as analysis
import notes_store as store
import notes as notes_cli
import serve


def question(key="caliber"):
    return {"key": key, "text": "采用哪个口径？", "short": "口径", "recommend": "year",
            "options": [{"id": "year", "label": "全年"}, {"id": "quarter", "label": "季度"}]}


class AnalysisLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.d = Path(self.tmp.name)

    def start(self, text="合成材料中的结论是否成立？"):
        _, note = store.add(self.d, {"page": 1, "text": text})
        state, seq = analysis.start(self.d, note["id"])
        return state["id"], seq

    def open_questions(self, count=1):
        aid, seq = self.start()
        claimed = analysis.claim(self.d, seq=seq)
        result = analysis.writeback(self.d, aid, seq, claimed["baseRev"],
                                    [question(f"p{i}") for i in range(count)])
        seq = result["nextRound"]
        for i in range(count):
            analysis.answer(self.d, aid, seq, f"S:p{i}", {"kind": "accept"})
        return aid, seq

    def test_page_and_blank_focus_do_not_create_or_hold_notes(self):
        _, untouched = store.add(self.d, {"page": 1, "text": "原有未处理批注"})
        (self.d / "review.json").write_text(json.dumps({"pages": [
            {"n": 1, "key": "summary", "title": "合成概要", "stable": True}]}))
        state, seq = analysis.start_question(self.d, "这一页的结论有依据吗？", page=1)
        self.assertIsNone(state["sourceNote"])
        self.assertEqual(state["sourceCopy"]["key"], "summary")
        self.assertEqual(state["sourceState"], "none")
        self.assertEqual(store.load(self.d)["notes"][0]["status"], "open")
        blank, _ = analysis.start_question(self.d, "跨页口径如何统一？")
        self.assertIsNone(blank["sourceNote"])
        self.assertEqual(len(store.load(self.d)["notes"]), 1)
        self.assertEqual(store.load(self.d)["notes"][0]["id"], untouched["id"])
        claimed = analysis.claim(self.d, seq=seq)
        follow = analysis.writeback(self.d, state["id"], seq, claimed["baseRev"], [question()])["nextRound"]
        analysis.answer(self.d, state["id"], follow, "S:caliber", {"kind": "accept"})
        analysis.answer(self.d, state["id"], follow, "Q:n1", {"kind": "accept"})
        draft = analysis.convert(self.d, state["id"], "n1")
        self.assertEqual(len(store.load(self.d)["notes"]), 1)
        note = analysis.confirm_convert(self.d, state["id"], draft["id"])
        self.assertEqual(note["key"], "summary")
        self.assertEqual(note["page"], 1)
        self.assertEqual(analysis.load(self.d, state["id"])["sourceState"], "none")

    def test_switching_focus_keeps_claimed_baseline_and_paused_queue(self):
        first, seq = self.start()
        claimed = analysis.claim(self.d, seq=seq)
        second, other = self.start("另一项合成分析")
        self.assertEqual(analysis.load(self.d, first)["status"], "paused")
        self.assertEqual(analysis.load(self.d, first)["rev"], claimed["baseRev"])
        analysis.writeback(self.d, first, seq, claimed["baseRev"], [question()])
        analysis.activate(self.d, first)
        self.assertEqual(analysis.load(self.d, first)["status"], "active")
        self.assertEqual(analysis.load(self.d, second)["status"], "paused")
        self.assertEqual(analysis.claim(self.d)["seq"], other)
        analysis.pause(self.d, first)
        self.assertEqual(analysis.load(self.d, first)["status"], "paused")

    def test_five_answers_preserve_original_text_and_merge_partial_round(self):
        aid, seq = self.open_questions(5)
        raw = "我倾向于  逐月核验\n并保留异常月份。"
        analysis.answer(self.d, aid, seq, "Q:n1", {"kind": "accept"})
        analysis.answer(self.d, aid, seq, "Q:n2", {"kind": "option", "option": "quarter"})
        state = analysis.answer(self.d, aid, seq, "Q:n3", {"kind": "text", "text": raw})
        dec = state["decisions"][state["nodes"]["n3"]["decision"]]
        self.assertEqual(dec["label"], raw)
        self.assertEqual(state["nodes"]["n3"]["options"][dec["option"]], raw)
        did = dec["id"]
        state = analysis.answer(self.d, aid, seq, "Q:n3", {"kind": "text", "text": raw})
        self.assertEqual(state["nodes"]["n3"]["decision"], did)
        state = analysis.answer(self.d, aid, seq, "Q:n4", {"kind": "park"})
        self.assertEqual(state["nodes"]["n4"]["status"], "parked")
        self.assertNotIn("Q:n4", [i["key"] for i in state["rounds"][-1]["items"]])
        # 第五题暂不答，重复交出仍是同一轮。
        state = analysis.submit_round(self.d, aid, seq)
        order = state["rounds"][-1]["order"]
        state = analysis.submit_round(self.d, aid, seq)
        self.assertEqual(state["rounds"][-1]["order"], order)
        state = analysis.answer(self.d, aid, seq, "Q:n5", {"kind": "verify"})
        self.assertIs(state["rounds"][-1]["answers"]["Q:n5"]["allowExternal"], False)
        for invalid in ("true", 1, None):
            with self.assertRaises(store.NoteError):
                analysis.answer(self.d, aid, seq, "Q:n5", {"kind": "verify", "allowExternal": invalid})
        state = analysis.answer(self.d, aid, seq, "Q:n5", {"kind": "verify", "allowExternal": True})
        self.assertIs(state["rounds"][-1]["answers"]["Q:n5"]["allowExternal"], True)
        answered = {i["key"]: i for i in analysis.round_context(self.d, aid, seq)["answeredItems"]}
        self.assertEqual(answered["Q:n5"]["text"], "采用哪个口径？")
        self.assertEqual(answered["Q:n5"]["recommend"], "year")
        claimed = analysis.claim(self.d, seq=seq)
        nxt = analysis.writeback(self.d, aid, seq, claimed["baseRev"], [], facts=[
            {"key": "sales", "text": "合成底稿全年收入为120。", "source": "合成底稿第2页", "node": "n5"}])["nextRound"]
        ctx = analysis.round_context(self.d, aid, nxt)
        self.assertEqual(ctx["facts"][0]["text"], "合成底稿全年收入为120。")
        self.assertIn("Q:n5", [i["key"] for i in ctx["items"]])
        self.assertNotIn("Q:n4", [i["key"] for i in ctx["items"]])

    def test_partial_answer_and_rejected_proposal_do_not_disappear_or_repeat(self):
        aid, seq = self.start()
        claimed = analysis.claim(self.d, seq=seq)
        seq = analysis.writeback(self.d, aid, seq, claimed["baseRev"], [question("keep"), question("drop")])["nextRound"]
        analysis.answer(self.d, aid, seq, "S:drop", {"kind": "reject"})
        analysis.submit_round(self.d, aid, seq)
        claimed = analysis.claim(self.d, seq=seq)
        seq = analysis.writeback(self.d, aid, seq, claimed["baseRev"], [question("drop")])["nextRound"]
        ctx = analysis.round_context(self.d, aid, seq)
        self.assertEqual([i["key"] for i in ctx["items"]], ["S:keep"])

    def test_assumptions_are_confirmed_per_line_and_attached_to_decisions(self):
        aid, seq = self.open_questions(2)
        state = analysis.answer(self.d, aid, seq, "Q:n1", {"kind": "accept"})
        did = state["nodes"]["n1"]["decision"]
        analysis.submit_round(self.d, aid, seq)
        claimed = analysis.claim(self.d, seq=seq)
        seq = analysis.writeback(self.d, aid, seq, claimed["baseRev"], [], assumptions=[
            {"key": "scope", "text": "两期合并范围一致。", "decisions": [did]},
            {"key": "season", "text": "不存在季节性影响。", "decisions": [did]}])["nextRound"]
        ctx = analysis.round_context(self.d, aid, seq)
        self.assertEqual([i["type"] for i in ctx["items"]], ["assumption", "assumption", "question"])
        analysis.answer(self.d, aid, seq, "A:a1", {"kind": "accept"})
        state = analysis.answer(self.d, aid, seq, "A:a2", {"kind": "reject"})
        self.assertEqual(state["assumptions"]["a1"]["confirmed"], {"main": "confirmed"})
        self.assertEqual(state["assumptions"]["a2"]["confirmed"], {"main": "rejected"})
        self.assertFalse(state["assumptions"]["a2"]["refuted"])
        self.assertEqual(state["decisions"][did]["assumptions"], ["a1", "a2"])
        ctx = analysis.round_context(self.d, aid, seq)
        self.assertEqual([a["status"] for a in ctx["assumptions"]], ["confirmed", "rejected"])
        self.assertEqual(ctx["answers"]["A:a2"], {"kind": "reject"})
        old_ids = set(state["assumptions"])
        state = analysis.answer(self.d, aid, seq, "Q:n2", {"kind": "accept"})
        other_decision = state["nodes"]["n2"]["decision"]
        analysis.submit_round(self.d, aid, seq)
        claimed = analysis.claim(self.d, seq=seq)
        after = analysis.writeback(self.d, aid, seq, claimed["baseRev"], [], assumptions=[
            {"key": "scope", "text": "两期合并范围一致。", "decisions": [other_decision]}])["state"]
        self.assertEqual(set(after["assumptions"]), old_ids)
        self.assertEqual(after["decisions"][other_decision]["assumptions"], ["a1"])
        self.assertEqual(after["assumptions"]["a1"]["confirmed"]["main"], "confirmed")

    def test_agent_suggestion_does_not_create_analysis(self):
        _, note = store.add(self.d, {"page": 1, "text": "需要展开的合成意见"})
        updated = analysis.suggest(self.d, note["id"], reason="需要先核对统计口径", by="Synthetic")
        self.assertEqual(updated["analysisSuggestion"]["reason"], "需要先核对统计口径")
        self.assertEqual(updated["status"], "open")
        self.assertEqual(analysis.list_analyses(self.d), [])
        store.set_status(self.d, note["id"], "resolved", resolution="合成处理")
        with self.assertRaises(store.HandoffError):
            analysis.suggest(self.d, note["id"], reason="不能对已处理意见建议")

    def test_structure_split_merge_and_void_require_confirmation_and_keep_history(self):
        aid, seq = self.open_questions(2)
        state = analysis.answer(self.d, aid, seq, "Q:n1", {"kind": "accept"})
        original = state["nodes"]["n1"]["decision"]
        analysis.submit_round(self.d, aid, seq)
        claimed = analysis.claim(self.d, seq=seq)
        split = {"key": "split", "op": "split", "targets": ["n1"],
                 "replacements": [question("split-a"), question("split-b")]}
        seq = analysis.writeback(self.d, aid, seq, claimed["baseRev"], [split])["nextRound"]
        before = analysis.load(self.d, aid)
        self.assertEqual(before["nodes"]["n1"]["status"], "decided")
        self.assertEqual(len(before["nodes"]), 2)
        # 先拒绝不会改树；同轮确认后才分配新问题，原节点和决策仍可回看。
        state = analysis.answer(self.d, aid, seq, "S:split", {"kind": "reject"})
        self.assertEqual(state["nodes"]["n1"]["decision"], original)
        state = analysis.answer(self.d, aid, seq, "S:split", {"kind": "accept"})
        self.assertEqual(state["nodes"]["n1"]["status"], "void")
        self.assertEqual(state["decisions"][original]["label"], "全年")
        self.assertEqual(state["nodes"]["n2"]["status"], "pending")
        new_ids = [n["id"] for n in state["nodes"].values() if n["id"] not in ("n1", "n2")]
        self.assertEqual(len(new_ids), 2)
        analysis.answer(self.d, aid, seq, "S:split", {"kind": "reject"})
        state = analysis.answer(self.d, aid, seq, "S:split", {"kind": "accept"})
        self.assertEqual(list(state["nodes"]), ["n1", "n2", *new_ids])
        analysis.submit_round(self.d, aid, seq)
        claimed = analysis.claim(self.d, seq=seq)
        merge = {"key": "merge", "op": "merge", "targets": new_ids,
                 "replacements": [question("merged")]}
        seq = analysis.writeback(self.d, aid, seq, claimed["baseRev"], [merge])["nextRound"]
        state = analysis.answer(self.d, aid, seq, "S:merge", {"kind": "accept"})
        self.assertTrue(all(state["nodes"][nid]["status"] == "void" for nid in new_ids))
        merged = "n5"
        self.assertEqual(state["nodes"][merged]["status"], "pending")
        analysis.submit_round(self.d, aid, seq)
        claimed = analysis.claim(self.d, seq=seq)
        seq = analysis.writeback(self.d, aid, seq, claimed["baseRev"], [
            {"key": "void", "op": "void", "targets": [merged]}])["nextRound"]
        state = analysis.answer(self.d, aid, seq, "S:void", {"kind": "accept"})
        self.assertEqual(state["nodes"][merged]["status"], "void")
        self.assertEqual(state["nodes"]["n2"]["status"], "pending")
        self.assertEqual(analysis.load(self.d, aid)["decisions"][original]["id"], original)

    def test_invalid_structure_cannot_partially_finish_a_claimed_round(self):
        aid, seq = self.open_questions()
        analysis.submit_round(self.d, aid, seq)
        claimed = analysis.claim(self.d, seq=seq)
        for proposal in [
            {"key": "bad", "op": "void", "targets": ["absent"]},
            {"key": "bad", "op": "merge", "targets": ["n1"], "replacements": [question()]},
            {"key": "bad", "op": "split", "targets": ["n1"], "replacements": [question()]},
        ]:
            with self.assertRaises(store.NoteError):
                analysis.writeback(self.d, aid, seq, claimed["baseRev"], [proposal])
            self.assertEqual(analysis.round_context(self.d, aid, seq)["status"], "claimed")


class AnalysisLifecycleTransportTests(unittest.TestCase):
    """只核对新增传输形状，不在 HTTP 重复存储规则。"""
    setUp = AnalysisLifecycleTests.setUp
    start = AnalysisLifecycleTests.start
    open_questions = AnalysisLifecycleTests.open_questions

    def test_cli_new_validation_error_is_localized_and_keeps_claim(self):
        (self.d / "review.json").write_text(json.dumps({"reviewId": "synthetic-life"}))
        aid, seq = self.start()
        claimed = analysis.claim(self.d, seq=seq)
        path = self.d / "bad.json"
        path.write_text(json.dumps({"proposals": [], "facts": [{"key": "synthetic-private", "text": ""}]}))
        errors = io.StringIO()
        with redirect_stdout(io.StringIO()), redirect_stderr(errors):
            code = notes_cli.main(["analysis-writeback", str(self.d), "--round", str(seq), "--base-rev",
                                   str(claimed["baseRev"]), "--proposals", str(path), "--locale", "en"])
        self.assertEqual(code, 1)
        self.assertIn("Invalid analysis input", errors.getvalue())
        self.assertNotIn("synthetic-private", errors.getvalue())
        self.assertEqual(analysis.round_context(self.d, aid, seq)["status"], "claimed")

    def test_cli_payload_exposes_assumptions_facts_and_suggestion(self):
        (self.d / "review.json").write_text(json.dumps({"reviewId": "synthetic-life"}))
        aid, seq = self.open_questions()
        state = analysis.answer(self.d, aid, seq, "Q:n1", {"kind": "accept"})
        did = state["nodes"]["n1"]["decision"]
        analysis.submit_round(self.d, aid, seq)
        claimed = analysis.claim(self.d, seq=seq)
        payload = {"proposals": [], "assumptions": [{"key": "scope", "text": "两期范围一致", "decisions": [did]}],
                   "facts": [{"key": "source", "text": "合成材料记载为120", "source": "合成底稿"}]}
        path = self.d / "writeback.json"
        path.write_text(json.dumps(payload))
        output = io.StringIO()
        with redirect_stdout(output):
            code = notes_cli.main(["analysis-writeback", str(self.d), "--round", str(seq),
                                   "--base-rev", str(claimed["baseRev"]), "--proposals", str(path)])
        self.assertEqual(code, 0)
        ctx = analysis.round_context(self.d, aid, json.loads(output.getvalue())["nextRound"])
        self.assertEqual(ctx["facts"][0]["text"], "合成材料记载为120")
        text = notes_cli.analysis_context_text(ctx, "en")
        self.assertIn("两期范围一致", text)
        self.assertIn("合成材料记载为120", text)
        _, note = store.add(self.d, {"page": 1, "text": "可以分析的合成意见"})
        with redirect_stdout(io.StringIO()):
            code = notes_cli.main(["suggest-analysis", str(self.d), note["id"], "--reason", "需要先查证"])
        self.assertEqual(code, 0)
        self.assertEqual(store.load(self.d)["notes"][-1]["analysisSuggestion"]["reason"], "需要先查证")
        store.set_status(self.d, note["id"], "resolved", resolution="合成处理")
        errors = io.StringIO()
        with redirect_stdout(io.StringIO()), redirect_stderr(errors):
            code = notes_cli.main(["suggest-analysis", str(self.d), note["id"], "--locale", "en"])
        self.assertEqual(code, 1)
        self.assertIn("only be suggested for an open annotation", errors.getvalue())

    def test_http_start_focus_and_switch_have_compatible_response(self):
        (self.d / "review.json").write_text(json.dumps({"reviewId": "synthetic-life", "pages": [
            {"n": 1, "key": "summary", "title": "合成概要", "stable": True}]}))
        server = serve.ReviewServer(self.d, 0, log_to_file=False, locale="en")
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        def cleanup():
            server.shutdown()
            server.server_close()
            worker.join(2)
        self.addCleanup(cleanup)
        headers = {"Host": f"127.0.0.1:{server.port}", "Origin": f"http://127.0.0.1:{server.port}",
                   "Content-Type": "application/json", "X-AE-Review": "synthetic-life"}
        code, _, raw = serve._req(server.port, "POST", "/api/analysis/start", headers, {"focus": ""})
        self.assertEqual(code, 400)
        rejected = json.loads(raw)
        self.assertEqual(rejected["code"], "focus-required")
        self.assertIn("focus question", rejected["error"])
        def post(op, data):
            code, _, raw = serve._req(server.port, "POST", "/api/analysis/" + op, headers, data)
            self.assertEqual(code, 200, raw)
            return json.loads(raw)
        first = post("start", {"focus": "合成页中的结论成立吗？", "page": 1})
        self.assertIsNone(first["note"])
        self.assertEqual(first["analysis"]["sourceCopy"]["key"], "summary")
        second = post("start", {"focus": "第二个合成问题"})
        state = post("activate", {"analysis": first["analysis"]["id"]})["analysis"]
        self.assertEqual(state["status"], "active")
        self.assertEqual(analysis.load(self.d, second["analysis"]["id"])["status"], "paused")
        state = post("pause", {"analysis": first["analysis"]["id"]})["analysis"]
        self.assertEqual(state["status"], "paused")
