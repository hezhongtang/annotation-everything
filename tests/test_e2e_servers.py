"""e2e 服务生命周期与兜底清理的测试（issue #16；等待与时序改造见 issue #51）。

不依赖 playwright、不需要真 Chrome：用 dummy 长驻子进程复现
「没调用 stop 就退出时，服务与临时目录都被回收」这一用户症状。

时序验证不靠睡够固定秒数：孙进程收到 SIGTERM 时写握手记号，父测试据此证明
信号确实送达、第二个信号确实落在清理的终止等待窗口内；「等满超时再升级」以
注入的短超时值和实际退出状态（SIGKILL）为断言。默认 5 秒 / 8 秒的产品等待
语义不变：短等待只通过受控参数注入，见 test_default_timeouts_keep_the_product_configuration
与保留的默认配置用例。
"""
from __future__ import annotations

import os
import shutil
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

# 测试注入的短等待（issue #51）：够观察升级 / 重入分支，又不拖慢套件。
# 二次信号的用例留更宽的窗口，握手后仍有余量，避免把负载抖动当成测试失败。
SHORT_TERMINATE = 0.5
HANDSHAKE_TERMINATE = 0.75
SHORT_READY = 0.5

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

    cleanup = e2e_servers.TestCleanup(terminate_timeout={terminate_timeout!r})
    e2e_servers.install(cleanup)
    grandchild = subprocess.Popen([sys.executable, "-c", {grandchild!r}],
                                  stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    while not Path({ready!r}).exists():
        time.sleep(0.02)
    d = Path(tempfile.mkdtemp(prefix="ae-registry-test-"))
    (d / "marker").write_text("x", encoding="utf-8")
    cleanup.add(grandchild)
    cleanup.add_dir(d)
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
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if not alive(pid):
            return True
        time.sleep(0.05)
    return not alive(pid)


def sleeper() -> subprocess.Popen:
    return subprocess.Popen([sys.executable, "-c", SLEEPER],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def grandchild_code(ready: Path, ignore_sigterm: bool, got_term: Path | None = None) -> str:
    """dummy 孙进程；ignore_sigterm 的版本收到 SIGTERM 只写握手记号、不退出。

    用「不退出的处理器」而不是 SIG_IGN：行为上同样不响应终止（升级分支仍要
    走 SIGKILL），同时留下「SIGTERM 已送达」的外部证据，供时序断言使用。
    """
    lines = ["from pathlib import Path"]
    if ignore_sigterm:
        lines.append("import signal")
        lines.append("def _stall(signum, frame):")
        lines.append(f"    Path({str(got_term)!r}).write_text('term')")
        lines.append("signal.signal(signal.SIGTERM, _stall)")
    lines.append(f"Path({str(ready)!r}).write_text('ready')")
    lines.append("import time")
    lines.append("time.sleep(120)")
    return "\n".join(lines) + "\n"


def second_signal_source(sig: str, got_term: Path, sent: Path) -> str:
    """等孙进程报告「已收到 SIGTERM」后再补一个信号，证明它落在清理的终止等待窗口内。

    孙进程还活着才发（此时 reap 仍阻塞在终止等待里）；发完写 during / late，
    late 时父测试按失败处理，不把错过窗口当成通过。
    """
    return textwrap.dedent(f"""
        def _second():
            while not Path({str(got_term)!r}).exists():
                time.sleep(0.005)
            try:
                os.kill(grandchild.pid, 0)
                during = True
            except OSError:
                during = False
            if during:
                os.kill(os.getpid(), signal.{sig})
            Path({str(sent)!r}).write_text('during' if during else 'late')
        threading.Thread(target=_second, daemon=True).start()
    """)


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
        reg = e2e_servers.TestCleanup()               # 缺省配置的真实子进程路径
        reg.add(p)
        reg.stop_all()
        self.assertTrue(wait_dead(p.pid), "登记过的服务没被回收")

    def test_stop_all_reaps_a_stopped_service(self):
        """被 SIGSTOP 卡住的服务也要回收（macOS 上 SIGTERM 即可，别的平台靠 SIGKILL 升级）。"""
        p = sleeper()
        self.addCleanup(self.kill, p)
        os.kill(p.pid, signal.SIGSTOP)
        reg = e2e_servers.TestCleanup(terminate_timeout=SHORT_TERMINATE)
        reg.add(p)
        reg.stop_all()
        self.assertTrue(wait_dead(p.pid), "被 SIGSTOP 卡住的服务没被回收")

    def test_stop_all_escalates_to_sigkill_when_a_service_ignores_sigterm(self):
        """等满注入的终止超时才升级 SIGKILL；以注入值和实际退出状态为断言。"""
        ready = self.root / "ready"
        got_term = self.root / "got-term"
        p = subprocess.Popen([sys.executable, "-c",
                              grandchild_code(ready, True, got_term)],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.addCleanup(self.kill, p)
        end = time.monotonic() + 10
        while not ready.exists() and time.monotonic() < end:
            time.sleep(0.02)
        self.assertTrue(ready.exists(), "子进程没就绪，测不到「忽略 SIGTERM」这条路")

        started = time.monotonic()
        reg = e2e_servers.TestCleanup(terminate_timeout=SHORT_TERMINATE)
        reg.add(p)
        reg.stop_all()
        elapsed = time.monotonic() - started
        self.assertTrue(got_term.exists(), "SIGTERM 没有送达，未进入忽略终止状态")
        self.assertEqual(p.poll(), -signal.SIGKILL, "没有走 SIGKILL 升级分支")
        self.assertTrue(wait_dead(p.pid), "忽略 SIGTERM 的服务没被回收")
        self.assertGreaterEqual(elapsed, SHORT_TERMINATE,
                                "没等满注入的终止超时就升级，说明跳过了目标分支")

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

    def test_default_timeouts_keep_the_product_configuration(self):
        """短等待只是测试注入的边界参数：缺省配置仍是产品的 5 秒 / 8 秒。"""
        self.assertEqual(e2e_servers.TERMINATE_TIMEOUT, 5.0)
        self.assertEqual(e2e_servers.READY_TIMEOUT, 8.0)
        self.assertEqual(e2e_servers.TestCleanup().terminate_timeout,
                         e2e_servers.TERMINATE_TIMEOUT)
        self.assertEqual(e2e_servers.TestCleanup(terminate_timeout=0.25).terminate_timeout,
                         0.25)


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
        # 缺省配置路径：不注入短等待，靠 stub 立即就绪
        self.addCleanup(e2e_servers.CLEANUP.stop_all)

        d = self.root / "review"
        d.mkdir()
        p = e2e_servers.start_server(d, e2e_servers.free_port(), scripts=self.scripts)

        self.assertIsNone(p.poll(), "服务应已就绪并在运行")
        e2e_servers.CLEANUP.stop_all()
        self.assertTrue(wait_dead(p.pid), "start_server 拉起的服务没登记，兜底清理回收不到")

    def test_start_server_cleans_up_when_the_service_never_reports_ready(self):
        (self.scripts / "serve.py").write_text("import time\ntime.sleep(120)\n", encoding="utf-8")
        self.addCleanup(e2e_servers.CLEANUP.stop_all)

        d = self.root / "review"
        d.mkdir()
        started = time.monotonic()
        with self.assertRaises(RuntimeError):
            e2e_servers.start_server(d, e2e_servers.free_port(), scripts=self.scripts,
                                     ready_timeout=SHORT_READY)
        elapsed = time.monotonic() - started
        self.assertGreaterEqual(elapsed, SHORT_READY, "没等满注入的就绪超时就放弃")
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

    def _salvage(self, probe: Path) -> None:
        """子进程超时或被打断时，按探针回收它登记的孙进程与临时目录。

        探针写了一半（只有 pid 行）时至少回收孙进程；探针没写出时无凭据可回收，
        孙进程只能靠自灭时限（CHILD 注入的 sleep(120)）退出——登记机制以探针为
        唯一凭据，这是它的固有边界。
        """
        if not probe.exists():
            return
        try:
            pid_text, *rest = probe.read_text(encoding="utf-8").split("\n")
            pid = int(pid_text)
        except ValueError:
            return
        self.kill_pid(pid)
        if rest and rest[0].strip():
            shutil.rmtree(rest[0], ignore_errors=True)

    def run_child(self, tail: str, extra: str = "", ignore_sigterm: bool = False,
                  terminate_timeout: float = SHORT_TERMINATE):
        ready = self.root / "ready"
        probe = self.root / "probe.txt"
        got_term = self.root / "got-term"
        code = CHILD.format(tests=str(TESTS),
                            grandchild=grandchild_code(ready, ignore_sigterm, got_term),
                            ready=str(ready), probe=str(probe), extra=extra, tail=tail,
                            terminate_timeout=terminate_timeout)
        # 探针断言与读取也在 try 里：断言失败（探针没写出 / 只写了一半）同样要
        # _salvage，不能把孙进程留在登记机制外。探针完全没写出时 _salvage 没有
        # 凭据，孙进程只能靠自灭时限（sleep(120)）退出——见 _salvage 的边界说明。
        try:
            proc = subprocess.run([sys.executable, "-c", code], capture_output=True,
                                  text=True, timeout=180)
            self.assertTrue(probe.exists(), f"子进程没写出探针：{proc.stdout!r} {proc.stderr!r}")
            pid_text, dir_text = probe.read_text(encoding="utf-8").split("\n")[:2]
        except BaseException:
            self._salvage(probe)
            raise
        return int(pid_text), Path(dir_text), proc

    def assert_cleaned_up(self, tail, expect_failure=False, returncode=None,
                          extra="", ignore_sigterm=False,
                          terminate_timeout=SHORT_TERMINATE, sent=None):
        pid, d, proc = self.run_child(tail, extra=extra, ignore_sigterm=ignore_sigterm,
                                      terminate_timeout=terminate_timeout)
        self.addCleanup(self.kill_pid, pid)
        if returncode is not None:
            self.assertEqual(proc.returncode, returncode, f"退出码不对：{proc.stderr}")
        elif expect_failure:
            self.assertNotEqual(proc.returncode, 0, "子进程本该异常退出")
        else:
            self.assertEqual(proc.returncode, 0, f"子进程异常：{proc.stderr}")
        if sent is not None:
            self.assertTrue(sent.exists(), "第二个信号没有发出")
            self.assertEqual(sent.read_text(encoding="utf-8"), "during",
                             "第二个信号没有落在清理的终止等待窗口内")
        self.assertTrue(wait_dead(pid),
                        f"退出后服务成了残留服务（pid {pid}，returncode {proc.returncode}）")
        self.assertFalse(d.exists(), f"退出后临时目录没清理：{d}")

    def test_probe_assertion_failure_still_salvages_the_grandchild(self):
        """R2：探针断言与读取失败也要走 _salvage，半截探针至少回收孙进程。

        模拟 CHILD 没能写出完整探针（写探针前崩溃 / 只写了一半）：探针存在但
        只有 pid 行时读取 unpack 会失败，这条路径不走 _salvage 就会漏掉回收。
        os._exit 绕过 atexit，排除子进程兜底清理的干扰，回收只能来自 _salvage。
        """
        probe = self.root / "probe.txt"
        orphan_pid = self.root / "grandchild-pid"
        tail = (f"Path({str(probe)!r}).write_text(str(grandchild.pid)); "
                f"Path({str(orphan_pid)!r}).write_text(str(grandchild.pid)); "
                "os._exit(3)")
        with self.assertRaises(ValueError):
            self.run_child(tail)
        pid = int(orphan_pid.read_text(encoding="utf-8"))
        self.addCleanup(self.kill_pid, pid)
        self.assertTrue(wait_dead(pid), "探针断言 / 读取失败路径没有回收孙进程")

    def test_missing_probe_assertion_failure_accepts_the_self_destruct_boundary(self):
        """R2 登记机制的固有边界：探针完全没写出时 _salvage 没有凭据，无孙进程 pid 可回收。

        孙进程在自灭时限（CHILD 注入的 sleep(120)）内会自行退出，测试不等待它，
        只证明断言失败路径确实执行、也不把「无凭据」伪装成已回收，并自己清场。
        """
        probe = self.root / "probe.txt"
        orphan_pid = self.root / "grandchild-pid"
        tail = (f"Path({str(orphan_pid)!r}).write_text(str(grandchild.pid)); "
                f"Path({str(probe)!r}).unlink(); os._exit(3)")
        with self.assertRaises(AssertionError):
            self.run_child(tail)
        pid = int(orphan_pid.read_text(encoding="utf-8"))
        self.addCleanup(self.kill_pid, pid)
        self.assertTrue(alive(pid),
                        "场景失真：探针没写出时孙进程应仍在自灭时限内运行，"
                        "而不是被凭空回收")

    def test_keyboard_interrupt_still_cleans_up(self):
        self.assert_cleaned_up("raise KeyboardInterrupt", expect_failure=True)

    def test_ctrl_c_during_stop_does_not_leave_services_behind(self):
        """正常 stop 等待退出时被打断，仍由 atexit 回收尚未结束的服务。"""
        sent = self.root / "sent"
        self.assert_cleaned_up("e2e_servers.stop(grandchild, cleanup)", expect_failure=True,
                               extra=second_signal_source("SIGINT", self.root / "got-term", sent),
                               ignore_sigterm=True,
                               terminate_timeout=HANDSHAKE_TERMINATE, sent=sent)

    def assert_start_timeout_cleans_up(self, interrupt=False):
        probe = self.root / "startup-pid"
        terminating = self.root / "terminating"
        scripts = self.root / "scripts"
        scripts.mkdir()
        (scripts / "serve.py").write_text(textwrap.dedent(f"""
            import os
            import signal
            import sys
            import time
            from pathlib import Path

            def ignore_termination(signum, frame):
                Path({str(terminating)!r}).write_text('terminating')
            signal.signal(signal.SIGTERM, ignore_termination)
            Path({str(probe)!r}).write_text(str(os.getpid()))
            print('startup stalled', file=sys.stderr, flush=True)
            time.sleep(120)
        """), encoding="utf-8")
        d = self.root / "review"
        d.mkdir()

        def cleanup_service():
            if probe.exists():
                self.kill_pid(int(probe.read_text()))
        self.addCleanup(cleanup_service)

        code = textwrap.dedent(f"""
            import os
            import signal
            import sys
            import threading
            import time
            from pathlib import Path

            sys.path.insert(0, {str(TESTS)!r})
            import e2e_servers
            cleanup = e2e_servers.TestCleanup(terminate_timeout={SHORT_TERMINATE!r})
            e2e_servers.install(cleanup)
            cleanup.add_dir(Path({str(d)!r}))
            def interrupt_cleanup():
                while not Path({str(terminating)!r}).exists():
                    time.sleep(0.01)
                os.kill(os.getpid(), signal.SIGINT)
            if {interrupt!r}:
                threading.Thread(target=interrupt_cleanup, daemon=True).start()
            e2e_servers.start_server(Path({str(d)!r}), 12345, cleanup,
                                     scripts=Path({str(scripts)!r}),
                                     ready_timeout={SHORT_READY!r})
        """)
        started = time.monotonic()
        proc = subprocess.run([sys.executable, "-c", code], capture_output=True,
                              text=True, timeout=30)
        elapsed = time.monotonic() - started
        self.assertTrue(probe.exists(), f"服务未启动：{proc.stderr}")
        self.assertTrue(terminating.exists(), "未进入忽略终止的失败回收路径")
        self.assertNotEqual(proc.returncode, 0, "未就绪的服务不应启动成功")
        self.assertGreaterEqual(elapsed, SHORT_READY + SHORT_TERMINATE,
                                "没有等满注入的就绪与终止超时")
        if interrupt:
            self.assertIn("KeyboardInterrupt", proc.stderr)
        else:
            self.assertIn("server did not start on the expected port: startup stalled", proc.stderr)
        self.assertTrue(wait_dead(int(probe.read_text())), "启动失败后留下残留服务")
        self.assertFalse(d.exists(), "启动失败后临时目录未清理")

    def test_start_timeout_escalates_when_the_service_ignores_sigterm(self):
        self.assert_start_timeout_cleans_up()

    def test_ctrl_c_during_start_timeout_does_not_leave_services_behind(self):
        self.assert_start_timeout_cleans_up(interrupt=True)

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
        sent = self.root / "sent"
        self.assert_cleaned_up("import os, signal; os.kill(os.getpid(), signal.SIGTERM)",
                               returncode=-15,
                               extra=second_signal_source("SIGTERM", self.root / "got-term", sent),
                               ignore_sigterm=True,
                               terminate_timeout=HANDSHAKE_TERMINATE, sent=sent)

    def test_second_ctrl_c_during_cleanup_does_not_leave_services_behind(self):
        """atexit 清理期间再按一次 Ctrl-C，同样不该留下残留服务。"""
        sent = self.root / "sent"
        self.assert_cleaned_up("raise KeyboardInterrupt", expect_failure=True,
                               extra=second_signal_source("SIGINT", self.root / "got-term", sent),
                               ignore_sigterm=True,
                               terminate_timeout=HANDSHAKE_TERMINATE, sent=sent)


if __name__ == "__main__":
    unittest.main()
