"""端到端测试的服务生命周期与兜底清理。

`e2e_browser.py` 模块级依赖 playwright，`python3 -m unittest discover tests`
导不进来，所以它无浏览器依赖的那一半放这里：起停 `serve.py`，加一张
子进程 / 临时目录登记表。

为什么需要登记：测试在多个用例里反复起服务，一旦被 Ctrl-C、未捕获异常或
SIGTERM / SIGHUP 打断，已启动的服务会变成残留服务继续占端口，`mkdtemp` 的
临时目录也会留在 /tmp（issue #16）。`atexit` 覆盖正常退出、未捕获异常与
KeyboardInterrupt；SIGTERM 与 SIGHUP 不触发 `atexit`，另接信号处理。

清理期间会把 SIGINT / SIGTERM / SIGHUP 临时挡掉：用户连按两次 Ctrl-C，或清理耗时
期间又补一个 kill，都不该把还没收完的进程留下。

这张登记表和「审阅台账」不是一回事——后者按审阅标识记录目录与端口。

等待参数（issue #51）：TERMINATE_TIMEOUT / READY_TIMEOUT 是缺省配置，语义与
产品超时一致；测试通过受控参数（TestCleanup(terminate_timeout=...) 与
start_server(ready_timeout=...)）注入更短的等待，不扩散成产品开关。
"""
from __future__ import annotations

import atexit
import contextlib
import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "skill" / "annotation-everything" / "scripts"

TERMINATE_TIMEOUT = 5.0
READY_TIMEOUT = 8.0
BLOCKED_SIGNALS = (signal.SIGINT, signal.SIGTERM, signal.SIGHUP)


class TestCleanup:
    """登记测试拉起的子进程与临时目录，退出时统一收掉。"""

    def __init__(self, terminate_timeout: float | None = None):
        self._procs: list[subprocess.Popen] = []
        self._dirs: list[Path] = []
        # None 表示沿用模块缺省；测试可注入短等待，缺省配置不变
        self.terminate_timeout = TERMINATE_TIMEOUT if terminate_timeout is None else terminate_timeout

    def add(self, proc: subprocess.Popen) -> None:
        self._procs.append(proc)

    def discard(self, proc: subprocess.Popen) -> None:
        if proc in self._procs:
            self._procs.remove(proc)

    def add_dir(self, path: Path) -> None:
        self._dirs.append(path)

    def stop_all(self) -> None:
        """回收所有登记项；可重复调用。清理期间挡掉信号，防止被二次打断。"""
        with _signals_deferred():
            while self._procs:
                proc = self._procs[0]
                reap(proc, timeout=self.terminate_timeout)
                self._procs.remove(proc)   # 收掉了才摘，中途被打断也不会丢下剩下的
            dirs, self._dirs = self._dirs, []
            for d in dirs:
                shutil.rmtree(d, ignore_errors=True)


@contextlib.contextmanager
def _signals_deferred(signals=BLOCKED_SIGNALS):
    """清理期间把中断信号挡在外面。

    只屏蔽主线程的信号掩码不够：信号可能由别的线程发出并直接在那个线程被接住，
    C 层一置位，主线程下一个检查点照样抛 KeyboardInterrupt。信号处置是进程级的，
    所以同时把这几个信号临时设成 SIG_IGN，清理完再还原。
    """
    deferred = {sig: signal.signal(sig, signal.SIG_IGN) for sig in signals}
    previous = signal.pthread_sigmask(signal.SIG_BLOCK, signals)
    try:
        yield
    finally:
        signal.pthread_sigmask(signal.SIG_SETMASK, previous)
        for sig, handler in deferred.items():
            signal.signal(sig, handler)


def reap(proc: subprocess.Popen, timeout: float | None = None) -> tuple[str | None, str | None]:
    """结束子进程并返回管道输出；不响应 SIGTERM 的升级到 SIGKILL。

    timeout 缺省用 TERMINATE_TIMEOUT；测试可注入短等待（issue #51）。
    """
    if timeout is None:
        timeout = TERMINATE_TIMEOUT
    if proc.poll() is None:
        proc.terminate()
    try:
        return proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        proc.kill()
        return proc.communicate()


CLEANUP = TestCleanup()
_INSTALLED = False


def install(cleanup: TestCleanup = CLEANUP) -> None:
    """在 e2e_browser.py 导入时调用一次。"""
    global _INSTALLED
    if _INSTALLED:
        return
    _INSTALLED = True
    atexit.register(cleanup.stop_all)
    handler = _cleanup_handler(cleanup)
    for sig in (signal.SIGTERM, signal.SIGHUP):
        signal.signal(sig, handler)


def _cleanup_handler(cleanup: TestCleanup):
    def handler(signum, _frame):
        signal.signal(signum, signal.SIG_IGN)   # 清理期间再来的同号信号直接丢掉
        try:
            cleanup.stop_all()
        finally:
            signal.signal(signum, signal.SIG_DFL)
            os.kill(os.getpid(), signum)
    return handler


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def start_server(d: Path, port: int, cleanup: TestCleanup = CLEANUP, *, scripts: Path | None = None,
                 ready_timeout: float | None = None) -> subprocess.Popen:
    scripts = SCRIPTS if scripts is None else scripts
    if ready_timeout is None:
        ready_timeout = READY_TIMEOUT
    p = subprocess.Popen([sys.executable, str(scripts / "serve.py"), str(d), "--port", str(port), "--no-open"],
                         stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    cleanup.add(p)
    end = time.monotonic() + ready_timeout
    while time.monotonic() < end:
        info = d / ".annotation-server.json"
        if info.exists() and json.loads(info.read_text()).get("port") == port:
            return p
        time.sleep(0.1)
    _, stderr = reap(p, timeout=cleanup.terminate_timeout)
    cleanup.discard(p)   # 启动失败也先完成回收，中断时仍由退出清理接手
    raise RuntimeError("server did not start on the expected port: " + (stderr or ""))


def stop(p: subprocess.Popen, cleanup: TestCleanup = CLEANUP) -> None:
    reap(p, timeout=cleanup.terminate_timeout)
    cleanup.discard(p)   # 回收成功后才注销；被中断时保留登记，交给退出清理
