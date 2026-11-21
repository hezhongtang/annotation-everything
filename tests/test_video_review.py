"""视频审阅构建（#192）：合成 MP4、V1 私有副本与不可变证据。"""

from __future__ import annotations

import hashlib
import json
import os
import struct
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "skill" / "annotation-everything" / "scripts"
sys.path.insert(0, str(SCRIPTS))
os.environ["AE_HOME"] = tempfile.mkdtemp(prefix="ae-video-test-home-")

import build_viewer  # noqa: E402


def box(kind: bytes, payload: bytes) -> bytes:
    return struct.pack(">I4s", len(payload) + 8, kind) + payload


def full_box(kind: bytes, version: int, flags: int, payload: bytes) -> bytes:
    return box(kind, bytes((version,)) + flags.to_bytes(3, "big") + payload)


def synthetic_mp4(*, frames: int = 180, fps: int = 30, width: int = 640, height: int = 360) -> bytes:
    """只含合成元数据与占位媒体字节的 MP4；构建和 API 测试不依赖本机编解码器。"""
    duration = frames
    mvhd = full_box(
        "mvhd".encode(),
        0,
        0,
        struct.pack(">IIIIII", 0, 0, fps, duration, 0x00010000, 0x01000000) + b"\0" * 80,
    )
    mdhd = full_box("mdhd".encode(), 0, 0, struct.pack(">IIIIHH", 0, 0, fps, duration, 0x55C4, 0))
    stts = full_box("stts".encode(), 0, 0, struct.pack(">III", 1, frames, 1))
    avc1 = b"\0" * 6 + struct.pack(">H", 1) + b"\0" * 16 + struct.pack(">HH", width, height) + b"\0" * 50
    stsd = full_box("stsd".encode(), 0, 0, struct.pack(">I", 1) + box(b"avc1", avc1))
    stbl = box(b"stbl", stsd + stts)
    minf = box(b"minf", stbl)
    hdlr = full_box("hdlr".encode(), 0, 0, b"\0" * 4 + b"vide" + b"\0" * 12 + b"Synthetic video\0")
    mdia = box(b"mdia", mdhd + hdlr + minf)
    tkhd = full_box(
        "tkhd".encode(),
        0,
        3,
        struct.pack(">IIIII", 0, 0, 1, 0, duration) + b"\0" * 52 + struct.pack(">II", width << 16, height << 16),
    )
    trak = box(b"trak", tkhd + mdia)
    moov = box(b"moov", mvhd + trak)
    return box(b"ftyp", b"isom\0\0\2\0isomiso2avc1mp41") + moov + box(b"mdat", b"synthetic-media")


class VideoBuildTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="ae-video-build-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = self.root / "synthetic.mp4"
        self.media = synthetic_mp4()
        self.source.write_bytes(self.media)
        self.out = self.root / "review"

    def build(self):
        return build_viewer.build(self.source, None, self.out, no_scan_skills=True, locale="zh-CN")

    def test_mp4_build_registers_v1_and_private_immutable_copy(self):
        result = self.build()
        manifest = json.loads((self.out / "review.json").read_text(encoding="utf-8"))

        self.assertEqual(result["pages"], 1)
        self.assertEqual(manifest["format"], {"report": "video", "version": 1})
        self.assertEqual(manifest["video"]["currentVersionId"], "V1")
        version = manifest["video"]["versions"][0]
        self.assertEqual(version["versionId"], "V1")
        self.assertEqual(version["path"], "media/v1.mp4")
        self.assertEqual(version["sha256"], hashlib.sha256(self.media).hexdigest())
        self.assertEqual((version["width"], version["height"]), (640, 360))
        self.assertEqual(version["duration"], 6.0)
        self.assertEqual(version["fps"], 30.0)
        self.assertEqual(version["timebase"], {"num": 1, "den": 30})
        self.assertEqual(version["frames"], 180)

        private = self.out / version["path"]
        self.source.write_bytes(b"overwritten outside the review")
        self.assertEqual(private.read_bytes(), self.media)
        page = (self.out / "index.html").read_text(encoding="utf-8")
        self.assertIn('id="videoPlayer"', page)
        self.assertIn('"versionId": "V1"', page)

    def test_rebuild_uses_registered_copy_and_does_not_remap_notes(self):
        self.build()
        notes = {
            "version": 1,
            "rev": 1,
            "notes": [
                {
                    "id": "frame96",
                    "page": 1,
                    "text": "合成字幕意见",
                    "video": {
                        "v": 1,
                        "versionId": "V1",
                        "frame": 96,
                        "fps": 30,
                        "timebase": {"num": 1, "den": 30},
                        "videoWidth": 640,
                        "videoHeight": 360,
                        "mark": {"kind": "region", "x": 0.1, "y": 0.8, "width": 0.5, "height": 0.1},
                    },
                }
            ],
        }
        (self.out / "notes.json").write_text(json.dumps(notes), encoding="utf-8")
        before = (self.out / "notes.json").read_bytes()
        self.source.write_bytes(b"not the registered movie")

        self.assertEqual(build_viewer.main(["--rebuild", str(self.out)]), 0)

        self.assertEqual((self.out / "notes.json").read_bytes(), before)
        manifest = json.loads((self.out / "review.json").read_text(encoding="utf-8"))
        private = self.out / manifest["video"]["versions"][0]["path"]
        self.assertEqual(private.read_bytes(), self.media)

    def test_non_mp4_and_invalid_mp4_are_rejected_with_specific_reason(self):
        bogus = self.root / "broken.mp4"
        bogus.write_bytes(b"not an mp4")
        with self.assertRaisesRegex(SystemExit, "MP4|mp4"):
            build_viewer.build(bogus, None, self.root / "broken-review", no_scan_skills=True)


if __name__ == "__main__":
    unittest.main()
