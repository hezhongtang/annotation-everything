"""#115 公开行为：推翻假设、死局及整线组合。"""
from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "skill/annotation-everything/scripts"))
sys.path.insert(0, str(ROOT))

import host_isolation
host_isolation.require_for_module(__name__)
import analysis_store as analysis
import notes_store as notes


def question(key, *, parent=None):
    return {"key": key, "text": "合成材料应采用哪个统计口径？", "short": "统计口径",
            "options": [{"id": "year", "label": "全年"}, {"id": "quarter", "label": "季度"}],
            "recommend": "year", "parent": parent}


class AnalysisResolutionTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="ae-analysis-resolution-")
        self.addCleanup(temporary.cleanup)
        self.review = Path(temporary.name)
        env = patch.dict(os.environ, {"AE_HOME": str(self.review / "ledger")})
        env.start()
        self.addCleanup(env.stop)
        state, self.opening = analysis.start_question(self.review, "合成材料的增长结论是否成立？")
        self.aid = state["id"]

    def write(self, number, proposals=(), **kwargs):
        analysis.submit_round(self.review, self.aid, number)
        claimed = analysis.claim(self.review, seq=number)
        return analysis.writeback(self.review, self.aid, number, claimed["baseRev"], list(proposals), **kwargs)

    def reasoning(self):
        number = self.write(self.opening, [question("caliber")])["nextRound"]
        analysis.answer(self.review, self.aid, number, "S:caliber", {"kind": "accept"})
        before = analysis.answer(self.review, self.aid, number, "Q:n1", {"kind": "accept"})
        first = before["nodes"]["n1"]["decision"]
        child = question("comparison", parent={"node": "n1", "option": "year"})
        child.update(text="合成材料的对比期间应采用哪个范围？", short="对比期间")
        number = self.write(number, [child], assumptions=[
            {"key": "source", "text": "合成原始材料的全年口径可比", "decisions": [first]}],
            facts=[{"key": "verified", "text": "合成查证：样例数据覆盖全年", "source": "fixtures/sample"}])["nextRound"]
        analysis.answer(self.review, self.aid, number, "A:a1", {"kind": "accept"})
        analysis.answer(self.review, self.aid, number, "S:comparison", {"kind": "accept"})
        before = analysis.answer(self.review, self.aid, number, "Q:n2", {"kind": "accept"})
        second = before["nodes"]["n2"]["decision"]
        linked = []
        for node in ("n1", "n2"):
            draft = analysis.convert(self.review, self.aid, node, text="合成批注：" + node)
            linked.append(analysis.confirm_convert(self.review, self.aid, draft["id"]))
        return number, first, second, linked

    def test_refuted_assumption_cannot_gain_new_decision_dependencies(self):
        number = self.write(self.opening, [question("one"), question("two")])["nextRound"]
        for key in ("one", "two"):
            analysis.answer(self.review, self.aid, number, "S:" + key, {"kind": "accept"})
        for node in ("n1", "n2"):
            state = analysis.answer(self.review, self.aid, number, "Q:" + node, {"kind": "accept"})
        first, second = (state["nodes"][node]["decision"] for node in ("n1", "n2"))
        candidate = {"key": "premise", "text": "合成已推翻前提", "decisions": [first]}
        number = self.write(number, assumptions=[candidate])["nextRound"]
        analysis.answer(self.review, self.aid, number, "A:a1", {"kind": "accept"})
        draft = analysis.convert(self.review, self.aid, "n2", text="合成独立结论批注")
        note = analysis.confirm_convert(self.review, self.aid, draft["id"])
        state = analysis.refute_assumption(self.review, self.aid, "a1")
        refresh = next(r for r in state["rounds"] if r["status"] == "submitted")
        claimed = analysis.claim(self.review, seq=refresh["seq"])
        before_state = analysis.load(self.review, self.aid)
        before_notes = notes.load(self.review)
        with self.assertRaises(notes.HandoffError) as rejected:
            analysis.writeback(self.review, self.aid, refresh["seq"], claimed["baseRev"], [],
                               assumptions=[{**candidate, "decisions": [second]}])
        self.assertEqual(rejected.exception.code, "analysis-invalid-input")
        self.assertEqual(analysis.load(self.review, self.aid), before_state)
        self.assertEqual(notes.load(self.review), before_notes)
        self.assertTrue(analysis.decision_in_force(before_state, second))
        self.assertEqual(next(n for n in before_notes["notes"] if n["id"] == note["id"])["status"], "open")
        retried = analysis.writeback(self.review, self.aid, refresh["seq"], claimed["baseRev"], [],
                                     assumptions=[candidate])["state"]
        self.assertEqual(retried["assumptions"]["a1"]["decisions"], [first])
        self.assertTrue(retried["assumptions"]["a1"]["refuted"])
        self.assertTrue(analysis.decision_in_force(retried, second))

    def test_prototype_from_annotation_through_verification_conversion_close_and_reopen(self):
        review = self.review / "from-annotation"
        review.mkdir()
        _doc, source = notes.add(review, {"page": 1, "text": "合成审阅：增长结论是否站得住？"})
        analysis.suggest(review, source["id"], reason="值得确认统计口径")
        self.assertEqual(analysis.list_analyses(review), [])
        state, opening = analysis.start(review, source["id"])
        aid = state["id"]
        claimed = analysis.claim(review, seq=opening)
        number = analysis.writeback(review, aid, opening, claimed["baseRev"], [question("scope")])["nextRound"]
        analysis.answer(review, aid, number, "S:scope", {"kind": "accept"})
        analysis.answer(review, aid, number, "Q:n1", {"kind": "verify"})
        analysis.submit_round(review, aid, number)
        claimed = analysis.claim(review, seq=number)
        self.assertFalse(analysis.round_context(review, aid, number)["verificationRequests"][0]["allowExternal"])
        number = analysis.writeback(review, aid, number, claimed["baseRev"], [], facts=[{
            "key": "coverage", "text": "合成核查：统计数据覆盖全年", "source": "fixtures/sample"}])["nextRound"]
        answered = analysis.answer(review, aid, number, "Q:n1", {"kind": "accept", "verbatim": "确认采用全年"})
        did = answered["nodes"]["n1"]["decision"]
        analysis.submit_round(review, aid, number)
        claimed = analysis.claim(review, seq=number)
        number = analysis.writeback(review, aid, number, claimed["baseRev"], [], assumptions=[{
            "key": "comparable", "text": "合成数据全年可比", "decisions": [did]}])["nextRound"]
        analysis.answer(review, aid, number, "A:a1", {"kind": "accept"})
        before = notes.load(review)
        draft = analysis.convert(review, aid, "n1", text="合成改稿意见：按全年口径说明增长")
        self.assertEqual(notes.load(review), before)
        derived = analysis.confirm_convert(review, aid, draft["id"])
        original = next(n for n in notes.load(review)["notes"] if n["id"] == source["id"])
        self.assertEqual((original["status"], original["analysisId"]), ("resolved", aid))
        notes.handoff_submit(review)
        batch = notes.handoff_claim(review, by="Synthetic")
        self.assertEqual(batch["openIds"], [derived["id"]])
        notes.set_status(review, derived["id"], "resolved", by="Synthetic", seq=batch["seq"])
        notes.handoff_done(review, seq=batch["seq"])
        closed = analysis.close_analysis(review, aid)
        self.assertEqual(closed["status"], "closed")
        self.assertTrue(closed["checkpoints"][-1]["sealed"])
        reopened = analysis.reopen_analysis(review, aid)
        self.assertEqual(reopened["status"], "active")
        self.assertEqual(analysis.queue(review)[0]["kind"], "reopen")
        self.assertEqual(reopened["facts"]["f1"]["text"], "合成核查：统计数据覆盖全年")

    def test_refuting_a_direct_assumption_forks_and_only_reuses_unaffected_decisions(self):
        number, first, second, linked = self.reasoning()
        before = analysis.load(self.review, self.aid)
        changed = analysis.refute_assumption(self.review, self.aid, "a1")
        self.assertNotEqual(changed["line"], "main")
        self.assertEqual(changed["checkpoints"][-1]["snapshot"]["nodes"]["n1"]["decision"], first)
        self.assertFalse(changed["checkpoints"][-1]["snapshot"]["assumptions"]["a1"]["refuted"])
        self.assertTrue(changed["assumptions"]["a1"]["refuted"])
        self.assertEqual(changed["facts"], before["facts"])
        self.assertEqual(changed["lines"]["main"]["nodes"]["n1"]["decision"], first)
        self.assertIn("a1", changed["lines"]["main"]["basedOnRefuted"])
        self.assertIsNone(changed["nodes"]["n1"]["decision"])
        self.assertEqual(changed["nodes"]["n1"]["reopened"]["answer"], "全年")
        self.assertEqual(changed["nodes"]["n2"]["branch"], "detached")
        self.assertEqual(next(r for r in changed["rounds"] if r["seq"] == number)["status"], "void")
        self.assertEqual([n["status"] for n in analysis.linked_notes(self.review, self.aid)], ["held", "held"])
        refresh = next(r for r in changed["rounds"] if r["line"] == changed["line"] and r["status"] == "submitted")
        restored = analysis.answer(self.review, self.aid, refresh["seq"], "Q:n1", {"kind": "accept"})
        self.assertNotEqual(restored["nodes"]["n1"]["decision"], first)
        self.assertEqual(restored["nodes"]["n2"]["decision"], second)
        self.assertEqual(restored["nodes"]["n2"]["branch"], "current")
        statuses = {n["id"]: n["status"] for n in analysis.linked_notes(self.review, self.aid)}
        self.assertEqual((statuses[linked[0]["id"]], statuses[linked[1]["id"]]), ("held", "open"))
        old = analysis.switch_line(self.review, self.aid, "main")
        self.assertTrue(old["nodes"]["n1"]["needsReview"])
        self.assertEqual([n["status"] for n in analysis.linked_notes(self.review, self.aid)], ["held", "held"])
        self.assertEqual(analysis.load(self.review, self.aid), old)

    def test_refuting_without_a_current_direct_dependency_reissues_without_forking(self):
        _number, _first, _second, _linked = self.reasoning()
        changed = analysis.redecide(self.review, self.aid, "n1", "quarter")
        queued = next(r for r in changed["rounds"] if r["status"] == "submitted")
        before = analysis.load(self.review, self.aid)
        refuted = analysis.refute_assumption(self.review, self.aid, "a1")
        self.assertEqual(refuted["line"], before["line"])
        self.assertEqual(refuted["lines"].keys(), before["lines"].keys())
        self.assertEqual(refuted["checkpoints"], before["checkpoints"])
        self.assertTrue(refuted["assumptions"]["a1"]["refuted"])
        self.assertEqual(next(r for r in refuted["rounds"] if r["seq"] == queued["seq"])["status"], "void")
        self.assertEqual(len(analysis.queue(self.review)), 1)
        self.assertEqual(analysis.refute_assumption(self.review, self.aid, "a1"), refuted)

    def test_reconsidering_the_same_answer_on_the_old_line_creates_a_fresh_direct_decision(self):
        _number, first, second, linked = self.reasoning()
        analysis.refute_assumption(self.review, self.aid, "a1")
        analysis.switch_line(self.review, self.aid, "main")
        restored = analysis.redecide(self.review, self.aid, "n1", "year")
        self.assertNotEqual(restored["nodes"]["n1"]["decision"], first)
        self.assertFalse(restored["nodes"]["n1"]["needsReview"])
        self.assertEqual(restored["nodes"]["n2"]["decision"], second)
        statuses = {n["id"]: n["status"] for n in analysis.linked_notes(self.review, self.aid)}
        self.assertEqual((statuses[linked[0]["id"]], statuses[linked[1]["id"]]), ("held", "open"))

    def test_confirmed_option_dead_end_blocks_redecision_across_lines_until_refutation(self):
        number, _first, _second, _linked = self.reasoning()
        result = self.write(number, dead_ends=[{
            "key": "quarter-impossible", "kind": "option", "node": "n1", "option": "quarter",
            "reason": "合成原始材料缺少季度可比范围", "assumptions": ["a1"]}])
        pending = result["state"]
        end = next(iter(pending["deadEnds"].values()))
        self.assertEqual(end["status"], "pending")
        analysis.redecide(self.review, self.aid, "n1", "quarter")
        state = analysis.redecide(self.review, self.aid, "n1", "year")
        number = analysis.queue(self.review)[0]["seq"]
        confirmed = analysis.answer(self.review, self.aid, number, "D:" + end["id"], {"kind": "accept"})
        self.assertEqual(confirmed["deadEnds"][end["id"]]["status"], "confirmed")
        with self.assertRaises(notes.HandoffError) as blocked:
            analysis.redecide(self.review, self.aid, "n1", "quarter")
        self.assertEqual(blocked.exception.code, "dead-end-option")
        self.assertEqual(blocked.exception.params["reason"], "合成原始材料缺少季度可比范围")
        self.assertEqual(blocked.exception.params["assumptions"], ["合成原始材料的全年口径可比"])
        self.assertEqual(analysis.load(self.review, self.aid), confirmed)
        cp = analysis.seal_checkpoint(self.review, self.aid)["checkpoints"][-1]
        alternative = analysis.fork_line(self.review, self.aid, cp["id"])
        with self.assertRaises(notes.HandoffError) as across_lines:
            analysis.redecide(self.review, self.aid, "n1", "quarter")
        self.assertEqual(across_lines.exception.code, "dead-end-option")
        released = analysis.refute_assumption(self.review, self.aid, "a1")
        self.assertEqual(released["deadEnds"][end["id"]]["status"], "review")
        refresh = analysis.queue(self.review)[0]["seq"]
        allowed = analysis.answer(self.review, self.aid, refresh, "Q:n1", {"kind": "option", "option": "quarter"})
        self.assertEqual(allowed["decisions"][allowed["nodes"]["n1"]["decision"]]["option"], "quarter")
        self.assertEqual(set(allowed["deadEnds"]), {end["id"]})
        self.assertEqual(analysis.load(self.review, self.aid)["deadEnds"], allowed["deadEnds"])

    def test_confirmed_line_dead_end_blocks_switch_and_checkpoint_fork_until_refutation(self):
        _number, _first, _second, _linked = self.reasoning()
        checkpoint = analysis.seal_checkpoint(self.review, self.aid)["checkpoints"][-1]
        other = analysis.fork_line(self.review, self.aid, checkpoint["id"])
        number = analysis.queue(self.review)[0]["seq"]
        result = self.write(number, dead_ends=[{
            "key": "old-line", "kind": "line", "targetLine": "main",
            "reason": "合成旧思路需要重新审视", "assumptions": ["a1"]}])
        end = next(iter(result["state"]["deadEnds"].values()))
        confirmed = analysis.answer(self.review, self.aid, result["nextRound"], "D:" + end["id"], {"kind": "accept"})
        for action in (
            lambda: analysis.switch_line(self.review, self.aid, "main"),
            lambda: analysis.fork_line(self.review, self.aid, checkpoint["id"]),
        ):
            with self.assertRaises(notes.HandoffError) as blocked:
                action()
            self.assertEqual(blocked.exception.code, "dead-end-line")
            self.assertEqual(blocked.exception.params["reason"], "合成旧思路需要重新审视")
            self.assertEqual(analysis.load(self.review, self.aid), confirmed)
        released = analysis.refute_assumption(self.review, self.aid, "a1")
        self.assertEqual(released["deadEnds"][end["id"]]["status"], "review")
        self.assertEqual(analysis.switch_line(self.review, self.aid, "main")["line"], "main")
        forked = analysis.fork_line(self.review, self.aid, checkpoint["id"])
        self.assertNotEqual(forked["line"], "main")
        self.assertEqual(set(forked["deadEnds"]), {end["id"]})

    def test_round_answers_cannot_choose_a_dead_option_by_recommendation_option_or_same_text(self):
        number, _first, _second, _linked = self.reasoning()
        proposed = question("pending")
        proposed["recommend"] = "quarter"
        number = self.write(number, [proposed])["nextRound"]
        analysis.answer(self.review, self.aid, number, "S:pending", {"kind": "accept"})
        result = self.write(number, dead_ends=[{
            "key": "pending-quarter", "kind": "option", "node": "n3", "option": "quarter",
            "reason": "合成季度路径不成立", "assumptions": ["a1"]}])
        end = next(iter(result["state"]["deadEnds"].values()))
        number = result["nextRound"]
        before = analysis.answer(self.review, self.aid, number, "D:" + end["id"], {"kind": "accept"})
        for value in ({"kind": "accept"}, {"kind": "option", "option": "quarter"}, {"kind": "text", "text": "季度"}):
            with self.assertRaises(notes.HandoffError) as blocked:
                analysis.answer(self.review, self.aid, number, "Q:n3", value)
            self.assertEqual(blocked.exception.code, "dead-end-option")
            self.assertEqual(analysis.load(self.review, self.aid), before)
        parked = analysis.answer(self.review, self.aid, number, "Q:n3", {"kind": "park", "option": "quarter"})
        self.assertEqual(parked["nodes"]["n3"]["status"], "parked")
        verifying = analysis.answer(self.review, self.aid, number, "Q:n3", {"kind": "verify", "option": "quarter"})
        self.assertEqual(verifying["nodes"]["n3"]["status"], "verifying")
        allowed = analysis.answer(self.review, self.aid, number, "Q:n3", {"kind": "option", "option": "year"})
        self.assertEqual(allowed["decisions"][allowed["nodes"]["n3"]["decision"]]["option"], "year")

    def test_a_claim_on_any_line_blocks_refutation(self):
        number, _first, _second, linked = self.reasoning()
        number = self.write(number, [question("open")])["nextRound"]
        analysis.answer(self.review, self.aid, number, "S:open", {"kind": "accept"})
        analysis.redecide(self.review, self.aid, "n1", "quarter")
        checkpoint = analysis.seal_checkpoint(self.review, self.aid)["checkpoints"][-1]
        forked = analysis.fork_line(self.review, self.aid, checkpoint["id"])
        number = analysis.queue(self.review)[0]["seq"]
        analysis.switch_line(self.review, self.aid, "main")
        claimed = analysis.claim(self.review, seq=number)
        before = analysis.load(self.review, self.aid)
        for action in (
            lambda: analysis.refute_assumption(self.review, self.aid, "a1"),
            lambda: analysis.redecide(self.review, self.aid, "n1", "year"),
            lambda: analysis.seal_checkpoint(self.review, self.aid),
            lambda: analysis.fork_line(self.review, self.aid, checkpoint["id"]),
            lambda: analysis.switch_line(self.review, self.aid, forked["line"]),
            lambda: analysis.request_update(self.review, self.aid, linked[0]["id"]),
            lambda: analysis.close_analysis(self.review, self.aid),
        ):
            with self.assertRaises(notes.HandoffError) as blocked:
                action()
            self.assertEqual(blocked.exception.code, "analysis-locked")
            self.assertEqual(analysis.load(self.review, self.aid), before)
        answer = {"kind": "text", "text": "合成处理期间原话"}
        answered = analysis.answer(self.review, self.aid, number, "Q:n3", answer)
        self.assertEqual(answered["rev"], claimed["baseRev"])
        self.assertIsNone(answered["nodes"]["n3"]["decision"])
        self.assertIsNotNone(answered["lines"][forked["line"]]["nodes"]["n3"]["decision"])
        analysis.writeback(self.review, self.aid, number, claimed["baseRev"], [])
        with self.assertRaises(notes.HandoffError) as old_page:
            analysis.answer(self.review, self.aid, number, "Q:n3", {"kind": "accept"})
        self.assertEqual(old_page.exception.code, "round-not-answerable")
        changed = analysis.refute_assumption(self.review, self.aid, "a1")
        self.assertTrue(changed["assumptions"]["a1"]["refuted"])

    def test_dead_end_confirmation_during_claim_preserves_baseline_and_history(self):
        number, _first, _second, _linked = self.reasoning()
        result = self.write(number, dead_ends=[{
            "key": "quarter", "kind": "option", "node": "n1", "option": "quarter",
            "reason": "合成季度路径不可用", "assumptions": ["a1"]}])
        number = result["nextRound"]
        analysis.submit_round(self.review, self.aid, number)
        claimed = analysis.claim(self.review, seq=number)
        state = analysis.answer(self.review, self.aid, number, "D:e1", {"kind": "accept", "verbatim": "处理期间确认死局"})
        self.assertEqual(state["rev"], claimed["baseRev"])
        result = analysis.writeback(self.review, self.aid, number, claimed["baseRev"], [])
        self.assertIsNone(result["nextRound"])
        context = analysis.round_context(self.review, self.aid, number)
        self.assertTrue(context["historical"])
        self.assertEqual(context["answers"]["D:e1"]["verbatim"], "处理期间确认死局")
        self.assertEqual(context["answeredItems"][0]["reason"], "合成季度路径不可用")

    def test_rejected_dead_end_does_not_block_and_repeated_writeback_keeps_identity(self):
        number, _first, _second, _linked = self.reasoning()
        analysis.submit_round(self.review, self.aid, number)
        claimed = analysis.claim(self.review, seq=number)
        before = analysis.load(self.review, self.aid)
        for proposal in ({"key": "invalid", "kind": "option", "node": "n1", "option": "quarter",
                          "assumptions": ["a1"]},
                         {"key": "invalid", "kind": "option", "node": "n1", "option": "quarter",
                          "reason": "合成理由", "assumptions": ["unknown"]}):
            with self.assertRaises(notes.HandoffError) as invalid:
                analysis.writeback(self.review, self.aid, number, claimed["baseRev"], [], dead_ends=[proposal])
            self.assertEqual(invalid.exception.code, "bad-dead-ends")
            self.assertEqual(analysis.load(self.review, self.aid), before)
        proposal = {"key": "quarter", "kind": "option", "node": "n1", "option": "quarter",
                    "reason": "合成提议理由", "assumptions": ["a1"]}
        result = analysis.writeback(self.review, self.aid, number, claimed["baseRev"], [], dead_ends=[proposal])
        number = result["nextRound"]
        rejected = analysis.answer(self.review, self.aid, number, "D:e1", {"kind": "reject"})
        self.assertEqual(rejected["deadEnds"]["e1"]["status"], "rejected")
        state = analysis.redecide(self.review, self.aid, "n1", "quarter")
        self.assertEqual(state["decisions"][state["nodes"]["n1"]["decision"]]["option"], "quarter")
        result = self.write(analysis.queue(self.review)[0]["seq"], dead_ends=[proposal])
        self.assertEqual(set(result["state"]["deadEnds"]), {"e1"})
        self.assertEqual(result["state"]["deadEnds"]["e1"]["status"], "rejected")

    def test_next_round_order_and_carry_preserve_all_valid_answers_once(self):
        number, _first, _second, linked = self.reasoning()
        number = self.write(number, [question("frontier")])["nextRound"]
        analysis.answer(self.review, self.aid, number, "S:frontier", {"kind": "accept"})
        state = analysis.redecide(self.review, self.aid, "n1", "quarter")
        decision = state["nodes"]["n1"]["decision"]
        analysis.request_update(self.review, self.aid, linked[0]["id"])
        number = analysis.queue(self.review)[0]["seq"]
        result = self.write(number, [question("later")],
                            updates=[{"noteId": linked[0]["id"], "text": "合成更新原话"}],
                            assumptions=[{"key": "new-source", "text": "合成新口径假设", "decisions": [decision]}],
                            dead_ends=[{"key": "comparison-quarter", "kind": "option", "node": "n2",
                                        "option": "quarter", "reason": "合成不可比路径", "assumptions": ["a1"]}])
        number = result["nextRound"]
        context = analysis.round_context(self.review, self.aid, number)
        self.assertEqual([item["type"] for item in context["items"]],
                         ["deadend", "update", "assumption", "structure", "question"])
        answers = {"D:e1": {"kind": "accept", "verbatim": "死局确认原话"},
                   "U:u1": {"kind": "accept", "verbatim": "新文本确认原话"},
                   "A:a2": {"kind": "accept", "verbatim": "假设确认原话"},
                   "S:later": {"kind": "accept", "verbatim": "结构确认原话"},
                   "Q:n3": {"kind": "text", "text": "合成保留原话", "verbatim": "问题回答原话"}}
        for key, value in answers.items():
            analysis.answer(self.review, self.aid, number, key, value)
        analysis.answer(self.review, self.aid, number, "Q:n4", {"kind": "accept"})
        state = analysis.redecide(self.review, self.aid, "n4", "quarter")
        refreshed = analysis.queue(self.review)[0]["seq"]
        context = analysis.round_context(self.review, self.aid, refreshed)
        for key, value in answers.items():
            self.assertEqual(context["answers"].get(key), value, key)
        self.assertNotIn("Q:n4", context["answers"])
        self.assertEqual([item["type"] for item in context["items"]],
                         sorted([item["type"] for item in context["items"]],
                                key={"deadend": 0, "update": 1, "assumption": 2, "structure": 3, "question": 4}.get))
        counts = (len(state["nodes"]), len(state["decisions"]), len(state["deadEnds"]), len(state["updates"]))
        claimed = analysis.claim(self.review, seq=refreshed)
        with self.assertRaises(notes.HandoffError) as stale:
            analysis.writeback(self.review, self.aid, refreshed, claimed["baseRev"] + 1, [])
        self.assertEqual(stale.exception.code, "base-rev-mismatch")
        refreshed = analysis.queue(self.review)[0]["seq"]
        for key, value in answers.items():
            self.assertEqual(analysis.round_context(self.review, self.aid, refreshed)["answers"].get(key), value, key)
        result = self.write(refreshed)
        self.assertIsNotNone(result["nextRound"])
        final = self.write(result["nextRound"])
        self.assertIsNone(final["nextRound"])
        state = final["state"]
        self.assertEqual((len(state["nodes"]), len(state["decisions"]), len(state["deadEnds"]), len(state["updates"])), counts)
        note = next(n for n in analysis.linked_notes(self.review, self.aid) if n["id"] == linked[0]["id"])
        self.assertEqual((note["text"], len(note["analysisHistory"])), ("合成更新原话", 1))


if __name__ == "__main__":
    unittest.main()
