"""ae.py 运行状态检查项的测试（均为合成数据）。

用真实 serve.py 子进程与真实 socket 构造「端口被占」「残留服务」「台账失效记录」等状态；
进程枚举与端口探测经 DoctorContext 注入，只针对测试自建的进程与端口断言，
绝不杀真实进程、绝不读写真实台账（AE_HOME 只在子进程环境里指向临时目录）。
"""
from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import host_isolation  # noqa: E402

# 直接跑 unittest 的旧入口与 verify.py 共用同一隔离协议：本模块触及真实进程视图与端口
host_isolation.require_for_module(__name__)

import ae  # noqa: E402

PASS, WARN, FAIL = ae.LEVEL_PASS, ae.LEVEL_WARN, ae.LEVEL_FAIL
SERVE_PY = ROOT / "skill" / "annotation-everything" / "scripts" / "serve.py"
POOL_FIRST, POOL_LAST = 8765, 9264            # ledger.BASE_PORT 起 MAX_SCAN 个专属端口
FIX_FORGET_STALE = "ae.py repair --forget-stale"
FIX_STOP_ORPHANS = "ae.py repair --stop-orphans"


def make_review_dir(root: Path, rid: str) -> Path:
    """最小可服务的审阅目录（合成）：serve.py 启动只需要 index.html 与 review.json 里的审阅标识。"""
    d = root / f"review-{rid[:8]}"
    d.mkdir(parents=True)
    (d / "index.html").write_text("<!doctype html><title>合成对照页</title>", encoding="utf-8")
    (d / "review.json").write_text(json.dumps({"reviewId": rid, "name": "合成审阅"}), encoding="utf-8")
    return d


def write_ledger(home: Path, reviews: dict) -> None:
    home.mkdir(parents=True, exist_ok=True)
    (home / "reviews.json").write_text(
        json.dumps({"version": 1, "reviews": reviews}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def free_pool_port() -> int:
    """专属端口池里找一个当下确实空闲的端口（避开本机真实服务与占用者）。"""
    for port in range(POOL_FIRST, POOL_LAST + 1):
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                pass
        except OSError:
            return port
    raise unittest.SkipTest("专属端口池 8765–9264 当前全被占用")


def runtime_ctx(root: Path, **overrides):
    """运行状态检查用的 DoctorContext：台账、技能副本全部指向临时目录。"""
    values = {"skill_copy": root / "skills" / "annotation-everything", "repo": root, "record": None,
              "ledger_home": root / "ledger"}
    values.update(overrides)
    return ae.DoctorContext(**values)


class ForeignListener:
    """占住一个专属端口的「别的进程」：真实 socket，对 HTTP 请求回一段非本工具的 health。"""

    BODY = b'{"app":"not-annotation-everything"}'

    def __init__(self, port: int):
        self.port = port
        self.sock = socket.socket()
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind(("127.0.0.1", port))
        self.sock.listen(5)
        self.sock.settimeout(0.2)
        self._stopped = threading.Event()
        self.thread = threading.Thread(target=self._serve, daemon=True)
        self.thread.start()

    def _serve(self) -> None:
        while not self._stopped.is_set():
            try:
                conn, _ = self.sock.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            try:
                conn.recv(65536)
                conn.sendall(b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nConnection: close\r\n"
                             b"Content-Length: " + str(len(self.BODY)).encode() + b"\r\n\r\n" + self.BODY)
            except OSError:
                pass
            finally:
                try:
                    conn.close()
                except OSError:
                    pass

    def close(self) -> None:
        self._stopped.set()
        self.thread.join(timeout=5)
        self.sock.close()


class ServeProcess:
    """真实 serve.py 子进程：状态文件写出即启动完成；stop() 优雅停止并确认端口已释放。"""

    def __init__(self, review_dir: Path, ledger_home: Path):
        self.review_dir = review_dir
        self.port = free_pool_port()
        self.cmdline = f"{sys.executable} {SERVE_PY} {review_dir} --port {self.port} --no-open"
        self.proc = subprocess.Popen(
            [sys.executable, str(SERVE_PY), str(review_dir), "--port", str(self.port), "--no-open"],
            env={**os.environ, "AE_HOME": str(ledger_home), "PYTHONDONTWRITEBYTECODE": "1"},
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            self._wait_ready()
        except BaseException:
            self.stop()
            raise

    def _wait_ready(self) -> None:
        status_file = self.review_dir / ".annotation-server.json"
        deadline = time.time() + 20
        while time.time() < deadline:
            if status_file.is_file():
                try:
                    info = json.loads(status_file.read_text(encoding="utf-8"))
                except ValueError:
                    info = {}
                if info.get("pid") == self.proc.pid and info.get("port") == self.port:
                    return
            if self.proc.poll() is not None:
                out, err = self.proc.communicate(timeout=10)
                raise AssertionError(f"serve.py 启动即退出：{out}{err}")
            time.sleep(0.1)
        raise AssertionError("serve.py 未在期限内写出状态文件")

    def stop(self) -> None:
        if self.proc.poll() is None:
            self.proc.terminate()
        try:
            self.proc.communicate(timeout=10)
        except subprocess.TimeoutExpired:
            self.proc.kill()
            self.proc.communicate(timeout=10)
        deadline = time.time() + 10
        while time.time() < deadline:
            try:
                with socket.create_connection(("127.0.0.1", self.port), timeout=0.2):
                    pass
            except OSError:
                return                                  # 连不上 = 端口已释放
            time.sleep(0.1)
        raise AssertionError(f"serve.py 停止后端口 {self.port} 仍未释放")


class TempRootTestCase(unittest.TestCase):

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="ae-runtime-test-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.ledger_home = self.root / "ledger"
        self.rid = "a1b2c3d4e5f60718"

    def ctx(self, **overrides):
        return runtime_ctx(self.root, ledger_home=self.ledger_home, **overrides)

    def start_server(self, review_dir: Path) -> ServeProcess:
        server = ServeProcess(review_dir, self.ledger_home)
        self.addCleanup(server.stop)
        return server


class LedgerReadableTests(TempRootTestCase):

    def test_fails_when_ledger_file_is_corrupt(self):
        self.ledger_home.mkdir(parents=True)
        (self.ledger_home / "reviews.json").write_text("{不是 JSON", encoding="utf-8")
        result = ae.check_ledger_readable(self.ctx())
        self.assertEqual(result.level, FAIL)
        self.assertEqual(result.code, "ledger-readable")
        self.assertEqual(result.category, "运行状态")
        self.assertTrue(result.fix_command.strip())

    def test_fails_when_ledger_structure_is_wrong(self):
        self.ledger_home.mkdir(parents=True)
        (self.ledger_home / "reviews.json").write_text('{"reviews": []}', encoding="utf-8")
        self.assertEqual(ae.check_ledger_readable(self.ctx()).level, FAIL)

    def test_passes_when_ledger_absent(self):
        self.assertEqual(ae.check_ledger_readable(self.ctx()).level, PASS)

    def test_passes_when_ledger_readable(self):
        write_ledger(self.ledger_home, {self.rid: {"dir": str(self.root / "gone"), "port": 8800}})
        result = ae.check_ledger_readable(self.ctx())
        self.assertEqual(result.level, PASS)
        self.assertIn("1", result.message)


class LedgerStaleTests(TempRootTestCase):

    def test_warns_when_review_dir_is_gone(self):
        write_ledger(self.ledger_home, {self.rid: {"dir": str(self.root / "gone"), "port": 8800}})
        result = ae.check_ledger_stale(self.ctx())
        self.assertEqual(result.level, WARN)
        self.assertEqual(result.code, "ledger-stale")
        self.assertIn(self.rid[:8], result.message)          # 审阅标识前缀
        self.assertIn("8800", result.message)                # 占用的专属端口
        self.assertTrue(result.fix_command.rstrip().endswith(FIX_FORGET_STALE), result.fix_command)

    def test_warns_when_review_id_mismatches(self):
        d = make_review_dir(self.root, "ffffffffffffffff")
        write_ledger(self.ledger_home, {self.rid: {"dir": str(d), "port": 8801}})
        result = ae.check_ledger_stale(self.ctx())
        self.assertEqual(result.level, WARN)
        self.assertIn("8801", result.message)
        self.assertTrue(result.fix_command.rstrip().endswith(FIX_FORGET_STALE), result.fix_command)

    def test_passes_when_entry_matches_dir(self):
        d = make_review_dir(self.root, self.rid)
        write_ledger(self.ledger_home, {self.rid: {"dir": str(d), "port": 8802}})
        self.assertEqual(ae.check_ledger_stale(self.ctx()).level, PASS)

    def test_passes_when_ledger_absent_or_unreadable(self):
        self.assertEqual(ae.check_ledger_stale(self.ctx()).level, PASS)
        self.ledger_home.mkdir(parents=True)
        (self.ledger_home / "reviews.json").write_text("坏", encoding="utf-8")
        self.assertEqual(ae.check_ledger_stale(self.ctx()).level, PASS)


class PortPoolTests(TempRootTestCase):

    def test_passes_when_pool_free(self):
        result = ae.check_port_pool(self.ctx(port_probe=lambda port: None))
        self.assertEqual(result.level, PASS)
        self.assertEqual(result.code, "port-pool")
        self.assertEqual(result.category, "运行状态")

    def test_fails_when_foreign_process_holds_pool_port(self):
        port = free_pool_port()
        foreign = ForeignListener(port)
        self.addCleanup(foreign.close)
        write_ledger(self.ledger_home, {self.rid: {"dir": str(self.root / "review"), "port": port}})
        probe = lambda p: ae.probe_port(p) if p == port else None     # 只探测测试自建的端口
        result = ae.check_port_pool(self.ctx(port_probe=probe))
        self.assertEqual(result.level, FAIL)
        self.assertIn(str(port), result.message)
        self.assertIn(str(port), result.fix_command)
        self.assertIn("lsof", result.fix_command)

    def test_passes_when_pool_port_used_by_own_serve(self):
        d = make_review_dir(self.root, self.rid)
        server = self.start_server(d)
        probe = lambda p: ae.probe_port(p) if p == server.port else None
        result = ae.check_port_pool(self.ctx(port_probe=probe))
        self.assertEqual(result.level, PASS)

    def test_probe_port_identifies_own_server_with_host_header(self):
        d = make_review_dir(self.root, self.rid)
        server = self.start_server(d)
        found = ae.probe_port(server.port)
        self.assertIsNotNone(found)
        self.assertEqual(found.get("app"), "annotation-everything")   # 缺 Host 头会被 421 拒、误判为外部占用


class OrphanServerTests(TempRootTestCase):

    def test_list_processes_includes_current_process(self):
        procs = dict(ae.list_processes())
        self.assertIn(os.getpid(), procs)              # 真实 ps 枚举：本进程一定在列
        self.assertTrue(procs[os.getpid()].strip())    # 且能拿到非空命令行

    def test_fails_when_serving_dir_not_in_ledger(self):
        d = make_review_dir(self.root, self.rid)
        server = self.start_server(d)                 # 启动时会登记进临时台账
        write_ledger(self.ledger_home, {})            # 模拟这份审阅已从台账 forget
        result = ae.check_orphan_servers(self.ctx(process_lister=lambda: [(server.proc.pid, server.cmdline)]))
        self.assertEqual(result.level, FAIL)
        self.assertEqual(result.code, "orphan-servers")
        self.assertEqual(result.category, "运行状态")
        self.assertIn(str(server.proc.pid), result.message)
        self.assertIn(str(server.port), result.message)
        self.assertTrue(result.fix_command.rstrip().endswith(FIX_STOP_ORPHANS), result.fix_command)

    def test_fails_when_serving_dir_deleted(self):
        d = make_review_dir(self.root, self.rid)
        server = self.start_server(d)
        shutil.rmtree(d)
        result = ae.check_orphan_servers(self.ctx(process_lister=lambda: [(server.proc.pid, server.cmdline)]))
        self.assertEqual(result.level, FAIL)
        self.assertIn(str(server.proc.pid), result.message)
        self.assertIn(str(server.port), result.message)
        self.assertTrue(result.fix_command.rstrip().endswith(FIX_STOP_ORPHANS), result.fix_command)

    def test_passes_when_serving_ledgered_review(self):
        d = make_review_dir(self.root, self.rid)
        server = self.start_server(d)                 # 台账保留启动时的登记
        result = ae.check_orphan_servers(self.ctx(process_lister=lambda: [(server.proc.pid, server.cmdline)]))
        self.assertEqual(result.level, PASS)

    def test_fails_when_dir_exists_but_not_in_ledger(self):
        d = make_review_dir(self.root, self.rid)
        write_ledger(self.ledger_home, {})
        cmdline = f"{sys.executable} {SERVE_PY} {d} --port 8900 --no-open"
        result = ae.check_orphan_servers(self.ctx(process_lister=lambda: [(424242, cmdline)]))
        self.assertEqual(result.level, FAIL)
        self.assertIn("424242", result.message)

    def test_rid_argument_resolves_to_ledgered_dir(self):
        d = make_review_dir(self.root, self.rid)
        write_ledger(self.ledger_home, {self.rid: {"dir": str(d), "port": 8901}})
        cmdline = f"{sys.executable} {SERVE_PY} {self.rid} --port 8901 --no-open"
        result = ae.check_orphan_servers(self.ctx(process_lister=lambda: [(424243, cmdline)]))
        self.assertEqual(result.level, PASS)

    def test_fails_when_rid_argument_absent_from_ledger(self):
        cmdline = f"{sys.executable} {SERVE_PY} {self.rid} --no-open"
        result = ae.check_orphan_servers(self.ctx(process_lister=lambda: [(424244, cmdline)]))
        self.assertEqual(result.level, FAIL)
        self.assertIn("424244", result.message)

    def test_dir_argument_found_after_flags(self):
        d = make_review_dir(self.root, self.rid)
        write_ledger(self.ledger_home, {self.rid: {"dir": str(d), "port": 8902}})
        cmdline = f"{sys.executable} {SERVE_PY} --port 8902 --no-open {d}"
        result = ae.check_orphan_servers(self.ctx(process_lister=lambda: [(424245, cmdline)]))
        self.assertEqual(result.level, PASS)

    def test_ignores_processes_without_serve_py_script(self):
        lister = lambda: [(999998, "/usr/bin/tail -f /var/log/system.log"),
                          (999997, f"{sys.executable} {ROOT / 'tests' / 'test_core.py'}")]
        result = ae.check_orphan_servers(self.ctx(process_lister=lister))
        self.assertEqual(result.level, PASS)

    def test_ignores_serve_py_mention_without_dir_argument(self):
        result = ae.check_orphan_servers(self.ctx(process_lister=lambda: [(999996, "grep serve.py")]))
        self.assertEqual(result.level, PASS)

    def test_skips_when_ledger_unreadable(self):
        self.ledger_home.mkdir(parents=True)
        (self.ledger_home / "reviews.json").write_text("坏", encoding="utf-8")
        lister = lambda: [(424246, f"{sys.executable} {SERVE_PY} {self.root / 'x'} --no-open")]
        result = ae.check_orphan_servers(self.ctx(process_lister=lister))
        self.assertEqual(result.level, PASS)


class DoctorCliTests(TempRootTestCase):
    """命令行体检把运行状态检查并入 --json 输出；台账经 AE_HOME 指向临时目录。"""

    def run_doctor(self, ledger_home: Path):
        env = {**os.environ, "HOME": str(self.root / "home"), "AE_HOME": str(ledger_home),
               "CLAUDE_SKILLS_DIR": str(self.root / "skills"), "PYTHONDONTWRITEBYTECODE": "1"}
        proc = subprocess.run([sys.executable, str(ROOT / "ae.py"), "doctor", "--json"],
                              env=env, capture_output=True, text=True, timeout=120)
        return proc, json.loads(proc.stdout)

    def test_doctor_fails_on_unreadable_ledger(self):
        self.ledger_home.mkdir(parents=True)
        (self.ledger_home / "reviews.json").write_text("{坏", encoding="utf-8")
        proc, doc = self.run_doctor(self.ledger_home)
        by_code = {r["code"]: r for r in doc["results"]}
        self.assertEqual(by_code["ledger-readable"]["level"], "失败")
        self.assertEqual(by_code["ledger-readable"]["category"], "运行状态")
        self.assertEqual(proc.returncode, 1)      # 台账这项确定失败，与机器上的真实进程无关

    def test_doctor_runtime_ledger_checks_pass_when_absent(self):
        proc, doc = self.run_doctor(self.root / "no-ledger")
        by_code = {r["code"]: r for r in doc["results"]}
        self.assertEqual(by_code["ledger-readable"]["level"], "通过")
        self.assertEqual(by_code["ledger-stale"]["level"], "通过")


if __name__ == "__main__":
    unittest.main()
