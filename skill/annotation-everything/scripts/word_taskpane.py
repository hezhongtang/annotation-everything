#!/usr/bin/env python3
"""Single-review HTTPS source for the macOS Word task pane prototype (#214)."""

from __future__ import annotations

import argparse
import errno
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
import json
from pathlib import Path
import secrets
import ssl
import sys
import unicodedata
import zipfile

from build_viewer import BuildError, resolve_sources
import docx_edit
import docx_review
import ledger
import notes_store
import review_stop
import serve

PORT = 43821
ASSETS = Path(__file__).resolve().parents[1] / "word-taskpane"
RESOURCES = {
    "/index.html": ("index.html", "text/html; charset=utf-8"),
    **{f"/api/icon/{n}.png": (f"icon-{n}.png", "image/png") for n in (16, 32, 80)},
}
MAX_DOCUMENT = 16 * 1024 * 1024  # The prototype is for small, local synthetic documents.
W = docx_review.W
IRREGULAR_TABLE = {W + tag for tag in ("gridSpan", "vMerge", "hMerge", "gridBefore", "gridAfter")}


class Refused(ValueError):
    def __init__(self, status, code):
        super().__init__(code)
        self.status = status
        self.code = code


def paragraph_locations(root):
    """Only expose top-level paragraphs and single-paragraph cells of simple top-level tables."""
    body = root.find(W + "body")
    by_element = {p: n for n, p in enumerate(root.iter(W + "p"))}
    locations = {by_element[p]: {"bodyParagraph": by_element[p]} for p in body.findall(W + "p")}
    tables = body.findall(W + "tbl")
    if tables != list(body.iter(W + "tbl")):
        return {}  # Word collection indices cannot be confirmed for nested/wrapped tables.
    for t, table in enumerate(tables):
        if any(el.tag in IRREGULAR_TABLE for el in table.iter()):
            continue
        for r, row in enumerate(table.findall(W + "tr")):
            for c, cell in enumerate(row.findall(W + "tc")):
                paragraphs = cell.findall(W + "p")
                if len(paragraphs) == 1:
                    locations[by_element[paragraphs[0]]] = {"table": t, "row": r, "column": c}
    return locations


class Binding:
    """CLI-selected local identity; a request can never choose a directory, document or port."""

    def __init__(self, review, document):
        self.review = review.resolve(strict=True)
        self.document = document.resolve(strict=True)
        paths = {self.document}
        if sys.platform == "darwin" and self.document.parts[:3] in (("/", "private", "tmp"), ("/", "private", "var")):
            alias = Path("/") / self.document.relative_to("/private")
            if alias.resolve() == self.document:
                paths.add(alias)
        self.document_keys = {hashlib.sha256(unicodedata.normalize("NFC", str(p)).encode()).hexdigest() for p in paths}
        manifest = ledger.read_manifest(self.review)
        self.rid = manifest.get("reviewId")
        if not isinstance(self.rid, str) or not ledger.RID.fullmatch(self.rid):
            raise Refused(409, "invalid-review")
        self.port = (ledger.get(self.rid) or {}).get("port")
        status = serve.read_status(self.review) or {}
        self.pid = status.get("pid")
        self.started = status.get("startedAt")
        docx = manifest.get("docx")
        if not isinstance(docx, dict) or not isinstance(docx.get("fingerprint"), str):
            raise Refused(409, "invalid-review")
        self.fingerprint = docx["fingerprint"]
        self.check_live()
        data = self.verified_bytes()
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            main, xml, self.restrictions, _ = docx_edit.inspect_package(zf, allow_untracked=True)
        self.paragraphs = docx_edit.parse_document(xml)
        self.locations = paragraph_locations(docx_review.parse_xml(xml, main))

    def verified_bytes(self):
        with self.document.open("rb") as stream:
            data = stream.read(MAX_DOCUMENT + 1)
        if len(data) > MAX_DOCUMENT or hashlib.sha256(data).hexdigest() != self.fingerprint:
            raise Refused(409, "document-changed")
        return data

    def check_live(self):
        manifest = ledger.read_manifest(self.review)
        entry = ledger.get(self.rid) or {}
        status = serve.read_status(self.review) or {}
        if (
            manifest.get("reviewId") != self.rid
            or ledger.report_format(manifest) != ("docx", docx_review.FORMAT_VERSION)
            or (manifest.get("docx") or {}).get("fingerprint") != self.fingerprint
            or entry.get("dir") != str(self.review)
            or type(self.port) is not int
            or not 1024 <= self.port <= 65535
            or self.port == PORT
            or entry.get("port") != self.port
            or status.get("port") != self.port
            or type(self.pid) is not int
            or status.get("pid") != self.pid
            or not self.started
            or status.get("startedAt") != self.started
            or not review_stop.accepts_work(self.review, manifest=manifest)
        ):
            raise Refused(409, "review-unavailable")
        if self.document.suffix.lower() != ".docx" or resolve_sources(self.review, manifest)["report"] != self.document:
            raise Refused(409, "wrong-document")
        health = serve.probe(self.port, timeout=0.6)
        if (
            not serve.overview_health(health, self.rid, self.review, self.port)
            or health.get("pid") != self.pid
            or health.get("hostKind")
        ):
            raise Refused(409, "review-unavailable")
        return ledger.review_name(manifest)

    def suggestions(self):
        result = []
        for note in notes_store.load(self.review)["notes"]:
            try:
                edit = docx_edit.annotation_edit(note)
                if edit["anchor"].get("fp") != self.fingerprint or not isinstance(edit["to"], str) or not edit["to"]:
                    continue
                p, start, end = docx_edit.locate_edit(self.paragraphs, edit, self.restrictions)
            except docx_edit.EditError:
                continue
            if p.n in self.locations and p.text.count(edit["from"]) == 1 and p.text[start:end] == edit["from"]:
                result.append({k: edit[k] for k in ("id", "from", "to")} | {"paragraph": p.text} | self.locations[p.n])
        return result


class TaskpaneServer(ThreadingHTTPServer):
    def __init__(self, review, document, tls, *, port=PORT):
        self.binding = Binding(review, document)
        if port and (port in ledger.owners() or port == ledger.overview_reserved_port()):
            raise Refused(409, "port-reserved")
        self.token = secrets.token_urlsafe(32)
        super().__init__(("127.0.0.1", port), TaskpaneHandler)
        try:
            self.socket = tls.wrap_socket(self.socket, server_side=True, do_handshake_on_connect=False)
        except BaseException:
            self.server_close()
            raise


class TaskpaneHandler(BaseHTTPRequestHandler):
    timeout = 8

    def log_message(self, *_):
        pass  # Never log request targets, capability tokens or note contents.

    def send_payload(self, code, payload, content_type="application/json; charset=utf-8"):
        if not isinstance(payload, bytes):
            payload = json.dumps(payload, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "same-origin")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(payload)

    def dispatch(self):
        host = f"127.0.0.1:{self.server.server_port}"
        try:
            if self.headers.get_all("Host") != [host]:
                raise Refused(421, "bad-host")
            # Word may append its own bootstrap parameters to the manifest URL.
            resource_path = self.path.partition("?")[0]
            if self.command in ("GET", "HEAD") and resource_path in RESOURCES:
                name, mime = RESOURCES[resource_path]
                self.send_payload(200, (ASSETS / name).read_bytes(), mime)
                return
            if self.command != "POST" or self.path not in ("/api/bind", "/api/suggestion"):
                raise Refused(404, "not-found")
            if self.headers.get_all("Origin") != ["https://" + host] or self.headers.get(
                "Sec-Fetch-Site", "same-origin"
            ) not in ("same-origin", "none"):
                raise Refused(403, "bad-origin")
            auth = self.headers.get("Authorization", "")
            if not auth.isascii() or not secrets.compare_digest(auth, "Bearer " + self.server.token):
                raise Refused(403, "bad-binding")
            if self.headers.get_content_type() != "application/json":
                raise Refused(415, "bad-content-type")
            lengths = self.headers.get_all("Content-Length") or []
            if (
                self.headers.get("Transfer-Encoding") is not None
                or len(lengths) != 1
                or not lengths[0].isascii()
                or not lengths[0].isdecimal()
            ):
                raise Refused(400, "bad-length")
            if len(lengths[0]) > 5 or int(lengths[0]) > 4096:
                raise Refused(413, "too-large")
            try:
                body = json.loads(self.rfile.read(int(lengths[0])).decode())
            except (ValueError, RecursionError):
                raise Refused(400, "bad-json") from None
            fields = {"documentKey"} if self.path == "/api/bind" else {"documentKey", "reviewId", "id"}
            if not isinstance(body, dict) or set(body) != fields or not all(isinstance(v, str) for v in body.values()):
                raise Refused(400, "bad-request")
            binding = self.server.binding
            if body["documentKey"] not in binding.document_keys or body.get("reviewId", binding.rid) != binding.rid:
                raise Refused(409, "bad-binding")
            title = binding.check_live()
            binding.verified_bytes()
            suggestions = binding.suggestions()
            if self.path == "/api/bind":
                self.send_payload(200, {"reviewId": binding.rid, "title": title, "suggestions": suggestions})
            else:
                suggestion = next((s for s in suggestions if s["id"] == body["id"]), None)
                if suggestion is None:
                    raise Refused(404, "no-suggestion")
                self.send_payload(200, {"reviewId": binding.rid, "suggestion": suggestion})
        except Refused as exc:
            self.send_payload(exc.status, {"error": exc.code})
        except (OSError, ValueError, BuildError, ledger.LedgerError, TypeError, KeyError, AttributeError):
            self.send_payload(409, {"error": "review-unavailable"})

    do_GET = do_HEAD = do_POST = do_PUT = do_PATCH = do_DELETE = do_OPTIONS = dispatch


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("review", type=Path)
    parser.add_argument("--document", required=True, type=Path)
    parser.add_argument("--cert", required=True, type=Path, help="Trusted local certificate chain (PEM)")
    parser.add_argument("--key", required=True, type=Path, help="Private key (PEM)")
    args = parser.parse_args(argv)
    try:
        tls = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        tls.minimum_version = ssl.TLSVersion.TLSv1_2
        tls.load_cert_chain(args.cert, args.key)
        with TaskpaneServer(args.review, args.document, tls) as server:
            print(f"https://127.0.0.1:{PORT}/index.html  review={server.binding.rid}", flush=True)
            print(f"Binding token: {server.token}", flush=True)
            server.serve_forever()
    except KeyboardInterrupt:
        return 0
    except (OSError, ValueError, BuildError, ledger.LedgerError, zipfile.BadZipFile, TypeError, AttributeError) as exc:
        if isinstance(exc, OSError) and exc.errno == errno.EADDRINUSE:
            print(f"Fixed port {PORT} is already in use.", file=sys.stderr)
        else:
            print(f"Cannot start Word task pane: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
