"""视频批注定位与交换（#192）：不可变版本、精确帧和半开区间。"""

from __future__ import annotations

import copy
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "skill" / "annotation-everything" / "scripts"
sys.path.insert(0, str(SCRIPTS))
os.environ["AE_HOME"] = tempfile.mkdtemp(prefix="ae-video-store-home-")

import export_format  # noqa: E402
import i18n  # noqa: E402
import notes as notes_cli  # noqa: E402
import notes_store as store  # noqa: E402


def video_frame():
    return {
        "v": 1,
        "versionId": "V1",
        "frame": 96,
        "fps": 30,
        "timebase": {"num": 1, "den": 30},
        "frames": 180,
        "videoWidth": 640,
        "videoHeight": 360,
        "mark": {"kind": "region", "x": 0.1, "y": 0.8, "width": 0.5, "height": 0.1},
    }


def video_range():
    return {
        "v": 1,
        "versionId": "V1",
        "startFrame": 120,
        "endFrame": 180,
        "fps": 30,
        "timebase": {"num": 1, "den": 30},
        "frames": 180,
        "videoWidth": 640,
        "videoHeight": 360,
    }


def review_dir() -> Path:
    path = Path(tempfile.mkdtemp(prefix="ae-video-store-"))
    version = {
        "versionId": "V1",
        "fps": 30,
        "frames": 180,
        "timebase": {"num": 1, "den": 30},
        "width": 640,
        "height": 360,
    }
    (path / "review.json").write_text(json.dumps({"video": {"currentVersionId": "V1", "versions": [version]}}))
    return path


class VideoStoreTests(unittest.TestCase):
    def test_interrupted_commit_recovers_all_accepted_notes_above_16_mib(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            anchor = video_frame()
            version = {
                "versionId": "V1",
                "frames": 180,
                "fps": 30,
                "timebase": anchor["timebase"],
                "width": 640,
                "height": 360,
            }
            (directory / "review.json").write_text(
                json.dumps({"reviewId": "ab" * 8, "video": {"currentVersionId": "V1", "versions": [version]}}),
                encoding="utf-8",
            )
            notes = [
                {"id": f"n{i}", "page": 1, "text": f"{i} " + "合成意见" * 4800, "video": anchor} for i in range(300)
            ]
            doc, added, _, _ = store.import_notes(directory, notes)
            self.assertEqual(added, len(notes))
            handoff = {"seq": 1, "items": [{"seq": 1, "status": "drafted", "openIds": ["n0"]}]}
            with mock.patch.object(store, "_publish_video_commit", side_effect=OSError("interrupted")):
                with self.assertRaises(OSError):
                    store._commit_video_pair(directory, doc, handoff, {"op": "video-receipt"}, {"op": "handoff-draft"})
            journal = directory / store.VIDEO_COMMIT_FILE
            self.assertGreater(journal.stat().st_size, 16 * 1024 * 1024)
            with self.assertRaisesRegex(ValueError, "file-too-large"):
                store.fsutil.read_private_text(journal, max_bytes=32 * 1024)
            with store.locked(directory):
                self.assertEqual(store.load(directory), doc)
                self.assertEqual(store.handoff_load(directory), handoff)
            self.assertFalse(journal.exists())

    def test_frame_and_range_keep_original_version_time_and_normalized_mark(self):
        directory = review_dir()
        _doc, frame = store.add(
            directory,
            {"id": "subtitle96", "page": 1, "text": "第 96 帧字幕需调整", "video": video_frame()},
        )
        _doc, music = store.add(
            directory,
            {"id": "music46", "page": 1, "text": "4 至 6 秒音乐过响", "video": video_range()},
        )

        self.assertEqual(frame["video"], video_frame())
        self.assertEqual(music["video"], video_range())
        self.assertNotIn("mark", music["video"])
        self.assertEqual(frame["page"], 1)
        self.assertEqual(frame["text"], "第 96 帧字幕需调整")

    def test_invalid_temporal_shapes_and_marks_are_rejected(self):
        invalid = []
        for value in (-1, 1.5, True, 180):
            anchor = video_frame()
            anchor["frame"] = value
            invalid.append(anchor)
        for start, end in ((120, 120), (121, 120), (-1, 2), (120, 181), (1.5, 3)):
            anchor = video_range()
            anchor["startFrame"], anchor["endFrame"] = start, end
            invalid.append(anchor)
        for mark in (
            {"kind": "point", "x": -0.1, "y": 0.5},
            {"kind": "point", "x": 0.5, "y": 1.1},
            {"kind": "region", "x": 0.8, "y": 0.8, "width": 0.3, "height": 0.1},
            {"kind": "region", "x": 0.1, "y": 0.1, "width": 0, "height": 0.1},
            {"kind": "other", "x": 0.1, "y": 0.1},
        ):
            anchor = video_frame()
            anchor["mark"] = mark
            invalid.append(anchor)
        both = video_frame()
        both.update(startFrame=96, endFrame=97)
        invalid.append(both)

        for anchor in invalid:
            with self.subTest(anchor=anchor), self.assertRaises(store.NoteError):
                store.add(review_dir(), {"page": 1, "text": "坏定位", "video": anchor})

    def test_video_location_is_immutable_when_text_or_status_changes(self):
        directory = review_dir()
        _doc, note = store.add(directory, {"id": "fixed", "page": 1, "text": "原话", "video": video_frame()})
        _doc, updated = store.update(directory, note["id"], {"text": "补充说明，位置不变"})
        self.assertEqual(updated["video"], video_frame())
        with self.assertRaises(store.NoteError):
            store.update(directory, note["id"], {"video": video_range()})
        with self.assertRaises(store.NoteError):
            store.update(directory, note["id"], {"video": None})
        legacy = review_dir()
        _doc, old = store.add(legacy, {"id": "legacy", "page": 2, "text": "旧批注"})
        with self.assertRaises(store.NoteError):
            store.update(legacy, old["id"], {"video": video_frame()})

    def test_dedup_uses_video_location_and_markdown_round_trip_preserves_it(self):
        directory = review_dir()
        first = {"id": "one", "page": 1, "text": "同一句话", "video": video_frame(), "at": "2026-01-01T00:00:00+00:00"}
        second = {"id": "two", "page": 1, "text": "同一句话", "video": video_range(), "at": "2026-01-01T00:00:01+00:00"}
        _doc, added, skipped, updated = store.import_notes(directory, [first, second, copy.deepcopy(first)])
        self.assertEqual((added, skipped, updated), (2, 1, 0))

        notes = store.load(directory)["notes"]
        for lang in ("zh-CN", "en"):
            text = export_format.render(notes, title="合成视频", lang=lang)
            parsed, _always = export_format.parse_markdown(text)
            self.assertEqual([item["video"] for item in parsed], [video_frame(), video_range()])

    def test_document_writes_reject_new_video_locations(self):
        for kind in ("html", "docx", "pptx"):
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as tmp:
                directory = Path(tmp)
                manifest = {} if kind == "html" else {"format": {"report": kind, "version": 1}}
                (directory / "review.json").write_text(json.dumps(manifest))
                note = {"id": "doc", "page": 1, "text": "保留原段落"}
                foreign = {"id": "foreign", "page": 1, "text": "字幕上移", "video": video_frame()}
                previous, _ = store.add(directory, note)
                for label, write in (
                    ("add", lambda: store.add(directory, foreign)),
                    ("import", lambda: store.import_notes(directory, [foreign])),
                    ("backfill", lambda: store.update(directory, "doc", {"video": video_frame()})),
                    (
                        "import existing",
                        lambda: store.import_notes(directory, [{**foreign, "id": "doc", "updatedAt": "2099-01-01"}]),
                    ),
                ):
                    with self.subTest(entry=label), self.assertRaisesRegex(store.NoteError, "视频审阅"):
                        write()
                self.assertEqual(store.load(directory), previous)

    def test_document_finalize_paths_preserve_only_legacy_video_items(self):
        anchors = {
            "html": {},
            "docx": {
                "docx": {
                    "v": 1,
                    "kind": "quote",
                    "via": "selection",
                    "para": {"id": "1234ABCD", "n": 0, "digest": "0123456789ab"},
                }
            },
            "pptx": {"pptx": {"v": 1, "slide": "s260", "kind": "page", "via": "thumb"}},
        }
        for kind, anchor in anchors.items():
            for action in ("done", "finalize", "finalize-all", "accept-rest"):
                for mixed in (False, True):
                    with self.subTest(kind=kind, action=action, mixed=mixed), tempfile.TemporaryDirectory() as tmp:
                        d = Path(tmp)
                        manifest = {} if kind == "html" else {"format": {"report": kind, "version": 1}}
                        (d / "review.json").write_text(json.dumps(manifest))
                        store.add(d, {"id": "doc", "page": 1, "text": "修正标题", **anchor})
                        if mixed:
                            store.add(d, {"id": "legacy", "page": 1, "text": "字幕上移"})
                            store.add(d, {"id": "approved", "page": 1, "text": "镜头已确认"})
                            store.set_status(d, "approved", "resolved")
                        store.handoff_submit(d, target="final" if action == "done" else "review")
                        store.handoff_claim(d)
                        store.set_status(d, "doc", "drafted", seq=1, by="editor")
                        if mixed:
                            old, _ = store.set_status(d, "legacy", "drafted", seq=1, by="editor")
                            # 旧版 review.json 与 notes.json 原样读入，模拟已经落盘的跨格式定位。
                            old["notes"][1]["video"] = video_frame()
                            old["notes"][2].update(
                                video=video_range(), videoReview={"state": "approved", "decision": "approved"}
                            )
                            protected = copy.deepcopy(old["notes"][1:])
                            (d / store.NOTES_FILE).write_text(json.dumps(old))
                            with self.assertRaises(store.HandoffError):
                                store.set_status(d, "legacy", "resolved", seq=1, by="editor")
                        store.handoff_done(d, seq=1)
                        if action == "finalize":
                            store.handoff_finalize(d, 1)
                        elif action == "finalize-all":
                            result = store.handoff_finalize_all(d, {1: {"doc", "legacy"} if mixed else {"doc"}})
                            self.assertEqual(result["finalizedIds"], ["doc"])
                        elif action == "accept-rest":
                            store.handoff_request_review(d, 1)
                            store.handoff_claim(d, stage="review")
                            store.handoff_done(d, seq=1, accept_rest=True)
                        actual = store.load(d)["notes"]
                        self.assertEqual(actual[0]["status"], "resolved")
                        self.assertEqual(actual[0]["resolvedBatch"], 1)
                        self.assertEqual({key: actual[0][key] for key in anchor}, anchor)
                        self.assertEqual(store.handoff_state(d)["latest"]["status"], "drafted" if mixed else "done")
                        if mixed:
                            with self.assertRaises(store.HandoffError):
                                store.set_status(d, "approved", "drafted", by="editor")
                            self.assertEqual(store.load(d)["notes"][1:], protected)

    def test_newer_location_version_survives_without_being_used_as_v1(self):
        future = {"v": 2, "versionId": "V9", "future": {"opaque": [1, 2, 3]}}
        _doc, note = store.add(review_dir(), {"page": 1, "text": "未来格式", "video": future})
        self.assertEqual(note["video"], future)
        for locale in ("zh-CN", "en"):
            i18n.set_locale(locale)
            with self.subTest(locale=locale):
                shown = notes_cli.video_lines(note, {"video": {"currentVersionId": "V1"}}, i18n.Translator(locale))
                self.assertIn("v2", " ".join(shown))
                self.assertIn("v1", " ".join(shown))

    def test_cli_show_describes_registered_frame_and_range_in_both_languages(self):
        manifest = {"video": {"currentVersionId": "V1"}}
        for locale in ("zh-CN", "en"):
            i18n.set_locale(locale)
            translator = i18n.Translator(locale)
            with self.subTest(locale=locale):
                frame = notes_cli.video_lines({"video": video_frame()}, manifest, translator)
                music = notes_cli.video_lines({"video": video_range()}, manifest, translator)
                self.assertIn("V1", " ".join(frame))
                self.assertIn("96", " ".join(frame))
                self.assertIn("120", " ".join(music))
                self.assertIn("180", " ".join(music))


if __name__ == "__main__":
    unittest.main()
