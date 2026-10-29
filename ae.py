#!/usr/bin/env python3
"""annotation-everything 统一管理命令：安装技能副本、体检、修复。

用法：
  python3 ae.py install            # 把技能本体复制为技能副本（Claude 技能目录，CLAUDE_SKILLS_DIR 可覆盖）
  python3 ae.py install --link codex,agents,zcode
                                    # 同时在这些 Agent 的技能目录里建指向技能副本的软链接
  python3 ae.py doctor [--json]    # 体检：逐项检查并给出级别与修复命令
  python3 ae.py repair [--dry-run] [--forget-stale] [--stop-orphans]
                                    # 修复体检发现的问题：先打印计划；安全项自动修，
                                    # 删除台账失效记录与结束残留服务需显式参数
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
import http.client
import json
import os
import platform
import re
import shlex
import shutil
import signal
import socket
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
CATEGORY_RUNTIME = "运行状态"
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
    # 运行状态检查的可注入点（默认 None = 本机真实来源；测试用来把范围限定在自建的台账、进程与端口）
    ledger_home: Optional[Path] = None             # 台账目录；默认 AE_HOME 或 ~/.annotation-everything
    process_lister: Optional[Callable[[], list]] = None   # () -> [(pid, 命令行)]；默认 ps 全机枚举
    port_probe: Optional[Callable[[int], Optional[dict]]] = None  # 端口探测；默认 probe_port
    cwd: Optional[Path] = None  # 运行位置；默认 Path.cwd()，与来源仓库是否可用无关


class CommandError(Exception):
    """命令的输入或本机元数据损坏；由统一入口报告并返回 2。"""


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


def _file_identity(path: Path):
    try:
        st = path.stat()
    except OSError:
        return None
    return (st.st_dev, st.st_ino)


def _lineage_identities(path: Path) -> set:
    """路径自身与各级父目录中已存在者的 (设备, inode)；字面父链与解析软链接后的父链都算。"""
    found = set()
    for chain in (Path(os.path.abspath(path)), path.resolve()):
        found.update(_file_identity(p) for p in (chain, *chain.parents))
    found.discard(None)
    return found


def paths_overlap(a: Path, b: Path) -> bool:
    """两个路径指向同一目录或互为父子目录。

    按 (设备, inode) 比较而不比较路径字符串：不区分大小写的文件系统和软链接都绕不过去。
    """
    ia, ib = _file_identity(a), _file_identity(b)
    return (ia is not None and ia in _lineage_identities(b)) or \
        (ib is not None and ib in _lineage_identities(a))


def copy_skill(src: Path, dst: Path) -> None:
    """把技能本体复制为技能副本：先清掉旧副本，保证重复运行结果一致。

    副本位置与本体重叠（如 CLAUDE_SKILLS_DIR 指进了仓库）时拒绝，避免清掉本体。
    """
    if paths_overlap(src, dst):
        raise CommandError(f"技能副本位置 {dst} 与技能本体 {src} 重叠，拒绝覆盖；"
                           "请检查 CLAUDE_SKILLS_DIR")

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
    try:
        previous = read_record(dst)  # 在清掉旧副本前读出已记录的链接
    except CommandError:
        # 重装可以修复损坏记录；已知位置的链接仍通过扫描保留。
        previous = {"links": scan_links(dst)}
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
    """读安装记录：缺失时回退扫描，存在但不可读或结构损坏时报告命令错误。"""
    path = skill_copy / RECORD_NAME
    try:
        record = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as exc:
        raise CommandError(f"安装记录 {path} 读不了（{exc}），可运行 ae.py install 重建") from None
    valid = isinstance(record, dict)
    if valid:
        valid = all(isinstance(record[key], str)
                    for key in ("repo", "commit", "version", "installed_at") if key in record)
        links = record.get("links", [])
        valid = valid and isinstance(links, list) and all(
            isinstance(entry, dict) and isinstance(entry.get("path"), str) and entry["path"]
            and isinstance(entry.get("agent", ""), str) for entry in links)
    if not valid:
        raise CommandError(f"安装记录 {path} 结构不对，可运行 ae.py install 重建")
    return record


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
                           f"Python {version} 低于 3.9，技能无法运行",
                           tool_fix("python@3.12" if sys.platform == "darwin" else "python3.12"))
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


# ── 体检：运行状态（审阅台账、专属端口、残留服务） ──────────────────
# 只读台账与进程/系统元数据：台账本身、审阅目录里的 review.json（只取审阅标识）与
# .annotation-server.json（pid/port/dir），绝不读批注或材料内容。

STATUS_FILE = ".annotation-server.json"   # 与技能本体 serve.py 的运行状态文件同名，只含 pid/port/dir 等元数据
HOST = "127.0.0.1"
# serve.py 的这些命令行选项各带一个值，解析目录参数时要连值一起跳过
SERVE_OPTIONS_WITH_VALUE = ("--port", "--claim-port", "--on-handoff")
SERVE_MAINTENANCE_COMMANDS = ("check", "status", "list", "forget")   # 不起服务的 serve.py 子命令

_skill_ledger = None


def skill_ledger_module():
    """加载技能本体的 ledger.py：台账格式、专属端口池与失效判定的单一事实来源。"""
    global _skill_ledger
    if _skill_ledger is None:
        scripts = Path(__file__).resolve().parent / SKILL_SRC / "scripts"
        sys.path.insert(0, str(scripts))
        import ledger
        _skill_ledger = ledger
    return _skill_ledger


def load_ledger(ctx: DoctorContext):
    """读台账，返回 (entries, error)：error 为 None 即可读（文件不存在视为空台账）。"""
    try:
        mod = skill_ledger_module()
    except ImportError:
        return None, f"技能本体不完整（找不到 {SKILL_SRC / 'scripts' / 'ledger.py'}），读不了审阅台账"
    home = Path(ctx.ledger_home) if ctx.ledger_home is not None else mod.home()
    path = home / mod.LEDGER_FILE
    if not path.exists():
        return {}, None
    try:
        # 复用 ledger.py 的读取、非法条目过滤与报错语义，保证与 serve.py 看到的一致
        return mod._read(path)["reviews"], None
    except mod.LedgerError as e:
        return None, str(e)


def repair_fix(repo: Optional[Path], *flags: str) -> str:
    target = repo / "ae.py" if repo else Path("ae.py")
    return f"python3 {target} repair " + " ".join(flags)


def probe_port(port: int, timeout: float = 1.0):
    """探测端口上的进程是不是本工具的服务；返回 None 表示端口空闲。

    与技能本体 serve.py 的 probe() 行为一致：请求必须带 Host: 127.0.0.1:<端口> 头，
    否则本工具的正常服务也会按 Host 校验返回 421，被误判成外部占用。
    """
    try:
        with socket.create_connection((HOST, port), timeout=timeout):
            pass
    except OSError:
        return None
    try:
        conn = http.client.HTTPConnection(HOST, port, timeout=timeout)
        conn.request("GET", "/api/health", headers={"Host": f"{HOST}:{port}"})
        response = conn.getresponse()
        data = json.loads(response.read() or b"{}") if response.status == 200 else {}
        conn.close()
        return data if isinstance(data, dict) and data.get("app") == APP else {"app": "other"}
    except (OSError, ValueError, http.client.HTTPException):
        return {"app": "other"}


def list_processes():
    """本机全部进程的 (pid, 命令行)；ps 不可用时返回空列表（检查项按「未发现」处理）。"""
    try:
        done = subprocess.run(["ps", "-ww", "-eo", "pid=,command="],   # -ww 不按终端宽度截断；格式须合并成一个参数
                              capture_output=True, text=True, timeout=15)
    except (OSError, subprocess.TimeoutExpired):
        return []
    procs = []
    for line in done.stdout.splitlines():
        head = line.strip().split(None, 1)
        if len(head) == 2 and head[0].isdigit():
            procs.append((int(head[0]), head[1]))
    return procs


def command_tokens(cmdline: str) -> list:
    try:
        return shlex.split(cmdline)
    except ValueError:
        return []


def serve_script_index(cmdline: str, pid: Optional[int] = None) -> Optional[int]:
    """只识别 Python 实际执行的本仓库或技能副本脚本；不匹配参数中的脚本名。"""
    tokens = command_tokens(cmdline)
    if not tokens or not re.fullmatch(r"python(?:\d+(?:\.\d+)*)?|pypy\d*",
                                      Path(tokens[0]).name.lower()):
        return None
    index = 1
    while index < len(tokens):
        token = tokens[index]
        if token.startswith(("-c", "-m")) or token == "-":
            return None
        if token == "--":
            index += 1
            break
        if token in ("-W", "-X"):
            index += 2
        elif token.startswith("-"):
            index += 1
        else:
            break
    if index >= len(tokens):
        return None
    script = Path(tokens[index])
    if not script.is_absolute():
        cwd = process_cwd(pid) if pid is not None else str(Path.cwd())
        if cwd is None:
            return None
        script = Path(cwd) / script
    trusted = (Path(__file__).resolve().parent / SERVE_PY,
               skill_copy_dir() / "scripts" / "serve.py")
    try:
        return index if any(script.resolve() == path.resolve() for path in trusted) else None
    except (OSError, ValueError):
        return None


def serve_dir_argument(cmdline: str, pid: Optional[int] = None) -> Optional[str]:
    """已核实脚本的审阅目录参数；排除维护子命令。

    子命令识别与 serve.py main() 一致：只看脚本后的第一个参数；`serve` 后面才是目录。
    不是 serve.py 进程、或没有目录参数时返回 None。
    """
    index = serve_script_index(cmdline, pid)
    if index is None:
        return None
    args = command_tokens(cmdline)[index + 1:]
    if args[:1] and args[0] in SERVE_MAINTENANCE_COMMANDS:
        return None
    if args[:1] == ["serve"]:
        args = args[1:]
    i = 0
    while i < len(args):
        token = args[i]
        if token in SERVE_OPTIONS_WITH_VALUE:
            i += 2
        elif token.startswith("-"):
            i += 1
        else:
            return token
    return None


def process_cwd(pid: int) -> Optional[str]:
    """进程的当前目录（解析相对的目录参数用）；拿不到返回 None。"""
    if sys.platform == "darwin":
        try:
            done = subprocess.run(["lsof", "-a", "-p", str(pid), "-d", "cwd", "-Fn"],
                                  capture_output=True, text=True, timeout=10)
        except (OSError, subprocess.TimeoutExpired):
            return None
        for line in done.stdout.splitlines():
            if line.startswith("n"):
                return line[1:]
        return None
    try:
        return os.readlink(f"/proc/{pid}/cwd")
    except OSError:
        return None


def listening_port_of(pid: int) -> Optional[int]:
    """只读进程的 TCP 监听元数据，目录已删时仍可获得实际端口。"""
    if sys.platform.startswith("linux"):
        try:
            inodes = {os.readlink(fd)[8:-1] for fd in Path(f"/proc/{pid}/fd").iterdir()
                      if os.readlink(fd).startswith("socket:[")}
            for family in ("tcp", "tcp6"):
                for line in Path(f"/proc/{pid}/net/{family}").read_text().splitlines()[1:]:
                    fields = line.split()
                    if len(fields) > 9 and fields[3] == "0A" and fields[9] in inodes:
                        return int(fields[1].rsplit(":", 1)[1], 16)
        except (OSError, ValueError):
            pass
    try:
        done = subprocess.run(["lsof", "-a", "-P", "-n", "-p", str(pid), "-iTCP", "-sTCP:LISTEN", "-Fn"],
                              capture_output=True, text=True, timeout=10)
        for line in done.stdout.splitlines():
            if line.startswith("n") and ":" in line:
                port = int(line.rsplit(":", 1)[1])
                if 1 <= port <= 65535:
                    return port
    except (OSError, ValueError, subprocess.TimeoutExpired):
        pass
    return None


def server_port_of(pid: int, review_dir: Optional[Path], cmdline: str = "") -> Optional[int]:
    """状态文件优先，其次实际监听端口，最后保留显式请求端口作为排障证据。"""
    try:
        info = json.loads((review_dir / STATUS_FILE).read_text(encoding="utf-8")) if review_dir else {}
    except (OSError, ValueError):
        info = {}
    port = info.get("port") if isinstance(info, dict) else None
    if isinstance(info, dict) and info.get("pid") == pid and isinstance(port, int) \
            and not isinstance(port, bool) and 1 <= port <= 65535:
        return port
    port = listening_port_of(pid)
    if port is not None:
        return port
    tokens = command_tokens(cmdline)
    for index, token in enumerate(tokens):
        raw = token.partition("=")[2] if token.startswith("--port=") else (
            tokens[index + 1] if token == "--port" and index + 1 < len(tokens) else "")
        if raw.isdigit() and 1 <= int(raw) <= 65535:
            return int(raw)
    return None


def _resolve_dir_argument(arg: str, entries: dict, cwd: Optional[str]) -> Optional[Path]:
    """把 serve.py 命令行里的目录参数解析为审阅目录，顺序与 ledger.resolve_dir 一致：
    按进程 cwd 已存在的路径直接用；带路径分隔符、以 . 或 ~ 开头、不像审阅标识的按路径处理；
    其余当审阅标识（前缀）从台账查，查不到或有歧义返回 None（按「不在台账里」处理）。
    进程 cwd 未知（None）时相对路径原样返回。"""
    path = Path(arg).expanduser()
    if not path.is_absolute() and cwd:
        path = Path(cwd) / path
    key = arg.strip().lower()
    if (path.is_absolute() and path.exists()) or "/" in arg or os.sep in arg \
            or arg.startswith((".", "~")) or not skill_ledger_module().RID_PREFIX.fullmatch(key):
        return path
    hits = [rid for rid in entries if rid.startswith(key)]
    if len(hits) != 1:
        return None
    return Path(entries[hits[0]]["dir"])


def _same_dir(a: Path, b: Path) -> bool:
    try:
        return a.resolve() == b.resolve()
    except OSError:
        return a == b


def check_ledger_readable(ctx: DoctorContext) -> CheckResult:
    entries, error = load_ledger(ctx)
    if error is not None:
        home = Path(ctx.ledger_home) if ctx.ledger_home is not None else ledger_home()
        return CheckResult("ledger-readable", CATEGORY_RUNTIME, LEVEL_FAIL, error,
                           f"python3 -m json.tool {shlex.quote(str(home / 'reviews.json'))}")
    return CheckResult("ledger-readable", CATEGORY_RUNTIME, LEVEL_PASS,
                       f"审阅台账可读（{len(entries)} 份审阅）")


def find_stale_entries(entries: dict) -> list:
    """台账里的失效记录 [(审阅标识, 端口)]：目录不在（missing）或标识对不上（mismatch）。"""
    mod = skill_ledger_module()
    return [(rid, e.get("port")) for rid, e in entries.items() if mod.entry_state(rid, e) != "ok"]


def check_ledger_stale(ctx: DoctorContext) -> CheckResult:
    entries, error = load_ledger(ctx)
    if error is not None:
        return CheckResult("ledger-stale", CATEGORY_RUNTIME, LEVEL_PASS, "台账不可读，未检查失效记录")
    stale = find_stale_entries(entries)
    if not stale:
        return CheckResult("ledger-stale", CATEGORY_RUNTIME, LEVEL_PASS, "台账里没有失效记录")
    detail = "、".join(f"{rid[:8]}（端口 {port if port else '未分配'}）" for rid, port in stale)
    return CheckResult("ledger-stale", CATEGORY_RUNTIME, LEVEL_WARN,
                       f"台账里有 {len(stale)} 条失效记录：{detail}",
                       repair_fix(ctx.repo, "--forget-stale"))


def check_port_pool(ctx: DoctorContext) -> CheckResult:
    """只探测台账里已分配给审阅的专属端口；池里未分配的端口被占用无害（分配时会跳过）。"""
    entries, error = load_ledger(ctx)
    if error is not None:
        return CheckResult("port-pool", CATEGORY_RUNTIME, LEVEL_PASS, "台账不可读，未检查专属端口")
    assigned = sorted({e["port"] for e in entries.values()
                       if isinstance(e.get("port"), int) and not isinstance(e.get("port"), bool)
                       and 1 <= e["port"] <= 65535})
    if not assigned:
        return CheckResult("port-pool", CATEGORY_RUNTIME, LEVEL_PASS, "台账里没有已分配的专属端口")
    probe = ctx.port_probe if ctx.port_probe is not None else probe_port
    foreign = [port for port in assigned
               if (found := probe(port)) is not None and found.get("app") != APP]
    if foreign:
        ports = "、".join(str(port) for port in foreign)
        return CheckResult("port-pool", CATEGORY_RUNTIME, LEVEL_FAIL,
                           f"专属端口被其他进程占用：{ports}",
                           "lsof -nP -iTCP:" + ",".join(map(str, foreign)) + " -sTCP:LISTEN")
    return CheckResult("port-pool", CATEGORY_RUNTIME, LEVEL_PASS,
                       f"{len(assigned)} 个专属端口未被其他进程占用")


def served_review_dir(pid: int, probe) -> Optional[Path]:
    """服务自报的审阅目录：经进程的监听端口请求 /api/health，pid 对得上才采信。

    ps 输出不保留引号，含空格或缩写选项的命令行解析不可靠；能问到服务本身时以它为准。
    """
    port = listening_port_of(pid)
    info = probe(port) if port is not None else None
    if isinstance(info, dict) and info.get("app") == APP and info.get("pid") == pid \
            and isinstance(info.get("dir"), str) and info["dir"]:
        return Path(info["dir"])
    return None


def find_orphan_servers(entries: dict, lister, probe=None) -> list:
    """枚举残留服务，返回 [(pid, 端口, 审阅目录)]：运行中的 serve.py，托管的审阅目录
    不在台账里或已不存在。端口/目录定位不到时为 None（如目录已删、标识查不到）。"""
    probe = probe if probe is not None else probe_port
    orphans = []
    for pid, cmdline in lister():
        arg = serve_dir_argument(cmdline, pid)
        if arg is None:
            continue
        review_dir = served_review_dir(pid, probe)
        if review_dir is None:
            cwd = None if Path(arg).expanduser().is_absolute() else process_cwd(pid)
            review_dir = _resolve_dir_argument(arg, entries, cwd)
        if review_dir is None or not review_dir.is_dir() \
                or not any(_same_dir(review_dir, Path(e["dir"])) for e in entries.values()):
            port = server_port_of(pid, review_dir, cmdline)
            orphans.append((pid, port, review_dir))
    return orphans


def check_orphan_servers(ctx: DoctorContext) -> CheckResult:
    """残留服务：运行中的 serve.py，托管的审阅目录不在台账里或已不存在。"""
    entries, error = load_ledger(ctx)
    if error is not None:
        return CheckResult("orphan-servers", CATEGORY_RUNTIME, LEVEL_PASS, "台账不可读，未检查残留服务")
    lister = ctx.process_lister if ctx.process_lister is not None else list_processes
    orphans = find_orphan_servers(entries, lister, ctx.port_probe)
    if orphans:
        detail = "；".join(f"pid {pid}（端口 {port if port else '未知'}）" for pid, port, _ in orphans)
        return CheckResult("orphan-servers", CATEGORY_RUNTIME, LEVEL_FAIL,
                           f"发现 {len(orphans)} 个残留服务（托管的审阅不在台账里或目录已不存在）：{detail}",
                           repair_fix(ctx.repo, "--stop-orphans"))
    running = sum(1 for pid, cmdline in lister() if serve_dir_argument(cmdline, pid) is not None)
    suffix = f"（{running} 个 serve.py 进程均在台账内）" if running else ""
    return CheckResult("orphan-servers", CATEGORY_RUNTIME, LEVEL_PASS, f"未发现残留服务{suffix}")


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
          check_ledger_readable, check_ledger_stale, check_port_pool, check_orphan_servers,
          check_fc_list, check_pdffonts)

# 仅在当前工作目录位于来源仓库内时运行；能定位来源仓库不等于正在仓库内运行。
REPO_CHECKS = (check_git_hooks, check_chrome)


def run_doctor(ctx: DoctorContext):
    cwd = (ctx.cwd or Path.cwd()).resolve()
    repo = ctx.repo.resolve() if ctx.repo is not None else None
    inside_repo = repo is not None and (cwd == repo or repo in cwd.parents)
    checks = CHECKS + REPO_CHECKS if inside_repo else CHECKS
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


# ── 修复 ───────────────────────────────────────────────────────
# 修复计划由体检结果（同一组检查项）生成，执行前先打印；安全项自动修（重新同步技能副本、
# 修复悬空链接、启用 git 钩子）；删除台账失效记录与结束残留服务是破坏性操作，必须加显式参数。

# 三个「技能副本」检查项合并为一个重新同步动作
REPAIR_COPY_CODES = ("skill-copy-present", "skill-copy-sync", "skill-copy-version")
# 计划项类别 → 覆盖的检查项编号（用于计算「不能自动修复」的剩余问题）
REPAIR_KIND_CODES = {
    "resync-copy": REPAIR_COPY_CODES,
    "fix-links": ("skill-links",),
    "git-hooks": ("git-hooks",),
    "forget-stale": ("ledger-stale",),
    "stop-orphans": ("orphan-servers",),
}


@dataclasses.dataclass
class PlanItem:
    """修复计划里的一项；flag 非空表示破坏性操作，需要对应显式参数才执行。"""
    kind: str        # 动作类别：resync-copy / fix-links / git-hooks / forget-stale / stop-orphans
    problem: str     # 问题描述（取体检项的说明）
    action: str      # 修复动作（给用户看）
    flag: str = ""   # 需要的显式参数；空 = 安全项，自动执行


def plan_from_results(results) -> list:
    """由体检结果生成修复计划：只收 ae.py 能修的项，其余问题见体检输出。"""
    by_code = {r.code: r for r in results}
    plan = []
    copy_broken = next((by_code[code] for code in REPAIR_COPY_CODES
                        if code in by_code and by_code[code].level == LEVEL_FAIL), None)
    if copy_broken is not None:
        plan.append(PlanItem("resync-copy", copy_broken.message,
                             "重新同步技能副本（覆盖副本内的手工改动，语义同 install）"))
    r = by_code.get("skill-links")
    if r is not None and r.level == LEVEL_FAIL:
        plan.append(PlanItem("fix-links", r.message, "修复指向技能副本的链接"))
    r = by_code.get("git-hooks")
    if r is not None and r.level in (LEVEL_WARN, LEVEL_FAIL):
        plan.append(PlanItem("git-hooks", r.message, "启用 git 钩子"))
    r = by_code.get("ledger-stale")
    if r is not None and r.level in (LEVEL_WARN, LEVEL_FAIL):
        plan.append(PlanItem("forget-stale", r.message, "删除台账失效记录并释放专属端口",
                             "--forget-stale"))
    r = by_code.get("orphan-servers")
    if r is not None and r.level == LEVEL_FAIL:
        plan.append(PlanItem("stop-orphans", r.message, "结束残留服务（SIGTERM 优雅停止）",
                             "--stop-orphans"))
    return plan


def recreate_link(link: Path, copy_dir: Path) -> None:
    """把链接位置重建为指向技能副本的软链接（悬空、指向别处、已删除统一走这里）。"""
    if link.is_symlink():
        link.unlink()
    link.parent.mkdir(parents=True, exist_ok=True)
    os.symlink(str(copy_dir.resolve()), str(link))


def repair_links(ctx: DoctorContext) -> tuple:
    """修复指向技能副本的链接；返回（已重建的链接条目, 无法修复的说明）。"""
    if not ctx.skill_copy.is_dir():
        return [], [f"技能副本缺失（{ctx.skill_copy}），先重新同步技能副本再修链接"]
    created, stuck = [], []
    for entry in known_links(ctx):
        if not (isinstance(entry, dict) and isinstance(entry.get("path"), str)):
            continue
        link = Path(entry["path"])
        if _link_status(link, ctx.skill_copy) == "正常":
            continue
        if link.exists() and not link.is_symlink():
            # 位置被非链接内容占用：与 install 同样不覆盖，留给用户手工处理
            stuck.append(f"{entry.get('agent') or '未知'}（{entry['path']}）：位置已是普通文件或目录，请手工处理")
            continue
        recreate_link(link, ctx.skill_copy)
        created.append({"agent": entry.get("agent", ""), "path": entry["path"]})
    return created, stuck


def forget_stale_entries(entries: dict) -> list:
    """删除台账里的失效记录并释放专属端口（用技能本体 ledger.py 的 forget 官方 API）。

    动手前按当前台账复核：条目已不在、或已恢复有效（如外接盘重新挂上）时跳过，不误删。
    返回 [(审阅标识, 端口, 说明)]，说明为 None 表示已删除。
    """
    mod = skill_ledger_module()
    done = []
    for rid, port in find_stale_entries(entries):
        try:
            current = mod.entries()
        except (mod.LedgerError, OSError) as e:
            done.append((rid, port, f"台账读不了，未删除（{e}）"))
            continue
        entry = current.get(rid)
        if entry is None:
            done.append((rid, port, "已不在台账里，无需删除"))
        elif mod.entry_state(rid, entry) == "ok":
            done.append((rid, port, "已恢复有效，跳过"))
        else:
            try:
                mod.forget(rid)
                done.append((rid, port, None))
            except (mod.LedgerError, OSError) as e:
                done.append((rid, port, f"未删除（{e}）"))
    return done


def stop_orphan_servers(entries: dict, lister) -> list:
    """结束残留服务：先按残留定义筛选，再用共享 stop_servers（SIGTERM 优雅停止，等不到 SIGKILL）。
    返回 [(pid, 端口, 说明)]，说明为 None 表示已停止。"""
    processes = lister()
    orphans = find_orphan_servers(entries, lambda: processes)
    if not orphans:
        return []
    pids = {pid for pid, _, _ in orphans}
    stuck = {pid for pid, _command in stop_servers([(pid, command) for pid, command in processes if pid in pids])}
    return [(pid, port, None if pid not in stuck else "未能确认服务身份或停止状态，未强行结束")
            for pid, port, _dir in orphans]


def execute_plan(plan: list, ctx: DoctorContext,
                 forget_stale: bool = False, stop_orphans: bool = False) -> tuple:
    """执行修复计划：破坏性项必须给出对应参数才动手。返回（报告行列表, 是否执行了动作）。"""
    lines, acted = [], False
    flag_given = {"--forget-stale": forget_stale, "--stop-orphans": stop_orphans}
    resynced = False
    for item in plan:
        if item.flag and not flag_given.get(item.flag):
            continue
        acted = True
        if item.kind == "resync-copy":
            src = ctx.repo / SKILL_SRC if ctx.repo is not None else None
            if src is None or not src.is_dir():
                lines.append(f"未重新同步技能副本：找不到技能本体（{src or '未定位到来源仓库'}）")
                continue
            copy_skill(src, ctx.skill_copy)
            resynced = True
            lines.append(f"已重新同步技能副本到 {ctx.skill_copy}")
            # 副本回来了：悬空链接多半自愈，其余重建；随后按 install 的合并语义补写安装记录
            created, stuck = repair_links(ctx)
            previous = ctx.record if isinstance(ctx.record, dict) \
                else {"links": scan_links(ctx.skill_copy)}
            write_record(ctx.skill_copy, ctx.repo, merged_links(previous, ctx.skill_copy, created))
            lines.extend(_link_report(created, stuck))
        elif item.kind == "fix-links":
            if resynced:
                continue                    # 重新同步时已一并修过链接
            created, stuck = repair_links(ctx)
            lines.extend(_link_report(created, stuck))
        elif item.kind == "git-hooks":
            lines.extend(_enable_git_hooks(ctx.repo))
        elif item.kind == "forget-stale":
            entries, error = load_ledger(ctx)
            if error is not None:
                lines.append(f"未删除台账失效记录：{error}")
                continue
            for rid, port, note in forget_stale_entries(entries):
                lines.append(f"台账失效记录 {rid[:8]}（{_port_text(port)}）：{note}"
                             if note is not None
                             else f"已删除台账失效记录 {rid[:8]}（{_port_text(port)}，已释放）")
        elif item.kind == "stop-orphans":
            entries, error = load_ledger(ctx)
            if error is not None:
                lines.append(f"未结束残留服务：{error}")
                continue
            lister = ctx.process_lister if ctx.process_lister is not None else list_processes
            for pid, port, note in stop_orphan_servers(entries, lister):
                detail = f"，端口 {port}" if port else ""
                lines.append(f"已结束残留服务 pid {pid}{detail}" if note is None
                             else f"残留服务 pid {pid}{detail}：{note}")
    return lines, acted


def _port_text(port) -> str:
    return f"端口 {port}" if port else "未分配端口"


def _link_report(created: list, stuck: list) -> list:
    lines = [f"已重建链接：{entry.get('agent') or entry['path']}（{entry['path']}）"
             for entry in created]
    lines.extend(f"无法修复链接 {note}" for note in stuck)
    return lines


def _enable_git_hooks(repo: Optional[Path]) -> list:
    """启用 git 钩子：执行仓库自带的 tools/setup-dev.sh（与其修复命令同一脚本）。"""
    script = repo / "tools" / "setup-dev.sh" if repo is not None else None
    if script is None or not script.is_file():
        return [f"未启用 git 钩子：找不到 {script}"]
    try:
        # 脚本带 bash 语法（set -o pipefail），显式用 bash 执行
        done = subprocess.run(["bash", str(script)], capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.TimeoutExpired) as e:
        return [f"启用 git 钩子失败：{e}"]
    if done.returncode == 0:
        return ["已启用 git 钩子（core.hooksPath = tools/git-hooks）"]
    return [f"启用 git 钩子失败：{(done.stderr or done.stdout).strip()}"]


def print_plan(plan: list, forget_stale: bool = False, stop_orphans: bool = False,
               dry_run: bool = False) -> None:
    print("修复计划：")
    if not plan:
        print("  没有发现可修复的问题")
        return
    given = {"--forget-stale": forget_stale, "--stop-orphans": stop_orphans}
    for index, item in enumerate(plan, 1):
        if item.flag and not given.get(item.flag):
            note = f"（破坏性操作，需要参数 {item.flag}；本次未执行）"
        elif dry_run:
            note = "（--dry-run，只打印不执行）"
        else:
            note = ""
        print(f"  {index}. {item.problem}")
        print(f"     → {item.action}{note}")


def _print_unrepairable(results, plan) -> None:
    covered = {code for item in plan for code in REPAIR_KIND_CODES.get(item.kind, ())}
    rest = [r.code for r in results if r.level != LEVEL_PASS and r.code not in covered]
    if rest:
        print(f"  （另有 {len(rest)} 项不能自动修复：{'、'.join(rest)}，见体检输出）")


def _fresh_context() -> DoctorContext:
    skill_copy = skill_copy_dir()
    record = read_record(skill_copy)
    return DoctorContext(skill_copy=skill_copy, repo=resolve_repo(record), record=record)


def cmd_repair(args) -> int:
    ctx = _fresh_context()
    results = run_doctor(ctx)
    plan = plan_from_results(results)
    print_plan(plan, forget_stale=args.forget_stale, stop_orphans=args.stop_orphans,
               dry_run=args.dry_run)
    _print_unrepairable(results, plan)
    if args.dry_run:
        return 1 if any(r.level == LEVEL_FAIL for r in results) else 0
    lines, acted = execute_plan(plan, ctx, forget_stale=args.forget_stale,
                                stop_orphans=args.stop_orphans)
    if lines:
        print("\n执行修复：")
        for line in lines:
            print(f"  - {line}")
    after = run_doctor(_fresh_context()) if acted else results
    print("\n修复后体检：")
    print_doctor(after)
    return 1 if any(r.level == LEVEL_FAIL for r in after) else 0
# ── 运行中的服务（卸载用；进程枚举与识别复用运行状态体检的设施） ──


def running_servers(lister: Optional[Callable[[], list]] = None) -> list:
    """正在运行的 serve.py 进程：[(pid, 命令行), ...]，与台账比对无关。

    只接受 Python 正在执行的本仓库或技能副本脚本，排除提及脚本名的其他程序。
    lister 返回 [(pid, 命令行), ...]，测试可注入；默认用 list_processes 枚举。
    """
    if lister is None:
        lister = list_processes
    return [(pid, command) for pid, command in lister()
            if serve_dir_argument(command, pid) is not None]


def _process_stopped(pid: int) -> bool:
    """进程是否已停止（查不到，或只剩僵尸）；无法查询时不宣称已停止。"""
    try:
        done = subprocess.run(["ps", "-p", str(pid), "-o", "stat="],
                              capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        return False
    if done.returncode != 0:
        return True
    state = done.stdout.strip().upper()
    return not state or state.startswith("Z")


def server_identity(pid: int, command: str) -> Optional[tuple]:
    index = serve_script_index(command, pid)
    if index is None or serve_dir_argument(command, pid) is None:
        return None
    tokens = command_tokens(command)
    script = Path(tokens[index])
    if not script.is_absolute():
        cwd = process_cwd(pid)
        if cwd is None:
            return None
        script = Path(cwd) / script
    return (str(script.resolve()), *tokens[index + 1:])


def same_server_process(pid: int, command: str) -> bool:
    """每次发信号前重新读命令行，拒绝已换身份或无法核实的 PID。"""
    expected = server_identity(pid, command)
    if expected is None or pid <= 0 or pid == os.getpid():
        return False
    try:
        current = subprocess.run(["ps", "-ww", "-p", str(pid), "-o", "command="],
                                 capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        return False
    return current.returncode == 0 and server_identity(pid, current.stdout.strip()) == expected


def stop_servers(servers: list, wait_seconds: float = 5.0) -> list:
    """结束 serve.py 进程：先 SIGTERM（serve.py 收到后优雅退出），等不到再 SIGKILL。

    返回未能确认停止的 [(pid, 命令行), ...]。
    """
    remaining, refused = [], []
    for pid, command in servers:
        if _process_stopped(pid):
            continue
        if not same_server_process(pid, command):
            refused.append((pid, command))
            continue
        try:
            os.kill(pid, signal.SIGTERM)
            remaining.append((pid, command))
        except OSError:
            if not _process_stopped(pid):
                refused.append((pid, command))
    deadline = time.monotonic() + wait_seconds
    while remaining and time.monotonic() < deadline:
        time.sleep(0.1)
        remaining = [(pid, command) for pid, command in remaining if not _process_stopped(pid)]
    for pid, command in remaining:  # 等了还活着：再次核实身份，才可强杀
        if not same_server_process(pid, command):
            refused.append((pid, command))
            continue
        try:
            os.kill(pid, signal.SIGKILL)
        except OSError:
            pass
    if remaining:
        time.sleep(0.2)
        remaining = [(pid, command) for pid, command in remaining if not _process_stopped(pid)]
    return list(dict.fromkeys(refused + remaining))


# ── 卸载 ───────────────────────────────────────────────────────


def ledger_home() -> Path:
    """审阅台账目录：与 skill/annotation-everything/scripts/ledger.py 的 home() 同一约定。"""
    env = os.environ.get("AE_HOME")
    return Path(env).expanduser().resolve() if env else Path.home() / ".annotation-everything"


def ledger_review_dirs(home_dir: Path) -> list:
    """purge 前严格读取所有目录证据；损坏条目不能按空台账处理。"""
    try:
        doc = json.loads((home_dir / "reviews.json").read_text(encoding="utf-8"))
    except FileNotFoundError:
        return []
    except (OSError, ValueError) as exc:
        raise CommandError(f"台账读不了，拒绝删除：{exc}") from None
    reviews = doc.get("reviews") if isinstance(doc, dict) else None
    if not isinstance(reviews, dict):
        raise CommandError("台账结构不对，拒绝删除")
    dirs = []
    for entry in reviews.values():
        if not isinstance(entry, dict) or not isinstance(entry.get("dir"), str) or not entry["dir"]:
            raise CommandError("台账目录记录不完整，拒绝删除")
        dirs.append(entry["dir"])
    return dirs


def purge_guard(home_dir: Path) -> Optional[str]:
    """--purge 的安全检查：返回拒绝原因字符串，None 表示可以删除。"""
    if not home_dir.exists():
        return None
    resolved = home_dir.resolve()
    if resolved == Path.home().resolve() or resolved == Path(resolved.root):
        return f"台账目录异常（{home_dir}），拒绝删除"
    try:
        review_dirs = ledger_review_dirs(home_dir)
    except CommandError as exc:
        return str(exc)
    for review_dir in review_dirs:
        try:
            entry = Path(review_dir).expanduser().resolve()
        except (OSError, ValueError):
            return "审阅目录无法解析，拒绝删除"
        if entry == resolved or resolved in entry.parents or entry in resolved.parents:
            return (f"审阅目录 {review_dir} 在台账目录里，拒绝删除"
                    "（卸载任何情况下不动审阅目录）")
    for child in home_dir.iterdir():
        if not is_ledger_file(child.name) or child.is_symlink() or not child.is_file():
            return f"台账目录包含非台账内容 {child.name}，拒绝删除（可能是未登记的审阅目录）"
    return None


def is_ledger_file(name: str) -> bool:
    """台账目录里的已知文件：台账、锁，以及写入中断时留下的临时文件（ledger.py 的 mkstemp）。"""
    return name in ("reviews.json", ".reviews.lock") or \
        (name.startswith(".reviews-") and name.endswith(".tmp"))


def purge_ledger(home_dir: Path) -> None:
    """只删除已知台账文件；新出现的目录或未知文件由 rmdir 拒绝，绝不递归删除。"""
    for child in home_dir.iterdir():
        if is_ledger_file(child.name) and child.is_file() and not child.is_symlink():
            child.unlink()
    home_dir.rmdir()


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


def ensure_not_skill_source(skill_copy: Path) -> None:
    """技能副本（非软链接）与本仓库的技能本体重叠时拒绝删除。"""
    source = Path(__file__).resolve().parent / SKILL_SRC
    if not skill_copy.is_symlink() and skill_copy.exists() and paths_overlap(source, skill_copy):
        raise CommandError(f"技能副本位置 {skill_copy} 与技能本体 {source} 重叠，拒绝删除；"
                           "请检查 CLAUDE_SKILLS_DIR")


def remove_skill_copy(skill_copy: Path) -> str:
    """删除技能副本；返回给用户看的说明。"""
    if skill_copy.is_symlink():
        skill_copy.unlink()
        return f"已删除技能副本（本身是软链接）：{skill_copy}"
    if skill_copy.exists():
        ensure_not_skill_source(skill_copy)
        shutil.rmtree(skill_copy)
        return f"已删除技能副本：{skill_copy}"
    return f"技能副本不存在：{skill_copy}"


def cmd_uninstall(args) -> int:
    skill_copy = skill_copy_dir()
    record = read_record(skill_copy)
    # 已知链接：记录优先，缺失时回退扫描三个已知 Agent 目录（含悬空链接）
    links = [entry for entry in known_links(DoctorContext(skill_copy=skill_copy, repo=None, record=record))
             if isinstance(entry, dict) and isinstance(entry.get("path"), str)]
    ensure_not_skill_source(skill_copy)   # 动手前拦下，避免删了链接才报错
    installed = skill_copy.is_symlink() or skill_copy.exists()
    if not installed and not links:
        print(f"技能未安装：未找到技能副本（{skill_copy}），也未发现指向它的链接，无需卸载")
        if not args.purge:
            return 0
    if args.purge:  # 先做安全检查，再停服务、删任何东西
        problem = purge_guard(ledger_home())
        if problem:
            print(problem, file=sys.stderr)
            return 2
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
    for path, action in remove_links(links, skill_copy):
        print(f"{action}：{path}")
    print(remove_skill_copy(skill_copy))
    if args.purge:
        home_dir = ledger_home()
        if home_dir.exists():
            purge_ledger(home_dir)
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
    p_repair = sub.add_parser(
        "repair", help="修复体检发现的问题：先打印计划，安全项自动执行，破坏性操作需显式参数")
    p_repair.add_argument("--dry-run", action="store_true", dest="dry_run",
                          help="只打印修复计划，不执行、不改动任何状态")
    p_repair.add_argument("--forget-stale", action="store_true", dest="forget_stale",
                          help="删除台账失效记录并释放专属端口（破坏性操作）")
    p_repair.add_argument("--stop-orphans", action="store_true", dest="stop_orphans",
                          help="结束残留服务（破坏性操作）")
    p_repair.set_defaults(handler=cmd_repair)
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
    try:
        return args.handler(args)
    except Exception as exc:
        message = f"命令执行失败（{type(exc).__name__}）：{exc}"
        if getattr(args, "as_json", False):
            print(json.dumps({"error": message}, ensure_ascii=False))
        print(message, file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
