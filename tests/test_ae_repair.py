"""ae.py repair 的测试：修复计划、安全项自动修复、显式破坏性参数与 --dry-run（均为合成数据）。

安全底线：真正杀进程的路径只在函数级用注入的 process_lister 验证，列表里只有测试自建的
serve.py 进程；CLI 子进程级绝不带 --stop-orphans，只测 --dry-run、不带参数的计划输出与
--forget-stale（不碰进程）。家目录、各 Agent 技能目录、审阅台账（AE_HOME）全部指向临时目录。
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
AE = ROOT / "ae.py"
SKILL_SRC = ROOT / "skill" / "annotation-everything"
SETUP_DEV = ROOT / "tools" / "setup-dev.sh"

sys.path.insert(0, str(ROOT))

import ae  # noqa: E402
from test_ae_checks_runtime import ServeProcess, make_review_dir, write_ledger  # noqa: E402

PASS, WARN, FAIL = ae.LEVEL_PASS, ae.LEVEL_WARN, ae.LEVEL_FAIL
RID_STALE, RID_VALID = "1111111111111111", "2222222222222222"
RID_SERVER = "abcdef0123456789"


def make_repo(root: Path, version: str = "9.9.9") -> Path:
    """带最小技能本体的合成仓库（serve.py 只要有 VERSION 常量）。"""
    skill = root / "skill" / "annotation-everything"
    (skill / "scripts").mkdir(parents=True)
    (skill / "SKILL.md").write_text("---\nname: annotation-everything\n---\n合成技能\n", encoding="utf-8")
    (skill / "scripts" / "serve.py").write_text(f'VERSION = "{version}"\n', encoding="utf-8")
    return root


def snapshot_all(root: Path) -> dict:
    """目录全量快照（含安装记录）：--dry-run 不得改动任何状态。"""
    entries = {}
    for path in sorted(root.rglob("*")):
        if path.is_dir():
            continue
        rel = path.relative_to(root).as_posix()
        if "__pycache__" in path.parts or rel.endswith((".pyc", ".pyo", ".DS_Store")):
            continue
        entries[rel] = hashlib.sha256(path.read_bytes()).hexdigest()
    return entries


class PlanFromResultsTests(unittest.TestCase):
    """修复计划由体检结果生成：能修的进计划，破坏性项带所需参数。"""

    @staticmethod
    def result(code, level, category=ae.CATEGORY_INSTALL):
        return ae.CheckResult(code, category, level, "说明")

    def test_copy_problems_collapse_into_one_resync(self):
        results = [self.result(c, FAIL) for c in ("skill-copy-present", "skill-copy-sync", "skill-copy-version")]
        plan = ae.plan_from_results(results)
        self.assertEqual([item.kind for item in plan], ["resync-copy"])
        self.assertEqual(plan[0].flag, "")

    def test_each_repairable_problem_becomes_plan_item(self):
        results = [
            self.result("python-version", PASS),
            self.result("skill-copy-present", PASS),
            self.result("skill-copy-sync", PASS),
            self.result("skill-copy-version", PASS),
            self.result("skill-links", FAIL),
            self.result("ledger-stale", WARN, ae.CATEGORY_RUNTIME),
            self.result("orphan-servers", FAIL, ae.CATEGORY_RUNTIME),
            self.result("git-hooks", WARN, ae.CATEGORY_DEV),
        ]
        plan = ae.plan_from_results(results)
        self.assertEqual([item.kind for item in plan],
                         ["fix-links", "git-hooks", "forget-stale", "stop-orphans"])
        flags = {item.kind: item.flag for item in plan}
        self.assertEqual(flags["forget-stale"], "--forget-stale")
        self.assertEqual(flags["stop-orphans"], "--stop-orphans")
        self.assertEqual(flags["fix-links"], "")
        self.assertEqual(flags["git-hooks"], "")

    def test_unrepairable_problems_stay_out_of_plan(self):
        results = [
            self.result("python-version", FAIL),
            self.result("ledger-readable", FAIL, ae.CATEGORY_RUNTIME),
            self.result("port-pool", FAIL, ae.CATEGORY_RUNTIME),
            self.result("fc-list", WARN, ae.CATEGORY_TOOLS),
            self.result("e2e-chrome", WARN, ae.CATEGORY_DEV),
        ]
        self.assertEqual(ae.plan_from_results(results), [])

    def test_all_pass_makes_empty_plan(self):
        self.assertEqual(ae.plan_from_results([self.result("skill-copy-sync", PASS)]), [])


class ExecutePlanTests(unittest.TestCase):
    """execute_plan 的函数级行为：安全项自动执行；破坏性项没有参数就不动手。"""

    KEYS = ("HOME", "CLAUDE_SKILLS_DIR", "CODEX_HOME", "AE_HOME")

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="ae-repair-test-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.home = self.root / "home"
        self.home.mkdir()
        self.skills = self.home / ".claude" / "skills"
        self.ledger_home = self.root / "ledger"
        self.repo = make_repo(self.root / "repo")
        self.copy = self.skills / "annotation-everything"
        self._saved = {key: os.environ.get(key) for key in self.KEYS}
        os.environ.update({"HOME": str(self.home), "CLAUDE_SKILLS_DIR": str(self.skills),
                           "CODEX_HOME": str(self.home / ".codex"), "AE_HOME": str(self.ledger_home)})

    def tearDown(self):
        for key, value in self._saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    def ctx(self, record=None, **overrides):
        values = {"skill_copy": self.copy, "repo": self.repo, "record": record}
        values.update(overrides)
        return ae.DoctorContext(**values)

    def install_state(self, links=None) -> dict:
        """构造安装后的状态：副本 + 安装记录；返回读回的记录。"""
        ae.copy_skill(self.repo / "skill" / "annotation-everything", self.copy)
        ae.write_record(self.copy, self.repo, links or [])
        return ae.read_record(self.copy)

    def make_link(self, agent: str, target=None) -> dict:
        """在 Agent 技能目录建软链接，返回与安装记录同构的条目。"""
        link = ae.link_path(agent)
        link.parent.mkdir(parents=True, exist_ok=True)
        os.symlink(str(target if target is not None else self.copy.resolve()), str(link))
        return {"agent": agent, "path": str(link)}

    # ── 安全项：重新同步技能副本 ────────────────────────────────

    def test_resync_overwrites_manual_edits_and_refreshes_record(self):
        entry = self.make_link("codex")
        record = self.install_state([entry])
        (self.copy / "手工改动.txt").write_text("多余文件", encoding="utf-8")
        (self.copy / "SKILL.md").write_text("手工覆盖", encoding="utf-8")
        (self.repo / "skill" / "annotation-everything" / "scripts" / "serve.py").write_text(
            'VERSION = "9.9.10"\n', encoding="utf-8")           # 仓库升级
        lines, acted = ae.execute_plan(
            [ae.PlanItem("resync-copy", "不一致", "重新同步技能副本")], self.ctx(record))
        self.assertTrue(acted)
        src = self.repo / "skill" / "annotation-everything"
        self.assertEqual(ae._digests(src), ae._digests(self.copy), "副本与来源仓库重新一致")
        new_record = ae.read_record(self.copy)
        self.assertEqual(new_record["version"], "9.9.10")
        self.assertEqual([e["path"] for e in new_record["links"]], [entry["path"]],
                         "重装保留安装记录里仍有效的链接")

    def test_resync_recreates_copy_and_self_heals_dangling_links(self):
        entry = self.make_link("codex")
        record = self.install_state([entry])
        shutil.rmtree(self.copy)                                  # 副本与安装记录一起消失，链接悬空
        plan = [ae.PlanItem("resync-copy", "副本缺失", "重新同步技能副本"),
                ae.PlanItem("fix-links", "链接悬空", "修复链接")]
        lines, acted = ae.execute_plan(plan, self.ctx(record))
        self.assertTrue(acted)
        self.assertTrue((self.copy / "SKILL.md").is_file())
        link = Path(entry["path"])
        self.assertTrue(link.is_symlink() and link.exists(), "悬空链接随副本重建自愈")
        self.assertEqual([e["path"] for e in ae.read_record(self.copy)["links"]], [entry["path"]])

    def test_resync_reports_when_source_repo_missing(self):
        record = self.install_state()
        ctx = self.ctx(record, repo=self.root / "not-a-repo")
        lines, acted = ae.execute_plan(
            [ae.PlanItem("resync-copy", "副本缺失", "重新同步技能副本")], ctx)
        self.assertTrue(acted)
        self.assertTrue(any("未重新同步" in line for line in lines), lines)

    # ── 安全项：修复悬空链接 ───────────────────────────────────

    def test_fix_links_recreates_broken_links_but_not_occupied(self):
        elsewhere = self.root / "elsewhere"
        elsewhere.mkdir()
        codex = self.make_link("codex", target=elsewhere)          # 指向别处
        agents = {"agent": "agents", "path": str(ae.link_path("agents"))}   # 已删除
        zcode = {"agent": "zcode", "path": str(ae.link_path("zcode"))}      # 位置被非链接占用
        zcode_dir = Path(zcode["path"])
        zcode_dir.parent.mkdir(parents=True)
        zcode_dir.mkdir()
        (zcode_dir / "keep.txt").write_text("已有内容", encoding="utf-8")
        record = self.install_state([codex, agents, zcode])
        record_before = (self.copy / ae.RECORD_NAME).read_bytes()
        lines, acted = ae.execute_plan(
            [ae.PlanItem("fix-links", "链接异常", "修复指向技能副本的链接")], self.ctx(record))
        self.assertTrue(acted)
        copy_real = str(self.copy.resolve())
        self.assertEqual(os.path.realpath(codex["path"]), copy_real, "指向别处的链接重建为指向技能副本")
        self.assertEqual(os.path.realpath(agents["path"]), copy_real, "已删除的链接原位重建")
        self.assertTrue((zcode_dir / "keep.txt").is_file(), "被非链接内容占用的位置不动手")
        self.assertTrue(any("zcode" in line and "无法修复" in line for line in lines), lines)
        self.assertEqual((self.copy / ae.RECORD_NAME).read_bytes(), record_before,
                         "未重装时不改写安装记录")

    # ── 安全项：启用 git 钩子 ──────────────────────────────────

    def git_repo(self) -> Path:
        subprocess.run(["git", "init", "-q", str(self.repo)], check=True,
                       capture_output=True, timeout=30)
        hooks = self.repo / "tools" / "git-hooks"
        hooks.mkdir(parents=True)
        (hooks / "pre-commit").write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        shutil.copy2(SETUP_DEV, self.repo / "tools" / "setup-dev.sh")
        return self.repo

    def test_git_hooks_enabled_by_running_setup_script(self):
        self.git_repo()
        ctx = self.ctx()
        self.assertEqual(ae.check_git_hooks(ctx).level, WARN)      # 前置：未启用
        lines, acted = ae.execute_plan(
            [ae.PlanItem("git-hooks", "未启用", "启用 git 钩子")], ctx)
        self.assertTrue(acted)
        self.assertEqual(ae.check_git_hooks(ctx).level, PASS)

    def test_git_hooks_reports_missing_setup_script(self):
        self.git_repo()
        (self.repo / "tools" / "setup-dev.sh").unlink()
        lines, acted = ae.execute_plan(
            [ae.PlanItem("git-hooks", "未启用", "启用 git 钩子")], self.ctx())
        self.assertTrue(acted)
        self.assertTrue(any("未启用" in line for line in lines), lines)
        self.assertEqual(ae.check_git_hooks(self.ctx()).level, WARN)

    # ── 破坏性项：--forget-stale ───────────────────────────────

    def stale_ledger(self) -> Path:
        valid_dir = make_review_dir(self.root, RID_VALID)
        write_ledger(self.ledger_home, {
            RID_STALE: {"dir": str(self.root / "gone"), "port": 8800},
            RID_VALID: {"dir": str(valid_dir), "port": 8801}})
        return valid_dir

    def test_forget_stale_without_flag_changes_nothing(self):
        self.stale_ledger()
        plan = [ae.PlanItem("forget-stale", "有失效记录", "删除失效记录", "--forget-stale")]
        lines, acted = ae.execute_plan(plan, self.ctx(ledger_home=self.ledger_home))
        self.assertFalse(acted)
        entries = ae.skill_ledger_module().entries()
        self.assertIn(RID_STALE, entries, "不带参数绝不删除台账记录")

    def test_forget_stale_with_flag_removes_stale_and_frees_port(self):
        self.stale_ledger()
        plan = [ae.PlanItem("forget-stale", "有失效记录", "删除失效记录", "--forget-stale")]
        lines, acted = ae.execute_plan(plan, self.ctx(ledger_home=self.ledger_home),
                                       forget_stale=True)
        self.assertTrue(acted)
        mod = ae.skill_ledger_module()
        entries = mod.entries()
        self.assertNotIn(RID_STALE, entries)
        self.assertIn(RID_VALID, entries)
        self.assertEqual(entries[RID_VALID]["port"], 8801)
        self.assertNotIn(8800, mod.owners(), "被删记录的专属端口随之释放")
        self.assertTrue(any(RID_STALE[:8] in line for line in lines), lines)

    # ── 破坏性项：--stop-orphans（只对注入列表里的自建进程动手） ──

    def orphan_state(self):
        review_dir = make_review_dir(self.root, RID_SERVER)
        server = ServeProcess(review_dir, self.ledger_home)       # 启动时登记进临时台账
        self.addCleanup(server.stop)
        write_ledger(self.ledger_home, {})                        # 模拟已 forget：进程成残留服务
        ctx = self.ctx(ledger_home=self.ledger_home,
                       process_lister=lambda: [(server.proc.pid, server.cmdline)])
        return server, ctx

    def test_stop_orphans_without_flag_leaves_process_running(self):
        server, ctx = self.orphan_state()
        plan = [ae.PlanItem("stop-orphans", "有残留服务", "结束残留服务", "--stop-orphans")]
        lines, acted = ae.execute_plan(plan, ctx)
        self.assertFalse(acted)
        self.assertIsNone(server.proc.poll(), "不带参数绝不结束进程")

    def test_stop_orphans_with_flag_gracefully_terminates(self):
        server, ctx = self.orphan_state()
        plan = [ae.PlanItem("stop-orphans", "有残留服务", "结束残留服务", "--stop-orphans")]
        lines, acted = ae.execute_plan(plan, ctx, stop_orphans=True)
        self.assertTrue(acted)
        self.assertIsNotNone(server.proc.poll(), "SIGTERM 后进程已退出")
        with self.assertRaises(OSError):
            socket.create_connection(("127.0.0.1", server.port), timeout=1)   # 端口已释放
        self.assertTrue(any(str(server.proc.pid) in line for line in lines), lines)


class RepairCliTests(unittest.TestCase):
    """命令行行为：子进程运行 repair，环境全部指向临时目录；绝不带 --stop-orphans。
    退出码受本机真实运行状态影响（可能有真实残留服务），只断言被修复的具体检查项。"""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="ae-repair-cli-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.home = self.root / "home"
        self.home.mkdir()
        self.skills = self.home / ".claude" / "skills"
        self.env = {**os.environ, "HOME": str(self.home), "CLAUDE_SKILLS_DIR": str(self.skills),
                    "CODEX_HOME": str(self.home / ".codex"), "AE_HOME": str(self.root / "ledger"),
                    "PYTHONDONTWRITEBYTECODE": "1"}

    def run_ae(self, *args):
        return subprocess.run([sys.executable, str(AE), *args],
                              env=self.env, capture_output=True, text=True, timeout=180)

    @property
    def copy_dir(self) -> Path:
        return self.skills / "annotation-everything"

    @property
    def codex_link(self) -> Path:
        return self.home / ".codex" / "skills" / "annotation-everything"

    def install(self, *link_agents):
        args = ("install",) + (("--link", ",".join(link_agents)) if link_agents else ())
        p = self.run_ae(*args)
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)

    def doctor_levels(self) -> dict:
        p = self.run_ae("doctor", "--json")
        return {r["code"]: r["level"] for r in json.loads(p.stdout)["results"]}

    def break_install_state(self):
        """构造三处可自动修复的问题：副本被改、版本号落后、链接指向别处。"""
        self.install("codex")
        (self.copy_dir / "手工改动.txt").write_text("多余文件", encoding="utf-8")
        record_path = self.copy_dir / ae.RECORD_NAME
        record = json.loads(record_path.read_text(encoding="utf-8"))
        record["version"] = "0.0.1"
        record_path.write_text(json.dumps(record, ensure_ascii=False), encoding="utf-8")
        elsewhere = self.root / "elsewhere"
        elsewhere.mkdir()
        os.unlink(self.codex_link)
        os.symlink(str(elsewhere), str(self.codex_link))

    def test_dry_run_prints_plan_and_changes_nothing(self):
        self.break_install_state()
        before = snapshot_all(self.copy_dir)
        link_before = os.readlink(self.codex_link)
        p = self.run_ae("repair", "--dry-run")
        self.assertEqual(p.returncode, 1, p.stdout + p.stderr)   # 安装状态的失败项是确定性的
        self.assertIn("修复计划", p.stdout)
        self.assertIn("重新同步", p.stdout)
        self.assertNotIn("执行修复", p.stdout, "--dry-run 只打印计划")
        self.assertNotIn("修复后体检", p.stdout)
        self.assertEqual(snapshot_all(self.copy_dir), before, "--dry-run 不改动技能副本")
        self.assertEqual(os.readlink(self.codex_link), link_before, "--dry-run 不改动链接")
        levels = self.doctor_levels()
        self.assertEqual(levels["skill-copy-sync"], "失败")
        self.assertEqual(levels["skill-links"], "失败")

    def test_repair_resyncs_copy_and_fixes_links(self):
        self.break_install_state()
        p = self.run_ae("repair")
        self.assertIn(p.returncode, (0, 1))          # 运行状态取决于本机真实进程
        self.assertIn("修复计划", p.stdout)
        self.assertIn("执行修复", p.stdout)
        self.assertIn("修复后体检", p.stdout)
        self.assertLess(p.stdout.index("修复计划"), p.stdout.index("执行修复"), "先计划后执行")
        self.assertLess(p.stdout.index("执行修复"), p.stdout.index("修复后体检"))
        levels = self.doctor_levels()
        for code in ("skill-copy-present", "skill-copy-sync", "skill-copy-version", "skill-links"):
            self.assertEqual(levels[code], "通过", f"{code} 应被修复")
        self.assertEqual(os.path.realpath(str(self.codex_link)), str(self.copy_dir.resolve()))

    def test_repair_enables_git_hooks(self):
        repo = self.root / "synthetic-repo"
        shutil.copytree(SKILL_SRC, repo / "skill" / "annotation-everything")
        hooks = repo / "tools" / "git-hooks"
        hooks.mkdir(parents=True)
        (hooks / "pre-commit").write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        shutil.copy2(SETUP_DEV, repo / "tools" / "setup-dev.sh")
        subprocess.run(["git", "init", "-q", str(repo)], check=True, capture_output=True, timeout=30)
        self.install()
        record_path = self.copy_dir / ae.RECORD_NAME
        record = json.loads(record_path.read_text(encoding="utf-8"))
        record["repo"] = str(repo)
        record_path.write_text(json.dumps(record, ensure_ascii=False), encoding="utf-8")
        self.assertEqual(self.doctor_levels()["git-hooks"], "警告")
        p = self.run_ae("repair")
        self.assertIn(p.returncode, (0, 1))
        self.assertEqual(self.doctor_levels()["git-hooks"], "通过", "git 钩子应被启用")

    def write_stale_ledger(self):
        valid_dir = make_review_dir(self.root, RID_VALID)
        write_ledger(self.root / "ledger", {
            RID_STALE: {"dir": str(self.root / "gone"), "port": 8800},
            RID_VALID: {"dir": str(valid_dir), "port": 8801}})

    def test_forget_stale_flag_removes_stale_entries(self):
        self.write_stale_ledger()
        self.install()
        p = self.run_ae("repair", "--forget-stale")
        self.assertIn(p.returncode, (0, 1))
        self.assertIn(RID_STALE[:8], p.stdout)
        entries = json.loads((self.root / "ledger" / "reviews.json").read_text(
            encoding="utf-8"))["reviews"]
        self.assertNotIn(RID_STALE, entries)
        self.assertIn(RID_VALID, entries)
        self.assertEqual(self.doctor_levels()["ledger-stale"], "通过")

    def test_without_flag_stale_entries_kept_with_hint(self):
        self.write_stale_ledger()
        self.install()
        p = self.run_ae("repair")
        self.assertIn(p.returncode, (0, 1))
        self.assertIn("--forget-stale", p.stdout, "计划应提示所需参数")
        entries = json.loads((self.root / "ledger" / "reviews.json").read_text(
            encoding="utf-8"))["reviews"]
        self.assertIn(RID_STALE, entries, "不带参数不删除台账记录")


if __name__ == "__main__":
    unittest.main()
