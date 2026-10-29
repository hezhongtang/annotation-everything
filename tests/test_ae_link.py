"""ae.py install --link 与链接体检的测试；均为合成环境，绝不触碰真实家目录。"""
from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
AE = ROOT / "ae.py"
sys.path.insert(0, str(ROOT))
import ae  # noqa: E402

RECORD_NAME = ae.RECORD_NAME
SKILL = "annotation-everything"
AGENTS = ("codex", "agents", "zcode")


def agent_link(home: Path, agent: str) -> Path:
    """Agent 技能目录下同名链接的位置；目录约定对齐 skill_sources.default_roots。"""
    if agent == "codex":
        return home / ".codex" / "skills" / SKILL
    if agent == "agents":
        return home / ".agents" / "skills" / SKILL
    return home / ".zcode" / "skills" / SKILL


class AeLinkCliTests(unittest.TestCase):
    """命令行行为：子进程运行 ae.py，HOME 与各技能目录全部指向临时目录。"""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="ae-link-test-")
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name) / "home"
        self.home.mkdir()
        self.skills = self.home / ".claude" / "skills"
        self.env = {**os.environ, "HOME": str(self.home),
                    "CLAUDE_SKILLS_DIR": str(self.skills),
                    "CODEX_HOME": str(self.home / ".codex"),
                    "PYTHONDONTWRITEBYTECODE": "1"}

    def run_ae(self, *args):
        return subprocess.run([sys.executable, str(AE), *args],
                              env=self.env, capture_output=True, text=True, timeout=60)

    @property
    def copy_dir(self) -> Path:
        return self.skills / SKILL

    def read_record(self) -> dict:
        return json.loads((self.copy_dir / RECORD_NAME).read_text(encoding="utf-8"))

    def doctor_result(self, code: str) -> dict:
        p = self.run_ae("doctor", "--json")
        results = [r for r in json.loads(p.stdout)["results"] if r["code"] == code]
        self.assertEqual(len(results), 1, p.stdout + p.stderr)
        return results[0]

    def test_link_creates_symlinks_to_copy_for_each_agent(self):
        p = self.run_ae("install", "--link", "codex,agents,zcode")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        copy_real = str(self.copy_dir.resolve())
        for agent in AGENTS:
            link = agent_link(self.home, agent)
            self.assertTrue(link.is_symlink(), f"{agent} 位置应是软链接")
            self.assertEqual(os.path.realpath(str(link)), copy_real, f"{agent} 链接应指向技能副本")
        recorded = {entry["agent"]: entry["path"] for entry in self.read_record()["links"]}
        self.assertEqual(set(recorded), set(AGENTS))
        for agent in AGENTS:
            self.assertEqual(recorded[agent], str(agent_link(self.home, agent)))

    def test_link_unknown_agent_is_command_error(self):
        p = self.run_ae("install", "--link", "codex,foo")
        self.assertEqual(p.returncode, 2)
        self.assertIn("foo", p.stderr)
        self.assertFalse(agent_link(self.home, "codex").exists(), "未知名称时不应创建任何链接")
        self.assertFalse(self.copy_dir.exists(), "未知名称时先报错，不做复制")

    def test_link_refuses_to_overwrite_non_link_directory(self):
        occupied = agent_link(self.home, "zcode")
        occupied.parent.mkdir(parents=True)
        occupied.mkdir()
        (occupied / "keep.txt").write_text("已有内容", encoding="utf-8")
        p = self.run_ae("install", "--link", "zcode")
        self.assertEqual(p.returncode, 2)
        self.assertIn("zcode", p.stderr)
        self.assertIn(str(occupied), p.stderr)
        self.assertTrue((occupied / "keep.txt").is_file(), "已有非链接目录不被覆盖")

    def test_repeated_install_records_each_link_once(self):
        self.assertEqual(self.run_ae("install", "--link", "codex").returncode, 0)
        p = self.run_ae("install", "--link", "codex")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        record = self.read_record()
        self.assertEqual(len(record["links"]), 1, "重复安装不重复记录")
        self.assertTrue(agent_link(self.home, "codex").is_symlink(), "链接仍有效")

    def test_new_install_keeps_previously_recorded_links(self):
        self.assertEqual(self.run_ae("install", "--link", "codex").returncode, 0)
        self.assertEqual(self.run_ae("install", "--link", "zcode").returncode, 0)
        recorded = {entry["agent"] for entry in self.read_record()["links"]}
        self.assertEqual(recorded, {"codex", "zcode"}, "换一组 --link 不丢已建链接的记录")

    def test_doctor_passes_with_healthy_links(self):
        self.assertEqual(self.run_ae("install", "--link", "codex,agents").returncode, 0)
        result = self.doctor_result("skill-links")
        self.assertEqual(result["level"], "通过")
        self.assertIn("codex", result["message"])
        self.assertIn("agents", result["message"])

    def test_doctor_fails_on_dangling_link_with_fix(self):
        self.assertEqual(self.run_ae("install", "--link", "codex").returncode, 0)
        shutil.rmtree(self.copy_dir)  # 副本与安装记录一起消失 → 回退扫描发现悬空链接
        result = self.doctor_result("skill-links")
        self.assertEqual(result["level"], "失败")
        self.assertIn("codex", result["message"])
        self.assertIn("install --link codex", result["fix_command"])

    def test_doctor_falls_back_to_scan_when_record_missing(self):
        self.assertEqual(self.run_ae("install").returncode, 0)
        link = agent_link(self.home, "agents")
        link.parent.mkdir(parents=True)
        os.symlink(str(self.copy_dir.resolve()), str(link))
        (self.copy_dir / RECORD_NAME).unlink()
        result = self.doctor_result("skill-links")
        self.assertEqual(result["level"], "通过")
        self.assertIn("agents", result["message"])
        self.assertIn(str(link), result["message"])


class LinkCheckTests(unittest.TestCase):
    """检查项函数：直接导入 ae，构造状态断言返回记录与回退扫描。"""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="ae-link-check-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.home = self.root / "home"
        self.home.mkdir()
        self.skills = self.home / ".claude" / "skills"
        self.copy = self.skills / SKILL
        self.copy.mkdir(parents=True)
        (self.copy / "SKILL.md").write_text("---\nname: annotation-everything\n---\n合成技能\n",
                                            encoding="utf-8")
        self.repo = self.root / "repo"
        self.repo.mkdir()
        self._saved = {key: os.environ.get(key) for key in ("HOME", "CODEX_HOME", "CLAUDE_SKILLS_DIR")}
        os.environ.update({"HOME": str(self.home), "CODEX_HOME": str(self.home / ".codex"),
                           "CLAUDE_SKILLS_DIR": str(self.skills)})

    def tearDown(self):
        for key, value in self._saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    def make_link(self, agent: str, target=None) -> dict:
        """在 Agent 技能目录建软链接，返回与安装记录同构的条目。"""
        link = agent_link(self.home, agent)
        link.parent.mkdir(parents=True, exist_ok=True)
        os.symlink(str(target if target is not None else self.copy.resolve()), str(link))
        return {"agent": agent, "path": str(link)}

    def base_record(self, *entries: dict) -> dict:
        return {"repo": str(self.repo), "commit": "0" * 40, "version": "9.9.9",
                "installed_at": "2026-10-29T00:00:00+08:00", "links": list(entries)}

    def ctx(self, record, copy=None):
        return ae.DoctorContext(skill_copy=copy or self.copy, repo=self.repo, record=record)

    def test_check_links_passes_when_all_links_healthy(self):
        entry = self.make_link("codex")
        result = ae.check_links(self.ctx(self.base_record(entry)))
        self.assertEqual(result.level, ae.LEVEL_PASS)
        self.assertEqual(result.code, "skill-links")
        self.assertEqual(result.category, "安装状态")

    def test_check_links_fails_on_dangling_link(self):
        entry = self.make_link("zcode")
        shutil.rmtree(self.copy)  # 链接指向的技能副本消失
        result = ae.check_links(self.ctx(self.base_record(entry), copy=self.copy))
        self.assertEqual(result.level, ae.LEVEL_FAIL)
        self.assertIn("悬空", result.message)
        self.assertIn("install --link zcode", result.fix_command)

    def test_check_links_fails_when_link_replaced_by_directory(self):
        link = agent_link(self.home, "codex")
        link.parent.mkdir(parents=True)
        link.mkdir()
        result = ae.check_links(self.ctx(self.base_record({"agent": "codex", "path": str(link)})))
        self.assertEqual(result.level, ae.LEVEL_FAIL)
        self.assertIn("已不是链接", result.message)

    def test_check_links_fails_when_recorded_link_deleted(self):
        result = ae.check_links(self.ctx(self.base_record({"agent": "codex", "path": str(agent_link(self.home, "codex"))})))
        self.assertEqual(result.level, ae.LEVEL_FAIL)
        self.assertIn("已删除", result.message)

    def test_check_links_reports_every_link(self):
        healthy = self.make_link("codex")
        dangling = self.make_link("zcode", target=self.root / "no-such-copy")
        result = ae.check_links(self.ctx(self.base_record(healthy, dangling)))
        self.assertEqual(result.level, ae.LEVEL_FAIL)
        self.assertIn("codex", result.message)
        self.assertIn("zcode", result.message)

    def test_check_links_without_recorded_links_passes(self):
        result = ae.check_links(self.ctx(self.base_record()))
        self.assertEqual(result.level, ae.LEVEL_PASS)

    def test_check_links_scans_known_dirs_when_record_missing(self):
        self.make_link("agents")
        self.make_link("zcode")
        result = ae.check_links(self.ctx(None))
        self.assertEqual(result.level, ae.LEVEL_PASS)
        self.assertIn("agents", result.message)
        self.assertIn("zcode", result.message)

    def test_scan_links_finds_only_links_pointing_at_copy(self):
        self.make_link("codex")
        self.make_link("agents")
        self.make_link("zcode", target=self.root / "elsewhere")  # 指向别处，不算
        found = ae.scan_links(self.copy)
        self.assertEqual({entry["agent"] for entry in found}, {"codex", "agents"})
        for entry in found:
            self.assertEqual(set(entry), {"agent", "path"})
            self.assertEqual(entry["path"], str(agent_link(self.home, entry["agent"])))

    def test_scan_links_includes_dangling_links(self):
        entry = self.make_link("codex")
        shutil.rmtree(self.copy)  # 副本消失后扫描仍能按目标路径认出悬空链接
        found = ae.scan_links(self.copy)
        self.assertEqual(found, [entry])


if __name__ == "__main__":
    unittest.main()
