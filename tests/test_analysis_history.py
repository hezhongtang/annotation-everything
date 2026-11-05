"""#114 公开行为：检查点、分析线、历史回看与结案。"""
from __future__ import annotations

import json
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


class AnalysisHistoryTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="ae-analysis-history-")
        self.addCleanup(temporary.cleanup)
        self.review = Path(temporary.name)
        env = patch.dict(os.environ, {"AE_HOME": str(self.review / "ledger")})
        env.start()
        self.addCleanup(env.stop)
        state, self.opening = analysis.start_question(self.review, "合成材料的增长结论是否成立？")
        self.aid = state["id"]

    def write(self, round_no, proposals=(), **kwargs):
        analysis.submit_round(self.review, self.aid, round_no)
        claimed = analysis.claim(self.review, seq=round_no)
        return analysis.writeback(self.review, self.aid, round_no, claimed["baseRev"], list(proposals), **kwargs)

    def open_questions(self, count=2):
        result = self.write(self.opening, [question(f"q{i}") for i in range(count)])
        number = result["nextRound"]
        for i in range(count):
            analysis.answer(self.review, self.aid, number, f"S:q{i}", {"kind": "accept"})
        return number

    def test_every_writeback_records_a_stable_complete_checkpoint(self):
        number = self.open_questions()
        analysis.answer(self.review, self.aid, number, "Q:n1", {"kind": "accept", "verbatim": "全年原话"})
        analysis.answer(self.review, self.aid, number, "Q:n2", {"kind": "park"})
        result = self.write(number, note="这是保留全文的合成分析说明。")
        state = result["state"]
        self.assertEqual(len(state["checkpoints"]), 2)
        checkpoint = state["checkpoints"][-1]
        self.assertEqual(checkpoint["round"], number)
        self.assertTrue(checkpoint["auto"])
        self.assertFalse(checkpoint["sealed"])
        snap = checkpoint["snapshot"]
        did = state["nodes"]["n1"]["decision"]
        self.assertEqual(snap["conclusions"], [did])
        self.assertEqual(snap["decisions"][did]["round"], number)
        self.assertEqual(snap["parked"], ["n2"])
        self.assertEqual(snap["nodes"]["n2"]["text"], "合成材料应采用哪个统计口径？")
        self.assertEqual(analysis.load(self.review, self.aid)["checkpoints"][-1], checkpoint)

    def test_sealing_preserves_the_snapshot_and_identity_after_later_answers(self):
        number = self.open_questions()
        state = analysis.answer(self.review, self.aid, number, "Q:n1", {"kind": "accept"})
        original = state["nodes"]["n1"]["decision"]
        sealed = analysis.seal_checkpoint(self.review, self.aid)["checkpoints"][-1]
        self.assertTrue(sealed["sealed"])
        self.assertEqual(sealed["snapshot"]["nodes"]["n1"]["decision"], original)
        self.assertEqual(sealed["snapshot"]["unresolved"], ["n2"])
        analysis.redecide(self.review, self.aid, "n1", "quarter")
        after = analysis.seal_checkpoint(self.review, self.aid, sealed["id"])
        saved = next(c for c in after["checkpoints"] if c["id"] == sealed["id"])
        self.assertEqual(saved, sealed)
        self.assertNotEqual(after["nodes"]["n1"]["decision"], original)

    def test_fork_inherits_ids_and_switches_restore_latest_state_and_note_basis(self):
        number = self.open_questions()
        before = analysis.answer(self.review, self.aid, number, "Q:n1", {"kind": "accept"})
        first_decision = before["nodes"]["n1"]["decision"]
        first_draft = analysis.convert(self.review, self.aid, "n1", text="封存前已有的结论")
        first_note = analysis.confirm_convert(self.review, self.aid, first_draft["id"])
        checkpoint = analysis.seal_checkpoint(self.review, self.aid)["checkpoints"][-1]
        before = analysis.answer(self.review, self.aid, number, "Q:n2", {"kind": "accept"})
        second_decision = before["nodes"]["n2"]["decision"]
        second_draft = analysis.convert(self.review, self.aid, "n2", text="封存后才得到的结论")
        second_note = analysis.confirm_convert(self.review, self.aid, second_draft["id"])
        forked = analysis.fork_line(self.review, self.aid, checkpoint["id"])
        other = forked["line"]
        self.assertNotEqual(other, "main")
        self.assertEqual(forked["nodes"]["n1"]["decision"], first_decision)
        self.assertIsNone(forked["nodes"]["n2"]["decision"])
        statuses = {n["id"]: n["status"] for n in notes.load(self.review)["notes"]}
        self.assertEqual(statuses[first_note["id"]], "open")
        self.assertEqual(statuses[second_note["id"]], "held")
        fork_round = next(r["seq"] for r in forked["rounds"] if r["line"] == other and r["status"] == "submitted")
        alternative = analysis.answer(self.review, self.aid, fork_round, "Q:n2",
                                      {"kind": "option", "option": "quarter"})
        alternative_decision = alternative["nodes"]["n2"]["decision"]
        restored = analysis.switch_line(self.review, self.aid, "main")
        self.assertEqual(restored["nodes"]["n2"]["decision"], second_decision)
        self.assertEqual(next(n for n in notes.load(self.review)["notes"] if n["id"] == second_note["id"])["status"], "open")
        latest = analysis.switch_line(self.review, self.aid, other)
        self.assertEqual(latest["nodes"]["n2"]["decision"], alternative_decision)
        self.assertEqual(next(c for c in latest["checkpoints"] if c["id"] == checkpoint["id"]), checkpoint)

    def test_queued_round_uses_its_own_line_while_another_line_is_displayed(self):
        number = self.open_questions()
        analysis.answer(self.review, self.aid, number, "Q:n1", {"kind": "accept"})
        checkpoint = analysis.seal_checkpoint(self.review, self.aid)["checkpoints"][-1]
        alternative = analysis.fork_line(self.review, self.aid, checkpoint["id"])
        other = alternative["line"]
        alternative = analysis.redecide(self.review, self.aid, "n1", "quarter")
        number = next(r["seq"] for r in alternative["rounds"] if r["line"] == other and r["status"] == "submitted")
        analysis.switch_line(self.review, self.aid, "main")
        summary = next(row for row in analysis.list_analyses(self.review) if row["id"] == self.aid)
        self.assertIsNone(summary["openRound"])
        claimed = analysis.claim(self.review, seq=number)
        context = analysis.round_context(self.review, self.aid, number)
        self.assertEqual(context["line"], other)
        self.assertEqual(context["decisions"][0]["label"], "季度")
        before_rev = analysis.load(self.review, self.aid)["rev"]
        state = analysis.answer(self.review, self.aid, number, "Q:n2", {"kind": "text", "text": "新线独有原话"})
        self.assertEqual(state["line"], "main")
        self.assertIsNone(state["nodes"]["n2"]["decision"])
        self.assertEqual(state["rev"], before_rev)
        result = analysis.writeback(self.review, self.aid, number, claimed["baseRev"], [])
        self.assertIsNone(result["nextRound"])
        checkpoint = result["state"]["checkpoints"][-1]
        self.assertEqual(checkpoint["line"], other)
        self.assertEqual(checkpoint["snapshot"]["nodes"]["n1"]["options"]["quarter"], "季度")
        self.assertEqual(checkpoint["snapshot"]["nodes"]["n2"]["status"], "decided")
        latest = analysis.switch_line(self.review, self.aid, other)
        self.assertEqual(latest["decisions"][latest["nodes"]["n2"]["decision"]]["label"], "新线独有原话")

    def test_close_requires_no_queued_work_seals_and_reopen_requests_questions(self):
        with self.assertRaises(notes.HandoffError) as pending:
            analysis.close_analysis(self.review, self.aid)
        self.assertEqual(pending.exception.code, "analysis-pending-rounds")
        number = self.open_questions(1)
        analysis.answer(self.review, self.aid, number, "Q:n1", {"kind": "accept"})
        closed = analysis.close_analysis(self.review, self.aid)
        self.assertEqual(closed["status"], "closed")
        self.assertTrue(closed["checkpoints"][-1]["sealed"])
        self.assertTrue(all(r["status"] not in ("open", "submitted", "claimed") for r in closed["rounds"]))
        for operation in (
            lambda: analysis.redecide(self.review, self.aid, "n1", "quarter"),
            lambda: analysis.convert(self.review, self.aid, "n1"),
            lambda: analysis.answer(self.review, self.aid, number, "Q:n1", {"kind": "accept"}),
        ):
            with self.assertRaises(notes.HandoffError) as refused:
                operation()
            self.assertEqual(refused.exception.code, "analysis-closed")
        reopened = analysis.reopen_analysis(self.review, self.aid)
        self.assertEqual(reopened["status"], "active")
        waiting = [r for r in reopened["rounds"] if r["status"] == "submitted"]
        self.assertEqual(len(waiting), 1)
        self.assertEqual(waiting[0]["line"], reopened["line"])
        self.assertEqual(waiting[0]["kind"], "reopen")
        self.assertEqual(len(analysis.reopen_analysis(self.review, self.aid)["rounds"]), len(reopened["rounds"]))

    def test_history_keeps_original_questions_answers_and_agent_note_without_changing_line(self):
        number = self.open_questions()
        analysis.answer(self.review, self.aid, number, "Q:n1", {"kind": "accept", "verbatim": "保留全年原话"})
        result = self.write(number, note="本轮 Agent 原文\n包含完整第二行。")
        original = analysis.round_context(self.review, self.aid, number)
        self.assertEqual(original["note"], "本轮 Agent 原文\n包含完整第二行。")
        self.assertEqual(original["decisions"][0]["label"], "全年")
        next_round = result["nextRound"]
        analysis.answer(self.review, self.aid, next_round, "Q:n2", {"kind": "text", "text": "以后才追加的选项"})
        analysis.redecide(self.review, self.aid, "n1", "quarter")
        before = analysis.load(self.review, self.aid)
        historical = analysis.round_context(self.review, self.aid, number)
        self.assertEqual(historical, original)
        self.assertEqual(historical["answers"]["Q:n1"]["verbatim"], "保留全年原话")
        self.assertNotIn("以后才追加的选项", json.dumps(historical, ensure_ascii=False))
        self.assertEqual(analysis.load(self.review, self.aid), before)

    def test_a_claim_on_another_line_locks_all_history_operations_but_not_answers(self):
        number = self.open_questions()
        analysis.answer(self.review, self.aid, number, "Q:n1", {"kind": "accept"})
        checkpoint = analysis.seal_checkpoint(self.review, self.aid)["checkpoints"][-1]
        other = analysis.fork_line(self.review, self.aid, checkpoint["id"])["line"]
        analysis.switch_line(self.review, self.aid, "main")
        claimed = analysis.claim(self.review)
        self.assertEqual(claimed["line"], other)
        for operation in (
            lambda: analysis.seal_checkpoint(self.review, self.aid),
            lambda: analysis.fork_line(self.review, self.aid, checkpoint["id"]),
            lambda: analysis.switch_line(self.review, self.aid, other),
            lambda: analysis.close_analysis(self.review, self.aid),
        ):
            with self.assertRaises(notes.HandoffError) as refused:
                operation()
            self.assertEqual(refused.exception.code, "analysis-locked")
        state = analysis.answer(self.review, self.aid, claimed["seq"], "Q:n2", {"kind": "accept"})
        self.assertEqual(state["rev"], claimed["baseRev"])
        with self.assertRaises(notes.HandoffError) as old:
            analysis.answer(self.review, self.aid, self.opening, "S:q0", {"kind": "accept"})
        self.assertEqual(old.exception.code, "round-not-answerable")

    def test_inherited_assumption_confirmation_is_per_line_and_facts_are_shared(self):
        number = self.open_questions()
        state = analysis.answer(self.review, self.aid, number, "Q:n1", {"kind": "accept"})
        did = state["nodes"]["n1"]["decision"]
        number = self.write(number, assumptions=[{"key": "scope", "text": "合成期间范围相同", "decisions": [did]}])["nextRound"]
        checkpoint = analysis.seal_checkpoint(self.review, self.aid)["checkpoints"][-1]
        alternative = analysis.fork_line(self.review, self.aid, checkpoint["id"])
        other = alternative["line"]
        number = next(r["seq"] for r in alternative["rounds"] if r["line"] == other and r["status"] == "submitted")
        analysis.answer(self.review, self.aid, number, "A:a1", {"kind": "accept"})
        self.write(number, facts=[{"key": "source", "text": "合成底稿确认120", "source": "合成底稿"}])
        main = analysis.switch_line(self.review, self.aid, "main")
        self.assertEqual(main["assumptions"]["a1"]["confirmed"], {"main": "pending", other: "confirmed"})
        self.assertEqual(main["facts"]["f1"]["text"], "合成底稿确认120")
        self.assertEqual(next(c for c in main["checkpoints"] if c["id"] == checkpoint["id"]), checkpoint)
        self.assertEqual(main["nodes"]["n1"]["decision"], did)

    def test_new_free_text_options_have_one_stable_identity_across_lines(self):
        number = self.open_questions(1)
        checkpoint = analysis.seal_checkpoint(self.review, self.aid)["checkpoints"][-1]
        forked = analysis.fork_line(self.review, self.aid, checkpoint["id"])
        other = forked["line"]
        number = next(r["seq"] for r in forked["rounds"] if r["status"] == "submitted")
        first = analysis.answer(self.review, self.aid, number, "Q:n1", {"kind": "text", "text": "新线的自选口径"})
        first_option = first["decisions"][first["nodes"]["n1"]["decision"]]["option"]
        analysis.switch_line(self.review, self.aid, "main")
        # 主线原轮已被分线作废；从原检查点再开一条独立线建立另一次自由回答。
        second = analysis.fork_line(self.review, self.aid, checkpoint["id"])
        number = next(r["seq"] for r in reversed(second["rounds"]) if r["status"] == "submitted")
        second = analysis.answer(self.review, self.aid, number, "Q:n1", {"kind": "text", "text": "另一条线的口径"})
        second_option = second["decisions"][second["nodes"]["n1"]["decision"]]["option"]
        self.assertNotEqual(first_option, second_option)
        restored = analysis.switch_line(self.review, self.aid, other)
        self.assertEqual(restored["nodes"]["n1"]["options"][first_option], "新线的自选口径")

    def test_fork_keeps_unconfirmed_structure_proposals_without_creating_nodes(self):
        result = self.write(self.opening, [question("pending")])
        checkpoint = result["state"]["checkpoints"][-1]
        self.assertEqual(result["state"]["nodes"], {})
        forked = analysis.fork_line(self.review, self.aid, checkpoint["id"])
        number = next(r["seq"] for r in forked["rounds"] if r["line"] == forked["line"] and r["status"] == "submitted")
        context = analysis.round_context(self.review, self.aid, number)
        self.assertEqual([item["key"] for item in context["items"]], ["S:pending"])
        self.assertEqual(forked["nodes"], {})
        accepted = analysis.answer(self.review, self.aid, number, "S:pending", {"kind": "accept"})
        self.assertEqual(len(accepted["nodes"]), 1)
        self.assertEqual(analysis.switch_line(self.review, self.aid, "main")["nodes"], {})

    def test_answering_the_same_option_on_a_fork_reuses_the_global_decision_and_note(self):
        number = self.open_questions(1)
        checkpoint = analysis.seal_checkpoint(self.review, self.aid)["checkpoints"][-1]
        original = analysis.answer(self.review, self.aid, number, "Q:n1", {"kind": "accept"})
        did = original["nodes"]["n1"]["decision"]
        draft = analysis.convert(self.review, self.aid, "n1", text="同一统计口径下的合成修改意见")
        note = analysis.confirm_convert(self.review, self.aid, draft["id"])
        forked = analysis.fork_line(self.review, self.aid, checkpoint["id"])
        self.assertIsNone(forked["nodes"]["n1"]["decision"])
        self.assertEqual(next(n for n in notes.load(self.review)["notes"] if n["id"] == note["id"])["status"], "held")
        number = next(r["seq"] for r in forked["rounds"] if r["line"] == forked["line"] and r["status"] == "submitted")
        answered = analysis.answer(self.review, self.aid, number, "Q:n1", {"kind": "accept"})
        self.assertEqual(answered["nodes"]["n1"]["decision"], did)
        self.assertEqual(len(answered["decisions"]), 1)
        self.assertEqual(next(n for n in notes.load(self.review)["notes"] if n["id"] == note["id"])["status"], "open")
