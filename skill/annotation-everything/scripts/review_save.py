"""Save receipts from browser storage contexts before the review service exits."""
from __future__ import annotations

import json
import os
import re
import stat
import threading
import time
import uuid
from pathlib import Path

import fsutil

PAGE_ID = re.compile(r"[0-9a-f]{32}")
REGISTRY = ".annotation-save-pages.json"
MAX_PAGES = 1024


def registered_pages(directory: Path, review_id: str) -> dict[str, tuple[str, str]]:
    path = directory / REGISTRY
    try:
        info = path.lstat()
    except FileNotFoundError:
        return {}
    if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
            or info.st_mode & 0o077 or info.st_size > 256 * 1024):
        raise ValueError("invalid-save-pages")
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict) or value.get("reviewId") != review_id:
        raise ValueError("invalid-save-pages")
    pages = value.get("pages")
    if not isinstance(pages, dict) or len(pages) > MAX_PAGES:
        raise ValueError("invalid-save-pages")
    for page_id, group in pages.items():
        if (not PAGE_ID.fullmatch(page_id) or not isinstance(group, list) or len(group) != 2
                or not isinstance(group[0], str) or not isinstance(group[1], str)
                or not PAGE_ID.fullmatch(group[1])):
            raise ValueError("invalid-save-pages")
    return {page_id: tuple(group) for page_id, group in pages.items()}


class SaveConflict(Exception):
    """The receipt no longer matches an active save request for this page."""


class BrowserSaves:
    def __init__(self, directory: Path):
        self.directory = directory
        self._condition = threading.Condition()
        self._pages = None
        self._request = None
        self._answers: dict[tuple[str, str], bool] = {}
        self._last_seen: dict[str, float] = {}

    def _load(self, review_id):
        if self._pages is None:
            self._pages = registered_pages(self.directory, review_id)
        return self._pages

    def _store(self, review_id, pages):
        fsutil.atomic_write(self.directory / REGISTRY,
                            json.dumps({"reviewId": review_id, "pages": pages}) + "\n",
                            prefix=".annotation-save-pages-")
        self._pages = pages

    def update(self, review_id, origin, body):
        page_id, group = body["pageId"], (origin, body["clientId"])
        with self._condition:
            pages = self._load(review_id)
            known = pages.get(page_id)
            if known is not None and known != group:
                raise SaveConflict
            if "saved" in body:
                if (known is None or self._request is None
                        or any(body[key] != self._request[key] for key in ("stopId", "token"))):
                    raise SaveConflict
                self._last_seen[page_id] = time.monotonic()
                self._answers[group] = self._answers.get(group, True) and body["saved"]
                self._condition.notify_all()
                return None
            if body.get("closing"):
                # A page disappearing mid-request still owes a receipt. Without a
                # request, a clean close can release the registration.
                if known is not None and (self._request is None or self._answers.get(known) is True):
                    self._store(review_id, {key: value for key, value in pages.items() if key != page_id})
                    self._last_seen.pop(page_id, None)
                return None
            if known is None:
                if len(pages) >= MAX_PAGES:
                    raise ValueError("too-many-save-pages")
                self._store(review_id, {**pages, page_id: group})
            self._last_seen[page_id] = time.monotonic()
            return self._request if group not in self._answers else None

    def allows_write(self, origin, page_id, token):
        with self._condition:
            if self._request is None or token != self._request["token"]:
                return False
            group = self._pages.get(page_id)
            return group is not None and group[0] == origin and group not in self._answers

    def protect(self, review_id, stop_id, timeout):
        """An unreachable registered browser keeps the save component incomplete."""
        with self._condition:
            self._load(review_id)
            self._request = {"stopId": stop_id, "token": uuid.uuid4().hex}
            self._answers.clear()

            def answered():
                return (False in self._answers.values()
                        or set(self._pages.values()).issubset(self._answers))

            try:
                if not self._condition.wait_for(answered, timeout):
                    return "save-protection-unconfirmed"
                if False in self._answers.values():
                    return "save-protection-failed"
                # A current page confirms the shared storage for its browser and
                # origin. Older registrations in that storage can now be retired.
                recent = time.monotonic() - 5
                current = {key: group for key, group in self._pages.items()
                           if self._last_seen.get(key, 0) >= recent}
                if current != self._pages:
                    self._store(review_id, current)
                    self._last_seen = {key: self._last_seen[key] for key in current}
                return None
            finally:
                self._request = None
                self._answers.clear()
