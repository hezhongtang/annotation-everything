"""审阅台账：本机所有已知审阅的登记簿（按审阅标识记录审阅目录与专属端口）。

位置：环境变量 AE_HOME 指定的目录，默认 ~/.annotation-everything/（目录 0700，文件 0600）。
只存审阅标识、审阅目录路径、专属端口；审阅名、运行状态等一律现场从审阅目录读，业务信息不离开审阅目录。

台账只是索引，事实以审阅目录为准：
- 生成对照页、启动服务、运行 notes.py 时登记（目录搬了家就按标识更新路径）；
- 专属端口在第一次启动服务时分配，此后归这份审阅独占，服务停了也不让给别的审阅，只有 forget 或 prune 才释放；
- 目录不在、标识对不上的条目只标出来，不自动删除（外接盘暂时没挂上时不误删、不释放端口）。

台账读写失败不应挡住生成、启动、批注这些主流程：调用方捕获 LedgerError / OSError，只给警告。

语言：LedgerError 的人读文案按界面语言变（`AE_LOCALE`，默认中文）；台账路径、标识、
端口与恢复命令保持原文。调用方（serve.py / notes.py / ae.py）只负责打印。
"""
from __future__ import annotations

import contextlib
import hashlib
import json
import os
import re
import stat
import sys
import tempfile
import uuid
from pathlib import Path
from typing import Any, Callable, Collection, Iterator

sys.path.insert(0, str(Path(__file__).resolve().parent))
import fsutil  # noqa: E402
import i18n  # noqa: E402

LEDGER_FILE = "reviews.json"
WAIT_PREFIX = ".annotation-wait."   # 审阅目录里：每个 notes.py wait 进程留一个 .annotation-wait.<pid>.json，退出时删掉
STATUS_FILE = ".annotation-server.json"   # 审阅目录里：服务运行状态（pid/端口/地址），停止时删除；服务与更新器共用这个名字
METADATA_LOCK_FILE = ".annotation-metadata.lock"   # manifest / 运行状态写入与失效元数据清理的审阅级互斥


def wait_file(review_dir: Path, pid: int) -> Path:
    return Path(review_dir) / f"{WAIT_PREFIX}{pid}.json"
LOCK_FILE = ".reviews.lock"
BASE_PORT = 8765
MAX_SCAN = 500          # 分配专属端口时最多往后找多少个
RID = re.compile(r"[0-9a-f]{16}")
RID_PREFIX = re.compile(r"[0-9a-f]{4,16}")
# 总览首页宿主：保留端口记录与运行状态都放在台账目录，独立于审阅条目（不登记成审阅、不改台账结构）
OVERVIEW_PORT_FILE = "overview-home.json"       # 保留端口记录：{"version": 1, "port": N}
HOST_KIND_OVERVIEW = "overview-home"            # /api/health 与页面注入的宿主种类；审阅宿主不带宿主种类字段
OVERVIEW_PAGE_FILE = ".overview-home.html"      # 仅总览视图的首页页面（build_overview_home 生成）
OVERVIEW_LOG_FILE = "overview-home.log"         # 首页服务日志
OVERVIEW_STATUS_FILE = ".overview-home-status.json"   # 首页运行状态（pid/端口/宿主标识），退出时删除
OVERVIEW_START_LOCK_FILE = ".overview-home-start.lock"   # 并发首次启动的串行化锁


def overview_port_file() -> Path:
    return home() / OVERVIEW_PORT_FILE


def _read_overview_port(path: Path) -> int | None:
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    port = doc.get("port") if isinstance(doc, dict) else None
    return port if isinstance(port, int) and not isinstance(port, bool) and 1024 <= port <= 65535 else None


def overview_reserved_port() -> int | None:
    """保留端口记录里的端口；没有记录或记录不合法返回 None（读失败不挡主流程，与台账读取同款降级）。"""
    return _read_overview_port(overview_port_file())


def ensure_home() -> Path:
    """台账目录（不存在就按 0700 创建）：首页的启动锁、页面、运行状态与保留记录都写在这里。"""
    return _ensure_home()


# 界面语言只有进程级一个来源（见 i18n.set_locale）：入口设置一次，台账与技能发现等各模块
# 共同遵循。定住后不再被 AE_LOCALE 覆盖——否则同一进程里两种来源不一致，会出现中英混排


def set_locale(locale: object = None) -> None:
    """薄转发：定进程级界面语言。locale 为 None 时按 AE_LOCALE 重新解析。"""
    i18n.set_locale(locale)


def _refresh_locale() -> None:
    """没有外部定过语言时，才按 AE_LOCALE 刷新；非法选择保持中文，不让台账操作失败。"""
    i18n.refresh_locale()


def T(key: str, **params: object) -> str:
    """按当前语言取一条消息；缺译时回退另一种语言（见 locales/README.md 的契约）。"""
    return i18n.T(key, **params)


class LedgerError(Exception):
    """台账读不了或请求的操作不成立（端口已归别的审阅、标识前缀有歧义等）。"""


class CopiedDir(LedgerError):
    """这个目录是从台账里登记的那份审阅复制来的（原目录还在、标识相同）：不让副本顶替原审阅的条目和专属端口。"""


def _reject_unc_path(raw: str | Path) -> None:
    if fsutil.is_unc_or_device_path(raw, allow_local_extended=True):
        raise LedgerError(f"Unsupported UNC or device path: {raw}")


def home() -> Path:
    env = os.environ.get("AE_HOME")
    if env:
        _reject_unc_path(env)
        return Path(env).expanduser().resolve()
    return Path.home() / ".annotation-everything"


def overview_host_id() -> str:
    """同一台账目录的首页标识固定，不把台账路径放进页面或健康响应。"""
    digest = hashlib.sha256(os.fsencode(home().resolve())).hexdigest()
    return f"{HOST_KIND_OVERVIEW}-{digest}"


def update_lock_path() -> Path:
    copy = Path(__file__).resolve().parent.parent
    return copy.parent / ".annotation-everything-update.lock"


@contextlib.contextmanager
def update_lock(*, exclusive: bool = False, nonblocking: bool = False):
    """按技能副本路径协调，不同 AE_HOME 的服务共用一把锁。"""
    with fsutil.flocked(update_lock_path(), exclusive=exclusive, nonblocking=nonblocking,
                        nofollow=True) as fd:
        yield fd


def status_file(review_dir: Path) -> Path:
    """审阅目录里的服务运行状态文件（pid / 端口 / 地址；只含元数据，不读批注）。"""
    return Path(review_dir) / STATUS_FILE


@contextlib.contextmanager
def metadata_lock(review_dir: Path, *, exclusive: bool = True):
    """manifest 与运行状态写入方共用的审阅级锁；调用方不得在持锁时登记台账。"""
    with fsutil.flocked(Path(review_dir) / METADATA_LOCK_FILE, exclusive=exclusive, nofollow=True) as fd:
        yield fd


@contextlib.contextmanager
def _metadata_lock_fd(directory_fd: Any):
    """在已固定的目录内持有同一把审阅级锁，避免路径切换影响清理临界区。"""
    if isinstance(directory_fd, _WinMetadataDir):
        directory = directory_fd.current_path()
        with fsutil.flocked(directory / METADATA_LOCK_FILE, exclusive=True, nofollow=True) as fd:
            yield fd
        return
    with fsutil.flocked(METADATA_LOCK_FILE, exclusive=True, nofollow=True, dir_fd=directory_fd) as fd:
        yield fd


def pid_alive(pid: int) -> bool:
    """这个 pid 的进程还在不在：POSIX 发信号 0，Windows 查 OpenProcess + 退出码；没权限时按活着算。"""
    if pid <= 0:
        return False
    if os.name == "nt":
        if pid > 0xFFFFFFFF:
            raise OverflowError("pid out of range")
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
        kernel32.GetExitCodeProcess.restype = wintypes.BOOL
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel32.CloseHandle.restype = wintypes.BOOL

        # PROCESS_QUERY_LIMITED_INFORMATION = 0x1000, ERROR_ACCESS_DENIED = 5, STILL_ACTIVE = 259
        handle = kernel32.OpenProcess(0x1000, False, pid)
        if not handle:
            return ctypes.get_last_error() == 5
        try:
            code = wintypes.DWORD()
            if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
                return False
            return code.value == 259
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def _ensure_home() -> Path:
    h = home()
    h.mkdir(mode=0o700, parents=True, exist_ok=True)
    return h


def _empty() -> dict[str, Any]:
    return {"version": 1, "reviews": {}}


def _read(path: Path) -> dict[str, Any]:
    _refresh_locale()
    if not path.exists():
        return _empty()
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        raise LedgerError(T("ledger.err.unreadable", path=path, type=type(e).__name__)) from None
    if not isinstance(doc, dict) or not isinstance(doc.get("reviews"), dict):
        raise LedgerError(T("ledger.err.structure", path=path))
    # 只留合法条目：标识 16 位十六进制，目录是字符串，端口是整数或空
    clean: dict[str, dict[str, Any]] = {}
    for rid, e in doc["reviews"].items():
        if RID.fullmatch(str(rid)) and isinstance(e, dict) and isinstance(e.get("dir"), str):
            port = e.get("port")
            clean[rid] = {"dir": e["dir"], "port": port if isinstance(port, int) and not isinstance(port, bool)
                          and 1024 <= port <= 65535 else None}
    return {"version": 1, "reviews": clean}


def _write(path: Path, doc: dict[str, Any]) -> None:
    fsutil.atomic_write(path, json.dumps(doc, ensure_ascii=False, indent=2) + "\n", prefix=".reviews-")


@contextlib.contextmanager
def _edit(*, rollback: Callable[[], None] | None = None) -> Iterator[dict[str, Any]]:
    """在台账文件锁下读改写：多个 serve / build 同时分配端口也不会撞号。"""
    _refresh_locale()
    h = _ensure_home()
    with fsutil.flocked(h / LOCK_FILE, exclusive=True):
        path = h / LEDGER_FILE
        doc = _read(path)
        before = json.dumps(doc, sort_keys=True)
        writing = False
        try:
            yield doc
            if json.dumps(doc, sort_keys=True) != before:
                writing = True
                _write(path, doc)
        except BaseException:
            # 中断可能发生在原子替换之后。提交状态以锁内读到的台账为准。
            if rollback is not None and (not writing or json.dumps(_read(path), sort_keys=True) == before):
                rollback()
            raise


def load() -> dict[str, Any]:
    return _read(home() / LEDGER_FILE)


def entries() -> dict[str, dict[str, Any]]:
    return load()["reviews"]


def get(rid: str) -> dict[str, Any] | None:
    return entries().get(rid)


def _set_dir(reviews: dict[str, Any], rid: str, d: str) -> dict[str, Any]:
    """取（或新建）条目并把目录更新为 d。d 是从登记在册的那份审阅复制来的副本：拒绝。

    原目录已不在（整个搬走了、或已删除）才按标识更新路径。候选原目录是台账登记的目录，
    比的是登记用的标识（d 里可能还没有 review.json），不看 d 的 manifest 里的 reviewDir。
    """
    e = reviews.get(rid)
    if e is None:
        e = reviews[rid] = {"dir": d, "port": None}
    elif e["dir"] != d:
        if copied_from(d, {"reviewId": rid}, origin=Path(e["dir"])) is not None:
            raise CopiedDir(T("ledger.err.copied", dir=d, old=Path(e["dir"]), rid=rid[:8]))
        e["dir"] = d
    return e


def register(rid: str, review_dir: Path) -> dict[str, Any]:
    """登记（或更新目录）一份审阅，返回它的条目。标识不合法时不登记。"""
    _refresh_locale()   # 标识非法的报错也可能先于任何读写发生，语言要已经定好
    if not RID.fullmatch(str(rid or "")):
        raise LedgerError(T("ledger.err.badRid", rid=rid))
    _reject_unc_path(review_dir)
    d = str(Path(review_dir).resolve())
    with _edit() as doc:
        return dict(_set_dir(doc["reviews"], rid, d))


def owners() -> dict[int, str]:
    """端口 → 拥有它的审阅标识。"""
    return {e["port"]: rid for rid, e in entries().items() if e.get("port")}


def assign_port(rid: str, review_dir: Path, *, prefer: int | None = None,
                is_free: Callable[[int], bool] = lambda p: True) -> int:
    """返回这份审阅的专属端口；还没有就分配一个，此后不变。

    prefer：已在运行的审阅第一次登记时，认领它正在用的端口（没被别的审阅认领的话）；
    否则从 8765 起，挑台账里没人认领、此刻也没被占用的最小端口。
    首页保留端口不算空闲：无论首页在不在运行，审阅的专属端口都不落在它上面。
    """
    _reject_unc_path(review_dir)
    d = str(Path(review_dir).resolve())
    with _edit() as doc:
        reviews = doc["reviews"]
        e = _set_dir(reviews, rid, d)
        if e.get("port"):
            return int(e["port"])
        taken = {x["port"] for k, x in reviews.items() if k != rid and x.get("port")}
        reserved = _read_overview_port(overview_port_file())
        if reserved is not None:
            taken.add(reserved)
        if prefer and 1024 <= prefer <= 65535 and prefer not in taken:
            e["port"] = prefer
            return prefer
        for p in range(BASE_PORT, BASE_PORT + MAX_SCAN):
            if p not in taken and is_free(p):
                e["port"] = p
                return p
        raise LedgerError(T("ledger.err.portsExhausted", base=BASE_PORT,
                            end=BASE_PORT + MAX_SCAN - 1))


def claim_port(rid: str, review_dir: Path, port: int) -> None:
    """把专属端口改成 port。端口归别的审阅或已保留给总览首页时拒绝（先 forget 那一份）。"""
    if not 1024 <= port <= 65535:
        raise LedgerError(T("ledger.err.portRange", port=port))
    _reject_unc_path(review_dir)
    d = str(Path(review_dir).resolve())
    with _edit() as doc:
        if _read_overview_port(overview_port_file()) == port:
            raise LedgerError(T("ledger.err.overviewPortTaken", port=port))
        for k, x in doc["reviews"].items():
            if k != rid and x.get("port") == port:
                raise LedgerError(T("ledger.err.portTaken", port=port, rid=k, short=k[:8]))
        _set_dir(doc["reviews"], rid, d)["port"] = port


def assign_overview_port(*, prefer: int | None = None,
                         is_free: Callable[[int], bool] = lambda p: True) -> int | None:
    """总览首页的保留端口：记录已有就直接用；没有才在台账写锁内挑一个写进独立记录。

    prefer：首次分配时优先考虑的端口（缺省从 8765 起）。候选避开台账里审阅已认领的专属端口
    与调用方 is_free 判为占用的端口；记录落盘与审阅条目的分配共用同一把台账写锁。
    记录里的端口已被某个审阅认领（旧副本登记过同号专属端口）时返回 None：当次顺延，
    双方记录都保留，下次无显式端口时再次尝试保留地址。挑不到可用端口也返回 None。
    """
    with _edit() as doc:
        taken = {x["port"] for x in doc["reviews"].values() if x.get("port")}
        path = overview_port_file()
        current = _read_overview_port(path)
        if current is not None:
            return None if current in taken else current
        candidates = ([prefer] if prefer and 1024 <= prefer <= 65535 else []) \
            + list(range(BASE_PORT, BASE_PORT + MAX_SCAN))
        for p in candidates:
            if p not in taken and is_free(p):
                fsutil.atomic_write(path, json.dumps({"version": 1, "port": p}, ensure_ascii=False) + "\n",
                                    prefix=".overview-home-")
                return p
        return None


def match(key: str) -> str:
    """按完整标识或唯一前缀（至少 4 位）找到台账里的审阅标识。"""
    key = str(key or "").strip().lower()
    if not RID_PREFIX.fullmatch(key):
        raise LedgerError(T("ledger.err.badKey", value=repr(key)))
    hits = [rid for rid in entries() if rid.startswith(key)]
    if not hits:
        raise LedgerError(T("ledger.err.noSuchKey", prefix=key))
    if len(hits) > 1:
        raise LedgerError(T("ledger.err.ambiguousKey", prefix=key,
                            hits=T("cli.ae.listSeparator").join(h[:8] for h in hits)))
    return hits[0]


def forget(key: str) -> tuple[str, dict[str, Any]]:
    rid = match(key)
    with _edit() as doc:
        e = doc["reviews"].pop(rid, None)
    if e is None:
        raise LedgerError(T("ledger.err.gone", rid=rid))
    return rid, e


def _default_temp_roots() -> tuple[Path, ...]:
    if os.name != "nt":
        return (Path("/tmp"), Path("/private/tmp"), Path("/var/folders"))
    roots: list[Path] = []
    seen: set[str] = set()
    for candidate in (tempfile.gettempdir(), os.environ.get("TEMP"), os.environ.get("TMP")):
        if not candidate:
            continue
        path = Path(candidate)
        key = os.path.normcase(str(path))
        if key not in seen:
            seen.add(key)
            roots.append(path)
    return tuple(roots)


TEMP_ROOTS = _default_temp_roots()
PRUNE_METADATA_FILES = (".annotation-archived", ".agent-binding.json", ".agent-deliveries.json",
                        ".annotation-stop.json")


def in_temp_area(directory: Path) -> bool:
    resolved = directory.resolve()
    for root in TEMP_ROOTS:
        try:
            if directory.is_relative_to(root):
                return True
        except ValueError:
            pass
        try:
            if resolved.is_relative_to(root.resolve()):
                return True
        except (OSError, ValueError):
            pass
    return False


class _MetadataUnsafe(Exception):
    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


class _WinMetadataDir:
    """Windows 下固定已打开的审阅目录身份：持有共享删除句柄并按需解析实时规范路径。"""

    def __init__(self, initial_path: Path, root: Path | None, handle: int, identity: tuple[int, int]):
        self.initial_path = initial_path
        self.root = root
        self.handle = handle
        self.identity = identity
        self.closed = False

    @staticmethod
    def _strip_device_prefix(raw: str) -> str:
        if raw.startswith("\\\\?\\UNC\\"):
            return "\\\\" + raw[8:]
        if raw.startswith("\\\\?\\"):
            return raw[4:]
        return raw

    def current_path(self) -> Path:
        if self.closed or not self.handle:
            raise _MetadataUnsafe("directory-unavailable")
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        get_final = kernel32.GetFinalPathNameByHandleW
        get_final.argtypes = [wintypes.HANDLE, wintypes.LPWSTR, wintypes.DWORD, wintypes.DWORD]
        get_final.restype = wintypes.DWORD
        needed = get_final(self.handle, None, 0, 0)
        if not needed:
            raise _MetadataUnsafe("directory-unavailable")
        buf = ctypes.create_unicode_buffer(needed + 1)
        written = get_final(self.handle, buf, len(buf), 0)
        if not written or written >= len(buf):
            raise _MetadataUnsafe("directory-unavailable")
        clean = self._strip_device_prefix(buf.value)
        resolved = Path(clean).resolve()
        if self.root is not None and not resolved.is_relative_to(self.root):
            raise _MetadataUnsafe("out-of-scope")
        try:
            info = resolved.lstat()
        except OSError:
            raise _MetadataUnsafe("directory-unavailable") from None
        if not stat.S_ISDIR(info.st_mode) or fsutil._is_reparse_or_symlink(info):
            raise _MetadataUnsafe("out-of-scope")
        if (info.st_dev, info.st_ino) != self.identity:
            raise _MetadataUnsafe("directory-changed")
        return resolved

    def close(self) -> None:
        if self.closed or not self.handle:
            return
        self.closed = True
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel32.CloseHandle.restype = wintypes.BOOL
        kernel32.CloseHandle(self.handle)
        self.handle = 0


def _close_metadata_dir(dir_ref: Any) -> None:
    if isinstance(dir_ref, _WinMetadataDir):
        dir_ref.close()
    else:
        os.close(dir_ref)


def _dir_identity(dir_ref: Any) -> tuple[int, int]:
    if isinstance(dir_ref, _WinMetadataDir):
        return dir_ref.identity
    info = os.fstat(dir_ref)
    return (info.st_dev, info.st_ino)


def _read_metadata_json(fd: Any, name: str, *, missing: Any) -> Any:
    if isinstance(fd, _WinMetadataDir):
        directory = fd.current_path()
        target = directory / name
        try:
            info = target.lstat()
        except FileNotFoundError:
            return missing
        except OSError:
            raise _MetadataUnsafe("identity-unconfirmed") from None
        if not stat.S_ISREG(info.st_mode) or fsutil._is_reparse_or_symlink(info):
            raise _MetadataUnsafe("identity-unconfirmed")
        flags = os.O_RDONLY | getattr(os, "O_BINARY", 0)
        try:
            raw = os.open(str(target), flags)
        except FileNotFoundError:
            return missing
        except OSError:
            raise _MetadataUnsafe("identity-unconfirmed") from None
        try:
            opened = os.fstat(raw)
            if not stat.S_ISREG(opened.st_mode) or (opened.st_dev, opened.st_ino) != (info.st_dev, info.st_ino):
                raise _MetadataUnsafe("identity-unconfirmed")
            with os.fdopen(raw, "r", encoding="utf-8") as fh:
                raw = -1
                try:
                    return json.load(fh)
                except (OSError, ValueError):
                    raise _MetadataUnsafe("identity-unconfirmed") from None
        finally:
            if raw >= 0:
                os.close(raw)
    flags = os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW
    try:
        raw = os.open(name, flags, dir_fd=fd)
    except FileNotFoundError:
        return missing
    except OSError:
        raise _MetadataUnsafe("identity-unconfirmed") from None
    try:
        if not stat.S_ISREG(os.fstat(raw).st_mode):
            raise _MetadataUnsafe("identity-unconfirmed")
        with os.fdopen(raw, "r", encoding="utf-8") as fh:
            raw = -1
            try:
                return json.load(fh)
            except (OSError, ValueError):
                raise _MetadataUnsafe("identity-unconfirmed") from None
    finally:
        if raw >= 0:
            os.close(raw)


def _open_metadata_dir(directory: Path, root: Path | None) -> tuple[Any, Path]:
    if fsutil.is_windows():
        try:
            orig_info = directory.lstat()
        except OSError:
            raise _MetadataUnsafe("directory-unavailable") from None
        if fsutil._is_reparse_or_symlink(orig_info):
            raise _MetadataUnsafe("out-of-scope")
        resolved = directory.resolve()
        if root is not None and not resolved.is_relative_to(root):
            raise _MetadataUnsafe("out-of-scope")
        try:
            info = resolved.lstat()
        except OSError:
            raise _MetadataUnsafe("directory-unavailable") from None
        if not stat.S_ISDIR(info.st_mode) or fsutil._is_reparse_or_symlink(info):
            raise _MetadataUnsafe("directory-unavailable")
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateFileW.argtypes = [
            wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p,
            wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE,
        ]
        kernel32.CreateFileW.restype = wintypes.HANDLE
        # FILE_READ_ATTRIBUTES (0x80), FILE_SHARE_READ|WRITE|DELETE (0x07), OPEN_EXISTING (3), FILE_FLAG_BACKUP_SEMANTICS (0x02000000)
        handle = kernel32.CreateFileW(str(resolved), 0x80, 0x07, None, 3, 0x02000000, None)
        invalid_handle = wintypes.HANDLE(-1).value
        if not handle or handle == invalid_handle:
            raise _MetadataUnsafe("directory-unavailable")
        win_dir = _WinMetadataDir(resolved, root, handle, (info.st_dev, info.st_ino))
        try:
            confirmed = directory.resolve()
            current = confirmed.lstat()
            if confirmed != resolved or (root is not None and not confirmed.is_relative_to(root)):
                raise _MetadataUnsafe("out-of-scope")
            if fsutil._is_reparse_or_symlink(current) or (info.st_dev, info.st_ino) != (current.st_dev, current.st_ino):
                raise _MetadataUnsafe("directory-changed")
            return win_dir, confirmed
        except BaseException:
            win_dir.close()
            raise
    resolved = directory.resolve()
    if root is not None and not resolved.is_relative_to(root):
        raise _MetadataUnsafe("out-of-scope")
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(resolved, flags)
    except (OSError, TypeError):
        raise _MetadataUnsafe("directory-unavailable") from None
    try:
        confirmed = directory.resolve()
        opened, current = os.fstat(fd), os.stat(confirmed)
        if confirmed != resolved or (root is not None and not confirmed.is_relative_to(root)):
            raise _MetadataUnsafe("out-of-scope")
        if (opened.st_dev, opened.st_ino) != (current.st_dev, current.st_ino):
            raise _MetadataUnsafe("directory-changed")
        return fd, confirmed
    except BaseException:
        os.close(fd)
        raise


def _metadata_plan(fd: Any, rid: str) -> tuple[str, str | None]:
    try:
        manifest = _read_metadata_json(fd, "review.json", missing=None)
    except _MetadataUnsafe:
        manifest = None
    try:
        status = _read_metadata_json(fd, STATUS_FILE, missing={})
    except _MetadataUnsafe:
        status = None
    status_valid = isinstance(status, dict) and ("pid" not in status
                    or (type(status["pid"]) is int and status["pid"] > 0))
    if status_valid and type(status.get("pid")) is int:
        try:
            if pid_alive(status["pid"]):
                return "skip", "running-review"
        except (OSError, OverflowError, ValueError):
            return "preserve", "status-unconfirmed"
    if not isinstance(manifest, dict):
        return "preserve", "identity-unconfirmed"
    if "reviewId" in manifest:
        owner = manifest["reviewId"]
        if not isinstance(owner, str) or not RID.fullmatch(owner):
            return "preserve", "identity-unconfirmed"
        if owner == rid:
            return "skip", "recovered"
        return "preserve", "owned-by-other-review"
    if not status_valid:
        return "preserve", "status-unconfirmed"
    return "delete", None


def _metadata_names(fd: Any) -> list[str]:
    if isinstance(fd, _WinMetadataDir):
        directory = fd.current_path()
        names = []
        for name in sorted(PRUNE_METADATA_FILES):
            target = directory / name
            try:
                info = os.stat(target, follow_symlinks=False)
            except FileNotFoundError:
                continue
            if not stat.S_ISREG(info.st_mode) and not stat.S_ISLNK(info.st_mode):
                raise OSError(f"metadata is not a file: {name}")
            names.append(name)
        for name in sorted(os.listdir(directory)):
            if not name.startswith(WAIT_PREFIX) or not name.endswith(".json") or name in names:
                continue
            info = os.stat(directory / name, follow_symlinks=False)
            if not stat.S_ISREG(info.st_mode) and not stat.S_ISLNK(info.st_mode):
                raise OSError(f"metadata is not a file: {name}")
            names.append(name)
        return names
    names = []
    for name in sorted(PRUNE_METADATA_FILES):
        try:
            info = os.stat(name, dir_fd=fd, follow_symlinks=False)
        except FileNotFoundError:
            continue
        if not stat.S_ISREG(info.st_mode) and not stat.S_ISLNK(info.st_mode):
            raise OSError(f"metadata is not a file: {name}")
        names.append(name)
    for name in sorted(os.listdir(fd)):
        if not name.startswith(WAIT_PREFIX) or not name.endswith(".json") or name in names:
            continue
        info = os.stat(name, dir_fd=fd, follow_symlinks=False)
        if not stat.S_ISREG(info.st_mode) and not stat.S_ISLNK(info.st_mode):
            raise OSError(f"metadata is not a file: {name}")
        names.append(name)
    return names


def _restore_metadata_file(fd: Any, quarantine: str, name: str) -> None:
    if isinstance(fd, _WinMetadataDir):
        directory = fd.current_path()
        source = directory / quarantine / name
        target = directory / name
        if not os.path.lexists(source):
            return
        if os.path.lexists(target):
            raise OSError(f"rollback target exists; quarantine kept: {quarantine}/{name}")
        os.rename(str(source), str(target))
        return
    source = f"{quarantine}/{name}"
    try:
        os.link(source, name, src_dir_fd=fd, dst_dir_fd=fd, follow_symlinks=False)
    except FileNotFoundError:
        # 尚未隔离或已经恢复的文件不需要再移动。
        return
    except FileExistsError:
        raise OSError(f"rollback target exists; quarantine kept: {source}") from None
    os.unlink(source, dir_fd=fd)


def _quarantine_metadata(item: dict[str, Any]) -> None:
    if not item["names"]:
        return
    fd = item["fd"]
    quarantine = f".annotation-prune-{uuid.uuid4().hex}"
    if isinstance(fd, _WinMetadataDir):
        directory = fd.current_path()
        os.mkdir(directory / quarantine, mode=0o700)
        item["quarantine"] = quarantine
        for name in item["names"]:
            os.rename(str(directory / name), str(directory / quarantine / name))
        return
    os.mkdir(quarantine, mode=0o700, dir_fd=fd)
    item["quarantine"] = quarantine
    for name in item["names"]:
        os.rename(name, f"{quarantine}/{name}", src_dir_fd=fd, dst_dir_fd=fd)


def _rollback_quarantines(prepared: list[dict[str, Any]]) -> None:
    errors = []
    for item in reversed(prepared):
        if not item["quarantine"]:
            continue
        fd, quarantine = item["fd"], item["quarantine"]
        try:
            win_dir = fd.current_path() if isinstance(fd, _WinMetadataDir) else None
        except _MetadataUnsafe as error:
            errors.append(f"{item['directory'] / quarantine}: {error.reason}")
            continue
        for name in reversed(item["names"]):
            try:
                _restore_metadata_file(fd, quarantine, name)
            except (OSError, _MetadataUnsafe) as error:
                detail = error.reason if isinstance(error, _MetadataUnsafe) else str(error)
                errors.append(f"{item['directory'] / quarantine / name}: {detail}")
        try:
            if win_dir is not None:
                os.rmdir(win_dir / quarantine)
            else:
                os.rmdir(quarantine, dir_fd=fd)
        except FileNotFoundError:
            pass
        except OSError as error:
            errors.append(f"{item['directory'] / quarantine}: {error}")
    if errors:
        raise LedgerError(T("ledger.err.pruneRollback", detail="; ".join(errors[:3])))


def _cleanup_quarantines(prepared: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    deleted, errors = [], []
    for item in prepared:
        if not item["quarantine"]:
            continue
        fd, quarantine = item["fd"], item["quarantine"]
        try:
            win_dir = fd.current_path() if isinstance(fd, _WinMetadataDir) else None
        except _MetadataUnsafe as error:
            errors.append({**item["row"], "reason": "metadata-cleanup-failed",
                           "quarantine": str(item["directory"] / quarantine), "detail": error.reason})
            continue
        failure = None
        for name in item["names"]:
            try:
                if win_dir is not None:
                    os.unlink(win_dir / quarantine / name)
                else:
                    os.unlink(f"{quarantine}/{name}", dir_fd=fd)
            except OSError as error:
                failure = str(error)
                break
        if failure is None:
            try:
                if win_dir is not None:
                    os.rmdir(win_dir / quarantine)
                else:
                    os.rmdir(quarantine, dir_fd=fd)
            except OSError as error:
                failure = str(error)
        if failure is None:
            deleted.append(item["row"])
        else:
            errors.append({**item["row"], "reason": "metadata-cleanup-failed",
                           "quarantine": str(item["directory"] / quarantine), "detail": failure})
    return deleted, errors


def _prune_candidates(doc: dict[str, Any], *, all_stale: bool, root: Path | None,
                      targets: set[str] | None) -> list[dict[str, Any]]:
    candidates = []
    for rid, entry in doc["reviews"].items():
        if targets is not None and rid not in targets:
            continue
        directory = Path(entry["dir"])
        if root is not None and not directory.resolve().is_relative_to(root):
            continue
        state = entry_state(rid, entry)
        if state != "ok" and (all_stale or (state == "missing" and in_temp_area(directory))):
            candidates.append({"reviewId": rid, "state": state, **entry})
    return candidates


def _preview_metadata_prune(
    candidates: list[dict[str, Any]], root: Path | None
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    preview, skipped = [], []
    for row in candidates:
        if row["state"] == "missing":
            preview.append(row)
            continue
        try:
            fd, _ = _open_metadata_dir(Path(row["dir"]), root)
        except _MetadataUnsafe as error:
            skipped.append({**row, "reason": error.reason})
            continue
        try:
            action, reason = _metadata_plan(fd, row["reviewId"])
        finally:
            _close_metadata_dir(fd)
        if action == "skip":
            skipped.append({**row, "reason": reason})
        else:
            preview.append(row)
    return preview, skipped


def prune_stale(*, all_stale: bool = False, under: Path | None = None,
                review_ids: Collection[str] | None = None, delete_metadata: bool = False,
                dry_run: bool = False) -> dict[str, list[dict[str, Any]]]:
    """预览或清理失效条目；元数据删除先隔离并可回滚，身份不明或仍在运行时保护。"""
    candidates: list[dict[str, Any]] = []
    removed: list[dict[str, Any]] = []
    metadata_preserved: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    prepared: list[dict[str, Any]] = []
    locked_directories: set[tuple[int, int]] = set()
    if under is not None:
        _reject_unc_path(under)
    root = under.expanduser().resolve() if under is not None else None
    targets = set(review_ids) if review_ids is not None else None
    if dry_run:
        preview = _prune_candidates(_read(home() / LEDGER_FILE), all_stale=all_stale, root=root, targets=targets)
        if delete_metadata:
            preview, skipped = _preview_metadata_prune(preview, root)
        return {"candidates": preview, "removed": [], "metadataDeleted": [],
                "metadataPreserved": [], "metadataErrors": [], "skipped": skipped}
    with contextlib.ExitStack() as stack:
        with _edit(rollback=lambda: _rollback_quarantines(prepared)) as doc:
            candidates = _prune_candidates(doc, all_stale=all_stale, root=root, targets=targets)
            for row in candidates:
                rid = row["reviewId"]
                entry = doc["reviews"].get(rid)
                if entry is None:
                    skipped.append({**row, "reason": "gone"})
                    continue
                directory = Path(entry["dir"])
                state = entry_state(rid, entry)
                if state == "ok" or (not all_stale and state != "missing"):
                    skipped.append({**row, "reason": "recovered"})
                    continue
                row = {"reviewId": rid, "state": state, **entry}
                if not delete_metadata or state == "missing":
                    removed.append(row)
                    continue
                try:
                    fd, confirmed = _open_metadata_dir(directory, root)
                except _MetadataUnsafe as error:
                    skipped.append({**row, "reason": error.reason})
                    continue
                stack.callback(_close_metadata_dir, fd)
                action, reason = _metadata_plan(fd, rid)
                if action == "delete":
                    identity = _dir_identity(fd)
                    if identity not in locked_directories:
                        try:
                            stack.enter_context(_metadata_lock_fd(fd))
                        except _MetadataUnsafe as error:
                            skipped.append({**row, "reason": error.reason})
                            continue
                        locked_directories.add(identity)
                    action, reason = _metadata_plan(fd, rid)
                if action == "preserve":
                    metadata_preserved.append({**row, "reason": reason})
                    removed.append(row)
                    continue
                if action == "skip":
                    skipped.append({**row, "reason": reason})
                    continue
                try:
                    names = _metadata_names(fd)
                except _MetadataUnsafe as error:
                    skipped.append({**row, "reason": error.reason})
                    continue
                item = {"row": row, "fd": fd, "directory": confirmed,
                        "quarantine": "", "names": names}
                # 先记录所有计划移动的文件，覆盖系统调用已经成功、Python 随即中断的情况。
                prepared.append(item)
                try:
                    _quarantine_metadata(item)
                except _MetadataUnsafe as error:
                    _rollback_quarantines([item])
                    prepared.pop()
                    skipped.append({**row, "reason": error.reason})
                    continue
                except PermissionError:
                    _rollback_quarantines([item])
                    prepared.pop()
                    metadata_preserved.append({**row, "reason": "identity-unconfirmed"})
                    skipped.append({**row, "reason": "metadata-locked"})
                    continue
                action, reason = _metadata_plan(fd, rid)
                if action != "delete":
                    _rollback_quarantines([item])
                    prepared.pop()
                    if action == "preserve":
                        metadata_preserved.append({**row, "reason": reason})
                        removed.append(row)
                    else:
                        skipped.append({**row, "reason": reason})
                    continue
                removed.append(row)
            for row in removed:
                doc["reviews"].pop(row["reviewId"], None)
        metadata_deleted, metadata_errors = _cleanup_quarantines(prepared)
    return {"candidates": candidates, "removed": removed, "metadataDeleted": metadata_deleted,
            "metadataPreserved": metadata_preserved, "metadataErrors": metadata_errors, "skipped": skipped}


def resolve_dir(arg: str | Path) -> Path:
    """命令行里的「审阅目录」参数：是已存在的路径就用它，否则当作审阅标识（或前缀）到台账里查目录。

    参数要按原样传进来（argparse 用 str，别用 Path：Path 会把 ./c71a、c71a/ 规整成 c71a）。
    带路径分隔符、以 . 或 ~ 开头的一律按路径处理，打错目录时不会悄悄换成另一份审阅。
    """
    raw = str(arg)
    _reject_unc_path(raw)
    p = Path(raw).expanduser()
    if p.exists() or os.sep in raw or "/" in raw or raw.startswith((".", "~")) \
            or not RID_PREFIX.fullmatch(raw.strip().lower()):
        return p
    rid = match(str(arg))
    return Path(entries()[rid]["dir"])


def read_manifest(review_dir: Path) -> dict[str, Any]:
    try:
        m = json.loads((Path(review_dir) / "review.json").read_text(encoding="utf-8"))
        return m if isinstance(m, dict) else {}
    except (OSError, ValueError):
        return {}


# 报告格式 → 本版本支持到的元数据版本。review.json 没有 format 时是逐页 HTML 报告（旧审阅都是这样）。
REPORT_FORMATS = {"html": 0, "docx": 1, "pptx": 1}


def report_format(manifest: dict[str, Any]) -> tuple[str, int]:
    """审阅的报告格式与元数据版本：("html", 0) 或 ("docx", 1) 等。"""
    f = manifest.get("format") if isinstance(manifest, dict) else None
    if not isinstance(f, dict):
        return "html", 0
    kind = str(f.get("report") or "html")[:20]
    v = f.get("version")
    return kind, (v if isinstance(v, int) and not isinstance(v, bool) else 0)


def unsupported_format(manifest: dict[str, Any]) -> str | None:
    """本版本处理不了的报告格式（更高版本写下的 DOCX 元数据、未知格式）：返回给用户的说明，能处理返回 None。"""
    kind, version = report_format(manifest)
    if kind in REPORT_FORMATS and version <= REPORT_FORMATS[kind]:
        return None
    return T("ledger.err.format", kind=kind, version=version)


def copied_from(review_dir: Path, manifest: dict[str, Any] | None = None, *,
                origin: Path | None = None) -> Path | None:
    """这个目录是从哪份审阅复制来的：候选原目录还在、不是它自己、且那份里还是同一个审阅标识。

    候选原目录默认取 manifest 的 reviewDir（build_viewer 与 serve 的口径）；台账传 origin，
    用登记在册的目录（搬家后没重建时 reviewDir 还指向旧位置，旧版 manifest 也可能没有它）。
    返回复制来源的目录，不是复制来的是 None。整个目录被移走或原目录已删除（候选不在）
    时不算复制，调用方沿用原标识。reviewDir 缺失、不是字符串或构不成路径（含空字节等）时按
    「没有复制来源信息」处理：manifest 写坏不拦登记、不拦服务启动，与 read_manifest 读不了时
    返回 {} 的容错一致。manifest 传调用方已经读好的 review.json，不传就自己读（读不了或不是
    JSON 对象都按 {} 处理，与 read_manifest 一致）；只用到其中的 reviewId 与 reviewDir。
    审阅标识的格式规则见 RID。
    """
    review_dir = Path(review_dir).resolve()
    m = manifest if manifest is not None else read_manifest(review_dir)
    rid = m.get("reviewId")
    orig = Path(origin) if origin is not None else None
    raw = m.get("reviewDir")
    if orig is None and isinstance(raw, str) and raw:
        try:
            orig = Path(raw)
        except (ValueError, OSError):   # 含空字节等构不成路径的值：按没有来源处理
            orig = None
    if orig is None or orig == review_dir or not orig.is_dir():
        return None
    return orig if read_manifest(orig).get("reviewId") == rid else None


def review_name(manifest: dict[str, Any]) -> str:
    """审阅名：review.json 的 name；旧版生成的没有 name，退回标题。"""
    return str(manifest.get("name") or manifest.get("title") or "").strip()


def entry_state(rid: str, e: dict[str, Any], manifest: dict[str, Any] | None = None) -> str:
    """ok / missing（目录不在）/ mismatch（目录里的审阅标识对不上，可能被重建或复制过）。

    manifest：调用方已读好的 review.json（不重复打开同一份文件时传入）；不传就自己读。
    """
    d = Path(e["dir"])
    if not d.is_dir():
        return "missing"
    if manifest is None:
        manifest = read_manifest(d)
    return "ok" if manifest.get("reviewId") == rid else "mismatch"


def try_register(rid: Any, review_dir: Path) -> str | None:
    """尽力登记；失败只返回一句警告，不抛错。"""
    if not RID.fullmatch(str(rid or "")):
        return None
    try:
        register(str(rid), review_dir)
        return None
    except CopiedDir as e:
        return T("ledger.err.notRegistered", detail=e)
    except (LedgerError, OSError) as e:
        return T("ledger.err.registerFailed", detail=e)
