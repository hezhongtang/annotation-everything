"""验证组内部的有界 POSIX 子进程调度；不获取、替代或绕过 host-state 锁。

调用方必须先持有验证组的资源锁。fork 继承其描述符和进程组；外部验证仍互斥，
父进程提前退出时仍运行的子进程继续持锁。仅执行调用方明确声明可隔离的任务。
"""
from __future__ import annotations

import os
from pathlib import Path
import signal
import sys
import time
import traceback


class InterruptedRun(KeyboardInterrupt):
    def __init__(self, results):
        self.results = results


def isolate_environment(root: Path) -> None:
    """子进程独享合成台账与技能目录；不继承其他任务可变的用户目录。"""
    home = root / "home"
    home.mkdir(parents=True)
    os.environ.update(HOME=str(home), AE_HOME=str(root / "ledger"),
                      CLAUDE_SKILLS_DIR=str(home / ".claude" / "skills"),
                      CODEX_HOME=str(home / ".codex"), XDG_DATA_HOME=str(home / ".local" / "share"))


def run_tasks(tasks, worker, root: Path, jobs: int = 3):
    """tasks 为 [(唯一 ID, 参数)]；worker(参数, JSON 输出路径) 返回原始退出码。

    每个任务独享日志、输出与合成环境。结果包括真实退出状态；缺失输出由调用方
    判为失败。异常或信号中断时先回收本次登记的任务，再携带已完成结果抛出中断。
    """
    tasks = list(tasks)
    if not 1 <= jobs <= 3 or len({key for key, _ in tasks}) != len(tasks):
        raise ValueError("任务 ID 必须唯一，并发数必须在 1–3 之间")
    root.mkdir(parents=True, exist_ok=True)
    active, results = {}, {}
    pending = iter(enumerate(tasks))
    exhausted = False

    def terminate(_signum, _frame):
        raise KeyboardInterrupt

    old_term = signal.signal(signal.SIGTERM, terminate)
    try:
        while active or not exhausted:
            while len(active) < jobs and not exhausted:
                try:
                    index, (key, payload) = next(pending)
                except StopIteration:
                    exhausted = True
                    break
                directory = root / str(index)
                directory.mkdir()
                output, log = directory / "result.json", directory / "output.log"
                sys.stdout.flush()
                sys.stderr.flush()
                started = time.monotonic()
                pid = os.fork()
                if pid == 0:
                    code = 1
                    try:
                        with log.open("w", encoding="utf-8") as stream:
                            os.dup2(stream.fileno(), 1)
                            os.dup2(stream.fileno(), 2)
                            isolate_environment(directory)
                            code = worker(payload, output)
                    except KeyboardInterrupt:
                        code = 130
                    except BaseException:
                        traceback.print_exc()
                    finally:
                        sys.stdout.flush()
                        sys.stderr.flush()
                    os._exit(code if isinstance(code, int) and 0 <= code < 256 else 1)
                active[pid] = {"id": key, "output": output, "log": log, "started": started}
            finished = False
            for pid in list(active):
                waited, status = os.waitpid(pid, os.WNOHANG)
                if not waited:
                    continue
                item = active.pop(pid)
                item["seconds"] = round(time.monotonic() - item.pop("started"), 6)
                item["exit_status"] = os.waitstatus_to_exitcode(status)
                results[item["id"]] = item
                finished = True
            if active and not finished:
                time.sleep(0.01)
    except BaseException as error:
        # SIGTERM 让任务自己的 finally 先回收服务、浏览器与登记的子进程。
        for pid in active:
            try:
                os.kill(pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
        deadline = time.monotonic() + 5
        while active and time.monotonic() < deadline:
            for pid in list(active):
                if os.waitpid(pid, os.WNOHANG)[0]:
                    active.pop(pid)
            if active:
                time.sleep(0.01)
        for pid in active:
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            os.waitpid(pid, 0)
        if isinstance(error, KeyboardInterrupt):
            raise InterruptedRun(results) from error
        raise
    finally:
        signal.signal(signal.SIGTERM, old_term)
    return results
