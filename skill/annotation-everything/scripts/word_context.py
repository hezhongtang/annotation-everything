"""Exchange explicitly selected Word material through the review's bound receiver."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import re
import sys

import notes_store as store
import review_stop
import wakeup

API = "/api/word/"
REQUEST_FIELDS = {
    API + "submit": {"submitId", "paragraphs"},
    API + "list": set(),
    API + "withdraw": {"id"},
    API + "recall": {"batch"},
}
MAX_BYTES = 256 * 1024


class ContextError(ValueError):
    pass


def require(ok, message="invalid-word-context"):
    if not ok:
        raise ContextError(message)


def keys(value, fields):
    return isinstance(value, dict) and set(value) == set(fields.split())


def string(value, limit=20_000):
    return isinstance(value, str) and len(value) <= limit


def validate_threads(threads):
    require(isinstance(threads, list) and len(threads) <= 100)
    for thread in threads:
        require(keys(thread, "id authorName creationDate content range resolved replies"))
        require(type(thread["resolved"]) is bool and keys(thread["range"], "text") and string(thread["range"]["text"]))
        require(isinstance(thread["replies"], list) and len(thread["replies"]) <= 100)
        for entry in [thread, *thread["replies"]]:
            require(
                string(entry["id"], 512)
                and bool(entry["id"])
                and string(entry["authorName"], 512)
                and (entry["creationDate"] is None or string(entry["creationDate"], 128))
                and string(entry["content"])
            )
        require(all(keys(reply, "id authorName creationDate content") for reply in thread["replies"]))


def validate_paragraphs(paragraphs):
    require(isinstance(paragraphs, list) and 1 <= len(paragraphs) <= 32)
    seen = set()
    for p in paragraphs:
        require(keys(p, "n digest original current revisions threads"))
        require(type(p["n"]) is int and p["n"] >= 0 and p["n"] not in seen)
        seen.add(p["n"])
        require(string(p["digest"], 64) and re.fullmatch("[0-9a-f]{64}", p["digest"]))
        require(string(p["original"]) and string(p["current"]))
        require(isinstance(p["revisions"], list) and len(p["revisions"]) <= 200)
        for change in p["revisions"]:
            require(keys(change, "type author date text verified"))
            require(all(string(change[k], 512) for k in ("type", "author", "date")))
            require(string(change["text"]) and type(change["verified"]) is bool)
            require(change["verified"] or not change["text"])
        validate_threads(p["threads"])
        require(p["original"] or p["current"] or p["revisions"] or p["threads"])


def submit(review, receiver_id, fingerprint, paragraphs, submit_id, authorize):
    require(receiver_id and string(submit_id, 64) and re.fullmatch(r"[A-Za-z0-9_-]{1,64}", submit_id))
    validate_paragraphs(paragraphs)
    snap = {"bindingId": receiver_id, "fingerprint": fingerprint, "paragraphs": paragraphs}
    _, item, _ = store.handoff_submit(
        review,
        "请根据所选 Word 原文、改后文字及讨论返回建议。保留需核对的修订。",
        by="word",
        authorize=authorize,
        submit_id=submit_id,
        word_submission=snap,
    )
    return item["seq"]


def find_batch(doc, seq):
    item = next((b for b in doc["items"] if b["seq"] == seq and "wordSubmission" in b), None)
    require(item, "no-word-batch")
    return item


def return_suggestions(review, seq, suggestions, harness=None, session=None):
    require(type(seq) is int and isinstance(suggestions, list) and len(suggestions) <= 100, "invalid-word-suggestions")
    with store.locked(review):
        review_stop.assert_accepts_work(review)
        doc = store.handoff_load(review)
        batch = find_batch(doc, seq)
        snap = batch["wordSubmission"]
        wakeup.assert_claim_owner(review, harness, session, expected_binding=snap["bindingId"])
        if batch.get("wordReceipt") == suggestions:
            return batch["wordSuggestions"]
        require(batch["status"] == "claimed" and "wordReceipt" not in batch, "word-batch-not-claimed")
        selected = {p["n"]: p for p in snap["paragraphs"]}
        entries = []
        for i, entry in enumerate(suggestions):
            require(keys(entry, "n from to reason") or keys(entry, "n from to reason edge"), "invalid-word-suggestion")
            require(type(entry["n"]) is int and entry["n"] in selected)
            p = selected[entry["n"]]
            require(all(string(entry[k]) for k in ("from", "to", "reason")) and entry["reason"].strip())
            require(entry["from"] != entry["to"])
            if entry["from"]:
                require(entry["from"] in p["current"] and "edge" not in entry, "outside-word-selection")
            else:
                require(entry.get("edge") in ("Start", "End") and p["current"], "word-insertion-needs-anchor")
            entries.append(
                {
                    **entry,
                    "id": f"word-{seq}-{i + 1}",
                    "sourceBatch": seq,
                    "status": "pending",
                    "digest": p["digest"],
                    "fp": snap["fingerprint"],
                    "selection": p["current"],
                }
            )
        batch.update(wordSuggestions=entries, wordReceipt=suggestions)
        store._finish(batch, None, doc["items"], by=wakeup.caller_identity(harness, session)["harness"], summary="")
        store._handoff_commit(review, doc, {"op": "word-return", "seq": seq, "count": len(entries)})
        return entries


def withdraw(review, item_id, authorize):
    with store.locked(review):
        authorize()
        doc = store.handoff_load(review)
        for batch in doc["items"]:
            for item in batch.get("wordSuggestions", []):
                if item["id"] == item_id:
                    if item["status"] != "withdrawn":
                        item["status"] = "withdrawn"
                        store._handoff_commit(review, doc, {"op": "word-withdraw", "seq": batch["seq"], "id": item_id})
                    return
        raise ContextError("no-word-suggestion")


def recall(review, seq, authorize):
    require(type(seq) is int, "no-word-batch")
    with store.locked(review):
        authorize()
        doc = store.handoff_load(review)
        batch = find_batch(doc, seq)
        if batch["status"] == "void":
            return
        require(batch["status"] == "pending", "word-batch-already-received")
        batch.update(status="void", withdrawnAt=store.now_iso())
        store._handoff_commit(review, doc, {"op": "word-recall", "seq": seq})


def suggestions(binding):
    result = []
    for batch in store.handoff_load(binding.review)["items"]:
        for item in batch.get("wordSuggestions", []):
            if item["status"] == "withdrawn":
                continue
            n = item["n"]
            location = binding.locations.get(n)
            paragraph = binding.paragraphs[n].text if location else ""
            valid = (
                location
                and item["fp"] == binding.fingerprint
                and hashlib.sha256(paragraph.encode()).hexdigest() == item["digest"]
                and paragraph.count(item["from"] or item["selection"]) == 1
            )
            result.append(
                {
                    **item,
                    **(location or {}),
                    "paragraph": paragraph,
                    "status": item["status"] if valid else "needs-relocation",
                }
            )
    return result


def handle(binding, path, body):
    if path == API + "submit":
        seq = submit(
            binding.review,
            binding.receiver_id,
            binding.fingerprint,
            body["paragraphs"],
            body["submitId"],
            binding.authorize_receiver,
        )
        active = wakeup.load_binding(binding.review)
        delivery = "waiting"
        if active and active["adapter"] == "codex-desktop":
            try:
                delivery = wakeup.deliver(binding.review, "handoff", seq, expected_binding=binding.receiver_id)["state"]
            except (ValueError, OSError):
                delivery = "unconfirmed"
        return {"batch": seq, "delivery": delivery}
    if path == API + "recall":
        recall(binding.review, body["batch"], binding.authorize_receiver)
    if path == API + "withdraw":
        withdraw(binding.review, body["id"], binding.authorize_receiver)
    return {
        "suggestions": binding.suggestions(),
        "pendingBatches": [
            b["seq"]
            for b in store.handoff_load(binding.review)["items"]
            if b["status"] == "pending" and "wordSubmission" in b
        ],
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["return"])
    parser.add_argument("review", type=Path)
    parser.add_argument("--batch", type=int, required=True)
    parser.add_argument(
        "--input", type=Path, required=True, help="JSON array of {n, from, to, reason}; insertion adds edge"
    )
    parser.add_argument("--harness")
    parser.add_argument("--session")
    args = parser.parse_args(argv)
    try:
        with args.input.open("rb") as source:
            data = source.read(MAX_BYTES + 1)
        require(len(data) <= MAX_BYTES)
        entries = return_suggestions(args.review, args.batch, json.loads(data), args.harness, args.session)
        print(json.dumps({"batch": args.batch, "suggestions": [e["id"] for e in entries]}))
        return 0
    except (ValueError, OSError, TypeError, KeyError, RecursionError) as exc:
        print(str(exc), file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
