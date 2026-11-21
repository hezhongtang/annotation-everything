"""视频批注交接（#193）：选择性提交、源工程关联与领取负载。

全部使用合成 MP4 与合成工程路径；不调用引擎、渲染器或 Node（#194 的范围）。
"""

from __future__ import annotations

import io
import json
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "skill" / "annotation-everything" / "scripts"
sys.path.insert(0, str(SCRIPTS))
os.environ["AE_HOME"] = tempfile.mkdtemp(prefix="ae-video-handoff-home-")

import build_viewer  # noqa: E402
import notes as notes_cli  # noqa: E402
import notes_store as store  # noqa: E402
import video_source  # noqa: E402
import wakeup  # noqa: E402

from tests.test_video_review import synthetic_mp4  # noqa: E402

V1_MEDIA = {"duration": 6.0, "fps": 30, "frames": 180, "width": 640, "height": 360}


def video_note(nid: str, text: str = "", *, frame=None, start=None, end=None, mark=None) -> dict:
    anchor = {
        "v": 1,
        "versionId": "V1",
        "fps": 30,
        "timebase": {"num": 1, "den": 30},
        "frames": 180,
        "videoWidth": 640,
        "videoHeight": 360,
    }
    if frame is not None:
        anchor["frame"] = frame
    else:
        anchor["startFrame"], anchor["endFrame"] = start, end
    if mark is not None:
        anchor["mark"] = mark
    return {"id": nid, "page": 1, "text": text or f"意见 {nid}", "video": anchor}


class VideoHandoffBase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="ae-video-handoff-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = self.root / "synthetic.mp4"
        self.source.write_bytes(synthetic_mp4())
        self.d = self.root / "review"
        build_viewer.build(self.source, None, self.d, no_scan_skills=True, locale="zh-CN")

    def manifest(self) -> dict:
        return json.loads((self.d / "review.json").read_text(encoding="utf-8"))

    def save_manifest(self, manifest: dict) -> None:
        (self.d / "review.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    def add_notes(self, *notes: dict) -> None:
        store.import_notes(self.d, list(notes))

    def link_source(self, **kwargs) -> dict:
        """把源工程关联写进清单；默认参数是一份与 V1 完全一致的成片特征。"""
        kwargs.setdefault("media", V1_MEDIA)
        manifest = self.manifest()
        manifest["video"] = video_source.link(
            manifest["video"],
            engine=kwargs.pop("engine", "remotion"),
            root=kwargs.pop("root", str(self.root / "project")),
            entry=kwargs.pop("entry", {"kind": "composition", "id": "Main"}),
            **kwargs,
        )
        self.save_manifest(manifest)
        return manifest


class SelectiveSubmitTests(VideoHandoffBase):
    def setUp(self):
        super().setUp()
        self.add_notes(
            video_note(
                "n1", "字幕遮挡图标", frame=96, mark={"kind": "region", "x": 0.1, "y": 0.8, "width": 0.5, "height": 0.1}
            ),
            video_note("n2", "音乐太响", start=120, end=180),
            video_note("n3", "片头太长", frame=3),
            video_note("n4", "转场生硬", start=60, end=90),
            video_note("n5", "配色偏冷", frame=30),
        )

    def test_submit_with_note_ids_snapshots_only_selected_notes(self):
        _doc, item, merged = store.handoff_submit(self.d, "本批只改三条", note_ids=["n1", "n3", "n5"])
        self.assertFalse(merged)
        self.assertEqual(item["openIds"], ["n1", "n3", "n5"])
        self.assertEqual(item["openCount"], 3)
        self.assertTrue(item["selected"])
        state = store.handoff_state(self.d, full=False)
        self.assertEqual(sorted(state["inFlightIds"]), ["n1", "n3", "n5"], "未选中的 2 条保持未处理")

    def test_unselected_notes_stay_open_for_later_batches(self):
        store.handoff_submit(self.d, note_ids=["n1"])
        notes = store.load(self.d)["notes"]
        by_id = {n["id"]: n for n in notes}
        self.assertEqual([by_id[f"n{i}"]["status"] for i in range(1, 6)], ["open"] * 5)

    def test_submit_rejects_unknown_or_not_open_ids(self):
        with self.assertRaises(store.NoteError):
            store.handoff_submit(self.d, note_ids=["n1", "ghost"])
        store.set_status(self.d, "n2", "resolved", resolution="已改", by="page")
        with self.assertRaises(store.NoteError):
            store.handoff_submit(self.d, note_ids=["n1", "n2"])
        with self.assertRaises(store.NoteError):
            store.handoff_submit(self.d, note_ids=[])

    def test_repeated_submit_id_merges_into_pending_batch(self):
        _doc, first, _m = store.handoff_submit(self.d, "第一次", note_ids=["n1", "n2"], submit_id="op-1")
        _doc, again, merged = store.handoff_submit(self.d, "改了附言", note_ids=["n1", "n2", "n3"], submit_id="op-1")
        self.assertTrue(merged)
        self.assertEqual(again["seq"], first["seq"], "重试同一次提交不新增批次")
        self.assertEqual(again["openIds"], ["n1", "n2", "n3"], "等待中的批次按最新选择刷新快照")
        self.assertEqual(len(store.handoff_load(self.d)["items"]), 1)

    def test_stale_submit_id_returns_existing_batch_without_new_one(self):
        _doc, first, _m = store.handoff_submit(self.d, note_ids=["n1"], submit_id="op-1")
        store.handoff_claim(self.d, by="agent")
        _doc, retried, merged = store.handoff_submit(self.d, "网络重试", note_ids=["n1"], submit_id="op-1")
        self.assertTrue(merged)
        self.assertEqual(retried["seq"], first["seq"])
        self.assertEqual(retried["status"], "claimed", "陈旧提交不改写已被领取的批次")
        self.assertEqual(len(store.handoff_load(self.d)["items"]), 1, "重试已领取的提交不产生新批次")
        doc = store.handoff_load(self.d)
        self.assertEqual(doc["items"][0].get("userMessage") or doc["items"][0].get("message"), first.get("userMessage"))

    def test_default_submit_still_takes_all_open_notes(self):
        _doc, item, _m = store.handoff_submit(self.d, "整批")
        self.assertEqual(item["openIds"], ["n1", "n2", "n3", "n4", "n5"])
        self.assertNotIn("selected", item)


class ReceiverGuardTests(VideoHandoffBase):
    def setUp(self):
        super().setUp()
        self.add_notes(video_note("n1", frame=1), video_note("n2", frame=2))
        self.link_source()

    def test_stale_session_cannot_claim_after_transfer(self):
        wakeup.bind_wait(self.d, "Claude Code", "session-a")
        store.handoff_submit(self.d, note_ids=["n1"])
        # 批次还在 pending：允许显式转移到新会话
        wakeup.bind_wait(self.d, "Claude Code", "session-b", replace=True)
        with self.assertRaises(ValueError):
            wakeup.assert_claim_owner(self.d, "Claude Code", "session-a")
        wakeup.assert_claim_owner(self.d, "Claude Code", "session-b")

    def test_claimed_batch_blocks_transfer(self):
        wakeup.bind_wait(self.d, "Claude Code", "session-a")
        store.handoff_submit(self.d, note_ids=["n1"])
        store.handoff_claim(self.d, by="agent")
        with self.assertRaises(ValueError):
            wakeup.bind_wait(self.d, "Claude Code", "session-b", replace=True)


class VideoSourceTests(VideoHandoffBase):
    def test_unique_media_match_links_directly(self):
        video = video_source.link(
            self.manifest()["video"],
            engine="remotion",
            root=str(self.root / "project"),
            entry={"kind": "composition", "id": "Main"},
            media=V1_MEDIA,
        )
        sp = video["sourceProject"]
        self.assertEqual(sp["status"], "linked")
        self.assertEqual(sp["match"], "verified")
        self.assertEqual(sp["versionId"], "V1")
        self.assertEqual(sp["engine"], "remotion")
        self.assertEqual(sp["entry"], {"kind": "composition", "id": "Main"})
        self.assertTrue(video_source.linked(video))

    def test_media_differences_require_manual_confirmation(self):
        video = video_source.link(
            self.manifest()["video"],
            engine="hyperframes",
            root=str(self.root / "hf"),
            entry={"kind": "scene", "id": "Intro"},
            media={**V1_MEDIA, "frames": 210, "duration": 7.0},
        )
        sp = video["sourceProject"]
        self.assertEqual(sp["status"], "pending")
        self.assertFalse(video_source.linked(video))
        fields = {diff["field"] for diff in sp["differences"]}
        self.assertIn("frames", fields)
        self.assertIn("duration", fields)
        confirmed = video_source.confirm(video, by="page")
        sp2 = confirmed["sourceProject"]
        self.assertEqual(sp2["status"], "linked")
        self.assertEqual(sp2["match"], "confirmed")
        self.assertEqual(sp2["confirmedBy"], "page")

    def test_filename_only_cannot_verify_a_match(self):
        video = video_source.link(
            self.manifest()["video"],
            engine="remotion",
            root=str(self.root / "project"),
            entry={"kind": "composition", "id": "Main"},
            media=None,
            filename="synthetic.mp4",
        )
        sp = video["sourceProject"]
        self.assertEqual(sp["status"], "pending", "仅文件名相同不足以证明匹配")
        self.assertEqual(sp["basis"]["kind"], "filename-only")
        video = video_source.confirm(video, by="page")
        self.assertEqual(video["sourceProject"]["match"], "confirmed")

    def test_multiple_candidates_require_explicit_choice(self):
        video = video_source.link(
            self.manifest()["video"],
            engine="remotion",
            root=str(self.root / "project"),
            entry={"kind": "composition", "id": "Main"},
            media=V1_MEDIA,
            candidates=[
                {"engine": "remotion", "root": str(self.root / "a"), "entry": {"kind": "composition", "id": "Main"}},
                {"engine": "remotion", "root": str(self.root / "b"), "entry": {"kind": "composition", "id": "Alt"}},
            ],
        )
        sp = video["sourceProject"]
        self.assertEqual(sp["status"], "pending")
        self.assertEqual(len(sp["candidates"]), 2)
        with self.assertRaises(video_source.SourceError):
            video_source.confirm(video, by="page")  # 多候选必须指明选择
        video = video_source.confirm(video, choice=sp["candidates"][1]["id"], by="page")
        sp2 = video["sourceProject"]
        self.assertEqual(sp2["status"], "linked")
        self.assertEqual(sp2["root"], str(self.root / "b"))
        self.assertEqual(sp2["entry"]["id"], "Alt")

    def test_link_rejects_unknown_engine_and_bad_entry(self):
        video = self.manifest()["video"]
        for engine in ("unity", ""):
            with self.assertRaises(video_source.SourceError):
                video_source.link(video, engine=engine, root="/tmp/x", entry={"kind": "composition", "id": "M"})
        for entry in ({"kind": "scene", "id": "M"}, {"kind": "composition"}, {"kind": "composition", "id": ""}):
            with self.assertRaises(video_source.SourceError):
                video_source.link(video, engine="remotion", root="/tmp/x", entry=entry)

    def test_public_summary_hides_nothing_from_the_agent_brief(self):
        video = video_source.link(
            self.manifest()["video"],
            engine="remotion",
            root="/abs/project",
            entry={"kind": "composition", "id": "Main"},
            media=V1_MEDIA,
        )
        public = video_source.public(video)
        self.assertEqual(public["status"], "linked")
        self.assertEqual(public["engine"], "remotion")
        self.assertEqual(public["entry"]["id"], "Main")
        self.assertEqual(public["root"], "/abs/project")
        brief = video_source.agent_brief(video)
        self.assertEqual(
            brief,
            {
                "engine": "remotion",
                "root": "/abs/project",
                "entry": {"kind": "composition", "id": "Main"},
                "versionId": "V1",
            },
        )

    def test_annotations_keep_pointing_at_the_registered_version(self):
        self.add_notes(video_note("n1", frame=96))
        before = store.load(self.d)["notes"][0]["video"]
        self.link_source(media={**V1_MEDIA, "frames": 210})
        video_source.confirm(self.manifest()["video"], by="page")
        after = store.load(self.d)["notes"][0]["video"]
        self.assertEqual(after, before, "关联与确认都不改原批注的原片定位")


class ClaimPayloadTests(VideoHandoffBase):
    def setUp(self):
        super().setUp()
        self.add_notes(
            video_note(
                "n1", "字幕遮挡图标", frame=96, mark={"kind": "region", "x": 0.1, "y": 0.8, "width": 0.5, "height": 0.1}
            ),
            video_note("n2", "音乐太响", start=120, end=180),
            video_note("n3", "这次不交", frame=9),
        )
        self.link_source()
        wakeup.bind_wait(self.d, "Claude Code", "session-a")

    def run_claim(self):
        env = {k: v for k, v in os.environ.items()}
        os.environ["AE_HARNESS"] = "Claude Code"
        os.environ["AE_SESSION_ID"] = "session-a"
        try:
            buf = io.StringIO()
            with redirect_stdout(buf):
                code = notes_cli.main(["claim", str(self.d)])
        finally:
            os.environ.clear()
            os.environ.update(env)
        return code, json.loads(buf.getvalue())

    def test_claim_json_carries_scope_evidence_and_source_project(self):
        store.handoff_submit(self.d, "批次附言：优先字幕", note_ids=["n1", "n2"], submit_id="op-9")
        code, payload = self.run_claim()
        self.assertEqual(code, 0)
        self.assertTrue(payload["claimed"])
        self.assertEqual(payload["reviewId"], self.manifest()["reviewId"])
        self.assertEqual(payload["seq"], 1)
        self.assertEqual(payload["noteIds"], ["n1", "n2"], "claim JSON 自带批次范围，不用再读 show --batch")
        self.assertEqual(payload["userMessage"], "批次附言：优先字幕")
        self.assertEqual(payload["sourceProject"]["root"], str(self.root / "project"))
        self.assertEqual(payload["sourceProject"]["entry"], {"kind": "composition", "id": "Main"})
        materials = {m["noteId"]: m for m in payload["materials"]}
        self.assertEqual(sorted(materials), ["n1", "n2"], "只领到选中的两条")
        self.assertEqual(materials["n1"]["text"], "字幕遮挡图标")
        loc = materials["n1"]["video"]
        self.assertEqual(loc["versionId"], "V1")
        self.assertEqual(loc["frame"], 96)
        self.assertEqual(loc["mark"], {"kind": "region", "x": 0.1, "y": 0.8, "width": 0.5, "height": 0.1})
        self.assertEqual(loc["frames"], 180)
        span = materials["n2"]["video"]
        self.assertEqual([span["startFrame"], span["endFrame"]], [120, 180])
        media = materials["n1"]["media"]
        self.assertEqual(media["url"], "/api/video/versions/V1/media")
        self.assertEqual(media["path"], "media/v1.mp4")

    def test_wait_output_lists_only_selected_notes_with_source_line(self):
        store.handoff_submit(self.d, "只交两条", note_ids=["n1", "n2"])
        env = {k: v for k, v in os.environ.items()}
        os.environ["AE_HARNESS"] = "Claude Code"
        os.environ["AE_SESSION_ID"] = "session-a"
        try:
            buf, err = io.StringIO(), io.StringIO()
            with redirect_stdout(buf), redirect_stderr(err):
                code = notes_cli.main(["wait", str(self.d), "--no-claim", "--timeout", "0"])
        finally:
            os.environ.clear()
            os.environ.update(env)
        self.assertEqual(code, 0)
        text = buf.getvalue()
        self.assertIn("n1", text)
        self.assertIn("n2", text)
        self.assertNotIn("这次不交", text, "未选中的批注不进 wait 输出")
        self.assertIn(str(self.root / "project"), text)
        self.assertIn("Main", text)


class ServeHandoffTests(VideoHandoffBase):
    def setUp(self):
        super().setUp()
        import serve

        self.add_notes(video_note("n1", frame=96), video_note("n2", frame=120), video_note("n3", frame=30))
        self.server = serve.ReviewServer(self.d, 0, log_to_file=False)
        import threading

        worker = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
        worker.start()

        def close():
            self.server.shutdown()
            self.server.server_close()
            worker.join(2)

        self.addCleanup(close)

    def post(self, path: str, body: dict):
        serve = sys.modules["serve"]
        headers = {
            "Host": f"127.0.0.1:{self.server.port}",
            "Origin": f"http://127.0.0.1:{self.server.port}",
            "Content-Type": "application/json",
        }
        return serve._req(self.server.port, "POST", path, headers, body)

    def get(self, path: str):
        serve = sys.modules["serve"]
        headers = {"Host": f"127.0.0.1:{self.server.port}"}
        return serve._req(self.server.port, "GET", path, headers)

    def test_video_handoff_requires_a_linked_source_project(self):
        code, _h, body = self.post("/api/handoff", {"message": "先交", "noteIds": ["n1"]})
        self.assertEqual(code, 400)
        self.assertEqual(json.loads(body)["code"], "video-source-required")
        code, _h, _body = self.post("/api/handoff", {"message": "整批也一样"})
        self.assertEqual(code, 400, "视频审阅的整批提交同样需要先关联源工程")

    def test_video_handoff_with_note_ids_creates_selected_batch(self):
        self.link_source()
        code, _h, body = self.post(
            "/api/handoff",
            {"message": "交两条", "noteIds": ["n1", "n2"], "submitId": "op-http-1"},
        )
        self.assertEqual(code, 200, body)
        data = json.loads(body)
        self.assertEqual(data["seq"], 1)
        self.assertFalse(data["merged"])
        items = store.handoff_load(self.d)["items"]
        self.assertEqual(items[0]["openIds"], ["n1", "n2"])

        code, _h, body = self.post(
            "/api/handoff",
            {"message": "网络重试", "noteIds": ["n1", "n2"], "submitId": "op-http-1"},
        )
        self.assertEqual(code, 200)
        self.assertTrue(json.loads(body)["merged"])
        self.assertEqual(len(store.handoff_load(self.d)["items"]), 1)

    def test_video_handoff_rejects_bad_note_ids(self):
        self.link_source()
        code, _h, body = self.post("/api/handoff", {"message": "x", "noteIds": ["n1", "ghost"]})
        self.assertEqual(code, 400)
        code, _h, body = self.post("/api/handoff", {"message": "x", "noteIds": "n1"})
        self.assertEqual(code, 400)
        code, _h, body = self.post("/api/handoff", {"message": "x", "submitId": 7})
        self.assertEqual(code, 400)

    def test_features_announce_the_new_capabilities(self):
        code, _h, body = self.get("/api/health")
        features = json.loads(body)["features"]
        self.assertIn("selective-handoff", features)
        self.assertIn("video-source", features)


class ServeSourceProjectTests(VideoHandoffBase):
    def setUp(self):
        super().setUp()
        import serve
        import threading

        self.server = serve.ReviewServer(self.d, 0, log_to_file=False)
        worker = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
        worker.start()

        def close():
            self.server.shutdown()
            self.server.server_close()
            worker.join(2)

        self.addCleanup(close)

    def post(self, path: str, body: dict):
        serve = sys.modules["serve"]
        headers = {
            "Host": f"127.0.0.1:{self.server.port}",
            "Origin": f"http://127.0.0.1:{self.server.port}",
            "Content-Type": "application/json",
        }
        return serve._req(self.server.port, "POST", path, headers, body)

    def get(self, path: str):
        serve = sys.modules["serve"]
        headers = {"Host": f"127.0.0.1:{self.server.port}"}
        return serve._req(self.server.port, "GET", path, headers)

    def test_get_before_link_reports_no_source_project(self):
        code, _h, body = self.get("/api/video/source-project")
        self.assertEqual(code, 200)
        self.assertIsNone(json.loads(body)["sourceProject"])

    def test_post_with_matching_media_links_directly(self):
        code, _h, body = self.post(
            "/api/video/source-project",
            {
                "engine": "remotion",
                "root": str(self.root / "project"),
                "entry": {"kind": "composition", "id": "Main"},
                "media": V1_MEDIA,
            },
        )
        self.assertEqual(code, 200, body)
        sp = json.loads(body)["sourceProject"]
        self.assertEqual(sp["status"], "linked")
        self.assertEqual(sp["match"], "verified")
        self.assertEqual(self.manifest()["video"]["sourceProject"]["entry"]["id"], "Main")

    def test_post_with_differences_returns_pending_then_confirm_links(self):
        code, _h, body = self.post(
            "/api/video/source-project",
            {
                "engine": "hyperframes",
                "root": str(self.root / "hf"),
                "entry": {"kind": "scene", "id": "Intro"},
                "media": {**V1_MEDIA, "fps": 25},
            },
        )
        self.assertEqual(code, 200, body)
        sp = json.loads(body)["sourceProject"]
        self.assertEqual(sp["status"], "pending")
        self.assertTrue(sp["differences"])

        code, _h, body = self.post("/api/video/source-project/confirm", {"choice": None})
        self.assertEqual(code, 200, body)
        sp2 = json.loads(body)["sourceProject"]
        self.assertEqual(sp2["status"], "linked")
        self.assertEqual(self.manifest()["video"]["sourceProject"]["status"], "linked")

    def test_post_validates_payload(self):
        code, _h, body = self.post(
            "/api/video/source-project", {"engine": "unity", "root": "/x", "entry": {"kind": "composition", "id": "M"}}
        )
        self.assertEqual(code, 400)
        code, _h, body = self.post("/api/video/source-project", {"engine": "remotion"})
        self.assertEqual(code, 400)

    def test_confirm_unknown_choice_or_without_link_is_rejected(self):
        code, _h, _body = self.post("/api/video/source-project/confirm", {"choice": "ghost"})
        self.assertEqual(code, 400)
        code, _h, body = self.post(
            "/api/video/source-project",
            {
                "engine": "remotion",
                "root": str(self.root / "p"),
                "entry": {"kind": "composition", "id": "M"},
                "media": V1_MEDIA,
            },
        )
        self.assertEqual(code, 200)
        code, _h, _body = self.post("/api/video/source-project/confirm", {"choice": "ghost"})
        self.assertEqual(code, 400)


if __name__ == "__main__":
    unittest.main()
