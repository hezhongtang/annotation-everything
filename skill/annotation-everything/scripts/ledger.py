"""审阅台账：本机所有已知审阅的登记簿（按审阅标识记录审阅目录与专属端口）。

位置：环境变量 AE_HOME 指定的目录，默认 ~/.annotation-everything/（目录 0700，文件 0600）。
只存审阅标识、审阅目录路径、专属端口；审阅名、运行状态等一律现场从审阅目录读，业务信息不离开审阅目录。

台账只是索引，事实以审阅目录为准：
- 生成对照页、启动服务、运行 notes.py 时登记（目录搬了家就按标识更新路径）；
- 专属端口在第一次启动服务时分配，此后归这份审阅独占，服务停了也不让给别的审阅，只有 forget 才释放；
- 目录不在、标识对不上的条目只标出来，不自动删除（外接盘暂时没挂上时不误删、不释放端口）。

台账读写失败不应挡住生成、启动、批注这些主流程：调用方捕获 LedgerError / OSError，只给警告。

语言：LedgerError 的人读文案按界面语言变（`AE_LOCALE`，默认中文）；台账路径、标识、
端口与恢复命令保持原文。调用方（serve.py / notes.py / ae.py）只负责打印。
"""
from __future__ import annotations

import contextlib
import json
import os
import re
import sys
from pathlib import Path
from typing import Any, Callable, Iterator

sys.path.insert(0, str(Path(__file__).resolve().parent))
import fsutil  # noqa: E402
import i18n  # noqa: E402

LEDGER_FILE = "reviews.json"
WAIT_PREFIX = ".annotation-wait."   # 审阅目录里：每个 notes.py wait 进程留一个 .annotation-wait.<pid>.json，退出时删掉
STATUS_FILE = ".annotation-server.json"   # 审阅目录里：服务运行状态（pid/端口/地址），停止时删除；服务与更新器共用这个名字


def wait_file(review_dir: Path, pid: int) -> Path:
    return Path(review_dir) / f"{WAIT_PREFIX}{pid}.json"
LOCK_FILE = ".reviews.lock"
BASE_PORT = 8765
MAX_SCAN = 500          # 分配专属端口时最多往后找多少个
RID = re.compile(r"[0-9a-f]{16}")
RID_PREFIX = re.compile(r"[0-9a-f]{4,16}")


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


def home() -> Path:
    env = os.environ.get("AE_HOME")
    return Path(env).expanduser().resolve() if env else Path.home() / ".annotation-everything"


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


def pid_alive(pid: int) -> bool:
    """这个 pid 的进程还在不在：发信号 0 探活；没权限（别人的进程）时按活着算。"""
    if pid <= 0:
        return False
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
def _edit() -> Iterator[dict[str, Any]]:
    _refresh_locale()
    """在台账文件锁下读改写：多个 serve / build 同时分配端口也不会撞号。"""
    h = _ensure_home()
    with fsutil.flocked(h / LOCK_FILE, exclusive=True):
        doc = _read(h / LEDGER_FILE)
        before = json.dumps(doc, sort_keys=True)
        yield doc
        if json.dumps(doc, sort_keys=True) != before:
            _write(h / LEDGER_FILE, doc)


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
    """
    d = str(Path(review_dir).resolve())
    with _edit() as doc:
        reviews = doc["reviews"]
        e = _set_dir(reviews, rid, d)
        if e.get("port"):
            return int(e["port"])
        taken = {x["port"] for k, x in reviews.items() if k != rid and x.get("port")}
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
    """把专属端口改成 port。端口归别的审阅时拒绝（先 forget 那一份）。"""
    if not 1024 <= port <= 65535:
        raise LedgerError(T("ledger.err.portRange", port=port))
    d = str(Path(review_dir).resolve())
    with _edit() as doc:
        for k, x in doc["reviews"].items():
            if k != rid and x.get("port") == port:
                raise LedgerError(T("ledger.err.portTaken", port=port, rid=k, short=k[:8]))
        _set_dir(doc["reviews"], rid, d)["port"] = port


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


def resolve_dir(arg: str | Path) -> Path:
    """命令行里的「审阅目录」参数：是已存在的路径就用它，否则当作审阅标识（或前缀）到台账里查目录。

    参数要按原样传进来（argparse 用 str，别用 Path：Path 会把 ./c71a、c71a/ 规整成 c71a）。
    带路径分隔符、以 . 或 ~ 开头的一律按路径处理，打错目录时不会悄悄换成另一份审阅。
    """
    raw = str(arg)
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
REPORT_FORMATS = {"html": 0, "docx": 1}


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
