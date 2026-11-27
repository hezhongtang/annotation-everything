"""Word 修订逐项写入许可和回执。素材全部为合成数据。"""

from concurrent.futures import ThreadPoolExecutor
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
            document_keys={"bound-key"},
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
