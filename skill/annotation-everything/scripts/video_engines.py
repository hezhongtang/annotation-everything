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
LISTING_TIMEOUT_CAP = 180.0
RENDER_TIMEOUT_ENV = "AE_VIDEO_RENDER_TIMEOUT"
FPS_TOLERANCE = 0.01
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
    detail 是语言无关的证据（路径、数字、引擎 stderr 摘录）。
    """

    def __init__(self, code: str, detail: str = ""):
        super().__init__(f"[{code}] {detail}".strip())
        self.code = code
        self.detail = detail


def _warn(locale: str | None, key: str, **params: object) -> str:
    import i18n

    tag = i18n.normalize_locale(locale) if locale else None
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


def _remotion_entry(root: Path) -> tuple[Path, Path | None]:
    config = next((root / name for name in REMOTION_CONFIG_NAMES if (root / name).is_file()), None)
    candidates: list[Path] = []
    if config is not None:
        try:
            text = config.read_text(encoding="utf-8")
        except OSError:
            text = ""
        for match in SET_ENTRY_POINT.finditer(text):
            candidates.append(root / match.group(1))
    candidates += [root / name for name in REMOTION_ENTRY_NAMES]
    for candidate in candidates:
        if not _inside(root, candidate):
            raise EngineError("entryOutside", str(candidate))
        if candidate.is_file():
            return candidate.resolve(), config
    tried = "、".join(str(c.relative_to(root)) if _inside(root, c) else str(c) for c in candidates)
    raise EngineError("noEntry", tried)


class _RootTagParser(HTMLParser):
    """记录文档根与第一个合成元素的帧率声明。"""

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


def scan_html_compositions(root: Path) -> list[dict[str, Any]]:
    """HyperFrames 候选：工程根与一层子目录里的 HTML（排除 node_modules 与隐藏目录）。"""
    htmls: list[Path] = []
    for pattern in ("*.html", "*/*.html"):
        for path in root.glob(pattern):
            rel = path.relative_to(root)
            if any(part.startswith(".") or part == "node_modules" for part in rel.parts):
                continue
            htmls.append(path)
    htmls = sorted(dict.fromkeys(htmls), key=lambda p: p.relative_to(root).as_posix())[:HTML_CAP]
    compositions: list[dict[str, Any]] = []
    for path in htmls:
        if not _inside(root, path):
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
        raw_fps = parser.attrs.get("data-fps", (parser.composition_attrs or {}).get("data-fps"))
        try:
            fps = float(raw_fps) if raw_fps is not None else None
        except ValueError:
            fps = None
        declared = fps is not None and fps > 0
        compositions.append(
            {
                "id": path.relative_to(root).as_posix(),
                "fps": fps if declared else 30.0,
                "fpsDeclared": declared,
                "width": None,
                "height": None,
                "frames": None,
            }
        )
    return compositions


def detect_project(root: Path) -> dict[str, Any]:
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
        raise EngineError("ambiguousEngine", "remotion + hyperframes indicators in one project")
    if remotion_hit:
        entry, config = _remotion_entry(root)
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
            raise EngineError("noCompositions", "HTML composition not found in project root")
        return {
            "engine": ENGINE_HYPERFRAMES,
            "root": root,
            "compositions": compositions,
            "package": package,
            "bin": hyperframes_bin,
        }
    raise EngineError("notProject", f"package.json={'yes' if package else 'no'} html={len(list(root.glob('*.html')))}")


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
        raise EngineError("compositionsUnparsed", "empty output")
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
    compositions: list[dict[str, Any]], wanted: str | None, engine: str, root: Path
) -> dict[str, Any]:
    if wanted is None:
        if len(compositions) != 1:
            raise EngineError("compositionAmbiguous", "\n".join(_composition_line(c) for c in compositions))
        wanted = compositions[0]["id"]
    if engine == ENGINE_HYPERFRAMES:
        candidate = root / wanted
        if Path(wanted).is_absolute() or not _inside(root, candidate):
            raise EngineError("compositionOutside", str(candidate))
    target = next((c for c in compositions if c["id"] == wanted), None)
    if target is None:
        raise EngineError("compositionMissing", "、".join(c["id"] for c in compositions))
    return target


def _composition_line(comp: dict[str, Any]) -> str:
    if comp.get("width") and comp.get("height"):
        source = "remotion compositions"
        return f"{comp['id']}  {comp['width']}x{comp['height']}  fps={comp['fps']}  frames={comp['frames']}  ({source})"
    declared = "data-fps" if comp.get("fpsDeclared") else "fallback"
    return f"{comp['id']}  fps={comp['fps']} ({declared})  width/height/frames: render result"


# ── 成片扫描、匹配与新鲜度 ──────────────────────────────────────────────────


def scan_movies(root: Path) -> list[dict[str, Any]]:
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
        except video_review.VideoError as exc:
            movies.append({"path": path, "meta": None, "reason": str(exc), "mtime": _mtime(path)})
            continue
        movies.append({"path": path, "meta": meta, "reason": "", "mtime": _mtime(path)})
    return movies


def _mtime(path: Path) -> float:
    try:
        return path.stat().st_mtime
    except OSError:
        return 0.0


def freshness_sources(info: dict[str, Any]) -> list[Path]:
    """根目录中的 MP4 可能是工程引用的素材，随其他输入参与新鲜度与内容校验。"""
    root = info["root"]
    sources = []
    for directory, dirs, files in os.walk(root):
        dirs[:] = sorted(d for d in dirs if not d.startswith(".") and d not in {"node_modules", "out", "dist", "build"})
        here = Path(directory)
        if here != root and "review.json" in files and "index.html" in files:
            dirs.clear()
            continue
        for name in sorted(files):
            path = here / name
            if not name.startswith("."):
                sources.append(path)
    return sources


def source_fingerprint(info: dict[str, Any]) -> str:
    import video_review

    digest = hashlib.sha256()
    for path in freshness_sources(info):
        if not path.is_file():
            raise EngineError("movieUnreadable", str(path))
        name = path.relative_to(info["root"]).as_posix().encode("utf-8")
        digest.update(len(name).to_bytes(8, "big") + name)
        digest.update(bytes.fromhex(video_review.file_digest(path)))
    return digest.hexdigest()


def _render_evidence(info, chosen, source_hash, movie):
    import ledger
    import video_review

    evidence = {
        "root": str(info["root"]),
        "engine": info["engine"],
        "entry": chosen["id"],
        "inputs": source_hash,
        "sha256": video_review.file_digest(movie),
    }
    key = hashlib.sha256(json.dumps(evidence, sort_keys=True).encode()).hexdigest()
    return ledger.home() / "video-renders" / (key + ".json"), evidence


def _private_evidence_directory(directory, *, create):
    import fsutil
    import ledger

    if create:
        ledger._ensure_home()
        directory.mkdir(mode=0o700, exist_ok=True)
    info = directory.lstat()
    if (
        not stat.S_ISDIR(info.st_mode)
        or fsutil._is_reparse_or_symlink(info)
        or not fsutil.is_windows()
        and (info.st_uid != os.getuid() or info.st_mode & 0o077)
    ):
        raise EngineError("stagingExists", str(directory))


def _known_render(info, chosen, source_hash, movie):
    path, evidence = _render_evidence(info, chosen, source_hash, movie)
    try:
        import fsutil

        _private_evidence_directory(path.parent, create=False)
        return json.loads(fsutil.read_private_text(path, max_bytes=32 * 1024)) == evidence
    except (OSError, ValueError):
        return False


def _matches(meta: dict[str, Any], comp: dict[str, Any]) -> bool:
    """复用证据：引擎声明的 fps 必须与探测一致；Remotion 还核对宽高与帧数。"""
    if abs(float(meta["fps"]) - float(comp["fps"])) >= FPS_TOLERANCE:
        return False
    if comp.get("width") and comp.get("height") and comp.get("frames"):
        return meta["width"] == comp["width"] and meta["height"] == comp["height"] and meta["frames"] == comp["frames"]
    return True


def _compare(meta: dict[str, Any], comp: dict[str, Any]) -> str:
    left = f"movie {meta['width']}x{meta['height']} fps={meta['fps']} frames={meta['frames']}"
    right = (
        f"composition {comp['width']}x{comp['height']} fps={comp['fps']} frames={comp['frames']}"
        if comp.get("width")
        else f"composition fps={comp['fps']}"
    )
    return f"{left} vs {right}"


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
        proc.kill()


def _run(argv: list[str], cwd: Path, timeout: float, timeout_code: str) -> tuple[int, str, str]:
    args = [str(item) for item in argv]
    kwargs: dict[str, Any] = {}
    if os.name == "posix":
        kwargs["start_new_session"] = True
    elif hasattr(subprocess, "CREATE_NEW_PROCESS_GROUP"):
        kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
    proc = subprocess.Popen(
        args,
        cwd=str(cwd),
        shell=False,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        **kwargs,
    )
    try:
        out, err = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        _kill_tree(proc)
        try:
            out, err = proc.communicate(timeout=10)
        except subprocess.TimeoutExpired:
            out, err = b"", b""
        raise EngineError(timeout_code, f"timeout={timeout}s argv={' '.join(args)}") from None
    return proc.returncode, out.decode("utf-8", "replace"), err.decode("utf-8", "replace")


def _resolve_timeout(render_timeout: float | None) -> float:
    if render_timeout is None:
        raw = os.environ.get(RENDER_TIMEOUT_ENV, "").strip()
        if raw:
            try:
                render_timeout = float(raw)
            except ValueError as exc:
                raise EngineError("badTimeout", f"{RENDER_TIMEOUT_ENV}={raw}") from exc
    if render_timeout is None:
        render_timeout = DEFAULT_RENDER_TIMEOUT
    if render_timeout <= 0:
        raise EngineError("badTimeout", str(render_timeout))
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
    existing = existing or {}
    if existing.get("format", {}).get("report") == "video":
        raise EngineError("alreadyRegistered", str(review_dir / "review.json"))
    if reuse_movie is not None and no_reuse:
        raise EngineError("reuseConflict", "")
    timeout = _resolve_timeout(render_timeout)
    info = detect_project(root)
    engine: str = info["engine"]
    compositions = list_compositions(info)
    chosen = choose_composition(compositions, composition, engine, info["root"])
    warnings: list[str] = []
    source_hash = source_fingerprint(info)
    sources = freshness_sources(info)
    source_mtime = max((_mtime(p) for p in sources), default=0.0)

    if reuse_movie is not None:
        movie = Path(reuse_movie).expanduser()
        if not movie.is_file():
            raise EngineError("movieUnreadable", str(movie))
        import video_review

        try:
            meta = video_review.probe(movie)
        except video_review.VideoError as exc:
            raise EngineError("movieUnreadable", str(exc)) from exc
        if not _matches(meta, chosen):
            raise EngineError("movieMismatch", f"{movie} {_compare(meta, chosen)}")
        if _mtime(movie) < source_mtime:
            warnings.append(_warn(locale, "engine.warn.staleReuse", path=str(movie), composition=chosen["id"]))
        warnings.append(_warn(locale, "engine.warn.reused", path=str(movie), composition=chosen["id"]))
        return {
            "action": "reused",
            "source": movie.resolve(),
            "cleanup": None,
            "composition": chosen,
            "engine": engine,
            "warnings": warnings,
            **_source_context(info, chosen),
        }

    movies = scan_movies(info["root"])
    matching = [m for m in movies if m["meta"] and _matches(m["meta"], chosen)]
    fresh = [m for m in matching if m["mtime"] >= source_mtime]
    if not no_reuse and fresh:
        known = [m for m in fresh if _known_render(info, chosen, source_hash, m["path"])]
        if len(known) == 1:
            picked = known[0]
            warnings.append(_warn(locale, "engine.warn.reused", path=str(picked["path"]), composition=chosen["id"]))
            return {
                "action": "reused",
                "source": picked["path"].resolve(),
                "cleanup": None,
                "composition": chosen,
                "engine": engine,
                "warnings": warnings,
                **_source_context(info, chosen),
            }
        if len(fresh) > 1 and engine == ENGINE_REMOTION:
            raise EngineError("movieAmbiguous", "\n".join(f"{m['path']} {_describe(m)}" for m in fresh))
        possible = [c["id"] for c in compositions if any(_matches(m["meta"], c) for m in fresh)]
        raise EngineError(
            "movieConfirm", "\n".join(f"{m['path']} {_describe(m)}" for m in fresh) + " — " + ", ".join(possible)
        )
    if not no_reuse and matching:
        raise EngineError(
            "movieStale" if engine == ENGINE_REMOTION else "movieConfirm",
            "\n".join(f"{m['path']} {_describe(m)}" for m in matching),
        )

    result = _render(info, chosen, review_dir, timeout, locale, warnings)
    if source_hash == source_fingerprint(info):
        import fsutil

        record, evidence = _render_evidence(info, chosen, source_hash, result["source"])
        _private_evidence_directory(record.parent, create=True)
        if record.exists() or record.is_symlink():
            fsutil.check_private_regular_file(record, max_bytes=32 * 1024)
        fsutil.atomic_write(record, json.dumps(evidence) + "\n", prefix=".render-")
        result["sourceFingerprint"] = source_hash
    else:
        result["sourceChanged"] = True
    return result


def _describe(movie: dict[str, Any]) -> str:
    meta = movie["meta"]
    return (
        f"{meta['width']}x{meta['height']} fps={meta['fps']} frames={meta['frames']} mtime={movie['mtime']:.0f}"
        if meta
        else f"unprobeable({movie['reason']})"
    )


def _render(
    info: dict[str, Any],
    chosen: dict[str, Any],
    review_dir: Path,
    timeout: float,
    locale: str | None,
    warnings: list[str],
) -> dict[str, Any]:
    import video_review

    engine: str = info["engine"]
    bin_path: Path = info["bin"]
    if not bin_path.is_file():
        raise EngineError("depsMissing", str(bin_path))
    staging_dir = Path(review_dir).resolve() / ".render"
    if staging_dir.is_symlink():
        raise EngineError("stagingExists", str(staging_dir))
    staging_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(dir=staging_dir)) / "v1.mp4"
    try:
        argv = render_argv(engine, bin_path, info.get("entry") or info["root"], chosen["id"], staging)
        code, _out, err = _run(argv, cwd=info["root"], timeout=timeout, timeout_code="renderTimeout")
        if code != 0:
            raise EngineError("renderFailed", f"exit={code} {_tail(err)}")
        if staging.is_symlink() or not staging.is_file():
            raise EngineError("renderNoOutput", str(staging))
        try:
            meta = video_review.probe(staging)
        except video_review.VideoError as exc:
            raise EngineError("renderInvalid", str(exc)) from exc
    except BaseException:
        _unlink(staging)
        staging.parent.rmdir()
        raise
    if engine == ENGINE_REMOTION and not _matches(meta, chosen):
        warnings.append(
            _warn(
                locale,
                "engine.warn.listingMismatch",
                composition=chosen["id"],
                detail=_compare(meta, chosen),
            )
        )
    warnings.append(
        _warn(
            locale,
            "engine.warn.rendered",
            engine=ENGINE_NAMES[engine],
            composition=chosen["id"],
        )
    )
    return {
        "action": "rendered",
        "source": staging,
        "cleanup": staging,
        "composition": chosen,
        "engine": engine,
        "warnings": warnings,
        **_source_context(info, chosen),
    }


def _unlink(path: Path) -> None:
    try:
        path.unlink()
    except FileNotFoundError:
        pass
    except OSError:
        pass


# ── 只读巡检与 CLI ──────────────────────────────────────────────────────────


def inspect(project: Path, render_timeout: float | None = None) -> dict[str, Any]:
    """给审阅人 / Agent 的候选与差异证据；只读，不渲染、不写审阅目录。"""
    _resolve_timeout(render_timeout)  # 只做参数校验，巡检本身不渲染
    info = detect_project(project)
    compositions = list_compositions(info)
    sources = freshness_sources(info)
    source_mtime = max((_mtime(p) for p in dict.fromkeys(sources)), default=0.0)
    movies = scan_movies(info["root"])
    return {
        "engine": ENGINE_NAMES[info["engine"]],
        "root": str(info["root"]),
        "entry": str(info["entry"]) if info["engine"] == ENGINE_REMOTION else None,
        "compositions": [
            {k: v for k, v in comp.items() if k != "fpsDeclared"} | {"evidence": _composition_line(comp)}
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

    argv = sys.argv[1:] if argv is None else argv
    ap = argparse.ArgumentParser(
        prog="video_engines.py", description="Inspect a Remotion / HyperFrames project for AE video review."
    )
    sub = ap.add_subparsers(dest="command", required=True)
    viewer = sub.add_parser("inspect", help="list engine, composition candidates and reusable movies as JSON")
    viewer.add_argument("project", type=Path)
    viewer.add_argument("--render-timeout", type=float, default=None)
    args = ap.parse_args(argv)
    try:
        report = inspect(args.project, render_timeout=args.render_timeout)
    except EngineError as exc:
        print(json.dumps({"error": exc.code, "detail": exc.detail}, ensure_ascii=False))
        return 2
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
