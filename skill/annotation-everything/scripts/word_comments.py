"""Persist a one-use permission before Word posts to a native comment thread."""

from __future__ import annotations

import hashlib
import json
import re
import secrets

import fsutil

PREFIX = "/api/comment-operations/"
IDENTITY = {"documentKey", "reviewId"}
INTENT = {"kind", "target", "location", "bodyHash", "preview"}
REQUEST_FIELDS = {
    PREFIX + "list": IDENTITY,
    PREFIX + "begin": IDENTITY | INTENT,
    PREFIX + "confirm": IDENTITY | {"id", "threadId", "replyId", "stamp"},
}
DIGEST = re.compile(r"[0-9a-f]{64}")


class CommentError(ValueError):
    def __init__(self, status, code):
        super().__init__(code)
        self.status = status
        self.code = code


def handle(binding, path, body):
    store = binding.review / "word-comments.json"
    document = hashlib.sha256(str(binding.document).encode()).hexdigest()
    with fsutil.flocked(binding.review / ".word-comments.lock", exclusive=True, nofollow=True):
        try:
            data = json.loads(fsutil.read_private_text(store, max_bytes=None))
        except FileNotFoundError:
            data = {"reviewId": binding.rid, "document": document, "operations": []}
        if data["reviewId"] != binding.rid or data["document"] != document:
            raise CommentError(409, "comment-document-changed")
        operations = data["operations"]
        if path == PREFIX + "list":
            return {"operations": operations}
        if path == PREFIX + "confirm":
            result = confirm(operations, body)
        elif path == PREFIX + "begin":
            result = begin(operations, body)
            if not result["mayWrite"]:
                return result
        else:
            raise CommentError(404, "not-found")
        fsutil.atomic_write(store, json.dumps(data, ensure_ascii=False), prefix=".word-comments-")
        return result


def begin(operations, body):
    intent = {key: body[key] for key in INTENT}
    kind = intent["kind"]
    if (
        kind not in {"publish", "reply", "resolve", "reopen"}
        or not DIGEST.fullmatch(intent["location"])
        or not DIGEST.fullmatch(intent["bodyHash"])
        or len(intent["target"]) > 256
        or (kind == "publish") != (intent["target"] == "")
        or not 0 < len(intent["preview"]) <= 160
    ):
        raise CommentError(400, "bad-comment-intent")
    for prior in reversed(operations):
        if prior["target"] != intent["target"]:
            continue
        if kind in {"resolve", "reopen"}:
            # An explicit later state change is safe after a confirmed receipt.
            same = prior["kind"] in {"resolve", "reopen"} and prior["receipt"] is None
        else:
            same = (
                prior["kind"] == kind
                and prior["bodyHash"] == intent["bodyHash"]
                and (kind == "reply" or prior["location"] == intent["location"] or prior["receipt"] is None)
            )
        if same:
            return {"mayWrite": False, "id": prior["id"]}
    op_id = secrets.token_hex(16)
    operations.append({**intent, "id": op_id, "receipt": None})
    return {"mayWrite": True, "id": op_id}


def confirm(operations, body):
    op = next((item for item in operations if item["id"] == body["id"]), None)
    if op is None:
        raise CommentError(404, "no-comment-operation")
    receipt = {key: body[key] for key in ("threadId", "replyId", "stamp")}
    if (
        not 0 < len(receipt["threadId"]) <= 256
        or len(receipt["replyId"]) > 256
        or (op["kind"] == "reply") != bool(receipt["replyId"])
        or (op["kind"] != "publish" and receipt["threadId"] != op["target"])
        or not DIGEST.fullmatch(receipt["stamp"])
        or (op["receipt"] is not None and op["receipt"] != receipt)
    ):
        raise CommentError(409, "wrong-comment-receipt")
    op["receipt"] = receipt
    return {"id": op["id"], "receipt": receipt}
