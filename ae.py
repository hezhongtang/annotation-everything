#!/usr/bin/env python3
"""annotation-everything 统一管理命令：安装技能副本、体检安装状态。

用法：
  python3 ae.py install            # 把技能本体复制为技能副本（Claude 技能目录，CLAUDE_SKILLS_DIR 可覆盖）
  python3 ae.py doctor [--json]    # 体检：逐项检查并给出级别与修复命令

约定：
- 只用 Python 标准库，语法兼容 Python 3.9；
- 退出码：0 全部通过或只有警告，1 有失败项，2 命令自身出错；
- 安装记录只写在技能副本里，不进仓库。
"""
from __future__ import annotations

import argparse
import dataclasses
import datetime
import hashlib
import json
import os
import platform
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Optional

APP = "annotation-everything"
SKILL_NAME = "annotation-everything"
SKILL_SRC = Path("skill") / SKILL_NAME
RECORD_NAME = ".install-record.json"
SERVE_PY = SKILL_SRC / "scripts" / "serve.py"
# 复制与比对都忽略的缓存文件：字节码、系统杂项
EXCLUDED_DIRS = {"__pycache__"}
EXCLUDED_NAMES = {".DS_Store"}
EXCLUDED_SUFFIXES = {".pyc", ".pyo"}

# 体检的级别与类别（文字即对用户的输出，勿改为英文）
LEVEL_PASS, LEVEL_WARN, LEVEL_FAIL = "通过", "警告", "失败"
CATEGORY_INSTALL = "安装状态"
MIN_PYTHON = (3, 9)


@dataclasses.dataclass
class CheckResult:
    """一个检查项的结果记录；字段名同时是 --json 的键名。"""
    code: str          # 检查项编号，如 "python-version"
    category: str      # 类别，如 "安装状态"
    level: str         # 通过 / 警告 / 失败
    message: str       # 说明
    fix_command: str = ""  # 修复命令，可空


@dataclasses.dataclass
class DoctorContext:
    """体检的输入状态：检查项函数的唯一参数。"""
    skill_copy: Path            # 技能副本目录
    repo: Optional[Path]        # 来源仓库根目录；找不到时为 None
    record: Optional[dict]      # 安装记录；读不到时为 None


def skill_copy_dir() -> Path:
    """技能副本的位置：CLAUDE_SKILLS_DIR 可覆盖，默认 ~/.claude/skills。"""
    skills_root = os.environ.get("CLAUDE_SKILLS_DIR") or Path.home() / ".claude" / "skills"
    return Path(skills_root).expanduser() / SKILL_NAME


def read_version(repo: Path) -> Optional[str]:
    """读来源仓库 serve.py 里的 VERSION 常量；读不到返回 None。"""
    try:
        text = (repo / SERVE_PY).read_text(encoding="utf-8")
    except OSError:
        return None
    match = re.search(r'^VERSION = "([^"]+)"', text, re.M)
    return match.group(1) if match else None


def read_commit(repo: Path) -> str:
    """读来源仓库当前提交号；不在 git 仓库里时为空字符串。"""
    try:
        done = subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"],
                              capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        return ""
    return done.stdout.strip() if done.returncode == 0 else ""


def copy_skill(src: Path, dst: Path) -> None:
    """把技能本体复制为技能副本：先清掉旧副本，保证重复运行结果一致。"""

    def ignore(directory, names):
        kept = []
        for name in names:
            path = Path(directory) / name
            if path.is_dir() and name in EXCLUDED_DIRS:
                continue
            suffix = Path(name).suffix
            if name in EXCLUDED_NAMES or suffix in EXCLUDED_SUFFIXES:
                continue
            kept.append(name)
        return [name for name in names if name not in kept]

    if dst.exists():
        shutil.rmtree(dst)
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(src, dst, ignore=ignore)


def write_record(dst: Path, repo: Path) -> Path:
    """在技能副本根目录写安装记录；返回记录文件路径。"""
    record = {
        "repo": str(repo.resolve()),
        "commit": read_commit(repo),
        "version": read_version(repo) or "",
        "installed_at": datetime.datetime.now().astimezone().isoformat(timespec="seconds"),
        "links": [],
    }
    path = dst / RECORD_NAME
    path.write_text(json.dumps(record, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return path


def cmd_install(args) -> int:
    repo = Path(__file__).resolve().parent
    src = repo / SKILL_SRC
    if not src.is_dir():
        print(f"找不到技能本体：{src}", file=sys.stderr)
        return 2
    dst = skill_copy_dir()
    copy_skill(src, dst)
    write_record(dst, repo)
    print(f"已安装技能副本到 {dst}")
    return 0


# ── 体检 ───────────────────────────────────────────────────────


def read_record(skill_copy: Path) -> Optional[dict]:
    """读技能副本里的安装记录；缺失或损坏返回 None。"""
    try:
        return json.loads((skill_copy / RECORD_NAME).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def resolve_repo(record: Optional[dict]) -> Optional[Path]:
    """定位来源仓库：以安装记录里的路径为准，找不到时回退到 ae.py 自身所在仓库。"""
    if record and record.get("repo"):
        candidate = Path(record["repo"])
        if (candidate / SKILL_SRC).is_dir():
            return candidate
    self_repo = Path(__file__).resolve().parent
    if (self_repo / SKILL_SRC).is_dir():
        return self_repo
    return None


def install_fix(repo: Optional[Path]) -> str:
    target = repo / "ae.py" if repo else Path("ae.py")
    return f"python3 {target} install"


def _excluded_rel(rel: Path) -> bool:
    """比对时忽略的相对路径：缓存文件与安装记录（后者只存在于副本）。"""
    if EXCLUDED_DIRS.intersection(rel.parts[:-1]):
        return True
    return rel.name in EXCLUDED_NAMES or rel.suffix in EXCLUDED_SUFFIXES or rel.as_posix() == RECORD_NAME


def _digests(root: Path) -> dict:
    entries = {}
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        rel = path.relative_to(root)
        if _excluded_rel(rel):
            continue
        entries[rel.as_posix()] = hashlib.sha256(path.read_bytes()).hexdigest()
    return entries


def check_python(ctx: DoctorContext) -> CheckResult:
    version = platform.python_version()
    if sys.version_info[:3] < MIN_PYTHON:
        return CheckResult("python-version", CATEGORY_INSTALL, LEVEL_FAIL,
                           f"Python {version} 低于 3.9，技能无法运行")
    return CheckResult("python-version", CATEGORY_INSTALL, LEVEL_PASS, f"Python {version}")


def check_copy_present(ctx: DoctorContext) -> CheckResult:
    if (ctx.skill_copy / "SKILL.md").is_file():
        return CheckResult("skill-copy-present", CATEGORY_INSTALL, LEVEL_PASS,
                           f"技能副本在 {ctx.skill_copy}")
    return CheckResult("skill-copy-present", CATEGORY_INSTALL, LEVEL_FAIL,
                       f"未找到技能副本（{ctx.skill_copy}）", install_fix(ctx.repo))


def check_copy_sync(ctx: DoctorContext) -> CheckResult:
    if not (ctx.skill_copy / "SKILL.md").is_file():
        return CheckResult("skill-copy-sync", CATEGORY_INSTALL, LEVEL_PASS, "技能副本缺失，未比对")
    if ctx.repo is None:
        return CheckResult("skill-copy-sync", CATEGORY_INSTALL, LEVEL_PASS, "未找到来源仓库，未比对")
    src, dst = ctx.repo / SKILL_SRC, ctx.skill_copy
    if _digests(src) == _digests(dst):
        return CheckResult("skill-copy-sync", CATEGORY_INSTALL, LEVEL_PASS, "技能副本与来源仓库一致")
    return CheckResult("skill-copy-sync", CATEGORY_INSTALL, LEVEL_FAIL,
                       f"技能副本与来源仓库（{src}）不一致", install_fix(ctx.repo))


def check_copy_version(ctx: DoctorContext) -> CheckResult:
    if ctx.record is None:
        return CheckResult("skill-copy-version", CATEGORY_INSTALL, LEVEL_PASS, "无安装记录，未比对")
    if ctx.repo is None:
        return CheckResult("skill-copy-version", CATEGORY_INSTALL, LEVEL_PASS, "未找到来源仓库，未比对")
    repo_version = read_version(ctx.repo)
    if repo_version is None:
        return CheckResult("skill-copy-version", CATEGORY_INSTALL, LEVEL_PASS, "读不到来源仓库的版本号，未比对")
    installed = ctx.record.get("version", "")
    if installed == repo_version:
        return CheckResult("skill-copy-version", CATEGORY_INSTALL, LEVEL_PASS,
                           f"版本号一致（{repo_version}）")
    return CheckResult("skill-copy-version", CATEGORY_INSTALL, LEVEL_FAIL,
                       f"技能副本装的是 {installed or '未知版本'}，来源仓库已是 {repo_version}",
                       install_fix(ctx.repo))


# 全部检查项：doctor 按此顺序执行；后续工单在元组里追加新检查项函数
CHECKS = (check_python, check_copy_present, check_copy_sync, check_copy_version)


def run_doctor(ctx: DoctorContext):
    return [check(ctx) for check in CHECKS]


def summarize(results):
    counts = {LEVEL_PASS: 0, LEVEL_WARN: 0, LEVEL_FAIL: 0}
    for result in results:
        counts[result.level] += 1
    return {"pass": counts[LEVEL_PASS], "warn": counts[LEVEL_WARN], "fail": counts[LEVEL_FAIL]}


def print_doctor(results) -> None:
    """文字输出：按类别分组，失败项附修复命令。"""
    categories = []
    for result in results:
        if result.category not in categories:
            categories.append(result.category)
    for category in categories:
        print(category)
        for result in (r for r in results if r.category == category):
            print(f"  [{result.level}] {result.code}：{result.message}")
            if result.fix_command:
                print(f"      修复：{result.fix_command}")
    summary = summarize(results)
    print(f"\n汇总：通过 {summary['pass']}，警告 {summary['warn']}，失败 {summary['fail']}")


def cmd_doctor(args) -> int:
    skill_copy = skill_copy_dir()
    record = read_record(skill_copy)
    repo = resolve_repo(record)
    results = run_doctor(DoctorContext(skill_copy=skill_copy, repo=repo, record=record))
    if args.as_json:
        print(json.dumps({"results": [dataclasses.asdict(r) for r in results],
                          "summary": summarize(results)}, ensure_ascii=False, indent=2))
    else:
        print_doctor(results)
    return 1 if any(r.level == LEVEL_FAIL for r in results) else 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="ae.py", description=f"{APP} 统一管理命令")
    sub = parser.add_subparsers(dest="command", required=True)
    p_install = sub.add_parser("install", help="把技能本体复制为技能副本")
    p_install.set_defaults(handler=cmd_install)
    p_doctor = sub.add_parser("doctor", help="体检：检查安装状态，给出级别与修复命令")
    p_doctor.add_argument("--json", action="store_true", dest="as_json",
                          help="输出结构化的体检结果（JSON）")
    p_doctor.set_defaults(handler=cmd_doctor)
    return parser


def main(argv=None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.handler(args)


if __name__ == "__main__":
    sys.exit(main())
