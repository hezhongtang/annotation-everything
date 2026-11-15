"""失效台账条目的批量清理：默认保守、可预览，并使用合成目录。"""
from __future__ import annotations

import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import unittest.mock
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "skill" / "annotation-everything" / "scripts"
sys.path.insert(0, str(SCRIPTS))
sys.path.insert(0, str(ROOT))
import host_isolation  # noqa: E402
host_isolation.require_for_module(__name__)


class LedgerPruneTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="ae-prune-test-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.home = self.root / "ledger"
        self.previous_home = os.environ.get("AE_HOME")
        os.environ["AE_HOME"] = str(self.home)
        self.addCleanup(self._restore_home)
        import ledger
        self.ledger = ledger
        self.reviews = {}
        self.temp_missing = self.root / "missing-in-temp"
        self.external_missing = Path("/Volumes") / f"ae-prune-test-{uuid.uuid4().hex}" / "review"
        self.valid = self.make_review("2000000000000000", "valid")
        self.archived = self.make_review("3000000000000000", "archived", archived=True)
        self.add("1000000000000000", self.temp_missing, 9101)
        self.add("4000000000000000", self.external_missing, 9104)
        self.write_ledger()

    def _restore_home(self):
        if self.previous_home is None:
            os.environ.pop("AE_HOME", None)
        else:
            os.environ["AE_HOME"] = self.previous_home

    def add(self, rid: str, directory: Path, port: int | None = None):
        self.reviews[rid] = {"dir": str(directory), "port": port}

    def make_review(self, rid: str, name: str, *, archived: bool = False) -> Path:
        directory = self.root / name
        directory.mkdir()
        (directory / "review.json").write_text(json.dumps({"reviewId": rid}), encoding="utf-8")
        (directory / "index.html").write_text("synthetic review", encoding="utf-8")
        if archived:
            (directory / ".annotation-archived").write_text("", encoding="utf-8")
        self.add(rid, directory, 9102 if name == "valid" else 9103)
        return directory

    def write_ledger(self):
        self.home.mkdir(parents=True, exist_ok=True)
        (self.home / "reviews.json").write_text(
            json.dumps({"version": 1, "reviews": self.reviews}), encoding="utf-8")

    def run_cli(self, *args: str):
        env = {**os.environ, "AE_HOME": str(self.home), "PYTHONDONTWRITEBYTECODE": "1"}
        return subprocess.run([sys.executable, str(SCRIPTS / "serve.py"), *args], env=env,
                              capture_output=True, text=True, timeout=60)

    def test_default_dry_run_previews_only_missing_temp_entries_without_writing(self):
        before = (self.home / "reviews.json").read_bytes()
        before_tree = sorted(str(path.relative_to(self.home)) for path in self.home.rglob("*"))

        result = self.ledger.prune_stale(dry_run=True)

        self.assertEqual([row["reviewId"] for row in result["candidates"]], ["1000000000000000"])
        self.assertEqual(result["removed"], [])
        self.assertEqual((self.home / "reviews.json").read_bytes(), before)
        self.assertEqual(sorted(str(path.relative_to(self.home)) for path in self.home.rglob("*")), before_tree)
        self.assertIn("4000000000000000", self.ledger.entries(), "未挂载的真实路径默认保留")
        self.assertIn("3000000000000000", self.ledger.entries(), "目录存在的归档审阅不属于失效项")

    def test_dry_run_on_an_empty_home_has_no_filesystem_side_effects(self):
        empty = self.root / "empty-home"
        with unittest.mock.patch.dict(os.environ, {"AE_HOME": str(empty)}):
            result = self.ledger.prune_stale(dry_run=True)

        self.assertEqual(result["candidates"], [])
        self.assertFalse(empty.exists(), "空台账预览不得创建 AE_HOME 或锁文件")

    def test_default_prune_removes_temp_entry_and_releases_only_its_port(self):
        result = self.ledger.prune_stale()

        self.assertEqual([row["reviewId"] for row in result["removed"]], ["1000000000000000"])
        self.assertEqual(set(self.ledger.entries()), {"2000000000000000", "3000000000000000", "4000000000000000"})
        self.assertNotIn(9101, self.ledger.owners())
        self.assertEqual(self.ledger.owners()[9102], "2000000000000000")
        self.assertEqual(self.ledger.owners()[9103], "3000000000000000")
        self.assertTrue(self.valid.is_dir())
        self.assertTrue((self.archived / ".annotation-archived").is_file())

    def test_all_stale_includes_external_missing_and_archived_mismatch(self):
        mismatch = self.root / "mismatch"
        mismatch.mkdir()
        (mismatch / "review.json").write_text(json.dumps({"reviewId": "f" * 16}), encoding="utf-8")
        (mismatch / ".annotation-archived").write_text("", encoding="utf-8")
        self.add("5000000000000000", mismatch, 9105)
        self.write_ledger()

        result = self.ledger.prune_stale(all_stale=True)

        self.assertEqual({row["reviewId"] for row in result["removed"]},
                         {"1000000000000000", "4000000000000000", "5000000000000000"})
        self.assertEqual(set(self.ledger.entries()), {"2000000000000000", "3000000000000000"})
        self.assertTrue(mismatch.is_dir(), "清理台账不删除失效条目的目录")
        self.assertTrue((mismatch / ".annotation-archived").is_file(), "未显式选择时保留元数据")

    def test_under_limits_all_stale_to_the_requested_directory(self):
        mismatch = self.root / "nested" / "mismatch"
        mismatch.mkdir(parents=True)
        (mismatch / "review.json").write_text(json.dumps({"reviewId": "e" * 16}), encoding="utf-8")
        self.add("5000000000000000", mismatch, 9105)
        self.write_ledger()

        result = self.ledger.prune_stale(all_stale=True, under=self.root)

        self.assertEqual({row["reviewId"] for row in result["removed"]},
                         {"1000000000000000", "5000000000000000"})
        self.assertIn("4000000000000000", self.ledger.entries(), "范围外的外部失效项保留")
        self.assertIn("2000000000000000", self.ledger.entries())

    def test_prune_waits_for_the_same_exclusive_ledger_lock(self):
        started = threading.Event()
        finished = threading.Event()
        errors = []

        def prune():
            started.set()
            try:
                self.ledger.prune_stale()
            except Exception as error:  # 线程断言在主线程统一报告
                errors.append(error)
            finally:
                finished.set()

        with self.ledger.fsutil.flocked(self.home / self.ledger.LOCK_FILE, exclusive=True):
            worker = threading.Thread(target=prune)
            worker.start()
            self.assertTrue(started.wait(1))
            time.sleep(0.05)
            self.assertFalse(finished.is_set(), "持锁期间批量清理必须等待")
        self.assertTrue(finished.wait(2), "释放台账锁后清理应继续")
        worker.join(timeout=1)
        self.assertEqual(errors, [])
        self.assertNotIn("1000000000000000", self.ledger.entries())

    def test_delete_metadata_skips_a_directory_owned_by_another_review(self):
        orphan = self.root / "orphan-metadata"
        owned = self.root / "owned-by-other-review"
        orphan.mkdir()
        owned.mkdir()
        (orphan / "review.json").write_text("{}", encoding="utf-8")
        (owned / "review.json").write_text(json.dumps({"reviewId": "f" * 16}), encoding="utf-8")
        names = (".annotation-archived", ".agent-binding.json", ".agent-deliveries.json",
                 ".annotation-wait.123.json", ".annotation-stop.json")
        for directory in (orphan, owned):
            for name in names:
                (directory / name).write_text("synthetic metadata", encoding="utf-8")
            (directory / "notes.json").write_text("synthetic annotations", encoding="utf-8")
            (directory / "report.html").write_text("synthetic material", encoding="utf-8")
        self.add("5000000000000000", orphan, 9105)
        self.add("6000000000000000", owned, 9106)
        self.write_ledger()

        result = self.ledger.prune_stale(all_stale=True, delete_metadata=True)

        self.assertIn("5000000000000000", {row["reviewId"] for row in result["metadataDeleted"]})
        preserved = {row["reviewId"]: row for row in result["metadataPreserved"]}
        self.assertEqual(preserved["6000000000000000"]["reason"], "owned-by-other-review")
        for name in names:
            self.assertFalse((orphan / name).exists(), name)
            self.assertTrue((owned / name).is_file(), name)
        for directory in (orphan, owned):
            self.assertTrue((directory / "notes.json").is_file())
            self.assertTrue((directory / "report.html").is_file())
            self.assertTrue(directory.is_dir())
        self.assertNotIn("5000000000000000", self.ledger.entries())
        self.assertNotIn("6000000000000000", self.ledger.entries())

    def test_metadata_delete_failure_rolls_back_every_entry_and_file(self):
        first = self.root / "first-metadata"
        second = self.root / "second-metadata"
        first.mkdir()
        second.mkdir()
        for directory in (first, second):
            (directory / "review.json").write_text("{}", encoding="utf-8")
            (directory / ".annotation-archived").write_text("archive", encoding="utf-8")
        (first / ".agent-binding.json").write_text("first binding", encoding="utf-8")
        (second / ".agent-binding.json").mkdir()
        self.add("5000000000000000", first)
        self.add("6000000000000000", second)
        self.write_ledger()

        with self.assertRaises(OSError):
            self.ledger.prune_stale(all_stale=True, review_ids=["5000000000000000", "6000000000000000"],
                                    delete_metadata=True)

        self.assertIn("5000000000000000", self.ledger.entries())
        self.assertIn("6000000000000000", self.ledger.entries())
        self.assertEqual((first / ".annotation-archived").read_text(encoding="utf-8"), "archive")
        self.assertEqual((first / ".agent-binding.json").read_text(encoding="utf-8"), "first binding")
        self.assertEqual((second / ".annotation-archived").read_text(encoding="utf-8"), "archive")
        self.assertTrue((second / ".agent-binding.json").is_dir())

    def test_cleanup_failure_after_commit_returns_a_recoverable_quarantine(self):
        directory = self.root / "cleanup-failure"
        directory.mkdir()
        (directory / "review.json").write_text("{}", encoding="utf-8")
        (directory / ".agent-binding.json").write_text("binding", encoding="utf-8")
        rid = "5000000000000000"
        self.add(rid, directory)
        self.write_ledger()
        original = self.ledger.os.unlink

        def fail_quarantined_unlink(path, *args, **kwargs):
            if ".annotation-prune-" in str(path):
                raise OSError("synthetic cleanup failure")
            return original(path, *args, **kwargs)

        with unittest.mock.patch.object(self.ledger.os, "unlink", side_effect=fail_quarantined_unlink):
            result = self.ledger.prune_stale(all_stale=True, review_ids=[rid], delete_metadata=True)

        self.assertNotIn(rid, self.ledger.entries(), "隔离完成后才提交台账")
        self.assertEqual(result["metadataDeleted"], [])
        error = result["metadataErrors"][0]
        self.assertEqual(error["reason"], "metadata-cleanup-failed")
        quarantine = Path(error["quarantine"])
        self.assertTrue(quarantine.is_dir())
        self.assertEqual((quarantine / ".agent-binding.json").read_text(encoding="utf-8"), "binding",
                         "提交后删除失败时文件仍可在隔离区恢复")

    def test_quarantine_internal_rollback_does_not_replace_a_concurrent_file(self):
        import serve
        directory = self.root / "internal-rollback"
        directory.mkdir()
        (directory / "review.json").write_text("{}", encoding="utf-8")
        (directory / ".agent-binding.json").write_text("old binding", encoding="utf-8")
        (directory / ".agent-deliveries.json").write_text("deliveries", encoding="utf-8")
        rid = "5000000000000000"
        self.add(rid, directory)
        self.write_ledger()
        original = self.ledger.os.rename

        def fail_second_move(src, dst, **kwargs):
            if str(src) == ".agent-deliveries.json" and ".annotation-prune-" in str(dst):
                (directory / ".agent-binding.json").write_text("new binding", encoding="utf-8")
                raise OSError("injected move failure")
            return original(src, dst, **kwargs)

        with unittest.mock.patch.object(self.ledger.os, "rename", side_effect=fail_second_move), \
                contextlib.redirect_stderr(io.StringIO()) as errors:
            code = serve.prune(all_stale=True, under=None, delete_metadata=True, dry_run=False, locale="en")

        self.assertEqual(code, 2)
        self.assertIn(".annotation-prune-", errors.getvalue())
        self.assertIn(str(directory), errors.getvalue(), "CLI 必须输出可定位的绝对隔离路径")
        self.assertEqual((directory / ".agent-binding.json").read_text(encoding="utf-8"), "new binding")
        quarantine = next(directory.glob(".annotation-prune-*"))
        self.assertEqual((quarantine / ".agent-binding.json").read_text(encoding="utf-8"), "old binding")

    def test_precommit_rollback_does_not_replace_a_concurrent_file(self):
        directory = self.root / "precommit-rollback"
        directory.mkdir()
        (directory / "review.json").write_text("{}", encoding="utf-8")
        (directory / ".agent-binding.json").write_text("old binding", encoding="utf-8")
        rid = "5000000000000000"
        self.add(rid, directory)
        self.write_ledger()
        original = self.ledger._metadata_plan
        calls = 0

        def create_new_file_and_fail(fd, review_id):
            nonlocal calls
            calls += 1
            if calls == 3:
                (directory / ".agent-binding.json").write_text("new binding", encoding="utf-8")
                raise RuntimeError("injected precommit failure")
            return original(fd, review_id)

        with unittest.mock.patch.object(self.ledger, "_metadata_plan", side_effect=create_new_file_and_fail), \
                self.assertRaises(Exception) as caught:
            self.ledger.prune_stale(all_stale=True, review_ids=[rid], delete_metadata=True)

        self.assertIn(".annotation-prune-", str(caught.exception))
        self.assertIn(rid, self.ledger.entries())
        self.assertEqual((directory / ".agent-binding.json").read_text(encoding="utf-8"), "new binding")
        quarantine = next(directory.glob(".annotation-prune-*"))
        self.assertEqual((quarantine / ".agent-binding.json").read_text(encoding="utf-8"), "old binding")

    def test_metadata_delete_rejects_invalid_manifest_and_status_structures_on_both_reads(self):
        cases = (("null-owner", {"reviewId": None}, None, "identity-unconfirmed"),
                 ("array-status", {}, [], "status-unconfirmed"),
                 ("invalid-pid", {}, {"pid": None}, "status-unconfirmed"),
                 ("overflow-pid", {}, {"pid": 1267650600228229401496703205376}, "status-unconfirmed"))
        for index, (name, manifest, status, reason) in enumerate(cases, start=5):
            with self.subTest(name=name):
                self.reviews = {}
                directory = self.root / name
                directory.mkdir()
                (directory / "review.json").write_text(json.dumps(manifest), encoding="utf-8")
                if status is not None:
                    (directory / ".annotation-server.json").write_text(json.dumps(status), encoding="utf-8")
                (directory / ".agent-binding.json").write_text("binding", encoding="utf-8")
                rid = f"{index:016x}"
                self.add(rid, directory)
                self.write_ledger()
                original = self.ledger._metadata_plan
                calls = 0

                def first_delete_then_read(fd, review_id):
                    nonlocal calls
                    calls += 1
                    return ("delete", None) if calls <= 2 else original(fd, review_id)

                with unittest.mock.patch.object(self.ledger, "_metadata_plan", side_effect=first_delete_then_read):
                    result = self.ledger.prune_stale(all_stale=True, review_ids=[rid], delete_metadata=True)

                self.assertEqual(result["metadataDeleted"], [])
                self.assertEqual(result["metadataPreserved"][0]["reason"], reason)
                self.assertEqual((directory / ".agent-binding.json").read_text(encoding="utf-8"), "binding")
                self.assertNotIn(rid, self.ledger.entries())

    def test_metadata_delete_skips_a_review_recovered_during_prune(self):
        directory = self.root / "recovering"
        directory.mkdir()
        rid = "5000000000000000"
        (directory / "review.json").write_text("{}", encoding="utf-8")
        (directory / ".agent-binding.json").write_text("binding", encoding="utf-8")
        self.add(rid, directory)
        self.write_ledger()
        original = self.ledger._open_metadata_dir

        def recover_before_open(path, root):
            (directory / "review.json").write_text(json.dumps({"reviewId": rid}), encoding="utf-8")
            return original(path, root)

        with unittest.mock.patch.object(self.ledger, "_open_metadata_dir", side_effect=recover_before_open):
            result = self.ledger.prune_stale(all_stale=True, review_ids=[rid], delete_metadata=True)

        self.assertIn(rid, self.ledger.entries(), "恢复为当前审阅后不得再解除台账关联")
        self.assertEqual((directory / ".agent-binding.json").read_text(encoding="utf-8"), "binding")
        self.assertEqual(result["removed"], [])
        self.assertEqual(result["skipped"][0]["reason"], "recovered")

    def test_manifest_writer_waits_for_the_prune_critical_section(self):
        directory = self.root / "ordered-recovery"
        directory.mkdir()
        (directory / "review.json").write_text("{}", encoding="utf-8")
        (directory / ".agent-binding.json").write_text("old binding", encoding="utf-8")
        rid = "5000000000000000"
        self.add(rid, directory)
        self.write_ledger()
        original_plan = self.ledger._metadata_plan
        original_cleanup = self.ledger._cleanup_quarantines
        order = []
        writer_started = threading.Event()
        workers = []
        calls = 0

        def recover_with_shared_lock():
            writer_started.set()
            with self.ledger.metadata_lock(directory, exclusive=True):
                order.append("writer-lock")
                (directory / "review.json").write_text(json.dumps({"reviewId": rid}), encoding="utf-8")
                order.append("manifest-written")

        def start_writer_on_final_plan(fd, review_id):
            nonlocal calls
            calls += 1
            if calls == 3:
                worker = threading.Thread(target=recover_with_shared_lock)
                workers.append(worker)
                worker.start()
                self.addCleanup(worker.join, 1)
                self.assertTrue(writer_started.wait(1))
                time.sleep(0.05)
            return original_plan(fd, review_id)

        def record_cleanup(prepared):
            order.append("cleanup-start")
            result = original_cleanup(prepared)
            order.append("cleanup-end")
            return result

        with unittest.mock.patch.object(self.ledger, "_metadata_plan", side_effect=start_writer_on_final_plan), \
                unittest.mock.patch.object(self.ledger, "_cleanup_quarantines", side_effect=record_cleanup):
            self.ledger.prune_stale(all_stale=True, review_ids=[rid], delete_metadata=True)

        for worker in workers:
            worker.join(timeout=1)
            self.assertFalse(worker.is_alive(), "恢复写线程应在 prune 释放锁后完成")
        self.assertLess(order.index("cleanup-end"), order.index("writer-lock"), order)
        self.assertLess(order.index("cleanup-end"), order.index("manifest-written"), order)
        self.assertEqual(json.loads((directory / "review.json").read_text())["reviewId"], rid)

    def test_metadata_delete_uses_the_opened_directory_after_a_symlink_swap(self):
        scope = self.root / "scope"
        scope.mkdir()
        directory = scope / "inside"
        outside = self.root / "outside"
        directory.mkdir()
        outside.mkdir()
        for candidate in (directory, outside):
            (candidate / "review.json").write_text("{}", encoding="utf-8")
            (candidate / ".agent-binding.json").write_text("binding", encoding="utf-8")
        rid = "5000000000000000"
        self.add(rid, directory)
        self.write_ledger()
        original = self.ledger._metadata_plan
        swapped = False

        def swap_after_open(fd, review_id):
            nonlocal swapped
            if not swapped:
                moved = self.root / "moved-inside"
                directory.rename(moved)
                directory.symlink_to(outside, target_is_directory=True)
                swapped = True
            return original(fd, review_id)

        with unittest.mock.patch.object(self.ledger, "_metadata_plan", side_effect=swap_after_open):
            self.ledger.prune_stale(all_stale=True, under=scope, review_ids=[rid], delete_metadata=True)

        self.assertEqual((outside / ".agent-binding.json").read_text(encoding="utf-8"), "binding",
                         "范围外 symlink 目标的元数据不得删除")
        self.assertFalse((self.root / "moved-inside" / ".agent-binding.json").exists())

    def test_metadata_delete_fails_closed_for_unknown_identity_and_live_service(self):
        running = self.root / "running-unknown"
        corrupt = self.root / "corrupt-manifest"
        missing = self.root / "missing-manifest"
        for directory in (running, corrupt, missing):
            directory.mkdir()
            (directory / ".agent-binding.json").write_text("binding", encoding="utf-8")
            (directory / ".annotation-stop.json").write_text("stop", encoding="utf-8")
        (running / "review.json").write_text("{broken", encoding="utf-8")
        (corrupt / "review.json").write_text("{broken", encoding="utf-8")
        (running / ".annotation-server.json").write_text(json.dumps({"pid": os.getpid()}), encoding="utf-8")
        ids = {"5000000000000000": running, "6000000000000000": corrupt, "7000000000000000": missing}
        for rid, directory in ids.items():
            self.add(rid, directory)
        self.write_ledger()

        result = self.ledger.prune_stale(all_stale=True, review_ids=list(ids), delete_metadata=True)

        self.assertIn("5000000000000000", self.ledger.entries(), "存活服务的台账与端口不得释放")
        self.assertNotIn("6000000000000000", self.ledger.entries())
        self.assertNotIn("7000000000000000", self.ledger.entries())
        self.assertEqual(result["skipped"][0]["reason"], "running-review")
        self.assertEqual({row["reason"] for row in result["metadataPreserved"]}, {"identity-unconfirmed"})
        for directory in ids.values():
            self.assertEqual((directory / ".agent-binding.json").read_text(encoding="utf-8"), "binding")
            self.assertEqual((directory / ".annotation-stop.json").read_text(encoding="utf-8"), "stop")

    def test_cli_reports_metadata_cleanup_errors_and_returns_partial_failure(self):
        import serve
        directory = self.root / "cleanup-error"
        directory.mkdir()
        (directory / "review.json").write_text("{}", encoding="utf-8")
        (directory / ".annotation-archived").write_text("archive", encoding="utf-8")
        rid = "5000000000000000"
        self.add(rid, directory)
        self.write_ledger()
        row = {"reviewId": rid, "state": "mismatch", "dir": str(directory), "port": None}
        error = {**row, "reason": "metadata-cleanup-failed",
                 "quarantine": str(directory / ".annotation-prune-synthetic"), "detail": "synthetic failure"}

        with unittest.mock.patch.object(self.ledger, "_cleanup_quarantines", return_value=([], [error])), \
                contextlib.redirect_stdout(io.StringIO()) as output:
            code = serve.prune(all_stale=True, under=None, delete_metadata=True, dry_run=False, locale="en")

        self.assertEqual(code, 1, "台账已提交但元数据未清完是部分失败")
        self.assertIn("Could not fully delete metadata for 1 review", output.getvalue())
        self.assertIn(".annotation-prune-synthetic", output.getvalue())
        self.assertIn("synthetic failure", output.getvalue())

    def test_explicit_review_ids_fix_the_confirmed_targets(self):
        result = self.ledger.prune_stale(all_stale=True, review_ids=["4000000000000000"])

        self.assertEqual([row["reviewId"] for row in result["removed"]], ["4000000000000000"])
        self.assertIn("1000000000000000", self.ledger.entries(),
                      "确认框之外的失效项不因后续批量请求被顺带删除")

    def test_cli_lists_then_removes_with_localized_counts(self):
        preview = self.run_cli("prune", "--stale", "--list", "--locale", "en")

        self.assertEqual(preview.returncode, 0, preview.stdout + preview.stderr)
        self.assertIn("Would remove 1 stale review", preview.stdout)
        self.assertIn("1000000000000000", preview.stdout)
        self.assertIn(str(self.temp_missing), preview.stdout)
        self.assertIn("1000000000000000", self.ledger.entries(), "--list 不得修改台账")

        removed = self.run_cli("prune", "--stale", "--locale", "en")

        self.assertEqual(removed.returncode, 0, removed.stdout + removed.stderr)
        self.assertIn("Removed 1 stale review", removed.stdout)
        self.assertIn("1000000000000000", removed.stdout)
        self.assertNotIn("1000000000000000", self.ledger.entries())
        self.assertIn("4000000000000000", self.ledger.entries())

        preview_zh = self.run_cli("prune", "--stale", "--list")
        self.assertEqual(preview_zh.returncode, 0, preview_zh.stdout + preview_zh.stderr)
        self.assertIn("将移除 0 条失效审阅", preview_zh.stdout)

    def test_cli_reports_metadata_deleted_and_preserved_counts(self):
        orphan = self.root / "orphan-cli"
        owned = self.root / "owned-cli"
        orphan.mkdir()
        owned.mkdir()
        (orphan / "review.json").write_text("{}", encoding="utf-8")
        (owned / "review.json").write_text(json.dumps({"reviewId": "d" * 16}), encoding="utf-8")
        (orphan / ".annotation-archived").write_text("", encoding="utf-8")
        (owned / ".annotation-archived").write_text("", encoding="utf-8")
        self.add("5000000000000000", orphan)
        self.add("6000000000000000", owned)
        self.write_ledger()

        result = self.run_cli("prune", "--stale", "--all", "--delete-metadata", "--locale", "en")

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("Removed 4 stale reviews", result.stdout)
        self.assertIn("Deleted metadata for 1 review", result.stdout)
        self.assertIn("Preserved metadata for 1 review owned by another review", result.stdout)
        self.assertFalse((orphan / ".annotation-archived").exists())
        self.assertTrue((owned / ".annotation-archived").is_file())

    def test_cli_distinguishes_unknown_identity_from_another_owner(self):
        directory = self.root / "unknown-cli"
        directory.mkdir()
        (directory / "review.json").write_text("{broken", encoding="utf-8")
        (directory / ".annotation-archived").write_text("archive", encoding="utf-8")
        rid = "5000000000000000"
        self.add(rid, directory)
        self.write_ledger()

        result = self.run_cli("prune", "--all", "--delete-metadata", "--locale", "en")

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("identity or status could not be confirmed", result.stdout)
        self.assertNotIn("owned by another review", result.stdout)
        self.assertTrue((directory / ".annotation-archived").is_file())
        self.assertNotIn(rid, self.ledger.entries())

    def test_cli_delete_metadata_requires_an_explicit_scope(self):
        result = self.run_cli("prune", "--delete-metadata", "--locale", "en")

        self.assertEqual(result.returncode, 2)
        self.assertIn("requires --under or --all", result.stderr)
        self.assertIn("1000000000000000", self.ledger.entries())


class OverviewPruneTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="ae-prune-http-test-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.home = self.root / "ledger"
        self.previous_home = os.environ.get("AE_HOME")
        os.environ["AE_HOME"] = str(self.home)
        self.addCleanup(self._restore_home)
        import ledger
        import serve
        self.ledger = ledger
        self.serve = serve
        self.reviews = {}
        self.scope = self.root / "scope"
        self.scope.mkdir()
        self.inside = self.scope / "inside-missing"
        self.outside = self.root / "outside-missing"
        self.add("1000000000000000", self.inside, 9201)
        self.add("2000000000000000", self.outside, 9202)
        self.write_ledger()
        self.host_id = ledger.overview_host_id()
        self.srv = serve.ReviewServer(None, 0, home_id=self.host_id, overview_under=self.scope,
                                      log_to_file=False)
        threading.Thread(target=self.srv.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True).start()
        self.addCleanup(self.srv.server_close)
        self.addCleanup(self.srv.shutdown)
        self.headers = {"Host": f"127.0.0.1:{self.srv.port}",
                        "Origin": f"http://127.0.0.1:{self.srv.port}",
                        "Content-Type": "application/json", "X-AE-Host": self.host_id}

    def _restore_home(self):
        if self.previous_home is None:
            os.environ.pop("AE_HOME", None)
        else:
            os.environ["AE_HOME"] = self.previous_home

    def add(self, rid: str, directory: Path, port: int | None = None):
        self.reviews[rid] = {"dir": str(directory), "port": port}

    def write_ledger(self):
        self.home.mkdir(parents=True, exist_ok=True)
        (self.home / "reviews.json").write_text(
            json.dumps({"version": 1, "reviews": self.reviews}), encoding="utf-8")

    def post(self, path: str, body: dict):
        status, _headers, raw = self.serve._req(self.srv.port, "POST", path, self.headers, body)
        return status, json.loads(raw)

    def test_prune_honors_confirmed_ids_and_home_under_scope(self):
        status, result = self.post("/api/overview/prune", {
            "reviewIds": ["1000000000000000", "2000000000000000"],
            "allStale": False, "deleteMetadata": False})

        self.assertEqual(status, 200, result)
        self.assertEqual([row["reviewId"] for row in result["removed"]], ["1000000000000000"])
        self.assertNotIn("1000000000000000", self.ledger.entries())
        self.assertIn("2000000000000000", self.ledger.entries(), "首页 --under 外的目标不得删除")
        status, _headers, raw = self.serve._req(self.srv.port, "GET", "/api/overview?scope=all",
                                                {"Host": self.headers["Host"]})
        self.assertEqual(status, 200, raw)
        self.assertEqual(json.loads(raw)["reviews"], [], "清理后的总览快照立即不再返回该条目")

    def test_snapshot_computes_default_prune_eligibility_from_the_ledger_path(self):
        import review_overview
        with tempfile.TemporaryDirectory(prefix="ae-prune-target-", dir="/var/tmp") as target:
            link = Path("/tmp") / f"ae-prune-link-{uuid.uuid4().hex}"
            link.symlink_to(target, target_is_directory=True)
            self.addCleanup(link.unlink, missing_ok=True)
            rid = "3000000000000000"
            self.add(rid, link / "missing")
            self.write_ledger()

            data = review_overview.snapshot(None, None, "all", self.serve.review_row)
            row = next(row for row in data["reviews"] if row["reviewId"] == rid)

            self.assertFalse(self.ledger.in_temp_area(Path(row["directory"])),
                             "解析后的 /var/tmp 路径本身不属于默认临时区")
            self.assertTrue(row["defaultPruneEligible"],
                            "原始 /tmp 台账路径仍须与 ledger 默认候选口径一致")

    def test_reveal_opens_an_existing_mismatch_directory_without_changing_it(self):
        directory = self.scope / "mismatch"
        directory.mkdir()
        (directory / "review.json").write_text(json.dumps({"reviewId": "f" * 16}), encoding="utf-8")
        (directory / "notes.json").write_text("synthetic annotations", encoding="utf-8")
        self.add("3000000000000000", directory)
        self.write_ledger()

        with unittest.mock.patch.object(self.serve.subprocess, "Popen") as opener:
            status, result = self.post("/api/overview/reveal", {"reviewId": "3000000000000000"})

        self.assertEqual(status, 200, result)
        self.assertEqual(result["reviewId"], "3000000000000000")
        command = opener.call_args.args[0]
        self.assertIn(str(directory.resolve()), command)
        self.assertNotIn("shell", opener.call_args.kwargs)
        self.assertEqual((directory / "notes.json").read_text(encoding="utf-8"), "synthetic annotations")

    def test_prune_metadata_response_reports_deleted_and_preserved_targets(self):
        orphan = self.scope / "orphan"
        owned = self.scope / "owned"
        orphan.mkdir()
        owned.mkdir()
        (orphan / "review.json").write_text("{}", encoding="utf-8")
        (owned / "review.json").write_text(json.dumps({"reviewId": "e" * 16}), encoding="utf-8")
        for directory in (orphan, owned):
            (directory / ".annotation-archived").write_text("", encoding="utf-8")
            (directory / "notes.json").write_text("synthetic annotations", encoding="utf-8")
        self.add("3000000000000000", orphan)
        self.add("4000000000000000", owned)
        self.write_ledger()

        status, result = self.post("/api/overview/prune", {
            "reviewIds": ["3000000000000000", "4000000000000000"],
            "allStale": True, "deleteMetadata": True})

        self.assertEqual(status, 200, result)
        self.assertEqual({row["reviewId"] for row in result["removed"]},
                         {"3000000000000000", "4000000000000000"})
        self.assertEqual([row["reviewId"] for row in result["metadataDeleted"]], ["3000000000000000"])
        self.assertEqual(result["metadataPreserved"][0]["reason"], "owned-by-other-review")
        self.assertFalse((orphan / ".annotation-archived").exists())
        self.assertTrue((owned / ".annotation-archived").is_file())
        self.assertTrue((orphan / "notes.json").is_file())
        self.assertTrue((owned / "notes.json").is_file())


class OverviewPrunePageTests(unittest.TestCase):
    def test_invalid_group_has_confirmed_prune_actions_refresh_and_backup_controls(self):
        text = (SCRIPTS / "viewer_template.html").read_text(encoding="utf-8")
        for needle in ("data-overview-prune", "function pruneOverview", "confirm(message)",
                       "await loadOverview()", "overviewCopyPath", "overviewReveal",
                       "result.metadataErrors", "result.skipped"):
            self.assertIn(needle, text)
        keys = ("viewer.overview.prune.temp", "viewer.overview.prune.all", "viewer.overview.prune.metadata",
                "viewer.overview.prune.confirm.temp", "viewer.overview.prune.confirm.all",
                "viewer.overview.prune.confirm.metadata", "viewer.overview.prune.metadataErrors",
                "viewer.overview.prune.metadataProtected", "viewer.overview.prune.skipped",
                "viewer.overview.copyPath", "viewer.overview.reveal")
        for locale in ("zh-CN", "en"):
            catalog = json.loads((SCRIPTS.parent / "locales" / f"{locale}.json").read_text(encoding="utf-8"))
            for key in keys:
                self.assertIn(key, catalog["messages"], f"{locale} 缺 {key}")

    def test_metadata_confirmation_and_output_disclose_every_deleted_category(self):
        expected = {"zh-CN": ("归档", "绑定", "投递", "等待", "停止"),
                    "en": ("archive", "binding", "delivery", "waiting", "stop-state")}
        for locale, words in expected.items():
            catalog = json.loads((SCRIPTS.parent / "locales" / f"{locale}.json").read_text(encoding="utf-8"))
            for key in ("viewer.overview.prune.confirm.metadata", "viewer.overview.prune.metadataDone",
                        "serve.opt.pruneDeleteMetadata", "serve.msg.pruneMetadataDone"):
                message = catalog["messages"][key].lower()
                for word in words:
                    self.assertIn(word.lower(), message, f"{locale} {key} 未披露 {word}")

    def test_prune_protocol_is_documented_in_both_format_references(self):
        for name, heading in (("formats.md", "## 失效台账清理"),
                              ("formats.en.md", "## Stale ledger pruning")):
            text = (SCRIPTS.parent / "references" / name).read_text(encoding="utf-8")
            self.assertIn(heading, text)
            for needle in ("POST /api/overview/prune", "POST /api/overview/reveal",
                           ".annotation-metadata.lock", "metadataErrors", "no-replace"):
                self.assertIn(needle, text, f"{name} 缺 {needle}")


if __name__ == "__main__":
    unittest.main()
