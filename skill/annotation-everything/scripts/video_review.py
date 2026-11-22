"""本地 MP4 审阅：媒体元数据、V1 私有副本与受控路径解析。

只用 Python 标准库。这里不调用编解码器；构建时读取 ISO BMFF 中播放所必需的
时长、恒定帧率和实际画面尺寸，浏览器负责判断本机能否解码。
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import struct
import sys
from datetime import datetime, timezone
from fractions import Fraction
from pathlib import Path, PurePosixPath
from typing import Any, Iterator

sys.path.insert(0, str(Path(__file__).resolve().parent))
import fsutil  # noqa: E402
import ledger  # noqa: E402

FORMAT_VERSION = 1
VERSION_ID = re.compile(r"^V[1-9][0-9]*$")
CONTAINERS = {b"moov", b"trak", b"mdia", b"minf", b"stbl"}
RECEIPT_TARGET_KINDS = ("frame", "range", "cut")
FAILURES_KEEP = 50
MANIFEST_FILE = "review.json"


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


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _stage_and_probe_mp4(source: Path, review_dir: Path, version_num: int) -> dict[str, Any]:
    """将源成片拷入私有临时文件（0600 + fsync）并做 MP4 探测，通过后再原子替换为 media/v{N}.mp4。"""
    review_dir.mkdir(parents=True, exist_ok=True)
    media_dir = review_dir / "media"
    media_dir.mkdir(exist_ok=True)
    rel_path = f"media/v{version_num}.mp4"
    target = media_dir / f"v{version_num}.mp4"
    if target.exists() or target.is_symlink():
        raise VideoError(f"{rel_path} 已存在但未登记；拒绝覆盖本机材料")
    temporary = media_dir / f".v{version_num}-{os.getpid()}.tmp"
    if temporary.exists() and not temporary.is_symlink() and not temporary.is_dir():
        temporary.unlink()
    digest = hashlib.sha256()
    try:
        try:
            with source.open("rb") as src, temporary.open("xb") as dst:
                os.chmod(temporary, 0o600)
                while chunk := src.read(1024 * 1024):
                    digest.update(chunk)
                    dst.write(chunk)
                dst.flush()
                os.fsync(dst.fileno())
        except OSError as exc:
            raise VideoError(f"无法读取或暂存 MP4：{exc}") from exc
        metadata = probe(temporary)
        version = {
            "versionId": f"V{version_num}",
            "path": rel_path,
            "sha256": digest.hexdigest(),
            **metadata,
        }
        os.replace(temporary, target)
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
        raise VideoError(f"无法读取审阅清单：{exc}") from exc
    if not isinstance(manifest, dict) or manifest.get("format", {}).get("report") != "video":
        raise VideoError("这份审阅不是视频审阅")
    if review_id is not None and manifest.get("reviewId") != review_id:
        raise VideoError("审阅标识不匹配，拒绝写入其他审阅")
    video = manifest.get("video")
    if not isinstance(video, dict) or not isinstance(video.get("versions"), list):
        raise VideoError("这份清单缺少有效视频版本记录")
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

    version = _stage_and_probe_mp4(source, review_dir, 1)
    return {"formatVersion": FORMAT_VERSION, "currentVersionId": "V1", "versions": [version]}


def register_version(
    review_dir: Path,
    source_mp4: Path,
    *,
    batch: int,
    by: str,
    review_id: str | None = None,
    receipt_id: str | None = None,
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
        if rid_key:
            for item in video["versions"]:
                if isinstance(item, dict) and item.get("batch") == batch and item.get("receiptId") == rid_key:
                    return json.loads(json.dumps(item))

        version_num = _next_version_number(video)
        version = _stage_and_probe_mp4(Path(source_mp4), Path(review_dir), version_num)
        version.update(batch=batch, by=actor, at=_now_iso())
        if rid_key:
            version["receiptId"] = rid_key
        video["versions"].append(version)
        video["currentVersionId"] = version["versionId"]
        video.pop("lastFailure", None)
        _write_manifest(manifest_path, manifest)
        return json.loads(json.dumps(version))


def record_failure(
    review_dir: Path,
    *,
    batch: int,
    by: str,
    reason: str,
    review_id: str | None = None,
    receipt_id: str | None = None,
    stage: str = "render",
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
        failures.append(failure)
        video["failures"] = failures[-FAILURES_KEEP:]
        video["lastFailure"] = failure
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


def clean_video_mark(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict) or value.get("kind") not in ("point", "region"):
        raise VideoError("回执画面标记须为归一化点或区域")
    kind = value["kind"]
    names = ("x", "y") if kind == "point" else ("x", "y", "width", "height")
    if set(value) != {"kind", *names}:
        raise VideoError("回执画面标记含无法识别的字段")
    mark = {"kind": kind, **{name: _check_number(value[name], f"mark.{name}") for name in names}}
    if any(mark[name] < 0 or mark[name] > 1 for name in ("x", "y")):
        raise VideoError("回执画面标记坐标须在 0 至 1 之间")
    if kind == "region":
        if mark["width"] <= 0 or mark["height"] <= 0:
            raise VideoError("回执画面标记区域宽高须大于 0")
        if mark["x"] + mark["width"] > 1 or mark["y"] + mark["height"] > 1:
            raise VideoError("回执画面标记区域不能超出实际视频画面")
    return mark


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
    tb = version_meta.get("timebase")
    if not isinstance(tb, dict) or set(tb) != {"num", "den"}:
        raise VideoError("目标版本缺少有效 timebase")
    num = _check_int(tb.get("num"), "version.timebase.num", minimum=1)
    den = _check_int(tb.get("den"), "version.timebase.den", minimum=1)

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
        frame = _check_int(raw_target.get("frame"), "frame")
        if frame > frames:
            raise VideoError(f"回执剪口 {frame} 超出 {version_id} 帧数范围 [0, {frames}]")
        out["frame"] = frame
        has_cs = "contextStart" in raw_target and raw_target["contextStart"] is not None
        has_ce = "contextEnd" in raw_target and raw_target["contextEnd"] is not None
        if has_cs != has_ce:
            raise VideoError("剪口上下文须同时提供 contextStart 与 contextEnd")
        if has_cs:
            cs = _check_int(raw_target.get("contextStart"), "contextStart")
            ce = _check_int(raw_target.get("contextEnd"), "contextEnd")
            if cs >= ce or ce > frames or not (cs <= frame <= ce):
                raise VideoError(f"剪口上下文 [{cs}, {ce}) 无效或未包含剪口 {frame}")
            out["contextStart"] = cs
            out["contextEnd"] = ce
    if raw_target.get("mark") is not None:
        out["mark"] = clean_video_mark(raw_target["mark"])
    return out


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
        if note.get("status") == "drafted" and is_video:
            return False, "missing-video-review"
        return True, "ok"
    if not isinstance(vr, dict):
        return False, "invalid-video-review"
    state = vr.get("state")
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
    target = vr.get("target")
    if not isinstance(target, dict):
        return False, "missing-target"
    try:
        clean_receipt_target(target, version_meta)
    except VideoError:
        return False, "invalid-target"
    base_dir = review_dir
    if base_dir is None and isinstance(manifest.get("reviewDir"), str) and manifest["reviewDir"]:
        base_dir = Path(manifest["reviewDir"])
    if base_dir is not None:
        try:
            path = media_path(base_dir, manifest, version_id)
            with path.open("rb") as fh:
                if len(fh.read(8)) < 8:
                    return False, "media-unreadable"
        except (OSError, VideoError):
            return False, "media-unreadable"
    return True, "ok"


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
            | {"url": f"/api/video/versions/{version_id}/media"}
        )
    out: dict[str, Any] = {
        "formatVersion": video.get("formatVersion", FORMAT_VERSION),
        "currentVersionId": video.get("currentVersionId"),
        "versions": versions,
    }
    lf = video.get("lastFailure")
    if isinstance(lf, dict):
        out["lastFailure"] = {
            k: lf[k] for k in ("batch", "by", "at", "stage", "reason", "receiptId") if lf.get(k) is not None
        }
    return out
