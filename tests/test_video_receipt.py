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

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "skill" / "annotation-everything" / "scripts"
sys.path.insert(0, str(SCRIPTS))
os.environ.setdefault("AE_HOME", tempfile.mkdtemp(prefix="ae-video-receipt-home-"))

import build_viewer  # noqa: E402
import ledger  # noqa: E402
import notes as notes_cli  # noqa: E402
import notes_store as store  # noqa: E402
import serve  # noqa: E402
import video_review  # noqa: E402
import video_source  # noqa: E402
import wakeup  # noqa: E402
from tests.test_video_handoff import V1_MEDIA, video_note  # noqa: E402
from tests.test_video_review import synthetic_mp4  # noqa: E402


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
        self.assertEqual(pub_v2["url"], "/api/video/versions/V2/media")
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
        self.assertEqual(pub["lastFailure"]["reason"], "render exited 1")

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
        self.assertTrue(video_review.review_readiness(plain_open, manifest, review_dir=self.review)[0])

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
        store.set_status(self.review, "n1", "resolved", by="page")
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
                store.set_status(self.review, bad_id, "resolved", by="page")
            self.assertEqual(exc2.exception.code, "video-not-ready")

        # _finalizable 只列 n1；handoff_finalize_all 不能夹带 n2/n3/n4
        state = store.handoff_state(self.review, full=False)
        self.assertEqual(state["finalizable"]["batches"], [{"seq": 1, "ids": ["n1"]}])
        res = store.handoff_finalize_all(self.review, {1: {"n1", "n2", "n3", "n4"}})
        self.assertEqual(res["finalizedIds"], ["n1"])
        self.assertEqual({s["id"] for s in res["skipped"]}, {"n2", "n3", "n4"})

        # 0 drafted 的批次可以用 handoff_done 结束为 done 且 resolvedCount = 0
        store.handoff_submit(self.review, "只问 n5", target="final", note_ids=["n5"])
        store.handoff_claim(self.review, by="claude", seq=2)
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
                code = notes_cli.main(argv)
        finally:
            sys.stdin = old_stdin
            os.environ.clear()
            os.environ.update(old_env)
        return code, out.getvalue(), err.getvalue()

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
        code_fail, fail_out, fail_err = self.run_cli(
            [
                "video-receipt",
                str(self.review),
                "--seq",
                "1",
                "--review-id",
                self.rid,
                "--render-error",
                "render exit 1",
                "--receipt-id",
                "rcpt-cli-fail",
                "--by",
                "claude",
                "--json",
            ]
        )
        self.assertEqual(code_fail, 0, fail_err)
        self.assertEqual(json.loads(fail_out)["failure"]["reason"], "render exit 1")

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

        c1, _, b1 = serve._req(server.port, "GET", "/api/video/versions/V1/media", {"Host": headers["Host"]})
        c2, _, b2 = serve._req(server.port, "GET", "/api/video/versions/V2/media", {"Host": headers["Host"]})
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
        ok_code, _, ok_body = serve._req(server.port, "PATCH", "/api/notes/n1", headers, {"status": "resolved"})
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
        self.assertEqual(notes_doc2["video"]["lastFailure"]["reason"], "ffmpeg segfault")

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


if __name__ == "__main__":
    unittest.main()
