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
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "skill" / "annotation-everything" / "scripts"
FIXTURES = ROOT / "tests" / "fixtures" / "video"
sys.path.insert(0, str(SCRIPTS))
os.environ["AE_HOME"] = tempfile.mkdtemp(prefix="ae-video-engines-home-")

import build_viewer  # noqa: E402
import video_engines  # noqa: E402


from tests.test_video_review import box, synthetic_mp4  # noqa: E402


# ── 假引擎 bin：只实现 AE 会用到的固定 argv 模板 ──────────────────────────────

FAKE_REMOTION_BIN = textwrap.dedent(
    """
    #!/usr/bin/env python3
    import json, os, shutil, subprocess, sys, time

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
            if mode in ("fail-with-helper", "sleep-with-helper"):
                child = subprocess.Popen([sys.executable, os.environ["AE_FAKE_HELPER"]],
                                         stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
                if child.stdout.readline() != b"ready\\n":
                    return 2
                if mode == "sleep-with-helper":
                    time.sleep(5)
                return 1
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

    def test_single_hyperframes_entry_must_stay_in_project_at_scan_and_selection(self):
        project = self.copy_fixture("hyperframes")
        scene = project / "index.html"
        (project / "compositions" / "scene-b.html").unlink()
        candidates = video_engines.scan_html_compositions(project)
        outside = self.root / "other-scene.html"
        scene.rename(outside)
        scene.symlink_to(outside)
        with self.assertRaises(video_engines.EngineError) as scan:
            video_engines.detect_project(project)
        self.assertEqual(scan.exception.code, "compositionOutside")
        with self.assertRaises(video_engines.EngineError) as choice:
            video_engines.choose_composition(candidates, None, "hyperframes", project)
        self.assertEqual(choice.exception.code, "compositionOutside")

    def test_windows_npm_launches_installed_entry_through_node(self):
        for engine, package in (("remotion", "@remotion/cli"), ("hyperframes", "hyperframes")):
            with self.subTest(engine=engine):
                project = self.copy_fixture(engine)
                modules = project / "node_modules"
                wrapper = modules / ".bin" / (engine + ".cmd")
                wrapper.parent.mkdir(parents=True)
                wrapper.touch()
                installed = modules / package
                installed.mkdir(parents=True)
                entry = installed / "run cli.js"
                entry.touch()
                (installed / "package.json").write_text(json.dumps({"bin": {engine: entry.name}}))
                with (
                    mock.patch.object(sys, "platform", "win32"),
                    mock.patch.object(shutil, "which", return_value="C:/Program Files/nodejs/node.exe"),
                ):
                    launcher = video_engines.detect_project(project)["bin"]
                    self.assertEqual(launcher, wrapper.resolve())
                    expected = ["C:/Program Files/nodejs/node.exe", str(entry.resolve())]
                    args = video_engines.render_argv(engine, launcher, project, "Scene & A", self.root / "out.mp4")
                    self.assertEqual(args[:3], [*expected, "render"])
                    if engine == "remotion":
                        self.assertEqual(video_engines.compositions_argv(launcher, entry)[:2], expected)

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
        self.arm_render(self.project, "remotion", media)
        with self.assertRaises(video_engines.EngineError) as uncertain:
            self.resolve()
        self.assertEqual(uncertain.exception.code, "movieConfirm")
        self.resolve(no_reuse=True)
        self.log.unlink()
        result = self.resolve()
        self.assertEqual(result["action"], "reused")
        self.assertEqual(result["source"], (self.project / "out" / "Main.mp4").resolve())
        self.assertEqual(result["cleanup"], None)
        self.assertTrue(any("复用" in w for w in result["warnings"]))
        commands = [entry["argv"][0] if entry["argv"] else "" for entry in self.read_bin_log()]
        self.assertEqual(commands, ["compositions"])

    def test_same_shape_compositions_require_explicit_movie_choice(self):
        self.make_movie(self.project / "out" / "Main.mp4")
        self.arm_render(self.project, "remotion", MAIN_MOVIE)
        self.set_env(AE_FAKE_COMPOSITIONS=json.dumps([MAIN_COMP, {**MAIN_COMP, "id": "Alternate"}]))
        with self.assertRaises(video_engines.EngineError) as reason:
            self.resolve(composition="Alternate")
        self.assertEqual(reason.exception.code, "movieConfirm")
        self.assertIn("Main", reason.exception.detail)
        self.assertIn("Alternate", reason.exception.detail)

    def test_changing_a_source_asset_invalidates_record_even_with_the_same_timestamp(self):
        asset = self.project / "src" / "asset.txt"
        asset.write_text("first")
        original = asset.stat()
        self.arm_render(self.project, "remotion", MAIN_MOVIE)
        self.resolve(no_reuse=True)
        self.make_movie(self.project / "out" / "Main.mp4")
        self.assertEqual(self.resolve()["action"], "reused")
        asset.write_text("other")
        os.utime(asset, ns=(original.st_atime_ns, original.st_mtime_ns))
        with self.assertRaises(video_engines.EngineError) as uncertain:
            self.resolve()
        self.assertEqual(uncertain.exception.code, "movieConfirm")

    def test_hidden_assets_and_config_invalidate_automatic_reuse(self):
        project = self.copy_fixture("hyperframes")
        (project / "index.html").write_text(
            '<html data-fps="30"><link rel="stylesheet" href=".theme.css"><img src=".assets/card.svg"></html>'
        )
        assets = project / ".assets"
        assets.mkdir()
        image, style = assets / "card.svg", project / ".theme.css"
        image.write_text('<svg fill="red"/>')
        style.write_text("body {color: red}")
        self.arm_render(project, "hyperframes", MAIN_MOVIE)

        def resolve(**options):
            return video_engines.resolve_source(project, self.review, {}, composition="index.html", **options)

        render = resolve(no_reuse=True)
        movie = project / "out" / "cut.mp4"
        movie.parent.mkdir()
        shutil.copyfile(render["source"], movie)
        self.assertEqual(resolve()["action"], "reused")
        for asset in (image, style):
            with self.subTest(asset=asset.name):
                original = asset.read_text()
                stamp = asset.stat().st_mtime_ns
                asset.write_text(original.replace("red", "blue"))
                os.utime(asset, ns=(stamp, stamp))
                with self.assertRaises(video_engines.EngineError) as changed:
                    resolve()
                self.assertEqual(changed.exception.code, "movieConfirm")
                asset.write_text(original)
                os.utime(asset, ns=(stamp, stamp))

    def test_hyperframes_render_history_is_not_a_source_change(self):
        project = self.copy_fixture("hyperframes")
        info = video_engines.detect_project(project)
        before = video_engines.source_fingerprint(info)
        state_dir = project / ".hyperframes"
        state_dir.mkdir()
        for name in ("history-id", "video-state.json", "render-outputs.json"):
            (state_dir / name).write_text("synthetic render history")
        (state_dir / "video-state-lock").mkdir()
        self.assertEqual(video_engines.source_fingerprint(info), before)
        (state_dir / "caption.svg").write_text("<svg/>")
        self.assertNotEqual(video_engines.source_fingerprint(info), before)

    def test_retargeting_an_internal_asset_link_invalidates_automatic_reuse(self):
        assets = self.project / "public"
        assets.mkdir()
        red, blue = assets / "red", assets / "blue"
        for directory in (red, blue):
            directory.mkdir()
            (directory / "title.svg").write_text(f'<svg fill="{directory.name}"/>')
        self.arm_render(self.project, "remotion", MAIN_MOVIE)
        movie = self.project / "out" / "Main.mp4"
        for link, first, second in (
            (assets / "active.svg", red / "title.svg", blue / "title.svg"),
            (assets / "theme", red, blue),
        ):
            with self.subTest(link=link.name):
                link.symlink_to(first, target_is_directory=first.is_dir())
                self.resolve(no_reuse=True)
                self.make_movie(movie)
                self.assertEqual(self.resolve()["action"], "reused")
                link.unlink()
                link.symlink_to(second, target_is_directory=second.is_dir())
                with self.assertRaises(video_engines.EngineError) as changed:
                    self.resolve()
                self.assertEqual(changed.exception.code, "movieConfirm")
                link.unlink()

    def test_external_assets_cannot_receive_or_reuse_a_render_receipt(self):
        external = self.root / "outside.svg"
        external.write_text('<svg fill="red"/>')
        (self.project / "icon.svg").symlink_to(external)
        self.arm_render(self.project, "remotion", MAIN_MOVIE)
        render = self.resolve(no_reuse=True)
        self.assertNotIn("sourceFingerprint", render)
        self.assertTrue(render["sourceChanged"])
        self.make_movie(self.project / "out" / "Main.mp4")
        with self.assertRaises(video_engines.EngineError) as uncertain:
            self.resolve()
        self.assertEqual(uncertain.exception.code, "movieConfirm")

    def test_root_mp4_input_change_invalidates_render_with_preserved_mtime(self):
        project = self.copy_fixture("hyperframes")
        entry = project / "index.html"
        entry.write_text('<html data-fps="24"><video src="clip.mp4"></video></html>')
        asset = project / "clip.mp4"
        self.make_movie(asset, fps=30)
        original = asset.stat()
        self.arm_render(project, "hyperframes", synthetic_mp4(fps=24))

        def resolve(**kwargs):
            return video_engines.resolve_source(project, self.review, {}, composition="index.html", **kwargs)

        render = resolve(no_reuse=True)
        output = project / "out" / "cut.mp4"
        output.parent.mkdir()
        shutil.copyfile(render["source"], output)
        self.assertEqual(resolve()["source"], output.resolve())
        self.make_movie(asset, frames=120, fps=30)
        os.utime(asset, ns=(original.st_atime_ns, original.st_mtime_ns))
        with self.assertRaises(video_engines.EngineError) as changed:
            resolve()
        self.assertEqual(changed.exception.code, "movieConfirm")

    def test_changed_project_asset_prevents_automatic_reuse(self):
        movie = self.project / "out" / "Main.mp4"
        self.make_movie(movie)
        self.arm_render(self.project, "remotion", MAIN_MOVIE)
        asset = self.project / "public" / "audio.wav"
        asset.parent.mkdir()
        asset.write_bytes(b"synthetic new audio")
        os.utime(asset, (movie.stat().st_mtime + 1, movie.stat().st_mtime + 1))
        with self.assertRaises(video_engines.EngineError) as reason:
            self.resolve()
        self.assertEqual(reason.exception.code, "movieStale")

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
        self.make_movie(self.project / "take2.mp4")
        media = self.make_movie(self.project / "out" / "Main.mp4")
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
        self.assertTrue(Path(render_argv[3]).is_absolute())
        self.assertEqual(Path(render_argv[3]).parent.parent, self.review.resolve() / ".render")
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
        self.assertEqual(list((self.review / ".render").iterdir()), [])
        entries = self.read_bin_log()
        pid = entries[-1]["pid"]
        for _ in range(40):
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                break
            time.sleep(0.05)
        else:
            self.fail("超时后引擎进程仍在运行")

    def test_wait_failure_and_interrupt_reap_the_running_engine(self):
        self.arm_render(self.project, "remotion", MAIN_MOVIE)
        self.set_env(AE_FAKE_ENGINE_MODE="sleep", AE_FAKE_ENGINE_SLEEP="5")
        communicate = subprocess.Popen.communicate
        for failure in (OSError, KeyboardInterrupt):
            with self.subTest(failure=failure.__name__):
                engine = []

                def fail_while_rendering(proc, *args, **kwargs):
                    if proc.args[1] != "render":
                        return communicate(proc, *args, **kwargs)
                    engine.append(proc)
                    raise failure("synthetic wait failure")

                with mock.patch.object(subprocess.Popen, "communicate", fail_while_rendering):
                    with self.assertRaises(failure):
                        self.resolve()
                (proc,) = engine
                try:
                    self.assertIsNotNone(proc.poll(), "等待失败后假引擎仍在运行")
                    self.assertEqual(list((self.review / ".render").iterdir()), [])
                finally:
                    # 旧实现漏回收时，用例收回自己启动的假引擎。
                    if proc.poll() is None:
                        proc.kill()
                    proc.wait(timeout=5)
                    proc.stdout.close()
                    proc.stderr.close()

    @unittest.skipUnless(os.name == "posix", "POSIX 引擎进程组与文件锁")
    def test_engine_failure_or_timeout_stops_its_background_helper(self):
        import fcntl

        helper = self.root / "helper.py"
        helper.write_text(
            textwrap.dedent("""
            import fcntl, os, time

            with open(os.environ["AE_FAKE_HELPER_LEASE"], "w") as lease:
                fcntl.flock(lease, fcntl.LOCK_EX)
                lease.write("started")
                lease.flush()
                print("ready", flush=True)
                time.sleep(3)
                lease.write(" finished")
        """)
        )
        self.arm_render(self.project, "remotion", MAIN_MOVIE)
        for mode, error in (("fail-with-helper", "renderFailed"), ("sleep-with-helper", "renderTimeout")):
            with self.subTest(mode=mode):
                lease = self.root / mode
                self.set_env(AE_FAKE_ENGINE_MODE=mode, AE_FAKE_HELPER=str(helper), AE_FAKE_HELPER_LEASE=str(lease))
                with self.assertRaises(video_engines.EngineError) as problem:
                    self.resolve(render_timeout=1.5)
                self.assertEqual(problem.exception.code, error)
                with lease.open() as handle:
                    fcntl.flock(handle, fcntl.LOCK_SH)  # 子进程退出时释放，漏回收时也会在 3 秒内结束。
                    self.assertEqual(handle.read(), "started")

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
    def test_inspect_identifies_each_unreadable_cut_and_keeps_the_other_candidate(self):
        import io

        project = self.copy_fixture("hyperframes")
        output = project / "out"
        self.make_movie(output / "good.mp4")
        (output / "empty.mp4").write_bytes(b"")
        for lang, reason, fallback in (
            ("en", "The MP4 file is empty", "default frame rate"),
            ("zh-CN", "MP4 文件为空", "默认帧率"),
        ):
            with self.subTest(lang=lang), redirect_stdout(io.StringIO()) as buffer:
                self.assertEqual(video_engines.main(["inspect", str(project), "--locale", lang]), 0)
                report = json.loads(buffer.getvalue())
                movies = {Path(m["path"]).name: m for m in report["movies"]}
                self.assertEqual(movies["empty.mp4"]["reason"], reason)
                self.assertEqual(movies["good.mp4"]["meta"]["frames"], 180)
                self.assertIn(fallback, report["compositions"][0]["evidence"])

    def test_invalid_timeout_is_rejected_before_the_engine_starts(self):
        import io

        project = self.copy_fixture("remotion")
        self.arm_render(project, "remotion", MAIN_MOVIE)
        self.set_env(AE_FAKE_COMPOSITIONS=json.dumps([MAIN_COMP]), AE_FAKE_BIN_LOG=str(self.root / "log"))
        for value in ("nan", "inf", "-inf", "0", "2147484", "1e300"):
            for origin in ("option", "environment"):
                with self.subTest(value=value, origin=origin):
                    self.set_env(AE_VIDEO_RENDER_TIMEOUT=value if origin == "environment" else "")
                    args = [f"--render-timeout={value}"] if origin == "option" else []
                    output = io.StringIO()
                    with redirect_stdout(output):
                        code = video_engines.main(["inspect", str(project), *args])
                    self.assertEqual(code, 2)
                    diagnostic = json.loads(output.getvalue())
                    self.assertEqual(diagnostic["error"], "badTimeout")
                    self.assertIn("2147483", diagnostic["detail"])
        self.assertEqual(self.read_bin_log(), [])
        # 上界仍可用于渲染；假引擎立即结束。
        maximum = video_engines.resolve_source(project, self.root / "review", {}, render_timeout=2147483)
        self.assertEqual(maximum["action"], "rendered")

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

    def test_candidate_lists_media_comparisons_and_file_descriptions_use_the_build_language(self):
        project = self.copy_fixture("hyperframes")
        movie = project / "out" / "candidate.mp4"
        self.make_movie(movie, fps=30)
        for lang, default, separator, comparison, modified in (
            ("en", "default frame rate", ", ", "Movie ", "modified"),
            ("zh-CN", "默认帧率", "、", "成片 ", "修改时间"),
        ):
            for opts, fragments in (
                ({}, [default, "data-fps"]),
                ({"video_composition": "missing"}, [separator, "scene-b.html", "index.html"]),
                ({"video_composition": "index.html", "reuse_movie": movie}, [comparison, "24", "30", "180"]),
                ({"video_composition": "compositions/scene-b.html"}, [modified, "180"]),
            ):
                with self.subTest(lang=lang, opts=opts):
                    with self.assertRaises(build_viewer.BuildError) as error:
                        build_viewer.build(
                            project, None, self.root / "review", locale=lang, no_scan_skills=True, **opts
                        )
                    for fragment in fragments:
                        self.assertIn(fragment, str(error.exception))
                    if lang == "en":
                        self.assertNotIn("、", str(error.exception))

    def build(self, report, out, **kwargs):
        return build_viewer.build(report, None, out, no_scan_skills=True, locale="zh-CN", **kwargs)

    @unittest.skipUnless(os.name == "posix", "POSIX 属主与权限位")
    def test_private_receipt_directory_is_diagnosed_and_render_can_retry(self):
        project = self.copy_fixture("remotion")
        self.arm_render(project, "remotion", MAIN_MOVIE)
        home = self.root / "ledger"
        evidence = home / "video-renders"
        evidence.mkdir(mode=0o700, parents=True)
        self.set_env(AE_HOME=str(home), AE_FAKE_COMPOSITIONS=json.dumps([MAIN_COMP]))
        for failure in ("permissions", "owner"):
            review = self.root / failure
            evidence.chmod(0o750 if failure == "permissions" else 0o700)
            uid = os.getuid() + int(failure == "owner")
            with mock.patch.object(os, "getuid", return_value=uid), self.assertRaises(build_viewer.BuildError) as error:
                self.build(project, review, no_reuse=True)
            with self.subTest(failure=failure, check="diagnosis"):
                self.assertIn("渲染凭据目录", str(error.exception))
                self.assertIn(str(evidence.resolve()), str(error.exception))
            with self.subTest(failure=failure, check="staging"):
                self.assertEqual(list((review / ".render").glob("*")), [])
            evidence.chmod(0o700)
            self.build(project, review, no_reuse=True)
            self.assertEqual((review / "media/v1.mp4").read_bytes(), MAIN_MOVIE)

    def test_receipt_write_failure_and_interrupt_release_render_staging(self):
        project = self.copy_fixture("remotion")
        self.arm_render(project, "remotion", MAIN_MOVIE)
        home = self.root / "ledger"
        self.set_env(AE_HOME=str(home), AE_FAKE_COMPOSITIONS=json.dumps([MAIN_COMP]))
        replace = os.replace
        for failure, diagnostic in ((PermissionError, build_viewer.BuildError), (KeyboardInterrupt, KeyboardInterrupt)):
            with self.subTest(failure=failure.__name__):
                review = self.root / failure.__name__

                def fail_receipt_write(source, target):
                    if Path(target).parent == home.resolve() / "video-renders":
                        raise failure("synthetic receipt write failure")
                    return replace(source, target)

                with mock.patch.object(os, "replace", fail_receipt_write), self.assertRaises(diagnostic) as error:
                    self.build(project, review)
                self.assertIn("synthetic receipt write failure", str(error.exception))
                self.assertEqual(list((review / ".render").iterdir()), [])
                self.build(project, review)
                self.assertEqual((review / "media/v1.mp4").read_bytes(), MAIN_MOVIE)

    def test_replaced_output_is_rejected_before_v1_publication_and_can_be_retried(self):
        import video_review

        project = self.copy_fixture("remotion")
        self.set_env(AE_FAKE_COMPOSITIONS=json.dumps([MAIN_COMP]))
        output = project / "out" / "Main.mp4"
        self.arm_render(project, "remotion", self.make_movie(output))
        self.build(project, self.root / "seed", no_reuse=True)
        prepare = video_review.prepare

        def finish_another_export(source, *args, **options):
            replacement = self.root / "new-export.mp4"
            replacement.write_bytes(MAIN_MOVIE + box(b"free", b"different export"))
            replacement.replace(source)
            return prepare(source, *args, **options)

        for kind, options in (("auto", {}), ("confirmed", {"reuse_movie": output}), ("rendered", {"no_reuse": True})):
            with self.subTest(kind=kind):
                review = self.root / kind
                with mock.patch.object(video_review, "prepare", side_effect=finish_another_export):
                    with self.assertRaisesRegex(build_viewer.BuildError, "成片已更改"):
                        self.build(project, review, **options)
                for relative in ("review.json", ".video-first-build.json", "media/v1.mp4"):
                    self.assertFalse((review / relative).exists())
                self.assertEqual(list((review / "media").iterdir()), [])
                output.write_bytes(MAIN_MOVIE)
                self.build(project, review, **options)
                self.assertEqual((review / "media/v1.mp4").read_bytes(), MAIN_MOVIE)

    def test_interrupted_explicit_reuse_recovers_only_the_selected_digest(self):
        project = self.copy_fixture("remotion")
        self.set_env(AE_FAKE_COMPOSITIONS=json.dumps([MAIN_COMP]))
        movie = project / "out" / "Main.mp4"
        self.arm_render(project, "remotion", self.make_movie(movie))
        review = self.root / "interrupted"
        with self.assertRaises(SystemExit):
            self.build(project, review, reuse_movie=movie, sections_arg="invalid")
        private = review / "media/v1.mp4"
        inode = private.stat().st_ino
        movie.write_bytes(MAIN_MOVIE + box(b"free", b"different export"))
        with self.assertRaises(build_viewer.BuildError):
            self.build(project, review, reuse_movie=movie)
        self.assertFalse((review / "review.json").exists())
        movie.write_bytes(MAIN_MOVIE)
        self.build(project, review, reuse_movie=movie)
        self.assertEqual(private.stat().st_ino, inode)
        self.assertEqual(private.read_bytes(), MAIN_MOVIE)

    def test_nested_review_is_neither_a_source_asset_nor_a_composition(self):
        project = self.copy_fixture("hyperframes")
        (project / "compositions" / "scene-b.html").unlink()
        (project / "index.html").write_text('<html data-fps="30"></html>')
        self.arm_render(project, "hyperframes", MAIN_MOVIE)
        first = project / "first-review"
        self.build(project, first)
        sp = json.loads((first / "review.json").read_text())["video"]["sourceProject"]
        self.assertEqual(sp["match"], "verified")
        movie = project / "out" / "cut.mp4"
        movie.parent.mkdir()
        shutil.copyfile(first / "media" / "v1.mp4", movie)
        git_dir = project / ".git"
        git_dir.mkdir()
        (git_dir / "HEAD").write_text("synthetic")
        result = video_engines.resolve_source(project, self.root / "second-review", {})
        self.assertEqual(result["action"], "reused")
        self.assertEqual(result["composition"]["id"], "index.html")

    def test_project_build_reuses_matching_movie_and_registers_v1(self):
        project = self.copy_fixture("remotion")
        media = self.make_movie(project / "out" / "Main.mp4")
        self.arm_render(project, "remotion", media)
        self.set_env(AE_FAKE_COMPOSITIONS=json.dumps([MAIN_COMP]))
        out = self.root / "review"
        self.build(project, self.root / "seed-review", no_reuse=True)
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
        self.set_env(AE_FAKE_COMPOSITIONS=json.dumps([MAIN_COMP]), AE_FAKE_BIN_LOG=str(self.root / "binlog"))
        script = self.root / "notes.md"
        script.write_text("### 第 1 页｜x\n", encoding="utf-8")
        with self.assertRaises(build_viewer.BuildError):
            build_viewer.build(project, script, self.root / "review", no_scan_skills=True, locale="zh-CN")
        self.assertEqual(self.read_bin_log(), [])
        self.assertFalse((self.root / "review" / ".render").exists())

    def test_project_output_cannot_overwrite_source_pages(self):
        project = self.copy_fixture("hyperframes")
        nested = project / "compositions"
        (nested / "index.html").write_text("<html>nested source</html>")
        for out in (project, nested):
            with self.subTest(out=out):
                source = (out / "index.html").read_bytes()
                with self.assertRaisesRegex(build_viewer.BuildError, "覆盖工程文件"):
                    self.build(project, out, video_composition="index.html")
                self.assertEqual((out / "index.html").read_bytes(), source)
                self.assertFalse((out / "review.json").exists())
                self.assertFalse((out / ".render").exists())
        self.arm_render(project, "hyperframes", synthetic_mp4(fps=24))
        self.build(project, project / "review", video_composition="index.html")
        self.assertTrue((project / "review" / "review.json").exists())


class SourceProjectLinkTests(EngineEnvMixin):
    """直接从工程开始时接入 #193 的源工程关联契约（manifest 的 video.sourceProject）。

    声明与登记 V1 一致 → linked/verified；渲染结果与声明有差异 → pending，
    审阅人用 video_source.confirm 确认；MP4 先批后绑不走这里的自动写入。
    """

    def build_from_project(self, project, out, **kwargs):
        return build_viewer.build(project, None, out, no_scan_skills=True, locale="zh-CN", **kwargs)

    def test_mp4_link_keeps_the_hyperframes_default_rate_check_when_no_fps_is_declared(self):
        import video_source

        project = self.copy_fixture("hyperframes")
        (project / "compositions" / "scene-b.html").unlink()
        (project / "index.html").write_text("<html></html>")
        cut = project / "out" / "cut.mp4"
        for fps in (30, 24):
            with self.subTest(fps=fps):
                self.arm_render(project, "hyperframes", synthetic_mp4(fps=fps))
                self.build_from_project(project, self.root / f"render-{fps}", no_reuse=True)
                self.make_movie(cut, fps=fps)
                review = self.root / f"mp4-{fps}"
                build_viewer.build(cut, None, review, no_scan_skills=True, locale="en")
                video = json.loads((review / "review.json").read_text())["video"]
                proposed = video_source.local_project_link(
                    video, engine="hyperframes", root=str(project), entry={"id": "index.html"}
                )["sourceProject"]
                if fps == 30:
                    self.assertEqual(proposed["match"], "verified")
                else:
                    self.assertEqual(proposed["status"], "pending")
                    self.assertIn("defaultFpsMismatch", proposed["reasons"])

    def test_reused_v1_links_verified_source_project(self):
        project = self.copy_fixture("remotion")
        media = self.make_movie(project / "out" / "Main.mp4")
        self.arm_render(project, "remotion", media)
        self.set_env(AE_FAKE_COMPOSITIONS=json.dumps([MAIN_COMP]))
        out = self.root / "review"
        self.build_from_project(project, self.root / "seed", no_reuse=True)
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

    def test_explicit_reuse_marks_the_project_as_reviewer_confirmed(self):
        project = self.copy_fixture("remotion")
        movie = project / "out" / "Main.mp4"
        self.arm_render(project, "remotion", self.make_movie(movie))
        self.set_env(AE_FAKE_COMPOSITIONS=json.dumps([MAIN_COMP]))
        out = self.root / "review"
        self.build_from_project(project, out, reuse_movie=movie)
        source = json.loads((out / "review.json").read_text())["video"]["sourceProject"]
        self.assertEqual((source["status"], source["match"]), ("linked", "confirmed"))

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
        self.build_from_project(project, self.root / "seed", no_reuse=True)
        self.build_from_project(project, out)
        previous = json.loads((out / "review.json").read_text(encoding="utf-8"))
        self.assertEqual(build_viewer.main(["--rebuild", str(out)]), 0)
        manifest = json.loads((out / "review.json").read_text(encoding="utf-8"))
        for field in ("title", "name", "namespace", "sources", "sourcesAbs"):
            self.assertEqual(manifest[field], previous[field], field)
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

    def test_symlinked_config_and_external_sources_are_blocked_or_skipped(self):
        outside = self.root / "outside.json"
        outside.write_text('{"dependencies":{"remotion":"4.0.0"}}', encoding="utf-8")

        pkg_proj = self.root / "symlink-pkg"
        pkg_proj.mkdir()
        (pkg_proj / "package.json").symlink_to(outside)
        with self.assertRaises(video_engines.EngineError) as pkg_err:
            video_engines.detect_project(pkg_proj)
        self.assertEqual(pkg_err.exception.code, "entryOutside")

        cfg_proj = self.copy_fixture("remotion")
        (cfg_proj / "remotion.config.ts").symlink_to(outside)
        with self.assertRaises(video_engines.EngineError) as cfg_err:
            video_engines.detect_project(cfg_proj)
        self.assertEqual(cfg_err.exception.code, "entryOutside")

        clean_proj = self.copy_fixture("hyperframes")
        (clean_proj / "ext.txt").symlink_to(outside)
        (clean_proj / "dangling.mp4").symlink_to(self.root / "missing-target.mp4")
        info = video_engines.detect_project(clean_proj)
        names = {p.name for p in video_engines.freshness_sources(info)}
        self.assertNotIn("ext.txt", names)
        self.assertNotIn("dangling.mp4", names)
        self.assertIsNone(video_engines.source_fingerprint(info))


if __name__ == "__main__":
    unittest.main()
