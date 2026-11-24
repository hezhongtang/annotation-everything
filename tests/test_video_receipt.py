"""视频批注回执、多版本登记与防陈旧门禁（#195）。

使用合成 MP4 与合成审阅目录，按 TDD 切片逐层验证 video_review、notes_store、notes CLI 与 HTTP API。
"""

from __future__ import annotations

import copy
import hashlib
import io
import json
import os
import sys
import tempfile
import threading
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "skill" / "annotation-everything" / "scripts"
sys.path.insert(0, str(SCRIPTS))
sys.path.insert(0, str(ROOT))

import host_isolation  # noqa: E402

host_isolation.require_for_module(__name__)
os.environ["AE_HOME"] = tempfile.mkdtemp(prefix="ae-video-receipt-home-")

import build_viewer  # noqa: E402
import ledger  # noqa: E402
import notes as notes_cli  # noqa: E402
import notes_store as store  # noqa: E402
import serve  # noqa: E402
import video_review  # noqa: E402
import video_source  # noqa: E402
import wakeup  # noqa: E402
from tests.test_video_handoff import V1_MEDIA, video_note  # noqa: E402
from tests.test_video_review import run_video_cli, synthetic_mp4  # noqa: E402


class VideoVersionRegistryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="ae-video-receipt-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.v1_bytes = synthetic_mp4(frames=180, fps=30, width=640, height=360)
        self.v2_bytes = synthetic_mp4(frames=192, fps=30, width=640, height=360)
        self.source = self.root / "dist" / "out.mp4"
        self.source.parent.mkdir(parents=True, exist_ok=True)
        self.source.write_bytes(self.v1_bytes)
        self.review = self.root / "review"
        build_viewer.build(self.source, None, self.review, no_scan_skills=True, locale="zh-CN")
        self.manifest = json.loads((self.review / "review.json").read_text(encoding="utf-8"))
        self.rid = self.manifest["reviewId"]

    def test_register_version_creates_v2_without_overwriting_v1_or_leaking_paths(self):
        # 工程覆盖自己的常见输出路径 out.mp4 或新路径 render_v2.mp4 生成 V2，都不影响 V1 副本且不泄露源产物路径
        self.source.write_bytes(self.v2_bytes)
        v2_output = self.root / "project" / "out" / "render_v2.mp4"
        v2_output.parent.mkdir(parents=True, exist_ok=True)
        v2_output.write_bytes(self.v2_bytes)
        v2 = video_review.register_version(
            self.review,
            v2_output,
            batch=1,
            by="claude",
            review_id=self.rid,
            receipt_id="rcpt-v2",
        )
        self.assertEqual(v2["versionId"], "V2")
        self.assertEqual(v2["path"], "media/v2.mp4")
        self.assertEqual(v2["sha256"], hashlib.sha256(self.v2_bytes).hexdigest())
        self.assertEqual(v2["frames"], 192)
        self.assertEqual(v2["batch"], 1)
        self.assertEqual(v2["by"], "claude")
        self.assertTrue(v2["at"])

        self.assertEqual((self.review / "media" / "v1.mp4").read_bytes(), self.v1_bytes)
        self.assertEqual((self.review / "media" / "v2.mp4").read_bytes(), self.v2_bytes)
        self.assertEqual(list((self.review / "media").glob("*.tmp")), [])

        updated = json.loads((self.review / "review.json").read_text(encoding="utf-8"))
        self.assertEqual(updated["video"]["currentVersionId"], "V2")
        self.assertEqual([v["versionId"] for v in updated["video"]["versions"]], ["V1", "V2"])
        self.assertNotIn(str(v2_output), json.dumps(updated, ensure_ascii=False))
        self.assertNotIn(str(self.source), json.dumps(updated["video"], ensure_ascii=False))

        pub = video_review.public_manifest(updated)
        self.assertEqual(pub["currentVersionId"], "V2")
        self.assertEqual(len(pub["versions"]), 2)
        pub_v2 = pub["versions"][1]
        self.assertEqual(pub_v2["url"], f"/api/video/{self.rid}/versions/V2/media")
        self.assertEqual(pub_v2["batch"], 1)
        self.assertEqual(pub_v2["by"], "claude")
        self.assertEqual(pub_v2["at"], v2["at"])
        self.assertNotIn("path", pub_v2)
        self.assertNotIn(str(v2_output), json.dumps(pub, ensure_ascii=False))

    def test_register_version_idempotent_retry_and_cross_review_rejection(self):
        v2_file = self.root / "v2.mp4"
        v2_file.write_bytes(self.v2_bytes)
        first = video_review.register_version(
            self.review,
            v2_file,
            batch=1,
            by="claude",
            review_id=self.rid,
            receipt_id="rcpt-1",
        )
        # 同一 (batch, receiptId) 重试：原样返回 V2，不生成 V3
        again = video_review.register_version(
            self.review,
            v2_file,
            batch=1,
            by="claude",
            review_id=self.rid,
            receipt_id="rcpt-1",
        )
        self.assertEqual(again["versionId"], first["versionId"])
        updated = json.loads((self.review / "review.json").read_text(encoding="utf-8"))
        self.assertEqual([v["versionId"] for v in updated["video"]["versions"]], ["V1", "V2"])

        with self.assertRaises(video_review.VideoError):
            video_review.register_version(
                self.review,
                v2_file,
                batch=2,
                by="claude",
                review_id="0000000000000000",
                receipt_id="rcpt-cross",
            )
        after_reject = json.loads((self.review / "review.json").read_text(encoding="utf-8"))
        self.assertEqual([v["versionId"] for v in after_reject["video"]["versions"]], ["V1", "V2"])

    def test_interrupted_manifest_write_can_resume_publication_of_the_same_render(self):
        from unittest.mock import patch

        self.source.write_bytes(self.v2_bytes)
        original = os.replace

        def fail_manifest(source, destination):
            if Path(destination) == self.review / "review.json":
                raise OSError("synthetic disk interruption")
            return original(source, destination)

        with patch.object(os, "replace", side_effect=fail_manifest), self.assertRaises(OSError):
            video_review.register_version(self.review, self.source, batch=1, by="agent", receipt_id="retry")
        v2 = video_review.register_version(self.review, self.source, batch=1, by="agent", receipt_id="retry")
        self.assertEqual(v2["versionId"], "V2")
        self.assertEqual([v["versionId"] for v in ledger.read_manifest(self.review)["video"]["versions"]], ["V1", "V2"])

    def test_failure_recording_and_unplayable_movie_keep_v1_and_clear_on_recovery(self):
        bad = self.root / "broken.mp4"
        bad.write_bytes(b"not a playable mp4")
        with self.assertRaises(video_review.VideoError):
            video_review.register_version(
                self.review,
                bad,
                batch=1,
                by="claude",
                review_id=self.rid,
                receipt_id="rcpt-bad",
            )
        self.assertFalse((self.review / "media" / "v2.mp4").exists())
        self.assertEqual(list((self.review / "media").glob("*.tmp")), [])

        fail = video_review.record_failure(
            self.review,
            batch=1,
            by="claude",
            reason="render exited 1",
            review_id=self.rid,
            receipt_id="fail-1",
        )
        self.assertEqual(fail["batch"], 1)
        self.assertEqual(fail["reason"], "render exited 1")
        # 幂等重放同一失败回执不重复追加
        video_review.record_failure(
            self.review,
            batch=1,
            by="claude",
            reason="render exited 1",
            review_id=self.rid,
            receipt_id="fail-1",
        )
        failed_manifest = json.loads((self.review / "review.json").read_text(encoding="utf-8"))
        self.assertEqual(failed_manifest["video"]["currentVersionId"], "V1")
        self.assertEqual(len(failed_manifest["video"]["failures"]), 1)
        pub = video_review.public_manifest(failed_manifest)
        self.assertEqual(pub["currentVersionId"], "V1")
        self.assertEqual(pub["lastFailure"]["reason"], "引擎渲染失败")

        # 恢复后登记 V2，清除 lastFailure 并保留 failures[] 历史
        v2_file = self.root / "recovered.mp4"
        v2_file.write_bytes(self.v2_bytes)
        v2 = video_review.register_version(
            self.review,
            v2_file,
            batch=1,
            by="claude",
            review_id=self.rid,
            receipt_id="rcpt-ok",
        )
        self.assertEqual(v2["versionId"], "V2")
        recovered_manifest = json.loads((self.review / "review.json").read_text(encoding="utf-8"))
        self.assertNotIn("lastFailure", recovered_manifest["video"])
        self.assertEqual(len(recovered_manifest["video"]["failures"]), 1)
        self.assertNotIn("lastFailure", video_review.public_manifest(recovered_manifest))

    def test_clean_receipt_target_validates_frame_range_cut_and_rejects_out_of_range(self):
        v2_meta = {
            "versionId": "V2",
            "width": 640,
            "height": 360,
            "duration": 6.4,
            "fps": 30.0,
            "timebase": {"num": 1, "den": 30},
            "frames": 192,
        }
        frame_t = video_review.clean_receipt_target({"kind": "frame", "frame": 108}, v2_meta)
        self.assertEqual(
            frame_t,
            {
                "v": 1,
                "kind": "frame",
                "versionId": "V2",
                "fps": 30.0,
                "timebase": {"num": 1, "den": 30},
                "frames": 192,
                "videoWidth": 640,
                "videoHeight": 360,
                "frame": 108,
            },
        )
        range_t = video_review.clean_receipt_target({"kind": "range", "startFrame": 132, "endFrame": 192}, v2_meta)
        self.assertEqual(range_t["kind"], "range")
        self.assertEqual((range_t["startFrame"], range_t["endFrame"]), (132, 192))

        cut_t = video_review.clean_receipt_target(
            {"kind": "cut", "frame": 108, "contextStart": 90, "contextEnd": 126}, v2_meta
        )
        self.assertEqual(cut_t["kind"], "cut")
        self.assertEqual((cut_t["frame"], cut_t["contextStart"], cut_t["contextEnd"]), (108, 90, 126))
        cut_end = video_review.clean_receipt_target({"kind": "cut", "frame": 192}, v2_meta)
        self.assertEqual(cut_end["frame"], 192)

        for bad_target in (
            {"kind": "frame", "frame": -1},
            {"kind": "frame", "frame": 192},
            {"kind": "frame", "frame": 10.5},
            {"kind": "frame", "frame": True},
            {"kind": "range", "startFrame": 10, "endFrame": 10},
            {"kind": "range", "startFrame": 10, "endFrame": 193},
            {"kind": "range", "startFrame": -1, "endFrame": 10},
            {"kind": "cut", "frame": -1},
            {"kind": "cut", "frame": 193},
            {"kind": "cut", "frame": 50, "contextStart": 60, "contextEnd": 60},
            {"kind": "cut", "frame": 50, "contextStart": 60, "contextEnd": 90},
            {"kind": "cut", "frame": 50, "contextStart": 10, "contextEnd": 193},
            {"kind": "cut", "frame": 50, "contextStart": 10},
            {"kind": "frame", "frame": 10, "versionId": "V1"},
            {"kind": "unknown", "frame": 10},
        ):
            with self.subTest(bad_target=bad_target), self.assertRaises(video_review.VideoError):
                video_review.clean_receipt_target(bad_target, v2_meta)

    def test_review_readiness_guards_state_evidence_and_media(self):
        v2_file = self.root / "v2.mp4"
        v2_file.write_bytes(self.v2_bytes)
        v2 = video_review.register_version(
            self.review, v2_file, batch=1, by="claude", review_id=self.rid, receipt_id="r-ready"
        )
        manifest = json.loads((self.review / "review.json").read_text(encoding="utf-8"))
        valid_target = video_review.clean_receipt_target({"kind": "frame", "frame": 108}, v2)
        ready_note = {
            "id": "n1",
            "page": 1,
            "status": "drafted",
            "resolution": "已避开图标",
            "video": {
                "v": 1,
                "versionId": "V1",
                "frame": 96,
                "fps": 30,
                "timebase": {"num": 1, "den": 30},
                "frames": 180,
                "videoWidth": 640,
                "videoHeight": 360,
            },
            "videoReview": {
                "v": 1,
                "state": "pending_review",
                "batch": 1,
                "versionId": "V2",
                "summary": "已避开图标",
                "target": valid_target,
            },
        }
        self.assertEqual(video_review.review_readiness(ready_note, manifest, review_dir=self.review), (True, "ok"))

        for bad_state in ("needs_reply", "needs_relocate", "failed", "missing_evidence", "returned"):
            bad = json.loads(json.dumps(ready_note))
            bad["videoReview"]["state"] = bad_state
            ok, _reason = video_review.review_readiness(bad, manifest, review_dir=self.review)
            self.assertFalse(ok, bad_state)

        no_summary = json.loads(json.dumps(ready_note))
        no_summary["videoReview"]["summary"] = "   "
        self.assertFalse(video_review.review_readiness(no_summary, manifest, review_dir=self.review)[0])

        no_target = json.loads(json.dumps(ready_note))
        no_target["videoReview"]["target"] = None
        self.assertFalse(video_review.review_readiness(no_target, manifest, review_dir=self.review)[0])

        bad_version = json.loads(json.dumps(ready_note))
        bad_version["videoReview"]["versionId"] = "V9"
        self.assertFalse(video_review.review_readiness(bad_version, manifest, review_dir=self.review)[0])

        drafted_without_vr = {"id": "n2", "page": 1, "status": "drafted", "video": ready_note["video"]}
        self.assertFalse(video_review.review_readiness(drafted_without_vr, manifest, review_dir=self.review)[0])

        plain_open = {"id": "n3", "page": 1, "status": "open", "video": ready_note["video"]}
        self.assertFalse(video_review.review_readiness(plain_open, manifest, review_dir=self.review)[0])

        # 媒体缺失或不可读时拒绝通过
        (self.review / "media" / "v2.mp4").unlink()
        self.assertFalse(video_review.review_readiness(ready_note, manifest, review_dir=self.review)[0])


class VideoReceiptStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="ae-video-receipt-store-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.v1_bytes = synthetic_mp4(frames=180, fps=30, width=640, height=360)
        self.v2_bytes = synthetic_mp4(frames=192, fps=30, width=640, height=360)
        self.v3_bytes = synthetic_mp4(frames=150, fps=30, width=640, height=360)
        self.v1_path = self.root / "proj" / "out" / "render.mp4"
        self.v1_path.parent.mkdir(parents=True, exist_ok=True)
        self.v1_path.write_bytes(self.v1_bytes)
        self.review = self.root / "review"
        build_viewer.build(self.v1_path, None, self.review, no_scan_skills=True, locale="zh-CN")
        manifest = json.loads((self.review / "review.json").read_text(encoding="utf-8"))
        manifest["video"] = video_source.link(
            manifest["video"],
            engine="remotion",
            root=str(self.root / "proj"),
            entry={"kind": "composition", "id": "Main"},
            media=V1_MEDIA,
            verified_source=True,
        )
        (self.review / "review.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
        self.rid = manifest["reviewId"]

        store.import_notes(
            self.review,
            [
                video_note(
                    "n1",
                    "字幕遮挡图标",
                    frame=96,
                    mark={"kind": "region", "x": 0.1, "y": 0.8, "width": 0.5, "height": 0.1},
                ),
                video_note("n2", "4 至 6 秒音乐太响", start=120, end=180),
                video_note("n3", "片头镜头要保留还是重拍", frame=12),
                video_note("n4", "转场节奏略快", start=60, end=90),
                video_note("n5", "结尾调色偏暗", frame=170),
            ],
        )

    def _v2_movie(self, data: bytes | None = None, name: str = "v2.mp4") -> Path:
        path = self.root / "proj" / "out" / name
        path.write_bytes(data if data is not None else self.v2_bytes)
        return path

    def test_deleted_shot_receipt_requires_context_on_each_available_side_of_the_cut(self):
        store.handoff_submit(self.review, note_ids=["n2"])
        store.handoff_claim(self.review)
        movie = self._v2_movie()

        def deliver(key, frame, start, end):
            target = {"kind": "cut", "frame": frame, "contextStart": start, "contextEnd": end}
            return store.video_receipt(
                self.review,
                receipt_id=key,
                finish=False,
                movie=movie if key == "interior" else None,
                receipt=[{"noteId": "n2", "outcome": "deleted", "summary": "移除合成镜头", "target": target}],
            )

        interior = deliver("interior", 60, 59, 61)
        original = store.load(self.review)
        for start, end in ((30, 60), (60, 90)):
            with self.subTest(start=start, end=end):
                with self.assertRaises(store.HandoffError) as invalid:
                    deliver(f"one-side-{start}", 60, start, end)
                self.assertEqual(invalid.exception.code, "invalid-receipt-target")
                self.assertEqual(store.load(self.review), original)
                with self.assertRaises(video_review.VideoError):
                    video_review.temporal_window(
                        {"kind": "cut", "frame": 60, "contextStart": start, "contextEnd": end}, interior["version"]
                    )
        for key, frame, start, end in (("head", 0, 0, 1), ("tail", 192, 191, 192)):
            with self.subTest(key=key):
                vr = deliver(key, frame, start, end)["notes"][0]["videoReview"]
                self.assertEqual(vr["state"], "pending_review")
                window = video_review.temporal_window(vr["target"], interior["version"])
                self.assertEqual((window["contextStart"], window["contextEnd"]), (start, end))

    def test_partial_completion_5_notes_submit_3_fix_2_question_1(self):
        store.handoff_submit(self.review, "先修前三条", target="final", note_ids=["n1", "n2", "n3"])
        store.handoff_claim(self.review, by="claude", seq=1)
        v2_file = self._v2_movie()

        result = store.video_receipt(
            self.review,
            seq=1,
            review_id=self.rid,
            receipt_id="rcpt-batch1",
            movie=v2_file,
            summary="V2 已调整字幕和背景音量，第 3 条待确认",
            by="claude",
            receipt={
                "notes": [
                    {
                        "noteId": "n1",
                        "outcome": "fixed",
                        "summary": "片头增加 12 帧并将字幕上移避开图标",
                        "target": {"kind": "frame", "frame": 108},
                    },
                    {
                        "noteId": "n2",
                        "outcome": "fixed",
                        "summary": "背景音乐压低 6dB（对应片头顺延 12 帧）",
                        "target": {"kind": "range", "startFrame": 132, "endFrame": 192},
                    },
                    {
                        "noteId": "n3",
                        "outcome": "question",
                        "question": "这段片头是要直接删掉还是只缩短到 1 秒？",
                    },
                ]
            },
        )
        self.assertTrue(result["ok"])
        self.assertEqual(result["version"]["versionId"], "V2")
        self.assertEqual(result["status"], "drafted", "即使 target=final，视频批注有待复核项也必须停在 drafted")

        by_id = {n["id"]: n for n in store.load(self.review)["notes"]}
        self.assertEqual(by_id["n1"]["status"], "drafted")
        self.assertEqual(by_id["n1"]["draftBatch"], 1)
        self.assertEqual(by_id["n1"]["resolution"], "片头增加 12 帧并将字幕上移避开图标")
        self.assertEqual(by_id["n1"]["video"]["versionId"], "V1")
        self.assertEqual(by_id["n1"]["video"]["frame"], 96)
        self.assertEqual(by_id["n1"]["videoReview"]["state"], "pending_review")
        self.assertEqual(by_id["n1"]["videoReview"]["target"]["frame"], 108)
        self.assertEqual(by_id["n1"]["videoReview"]["target"]["versionId"], "V2")

        self.assertEqual(by_id["n2"]["status"], "drafted")
        self.assertEqual(by_id["n2"]["videoReview"]["state"], "pending_review")
        self.assertEqual(
            (by_id["n2"]["videoReview"]["target"]["startFrame"], by_id["n2"]["videoReview"]["target"]["endFrame"]),
            (132, 192),
        )

        self.assertEqual(by_id["n3"]["status"], "open")
        self.assertEqual(by_id["n3"]["videoReview"]["state"], "needs_reply")
        self.assertEqual(by_id["n3"]["videoReview"]["question"], "这段片头是要直接删掉还是只缩短到 1 秒？")

        self.assertEqual(by_id["n4"]["status"], "open")
        self.assertNotIn("videoReview", by_id["n4"])
        self.assertEqual(by_id["n5"]["status"], "open")
        self.assertNotIn("videoReview", by_id["n5"])

        state = store.handoff_state(self.review, full=True)
        self.assertEqual(state["inFlightIds"], [])
        latest = state["latest"]
        self.assertEqual(latest["status"], "drafted")
        self.assertEqual(latest["draftedIds"], ["n1", "n2"])
        self.assertEqual(latest["draftedCount"], 2)
        self.assertEqual(latest["resolvedCount"], 0)
        self.assertEqual(latest["videoVersionId"], "V2")
        self.assertEqual(latest["videoStats"]["pendingReviewIds"], ["n1", "n2"])
        self.assertEqual(latest["videoStats"]["needsReplyIds"], ["n3"])
        self.assertEqual(latest["videoStats"]["unsubmittedIds"], ["n4", "n5"])
        self.assertEqual(latest["videoStats"]["unsubmittedCount"], 2)
        btxt = notes_cli.batches_text(state, store.load(self.review)["notes"], lang="zh-CN")
        self.assertIn("待复核 2，待答复 1，待定位 0，缺证据 0，失败 0，未提交 2", btxt)

    def test_missing_evidence_needs_relocate_cut_and_followup_history(self):
        store.handoff_submit(self.review, "交三条", note_ids=["n1", "n2", "n3"])
        store.handoff_claim(self.review, by="claude", seq=1)
        v2_file = self._v2_movie()

        # 越界位置必须整体拒绝且不写脏 notes.json / review.json
        with self.assertRaises(store.HandoffError):
            store.video_receipt(
                self.review,
                seq=1,
                receipt_id="rcpt-oob",
                movie=v2_file,
                receipt={
                    "notes": [
                        {
                            "noteId": "n1",
                            "outcome": "fixed",
                            "summary": "越界帧",
                            "target": {"kind": "frame", "frame": 999},
                        }
                    ]
                },
            )
        self.assertEqual(json.loads((self.review / "review.json").read_text())["video"]["currentVersionId"], "V1")

        # 缺 target -> missing_evidence；deleted 带 cut -> pending_review；needs_relocate -> needs_relocate
        store.video_receipt(
            self.review,
            seq=1,
            receipt_id="rcpt-partial",
            movie=v2_file,
            finish=False,
            receipt={
                "notes": [
                    {"noteId": "n1", "outcome": "fixed", "summary": "字幕已改但暂未附坐标"},
                    {
                        "noteId": "n2",
                        "outcome": "deleted",
                        "summary": "已按意见删掉该段音乐",
                        "target": {"kind": "cut", "frame": 120, "contextStart": 100, "contextEnd": 140},
                    },
                    {"noteId": "n3", "outcome": "needs_relocate", "relocateReason": "原镜头拆分到了两个片段"},
                ]
            },
        )
        by_id = {n["id"]: n for n in store.load(self.review)["notes"]}
        self.assertEqual(by_id["n1"]["status"], "open")
        self.assertEqual(by_id["n1"]["videoReview"]["state"], "missing_evidence")
        self.assertEqual(by_id["n2"]["status"], "drafted")
        self.assertEqual(by_id["n2"]["videoReview"]["state"], "pending_review")
        self.assertEqual(by_id["n2"]["videoReview"]["target"]["kind"], "cut")
        self.assertEqual(by_id["n3"]["status"], "open")
        self.assertEqual(by_id["n3"]["videoReview"]["state"], "needs_relocate")

        # 同一批次续交补齐 n1 证据，保留 history 两轮记录
        store.video_receipt(
            self.review,
            seq=1,
            receipt_id="rcpt-supplement",
            finish=True,
            receipt={
                "notes": [
                    {
                        "noteId": "n1",
                        "outcome": "fixed",
                        "summary": "补齐 V2 帧号",
                        "versionId": "V2",
                        "target": {"kind": "frame", "frame": 108},
                    }
                ]
            },
        )
        by_id2 = {n["id"]: n for n in store.load(self.review)["notes"]}
        self.assertEqual(by_id2["n1"]["status"], "drafted")
        self.assertEqual(by_id2["n1"]["videoReview"]["state"], "pending_review")
        self.assertEqual(len(by_id2["n1"]["videoReview"]["history"]), 2)
        self.assertEqual(by_id2["n1"]["videoReview"]["history"][0]["state"], "missing_evidence")
        self.assertEqual(by_id2["n1"]["videoReview"]["history"][1]["state"], "pending_review")

    def test_render_failure_and_unplayable_movie_preserve_v1_and_allow_retry(self):
        store.handoff_submit(self.review, "修第一条", note_ids=["n1", "n2"])
        store.handoff_claim(self.review, by="claude", seq=1)

        fail_res = store.video_receipt(
            self.review,
            seq=1,
            receipt_id="rcpt-render-fail",
            render_error="Remotion render exited with code 1",
            by="claude",
        )
        self.assertTrue(fail_res["ok"])
        manifest = json.loads((self.review / "review.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["video"]["currentVersionId"], "V1")
        self.assertEqual(manifest["video"]["lastFailure"]["reason"], "Remotion render exited with code 1")
        by_id = {n["id"]: n for n in store.load(self.review)["notes"]}
        self.assertEqual(by_id["n1"]["status"], "open")
        self.assertEqual(by_id["n1"]["videoReview"]["state"], "failed")
        self.assertEqual(by_id["n2"]["status"], "open")
        self.assertEqual(by_id["n2"]["videoReview"]["state"], "failed")
        self.assertEqual(store.handoff_load(self.review)["items"][0]["status"], "claimed")

        # 产出一个不可播放的坏 MP4：同样拒绝并保留 V1 与未完成项
        bad_file = self._v2_movie(b"corrupt mp4 bytes", name="bad.mp4")
        with self.assertRaises(store.HandoffError):
            store.video_receipt(
                self.review,
                seq=1,
                receipt_id="rcpt-bad-mp4",
                movie=bad_file,
                by="claude",
                receipt={
                    "notes": [
                        {
                            "noteId": "n1",
                            "outcome": "fixed",
                            "summary": "改好了",
                            "target": {"kind": "frame", "frame": 96},
                        }
                    ]
                },
            )
        manifest2 = json.loads((self.review / "review.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest2["video"]["currentVersionId"], "V1")
        self.assertFalse((self.review / "media" / "v2.mp4").exists())
        by_id2 = {n["id"]: n for n in store.load(self.review)["notes"]}
        self.assertEqual(by_id2["n1"]["status"], "open")

        # 恢复后同一批次重试成功
        good_file = self._v2_movie(self.v2_bytes, name="good.mp4")
        ok_res = store.video_receipt(
            self.review,
            seq=1,
            receipt_id="rcpt-recover",
            movie=good_file,
            by="claude",
            receipt={
                "notes": [
                    {
                        "noteId": "n1",
                        "outcome": "fixed",
                        "summary": "已修复",
                        "target": {"kind": "frame", "frame": 108},
                    },
                    {
                        "noteId": "n2",
                        "outcome": "fixed",
                        "summary": "已调低音量",
                        "target": {"kind": "range", "startFrame": 132, "endFrame": 192},
                    },
                ]
            },
        )
        self.assertEqual(ok_res["version"]["versionId"], "V2")
        self.assertEqual(ok_res["status"], "drafted")
        manifest3 = json.loads((self.review / "review.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest3["video"]["currentVersionId"], "V2")
        self.assertNotIn("lastFailure", manifest3["video"])
        by_id3 = {n["id"]: n for n in store.load(self.review)["notes"]}
        self.assertGreaterEqual(len(by_id3["n1"]["videoReview"]["history"]), 2)
        self.assertEqual(by_id3["n1"]["videoReview"]["state"], "pending_review")

    def test_rejected_and_conflicting_receipts_discard_staged_version(self):
        store.handoff_submit(self.review, note_ids=["n1"])
        store.handoff_claim(self.review)
        movie = self._v2_movie()
        fixed = {"noteId": "n1", "outcome": "fixed", "summary": "改字幕"}
        with self.assertRaises(store.HandoffError) as invalid:
            store.video_receipt(self.review, seq=1, movie=movie, receipt=[{**fixed, "versionId": "V999"}])
        self.assertEqual(invalid.exception.code, "invalid-receipt-target")
        register = video_review.register_version

        def concurrent_edit(*args, **kwargs):
            version = register(*args, **kwargs)
            self.assertEqual(ledger.read_manifest(self.review)["video"]["currentVersionId"], "V1")
            store.update(self.review, "n1", {"text": "并发编辑"})
            return version

        with (
            patch.object(video_review, "register_version", concurrent_edit),
            self.assertRaises(store.HandoffError) as conflict,
        ):
            store.video_receipt(self.review, seq=1, movie=movie, receipt=[fixed])
        self.assertEqual(conflict.exception.code, "receipt-conflict")
        self.assertEqual([v["versionId"] for v in ledger.read_manifest(self.review)["video"]["versions"]], ["V1"])
        self.assertFalse((self.review / "media" / "v2.mp4").exists())
        self.assertFalse((self.review / ".video-registration.json").exists())
        self.assertNotIn("videoReceipts", store.handoff_load(self.review)["items"][0])
        self.assertEqual(
            store.video_receipt(self.review, seq=1, movie=movie, receipt=[fixed])["version"]["versionId"], "V2"
        )

    def test_failed_receipt_conflict_does_not_publish_a_failure(self):
        store.handoff_submit(self.review, note_ids=["n1"])
        store.handoff_claim(self.review)
        record_failure = video_review.record_failure
        bad_movie = self._v2_movie(b"invalid mp4", name="invalid.mp4")
        for other, arguments in (("n3", {"render_error": "render stopped"}), ("n4", {"movie": bad_movie})):
            with self.subTest(arguments=arguments):

                def submit_another_batch(*args, **kwargs):
                    failure = record_failure(*args, **kwargs)
                    self.assertNotIn("lastFailure", ledger.read_manifest(self.review)["video"])
                    store.handoff_submit(self.review, note_ids=[other])
                    return failure

                with (
                    patch.object(video_review, "record_failure", submit_another_batch),
                    self.assertRaises(store.HandoffError) as raised,
                ):
                    store.video_receipt(self.review, seq=1, receipt_id="failed", **arguments)
                self.assertEqual(raised.exception.code, "receipt-conflict")
                video = ledger.read_manifest(self.review)["video"]
                self.assertNotIn("lastFailure", video)
                self.assertNotIn("failures", video)
                self.assertNotIn("videoReceipts", store.handoff_load(self.review)["items"][0])
                self.assertFalse((self.review / ".video-registration.json").exists())

    def test_failure_publication_recovers_the_committed_receipt(self):
        store.handoff_submit(self.review, note_ids=["n1"])
        store.handoff_claim(self.review)
        with (
            patch.object(video_review, "finish_receipt_version", side_effect=OSError("interrupted")),
            self.assertRaises(OSError),
        ):
            store.video_receipt(self.review, seq=1, receipt_id="retry", render_error="original error")
        committed = store.handoff_load(self.review)["items"][0]["videoFailure"]
        self.assertNotIn("lastFailure", ledger.read_manifest(self.review)["video"])
        replay = store.video_receipt(self.review, seq=1, receipt_id="retry", render_error="different error")
        self.assertTrue(replay["replayed"])
        self.assertEqual(replay["failure"], committed)
        video = ledger.read_manifest(self.review)["video"]
        self.assertEqual(video["lastFailure"], committed)
        self.assertEqual(video["failures"], [committed])

    def test_historical_decode_retry_requires_the_current_render_base(self):
        wakeup.bind_wait(self.review, "Claude Code", "session-a")

        def fixed(nid, frame):
            return {
                "noteId": nid,
                "outcome": "fixed",
                "summary": "修改画面",
                "target": {"kind": "frame", "frame": frame},
            }

        original_fix = [fixed("n1", 96)]
        store.handoff_submit(self.review, note_ids=["n1"])
        self.assertEqual(notes_cli.claim_next(self.review, by="agent")[1]["seq"], 1)
        first = store.video_receipt(
            self.review, seq=1, receipt_id="first", movie=self._v2_movie(), receipt=original_fix
        )
        token = first["notes"][0]["videoReview"]["evidenceRevision"]
        store.handoff_submit(self.review, note_ids=["n3"])
        self.assertEqual(notes_cli.claim_next(self.review, by="agent")[1]["seq"], 2)
        second = store.video_receipt(
            self.review,
            seq=2,
            receipt_id="second",
            movie=self._v2_movie(self.v3_bytes, "second.mp4"),
            receipt=[fixed("n3", 12)],
        )
        self.assertEqual(second["currentVersionId"], "V3")
        approved = store.video_decision(self.review, "n3", action="approve")["note"]
        store.video_decode_failed(
            self.review,
            note_id="n1",
            version_id="V2",
            seq=1,
            expected_evidence=token,
            review_id=self.rid,
            reason="decoder rejected V2",
        )
        failed_notes = store.load(self.review)
        before = ledger.read_manifest(self.review)["video"]
        with self.assertRaises(store.HandoffError) as stale:
            store.video_receipt(self.review, seq=1, receipt_id="late", movie=self._v2_movie(), receipt=original_fix)
        self.assertEqual(stale.exception.code, "stale-render-base")
        self.assertEqual(stale.exception.params["current"], "V3")
        self.assertEqual(ledger.read_manifest(self.review)["video"], before)
        self.assertEqual(store.load(self.review), failed_notes)
        self.assertFalse((self.review / "media" / "v4.mp4").exists())
        self.assertEqual(store.handoff_load(self.review)["items"][0]["status"], "claimed")
        rebased_movie = self._v2_movie(synthetic_mp4(frames=200, fps=30, width=640, height=360), "rebased.mp4")
        result = store.video_receipt(
            self.review,
            seq=1,
            receipt_id="rebased",
            movie=rebased_movie,
            receipt={"baseVersionId": "V3", "notes": original_fix},
        )
        self.assertEqual((result["version"]["baseVersionId"], result["currentVersionId"]), ("V3", "V4"))
        self.assertEqual(next(n for n in store.load(self.review)["notes"] if n["id"] == "n3"), approved)
        with self.assertRaises(store.HandoffError) as failed_key:
            store.video_receipt(self.review, seq=1, receipt_id="first", movie=self._v2_movie(), receipt=original_fix)
        self.assertEqual(failed_key.exception.code, "receipt-decode-failed")
        replay = store.video_receipt(
            self.review, seq=1, receipt_id="rebased", movie=rebased_movie, receipt=original_fix
        )
        self.assertTrue(replay["replayed"])
        self.assertEqual((replay["version"]["versionId"], replay["currentVersionId"]), ("V4", "V4"))

    def test_legacy_batches_use_only_versions_known_before_submission(self):
        video = {
            "currentVersionId": "V3",
            "versions": [
                {"versionId": "V1"},
                {"versionId": "V2", "at": "2026-01-01T12:00:59Z"},
                {"versionId": "V3", "at": "2026-01-01T12:01:00Z"},
            ],
        }
        self.assertEqual(store._receipt_render_base({"at": "2026-01-01T20:01:00+08:00"}, video), "V2")
        self.assertEqual(store._receipt_render_base({}, video), "V1")
        self.assertEqual(store._receipt_render_base({"videoVersionId": "V3"}, video), "V3")

    def test_committed_receipt_resumes_interrupted_version_publication(self):
        store.handoff_submit(self.review, note_ids=["n1"])
        store.handoff_claim(self.review)
        receipt = [{"noteId": "n1", "outcome": "fixed", "summary": "改字幕", "target": {"frame": 12}}]
        with (
            patch.object(video_review, "finish_receipt_version", side_effect=OSError("interrupted")),
            self.assertRaises(OSError),
        ):
            store.video_receipt(self.review, seq=1, receipt_id="receipt", movie=self._v2_movie(), receipt=receipt)
        self.assertEqual(ledger.read_manifest(self.review)["video"]["currentVersionId"], "V1")
        self.assertEqual(store.handoff_load(self.review)["items"][0]["videoReceipts"][0]["versionId"], "V2")
        replay = store.video_receipt(self.review, seq=1, receipt_id="receipt", receipt=receipt)
        self.assertTrue(replay["replayed"])
        self.assertEqual(replay["version"]["versionId"], "V2")
        self.assertEqual(ledger.read_manifest(self.review)["video"]["currentVersionId"], "V2")

    def test_import_and_add_check_registered_anchor(self):
        for fields in ({"versionId": "V3"}, {"frames": 200, "frame": 193}, {"videoWidth": 320}):
            note = video_note("bad", "错误定位", frame=0)
            note["video"].update(fields)
            with self.subTest(fields=fields):
                with self.assertRaises(store.NoteError):
                    store.import_notes(self.review, [note])
                with self.assertRaises(store.NoteError):
                    store.add(self.review, note)
                self.assertNotIn("bad", [n["id"] for n in store.load(self.review)["notes"]])

    def test_received_fix_requires_a_render_from_the_claimed_batch(self):
        store.handoff_submit(self.review, note_ids=["n1"])
        store.handoff_claim(self.review)
        with self.assertRaises(store.HandoffError) as why:
            store.video_receipt(
                self.review,
                seq=1,
                by="agent",
                review_id=self.rid,
                receipt_id="wrong-cut",
                receipt={
                    "notes": [
                        {
                            "noteId": "n1",
                            "outcome": "fixed",
                            "summary": "字幕已调整",
                            "versionId": "V1",
                            "target": {"kind": "frame", "frame": 96},
                        }
                    ]
                },
            )
        self.assertEqual(why.exception.code, "invalid-receipt-target")
        self.assertEqual(store.load(self.review)["notes"][0]["status"], "open")

    def test_idempotency_and_stale_cross_review_and_patch_import_protection(self):
        store.handoff_submit(self.review, "第一批", note_ids=["n1"])
        store.handoff_claim(self.review, by="claude", seq=1)
        v2_file = self._v2_movie()

        first = store.video_receipt(
            self.review,
            seq=1,
            review_id=self.rid,
            receipt_id="rcpt-same",
            movie=v2_file,
            by="claude",
            receipt={
                "notes": [
                    {"noteId": "n1", "outcome": "fixed", "summary": "改好了", "target": {"kind": "frame", "frame": 108}}
                ]
            },
        )
        # 同 (seq, receiptId) 幂等重放，即使 batch 1 已转为 drafted，也原样返回不新建版本/历史
        replay = store.video_receipt(
            self.review,
            seq=1,
            review_id=self.rid,
            receipt_id="rcpt-same",
            movie=v2_file,
            by="claude",
            receipt={
                "notes": [
                    {"noteId": "n1", "outcome": "fixed", "summary": "改好了", "target": {"kind": "frame", "frame": 108}}
                ]
            },
        )
        self.assertEqual(replay["version"]["versionId"], first["version"]["versionId"])
        manifest = json.loads((self.review / "review.json").read_text(encoding="utf-8"))
        self.assertEqual([v["versionId"] for v in manifest["video"]["versions"]], ["V1", "V2"])
        n1 = next(n for n in store.load(self.review)["notes"] if n["id"] == "n1")
        self.assertEqual(len(n1["videoReview"]["history"]), 1)

        # 陈旧批次（batch 1 已离开 claimed，带新 receiptId）拒绝
        with self.assertRaises(store.HandoffError):
            store.video_receipt(
                self.review,
                seq=1,
                receipt_id="rcpt-stale-seq",
                receipt={"notes": [{"noteId": "n1", "outcome": "question", "question": "覆盖？"}]},
            )

        # 第 2 批：提交 n2，拒绝跨审阅、批次外批注、接收方拒绝、以及已通过条目的覆盖
        store.handoff_submit(self.review, "第二批", note_ids=["n2"])
        store.handoff_claim(self.review, by="claude", seq=2)
        with self.assertRaises(store.HandoffError) as cross:
            store.video_receipt(
                self.review, seq=2, review_id="0000000000000000", receipt_id="rcpt-x", receipt={"notes": []}
            )
        self.assertEqual(cross.exception.code, "review-mismatch")

        with self.assertRaises(store.HandoffError):
            store.video_receipt(
                self.review,
                seq=2,
                receipt_id="rcpt-outside",
                receipt={"notes": [{"noteId": "n3", "outcome": "question", "question": "不在第2批"}]},
            )

        def deny():
            raise ValueError("wrong session")

        with self.assertRaises(ValueError):
            store.video_receipt(self.review, seq=2, receipt_id="rcpt-auth", authorize=deny, receipt={"notes": []})

        # 页面通用 update/PATCH 不能篡改 videoReview；import 不能修改已有 videoReview 或伪造新条目的 videoReview
        before_vr = copy.deepcopy(n1["videoReview"])
        store.update(self.review, "n1", {"text": "补一句说明", "videoReview": {"state": "hacked"}})
        n1_after = next(n for n in store.load(self.review)["notes"] if n["id"] == "n1")
        self.assertEqual(n1_after["videoReview"], before_vr)

        with self.assertRaises(store.NoteError):
            store.import_notes(
                self.review,
                [
                    {
                        **n1_after,
                        "updatedAt": "2099-01-01T00:00:00+00:00",
                        "videoReview": {"state": "tampered"},
                    }
                ],
            )
        forged = video_note("forged", "伪造", frame=10)
        forged["videoReview"] = {"v": 1, "state": "pending_review", "summary": "fake"}
        store.import_notes(self.review, [forged])
        forged_saved = next(n for n in store.load(self.review)["notes"] if n["id"] == "forged")
        self.assertNotIn("videoReview", forged_saved)

        # 已人工通过的批注不能被回执覆盖
        store.set_status(self.review, "n1", "resolved", by="page", reviewer=True)
        # 即使人工在另一处写入较新记录或有人尝试传回执给已 resolved 的 n1
        with self.assertRaises(store.HandoffError):
            store.video_receipt(
                self.review,
                seq=2,
                receipt_id="rcpt-resolved",
                receipt={"notes": [{"noteId": "n1", "outcome": "question", "question": "再改？"}]},
            )

    def test_manual_approval_gate_and_no_nested_locks(self):
        store.handoff_submit(self.review, "交前四条", target="final", note_ids=["n1", "n2", "n3", "n4"])
        store.handoff_claim(self.review, by="claude", seq=1)
        v2_file = self._v2_movie()
        store.video_receipt(
            self.review,
            seq=1,
            receipt_id="rcpt-gate",
            movie=v2_file,
            finish=False,
            receipt={
                "notes": [
                    {"noteId": "n1", "outcome": "fixed", "summary": "ok", "target": {"kind": "frame", "frame": 108}},
                    {"noteId": "n2", "outcome": "question", "question": "确认音量？"},
                    {"noteId": "n3", "outcome": "needs_relocate", "relocateReason": "找不到对应片段"},
                    {"noteId": "n4", "outcome": "fixed", "summary": "缺定位"},
                ]
            },
        )
        # handoff_done 即使 target=final 也绝不把 drafted 标为 resolved
        done_item = store.handoff_done(self.review, seq=1, summary="完成首轮", by="claude")
        self.assertEqual(done_item["status"], "drafted")
        by_id = {n["id"]: n for n in store.load(self.review)["notes"]}
        self.assertEqual(by_id["n1"]["status"], "drafted")

        # Agent 不能直接标为 resolved 或 review --ok
        with self.assertRaises(store.HandoffError) as exc1:
            store.set_status(self.review, "n1", "resolved", by="claude")
        self.assertEqual(exc1.exception.code, "video-manual-approval-required")

        # 非完备条目（待答复、待定位、缺证据）即使 by="page" 也绝不能通过
        for bad_id in ("n2", "n3", "n4"):
            with self.subTest(bad_id=bad_id), self.assertRaises(store.HandoffError) as exc2:
                store.set_status(self.review, bad_id, "resolved", by="page", reviewer=True)
            self.assertEqual(exc2.exception.code, "video-not-ready")

        # _finalizable 只列 n1；handoff_finalize_all 不能夹带 n2/n3/n4
        state = store.handoff_state(self.review, full=False)
        self.assertEqual(state["finalizable"]["batches"], [{"seq": 1, "ids": ["n1"]}])
        res = store.handoff_finalize_all(self.review, {1: {"n1", "n2", "n3", "n4"}})
        self.assertEqual(res["finalizedIds"], ["n1"])
        self.assertEqual({s["id"] for s in res["skipped"]}, {"n2", "n3", "n4"})

        # 已明确逐项提问且没有 drafted 的批次可以收尾，resolvedCount = 0。
        store.handoff_submit(self.review, "只问 n5", target="final", note_ids=["n5"])
        store.handoff_claim(self.review, by="claude", seq=2)
        store.video_receipt(
            self.review,
            seq=2,
            receipt_id="question",
            finish=False,
            receipt=[{"noteId": "n5", "outcome": "question", "question": "需要较亮的片尾吗？"}],
        )
        done2 = store.handoff_done(self.review, seq=2, summary="无改动", by="claude")
        self.assertEqual(done2["status"], "done")
        self.assertEqual(done2["resolvedCount"], 0)
        n5 = next(n for n in store.load(self.review)["notes"] if n["id"] == "n5")
        self.assertEqual(n5["status"], "open")

    def test_video_receipt_never_nests_store_and_metadata_locks(self):
        store.handoff_submit(self.review, "锁顺序检查", note_ids=["n1"])
        store.handoff_claim(self.review, by="claude", seq=1)
        v2_file = self._v2_movie()

        held = {"store": False, "meta": False}
        orig_store_locked = store.locked
        orig_meta_lock = ledger.metadata_lock
        import contextlib

        @contextlib.contextmanager
        def track_store(*args, **kwargs):
            self.assertFalse(held["meta"], "不得在持有 metadata_lock 时再取 store.locked")
            held["store"] = True
            try:
                with orig_store_locked(*args, **kwargs):
                    yield
            finally:
                held["store"] = False

        @contextlib.contextmanager
        def track_meta(*args, **kwargs):
            self.assertFalse(held["store"], "不得在持有 store.locked 时再取 metadata_lock")
            held["meta"] = True
            try:
                with orig_meta_lock(*args, **kwargs):
                    yield
            finally:
                held["meta"] = False

        old_store, old_meta = store.locked, ledger.metadata_lock
        store.locked = track_store
        ledger.metadata_lock = track_meta
        try:
            store.video_receipt(
                self.review,
                seq=1,
                receipt_id="rcpt-locks",
                movie=v2_file,
                receipt={
                    "notes": [
                        {
                            "noteId": "n1",
                            "outcome": "fixed",
                            "summary": "锁不嵌套",
                            "target": {"kind": "frame", "frame": 108},
                        }
                    ]
                },
            )
        finally:
            store.locked = old_store
            ledger.metadata_lock = old_meta


class VideoReceiptCliAndServerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="ae-video-receipt-cli-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.v1_bytes = synthetic_mp4(frames=180, fps=30, width=640, height=360)
        self.v2_bytes = synthetic_mp4(frames=192, fps=30, width=640, height=360)
        self.v1_path = self.root / "proj" / "out" / "render.mp4"
        self.v1_path.parent.mkdir(parents=True, exist_ok=True)
        self.v1_path.write_bytes(self.v1_bytes)
        self.review = self.root / "review"
        build_viewer.build(self.v1_path, None, self.review, no_scan_skills=True, locale="zh-CN")
        manifest = json.loads((self.review / "review.json").read_text(encoding="utf-8"))
        manifest["video"] = video_source.link(
            manifest["video"],
            engine="remotion",
            root=str(self.root / "proj"),
            entry={"kind": "composition", "id": "Main"},
            media=V1_MEDIA,
            verified_source=True,
        )
        (self.review / "review.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
        self.rid = manifest["reviewId"]
        store.import_notes(
            self.review,
            [
                video_note(
                    "n1",
                    "字幕遮挡图标",
                    frame=96,
                    mark={"kind": "region", "x": 0.1, "y": 0.8, "width": 0.5, "height": 0.1},
                ),
                video_note("n2", "4 至 6 秒音乐太响", start=120, end=180),
                video_note("n3", "片头镜头要保留还是重拍", frame=12),
                video_note("n4", "转场节奏略快", start=60, end=90),
                video_note("n5", "结尾调色偏暗", frame=170),
            ],
        )
        wakeup.bind_wait(self.review, "Claude Code", "session-a")

    def run_cli(self, argv: list[str], *, stdin_text: str | None = None, env_extra: dict[str, str] | None = None):
        old_env = dict(os.environ)
        old_stdin = sys.stdin
        os.environ["AE_HARNESS"] = "Claude Code"
        os.environ["AE_SESSION_ID"] = "session-a"
        if env_extra:
            os.environ.update(env_extra)
        if stdin_text is not None:
            sys.stdin = io.StringIO(stdin_text)
        out, err = io.StringIO(), io.StringIO()
        try:
            with redirect_stdout(out), redirect_stderr(err):
                code = run_video_cli(notes_cli.main, argv)
        finally:
            sys.stdin = old_stdin
            os.environ.clear()
            os.environ.update(old_env)
        return code, out.getvalue(), err.getvalue()

    def _receipt_for_decode_recovery(self):
        store.handoff_submit(self.review, note_ids=["n1"])
        store.handoff_claim(self.review, by="shared display name")
        result = store.video_receipt(
            self.review,
            seq=1,
            movie=self.v1_path,
            receipt_id="original",
            receipt=[{"noteId": "n1", "outcome": "fixed", "summary": "字幕", "target": {"frame": 96}}],
        )
        return dict(
            note_id="n1",
            version_id="V2",
            seq=1,
            review_id=self.rid,
            expected_evidence=result["notes"][0]["videoReview"]["evidenceRevision"],
            reason="decoder rejected V2",
        )

    def test_live_notes_and_batch_history_keep_render_diagnostics_in_local_cli(self):
        store.handoff_submit(self.review, note_ids=["n1", "n2"])
        self.assertEqual(self.run_cli(["claim", str(self.review)])[0], 0)
        reason = "renderer could not read /synthetic/private/overlay.png"
        explanation = "The edit needs the original title card"
        code, _, err = self.run_cli(
            ["video-receipt", str(self.review), "--receipt-id", "attempt", "--render-error", reason, "--receipt", "-"],
            stdin_text=json.dumps(
                [
                    {"noteId": "n1", "outcome": "failed"},
                    {"noteId": "n2", "outcome": "failed", "failure": explanation},
                ]
            ),
        )
        self.assertEqual(code, 0, err)
        code, output, err = self.run_cli(["show", str(self.review), "--locale", "en"])
        self.assertEqual(code, 0, err)
        self.assertIn(reason, output)
        for locale, summary in (("en", "Engine render failed"), ("zh-CN", "引擎渲染失败")):
            with self.subTest(locale=locale):
                server = serve.ReviewServer(self.review, 0, log_to_file=False, locale=locale)
                worker = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
                worker.start()
                self.addCleanup(worker.join, 2)
                self.addCleanup(server.server_close)
                self.addCleanup(server.shutdown)
                host = f"127.0.0.1:{server.port}"
                headers = {"Host": host, "Origin": "http://" + host, "Content-Type": "application/json"}
                for method, path, body in (
                    ("GET", "/api/notes", None),
                    ("GET", "/api/handoff", None),
                    ("POST", "/api/notes/import", {"notes": []}),
                    ("PATCH", "/api/notes/n1", {"text": "Recheck the title"}),
                ):
                    with self.subTest(method=method, path=path):
                        status, _, raw = serve._req(server.port, method, path, headers, body)
                        self.assertEqual(status, 200, raw)
                        self.assertNotIn(reason.encode(), raw)
                        data = json.loads(raw)
                        doc = data.get("doc", data)
                        if "notes" in doc:
                            n1, n2 = doc["notes"][:2]
                            self.assertEqual(n1["videoReview"]["failure"], summary)
                            self.assertEqual(n1["videoReview"]["history"][-1]["failure"], summary)
                            self.assertEqual(n1["trail"][-1]["note"], summary)
                            self.assertEqual(n2["videoReview"]["failure"], explanation)
                        handoff = data.get("handoff", data)
                        for batch in [handoff.get("latest"), *(handoff.get("history") or [])]:
                            if batch:
                                self.assertEqual(batch["videoFailure"]["reason"], summary)
                                receipt = batch["videoReceipts"][-1]
                                self.assertEqual(receipt["failure"]["reason"], summary)
                                self.assertEqual([i["failure"] for i in receipt["items"]], [summary, explanation])
        self.assertEqual(store.load(self.review)["notes"][0]["videoReview"]["failure"], reason)

    def test_cli_translates_recovery_diagnostics_for_damaged_and_foreign_records(self):
        store.handoff_submit(self.review, note_ids=["n1"])
        self.assertEqual(self.run_cli(["claim", str(self.review)])[0], 0)
        journal = self.review / store.VIDEO_COMMIT_FILE
        for raw, detail in ((b"{", "damaged (JSON)"), (b'{"reviewId":"abababababababab"}', "another review")):
            with self.subTest(raw=raw):
                journal.write_bytes(raw)
                journal.chmod(0o600)
                code, _, error = self.run_cli(
                    ["video-receipt", str(self.review), "--locale", "en", "--render-error", "test"]
                )
                self.assertNotEqual(code, 0)
                self.assertIn(detail, error)
                self.assertIn("Recover video review", error)
                self.assertEqual(journal.read_bytes(), raw)

    def test_legacy_failure_trails_and_page_exports_round_trip_without_losing_private_records(self):
        store.handoff_submit(self.review, note_ids=["n1", "n2"])
        self.assertEqual(self.run_cli(["claim", str(self.review)])[0], 0)
        private = "legacy diagnostic /synthetic/inputs/overlay.mkv"
        code, _, err = self.run_cli(
            ["video-receipt", str(self.review), "--receipt-id", "old", "--render-error", private]
        )
        self.assertEqual(code, 0, err)
        # 旧批注没有阶段标记。第二条仅保留处理痕迹。
        notes = store.load(self.review)
        for note in notes["notes"][:2]:
            review = note["videoReview"]
            for row in [review, *review["history"], *note["trail"]]:
                row.pop("failureStage", None)
        notes["notes"][1].pop("videoReview")
        (self.review / store.NOTES_FILE).write_text(json.dumps(notes))
        with serve.ReviewServer(self.review, 0, log_to_file=False, locale="en") as server:
            worker = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
            worker.start()
            try:
                host = f"127.0.0.1:{server.port}"
                headers = {"Host": host, "Origin": "http://" + host, "Content-Type": "application/json"}
                status, _, raw = serve._req(server.port, "GET", "/api/notes", headers)
                self.assertEqual(status, 200, raw)
                exported = json.loads(raw)["notes"]
                with self.subTest("legacy trail"):
                    self.assertEqual(exported[1]["trail"][-1]["note"], "Engine render failed")
                with self.subTest("import current public copy"):
                    status, _, raw = serve._req(server.port, "POST", "/api/notes/import", headers, {"notes": exported})
                    self.assertEqual(status, 200, raw)
                    self.assertNotIn(private.encode(), raw)
                    self.assertEqual(store.load(self.review)["notes"], notes["notes"])
                server.locale = "zh-CN"
                status, _, raw = serve._req(server.port, "POST", "/api/notes/import", headers, {"notes": exported})
                self.assertEqual(status, 200, raw)
                exported[0]["videoReview"]["decision"] = "approved"
                self.assertEqual(
                    serve._req(server.port, "POST", "/api/notes/import", headers, {"notes": exported})[0], 400
                )
            finally:
                server.shutdown()
                worker.join(2)

    def test_deletion_records_withdrawal_without_hiding_a_missing_receipt(self):
        store.handoff_submit(self.review, note_ids=["n1", "n2", "n3", "n4"])
        self.assertEqual(self.run_cli(["claim", str(self.review)])[0], 0)
        store.delete(self.review, "n1")
        # 升级前，删除普通批注只更新 notes.json，未向原批次记录撤回。
        old = store.load(self.review)
        old["notes"] = [n for n in old["notes"] if n["id"] != "n2"]
        (self.review / store.NOTES_FILE).write_text(json.dumps(old))
        with self.assertRaises(store.HandoffError) as missing:
            store.video_receipt(self.review, receipt=[{"noteId": "n3", "outcome": "question", "question": "换色吗？"}])
        self.assertEqual(missing.exception.params["ids"], ["n4"])
        result = store.video_receipt(
            self.review,
            receipt=[{"noteId": nid, "outcome": "question", "question": "换色吗？"} for nid in ("n3", "n4")],
        )
        self.assertEqual(result["status"], "done")
        batch = store.handoff_state(self.review)["history"][0]
        self.assertEqual(batch["videoWithdrawals"]["n1"]["by"], "page")
        self.assertEqual(batch["videoWithdrawals"]["n2"]["source"], "missing-note")
        self.assertIn("n1", batch["videoWithdrawals"])
        self.assertEqual([i["noteId"] for i in batch["videoReceipts"][0]["items"]], ["n3", "n4"])

    def test_legacy_agent_review_recovers_current_work_and_ready_reviewer_decisions(self):
        def old_stage(seq, state):
            handoff = store.handoff_load(self.review)
            next(b for b in handoff["items"] if b["seq"] == seq)["status"] = state
            (self.review / store.HANDOFF_FILE).write_text(json.dumps(handoff))

        store.handoff_submit(self.review, note_ids=["n1", "n2"])
        self.assertEqual(self.run_cli(["claim", str(self.review)])[0], 0)
        store.video_receipt(
            self.review,
            receipt_id="partial",
            finish=False,
            receipt=[{"noteId": "n1", "outcome": "question", "question": "用原色吗？"}],
        )
        store.video_decision(self.review, "n1", action="reply", reply="是")
        retained = store.load(self.review)["notes"][0]
        old_stage(1, "reviewing")
        store.handoff_submit(self.review, note_ids=["n3"])
        code, output, err = self.run_cli(
            ["video-receipt", str(self.review), "--seq", "1", "--receipt-id", "complete", "--receipt", "-", "--json"],
            stdin_text=json.dumps([{"noteId": "n2", "outcome": "question", "question": "用原音吗？"}]),
        )
        self.assertEqual(code, 0, err)
        self.assertEqual(json.loads(output)["status"], "done")
        self.assertEqual(store.load(self.review)["notes"][0], retained)
        self.assertEqual(self.run_cli(["claim", str(self.review), "--seq", "2"])[0], 0)
        store.video_receipt(
            self.review,
            seq=2,
            movie=self.v1_path,
            receipt=[{"noteId": "n3", "outcome": "fixed", "summary": "片头已缩短", "target": {"frame": 10}}],
        )
        old_stage(2, "review")
        store.handoff_submit(self.review, note_ids=["n4"])
        code, output, err = self.run_cli(["claim", str(self.review)])
        self.assertEqual(code, 0, err)
        self.assertEqual(json.loads(output)["seq"], 3)
        self.assertEqual(store.handoff_load(self.review)["items"][1]["status"], "drafted")
        before = store.handoff_load(self.review)
        store.recover_video_writes(self.review, self.rid)
        self.assertEqual(store.handoff_load(self.review), before)
        self.assertEqual(store.video_decision(self.review, "n3", action="approve")["note"]["status"], "resolved")

    def test_prior_approval_stays_in_the_finished_batch_when_legacy_receipt_details_are_absent(self):
        store.handoff_submit(self.review, note_ids=["n1", "n3"])
        store.handoff_claim(self.review)
        store.video_receipt(
            self.review,
            receipt_id="first-cut",
            movie=self.v1_path,
            finish=False,
            receipt=[{"noteId": "n1", "outcome": "fixed", "summary": "字幕上移", "target": {"frame": 30}}],
        )
        store.video_decision(self.review, "n1", action="approve")
        notes = store.load(self.review)
        approved = next(n for n in notes["notes"] if n["id"] == "n1")
        approved.pop("videoReview")
        (self.review / store.NOTES_FILE).write_text(json.dumps(notes))
        handoff = store.handoff_load(self.review)
        handoff["items"][0].pop("videoReceipts")
        (self.review / store.HANDOFF_FILE).write_text(json.dumps(handoff))
        result = store.video_receipt(
            self.review,
            receipt=[
                {"noteId": "n3", "outcome": "fixed", "summary": "片头已缩短", "target": {"frame": 12}},
            ],
        )
        self.assertEqual(result["status"], "drafted")
        self.assertEqual(result["batch"]["resolvedIds"], ["n1"])
        self.assertEqual(next(n for n in store.load(self.review)["notes"] if n["id"] == "n1"), approved)

    def test_finishing_legacy_partial_batch_counts_its_history_and_decision_only_for_that_batch(self):
        store.handoff_submit(self.review, note_ids=["n1", "n2", "n3"])
        store.handoff_claim(self.review)
        store.video_receipt(
            self.review,
            receipt_id="partial",
            finish=False,
            receipt=[
                {"noteId": "n1", "outcome": "question", "question": "字幕上移吗？"},
                {"noteId": "n2", "outcome": "question", "question": "保留原音吗？"},
            ],
        )
        store.video_decision(self.review, "n2", action="reply", reply="保留")
        # 旧份本：n1 只有逐条历史，n2 只有人工决定，批次汇总未写入。
        handoff = store.handoff_load(self.review)
        del handoff["items"][0]["videoReceipts"]
        (self.review / store.HANDOFF_FILE).write_text(json.dumps(handoff))
        notes = store.load(self.review)
        for n in notes["notes"]:
            if n["id"] in ("n1", "n2"):
                vr = n["videoReview"]
                vr.pop("receiptId")
                if n["id"] == "n2":
                    vr["history"] = [h for h in vr["history"] if h.get("decision")]
        (self.review / store.NOTES_FILE).write_text(json.dumps(notes))
        entry = {"noteId": "n3", "outcome": "failed", "failure": "缺少合成工程素材"}
        final = store.video_receipt(self.review, receipt_id="last", receipt=[entry])
        self.assertEqual(final["status"], "done")
        self.assertEqual([n["id"] for n in final["notes"]], ["n3"])
        store.handoff_submit(self.review, note_ids=["n1", "n2"])
        store.handoff_claim(self.review)
        with self.assertRaises(store.HandoffError) as missing:
            store.video_receipt(
                self.review, seq=2, receipt=[{"noteId": "n1", "outcome": "question", "question": "要改色吗？"}]
            )
        self.assertEqual(missing.exception.params["ids"], ["n2"])

    def test_implicit_render_failure_preserves_reviewer_decisions_and_ready_cut(self):
        store.handoff_submit(self.review, note_ids=["n1", "n2", "n3", "n4", "n5"])
        store.handoff_claim(self.review)
        store.video_receipt(
            self.review,
            receipt_id="partial",
            finish=False,
            movie=self.v1_path,
            receipt=[
                {"noteId": "n1", "outcome": "fixed", "summary": "字幕上移", "target": {"frame": 30}},
                {
                    "noteId": "n2",
                    "outcome": "fixed",
                    "summary": "降低音乐",
                    "target": {"startFrame": 120, "endFrame": 180},
                },
                {"noteId": "n3", "outcome": "question", "question": "片头是否保留？"},
                {
                    "noteId": "n4",
                    "outcome": "fixed",
                    "summary": "转场变短",
                    "target": {"startFrame": 60, "endFrame": 80},
                },
            ],
        )
        store.video_decision(self.review, "n1", action="approve")
        store.video_decision(self.review, "n2", action="return", reason="还需调低")
        store.video_decision(self.review, "n3", action="reply", reply="保留")
        # 旧退回记录缺少批次号。原批次提交时尚无这个决定，后来显式重交才会携带它。
        legacy = store.load(self.review)
        returned = next(n["videoReview"] for n in legacy["notes"] if n["id"] == "n2")
        returned.pop("decisionBatch")
        returned.pop("batch")
        (self.review / store.NOTES_FILE).write_text(json.dumps(legacy))
        before = {n["id"]: n for n in store.load(self.review)["notes"]}
        reason = "合成片尾渲染中断"
        args = ["video-receipt", str(self.review), "--receipt-id", "render-error", "--render-error", reason, "--json"]
        code, output, err = self.run_cli(args)
        self.assertEqual(code, 0, err)
        result = json.loads(output)
        self.assertEqual((result["status"], result["currentVersionId"]), ("claimed", "V2"))
        self.assertEqual([n["id"] for n in result["notes"]], ["n5"])
        after = {n["id"]: n for n in store.load(self.review)["notes"]}
        for nid in ("n1", "n2", "n3", "n4"):
            self.assertEqual(after[nid], before[nid])
        self.assertEqual(after["n5"]["videoReview"]["failure"], reason)
        # 显式点名同批已退回项，仍须拒绝。
        code, _, err = self.run_cli(
            [*args, "--receipt-id", "explicit-error", "--receipt", "-"],
            stdin_text=json.dumps([{"noteId": "n2", "outcome": "failed"}]),
        )
        self.assertNotEqual(code, 0)
        self.assertIn("n2", err)
        store.video_receipt(self.review, seq=1, receipt_id="finish")
        store.handoff_submit(self.review, note_ids=["n2"])
        store.handoff_claim(self.review, seq=2)
        rework = store.video_receipt(
            self.review,
            seq=2,
            receipt=[
                {"noteId": "n2", "outcome": "question", "question": "音量再调低几分贝？"},
            ],
        )
        self.assertEqual(rework["notes"][0]["videoReview"]["question"], "音量再调低几分贝？")

    def test_cli_draft_finish_returns_questions_to_the_reviewer(self):
        store.handoff_submit(self.review, note_ids=["n3"], allow_subagents=True)
        store.handoff_claim(self.review)
        store.video_receipt(
            self.review,
            receipt_id="clarify",
            finish=False,
            receipt=[{"noteId": "n3", "outcome": "question", "question": "保留片头吗？"}],
        )
        code, out, err = self.run_cli(["done", str(self.review), "--seq", "1", "--draft"])
        self.assertEqual(code, 0, err)
        self.assertEqual(store.handoff_load(self.review)["items"][0]["status"], "done")
        self.assertIsNone(store.handoff_claim(self.review, stage="review"))
        self.assertIn("答复", out)

    def test_finish_names_uncovered_ids_and_accepts_prior_delivery_and_reviewer_reply(self):
        store.handoff_submit(self.review, note_ids=["n1", "n2", "n3", "n4"])
        store.handoff_claim(self.review)
        store.video_receipt(
            self.review,
            receipt_id="first",
            finish=False,
            movie=self.v1_path,
            receipt=[
                {"noteId": "n1", "outcome": "fixed", "summary": "字幕上移", "target": {"frame": 30}},
                {"noteId": "n3", "outcome": "question", "question": "片头是否保留？"},
            ],
        )
        store.video_decision(self.review, "n3", action="reply", reply="保留片头")
        earlier_notes = store.load(self.review)
        earlier_batch = store.handoff_load(self.review)
        args = [
            "video-receipt",
            str(self.review),
            "--receipt-id",
            "last",
            "--receipt",
            "-",
            "--movie",
            str(self.v1_path),
        ]
        failed = {"noteId": "n2", "outcome": "failed", "failure": "合成配乐缺少独立音轨"}
        for lang in ("zh-CN", "en"):
            code, _, err = self.run_cli([*args, "--locale", lang], stdin_text=json.dumps([failed]))
            self.assertNotEqual(code, 0)
            self.assertIn("n4", err)
            for covered in ("n1", "n2", "n3", "n5"):
                self.assertNotIn(covered, err)
        code, _, err = self.run_cli(["done", str(self.review)])
        self.assertNotEqual(code, 0)
        self.assertIn("n2", err)
        self.assertIn("n4", err)
        self.assertEqual(store.load(self.review), earlier_notes)
        self.assertEqual(store.handoff_load(self.review), earlier_batch)
        self.assertEqual(ledger.read_manifest(self.review)["video"]["currentVersionId"], "V2")
        relocated = {"noteId": "n4", "outcome": "needs_relocate", "relocateReason": "转场分成两段"}
        code, out, err = self.run_cli([*args, "--json"], stdin_text=json.dumps([failed, relocated]))
        self.assertEqual(code, 0, err)
        result = json.loads(out)
        self.assertEqual((result["status"], result["currentVersionId"]), ("drafted", "V3"))
        notes = {n["id"]: n for n in store.load(self.review)["notes"]}
        self.assertEqual(notes["n3"], next(n for n in earlier_notes["notes"] if n["id"] == "n3"))
        self.assertEqual(notes["n2"]["videoReview"]["failure"], failed["failure"])

    def test_workflow_receipt_examples_can_be_submitted_to_the_cli(self):
        store.handoff_submit(self.review, note_ids=["n1", "n2", "n3", "n4", "n5"])
        store.handoff_claim(self.review)
        references = SCRIPTS.parent / "references"
        for lang, ref, heading in (
            ("zh-CN", "formats.md", "逐条视频回执示例"),
            ("en", "formats.en.md", "Per-annotation video receipt example"),
        ):
            with self.subTest(lang=lang):
                anchor = heading.lower().replace(" ", "-")
                workflow = (references / f"workflow.{lang}.md").read_text(encoding="utf-8")
                self.assertTrue(f"({ref}#{anchor})" in workflow, f"{lang} 工作流缺少回执示例链接")
                section = (references / ref).read_text(encoding="utf-8").split("### " + heading + "\n", 1)[1]
                example = section.split("```json\n", 1)[1].split("\n```", 1)[0]
                args = [
                    "video-receipt",
                    str(self.review),
                    "--movie",
                    str(self.v1_path),
                    "--receipt",
                    "-",
                    "--json",
                    "--receipt-id",
                    f"example-{lang}",
                ]
                if lang == "zh-CN":
                    args.append("--no-finish")
                code, out, err = self.run_cli(args, stdin_text=example)
                self.assertEqual(code, 0, err)
                result = json.loads(out)
                self.assertFalse(result["replayed"])
                self.assertEqual(
                    {n["id"]: n["videoReview"]["state"] for n in result["notes"]},
                    {
                        "n1": "pending_review",
                        "n2": "pending_review",
                        "n3": "needs_reply",
                        "n4": "needs_relocate",
                        "n5": "failed",
                    },
                )

    def test_cli_lists_only_annotations_missing_evidence_in_this_receipt(self):
        store.handoff_submit(self.review, note_ids=["n1", "n2", "n3", "n4"])
        store.handoff_claim(self.review)
        command = ["video-receipt", str(self.review), "--receipt", "-"]
        earlier = [{"noteId": "n3", "outcome": "fixed", "summary": "尚缺新版位置"}]
        code, _, err = self.run_cli(
            [*command, "--no-finish", "--receipt-id", "opening"], stdin_text=json.dumps(earlier)
        )
        self.assertEqual(code, 0, err)
        final = [
            {"noteId": "n1", "outcome": "fixed", "summary": "字幕已上移"},
            {"noteId": "n2", "outcome": "fixed", "summary": "已调音量", "target": {"frame": 20}},
            {
                "noteId": "n4",
                "outcome": "fixed",
                "summary": "已缩短转场",
                "target": {"kind": "range", "startFrame": 24, "endFrame": 48},
            },
        ]
        args = [*command, "--movie", str(self.v1_path), "--receipt-id", "later"]
        for lang, label in (("zh-CN", "缺证据"), ("en", "missing evidence")):
            code, output, err = self.run_cli([*args, "--locale", lang], stdin_text=json.dumps(final))
            self.assertEqual(code, 0, err)
            self.assertIn(label, output.lower())
            for note_id in ("n1", "n2"):
                self.assertIn(note_id, output)
            for note_id in ("n3", "n4"):
                self.assertNotIn(note_id, output)

    def test_receipt_retry_with_a_new_claim_keeps_the_returned_note_and_original_batch(self):
        store.handoff_submit(self.review, note_ids=["n1"])
        self.assertEqual(self.run_cli(["claim", str(self.review)])[0], 0)
        command = [
            "video-receipt",
            str(self.review),
            "--receipt-id",
            "subtitle-fix",
            "--movie",
            str(self.v1_path),
            "--receipt",
            "-",
            "--json",
        ]
        payload = json.dumps([{"noteId": "n1", "outcome": "fixed", "summary": "字幕上移", "target": {"frame": 108}}])
        self.assertEqual(self.run_cli(command, stdin_text=payload)[0], 0)
        store.video_decision(self.review, "n1", action="return", reason="再向上移一行", by="page")
        store.handoff_submit(self.review, note_ids=["n1"])
        self.assertEqual(self.run_cli(["claim", str(self.review)])[0], 0)
        before = store.load(self.review)
        handoff = store.handoff_load(self.review)
        returned = next(n["videoReview"] for n in before["notes"] if n["id"] == "n1")
        self.assertEqual((returned["decision"], returned["returnReason"]), ("returned", "再向上移一行"))
        for args in ([], ["--seq", "1"]):
            code, output, err = self.run_cli([*command, *args], stdin_text=payload)
            self.assertEqual(code, 0, err)
            replay = json.loads(output)
            self.assertEqual((replay["seq"], replay["replayed"], replay["currentVersionId"]), (1, True, "V2"))
        code, _, err = self.run_cli([*command, "--seq", "2"], stdin_text=payload)
        self.assertNotEqual(code, 0)
        self.assertIn("--seq", err)
        self.assertEqual(store.load(self.review), before)
        self.assertEqual(store.handoff_load(self.review), handoff)
        self.assertEqual(ledger.read_manifest(self.review)["video"]["currentVersionId"], "V2")

    def test_incomplete_legacy_receipt_cannot_overwrite_a_later_return(self):
        self._receipt_for_decode_recovery()
        store.video_decision(self.review, "n1", action="return", reason="字幕仍然偏低")
        store.handoff_submit(self.review, note_ids=["n1"])
        store.handoff_claim(self.review, seq=2)
        handoff = store.handoff_load(self.review)
        handoff["items"][0].pop("videoReceipts")
        (self.review / store.HANDOFF_FILE).write_text(json.dumps(handoff))
        notes = store.load(self.review)
        vr = next(n["videoReview"] for n in notes["notes"] if n["id"] == "n1")
        for entry in [vr, *vr["history"]]:
            if entry.get("receiptId") == "original":
                entry.pop("batch")
        (self.review / store.NOTES_FILE).write_text(json.dumps(notes))

        # 只剩成片登记能证明是第 1 批。旧副本连这条批次号也缺失时同样须保留人工决定。
        command = ["video-receipt", str(self.review), "--receipt-id", "original", "--movie", str(self.v1_path)]
        for has_version_batch in (True, False):
            if not has_version_batch:
                manifest = ledger.read_manifest(self.review)
                manifest["video"]["versions"][1].pop("batch")
                (self.review / "review.json").write_text(json.dumps(manifest))
            code, _, err = self.run_cli([*command, "--seq", "2"])
            self.assertNotEqual(code, 0)
            self.assertIn("--seq", err)
            if not has_version_batch:
                code, _, err = self.run_cli(command)
                self.assertNotEqual(code, 0)
                self.assertIn("--seq", err)
        self.assertEqual(store.handoff_load(self.review), handoff)
        self.assertEqual(store.load(self.review), notes)
        self.assertEqual(ledger.read_manifest(self.review)["video"]["currentVersionId"], "V2")

    def test_legacy_receipt_key_shared_by_two_batches_requires_the_original_seq(self):
        for seq, nid in enumerate(["n1", "n2"], start=1):
            store.handoff_submit(self.review, note_ids=[nid])
            store.handoff_claim(self.review, seq=seq)
            store.video_receipt(
                self.review,
                seq=seq,
                receipt_id=f"part-{seq}",
                receipt=[{"noteId": nid, "outcome": "question", "question": "是否保留？"}],
            )
        handoff = store.handoff_load(self.review)
        # 旧版允许空清单收尾，也允许两批显式使用相同的标识。
        for item in handoff["items"]:
            item["videoReceipts"][0].update(receiptId="same-key", items=[])
        (self.review / store.HANDOFF_FILE).write_text(json.dumps(handoff))
        command = ["video-receipt", str(self.review), "--receipt-id", "same-key", "--json"]
        code, _, err = self.run_cli(command)
        self.assertNotEqual(code, 0)
        self.assertIn("--seq", err)
        for seq in (1, 2):
            code, output, err = self.run_cli([*command, "--seq", str(seq)])
            self.assertEqual(code, 0, err)
            self.assertEqual((json.loads(output)["seq"], json.loads(output)["replayed"]), (seq, True))
        self.assertEqual(store.handoff_load(self.review), handoff)

    def test_partial_receipt_requires_a_retry_key_and_allows_a_second_delivery(self):
        store.handoff_submit(self.review, note_ids=["n1", "n2"])
        self.assertEqual(self.run_cli(["claim", str(self.review)])[0], 0)
        command = ["video-receipt", str(self.review), "--movie", str(self.v1_path), "--receipt", "-", "--json"]
        entry = {"noteId": "n1", "outcome": "fixed", "summary": "字幕上移", "target": {"frame": 108}}
        initial = store.load(self.review)
        batch = store.handoff_load(self.review)
        code, _, err = self.run_cli([*command, "--no-finish"], stdin_text=json.dumps([entry]))
        self.assertNotEqual(code, 0)
        self.assertIn("--receipt-id", err)
        self.assertEqual(store.load(self.review), initial)
        self.assertEqual(store.handoff_load(self.review), batch)
        self.assertEqual(ledger.read_manifest(self.review)["video"]["currentVersionId"], "V1")

        first = json.dumps({"receiptId": "part-one", "notes": [entry]})
        # 第一次交回的输出未送达。接收会话只知道自己预先提供的标识。
        self.assertEqual(self.run_cli([*command, "--no-finish"], stdin_text=first)[0], 0)
        code, output, err = self.run_cli([*command, "--no-finish"], stdin_text=first)
        self.assertEqual(code, 0, err)
        replay = json.loads(output)
        self.assertEqual((replay["replayed"], replay["status"], replay["currentVersionId"]), (True, "claimed", "V2"))
        self.assertEqual(len(replay["batch"]["videoReceipts"]), 1)
        self.assertEqual(len(replay["notes"][0]["videoReview"]["history"]), 1)

        later = [{"noteId": "n2", "outcome": "question", "question": "音乐再轻一点吗？"}]
        code, output, err = self.run_cli([*command, "--receipt-id", "part-two"], stdin_text=json.dumps(later))
        self.assertEqual(code, 0, err)
        finished = json.loads(output)
        self.assertEqual(
            (finished["replayed"], finished["status"], finished["currentVersionId"]), (False, "drafted", "V3")
        )
        self.assertEqual([r["receiptId"] for r in finished["batch"]["videoReceipts"]], ["part-one", "part-two"])

    def test_repairing_a_decode_failure_requires_a_new_receipt_id(self):
        store.handoff_submit(self.review, note_ids=["n1", "n3"])
        self.assertEqual(self.run_cli(["claim", str(self.review)])[0], 0)
        command = ["video-receipt", str(self.review), "--movie", str(self.v1_path), "--receipt", "-", "--json"]
        entries = [
            {"noteId": nid, "outcome": "fixed", "summary": "合成修改", "target": {"frame": 12}} for nid in ("n1", "n3")
        ]
        code, output, error = self.run_cli([*command, "--receipt-id", "before"], stdin_text=json.dumps(entries))
        self.assertEqual(code, 0, error)
        landed = {n["id"]: n for n in json.loads(output)["notes"]}
        store.video_decision(self.review, "n1", action="approve")
        approved = next(n for n in store.load(self.review)["notes"] if n["id"] == "n1")
        store.video_decode_failed(
            self.review,
            note_id="n3",
            version_id="V2",
            seq=1,
            review_id=self.rid,
            expected_evidence=landed["n3"]["videoReview"]["evidenceRevision"],
            reason="synthetic decoder error",
        )
        self.v1_path.write_bytes(synthetic_mp4(frames=192))
        correction = json.dumps(entries[1:])
        for lang, prompt in (("zh-CN", "新"), ("en", "new")):
            code, _, error = self.run_cli([*command, "--receipt-id", "before", "--locale", lang], stdin_text=correction)
            self.assertNotEqual(code, 0)
            self.assertIn(prompt, error)
            self.assertIn("--receipt-id", error)
        self.assertEqual(ledger.read_manifest(self.review)["video"]["currentVersionId"], "V1")
        code, output, error = self.run_cli([*command, "--receipt-id", "after"], stdin_text=correction)
        self.assertEqual(code, 0, error)
        response = json.loads(output)
        self.assertEqual((response["seq"], response["currentVersionId"], response["status"]), (1, "V3", "drafted"))
        notes = {n["id"]: n for n in store.load(self.review)["notes"]}
        self.assertEqual(notes["n1"], approved)
        self.assertEqual(notes["n3"]["videoReview"]["state"], "pending_review")

    def test_reviewer_releases_only_the_failed_batch_and_can_submit_its_items_to_another_receiver(self):
        store.handoff_submit(self.review, note_ids=["n1", "n3"])
        self.assertEqual(self.run_cli(["claim", str(self.review)])[0], 0)
        entries = [
            {"noteId": nid, "outcome": "fixed", "summary": "合成", "target": {"frame": 12}} for nid in ("n1", "n3")
        ]
        result = store.video_receipt(self.review, seq=1, movie=self.v1_path, receipt_id="before", receipt=entries)
        store.video_decision(self.review, "n1", action="approve")
        failure = dict(
            note_id="n3",
            version_id="V2",
            seq=1,
            review_id=self.rid,
            expected_evidence=result["notes"][1]["videoReview"]["evidenceRevision"],
            reason="MediaError 3",
        )
        store.video_decode_failed(self.review, **failure)
        old_notes = store.load(self.review)["notes"]
        self.assertIn("n3", store.handoff_state(self.review)["inFlightIds"])
        with self.assertRaisesRegex(ValueError, "batch-in-progress"):
            wakeup.bind_wait(self.review, "Claude Code", "session-b", replace=True)

        with serve.ReviewServer(self.review, 0, log_to_file=False) as server:
            host = f"127.0.0.1:{server.port}"
            headers = {
                "Host": host,
                "Origin": "http://" + host,
                "Content-Type": "application/json",
                "X-AE-Review": self.rid,
            }
            worker = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
            worker.start()
            try:

                def release(seq=1, version="V2"):
                    status, _, raw = serve._req(
                        server.port, "POST", "/api/video/release-repair", headers, {"batch": seq, "versionId": version}
                    )
                    return status, json.loads(raw)

                code, body = release()
                self.assertEqual(code, 200, body)
                record = body["handoff"]["latest"]["videoRepairRelease"]
                self.assertEqual((record["versionId"], record["ids"], record["by"]), ("V2", ["n3"], "page"))
                self.assertTrue(record["at"])
                self.assertEqual(body["handoff"]["inFlightIds"], [])
                self.assertEqual(store.load(self.review)["notes"], old_notes)
                self.assertEqual(store.handoff_state(self.review)["total"], 1)
                self.assertEqual(
                    ledger.read_manifest(self.review)["video"]["versions"][1]["playbackFailure"]["stage"], "decode"
                )
                with redirect_stdout(io.StringIO()):
                    self.assertEqual(wakeup.main(["unbind", str(self.review)]), 0)
                wakeup.bind_wait(self.review, "Claude Code", "session-b")
                store.handoff_submit(self.review, note_ids=["n3"])
                other = {"AE_SESSION_ID": "session-b"}
                code, output, err = self.run_cli(["claim", str(self.review)], env_extra=other)
                self.assertEqual(code, 0, err)
                self.assertEqual(json.loads(output)["seq"], 2)
                before = store.handoff_load(self.review)
                self.assertGreaterEqual(release(2)[0], 400)
                self.assertEqual(release()[0], 200)
                self.assertEqual(store.handoff_load(self.review), before)
                with self.assertRaisesRegex(ValueError, "batch-in-progress"):
                    wakeup.bind_wait(self.review, "Claude Code", "session-a", replace=True)
                later = store.video_receipt(
                    self.review, seq=2, movie=self.v1_path, receipt=entries[1:], receipt_id="after"
                )
                self.assertEqual(later["version"]["versionId"], "V3")
                store.video_decision(self.review, "n3", action="approve")
                final_notes = store.load(self.review)["notes"]
                final_batches = store.handoff_load(self.review)
                for key in ("before", "late-key"):
                    command = [
                        "video-receipt",
                        str(self.review),
                        "--seq",
                        "1",
                        "--receipt-id",
                        key,
                        "--movie",
                        str(self.v1_path),
                        "--receipt",
                        "-",
                    ]
                    self.assertNotEqual(self.run_cli(command, stdin_text=json.dumps(entries[1:]))[0], 0)
                self.assertEqual(release()[0], 200)
                self.assertEqual(store.load(self.review)["notes"], final_notes)
                self.assertEqual(store.handoff_load(self.review), final_batches)
                by_id = {n["id"]: n for n in final_notes}
                self.assertEqual(by_id["n1"], next(n for n in old_notes if n["id"] == "n1"))
                self.assertEqual(by_id["n3"]["videoReview"]["decisionBatch"], 2)
                self.assertTrue(
                    any(
                        h.get("source") == "browser" and h.get("versionId") == "V2"
                        for h in by_id["n3"]["videoReview"]["history"]
                    )
                )
            finally:
                server.shutdown()
                worker.join(2)

    def test_unbound_receiver_can_restore_only_its_own_decode_repair(self):
        failure = self._receipt_for_decode_recovery()
        with redirect_stdout(io.StringIO()):
            self.assertEqual(wakeup.main(["unbind", str(self.review)]), 0)
        store.video_decode_failed(self.review, **failure)
        self.assertIsNone(wakeup.load_binding(self.review))
        with self.assertRaisesRegex(ValueError, "batch-in-progress"):
            wakeup.bind_wait(self.review, "Claude Code", "session-b")
        wakeup.bind_wait(self.review, "Claude Code", "session-a")
        repair = [{"noteId": "n1", "outcome": "fixed", "summary": "重渲染字幕", "target": {"frame": 96}}]
        code, output, error = self.run_cli(
            [
                "video-receipt",
                str(self.review),
                "--seq",
                "1",
                "--receipt-id",
                "repair",
                "--receipt",
                "-",
                "--movie",
                str(self.v1_path),
                "--json",
            ],
            stdin_text=json.dumps(repair),
        )
        self.assertEqual(code, 0, error)
        self.assertEqual(json.loads(output)["currentVersionId"], "V3")

    def test_changed_receiver_cannot_close_or_submit_the_original_decode_repair(self):
        failure = self._receipt_for_decode_recovery()
        wakeup.bind_wait(self.review, "Claude Code", "session-b", replace=True)
        store.handoff_submit(self.review, note_ids=["n3"])
        store.handoff_claim(self.review, by="shared display name")
        store.video_decode_failed(self.review, **failure)
        other = {"AE_SESSION_ID": "session-b"}
        previous = notes_cli.i18n.current_locale()
        code, _, error = self.run_cli(
            ["video-receipt", str(self.review), "--seq", "1", "--render-error", "failed", "--locale", "en"],
            env_extra=other,
        )
        self.assertNotEqual(code, 0)
        self.assertIn("original receiver", error)
        self.assertEqual(notes_cli.i18n.current_locale(), previous)
        code, _, error = self.run_cli(["done", str(self.review), "--seq", "1", "--locale", "en"], env_extra=other)
        self.assertNotEqual(code, 0)
        self.assertIn("original receiver", error)
        with self.assertRaisesRegex(ValueError, "batch-in-progress"):
            wakeup.bind_wait(self.review, "Claude Code", "session-a", replace=True)
        store.video_receipt(self.review, seq=2, receipt=[{"noteId": "n3", "outcome": "question", "question": "配音？"}])
        wakeup.bind_wait(self.review, "Claude Code", "session-a", replace=True)
        self.assertEqual([i["status"] for i in store.handoff_load(self.review)["items"]], ["claimed", "done"])

    def test_unattributable_legacy_decode_repair_requires_explicit_resubmission(self):
        failure = self._receipt_for_decode_recovery()
        handoff = store.handoff_load(self.review)
        handoff["items"][0]["videoSubmission"].pop("claimedReceiver")
        (self.review / store.HANDOFF_FILE).write_text(json.dumps(handoff))
        with redirect_stdout(io.StringIO()):
            self.assertEqual(wakeup.main(["unbind", str(self.review)]), 0)
        store.video_decode_failed(self.review, **failure)
        self.assertEqual(store.handoff_load(self.review)["items"][0]["status"], "done")
        self.assertEqual(
            next(n for n in store.load(self.review)["notes"] if n["id"] == "n1")["videoReview"]["state"], "failed"
        )
        wakeup.bind_wait(self.review, "Claude Code", "session-b")
        with self.assertRaises(store.HandoffError):
            store.video_receipt(self.review, seq=1, receipt_id="wrong-session", render_error="wrong")
        store.handoff_submit(self.review, note_ids=["n1"])
        self.assertEqual(store.handoff_claim(self.review)["seq"], 2)

    def test_desktop_receiver_can_restore_its_acknowledged_decode_repair(self):
        original = "d2bce608-0b30-42b2-94d2-c1296c3079ae"
        other = "f5a5b85d-a352-470e-9701-11e461771fe7"
        socket = self.root / "synthetic.sock"
        with patch.object(wakeup, "DesktopIPC"):
            binding = wakeup.bind(self.review, original, socket, replace=True)
            failure = self._receipt_for_decode_recovery()
            wakeup._write(
                self.review / wakeup.DELIVERY_FILE,
                {
                    "version": 1,
                    "items": {
                        "original": {
                            "bindingId": binding["bindingId"],
                            "seq": 1,
                            "event": "handoff",
                            "state": "received",
                        }
                    },
                },
            )
            with redirect_stdout(io.StringIO()):
                self.assertEqual(wakeup.main(["unbind", str(self.review)]), 0)
            store.video_decode_failed(self.review, **failure)
            with self.assertRaisesRegex(ValueError, "batch-in-progress"):
                wakeup.bind(self.review, other, socket)
            restored = wakeup.bind(self.review, original, socket)
        self.assertEqual(wakeup.binding_identity(restored), {"harness": "Codex", "sessionId": original})
        self.assertNotEqual(restored["bindingId"], binding["bindingId"])

    def test_second_submission_uses_its_frozen_base_and_cli_reports_the_retained_version(self):
        store.handoff_submit(self.review, note_ids=["n1"])
        store.handoff_submit(self.review, note_ids=["n3"])
        self.assertEqual(self.run_cli(["claim", str(self.review)])[0], 0)
        cut = self.root / "second.mp4"
        cut.write_bytes(self.v2_bytes)
        store.video_receipt(
            self.review,
            seq=1,
            receipt_id="first",
            movie=cut,
            receipt=[
                {"noteId": "n1", "outcome": "fixed", "summary": "修正字幕", "target": {"kind": "frame", "frame": 96}}
            ],
        )
        claim, data, _ = self.run_cli(["claim", str(self.review)])
        self.assertEqual(claim, 0)
        self.assertEqual(json.loads(data)["video"]["currentVersionId"], "V1")
        command = ["video-receipt", str(self.review), "--seq", "2", "--locale", "en"]
        code, _, error = self.run_cli([*command, "--movie", str(cut)])
        self.assertEqual(code, 1)
        self.assertIn("baseVersionId", error)
        self.assertFalse((self.review / "media" / "v3.mp4").exists())
        code, output, _ = self.run_cli([*command, "--render-error", "renderer stopped"])
        self.assertEqual(code, 0)
        self.assertIn("V2", output)
        code, output, _ = self.run_cli(
            [*command, "--receipt", "-"],
            stdin_text=json.dumps([{"noteId": "n3", "outcome": "question", "question": "保留片头吗？"}]),
        )
        self.assertEqual(code, 0)
        self.assertIn("V2", output)

    def test_agent_display_name_cannot_authorize_a_reviewer_decision(self):
        store.handoff_submit(self.review, note_ids=["n1"])
        store.handoff_claim(self.review)
        store.video_receipt(
            self.review,
            seq=1,
            receipt_id="screen-ready",
            movie=self.v1_path,
            receipt=[
                {"noteId": "n1", "outcome": "fixed", "summary": "修正字幕", "target": {"kind": "frame", "frame": 96}}
            ],
        )
        code, _, _ = self.run_cli(["resolve", str(self.review), "n1", "--by", "page"])
        self.assertNotEqual(code, 0)
        self.assertEqual(next(n for n in store.load(self.review)["notes"] if n["id"] == "n1")["status"], "drafted")

    def test_cli_video_receipt_show_and_claim_payload_across_partial_and_retry(self):
        store.handoff_submit(self.review, "先改前三条", note_ids=["n1", "n2", "n3"])
        code, claim_out, claim_err = self.run_cli(["claim", str(self.review)])
        self.assertEqual(code, 0, claim_err)
        claim_data = json.loads(claim_out)
        self.assertEqual(claim_data["video"]["currentVersionId"], "V1")

        # 接收方绑定校验
        code_bad_sess, _, _ = self.run_cli(
            ["video-receipt", str(self.review), "--seq", "1", "--render-error", "x", "--session", "wrong"]
        )
        self.assertEqual(code_bad_sess, 2)

        # 跨审阅拒绝
        code_cross, _, _ = self.run_cli(
            ["video-receipt", str(self.review), "--seq", "1", "--review-id", "0000000000000000", "--render-error", "x"]
        )
        self.assertEqual(code_cross, 1)

        # 1) 先报一次渲染失败
        reason = "renderer could not read /synthetic/private/overlay.png"
        code_fail, fail_out, fail_err = self.run_cli(
            [
                "video-receipt",
                str(self.review),
                "--seq",
                "1",
                "--review-id",
                self.rid,
                "--render-error",
                reason,
                "--receipt-id",
                "rcpt-cli-fail",
                "--by",
                "claude",
                "--json",
            ]
        )
        self.assertEqual(code_fail, 0, fail_err)
        self.assertEqual(json.loads(fail_out)["failure"]["reason"], reason)
        code_show, text, err = self.run_cli(["show", str(self.review), "--locale", "en"])
        self.assertEqual(code_show, 0, err)
        self.assertIn(reason, text)
        code_json, report, err = self.run_cli(["show", str(self.review), "--json", "--locale", "en"])
        self.assertEqual(code_json, 0, err)
        self.assertEqual(json.loads(report)["video"]["lastFailure"]["reason"], "Engine render failed")

        # 2) 恢复后传文件回执 + --no-finish
        v2_file = self.root / "proj" / "out" / "v2.mp4"
        v2_file.write_bytes(self.v2_bytes)
        receipt_file = self.root / "receipt1.json"
        receipt_file.write_text(
            json.dumps(
                {
                    "notes": [
                        {
                            "noteId": "n1",
                            "outcome": "fixed",
                            "summary": "片头+12帧并上移字幕",
                            "target": {"kind": "frame", "frame": 108},
                        },
                        {
                            "noteId": "n2",
                            "outcome": "fixed",
                            "summary": "音乐压低 6dB",
                            "target": {"kind": "range", "startFrame": 132, "endFrame": 192},
                        },
                    ]
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        code_part, part_out, part_err = self.run_cli(
            [
                "video-receipt",
                str(self.review),
                "--seq",
                "1",
                "--review-id",
                self.rid,
                "--movie",
                str(v2_file),
                "--receipt",
                str(receipt_file),
                "--receipt-id",
                "rcpt-cli-part",
                "--no-finish",
                "--by",
                "claude",
                "--json",
            ]
        )
        self.assertEqual(code_part, 0, part_err)
        part_data = json.loads(part_out)
        self.assertEqual(part_data["status"], "claimed")
        self.assertEqual(part_data["version"]["versionId"], "V2")

        # 3) 通过 stdin ("-") 补交第 3 条提问并完成本批
        stdin_payload = json.dumps({"notes": [{"noteId": "n3", "outcome": "question", "question": "片头保留吗？"}]})
        code_fin, fin_out, fin_err = self.run_cli(
            [
                "video-receipt",
                str(self.review),
                "--seq",
                "1",
                "--review-id",
                self.rid,
                "--receipt",
                "-",
                "--receipt-id",
                "rcpt-cli-fin",
                "--summary",
                "已改两条、一条提问",
                "--by",
                "claude",
                "--json",
            ],
            stdin_text=stdin_payload,
        )
        self.assertEqual(code_fin, 0, fin_err)
        fin_data = json.loads(fin_out)
        self.assertEqual(fin_data["status"], "drafted")

        # Agent 不能 resolve 视频批注，中英报错明确
        for lang, expected_word in (("zh-CN", "页面"), ("en", "reviewer")):
            code_res, _, res_err = self.run_cli(["resolve", str(self.review), "n1", "--by", "claude", "--locale", lang])
            self.assertEqual(code_res, 1)
            self.assertIn(expected_word.lower(), res_err.lower())

        # notes.py show 中英文输出均含视频回执与版本信息
        for lang, labels in (
            ("zh-CN", ("待人工复核", "待答复", "V2", "108", "片头保留吗？")),
            ("en", ("Pending review", "Needs reply", "V2", "108", "片头保留吗？")),
        ):
            code_show, show_out, show_err = self.run_cli(["show", str(self.review), "--all", "--locale", lang])
            self.assertEqual(code_show, 0, show_err)
            for label in labels:
                self.assertIn(label, show_out)

        # 下一批领取 n3 时携带已有 videoReview 历史和当前版本列表
        store.handoff_submit(self.review, "答复后重交 n3", note_ids=["n3"])
        code_c2, c2_out, c2_err = self.run_cli(["claim", str(self.review)])
        self.assertEqual(code_c2, 0, c2_err)
        c2_data = json.loads(c2_out)
        self.assertEqual(c2_data["video"]["currentVersionId"], "V2")
        self.assertEqual(c2_data["materials"][0]["videoReview"]["question"], "片头保留吗？")
        code, scoped, errors = self.run_cli(["show", str(self.review), "--batch", "2", "--json"])
        self.assertEqual(code, 0, errors)
        exported = json.loads(scoped)
        self.assertEqual([item["seq"] for item in exported["handoff"]["history"]], [2])
        self.assertEqual(exported["handoff"]["latest"]["seq"], 2)
        self.assertNotIn("片头+12帧并上移字幕", scoped)
        self.assertEqual([n["id"] for n in exported["notes"]], ["n3"])

    def test_server_exposes_video_receipt_versions_and_enforces_gates(self):
        store.handoff_submit(self.review, "提交三条", note_ids=["n1", "n2", "n3"])
        store.handoff_claim(self.review, by="claude", seq=1)
        v2_file = self.root / "proj" / "out" / "v2.mp4"
        v2_file.write_bytes(self.v2_bytes)
        store.video_receipt(
            self.review,
            seq=1,
            receipt_id="rcpt-srv",
            movie=v2_file,
            by="claude",
            receipt={
                "notes": [
                    {
                        "noteId": "n1",
                        "outcome": "fixed",
                        "summary": "字幕上移",
                        "target": {"kind": "frame", "frame": 108},
                    },
                    {"noteId": "n2", "outcome": "question", "question": "音量要几 dB？"},
                    {"noteId": "n3", "outcome": "fixed", "summary": "缺证据"},
                ]
            },
        )

        server = serve.ReviewServer(self.review, 0, log_to_file=False)
        worker = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
        worker.start()

        def close():
            server.shutdown()
            server.server_close()
            worker.join(2)

        self.addCleanup(close)
        headers = {
            "Host": f"127.0.0.1:{server.port}",
            "Origin": f"http://127.0.0.1:{server.port}",
            "Content-Type": "application/json",
        }

        code, h_hdr, h_body = serve._req(server.port, "GET", "/api/health", {"Host": headers["Host"]})
        self.assertEqual(code, 200)
        self.assertIn("video-receipt", json.loads(h_body)["features"])
        self.assertNotIn("Access-Control-Allow-Origin", h_hdr)

        code, _, notes_body = serve._req(server.port, "GET", "/api/notes", {"Host": headers["Host"]})
        self.assertEqual(code, 200)
        notes_doc = json.loads(notes_body)
        self.assertEqual(notes_doc["video"]["currentVersionId"], "V2")
        self.assertEqual([v["versionId"] for v in notes_doc["video"]["versions"]], ["V1", "V2"])
        self.assertNotIn("path", notes_doc["video"]["versions"][1])
        self.assertNotIn(str(v2_file), notes_body.decode("utf-8"))

        c1, _, b1 = serve._req(server.port, "GET", notes_doc["video"]["versions"][0]["url"], {"Host": headers["Host"]})
        c2, _, b2 = serve._req(server.port, "GET", notes_doc["video"]["versions"][1]["url"], {"Host": headers["Host"]})
        self.assertEqual((c1, b1), (200, self.v1_bytes))
        self.assertEqual((c2, b2), (200, self.v2_bytes))

        # 非完备条目从页面 PATCH resolve -> 400 + video-not-ready
        for bad_id in ("n2", "n3"):
            pcode, _, pbody = serve._req(server.port, "PATCH", f"/api/notes/{bad_id}", headers, {"status": "resolved"})
            self.assertEqual(pcode, 400, pbody)
            pdata = json.loads(pbody)
            self.assertEqual(pdata["code"], "video-not-ready")

        # 通用 PATCH 不能篡改 videoReview
        serve._req(
            server.port,
            "PATCH",
            "/api/notes/n1",
            headers,
            {"text": "补充说明", "videoReview": {"state": "tampered"}},
        )
        n1_after = next(n for n in store.load(self.review)["notes"] if n["id"] == "n1")
        self.assertEqual(n1_after["videoReview"]["state"], "pending_review")

        # 完备条目页面 PATCH resolve -> 200
        ok_code, _, ok_body = serve._req(
            server.port,
            "PATCH",
            "/api/notes/n1",
            headers,
            {
                "status": "resolved",
                "expectedEvidence": next(n for n in store.load(self.review)["notes"] if n["id"] == "n1")["videoReview"][
                    "evidenceRevision"
                ],
            },
        )
        self.assertEqual(ok_code, 200, ok_body)
        self.assertEqual(json.loads(ok_body)["note"]["status"], "resolved")

        # 后续批渲染失败 -> GET /api/notes 带出 lastFailure 且保留 V2
        store.handoff_submit(self.review, "重试 n2", note_ids=["n2"])
        store.handoff_claim(self.review, by="claude", seq=2)
        store.video_receipt(self.review, seq=2, receipt_id="rcpt-fail-srv", render_error="ffmpeg segfault", by="claude")
        code, _, notes_body2 = serve._req(server.port, "GET", "/api/notes", {"Host": headers["Host"]})
        self.assertEqual(code, 200)
        notes_doc2 = json.loads(notes_body2)
        self.assertEqual(notes_doc2["video"]["currentVersionId"], "V2")
        self.assertEqual(notes_doc2["video"]["lastFailure"]["reason"], "引擎渲染失败")

        # 英文服务下的 HandoffError 响应也返回本地化英文报错与 code
        server_en = serve.ReviewServer(self.review, 0, log_to_file=False, locale="en")
        worker_en = threading.Thread(target=server_en.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
        worker_en.start()

        def close_en():
            server_en.shutdown()
            server_en.server_close()
            worker_en.join(2)

        self.addCleanup(close_en)
        headers_en = {
            "Host": f"127.0.0.1:{server_en.port}",
            "Origin": f"http://127.0.0.1:{server_en.port}",
            "Content-Type": "application/json",
        }
        ecode, _, ebody = serve._req(server_en.port, "PATCH", "/api/notes/n2", headers_en, {"status": "resolved"})
        self.assertEqual(ecode, 400)
        edata = json.loads(ebody)
        self.assertEqual(edata["code"], "video-not-ready")
        self.assertIn("not ready", edata["error"])

    def test_needs_relocate_tracks_batch_version_for_decode_failure_and_blocks_failed_target(self):
        store.handoff_submit(self.review, "检查镜头移位", note_ids=["n1"])
        store.handoff_claim(self.review, by="claude", seq=1)
        v2_file = self.root / "v2-reloc.mp4"
        v2_file.write_bytes(synthetic_mp4(frames=192))

        with self.assertRaises(store.HandoffError) as bad_ver:
            store.video_receipt(
                self.review,
                seq=1,
                receipt_id="rcpt-reloc-badver",
                movie=v2_file,
                receipt=[{"noteId": "n1", "outcome": "needs_relocate", "versionId": "V1", "relocateReason": "错版"}],
                by="claude",
            )
        self.assertEqual(bad_ver.exception.code, "invalid-receipt-target")

        res = store.video_receipt(
            self.review,
            seq=1,
            receipt_id="rcpt-reloc-ok",
            movie=v2_file,
            receipt=[{"noteId": "n1", "outcome": "needs_relocate", "relocateReason": "剪辑变化较大需确认"}],
            by="claude",
        )
        self.assertEqual(res["version"]["versionId"], "V2")
        n1 = next(n for n in store.load(self.review)["notes"] if n["id"] == "n1")
        self.assertEqual(n1["videoReview"]["state"], "needs_relocate")
        self.assertEqual(n1["videoReview"]["versionId"], "V2")

        # V2 解码失败后，needs_relocate 条目随批次一起转入 failed，且不能被重定位回已损坏的 V2
        store.video_decode_failed(
            self.review,
            note_id="n1",
            version_id="V2",
            seq=1,
            expected_evidence=n1["videoReview"]["evidenceRevision"],
            review_id=self.rid,
            reason="MediaError 3",
        )
        n1_after = next(n for n in store.load(self.review)["notes"] if n["id"] == "n1")
        self.assertEqual(n1_after["videoReview"]["state"], "failed")
        with self.assertRaises(store.HandoffError) as broken_target:
            store.relocate_video_note(
                self.review,
                "n1",
                target={"kind": "frame", "frame": 10},
                version_id="V2",
                summary="尝试指向损坏版",
            )
        self.assertEqual(broken_target.exception.code, "invalid-receipt-target")


if __name__ == "__main__":
    unittest.main()
