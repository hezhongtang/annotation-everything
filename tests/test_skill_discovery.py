"""技能发现的命令行测试；所有技能和审阅均为合成数据。"""
from __future__ import annotations

import fcntl
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "skill" / "annotation-everything" / "scripts"
FIXTURE = ROOT / "fixtures" / "sample"


class SkillDiscoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="ae-skills-test-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.home = self.root / "home"
        self.home.mkdir()
        self.sources = self.root / "sources"
        self.sources.mkdir()
        self.review = self.root / "review"
        self.env = {**os.environ, "HOME": str(self.home), "AE_HOME": str(self.root / "ledger"),
                    "CODEX_HOME": str(self.home / ".codex"), "CLAUDE_SKILLS_DIR": str(self.home / ".claude" / "skills")}

    def skill(self, folder: str, name: str, description: str):
        d = self.sources / folder
        d.mkdir(parents=True, exist_ok=True)
        (d / "SKILL.md").write_text(f"---\nname: {name}\ndescription: {description}\n---\n合成技能正文\n", encoding="utf-8")

    def run_cli(self, script: str, *args: str):
        return subprocess.run([sys.executable, str(SCRIPTS / script), *map(str, args)],
                              env=self.env, capture_output=True, text=True, timeout=30)

    def catalog(self):
        return json.loads((self.review / "skills.json").read_text(encoding="utf-8"))["skills"]

    def test_build_discovers_configured_skill_metadata(self):
        self.skill("writer", "writer", "文字校对")
        self.skill("chart", "chart", "图表设计")
        p = self.run_cli("build_viewer.py", "--report", FIXTURE / "report.html", "--out", self.review,
                         "--skills-from", self.sources)
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertCountEqual(self.catalog(), [{"name": "writer", "desc": "文字校对"},
                                               {"name": "chart", "desc": "图表设计"}])
        page = (self.review / "index.html").read_text(encoding="utf-8")
        self.assertIn('"writer"', page)
        self.assertNotIn(str(self.sources), page, "页面不包含本机扫描路径")
        self.assertNotIn("合成技能正文", page, "页面只包含名称和说明")
        state = self.review / ".skill-sources.json"
        self.assertEqual(state.stat().st_mode & 0o777, 0o600)
        self.assertNotIn(str(self.sources), state.read_text(encoding="utf-8"))

    def test_rebuild_preserves_manual_override_and_removal(self):
        self.skill("writer", "writer", "扫描的旧说明")
        self.skill("chart", "chart", "图表设计")
        p = self.run_cli("build_viewer.py", "--report", FIXTURE / "report.html", "--out", self.review,
                         "--skills-from", self.sources)
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        p = self.run_cli("notes.py", "skills", self.review, "--add", "writer=手工说明", "--remove", "chart",
                         "--always", "writer=讲稿")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.skill("writer", "writer", "扫描的新说明")
        self.skill("fresh", "fresh", "新安装的技能")
        p = self.run_cli("build_viewer.py", "--rebuild", self.review)
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertCountEqual(self.catalog(), [{"name": "writer", "desc": "手工说明"},
                                               {"name": "fresh", "desc": "新安装的技能"}])
        doc = json.loads((self.review / "skills.json").read_text(encoding="utf-8"))
        self.assertEqual(doc["always"], [{"name": "writer", "scope": "讲稿"}])

    def test_large_catalog_survives_build_and_always_save(self):
        for n in range(205):
            self.skill(f"s{n:03d}", f"skill-{n:03d}", "合成说明")
        p = self.run_cli("build_viewer.py", "--report", FIXTURE / "report.html", "--out", self.review,
                         "--skills-from", self.sources)
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertEqual(len(self.catalog()), 205)
        page = (self.review / "index.html").read_text(encoding="utf-8")
        self.assertTrue('"skill-204"' in page, "页面可用完整目录")
        p = self.run_cli("notes.py", "skills", self.review, "--always", "skill-204")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertEqual(len(self.catalog()), 205, "保存常驻技能不会截断候选目录")

    def test_default_discovery_can_be_disabled_and_remembers_choice(self):
        default = self.home / ".agents" / "skills" / "default-skill"
        default.mkdir(parents=True)
        (default / "SKILL.md").write_text("---\nname: default-skill\ndescription: 合成默认技能\n---\n", encoding="utf-8")
        p = self.run_cli("build_viewer.py", "--report", FIXTURE / "report.html", "--out", self.review)
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertIn({"name": "default-skill", "desc": "合成默认技能"}, self.catalog())
        empty_review = self.root / "no-scan"
        p = self.run_cli("build_viewer.py", "--report", FIXTURE / "report.html", "--out", empty_review,
                         "--no-scan-skills")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        p = self.run_cli("build_viewer.py", "--rebuild", empty_review)
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertFalse((empty_review / "skills.json").exists(), "重建保留禁用扫描的选择")

    def test_unavailable_source_keeps_previous_catalog_and_warns(self):
        self.skill("writer", "writer", "文字校对")
        p = self.run_cli("build_viewer.py", "--report", FIXTURE / "report.html", "--out", self.review,
                         "--skills-from", self.sources)
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.sources.rename(self.root / "temporarily-unavailable")
        p = self.run_cli("build_viewer.py", "--rebuild", self.review)
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertIn({"name": "writer", "desc": "文字校对"}, self.catalog())
        self.assertIn("技能", p.stderr)
        self.assertIn("不可", p.stderr)

    def test_automatic_updates_refresh_but_clear_does_not_resurrect(self):
        self.skill("writer", "writer", "旧说明")
        self.skill("chart", "chart", "图表设计")
        p = self.run_cli("build_viewer.py", "--report", FIXTURE / "report.html", "--out", self.review,
                         "--skills-from", self.sources)
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.skill("writer", "writer", "更新说明")
        (self.sources / "chart" / "SKILL.md").unlink()
        p = self.run_cli("build_viewer.py", "--rebuild", self.review)
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertEqual(self.catalog(), [{"name": "writer", "desc": "更新说明"}])
        p = self.run_cli("notes.py", "skills", self.review, "--clear")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        p = self.run_cli("build_viewer.py", "--rebuild", self.review)
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertEqual(self.catalog(), [], "手工清空后重建不恢复旧候选")

    def test_corrupt_catalog_is_not_overwritten_by_discovery(self):
        self.skill("writer", "writer", "文字校对")
        self.review.mkdir()
        catalog = self.review / "skills.json"
        catalog.write_text("{broken", encoding="utf-8")
        p = self.run_cli("build_viewer.py", "--report", FIXTURE / "report.html", "--out", self.review,
                         "--skills-from", self.sources)
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertEqual(catalog.read_text(encoding="utf-8"), "{broken")
        self.assertIn("未覆盖", p.stderr)

    def test_concurrent_rebuild_and_manual_changes_keep_both_updates(self):
        self.skill("writer", "writer", "文字校对")
        p = self.run_cli("build_viewer.py", "--report", FIXTURE / "report.html", "--out", self.review,
                         "--skills-from", self.sources)
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.skill("fresh", "fresh", "新安装技能")
        commands = [
            ["build_viewer.py", "--rebuild", str(self.review)],
            ["notes.py", "skills", str(self.review), "--add", "manual=手工说明", "--remove", "writer"],
            ["notes.py", "skills", str(self.review), "--always", "manual=讲稿"],
        ]
        with (self.review / ".notes.lock").open("r+") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            processes = [subprocess.Popen([sys.executable, str(SCRIPTS / cmd[0]), *cmd[1:]], env=self.env,
                                          stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
                         for cmd in commands]
            time.sleep(0.2)
            self.assertTrue(all(p.poll() is None for p in processes), "各操作同时等待审阅目录锁")
            fcntl.flock(lock, fcntl.LOCK_UN)
        for process in processes:
            stdout, stderr = process.communicate(timeout=30)
            self.assertEqual(process.returncode, 0, stdout + stderr)
        self.assertCountEqual(self.catalog(), [{"name": "fresh", "desc": "新安装技能"},
                                               {"name": "manual", "desc": "手工说明"}])
        doc = json.loads((self.review / "skills.json").read_text(encoding="utf-8"))
        self.assertEqual(doc["always"], [{"name": "manual", "scope": "讲稿"}])
        p = self.run_cli("build_viewer.py", "--rebuild", self.review)
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertNotIn("writer", [item["name"] for item in self.catalog()])

    def test_plugin_cache_uses_newest_version_and_prefers_direct_skills(self):
        for version, desc in [("1.9", "旧版"), ("1.10", "新版")]:
            d = self.home / ".codex" / "plugins" / "cache" / "market" / "sample" / version / "skills" / "sample"
            d.mkdir(parents=True)
            (d / "SKILL.md").write_text(f"---\nname: sample\ndescription: {desc}\n---\n", encoding="utf-8")
        p = self.run_cli("build_viewer.py", "--report", FIXTURE / "report.html", "--out", self.review)
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertIn({"name": "sample", "desc": "新版"}, self.catalog())
        direct = self.home / ".agents" / "skills" / "sample"
        direct.mkdir(parents=True)
        (direct / "SKILL.md").write_text("---\nname: sample\ndescription: 直接安装\n---\n", encoding="utf-8")
        p = self.run_cli("build_viewer.py", "--rebuild", self.review)
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertEqual([x for x in self.catalog() if x["name"] == "sample"],
                         [{"name": "sample", "desc": "直接安装"}])

    def test_bad_skill_file_preserves_previous_entries_and_imports_good_ones(self):
        self.skill("writer", "writer", "文字校对")
        self.skill("chart", "chart", "图表设计")
        p = self.run_cli("build_viewer.py", "--report", FIXTURE / "report.html", "--out", self.review,
                         "--skills-from", self.sources)
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        (self.sources / "chart" / "SKILL.md").write_text("---\nname: chart\n", encoding="utf-8")
        self.skill("fresh", "fresh", "新安装技能")
        p = self.run_cli("build_viewer.py", "--rebuild", self.review)
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertCountEqual(self.catalog(), [{"name": "writer", "desc": "文字校对"},
                                               {"name": "chart", "desc": "图表设计"},
                                               {"name": "fresh", "desc": "新安装技能"}])
        self.assertIn("技能", p.stderr)

    def test_complete_description_is_available_for_search(self):
        description = "合成说明" * 40 + "字体校对"
        self.skill("writer", "writer", description)
        p = self.run_cli("build_viewer.py", "--report", FIXTURE / "report.html", "--out", self.review,
                         "--skills-from", self.sources)
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertEqual(self.catalog()[0]["desc"], description)
        self.assertTrue("字体校对" in (self.review / "index.html").read_text(encoding="utf-8"))

    def test_one_flag_can_specify_multiple_sources(self):
        self.skill("first", "first", "第一来源")
        other = self.root / "other"
        (other / "second").mkdir(parents=True)
        (other / "second" / "SKILL.md").write_text("---\nname: second\ndescription: 第二来源\n---\n", encoding="utf-8")
        p = self.run_cli("build_viewer.py", "--report", FIXTURE / "report.html", "--out", self.review,
                         "--skills-from", self.sources, other)
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertCountEqual(self.catalog(), [{"name": "first", "desc": "第一来源"},
                                               {"name": "second", "desc": "第二来源"}])

    def test_unavailable_default_source_warns_when_prior_results_are_kept(self):
        root = self.home / ".agents" / "skills"
        (root / "writer").mkdir(parents=True)
        (root / "writer" / "SKILL.md").write_text("---\nname: writer\ndescription: 合成说明\n---\n", encoding="utf-8")
        p = self.run_cli("build_viewer.py", "--report", FIXTURE / "report.html", "--out", self.review)
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        root.rename(root.with_name("missing-skills"))
        p = self.run_cli("build_viewer.py", "--rebuild", self.review)
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertEqual(self.catalog(), [{"name": "writer", "desc": "合成说明"}])
        self.assertIn("不可访问", p.stderr)

    def test_clear_and_set_in_one_command_keeps_the_new_manual_entry(self):
        self.skill("writer", "writer", "扫描说明")
        p = self.run_cli("build_viewer.py", "--report", FIXTURE / "report.html", "--out", self.review,
                         "--skills-from", self.sources)
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        p = self.run_cli("notes.py", "skills", self.review, "--clear", "--set", "writer=手工说明")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertEqual(self.catalog(), [{"name": "writer", "desc": "手工说明"}])
        p = self.run_cli("build_viewer.py", "--rebuild", self.review)
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertEqual(self.catalog(), [{"name": "writer", "desc": "手工说明"}])

    def test_symlink_aliases_and_cycles_do_not_duplicate_or_recurse(self):
        self.skill("writer", "writer", "文字校对")
        (self.sources / "alias").symlink_to(self.sources / "writer", target_is_directory=True)
        (self.sources / "cycle").symlink_to(self.sources, target_is_directory=True)
        p = self.run_cli("build_viewer.py", "--report", FIXTURE / "report.html", "--out", self.review,
                         "--skills-from", self.sources)
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertEqual(self.catalog(), [{"name": "writer", "desc": "文字校对"}])

    def test_setting_duplicate_names_keeps_the_last_description(self):
        p = self.run_cli("build_viewer.py", "--report", FIXTURE / "report.html", "--out", self.review,
                         "--no-scan-skills")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        p = self.run_cli("notes.py", "skills", self.review, "--set", "writer=第一版", "writer=第二版")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertEqual(self.catalog(), [{"name": "writer", "desc": "第二版"}])

    def test_invalid_always_update_does_not_partially_change_catalog(self):
        p = self.run_cli("build_viewer.py", "--report", FIXTURE / "report.html", "--out", self.review,
                         "--no-scan-skills")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        always = [f"a{n}" for n in range(20)]
        p = self.run_cli("notes.py", "skills", self.review, "--always", *always)
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        p = self.run_cli("notes.py", "skills", self.review, "--add", "writer=文字校对", "--always-add", "too-many")
        self.assertNotEqual(p.returncode, 0)
        self.assertEqual(self.catalog(), [], "常驻技能参数失败时，目录变更不应部分生效")

    def test_malformed_source_state_is_not_overwritten(self):
        self.skill("writer", "writer", "文字校对")
        p = self.run_cli("build_viewer.py", "--report", FIXTURE / "report.html", "--out", self.review,
                         "--skills-from", self.sources)
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        state = self.review / ".skill-sources.json"
        broken = json.loads(state.read_text(encoding="utf-8"))
        broken["sources"] = {"bad": "not-a-list"}
        state.write_text(json.dumps(broken, ensure_ascii=False), encoding="utf-8")
        before = state.read_bytes()
        catalog = (self.review / "skills.json").read_bytes()
        p = self.run_cli("build_viewer.py", "--rebuild", self.review)
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertEqual(state.read_bytes(), before)
        self.assertEqual((self.review / "skills.json").read_bytes(), catalog)
        self.assertIn("未覆盖", p.stderr)

    def test_failed_catalog_write_rolls_back_source_state(self):
        self.skill("writer", "writer", "文字校对")
        p = self.run_cli("build_viewer.py", "--report", FIXTURE / "report.html", "--out", self.review,
                         "--skills-from", self.sources)
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        state, catalog = self.review / ".skill-sources.json", self.review / "skills.json"
        before_state, before_catalog = state.read_bytes(), catalog.read_bytes()
        fault = '''
import os, pathlib, sys
sys.path.insert(0, sys.argv[1])
import notes
real_replace = os.replace
def replace(src, dst):
    if pathlib.Path(dst).name == "skills.json":
        raise OSError("synthetic write failure")
    return real_replace(src, dst)
os.replace = replace
sys.exit(notes.main(["skills", sys.argv[2], "--remove", "writer"]))
'''
        p = subprocess.run([sys.executable, "-c", fault, str(SCRIPTS), str(self.review)],
                           env=self.env, capture_output=True, text=True, timeout=30)
        self.assertNotEqual(p.returncode, 0)
        self.assertEqual(catalog.read_bytes(), before_catalog)
        self.assertEqual(state.read_bytes(), before_state, "目录写入失败后恢复来源状态")

    def test_rebuild_follows_a_retargeted_explicit_symlink(self):
        self.skill("writer", "writer", "旧位置")
        other = self.root / "new-source" / "writer"
        other.mkdir(parents=True)
        (other / "SKILL.md").write_text("---\nname: writer\ndescription: 新位置\n---\n", encoding="utf-8")
        alias = self.root / "current-skills"
        alias.symlink_to(self.sources, target_is_directory=True)
        p = self.run_cli("build_viewer.py", "--report", FIXTURE / "report.html", "--out", self.review,
                         "--skills-from", alias)
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        alias.unlink()
        alias.symlink_to(other.parent, target_is_directory=True)
        p = self.run_cli("build_viewer.py", "--rebuild", self.review)
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertEqual(self.catalog(), [{"name": "writer", "desc": "新位置"}])


if __name__ == "__main__":
    unittest.main()
