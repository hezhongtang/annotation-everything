"""分析存储层：批注联动、轮次流转、结论转批注。只走公开接口，只断言外部可见行为。

情形来源：prototype/analysis-model 原型的「从批注到结论」（本票范围内的步骤）、
「已交出但没被接手的批注」「已经在改的批注」；原型中「应被拒绝」的步骤断言为拒绝。
"""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "skill/annotation-everything" / "scripts"
sys.path.insert(0, str(SCRIPTS))
sys.path.insert(0, str(ROOT))

import host_isolation  # noqa: E402

host_isolation.require_for_module(__name__)

import analysis_store  # noqa: E402
import notes_store as store  # noqa: E402


def tmpdir() -> Path:
    return Path(tempfile.mkdtemp())


def add_note(d: Path, text: str = "第 3 页「增长主要来自新客」这个结论站得住吗？", page: int = 3) -> dict:
    _doc, note = store.add(d, {"page": page, "text": text, "target": "报告",
                               "quote": "增长主要来自新客", "quoteFrom": "报告"})
    return note


def propose_caliber(key: str = "p1", parent: dict | None = None) -> dict:
    """Agent 写回的结构提议：问题文本、简称、选项、推荐答案、挂在哪个问题下。"""
    return {"key": key, "text": "第 3 页「增长主要来自新客」用年度还是季度口径来验证？",
            "short": "验证口径", "recommend": "Y",
            "options": [{"id": "Y", "label": "年度"}, {"id": "S", "label": "季度"}],
            "parent": parent}


class AnalysisStartTests(unittest.TestCase):
    """发起分析与批注联动。"""

    def setUp(self):
        self.d = tmpdir()

    def test_start_holds_open_note_and_requests_opening(self):
        """未处理批注发起分析：批注搁住（分析中）、不进提交快照、自动请求开题。"""
        note = add_note(self.d)
        state, round_seq = analysis_store.start(self.d, note["id"])
        held = store.load(self.d)["notes"][0]
        self.assertEqual(held["status"], "held")
        self.assertEqual(held["hold"], "分析中")
        self.assertEqual(held["heldBy"], state["id"])
        self.assertEqual(state["focus"], note["text"])
        self.assertEqual(state["sourceNote"], note["id"])
        self.assertEqual(state["status"], "active")
        # 开题轮次已入队等待 Agent
        self.assertEqual(round_seq, 1)
        rounds = analysis_store.load(self.d, state["id"])["rounds"]
        self.assertEqual(rounds[0]["seq"], 1)
        self.assertEqual(rounds[0]["kind"], "opening")
        self.assertEqual(rounds[0]["status"], "submitted")
        # 搁住的批注不进「交给 Agent 改稿」的批次快照
        _doc, item, _m = store.handoff_submit(self.d, "合成附言")
        self.assertNotIn(note["id"], item["openIds"])
        self.assertEqual(item["openCount"], 0)

    def test_start_pulls_note_out_of_unclaimed_batch(self):
        """原型情形「已交出但没被接手的批注」：发起分析把它从这次改稿里拿出来。"""
        first = add_note(self.d, page=3)
        second = add_note(self.d, text="第 5 页图表配色太浅，投影上看不清", page=5)
        _doc, item, _m = store.handoff_submit(self.d, "合成附言")
        self.assertEqual(item["openIds"], [first["id"], second["id"]])
        analysis_store.start(self.d, first["id"])
        batch = store.handoff_load(self.d)["items"][0]
        self.assertEqual(batch["openIds"], [second["id"]])
        self.assertEqual(batch["status"], "pending")
        # Agent 只会改剩下的那条
        claimed = store.handoff_claim(self.d, by="Synthetic")
        self.assertEqual(claimed["openIds"], [second["id"]])

    def test_start_emptying_a_batch_voids_it(self):
        """批次里只剩这一条时，移出后整批作废，不再被领取。"""
        note = add_note(self.d)
        store.handoff_submit(self.d, "合成附言")
        analysis_store.start(self.d, note["id"])
        batch = store.handoff_load(self.d)["items"][0]
        self.assertEqual(batch["status"], "void")
        self.assertEqual(batch["openIds"], [])
        self.assertIsNone(store.handoff_claim(self.d, by="Synthetic"))

    def test_start_refuses_note_already_under_revision(self):
        """原型情形「已经在改的批注」：Agent 已在改的批注不能发起分析。"""
        note = add_note(self.d)
        store.handoff_submit(self.d, "合成附言")
        store.handoff_claim(self.d, by="Synthetic")
        with self.assertRaises(store.HandoffError) as ctx:
            analysis_store.start(self.d, note["id"])
        self.assertIn("Synthetic", str(ctx.exception))   # 说明原因
        self.assertEqual(ctx.exception.code, "note-in-progress")
        # 批注未被搁住，也没有产生分析
        self.assertEqual(store.load(self.d)["notes"][0]["status"], "open")
        self.assertEqual(analysis_store.list_analyses(self.d), [])

    def test_start_refuses_processed_note(self):
        """已处理（或已初改、已搁住）的批注不能发起分析。"""
        note = add_note(self.d)
        store.set_status(self.d, note["id"], "resolved", resolution="已改", by="Synthetic")
        with self.assertRaises(store.HandoffError) as ctx:
            analysis_store.start(self.d, note["id"])
        self.assertEqual(ctx.exception.code, "note-not-open")
        drafted = add_note(self.d, page=4)
        store.import_notes(self.d, [{"id": drafted["id"], "page": 4, "text": "x",
                                     "status": "drafted", "draftedAt": "2030-01-01T00:00:00Z"}])
        with self.assertRaises(store.HandoffError):
            analysis_store.start(self.d, drafted["id"])
        held = add_note(self.d, page=5)
        analysis_store.start(self.d, held["id"])
        with self.assertRaises(store.HandoffError) as ctx:
            analysis_store.start(self.d, held["id"])
        self.assertEqual(ctx.exception.code, "note-not-open")
        self.assertEqual(len(analysis_store.list_analyses(self.d)), 1)

    def test_source_copy_survives_edit_and_delete(self):
        """分析保存源批注原文副本；源批注被编辑或删除后回看仍是原文。"""
        note = add_note(self.d)
        state, _seq = analysis_store.start(self.d, note["id"])
        store.update(self.d, note["id"], {"text": "后来改过的话"})
        kept = analysis_store.load(self.d, state["id"])["sourceCopy"]
        self.assertEqual(kept["text"], "第 3 页「增长主要来自新客」这个结论站得住吗？")
        self.assertEqual(kept["quote"], "增长主要来自新客")
        store.delete(self.d, note["id"])
        self.assertEqual(analysis_store.load(self.d, state["id"])["sourceCopy"]["text"],
                         "第 3 页「增长主要来自新客」这个结论站得住吗？")

    def test_release_returns_source_note_to_open(self):
        """不打算继续分析时手动解除搁住，批注回到未处理。"""
        note = add_note(self.d)
        state, _seq = analysis_store.start(self.d, note["id"])
        analysis_store.release_source(self.d, state["id"])
        after = store.load(self.d)["notes"][0]
        self.assertEqual(after["status"], "open")
        self.assertNotIn("hold", after)
        with self.assertRaises(store.HandoffError):
            analysis_store.release_source(self.d, state["id"])   # 只能解除一次


class RoundFlowTests(unittest.TestCase):
    """轮次流转：领取、写回、作答、交出；决策与结构提议的分工。"""

    def setUp(self):
        self.d = tmpdir()
        self.note = add_note(self.d)
        self.state, self.round1 = analysis_store.start(self.d, self.note["id"])

    def test_writeback_then_answer_then_submit_composes_next_round(self):
        """从开题到追问：Agent 提问、审阅人确认并作答、交出、Agent 写回下一轮。"""
        claimed = analysis_store.claim(self.d, by="Synthetic")
        self.assertEqual(claimed["seq"], self.round1)
        self.assertEqual(claimed["baseRev"], 1)
        analysis_store.writeback(self.d, self.state["id"], self.round1, 1,
                                 [propose_caliber()], note="先问口径", by="Synthetic")
        state = analysis_store.load(self.d, self.state["id"])
        self.assertEqual([r["seq"] for r in state["rounds"]], [1, 2])
        self.assertEqual(state["rounds"][1]["status"], "open")
        # 结构提议未确认前不在问题树上
        self.assertEqual(state["nodes"], {})
        self.assertEqual(state["rounds"][1]["items"][0]["type"], "structure")
        # 确认结构提议 → 问题节点入树；问题本身出现在同一轮
        analysis_store.answer(self.d, self.state["id"], 2, "S:p1", {"kind": "accept"})
        state = analysis_store.load(self.d, self.state["id"])
        self.assertEqual(list(state["nodes"]), ["n1"])
        self.assertEqual(state["nodes"]["n1"]["short"], "验证口径")
        self.assertEqual(state["nodes"]["n1"]["options"], {"Y": "年度", "S": "季度"})
        self.assertEqual(state["nodes"]["n1"]["recommend"], "Y")
        question_keys = [i["key"] for i in state["rounds"][1]["items"] if i["type"] == "question"]
        self.assertEqual(question_keys, ["Q:n1"])
        # 决策由系统根据回答产生：选「年度」→ 决策入账，节点已决
        analysis_store.answer(self.d, self.state["id"], 2, "Q:n1", {"kind": "option", "option": "Y"})
        state = analysis_store.load(self.d, self.state["id"])
        self.assertEqual(state["nodes"]["n1"]["status"], "decided")
        self.assertEqual(list(state["decisions"]), ["d1"])
        self.assertEqual(state["decisions"]["d1"]["option"], "Y")
        self.assertEqual(state["decisions"]["d1"]["label"], "年度")
        # 交出这一轮 → 进入队列；Agent 写回时无新提议、无待问问题，不再有下一轮
        analysis_store.submit_round(self.d, self.state["id"], 2)
        state = analysis_store.load(self.d, self.state["id"])
        self.assertEqual(state["rounds"][1]["status"], "submitted")
        second = analysis_store.claim(self.d, by="Synthetic")
        self.assertEqual(second["seq"], 2)
        result = analysis_store.writeback(self.d, self.state["id"], 2, second["baseRev"], [],
                                          note="没有要追问的了", by="Synthetic")
        self.assertIsNone(result.get("nextRound"))
        self.assertEqual(analysis_store.load(self.d, self.state["id"])["rounds"][1]["status"], "done")

    def test_answer_retries_are_idempotent_and_changes_keep_the_last_answer(self):
        """同题重传不重复建节点/决策，接手前改选保留最新原话答案。"""
        analysis_store.claim(self.d)
        analysis_store.writeback(self.d, self.state["id"], self.round1, 1, [propose_caliber()])
        accepted = analysis_store.answer(self.d, self.state["id"], 2, "S:p1", {"kind": "accept"})
        retried = analysis_store.answer(self.d, self.state["id"], 2, "S:p1", {"kind": "accept"})
        self.assertEqual(retried, accepted)
        first = {"kind": "option", "option": "Y", "verbatim": "先用年度"}
        decided = analysis_store.answer(self.d, self.state["id"], 2, "Q:n1", first)
        self.assertEqual(analysis_store.answer(self.d, self.state["id"], 2, "Q:n1", first), decided)
        last = {"kind": "option", "option": "S", "verbatim": "改用季度"}
        changed = analysis_store.answer(self.d, self.state["id"], 2, "Q:n1", last)
        self.assertEqual(changed["rounds"][1]["answers"]["Q:n1"], last)
        self.assertEqual(len(changed["nodes"]), 1)
        self.assertEqual(len(changed["decisions"]), 2)
        self.assertEqual(changed["decisions"][changed["nodes"]["n1"]["decision"]]["option"], "S")

    def test_structure_answer_changes_reuse_the_confirmed_node(self):
        """确认后改为不采纳会撤下该题，再确认复用稳定节点与已有回答。"""
        analysis_store.claim(self.d)
        analysis_store.writeback(self.d, self.state["id"], self.round1, 1, [propose_caliber()])
        analysis_store.answer(self.d, self.state["id"], 2, "S:p1", {"kind": "accept"})
        analysis_store.answer(self.d, self.state["id"], 2, "Q:n1", {"kind": "accept"})
        rejected = analysis_store.answer(self.d, self.state["id"], 2, "S:p1", {"kind": "reject"})
        self.assertEqual(rejected["rounds"][1]["answers"]["S:p1"], {"kind": "reject"})
        with self.assertRaises(store.HandoffError):
            analysis_store.convert(self.d, self.state["id"], "n1")
        restored = analysis_store.answer(self.d, self.state["id"], 2, "S:p1", {"kind": "accept"})
        self.assertEqual(list(restored["nodes"]), ["n1"])
        self.assertEqual(restored["nodes"]["n1"]["decision"], "d1")
        self.assertEqual(restored["rounds"][1]["rejected"], [])

    def test_recommend_answer_kind_uses_recommended_option(self):
        """作答「用推荐答案」与选选项等效。"""
        analysis_store.claim(self.d, by="Synthetic")
        analysis_store.writeback(self.d, self.state["id"], self.round1, 1, [propose_caliber()], by="Synthetic")
        analysis_store.answer(self.d, self.state["id"], 2, "S:p1", {"kind": "accept"})
        analysis_store.answer(self.d, self.state["id"], 2, "Q:n1", {"kind": "accept"})
        state = analysis_store.load(self.d, self.state["id"])
        self.assertEqual(state["decisions"]["d1"]["option"], "Y")

    def test_rejected_proposal_never_enters_tree(self):
        """结构提议不采纳：不入树，也不出现在下一轮。"""
        analysis_store.claim(self.d, by="Synthetic")
        analysis_store.writeback(self.d, self.state["id"], self.round1, 1,
                                 [propose_caliber("p1"), propose_caliber("p2")], by="Synthetic")
        analysis_store.answer(self.d, self.state["id"], 2, "S:p1", {"kind": "reject"})
        analysis_store.answer(self.d, self.state["id"], 2, "S:p2", {"kind": "accept"})
        state = analysis_store.load(self.d, self.state["id"])
        self.assertEqual(list(state["nodes"]), ["n1"])
        analysis_store.submit_round(self.d, self.state["id"], 2)
        claimed = analysis_store.claim(self.d, by="Synthetic")
        result = analysis_store.writeback(self.d, self.state["id"], 2, claimed["baseRev"], [], by="Synthetic")
        next_round = result.get("nextRound")
        if next_round:
            items = analysis_store.load(self.d, self.state["id"])["rounds"][-1]["items"]
            self.assertEqual([i["key"] for i in items if i["type"] == "question"], ["Q:n1"])

    def test_child_question_appears_only_on_matching_branch(self):
        """挂在某选项下的问题，只有答到那个选项才出现在轮次里。"""
        analysis_store.claim(self.d, by="Synthetic")
        analysis_store.writeback(self.d, self.state["id"], self.round1, 1, [propose_caliber()], by="Synthetic")
        analysis_store.answer(self.d, self.state["id"], 2, "S:p1", {"kind": "accept"})
        analysis_store.answer(self.d, self.state["id"], 2, "Q:n1", {"kind": "option", "option": "S"})
        analysis_store.submit_round(self.d, self.state["id"], 2)
        claimed = analysis_store.claim(self.d, by="Synthetic")
        # 子问题挂在「年度」下；当前答了「季度」，写回后下一轮只有结构提议、没有可答的问题
        child = {"key": "p2", "text": "年度口径下，对比对象选谁？", "short": "对比对象",
                 "options": [{"id": "P", "label": "去年同期"}, {"id": "I", "label": "行业均值"}],
                 "recommend": "P", "parent": {"node": "n1", "option": "Y"}}
        result = analysis_store.writeback(self.d, self.state["id"], 2, claimed["baseRev"], [child], by="Synthetic")
        nxt = result["nextRound"]
        items = analysis_store.load(self.d, self.state["id"])["rounds"][-1]["items"]
        self.assertEqual([i["key"] for i in items], ["S:p2"])
        # 确认子问题入树后交出：n1 仍是「季度」，挂在「年度」下的 n2 不出现在下一轮
        analysis_store.answer(self.d, self.state["id"], nxt, "S:p2", {"kind": "accept"})
        analysis_store.submit_round(self.d, self.state["id"], nxt)
        claimed2 = analysis_store.claim(self.d, by="Synthetic")
        result2 = analysis_store.writeback(self.d, self.state["id"], nxt, claimed2["baseRev"], [], by="Synthetic")
        if result2.get("nextRound"):
            items2 = analysis_store.load(self.d, self.state["id"])["rounds"][-1]["items"]
            self.assertNotIn("Q:n2", [i["key"] for i in items2])
            self.assertEqual(items2, [])

    def test_answers_are_kept_verbatim_without_truncation(self):
        """问答记录不截断：问题文本、选项与审阅人回答原文保留。"""
        long_text = "问题原文很长。" * 60   # 远超批注侧 200 字的短字段上限
        analysis_store.claim(self.d, by="Synthetic")
        proposal = propose_caliber()
        proposal["text"] = long_text
        analysis_store.writeback(self.d, self.state["id"], self.round1, 1, [proposal], by="Synthetic")
        analysis_store.answer(self.d, self.state["id"], 2, "S:p1", {"kind": "accept"})
        analysis_store.answer(self.d, self.state["id"], 2, "Q:n1", {"kind": "option", "option": "S"})
        state = analysis_store.load(self.d, self.state["id"])
        self.assertEqual(state["nodes"]["n1"]["text"], long_text)
        self.assertEqual(state["rounds"][1]["answers"]["Q:n1"], {"kind": "option", "option": "S"})

    def test_unanswered_questions_reappear_in_next_round(self):
        """只答一部分就交出：没答的问题下一轮还会出现。"""
        analysis_store.claim(self.d, by="Synthetic")
        analysis_store.writeback(self.d, self.state["id"], self.round1, 1,
                                 [propose_caliber("p1"), propose_caliber("p2")], by="Synthetic")
        analysis_store.answer(self.d, self.state["id"], 2, "S:p1", {"kind": "accept"})
        analysis_store.answer(self.d, self.state["id"], 2, "Q:n1", {"kind": "option", "option": "Y"})
        analysis_store.submit_round(self.d, self.state["id"], 2)   # S:p2 没答就交出
        claimed = analysis_store.claim(self.d, by="Synthetic")
        analysis_store.writeback(self.d, self.state["id"], 2, claimed["baseRev"], [], by="Synthetic")
        state = analysis_store.load(self.d, self.state["id"])
        last = state["rounds"][-1]
        self.assertIn("S:p2", [i["key"] for i in last["items"]])
        analysis_store.answer(self.d, self.state["id"], last["seq"], "S:p2", {"kind": "accept"})
        state = analysis_store.load(self.d, self.state["id"])
        self.assertEqual(sorted(state["nodes"]), ["n1", "n2"])

    def test_answers_merge_into_submitted_round_without_requeue(self):
        """被领取前重复交出合并为同一轮：交出后补答仍进同一轮，不重复排队。"""
        analysis_store.claim(self.d, by="Synthetic")
        analysis_store.writeback(self.d, self.state["id"], self.round1, 1, [propose_caliber()], by="Synthetic")
        analysis_store.answer(self.d, self.state["id"], 2, "S:p1", {"kind": "accept"})
        analysis_store.submit_round(self.d, self.state["id"], 2)
        seq_before = analysis_store.load(self.d, self.state["id"])["rounds"][1]["seq"]
        analysis_store.answer(self.d, self.state["id"], 2, "Q:n1", {"kind": "option", "option": "Y"})
        state = analysis_store.load(self.d, self.state["id"])
        self.assertEqual(len(state["rounds"]), 2)
        self.assertEqual(state["rounds"][1]["seq"], seq_before)
        self.assertEqual(state["rounds"][1]["status"], "submitted")

    def test_answer_refused_for_done_round(self):
        """已处理完的轮次不能再作答。"""
        analysis_store.claim(self.d, by="Synthetic")
        analysis_store.writeback(self.d, self.state["id"], self.round1, 1, [propose_caliber()], by="Synthetic")
        with self.assertRaises(store.HandoffError) as ctx:
            analysis_store.answer(self.d, self.state["id"], 1, "S:p1", {"kind": "accept"})
        self.assertEqual(ctx.exception.code, "round-not-answerable")

    def test_writeback_requires_claim_and_matching_base(self):
        """写回只针对已领取的轮次；基线版本不符被拒收。"""
        with self.assertRaises(store.HandoffError):
            analysis_store.writeback(self.d, self.state["id"], self.round1, 1, [], by="Synthetic")
        claimed = analysis_store.claim(self.d, by="Synthetic")
        with self.assertRaises(store.HandoffError) as ctx:
            analysis_store.writeback(self.d, self.state["id"], self.round1, claimed["baseRev"] + 5,
                                     [], by="Synthetic")
        self.assertEqual(ctx.exception.code, "base-rev-mismatch")

    def test_agent_handles_one_item_at_a_time(self):
        """Agent 一次只处理一项：有处理中的轮次时不能再领。"""
        analysis_store.claim(self.d, by="Synthetic")
        other = add_note(self.d, page=6)
        _state2, round2 = analysis_store.start(self.d, other["id"])
        with self.assertRaises(store.HandoffError) as ctx:
            analysis_store.claim(self.d, by="Synthetic")
        self.assertEqual(ctx.exception.code, "agent-busy")
        # 批次在处理中时同样领不到轮次
        analysis_store.writeback(self.d, self.state["id"], self.round1, 1, [], by="Synthetic")
        third = add_note(self.d, page=7)
        store.handoff_submit(self.d, "合成附言")
        store.handoff_claim(self.d, by="Synthetic")
        with self.assertRaises(store.HandoffError):
            analysis_store.claim(self.d, by="Synthetic")


class AnalysisModelTests(unittest.TestCase):
    """存储形态：事件只追加、schema 版本、稳定标识、轮次号审阅内唯一、基线单调递增。"""

    def setUp(self):
        self.d = tmpdir()

    def events(self, aid: str) -> list[dict]:
        path = self.d / "analysis" / aid / "events.jsonl"
        return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]

    def test_events_append_only_with_schema_version(self):
        note = add_note(self.d)
        state, round1 = analysis_store.start(self.d, note["id"])
        first = self.events(state["id"])
        self.assertEqual(first[0]["type"], "started")
        self.assertEqual(first[0]["schema"], 1)
        analysis_store.claim(self.d, by="Synthetic")
        analysis_store.writeback(self.d, state["id"], round1, 1, [propose_caliber()], by="Synthetic")
        second = self.events(state["id"])
        self.assertEqual(second[:len(first)], first)   # 已写下的事件一个字都不改
        self.assertGreater(len(second), len(first))
        for f in (self.d / "analysis" / state["id"] / "events.jsonl", self.d / "analysis" / "counter.json"):
            self.assertEqual(f.stat().st_mode & 0o777, 0o600, f)

    def test_round_numbers_unique_across_analyses_and_baseline_monotonic(self):
        one = add_note(self.d, page=3)
        two = add_note(self.d, page=5)
        state1, round1 = analysis_store.start(self.d, one["id"])
        state2, round2 = analysis_store.start(self.d, two["id"])
        self.assertEqual(round1, 1)
        self.assertEqual(round2, 2)   # 轮次号在整份审阅内唯一，不按分析各自编号
        self.assertGreaterEqual(round2, round1 + 1)
        # 分析线标识预留：本票只有主线
        self.assertEqual(state1["line"], "main")
        # 每个分析有自己的单调递增基线版本
        self.assertEqual(state1["rev"], 1)
        self.assertEqual(state2["rev"], 1)
        analysis_store.claim(self.d, by="Synthetic")
        result = analysis_store.writeback(self.d, state1["id"], round1, 1, [propose_caliber()], by="Synthetic")
        state1_after = analysis_store.load(self.d, state1["id"])
        self.assertEqual(state1_after["rev"], 2)
        self.assertEqual(analysis_store.load(self.d, state2["id"])["rev"], 1)
        # 写回产生的新一轮预填上下文里带轮次号与提议键
        self.assertEqual(result["nextRound"], 2 + 1)

    def test_list_reports_analyses_with_status(self):
        note = add_note(self.d)
        state, _round = analysis_store.start(self.d, note["id"])
        rows = analysis_store.list_analyses(self.d)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["id"], state["id"])
        self.assertEqual(rows[0]["status"], "active")
        self.assertEqual(rows[0]["sourceNote"], note["id"])

    def test_state_derived_from_event_log_only(self):
        """当前状态由事件日志推导：删掉推导缓存外的任何东西都不影响——事件日志是唯一事实来源。"""
        note = add_note(self.d)
        state, round1 = analysis_store.start(self.d, note["id"])
        analysis_store.claim(self.d, by="Synthetic")
        analysis_store.writeback(self.d, state["id"], round1, 1, [propose_caliber()], by="Synthetic")
        analysis_store.answer(self.d, state["id"], 2, "S:p1", {"kind": "accept"})
        again = analysis_store.load(self.d, state["id"])
        self.assertEqual(list(again["nodes"]), ["n1"])   # 无缓存文件也推导出同一状态
        self.assertEqual(sorted(p.name for p in (self.d / "analysis" / state["id"]).iterdir()),
                         ["events.jsonl"])


class ConvertTests(unittest.TestCase):
    """结论落地：转批注草稿、确认、源批注标已处理并指向分析。"""

    def setUp(self):
        self.d = tmpdir()
        self.note = add_note(self.d)
        self.state, self.round1 = analysis_store.start(self.d, self.note["id"])

    def decide_caliber(self, option: str = "Y") -> None:
        analysis_store.claim(self.d, by="Synthetic")
        analysis_store.writeback(self.d, self.state["id"], self.round1, 1, [propose_caliber()], by="Synthetic")
        analysis_store.answer(self.d, self.state["id"], 2, "S:p1", {"kind": "accept"})
        analysis_store.answer(self.d, self.state["id"], 2, "Q:n1", {"kind": "option", "option": option})

    def test_convert_creates_draft_confirm_adds_note_and_resolves_source(self):
        """原型情形「从批注到结论」的收尾：草稿 → 确认 → 进入批注列表、源批注已处理并指向分析。"""
        self.decide_caliber()
        draft = analysis_store.convert(self.d, self.state["id"], "n1")
        self.assertEqual(draft["status"], "pending")
        self.assertEqual(draft["basis"]["decision"], "d1")
        # 草稿还没进批注列表
        self.assertEqual(len(store.load(self.d)["notes"]), 1)
        confirmed = analysis_store.confirm_convert(self.d, self.state["id"], draft["id"])
        notes = store.load(self.d)["notes"]
        self.assertEqual(len(notes), 2)
        self.assertEqual(confirmed["id"], notes[1]["id"])
        self.assertEqual(confirmed["status"], "open")
        self.assertEqual(confirmed["basis"]["analysis"], self.state["id"])
        self.assertEqual(confirmed["fromAnalysis"], self.state["id"])
        self.assertEqual(confirmed["page"], self.note["page"])
        source = next(n for n in notes if n["id"] == self.note["id"])
        self.assertEqual(source["status"], "resolved")
        self.assertEqual(source["analysisId"], self.state["id"])   # 指向分析
        # 草稿确认一次后不能再来
        with self.assertRaises(store.HandoffError):
            analysis_store.confirm_convert(self.d, self.state["id"], draft["id"])

    def test_convert_tracks_the_source_page_key_through_reorder_and_deletion(self):
        """原文副本保持原页，转出批注按当前页面键定位；确认前删页则明确悬空。"""
        with tempfile.TemporaryDirectory() as directory:
            review = Path(directory)
            manifest = review / "review.json"
            manifest.write_text(json.dumps({"pages": [{"n": 3, "key": "growth", "stable": True,
                                                       "title": "原来的增长页"}]}))
            _doc, note = store.add(review, {"page": 3, "text": "增长结论对吗？", "key": "growth"})
            state, round_no = analysis_store.start(review, note["id"])
            analysis_store.claim(review)
            analysis_store.writeback(review, state["id"], round_no, 1, [propose_caliber()])
            analysis_store.answer(review, state["id"], 2, "S:p1", {"kind": "accept"})
            analysis_store.answer(review, state["id"], 2, "Q:n1", {"kind": "accept"})
            manifest.write_text(json.dumps({"pages": [{"n": 7, "key": "growth", "stable": True,
                                                       "title": "重排后的增长页"}]}))
            draft = analysis_store.convert(review, state["id"], "n1")
            self.assertEqual((draft["page"], draft["key"]), (7, "growth"))
            manifest.write_text(json.dumps({"pages": [{"n": 8, "key": "growth", "stable": True,
                                                       "title": "再次重排的增长页"}]}))
            converted = analysis_store.confirm_convert(review, state["id"], draft["id"])
            self.assertEqual((converted["page"], converted["key"], converted["title"]),
                             (8, "growth", "再次重排的增长页"))
            self.assertFalse(converted.get("orphan"))
            original = analysis_store.load(review, state["id"])["sourceCopy"]
            self.assertEqual((original["page"], original["key"], original["text"]),
                             (3, "growth", "增长结论对吗？"))
            draft = analysis_store.convert(review, state["id"], "n1")
            manifest.write_text(json.dumps({"pages": [{"n": 8, "key": "other", "stable": True}]}))
            orphan = analysis_store.confirm_convert(review, state["id"], draft["id"])
            self.assertTrue(orphan["orphan"])
            self.assertEqual(orphan["key"], "growth")

    def test_source_key_is_captured_from_manifest_and_ambiguous_pages_are_orphans(self):
        """旧批注没有键时从发起时的映射补全；同一键重复出现时不猜页面。"""
        with tempfile.TemporaryDirectory() as directory:
            review = Path(directory)
            manifest = review / "review.json"
            manifest.write_text(json.dumps({"pages": [{"n": 3, "key": "growth", "stable": True}]}))
            state, round_no = analysis_store.start(review, add_note(review)["id"])
            self.assertEqual(state["sourceCopy"]["key"], "growth")
            analysis_store.claim(review)
            analysis_store.writeback(review, state["id"], round_no, 1, [propose_caliber()])
            analysis_store.answer(review, state["id"], 2, "S:p1", {"kind": "accept"})
            analysis_store.answer(review, state["id"], 2, "Q:n1", {"kind": "accept"})
            manifest.write_text(json.dumps({"pages": [{"n": 7, "key": "growth", "stable": True},
                                                       {"n": 8, "key": "growth", "stable": False}]}))
            draft = analysis_store.convert(review, state["id"], "n1")
            self.assertTrue(draft["orphan"])

    def test_convert_refuses_undecided_node(self):
        """没有生效结论的节点不能转成批注。"""
        with self.assertRaises(store.HandoffError) as ctx:
            analysis_store.convert(self.d, self.state["id"], "n1")
        self.assertEqual(ctx.exception.code, "node-not-decided")
        self.decide_caliber(option="S")   # 答了「季度」：根节点的结论照样成立
        draft = analysis_store.convert(self.d, self.state["id"], "n1")
        self.assertEqual(draft["basis"]["option"], "S")


class AnalysisBoundaryTests(unittest.TestCase):
    def test_analysis_paths_cannot_escape_the_review_or_follow_foreign_links(self):
        """分析标识、分析目录和事件文件都不能借绝对路径、父目录或符号链接跨审阅。"""
        with tempfile.TemporaryDirectory() as first, tempfile.TemporaryDirectory() as second:
            review, other = Path(first), Path(second)
            foreign, _round_no = analysis_store.start(other, add_note(other)["id"])
            foreign_dir = other / "analysis" / foreign["id"]
            original = (foreign_dir / "events.jsonl").read_bytes()
            for analysis_id in (str(foreign_dir), f"../../{other.name}/analysis/{foreign['id']}",
                                foreign["id"] + "\n"):
                with self.subTest(analysis_id=analysis_id), self.assertRaises(store.NoteError):
                    analysis_store.load(review, analysis_id)
            (review / "analysis").mkdir()
            (review / "analysis" / foreign["id"]).symlink_to(foreign_dir, target_is_directory=True)
            with self.assertRaises(store.NoteError):
                analysis_store.load(review, foreign["id"])
            with self.assertRaises(store.NoteError):
                analysis_store.list_analyses(review)
            (review / "analysis" / foreign["id"]).unlink()
            local = review / "analysis" / foreign["id"]
            local.mkdir()
            (local / "events.jsonl").symlink_to(foreign_dir / "events.jsonl")
            with self.assertRaises(store.NoteError):
                analysis_store.load(review, foreign["id"])
            (local / "events.jsonl").unlink()
            local.rmdir()
            counter = review / "analysis" / "counter.json"
            counter.symlink_to(other / "analysis" / "counter.json")
            note = add_note(review)
            with self.assertRaises(store.NoteError):
                analysis_store.start(review, note["id"])
            self.assertEqual(store.load(review)["notes"][0]["status"], "open")
            counter.unlink()
            (review / "analysis").rmdir()
            (review / "analysis").symlink_to(other / "analysis", target_is_directory=True)
            with self.assertRaises(store.NoteError):
                analysis_store.start(review, note["id"])
            self.assertEqual(store.load(review)["notes"][0]["status"], "open")
            self.assertEqual((foreign_dir / "events.jsonl").read_bytes(), original)


class SharedLockTests(unittest.TestCase):
    """分析存储与批注存储共用审阅目录的锁：同一次写入里同步批注状态与批次。"""

    def setUp(self):
        self.d = tmpdir()

    def test_start_rewrites_notes_and_handoff_atomically_under_one_lock(self):
        note = add_note(self.d)
        other = add_note(self.d, page=5)
        store.handoff_submit(self.d, "合成附言")
        state, _round = analysis_store.start(self.d, note["id"])
        # 一次 start 之后：批注已搁住、批次已移出，两者一致（同一把锁内完成）
        doc = store.load(self.d)
        by_id = {n["id"]: n for n in doc["notes"]}
        self.assertEqual(by_id[note["id"]]["status"], "held")
        batch = store.handoff_load(self.d)["items"][0]
        self.assertEqual(batch["openIds"], [other["id"]])
        # 批注日志也记录了这次搁住
        log_ops = [json.loads(x) for x in (self.d / "notes.jsonl").read_text().splitlines()]
        self.assertTrue(any(e.get("id") == note["id"] and "held" in str(e.get("op", "")) for e in log_ops))


if __name__ == "__main__":
    unittest.main()
