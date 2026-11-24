"""视频审阅的源工程关联（#193）：人工关联、唯一匹配直绑、歧义待确认。

页面关联只静态读取工程资料和已生成的成片。Remotion 的动态入口由 CLI 探测，
候选与差异通过同一套确认流程写入清单。

关联记录存在 review.json 的 manifest["video"]["sourceProject"]：
    v / status（linked | pending）/ engine（remotion | hyperframes）/ root（本机工程根）/
    entry（composition 或 scene 入口）/ versionId（对准的原成片版本）/
    basis（匹配依据：工程侧成片特征或仅文件名）/ match（verified | confirmed）/
    differences（与原片的对照片段）/ candidates（多候选时的候选清单）。
原批注继续指向原片：本模块不触碰 notes.json。
"""

from __future__ import annotations

import hashlib
import json
import math
import fsutil
import os
import re
from pathlib import Path
from datetime import datetime, timezone
from typing import Any

import i18n
import video_engines
import video_review

SOURCE_VERSION = 1
ENGINES = {"remotion": "composition", "hyperframes": "scene"}
# 对照登记版本时按字段类型区分：帧数与画面尺寸要求一致；时长与帧率允许浮点误差。
EXACT_FIELDS = ("frames", "width", "height")
CLOSE_FIELDS = ("duration", "fps")
MEDIA_FIELDS = ("duration", "fps", "frames", "width", "height")


class SourceError(ValueError):
    def __init__(self, code: str, **params):
        self.code, self.params = code, params
        super().__init__(self.localized("zh-CN"))

    def localized(self, locale: str) -> str:
        catalog = i18n.catalog(locale)
        params = self.params
        if self.code == "entries":
            params = {"entries": i18n.translate(catalog, "build.source.listSep").join(params["entries"])}
        return i18n.translate(catalog, "video.source.error." + self.code, **params)


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _clean_engine(engine: Any) -> str:
    if not isinstance(engine, str) or engine not in ENGINES:
        raise SourceError("engine")
    return str(engine)


def _clean_root(root: Any) -> str:
    if not isinstance(root, str) or not root.strip() or "\0" in root:
        raise SourceError("root")
    path = os.path.expanduser(root.strip())
    if not os.path.isabs(path) or fsutil.is_unc_or_device_path(path):
        raise SourceError("root")
    return os.path.normpath(path)


def _clean_entry(engine: str, entry: Any) -> dict[str, str]:
    if not isinstance(entry, dict):
        raise SourceError("entry")
    kind = entry.get("kind", ENGINES[engine])
    identifier = entry.get("id")
    if kind != ENGINES[engine]:
        raise SourceError("kind", engine=engine, kind=ENGINES[engine])
    if not isinstance(identifier, str) or not identifier.strip() or len(identifier) > 200:
        raise SourceError("entryId")
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
        raise SourceError("media")
    out: dict[str, Any] = {}
    for name in MEDIA_FIELDS:
        if name not in media:
            continue
        value = media[name]
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(float(value)):
            raise SourceError("number", field=name)
        out[name] = value
    return out or None


def _clean_candidate(raw: Any, index: int) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise SourceError("candidate")
    engine = _clean_engine(raw.get("engine"))
    out = {
        "id": str(raw.get("id") or f"c{index + 1}"),
        "engine": engine,
        "root": _clean_root(raw.get("root")),
        "entry": _clean_entry(engine, raw.get("entry")),
        "media": _clean_media(raw.get("media")),
    }
    if not out["id"] or len(out["id"]) > 64:
        raise SourceError("candidateId")
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
        if name not in media or name not in version:
            continue
        project_val, review_val = float(media[name]), float(version[name])
        mismatch = (
            abs(project_val - review_val) >= video_engines.FPS_TOLERANCE
            if name == "fps"
            else not math.isclose(project_val, review_val, rel_tol=1e-3, abs_tol=1e-2)
        )
        if mismatch:
            out.append({"field": name, "review": version[name], "project": media[name]})
    return out


def _source_project(video: dict[str, Any]) -> dict[str, Any]:
    sp = video.get("sourceProject") if isinstance(video, dict) else None
    if not isinstance(sp, dict):
        raise SourceError("missing")
    return sp


def _compare_project_cuts(
    info: dict[str, Any], entry: dict[str, Any], version: dict[str, Any]
) -> tuple[dict[str, Any], list[str]]:
    movies = video_engines.scan_movies(info["root"])
    cuts = [
        {
            "name": movie["path"].relative_to(info["root"]).as_posix(),
            "media": _clean_media(movie["meta"]),
            **({"diagnostic": movie["diagnostic"]} if "diagnostic" in movie else {}),
            "differences": _differences(version, movie["meta"]),
        }
        for movie in movies
    ]
    comparison = {"review": _clean_media(version), "movies": cuts}
    reasons = []
    if not movies:
        return comparison, ["noMovie"]
    if len(movies) > 1:
        reasons.append("multipleMovies")
    if any(not cut["media"] for cut in cuts):
        reasons.append("unreadableMovie")
    if any(cut["differences"] for cut in cuts):
        reasons.append("mediaMismatch")
    if reasons:
        return comparison, reasons
    movie = movies[0]
    source_time = max((video_engines._mtime(p) for p in video_engines.freshness_sources(info)), default=0.0)
    if movie["mtime"] < source_time:
        return comparison, ["staleMovie"]
    try:
        digest = video_review.file_digest(movie["path"])
    except OSError:
        return comparison, ["unreadableMovie"]
    if digest != version["sha256"]:
        return comparison, ["contentMismatch"]
    try:
        if video_engines.known_render(info, entry, video_engines.source_fingerprint(info), digest):
            return comparison, []
    except (OSError, video_engines.EngineError):
        pass
    return comparison, ["missingReceipt"]


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
    verified_source: bool = False,
) -> dict[str, Any]:
    """记录一次用户明确的源工程关联。返回带 sourceProject 的新 video（原字典不改）。

    调用方已核实成片来源、声明相符且没有多个入口时直接 linked/verified；
    有差异、只有文件名或给出多个候选时进入 pending，等审阅人 `confirm`。
    仅文件名相同不能证明匹配（filename 只入依据，不参与核验）。
    """
    engine = _clean_engine(engine)
    current = video["currentVersionId"] if isinstance(video, dict) else None
    if not current:
        raise SourceError("version")
    version = video_review.registered_version({"video": video}, str(current))
    clean_media = _clean_media(media)
    clean_candidates = [_clean_candidate(c, i) for i, c in enumerate(candidates or [])]
    basis: dict[str, Any] = {"kind": "media-features" if clean_media else "filename-only"}
    if clean_media is not None:
        basis["media"] = clean_media
    if isinstance(filename, str) and filename.strip():
        basis["filename"] = filename.strip()[:200]
    differences = _differences(version, clean_media)
    unique = verified_source and not differences and len(clean_candidates) <= 1
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


def local_project_link(video: dict[str, Any], **payload) -> dict[str, Any]:
    """本地表单只读取工程数据，不执行工程代码。动态 Remotion 入口可由 CLI 巡检后人工确认。"""
    engine = _clean_engine(payload.get("engine"))
    root = Path(_clean_root(payload.get("root")))
    entry = _clean_entry(engine, payload.get("entry"))
    try:
        info = video_engines.detect_project(root)
    except video_engines.EngineError as exc:
        if exc.code == "configUnreadable":
            raise SourceError("configUnreadable", file=exc.detail) from exc
        raise SourceError("project", root=str(root)) from exc
    if engine != info["engine"]:
        raise SourceError("engineMismatch", engine=info["engine"])
    if engine == "hyperframes":
        declarations = info["compositions"]
        if entry["id"] not in {c["id"] for c in declarations}:
            raise SourceError("entries", entries=[c["id"] for c in declarations])
    else:
        # JSX 字面量仅作人工选择线索，动态生成的合成以工程 CLI 巡检为准。
        declarations = []
        for source in video_engines.freshness_sources(info):
            if source.suffix not in (".tsx", ".jsx") or source.is_symlink():
                continue
            try:
                if source.stat().st_size > 1024 * 1024:
                    continue
                text = source.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            for tag in re.finditer(r"<Composition\b[^>]*>", text):
                ident = re.search(r"""\bid\s*=\s*["']([^"']+)["']""", tag[0])
                if not ident:
                    continue
                comp = {"id": ident[1]}
                for name, attr in (
                    ("fps", "fps"),
                    ("width", "width"),
                    ("height", "height"),
                    ("frames", "durationInFrames"),
                ):
                    value = re.search(r"\b" + attr + r"\s*=\s*\{\s*(\d+(?:\.\d+)?)\s*\}", tag[0])
                    if value:
                        comp[name] = float(value[1])
                declarations.append(comp)
    discovered = [
        {
            "engine": engine,
            "root": str(root),
            "entry": {"kind": entry["kind"], "id": comp["id"]},
            "media": {
                k: comp[k]
                for k in MEDIA_FIELDS
                if comp.get(k) is not None and (k != "fps" or comp.get("fpsDeclared", True))
            },
        }
        for comp in declarations
    ]
    selected = next((c for c in discovered if c["entry"]["id"] == entry["id"]), None)
    if discovered and not selected:
        # 静态清单可能遗漏动态合成。把手填项与可见入口分开，确认时由审阅人明确选择。
        payload["candidates"] = [
            *discovered,
            {"engine": engine, "root": str(root), "entry": entry, "media": payload.get("media")},
        ]
    elif len(discovered) > 1:
        payload["candidates"] = discovered
    elif payload.get("candidates"):
        provided = [_clean_candidate(raw, i) for i, raw in enumerate(payload["candidates"])]
        if any(c["engine"] != engine or Path(c["root"]) != root for c in provided):
            raise SourceError("candidate")
        payload["candidates"] = provided
    version = video_review.registered_version({"video": video}, video["currentVersionId"])
    declared = selected["media"] if selected else {}
    if not payload.get("media") and declared:
        payload["media"] = declared
    comparison, reasons = _compare_project_cuts(info, entry, version)
    if not selected:
        reasons.append("entryNotListed")
    if engine == "hyperframes" and "fps" not in declared:
        fallback = video_engines.HYPERFRAMES_DEFAULT_FPS
        if _differences(version, {"fps": fallback}):
            reasons.append("defaultFpsMismatch")
            comparison["defaultFps"] = fallback
    verified = bool(len(discovered) == 1 and selected and not _differences(version, declared) and not reasons)
    updated = link(video, **payload, verified_source=verified)
    sp = updated["sourceProject"]
    if sp.get("match") == "verified":
        sp["basis"].update(kind="sha256", sha256=version["sha256"])
    else:
        sp["cutComparison"] = comparison
        sp["reasons"] = reasons
        for diff in _differences(version, declared):
            if diff not in sp["differences"]:
                sp["differences"].append(diff)
    return updated


def confirm(video: dict[str, Any], *, choice: str | None = None, by: str = "page") -> dict[str, Any]:
    """审阅人确认待定关联：多候选必须指明 choice，差异确认不指明。"""
    sp = _source_project(video)
    if sp.get("status") != "pending":
        raise SourceError("pending")
    out = dict(video)
    new_sp = dict(sp)
    candidates = [c for c in sp.get("candidates") or [] if isinstance(c, dict)]
    if len(candidates) > 1:
        if not choice:
            raise SourceError("choice")
        picked = next((c for c in candidates if c.get("id") == choice), None)
        if picked is None:
            raise SourceError("unknownChoice", choice=choice)
        new_sp.update(engine=picked["engine"], root=picked["root"], entry=picked["entry"], confirmedChoice=picked["id"])
        basis = dict(new_sp.get("basis") or {})
        if picked.get("media"):
            basis.update(kind="media-features", media=picked["media"])
            basis.pop("sha256", None)
            new_sp["basis"] = basis
            version = video_review.registered_version(
                {"video": out}, str(new_sp.get("versionId") or out.get("currentVersionId"))
            )
            new_sp["differences"] = _differences(version, picked["media"])
        else:
            basis.pop("media", None)
            basis.pop("sha256", None)
            basis["kind"] = "confirmed-candidate"
            new_sp["basis"] = basis
            new_sp["differences"] = []
    elif choice:
        raise SourceError("noChoice")
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
        "cutComparison",
        "reasons",
        "linkedAt",
        "proposedAt",
        "confirmedAt",
        "confirmedChoice",
    )
    result = {k: sp[k] for k in keys if sp.get(k) is not None}
    if sp.get("status") == "pending":
        proposal = json.dumps(sp, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        result["proposalRevision"] = hashlib.sha256(proposal.encode("utf-8")).hexdigest()
    return result


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
