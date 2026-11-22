"""视频批注人工复核决定、选择性批量通过与退回重交闭环（#197）。

全部使用合成 MP4、合成工程路径与受控服务实例，按 TDD 切片验证存储层、批量确认、
选择性返工交接、防陈旧/防改写、HTTP 接口、CLI 输出与页面交互。
"""

from __future__ import annotations

import contextlib
import copy
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
sys.path.insert(0, str(ROOT))

import host_isolation  # noqa: E402

host_isolation.require_for_module(__name__)
os.environ["AE_HOME"] = tempfile.mkdtemp(prefix="ae-video-rework-home-")

import build_viewer  # noqa: E402
import ledger  # noqa: E402
import notes as notes_cli  # noqa: E402
import notes_store as store  # noqa: E402
import review_stop  # noqa: E402
import serve  # noqa: E402
import video_source  # noqa: E402
import wakeup  # noqa: E402
from tests.test_analysis_page import interaction_program  # noqa: E402
from tests.test_video_handoff import V1_MEDIA, video_note  # noqa: E402
from tests.test_video_review import synthetic_mp4  # noqa: E402


class VideoReviewReworkBase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="ae-video-rework-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.v1_bytes = synthetic_mp4(frames=180, fps=30, width=640, height=360)
        self.v2_bytes = synthetic_mp4(frames=192, fps=30, width=640, height=360)
        self.v3_bytes = synthetic_mp4(frames=192, fps=30, width=640, height=360)
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
                    "第96帧字幕遮挡图标",
                    frame=96,
                    mark={"kind": "region", "x": 0.1, "y": 0.8, "width": 0.5, "height": 0.1},
                ),
                video_note("n2", "4 至 6 秒音乐太响", start=120, end=180),
                video_note("n3", "片头镜头要保留还是重拍", frame=12),
                video_note("n4", "转场节奏略快", start=60, end=90),
                video_note("n5", "结尾调色偏暗", frame=170),
                video_note("n6", "片尾字幕字体偏小", frame=150),
            ],
        )
        wakeup.bind_wait(self.review, "Claude Code", "session-a")

    def _movie(self, data: bytes, name: str) -> Path:
        path = self.root / "proj" / "out" / name
        path.write_bytes(data)
        return path

    def _note(self, nid: str) -> dict:
        return next(n for n in store.load(self.review)["notes"] if n["id"] == nid)

    def _deliver_v2_mixed_batch(self) -> dict:
        """提交 n1..n5 并交回 V2 混合回执：n1/n2 待复核，n3 待答复，n4 待定位，n5 缺证据。"""
        store.handoff_submit(self.review, "首轮修复前五条", note_ids=["n1", "n2", "n3", "n4", "n5"])
        store.handoff_claim(self.review, by="claude", seq=1)
        v2_file = self._movie(self.v2_bytes, "v2.mp4")
        return store.video_receipt(
            self.review,
            seq=1,
            receipt_id="rcpt-v2-mixed",
            movie=v2_file,
            by="claude",
            receipt={
                "summary": "V2 修复字幕与音量，其余待确认",
                "notes": [
                    {
                        "noteId": "n1",
                        "outcome": "fixed",
                        "summary": "字幕上移避开图标",
                        "target": {"kind": "frame", "frame": 108},
                    },
                    {
                        "noteId": "n2",
                        "outcome": "fixed",
                        "summary": "背景音乐已压低 3dB",
                        "target": {"kind": "range", "startFrame": 132, "endFrame": 192},
                    },
                    {
                        "noteId": "n3",
                        "outcome": "question",
                        "question": "片头保留实拍还是改用动画？",
                    },
                    {
                        "noteId": "n4",
                        "outcome": "needs_relocate",
                        "relocateReason": "转场重剪后位置存疑",
                    },
                    {
                        "noteId": "n5",
                        "outcome": "fixed",
                        "summary": "已调色但缺目标帧",
                    },
                ],
            },
        )


class VideoDecisionApprovalAndBatchTests(VideoReviewReworkBase):
    def test_single_approval_records_human_decision_history_and_batch_stats(self):
        self._deliver_v2_mixed_batch()
        res = store.video_decision(
            self.review,
            "n1",
            action="approve",
            expected_batch=1,
            expected_version="V2",
            by="page",
        )
        self.assertFalse(res["replayed"])
        n1 = self._note("n1")
        self.assertEqual(n1["status"], "resolved")
        self.assertEqual(n1["resolvedBy"], "page")
        self.assertEqual(n1["resolvedBatch"], 1)
        vr = n1["videoReview"]
        self.assertEqual(vr["state"], "approved")
        self.assertEqual(vr["decision"], "approved")
        self.assertEqual(vr["decisionBatch"], 1)
        self.assertEqual(vr["decidedBy"], "page")
        self.assertTrue(vr["decidedAt"])
        self.assertEqual(len(vr["history"]), 2)
        self.assertEqual(vr["history"][-1]["source"], "decision")
        self.assertEqual(vr["history"][-1]["action"], "approve")
        self.assertEqual(vr["history"][-1]["decision"], "approved")
        self.assertEqual(vr["history"][-1]["versionId"], "V2")
        self.assertEqual(vr["history"][-1]["batch"], 1)
        self.assertEqual(n1["trail"][-1]["action"], "accepted")

        # 批次还有 n2 待复核，仍停在 drafted，并同步统计 approvedIds/pendingReviewIds
        hstate = store.handoff_state(self.review, full=True)
        b1 = hstate["history"][0]
        self.assertEqual(b1["status"], "drafted")
        self.assertEqual(b1["draftedIds"], ["n2"])
        self.assertEqual(b1["videoStats"]["approvedIds"], ["n1"])
        self.assertEqual(b1["videoStats"]["pendingReviewIds"], ["n2"])

        # 通过 set_status("resolved", by="page") 审批 n2 也记录完整人工决定并把第 1 批收尾为 done
        store.set_status(self.review, "n2", "resolved", by="page", reviewer=True)
        n2 = self._note("n2")
        self.assertEqual(n2["videoReview"]["decision"], "approved")
        self.assertEqual(n2["videoReview"]["state"], "approved")
        self.assertEqual(n2["videoReview"]["history"][-1]["decision"], "approved")

        # 重复确认 n1 幂等返回，不改时间戳也不重复追加 history / trail
        decided_at = n1["videoReview"]["decidedAt"]
        hist_len = len(n1["videoReview"]["history"])
        trail_len = len(n1["trail"])
        replay = store.video_decision(
            self.review,
            "n1",
            action="approve",
            expected_batch=1,
            expected_version="V2",
            by="page",
        )
        self.assertTrue(replay["replayed"])
        n1_after = self._note("n1")
        self.assertEqual(n1_after["videoReview"]["decidedAt"], decided_at)
        self.assertEqual(len(n1_after["videoReview"]["history"]), hist_len)
        self.assertEqual(len(n1_after["trail"]), trail_len)

        # set_status 再次 resolved 同批也不重复追加 history / trail
        store.set_status(self.review, "n2", "resolved", by="page", reviewer=True)
        n2_after = self._note("n2")
        self.assertEqual(len(n2_after["videoReview"]["history"]), 2)
        self.assertEqual(len(n2_after["trail"]), 2)

    def test_page_decision_retry_finishes_batch_after_an_interrupted_write(self):
        from unittest.mock import patch

        self._deliver_v2_mixed_batch()
        replace = os.replace

        def fail_batch(source, destination):
            if Path(destination) == self.review / "handoff.json":
                raise OSError("synthetic handoff write interruption")
            return replace(source, destination)

        with patch.object(os, "replace", side_effect=fail_batch), self.assertRaises(OSError):
            store.video_decision(self.review, "n1", action="approve")
        replay = store.video_decision(self.review, "n1", action="approve")
        self.assertTrue(replay["replayed"])
        self.assertEqual(store.handoff_load(self.review)["items"][0]["draftedIds"], ["n2"])
        self.assertEqual(len(self._note("n1")["videoReview"]["history"]), 2)

    def test_approval_requires_the_evidence_revision_selected_by_the_reviewer(self):
        self._deliver_v2_mixed_batch()
        stale = "older-location"
        with self.assertRaises(store.HandoffError) as why:
            store.video_decision(
                self.review, "n1", action="approve", expected_batch=1, expected_version="V2", expected_evidence=stale
            )
        self.assertEqual(why.exception.code, "stale-decision")
        result = store.handoff_finalize_all(
            self.review,
            {1: {"n1", "n2"}},
            evidence={"n1": stale, "n2": self._note("n2")["videoReview"]["evidenceRevision"]},
        )
        self.assertEqual(result["finalizedIds"], ["n2"])
        self.assertEqual(self._note("n1")["status"], "drafted")

    def test_selective_batch_finalize_approves_only_picked_ready_items_and_blocks_all_unready_states(self):
        self._deliver_v2_mixed_batch()
        # 额外提交 n6 并交回 failed 状态
        store.handoff_submit(self.review, "补交 n6", note_ids=["n6"])
        store.handoff_claim(self.review, by="claude", seq=2)
        store.video_receipt(
            self.review,
            seq=2,
            receipt_id="rcpt-n6-fail",
            render_error="ffmpeg encoder failed",
            by="claude",
        )

        # 第 1 批只选 n1（不选 n2），同时尝试夹带 n3(needs_reply), n4(needs_relocate), n5(missing_evidence) 与第 2 批 n6(failed)
        res = store.handoff_finalize_all(
            self.review,
            {1: {"n1", "n3", "n4", "n5"}, 2: {"n6"}},
        )
        self.assertEqual(res["finalizedIds"], ["n1"])
        self.assertEqual(res["finalizedCount"], 1)
        self.assertEqual(res["batches"], [1])
        skipped = {s["id"]: s for s in res["skipped"]}
        self.assertEqual(skipped["n2"]["reason"], "unconfirmed")
        for bad_id in ("n3", "n4", "n5", "n6"):
            self.assertEqual(skipped[bad_id]["reason"], "unready", bad_id)

        n1 = self._note("n1")
        n2 = self._note("n2")
        self.assertEqual(n1["status"], "resolved")
        self.assertEqual(n1["videoReview"]["decision"], "approved")
        self.assertEqual(n1["videoReview"]["state"], "approved")
        self.assertEqual(n1["videoReview"]["decisionBatch"], 1)
        self.assertEqual(n1["videoReview"]["history"][-1]["decision"], "approved")

        # 未选的 n2 保持 drafted/pending_review，批次 1 保持 drafted
        self.assertEqual(n2["status"], "drafted")
        self.assertEqual(n2["videoReview"]["state"], "pending_review")
        self.assertNotIn("decision", n2["videoReview"])
        b1 = store.handoff_state(self.review, full=True)["history"][1]
        self.assertEqual(b1["status"], "drafted")
        self.assertEqual(b1["draftedIds"], ["n2"])

        # 非完备状态单条 approve 全部拒绝
        for bad_id in ("n3", "n4", "n5", "n6"):
            with self.subTest(bad_id=bad_id), self.assertRaises(store.HandoffError) as exc:
                store.video_decision(self.review, bad_id, action="approve", by="page")
            self.assertEqual(exc.exception.code, "video-not-ready")

    def test_concurrent_changes_between_selection_and_finalize_report_actual_results(self):
        self._deliver_v2_mixed_batch()
        preview = store.handoff_state(self.review, full=False)["finalizable"]
        self.assertEqual(preview["batches"], [{"seq": 1, "ids": ["n1", "n2"]}])

        # 确认后、提交 finalize-all 前：n2 被并发退回（returned），同时 V2 媒体对 n1 仍完整
        store.video_decision(self.review, "n2", action="return", reason="音量还要再低 2dB", by="page")

        # 确认期间试图整批通过 [n1, n2]：仅 n1 通过，n2 被跳过且保持 returned/open 不受污染
        res = store.handoff_finalize_all(self.review, {1: {"n1", "n2"}})
        self.assertEqual(res["finalizedIds"], ["n1"])
        skipped = {s["id"]: s for s in res["skipped"]}
        self.assertEqual(skipped["n2"]["reason"], "unready")
        self.assertEqual(skipped["n2"]["detail"], "returned")
        n2 = self._note("n2")
        self.assertEqual(n2["status"], "open")
        self.assertEqual(n2["videoReview"]["state"], "returned")
        self.assertEqual(n2["videoReview"]["returnReason"], "音量还要再低 2dB")

        # 媒体缺失时即使选中也不能通过
        self._deliver_v2_batch_for_media_loss_test()

    def _deliver_v2_batch_for_media_loss_test(self):
        store.handoff_submit(self.review, "提交 n6", note_ids=["n6"])
        store.handoff_claim(self.review, by="claude", seq=2)
        v3_file = self._movie(self.v3_bytes, "v3-tmp.mp4")
        store.video_receipt(
            self.review,
            seq=2,
            receipt_id="rcpt-n6-ok",
            movie=v3_file,
            by="claude",
            receipt={
                "notes": [
                    {
                        "noteId": "n6",
                        "outcome": "fixed",
                        "summary": "字体放大",
                        "target": {"kind": "frame", "frame": 150},
                    }
                ]
            },
        )
        # 删除 V3 私有媒体模拟媒体缺失
        (self.review / "media" / "v3.mp4").unlink()
        res = store.handoff_finalize_all(self.review, {2: {"n6"}})
        self.assertEqual(res["finalizedCount"], 0)
        self.assertEqual(res["skipped"][0]["id"], "n6")
        self.assertEqual(res["skipped"][0]["reason"], "unready")
        self.assertEqual(self._note("n6")["status"], "drafted")
        with self.assertRaises(store.HandoffError) as exc:
            store.video_decision(self.review, "n6", action="approve", by="page")
        self.assertEqual(exc.exception.code, "video-not-ready")


class VideoReturnReplyReworkAndImmutabilityTests(VideoReviewReworkBase):
    def _run_cli(self, argv: list[str], *, env_extra: dict[str, str] | None = None):
        old_env = dict(os.environ)
        os.environ["AE_HARNESS"] = "Claude Code"
        os.environ["AE_SESSION_ID"] = "session-a"
        if env_extra:
            os.environ.update(env_extra)
        out, err = io.StringIO(), io.StringIO()
        try:
            with redirect_stdout(out), redirect_stderr(err):
                try:
                    code = notes_cli.main(argv)
                except SystemExit as exc:
                    code = int(exc.code) if isinstance(exc.code, int) else 2
        finally:
            os.environ.clear()
            os.environ.update(old_env)
        return code, out.getvalue(), err.getvalue()

    def test_return_and_reply_save_without_auto_submit_and_support_selective_rework_v1_v2_v3(self):
        self._deliver_v2_mixed_batch()
        before_batches = len(store.handoff_load(self.review)["items"])
        self.assertEqual(before_batches, 1)

        # V2 上：通过 n1（字幕），退回 n2（音量），答复 n3（待答复）
        store.video_decision(self.review, "n1", action="approve", expected_batch=1, expected_version="V2", by="page")
        ret = store.video_decision(
            self.review,
            "n2",
            action="return",
            reason="音乐还是太响，请再降 3dB",
            expected_batch=1,
            expected_version="V2",
            by="page",
        )
        rep = store.video_decision(
            self.review,
            "n3",
            action="reply",
            reply="片头改用 2 秒极简动画",
            expected_batch=1,
            expected_version="V2",
            by="page",
        )
        self.assertFalse(ret["replayed"])
        self.assertFalse(rep["replayed"])

        # 退回与答复绝不自动提交或新建 handoff 批次
        hdoc = store.handoff_load(self.review)
        self.assertEqual(len(hdoc["items"]), 1)
        self.assertEqual(hdoc["items"][0]["status"], "done")
        self.assertEqual(hdoc["items"][0]["leftOpen"], 4)
        state = store.handoff_state(self.review, full=True)
        self.assertEqual(state["inFlightIds"], [])
        self.assertEqual(state["history"][0]["videoStats"]["approvedIds"], ["n1"])
        self.assertEqual(state["history"][0]["videoStats"]["returnedIds"], ["n2"])
        self.assertEqual(state["history"][0]["videoStats"]["needsReplyIds"], ["n3"])

        n2 = self._note("n2")
        self.assertEqual(n2["status"], "open")
        self.assertEqual(n2["videoReview"]["state"], "returned")
        self.assertEqual(n2["videoReview"]["decision"], "returned")
        self.assertEqual(n2["videoReview"]["decisionBatch"], 1)
        self.assertEqual(n2["videoReview"]["returnReason"], "音乐还是太响，请再降 3dB")
        self.assertEqual(n2["trail"][-1]["action"], "returned")

        n3 = self._note("n3")
        self.assertEqual(n3["status"], "open")
        self.assertEqual(n3["videoReview"]["state"], "needs_reply")
        self.assertEqual(n3["videoReview"]["decision"], "replied")
        self.assertEqual(n3["videoReview"]["reply"], "片头改用 2 秒极简动画")
        self.assertTrue(n3["videoReview"]["repliedAt"])
        self.assertEqual(n3["videoReview"]["repliedBy"], "page")
        self.assertEqual(n3["trail"][-1]["action"], "replied")

        # 已答复但未重新交给 Agent 改稿前，仍不可通过审批
        with self.assertRaises(store.HandoffError) as exc_rep:
            store.video_decision(self.review, "n3", action="approve", by="page")
        self.assertEqual(exc_rep.exception.code, "video-not-ready")

        # 显式只勾选退回的音量批注 n2 提交第 2 批
        store.handoff_submit(self.review, "只重交音量 n2", note_ids=["n2"])
        c2, out2, err2 = self._run_cli(["claim", str(self.review)])
        self.assertEqual(c2, 0, err2)
        claim2 = json.loads(out2)
        self.assertTrue(claim2["claimed"])
        self.assertEqual(claim2["seq"], 2)
        self.assertEqual(claim2["noteIds"], ["n2"])
        self.assertEqual([m["noteId"] for m in claim2["materials"]], ["n2"])
        mat2 = claim2["materials"][0]
        self.assertEqual(mat2["video"]["versionId"], "V1")
        self.assertEqual((mat2["video"]["startFrame"], mat2["video"]["endFrame"]), (120, 180))
        self.assertEqual(mat2["videoReview"]["returnReason"], "音乐还是太响，请再降 3dB")
        self.assertEqual(mat2["videoReview"]["target"]["startFrame"], 132)
        self.assertEqual(len(mat2["videoReview"]["history"]), 2)

        # show --batch 2 只包含 n2，不含已通过的 n1 与未提交的答复 n3
        sc, sout, serr = self._run_cli(["show", str(self.review), "--batch", "2"])
        self.assertEqual(sc, 0, serr)
        self.assertIn("n2", sout)
        self.assertIn("音乐还是太响，请再降 3dB", sout)
        self.assertNotIn("n1", sout)
        self.assertNotIn("n3", sout)

        # 第 2 批交回 V3 修复结果，同一批注 ID n2 保留完整 V1 -> V2 退回 -> V3 证据历史
        v3_file = self._movie(self.v3_bytes, "v3.mp4")
        store.video_receipt(
            self.review,
            seq=2,
            receipt_id="rcpt-v3-n2",
            movie=v3_file,
            by="claude",
            receipt={
                "summary": "V3 按退回意见再压低 3dB",
                "notes": [
                    {
                        "noteId": "n2",
                        "outcome": "fixed",
                        "summary": "4.4–6.4s 背景音再降 3dB 至 -21 LUFS",
                        "target": {"kind": "range", "startFrame": 132, "endFrame": 192},
                    }
                ],
            },
        )
        n2_v3 = self._note("n2")
        self.assertEqual(n2_v3["id"], "n2")
        self.assertEqual(n2_v3["status"], "drafted")
        self.assertEqual(n2_v3["video"]["versionId"], "V1")
        self.assertEqual((n2_v3["video"]["startFrame"], n2_v3["video"]["endFrame"]), (120, 180))
        vr_v3 = n2_v3["videoReview"]
        self.assertEqual(vr_v3["state"], "pending_review")
        self.assertEqual(vr_v3["versionId"], "V3")
        self.assertNotIn("decision", vr_v3)
        self.assertNotIn("returnReason", vr_v3)
        self.assertEqual([h["state"] for h in vr_v3["history"]], ["pending_review", "returned", "pending_review"])
        self.assertEqual(vr_v3["history"][0]["versionId"], "V2")
        self.assertEqual(vr_v3["history"][1]["returnReason"], "音乐还是太响，请再降 3dB")
        self.assertEqual(vr_v3["history"][2]["versionId"], "V3")

        # 在 V3 上人工通过 n2
        store.video_decision(self.review, "n2", action="approve", expected_batch=2, expected_version="V3", by="page")
        n2_final = self._note("n2")
        self.assertEqual(n2_final["status"], "resolved")
        self.assertEqual(n2_final["videoReview"]["decision"], "approved")
        self.assertEqual(n2_final["videoReview"]["decisionBatch"], 2)
        self.assertEqual(
            [h["state"] for h in n2_final["videoReview"]["history"]],
            ["pending_review", "returned", "pending_review", "approved"],
        )

        # 随后若单独重交已答复的 n3（第 3 批），claim 携带其答复文本与历史
        store.handoff_submit(self.review, "提交已答复的 n3", note_ids=["n3"])
        c3, out3, err3 = self._run_cli(["claim", str(self.review)])
        self.assertEqual(c3, 0, err3)
        claim3 = json.loads(out3)
        self.assertEqual(claim3["noteIds"], ["n3"])
        self.assertEqual(claim3["materials"][0]["videoReview"]["reply"], "片头改用 2 秒极简动画")

    def test_immutability_idempotency_and_stale_receipt_protection(self):
        # 用 finish=False 模拟第 1 批仍在 claimed 时，审阅人先决定后再收到同批陈旧回执
        store.handoff_submit(self.review, "部分交回", note_ids=["n1", "n2", "n3"])
        store.handoff_claim(self.review, by="claude", seq=1)
        v2_file = self._movie(self.v2_bytes, "v2.mp4")
        store.video_receipt(
            self.review,
            seq=1,
            receipt_id="rcpt-partial-1",
            movie=v2_file,
            finish=False,
            by="claude",
            receipt={
                "notes": [
                    {
                        "noteId": "n1",
                        "outcome": "fixed",
                        "summary": "字幕上移",
                        "target": {"kind": "frame", "frame": 108},
                    },
                    {
                        "noteId": "n2",
                        "outcome": "fixed",
                        "summary": "压低音乐",
                        "target": {"kind": "range", "startFrame": 132, "endFrame": 192},
                    },
                    {"noteId": "n3", "outcome": "question", "question": "片头保留吗？"},
                ]
            },
        )
        store.video_decision(self.review, "n1", action="approve", expected_batch=1, expected_version="V2", by="page")
        store.video_decision(
            self.review,
            "n2",
            action="return",
            reason="低频不够干净",
            expected_batch=1,
            expected_version="V2",
            by="page",
        )
        store.video_decision(
            self.review,
            "n3",
            action="reply",
            reply="保留并微调音量",
            expected_batch=1,
            expected_version="V2",
            by="page",
        )

        # 1) approved 不可被任何操作改写
        for fn in (
            lambda: store.video_decision(self.review, "n1", action="return", reason="改主意", by="page"),
            lambda: store.video_decision(self.review, "n1", action="reply", reply="再答", by="page"),
            lambda: store.set_status(self.review, "n1", "open", resolution="重开", by="page"),
            lambda: store.relocate_video_note(self.review, "n1", target={"kind": "frame", "frame": 10}, summary="x"),
            lambda: store.video_receipt(
                self.review,
                seq=1,
                receipt_id="rcpt-stale-n1",
                receipt={"notes": [{"noteId": "n1", "outcome": "question", "question": "覆盖？"}]},
            ),
        ):
            with self.assertRaises(store.HandoffError) as exc:
                fn()
            self.assertEqual(exc.exception.code, "note-already-decided")

        # 2) return / reply 幂等重放不改写时间戳与历史
        n2_before = copy.deepcopy(self._note("n2"))
        rep_ret = store.video_decision(self.review, "n2", action="return", reason="低频不够干净", by="page")
        self.assertTrue(rep_ret["replayed"])
        self.assertEqual(self._note("n2"), n2_before)

        n3_before = copy.deepcopy(self._note("n3"))
        rep_rep = store.video_decision(self.review, "n3", action="reply", reply="保留并微调音量", by="page")
        self.assertTrue(rep_rep["replayed"])
        self.assertEqual(self._note("n3"), n3_before)

        # 3) 同批或更早批次（seq <= decisionBatch）的陈旧回执不能覆盖已记录的 return / reply
        for nid in ("n2", "n3"):
            with self.subTest(nid=nid), self.assertRaises(store.HandoffError) as exc_stale:
                store.video_receipt(
                    self.review,
                    seq=1,
                    receipt_id=f"rcpt-stale-{nid}",
                    receipt={
                        "notes": [
                            {
                                "noteId": nid,
                                "outcome": "fixed",
                                "summary": "陈旧回执覆盖",
                                "target": {"kind": "frame", "frame": 50},
                            }
                        ]
                    },
                )
            self.assertEqual(exc_stale.exception.code, "stale-receipt")

        # 4) 校验错误参数与过时 expected_batch / expected_version
        with self.assertRaises(store.HandoffError) as exc_empty_ret:
            store.video_decision(self.review, "n2", action="return", reason="   ", by="page")
        self.assertEqual(exc_empty_ret.exception.code, "video-return-reason-required")
        with self.assertRaises(store.HandoffError) as exc_empty_rep:
            store.video_decision(self.review, "n3", action="reply", reply=" ", by="page")
        self.assertEqual(exc_empty_rep.exception.code, "video-reply-required")
        with self.assertRaises(store.HandoffError) as exc_agent:
            store.video_decision(self.review, "n2", action="approve", by="claude")
        self.assertEqual(exc_agent.exception.code, "video-manual-approval-required")
        with self.assertRaises(store.HandoffError) as exc_stale_ctx:
            store.video_decision(self.review, "n2", action="return", reason="再退", expected_batch=99, by="page")
        self.assertEqual(exc_stale_ctx.exception.code, "stale-decision")

        # 显式重交的第 2 批（seq=2 > 1）可写入 V3 回执
        store.handoff_done(self.review, seq=1, summary="第1批结束", by="claude")
        store.handoff_submit(self.review, "第2批重交 n2", note_ids=["n2"])
        store.handoff_claim(self.review, by="claude", seq=2)
        v3_file = self._movie(self.v3_bytes, "v3-ok.mp4")
        res_v3 = store.video_receipt(
            self.review,
            seq=2,
            receipt_id="rcpt-v3-fresh",
            movie=v3_file,
            by="claude",
            receipt={
                "notes": [
                    {
                        "noteId": "n2",
                        "outcome": "fixed",
                        "summary": "低频已清理",
                        "target": {"kind": "range", "startFrame": 132, "endFrame": 192},
                    }
                ]
            },
        )
        self.assertEqual(res_v3["version"]["versionId"], "V3")

    def test_server_video_decision_endpoint_security_locks_and_cli_localization(self):
        self._deliver_v2_mixed_batch()
        server = serve.ReviewServer(self.review, 0, log_to_file=False, locale="zh-CN")
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

        hcode, _, hbody = serve._req(server.port, "GET", "/api/health", {"Host": headers["Host"]})
        self.assertEqual(hcode, 200)
        self.assertIn("video-decisions", json.loads(hbody)["features"])

        # 安全守卫：Host / Origin / Content-Type / Sec-Fetch-Site / X-AE-Review / 白名单
        for bad_hdrs, expected in (
            ({**headers, "Host": "evil.example:8765"}, 421),
            ({k: v for k, v in headers.items() if k != "Origin"}, 403),
            ({**headers, "Origin": "http://evil.example"}, 403),
            ({**headers, "Sec-Fetch-Site": "cross-site"}, 403),
            ({**headers, "Content-Type": "text/plain"}, 415),
            ({**headers, "X-AE-Review": "0000000000000000"}, 409),
        ):
            code, resp_hdrs, _ = serve._req(
                server.port,
                "POST",
                "/api/notes/n1/video-decision",
                bad_hdrs,
                {"action": "approve"},
            )
            self.assertEqual(code, expected)
            self.assertNotIn("Access-Control-Allow-Origin", resp_hdrs)

        for bad_body in (
            {"action": "approve", "extra": 1},
            {"action": "unknown"},
            {"action": "return", "reason": ""},
            {"action": "reply", "reply": 123},
            {"action": "approve", "expectedBatch": True},
        ):
            bcode, _, _ = serve._req(server.port, "POST", "/api/notes/n1/video-decision", headers, bad_body)
            self.assertEqual(bcode, 400)

        # 验证 video_decision 绝不嵌套 store.locked 与 ledger.metadata_lock
        held = {"store": False, "meta": False}
        orig_store_locked, orig_meta_lock = store.locked, ledger.metadata_lock

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

        store.locked, ledger.metadata_lock = track_store, track_meta
        try:
            c1, _, raw1 = serve._req(
                server.port,
                "POST",
                "/api/notes/n1/video-decision",
                headers,
                {
                    "action": "approve",
                    "expectedBatch": 1,
                    "expectedVersion": "V2",
                    "expectedEvidence": self._note("n1")["videoReview"]["evidenceRevision"],
                },
            )
            self.assertEqual(c1, 200, raw1)
            d1 = json.loads(raw1)
            self.assertFalse(d1["replayed"])
            self.assertEqual(d1["note"]["videoReview"]["decision"], "approved")

            # 重复确认返回 replayed: true
            c1r, _, raw1r = serve._req(
                server.port,
                "POST",
                "/api/notes/n1/video-decision",
                headers,
                {
                    "action": "approve",
                    "expectedBatch": 1,
                    "expectedVersion": "V2",
                    "expectedEvidence": self._note("n1")["videoReview"]["evidenceRevision"],
                },
            )
            self.assertEqual(c1r, 200, raw1r)
            self.assertTrue(json.loads(raw1r)["replayed"])

            c2, _, raw2 = serve._req(
                server.port,
                "POST",
                "/api/notes/n2/video-decision",
                headers,
                {"action": "return", "reason": "人声被背景音盖住", "expectedBatch": 1, "expectedVersion": "V2"},
            )
            self.assertEqual(c2, 200, raw2)
            self.assertEqual(json.loads(raw2)["note"]["videoReview"]["state"], "returned")

            c3, _, raw3 = serve._req(
                server.port,
                "POST",
                "/api/notes/n3/video-decision",
                headers,
                {"action": "reply", "reply": "保留原片头并渐隐", "expectedBatch": 1, "expectedVersion": "V2"},
            )
            self.assertEqual(c3, 200, raw3)
            self.assertEqual(json.loads(raw3)["note"]["videoReview"]["reply"], "保留原片头并渐隐")
        finally:
            store.locked, ledger.metadata_lock = orig_store_locked, orig_meta_lock

        # CLI show 与 batches 在中英文下正确渲染 decision / returnReason / reply / history / replied 痕迹
        for lang, expected_tokens in (
            ("zh-CN", ("已通过", "人声被背景音盖住", "保留原片头并渐隐", "已答复", "历史")),
            ("en", ("Approved", "人声被背景音盖住", "保留原片头并渐隐", "Replied", "History")),
        ):
            sc, sout, serr = self._run_cli(["show", str(self.review), "--all", "--locale", lang])
            self.assertEqual(sc, 0, serr)
            for tok in expected_tokens:
                self.assertIn(tok, sout)
            self.assertNotIn("cli.batch.trail.action.", sout)

        # 停止审阅门禁：stopping/stopped 后拒绝 video-decision
        review_stop.begin(self.review)
        scode, _, sraw = serve._req(
            server.port,
            "POST",
            "/api/notes/n4/video-decision",
            headers,
            {"action": "return", "reason": "stopped"},
        )
        self.assertEqual(scode, 409, sraw)
        self.assertEqual(json.loads(sraw)["code"], "review-stopped")

    def test_non_video_finalize_all_keeps_whole_batch_atomic_rule(self):
        doc_dir = self.root / "doc-review"
        build_viewer.build(
            ROOT / "fixtures" / "sample" / "report.html",
            ROOT / "fixtures" / "sample" / "script.md",
            doc_dir,
            no_scan_skills=True,
            locale="zh-CN",
        )
        n1 = store.add(doc_dir, {"page": 1, "target": "报告", "text": "意见一"})[1]["id"]
        n2 = store.add(doc_dir, {"page": 1, "target": "报告", "text": "意见二"})[1]["id"]
        store.handoff_submit(doc_dir, "文档批", target="review")
        store.handoff_claim(doc_dir, seq=1)
        store.set_status(doc_dir, n1, "resolved", resolution="已初改1", by="claude")
        store.set_status(doc_dir, n2, "resolved", resolution="已初改2", by="claude")
        store.handoff_done(doc_dir, seq=1)
        # 非视频审阅：只传 n1 而漏传同批 n2 时，整批被跳过，保持原有语义
        res = store.handoff_finalize_all(doc_dir, {1: {n1}})
        self.assertEqual(res["finalizedCount"], 0)
        self.assertEqual({s["id"] for s in res["skipped"]}, {n1, n2})


class VideoPageReworkUITests(unittest.TestCase):
    TEMPLATE = ROOT / "skill" / "annotation-everything" / "scripts" / "viewer_template.html"
    PURE_START = "// ---------- 视频审阅纯函数 ----------"
    PURE_END = "// ---------- 视频审阅 DOM ----------"
    COMPARE_START = "// ---------- 视频对照与重定位 ----------"
    COMPARE_END = "// ---------- 批注区：列表与输入框分开"
    NOTE_START = "function noteHtml(x){"
    NOTE_END = "// 把正在编辑的那张卡片"
    FINALIZE_START = "const FINALIZE_SKIP={"
    FINALIZE_END = '$("finalizeAllBtn").onclick=finalizeAll;'
    PICK_START = "// ---------- 提交弹窗的可勾选列表与源工程关联"
    PICK_END = "function renderHandSource(){"

    @staticmethod
    def _manifest():
        return {
            "v": 1,
            "currentVersionId": "V3",
            "versions": [
                {
                    "versionId": "V1",
                    "fps": 30,
                    "timebase": {"num": 1, "den": 30},
                    "frames": 180,
                    "width": 640,
                    "height": 360,
                    "url": "/api/video/versions/V1/media",
                },
                {
                    "versionId": "V2",
                    "fps": 30,
                    "timebase": {"num": 1, "den": 30},
                    "frames": 192,
                    "width": 640,
                    "height": 360,
                    "url": "/api/video/versions/V2/media",
                },
                {
                    "versionId": "V3",
                    "fps": 30,
                    "timebase": {"num": 1, "den": 30},
                    "frames": 192,
                    "width": 640,
                    "height": 360,
                    "url": "/api/video/versions/V3/media",
                },
            ],
        }

    def test_pure_history_receipt_card_and_note_html_render_decisions_safely(self):
        manifest = self._manifest()
        v1_anchor = {
            "v": 1,
            "versionId": "V1",
            "frame": 60,
            "fps": 30,
            "timebase": {"num": 1, "den": 30},
            "frames": 180,
            "videoWidth": 640,
            "videoHeight": 360,
        }
        notes = [
            {
                "id": "n1",
                "page": 1,
                "target": "00:02",
                "status": "drafted",
                "text": "字幕上移",
                "video": v1_anchor,
                "videoReview": {
                    "state": "pending_review",
                    "batch": 1,
                    "versionId": "V2",
                    "summary": "已上移字幕 24px",
                    "target": {"kind": "frame", "versionId": "V2", "frame": 60},
                },
            },
            {
                "id": "n2",
                "page": 1,
                "target": "00:02",
                "status": "open",
                "text": "背景音乐太响",
                "video": v1_anchor,
                "videoReview": {
                    "state": "needs_reply",
                    "batch": 1,
                    "question": "保留人声还是配乐全降？",
                    "reply": "人声清晰，配乐 <script>alert(1)</script> 降 6dB",
                    "decision": "replied",
                    "decisionBatch": 1,
                    "history": [
                        {
                            "source": "receipt",
                            "batch": 1,
                            "state": "needs_reply",
                            "question": "保留人声还是配乐全降？",
                            "by": "claude",
                            "at": "2026-11-22T01:00:00Z",
                        },
                        {
                            "source": "decision",
                            "batch": 1,
                            "action": "reply",
                            "decision": "replied",
                            "state": "needs_reply",
                            "reply": "人声清晰，配乐降 6dB",
                            "by": "page",
                            "at": "2026-11-22T01:02:00Z",
                        },
                    ],
                },
            },
            {
                "id": "n3",
                "page": 1,
                "target": "00:02",
                "status": "resolved",
                "resolvedBy": "page",
                "text": "片尾音量收尾",
                "video": v1_anchor,
                "videoReview": {
                    "state": "approved",
                    "decision": "approved",
                    "decisionBatch": 2,
                    "decidedBy": "page",
                    "decidedAt": "2026-11-22T02:00:00Z",
                    "batch": 2,
                    "versionId": "V3",
                    "summary": "已按退回要求压低音量",
                    "target": {"kind": "frame", "versionId": "V3", "frame": 72},
                    "history": [
                        {
                            "source": "receipt",
                            "batch": 1,
                            "versionId": "V2",
                            "state": "pending_review",
                            "summary": "第一次压音量",
                            "target": {"kind": "frame", "versionId": "V2", "frame": 60},
                        },
                        {
                            "source": "decision",
                            "batch": 1,
                            "action": "return",
                            "decision": "returned",
                            "state": "returned",
                            "returnReason": "还是盖住人声",
                            "by": "page",
                        },
                        {
                            "source": "receipt",
                            "batch": 2,
                            "versionId": "V3",
                            "state": "pending_review",
                            "summary": "二次压低至 -18LUFS",
                            "target": {"kind": "frame", "versionId": "V3", "frame": 72},
                        },
                        {
                            "source": "decision",
                            "batch": 2,
                            "action": "approve",
                            "decision": "approved",
                            "state": "approved",
                            "versionId": "V3",
                            "by": "page",
                        },
                    ],
                },
            },
            {
                "id": "n4",
                "page": 1,
                "target": "00:02",
                "status": "open",
                "text": "原镜头缺失",
                "video": v1_anchor,
                "videoReview": {"state": "needs_relocate", "versionId": "V2", "relocateReason": "找不到对应画面"},
            },
        ]
        # 1. Pure helpers + noteHtml WITH VIDEO defined
        for lang in ("zh-CN", "en"):
            setup = (
                "let videoRangeEnd=null,editingId=null,editBuf={},analysisSupported=false,DOCX=null,PPTX=null;"
                "const FILE=false,VIDEO=" + json.dumps(manifest) + ",videoManifest=()=>VIDEO;"
                "let videoApproveSel=new Set(['n1']);"
                "const P=[{n:1,title:'V'}],isDone=x=>x.status==='resolved',isHeld=x=>x.status==='held',isDraft=x=>x.status==='drafted',"
                "inFlight=()=>false,hasMark=()=>false,regionOf=()=>null,targetLabel=t=>t||'',alwaysFor=()=>[],trailText=()=>'',"
                "clip=(s,n)=>String(s||'').slice(0,n),editOf=()=>null;"
            )
            expr = (
                "const notes=" + json.dumps(notes, ensure_ascii=False) + ";"
                "return {"
                "batchReady:notes.map(n=>videoBatchApprovable(n,VIDEO)),"
                "histN3:videoTimelineHistory(notes[2]).map(h=>h.text),"
                "vrCards:notes.map(n=>videoReceiptCardHtml(n,VIDEO)),"
                "noteCards:notes.map(noteHtml)"
                "};"
            )
            actual = interaction_program(
                [(self.PURE_START, self.PURE_END), (self.NOTE_START, self.NOTE_END)], setup, expr, locale=lang
            )
            self.assertEqual(actual["batchReady"], [True, False, False, False])
            self.assertEqual(len(actual["histN3"]), 4)
            self.assertIn("还是盖住人声", actual["histN3"][1])
            self.assertIn("V3", actual["histN3"][3])
            # n1 pending_review has batch pick checkbox + approve + return-open buttons
            self.assertIn('data-act="video-batch-pick"', actual["vrCards"][0])
            self.assertIn('data-act="video-approve"', actual["noteCards"][0])
            self.assertIn('data-act="video-return-open"', actual["noteCards"][0])
            self.assertNotIn('data-act="reopen"', actual["noteCards"][0])
            # n2 needs_reply has no batch pick, has escaped reply + inline reply form, no approve button
            self.assertNotIn('data-act="video-batch-pick"', actual["vrCards"][1])
            self.assertNotIn("<script>alert(1)</script>", actual["vrCards"][1])
            self.assertIn("&lt;script&gt;alert(1)&lt;/script&gt;", actual["vrCards"][1])
            self.assertIn('data-vid-reply="n2"', actual["vrCards"][1])
            self.assertIn('data-act="video-reply-save"', actual["vrCards"][1])
            self.assertNotIn('data-act="video-approve"', actual["noteCards"][1])
            self.assertNotIn('data-act="resolve"', actual["noteCards"][1])
            # n3 approved is immutable in card UI: no batch pick, no edit, no reopen, no approve
            self.assertNotIn('data-act="video-batch-pick"', actual["vrCards"][2])
            self.assertIn("vr-ok", actual["vrCards"][2])
            self.assertNotIn('data-act="reopen"', actual["noteCards"][2])
            self.assertNotIn('data-act="edit"', actual["noteCards"][2])
            self.assertNotIn('data-act="video-approve"', actual["noteCards"][2])
            # n4 needs_relocate has no batch pick and no approve button
            self.assertNotIn('data-act="video-batch-pick"', actual["vrCards"][3])
            self.assertNotIn('data-act="video-approve"', actual["noteCards"][3])

        # 2. noteHtml extracted WITHOUT VIDEO defined must NOT throw ReferenceError
        doc_setup = (
            "let editingId=null,editBuf={},analysisSupported=false;const FILE=false,P=[{n:1,title:'S1'}];"
            "const isDone=x=>x.status==='resolved',isHeld=()=>false,isDraft=()=>false,inFlight=()=>false,"
            "hasMark=()=>false,regionOf=()=>null,targetLabel=t=>t||'',alwaysFor=()=>[],trailText=()=>'',"
            "clip=(s,n)=>String(s||'').slice(0,n),editOf=()=>null;"
        )
        doc_html = interaction_program(
            [(self.NOTE_START, self.NOTE_END)],
            doc_setup,
            "return noteHtml({id:'d1',page:1,target:'报告',status:'open',text:'普通报告批注'});",
            locale="zh-CN",
        )
        self.assertIn("普通报告批注", doc_html)
        self.assertIn('data-act="resolve"', doc_html)

    def test_selective_batch_approve_ui_filters_to_picked_ready_notes_and_blocks_unready(self):
        manifest = self._manifest()
        v1_anchor = {
            "v": 1,
            "versionId": "V1",
            "frame": 30,
            "fps": 30,
            "timebase": {"num": 1, "den": 30},
            "frames": 180,
            "videoWidth": 640,
            "videoHeight": 360,
        }
        notes = [
            {
                "id": "n1",
                "status": "drafted",
                "draftBatch": 1,
                "text": "N1",
                "video": v1_anchor,
                "videoReview": {
                    "state": "pending_review",
                    "batch": 1,
                    "versionId": "V2",
                    "summary": "S1",
                    "target": {"kind": "frame", "versionId": "V2", "frame": 30},
                },
            },
            {
                "id": "n2",
                "status": "drafted",
                "draftBatch": 1,
                "text": "N2",
                "video": v1_anchor,
                "videoReview": {
                    "state": "pending_review",
                    "batch": 1,
                    "versionId": "V2",
                    "summary": "S2",
                    "target": {"kind": "frame", "versionId": "V2", "frame": 60},
                },
            },
            {
                "id": "n3",
                "status": "open",
                "text": "N3",
                "video": v1_anchor,
                "videoReview": {"state": "needs_reply", "batch": 1, "question": "Q"},
            },
            {
                "id": "n4",
                "status": "open",
                "text": "N4",
                "video": v1_anchor,
                "videoReview": {"state": "needs_relocate", "batch": 1, "relocateReason": "R"},
            },
            {
                "id": "n5",
                "status": "open",
                "text": "N5",
                "video": v1_anchor,
                "videoReview": {"state": "returned", "batch": 1, "returnReason": "Back"},
            },
            {
                "id": "n6",
                "status": "open",
                "text": "N6",
                "video": v1_anchor,
                "videoReview": {"state": "failed", "batch": 1, "failure": "Err"},
            },
            {
                "id": "n7",
                "status": "drafted",
                "draftBatch": 1,
                "text": "N7",
                "video": v1_anchor,
                "videoReview": {"state": "missing_evidence", "batch": 1, "versionId": "V2", "summary": "No tgt"},
            },
        ]
        plan = {
            "count": 2,
            "batches": [{"seq": 1, "ids": ["n1", "n2"]}],
            "skipped": [
                {"id": "n3", "reason": "unready", "detail": "needs_reply"},
                {"id": "n4", "reason": "unready", "detail": "needs_relocate"},
                {"id": "n5", "reason": "unready", "detail": "returned"},
                {"id": "n6", "reason": "unready", "detail": "failed"},
                {"id": "n7", "reason": "unready", "detail": "missing_evidence"},
            ],
        }
        setup = (
            "let videoRangeEnd=null;const FILE=false,VIDEO="
            + json.dumps(manifest)
            + ";let mode='server',finalizeAllSupported=true,finalizingAll=false,finalizeAllError='',finalizeAllResult=null,busy=0;"
            "let hand={finalizable:" + json.dumps(plan) + "};let notes=" + json.dumps(notes, ensure_ascii=False) + ";"
            "const pend={get:()=>[]},failed={get:()=>[]},isDraft=x=>x.status==='drafted',videoManifest=()=>VIDEO;"
            "const toasts=[],confirms=[],apiCalls=[];let playable=false;const videoApprovalPlayable=async()=>playable;"
            "const toast=m=>toasts.push(m);const confirm=m=>{confirms.push(m);return true};"
            "const flush=async()=>true,updateMode=()=>{},loadBatches=()=>{},sameReview=()=>true;"
            "const applyServer=(d,q)=>{if(d.notes)notes=d.notes;if(d.handoff)hand=d.handoff;if(typeof videoSyncApproveSel==='function')videoSyncApproveSel();renderFinalizeAll()};"
            "const getNotes=async()=>({notes,handoff:hand});"
            "const api=async(m,p,b)=>{apiCalls.push({m,p,b});return {doc:{notes,rev:2},handoff:{finalizable:{count:1,batches:[{seq:1,ids:['n2']}],skipped:[]}},finalizedCount:1,finalizedIds:['n1'],skippedCount:0,skipped:[],batches:[{seq:1,ids:['n1']}]}};"
        )
        actual = interaction_program(
            [(self.PURE_START, self.PURE_END), (self.FINALIZE_START, self.FINALIZE_END)],
            setup,
            """
            videoSyncApproveSel();
            renderFinalizeAll();
            const initialDisabled = $("finalizeAllBtn").disabled;
            const initialSelSize = videoApproveSel.size;
            const infoText = $("finalizeAllInfo").textContent;
            const blockedHtml = $("videoBatchBlocked") ? $("videoBatchBlocked").textContent : infoText;

            // 试图直接勾选一个不可复核条目也不得生效，只勾选 n1
            videoToggleApprovePick("n3", true);
            videoToggleApprovePick("n1", true);
            const selAfterPick = [...videoApproveSel.keys()];
            renderFinalizeAll();
            const btnAfterPick = $("finalizeAllBtn").textContent;
            await finalizeAll();
            const blockedCount=apiCalls.length;
            playable=true;
            await finalizeAll();
            const postPicked = apiCalls[0] ? apiCalls[0].b : null;

            // 全选待复核只能选中 n1, n2（其余 n3..n7 全部排除）
            videoSelectAllReady();
            const selAll = [...videoApproveSel.keys()];
            return { initialDisabled, initialSelSize, blockedHtml, selAfterPick, btnAfterPick, postPicked, selAll, blockedCount };
            """,
            locale="zh-CN",
        )
        self.assertTrue(actual["initialDisabled"])
        self.assertEqual(actual["initialSelSize"], 0)
        self.assertTrue(actual["blockedHtml"])
        self.assertEqual(actual["blockedCount"], 0)
        self.assertEqual(actual["selAfterPick"], ["n1"])
        self.assertIn("1", actual["btnAfterPick"])
        self.assertEqual(actual["postPicked"], {"batches": [{"seq": 1, "ids": ["n1"]}], "evidence": {"n1": "legacy"}})
        self.assertEqual(actual["selAll"], ["n1", "n2"])

    def test_selective_rework_handpick_and_compare_decisions_wire_cleanly(self):
        manifest = self._manifest()
        v1_anchor = {
            "v": 1,
            "versionId": "V1",
            "frame": 30,
            "fps": 30,
            "timebase": {"num": 1, "den": 30},
            "frames": 180,
            "videoWidth": 640,
            "videoHeight": 360,
        }
        # Rework scenario: n1 approved, n2 returned, n3 needs_reply with saved reply, n4 new open
        rework_notes = [
            {
                "id": "n1",
                "status": "resolved",
                "text": "N1",
                "video": v1_anchor,
                "videoReview": {"state": "approved", "decision": "approved", "batch": 1, "versionId": "V2"},
            },
            {
                "id": "n2",
                "status": "open",
                "text": "音乐太响",
                "video": v1_anchor,
                "videoReview": {
                    "state": "returned",
                    "decision": "returned",
                    "returnReason": "仍然盖过人声",
                    "batch": 1,
                    "versionId": "V2",
                },
            },
            {
                "id": "n3",
                "status": "open",
                "text": "配乐选型",
                "video": v1_anchor,
                "videoReview": {
                    "state": "needs_reply",
                    "question": "要轻快吗？",
                    "reply": "换轻快的",
                    "decision": "replied",
                    "batch": 1,
                },
            },
            {"id": "n4", "status": "open", "text": "新增建议", "video": v1_anchor},
        ]
        # 1. renderHandPick behavior on rework vs fresh V1
        pick_setup = (
            "let videoRangeEnd=null;const VIDEO="
            + json.dumps(manifest)
            + ";let mode='server',handMode=null,srvNoSelect=false,handPickSel=null,flight=new Set(['n9']);"
            "const clip=(s,n)=>String(s||'').slice(0,n);"
            "$('hPickOld').style={};$('handMsg').addEventListener=()=>{};const LS={set:()=>true},HOPT='options';"
            "let handPickClick=null;$('handPick').addEventListener=(ev,fn)=>{if(ev==='click')handPickClick=fn};"
            "$('handPick').querySelector=()=>null;"
            "let notes="
            + json.dumps([{"id": "f1", "status": "open", "text": "V1批注", "video": v1_anchor}], ensure_ascii=False)
            + ";"
        )
        pick_actual = interaction_program(
            [(self.PURE_START, self.PURE_END), (self.PICK_START, self.PICK_END)],
            pick_setup,
            """
            // Fresh V1: defaults to all open selected, with an explicit ID snapshot
            renderHandPick();
            const v1Sel = [...handPickSel];
            const v1Ids = handoffNoteIds(submittableNotes().map(x=>x.id), handPickSel);

            // Rework V2: starts empty so reviewer explicitly chooses rework items (won't accidentally carry n3)
            notes = """
            + json.dumps(rework_notes, ensure_ascii=False)
            + """;
            handPickSel = null;
            renderHandPick();
            const reworkInitial = [...handPickSel];
            const reworkHtml = $('handPick').innerHTML;

            // Trigger actual [data-pick="returned"] click handler on handPick
            handPickClick({ target: { closest: sel => sel === '[data-pick]' ? { dataset: { pick: 'returned' } } : null } });
            const afterReturnedBtn = [...handPickSel];
            const retHandoffIds = handoffNoteIds(submittableNotes().map(x=>x.id), handPickSel);
            return { v1Sel, v1Ids, reworkInitial, reworkHtml, afterReturnedBtn, retHandoffIds };
            """,
            locale="zh-CN",
        )
        self.assertEqual(pick_actual["v1Sel"], ["f1"])
        self.assertEqual(pick_actual["v1Ids"], ["f1"])
        self.assertEqual(pick_actual["reworkInitial"], [])
        self.assertNotIn("N1", pick_actual["reworkHtml"])
        self.assertIn("仍然盖过人声", pick_actual["reworkHtml"])
        self.assertIn("换轻快的", pick_actual["reworkHtml"])
        self.assertEqual(pick_actual["afterReturnedBtn"], ["n2"])
        self.assertEqual(pick_actual["retHandoffIds"], ["n2"])

        # 2. videoCompare stage decisions (approve / return / reply) call /api/notes/<id>/video-decision
        cmp_notes = [
            {
                "id": "n2",
                "status": "drafted",
                "draftBatch": 2,
                "text": "音乐太响",
                "video": v1_anchor,
                "videoReview": {
                    "state": "pending_review",
                    "batch": 2,
                    "versionId": "V2",
                    "summary": "已压低",
                    "target": {"kind": "frame", "versionId": "V2", "frame": 30},
                },
            },
            {
                "id": "n3",
                "status": "open",
                "text": "配乐选型",
                "video": v1_anchor,
                "videoReview": {"state": "needs_reply", "batch": 2, "question": "要轻快吗？"},
            },
        ]
        cmp_setup = (
            "let videoActiveVersionId='V2',mode='server';const VIDEO="
            + json.dumps(manifest)
            + ";let notes="
            + json.dumps(cmp_notes, ensure_ascii=False)
            + ";"
            "const srNodes=new Map();"
            "const makeBox=id=>({id,hidden:false,disabled:false,value:'',textContent:'',innerHTML:'',clientWidth:600,clientHeight:340,style:{},classList:{toggle(){},add(){},remove(){},contains(){return false}},appendChild(){},querySelectorAll(){return []},addEventListener(){},pause(){},load(){},setAttribute(){},getAttribute(){return null},currentTime:0,duration:6.4,paused:true,muted:false});"
            "const getEl=id=>{if(!srNodes.has(id))srNodes.set(id,makeBox(id));return srNodes.get(id)};"
            "const sr={getElementById:getEl},probes=[];let decode=false;const document={createElement:tag=>tag==='video'?{removeAttribute(){this.src=''},load(){if(this.src){probes.push(this.src);queueMicrotask(()=>decode?this.onloadeddata():this.onerror())}}}:makeBox('d')};"
            "const videoEl=()=>getEl('videoPlayer'),videoManifest=()=>VIDEO,videoFindVersion=vid=>VIDEO.versions.find(v=>v.versionId===(vid||videoActiveVersionId))||null,videoCurrentVersion=()=>videoFindVersion(videoActiveVersionId);"
            "const videoLayout=()=>{},setMarkMode=()=>{},mutate=()=>{};"
            "const apiCalls=[],toasts=[],applyCalls=[];"
            "const toast=m=>toasts.push(m);const applyServer=(d,q)=>applyCalls.push({d,q});"
            "const api=async(m,p,b)=>{apiCalls.push({method:m,path:p,body:b});return {ok:true,replayed:false,doc:{notes,rev:3,video:VIDEO},note:notes[0],handoff:{latest:{seq:2}}}};"
        )
        cmp_actual = interaction_program(
            [(self.PURE_START, self.PURE_END), (self.COMPARE_START, self.COMPARE_END)],
            cmp_setup,
            """
            setVideoServerFeatures({ features: ["video", "video-receipt", "video-compare", "video-relocate", "video-decisions", "video-evidence-revision"] });
            videoCompareOpen(notes[0], false);
            await videoCompareDecision("approve");
            const blockedCount=apiCalls.length;
            decode=true;
            await videoCompareDecision("approve");
            getEl("videoCompareReturnReason").value = "  ";
            await videoCompareDecision("return");
            const emptyErr = getEl("videoCompareDecisionErr").textContent;
            getEl("videoCompareReturnReason").value = "再轻 3dB";
            await videoCompareDecision("return");
            videoCompareOpen(notes[1], false);
            const appHiddenOnReply = getEl("videoCompareApprove").hidden;
            const retHiddenOnReply = getEl("videoCompareReturnBtn").hidden;
            const replyBoxHidden = getEl("videoCompareReplyBox").hidden;
            getEl("videoCompareReplyInput").value = "换成轻快的纯音乐";
            await videoCompareDecision("reply");
            return { apiCalls, blockedCount, probes, emptyErr, appHiddenOnReply, retHiddenOnReply, replyBoxHidden, applyCount: applyCalls.length };
            """,
            locale="zh-CN",
        )
        self.assertEqual(cmp_actual["blockedCount"], 0)
        self.assertEqual(set(cmp_actual["probes"]), {"/api/video/versions/V1/media", "/api/video/versions/V2/media"})
        self.assertTrue(cmp_actual["emptyErr"])
        self.assertEqual(len(cmp_actual["apiCalls"]), 3)
        self.assertEqual(cmp_actual["apiCalls"][0]["path"], "/api/notes/n2/video-decision")
        self.assertEqual(
            cmp_actual["apiCalls"][0]["body"],
            {"action": "approve", "expectedBatch": 2, "expectedVersion": "V2", "expectedEvidence": "legacy"},
        )
        self.assertEqual(
            cmp_actual["apiCalls"][1]["body"],
            {"action": "return", "reason": "再轻 3dB", "expectedBatch": 2, "expectedVersion": "V2"},
        )
        self.assertTrue(cmp_actual["appHiddenOnReply"])
        self.assertFalse(cmp_actual["replyBoxHidden"])
        self.assertEqual(cmp_actual["apiCalls"][2]["path"], "/api/notes/n3/video-decision")
        self.assertEqual(
            cmp_actual["apiCalls"][2]["body"], {"action": "reply", "reply": "换成轻快的纯音乐", "expectedBatch": 2}
        )

    def test_rework_dom_wiring_exists_in_template(self):
        source = self.TEMPLATE.read_text(encoding="utf-8")
        for fragment in (
            'id="finalizeAllBar"',
            'id="videoBatchTools"',
            'id="videoBatchSelectAll"',
            'id="videoBatchClear"',
            'id="videoBatchBlocked"',
            'id="videoCompareReturnBtn"',
            'id="videoCompareReturnBox"',
            'id="videoCompareReplyBox"',
            "/video-decision",
            '"video-decisions"',
            'data-act="video-approve"',
            'data-act="video-return-open"',
            'data-act="video-reply-save"',
            'data-act="video-batch-pick"',
            'data-pick="returned"',
            "videoBatchApprovable",
        ):
            with self.subTest(fragment=fragment):
                self.assertIn(fragment, source)


if __name__ == "__main__":
    unittest.main()
