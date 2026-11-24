"""视频审阅构建（#192）：合成 MP4、V1 私有副本与不可变证据。"""

from __future__ import annotations

import copy
import hashlib
import json
import os
import struct
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "skill" / "annotation-everything" / "scripts"
sys.path.insert(0, str(SCRIPTS))
os.environ["AE_HOME"] = tempfile.mkdtemp(prefix="ae-video-test-home-")

import build_viewer  # noqa: E402
import i18n  # noqa: E402
import video_review  # noqa: E402


def run_video_cli(main, argv: list[str]) -> int:
    """在本进程运行 CLI，隔离语言和定住状态，异常退出时也恢复。"""
    with patch.object(i18n, "_STATE", copy.copy(i18n._STATE)):
        return main(argv)


def box(kind: bytes, payload: bytes) -> bytes:
    return struct.pack(">I4s", len(payload) + 8, kind) + payload


def full_box(kind: bytes, version: int, flags: int, payload: bytes) -> bytes:
    return box(kind, bytes((version,)) + flags.to_bytes(3, "big") + payload)


def synthetic_mp4(
    *,
    frames: int = 180,
    fps: int = 30,
    width: int = 640,
    height: int = 360,
    edits: bytes = b"",
    composition: bytes = b"",
) -> bytes:
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
    header = box(b"ftyp", b"isom" + bytes(4) + b"isomiso2avc1mp41")
    media = box(b"mdat", bytes((42,)) * frames)
    stsc = full_box(b"stsc", 0, 0, struct.pack(">IIII", 1, 1, frames, 1))
    stsz = full_box(b"stsz", 0, 0, struct.pack(">II", 1, frames))
    stco = full_box(b"stco", 0, 0, struct.pack(">II", 1, len(header) + 8))
    stbl = box(b"stbl", stsd + stts + stsc + stsz + stco + composition)
    minf = box(b"minf", stbl)
    hdlr = full_box("hdlr".encode(), 0, 0, b"\0" * 4 + b"vide" + b"\0" * 12 + b"Synthetic video\0")
    mdia = box(b"mdia", mdhd + hdlr + minf)
    tkhd = full_box(
        "tkhd".encode(),
        0,
        3,
        struct.pack(">IIIII", 0, 0, 1, 0, duration) + b"\0" * 52 + struct.pack(">II", width << 16, height << 16),
    )
    trak = box(b"trak", tkhd + edits + mdia)
    moov = box(b"moov", mvhd + trak)
    return header + media + moov


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

    def test_edit_list_uses_playback_timeline_and_distinct_movie_clock(self):
        for version, fmt in ((0, ">Iihh"), (1, ">Qqhh")):
            with self.subTest(version=version):
                # 120 帧等于 4000 个 movie ticks。前两帧的呈现偏移来自 ctts。
                edit = full_box(b"elst", version, 0, struct.pack(">I", 1) + struct.pack(fmt, 4000, 62, 1, 0))
                composition = full_box(b"ctts", 0, 0, struct.pack(">III", 1, 180, 2))
                data = bytearray(synthetic_mp4(edits=box(b"edts", edit), composition=composition))
                struct.pack_into(">II", data, data.index(b"mvhd") + 16, 1000, 4000)
                self.source.write_bytes(data)
                meta = video_review.probe(self.source)
                self.assertEqual((meta["duration"], meta["frames"], meta["fps"]), (4, 120, 30))

    def test_unrepresentable_edit_lists_are_rejected_before_registration(self):
        for count, start, rate, length in ((2, 0, 1, 60), (1, -1, 1, 120), (1, 0, 2, 120), (1, 100, 1, 120)):
            with self.subTest(count=count, start=start, rate=rate):
                entries = struct.pack(">Iihh", length, start, rate, 0) * count
                edit = full_box(b"elst", 0, 0, struct.pack(">I", count) + entries)
                self.source.write_bytes(synthetic_mp4(edits=box(b"edts", edit)))
                with self.assertRaisesRegex(build_viewer.BuildError, "编辑"):
                    self.build()
                self.assertFalse((self.out / "media/v1.mp4").exists())

    def test_interrupted_first_build_recovers_only_its_own_original(self):
        with self.assertRaises(SystemExit):
            build_viewer.build(self.source, None, self.out, sections_arg="invalid", no_scan_skills=True)
        private = self.out / "media" / "v1.mp4"
        self.assertEqual(private.read_bytes(), self.media)
        original_inode = private.stat().st_ino
        other = self.root / "other.mp4"
        other.write_bytes(self.media)
        with self.assertRaises(build_viewer.BuildError):
            build_viewer.build(other, None, self.out, no_scan_skills=True)
        self.build()
        manifest = json.loads((self.out / "review.json").read_text())
        self.assertEqual(manifest["video"]["versions"][0]["versionId"], "V1")
        self.assertEqual(private.stat().st_ino, original_inode)
        unknown = self.root / "unknown"
        (unknown / "media").mkdir(parents=True)
        (unknown / "media" / "v1.mp4").write_bytes(self.media)
        with self.assertRaises(build_viewer.BuildError):
            build_viewer.build(self.source, None, unknown, no_scan_skills=True)

    def test_registration_retry_removes_abandoned_copy_and_preserves_media(self):
        worker = textwrap.dedent("""
            import os, sys
            from pathlib import Path
            sys.path.insert(0, sys.argv[1])
            import video_review

            review, source, stage = Path(sys.argv[2]), Path(sys.argv[3]), sys.argv[4]
            if stage == "copy":
                os.fsync = lambda fd: os._exit(73)
            else:
                publish = os.link
                def stop_after_publish(source, destination):
                    publish(source, destination)
                    os._exit(73)
                os.link = stop_after_publish
            video_review.register_version(review, source, batch=1, by="agent", receipt_id="retry",
                                          await_receipt=stage == "receipt")
        """)
        for interruption in ("copy", "publish", "receipt"):
            with self.subTest(interruption=interruption):
                review = self.root / interruption
                build_viewer.build(self.source, None, review, no_scan_skills=True)
                media = review / "media"
                v1 = media / "v1.mp4"
                v1_inode = v1.stat().st_ino
                revision = media / ".v2-original.tmp"
                revision.write_bytes(synthetic_mp4(frames=192))
                revision.chmod(0o600)
                other = media / "personal.tmp"
                other.write_text("synthetic local file")
                result = subprocess.run(
                    [sys.executable, "-c", worker, str(SCRIPTS), str(review), str(revision), interruption],
                    capture_output=True,
                    timeout=10,
                )
                self.assertEqual(result.returncode, 73, result.stderr)
                abandoned = set(media.rglob("*.tmp")) - {revision, other}
                self.assertEqual(len(abandoned), 1)

                if interruption == "receipt":
                    pending = json.loads((review / ".video-registration.json").read_text())
                    video_review.finish_receipt_version(review, pending, accepted=True)
                    version = json.loads((review / "review.json").read_text())["video"]["versions"][-1]
                else:
                    version = video_review.register_version(review, revision, batch=1, by="agent", receipt_id="retry")
                self.assertEqual(version["versionId"], "V2")
                self.assertEqual([file for file in abandoned if file.exists()], [])
                self.assertEqual((v1.stat().st_ino, v1.read_bytes()), (v1_inode, self.media))
                self.assertEqual((media / "v2.mp4").read_bytes(), revision.read_bytes())
                self.assertEqual(revision.read_bytes(), synthetic_mp4(frames=192))
                self.assertEqual(other.read_text(), "synthetic local file")

    def test_registration_preserves_another_staging_named_input(self):
        self.build()
        media = self.out / "media"
        waiting = media / ".v2-personal.tmp"
        waiting.write_bytes(synthetic_mp4(frames=128))
        waiting.chmod(0o600)
        other = self.root / "first.mp4"
        other.write_bytes(synthetic_mp4(frames=190))
        video_review.register_version(self.out, other, batch=1, by="agent", receipt_id="first")
        self.assertTrue(waiting.is_file())
        version = video_review.register_version(self.out, waiting, batch=2, by="agent", receipt_id="second")
        self.assertEqual(version["frames"], 128)
        self.assertEqual(waiting.read_bytes(), (media / "v3.mp4").read_bytes())

    def test_concurrent_registration_preserves_the_active_copy(self):
        import threading
        from concurrent.futures import ThreadPoolExecutor
        from unittest.mock import patch
        import ledger

        self.build()
        staged, contender = threading.Event(), threading.Event()
        link, open_file = os.link, os.open

        def wait_at_publication(source, target):
            if Path(target).name == "v2.mp4":
                staged.set()
                self.assertTrue(contender.wait(5), "第二次登记未到达元数据锁")
            return link(source, target)

        def observe_lock(path, *args, **kwargs):
            if staged.is_set() and Path(path).name == ledger.METADATA_LOCK_FILE:
                contender.set()
            return open_file(path, *args, **kwargs)

        movies = [self.root / f"revision-{frames}.mp4" for frames in (192, 204)]
        for path, frames in zip(movies, (192, 204)):
            path.write_bytes(synthetic_mp4(frames=frames))
        with patch.object(os, "link", wait_at_publication), patch.object(os, "open", observe_lock):
            with ThreadPoolExecutor(max_workers=2) as workers:
                first = workers.submit(video_review.register_version, self.out, movies[0], batch=1, by="agent")
                self.assertTrue(staged.wait(5))
                second = workers.submit(video_review.register_version, self.out, movies[1], batch=2, by="agent")
                self.assertEqual([first.result()["versionId"], second.result()["versionId"]], ["V2", "V3"])
        for version, source in zip(("v2", "v3"), movies):
            self.assertEqual((self.out / "media" / f"{version}.mp4").read_bytes(), source.read_bytes())

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

        self.assertEqual(run_video_cli(build_viewer.main, ["--rebuild", str(self.out)]), 0)

        self.assertEqual((self.out / "notes.json").read_bytes(), before)
        manifest = json.loads((self.out / "review.json").read_text(encoding="utf-8"))
        private = self.out / manifest["video"]["versions"][0]["path"]
        self.assertEqual(private.read_bytes(), self.media)
        self.assertEqual((manifest["title"], manifest["name"]), ("synthetic", "synthetic"))

    def test_reusing_a_review_directory_cannot_change_its_video_material_type(self):
        report = self.root / "report.html"
        report.write_text('<section class="page" data-page="1" data-key="one"><h1>Page</h1></section>')
        document = self.root / "document-review"
        build_viewer.build(report, None, document, no_scan_skills=True)
        self.build()
        for source, destination in ((self.source, document), (report, self.out)):
            with self.subTest(destination=destination):
                files = [destination / name for name in ("review.json", "index.html")]
                before = [path.read_bytes() for path in files]
                with self.assertRaisesRegex(build_viewer.BuildError, "其他类型"):
                    build_viewer.build(source, None, destination, no_scan_skills=True, locale="zh-CN")
                self.assertEqual([path.read_bytes() for path in files], before)
        self.assertFalse((document / "media").exists())

    def test_cut_with_metadata_but_no_media_samples_cannot_be_registered(self):
        self.source.write_bytes(self.media.replace(b"mdat", b"free", 1))
        with self.assertRaisesRegex(build_viewer.BuildError, "MP4|mp4"):
            self.build()

    def test_chunk_offset_must_point_to_the_registered_media_samples(self):
        data = bytearray(self.media)
        struct.pack_into(">I", data, data.index(b"stco") + 12, 0)
        self.source.write_bytes(data)
        with self.assertRaisesRegex(build_viewer.BuildError, "MP4|mp4"):
            self.build()

    def test_rebuild_preserves_a_version_published_during_page_generation(self):
        self.build()
        v2 = self.root / "revision.mp4"
        v2.write_bytes(synthetic_mp4(frames=192))
        original = Path.read_text

        def publish_during_rebuild(path, *args, **kwargs):
            result = original(path, *args, **kwargs)
            if path == build_viewer.TEMPLATE:
                video_review.register_version(self.out, v2, batch=1, by="agent", receipt_id="concurrent")
            return result

        with patch.object(Path, "read_text", publish_during_rebuild):
            self.assertEqual(run_video_cli(build_viewer.main, ["--rebuild", str(self.out)]), 0)
        manifest = json.loads((self.out / "review.json").read_text())
        self.assertEqual(manifest["video"]["currentVersionId"], "V2")
        self.assertIn('"currentVersionId": "V2"', (self.out / "index.html").read_text())

    def test_public_failure_summary_survives_first_build_and_each_rebuild(self):
        result = self.build()
        reason = "renderer could not open /synthetic/private/overlay.png"
        self.assertNotIn("lastFailure", result["video"])
        self.assertNotIn(reason, (self.out / "index.html").read_text())
        original = Path.read_text

        def report_failure(path, *args, **kwargs):
            text = original(path, *args, **kwargs)
            if path == build_viewer.TEMPLATE:
                video_review.record_failure(self.out, batch=1, by="agent", receipt_id="synthetic", reason=reason)
            return text

        for concurrent in (True, False):
            with (
                self.subTest(concurrent=concurrent),
                patch.object(Path, "read_text", report_failure if concurrent else original),
            ):
                self.assertEqual(run_video_cli(build_viewer.main, ["--rebuild", str(self.out)]), 0)
                manifest = json.loads((self.out / "review.json").read_text())
                self.assertEqual(manifest["video"]["lastFailure"]["reason"], reason)
                self.assertEqual(manifest["video"]["failures"][0]["reason"], reason)
                public = video_review.public_manifest(manifest)
                self.assertEqual(public["lastFailure"]["reason"], "引擎渲染失败")
                page = (self.out / "index.html").read_text()
                self.assertNotIn(reason, page)
                self.assertIn('"reason": "引擎渲染失败"', page)

    def test_legacy_playback_failure_shows_a_safe_summary_in_each_public_version(self):
        self.build()
        manifest = json.loads((self.out / "review.json").read_text())
        reason = "decoder log at /synthetic/private/decoder.log"
        old = {"stage": "decode", "batch": 1, "versionId": "V1", "reason": reason}
        manifest["video"]["lastFailure"] = old
        manifest["video"]["versions"][0]["playbackFailure"] = old
        for lang, summary in (("en", "The browser could not decode the video"), ("zh-CN", "浏览器无法解码成片")):
            with self.subTest(lang=lang):
                manifest["locale"] = lang
                public = video_review.public_manifest(manifest)
                self.assertNotIn(reason, json.dumps(public))
                self.assertEqual(public["lastFailure"]["reason"], summary)
                self.assertEqual(public["versions"][0]["playbackFailure"]["reason"], summary)
        self.assertEqual(old["reason"], reason)

    def test_non_mp4_and_invalid_mp4_are_rejected_with_specific_reason(self):
        bogus = self.root / "broken.mp4"
        bogus.write_bytes(b"not an mp4")
        with self.assertRaisesRegex(SystemExit, "MP4|mp4"):
            build_viewer.build(bogus, None, self.root / "broken-review", no_scan_skills=True)

    def test_invalid_mp4_and_missing_rebuild_media_explain_the_input_in_the_selected_language(self):
        self.build()
        self.source.write_bytes(b"")
        (self.out / "media/v1.mp4").unlink()
        for lang, empty, missing in (
            ("en", "The MP4 file is empty", "The registered video is missing"),
            ("zh-CN", "MP4 文件为空", "视频版本的媒体不存在"),
        ):
            with self.subTest(lang=lang):
                with self.assertRaises(build_viewer.BuildError) as build_error:
                    build_viewer.build(self.source, None, self.root / "empty", no_scan_skills=True, locale=lang)
                self.assertIn(empty, str(build_error.exception))
                with self.assertRaises(build_viewer.BuildError) as rebuild_error:
                    build_viewer.resolve_sources(self.out, locale=lang)
                self.assertIn(missing, str(rebuild_error.exception))

    def test_mp4_beyond_the_supported_box_depth_has_an_actionable_build_error(self):
        for lang, part in (("en", "MP4 box nesting exceeds"), ("zh-CN", "MP4 媒体盒嵌套超出")):
            with self.subTest(lang=lang), patch.object(video_review, "MAX_BOX_DEPTH", 2, create=True):
                with self.assertRaises(build_viewer.BuildError) as error:
                    build_viewer.build(self.source, None, self.out, no_scan_skills=True, locale=lang)
                self.assertIn(part, str(error.exception))
                self.assertIn("2", str(error.exception))
                self.assertFalse((self.out / "media/v1.mp4").exists())
        self.assertEqual(self.build()["video"]["currentVersionId"], "V1")


if __name__ == "__main__":
    unittest.main()
