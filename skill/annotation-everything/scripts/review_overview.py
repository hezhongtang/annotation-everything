"""审阅总览：复用台账行状态，仅公开适合页面显示的字段。"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Callable

import analysis_store
import build_viewer
import fsutil
import ledger
import notes_store as store
import report_check
import review_stop

STATE_ORDER = {"running": 0, "running-temp": 0, "stopped": 1, "missing": 3, "mismatch": 3}
ARCHIVE_FILE = ".annotation-archived"


def is_archived(review_dir: Path) -> bool:
    """目录标记即归档事实；状态轮询只 stat，不打开额外文件。"""
    return (Path(review_dir) / ARCHIVE_FILE).is_file()


def set_archived(review_dir: Path, archived: bool) -> None:
    """只改归档标记，台账、专属端口和审阅内容均不参与写入。"""
    marker = Path(review_dir) / ARCHIVE_FILE
    if archived:
        fsutil.atomic_write(marker, "", prefix=".archive-")
    else:
        marker.unlink(missing_ok=True)


def workspace_for(review_dir: Path) -> Path:
    """最近的 git 工作区；非 git 审阅以父目录为工作区。"""
    directory = Path(review_dir).resolve()
    for candidate in (directory, *directory.parents):
        if (candidate / ".git").exists():
            return candidate
    return directory.parent


def in_scope(directory: Path, workspace: Path, scope: str) -> bool:
    return scope == "all" or workspace_for(directory) == workspace


def display_path(directory: Path, workspace: Path) -> str:
    """工作区内显示相对路径；其他位置显示 ~ 路径或目录名。"""
    try:
        return directory.relative_to(workspace).as_posix()
    except ValueError:
        try:
            return "~/" + directory.relative_to(Path.home().resolve()).as_posix()
        except ValueError:
            return directory.name


def last_activity(directory: Path) -> float | None:
    times = []
    for name in (store.NOTES_FILE, store.HANDOFF_FILE):
        try:
            times.append((directory / name).stat().st_mtime)
        except OSError:
            pass
    return max(times) if times else None


def report_metadata(directory: Path, review_id: str) -> dict[str, Any]:
    """顺着报告来源读取可展示的核验信息；损坏记录不影响其他审阅。"""
    metadata: dict[str, Any] = {"summary": "", "purpose": "", "audience": "",
                                "coreQuestions": [], "coverTitle": ""}
    manifest = ledger.read_manifest(directory)
    if manifest.get("reviewId") != review_id:
        return metadata
    pages = manifest.get("pages")
    if isinstance(pages, list) and pages and isinstance(pages[0], dict):
        title = pages[0].get("title")
        if isinstance(title, str):
            metadata["coverTitle"] = title
    # 与构建入口相同：相对路径优先，兼容 0.1 的 sourcesRel / sources 组合。
    relative, absolute = ((manifest.get("sourcesRel"), manifest.get("sources"))
                          if "sourcesRel" in manifest else
                          (manifest.get("sources"), manifest.get("sourcesAbs")))
    report = None
    for base, sources in ((directory, relative), (Path(), absolute)):
        name = sources.get("report") if isinstance(sources, dict) else None
        if not isinstance(name, str) or not name:
            continue
        try:
            candidate = base / name
            if candidate.is_file():
                report = candidate.resolve()
                break
        except (OSError, ValueError, RuntimeError):
            continue
    if report is None:
        return metadata
    try:
        cover = build_viewer.find_pages(report.read_text(encoding="utf-8"), locale="en")
        if cover:
            metadata["coverTitle"] = cover[0]["title"]
    except (OSError, UnicodeError, SystemExit):
        pass
    record, valid = report_check.load_record(report.with_name("report-record.json"), report_check.Checks("en"))
    if not valid or record is None:
        return metadata
    request = record["request"]
    purpose, audience = request.get("purpose", ""), request.get("audience", "")
    questions = request.get("coreQuestions", [])
    if not isinstance(purpose, str) or not isinstance(audience, str) \
            or not isinstance(questions, list) or any(not isinstance(q, str) for q in questions):
        return metadata
    # JSON 能解码孤立 UTF-16 代理，但总览响应必须是有效 UTF-8。
    try:
        for text in (record.get("summary", ""), purpose, audience, *questions):
            text.encode("utf-8")
    except UnicodeEncodeError:
        return metadata
    metadata.update(summary=record.get("summary", ""), purpose=purpose,
                    audience=audience, coreQuestions=questions)
    return metadata


def snapshot(review_dir: Path, review_id: str | None, scope: str,
             row_builder: Callable[[str, dict[str, Any]], dict[str, Any]]) -> dict[str, Any]:
    """台账是索引，审阅名、计数和运行状态由 list 的同一份逻辑现读。"""
    workspace = workspace_for(review_dir)
    rows = []
    for rid, entry in ledger.entries().items():
        directory = Path(entry["dir"]).resolve()
        if not in_scope(directory, workspace, scope):
            continue
        source = row_builder(rid, entry)
        state = source["state_id"]
        running = source["running"] if state in ("running", "running-temp") else None
        stop = None
        if state not in ("missing", "mismatch"):
            try:
                stop = review_stop.public_status(directory)
            except (OSError, ValueError):
                pass
        work = {}
        if running:
            work = {"waiters": source["waiting"], "batches": source.get("processing", False),
                    "hooks": source["hook"]}
            try:
                work["analysis"] = analysis_store.has_claimed_round(directory)
            except (OSError, ValueError, store.NotFound):
                pass
        rows.append({
            "reviewId": rid, "name": source["name"] or directory.name,
            "path": display_path(directory, workspace), "state": state,
            "archived": source["archived"],
            "port": source["port"], "running": running,
            "stop": stop, "stopWork": work,
            "pending": source["pending"], "review": source["review"],
            "lastActivity": last_activity(directory), "current": rid == review_id,
            "url": f"http://127.0.0.1:{running}/" if running else None,
            "forgetCommand": f"serve.py forget {rid[:8]}" if state in ("missing", "mismatch") else None,
            **report_metadata(directory, rid),
        })
    rows.sort(key=lambda row: (2 if row["archived"] and row["state"] not in ("missing", "mismatch")
                              else STATE_ORDER.get(row["state"], 3), -(row["lastActivity"] or 0),
                               row["name"].casefold(), row["reviewId"]))
    return {"workspace": workspace.name, "scope": scope, "reviews": rows}
