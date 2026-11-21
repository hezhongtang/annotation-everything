"""本地 MP4 审阅：媒体元数据、V1 私有副本与受控路径解析。

只用 Python 标准库。这里不调用编解码器；构建时读取 ISO BMFF 中播放所必需的
时长、恒定帧率和实际画面尺寸，浏览器负责判断本机能否解码。
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import struct
from fractions import Fraction
from pathlib import Path, PurePosixPath
from typing import Any, Iterator

FORMAT_VERSION = 1
VERSION_ID = re.compile(r"^V[1-9][0-9]*$")
CONTAINERS = {b"moov", b"trak", b"mdia", b"minf", b"stbl"}


class VideoError(ValueError):
    """MP4 无法识别、版本未登记或私有媒体路径不安全。"""


def is_mp4(path: Path) -> bool:
    return path.suffix.lower() == ".mp4"


def _boxes(data: bytes, start: int = 0, end: int | None = None) -> Iterator[tuple[bytes, int, int]]:
    end = len(data) if end is None else end
    pos = start
    while pos < end:
        if end - pos < 8:
            raise VideoError("MP4 不完整：媒体盒头被截断")
        size, kind = struct.unpack_from(">I4s", data, pos)
        header = 8
        if size == 1:
            if end - pos < 16:
                raise VideoError("MP4 不完整：扩展媒体盒头被截断")
            size = struct.unpack_from(">Q", data, pos + 8)[0]
            header = 16
        elif size == 0:
            size = end - pos
        if size < header or pos + size > end:
            raise VideoError("MP4 不完整：媒体盒长度无效")
        yield kind, pos + header, pos + size
        pos += size


def _children(data: bytes, start: int, end: int) -> Iterator[tuple[bytes, int, int]]:
    yield from _boxes(data, start, end)


def _full_payload(data: bytes, start: int, end: int) -> bytes:
    if end - start < 4:
        raise VideoError("MP4 元数据不完整")
    return data[start + 4 : end]


def _media_time(data: bytes, start: int, end: int, kind: bytes) -> tuple[int, int]:
    payload = _full_payload(data, start, end)
    version = data[start]
    if version == 0:
        if len(payload) < 16:
            raise VideoError("MP4 时长元数据不完整")
        scale, duration = struct.unpack_from(">II", payload, 8)
    elif version == 1:
        if len(payload) < 28:
            raise VideoError("MP4 时长元数据不完整")
        scale, duration = struct.unpack_from(">IQ", payload, 16)
    else:
        raise VideoError(f"MP4 的 {kind.decode()} 版本无法识别")
    if not scale or not duration:
        raise VideoError("MP4 缺少有效的媒体时长")
    return scale, duration


def _video_track(data: bytes, start: int, end: int) -> dict[str, Any]:
    scale = duration = None
    frames = delta = width = height = None
    variable = False
    pending = list(_children(data, start, end))
    while pending:
        kind, child_start, child_end = pending.pop(0)
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
                    raise VideoError("MP4 画面尺寸元数据不完整")
                width, height = struct.unpack_from(">HH", data, sample_start + 24)
        elif kind == b"stts":
            payload = _full_payload(data, child_start, child_end)
            if len(payload) < 4:
                continue
            count = struct.unpack_from(">I", payload, 0)[0]
            if len(payload) < 4 + count * 8:
                raise VideoError("MP4 帧时间表不完整")
            frames = 0
            deltas = set()
            for index in range(count):
                samples, sample_delta = struct.unpack_from(">II", payload, 4 + index * 8)
                if not samples or not sample_delta:
                    raise VideoError("MP4 帧时间表包含空样本")
                frames += samples
                deltas.add(sample_delta)
            variable = len(deltas) != 1
            delta = deltas.pop() if not variable else None
    if not all(isinstance(value, int) and value > 0 for value in (scale, duration, frames, width, height)):
        raise VideoError("MP4 缺少可批注的视频轨、画面尺寸或帧时间表")
    if variable or not delta:
        raise VideoError("MP4 使用可变帧时长，首版只支持恒定帧率视频")
    frame_clock = Fraction(delta, scale)
    fps = 1 / frame_clock
    seconds = Fraction(duration, scale)
    return {
        "width": width,
        "height": height,
        "duration": float(seconds),
        "fps": float(fps),
        "timebase": {"num": frame_clock.numerator, "den": frame_clock.denominator},
        "frames": frames,
    }


def probe(path: Path) -> dict[str, Any]:
    """读取 MP4 元数据；无法确认精确帧定位时明确拒绝，不猜测帧率。"""
    try:
        data = path.read_bytes()
    except OSError as exc:
        raise VideoError(f"无法读取 MP4：{exc}") from exc
    if not data:
        raise VideoError("MP4 文件为空")
    try:
        top = list(_boxes(data))
    except VideoError:
        raise
    if not any(kind == b"ftyp" for kind, _start, _end in top):
        raise VideoError("不是可识别的 MP4：缺少 ftyp 媒体盒")
    moov = next(((start, end) for kind, start, end in top if kind == b"moov"), None)
    if moov is None:
        raise VideoError("不是可识别的 MP4：缺少 moov 元数据盒")

    def walk(start: int, end: int) -> Iterator[tuple[bytes, int, int]]:
        for kind, child_start, child_end in _boxes(data, start, end):
            yield kind, child_start, child_end
            if kind in CONTAINERS:
                yield from walk(child_start, child_end)

    tracks = []
    for kind, start, end in walk(*moov):
        if kind != b"trak":
            continue
        descendants = list(walk(start, end))
        if any(k == b"hdlr" and data[s + 8 : s + 12] == b"vide" for k, s, _e in descendants if _e - s >= 12):
            tracks.append((start, end))
    if len(tracks) != 1:
        raise VideoError("MP4 必须恰好包含一条可批注的视频轨")
    return _video_track(data, *tracks[0])


def registered_version(manifest: dict[str, Any], version_id: str) -> dict[str, Any]:
    video = manifest.get("video") if isinstance(manifest, dict) else None
    versions = video.get("versions") if isinstance(video, dict) else None
    if not VERSION_ID.fullmatch(version_id) or not isinstance(versions, list):
        raise VideoError("视频版本未登记")
    for version in versions:
        if isinstance(version, dict) and version.get("versionId") == version_id:
            return version
    raise VideoError("视频版本未登记")


def media_path(review_dir: Path, manifest: dict[str, Any], version_id: str) -> Path:
    """只解析清单内的私有相对路径；拒绝绝对路径、穿越、符号链接和跨目录读取。"""
    version = registered_version(manifest, version_id)
    raw = version.get("path")
    if not isinstance(raw, str) or not raw or "\\" in raw or "\0" in raw:
        raise VideoError("视频版本的私有路径无效")
    pure = PurePosixPath(raw)
    if pure.is_absolute() or ".." in pure.parts or not pure.parts or pure.parts[0] != "media":
        raise VideoError("视频版本的私有路径无效")
    root = (review_dir / "media").resolve()
    candidate = review_dir.joinpath(*pure.parts)
    current = review_dir
    for part in pure.parts:
        current = current / part
        if current.is_symlink():
            raise VideoError("视频版本的私有路径不能是符号链接")
    try:
        resolved = candidate.resolve(strict=True)
        relative = resolved.relative_to(root)
    except (OSError, ValueError) as exc:
        raise VideoError("视频版本的媒体不存在或不在本审阅内") from exc
    if relative.as_posix() != PurePosixPath(*pure.parts[1:]).as_posix() or not resolved.is_file():
        raise VideoError("视频版本的私有路径无效")
    return resolved


def prepare(source: Path, review_dir: Path, existing: dict[str, Any] | None = None) -> dict[str, Any]:
    """登记 V1。重建时从私有副本读取，绝不回头采用可能已被覆盖的原文件。"""
    existing = existing or {}
    if existing.get("format", {}).get("report") == "video":
        version = registered_version(existing, str(existing.get("video", {}).get("currentVersionId") or "V1"))
        private = media_path(review_dir, existing, version["versionId"])
        if source.resolve() != private:
            raise VideoError("这份审阅已经登记 V1；重建须使用审阅内已登记的媒体")
        digest = hashlib.sha256(private.read_bytes()).hexdigest()
        if digest != version.get("sha256"):
            raise VideoError("审阅内 V1 的 SHA-256 与清单不符；请恢复原始证据后再重建")
        return json.loads(json.dumps(existing["video"]))

    review_dir.mkdir(parents=True, exist_ok=True)
    media_dir = review_dir / "media"
    media_dir.mkdir(exist_ok=True)
    target = media_dir / "v1.mp4"
    if target.exists():
        raise VideoError("media/v1.mp4 已存在但未登记；拒绝覆盖本机材料")
    temporary = media_dir / f".v1-{os.getpid()}.tmp"
    digest = hashlib.sha256()
    try:
        with source.open("rb") as src, temporary.open("xb") as dst:
            os.chmod(temporary, 0o600)
            while chunk := src.read(1024 * 1024):
                digest.update(chunk)
                dst.write(chunk)
            dst.flush()
            os.fsync(dst.fileno())
        metadata = probe(temporary)
        version = {
            "versionId": "V1",
            "path": "media/v1.mp4",
            "sha256": digest.hexdigest(),
            **metadata,
        }
        os.replace(temporary, target)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass
    return {"formatVersion": FORMAT_VERSION, "currentVersionId": "V1", "versions": [version]}


def public_manifest(manifest: dict[str, Any]) -> dict[str, Any]:
    """浏览器只获得版本身份、时间基准与受控 URL，不获得任意本机读取路径。"""
    video = manifest.get("video")
    if not isinstance(video, dict):
        raise VideoError("这份审阅没有视频")
    versions = []
    for item in video.get("versions", []):
        version_id = item.get("versionId") if isinstance(item, dict) else None
        if not isinstance(version_id, str):
            continue
        versions.append(
            {
                key: value
                for key, value in item.items()
                if key in ("versionId", "sha256", "width", "height", "duration", "fps", "timebase", "frames")
            }
            | {"url": f"/api/video/versions/{version_id}/media"}
        )
    return {
        "formatVersion": video.get("formatVersion", FORMAT_VERSION),
        "currentVersionId": video.get("currentVersionId"),
        "versions": versions,
    }
