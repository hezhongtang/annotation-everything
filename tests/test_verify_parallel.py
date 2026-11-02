"""有界验证进程调度的行为测试；只用合成目录和本测试启动的进程。"""
from __future__ import annotations

import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import host_isolation as hi
import verify_parallel as parallel


class ParallelProcessTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="ae-parallel-test-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def test_tasks_overlap_with_distinct_user_directories_and_preserve_failures(self):
        markers = self.root / "markers"
        markers.mkdir()
        before = dict(os.environ)

        def worker(name, output):
            (markers / name).write_text(str(os.getpid()))
            deadline = time.monotonic() + 5
            while len(list(markers.iterdir())) < 2 and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertEqual(len(list(markers.iterdir())), 2, "两项任务必须真正重叠")
            output.write_text(json.dumps({"home": os.environ["HOME"], "ledger": os.environ["AE_HOME"]}))
            print("synthetic output " + name)
            return 1 if name == "failure" else 0

        results = parallel.run_tasks([("success", "success"), ("failure", "failure")], worker,
                                     self.root / "workers", jobs=2)
        self.assertEqual(results["success"]["exit_status"], 0)
        self.assertEqual(results["failure"]["exit_status"], 1)
        environments = [json.loads(item["output"].read_text()) for item in results.values()]
        self.assertNotEqual(environments[0], environments[1])
        self.assertEqual(dict(os.environ), before, "子进程目录不能污染调用方环境")
        for name, result in results.items():
            self.assertIn("synthetic output " + name, result["log"].read_text())

    def test_killed_task_is_not_reported_as_success_and_other_task_finishes(self):
        def worker(name, output):
            if name == "killed":
                os.kill(os.getpid(), signal.SIGKILL)
            output.write_text('{"done": true}')
            return 0

        results = parallel.run_tasks([("killed", "killed"), ("passed", "passed")], worker,
                                     self.root / "workers", jobs=2)
        self.assertEqual(results["killed"]["exit_status"], -signal.SIGKILL)
        self.assertFalse(results["killed"]["output"].exists())
        self.assertEqual(results["passed"]["exit_status"], 0)

    def test_invalid_capacity_and_duplicate_ids_do_not_start_workers(self):
        def worker(*_):
            self.fail("非法请求不能启动任务")
        for jobs, tasks in ((0, [("one", 1)]), (4, [("one", 1)]), (2, [("one", 1), ("one", 2)])):
            with self.subTest(jobs=jobs, tasks=tasks), self.assertRaises(ValueError):
                parallel.run_tasks(tasks, worker, self.root / "workers", jobs=jobs)

    def start_script(self, body):
        script = self.root / "launcher.py"
        script.write_text("import sys\nsys.path.insert(0, " + repr(str(ROOT)) + ")\n" + body)
        env = dict(os.environ, AE_LOCK_DIR=str(self.root / "locks"))
        env.pop(hi.HELD_ENV, None)
        process = subprocess.Popen([sys.executable, str(script), str(self.root)], env=env,
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                                   start_new_session=True)
        def close():
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.communicate(timeout=10)
        self.addCleanup(close)
        return process, env

    def wait_file(self, path):
        deadline = time.monotonic() + 10
        while not path.exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertTrue(path.exists(), "等不到合成进程信号：" + path.name)

    def test_interrupt_runs_worker_cleanup_and_reaps_registered_children(self):
        process, _env = self.start_script('''
import os, time
from pathlib import Path
import verify_parallel as parallel
root = Path(sys.argv[1])
def worker(name, output):
    (root / name).write_text(str(os.getpid()))
    try:
        while True:
            time.sleep(0.01)
    finally:
        (root / (name + '.stopped')).write_text('cleaned')
try:
    parallel.run_tasks([('first', 'first'), ('second', 'second')], worker, root / 'workers', jobs=2)
except parallel.InterruptedRun:
    sys.exit(130)
''')
        for name in ("first", "second"):
            self.wait_file(self.root / name)
        process.terminate()
        out, err = process.communicate(timeout=10)
        self.assertEqual(process.returncode, 130, out + err)
        for name in ("first", "second"):
            self.assertEqual((self.root / (name + ".stopped")).read_text(), "cleaned")
            with self.assertRaises(ProcessLookupError):
                os.kill(int((self.root / name).read_text()), 0)

    def test_forked_worker_keeps_existing_resource_lock_if_parent_is_killed(self):
        process, env = self.start_script('''
import os, time
from pathlib import Path
import host_isolation as hi
import verify_parallel as parallel
root = Path(sys.argv[1])
held = hi.acquire(['host-state'])
def worker(name, output):
    (root / 'entered').write_text(str(os.getpid()))
    deadline = time.monotonic() + 10
    while not (root / 'release').exists() and time.monotonic() < deadline:
        time.sleep(0.01)
    return 0
parallel.run_tasks([('one', 'one')], worker, root / 'workers', jobs=1)
''')
        self.wait_file(self.root / "entered")
        process.kill()
        process.wait(timeout=10)
        with self.assertRaises(hi.LockTimeoutError):
            hi.acquire(["host-state"], timeout=0.1, env=env)
        (self.root / "release").write_text("go")
        handle = hi.acquire(["host-state"], timeout=10, env=env)
        hi.release(handle, env)


class PartialResultAndFamilyTests(unittest.TestCase):
    setUp = ParallelProcessTests.setUp
    start_script = ParallelProcessTests.start_script
    wait_file = ParallelProcessTests.wait_file
    def test_cancellation_keeps_a_workers_already_written_failure(self):
        process, _env = self.start_script('''
import os, time, json
from pathlib import Path
import verify_parallel as parallel
root = Path(sys.argv[1])
def worker(name, output):
    output.write_text(json.dumps({'failed': 1, 'passed': 2}))
    (root / 'ready').write_text('ready')
    try:
        while True: time.sleep(.01)
    finally:
        output.write_text(json.dumps({'failed': 1, 'passed': 2, 'interrupted': True}))
try:
    parallel.run_tasks([('partial', 'partial')], worker, root / 'workers', jobs=1)
except parallel.InterruptedRun as error:
    result = error.results.get('partial')
    if result:
        (root / 'preserved').write_text(result['output'].read_text())
    sys.exit(130)
''')
        self.wait_file(self.root / "ready")
        process.terminate()
        out, err = process.communicate(timeout=10)
        self.assertEqual(process.returncode, 130, out + err)
        self.assertTrue((self.root / "preserved").exists(), "取消必须保留 worker 已写出的原始失败")
        self.assertEqual(json.loads((self.root / "preserved").read_text())["failed"], 1)

    def test_killed_worker_does_not_leave_its_service_child_running(self):
        import socket
        def worker(_name, _output):
            script = """
import signal, socket, sys, time
from pathlib import Path
signal.signal(signal.SIGTERM, signal.SIG_IGN)
sock = socket.socket()
sock.bind(('127.0.0.1', 0))
sock.listen()
Path(sys.argv[1]).write_text(str(sock.getsockname()[1]))
while True: time.sleep(.01)
"""
            child = subprocess.Popen([sys.executable, "-c", script, str(self.root / "port")])
            (self.root / "child-pid").write_text(str(child.pid))
            self.wait_file(self.root / "port")
            os.kill(os.getpid(), signal.SIGKILL)
        def cleanup_child():
            pidfile = self.root / "child-pid"
            if pidfile.exists():
                try: os.kill(int(pidfile.read_text()), signal.SIGKILL)
                except ProcessLookupError: pass
        self.addCleanup(cleanup_child)
        results = parallel.run_tasks([("killed", "killed")], worker, self.root / "workers", jobs=1)
        self.assertEqual(results["killed"]["exit_status"], -signal.SIGKILL)
        port = int((self.root / "port").read_text())
        deadline = time.monotonic() + 3
        while True:
            with socket.socket() as probe:
                probe.settimeout(1)
                closed = probe.connect_ex(("127.0.0.1", port)) != 0
            if closed or time.monotonic() >= deadline:
                break
            time.sleep(0.01)
        self.assertTrue(closed, "异常 worker 的受控服务后代必须被回收")


    def test_signal_at_fork_cannot_escape_worker_registration(self):
        process, _env = self.start_script("""
import os, signal, time
from pathlib import Path
import verify_parallel as parallel
root = Path(sys.argv[1])
original = parallel.os.fork
def interrupted_fork():
    pid = original()
    if pid:
        os.kill(os.getpid(), signal.SIGTERM)
    return pid
parallel.os.fork = interrupted_fork
def worker(name, output):
    time.sleep(30)
    return 0
try:
    parallel.run_tasks([('one', 'one')], worker, root / 'workers', jobs=1)
except parallel.InterruptedRun as error:
    (root / 'registered').write_text(str(len(error.results)))
    sys.exit(130)
""")
        out, err = process.communicate(timeout=10)
        self.assertEqual(process.returncode, 130, out + err)
        self.assertEqual((self.root / "registered").read_text(), "1",
                         "fork 后立即取消，也必须先登记再回收 worker")

    def test_inline_task_keeps_original_process_and_child_timings_are_not_reap_delays(self):
        parent = os.getpid()
        marker = self.root / "child-done"
        def worker(name, output):
            if name == "serial":
                self.wait_file(marker)
                time.sleep(0.1)
            else:
                marker.write_text("done")
            output.write_text(str(os.getpid()))
            return 0
        results = parallel.run_tasks([("child", "child")], worker, self.root / "workers", jobs=2,
                                     inline=("serial", "serial"))
        self.assertEqual(int(results["serial"]["output"].read_text()), parent)
        self.assertNotEqual(int(results["child"]["output"].read_text()), parent)
        self.assertLess(results["child"]["seconds"], results["serial"]["seconds"])
        self.assertEqual(results["child"]["timing_precision"], "measured")

    def test_cancel_after_background_finished_while_inline_waits_keeps_all_records(self):
        process, _env = self.start_script('''
import os, time
from pathlib import Path
import verify_parallel as parallel
root = Path(sys.argv[1])
def worker(name, output):
    if name == 'inline':
        deadline = time.monotonic() + 10
        while not (root / 'background-ended').exists() and time.monotonic() < deadline:
            time.sleep(.01)
        time.sleep(.1)
        (root / 'ready').write_text('ready')
        while True: time.sleep(.01)
    output.write_text('original failure')
    (root / 'background-ended').write_text('done')
    return 1
try:
    parallel.run_tasks([('background', 'background')], worker, root / 'workers', jobs=2,
                       inline=('inline', 'inline'))
except parallel.InterruptedRun as error:
    (root / 'preserved').write_text(error.results['background']['output'].read_text())
    sys.exit(128 + error.signum)
''')
        self.wait_file(self.root / "ready")
        process.terminate()
        out, err = process.communicate(timeout=10)
        self.assertEqual(process.returncode, 143, out + err)
        self.assertEqual((self.root / "preserved").read_text(), "original failure")

    def test_cancel_during_family_cleanup_still_kills_registered_service(self):
        import socket
        process, _env = self.start_script('''
import os, signal, subprocess, time
from pathlib import Path
import verify_parallel as parallel
root = Path(sys.argv[1])
original = parallel._signal_family
triggered = False
def during_cleanup(pid, signum):
    global triggered
    result = original(pid, signum)
    if signum == signal.SIGTERM and not triggered:
        triggered = True
        os.kill(os.getpid(), signal.SIGTERM)
    return result
parallel._signal_family = during_cleanup
def worker(name, output):
    script = "import signal,socket,sys,time;from pathlib import Path;signal.signal(signal.SIGTERM,signal.SIG_IGN);s=socket.socket();s.bind(('127.0.0.1',0));s.listen();Path(sys.argv[1]).write_text(str(s.getsockname()[1]));time.sleep(30)"
    child = subprocess.Popen([sys.executable, '-c', script, str(root / 'port')])
    (root / 'child-pid').write_text(str(child.pid))
    deadline = time.monotonic() + 10
    while not (root / 'port').exists() and time.monotonic() < deadline: time.sleep(.01)
    os.kill(os.getpid(), signal.SIGKILL)
try:
    parallel.run_tasks([('family', 'family')], worker, root / 'workers', jobs=1)
except parallel.InterruptedRun as error:
    sys.exit(128 + error.signum)
''')
        out, err = process.communicate(timeout=10)
        self.assertEqual(process.returncode, 143, out + err)
        port = int((self.root / "port").read_text())
        deadline = time.monotonic() + 3
        while True:
            with socket.socket() as probe:
                probe.settimeout(1)
                closed = probe.connect_ex(("127.0.0.1", port)) != 0
            if closed or time.monotonic() >= deadline:
                break
            time.sleep(.01)
        self.assertTrue(closed, "正常收尾期间取消，也必须收完已登记进程族")

    def test_cancel_after_waitpid_before_registration_keeps_the_reaped_result(self):
        process, _env = self.start_script('''
import os, signal
from pathlib import Path
import verify_parallel as parallel
root = Path(sys.argv[1])
original = parallel.os.waitpid
sent = False
def interrupted_waitpid(pid, options):
    global sent
    result = original(pid, options)
    if result[0] and not sent:
        sent = True
        os.kill(os.getpid(), signal.SIGTERM)
    return result
parallel.os.waitpid = interrupted_waitpid
def worker(name, output):
    output.write_text('original failed result')
    return 1
try:
    parallel.run_tasks([('one', 'one')], worker, root / 'workers', jobs=1)
except parallel.InterruptedRun as error:
    result = error.results['one']
    (root / 'preserved').write_text(result['output'].read_text())
    sys.exit(128 + error.signum)
''')
        out, err = process.communicate(timeout=10)
        self.assertEqual(process.returncode, 143, out + err)
        self.assertEqual((self.root / "preserved").read_text(), "original failed result")
