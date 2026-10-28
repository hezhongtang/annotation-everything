#!/usr/bin/env python3
"""annotation-everything 本地批注服务：只监听本机，托管对照页，批注每次增删改即落盘。

用法：
  python3 serve.py <审阅目录>                 # 启动（默认端口 8765），自动打开浏览器，Ctrl+C 停止
  python3 serve.py <审阅目录> --port 9000 --no-open --debug
  python3 serve.py <审阅目录> --on-handoff "命令 参数"   # 可选：用户点「交给 Agent」时运行这条命令
  python3 serve.py status <审阅目录>          # 查看服务是否在跑、实际端口、拒绝/错误计数（给 Claude 排障用）
  python3 serve.py check                      # 自检：回环绑定、伪造 Host/Origin 被拒、落盘与重启恢复

安全做法：
- 只绑 127.0.0.1；不发任何 CORS 头；不联网、无遥测；
- 每个请求校验 Host 必须是 127.0.0.1:端口 或 localhost:端口（防 DNS 重绑定）；
- 写操作要求 Origin 与本服务同源（缺 Origin 也拒绝）且 Content-Type 为 application/json，
  拒绝 Sec-Fetch-Site 为 cross-site / same-site 的请求；
- 页面 CSP 只放行页面自身那段脚本（按 sha256），注入的脚本和内联事件不会执行；
- 只提供 index.html 与 /api/*，其他路径一律 404；
- 日志里来自请求的字段（路径、Host、Origin）只保留安全字符并截断；`status` 只汇总计数，不回显请求内容。

「交给 Agent」：页面 POST /api/handoff 记一次提交（handoff.json）。代理用 notes.py wait 等待或轮询该文件；
可选的 --on-handoff 钩子在每次新提交（AE_EVENT=handoff）和请求复核（AE_EVENT=review）时运行一条命令（不经 shell），
提交内容以 JSON 从标准输入传入（含 stage / target / allowSubagents），另有环境变量 AE_EVENT / AE_REVIEW_DIR /
AE_HANDOFF_SEQ / AE_URL；运行中又来的事件排队依次补跑；命令输出丢弃，退出码记入 server.log。

端口回退：默认端口被占用时，若占用者正是本服务且托管同一目录，直接复用并打开浏览器；
否则顺延尝试后续端口（最多 20 个），实际端口写进审阅目录的 .annotation-server.json。
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import http.client
import json
import os
import re
import shlex
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
import webbrowser
from collections import Counter
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
import notes_store as store  # noqa: E402

APP = "annotation-everything"
VERSION = "0.6.0"
# 服务支持的功能；页面据此判断背后的服务是不是旧版（旧服务会静默丢掉框选、重新标注等新字段）
FEATURES = ["notes", "handoff", "marks", "batches", "stages", "always"]


class StaleServer(Exception):
    """本目录已有旧版本的服务在跑：复用它会让新页面的改动被静默丢掉。"""
DEFAULT_PORT = 8765
PORT_TRIES = 20
STATUS_FILE = ".annotation-server.json"
LOG_FILE = "server.log"
MANIFEST = "review.json"
MAX_BODY = 2 * 1024 * 1024
HOST = "127.0.0.1"
NOTE_PATH = re.compile(r"^/api/notes/([A-Za-z0-9_-]{1,64})$")
INLINE_SCRIPT = re.compile(r"<script>(.*?)</script>", re.S)
SAFE_CHARS = re.compile(r"[^A-Za-z0-9._:/?=&%+@-]")


def csp_for(html_text: str) -> str:
    """只放行页面里的内联脚本（按内容 sha256），其余脚本、内联事件一律不执行。"""
    hashes = " ".join("'sha256-" + base64.b64encode(hashlib.sha256(s.encode("utf-8")).digest()).decode() + "'"
                      for s in INLINE_SCRIPT.findall(html_text))
    return (f"default-src 'none'; script-src {hashes or chr(39) + 'none' + chr(39)}; style-src 'unsafe-inline'; "
            "img-src data: blob:; font-src data:; media-src data: blob:; connect-src 'self'; "
            "frame-ancestors 'none'; base-uri 'none'; form-action 'none'; object-src 'none'")


def clean(value: Any, limit: int = 60) -> str:
    """把来自请求的字段变成安全的日志片段：只留常见 URL 字符，其余换成 ?，并截断。"""
    if value is None:
        return "-"
    s = SAFE_CHARS.sub("?", str(value))
    return s if len(s) <= limit else s[:limit] + "…"


class Reject(Exception):
    """拒绝请求。code 是固定的原因代码，msg 是给页面看的说明（不含请求内容）。"""

    def __init__(self, status: int, code: str, msg: str):
        super().__init__(msg)
        self.status, self.code, self.msg = status, code, msg


class ReviewServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, review_dir: Path, port: int, debug: bool = False, log_to_file: bool = True,
                 on_handoff: list[str] | None = None, hook_cwd: str | None = None):
        self.review_dir = review_dir
        self.debug = debug
        self.on_handoff = on_handoff
        self.hook_cwd = hook_cwd or os.getcwd()
        self._hook_lock = threading.Lock()
        self._hook_running = False
        self._hook_queue: list[dict[str, Any]] = []   # 钩子运行中又来的事件：按到达顺序排队，同一批同一事件只留最新
        self.log_to_file = log_to_file
        self._log_lock = threading.Lock()
        self._cache: dict[str, tuple[float, Any]] = {}
        super().__init__((HOST, port), Handler)
        self.port = self.server_address[1]
        self.allowed_hosts = {f"127.0.0.1:{self.port}", f"localhost:{self.port}"}
        self.allowed_origins = {f"http://{h}" for h in self.allowed_hosts}

    def log(self, level: str, msg: str) -> None:
        line = f"{time.strftime('%Y-%m-%d %H:%M:%S')} [{level}] {msg}"
        with self._log_lock:
            print(line, file=sys.stderr, flush=True)
            if self.log_to_file:
                try:
                    store.append_private(self.review_dir / LOG_FILE, line)
                except OSError:
                    pass

    def _cached(self, name: str, build: Any) -> Any:
        f = self.review_dir / name
        try:
            mtime = f.stat().st_mtime
        except OSError:
            return build(None)
        hit = self._cache.get(name)
        if hit and hit[0] == mtime:
            return hit[1]
        val = build(f)
        self._cache[name] = (mtime, val)
        return val

    def run_hook(self, item: dict[str, Any], merged: bool, event: str = "handoff") -> None:
        """「交给 Agent」钩子：后台运行用户配置的命令，提交内容经标准输入传 JSON，不经 shell。

        只在新的提交时触发（合并进还没被接收的提交不再触发）；同一时间只跑一个，运行中又来新提交就等它结束后补跑一次。
        """
        if not self.on_handoff or merged:
            return
        url = f"http://127.0.0.1:{self.port}/"
        payload = {"event": event, "app": APP, "reviewId": self.review_id(), "reviewDir": str(self.review_dir),
                   "url": url, "seq": item.get("seq"), "message": item.get("message", ""),
                   "stage": "review" if event == "review" else "draft", "target": item.get("target", "final"),
                   "allowSubagents": bool(item.get("allowSubagents")), "reviewMessage": item.get("reviewMessage", ""),
                   "openCount": item.get("openCount"), "pages": item.get("pages"),
                   "notesFile": str(self.review_dir / store.NOTES_FILE),
                   "handoffFile": str(self.review_dir / store.HANDOFF_FILE),
                   "notesCli": str(Path(__file__).resolve().with_name("notes.py"))}
        env = {**os.environ, "AE_EVENT": event, "AE_REVIEW_DIR": str(self.review_dir),
               "AE_HANDOFF_SEQ": str(item.get("seq")), "AE_URL": url}
        job = {"payload": payload, "env": env, "seq": item.get("seq"), "event": event}
        with self._hook_lock:
            if self._hook_running:
                self._hook_queue = [j for j in self._hook_queue if (j["event"], j["seq"]) != (job["event"], job["seq"])] + [job]
                return
            self._hook_running = True
        threading.Thread(target=self._hook_worker, args=(job,), daemon=True).start()

    def _hook_worker(self, job: dict[str, Any]) -> None:
        while job is not None:
            try:
                proc = subprocess.Popen(list(self.on_handoff), stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
                                        stderr=subprocess.DEVNULL, cwd=self.hook_cwd, env=job["env"], start_new_session=True)
                proc.communicate(json.dumps(job["payload"], ensure_ascii=False).encode("utf-8"))
                if proc.returncode == 0:
                    self.log("info", f"hook seq={job['seq']} exit=0")
                else:
                    self.log("error", f"code=hook-failed seq={job['seq']} exit={proc.returncode}")
            except OSError as e:
                self.log("error", f"code=hook-failed seq={job['seq']} {type(e).__name__}: {e}")
            with self._hook_lock:
                job = self._hook_queue.pop(0) if self._hook_queue else None
                if job is None:
                    self._hook_running = False

    def review_id(self) -> str | None:
        def read(f: Path | None) -> str | None:
            try:
                return json.loads(f.read_text(encoding="utf-8")).get("reviewId") if f else None
            except (OSError, ValueError, AttributeError):
                return None
        return self._cached(MANIFEST, read)

    def index(self) -> tuple[bytes, str] | None:
        def read(f: Path | None) -> tuple[bytes, str] | None:
            if f is None:
                return None
            raw = f.read_bytes()
            return raw, csp_for(raw.decode("utf-8", errors="replace"))
        return self._cached("index.html", read)


class Handler(BaseHTTPRequestHandler):
    server: ReviewServer
    server_version = f"{APP}/{VERSION}"
    sys_version = ""
    protocol_version = "HTTP/1.1"
    timeout = 15  # 慢速或不发完请求体的连接 15 秒后断开

    # ---------- 通用 ----------
    def log_message(self, fmt: str, *args: Any) -> None:  # 默认访问日志
        if self.server.debug:
            self.server.log("debug", f"{self.command} {clean(self.path)} {clean(args[1] if len(args) > 1 else '')}")

    def _send(self, code: int, body: bytes, ctype: str, extra: dict[str, str] | None = None) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Cross-Origin-Resource-Policy", "same-origin")
        self.send_header("X-Frame-Options", "DENY")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, code: int, obj: Any) -> None:
        self._send(code, json.dumps(obj, ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8")

    def _reject(self, e: Reject) -> None:
        self.server.log("reject", f"code={e.code} status={e.status} method={clean(self.command, 12)} "
                                  f"path={clean(self.path)} host={clean(self.headers.get('Host'))} "
                                  f"origin={clean(self.headers.get('Origin'))} site={clean(self.headers.get('Sec-Fetch-Site'), 20)}")
        self.close_connection = True
        self._json(e.status, {"error": e.msg, "code": e.code, "app": APP})

    def _check_host(self) -> None:
        host = (self.headers.get("Host") or "").strip().lower()
        if host not in self.server.allowed_hosts:
            raise Reject(421, "bad-host", "Host 不是本服务（请用启动时打印的 127.0.0.1 地址打开）")

    def _check_api_origin(self, mutating: bool) -> None:
        site = self.headers.get("Sec-Fetch-Site")
        if site and site not in ("same-origin", "none"):
            raise Reject(403, "cross-site", "来自其他网站或端口的请求")
        origin = self.headers.get("Origin")
        if origin is not None and origin.lower() not in self.server.allowed_origins:
            raise Reject(403, "bad-origin", "Origin 不是本服务")
        if mutating and origin is None:
            raise Reject(403, "no-origin", "写操作缺少 Origin")

    def _body(self) -> Any:
        ctype = (self.headers.get("Content-Type") or "").split(";")[0].strip().lower()
        if ctype != "application/json":
            raise Reject(415, "bad-content-type", "Content-Type 须为 application/json")
        if self.headers.get("Transfer-Encoding"):
            raise Reject(411, "chunked", "不支持分块传输，请带 Content-Length")
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError as e:
            raise Reject(400, "bad-length", "Content-Length 不合法") from e
        if length < 0:
            raise Reject(400, "bad-length", "Content-Length 不合法")
        if length > MAX_BODY:
            raise Reject(413, "too-large", "请求体过大")
        raw = self.rfile.read(length) if length else b""
        try:
            return json.loads(raw.decode("utf-8") or "null")
        except (UnicodeDecodeError, json.JSONDecodeError) as e:
            raise Reject(400, "bad-json", "请求体不是合法 JSON") from e

    def _dispatch(self) -> None:
        try:
            self._check_host()
            path = self.path.split("?", 1)[0]
            if path.startswith("/api/"):
                self._check_api_origin(mutating=self.command not in ("GET", "HEAD"))
                self._api(path)
            elif self.command in ("GET", "HEAD") and path in ("/", "/index.html"):
                self._index()
            elif self.command in ("GET", "HEAD") and path == "/favicon.ico":
                self._send(204, b"", "image/x-icon")
            else:
                raise Reject(404, "not-found", "没有这个路径")
        except Reject as e:
            self._reject(e)
        except (store.NoteError, UnicodeEncodeError) as e:  # 由请求内容引起的错误一律 4xx，页面不会反复重试
            self._json(400, {"error": str(e) if isinstance(e, store.NoteError) else "内容含无法保存的字符",
                             "code": "bad-note", "app": APP})
        except store.NotFound:
            self._json(404, {"error": "批注不存在（可能已被删除）", "code": "no-note", "app": APP})
        except Exception as e:  # 服务端错误也写日志，方便排障
            self.server.log("error", f"code=server-error method={clean(self.command, 12)} path={clean(self.path)} "
                                     f"{type(e).__name__}: {e}")
            self._json(500, {"error": f"服务端写入失败（{type(e).__name__}），批注暂存浏览器，稍后自动重试",
                             "code": "server-error", "app": APP})

    do_GET = do_HEAD = do_POST = do_PATCH = do_DELETE = do_PUT = _dispatch

    def do_OPTIONS(self) -> None:  # 不支持预检：跨源写请求在浏览器侧即失败
        try:
            self._check_host()
            raise Reject(405, "preflight", "不支持跨源预检")
        except Reject as e:
            self._reject(e)

    # ---------- 页面 ----------
    def _index(self) -> None:
        got = self.server.index()
        if got is None:
            raise Reject(404, "no-index", "审阅目录里没有 index.html，先运行 build_viewer.py")
        body, csp = got
        self._send(200, body, "text/html; charset=utf-8", {"Content-Security-Policy": csp})

    # ---------- API ----------
    def _ident(self) -> dict[str, Any]:
        return {"app": APP, "reviewId": self.server.review_id(), "version": VERSION, "features": FEATURES}

    def _api(self, path: str) -> None:
        d, m = self.server.review_dir, NOTE_PATH.match(path)
        cmd = "GET" if self.command == "HEAD" else self.command
        # 页面带上自己的审阅标识：端口上换成了另一份审阅的服务时，先拒绝，绝不写进别人的 notes.json
        claimed = self.headers.get("X-AE-Review")
        if claimed is not None and path != "/api/health":
            rid = self.server.review_id()
            if not rid or claimed != rid:
                raise Reject(409, "review-mismatch", "页面属于另一份审阅（审阅标识不一致）")
        if path == "/api/health" and cmd == "GET":
            self._json(200, {**self._ident(), "dir": str(d), "pid": os.getpid(),
                             "port": self.server.port, "hook": bool(self.server.on_handoff)})
        elif path == "/api/notes" and cmd == "GET":
            doc = store.load(d)
            idx = d / "index.html"
            doc["indexMtime"] = idx.stat().st_mtime if idx.exists() else None  # 页面据此提示「已重建，请刷新」
            doc["handoff"] = store.handoff_state(d, full=False)   # 轮询只带最新一批；处理记录全量走 GET /api/handoff
            doc["skillCatalog"] = store.skills_catalog(d)
            doc["alwaysSkills"] = store.always_skills(d)
            self._json(200, {**doc, **self._ident()})
        elif path == "/api/handoff" and cmd == "GET":
            self._json(200, {**store.handoff_state(d), **self._ident()})
        elif path == "/api/handoff" and cmd == "POST":
            body = self._expect_obj(self._body())
            _h, item, merged = store.handoff_submit(d, str(body.get("message") or ""), by="page",
                                                    target=str(body.get("target") or "final"),
                                                    allow_subagents=body.get("allowSubagents") is True)
            self.server.log("info", f"handoff seq={item['seq']} open={item['openCount']} merged={merged} target={item['target']}")
            self.server.run_hook(item, merged)
            self._json(200, {"doc": {**store.load(d), **self._ident()}, "handoff": store.handoff_state(d, full=False),
                             "merged": merged, "seq": item["seq"]})
        elif path == "/api/skills/always" and cmd == "POST":
            body = self._expect_obj(self._body())
            if "always" not in body:
                raise Reject(400, "bad-request", "缺少 always（要清空请传空列表）")
            items = store.always_save(d, body.get("always"))
            self.server.log("info", f"always-skills count={len(items)}")
            self._json(200, {"alwaysSkills": items, **self._ident()})
        elif path in ("/api/handoff/review", "/api/handoff/finalize") and cmd == "POST":
            body = self._expect_obj(self._body())
            seq = body.get("seq")
            if not isinstance(seq, int) or isinstance(seq, bool):
                raise Reject(400, "bad-request", "缺少批次序号 seq")
            if path.endswith("review"):
                item = store.handoff_request_review(d, seq, str(body.get("message") or ""),
                                                    allow_subagents=body.get("allowSubagents") is True, by="page")
                self.server.log("info", f"handoff-review seq={seq}")
                self.server.run_hook(item, False, event="review")
            else:
                item = store.handoff_finalize(d, seq, by="page")
                self.server.log("info", f"handoff-finalize seq={seq}")
            self._json(200, {"doc": {**store.load(d), **self._ident()}, "handoff": store.handoff_state(d, full=False),
                             "seq": seq, "status": item["status"]})
        elif path == "/api/notes" and cmd == "POST":
            doc, note = store.add(d, self._expect_obj(self._body()), by="page")
            self.server.log("info", f"add {note['id']} page={note['page']}")
            self._json(201, {"doc": {**doc, **self._ident()}, "note": note})
        elif path == "/api/notes/import" and cmd == "POST":
            body = self._body()
            items = body.get("notes") if isinstance(body, dict) else body
            doc, added, skipped, updated = store.import_notes(d, items, by="page-import")
            self.server.log("info", f"import added={added} updated={updated} skipped={skipped}")
            self._json(200, {"doc": {**doc, **self._ident()}, "added": added, "skipped": skipped, "updated": updated})
        elif m and cmd == "PATCH":
            body = self._expect_obj(self._body())
            if "status" in body:
                doc, note = store.set_status(d, m.group(1), str(body["status"]),
                                             resolution=str(body.get("resolution") or ""), by="page")
            else:
                doc, note = store.update(d, m.group(1), body, by="page")
            self.server.log("info", f"update {m.group(1)}")
            self._json(200, {"doc": {**doc, **self._ident()}, "note": note})
        elif m and cmd == "DELETE":
            doc = store.delete(d, m.group(1), by="page")
            self.server.log("info", f"delete {m.group(1)}")
            self._json(200, {"doc": {**doc, **self._ident()}})
        else:
            raise Reject(404 if cmd == "GET" else 405, "no-api", "没有这个接口")

    @staticmethod
    def _expect_obj(body: Any) -> dict[str, Any]:
        if not isinstance(body, dict):
            raise store.NoteError("请求体须为 JSON 对象")
        return body


# ---------- 端口与状态文件 ----------
def probe(port: int, timeout: float = 1.0) -> dict[str, Any] | None:
    """看端口上是否有程序；有则尝试读 /api/health。返回 None 表示端口空闲。"""
    try:
        with socket.create_connection((HOST, port), timeout=timeout):
            pass
    except OSError:
        return None
    try:
        c = http.client.HTTPConnection(HOST, port, timeout=timeout)
        c.request("GET", "/api/health", headers={"Host": f"127.0.0.1:{port}"})
        r = c.getresponse()
        data = json.loads(r.read() or b"{}") if r.status == 200 else {}
        c.close()
        return data if isinstance(data, dict) and data.get("app") == APP else {"app": "other"}
    except (OSError, ValueError, http.client.HTTPException):
        return {"app": "other"}


def pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def read_status(review_dir: Path) -> dict[str, Any] | None:
    try:
        info = json.loads((review_dir / STATUS_FILE).read_text(encoding="utf-8"))
        return info if isinstance(info, dict) else None
    except (OSError, ValueError):
        return None


def write_status(server: ReviewServer) -> None:
    info = {"app": APP, "version": VERSION, "pid": os.getpid(), "port": server.port,
            "url": f"http://127.0.0.1:{server.port}/", "dir": str(server.review_dir),
            "startedAt": store.now_iso()}
    f = server.review_dir / STATUS_FILE
    fd = os.open(f, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(json.dumps(info, ensure_ascii=False, indent=2) + "\n")


def clear_status(review_dir: Path) -> None:
    info = read_status(review_dir)
    if info and info.get("pid") == os.getpid():
        (review_dir / STATUS_FILE).unlink(missing_ok=True)


def same_dir(found: dict[str, Any], review_dir: Path) -> bool:
    try:
        return found.get("app") == APP and Path(found.get("dir", "")).resolve() == review_dir.resolve()
    except (OSError, TypeError):
        return False


def bind(review_dir: Path, port: int, debug: bool, tries: int = PORT_TRIES,
         on_handoff: list[str] | None = None) -> tuple[ReviewServer | None, str | None, list[str]]:
    """返回 (服务, 已在运行的 URL, 过程记录)。本目录已有旧版本服务在跑时抛 StaleServer（不静默复用）。"""
    notes: list[str] = []

    def check_version(found: dict[str, Any], p: int) -> None:
        if found.get("version") != VERSION:
            raise StaleServer(f"本目录已有 {found.get('version') or '旧'} 版的服务在端口 {p} 运行（pid {found.get('pid')}），"
                              f"当前是 {VERSION}。旧服务不认识新版页面的字段（框选、重新标注等会被静默丢掉），"
                              f"请先停掉它（在它的终端按 Ctrl+C，或 kill {found.get('pid')}），再重新运行本命令。")

    info = read_status(review_dir)
    if info and pid_alive(int(info.get("pid") or 0)) and info.get("port"):
        found = probe(int(info["port"]))
        if found and same_dir(found, review_dir):
            check_version(found, int(info["port"]))
            notes.append(f"本目录的服务已在端口 {info['port']} 运行（pid {info.get('pid')}），直接复用")
            return None, f"http://127.0.0.1:{info['port']}/", notes
    for p in range(port, port + tries):
        found = probe(p)
        if found is not None:
            if same_dir(found, review_dir):
                check_version(found, p)
                notes.append(f"端口 {p}：本服务已在托管此目录（pid {found.get('pid')}），直接复用")
                return None, f"http://127.0.0.1:{p}/", notes
            who = "另一个审阅目录的本服务" if found.get("app") == APP else "其他程序"
            notes.append(f"端口 {p} 被{who}占用，顺延")
            continue
        try:
            return ReviewServer(review_dir, p, debug=debug, on_handoff=on_handoff), None, notes
        except OSError as e:
            notes.append(f"端口 {p} 绑定失败（{e.strerror or e}），顺延")
    return None, None, notes


def parse_hook(cmd: str | None) -> list[str] | None:
    """--on-handoff 的命令按 shell 规则拆分但不经 shell 执行；找不到可执行文件就报错。"""
    if not cmd:
        return None
    try:
        argv = shlex.split(cmd)
    except ValueError as e:
        raise SystemExit(f"错误：--on-handoff 的命令写法不对（{e}），检查引号是否配对") from None
    if not argv:
        return None
    exe = argv[0]
    full = os.path.abspath(exe) if os.sep in exe else shutil.which(exe)
    if not full or not os.access(full, os.X_OK):
        raise SystemExit(f"错误：--on-handoff 的命令找不到或不可执行：{exe}")
    return [full, *argv[1:]]   # 钩子在启动 serve.py 的目录下运行，参数里的相对路径按这个目录解析


def serve(review_dir: Path, port: int, open_browser: bool, debug: bool, on_handoff: list[str] | None = None) -> int:
    review_dir = review_dir.resolve()
    if not (review_dir / "index.html").is_file():
        print(f"错误：{review_dir} 下没有 index.html。先用 build_viewer.py 生成对照页。", file=sys.stderr)
        return 2
    try:
        store.load(review_dir)
    except ValueError as e:
        print(f"错误：notes.json 无法读取（{e}）。请先修复或改名备份后再启动，服务不会覆盖它。", file=sys.stderr)
        return 2
    try:
        m = json.loads((review_dir / MANIFEST).read_text(encoding="utf-8"))
        orig = Path(m["reviewDir"]) if m.get("reviewDir") else None
        if orig and orig != review_dir and (orig / MANIFEST).exists():
            om = json.loads((orig / MANIFEST).read_text(encoding="utf-8"))
            if om.get("reviewId") == m.get("reviewId"):
                print(f"提示：这个审阅目录是从 {orig} 复制来的，与原目录共用审阅标识。"
                      f"建议先运行 build_viewer.py --rebuild {review_dir}，让它换一个新标识。", file=sys.stderr)
    except (OSError, ValueError, AttributeError):
        pass
    try:
        server, running_url, trail = bind(review_dir, port, debug, on_handoff=on_handoff)
    except StaleServer as e:
        print(f"错误：{e}", file=sys.stderr)
        return 6
    for t in trail:
        print(f"[端口] {t}", file=sys.stderr)
    if running_url:
        print(f"已在运行：{running_url}" + ("（注意：沿用已在运行的服务，本次的 --on-handoff 不生效；要换钩子先停掉它）"
                                          if on_handoff else ""))
        if open_browser:
            webbrowser.open(running_url)
        return 0
    if server is None:
        print(f"错误：端口 {port}–{port + PORT_TRIES - 1} 都不可用。可用 --port 指定其他端口，"
              f"或运行 `lsof -nP -iTCP:{port} -sTCP:LISTEN` 查看占用者。", file=sys.stderr)
        return 3
    url = f"http://127.0.0.1:{server.port}/"
    if server.port != port:
        print(f"[端口] 首选端口 {port} 不可用，改用 {server.port}。", file=sys.stderr)
    write_status(server)
    server.log("info", f"启动 {APP} {VERSION}：{url} 托管 {review_dir}（pid {os.getpid()}）")
    print(f"对照页：{url}\n批注落盘：{review_dir / store.NOTES_FILE}"
          + (f"\n「交给 Agent」时运行：{shlex.join(on_handoff)}" if on_handoff else "")
          + "\n按 Ctrl+C 停止。", flush=True)

    def _term(*_a: Any) -> None:
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, _term)
    if open_browser:
        threading.Timer(0.3, webbrowser.open, args=(url,)).start()
    try:
        server.serve_forever(poll_interval=0.3)
    except KeyboardInterrupt:
        pass
    finally:
        server.log("info", "停止")
        server.server_close()
        clear_status(review_dir)
    return 0


def status(review_dir: Path) -> int:
    """汇总运行状态。只输出计数与本服务写的字段，不回显请求内容（日志里的路径等来自外部，不可信）。"""
    review_dir = review_dir.resolve()
    out: dict[str, Any] = {"dir": str(review_dir), "index_html": (review_dir / "index.html").is_file()}
    try:
        out["reviewId"] = json.loads((review_dir / MANIFEST).read_text(encoding="utf-8")).get("reviewId")
    except (OSError, ValueError):
        out["reviewId"] = None
    try:
        doc = store.load(review_dir)
        notes = doc["notes"]
        out["notes"] = {"rev": doc.get("rev"), "total": len(notes),
                        "open": sum(n.get("status", "open") == "open" for n in notes),
                        "drafted": sum(n.get("status") == "drafted" for n in notes),
                        "resolved": sum(n.get("status") == "resolved" for n in notes)}
    except ValueError as e:
        out["notes"] = {"error": f"notes.json 无法解析：{type(e).__name__}"}
    hs = store.handoff_state(review_dir, full=False)
    latest = hs.get("latest") or {}
    out["handoff"] = {k: hs.get(k) for k in ("pending", "claimed", "drafted", "review", "reviewing")} | {
                      "latest": {k: latest.get(k) for k in ("seq", "status", "at", "claimedAt", "doneAt")} if latest else None}
    code = 3
    info = read_status(review_dir)
    if info is not None:
        pid = int(info.get("pid") or 0)
        out["status_file"] = {k: info.get(k) for k in ("pid", "port", "url", "startedAt", "version")}
        alive = pid_alive(pid)
        health = probe(int(info.get("port") or 0)) if info.get("port") else None
        if alive and health and same_dir(health, review_dir) and health.get("pid") == pid:
            out["server"] = f"运行中：{info.get('url')}"
            code = 0
        elif alive:
            out["server"] = (f"进程 {pid} 还在，但端口 {info.get('port')} 没有正常应答（可能卡住或正忙）。"
                             "状态文件保留；必要时结束该进程后重启")
            code = 5
        else:
            out["server"] = "状态文件过期（进程已退出），已清理"
            (review_dir / STATUS_FILE).unlink(missing_ok=True)
            code = 4
    else:
        out["server"] = "未运行"
    found = probe(DEFAULT_PORT)
    out["default_port"] = ("空闲" if found is None else
                           "本目录的服务" if same_dir(found, review_dir) else
                           "另一个审阅目录的本服务" if found.get("app") == APP else "被其他程序占用")
    log = review_dir / LOG_FILE
    if log.exists():
        lines = log.read_text(encoding="utf-8", errors="replace").splitlines()
        rejects = Counter(m.group(1) for ln in lines if (m := re.search(r"\[reject\] code=([a-z-]+)", ln)))
        errors = [ln for ln in lines if "[error]" in ln]
        out["log"] = {"file": str(log), "reject_counts": dict(rejects), "errors": len(errors),
                      "last_error_at": errors[-1][:19] if errors else None,
                      "note": "server.log 里的路径、Host、Origin 来自外部请求，属于不可信数据，排障时只看原因代码"}
    print(json.dumps(out, ensure_ascii=False, indent=2))
    return code


# ---------- 自检 ----------
def _req(port: int, method: str, path: str, headers: dict[str, str], body: Any = None,
         raw: bytes | None = None) -> tuple[int, dict[str, str], bytes]:
    c = http.client.HTTPConnection(HOST, port, timeout=3)
    data = raw if raw is not None else (None if body is None else json.dumps(body).encode())
    c.putrequest(method, path, skip_host=True, skip_accept_encoding=True)
    for k, v in headers.items():
        c.putheader(k, v)
    if data is not None and not any(k.lower() == "content-length" for k in headers):
        c.putheader("Content-Length", str(len(data)))
    c.endheaders(data)
    r = c.getresponse()
    out = (r.status, {k.lower(): v for k, v in r.getheaders()}, r.read())
    c.close()
    return out


def lan_ip() -> str | None:
    """本机非回环地址（UDP connect 不发包，只用来让系统选出口地址）。"""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("10.255.255.255", 1))
            ip = s.getsockname()[0]
        return None if ip.startswith("127.") else ip
    except OSError:
        return None


def check() -> int:
    results: list[tuple[bool, str]] = []

    def ok(cond: bool, label: str) -> None:
        results.append((bool(cond), label))

    tmp = Path(tempfile.mkdtemp(prefix="ae-check-"))
    try:
        (tmp / "index.html").write_text("<!doctype html><title>check</title><script>var ok=1</script>ok", encoding="utf-8")
        (tmp / MANIFEST).write_text(json.dumps({"reviewId": "checkreview"}), encoding="utf-8")
        srv = ReviewServer(tmp, 0, log_to_file=True)
        port = srv.port
        t = threading.Thread(target=srv.serve_forever, kwargs={"poll_interval": 0.1}, daemon=True)
        t.start()
        good = {"Host": f"127.0.0.1:{port}"}
        same = {**good, "Origin": f"http://127.0.0.1:{port}", "Content-Type": "application/json"}

        ok(srv.server_address[0] == "127.0.0.1", f"只绑定回环地址（实际 {srv.server_address[0]}）")
        ip = lan_ip()
        if ip:
            # 只看能否拿到本服务的应答：代理/TUN 网卡（如 198.18.x.x）可能接受任意 TCP 连接，单测连通会误报
            reached = False
            try:
                c = http.client.HTTPConnection(ip, port, timeout=1.5)
                c.request("GET", "/api/health", headers={"Host": f"127.0.0.1:{port}"})
                r = c.getresponse()
                body = r.read()
                reached = r.status == 200 and APP.encode() in body and str(os.getpid()).encode() in body
                c.close()
            except (OSError, http.client.HTTPException):
                pass
            ok(not reached, f"从非回环地址 {ip} 访问不到本服务")
        else:
            results.append((True, "（无非回环网卡，跳过局域网连接检查）"))
        s, h, b = _req(port, "GET", "/", good)
        ok(s == 200 and b"ok" in b, "正常 Host 能打开对照页")
        csp = h.get("content-security-policy", "")
        ok("'sha256-" in csp and "unsafe-inline" not in csp.split("script-src", 1)[1].split(";", 1)[0],
           "对照页 CSP 只按哈希放行页面脚本")
        s, _, _ = _req(port, "GET", "/", {"Host": f"evil.example:{port}"})
        ok(s == 421, f"伪造 Host（DNS 重绑定）被拒（{s}）")
        s, _, _ = _req(port, "GET", "/api/notes", {"Host": f"evil.example:{port}"})
        ok(s == 421, f"伪造 Host 读批注被拒（{s}）")
        s, _, _ = _req(port, "POST", "/api/notes", {**same, "Origin": "http://evil.example"}, {"page": 1, "text": "x"})
        ok(s == 403, f"伪造 Origin 写批注被拒（{s}）")
        s, _, _ = _req(port, "POST", "/api/notes", {**same, "Origin": "null"}, {"page": 1, "text": "x"})
        ok(s == 403, f"Origin=null（file:// 或沙箱页）写批注被拒（{s}）")
        hdr = {k: v for k, v in same.items() if k != "Origin"}
        s, _, _ = _req(port, "POST", "/api/notes", hdr, {"page": 1, "text": "x"})
        ok(s == 403, f"缺 Origin 的写请求被拒（{s}）")
        s, _, _ = _req(port, "POST", "/api/notes", {**same, "Content-Type": "text/plain"}, {"page": 1, "text": "x"})
        ok(s == 415, f"非 JSON Content-Type（可绕过预检的简单请求）被拒（{s}）")
        s, _, _ = _req(port, "POST", "/api/notes", {**same, "Sec-Fetch-Site": "cross-site"}, {"page": 1, "text": "x"})
        ok(s == 403, f"Sec-Fetch-Site=cross-site 被拒（{s}）")
        s, _, _ = _req(port, "POST", "/api/notes", {**same, "Content-Length": "-5"}, raw=b"")
        ok(s == 400, f"负数 Content-Length 被拒（{s}）")
        s, h, _ = _req(port, "OPTIONS", "/api/notes", {**good, "Origin": "http://evil.example",
                                                         "Access-Control-Request-Method": "POST"})
        ok(s == 405 and "access-control-allow-origin" not in h, f"预检被拒且无 CORS 头（{s}）")
        s, _, _ = _req(port, "GET", "/../notes.json", good)
        ok(s == 404, f"路径穿越 / 其他文件 404（{s}）")
        s, _, _ = _req(port, "GET", "/server.log", good)
        ok(s == 404, f"不暴露审阅目录其他文件（{s}）")

        evil_id = 'z"><img src=x onerror=alert(1)>'
        s, h, b = _req(port, "POST", "/api/notes", same, {"id": evil_id, "page": 2, "text": "自检批注",
                                                           "pin": {"x": 10, "y": 20}})
        ok(s == 201 and "access-control-allow-origin" not in h, f"同源写批注成功且无 CORS 头（{s}）")
        nid = json.loads(b)["note"]["id"] if s == 201 else ""
        ok(nid and nid != evil_id and store.valid_id(nid), "不合法的批注 id 被重新生成")
        s, _, b = _req(port, "GET", "/api/notes", good)
        ok(s == 200 and json.loads(b).get("reviewId") == "checkreview", "接口返回审阅标识（页面据此确认连对了服务）")
        disk = json.loads((tmp / store.NOTES_FILE).read_text(encoding="utf-8"))
        ok(any(n["id"] == nid for n in disk["notes"]), "批注已写入 notes.json")
        log_lines = (tmp / store.LOG_FILE).read_text(encoding="utf-8").strip().splitlines()
        ok(log_lines and json.loads(log_lines[-1])["op"] == "add", "notes.jsonl 追加了 add 记录")
        ok(oct((tmp / store.NOTES_FILE).stat().st_mode & 0o777) == "0o600"
           and oct((tmp / store.LOG_FILE).stat().st_mode & 0o777) == "0o600", "批注文件只有本用户可读写（0600）")
        s, _, _ = _req(port, "PATCH", f"/api/notes/{nid}", same, {"status": "resolved", "resolution": "已改"})
        ok(s == 200, f"标记已处理（{s}）")
        hook_out = tmp / "hook.json"
        srv.on_handoff = [sys.executable, "-c", "import sys,pathlib;pathlib.Path(sys.argv[1]).write_text(sys.stdin.read())",
                          str(hook_out)]
        s, _, _ = _req(port, "POST", "/api/handoff", {**same, "Origin": "http://evil.example"}, {"message": "x"})
        ok(s == 403, f"跨源「交给 Agent」被拒（{s}）")
        s, _, b = _req(port, "POST", "/api/handoff", same, {"message": "请处理"})
        h = json.loads(b) if s == 200 else {}
        ok(s == 200 and (h.get("handoff") or {}).get("pending") == 1, f"同源「交给 Agent」记下一次提交（{s}）")
        for _ in range(50):
            if hook_out.exists() and hook_out.stat().st_size:
                break
            time.sleep(0.1)
        payload = json.loads(hook_out.read_text(encoding="utf-8")) if hook_out.exists() and hook_out.stat().st_size else {}
        ok(payload.get("seq") == 1 and payload.get("reviewId") == "checkreview" and payload.get("message") == "请处理",
           "提交时运行 --on-handoff 钩子，提交内容经标准输入传入")
        s, _, b = _req(port, "POST", "/api/handoff", same, {"message": "补充"})
        ok(s == 200 and json.loads(b).get("merged") and json.loads(b).get("seq") == 1, "未被接收的提交再次提交时合并，不重复排队")
        srv.shutdown()
        srv.server_close()
        srv2 = ReviewServer(tmp, 0, log_to_file=True)
        t2 = threading.Thread(target=srv2.serve_forever, kwargs={"poll_interval": 0.1}, daemon=True)
        t2.start()
        s, _, b = _req(srv2.port, "GET", "/api/notes", {"Host": f"127.0.0.1:{srv2.port}"})
        notes = json.loads(b)["notes"] if s == 200 else []
        ok(any(n["id"] == nid and n["status"] == "resolved" for n in notes), "重启服务后批注与已处理状态仍在")
        srv2.shutdown()
        srv2.server_close()
        log_text = (tmp / LOG_FILE).read_text(encoding="utf-8")
        ok(log_text.count("[reject]") >= 10, f"拒绝原因写入 server.log（{log_text.count('[reject]')} 条）")
        ok("evil.example" in log_text and "<" not in log_text, "日志里的请求字段已过滤特殊字符")
    except Exception as e:  # 自检本身出错也要清楚报告
        results.append((False, f"自检异常：{type(e).__name__}: {e}"))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    for passed, label in results:
        print(f"{'PASS' if passed else 'FAIL'}  {label}")
    failed = sum(not p for p, _ in results)
    print(f"\n{len(results) - failed}/{len(results)} 通过")
    return 1 if failed else 0


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv[:1] == ["check"]:
        return check()
    if argv[:1] == ["status"]:
        ap = argparse.ArgumentParser(prog="serve.py status")
        ap.add_argument("dir", type=Path)
        return status(ap.parse_args(argv[1:]).dir)
    if argv[:1] == ["serve"]:
        argv = argv[1:]
    ap = argparse.ArgumentParser(prog="serve.py", description="annotation-everything 本地批注服务（只监听 127.0.0.1）")
    ap.add_argument("dir", type=Path, help="审阅目录（含 build_viewer.py 生成的 index.html）")
    ap.add_argument("--port", type=int, default=DEFAULT_PORT, help=f"首选端口，默认 {DEFAULT_PORT}；被占用时顺延")
    ap.add_argument("--no-open", action="store_true", help="不自动打开浏览器")
    ap.add_argument("--debug", action="store_true", help="记录每个请求（默认只记录拒绝、错误与批注改动）")
    ap.add_argument("--on-handoff", metavar="命令", help="可选：用户点「交给 Agent」时运行的命令（不经 shell；"
                                                      "提交内容以 JSON 从标准输入传入）")
    a = ap.parse_args(argv)
    return serve(a.dir, a.port, not a.no_open, a.debug, parse_hook(a.on_handoff))


if __name__ == "__main__":
    sys.exit(main())
