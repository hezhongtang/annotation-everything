"""同机资源隔离协议的测试（issue #52）。

协议的参与方是验证入口（verify.py）与资源敏感的测试入口（unittest discover、
e2e_browser.py 直跑）。锁只协调参与者：外部占用者与未登记进程绝不因锁被停止。

测试只用合成锁目录（AE_LOCK_DIR 指向临时目录）与登记过的合成子进程，
不与并行会话的真实互斥发生冲突。
"""
from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import host_isolation as hi  # noqa: E402


class TemporaryLockDirTestCase(unittest.TestCase):
    """每个用例一套独立锁目录；子进程脚本据此并行扮演不同 worktree 的验证入口。"""

    def setUp(self):
        self.lock_dir = tempfile.TemporaryDirectory(prefix="ae-lock-test-")
        self.addCleanup(self.lock_dir.cleanup)
        self.lock_root = Path(self.lock_dir.name)

    def child_env(self, **extra) -> dict:
        env = {**os.environ, "AE_LOCK_DIR": str(self.lock_root)}
        env.pop(hi.HELD_ENV, None)
        env.update({key: str(value) for key, value in extra.items()})
        return env


class ResourceDeclarationTests(TemporaryLockDirTestCase):
    def test_declares_host_state_resource_with_reason_beyond_one_directory(self):
        declarations = hi.resource_declarations()
        self.assertIn("host-state", declarations)
        entry = declarations["host-state"]
        self.assertEqual(entry["scope"], "machine-user")
        self.assertTrue(entry["why"].strip(), "资源归属必须说明为何超出单个目录")
        self.assertTrue(entry["title"].strip())

    def test_known_independent_resources_are_declared_with_rationale(self):
        """台账/技能副本走合成注入，不共享；声明里要写清为什么不需要互斥。"""
        declarations = hi.resource_declarations()
        self.assertIn("ledger-and-skill-copy", declarations)
        self.assertEqual(declarations["ledger-and-skill-copy"]["mutually_exclusive"], False)

    def test_resource_order_is_fixed_for_consistent_acquisition(self):
        """多资源按声明顺序获取，避免交叉死锁；顺序必须稳定可复述。"""
        order = hi.resource_order()
        self.assertEqual(order, sorted(order))
        self.assertEqual(set(order), set(hi.resource_declarations()))


class AcquireReleaseTests(TemporaryLockDirTestCase):
    def test_acquire_and_release_roundtrip(self):
        # 用独立的干净环境：本进程可能在别的测试里接过敏感模块的锁（os.environ 带豁免变量），
        # 那种「借入」不属于本用例要验证的路径
        env = self.child_env()
        handle = hi.acquire(["host-state"], timeout=5.0, env=env)
        self.addCleanup(hi.release, handle, env)
        self.assertEqual(handle.resources, ("host-state",))
        self.assertFalse(handle.borrowed)

    def test_empty_resources_returns_immediately_without_lock(self):
        handle = hi.acquire([], timeout=5.0, env=self.child_env())
        self.assertEqual(handle.resources, ())
        hi.release(handle)

    def test_lock_directory_honors_override_and_user_shared_fallback(self):
        with tempfile.TemporaryDirectory() as other:
            env = {**os.environ, "AE_LOCK_DIR": other}
            self.assertEqual(hi.lock_root(env), Path(other))
        env = dict(os.environ)
        env.pop("AE_LOCK_DIR", None)
        root = hi.lock_root(env)
        self.assertIn("annotation-everything", root.name)
        self.assertNotIn(str(ROOT), str(root), "锁目录不能落在某个检出目录里")

    def test_unknown_resource_is_rejected(self):
        with self.assertRaises(hi.UnknownResourceError):
            hi.acquire(["no-such-resource"], timeout=1.0)


class WorkerScript:
    """合成参与者：按协议拿锁，进 / 出临界区各写一行时间戳，供父进程验证不重叠。"""

    TEMPLATE = textwrap.dedent("""
        import json, sys, time
        sys.path.insert(0, {root!r})
        import host_isolation as hi
        resources = json.loads(sys.argv[1])
        marker = sys.argv[2]
        hold = float(sys.argv[3])
        started = time.monotonic()
        handle = hi.acquire(resources, timeout={timeout!r})
        acquired = time.monotonic()
        with open(marker + ".enter", "w") as fh:
            fh.write(json.dumps({{"pid": __import__("os").getpid(),
                                  "waited": acquired - started}}))
        time.sleep(hold)
        hi.release(handle)
        with open(marker + ".exit", "w") as fh:
            fh.write("done")
    """)

    def __init__(self, test: TemporaryLockDirTestCase, resources, hold: float, timeout: float = 30.0):
        self.test = test
        self.resources = json_dumps(resources)
        self.hold = hold
        self.timeout = timeout
        self.marker_dir = Path(tempfile.mkdtemp(prefix="ae-lock-worker-"))
        test.addCleanup(shutil_rmtree, self.marker_dir)

    def start(self, name: str, env=None) -> subprocess.Popen:
        script = self.marker_dir / (name + ".py")
        script.write_text(self.TEMPLATE.format(root=str(ROOT), timeout=self.timeout), encoding="utf-8")
        marker = self.marker_dir / name
        proc = subprocess.Popen(
            [sys.executable, str(script), self.resources, str(marker), str(self.hold)],
            env=env or self.test.child_env(),
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        self.test.addCleanup(reap, proc)
        return proc, marker


def json_dumps(value) -> str:
    import json
    return json.dumps(list(value))


def shutil_rmtree(path: Path) -> None:
    import shutil
    shutil.rmtree(path, ignore_errors=True)


def reap(proc: subprocess.Popen) -> None:
    if proc.poll() is None:
        proc.kill()
    try:
        proc.communicate(timeout=10)
    except (subprocess.TimeoutExpired, OSError, ValueError):
        pass


def wait_for_file(path: Path, timeout: float = 30.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.is_file():
            return
        time.sleep(0.02)
    raise AssertionError("等不到 %s" % path)


def assert_no_overlap(test: unittest.TestCase, first: Path, second: Path) -> None:
    """用 enter/exit 标记文件的 mtime 证明两个临界区没有重叠。"""
    test.assertLessEqual(first.with_suffix(".exit").stat().st_mtime_ns,
                         second.with_suffix(".enter").stat().st_mtime_ns,
                         "临界区重叠：%s 未退出前 %s 已进入" % (first.name, second.name))


class CrossProcessMutexTests(TemporaryLockDirTestCase):
    def test_two_synthetic_worktrees_never_overlap_critical_section(self):
        """两个合成 worktree（同一用户、不同检出目录）同时启动敏感组，临界区串行。"""
        worker = WorkerScript(self, ["host-state"], hold=0.4)
        proc_a, marker_a = worker.start("worktree-a", env=self.child_env())
        proc_b, marker_b = worker.start("worktree-b", env=self.child_env())
        first, second = self._first_entrant((marker_a, marker_b))
        # 先进入者持有期间，后到者必须还在等锁
        self.assertFalse(second.with_suffix(".enter").exists(),
                         "先进入者持有 host-state 期间另一方进入了临界区")
        wait_for_file(first.with_suffix(".exit"))
        wait_for_file(second.with_suffix(".enter"))
        wait_for_file(second.with_suffix(".exit"))
        assert_no_overlap(self, first, second)
        for proc in (proc_a, proc_b):
            self.assertEqual(proc.wait(timeout=30), 0)

    def _first_entrant(self, markers, timeout: float = 30.0):
        """等待任一参与方进入临界区，返回（先进入者, 后到者）。"""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            entered = [m for m in markers if m.with_suffix(".enter").is_file()]
            if entered:
                other = [m for m in markers if m not in entered]
                return entered[0], other[0]
            time.sleep(0.01)
        raise AssertionError("没有任何一方进入临界区")

    def test_pure_logic_participant_not_blocked_by_unrelated_lock(self):
        """纯逻辑参与方（无资源要求）不被别人持有的 host-state 锁阻塞。"""
        holder = WorkerScript(self, ["host-state"], hold=1.0)
        proc, marker = holder.start("holder")
        try:
            wait_for_file(marker.with_suffix(".enter"))
            started = time.monotonic()
            handle = hi.acquire([], timeout=30.0, env=self.child_env())
            hi.release(handle)
            self.assertLess(time.monotonic() - started, 0.5, "空资源需求被无关锁阻塞")
        finally:
            self.assertEqual(proc.wait(timeout=30), 0)

    def test_lock_is_released_when_holder_is_killed(self):
        """持有者被 SIGKILL 后，后继运行可立即继续（内核回收 flock）。"""
        victim = WorkerScript(self, ["host-state"], hold=30.0)
        proc, marker = victim.start("victim")
        wait_for_file(marker.with_suffix(".enter"))
        proc.kill()
        proc.wait()
        started = time.monotonic()
        handle = hi.acquire(["host-state"], timeout=10.0, env=self.child_env())
        try:
            self.assertLess(time.monotonic() - started, 2.0, "持有者被杀后锁没有及时释放")
        finally:
            hi.release(handle)

    def test_cross_resource_deadlock_is_avoided_by_fixed_order(self):
        """两个参与方以相反顺序请求同一组资源，按固定顺序获取后都能完成。"""
        worker = WorkerScript(self, ["synthetic-a", "synthetic-b"], hold=0.2)
        proc_a, marker_a = worker.start("order-ab")
        proc_b, marker_b = worker.start("order-ba")
        for proc, marker in ((proc_a, marker_a), (proc_b, marker_b)):
            wait_for_file(marker.with_suffix(".exit"), timeout=30)
            self.assertEqual(proc.wait(timeout=30), 0, "交叉请求多资源发生死锁")


class NestedEntryTests(TemporaryLockDirTestCase):
    def test_child_of_holder_reenters_without_deadlock(self):
        """入口持锁后拉起的子流程（同一资源）按借入处理，立即获得，不死锁。"""
        env = self.child_env()
        parent = hi.acquire(["host-state"], timeout=5.0, env=env)
        self.addCleanup(hi.release, parent, env)
        worker = WorkerScript(self, ["host-state"], hold=0.1)
        proc, marker = worker.start("nested-child", env=env)
        try:
            wait_for_file(marker.with_suffix(".enter"), timeout=5.0)
            with open(marker.with_suffix(".enter"), encoding="utf-8") as fh:
                note = json.load(fh)
            self.assertEqual(note["pid"], proc.pid)
            self.assertEqual(proc.wait(timeout=5), 0, "嵌套子流程被自身入口的锁阻塞")
        finally:
            proc.kill()
            proc.wait()

    def test_sibling_without_inheritance_still_waits(self):
        """没有继承豁免的同级进程仍要等锁：豁免只沿父子链传递。"""
        parent = hi.acquire(["host-state"], timeout=5.0, env=self.child_env())
        self.addCleanup(hi.release, parent)
        started = time.monotonic()
        try:
            hi.acquire(["host-state"], timeout=0.3, env=self.child_env())
            self.fail("未豁免的进程不应拿到别人持有的锁")
        except hi.LockTimeoutError as exc:
            self.assertEqual(exc.resource, "host-state")
            self.assertGreaterEqual(exc.waited_seconds, 0.25)


class ExternalHolderTests(TemporaryLockDirTestCase):
    def test_external_holder_is_never_stopped_and_timeout_is_failure(self):
        """合成外部占用者持锁时：等待超时是明确失败，占用者绝不被停止。"""
        squatter = WorkerScript(self, ["host-state"], hold=2.0)
        proc, marker = squatter.start("squatter")
        try:
            wait_for_file(marker.with_suffix(".enter"))
            started = time.monotonic()
            with self.assertRaises(hi.LockTimeoutError) as caught:
                hi.acquire(["host-state"], timeout=0.3, env=self.child_env())
            self.assertGreaterEqual(caught.exception.waited_seconds, 0.25)
            self.assertLess(time.monotonic() - started, 1.5)
            self.assertIsNone(proc.poll(), "锁等待绝不能停止外部占用者")
            note = hi.read_holder_note("host-state", env=self.child_env())
            self.assertEqual(note["pid"], proc.pid, "诊断信息应指向真实持有者")
        finally:
            self.assertEqual(proc.wait(timeout=30), 0, "外部占用者被提前终止")

    def test_wait_limit_env_override(self):
        env = self.child_env(AE_LOCK_WAIT_SECONDS="0.2")
        self.assertEqual(hi.wait_limit(env), 0.2)
        env = self.child_env(AE_LOCK_WAIT_SECONDS="abc")
        self.assertEqual(hi.wait_limit(env), hi.DEFAULT_WAIT_SECONDS)


class LegacyEntryIntegrationTests(TemporaryLockDirTestCase):
    """直接跑 unittest discover 的旧入口与 e2e_browser 直跑都必须进入同一协议。"""

    IMPORT_PROBE = textwrap.dedent("""
        import sys, time
        sys.path.insert(0, {root!r})
        import tests.test_ae_cli
        print("imported", flush=True)
        time.sleep(0.4)   # 模块级持锁存活到进程结束：discover 的其余模块同受保护
    """)

    def test_discover_import_of_sensitive_module_holds_host_state(self):
        proc = subprocess.Popen(
            [sys.executable, "-c", self.IMPORT_PROBE.format(root=str(ROOT))],
            env=self.child_env(), stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        self.addCleanup(reap, proc)
        first = proc.stdout.readline().strip()
        self.assertEqual(first, "imported", "敏感模块导入失败：%s" % first)
        with self.assertRaises(hi.LockTimeoutError):
            hi.acquire(["host-state"], timeout=0.3, env=self.child_env())
        reap(proc)
        handle = hi.acquire(["host-state"], timeout=10.0, env=self.child_env())
        hi.release(handle)

    def test_browser_entry_script_joins_protocol_before_starting_services(self):
        """e2e_browser.py 直跑（uv run）也要先取锁：不留绕过路径。"""
        text = (ROOT / "tests" / "e2e_browser.py").read_text(encoding="utf-8")
        self.assertIn('require_for_module("tests.e2e_browser")', text)
        join_line = text.index('require_for_module("tests.e2e_browser")')
        first_server = text.index("start_server(")
        self.assertLess(join_line, first_server,
                        "e2e_browser 必须在启动任何服务之前加入隔离协议")


class RealEntryIntegrationTests(TemporaryLockDirTestCase):
    """真实仓库的新入口端到端：敏感选择器触发锁等待，且入口持锁不会锁死自己的子进程。"""

    def test_unit_related_with_sensitive_selector_waits_and_never_deadlocks(self):
        holder = WorkerScript(self, ["host-state"], hold=0.8)
        proc, marker = holder.start("real-entry-holder")
        wait_for_file(marker.with_suffix(".enter"))
        out_dir = Path(tempfile.mkdtemp(prefix="ae-verify-out-"))
        self.addCleanup(shutil_rmtree, out_dir)
        env = self.child_env(AE_VERIFY_DIR=str(out_dir), AE_HOME=str(out_dir / "home"))
        started = time.monotonic()
        done = subprocess.run(
            [sys.executable, str(ROOT / "verify.py"), "run",
             "unit-related", "tests.test_ae_cli.AeCliTests.test_missing_subcommand_is_command_error",
             "--quiet", "--out-dir", str(out_dir), "--phase", "development", "--reason",
             "issue 52 隔离协议端到端"],
            env=env, capture_output=True, text=True, timeout=120)
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        record = json.loads((out_dir / "runs" / json.loads(
            (out_dir / "latest.json").read_text(encoding="utf-8"))["run_id"]
            / "record.json").read_text(encoding="utf-8"))
        group = record["groups"][0]
        self.assertEqual(group["lock"]["resources"], ["host-state"],
                         "敏感选择器（tests.test_ae_cli）应触发 host-state 锁")
        self.assertGreaterEqual(group["lock"]["wait_seconds"], 0.4, "入口等到了外部持有者释放")
        self.assertEqual(group["outcome"], "passed")
        self.assertEqual(proc.wait(timeout=30), 0)
        # 入口拿锁后拉起 _unit-runner：敏感模块导入按借入处理，没有自锁
        self.assertLess(time.monotonic() - started, 60)

    def test_interrupted_entry_releases_its_lock(self):
        """入口被信号中断并回收子进程后，锁立即可用（内核回收 flock，无残留占用）。"""
        out_dir = Path(tempfile.mkdtemp(prefix="ae-verify-out-"))
        self.addCleanup(shutil_rmtree, out_dir)
        env = self.child_env(AE_VERIFY_DIR=str(out_dir))
        wrapper = subprocess.Popen(
            [sys.executable, str(ROOT / "verify.py"), "run", "--group", "unit",
             "--out-dir", str(out_dir), "--quiet"],
            env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            pointer = out_dir / "latest.json"
            deadline = time.monotonic() + 30
            while time.monotonic() < deadline and not pointer.is_file():
                time.sleep(0.05)
            self.assertTrue(pointer.is_file(), "入口没有写下运行目录")
            # 等入口真正进入组执行（children.json 在拿锁之后才写）再发信号
            run_id = json.loads(pointer.read_text(encoding="utf-8"))["run_id"]
            children = out_dir / "runs" / run_id / "children.json"
            deadline = time.monotonic() + 60
            while time.monotonic() < deadline and not children.is_file():
                time.sleep(0.05)
            self.assertTrue(children.is_file(), "入口没有进入组执行（没拿到锁？）")
            wrapper.send_signal(signal.SIGTERM)
            self.assertEqual(wrapper.wait(timeout=30), 128 + signal.SIGTERM)
        finally:
            reap(wrapper)
        started = time.monotonic()
        handle = hi.acquire(["host-state"], timeout=10.0, env=self.child_env())
        try:
            self.assertLess(time.monotonic() - started, 2.0, "中断退出的入口没有释放锁")
        finally:
            hi.release(handle)


class ModuleRequirementTests(TemporaryLockDirTestCase):
    def test_sensitive_modules_are_declared_and_exist(self):
        for name in hi.MODULE_RESOURCES:
            path = ROOT / (name.replace(".", "/") + ".py")
            self.assertTrue(path.is_file(), "敏感清单里的模块不存在：%s" % name)
            self.assertEqual(hi.MODULE_RESOURCES[name], ("host-state",))

    def test_declared_sensitive_modules_really_touch_host_state(self):
        """防清单腐烂：列入敏感清单的模块必须能找到触及宿主机状态的真实路径。"""
        signals = ("start_server", "free_port", "spawn_server", '"doctor"', "'doctor'",
                   "probe_port(", "list_processes()")
        for name in hi.MODULE_RESOURCES:
            text = (ROOT / (name.replace(".", "/") + ".py")).read_text(encoding="utf-8")
            self.assertTrue(any(token in text for token in signals),
                            "%s 里找不到触及宿主机状态的特征，不应列入敏感清单" % name)

    def test_pure_logic_modules_are_not_declared(self):
        for name in ("tests.test_i18n", "tests.test_script_formats", "tests.test_host_isolation"):
            self.assertNotIn(name, hi.MODULE_RESOURCES,
                             "%s 是纯逻辑模块，不该被资源锁阻塞" % name)

    def test_require_for_module_acquires_declared_resources(self):
        env = self.child_env()
        handle = hi.require_for_module("tests.test_ae_cli", env=env)
        try:
            self.assertIsNotNone(handle)
            self.assertEqual(handle.resources, ("host-state",))
            self.assertIn("host-state", hi.held_from_env(env))
        finally:
            hi.release(handle, env=env)
        self.assertEqual(hi.require_for_module("tests.test_i18n", env=self.child_env()), None)

    def test_require_for_module_times_out_as_failure(self):
        squatter = WorkerScript(self, ["host-state"], hold=1.5)
        proc, marker = squatter.start("module-squatter")
        try:
            wait_for_file(marker.with_suffix(".enter"))
            with self.assertRaises(hi.LockTimeoutError):
                hi.require_for_module("tests.test_ae_cli",
                                      env=self.child_env(AE_LOCK_WAIT_SECONDS="0.2"))
        finally:
            self.assertEqual(proc.wait(timeout=30), 0)


if __name__ == "__main__":
    unittest.main()
