"""统一验证入口（verify.py）的外部行为测试：命令行、退出码、结构化记录与子进程生命周期。

每个用例都跑在合成仓库里（临时目录 + git init + 合成测试程序 + 合成 uv），
不碰真实仓库内容、不启动真实浏览器、不读取任何真实审阅材料。
只验证命令行边界与落盘证据，不绑定入口内部的模块拆分。对应 issue #50。
"""
from __future__ import annotations

import datetime
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

# 合成浏览器入口（场景化）：在「N/M 通过」后再打一行 AE-SCENARIOS 摘要，
# 与 tests/e2e_browser.py 的场景执行清单同一格式（issue #53）
BROWSER_SCENARIO_STUB = '''#!/usr/bin/env python3
import json
import os
import sys
import time

STATUS = os.environ.get("SYNTH_SCENARIO_STATUS", "passed")
if STATUS == "interrupted":
    # 中断形态：当前场景记 interrupted（保留已执行的检查），其后记 not_run，进程以 130 退出
    scenarios = [
        {"id": "skill-search", "status": "passed", "total": 2},
        {"id": "server-mode", "status": "interrupted", "total": 1},
        {"id": "css-fidelity", "status": "not_run", "total": 0},
    ]
else:
    scenarios = [
        {"id": "skill-search", "status": "passed", "total": 2},
        {"id": "server-mode", "status": STATUS, "total": 2},
    ]
records = []
passed_total = 0
for scenario in scenarios:
    started = time.time()
    time.sleep(0.05)
    failed = scenario["status"] == "failed"
    print("=== 场景 %s ===" % scenario["id"])
    for index in range(scenario["total"]):
        print("%s  合成检查 %s-%d" % ("FAIL" if failed else "PASS", scenario["id"], index))
    checks = {"total": scenario["total"],
              "passed": 0 if failed else scenario["total"],
              "failed": scenario["total"] if failed else 0}
    passed_total += checks["passed"]
    records.append({"id": scenario["id"], "status": scenario["status"], "checks": checks,
                    "started_at": "2026-10-31T00:00:00+00:00",
                    "ended_at": "2026-10-31T00:00:01+00:00",
                    "seconds": round(time.time() - started, 6)})
total = sum(item["checks"]["total"] for item in records)
print("\\n%d/%d 通过" % (passed_total, total))
print("AE-SCENARIOS: " + json.dumps({
    "mode": "partial", "source": "--only",
    "plan": [item["id"] for item in records], "scenarios": records}, ensure_ascii=False))
if STATUS == "interrupted":
    sys.exit(130)
sys.exit(0 if passed_total == total else 1)
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

# 合成版全局词典一致性契约：真实仓库里这一检查在 tests/test_i18n_catalog_contract.py，
# unit-related 组固定附带它（issue #51）。合成版检查合成仓库自己的 locales/。
CONTRACT_MODULE = '''import json
import unittest
from pathlib import Path

LOCALES = Path(__file__).resolve().parents[1] / "locales"


class RepoCatalogContractTests(unittest.TestCase):
    def test_shipped_catalogs_stay_consistent(self):
        zh = json.loads((LOCALES / "zh-CN.json").read_text(encoding="utf-8"))
        en = json.loads((LOCALES / "en.json").read_text(encoding="utf-8"))
        missing = sorted(set(zh) - set(en)) + sorted(set(en) - set(zh))
        self.assertEqual(missing, [], "合成词典 key 必须一致")
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
        self.write("tests/test_i18n_catalog_contract.py", CONTRACT_MODULE)
        self.write("locales/zh-CN.json", '{"a": "甲"}\n')
        self.write("locales/en.json", '{"a": "A"}\n')
        self.write("tests/e2e_browser.py", BROWSER_STUB, executable=True)
        self.write("skill/annotation-everything/scripts/serve.py", SERVE_STUB, executable=True)
        shutil.copy2(VERIFY, self.path / "verify.py")
        shutil.copy2(ROOT / "host_isolation.py", self.path / "host_isolation.py")
        shutil.copy2(ROOT / "verify_selection.py", self.path / "verify_selection.py")
        shutil.copy2(ROOT / "verify_evidence.py", self.path / "verify_evidence.py")
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
        # 合成仓库的隔离锁环境：宿主进程可能正持有真实资源锁（豁免变量会让合成入口
        # 全部借入、锁语义测不到），真实锁目录也可能被同机其他验证占用——测试行为
        # 不能依赖它们。AE_LOCK_DIR 必须无条件覆盖继承值：外层若已设它（且那里
        # host-state 正被占用），嵌套的合成 verify.py 会与宿主互等满超时上限。
        # IsolationLockTests 用 extra_env 指定自己的锁目录（extra_env 仍优先）。
        env.pop("AE_HELD_LOCKS", None)
        env["AE_LOCK_DIR"] = str(self.test_lock_dir)
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
        # 本用例专属的隔离锁目录：合成入口的锁行为与宿主进程、同机其他验证互不相干
        lock_temp = tempfile.TemporaryDirectory(prefix="verify-entry-lock-")
        self.addCleanup(lock_temp.cleanup)
        self.test_lock_dir = Path(lock_temp.name)
        self.repo = SynthRepo(Path(self.temp.name))
        self.repo.test_lock_dir = self.test_lock_dir

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

    def test_plan_pops_the_leading_group_name_out_of_selectors(self):
        """R3：plan 与 run 一致，首个位置参数是组名时当组名用，不混进选择器展示。"""
        p = self.repo.run_verify("plan", "unit-related", "tests/test_synth.py", "--json")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        plan = json.loads(p.stdout)
        self.assertEqual(plan["requested"], ["unit-related"], "组名要弹出到组请求里")
        unit_related = {group["name"]: group for group in plan["groups"]}["unit-related"]
        self.assertNotIn("unit-related", unit_related["command"],
                         "组名不是选择器，不得出现在实际命令里")
        self.assertNotIn("unit-related", unit_related["equivalent"],
                         "组名不得出现在等价命令里")
        text = self.repo.run_verify("plan", "unit-related", "tests/test_synth.py")
        self.assertEqual(text.returncode, 0, text.stdout + text.stderr)
        self.assertIn("python3 -m unittest tests/test_synth.py", text.stdout)
        self.assertNotIn("unittest unit-related", text.stdout, "组名不进等价命令")

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
        self.assertEqual(counts["cases"], 4)  # 3 个合成用例 + 自动发现的契约用例
        self.assertEqual(counts["passed"], 4)
        self.assertEqual(counts["failed"], 0)
        self.assertEqual(counts["skipped"], 0)
        group = record["groups"][0]
        self.assertEqual(group["name"], "unit")
        self.assertEqual(group["outcome"], "passed")
        self.assertEqual(len(group["cases"]), 4, "每个用例一条记录")
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
        self.assertEqual(record["result"]["counts"]["cases"], 2)  # 导入错误 + 契约用例
        self.assertFalse(record["result"]["usable_as_evidence"])

    def test_subtest_failures_are_counted_apart_from_cases(self):
        self.unit(UNIT_SUBTEST)
        self.repo.commit_all("合成子测试")
        p, record = self.outcome_of("run", "--group", "unit")
        self.assertEqual(p.returncode, 1, p.stdout + p.stderr)
        counts = record["result"]["counts"]
        self.assertEqual(counts["cases"], 2, "子测试失败不重复计成独立用例")
        self.assertEqual(counts["subtest_failures"], 1)
        self.assertEqual(counts["failed"], 1)
        self.assertEqual(record["result"]["outcome"], "failed")
        case = {item["id"]: item for item in record["groups"][0]["cases"]}[
            "test_synth.SubTests.test_subtests"]
        self.assertEqual(case["subtest_failures"], 1)
        self.assertEqual(case["status"], "failed")

    def test_all_skipped_is_not_a_pass(self):
        self.unit(UNIT_SKIP)
        # 契约模块也要整个跳过，套件才是「全部跳过」
        self.repo.write("tests/test_i18n_catalog_contract.py", UNIT_SKIP)
        self.repo.commit_all("合成跳过")
        p, record = self.outcome_of("run", "--group", "unit")
        self.assertEqual(p.returncode, 1, p.stdout + p.stderr)
        self.assertEqual(record["result"]["outcome"], "skipped")
        counts = record["result"]["counts"]
        self.assertEqual((counts["skipped"], counts["cases"]), (2, 2))
        self.assertFalse(record["result"]["usable_as_evidence"])
        self.assertTrue(any("跳过" in note
                            for note in record["result"]["notes"] + [record["result"]["summary"]]))

    def test_empty_collection_is_not_a_pass(self):
        (self.repo.path / "tests" / "test_synth.py").unlink()
        (self.repo.path / "tests" / "test_i18n_catalog_contract.py").unlink()
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
        # 契约模块按字母序先被发现，test_synth 的模块级准备落在「运行开始」或
        # 「模块边界」空隙里；两种空隙合计都要覆盖 setUpModule 的 0.3 秒
        self.assertGreaterEqual(
            by_kind.get("run-start", 0.0) + by_kind.get("class-boundary", 0.0), 0.3,
            "模块级准备计入运行开始或模块边界空隙")
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
    def test_repeated_abort_preserves_time_spent_reaping_children(self):
        import verify

        ctx = verify.RunContext("cleanup-probe", self.repo.artifact_dir,
                                None, sys.stdout, True)
        ctx.run_dir.mkdir(parents=True)
        child = subprocess.Popen([sys.executable, "-c", '''import signal, sys, time
def cleanup(signum, frame):
    time.sleep(0.2)
    sys.exit(128 + signum)
signal.signal(signal.SIGTERM, cleanup)
print("ready", flush=True)
time.sleep(30)
'''], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                                 start_new_session=True)
        self.addCleanup(_reap, child)
        self.assertEqual(child.stdout.readline().strip(), "ready")
        ctx.registry.register(child, "synthetic")
        ctx.abort(signal.SIGTERM)
        first_cleanup = ctx.cleanup_seconds
        self.assertGreaterEqual(first_cleanup, 0.15, "第一次回收包含子进程的实际清理")
        self.assertEqual(child.returncode, 143)
        ctx.abort(signal.SIGTERM)  # 外层收尾再次调用：已经没有活跃子进程
        self.assertGreaterEqual(ctx.cleanup_seconds, first_cleanup,
                                "第二次空回收不得抹去已经发生的清理耗时")

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
        env.pop("AE_HELD_LOCKS", None)
        env["AE_LOCK_DIR"] = str(self.test_lock_dir)
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


# 外部锁占用者：扮演另一检出里直跑 unittest / 浏览器入口的参与者（同样遵守协议），
# 或一个只持锁的合成占用者。绝不被入口停止。
LOCK_HOLDER = '''
import sys, time
sys.path.insert(0, {root!r})
import host_isolation as hi
handle = hi.acquire([{resource!r}], timeout=30)
print("held", flush=True)
time.sleep({hold!r})
hi.release(handle)
'''


class IsolationLockTests(VerifyEntryTestCase):
    """issue #52：资源敏感组经 verify.py 与其他参与者共用同一隔离协议。

    锁等待单独计时、超时是明确非成功、纯逻辑组不被阻塞、并发敏感组串行且无假失败。
    """

    def setUp(self):
        super().setUp()
        lock_dir = tempfile.TemporaryDirectory(prefix="verify-lock-test-")
        self.addCleanup(lock_dir.cleanup)
        self.lock_root = Path(lock_dir.name)

    def lock_env(self, **extra) -> dict:
        env = {"AE_LOCK_DIR": str(self.lock_root)}
        env.update(extra)
        return env

    def start_holder(self, hold: float, resource: str = "host-state") -> subprocess.Popen:
        """合成占用者：持锁 sleep 后自行退出。放在仓库外的临时目录，不污染输入指纹。"""
        holder_dir = Path(tempfile.mkdtemp(prefix="verify-holder-"))
        self.addCleanup(shutil.rmtree, holder_dir, ignore_errors=True)
        script = holder_dir / "holder.py"
        script.write_text(LOCK_HOLDER.format(root=str(ROOT), resource=resource, hold=hold),
                          encoding="utf-8")
        proc = subprocess.Popen([sys.executable, str(script)], env=self.lock_env(),
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        self.addCleanup(_reap, proc)
        self.assertEqual(proc.stdout.readline().strip(), "held", "占用者没拿到锁")
        return proc

    def test_plan_declares_isolation_resources_per_group(self):
        p = self.repo.run_verify("plan", "--json")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        plan = json.loads(p.stdout)
        isolation = plan["isolation"]
        self.assertIn("host-state", isolation["resources"])
        self.assertEqual(isolation["resources"]["host-state"]["scope"], "machine-user")
        resources = {group["name"]: group["resources"] for group in plan["groups"]}
        self.assertEqual(resources["unit"], ["host-state"])
        self.assertEqual(resources["browser"], ["host-state"])
        self.assertEqual(resources["serve-check"], [], "纯静态自检不占用宿主机资源")
        text = self.repo.run_verify("plan").stdout
        self.assertIn("隔离", text, "文本计划要说明互斥与独立资源")

    def test_sensitive_group_waits_for_external_holder_and_records_wait(self):
        holder = self.start_holder(hold=0.8)
        p = self.repo.run_verify("run", "--group", "browser",
                                 extra_env=self.lock_env())
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        record = read_record(self.repo.artifact_dir, self.repo.latest_run_id())
        group = record["groups"][0]
        self.assertEqual(group["outcome"], "passed")
        self.assertEqual(group["lock"]["resources"], ["host-state"])
        self.assertGreaterEqual(group["lock"]["wait_seconds"], 0.4, "锁等待被单独记录")
        self.assertGreaterEqual(record["timing"]["lock_wait_seconds"], 0.4)
        # 锁等待不摊进入口自身开销，也不摊进组执行耗时
        self.assertLess(record["timing"]["wrapper_seconds"],
                        record["timing"]["wall_seconds"] - 0.4,
                        "锁等待不应计入 wrapper_seconds")
        self.assertLess(group["timing"]["child_seconds"], 0.4 + 0.2,
                        "锁等待不应计入组执行耗时")
        self.assertTrue(record["environment"]["isolation"]["mechanism"])
        self.assertIn("锁等待", p.stdout, "报告文本要呈现锁等待")
        self.assertEqual(holder.wait(timeout=15), 0)

    def test_lock_wait_timeout_is_failure_and_external_holder_survives(self):
        holder = self.start_holder(hold=2.0)
        p = self.repo.run_verify("run", "--group", "browser",
                                 extra_env=self.lock_env(AE_LOCK_WAIT_SECONDS="0.3"))
        self.assertEqual(p.returncode, 124, p.stdout + p.stderr)
        record = read_record(self.repo.artifact_dir, self.repo.latest_run_id())
        group = record["groups"][0]
        self.assertEqual(group["outcome"], "timeout", "锁等待超时不是通过")
        self.assertIn("锁", group["note"])
        self.assertNotIn("}", group["note"], "超时 note 里不得有格式串杂散字符")
        self.assertIn("超过 0.3 秒", group["note"], "note 应写明等待的资源与上限")
        self.assertGreaterEqual(record["timing"]["lock_wait_seconds"], 0.25)
        self.assertFalse(record["result"]["usable_as_evidence"])
        self.assertEqual(record["result"]["counts"]["groups"], 1)
        self.assertIsNone(group["started_at"], "锁等待超时没有启动本组子进程")
        self.assertEqual(record["shadow"]["executed_groups"], [])
        self.assertFalse(record["shadow"]["coverage"]["browser"])
        self.assertIsNone(holder.poll(), "等待超时绝不停止外部占用者")
        self.assertEqual(holder.wait(timeout=15), 0, "占用者按自己的节奏退出")

    def test_timeout_message_uses_limit_while_record_keeps_actual_wait(self):
        """调度延迟使实际等待越过上限时，提示上限与耗时记录不能混为一谈。"""
        module = self.repo.path / "host_isolation.py"
        module.write_text(module.read_text(encoding="utf-8") + '''
def acquire(resources, timeout=None, env=None):
    raise LockTimeoutError("host-state", 0.45)
''', encoding="utf-8")
        p = self.repo.run_verify("run", "--group", "browser",
                                 extra_env=self.lock_env(AE_LOCK_WAIT_SECONDS="0.3"))
        self.assertEqual(p.returncode, 124, p.stdout + p.stderr)
        record = read_record(self.repo.artifact_dir, self.repo.latest_run_id())
        group = record["groups"][0]
        self.assertIn("超过 0.3 秒", group["note"])
        self.assertEqual(group["lock"]["wait_seconds"], 0.45)

    def test_pure_logic_group_not_blocked_by_unrelated_lock(self):
        holder = self.start_holder(hold=1.5)
        started = time.monotonic()
        p = self.repo.run_verify("run", "unit-related", "tests/test_synth.py",
                                 extra_env=self.lock_env())
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertLess(time.monotonic() - started, 1.4, "纯逻辑组被无关资源锁阻塞")
        record = read_record(self.repo.artifact_dir, self.repo.latest_run_id())
        group = record["groups"][0]
        self.assertEqual(group["lock"]["resources"], [])
        self.assertEqual(record["timing"]["lock_wait_seconds"], 0.0)
        self.assertIsNone(holder.poll())

    def start_wrapper(self, group="browser") -> subprocess.Popen:
        env = dict(os.environ)
        env["PATH"] = "%s%s%s" % (self.repo.path / "bin", os.pathsep, env["PATH"])
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        env.pop("AE_HELD_LOCKS", None)
        env.update(self.lock_env())
        proc = subprocess.Popen(
            [sys.executable, "verify.py", "run", "--group", group, "--quiet"],
            cwd=str(self.repo.path), env=env, stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT, text=True)
        self.addCleanup(_reap, proc)
        return proc

    def wait_for_marker(self, path: Path) -> None:
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            if path.is_file():
                return
            time.sleep(0.01)
        self.fail("没有等到合成进程握手：%s" % path.name)

    def test_cancelled_lock_wait_keeps_elapsed_wait_and_does_not_start_group(self):
        holder = self.start_holder(hold=30)
        wrapper = self.start_wrapper(group="unit")
        self.wait_for_marker(self.repo.artifact_dir / "latest.json")
        # 外部占用者一直持锁，确保取消落在等待阶段，而不是产品执行阶段。
        time.sleep(0.3)
        wrapper.send_signal(signal.SIGTERM)
        output, _ = wrapper.communicate(timeout=15)
        self.assertEqual(wrapper.returncode, 143, output)
        run_id = self.repo.latest_run_id()
        record = read_record(self.repo.artifact_dir, run_id)
        group = record["groups"][0]
        self.assertEqual(record["state"], "aborted")
        self.assertEqual(group["outcome"], "interrupted")
        self.assertIn("等待共享资源 host-state", group["note"])
        self.assertIn("本组没有执行", group["note"])
        self.assertGreaterEqual(group["lock"]["wait_seconds"], 0.2)
        self.assertEqual(record["timing"]["lock_wait_seconds"], group["lock"]["wait_seconds"])
        self.assertLess(record["timing"]["wrapper_seconds"], record["timing"]["wall_seconds"] - 0.2)
        self.assertFalse((self.repo.artifact_dir / "runs" / run_id / "unit.log").exists())
        self.assertIsNone(group["started_at"])
        self.assertEqual(record["shadow"]["executed_groups"], [])
        self.assertFalse(record["shadow"]["coverage"]["unit"])
        self.assertIsNone(holder.poll(), "等待者取消不得停止占用者")

    def test_interrupted_group_holds_lock_until_child_cleanup_finishes(self):
        import host_isolation as hi

        started = self.lock_root / "child-started"
        cleaning = self.lock_root / "child-cleaning"
        finished = self.lock_root / "allow-child-exit"
        self.repo.write("tests/e2e_browser.py", '''import signal, sys, time
from pathlib import Path

def cleanup(signum, frame):
    Path(%r).write_text("cleaning", encoding="utf-8")
    while not Path(%r).exists():
        time.sleep(0.01)
    print("1/1 通过", flush=True)
    sys.exit(128 + signum)

signal.signal(signal.SIGTERM, cleanup)
Path(%r).write_text("started", encoding="utf-8")
time.sleep(30)
''' % (str(cleaning), str(finished), str(started)))
        wrapper = self.start_wrapper()
        self.wait_for_marker(started)
        wrapper.send_signal(signal.SIGTERM)
        try:
            self.wait_for_marker(cleaning)
            with self.assertRaises(hi.LockTimeoutError,
                                   msg="旧组仍在清理时不得让后继进入临界区"):
                handle = hi.acquire(["host-state"], timeout=0.2, env=self.lock_env())
                hi.release(handle)
        finally:
            finished.write_text("exit", encoding="utf-8")
            output, _ = wrapper.communicate(timeout=15)
        self.assertEqual(wrapper.returncode, 143, output)
        handle = hi.acquire(["host-state"], timeout=0.2, env=self.lock_env())
        hi.release(handle)
        record = read_record(self.repo.artifact_dir, self.repo.latest_run_id())
        self.assertEqual(record["groups"][0]["outcome"], "interrupted")

    def test_concurrent_sensitive_groups_serialize_without_false_failure(self):
        env = dict(os.environ)
        env["PATH"] = "%s%s%s" % (self.repo.path / "bin", os.pathsep, env["PATH"])
        env["GIT_CONFIG_GLOBAL"] = os.devnull
        env["GIT_CONFIG_SYSTEM"] = os.devnull
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        env.pop("AE_HELD_LOCKS", None)
        env.update(self.lock_env(SYNTH_BROWSER_SECONDS="1.0"))
        command = [sys.executable, "verify.py", "run", "--group", "browser", "--quiet"]
        first = subprocess.Popen(command, cwd=str(self.repo.path), env=env,
                                 stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        self.addCleanup(_reap, first)
        second = subprocess.Popen(command, cwd=str(self.repo.path), env=env,
                                  stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        self.addCleanup(_reap, second)
        self.assertEqual(first.wait(timeout=60), 0, "并发敏感组出现假失败（第一个）")
        self.assertEqual(second.wait(timeout=60), 0, "并发敏感组出现假失败（第二个）")
        records = [read_record(self.repo.artifact_dir, run_id) for run_id in self.repo.run_ids()]
        waits = [group["lock"]["wait_seconds"] for record in records for group in record["groups"]]
        self.assertGreaterEqual(max(waits), 0.3, "后到者应当等待先到者释放 host-state")

    def test_killed_wrapper_keeps_lock_in_running_group_until_it_exits(self):
        import host_isolation as hi

        started = self.lock_root / "surviving-child"
        finished = self.lock_root / "allow-surviving-child-exit"
        self.repo.write("tests/e2e_browser.py", '''import os, time
from pathlib import Path

Path(%r).write_text(str(os.getpid()), encoding="utf-8")
while not Path(%r).exists():
    time.sleep(0.01)
''' % (str(started), str(finished)))
        wrapper = self.start_wrapper()
        self.wait_for_marker(started)
        child_pid = int(started.read_text(encoding="utf-8"))
        wrapper.kill()
        wrapper.wait(timeout=15)
        try:
            self.assertTrue(pid_alive(child_pid), "合成组仍运行，才能覆盖入口先退出的时序")
            with self.assertRaises(hi.LockTimeoutError,
                                   msg="入口被杀时，仍运行的组必须继续持有锁"):
                handle = hi.acquire(["host-state"], timeout=0.2, env=self.lock_env())
                hi.release(handle)
        finally:
            finished.write_text("exit", encoding="utf-8")
            deadline = time.monotonic() + 5
            while pid_alive(child_pid) and time.monotonic() < deadline:
                time.sleep(0.01)
            if pid_alive(child_pid):
                os.kill(child_pid, signal.SIGKILL)
        handle = hi.acquire(["host-state"], timeout=2, env=self.lock_env())
        hi.release(handle)

    def test_unit_group_holds_lock_while_its_children_run(self):
        """unit 组持锁期间子流程正常运行：组级持锁与子进程执行不互相阻塞。"""
        p = self.repo.run_verify("run", "--group", "unit", extra_env=self.lock_env())
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        record = read_record(self.repo.artifact_dir, self.repo.latest_run_id())
        group = record["groups"][0]
        self.assertEqual(group["lock"]["resources"], ["host-state"])
        self.assertEqual(group["outcome"], "passed")

    def test_detour_relative_selector_still_maps_to_sensitive_module(self):
        """从仓库外 cwd 以 ../<repo>/tests/... 绕行相对路径指定敏感模块，资源判定不落空。"""
        self.repo.write("tests/test_ae_cli.py", UNIT_PASS)  # 名字命中敏感清单
        self.repo.commit_all("补敏感名字的合成模块")
        outside = self.repo.path.parent / "outside-cwd"
        outside.mkdir()
        self.addCleanup(shutil.rmtree, outside, ignore_errors=True)
        detour = os.path.relpath(self.repo.path / "tests" / "test_ae_cli.py", outside)
        self.assertTrue(detour.startswith(".."), "绕行路径应含 .. 段：%s" % detour)
        plan = self.repo.run_verify("plan", "--json", "unit-related", detour, cwd=outside)
        self.assertEqual(plan.returncode, 0, plan.stdout + plan.stderr)
        planned = {group["name"]: group["resources"]
                   for group in json.loads(plan.stdout)["groups"]}
        self.assertEqual(planned["unit-related"], ["host-state"],
                         "plan 阶段绕行相对路径没有归一到敏感模块")
        p = self.repo.run_verify("run", "unit-related", detour,
                                 cwd=outside, extra_env=self.lock_env())
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        record = read_record(self.repo.artifact_dir, self.repo.latest_run_id())
        group = record["groups"][0]
        self.assertEqual(group["lock"]["resources"], ["host-state"],
                         "绕行相对路径的选择器让组级资源判定落空（R5 回归）")
        self.assertEqual(group["outcome"], "passed")

    def test_inherited_outer_lock_dir_does_not_make_synth_repo_wait(self):
        """外层环境带着别人的 AE_LOCK_DIR（那里 host-state 被占住）时不得被继承。

        run_env 若只 setdefault，合成入口会沿用外层锁目录，与外部占用者互等
        满超时上限（600 秒/组）；必须无条件覆盖成本用例专属目录。
        """
        hostile_dir = Path(tempfile.mkdtemp(prefix="verify-hostile-lock-"))
        self.addCleanup(shutil.rmtree, hostile_dir, ignore_errors=True)
        script = hostile_dir / "hostile-holder.py"
        script.write_text(LOCK_HOLDER.format(root=str(ROOT), resource="host-state", hold=45),
                          encoding="utf-8")
        holder = subprocess.Popen([sys.executable, str(script)],
                                  env={"AE_LOCK_DIR": str(hostile_dir)},
                                  stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        self.addCleanup(_reap, holder)
        self.assertEqual(holder.stdout.readline().strip(), "held", "占用者没拿到锁")
        inherited = os.environ.get("AE_LOCK_DIR")
        try:
            os.environ["AE_LOCK_DIR"] = str(hostile_dir)
            started = time.monotonic()
            p = self.repo.run_verify("run", "--group", "unit", timeout=90)
        finally:
            if inherited is None:
                os.environ.pop("AE_LOCK_DIR", None)
            else:
                os.environ["AE_LOCK_DIR"] = inherited
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertLess(time.monotonic() - started, 30,
                        "合成入口继承了外层 AE_LOCK_DIR，与外部占用者互等")
        record = read_record(self.repo.artifact_dir, self.repo.latest_run_id())
        self.assertEqual(record["groups"][0]["lock"]["resources"], ["host-state"])


def _reap(proc: subprocess.Popen) -> None:
    if proc.poll() is None:
        proc.kill()
    try:
        proc.communicate(timeout=10)
    except (subprocess.TimeoutExpired, OSError, ValueError):
        pass


class SignalDuringFinalizeTests(VerifyEntryTestCase):
    """R1/R2：信号落在收尾段或组与组之间时，入口仍落最终记录、按 128+信号退出，
    已完整执行的组保留真实结果，只有没执行的组标 interrupted。"""

    def start_wrapper(self, *args: str, extra_env: dict | None = None,
                      launcher: str | None = None) -> subprocess.Popen:
        env = dict(os.environ)
        env["PATH"] = "%s%s%s" % (self.repo.path / "bin", os.pathsep, env["PATH"])
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        env.pop("AE_VERIFY_DIR", None)
        env.pop("AE_HELD_LOCKS", None)
        env.setdefault("AE_LOCK_DIR", str(self.test_lock_dir))
        if extra_env:
            env.update(extra_env)
        command = [sys.executable, "-c", launcher] if launcher else [sys.executable, "verify.py"]
        return subprocess.Popen([*command, *args], cwd=str(self.repo.path),
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

    def index_entries(self) -> list:
        return [json.loads(line) for line in
                (self.repo.artifact_dir / "index.jsonl").read_text(encoding="utf-8").splitlines()]

    def test_signal_during_finalize_seals_aborted_record(self):
        marker = Path(self.temp.name) / "finalize-ready"
        # reaped 只说明子进程结束，尚可能处于组证据收尾。明确在第二次输入快照
        # （整次运行收尾）握手，不用创建大量文件来赌不同平台上的耗时窗口。
        launcher = '''import os
import signal
import sys
import time
from pathlib import Path
import verify

original_snapshot = verify.snapshot_input
snapshot_calls = 0
def snapshot_with_barrier():
    global snapshot_calls
    snapshot_calls += 1
    if snapshot_calls == 2:
        assert callable(signal.getsignal(signal.SIGTERM))
        Path(os.environ["SYNTH_FINALIZE_READY"]).write_text("ready")
        time.sleep(30)
    return original_snapshot()

verify.snapshot_input = snapshot_with_barrier
sys.exit(verify.main())
'''
        wrapper = self.start_wrapper("run", "--group", "unit", launcher=launcher,
                                     extra_env={"SYNTH_FINALIZE_READY": str(marker)})
        self.addCleanup(_reap, wrapper)
        try:
            run_id = self.wait_latest_run()
            deadline = time.monotonic() + 10
            while not marker.exists() and wrapper.poll() is None and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertTrue(marker.exists(), "入口已进入最终输入快照，等待取消信号")
            wrapper.send_signal(signal.SIGTERM)  # 组已完成，信号落在收尾段
            output, _ = wrapper.communicate(timeout=15)
            code = wrapper.returncode
        finally:
            wrapper.stdout.close()
        self.assertEqual(code, 143, "入口按 128+SIGTERM 退出\n" + output)
        self.assertNotIn("Traceback", output, "收尾段被打断也不留裸 traceback")
        record = read_record(self.repo.artifact_dir, run_id)
        self.assertEqual(record["state"], "aborted", "最终记录落成 aborted，不停在 running")
        self.assertEqual(record["result"]["outcome"], "interrupted")
        self.assertEqual(record["result"]["exit_status"], 143)
        self.assertFalse(record["result"]["usable_as_evidence"])
        self.assertEqual(record["groups"][0]["outcome"], "passed", "已完成的组保留真实结果")
        self.assertEqual(record["shadow"]["run_outcome"], "interrupted",
                         "收尾中断时影子结论必须与整次运行一致")
        self.assertFalse(record["shadow"]["usable_as_evidence"])
        self.assertIn(run_id, [entry["run_id"] for entry in self.index_entries()],
                      "index.jsonl 记下这次运行")

    def test_first_signal_during_group_evidence_is_captured_not_ignored(self):
        """子进程已结束但组证据尚未交回时，首次取消不能被二次信号保护吞掉。"""
        wrapper_path = self.repo.path / "verify.py"
        source = wrapper_path.read_text(encoding="utf-8")
        boundary = "cleanup_guard.enter_context(capture_termination(result))"
        self.assertIn(boundary, source)
        source = source.replace(boundary, boundary + "\n"
                                "                os.kill(os.getpid(), signal.SIGTERM)", 1)
        wrapper_path.write_text(source, encoding="utf-8")
        self.repo.commit_all("合成组证据收尾首次取消")
        p = self.repo.run_verify("run", "--group", "unit", "--group", "serve-check")
        self.assertEqual(p.returncode, 143, p.stdout + p.stderr)
        run_id = self.repo.latest_run_id()
        record = read_record(self.repo.artifact_dir, run_id)
        self.assertEqual(record["state"], "aborted")
        self.assertEqual(record["result"]["outcome"], "interrupted")
        self.assertFalse(record["result"]["usable_as_evidence"])
        group = record["groups"][0]
        self.assertEqual(group["outcome"], "passed", "已完成组保留真实结果")
        self.assertEqual(group["counts"]["passed"], 4)
        self.assertEqual(record["groups"][1]["outcome"], "interrupted")
        self.assertIsNotNone(group["started_at"], "已执行的组有进程登记时间")
        self.assertIsNone(record["groups"][1]["started_at"], "取消后的计划组未启动")
        self.assertEqual(record["shadow"]["executed_groups"], ["unit"])
        self.assertEqual(record["shadow"]["coverage"],
                         {"unit": True, "serve-check": False, "browser": False})
        children = json.loads((self.repo.artifact_dir / "runs" / run_id / "children.json").read_text())
        self.assertEqual(len(children), 1, "首次取消后不再启动后续组")
        self.assertTrue(children[0]["reaped"])

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
        self.assertEqual(by_name["unit"]["counts"]["cases"], 4)
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
        self.assertEqual(record["result"]["counts"]["cases"], 4)
        self.assertEqual(record["result"]["counts"]["checks"], 6)
        self.assertTrue(record["result"]["usable_as_evidence"])
        for name in ("unit", "serve-check", "browser"):
            self.assertTrue((self.repo.artifact_dir / "runs" / run_id / ("%s.log" % name)).is_file())
        index = [json.loads(line) for line in
                 (self.repo.artifact_dir / "index.jsonl").read_text(encoding="utf-8").splitlines()]
        self.assertEqual(len(index), 1)
        self.assertEqual(index[0]["run_id"], run_id)
        self.assertEqual(index[0]["outcome"], "passed")


class BrowserScenarioTests(VerifyEntryTestCase):
    """issue #53：浏览器组从场景摘要行接入场景级记录。"""

    def use_scenario_browser(self) -> None:
        self.repo.write("tests/e2e_browser.py", BROWSER_SCENARIO_STUB, executable=True)
        self.repo.commit_all("换成带场景摘要的合成浏览器入口")

    def test_browser_group_records_scenario_list_and_times(self):
        self.use_scenario_browser()
        p = self.repo.run_verify("run", "--group", "browser")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        record = read_record(self.repo.artifact_dir, self.repo.latest_run_id())
        group = record["groups"][0]
        self.assertEqual(group["granularity"], "scenario", "有场景摘要时按场景计时")
        scenarios = group["scenarios"]
        self.assertEqual([item["id"] for item in scenarios], ["skill-search", "server-mode"])
        for item in scenarios:
            self.assertEqual(item["status"], "passed")
            self.assertEqual(item["checks"], {"total": 2, "passed": 2, "failed": 0})
            self.assertIn("started_at", item)
            self.assertIn("ended_at", item)
            self.assertGreaterEqual(item["seconds"], 0.05)
        self.assertEqual(group["counts"], {"total": 4, "passed": 4, "failed": 0})
        self.assertIn("skill-search", p.stdout)
        self.assertIn("server-mode", p.stdout)
        self.assertIn("场景", p.stdout)

    def test_failing_scenario_is_identifiable_in_the_record(self):
        self.use_scenario_browser()
        p = self.repo.run_verify("run", "--group", "browser",
                                 extra_env={"SYNTH_SCENARIO_STATUS": "failed"})
        self.assertEqual(p.returncode, 1, p.stdout + p.stderr)
        record = read_record(self.repo.artifact_dir, self.repo.latest_run_id())
        group = record["groups"][0]
        self.assertEqual(group["outcome"], "failed")
        statuses = {item["id"]: item["status"] for item in group["scenarios"]}
        self.assertEqual(statuses, {"skill-search": "passed", "server-mode": "failed"})

    def test_browser_exit_130_is_interrupted_not_failed(self):
        """子进程以 130 自行退出（浏览器脚本的中断路径）时按中断归类，不冒充失败。"""
        self.use_scenario_browser()
        p = self.repo.run_verify("run", "--group", "browser",
                                 extra_env={"SYNTH_SCENARIO_STATUS": "interrupted"})
        self.assertEqual(p.returncode, 130, p.stdout + p.stderr)
        record = read_record(self.repo.artifact_dir, self.repo.latest_run_id())
        group = record["groups"][0]
        self.assertEqual(group["outcome"], "interrupted", p.stdout)
        self.assertEqual(group["exit_status"], 130)
        statuses = {item["id"]: item["status"] for item in group["scenarios"]}
        self.assertEqual(statuses, {"skill-search": "passed", "server-mode": "interrupted",
                                    "css-fidelity": "not_run"})
        self.assertFalse(record["result"]["usable_as_evidence"], "中断不能作为通过证据")

    def test_interrupting_wrapper_preserves_scenarios_and_finishes_child_cleanup(self):
        """真实中断父入口：日志流须留到子进程交出场景摘要，二次信号不打断回收。"""
        script = BROWSER_SCENARIO_STUB.replace(
            'STATUS = os.environ.get("SYNTH_SCENARIO_STATUS", "passed")', '''from pathlib import Path
import signal

def cancel(signum, frame):
    raise KeyboardInterrupt

signal.signal(signal.SIGTERM, cancel)
try:
    Path(os.environ["SYNTH_READY"]).write_text("ready")
    time.sleep(30)
except KeyboardInterrupt:
    Path(os.environ["SYNTH_CLEANING"]).write_text("cleaning")
    time.sleep(0.3)
    STATUS = "interrupted"
''')
        self.repo.write("tests/e2e_browser.py", script, executable=True)
        self.repo.commit_all("合成父入口取消与场景摘要")
        for signum in (signal.SIGINT, signal.SIGTERM):
            with self.subTest(signum=signum):
                ready = Path(self.temp.name) / ("ready-%s" % signum)
                cleaning = Path(self.temp.name) / ("cleaning-%s" % signum)
                env = dict(os.environ)
                env.update(PATH=str(self.repo.path / "bin") + os.pathsep + env["PATH"],
                           SYNTH_READY=str(ready), SYNTH_CLEANING=str(cleaning))
                wrapper = subprocess.Popen(
                    [sys.executable, "verify.py", "run", "--group", "browser",
                     "--group", "serve-check"], cwd=self.repo.path, env=env,
                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
                try:
                    deadline = time.monotonic() + 10
                    while not ready.exists() and wrapper.poll() is None and time.monotonic() < deadline:
                        time.sleep(0.02)
                    self.assertTrue(ready.exists(), "浏览器子进程已就绪")
                    wrapper.send_signal(signum)
                    while not cleaning.exists() and wrapper.poll() is None and time.monotonic() < deadline:
                        time.sleep(0.02)
                    self.assertTrue(cleaning.exists(), "子进程正在处理取消并准备摘要")
                    wrapper.send_signal(signum)
                    output, _ = wrapper.communicate(timeout=15)
                finally:
                    if wrapper.poll() is None:
                        wrapper.kill()
                        wrapper.wait()
                    wrapper.stdout.close()
                self.assertEqual(wrapper.returncode, 128 + signum, output)
                run_id = self.repo.latest_run_id()
                record = read_record(self.repo.artifact_dir, run_id)
                self.assertEqual(record["state"], "aborted")
                group = record["groups"][0]
                self.assertEqual(group["outcome"], "interrupted")
                self.assertEqual(group["exit_status"], 128 + signum)
                self.assertEqual(group["signal"], signal.Signals(signum).name)
                self.assertEqual(group["granularity"], "scenario")
                self.assertEqual(group["counts"], {"total": 3, "passed": 3, "failed": 0})
                self.assertEqual({item["id"]: item["status"] for item in group["scenarios"]},
                                 {"skill-search": "passed", "server-mode": "interrupted",
                                  "css-fidelity": "not_run"})
                self.assertFalse(record["result"]["usable_as_evidence"])
                run_dir = self.repo.artifact_dir / "runs" / run_id
                self.assertIn("AE-SCENARIOS:", (run_dir / "browser.log").read_text())
                children = json.loads((run_dir / "children.json").read_text())
                self.assertEqual(len(children), 1, "取消后不再执行后续组")
                self.assertTrue(children[0]["reaped"])
                self.assertFalse(pid_alive(children[0]["pid"]))

    def test_signal_while_draining_log_keeps_completed_scenario_evidence(self):
        """子进程退出与读线程结束之间收到信号，也不能丢掉已输出的场景证据。"""
        self.use_scenario_browser()
        wrapper_path = self.repo.path / "verify.py"
        source = wrapper_path.read_text(encoding="utf-8")
        # 在合成入口的日志排空边界注入一次真实信号，避免依赖调度速度碰竞态。
        source = source.replace("reader.join(timeout=5)",
                                "os.kill(os.getpid(), signal.SIGINT)\n"
                                "                reader.join(timeout=5)", 1)
        wrapper_path.write_text(source, encoding="utf-8")
        self.repo.commit_all("合成日志排空期间取消")
        p = self.repo.run_verify("run", "--group", "browser", "--group", "serve-check")
        self.assertEqual(p.returncode, 130, p.stdout + p.stderr)
        run_id = self.repo.latest_run_id()
        record = read_record(self.repo.artifact_dir, run_id)
        self.assertEqual(record["state"], "aborted")
        group = record["groups"][0]
        self.assertEqual(group["outcome"], "interrupted")
        self.assertEqual(group["counts"], {"total": 4, "passed": 4, "failed": 0})
        self.assertEqual([item["status"] for item in group["scenarios"]], ["passed", "passed"])
        self.assertFalse(record["result"]["usable_as_evidence"])
        children = json.loads((self.repo.artifact_dir / "runs" / run_id / "children.json").read_text())
        self.assertEqual(len(children), 1)
        self.assertTrue(children[0]["reaped"])


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
        self.assertEqual(sorted(ids), ["test_i18n_catalog_contract.RepoCatalogContractTests"
                                       ".test_shipped_catalogs_stay_consistent",
                                       "test_synth.AlphaTests.test_alpha_also_ok",
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


class RelatedContractTests(VerifyEntryTestCase):
    """unit-related 固定附带全局词典契约（issue #51）：去重后独立跑相关组也不丢全局保护。"""

    CONTRACT_ID = ("test_i18n_catalog_contract.RepoCatalogContractTests"
                   ".test_shipped_catalogs_stay_consistent")

    def test_unit_related_auto_includes_the_global_catalog_contract(self):
        p = self.repo.run_verify("run", "unit-related", "tests/test_synth.py")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        record = read_record(self.repo.artifact_dir, self.repo.latest_run_id())
        self.assertEqual(record["context"]["selectors"], ["tests/test_synth.py"],
                         "记录保留调用方的原始选择器")
        self.assertEqual(record["context"]["selectors_effective"],
                         ["tests/test_synth.py", "tests/test_i18n_catalog_contract.py"],
                         "生效选择器要写明自动附带的契约模块")
        ids = [case["id"] for case in record["groups"][0]["cases"]]
        self.assertIn(self.CONTRACT_ID, ids, "相关组自动执行全局词典契约")
        self.assertIn("test_synth.AlphaTests.test_alpha_ok", ids)

    def test_explicit_contract_selector_is_not_duplicated(self):
        p = self.repo.run_verify("run", "unit-related", "tests/test_i18n_catalog_contract.py")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        record = read_record(self.repo.artifact_dir, self.repo.latest_run_id())
        ids = [case["id"] for case in record["groups"][0]["cases"]]
        self.assertEqual(ids.count(self.CONTRACT_ID), 1, "契约只执行一次，不重复")
        self.assertEqual(record["context"]["selectors_effective"],
                         ["tests/test_i18n_catalog_contract.py"])

    def test_method_selector_of_the_contract_module_is_not_duplicated(self):
        p = self.repo.run_verify(
            "run", "unit-related",
            "tests.test_i18n_catalog_contract.RepoCatalogContractTests.test_shipped_catalogs_stay_consistent")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        record = read_record(self.repo.artifact_dir, self.repo.latest_run_id())
        ids = [case["id"] for case in record["groups"][0]["cases"]]
        # 点分选择器导入的模块名带 tests. 前缀，用方法后缀计数判断唯一
        contract_runs = [i for i in ids if i.endswith(".RepoCatalogContractTests"
                                                     ".test_shipped_catalogs_stay_consistent")]
        self.assertEqual(len(contract_runs), 1, "指到契约模块内部时也不能重复执行")

    def test_explicit_contract_selector_with_dot_slash_prefix_is_not_duplicated(self):
        """R1：./ 前缀写法与标准写法指向同一模块，去重按归一后的前缀判断。"""
        p = self.repo.run_verify("run", "unit-related", "./tests/test_i18n_catalog_contract.py")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        record = read_record(self.repo.artifact_dir, self.repo.latest_run_id())
        self.assertEqual(record["context"]["selectors_effective"],
                         ["./tests/test_i18n_catalog_contract.py"],
                         "./ 写法归一后不得再附带第二种写法的契约模块")
        ids = [case["id"] for case in record["groups"][0]["cases"]]
        self.assertEqual(ids.count(self.CONTRACT_ID), 1, "契约只执行一次，不重复")

    def test_explicit_contract_selector_via_detour_relative_path_is_not_duplicated(self):
        """R1：绕行相对路径（../<repo>/tests/…，从仓库外 cwd 出发）也归一到同一前缀。"""
        outside = Path(self.temp.name) / "outside"
        outside.mkdir()
        p = self.repo.run_verify("run", "unit-related",
                                 "../repo/tests/test_i18n_catalog_contract.py", cwd=outside)
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        record = read_record(self.repo.artifact_dir, self.repo.latest_run_id())
        self.assertEqual(record["context"]["selectors_effective"],
                         ["../repo/tests/test_i18n_catalog_contract.py"],
                         "绕行写法归一后不得再附带契约模块的第二种写法")
        ids = [case["id"] for case in record["groups"][0]["cases"]]
        self.assertEqual(ids.count(self.CONTRACT_ID), 1, "绕行写法也只执行一次契约")

    def test_missing_contract_module_fails_loudly_instead_of_silently(self):
        """契约模块被删时局部组必须报错：全局保护不能静默消失。"""
        (self.repo.path / "tests" / "test_i18n_catalog_contract.py").unlink()
        self.repo.commit_all("合成删除契约模块")
        p = self.repo.run_verify("run", "unit-related", "tests/test_synth.py")
        self.assertEqual(p.returncode, 1, p.stdout + p.stderr)
        record = read_record(self.repo.artifact_dir, self.repo.latest_run_id())
        self.assertEqual(record["groups"][0]["outcome"], "error")

    def test_synthetic_inconsistent_catalog_fails_local_and_full_groups(self):
        """合成的不一致词典让局部组与完整组都失败，修复后都恢复通过。"""
        en = self.repo.path / "locales" / "en.json"
        original = en.read_text(encoding="utf-8")
        en.write_text('{"a": "A", "probe": "P"}\n', encoding="utf-8")
        try:
            local = self.repo.run_verify("run", "unit-related", "tests/test_synth.py")
            self.assertEqual(local.returncode, 1, local.stdout + local.stderr)
            record = read_record(self.repo.artifact_dir, self.repo.latest_run_id())
            by_id = {case["id"]: case for case in record["groups"][0]["cases"]}
            self.assertEqual(by_id[self.CONTRACT_ID]["status"], "failed",
                             "局部组要发现词典不一致")
            self.assertEqual(by_id["test_synth.AlphaTests.test_alpha_ok"]["status"], "passed",
                             "失败只来自词典契约，不连累指定模块")
            full = self.repo.run_verify("run", "--group", "unit")
            self.assertEqual(full.returncode, 1, full.stdout + full.stderr)
            full_record = read_record(self.repo.artifact_dir, self.repo.latest_run_id())
            full_status = {case["id"]: case["status"]
                           for case in full_record["groups"][0]["cases"]}
            self.assertEqual(full_status[self.CONTRACT_ID], "failed",
                             "完整组同样发现词典不一致")
        finally:
            en.write_text(original, encoding="utf-8")
        local = self.repo.run_verify("run", "unit-related", "tests/test_synth.py")
        self.assertEqual(local.returncode, 0, local.stdout + local.stderr)
        full = self.repo.run_verify("run", "--group", "unit")
        self.assertEqual(full.returncode, 0, full.stdout + full.stderr)

    def test_plan_shows_the_auto_included_contract(self):
        p = self.repo.run_verify("plan", "--group", "unit-related", "tests/test_synth.py")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertIn("test_i18n_catalog_contract", p.stdout, "plan 说明自动附带的契约模块")
        plan = json.loads(self.repo.run_verify(
            "plan", "--group", "unit-related", "tests/test_synth.py", "--json").stdout)
        unit_related = {group["name"]: group for group in plan["groups"]}["unit-related"]
        self.assertEqual(unit_related["auto_included"], ["tests/test_i18n_catalog_contract.py"])
        plain = json.loads(self.repo.run_verify("plan", "--json").stdout)
        plain_related = {group["name"]: group for group in plain["groups"]}["unit-related"]
        self.assertEqual(plain_related["auto_included"], [], "没有选择器就没有自动附带")


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
        self.assertIsNone(record["groups"][0]["started_at"])
        self.assertEqual(record["shadow"]["executed_groups"], [])
        self.assertFalse(record["shadow"]["coverage"]["browser"])
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
        run_dir = self.repo.artifact_dir / "runs" / record["run_id"]
        original_record = (run_dir / "record.json").read_bytes()
        log = run_dir / "unit.log"
        log.write_text(log.read_text(encoding="utf-8") + "事后补的一行\n", encoding="utf-8")
        p = self.repo.run_verify("report", "--run", record["run_id"])
        self.assertEqual(p.returncode, 1, p.stdout + p.stderr)
        self.assertIn("校验和不一致", p.stdout)
        self.assertIn("不能作为通过证据", p.stdout)
        self.assertNotIn("；可作为通过证据", p.stdout)
        report = self.repo.run_verify("report", "--run", record["run_id"], "--json")
        self.assertEqual(report.returncode, 1, report.stdout + report.stderr)
        current = json.loads(report.stdout)
        self.assertFalse(current["result"]["usable_as_evidence"])
        self.assertFalse(current["shadow"]["usable_as_evidence"], "影子证据同步降级")
        self.assertFalse(current["shadow"]["comparable"])
        self.assertIn("校验失败", current["shadow"]["incomparable_reason"])
        self.assertEqual(current["shadow"]["candidate_omissions"], [])
        self.assertEqual(current["evidence"]["integrity"], "degraded")
        self.assertIn("校验和不一致", report.stdout)
        self.assertEqual(current["result"]["outcome"], "passed", "原始测试结论仍然保留")
        self.assertEqual((run_dir / "record.json").read_bytes(), original_record,
                         "报告重新校验证据，但不改写原始运行记录")

    def test_missing_log_breaks_the_evidence(self):
        record = self.prepare("run", "--group", "unit")
        (self.repo.artifact_dir / "runs" / record["run_id"] / "unit.log").unlink()
        p = self.repo.run_verify("report", "--run", record["run_id"])
        self.assertEqual(p.returncode, 1, p.stdout + p.stderr)
        self.assertIn("缺少日志", p.stdout)
        self.assertIn("不能作为通过证据", p.stdout)
        report = self.repo.run_verify("report", "--run", record["run_id"], "--json")
        self.assertEqual(report.returncode, 1, report.stdout + report.stderr)
        current = json.loads(report.stdout)
        self.assertFalse(current["result"]["usable_as_evidence"])
        self.assertEqual(current["evidence"]["integrity"], "degraded")
        self.assertIn("缺少日志", report.stdout)

    def test_report_without_any_record_is_not_a_success(self):
        p = self.repo.run_verify("report", "--latest")
        self.assertEqual(p.returncode, 1, p.stdout + p.stderr)
        self.assertIn("还没有可报告的验证记录", p.stderr)


class ReviewRegressionTests(VerifyEntryTestCase):
    """PR #62 独立审查：边界输入、证据读取与异常退出的命令行回归。"""

    def test_absolute_output_and_selector_paths_are_redacted_in_records(self):
        selector = self.repo.path / "tests" / "test_synth.py"
        for equals_style in (False, True):
            with self.subTest(equals_style=equals_style):
                artifacts = Path(self.temp.name) / ("out-%s" % equals_style)
                out_args = (["--out-dir=" + str(artifacts)] if equals_style
                            else ["--out-dir", str(artifacts)])
                p = self.repo.run_verify("run", "unit-related", str(selector), *out_args)
                self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
                run_id = json.loads((artifacts / "latest.json").read_text(encoding="utf-8"))["run_id"]
                record = read_record(artifacts, run_id)
                serialized = json.dumps(record, ensure_ascii=False)
                self.assertNotIn(str(artifacts), serialized, "产物路径不能落入共享记录")
                self.assertNotIn(str(artifacts.resolve()), serialized)
                self.assertNotIn(str(self.repo.path), serialized, "选择器不能暴露本机仓库位置")
                self.assertNotIn(str(self.repo.path.resolve()), serialized)
                self.assertEqual(record["context"]["selectors"], ["tests/test_synth.py"])
                self.assertEqual(absolute_strings(record), [])
                sidecar = (artifacts / "runs" / run_id / "unit-related.cases.json").read_text(
                    encoding="utf-8")
                self.assertNotIn(str(self.repo.path), sidecar, "用例明细也只记仓库相对选择器")
                self.assertNotIn(str(self.repo.path.resolve()), sidecar)

    def test_equal_size_large_fixture_change_invalidates_input_coverage(self):
        self.unit('''import pathlib
import unittest

class LargeFixtureTests(unittest.TestCase):
    def test_change(self):
        with pathlib.Path("fixtures/large.bin").open("r+b") as handle:
            handle.seek(8)
            handle.write(b"B")
''')
        self.repo.commit_all("合成大文件变更测试")
        fixture = self.repo.path / "fixtures" / "large.bin"
        fixture.parent.mkdir()
        fixture.write_bytes(b"A" * (4 * 1024 * 1024 + 1))
        size = fixture.stat().st_size
        p = self.repo.run_verify("run", "--quiet")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertEqual(fixture.stat().st_size, size, "复现保持文件长度不变")
        record = read_record(self.repo.artifact_dir, self.repo.latest_run_id())
        self.assertTrue(record["input"]["changed_during_run"])
        self.assertFalse(record["input"]["covers_final_input"])
        self.assertFalse(record["result"]["usable_as_evidence"])
        self.assertNotEqual(record["input"]["fingerprint"], record["input"]["final_fingerprint"])

    @unittest.skipUnless(os.name == "posix" and os.geteuid() != 0,
                         "文件读取权限回归需要非 root 的 POSIX 用户")
    def test_unreadable_input_cannot_produce_usable_evidence(self):
        fixture = self.repo.write("fixtures/unreadable.txt", "合成受限输入")
        fixture.chmod(0)
        self.addCleanup(fixture.chmod, 0o600)
        p = self.repo.run_verify("run", "--quiet")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        record = read_record(self.repo.artifact_dir, self.repo.latest_run_id())
        self.assertFalse(record["input"]["available"])
        self.assertTrue(record["input"]["reason"])
        self.assertIsNot(record["input"]["covers_final_input"], True)
        self.assertFalse(record["result"]["usable_as_evidence"])
        report = self.repo.run_verify("report", "--latest")
        self.assertEqual(report.returncode, 1, report.stdout + report.stderr)

    def test_multiple_failed_subtests_count_one_failed_main_case(self):
        self.unit('''import unittest

class SubTests(unittest.TestCase):
    def test_failures(self):
        for index in range(3):
            with self.subTest(index=index):
                self.fail("合成子测试失败")

    def test_pass(self):
        self.assertTrue(True)
''')
        self.repo.commit_all("合成多个失败子测试")
        p = self.repo.run_verify("run", "--quiet")
        self.assertEqual(p.returncode, 1, p.stdout + p.stderr)
        record = read_record(self.repo.artifact_dir, self.repo.latest_run_id())
        counts = record["result"]["counts"]
        self.assertEqual((counts["cases"], counts["failed"], counts["passed"]), (3, 1, 2))
        self.assertEqual(counts["subtest_failures"], 3)
        self.assertEqual([case["status"] for case in record["groups"][0]["cases"]],
                         ["passed", "failed", "passed"])

    def test_child_sigint_preserves_interruption_and_completed_cases(self):
        self.unit('''import os
import signal
import unittest

class InterruptTests(unittest.TestCase):
    def test_a_completed(self):
        self.assertTrue(True)

    def test_b_interrupt(self):
        os.kill(os.getpid(), signal.SIGINT)

    def test_c_not_started(self):
        self.fail("中断后的用例不能执行")
''')
        self.repo.commit_all("合成子进程 SIGINT")
        p = self.repo.run_verify("run", "--quiet")
        self.assertEqual(p.returncode, 130, p.stdout + p.stderr)
        run_id = self.repo.latest_run_id()
        record = read_record(self.repo.artifact_dir, run_id)
        self.assertEqual(record["result"]["outcome"], "interrupted")
        self.assertEqual(record["result"]["exit_status"], 130)
        self.assertFalse(record["result"]["usable_as_evidence"])
        cases = {case["name"]: case for case in record["groups"][0]["cases"]}
        self.assertEqual(cases["test_a_completed"]["status"], "passed")
        self.assertNotEqual(cases.get("test_b_interrupt", {}).get("status"), "passed")
        self.assertNotIn("test_c_not_started", cases)
        children = json.loads((self.repo.artifact_dir / "runs" / run_id / "children.json").read_text(
            encoding="utf-8"))
        self.assertEqual(children[0]["exit_status"], 130)
        self.assertTrue(children[0]["reaped"])

    def test_registry_write_failure_reaps_owned_child_and_preserves_decoy(self):
        self.unit(UNIT_SLOW)
        self.repo.commit_all("合成登记失败时的慢测试")
        injection = Path(self.temp.name) / "injection"
        injection.mkdir()
        pid_file = Path(self.temp.name) / "owned-child.pid"
        (injection / "sitecustomize.py").write_text('''import errno
import json
import os
from pathlib import Path

original_replace = os.replace

def replace_with_registry_failure(source, target):
    if str(target).endswith("/children.json"):
        entries = json.loads(Path(source).read_text(encoding="utf-8"))
        Path(os.environ["SYNTH_CHILD_PID"]).write_text(str(entries[0]["pid"]), encoding="utf-8")
        raise OSError(errno.ENOSPC, "synthetic registry disk full")
    return original_replace(source, target)

os.replace = replace_with_registry_failure
''', encoding="utf-8")
        decoy = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"],
                                 stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                 stderr=subprocess.DEVNULL)
        child_pid = None
        try:
            p = self.repo.run_verify("run", "--quiet", timeout=20, extra_env={
                "PYTHONPATH": str(injection), "SYNTH_CHILD_PID": str(pid_file)})
            self.assertTrue(pid_file.is_file(), "故障发生在子进程已经启动并登记之后")
            child_pid = int(pid_file.read_text(encoding="utf-8"))
            self.assertEqual(p.returncode, 2, p.stdout + p.stderr)
            self.assertIn("入口自身失败", p.stderr)
            self.assertNotIn("Traceback", p.stderr)
            self.assertFalse(pid_alive(child_pid), "写登记文件失败也必须回收已启动的子进程")
            self.assertIsNone(decoy.poll(), "未登记进程不受异常收尾影响")
            record = read_record(self.repo.artifact_dir, self.repo.latest_run_id())
            self.assertEqual(record["result"]["outcome"], "wrapper_error")
            self.assertFalse(record["result"]["usable_as_evidence"])
        finally:
            if child_pid is None and pid_file.is_file():
                child_pid = int(pid_file.read_text(encoding="utf-8"))
            if child_pid and pid_alive(child_pid):
                try:
                    os.kill(child_pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            decoy.kill()
            decoy.wait(timeout=5)


class UnitOutcomeBoundaryTests(VerifyEntryTestCase):
    """主用例、子测试与 fixture 的结论分别计数，加载期间的信号仍属于中断。"""

    def run_body(self, body: str, expected_exit: int, *selection: str) -> dict:
        self.unit(body)
        self.repo.commit_all("合成单元结论边界")
        p = self.repo.run_verify("run", *selection, "--quiet")
        self.assertEqual(p.returncode, expected_exit, p.stdout + p.stderr)
        return read_record(self.repo.artifact_dir, self.repo.latest_run_id())

    def test_only_skipped_subtest_is_not_a_pass(self):
        # 契约模块也要整个跳过，套件才是「只有跳过」的形态
        self.repo.write("tests/test_i18n_catalog_contract.py", UNIT_SKIP)
        record = self.run_body('''import unittest

class SubTests(unittest.TestCase):
    def test_skipped(self):
        with self.subTest(name="合成跳过"):
            self.skipTest("没有执行子测试")
''', 1)
        self.assertEqual(record["result"]["outcome"], "skipped")
        self.assertFalse(record["result"]["usable_as_evidence"])
        counts = record["result"]["counts"]
        self.assertEqual((counts["cases"], counts["passed"], counts["skipped"]), (2, 0, 2))
        self.assertEqual(counts["subtest_skips"], 1)
        self.assertEqual(record["groups"][0]["cases"][-1]["status"], "skipped")

    def test_class_fixture_skip_does_not_skip_another_passed_case(self):
        record = self.run_body('''import unittest

class AUnavailableTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        raise unittest.SkipTest("合成类 fixture 不适用")

    def test_not_started(self):
        self.fail("被跳过的类不能执行")

class BAvailableTests(unittest.TestCase):
    def test_pass(self):
        self.assertTrue(True)
''', 0)
        self.assertEqual(record["result"]["outcome"], "passed")
        self.assertTrue(record["result"]["usable_as_evidence"])
        counts = record["result"]["counts"]
        self.assertEqual((counts["cases"], counts["passed"], counts["skipped"]), (2, 2, 0))
        self.assertEqual(counts["fixture_skips"], 1)
        self.assertEqual([case["name"] for case in record["groups"][0]["cases"]],
                         ["test_shipped_catalogs_stay_consistent", "test_pass"])

    def test_sigint_during_explicit_module_import_is_interrupted(self):
        record = self.run_body('''import os
import signal

os.kill(os.getpid(), signal.SIGINT)
''', 130, "unit-related", "tests/test_synth.py")
        self.assertEqual(record["result"]["outcome"], "interrupted")
        self.assertEqual(record["result"]["exit_status"], 130)
        self.assertFalse(record["result"]["usable_as_evidence"])
        self.assertEqual(record["result"]["counts"]["cases"], 0)
        self.assertEqual(record["groups"][0]["cases"], [])

    def test_class_fixture_error_is_separate_from_case_errors(self):
        record = self.run_body('''import unittest

class BrokenFixtureTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        raise ValueError("合成类 fixture 错误")

    def test_not_started(self):
        self.fail("准备失败后不能执行")
''', 1)
        self.assertEqual(record["result"]["outcome"], "error")
        self.assertFalse(record["result"]["usable_as_evidence"])
        counts = record["result"]["counts"]
        self.assertEqual((counts["cases"], counts["passed"], counts["errors"]), (1, 1, 0))
        self.assertEqual(counts["fixture_errors"], 1)
        self.assertEqual([case["name"] for case in record["groups"][0]["cases"]],
                         ["test_shipped_catalogs_stay_consistent"])

    def test_passed_and_skipped_subtests_preserve_passed_main_case(self):
        record = self.run_body('''import unittest

class SubTests(unittest.TestCase):
    def test_mixed(self):
        with self.subTest(name="通过"):
            self.assertTrue(True)
        with self.subTest(name="跳过"):
            self.skipTest("合成子测试不适用")
''', 0)
        self.assertEqual(record["result"]["outcome"], "passed")
        self.assertTrue(record["result"]["usable_as_evidence"])
        counts = record["result"]["counts"]
        self.assertEqual((counts["cases"], counts["passed"], counts["skipped"]), (2, 2, 0))
        self.assertEqual(counts["subtest_skips"], 1)
        self.assertEqual(record["groups"][0]["cases"][-1]["status"], "passed")

    def test_unexpected_success_is_not_counted_as_passed(self):
        record = self.run_body('''import unittest

class ExpectedFailureTests(unittest.TestCase):
    @unittest.expectedFailure
    def test_unexpected_success(self):
        self.assertTrue(True)
''', 1)
        self.assertEqual(record["result"]["outcome"], "failed")
        self.assertFalse(record["result"]["usable_as_evidence"])
        counts = record["result"]["counts"]
        self.assertEqual((counts["cases"], counts["passed"], counts["unexpected_successes"]), (2, 1, 1))
        self.assertEqual(record["groups"][0]["cases"][-1]["status"], "unexpected_success")


# ── 阶段选测与影子对照（issue #55） ─────────────────────────────────────────
#
# 全部用合成仓库验证外部行为：plan 的建议 / 回退解释，run 的影子记录与遗漏归因。
# 合成映射经 AE_SELECTION_MAP 注入（结构与真实仓库内置映射相同）；测试不镜像
# 内置映射表逐字断言，只验证计划、执行与报告的外部行为。

E2E_SCENARIOS_STUB = '''"""合成浏览器场景注册表：与真实 tests/e2e_scenarios.py 同构的最小清单。"""
import dataclasses


@dataclasses.dataclass(frozen=True)
class Scenario:
    id: str
    title: str
    fixture: str
    isolation: str
    full_required: bool = True
    aliases: tuple = ()


SCENARIOS = [
    Scenario(id="skill-search", title="合成场景一", fixture="合成材料", isolation="独立上下文"),
    Scenario(id="server-mode", title="合成场景二", fixture="合成材料", isolation="独立上下文"),
]
SCENARIO_IDS = tuple(scenario.id for scenario in SCENARIOS)
'''

# 合成映射：结构与内置映射一致；version 取 7 以区分内置版本，验证版本随来源透传
SELECTION_MAP_OK = {
    "schema": "ae-verify-selection/1",
    "version": 7,
    "doc_allowlist": [
        {"prefix": "README.md", "category": "仓库说明"},
        {"prefix": "docs/assets/", "category": "头图与截图"},
    ],
    "rules": [
        {"id": "r-cli", "prefixes": ["tools/synth-cli.py"],
         "selectors": ["tests/test_synth.py"],
         "why": "合成 CLI 影响 test_synth"},
        {"id": "r-serve", "prefixes": ["skill/annotation-everything/scripts/serve.py"],
         "selectors": ["tests/test_synth.py"],
         "serve_check": True,
         "scenarios": ["skill-search"],
         "why": "合成服务影响服务安全自检与场景"},
        {"id": "r-page", "prefixes": ["templates/"],
         "selectors": ["tests/test_synth.py"],
         "scenarios": ["skill-search"],
         "why": "合成页面模板影响场景"},
        {"id": "r-dict", "prefixes": ["locales/"],
         "selectors": ["tests/test_i18n_catalog_contract.py", "tests/test_synth.py"],
         "why": "合成词典与全局契约"},
        {"id": "r-docs", "prefixes": ["docs/"],
         "selectors": [], "fallback": "guarded_doc",
         "why": "docs 不在允许列表，保守回退"},
        {"id": "r-skill", "prefixes": ["SKILL.md"],
         "selectors": [], "fallback": "shared_resource",
         "why": "技能指引不放行"},
        {"id": "r-exec", "prefixes": ["verify.py"],
         "selectors": [], "fallback": "strategy_change",
         "why": "验证入口自身变化"},
        {"id": "r-fixture", "prefixes": ["fixtures/"],
         "selectors": [], "fallback": "shared_resource",
         "why": "fixture 不能按扩展名放行"},
    ],
}

# 运行期间改写已跟踪文件的慢测试：让结束快照与开始快照不同（AC8 的输入变化）
CHURN_BODY = '''import pathlib
import time
import unittest


class ChurnShadowTests(unittest.TestCase):
    def test_touches_source_while_running(self):
        path = pathlib.Path(__file__).resolve().parents[1] / "README.md"
        path.write_text(path.read_text(encoding="utf-8") + "运行期改动\\n", encoding="utf-8")
        time.sleep(1.0)
        self.assertTrue(True)
'''


class SelectionPlanTests(VerifyEntryTestCase):
    """阶段选测计划：plan --phase 的建议、回退与可解释性。"""

    def setUp(self):
        super().setUp()
        self.repo.write("tests/e2e_scenarios.py", E2E_SCENARIOS_STUB)
        self.map_path = self.repo.write(
            "selection-map.json", json.dumps(SELECTION_MAP_OK, ensure_ascii=False, indent=2))
        self.sel_env = {"AE_SELECTION_MAP": str(self.map_path)}
        # 映射与场景 stub 属于测试基建：提交进基线，避免被当成未分类改动
        self.repo.commit_all("选测测试基建")

    def selection(self, *args: str, env: dict | None = None) -> dict:
        merged = dict(self.sel_env)
        if env:
            merged.update(env)
        p = self.repo.run_verify("plan", "--json", *args, extra_env=merged)
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        return json.loads(p.stdout)["selection"]

    def write_map(self, payload: dict) -> None:
        self.map_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    def test_development_plan_matches_rules_and_is_deterministic(self):
        self.repo.write("tools/synth-cli.py", "#!/usr/bin/env python3\nprint('cli')\n")
        self.repo.commit_all("合成 CLI")
        self.repo.write("tools/synth-cli.py", "#!/usr/bin/env python3\nprint('cli v2')\n")
        first = self.selection("--phase", "development")
        self.assertEqual(first["schema"], "ae-verify-selection/1")
        self.assertEqual(first["strategy_version"], 7, "策略版本随映射来源透传")
        self.assertEqual(first["strategy_source"], "override")
        self.assertIsNone(first["fallback"], "命中规则时不需要回退")
        matched = {item["id"]: item for item in first["matched_rules"]}
        self.assertIn("r-cli", matched, "改动命中声明的规则")
        self.assertIn("tools/synth-cli.py", matched["r-cli"]["paths"])
        self.assertTrue(matched["r-cli"]["why"], "每条命中规则可解释")
        suggested = first["suggested"]
        self.assertEqual(suggested["groups"], ["unit-related"])
        self.assertIn("tests/test_synth.py", suggested["selectors"])
        self.assertTrue(suggested["runs_tests"])
        second = self.selection("--phase", "development")
        self.assertEqual(first, second, "同一输入、阶段和策略版本得到相同计划")

    def test_unknown_path_falls_back_to_full_check(self):
        self.repo.write("mystery.xyz", "未知类别\n")
        plan = self.selection("--phase", "development")
        self.assertIsNotNone(plan["fallback"], "未分类路径保守回退")
        self.assertEqual(plan["fallback"]["kind"], "unknown_path")
        self.assertIn("mystery.xyz", plan["fallback"]["reason"], "回退原因点名未知路径")
        self.assertIn("mystery.xyz", plan["unknown_paths"])
        self.assertEqual(plan["suggested"]["groups"], ["unit"], "回退建议完整检查，不是空集合")
        self.assertTrue(plan["suggested"]["notes"])

    def test_bad_baseline_falls_back(self):
        self.repo.write("tools/synth-cli.py", "v1\n")
        self.repo.commit_all("合成 CLI")
        plan = self.selection("--phase", "development", "--baseline", "deadbeefdeadbeef")
        self.assertIsNone(plan["baseline"]["commit"], "解析不了的基线明示为空")
        self.assertEqual(plan["fallback"]["kind"], "bad_baseline")

    def test_unparsable_map_falls_back(self):
        self.repo.write("selection-map.json", "{ 不是 JSON")
        plan = self.selection("--phase", "development")
        self.assertEqual(plan["fallback"]["kind"], "broken_rules")
        self.assertTrue(plan["fallback"]["reason"])

    def test_unknown_scenario_id_falls_back(self):
        import copy
        payload = copy.deepcopy(SELECTION_MAP_OK)
        for rule in payload["rules"]:
            if rule["id"] == "r-serve":
                rule["scenarios"] = ["no-such-scene"]
        self.write_map(payload)
        self.repo.write("skill/annotation-everything/scripts/serve.py", SERVE_STUB)
        plan = self.selection("--phase", "development")
        self.assertEqual(plan["fallback"]["kind"], "incomplete_manifest",
                         "规则引用不存在的场景：映射与场景注册表失配，保守回退")
        self.assertIn("no-such-scene", plan["fallback"]["reason"])

    def test_manifest_gaps_fall_back(self):
        import copy
        # 选择器指向不存在的测试文件：测试清单不完整
        payload = copy.deepcopy(SELECTION_MAP_OK)
        for rule in payload["rules"]:
            if rule["id"] == "r-cli":
                rule["selectors"] = ["tests/test_missing.py"]
        self.write_map(payload)
        self.repo.write("tools/synth-cli.py", "v1\n")
        plan = self.selection("--phase", "development")
        self.assertEqual(plan["fallback"]["kind"], "incomplete_manifest")
        # 场景注册表缺失但规则引用场景
        self.write_map(SELECTION_MAP_OK)
        (self.repo.path / "tests" / "e2e_scenarios.py").unlink()
        plan2 = self.selection("--phase", "development")
        self.assertEqual(plan2["fallback"]["kind"], "incomplete_manifest")

    def test_doc_allowlist_is_narrow(self):
        self.repo.write("README.md", "# 合成仓库\n改动\n")
        plan = self.selection("--phase", "development")
        self.assertIsNone(plan["fallback"], "允许列表内的文档不需要产品测试")
        exempt = {item["path"]: item for item in plan["doc_exempt"]}
        self.assertIn("README.md", exempt)
        self.assertTrue(exempt["README.md"]["category"])
        self.assertEqual(plan["suggested"]["groups"], [])
        self.assertIn("unit", plan["suggested"]["not_applicable"], "确认不适用的组显式标注")

    def test_skill_guidance_and_guarded_docs_fall_back(self):
        self.repo.write("SKILL.md", "技能指引\n")
        plan = self.selection("--phase", "development")
        self.assertEqual(plan["fallback"]["kind"], "shared_resource",
                         "技能指引不能按扩展名放行")
        self.repo.commit_all("技能指引")
        self.repo.write("docs/verify/onboarding.md", "接入说明\n")
        plan2 = self.selection("--phase", "development")
        self.assertEqual(plan2["fallback"]["kind"], "guarded_doc")

    def test_rename_cannot_disguise_into_allowlist(self):
        self.repo.git("mv", "README.md", "NOTICE.md")
        plan = self.selection("--phase", "development")
        self.assertIsNotNone(plan["fallback"], "改名换姓进不了允许列表")
        self.assertEqual(plan["fallback"]["kind"], "unknown_path")
        self.assertIn("NOTICE.md", plan["fallback"]["reason"])
        self.assertEqual(plan["changes"]["renamed"], [{"from": "README.md", "to": "NOTICE.md"}],
                         "重命名两端都被识别")

    def test_added_and_deleted_tests_trigger_manifest_check(self):
        # 新增测试用不同内容：与被删文件内容相同时 git 的 rename 检测会把它配成重命名
        self.repo.write("tests/test_new_area.py",
                        UNIT_PASS.replace("AlphaTests", "NewAreaTests")
                                 .replace("test_alpha_ok", "test_new_area_ok"))
        plan = self.selection("--phase", "development")
        self.assertEqual(plan["fallback"]["kind"], "incomplete_manifest",
                         "新增测试未纳入映射，触发清单覆盖检查并保守回退")
        self.assertIn("tests/test_new_area.py", plan["manifest_check"]["new_tests"])
        self.repo.git("add", "-A")
        plan2 = self.selection("--phase", "development")
        self.assertIn("tests/test_new_area.py", plan2["manifest_check"]["new_tests"],
                      "新增测试提交前后都触发清单覆盖检查")
        # 先把新增测试提交进基线，再删除另一个测试文件：差异里只有删除，
        # 不会被 git 的 rename 检测把新增与删除配对成重命名
        self.repo.commit_all("新增区域测试")
        self.repo.git("rm", "-q", "tests/test_synth.py")
        plan3 = self.selection("--phase", "development")
        self.assertEqual(plan3["fallback"]["kind"], "incomplete_manifest")
        self.assertIn("tests/test_synth.py", plan3["manifest_check"]["deleted_tests"])

    def test_modified_test_suggests_itself(self):
        self.repo.write("tests/test_synth.py", UNIT_PASS + "\n# 改动\n")
        plan = self.selection("--phase", "development")
        self.assertIsNone(plan["fallback"], "修改已有测试是可解释的常见情形")
        self.assertIn("tests/test_synth.py", plan["suggested"]["selectors"],
                      "测试自身变化建议运行自身")

    def test_fallback_suggestion_keeps_matched_serve_check_and_scenarios(self):
        # R2 回归：回退建议取并集——命中规则的 serve-check 与场景计划不能被丢弃
        self.repo.write("skill/annotation-everything/scripts/serve.py",
                        SERVE_STUB + "\n# 合成变更\n")
        self.repo.write("scratch.xyz", "?\n")
        plan = self.selection("--phase", "development")
        self.assertIsNotNone(plan["fallback"], "未分类路径触发回退")
        self.assertEqual(plan["fallback"]["kind"], "unknown_path")
        matched = {item["id"] for item in plan["matched_rules"]}
        self.assertIn("r-serve", matched, "回退时命中规则仍保留在计划里")
        suggested = plan["suggested"]
        self.assertEqual(suggested["groups"], ["unit", "serve-check"],
                         "回退建议保留命中规则的 serve-check")
        self.assertTrue(suggested["serve_check"])
        self.assertIn("skill-search", suggested["scenarios"],
                      "回退建议保留命中规则的场景计划")

    def test_issue_phase_does_not_auto_start_tests(self):
        self.repo.write("tools/synth-cli.py", "v1\n")
        plan = self.selection("--phase", "issue")
        self.assertFalse(plan["suggested"]["runs_tests"], "需求阶段不自动启动测试")
        self.assertEqual(plan["suggested"]["groups"], [])
        notes = "\n".join(plan["suggested"]["notes"])
        self.assertIn("unit-related", notes, "缺陷复现的显式请求入口写进说明")

    def test_pr_phase_requires_full_acceptance(self):
        self.repo.write("tools/synth-cli.py", "v1\n")
        plan = self.selection("--phase", "pr")
        self.assertIn("all", plan["suggested"]["groups"], "PR 收敛完成完整验收")
        notes = "\n".join(plan["suggested"]["notes"])
        self.assertIn("CI", notes, "双 Python 全量与浏览器责任由 CI 说明")

    def test_ci_phase_matches_matrix(self):
        self.repo.write("tools/synth-cli.py", "v1\n")
        plan = self.selection("--phase", "ci")
        self.assertEqual(plan["suggested"]["groups"], ["unit", "serve-check"],
                         "CI 阶段与现有矩阵一致：完整单元 + 服务安全自检")

    def test_dict_rule_keeps_global_contract(self):
        self.repo.write("locales/zh-CN.json", '{"a": "甲", "b": "乙"}\n')
        plan = self.selection("--phase", "development")
        matched = {item["id"] for item in plan["matched_rules"]}
        self.assertIn("r-dict", matched)
        self.assertIn("tests/test_i18n_catalog_contract.py", plan["suggested"]["selectors_effective"],
                      "词典功能局部组包含全局契约")

    def test_uncommitted_changes_are_collected(self):
        self.repo.write("tools/synth-cli.py", "v1\n")
        self.repo.git("add", "tools/synth-cli.py")
        self.repo.write("tools/synth-cli.py", "v2\n")
        self.repo.write("notes-draft.md", "草稿\n")
        plan = self.selection("--phase", "development")
        self.assertIn("tools/synth-cli.py", plan["changes"]["staged"])
        self.assertIn("tools/synth-cli.py", plan["changes"]["unstaged"])
        self.assertIn("notes-draft.md", plan["changes"]["untracked"])
        self.assertIn("notes-draft.md", plan["unknown_paths"], "未分类的未提交文件保守回退")

    def test_baseline_change_changes_the_plan(self):
        self.repo.write("tools/synth-cli.py", "v1\n")
        self.repo.commit_all("CLI v1")
        first = self.repo.git("rev-parse", "HEAD").stdout.strip()
        self.repo.write("README.md", "# 改\n")
        self.repo.commit_all("文档改动")
        with_baseline = self.selection("--phase", "development", "--baseline", first)
        self.assertIsNone(with_baseline["fallback"], "基线之后只有允许列表内的文档")
        self.assertEqual(with_baseline["suggested"]["groups"], [])
        head_plan = self.selection("--phase", "development")
        self.assertIsNotNone(head_plan["fallback"], "默认基线（HEAD）下没有差异")
        self.assertEqual(head_plan["fallback"]["kind"], "no_changes",
                         "空差异不解释成不用测，保守按完整检查")
        self.assertEqual(with_baseline["baseline"]["source"], "explicit")


class SelectionShadowTests(VerifyEntryTestCase):
    """影子对照：run 默认记录建议范围并对照实际完整检查，不改变既有门禁。"""

    def setUp(self):
        super().setUp()
        self.repo.write("tests/e2e_scenarios.py", E2E_SCENARIOS_STUB)
        self.map_path = self.repo.write(
            "selection-map.json", json.dumps(SELECTION_MAP_OK, ensure_ascii=False, indent=2))
        self.sel_env = {"AE_SELECTION_MAP": str(self.map_path)}
        # 映射与场景 stub 属于测试基建：提交进基线，避免被当成未分类改动
        self.repo.commit_all("选测测试基建")

    def run_shadow(self, *args: str, env: dict | None = None,
                   timeout: float = 120) -> tuple:
        merged = dict(self.sel_env)
        if env:
            merged.update(env)
        p = self.repo.run_verify("run", *args, extra_env=merged, timeout=timeout)
        record = read_record(self.repo.artifact_dir, self.repo.latest_run_id())
        return p, record

    def test_full_group_suggestions_cover_failures_without_false_omissions(self):
        self.repo.write("tests/test_synth.py", UNIT_PASS.replace("1 + 1, 2", "1 + 1, 3"))
        self.repo.write("tests/e2e_browser.py", BROWSER_SCENARIO_STUB, executable=True)
        self.repo.commit_all("合成失败与场景记录")
        self.repo.write("tools/synth-cli.py", "v1\n")
        for phase, groups, expected_groups in (
                ("pr", ["--group", "all"], {"unit", "serve-check", "browser"}),
                ("ci", ["--group", "unit", "--group", "serve-check"], {"unit", "serve-check"})):
            with self.subTest(phase=phase):
                p, record = self.run_shadow(*groups, "--phase", phase,
                    env={"SYNTH_SERVE_FAILED": "1", "SYNTH_SCENARIO_STATUS": "failed"})
                self.assertEqual(p.returncode, 1, p.stdout + p.stderr)
                self.assertIsNone(record["context"]["selection"]["fallback"])
                shadow = record["shadow"]
                self.assertTrue(shadow["comparable"])
                self.assertEqual({item["group"] for item in shadow["failures"]}, expected_groups)
                self.assertEqual(shadow["candidate_omissions"], [],
                                 "完整组已覆盖失败，不能根据空 selectors 误报遗漏")

    def test_wildcard_only_expands_for_a_matching_rule(self):
        import copy
        payload = copy.deepcopy(SELECTION_MAP_OK)
        next(rule for rule in payload["rules"] if rule["id"] == "r-page")["scenarios"] = ["*"]
        self.map_path.write_text(json.dumps(payload), encoding="utf-8")
        self.repo.write("tests/e2e_browser.py", BROWSER_SCENARIO_STUB, executable=True)
        self.repo.commit_all("合成全场景映射")
        self.repo.write("tools/synth-cli.py", "v1\n")
        plan = self.repo.run_verify("plan", "--phase", "development", "--json",
                                   extra_env=self.sel_env)
        self.assertEqual(plan.returncode, 0, plan.stdout + plan.stderr)
        self.assertEqual(json.loads(plan.stdout)["selection"]["suggested"]["scenarios"], [],
                         "未命中的全场景规则不能把 CLI 建议扩大到浏览器")
        self.repo.write("templates/page.html", "合成模板\n")
        p, record = self.run_shadow("--group", "all", "--phase", "development",
                                   env={"SYNTH_SCENARIO_STATUS": "failed"})
        self.assertEqual(p.returncode, 1, p.stdout + p.stderr)
        self.assertEqual(record["shadow"]["browser_planned_scenarios"],
                         ["server-mode", "skill-search"])
        self.assertEqual(record["shadow"]["candidate_omissions"], [],
                         "全场景覆盖必须识别实际失败场景")
        self.repo.write("unknown.txt", "触发回退\n")
        fallback_plan = self.repo.run_verify("plan", "--phase", "development", "--json",
                                            extra_env=self.sel_env)
        self.assertEqual(json.loads(fallback_plan.stdout)["selection"]["suggested"]["scenarios"],
                         ["server-mode", "skill-search"], "回退也保留展开后的场景")

    def test_non_utf8_override_falls_back_and_still_executes_gate(self):
        self.map_path.write_bytes(b"\xff\xfe")
        p, record = self.run_shadow("--phase", "development")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertEqual(record["context"]["selection"]["fallback"]["kind"], "broken_rules")
        self.assertGreater(record["result"]["counts"]["cases"], 0, "损坏映射不能阻止实际门禁")

    def test_missing_full_unit_group_is_not_comparable(self):
        self.repo.write("tools/synth-cli.py", "v1\n")
        for group in ("serve-check", "browser"):
            with self.subTest(group=group):
                p, record = self.run_shadow("--group", group, "--phase", "development")
                self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
                self.assertFalse(record["shadow"]["comparable"])
                self.assertIn("完整单元", record["shadow"]["incomparable_reason"])

    def test_fallback_keeps_observed_failures(self):
        self.repo.write("tests/test_synth.py", UNIT_PASS.replace("1 + 1, 2", "1 + 1, 3"))
        self.repo.commit_all("合成失败")
        p, record = self.run_shadow("--phase", "development")
        self.assertEqual(p.returncode, 1, p.stdout + p.stderr)
        self.assertIsNotNone(record["context"]["selection"]["fallback"])
        self.assertTrue(record["shadow"]["failures"], "回退只禁止遗漏判断，不丢实际失败")
        self.assertEqual(record["shadow"]["candidate_omissions"], [])

    def test_incomparable_input_keeps_observed_failures(self):
        self.repo.write("tests/test_synth.py", CHURN_BODY.replace("self.assertTrue(True)",
                                                                "self.fail('合成失败')"))
        self.repo.commit_all("合成输入变化与失败")
        p, record = self.run_shadow("--phase", "development")
        self.assertEqual(p.returncode, 1, p.stdout + p.stderr)
        self.assertFalse(record["shadow"]["comparable"])
        self.assertTrue(record["shadow"]["failures"], "不可比较不丢实际失败")
        self.assertEqual(record["shadow"]["candidate_omissions"], [])

    def test_run_records_selection_and_shadow_without_changing_gate(self):
        self.repo.write("tools/synth-cli.py", "v1\n")
        p, record = self.run_shadow("--phase", "development", "--reason", "影子样本")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertEqual(record["result"]["outcome"], "passed", "影子模式不改变既有完整门禁的结论")
        selection = record["context"]["selection"]
        self.assertEqual(selection["schema"], "ae-verify-selection/1")
        self.assertEqual(selection["strategy_version"], 7)
        self.assertEqual(selection["suggested"]["groups"], ["unit-related"])
        shadow = record["shadow"]
        self.assertEqual(shadow["schema"], "ae-verify-shadow/1")
        self.assertTrue(shadow["comparable"])
        self.assertIsNone(shadow["incomparable_reason"])
        self.assertEqual(shadow["run_outcome"], "passed", "可比较样本同时给出整次运行结论")
        self.assertTrue(shadow["usable_as_evidence"], "可比较样本同时给出证据可用性")
        self.assertEqual(shadow["suggested_groups"], ["unit-related"])
        self.assertEqual(shadow["executed_groups"], ["unit"])
        self.assertTrue(shadow["coverage"]["unit"])
        self.assertFalse(shadow["coverage"]["serve-check"], "未执行的组在覆盖标注里明示")
        self.assertFalse(shadow["coverage"]["browser"])
        self.assertEqual(shadow["candidate_omissions"], [], "完整门禁全绿时没有候选遗漏")
        shadow_file = self.repo.artifact_dir / "runs" / record["run_id"] / "shadow.json"
        self.assertTrue(shadow_file.is_file(), "影子对照单独落盘供 G / H 读取")
        self.assertEqual(json.loads(shadow_file.read_text(encoding="utf-8"))["schema"],
                         "ae-verify-shadow/1")

    def test_planted_miss_is_recorded_and_failure_kept(self):
        import copy
        payload = copy.deepcopy(SELECTION_MAP_OK)
        for rule in payload["rules"]:
            if rule["id"] == "r-serve":
                rule["selectors"] = ["tests/test_synth.py"]
                rule["serve_check"] = False
                rule["scenarios"] = []
        self.map_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        # 窄映射提交进基线；服务变更留在工作树未提交（合成仓库在 main 上，
        # 默认基线退化为 HEAD，提交过的变更相对基线不可见）
        self.repo.commit_all("窄映射")
        self.repo.write("skill/annotation-everything/scripts/serve.py",
                        SERVE_STUB + "\n# 合成变更\n")
        p, record = self.run_shadow("--group", "unit", "--group", "serve-check",
                                    "--phase", "development", env={"SYNTH_SERVE_FAILED": "1"})
        self.assertEqual(p.returncode, 1, "完整门禁的失败必须保留，不能被影子层吞掉")
        self.assertEqual(record["result"]["outcome"], "failed")
        serve_group = {group["name"]: group for group in record["groups"]}["serve-check"]
        self.assertEqual(serve_group["outcome"], "failed")
        shadow = record["shadow"]
        omissions = shadow["candidate_omissions"]
        self.assertTrue(omissions, "人工植入的漏选被对照识别")
        targets = {item["group"] for item in omissions}
        self.assertIn("serve-check", targets)
        self.assertTrue(all(item.get("attribution") for item in omissions), "每条候选遗漏带归因")
        self.assertTrue(shadow["rule_candidates"], "遗漏产生修正规则的候选")
        self.assertTrue(shadow["failures"], "实际失败清单保留在影子记录里")

    def test_input_change_marks_shadow_incomparable(self):
        self.repo.write("tests/test_synth.py", CHURN_BODY)
        self.repo.commit_all("慢测试")
        p, record = self.run_shadow("--group", "unit", "--phase", "development")
        self.assertEqual(record["input"]["covers_final_input"], False)
        shadow = record["shadow"]
        self.assertFalse(shadow["comparable"], "输入不匹配的运行不能计为选择遗漏")
        self.assertIn("输入", shadow["incomparable_reason"])
        self.assertEqual(shadow["candidate_omissions"], [], "不可比较时不产出遗漏判断")

    def test_timeout_run_is_not_compared(self):
        # R1 回归：超时的运行没有完整通过的执行可对照，影子不能判「可比较」
        self.repo.write("tools/synth-cli.py", "v1\n")
        p, record = self.run_shadow("--phase", "development", "--timeout", "0.001")
        self.assertEqual(p.returncode, 124, p.stdout + p.stderr)
        self.assertEqual(record["result"]["outcome"], "timeout")
        self.assertFalse(record["result"]["usable_as_evidence"])
        shadow = record["shadow"]
        self.assertEqual(shadow["schema"], "ae-verify-shadow/1")
        self.assertFalse(shadow["comparable"], "超时的运行不能计为选择遗漏的对照")
        self.assertIn("超时", shadow["incomparable_reason"])
        self.assertEqual(shadow["candidate_omissions"], [], "不可比较时不产出遗漏判断")
        self.assertEqual(shadow["rule_candidates"], [])
        self.assertEqual(shadow["run_outcome"], "timeout", "shadow 顶层带整次运行结论")
        self.assertFalse(shadow["usable_as_evidence"], "shadow 顶层带证据可用性")

    def test_partial_run_is_not_compared_as_full_check(self):
        self.repo.write("tools/synth-cli.py", "v1\n")
        p, record = self.run_shadow("unit-related", "tests/test_synth.py", "--phase", "development")
        shadow = record["shadow"]
        self.assertFalse(shadow["comparable"], "局部执行不是完整检查，不产出遗漏判断")
        self.assertIn("完整", shadow["incomparable_reason"])
        self.assertEqual(shadow["candidate_omissions"], [])

    def test_report_shows_suggestion_and_actual_side_by_side(self):
        self.repo.write("tools/synth-cli.py", "v1\n")
        self.run_shadow("--phase", "development")
        p = self.repo.run_verify("report", "--latest")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertIn("影子", p.stdout, "报告并列呈现影子对照")
        self.assertIn("unit-related", p.stdout, "建议范围出现在报告里")

    def test_browser_scenarios_stay_at_plan_level_when_not_executed(self):
        self.repo.write("skill/annotation-everything/scripts/serve.py", SERVE_STUB + "\n# 合成变更\n")
        p, record = self.run_shadow("--group", "unit", "--phase", "development")
        shadow = record["shadow"]
        self.assertFalse(shadow["coverage"]["browser"], "浏览器未执行")
        self.assertIn("skill-search", shadow["browser_planned_scenarios"],
                      "浏览器场景只在计划层对照")


class SelectionSampleReplayTests(unittest.TestCase):
    """docs/verify/shadow-samples.json 的合成样本可复现（R3）：重放变更形态，
    记录字段必须与样本一致；S3 / S13 / S14 由 SelectionShadowTests 的既有用例覆盖。"""

    SAMPLES_PATH = ROOT / "docs" / "verify" / "shadow-samples.json"
    REPLAY_IDS = ("S1", "S2", "S4", "S5", "S6", "S7", "S8", "S9", "S10", "S11", "S12")

    def setUp(self):
        self.counter = 0

    def make_repo(self) -> SynthRepo:
        self.counter += 1
        temp = tempfile.TemporaryDirectory(prefix="verify-sample-replay-")
        self.addCleanup(temp.cleanup)
        lock = tempfile.TemporaryDirectory(prefix="verify-sample-replay-lock-")
        self.addCleanup(lock.cleanup)
        repo = SynthRepo(Path(temp.name) / ("repo-%d" % self.counter))
        repo.test_lock_dir = Path(lock.name)
        # 与 SelectionShadowTests 相同的测试基建：场景 stub + 覆盖映射提交进基线
        repo.write("tests/e2e_scenarios.py", E2E_SCENARIOS_STUB)
        map_path = repo.write(
            "selection-map.json", json.dumps(SELECTION_MAP_OK, ensure_ascii=False, indent=2))
        repo.commit_all("选测测试基建")
        self.sel_env = {"AE_SELECTION_MAP": str(map_path)}
        return repo

    def prepare(self, repo: SynthRepo, sample_id: str) -> None:
        """样本形态的变更（与 shadow-samples.json 的 change 描述一致）。"""
        if sample_id == "S1":
            repo.write("README.md", "# 合成仓库\n\n影子样本说明\n")
        elif sample_id == "S2":
            repo.write("SKILL.md", "# 技能指引\n")
        elif sample_id == "S4":
            repo.write("templates/viewer.html", "<!-- 合成页面 v1 -->\n")
            repo.commit_all("合成页面")
            repo.write("templates/viewer.html", "<!-- 合成页面 v2 -->\n")
        elif sample_id == "S5":
            repo.write("skill/annotation-everything/scripts/serve.py",
                       SERVE_STUB + "\n# 合成变更\n")
        elif sample_id == "S6":
            repo.write("locales/zh-CN.json", '{"a": "甲", "b": "乙"}\n')
        elif sample_id == "S7":
            repo.write("fixtures/sample/new.json", "{}\n")
        elif sample_id == "S8":
            repo.write("scratch.xyz", "?\n")
        elif sample_id == "S9":
            repo.write("tests/test_new_area.py",
                       UNIT_PASS.replace("AlphaTests", "NewAreaTests")
                                .replace("test_alpha_ok", "test_new_area_ok"))
        elif sample_id == "S10":
            repo.git("rm", "-q", "tests/test_synth.py")
        elif sample_id == "S11":
            repo.write("tools/synth-cli.py", "#!/usr/bin/env python3\nprint('cli')\n")
            repo.commit_all("合成 CLI")
            repo.git("mv", "tools/synth-cli.py", "tools/renamed-cli.py")
            repo.write("tools/renamed-cli.py", "#!/usr/bin/env python3\nprint('cli v2')\n")
        elif sample_id == "S12":
            entry = repo.path / "verify.py"
            entry.write_text(entry.read_text(encoding="utf-8") + "\n# 合成变更\n",
                             encoding="utf-8")
        else:
            raise AssertionError("未登记的重放形态：%s" % sample_id)

    def assert_replays_to_sample(self, repo: SynthRepo, sample: dict) -> None:
        where = sample["id"]
        p = repo.run_verify("run", "--group", "unit", "--group", "serve-check",
                            "--phase", "development", extra_env=dict(self.sel_env))
        self.assertEqual(p.returncode, sample["exit_status"],
                         where + " 退出码与样本不一致：" + p.stdout + p.stderr)
        record = read_record(repo.artifact_dir, repo.latest_run_id())
        selection = record["context"]["selection"]
        shadow = record["shadow"]
        self.assertEqual(record["result"]["outcome"], sample["outcome"], where)
        fallback = selection.get("fallback")
        self.assertEqual(fallback["kind"] if fallback else None, sample["fallback"], where)
        if fallback is not None:
            self.assertTrue(fallback["reason"], where + " 回退必须带原因")
            self.assertEqual(fallback["reason"], sample.get("fallback_reason"),
                             where + " 回退原因与样本记录一致")
        self.assertEqual(sorted(item["id"] for item in selection["matched_rules"]),
                         sample["matched_rules"], where)
        suggested = selection["suggested"]
        self.assertEqual(suggested["groups"], sample["suggested_groups"], where)
        self.assertEqual(suggested.get("not_applicable"), sample.get("not_applicable"), where)
        self.assertEqual(shadow["suggested_selectors"], sample["suggested_selectors"], where)
        self.assertEqual(shadow["browser_planned_scenarios"],
                         sample["suggested_scenarios"], where)
        self.assertEqual(shadow["comparable"], sample["comparable"], where)
        if sample["comparable"]:
            self.assertIsNone(shadow["incomparable_reason"], where)
        else:
            self.assertEqual(shadow["incomparable_reason"],
                             sample["incomparable_reason"], where)
        self.assertEqual(shadow["coverage"], sample["coverage"], where)
        expected_failures = sample["failures"]
        if isinstance(expected_failures, int):
            self.assertEqual(len(shadow["failures"]), expected_failures, where)
        else:
            self.assertEqual(shadow["failures"], expected_failures, where)
        expected_omissions = sample["candidate_omissions"]
        if isinstance(expected_omissions, int):
            self.assertEqual(len(shadow["candidate_omissions"]), expected_omissions, where)
        else:
            self.assertEqual(
                [(item["group"], item["target"], item["attribution"])
                 for item in shadow["candidate_omissions"]],
                [(item["group"], item["target"], item["attribution"])
                 for item in expected_omissions], where)

    def test_synthetic_samples_replay_to_recorded_fields(self):
        samples = {item["id"]: item for item in
                   json.loads(self.SAMPLES_PATH.read_text(encoding="utf-8"))["synthetic_samples"]}
        for sample_id in self.REPLAY_IDS:
            with self.subTest(sample=sample_id):
                repo = self.make_repo()
                self.prepare(repo, sample_id)
                self.assert_replays_to_sample(repo, samples[sample_id])


class SelectionBuiltinMapTests(unittest.TestCase):
    """内置映射自身的清单完整性：策略声明属于外部行为，直接校验。"""

    def test_builtin_map_declares_real_repo_manifest(self):
        sys.path.insert(0, str(ROOT))
        import verify_selection as vs
        problems = vs.validate_builtin_map(repo_root=ROOT)
        self.assertEqual(problems, [], "内置映射的选择器与场景必须在真实仓库里全部可解析")

    def test_builtin_map_falls_back_safely_in_foreign_repo(self):
        # 不注入映射：内置映射的选择器在合成仓库不存在，必须安全回退而不是崩溃或空建议
        temp = tempfile.TemporaryDirectory(prefix="verify-selection-builtin-")
        self.addCleanup(temp.cleanup)
        lock_temp = tempfile.TemporaryDirectory(prefix="verify-selection-lock-")
        self.addCleanup(lock_temp.cleanup)
        repo = SynthRepo(Path(temp.name))
        repo.test_lock_dir = Path(lock_temp.name)
        repo.write("mystery.xyz", "?\n")
        p = repo.run_verify("plan", "--json", "--phase", "development")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        selection = json.loads(p.stdout)["selection"]
        self.assertEqual(selection["strategy_source"], "builtin")
        self.assertIsNotNone(selection["fallback"], "外来仓库对内置映射保守回退")


# ── 证据复用与流程接入（issue #56） ─────────────────────────────────────────

# 带执行痕迹的合成单元测试：每次真实执行都在 SYNTH_TOUCH 指向的文件（仓库外）追加一行。
# 「没有重新执行」用真实执行事件证明，不靠报告文字。
UNIT_TOUCH = '''import os
import unittest


class TouchTests(unittest.TestCase):
    def test_touch(self):
        with open(os.environ["SYNTH_TOUCH"], "a", encoding="utf-8") as fh:
            fh.write("unit\\n")
        self.assertTrue(True)
'''

# 带执行痕迹的合成服务安全自检：摘要行与 SERVE_STUB 相同，另追加执行痕迹
SERVE_TOUCH_STUB = '''#!/usr/bin/env python3
import os
import sys

touch = os.environ.get("SYNTH_SERVE_TOUCH")
if touch:
    with open(touch, "a", encoding="utf-8") as fh:
        fh.write("serve\\n")
total = int(os.environ.get("SYNTH_SERVE_TOTAL", "3"))
failed = int(os.environ.get("SYNTH_SERVE_FAILED", "0"))
for index in range(total - failed):
    print("通过  合成自检项 %d" % index)
for index in range(failed):
    print("失败  合成自检项 %d" % (total - failed + index))
print("\\n%d/%d 通过" % (total - failed, total))
sys.exit(1 if failed else 0)
'''

# 带执行痕迹的合成浏览器入口：摘要行与 BROWSER_STUB 相同，另追加执行痕迹
BROWSER_TOUCH_STUB = '''#!/usr/bin/env python3
import os
import sys
import time

touch = os.environ.get("SYNTH_BROWSER_TOUCH")
if touch:
    with open(touch, "a", encoding="utf-8") as fh:
        fh.write("browser\\n")
total = int(os.environ.get("SYNTH_BROWSER_TOTAL", "3"))
failed = int(os.environ.get("SYNTH_BROWSER_FAILED", "0"))
time.sleep(0.2)
for index in range(total - failed):
    print("PASS  合成场景 %d" % index)
for index in range(failed):
    print("FAIL  合成场景 %d" % (total - failed + index))
print("\\n%d/%d 通过" % (total - failed, total))
sys.exit(1 if failed else 0)
'''


class EvidenceReuseTests(VerifyEntryTestCase):
    """证据复用（issue #56）：同输入、同环境、范围覆盖且当前有效的通过记录跨阶段引用。

    复用判定从紧：任一条件不满足就实际执行，失效证据绝不冒充通过。
    组的执行痕迹（trace）证明是否真的重跑：复用后行数不增加。
    """

    def setUp(self):
        super().setUp()
        self.unit_touch = Path(self.temp.name) / "unit-touch.txt"
        self.serve_touch = Path(self.temp.name) / "serve-touch.txt"
        self.browser_touch = Path(self.temp.name) / "browser-touch.txt"
        self.repo.write("tests/test_synth.py", UNIT_TOUCH)
        self.repo.write("tests/e2e_scenarios.py", E2E_SCENARIOS_STUB)
        self.repo.write("skill/annotation-everything/scripts/serve.py",
                        SERVE_TOUCH_STUB, executable=True)
        self.repo.write("tests/e2e_browser.py", BROWSER_TOUCH_STUB, executable=True)
        self.repo.commit_all("合成执行痕迹基建")
        self.trace_env = {"SYNTH_TOUCH": str(self.unit_touch),
                          "SYNTH_SERVE_TOUCH": str(self.serve_touch),
                          "SYNTH_BROWSER_TOUCH": str(self.browser_touch)}

    # -- 工具 -------------------------------------------------------------

    def verify(self, *args, **kwargs):
        extra = dict(self.trace_env)
        extra.update(kwargs.pop("extra_env", {}) or {})
        return self.repo.run_verify(*args, extra_env=extra, **kwargs)

    def trace(self, path: Path) -> int:
        return len(path.read_text(encoding="utf-8").splitlines()) if path.exists() else 0

    def record(self) -> dict:
        return read_record(self.repo.artifact_dir, self.repo.latest_run_id())

    def decision(self, record: dict, group: str) -> dict:
        return next(item for item in record["reuse"]["decisions"] if item["group"] == group)

    def group(self, record: dict, name: str) -> dict:
        return next(item for item in record["groups"] if item["name"] == name)

    def index_entries(self) -> list:
        text = (self.repo.artifact_dir / "index.jsonl").read_text(encoding="utf-8")
        return [json.loads(line) for line in text.splitlines() if line.strip()]

    def backdate(self, run_id: str, hours: float) -> None:
        """把来源记录的结束时间改到 hours 小时前；负数表示未来（时间回退形态）。"""
        path = self.repo.artifact_dir / "runs" / run_id / "record.json"
        payload = json.loads(path.read_text(encoding="utf-8"))
        stamp = (datetime.datetime.now(datetime.timezone.utc)
                 - datetime.timedelta(hours=hours)).isoformat()
        payload["ended_at"] = stamp
        payload["timing"]["ended_at"] = stamp
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    # -- 跨阶段演示 ---------------------------------------------------------

    def test_cross_stage_demo_reuses_evidence_and_pr_gate_reruns(self):
        """开发验证 → 审查 → PR 整理引用原有效结果（0 次重跑）；pr 门禁仍实际执行。"""
        p = self.verify("run", "--group", "serve-check", "--phase", "development",
                        "--issue", "56", "--reason", "开发验证")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        dev_serve = self.repo.latest_run_id()
        p = self.verify("run", "unit-related", "tests/test_synth.py",
                        "--phase", "development", "--reason", "开发验证")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        dev_unit = self.repo.latest_run_id()
        self.assertEqual(self.trace(self.serve_touch), 1)
        self.assertEqual(self.trace(self.unit_touch), 1)

        # 审查阶段：读取有效证据，不重跑
        p = self.verify("run", "--group", "serve-check", "--phase", "review",
                        "--reason", "独立审查读取有效证据")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        review_serve = self.record()
        self.assertEqual(self.group(review_serve, "serve-check")["outcome"], "reused")
        self.assertEqual(self.decision(review_serve, "serve-check")["source_run_id"], dev_serve)
        self.assertEqual(self.trace(self.serve_touch), 1, "审查阶段没有重新执行 serve-check")

        p = self.verify("run", "unit-related", "tests/test_synth.py", "--phase", "review",
                        "--reason", "独立审查读取有效证据")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        review_unit = self.record()
        self.assertEqual(self.group(review_unit, "unit-related")["outcome"], "reused")
        self.assertEqual(self.decision(review_unit, "unit-related")["source_run_id"], dev_unit)
        self.assertEqual(self.trace(self.unit_touch), 1, "审查阶段没有重新执行相关单元")

        # PR 整理（manual 阶段）：继续引用，且来源是实际执行的记录而不是复用记录
        p = self.verify("run", "--group", "serve-check", "--phase", "manual",
                        "--reason", "PR 整理，引用已有验证")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        tidy = self.record()
        self.assertEqual(self.group(tidy, "serve-check")["outcome"], "reused")
        self.assertEqual(self.decision(tidy, "serve-check")["source_run_id"], dev_serve,
                         "复用来源必须是实际执行的记录，不能链式引用另一条复用记录")
        self.assertEqual(self.trace(self.serve_touch), 1)
        self.assertEqual(self.trace(self.unit_touch), 1,
                         "开发验证 → 审查 → PR 整理全程 0 次重复执行")

        # 引用不重复计入通过数量：引用组的检查数不计入 result.counts
        self.assertEqual(review_serve["result"]["counts"]["checks"], 0)
        self.assertEqual(tidy["result"]["counts"]["checks"], 0)
        self.assertEqual(tidy["result"]["outcome"], "passed")
        self.assertTrue(tidy["result"]["usable_as_evidence"])

        # pr 门禁：按口径必须实际执行，不引用
        p = self.verify("run", "--group", "serve-check", "--phase", "pr",
                        "--reason", "PR 收敛门禁")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        gate = self.record()
        self.assertEqual(self.group(gate, "serve-check")["outcome"], "passed")
        self.assertEqual(self.decision(gate, "serve-check")["decision"], "run")
        self.assertIn("门禁", self.decision(gate, "serve-check")["reason"])
        self.assertFalse(gate["reuse"]["enabled"])
        self.assertEqual(self.trace(self.serve_touch), 2)

        # 索引带引用清单，供 H 项汇总
        by_id = {entry["run_id"]: entry for entry in self.index_entries()}
        self.assertEqual(by_id[review_serve["run_id"]]["reused_groups"], ["serve-check"])
        self.assertEqual(by_id[gate["run_id"]]["reused_groups"], [])

        # report 展示证据链：来源 run、时间与指纹（门禁运行没有引用可展示）
        p = self.repo.run_verify("report", "--run", tidy["run_id"])
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertIn("证据复用", p.stdout)
        self.assertIn("引用已有验证", p.stdout)

    # -- 输入与环境变化 -------------------------------------------------------

    def test_single_byte_change_forces_rerun(self):
        self.verify("run", "--group", "serve-check", "--phase", "development")
        # 改动对象是非文档有效输入（README 属于文档允许列表，见文档无关等价用例）
        self.repo.write("locales/en.json", '{"a": "A!"}\n')
        p = self.verify("run", "--group", "serve-check", "--phase", "review")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        item = self.decision(self.record(), "serve-check")
        self.assertEqual(item["decision"], "run")
        self.assertIn("输入指纹不同", item["reason"])
        self.assertEqual(self.trace(self.serve_touch), 2)

    def test_commit_between_runs_keeps_fingerprints_apart(self):
        """未提交改动与提交落地是不同输入（HEAD 参与指纹）：互不沿用。"""
        self.verify("run", "--group", "serve-check", "--phase", "development")
        self.repo.write("locales/en.json", '{"a": "A", "b": "B"}\n')
        p = self.verify("run", "--group", "serve-check", "--phase", "development")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.repo.commit_all("改动落地")
        p = self.verify("run", "--group", "serve-check", "--phase", "review")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        item = self.decision(self.record(), "serve-check")
        self.assertEqual(item["decision"], "run")
        self.assertIn("输入指纹不同", item["reason"])
        self.assertEqual(self.trace(self.serve_touch), 3)

    def test_deleted_file_invalidates_evidence(self):
        self.verify("run", "--group", "serve-check", "--phase", "development")
        self.repo.git("rm", "-q", "locales/zh-CN.json")
        p = self.verify("run", "--group", "serve-check", "--phase", "review")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        item = self.decision(self.record(), "serve-check")
        self.assertEqual(item["decision"], "run")
        self.assertIn("输入指纹不同", item["reason"])
        self.assertEqual(self.trace(self.serve_touch), 2)

    def test_doc_only_change_still_reuses(self):
        """验收：仅变化显式允许且已证明无关的文档时，独立测试组仍可复用。"""
        self.verify("run", "--group", "serve-check", "--phase", "development")
        self.repo.write("README.md", "# 合成仓库\n\n只改了说明文档\n")
        p = self.verify("run", "--group", "serve-check", "--phase", "review")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        item = self.decision(self.record(), "serve-check")
        self.assertEqual(item["decision"], "reuse")
        self.assertIn("文档", item["reason"])
        self.assertEqual(self.trace(self.serve_touch), 1)

    def test_committed_doc_only_change_still_reuses(self):
        """文档改动已提交（HEAD 变化）时同样按文档无关等价处理。"""
        self.verify("run", "--group", "serve-check", "--phase", "development")
        self.repo.write("README.md", "# 合成仓库\n\n提交的文档改动\n")
        self.repo.commit_all("文档改动")
        p = self.verify("run", "--group", "serve-check", "--phase", "review")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        item = self.decision(self.record(), "serve-check")
        self.assertEqual(item["decision"], "reuse")
        self.assertIn("文档", item["reason"])
        self.assertEqual(self.trace(self.serve_touch), 1)

    def test_doc_change_with_code_change_reruns(self):
        """文档之外还有任何有效输入变化时无法证明无关，回退重新验证。"""
        self.verify("run", "--group", "serve-check", "--phase", "development")
        self.repo.write("README.md", "# 合成仓库\n\n文档也改了\n")
        self.repo.write("locales/en.json", '{"a": "A2"}\n')
        p = self.verify("run", "--group", "serve-check", "--phase", "review")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        item = self.decision(self.record(), "serve-check")
        self.assertEqual(item["decision"], "run")
        self.assertIn("非文档输入不同", item["reason"])
        self.assertEqual(self.trace(self.serve_touch), 2)

    def test_doc_equivalence_requires_builtin_strategy(self):
        """覆盖映射（AE_SELECTION_MAP）下文档等价停用：无法证明独立性就重跑。"""
        map_path = Path(self.temp.name) / "map.json"
        map_path.write_text(json.dumps(dict(SELECTION_MAP_OK)), encoding="utf-8")
        env = {"AE_SELECTION_MAP": str(map_path)}
        self.verify("run", "--group", "serve-check", "--phase", "development", extra_env=env)
        self.repo.write("README.md", "# 合成仓库\n\n覆盖映射下的文档改动\n")
        p = self.verify("run", "--group", "serve-check", "--phase", "review", extra_env=env)
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        item = self.decision(self.record(), "serve-check")
        self.assertEqual(item["decision"], "run")
        self.assertIn("无法证明文档无关", item["reason"])
        self.assertEqual(self.trace(self.serve_touch), 2)

    def test_environment_change_invalidates_evidence(self):
        self.verify("run", "--group", "serve-check", "--phase", "development")
        p = self.verify("run", "--group", "serve-check", "--phase", "review",
                        extra_env={"AE_LOCALE": "fr"})
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        item = self.decision(self.record(), "serve-check")
        self.assertEqual(item["decision"], "run")
        self.assertIn("环境不同", item["reason"])
        self.assertEqual(self.trace(self.serve_touch), 2)

    def test_baseline_change_invalidates_evidence(self):
        first_commit = self.repo.git("rev-parse", "HEAD~1").stdout.strip()
        self.verify("run", "--group", "serve-check", "--phase", "development")
        p = self.verify("run", "--group", "serve-check", "--phase", "review",
                        "--baseline", first_commit)
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        item = self.decision(self.record(), "serve-check")
        self.assertEqual(item["decision"], "run")
        self.assertIn("基线不同", item["reason"])
        self.assertEqual(self.trace(self.serve_touch), 2)

    # -- 时间与证据完整性 -----------------------------------------------------

    def test_expired_evidence_is_rejected(self):
        self.verify("run", "--group", "serve-check", "--phase", "development")
        self.backdate(self.repo.latest_run_id(), hours=25)
        p = self.verify("run", "--group", "serve-check", "--phase", "review")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        item = self.decision(self.record(), "serve-check")
        self.assertEqual(item["decision"], "run")
        self.assertIn("保鲜", item["reason"])
        self.assertEqual(self.trace(self.serve_touch), 2)

    def test_end_time_in_the_future_is_rejected(self):
        self.verify("run", "--group", "serve-check", "--phase", "development")
        self.backdate(self.repo.latest_run_id(), hours=-2)
        p = self.verify("run", "--group", "serve-check", "--phase", "review")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        item = self.decision(self.record(), "serve-check")
        self.assertEqual(item["decision"], "run")
        self.assertIn("未来", item["reason"])
        self.assertEqual(self.trace(self.serve_touch), 2)

    def test_tampered_source_log_is_rejected(self):
        self.verify("run", "--group", "serve-check", "--phase", "development")
        run_id = self.repo.latest_run_id()
        log = self.repo.artifact_dir / "runs" / run_id / "serve-check.log"
        log.write_text(log.read_text(encoding="utf-8") + "事后补写的一行\n", encoding="utf-8")
        p = self.verify("run", "--group", "serve-check", "--phase", "review")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        item = self.decision(self.record(), "serve-check")
        self.assertEqual(item["decision"], "run")
        self.assertIn("证据当前校验不可用", item["reason"])
        self.assertEqual(self.trace(self.serve_touch), 2)

    def test_sources_that_are_not_passes_are_rejected(self):
        self.repo.write("skill/annotation-everything/scripts/serve.py",
                        SERVE_TOUCH_STUB.replace('SYNTH_SERVE_FAILED", "0"',
                                                 'SYNTH_SERVE_FAILED", "1"'),
                        executable=True)
        self.repo.commit_all("自检失败形态")
        p = self.verify("run", "--group", "serve-check", "--phase", "development")
        self.assertEqual(p.returncode, 1, p.stdout + p.stderr)
        self.repo.write("skill/annotation-everything/scripts/serve.py",
                        SERVE_TOUCH_STUB, executable=True)
        self.repo.commit_all("自检恢复")
        p = self.verify("run", "--group", "serve-check", "--phase", "review")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        item = self.decision(self.record(), "serve-check")
        self.assertEqual(item["decision"], "run")
        self.assertIn("不能作为通过证据", item["reason"])
        self.assertEqual(self.trace(self.serve_touch), 2)

        # 半写 / 中止形态：state 不是 complete 的记录同样拒用。
        # 前一条通过记录（恢复后的 run 2）先回拨超期，让中止记录成为唯一候选，
        # 单独验证「中止」这一拒用原因。
        p = self.verify("run", "--group", "serve-check", "--phase", "review")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        source_id = self.repo.latest_run_id()
        path = self.repo.artifact_dir / "runs" / source_id / "record.json"
        payload = json.loads(path.read_text(encoding="utf-8"))
        payload["state"] = "aborted"
        path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        for entry in self.index_entries():
            if entry["run_id"] not in (source_id,) and entry["outcome"] == "passed":
                self.backdate(entry["run_id"], hours=25)
        p = self.verify("run", "--group", "serve-check", "--phase", "review")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        item = self.decision(self.record(), "serve-check")
        self.assertEqual(item["decision"], "run")
        self.assertIn("证据当前校验不可用", item["reason"])
        self.assertEqual(self.trace(self.serve_touch), 3)

    def test_records_without_reuse_policy_version_are_never_reused(self):
        """统一入口 G 之前的记录没有复用策略版本，不参与复用（迁移口径）。"""
        self.verify("run", "--group", "serve-check", "--phase", "development")
        path = self.repo.artifact_dir / "runs" / self.repo.latest_run_id() / "record.json"
        payload = json.loads(path.read_text(encoding="utf-8"))
        payload.pop("reuse")
        path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        p = self.verify("run", "--group", "serve-check", "--phase", "review")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        item = self.decision(self.record(), "serve-check")
        self.assertEqual(item["decision"], "run")
        self.assertIn("复用策略版本", item["reason"])
        self.assertEqual(self.trace(self.serve_touch), 2)

    # -- 版本与范围 -----------------------------------------------------------

    def test_strategy_version_change_invalidates(self):
        payload_v7 = dict(SELECTION_MAP_OK)
        payload_v8 = dict(SELECTION_MAP_OK)
        payload_v8["version"] = 8
        map_v7 = Path(self.temp.name) / "map-v7.json"
        map_v8 = Path(self.temp.name) / "map-v8.json"
        map_v7.write_text(json.dumps(payload_v7), encoding="utf-8")
        map_v8.write_text(json.dumps(payload_v8), encoding="utf-8")
        self.verify("run", "--group", "serve-check", "--phase", "development",
                    extra_env={"AE_SELECTION_MAP": str(map_v7)})
        p = self.verify("run", "--group", "serve-check", "--phase", "review",
                        extra_env={"AE_SELECTION_MAP": str(map_v8)})
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        item = self.decision(self.record(), "serve-check")
        self.assertEqual(item["decision"], "run")
        self.assertIn("策略版本不同", item["reason"])
        self.assertEqual(self.trace(self.serve_touch), 2)

    def test_scope_not_covered_reruns(self):
        self.repo.write("tests/test_extra.py", UNIT_TOUCH)
        self.repo.commit_all("新增合成模块")
        self.verify("run", "unit-related", "tests/test_synth.py", "--phase", "development")
        p = self.verify("run", "unit-related", "tests/test_synth.py", "tests/test_extra.py",
                        "--phase", "review")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        item = self.decision(self.record(), "unit-related")
        self.assertEqual(item["decision"], "run")
        self.assertIn("范围不覆盖", item["reason"])
        self.assertEqual(self.trace(self.unit_touch), 3, "两个模块都实际执行")

    def test_case_level_request_reruns_for_targeted_review(self):
        """按疑点点名到类 / 用例的选择器不复用：独立复测不受证据复用限制。"""
        self.verify("run", "unit-related", "tests/test_synth.py", "--phase", "development")
        p = self.verify("run", "unit-related", "tests.test_synth.TouchTests", "--phase", "review")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        item = self.decision(self.record(), "unit-related")
        self.assertEqual(item["decision"], "run")
        self.assertIn("按疑点复测", item["reason"])
        self.assertEqual(self.trace(self.unit_touch), 2)

    def test_host_state_groups_are_never_reused(self):
        """依赖宿主机状态的组指纹相同也不复用；纯逻辑组在同一记录里照常复用。"""
        self.verify("run", "--group", "browser", "--group", "serve-check",
                    "--phase", "development")
        p = self.verify("run", "--group", "browser", "--group", "serve-check",
                        "--phase", "review")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        record = self.record()
        browser = self.decision(record, "browser")
        serve = self.decision(record, "serve-check")
        self.assertEqual(browser["decision"], "run")
        self.assertIn("宿主机状态", browser["reason"])
        self.assertEqual(serve["decision"], "reuse")
        self.assertEqual(self.trace(self.browser_touch), 2, "浏览器组重新执行")
        self.assertEqual(self.trace(self.serve_touch), 1, "serve-check 引用已有验证")

    def test_sensitive_module_run_is_never_reused(self):
        """unit-related 命中资源敏感模块（host-state）时不复用。"""
        self.repo.write("tests/test_core.py", UNIT_TOUCH)
        self.repo.commit_all("合成敏感模块")
        self.verify("run", "unit-related", "tests/test_core.py", "--phase", "development")
        p = self.verify("run", "unit-related", "tests/test_core.py", "--phase", "review")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        item = self.decision(self.record(), "unit-related")
        self.assertEqual(item["decision"], "run")
        self.assertIn("宿主机状态", item["reason"])
        self.assertEqual(self.trace(self.unit_touch), 2)

    # -- 开关与阶段 -----------------------------------------------------------

    def test_no_reuse_flag_forces_full_rerun(self):
        self.verify("run", "--group", "serve-check", "--phase", "development")
        p = self.verify("run", "--group", "serve-check", "--phase", "review", "--no-reuse")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        record = self.record()
        self.assertFalse(record["reuse"]["enabled"])
        self.assertIn("--no-reuse", record["reuse"]["reason"])
        self.assertEqual(self.decision(record, "serve-check")["decision"], "run")
        self.assertEqual(self.trace(self.serve_touch), 2)

    def test_pr_and_ci_phases_do_not_reuse(self):
        self.verify("run", "--group", "serve-check", "--phase", "development")
        for phase in ("pr", "ci"):
            p = self.verify("run", "--group", "serve-check", "--phase", phase)
            self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
            item = self.decision(self.record(), "serve-check")
            self.assertEqual(item["decision"], "run")
            self.assertIn("门禁", item["reason"])
        self.assertEqual(self.trace(self.serve_touch), 3)

    # -- 报告呈现与引用链 -------------------------------------------------------

    def test_report_shows_the_reuse_chain(self):
        self.verify("run", "--group", "serve-check", "--phase", "development")
        first = self.repo.latest_run_id()
        self.verify("run", "--group", "serve-check", "--phase", "review",
                    "--reason", "独立审查")
        second = self.repo.latest_run_id()
        p = self.repo.run_verify("report", "--run", second)
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertIn("证据复用", p.stdout)
        self.assertIn("引用已有验证", p.stdout)
        self.assertIn(first, p.stdout, "报告列出被引用的 run ID")
        self.assertIn("serve-check", p.stdout)
        j = self.repo.run_verify("report", "--run", second, "--json")
        self.assertEqual(j.returncode, 0, j.stdout + j.stderr)
        payload = json.loads(j.stdout)
        self.assertTrue(payload["result"]["usable_as_evidence"])
        self.assertTrue(payload["reuse"]["enabled"])
        self.assertEqual(payload["reuse"]["decisions"][0]["source_run_id"], first)
        self.assertEqual(payload["groups"][0]["outcome"], "reused")
        self.assertEqual(payload["groups"][0]["reuse"]["source_run_id"], first)

    def test_report_revalidates_the_reuse_chain(self):
        """来源证据事后被篡改时，引用它的记录在报告里降级为不可用。"""
        self.verify("run", "--group", "serve-check", "--phase", "development")
        first = self.repo.latest_run_id()
        self.verify("run", "--group", "serve-check", "--phase", "review")
        second = self.repo.latest_run_id()
        log = self.repo.artifact_dir / "runs" / first / "serve-check.log"
        log.write_text(log.read_text(encoding="utf-8") + "事后补写的一行\n", encoding="utf-8")
        p = self.repo.run_verify("report", "--run", second)
        self.assertEqual(p.returncode, 1, p.stdout + p.stderr)
        self.assertIn("引用的证据 run %s" % first, p.stdout)
        self.assertIn("不能作为通过证据", p.stdout)
        j = self.repo.run_verify("report", "--run", second, "--json")
        self.assertEqual(j.returncode, 1, j.stdout + j.stderr)
        payload = json.loads(j.stdout)
        self.assertFalse(payload["result"]["usable_as_evidence"])
        self.assertTrue(any("引用的证据" in item for item in payload["report_validation"]["problems"]))
        # 原始记录不被改写
        record = read_record(self.repo.artifact_dir, second)
        self.assertTrue(record["result"]["usable_as_evidence"])

    # -- 降级 -----------------------------------------------------------------

    def test_broken_policy_module_disables_reuse(self):
        self.verify("run", "--group", "serve-check", "--phase", "development")
        self.repo.write("verify_evidence.py", "raise RuntimeError('合成损坏')\n")
        p = self.verify("run", "--group", "serve-check", "--phase", "review")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        record = self.record()
        self.assertFalse(record["reuse"]["enabled"])
        self.assertIn("加载失败", record["reuse"]["reason"])
        self.assertEqual(self.decision(record, "serve-check")["decision"], "run")
        self.assertEqual(self.trace(self.serve_touch), 2)

    def test_first_run_without_any_records_executes_everything(self):
        p = self.verify("run", "--group", "serve-check", "--phase", "development")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        record = self.record()
        self.assertTrue(record["reuse"]["enabled"])
        item = self.decision(record, "serve-check")
        self.assertEqual(item["decision"], "run")
        self.assertIn("没有可引用的已有记录", item["reason"])
        self.assertEqual(item["attempts"], [])
