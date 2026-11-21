"""视频批注定位与交换（#192）：不可变版本、精确帧和半开区间。"""

from __future__ import annotations

import copy
import os
import sys
import tempfile
import unittest
from pathlib import Path

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
    (path / "review.json").write_text("{}", encoding="utf-8")
    return path


class VideoStoreTests(unittest.TestCase):
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
