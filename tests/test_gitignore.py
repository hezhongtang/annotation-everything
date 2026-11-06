"""忽略规则：真实材料、审阅产物和本机状态不入库，合成样例不被误伤。"""
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
EVAL_REVIEW = "skill/annotation-everything/evals/apply-notes-and-resolve/files/review"

MUST_IGNORE = (
    "material/slides.ppsx", "material/brief.docm", "material/model.xlsb", "material/plan.odt",
    "material/sheet.ods", "material/deck.odp", "material/memo.rtf", "material/table.tsv",
    "material/report.wps", "material/report.ofd",
    "work/review/notes.json", "work/review/analysis/a1/events.jsonl",
    "work/review/analysis/counter.json", "work/review/.annotation-archived",
    "work/report-record.json",
    ".env.local", "deploy.p8", "server.ppk", "id_ecdsa", ".netrc", ".pypirc",
    ".zcode/config.json", ".claude/worktrees/task/file.txt", ".claude/settings.local.json",
    ".codegraph/codegraph.db", ".ae-home/reviews.json",
)
MUST_KEEP = (
    "docs/analysis/acceptance.md", ".env.example", f"{EVAL_REVIEW}/notes.json",
    f"{EVAL_REVIEW}/notes.jsonl", "fixtures/sample/report.html", "LICENSE", "NOTICE",
    ".github/workflows/ci.yml",
)


def ignored(path: str) -> bool:
    result = subprocess.run(["git", "-C", str(ROOT), "check-ignore", "-q", "--no-index", path],
                            capture_output=True, text=True)
    if result.returncode not in (0, 1):
        raise AssertionError(result.stderr)
    return result.returncode == 0


class GitignoreTests(unittest.TestCase):
    def test_sensitive_and_local_paths_are_ignored(self):
        for path in MUST_IGNORE:
            with self.subTest(path=path):
                self.assertTrue(ignored(path))

    def test_synthetic_and_project_files_stay_tracked(self):
        for path in MUST_KEEP:
            with self.subTest(path=path):
                self.assertFalse(ignored(path))

    def test_no_tracked_file_matches_an_ignore_rule(self):
        result = subprocess.run(["git", "-C", str(ROOT), "ls-files", "-ci", "--exclude-standard"],
                                capture_output=True, text=True, check=True)
        self.assertEqual(result.stdout, "")

    def test_each_rule_is_listed_once(self):
        rules = [line.strip() for line in (ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()
                 if line.strip() and not line.startswith("#")]
        self.assertEqual(sorted(rules), sorted(set(rules)))


if __name__ == "__main__":
    unittest.main()
