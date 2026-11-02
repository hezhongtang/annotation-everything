#!/usr/bin/env python3
"""只读 GitHub 现有验证构件与合并事件，不启动测试。认证沿用 gh 的现有环境。"""
from __future__ import annotations

import argparse
import datetime
import io
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path, PurePosixPath

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import verify_report

MAX_COMPRESSED = 30 * 1024 * 1024
MAX_EXPANDED = 60 * 1024 * 1024


def gh_api(endpoint, binary=False):
    try:
        result = subprocess.run(["gh", "api", endpoint], capture_output=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ValueError("远端不可用或网络超时（%s）" % type(exc).__name__) from exc
    if result.returncode:
        text = result.stderr.decode("utf-8", errors="replace")
        reason = "权限不足" if "403" in text or "401" in text else "远端不可用、构件缺失或网络失败"
        raise ValueError(reason)
    if binary:
        if len(result.stdout) > MAX_COMPRESSED:
            raise ValueError("构件超过压缩体积上限")
        return result.stdout
    try:
        return json.loads(result.stdout)
    except ValueError as exc:
        raise ValueError("远端响应 schema 不兼容") from exc


def extract_artifact(repository, artifact, target):
    if artifact.get("expired"):
        raise ValueError("构件已过期")
    if artifact.get("size_in_bytes", MAX_COMPRESSED + 1) > MAX_COMPRESSED:
        raise ValueError("构件超过压缩体积上限")
    if target.is_dir():
        return
    data = gh_api("repos/%s/actions/artifacts/%s/zip" % (repository, artifact["id"]), binary=True)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = Path(tempfile.mkdtemp(prefix=".ci-artifact-", dir=str(target.parent)))
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            members = archive.infolist()
            if len(members) > 5000 or sum(item.file_size for item in members) > MAX_EXPANDED:
                raise ValueError("构件展开超过有界历史限制")
            for item in members:
                name = PurePosixPath(item.filename)
                if (name.is_absolute() or ".." in name.parts or "\\" in item.filename
                        or stat.S_ISLNK(item.external_attr >> 16)):
                    raise ValueError("构件含不安全路径或符号链接")
            archive.extractall(temporary)
        os.replace(temporary, target)
    finally:
        if temporary.exists():
            shutil.rmtree(temporary)


def collect(repository, out_dir, limit):
    out_dir.mkdir(parents=True, exist_ok=True)
    gaps, merged, sources, locations = [], [], [], []
    def gap(source, reason):
        gaps.append({"source": source, "reason": str(reason)})
    try:
        prs = gh_api("repos/%s/pulls?state=closed&sort=updated&direction=desc&per_page=100" % repository)
        if not isinstance(prs, list):
            raise ValueError("合并 PR 清单 schema 不兼容")
        merged = ["%s#%s" % (repository, item["number"]) for item in prs if item.get("merged_at")]
        if len(prs) == 100:
            gap("merged-prs", "只覆盖最近100个已关闭PR；更早事件缺失，不推断完整历史")
    except (ValueError, KeyError, TypeError) as exc:
        gap("merged-prs", exc)
    try:
        response = gh_api("repos/%s/actions/artifacts?per_page=100" % repository)
        artifacts = response["artifacts"]
        if not isinstance(artifacts, list):
            raise ValueError("构件清单 schema 不兼容")
        if response.get("total_count", 0) > 100:
            gap("ci-artifacts", "只覆盖最近100个构件；更早构件不在本次采集范围")
        selected = sorted([a for a in artifacts if a.get("name", "").startswith("verify-python")],
                          key=lambda a: a.get("created_at", ""), reverse=True)[:limit]
        if not selected:
            gap("ci-artifacts", "没有可导入的 CI 验证构件，不能推断 CI 零失败")
        for artifact in selected:
            aid = str(artifact["id"])
            if not aid.isdigit():
                gap("ci-artifacts", "构件 ID 不合法")
                continue
            target = out_dir / "ci" / aid
            try:
                extract_artifact(repository, artifact, target)
                # upload-artifact 可能剥去 .verify 共同祖先；两种结构均只认 records 根。
                source = target if (target / "runs").is_dir() else target / ".verify"
                if not (source / "runs").is_dir():
                    raise ValueError("构件缺少结构化 runs 目录；旧日志不能当作验证成功")
                sources.append(str(source.relative_to(out_dir)))
                locations.append({"source": str(source.relative_to(out_dir)), "artifact_id": aid,
                                  "url": "https://github.com/%s/actions/runs/%s" % (
                                      repository, (artifact.get("workflow_run") or {}).get("id", "unknown"))})
            except (ValueError, OSError, zipfile.BadZipFile) as exc:
                gap("artifact:%s" % aid, exc)
        # 只恢复同仓库、默认分支、报告工作流自己的状态；PR 构件不能设置守卫或去重状态。
        prior = next((a for a in sorted(artifacts, key=lambda a: a.get("created_at", ""), reverse=True)
                      if a.get("name") == "verify-efficiency-history" and not a.get("expired")), None)
        if prior is not None and not (out_dir / "efficiency" / "state.json").exists():
            try:
                repo = gh_api("repos/%s" % repository)
                workflow = gh_api("repos/%s/actions/workflows/verify-efficiency.yml" % repository)
                run = gh_api("repos/%s/actions/runs/%s" % (repository, prior["workflow_run"]["id"]))
                if (run.get("workflow_id") != workflow.get("id") or run.get("head_branch") != repo.get("default_branch")
                        or (run.get("head_repository") or {}).get("id") != repo.get("id")
                        or run.get("event") not in ("workflow_run", "schedule", "workflow_dispatch")):
                    raise ValueError("历史回顾状态来源无法确认为受信任报告工作流")
                cached = out_dir / "ci" / ("history-" + str(prior["id"]))
                extract_artifact(repository, prior, cached)
                states = list(cached.glob("**/state.json"))
                if len(states) != 1:
                    raise ValueError("历史状态缺失或不唯一")
                state = json.loads(states[0].read_text(encoding="utf-8"))
                if state.get("schema") != verify_report.SCHEMA:
                    raise ValueError("历史状态 schema 不兼容")
                verify_report._write(out_dir / "efficiency" / "state.json", json.dumps(state, ensure_ascii=False))
            except (ValueError, OSError, KeyError, TypeError, zipfile.BadZipFile) as exc:
                gap("review-state", exc)
        wanted = {Path(source).parts[1] for source in sources}
        ci_dir = out_dir / "ci"
        if ci_dir.exists():
            for directory in ci_dir.iterdir():
                if directory.is_dir() and directory.name not in wanted:
                    shutil.rmtree(directory)
    except (ValueError, KeyError, TypeError) as exc:
        gap("ci-artifacts", exc)
    payload = {"schema": "ae-verify-events/1", "collected_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
               "merged_prs": merged, "sources": sources, "source_artifacts": locations, "gaps": gaps}
    verify_report._write(out_dir / "ci-events.json", json.dumps(payload, ensure_ascii=False, indent=2))
    print("只读 CI 采集：%d 个记录根、%d 个合并 PR、%d 个明确缺口" % (len(sources), len(merged), len(gaps)))
    return 1 if gaps else 0


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", required=True, help="owner/name")
    parser.add_argument("--out-dir", default=".verify")
    parser.add_argument("--limit", type=int, default=20, help="最多导入的验证构件数（1–50）")
    args = parser.parse_args(argv)
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", args.repo) or not 1 <= args.limit <= 50:
        parser.error("--repo 需要 owner/name；--limit 必须为1–50")
    return collect(args.repo, Path(args.out_dir), args.limit)


if __name__ == "__main__":
    sys.exit(main())
