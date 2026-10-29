"""ae.py 可选工具与开发环境检查的测试：检查项直接导入 + 子进程命令行，均为合成数据。

- 可选工具：在 PATH 前端放假 bin 或收窄 PATH 构造存在/缺失（参照 test_pptx_fonts.py 的模式）；
- git 钩子：临时 git 仓库构造启用/未启用，不依赖本仓库或本机真实 git 配置；
- Chrome：经 DoctorContext 注入探测函数，CI 无 Chrome 时测试不受影响；
- 命令行：doctor 的 --json 与退出码，家目录与技能目录全部指向临时目录。
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
AE = ROOT / "ae.py"
SKILL_SRC = ROOT / "skill" / "annotation-everything"
RECORD_NAME = ".install-record.json"

sys.path.insert(0, str(ROOT))

import ae  # noqa: E402

PASS, WARN, FAIL = ae.LEVEL_PASS, ae.LEVEL_WARN, ae.LEVEL_FAIL


class ToolCheckTests(unittest.TestCase):
    """fc-list / pdffonts：PATH 决定结果；缺失为警告并附安装提示，绝不为失败。"""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="ae-tools-test-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.bin = self.root / "bin"
        self.bin.mkdir()
        self.old_path = os.environ.get("PATH", os.defpath)
        self.addCleanup(self.restore_path)
        self.ctx = ae.DoctorContext(skill_copy=self.root / "skills" / "annotation-everything",
                                    repo=None, record=None)

    def restore_path(self):
        os.environ["PATH"] = self.old_path

    def fake_tool(self, name):
        path = self.bin / name
        path.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        path.chmod(0o755)

    def with_only_fake_bin(self):
        """PATH 只剩假 bin：shutil.which 找不到任何真实工具。"""
        os.environ["PATH"] = str(self.bin)

    def with_fake_bin_first(self):
        os.environ["PATH"] = os.pathsep.join([str(self.bin), self.old_path])

    def test_fc_list_passes_when_on_path(self):
        self.fake_tool("fc-list")
        self.with_fake_bin_first()
        result = ae.check_fc_list(self.ctx)
        self.assertEqual(result.level, PASS)
        self.assertEqual(result.code, "fc-list")
        self.assertEqual(result.category, "可选工具")

    def test_fc_list_warns_with_fontconfig_hint_when_missing(self):
        self.with_only_fake_bin()
        result = ae.check_fc_list(self.ctx)
        self.assertEqual(result.level, WARN)
        self.assertEqual(result.code, "fc-list")
        self.assertIn("fontconfig", result.fix_command)

    def test_pdffonts_passes_when_on_path(self):
        self.fake_tool("pdffonts")
        self.with_fake_bin_first()
        self.assertEqual(ae.check_pdffonts(self.ctx).level, PASS)

    def test_pdffonts_warns_with_poppler_hint_when_missing(self):
        self.with_only_fake_bin()
        result = ae.check_pdffonts(self.ctx)
        self.assertEqual(result.level, WARN)
        self.assertEqual(result.code, "pdffonts")
        self.assertIn("poppler", result.fix_command)

    def test_missing_optional_tools_are_never_failures(self):
        self.with_only_fake_bin()
        for result in (ae.check_fc_list(self.ctx), ae.check_pdffonts(self.ctx)):
            self.assertEqual(result.category, "可选工具")
            self.assertNotEqual(result.level, FAIL)

    def test_one_tool_present_one_missing_reports_each_separately(self):
        self.fake_tool("fc-list")
        self.with_only_fake_bin()
        self.assertEqual(ae.check_fc_list(self.ctx).level, PASS)
        self.assertEqual(ae.check_pdffonts(self.ctx).level, WARN)


class GitHooksCheckTests(unittest.TestCase):
    """git 钩子检查：临时 git 仓库构造启用/未启用，不碰本仓库与本机配置。"""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="ae-hooks-test-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def make_repo(self, hooks_dir=True, git_init=True) -> Path:
        repo = self.root / "repo"
        skill = repo / "skill" / "annotation-everything"
        skill.mkdir(parents=True)
        (skill / "SKILL.md").write_text("---\nname: annotation-everything\n---\n合成技能\n",
                                        encoding="utf-8")
        if hooks_dir:
            hooks = repo / "tools" / "git-hooks"
            hooks.mkdir(parents=True)
            (hooks / "pre-commit").write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        if git_init:
            subprocess.run(["git", "init", "-q", str(repo)],
                           check=True, capture_output=True, timeout=30)
        return repo

    def ctx(self, repo: Path):
        return ae.DoctorContext(skill_copy=self.root / "skills" / "annotation-everything",
                                repo=repo, record=None)

    def configure(self, repo: Path, value: str):
        subprocess.run(["git", "-C", str(repo), "config", "core.hooksPath", value],
                       check=True, capture_output=True, timeout=30)

    def test_passes_when_hooks_path_configured(self):
        repo = self.make_repo()
        self.configure(repo, "tools/git-hooks")
        result = ae.check_git_hooks(self.ctx(repo))
        self.assertEqual(result.level, PASS)
        self.assertEqual(result.code, "git-hooks")
        self.assertEqual(result.category, "开发环境")

    def test_passes_when_configured_with_absolute_path(self):
        repo = self.make_repo()
        self.configure(repo, str(repo / "tools" / "git-hooks"))
        self.assertEqual(ae.check_git_hooks(self.ctx(repo)).level, PASS)

    def test_warns_with_setup_command_when_not_configured(self):
        repo = self.make_repo()
        result = ae.check_git_hooks(self.ctx(repo))
        self.assertEqual(result.level, WARN)
        self.assertEqual(result.code, "git-hooks")
        self.assertIn("setup-dev.sh", result.fix_command)

    def test_warns_when_pointing_elsewhere(self):
        repo = self.make_repo()
        other = repo / ".githooks"
        other.mkdir()
        self.configure(repo, ".githooks")
        self.assertEqual(ae.check_git_hooks(self.ctx(repo)).level, WARN)

    def test_warns_when_not_a_git_repository(self):
        repo = self.make_repo(git_init=False)
        result = ae.check_git_hooks(self.ctx(repo))
        self.assertEqual(result.level, WARN)
        self.assertIn("setup-dev.sh", result.fix_command)

    def test_warns_when_hooks_directory_missing(self):
        repo = self.make_repo(hooks_dir=False)
        self.configure(repo, "tools/git-hooks")
        self.assertEqual(ae.check_git_hooks(self.ctx(repo)).level, WARN)


class ChromeCheckTests(unittest.TestCase):
    """Chrome 检查：探测经 DoctorContext 注入，结果不依赖本机是否装有 Chrome。"""

    def ctx(self, probe):
        temp_root = Path(tempfile.mkdtemp(prefix="ae-chrome-test-"))
        self.addCleanup(shutil.rmtree, temp_root, ignore_errors=True)
        return ae.DoctorContext(skill_copy=temp_root / "skills" / "annotation-everything",
                                repo=temp_root / "repo", record=None, chrome_probe=probe)

    def test_passes_when_probe_finds_executable(self):
        fake = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
        result = ae.check_chrome(self.ctx(lambda: fake))
        self.assertEqual(result.level, PASS)
        self.assertEqual(result.code, "e2e-chrome")
        self.assertEqual(result.category, "开发环境")

    def test_warns_when_probe_finds_none(self):
        result = ae.check_chrome(self.ctx(lambda: None))
        self.assertEqual(result.level, WARN)
        self.assertEqual(result.code, "e2e-chrome")
        self.assertEqual(result.category, "开发环境")

    def test_default_probe_returns_existing_path_or_none(self):
        found = ae.find_chrome()
        self.assertTrue(found is None or Path(found).is_file(), found)


class DoctorScopeTests(unittest.TestCase):
    """开发环境类检查只在仓库内（ctx.repo 非 None）出现；可选工具始终检查。"""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="ae-scope-test-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.repo = self.root / "repo"
        skill = self.repo / "skill" / "annotation-everything"
        skill.mkdir(parents=True)
        (skill / "SKILL.md").write_text("---\nname: annotation-everything\n---\n合成技能\n",
                                        encoding="utf-8")

    def test_dev_checks_absent_outside_repo(self):
        ctx = ae.DoctorContext(skill_copy=self.root / "skills" / "annotation-everything",
                               repo=None, record=None)
        results = ae.run_doctor(ctx)
        categories = {r.category for r in results}
        codes = {r.code for r in results}
        self.assertNotIn("开发环境", categories)
        self.assertNotIn("git-hooks", codes)
        self.assertNotIn("e2e-chrome", codes)
        self.assertIn("可选工具", categories)
        self.assertIn("fc-list", codes)
        self.assertIn("pdffonts", codes)

    def test_dev_checks_present_inside_repo(self):
        ctx = ae.DoctorContext(skill_copy=self.root / "skills" / "annotation-everything",
                               repo=self.repo, record=None)
        results = ae.run_doctor(ctx)
        categories = {r.category for r in results}
        codes = {r.code for r in results}
        self.assertIn("开发环境", categories)
        self.assertIn("git-hooks", codes)
        self.assertIn("e2e-chrome", codes)


class DoctorCliTests(unittest.TestCase):
    """命令行层面：可选工具缺失只警告不影响退出码；开发环境类别随仓库出现/消失。"""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="ae-tools-cli-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.home = self.root / "home"
        self.home.mkdir()
        self.skills = self.home / ".claude" / "skills"
        self.env = {**os.environ, "HOME": str(self.home), "CLAUDE_SKILLS_DIR": str(self.skills),
                    "PYTHONDONTWRITEBYTECODE": "1"}

    @property
    def copy_dir(self) -> Path:
        return self.skills / "annotation-everything"

    def run_ae(self, script: Path, *args: str, path=None):
        env = dict(self.env)
        if path is not None:
            env["PATH"] = path
        return subprocess.run([sys.executable, str(script), *args],
                              env=env, capture_output=True, text=True, timeout=60)

    def install(self):
        p = self.run_ae(AE, "install")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)

    def doctor_json(self, script: Path = AE, path=None):
        p = self.run_ae(script, "doctor", "--json", path=path)
        return p, json.loads(p.stdout)

    def rewrite_record_repo(self, repo: str):
        record_path = self.copy_dir / RECORD_NAME
        record = json.loads(record_path.read_text(encoding="utf-8"))
        record["repo"] = repo
        record_path.write_text(json.dumps(record, ensure_ascii=False), encoding="utf-8")

    def test_missing_optional_tools_warn_but_exit_zero(self):
        self.install()
        empty_bin = self.root / "empty-bin"
        empty_bin.mkdir()
        p, doc = self.doctor_json(path=str(empty_bin))
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        by_code = {r["code"]: r for r in doc["results"]}
        self.assertEqual(by_code["fc-list"]["level"], "警告")
        self.assertEqual(by_code["pdffonts"]["level"], "警告")
        self.assertIn("fontconfig", by_code["fc-list"]["fix_command"])
        self.assertIn("poppler", by_code["pdffonts"]["fix_command"])
        self.assertEqual(doc["summary"]["fail"], 0)

    def test_dev_environment_present_with_warns_inside_repo(self):
        self.install()
        repo = self.root / "synthetic-repo"
        shutil.copytree(SKILL_SRC, repo / "skill" / "annotation-everything")
        self.rewrite_record_repo(str(repo))
        p, doc = self.doctor_json()
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertIn("开发环境", {r["category"] for r in doc["results"]})
        by_code = {r["code"]: r for r in doc["results"]}
        self.assertEqual(by_code["git-hooks"]["level"], "警告", "合成仓库不是 git 仓库")
        self.assertIn(by_code["e2e-chrome"]["level"], ("通过", "警告"))
        self.assertEqual(doc["summary"]["fail"], 0)

    def test_dev_environment_absent_outside_repo(self):
        outside = self.root / "outside"
        outside.mkdir()
        shutil.copyfile(AE, outside / "ae.py")
        self.install()
        self.rewrite_record_repo(str(self.root / "gone"))
        p, doc = self.doctor_json(script=outside / "ae.py")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        categories = {r["category"] for r in doc["results"]}
        codes = {r["code"] for r in doc["results"]}
        self.assertNotIn("开发环境", categories)
        self.assertNotIn("git-hooks", codes)
        self.assertNotIn("e2e-chrome", codes)
        self.assertIn("可选工具", categories)


if __name__ == "__main__":
    unittest.main()
