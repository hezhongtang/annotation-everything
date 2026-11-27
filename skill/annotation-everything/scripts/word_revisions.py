"""Persist the selected Word suggestions, one-use write permissions and native readbacks."""

from __future__ import annotations

import hashlib
import json
import secrets

import fsutil
import notes_store

PREFIX = "/api/revision-operations/"
IDENTITY = {"reviewId", "documentKey"}
REQUEST_FIELDS = {
    PREFIX + "plan": IDENTITY | {"items"},
    PREFIX + "begin": IDENTITY | {"batch", "id", "plan"},
    PREFIX + "observe": IDENTITY | {"batch", "id", "status", "after", "permit"},
}
FILE = "word-revisions.json"


class RevisionError(ValueError):
    pass


def require(ok, code):
    if not ok:
        raise RevisionError(code)


def load(review):
    try:
        journal = json.loads(fsutil.read_private_text(review / FILE, max_bytes=None))
    except FileNotFoundError:
        return None
    require(isinstance(journal, dict) and isinstance(journal.get("operations"), list), "invalid-revision-journal")
    return journal


def operations(binding):
    journal = load(binding.review)
    if journal is None:
        return []
    doc_key = hashlib.sha256(str(binding.document).encode()).hexdigest()
    require(journal["document"] == doc_key and journal["reviewId"] == binding.rid, "revision-document-changed")
    return journal["operations"]


def ensure_withdrawable(review, suggestion_id):
    # Called while holding the same review lock that grants the write permission.
    journal = load(review)
    if journal:
        require(
            all(op["status"] == "unexecuted" for op in journal["operations"] if op["id"] == suggestion_id),
            "revision-already-attempted",
        )


def valid_snapshot(value):
    return (
        isinstance(value, dict)
        and set(value) == {"original", "current", "revisions"}
        and all(isinstance(value[k], str) for k in ("original", "current"))
        and isinstance(value["revisions"], list)
        and all(
            isinstance(r, dict)
            and set(r) == {"type", "author", "text", "start", "date"}
            and r["type"] in ("Added", "Deleted")
            and all(isinstance(r[k], str) for k in ("author", "text", "date"))
            and type(r["start"]) is int
            and r["start"] >= 0
            for r in value["revisions"]
        )
    )


def validate_plan(binding, item, plan):
    location = ("cell/{table}/{row}/{column}" if "table" in item else "body/{bodyParagraph}").format_map(item)
    require(
        isinstance(plan, dict)
        and set(plan) == {"documentKey", "reviewId", "paragraphKey", "id", "start", "end", "before", "expected"}
        and plan["documentKey"] in binding.document_keys
        and plan["reviewId"] == binding.rid
        and plan["id"] == item["id"]
        and plan["paragraphKey"] == location
        and type(plan["start"]) is int
        and type(plan["end"]) is int
        and isinstance(plan["expected"], str)
        and valid_snapshot(plan["before"]),
        "bad-revision-plan",
    )
    # Office.js range offsets index UTF-16 code units, including surrogate pairs.
    current = plan["before"]["current"].encode("utf-16-le")
    start, end = 2 * plan["start"], 2 * plan["end"]
    require(
        0 <= start <= end <= len(current)
        and current[start:end] == item["from"].encode("utf-16-le")
        and current[:start] + item["to"].encode("utf-16-le") + current[end:] == plan["expected"].encode("utf-16-le"),
        "bad-revision-range",
    )


def handle(binding, path, body):
    with notes_store.locked(binding.review):
        ops = operations(binding)
        by_id = {op["id"]: op for op in ops}
        if path == PREFIX + "plan":
            ids = body["items"]
            require(
                isinstance(ids, list)
                and 0 < len(ids) <= 100
                and all(isinstance(i, str) for i in ids)
                and len(ids) == len(set(ids)),
                "bad-revision-selection",
            )
            suggestions = {s["id"]: s for s in binding.suggestions()}
            require(all(i in suggestions for i in ids), "no-suggestion")
            require(
                all(i not in by_id or by_id[i]["status"] == "unexecuted" for i in ids), "revision-already-attempted"
            )
            batch = secrets.token_hex(16)
            for i in ids:
                ops.append(dict(batch=batch, id=i, item=suggestions[i], plan=None, receipt=None, status="unexecuted"))
            result = {"batch": batch}
        else:
            op = by_id.get(body["id"])
            require(op and op["batch"] == body["batch"], "revision-batch-changed")
            if path == PREFIX + "begin":
                if op["status"] != "unexecuted":
                    return {"mayWrite": False}
                require(op["item"] in binding.suggestions(), "revision-suggestion-changed")
                validate_plan(binding, op["item"], body["plan"])
                permit = secrets.token_hex(16)
                op.update(
                    plan=body["plan"],
                    status="unknown",
                    sequence=1 + max((entry.get("sequence", 0) for entry in ops), default=0),
                    permitHash=hashlib.sha256(permit.encode()).hexdigest(),
                )
                result = {"mayWrite": True, "permit": permit}
            elif path == PREFIX + "observe":
                require(op["plan"] is not None, "revision-not-started")
                status = body["status"]
                if status == "unexecuted":
                    # Only the owner that knows it never invoked a native write can release this attempt.
                    require(
                        op["status"] == "unknown"
                        and op["receipt"] is None
                        and secrets.compare_digest(
                            op.get("permitHash", ""), hashlib.sha256(body["permit"].encode()).hexdigest()
                        ),
                        "revision-permission-spent",
                    )
                    op.pop("permitHash", None)
                else:
                    require(status in ("unknown", "written") and op["status"] != "unexecuted", "bad-revision-status")
                    if status == "written":
                        after = body["after"]
                        require(
                            valid_snapshot(after) and (op["receipt"] is None or op["receipt"] == after),
                            "wrong-revision-receipt",
                        )
                        op["receipt"] = after
                    op.pop("permitHash", None)
                    # A subsequent readback can become uncertain after a native decision or undo.
                    # Retain the first confirmed receipt so a missing revision can never reopen the permit.
                if body["after"] is not None:
                    require(valid_snapshot(body["after"]), "wrong-revision-receipt")
                op["lastRead"] = {"status": status, "at": notes_store.now_iso()}
                if status == "unknown" and body["after"] is not None:
                    op["lastRead"]["after"] = body["after"]
                op["status"] = status
                result = {}
            else:
                raise RevisionError("unknown-revision-operation")
        journal = {
            "reviewId": binding.rid,
            "document": hashlib.sha256(str(binding.document).encode()).hexdigest(),
            "operations": ops,
        }
        fsutil.atomic_write(binding.review / FILE, json.dumps(journal, ensure_ascii=False), prefix=".word-revisions-")
        return result
