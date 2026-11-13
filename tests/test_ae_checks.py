"""ae.py 检查项函数的测试：直接导入，构造状态，断言返回记录的级别与修复命令。"""
from __future__ import annotations

import json
from pathlib import Path
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import ae  # noqa: E402

PASS, WARN, FAIL = ae.LEVEL_PASS, ae.LEVEL_WARN, ae.LEVEL_FAIL
FIX_INSTALL = "ae.py install"


def make_repo(root: Path, version: str = "9.9.9") -> Path:
    """构造一个带最小技能本体的合成仓库。"""
    skill = root / "skill" / "annotation-everything"
    (skill / "scripts").mkdir(parents=True)
    (skill / "SKILL.md").write_text("---\nname: annotation-everything\n---\n合成技能\n", encoding="utf-8")
    (skill / "scripts" / "serve.py").write_text(f'VERSION = "{version}"\n', encoding="utf-8")
    return root


class CheckFunctionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="ae-check-test-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.repo = make_repo(self.root / "repo")
        self.copy = self.root / "skills" / "annotation-everything"
        self.record = {"repo": str(self.repo), "commit": "0" * 40, "version": "9.9.9",
                       "installed_at": "2026-10-29T00:00:00+08:00", "links": []}

    def install_copy(self):
        ae.copy_skill(self.repo / "skill" / "annotation-everything", self.copy)
        (self.copy / ae.RECORD_NAME).write_text(json.dumps(self.record), encoding="utf-8")

    def ctx(self, **overrides):
        values = {"skill_copy": self.copy, "repo": self.repo, "record": self.record}
        values.update(overrides)
        return ae.DoctorContext(**values)

    def test_python_version_passes_on_modern_interpreter(self):
        self.assertEqual(ae.check_python(self.ctx()).level, PASS)

    def test_copy_present_fails_without_fix_when_missing(self):
        result = ae.check_copy_present(self.ctx())
        self.assertEqual(result.level, FAIL)
        self.assertTrue(result.fix_command.rstrip().endswith(FIX_INSTALL), result.fix_command)
        self.assertEqual(result.code, "skill-copy-present")
        self.assertEqual(result.category, "安装状态")

    def test_copy_present_passes_when_installed(self):
        self.install_copy()
        self.assertEqual(ae.check_copy_present(self.ctx()).level, PASS)

    def test_copy_sync_passes_when_identical(self):
        self.install_copy()
        self.assertEqual(ae.check_copy_sync(self.ctx()).level, PASS)

    def test_copy_sync_fails_on_local_modification_with_fix(self):
        self.install_copy()
        (self.copy / "SKILL.md").write_text("手工改动", encoding="utf-8")
        result = ae.check_copy_sync(self.ctx())
        self.assertEqual(result.level, FAIL)
        self.assertEqual(result.code, "skill-copy-sync")
        self.assertTrue(result.fix_command.rstrip().endswith(FIX_INSTALL), result.fix_command)

    def test_copy_sync_fails_on_extra_file_in_copy(self):
        self.install_copy()
        (self.copy / "手工.txt").write_text("多余文件", encoding="utf-8")
        self.assertEqual(ae.check_copy_sync(self.ctx()).level, FAIL)

    def test_copy_sync_skips_comparison_when_copy_missing(self):
        self.assertEqual(ae.check_copy_sync(self.ctx(skill_copy=self.root / "nope")).level, PASS)

    def test_copy_sync_skips_comparison_when_repo_unavailable(self):
        self.install_copy()
        self.assertEqual(ae.check_copy_sync(self.ctx(repo=None)).level, PASS)

    def test_copy_sync_ignores_cache_and_record_files(self):
        self.install_copy()
        cache = self.copy / "scripts" / "__pycache__"
        cache.mkdir()
        (cache / "serve.pyc").write_bytes(b"bytecode")
        (self.copy / "scripts" / ".DS_Store").write_bytes(b"junk")
        self.assertEqual(ae.check_copy_sync(self.ctx()).level, PASS)

    def test_copy_version_passes_when_record_matches_repo(self):
        self.install_copy()
        self.assertEqual(ae.check_copy_version(self.ctx()).level, PASS)

    def test_copy_version_fails_when_repo_upgraded(self):
        self.install_copy()
        result = ae.check_copy_version(self.ctx(record={**self.record, "version": "0.1.0"}))
        self.assertEqual(result.level, FAIL)
        self.assertEqual(result.code, "skill-copy-version")
        self.assertTrue(result.fix_command.rstrip().endswith(FIX_INSTALL), result.fix_command)

    def test_copy_version_skips_comparison_without_record_or_repo(self):
        self.install_copy()
        self.assertEqual(ae.check_copy_version(self.ctx(record=None)).level, PASS)
        self.assertEqual(ae.check_copy_version(self.ctx(repo=None)).level, PASS)

    def test_check_result_fields(self):
        result = ae.check_python(self.ctx())
        self.assertEqual(
            {f.name for f in __import__("dataclasses").fields(result)},
            {"code", "category", "level", "message", "fix_command"})


class OverviewHomeDoctorTests(unittest.TestCase):
    """体检跳过总览首页：首页进程不进审阅服务计数，也不算孤儿审阅。"""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="ae-home-doctor-")
        self.addCleanup(self.temp.cleanup)
        self.ae_home = Path(self.temp.name) / "ledger"
        self.ae_home.mkdir(parents=True)
        (self.ae_home / "reviews.json").write_text(json.dumps({"version": 1, "reviews": {}}), encoding="utf-8")
        self.serve = ROOT / "skill" / "annotation-everything" / "scripts" / "serve.py"
        self.home_line = f"python3 {self.serve} overview --no-open"

    def ctx(self, lister):
        return ae.DoctorContext(skill_copy=Path(self.temp.name) / "copy", repo=None, record=None,
                                ledger_home=self.ae_home, process_lister=lister,
                                port_probe=lambda port: None)

    def test_find_orphan_servers_skips_overview_home(self):
        self.assertEqual(ae.find_orphan_servers({}, lambda: [(99, self.home_line)], probe=lambda port: None), [],
                         "总览首页不是孤儿审阅，孤儿修复也不该碰它")

    def test_check_orphan_servers_skips_overview_home_in_count(self):
        result = ae.check_orphan_servers(self.ctx(lambda: [(99, self.home_line)]))
        self.assertEqual(result.level, PASS, result.message)
        self.assertNotIn("99", result.message, "首页进程不进审阅服务计数")


if __name__ == "__main__":
    unittest.main()
