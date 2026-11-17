"""ae.py install --link 与链接体检的测试；均为合成环境，绝不触碰真实家目录。"""
from __future__ import annotations

import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
AE = ROOT / "ae.py"
sys.path.insert(0, str(ROOT))
import ae  # noqa: E402


def _can_symlink() -> bool:
    if os.name != "nt":
        return True
    with tempfile.TemporaryDirectory() as directory:
        src = Path(directory) / "src"
        src.mkdir()
        try:
            os.symlink(str(src), str(Path(directory) / "lnk"), target_is_directory=True)
            return True
        except OSError:
            return False


CAN_SYMLINK = _can_symlink()

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


def _can_symlink() -> bool:
    if os.name != "nt":
        return True
    with tempfile.TemporaryDirectory() as d:
        src = Path(d) / "src"
        src.mkdir()
        try:
            os.symlink(str(src), str(Path(d) / "lnk"), target_is_directory=True)
            return True
        except OSError:
            return False


CAN_SYMLINK = _can_symlink()


def _resolved_link_target(link: Path) -> Path:
    raw = os.readlink(link)
    if raw.startswith("\\\\?\\") and not raw.startswith("\\\\?\\UNC\\"):
        raw = raw[4:]
    return Path(raw).resolve()


class AeLinkCliTests(unittest.TestCase):
    """命令行行为：子进程运行 ae.py，HOME 与各技能目录全部指向临时目录。"""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="ae-link-test-")
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name) / "home"
        self.home.mkdir()
        self.skills = self.home / ".claude" / "skills"
        self.env = {**os.environ, "HOME": str(self.home), "USERPROFILE": str(self.home),
                    "AE_HOME": str(self.home / ".annotation-everything"),
                    "CLAUDE_SKILLS_DIR": str(self.skills),
                    "CODEX_HOME": str(self.home / ".codex"),
                    "PYTHONDONTWRITEBYTECODE": "1"}

    def run_ae(self, *args, cwd=None):
        return subprocess.run([sys.executable, str(AE), *args], cwd=cwd,
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

    @unittest.skipUnless(CAN_SYMLINK, "Windows 当前账户无符号链接权限")
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

    @unittest.skipUnless(CAN_SYMLINK, "Windows 当前账户无符号链接权限")
    def test_repeated_install_records_each_link_once(self):
        self.assertEqual(self.run_ae("install", "--link", "codex").returncode, 0)
        p = self.run_ae("install", "--link", "codex")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        record = self.read_record()
        self.assertEqual(len(record["links"]), 1, "重复安装不重复记录")
        self.assertTrue(agent_link(self.home, "codex").is_symlink(), "链接仍有效")

    @unittest.skipUnless(CAN_SYMLINK, "Windows 当前账户无符号链接权限")
    def test_new_install_keeps_previously_recorded_links(self):
        self.assertEqual(self.run_ae("install", "--link", "codex").returncode, 0)
        self.assertEqual(self.run_ae("install", "--link", "zcode").returncode, 0)
        recorded = {entry["agent"] for entry in self.read_record()["links"]}
        self.assertEqual(recorded, {"codex", "zcode"}, "换一组 --link 不丢已建链接的记录")

    @unittest.skipUnless(CAN_SYMLINK, "Windows 当前账户无符号链接权限")
    def test_doctor_passes_with_healthy_links(self):
        self.assertEqual(self.run_ae("install", "--link", "codex,agents").returncode, 0)
        result = self.doctor_result("skill-links")
        self.assertEqual(result["level"], "通过")
        self.assertIn("codex", result["message"])
        self.assertIn("agents", result["message"])

    @unittest.skipUnless(CAN_SYMLINK, "Windows 当前账户无符号链接权限")
    def test_doctor_fails_on_dangling_link_with_fix(self):
        self.assertEqual(self.run_ae("install", "--link", "codex").returncode, 0)
        shutil.rmtree(self.copy_dir)  # 副本与安装记录一起消失 → 回退扫描发现悬空链接
        result = self.doctor_result("skill-links")
        self.assertEqual(result["level"], "失败")
        self.assertIn("codex", result["message"])
        self.assertIn("install --link codex", result["fix_command"])

    @unittest.skipUnless(CAN_SYMLINK, "Windows 当前账户无符号链接权限")
    def test_doctor_falls_back_to_scan_when_record_missing(self):
        self.assertEqual(self.run_ae("install").returncode, 0)
        link = agent_link(self.home, "agents")
        link.parent.mkdir(parents=True)
        os.symlink(str(self.copy_dir.resolve()), str(link), target_is_directory=True)
        (self.copy_dir / RECORD_NAME).unlink()
        result = self.doctor_result("skill-links")
        self.assertEqual(result["level"], "通过")
        self.assertIn("agents", result["message"])
        self.assertIn(str(link), result["message"])

    # ── --link 的 名称=路径 写法（#45）：为任意技能目录建链接并记入安装记录 ──

    def custom_dir(self, name="cursor-skills") -> Path:
        return self.home / name

    def custom_link(self, name="cursor-skills") -> Path:
        return self.custom_dir(name) / SKILL

    @unittest.skipUnless(CAN_SYMLINK, "Windows 当前账户无符号链接权限")
    def test_link_name_path_creates_symlink_and_records(self):
        custom = self.custom_dir()
        p = self.run_ae("install", "--link", f"cursor={custom}")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertTrue(self.custom_link().is_symlink(), "应在指定目录建出软链接")
        self.assertEqual(os.path.realpath(str(self.custom_link())), str(self.copy_dir.resolve()))
        recorded = {entry["agent"]: entry["path"] for entry in self.read_record()["links"]}
        self.assertEqual(recorded, {"cursor": str(self.custom_link())}, "自定义链接应记入安装记录")

    @unittest.skipUnless(CAN_SYMLINK, "Windows 当前账户无符号链接权限")
    def test_link_name_path_mixes_with_builtin(self):
        custom = self.custom_dir()
        p = self.run_ae("install", "--link", f"codex,cursor={custom}")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertTrue(agent_link(self.home, "codex").is_symlink())
        self.assertTrue(self.custom_link().is_symlink())
        recorded = {entry["agent"] for entry in self.read_record()["links"]}
        self.assertEqual(recorded, {"codex", "cursor"})

    @unittest.skipUnless(CAN_SYMLINK, "Windows 当前账户无符号链接权限")
    def test_link_name_path_expands_user(self):
        p = self.run_ae("install", "--link", "cursor=~/.cursor/skills")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        link = self.home / ".cursor" / "skills" / SKILL
        self.assertTrue(link.is_symlink(), "~ 应展开到临时 HOME")
        self.assertEqual(os.path.realpath(str(link)), str(self.copy_dir.resolve()))

    @unittest.skipUnless(CAN_SYMLINK, "Windows 当前账户无符号链接权限")
    def test_link_name_path_relative_to_cwd(self):
        custom = Path("relative-skills")
        p = self.run_ae("install", "--link", f"cursor={custom}", cwd=str(self.home))
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        link = self.home.resolve() / custom / SKILL   # 子进程的 cwd 是解析后的真实路径
        self.assertTrue(link.is_symlink(), "相对路径按运行目录解析并记为绝对路径")
        self.assertEqual(os.path.realpath(str(link)), str(self.copy_dir.resolve()))
        recorded = [Path(entry["path"]).resolve() for entry in self.read_record()["links"]]
        self.assertEqual(recorded, [link.resolve()])

    @unittest.skipUnless(CAN_SYMLINK, "Windows 当前账户无符号链接权限")
    def test_link_name_path_repeated_keeps_single_entry(self):
        custom = self.custom_dir()
        self.assertEqual(self.run_ae("install", "--link", f"cursor={custom}").returncode, 0)
        p = self.run_ae("install", "--link", f"cursor={custom}")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertEqual(len(self.read_record()["links"]), 1, "重复安装不重复记录")

    def test_link_bad_spec_is_command_error(self):
        for spec in ("=/some/dir", "cursor="):
            p = self.run_ae("install", "--link", spec)
            self.assertEqual(p.returncode, 2, (spec, p.stdout + p.stderr))
            self.assertIn(spec, p.stderr, spec)
            self.assertFalse(self.copy_dir.exists(), f"{spec} 报错时不应做复制")

    def test_link_name_path_occupied_is_refused(self):
        occupied = self.custom_link()
        occupied.parent.mkdir(parents=True)
        occupied.mkdir()
        (occupied / "keep.txt").write_text("已有内容", encoding="utf-8")
        p = self.run_ae("install", "--link", f"cursor={self.custom_dir()}")
        self.assertEqual(p.returncode, 2, p.stdout + p.stderr)
        self.assertTrue((occupied / "keep.txt").is_file(), "已有非链接目录不被覆盖")

    def test_link_name_path_overlapping_copy_is_refused(self):
        target = self.home / "target-skills"
        p = self.run_ae("install", "--target", str(target), "--link", f"x={target}")
        self.assertEqual(p.returncode, 2, p.stdout + p.stderr)
        self.assertFalse((target / SKILL).exists(), "链接位置与技能副本相同时不动手")

    def test_link_name_path_overlapping_source_is_refused(self):
        source = ROOT / "skill"
        p = self.run_ae("install", "--link", f"x={source}")
        self.assertEqual(p.returncode, 2, p.stdout + p.stderr)
        self.assertFalse((source / SKILL).is_symlink(), "技能本体不被改写")

    def test_nested_link_locations_are_refused_before_install(self):
        outer = self.custom_dir("outer")
        specs = [f"outer={outer}", f"inner={outer / SKILL / 'nested'}"]
        for ordered in (specs, list(reversed(specs))):
            with self.subTest(specs=ordered):
                p = self.run_ae("install", "--link", ",".join(ordered))
                self.assertEqual(p.returncode, 2, p.stdout + p.stderr)
                self.assertFalse(self.copy_dir.exists(), "路径重叠时不能先复制")
                self.assertFalse(outer.exists(), "路径重叠时不能先创建链接目录")

    @unittest.skipUnless(CAN_SYMLINK, "Windows 当前账户无符号链接权限")
    def test_existing_symlink_inside_copy_is_refused_before_resync(self):
        self.assertEqual(self.run_ae("install").returncode, 0)
        nested = self.copy_dir / "nested"
        nested.mkdir()
        os.symlink(str(self.home / "missing"), str(nested / SKILL), target_is_directory=True)
        p = self.run_ae("install", "--link", f"inner={nested}")
        self.assertEqual(p.returncode, 2, p.stdout + p.stderr)
        self.assertEqual(_resolved_link_target(nested / SKILL), (self.home / "missing").resolve())

    @unittest.skipUnless(CAN_SYMLINK, "Windows 当前账户无符号链接权限")
    def test_nested_link_locations_with_existing_parent_link_are_refused(self):
        outer = self.custom_dir("outer")
        outer.mkdir()
        previous = self.custom_dir("previous")
        previous.mkdir()
        os.symlink(str(previous), str(outer / SKILL), target_is_directory=True)
        specs = [f"outer={outer}", f"inner={outer / SKILL / 'nested'}"]
        for ordered in (specs, list(reversed(specs))):
            with self.subTest(specs=ordered):
                p = self.run_ae("install", "--link", ",".join(ordered))
                self.assertEqual(p.returncode, 2, p.stdout + p.stderr)
                self.assertFalse(self.copy_dir.exists(), "路径重叠时不能先复制")
                self.assertEqual(_resolved_link_target(outer / SKILL), previous.resolve())
                self.assertEqual(list(previous.iterdir()), [], "不能在旧链接目标内创建子链接")

    @unittest.skipUnless(CAN_SYMLINK, "Windows 当前账户无符号链接权限")
    def test_doctor_fix_command_repairs_custom_path_with_shell_characters(self):
        custom = self.custom_dir("path with space & 'quote'")
        p = self.run_ae("install", "--link", f"cursor={custom}")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        link = custom / SKILL
        link.unlink()
        result = self.doctor_result("skill-links")
        command = ae.command_tokens(result["fix_command"]) if os.name == "nt" else shlex.split(result["fix_command"])
        p = subprocess.run(command, env=self.env, capture_output=True, text=True, timeout=60)
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertTrue(link.is_symlink())
        self.assertEqual(link.resolve(), self.copy_dir.resolve())

    @unittest.skipUnless(CAN_SYMLINK, "Windows 当前账户无符号链接权限")
    def test_uninstall_removes_custom_link(self):
        custom = self.custom_dir()
        self.assertEqual(self.run_ae("install", "--link", f"cursor={custom}").returncode, 0)
        p = self.run_ae("uninstall")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertFalse(self.custom_link().exists(), "自定义链接应随卸载删除")
        self.assertFalse(self.copy_dir.exists())

    @unittest.skipUnless(CAN_SYMLINK, "Windows 当前账户无符号链接权限")
    def test_repair_rebuilds_custom_link(self):
        custom = self.custom_dir()
        self.assertEqual(self.run_ae("install", "--link", f"cursor={custom}").returncode, 0)
        os.unlink(self.custom_link())   # 链接被删：doctor 应报失败，repair 应按记录重建
        p = self.run_ae("doctor", "--json")
        result = self.doctor_result("skill-links")
        self.assertEqual(result["level"], "失败")
        self.assertIn("cursor=", result["fix_command"], "修复命令应能复现自定义链接")
        p = self.run_ae("repair")
        self.assertIn(p.returncode, (0, 1), p.stdout + p.stderr)
        self.assertTrue(self.custom_link().is_symlink(), "repair 应重建自定义链接")
        self.assertEqual(os.path.realpath(str(self.custom_link())), str(self.copy_dir.resolve()))

    def test_windows_link_privilege_error_fails_cleanly_with_target_guidance(self):
        import contextlib, io, unittest.mock
        previous = ae.current_locale()
        self.addCleanup(ae.set_locale, previous)
        custom = self.custom_dir("中文 目标")
        win_err = OSError(1314, "A required privilege is not held by the client")
        win_err.winerror = 1314
        out, err = io.StringIO(), io.StringIO()
        with unittest.mock.patch.dict(os.environ, {**self.env, "AE_LOCALE": "zh-CN"}), \
                unittest.mock.patch.object(ae, "_is_windows", return_value=True, create=True), \
                unittest.mock.patch("ae.os.symlink", side_effect=win_err), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = ae.main(["install", "--link", f"cursor={custom}"])
        self.assertEqual(code, 2, out.getvalue() + err.getvalue())
        self.assertEqual(out.getvalue(), "", "未完成链接时不能输出安装成功")
        self.assertIn("--link", err.getvalue())
        self.assertIn(str(custom), err.getvalue())
        self.assertNotIn("--target", err.getvalue())
        self.assertFalse(self.copy_dir.exists(), "全新 --link 失败时不留下部分安装副本")
        self.assertFalse((custom / SKILL).exists())

        # 第一条链接成功、第二条触发 1314 时，应回滚第一条链接，不留半成品
        calls = {"n": 0}
        real_symlink = os.symlink

        def fail_second(src, dst, target_is_directory=False):
            calls["n"] += 1
            if calls["n"] == 2:
                raise win_err
            return real_symlink(src, dst, target_is_directory=target_is_directory)

        out2, err2 = io.StringIO(), io.StringIO()
        with unittest.mock.patch.dict(os.environ, {**self.env, "AE_LOCALE": "en"}), \
                unittest.mock.patch.object(ae, "_is_windows", return_value=True, create=True), \
                unittest.mock.patch("ae.os.symlink", side_effect=fail_second), \
                contextlib.redirect_stdout(out2), contextlib.redirect_stderr(err2):
            code2 = ae.main(["install", "--link", f"codex,cursor={custom}"])
        self.assertEqual(code2, 2)
        self.assertFalse(agent_link(self.home, "codex").exists(), "部分建出的链接应回滚")
        self.assertIn("--link", err2.getvalue())

        # 已有安装副本时 --link 触发 1314：必须保留/恢复原副本与 .install-record.json
        with unittest.mock.patch.dict(os.environ, self.env), \
                contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(ae.main(["install"]), 0)
        record_before = (self.copy_dir / ae.RECORD_NAME).read_text(encoding="utf-8")
        out3, err3 = io.StringIO(), io.StringIO()
        with unittest.mock.patch.dict(os.environ, {**self.env, "AE_LOCALE": "zh-CN"}), \
                unittest.mock.patch.object(ae, "_is_windows", return_value=True, create=True), \
                unittest.mock.patch("ae.os.symlink", side_effect=win_err), \
                contextlib.redirect_stdout(out3), contextlib.redirect_stderr(err3):
            code3 = ae.main(["install", "--link", f"cursor={custom}"])
        self.assertEqual(code3, 2)
        self.assertTrue((self.copy_dir / ae.RECORD_NAME).is_file())
        self.assertTrue((self.copy_dir / "SKILL.md").is_file())
        self.assertEqual((self.copy_dir / ae.RECORD_NAME).read_text(encoding="utf-8"), record_before)

    @unittest.skipUnless(CAN_SYMLINK, "Windows 当前账户无符号链接权限")
    def test_existing_correct_links_need_no_symlink_privilege(self):
        import contextlib, io, unittest.mock as mock
        self.addCleanup(ae.set_locale, ae.current_locale())
        self.assertEqual(self.run_ae("install", "--link", "codex").returncode, 0)
        link = agent_link(self.home, "codex")
        old_target = os.readlink(link)
        win_err = OSError(1314, "synthetic missing symlink privilege")
        win_err.winerror = 1314
        for command in ("install", "upgrade"):
            with self.subTest(command=command), mock.patch.dict(os.environ, self.env), \
                    mock.patch.object(ae, "list_processes", return_value=[]), \
                    mock.patch("ae.os.symlink", side_effect=win_err) as symlink, \
                    contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(ae.main([command, "--link", "codex"]), 0)
            symlink.assert_not_called()
            self.assertEqual(os.readlink(link), old_target)

    @unittest.skipUnless(CAN_SYMLINK, "Windows 当前账户无符号链接权限")
    def test_link_failure_restores_replaced_links_and_preserves_existing_links(self):
        import contextlib, io, unittest.mock as mock
        self.addCleanup(ae.set_locale, ae.current_locale())
        self.assertEqual(self.run_ae("install", "--link", "codex").returncode, 0)
        old_target = self.home / "previous-skill"
        old_target.mkdir()
        replaced = self.custom_dir("replaced") / SKILL
        failed = self.custom_dir("failed") / SKILL
        fresh = self.custom_dir("fresh") / SKILL
        for link in (replaced, failed):
            link.parent.mkdir(parents=True)
            link.symlink_to(old_target, target_is_directory=True)
        original_targets = {link: os.readlink(link) for link in (agent_link(self.home, "codex"), replaced, failed)}
        record_before = (self.copy_dir / RECORD_NAME).read_bytes()
        real_symlink = os.symlink
        win_err = OSError(1314, "synthetic missing symlink privilege")
        win_err.winerror = 1314

        def fail_last(src, dst, target_is_directory=False):
            if Path(dst) == failed:
                raise win_err
            return real_symlink(src, dst, target_is_directory=target_is_directory)

        links = f"codex,old={replaced.parent},new={fresh.parent},failed={failed.parent}"
        for command in ("install", "upgrade"):
            with self.subTest(command=command), mock.patch.dict(os.environ, self.env), \
                    mock.patch.object(ae, "_is_windows", return_value=True), \
                    mock.patch.object(ae, "list_processes", return_value=[]), \
                    mock.patch("ae.os.symlink", side_effect=fail_last), \
                    contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(ae.main([command, "--link", links]), 2)
            self.assertEqual((self.copy_dir / RECORD_NAME).read_bytes(), record_before)
            self.assertTrue((self.copy_dir / "SKILL.md").is_file())
            self.assertFalse(fresh.is_symlink(), "本次新建链接必须回滚")
            for link, target in original_targets.items():
                self.assertEqual(os.readlink(link), target, "原有链接必须完整保留")
            self.assertEqual(list(self.home.rglob(".ae-link-backup-*")), [])

    @unittest.skipUnless(CAN_SYMLINK, "Windows 当前账户无符号链接权限")
    def test_link_rollback_does_not_replace_an_existing_directory_symlink(self):
        import unittest.mock as mock
        original = self.home / "original-skill"
        replacement = self.home / "replacement-skill"
        original.mkdir()
        replacement.mkdir()
        directory = self.custom_dir("rollback")
        directory.mkdir()
        link = directory / SKILL
        link.symlink_to(original, target_is_directory=True)
        old_target = os.readlink(link)
        real_replace = os.replace

        def windows_replace(src, dst):
            if Path(dst).is_symlink() and Path(dst).is_dir():
                raise PermissionError("Windows cannot replace a directory symlink")
            return real_replace(src, dst)

        with mock.patch("ae.os.replace", side_effect=windows_replace):
            with ae.LinkTransaction() as changes:
                changes.create("synthetic", directory, replacement)
                self.assertEqual(link.resolve(), replacement.resolve())
                # 未提交事务：模拟后续链接或安装核验失败。
        self.assertEqual(os.readlink(link), old_target)
        self.assertEqual(list(directory.glob(".ae-link-backup-*")), [])

    @unittest.skipUnless(CAN_SYMLINK, "Windows 当前账户无符号链接权限")
    def test_upgrade_post_verification_failure_rolls_back_link_changes(self):
        import contextlib, io, unittest.mock as mock
        self.addCleanup(ae.set_locale, ae.current_locale())
        self.assertEqual(self.run_ae("install", "--link", "codex").returncode, 0)
        old_target = self.home / "previous-skill"
        old_target.mkdir()
        link = self.custom_dir("replacement") / SKILL
        link.parent.mkdir(parents=True)
        link.symlink_to(old_target, target_is_directory=True)
        target_before = os.readlink(link)
        record_before = (self.copy_dir / RECORD_NAME).read_bytes()
        real_write = ae.write_record

        def corrupt_stage(dst, repo, links=None):
            result = real_write(dst, repo, links)
            if ".ae-upgrade-stage-" in str(dst):
                (dst / "SKILL.md").unlink()
            return result

        with mock.patch.dict(os.environ, self.env), \
                mock.patch.object(ae, "list_processes", return_value=[]), \
                mock.patch.object(ae, "write_record", side_effect=corrupt_stage), \
                contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            code = ae.main(["upgrade", "--link", f"codex,old={link.parent}"])
        self.assertEqual(code, 1)
        self.assertEqual(os.readlink(link), target_before)
        self.assertTrue(agent_link(self.home, "codex").is_symlink())
        self.assertEqual((self.copy_dir / RECORD_NAME).read_bytes(), record_before)

    def test_windows_privilege_error_preserves_target_and_link_intent_across_commands(self):
        import contextlib, io, unittest.mock
        previous = ae.current_locale()
        self.addCleanup(ae.set_locale, previous)
        custom_target = self.home.parent / "real-target"
        custom_link = self.custom_dir("cursor-dir")
        win_err = OSError(1314, "A required privilege is not held by the client")
        win_err.winerror = 1314

        with unittest.mock.patch.object(ae, "_is_windows", return_value=True, create=True):
            expected_target = f"--target {ae._quote_cli(str(custom_target))}"
            expected_link = f"--link {ae._quote_cli(f'cursor={custom_link}')}"
            unexpected_target = f"--target {ae._quote_cli(str(custom_link))}"

        # 1) cmd_install: preserves real --target and --link intent, never passes link dir as --target
        out, err = io.StringIO(), io.StringIO()
        with unittest.mock.patch.dict(os.environ, {**self.env, "AE_LOCALE": "en"}), \
                unittest.mock.patch.object(ae, "_is_windows", return_value=True, create=True), \
                unittest.mock.patch("ae.os.symlink", side_effect=win_err), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = ae.main(["install", "--target", str(custom_target), "--link", f"cursor={custom_link}"])
        self.assertEqual(code, 2)
        msg = err.getvalue()
        self.assertIn(expected_target, msg)
        self.assertIn(expected_link, msg)
        self.assertNotIn(unexpected_target, msg)
        self.assertNotIn(f"--target {custom_link}", msg)

        # 2) cmd_upgrade: install directly first, then fail symlink in upgrade
        with unittest.mock.patch.dict(os.environ, self.env), \
                contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(ae.main(["install", "--target", str(custom_target)]), 0)
        out_up, err_up = io.StringIO(), io.StringIO()
        with unittest.mock.patch.dict(os.environ, {**self.env, "AE_LOCALE": "en"}), \
                unittest.mock.patch.object(ae, "_is_windows", return_value=True, create=True), \
                unittest.mock.patch.object(ae, "list_processes", return_value=[]), \
                unittest.mock.patch("ae.os.symlink", side_effect=win_err), \
                contextlib.redirect_stdout(out_up), contextlib.redirect_stderr(err_up):
            code_up = ae.main(["upgrade", "--target", str(custom_target), "--link", f"cursor={custom_link}"])
        self.assertEqual(code_up, 2)
        msg_up = err_up.getvalue()
        self.assertIn(expected_target, msg_up)
        self.assertIn(expected_link, msg_up)
        self.assertNotIn(unexpected_target, msg_up)
        self.assertNotIn(f"--target {custom_link}", msg_up)

        # 3) repair_links: preserves ctx.target and link spec
        copy_dir = custom_target / SKILL
        record = {"links": [{"agent": "cursor", "path": str(custom_link / SKILL)}]}
        ctx = ae.DoctorContext(skill_copy=copy_dir, repo=ROOT, record=record, target=custom_target)
        with unittest.mock.patch.object(ae, "_is_windows", return_value=True, create=True), \
                unittest.mock.patch("ae.os.symlink", side_effect=win_err):
            created, stuck = ae.repair_links(ctx)
        self.assertEqual(created, [])
        self.assertEqual(len(stuck), 1)
        self.assertIn(expected_target, stuck[0])
        self.assertIn(expected_link, stuck[0])
        self.assertNotIn(unexpected_target, stuck[0])
        self.assertNotIn(f"--target {custom_link}", stuck[0])


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
        self._saved = {key: os.environ.get(key) for key in ("HOME", "USERPROFILE", "CODEX_HOME", "CLAUDE_SKILLS_DIR")}
        os.environ.update({"HOME": str(self.home), "USERPROFILE": str(self.home),
                           "CODEX_HOME": str(self.home / ".codex"),
                           "CLAUDE_SKILLS_DIR": str(self.skills)})

    def tearDown(self):
        for key, value in self._saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    def make_link(self, agent: str, target=None) -> dict:
        """在 Agent 技能目录建软链接，返回与安装记录同构的条目。"""
        if not CAN_SYMLINK:
            self.skipTest("Windows 当前账户无符号链接权限")
        link = agent_link(self.home, agent)
        link.parent.mkdir(parents=True, exist_ok=True)
        os.symlink(str(target if target is not None else self.copy.resolve()), str(link), target_is_directory=True)
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


class ParseLinkAgentsTests(unittest.TestCase):
    """parse_link_agents 的写法解析：内置名称、名称=路径、去重与报错。"""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="ae-parse-link-")
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name) / "home"
        self.home.mkdir()
        self._saved = {key: os.environ.get(key) for key in ("HOME", "USERPROFILE")}
        os.environ.update({"HOME": str(self.home), "USERPROFILE": str(self.home)})

    def tearDown(self):
        for key, value in self._saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    def test_builtin_names_map_to_their_dirs(self):
        pairs = ae.parse_link_agents("codex,agents,zcode")
        self.assertEqual([name for name, _dir in pairs], ["codex", "agents", "zcode"])
        for name, directory in pairs:
            self.assertEqual(directory, ae.link_target_dir(name))

    def test_shared_directory_keeps_all_registered_names(self):
        shared = self.home / ".agents" / "skills"
        with mock.patch.dict(os.environ, {
                "CODEX_HOME": str(shared.parent), "CLAUDE_SKILLS_DIR": str(shared)}):
            self.assertEqual(ae.parse_link_agents("codex,claude-code,agents"),
                             [(name, shared) for name in ("codex", "claude-code", "agents")])
            scanned = [(name, path) for name, path in ae.known_skill_roots() if path == shared]
        self.assertEqual(scanned, [("codex", shared)], "扫描同一路径仍只做一次")

    def test_unknown_bare_name_keeps_none_dir(self):
        pairs = ae.parse_link_agents("foo")
        self.assertEqual(pairs, [("foo", None)], "未知名称交给调用方报错")

    def test_name_path_maps_to_given_dir(self):
        pairs = ae.parse_link_agents("cursor=~/.cursor/skills, my-agent=/opt/skills ")
        self.assertEqual(pairs, [("cursor", self.home / ".cursor" / "skills"),
                                 ("my-agent", Path(os.path.abspath("/opt/skills")))])

    def test_dedup_keeps_first_occurrence(self):
        pairs = ae.parse_link_agents("cursor=/a, codex, cursor=/b")
        self.assertEqual([name for name, _dir in pairs], ["cursor", "codex"])
        self.assertEqual(pairs[0][1], Path(os.path.abspath("/a")))

    def test_blank_parts_skipped(self):
        self.assertEqual(ae.parse_link_agents(" codex ,, "), [("codex", ae.link_target_dir("codex"))])
        self.assertEqual(ae.parse_link_agents(""), [])
        self.assertEqual(ae.parse_link_agents(","), [])

    def test_malformed_spec_raises_command_error(self):
        for spec in ("=/some/dir", "cursor=", "= "):
            with self.assertRaises(ae.CommandError, msg=spec):
                ae.parse_link_agents(spec)


class LinkFixSpecTests(unittest.TestCase):
    """doctor 的链接修复命令：内置名称用裸名，自定义目录还原成 名称=路径。"""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="ae-link-fix-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.home = self.root / "home"
        self.home.mkdir()
        self.skills = self.home / ".claude" / "skills"
        self.copy = self.skills / SKILL
        self.copy.mkdir(parents=True)
        (self.copy / "SKILL.md").write_text("---\nname: annotation-everything\n---\n合成技能\n",
                                            encoding="utf-8")
        self._saved = {key: os.environ.get(key) for key in ("HOME", "USERPROFILE", "CODEX_HOME", "CLAUDE_SKILLS_DIR")}
        os.environ.update({"HOME": str(self.home), "USERPROFILE": str(self.home),
                           "CODEX_HOME": str(self.home / ".codex"),
                           "CLAUDE_SKILLS_DIR": str(self.skills)})

    def tearDown(self):
        for key, value in self._saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    def ctx(self, record):
        return ae.DoctorContext(skill_copy=self.copy, repo=self.root / "repo", record=record)

    def base_record(self, *entries):
        return {"repo": str(self.root / "repo"), "commit": "0" * 40, "version": "9.9.9",
                "installed_at": "2026-10-29T00:00:00+08:00", "links": list(entries)}

    @unittest.skipUnless(CAN_SYMLINK, "Windows 当前账户无符号链接权限")
    def test_fix_command_uses_name_path_for_custom_entries(self):
        custom = self.home / "cursor-skills"
        link = custom / SKILL
        link.parent.mkdir(parents=True)
        os.symlink(str(self.root / "no-such-copy"), str(link), target_is_directory=True)   # 悬空
        record = self.base_record({"agent": "cursor", "path": str(link)})
        result = ae.check_links(self.ctx(record))
        self.assertEqual(result.level, ae.LEVEL_FAIL)
        self.assertIn(f"install --link {ae._quote_cli(f'cursor={custom}')}", result.fix_command)

    @unittest.skipUnless(CAN_SYMLINK, "Windows 当前账户无符号链接权限")
    def test_fix_command_keeps_bare_names_for_builtin_entries(self):
        link = self.home / ".codex" / "skills" / SKILL
        link.parent.mkdir(parents=True)
        os.symlink(str(self.root / "no-such-copy"), str(link), target_is_directory=True)
        record = self.base_record({"agent": "codex", "path": str(link)})
        result = ae.check_links(self.ctx(record))
        self.assertEqual(result.level, ae.LEVEL_FAIL)
        self.assertIn("install --link codex", result.fix_command)
        self.assertNotIn("codex=", result.fix_command)

    @unittest.skipUnless(CAN_SYMLINK, "Windows 当前账户无符号链接权限")
    def test_fix_command_rebuilds_moved_builtin_at_recorded_location(self):
        elsewhere = self.home / "moved-skills"
        link = elsewhere / SKILL
        link.parent.mkdir(parents=True)
        os.symlink(str(self.root / "no-such-copy"), str(link), target_is_directory=True)
        record = self.base_record({"agent": "codex", "path": str(link)})
        result = ae.check_links(self.ctx(record))
        self.assertIn(f"codex={elsewhere}", result.fix_command, "内置名称但位置对不上时按记录位置重建")

    def test_fix_command_carries_target(self):
        target = self.root / "custom"
        result = ae.check_links(ae.DoctorContext(
            skill_copy=target / SKILL, repo=self.root / "repo",
            record=self.base_record({"agent": "codex", "path": str(self.home / ".codex" / "skills" / SKILL)}),
            target=target))
        self.assertIn("--target", result.fix_command)

    @unittest.skipUnless(CAN_SYMLINK, "Windows 当前账户无符号链接权限")
    def test_old_record_entry_without_agent_field_is_tolerated(self):
        link = self.home / ".codex" / "skills" / SKILL
        link.parent.mkdir(parents=True)
        os.symlink(str(self.copy.resolve()), str(link), target_is_directory=True)
        record = self.base_record({"path": str(link)})   # 旧记录缺 agent 字段：按缺省处理，不报错
        result = ae.check_links(self.ctx(record))
        self.assertEqual(result.level, ae.LEVEL_PASS)
        self.assertIn(str(link), result.message)

    @unittest.skipUnless(CAN_SYMLINK, "Windows 当前账户无符号链接权限")
    def test_record_without_links_key_falls_back_to_scan(self):
        link = self.home / ".agents" / "skills" / SKILL   # 回退扫描看已登记的宿主目录
        link.parent.mkdir(parents=True)
        os.symlink(str(self.copy.resolve()), str(link), target_is_directory=True)
        record = self.base_record()
        del record["links"]                              # 更老的记录没有 links 键
        result = ae.check_links(self.ctx(record))
        self.assertEqual(result.level, ae.LEVEL_PASS)
        self.assertIn("agents", result.message)

    def test_install_link_fix_formats_specs_and_target(self):
        py = ae._python_cli()
        repo = Path("/repo")
        self.assertEqual(ae.install_link_fix(repo, ["codex"]),
                         f"{py} {ae._quote_cli(str(repo / 'ae.py'))} install --link codex")
        target = Path("/dir with space")
        self.assertEqual(ae.install_link_fix(None, ["codex", "cursor=/x y"], target=target),
                         f"{py} ae.py install --link {ae._quote_cli('codex,cursor=/x y')} --target {ae._quote_cli(str(target))}")

    @unittest.skipUnless(CAN_SYMLINK, "Windows 当前账户无符号链接权限")
    def test_merged_links_keeps_custom_entries(self):
        custom = self.home / "cursor-skills"
        link = custom / SKILL
        link.parent.mkdir(parents=True)
        os.symlink(str(self.copy.resolve()), str(link), target_is_directory=True)
        previous = self.base_record({"agent": "cursor", "path": str(link)})
        merged = ae.merged_links(previous, self.copy, [])
        self.assertEqual(merged, [{"agent": "cursor", "path": str(link)}])


class IndependentCopyTests(unittest.TestCase):
    """已登记宿主目录里的实目录副本：只在合成家目录里体检，不读用户的技能目录。"""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="ae-independent-")
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name) / "home"
        self.home.mkdir()
        self.skills = self.home / ".claude" / "skills"
        self._saved = {key: os.environ.get(key) for key in (
            "HOME", "USERPROFILE", "CODEX_HOME", "CLAUDE_SKILLS_DIR",
            "XDG_CONFIG_HOME", "GROK_HOME", "HERMES_HOME", "AUTOHAND_HOME",
            "VIBE_HOME", "REASONIX_HOME", "CLAUDE_CONFIG_DIR")}
        for key in self._saved:
            os.environ.pop(key, None)
        os.environ.update({
            "HOME": str(self.home),
            "USERPROFILE": str(self.home),
            "CODEX_HOME": str(self.home / ".codex"),
            "CLAUDE_SKILLS_DIR": str(self.skills),
        })
        self.env = dict(os.environ)

    def tearDown(self):
        for key, value in self._saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    def run_ae(self, *args):
        return subprocess.run([sys.executable, str(AE), *args], cwd=self.home,
                              env=self.env, capture_output=True, text=True, timeout=60)

    @property
    def copy_dir(self) -> Path:
        return self.skills / SKILL

    def doctor_result(self, code: str, *args) -> dict:
        p = self.run_ae("doctor", "--json", *args)
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        results = [item for item in json.loads(p.stdout)["results"] if item["code"] == code]
        self.assertEqual(len(results), 1, p.stdout + p.stderr)
        return results[0]

    def plant(self, *parts) -> Path:
        path = self.home.joinpath(*parts) / SKILL
        path.mkdir(parents=True)
        (path / "keep.txt").write_text("保留", encoding="utf-8")
        return path

    def test_canonical_copy_is_not_reported(self):
        self.assertEqual(self.run_ae("install").returncode, 0)
        result = self.doctor_result("independent-copies")
        self.assertEqual(result["level"], "通过")
        self.assertEqual(result["status"], "pass")

    def test_real_directory_warns_even_when_link_record_is_empty(self):
        self.assertEqual(self.run_ae("install").returncode, 0)
        antigravity = self.plant(".gemini", "config", "skills")
        gemini = self.plant(".gemini", "skills")
        result = self.doctor_result("independent-copies")
        self.assertEqual(result["level"], "警告")
        self.assertEqual(result["status"], "warn")
        self.assertIn(str(antigravity), result["message"])
        self.assertIn(str(gemini), result["message"])
        self.assertIn("--replace-with-links", result["fix_command"])
        self.assertIn(f"install --target {ae._quote_cli(str(antigravity.parent))}", result["fix_command"])
        self.assertIn(f"install --target {ae._quote_cli(str(gemini.parent))}", result["fix_command"])
        links = self.doctor_result("skill-links")
        self.assertEqual(links["level"], "通过")
        self.assertIn("未发现指向技能副本的链接", links["message"])
        self.assertTrue((antigravity / "keep.txt").is_file())

    def test_english_warning_names_the_same_paths(self):
        self.assertEqual(self.run_ae("install").returncode, 0)
        antigravity = self.plant(".gemini", "config", "skills")
        p = self.run_ae("doctor", "--json", "--locale", "en")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        result = next(item for item in json.loads(p.stdout)["results"]
                      if item["code"] == "independent-copies")
        self.assertEqual(result["level"], "警告")
        self.assertIn(str(antigravity), result["message"])
        self.assertIn("symlink", result["message"].lower())
        self.assertIn("--replace-with-links", result["fix_command"])

    @unittest.skipUnless(CAN_SYMLINK, "Windows 当前账户无符号链接权限")
    def test_symlink_to_canonical_copy_passes(self):
        self.assertEqual(self.run_ae("install", "--link", "antigravity,gemini-cli").returncode, 0)
        for parts in ((".gemini", "config", "skills"), (".gemini", "skills")):
            link = self.home.joinpath(*parts) / SKILL
            self.assertTrue(link.is_symlink(), parts)
        result = self.doctor_result("independent-copies")
        self.assertEqual(result["level"], "通过")
        links = self.doctor_result("skill-links")
        self.assertEqual(links["level"], "通过")

    def test_target_copy_is_the_canonical_one(self):
        gemini_root = self.home / ".gemini" / "skills"
        self.assertEqual(self.run_ae("install", "--target", str(gemini_root)).returncode, 0)
        codex = self.plant(".codex", "skills")
        result = self.doctor_result("independent-copies", "--target", str(gemini_root))
        self.assertIn(str(codex), result["message"])
        self.assertNotIn(str(gemini_root / SKILL), result["message"])

    @unittest.skipUnless(CAN_SYMLINK, "Windows 当前账户无符号链接权限")
    def test_install_link_accepts_name_when_codex_shares_its_directory(self):
        self.env["CODEX_HOME"] = str(self.home / ".agents")
        p = self.run_ae("install", "--link", "agents")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        link = self.home / ".agents" / "skills" / SKILL
        self.assertTrue(link.is_symlink())
        self.assertEqual(link.resolve(), self.copy_dir.resolve())

    def test_replace_refuses_source_and_overlapping_directories(self):
        repo = self.home / "source-repo"
        source = repo / ae.SKILL_SRC
        nested = source / "nested"
        nested.mkdir(parents=True)
        marker = nested / "uncommitted.txt"
        marker.write_text("合成未提交改动", encoding="utf-8")
        self.copy_dir.mkdir(parents=True)
        # 同时保护显式来源仓库，以及没有来源记录时执行脚本所在的仓库。
        for script_repo, recorded_repo in ((self.home / "runner", repo), (repo, None)):
            with mock.patch.object(ae, "__file__", str(script_repo / "ae.py")):
                for occupied in (source, source.parent, nested):
                    with self.subTest(script_repo=script_repo, occupied=occupied):
                        with mock.patch.object(ae.os, "replace") as move:
                            message = ae.replace_independent_copy(occupied, self.copy_dir, recorded_repo)
                        move.assert_not_called()
                        self.assertIsNotNone(message)
                        self.assertIn(str(source), message)
                        self.assertFalse(occupied.is_symlink())
                        self.assertEqual(marker.read_text(encoding="utf-8"), "合成未提交改动")

    def test_every_registered_root_is_checked_together(self):
        self.assertEqual(self.run_ae("install").returncode, 0)
        canonical = os.path.normcase(os.path.abspath(self.copy_dir))
        planted = []
        for _name, root in ae.known_skill_roots():
            candidate = root / SKILL
            if os.path.normcase(os.path.abspath(candidate)) == canonical:
                continue
            candidate.mkdir(parents=True)
            (candidate / "keep.txt").write_text("保留", encoding="utf-8")
            planted.append(candidate)
        self.assertGreaterEqual(len(planted), 10)
        result = self.doctor_result("independent-copies")
        for path in planted:
            self.assertIn(str(path), result["message"])
            self.assertTrue((path / "keep.txt").is_file())
        self.assertNotIn(str(self.copy_dir), result["message"])

    def test_repair_without_flag_keeps_the_real_directory(self):
        self.assertEqual(self.run_ae("install").returncode, 0)
        antigravity = self.plant(".gemini", "config", "skills")
        p = self.run_ae("repair")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertIn("--replace-with-links", p.stdout)
        self.assertIn("本次未执行", p.stdout)
        self.assertIn(str(antigravity), p.stdout)
        self.assertTrue((antigravity / "keep.txt").is_file())
        self.assertFalse(antigravity.is_symlink())

    @unittest.skipUnless(CAN_SYMLINK, "当前账户无符号链接权限")
    def test_replace_flag_swaps_real_directory_for_symlink_and_records_it(self):
        self.assertEqual(self.run_ae("install").returncode, 0)
        antigravity = self.plant(".gemini", "config", "skills")
        sibling = antigravity.parent / "keep-sibling.txt"
        sibling.write_text("旁边的文件", encoding="utf-8")
        p = self.run_ae("repair", "--replace-with-links")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertLess(p.stdout.index("修复计划"), p.stdout.index("执行修复"))
        self.assertTrue(antigravity.is_symlink())
        self.assertEqual(os.path.realpath(str(antigravity)), str(self.copy_dir.resolve()))
        self.assertFalse((antigravity / "keep.txt").is_file())
        self.assertTrue(sibling.is_file())
        record = json.loads((self.copy_dir / RECORD_NAME).read_text(encoding="utf-8"))
        self.assertIn(str(antigravity), [entry["path"] for entry in record["links"]])

    @unittest.skipUnless(CAN_SYMLINK, "当前账户无符号链接权限")
    def test_replace_keeps_recorded_path_that_is_no_longer_a_link(self):
        """替换一份已登记实目录时，记录里另一条已不是链接的路径仍留下，下次体检仍失败。"""
        self.assertEqual(self.run_ae("install").returncode, 0)
        antigravity = self.plant(".gemini", "config", "skills")
        custom = self.home / "custom-skills" / SKILL
        custom.mkdir(parents=True)
        (custom / "keep.txt").write_text("保留", encoding="utf-8")
        record_path = self.copy_dir / RECORD_NAME
        record = json.loads(record_path.read_text(encoding="utf-8"))
        record["links"] = [{"agent": "custom", "path": str(custom)}]
        record_path.write_text(json.dumps(record, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        p = self.run_ae("repair", "--replace-with-links")
        # custom 无法自动修复（请手工处理），修复后体检仍失败，repair 退出码跟随体检
        self.assertEqual(p.returncode, 1, p.stdout + p.stderr)
        self.assertTrue(antigravity.is_symlink())
        updated = json.loads(record_path.read_text(encoding="utf-8"))
        paths = [entry["path"] for entry in updated["links"]]
        self.assertIn(str(custom), paths)
        self.assertIn(str(antigravity), paths)
        self.assertEqual(updated.get("tree"), record.get("tree"))
        self.assertEqual(updated.get("version"), record.get("version"))
        self.assertTrue((custom / "keep.txt").is_file())
        doctor = self.run_ae("doctor", "--json")
        self.assertEqual(doctor.returncode, 1, doctor.stdout + doctor.stderr)
        links = next(item for item in json.loads(doctor.stdout)["results"]
                     if item["code"] == "skill-links")
        self.assertEqual(links["level"], "失败")
        self.assertIn(str(custom), links["message"])
        self.assertIn("已不是链接", links["message"])
        self.assertIn("install --link", links["fix_command"])

    @unittest.skipUnless(CAN_SYMLINK, "当前账户无符号链接权限")
    def test_replace_preserves_scanned_links_from_legacy_record(self):
        p = self.run_ae("install", "--link", "codex")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        original_link = self.home / ".codex" / "skills" / SKILL
        record_path = self.copy_dir / RECORD_NAME
        record = json.loads(record_path.read_text(encoding="utf-8"))
        del record["links"]
        record_path.write_text(json.dumps(record), encoding="utf-8")
        replaced = self.plant(".gemini", "config", "skills")
        p = self.run_ae("repair", "--replace-with-links")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        updated = json.loads(record_path.read_text(encoding="utf-8"))
        self.assertEqual({entry["path"] for entry in updated["links"]},
                         {str(original_link), str(replaced)})
        self.assertEqual(updated.get("tree"), record.get("tree"))
        self.assertEqual(updated.get("version"), record.get("version"))
        p = self.run_ae("uninstall")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        for link in (original_link, replaced):
            self.assertFalse(link.exists())
            self.assertFalse(link.is_symlink(), "卸载不得留下悬空链接")

    @unittest.skipUnless(CAN_SYMLINK, "当前账户无符号链接权限")
    def test_replace_does_not_scan_when_record_explicitly_has_no_links(self):
        p = self.run_ae("install", "--link", "codex")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        record_path = self.copy_dir / RECORD_NAME
        record = json.loads(record_path.read_text(encoding="utf-8"))
        record["links"] = []
        record_path.write_text(json.dumps(record), encoding="utf-8")
        replaced = self.plant(".gemini", "config", "skills")
        p = self.run_ae("repair", "--replace-with-links")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        updated = json.loads(record_path.read_text(encoding="utf-8"))
        self.assertEqual([entry["path"] for entry in updated["links"]], [str(replaced)])

    @unittest.skipUnless(CAN_SYMLINK, "当前账户无符号链接权限")
    def test_dry_run_with_flag_does_not_replace(self):
        self.assertEqual(self.run_ae("install").returncode, 0)
        antigravity = self.plant(".gemini", "config", "skills")
        p = self.run_ae("repair", "--dry-run", "--replace-with-links")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertIn("--dry-run", p.stdout)
        self.assertTrue((antigravity / "keep.txt").is_file())
        self.assertFalse(antigravity.is_symlink())

    def test_install_link_still_refuses_real_directory(self):
        occupied = self.plant(".gemini", "config", "skills")
        p = self.run_ae("install", "--link", "antigravity")
        self.assertEqual(p.returncode, 2, p.stdout + p.stderr)
        self.assertIn(str(occupied), p.stderr)
        self.assertTrue((occupied / "keep.txt").is_file())
        self.assertFalse(self.copy_dir.exists())

    def test_uninstall_leaves_unrecorded_real_directory(self):
        self.assertEqual(self.run_ae("install").returncode, 0)
        occupied = self.plant(".gemini", "skills")
        p = self.run_ae("uninstall")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertTrue((occupied / "keep.txt").is_file())
        self.assertFalse(self.copy_dir.exists())

    def test_privilege_error_restores_the_real_directory(self):
        self.assertEqual(self.run_ae("install").returncode, 0)
        occupied = self.plant(".grok", "skills")
        with mock.patch.object(ae.os, "symlink", side_effect=PermissionError(1, "Operation not permitted")):
            message = ae.replace_independent_copy(occupied, self.copy_dir, ROOT)
        self.assertIsNotNone(message)
        self.assertIn("install --target", message)
        self.assertIn(str(occupied.parent), message)
        self.assertFalse(occupied.is_symlink())
        self.assertEqual((occupied / "keep.txt").read_text(encoding="utf-8"), "保留")

    def test_updater_does_not_scan_other_host_directories(self):
        text = (ROOT / "skill" / "annotation-everything" / "scripts" / "update.py").read_text(encoding="utf-8")
        self.assertNotIn("known_skill_roots", text)
        self.assertNotIn("find_independent_copies", text)
        self.assertNotIn("independent-copies", text)


if __name__ == "__main__":
    unittest.main()
