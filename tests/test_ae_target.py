"""ae.py --target 的测试：技能副本装到指定目录，doctor / repair / uninstall 对其同样生效。

均为合成环境：HOME、CLAUDE_SKILLS_DIR、CODEX_HOME 与审阅台账（AE_HOME）全部指向临时目录，
绝不触碰真实家目录。CLI 用例子进程运行 ae.py；单元用例直接导入 ae。
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
AE = ROOT / "ae.py"
SKILL_SRC = ROOT / "skill" / "annotation-everything"
sys.path.insert(0, str(ROOT))
import ae  # noqa: E402

RECORD_NAME = ae.RECORD_NAME
SKILL = "annotation-everything"


def snapshot(root: Path) -> dict:
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


class TargetCliTests(unittest.TestCase):
    """命令行行为：子进程运行 ae.py，环境全部指向临时目录。"""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="ae-target-test-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.home = self.root / "home"
        self.home.mkdir()
        self.skills = self.home / ".claude" / "skills"
        self.target = self.root / "custom-skills"
        self.env = {**os.environ, "HOME": str(self.home), "CLAUDE_SKILLS_DIR": str(self.skills),
                    "CODEX_HOME": str(self.home / ".codex"),
                    "AE_HOME": str(self.root / "ledger"),     # 运行状态检查读台账：指向临时目录
                    "PYTHONDONTWRITEBYTECODE": "1"}

    def run_ae(self, *args):
        return subprocess.run([sys.executable, str(AE), *args],
                              env=self.env, capture_output=True, text=True, timeout=120)

    @property
    def default_copy(self) -> Path:
        return self.skills / SKILL

    @property
    def target_copy(self) -> Path:
        return self.target / SKILL

    def install_at_target(self, *extra):
        p = self.run_ae("install", "--target", str(self.target), *extra)
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)

    def doctor_json(self, *extra):
        p = self.run_ae("doctor", "--json", *extra)
        return p, json.loads(p.stdout)

    @staticmethod
    def install_results(doc) -> list:
        return [r for r in doc["results"] if r["category_id"] == "install"]

    def test_install_target_copies_skill_into_custom_dir(self):
        self.install_at_target()
        self.assertTrue((self.target_copy / "SKILL.md").is_file(), "副本应装到 --target 目录")
        self.assertTrue((self.target_copy / "scripts" / "serve.py").is_file())
        self.assertEqual(snapshot(self.target_copy), snapshot(SKILL_SRC), "副本内容与技能本体一致")
        self.assertFalse(self.default_copy.exists(), "缺省位置不应被写入")

    def test_install_target_writes_record(self):
        self.install_at_target()
        record = json.loads((self.target_copy / RECORD_NAME).read_text(encoding="utf-8"))
        self.assertEqual(record["repo"], str(ROOT))
        self.assertEqual(record["links"], [])

    def test_install_target_wins_over_claude_skills_dir(self):
        self.install_at_target()
        self.assertTrue(self.target_copy.is_dir(), "--target 显式指定时优先于 CLAUDE_SKILLS_DIR")
        self.assertFalse(self.default_copy.exists())

    def test_doctor_passes_at_target_after_install(self):
        self.install_at_target()
        p, doc = self.doctor_json("--target", str(self.target))
        install_items = self.install_results(doc)
        self.assertTrue(install_items, doc["results"])
        self.assertTrue(all(r["level"] == "通过" for r in install_items), doc["results"])

    def test_doctor_without_target_still_checks_default_location(self):
        self.install_at_target()
        p, doc = self.doctor_json()
        failing = [r["code"] for r in self.install_results(doc) if r["level"] == "失败"]
        self.assertEqual(failing, ["skill-copy-present"], "缺省位置没有副本，应如实报告")

    def test_doctor_missing_copy_fix_command_includes_target(self):
        p, doc = self.doctor_json("--target", str(self.target))
        failing = {r["code"]: r for r in self.install_results(doc) if r["level"] == "失败"}
        self.assertEqual(list(failing), ["skill-copy-present"])
        self.assertIn("--target", failing["skill-copy-present"]["fix_command"])
        self.assertIn(str(self.target), failing["skill-copy-present"]["fix_command"])

    def test_doctor_version_differs_fix_command_includes_target(self):
        self.install_at_target()
        record_path = self.target_copy / RECORD_NAME
        record = json.loads(record_path.read_text(encoding="utf-8"))
        record["version"] = "0.0.1"
        record_path.write_text(json.dumps(record, ensure_ascii=False), encoding="utf-8")
        p, doc = self.doctor_json("--target", str(self.target))
        failing = {r["code"]: r for r in self.install_results(doc) if r["level"] == "失败"}
        self.assertEqual(list(failing), ["skill-copy-version"])
        self.assertIn("--target", failing["skill-copy-version"]["fix_command"])
        self.assertIn(str(self.target), failing["skill-copy-version"]["fix_command"])

    def test_repair_resyncs_copy_at_target(self):
        self.install_at_target()
        (self.target_copy / "手工改动.txt").write_text("多余文件", encoding="utf-8")
        p = self.run_ae("repair", "--target", str(self.target))
        self.assertIn(p.returncode, (0, 1), p.stdout + p.stderr)
        self.assertEqual(snapshot(self.target_copy), snapshot(SKILL_SRC), "repair 应重新同步自定义位置的副本")
        p2, doc2 = self.doctor_json("--target", str(self.target))
        self.assertTrue(all(r["level"] == "通过" for r in self.install_results(doc2)), doc2["results"])

    def test_uninstall_removes_copy_and_links_at_target(self):
        self.install_at_target("--link", "codex")
        codex_link = self.home / ".codex" / "skills" / SKILL
        self.assertTrue(codex_link.is_symlink(), "内置 --link 指向自定义位置的副本")
        self.assertEqual(os.path.realpath(str(codex_link)), str(self.target_copy.resolve()))
        p = self.run_ae("uninstall", "--target", str(self.target))
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertFalse(self.target_copy.exists(), "自定义位置的副本应删除")
        self.assertFalse(codex_link.exists(), "指向它的链接应删除")

    def test_uninstall_target_without_install_is_hint_only(self):
        p = self.run_ae("uninstall", "--target", str(self.target))
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertIn("未安装", p.stdout)

    def test_install_target_overlapping_source_is_refused(self):
        p = self.run_ae("install", "--target", str(ROOT / "skill"))
        self.assertEqual(p.returncode, 2, p.stdout + p.stderr)
        self.assertFalse(self.default_copy.exists(), "拒绝时不应改写缺省位置")


class TargetUnitTests(unittest.TestCase):
    """单元行为：副本位置解析、修复命令、--target 进入体检上下文与 serve.py 识别。"""

    KEYS = ("HOME", "CLAUDE_SKILLS_DIR")

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="ae-target-unit-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.home = self.root / "home"
        self.home.mkdir()
        self._saved = {key: os.environ.get(key) for key in self.KEYS}
        os.environ.update({"HOME": str(self.home), "CLAUDE_SKILLS_DIR": str(self.home / ".claude" / "skills")})

    def tearDown(self):
        for key, value in self._saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    def test_resolve_copy_dir_default_and_custom(self):
        self.assertEqual(ae.resolve_copy_dir(None),
                         self.home / ".claude" / "skills" / SKILL, "缺省走 CLAUDE_SKILLS_DIR 或 ~/.claude/skills")
        custom = self.root / "anywhere"
        self.assertEqual(ae.resolve_copy_dir(custom), custom / SKILL)

    def test_resolve_copy_dir_expands_user(self):
        self.assertEqual(ae.resolve_copy_dir("~/ae-target-fixture"),
                         self.home / "ae-target-fixture" / SKILL)

    def test_install_fix_appends_target_only_when_given(self):
        self.assertEqual(ae.install_fix(None), "python3 ae.py install")
        self.assertEqual(ae.install_fix(Path("/repo")), "python3 /repo/ae.py install")
        self.assertEqual(ae.install_fix(Path("/repo"), Path("/dir with space")),
                         "python3 /repo/ae.py install --target '/dir with space'")

    def test_target_reaches_copy_check_fix_commands(self):
        target = self.root / "custom"
        ctx = ae.DoctorContext(skill_copy=target / SKILL, repo=ROOT, record=None, target=target)
        result = ae.check_copy_present(ctx)
        self.assertEqual(result.level, ae.LEVEL_FAIL)
        self.assertIn("--target", result.fix_command)
        self.assertIn(str(target), result.fix_command)

    def test_serve_script_index_trusts_extra_copy(self):
        custom_scripts = self.root / "custom" / SKILL / "scripts"
        custom_scripts.mkdir(parents=True)
        serve = custom_scripts / "serve.py"
        serve.write_text("# 合成脚本\n", encoding="utf-8")
        cmdline = f"python3 {serve} /review"
        self.assertIsNone(ae.serve_script_index(cmdline), "缺省不信任自定义副本的 serve.py")
        self.assertEqual(ae.serve_script_index(cmdline, extra_copy=custom_scripts.parent), 1)
        self.assertIsNone(ae.serve_script_index(cmdline, extra_copy=self.root / "other"), "别的目录仍不信任")

    def test_running_servers_with_extra_copy(self):
        custom_scripts = self.root / "custom" / SKILL / "scripts"
        custom_scripts.mkdir(parents=True)
        serve = custom_scripts / "serve.py"
        serve.write_text("# 合成脚本\n", encoding="utf-8")
        cmdline = f"python3 {serve} /review"
        fake = [(4321, cmdline)]
        self.assertEqual(ae.running_servers(lister=lambda: fake, extra_copy=custom_scripts.parent), fake)
        self.assertEqual(ae.running_servers(lister=lambda: fake), [], "缺省不把自定义副本的 serve.py 当本工具进程")


if __name__ == "__main__":
    unittest.main()
