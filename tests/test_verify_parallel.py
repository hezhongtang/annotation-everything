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
            if process.poll() is None:
                os.killpg(process.pid, signal.SIGKILL)
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
