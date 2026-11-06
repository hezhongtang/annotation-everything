#!/usr/bin/env python3
"""版本提交通过 CI 后补齐标签、GitHub Release 和里程碑。可重复运行。"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from dataclasses import dataclass
from typing import Callable, Iterable, List, Optional, Tuple

VERSION_FILE = "skill/annotation-everything/scripts/serve.py"
VERSION_LINE = re.compile(r'^VERSION = "([^"]+)"', re.M)
VERSION = re.compile(r"(\d+)\.(\d+)\.(\d+)")
SHA = re.compile(r"[0-9a-f]{40,64}")
REPO = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+")
BOT = ("github-actions[bot]", "41898282+github-actions[bot]@users.noreply.github.com")

Runner = Callable[[List[str]], str]


class ReleaseError(Exception):
    pass


@dataclass
class Plan:
    new_tag: Optional[str]
    missing: List[Tuple[str, Optional[str]]]
    latest: Optional[str]


def parse_version(value: str) -> Tuple[int, int, int]:
    match = VERSION.fullmatch(value)
    if not match:
        raise ReleaseError(f"版本号格式不对：{value!r}")
    return tuple(int(part) for part in match.groups())


def release_tags(tags: Iterable[str]) -> List[str]:
    """只保留 vX.Y.Z 正式标签，按版本号升序。"""
    found = [tag for tag in tags if tag.startswith("v") and VERSION.fullmatch(tag[1:])]
    return sorted(found, key=lambda tag: parse_version(tag[1:]))


def make_plan(version: str, tags: Iterable[str], releases: Iterable[str]) -> Plan:
    current = parse_version(version)
    ordered = release_tags(tags)
    # 自动更新安装最高版本标签，所以版本号只能前进，回退或重复都不发布。
    newest = parse_version(ordered[-1][1:]) if ordered else None
    new_tag = f"v{version}" if newest is None or current > newest else None
    if new_tag:
        ordered.append(new_tag)
    published = set(releases)
    missing = [(tag, ordered[index - 1] if index else None)
               for index, tag in enumerate(ordered) if tag not in published]
    return Plan(new_tag, missing, ordered[-1] if ordered else None)


def read_version(run: Runner, sha: str) -> str:
    match = VERSION_LINE.search(run(["git", "show", f"{sha}:{VERSION_FILE}"]))
    if not match:
        raise ReleaseError(f"{VERSION_FILE} 里找不到 VERSION")
    return match.group(1)


def create_tag(run: Runner, tag: str, sha: str) -> None:
    run(["git", "-c", f"user.name={BOT[0]}", "-c", f"user.email={BOT[1]}",
         "tag", "-a", tag, sha, "-m", f"annotation-everything {tag}"])
    run(["git", "push", "origin", f"refs/tags/{tag}"])


def create_release(run: Runner, repo: str, tag: str, previous: Optional[str], latest: bool) -> None:
    args = ["gh", "release", "create", tag, "--repo", repo, "--verify-tag",
            "--title", tag, "--generate-notes", "--latest" if latest else "--latest=false"]
    if previous:
        args += ["--notes-start-tag", previous]
    run(args)


def close_milestone(run: Runner, repo: str, tag: str) -> None:
    milestones = json.loads(run(["gh", "api", f"repos/{repo}/milestones?state=open&per_page=100"]))
    for milestone in milestones:
        if milestone["title"] != tag:
            continue
        if milestone["open_issues"]:
            print(f"里程碑 {tag} 还有 {milestone['open_issues']} 项未完成，保持打开")
        else:
            run(["gh", "api", "--method", "PATCH", f"repos/{repo}/milestones/{milestone['number']}",
                 "-f", "state=closed"])


def execute(run: Runner, repo: str, sha: str, dry_run: bool = False) -> Plan:
    if not REPO.fullmatch(repo):
        raise ReleaseError(f"仓库名格式不对：{repo!r}")
    if not SHA.fullmatch(sha):
        raise ReleaseError(f"提交 SHA 格式不对：{sha!r}")
    run(["git", "merge-base", "--is-ancestor", sha, "origin/main"])
    version = read_version(run, sha)
    tags = run(["git", "tag", "--list", "v*"]).split()
    releases = run(["gh", "release", "list", "--repo", repo, "--limit", "1000",
                    "--json", "tagName", "--jq", ".[].tagName"]).split()
    plan = make_plan(version, tags, releases)
    print(f"版本 {version}；新标签：{plan.new_tag or '无'}；待建 Release：{[tag for tag, _ in plan.missing]}")
    if dry_run:
        return plan
    if plan.new_tag:
        create_tag(run, plan.new_tag, sha)
    for tag, previous in plan.missing:
        create_release(run, repo, tag, previous, tag == plan.latest)
    close_milestone(run, repo, f"v{version}")
    return plan


def run_command(args: List[str]) -> str:
    result = subprocess.run(args, capture_output=True, text=True)
    if result.returncode != 0:
        raise ReleaseError(f"命令失败（{result.returncode}）：{' '.join(args)}\n{result.stderr}")
    return result.stdout


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", required=True, help="owner/name")
    parser.add_argument("--sha", required=True, help="已通过 CI 的 main 提交")
    parser.add_argument("--dry-run", action="store_true", help="只打印计划，不写入")
    args = parser.parse_args()
    try:
        execute(run_command, args.repo, args.sha, args.dry_run)
    except ReleaseError as exc:
        print(exc, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
