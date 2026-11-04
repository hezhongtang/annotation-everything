"""审阅总览：复用台账行状态，仅公开适合页面显示的字段。"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Callable

import fsutil
import ledger
import notes_store as store

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


def snapshot(review_dir: Path, review_id: str | None, scope: str,
             row_builder: Callable[[str, dict[str, Any]], dict[str, Any]]) -> dict[str, Any]:
    """台账是索引，审阅名、计数和运行状态由 list 的同一份逻辑现读。"""
    workspace = workspace_for(review_dir)
    rows = []
    for rid, entry in ledger.entries().items():
        directory = Path(entry["dir"]).resolve()
        if scope == "workspace" and directory != workspace and workspace not in directory.parents:
            continue
        source = row_builder(rid, entry)
        state = source["state_id"]
        running = source["running"] if state in ("running", "running-temp") else None
        rows.append({
            "reviewId": rid, "name": source["name"] or directory.name,
            "path": display_path(directory, workspace), "state": state,
            "archived": source["archived"],
            "port": source["port"], "running": running,
            "pending": source["pending"], "review": source["review"],
            "lastActivity": last_activity(directory), "current": rid == review_id,
            "url": f"http://127.0.0.1:{running}/" if running else None,
            "forgetCommand": f"serve.py forget {rid[:8]}" if state in ("missing", "mismatch") else None,
        })
    rows.sort(key=lambda row: (2 if row["archived"] and row["state"] not in ("missing", "mismatch")
                              else STATE_ORDER.get(row["state"], 3), -(row["lastActivity"] or 0),
                               row["name"].casefold(), row["reviewId"]))
    return {"workspace": workspace.name, "scope": scope, "reviews": rows}
