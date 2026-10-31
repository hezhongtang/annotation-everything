"""统一验证入口（verify.py）的外部行为测试：命令行、退出码、结构化记录与子进程生命周期。

每个用例都跑在合成仓库里（临时目录 + git init + 合成测试程序 + 合成 uv），
不碰真实仓库内容、不启动真实浏览器、不读取任何真实审阅材料。
只验证命令行边界与落盘证据，不绑定入口内部的模块拆分。对应 issue #50。
"""
from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
VERIFY = ROOT / "verify.py"

# 合成 uv：剥掉 run --no-project --with <依赖>，直接执行剩下的命令（浏览器依赖仍只在真入口里导入）
UV_STUB = '''#!/usr/bin/env python3
import os
import sys

args = sys.argv[1:]
index = 0
while index < len(args):
    token = args[index]
    if token in ("run", "--no-project"):
        index += 1
    elif token == "--with":
        index += 2
    else:
        break
if args[index] == "python":
    args[index] = sys.executable
os.execvp(args[index], args[index:])
'''

# 合成服务安全自检：只打印与真自检相同的摘要行，退出码由 SYNTH_SERVE_FAILED 决定
SERVE_STUB = '''#!/usr/bin/env python3
import os
import sys

total = int(os.environ.get("SYNTH_SERVE_TOTAL", "3"))
failed = int(os.environ.get("SYNTH_SERVE_FAILED", "0"))
for index in range(total - failed):
    print("通过  合成自检项 %d" % index)
for index in range(failed):
    print("失败  合成自检项 %d" % (total - failed + index))
print("\\n%d/%d 通过" % (total - failed, total))
sys.exit(1 if failed else 0)
'''

# 合成浏览器入口：打印与 tests/e2e_browser.py 相同的 PASS / FAIL 行与摘要
BROWSER_STUB = '''#!/usr/bin/env python3
import os
import sys
import time

total = int(os.environ.get("SYNTH_BROWSER_TOTAL", "3"))
failed = int(os.environ.get("SYNTH_BROWSER_FAILED", "0"))
seconds = float(os.environ.get("SYNTH_BROWSER_SECONDS", "0.2"))
time.sleep(seconds)
for index in range(total - failed):
    print("PASS  合成场景 %d" % index)
for index in range(failed):
    print("FAIL  合成场景 %d" % (total - failed + index))
print("\\n%d/%d 通过" % (total - failed, total))
sys.exit(1 if failed else 0)
'''

UNIT_PASS = '''import unittest


class AlphaTests(unittest.TestCase):
    def test_alpha_ok(self):
        self.assertEqual(1 + 1, 2)

    def test_alpha_also_ok(self):
        self.assertIn("a", "cat")


class BetaTests(unittest.TestCase):
    def test_beta_ok(self):
        self.assertGreater(3, 2)
'''


def read_record(artifact_dir: Path, run_id: str) -> dict:
    return json.loads((artifact_dir / "runs" / run_id / "record.json").read_text(encoding="utf-8"))


class SynthRepo:
    """合成仓库：自带 git 历史、合成测试程序与合成 uv；verify.py 用本仓库的副本运行。"""

    def __init__(self, root: Path):
        self.path = root / "repo"
        (self.path / "tests").mkdir(parents=True)
        (self.path / "skill" / "annotation-everything" / "scripts").mkdir(parents=True)
        (self.path / "bin").mkdir(parents=True)
        self.write("bin/uv", UV_STUB, executable=True)
        self.write(".gitignore", ".verify/\n__pycache__/\n*.pyc\n*.log\n")
        self.write("README.md", "# 合成仓库\n")
        self.write("tests/test_synth.py", UNIT_PASS)
        self.write("tests/e2e_browser.py", BROWSER_STUB, executable=True)
        self.write("skill/annotation-everything/scripts/serve.py", SERVE_STUB, executable=True)
        shutil.copy2(VERIFY, self.path / "verify.py")
        self.git("init", "-q")
        self.git("add", "-A")
        self.git("-c", "user.name=synth", "-c", "user.email=synth@example.invalid",
                 "commit", "-q", "-m", "合成基线")

    # -- 文件与 git -------------------------------------------------------
    def write(self, rel: str, text: str, executable: bool = False) -> Path:
        path = self.path / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        if executable:
            path.chmod(0o755)
        return path

    def git(self, *args: str) -> subprocess.CompletedProcess:
        return subprocess.run(["git", *args], cwd=self.path, capture_output=True, text=True,
                              env=self.git_env, timeout=60)

    @property
    def git_env(self) -> dict:
        env = dict(os.environ)
        env["GIT_CONFIG_GLOBAL"] = os.devnull
        env["GIT_CONFIG_SYSTEM"] = os.devnull
        env["GIT_TERMINAL_PROMPT"] = "0"
        return env

    def commit_all(self, message: str) -> None:
        self.git("add", "-A")
        self.git("-c", "user.name=synth", "-c", "user.email=synth@example.invalid",
                 "commit", "-q", "-m", message)

    # -- 运行入口 --------------------------------------------------------
    @property
    def artifact_dir(self) -> Path:
        return self.path / ".verify"

    def run_verify(self, *args: str, timeout: float = 120, extra_env: dict | None = None,
                   cwd: Path | None = None) -> subprocess.CompletedProcess:
        env = dict(os.environ)
        env["PATH"] = f"{self.path / 'bin'}{os.pathsep}{env['PATH']}"
        env["GIT_CONFIG_GLOBAL"] = os.devnull
        env["GIT_CONFIG_SYSTEM"] = os.devnull
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        env.pop("AE_VERIFY_DIR", None)
        env.pop("AE_LOCALE", None)
        if extra_env:
            env.update(extra_env)
        return subprocess.run([sys.executable, str((self.path / "verify.py").resolve()), *args],
                              cwd=str(cwd or self.path), env=env,
                              capture_output=True, text=True, timeout=timeout)

    def latest_run_id(self) -> str:
        return json.loads((self.artifact_dir / "latest.json").read_text(encoding="utf-8"))["run_id"]

    def run_ids(self) -> list:
        return sorted(p.name for p in (self.artifact_dir / "runs").iterdir())


class VerifyEntryTestCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="verify-entry-test-")
        self.addCleanup(self.temp.cleanup)
        self.repo = SynthRepo(Path(self.temp.name))

    def unit(self, body: str) -> None:
        self.repo.write("tests/test_synth.py", body)


class PlanTests(VerifyEntryTestCase):
    def test_plan_explains_every_group_and_command_without_running_tests(self):
        p = self.repo.run_verify("plan")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        for name in ("unit", "unit-related", "browser", "serve-check", "all"):
            self.assertIn(name, p.stdout, f"plan 说明 {name}")
        self.assertIn("python3 -m unittest discover tests", p.stdout, "说明完整单元的等价命令")
        self.assertIn("uv run --no-project --with playwright python tests/e2e_browser.py", p.stdout,
                      "说明完整浏览器的等价命令")
        self.assertIn("skill/annotation-everything/scripts/serve.py check", p.stdout, "说明服务安全自检")
        self.assertFalse((self.repo.artifact_dir).exists(), "plan 不写任何记录")
        self.assertNotIn("test_alpha_ok", p.stdout, "plan 不执行测试")

    def test_plan_json_is_readable_and_marks_the_default_group(self):
        p = self.repo.run_verify("plan", "--json")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        plan = json.loads(p.stdout)
        names = [group["name"] for group in plan["groups"]]
        self.assertEqual(names, ["unit", "unit-related", "browser", "serve-check", "all"])
        unit = plan["groups"][0]
        self.assertEqual(unit["counter"], "unittest")
        self.assertEqual(unit["granularity"], "case")
        self.assertEqual(unit["selectors"], False)
        browser = plan["groups"][2]
        self.assertEqual(browser["granularity"], "group", "浏览器未拆场景前按整组计时")
        self.assertEqual(plan["default_group"], "unit")


class PlanPurposeTests(VerifyEntryTestCase):
    def test_unit_related_purpose_does_not_advertise_directory_selectors(self):
        """R4：plan 不把「目录」说成 unit-related 受支持的选择器，与 onboarding 口径一致。"""
        p = self.repo.run_verify("plan")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertNotIn("目录", p.stdout, "plan 不宣称支持目录选择器")
        plan = json.loads(self.repo.run_verify("plan", "--json").stdout)
        purpose = {group["name"]: group["purpose"] for group in plan["groups"]}["unit-related"]
        self.assertNotIn("目录", purpose)
        self.assertIn("用例", purpose)


class UnitRunTests(VerifyEntryTestCase):
    def test_successful_unit_run_records_counts_timing_and_evidence(self):
        p = self.repo.run_verify("run", "--phase", "development", "--issue", "50")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        run_id = self.repo.latest_run_id()
        record = read_record(self.repo.artifact_dir, run_id)
        self.assertEqual(record["schema"], "ae-verify-run/1")
        self.assertEqual(record["state"], "complete")
        self.assertEqual(record["result"]["outcome"], "passed")
        self.assertEqual(record["result"]["exit_status"], 0)
        self.assertEqual(record["context"]["issue"], 50)
        self.assertEqual(record["context"]["phase"], "development")
        counts = record["result"]["counts"]
        self.assertEqual(counts["cases"], 3)
        self.assertEqual(counts["passed"], 3)
        self.assertEqual(counts["failed"], 0)
        self.assertEqual(counts["skipped"], 0)
        group = record["groups"][0]
        self.assertEqual(group["name"], "unit")
        self.assertEqual(group["outcome"], "passed")
        self.assertEqual(len(group["cases"]), 3, "每个用例一条记录")
        for case in group["cases"]:
            self.assertIn("seconds", case)
            self.assertGreaterEqual(case["seconds"], 0.0)
            self.assertIn(case["status"], ("passed", "failed", "error", "skipped",
                                           "expected_failure", "unexpected_success"))
        # 用例耗时合计与墙钟分开陈述，不用累计值冒充等待时间
        timing = record["timing"]
        self.assertGreater(timing["wall_seconds"], 0)
        self.assertGreaterEqual(timing["case_seconds_total"], 0)
        self.assertIn("fixture_seconds_total", timing)
        self.assertLessEqual(timing["queue_seconds"], timing["wall_seconds"])
        self.assertEqual(record["evidence"]["integrity"], "ok")
        log = self.repo.artifact_dir / "runs" / run_id / "unit.log"
        self.assertTrue(log.is_file(), "保留子进程原始日志")
        self.assertIn("test_alpha_ok", log.read_text(encoding="utf-8"))

    def test_failing_unit_run_keeps_original_exit_status_and_evidence(self):
        self.unit(UNIT_PASS.replace("self.assertEqual(1 + 1, 2)", "self.assertEqual(1 + 1, 3)"))
        self.repo.commit_all("合成失败")
        p = self.repo.run_verify("run", "--group", "unit")
        self.assertEqual(p.returncode, 1, p.stdout + p.stderr)
        record = read_record(self.repo.artifact_dir, self.repo.latest_run_id())
        self.assertEqual(record["result"]["outcome"], "failed")
        self.assertEqual(record["result"]["exit_status"], 1)
        self.assertEqual(record["result"]["counts"]["failed"], 1)
        case = {item["id"]: item for item in record["groups"][0]["cases"]}["test_synth.AlphaTests.test_alpha_ok"]
        self.assertEqual(case["status"], "failed")
        self.assertEqual(case["name"], "test_alpha_ok")
        self.assertTrue(case["message"], "失败用例留下可读信息")
        log = (self.repo.artifact_dir / "runs" / self.repo.latest_run_id() / "unit.log").read_text(encoding="utf-8")
        self.assertIn("FAILED", log, "原始日志保留断言失败")
        self.assertEqual(record["evidence"]["integrity"], "ok")


UNIT_SLOW = '''import time
import unittest


class SlowTests(unittest.TestCase):
    def test_sleeps(self):
        time.sleep(30)
'''

UNIT_FIXTURE = '''import time
import unittest


def setUpModule():
    time.sleep(0.3)


class SlowFixtureTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        time.sleep(0.4)

    def test_one(self):
        self.assertTrue(True)

    def test_two(self):
        self.assertTrue(True)


class OtherFixtureTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        time.sleep(0.3)

    def test_three(self):
        self.assertTrue(True)
'''

UNIT_SUBTEST = '''import unittest


class SubTests(unittest.TestCase):
    def test_subtests(self):
        for index in range(3):
            with self.subTest(index=index):
                self.assertLess(index, 2)
'''

UNIT_SKIP = '''import unittest


class SkipTests(unittest.TestCase):
    @unittest.skip("合成跳过")
    def test_skipped(self):
        self.assertTrue(False)
'''

UNIT_IMPORT_ERROR = "raise ImportError('合成导入错误')\n"

UNIT_HARD_EXIT = '''import os
import unittest


class EscapeTests(unittest.TestCase):
    def test_leaves_without_reporting(self):
        os._exit(0)
'''

UNIT_CHANGES_INPUT = '''import pathlib
import time
import unittest


class ChurnTests(unittest.TestCase):
    def test_writes_source_while_running(self):
        path = pathlib.Path(__file__).resolve().parents[1] / "README.md"
        path.write_text(path.read_text(encoding="utf-8") + "运行期改动\\n", encoding="utf-8")
        time.sleep(1.0)
        self.assertTrue(True)
'''


def pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


class OutcomeTests(VerifyEntryTestCase):
    def outcome_of(self, *args: str, timeout: float = 120):
        p = self.repo.run_verify(*args, timeout=timeout)
        record = read_record(self.repo.artifact_dir, self.repo.latest_run_id())
        return p, record

    def test_import_error_is_reported_as_error_not_success(self):
        self.unit(UNIT_IMPORT_ERROR)
        self.repo.commit_all("合成导入错误")
        p, record = self.outcome_of("run", "--group", "unit")
        self.assertEqual(p.returncode, 1, p.stdout + p.stderr)
        self.assertEqual(record["result"]["outcome"], "error")
        self.assertEqual(record["result"]["counts"]["errors"], 1)
        self.assertEqual(record["result"]["counts"]["cases"], 1)
        self.assertFalse(record["result"]["usable_as_evidence"])

    def test_subtest_failures_are_counted_apart_from_cases(self):
        self.unit(UNIT_SUBTEST)
        self.repo.commit_all("合成子测试")
        p, record = self.outcome_of("run", "--group", "unit")
        self.assertEqual(p.returncode, 1, p.stdout + p.stderr)
        counts = record["result"]["counts"]
        self.assertEqual(counts["cases"], 1, "子测试失败不重复计成独立用例")
        self.assertEqual(counts["subtest_failures"], 1)
        self.assertEqual(counts["failed"], 1)
        self.assertEqual(record["result"]["outcome"], "failed")
        case = record["groups"][0]["cases"][0]
        self.assertEqual(case["subtest_failures"], 1)
        self.assertEqual(case["status"], "failed")

    def test_all_skipped_is_not_a_pass(self):
        self.unit(UNIT_SKIP)
        self.repo.commit_all("合成跳过")
        p, record = self.outcome_of("run", "--group", "unit")
        self.assertEqual(p.returncode, 1, p.stdout + p.stderr)
        self.assertEqual(record["result"]["outcome"], "skipped")
        counts = record["result"]["counts"]
        self.assertEqual((counts["skipped"], counts["cases"]), (1, 1))
        self.assertFalse(record["result"]["usable_as_evidence"])
        self.assertTrue(any("跳过" in note
                            for note in record["result"]["notes"] + [record["result"]["summary"]]))

    def test_empty_collection_is_not_a_pass(self):
        (self.repo.path / "tests" / "test_synth.py").unlink()
        self.repo.commit_all("合成空集合")
        p, record = self.outcome_of("run", "--group", "unit")
        self.assertEqual(p.returncode, 1, p.stdout + p.stderr)
        self.assertEqual(record["result"]["outcome"], "empty")
        self.assertEqual(record["result"]["counts"]["cases"], 0)
        self.assertFalse(record["result"]["usable_as_evidence"])

    def test_child_that_escapes_the_runner_is_not_a_pass(self):
        self.unit(UNIT_HARD_EXIT)
        self.repo.commit_all("合成硬退出")
        p, record = self.outcome_of("run", "--group", "unit")
        self.assertEqual(p.returncode, 1, p.stdout + p.stderr)
        self.assertEqual(record["result"]["outcome"], "error")
        self.assertIn("计时证据", record["groups"][0]["note"])
        self.assertFalse(record["result"]["usable_as_evidence"])

    def test_every_run_gets_a_unique_id(self):
        first, _ = self.outcome_of("run", "--group", "unit")
        second, _ = self.outcome_of("run", "--group", "unit")
        self.assertEqual((first.returncode, second.returncode), (0, 0))
        self.assertEqual(len(self.repo.run_ids()), 2)
        self.assertNotEqual(self.repo.run_ids()[0], self.repo.run_ids()[1])


class TimingAttributionTests(VerifyEntryTestCase):
    def test_class_and_module_overhead_is_not_charged_to_a_case(self):
        self.unit(UNIT_FIXTURE)
        self.repo.commit_all("合成 fixture 开销")
        p = self.repo.run_verify("run", "--group", "unit")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        run_id = self.repo.latest_run_id()
        record = read_record(self.repo.artifact_dir, run_id)
        timing = record["timing"]
        self.assertGreaterEqual(timing["fixture_seconds_total"], 0.95,
                                "setUpModule 与两个 setUpClass 的耗时都要有归属")
        for case in record["groups"][0]["cases"]:
            self.assertLess(case["seconds"], 0.2, "用例自身很快，fixture 时间不能摊到它头上")
        self.assertLess(timing["case_seconds_total"], timing["fixture_seconds_total"])
        sidecar = json.loads((self.repo.artifact_dir / "runs" / run_id
                              / "unit.cases.json").read_text(encoding="utf-8"))
        by_kind = sidecar["seconds"]["gap_by_kind"]
        self.assertGreaterEqual(by_kind.get("run-start", 0.0), 0.3, "模块级准备计入运行开始空隙")
        self.assertGreaterEqual(by_kind.get("class-boundary", 0.0), 0.3, "换类时的准备与清理计入边界空隙")
        self.assertLess(sidecar["seconds"]["unattributed"], 0.05, "同类相邻用例之间不应有大额未归属开销")
        self.assertAlmostEqual(sum(gap["seconds"] for gap in sidecar["gaps"]),
                               sidecar["seconds"]["gap_total"], places=5, msg="空隙耗时不重复累计")


class InputSnapshotTests(VerifyEntryTestCase):
    def test_input_changed_during_the_run_cannot_cover_the_final_input(self):
        self.unit(UNIT_CHANGES_INPUT)
        self.repo.commit_all("合成运行期改动")
        p = self.repo.run_verify("run", "--group", "unit")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        record = read_record(self.repo.artifact_dir, self.repo.latest_run_id())
        self.assertTrue(record["input"]["changed_during_run"])
        self.assertFalse(record["input"]["covers_final_input"])
        self.assertTrue(record["input"]["stale_reason"])
        self.assertNotEqual(record["input"]["fingerprint"], record["input"]["final_fingerprint"])
        self.assertFalse(record["result"]["usable_as_evidence"])
        report = self.repo.run_verify("report", "--run", record["run_id"])
        self.assertEqual(report.returncode, 1)
        self.assertIn("不能覆盖最终输入", report.stdout)

    def test_staged_unstaged_and_untracked_are_told_apart(self):
        self.repo.write("tests/test_extra.py", UNIT_PASS)
        self.repo.write("README.md", "# 合成仓库\n\n未暂存改动\n")
        self.repo.git("add", "tests/test_extra.py")
        p = self.repo.run_verify("run", "--group", "unit")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        record = read_record(self.repo.artifact_dir, self.repo.latest_run_id())
        at_start = record["input"]["at_start"]
        self.assertEqual(at_start["staged"], ["tests/test_extra.py"])
        self.assertEqual(at_start["unstaged"], ["README.md"])
        self.assertEqual(at_start["untracked"], [])
        self.assertEqual(at_start["counts"], {"staged": 1, "unstaged": 1, "untracked": 0})
        self.assertTrue(at_start["fingerprint"].startswith("sha256:"))
        self.assertEqual(record["input"]["changed_since_baseline"], ["README.md", "tests/test_extra.py"])


class ProcessLifecycleTests(VerifyEntryTestCase):
    def test_group_timeout_reaps_the_child_and_reports_timeout(self):
        self.unit(UNIT_SLOW)
        self.repo.commit_all("合成超时")
        p = self.repo.run_verify("run", "--group", "unit", "--timeout", "1")
        self.assertEqual(p.returncode, 124, p.stdout + p.stderr)
        run_id = self.repo.latest_run_id()
        record = read_record(self.repo.artifact_dir, run_id)
        self.assertEqual(record["result"]["outcome"], "timeout")
        self.assertTrue(record["groups"][0]["timed_out"])
        children = json.loads((self.repo.artifact_dir / "runs" / run_id
                               / "children.json").read_text(encoding="utf-8"))
        self.assertEqual(len(children), 1)
        self.assertTrue(children[0]["reaped"])
        deadline = time.time() + 5
        while time.time() < deadline and pid_alive(children[0]["pid"]):
            time.sleep(0.1)
        self.assertFalse(pid_alive(children[0]["pid"]), "超时后子进程被回收")

    def test_terminating_the_wrapper_reaps_only_its_own_children(self):
        self.unit(UNIT_SLOW)
        self.repo.commit_all("合成信号")
        env = dict(os.environ)
        env["PATH"] = "%s%s%s" % (self.repo.path / "bin", os.pathsep, env["PATH"])
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        wrapper = subprocess.Popen([sys.executable, "verify.py", "run", "--group", "unit"],
                                   cwd=str(self.repo.path), env=env,
                                   stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        run_id, children = self._wait_for_children()
        self.assertEqual(len(children), 1, "本次登记的子进程只有一个")
        child_pid = children[0]["pid"]
        decoy = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"])
        try:
            wrapper.send_signal(signal.SIGTERM)
            code = wrapper.wait(timeout=30)
            self.assertEqual(code, 143, "入口按 128+SIGTERM 退出")
            deadline = time.time() + 5
            while time.time() < deadline and pid_alive(child_pid):
                time.sleep(0.1)
            self.assertFalse(pid_alive(child_pid), "登记过的子进程被回收")
            self.assertTrue(pid_alive(decoy.pid), "没登记过的进程不动")
            record = read_record(self.repo.artifact_dir, run_id)
            self.assertEqual(record["state"], "aborted")
            self.assertEqual(record["result"]["outcome"], "interrupted")
            self.assertFalse(record["result"]["usable_as_evidence"])
            report = self.repo.run_verify("report", "--run", run_id)
            self.assertEqual(report.returncode, 1)
            self.assertIn("没有正常结束", report.stdout)
        finally:
            decoy.kill()
            decoy.wait()
            wrapper.stdout.close()

    def _wait_for_children(self, timeout: float = 20) -> tuple:
        """等入口把自己的子进程登记下来：latest.json 先写，children.json 后写。"""
        pointer = self.repo.artifact_dir / "latest.json"
        deadline = time.time() + timeout
        while time.time() < deadline:
            if pointer.is_file():
                try:
                    run_id = json.loads(pointer.read_text(encoding="utf-8"))["run_id"]
                except (OSError, ValueError, KeyError):
                    run_id = None
                children_path = self.repo.artifact_dir / "runs" / (run_id or "") / "children.json"
                if run_id and children_path.is_file():
                    try:
                        children = json.loads(children_path.read_text(encoding="utf-8"))
                    except (OSError, ValueError):
                        children = []
                    if children:
                        return run_id, children
            time.sleep(0.05)
        self.fail("入口没有登记子进程")


# 收尾段（结束快照 + 落最终记录）要长到足够在组执行结束后发一个信号进去：
# 未跟踪文件越多，结束快照的 git status 与逐文件哈希越慢。
FINALIZE_CHURN_FILES = 1500


class SignalDuringFinalizeTests(VerifyEntryTestCase):
    """R1/R2：信号落在收尾段或组与组之间时，入口仍落最终记录、按 128+信号退出，
    已完整执行的组保留真实结果，只有没执行的组标 interrupted。"""

    def start_wrapper(self, *args: str, extra_env: dict | None = None) -> subprocess.Popen:
        env = dict(os.environ)
        env["PATH"] = "%s%s%s" % (self.repo.path / "bin", os.pathsep, env["PATH"])
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        env.pop("AE_VERIFY_DIR", None)
        if extra_env:
            env.update(extra_env)
        return subprocess.Popen([sys.executable, "verify.py", *args], cwd=str(self.repo.path),
                                env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)

    def wait_latest_run(self, timeout: float = 30) -> str:
        pointer = self.repo.artifact_dir / "latest.json"
        deadline = time.time() + timeout
        while time.time() < deadline:
            if pointer.is_file():
                try:
                    return json.loads(pointer.read_text(encoding="utf-8"))["run_id"]
                except (OSError, ValueError, KeyError):
                    pass
            time.sleep(0.05)
        self.fail("入口没有写下 latest.json")

    def wait_children(self, run_id: str, count: int, timeout: float = 30) -> list:
        path = self.repo.artifact_dir / "runs" / run_id / "children.json"
        deadline = time.time() + timeout
        while time.time() < deadline:
            if path.is_file():
                try:
                    children = json.loads(path.read_text(encoding="utf-8"))
                except (OSError, ValueError):
                    children = []
                if len(children) >= count:
                    return children
            time.sleep(0.05)
        self.fail("入口没有登记 %d 个子进程" % count)

    def wait_all_reaped(self, run_id: str, timeout: float = 30) -> None:
        """等登记的子进程全部回收：组执行已结束，主进程正走在收尾段上。"""
        path = self.repo.artifact_dir / "runs" / run_id / "children.json"
        deadline = time.time() + timeout
        while time.time() < deadline:
            if path.is_file():
                try:
                    children = json.loads(path.read_text(encoding="utf-8"))
                except (OSError, ValueError):
                    children = []
                if children and all(entry["reaped"] and entry["exit_status"] is not None
                                    for entry in children):
                    return
            time.sleep(0.005)
        self.fail("子进程没有在时限内完成，没有出现收尾窗口")

    def index_entries(self) -> list:
        return [json.loads(line) for line in
                (self.repo.artifact_dir / "index.jsonl").read_text(encoding="utf-8").splitlines()]

    def test_signal_during_finalize_seals_aborted_record(self):
        churn = self.repo.path / "churn"
        churn.mkdir()
        for index in range(FINALIZE_CHURN_FILES):
            (churn / ("file-%04d.txt" % index)).write_text("churn %d\n" % index, encoding="utf-8")
        wrapper = self.start_wrapper("run", "--group", "unit")
        try:
            run_id = self.wait_latest_run()
            self.wait_all_reaped(run_id)
            wrapper.send_signal(signal.SIGTERM)  # 组已完成，信号落在收尾段
            code = wrapper.wait(timeout=30)
            output = wrapper.stdout.read()
        finally:
            wrapper.stdout.close()
        self.assertEqual(code, 143, "入口按 128+SIGTERM 退出\n" + output)
        self.assertNotIn("Traceback", output, "收尾段被打断也不留裸 traceback")
        record = read_record(self.repo.artifact_dir, run_id)
        self.assertEqual(record["state"], "aborted", "最终记录落成 aborted，不停在 running")
        self.assertEqual(record["groups"][0]["outcome"], "passed", "已完成的组保留真实结果")
        self.assertIn(run_id, [entry["run_id"] for entry in self.index_entries()],
                      "index.jsonl 记下这次运行")

    def test_interrupt_between_groups_keeps_completed_group_results(self):
        wrapper = self.start_wrapper("run", "--group", "all",
                                     extra_env={"SYNTH_BROWSER_SECONDS": "30"})
        try:
            run_id = self.wait_latest_run()
            children = self.wait_children(run_id, 3)
            self.assertEqual([entry["group"] for entry in children],
                             ["unit", "serve-check", "browser"])
            self.assertFalse(children[2]["reaped"], "browser 还在跑：前两组已完整执行")
            wrapper.send_signal(signal.SIGTERM)
            code = wrapper.wait(timeout=30)
            output = wrapper.stdout.read()
        finally:
            wrapper.stdout.close()
        self.assertEqual(code, 143, "入口按 128+SIGTERM 退出\n" + output)
        self.assertNotIn("Traceback", output)
        record = read_record(self.repo.artifact_dir, run_id)
        self.assertEqual(record["state"], "aborted")
        by_name = {group["name"]: group for group in record["groups"]}
        self.assertEqual(by_name["unit"]["outcome"], "passed", "第一组保留真实结果")
        self.assertEqual(by_name["unit"]["counts"]["cases"], 3)
        self.assertIsNotNone(by_name["unit"]["timing"].get("child_seconds"))
        self.assertEqual(by_name["serve-check"]["outcome"], "passed", "第二组保留真实结果")
        self.assertEqual(by_name["browser"]["outcome"], "interrupted", "未执行的组标 interrupted")
        self.assertIsNone(by_name["browser"]["counts"])
        self.assertIn("没有拿到执行结果", by_name["browser"]["note"])
        self.assertEqual(record["result"]["outcome"], "interrupted")
        self.assertIn(run_id, [entry["run_id"] for entry in self.index_entries()])


UNIT_MARKER = '''import os
import pathlib
import unittest


class MarkerTests(unittest.TestCase):
    def test_writes_marker(self):
        pathlib.Path(os.environ["SYNTH_MARKER"]).write_text("跑过了", encoding="utf-8")
        self.assertTrue(True)
'''


def absolute_strings(value, found=None):
    """递归找出记录里所有像本机绝对路径的字符串。"""
    found = [] if found is None else found
    if isinstance(value, str):
        if value.startswith("/") or value.startswith("~/"):
            found.append(value)
    elif isinstance(value, dict):
        for item in value.values():
            absolute_strings(item, found)
    elif isinstance(value, list):
        for item in value:
            absolute_strings(item, found)
    return found


class OtherGroupTests(VerifyEntryTestCase):
    def test_browser_group_times_as_a_whole_and_reserves_scenario_slots(self):
        p = self.repo.run_verify("run", "--group", "browser")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        record = read_record(self.repo.artifact_dir, self.repo.latest_run_id())
        group = record["groups"][0]
        self.assertEqual(group["name"], "browser")
        self.assertEqual(group["outcome"], "passed")
        self.assertEqual(group["granularity"], "group", "未拆场景前按整组计时")
        self.assertEqual(group["scenarios"], [], "记录里给场景级计时留了位置")
        self.assertEqual(group["counts"], {"total": 3, "passed": 3, "failed": 0})
        self.assertGreaterEqual(group["timing"]["child_seconds"], 0.2)
        log = (self.repo.artifact_dir / "runs" / self.repo.latest_run_id() / "browser.log").read_text(encoding="utf-8")
        self.assertIn("PASS  合成场景 0", log)
        self.assertEqual(record["result"]["counts"]["checks"], 3)

    def test_browser_failure_keeps_the_exit_status(self):
        p = self.repo.run_verify("run", "--group", "browser", extra_env={"SYNTH_BROWSER_FAILED": "1"})
        self.assertEqual(p.returncode, 1, p.stdout + p.stderr)
        record = read_record(self.repo.artifact_dir, self.repo.latest_run_id())
        self.assertEqual(record["groups"][0]["outcome"], "failed")
        self.assertEqual(record["groups"][0]["counts"]["failed"], 1)

    def test_browser_timeout_is_reported_as_timeout(self):
        p = self.repo.run_verify("run", "--group", "browser", "--timeout", "0.3",
                                 extra_env={"SYNTH_BROWSER_SECONDS": "8"})
        self.assertEqual(p.returncode, 124, p.stdout + p.stderr)
        record = read_record(self.repo.artifact_dir, self.repo.latest_run_id())
        self.assertEqual(record["result"]["outcome"], "timeout")

    def test_serve_check_group_reports_counts_from_its_summary(self):
        p = self.repo.run_verify("run", "--group", "serve-check")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        record = read_record(self.repo.artifact_dir, self.repo.latest_run_id())
        group = record["groups"][0]
        self.assertEqual(group["outcome"], "passed")
        self.assertEqual(group["counts"], {"total": 3, "passed": 3, "failed": 0})
        self.assertEqual(group["granularity"], "group")
        self.assertIsNone(group["scenarios"], "非浏览器组不留场景字段")

    def test_serve_check_failure_is_not_a_pass(self):
        p = self.repo.run_verify("run", "--group", "serve-check", extra_env={"SYNTH_SERVE_FAILED": "2"})
        self.assertEqual(p.returncode, 1, p.stdout + p.stderr)
        record = read_record(self.repo.artifact_dir, self.repo.latest_run_id())
        self.assertEqual(record["groups"][0]["outcome"], "failed")
        self.assertEqual(record["groups"][0]["counts"], {"total": 3, "passed": 1, "failed": 2})

    def test_serve_check_without_any_check_is_not_a_pass(self):
        p = self.repo.run_verify("run", "--group", "serve-check", extra_env={"SYNTH_SERVE_TOTAL": "0"})
        self.assertEqual(p.returncode, 1, p.stdout + p.stderr)
        record = read_record(self.repo.artifact_dir, self.repo.latest_run_id())
        self.assertEqual(record["groups"][0]["outcome"], "empty")

    def test_all_runs_the_three_groups_in_order_under_one_record(self):
        p = self.repo.run_verify("run", "--group", "all", "--phase", "pr", "--pr", "50")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        run_id = self.repo.latest_run_id()
        record = read_record(self.repo.artifact_dir, run_id)
        self.assertEqual([group["name"] for group in record["groups"]],
                         ["unit", "serve-check", "browser"])
        self.assertEqual(record["context"]["phase"], "pr")
        self.assertEqual(record["context"]["pr"], 50)
        self.assertEqual(record["result"]["counts"]["groups"], 3)
        self.assertEqual(record["result"]["counts"]["cases"], 3)
        self.assertEqual(record["result"]["counts"]["checks"], 6)
        self.assertTrue(record["result"]["usable_as_evidence"])
        for name in ("unit", "serve-check", "browser"):
            self.assertTrue((self.repo.artifact_dir / "runs" / run_id / ("%s.log" % name)).is_file())
        index = [json.loads(line) for line in
                 (self.repo.artifact_dir / "index.jsonl").read_text(encoding="utf-8").splitlines()]
        self.assertEqual(len(index), 1)
        self.assertEqual(index[0]["run_id"], run_id)
        self.assertEqual(index[0]["outcome"], "passed")


class RelatedUnitTests(VerifyEntryTestCase):
    def test_explicit_selector_runs_only_what_was_named(self):
        self.repo.write("tests/test_other.py", '''import unittest


class OtherTests(unittest.TestCase):
    def test_other(self):
        self.assertTrue(True)
''')
        self.repo.commit_all("合成两个模块")
        p = self.repo.run_verify("run", "unit-related", "tests/test_synth.py")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        record = read_record(self.repo.artifact_dir, self.repo.latest_run_id())
        self.assertEqual(record["groups"][0]["name"], "unit-related")
        ids = [case["id"] for case in record["groups"][0]["cases"]]
        self.assertEqual(sorted(ids), ["test_synth.AlphaTests.test_alpha_also_ok",
                                       "test_synth.AlphaTests.test_alpha_ok",
                                       "test_synth.BetaTests.test_beta_ok"])
        self.assertNotIn("test_other.OtherTests.test_other", ids)
        self.assertEqual(record["context"]["selectors"], ["tests/test_synth.py"])

    def test_selector_without_unit_related_group_is_rejected(self):
        p = self.repo.run_verify("run", "--group", "unit", "tests/test_synth.py")
        self.assertEqual(p.returncode, 2, p.stdout + p.stderr)
        self.assertIn("只能配合", p.stderr)

    def test_unit_related_without_selectors_is_rejected(self):
        p = self.repo.run_verify("run", "--group", "unit-related")
        self.assertEqual(p.returncode, 2, p.stdout + p.stderr)
        self.assertIn("空集合", p.stderr)
        self.assertFalse((self.repo.artifact_dir).exists(), "入口自身失败不留下运行记录")

    def test_unknown_group_is_rejected(self):
        p = self.repo.run_verify("run", "--group", "nope")
        self.assertEqual(p.returncode, 2, p.stdout + p.stderr)
        self.assertIn("未知的验证组", p.stderr)

    def test_unknown_phase_is_rejected(self):
        p = self.repo.run_verify("run", "--group", "unit", "--phase", "whatever")
        self.assertEqual(p.returncode, 2, p.stdout + p.stderr)
        self.assertIn("开发阶段", p.stderr)

    def test_unknown_selector_is_an_error_not_a_pass(self):
        p = self.repo.run_verify("run", "unit-related", "tests/test_missing.py")
        self.assertNotEqual(p.returncode, 0)
        record = read_record(self.repo.artifact_dir, self.repo.latest_run_id())
        self.assertEqual(record["groups"][0]["outcome"], "error")


class OutDirTests(VerifyEntryTestCase):
    """R3：相对 --out-dir 配合仓库外 cwd 时，主进程与子进程不能把证据写成分裂的两处。"""

    def test_relative_out_dir_from_outside_cwd_keeps_evidence_together(self):
        outside = Path(self.temp.name) / "outside"
        outside.mkdir()
        artifacts = outside / "evidence-out"
        p = self.repo.run_verify("run", "--group", "unit", "--out-dir", "evidence-out",
                                 cwd=outside)
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        run_id = json.loads((artifacts / "latest.json").read_text(encoding="utf-8"))["run_id"]
        record = read_record(artifacts, run_id)
        self.assertEqual(record["groups"][0]["outcome"], "passed", "通过的用例不被误判 error")
        self.assertEqual(record["evidence"]["integrity"], "ok")
        self.assertTrue((artifacts / "runs" / run_id / "unit.cases.json").is_file(),
                        "计时证据与主进程写在同一处")
        status = self.repo.git("status", "--porcelain").stdout
        self.assertEqual(status, "", "仓库工作树不留下未忽略的产物目录")
        report = self.repo.run_verify("report", "--latest", "--out-dir", "evidence-out", cwd=outside)
        self.assertEqual(report.returncode, 0, report.stdout + report.stderr)
        self.assertIn(str((artifacts / "runs" / run_id).resolve()), report.stdout,
                      "报告显示的记录目录与实际一致")


class WrapperFailureTests(VerifyEntryTestCase):
    def test_missing_dependency_is_reported_not_passed(self):
        p = self.repo.run_verify("run", "--group", "browser", extra_env={"PATH": "/usr/bin:/bin"})
        self.assertEqual(p.returncode, 2, p.stdout + p.stderr)
        record = read_record(self.repo.artifact_dir, self.repo.latest_run_id())
        self.assertEqual(record["groups"][0]["outcome"], "unavailable")
        self.assertIn("uv", record["groups"][0]["note"])
        self.assertFalse(record["result"]["usable_as_evidence"])

    def test_run_json_mode_prints_the_record_on_stdout(self):
        p = self.repo.run_verify("run", "--group", "unit", "--json")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        record = json.loads(p.stdout)
        self.assertEqual(record["schema"], "ae-verify-run/1")
        self.assertEqual(record["state"], "complete")
        self.assertEqual(record["result"]["outcome"], "passed")

    def test_record_keeps_no_local_absolute_paths(self):
        p = self.repo.run_verify("run", "--group", "all", "--issue", "50")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        record = read_record(self.repo.artifact_dir, self.repo.latest_run_id())
        self.assertEqual(absolute_strings(record), [], "记录里不落本机绝对路径")

    def test_missing_subcommand_is_a_wrapper_error(self):
        p = self.repo.run_verify()
        self.assertEqual(p.returncode, 2, p.stdout + p.stderr)
        self.assertIn("缺子命令", p.stderr)

    def test_unwritable_artifact_dir_is_a_wrapper_error_not_a_pass(self):
        blocker = Path(self.temp.name) / "blocker"
        blocker.write_text("x", encoding="utf-8")
        p = self.repo.run_verify("run", "--group", "unit", "--out-dir", str(blocker))
        self.assertEqual(p.returncode, 2, p.stdout + p.stderr)
        self.assertIn("写不了验证记录", p.stderr)

    def test_index_write_failure_is_reported_without_changing_the_verdict(self):
        first = self.repo.run_verify("run", "--group", "unit")
        self.assertEqual(first.returncode, 0, first.stdout + first.stderr)
        index = self.repo.artifact_dir / "index.jsonl"
        index.unlink()
        index.mkdir()
        p = self.repo.run_verify("run", "--group", "unit")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        record = read_record(self.repo.artifact_dir, self.repo.latest_run_id())
        self.assertEqual(record["result"]["outcome"], "passed")
        self.assertTrue(any("索引追加失败" in note for note in record["result"]["notes"]),
                        "元数据写入失败要写进记录，不静默")


class ReportTests(VerifyEntryTestCase):
    def prepare(self, *args: str, **kwargs):
        p = self.repo.run_verify(*args, **kwargs)
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        return read_record(self.repo.artifact_dir, self.repo.latest_run_id())

    def test_report_reads_the_record_without_running_tests(self):
        self.unit(UNIT_MARKER)
        self.repo.commit_all("合成标记")
        marker = Path(self.temp.name) / "marker.txt"
        record = self.prepare("run", "--group", "unit", extra_env={"SYNTH_MARKER": str(marker)})
        self.assertTrue(marker.is_file())
        marker.unlink()
        before = self.repo.run_ids()
        p = self.repo.run_verify("report", "--run", record["run_id"])
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertIn("完整成功", p.stdout)
        self.assertIn("可作为通过证据", p.stdout)
        self.assertIn("record.json", p.stdout)
        self.assertEqual(self.repo.run_ids(), before, "report 不再产生新的运行")
        self.assertFalse(marker.exists(), "report 不启动产品测试")

    def test_report_json_returns_the_raw_record(self):
        record = self.prepare("run", "--group", "unit")
        p = self.repo.run_verify("report", "--run", record["run_id"], "--json")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertEqual(json.loads(p.stdout)["run_id"], record["run_id"])

    def test_report_of_a_failed_run_is_not_a_success(self):
        self.unit(UNIT_PASS.replace("self.assertEqual(1 + 1, 2)", "self.assertEqual(1 + 1, 3)"))
        self.repo.commit_all("合成失败")
        p = self.repo.run_verify("run", "--group", "unit")
        self.assertEqual(p.returncode, 1)
        p = self.repo.run_verify("report", "--latest")
        self.assertEqual(p.returncode, 1, p.stdout + p.stderr)
        self.assertIn("不能作为通过证据", p.stdout)

    def test_corrupt_record_is_reported_as_unusable_evidence(self):
        record = self.prepare("run", "--group", "unit")
        path = self.repo.artifact_dir / "runs" / record["run_id"] / "record.json"
        path.write_text('{"schema": "ae-verify-run/1", "state": "compl', encoding="utf-8")
        p = self.repo.run_verify("report", "--run", record["run_id"])
        self.assertEqual(p.returncode, 1, p.stdout + p.stderr)
        self.assertIn("证据不可用", p.stderr)

    def test_unknown_schema_is_reported_as_unusable_evidence(self):
        record = self.prepare("run", "--group", "unit")
        path = self.repo.artifact_dir / "runs" / record["run_id"] / "record.json"
        payload = json.loads(path.read_text(encoding="utf-8"))
        payload["schema"] = "ae-verify-run/999"
        path.write_text(json.dumps(payload), encoding="utf-8")
        p = self.repo.run_verify("report", "--run", record["run_id"])
        self.assertEqual(p.returncode, 1, p.stdout + p.stderr)
        self.assertIn("不支持的记录 schema", p.stderr)

    def test_unfinished_run_is_reported_as_unusable_evidence(self):
        record = self.prepare("run", "--group", "unit")
        run_dir = self.repo.artifact_dir / "runs" / record["run_id"]
        (run_dir / "record.json").unlink()
        p = self.repo.run_verify("report", "--run", record["run_id"])
        self.assertEqual(p.returncode, 1, p.stdout + p.stderr)
        self.assertIn("没有留下结束记录", p.stderr)

    def test_tampered_log_breaks_the_evidence(self):
        record = self.prepare("run", "--group", "unit")
        log = self.repo.artifact_dir / "runs" / record["run_id"] / "unit.log"
        log.write_text(log.read_text(encoding="utf-8") + "事后补的一行\n", encoding="utf-8")
        p = self.repo.run_verify("report", "--run", record["run_id"])
        self.assertEqual(p.returncode, 1, p.stdout + p.stderr)
        self.assertIn("校验和不一致", p.stdout)

    def test_missing_log_breaks_the_evidence(self):
        record = self.prepare("run", "--group", "unit")
        (self.repo.artifact_dir / "runs" / record["run_id"] / "unit.log").unlink()
        p = self.repo.run_verify("report", "--run", record["run_id"])
        self.assertEqual(p.returncode, 1, p.stdout + p.stderr)
        self.assertIn("缺少日志", p.stdout)

    def test_report_without_any_record_is_not_a_success(self):
        p = self.repo.run_verify("report", "--latest")
        self.assertEqual(p.returncode, 1, p.stdout + p.stderr)
        self.assertIn("还没有可报告的验证记录", p.stderr)
