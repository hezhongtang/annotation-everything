#!/usr/bin/env python3
"""annotation-everything 统一管理命令：安装技能副本、体检安装状态。

用法：
  python3 ae.py install            # 把技能本体复制为技能副本（Claude 技能目录，CLAUDE_SKILLS_DIR 可覆盖）
  python3 ae.py install --link codex,agents,zcode
                                    # 同时在这些 Agent 的技能目录里建指向技能副本的软链接
  python3 ae.py doctor [--json]    # 体检：逐项检查并给出级别与修复命令
  python3 ae.py uninstall [--purge] [--stop-servers]
                                    # 删除技能副本与指向它的链接；默认保留审阅台账（--purge 一并删除），
                                    # 任何情况下不动审阅目录；检测到 serve.py 在运行会拒绝（--stop-servers 先结束）

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
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Callable, Optional

APP = "annotation-everything"
SKILL_NAME = "annotation-everything"
SKILL_SRC = Path("skill") / SKILL_NAME
RECORD_NAME = ".install-record.json"
SERVE_PY = SKILL_SRC / "scripts" / "serve.py"
# --link 接受的 Agent 名称；目录约定与 skill_sources.default_roots 一致
LINK_AGENTS = ("codex", "agents", "zcode")
# 复制与比对都忽略的缓存文件：字节码、系统杂项
EXCLUDED_DIRS = {"__pycache__"}
EXCLUDED_NAMES = {".DS_Store"}
EXCLUDED_SUFFIXES = {".pyc", ".pyo"}

# 体检的级别与类别（文字即对用户的输出，勿改为英文）
LEVEL_PASS, LEVEL_WARN, LEVEL_FAIL = "通过", "警告", "失败"
CATEGORY_INSTALL = "安装状态"
CATEGORY_TOOLS = "可选工具"
CATEGORY_DEV = "开发环境"
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
    # Chrome 探测函数（返回可执行文件路径或 None）；测试注入用，None 时用默认探测 find_chrome
    chrome_probe: Optional[Callable[[], Optional[str]]] = None


def skill_copy_dir() -> Path:
    """技能副本的位置：CLAUDE_SKILLS_DIR 可覆盖，默认 ~/.claude/skills。"""
    skills_root = os.environ.get("CLAUDE_SKILLS_DIR") or Path.home() / ".claude" / "skills"
    return Path(skills_root).expanduser() / SKILL_NAME


def link_target_dir(agent: str) -> Optional[Path]:
    """Agent 的技能目录；未知名称返回 None。目录约定与 skill_sources 一致。"""
    home = Path.home()
    if agent == "codex":
        return Path(os.environ.get("CODEX_HOME") or home / ".codex").expanduser() / "skills"
    if agent == "agents":
        return home / ".agents" / "skills"
    if agent == "zcode":
        return home / ".zcode" / "skills"
    return None


def link_path(agent: str) -> Optional[Path]:
    """Agent 技能目录里软链接的位置。"""
    target_dir = link_target_dir(agent)
    return target_dir / SKILL_NAME if target_dir else None


def parse_link_agents(spec: str) -> list:
    """解析 --link 的逗号分隔名称：去空白、去重，保留出现顺序。"""
    agents = []
    for part in (spec or "").split(","):
        name = part.strip()
        if name and name not in agents:
            agents.append(name)
    return agents


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


def write_record(dst: Path, repo: Path, links: Optional[list] = None) -> Path:
    """在技能副本根目录写安装记录；返回记录文件路径。"""
    record = {
        "repo": str(repo.resolve()),
        "commit": read_commit(repo),
        "version": read_version(repo) or "",
        "installed_at": datetime.datetime.now().astimezone().isoformat(timespec="seconds"),
        "links": list(links or []),
    }
    path = dst / RECORD_NAME
    path.write_text(json.dumps(record, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return path


def create_link(agent: str, copy_dir: Path) -> dict:
    """在 Agent 技能目录里建指向技能副本的软链接；返回写入安装记录的条目。

    位置上已有的链接（含悬空的、指向别处的）一律重建；非链接内容由调用方先拦截。
    """
    link = link_path(agent)
    if link.is_symlink():
        link.unlink()
    link.parent.mkdir(parents=True, exist_ok=True)
    os.symlink(str(copy_dir.resolve()), str(link))
    return {"agent": agent, "path": str(link)}


def merged_links(previous: Optional[dict], copy_dir: Path, created: list) -> list:
    """合并历史记录与本次新建的链接：只保留仍是链接且指向技能副本的条目，按路径去重。"""
    target = os.path.realpath(str(copy_dir))
    entries = []
    if isinstance(previous, dict) and isinstance(previous.get("links"), list):
        entries.extend(entry for entry in previous["links"] if isinstance(entry, dict))
    entries.extend(created)
    merged, seen = [], set()
    for entry in entries:
        path = entry.get("path")
        if not isinstance(path, str) or path in seen:
            continue
        if Path(path).is_symlink() and os.path.realpath(path) == target:
            merged.append({"agent": entry.get("agent", ""), "path": path})
            seen.add(path)
    return merged


def cmd_install(args) -> int:
    repo = Path(__file__).resolve().parent
    src = repo / SKILL_SRC
    if not src.is_dir():
        print(f"找不到技能本体：{src}", file=sys.stderr)
        return 2
    agents = parse_link_agents(getattr(args, "link", "") or "")
    unknown = [agent for agent in agents if link_target_dir(agent) is None]
    if unknown:
        print(f"未知的 Agent 名称：{'、'.join(unknown)}（可选：{'、'.join(LINK_AGENTS)}）",
              file=sys.stderr)
        return 2
    # 先拦下所有冲突，再动手：目标位置已有同名非链接内容时不覆盖、不复制
    for agent in agents:
        target = link_path(agent)
        if target.exists() and not target.is_symlink():
            print(f"{agent} 的技能目录里已有同名非链接内容，未覆盖：{target}", file=sys.stderr)
            return 2
    dst = skill_copy_dir()
    previous = read_record(dst)  # 在清掉旧副本前读出已记录的链接
    copy_skill(src, dst)
    created = [create_link(agent, dst) for agent in agents]
    links = merged_links(previous, dst, created)
    write_record(dst, repo, links)
    print(f"已安装技能副本到 {dst}")
    if created:
        print("已建立链接：" + "、".join(f"{entry['agent']}（{entry['path']}）" for entry in created))
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


# ── 链接体检 ───────────────────────────────────────────────────


def scan_links(skill_copy: Path) -> list:
    """回退扫描：在三个已知 Agent 技能目录里找指向技能副本的链接（含悬空的）。

    返回条目与安装记录 links 同构：{"agent": 名称, "path": 链接绝对路径}。
    """
    target = os.path.realpath(str(skill_copy))
    found = []
    for agent in LINK_AGENTS:
        link = link_path(agent)
        if not link.is_symlink():
            continue
        if os.path.realpath(str(link)) == target or os.readlink(str(link)) == target:
            found.append({"agent": agent, "path": str(link)})
    return found


def _link_status(link: Path, skill_copy: Path) -> str:
    """单个链接的状态：正常 / 悬空 / 指向别处 / 已不是链接 / 已删除。"""
    if link.is_symlink():
        if not link.exists():
            return "悬空"
        if os.path.realpath(str(link)) != os.path.realpath(str(skill_copy)):
            return "指向别处"
        return "正常"
    if link.exists():
        return "已不是链接"
    return "已删除"


def install_link_fix(repo: Optional[Path], agents: list) -> str:
    target = repo / "ae.py" if repo else Path("ae.py")
    return f"python3 {target} install --link {','.join(agents)}"


def known_links(ctx: DoctorContext) -> list:
    """已知链接：优先取安装记录；记录缺失时回退扫描三个已知 Agent 目录。"""
    links = ctx.record.get("links") if isinstance(ctx.record, dict) else None
    if isinstance(links, list):
        return links
    return scan_links(ctx.skill_copy)


def check_links(ctx: DoctorContext) -> CheckResult:
    entries = [entry for entry in known_links(ctx)
               if isinstance(entry, dict) and isinstance(entry.get("path"), str)]
    if not entries:
        return CheckResult("skill-links", CATEGORY_INSTALL, LEVEL_PASS, "未发现指向技能副本的链接")
    ok, broken = [], []
    for entry in entries:
        status = _link_status(Path(entry["path"]), ctx.skill_copy)
        report = f"{entry.get('agent') or '未知'}（{entry['path']}）：{status}"
        (ok if status == "正常" else broken).append(report)
    if not broken:
        return CheckResult("skill-links", CATEGORY_INSTALL, LEVEL_PASS,
                           f"{len(ok)} 个链接正常：{'；'.join(ok)}")
    agents = sorted({str(entry.get("agent") or "") for entry in entries
                     if _link_status(Path(entry["path"]), ctx.skill_copy) != "正常"})
    summary = f"{len(broken)} 个链接异常：{'；'.join(broken + ok)}"
    return CheckResult("skill-links", CATEGORY_INSTALL, LEVEL_FAIL, summary,
                       install_link_fix(ctx.repo, agents))


# ── 可选工具（缺失只警告，不影响退出码） ────────────────────────


def tool_fix(package: str) -> str:
    """可选工具缺失时的安装提示：按平台给出包管理器命令。"""
    if sys.platform == "darwin":
        return f"brew install {package}"
    return f"sudo apt install {package}"


def check_fc_list(ctx: DoctorContext) -> CheckResult:
    if shutil.which("fc-list"):
        return CheckResult("fc-list", CATEGORY_TOOLS, LEVEL_PASS, "fc-list 可用，字体核查可枚举本机字体")
    return CheckResult("fc-list", CATEGORY_TOOLS, LEVEL_WARN,
                       "缺少 fc-list，字体核查无法枚举本机字体", tool_fix("fontconfig"))


def check_pdffonts(ctx: DoctorContext) -> CheckResult:
    if shutil.which("pdffonts"):
        return CheckResult("pdffonts", CATEGORY_TOOLS, LEVEL_PASS, "pdffonts 可用，可列出 PDF 字体证据")
    package = "poppler" if sys.platform == "darwin" else "poppler-utils"
    return CheckResult("pdffonts", CATEGORY_TOOLS, LEVEL_WARN,
                       "缺少 pdffonts，字体核查无法检查 PDF 字体", tool_fix(package))


# ── 开发环境（仅在仓库内运行时执行，见 REPO_CHECKS） ─────────────


def git_config(repo: Path, key: str) -> Optional[str]:
    """读仓库的 git 配置；git 不可用或未设置时返回 None。"""
    try:
        done = subprocess.run(["git", "-C", str(repo), "config", "--get", key],
                              capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        return None
    value = done.stdout.strip() if done.returncode == 0 else ""
    return value or None


def check_git_hooks(ctx: DoctorContext) -> CheckResult:
    expected = ctx.repo / "tools" / "git-hooks"
    configured = git_config(ctx.repo, "core.hooksPath")
    if configured is not None:
        hooks = Path(configured).expanduser()
        if not hooks.is_absolute():
            hooks = ctx.repo / hooks
        if Path(os.path.normpath(hooks)) == Path(os.path.normpath(expected)) and expected.is_dir():
            return CheckResult("git-hooks", CATEGORY_DEV, LEVEL_PASS, "git 钩子已启用（tools/git-hooks）")
    return CheckResult("git-hooks", CATEGORY_DEV, LEVEL_WARN,
                       "git 钩子未启用，main 保护（拒绝在 main 上提交/推送/删除）不生效",
                       f"sh {ctx.repo / 'tools' / 'setup-dev.sh'}")


def find_chrome() -> Optional[str]:
    """定位端到端测试用的 Chrome：与 tests/e2e_browser.py 的 playwright channel="chrome" 同一目标。"""
    if sys.platform == "darwin":
        candidates = (
            Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"),
            Path.home() / "Applications" / "Google Chrome.app" / "Contents" / "MacOS" / "Google Chrome",
        )
        for candidate in candidates:
            if candidate.is_file():
                return str(candidate)
        return None
    if sys.platform.startswith("linux"):
        for name in ("google-chrome", "google-chrome-stable"):
            found = shutil.which(name)
            if found:
                return found
        chrome = Path("/opt/google/chrome/chrome")
        return str(chrome) if chrome.is_file() else None
    return None


def check_chrome(ctx: DoctorContext) -> CheckResult:
    probe = ctx.chrome_probe if ctx.chrome_probe is not None else find_chrome
    found = probe()
    if found:
        return CheckResult("e2e-chrome", CATEGORY_DEV, LEVEL_PASS, f"端到端测试可用的 Chrome：{found}")
    return CheckResult("e2e-chrome", CATEGORY_DEV, LEVEL_WARN,
                       "未找到端到端测试需要的 Chrome（tests/e2e_browser.py 按 channel=chrome 启动），"
                       "请安装 Google Chrome")


# 全部检查项：doctor 按此顺序执行；后续工单在元组里追加新检查项函数
CHECKS = (check_python, check_copy_present, check_copy_sync, check_copy_version, check_links,
          check_fc_list, check_pdffonts)

# 仅在仓库内运行（ctx.repo 非 None）的检查项：仓库外 doctor 的输出与 --json 都不出现「开发环境」类别
REPO_CHECKS = (check_git_hooks, check_chrome)


def run_doctor(ctx: DoctorContext):
    checks = CHECKS + REPO_CHECKS if ctx.repo is not None else CHECKS
    return [check(ctx) for check in checks]


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


# ── 运行中的服务（卸载用；运行状态体检复用时统一走这里） ────────


def _parse_ps(stdout: str) -> list:
    """解析 `ps -axo pid=,command=` 的输出：[(pid, 完整命令行), ...]。"""
    processes = []
    for line in stdout.splitlines():
        parts = line.strip().split(None, 1)
        if len(parts) == 2 and parts[0].isdigit():
            processes.append((int(parts[0]), parts[1].strip()))
    return processes


def _ps_processes() -> list:
    """列出本机全部进程；ps 不可用或失败时返回空列表（宁可漏报不误伤）。"""
    try:
        done = subprocess.run(["ps", "-axo", "pid=,command="],
                              capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        return []
    return _parse_ps(done.stdout) if done.returncode == 0 else []


def running_servers(lister: Optional[Callable[[], list]] = None) -> list:
    """正在运行的 serve.py 进程：[(pid, 命令行), ...]，与台账比对无关。

    serve.py 没有专属的进程标记，识别特征只有命令行里出现以 serve.py 结尾的
    脚本路径；因此宁可多报不漏报，卸载与 --stop-servers 都偏保守。
    lister 返回 [(pid, 命令行), ...]，测试可注入；默认用 ps 列举。
    """
    if lister is None:
        lister = _ps_processes
    found = []
    for pid, command in lister():
        args = command.split()
        if any(os.path.basename(part) == "serve.py" for part in args[1:]):
            found.append((pid, command))
    return found


def _process_stopped(pid: int) -> bool:
    """进程是否已停止（查不到，或只剩僵尸）；ps 不可用时只能假设已停止。"""
    try:
        done = subprocess.run(["ps", "-p", str(pid), "-o", "stat="],
                              capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        return True
    if done.returncode != 0:
        return True
    state = done.stdout.strip().upper()
    return not state or state.startswith("Z")


def stop_servers(servers: list, wait_seconds: float = 5.0) -> list:
    """结束 serve.py 进程：先 SIGTERM（serve.py 收到后优雅退出），等不到再 SIGKILL。

    返回未能确认停止的 [(pid, 命令行), ...]。
    """
    remaining = list(servers)
    for pid, _command in remaining:
        try:
            os.kill(pid, signal.SIGTERM)
        except OSError:
            pass  # 已退出，或不是自己的进程
    deadline = time.monotonic() + wait_seconds
    while remaining and time.monotonic() < deadline:
        time.sleep(0.1)
        remaining = [(pid, command) for pid, command in remaining if not _process_stopped(pid)]
    for pid, _command in remaining:  # 等了还活着：强杀
        try:
            os.kill(pid, signal.SIGKILL)
        except OSError:
            pass
    if remaining:
        time.sleep(0.2)
        remaining = [(pid, command) for pid, command in remaining if not _process_stopped(pid)]
    return remaining


# ── 卸载 ───────────────────────────────────────────────────────


def ledger_home() -> Path:
    """审阅台账目录：与 skill/annotation-everything/scripts/ledger.py 的 home() 同一约定。"""
    env = os.environ.get("AE_HOME")
    return Path(env).expanduser().resolve() if env else Path.home() / ".annotation-everything"


def ledger_review_dirs(home_dir: Path) -> list:
    """读台账里登记的审阅目录路径；台账缺失或读不了时返回空列表。"""
    try:
        doc = json.loads((home_dir / "reviews.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    reviews = doc.get("reviews") if isinstance(doc, dict) else None
    dirs = []
    if isinstance(reviews, dict):
        for entry in reviews.values():
            if isinstance(entry, dict) and isinstance(entry.get("dir"), str) and entry["dir"]:
                dirs.append(entry["dir"])
    return dirs


def purge_guard(home_dir: Path) -> Optional[str]:
    """--purge 的安全检查：返回拒绝原因字符串，None 表示可以删除。"""
    if not home_dir.exists():
        return None
    resolved = home_dir.resolve()
    if resolved == Path.home().resolve() or resolved == Path(resolved.root):
        return f"台账目录异常（{home_dir}），拒绝删除"
    for review_dir in ledger_review_dirs(home_dir):
        try:
            entry = Path(review_dir).expanduser().resolve()
        except OSError:
            continue
        if entry == resolved or resolved in entry.parents:
            return (f"审阅目录 {review_dir} 在台账目录里，拒绝删除"
                    "（卸载任何情况下不动审阅目录）")
    return None


def remove_links(entries: list, skill_copy: Path) -> list:
    """删除安装记录列出（或扫描发现）的、指向技能副本的链接。

    只删软链接：指向别处的不动，已变成普通文件/目录或已消失的不动。
    返回 [(路径, 处理说明), ...] 供输出。
    """
    target = os.path.realpath(str(skill_copy))
    report = []
    for entry in entries:
        link = Path(entry["path"])
        if link.is_symlink():
            if os.path.realpath(str(link)) == target or os.readlink(str(link)) == target:
                link.unlink()
                report.append((str(link), "已删除链接"))
            else:
                report.append((str(link), "指向别处，未删除"))
        else:
            report.append((str(link), "已不是链接，未删除"))
    return report


def remove_skill_copy(skill_copy: Path) -> str:
    """删除技能副本；返回给用户看的说明。"""
    if skill_copy.is_symlink():
        skill_copy.unlink()
        return f"已删除技能副本（本身是软链接）：{skill_copy}"
    if skill_copy.exists():
        shutil.rmtree(skill_copy)
        return f"已删除技能副本：{skill_copy}"
    return f"技能副本不存在：{skill_copy}"


def cmd_uninstall(args) -> int:
    skill_copy = skill_copy_dir()
    record = read_record(skill_copy)
    # 已知链接：记录优先，缺失时回退扫描三个已知 Agent 目录（含悬空链接）
    links = [entry for entry in known_links(DoctorContext(skill_copy=skill_copy, repo=None, record=record))
             if isinstance(entry, dict) and isinstance(entry.get("path"), str)]
    installed = skill_copy.is_symlink() or skill_copy.exists()
    if not installed and not links:
        print(f"技能未安装：未找到技能副本（{skill_copy}），也未发现指向它的链接，无需卸载")
        if not args.purge:
            return 0
    servers = running_servers()
    if servers:
        print("检测到正在运行的 serve.py：")
        for pid, command in servers:
            print(f"  pid {pid}：{command}")
        if not args.stop_servers:
            print("请先停止服务，或加 --stop-servers 由本命令结束它们；已拒绝卸载", file=sys.stderr)
            return 1
        stuck = stop_servers(servers)
        if stuck:
            print(f"有 {len(stuck)} 个 serve.py 未能结束，已拒绝卸载：", file=sys.stderr)
            for pid, command in stuck:
                print(f"  pid {pid}：{command}", file=sys.stderr)
            return 1
        print("已结束全部 serve.py")
    if args.purge:  # 先做安全检查再删任何东西
        problem = purge_guard(ledger_home())
        if problem:
            print(problem, file=sys.stderr)
            return 2
    for path, action in remove_links(links, skill_copy):
        print(f"{action}：{path}")
    print(remove_skill_copy(skill_copy))
    if args.purge:
        home_dir = ledger_home()
        if home_dir.exists():
            shutil.rmtree(home_dir)
            print(f"已删除台账目录：{home_dir}")
        else:
            print(f"台账目录不存在，无需删除：{home_dir}")
    else:
        print(f"已保留审阅台账：{ledger_home()}（--purge 可一并删除；审阅目录任何情况下都不动）")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="ae.py", description=f"{APP} 统一管理命令")
    sub = parser.add_subparsers(dest="command", required=True)
    p_install = sub.add_parser("install", help="把技能本体复制为技能副本")
    p_install.add_argument("--link", default="", metavar="AGENTS",
                           help="为其他 Agent 建指向技能副本的软链接，逗号分隔：codex,agents,zcode")
    p_install.set_defaults(handler=cmd_install)
    p_doctor = sub.add_parser("doctor", help="体检：检查安装状态，给出级别与修复命令")
    p_doctor.add_argument("--json", action="store_true", dest="as_json",
                          help="输出结构化的体检结果（JSON）")
    p_doctor.set_defaults(handler=cmd_doctor)
    p_uninstall = sub.add_parser("uninstall", help="删除技能副本与指向它的链接，默认保留审阅台账")
    p_uninstall.add_argument("--purge", action="store_true",
                             help="同时删除台账目录（AE_HOME，默认 ~/.annotation-everything）；"
                                  "审阅目录任何情况下都不动")
    p_uninstall.add_argument("--stop-servers", action="store_true",
                             help="先结束正在运行的 serve.py 再卸载（默认检测到就拒绝卸载）")
    p_uninstall.set_defaults(handler=cmd_uninstall)
    return parser


def main(argv=None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.handler(args)


if __name__ == "__main__":
    sys.exit(main())
