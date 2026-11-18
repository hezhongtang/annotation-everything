"""PR #25 审查缺陷的回归测试；仅使用合成文件与注入的进程表。"""
from __future__ import annotations

import contextlib
import io
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import ae

SERVE = ROOT / ae.SERVE_PY


def _join_cmd(argv) -> str:
    items = [str(item) for item in argv]
    return subprocess.list2cmdline(items) if os.name == "nt" else shlex.join(items)


class RegressionCase(unittest.TestCase):
    """公共夹具：HOME、台账、技能副本都指向临时目录。"""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="ae-review-regression-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.home = self.root / "home"
        self.home.mkdir()
        self.ledger = self.root / "ledger"
        self.copy = self.root / "skills" / ae.SKILL_NAME
        self.copy.mkdir(parents=True)
        self.env = {"HOME": str(self.home), "USERPROFILE": str(self.home), "AE_HOME": str(self.ledger),
                    "CLAUDE_SKILLS_DIR": str(self.copy.parent),
                    "CODEX_HOME": str(self.home / ".codex"), "PYTHONDONTWRITEBYTECODE": "1"}
        self.env_patch = patch.dict(os.environ, self.env)
        self.env_patch.start()
        self.addCleanup(self.env_patch.stop)

    def ctx(self, **overrides):
        values = dict(skill_copy=self.copy, repo=ROOT, record=None, ledger_home=self.ledger,
                      process_lister=lambda: [], port_probe=lambda port: None)
        values.update(overrides)
        return ae.DoctorContext(**values)

    def run_ae(self, *args, cwd=None):
        return subprocess.run([sys.executable, str(ROOT / "ae.py"), *args],
                              cwd=cwd or self.root, capture_output=True, text=True, timeout=40)

    def uninstall(self):
        with patch.object(ae, "running_servers", return_value=[]), \
                contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            return ae.main(["uninstall", "--purge"])


class ReviewRegressionTests(RegressionCase):
    def test_failed_checks_always_include_a_diagnostic_command(self):
        self.ledger.mkdir()
        cmd = _join_cmd([sys.executable, SERVE, self.root / "gone", "--port", "18900"])
        ctx = self.ctx(port_probe=lambda port: {"app": "other"} if port == 8765 else None,
                       process_lister=lambda: [(4242, cmd)])
        assigned = json.dumps({"version": 1, "reviews": {
            "a" * 16: {"dir": str(self.root / "gone"), "port": 8765}}})
        failed = set()
        for ledger_text in ("{invalid", assigned):   # 台账不可读 / 已分配的专属端口被占用
            (self.ledger / "reviews.json").write_text(ledger_text, encoding="utf-8")
            with patch.object(ae.sys, "version_info", (3, 8, 0)):
                results = ae.run_doctor(ctx)
            for result in results:
                if result.level == ae.LEVEL_FAIL:
                    failed.add(result.code)
                    with self.subTest(code=result.code):
                        self.assertTrue(result.fix_command.strip())
        self.assertLessEqual({"ledger-readable", "port-pool", "orphan-servers"}, failed)

    def test_deleted_review_reports_port_from_command_line(self):
        cmd = _join_cmd([sys.executable, SERVE, self.root / "gone", "--port=18900"])
        result = ae.check_orphan_servers(self.ctx(process_lister=lambda: [(4242, cmd)]))
        self.assertEqual(result.level, ae.LEVEL_FAIL)
        self.assertIn("4242", result.message)
        self.assertIn("18900", result.message)

    def test_absolute_script_invocation_outside_repo_omits_dev_checks(self):
        p = self.run_ae("doctor", "--json")
        self.assertIn(p.returncode, (0, 1), p.stderr)
        results = json.loads(p.stdout)["results"]
        self.assertNotIn(ae.CATEGORY_DEV, {r["category"] for r in results})

    def test_malformed_install_record_exits_two_without_traceback(self):
        for data in ([1], "record", {"repo": []}, {"links": "bad"}, {"links": [{"path": 1}]}):
            with self.subTest(data=data):
                (self.copy / ae.RECORD_NAME).write_text(json.dumps(data), encoding="utf-8")
                p = self.run_ae("doctor", "--json")
                self.assertEqual(p.returncode, 2, p.stdout + p.stderr)
                self.assertNotIn("Traceback", p.stderr)

    def test_unexpected_command_exception_exits_two(self):
        with patch.object(ae, "cmd_doctor", side_effect=RuntimeError("synthetic failure")), \
                contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(ae.main(["doctor", "--json"]), 2)

    def test_install_recovers_from_invalid_record(self):
        (self.copy / ae.RECORD_NAME).write_text("[1]", encoding="utf-8")
        p = self.run_ae("install")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertIsInstance(json.loads((self.copy / ae.RECORD_NAME).read_text()), dict)

    def test_purge_rejects_corrupt_ledger_before_removing_copy(self):
        self.ledger.mkdir()
        path = self.ledger / "reviews.json"
        path.write_text("{invalid", encoding="utf-8")
        self.assertEqual(self.uninstall(), 2)
        self.assertEqual(path.read_text(), "{invalid")
        self.assertTrue(self.copy.is_dir())

    def test_purge_preserves_unregistered_review_and_unknown_files(self):
        self.ledger.mkdir()
        (self.ledger / "reviews.json").write_text('{"version":1,"reviews":{}}', encoding="utf-8")
        for name, is_dir in (("unregistered-review", True), ("material.txt", False)):
            with self.subTest(name=name):
                path = self.ledger / name
                if is_dir:
                    path.mkdir()
                    marker = path / "index.html"
                else:
                    marker = path
                marker.write_text("synthetic review", encoding="utf-8")
                self.assertEqual(self.uninstall(), 2)
                self.assertEqual(marker.read_text(), "synthetic review")
                self.assertTrue(self.copy.exists())
                marker.unlink()
                if is_dir:
                    path.rmdir()

    def test_purge_rejects_invalid_ledger_structure(self):
        self.ledger.mkdir()
        for data in ([], {"reviews": []}, {"reviews": {"a" * 16: {"dir": []}}}):
            with self.subTest(data=data):
                path = self.ledger / "reviews.json"
                path.write_text(json.dumps(data), encoding="utf-8")
                self.assertEqual(self.uninstall(), 2)
                self.assertTrue(path.exists())

    def test_service_detection_ignores_mentions_and_unrelated_scripts(self):
        own = _join_cmd([sys.executable, SERVE, self.root / "review"])
        fake = [(41001, "grep serve.py notes.txt"),
                (41002, _join_cmd(["python3", self.root / "other" / "serve.py", "/review"])),
                (41003, _join_cmd([sys.executable, "-c", "pass", SERVE, "/review"])),
                (41004, own)]
        self.assertEqual([pid for pid, _ in ae.running_servers(lister=lambda: fake)], [41004])

    def test_stop_servers_rejects_unrelated_live_process(self):
        proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
        self.addCleanup(self.stop, proc)
        command = f"{sys.executable} {SERVE} {self.root}/review"
        with patch.object(ae.os, "kill") as kill:
            stuck = ae.stop_servers([(proc.pid, command)], wait_seconds=0)
        kill.assert_not_called()
        self.assertEqual([pid for pid, _ in stuck], [proc.pid])
        self.assertIsNone(proc.poll())

    @staticmethod
    def stop(proc):
        if proc.poll() is None:
            proc.terminate()
        proc.communicate(timeout=10)


class SecondReviewRegressionTests(RegressionCase):
    """PR #25 第二轮审查的回归测试；仅使用合成文件、注入的进程表和测试自建的 socket。"""

    RID = "c0ffee00c0ffee00"

    def registered_review(self, name="review"):
        review = self.root / name
        review.mkdir()
        return review, {self.RID: {"dir": str(review), "port": 8800}}

    def test_serve_subcommand_mirrors_serve_py_main(self):
        review, entries = self.registered_review()
        def serve(*args):
            return _join_cmd([sys.executable, SERVE, *args])
        self.assertEqual(ae.serve_dir_argument(serve("serve", str(review), "--no-open")), str(review))
        self.assertEqual(ae.serve_dir_argument(serve("--no-open", "list")), "list")  # 选项在前时 list 是目录
        for sub in ("check", "status", "stop", "stop-status", "list", "forget", "prune"):
            with self.subTest(sub=sub):
                self.assertIsNone(ae.serve_dir_argument(serve(sub, str(review))))
        self.assertEqual(ae.find_orphan_servers(
            entries, lambda: [(4300, serve("serve", str(review), "--no-open"))], lambda port: None), [])

    def test_locale_flag_does_not_turn_maintenance_commands_into_servers(self):
        processes = []
        for prefix in (("--locale", "en"), ("--locale=en",)):
            for sub in ("check", "status", "stop", "stop-status", "list", "forget", "prune"):
                cmd = _join_cmd([sys.executable, SERVE, *prefix, sub])
                processes.append((4300 + len(processes), cmd))
        self.assertEqual(ae.running_servers(lister=lambda: processes), [])
        with patch.object(ae, "served_review_dir") as probe:
            self.assertEqual(ae.find_orphan_servers({}, lambda: processes), [])
        probe.assert_not_called()

    def test_locale_flag_keeps_registered_service_directory_when_probe_fails(self):
        review, entries = self.registered_review()
        invocations = [
            ["--locale", "en", str(review)],
            ["--locale=en", str(review)],
            ["--locale", "en", "serve", str(review)],
            ["--locale=en", "serve", str(review)],
            ["serve", "--locale", "en", str(review)],
            [str(review), "--locale", "en"],
        ]
        for args in invocations:
            cmd = _join_cmd([sys.executable, SERVE, *args, "--no-open"])
            processes = [(4300, cmd)]
            with self.subTest(args=args), patch.object(ae, "served_review_dir", return_value=None):
                self.assertEqual(ae.running_servers(lister=lambda: processes), processes)
                self.assertEqual(ae.find_orphan_servers(entries, lambda: processes), [])

    def test_rid_like_relative_dir_resolves_against_process_cwd(self):
        review, entries = self.registered_review("20241029")
        cmd = _join_cmd([sys.executable, SERVE, "20241029", "--no-open"])
        with patch.object(ae, "process_cwd", return_value=str(self.root)):
            self.assertEqual(ae.find_orphan_servers(entries, lambda: [(4301, cmd)], lambda port: None), [])

    def test_listening_port_is_numeric_even_when_port_has_service_name(self):
        import socket
        sock = None
        for port in range(1024, 20000):
            try:
                socket.getservbyport(port, "tcp")
            except OSError:
                continue
            candidate = socket.socket()
            try:
                candidate.bind(("127.0.0.1", port))
            except OSError:
                candidate.close()
                continue
            sock = candidate
            break
        if sock is None:
            self.skipTest("找不到空闲且有服务名的端口")
        self.addCleanup(sock.close)
        sock.listen(1)
        self.assertEqual(ae.listening_port_of(os.getpid()), port)

    def test_port_pool_ignores_unassigned_ports(self):
        ctx = self.ctx(port_probe=lambda port: {"app": "other"})
        self.assertEqual(ae.check_port_pool(ctx).level, ae.LEVEL_PASS)

    def test_port_pool_fails_only_for_assigned_port(self):
        review, entries = self.registered_review()
        self.ledger.mkdir()
        (self.ledger / "reviews.json").write_text(
            json.dumps({"version": 1, "reviews": entries}), encoding="utf-8")
        probed = []
        def probe(port):
            return probed.append(port) or {"app": "other"}
        result = ae.check_port_pool(self.ctx(port_probe=probe))
        self.assertEqual(result.level, ae.LEVEL_FAIL)
        self.assertIn("8800", result.message)
        self.assertEqual(probed, [8800])

    def test_copy_skill_refuses_overlapping_source(self):
        src = self.root / "repo" / "skill" / ae.SKILL_NAME
        src.mkdir(parents=True)
        marker = src / "SKILL.md"
        marker.write_text("synthetic skill", encoding="utf-8")
        for dst in (src, src / "nested", src.parent):
            with self.subTest(dst=dst):
                with self.assertRaises(ae.CommandError):
                    ae.copy_skill(src, dst)
                self.assertEqual(marker.read_text(), "synthetic skill")

    def test_copy_skill_refuses_source_spelled_with_different_case(self):
        src = self.root / "repo" / "skill" / ae.SKILL_NAME
        src.mkdir(parents=True)
        marker = src / "SKILL.md"
        marker.write_text("synthetic skill", encoding="utf-8")
        respelled = Path(str(src).replace("repo", "REPO"))
        if not respelled.exists():
            self.skipTest("文件系统区分大小写")
        with self.assertRaises(ae.CommandError):
            ae.copy_skill(src, respelled)
        self.assertEqual(marker.read_text(), "synthetic skill")

    def test_copy_skill_refuses_destination_under_symlink_into_source(self):
        src = self.root / "repo" / "skill" / ae.SKILL_NAME
        (src / "scripts").mkdir(parents=True)
        try:
            (self.root / "skills-link").symlink_to(src / "scripts", target_is_directory=True)
        except OSError as exc:
            if os.name == "nt" and getattr(exc, "winerror", None) == 1314:
                self.skipTest("当前 Windows 账户无符号链接权限")
            raise
        with self.assertRaises(ae.CommandError):
            ae.copy_skill(src, self.root / "skills-link" / ae.SKILL_NAME)   # 目标尚不存在
        self.assertFalse((src / "scripts" / ae.SKILL_NAME).exists())

    def test_health_confirms_registered_dir_with_space(self):
        review, entries = self.registered_review("my review")
        cmd = f"{sys.executable} {SERVE} {review} --no-open"   # ps 输出不保留引号
        def health(pid):
            return {"app": ae.APP, "pid": pid, "dir": str(review), "port": 8800}
        with patch.object(ae, "listening_port_of", return_value=8800):
            self.assertEqual(ae.find_orphan_servers(
                entries, lambda: [(4303, cmd)], lambda port: health(4303)), [])
            orphans = ae.find_orphan_servers(    # health 的 pid 对不上时不采信
                entries, lambda: [(4303, cmd)], lambda port: health(9999))
        self.assertEqual([pid for pid, _, _ in orphans], [4303])

    def test_uninstall_checks_source_overlap_before_removing_links(self):
        repo = self.root / "repo"
        src = repo / ae.SKILL_SRC
        src.mkdir(parents=True)
        (src / "SKILL.md").write_text("synthetic skill", encoding="utf-8")
        link = self.home / ".codex" / "skills" / ae.SKILL_NAME
        link.parent.mkdir(parents=True)
        try:
            link.symlink_to(src, target_is_directory=True)
        except OSError as exc:
            if os.name == "nt" and getattr(exc, "winerror", None) == 1314:
                self.skipTest("当前 Windows 账户无符号链接权限")
            raise
        with patch.object(ae, "__file__", str(repo / "ae.py")), \
                patch.dict(os.environ, {"CLAUDE_SKILLS_DIR": str(src.parent)}), \
                patch.object(ae, "running_servers", return_value=[]), \
                contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(ae.main(["uninstall"]), 2)
        self.assertTrue(link.is_symlink())
        self.assertTrue((src / "SKILL.md").is_file())

    def test_uninstall_refuses_to_remove_skill_source(self):
        repo = self.root / "repo"
        src = repo / ae.SKILL_SRC
        src.mkdir(parents=True)
        (src / "SKILL.md").write_text("synthetic skill", encoding="utf-8")
        with patch.object(ae, "__file__", str(repo / "ae.py")):
            with self.assertRaises(ae.CommandError):
                ae.remove_skill_copy(src)
        self.assertTrue((src / "SKILL.md").is_file())

    def test_purge_guard_runs_before_stopping_servers(self):
        self.ledger.mkdir()
        (self.ledger / "reviews.json").write_text("{invalid", encoding="utf-8")
        server = [(4302, _join_cmd([sys.executable, SERVE, self.root / "r"]))]
        with patch.object(ae, "running_servers", return_value=server), \
                patch.object(ae, "stop_servers", return_value=[]) as stop, \
                contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(ae.main(["uninstall", "--purge", "--stop-servers"]), 2)
        stop.assert_not_called()
        self.assertTrue(self.copy.is_dir())

    def test_purge_removes_leftover_ledger_temp_file(self):
        self.ledger.mkdir()
        (self.ledger / "reviews.json").write_text('{"version":1,"reviews":{}}', encoding="utf-8")
        (self.ledger / ".reviews-abc123.tmp").write_text("{}", encoding="utf-8")
        self.assertEqual(self.uninstall(), 0)
        self.assertFalse(self.ledger.exists())

    def test_process_listing_requests_untruncated_command_lines(self):
        done = subprocess.CompletedProcess([], 0, stdout="", stderr="")
        with patch.object(ae, "_is_windows", return_value=False), \
                patch.object(ae.subprocess, "run", return_value=done) as run:
            ae.list_processes()
        self.assertIn("-ww", run.call_args[0][0])

        with patch.object(ae, "_is_windows", return_value=True), \
                patch.object(ae, "_windows_powershell", return_value="powershell.exe"), \
                patch.object(ae.subprocess, "run", return_value=done) as run_win:
            ae.list_processes()
        self.assertIn("Get-CimInstance", " ".join(run_win.call_args[0][0]))


class Win32CtypesSafetyTests(RegressionCase):
    """Win32 ctypes 句柄与指针 FFI 必须显式配置 argtypes/restype（防止 64 位截断）。"""

    def test_ae_win32_apis_configure_exact_argtypes_and_restypes(self):
        import ctypes
        from ctypes import wintypes

        expected = {
            "OpenProcess": ([wintypes.DWORD, wintypes.BOOL, wintypes.DWORD], wintypes.HANDLE),
            "IsWow64Process": ([wintypes.HANDLE, ctypes.POINTER(wintypes.BOOL)], wintypes.BOOL),
            "ReadProcessMemory": (
                [wintypes.HANDLE, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_size_t, ctypes.POINTER(ctypes.c_size_t)],
                wintypes.BOOL,
            ),
            "GetExitCodeProcess": ([wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)], wintypes.BOOL),
            "TerminateProcess": ([wintypes.HANDLE, wintypes.UINT], wintypes.BOOL),
            "OpenEventW": ([wintypes.DWORD, wintypes.BOOL, wintypes.LPCWSTR], wintypes.HANDLE),
            "SetEvent": ([wintypes.HANDLE], wintypes.BOOL),
            "CloseHandle": ([wintypes.HANDLE], wintypes.BOOL),
            "NtQueryInformationProcess": (
                [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.ULONG, ctypes.POINTER(wintypes.ULONG)],
                ctypes.c_long,
            ),
        }

        class CheckedFn:
            def __init__(self, name, impl):
                self.name = name
                self.impl = impl
                self.argtypes = None
                self.restype = None

            def __call__(self, *args):
                exp_args, exp_res = expected[self.name]
                if self.argtypes != exp_args or self.restype != exp_res:
                    raise AssertionError(f"{self.name} 缺少精确 argtypes/restype: {self.argtypes!r}, {self.restype!r}")
                return self.impl(*args)

        closed = []
        high_handle = (1 << 40) + 0x1234
        cwd_bytes = "C:\\synthetic\\dir".encode("utf-16-le")
        peb_addr = 0x10000
        params_addr = 0x20000
        buf_addr = 0x30000

        def fake_read_mem(_h, addr, out_buf, size, read_ptr):
            raw_addr = addr.value if hasattr(addr, "value") else addr
            if raw_addr == peb_addr + 0x20:
                out_buf._obj.value = params_addr
                read_ptr._obj.value = 8
            elif raw_addr == params_addr + 0x38:
                us = out_buf._obj
                us.Length = len(cwd_bytes)
                us.MaximumLength = len(cwd_bytes)
                us.Buffer = buf_addr
                read_ptr._obj.value = size
            elif raw_addr == buf_addr:
                ctypes.memmove(out_buf, cwd_bytes, len(cwd_bytes))
                read_ptr._obj.value = len(cwd_bytes)
            else:
                return 0
            return 1

        def fake_nt_query(_h, _cls, out_pbi, size, ret_len):
            out_pbi._obj.PebBaseAddress = peb_addr
            ret_len._obj.value = size
            return 0

        def fake_get_exit(_h, code_ptr):
            code_ptr._obj.value = 259
            return 1

        class FakeKernel32:
            OpenProcess = CheckedFn("OpenProcess", lambda *_a: high_handle)
            IsWow64Process = CheckedFn("IsWow64Process", lambda _h, ptr: 1)
            ReadProcessMemory = CheckedFn("ReadProcessMemory", fake_read_mem)
            GetExitCodeProcess = CheckedFn("GetExitCodeProcess", fake_get_exit)
            TerminateProcess = CheckedFn("TerminateProcess", lambda _h, _c: 1)
            OpenEventW = CheckedFn("OpenEventW", lambda *_a: high_handle)
            SetEvent = CheckedFn("SetEvent", lambda _h: 1)
            CloseHandle = CheckedFn("CloseHandle", lambda h: closed.append(h) or 1)

        class FakeNtdll:
            NtQueryInformationProcess = CheckedFn("NtQueryInformationProcess", fake_nt_query)

        def fake_windll(name, use_last_error=False):
            return FakeKernel32() if name == "kernel32" else FakeNtdll()

        with patch.object(ctypes, "WinDLL", side_effect=fake_windll, create=True), \
                patch.object(ae, "_is_windows", return_value=True):
            self.assertEqual(ae._windows_process_cwd(999999), "C:\\synthetic\\dir")
            self.assertFalse(ae._process_stopped(999999))
            self.assertTrue(ae._win_signal_stop_event(999999))
            self.assertTrue(ae._win_terminate_process(999999))
        self.assertEqual(len(closed), 4)

    def test_ae_cli_rejects_unc_target_and_link_paths_before_resolution(self):
        for unc in (r"\\evil-smb\share\skills", "//evil-smb/share/skills", r"\\?\UNC\evil-smb\share\skills"):
            with self.subTest(unc=unc):
                with self.assertRaises(ae.CommandError):
                    ae.resolve_copy_dir(unc)
                with self.assertRaises(ae.CommandError):
                    ae.parse_link_agents(f"custom={unc}")
                with patch.dict(os.environ, {"AE_HOME": unc}):
                    with self.assertRaises(ae.CommandError):
                        ae.ledger_home()

    def test_quote_cli_trailing_backslash_uses_single_quotes_on_windows(self):
        with patch.object(ae, "_is_windows", return_value=True):
            self.assertEqual(ae._quote_cli("C:\\dir with space\\"), "'C:\\dir with space\\'")

    def test_link_targets_copy_handles_windows_prefix_and_case(self):
        copy_dir = self.root / "CustomSkills" / ae.SKILL_NAME
        copy_dir.mkdir(parents=True)
        link = self.root / "agent" / ae.SKILL_NAME
        link.parent.mkdir(parents=True)
        os.symlink(str(copy_dir), str(link))
        with patch.object(ae, "_is_windows", return_value=True), \
                patch.object(ae.os, "readlink", return_value="\\\\?\\" + str(copy_dir).upper()), \
                patch.object(ae.os.path, "realpath", side_effect=lambda p: p.upper()):
            self.assertTrue(ae._link_targets_copy(link, copy_dir))


if __name__ == "__main__":
    unittest.main()
