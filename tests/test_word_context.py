"""Selected synthetic Word material crosses the existing receiver queue."""

import hashlib
import io
import json
import os
import sys
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import redirect_stderr, redirect_stdout
from types import SimpleNamespace
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "skill/annotation-everything/scripts"))

import notes as notes_cli
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

    def test_rebuilt_word_suggestion_requires_the_same_document_digest_and_unique_selection(self):
        original = "样机编号、附件编号。"
        p = {**self.para, "digest": hashlib.sha256(original.encode()).hexdigest(), "current": "编号"}
        seq = word_context.submit(
            self.review, self.receiver, self.fp, [p], "before-save", lambda: None, "original-file"
        )
        notes_store.handoff_claim(self.review, seq=seq)
        word_context.return_suggestions(
            self.review,
            seq,
            [{"n": p["n"], "from": "编号", "to": "标识", "reason": "与清单一致。"}],
            "Codex",
            "synthetic-session",
        )
        for document_key, text, pid, refusal in (
            ("another-file", original, p["paraId"], "同一文档"),
            ("original-file", "样机编号。", p["paraId"], "原段落及送审选区"),
            ("original-file", original, p["paraId"], "原段落及送审选区"),
            ("original-file", original, None, "段落标识"),
        ):
            with self.subTest(document_key=document_key, text=text, para_id=pid):
                binding = SimpleNamespace(
                    review=self.review,
                    canonical_document_key=document_key,
                    fingerprint="f" * 64,
                    locations={4: {"bodyParagraph": 4, **({"paraId": pid} if pid else {})}},
                    paragraphs={4: SimpleNamespace(text=text)},
                    restrictions={},
                )
                [item] = word_context.suggestions(binding)
                self.assertIn(refusal, item["writeRestriction"])
                self.assertEqual(item["status"], "needs-relocation")

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

    def notes_command(self, command, *args):
        output, errors = io.StringIO(), io.StringIO()
        with (
            patch.dict(os.environ, AE_HOME=str(self.review / "ledger")),
            redirect_stdout(output),
            redirect_stderr(errors),
        ):
            code = notes_cli.main([command, str(self.review), *args])
        return code, output.getvalue(), errors.getvalue()

    def test_shared_handoff_views_only_publish_word_batch_metadata(self):
        thread = {
            "id": "selected-thread",
            "authorName": "合成讨论人",
            "creationDate": None,
            "content": "请核对样机清单。",
            "range": {"text": "编号"},
            "resolved": False,
            "replies": [
                {"id": "selected-reply", "authorName": "合成回复人", "creationDate": None, "content": "按附表核对。"}
            ],
        }
        revision = {**self.para["revisions"][0], "author": "合成修订人"}
        paragraph = {**self.para, "threads": [thread], "revisions": [revision, self.para["revisions"][1]]}
        change = {"n": 3, "from": "编号", "to": "样机序号", "reason": "复核采用附表的命名。"}
        seq = word_context.submit(self.review, self.receiver, self.fp, [paragraph], "private-submit", lambda: None)

        def check_state(state, status):
            self.assertEqual((state["latest"]["seq"], state["latest"]["status"]), (seq, status))
            self.assertEqual(state[status], 1)
            if status != "done":
                self.assertEqual(state["open"][0]["seq"], seq)
            encoded = json.dumps(state, ensure_ascii=False)
            for private in (
                "wordSubmission",
                "wordSuggestions",
                "wordReceipt",
                "wordAwaitingReceipt",
                "private-submit",
                self.receiver,
                self.fp,
                paragraph["original"],
                paragraph["current"],
                revision["author"],
                thread["authorName"],
                thread["replies"][0]["content"],
                change["to"],
                change["reason"],
            ):
                self.assertNotIn(private, encoded)

        check_state(notes_store.handoff_state(self.review, full=False), "pending")
        code, output, err = self.notes_command(
            "claim", "--seq", str(seq), "--harness", "Codex", "--session", "synthetic-session"
        )
        self.assertEqual(code, 0, err)
        self.assertEqual(json.loads(output)["wordSubmission"]["paragraphs"], [paragraph])
        check_state(notes_store.handoff_state(self.review), "claimed")
        [suggestion] = word_context.return_suggestions(self.review, seq, [change], "Codex", "synthetic-session")
        self.assertEqual(suggestion["reason"], change["reason"])
        check_state(notes_store.handoff_state(self.review, full=False), "done")
        for cmd in (("show", "--json"), ("show", "--batch", str(seq), "--json"), ("batches", "--json")):
            with self.subTest(cmd=cmd):
                code, output, err = self.notes_command(*cmd)
                self.assertEqual(code, 0, err)
                payload = json.loads(output)
                check_state(payload.get("handoff", payload), "done")

    def test_generic_done_preserves_pending_and_claimed_word_batches(self):
        first = word_context.submit(self.review, self.receiver, self.fp, [self.para], "old", lambda: None)
        new_receiver = wakeup.bind_wait(self.review, "Codex", "new-session", replace=True)["bindingId"]
        caller = ("--harness", "Codex", "--session", "new-session")
        handoff_file = self.review / notes_store.HANDOFF_FILE

        def check_refusal(seq):
            before = handoff_file.read_bytes()
            for target in ((), ("--seq", str(seq))):
                with self.subTest(seq=seq, target=target):
                    code, _, err = self.notes_command("done", *target, *caller, "--locale", "en", "--summary", "done")
                    self.assertEqual(code, 1, err)
                    self.assertIn("word_context.py return", err)
                    self.assertEqual(handoff_file.read_bytes(), before)

        check_refusal(first)
        word_context.recall(self.review, first, lambda: None)
        second = word_context.submit(self.review, new_receiver, self.fp, [self.para], "new", lambda: None)
        code, _, err = self.notes_command("claim", "--seq", str(second), *caller)
        self.assertEqual(code, 0, err)
        check_refusal(second)
        source = self.review / "empty.json"
        source.write_text("[]", encoding="utf-8")
        result = io.StringIO()
        with redirect_stdout(result):
            code = word_context.main(
                ["return", str(self.review), "--batch", str(second), "--input", str(source), *caller]
            )
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(result.getvalue()), {"batch": second, "suggestions": []})
        self.assertEqual(notes_store.handoff_load(self.review)["items"][-1]["status"], "done")
        notes_store.handoff_submit(self.review, "普通批次")
        code, _, err = self.notes_command("done", *caller, "--summary", "普通批次已完成")
        self.assertEqual(code, 0, err)
        self.assertEqual(notes_store.handoff_state(self.review)["latest"]["summary"], "普通批次已完成")

    def test_recalled_unacknowledged_word_submission_can_be_submitted_again(self):
        def send(key):
            return word_context.submit(self.review, self.receiver, self.fp, [self.para], key, lambda: None, "file")

        original = send("original")
        self.assertEqual(send("recovered-before-recall"), original)
        self.assertEqual(
            word_context.recall(self.review, original, lambda: None), ["original", "recovered-before-recall"]
        )
        doc = notes_store.handoff_load(self.review)
        self.assertEqual(doc["items"][0]["status"], "void")
        self.assertNotIn("wordAwaitingReceipt", doc["items"][0])
        # Also exercise a stored recall from before the receipt flag was cleared.
        doc["items"][0]["wordAwaitingReceipt"] = True
        (self.review / notes_store.HANDOFF_FILE).write_text(json.dumps(doc), encoding="utf-8")
        with ThreadPoolExecutor(max_workers=2) as pool:
            fresh, recovered = pool.map(send, ("fresh", "recovered-after-recall"))
        self.assertNotEqual(fresh, original)
        self.assertEqual(recovered, fresh)
        bound = SimpleNamespace(
            review=self.review,
            receiver_id=self.receiver,
            fingerprint=self.fp,
            authorize_receiver=lambda: None,
            canonical_document_key="file",
        )
        with (
            patch.object(wakeup, "load_binding", return_value={"adapter": "codex-desktop"}),
            patch.object(wakeup, "deliver") as delivery,
        ):
            for key in ("original", "recovered-before-recall"):
                with self.assertRaisesRegex(word_context.ContextError, "word-batch-recalled"):
                    word_context.handle(bound, "/api/word/submit", {"submitId": key, "paragraphs": [self.para]})
            delivery.assert_not_called()
        with self.assertRaisesRegex(word_context.ContextError, "word-batch-recalled"):
            word_context.acknowledge(bound, "file", original)
        self.assertIsNone(notes_store.handoff_claim(self.review, seq=original))
        self.assertEqual([b["status"] for b in notes_store.handoff_load(self.review)["items"]], ["void", "pending"])
        with patch.object(notes_store, "HANDOFF_KEEP", 1):
            word_context.acknowledge(bound, "file", fresh)
        self.assertEqual([b["seq"] for b in notes_store.handoff_load(self.review)["items"]], [fresh])
        for key in ("fresh", "recovered-after-recall"):
            self.assertEqual(send(key), fresh)
        self.assertEqual(notes_store.handoff_claim(self.review, seq=fresh)["status"], "claimed")

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
