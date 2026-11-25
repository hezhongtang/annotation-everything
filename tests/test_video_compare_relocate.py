"""视频批注前后对照、删剪回执与人工重定位（#196）。

按 TDD 垂直切片覆盖：
1. video_review 分版本时间基准换算（含 30fps 移位、非等帧率及 NTSC timebase）与剪口上下文窗口；
2. notes_store.relocate_video_note 人工重定位、可追溯历史、不篡改原证据及严格验收门禁；
3. serve.py POST /api/notes/<id>/video-relocate 端点与安全底线；
4. viewer_template.html 前后独立播放对照、删镜/待定位视图、重定位交互与 DOCX/PPTX 兼容。
"""

from __future__ import annotations

import copy
import io
import json
import os
import sys
import tempfile
import threading
import unittest
from contextlib import redirect_stderr, redirect_stdout
from fractions import Fraction
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "skill" / "annotation-everything" / "scripts"
sys.path.insert(0, str(SCRIPTS))
sys.path.insert(0, str(ROOT))

import host_isolation  # noqa: E402

host_isolation.require_for_module(__name__)
os.environ["AE_HOME"] = tempfile.mkdtemp(prefix="ae-video-compare-home-")

import build_viewer  # noqa: E402
import docx_review  # noqa: E402
import notes as notes_cli  # noqa: E402
import notes_store as store  # noqa: E402
import serve  # noqa: E402
import video_review  # noqa: E402
import video_source  # noqa: E402
from tests.test_analysis_page import interaction_program  # noqa: E402
from tests.test_video_handoff import V1_MEDIA, video_note  # noqa: E402
from tests.test_video_review import synthetic_mp4  # noqa: E402

TEMPLATE = ROOT / "skill" / "annotation-everything" / "scripts" / "viewer_template.html"
PURE_START = "// ---------- 视频审阅纯函数 ----------"
PURE_END = "// ---------- 视频审阅 DOM ----------"
COMPARE_START = "// ---------- 视频对照与重定位 ----------"
COMPARE_END = "// ---------- 批注区："


class VideoTimebaseAndTargetTests(unittest.TestCase):
    def test_per_version_timebase_conversion_handles_shift_and_unequal_rates(self):
        v1_30 = {
            "versionId": "V1",
            "fps": 30.0,
            "timebase": {"num": 1, "den": 30},
            "frames": 180,
            "width": 640,
            "height": 360,
        }
        v2_30 = {
            "versionId": "V2",
            "fps": 30.0,
            "timebase": {"num": 1, "den": 30},
            "frames": 192,
            "width": 640,
            "height": 360,
        }
        v2_24 = {
            "versionId": "V2",
            "fps": 24.0,
            "timebase": {"num": 1, "den": 24},
            "frames": 144,
            "width": 640,
            "height": 360,
        }
        v_ntsc = {
            "versionId": "V3",
            "fps": 30000 / 1001,
            "timebase": {"num": 1001, "den": 30000},
            "frames": 600,
            "width": 640,
            "height": 360,
        }

        self.assertEqual(video_review.version_timebase(v1_30), Fraction(1, 30))
        self.assertEqual(video_review.version_timebase(v_ntsc), Fraction(1001, 30000))
        self.assertAlmostEqual(video_review.frame_to_seconds(96, v1_30), 3.2)
        self.assertAlmostEqual(video_review.frame_to_seconds(108, v2_30), 3.6)
        self.assertAlmostEqual(video_review.frame_to_seconds(120, v1_30), 4.0)
        self.assertAlmostEqual(video_review.frame_to_seconds(180, v1_30), 6.0)
        self.assertAlmostEqual(video_review.frame_to_seconds(132, v2_30), 4.4)
        self.assertAlmostEqual(video_review.frame_to_seconds(192, v2_30), 6.4)
        self.assertAlmostEqual(video_review.frame_to_seconds(48, v2_24), 2.0)
        self.assertAlmostEqual(video_review.frame_to_seconds(300, v_ntsc), 10.01)

        # IEEE-754 边界（1.4 * 30 == 41.99999999999999）必须准确回到第 42 帧，不同版本按自身 timebase 换算
        self.assertEqual(video_review.seconds_to_frame(1.4, v1_30), 42)
        self.assertEqual(video_review.seconds_to_frame(4.4, v2_30), 132)
        self.assertEqual(video_review.seconds_to_frame(2.0, v2_24), 48)
        self.assertEqual(video_review.seconds_to_frame(10.01, v_ntsc), 300)
        self.assertEqual(video_review.seconds_to_frame(999.0, v1_30, clamp=True), 179)

        with self.assertRaises(video_review.VideoError):
            video_review.seconds_to_frame(999.0, v1_30, clamp=False)
        with self.assertRaises(video_review.VideoError):
            video_review.version_timebase({"versionId": "V1", "fps": 30})
        with self.assertRaises(video_review.VideoError):
            video_review.frame_to_seconds(-1, v1_30)

    def test_cut_context_and_temporal_window_keep_mark_and_bound_cut_edges(self):
        v2 = {
            "versionId": "V2",
            "fps": 30.0,
            "timebase": {"num": 1, "den": 30},
            "frames": 192,
            "width": 640,
            "height": 360,
        }
        explicit = video_review.cut_context_window(
            {"kind": "cut", "frame": 108, "contextStart": 90, "contextEnd": 126}, v2
        )
        self.assertEqual(explicit, (90, 126))
        self.assertEqual(video_review.cut_context_window({"kind": "cut", "frame": 0}, v2), (0, 30))
        self.assertEqual(video_review.cut_context_window({"kind": "cut", "frame": 192}, v2), (162, 192))

        win = video_review.temporal_window({"kind": "range", "startFrame": 132, "endFrame": 192}, v2)
        self.assertEqual(win["kind"], "range")
        self.assertAlmostEqual(win["startTime"], 4.4)
        self.assertAlmostEqual(win["endTime"], 6.4)

        cut_win = video_review.temporal_window({"kind": "cut", "frame": 108}, v2)
        self.assertEqual(cut_win["kind"], "cut")
        self.assertEqual((cut_win["contextStart"], cut_win["contextEnd"]), (78, 138))
        self.assertAlmostEqual(cut_win["time"], 3.6)

        marked = video_review.clean_receipt_target(
            {
                "kind": "frame",
                "frame": 108,
                "mark": {"kind": "region", "x": 0.1, "y": 0.7, "width": 0.4, "height": 0.1},
            },
            v2,
        )
        self.assertEqual(marked["mark"], {"kind": "region", "x": 0.1, "y": 0.7, "width": 0.4, "height": 0.1})
        with self.assertRaises(video_review.VideoError):
            video_review.clean_receipt_target(
                {
                    "kind": "frame",
                    "frame": 108,
                    "mark": {"kind": "region", "x": 0.8, "y": 0.8, "width": 0.4, "height": 0.1},
                },
                v2,
            )


class VideoRelocateStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="ae-video-relocate-store-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.v1_bytes = synthetic_mp4(frames=180, fps=30, width=640, height=360)
        self.v2_bytes = synthetic_mp4(frames=192, fps=30, width=640, height=360)
        self.v3_24_bytes = synthetic_mp4(frames=144, fps=24, width=640, height=360)
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
                video_note("n3", "片头镜头节奏拖沓", frame=12),
                video_note("n4", "结尾画面偏暗", frame=170),
            ],
        )

    def _movie(self, data: bytes, name: str) -> Path:
        path = self.root / "proj" / "out" / name
        path.write_bytes(data)
        return path

    def test_relocate_from_needs_relocate_preserves_history_original_and_allows_approval(self):
        store.handoff_submit(self.review, "修 n3", note_ids=["n3"])
        store.handoff_claim(self.review, by="claude", seq=1)
        v2_file = self._movie(self.v2_bytes, "v2.mp4")
        store.video_receipt(
            self.review,
            seq=1,
            receipt_id="rcpt-reloc-1",
            movie=v2_file,
            by="claude",
            receipt={
                "notes": [
                    {
                        "noteId": "n3",
                        "outcome": "needs_relocate",
                        "versionId": "V2",
                        "relocateReason": "片头重剪后镜头位置不确定",
                    }
                ]
            },
        )
        before_doc = store.load(self.review)
        n3_before = next(n for n in before_doc["notes"] if n["id"] == "n3")
        orig_video = copy.deepcopy(n3_before["video"])
        orig_text = n3_before["text"]
        self.assertEqual(n3_before["status"], "open")
        self.assertEqual(n3_before["videoReview"]["state"], "needs_relocate")
        # needs_relocate 不可直接通过
        with self.assertRaises(store.HandoffError) as err:
            store.set_status(self.review, "n3", "resolved", by="page", reviewer=True)
        self.assertEqual(err.exception.code, "video-not-ready")

        # 审阅人人工重定位到 V2 第 24 帧并补齐说明 -> 变成 pending_review / drafted，且第 1 批从 done 恢复成可定稿 drafted
        doc, n3_after, handoff_doc = store.relocate_video_note(
            self.review,
            "n3",
            version_id="V2",
            target={"kind": "frame", "frame": 24},
            summary="已确认移位至 V2 第 24 帧",
            reason="片头延长 12 帧后镜头顺移",
            by="page",
        )
        self.assertEqual(n3_after["video"], orig_video)
        self.assertEqual(n3_after["text"], orig_text)
        self.assertEqual(n3_after["status"], "drafted")
        self.assertEqual(n3_after["draftBatch"], 1)
        vr = n3_after["videoReview"]
        self.assertEqual(vr["state"], "pending_review")
        self.assertEqual(vr["versionId"], "V2")
        self.assertEqual(vr["target"]["frame"], 24)
        self.assertEqual(vr["summary"], "已确认移位至 V2 第 24 帧")
        self.assertEqual(vr["relocatedFrom"]["state"], "needs_relocate")
        self.assertEqual(vr["relocatedFrom"]["relocateReason"], "片头重剪后镜头位置不确定")
        self.assertEqual(len(vr["history"]), 2)
        self.assertEqual(vr["history"][0]["state"], "needs_relocate")
        self.assertEqual(vr["history"][1]["source"], "relocate")
        self.assertEqual(vr["history"][1]["previousState"], "needs_relocate")
        self.assertEqual(vr["history"][1]["target"]["frame"], 24)
        self.assertEqual(vr["history"][1]["reason"], "片头延长 12 帧后镜头顺移")
        self.assertEqual(n3_after["trail"][-1]["action"], "relocated")

        batch1 = next(it for it in handoff_doc["items"] if it["seq"] == 1)
        self.assertEqual(batch1["status"], "drafted")
        self.assertEqual(batch1["videoStats"]["pendingReviewIds"], ["n3"])
        self.assertEqual(batch1["videoStats"]["needsRelocateCount"], 0)

        # 继续复核并人工通过（保留前两轮历史并记录人工通过决定）
        store.set_status(self.review, "n3", "resolved", by="page", reviewer=True)
        n3_done = next(n for n in store.load(self.review)["notes"] if n["id"] == "n3")
        self.assertEqual(n3_done["status"], "resolved")
        self.assertEqual(n3_done["videoReview"]["decision"], "approved")
        self.assertEqual(len(n3_done["videoReview"]["history"]), 3)
        self.assertEqual(n3_done["videoReview"]["history"][-1]["decision"], "approved")

        # 已通过的条目不能再重定位
        with self.assertRaises(store.HandoffError) as decided_err:
            store.relocate_video_note(self.review, "n3", target={"kind": "frame", "frame": 10}, summary="x")
        self.assertEqual(decided_err.exception.code, "note-already-decided")

    def test_relocate_rejects_out_of_bounds_and_keeps_missing_evidence_until_summary_present(self):
        store.handoff_submit(self.review, "修 n1 与 n2", note_ids=["n1", "n2"])
        store.handoff_claim(self.review, by="claude", seq=1)
        v2_file = self._movie(self.v2_bytes, "v2.mp4")
        store.video_receipt(
            self.review,
            seq=1,
            receipt_id="rcpt-miss",
            movie=v2_file,
            by="claude",
            receipt={
                "notes": [
                    {"noteId": "n1", "outcome": "fixed", "summary": ""},
                    {
                        "noteId": "n2",
                        "outcome": "fixed",
                        "summary": "音乐区间已调整",
                        "target": {"kind": "range", "startFrame": 130, "endFrame": 190},
                    },
                ]
            },
        )
        # 越界/未登记版本直接拒绝
        with self.assertRaises(store.HandoffError) as oob_err:
            store.relocate_video_note(self.review, "n1", version_id="V2", target={"kind": "frame", "frame": 192})
        self.assertEqual(oob_err.exception.code, "invalid-receipt-target")
        with self.assertRaises(store.HandoffError) as unreg_err:
            store.relocate_video_note(self.review, "n1", version_id="V9", target={"kind": "frame", "frame": 10})
        self.assertEqual(unreg_err.exception.code, "invalid-receipt-target")

        # missing_evidence 仅补 target、没给 summary 时不能伪造说明或变成可验收
        _, n1_partial, _ = store.relocate_video_note(
            self.review,
            "n1",
            version_id="V2",
            target={"kind": "frame", "frame": 108},
            reason="补齐目标帧但未填说明",
        )
        self.assertEqual(n1_partial["status"], "open")
        self.assertEqual(n1_partial["videoReview"]["state"], "missing_evidence")
        self.assertEqual(n1_partial["videoReview"]["target"]["frame"], 108)
        manifest = json.loads((self.review / "review.json").read_text(encoding="utf-8"))
        ready, _ = video_review.review_readiness(n1_partial, manifest, review_dir=self.review)
        self.assertFalse(ready)
        with self.assertRaises(store.HandoffError):
            store.set_status(self.review, "n1", "resolved", by="page", reviewer=True)

        # 补齐 summary 后转为 pending_review，且保留先前的 relocatedFrom 与完整 history
        _, n1_ready, _ = store.relocate_video_note(
            self.review,
            "n1",
            version_id="V2",
            target={"kind": "frame", "frame": 108},
            summary="V2 第 108 帧字幕已避开图标",
        )
        self.assertEqual(n1_ready["status"], "drafted")
        self.assertEqual(n1_ready["videoReview"]["state"], "pending_review")
        self.assertEqual(n1_ready["videoReview"]["relocatedFrom"]["state"], "missing_evidence")
        self.assertEqual(len(n1_ready["videoReview"]["history"]), 3)
        self.assertTrue(video_review.review_readiness(n1_ready, manifest, review_dir=self.review)[0])

        # 多次校正 pending_review（n2）后再进入新批次交回时，relocatedFrom 和重定位历史不被抹除
        _, n2_reloc, _ = store.relocate_video_note(
            self.review,
            "n2",
            version_id="V2",
            target={"kind": "range", "startFrame": 132, "endFrame": 192},
            reason="精准对齐 4.4–6.4s",
        )
        self.assertEqual(n2_reloc["videoReview"]["relocatedFrom"]["target"]["startFrame"], 130)
        self.assertEqual(n2_reloc["videoReview"]["summary"], "音乐区间已调整")
        store.video_decision(self.review, "n2", action="return", reason="再微调 1 dB", by="page")
        store.handoff_submit(self.review, "二轮修 n2", note_ids=["n2"])
        store.handoff_claim(self.review, by="claude", seq=2)
        v3_file = self._movie(self.v3_24_bytes, "v3.mp4")
        store.video_receipt(
            self.review,
            seq=2,
            receipt_id="rcpt-v3",
            movie=v3_file,
            by="claude",
            receipt={
                "notes": [
                    {
                        "noteId": "n2",
                        "outcome": "fixed",
                        "summary": "24fps 版对应 4.4–6.4s 压低 1dB",
                        "target": {"kind": "range", "startFrame": 106, "endFrame": 144},
                    }
                ]
            },
        )
        n2_v3 = next(n for n in store.load(self.review)["notes"] if n["id"] == "n2")
        self.assertIn("relocatedFrom", n2_v3["videoReview"])
        self.assertGreaterEqual(len(n2_v3["videoReview"]["history"]), 3)

    def test_relocation_requires_evidence_for_this_note_and_keeps_legacy_notes_repairable(self):
        store.handoff_submit(self.review, note_ids=["n1"])
        store.handoff_claim(self.review, seq=1)
        path = self._movie(self.v2_bytes, "v2.mp4")
        video_review.register_version(self.review, path, batch=1, by="claude", review_id=self.rid, receipt_id="r-v2")

        store.import_notes(self.review, [video_note("history", "旧处理记录", frame=5)])
        doc = store.load(self.review)
        by_id = {n["id"]: n for n in doc["notes"]}
        # 合成旧记录。n1 只在提交批次中点名，n3 只保留对应成片，n4 只保留逐条处理痕迹。
        by_id["n3"]["videoReview"] = {"versionId": "V2"}
        by_id["history"]["videoReview"] = {"history": [{"state": "needs_relocate", "batch": 1, "versionId": "V2"}]}

        by_id["n4"]["trail"] = [{"at": "2020-01-01", "by": "agent", "stage": "draft", "action": "drafted"}]
        (self.review / store.NOTES_FILE).write_text(json.dumps(doc))
        target = {"kind": "frame", "frame": 182}
        snapshot = store.handoff_state(self.review)
        self.assertEqual(snapshot["videoRelocationBatches"], {"n1": 1, "n3": None, "n4": None, "history": 1})
        self.assertEqual(snapshot["latest"]["videoStats"]["missingEvidenceIds"], ["n1"])
        self.assertEqual(snapshot["latest"]["videoStats"]["unsubmittedIds"], ["n2"])
        with self.assertRaises(store.HandoffError) as unsubmitted:
            store.relocate_video_note(
                self.review, "n2", version_id="V2", target={"kind": "range", "startFrame": 130, "endFrame": 180}
            )
        self.assertEqual(unsubmitted.exception.params["reason"], "unsubmitted")
        self.assertEqual(store.load(self.review), doc)
        with self.assertRaises(store.HandoffError) as wrong_version:
            store.relocate_video_note(self.review, "n4", version_id="V1", target={"kind": "frame", "frame": 170})
        self.assertEqual(wrong_version.exception.code, "invalid-receipt-target")
        for nid in ("n1", "n3", "n4", "history"):
            with self.subTest(note=nid):
                _, repaired, _ = store.relocate_video_note(
                    self.review, nid, version_id="V2", target=target, summary="补齐对应帧"
                )
                self.assertEqual(repaired["video"], by_id[nid]["video"])
                self.assertEqual(repaired["videoReview"]["target"]["frame"], 182)
                self.assertEqual(repaired["videoReview"]["history"][-1]["source"], "relocate")
                if nid == "n1":
                    self.assertEqual(repaired["videoReview"]["batch"], 1)
                if nid == "n4":
                    self.assertEqual(repaired["videoReview"]["state"], "missing_evidence")


class VideoRelocateServerAndCliTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="ae-video-reloc-srv-")
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
                video_note("n1", "字幕遮挡图标", frame=96),
                video_note("n2", "4 至 6 秒音乐太响", start=120, end=180),
                video_note("n3", "片头镜头位置存疑", frame=12),
            ],
        )

    def _run_cli(self, argv):
        out, err = io.StringIO(), io.StringIO()
        previous_locale = notes_cli.i18n.current_locale()
        with redirect_stdout(out), redirect_stderr(err):
            try:
                code = notes_cli.main(argv)
            except SystemExit as exc:
                code = int(exc.code) if isinstance(exc.code, int) else 2
            finally:
                notes_cli.i18n.set_locale(previous_locale)
        return code, out.getvalue(), err.getvalue()

    def test_server_video_relocate_endpoint_security_and_cli_show_trace(self):
        store.handoff_submit(self.review, "提交 n3", note_ids=["n3"])
        store.handoff_claim(self.review, by="claude", seq=1)
        v2_file = self.root / "proj" / "out" / "v2.mp4"
        v2_file.write_bytes(self.v2_bytes)
        store.video_receipt(
            self.review,
            seq=1,
            receipt_id="rcpt-srv-reloc",
            movie=v2_file,
            by="claude",
            receipt={
                "notes": [
                    {
                        "noteId": "n3",
                        "outcome": "needs_relocate",
                        "versionId": "V2",
                        "relocateReason": "片头已重剪，需确认位置",
                    }
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
            "X-AE-Review": self.rid,
        }

        code, h_hdr, h_body = serve._req(server.port, "GET", "/api/health", {"Host": headers["Host"]})
        self.assertEqual(code, 200)
        features = json.loads(h_body)["features"]
        self.assertIn("video-compare", features)
        self.assertIn("video-relocate", features)
        self.assertIn("video-relocate-revision", features)
        self.assertNotIn("Access-Control-Allow-Origin", h_hdr)

        before = store.load(self.review)
        no_basis, _, response = serve._req(
            server.port,
            "POST",
            "/api/notes/n1/video-relocate",
            headers,
            {"versionId": "V2", "target": {"kind": "frame", "frame": 24}, "expectedEvidence": "legacy"},
        )
        self.assertEqual((no_basis, json.loads(response)["code"]), (400, "video-not-ready"))
        self.assertEqual(store.load(self.review), before)

        # 安全底线：Host / Origin / Content-Type / ReviewMismatch / 404 / 越界
        bad_host, _, _ = serve._req(
            server.port,
            "POST",
            "/api/notes/n3/video-relocate",
            {**headers, "Host": "evil.example:8765"},
            {"versionId": "V2", "target": {"kind": "frame", "frame": 24}, "summary": "x"},
        )
        self.assertEqual(bad_host, 421)

        no_origin, _, _ = serve._req(
            server.port,
            "POST",
            "/api/notes/n3/video-relocate",
            {"Host": headers["Host"], "Content-Type": "application/json"},
            {"versionId": "V2", "target": {"kind": "frame", "frame": 24}, "summary": "x"},
        )
        self.assertEqual(no_origin, 403)

        bad_origin, _, _ = serve._req(
            server.port,
            "POST",
            "/api/notes/n3/video-relocate",
            {**headers, "Origin": "http://evil.example"},
            {"versionId": "V2", "target": {"kind": "frame", "frame": 24}, "summary": "x"},
        )
        self.assertEqual(bad_origin, 403)

        bad_ctype, _, _ = serve._req(
            server.port,
            "POST",
            "/api/notes/n3/video-relocate",
            {**headers, "Content-Type": "text/plain"},
            raw=b"{}",
        )
        self.assertEqual(bad_ctype, 415)

        mismatch, _, _ = serve._req(
            server.port,
            "POST",
            "/api/notes/n3/video-relocate",
            {**headers, "X-AE-Review": "0000000000000000"},
            {"versionId": "V2", "target": {"kind": "frame", "frame": 24}, "summary": "x"},
        )
        self.assertEqual(mismatch, 409)

        missing_note, _, _ = serve._req(
            server.port,
            "POST",
            "/api/notes/missing99/video-relocate",
            headers,
            {"versionId": "V2", "target": {"kind": "frame", "frame": 24}, "summary": "x"},
        )
        self.assertEqual(missing_note, 404)

        revision = next(n for n in store.load(self.review)["notes"] if n["id"] == "n3")["videoReview"][
            "evidenceRevision"
        ]
        oob_code, _, oob_body = serve._req(
            server.port,
            "POST",
            "/api/notes/n3/video-relocate",
            headers,
            {
                "expectedEvidence": revision,
                "versionId": "V2",
                "target": {"kind": "frame", "frame": 192},
                "summary": "越界",
            },
        )
        self.assertEqual(oob_code, 400)
        self.assertEqual(json.loads(oob_body)["code"], "invalid-receipt-target")

        # 正常重定位并返回更新后的 doc / note / handoff
        ok_code, ok_hdr, ok_body = serve._req(
            server.port,
            "POST",
            "/api/notes/n3/video-relocate",
            headers,
            {
                "expectedEvidence": revision,
                "versionId": "V2",
                "target": {"kind": "frame", "frame": 24},
                "summary": "定位到 V2 第 24 帧并确认片头节奏",
                "reason": "片头增长 12 帧",
            },
        )
        self.assertEqual(ok_code, 200, ok_body)
        self.assertNotIn("Access-Control-Allow-Origin", ok_hdr)
        payload = json.loads(ok_body)
        self.assertEqual(payload["note"]["videoReview"]["state"], "pending_review")
        self.assertEqual(payload["note"]["videoReview"]["target"]["frame"], 24)
        self.assertEqual(payload["handoff"]["latest"]["videoStats"]["pendingReviewCount"], 1)
        for expected in (revision, ""):
            stale, _, raw = serve._req(
                server.port,
                "POST",
                "/api/notes/n3/video-relocate",
                headers,
                {"expectedEvidence": expected, "versionId": "V2", "target": {"kind": "frame", "frame": 25}},
            )
            self.assertEqual((stale, json.loads(raw)["code"]), (400, "stale-decision"))
        self.assertEqual(next(n for n in store.load(self.review)["notes"] if n["id"] == "n3"), payload["note"])

        # CLI show 中英文都能展示重定位记录，且不污染进程语言
        before_locale = notes_cli.i18n.current_locale()
        for lang, expected in (
            ("zh-CN", ("先前位置记录", "片头增长 12 帧", "24")),
            ("en", ("Prior location record", "24")),
        ):
            rc, sout, serr = self._run_cli(["show", str(self.review), "--all", "--locale", lang])
            self.assertEqual(rc, 0, serr)
            self.assertEqual(notes_cli.i18n.current_locale(), before_locale)
            for token in expected:
                self.assertIn(token, sout)

        # 人工通过后再重定位应拒绝
        res_code, _, res_body = serve._req(
            server.port,
            "PATCH",
            "/api/notes/n3",
            headers,
            {
                "status": "resolved",
                "expectedEvidence": next(n for n in store.load(self.review)["notes"] if n["id"] == "n3")["videoReview"][
                    "evidenceRevision"
                ],
            },
        )
        self.assertEqual(res_code, 200, res_body)
        decided_code, _, decided_body = serve._req(
            server.port,
            "POST",
            "/api/notes/n3/video-relocate",
            headers,
            {"versionId": "V2", "target": {"kind": "frame", "frame": 25}, "summary": "x"},
        )
        self.assertEqual(decided_code, 400)
        self.assertEqual(json.loads(decided_body)["code"], "note-already-decided")

    def test_docx_and_pptx_positioning_rules_remain_untouched(self):
        docx_dir = self.root / "docx-review"
        docx_dir.mkdir()
        (docx_dir / "review.json").write_text(
            json.dumps({"reviewId": "dddd000011112222", "format": {"report": "docx", "version": 1}}),
            encoding="utf-8",
        )
        digest = docx_review.text_digest("首段文本内容")
        store.import_notes(
            docx_dir,
            [
                {
                    "id": "d1",
                    "page": 1,
                    "text": "修正首段",
                    "docx": {
                        "v": 1,
                        "kind": "quote",
                        "via": "selection",
                        "para": {"id": "1A2B3C4D", "n": 1, "digest": digest},
                    },
                }
            ],
        )
        remapped = store.remap_pages(
            docx_dir,
            {1: "p1"},
            {"p1": 2},
            {2: "第2页"},
            docx={
                "paragraphs": [{"id": "1A2B3C4D", "n": 1, "digest": digest, "frags": [{"page": 2, "start": 0}]}],
                "keys": {2: "p1"},
            },
        )
        self.assertEqual(len(remapped["moved"]), 1)
        self.assertEqual(next(n for n in store.load(docx_dir)["notes"] if n["id"] == "d1")["page"], 2)

        pptx_dir = self.root / "pptx-review"
        pptx_dir.mkdir()
        (pptx_dir / "review.json").write_text(
            json.dumps({"reviewId": "eeee000011112222", "format": {"report": "pptx", "version": 1}}),
            encoding="utf-8",
        )
        store.import_notes(
            pptx_dir,
            [
                {
                    "id": "p1",
                    "page": 1,
                    "text": "调整幻灯片标题",
                    "pptx": {"v": 1, "kind": "page", "slide": "slide-1", "via": "thumb"},
                }
            ],
        )
        pptx_remapped = store.remap_pages(
            pptx_dir,
            {1: "slide-1"},
            {"slide-1": 3},
            {3: "第3页"},
            pptx={"slides": [{"key": "slide-1", "n": 3, "elements": []}], "keys": {3: "slide-1"}},
        )
        self.assertEqual(len(pptx_remapped["moved"]), 1)
        self.assertEqual(next(n for n in store.load(pptx_dir)["notes"] if n["id"] == "p1")["page"], 3)


class VideoComparePageTests(unittest.TestCase):
    def _manifest(self):
        return {
            "currentVersionId": "V2",
            "versions": [
                {
                    "versionId": "V1",
                    "url": "/api/video/versions/V1/media",
                    "fps": 30,
                    "timebase": {"num": 1, "den": 30},
                    "frames": 180,
                    "width": 640,
                    "height": 360,
                    "duration": 6.0,
                },
                {
                    "versionId": "V2",
                    "url": "/api/video/versions/V2/media",
                    "fps": 30,
                    "timebase": {"num": 1, "den": 30},
                    "frames": 192,
                    "width": 640,
                    "height": 360,
                    "duration": 6.4,
                },
                {
                    "versionId": "V3",
                    "url": "/api/video/versions/V3/media",
                    "fps": 24,
                    "timebase": {"num": 1, "den": 24},
                    "frames": 144,
                    "width": 640,
                    "height": 360,
                    "duration": 6.0,
                },
            ],
        }

    def test_compare_offers_a_cut_window_only_when_it_contains_each_in_movie_side(self):
        comparison = interaction_program(
            [(PURE_START, PURE_END)],
            "let videoRangeEnd=null; const manifest=" + json.dumps(self._manifest()) + ";",
            """
            return [[60,30,60],[60,60,90],[60,59,61],[0,0,1],[192,191,192],[192],[0],[60]].map(
              ([frame,contextStart,contextEnd])=>{
                const note={id:"n2",status:"drafted",video:{versionId:"V1",startFrame:65,endFrame:80},
                  videoReview:{state:"pending_review",outcome:"deleted",versionId:"V2",summary:"Removed shot",
                    target:{kind:"cut",frame,contextStart,contextEnd}}};
                const pair=videoComparePair(note,manifest);
                const range=pair.after.spec?.playRange;
                return [pair.ready, range?[range.startFrame,range.endFrame]:null];
              });
            """,
        )
        self.assertEqual(
            comparison,
            [
                [False, None],
                [False, None],
                [True, [59, 61]],
                [True, [0, 1]],
                [True, [191, 192]],
                [True, [162, 192]],
                [True, [0, 30]],
                [True, [30, 90]],
            ],
        )

    def test_pure_compare_helpers_use_per_version_timebase_and_format_deleted_and_relocated_cards(self):
        manifest = self._manifest()
        notes = [
            {
                "id": "n1",
                "status": "drafted",
                "text": "第96帧字幕遮挡图标",
                "video": {
                    "v": 1,
                    "versionId": "V1",
                    "frame": 96,
                    "fps": 30,
                    "timebase": {"num": 1, "den": 30},
                    "frames": 180,
                    "videoWidth": 640,
                    "videoHeight": 360,
                    "mark": {"kind": "region", "x": 0.1, "y": 0.8, "width": 0.5, "height": 0.1},
                },
                "videoReview": {
                    "state": "pending_review",
                    "versionId": "V2",
                    "summary": "片头增加 12 帧后上移字幕",
                    "target": {
                        "kind": "frame",
                        "versionId": "V2",
                        "frame": 108,
                        "mark": {"kind": "region", "x": 0.1, "y": 0.65, "width": 0.5, "height": 0.1},
                    },
                },
            },
            {
                "id": "n2",
                "status": "drafted",
                "text": "4 至 6 秒音乐太响",
                "video": {
                    "v": 1,
                    "versionId": "V1",
                    "startFrame": 120,
                    "endFrame": 180,
                    "fps": 30,
                    "timebase": {"num": 1, "den": 30},
                    "frames": 180,
                    "videoWidth": 640,
                    "videoHeight": 360,
                },
                "videoReview": {
                    "state": "pending_review",
                    "versionId": "V2",
                    "summary": "片头+12帧后对应 4.4–6.4s 压低配乐",
                    "target": {"kind": "range", "versionId": "V2", "startFrame": 132, "endFrame": 192},
                },
            },
            {
                "id": "n3",
                "status": "drafted",
                "text": "中间转场镜头多余",
                "video": {
                    "v": 1,
                    "versionId": "V1",
                    "startFrame": 30,
                    "endFrame": 60,
                    "fps": 30,
                    "timebase": {"num": 1, "den": 30},
                    "frames": 180,
                    "videoWidth": 640,
                    "videoHeight": 360,
                },
                "videoReview": {
                    "state": "pending_review",
                    "outcome": "deleted",
                    "versionId": "V2",
                    "summary": "已整段删去该冗余镜头",
                    "target": {"kind": "cut", "versionId": "V2", "frame": 42},
                },
            },
            {
                "id": "n4",
                "status": "open",
                "text": "片尾文案确认 <script>alert(1)</script>",
                "video": {
                    "v": 1,
                    "versionId": "V1",
                    "frame": 160,
                    "fps": 30,
                    "timebase": {"num": 1, "den": 30},
                    "frames": 180,
                    "videoWidth": 640,
                    "videoHeight": 360,
                },
                "videoReview": {
                    "state": "needs_relocate",
                    "versionId": "V2",
                    "relocateReason": "重剪后位置存疑 <b>待确认</b>",
                    "relocatedFrom": {"state": "needs_relocate", "versionId": "V2", "relocateReason": "重剪后位置存疑"},
                    "history": [
                        {
                            "source": "receipt",
                            "state": "needs_relocate",
                            "versionId": "V2",
                            "relocateReason": "重剪后位置存疑",
                            "by": "claude",
                            "at": "2026-11-22T00:00:00Z",
                        }
                    ],
                },
            },
        ]
        for lang in ("zh-CN", "en"):
            actual = interaction_program(
                [(PURE_START, PURE_END)],
                "let videoRangeEnd=null;",
                "const manifest="
                + json.dumps(manifest, ensure_ascii=False)
                + ",notes="
                + json.dumps(notes, ensure_ascii=False)
                + ";"
                "const v3=manifest.versions[2];"
                "return {"
                "p1:videoComparePair(notes[0],manifest),"
                "p2:videoComparePair(notes[1],manifest),"
                "p3:videoComparePair(notes[2],manifest),"
                "p4:videoComparePair(notes[3],manifest),"
                "t24:videoFrameTime(48,v3),"
                "f24:videoTimeToFrame(2.0,v3),"
                "cards:notes.map(n=>videoReceiptCardHtml(n,manifest))"
                "};",
                locale=lang,
            )
            p1, p2, p3, p4 = actual["p1"], actual["p2"], actual["p3"], actual["p4"]
            self.assertAlmostEqual(p1["before"]["spec"]["seekTime"], 3.2)
            self.assertAlmostEqual(p1["after"]["spec"]["seekTime"], 3.6)
            self.assertEqual(p1["before"]["mark"]["kind"], "region")
            self.assertEqual(p1["after"]["mark"]["y"], 0.65)
            self.assertAlmostEqual(p2["before"]["spec"]["playRange"]["startTime"], 4.0)
            self.assertAlmostEqual(p2["before"]["spec"]["playRange"]["endTime"], 6.0)
            self.assertAlmostEqual(p2["after"]["spec"]["playRange"]["startTime"], 4.4)
            self.assertAlmostEqual(p2["after"]["spec"]["playRange"]["endTime"], 6.4)
            self.assertEqual(p3["after"]["outcome"], "deleted")
            self.assertEqual(p3["after"]["spec"]["kind"], "cut")
            self.assertEqual(p3["after"]["spec"]["playRange"]["startFrame"], 12)
            self.assertEqual(p3["after"]["spec"]["playRange"]["endFrame"], 72)
            self.assertAlmostEqual(p4["before"]["spec"]["seekTime"], 160 / 30)
            self.assertIsNone(p4["after"]["spec"])
            self.assertTrue(p4["canRelocate"])
            self.assertAlmostEqual(actual["t24"], 2.0)
            self.assertEqual(actual["f24"], 48)
            self.assertIn('data-act="video-compare"', actual["cards"][0])
            self.assertIn('data-act="video-relocate"', actual["cards"][3])
            self.assertNotIn("<b>待确认</b>", actual["cards"][3])
            self.assertIn("&lt;b&gt;待确认&lt;/b&gt;", actual["cards"][3])

    def test_compare_stage_plays_before_after_with_unmuted_isolated_audio_and_relocates(self):
        manifest = self._manifest()
        notes = [
            {
                "id": "n2",
                "status": "drafted",
                "text": "4 至 6 秒音乐太响",
                "video": {
                    "v": 1,
                    "versionId": "V1",
                    "startFrame": 120,
                    "endFrame": 180,
                    "fps": 30,
                    "timebase": {"num": 1, "den": 30},
                    "frames": 180,
                    "videoWidth": 640,
                    "videoHeight": 360,
                },
                "videoReview": {
                    "state": "pending_review",
                    "versionId": "V2",
                    "summary": "调整音乐区间",
                    "target": {"kind": "range", "versionId": "V2", "startFrame": 132, "endFrame": 192},
                },
            },
            {
                "id": "n4",
                "status": "open",
                "text": "镜头位置待定位",
                "video": {
                    "v": 1,
                    "versionId": "V1",
                    "frame": 96,
                    "fps": 30,
                    "timebase": {"num": 1, "den": 30},
                    "frames": 180,
                    "videoWidth": 640,
                    "videoHeight": 360,
                    "mark": {"kind": "region", "x": 0.1, "y": 0.8, "width": 0.5, "height": 0.1},
                },
                "videoReview": {
                    "state": "needs_relocate",
                    "versionId": "V2",
                    "relocateReason": "片头增加 12 帧",
                    "history": [{"source": "receipt", "state": "needs_relocate", "versionId": "V2"}],
                },
            },
        ]
        setup = """
          const VIDEO = __MANIFEST__;
          let liveVideo = VIDEO, mode = "server", notes = __NOTES__, videoActiveVersionId = "V2", videoLatestId = "V2";
          let videoRangeEnd = null, videoSelectedId = null, videoShowingTarget = false, videoPendingMark = null;
          let markMode = false;
          const serverFeatures = new Set(["video", "video-receipt", "video-compare", "video-relocate", "video-relocate-revision"]);
          const hasFeature = (d, n) => Array.isArray(d.features) && d.features.includes(n);
          const srNodes = new Map();
          const makeVideo = id => {
            const listeners = {};
            const el = {
              id, src: "", currentTime: 0, muted: true, paused: true, readyState: 2, controls: true,
              videoWidth: 640, videoHeight: 360, style: {}, calls: [],
              getAttribute(k){ return k === "src" ? this.src : null },
              setAttribute(k, v){ if (k === "src") this.src = v; },
              load(){ this.calls.push("load"); },
              pause(){ this.paused = true; this.calls.push("pause"); },
              play(){ this.paused = false; this.calls.push("play"); return Promise.resolve(); },
              addEventListener(ev, fn){ (listeners[ev] ||= []).push(fn); },
            };
            return el;
          };
          const makeBox = id => ({
            id, hidden: false, value: "", textContent: "", innerHTML: "", clientWidth: 600, clientHeight: 340,
            style: {}, classList: { toggle(){}, add(){}, remove(){}, contains(){ return false; } },
            appendChild(){}, querySelectorAll(){ return []; },
          });
          const getEl = id => {
            if (!srNodes.has(id)) srNodes.set(id, (id === "videoPlayer" || id === "videoCompareBefore" || id === "videoCompareAfter") ? makeVideo(id) : makeBox(id));
            return srNodes.get(id);
          };
          const sr = { getElementById: getEl };
          const document = { createElement: () => makeBox("dyn") };
          const videoEl = () => getEl("videoPlayer");
          const videoManifest = () => liveVideo || VIDEO;
          const videoFindVersion = vid => (videoManifest().versions || []).find(v => v.versionId === (vid || videoActiveVersionId || videoManifest().currentVersionId)) || null;
          const videoCurrentVersion = () => videoFindVersion(videoActiveVersionId);
          const videoLayout = () => {};
          const setMarkMode = () => {};
          const mutate = () => {};
          const applyCalls = [], apiCalls = [], toasts = [];
          const toast = msg => toasts.push(msg);
          const applyServer = (d, q) => { applyCalls.push({d, q}); notes = d.notes || notes; };
          const api = async (method, path, body) => {
            apiCalls.push({method, path, body});
            const updated = {
              ...notes[1],
              status: "drafted",
              videoReview: {
                ...notes[1].videoReview,
                state: "pending_review",
                versionId: body.versionId,
                target: body.target,
                summary: body.summary,
              },
            };
            return { doc: { notes: [notes[0], updated], rev: 2, video: VIDEO }, note: updated, handoff: { latest: { seq: 1 } } };
          };
        """.replace("__MANIFEST__", json.dumps(manifest)).replace("__NOTES__", json.dumps(notes, ensure_ascii=False))

        actual = interaction_program(
            [(PURE_START, PURE_END), (COMPARE_START, COMPARE_END)],
            setup,
            """
          setVideoServerFeatures({ features: ["video", "video-receipt", "video-compare", "video-relocate", "video-relocate-revision"] });
          videoCompareOpen(notes[0], false);
          const bEl = getEl("videoCompareBefore"), aEl = getEl("videoCompareAfter");
          const rangeControls = [bEl.controls, aEl.controls];
          await videoComparePlay("before");
          const bPlaying = { muted: bEl.muted, time: bEl.currentTime, src: bEl.src, aPaused: aEl.paused };
          bEl.currentTime = 6.0;
          videoCompareTick("before");
          const bStopped = { paused: bEl.paused, time: bEl.currentTime };

          await videoComparePlay("after");
          const aPlaying = { muted: aEl.muted, time: aEl.currentTime, src: aEl.src, bPaused: bEl.paused };
          aEl.currentTime = 6.4;
          videoCompareTick("after");
          const aStopped = { paused: aEl.paused, time: aEl.currentTime };

          videoCompareOpen(notes[1], true);
          aEl.currentTime = 3.6;
          videoRelocateUseFrame();
          const capturedFrame = getEl("videoRelocateFrame").value;
          getEl("videoRelocateSummary").value = "已移位到 V2 第 108 帧";
          getEl("videoRelocateReason").value = "片头加了 12 帧";

          // 同版本/同批注的空轮询同步不得打断播放或清空已输入的重定位表单
          aEl.paused = false;
          aEl.currentTime = 3.8;
          videoCompareSync();
          const pollPreserved = {
            paused: aEl.paused,
            time: aEl.currentTime,
            summary: getEl("videoRelocateSummary").value,
          };

          // 能力与离线门禁：缺 video-relocate 或离线时不发请求并给出明确错误
          setVideoServerFeatures({ features: ["video", "video-receipt"] });
          await videoRelocateSave();
          const legacyErr = getEl("videoRelocateErr").textContent;
          setVideoServerFeatures({ features: ["video", "video-receipt", "video-compare", "video-relocate", "video-relocate-revision"] });
          mode = "offline";
          await videoRelocateSave();
          const offlineErr = getEl("videoRelocateErr").textContent;
          mode = "server";

          // 越界帧不提交到后端
          getEl("videoRelocateFrame").value = "999";
          await videoRelocateSave();
          const oobErr = getEl("videoRelocateErr").textContent;
          getEl("videoRelocateFrame").value = "108";

          await videoRelocateSave();
          const frameControls = [bEl.controls, aEl.controls];
          videoRelocatePopulate(videoComparePairState, true);
          const scrubControls = [bEl.controls, aEl.controls];
          videoRelocatePopulate(videoComparePairState, false);
          return { rangeControls, frameControls, scrubControls, afterScrub: aEl.controls, bPlaying, bStopped, aPlaying, aStopped, capturedFrame, pollPreserved, legacyErr, offlineErr, oobErr, apiCalls, applyLen: applyCalls.length, stateAfter: getEl("videoCompareState").textContent };
        """,
            locale="zh-CN",
        )
        self.assertEqual(actual["rangeControls"], [True, True])
        self.assertEqual(actual["frameControls"], [False, False])
        self.assertEqual(actual["scrubControls"], [False, True])
        self.assertFalse(actual["afterScrub"])
        self.assertFalse(actual["bPlaying"]["muted"])
        self.assertAlmostEqual(actual["bPlaying"]["time"], 4.0)
        self.assertEqual(actual["bPlaying"]["src"], "/api/video/versions/V1/media")
        self.assertTrue(actual["bPlaying"]["aPaused"])
        self.assertTrue(actual["bStopped"]["paused"])
        self.assertAlmostEqual(actual["bStopped"]["time"], 6.0)

        self.assertFalse(actual["aPlaying"]["muted"])
        self.assertAlmostEqual(actual["aPlaying"]["time"], 4.4)
        self.assertEqual(actual["aPlaying"]["src"], "/api/video/versions/V2/media")
        self.assertTrue(actual["aPlaying"]["bPaused"])
        self.assertTrue(actual["aStopped"]["paused"])
        self.assertAlmostEqual(actual["aStopped"]["time"], 6.4)

        self.assertEqual(str(actual["capturedFrame"]), "108")
        self.assertFalse(actual["pollPreserved"]["paused"])
        self.assertAlmostEqual(actual["pollPreserved"]["time"], 3.8)
        self.assertEqual(actual["pollPreserved"]["summary"], "已移位到 V2 第 108 帧")
        self.assertIn("video-relocate", actual["legacyErr"])
        self.assertTrue(actual["offlineErr"])
        self.assertTrue(actual["oobErr"])
        self.assertEqual(len(actual["apiCalls"]), 1)
        self.assertEqual(actual["apiCalls"][0]["method"], "POST")
        self.assertEqual(actual["apiCalls"][0]["path"], "/api/notes/n4/video-relocate")
        self.assertEqual(actual["apiCalls"][0]["body"]["target"], {"kind": "frame", "frame": 108})
        self.assertEqual(actual["apiCalls"][0]["body"]["expectedEvidence"], "legacy")
        self.assertEqual(actual["applyLen"], 1)

    def test_compare_and_relocate_dom_wiring_exists_in_template(self):
        source = TEMPLATE.read_text(encoding="utf-8")
        for fragment in (
            'id="videoCompareStage"',
            'id="videoCompareBefore"',
            'id="videoCompareAfter"',
            'id="videoCompareBeforeMarks"',
            'id="videoCompareAfterMarks"',
            'id="videoComparePlayBefore"',
            'id="videoComparePlayAfter"',
            'id="videoRelocateVersion"',
            'id="videoRelocateKind"',
            'id="videoRelocateFrame"',
            'id="videoRelocateSave"',
            "/video-relocate",
            'case "video-compare"',
            'case "video-relocate"',
            "setVideoServerFeatures",
        ):
            with self.subTest(fragment=fragment):
                self.assertIn(fragment, source)


if __name__ == "__main__":
    unittest.main()
