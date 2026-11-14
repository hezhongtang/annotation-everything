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


def display_path(directory: Path, workspace: Path | None) -> str:
    """工作区内显示相对路径；其他位置显示 ~ 路径或目录名。workspace 传 None（总览首页的全台账范围）直接按 ~ 路径显示。"""
    if workspace is not None:
        try:
            return directory.relative_to(workspace).as_posix()
        except ValueError:
            pass
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


def wait_command(review_id: str, *, hook: bool, receiver: Any) -> str | None:
    """可复制的等待命令：等待型绑定提示从绑定会话运行；未绑定且未配置自定义钩子时也提供。

    Codex 桌面事件绑定按现有协议投递与接手，省略认领型等待命令；已配置自定义钩子时
    显示「已配置」，不另给等待命令。adapter 只认公开接收方状态里的值。
    """
    if hook:
        return None
    adapter = (receiver or {}).get("adapter") if isinstance(receiver, dict) else None
    return f"notes.py wait {review_id[:8]}" if adapter in (None, "wait") else None


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


def snapshot(review_dir: Path | None, review_id: str | None, scope: str,
             row_builder: Callable[[str, dict[str, Any]], dict[str, Any]], *,
             workspace_dir: Path | None = None, under_dir: Path | None = None) -> dict[str, Any]:
    """台账是索引，审阅名、计数和运行状态由 list 的同一份逻辑现读。

    workspace_dir / under_dir：总览首页宿主没有审阅目录，显式传显示用工作区与 --under 范围
    （沿用 list --under 的目录包含关系，不是 git 根推断）；不传时行为与审阅宿主一致。
    """
    if workspace_dir is not None:
        workspace = workspace_dir
    elif review_dir is not None:
        workspace = workspace_for(review_dir)
    else:
        workspace = None   # 总览首页的全台账范围：没有工作区，显示路径全部走 ~/ 或目录名
    under = under_dir.resolve() if under_dir is not None else None
    rows = []
    for rid, entry in ledger.entries().items():
        directory = Path(entry["dir"]).resolve()
        if under is not None and not directory.is_relative_to(under):
            continue
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
            # 公开接收方字段：绑定状态与接收方式（不含钩子命令、工作目录、套接字地址或日志）
            "hook": source["hook"], "agentDelivery": source["agentDelivery"],
            "waitCommand": wait_command(rid, hook=source["hook"], receiver=source["agentDelivery"]),
            "lastActivity": last_activity(directory), "current": rid == review_id,
            "url": f"http://127.0.0.1:{running}/" if running else None,
            "forgetCommand": f"serve.py forget {rid[:8]}" if state in ("missing", "mismatch") else None,
            **report_metadata(directory, rid),
        })
    rows.sort(key=lambda row: (2 if row["archived"] and row["state"] not in ("missing", "mismatch")
                              else STATE_ORDER.get(row["state"], 3), -(row["lastActivity"] or 0),
                               row["name"].casefold(), row["reviewId"]))
    return {"workspace": workspace.name if workspace is not None else None, "scope": scope, "reviews": rows}
