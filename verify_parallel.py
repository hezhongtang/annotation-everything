"""验证组内的有界 POSIX 进程调度；沿用调用方已持有的资源锁。

独立任务各自拥有进程组，fork 继承原锁描述符。敏感及未知单元在调用方原进程
执行；调度器只回收本次创建的进程族，不枚举或停止宿主机的其他进程。
"""
from __future__ import annotations

import contextlib
import json
import os
from pathlib import Path
import signal
import sys
import time
import traceback


class InterruptedRun(KeyboardInterrupt):
    def __init__(self, results, signum=signal.SIGINT):
        self.results, self.signum = results, signum


def isolate_environment(root: Path) -> None:
    home = root / "home"
    home.mkdir(parents=True)
    os.environ.update(HOME=str(home), AE_HOME=str(root / "ledger"),
                      CLAUDE_SKILLS_DIR=str(home / ".claude" / "skills"),
                      CODEX_HOME=str(home / ".codex"), XDG_DATA_HOME=str(home / ".local" / "share"))


def _signal_family(pid, signum):
    try:
        os.killpg(pid, signum)
    except ProcessLookupError:
        return False
    return True


def _stop_families(pids, grace=1.0, reap=None):
    """只处理本次登记的进程组；SIGCONT 使被 SIGSTOP 的受控服务也能退出。"""
    if reap:
        reap()  # macOS 对尚未 waitpid 的 zombie 进程组发信号会返回 EPERM。
    remaining = []
    for pid in pids:
        if _signal_family(pid, signal.SIGTERM):
            remaining.append(pid)
            _signal_family(pid, signal.SIGCONT)
    if remaining:
        deadline = time.monotonic() + grace
        while time.monotonic() < deadline:
            if reap:
                reap()
            time.sleep(0.01)
    for pid in remaining:
        _signal_family(pid, signal.SIGKILL)


def run_tasks(tasks, worker, root: Path, jobs: int = 3, inline=None):
    """worker(参数, JSON 输出路径) 返回原始退出码；可选 inline 在调用方进程执行。

    并发数包含 inline。每个 fork 任务有独立环境、日志和进程组；其非分离后代
    都属于本任务的收尾范围。使用分离会话的敏感测试必须留在调用方执行。
    """
    tasks = list(tasks)
    keys = [key for key, _ in tasks] + ([inline[0]] if inline else [])
    if not 1 <= jobs <= 3 or len(set(keys)) != len(keys) or (inline and jobs < 2 and tasks):
        raise ValueError("任务 ID 必须唯一，并发数必须在 1–3 之间且容纳调用方任务")
    root.mkdir(parents=True, exist_ok=True)
    active, results, families = {}, {}, set()
    pending = iter(enumerate(tasks))
    exhausted, inline_done = False, inline is None
    cancelled_signal = signal.SIGINT
    cancel_signals = {signal.SIGTERM, signal.SIGINT, signal.SIGHUP}

    def terminate(signum, _frame):
        nonlocal cancelled_signal
        cancelled_signal = signum
        mask = signal.pthread_sigmask(signal.SIG_BLOCK, cancel_signals)
        try:
            for sig in cancel_signals:
                signal.signal(sig, signal.SIG_IGN)
        finally:
            signal.pthread_sigmask(signal.SIG_SETMASK, mask)
        raise KeyboardInterrupt

    previous_handlers = {sig: signal.signal(sig, terminate) for sig in cancel_signals}

    def remember(pid, status):
        item = active.pop(pid)
        started = item.pop("started")
        try:
            ended = json.loads((item["output"].parent / "timing.json").read_text())["ended"]
            if not isinstance(ended, (int, float)) or not started <= ended <= time.monotonic():
                raise ValueError("invalid timing")
            item["seconds"] = round(ended - started, 6)
            item["timing_precision"] = "measured"
        except (OSError, ValueError, KeyError, TypeError):
            item["seconds"] = round(time.monotonic() - started, 6)
            item["timing_precision"] = "reaped-upper-bound"
        item["exit_status"] = os.waitstatus_to_exitcode(status)
        results[item["id"]] = item

    def location(key, index):
        directory = root / str(index)
        directory.mkdir()
        return directory, {"id": key, "output": directory / "result.json", "log": directory / "output.log",
                           "started": time.monotonic()}

    try:
        while active or not exhausted or not inline_done:
            capacity = jobs - (0 if inline_done else 1)
            while len(active) < capacity and not exhausted:
                try:
                    index, (key, payload) = next(pending)
                except StopIteration:
                    exhausted = True
                    break
                directory, item = location(key, index)
                sys.stdout.flush()
                sys.stderr.flush()
                # 信号只能在登记完成后交付；子进程先建立进程组，再恢复原信号屏蔽集。
                old_mask = signal.pthread_sigmask(signal.SIG_BLOCK, cancel_signals)
                try:
                    pid = os.fork()
                    if pid == 0:
                        code = 1
                        try:
                            os.setpgid(0, 0)
                            signal.pthread_sigmask(signal.SIG_SETMASK, old_mask)
                            with item["log"].open("w", encoding="utf-8") as stream:
                                os.dup2(stream.fileno(), 1)
                                os.dup2(stream.fileno(), 2)
                                # 调用方可能已重定向到其他 fd；重新绑定 Python 文本流。
                                sys.stdout = os.fdopen(os.dup(1), "w", buffering=1, encoding="utf-8", errors="backslashreplace")
                                sys.stderr = os.fdopen(os.dup(2), "w", buffering=1, encoding="utf-8", errors="backslashreplace")
                                isolate_environment(directory)
                                code = worker(payload, item["output"])
                        except KeyboardInterrupt:
                            code = 128 + cancelled_signal
                        except BaseException:
                            traceback.print_exc()
                        finally:
                            sys.stdout.flush()
                            sys.stderr.flush()
                            try:
                                (directory / "timing.json").write_text(json.dumps({"ended": time.monotonic()}))
                            except OSError:
                                pass
                        os._exit(code if isinstance(code, int) and 0 <= code < 256 else 1)
                    active[pid] = item
                    families.add(pid)
                    # parent/child 都可先执行；父级设置保证取消时进程组已经存在。
                    try:
                        os.setpgid(pid, pid)
                    except ProcessLookupError:
                        pass
                    except PermissionError:
                        if os.getpgid(pid) != pid:
                            raise
                finally:
                    signal.pthread_sigmask(signal.SIG_SETMASK, old_mask)
            if not inline_done:
                key, payload = inline
                _directory, item = location(key, "inline")
                code = 128 + cancelled_signal
                try:
                    with item["log"].open("w", encoding="utf-8") as stream, \
                            contextlib.redirect_stdout(stream), contextlib.redirect_stderr(stream):
                        code = worker(payload, item["output"])
                finally:
                    item["seconds"] = round(time.monotonic() - item.pop("started"), 6)
                    item["inline"] = True
                    item["exit_status"] = code
                    results[key] = item
                    inline_done = True
                if code in (129, 130, 143):
                    raise KeyboardInterrupt
            old_mask = signal.pthread_sigmask(signal.SIG_BLOCK, cancel_signals)
            try:
                completed = []
                for pid in list(active):
                    waited, status = os.waitpid(pid, os.WNOHANG)
                    if waited:
                        remember(pid, status)
                        completed.append(pid)
                _stop_families(completed)
                families.difference_update(completed)
            finally:
                signal.pthread_sigmask(signal.SIG_SETMASK, old_mask)
            if active and not completed:
                time.sleep(0.01)
    except BaseException as error:
        # 取消收尾本身不接受第二个终止信号；所有部分结果收齐后再向调用方报告取消。
        for sig in cancel_signals:
            signal.signal(sig, signal.SIG_IGN)
        def reap_ready():
            for pid in list(active):
                waited, status = os.waitpid(pid, os.WNOHANG)
                if waited:
                    remember(pid, status)
        _stop_families(list(families), reap=reap_ready)
        for pid in list(active):
            _pid, status = os.waitpid(pid, 0)
            remember(pid, status)
        if isinstance(error, KeyboardInterrupt):
            raise InterruptedRun(results, cancelled_signal) from error
        raise
    finally:
        for sig, handler in previous_handlers.items():
            signal.signal(sig, handler)
    return results
