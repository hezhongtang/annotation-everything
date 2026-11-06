"""许可证随仓库和技能副本一起交付。"""
import hashlib
import os
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SKILL_SRC = ROOT / "skill" / "annotation-everything"
AE = ROOT / "ae.py"
# apache.org 发布的 LICENSE-2.0.txt 的 SHA-256，改动原文会让 GitHub 和合规工具识别失败。
APACHE_2_SHA256 = "cfc7749b96f63bd31c3c42b5c471bf756814053e847c10f3eb003417bc523d30"


class LicenseFilesTests(unittest.TestCase):
    def test_root_license_is_unmodified_apache_2_text(self):
        digest = hashlib.sha256((ROOT / "LICENSE").read_bytes()).hexdigest()
        self.assertEqual(digest, APACHE_2_SHA256)

    def test_notice_names_project_and_copyright_holder(self):
        notice = (ROOT / "NOTICE").read_text(encoding="utf-8")
        self.assertIn("annotation-everything", notice)
        self.assertRegex(notice, r"Copyright 2026 hezhongtang")

    def test_skill_carries_identical_license_files(self):
        for name in ("LICENSE", "NOTICE"):
            with self.subTest(name=name):
                self.assertEqual((SKILL_SRC / name).read_bytes(), (ROOT / name).read_bytes())

    def test_skill_frontmatter_declares_license(self):
        text = (SKILL_SRC / "SKILL.md").read_text(encoding="utf-8")
        frontmatter = re.match(r"---\n(.*?)\n---\n", text, re.S)
        self.assertIsNotNone(frontmatter)
        self.assertRegex(frontmatter.group(1), r"(?m)^license: Apache-2\.0$")

    def test_install_delivers_license_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            skills = root / "home" / ".claude" / "skills"
            skills.mkdir(parents=True)
            env = {**os.environ, "HOME": str(root / "home"), "CLAUDE_SKILLS_DIR": str(skills),
                   "AE_HOME": str(root / "ledger"), "PYTHONDONTWRITEBYTECODE": "1"}
            result = subprocess.run([sys.executable, str(AE), "install"], env=env,
                                    capture_output=True, text=True, timeout=60)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            for name in ("LICENSE", "NOTICE"):
                with self.subTest(name=name):
                    self.assertTrue((skills / "annotation-everything" / name).is_file())

    def test_both_readmes_link_license_and_notice(self):
        for name in ("README.md", "README.zh-CN.md"):
            text = (ROOT / name).read_text(encoding="utf-8")
            with self.subTest(name=name):
                self.assertIn("Apache-2.0", text)
                self.assertIn("(LICENSE)", text)
                self.assertIn("(NOTICE)", text)


if __name__ == "__main__":
    unittest.main()
