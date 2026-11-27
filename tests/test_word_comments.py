"""Word 批注操作的持久回执，全部内容来自合成样例。"""

from concurrent.futures import ThreadPoolExecutor
import hashlib
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
import threading
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "skill/annotation-everything/scripts"))


class WordCommentOperations(unittest.TestCase):
    def setUp(self):
        import word_comments

        self.handle = word_comments.handle
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        review = Path(temp.name)
        self.binding = SimpleNamespace(review=review, rid="r-synthetic", document=review / "contract.docx")
        self.intent = {
            "kind": "publish",
            "target": "",
            "location": "a" * 64,
            "bodyHash": "b" * 64,
            "preview": "请核对表格期限",
        }

    def call(self, action, **data):
        return self.handle(self.binding, "/api/comment-operations/" + action, data)

    def proof(self, permit):
        return {"id": hashlib.sha256(permit.encode("ascii")).hexdigest(), "permit": permit}

    def test_lost_receipt_never_issues_second_write_permission_after_reopen(self):
        started = self.call("begin", **self.intent)
        self.assertTrue(started["mayWrite"])
        reopened = self.call("list")["operations"]
        self.assertEqual(len(reopened), 1)
        self.assertEqual(reopened[0]["id"], started["id"])
        self.assertIsNone(reopened[0]["receipt"])
        with self.assertRaisesRegex(ValueError, "wrong-comment-permit"):
            self.call("cancel", id=started["id"], permit="f" * 32)
        self.assertEqual(self.call("begin", **self.intent), {"mayWrite": False, "id": started["id"]})

    def test_confirmed_reply_keeps_word_ids_and_cannot_be_replayed_on_that_thread(self):
        reply = dict(self.intent, kind="reply", target="synthetic-thread")
        started = self.call("begin", **reply)
        receipt = {
            "id": started["id"],
            "threadId": reply["target"],
            "replyId": "synthetic-reply",
            "stamp": "c" * 64,
        }
        with self.assertRaisesRegex(ValueError, "wrong-comment-receipt"):
            self.call("confirm", **dict(receipt, threadId="different-thread"))
        self.call("confirm", **receipt)
        self.call("confirm", **receipt)
        self.assertEqual(self.call("list")["operations"][0]["receipt"]["replyId"], "synthetic-reply")
        self.assertFalse(self.call("begin", **reply)["mayWrite"])
        self.assertTrue(self.call("begin", **dict(reply, target="another-thread"))["mayWrite"])

    def test_racing_panes_receive_one_write_permission(self):
        gate = threading.Barrier(2)
        proofs = [self.proof(char * 32) for char in "12"]

        def begin(proof):
            gate.wait(timeout=3)
            return self.call("begin", **self.intent, permitHash=proof["id"])

        with ThreadPoolExecutor(max_workers=2) as pool:
            pair = list(pool.map(begin, proofs))
        self.assertEqual({result["mayWrite"] for result in pair}, {True, False})
        self.assertEqual(pair[0]["id"], pair[1]["id"])
        winner, loser = proofs if pair[0]["mayWrite"] else reversed(proofs)
        with self.assertRaisesRegex(ValueError, "wrong-comment-permit"):
            self.call("cancel", id=winner["id"], permit=loser["permit"])
        self.call("cancel", **loser)
        self.assertEqual([op["id"] for op in self.call("list")["operations"]], [winner["id"]])
        self.call("cancel", **winner)
        self.assertEqual(self.call("list")["operations"], [])

    def test_only_unused_attempt_is_released_and_delayed_messages_cannot_affect_its_retry(self):
        first, second = (self.proof(pair * 16) for pair in ("a1", "b2"))
        self.assertTrue(self.call("begin", **self.intent, permitHash=first["id"])["mayWrite"])
        self.assertNotIn(first["permit"], str(self.call("list")))
        self.call("cancel", **first)
        self.assertTrue(self.call("begin", **self.intent, permitHash=second["id"])["mayWrite"])
        self.call("cancel", **first)
        self.assertEqual(self.call("list")["operations"][0]["id"], second["id"])
        self.assertFalse(self.call("begin", **self.intent, permitHash=first["id"])["mayWrite"])
        with self.assertRaisesRegex(ValueError, "no-comment-operation"):
            self.call("confirm", id=first["id"], threadId="synthetic", replyId="", stamp="c" * 64)
        self.call("confirm", id=second["id"], threadId="synthetic", replyId="", stamp="c" * 64)
        with self.assertRaisesRegex(ValueError, "comment-permission-spent"):
            self.call("cancel", **second)
        self.assertFalse(self.call("begin", **self.intent)["mayWrite"])

    def test_lost_begin_response_can_be_cancelled_even_if_requests_arrive_out_of_order(self):
        proof = self.proof("c" * 32)
        self.call("cancel", **proof)
        self.assertFalse(self.call("begin", **self.intent, permitHash=proof["id"])["mayWrite"])
        self.assertEqual(self.call("list")["operations"], [])
        self.assertTrue(self.call("begin", **self.intent, permitHash=self.proof("d" * 32)["id"])["mayWrite"])

    def test_saved_receipt_does_not_block_a_second_valid_selection(self):
        first = self.call("begin", **self.intent)
        self.call("confirm", id=first["id"], threadId="first", replyId="", stamp="c" * 64)
        self.assertFalse(self.call("begin", **self.intent)["mayWrite"])
        self.assertTrue(self.call("begin", **dict(self.intent, location="c" * 64))["mayWrite"])


class WordCommentPane(unittest.TestCase):
    @unittest.skipUnless(shutil.which("node"), "JavaScript engine required")
    def test_native_selection_thread_lifecycle_and_lost_receipt(self):
        root = Path(__file__).resolve().parents[1]
        subprocess.run(
            [
                shutil.which("node"),
                root / "tests/fixtures/word_comments.cjs",
                root / "skill/annotation-everything/word-taskpane/comments.pane.html",
            ],
            check=True,
            timeout=15,
        )
