"""视频引擎入口（#194）：Remotion / HyperFrames 工程探测、复用判定与渲染边界。

用合成工程骨架（tests/fixtures/video/）加假引擎 bin 验证：
静态探测、固定 argv、路径不得越出工程、输出路径预检、超时与进程组管理、
渲染失败后的恢复重试。不在实现阶段调用本机 Node / Chrome / FFmpeg，
实机双引擎演示归 #198 / PR review。
"""

from __future__ import annotations

import json
import os
import shutil
import struct
import sys
import tempfile
import textwrap
import unittest
from contextlib import redirect_stdout
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "skill" / "annotation-everything" / "scripts"
FIXTURES = ROOT / "tests" / "fixtures" / "video"
sys.path.insert(0, str(SCRIPTS))
os.environ["AE_HOME"] = tempfile.mkdtemp(prefix="ae-video-engines-home-")

import build_viewer  # noqa: E402
import video_engines  # noqa: E402


# ── 合成 MP4（与 test_video_review 同口径：只有元数据与占位媒体字节） ─────────


def box(kind: bytes, payload: bytes) -> bytes:
    return struct.pack(">I4s", len(payload) + 8, kind) + payload


def full_box(kind: bytes, version: int, flags: int, payload: bytes) -> bytes:
    return box(kind, bytes((version,)) + flags.to_bytes(3, "big") + payload)


def synthetic_mp4(*, frames: int = 180, fps: int = 30, width: int = 640, height: int = 360) -> bytes:
    duration = frames
    mvhd = full_box(
        "mvhd".encode(), 0, 0, struct.pack(">IIIIII", 0, 0, fps, duration, 0x00010000, 0x01000000) + b"\0" * 80
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


# ── 假引擎 bin：只实现 AE 会用到的固定 argv 模板 ──────────────────────────────

FAKE_REMOTION_BIN = textwrap.dedent(
    """
    #!/usr/bin/env python3
    import json, os, shutil, sys, time

    def _log():
        path = os.environ.get("AE_FAKE_BIN_LOG")
        if path:
            with open(path, "a", encoding="utf-8") as fh:
                fh.write(json.dumps({"bin": "remotion", "pid": os.getpid(),
                                     "cwd": os.getcwd(), "argv": sys.argv[1:]}) + "\\n")

    def main():
        _log()
        mode = os.environ.get("AE_FAKE_ENGINE_MODE", "ok")
        if sys.argv[1:] and sys.argv[1] == "compositions":
            if mode == "fail":
                print("synthetic remotion: cannot load entry module", file=sys.stderr)
                return 1
            print("Composition ID  Width  Height  FPS  Duration")
            for spec in json.loads(os.environ.get("AE_FAKE_COMPOSITIONS", "[]")):
                print(f"{spec['id']}  {spec['width']}  {spec['height']}  {spec['fps']}  {spec['frames']}")
            return 0
        if sys.argv[1:] and sys.argv[1] == "render":
            if mode in ("fail", "render-fail"):
                print("synthetic remotion render failed: dependency boom", file=sys.stderr)
                return 1
            if mode == "sleep":
                time.sleep(float(os.environ.get("AE_FAKE_ENGINE_SLEEP", "5")))
                return 0
            shutil.copyfile(os.environ["AE_FAKE_RENDER_TEMPLATE"], sys.argv[4])
            return 0
        print("synthetic remotion: unsupported argv", file=sys.stderr)
        return 64

    if __name__ == "__main__":
        sys.exit(main())
    """
).lstrip("\n")

FAKE_HYPERFRAMES_BIN = textwrap.dedent(
    """
    #!/usr/bin/env python3
    import json, os, shutil, sys, time

    def _log():
        path = os.environ.get("AE_FAKE_BIN_LOG")
        if path:
            with open(path, "a", encoding="utf-8") as fh:
                fh.write(json.dumps({"bin": "hyperframes", "pid": os.getpid(),
                                     "cwd": os.getcwd(), "argv": sys.argv[1:]}) + "\\n")

    def main():
        _log()
        mode = os.environ.get("AE_FAKE_ENGINE_MODE", "ok")
        if sys.argv[1:] and sys.argv[1] == "render":
            if mode in ("fail", "render-fail"):
                print("synthetic hyperframes render failed: chrome not found", file=sys.stderr)
                return 1
            if mode == "sleep":
                time.sleep(float(os.environ.get("AE_FAKE_ENGINE_SLEEP", "5")))
                return 0
            out = sys.argv[sys.argv.index("-o") + 1]
            shutil.copyfile(os.environ["AE_FAKE_RENDER_TEMPLATE"], out)
            return 0
        print("synthetic hyperframes: unsupported argv", file=sys.stderr)
        return 64

    if __name__ == "__main__":
        sys.exit(main())
    """
).lstrip("\n")


class EngineEnvMixin(unittest.TestCase):
    """每个用例独立临时目录、独立假引擎环境；结束后恢复进程环境。"""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="ae-engines-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self._saved_env: dict[str, str] = {}

    def set_env(self, **values: str) -> None:
        for key, value in values.items():
            if key not in self._saved_env:
                self._saved_env[key] = os.environ.get(key, "")
            os.environ[key] = value

    def tearDown(self):
        for key, value in self._saved_env.items():
            os.environ[key] = value

    def copy_fixture(self, name: str) -> Path:
        project = self.root / name
        shutil.copytree(FIXTURES / name, project)
        return project

    def install_fake_bin(self, project: Path, engine: str) -> Path:
        script = FAKE_REMOTION_BIN if engine == "remotion" else FAKE_HYPERFRAMES_BIN
        bin_dir = project / "node_modules" / ".bin"
        bin_dir.mkdir(parents=True, exist_ok=True)
        target = bin_dir / engine
        target.write_text(script, encoding="utf-8")
        target.chmod(0o755)
        return target

    def make_movie(self, path: Path, *, frames: int = 180, fps: int = 30, width: int = 640, height: int = 360) -> bytes:
        path.parent.mkdir(parents=True, exist_ok=True)
        media = synthetic_mp4(frames=frames, fps=fps, width=width, height=height)
        path.write_bytes(media)
        return media

    def arm_render(self, project: Path, engine: str, media: bytes) -> Path:
        """装好假 bin 与渲染模板，返回 bin 路径。"""
        template = self.root / f"template-{engine}.mp4"
        template.write_bytes(media)
        self.set_env(AE_FAKE_RENDER_TEMPLATE=str(template))
        return self.install_fake_bin(project, engine)

    def read_bin_log(self) -> list[dict]:
        log = Path(os.environ["AE_FAKE_BIN_LOG"])
        if not log.exists():
            return []
        return [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines() if line.strip()]


MAIN_COMP = {"id": "Main", "width": 640, "height": 360, "fps": 30, "frames": 180}
INTRO_COMP = {"id": "Intro", "width": 320, "height": 180, "fps": 24, "frames": 48}
MAIN_MOVIE = synthetic_mp4()


class DetectionTests(EngineEnvMixin):
    """静态探测：只读工程文件，不执行任何工程代码。"""

    def test_remotion_project_detected_with_default_entry(self):
        project = self.copy_fixture("remotion")
        info = video_engines.detect_project(project)
        self.assertEqual(info["engine"], "remotion")
        self.assertEqual(info["entry"], (project / "src" / "index.tsx").resolve())

    def test_remotion_config_entry_override_wins(self):
        project = self.copy_fixture("remotion")
        other = project / "src" / "other.tsx"
        other.write_text("// entry override\n", encoding="utf-8")
        (project / "remotion.config.ts").write_text(
            'import { Config } from "@remotion/cli/config";\nConfig.setEntryPoint("src/other.tsx");\n',
            encoding="utf-8",
        )
        info = video_engines.detect_project(project)
        self.assertEqual(info["engine"], "remotion")
        self.assertEqual(info["entry"], other.resolve())

    def test_remotion_entry_outside_project_is_rejected(self):
        project = self.copy_fixture("remotion")
        (project / "remotion.config.ts").write_text(
            'import { Config } from "@remotion/cli/config";\nConfig.setEntryPoint("../outside.tsx");\n',
            encoding="utf-8",
        )
        with self.assertRaises(video_engines.EngineError) as ctx:
            video_engines.detect_project(project)
        self.assertEqual(ctx.exception.code, "entryOutside")

    def test_remotion_project_without_entry_reports_candidates(self):
        project = self.copy_fixture("remotion")
        shutil.rmtree(project / "src")
        with self.assertRaises(video_engines.EngineError) as ctx:
            video_engines.detect_project(project)
        self.assertEqual(ctx.exception.code, "noEntry")
        self.assertIn("src/index.tsx", ctx.exception.detail)

    def test_hyperframes_project_detected_with_fps_evidence(self):
        project = self.copy_fixture("hyperframes")
        info = video_engines.detect_project(project)
        self.assertEqual(info["engine"], "hyperframes")
        ids = [c["id"] for c in info["compositions"]]
        self.assertEqual(ids, ["compositions/scene-b.html", "index.html"])
        by_id = {c["id"]: c for c in info["compositions"]}
        self.assertEqual(by_id["index.html"]["fps"], 24.0)
        self.assertTrue(by_id["index.html"]["fpsDeclared"])
        self.assertEqual(by_id["compositions/scene-b.html"]["fps"], 30.0)
        self.assertFalse(by_id["compositions/scene-b.html"]["fpsDeclared"])

    def test_plain_directory_is_not_an_engine_project(self):
        plain = self.root / "plain"
        plain.mkdir()
        (plain / "readme.txt").write_text("nothing here\n", encoding="utf-8")
        with self.assertRaises(video_engines.EngineError) as ctx:
            video_engines.detect_project(plain)
        self.assertEqual(ctx.exception.code, "notProject")

    def test_hyperframes_composition_element_declares_fps_and_document_takes_precedence(self):
        project = self.copy_fixture("hyperframes")
        scene = '<body data-fps="90"><div data-composition-id="root" data-fps="25" data-duration="1"></div></body>'
        for attrs, expected in (("", 25), ('data-fps="24"', 24)):
            with self.subTest(attrs=attrs):
                (project / "index.html").write_text(f"<html {attrs}>{scene}</html>", encoding="utf-8")
                comp = next(c for c in video_engines.detect_project(project)["compositions"] if c["id"] == "index.html")
                self.assertEqual((comp["fps"], comp["fpsDeclared"]), (expected, True))

    def test_project_with_both_engine_indicators_is_ambiguous(self):
        project = self.copy_fixture("remotion")
        pkg = json.loads((project / "package.json").read_text(encoding="utf-8"))
        pkg["dependencies"]["hyperframes"] = "0.9.77"
        (project / "package.json").write_text(json.dumps(pkg), encoding="utf-8")
        with self.assertRaises(video_engines.EngineError) as ctx:
            video_engines.detect_project(project)
        self.assertEqual(ctx.exception.code, "ambiguousEngine")

    def test_missing_project_directory_is_rejected(self):
        with self.assertRaises(video_engines.EngineError) as ctx:
            video_engines.detect_project(self.root / "nope")
        self.assertEqual(ctx.exception.code, "notProject")


class ArgvAndParseTests(unittest.TestCase):
    def test_render_argv_remotion_is_fixed_template(self):
        argv = video_engines.render_argv(
            "remotion", Path("/proj/node_modules/.bin/remotion"), Path("/proj/src/index.tsx"), "Main", Path("/o.mp4")
        )
        self.assertEqual(
            argv,
            [
                "/proj/node_modules/.bin/remotion",
                "render",
                "/proj/src/index.tsx",
                "Main",
                "/o.mp4",
                "--codec=h264",
                "--overwrite=false",
            ],
        )

    def test_render_argv_hyperframes_is_fixed_template(self):
        argv = video_engines.render_argv(
            "hyperframes",
            Path("/proj/node_modules/.bin/hyperframes"),
            Path("/proj"),
            "index.html",
            Path("/o.mp4"),
        )
        self.assertEqual(
            argv,
            [
                "/proj/node_modules/.bin/hyperframes",
                "render",
                ".",
                "--format",
                "mp4",
                "-o",
                "/o.mp4",
                "-c",
                "/proj/index.html",
            ],
        )

    def test_compositions_argv(self):
        argv = video_engines.compositions_argv(Path("/b"), Path("/e.tsx"))
        self.assertEqual(argv, ["/b", "compositions", "/e.tsx"])

    def test_parse_compositions_table_maps_header_columns(self):
        table = (
            "Composition ID  Width  Height  FPS  Duration\n"
            "Main  640  360  30  180\n"
            "Intro  320  180  23.976  48\n"
            "garbage row without numbers\n"
        )
        comps, unparsed = video_engines.parse_compositions(table)
        self.assertEqual(unparsed, 1)
        self.assertEqual(comps[0], {"id": "Main", "width": 640, "height": 360, "fps": 30.0, "frames": 180})
        self.assertEqual(comps[1]["fps"], 23.976)
        self.assertEqual(comps[1]["frames"], 48)

    def test_parse_compositions_accepts_seconds_duration(self):
        table = "Composition ID  Width  Height  FPS  Duration\nMain  640  360  30  6.0\n"
        comps, _unparsed = video_engines.parse_compositions(table)
        self.assertEqual(comps[0]["frames"], 180)

    def test_parse_remotion_4_listing_after_bundling(self):
        listing = (
            "\x1b[2KBundling code 90%\rBundling code 100%\n"
            "The following compositions are available:\n\n"
            "\x1b[32mDemo\x1b[0m    30      640x360        30 (1.00 sec)\n"
            "Alternate    23.976      1280x720        48 (2.00 sec)\n"
        )
        self.assertEqual(
            video_engines.parse_compositions(listing),
            (
                [
                    {"id": "Demo", "fps": 30.0, "width": 640, "height": 360, "frames": 30},
                    {"id": "Alternate", "fps": 23.976, "width": 1280, "height": 720, "frames": 48},
                ],
                0,
            ),
        )


class ReuseTests(EngineEnvMixin):
    """匹配复用：只有可证明匹配的成片才能登记 V1，不能只凭文件名。"""

    def setUp(self):
        super().setUp()
        self.project = self.copy_fixture("remotion")
        self.review = self.root / "review"
        self.set_env(AE_FAKE_COMPOSITIONS=json.dumps([MAIN_COMP]))
        self.log = self.root / "binlog.jsonl"
        self.set_env(AE_FAKE_BIN_LOG=str(self.log))

    def resolve(self, **kwargs):
        return video_engines.resolve_source(self.project, self.review, {}, locale="zh-CN", **kwargs)

    def test_single_fresh_matching_movie_is_reused(self):
        media = self.make_movie(self.project / "out" / "Main.mp4")
        self.arm_render(self.project, "remotion", media)  # 复用路径不应触发渲染
        result = self.resolve()
        self.assertEqual(result["action"], "reused")
        self.assertEqual(result["source"], (self.project / "out" / "Main.mp4").resolve())
        self.assertEqual(result["cleanup"], None)
        self.assertTrue(any("复用" in w for w in result["warnings"]))
        commands = [entry["argv"][0] if entry["argv"] else "" for entry in self.read_bin_log()]
        self.assertEqual(commands, ["compositions"])

    def test_reuse_needs_listing_so_missing_deps_reports_recovery(self):
        self.make_movie(self.project / "out" / "Main.mp4")
        # 没有 node_modules/.bin/remotion：列出合成的第一步就给出安装依赖的恢复信息
        with self.assertRaises(video_engines.EngineError) as ctx:
            self.resolve()
        self.assertEqual(ctx.exception.code, "depsMissing")

    def test_stale_movie_requires_explicit_confirmation(self):
        media = self.make_movie(self.project / "out" / "Main.mp4")
        self.arm_render(self.project, "remotion", media)
        now = (self.project / "src" / "index.tsx").stat().st_mtime
        os.utime(self.project / "out" / "Main.mp4", (now - 100, now - 100))
        with self.assertRaises(video_engines.EngineError) as ctx:
            self.resolve()
        self.assertEqual(ctx.exception.code, "movieStale")
        # 显式确认陈旧成片后可以复用，并记录陈旧警告
        result = self.resolve(reuse_movie=self.project / "out" / "Main.mp4")
        self.assertEqual(result["action"], "reused")
        self.assertTrue(any("旧" in w for w in result["warnings"]))

    def test_no_reuse_skips_matching_and_renders(self):
        media = self.make_movie(self.project / "out" / "Main.mp4")
        self.arm_render(self.project, "remotion", media)
        result = self.resolve(no_reuse=True)
        self.assertEqual(result["action"], "rendered")

    def test_multiple_fresh_matches_are_ambiguous(self):
        media = self.make_movie(self.project / "out" / "Main.mp4")
        self.make_movie(self.project / "take2.mp4")
        self.arm_render(self.project, "remotion", media)
        with self.assertRaises(video_engines.EngineError) as ctx:
            self.resolve()
        self.assertEqual(ctx.exception.code, "movieAmbiguous")

    def test_mismatched_movie_metadata_is_rejected_for_reuse(self):
        media = self.make_movie(self.project / "out" / "Main.mp4", frames=120)
        self.arm_render(self.project, "remotion", media)
        with self.assertRaises(video_engines.EngineError) as ctx:
            self.resolve(reuse_movie=self.project / "out" / "Main.mp4")
        self.assertEqual(ctx.exception.code, "movieMismatch")

    def test_missing_reuse_movie_is_rejected(self):
        self.arm_render(self.project, "remotion", MAIN_MOVIE)
        with self.assertRaises(video_engines.EngineError) as ctx:
            self.resolve(reuse_movie=self.project / "out" / "nothing.mp4")
        self.assertEqual(ctx.exception.code, "movieUnreadable")

    def test_hyperframes_movie_needs_explicit_confirmation(self):
        project = self.copy_fixture("hyperframes")
        self.arm_render(project, "hyperframes", synthetic_mp4(fps=24))
        self.make_movie(project / "output.mp4", fps=24)
        with self.assertRaises(video_engines.EngineError) as ctx:
            video_engines.resolve_source(project, self.root / "review2", {}, composition="index.html", locale="zh-CN")
        self.assertEqual(ctx.exception.code, "movieConfirm")
        result = video_engines.resolve_source(
            project,
            self.root / "review2",
            {},
            composition="index.html",
            reuse_movie=project / "output.mp4",
            locale="zh-CN",
        )
        self.assertEqual(result["action"], "reused")

    def test_already_registered_review_refuses_project_entry(self):
        existing = {"format": {"report": "video", "version": 1}}
        with self.assertRaises(video_engines.EngineError) as ctx:
            video_engines.resolve_source(self.project, self.review, existing, locale="zh-CN")
        self.assertEqual(ctx.exception.code, "alreadyRegistered")


class RenderTests(EngineEnvMixin):
    """缺成片时由工程引擎渲染到新路径；失败给恢复信息，重试成功继续同一审阅。"""

    def setUp(self):
        super().setUp()
        self.project = self.copy_fixture("remotion")
        self.review = self.root / "review"
        self.set_env(AE_FAKE_COMPOSITIONS=json.dumps([MAIN_COMP]))
        self.log = self.root / "binlog.jsonl"
        self.set_env(AE_FAKE_BIN_LOG=str(self.log))

    def resolve(self, **kwargs):
        return video_engines.resolve_source(
            self.project, self.review, {}, locale="zh-CN", render_timeout=kwargs.pop("render_timeout", 30), **kwargs
        )

    def test_render_uses_fixed_argv_and_registers_staging_output(self):
        self.arm_render(self.project, "remotion", MAIN_MOVIE)
        result = self.resolve()
        self.assertEqual(result["action"], "rendered")
        self.assertEqual(result["composition"]["id"], "Main")
        self.assertEqual(result["source"], result["cleanup"])
        self.assertTrue(result["source"].is_file())
        entries = self.read_bin_log()
        self.assertEqual(entries[0]["argv"][0], "compositions")
        render_argv = entries[1]["argv"]
        self.assertEqual(render_argv[0], "render")
        self.assertEqual(render_argv[1], str((self.project / "src" / "index.tsx").resolve()))
        self.assertEqual(render_argv[2], "Main")
        self.assertEqual(render_argv[4:], ["--codec=h264", "--overwrite=false"])
        self.assertTrue(render_argv[3].startswith(str(self.review / ".render")))
        self.assertEqual(entries[1]["cwd"], str(self.project.resolve()))

    def test_render_failure_reports_recovery_and_retry_continues_same_review(self):
        self.arm_render(self.project, "remotion", MAIN_MOVIE)
        self.set_env(AE_FAKE_ENGINE_MODE="render-fail")
        with self.assertRaises(video_engines.EngineError) as ctx:
            self.resolve()
        self.assertEqual(ctx.exception.code, "renderFailed")
        self.assertIn("dependency boom", ctx.exception.detail)
        staging_dir = self.review / ".render"
        if staging_dir.exists():
            self.assertEqual(list(staging_dir.iterdir()), [])
        self.set_env(AE_FAKE_ENGINE_MODE="ok")
        result = self.resolve()
        self.assertEqual(result["action"], "rendered")

    def test_render_timeout_kills_process_group_and_cleans_staging(self):
        self.arm_render(self.project, "remotion", MAIN_MOVIE)
        self.set_env(AE_FAKE_ENGINE_MODE="sleep", AE_FAKE_ENGINE_SLEEP="5")
        with self.assertRaises(video_engines.EngineError) as ctx:
            self.resolve(render_timeout=0.5)
        self.assertEqual(ctx.exception.code, "renderTimeout")
        self.assertFalse(list((self.review / ".render").glob("*.mp4")))
        entries = self.read_bin_log()
        pid = entries[-1]["pid"]
        for _ in range(40):
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                break
            import time

            time.sleep(0.05)
        else:
            self.fail("超时后引擎进程仍在运行")

    def test_compositions_failure_reports_engine_stderr(self):
        self.arm_render(self.project, "remotion", MAIN_MOVIE)
        self.set_env(AE_FAKE_ENGINE_MODE="fail")
        with self.assertRaises(video_engines.EngineError) as ctx:
            self.resolve()
        self.assertEqual(ctx.exception.code, "compositionsFailed")
        self.assertIn("cannot load entry module", ctx.exception.detail)

    def test_composition_choice_is_validated(self):
        self.arm_render(self.project, "remotion", MAIN_MOVIE)
        with self.assertRaises(video_engines.EngineError) as ctx:
            self.resolve(composition="NoSuch")
        self.assertEqual(ctx.exception.code, "compositionMissing")

    def test_ambiguous_composition_requires_explicit_choice(self):
        self.set_env(AE_FAKE_COMPOSITIONS=json.dumps([MAIN_COMP, INTRO_COMP]))
        self.arm_render(self.project, "remotion", MAIN_MOVIE)
        with self.assertRaises(video_engines.EngineError) as ctx:
            self.resolve()
        self.assertEqual(ctx.exception.code, "compositionAmbiguous")
        self.assertIn("Main", ctx.exception.detail)
        result = self.resolve(composition="Intro")
        self.assertEqual(result["composition"]["id"], "Intro")

    def test_render_output_that_cannot_be_probed_is_rejected(self):
        template = self.root / "bad.mp4"
        template.write_bytes(b"not an mp4")
        self.set_env(AE_FAKE_RENDER_TEMPLATE=str(template))
        self.install_fake_bin(self.project, "remotion")
        with self.assertRaises(video_engines.EngineError) as ctx:
            self.resolve()
        self.assertEqual(ctx.exception.code, "renderInvalid")

    def test_hyperframes_renders_chosen_html_entry(self):
        project = self.copy_fixture("hyperframes")
        self.set_env(AE_FAKE_BIN_LOG=str(self.log))
        movie24 = synthetic_mp4(fps=24)
        self.arm_render(project, "hyperframes", movie24)
        with self.assertRaises(video_engines.EngineError) as ctx:
            video_engines.resolve_source(project, self.root / "review2", {}, locale="zh-CN", render_timeout=30)
        self.assertEqual(ctx.exception.code, "compositionAmbiguous")
        result = video_engines.resolve_source(
            project, self.root / "review2", {}, composition="index.html", locale="zh-CN", render_timeout=30
        )
        self.assertEqual(result["composition"]["id"], "index.html")
        entries = self.read_bin_log()
        render_argv = entries[-1]["argv"]
        self.assertEqual(render_argv[0], "render")
        self.assertEqual(render_argv[1:4], [".", "--format", "mp4"])
        self.assertEqual(render_argv[7], str((project / "index.html").resolve()))
        self.assertEqual(entries[-1]["cwd"], str(project.resolve()))

    def test_hyperframes_composition_must_stay_inside_project(self):
        project = self.copy_fixture("hyperframes")
        self.arm_render(project, "hyperframes", MAIN_MOVIE)
        with self.assertRaises(video_engines.EngineError) as ctx:
            video_engines.resolve_source(
                project, self.root / "review3", {}, composition="../../outside.html", locale="zh-CN"
            )
        self.assertEqual(ctx.exception.code, "compositionOutside")


class InspectCliTests(EngineEnvMixin):
    def test_inspect_prints_candidate_evidence_as_json(self):
        project = self.copy_fixture("remotion")
        media = self.make_movie(project / "out" / "Main.mp4")
        self.arm_render(project, "remotion", media)
        self.set_env(AE_FAKE_COMPOSITIONS=json.dumps([MAIN_COMP, INTRO_COMP]))
        import io

        buffer = io.StringIO()
        with redirect_stdout(buffer):
            code = video_engines.main(["inspect", str(project)])
        self.assertEqual(code, 0)
        report = json.loads(buffer.getvalue())
        self.assertEqual(report["engine"], "Remotion")
        self.assertEqual([c["id"] for c in report["compositions"]], ["Main", "Intro"])
        movie = report["movies"][0]
        self.assertEqual(movie["matches"], ["Main"])
        self.assertTrue(movie["fresh"])
        self.assertTrue(report["render"]["available"])

    def test_inspect_error_outputs_json_and_nonzero(self):
        plain = self.root / "plain"
        plain.mkdir()
        import io

        buffer = io.StringIO()
        with redirect_stdout(buffer):
            code = video_engines.main(["inspect", str(plain)])
        self.assertEqual(code, 2)
        self.assertEqual(json.loads(buffer.getvalue())["error"], "notProject")


class BuildIntegrationTests(EngineEnvMixin):
    """从工程目录开始的完整构建：与 MP4 入口同一审阅形态。"""

    def build(self, report, out, **kwargs):
        return build_viewer.build(report, None, out, no_scan_skills=True, locale="zh-CN", **kwargs)

    def test_project_build_reuses_matching_movie_and_registers_v1(self):
        project = self.copy_fixture("remotion")
        media = self.make_movie(project / "out" / "Main.mp4")
        self.arm_render(project, "remotion", media)
        self.set_env(AE_FAKE_COMPOSITIONS=json.dumps([MAIN_COMP]))
        out = self.root / "review"
        result = self.build(project, out)
        self.assertTrue(any("复用" in w for w in result["warnings"]))
        manifest = json.loads((out / "review.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["format"], {"report": "video", "version": 1})
        version = manifest["video"]["versions"][0]
        self.assertEqual(version["fps"], 30.0)
        self.assertEqual((out / "media" / "v1.mp4").read_bytes(), media)
        self.assertIn('id="videoPlayer"', (out / "index.html").read_text(encoding="utf-8"))

    def test_project_build_renders_v1_when_no_movie_matches(self):
        project = self.copy_fixture("hyperframes")
        self.set_env(AE_FAKE_BIN_LOG=str(self.root / "log.jsonl"))
        movie24 = synthetic_mp4(fps=24)
        self.arm_render(project, "hyperframes", movie24)
        out = self.root / "review"
        result = self.build(project, out, video_composition="index.html")
        self.assertTrue(any("渲染" in w for w in result["warnings"]))
        manifest = json.loads((out / "review.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["video"]["versions"][0]["fps"], 24.0)
        self.assertEqual((out / "media" / "v1.mp4").read_bytes(), movie24)
        # 渲染暂存已清理：不留在审阅目录里
        leftovers = list(out.glob(".render/*"))
        self.assertEqual(leftovers, [])

    def test_project_build_failure_is_recoverable_and_retry_succeeds(self):
        project = self.copy_fixture("remotion")
        self.set_env(AE_FAKE_COMPOSITIONS=json.dumps([MAIN_COMP]))
        self.arm_render(project, "remotion", MAIN_MOVIE)
        self.set_env(AE_FAKE_ENGINE_MODE="render-fail")
        out = self.root / "review"
        with self.assertRaises(build_viewer.BuildError):
            self.build(project, out)
        self.set_env(AE_FAKE_ENGINE_MODE="ok")
        result = self.build(project, out)
        self.assertTrue((out / "review.json").exists())
        self.assertEqual((out / "media" / "v1.mp4").read_bytes(), MAIN_MOVIE)
        self.assertEqual(list(out.glob(".render/*")), [])
        self.assertEqual(result["pages"], 1)

    def test_project_build_rejects_script_input(self):
        project = self.copy_fixture("remotion")
        self.arm_render(project, "remotion", MAIN_MOVIE)
        self.set_env(AE_FAKE_COMPOSITIONS=json.dumps([MAIN_COMP]))
        script = self.root / "notes.md"
        script.write_text("### 第 1 页｜x\n", encoding="utf-8")
        with self.assertRaises(build_viewer.BuildError):
            build_viewer.build(project, script, self.root / "review", no_scan_skills=True, locale="zh-CN")


class SourceProjectLinkTests(EngineEnvMixin):
    """直接从工程开始时接入 #193 的源工程关联契约（manifest 的 video.sourceProject）。

    声明与登记 V1 一致 → linked/verified；渲染结果与声明有差异 → pending，
    审阅人用 video_source.confirm 确认；MP4 先批后绑不走这里的自动写入。
    """

    def build_from_project(self, project, out, **kwargs):
        return build_viewer.build(project, None, out, no_scan_skills=True, locale="zh-CN", **kwargs)

    def test_reused_v1_links_verified_source_project(self):
        project = self.copy_fixture("remotion")
        media = self.make_movie(project / "out" / "Main.mp4")
        self.arm_render(project, "remotion", media)
        self.set_env(AE_FAKE_COMPOSITIONS=json.dumps([MAIN_COMP]))
        out = self.root / "review"
        self.build_from_project(project, out)
        manifest = json.loads((out / "review.json").read_text(encoding="utf-8"))
        sp = manifest["video"]["sourceProject"]
        self.assertEqual(sp["status"], "linked")
        self.assertEqual(sp["match"], "verified")
        self.assertEqual(sp["engine"], "remotion")
        self.assertEqual(sp["root"], str(project.resolve()))
        self.assertEqual(sp["entry"], {"kind": "composition", "id": "Main"})
        self.assertEqual(sp["versionId"], "V1")
        self.assertEqual(sp["basis"]["kind"], "media-features")
        self.assertEqual(sp["basis"]["media"], {"width": 640, "height": 360, "fps": 30.0, "frames": 180})
        self.assertEqual(sp["differences"], [])
        self.assertEqual(sp["candidates"], [])
        self.assertIn("linkedAt", sp)

    def test_hyperframes_rendered_v1_links_verified_scene_entry(self):
        project = self.copy_fixture("hyperframes")
        self.arm_render(project, "hyperframes", synthetic_mp4(fps=24))
        out = self.root / "review"
        self.build_from_project(project, out, video_composition="index.html")
        manifest = json.loads((out / "review.json").read_text(encoding="utf-8"))
        sp = manifest["video"]["sourceProject"]
        self.assertEqual(sp["status"], "linked")
        self.assertEqual(sp["match"], "verified")
        self.assertEqual(sp["engine"], "hyperframes")
        self.assertEqual(sp["entry"], {"kind": "scene", "id": "index.html"})
        self.assertEqual(sp["basis"]["media"], {"fps": 24.0})

    def test_declaration_mismatch_links_pending_and_confirm_restores(self):
        import video_source

        project = self.copy_fixture("remotion")
        # 引擎实际渲染出 320x180、120 帧：与合成声明 640x360、180 帧不符
        self.arm_render(project, "remotion", synthetic_mp4(width=320, height=180, frames=120))
        self.set_env(AE_FAKE_COMPOSITIONS=json.dumps([MAIN_COMP]))
        out = self.root / "review"
        result = self.build_from_project(project, out)
        self.assertTrue(any("不一致" in w for w in result["warnings"]))
        manifest = json.loads((out / "review.json").read_text(encoding="utf-8"))
        sp = manifest["video"]["sourceProject"]
        self.assertEqual(sp["status"], "pending")
        self.assertIsNone(sp["match"])
        fields = {d["field"] for d in sp["differences"]}
        self.assertEqual(fields, {"width", "height", "frames"})
        self.assertIn("proposedAt", sp)
        self.assertFalse(video_source.linked(manifest["video"]))
        # 审阅人确认后变为 linked/confirmed，差异证据保留
        confirmed = video_source.confirm(manifest["video"])
        self.assertEqual(confirmed["sourceProject"]["status"], "linked")
        self.assertEqual(confirmed["sourceProject"]["match"], "confirmed")
        self.assertEqual(confirmed["sourceProject"]["differences"], sp["differences"])

    def test_rebuild_preserves_linked_source_project(self):
        project = self.copy_fixture("remotion")
        media = self.make_movie(project / "out" / "Main.mp4")
        self.arm_render(project, "remotion", media)
        self.set_env(AE_FAKE_COMPOSITIONS=json.dumps([MAIN_COMP]))
        out = self.root / "review"
        self.build_from_project(project, out)
        self.assertEqual(build_viewer.main(["--rebuild", str(out)]), 0)
        manifest = json.loads((out / "review.json").read_text(encoding="utf-8"))
        sp = manifest["video"]["sourceProject"]
        self.assertEqual(sp["status"], "linked")
        self.assertEqual(sp["match"], "verified")
        self.assertEqual(sp["entry"], {"kind": "composition", "id": "Main"})

    def test_mp4_entry_does_not_write_source_project(self):
        source = self.root / "cut.mp4"
        source.write_bytes(MAIN_MOVIE)
        out = self.root / "mp4-review"
        build_viewer.build(source, None, out, no_scan_skills=True, locale="zh-CN")
        manifest = json.loads((out / "review.json").read_text(encoding="utf-8"))
        self.assertNotIn("sourceProject", manifest["video"])


if __name__ == "__main__":
    unittest.main()
