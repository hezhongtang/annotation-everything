"""Persistent, review-scoped stop coordination."""
from __future__ import annotations

import contextlib
import json
import os
from pathlib import Path
import stat
import time
import uuid

import fsutil
import ledger
import notes_store as store
import wakeup

STATE_FILE = ".annotation-stop.json"
LOCK_FILE = ".annotation-stop.lock"
OPERATION_LOCK = ".annotation-stop-operation.lock"
SCHEMA = 1
COMPONENTS = ("gate", "save", "waiters", "hooks", "agent", "http")


class OperationBusy(Exception):
    """Another process is coordinating this review's start or stop."""


@contextlib.contextmanager
def operation(review, *, deadline=None):
    # Keep the state lock short so queries and hook registration can proceed during a stop.
    with contextlib.ExitStack() as stack:
        try:
            stack.enter_context(fsutil.flocked(Path(review) / OPERATION_LOCK, exclusive=True,
                                               nofollow=True, deadline=deadline))
        except BlockingIOError:
            raise OperationBusy from None
        yield


def remaining(deadline):
    return max(0.0, deadline - time.monotonic())


def _now():
    return store.now_iso()


def _initial(review, manifest=None):
    manifest = manifest if isinstance(manifest, dict) else ledger.read_manifest(review)
    rid = manifest.get("reviewId")
    if not isinstance(rid, str) or not rid:
        raise ValueError("review-identity-required")
    return {"version": SCHEMA, "reviewId": rid, "state": "active", "stopId": None,
            "components": {name: {"state": "active"} for name in COMPONENTS}}


def _read(review, manifest=None):
    path = Path(review) / STATE_FILE
    if not path.exists() and not path.is_symlink():
        return _initial(review, manifest)
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise ValueError("private-stop-state-required")
    if info.st_size > 1024 * 1024:
        raise ValueError("stop-state-too-large")
    value = json.loads(path.read_text(encoding="utf-8"))
    initial = _initial(review, manifest)
    if not isinstance(value, dict) or value.get("version") != SCHEMA \
            or value.get("reviewId") != initial["reviewId"] \
            or value.get("state") not in ("active", "stopping", "stopped", "incomplete") \
            or not isinstance(value.get("components"), dict):
        raise ValueError("invalid-stop-state")
    for name in COMPONENTS:
        component = value["components"].get(name)
        if not isinstance(component, dict) or component.get("state") not in ("active", "pending", "stopping", "stopped", "incomplete"):
            raise ValueError("invalid-stop-component")
    return value


def _write(review, value):
    value["updatedAt"] = _now()
    fsutil.atomic_write(Path(review) / STATE_FILE,
                        json.dumps(value, ensure_ascii=False, indent=2) + "\n",
                        prefix=".annotation-stop-")


def _edit(review, change):
    with fsutil.flocked(Path(review) / LOCK_FILE, exclusive=True):
        value = _read(review)
        result = change(value)
        _write(review, value)
        return result if result is not None else value


def load(review):
    with fsutil.flocked(Path(review) / LOCK_FILE, exclusive=False):
        return _read(review)


def accepts_work(review, *, manifest=None):
    path = Path(review) / STATE_FILE
    if not path.exists() and not path.is_symlink():
        return True
    return _read(review, manifest)["state"] == "active"


def assert_accepts_work(review, *, manifest=None):
    if not accepts_work(review, manifest=manifest):
        raise ValueError("review-stopped: explicit start required")


def validate_timeout(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not 0 <= value <= 60:
        raise ValueError("invalid-stop-timeout")
    return float(value)


def ready_for_http(state):
    return all(state["components"][name]["state"] == "stopped" for name in COMPONENTS if name != "http")


def begin(review, *, http=None):
    def change(value):
        if value["state"] == "stopped":
            return value
        if value["state"] == "active":
            hook_runtime = dict(value["components"]["hooks"])
            value.update(stopId=str(uuid.uuid4()), requestedAt=_now(), attempts=0)
            for name in COMPONENTS:
                value["components"][name] = {"state": "pending"}
            value["components"]["hooks"] = {**hook_runtime, "state": "pending"}
            value["components"]["gate"] = {"state": "stopped", "stoppedAt": _now()}
        value["attempts"] = int(value.get("attempts", 0)) + 1
        value["state"] = "stopping"
        value.pop("incompleteReasons", None)
        for name in COMPONENTS:
            component = value["components"][name]
            if component["state"] == "incomplete":
                component["state"] = "pending"
                component.pop("error", None)
        running = http if http is not None else value["components"]["http"]
        if running.get("state") != "stopped":
            value["components"]["http"] = {**running, "state": "stopping"}
        return value
    return _edit(review, change)


def mark(review, name, state, error=None, *, stop_id=None, **details):
    if name not in COMPONENTS or state not in ("stopping", "stopped", "incomplete"):
        raise ValueError("invalid-stop-transition")
    def change(value):
        if stop_id is not None and value["stopId"] != stop_id:
            return value
        if value["state"] == "active":
            raise ValueError("stop-not-requested")
        component = {**value["components"][name], **details, "state": state}
        if error:
            component["error"] = error
        else:
            component.pop("error", None)
        if state == "stopped":
            component["stoppedAt"] = component.get("stoppedAt") or _now()
        value["components"][name] = component
        components = value["components"]
        if any(components[item]["state"] == "incomplete" for item in COMPONENTS):
            value["state"] = "incomplete"
            value["incompleteReasons"] = {item: components[item]["error"]
                                          for item in COMPONENTS if components[item]["state"] == "incomplete"}
        elif all(components[item]["state"] == "stopped" for item in COMPONENTS):
            value["state"] = "stopped"
            value["stoppedAt"] = _now()
            value.pop("incompleteReasons", None)
        else:
            value["state"] = "stopping"
        return value
    return _edit(review, change)


def start(review):
    def change(value):
        previous = value.get("stopId")
        hook_runtime = dict(value["components"]["hooks"])
        value.update(state="active", stopId=None, previousStopId=previous, startedAt=_now())
        value.pop("requestedAt", None)
        value.pop("stoppedAt", None)
        value.pop("incompleteReasons", None)
        value["components"] = {name: {"state": "active"} for name in COMPONENTS}
        value["components"]["hooks"] = {**hook_runtime, "state": "active"}
        return value
    return _edit(review, change)


def configure_hooks(review, configured):
    def change(value):
        component = value["components"]["hooks"]
        component.update(configured=bool(configured), tracked=True)
        component.setdefault("workers", [])
        return value
    return _edit(review, change)


def hook_started(review, pid, seq, event):
    if not ledger.read_manifest(review).get("reviewId"):
        return
    def change(value):
        component = value["components"]["hooks"]
        component.update(configured=True, tracked=True)
        workers = [worker for worker in component.get("workers", []) if worker.get("pid") != pid]
        workers.append({"pid": pid, "seq": seq, "event": event})
        component["workers"] = workers
        return value
    return _edit(review, change)


def hook_finished(review, pid):
    if not ledger.read_manifest(review).get("reviewId"):
        return
    def change(value):
        component = value["components"]["hooks"]
        component["workers"] = [worker for worker in component.get("workers", [])
                                if worker.get("pid") != pid]
        return value
    return _edit(review, change)


def _live_waiters(review):
    live = []
    for path in Path(review).glob(ledger.WAIT_PREFIX + "*.json"):
        try:
            wait_mark = json.loads(path.read_text(encoding="utf-8"))
            pid = wait_mark.get("pid")
            if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0 \
                    or path.name != ledger.wait_file(Path(review), pid).name:
                continue
            if ledger.pid_alive(pid):
                live.append(pid)
            else:
                path.unlink(missing_ok=True)
        except (OSError, ValueError, TypeError):
            continue
    return sorted(set(live))


def _protect_save(review, deadline):
    if load(review)["components"]["save"]["state"] == "stopped":
        return
    mark(review, "save", "stopping")
    try:
        with fsutil.flocked(Path(review) / store.LOCK_FILE, exclusive=True, deadline=deadline):
            mark(review, "save", "stopped", protectedAt=_now())
    except BlockingIOError:
        mark(review, "save", "incomplete", "save-protection-timeout")
    except OSError as error:
        mark(review, "save", "incomplete", "save-protection-unavailable", detail=type(error).__name__)


def _coordinate_hooks(review, server, deadline):
    component = load(review)["components"]["hooks"]
    if component["state"] == "stopped":
        return
    if component.get("configured") and not component.get("tracked"):
        mark(review, "hooks", "incomplete", "background-hooks-unconfirmed")
        return
    if server is not None and not server.wait_for_hooks(remaining(deadline)):
        mark(review, "hooks", "incomplete", "background-hook-timeout")
        return
    while True:
        workers = load(review)["components"]["hooks"].get("workers", [])
        pids = sorted({worker.get("pid") for worker in workers
                       if type(worker.get("pid")) is int and worker["pid"] > 0 and ledger.pid_alive(worker["pid"])})
        if not pids:
            mark(review, "hooks", "stopped", pids=[], workers=[])
            return
        left = remaining(deadline)
        if not left:
            mark(review, "hooks", "incomplete", "background-hook-running", pids=pids)
            return
        mark(review, "hooks", "stopping", pids=pids)
        time.sleep(min(.05, left))


def _coordinate_waiters(review, deadline):
    if load(review)["components"]["waiters"]["state"] == "stopped":
        return
    while True:
        waiters = _live_waiters(review)
        if not waiters:
            mark(review, "waiters", "stopped", pids=[])
            return
        left = remaining(deadline)
        if not left:
            mark(review, "waiters", "incomplete", "waiter-exit-timeout", pids=waiters)
            return
        mark(review, "waiters", "stopping", pids=waiters)
        time.sleep(min(.05, left))


def _coordinate_agent(review, deadline):
    if load(review)["components"]["agent"]["state"] == "stopped":
        return
    try:
        with wakeup.dispatch_lock(review, deadline=deadline):
            result = wakeup.cancel_review(review, deadline=deadline)
    except BlockingIOError:
        result = {"state": "incomplete", "error": "agent-dispatch-timeout"}
    except Exception as error:
        result = {"state": "incomplete", "error": "agent-cancel-unconfirmed",
                  "detail": type(error).__name__}
    mark(review, "agent", result["state"], result.get("error"),
         **{k: v for k, v in result.items() if k not in ("state", "error")})


def coordinate(review, *, deadline, server=None):
    """Drain admitted work while the caller holds the review's operation lock."""
    state = load(review)
    if state["state"] == "stopped":
        return state
    if server is not None and not server.wait_for_writes(remaining(deadline)):
        mark(review, "save", "incomplete", "save-protection-timeout")
    else:
        _protect_save(review, deadline)
    save_protected = load(review)["components"]["save"]["state"] == "stopped"
    if save_protected:
        _coordinate_waiters(review, deadline)
    _coordinate_hooks(review, server, deadline)
    if save_protected:
        _coordinate_agent(review, deadline)
    return load(review)


def http_stopped(review, stop_id):
    return mark(review, "http", "stopped", stop_id=stop_id)
