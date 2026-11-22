"""运行时脚本的公共小工具：原子写入、文件锁、CSP 脚本哈希。只依赖标准库，不导入本目录的其他模块。"""

from __future__ import annotations

import base64
import contextlib
import errno
import hashlib
import os
import re
import shlex
import stat
import sys
import tempfile
import time
from pathlib import Path
from typing import Iterable, Iterator

try:
    import fcntl
except ImportError:
    fcntl = None  # type: ignore[assignment]

_WIN_LOCK_OFFSET = 0x40000000
_WIN_LOCKFILE_FAIL_IMMEDIATELY = 0x00000001
_WIN_LOCKFILE_EXCLUSIVE_LOCK = 0x00000002
_WIN_ERROR_LOCK_VIOLATION = 33
_WIN_REPARSE_POINT_ATTR = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x0400)
_WIN_REPLACE_DELAYS = (0.01, 0.02, 0.04, 0.08, 0.1, 0.1, 0.1)


def _replace_with_retry(src: str | Path, dst: Path) -> None:
    if is_windows():
        for delay in _WIN_REPLACE_DELAYS:
            try:
                os.replace(src, dst)
                return
            except PermissionError:
                time.sleep(delay)
    os.replace(src, dst)


def atomic_write(path: Path, text: str, *, prefix: str, fsync: bool = True) -> None:
    """原子写入文本：同目录临时文件（mkstemp 建出的文件是 0600）+ fsync + os.replace。

    fsync=False 用于频繁刷新、丢了也能重建的文件（如等待标记），省掉每次落盘的等待。

    读方不会读到写了一半的文件；写入或替换失败时删掉临时文件，再把原异常抛出。
    Windows 上短暂的读方共享冲突会触发有限次重试，避免误把瞬时占用当失败。
    """
    fd, tmp = tempfile.mkstemp(prefix=prefix, suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as fh:
            fh.write(text)
            if fsync:
                fh.flush()
                os.fsync(fh.fileno())
        _replace_with_retry(tmp, path)
    except BaseException:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(tmp)
        raise


def atomic_write_bytes(path: Path, data: bytes, *, prefix: str) -> None:
    """原子写入二进制：同目录临时文件（0600）+ os.replace（Windows 共享冲突有限次重试）。"""
    fd, tmp = tempfile.mkstemp(prefix=prefix, suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
        _replace_with_retry(tmp, path)
    except BaseException:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(tmp)
        raise


def _win_lock_api():
    import ctypes
    from ctypes import wintypes

    class _OverlappedOffset(ctypes.Structure):
        _fields_ = [("Offset", wintypes.DWORD), ("OffsetHigh", wintypes.DWORD)]

    class _OverlappedUnion(ctypes.Union):
        _fields_ = [("DUMMYSTRUCTNAME", _OverlappedOffset), ("Pointer", ctypes.c_void_p)]

    class _Overlapped(ctypes.Structure):
        _anonymous_ = ("DUMMYUNIONNAME",)
        _fields_ = [
            ("Internal", ctypes.c_void_p),
            ("InternalHigh", ctypes.c_void_p),
            ("DUMMYUNIONNAME", _OverlappedUnion),
            ("hEvent", wintypes.HANDLE),
        ]

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    lock_ex = kernel32.LockFileEx
    lock_ex.argtypes = [
        wintypes.HANDLE,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.DWORD,
        ctypes.POINTER(_Overlapped),
    ]
    lock_ex.restype = wintypes.BOOL
    unlock_ex = kernel32.UnlockFileEx
    unlock_ex.argtypes = [
        wintypes.HANDLE,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.DWORD,
        ctypes.POINTER(_Overlapped),
    ]
    unlock_ex.restype = wintypes.BOOL
    return ctypes, lock_ex, unlock_ex, _Overlapped


def _win_overlapped(overlapped_cls):
    ov = overlapped_cls()
    ov.Offset = _WIN_LOCK_OFFSET
    ov.OffsetHigh = 0
    return ov


def lock_fd(fd: int, *, exclusive: bool = False, nonblocking: bool = False) -> None:
    """对已打开的文件描述符加锁（共享或独占）；Windows 使用不与文件正文重叠的高位字节锁。"""
    if fcntl is not None and os.name != "nt":
        mode = fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH
        fcntl.flock(fd, mode | (fcntl.LOCK_NB if nonblocking else 0))
        return
    if os.name == "nt":
        import msvcrt

        ctypes, lock_ex, _unlock_ex, overlapped_cls = _win_lock_api()
        handle = msvcrt.get_osfhandle(fd)
        flags = (_WIN_LOCKFILE_EXCLUSIVE_LOCK if exclusive else 0) | (
            _WIN_LOCKFILE_FAIL_IMMEDIATELY if nonblocking else 0
        )
        ov = _win_overlapped(overlapped_cls)
        if not lock_ex(handle, flags, 0, 1, 0, ctypes.byref(ov)):
            err = ctypes.get_last_error()
            if err == _WIN_ERROR_LOCK_VIOLATION:
                raise BlockingIOError(errno.EAGAIN, "Resource temporarily unavailable")
            raise ctypes.WinError(err)
        return
    raise RuntimeError("当前平台缺少可用的文件锁实现")


def unlock_fd(fd: int) -> None:
    """释放 lock_fd 取得的锁。"""
    if fcntl is not None and os.name != "nt":
        fcntl.flock(fd, fcntl.LOCK_UN)
        return
    if os.name == "nt":
        import msvcrt

        ctypes, _lock_ex, unlock_ex, overlapped_cls = _win_lock_api()
        handle = msvcrt.get_osfhandle(fd)
        ov = _win_overlapped(overlapped_cls)
        if not unlock_ex(handle, 0, 1, 0, ctypes.byref(ov)):
            err = ctypes.get_last_error()
            # 退出清理时若未锁或已释放（ERROR_NOT_LOCKED=158），不掩盖主体异常
            if err != 158:
                raise ctypes.WinError(err)
        return
    raise RuntimeError("当前平台缺少可用的文件锁实现")


def _is_reparse_or_symlink(info: os.stat_result) -> bool:
    if stat.S_ISLNK(info.st_mode):
        return True
    return bool((getattr(info, "st_file_attributes", 0) or 0) & _WIN_REPARSE_POINT_ATTR)


def _win_same_owner(path: Path) -> bool:
    """Windows 真实环境核对文件属主 SID 与当前进程令牌用户或默认属主是否一致。"""
    if sys.platform != "win32":
        return True
    import ctypes
    from ctypes import wintypes

    advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

    kernel32.GetCurrentProcess.argtypes = []
    kernel32.GetCurrentProcess.restype = wintypes.HANDLE
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL
    kernel32.LocalFree.argtypes = [wintypes.HLOCAL]
    kernel32.LocalFree.restype = wintypes.HLOCAL

    advapi32.OpenProcessToken.argtypes = [
        wintypes.HANDLE,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.HANDLE),
    ]
    advapi32.OpenProcessToken.restype = wintypes.BOOL
    advapi32.GetTokenInformation.argtypes = [
        wintypes.HANDLE,
        ctypes.c_int,
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD),
    ]
    advapi32.GetTokenInformation.restype = wintypes.BOOL
    advapi32.GetNamedSecurityInfoW.argtypes = [
        wintypes.LPCWSTR,
        ctypes.c_int,
        wintypes.DWORD,
        ctypes.POINTER(ctypes.c_void_p),
        ctypes.POINTER(ctypes.c_void_p),
        ctypes.POINTER(ctypes.c_void_p),
        ctypes.POINTER(ctypes.c_void_p),
        ctypes.POINTER(ctypes.c_void_p),
    ]
    advapi32.GetNamedSecurityInfoW.restype = wintypes.DWORD
    advapi32.EqualSid.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
    advapi32.EqualSid.restype = wintypes.BOOL

    token = wintypes.HANDLE()
    # TOKEN_QUERY = 0x0008
    if not advapi32.OpenProcessToken(kernel32.GetCurrentProcess(), 0x0008, ctypes.byref(token)):
        return False
    owner_sd = ctypes.c_void_p()

    def _query_token_sid(info_class: int) -> tuple[int, object] | None:
        needed = wintypes.DWORD(0)
        advapi32.GetTokenInformation(token, info_class, None, 0, ctypes.byref(needed))
        if not needed.value:
            return None
        buf = ctypes.create_string_buffer(needed.value)
        if not advapi32.GetTokenInformation(token, info_class, buf, needed.value, ctypes.byref(needed)):
            return None
        sid = ctypes.cast(buf, ctypes.POINTER(ctypes.c_void_p))[0]
        if not sid:
            return None
        return sid, buf

    try:
        # Administrators 组用户新建文件属主为 TokenOwner(4)，而非 TokenUser(1)
        allowed_sids = [entry for cls in (1, 4) if (entry := _query_token_sid(cls)) is not None]
        if not allowed_sids:
            return False

        file_sid = ctypes.c_void_p()
        # SE_FILE_OBJECT = 1, OWNER_SECURITY_INFORMATION = 1
        rc = advapi32.GetNamedSecurityInfoW(
            str(path), 1, 1, ctypes.byref(file_sid), None, None, None, ctypes.byref(owner_sd)
        )
        if rc != 0 or not file_sid.value:
            return False
        return any(bool(advapi32.EqualSid(file_sid, sid)) for sid, _buf in allowed_sids)
    finally:
        if owner_sd.value:
            kernel32.LocalFree(owner_sd)
        kernel32.CloseHandle(token)


def check_private_regular_file(
    path: Path,
    *,
    max_bytes: int | None = None,
    error_prefix: str = "file",
    private_error: str | None = None,
    size_error: str | None = None,
) -> os.stat_result:
    """校验 path 为当前用户私有的普通文件（非符号链接/重解析点）且未超限。

    POSIX 检查属主与权限位（无 group/other 位）；Windows 不依赖 NTFS 合成的 0o066 权限位，
    改为拒绝重解析点并核对文件属主 SID。
    """
    info = path.lstat()
    priv_msg = private_error or f"private-{error_prefix}-required"
    size_msg = size_error or f"{error_prefix}-too-large"
    if not stat.S_ISREG(info.st_mode) or _is_reparse_or_symlink(info):
        raise ValueError(priv_msg)
    if is_windows():
        if not _win_same_owner(path):
            raise ValueError(priv_msg)
    else:
        if info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise ValueError(priv_msg)
    if max_bytes is not None and info.st_size > max_bytes:
        raise ValueError(size_msg)
    return info


def read_private_text(path: Path, *, max_bytes: int) -> str:
    """读取已核实身份、权限和大小的本机私有文本。"""
    info = check_private_regular_file(path, max_bytes=max_bytes)
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0))
    with os.fdopen(fd, "rb") as src:
        opened = os.fstat(src.fileno())
        if not os.path.samestat(info, opened) or not stat.S_ISREG(opened.st_mode):
            raise ValueError("private-file-required")
        data = src.read(max_bytes + 1)
        if len(data) > max_bytes:
            raise ValueError("file-too-large")
    return data.decode("utf-8")


@contextlib.contextmanager
def flocked(
    path: Path,
    *,
    exclusive: bool = False,
    nonblocking: bool = False,
    nofollow: bool = False,
    deadline: float | None = None,
    dir_fd: int | None = None,
) -> Iterator[int]:
    """打开锁文件、按模式加锁、退出时解锁：文件锁只有一个实现，各处只差显式参数。

    exclusive：独占（LOCK_EX）还是共享（LOCK_SH）；nonblocking：拿不到锁立刻抛
    BlockingIOError，否则等到 deadline（monotonic 时间）或获得锁；nofollow：拒绝跟随符号链接（O_NOFOLLOW）——按各处原来的
    行为，只有台账的更新协调锁为真，其余为假（放行符号链接的兼容问题另行评估）。
    dir_fd：在已打开的目录内解析相对 path，供需要固定目录身份的调用方使用；缺省保持原调用方式。
    锁文件以 0600 新建；yield 文件描述符，调用方不需要时可以忽略。
    锁的粒度与持有范围由调用方的 with 块决定，这里不加也不减。
    """
    target = Path(path)
    if nofollow and os.name == "nt":
        try:
            info = target.lstat()
        except FileNotFoundError:
            info = None
        if info is not None and _is_reparse_or_symlink(info):
            raise OSError(errno.ELOOP, "Too many levels of symbolic links", str(target))
    flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_BINARY", 0) | (getattr(os, "O_NOFOLLOW", 0) if nofollow else 0)
    fd = os.open(target, flags, 0o600) if dir_fd is None else os.open(target, flags, 0o600, dir_fd=dir_fd)
    try:
        fh = os.fdopen(fd, "r+")
    except BaseException:
        os.close(fd)
        raise
    with fh:
        if nofollow and os.name == "nt":
            try:
                cur = target.lstat()
                opened = os.fstat(fh.fileno())
                if _is_reparse_or_symlink(cur) or (cur.st_dev, cur.st_ino) != (opened.st_dev, opened.st_ino):
                    raise OSError(errno.ELOOP, "Too many levels of symbolic links", str(target))
            except FileNotFoundError:
                raise OSError(errno.ELOOP, "Too many levels of symbolic links", str(target)) from None
        if deadline is None or nonblocking:
            lock_fd(fh.fileno(), exclusive=exclusive, nonblocking=nonblocking)
        else:
            while True:
                try:
                    lock_fd(fh.fileno(), exclusive=exclusive, nonblocking=True)
                    break
                except BlockingIOError:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise
                    time.sleep(min(0.02, remaining))
        try:
            yield fh.fileno()
        finally:
            unlock_fd(fh.fileno())


_PS_UNSAFE_RE = re.compile(r"[^A-Za-z0-9_./:\\-]")


def is_windows() -> bool:
    return os.name == "nt"


def is_unc_or_device_path(raw: str | os.PathLike[str], *, allow_local_extended: bool = False) -> bool:
    """检测 UNC 共享路径（\\\\server\\share、//server/share、\\\\?\\UNC\\...）或 Windows 设备路径（\\\\.\\...、\\\\?\\...）。

    allow_local_extended=True 时放行本地驱动器规范前缀（如系统 API 返回的 \\\\?\\C:\\...），仍拦截 UNC 与 \\\\.\\ 设备路径。
    """
    s = os.fspath(raw).strip().replace("\\", "/")
    if not (len(s) > 2 and s.startswith("//") and s[2] != "/"):
        return False
    if allow_local_extended and len(s) >= 7 and s[:4] == "//?/" and s[4].isalpha() and s[5] == ":" and s[6] == "/":
        return False
    return True


def python_cli() -> str:
    return "python" if is_windows() else "python3"


def quote_cli(arg: str) -> str:
    """按当前平台 shell 习惯引用命令行参数：POSIX 用 shlex.quote，Windows 用 PowerShell/cmd 兼容写法。"""
    if not is_windows():
        return shlex.quote(arg)
    if arg and not _PS_UNSAFE_RE.search(arg):
        return arg
    if not any(ch in arg for ch in ('"', "$", "`")) and not arg.endswith("\\"):
        return f'"{arg}"'
    return "'" + arg.replace("'", "''") + "'"


def join_cli(argv: Iterable[str]) -> str:
    """将命令行参数列表拼接成当前平台可直接复制执行的命令串。"""
    return " ".join(quote_cli(str(arg)) for arg in argv)


def split_cli(cmd: str) -> list[str]:
    """按当前平台规则拆分命令行字符串，保留 Windows 路径反斜杠并兼容 PowerShell 单引号。"""
    if not is_windows():
        return shlex.split(cmd)
    args: list[str] = []
    i, n = 0, len(cmd)
    while i < n:
        while i < n and cmd[i] in " \t\r\n":
            i += 1
        if i >= n:
            break
        buf: list[str] = []
        in_double = False
        while i < n:
            ch = cmd[i]
            if not in_double and ch in " \t\r\n":
                break
            if not in_double and not buf and ch == "'":
                i += 1
                while i < n:
                    if cmd[i] == "'":
                        if i + 1 < n and cmd[i + 1] == "'":
                            buf.append("'")
                            i += 2
                            continue
                        i += 1
                        break
                    buf.append(cmd[i])
                    i += 1
                else:
                    raise ValueError("No closing quotation")
                continue
            if ch == "\\":
                bs = 0
                while i < n and cmd[i] == "\\":
                    bs += 1
                    i += 1
                if i < n and cmd[i] == '"':
                    buf.append("\\" * (bs // 2))
                    if bs % 2 == 1:
                        buf.append('"')
                        i += 1
                    continue
                buf.append("\\" * bs)
                continue
            if ch == '"':
                in_double = not in_double
                i += 1
                continue
            buf.append(ch)
            i += 1
        if in_double:
            raise ValueError("No closing quotation")
        args.append("".join(buf))
    return args


INLINE_SCRIPT = re.compile(r"<script>(.*?)</script>", re.S)


def script_hashes(html_text: str) -> str:
    """页面里内联脚本的 sha256 列表（CSP 的 script-src 用）：服务响应头与页面 meta 共用这一份算法。

    两个出口各有一条自己的策略串（响应头版带 frame-ancestors 与无脚本时的兜底，meta 版都没有），
    但哈希算法、匹配的正则与 base64 编码只在这里算一次。
    """
    return " ".join(
        "'sha256-" + base64.b64encode(hashlib.sha256(s.encode("utf-8")).digest()).decode() + "'"
        for s in INLINE_SCRIPT.findall(html_text)
    )


def configure_stdio() -> None:
    """把标准输出与标准错误切到 UTF-8，避免 Windows 孤立模式（-I）或 cp1252 终端输出中文时报错。"""
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if callable(reconfigure):
            try:
                reconfigure(encoding="utf-8", errors="replace")
            except (OSError, ValueError):
                pass
