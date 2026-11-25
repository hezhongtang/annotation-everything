"""从 Remotion / HyperFrames 源工程解析视频审阅的起始成片（#194）。

职责边界：
- 静态识别独立工程（package.json 依赖 / 入口文件 / composition HTML），不执行工程代码；
- 列出合成或场景候选及 fps、宽高、时长等依据，歧义时把证据交给审阅人确认；
- 复用只认「可证明匹配」的成片（引擎声明的元数据与 MP4 探测结果一致），不认文件名；
- 缺成片时在工程环境调用其引擎渲染到新路径，失败给出可执行的恢复信息。

安全约束：子进程一律 shell=False、固定 argv 模板、cwd 为用户明确关联的工程根；
入口 / composition 路径解析后不得越出工程；新输出路径预先检查不存在；超时后
终止整个进程组。只用 Python 标准库；引擎与依赖由各自工程提供，AE 不安装、不联网。
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import shutil
import subprocess
import sys
import tempfile
import stat
from html.parser import HTMLParser
from pathlib import Path
from typing import Any

ENGINE_REMOTION = "remotion"
ENGINE_HYPERFRAMES = "hyperframes"
ENGINE_NAMES = {ENGINE_REMOTION: "Remotion", ENGINE_HYPERFRAMES: "HyperFrames"}
DEFAULT_RENDER_TIMEOUT = 1800.0
MAX_RENDER_TIMEOUT = 2_147_483  # poll 的有符号 32 位毫秒值能表示的最大整秒数。
LISTING_TIMEOUT_CAP = 180.0
RENDER_TIMEOUT_ENV = "AE_VIDEO_RENDER_TIMEOUT"
FPS_TOLERANCE = 0.01
HYPERFRAMES_DEFAULT_FPS = 30.0
MOVIE_PATTERNS = ("*.mp4", "out/*.mp4")
MOVIE_CAP = 64
HTML_CAP = 32
STDERR_TAIL = 2000
REMOTION_ENTRY_NAMES = tuple(f"src/index.{ext}" for ext in ("tsx", "ts", "jsx", "js"))
REMOTION_CONFIG_NAMES = ("remotion.config.ts", "remotion.config.js", "remotion.config.mjs")
SET_ENTRY_POINT = re.compile(r"setEntryPoint\s*\(\s*['\"]([^'\"]+)['\"]")
SPLIT_COLUMNS = re.compile(r"\s{2,}")
ANSI_CSI = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
REMOTION_ROW = re.compile(r"(\S+)\s+(\d+(?:\.\d+)?)\s+(\d+)x(\d+)\s+(\d+)\s+\([\d.]+ sec\)")


class EngineError(ValueError):
    """工程无法识别、候选有歧义、复用证据不足或渲染失败。

    code 是稳定的错误标识（build_viewer 按 build.video.<code> 取本地化文案），
    detail 包含原始证据（路径、数字、引擎 stderr 摘录）及所选语言的标签。
    """

    def __init__(self, code: str, detail: str = ""):
        super().__init__(f"[{code}] {detail}".strip())
        self.code = code
        self.detail = detail


def _text(locale: str | None, key: str, **params: object) -> str:
    import i18n

    tag = (
        i18n.resolve_locale("auto", system_languages=i18n.system_languages())
        if locale == "auto"
        else i18n.normalize_locale(locale)
    )
    if tag is None:  # 未知或未给语言时按默认语言给警告，不让诊断中断流程
        tag = i18n.DEFAULT_LOCALE
    return i18n.translate(i18n.catalog(tag), key, **params)


def _tail(text: str, limit: int = STDERR_TAIL) -> str:
    return text.strip()[-limit:]


def _inside(root: Path, candidate: Path) -> bool:
    try:
        candidate.resolve().relative_to(root.resolve())
    except ValueError:
        return False
    return True


# ── 静态探测 ─────────────────────────────────────────────────────────────────


def _package_deps(root: Path) -> tuple[Path | None, set[str]]:
    package = root / "package.json"
    if package.is_symlink() or not _inside(root, package):
        raise EngineError("entryOutside", str(package))
    if not package.is_file():
        return None, set()
    try:
        data = json.loads(package.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return package, set()
    if not isinstance(data, dict):
        return package, set()
    deps: set[str] = set()
    for section in ("dependencies", "devDependencies"):
        value = data.get(section)
        if isinstance(value, dict):
            deps.update(str(name) for name in value)
    return package, deps


def _remotion_entry(root: Path, locale: str | None) -> tuple[Path, Path | None]:
    config = None
    for name in REMOTION_CONFIG_NAMES:
        cand = root / name
        if cand.is_symlink() or not _inside(root, cand):
            raise EngineError("entryOutside", str(cand))
        if cand.is_file():
            config = cand
            break
    candidates: list[Path] = []
    if config is not None:
        try:
            text = config.read_text(encoding="utf-8")
        except (OSError, UnicodeError) as exc:
            raise EngineError("configUnreadable", config.name) from exc
        for match in SET_ENTRY_POINT.finditer(text):
            candidates.append(root / match.group(1))
    candidates += [root / name for name in REMOTION_ENTRY_NAMES]
    for candidate in candidates:
        if candidate.is_symlink() or not _inside(root, candidate):
            raise EngineError("entryOutside", str(candidate))
        if candidate.is_file():
            return candidate.resolve(), config
    tried = _text(locale, "build.source.listSep").join(
        str(c.relative_to(root)) if _inside(root, c) else str(c) for c in candidates
    )
    raise EngineError("noEntry", tried)


class _RootTagParser(HTMLParser):
    """记录文档根与第一个合成元素的媒体声明。"""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.tag = ""
        self.attrs: dict[str, str | None] = {}
        self.composition_attrs: dict[str, str | None] | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if not self.tag:  # 只认文档里的第一个标签
            self.tag = tag
            self.attrs = dict(attrs)
        if self.composition_attrs is None and any(key == "data-composition-id" for key, _ in attrs):
            self.composition_attrs = dict(attrs)

    def media_number(self, name: str) -> float | int | None:
        key = "data-" + name
        raw = self.attrs.get(key, (self.composition_attrs or {}).get(key))
        if not raw:
            return None
        try:
            value = float(raw)
        except ValueError:
            return None
        if not math.isfinite(value) or value <= 0:
            return None
        if name in ("width", "height"):
            return int(value) if value.is_integer() else None
        return value


def scan_html_compositions(root: Path) -> list[dict[str, Any]]:
    """HyperFrames 候选：工程根与一层子目录里的 HTML（排除 node_modules 与隐藏目录）。"""
    htmls: list[Path] = []
    for pattern in ("*.html", "*/*.html"):
        for path in root.glob(pattern):
            rel = path.relative_to(root)
            if any(part.startswith(".") or part == "node_modules" for part in rel.parts):
                continue
            if path.parent != root and _inside(root, path.parent) and _is_review_directory(path.parent):
                continue
            htmls.append(path)
    htmls = sorted(dict.fromkeys(htmls), key=lambda p: p.relative_to(root).as_posix())[:HTML_CAP]
    compositions: list[dict[str, Any]] = []
    for path in htmls:
        if path.is_symlink() or not _inside(root, path):
            raise EngineError("compositionOutside", str(path))
        try:
            with path.open(encoding="utf-8", errors="replace") as source:
                head = source.read(65536)
        except OSError:
            continue
        parser = _RootTagParser()
        try:
            parser.feed(head)
        except Exception:
            continue
        if parser.tag != "html":
            continue
        fps = parser.media_number("fps")
        duration = parser.media_number("duration")
        frames = fps * duration if fps and duration else None
        frame_count = round(frames) if frames and math.isfinite(frames) else 0
        compositions.append(
            {
                "id": path.relative_to(root).as_posix(),
                "fps": fps or HYPERFRAMES_DEFAULT_FPS,
                "fpsDeclared": fps is not None,
                "duration": duration,
                "width": parser.media_number("width"),
                "height": parser.media_number("height"),
                "frames": frame_count
                if frame_count and math.isclose(frames, frame_count, rel_tol=0, abs_tol=1e-6)
                else None,
            }
        )
    return compositions


def detect_project(root: Path, *, locale: str | None = None) -> dict[str, Any]:
    """识别引擎与入口。只读工程文件，不执行任何工程代码。"""
    root = Path(root).expanduser()
    if not root.is_dir():
        raise EngineError("notProject", str(root))
    root = root.resolve()
    package, deps = _package_deps(root)
    suffix = ".cmd" if sys.platform == "win32" else ""
    bin_dir = root / "node_modules" / ".bin"
    remotion_bin = bin_dir / ("remotion" + suffix)
    hyperframes_bin = bin_dir / ("hyperframes" + suffix)
    remotion_hit = "remotion" in deps or remotion_bin.is_file()
    hyperframes_hit = "hyperframes" in deps or hyperframes_bin.is_file()
    if remotion_hit and hyperframes_hit:
        raise EngineError("ambiguousEngine", "Remotion + HyperFrames")
    if remotion_hit:
        entry, config = _remotion_entry(root, locale)
        return {
            "engine": ENGINE_REMOTION,
            "root": root,
            "entry": entry,
            "config": config,
            "package": package,
            "bin": remotion_bin,
        }
    if hyperframes_hit:
        compositions = scan_html_compositions(root)
        if not compositions:
            raise EngineError("noCompositions", _text(locale, "engine.detail.noHtml"))
        return {
            "engine": ENGINE_HYPERFRAMES,
            "root": root,
            "compositions": compositions,
            "package": package,
            "bin": hyperframes_bin,
        }
    raise EngineError("notProject", f"package.json={int(package is not None)} *.html={len(list(root.glob('*.html')))}")


# ── 固定 argv 模板与输出解析 ─────────────────────────────────────────────────


def _engine_command(bin_path: Path) -> list[str]:
    if bin_path.suffix != ".cmd":
        return [str(bin_path)]
    # npm 在 Windows 上安装的是批处理启动器。直接用 Node 运行包声明的入口，
    # 让工程路径、合成名等参数在整个调用链中都不经过 cmd.exe。
    engine = bin_path.stem
    package = bin_path.parent.parent / ("@remotion/cli" if engine == ENGINE_REMOTION else engine)
    manifest = package / "package.json"
    try:
        entry = json.loads(manifest.read_text(encoding="utf-8"))["bin"]
    except (OSError, ValueError, TypeError, KeyError) as exc:
        raise EngineError("depsMissing", str(manifest)) from exc
    if isinstance(entry, dict):
        entry = entry.get(engine)
    if not isinstance(entry, str) or Path(entry).is_absolute() or not _inside(package, package / entry):
        raise EngineError("depsMissing", str(manifest))
    script = package / entry
    if not script.is_file():
        raise EngineError("depsMissing", str(script))
    node = shutil.which("node.exe")
    if node is None:
        raise EngineError("depsMissing", "node.exe")
    return [node, str(script)]


def compositions_argv(bin_path: Path, entry: Path) -> list[str]:
    return [*_engine_command(bin_path), "compositions", str(entry)]


def render_argv(engine: str, bin_path: Path, entry_or_root: Path, composition_id: str, output: Path) -> list[str]:
    """渲染命令的固定模板；调用方保证各路径已在工程或审阅目录内。"""
    if engine == ENGINE_REMOTION:
        return [
            *_engine_command(bin_path),
            "render",
            str(entry_or_root),
            str(composition_id),
            str(output),
            "--codec=h264",
            "--overwrite=false",
        ]
    return [
        *_engine_command(bin_path),
        "render",
        ".",
        "--format",
        "mp4",
        "-o",
        str(output),
        "-c",
        str(entry_or_root / composition_id),
    ]


def parse_compositions(stdout: str) -> tuple[list[dict[str, Any]], int]:
    """识别 Remotion 4 的无表头行，兼容按表头映射列位置的旧清单。"""
    lines = [line.strip() for line in ANSI_CSI.sub("", stdout).splitlines() if line.strip()]
    if not lines:
        raise EngineError("compositionsUnparsed", "stdout=0 B")
    rows = [match.groups() for line in lines if (match := REMOTION_ROW.fullmatch(line))]
    if rows:
        return [
            {"id": cid, "fps": float(fps), "width": int(width), "height": int(height), "frames": int(frames)}
            for cid, fps, width, height, frames in rows
        ], 0
    header = SPLIT_COLUMNS.split(lines[0].strip())
    index: dict[str, int] = {}
    for position, label in enumerate(header):
        low = label.lower()
        if "id" in low and "id" not in index:
            index["id"] = position
        elif "width" in low:
            index["width"] = position
        elif "height" in low:
            index["height"] = position
        elif "fps" in low:
            index["fps"] = position
        elif "duration" in low:
            index["duration"] = position
    if not {"id", "width", "height", "fps", "duration"} <= index.keys():
        raise EngineError("compositionsUnparsed", _tail(stdout, 400))
    compositions: list[dict[str, Any]] = []
    unparsed = 0
    for line in lines[1:]:
        cells = SPLIT_COLUMNS.split(line.strip())
        if len(cells) < len(header):
            unparsed += 1
            continue
        try:
            width = int(cells[index["width"]])
            height = int(cells[index["height"]])
            fps = float(cells[index["fps"]])
            raw_duration = cells[index["duration"]]
            frames = int(raw_duration) if re.fullmatch(r"\d+", raw_duration) else round(float(raw_duration) * fps)
        except ValueError:
            unparsed += 1
            continue
        compositions.append(
            {"id": cells[index["id"]].strip(), "width": width, "height": height, "fps": fps, "frames": frames}
        )
    return compositions, unparsed


def list_compositions(info: dict[str, Any]) -> list[dict[str, Any]]:
    """合成 / 场景候选。Remotion 走工程内引擎；HyperFrames 静态解析 HTML。

    列表有自己的超时上限，不与 --render-timeout 耦合：渲染大合成可以调大，
    挂死的入口仍应尽快失败。
    """
    if info["engine"] == ENGINE_HYPERFRAMES:
        return list(info["compositions"])
    bin_path: Path = info["bin"]
    if not bin_path.is_file():
        raise EngineError("depsMissing", str(bin_path))
    code, out, err = _run(
        compositions_argv(bin_path, info["entry"]),
        cwd=info["root"],
        timeout=LISTING_TIMEOUT_CAP,
        timeout_code="compositionsTimeout",
    )
    if code != 0:
        raise EngineError("compositionsFailed", f"exit={code} {_tail(err)}")
    compositions, _unparsed = parse_compositions(out)
    if not compositions:
        raise EngineError("noCompositions", _tail(out, 400))
    return compositions


def choose_composition(
    compositions: list[dict[str, Any]], wanted: str | None, engine: str, root: Path, *, locale: str | None = None
) -> dict[str, Any]:
    if wanted is None:
        if len(compositions) != 1:
            raise EngineError("compositionAmbiguous", "\n".join(_composition_line(c, locale) for c in compositions))
        wanted = compositions[0]["id"]
    if engine == ENGINE_HYPERFRAMES:
        candidate = root / wanted
        if Path(wanted).is_absolute() or not _inside(root, candidate):
            raise EngineError("compositionOutside", str(candidate))
    target = next((c for c in compositions if c["id"] == wanted), None)
    if target is None:
        raise EngineError(
            "compositionMissing", _text(locale, "build.source.listSep").join(c["id"] for c in compositions)
        )
    return target


def _media_text(meta: dict[str, Any], locale: str | None) -> str:
    unknown = _text(locale, "viewer.video.source.unknown")
    fields = {key: meta.get(key) or unknown for key in ("width", "height", "fps", "frames")}
    return _text(locale, "viewer.video.source.mediaSummary", **fields)


def _composition_line(comp: dict[str, Any], locale: str | None) -> str:
    source = "remotion compositions"
    if "fpsDeclared" in comp:
        source = "data-fps" if comp["fpsDeclared"] else _text(locale, "engine.detail.defaultFps")
    return _text(locale, "engine.detail.candidate", id=comp["id"], media=_media_text(comp, locale), source=source)


# ── 成片扫描、匹配与新鲜度 ──────────────────────────────────────────────────


def scan_movies(root: Path, *, locale: str | None = None) -> list[dict[str, Any]]:
    import video_review

    paths: list[Path] = []
    for pattern in MOVIE_PATTERNS:
        paths += sorted(root.glob(pattern))
    paths = list(dict.fromkeys(paths))[:MOVIE_CAP]
    movies: list[dict[str, Any]] = []
    for path in paths:
        if path.is_symlink() or not _inside(root, path) or not path.is_file():
            continue
        try:
            meta = video_review.probe(path)
        except video_review.MediaError as exc:
            movies.append(
                {
                    "path": path,
                    "meta": None,
                    "reason": exc.localized(locale),
                    "diagnostic": {"code": exc.code, "params": exc.params},
                    "mtime": _mtime(path),
                }
            )
            continue
        movies.append({"path": path, "meta": meta, "reason": "", "mtime": _mtime(path)})
    return movies


def _mtime(path: Path) -> float:
    try:
        return path.stat().st_mtime
    except OSError:
        return 0.0


def _is_review_directory(directory: Path) -> bool:
    import fsutil
    import ledger

    try:
        for name in ("review.json", "index.html"):
            info = (directory / name).lstat()
            if not stat.S_ISREG(info.st_mode) or fsutil.is_reparse_or_symlink(info):
                return False
    except OSError:
        return False
    return ledger.read_manifest(directory).get("app") == "annotation-everything"


def _project_inputs(info: dict[str, Any], staging_dir: Path | None = None) -> tuple[list[Path], dict[Path, Path], bool]:
    """只遍历工程内的真实目录。链接可参与凭据的前提是其目标也在扫描范围内。"""
    import fsutil
    import ledger

    root = info["root"]
    excluded = {ledger.home(), staging_dir}
    if info["engine"] == ENGINE_HYPERFRAMES:
        # 本地渲染器在渲染过程中维护历史和输出清单；同目录的其他文件仍可作为素材。
        state_dir = root / ".hyperframes"
        excluded.update(
            state_dir / name
            for name in (
                "history-id",
                "video-state.json",
                "video-state-lock",
                "render-outputs.json",
                "render-outputs-lock",
            )
        )
    files_seen: list[Path] = []
    directories_seen: set[Path] = set()
    links: list[Path] = []
    complete = True

    def unreadable(_error):
        nonlocal complete
        complete = False

    for directory, dirs, files in os.walk(root, onerror=unreadable):
        here = Path(directory)
        if here != root and "review.json" in files and "index.html" in files and _is_review_directory(here):
            dirs.clear()
            continue
        directories_seen.add(here)
        children = sorted((*dirs, *files))
        dirs.clear()
        for name in children:
            path = here / name
            if name in {".git", "node_modules"} or path in excluded:
                continue
            # out/*.mp4 是约定的成片输出。根 MP4 及各目录下的源码、隐藏配置与素材均可被引擎读取。
            if here == root / "out" and name.endswith(".mp4"):
                continue
            try:
                info_stat = path.lstat()
            except OSError:
                complete = False
                continue
            if fsutil._is_reparse_or_symlink(info_stat):
                links.append(path)
            elif stat.S_ISDIR(info_stat.st_mode):
                dirs.append(name)
            elif stat.S_ISREG(info_stat.st_mode):
                files_seen.append(path)
            else:
                complete = False

    covered = directories_seen.union(files_seen)
    internal_links = {}
    for link in links:
        try:
            target = link.resolve(strict=True)
            if target in covered:
                internal_links[link] = target
                continue
        except (OSError, RuntimeError):
            pass
        complete = False
    return files_seen, internal_links, complete


def freshness_sources(info: dict[str, Any], *, staging_dir: Path | None = None) -> list[Path]:
    files, links, _complete = _project_inputs(info, staging_dir)
    return [*files, *links]


def source_fingerprint(info: dict[str, Any], *, staging_dir: Path | None = None) -> str | None:
    import video_review

    files, links, complete = _project_inputs(info, staging_dir)
    if not complete:
        return None
    # 旧算法未记录隐藏素材与链接目标，它签发的凭据不再自动复用。
    digest = hashlib.sha256(b"ae-video-project-inputs/2\0")
    root = info["root"]

    def add_path(path):
        name = os.fsencode(path.relative_to(root).as_posix())
        digest.update(len(name).to_bytes(8, "big") + name)

    try:
        for path in files:
            if path.resolve(strict=True) != path or not stat.S_ISREG(path.lstat().st_mode):
                return None
            digest.update(b"F")
            add_path(path)
            digest.update(bytes.fromhex(video_review.file_digest(path)))
        for link, target in links.items():
            if link.resolve(strict=True) != target:
                return None
            digest.update(b"L")
            add_path(link)
            add_path(target)
    except (OSError, RuntimeError):
        return None
    return digest.hexdigest()


def _render_evidence(info, chosen, source_hash, movie_sha256):
    import ledger

    evidence = {
        "root": str(info["root"]),
        "engine": info["engine"],
        "entry": chosen["id"],
        "inputs": source_hash,
        "sha256": movie_sha256,
    }
    key = hashlib.sha256(json.dumps(evidence, sort_keys=True).encode()).hexdigest()
    return ledger.home() / ledger.VIDEO_RENDERS_DIR / (key + ".json"), evidence


def _private_evidence_directory(directory, *, create):
    import fsutil
    import ledger

    try:
        if create:
            ledger._ensure_home()
            directory.mkdir(mode=0o700, exist_ok=True)
        fsutil.check_private_dir(directory, exact_mode=0o700)
    except OSError as exc:
        if not create and isinstance(exc, FileNotFoundError):
            return False
        raise EngineError("renderEvidenceDirectory", f"{directory}: {exc}") from exc
    except ValueError as exc:
        raise EngineError("renderEvidenceDirectory", str(directory)) from exc
    return True


def known_render(info, chosen, source_hash: str | None, movie_sha256: str) -> bool:
    if source_hash is None:
        return False
    path, evidence = _render_evidence(info, chosen, source_hash, movie_sha256)
    if not _private_evidence_directory(path.parent, create=False):
        return False
    try:
        import fsutil

        return json.loads(fsutil.read_private_text(path, max_bytes=32 * 1024)) == evidence
    except (OSError, ValueError):
        return False


def _matches(meta: dict[str, Any], comp: dict[str, Any]) -> bool:
    """复用证据：引擎声明的 fps 必须与探测一致；Remotion 还核对宽高与帧数。"""
    if abs(float(meta["fps"]) - float(comp["fps"])) >= FPS_TOLERANCE:
        return False
    if "fpsDeclared" not in comp and comp.get("width") and comp.get("height") and comp.get("frames"):
        return meta["width"] == comp["width"] and meta["height"] == comp["height"] and meta["frames"] == comp["frames"]
    return True


def _compare(meta: dict[str, Any], comp: dict[str, Any], locale: str | None) -> str:
    return _text(
        locale, "engine.detail.compare", movie=_media_text(meta, locale), composition=_media_text(comp, locale)
    )


def _source_context(info: dict[str, Any], chosen: dict[str, Any]) -> dict[str, Any]:
    """#193 `video_source.link` 的入参：工程根、入口与工程侧声明特征。

    entry.kind 与 video_source.ENGINES 的口径一致（remotion→composition、
    hyperframes→scene）。declaration 只放引擎确实声明的字段；HyperFrames 的
    宽高与帧数以渲染结果为准，不冒充声明。
    """
    engine: str = info["engine"]
    declaration: dict[str, Any] = {"fps": float(chosen["fps"])}
    if engine == ENGINE_REMOTION:
        declaration.update(width=int(chosen["width"]), height=int(chosen["height"]), frames=int(chosen["frames"]))
    return {
        "root": str(info["root"]),
        "entry": {"kind": "composition" if engine == ENGINE_REMOTION else "scene", "id": chosen["id"]},
        "declaration": declaration,
    }


# ── 子进程：固定 argv、超时与进程组管理 ──────────────────────────────────────


def _kill_tree(proc: subprocess.Popen) -> None:
    if os.name == "posix":
        import signal

        try:
            os.killpg(proc.pid, signal.SIGKILL)  # start_new_session ⇒ 进程组即引擎及其子进程
        except (ProcessLookupError, PermissionError):
            proc.kill()
    else:
        try:
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=10,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            pass
        try:
            proc.kill()
        except OSError:
            pass


def _run(argv: list[str], cwd: Path, timeout: float, timeout_code: str) -> tuple[int, str, str]:
    args = [str(item) for item in argv]
    kwargs: dict[str, Any] = {}
    if os.name == "posix":
        kwargs["start_new_session"] = True
    elif hasattr(subprocess, "CREATE_NEW_PROCESS_GROUP"):
        kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
    with subprocess.Popen(
        args,
        cwd=str(cwd),
        shell=False,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        **kwargs,
    ) as proc:
        try:
            out, err = proc.communicate(timeout=timeout)
        except BaseException as exc:
            _kill_tree(proc)
            proc.wait()
            if isinstance(exc, subprocess.TimeoutExpired):
                raise EngineError(timeout_code, f"timeout={timeout}s argv={' '.join(args)}") from None
            raise
        if proc.returncode:
            # 引擎可能先报错退出，后台辅助进程仍在本次进程组内。
            _kill_tree(proc)
        return proc.returncode, out.decode("utf-8", "replace"), err.decode("utf-8", "replace")


def _resolve_timeout(render_timeout: float | None) -> float:
    bounds = f"0 < seconds <= {MAX_RENDER_TIMEOUT}"
    if render_timeout is None:
        raw = os.environ.get(RENDER_TIMEOUT_ENV, "").strip()
        if raw:
            try:
                render_timeout = float(raw)
            except ValueError as exc:
                raise EngineError("badTimeout", f"{RENDER_TIMEOUT_ENV}={raw} ({bounds})") from exc
    if render_timeout is None:
        render_timeout = DEFAULT_RENDER_TIMEOUT
    if not 0 < render_timeout <= MAX_RENDER_TIMEOUT:
        raise EngineError("badTimeout", f"{render_timeout} ({bounds})")
    return render_timeout


# ── 主流程：解析起始成片（复用或渲染） ─────────────────────────────────────


def resolve_source(
    root: Path,
    review_dir: Path,
    existing: dict[str, Any] | None,
    *,
    composition: str | None = None,
    reuse_movie: Path | None = None,
    no_reuse: bool = False,
    render_timeout: float | None = None,
    locale: str | None = None,
) -> dict[str, Any]:
    """决定这份审阅的起始成片：可证明匹配的既有成片，或工程引擎渲染的新输出。

    返回 {action, source, cleanup, composition, engine, warnings, root, entry,
    declaration}；cleanup 只在渲染路径给出（调用方登记 V1 后负责删除暂存），
    root / entry / declaration 供调用方写入 #193 的 video.sourceProject 关联。
    """
    import video_review

    existing = existing or {}
    if existing.get("format", {}).get("report") == "video":
        raise EngineError("alreadyRegistered", str(review_dir / "review.json"))
    if reuse_movie is not None and no_reuse:
        raise EngineError("reuseConflict", "")
    timeout = _resolve_timeout(render_timeout)
    info = detect_project(root, locale=locale)
    engine: str = info["engine"]
    compositions = list_compositions(info)
    chosen = choose_composition(compositions, composition, engine, info["root"], locale=locale)
    warnings: list[str] = []
    staging_dir = Path(review_dir).resolve() / ".render"
    source_hash = source_fingerprint(info, staging_dir=staging_dir)
    sources = freshness_sources(info, staging_dir=staging_dir)
    source_mtime = max((_mtime(p) for p in sources), default=0.0)

    if reuse_movie is not None:
        movie = Path(reuse_movie).expanduser()
        if not movie.is_file():
            raise EngineError("movieUnreadable", str(movie))
        try:
            meta = video_review.probe(movie, with_digest=True)
        except video_review.MediaError as exc:
            raise EngineError("movieUnreadable", exc.localized(locale)) from exc
        if not _matches(meta, chosen):
            raise EngineError("movieMismatch", f"{movie} {_compare(meta, chosen, locale)}")
        if _mtime(movie) < source_mtime:
            warnings.append(_text(locale, "engine.warn.staleReuse", path=str(movie), composition=chosen["id"]))
        warnings.append(_text(locale, "engine.warn.reused", path=str(movie), composition=chosen["id"]))
        return {
            "action": "reused",
            "source": movie.resolve(),
            "movieSha256": meta["sha256"],
            "cleanup": None,
            "composition": chosen,
            "engine": engine,
            "warnings": warnings,
            **_source_context(info, chosen),
        }

    movies = scan_movies(info["root"], locale=locale)
    matching = [m for m in movies if m["meta"] and _matches(m["meta"], chosen)]
    fresh = [m for m in matching if m["mtime"] >= source_mtime]
    if not no_reuse and fresh:
        known = []
        if source_hash is not None:
            for candidate in fresh:
                try:
                    digest = video_review.file_digest(candidate["path"])
                except OSError as exc:
                    raise EngineError("movieUnreadable", str(candidate["path"])) from exc
                if known_render(info, chosen, source_hash, digest):
                    known.append((candidate, digest))
        if len(known) == 1:
            picked, digest = known[0]
            warnings.append(_text(locale, "engine.warn.reused", path=str(picked["path"]), composition=chosen["id"]))
            return {
                "action": "reused",
                "source": picked["path"].resolve(),
                "movieSha256": digest,
                "cleanup": None,
                "composition": chosen,
                "engine": engine,
                "warnings": warnings,
                **_source_context(info, chosen),
            }
        if len(fresh) > 1 and engine == ENGINE_REMOTION:
            raise EngineError("movieAmbiguous", "\n".join(f"{m['path']} {_describe(m, locale)}" for m in fresh))
        possible = [c["id"] for c in compositions if any(_matches(m["meta"], c) for m in fresh)]
        raise EngineError(
            "movieConfirm",
            "\n".join(f"{m['path']} {_describe(m, locale)}" for m in fresh)
            + " — "
            + _text(locale, "build.source.listSep").join(possible),
        )
    if not no_reuse and matching:
        raise EngineError(
            "movieStale" if engine == ENGINE_REMOTION else "movieConfirm",
            "\n".join(f"{m['path']} {_describe(m, locale)}" for m in matching),
        )

    if not info["bin"].is_file():
        raise EngineError("depsMissing", str(info["bin"]))
    if staging_dir.is_symlink():
        raise EngineError("stagingExists", str(staging_dir))
    staging_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(dir=staging_dir)) / "v1.mp4"
    try:
        result = _render(info, chosen, staging, timeout, locale, warnings)
        if source_hash is not None and source_hash == source_fingerprint(info, staging_dir=staging_dir):
            import fsutil

            record, evidence = _render_evidence(info, chosen, source_hash, result["movieSha256"])
            _private_evidence_directory(record.parent, create=True)
            try:
                if record.exists() or record.is_symlink():
                    fsutil.check_private_regular_file(record, max_bytes=32 * 1024)
                fsutil.atomic_write(record, json.dumps(evidence) + "\n", prefix=".render-")
            except (OSError, ValueError) as exc:
                raise EngineError("renderEvidenceWrite", f"{record}: {exc}") from exc
            result["sourceFingerprint"] = source_hash
        else:
            result["sourceChanged"] = True
        return result
    except BaseException:
        remove_render_staging(staging)
        raise


def _describe(movie: dict[str, Any], locale: str | None) -> str:
    return _text(locale, "engine.detail.movie", media=_media_text(movie["meta"], locale), mtime=f"{movie['mtime']:.0f}")


def _render(
    info: dict[str, Any],
    chosen: dict[str, Any],
    staging: Path,
    timeout: float,
    locale: str | None,
    warnings: list[str],
) -> dict[str, Any]:
    import video_review

    engine: str = info["engine"]
    bin_path: Path = info["bin"]
    argv = render_argv(engine, bin_path, info.get("entry") or info["root"], chosen["id"], staging)
    code, _out, err = _run(argv, cwd=info["root"], timeout=timeout, timeout_code="renderTimeout")
    if code != 0:
        raise EngineError("renderFailed", f"exit={code} {_tail(err)}")
    if staging.is_symlink() or not staging.is_file():
        raise EngineError("renderNoOutput", str(staging))
    try:
        meta = video_review.probe(staging, with_digest=True)
    except video_review.MediaError as exc:
        raise EngineError("renderInvalid", exc.localized(locale)) from exc
    if engine == ENGINE_REMOTION and not _matches(meta, chosen):
        warnings.append(
            _text(
                locale,
                "engine.warn.listingMismatch",
                composition=chosen["id"],
                detail=_compare(meta, chosen, locale),
            )
        )
    warnings.append(
        _text(
            locale,
            "engine.warn.rendered",
            engine=ENGINE_NAMES[engine],
            composition=chosen["id"],
        )
    )
    return {
        "action": "rendered",
        "source": staging,
        "movieSha256": meta["sha256"],
        "cleanup": staging,
        "composition": chosen,
        "engine": engine,
        "warnings": warnings,
        **_source_context(info, chosen),
    }


def remove_render_staging(path: Path) -> None:
    """清理本次渲染独占的临时目录，其他并发渲染各有自己的目录。"""
    try:
        shutil.rmtree(path.parent)
    except FileNotFoundError:
        pass
    except OSError as exc:
        print(f"warning: cannot remove render staging {path}: {exc}", file=sys.stderr)


# ── 只读巡检与 CLI ──────────────────────────────────────────────────────────


def inspect(project: Path, render_timeout: float | None = None, *, locale: str | None = None) -> dict[str, Any]:
    """给审阅人 / Agent 的候选与差异证据；只读，不渲染、不写审阅目录。"""
    _resolve_timeout(render_timeout)  # 只做参数校验，巡检本身不渲染
    info = detect_project(project, locale=locale)
    compositions = list_compositions(info)
    sources = freshness_sources(info)
    source_mtime = max((_mtime(p) for p in dict.fromkeys(sources)), default=0.0)
    movies = scan_movies(info["root"], locale=locale)
    return {
        "engine": ENGINE_NAMES[info["engine"]],
        "root": str(info["root"]),
        "entry": str(info["entry"]) if info["engine"] == ENGINE_REMOTION else None,
        "compositions": [
            {k: v for k, v in comp.items() if k != "fpsDeclared"} | {"evidence": _composition_line(comp, locale)}
            for comp in compositions
        ],
        "movies": [
            {
                "path": str(m["path"]),
                "meta": m["meta"],
                "reason": m["reason"],
                "mtime": m["mtime"],
                "fresh": m["mtime"] >= source_mtime,
                "matches": [c["id"] for c in compositions if m["meta"] and _matches(m["meta"], c)],
            }
            for m in movies
        ],
        "freshnessSources": [str(p) for p in dict.fromkeys(sources)],
        "render": {"command": str(info["bin"]), "available": info["bin"].is_file()},
    }


def main(argv: list[str] | None = None) -> int:
    import argparse
    import i18n

    argv = sys.argv[1:] if argv is None else argv
    try:
        locale = i18n.cli_locale(i18n.explicit_locale(argv))
    except i18n.LocaleError as exc:
        print(json.dumps({"error": "invalidLocale", "detail": exc.params.get("locale", "")}, ensure_ascii=False))
        return 2
    t = i18n.Translator(locale)
    ap = argparse.ArgumentParser(prog="video_engines.py", description=t("engine.inspect.help"))
    ap.add_argument("--locale", help=t("engine.inspect.locale"))
    sub = ap.add_subparsers(dest="command", required=True)
    viewer = sub.add_parser("inspect", help=t("engine.inspect.help"))
    viewer.add_argument("--locale", help=t("engine.inspect.locale"))
    viewer.add_argument("project", type=Path)
    viewer.add_argument("--render-timeout", type=float, default=None)
    args = ap.parse_args(argv)
    try:
        report = inspect(args.project, render_timeout=args.render_timeout, locale=locale)
    except EngineError as exc:
        message = t("build.video." + exc.code, path=args.project, detail=exc.detail or "—")
        print(json.dumps({"error": exc.code, "detail": exc.detail, "message": message}, ensure_ascii=False))
        return 2
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
