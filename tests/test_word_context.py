"""Selected synthetic Word material crosses the existing receiver queue."""

import hashlib
import json
import sys
import tempfile
import unittest
from types import SimpleNamespace
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "skill/annotation-everything/scripts"))

import notes_store
import word_context
import wakeup


class WordContextTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.review = Path(temp.name)
        (self.review / "review.json").write_text(json.dumps({"reviewId": "a" * 32}))
        self.receiver = wakeup.bind_wait(self.review, "Codex", "synthetic-session")["bindingId"]
        self.fp = "e" * 64
        self.para = {
            "n": 3,
            "paraId": "1A000003",
            "digest": hashlib.sha256("共同登记样机编号。".encode()).hexdigest(),
            "original": "分别登记样机编号。",
            "current": "共同登记样机编号。",
            "revisions": [
                {"type": "Added", "author": "甲", "date": "", "text": "共同", "verified": True},
                {"type": "Deleted", "author": "乙", "date": "", "text": "分别", "verified": True},
            ],
            "threads": [],
        }

    def test_relocation_cannot_authorize_a_replaced_document_or_complex_paragraph(self):
        seq = word_context.submit(self.review, self.receiver, self.fp, [self.para], "word-review", lambda: None)
        notes_store.handoff_claim(self.review, seq=seq)
        word_context.return_suggestions(
            self.review,
            seq,
            [{"n": 3, "from": "编号", "to": "序号", "reason": "与清单一致。"}],
            "Codex",
            "synthetic-session",
        )
        bound = SimpleNamespace(
            review=self.review,
            fingerprint=self.fp,
            locations={3: {"bodyParagraph": 3, "paraId": "1A000003"}},
            paragraphs={3: SimpleNamespace(text="共同登记样机编号。")},
            restrictions={3: {"revision"}},
        )
        self.assertNotIn("writeRestriction", word_context.suggestions(bound)[0])
        bound.fingerprint = "0" * 64
        self.assertIn("文档", word_context.suggestions(bound)[0]["writeRestriction"])
        bound.fingerprint = self.fp
        bound.restrictions = {3: {"revision", "field"}}
        self.assertIn("结构", word_context.suggestions(bound)[0]["writeRestriction"])
        bound.restrictions = {}
        bound.locations[3]["paraId"] = "1A000004"
        self.assertIn("段落标识", word_context.suggestions(bound)[0]["writeRestriction"])

    def test_recreated_preview_recovers_only_the_unacknowledged_identical_submission(self):
        binding = SimpleNamespace(
            review=self.review, receiver_id=self.receiver, fingerprint=self.fp, authorize_receiver=lambda: None
        )

        def send(key, receiver=self.receiver, doc_key="a", paragraphs=None):
            return word_context.submit(
                self.review, receiver, self.fp, paragraphs or [self.para], key, lambda: None, doc_key
            )

        original = send("first")
        notes_store.handoff_claim(self.review, seq=original)
        word_context.return_suggestions(self.review, original, [], "Codex", "synthetic-session")
        self.assertEqual(send("regenerated"), original)
        self.assertNotEqual(send("other-receiver", receiver="b"), original)
        self.assertNotEqual(send("other-file", doc_key="c"), original)
        self.assertNotEqual(send("other-selection", paragraphs=[{**self.para, "current": "编号"}]), original)
        with self.assertRaisesRegex(ValueError, "receipt-changed"):
            word_context.acknowledge(binding, "c", original)
        self.assertEqual(send("still-unacknowledged"), original)
        word_context.acknowledge(binding, "a", original)
        self.assertNotEqual(send("intentional-repeat"), original)

    def test_old_pending_claimed_and_returned_word_work_survive_handoff_history_limit(self):
        def send(n):
            seq = word_context.submit(
                self.review, self.receiver, self.fp, [{**self.para, "n": n}], f"request-{n}", lambda: None, "file"
            )
            bound = SimpleNamespace(
                review=self.review, receiver_id=self.receiver, fingerprint=self.fp, authorize_receiver=lambda: None
            )
            word_context.acknowledge(bound, "file", seq)
            return seq

        returned, claimed, pending = (send(n) for n in (1, 2, 3))
        notes_store.handoff_claim(self.review, seq=returned)
        [suggestion] = word_context.return_suggestions(
            self.review,
            returned,
            [{"n": 1, "from": "编号", "to": "序号", "reason": "清单用词"}],
            "Codex",
            "synthetic-session",
        )
        notes_store.handoff_claim(self.review, seq=claimed)
        doc = notes_store.handoff_load(self.review)
        # Seed finished history at the limit, then cross it via an actual handoff.
        for i in range(notes_store.HANDOFF_KEEP):
            doc["seq"] += 1
            doc["items"].append({"seq": doc["seq"], "status": "done"})
        (self.review / notes_store.HANDOFF_FILE).write_text(json.dumps(doc))
        notes_store.handoff_submit(self.review, "浏览器批次")
        batches = notes_store.handoff_load(self.review)["items"]
        self.assertEqual([b["seq"] for b in batches[:3]], [returned, claimed, pending])
        self.assertEqual(len(batches), notes_store.HANDOFF_KEEP + 3)
        self.assertEqual(batches[0]["wordSuggestions"], [suggestion])
        self.assertEqual(batches[1]["status"], "claimed")
        self.assertEqual(batches[2]["status"], "pending")
        word_context.withdraw(self.review, suggestion["id"], lambda: None)
        self.assertNotIn(returned, [b["seq"] for b in notes_store.handoff_load(self.review)["items"]])

    def test_receiver_claim_contains_only_selected_word_material(self):
        notes_store.handoff_submit(self.review, "浏览器私有旧批次")
        result = word_context.submit(self.review, self.receiver, self.fp, [self.para], "word-submit-a", lambda: None)
        self.assertEqual(result, 2)
        claimed = notes_store.handoff_claim(self.review, seq=result)
        self.assertEqual(claimed["wordSubmission"]["paragraphs"], [self.para])
        self.assertEqual(claimed["openIds"], [])
        self.assertNotIn("浏览器私有", str(claimed))
        self.assertEqual(
            word_context.submit(self.review, self.receiver, self.fp, [self.para], "word-submit-a", lambda: None), 2
        )
        self.assertEqual(notes_store.handoff_submit(self.review, "下一批浏览器意见")[1]["seq"], 3)

    def test_only_claiming_receiver_can_return_and_withdraw_keeps_source(self):
        seq = word_context.submit(self.review, self.receiver, self.fp, [self.para], "word-a", lambda: None)
        notes_store.handoff_claim(self.review, seq=seq)
        changes = [{"n": 3, "from": "编号", "to": "序号", "reason": "与清单用词一致。"}]
        with self.assertRaisesRegex(ValueError, "another-session"):
            word_context.return_suggestions(self.review, seq, changes, "Codex", "other-session")
        entries = word_context.return_suggestions(self.review, seq, changes, "Codex", "synthetic-session")
        self.assertEqual((entries[0]["reason"], entries[0]["sourceBatch"]), ("与清单用词一致。", seq))
        self.assertEqual(
            word_context.return_suggestions(self.review, seq, changes, "Codex", "synthetic-session"), entries
        )
        word_context.withdraw(self.review, entries[0]["id"], lambda: None)
        batch = notes_store.handoff_load(self.review)["items"][0]
        self.assertEqual(batch["wordSuggestions"][0]["status"], "withdrawn")
        self.assertEqual(batch["wordSubmission"]["paragraphs"], [self.para])

    def test_changed_receiver_cannot_collect_previously_selected_material(self):
        seq = word_context.submit(self.review, self.receiver, self.fp, [self.para], "word-rebind", lambda: None)
        wakeup.bind_wait(self.review, "Codex", "synthetic-other", replace=True)
        self.assertIsNone(notes_store.handoff_claim(self.review, seq=seq))
        word_context.recall(self.review, seq, lambda: None)
        self.assertEqual(notes_store.handoff_load(self.review)["items"][0]["status"], "void")

    def test_scoped_native_evidence_reconciles_empty_deletions_and_ambiguous_authorship(self):
        import json
        import shutil
        import subprocess

        node = shutil.which("node")
        if not node:
            self.skipTest("Word evidence adapter uses JavaScript; Node unavailable")
        adapter = ROOT / "skill/annotation-everything/word-taskpane/word-evidence.js"
        script = """
            const read = require(process.argv[1]);
            const {api, xml, changes} = JSON.parse(process.argv[2]);
            process.stdout.write(JSON.stringify(read(api, xml, changes)));
        """

        def read(api, xml, changes):
            data = json.dumps({"api": api, "xml": xml, "changes": changes}, ensure_ascii=False)
            return json.loads(subprocess.check_output([node, "-e", script, str(adapter), data]))

        native = {
            "inside": True,
            "date": "",
            "type": "Deleted",
            "author": "甲",
            "text": "",
            "range": {"original": "及备用件", "current": ""},
        }
        p = read({"original": "含样机及备用件", "current": "含样机", "text": "含样机"}, None, [native])
        self.assertEqual((p["revisions"][0]["author"], p["revisions"][0]["text"]), ("甲", "及备用件"))
        replaced = read(
            {"original": "分别登记", "current": "共同分别登记", "text": "共同登记"},
            {
                "original": "分别登记",
                "current": "共同登记",
                "revisions": [
                    {"type": "Added", "author": "乙", "text": "共同"},
                    {"type": "Deleted", "author": "甲", "text": "分别"},
                ],
            },
            [
                {**native, "type": "Added", "author": "乙", "range": {"current": "共同", "original": ""}},
                {**native, "range": {"original": "分别", "current": ""}},
            ],
        )
        self.assertEqual(replaced["current"], "共同登记")
        self.assertEqual([(r["author"], r["text"]) for r in replaced["revisions"]], [("乙", "共同"), ("甲", "分别")])
        ambiguous = read(
            {"original": "传真备份", "current": "", "text": ""},
            None,
            [
                {**native, "range": {"original": "", "current": ""}},
                {**native, "author": "乙", "range": {"original": "", "current": ""}},
            ],
        )
        self.assertTrue(all(not r["verified"] and r["text"] == "" for r in ambiguous["revisions"]))
