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
sys.path.insert(0, str(ROOT))

import host_isolation  # noqa: E402

# 直接跑 unittest 的旧入口与 verify.py 共用同一隔离协议：doctor / uninstall 读真实全机进程视图
host_isolation.require_for_module(__name__)

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
        self.env = {**os.environ, "HOME": str(self.home), "USERPROFILE": str(self.home),
                    "CLAUDE_SKILLS_DIR": str(self.skills),
                    "CODEX_HOME": str(self.home / ".codex"),  # 体检会看 $CODEX_HOME/skills，不能落到真实目录
                    "AE_HOME": str(self.root / "ledger"),     # 运行状态检查读台账：指向临时目录，不碰真实台账
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

    def test_installed_language_guides_and_their_links_are_complete(self):
        """安装副本能按语言找到操作说明，说明里的本地引用也必须随安装交付。"""
        p = self.run_ae("install")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        pending = [self.copy_dir / "SKILL.md"]
        visited = set()
        while pending:
            path = pending.pop()
            if path in visited:
                continue
            self.assertTrue(path.is_file(), f"安装副本缺少引用文件：{path}")
            visited.add(path)
            text = path.read_text(encoding="utf-8")
            for target in re.findall(r"\[[^\]]+\]\(([^)#]+)(?:#[^)]*)?\)", text):
                if "://" not in target and target.endswith(".md"):
                    linked = (path.parent / target).resolve()
                    self.assertTrue(linked.is_relative_to(self.copy_dir.resolve()))
                    pending.append(linked)
        self.assertTrue(any(p.name == "workflow.en.md" for p in visited), "英文流程可从技能入口到达")
        self.assertTrue(any(p.name == "workflow.zh-CN.md" for p in visited), "中文流程可从技能入口到达")

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
        install = [r for r in doc["results"] if r["category"] == "安装状态"]
        self.assertTrue(install and all(r["level"] == "通过" for r in install), doc["results"])
        self.assertTrue(any(r["category"] == "运行状态" for r in doc["results"]), "体检包含运行状态检查")
        # 退出码还取决于本机真实运行状态（可能有残留服务），这里只断言安装状态全部通过

    def test_doctor_fails_when_copy_missing(self):
        p, doc = self.doctor_json()
        self.assertEqual(p.returncode, 1)
        failing = [r for r in doc["results"] if r["level"] == "失败" and r["category"] == "安装状态"]
        self.assertEqual([r["code"] for r in failing], ["skill-copy-present"])
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
        failing = {r["code"]: r for r in doc["results"]
                   if r["level"] == "失败" and r["category"] == "安装状态"}
        self.assertEqual(list(failing), ["skill-copy-version"])
        self.assertIn("ae.py install", failing["skill-copy-version"]["fix_command"])

    def test_doctor_json_structure(self):
        self.assertEqual(self.run_ae("install").returncode, 0)
        p, doc = self.doctor_json()
        self.assertIn(p.returncode, (0, 1))     # 退出码取决于本机真实运行状态，结构则恒定
        for r in doc["results"]:
            # 旧消费者读的五个键原样保留；status / category_id 是 #33 新增的语言无关判定字段
            self.assertEqual(set(r) - {"status", "category_id"},
                             {"code", "category", "level", "message", "fix_command"})
            self.assertIn(r["status"], ("pass", "warn", "fail"))
            self.assertIn(r["category_id"], ("install", "tools", "dev", "runtime"))
        self.assertEqual(set(doc["summary"]), {"pass", "warn", "fail"})

    def test_doctor_text_output_groups_by_category(self):
        self.assertEqual(self.run_ae("install").returncode, 0)
        p = self.run_ae("doctor")
        self.assertIn(p.returncode, (0, 1))     # 退出码取决于本机真实运行状态
        self.assertIn("安装状态", p.stdout)
        self.assertIn("运行状态", p.stdout)
        self.assertIn("汇总", p.stdout)

    def test_install_and_doctor_with_chinese_and_space_target_path(self):
        target = self.root / "中文 用户名" / "技能 目标"
        p = self.run_ae("install", "--target", str(target))
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        copy = target / "annotation-everything"
        self.assertTrue((copy / "SKILL.md").is_file())
        d, doc = self.doctor_json("--target", str(target))
        install = [r for r in doc["results"] if r["category_id"] == "install"]
        self.assertTrue(install and all(r["status"] == "pass" for r in install), doc["results"])



def _rmtree(path: Path):
    import shutil
    shutil.rmtree(path, ignore_errors=True)


class WindowsUpdateNoticeTests(unittest.TestCase):
    """Windows 首期不执行自动原地更新：auto 不阻断且提示手动维护，now 明确说明手动路径。"""

    def test_windows_auto_update_is_non_blocking_and_points_to_manual_install(self):
        import contextlib, io, unittest.mock
        sys.path.insert(0, str(SKILL_SRC / "scripts"))
        import update

        for locale, needle in (("zh-CN", "手动"), ("en", "manual")):
            out, err = io.StringIO(), io.StringIO()
            with unittest.mock.patch.object(update.fsutil, "is_windows", return_value=True), \
                    unittest.mock.patch("update.has_legacy_process", side_effect=AssertionError("should not run ps")), \
                    unittest.mock.patch("update.exchange_directories", side_effect=AssertionError("should not swap")), \
                    contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                code = update.main(["auto", "--locale", locale])
            self.assertEqual(code, 0, out.getvalue() + err.getvalue())
            self.assertEqual(err.getvalue(), "")
            self.assertIn(needle, out.getvalue().lower() if locale == "en" else out.getvalue())
            self.assertIn("ae.py install", out.getvalue())

        out_quiet, err_quiet = io.StringIO(), io.StringIO()
        with unittest.mock.patch.dict(os.environ, {"AE_AUTO_UPDATE": "0"}), \
                unittest.mock.patch.object(update.fsutil, "is_windows", return_value=True), \
                contextlib.redirect_stdout(out_quiet), contextlib.redirect_stderr(err_quiet):
            code = update.main(["auto", "--locale", "en"])
        self.assertEqual(code, 0)
        self.assertEqual(out_quiet.getvalue() + err_quiet.getvalue(), "")

        out_now, err_now = io.StringIO(), io.StringIO()
        with unittest.mock.patch.object(update.fsutil, "is_windows", return_value=True), \
                unittest.mock.patch("update.has_legacy_process", side_effect=AssertionError("should not run ps")), \
                contextlib.redirect_stdout(out_now), contextlib.redirect_stderr(err_now):
            code_now = update.main(["now", "--locale", "en"])
        self.assertEqual(code_now, 1)
        self.assertIn("manual", err_now.getvalue().lower())
        self.assertIn("ae.py install", err_now.getvalue())


class AeUpgradeTests(unittest.TestCase):
    """ae.py upgrade 的手动升级契约：前置安全检查、拒绝不破坏旧副本、可恢复替换与数据保留。"""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="ae-upgrade-test-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.home = self.root / "home"
        self.home.mkdir()
        self.skills = self.home / ".claude" / "skills"
        self.ledger = self.root / "ledger"
        self.repo = self.root / "repo"
        import shutil
        shutil.copy2(AE, self.repo / "ae.py") if (self.repo.mkdir(), True) else None
        shutil.copytree(SKILL_SRC, self.repo / "skill" / "annotation-everything",
                        ignore=shutil.ignore_patterns("__pycache__", "*.pyc", ".DS_Store"))
        self.env = {**os.environ, "HOME": str(self.home), "USERPROFILE": str(self.home),
                    "CLAUDE_SKILLS_DIR": str(self.skills), "AE_HOME": str(self.ledger),
                    "PYTHONDONTWRITEBYTECODE": "1"}
        self.copy = self.skills / "annotation-everything"

    def run_repo_ae(self, *args: str):
        return subprocess.run([sys.executable, str(self.repo / "ae.py"), *args],
                              env=self.env, capture_output=True, text=True, timeout=60)

    def bump_repo_version(self, version: str = "9.9.9") -> None:
        serve = self.repo / "skill" / "annotation-everything" / "scripts" / "serve.py"
        text = serve.read_text(encoding="utf-8")
        serve.write_text(re.sub(r'^VERSION = "[^"]+"', f'VERSION = "{version}"', text, flags=re.M), encoding="utf-8")
        (self.repo / "skill" / "annotation-everything" / "upgrade-marker.txt").write_text(version, encoding="utf-8")

    def test_upgrade_updates_clean_copy_and_preserves_review_and_ledger_data(self):
        self.assertEqual(self.run_repo_ae("install", "--locale", "en").returncode, 0)
        review_dir = self.root / "synthetic-review"
        review_dir.mkdir()
        (review_dir / "notes.json").write_text('{"notes": [{"id": "n1", "text": "keep"}]}', encoding="utf-8")
        (review_dir / ".agent-binding.json").write_text('{"bindingId": "b1", "adapter": "wait"}', encoding="utf-8")
        self.ledger.mkdir(parents=True, exist_ok=True)
        ledger_file = self.ledger / "reviews.json"
        ledger_file.write_text(json.dumps({"version": 1, "reviews": {
            "0123456789abcdef": {"dir": str(review_dir), "port": 8805}}}), encoding="utf-8")

        self.bump_repo_version("9.9.9")
        p = self.run_repo_ae("upgrade", "--locale", "en")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertEqual((self.copy / "upgrade-marker.txt").read_text(encoding="utf-8"), "9.9.9")
        record = json.loads((self.copy / RECORD_NAME).read_text(encoding="utf-8"))
        self.assertEqual(record["version"], "9.9.9")
        self.assertEqual(json.loads((review_dir / "notes.json").read_text(encoding="utf-8"))["notes"][0]["id"], "n1")
        self.assertEqual(json.loads((review_dir / ".agent-binding.json").read_text(encoding="utf-8"))["bindingId"], "b1")
        self.assertEqual(json.loads(ledger_file.read_text(encoding="utf-8"))["reviews"]["0123456789abcdef"]["port"], 8805)

    def test_upgrade_refuses_when_local_modifications_exist_without_touching_copy(self):
        self.assertEqual(self.run_repo_ae("install").returncode, 0)
        (self.copy / "local-custom.txt").write_text("do not overwrite", encoding="utf-8")
        self.bump_repo_version("9.9.9")
        p = self.run_repo_ae("upgrade", "--locale", "en")
        self.assertEqual(p.returncode, 1, p.stdout + p.stderr)
        self.assertIn("local", p.stderr.lower())
        self.assertEqual((self.copy / "local-custom.txt").read_text(encoding="utf-8"), "do not overwrite")
        self.assertFalse((self.copy / "upgrade-marker.txt").exists())

    def test_upgrade_refuses_when_overview_or_enumeration_unsafe_and_recovers_on_swap_failure(self):
        import contextlib, io, unittest.mock
        self.assertEqual(self.run_repo_ae("install").returncode, 0)
        self.bump_repo_version("9.9.9")
        old_version = json.loads((self.copy / RECORD_NAME).read_text(encoding="utf-8"))["version"]

        sys.path.insert(0, str(self.root / "unused"))
        import ae
        self.addCleanup(ae.set_locale, ae.current_locale())

        with unittest.mock.patch.dict(os.environ, self.env), \
                unittest.mock.patch.object(ae, "__file__", str(self.repo / "ae.py")), \
                unittest.mock.patch.object(ae, "list_processes", side_effect=ae.ProcessEnumerationError("denied")), \
                contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()) as err:
            code = ae.main(["upgrade", "--locale", "en"])
        self.assertEqual(code, 1)
        self.assertIn("denied", err.getvalue())
        self.assertFalse((self.copy / "upgrade-marker.txt").exists())

        # 模拟移入新版时失败：自动回滚保留旧副本，不报告半更新为完成
        real_replace = ae.os.replace
        calls = 0

        def fail_second_replace(src, dst):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise PermissionError("file in use")
            return real_replace(src, dst)

        with unittest.mock.patch.dict(os.environ, self.env), \
                unittest.mock.patch.object(ae, "__file__", str(self.repo / "ae.py")), \
                unittest.mock.patch.object(ae, "list_processes", return_value=[]), \
                unittest.mock.patch.object(ae.os, "replace", side_effect=fail_second_replace), \
                contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()) as err:
            code = ae.main(["upgrade", "--locale", "en"])
        self.assertEqual(code, 1)
        self.assertFalse((self.copy / "upgrade-marker.txt").exists(), "失败回滚后不得留半更新版本")
        self.assertEqual(json.loads((self.copy / RECORD_NAME).read_text(encoding="utf-8"))["version"], old_version)

        # 模拟运行中审阅服务/总览首页拒绝升级（不碰旧副本）
        self.ledger.mkdir(parents=True, exist_ok=True)
        (self.ledger / ".overview-home-status.json").write_text(
            json.dumps({"app": "annotation-everything", "pid": os.getpid()}), encoding="utf-8")
        with unittest.mock.patch.dict(os.environ, self.env), \
                unittest.mock.patch.object(ae, "__file__", str(self.repo / "ae.py")), \
                unittest.mock.patch.object(ae, "list_processes", return_value=[]), \
                contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()) as err:
            code = ae.main(["upgrade", "--locale", "en"])
        self.assertEqual(code, 1)
        self.assertIn("overview-home", err.getvalue())
        self.assertFalse((self.copy / "upgrade-marker.txt").exists())
        (self.ledger / ".overview-home-status.json").unlink()

        # 模拟旧副本被占用导致第一步移动失败：旧副本原样保留，不留下半更新
        with unittest.mock.patch.dict(os.environ, self.env), \
                unittest.mock.patch.object(ae, "__file__", str(self.repo / "ae.py")), \
                unittest.mock.patch.object(ae, "list_processes", return_value=[]), \
                unittest.mock.patch.object(ae.os, "replace", side_effect=PermissionError("locked by explorer")), \
                contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()) as err:
            code = ae.main(["upgrade", "--locale", "en"])
        self.assertEqual(code, 1)
        self.assertIn("locked", err.getvalue().lower())
        self.assertFalse((self.copy / "upgrade-marker.txt").exists())
        self.assertEqual(json.loads((self.copy / RECORD_NAME).read_text(encoding="utf-8"))["version"], old_version)

    def test_upgrade_rolls_back_when_post_install_verification_fails(self):
        import contextlib, io, unittest.mock
        self.assertEqual(self.run_repo_ae("install").returncode, 0)
        self.bump_repo_version("9.9.9")
        old_version = json.loads((self.copy / RECORD_NAME).read_text(encoding="utf-8"))["version"]

        import ae
        self.addCleanup(ae.set_locale, ae.current_locale())
        real_write_record = ae.write_record

        def corrupt_write_record(dst, repo, links=None):
            path = real_write_record(dst, repo, links)
            if ".ae-upgrade-stage-" in str(dst):
                (dst / "SKILL.md").unlink()
            return path

        with unittest.mock.patch.dict(os.environ, self.env), \
                unittest.mock.patch.object(ae, "__file__", str(self.repo / "ae.py")), \
                unittest.mock.patch.object(ae, "list_processes", return_value=[]), \
                unittest.mock.patch.object(ae, "write_record", side_effect=corrupt_write_record), \
                contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            code = ae.main(["upgrade", "--locale", "en"])
        self.assertNotEqual(code, 0)
        self.assertTrue((self.copy / "SKILL.md").is_file(), "后置校验失败后必须回滚旧副本")
        self.assertFalse((self.copy / "upgrade-marker.txt").exists(), "不得留下失败的新版文件")
        self.assertEqual(json.loads((self.copy / RECORD_NAME).read_text(encoding="utf-8"))["version"], old_version)


if __name__ == "__main__":
    unittest.main()
