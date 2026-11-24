"""本地 MP4 审阅：媒体元数据、V1 私有副本与受控路径解析。

只用 Python 标准库。这里不调用编解码器；构建时读取 ISO BMFF 中播放所必需的
时长、恒定帧率和实际画面尺寸，浏览器负责判断本机能否解码。
"""

from __future__ import annotations

import copy
import hashlib
import json
import math
import mmap
import os
import re
import struct
import sys
import stat
from datetime import datetime, timezone
from fractions import Fraction
from functools import lru_cache
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Iterator

sys.path.insert(0, str(Path(__file__).resolve().parent))
import fsutil  # noqa: E402
import i18n  # noqa: E402
import ledger  # noqa: E402

FORMAT_VERSION = 1
VERSION_ID = re.compile(r"^V[1-9][0-9]*$")
CONTAINERS = {b"moov", b"trak", b"mdia", b"minf", b"stbl", b"edts"}
MAX_BOX_DEPTH = 32
RECEIPT_TARGET_KINDS = ("frame", "range", "cut")
FAILURES_KEEP = 50
MANIFEST_FILE = "review.json"


class VideoError(ValueError):
    """MP4 无法识别、版本未登记或私有媒体路径不安全。"""


class MediaError(VideoError):
    """媒体与登记错误用稳定错误码传递，在显示处选择语言。"""

    def __init__(self, code: str, **params: object):
        self.code = code
        self.params = params
        super().__init__(code)

    def __str__(self) -> str:
        return self.localized(i18n.current_locale())

    def localized(self, locale: str | None) -> str:
        lang = i18n.resolve_locale(locale, system_languages=i18n.system_languages())
        return i18n.translate(i18n.catalog(lang), "video.media.error." + self.code, **self.params)


class StaleRenderBase(VideoError):
    def __init__(self, expected: str, current: str):
        self.expected = expected
        self.current = current
        super().__init__(f"成片基于 {expected}，当前版本为 {current}")


def is_mp4(path: Path) -> bool:
    return path.suffix.lower() == ".mp4"


def _boxes(data: bytes, start: int = 0, end: int | None = None) -> Iterator[tuple[bytes, int, int]]:
    end = len(data) if end is None else end
    pos = start
    while pos < end:
        if end - pos < 8:
            raise MediaError("boxHeader")
        size, kind = struct.unpack_from(">I4s", data, pos)
        header = 8
        if size == 1:
            if end - pos < 16:
                raise MediaError("extendedHeader")
            size = struct.unpack_from(">Q", data, pos + 8)[0]
            header = 16
        elif size == 0:
            size = end - pos
        if size < header or pos + size > end:
            raise MediaError("boxSize")
        yield kind, pos + header, pos + size
        pos += size


def _children(data: bytes, start: int, end: int) -> Iterator[tuple[bytes, int, int]]:
    yield from _boxes(data, start, end)


def _full_payload(data: bytes, start: int, end: int) -> bytes:
    if end - start < 4:
        raise MediaError("metadata")
    return data[start + 4 : end]


def _media_time(data: bytes, start: int, end: int, kind: bytes) -> tuple[int, int]:
    payload = _full_payload(data, start, end)
    version = data[start]
    if version == 0:
        if len(payload) < 16:
            raise MediaError("timing")
        scale, duration = struct.unpack_from(">II", payload, 8)
    elif version == 1:
        if len(payload) < 28:
            raise MediaError("timing")
        scale, duration = struct.unpack_from(">IQ", payload, 16)
    else:
        raise MediaError("timingVersion", box=kind.decode())
    if not scale or not duration:
        raise MediaError("duration")
    return scale, duration


def _check_samples(data: bytes, tables: dict, media: list[tuple[int, int]], frames: int) -> None:
    def rows(kind: bytes, fmt: str):
        start, end = tables.get(kind, (0, 0))
        if end - start < 8:
            raise MediaError("sampleTable")
        count = struct.unpack_from(">I", data, start + 4)[0]
        if count == 0 or end - start - 8 != count * struct.calcsize(fmt):
            raise MediaError("sampleTableSize")
        return struct.iter_unpack(fmt, data[start + 8 : end])

    offsets = rows(b"stco", ">I") if b"stco" in tables else rows(b"co64", ">Q")
    chunks = list(rows(b"stsc", ">III"))
    if chunks[0][0] != 1 or any(a[0] >= b[0] for a, b in zip(chunks, chunks[1:])):
        raise MediaError("chunkTable")
    start, end = tables.get(b"stsz", tables.get(b"stz2", (0, 0)))
    if end - start < 12:
        raise MediaError("sampleSize")
    size, count = struct.unpack_from(">II", data, start + 4)
    if count != frames or frames > sum(b - a for a, b in media):
        raise MediaError("sampleCount")
    field = size & 255 if b"stsz" not in tables else 0 if size else 32
    if field not in (0, 4, 8, 16, 32) or end - start - 12 != (count * field + 7) // 8:
        raise MediaError("sizeTable")
    pos = start + 12
    index = run = 0
    for chunk, (offset,) in enumerate(offsets, 1):
        if run + 1 < len(chunks) and chunk == chunks[run + 1][0]:
            run += 1
        samples, desc = chunks[run][1:]
        if not desc or not samples or index + samples > frames:
            raise MediaError("chunkSamples")
        if field:
            length = 0
            for sample in range(index, index + samples):
                if field == 4:
                    n = data[pos + sample // 2]
                    n = n & 15 if sample % 2 else n >> 4
                else:
                    width = field // 8
                    n = int.from_bytes(data[pos + sample * width : pos + (sample + 1) * width], "big")
                if not n:
                    raise MediaError("emptySample")
                length += n
        else:
            length = size * samples
        if not any(a <= offset and offset + length <= b for a, b in media):
            raise MediaError("sampleBounds")
        index += samples
    if index != frames or run != len(chunks) - 1:
        raise MediaError("chunkCoverage")


def _composition_start(data: bytes, ctts: tuple[int, int] | None, frames: int, delta: int) -> int:
    """编辑列表的起点采用呈现时钟，含 B 帧的成片可有非零起始偏移。"""
    if ctts is None:
        return 0
    start, end = ctts
    if end - start < 8 or data[start] not in (0, 1):
        raise MediaError("presentationTable")
    runs = struct.unpack_from(">I", data, start + 4)[0]
    if not runs or end - start - 8 != runs * 8:
        raise MediaError("presentationTableSize")
    fmt = ">II" if data[start] == 0 else ">Ii"
    first_pts = None
    samples = 0
    for pos in range(start + 8, end, 8):
        count, offset = struct.unpack_from(fmt, data, pos)
        if not count:
            raise MediaError("emptyPresentationRun")
        pts = samples * delta + offset
        first_pts = pts if first_pts is None else min(first_pts, pts)
        samples += count
    if samples != frames or first_pts is None:
        raise MediaError("presentationCount")
    return first_pts


def _edited_timeline(
    data: bytes,
    edit: tuple[int, int],
    movie_header: tuple[int, int] | None,
    ctts: tuple[int, int] | None,
    scale: int,
    delta: int,
    frames: int,
) -> tuple[Fraction, int]:
    start, end = edit
    if end - start < 8 or data[start] not in (0, 1) or movie_header is None:
        raise MediaError("editClock")
    fmt = ">Iihh" if data[start] == 0 else ">Qqhh"
    count = struct.unpack_from(">I", data, start + 4)[0]
    if count != 1 or end - start != 8 + struct.calcsize(fmt):
        raise MediaError("editCount")
    length, media_start, rate, fraction = struct.unpack_from(fmt, data, start + 8)
    if not length or media_start < 0 or (rate, fraction) != (1, 0):
        raise MediaError("editRate")
    first_pts = _composition_start(data, ctts, frames, delta)
    skipped, remainder = divmod(media_start - first_pts, delta)
    if remainder or not 0 <= skipped < frames:
        raise MediaError("editStart")
    movie_scale, _ = _media_time(data, *movie_header, b"mvhd")
    seconds = Fraction(length, movie_scale)
    frame_count = seconds * scale / delta
    # movie timescale 可能比媒体时钟粗。末帧能因换算多出不足一个 movie tick。
    remaining = frames - skipped
    if frame_count > remaining and seconds - Fraction(remaining * delta, scale) >= Fraction(1, movie_scale):
        raise MediaError("editBounds")
    return seconds, min(math.ceil(frame_count), remaining)


def _video_track(data: bytes, start: int, end: int, media: list[tuple[int, int]], movie_header) -> dict[str, Any]:
    scale = duration = None
    frames = delta = width = height = None
    variable = False
    tables = {}
    pending = list(_children(data, start, end))
    while pending:
        kind, child_start, child_end = pending.pop(0)
        if kind == b"elst" and kind in tables:
            raise MediaError("duplicateEdits")
        if kind in (b"stsz", b"stz2", b"stsc", b"stco", b"co64", b"ctts", b"elst"):
            tables[kind] = (child_start, child_end)
        if kind in CONTAINERS:
            pending[0:0] = list(_children(data, child_start, child_end))
            continue
        if kind == b"mdhd":
            scale, duration = _media_time(data, child_start, child_end, kind)
        elif kind == b"stsd":
            payload = _full_payload(data, child_start, child_end)
            if len(payload) < 4:
                continue
            entries_at = child_start + 8
            for sample_kind, sample_start, sample_end in _boxes(data, entries_at, child_end):
                if sample_kind not in (b"avc1", b"avc3", b"hvc1", b"hev1", b"vp09", b"av01"):
                    continue
                if sample_end - sample_start < 28:
                    raise MediaError("dimensions")
                width, height = struct.unpack_from(">HH", data, sample_start + 24)
        elif kind == b"stts":
            payload = _full_payload(data, child_start, child_end)
            if len(payload) < 4:
                continue
            count = struct.unpack_from(">I", payload, 0)[0]
            if len(payload) < 4 + count * 8:
                raise MediaError("frameTimes")
            frames = 0
            deltas = set()
            for index in range(count):
                samples, sample_delta = struct.unpack_from(">II", payload, 4 + index * 8)
                if not samples or not sample_delta:
                    raise MediaError("emptyFrameTime")
                frames += samples
                deltas.add(sample_delta)
            variable = len(deltas) != 1
            delta = deltas.pop() if not variable else None
    if not all(isinstance(value, int) and value > 0 for value in (scale, duration, frames, width, height)):
        raise MediaError("trackMetadata")
    if variable or not delta:
        raise MediaError("variableFrameRate")
    _check_samples(data, tables, media, frames)
    frame_clock = Fraction(delta, scale)
    fps = 1 / frame_clock
    seconds = Fraction(duration, scale)
    if b"elst" in tables:
        seconds, frames = _edited_timeline(
            data, tables[b"elst"], movie_header, tables.get(b"ctts"), scale, delta, frames
        )
    return {
        "width": width,
        "height": height,
        "duration": float(seconds),
        "fps": float(fps),
        "timebase": {"num": frame_clock.numerator, "den": frame_clock.denominator},
        "frames": frames,
    }


def probe(path: Path, *, with_digest: bool = False) -> dict[str, Any]:
    """读取 MP4 元数据；无法确认精确帧定位时明确拒绝，不猜测帧率。"""
    try:
        with path.open("rb") as source:
            before = os.fstat(source.fileno())
            if not before.st_size:
                raise MediaError("emptyFile")
            with mmap.mmap(source.fileno(), 0, access=mmap.ACCESS_READ) as data:
                metadata = _probe_container(data)
            if with_digest:
                metadata["sha256"] = _stream_digest(source)
                after = os.fstat(source.fileno())
                if (before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (
                    after.st_size,
                    after.st_mtime_ns,
                    after.st_ctime_ns,
                ):
                    raise MediaError("fileChanged")
            return metadata
    except OSError as exc:
        raise MediaError("read", detail=str(exc)) from exc


def _probe_container(data: mmap.mmap) -> dict[str, Any]:
    top = list(_boxes(data))
    media = [(start, end) for kind, start, end in top if kind == b"mdat" and end > start]
    if not media:
        raise MediaError("noMediaData")
    if not any(kind == b"ftyp" for kind, _start, _end in top):
        raise MediaError("noFileType")
    moov = next(((start, end) for kind, start, end in top if kind == b"moov"), None)
    if moov is None:
        raise MediaError("noMetadataBox")

    def walk(start: int, end: int, depth: int = 1) -> Iterator[tuple[bytes, int, int]]:
        if depth > MAX_BOX_DEPTH:
            raise MediaError("boxDepth", limit=MAX_BOX_DEPTH)
        for kind, child_start, child_end in _boxes(data, start, end):
            yield kind, child_start, child_end
            if kind in CONTAINERS:
                yield from walk(child_start, child_end, depth + 1)

    tracks = []
    for kind, start, end in walk(*moov):
        if kind != b"trak":
            continue
        descendants = list(walk(start, end))
        if any(k == b"hdlr" and data[s + 8 : s + 12] == b"vide" for k, s, _e in descendants if _e - s >= 12):
            tracks.append((start, end))
    if len(tracks) != 1:
        raise MediaError("trackCount")
    movie_header = next(((s, e) for k, s, e in _boxes(data, *moov) if k == b"mvhd"), None)
    return _video_track(data, *tracks[0], media, movie_header)


def registered_version(manifest: dict[str, Any], version_id: str) -> dict[str, Any]:
    video = manifest.get("video") if isinstance(manifest, dict) else None
    versions = video.get("versions") if isinstance(video, dict) else None
    if not VERSION_ID.fullmatch(version_id) or not isinstance(versions, list):
        raise MediaError("unregistered")
    for version in versions:
        if isinstance(version, dict) and version.get("versionId") == version_id:
            return version
    raise MediaError("unregistered")


def media_path(review_dir: Path, manifest: dict[str, Any], version_id: str) -> Path:
    """只解析清单内的私有相对路径；拒绝绝对路径、穿越、符号链接和跨目录读取。"""
    version = registered_version(manifest, version_id)
    raw = version.get("path")
    if not isinstance(raw, str) or not raw or "\\" in raw or "\0" in raw:
        raise MediaError("privatePath")
    pure = PurePosixPath(raw)
    if pure.is_absolute() or ".." in pure.parts or not pure.parts or pure.parts[0] != "media":
        raise MediaError("privatePath")
    root = (review_dir / "media").resolve()
    candidate = review_dir.joinpath(*pure.parts)
    current = review_dir
    for part in pure.parts:
        current = current / part
        if current.is_symlink():
            raise MediaError("mediaSymlink")
    try:
        resolved = candidate.resolve(strict=True)
        relative = resolved.relative_to(root)
    except (OSError, ValueError) as exc:
        raise MediaError("missingMedia") from exc
    if relative.as_posix() != PurePosixPath(*pure.parts[1:]).as_posix() or not resolved.is_file():
        raise MediaError("privatePath")
    return resolved


def _stream_digest(source) -> str:
    digest = hashlib.sha256()
    for block in iter(lambda: source.read(1024 * 1024), b""):
        digest.update(block)
    return digest.hexdigest()


def file_digest(path: Path) -> str:
    with path.open("rb") as source:
        return _stream_digest(source)


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _staging_slot(review_dir: Path, *, create: bool = False) -> Path | None:
    """仅在元数据锁内使用。由 AE 声明的私有目录拥有一个成片复制槽。"""
    media = review_dir / "media"
    try:
        media_info = media.lstat()
    except FileNotFoundError:
        return None
    if fsutil._is_reparse_or_symlink(media_info):
        raise MediaError("directorySymlink")
    directory = media / ".ae-staging"
    owner = directory / ".ae-staged-mp4-v1"
    try:
        info = directory.lstat()
    except FileNotFoundError:
        if not create:
            return None
        directory.mkdir(mode=0o700)
        info = directory.lstat()
    if (
        not stat.S_ISDIR(info.st_mode)
        or fsutil._is_reparse_or_symlink(info)
        or (not fsutil.is_windows() and (info.st_uid != os.getuid() or info.st_mode & 0o077))
        or (fsutil.is_windows() and not fsutil._win_same_owner(directory))
    ):
        raise MediaError("unregisteredCopy", path="media/.ae-staging")
    try:
        fsutil.check_private_regular_file(owner, max_bytes=0)
    except FileNotFoundError:
        if not create:
            return None
        if any(directory.iterdir()):
            raise MediaError("unregisteredCopy", path="media/.ae-staging")
        # 空标记由一次独占创建完成，复制中断时不会留下半写的归属声明。
        os.close(os.open(owner, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600))
    except ValueError as exc:
        raise MediaError("unregisteredCopy", path="media/.ae-staging") from exc
    return directory / "copy.tmp"


def _remove_abandoned_staging(review_dir: Path, source: Path | None = None) -> None:
    slot = _staging_slot(review_dir)
    if slot is not None:
        if source is not None and Path(source).resolve() == slot.resolve():
            raise MediaError("unregisteredCopy", path="media/.ae-staging/copy.tmp")
        slot.unlink(missing_ok=True)


def _stage_and_probe_mp4(
    source: Path, review_dir: Path, version_num: int, before_publish: Callable | None = None
) -> dict[str, Any]:
    """将源成片拷入私有临时文件（0600 + fsync）并做 MP4 探测，通过后再原子替换为 media/v{N}.mp4。"""
    review_dir.mkdir(parents=True, exist_ok=True)
    media_dir = review_dir / "media"
    if media_dir.is_symlink():
        raise MediaError("directorySymlink")
    media_dir.mkdir(mode=0o700, exist_ok=True)
    rel_path = f"media/v{version_num}.mp4"
    target = media_dir / f"v{version_num}.mp4"
    if target.exists() or target.is_symlink():
        raise MediaError("unregisteredCopy", path=rel_path)
    temporary = _staging_slot(review_dir, create=True)
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    digest = hashlib.sha256()
    try:
        try:
            with os.fdopen(fd, "wb") as dst, source.open("rb") as src:
                while chunk := src.read(1024 * 1024):
                    digest.update(chunk)
                    dst.write(chunk)
                dst.flush()
                os.fsync(dst.fileno())
        except OSError as exc:
            raise MediaError("stage", detail=str(exc)) from exc
        metadata = probe(temporary)
        version = {
            "versionId": f"V{version_num}",
            "path": rel_path,
            "sha256": digest.hexdigest(),
            **metadata,
        }
        if before_publish:
            before_publish(version)
        os.link(temporary, target)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass
    return version


def _read_video_manifest(review_dir: Path, *, review_id: str | None = None) -> tuple[Path, dict[str, Any]]:
    manifest_path = Path(review_dir) / MANIFEST_FILE
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise MediaError("readManifest", detail=str(exc)) from exc
    if not isinstance(manifest, dict) or manifest.get("format", {}).get("report") != "video":
        raise MediaError("notVideo")
    if review_id is not None and manifest.get("reviewId") != review_id:
        raise MediaError("reviewMismatch")
    video = manifest.get("video")
    if not isinstance(video, dict) or not isinstance(video.get("versions"), list):
        raise MediaError("versionList")
    return manifest_path, manifest


def _write_manifest(manifest_path: Path, manifest: dict[str, Any]) -> None:
    fsutil.atomic_write(
        manifest_path,
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        prefix="." + MANIFEST_FILE + "-",
    )


def _next_version_number(video: dict[str, Any]) -> int:
    highest = 0
    for item in video.get("versions", []):
        if not isinstance(item, dict):
            continue
        vid = str(item.get("versionId") or "")
        if VERSION_ID.fullmatch(vid):
            highest = max(highest, int(vid[1:]))
    return highest + 1


def prepare(
    source: Path,
    review_dir: Path,
    existing: dict[str, Any] | None = None,
    *,
    origin: dict | None = None,
    expected_sha256: str | None = None,
) -> dict[str, Any]:
    """登记 V1。重建时从私有副本读取，绝不回头采用可能已被覆盖的原文件。"""
    existing = existing or {}
    if existing.get("format", {}).get("report") == "video":
        version = registered_version(existing, str(existing.get("video", {}).get("currentVersionId") or "V1"))
        private = media_path(review_dir, existing, version["versionId"])
        if source.resolve() != private:
            raise MediaError("rebuildSource")
        digest = file_digest(private)
        if digest != version.get("sha256"):
            raise MediaError("rebuildDigest", version=version["versionId"])
        return json.loads(json.dumps(existing["video"]))

    review_dir.mkdir(parents=True, exist_ok=True)
    journal = review_dir / ".video-first-build.json"
    identity = origin or {"source": str(source.resolve())}
    with ledger.metadata_lock(review_dir, exclusive=True):
        _remove_abandoned_staging(review_dir, source)
        if journal.is_file() and (review_dir / "media" / "v1.mp4").exists():
            try:
                saved = json.loads(fsutil.read_private_text(journal, max_bytes=32 * 1024))
            except (OSError, ValueError) as exc:
                raise MediaError("buildRecord") from exc
            version = saved["version"]
            if saved.get("origin") != identity or version.get("path") != "media/v1.mp4":
                raise MediaError("otherBuild")
            private = media_path(review_dir, {"video": {"versions": [version]}}, "V1")
            if (
                file_digest(private) != version["sha256"]
                or not identity.get("sourceFingerprint")
                and (expected_sha256 or file_digest(source)) != version["sha256"]
            ):
                raise MediaError("sourceDigest")
        else:

            def save_intent(entry):
                if expected_sha256 is not None and entry["sha256"] != expected_sha256:
                    raise MediaError("changedAfterVerification")
                _write_manifest(journal, {"origin": identity, "version": entry})

            version = _stage_and_probe_mp4(source, review_dir, 1, save_intent)
    return {"formatVersion": FORMAT_VERSION, "currentVersionId": "V1", "versions": [version]}


def register_version(
    review_dir: Path,
    source_mp4: Path,
    *,
    batch: int,
    by: str,
    review_id: str | None = None,
    receipt_id: str | None = None,
    await_receipt: bool = False,
    base_version_id: str | None = None,
) -> dict[str, Any]:
    """在元数据锁内登记新版成片（V2/V3…），绝不覆盖旧版，也不写入本机源输出路径。"""
    if isinstance(batch, bool) or not isinstance(batch, int) or batch < 1:
        raise VideoError("batch 须为正整数")
    actor = str(by or "").strip()[:200]
    if not actor:
        raise VideoError("by 须为非空处理者标识")
    rid_key = str(receipt_id).strip()[:128] if receipt_id is not None else None
    if receipt_id is not None and not rid_key:
        raise VideoError("receipt_id 不能为空")

    with ledger.metadata_lock(review_dir, exclusive=True):
        manifest_path, manifest = _read_video_manifest(review_dir, review_id=review_id)
        video = manifest["video"]
        _remove_abandoned_staging(review_dir, source_mp4)
        if rid_key:
            for item in video["versions"]:
                if isinstance(item, dict) and item.get("batch") == batch and item.get("receiptId") == rid_key:
                    return json.loads(json.dumps(item))

        journal = Path(review_dir) / ".video-registration.json"
        if journal.exists():
            pending = json.loads(fsutil.read_private_text(journal, max_bytes=32 * 1024))
            if pending["reviewId"] != manifest["reviewId"]:
                raise VideoError("待恢复的视频版本与审阅标识不匹配")
            if pending.get("awaitReceipt"):
                raise VideoError("上一份视频回执仍在提交，请重试")
            entry = pending["version"]
            expected = f"media/v{_next_version_number(video)}.mp4"
            if entry.get("path") == expected and (Path(review_dir) / expected).exists():
                candidate = {**manifest, "video": {**video, "versions": [*video["versions"], entry]}}
                published = media_path(Path(review_dir), candidate, entry["versionId"])
                if file_digest(published) != entry["sha256"]:
                    raise VideoError("待恢复的视频内容与登记信息不匹配")
                video["versions"].append(entry)
                if entry.get("baseVersionId", video["currentVersionId"]) == video["currentVersionId"]:
                    video["currentVersionId"] = entry["versionId"]
                    video.pop("lastFailure", None)
                _write_manifest(manifest_path, manifest)
                journal.unlink()
                if entry.get("receiptId") == rid_key and entry["batch"] == batch:
                    return entry
            else:
                journal.unlink()

        current = video["currentVersionId"]
        if base_version_id is not None and base_version_id != current:
            raise StaleRenderBase(base_version_id, current)

        def save_intent(entry):
            entry.update(batch=batch, by=actor, at=_now_iso(), baseVersionId=current)
            if rid_key:
                entry["receiptId"] = rid_key
            _write_manifest(
                journal, {"reviewId": manifest["reviewId"], "version": entry, "awaitReceipt": await_receipt}
            )

        version = _stage_and_probe_mp4(Path(source_mp4), Path(review_dir), _next_version_number(video), save_intent)
        if await_receipt:
            return version
        video["versions"].append(version)
        video["currentVersionId"] = version["versionId"]
        video.pop("lastFailure", None)
        _write_manifest(manifest_path, manifest)
        journal.unlink()
        return json.loads(json.dumps(version))


def finish_receipt_version(review_dir: Path, pending: dict, *, accepted: bool | None) -> None:
    """批次落盘后发布相应成片或失败记录，中断后以同一份回执重放。"""
    with ledger.metadata_lock(review_dir, exclusive=True):
        journal = review_dir / ".video-registration.json"
        try:
            current = json.loads(fsutil.read_private_text(journal, max_bytes=32 * 1024))
        except FileNotFoundError:
            return  # 直接登记成片的写入方已在同一把元数据锁内完成。
        if current != pending:
            raise VideoError("暂存的视频版本已改变")
        manifest_path, manifest = _read_video_manifest(review_dir, review_id=pending["reviewId"])
        video = manifest["video"]
        _remove_abandoned_staging(review_dir)
        if failure := pending.get("failure"):
            if accepted and failure not in video.get("failures", []):
                _append_failure(video, failure)
                _write_manifest(manifest_path, manifest)
            journal.unlink()
            return
        version = pending["version"]
        if version not in video["versions"]:
            number = _next_version_number(video)
            if version.get("versionId") != f"V{number}" or version.get("path") != f"media/v{number}.mp4":
                raise VideoError("暂存的视频版本与版本清单不匹配")
            present = (review_dir / version["path"]).exists()
            if accepted is None:
                # 直接登记的有效成片只需核对已暂存的副本，无需等待批次回执。
                accepted = present
            if accepted or present:
                candidate = {**manifest, "video": {**video, "versions": [*video["versions"], version]}}
                staged = media_path(review_dir, candidate, version["versionId"])
                if file_digest(staged) != version["sha256"]:
                    raise VideoError("暂存的视频内容与登记信息不匹配")
                if accepted:
                    video["versions"].append(version)
                    # 旧写入方可能在中断期间切换过默认播放版本。保留已接收的证据及当前成片。
                    if version.get("baseVersionId", video["currentVersionId"]) == video["currentVersionId"]:
                        video["currentVersionId"] = version["versionId"]
                        video.pop("lastFailure", None)
                    _write_manifest(manifest_path, manifest)
                else:
                    staged.unlink()
        journal.unlink()


def reject_decoded_version(review_dir: Path, version_id: str, batch: int, review_id: str, reason: str) -> dict:
    """保留浏览器不能解码的版本证据，默认播放最近的有效成片。"""
    with ledger.metadata_lock(review_dir, exclusive=True):
        path, manifest = _read_video_manifest(review_dir, review_id=review_id)
        video = manifest["video"]
        version = registered_version(manifest, version_id)
        if version.get("batch") != batch:
            raise VideoError("版本不属于指定批次")
        if version.get("playbackFailure"):
            return version["playbackFailure"]
        failure = {
            "versionId": version_id,
            "batch": batch,
            "by": "page",
            "at": _now_iso(),
            "stage": "decode",
            "reason": reason,
        }
        version["playbackFailure"] = failure
        video["failures"] = [*video.get("failures", []), failure][-FAILURES_KEEP:]
        if video["currentVersionId"] == version_id:
            video["currentVersionId"] = next(
                v["versionId"] for v in reversed(video["versions"]) if not v.get("playbackFailure")
            )
            video["lastFailure"] = failure
        _write_manifest(path, manifest)
        return failure


def finish_decode_recovery(review_dir: Path, review_id: str, failures: list[dict]) -> None:
    """批注和批次已落盘才标记完成。旧版缺少标记的解码失败也可恢复。"""
    with ledger.metadata_lock(review_dir, exclusive=True):
        path, manifest = _read_video_manifest(review_dir, review_id=review_id)
        changed = False
        for failure in failures:
            version = registered_version(manifest, failure["versionId"])
            if version.get("playbackFailure") == failure and not version.get("playbackFailureSynced"):
                version["playbackFailureSynced"] = True
                changed = True
        if changed:
            _write_manifest(path, manifest)


def _append_failure(video: dict, failure: dict) -> None:
    failures = video.get("failures") if isinstance(video.get("failures"), list) else []
    video["failures"] = [*failures, failure][-FAILURES_KEEP:]
    video["lastFailure"] = failure


def record_failure(
    review_dir: Path,
    *,
    batch: int,
    by: str,
    reason: str,
    review_id: str | None = None,
    receipt_id: str | None = None,
    stage: str = "render",
    await_receipt: bool = False,
) -> dict[str, Any]:
    """在元数据锁内记录渲染或产物不可播放失败，保留上一有效版与已有版本不变。"""
    if isinstance(batch, bool) or not isinstance(batch, int) or batch < 1:
        raise VideoError("batch 须为正整数")
    actor = str(by or "").strip()[:200]
    if not actor:
        raise VideoError("by 须为非空处理者标识")
    clean_reason = str(reason or "").strip()[:2000]
    if not clean_reason:
        raise VideoError("失败记录须说明原因")
    clean_stage = str(stage or "render").strip()[:40] or "render"
    rid_key = str(receipt_id).strip()[:128] if receipt_id is not None else None

    with ledger.metadata_lock(review_dir, exclusive=True):
        manifest_path, manifest = _read_video_manifest(review_dir, review_id=review_id)
        video = manifest["video"]
        failures = video.get("failures") if isinstance(video.get("failures"), list) else []
        if rid_key:
            for entry in failures:
                if isinstance(entry, dict) and entry.get("batch") == batch and entry.get("receiptId") == rid_key:
                    return json.loads(json.dumps(entry))
        failure: dict[str, Any] = {
            "batch": batch,
            "by": actor,
            "at": _now_iso(),
            "stage": clean_stage,
            "reason": clean_reason,
        }
        if rid_key:
            failure["receiptId"] = rid_key
        if await_receipt:
            journal = Path(review_dir) / ".video-registration.json"
            if journal.exists():
                raise VideoError("上一份视频回执仍在提交，请重试")
            _write_manifest(journal, {"reviewId": manifest["reviewId"], "failure": failure, "awaitReceipt": True})
        else:
            _append_failure(video, failure)
            _write_manifest(manifest_path, manifest)
        return json.loads(json.dumps(failure))


def _check_int(value: Any, name: str, *, minimum: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise VideoError(f"回执定位 {name} 须为不小于 {minimum} 的整数")
    return value


def _check_number(value: Any, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(float(value)):
        raise VideoError(f"回执定位 {name} 须为有限数字")
    return float(value)


def clean_video_mark(value: Any, *, label: str = "回执画面标记") -> dict[str, Any]:
    if not isinstance(value, dict) or value.get("kind") not in ("point", "region"):
        raise VideoError(f"{label}须为归一化点或区域")
    kind = value["kind"]
    names = ("x", "y") if kind == "point" else ("x", "y", "width", "height")
    if set(value) != {"kind", *names}:
        raise VideoError(f"{label}含无法识别的字段")
    mark = {"kind": kind, **{name: _check_number(value[name], f"mark.{name}") for name in names}}
    if any(mark[name] < 0 or mark[name] > 1 for name in ("x", "y")):
        raise VideoError(f"{label}坐标须在 0 至 1 之间")
    if kind == "region":
        if mark["width"] <= 0 or mark["height"] <= 0:
            raise VideoError(f"{label}区域宽高须大于 0")
        if mark["x"] + mark["width"] > 1 or mark["y"] + mark["height"] > 1:
            raise VideoError(f"{label}区域不能超出实际视频画面")
    return mark


def version_timebase(version_meta: dict[str, Any]) -> Fraction:
    """读取并校验单个视频版本的精确帧时间基准（秒/帧）。"""
    if not isinstance(version_meta, dict):
        raise VideoError("缺少目标版本元数据")
    tb = version_meta.get("timebase")
    if not isinstance(tb, dict) or set(tb) != {"num", "den"}:
        raise VideoError("目标版本缺少有效 timebase")
    num = _check_int(tb.get("num"), "version.timebase.num", minimum=1)
    den = _check_int(tb.get("den"), "version.timebase.den", minimum=1)
    return Fraction(num, den)


def frame_to_seconds(frame: int, version_meta: dict[str, Any]) -> float:
    """按版本自身的 timebase 将帧号换算为精确秒数。"""
    tb = version_timebase(version_meta)
    idx = _check_int(frame, "frame", minimum=0)
    if version_meta.get("frames") is not None:
        frames = _check_int(version_meta["frames"], "version.frames", minimum=1)
        if idx > frames:
            raise VideoError(f"帧号 {idx} 超出版本帧数范围 [0, {frames}]")
    return float(Fraction(idx) * tb)


def seconds_to_frame(seconds: float, version_meta: dict[str, Any], *, clamp: bool = True) -> int:
    """按版本自身的 timebase 将秒数换算为帧号；保留 IEEE-754 边界容差。"""
    tb = version_timebase(version_meta)
    sec = _check_number(seconds, "seconds")
    if sec < 0:
        if not clamp:
            raise VideoError("秒数不能为负")
        sec = 0.0
    idx = math.floor(sec * tb.denominator / tb.numerator + 1e-9)
    if version_meta.get("frames") is not None:
        frames = _check_int(version_meta["frames"], "version.frames", minimum=1)
        if idx >= frames:
            if not clamp:
                raise VideoError(f"时间 {sec}s 超出版本帧数范围 [0, {frames})")
            return frames - 1
    return idx


def cut_context_window(
    target: dict[str, Any], version_meta: dict[str, Any], *, pad_seconds: float = 1.0
) -> tuple[int, int]:
    """返回剪口定位的可播放上下文区间 [start, end)；未显式给出时按版本帧率截取有界窗口。"""
    if not isinstance(target, dict):
        raise VideoError("回执位置须为对象")
    tb = version_timebase(version_meta)
    frames = _check_int(version_meta.get("frames"), "version.frames", minimum=1)
    cut_frame = _check_int(target.get("frame"), "frame")
    if cut_frame > frames:
        raise VideoError(f"回执剪口 {cut_frame} 超出帧数范围 [0, {frames}]")
    has_cs = "contextStart" in target and target["contextStart"] is not None
    has_ce = "contextEnd" in target and target["contextEnd"] is not None
    if has_cs != has_ce:
        raise VideoError("剪口上下文须同时提供 contextStart 与 contextEnd")
    if has_cs:
        cs = _check_int(target.get("contextStart"), "contextStart")
        ce = _check_int(target.get("contextEnd"), "contextEnd")
        # 半开区间须覆盖剪口前一帧和后一帧。片头、片尾各只有一侧在片内。
        if cs > max(0, cut_frame - 1) or not min(frames, cut_frame + 1) <= ce <= frames:
            raise VideoError(f"剪口上下文 [{cs}, {ce}) 须包含剪口 {cut_frame} 两侧在片内的帧")
        return cs, ce
    pad_sec = _check_number(pad_seconds, "padSeconds")
    if pad_sec <= 0:
        raise VideoError("剪口上下文时长须大于 0")
    pad = max(1, round(pad_sec * tb.denominator / tb.numerator))
    cs = max(0, min(frames - 1, cut_frame - pad))
    ce = min(frames, max(cs + 1, cut_frame + pad))
    return cs, ce


def temporal_window(
    location: dict[str, Any], version_meta: dict[str, Any], *, pad_seconds: float = 1.0
) -> dict[str, Any]:
    """按目标版本时间基准解析帧、区间或剪口及其可播放时间窗。"""
    if not isinstance(location, dict):
        raise VideoError("视频定位须为对象")
    tb = version_timebase(version_meta)
    frames = _check_int(version_meta.get("frames"), "version.frames", minimum=1)
    kind = location.get("kind")
    if kind is None:
        if "frame" in location and "startFrame" not in location and "endFrame" not in location:
            kind = "frame"
        elif "startFrame" in location and "endFrame" in location and "frame" not in location:
            kind = "range"
    if kind not in RECEIPT_TARGET_KINDS:
        raise VideoError(f"回执位置 kind 须为 {'/'.join(RECEIPT_TARGET_KINDS)} 之一")
    base: dict[str, Any] = {
        "kind": kind,
        "versionId": version_meta.get("versionId"),
        "fps": float(1 / tb),
        "timebase": {"num": tb.numerator, "den": tb.denominator},
    }
    if kind == "frame":
        frame = _check_int(location.get("frame"), "frame")
        if frame >= frames:
            raise VideoError(f"回执帧 {frame} 超出帧数范围 [0, {frames})")
        return {**base, "frame": frame, "time": frame_to_seconds(frame, version_meta)}
    if kind == "range":
        start = _check_int(location.get("startFrame"), "startFrame")
        end = _check_int(location.get("endFrame"), "endFrame")
        if start >= end or end > frames:
            raise VideoError(f"回执区间 [{start}, {end}) 超出帧数范围 [0, {frames}]")
        return {
            **base,
            "startFrame": start,
            "endFrame": end,
            "startTime": frame_to_seconds(start, version_meta),
            "endTime": frame_to_seconds(end, version_meta),
        }
    frame = _check_int(location.get("frame"), "frame")
    cs, ce = cut_context_window(location, version_meta, pad_seconds=pad_seconds)
    return {
        **base,
        "frame": frame,
        "time": frame_to_seconds(frame, version_meta),
        "contextStart": cs,
        "contextEnd": ce,
        "contextStartTime": frame_to_seconds(cs, version_meta),
        "contextEndTime": frame_to_seconds(ce, version_meta),
    }


def clean_receipt_target(raw_target: Any, version_meta: dict[str, Any]) -> dict[str, Any]:
    """校验并规范化新版回执定位（frame / range / cut），越出成片范围一律拒绝。"""
    if not isinstance(raw_target, dict):
        raise VideoError("回执位置须为对象")
    if not isinstance(version_meta, dict):
        raise VideoError("缺少目标版本元数据")
    version_id = version_meta.get("versionId")
    if not isinstance(version_id, str) or not VERSION_ID.fullmatch(version_id):
        raise VideoError("目标版本标识无效")
    if "versionId" in raw_target and raw_target["versionId"] is not None and raw_target["versionId"] != version_id:
        raise VideoError(f"回执位置 versionId（{raw_target['versionId']}）与目标版本 {version_id} 不符")

    frames = _check_int(version_meta.get("frames"), "version.frames", minimum=1)
    width = _check_int(version_meta.get("width", version_meta.get("videoWidth")), "version.width", minimum=1)
    height = _check_int(version_meta.get("height", version_meta.get("videoHeight")), "version.height", minimum=1)
    fps = _check_number(version_meta.get("fps"), "version.fps")
    if fps <= 0:
        raise VideoError("目标版本 fps 须大于 0")
    tb = version_timebase(version_meta)
    num, den = tb.numerator, tb.denominator

    kind = raw_target.get("kind")
    if kind is None:
        if "frame" in raw_target and "startFrame" not in raw_target and "endFrame" not in raw_target:
            kind = "frame"
        elif "startFrame" in raw_target and "endFrame" in raw_target and "frame" not in raw_target:
            kind = "range"
    if kind not in RECEIPT_TARGET_KINDS:
        raise VideoError(f"回执位置 kind 须为 {'/'.join(RECEIPT_TARGET_KINDS)} 之一")

    allowed_by_kind = {
        "frame": {"v", "kind", "versionId", "fps", "timebase", "frames", "videoWidth", "videoHeight", "frame", "mark"},
        "range": {
            "v",
            "kind",
            "versionId",
            "fps",
            "timebase",
            "frames",
            "videoWidth",
            "videoHeight",
            "startFrame",
            "endFrame",
            "mark",
        },
        "cut": {
            "v",
            "kind",
            "versionId",
            "fps",
            "timebase",
            "frames",
            "videoWidth",
            "videoHeight",
            "frame",
            "contextStart",
            "contextEnd",
            "mark",
        },
    }
    unknown = set(raw_target) - allowed_by_kind[kind]
    if unknown:
        raise VideoError(f"回执位置含不支持的字段：{', '.join(sorted(unknown))}")

    out: dict[str, Any] = {
        "v": 1,
        "kind": kind,
        "versionId": version_id,
        "fps": fps,
        "timebase": {"num": num, "den": den},
        "frames": frames,
        "videoWidth": width,
        "videoHeight": height,
    }
    if kind == "frame":
        frame = _check_int(raw_target.get("frame"), "frame")
        if frame >= frames:
            raise VideoError(f"回执帧 {frame} 超出 {version_id} 帧数范围 [0, {frames})")
        out["frame"] = frame
    elif kind == "range":
        start = _check_int(raw_target.get("startFrame"), "startFrame")
        end = _check_int(raw_target.get("endFrame"), "endFrame")
        if start >= end or end > frames:
            raise VideoError(f"回执区间 [{start}, {end}) 超出 {version_id} 帧数范围 [0, {frames}]")
        out["startFrame"] = start
        out["endFrame"] = end
    else:
        start, end = cut_context_window(raw_target, version_meta)
        out["frame"] = raw_target["frame"]
        if raw_target.get("contextStart") is not None:
            out.update(contextStart=start, contextEnd=end)
    if raw_target.get("mark") is not None:
        out["mark"] = clean_video_mark(raw_target["mark"])
    return out


@lru_cache(maxsize=64)
def _verified_local_media(path: Path, digest: str, identity: tuple[int, ...]) -> bool:
    # 每次检查重新 stat；文件身份、内容时间或大小变化时重新探测并核对登记摘要。
    probe(path)
    return file_digest(path) == digest


def target_covers_interval(note: dict, target: dict) -> bool:
    return "startFrame" not in (note.get("video") or {}) or target["kind"] in ("range", "cut")


def display_state(review: dict[str, Any]) -> str:
    """从本条人工决定派生显示标签，保留原回执的存储状态。"""
    if review.get("decision") in ("replied", "returned", "approved"):
        return review["decision"]
    return str(review.get("state") or "")


def review_readiness(
    note: dict[str, Any], manifest: dict[str, Any], *, review_dir: Path | None = None
) -> tuple[bool, str]:
    """判定视频批注是否满足人工通过条件；不完备或媒体不可读一律返回 (False, reason)。"""
    if not isinstance(note, dict):
        return False, "invalid-note"
    manifest = manifest if isinstance(manifest, dict) else {}
    vr = note.get("videoReview")
    is_video = isinstance(note.get("video"), dict) or isinstance(manifest.get("video"), dict)
    if vr is None:
        if is_video:
            return False, "missing-video-review"
        return True, "ok"
    if not isinstance(vr, dict):
        return False, "invalid-video-review"
    state = display_state(vr)
    if state != "pending_review":
        return False, str(state) if isinstance(state, str) and state else "not-pending-review"
    summary = vr.get("summary")
    if not isinstance(summary, str) or not summary.strip():
        return False, "missing-summary"
    version_id = vr.get("versionId")
    if not isinstance(version_id, str) or not VERSION_ID.fullmatch(version_id):
        return False, "unregistered-version"
    try:
        version_meta = registered_version(manifest, version_id)
    except VideoError:
        return False, "unregistered-version"
    if version_meta.get("playbackFailure"):
        return False, "media-unreadable"
    if not isinstance(vr.get("batch"), int) or version_meta.get("batch") != vr["batch"]:
        return False, "unregistered-version"
    target = vr.get("target")
    if not isinstance(target, dict) or vr.get("outcome") == "deleted" and target.get("kind") != "cut":
        return False, "missing-target"
    try:
        if not target_covers_interval(note, clean_receipt_target(target, version_meta)):
            return False, "invalid-target"
    except VideoError:
        return False, "invalid-target"
    base_dir = review_dir
    if base_dir is None and isinstance(manifest.get("reviewDir"), str) and manifest["reviewDir"]:
        base_dir = Path(manifest["reviewDir"])
    if base_dir is not None:
        try:
            original = (note.get("video") or {}).get("versionId")
            if not original:
                return False, "missing-target"
            for vid in {version_id, original}:
                cut = registered_version(manifest, vid)
                if cut.get("playbackFailure"):
                    return False, "media-unreadable"
                path = media_path(base_dir, manifest, vid)
                stat = path.stat()
                identity = (stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns)
                if not _verified_local_media(path, cut.get("sha256", ""), identity):
                    return False, "media-unreadable"
        except (OSError, VideoError):
            return False, "media-unreadable"
    return True, "ok"


def media_url(review_id: str, version_id: str) -> str:
    return f"/api/video/{review_id}/versions/{version_id}/media"


def _public_failure(failure: dict, locale: str | None) -> dict:
    stage = failure.get("stage")
    if stage not in ("render", "probe", "decode"):
        stage = "unknown"
    lang = i18n.resolve_locale(locale, system_languages=i18n.system_languages())
    return {
        **{k: failure[k] for k in ("batch", "at", "versionId") if failure.get(k) is not None},
        "stage": stage,
        "reason": i18n.translate(i18n.catalog(lang), "viewer.video.failure." + stage),
    }


def public_review_response(response: dict, video: dict, handoff: dict, locale: str) -> dict:
    """将实时批注、处理痕迹与批次里复制的引擎诊断投影为同一阶段摘要。"""
    diagnostics = {}
    authored = set()

    def remember(failure):
        if isinstance(failure, dict) and isinstance(failure.get("reason"), str):
            diagnostics[failure.get("batch"), failure["reason"]] = failure.get("stage") or "unknown"

    for failure in video.get("failures") or []:
        remember(failure)
    remember(video.get("lastFailure"))
    for version in video.get("versions") or []:
        remember(version.get("playbackFailure"))
    for batch in handoff.get("items") or []:
        remember(batch.get("videoFailure"))
        for receipt in batch.get("videoReceipts") or []:
            failure = receipt.get("failure")
            remember(failure)
            for item in receipt.get("items") or []:
                reason = item.get("failure")
                if isinstance(reason, str):
                    if failure and failure.get("reason") == reason.strip()[:2000]:
                        diagnostics[batch["seq"], reason] = failure.get("stage") or "unknown"
                    else:
                        authored.add((batch["seq"], item.get("noteId"), reason))

    summaries = {}

    def project_text(row, field, batch, note_id):
        if not isinstance(row, dict) or not isinstance(row.get(field), str):
            return
        text = row[field]
        stage = row.get("failureStage") or diagnostics.get((batch, text))
        if stage == "item" or (not stage and (batch, note_id, text) in authored):
            return
        if stage not in ("render", "probe", "decode"):
            stage = "unknown"
        if stage not in summaries:
            summaries[stage] = _public_failure({"stage": stage}, locale)["reason"]
        row[field] = summaries[stage]

    def project_note(note):
        if not isinstance(note, dict):
            return
        vr = note.get("videoReview") if isinstance(note.get("videoReview"), dict) else {}
        note_id = note.get("id")
        for row in [vr, *(vr.get("history") or []), vr.get("relocatedFrom")]:
            if isinstance(row, dict):
                project_text(row, "failure", row.get("batch", vr.get("batch")), note_id)
        for trail in note.get("trail") or []:
            if isinstance(trail, dict) and trail.get("action") == "failed":
                project_text(trail, "note", trail.get("batch"), note_id)

    def project_failure(row, key):
        failure = row.get(key)
        if isinstance(failure, dict):
            row[key] = {
                **{k: failure[k] for k in ("by", "receiptId") if k in failure},
                **_public_failure(failure, locale),
            }

    result = copy.deepcopy(response)
    notes = [*(result.get("notes") or []), *((result.get("doc") or {}).get("notes") or []), result.get("note")]
    for note in {id(note): note for note in notes}.values():
        project_note(note)
    state = result.get("handoff") or result
    # latest 和 history[0] 可以引用同一批次。每一份对象只投影一次。
    batches = {id(b): b for b in [state.get("latest"), *(state.get("history") or [])] if isinstance(b, dict)}
    for batch in batches.values():
        project_failure(batch, "videoFailure")
        for receipt in batch.get("videoReceipts") or []:
            project_failure(receipt, "failure")
            for item in receipt.get("items") or []:
                project_text(item, "failure", batch["seq"], item.get("noteId"))
    return result


def public_manifest(manifest: dict[str, Any], *, locale: str | None = None) -> dict[str, Any]:
    """浏览器只获得版本身份、时间基准与受控 URL，不获得任意本机读取路径。"""
    video = manifest.get("video")
    if not isinstance(video, dict) or not ledger.RID.fullmatch(str(manifest.get("reviewId", ""))):
        raise VideoError("这份审阅没有有效的视频或审阅标识")
    locale = locale or manifest.get("locale")
    versions = []
    for item in video.get("versions", []):
        version_id = item.get("versionId") if isinstance(item, dict) else None
        if not isinstance(version_id, str):
            continue
        versions.append(
            {
                key: value
                for key, value in item.items()
                if key
                in (
                    "versionId",
                    "sha256",
                    "width",
                    "height",
                    "duration",
                    "fps",
                    "timebase",
                    "frames",
                    "batch",
                    "at",
                    "by",
                )
            }
            | {"url": media_url(manifest["reviewId"], version_id)}
        )
        if isinstance(item.get("playbackFailure"), dict):
            versions[-1]["playbackFailure"] = _public_failure(item["playbackFailure"], locale)
    out: dict[str, Any] = {
        "formatVersion": video.get("formatVersion", FORMAT_VERSION),
        "currentVersionId": video.get("currentVersionId"),
        "versions": versions,
    }
    lf = video.get("lastFailure")
    if isinstance(lf, dict):
        out["lastFailure"] = _public_failure(lf, locale)
    return out
