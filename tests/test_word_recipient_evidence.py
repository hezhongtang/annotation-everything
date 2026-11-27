"""合成收件人验收文件的只读辅助核对入口。"""

from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from zipfile import ZipFile

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "fixtures" / "docx"
sys.path.insert(0, str(FIXTURES))
import make_word_native


class RecipientEvidenceTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.original = make_word_native.build(Path(tmp.name) / "original.docx", "A")
        self.copy = self.original.with_name("copy.docx")

    def check_copy(self, path, expected, *options):
        return subprocess.run(
            [sys.executable, str(FIXTURES / "check_word_recipient.py"), str(path), "--expect", expected, *options],
            capture_output=True,
            text=True,
            timeout=10,
        )

    def rewrite(self, part, old, new):
        with ZipFile(self.original) as source, ZipFile(self.copy, "w") as output:
            for name in source.namelist():
                data = source.read(name)
                if name == part:
                    self.assertIn(old.encode(), data)
                    data = data.replace(old.encode(), new.encode(), 1)
                output.writestr(name, data)

    def test_recipient_text_and_protected_thread_are_checked_independently(self):
        result = self.check_copy(self.original, "original")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.rewrite("word/document.xml", "验收异议", "交付异议")
        result = self.check_copy(self.copy, "accepted")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.check_copy(self.copy, "rejected").returncode, 1)
        self.assertEqual(self.check_copy(self.copy, "accepted", "--reply", "未发表的回复").returncode, 1)

    def test_tracked_replacement_accepts_word_splitting_the_marks(self):
        target_run = '<w:r><w:t xml:space="preserve">验收异议</w:t></w:r>'
        split_marks = "".join(
            f'<w:{kind} w:author="审阅人"><w:r><w:{tag}>{text}</w:{tag}></w:r></w:{kind}>'
            for kind, tag, text in (("del", "delText", "验收"), ("ins", "t", "交"), ("ins", "t", "付"))
        )
        self.rewrite("word/document.xml", target_run, split_marks + "<w:r><w:t>异议</w:t></w:r>")
        result = self.check_copy(self.copy, "tracked")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.check_copy(self.copy, "accepted").returncode, 1)

    def test_detects_old_thread_with_incorrect_range_or_detached_reply(self):
        start = '<w:commentRangeStart w:id="0"/>'
        prefix = '<w:r><w:t xml:space="preserve">应于</w:t></w:r>'
        self.rewrite("word/document.xml", prefix + start, start + prefix)
        self.assertEqual(self.check_copy(self.copy, "original").returncode, 1)
        self.rewrite("word/commentsExtended.xml", 'w15:paraIdParent="01000001"', 'w15:paraIdParent="01ABCDEF"')
        self.assertEqual(self.check_copy(self.copy, "original").returncode, 1)
