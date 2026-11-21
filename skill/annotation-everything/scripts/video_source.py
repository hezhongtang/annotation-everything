"""视频审阅的源工程关联（#193）：人工关联、唯一匹配直绑、歧义待确认。

只定义数据结构与判定，不读取工程文件、不调用渲染器、不引入 Node 依赖。
引擎探测（从工程根找出 composition / scene 候选）与渲染由 #194 提供，接入点是
本模块的 `probe` 契约：输入工程根，输出候选列表（engine / root / entry / media），
候选交给 `link(..., candidates=...)` 后仍走这里的同一套人工确认流程。

关联记录存在 review.json 的 manifest["video"]["sourceProject"]：
    v / status（linked | pending）/ engine（remotion | hyperframes）/ root（本机工程根）/
    entry（composition 或 scene 入口）/ versionId（对准的原成片版本）/
    basis（匹配依据：工程侧成片特征或仅文件名）/ match（verified | confirmed）/
    differences（与原片的对照片段）/ candidates（多候选时的候选清单）。
原批注继续指向原片：本模块不触碰 notes.json。
"""

from __future__ import annotations

import math
import os
from datetime import datetime, timezone
from typing import Any

import video_review

SOURCE_VERSION = 1
ENGINES = {"remotion": "composition", "hyperframes": "scene"}
# 对照登记版本时按字段类型区分：帧数与画面尺寸要求一致；时长与帧率允许浮点误差。
EXACT_FIELDS = ("frames", "width", "height")
CLOSE_FIELDS = ("duration", "fps")
MEDIA_FIELDS = ("duration", "fps", "frames", "width", "height")


class SourceError(ValueError):
    """源工程关联的输入无效，或当前状态不允许这个操作。"""


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _clean_engine(engine: Any) -> str:
    if engine not in ENGINES:
        raise SourceError(f"engine 须为 {'/'.join(sorted(ENGINES))} 之一")
    return str(engine)


def _clean_root(root: Any) -> str:
    if not isinstance(root, str) or not root.strip() or "\0" in root:
        raise SourceError("root 须为本机工程根的绝对路径")
    path = os.path.expanduser(root.strip())
    if not os.path.isabs(path):
        raise SourceError("root 须为本机工程根的绝对路径")
    return os.path.normpath(path)


def _clean_entry(engine: str, entry: Any) -> dict[str, str]:
    if not isinstance(entry, dict):
        raise SourceError("entry 须为入口对象")
    kind = entry.get("kind", ENGINES[engine])
    identifier = entry.get("id")
    if kind != ENGINES[engine]:
        raise SourceError(f"{engine} 的入口须为 {ENGINES[engine]}")
    if not isinstance(identifier, str) or not identifier.strip() or len(identifier) > 200:
        raise SourceError("entry.id 须为非空入口标识")
    out = {"kind": kind, "id": identifier.strip()}
    label = entry.get("label")
    if isinstance(label, str) and label.strip():
        out["label"] = label.strip()[:200]
    return out


def _clean_media(media: Any) -> dict[str, Any] | None:
    """工程侧成片特征；提供哪些字段就核验哪些，至少一个才算可核验依据。"""
    if media is None:
        return None
    if not isinstance(media, dict):
        raise SourceError("media 须为工程侧成片特征对象")
    out: dict[str, Any] = {}
    for name in MEDIA_FIELDS:
        if name not in media:
            continue
        value = media[name]
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(float(value)):
            raise SourceError(f"media.{name} 须为有限数字")
        out[name] = value
    return out or None


def _clean_candidate(raw: Any, index: int) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise SourceError("candidates 每项须为候选对象")
    engine = _clean_engine(raw.get("engine"))
    out = {
        "id": str(raw.get("id") or f"c{index + 1}"),
        "engine": engine,
        "root": _clean_root(raw.get("root")),
        "entry": _clean_entry(engine, raw.get("entry")),
        "media": _clean_media(raw.get("media")),
    }
    if not out["id"] or len(out["id"]) > 64:
        raise SourceError("候选 id 须为 1 至 64 个字符")
    return out


def _differences(version: dict[str, Any], media: dict[str, Any] | None) -> list[dict[str, Any]]:
    """工程侧特征与登记成片的对照；没有可核验字段时不冒充匹配（返回空表由 basis 说明）。"""
    out: list[dict[str, Any]] = []
    if not media:
        return out
    for name in EXACT_FIELDS:
        if name in media and name in version and media[name] != version[name]:
            out.append({"field": name, "review": version[name], "project": media[name]})
    for name in CLOSE_FIELDS:
        if (
            name in media
            and name in version
            and not math.isclose(float(media[name]), float(version[name]), rel_tol=1e-6, abs_tol=1e-6)
        ):
            out.append({"field": name, "review": version[name], "project": media[name]})
    return out


def _source_project(video: dict[str, Any]) -> dict[str, Any]:
    sp = video.get("sourceProject") if isinstance(video, dict) else None
    if not isinstance(sp, dict):
        raise SourceError("这份视频审阅还没有源工程关联")
    return sp


def link(
    video: dict[str, Any],
    *,
    engine: str,
    root: str,
    entry: dict[str, Any],
    media: dict[str, Any] | None = None,
    filename: str | None = None,
    candidates: list[dict[str, Any]] | None = None,
    by: str = "page",
) -> dict[str, Any]:
    """记录一次用户明确的源工程关联。返回带 sourceProject 的新 video（原字典不改）。

    唯一确认（工程侧成片特征与登记版本完全一致、且没有多候选）时直接 linked/verified；
    有差异、只有文件名或给出多个候选时进入 pending，等审阅人 `confirm`。
    仅文件名相同不能证明匹配（filename 只入依据，不参与核验）。
    """
    engine = _clean_engine(engine)
    current = video["currentVersionId"] if isinstance(video, dict) else None
    if not current:
        raise SourceError("这份清单没有视频版本")
    version = video_review.registered_version({"video": video}, str(current))
    clean_media = _clean_media(media)
    clean_candidates = [_clean_candidate(c, i) for i, c in enumerate(candidates or [])]
    basis: dict[str, Any] = {"kind": "media-features" if clean_media else "filename-only"}
    if clean_media is not None:
        basis["media"] = clean_media
    if isinstance(filename, str) and filename.strip():
        basis["filename"] = filename.strip()[:200]
    differences = _differences(version, clean_media)
    unique = clean_media is not None and not differences and len(clean_candidates) <= 1
    sp: dict[str, Any] = {
        "v": SOURCE_VERSION,
        "status": "linked" if unique else "pending",
        "match": "verified" if unique else None,
        "engine": engine,
        "root": _clean_root(root),
        "entry": _clean_entry(engine, entry),
        "versionId": version["versionId"],
        "basis": basis,
        "differences": differences,
        "candidates": clean_candidates,
        **({"linkedAt": _now_iso(), "linkedBy": by} if unique else {"proposedAt": _now_iso(), "proposedBy": by}),
    }
    out = dict(video)
    out["sourceProject"] = sp
    return out


def confirm(video: dict[str, Any], *, choice: str | None = None, by: str = "page") -> dict[str, Any]:
    """审阅人确认待定关联：多候选必须指明 choice，差异确认不指明。"""
    sp = _source_project(video)
    if sp.get("status") != "pending":
        raise SourceError("只有待确认的关联需要确认")
    out = dict(video)
    new_sp = dict(sp)
    candidates = [c for c in sp.get("candidates") or [] if isinstance(c, dict)]
    if len(candidates) > 1:
        if not choice:
            raise SourceError("多个候选须指明确认哪一个")
        picked = next((c for c in candidates if c.get("id") == choice), None)
        if picked is None:
            raise SourceError(f"没有这个候选：{choice}")
        new_sp.update(engine=picked["engine"], root=picked["root"], entry=picked["entry"], confirmedChoice=picked["id"])
        if picked.get("media") is not None:
            basis = dict(new_sp.get("basis") or {})
            basis.update(kind="media-features", media=picked["media"])
            new_sp["basis"] = basis
            version = video_review.registered_version(
                {"video": out}, str(new_sp.get("versionId") or out.get("currentVersionId"))
            )
            new_sp["differences"] = _differences(version, picked["media"])
    elif choice:
        raise SourceError("这个关联没有候选可选，直接确认即可")
    new_sp.update(status="linked", match="confirmed", confirmedAt=_now_iso(), confirmedBy=by)
    out["sourceProject"] = new_sp
    return out


def linked(video: dict[str, Any]) -> bool:
    sp = video.get("sourceProject") if isinstance(video, dict) else None
    return isinstance(sp, dict) and sp.get("status") == "linked"


def public(video: dict[str, Any]) -> dict[str, Any] | None:
    """页面与状态输出用的摘要；manifest 里没有关联时返回 None。"""
    sp = video.get("sourceProject") if isinstance(video, dict) else None
    if not isinstance(sp, dict):
        return None
    keys = (
        "v",
        "status",
        "match",
        "engine",
        "root",
        "entry",
        "versionId",
        "basis",
        "differences",
        "candidates",
        "linkedAt",
        "proposedAt",
        "confirmedAt",
        "confirmedChoice",
    )
    return {k: sp[k] for k in keys if sp.get(k) is not None}


def agent_brief(video: dict[str, Any]) -> dict[str, Any] | None:
    """领取负载里的源工程信息：Agent 改稿要对准的引擎、工程根、入口与原成片版本。"""
    if not linked(video):
        return None
    sp = video["sourceProject"]
    return {
        "engine": sp.get("engine"),
        "root": sp.get("root"),
        "entry": sp.get("entry"),
        "versionId": sp.get("versionId"),
    }
