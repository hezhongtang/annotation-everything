"""Word 原型的 HTTPS 接口：两份同名合成 DOCX 与独立审阅台账。"""

from __future__ import annotations

from contextlib import closing, redirect_stderr, redirect_stdout
import hashlib
import io
import http.client
import json
import os
from pathlib import Path
import shutil
import ssl
import subprocess
import sys
import tempfile
import threading
import unittest
import zipfile
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "skill/annotation-everything/scripts"), str(ROOT / "fixtures/docx")]
os.environ["AE_HOME"] = tempfile.mkdtemp(prefix="ae-test-home-")

import ledger
import make_word_native
import notes_store
import serve
import wakeup


@unittest.skipUnless(shutil.which("node"), "taskpane interaction test requires Node.js")
class TaskpaneInteractionTests(unittest.TestCase):
    def exercise_pane(self, *scenarios):
        import word_taskpane

        subprocess.run(
            ["node", str(ROOT / "tests/word_taskpane_interaction.cjs"), *scenarios],
            input=word_taskpane.index_page(),
            check=True,
            timeout=10,
        )

    def test_selected_body_and_table_edits_are_verified_as_word_revisions(self):
        self.exercise_pane("batch")

    def test_preflight_reports_every_conflict_before_writing_any_selection(self):
        self.exercise_pane("overlap")

    def test_reconnect_reads_a_lost_receipt_and_can_apply_only_remaining_suggestions(self):
        self.exercise_pane("reconnect")

    def test_competing_pane_cannot_mask_the_native_result_or_retry(self):
        for scenario in ("concurrent-written", "concurrent-unknown"):
            with self.subTest(scenario=scenario):
                self.exercise_pane(scenario)

    def test_rebind_and_repreview_retry_only_the_approved_selection(self):
        self.exercise_pane("resubmit")

    def test_undo_in_word_marks_the_attempt_uncertain_and_blocks_another_write(self):
        self.exercise_pane("undo")

    def test_manual_edit_between_items_interrupts_the_batch_and_preserves_remaining_suggestions(self):
        self.exercise_pane("manual")

    def test_overlapping_or_unreadable_threads_keep_suggestions_pending_and_separate_ranges_can_be_edited(self):
        self.exercise_pane()


@unittest.skipUnless(shutil.which("openssl"), "HTTPS test certificate requires openssl")
class TaskpaneTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        temp = tempfile.TemporaryDirectory()
        cls.addClassCleanup(temp.cleanup)
        cert, key = (Path(temp.name) / name for name in ("cert.pem", "key.pem"))
        config = Path(temp.name) / "openssl.cnf"
        config.write_text(
            "[req]\nprompt=no\ndistinguished_name=dn\nx509_extensions=server\n[dn]\nCN=127.0.0.1\n"
            "[server]\nbasicConstraints=critical,CA:FALSE\nkeyUsage=critical,digitalSignature,keyEncipherment\n"
            "extendedKeyUsage=serverAuth\nsubjectAltName=IP:127.0.0.1\n",
            encoding="ascii",
        )
        subprocess.run(
            [
                "openssl",
                "req",
                "-x509",
                "-newkey",
                "rsa:2048",
                "-nodes",
                "-days",
                "1",
                "-config",
                str(config),
                "-out",
                str(cert),
                "-keyout",
                str(key),
            ],
            capture_output=True,
            check=True,
            timeout=10,
        )
        cls.cert, cls.private_key = cert, key
        cls.tls = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        cls.tls.load_cert_chain(cert, key)
        cls.client_tls = ssl.create_default_context(cafile=str(cert))

    def setUp(self):
        import word_taskpane

        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve()
        env = patch.dict(os.environ, {"AE_HOME": str(self.root / "ledger")})
        env.start()
        self.addCleanup(env.stop)
        self.review, self.file, self.rid, self.nid = self.review_fixture("a")
        self.other, self.other_file, self.other_id, self.other_note = self.review_fixture("b")
        self.server = word_taskpane.TaskpaneServer(self.review, self.file, self.tls, port=0)
        self.start(self.server)

    def start(self, server):
        thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)

    def review_fixture(self, letter, session=None):
        label = letter.upper()
        file = make_word_native.build(self.root / label / "contract.docx", label)
        review = make_word_native.prepare_review(file, label)
        rid = ledger.read_manifest(review)["reviewId"]
        nid = notes_store.load(review)["notes"][0]["id"]
        if session:
            wakeup.bind_wait(review, "Codex", session)
        server = serve.ReviewServer(review, 0, log_to_file=False)
        ledger.claim_port(rid, review, server.port)
        serve.write_status(server)
        self.start(server)
        return review, file, rid, nid

    def request(self, path, body, *, token=None, headers=None, method="POST"):
        port = self.server.server_port
        connection = http.client.HTTPSConnection("127.0.0.1", port, context=self.client_tls, timeout=3)
        self.addCleanup(connection.close)
        request_headers = {
            "Host": f"127.0.0.1:{port}",
            "Origin": f"https://127.0.0.1:{port}",
            "Content-Type": "application/json",
            "Authorization": "Bearer " + (self.server.token if token is None else token),
        }
        connection.request(method, path, body=json.dumps(body).encode(), headers={**request_headers, **(headers or {})})
        response = connection.getresponse()
        self.assertIsNone(response.getheader("Access-Control-Allow-Origin"))
        return response.status, json.loads(response.read())

    def key(self, path):
        return hashlib.sha256(str(path).encode()).hexdigest()

    def test_client_requires_trust_for_the_loopback_certificate(self):
        port = self.server.server_port
        client_tls = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        with self.assertRaises(ssl.SSLCertVerificationError):
            with closing(http.client.HTTPSConnection("127.0.0.1", port, context=client_tls, timeout=3)) as client:
                client.request("GET", "/index.html")
        client_tls.load_verify_locations(cafile=self.cert)
        with closing(http.client.HTTPSConnection("127.0.0.1", port, context=client_tls, timeout=3)) as client:
            client.request("GET", "/index.html")
            self.assertEqual(client.getresponse().status, 200)

    def test_open_word_failure_identifies_word_and_preserves_the_browser_review(self):
        import word_taskpane

        command = [
            str(self.review),
            "--document",
            str(self.file),
            "--cert",
            str(self.cert),
            "--key",
            str(self.private_key),
            "--open-word",
        ]
        errors, output = io.StringIO(), io.StringIO()
        with (
            patch.object(word_taskpane, "PORT", 0),
            patch.object(sys, "platform", "darwin"),
            patch("subprocess.run", return_value=subprocess.CompletedProcess([], 1)) as launch,
            redirect_stderr(errors),
            redirect_stdout(output),
        ):
            self.assertEqual(word_taskpane.main(command), 2)
        self.assertEqual(launch.call_args.args[0], ["/usr/bin/open", "-b", "com.microsoft.Word", str(self.file)])
        self.assertIn("Microsoft Word", errors.getvalue())
        self.assertIn(f"http://127.0.0.1:{ledger.get(self.rid)['port']}/", output.getvalue())
        self.assertNotIn("Binding token", output.getvalue())

    def test_binds_one_live_docx_and_uses_its_table_suggestion(self):
        key = self.key(self.file)
        code, bound = self.request("/api/bind", {"documentKey": key})
        self.assertEqual(code, 200, bound)
        expected = {
            "id": self.nid,
            "from": "五日",
            "to": "七日",
            "paragraph": "应于五日内回复",
            "table": 0,
            "row": 0,
            "column": 1,
            "paraId": "1A000002",
        }
        self.assertEqual(bound["reviewId"], self.rid)
        self.assertEqual(bound["sourceTailId"], self.server.binding.paragraphs[-1].pid)
        self.assertEqual(bound["title"], "合成合同 A")
        table, tail = bound["suggestions"]
        self.assertEqual(table, expected)
        self.assertEqual(
            (tail["from"], tail["paragraph"], tail["bodyParagraph"]), ("签收即生效", "本合同自签收即生效。", 27)
        )
        self.assertTrue(tail["to"].endswith("各存一份"))
        self.assertNotIn("table", tail)
        self.assertEqual(
            self.request("/api/suggestion", {"documentKey": key, "reviewId": self.rid, "id": self.nid}),
            (200, {"reviewId": self.rid, "suggestion": expected}),
        )

    def test_pane_reads_document_current_batch_and_receiver_without_editing_word(self):
        request = {"documentKey": self.key(self.file)}
        source = self.file.read_bytes()
        status, opened = self.request("/api/bind", request)
        self.assertEqual(status, 200)
        self.assertEqual(
            (opened["documentName"], opened["receiver"], opened["batch"], opened["browserUrl"]),
            ("contract.docx", {"state": "unconfigured"}, None, f"http://127.0.0.1:{ledger.get(self.rid)['port']}/"),
        )
        notes_store.handoff_submit(self.review, "核对表格")
        status, refreshed = self.request("/api/status", {**request, "reviewId": self.rid})
        self.assertEqual(status, 200)
        self.assertEqual(refreshed["batch"], {"seq": 1, "status": "pending"})
        self.assertEqual(self.file.read_bytes(), source)

    def test_legacy_repeated_quote_keeps_verified_offset_or_requests_relocation(self):
        import docx_review
        import word_taskpane

        clause = "样机🧾编号及备用编号。"
        with patch.object(make_word_native, "TERMS", (clause,)):
            review, file, rid, _ = self.review_fixture("r")
        source = docx_review.render(file)
        para = next(p for p in source["paragraphs"] if p["digest"] == docx_review.text_digest(clause))
        anchor = {"v": 1, "kind": "quote", "via": "text", "fp": source["fingerprint"], "para": para}
        for offsets in ({}, {"start": clause.rindex("编号"), "end": clause.rindex("编号") + 2}):
            notes_store.add(
                review,
                {
                    "page": 1,
                    "target": "报告",
                    "quoteFrom": "报告",
                    "quote": "编号",
                    "text": "核对",
                    "docx": {**anchor, **offsets},
                    "edit": {"op": "replace", "from": "编号", "to": "序号"},
                },
            )
        self.server = word_taskpane.TaskpaneServer(review, file, self.tls, port=0)
        self.start(self.server)
        status, result = self.request("/api/bind", {"documentKey": self.key(file)})
        self.assertEqual(status, 200)
        matches = [s for s in result["suggestions"] if s["from"] == "编号"]
        self.assertEqual(len(matches), 2)
        self.assertEqual(matches[0]["status"], "needs-relocation")
        self.assertEqual(matches[1]["sourceOffset"], len(clause[: clause.rindex("编号")].encode("utf-16-le")) // 2)

    def test_bound_receiver_returns_selected_thread_and_locatable_suggestion(self):
        import word_context
        import word_taskpane

        review, source, rid, _ = self.review_fixture("c", session="word-reviewer")
        original_package = source.read_bytes()
        self.server = word_taskpane.TaskpaneServer(review, source, self.tls, port=0)
        self.start(self.server)
        identity = {"documentKey": self.key(source), "reviewId": rid}
        paragraph = {
            "n": 8,
            "paraId": "1A000002",
            "digest": hashlib.sha256("应于五日内回复".encode()).hexdigest(),
            "original": "五日",
            "current": "五日",
            "revisions": [],
            "threads": [
                {
                    "id": "t1",
                    "authorName": "乙",
                    "creationDate": None,
                    "content": "五日够吗？",
                    "range": {"text": "五日"},
                    "resolved": False,
                    "replies": [
                        {"id": "r1", "authorName": "甲", "creationDate": "", "content": "请按验收安排复核时限。"}
                    ],
                }
            ],
        }
        submission = {**identity, "submitId": "test-word-selection", "paragraphs": [paragraph]}
        self.assertEqual(self.request("/api/word/submit", submission), (200, {"batch": 1, "delivery": "waiting"}))
        self.assertEqual(self.request("/api/word/submit", submission)[1]["batch"], 1)
        self.assertEqual(self.request("/api/word/submit", {**submission, "submitId": "new-preview"})[1]["batch"], 1)
        self.assertEqual(self.request("/api/word/ack", {**identity, "batch": 1}), (200, {}))
        claimed = notes_store.handoff_claim(review, seq=1)
        self.assertEqual(claimed["wordSubmission"]["paragraphs"], [paragraph])
        reason = "甲回复“请按验收安排复核时限。”"
        [item] = word_context.return_suggestions(
            review, 1, [{"n": 8, "from": "五日", "to": "七日", "reason": reason}], "Codex", "word-reviewer"
        )
        entries = self.request("/api/word/list", identity)[1]["suggestions"]
        suggestion = next(s for s in entries if s["id"] == item["id"])
        self.assertEqual(
            (
                suggestion["sourceBatch"],
                suggestion["reason"],
                suggestion["status"],
                suggestion["table"],
                suggestion["column"],
            ),
            (1, reason, "pending", 0, 1),
        )
        self.assertEqual(self.request("/api/suggestion", {**identity, "id": item["id"]})[1]["suggestion"], suggestion)
        self.assertEqual(self.request("/api/word/withdraw", {**identity, "id": item["id"]})[0], 200)
        self.assertEqual(self.request("/api/suggestion", {**identity, "id": item["id"]})[0], 404)
        self.assertEqual(source.read_bytes(), original_package)

    def test_receiver_transfer_invalidates_the_open_pane(self):
        import word_taskpane

        review, file, rid, nid = self.review_fixture("c", session="first-session")
        self.server = word_taskpane.TaskpaneServer(review, file, self.tls, port=0)
        self.start(self.server)
        body = {"documentKey": self.key(file)}
        status, opened = self.request("/api/bind", body)
        self.assertEqual(status, 200)
        received = opened["receiver"]
        self.assertEqual(
            (received["state"], received["harness"], received["sessionId"]), ("idle", "Codex", "first-session")
        )
        wakeup.bind_wait(review, "Codex", "other-session", replace=True)
        for path, request in (
            ("/api/status", {**body, "reviewId": rid}),
            ("/api/suggestion", {**body, "reviewId": rid, "id": nid}),
            ("/api/bind", body),
        ):
            with self.subTest(path=path):
                self.assertEqual(self.request(path, request), (409, {"error": "receiver-changed"}))

    def test_file_replaced_after_binding_cannot_receive_the_cached_suggestion(self):
        body = {"documentKey": self.key(self.file)}
        self.assertEqual(self.request("/api/bind", body)[0], 200)
        self.file.write_bytes(b"updated synthetic document")
        self.assertEqual(
            self.request("/api/suggestion", {**body, "reviewId": self.rid, "id": self.nid}),
            (409, {"error": "document-changed"}),
        )

    def test_reopen_after_save_requires_explicit_rebuild_and_new_connection(self):
        import build_viewer
        import word_taskpane

        body = {"documentKey": self.key(self.file)}
        status, before = self.request("/api/bind", body)
        self.assertEqual(status, 200)
        old_token = self.server.token
        notes_store.handoff_submit(self.review, "保留既有批次")
        # A save can change the ZIP fingerprint while preserving all paragraph text.
        with zipfile.ZipFile(self.file, "a") as package:
            package.comment = b"synthetic save"
        self.assertEqual(self.request("/api/bind", body), (409, {"error": "document-changed"}))
        self.assertEqual(build_viewer.main(["--rebuild", str(self.review)]), 0)
        self.assertEqual(self.request("/api/bind", body), (409, {"error": "document-changed"}))
        self.server = word_taskpane.TaskpaneServer(self.review, self.file, self.tls, port=0)
        self.start(self.server)
        self.assertEqual(self.request("/api/bind", body, token=old_token)[0], 403)
        status, reopened = self.request("/api/bind", body)
        self.assertEqual(status, 200)
        self.assertEqual((reopened["reviewId"], reopened["batch"]["seq"]), (self.rid, 1))
        self.assertEqual(reopened["suggestions"], before["suggestions"])

    def test_second_live_review_cannot_be_selected_by_identity_document_or_note(self):
        key = self.key(self.file)
        self.assertEqual(self.request("/api/bind", {"documentKey": self.key(self.other_file)})[0], 409)
        self.assertEqual(self.request("/api/bind", {"documentKey": key}, token="invalid")[0], 403)
        for rid, nid, status in ((self.other_id, self.other_note, 409), (self.rid, self.other_note, 404)):
            with self.subTest(rid=rid):
                self.assertEqual(
                    self.request("/api/suggestion", {"documentKey": key, "reviewId": rid, "id": nid})[0], status
                )
        self.assertEqual(self.request("/api/bind", {"documentKey": key, "url": "synthetic"})[0], 400)

    def test_only_same_origin_json_and_explicit_resources_are_served(self):
        body = {"documentKey": self.key(self.file)}
        for header, status in (
            ({"Host": "localhost"}, 421),
            ({"Origin": "null"}, 403),
            ({"Sec-Fetch-Site": "cross-site"}, 403),
            ({"Content-Type": "text/plain"}, 415),
        ):
            with self.subTest(headers=header):
                self.assertEqual(self.request("/api/bind", body, headers=header)[0], status)
        for path in ("/", "/review.json", "/api/notes", "/api/overview/start", "/api/health", "/manifest.xml"):
            with self.subTest(path=path):
                self.assertEqual(self.request(path, {}, method="GET")[0], 404)

    def test_stopped_review_and_reused_port_invalidate_binding(self):
        key = self.key(self.file)
        body = {"reviewId": self.rid, "id": self.nid, "documentKey": key}
        real_status = ledger.status_file(self.review).read_bytes()
        ledger.status_file(self.review).write_bytes(ledger.status_file(self.other).read_bytes())
        self.assertEqual(self.request("/api/suggestion", body), (409, {"error": "review-unavailable"}))
        ledger.status_file(self.review).write_bytes(real_status)
        import review_stop

        review_stop.begin(self.review)
        self.assertEqual(self.request("/api/suggestion", body), (409, {"error": "review-unavailable"}))

    def test_legacy_suggestion_keeps_its_native_receipt_after_reopen_and_undo(self):
        import word_taskpane

        identity = {"reviewId": self.rid, "documentKey": self.key(self.file)}
        route = "/api/revision-operations/"

        def ops():
            return self.request("/api/word/list", identity)[1]["revisionOperations"]

        self.assertEqual(ops(), [])
        status, selection = self.request(route + "plan", dict(identity, items=[self.nid]))
        self.assertEqual(status, 200)
        base = dict(identity, **selection, id=self.nid)
        before = {"original": "应于五日内回复", "current": "应于五日内回复", "revisions": []}
        plan = dict(
            identity, id=self.nid, paragraphKey="cell/0/0/1", start=2, end=4, before=before, expected="应于七日内回复"
        )
        self.assertTrue(self.request(route + "begin", dict(base, plan=plan))[1]["mayWrite"])
        self.server = word_taskpane.TaskpaneServer(self.review, self.file, self.tls, port=0)
        self.start(self.server)
        self.assertEqual(ops()[0]["status"], "unknown")
        self.assertFalse(self.request(route + "begin", dict(base, plan=plan))[1]["mayWrite"])
        after = {
            "original": before["original"],
            "current": plan["expected"],
            "revisions": [
                {"type": kind, "text": text, "author": "乙", "start": 2, "date": ""}
                for kind, text in (("Deleted", "五日"), ("Added", "七日"))
            ],
        }
        self.assertEqual(self.request(route + "observe", dict(base, status="written", after=after, permit=""))[0], 200)
        self.assertEqual(self.request(route + "observe", dict(base, status="unknown", after=before, permit=""))[0], 200)
        self.assertEqual((ops()[0]["status"], ops()[0]["receipt"]), ("unknown", after))
        self.assertEqual(
            self.request(route + "observe", dict(base, status="unexecuted", after=None, permit=""))[0], 409
        )
        self.assertEqual(self.request(route + "plan", dict(identity, items=[self.nid]))[0], 409)

    def test_comment_journal_requests_stay_with_the_bound_document(self):
        identity = {"reviewId": self.rid, "documentKey": self.key(self.file)}
        intent = dict(identity, kind="publish", target="", location="a" * 64, bodyHash="b" * 64, preview="合成意见")
        route = "/api/comment-operations/begin"
        self.assertEqual(self.request(route, dict(intent, documentKey=self.key(self.other_file)))[0], 409)
        self.assertEqual(self.request(route, dict(intent, reviewId=self.other_id))[0], 409)
        self.assertEqual(self.request(route, dict(intent, kind="private"))[0], 400)
        self.assertEqual(self.request("/api/comment-operations/list", identity), (200, {"operations": []}))
        status, first = self.request(route, intent)
        self.assertEqual(status, 200)
        self.assertTrue(first["mayWrite"])
        self.assertEqual(self.request(route, intent), (200, {"mayWrite": False, "id": first["id"]}))
        for resource in ("/comments.pane.html", "/word-comments.json"):
            self.assertEqual(self.request(resource, {}, method="GET")[0], 404)

    def test_reserved_port_is_left_available_for_its_existing_owner(self):
        import word_taskpane

        reserved = ledger.assign_overview_port(prefer=self.server.server_port)
        with self.assertRaisesRegex(ValueError, "port-reserved"):
            with word_taskpane.TaskpaneServer(self.review, self.file, self.tls, port=reserved):
                pass

    @unittest.skipUnless(sys.platform == "darwin", "Word file URL aliases are macOS-specific")
    def test_word_can_reopen_the_same_file_through_macos_system_aliases(self):
        import word_taskpane

        for alias in ("tmp", "var"):
            base = "/private/tmp" if alias == "tmp" else "/private/var/tmp"
            with self.subTest(alias=alias), tempfile.TemporaryDirectory(dir=base) as temp:
                self.root = Path(temp).resolve()
                review, document, rid, _ = self.review_fixture("alias")
                other_path = document.parent.parent / "other" / document.name
                server = word_taskpane.TaskpaneServer(review, document, self.tls, port=0)
                self.start(server)
                self.server = server
                alias_path = Path("/") / document.relative_to("/private")
                self.assertEqual(alias_path.parts[1], alias)
                self.assertEqual(
                    self.request("/api/bind", {"documentKey": self.key(alias_path)})[1].get("reviewId"), rid
                )
                self.assertEqual(self.request("/api/bind", {"documentKey": self.key(other_path)})[0], 409)

    def test_office_can_supply_its_host_information_when_opening_the_static_pane(self):
        port = self.server.server_port
        connection = http.client.HTTPSConnection("127.0.0.1", port, context=self.client_tls, timeout=3)
        self.addCleanup(connection.close)
        connection.request("GET", "/index.html?_host_Info=Word%24Mac")
        response = connection.getresponse()
        self.assertEqual(response.status, 200)
        self.assertEqual(response.getheader("Referrer-Policy"), "same-origin")
        html = response.read()
        for expected in (b"appsforoffice.microsoft.com", b"function selectedPassage(", b'id="comment-post"'):
            self.assertIn(expected, html)
        self.assertNotIn(b"<!-- AE-PANE-FRAGMENTS -->", html)
