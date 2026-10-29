"""ae.py 命令行的外部行为测试；技能与仓库均为合成环境，绝不触碰真实家目录。"""
from __future__ import annotations

import hashlib
import json
import os
import re
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
AE = ROOT / "ae.py"
SKILL_SRC = ROOT / "skill" / "annotation-everything"
SERVE_PY = SKILL_SRC / "scripts" / "serve.py"
RECORD_NAME = ".install-record.json"


def read_version():
    text = SERVE_PY.read_text(encoding="utf-8")
    match = re.search(r'^VERSION = "([^"]+)"', text, re.M)
    if not match:
        raise AssertionError("serve.py 里找不到 VERSION 常量")
    return match.group(1)


def snapshot(root: Path):
    """目录内容快照：相对路径 -> 内容哈希；排除安装记录与缓存。"""
    entries = {}
    for path in sorted(root.rglob("*")):
        if path.is_dir():
            continue
        rel = path.relative_to(root).as_posix()
        if rel == RECORD_NAME or "__pycache__" in path.parts or rel.endswith((".pyc", ".DS_Store")):
            continue
        entries[rel] = hashlib.sha256(path.read_bytes()).hexdigest()
    return entries


class AeCliTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="ae-cli-test-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.home = self.root / "home"
        self.home.mkdir()
        self.skills = self.home / ".claude" / "skills"
        self.env = {**os.environ, "HOME": str(self.home), "CLAUDE_SKILLS_DIR": str(self.skills),
                    "PYTHONDONTWRITEBYTECODE": "1"}

    def run_ae(self, *args: str):
        return subprocess.run([sys.executable, str(AE), *args],
                              env=self.env, capture_output=True, text=True, timeout=60)

    @property
    def copy_dir(self) -> Path:
        return self.skills / "annotation-everything"

    def test_install_copies_skill_into_claude_skills_dir(self):
        p = self.run_ae("install")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertTrue((self.copy_dir / "SKILL.md").is_file(), "副本里有 SKILL.md")
        self.assertTrue((self.copy_dir / "scripts" / "serve.py").is_file(), "副本里有 serve.py")
        self.assertTrue((self.copy_dir / "evals" / "build-review-page" / "prompt.md").is_file())
        self.assertEqual(snapshot(self.copy_dir), snapshot(SKILL_SRC), "副本内容与技能本体一致")

    def test_install_skips_cache_files(self):
        cache = SKILL_SRC / "scripts" / "__pycache__"
        cache.mkdir(exist_ok=True)
        (cache / "serve.cpython-312.pyc").write_bytes(b"stale bytecode")
        marker = SKILL_SRC / "scripts" / ".DS_Store"
        marker.write_bytes(b"junk")
        self.addCleanup(marker.unlink)
        self.addCleanup(lambda: _rmtree(cache))
        p = self.run_ae("install")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertFalse((self.copy_dir / "scripts" / "__pycache__").exists(), "不复制 __pycache__")
        self.assertFalse(list(self.copy_dir.rglob("*.pyc")), "不复制 .pyc")
        self.assertFalse(list(self.copy_dir.rglob(".DS_Store")), "不复制 .DS_Store")

    def test_install_is_idempotent(self):
        self.assertEqual(self.run_ae("install").returncode, 0)
        first = snapshot(self.copy_dir)
        (self.copy_dir / "extra.txt").write_text("手工改动", encoding="utf-8")
        (self.copy_dir / "SKILL.md").write_text("手工覆盖", encoding="utf-8")
        p = self.run_ae("install")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertEqual(snapshot(self.copy_dir), first, "重复安装后副本与首次一致")

    def test_install_writes_record(self):
        p = self.run_ae("install")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        record = json.loads((self.copy_dir / RECORD_NAME).read_text(encoding="utf-8"))
        self.assertEqual(record["repo"], str(ROOT))
        self.assertRegex(record["commit"], r"^[0-9a-f]{40}$")
        self.assertEqual(record["version"], read_version())
        self.assertEqual(record["links"], [])
        self.assertRegex(record["installed_at"], r"^\d{4}-\d{2}-\d{2}T")

    def test_missing_subcommand_is_command_error(self):
        p = self.run_ae()
        self.assertEqual(p.returncode, 2)
        p = self.run_ae("no-such-subcommand")
        self.assertEqual(p.returncode, 2)

    def doctor_json(self, *extra):
        p = self.run_ae("doctor", "--json", *extra)
        return p, json.loads(p.stdout)

    def test_doctor_passes_after_install(self):
        self.assertEqual(self.run_ae("install").returncode, 0)
        p, doc = self.doctor_json()
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertEqual(doc["summary"], {"pass": 5, "warn": 0, "fail": 0})
        self.assertTrue(all(r["category"] == "安装状态" for r in doc["results"]))

    def test_doctor_fails_when_copy_missing(self):
        p, doc = self.doctor_json()
        self.assertEqual(p.returncode, 1)
        self.assertEqual(doc["summary"]["fail"], 1)
        failing = [r for r in doc["results"] if r["level"] == "失败"]
        self.assertEqual(failing[0]["code"], "skill-copy-present")
        self.assertIn("ae.py install", failing[0]["fix_command"])

    def test_doctor_fails_when_copy_modified(self):
        self.assertEqual(self.run_ae("install").returncode, 0)
        (self.copy_dir / "手工改动.txt").write_text("多余文件", encoding="utf-8")
        p, doc = self.doctor_json()
        self.assertEqual(p.returncode, 1)
        failing = {r["code"]: r for r in doc["results"] if r["level"] == "失败"}
        self.assertIn("skill-copy-sync", failing)
        self.assertIn("ae.py install", failing["skill-copy-sync"]["fix_command"])

    def test_doctor_fails_when_record_version_differs_from_repo(self):
        self.assertEqual(self.run_ae("install").returncode, 0)
        record_path = self.copy_dir / RECORD_NAME
        record = json.loads(record_path.read_text(encoding="utf-8"))
        record["version"] = "0.0.1"
        record_path.write_text(json.dumps(record, ensure_ascii=False), encoding="utf-8")
        p, doc = self.doctor_json()
        self.assertEqual(p.returncode, 1)
        failing = {r["code"]: r for r in doc["results"] if r["level"] == "失败"}
        self.assertEqual(list(failing), ["skill-copy-version"])
        self.assertIn("ae.py install", failing["skill-copy-version"]["fix_command"])

    def test_doctor_json_structure(self):
        self.assertEqual(self.run_ae("install").returncode, 0)
        p, doc = self.doctor_json()
        self.assertEqual(p.returncode, 0)
        for r in doc["results"]:
            self.assertEqual(set(r), {"code", "category", "level", "message", "fix_command"})
        self.assertEqual(set(doc["summary"]), {"pass", "warn", "fail"})

    def test_doctor_text_output_groups_by_category(self):
        self.assertEqual(self.run_ae("install").returncode, 0)
        p = self.run_ae("doctor")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertIn("安装状态", p.stdout)
        self.assertIn("汇总", p.stdout)



def _rmtree(path: Path):
    import shutil
    shutil.rmtree(path, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
