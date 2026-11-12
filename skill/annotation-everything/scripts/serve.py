#!/usr/bin/env python3
"""annotation-everything 本地批注服务：只监听本机，托管对照页，批注每次增删改即落盘。

用法：
  python3 serve.py <审阅目录或审阅标识>       # 启动（用这份审阅的专属端口），自动打开浏览器，Ctrl+C 停止
  python3 serve.py <审阅目录> --port 9000 --no-open --debug   # --port 只是本次临时换端口
  python3 serve.py <审阅目录> --claim-port 8800              # 把专属端口改成 8800 再启动
  python3 serve.py <审阅目录> --on-handoff "命令 参数"   # 可选：用户点「交给 Agent」时运行这条命令
  python3 serve.py status <审阅目录>          # 查看服务是否在跑、实际端口、拒绝/错误计数（给 Claude 排障用）
  python3 serve.py stop <审阅目录或标识> [--timeout 秒]  # 停止整份审阅；--status 只查询持久状态
  python3 serve.py list [--under 目录] [--json]   # 审阅台账：本机有哪些审阅、端口、状态、待接收的提交、有没有代理在等
  python3 serve.py forget <标识前缀>            # 从台账移除一份审阅，释放它的专属端口（不动审阅目录）
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
提交内容以 JSON 从标准输入传入（含 stage / target / allowSubagents / fullCheck / userMessage），另有环境变量 AE_EVENT / AE_REVIEW_DIR /
AE_HANDOFF_SEQ / AE_URL；运行中又来的事件排队依次补跑；命令输出丢弃，退出码记入 server.log。

专属端口：每份审阅第一次启动时在审阅台账（~/.annotation-everything/，AE_HOME 可改）里分到一个端口，此后一直用它，
停了也不让给别的审阅——浏览器暂存按地址（127.0.0.1:端口）隔离，地址不变，旧标签页和暂存才接得上。
本目录的服务已在跑就直接复用；专属端口被别的程序占用（或传了 --port）时本次临时顺延（跳过别人的专属端口，最多 20 个）
并警告，实际端口写进审阅目录的 .annotation-server.json。

语言：`--locale auto|zh-CN|en`（写在子命令前后都认）优先于环境变量 AE_LOCALE，再优先于审阅里的 locale；
三者都没有则中文，只有 auto 才读 LC_ALL / LC_MESSAGES / LANG。拒绝与错误的原因代码、HTTP 状态和退出码
不随语言变，只换给人看的说明；来源路径、命令与系统原始错误保持原文。
"""
from __future__ import annotations

import argparse
import contextlib
import fcntl
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
from urllib.parse import parse_qs

sys.path.insert(0, str(Path(__file__).resolve().parent))
import analysis_store  # noqa: E402
import fsutil  # noqa: E402
import i18n  # noqa: E402
import ledger  # noqa: E402
import notes_store as store  # noqa: E402
import review_overview  # noqa: E402
import review_save  # noqa: E402
import review_stop  # noqa: E402
import wakeup  # noqa: E402

APP = "annotation-everything"
VERSION = "0.12.1"
# 服务支持的功能；页面据此判断背后的服务是不是旧版（旧服务会静默丢掉框选、重新标注等新字段）
FEATURES = ["notes", "handoff", "marks", "batches", "stages", "always", "full-check", "agent-delivery", "stop-service", "stop-review", "stop-save-protection", "resume-review", "receiver-binding", "overview", "overview-stop", "analysis", "finalize-all", "docx"]

# Read one byte without buffering the hook's JSON input. EOF before registration exits
# without running the hook. exec preserves the registered PID and stdin for the command.
_HOOK_EXEC = """
import os
import sys
if os.read(0, 1) == b"1":
    os.execvp(sys.argv[1], sys.argv[1:])
"""

# 界面语言只有进程级一个来源（见 i18n.set_locale）：main() 与 serve() 按共同契约解析后
# 设置一次，服务与台账等各模块共同遵循；未设置前保持中文（旧调用行为不变）

# 本入口的 CLI 消息 key 表：argparse 报错类别与语言选择问题 → 消息 key
ERROR_KEYS = {
    i18n.CLI_ERROR_INVALID_CHOICE: "serve.err.unknownChoice",
    i18n.CLI_ERROR_MISSING_VALUE: "serve.err.missingValue",
    i18n.CLI_ERROR_UNKNOWN_ARGUMENT: "serve.err.unknownArgument",
    i18n.CLI_ERROR_MISSING_ARGUMENT: "serve.err.missingArgument",
    i18n.CLI_ERROR_GENERIC: "serve.err.generic",
    i18n.CLI_ERROR_EMPTY_LOCALE: "serve.err.emptyLocale",
    i18n.CLI_ERROR_INVALID_LOCALE: "serve.err.invalidLocale",
}


def T(key: str, **params: object) -> str:
    """按当前语言取一条消息；缺译时回退另一种语言（见 locales/README.md 的契约）。"""
    return i18n.T(key, **params)


def set_locale(locale: str) -> None:
    """薄转发：定进程级界面语言。台账等模块的错误因此也用同一种语言，不会中英混排。"""
    i18n.set_locale(locale)


def stored_locale(review_dir) -> object:
    """审阅里保存的语言选择（review.json 的 locale）；读不到返回 None。"""
    if review_dir is None:
        return None
    try:
        manifest = json.loads((Path(review_dir) / MANIFEST).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    value = manifest.get("locale") if isinstance(manifest, dict) else None
    return value if isinstance(value, str) and value.strip() else None


def cli_locale(explicit: object, review_dir=None) -> str:
    """按共同契约解析命令行语言：--locale > AE_LOCALE > 审阅 locale > 默认中文。"""
    return i18n.cli_locale(explicit, stored=stored_locale(review_dir))


class StaleServer(Exception):
    """本目录已有旧版本的服务在跑：复用它会让新页面的改动被静默丢掉。"""
DEFAULT_PORT = 8765
PORT_TRIES = 20
STATUS_FILE = ledger.STATUS_FILE   # 运行状态文件名只在台账模块定义一次；这里是给既有调用方的名字
LOG_FILE = "server.log"
MANIFEST = "review.json"
MAX_BODY = 2 * 1024 * 1024
HOST = "127.0.0.1"
NOTE_PATH = re.compile(r"^/api/notes/([A-Za-z0-9_-]{1,64})$")
SAFE_CHARS = re.compile(r"[^A-Za-z0-9._:/?=&%+@-]")


def csp_for(html_text: str) -> str:
    """只放行页面里的内联脚本（按内容 sha256），其余脚本、内联事件一律不执行。"""
    hashes = fsutil.script_hashes(html_text)
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

    def __init__(self, status: int, code: str, msg: str, *, params: dict[str, Any] | None = None):
        super().__init__(msg)
        self.status, self.code, self.msg = status, code, msg
        self.params = params


class _RequestReads:
    """一次请求里每个审阅文件只读一次：接收方绑定、批次记录、批注、技能目录。

    读失败也记住结果，同一请求里再要就按当时的异常重放，让公开状态、身份信息和批次摘要各自
    按自己原有的降级结果处理——复用读取结果不改变任何响应。
    """

    def __init__(self, server: "ReviewServer"):
        self.server = server
        self._done: dict[str, Any] = {}

    def _once(self, name: str, read: Any) -> Any:
        """读一次并记住；失败时返回当时的异常（不抛），交给消费方按原有降级结果处理。"""
        if name not in self._done:
            try:
                self._done[name] = read()
            except Exception as exc:   # 记住失败：同一请求里重放同一异常
                self._done[name] = exc
        return self._done[name]

    def _read_notes(self) -> dict[str, Any]:
        return store.load(self.server.review_dir)

    def binding(self) -> Any:
        """接收方绑定；manifest 取服务已有的缓存（按 mtime 判断，命中不开文件）。"""
        return self._once("binding", lambda: wakeup.load_binding(
            self.server.review_dir, manifest=self.server.manifest()))

    def handoff(self) -> Any:
        """批次记录。"""
        return self._once("handoff", lambda: store.handoff_load(self.server.review_dir))

    def notes_doc(self) -> dict[str, Any]:
        """批注文档；读失败就抛（与 store.load 自己读时一样）。"""
        got = self._once("notes", self._read_notes)
        if isinstance(got, Exception):
            raise got
        return got

    def notes_list(self) -> Any:
        """批注列表；读失败时返回当时的异常（消费方按同一降级结果处理）。"""
        got = self._once("notes", self._read_notes)
        return got["notes"] if isinstance(got, dict) else got

    def skills(self) -> dict[str, Any]:
        """技能目录。"""
        return self._once("skills", lambda: store.skills_doc(self.server.review_dir))

    def waiting(self) -> Any:
        """有没有这个绑定正在等的代理（读等待标记）；还没到读它那一步时返回 None。

        None 的语义是「调用方确认还没到读等待标记那一步」：没有绑定（或读取失败）、有批次在
        处理中、codex-desktop 三种情况都在读标记之前返回。当前这样是安全的——receiver_status
        收到 None 时会自己读一次等待标记，而这三种情况它在读之前就先返回了（未配置 / error /
        processing / event），既不会多开文件也不会漏判。将来新增分支时注意：只有在确认不可能
        有代理在等时才返回 None，否则要真的去读一次等待标记。
        """
        def read():
            binding, handoff = self.binding(), self.handoff()
            if isinstance(binding, Exception) or isinstance(handoff, Exception) or not binding:
                return None
            if any(b.get("status") in ("claimed", "reviewing") for b in handoff.get("items") or []):
                return None
            if binding.get("adapter") == "codex-desktop":
                return None
            return wakeup.waiting_state(self.server.review_dir, binding)
        return self._once("waiting", read)


class ReviewServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, review_dir: Path, port: int, debug: bool = False, log_to_file: bool = True,
                 on_handoff: list[str] | None = None, hook_cwd: str | None = None,
                 locale: str | None = None):
        self.review_dir = review_dir
        self.debug = debug
        # Capture identity and route from one atomic metadata read without changing an untouched review.
        self.agent_binding = wakeup.load_binding(review_dir)
        self.on_handoff = wakeup.hook_command(review_dir, on_handoff, binding=self.agent_binding)
        self.hook_cwd = hook_cwd or os.getcwd()
        # 诊断说明用的语言；None 时保持中文（旧调用行为不变）
        self.locale = locale or i18n.DEFAULT_LOCALE
        self._hook_lock = threading.Lock()
        self._hook_running = False
        self._hook_queue: list[dict[str, Any]] = []   # 钩子运行中又来的事件：按到达顺序排队，同一批同一事件只留最新
        self._writes = threading.Condition()
        self._active_writes = 0
        self.browser_saves = review_save.BrowserSaves(review_dir)
        self._close_lock = threading.Lock()
        self.closing = False
        self.stop_id: str | None = None
        self.shutdown_complete: threading.Event | None = None
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
        if not self.on_handoff or (merged and not self.agent_binding):
            return
        if not self._hook_binding_ok():
            return
        url = f"http://127.0.0.1:{self.port}/"
        user_message = store.handoff_user_message(item)
        message = store.full_check_message(user_message) if item.get("fullCheck") is True else user_message
        payload = {"event": event, "app": APP, "reviewId": self.review_id(), "reviewDir": str(self.review_dir),
                   "bindingId": (self.agent_binding or {}).get("bindingId"),
                   "url": url, "seq": item.get("seq"), "message": message, "userMessage": user_message,
                   "stage": "review" if event == "review" else "draft", "target": item.get("target", "final"),
                   "allowSubagents": bool(item.get("allowSubagents")), "fullCheck": bool(item.get("fullCheck")),
                   "reviewMessage": item.get("reviewMessage", ""),
                   "openCount": item.get("openCount"), "pages": item.get("pages"),
                   "notesFile": str(self.review_dir / store.NOTES_FILE),
                   "handoffFile": str(self.review_dir / store.HANDOFF_FILE),
                   "notesCli": str(Path(__file__).resolve().with_name("notes.py"))}
        self._enqueue_hook(payload, event, item.get("seq"))

    def run_analysis_hook(self, round_no: int) -> None:
        """分析轮次入队：与改稿批次同一个 hook 命令，事件类型为 analysis（轮次号放在 seq 位置）。

        等待命令要显式声明 --events analysis 才领得到，旧版等待进程不受影响。"""
        if not self.on_handoff or not self.agent_binding:
            return
        if not self._hook_binding_ok():
            return
        payload = {"event": "analysis", "app": APP, "reviewId": self.review_id(),
                   "reviewDir": str(self.review_dir),
                   "bindingId": (self.agent_binding or {}).get("bindingId"),
                   "url": f"http://127.0.0.1:{self.port}/", "seq": round_no,
                   "notesFile": str(self.review_dir / store.NOTES_FILE),
                   "notesCli": str(Path(__file__).resolve().with_name("notes.py"))}
        self._enqueue_hook(payload, "analysis", round_no)

    def _hook_binding_ok(self) -> bool:
        """钩子触发前核对接收方还是触发时那一个（中途换绑就不再投递）。"""
        try:
            current = wakeup.load_binding(self.review_dir)
        except (OSError, ValueError, KeyError):
            self.log("error", "code=receiver-invalid")
            return False
        if (current or {}).get("bindingId") != (self.agent_binding or {}).get("bindingId"):
            self.log("error", "code=receiver-restart")
            return False
        return True

    def _enqueue_hook(self, payload: dict[str, Any], event: str, seq: int | None) -> None:
        env = {**os.environ, "AE_EVENT": event, "AE_REVIEW_DIR": str(self.review_dir),
               "AE_HANDOFF_SEQ": str(seq), "AE_URL": payload["url"]}
        job = {"payload": payload, "env": env, "seq": seq, "event": event}
        with self._hook_lock:
            if not review_stop.accepts_work(self.review_dir):
                return
            if self._hook_running:
                self._hook_queue = [j for j in self._hook_queue if (j["event"], j["seq"]) != (job["event"], job["seq"])] + [job]
                return
            self._hook_running = True
        threading.Thread(target=self._hook_worker, args=(job,), daemon=True).start()

    def recover_deliveries(self) -> None:
        """Recover pending submissions once at startup; no periodic agent polling."""
        if self.agent_binding:
            for item in store.handoff_load(self.review_dir)["items"]:
                if item.get("status") in ("pending", "review"):
                    self.run_hook(item, False, "review" if item["status"] == "review" else "handoff")
            for round_ in analysis_store.queue(self.review_dir):
                self.run_analysis_hook(round_["seq"])

    def _hook_worker(self, job: dict[str, Any]) -> None:
        while job is not None:
            if not review_stop.accepts_work(self.review_dir):
                self.log("info", f"hook seq={job['seq']} skipped=review-stopped")
            else:
                proc = None
                try:
                    payload = b"1" + json.dumps(job["payload"], ensure_ascii=False).encode("utf-8")
                    command = [sys.executable, "-I", "-S", "-c", _HOOK_EXEC, *self.on_handoff]
                    proc = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
                                            stderr=subprocess.DEVNULL, cwd=self.hook_cwd, env=job["env"], start_new_session=True)
                    try:
                        review_stop.hook_started(self.review_dir, proc.pid, job["seq"], job["event"])
                    except BaseException:
                        proc.terminate()
                        proc.wait()
                        raise
                    try:
                        proc.communicate(payload)
                    finally:
                        try:
                            review_stop.hook_finished(self.review_dir, proc.pid)
                        except (OSError, ValueError) as e:
                            self.log("error", f"code=hook-tracking-failed seq={job['seq']} {type(e).__name__}: {e}")
                    if proc.returncode == 0:
                        self.log("info", f"hook seq={job['seq']} exit=0")
                    else:
                        self.log("error", f"code=hook-failed seq={job['seq']} exit={proc.returncode}")
                except (OSError, ValueError) as e:
                    self.log("error", f"code=hook-failed seq={job['seq']} {type(e).__name__}: {e}")
            with self._hook_lock:
                job = self._hook_queue.pop(0) if self._hook_queue else None
                if job is None:
                    self._hook_running = False

    def wait_for_hooks(self, timeout: float) -> bool:
        deadline = time.monotonic() + max(0.0, timeout)
        while True:
            with self._hook_lock:
                if not self._hook_running and not self._hook_queue:
                    return True
            left = review_stop.remaining(deadline)
            if not left:
                return False
            time.sleep(min(.02, left))

    @contextlib.contextmanager
    def write_request(self, *, origin=None, page_id=None, token=None):
        # Admission and draining share this condition. The persisted gate also applies to CLI work.
        with self._writes:
            if (not review_stop.accepts_work(self.review_dir)
                    and not self.browser_saves.allows_write(origin, page_id, token)):
                raise Reject(409, "review-stopped", i18n.Translator(self.locale)("serve.err.reviewStopped"))
            self._active_writes += 1
        try:
            yield
        finally:
            with self._writes:
                self._active_writes -= 1
                self._writes.notify_all()

    def wait_for_writes(self, timeout: float) -> bool:
        with self._writes:
            return self._writes.wait_for(lambda: self._active_writes == 0, timeout)

    def protect_browser_saves(self, stop_id: str, timeout: float, *, require_protocol: bool) -> str | None:
        if require_protocol:
            # Older generated pages run their own save preflight for a local stop.
            # An external caller cannot use that browser's preflight.
            index = self.index()
            if (index and b'const APP="annotation-everything", FILE=' in index[0]
                    and b'"/api/server/save-protection"' not in index[0]):
                return "save-protection-upgrade-required"
        return self.browser_saves.protect(self.review_id(), stop_id, timeout)

    def commit_stop(self, stop_id: str) -> bool:
        with self._close_lock:
            if self.closing:
                return False
            self.stop_id = stop_id
            return True

    def begin_close(self) -> str | None:
        with self._close_lock:
            self.closing = True
            return self.stop_id

    def manifest(self) -> dict[str, Any]:
        def read(f: Path | None) -> dict[str, Any]:
            try:
                m = json.loads(f.read_text(encoding="utf-8")) if f else {}
                return m if isinstance(m, dict) else {}
            except (OSError, ValueError):
                return {}
        return self._cached(MANIFEST, read)

    def review_id(self) -> str | None:
        return self.manifest().get("reviewId")

    def review_name(self) -> str:
        return ledger.review_name(self.manifest())

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
    _reads: "_RequestReads | None" = None

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
        self._json(e.status, {"error": e.msg, "code": e.code, "app": APP,
                              **({"params": e.params} if e.params is not None else {})})

    def _t(self, key: str, **params: object) -> str:
        """拒绝说明用的消息：按服务启动时定的语言取，原因代码与状态不走这里。"""
        return i18n.Translator(self.server.locale)(key, **params)

    def _check_host(self) -> None:
        host = (self.headers.get("Host") or "").strip().lower()
        if host not in self.server.allowed_hosts:
            raise Reject(421, "bad-host", self._t("serve.err.badHost"))

    def _check_api_origin(self, mutating: bool) -> None:
        site = self.headers.get("Sec-Fetch-Site")
        if site and site not in ("same-origin", "none"):
            raise Reject(403, "cross-site", self._t("serve.err.crossSite"))
        origin = self.headers.get("Origin")
        if origin is not None and origin.lower() not in self.server.allowed_origins:
            raise Reject(403, "bad-origin", self._t("serve.err.badOrigin"))
        if mutating and origin is None:
            raise Reject(403, "no-origin", self._t("serve.err.noOrigin"))

    def _body(self) -> Any:
        ctype = (self.headers.get("Content-Type") or "").split(";")[0].strip().lower()
        if ctype != "application/json":
            raise Reject(415, "bad-content-type", self._t("serve.err.badContentType"))
        if self.headers.get("Transfer-Encoding"):
            raise Reject(411, "chunked", self._t("serve.err.chunked"))
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError as e:
            raise Reject(400, "bad-length", self._t("serve.err.badLength")) from e
        if length < 0:
            raise Reject(400, "bad-length", self._t("serve.err.badLength"))
        if length > MAX_BODY:
            raise Reject(413, "too-large", self._t("serve.err.tooLarge"))
        raw = self.rfile.read(length) if length else b""
        try:
            return json.loads(raw.decode("utf-8") or "null")
        except (UnicodeDecodeError, json.JSONDecodeError) as e:
            raise Reject(400, "bad-json", self._t("serve.err.badJson")) from e

    def _dispatch(self) -> None:
        self._reads = None   # 长连接复用同一个 handler：每个请求重新读一次审阅文件
        try:
            self._check_host()
            path = self.path.split("?", 1)[0]
            if path.startswith("/api/"):
                mutating = self.command not in ("GET", "HEAD")
                self._check_api_origin(mutating=mutating)
                # Overview actions address the review named in their body. Archiving changes
                # an atomic metadata marker and is also available for already stopped reviews.
                if mutating and path not in ("/api/server/stop", "/api/server/resume",
                                             "/api/server/save-protection", "/api/overview/start",
                                             "/api/overview/archive", "/api/overview/stop"):
                    save = (path in ("/api/notes", "/api/notes/import") or NOTE_PATH.fullmatch(path))
                    token = self.headers.get("X-AE-Save-Token") if save else None
                    if token is not None and (self.headers.get("Content-Type") or "").split(";")[0].strip().lower() != "application/json":
                        raise Reject(415, "bad-content-type", self._t("serve.err.badContentType"))
                    with self.server.write_request(
                        origin=(self.headers.get("Origin") or "").lower(),
                        page_id=self.headers.get("X-AE-Page") if save else None,
                        token=token,
                    ):
                        self._api(path)
                else:
                    self._api(path)
            elif self.command in ("GET", "HEAD") and path in ("/", "/index.html"):
                self._index()
            elif self.command in ("GET", "HEAD") and path == "/favicon.ico":
                self._send(204, b"", "image/x-icon")
            else:
                raise Reject(404, "not-found", self._t("serve.err.notFound"))
        except Reject as e:
            self._reject(e)
        except (store.NoteError, UnicodeEncodeError) as e:  # 由请求内容引起的错误一律 4xx，页面不会反复重试
            detail = {"error": str(e) if isinstance(e, store.NoteError)
                       else self._t("serve.err.badCharacters"), "code": "bad-note", "app": APP}
            if isinstance(e, store.TextLimitError):
                params = {"field": e.field, "limit": e.limit}
                detail.update(error=self._t("serve.err.textTooLong", **params),
                              reason="text-too-long", params=params)
            self._json(400, detail)
        except store.NotFound:
            self._json(404, {"error": self._t("serve.err.noNote"), "code": "no-note", "app": APP})
        except Exception as e:  # 服务端错误也写日志，方便排障
            self.server.log("error", f"code=server-error method={clean(self.command, 12)} path={clean(self.path)} "
                                     f"{type(e).__name__}: {e}")
            # 只说明类型与恢复动作，不回显异常内容（可能含请求数据）
            self._json(500, {"error": self._t("serve.err.serverError", type=type(e).__name__),
                             "code": "server-error", "app": APP})

    do_GET = do_HEAD = do_POST = do_PATCH = do_DELETE = do_PUT = _dispatch

    def do_OPTIONS(self) -> None:  # 不支持预检：跨源写请求在浏览器侧即失败
        try:
            self._check_host()
            raise Reject(405, "preflight", self._t("serve.err.preflight"))
        except Reject as e:
            self._reject(e)

    # ---------- 页面 ----------
    def _index(self) -> None:
        got = self.server.index()
        if got is None:
            raise Reject(404, "no-index", self._t("serve.err.noIndex"))
        body, csp = got
        self._send(200, body, "text/html; charset=utf-8", {"Content-Security-Policy": csp})

    # ---------- API ----------
    def _request_reads(self) -> "_RequestReads":
        """本次请求的审阅文件读取：每个文件只读一次。"""
        if self._reads is None:
            self._reads = _RequestReads(self.server)
        return self._reads

    def _receiver(self, active_binding) -> dict[str, Any]:
        reads = self._request_reads()
        return wakeup.receiver_status(self.server.review_dir, active_binding, binding=reads.binding(),
                                      handoff=reads.handoff(), waiting=reads.waiting())

    def _public_status(self) -> dict[str, Any]:
        reads = self._request_reads()
        return wakeup.public_status(self.server.review_dir, binding=reads.binding(),
                                    handoff=reads.handoff(), waiting=reads.waiting())

    def _handoff_state(self, *, full: bool) -> dict[str, Any]:
        reads = self._request_reads()
        return store.handoff_state(self.server.review_dir, full=full,
                                   handoff=reads.handoff(), notes=reads.notes_list())

    def _notes_doc(self) -> dict[str, Any]:
        return self._request_reads().notes_doc()

    def _ident(self) -> dict[str, Any]:
        # name：审阅名。别的审阅的旧标签页连到这个端口时，据此告诉用户「这里现在是哪一份」（无 CORS 头，只有同源页面读得到）
        return {"app": APP, "reviewId": self.server.review_id(), "name": self.server.review_name(),
                "archived": review_overview.is_archived(self.server.review_dir),
                "version": VERSION, "features": FEATURES,
                "receiver": self._receiver(self.server.agent_binding)}

    def _stop_review(self, body: dict[str, Any]) -> None:
        try:
            timeout = review_stop.validate_timeout(body.get("timeout", 10.0))
        except ValueError:
            raise Reject(400, "bad-request", self._t("serve.err.badStopTimeout")) from None
        deadline = time.monotonic() + timeout
        d = self.server.review_dir
        try:
            with review_stop.operation(d, deadline=deadline):
                if self.server.closing or self.server.stop_id is not None:
                    raise review_stop.OperationBusy
                self.server.log("info", "review stop requested")
                review_stop.begin(d, http={"pid": os.getpid(), "port": self.server.port})
                result = review_stop.coordinate(d, server=self.server, deadline=deadline,
                                                browser_protocol_required=body.get("requirePageSave") is True)
                ready = review_stop.ready_for_http(result) and self.server.commit_stop(result["stopId"])
                try:
                    self._json(200, {**self._ident(), "stopping": ready,
                                     "state": result["state"], "stop": result})
                    self.wfile.flush()
                except ConnectionError:
                    if not ready:
                        raise
                finally:
                    if ready:
                        self.server.shutdown()
                        if self.server.shutdown_complete is not None:
                            # Keep the operation lock until the serving thread has closed the port and
                            # recorded this stopId. A concurrent start will then probe the new reality.
                            self.server.shutdown_complete.wait()
        except review_stop.OperationBusy:
            raise Reject(409, "review-stop-busy", self._t("serve.err.reviewLifecycleBusy")) from None

    def _resume_review(self) -> None:
        d = self.server.review_dir
        try:
            with review_stop.operation(d, deadline=time.monotonic() + 5):
                if self.server.closing or self.server.stop_id is not None:
                    raise review_stop.OperationBusy
                if not review_stop.accepts_work(d):
                    if not self.server._hook_binding_ok():
                        raise Reject(409, "receiver-restart", self._t("serve.err.receiverRestart"))
                    review_stop.start(d)
                    self.server.recover_deliveries()
        except review_stop.OperationBusy:
            raise Reject(409, "review-stop-busy", self._t("serve.err.reviewLifecycleBusy")) from None
        self._json(200, {"app": APP, "reviewId": self.server.review_id()})

    def _check_receiver(self, body: dict[str, Any]) -> None:
        # Called inside the batch lock: stop gating, transfer and submission share one authorization point.
        if not review_stop.accepts_work(self.server.review_dir):
            raise Reject(409, "review-stopped", self._t("serve.err.reviewStopped"))
        try:
            binding = wakeup.load_binding(self.server.review_dir)
        except (OSError, ValueError, KeyError):
            raise Reject(409, "receiver-invalid", self._t("serve.err.receiverInvalid"))
        current = (binding or {}).get("bindingId")
        if current != (self.server.agent_binding or {}).get("bindingId"):
            raise Reject(409, "receiver-restart", self._t("serve.err.receiverRestart"))
        if body.get("receiverBindingId") != current:
            raise Reject(409, "receiver-mismatch", self._t("serve.err.receiverMismatch"))

    def _analysis_round(self, body: dict[str, Any]) -> int:
        round_no = body.get("round")
        if not isinstance(round_no, int) or isinstance(round_no, bool):
            raise Reject(400, "bad-request", self._t("serve.err.noRound"))
        return round_no

    def _analysis_error_text(self, error: store.HandoffError) -> str:
        messages = {"focus-required": "serve.life.focusRequired", "page-not-found": "serve.life.pageMissing",
                    "analysis-closed": "serve.life.analysisClosed", "analysis-invalid-input": "serve.life.invalidInput",
                    "structure-target-changed": "serve.life.structureChanged"}
        protection_codes = ("analysis-locked", "bad-option", "draft-basis-changed", "note-not-changed", "bad-updates",
                            "updates-required", "update-not-pending", "update-source-changed", "round-not-answerable")
        if error.code in protection_codes:
            return self._t("cli.analysis.protect.error." + error.code)
        if error.code in ("checkpoint-not-found", "line-not-found", "analysis-pending-rounds"):
            return self._t("cli.analysis.history.error." + error.code)
        if error.code in ("assumption-not-found", "bad-dead-ends", "dead-end-option", "dead-end-line", "dead-end-obsolete"):
            params = dict(error.params)
            if error.code in ("dead-end-option", "dead-end-line"):
                params["assumptions"] = " / ".join(params.get("assumptions", [])) or self._t("cli.analysis.resolution.noAssumptions")
            return self._t("cli.analysis.resolution.error." + error.code, **params)
        return self._t(messages[error.code]) if error.code in messages else str(error)

    def _analysis_state(self, d: Path, aid: str) -> dict[str, Any]:
        state = analysis_store.load(d, aid)
        state["linkedNotes"] = analysis_store.linked_notes(d, aid)
        return state

    def _analysis_write(self, d: Path, body: dict[str, Any], op: str) -> Any:
        """审阅人侧的分析写操作：校验入参，存储拒绝映射成 4xx，
        原因代码随响应给页面（与存储层 HandoffError 的 code 一致）。"""
        aid = body.get("analysis")
        if not isinstance(aid, str) or not aid:
            raise Reject(400, "bad-request", self._t("serve.err.noAnalysis"))
        try:
            if op == "answer":
                key, value = body.get("key"), body.get("value")
                if not isinstance(key, str) or not isinstance(value, dict):
                    raise Reject(400, "bad-request", self._t("serve.err.noAnswer"))
                return analysis_store.answer(d, aid, self._analysis_round(body), key, value, by="page")
            if op == "submit":
                return analysis_store.submit_round(d, aid, self._analysis_round(body), by="page",
                                                   authorize=lambda: self._check_receiver(body))
            if op == "convert":
                node = body.get("node")
                if not isinstance(node, str) or not node:
                    raise Reject(400, "bad-request", self._t("serve.err.noNode"))
                return analysis_store.convert(d, aid, node, text=str(body.get("text") or ""), by="page")
            if op == "confirm":
                draft = body.get("draft")
                if not isinstance(draft, str) or not draft:
                    raise Reject(400, "bad-request", self._t("serve.err.noDraft"))
                return analysis_store.confirm_convert(d, aid, draft, by="page")
            if op == "release":
                return analysis_store.release_source(d, aid, by="page")
            if op == "activate":
                return analysis_store.activate(d, aid, by="page")
            if op == "pause":
                return analysis_store.pause(d, aid, by="page")
            if op == "refute":
                assumption = body.get("assumption")
                if not isinstance(assumption, str) or not assumption:
                    raise Reject(400, "bad-request", self._t("cli.analysis.resolution.error.assumption-not-found"))
                return analysis_store.refute_assumption(d, aid, assumption, by="page",
                                                        authorize=lambda: self._check_receiver(body))
            if op in ("seal", "fork", "switch-line", "close", "reopen"):
                operations = {"seal": analysis_store.seal_checkpoint, "fork": analysis_store.fork_line,
                              "switch-line": analysis_store.switch_line, "close": analysis_store.close_analysis,
                              "reopen": analysis_store.reopen_analysis}
                args = []
                if op in ("seal", "fork"):
                    checkpoint = body.get("checkpoint")
                    if ((op == "fork" and checkpoint is None) or
                            (checkpoint is not None and (not isinstance(checkpoint, str) or not checkpoint))):
                        raise Reject(400, "bad-request", self._t("cli.analysis.history.error.checkpoint-not-found"))
                    args.append(checkpoint)
                if op == "switch-line":
                    line = body.get("line")
                    if not isinstance(line, str) or not line:
                        raise Reject(400, "bad-request", self._t("cli.analysis.history.error.line-not-found"))
                    args.append(line)
                return operations[op](d, aid, *args, by="page", authorize=lambda: self._check_receiver(body))
            if op == "redecide":
                node, option = body.get("node"), body.get("option")
                if not isinstance(node, str) or not node:
                    raise Reject(400, "bad-request", self._t("cli.analysis.protect.noQuestion"))
                if not isinstance(option, str) or not option:
                    raise Reject(400, "bad-request", self._t("cli.analysis.protect.noOption"))
                return analysis_store.redecide(d, aid, node, option, by="page",
                                               authorize=lambda: self._check_receiver(body))
            if op in ("update-note", "keep-note", "delete-note"):
                note_id = body.get("noteId")
                if not isinstance(note_id, str) or not note_id:
                    raise Reject(400, "bad-request", self._t("cli.analysis.protect.noNote"))
                operation = {"update-note": analysis_store.request_update, "keep-note": analysis_store.keep_note,
                             "delete-note": analysis_store.delete_note}[op]
                operation(d, aid, note_id, by="page", authorize=lambda: self._check_receiver(body))
                return analysis_store.load(d, aid)
        except store.NotFound:
            raise Reject(404, "no-round", self._t("serve.err.noRound"))
        except store.HandoffError as e:
            params = ({"reason": e.params["reason"], "assumptions": e.params["assumptions"]}
                      if e.code in ("dead-end-option", "dead-end-line") else None)
            raise Reject(400, e.code, self._analysis_error_text(e), params=params)
        raise Reject(400, "bad-request", self._t("serve.err.noAnalysis"))   # 未覆盖的 op，不可达

    def _api(self, path: str) -> None:
        d, m = self.server.review_dir, NOTE_PATH.match(path)
        cmd = "GET" if self.command == "HEAD" else self.command
        # 页面带上自己的审阅标识：端口上换成了另一份审阅的服务时，先拒绝，绝不写进别人的 notes.json
        claimed = self.headers.get("X-AE-Review")
        if claimed is not None and path != "/api/health":
            rid = self.server.review_id()
            if not rid or claimed != rid:
                raise Reject(409, "review-mismatch", self._t("serve.err.reviewMismatch"))
        if path == "/api/health" and cmd == "GET":
            self._json(200, {**self._ident(), "dir": str(d), "pid": os.getpid(),
                             "port": self.server.port, "hook": bool(self.server.on_handoff),
                             "agentDelivery": self._public_status()})
        elif path == "/api/notes" and cmd == "GET":
            doc = self._notes_doc()
            idx = d / "index.html"
            doc["indexMtime"] = idx.stat().st_mtime if idx.exists() else None  # 页面据此提示「已重建，请刷新」
            doc["handoff"] = self._handoff_state(full=False)   # 轮询只带最新一批；处理记录全量走 GET /api/handoff
            doc["agentDelivery"] = self._public_status()
            skills = self._request_reads().skills()
            doc["skillCatalog"] = store.skills_catalog(d, doc=skills)
            doc["alwaysSkills"] = store.always_skills(d, doc=skills)
            self._json(200, {**doc, **self._ident()})
        elif path == "/api/handoff" and cmd == "GET":
            self._json(200, {**self._handoff_state(full=True), **self._ident()})
        elif path == "/api/server/save-protection" and cmd == "POST":
            if not claimed:
                raise Reject(409, "review-mismatch", self._t("serve.err.reviewMismatch"))
            self._save_protection(claimed)
        elif path in ("/api/server/stop", "/api/server/resume") and cmd == "POST":
            body = self._expect_obj(self._body())
            # 启停必须明确指向当前审阅；旧停止入口也进入完整生命周期。
            if not claimed or claimed != self.server.review_id():
                raise Reject(409, "review-mismatch", self._t("serve.err.reviewMismatch"))
            if path == "/api/server/stop":
                self._stop_review(body)
            else:
                self._resume_review()
        elif path == "/api/handoff" and cmd == "POST":
            body = self._expect_obj(self._body())
            if "fullCheck" in body and not isinstance(body["fullCheck"], bool):
                raise Reject(400, "bad-request", self._t("serve.err.fullCheck"))
            if "userMessage" in body and not isinstance(body["userMessage"], str):
                raise Reject(400, "bad-request", self._t("serve.err.userMessage"))
            message = body["userMessage"] if body.get("fullCheck") is True and "userMessage" in body else str(body.get("message") or "")
            _h, item, merged = store.handoff_submit(d, message, by="page",
                                                    target=str(body.get("target") or "final"),
                                                    allow_subagents=body.get("allowSubagents") is True,
                                                    full_check=body.get("fullCheck"),
                                                    authorize=lambda: self._check_receiver(body))
            self.server.log("info", f"handoff seq={item['seq']} open={item['openCount']} merged={merged} target={item['target']}")
            self.server.run_hook(item, merged)
            self._json(200, {"doc": {**self._notes_doc(), **self._ident()}, "handoff": self._handoff_state(full=False),
                             "merged": merged, "seq": item["seq"], "acceptedFullCheck": item["fullCheck"]})
        elif path == "/api/analysis" and cmd == "GET":
            params = parse_qs(self.path.partition("?")[2], keep_blank_values=True)
            ids = params.get("analysis", [])
            if len(ids) > 1 or (ids and not ids[0]):
                raise Reject(400, "bad-request", self._t("serve.err.noAnalysis"))
            requested = params.get("round", [])
            if len(requested) > 1 or (requested and (not ids or re.fullmatch(r"[1-9][0-9]*", requested[0]) is None)):
                raise Reject(400, "bad-request", self._t("serve.err.noRound"))
            requested_round = int(requested[0]) if requested else None
            rounds = analysis_store.delivery_items(d)
            result = {"analyses": analysis_store.list_analyses(d),
                      "queue": {status: sum(r["status"] == status for r in rounds)
                                for status in ("submitted", "claimed")}}
            if ids:
                try:
                    state = self._analysis_state(d, ids[0])
                    current = next((r for r in reversed(state["rounds"])
                                    if r.get("line", "main") == state["line"]
                                    and r["status"] in ("open", "submitted", "claimed")), None)
                    result.update(analysis=state,
                                  context=analysis_store.round_context(d, ids[0], requested_round)
                                  if requested_round is not None else
                                  analysis_store.round_context(d, ids[0], current["seq"]) if current else None)
                except store.NotFound:
                    raise Reject(404, "no-round" if requested_round is not None else "no-analysis",
                                 self._t("serve.err.noRound" if requested_round is not None else "serve.err.noAnalysis"))
            self._json(200, {**result, **self._ident()})
        elif path == "/api/analysis/start" and cmd == "POST":
            body = self._expect_obj(self._body())
            note_id = body.get("noteId")
            try:
                if "noteId" in body:
                    if not isinstance(note_id, str) or not note_id:
                        raise Reject(400, "bad-request", self._t("serve.err.noNoteId"))
                    state, round_no = analysis_store.start(d, note_id, by="page",
                                                           authorize=lambda: self._check_receiver(body))
                else:
                    state, round_no = analysis_store.start_question(d, body.get("focus"), page=body.get("page"),
                                                                    by="page", authorize=lambda: self._check_receiver(body))
            except store.HandoffError as error:
                raise Reject(400, error.code, self._analysis_error_text(error))
            except store.NotFound:
                raise Reject(404, "no-note", self._t("serve.err.noNote"))
            note = next((n for n in store.load(d)["notes"] if n.get("id") == note_id), None)
            self.server.log("info", f"analysis start note={note_id} analysis={state['id']} round={round_no}")
            self.server.run_analysis_hook(round_no)
            self._json(200, {"round": round_no, "analysis": state, "note": note, **self._ident()})
        elif path in ("/api/analysis/activate", "/api/analysis/pause") and cmd == "POST":
            body = self._expect_obj(self._body())
            state = self._analysis_write(d, body, path.rsplit("/", 1)[-1])
            self._json(200, {"analysis": state, **self._ident()})
        elif path in ("/api/analysis/redecide", "/api/analysis/update-note", "/api/analysis/keep-note",
                      "/api/analysis/delete-note", "/api/analysis/seal", "/api/analysis/fork",
                      "/api/analysis/switch-line", "/api/analysis/close", "/api/analysis/reopen",
                      "/api/analysis/refute") and cmd == "POST":
            body = self._expect_obj(self._body())
            state = self._analysis_write(d, body, path.rsplit("/", 1)[-1])
            if path.endswith(("/redecide", "/update-note", "/fork", "/reopen", "/refute")):
                for round_ in state["rounds"]:
                    if round_["status"] == "submitted":
                        self.server.run_analysis_hook(round_["seq"])
            self._json(200, {"analysis": self._analysis_state(d, body["analysis"]),
                             "doc": {**self._notes_doc(), **self._ident()}, **self._ident()})
        elif path == "/api/analysis/answer" and cmd == "POST":
            body = self._expect_obj(self._body())
            self._analysis_write(d, body, "answer")
            self._json(200, {"analysis": analysis_store.load(d, body["analysis"]), **self._ident()})
        elif path == "/api/analysis/submit" and cmd == "POST":
            body = self._expect_obj(self._body())
            self._analysis_write(d, body, "submit")
            self.server.run_analysis_hook(self._analysis_round(body))
            self._json(200, {"analysis": analysis_store.load(d, body["analysis"]), **self._ident()})
        elif path == "/api/analysis/convert" and cmd == "POST":
            body = self._expect_obj(self._body())
            draft = self._analysis_write(d, body, "convert")
            self.server.log("info", f"analysis convert analysis={body['analysis']} draft={draft['id']}")
            self._json(200, {"draft": draft, "analysis": analysis_store.load(d, body["analysis"]),
                             **self._ident()})
        elif path == "/api/analysis/confirm" and cmd == "POST":
            body = self._expect_obj(self._body())
            note = self._analysis_write(d, body, "confirm")
            self.server.log("info", f"analysis confirm analysis={body['analysis']} note={note['id']}")
            self._json(200, {"note": note, "analysis": analysis_store.load(d, body["analysis"]),
                             "doc": {**self._notes_doc(), **self._ident()}})
        elif path == "/api/analysis/release" and cmd == "POST":
            body = self._expect_obj(self._body())
            note = self._analysis_write(d, body, "release")
            self.server.log("info", f"analysis release analysis={body['analysis']} note={note['id']}")
            self._json(200, {"note": note, "analysis": analysis_store.load(d, body["analysis"]),
                             "doc": {**self._notes_doc(), **self._ident()}})
        elif path == "/api/skills/always" and cmd == "POST":
            body = self._expect_obj(self._body())
            if "always" not in body:
                raise Reject(400, "bad-request", self._t("serve.err.noAlways"))
            items = store.always_save(d, body.get("always"))
            self.server.log("info", f"always-skills count={len(items)}")
            self._json(200, {"alwaysSkills": items, **self._ident()})
        elif path == "/api/handoff/finalize-all" and cmd == "POST":
            body = self._expect_obj(self._body())
            groups = body.get("batches")
            selections, seen = {}, set()
            if not isinstance(groups, list):
                raise Reject(400, "bad-request", self._t("serve.err.badFinalizeSelection"))
            for group in groups:
                seq, ids = (group.get("seq"), group.get("ids")) if isinstance(group, dict) else (None, None)
                if (not isinstance(seq, int) or isinstance(seq, bool) or seq < 1 or seq in selections
                        or not isinstance(ids, list) or not ids or any(not store.valid_id(nid) for nid in ids)
                        or len(set(ids)) != len(ids) or seen.intersection(ids)):
                    raise Reject(400, "bad-request", self._t("serve.err.badFinalizeSelection"))
                selections[seq] = set(ids)
                seen.update(ids)
            result = store.handoff_finalize_all(d, selections)
            self.server.log("info", f"finalize-all count={result['finalizedCount']} skipped={result['skippedCount']}")
            self._json(200, {**result, "doc": {**self._notes_doc(), **self._ident()},
                             "handoff": self._handoff_state(full=False), **self._ident()})
        elif path in ("/api/handoff/review", "/api/handoff/finalize") and cmd == "POST":
            body = self._expect_obj(self._body())
            seq = body.get("seq")
            if not isinstance(seq, int) or isinstance(seq, bool):
                raise Reject(400, "bad-request", self._t("serve.err.noSeq"))
            if path.endswith("review"):
                item = store.handoff_request_review(d, seq, str(body.get("message") or ""),
                                                    allow_subagents=body.get("allowSubagents") is True, by="page",
                                                    authorize=lambda: self._check_receiver(body))
                self.server.log("info", f"handoff-review seq={seq}")
                self.server.run_hook(item, False, event="review")
            else:
                item = store.handoff_finalize(d, seq, by="page")
                self.server.log("info", f"handoff-finalize seq={seq}")
            self._json(200, {"doc": {**self._notes_doc(), **self._ident()}, "handoff": self._handoff_state(full=False),
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
        elif path == "/api/overview" and cmd == "GET":
            scopes = parse_qs(self.path.partition("?")[2], keep_blank_values=True).get("scope", ["workspace"])
            if len(scopes) != 1 or scopes[0] not in ("workspace", "all"):
                raise Reject(400, "bad-scope", self._t("serve.err.overviewScope"))
            self._json(200, {"app": APP, "reviewId": self.server.review_id(), "features": FEATURES,
                             **review_overview.snapshot(d, self.server.review_id(), scopes[0], review_row)})
        elif path == "/api/overview/stop" and cmd == "POST":
            if not claimed:
                raise Reject(409, "review-mismatch", self._t("serve.err.reviewMismatch"))
            body = self._body()
            if (not isinstance(body, dict) or set(body) != {"reviewId", "scope"}
                    or not isinstance(body["reviewId"], str) or not ledger.RID.fullmatch(body["reviewId"])):
                raise Reject(400, "bad-overview-stop", self._t("serve.err.overviewStopBody"))
            rid, scope = body["reviewId"], body["scope"]
            if scope not in ("workspace", "all"):
                raise Reject(400, "bad-scope", self._t("serve.err.overviewScope"))
            target, _entry = overview_start_target(rid, self._t)
            if not review_overview.in_scope(target, review_overview.workspace_for(d), scope):
                raise Reject(404, "overview-review-out-of-scope", self._t("serve.err.overviewStopScope"))
            if rid == claimed:
                if target != d:
                    raise Reject(409, "overview-review-invalid", self._t("serve.err.overviewStartInvalid"))
                self._stop_review({"waitForExit": True, "requirePageSave": True})
            else:
                result = review_overview.stop_summary(stop_overview_review(target, rid, self._t))
                self._json(200, {"app": APP, "reviewId": rid, "state": result["state"], "stop": result})
        elif path == "/api/overview/start" and cmd == "POST":
            body = self._body()
            if not isinstance(body, dict) or set(body) != {"reviewId"}:
                raise Reject(400, "bad-overview-start", self._t("serve.err.overviewStartBody"))
            url = start_overview_review(body["reviewId"], self._t)
            self._json(200, {"app": APP, "url": url})
        elif path == "/api/overview/archive" and cmd == "POST":
            body = self._expect_obj(self._body())
            target, archived = body.get("reviewId"), body.get("archived")
            if not isinstance(target, str) or not ledger.RID.fullmatch(target) or not isinstance(archived, bool):
                raise Reject(400, "bad-archive", self._t("serve.err.archivePayload"))
            entry = ledger.get(target)
            if entry is None:
                raise Reject(404, "archive-unknown", self._t("serve.err.archiveUnknown"))
            if ledger.entry_state(target, entry) != "ok":
                raise Reject(409, "archive-unavailable", self._t("serve.err.archiveUnavailable"))
            review_overview.set_archived(Path(entry["dir"]), archived)
            self._json(200, {"app": APP, "reviewId": target, "archived": archived})
        else:
            raise Reject(404 if cmd == "GET" else 405, "no-api", self._t("serve.err.noApi"))

    def _expect_obj(self, body: Any) -> dict[str, Any]:
        if not isinstance(body, dict):
            raise store.NoteError(self._t("serve.err.notAnObject"))
        return body

    def _save_protection(self, rid: str) -> None:
        body = self._body()
        identity = {"pageId", "clientId"}
        receipt = identity | {"stopId", "token", "saved"}
        if (not isinstance(body, dict)
                or set(body) not in (identity, identity | {"closing"}, receipt)
                or any(not isinstance(body[key], str) or not review_save.PAGE_ID.fullmatch(body[key])
                       for key in identity)
                or ("closing" in body and body["closing"] is not True)
                or (set(body) == receipt and (not isinstance(body["saved"], bool)
                                             or not isinstance(body["stopId"], str)
                                             or not isinstance(body["token"], str)))):
            raise Reject(400, "bad-save-protection", self._t("serve.err.saveProtectionBody"))
        try:
            result = self.server.browser_saves.update(rid, self.headers["Origin"].lower(), body)
        except review_save.SaveConflict:
            raise Reject(409, "save-protection-conflict", self._t("serve.err.saveProtectionConflict")) from None
        self._json(200, {"app": APP, "reviewId": rid, "request": result,
                         "active": review_stop.accepts_work(self.server.review_dir)})


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


def read_status(review_dir: Path) -> dict[str, Any] | None:
    try:
        info = json.loads(ledger.status_file(review_dir).read_text(encoding="utf-8"))
        return info if isinstance(info, dict) else None
    except (OSError, ValueError):
        return None


def write_status(server: ReviewServer) -> None:
    info = {"app": APP, "version": VERSION, "pid": os.getpid(), "port": server.port,
            "url": f"http://127.0.0.1:{server.port}/", "dir": str(server.review_dir),
            "hook": bool(server.on_handoff), "startedAt": store.now_iso()}
    f = ledger.status_file(server.review_dir)
    fd = os.open(f, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(json.dumps(info, ensure_ascii=False, indent=2) + "\n")


def clear_status(review_dir: Path) -> None:
    info = read_status(review_dir)
    if info and info.get("pid") == os.getpid():
        ledger.status_file(review_dir).unlink(missing_ok=True)


def same_dir(found: dict[str, Any], review_dir: Path) -> bool:
    try:
        return found.get("app") == APP and Path(found.get("dir", "")).resolve() == review_dir.resolve()
    except (OSError, TypeError):
        return False


def overview_start_target(rid: Any, translate: Any) -> tuple[Path, dict[str, Any]]:
    """只按完整台账标识找目标；路径和目录中的身份必须仍然有效。"""
    if not isinstance(rid, str) or not ledger.RID.fullmatch(rid):
        raise Reject(400, "bad-overview-start", translate("serve.err.overviewStartBody"))
    entry = ledger.get(rid)
    if entry is None:
        raise Reject(404, "overview-review-unknown", translate("serve.err.overviewStartUnknown"))
    directory = Path(entry["dir"]).resolve()
    if ledger.entry_state(rid, entry) != "ok" or not (directory / "index.html").is_file():
        raise Reject(409, "overview-review-invalid", translate("serve.err.overviewStartInvalid"))
    return directory, entry


def overview_health(found: dict[str, Any] | None, rid: str, directory: Path, port: int) -> bool:
    return bool(found and same_dir(found, directory) and found.get("reviewId") == rid
                and found.get("port") == port)


def stop_overview_review(directory: Path, rid: str, translate: Any) -> dict[str, Any]:
    """Only dispatch to the live service verified for this ledger entry."""
    deadline = time.monotonic() + 10.0
    state = review_stop.load(directory)
    if state["state"] == "stopped":
        return state

    def unconfirmed(component: str, code: str) -> dict[str, Any]:
        return {**state, "state": "incomplete", "components": {
            **state["components"], component: {"state": "incomplete", "error": code},
        }, "incompleteReasons": {**state.get("incompleteReasons", {}), component: code}}

    info = read_status(directory) or {}
    port, pid = info.get("port"), info.get("pid")
    if type(pid) is not int or not ledger.pid_alive(pid):
        if state["state"] == "active":
            raise Reject(409, "overview-review-not-running", translate("serve.err.overviewStopNotRunning"))
        return unconfirmed("http", "http-status-unconfirmed")
    found = probe(port, timeout=.5) if type(port) is int and 1024 <= port <= 65535 else None
    if not overview_health(found, rid, directory, port) or found.get("pid") != pid:
        return unconfirmed("http", "http-status-unconfirmed")
    features = found.get("features")
    if not isinstance(features, list) or not all(name in features for name in ("stop-review", "stop-save-protection")):
        return unconfirmed("http", "upgrade-required")
    state = _request_live_review_stop(directory, port, state, deadline)
    if state["state"] == "active":
        return unconfirmed("http", "stop-status-unconfirmed")
    return state


def _review_request(port: int, rid: str, path: str, body: dict[str, Any], timeout: float):
    headers = {"Host": f"127.0.0.1:{port}", "Origin": f"http://127.0.0.1:{port}",
               "Content-Type": "application/json", "X-AE-Review": rid}
    return _req(port, "POST", path, headers, body, timeout=timeout)


def resume_running_review(directory: Path, rid: str, port: int, found: dict[str, Any],
                          timeout: float = 6.0) -> bool:
    if not overview_health(found, rid, directory, port):
        return False
    if "resume-review" not in (found.get("features") or []):
        return review_stop.accepts_work(directory)
    try:
        code, _, raw = _review_request(port, rid, "/api/server/resume", {}, timeout)
        result = json.loads(raw)
        return code == 200 and result.get("app") == APP and result.get("reviewId") == rid
    except (OSError, ValueError, http.client.HTTPException):
        return False


def stop_overview_child(child: subprocess.Popen) -> None:
    """失败只回收本次 Popen 持有的进程，从不按状态文件 PID 停服务。"""
    if child.poll() is None:
        child.terminate()
    try:
        child.wait(timeout=1)
    except subprocess.TimeoutExpired:
        child.kill()
        child.wait()


def start_overview_review(rid: Any, translate: Any) -> str:
    overview_start_target(rid, translate)
    deadline = time.monotonic() + 5
    # 每份审阅一把启动锁，跨托管服务串行化；不能持台账写锁等子进程登记。
    try:
        fd = os.open(ledger.home() / f".overview-start.{rid}.lock",
                     os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600)
        with os.fdopen(fd, "r+") as lock:
            while True:
                try:
                    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if time.monotonic() >= deadline:
                        raise Reject(503, "overview-start-failed", translate("serve.err.overviewStartFailed"))
                    time.sleep(0.05)
            try:
                return _start_overview_review_locked(rid, translate, deadline)
            finally:
                fcntl.flock(lock, fcntl.LOCK_UN)
    except OSError:
        raise Reject(503, "overview-start-failed", translate("serve.err.overviewStartFailed")) from None


def _start_overview_review_locked(rid: str, translate: Any, deadline: float) -> str:
    directory, entry = overview_start_target(rid, translate)
    # 状态文件只提供候选端口；旧版服务也要实际核对健康响应中的身份。
    info = read_status(directory) or {}
    candidates = (entry.get("port"), info.get("port"))
    for port in candidates:
        if isinstance(port, int) and not isinstance(port, bool) and 1024 <= port <= 65535:
            found = probe(port, timeout=0.2)
            if overview_health(found, rid, directory, port):
                if resume_running_review(directory, rid, port, found, max(.1, review_stop.remaining(deadline))):
                    return f"http://127.0.0.1:{port}/"
                if probe(port, timeout=0.2) is not None:
                    raise Reject(503, "overview-start-failed", translate("serve.err.resumeFailed"))
    port = ledger.assign_port(rid, directory,
                              is_free=lambda candidate: probe(candidate, timeout=0.2) is None)
    if probe(port, timeout=0.2) is not None:
        raise Reject(409, "overview-port-busy", translate("serve.err.overviewStartBusy"))
    if time.monotonic() >= deadline:
        raise Reject(503, "overview-start-failed", translate("serve.err.overviewStartFailed"))
    child = None
    ready = False
    try:
        child = subprocess.Popen([sys.executable, str(Path(__file__).resolve()), str(directory),
                                  "--no-open", "--overview-start", rid],
                                 cwd=str(directory), stdin=subprocess.DEVNULL,
                                 env={**os.environ, "AE_HOME": str(ledger.home())},
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                 start_new_session=True)
        while time.monotonic() < deadline:
            found = probe(port, timeout=0.2)
            if overview_health(found, rid, directory, port):
                if found.get("pid") != child.pid:
                    stop_overview_child(child)
                    if not resume_running_review(directory, rid, port, found,
                                                 max(.1, review_stop.remaining(deadline))):
                        raise Reject(503, "overview-start-failed", translate("serve.err.resumeFailed"))
                else:
                    threading.Thread(target=child.wait, daemon=True).start()
                ready = True
                return f"http://127.0.0.1:{port}/"
            if child.poll() is not None:
                break
            time.sleep(0.05)
        raise Reject(503, "overview-start-failed", translate("serve.err.overviewStartFailed"))
    except OSError:
        raise Reject(503, "overview-start-failed", translate("serve.err.overviewStartFailed")) from None
    finally:
        if child is not None and not ready:
            stop_overview_child(child)


def bind(review_dir: Path, port: int, debug: bool, tries: int = PORT_TRIES,
         on_handoff: list[str] | None = None,
         avoid: dict[int, str] | None = None,
         locale: str | None = None) -> tuple[ReviewServer | None, str | None, list[str]]:
    """返回 (服务, 已在运行的 URL, 过程记录)。本目录已有旧版本服务在跑时抛 StaleServer（不静默复用）。

    avoid：别的审阅的专属端口（端口 → 标识）。顺延时跳过它们，免得占了别人的地址。
    """
    avoid = avoid or {}
    notes: list[str] = []

    def check_version(found: dict[str, Any], p: int) -> None:
        if found.get("version") != VERSION:
            raise StaleServer(T("serve.stale.detail", old=found.get('version') or T("serve.stale.oldWord"),
                                port=p, pid=found.get('pid'), version=VERSION))

    info = read_status(review_dir)
    if info and ledger.pid_alive(int(info.get("pid") or 0)) and info.get("port"):
        found = probe(int(info["port"]))
        if found and same_dir(found, review_dir):
            check_version(found, int(info["port"]))
            notes.append(T("serve.bind.runningHere", port=info['port'], pid=info.get('pid')))
            return None, f"http://127.0.0.1:{info['port']}/", notes
    for p in range(port, port + tries):
        if p in avoid:
            notes.append(T("serve.bind.skipReserved", port=p, rid=avoid[p][:8]))
            continue
        found = probe(p)
        if found is not None:
            if same_dir(found, review_dir):
                check_version(found, p)
                notes.append(T("serve.bind.runningThere", port=p, pid=found.get('pid')))
                return None, f"http://127.0.0.1:{p}/", notes
            who = T("serve.bind.otherReview") if found.get("app") == APP else T("serve.bind.otherProgram")
            notes.append(T("serve.bind.busy", port=p, who=who))
            continue
        try:
            return ReviewServer(review_dir, p, debug=debug, on_handoff=on_handoff,
                                locale=locale or i18n.current_locale()), None, notes
        except OSError as e:
            notes.append(T("serve.bind.failed", port=p, detail=e.strerror or e))
    return None, None, notes


def parse_hook(cmd: str | None) -> list[str] | None:
    """--on-handoff 的命令按 shell 规则拆分但不经 shell 执行；找不到可执行文件就报错。"""
    if not cmd:
        return None
    try:
        argv = shlex.split(cmd)
    except ValueError as e:
        raise SystemExit(T("serve.err.hookSyntax", detail=e)) from None
    if not argv:
        return None
    exe = argv[0]
    full = os.path.abspath(exe) if os.sep in exe else shutil.which(exe)
    if not full or not os.access(full, os.X_OK):
        raise SystemExit(T("serve.err.hookMissing", command=exe))
    return [full, *argv[1:]]   # 钩子在启动 serve.py 的目录下运行，参数里的相对路径按这个目录解析


class ClaimRefused(Exception):
    """--claim-port 不成立（端口归别的审阅、范围不对、台账不可用）：不启动，免得占了别人的地址。"""


def _others_ports(rid: str | None) -> dict[int, str]:
    """别的审阅的专属端口（台账读不了就只能空着）。复制来的副本连原审阅（同一标识）的端口也要避开，所以 rid 传 None。"""
    try:
        return {pt: k for pt, k in ledger.owners().items() if k != rid}
    except (ledger.LedgerError, OSError):
        return {}


def plan_port(review_dir: Path, rid: str | None, port: int | None, claim: int | None) -> tuple[int, int | None, dict[int, str]]:
    """决定本次先试哪个端口。返回 (首选端口, 专属端口, 要避开的别人的专属端口)。

    - 不传 --port：用专属端口；还没有就分配（已在运行的认领当前端口，否则从 8765 起挑没人认领、此刻空闲的最小端口）；
    - --port N：本次临时用 N（被占就顺延），专属端口不变；
    - --claim-port N：把专属端口改成 N 再启动；不成立就抛 ClaimRefused，不启动。
    分不到专属端口时（旧版目录没有标识、复制来的副本、台账不可用、端口都被认领）退回旧做法：从 --port 或 8765 起顺延，
    但仍避开台账里别人的专属端口，只给警告。
    """
    fallback = port or DEFAULT_PORT
    if claim and not rid:
        raise ClaimRefused(T("serve.port.claimNoRid"))
    if not rid:
        print(T("serve.port.noRid"), file=sys.stderr)
        return fallback, None, _others_ports(None)
    if claim:
        try:
            ledger.claim_port(rid, review_dir, claim)
        except (ledger.LedgerError, OSError) as e:
            raise ClaimRefused(str(e)) from None
        print(T("serve.port.claimed", port=claim), file=sys.stderr)
    try:
        ledger.register(rid, review_dir)
        owned = (ledger.get(rid) or {}).get("port")
        if not owned:
            info = read_status(review_dir)
            running = int(info["port"]) if info and info.get("port") and ledger.pid_alive(int(info.get("pid") or 0)) else None

            def free(p: int) -> bool:
                found = probe(p, timeout=0.5)
                return found is None or same_dir(found, review_dir)
            owned = ledger.assign_port(rid, review_dir, prefer=running, is_free=free)
            print(T("serve.port.owned", port=owned), file=sys.stderr)
    except ledger.CopiedDir as e:
        print(T("serve.port.copied", detail=e, port=fallback), file=sys.stderr)
        return fallback, None, _others_ports(None)
    except (ledger.LedgerError, OSError) as e:
        print(T("serve.port.fallback", detail=e, port=fallback), file=sys.stderr)
        return fallback, None, _others_ports(rid)
    return (port if port else owned), owned, _others_ports(rid)


def serve(review_dir: Path, port: int | None, open_browser: bool, debug: bool, on_handoff: list[str] | None = None,
          claim: int | None = None, locale: str | None = None, overview_start: str | None = None) -> int:
    set_locale(locale or cli_locale(None, review_dir))
    # 启动期间和整个服务生命周期持共享锁；更新只在没有服务时原子切换。
    with contextlib.ExitStack() as stack:
        try:
            stack.enter_context(ledger.update_lock())
        except OSError:
            # 台账位置不可用时仍允许审阅；更新器遇到同一错误时会拒绝切换。
            print(T("serve.serve.updateLockUnavailable"), file=sys.stderr)
        try:
            source = Path(__file__).resolve().read_text(encoding="utf-8")
            match = re.search(r'^VERSION = "([^"]+)"', source, re.M)
            if match is None or match.group(1) != VERSION:
                print(T("serve.serve.updateRestart"), file=sys.stderr)
                return 6
        except OSError:
            print(T("serve.serve.updateRestart"), file=sys.stderr)
            return 6
        return _serve_locked(review_dir, port, open_browser, debug, on_handoff, claim, locale, overview_start)


def _serve_locked(review_dir: Path, port: int | None, open_browser: bool, debug: bool,
                  on_handoff: list[str] | None = None, claim: int | None = None,
                  locale: str | None = None, overview_start: str | None = None) -> int:
    review_dir = review_dir.resolve()
    set_locale(locale or cli_locale(None, review_dir))
    if not (review_dir / "index.html").is_file():
        print(T("serve.serve.noIndex", dir=review_dir), file=sys.stderr)
        return 2
    try:
        store.load(review_dir)
    except ValueError as e:
        print(T("serve.serve.badNotes", detail=e), file=sys.stderr)
        return 2
    m = ledger.read_manifest(review_dir)   # 读不了或不是 JSON 对象都返回 {}：不提示复制
    problem = ledger.unsupported_format(m)
    if problem:   # 更高版本的报告格式（如新版 DOCX 元数据）：不启动，免得旧服务读写它
        print(problem, file=sys.stderr)
        return 2
    if overview_start is not None:
        try:
            directory, _entry = overview_start_target(overview_start, T)
        except (Reject, ledger.LedgerError, OSError):
            print(T("serve.err.overviewStartInvalid"), file=sys.stderr)
            return 2
        if directory != review_dir or m.get("reviewId") != overview_start or port is not None or claim is not None:
            print(T("serve.err.overviewStartInvalid"), file=sys.stderr)
            return 2
    orig = ledger.copied_from(review_dir, m)
    if orig is not None:
        print(T("serve.serve.copiedHint", orig=orig, dir=review_dir), file=sys.stderr)
    rid = m.get("reviewId")
    with review_stop.operation(review_dir) if rid else contextlib.nullcontext():
        try:
            first, owned, avoid = plan_port(review_dir, rid, port, claim)
        except ClaimRefused as e:
            print(T("serve.serve.claimRefused", port=claim, detail=e), file=sys.stderr)
            return 2
        if overview_start is not None and (not owned or first != owned):
            print(T("serve.err.overviewStartFailed"), file=sys.stderr)
            return 3
        try:
            server, running_url, trail = bind(review_dir, first, debug, tries=1 if overview_start else PORT_TRIES,
                                              on_handoff=on_handoff, avoid=avoid,
                                              locale=i18n.current_locale())
        except StaleServer as e:
            print(T("serve.serve.stale", detail=e), file=sys.stderr)
            return 6
        for t in trail:
            print(T("serve.port.trail", detail=t), file=sys.stderr)
        if overview_start is not None and running_url and running_url != f"http://127.0.0.1:{owned}/":
            print(T("serve.err.overviewStartFailed"), file=sys.stderr)
            return 3
        if not running_url:
            if server is None:
                print(T("serve.serve.noPort", first=first, last=first + PORT_TRIES - 1), file=sys.stderr)
                return 3
            url = f"http://127.0.0.1:{server.port}/"
            if owned and server.port != owned:
                why = T("serve.serve.whyTemp", port=server.port) if port \
                    else T("serve.serve.whyOccupied", owned=owned, port=server.port)
                print(T("serve.serve.tempPortNote", why=why, owned=owned), file=sys.stderr)
            elif not owned and server.port != first:
                print(T("serve.port.firstBusy", first=first, port=server.port), file=sys.stderr)
            if rid:
                server.shutdown_complete = threading.Event()
                review_stop.start(review_dir)
                review_stop.configure_hooks(review_dir, bool(server.on_handoff))
            write_status(server)
            server.recover_deliveries()
    if running_url:
        binding = wakeup.load_binding(review_dir)
        running_port = int(running_url.rstrip("/").rsplit(":", 1)[1])
        found = probe(running_port) or {}
        receiver = found.get("receiver") or {}
        needs_restart = receiver.get("state") == "restart-required"
        if binding:
            needs_restart = needs_restart or "receiver-binding" not in found.get("features", []) or receiver.get("bindingId") != binding["bindingId"]
            if binding["adapter"] == "codex-desktop":
                needs_restart = needs_restart or "agent-delivery" not in found.get("features", []) or not found.get("hook")
        if needs_restart:
            print(T("serve.serve.agentRestart"), file=sys.stderr)
            return 6
        if rid and not resume_running_review(review_dir, rid, running_port, found):
            print(T("serve.err.resumeFailed"), file=sys.stderr)
            return 6
        if claim and f":{claim}/" not in running_url:
            print(T("serve.port.claimButRunning", port=claim), file=sys.stderr)
        print(T("serve.serve.running", url=running_url)
              + (T("serve.serve.runningHookNote") if on_handoff else ""))
        if open_browser:
            webbrowser.open(running_url)
        return 0
    server.log("info", f"启动 {APP} {VERSION}：{url} 托管 {review_dir}（pid {os.getpid()}）")
    print(T("serve.serve.ready", url=url, path=review_dir / store.NOTES_FILE)
          + (T("serve.serve.hookLine", command=shlex.join(on_handoff)) if on_handoff else "")
          + T("serve.serve.stopHint"), flush=True)

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
        stop_id = server.begin_close()
        server.log("info", T("serve.serve.stoppedLog"))
        try:
            # A coordinated stop already owns the lifecycle lock in the request thread.
            with review_stop.operation(review_dir) if rid and stop_id is None else contextlib.nullcontext():
                server.server_close()
                clear_status(review_dir)
                if stop_id is not None:
                    review_stop.http_stopped(review_dir, stop_id)
        finally:
            if server.shutdown_complete is not None:
                server.shutdown_complete.set()
    return 0


def status(review_dir: Path, locale: str | None = None) -> int:
    """汇总运行状态。只输出计数与本服务写的字段，不回显请求内容（日志里的路径等来自外部，不可信）。"""
    set_locale(locale or cli_locale(None, review_dir))
    review_dir = review_dir.resolve()
    out: dict[str, Any] = {"dir": str(review_dir), "index_html": (review_dir / "index.html").is_file()}
    m = ledger.read_manifest(review_dir)
    out["reviewId"] = m.get("reviewId")
    out["name"] = ledger.review_name(m) or None
    try:
        e = ledger.get(out["reviewId"]) if out["reviewId"] else None
        out["owned_port"] = (e or {}).get("port")
    except (ledger.LedgerError, OSError) as err:
        out["owned_port"] = T("serve.status.ledgerDown", detail=err)
    try:
        doc = store.load(review_dir)
        notes = doc["notes"]
        out["notes"] = {"rev": doc.get("rev"), "total": len(notes),
                        "open": sum(n.get("status", "open") == "open" for n in notes),
                        "drafted": sum(n.get("status") == "drafted" for n in notes),
                        "resolved": sum(n.get("status") == "resolved" for n in notes)}
    except ValueError as e:
        out["notes"] = {"error": T("serve.status.notesBroken", type=type(e).__name__)}
    hs = store.handoff_state(review_dir, full=False)
    latest = hs.get("latest") or {}
    out["handoff"] = {k: hs.get(k) for k in ("pending", "claimed", "drafted", "review", "reviewing")} | {
                      "latest": {k: latest.get(k) for k in ("seq", "status", "at", "claimedAt", "doneAt")} if latest else None}
    code = 3
    info = read_status(review_dir)
    if info is not None:
        pid = int(info.get("pid") or 0)
        out["status_file"] = {k: info.get(k) for k in ("pid", "port", "url", "startedAt", "version")}
        alive = ledger.pid_alive(pid)
        health = probe(int(info.get("port") or 0)) if info.get("port") else None
        if alive and health and same_dir(health, review_dir) and health.get("pid") == pid:
            out["server"] = T("serve.status.running", url=info.get('url'))
            code = 0
        elif alive:
            out["server"] = T("serve.status.stuck", pid=pid, port=info.get('port'))
            code = 5
        else:
            out["server"] = T("serve.status.stale")
            ledger.status_file(review_dir).unlink(missing_ok=True)
            code = 4
    else:
        out["server"] = T("serve.status.notRunning")
    check_port = out["owned_port"] if isinstance(out.get("owned_port"), int) else DEFAULT_PORT
    found = probe(check_port)
    out["owned_port_now" if isinstance(out.get("owned_port"), int) else "default_port"] = (
        T("serve.status.portIdle") if found is None else
        T("serve.status.portSelf") if same_dir(found, review_dir) else
        T("serve.status.portOther") if found.get("app") == APP else T("serve.status.portForeign"))
    out["agent_waiting"] = read_wait(review_dir)
    out["agentDelivery"] = wakeup.public_status(review_dir)
    log = review_dir / LOG_FILE
    if log.exists():
        lines = log.read_text(encoding="utf-8", errors="replace").splitlines()
        rejects = Counter(m.group(1) for ln in lines if (m := re.search(r"\[reject\] code=([a-z-]+)", ln)))
        errors = [ln for ln in lines if "[error]" in ln]
        out["log"] = {"file": str(log), "reject_counts": dict(rejects), "errors": len(errors),
                      "last_error_at": errors[-1][:19] if errors else None,
                      "note": T("serve.status.logNote")}
    print(json.dumps(out, ensure_ascii=False, indent=2))
    return code


# ---------- 审阅台账：list / forget ----------
def read_wait(review_dir: Path) -> list[str]:
    """正在 notes.py wait 这份审阅的代理所等的阶段（draft / review / any）；标记过期（进程已退出）的不算。"""
    stages: list[str] = []
    try:
        marks = list(Path(review_dir).glob(ledger.WAIT_PREFIX + "*.json"))
    except OSError:
        return stages
    for f in marks:
        try:
            w = json.loads(f.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(w, dict) and f.name == ledger.wait_file(review_dir, int(w.get("pid") or 0)).name \
                and ledger.pid_alive(int(w.get("pid") or 0)):
            stages.append(str(w.get("stage") or "any"))
    return stages


def review_row(rid: str, e: dict[str, Any]) -> dict[str, Any]:
    """台账里一份审阅的现场状态。审阅名、运行与提交状态都从审阅目录现读。

    同一份 review.json 只读一次：状态判定、审阅名与接收方绑定的校验共用它；待接收 / 待复核计数
    直接用批次记录，不为此加载批注。
    """
    d = Path(e["dir"])
    row: dict[str, Any] = {"reviewId": rid, "dir": str(d), "port": e.get("port"), "name": None, "state": None,
                           "archived": review_overview.is_archived(d),
                           "state_id": None,
                           "running": None, "pending": 0, "review": 0, "waiting": False, "hook": False, "unattended": False,
                           "agentDelivery": None, "receiptConfirmed": None}
    m = ledger.read_manifest(d)
    st = ledger.entry_state(rid, e, m)
    # state 是给脚本读的判定值，恒为中文、不随语言改值；state_id 是语言无关的稳定值；
    # 给人看的那一层由 list_reviews 用 _state_label 按语言显示。
    if st == "missing":
        row["state"], row["state_id"] = "目录不在", "missing"
        return row
    row["name"] = ledger.review_name(m) or None
    if st == "mismatch":
        row["state"] = f"标识不符（目录里现在是 {str(m.get('reviewId') or '无')[:8]}）"
        row["state_id"] = "mismatch"
        return row
    info = read_status(d)
    if info and info.get("port") and ledger.pid_alive(int(info.get("pid") or 0)):
        row["running"] = int(info["port"])
        row["hook"] = bool(info.get("hook"))
        row["state_id"] = "running" if row["running"] == e.get("port") else "running-temp"
        row["state"] = ("运行中" if row["running"] == e.get("port")
                        else f"运行中（临时端口 {row['running']}）")
    else:
        row["state"], row["state_id"] = "已停", "stopped"
    try:
        try:
            handoff = store.handoff_load(d)
        except ValueError as exc:   # 批次记录损坏：计数按 0，投递状态按同一降级结果处理
            handoff = exc
        items = handoff["items"] if isinstance(handoff, dict) else []
        row["pending"] = sum(1 for i in items if i.get("status") == "pending")
        row["review"] = sum(1 for i in items if i.get("status") == "review")
        row["processing"] = any(i.get("status") in ("claimed", "reviewing") for i in items)
        # latest 只取 status / seq 两个字段判断回执，与 handoff_state 的 userMessage 归一无关；
        # 将来给这里加字段（或改「最新一条」的取法）时，需重新核对取值口径与回执判定。
        latest = items[-1] if items else {}
        delivery = row["agentDelivery"] = wakeup.public_status(d, handoff=handoff, manifest=m)
        event = "review" if latest.get("status") in ("review", "reviewing") else "handoff"
        if delivery.get("state") == "received" and delivery.get("seq") == latest.get("seq") and delivery.get("event") == event:
            row["receiptConfirmed"] = True
        # No receipt is unknown, never evidence that a claimed worker is offline.
    except (OSError, ValueError):
        pass
    stages = read_wait(d)
    row["waiting"] = bool(stages)
    # 有提交在等接收，却既没有对应阶段的代理在 wait、也没有钩子：用户点了「交给 Agent」也没人接
    takes_draft = any(x in ("draft", "any") for x in stages)
    takes_review = any(x in ("review", "any") for x in stages)
    row["unattended"] = not row["hook"] and bool((row["pending"] and not takes_draft) or (row["review"] and not takes_review))
    return row


def _state_label(r: dict[str, Any]) -> str:
    """状态的人读文案；判定值本身不随语言变。"""
    if r["state_id"] == "missing":
        return T("serve.msg.stateMissing")
    if r["state_id"] == "mismatch":
        rid = str(ledger.read_manifest(Path(r["dir"])).get("reviewId") or "")[:8]
        return (T("serve.msg.stateMismatch", id=rid) if rid
                else T("serve.msg.stateMismatchNoId"))
    if r["state_id"] == "running-temp":
        return T("serve.msg.stateRunningTemp", port=r["running"])
    if r["state_id"] == "running":
        return T("serve.msg.stateRunning")
    return T("serve.msg.stateStopped")


def list_reviews(under: Path | None, as_json: bool, locale: str | None = None) -> int:
    set_locale(locale or cli_locale(None))
    try:
        items = ledger.entries()
    except (ledger.LedgerError, OSError) as e:
        print(T("serve.msg.error", detail=e), file=sys.stderr)
        return 2
    root = under.expanduser().resolve() if under else None
    rows = [review_row(rid, e) for rid, e in items.items()
            if root is None or Path(e["dir"]) == root or root in Path(e["dir"]).parents]
    rows.sort(key=lambda r: (r["port"] or 1 << 20, r["dir"]))
    if as_json:
        print(json.dumps({"ledger": str(ledger.home() / ledger.LEDGER_FILE), "reviews": rows}, ensure_ascii=False, indent=2))
        return 0
    if not rows:
        print(T("serve.msg.listEmpty") + (T("serve.msg.listEmptyUnder", root=root) if root else "")
              + T("serve.msg.listAutoRegister"))
        return 0
    print(T("serve.msg.listHeader", path=ledger.home() / ledger.LEDGER_FILE, count=len(rows)))
    for r in rows:
        parts = [T("serve.msg.pending", count=r["pending"]) if r["pending"] else "",
                 T("serve.msg.review", count=r["review"]) if r["review"] else ""]
        sub = T("serve.msg.listSeparator").join(x for x in parts if x) or T("serve.msg.noPending")
        who = (T("serve.msg.agentWaiting") if r["waiting"]
               else T("serve.msg.hook") if r["hook"] else T("serve.msg.nobodyWaiting"))
        state = _state_label(r)
        if r["archived"]:
            state += " · " + T("serve.msg.archived")
        print(T("serve.msg.reviewHead", rid=r['reviewId'][:8], name=r['name'] or T("serve.msg.unknownName")))
        print(T("serve.msg.reviewLine", port=r['port'] or T("serve.msg.noPort"),
                state=state, sub=sub, who=who))
        print(T("serve.msg.reviewDir", dir=r['dir']))
        if r["unattended"]:
            print(T("serve.msg.unattended",
                    command=f"python3 {Path(__file__).resolve().with_name('notes.py')} wait {r['dir']}"))
        print()
    stale = [r for r in rows if r["state_id"] in ("missing", "mismatch")]
    if stale:
        print(T("serve.msg.stale", count=len(stale)))
    return 0


def forget(key: str, locale: str | None = None) -> int:
    set_locale(locale or cli_locale(None))
    try:
        rid, e = ledger.forget(key)
    except (ledger.LedgerError, OSError) as err:
        print(T("serve.msg.error", detail=err), file=sys.stderr)
        return 2
    print(T("serve.msg.forgetDone", rid=rid, dir=e['dir'])
          + (T("serve.msg.forgetPort", port=e['port']) if e.get("port") else "")
          + T("serve.msg.forgetTail"))
    return 0


def resolve_arg(arg: str) -> Path:
    """审阅目录参数也接受审阅标识（或至少 4 位的前缀）：从台账里查目录。"""
    try:
        return ledger.resolve_dir(arg)
    except (ledger.LedgerError, OSError) as e:
        raise SystemExit(T("serve.msg.error", detail=e)) from None


# ---------- 自检 ----------
def _req(port: int, method: str, path: str, headers: dict[str, str], body: Any = None,
         raw: bytes | None = None, timeout: float = 3) -> tuple[int, dict[str, str], bytes]:
    c = http.client.HTTPConnection(HOST, port, timeout=timeout)
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


def check(locale: str | None = None) -> int:
    from unittest.mock import patch

    set_locale(locale or cli_locale(None))
    results: list[tuple[bool, str]] = []

    def ok(cond: bool, label: str) -> None:
        results.append((bool(cond), label))

    tmp = Path(tempfile.mkdtemp(prefix="ae-check-"))
    previous_home = os.environ.get("AE_HOME")
    os.environ["AE_HOME"] = str(tmp / "ledger")
    try:
        (tmp / "index.html").write_text("<!doctype html><title>check</title><script>var ok=1</script>ok", encoding="utf-8")
        (tmp / MANIFEST).write_text(json.dumps({"reviewId": "checkreview", "pages": [
            {"n": 1, "key": "synthetic-summary", "title": "合成概要", "stable": True}]}), encoding="utf-8")
        srv = ReviewServer(tmp, 0, log_to_file=True, locale=i18n.current_locale())
        port = srv.port
        t = threading.Thread(target=srv.serve_forever, kwargs={"poll_interval": 0.1}, daemon=True)
        t.start()
        good = {"Host": f"127.0.0.1:{port}"}
        same = {**good, "Origin": f"http://127.0.0.1:{port}", "Content-Type": "application/json"}

        ok(srv.server_address[0] == "127.0.0.1", T("serve.check.loopback", host=srv.server_address[0]))
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
            ok(not reached, T("serve.check.lanUnreachable", ip=ip))
        else:
            results.append((True, T("serve.check.noLan")))
        s, h, b = _req(port, "GET", "/", good)
        ok(s == 200 and b"ok" in b, T("serve.check.indexOpens"))
        csp = h.get("content-security-policy", "")
        ok("'sha256-" in csp and "unsafe-inline" not in csp.split("script-src", 1)[1].split(";", 1)[0],
           T("serve.check.csp"))
        s, _, _ = _req(port, "GET", "/", {"Host": f"evil.example:{port}"})
        ok(s == 421, T("serve.check.badHost", status=s))
        s, _, _ = _req(port, "GET", "/api/notes", {"Host": f"evil.example:{port}"})
        ok(s == 421, T("serve.check.badHostRead", status=s))
        s, _, _ = _req(port, "GET", "/api/overview?scope=all", {"Host": f"evil.example:{port}"})
        ok(s == 421, T("serve.check.overviewHost", status=s))
        # 新写接口逐项过同一套安全门；被拒请求不能创建或改写状态文件。
        state_paths = [tmp / store.NOTES_FILE, tmp / store.LOG_FILE, tmp / store.HANDOFF_FILE]
        before_finalize = [path.read_bytes() if path.exists() else None for path in state_paths]
        finalize_guards = [({**same, "Host": "evil.example"}, 421, "Host"),
                           ({**same, "Origin": "http://evil.example"}, 403, "Origin"),
                           ({k: v for k, v in same.items() if k != "Origin"}, 403, "NoOrigin"),
                           ({**same, "Content-Type": "text/plain"}, 415, "Content-Type"),
                           ({**same, "Sec-Fetch-Site": "cross-site"}, 403, "Sec-Fetch-Site")]
        for headers, expected, guard in finalize_guards:
            s, h, _ = _req(port, "POST", "/api/handoff/finalize-all", headers, {"batches": []})
            ok(s == expected and "access-control-allow-origin" not in h,
               T("serve.check.finalizeAllGuard", guard=guard, status=s))
        ok([path.read_bytes() if path.exists() else None for path in state_paths] == before_finalize,
           T("serve.check.finalizeAllUnchanged"))
        # 在进程边界设置拒绝哨兵：失守也不能真的拉起不应启动的服务。
        start_headers = {**same, "X-AE-Review": "checkreview"}
        with patch.object(subprocess, "Popen", side_effect=AssertionError("unexpected overview child")) as spawned:
            guards = [({**start_headers, "Host": "evil.example"}, 421, "Host"),
                      ({**start_headers, "Origin": "http://evil.example"}, 403, "Origin"),
                      ({key: value for key, value in start_headers.items() if key != "Origin"}, 403, "NoOrigin"),
                      ({**start_headers, "Content-Type": "text/plain"}, 415, "ContentType"),
                      ({**start_headers, "Sec-Fetch-Site": "cross-site"}, 403, "CrossSite")]
            for headers, expected, label in guards:
                s, _, _ = _req(port, "POST", "/api/overview/start", headers, {"reviewId": "0" * 16})
                ok(s == expected, T("serve.check.overviewStart" + label, status=s))
            s, _, raw = _req(port, "POST", "/api/overview/start", start_headers, {"reviewId": "0" * 16})
            ok(s == 404 and json.loads(raw).get("code") == "overview-review-unknown",
               T("serve.check.overviewStartUnknown", status=s))
            ok(spawned.call_count == 0, T("serve.check.overviewStartNoProcess"))

        save_state_paths = [tmp / review_stop.STATE_FILE, tmp / review_save.REGISTRY]
        before_stop = [path.read_bytes() if path.exists() else None for path in save_state_paths]
        stop_body = {"reviewId": "0" * 16, "scope": "all"}
        save_body = {"pageId": "0" * 32, "clientId": "1" * 32}
        for path, body in (("/api/overview/stop", stop_body), ("/api/server/save-protection", save_body)):
            for headers, expected, guard in (*guards,
                    ({**start_headers, "X-AE-Review": "2" * 16}, 409, "X-AE-Review"),
                    (same, 409, "X-AE-Review")):
                s, h, _ = _req(port, "POST", path, headers, body)
                ok(s == expected and "access-control-allow-origin" not in h,
                   T("serve.check.overviewStopGuard", path=path, guard=guard, status=s))
        s, _, raw = _req(port, "POST", "/api/overview/stop", start_headers, stop_body)
        ok(s == 404 and json.loads(raw).get("code") == "overview-review-unknown",
           T("serve.check.overviewStopUnknown", status=s))
        s, _, raw = _req(port, "POST", "/api/overview/stop", start_headers, {**stop_body, "port": port})
        ok(s == 400 and json.loads(raw).get("code") == "bad-overview-stop",
           T("serve.check.overviewStopTarget", status=s))
        ok([path.read_bytes() if path.exists() else None for path in save_state_paths] == before_stop,
           T("serve.check.saveProtectionUnchanged"))

        archive = {"reviewId": "0" * 16, "archived": True}
        s, _, _ = _req(port, "POST", "/api/overview/archive", {**same, "Host": f"evil.example:{port}"}, archive)
        ok(s == 421, T("serve.check.archiveHost", status=s))
        s, _, _ = _req(port, "POST", "/api/overview/archive", {**same, "Origin": "http://evil.example"}, archive)
        ok(s == 403, T("serve.check.archiveOrigin", status=s))
        s, _, _ = _req(port, "POST", "/api/overview/archive", {k: v for k, v in same.items() if k != "Origin"}, archive)
        ok(s == 403, T("serve.check.archiveNoOrigin", status=s))
        s, _, _ = _req(port, "POST", "/api/overview/archive", {**same, "Content-Type": "text/plain"}, archive)
        ok(s == 415, T("serve.check.archiveContentType", status=s))
        s, _, _ = _req(port, "POST", "/api/notes", {**same, "Origin": "http://evil.example"}, {"page": 1, "text": "x"})
        ok(s == 403, T("serve.check.badOrigin", status=s))
        s, _, _ = _req(port, "POST", "/api/notes", {**same, "Origin": "null"}, {"page": 1, "text": "x"})
        ok(s == 403, T("serve.check.nullOrigin", status=s))
        hdr = {k: v for k, v in same.items() if k != "Origin"}
        s, _, _ = _req(port, "POST", "/api/notes", hdr, {"page": 1, "text": "x"})
        ok(s == 403, T("serve.check.noOrigin", status=s))
        s, _, _ = _req(port, "POST", "/api/notes", {**same, "Content-Type": "text/plain"}, {"page": 1, "text": "x"})
        ok(s == 415, T("serve.check.badContentType", status=s))
        s, _, _ = _req(port, "POST", "/api/notes", {**same, "Sec-Fetch-Site": "cross-site"}, {"page": 1, "text": "x"})
        ok(s == 403, T("serve.check.crossSite", status=s))
        s, _, _ = _req(port, "POST", "/api/notes", {**same, "Content-Length": "-5"}, raw=b"")
        ok(s == 400, T("serve.check.negativeLength", status=s))
        s, h, _ = _req(port, "OPTIONS", "/api/notes", {**good, "Origin": "http://evil.example",
                                                         "Access-Control-Request-Method": "POST"})
        ok(s == 405 and "access-control-allow-origin" not in h, T("serve.check.preflight", status=s))
        s, _, _ = _req(port, "GET", "/../notes.json", good)
        ok(s == 404, T("serve.check.traversal", status=s))
        s, _, _ = _req(port, "GET", "/server.log", good)
        ok(s == 404, T("serve.check.otherFiles", status=s))

        evil_id = 'z"><img src=x onerror=alert(1)>'
        s, h, b = _req(port, "POST", "/api/notes", same, {"id": evil_id, "page": 2, "text": "自检批注",
                                                           "pin": {"x": 10, "y": 20}})
        ok(s == 201 and "access-control-allow-origin" not in h, T("serve.check.sameOriginWrite", status=s))
        nid = json.loads(b)["note"]["id"] if s == 201 else ""
        ok(nid and nid != evil_id and store.valid_id(nid), T("serve.check.idRegenerated"))
        s, _, b = _req(port, "GET", "/api/notes", good)
        ok(s == 200 and json.loads(b).get("reviewId") == "checkreview", T("serve.check.reviewId"))
        disk = json.loads((tmp / store.NOTES_FILE).read_text(encoding="utf-8"))
        ok(any(n["id"] == nid for n in disk["notes"]), T("serve.check.noteOnDisk"))
        log_lines = (tmp / store.LOG_FILE).read_text(encoding="utf-8").strip().splitlines()
        ok(log_lines and json.loads(log_lines[-1])["op"] == "add", T("serve.check.logAppended"))
        ok(oct((tmp / store.NOTES_FILE).stat().st_mode & 0o777) == "0o600"
           and oct((tmp / store.LOG_FILE).stat().st_mode & 0o777) == "0o600", T("serve.check.fileMode"))
        s, _, _ = _req(port, "PATCH", f"/api/notes/{nid}", same, {"status": "resolved", "resolution": "已改"})
        ok(s == 200, T("serve.check.markResolved", status=s))
        hook_out = tmp / "hook.json"
        srv.on_handoff = [sys.executable, "-c", "import sys,pathlib;pathlib.Path(sys.argv[1]).write_text(sys.stdin.read())",
                          str(hook_out)]
        s, _, _ = _req(port, "POST", "/api/handoff", {**same, "Origin": "http://evil.example"}, {"message": "x"})
        ok(s == 403, T("serve.check.crossSiteHandoff", status=s))
        s, _, b = _req(port, "POST", "/api/handoff", same, {"message": "请处理"})
        h = json.loads(b) if s == 200 else {}
        ok(s == 200 and (h.get("handoff") or {}).get("pending") == 1, T("serve.check.handoffRecorded", status=s))
        for _ in range(50):
            if hook_out.exists() and hook_out.stat().st_size:
                break
            time.sleep(0.1)
        payload = json.loads(hook_out.read_text(encoding="utf-8")) if hook_out.exists() and hook_out.stat().st_size else {}
        ok(payload.get("seq") == 1 and payload.get("reviewId") == "checkreview" and payload.get("message") == "请处理",
           T("serve.check.hookRuns"))
        s, _, b = _req(port, "POST", "/api/handoff", same, {"message": "补充"})
        ok(s == 200 and json.loads(b).get("merged") and json.loads(b).get("seq") == 1, T("serve.check.merge"))
        s, _, b = _req(port, "POST", "/api/notes", same, {"page": 1, "text": "自检：第 1 页的口径对吗"})
        anid = json.loads(b)["note"]["id"] if s == 201 else ""
        analysis_guards = [({**same, "Host": f"evil.example:{port}"}, 421, "Host"),
                           ({**same, "Origin": "http://evil.example"}, 403, "Origin"),
                           ({k: v for k, v in same.items() if k != "Origin"}, 403, "NoOrigin"),
                           ({**same, "Content-Type": "text/plain"}, 415, "ContentType")]
        for headers, expected, label in analysis_guards:
            s, _, _ = _req(port, "POST", "/api/analysis/start", headers, {"noteId": anid})
            ok(s == expected, T("serve.check.analysisStart" + label, status=s))
        s, _, b = _req(port, "POST", "/api/analysis/start", same, {"noteId": anid})
        ok(s == 200 and json.loads(b).get("note", {}).get("status") == "held"
           and json.loads(b).get("round") == 1, T("serve.check.analysisStartOk", status=s))
        first = json.loads(b).get("analysis", {})
        claimed_round = analysis_store.claim(tmp, seq=1)
        count = len(store.load(tmp)["notes"])
        s, _, b = _req(port, "POST", "/api/analysis/start", same,
                       {"focus": "自检：这一页的结论有依据吗？", "page": 1})
        second = json.loads(b).get("analysis", {})
        ok(s == 200 and json.loads(b).get("note") is None and second.get("sourceNote") is None
           and second.get("sourceCopy", {}).get("key") == "synthetic-summary"
           and len(store.load(tmp)["notes"]) == count, T("serve.check.lifeFocus", status=s))
        s, _, b = _req(port, "POST", "/api/analysis/activate", same, {"analysis": first["id"]})
        active = json.loads(b).get("analysis", {})
        ok(s == 200 and active.get("status") == "active" and active.get("rev") == claimed_round["baseRev"]
           and analysis_store.load(tmp, second["id"])["status"] == "paused", T("serve.check.lifeSwitch", status=s))
        for headers, expected, label in analysis_guards:
            codes = [_req(port, "POST", "/api/analysis/" + op, headers, {"analysis": first["id"]})[0]
                     for op in ("activate", "pause", "redecide", "update-note", "keep-note", "delete-note",
                                "seal", "fork", "switch-line", "close", "reopen", "refute")]
            ok(all(code == expected for code in codes), T("serve.check.lifeGuard", guard=label, status=codes[-1]))
        analysis_store.writeback(tmp, first["id"], 1, claimed_round["baseRev"], [])
        s, _, _ = _req(port, "POST", "/api/server/stop", same, {})
        ok(s == 409, T("serve.check.stopNeedsReview"))
        s, _, b = _req(port, "POST", "/api/server/stop", {**same, "X-AE-Review": "checkreview"}, {})
        t.join(timeout=3)
        ok(s == 200 and json.loads(b).get("stopping") is True and not t.is_alive(),
           T("serve.check.stopFromPage"))
        srv.shutdown()
        srv.server_close()
        srv2 = ReviewServer(tmp, 0, log_to_file=True, locale=i18n.current_locale())
        t2 = threading.Thread(target=srv2.serve_forever, kwargs={"poll_interval": 0.1}, daemon=True)
        t2.start()
        s, _, b = _req(srv2.port, "GET", "/api/notes", {"Host": f"127.0.0.1:{srv2.port}"})
        notes = json.loads(b)["notes"] if s == 200 else []
        ok(any(n["id"] == nid and n["status"] == "resolved" for n in notes), T("serve.check.restartKeeps"))
        srv2.shutdown()
        srv2.server_close()
        log_text = (tmp / LOG_FILE).read_text(encoding="utf-8")
        ok(log_text.count("[reject]") >= 10, T("serve.check.rejectsLogged", count=log_text.count("[reject]")))
        ok("evil.example" in log_text and "<" not in log_text, T("serve.check.logFiltered"))
    except Exception as e:  # 自检本身出错也要清楚报告
        results.append((False, T("serve.check.crashed", type=type(e).__name__, detail=e)))
    finally:
        if previous_home is None:
            os.environ.pop("AE_HOME", None)
        else:
            os.environ["AE_HOME"] = previous_home
        shutil.rmtree(tmp, ignore_errors=True)
    for passed, label in results:
        print(T("serve.check.row", verdict=T("serve.check.pass" if passed else "serve.check.fail"), label=label))
    failed = sum(not p for p, _ in results)
    print(T("serve.check.summary", passed=len(results) - failed, total=len(results)))
    return 1 if failed else 0


def _stop_review_from_cli(review_dir: Path, timeout: float) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    try:
        with review_stop.operation(review_dir, deadline=deadline):
            state = review_stop.load(review_dir)
            if state["state"] == "stopped":
                return state
            info = read_status(review_dir) or {}
            alive = ledger.pid_alive(int(info.get("pid") or 0))
            port = int(info.get("port") or 0)
            probe_timeout = min(.5, max(.05, review_stop.remaining(deadline)))
            health = probe(port, timeout=probe_timeout) if alive and port else None
            running = (overview_health(health, state["reviewId"], review_dir, port)
                       and health.get("pid") == info.get("pid"))
            features = health.get("features") if running else None
            if not isinstance(features, list) or not all(name in features for name in ("stop-review", "stop-save-protection")):
                http = {"pid": info["pid"], "port": port} if alive else {"state": "stopped"}
                review_stop.begin(review_dir, http=http)
                if running:
                    return review_stop.mark(review_dir, "http", "incomplete", "upgrade-required")
                if not alive:
                    review_stop.mark(review_dir, "http", "stopped")
                    return review_stop.coordinate(review_dir, deadline=deadline)
                review_stop.coordinate(review_dir, deadline=deadline)
                review_stop.mark(review_dir, "hooks", "incomplete", "background-hooks-unconfirmed")
                return review_stop.mark(review_dir, "http", "incomplete", "http-status-unconfirmed")
    except review_stop.OperationBusy:
        return review_stop.load(review_dir)

    return _request_live_review_stop(review_dir, port, state, deadline)


def _request_live_review_stop(review_dir: Path, port: int, state: dict[str, Any],
                              deadline: float) -> dict[str, Any]:
    # The live server owns coordination, including the last state write after the socket closes.
    try:
        remaining = review_stop.remaining(deadline)
        code, _, raw = _review_request(port, state["reviewId"], "/api/server/stop",
                                       {"timeout": remaining, "requirePageSave": True}, remaining + 1)
        response = json.loads(raw)
        stop = response.get("stop") if isinstance(response, dict) else None
        if (code == 200 and isinstance(stop, dict) and response.get("app") == APP
                and response.get("reviewId") == state["reviewId"] == stop.get("reviewId")
                and response.get("stopping") is True):
            stop_id = stop.get("stopId")
            result = review_stop.load(review_dir)
            while result["stopId"] == stop_id and result["state"] == "stopping" \
                    and time.monotonic() < deadline + 1:
                time.sleep(.03)
                result = review_stop.load(review_dir)
            return result
    except (OSError, ValueError, http.client.HTTPException):
        pass
    return review_stop.load(review_dir)


def stop_review_command(review_dir: Path, *, query: bool, timeout: float) -> int:
    review_dir = review_dir.resolve()
    try:
        timeout = review_stop.validate_timeout(timeout)
    except ValueError:
        raise ValueError(T("serve.err.badStopTimeout")) from None
    result = review_stop.load(review_dir) if query else _stop_review_from_cli(review_dir, timeout)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if query or result["state"] == "stopped" else 1


def _add_help(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("-h", "--help", action="help", default=argparse.SUPPRESS,
                        help=T("serve.opt.help"))


def _add_locale(parser: argparse.ArgumentParser) -> None:
    # 子命令上的 --locale 用 SUPPRESS 默认值：没写时不覆盖写在根命令上的选择
    parser.add_argument("--locale", metavar="LANG", default=argparse.SUPPRESS,
                        help=T("serve.opt.locale"))


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    locale_choice = i18n.explicit_locale(argv)
    # 先按共同契约定语言，再建解析器：帮助、参数说明与错误消息才能用同一种语言
    try:
        set_locale(i18n.cli_locale(locale_choice))
    except i18n.LocaleError as exc:
        set_locale(i18n.DEFAULT_LOCALE)
        print(i18n.locale_error_message(exc, ERROR_KEYS, T), file=sys.stderr)
        return 2
    # 子命令也可以写在 --locale 之后：认子命令前先剥掉开头这个选项
    rest = argv[2:] if argv[:1] == ["--locale"] else (argv[1:] if argv[:1] and argv[0].startswith("--locale=") else argv)
    if rest[:1] == ["check"]:
        return check(locale=i18n.current_locale())
    if rest[:1] == ["status"]:
        ap = i18n.LocalizedParser(prog="serve.py status", add_help=False,
                                  keys=ERROR_KEYS, translate=T)
        _add_help(ap)
        _add_locale(ap)
        ap.add_argument("dir", type=str, help=T("serve.opt.dirOrId"))
        a = ap.parse_args(rest[1:])
        d = resolve_arg(a.dir)
        # 目录解析出来后还有一级兜底：审阅里保存的 locale
        set_locale(cli_locale(locale_choice, d))
        return status(d, locale=i18n.current_locale())
    if rest[:1] in (["stop"], ["stop-status"]):
        ap = i18n.LocalizedParser(prog="serve.py stop", add_help=False, description=T("serve.desc.stop"),
                                  keys=ERROR_KEYS, translate=T)
        _add_help(ap)
        _add_locale(ap)
        ap.add_argument("dir", type=str, help=T("serve.opt.dirOrId"))
        ap.add_argument("--status", action="store_true", help=T("serve.opt.stopStatus"))
        ap.add_argument("--timeout", type=float, default=10.0, help=T("serve.opt.stopTimeout"))
        a = ap.parse_args(rest[1:])
        try:
            d = ledger.resolve_dir(a.dir).resolve()
            set_locale(cli_locale(locale_choice, d))
            return stop_review_command(d, query=a.status or rest[0] == "stop-status", timeout=a.timeout)
        except (ledger.LedgerError, OSError, ValueError) as error:
            print(T("serve.msg.error", detail=error), file=sys.stderr)
            return 2
    if rest[:1] == ["list"]:
        ap = i18n.LocalizedParser(prog="serve.py list", add_help=False, description=T("serve.desc.list"),
                                  keys=ERROR_KEYS, translate=T)
        _add_help(ap)
        _add_locale(ap)
        ap.add_argument("--under", type=Path, metavar="目录" if i18n.current_locale() == "zh-CN" else "DIR",
                        help=T("serve.opt.under"))
        ap.add_argument("--json", action="store_true", help=T("serve.opt.json"))
        a = ap.parse_args(rest[1:])
        return list_reviews(a.under, a.json, locale=i18n.current_locale())
    if rest[:1] == ["forget"]:
        ap = i18n.LocalizedParser(prog="serve.py forget", add_help=False, description=T("serve.desc.forget"),
                                  keys=ERROR_KEYS, translate=T)
        _add_help(ap)
        _add_locale(ap)
        ap.add_argument("key", help=T("serve.opt.key"))
        a = ap.parse_args(rest[1:])
        return forget(a.key, locale=i18n.current_locale())
    argv = rest[1:] if rest[:1] == ["serve"] else rest
    ap = i18n.LocalizedParser(prog="serve.py", add_help=False, description=T("serve.desc"),
                              keys=ERROR_KEYS, translate=T)
    _add_help(ap)
    _add_locale(ap)
    ap.add_argument("dir", type=str, help=T("serve.opt.dir"))
    ap.add_argument("--port", type=int, help=T("serve.opt.port"))
    ap.add_argument("--claim-port", type=int, metavar="N", help=T("serve.opt.claimPort"))
    ap.add_argument("--no-open", action="store_true", help=T("serve.opt.noOpen"))
    ap.add_argument("--debug", action="store_true", help=T("serve.opt.debug"))
    ap.add_argument("--overview-start", help=argparse.SUPPRESS)
    ap.add_argument("--on-handoff", metavar="命令" if i18n.current_locale() == "zh-CN" else "COMMAND",
                    help=T("serve.opt.onHandoff"))
    a = ap.parse_args(argv)
    d = resolve_arg(a.dir)
    # 目录解析出来后还有一级兜底：审阅里保存的 locale
    set_locale(cli_locale(locale_choice, d))
    return serve(d, a.port, not a.no_open, a.debug, parse_hook(a.on_handoff),
                 claim=a.claim_port, locale=i18n.current_locale(), overview_start=a.overview_start)


if __name__ == "__main__":
    sys.exit(main())
