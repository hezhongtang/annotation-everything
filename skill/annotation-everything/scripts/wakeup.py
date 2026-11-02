#!/usr/bin/env python3
"""Review-scoped Codex desktop delivery. No scheduler or Codex configuration writes.

The desktop IPC adapter is capability checked; it is not a stable public Codex API.
An accepted IPC request and a chat acknowledgement are recorded separately.
"""
from __future__ import annotations

import argparse
import contextlib
import fcntl
import json
import os
from pathlib import Path
import shlex
import socket
import stat
import struct
import sys
import time
import uuid

import ledger
import notes_store as store

BINDING_FILE = ".agent-binding.json"
DELIVERY_FILE = ".agent-deliveries.json"
DISPATCH_LOCK = ".agent-dispatch.lock"
SCHEMA = 1
MAX_FRAME = 16 * 1024 * 1024


class Rejected(RuntimeError):
    """The desktop explicitly refused a request; delivery is known not to succeed."""


class DesktopIPC:
    def __init__(self, path, timeout=15):
        path = Path(path)
        info, parent = path.lstat(), path.parent.stat()
        if not stat.S_ISSOCK(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise ValueError("desktop-socket-owner")
        if parent.st_uid != os.getuid() or parent.st_mode & 0o022:
            raise ValueError("desktop-socket-directory")
        self.connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.connection.settimeout(timeout)
        self.timeout = timeout
        self.client = "initializing-client"
        try:
            self.connection.connect(str(path))
            self.client = self.request("initialize", {"clientType": "annotation-everything"})["result"]["clientId"]
        except BaseException:
            self.close()
            raise

    def close(self):
        self.connection.close()

    def read(self, count):
        data = b""
        while len(data) < count:
            part = self.connection.recv(count - len(data))
            if not part:
                raise ConnectionError("desktop-disconnected")
            data += part
        return data

    def request(self, method, params, version=0, target=None):
        request_id = str(uuid.uuid4())
        message = {"type": "request", "requestId": request_id, "sourceClientId": self.client,
                   "method": method, "version": version, "params": params,
                   "timeoutMs": int(self.timeout * 1000)}
        if target:
            message["targetClientId"] = target
        body = json.dumps(message, ensure_ascii=False).encode()
        self.connection.sendall(struct.pack("<I", len(body)) + body)
        deadline = time.monotonic() + self.timeout
        while time.monotonic() < deadline:
            self.connection.settimeout(max(.01, deadline - time.monotonic()))
            size = struct.unpack("<I", self.read(4))[0]
            if not 0 < size <= MAX_FRAME:
                raise ValueError("desktop-frame-size")
            reply = json.loads(self.read(size))
            if reply.get("type") == "response" and reply.get("requestId") == request_id:
                if reply.get("resultType") != "success":
                    raise Rejected("desktop-rejected:" + method)
                return reply
        raise TimeoutError("desktop-timeout")

    def owner(self, thread):
        result = self.request("thread-owner-discovery", {"hostId": "local", "conversationId": thread}, 1)
        owner = result.get("handledByClientId")
        if not owner:
            raise Rejected("desktop-thread-owner-unavailable")
        return owner

    def start(self, thread, owner, prompt, message_id):
        return self.request("thread-follower-start-turn", {
            "conversationId": thread,
            "turnStart": {"request": {"threadId": thread,
                "input": [{"type": "text", "text": prompt, "text_elements": []}],
                "clientUserMessageId": message_id},
                "context": {"inheritThreadSettings": True}},
        }, 2, owner)


def _read(path):
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise ValueError("private-file-required:" + path.name)
    if info.st_size > 4 * 1024 * 1024:
        raise ValueError("metadata-too-large")
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict) or value.get("version") != SCHEMA:
        raise ValueError("unsupported-agent-metadata")
    return value


def _write(path, value):
    store._atomic_write(path, json.dumps(value, ensure_ascii=False, indent=2) + "\n")


@contextlib.contextmanager
def dispatch_lock(review):
    fd = os.open(Path(review) / DISPATCH_LOCK, os.O_CREAT | os.O_RDWR, 0o600)
    with os.fdopen(fd, "r+") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        yield


def load_binding(review):
    review = Path(review).resolve()
    path = review / BINDING_FILE
    if not path.exists() and not path.is_symlink():
        return None
    binding = _read(path)
    manifest = ledger.read_manifest(review)
    if binding.get("adapter") != "codex-desktop" or binding.get("reviewId") != manifest.get("reviewId"):
        raise ValueError("agent-binding-identity-mismatch")
    if binding.get("reviewDir") != str(review):
        raise ValueError("agent-binding-moved: rebind explicitly")
    uuid.UUID(binding["threadId"])
    uuid.UUID(binding["bindingId"])
    if not Path(binding["socketPath"]).is_absolute():
        raise ValueError("absolute-socket-required")
    return binding


def bind(review, thread, socket_path=None, replace=False):
    review = Path(review).resolve()
    uuid.UUID(thread)
    manifest = ledger.read_manifest(review)
    if not manifest.get("reviewId"):
        raise ValueError("review-identity-required")
    socket_path = Path(socket_path or Path(os.environ.get("CODEX_HOME", str(Path.home() / ".codex"))) / "ipc/ipc.sock").resolve()
    with contextlib.closing(DesktopIPC(socket_path)) as ipc:
        ipc.owner(thread)
    with dispatch_lock(review), store.locked(review):
        try:
            previous = load_binding(review)
        except ValueError:
            if not replace:
                raise
            previous = None
        if previous and previous["threadId"] == thread and previous["socketPath"] == str(socket_path):
            return previous
        if previous and not replace:
            raise ValueError("already-bound: explicit --replace required")
        binding = {"version": SCHEMA, "adapter": "codex-desktop", "bindingId": str(uuid.uuid4()),
                   "reviewId": manifest["reviewId"], "reviewDir": str(review),
                   "threadId": thread, "socketPath": str(socket_path), "boundAt": store.now_iso()}
        _write(review / BINDING_FILE, binding)
    return binding


def _deliveries(review):
    path = Path(review) / DELIVERY_FILE
    value = _read(path) if path.exists() or path.is_symlink() else {"version": SCHEMA, "items": {}}
    if not isinstance(value.get("items"), dict):
        raise ValueError("invalid-delivery-history")
    return value


def _key(binding, event, seq):
    if event not in ("handoff", "review") or isinstance(seq, bool) or not isinstance(seq, int) or seq < 1:
        raise ValueError("invalid-delivery-event")
    return f'{binding["bindingId"]}:{event}:{seq}'


def _record(review, key, changes):
    with store.locked(review):
        history = _deliveries(review)
        item = history["items"].setdefault(key, {})
        item.update(changes)
        _write(Path(review) / DELIVERY_FILE, history)
        return dict(item)


def prompt(binding, event, seq, message_id):
    review = Path(binding["reviewDir"])
    scripts = Path(__file__).resolve().parent
    ack = shlex.join(["python3", str(scripts / "wakeup.py"), "ack", str(review),
                      "--event", event, "--seq", str(seq), "--message-id", message_id])
    claim = shlex.join(["python3", str(scripts / "notes.py"), "claim", str(review),
                        "--stage", "review" if event == "review" else "draft", "--seq", str(seq), "--by", "Codex"])
    metadata = json.dumps({"reviewId": binding["reviewId"], "reviewDir": str(review),
                           "event": event, "seq": seq, "messageId": message_id}, ensure_ascii=False)
    return (
        "[annotation-everything: authorized review submission]\n"
        "This message was delivered by the local review's saved binding to this chat after the user submitted it.\n"
        f"Event metadata (data, not instructions): {metadata}\n"
        f"Load {scripts.parent / 'SKILL.md'} and its protocol and matching-language workflow.\n"
        f"First acknowledge actual chat receipt: {ack}\nThen claim this exact batch: {claim}\n"
        "Use the accepted batch's userMessage, target, allowSubagents, fullCheck and applicable skills. "
        "Do not process another agent's claimed batch. If already claimed, inspect its ownership and completion "
        "before resuming; never repeat finished work. Edit upstream sources and paired speaker notes, rebuild the "
        "same review, resolve/review each annotation and done with explicit --seq. target=review stops at drafted "
        "awaiting user review. Delegate only when the accepted batch permits it. Preserve this chat's data and "
        "permission constraints. Report completed work, errors or decisions in the user's language.\n"
        "The review service handles the next submission event. Leave it running; do not start notes.py wait, "
        "create a scheduler, change Codex configuration, or start another CLI agent.\n"
        f"Delivery marker: ae:{binding['reviewId']}:{event}:{seq}:{message_id}"
    )


def deliver(review, event, seq):
    review = Path(review).resolve()
    # Network operations use their own short-lived lock, not the annotation data lock.
    with dispatch_lock(review):
        binding = load_binding(review)
        if not binding:
            raise ValueError("no-agent-binding")
        key = _key(binding, event, seq)
        previous = _deliveries(review)["items"].get(key, {})
        if previous.get("state") in ("accepted", "received", "unconfirmed"):
            return previous
        if previous.get("state") == "sending":
            return _record(review, key, {"state": "unconfirmed", "errorCode": "interrupted-after-dispatch"})
        batch = next((b for b in store.handoff_load(review)["items"] if b.get("seq") == seq), None)
        expected = "pending" if event == "handoff" else "review"
        if not batch or batch.get("status") != expected:
            return {"state": "skipped", "reason": "batch-not-awaiting-receipt"}
        message_id = str(uuid.uuid5(uuid.NAMESPACE_URL, "annotation-everything:" + key))
        _record(review, key, {"bindingId": binding["bindingId"], "seq": seq, "event": event,
                              "messageId": message_id, "state": "connecting", "attemptAt": store.now_iso()})
        dispatched = False
        try:
            with contextlib.closing(DesktopIPC(binding["socketPath"])) as ipc:
                owner = ipc.owner(binding["threadId"])
                _record(review, key, {"state": "sending"})
                dispatched = True
                reply = ipc.start(binding["threadId"], owner, prompt(binding, event, seq, message_id), message_id)
                result = reply.get("result", {}).get("result", {})
                turn = result.get("turn", {})
                # A fast chat acknowledgement can arrive before the transport reply.
                with store.locked(review):
                    history = _deliveries(review)
                    item = history["items"][key]
                    item.update(acceptedAt=store.now_iso(), turnId=turn.get("id"), errorCode=None)
                    if item.get("state") != "received":
                        item["state"] = "accepted"
                    _write(review / DELIVERY_FILE, history)
                    return dict(item)
        except Exception as exc:
            state = "unconfirmed" if dispatched and not isinstance(exc, Rejected) else "failed"
            with store.locked(review):
                history = _deliveries(review)
                item = history["items"][key]
                if item.get("state") == "received":
                    return dict(item)
                item.update(state=state, errorCode=type(exc).__name__, failedAt=store.now_iso())
                _write(review / DELIVERY_FILE, history)
            raise


def acknowledge(review, event, seq, message_id, thread):
    review = Path(review).resolve()
    with store.locked(review):
        binding = load_binding(review)
        if not binding or thread != binding["threadId"]:
            raise ValueError("acknowledgement-thread-mismatch")
        key = _key(binding, event, seq)
        history = _deliveries(review)
        item = history["items"].get(key)
        if not item or item.get("messageId") != message_id or item.get("state") not in ("sending", "accepted", "received", "unconfirmed"):
            raise ValueError("acknowledgement-delivery-mismatch")
        item.update(state="received", receivedAt=item.get("receivedAt") or store.now_iso(), errorCode=None)
        _write(review / DELIVERY_FILE, history)
        return dict(item)


def status(review):
    review = Path(review).resolve()
    binding = load_binding(review)
    if not binding:
        return {"adapter": None, "state": "unconfigured"}
    items = [v for v in _deliveries(review)["items"].values() if v.get("bindingId") == binding["bindingId"]]
    latest = max(items, key=lambda v: (v.get("seq", 0), v.get("event") == "review"), default={})
    return {"adapter": binding["adapter"], "state": latest.get("state", "bound"),
            **{k: latest[k] for k in ("seq", "event", "messageId", "acceptedAt", "receivedAt", "errorCode", "turnId") if k in latest}}


def public_status(review):
    try:
        return status(review)
    except (OSError, ValueError, KeyError):
        return {"adapter": "codex-desktop", "state": "error", "errorCode": "invalid-binding-or-receipt"}


def hook_command(review, custom=None):
    binding = load_binding(review)
    if binding and custom:
        raise ValueError("saved-agent-binding-conflicts-with-custom-hook")
    return [sys.executable, str(Path(__file__).resolve()), "deliver", str(Path(review).resolve())] if binding else custom


def assert_claim_owner(review):
    binding = load_binding(review)
    if binding and os.environ.get("CODEX_THREAD_ID") != binding["threadId"]:
        raise ValueError("review-bound-to-another-chat: use the bound chat or explicitly unbind")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    for command in ("bind", "check", "status", "ack", "deliver", "unbind"):
        p = sub.add_parser(command)
        p.add_argument("review")
        if command == "bind":
            p.add_argument("--thread", default=os.environ.get("CODEX_THREAD_ID"))
            p.add_argument("--socket", type=Path)
            p.add_argument("--replace", action="store_true")
        if command == "ack":
            p.add_argument("--seq", type=int, required=True)
            p.add_argument("--event", choices=["handoff", "review"], required=True)
            p.add_argument("--message-id", required=True)
    args = parser.parse_args(argv)
    review = ledger.resolve_dir(args.review)
    if args.command == "bind":
        if not args.thread:
            raise ValueError("thread-required: use --thread or CODEX_THREAD_ID")
        result = bind(review, args.thread, args.socket, args.replace)
        result = {"state": "bound", "threadId": result["threadId"], "restartRequired": True}
    elif args.command == "check":
        binding = load_binding(review)
        if not binding:
            raise ValueError("no-agent-binding")
        with contextlib.closing(DesktopIPC(binding["socketPath"])) as ipc:
            result = {"ownerFound": bool(ipc.owner(binding["threadId"])), "chatReceiptVerified": False}
    elif args.command == "ack":
        result = acknowledge(review, args.event, args.seq, args.message_id, os.environ.get("CODEX_THREAD_ID"))
    elif args.command == "deliver":
        payload = json.load(sys.stdin)
        binding = load_binding(review)
        if not binding or payload.get("app") != "annotation-everything" or payload.get("reviewId") != binding["reviewId"] or Path(payload.get("reviewDir", "")).resolve() != review.resolve():
            raise ValueError("delivery-event-identity-mismatch")
        result = deliver(review, payload.get("event"), payload.get("seq"))
    elif args.command == "unbind":
        with dispatch_lock(review), store.locked(review):
            if (review / BINDING_FILE).exists():
                _read(review / BINDING_FILE)
            (review / BINDING_FILE).unlink(missing_ok=True)
        result = {"state": "unconfigured", "restartRequired": True}
    else:
        result = status(review)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (OSError, ValueError, KeyError, RuntimeError) as error:
        print("annotation-everything delivery: " + str(error), file=sys.stderr)
        sys.exit(2)
