"""#113 的公开行为：分析锁、重裁与断枝、基线恢复和结论批注保护。"""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "skill/annotation-everything/scripts"
sys.path.insert(0, str(SCRIPTS))
sys.path.insert(0, str(ROOT))

import host_isolation
host_isolation.require_for_module(__name__)

import analysis_store as analysis
import notes_store as notes


def proposal(key="caliber", *, parent=None):
    return {"key": key, "text": "按哪种口径核对增长？", "short": "增长口径",
            "options": [{"id": "Y", "label": "年度"}, {"id": "Q", "label": "季度"}],
            "recommend": "Y", "parent": parent}


class ProtectionTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="ae-analysis-protect-")
        self.addCleanup(temporary.cleanup)
        self.review = Path(temporary.name)
        _doc, self.source = notes.add(self.review, {"page": 1, "text": "增长结论站得住吗？"})
        self.state, self.opening = analysis.start(self.review, self.source["id"])
        self.aid = self.state["id"]

    def questions(self, proposals=None):
        claimed = analysis.claim(self.review, by="Synthetic")
        result = analysis.writeback(self.review, self.aid, claimed["seq"], claimed["baseRev"],
                                    proposals if proposals is not None else [proposal()])
        return result["nextRound"]

    def branch_note(self):
        round_no = self.questions()
        analysis.answer(self.review, self.aid, round_no, "S:caliber", {"kind": "accept"})
        analysis.answer(self.review, self.aid, round_no, "Q:n1", {"kind": "accept"})
        analysis.submit_round(self.review, self.aid, round_no)
        child = proposal("comparison", parent={"node": "n1", "option": "Y"})
        child.update(text="和哪一期作比较？", short="对比期间")
        round_no = self.questions([child])
        analysis.answer(self.review, self.aid, round_no, "S:comparison", {"kind": "accept"})
        analysis.answer(self.review, self.aid, round_no, "Q:n2", {"kind": "accept"})
        draft = analysis.convert(self.review, self.aid, "n2", text="按年度同期口径说明增长")
        note = analysis.confirm_convert(self.review, self.aid, draft["id"])
        return round_no, note

    def test_claimed_round_accepts_answers_without_changing_baseline(self):
        round_no = self.questions()
        analysis.submit_round(self.review, self.aid, round_no)
        claimed = analysis.claim(self.review)
        analysis.answer(self.review, self.aid, round_no, "S:caliber", {"kind": "accept"})
        current = analysis.answer(self.review, self.aid, round_no, "Q:n1",
                                  {"kind": "option", "option": "Y", "verbatim": "沿用年度口径"})
        self.assertEqual(current["rev"], claimed["baseRev"])
        self.assertEqual(current["rounds"][-1]["answers"]["Q:n1"]["verbatim"], "沿用年度口径")
        analysis.writeback(self.review, self.aid, round_no, claimed["baseRev"], [])
        with self.assertRaises(notes.HandoffError) as refused:
            analysis.answer(self.review, self.aid, round_no, "Q:n1", {"kind": "accept"})
        self.assertEqual(refused.exception.code, "round-not-answerable")

    def test_claimed_round_locks_its_whole_analysis_and_unlocks_after_writeback(self):
        round_no = self.questions()
        analysis.answer(self.review, self.aid, round_no, "S:caliber", {"kind": "accept"})
        analysis.answer(self.review, self.aid, round_no, "Q:n1", {"kind": "accept"})
        analysis.submit_round(self.review, self.aid, round_no)
        claimed = analysis.claim(self.review)
        with self.assertRaises(notes.HandoffError) as locked:
            analysis.redecide(self.review, self.aid, "n1", "Q")
        self.assertEqual(locked.exception.code, "analysis-locked")
        self.assertTrue(analysis.load(self.review, self.aid)["protection"]["locked"])
        analysis.writeback(self.review, self.aid, round_no, claimed["baseRev"], [])
        changed = analysis.redecide(self.review, self.aid, "n1", "Q")
        self.assertFalse(changed["protection"]["locked"])
        self.assertEqual(changed["decisions"][changed["nodes"]["n1"]["decision"]]["option"], "Q")

    def test_redecide_detaches_and_revives_the_same_decisions_and_note(self):
        round_no, note = self.branch_note()
        original = analysis.load(self.review, self.aid)
        original_decision = original["nodes"]["n2"]["decision"]
        notes.handoff_submit(self.review)
        changed = analysis.redecide(self.review, self.aid, "n1", "Q")
        self.assertEqual(changed["nodes"]["n2"]["branch"], "detached")
        self.assertEqual(changed["nodes"]["n2"]["decision"], original_decision)
        held = next(n for n in notes.load(self.review)["notes"] if n["id"] == note["id"])
        self.assertEqual((held["status"], held["hold"], held["text"]),
                         ("held", "结论已变", note["text"]))
        self.assertEqual(notes.handoff_load(self.review)["items"][-1]["status"], "void")
        with self.assertRaises(notes.HandoffError) as old_round:
            analysis.answer(self.review, self.aid, round_no, "Q:n2", {"kind": "accept"})
        self.assertEqual(old_round.exception.code, "round-not-answerable")
        restored = analysis.redecide(self.review, self.aid, "n1", "Y")
        self.assertEqual(restored["nodes"]["n2"]["branch"], "current")
        self.assertEqual(restored["nodes"]["n2"]["decision"], original_decision)
        reopened = next(n for n in notes.load(self.review)["notes"] if n["id"] == note["id"])
        self.assertEqual((reopened["status"], reopened["text"]), ("open", note["text"]))
        self.assertEqual(len(analysis.queue(self.review)), 1)

    def test_rejected_writeback_voids_and_reissues_once(self):
        claimed = analysis.claim(self.review)
        with self.assertRaises(notes.HandoffError) as rejected:
            analysis.writeback(self.review, self.aid, claimed["seq"], claimed["baseRev"] + 1, [])
        self.assertEqual(rejected.exception.code, "base-rev-mismatch")
        current = analysis.load(self.review, self.aid)
        self.assertEqual(current["rounds"][0]["status"], "void")
        self.assertEqual(len(current["rejections"]), 1)
        self.assertFalse(current["protection"]["locked"])
        self.assertEqual([(r["kind"], r["status"], r["line"]) for r in current["rounds"][1:]],
                         [("refresh", "submitted", "main")])
        with self.assertRaises(notes.HandoffError):
            analysis.writeback(self.review, self.aid, claimed["seq"], claimed["baseRev"], [])
        self.assertEqual(len(analysis.queue(self.review)), 1)

    def test_void_round_carries_only_valid_answers_and_uses_latest_claimed_answer(self):
        other = proposal("comparison")
        other.update(text="与哪期相比？", short="对比期间")
        round_no = self.questions([proposal(), other])
        for key in ("caliber", "comparison"):
            analysis.answer(self.review, self.aid, round_no, "S:" + key, {"kind": "accept"})
        analysis.answer(self.review, self.aid, round_no, "Q:n1", {"kind": "accept"})
        original = {"kind": "option", "option": "Y", "verbatim": "另一题先按年度 ☀"}
        analysis.answer(self.review, self.aid, round_no, "Q:n2", original)
        changed = analysis.redecide(self.review, self.aid, "n1", "Q")
        refreshed = changed["rounds"][-1]
        self.assertEqual(refreshed["answers"]["Q:n2"], original)
        self.assertNotIn("Q:n1", refreshed["answers"])
        claimed = analysis.claim(self.review)
        latest = {"kind": "option", "option": "Q", "verbatim": "接手期间补答，最终改用季度"}
        analysis.answer(self.review, self.aid, refreshed["seq"], "Q:n2", latest)
        result = analysis.writeback(self.review, self.aid, claimed["seq"], claimed["baseRev"], [])
        context = analysis.round_context(self.review, self.aid, result["nextRound"])
        self.assertEqual(context["answers"]["Q:n2"], latest)
        analysis.submit_round(self.review, self.aid, result["nextRound"])
        final_claim = analysis.claim(self.review)
        finished = analysis.writeback(self.review, self.aid, final_claim["seq"], final_claim["baseRev"], [])
        self.assertIsNone(finished["nextRound"], "回答带入不应造成永远重复的下一轮")

    def test_update_is_drafted_in_a_round_and_only_confirmation_replaces_text(self):
        round_no = self.questions()
        analysis.answer(self.review, self.aid, round_no, "S:caliber", {"kind": "accept"})
        analysis.answer(self.review, self.aid, round_no, "Q:n1", {"kind": "accept"})
        draft = analysis.convert(self.review, self.aid, "n1", text="原批注：按年度说明增长")
        note = analysis.confirm_convert(self.review, self.aid, draft["id"])
        analysis.redecide(self.review, self.aid, "n1", "Q")
        requested = analysis.request_update(self.review, self.aid, note["id"])
        self.assertEqual(requested["updateRequests"][note["id"]]["status"], "requested")
        claimed = analysis.claim(self.review)
        text = "新批注：按季度说明增长，先说明口径变化"
        result = analysis.writeback(self.review, self.aid, claimed["seq"], claimed["baseRev"], [],
                                    updates=[{"noteId": note["id"], "text": text}])
        context = analysis.round_context(self.review, self.aid, result["nextRound"])
        item = next(item for item in context["items"] if item["type"] == "update")
        self.assertEqual((item["beforeText"], item["text"]), (note["text"], text))
        before = next(n for n in notes.load(self.review)["notes"] if n["id"] == note["id"])
        self.assertEqual((before["text"], before["status"]), (note["text"], "held"))
        analysis.answer(self.review, self.aid, result["nextRound"], item["key"], {"kind": "accept"})
        confirmed = next(n for n in notes.load(self.review)["notes"] if n["id"] == note["id"])
        self.assertEqual((confirmed["text"], confirmed["status"]), (text, "open"))
        self.assertEqual(confirmed["basis"]["decision"], analysis.load(self.review, self.aid)["nodes"]["n1"]["decision"])
        self.assertEqual(confirmed["analysisHistory"][-1]["text"], note["text"])
        analysis.answer(self.review, self.aid, result["nextRound"], item["key"], {"kind": "accept"})
        retried = next(n for n in notes.load(self.review)["notes"] if n["id"] == note["id"])
        self.assertEqual(retried, confirmed, "确认重传不能再次替换文本或重复留旧文")

    def test_keep_stops_tracking_and_delete_removes_only_the_selected_note(self):
        _round_no, note = self.branch_note()
        analysis.redecide(self.review, self.aid, "n1", "Q")
        kept = analysis.keep_note(self.review, self.aid, note["id"])
        self.assertEqual((kept["text"], kept["status"], kept["tracking"]), (note["text"], "open", False))
        analysis.redecide(self.review, self.aid, "n1", "Y")
        draft = analysis.convert(self.review, self.aid, "n2", text="另一条按年度同期的修改意见")
        other = analysis.confirm_convert(self.review, self.aid, draft["id"])
        analysis.redecide(self.review, self.aid, "n1", "Q")
        analysis.delete_note(self.review, self.aid, other["id"])
        current = {n["id"]: n for n in notes.load(self.review)["notes"]}
        self.assertNotIn(other["id"], current)
        self.assertEqual((current[note["id"]]["status"], current[note["id"]]["text"]), ("open", note["text"]))

    def test_full_check_reads_conclusions_without_an_open_round_and_never_rewrites_notes(self):
        round_no, child_note = self.branch_note()
        draft = analysis.convert(self.review, self.aid, "n1", text="已经改完的年度结论")
        resolved = analysis.confirm_convert(self.review, self.aid, draft["id"])
        notes.set_status(self.review, resolved["id"], "resolved", by="Synthetic")
        analysis.submit_round(self.review, self.aid, round_no)
        claimed = analysis.claim(self.review)
        finished = analysis.writeback(self.review, self.aid, round_no, claimed["baseRev"], [])
        self.assertIsNone(finished["nextRound"])
        self.assertTrue(analysis.full_check_context(self.review)["analyses"][0]["conclusions"])
        analysis.redecide(self.review, self.aid, "n1", "Q")
        before = notes.load(self.review)
        notes.handoff_submit(self.review, full_check=True)
        context = analysis.full_check_context(self.review)
        categories = {item["kind"]: item for item in context["checks"]}
        self.assertEqual(set(categories), {"changed-notes", "resolved-changed-basis", "report-conflicts", "analysis-conflicts"})
        self.assertEqual([n["id"] for n in categories["changed-notes"]["notes"]], [child_note["id"]])
        self.assertEqual([n["id"] for n in categories["resolved-changed-basis"]["notes"]], [resolved["id"]])
        self.assertEqual(notes.load(self.review), before)

    def test_claimed_revision_snapshot_is_not_pulled_and_resolved_note_is_not_rewound(self):
        _round_no, note = self.branch_note()
        notes.handoff_submit(self.review)
        batch = notes.handoff_claim(self.review)
        analysis.redecide(self.review, self.aid, "n1", "Q")
        in_progress = next(n for n in notes.load(self.review)["notes"] if n["id"] == note["id"])
        self.assertEqual(in_progress["status"], "open")
        self.assertIn(note["id"], notes.handoff_load(self.review)["items"][-1]["openIds"])
        notes.set_status(self.review, note["id"], "resolved", by="Synthetic", seq=batch["seq"])
        notes.handoff_done(self.review, seq=batch["seq"])
        analysis.sync_derived_notes(self.review, self.aid)
        processed = next(n for n in notes.load(self.review)["notes"] if n["id"] == note["id"])
        self.assertEqual((processed["status"], processed["text"]), ("resolved", note["text"]))

    def test_another_analysis_can_start_while_this_one_is_claimed(self):
        claimed = analysis.claim(self.review)
        _doc, other = notes.add(self.review, {"page": 2, "text": "另一个独立焦点"})
        second, _number = analysis.start(self.review, other["id"])
        self.assertFalse(second["protection"]["locked"])
        self.assertEqual(analysis.load(self.review, self.aid)["rev"], claimed["baseRev"])
        analysis.writeback(self.review, self.aid, claimed["seq"], claimed["baseRev"], [])

    def test_structure_void_and_rejection_sync_the_linked_note(self):
        round_no, note = self.branch_note()
        original = analysis.load(self.review, self.aid)["nodes"]["n2"]["decision"]
        analysis.submit_round(self.review, self.aid, round_no)
        next_round = self.questions([{"key": "remove", "op": "void", "targets": ["n1"]}])
        notes.handoff_submit(self.review)
        analysis.answer(self.review, self.aid, next_round, "S:remove", {"kind": "accept"})
        held = next(n for n in notes.load(self.review)["notes"] if n["id"] == note["id"])
        self.assertEqual((held["status"], held["hold"]), ("held", "结论已变"))
        restored = analysis.answer(self.review, self.aid, next_round, "S:remove", {"kind": "reject"})
        self.assertEqual(restored["nodes"]["n2"]["decision"], original)
        reopened = next(n for n in notes.load(self.review)["notes"] if n["id"] == note["id"])
        self.assertEqual(reopened["status"], "open")


if __name__ == "__main__":
    unittest.main()
