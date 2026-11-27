"""Persist the selected Word suggestions, one-use write permissions and native readbacks."""

from __future__ import annotations

from collections import Counter
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
    doc_key = binding.canonical_document_key
    require(journal["document"] == doc_key and journal["reviewId"] == binding.rid, "revision-document-changed")
    for op in journal["operations"]:
        plan = op["plan"]
        # Earlier plans stored the form of the path Word happened to report when the permit was issued.
        if plan and plan["documentKey"] in binding.document_keys:
            plan["documentKey"] = doc_key
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
        and set(plan) - {"writer"}
        == {"documentKey", "reviewId", "paragraphKey", "id", "start", "end", "before", "expected"}
        and (
            "writer" not in plan
            or isinstance(plan["writer"], str)
            and plan["writer"].strip()
            and len(plan["writer"]) <= 512
        )
        and (plan["documentKey"] == binding.canonical_document_key or plan["documentKey"] in binding.document_keys)
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


def matches_first_receipt(plan, after, owns_permit):
    """A live writer can attest its own sync. A later observer also needs the author recorded before that write."""
    before = plan["before"]
    writer = plan.get("writer")
    if not (owns_permit or writer) or before["original"] != after["original"] or after["current"] != plan["expected"]:
        return False
    current = before["current"].encode("utf-16-le")
    rejected = after["current"].encode("utf-16-le")
    delta = (len(rejected) - len(current)) // 2

    def signature(r, start):
        return (r["type"], r["author"], r["text"], r["date"], start)

    # Existing runs after the target move by the UTF-16 length of the edit.
    old = Counter(signature(r, r["start"] + (delta if r["start"] >= plan["end"] else 0)) for r in before["revisions"])
    fresh = []
    for r in after["revisions"]:
        key = signature(r, r["start"])
        if old[key]:
            old[key] -= 1
        else:
            fresh.append(r)
    authors = {r["author"] for r in fresh}
    if any(old.values()) or len(authors) != 1 or "" in authors or (writer and writer not in authors):
        return False
    limit = 2 * (plan["end"] + delta)
    for r in reversed(fresh):
        at, text = 2 * r["start"], r["text"].encode("utf-16-le")
        if not text or not 2 * plan["start"] <= at <= limit:
            return False
        if r["type"] == "Deleted":
            rejected = rejected[:at] + text + rejected[at:]
        elif at + len(text) <= limit and rejected[at : at + len(text)] == text:
            rejected = rejected[:at] + rejected[at + len(text) :]
        else:
            return False
    return rejected == current


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
            require(all(not suggestions[i].get("writeRestriction") for i in ids), "revision-write-restricted")
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
                item = next((s for s in binding.suggestions() if s["id"] == op["id"]), None)
                require(item is not None, "revision-suggestion-changed")
                require(not item.get("writeRestriction"), "revision-write-restricted")
                require(op["item"] == item, "revision-suggestion-changed")
                validate_plan(binding, op["item"], body["plan"])
                permit = secrets.token_hex(16)
                op.update(
                    plan={**body["plan"], "documentKey": binding.canonical_document_key},
                    status="unknown",
                    sequence=1 + max((entry.get("sequence", 0) for entry in ops), default=0),
                    permitHash=hashlib.sha256(permit.encode()).hexdigest(),
                )
                result = {"mayWrite": True, "permit": permit}
            elif path == PREFIX + "observe":
                require(op["plan"] is not None, "revision-not-started")
                status = body["status"]
                owns_permit = secrets.compare_digest(
                    op.get("permitHash", ""), hashlib.sha256(body["permit"].encode()).hexdigest()
                )
                if status == "unexecuted":
                    # Only the owner that knows it never invoked a native write can release this attempt.
                    require(
                        op["status"] == "unknown" and op["receipt"] is None and owns_permit,
                        "revision-permission-spent",
                    )
                    op.pop("permitHash", None)
                else:
                    require(status in ("unknown", "written") and op["status"] != "unexecuted", "bad-revision-status")
                    if status == "written":
                        after = body["after"]
                        require(
                            valid_snapshot(after)
                            and op["plan"]["documentKey"] == binding.canonical_document_key
                            and (
                                op["receipt"] == after
                                or (op["receipt"] is None and matches_first_receipt(op["plan"], after, owns_permit))
                            ),
                            "wrong-revision-receipt",
                        )
                        op["receipt"] = after
                    if status == "written" or owns_permit:
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
            "document": binding.canonical_document_key,
            "operations": ops,
        }
        fsutil.atomic_write(binding.review / FILE, json.dumps(journal, ensure_ascii=False), prefix=".word-revisions-")
        return result
