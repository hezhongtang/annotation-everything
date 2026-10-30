"""e2e 服务生命周期与兜底清理的测试（issue #16）。

不依赖 playwright、不需要真 Chrome：用 dummy 长驻子进程复现
「没调用 stop 就退出时，服务与临时目录都被回收」这一用户症状。
"""
from __future__ import annotations

import os
import signal
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest
from pathlib import Path

TESTS = Path(__file__).resolve().parent
sys.path.insert(0, str(TESTS))

import e2e_servers  # noqa: E402

SLEEPER = "import time; time.sleep(120)"

CHILD = textwrap.dedent("""
    import os
    import signal
    import subprocess
    import sys
    import tempfile
    import threading
    import time
    from pathlib import Path

    sys.path.insert(0, {tests!r})
    import e2e_servers

    e2e_servers.install()
    grandchild = subprocess.Popen([sys.executable, "-c", {grandchild!r}],
                                  stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    while not Path({ready!r}).exists():
        time.sleep(0.02)
    d = Path(tempfile.mkdtemp(prefix="ae-registry-test-"))
    (d / "marker").write_text("x", encoding="utf-8")
    e2e_servers.CLEANUP.add(grandchild)
    e2e_servers.CLEANUP.add_dir(d)
    Path({probe!r}).write_text(f"{{grandchild.pid}}\\n{{d}}", encoding="utf-8")
    {extra}
    {tail}
""")


def alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def wait_dead(pid: int, timeout: float = 15.0) -> bool:
    end = time.time() + timeout
    while time.time() < end:
        if not alive(pid):
            return True
        time.sleep(0.05)
    return not alive(pid)


def sleeper() -> subprocess.Popen:
    return subprocess.Popen([sys.executable, "-c", SLEEPER],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def grandchild_code(ready: Path, ignore_sigterm: bool) -> str:
    """dummy 孙进程；ignore_sigterm 的版本专门用来拖满回收超时。"""
    head = "import signal\nimport time\n" if ignore_sigterm else "import time\n"
    ignore = "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n" if ignore_sigterm else ""
    return (head + "from pathlib import Path\n" + ignore
            + f"Path({str(ready)!r}).write_text('ready')\n"
              "time.sleep(120)\n")


class CleanupTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="ae-registry-test-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def kill(self, p):
        if p.poll() is None:
            p.kill()
            p.communicate()

    def test_stop_all_reaps_a_registered_service(self):
        p = sleeper()
        self.addCleanup(self.kill, p)
        e2e_servers.TestCleanup().stop_all()          # 空登记表不该炸
        reg = e2e_servers.TestCleanup()
        reg.add(p)
        reg.stop_all()
        self.assertTrue(wait_dead(p.pid), "登记过的服务没被回收")

    def test_stop_all_reaps_a_stopped_service(self):
        """被 SIGSTOP 卡住的服务也要回收（macOS 上 SIGTERM 即可，别的平台靠 SIGKILL 升级）。"""
        p = sleeper()
        self.addCleanup(self.kill, p)
        os.kill(p.pid, signal.SIGSTOP)
        reg = e2e_servers.TestCleanup()
        reg.add(p)
        reg.stop_all()
        self.assertTrue(wait_dead(p.pid), "被 SIGSTOP 卡住的服务没被回收")

    def test_stop_all_escalates_to_sigkill_when_a_service_ignores_sigterm(self):
        """不响应 SIGTERM 的服务必须升级到 SIGKILL，否则退出清理会一直等下去。"""
        ready = self.root / "ready"
        code = (f"import signal\nimport time\nfrom pathlib import Path\n"
                f"signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
                f"Path({str(ready)!r}).write_text('ready')\n"
                f"time.sleep(120)\n")
        p = subprocess.Popen([sys.executable, "-c", code],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.addCleanup(self.kill, p)
        end = time.time() + 10
        while not ready.exists() and time.time() < end:
            time.sleep(0.02)
        self.assertTrue(ready.exists(), "子进程没就绪，测不到「忽略 SIGTERM」这条路")

        started = time.time()
        reg = e2e_servers.TestCleanup()
        reg.add(p)
        reg.stop_all()
        elapsed = time.time() - started
        self.assertTrue(wait_dead(p.pid), "忽略 SIGTERM 的服务没被回收")
        self.assertGreaterEqual(elapsed, e2e_servers.TERMINATE_TIMEOUT,
                                "没等满超时就返回，说明根本没走 SIGKILL 升级分支")

    def test_stop_all_removes_registered_dirs(self):
        d = self.root / "review"
        d.mkdir()
        (d / "notes.json").write_text("{}", encoding="utf-8")
        reg = e2e_servers.TestCleanup()
        reg.add_dir(d)
        reg.stop_all()
        self.assertFalse(d.exists(), "登记过的临时目录没被清理")

    def test_stop_all_is_idempotent(self):
        p = sleeper()
        self.addCleanup(self.kill, p)
        reg = e2e_servers.TestCleanup()
        reg.add(p)
        reg.stop_all()
        reg.stop_all()
        self.assertTrue(wait_dead(p.pid))

    def test_discard_leaves_the_service_running(self):
        """正常 stop 掉的服务要注销，兜底清理不该去动已经处理过的。"""
        p = sleeper()
        self.addCleanup(self.kill, p)
        reg = e2e_servers.TestCleanup()
        reg.add(p)
        reg.discard(p)
        reg.stop_all()
        self.assertIsNone(p.poll(), "已 discard 的服务被兜底清理误杀了")


class StartServerTests(unittest.TestCase):
    """start_server 是真正的调用点：它拉起的服务必须登记。"""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="ae-registry-test-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        stub = self.root / "scripts"
        stub.mkdir()
        (stub / "serve.py").write_text(textwrap.dedent("""
            import json
            import sys
            import time
            from pathlib import Path

            d = Path(sys.argv[1])
            (d / ".annotation-server.json").write_text(json.dumps({"port": int(sys.argv[3])}))
            time.sleep(120)
        """), encoding="utf-8")
        self.scripts = stub

    def test_start_server_registers_the_service_it_starts(self):
        original = e2e_servers.SCRIPTS
        e2e_servers.SCRIPTS = self.scripts
        self.addCleanup(setattr, e2e_servers, "SCRIPTS", original)
        self.addCleanup(e2e_servers.CLEANUP.stop_all)

        d = self.root / "review"
        d.mkdir()
        p = e2e_servers.start_server(d, e2e_servers.free_port())

        self.assertIsNone(p.poll(), "服务应已就绪并在运行")
        e2e_servers.CLEANUP.stop_all()
        self.assertTrue(wait_dead(p.pid), "start_server 拉起的服务没登记，兜底清理回收不到")

    def test_start_server_cleans_up_when_the_service_never_reports_ready(self):
        original = e2e_servers.SCRIPTS
        e2e_servers.SCRIPTS = self.scripts
        self.addCleanup(setattr, e2e_servers, "SCRIPTS", original)
        self.addCleanup(e2e_servers.CLEANUP.stop_all)
        (self.scripts / "serve.py").write_text("import time\ntime.sleep(120)\n", encoding="utf-8")

        d = self.root / "review"
        d.mkdir()
        with self.assertRaises(RuntimeError):
            e2e_servers.start_server(d, e2e_servers.free_port())
        e2e_servers.CLEANUP.stop_all()


class AbnormalExitTests(unittest.TestCase):
    """issue #16 的用户症状：退出路径不调用 stop，服务和临时目录也要被回收。"""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="ae-registry-test-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def kill_pid(self, pid):
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass

    def run_child(self, tail: str, extra: str = "", ignore_sigterm: bool = False):
        ready = self.root / "ready"
        probe = self.root / "probe.txt"
        code = CHILD.format(tests=str(TESTS), grandchild=grandchild_code(ready, ignore_sigterm),
                            ready=str(ready), probe=str(probe), extra=extra, tail=tail)
        proc = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=180)
        self.assertTrue(probe.exists(), f"子进程没写出探针：{proc.stdout!r} {proc.stderr!r}")
        pid_text, dir_text = probe.read_text(encoding="utf-8").split("\n")[:2]
        return int(pid_text), Path(dir_text), proc

    def assert_cleaned_up(self, tail, expect_failure=False, returncode=None,
                          extra="", ignore_sigterm=False):
        pid, d, proc = self.run_child(tail, extra=extra, ignore_sigterm=ignore_sigterm)
        self.addCleanup(self.kill_pid, pid)
        if returncode is not None:
            self.assertEqual(proc.returncode, returncode, f"退出码不对：{proc.stderr}")
        elif expect_failure:
            self.assertNotEqual(proc.returncode, 0, "子进程本该异常退出")
        else:
            self.assertEqual(proc.returncode, 0, f"子进程异常：{proc.stderr}")
        self.assertTrue(wait_dead(pid),
                        f"退出后服务成了残留服务（pid {pid}，returncode {proc.returncode}）")
        self.assertFalse(d.exists(), f"退出后临时目录没清理：{d}")

    @staticmethod
    def second_signal(sig: str, after: float = 0.3) -> str:
        """让子进程在清理进行中再补一个信号，用来验重入防护。"""
        return textwrap.dedent(f"""
            def _second():
                time.sleep({after})
                os.kill(os.getpid(), signal.{sig})
            threading.Thread(target=_second, daemon=True).start()
        """)

    def test_keyboard_interrupt_still_cleans_up(self):
        self.assert_cleaned_up("raise KeyboardInterrupt", expect_failure=True)

    def test_uncaught_exception_still_cleans_up(self):
        self.assert_cleaned_up("raise RuntimeError('boom')", expect_failure=True)

    def test_normal_exit_without_stop_still_cleans_up(self):
        self.assert_cleaned_up("pass")

    def test_sigterm_still_cleans_up(self):
        """SIGTERM 不触发 atexit，只能靠 install() 装的处理器回收。"""
        self.assert_cleaned_up("import os, signal; os.kill(os.getpid(), signal.SIGTERM)",
                               returncode=-15)

    def test_sighup_still_cleans_up(self):
        """关终端发的是 SIGHUP，同样不触发 atexit。"""
        self.assert_cleaned_up("import os, signal; os.kill(os.getpid(), signal.SIGHUP)",
                               returncode=-1)

    def test_second_sigterm_during_cleanup_does_not_leave_services_behind(self):
        """清理耗时期间又补一个 SIGTERM，不该把还没收完的进程留下。"""
        self.assert_cleaned_up("import os, signal; os.kill(os.getpid(), signal.SIGTERM)",
                               returncode=-15, extra=self.second_signal("SIGTERM"),
                               ignore_sigterm=True)

    def test_second_ctrl_c_during_cleanup_does_not_leave_services_behind(self):
        """atexit 清理期间再按一次 Ctrl-C，同样不该留下残留服务。"""
        self.assert_cleaned_up("raise KeyboardInterrupt", expect_failure=True,
                               extra=self.second_signal("SIGINT"), ignore_sigterm=True)


if __name__ == "__main__":
    unittest.main()
