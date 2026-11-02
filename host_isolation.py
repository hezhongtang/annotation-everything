"""同机资源隔离协议（issue #52）：验证参与方按共享资源互斥，而不是按目录。

谁是参与者：verify.py 的验证组、直接跑的 unittest discover、直接跑的
tests/e2e_browser.py。它们在启动资源敏感的验证前先取锁，结束后释放。

锁只协调参与者。外部进程（用户真实服务、别的程序）不参与协议，锁既看不到
它们，也绝不会去停止它们——这是与「清理登记子进程」并列的安全边界。

机制：fcntl.flock 排他锁。锁文件放在同一用户共享的位置（XDG_RUNTIME_DIR 或
系统临时目录），跨 worktree、跨独立 checkout 生效；进程无论正常退出、失败还
是被 SIGKILL，内核都会在最后一个持有该文件描述符的进程退出后释放锁。验证入口
把锁描述符传给执行中的组，入口被杀时不会让仍运行的组失去保护。锁文件内容只是
持有者诊断信息，所有权判断以
flock 为准。只用 Python 标准库，语法兼容 Python 3.9；依赖 POSIX flock
（macOS 与 Linux 均支持，Windows 不在支持范围）。
"""
from __future__ import annotations

import datetime
import fcntl
import json
import os
import tempfile
import time
from pathlib import Path
from typing import Optional

# 嵌套参与者的重入豁免：祖先已持有的资源集合经环境变量传给子进程。
# verify.py 持锁后拉起的测试子进程看到它，就不再对同一资源重复 flock
# （不同进程对同一锁文件 flock 会互相阻塞，不豁免就是自锁）。
HELD_ENV = "AE_HELD_LOCKS"

# 锁等待上限（秒）。可被 AE_LOCK_WAIT_SECONDS 覆盖；等待超时不是通过。
DEFAULT_WAIT_SECONDS = 600.0

# 等待期间的 flock 重试间隔（秒）。
_RETRY_INTERVAL = 0.05

# 资源组声明。顺序即多资源获取顺序（按名称排序），所有参与方一致，避免交叉死锁。
# - host-state：全机进程视图 + 共享端口池。doctor / uninstall 的真实 ps 全机枚举
#   与端口探测读取它；任何测试启动的真实 serve.py 都会改变它。读取者与改变者
#   必须互斥，因此 unit（含真实路径）与 browser（启动真实服务）同组互斥。
#   进程视图与端口池按整台机器共享，超出任何检出目录，锁范围是 machine-user。
# - ledger-and-skill-copy：台账与技能副本。测试已全部通过 AE_HOME /
#   CLAUDE_SKILLS_DIR 等环境变量注入合成位置，没有跨进程共享的可变状态，
#   因此声明为不需要互斥（登记在此是为了说明「为什么独立」）。
RESOURCE_DECLARATIONS = {
    "host-state": {
        "title": "宿主机进程视图与端口池",
        "scope": "machine-user",
        "mutually_exclusive": True,
        "shared_by": "同一用户在同一台机器上的全部检出（worktree、独立 checkout）",
        "why": "doctor / uninstall 用真实 ps 枚举本机全部进程并探测端口，"
               "任何并行启动的真实 serve.py 都会进入盘点结果、被误判为残留服务；"
               "端口分配也共享同一内核端口池。状态属于机器与用户，不属于任何一个检出，"
               "按检出加锁挡不住别的 worktree 或独立 checkout 里的参与者。",
    },
    "ledger-and-skill-copy": {
        "title": "审阅台账与技能副本",
        "scope": "machine-user",
        "mutually_exclusive": False,
        "shared_by": "同一用户的真实台账与技能副本位置",
        "why": "测试与 eval 已全部通过 AE_HOME、CLAUDE_SKILLS_DIR、HOME 注入合成位置，"
               "真实台账与技能副本从不被读写，没有跨进程共享的可变状态，不需要互斥。"
               "登记在此说明独立的理由，也提醒后续改动：一旦测试开始共享这些位置，"
               "就必须改为互斥资源。",
    },
}

# 验证组要求的资源。unit-related 按选择器涉及的模块动态判定（见 group_resources）。
GROUP_RESOURCES = {
    "unit": ("host-state",),
    "browser": ("host-state",),
}

# 资源敏感的测试模块（unittest 可发现的）。这些模块在导入时经
# require_for_module 取锁，直接跑 unittest discover 时同样受协议保护。
# e2e_browser.py 不进 discover，单独在文件头接入。
MODULE_RESOURCES = {
    "tests.test_agent_delivery": ("host-state",),     # 普通 CLI 重启的合成服务
    "tests.test_ae_checks_runtime": ("host-state",),  # 真实 probe_port / ps 枚举 + 真实 serve.py
    "tests.test_ae_cli": ("host-state",),              # doctor / uninstall 真实子进程读全机视图
    "tests.test_ae_repair": ("host-state",),           # 真实 serve.py + doctor 子进程
    "tests.test_ae_target": ("host-state",),           # --target doctor 真实子进程读全机视图
    "tests.test_ae_uninstall": ("host-state",),        # 真实 serve.py + real_ps 用例
    "tests.test_core": ("host-state",),                # ServerTests / StaleServerTests 启动真实 serve.py
    "tests.test_e2e_servers": ("host-state",),         # 真实 serve.py 生命周期
    "tests.test_host_interference": ("host-state",),   # 真实 serve.py × 真实 doctor 干扰回归
    "tests.e2e_browser": ("host-state",),              # 直跑入口：真实 serve.py + 浏览器
}

# 合成资源名：只用于协议自身的测试与顺序获取演示，不映射到真实组。
SYNTHETIC_RESOURCES = ("synthetic-a", "synthetic-b")

# MODULE_RESOURCES 的键所在的测试包。同一模块有两种加载形态：显式
# `python3 -m unittest tests.test_x` 得到带包前缀的模块名；discover
# （`python3 -m unittest discover tests` 与 verify.py 的 _unit-runner）把
# tests 目录作为顶层目录加载，得到无前缀的顶层名。查表必须两种都认。
TEST_PACKAGE = "tests"


class UnknownResourceError(Exception):
    """资源名不在声明里：拼错名字应当失败，而不是静默拿到一把没人认识的锁。"""


class LockTimeoutError(Exception):
    """锁等待超过上限。这不是通过：调用方必须按非成功处理。"""

    def __init__(self, resource: str, waited: float):
        super().__init__(resource)
        self.resource = resource
        self.waited_seconds = waited


def _all_resources() -> dict:
    known = dict(RESOURCE_DECLARATIONS)
    for name in SYNTHETIC_RESOURCES:
        known[name] = {"title": "合成资源（协议测试用）", "scope": "test-only",
                       "mutually_exclusive": True, "shared_by": "测试临时锁目录",
                       "why": "验证多资源按序获取与跨进程互斥，不对应真实共享状态。"}
    return known


def resource_declarations() -> dict:
    """可读取的资源组声明：哪些资源互斥、哪些独立、归属为何超出单个目录。"""
    return {name: dict(entry) for name, entry in _all_resources().items()}


def resource_order() -> list:
    """多资源获取的固定顺序（名称排序）；所有参与方一致，避免交叉死锁。"""
    return sorted(_all_resources())


def lock_root(env=None) -> Path:
    """锁文件根目录：同一用户在同一台机器上共享，不依赖某个人的绝对目录。

    AE_LOCK_DIR 可覆盖（测试与特殊环境用）。缺省依次取 XDG_RUNTIME_DIR（Linux
    用户运行时目录，每用户独立）与系统临时目录（macOS 的 TMPDIR 同样每用户
    独立）。目录名包含用户 UID，Linux 回退到共享 /tmp 时不同用户也不会争用
    同一目录的写权限。两个检出无论是不是同一个仓库的 worktree，解析结果都相同。
    """
    env = os.environ if env is None else env
    override = env.get("AE_LOCK_DIR")
    if override:
        return Path(override)
    runtime = env.get("XDG_RUNTIME_DIR")
    base = Path(runtime) if runtime else Path(tempfile.gettempdir())
    return base / ("annotation-everything-locks-%s" % os.getuid())


def wait_limit(env=None) -> float:
    """锁等待上限（秒）。AE_LOCK_WAIT_SECONDS 覆盖缺省值。"""
    env = os.environ if env is None else env
    raw = env.get("AE_LOCK_WAIT_SECONDS")
    if raw is None:
        return DEFAULT_WAIT_SECONDS
    try:
        value = float(raw)
    except ValueError:
        return DEFAULT_WAIT_SECONDS
    return value if value > 0 else DEFAULT_WAIT_SECONDS


def normalize_resources(resources) -> tuple:
    """资源集合 -> 按固定顺序去重后的元组；未知资源直接报错。"""
    known = _all_resources()
    names = set(resources or ())
    unknown = names - set(known)
    if unknown:
        raise UnknownResourceError("未声明的资源：%s（可选：%s）"
                                   % ("、".join(sorted(unknown)), "、".join(resource_order())))
    return tuple(name for name in resource_order() if name in names)


def held_from_env(env=None) -> set:
    """环境变量里祖先已持有的资源集合（重入豁免依据）。"""
    env = os.environ if env is None else env
    raw = env.get(HELD_ENV, "")
    return {item for item in raw.split(":") if item}


class LockHandle:
    """一次 acquire 的结果。borrowed_resources 是借自祖先的资源子集（不真正持有）。

    全部借入时没有任何新获取，release 对锁与豁免标记都是空操作；部分借入时
    release 只撤销本次新获取部分的豁免标记，借入部分仍属于祖先，不能动。

    env 记住 acquire 时写入持有集合的环境：release 不带 env 参数时改它，
    避免调用方拿 os.environ 之外的 env 获取、却又把 os.environ 的豁免标记
    误清掉——那会让同进程后续的子参与者失去豁免、与宿主持有的锁互相等待。
    """

    def __init__(self, resources: tuple, borrowed_resources, waited_seconds: float,
                 files: Optional[list] = None, env=None):
        self.resources = resources
        self.borrowed_resources = tuple(borrowed_resources or ())
        self.borrowed = bool(self.borrowed_resources)
        self.waited_seconds = waited_seconds
        self._files = files or []  # [(资源名, 打开的锁文件对象)]；借入时为空
        self.env = env

    def describe(self) -> dict:
        return {"resources": list(self.resources), "borrowed": self.borrowed,
                "borrowed_resources": list(self.borrowed_resources),
                "wait_seconds": round(self.waited_seconds, 6)}

    def file_descriptors(self) -> tuple:
        """本次实际持有的锁描述符，传给组子进程以覆盖入口先退出的时序。"""
        return tuple(handle.fileno() for _name, handle in self._files)


def _write_holder_note(path: Path, resource: str) -> None:
    """把持有者诊断信息写进锁文件。只用于观测（报告谁在持有），所有权以 flock 为准。"""
    try:
        path.write_text(json.dumps({
            "pid": os.getpid(),
            "resource": resource,
            "at": datetime.datetime.now(datetime.timezone.utc).astimezone().isoformat(timespec="seconds"),
        }, ensure_ascii=False) + "\n", encoding="utf-8")
    except OSError:
        pass


def read_holder_note(resource: str, env=None) -> Optional[dict]:
    """读某资源当前锁文件里的诊断信息；文件没有或读不出时返回 None。"""
    path = lock_root(env) / ("%s.lock" % resource)
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def acquire(resources, timeout: Optional[float] = None, env=None):
    """按固定顺序获取一组资源的排他锁；祖先已持有的资源按借入处理。

    - resources 为空时立即返回（纯逻辑参与方不被无关锁阻塞）；
    - timeout 缺省取 wait_limit()；等待超时抛 LockTimeoutError，已获取的资源先释放；
    - 成功后把本次持有的资源并入 env 的 HELD_ENV 集合，子进程据此豁免重复加锁。
    本函数绝不会停止任何进程：等不到就等，等到上限就失败。
    """
    import_env = os.environ if env is None else env
    names = normalize_resources(resources)
    timeout = wait_limit(import_env) if timeout is None else timeout
    held = held_from_env(import_env)
    to_lock = [name for name in names if name not in held]
    borrowed = tuple(name for name in names if name in held)
    if not to_lock:
        return LockHandle(names, borrowed, 0.0, env=import_env)
    root = lock_root(import_env)
    started = time.monotonic()
    files = []
    try:
        for name in to_lock:
            root.mkdir(parents=True, exist_ok=True, mode=0o700)
            path = root / ("%s.lock" % name)
            handle = open(path, "a+", encoding="utf-8")
            files.append((name, handle))
            while True:
                try:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except OSError:
                    if time.monotonic() - started >= timeout:
                        waited = time.monotonic() - started
                        raise LockTimeoutError(name, waited)
                    time.sleep(_RETRY_INTERVAL)
            _write_holder_note(path, name)
    except BaseException:
        for _name, handle in files:
            try:
                handle.close()
            except OSError:
                pass
        raise
    waited = time.monotonic() - started
    held.update(to_lock)
    import_env[HELD_ENV] = ":".join(sorted(held))
    return LockHandle(names, borrowed, waited, files, env=import_env)


def release(handle: LockHandle, env=None) -> None:
    """释放锁（关闭文件描述符即释放）。借入的资源不属于本进程，不动。

    豁免标记只撤销本次新获取的部分（resources 减去 borrowed_resources）：
    部分借入时祖先的标记必须原样保留，否则祖先仍在持有的资源会被当成
    已释放、同进程后续的子参与者失去豁免、与祖先的锁互相等待。
    回退到 acquire 实际写入的环境（handle.env）：显式传入 env 以兼容
    旧调用方，但不带 env 时绝不去动 os.environ——handle 不是从它获取的。
    """
    import_env = env if env is not None else handle.env
    if import_env is None:
        import_env = os.environ
    for _name, file in getattr(handle, "_files", None) or []:
        try:
            file.close()
        except OSError:
            pass
    handle._files = []
    acquired = set(handle.resources) - set(handle.borrowed_resources)
    if acquired:
        remaining = held_from_env(import_env) - acquired
        if remaining:
            import_env[HELD_ENV] = ":".join(sorted(remaining))
        else:
            import_env.pop(HELD_ENV, None)


def resources_for_module(module_name: str) -> Optional[tuple]:
    """模块名 -> 资源要求；带包前缀（tests.test_x）与 discover 的顶层名（test_x）都命中。

    discover 把 tests 目录作为顶层目录加载，模块 __name__ 没有包前缀；显式
    `python3 -m unittest tests.test_x` 才有。两种加载形态必须是同一模块，
    查表口径不同会让 discover 直跑时锁协议整体失效。
    """
    if module_name in MODULE_RESOURCES:
        return MODULE_RESOURCES[module_name]
    if "." not in module_name:
        return MODULE_RESOURCES.get("%s.%s" % (TEST_PACKAGE, module_name))
    return None


def require_for_module(module_name: str, env=None) -> Optional[LockHandle]:
    """测试模块导入时声明资源要求：直接跑 unittest 的旧入口由此进入同一协议。

    等待超时抛 LockTimeoutError——unittest 会把它记成模块级错误，明确非成功，
    而不是把等待超时悄悄当通过。返回的句柄存活到进程结束（模块导入级持有），
    discover 的单进程按序执行因此整组互斥。
    """
    resources = resources_for_module(module_name)
    if not resources:
        return None
    handle = acquire(resources, env=env)
    _module_handles.append(handle)
    return handle


_module_handles: list = []  # 进程退出时内核回收全部 fd，这里只保引用防提前释放
