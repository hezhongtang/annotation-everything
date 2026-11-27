"""Word 修订逐项写入许可和回执。素材全部为合成数据。"""

from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
from types import SimpleNamespace
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "skill/annotation-everything/scripts"))


class WordRevisionJournalTests(unittest.TestCase):
    def setUp(self):
        import word_revisions

        self.handle = word_revisions.handle
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        root = Path(temp.name)
        self.items = [
            {"id": letter, "from": "五", "to": "七", "bodyParagraph": n, "paragraph": "第五日"}
            for n, letter in enumerate(("a", "b"))
        ]
        self.binding = SimpleNamespace(
            review=root,
            rid="review",
            document=root / "synthetic.docx",
            suggestions=lambda: self.items,
            canonical_document_key="canonical-key",
            document_keys={"bound-key", "canonical-key"},
        )
        self.plan = {
            "documentKey": "bound-key",
            "reviewId": "review",
            "paragraphKey": "body/0",
            "id": "a",
            "start": 1,
            "end": 2,
            "before": {"original": "第五日", "current": "第五日", "revisions": []},
            "expected": "第七日",
        }

    def call(self, action, **body):
        return self.handle(self.binding, "/api/revision-operations/" + action, {"documentKey": "bound-key", **body})

    def after(self, author):
        return {
            "original": "第五日",
            "current": "第七日",
            "revisions": [
                {"type": kind, "text": text, "start": 1, "author": author, "date": "2026-11-27"}
                for kind, text in (("Deleted", "五"), ("Added", "七"))
            ],
        }

    def test_old_alias_plan_and_receipt_are_rebound_only_to_the_same_document(self):
        import word_revisions

        batch = self.call("plan", items=["a"])["batch"]
        with self.assertRaisesRegex(ValueError, "bad-revision-plan"):
            self.call("begin", batch=batch, id="a", plan=dict(self.plan, documentKey="other-directory/synthetic.docx"))
        permit = self.call("begin", batch=batch, id="a", plan=self.plan)["permit"]
        self.assertEqual(word_revisions.operations(self.binding)[0]["plan"]["documentKey"], "canonical-key")
        after = self.after("乙")
        self.call("observe", batch=batch, id="a", permit=permit, status="written", after=after)
        # The previous pane and service stored the URL form Word supplied at write time.
        path = self.binding.review / word_revisions.FILE
        journal = json.loads(path.read_text(encoding="utf-8"))
        journal["operations"][0]["plan"]["documentKey"] = "bound-key"
        path.write_text(json.dumps(journal), encoding="utf-8")
        self.call("observe", documentKey="canonical-key", batch=batch, id="a", permit="", status="written", after=after)
        self.assertEqual(word_revisions.operations(self.binding)[0]["receipt"], after)
        self.binding.canonical_document_key = "other-directory/synthetic.docx"
        with self.assertRaisesRegex(ValueError, "revision-document-changed"):
            word_revisions.operations(self.binding)

    def test_missing_receipt_requires_fresh_revisions_from_the_recorded_writer(self):
        import word_revisions

        batch = self.call("plan", items=["a", "b"])["batch"]
        self.call("begin", batch=batch, id="a", plan=dict(self.plan, writer="乙"))
        attempt = {"batch": batch, "id": "a", "permit": ""}
        with self.assertRaisesRegex(ValueError, "wrong-revision-receipt"):
            self.call("observe", **attempt, status="written", after=self.after("丙"))
        self.call("observe", **attempt, status="unknown", after=self.after("丙"))
        self.assertIsNone(word_revisions.operations(self.binding)[0]["receipt"])
        with self.assertRaisesRegex(ValueError, "revision-already-attempted"):
            self.call("plan", items=["a"])
        self.call("observe", **attempt, status="written", after=self.after("乙"))
        self.assertEqual(word_revisions.operations(self.binding)[0]["status"], "written")
        next_plan = dict(self.plan, id="b", paragraphKey="body/1")
        self.assertTrue(self.call("begin", batch=batch, id="b", plan=next_plan)["mayWrite"])

    def test_unidentified_writer_needs_the_original_permit_for_a_first_receipt(self):
        batch = self.call("plan", items=["a"])["batch"]
        permit = self.call("begin", batch=batch, id="a", plan=self.plan)["permit"]
        attempt = {"batch": batch, "id": "a"}
        self.call("observe", **attempt, permit="", status="unknown", after=self.after("丙"))
        with self.assertRaisesRegex(ValueError, "wrong-revision-receipt"):
            self.call("observe", **attempt, permit="", status="written", after=self.after("丙"))
        # A passive reader cannot spend the writer's permit or block its verified native readback.
        self.call("observe", **attempt, permit=permit, status="written", after=self.after("乙"))
        self.call("observe", **attempt, permit="", status="written", after=self.after("乙"))
        with self.assertRaisesRegex(ValueError, "wrong-revision-receipt"):
            self.call("observe", **attempt, permit="", status="written", after=self.after("丙"))

    def test_recovery_preserves_other_authors_shifted_by_a_utf16_replacement(self):
        def revision(kind, author, text, start):
            return {"type": kind, "author": author, "text": text, "start": start, "date": "2026-11-27"}

        old = [revision("Deleted", "甲", "签", 3), revision("Added", "甲", "发", 3)]
        before = {"original": "📄五签", "current": "📄五发", "revisions": old}
        self.items[0].update(paragraph=before["current"], to="七📌")
        plan = dict(self.plan, start=2, end=3, before=before, expected="📄七📌发", writer="乙")
        batch = self.call("plan", items=["a"])["batch"]
        self.call("begin", batch=batch, id="a", plan=plan)
        fresh = [revision("Deleted", "乙", "五", 2), revision("Added", "乙", "七📌", 2)]
        after = dict(before, current=plan["expected"], revisions=fresh + [dict(r, start=5) for r in old])
        attempt = {"batch": batch, "id": "a", "permit": "", "status": "written"}
        with self.assertRaisesRegex(ValueError, "wrong-revision-receipt"):
            self.call("observe", **attempt, after=dict(after, revisions=fresh + old))
        self.call("observe", **attempt, after=after)

    def test_write_restrictions_are_checked_at_selection_and_again_at_the_grant(self):
        self.items[0]["writeRestriction"] = "合成文档的段落标识需核对。"
        with self.assertRaisesRegex(ValueError, "revision-write-restricted"):
            self.call("plan", items=["a", "b"])
        del self.items[0]["writeRestriction"]
        batch = self.call("plan", items=["a"])["batch"]
        self.items[0]["writeRestriction"] = "合成文档包含不支持的结构。"
        with self.assertRaisesRegex(ValueError, "revision-write-restricted"):
            self.call("begin", batch=batch, id="a", plan=self.plan)

    def test_racing_writers_cannot_replay_a_lost_receipt_or_withdraw_an_attempt(self):
        import word_revisions

        self.assertEqual(word_revisions.operations(self.binding), [])
        batch = self.call("plan", items=["a", "b"])["batch"]
        with ThreadPoolExecutor(max_workers=2) as workers:
            grants = list(workers.map(lambda _: self.call("begin", batch=batch, id="a", plan=self.plan), range(2)))
        self.assertEqual(sorted(g["mayWrite"] for g in grants), [False, True])
        ops = word_revisions.operations(self.binding)
        self.assertEqual([(o["id"], o["status"]) for o in ops], [("a", "unknown"), ("b", "unexecuted")])
        with self.assertRaisesRegex(ValueError, "revision-already-attempted"):
            self.call("plan", items=["a"])
        with self.assertRaisesRegex(ValueError, "revision-already-attempted"):
            word_revisions.ensure_withdrawable(self.binding.review, "a")
        word_revisions.ensure_withdrawable(self.binding.review, "b")
