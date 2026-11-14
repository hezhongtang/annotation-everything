"""ae.py uninstall 的测试；均为合成数据与临时目录，绝不触碰真实家目录。

进程检测的隔离：CLI 用例在 PATH 最前面放一个假的 `ps`，全表查询（-ww -eo）输出测试
指定的进程表，其余查询（-p）转交真 ps——这样本机遗留的 serve.py 不会影响用例，
也不可能在测试里误杀真实进程；「拒绝卸载」用例则用真 ps 验证真实进程表可用。
进程枚举与 serve.py 识别复用运行状态体检（#20）的 list_processes / serve_script_index。
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import unittest


ROOT = Path(__file__).resolve().parents[1]
AE = ROOT / "ae.py"
SERVE = ROOT / "skill" / "annotation-everything" / "scripts" / "serve.py"
sys.path.insert(0, str(ROOT))

import host_isolation  # noqa: E402

# 直接跑 unittest 的旧入口与 verify.py 共用同一隔离协议：本模块启动真实 serve.py、含 real_ps 用例
host_isolation.require_for_module(__name__)

import ae  # noqa: E402

RECORD_NAME = ae.RECORD_NAME
SKILL = "annotation-everything"
AGENTS = ("codex", "agents", "zcode")
# serve.py 运行时写进审阅目录的文件：对照「审阅目录逐字节不变」时排除
RUNTIME_FILES = {".annotation-server.json", "server.log"}


def agent_link(home: Path, agent: str) -> Path:
    """Agent 技能目录下同名链接的位置；目录约定对齐 skill_sources.default_roots。"""
    if agent == "codex":
        return home / ".codex" / "skills" / SKILL
    if agent == "agents":
        return home / ".agents" / "skills" / SKILL
    return home / ".zcode" / "skills" / SKILL


class UninstallCliTests(unittest.TestCase):
    """命令行行为：子进程运行 ae.py，HOME、各技能目录、台账目录全部指向临时目录。"""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="ae-uninstall-test-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.home = self.root / "home"
        self.home.mkdir()
        self.skills = self.home / ".claude" / "skills"
        self.ae_home = self.root / "ledger"
        # 假 ps：全表查询（-ww -eo）输出测试指定的进程表；其余转交真 ps
        real_ps = shutil.which("ps") or "/bin/ps"
        self.bin = self.root / "bin"
        self.bin.mkdir()
        self.ps_table = self.root / "ps-table.txt"
        self.ps_table.write_text("", encoding="utf-8")
        shim = self.bin / "ps"
        shim.write_text(
            f'#!/bin/sh\nif [ "$1 $2" = "-ww -eo" ]; then\n  cat "{self.ps_table}"\nelse\n  exec "{real_ps}" "$@"\nfi\n',
            encoding="utf-8")
        shim.chmod(0o755)
        self.env = {**os.environ, "HOME": str(self.home),
                    "CLAUDE_SKILLS_DIR": str(self.skills),
                    "CODEX_HOME": str(self.home / ".codex"),
                    "AE_HOME": str(self.ae_home),
                    "PATH": str(self.bin) + os.pathsep + os.environ.get("PATH", os.defpath),
                    "PYTHONDONTWRITEBYTECODE": "1"}

    def run_ae(self, *args, real_ps: bool = False):
        env = dict(self.env)
        if real_ps:  # 指定用本机真实的 ps 与进程表
            env["PATH"] = os.environ.get("PATH", os.defpath)
        return subprocess.run([sys.executable, str(AE), *args],
                              env=env, capture_output=True, text=True, timeout=60)

    @property
    def copy_dir(self) -> Path:
        return self.skills / SKILL

    def snapshot(self, directory: Path, ignore=frozenset()) -> dict:
        """目录内容的字节快照：{相对路径: sha256}，忽略运行时文件时可传 ignore。"""
        files = {}
        for path in sorted(directory.rglob("*")):
            if path.is_file() and path.name not in ignore:
                files[str(path.relative_to(directory))] = hashlib.sha256(path.read_bytes()).hexdigest()
        return files

    def write_ledger(self, *review_dirs: Path) -> Path:
        """在临时台账目录写合成的 reviews.json；返回台账文件路径。"""
        self.ae_home.mkdir(parents=True, exist_ok=True)
        reviews = {}
        for index, directory in enumerate(review_dirs):
            reviews[f"{index:016x}"] = {"dir": str(directory), "port": 8765 + index}
        path = self.ae_home / "reviews.json"
        path.write_text(json.dumps({"version": 1, "reviews": reviews}, ensure_ascii=False, indent=2) + "\n",
                        encoding="utf-8")
        return path

    def make_review_dir(self, name: str = "review") -> Path:
        directory = self.root / name
        directory.mkdir()
        (directory / "index.html").write_text("<!doctype html><title>合成对照页</title>", encoding="utf-8")
        (directory / "review.json").write_text(
            json.dumps({"reviewId": f"{int(name == 'review'):016x}", "name": "合成审阅"}, ensure_ascii=False),
            encoding="utf-8")
        return directory

    def free_port(self) -> int:
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            return sock.getsockname()[1]

    def spawn_server(self, directory: Path):
        """启动真实的 serve.py 子进程并等它写出状态文件；返回 (进程, 命令行文本)。"""
        argv = [sys.executable, str(SERVE), str(directory), "--port", str(self.free_port()), "--no-open"]
        proc = subprocess.Popen(argv, env=self.env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        status = directory / ".annotation-server.json"
        for _ in range(100):
            if status.exists():
                break
            exited = proc.poll()
            if exited is not None:
                out, err = proc.communicate(timeout=10)
                self.fail(f"服务进程提前退出（{exited}）：{err}{out}")
            time.sleep(0.1)
        self.assertTrue(status.exists(), "等不到状态文件，服务没起来")
        return proc, " ".join(argv)

    def stop_server(self, proc) -> None:
        """兜底清理：先 SIGTERM，超时再 SIGKILL，最后收尸。"""
        if proc.poll() is None:
            proc.terminate()
        try:
            proc.communicate(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.communicate(timeout=10)

    def spawn_home(self):
        """启动真实的总览首页子进程（无显式端口：走保留地址）并等它写出运行状态文件。"""
        argv = [sys.executable, str(SERVE), "overview", "--no-open"]
        proc = subprocess.Popen(argv, env=self.env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        status = self.ae_home / ".overview-home-status.json"
        for _ in range(100):
            if status.exists():
                break
            exited = proc.poll()
            if exited is not None:
                out, err = proc.communicate(timeout=10)
                self.fail(f"首页进程提前退出（{exited}）：{err}{out}")
            time.sleep(0.1)
        self.assertTrue(status.exists(), "等不到首页运行状态文件")
        return proc, " ".join(argv)

    def test_uninstall_removes_copy_and_links_keeps_ledger_and_review_dir(self):
        self.assertEqual(self.run_ae("install", "--link", "codex,agents,zcode").returncode, 0)
        review = self.make_review_dir()
        ledger = self.write_ledger(review)
        ledger_before = ledger.read_bytes()
        review_before = self.snapshot(review)
        p = self.run_ae("uninstall")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertFalse(self.copy_dir.exists(), "技能副本应删除")
        for agent in AGENTS:
            self.assertFalse(agent_link(self.home, agent).exists(), f"{agent} 的链接应删除")
        self.assertTrue(ledger.read_bytes() == ledger_before, "台账应逐字节保留")
        self.assertTrue(self.ae_home.is_dir(), "台账目录应保留")
        self.assertEqual(self.snapshot(review), review_before, "审阅目录应逐字节不变")

    def test_uninstall_purge_removes_ledger_home_but_not_review_dir(self):
        self.assertEqual(self.run_ae("install", "--link", "codex").returncode, 0)
        review = self.make_review_dir()
        self.write_ledger(review)
        review_before = self.snapshot(review)
        p = self.run_ae("uninstall", "--purge")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertFalse(self.ae_home.exists(), "--purge 应删除台账目录")
        self.assertFalse(self.copy_dir.exists())
        self.assertFalse(agent_link(self.home, "codex").exists())
        self.assertEqual(self.snapshot(review), review_before, "审阅目录应逐字节不变")

    def test_uninstall_purge_refuses_when_review_dir_inside_ledger_home(self):
        self.assertEqual(self.run_ae("install").returncode, 0)
        inside = self.ae_home / "review-inside"
        inside.mkdir(parents=True)
        (inside / "index.html").write_text("合成", encoding="utf-8")
        ledger = self.write_ledger(inside)
        inside_before = self.snapshot(inside)
        p = self.run_ae("uninstall", "--purge")
        self.assertEqual(p.returncode, 2, p.stdout + p.stderr)
        self.assertIn("审阅目录", p.stderr)
        self.assertTrue(ledger.exists(), "拒绝时台账应保留")
        self.assertTrue(self.copy_dir.exists(), "拒绝时不动技能副本")
        self.assertEqual(self.snapshot(inside), inside_before, "拒绝时审阅目录应逐字节不变")

    def test_uninstall_without_install_exits_zero_with_hint(self):
        p = self.run_ae("uninstall")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertIn("未安装", p.stdout)

    def test_uninstall_purge_without_install_still_purges_ledger(self):
        review = self.make_review_dir()
        self.write_ledger(review)
        p = self.run_ae("uninstall", "--purge")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertFalse(self.ae_home.exists())
        self.assertTrue((review / "index.html").is_file(), "审阅目录不动")

    def test_uninstall_removes_dangling_links_found_by_scan(self):
        self.assertEqual(self.run_ae("install", "--link", "codex").returncode, 0)
        shutil.rmtree(self.copy_dir)  # 副本与安装记录一起消失 → 只能靠扫描发现悬空链接
        p = self.run_ae("uninstall")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertFalse(agent_link(self.home, "codex").exists(), "扫描发现的悬空链接也应删除")

    def test_uninstall_keeps_unrelated_link_and_non_link_content(self):
        self.assertEqual(self.run_ae("install", "--link", "codex").returncode, 0)
        elsewhere = self.root / "elsewhere"
        elsewhere.mkdir()
        other_link = agent_link(self.home, "agents")
        other_link.parent.mkdir(parents=True)
        os.symlink(str(elsewhere), str(other_link))
        occupied = agent_link(self.home, "zcode")
        occupied.parent.mkdir(parents=True)
        occupied.mkdir()
        (occupied / "keep.txt").write_text("保留内容", encoding="utf-8")
        p = self.run_ae("uninstall")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertFalse(agent_link(self.home, "codex").exists())
        self.assertTrue(other_link.is_symlink() and other_link.exists(), "指向别处的链接不删")
        self.assertEqual(os.path.realpath(str(other_link)), os.path.realpath(str(elsewhere)))
        self.assertTrue((occupied / "keep.txt").is_file(), "非链接内容不删")

    def test_uninstall_refuses_while_server_running(self):
        self.assertEqual(self.run_ae("install", "--link", "codex").returncode, 0)
        review = self.make_review_dir()
        ledger = self.write_ledger(review)
        proc, _line = self.spawn_server(review)
        self.addCleanup(self.stop_server, proc)
        review_before = self.snapshot(review, ignore=RUNTIME_FILES)
        p = self.run_ae("uninstall", real_ps=True)
        self.assertEqual(p.returncode, 1, p.stdout + p.stderr)
        self.assertIn("--stop-servers", p.stdout + p.stderr, "要提示先停止或加 --stop-servers")
        self.assertTrue(self.copy_dir.exists(), "拒绝卸载时不动技能副本")
        self.assertTrue(agent_link(self.home, "codex").exists(), "拒绝卸载时不动链接")
        self.assertTrue(ledger.exists(), "拒绝卸载时不动台账")
        self.assertEqual(self.snapshot(review, ignore=RUNTIME_FILES), review_before)

    def test_uninstall_stop_servers_stops_then_uninstalls(self):
        self.assertEqual(self.run_ae("install", "--link", "codex").returncode, 0)
        review = self.make_review_dir()
        self.write_ledger(review)
        proc, line = self.spawn_server(review)
        self.addCleanup(self.stop_server, proc)
        review_before = self.snapshot(review, ignore=RUNTIME_FILES)
        self.ps_table.write_text(f"{proc.pid} {line}\n", encoding="utf-8")  # 只上报这一个服务
        p = self.run_ae("uninstall", "--stop-servers")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertIsNotNone(proc.poll(), "服务进程应已被结束")
        self.assertFalse((review / ".annotation-server.json").exists(), "优雅停止后状态文件应清理")
        self.assertFalse(self.copy_dir.exists(), "停服务后应删除技能副本")
        self.assertFalse(agent_link(self.home, "codex").exists())
        self.assertTrue((self.ae_home / "reviews.json").exists(), "默认保留台账")
        self.assertEqual(self.snapshot(review, ignore=RUNTIME_FILES), review_before, "审阅目录应逐字节不变")

    def test_uninstall_refuses_while_overview_home_running(self):
        self.assertEqual(self.run_ae("install", "--link", "codex").returncode, 0)
        proc, line = self.spawn_home()
        self.addCleanup(self.stop_server, proc)
        self.ps_table.write_text(f"{proc.pid} {line}\n", encoding="utf-8")
        p = self.run_ae("uninstall")
        self.assertEqual(p.returncode, 1, p.stdout + p.stderr)
        self.assertIn("--stop-servers", p.stdout + p.stderr, "运行中的首页沿用存在运行服务时的卸载规则")
        self.assertTrue(self.copy_dir.exists(), "拒绝卸载时不动技能副本")

    def test_uninstall_stop_servers_stops_overview_home(self):
        self.assertEqual(self.run_ae("install", "--link", "codex").returncode, 0)
        proc, line = self.spawn_home()
        self.addCleanup(self.stop_server, proc)
        self.ps_table.write_text(f"{proc.pid} {line}\n", encoding="utf-8")
        p = self.run_ae("uninstall", "--stop-servers")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertIsNotNone(proc.poll(), "核对首页进程身份后应结束它")
        self.assertFalse((self.ae_home / ".overview-home-status.json").exists(), "优雅退出应清理首页运行状态")
        self.assertFalse(self.copy_dir.exists(), "停服务后应删除技能副本")

    def test_uninstall_purge_cleans_overview_home_files(self):
        self.assertEqual(self.run_ae("install", "--link", "codex").returncode, 0)
        proc, _line = self.spawn_home()
        self.stop_server(proc)
        self.assertEqual(proc.returncode, 0, "首页应优雅退出并清理运行状态")
        for name in ("overview-home.json", ".overview-home.html", "overview-home.log", ".overview-home-start.lock"):
            self.assertTrue((self.ae_home / name).exists(), f"启动并退出首页后应留下已知文件 {name}")
        p = self.run_ae("uninstall", "--purge")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertFalse(self.ae_home.exists(), "首页生成的已知文件都应可清理，台账目录才删得掉")
        self.assertFalse(self.copy_dir.exists())


class RemoveLinksTests(unittest.TestCase):
    """remove_links 的取舍规则：只删指向技能副本的软链接。"""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="ae-remove-links-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.copy = self.root / "skills" / SKILL
        self.copy.mkdir(parents=True)
        (self.copy / "SKILL.md").write_text("合成技能", encoding="utf-8")
        self.links = self.root / "links"
        self.links.mkdir()

    def make_link(self, name: str, target) -> dict:
        link = self.links / name
        os.symlink(str(target), str(link))
        return {"agent": name, "path": str(link)}

    def actions(self, entries) -> dict:
        return {Path(path).name: action for path, action in ae.remove_links(entries, self.copy)}

    def test_removes_links_pointing_at_copy(self):
        good = self.make_link("good", self.copy)
        report = ae.remove_links([good], self.copy)
        self.assertEqual(report, [(good["path"], "已删除链接")])
        self.assertFalse(Path(good["path"]).exists())

    def test_removes_dangling_link_to_copy(self):
        entry = self.make_link("dangling", self.copy)
        shutil.rmtree(self.copy)  # 副本消失，链接悬空但目标路径仍是技能副本
        report = ae.remove_links([entry], self.copy)
        self.assertEqual(report, [(entry["path"], "已删除链接")])
        self.assertFalse(Path(entry["path"]).exists())

    def test_keeps_link_pointing_elsewhere_and_non_link(self):
        elsewhere = self.root / "elsewhere"
        elsewhere.mkdir()
        elsewhere_link = self.make_link("elsewhere", elsewhere)
        replaced = self.links / "replaced"
        replaced.mkdir()
        (replaced / "keep.txt").write_text("保留", encoding="utf-8")
        deleted = self.links / "deleted"  # 记录里有、磁盘上已没有
        report = ae.remove_links(
            [elsewhere_link, {"agent": "replaced", "path": str(replaced)},
             {"agent": "deleted", "path": str(deleted)}], self.copy)
        actions = {Path(path).name: action for path, action in report}
        self.assertEqual(actions["elsewhere"], "指向别处，未删除")
        self.assertEqual(actions["replaced"], "已不是链接，未删除")
        self.assertEqual(actions["deleted"], "已不是链接，未删除")
        self.assertTrue(Path(elsewhere_link["path"]).is_symlink(), "指向别处的链接保留")
        self.assertTrue((replaced / "keep.txt").is_file(), "非链接内容保留")


class RunningServersTests(unittest.TestCase):
    """running_servers 与 stop_servers 的单元行为：注入列举器，不依赖真实进程表。"""

    def test_running_servers_only_matches_own_serve_script(self):
        fake = [(1, f"python3 {SERVE} /r"),
                (2, "/usr/bin/python3 /b/serve.py --port 9"),
                (3, "python3 /c/other.py x"),
                (4, "grep serve.py notes.txt")]
        found = ae.running_servers(lister=lambda: fake)
        self.assertEqual([pid for pid, _command in found], [1],
                         "只识别本项目脚本，不匹配其他项目或 grep 参数")

    def test_running_servers_empty_when_no_processes(self):
        self.assertEqual(ae.running_servers(lister=lambda: []), [])

    def test_running_home_servers_matches_only_overview_subcommand(self):
        fake = [(1, f"python3 {SERVE} overview --no-open"),
                (2, f"python3 {SERVE} /r"),
                (3, f"python3 {SERVE} overview --locale=en"),
                (4, "grep overview notes.txt")]
        self.assertEqual([pid for pid, _command in ae.running_home_servers(lister=lambda: fake)], [1, 3])
        self.assertEqual([pid for pid, _command in ae.running_servers(lister=lambda: fake)], [2],
                         "overview 进程不算审阅服务：体检的审阅服务计数与孤儿修复跳过它")
        self.assertIsNone(ae.serve_dir_argument(fake[0][1], fake[0][0]))

    def test_server_identity_accepts_overview_home_process(self):
        line = f"python3 {SERVE} overview --no-open"
        identity = ae.server_identity(None, line)
        self.assertIsNotNone(identity, "卸载要能核对正在运行的首页进程身份")
        self.assertEqual(identity[0], str(SERVE.resolve()))

    def test_stop_servers_refuses_unrelated_real_process(self):
        proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
        self.addCleanup(self.stop, proc)
        stuck = ae.stop_servers([(proc.pid, "python3 -c import time")])
        self.assertEqual([pid for pid, _ in stuck], [proc.pid], "无关进程应拒绝停止")
        self.assertIsNone(proc.poll(), "无关进程应保持运行")

    def stop(self, proc) -> None:
        if proc.poll() is None:
            proc.terminate()
        try:
            proc.communicate(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.communicate(timeout=10)


class PurgeGuardTests(unittest.TestCase):
    """purge_guard 的安全检查：审阅目录落在台账目录里时拒绝 --purge。"""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="ae-purge-guard-")
        self.addCleanup(self.temp.cleanup)
        self.ledger_home = Path(self.temp.name) / "ledger"

    def write_ledger(self, *review_dirs: str):
        self.ledger_home.mkdir(parents=True, exist_ok=True)
        reviews = {f"{index:016x}": {"dir": directory, "port": 8765 + index}
                   for index, directory in enumerate(review_dirs)}
        (self.ledger_home / "reviews.json").write_text(
            json.dumps({"version": 1, "reviews": reviews}), encoding="utf-8")

    def test_accepts_missing_ledger_home(self):
        self.assertIsNone(ae.purge_guard(self.ledger_home))

    def test_accepts_review_dirs_outside_ledger_home(self):
        outside = Path(self.temp.name) / "review"
        outside.mkdir()
        self.write_ledger(str(outside))
        self.assertIsNone(ae.purge_guard(self.ledger_home))

    def test_rejects_when_review_dir_inside_ledger_home(self):
        inside = self.ledger_home / "review"
        self.write_ledger(str(inside))
        reason = ae.purge_guard(self.ledger_home)
        self.assertIsNotNone(reason)
        self.assertIn("审阅目录", reason)

    def test_rejects_unreadable_ledger(self):
        self.ledger_home.mkdir(parents=True)
        (self.ledger_home / "reviews.json").write_text("不是 JSON", encoding="utf-8")
        self.assertIsNotNone(ae.purge_guard(self.ledger_home))


if __name__ == "__main__":
    unittest.main()
