"""持续效率报告的 CLI 契约；历史、环境和 PR 事件均为合成数据。"""
from __future__ import annotations

import datetime
import base64
import hashlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
EPOCH = datetime.datetime(2026, 1, 1, tzinfo=datetime.timezone.utc)


def stamp(seconds=0):
    return (EPOCH + datetime.timedelta(seconds=seconds)).isoformat()


class EfficiencyCliTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="ae-efficiency-synthetic-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / ".verify"
        self.root.mkdir()

    def record(self, rid, start=0, seconds=20, outcome="passed", python="3.12.1",
               fingerprint="same-input", reused=False, omissions=None):
        directory = self.root / "runs" / rid
        directory.mkdir(parents=True)
        log = b"synthetic verification output\n"
        (directory / "unit.log").write_bytes(log)
        group = {"name": "unit-related", "outcome": "reused" if reused else outcome,
                 "started_at": None if reused else stamp(start), "resources": [],
                 "counter": "unittest", "timing": {"child_seconds": 0 if reused else seconds},
                 "cases": [] if reused else [{"id": "test_synthetic.T.test_ok", "status": outcome,
                                              "seconds": seconds}],
                 "counts": {"cases": 0 if reused else 1, "skipped": 0}, "scenarios": None}
        record = {"schema": "ae-verify-run/1", "state": "complete", "run_id": rid,
                  "wrapper_version": 1, "started_at": stamp(start),
                  "ended_at": stamp(start + seconds),
                  "context": {"phase": "development", "selectors_effective": ["tests/test_synthetic.py"],
                              "session": {"id": "synthetic-session"},
                              "selection": {"strategy_version": 2}},
                  "input": {"fingerprint": fingerprint, "covers_final_input": True},
                  "environment": {"os": {"system": "SyntheticOS", "release": "1", "machine": "arm64"},
                                  "python": {"version": python, "implementation": "CPython"},
                                  "ci": {"is_ci": False}, "config_summary": {},
                                  "isolation": {"mechanism": "flock"}},
                  "groups": [group], "timing": {"queued_at": stamp(start),
                       "started_at": stamp(start), "ended_at": stamp(start + seconds),
                       "queue_seconds": 2, "lock_wait_seconds": 3, "cleanup_seconds": 1},
                  "result": {"outcome": outcome, "exit_status": 0 if outcome == "passed" else 1,
                             "usable_as_evidence": outcome == "passed"},
                  "evidence": {"integrity": "ok", "logs": [{"path": "unit.log",
                       "sha256": hashlib.sha256(log).hexdigest()}]},
                  "shadow": {"comparable": True, "candidate_omissions": omissions or []}}
        (directory / "record.json").write_text(json.dumps(record), encoding="utf-8")
        return directory / "record.json"

    def cli(self, *args, now=3600):
        env = dict(os.environ)
        env["AE_LOCK_DIR"] = str(Path(self.tmp.name) / "locks")
        env.pop("AE_HELD_LOCKS", None)
        return subprocess.run([sys.executable, str(ROOT / "verify.py"), "report", "--efficiency",
                               "--out-dir", str(self.root), "--now", stamp(now), "--json", *args],
                              capture_output=True, text=True, env=env, timeout=20)

    def test_report_distinguishes_parallel_execution_and_only_reads_history(self):
        first = self.record("one", seconds=20)
        self.record("two", start=10, seconds=20)
        original = first.read_bytes()
        result = self.cli()
        self.assertEqual(result.returncode, 0, result.stderr)
        report = json.loads(result.stdout)
        self.assertEqual(report["metrics"]["execution_seconds"], 40)
        self.assertEqual(report["metrics"]["execution_union_seconds"], 30)
        self.assertEqual(report["metrics"]["queue_seconds"], 4)
        self.assertEqual(report["metrics"]["lock_wait_seconds"], 6)
        self.assertEqual(report["metrics"]["cleanup_seconds"], 2)
        self.assertEqual(first.read_bytes(), original)
        self.assertFalse((self.root / "efficiency").exists())

    def test_legacy_feedback_does_not_exceed_its_recorded_verification_window(self):
        path = self.record("cross-second", seconds=8.052128)
        record = json.loads(path.read_text())
        record["groups"][0]["started_at"] = stamp(1)
        record["timing"].update(queue_seconds=0, lock_wait_seconds=0, wall_seconds=8.082984)
        path.write_text(json.dumps(record))
        feedback = json.loads(self.cli().stdout)["metrics"]["first_related_feedback_seconds"][0]
        self.assertLessEqual(feedback["seconds"], 8.082984)
        self.assertEqual(feedback["precision"], "estimated")

    def test_legacy_execution_union_is_bounded_by_the_verification_window(self):
        path = self.record("rounded-end", seconds=8.052128)
        record = json.loads(path.read_text())
        record["timing"].update(queue_seconds=0, lock_wait_seconds=0, wall_seconds=8.082984,
                                 ended_at=stamp(8))
        record["ended_at"] = stamp(8)
        path.write_text(json.dumps(record))
        metrics = json.loads(self.cli().stdout)["metrics"]
        self.assertLessEqual(metrics["execution_union_seconds"], metrics["verification_interval_union_seconds"])
        self.assertEqual(metrics["interval_precision"], "estimated")

    def test_tenth_merge_creates_one_review_and_replayed_events_stay_quiet(self):
        self.record("first")
        events = Path(self.tmp.name) / "events.json"
        events.write_text(json.dumps({"schema": "ae-verify-events/1", "merged_prs": list(range(1, 11)), "gaps": []}))
        first = self.cli("--rebuild", "--events", str(events))
        self.assertEqual(first.returncode, 0, first.stderr)
        report = json.loads(first.stdout)
        self.assertTrue(report["review"]["generated"])
        second = json.loads(self.cli("--rebuild", "--events", str(events)).stdout)
        self.assertFalse(second["review"]["generated"])
        self.assertEqual(len(list((self.root / "efficiency" / "reviews").glob("*.json"))), 1)

    def test_seven_days_consumes_pending_samples_once_even_when_both_thresholds_match(self):
        self.record("initial")
        self.cli("--rebuild")
        self.record("pending", start=4000)
        self.cli("--rebuild", now=5000)
        events = Path(self.tmp.name) / "events.json"
        events.write_text(json.dumps({"schema": "ae-verify-events/1", "merged_prs": list(range(1, 11)), "gaps": []}))
        report = json.loads(self.cli("--rebuild", "--events", str(events), now=8 * 86400).stdout)
        self.assertTrue(report["review"]["generated"])
        self.assertIn("同时", report["review"]["reason"])
        again = json.loads(self.cli("--rebuild", "--events", str(events), now=9 * 86400).stdout)
        self.assertFalse(again["review"]["generated"])

    def test_run_automatically_updates_a_summary_without_adding_test_execution(self):
        repo = Path(self.tmp.name) / "repo"
        scripts = repo / "skill" / "annotation-everything" / "scripts"
        scripts.mkdir(parents=True)
        (repo / ".gitignore").write_text(".verify/\n__pycache__/\n")
        for name in ("verify.py", "verify_evidence.py", "verify_selection.py", "verify_report.py", "host_isolation.py"):
            shutil.copy2(ROOT / name, repo / name)
        (scripts / "serve.py").write_text("print('1/1 通过')\n")
        subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
        subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
        subprocess.run(["git", "-c", "user.name=synthetic", "-c", "user.email=synthetic@example.invalid",
                        "commit", "-qm", "synthetic"], cwd=repo, check=True)
        result = subprocess.run([sys.executable, str(repo / "verify.py"), "run", "--group", "serve-check", "--json"],
                                cwd=repo, capture_output=True, text=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["result"]["counts"]["checks"], 1)
        summary = json.loads((repo / ".verify" / "efficiency" / "summary.json").read_text())
        self.assertEqual(summary["metrics"]["runs"], 1)
        self.assertEqual(summary["metrics"]["first_related_feedback_seconds"][0]["precision"], "monotonic")
        self.assertEqual(len(list((repo / ".verify" / "runs").iterdir())), 1)

    def test_confirmed_omission_expands_plan_and_broken_guard_stays_conservative(self):
        self.record("finding")
        events = Path(self.tmp.name) / "events.json"
        events.write_text(json.dumps({"schema": "ae-verify-events/1", "merged_prs": [], "gaps": [],
            "selection_findings": [{"status": "confirmed", "strategy_version": 3,
                "reason": "合成的已确认漏选", "evidence": ["runs/finding/record.json"]}]}))
        self.cli("--rebuild", "--events", str(events))
        env = dict(os.environ, AE_VERIFY_DIR=str(self.root))
        def plan():
            result = subprocess.run([sys.executable, str(ROOT / "verify.py"), "plan", "--phase", "development", "--json"],
                                    capture_output=True, text=True, env=env, timeout=20)
            self.assertEqual(result.returncode, 0, result.stderr)
            return json.loads(result.stdout)["selection"]
        selection = plan()
        self.assertEqual(selection["suggested"]["groups"], ["all"])
        self.assertIn("已确认漏选", selection["fallback"]["reason"])
        (self.root / "efficiency" / "selection-guard.json").write_text("{broken")
        self.assertEqual(plan()["suggested"]["groups"], ["all"])
        (self.root / "efficiency" / "selection-guard.json").unlink()
        self.assertEqual(plan()["suggested"]["groups"], ["all"])

    def test_failures_retries_scope_groups_and_gaps_remain_visible(self):
        first = self.record("failure", outcome="failed")
        self.record("retry", start=30)
        self.record("duplicate", start=60)
        self.record("other-python", start=90, python="3.9.25")
        report = json.loads(self.cli().stdout)
        self.assertEqual(report["metrics"]["outcomes"]["failed"], 1)
        self.assertEqual(report["metrics"]["actual_duplicate_executions"], 1)
        self.assertEqual(len(report["comparison_groups"]), 2)
        self.assertIn("flaky-failure-candidate", [c["kind"] for c in report["candidates"]])
        self.assertEqual(json.loads(first.read_text())["result"]["outcome"], "failed")
        (first.parent / "unit.log").write_text("damaged")
        self.record("old", start=-2 * 86400)
        broken = self.root / "runs" / "broken"
        broken.mkdir()
        (broken / "record.json").write_text("{")
        report_result = self.cli()
        self.assertEqual(report_result.returncode, 1)
        self.assertGreaterEqual(len(json.loads(report_result.stdout)["gaps"]), 3)

    def test_performance_regression_needs_three_consecutive_valid_passes(self):
        for index in range(3):
            self.record("base-%d" % index, start=index * 30, seconds=20, fingerprint="base-%d" % index)
        self.record("slow-0", start=100, seconds=35, fingerprint="slow-0")
        self.record("slow-1", start=140, seconds=35, fingerprint="slow-1")
        self.record("failure", start=180, seconds=35, outcome="failed", fingerprint="failure")
        self.record("slow-2", start=220, seconds=35, fingerprint="slow-2")
        report = json.loads(self.cli().stdout)
        self.assertNotIn("performance-regression", [c["kind"] for c in report["candidates"]])
        self.record("slow-3", start=260, seconds=35, fingerprint="slow-3")
        self.record("slow-4", start=300, seconds=35, fingerprint="slow-4")
        report = json.loads(self.cli().stdout)
        self.assertIn("performance-regression", [c["kind"] for c in report["candidates"]])

    def test_imported_copies_are_deduplicated_and_conflicts_are_gaps(self):
        original = self.record("same-run")
        imported = Path(self.tmp.name) / "imported"
        shutil.copytree(self.root, imported)
        report = json.loads(self.cli("--source", str(imported)).stdout)
        self.assertEqual(report["metrics"]["runs"], 1)
        payload = json.loads((imported / "runs" / "same-run" / "record.json").read_text())
        payload["result"]["outcome"] = "failed"
        (imported / "runs" / "same-run" / "record.json").write_text(json.dumps(payload))
        result = self.cli("--source", str(imported))
        self.assertEqual(result.returncode, 1)
        self.assertIn("内容冲突", result.stdout)
        self.assertEqual(json.loads(original.read_text())["result"]["outcome"], "passed")

    def test_ci_collection_failure_is_an_explicit_gap(self):
        fake_bin = Path(self.tmp.name) / "bin"
        fake_bin.mkdir()
        gh = fake_bin / "gh"
        gh.write_text("#!/usr/bin/env python3\nimport sys\nprint('HTTP 403 permission denied', file=sys.stderr)\nsys.exit(1)\n")
        gh.chmod(0o755)
        env = dict(os.environ, PATH=str(fake_bin) + os.pathsep + os.environ["PATH"])
        result = subprocess.run([sys.executable, str(ROOT / "tools" / "collect_verify_history.py"),
                                 "--repo", "synthetic/repo", "--out-dir", str(self.root)],
                                env=env, capture_output=True, text=True, timeout=20)
        self.assertEqual(result.returncode, 1)
        report = self.cli("--events", str(self.root / "ci-events.json"))
        self.assertEqual(report.returncode, 1)
        self.assertIn("权限", report.stdout)

    def test_ci_collection_imports_structured_artifact_and_marks_expired_artifacts(self):
        original = self.record("ci-run")
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as archive:
            for path in original.parent.iterdir():
                archive.write(path, "runs/ci-run/" + path.name)
        shutil.rmtree(self.root / "runs")
        fake_bin = Path(self.tmp.name) / "bin"
        fake_bin.mkdir()
        script = """#!/usr/bin/env python3
import base64, json, sys
endpoint = sys.argv[-1]
if '/pulls?' in endpoint:
    print(json.dumps([{'number': 1, 'merged_at': 'synthetic'}]))
elif '/actions/artifacts?' in endpoint:
    print(json.dumps({'total_count': 2, 'artifacts': [
        {'id': 1, 'name': 'verify-python3.12', 'size_in_bytes': 100, 'created_at': '2026-01-01', 'workflow_run': {'id': 1}},
        {'id': 2, 'name': 'verify-python3.9', 'expired': True, 'size_in_bytes': 100, 'created_at': '2026-01-01'}]}))
elif endpoint.endswith('/1/zip'):
    sys.stdout.buffer.write(base64.b64decode(BYTES))
else:
    sys.exit(1)
""".replace("BYTES", repr(base64.b64encode(buffer.getvalue()).decode()))
        gh = fake_bin / "gh"
        gh.write_text(script)
        gh.chmod(0o755)
        env = dict(os.environ, PATH=str(fake_bin) + os.pathsep + os.environ["PATH"])
        result = subprocess.run([sys.executable, str(ROOT / "tools" / "collect_verify_history.py"),
                                 "--repo", "synthetic/repo", "--out-dir", str(self.root)],
                                env=env, capture_output=True, text=True, timeout=20)
        self.assertEqual(result.returncode, 1, result.stderr)
        report = json.loads(self.cli("--events", str(self.root / "ci-events.json")).stdout)
        self.assertEqual(report["metrics"]["runs"], 1)
        self.assertIn("过期", json.dumps(report["gaps"], ensure_ascii=False))

    def test_concurrent_rebuilds_consume_one_merge_event_batch(self):
        self.record("concurrent")
        events = Path(self.tmp.name) / "events.json"
        events.write_text(json.dumps({"schema": "ae-verify-events/1", "merged_prs": list(range(10)), "gaps": []}))
        command = [sys.executable, str(ROOT / "verify.py"), "report", "--efficiency", "--rebuild",
                   "--out-dir", str(self.root), "--events", str(events), "--now", stamp(3600), "--json"]
        env = dict(os.environ, AE_LOCK_DIR=str(Path(self.tmp.name) / "locks"))
        env.pop("AE_HELD_LOCKS", None)
        first = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env)
        second = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env)
        outputs = [first.communicate(timeout=20), second.communicate(timeout=20)]
        self.assertEqual(first.returncode, 0, outputs[0][1])
        self.assertEqual(second.returncode, 0, outputs[1][1])
        self.assertEqual(sum(json.loads(output[0])["review"]["generated"] for output in outputs), 1)

    def test_seven_day_review_without_merge_events_and_no_sample_stays_quiet(self):
        self.record("initial")
        self.cli("--rebuild")
        no_sample = json.loads(self.cli("--rebuild", now=8 * 86400).stdout)
        self.assertFalse(no_sample["review"]["generated"])
        self.record("new-sample", start=8 * 86400)
        new_sample = json.loads(self.cli("--rebuild", now=8 * 86400 + 3600).stdout)
        self.assertTrue(new_sample["review"]["generated"])
        self.assertIn("满7天", new_sample["review"]["reason"])

    def test_partial_browser_version_metadata_is_not_a_comparable_environment(self):
        path = self.record("partial-browser")
        record = json.loads(path.read_text())
        record["groups"][0].update(name="browser", browser_environment={"version": "synthetic"})
        path.write_text(json.dumps(record))
        report = json.loads(self.cli().stdout)
        self.assertFalse(report["comparison_groups"][0]["comparable"])

    def test_unconfirmed_omission_is_a_candidate_without_setting_a_guard(self):
        self.record("candidate", outcome="failed", omissions=[{"group": "unit", "target": "test_missing.T.test_case"}])
        report = json.loads(self.cli("--rebuild").stdout)
        self.assertIn("selection-omission-candidate", [item["kind"] for item in report["candidates"]])
        self.assertFalse((self.root / "efficiency" / "selection-guard.json").exists())

    def test_bounded_history_reports_omitted_samples_and_keeps_raw_evidence(self):
        for index in range(201):
            self.record("history-%03d" % index, start=index)
        report = json.loads(self.cli().stdout)
        self.assertEqual(report["metrics"]["runs"], 200)
        self.assertEqual(report["retention"]["omitted_runs"], 1)
        self.assertEqual(len(list((self.root / "runs").iterdir())), 201)

    def test_valid_json_with_broken_nested_shapes_is_a_gap_not_a_traceback(self):
        path = self.record("broken-shape")
        record = json.loads(path.read_text())
        record["groups"][0]["cases"] = ["not-a-case-object"]
        path.write_text(json.dumps(record))
        result = self.cli()
        self.assertEqual(result.returncode, 1)
        self.assertEqual(json.loads(result.stdout)["status"], "gaps")
        self.assertNotIn("Traceback", result.stderr)


if __name__ == "__main__":
    unittest.main()
