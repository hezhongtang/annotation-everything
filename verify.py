#!/usr/bin/env python3
"""统一验证入口：计划、执行、报告仓库的验证组，并留下结构化计时证据。

  python3 verify.py plan [--group NAME]... [--json]
  python3 verify.py run [--group NAME]... [--issue N] [--pr N] [--phase PHASE]
                        [--reason 文本] [--timeout 秒] [--baseline SHA] [--no-reuse]
                        [--out-dir 目录] [--quiet] [--json] [选择器...]
  python3 verify.py report [--run ID | --latest] [--out-dir 目录] [--json]

验证组（--group 可重复；不写 --group 时，有选择器按 unit-related，否则按 unit）：
  unit         完整单元       python3 -m unittest discover tests
  unit-related 相关单元       显式指定用例 / 模块，如 tests/test_core.py
  browser      完整浏览器     uv run --no-project --with playwright python tests/e2e_browser.py
  serve-check  服务安全自检   python3 skill/annotation-everything/scripts/serve.py check
  all          完整验收       unit + serve-check + browser

行为：
- plan 只解释将执行什么，不启动任何产品测试，也不写记录；
- run 登记子进程、逐个用例计时，把原始日志与结构化记录写进 .verify/（已被忽略）；
- report 只读已有记录，不启动任何产品测试。

退出码：0 全部组完整成功；1 有失败 / 错误 / 空集合 / 全跳过 / 证据不可用；124 组超时；
2 入口自身失败（参数、依赖缺失、记录写不进去）；128+信号 入口被信号中断。
字段契约见 docs/verify/record-contract.md。只用 Python 标准库，语法兼容 Python 3.9。
"""
from __future__ import annotations

import argparse
import contextlib
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
import tempfile
import threading
import time
from pathlib import Path
from typing import Optional

SCHEMA = "ae-verify-run/1"
CASES_SCHEMA = "ae-verify-cases/1"
PLAN_SCHEMA = "ae-verify-plan/1"
SHADOW_SCHEMA = "ae-verify-shadow/1"
WRAPPER_VERSION = 1
ARTIFACT_DIRNAME = ".verify"
TEST_START_DIR = "tests"
KILL_GRACE_SECONDS = 5.0

EXIT_OK = 0
EXIT_FAILED = 1
EXIT_WRAPPER = 2
EXIT_TIMEOUT = 124
EXIT_INTERRUPTED_BASE = 128

# 组的结论。汇总时按顺序取第一个出现的（越靠前越严重）
PASSED = "passed"
FAILED = "failed"
ERROR = "error"
SKIPPED = "skipped"
EMPTY = "empty"
TIMEOUT = "timeout"
INTERRUPTED = "interrupted"
UNAVAILABLE = "unavailable"
WRAPPER_ERROR = "wrapper_error"
# 引用有效证据的组：不是跳过（空集合 / 全跳过是非成功），也不是新的执行（G 项）
REUSED = "reused"
OUTCOME_ORDER = (WRAPPER_ERROR, UNAVAILABLE, ERROR, FAILED, TIMEOUT, INTERRUPTED, SKIPPED, EMPTY, PASSED)

# 结论的人话说法（给人看的报告用；机器可读的仍是英文 outcome）
VERDICT_LABELS = {PASSED: "完整成功", FAILED: "有用例失败", ERROR: "有用例错误", SKIPPED: "全部用例跳过",
                  EMPTY: "空集合", TIMEOUT: "超时", INTERRUPTED: "被信号中断",
                  UNAVAILABLE: "依赖缺失没有执行", WRAPPER_ERROR: "入口自身失败",
                  REUSED: "引用已有验证"}

PHASES = ("issue", "development", "review", "pr", "ci", "manual")
DEFAULT_PHASE = "development"

REPO = Path(__file__).resolve().parent
VERIFY_PY = Path(__file__).resolve()

# 同机资源隔离协议（issue #52）：资源敏感组按共享资源互斥，锁只协调参与者。
# 协议本体在仓库根的 host_isolation.py；直接跑 unittest / e2e_browser 的旧入口共用它。
import host_isolation  # noqa: E402

# 阶段选测与影子对照（issue #55）：策略层在仓库根 verify_selection.py。
# 加载失败不拖垮入口：影子建议按不可用处理（保守回退完整检查），实际执行不受影响。
try:
    import verify_selection  # noqa: E402
except Exception as _selection_error:  # pragma: no cover - 防御分支
    verify_selection = None  # type: ignore[assignment]
    _SELECTION_IMPORT_ERROR = "%s: %s" % (type(_selection_error).__name__, _selection_error)
else:
    _SELECTION_IMPORT_ERROR = None

# 证据复用（issue #56）：策略层在仓库根 verify_evidence.py。
# 加载失败同样不拖垮入口：复用整体停用并写明原因，所有组实际执行。
try:
    import verify_evidence  # noqa: E402
except Exception as _evidence_error:  # pragma: no cover - 防御分支
    verify_evidence = None  # type: ignore[assignment]
    _EVIDENCE_IMPORT_ERROR = "%s: %s" % (type(_evidence_error).__name__, _evidence_error)
else:
    _EVIDENCE_IMPORT_ERROR = None


class UsageError(Exception):
    """命令行用错：退出码 2，不冒充产品失败。"""


class Interrupted(Exception):
    """入口收到信号：先回收登记的子进程，再按 128+信号 退出。"""

    def __init__(self, signum: int):
        super().__init__(signum)
        self.signum = signum


# ── 小工具 ────────────────────────────────────────────────────────────────

def now_iso() -> str:
    return datetime.datetime.now(datetime.timezone.utc).astimezone().isoformat(timespec="microseconds")


def new_run_id() -> str:
    stamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return "%s-%s" % (stamp, os.urandom(3).hex())


def display_argv(argv: list) -> list:
    """路径参数只记仓库相对位置；仓库外路径使用占位符。"""
    out = []
    for token in argv:
        if token == sys.executable:
            out.append("python3")
        elif token == str(VERIFY_PY):
            out.append("verify.py")
        else:
            option, separator, value = token.partition("=") if token.startswith("--") else ("", "", token)
            path = Path(value).expanduser() if value.startswith("~/") else Path(value)
            if path.is_absolute():
                try:
                    value = str(path.resolve().relative_to(REPO))
                except (OSError, ValueError):
                    value = "<external-path>"
            out.append(option + separator + value)
    return out


def write_json_atomic(path: Path, payload) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, tmp_name = tempfile.mkstemp(dir=str(path.parent), prefix=".tmp-", suffix=".json")
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=False, indent=2)
            fh.write("\n")
        os.replace(tmp_name, path)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


def append_jsonl(path: Path, payload) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(payload, ensure_ascii=False) + "\n")


def digest_file(path: Path) -> Optional[str]:
    sha = hashlib.sha256()
    try:
        with open(path, "rb") as fh:
            for block in iter(lambda: fh.read(65536), b""):
                sha.update(block)
    except OSError:
        return None
    return sha.hexdigest()


def signal_name(code: Optional[int]) -> Optional[str]:
    if code is None or code >= 0:
        return None
    try:
        return signal.Signals(-code).name
    except (ValueError, AttributeError):
        return "signal%d" % (-code)


# ── git 与输入快照 ────────────────────────────────────────────────────────

def _doc_exempt_path(path: str) -> bool:
    """路径是否在选测策略的狭窄文档允许列表内（单一实现在 verify_evidence）。

    选测或复用策略层不可用时没有可依据的允许列表，一律不豁免：核心摘要退化为
    与完整指纹同范围，文档差异照样阻断复用（从紧）。
    """
    if verify_evidence is not None:
        return verify_evidence.doc_exempt_path(path)
    return False


def git_output(args: list) -> tuple:
    try:
        proc = subprocess.run(["git", *args], cwd=str(REPO), capture_output=True, text=True, timeout=120)
    except (OSError, subprocess.SubprocessError) as exc:
        return 1, str(exc)
    return proc.returncode, proc.stdout


def parse_status(text: str) -> list:
    """git status --porcelain=v1 -z 的条目 -> [(XY, 路径, 旧路径或 None)]。"""
    entries = []
    fields = text.split("\0")
    index = 0
    while index < len(fields):
        chunk = fields[index]
        index += 1
        if not chunk:
            continue
        code = chunk[:2]
        path = chunk[3:]
        old = None
        if code[:1] in ("R", "C"):
            old = fields[index] if index < len(fields) else ""
            index += 1
        entries.append((code, path, old))
    return entries


def snapshot_input() -> dict:
    """被测输入的内容指纹：HEAD 加上工作树里所有非忽略路径的内容摘要。

    覆盖源码、测试、fixture、模板、词典与执行配置——只要它们有改动就进指纹；
    被 git 忽略的路径不是仓库输入，不参与。产物目录永远排除，避免记录自身污染指纹。
    """
    code, out = git_output(["rev-parse", "--is-inside-work-tree"])
    if code != 0 or out.strip() != "true":
        return {"available": False, "reason": "不是 git 工作树", "fingerprint": None}
    code, head = git_output(["rev-parse", "HEAD"])
    if code != 0:
        return {"available": False, "reason": "读不到 HEAD：%s" % head.strip(), "fingerprint": None}
    head = head.strip()
    code, status = git_output(["status", "--porcelain=v1", "-z", "--untracked-files=all"])
    if code != 0:
        return {"available": False, "reason": "git status 失败：%s" % status.strip(), "fingerprint": None}
    staged, unstaged, untracked, touched = [], [], [], []
    for code, path, old in parse_status(status):
        if code == "??":
            untracked.append(path)
        elif code == "!!":
            continue
        else:
            if code[:1] not in (" ", "?"):
                staged.append(path)
            if code[1:2] not in (" ", "?"):
                unstaged.append(path)
        touched.append(path)
        if old:
            touched.append(old)
    unreadable = []
    digests = []
    core_digests = {}
    for path in sorted(set(touched)):
        if path == ARTIFACT_DIRNAME or path.startswith(ARTIFACT_DIRNAME + "/"):
            continue
        full = REPO / path
        try:
            full.stat()
        except FileNotFoundError:
            digests.append("%s:absent" % path)
            if not _doc_exempt_path(path):
                core_digests[path] = "absent"
            continue
        except OSError:
            unreadable.append(path)
            continue
        digest = digest_file(full)
        if digest is None:
            unreadable.append(path)
        else:
            digests.append("%s:%s" % (path, digest))
            if not _doc_exempt_path(path):
                core_digests[path] = digest
    fingerprint = "sha256:" + hashlib.sha256(
        ("\n".join(["head:%s" % head] + digests)).encode("utf-8")).hexdigest()
    core_items = ["%s:%s" % (path, core_digests[path]) for path in sorted(core_digests)]
    core_fingerprint = "sha256:" + hashlib.sha256(
        ("\n".join(["head:%s" % head] + core_items)).encode("utf-8")).hexdigest()
    return {
        "available": not unreadable,
        "reason": "无法读取有效输入：%s" % "、".join(unreadable) if unreadable else None,
        "fingerprint": None if unreadable else fingerprint,
        "fingerprint_scope": "HEAD + 工作树中所有非忽略路径的内容摘要（源码、测试、fixture、资源与执行配置）",
        "core_fingerprint": None if unreadable else core_fingerprint,
        "core_digests": None if unreadable else core_digests,
        "core_scope": "非文档有效输入摘要：HEAD 加工作树非忽略路径的内容摘要，"
                      "排除选测策略文档允许列表内的路径（G 项文档无关等价判断用）",
        "head": head,
        "staged": sorted(staged),
        "unstaged": sorted(unstaged),
        "untracked": sorted(untracked),
        "counts": {"staged": len(staged), "unstaged": len(unstaged), "untracked": len(untracked)},
        "skipped_large": [],  # 保留 v1 字段；所有大小的文件现在都计算完整摘要
        "unreadable": unreadable,
    }


def resolve_baseline(explicit: Optional[str]) -> dict:
    if explicit:
        code, out = git_output(["rev-parse", "--verify", "%s^{commit}" % explicit])
        if code != 0:
            return {"commit": None, "source": "explicit", "error": "解析不了基线提交 %s" % explicit}
        return {"commit": out.strip(), "source": "explicit"}
    for ref in ("origin/main", "main"):
        code, out = git_output(["merge-base", "HEAD", ref])
        if code == 0 and out.strip():
            return {"commit": out.strip(), "source": "merge-base(%s)" % ref}
    code, out = git_output(["rev-parse", "HEAD"])
    if code == 0 and out.strip():
        return {"commit": out.strip(), "source": "head"}
    return {"commit": None, "source": "head", "error": "读不到 HEAD"}


def changed_since_baseline(baseline: Optional[str], snapshot: dict) -> Optional[list]:
    if not baseline:
        return None
    code, out = git_output(["diff", "--name-only", "-z", baseline])
    if code != 0:
        return None
    paths = {item for item in out.split("\0") if item}
    paths.update(snapshot.get("untracked") or [])
    return sorted(path for path in paths
                  if path != ARTIFACT_DIRNAME and not path.startswith(ARTIFACT_DIRNAME + "/"))


def commit_summary(snapshot: dict) -> dict:
    code, out = git_output(["rev-parse", "HEAD"])
    head = out.strip() if code == 0 else None
    code, out = git_output(["log", "-1", "--format=%h%x00%s"])
    short, subject = ("", "")
    if code == 0:
        parts = out.rstrip("\n").split("\0", 1)
        short, subject = parts[0], (parts[1] if len(parts) > 1 else "")
    code, out = git_output(["rev-parse", "--abbrev-ref", "HEAD"])
    branch = out.strip() if code == 0 else None
    counts = snapshot.get("counts") or {}
    return {"head": head, "short": short or None, "subject": subject or None, "branch": branch,
            "working_tree_dirty": bool(counts.get("staged") or counts.get("unstaged"))}


def logical_session() -> dict:
    """只保存会话摘要；无法继承宿主身份时由调用方显式设置 AE_VERIFY_SESSION。"""
    for name in ("AE_VERIFY_SESSION", "CODEX_THREAD_ID", "CLAUDE_SESSION_ID"):
        if name not in os.environ:
            continue
        value = os.environ[name]
        if not value.strip():
            return {"id": None, "source": name}
        digest = hashlib.sha256((name + ":" + value).encode("utf-8")).hexdigest()
        return {"id": "sha256:" + digest, "source": name}
    return {"id": None, "source": None}


def environment_summary() -> dict:
    return {
        "os": {"system": platform.system(), "release": platform.release(), "machine": platform.machine()},
        "python": {"version": platform.python_version(), "implementation": platform.python_implementation(),
                   "matches_wrapper": True},
        "host": {"cpu_count": os.cpu_count(),
                 "timezone": datetime.datetime.now().astimezone().strftime("%z") or None},
        "ci": {"is_ci": bool(os.environ.get("CI")),
               "provider": "github-actions" if os.environ.get("GITHUB_ACTIONS") == "true" else None,
               "runner_os": os.environ.get("RUNNER_OS")},
        "config_summary": {
            "ae_home_set": bool(os.environ.get("AE_HOME")),
            "ae_locale": os.environ.get("AE_LOCALE"),
            "ae_verify_dir_set": bool(os.environ.get("AE_VERIFY_DIR")),
        },
        # 环境隔离方式（issue #52）：锁在哪类共享位置、覆盖哪些资源；不记本机绝对路径
        "isolation": {
            "mechanism": "flock",
            "lock_dir_source": ("AE_LOCK_DIR" if os.environ.get("AE_LOCK_DIR")
                                else "XDG_RUNTIME_DIR" if os.environ.get("XDG_RUNTIME_DIR")
                                else "tmpdir"),
            "resources": sorted(host_isolation.RESOURCE_DECLARATIONS),
            "note": "资源锁按同机同用户的共享资源划分，跨 worktree 与独立 checkout 生效；"
                    "锁只协调验证参与者，不影响也不停止外部进程",
        },
    }


# ── 验证组 ────────────────────────────────────────────────────────────────

# unit-related 固定附带的全局契约（issue #51）：无参数全词典一致性检查集中在
# tests/test_i18n_catalog_contract.py，局部组自动带上它，去重后独立跑相关组
# 也不丢全局保护。模块缺失时局部组加载失败报 error，不会静默失去保护。
RELATED_CONTRACT_SELECTORS = ("tests/test_i18n_catalog_contract.py",)


@dataclasses.dataclass
class Group:
    name: str
    title: str
    purpose: str
    equivalent: str
    argv: list
    counter: str
    requires: tuple = ()
    selectors: bool = False
    scenarios: bool = False
    expands: tuple = ()
    auto_included: tuple = ()
    resources: tuple = ()  # 组要求的同机共享资源；为空即纯逻辑组，不参与资源锁

    @property
    def granularity(self) -> str:
        return "case" if self.counter == "unittest" else "group"


def _selector_module_prefix(selector: str) -> str:
    """把路径或点分选择器归一成模块名前缀（不带 tests.），用于判断是否已含某模块。

    tests/test_x.py、tests.test_x、./tests/test_x.py、绕行相对路径（../51/tests/…）、
    绝对路径与指到类 / 方法的点分写法都归一到同一个前缀，避免同一模块因写法不同
    被附带两次。
    """
    text = str(selector).replace("\\", "/")
    if text.endswith(".py"):
        text = text[:-len(".py")]
    candidate = Path(text)
    if candidate.is_absolute() or ".." in text.split("/"):
        # 绝对路径与含 .. 的绕行相对路径先落成仓库内相对路径：段处理消不掉 .. 段，
        # 不归一的话 ../51/tests/x 与 tests/x 会被当成两个模块
        try:
            text = "/".join(candidate.resolve()
                            .relative_to((REPO / TEST_START_DIR).resolve()).parts)
        except (OSError, ValueError):
            if candidate.is_absolute():
                return text  # 指到仓库外：没有可归一的模块名，保持去 .py 后的原文
    parts = [part for part in text.split("/") if part and part != "."]
    if parts and parts[0] == TEST_START_DIR:
        parts = parts[1:]
    joined = ".".join(parts)
    if joined.startswith(TEST_START_DIR + "."):
        joined = joined[len(TEST_START_DIR) + 1:]
    return joined


def expand_related_selectors(selectors: list) -> tuple:
    """unit-related 的生效选择器：显式指定的模块，加上固定附带的契约模块。

    返回 (生效选择器, 自动补上的选择器)。已显式指到契约模块（含类 / 方法级）
    时不重复附带；契约模块本体缺失不在这里兜底——局部组会加载失败报 error。
    """
    effective, added = list(selectors), []
    keys = {item: _selector_module_prefix(item) for item in effective}
    for contract in RELATED_CONTRACT_SELECTORS:
        key = _selector_module_prefix(contract)
        if any(value == key or value.startswith(key + ".") for value in keys.values()):
            continue
        effective.append(contract)
        keys[contract] = key
        added.append(contract)
    return effective, added


def selector_modules(selectors: Optional[list]) -> set:
    """选择器 -> 模块名集合（tests.test_x），用于判定 unit-related 的资源要求。

    复用 _selector_module_prefix 的归一（./ 前缀、绕行相对路径、点分到类 / 方法
    的写法），与契约模块附带的判定保持同一口径；类 / 方法级选择器归到所在模块。
    """
    modules = set()
    for selector in selectors or []:
        prefix = _selector_module_prefix(selector)
        if not prefix or "/" in prefix:
            continue   # 空选择器或指到仓库外：没有可归一的模块名
        module = prefix.split(".")[0]
        if module:
            modules.add(TEST_START_DIR + "." + module)
    return modules


def unit_related_resources(selectors: Optional[list]) -> tuple:
    """unit-related 按选择器涉及的敏感模块决定资源；纯逻辑选择器不参与资源锁。"""
    needed = set()
    for module in selector_modules(selectors):
        needed.update(host_isolation.resources_for_module(module) or ())
    return host_isolation.normalize_resources(needed)


def build_groups(selectors: Optional[list] = None) -> dict:
    names = list(selectors or [])
    auto_included: tuple = ()
    if names:
        names, appended = expand_related_selectors(names)
        auto_included = tuple(appended)
    shown = display_argv(names) or ["<选择器>"]
    groups = {
        "unit": Group(
            name="unit", title="完整单元",
            purpose="仓库全部单元测试；逐个用例计时，类 / 模块级开销单独归属",
            equivalent="python3 -m unittest discover tests",
            argv=[sys.executable, str(VERIFY_PY), "_unit-runner", "discover", TEST_START_DIR],
            counter="unittest",
            resources=host_isolation.normalize_resources(host_isolation.GROUP_RESOURCES["unit"])),
        "unit-related": Group(
            name="unit-related", title="相关单元",
            purpose="只执行显式指定的用例 / 模块，并固定附带全局词典一致性契约；"
                    "不给选择器即空集合，入口报错",
            equivalent="python3 -m unittest " + " ".join(shown),
            argv=[sys.executable, str(VERIFY_PY), "_unit-runner", "names", *names],
            counter="unittest", selectors=True, auto_included=auto_included,
            resources=unit_related_resources(names)),
        "browser": Group(
            name="browser", title="完整浏览器",
            purpose="浏览器端到端回归：按注册场景执行，记录里带逐场景的执行清单与计时；"
                    "局部场景用 e2e_browser.py --only 直接跑",
            equivalent="uv run --no-project --with playwright python tests/e2e_browser.py",
            argv=["uv", "run", "--no-project", "--with", "playwright", "python", "tests/e2e_browser.py"],
            counter="log", requires=("uv",), scenarios=True,
            resources=host_isolation.normalize_resources(host_isolation.GROUP_RESOURCES["browser"])),
        "serve-check": Group(
            name="serve-check", title="服务安全自检",
            purpose="服务安全约束自检：127.0.0.1 监听、Host 校验、写操作同源 Origin 与 JSON "
                    "Content-Type、无 CORS 头、只暴露 index.html 与 /api/*",
            equivalent="python3 skill/annotation-everything/scripts/serve.py check",
            argv=[sys.executable, "skill/annotation-everything/scripts/serve.py", "check"],
            counter="log"),
    }
    groups["all"] = Group(
        name="all", title="完整验收",
        purpose="PR 收敛时的完整验收：按 unit → serve-check → browser 顺序执行",
        equivalent="依次执行 unit、serve-check、browser 三组的等价命令",
        argv=[], counter="composite", expands=("unit", "serve-check", "browser"))
    return groups


def expand_groups(requested: list, groups: dict) -> list:
    ordered = []
    for name in requested:
        group = groups.get(name)
        if group is None:
            raise UsageError("未知的验证组：%s（可选：%s）" % (name, "、".join(groups)))
        for member in (expand_groups(list(group.expands), groups) if group.expands else [name]):
            if member not in ordered:
                ordered.append(member)
    return ordered


# ── 计时探针（在子进程里跑，只面向 unittest） ──────────────────────────────

def _test_id(test) -> str:
    try:
        return test.id()
    except Exception:
        return repr(test)


def _class_label(test) -> Optional[str]:
    return "%s.%s" % (getattr(test, "__module__", "?"), test.__class__.__name__)


def _short_message(text: str, limit: int = 200) -> str:
    for line in (text or "").splitlines():
        line = line.strip()
        if line:
            return line[:limit]
    return ""


def _selector_name(selector: str) -> str:
    """tests/xxx.py 这类路径选择器换成 discover 模式下的模块名，其余原样交给 unittest。"""
    candidate = Path(selector)
    if candidate.suffix == ".py" and candidate.exists():
        try:
            relative = candidate.resolve().relative_to((REPO / TEST_START_DIR).resolve())
        except ValueError:
            return selector
        return ".".join(relative.with_suffix("").parts)
    return selector


def cmd_unit_runner(argv: list) -> int:
    import unittest

    parser = argparse.ArgumentParser(prog="verify.py _unit-runner", add_help=False)
    parser.add_argument("mode", choices=["discover", "names"])
    parser.add_argument("targets", nargs="*")
    parser.add_argument("--pattern", default="test*.py")
    parser.add_argument("--cases-out", required=True)
    parser.add_argument("--run-id", default="")
    parser.add_argument("--verbosity", type=int, default=2)
    args = parser.parse_args(argv)

    class TimingResult(unittest.TextTestResult):
        """逐个用例计时；类 / 模块级开销归到用例之间的空隙，不摊到任何单个用例上。"""

        def __init__(self, *result_args, **result_kwargs):
            super().__init__(*result_args, **result_kwargs)
            self.case_records = []
            self.gaps = []
            self.subtest_failures = 0
            self.subtest_errors = 0
            self.subtest_skips = 0
            self._subtest_by_case = {"failures": {}, "errors": {}, "passed": {}, "skipped": {}}
            self._run_started = None
            self._last_stop = None
            self._previous_class = None
            self._pending = None
            self.fixture_errors = 0
            self.fixture_skips = 0

        def startTestRun(self):
            super().startTestRun()
            self._run_started = time.monotonic()
            self._last_stop = self._run_started

        def stopTestRun(self):
            super().stopTestRun()
            stopped = time.monotonic()
            if self._last_stop is not None:
                self._add_gap(self._previous_class, None, stopped - self._last_stop)
            self._last_stop = None

        def startTest(self, test):
            now = time.monotonic()
            self._add_gap(self._previous_class, _class_label(test), now - self._last_stop)
            super().startTest(test)
            self._pending = (test, now)

        def stopTest(self, test):
            now = time.monotonic()
            super().stopTest(test)
            started = self._pending[1] if self._pending else now
            self._pending = None
            self._last_stop = now
            self._previous_class = _class_label(test)
            identifier = _test_id(test)
            status = self._status_of(test)
            subtest_failures = self._subtest_by_case["failures"].get(identifier, 0)
            subtest_errors = self._subtest_by_case["errors"].get(identifier, 0)
            subtest_skips = self._subtest_by_case["skipped"].get(identifier, 0)
            if status == PASSED and (subtest_failures or subtest_errors):
                # 用例本身跑完了，但它的子测试有失败：结论不能算通过
                status = ERROR if subtest_errors else FAILED
            active_exception = sys.exc_info()[0]
            if active_exception is not None and issubclass(active_exception, KeyboardInterrupt):
                status = INTERRUPTED
            elif (status == PASSED and subtest_skips
                  and not self._subtest_by_case["passed"].get(identifier, 0)):
                status = SKIPPED
            self.case_records.append({
                "id": identifier,
                "module": getattr(test, "__module__", None),
                "class": _class_label(test),
                "name": getattr(test, "_testMethodName", None),
                "status": status,
                "seconds": round(now - started, 6),
                "subtest_failures": subtest_failures,
                "subtest_errors": subtest_errors,
                "subtest_skips": subtest_skips,
                "message": self._message_of(test),
            })

        def addError(self, test, err):
            if self._pending is None:
                self.fixture_errors += 1
            super().addError(test, err)

        def addSkip(self, test, reason):
            if self._pending is None:
                self.fixture_skips += 1
            elif test is not self._pending[0]:
                self.subtest_skips += 1
                identifier = _test_id(self._pending[0])
                counter = self._subtest_by_case["skipped"]
                counter[identifier] = counter.get(identifier, 0) + 1
            super().addSkip(test, reason)

        def addSubTest(self, test, subtest, err):
            if err is not None:
                if issubclass(err[0], test.failureException):
                    self.subtest_failures += 1
                    bucket = "failures"
                else:
                    self.subtest_errors += 1
                    bucket = "errors"
            else:
                bucket = "passed"
            counter = self._subtest_by_case[bucket]
            identifier = _test_id(test)
            counter[identifier] = counter.get(identifier, 0) + 1
            super().addSubTest(test, subtest, err)

        def _add_gap(self, from_label, to_label, seconds):
            if self._last_stop is None or seconds is None or seconds < 0:
                return
            if from_label is None:
                kind = "run-start"
            elif to_label is None:
                kind = "run-end"
            elif from_label == to_label:
                kind = "same-class"
            else:
                kind = "class-boundary"
            self.gaps.append({"kind": kind, "from": from_label, "to": to_label,
                              "seconds": round(seconds, 6)})

        def _status_of(self, test) -> str:
            identifier = _test_id(test)
            for bucket, status in (("errors", ERROR), ("failures", FAILED), ("skipped", SKIPPED),
                                   ("expectedFailures", "expected_failure")):
                for entry in getattr(self, bucket, None) or []:
                    if _test_id(entry[0]) == identifier:
                        return status
            for entry in getattr(self, "unexpectedSuccesses", None) or []:
                if _test_id(entry) == identifier:
                    return "unexpected_success"
            return PASSED

        def _message_of(self, test) -> str:
            identifier = _test_id(test)
            for bucket in ("failures", "errors", "skipped"):
                for entry in getattr(self, bucket, None) or []:
                    if _test_id(entry[0]) == identifier:
                        return _short_message(entry[1])
            return ""

    loader = unittest.TestLoader()
    suite = None
    load_error = None
    interrupted = False
    try:
        if args.mode == "discover":
            suite = loader.discover(args.targets[0], pattern=args.pattern)
        else:
            sys.path.insert(0, os.path.abspath(TEST_START_DIR))
            suite = loader.loadTestsFromNames([_selector_name(target) for target in args.targets])
    except KeyboardInterrupt:
        interrupted = True
    except BaseException as exc:  # 加载失败也要留下记录，不能静默通过
        load_error = "%s: %s" % (type(exc).__name__, exc)
        print(load_error, file=sys.stderr)

    result = None
    runner_error = None

    class TimingRunner(unittest.TextTestRunner):
        def _makeResult(self):
            nonlocal result
            result = super()._makeResult()
            return result

    if load_error is None and not interrupted:
        runner = TimingRunner(verbosity=args.verbosity, resultclass=TimingResult)
        try:
            result = runner.run(suite)
        except KeyboardInterrupt:
            interrupted = True
        except BaseException as exc:
            runner_error = "%s: %s" % (type(exc).__name__, exc)
            print(runner_error, file=sys.stderr)

    def counts_of(result_obj) -> dict:
        if result_obj is None:
            return {}
        statuses = [case["status"] for case in result_obj.case_records]
        return {
            "cases": result_obj.testsRun,
            "passed": statuses.count(PASSED),
            "failed": statuses.count(FAILED),
            "errors": statuses.count(ERROR),
            "skipped": statuses.count(SKIPPED),
            "fixture_errors": result_obj.fixture_errors,
            "fixture_skips": result_obj.fixture_skips,
            "expected_failures": statuses.count("expected_failure"),
            "unexpected_successes": statuses.count("unexpected_success"),
            "interrupted": statuses.count(INTERRUPTED),
            "subtest_failures": result_obj.subtest_failures,
            "subtest_errors": result_obj.subtest_errors,
            "subtest_skips": result_obj.subtest_skips,
        }

    cases = list(getattr(result, "case_records", None) or [])
    gaps = list(getattr(result, "gaps", None) or [])
    gap_by_kind: dict = {}
    for gap in gaps:
        gap_by_kind[gap["kind"]] = round(gap_by_kind.get(gap["kind"], 0.0) + gap["seconds"], 6)
    write_json_atomic(Path(args.cases_out), {
        "schema": CASES_SCHEMA,
        "run_id": args.run_id,
        "mode": args.mode,
        "targets": display_argv(args.targets),
        "load_error": load_error,
        "runner_error": runner_error,
        "interrupted": interrupted,
        "counts": counts_of(result),
        "cases": cases,
        "gaps": gaps,
        "seconds": {
            "case_total": round(sum(item["seconds"] for item in cases), 6),
            "gap_total": round(sum(gap["seconds"] for gap in gaps), 6),
            "gap_by_kind": gap_by_kind,
            "unattributed": round(gap_by_kind.get("same-class", 0.0), 6),
        },
        "python": platform.python_version(),
    })
    if interrupted:
        return EXIT_INTERRUPTED_BASE + int(signal.SIGINT)
    if load_error is not None or runner_error is not None:
        return EXIT_FAILED
    return EXIT_OK if result is not None and result.wasSuccessful() else EXIT_FAILED


def parse_log_counts(text: str) -> Optional[dict]:
    """从日志末尾的「N/M 通过」摘要行取计数；取不到返回 None。"""
    best = None
    for match in re.finditer(r"(\d+)\s*/\s*(\d+)\s*(?:通过|passed)", text):
        best = match
    if best is None:
        return None
    passed, total = int(best.group(1)), int(best.group(2))
    return {"total": total, "passed": passed, "failed": max(0, total - passed)}


# 浏览器入口在「N/M 通过」后输出的场景执行清单行（issue #53）
SCENARIO_MARKER = "AE-SCENARIOS: "
# 场景结论的人话说法（报告里用；机器可读的仍是英文 status）
SCENARIO_LABELS = {"passed": "通过", "failed": "失败", "error": "错误",
                   "interrupted": "中断", "not_run": "未运行"}


def parse_scenario_summary(text: str) -> Optional[dict]:
    """取日志里最后一行场景执行清单；没有或读不出来返回 None，不影响整组计数。"""
    payload = None
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith(SCENARIO_MARKER):
            payload = stripped[len(SCENARIO_MARKER):]
    if payload is None:
        return None
    try:
        summary = json.loads(payload)
    except ValueError:
        return None
    if not isinstance(summary, dict) or not isinstance(summary.get("scenarios"), list):
        return None
    return summary


def parse_browser_environment(text: str) -> Optional[dict]:
    for line in reversed(text.splitlines()):
        if line.startswith("AE-BROWSER-ENV: "):
            try:
                payload = json.loads(line[len("AE-BROWSER-ENV: "):])
                if (isinstance(payload, dict) and payload.get("schema") == "ae-browser-environment/1"
                        and all(isinstance(payload.get(key), str) and payload[key]
                                for key in ("browser", "channel", "version", "playwright"))):
                    return {key: payload[key] for key in ("schema", "browser", "channel", "version", "playwright")}
            except ValueError:
                pass
            return None
    return None


# ── 执行 ──────────────────────────────────────────────────────────────────

def _pgid_of(proc) -> Optional[int]:
    try:
        return os.getpgid(proc.pid)
    except (OSError, AttributeError):
        return None


def signal_group(proc, sig: int) -> None:
    try:
        os.killpg(os.getpgid(proc.pid), sig)
        return
    except (OSError, AttributeError):
        pass
    try:
        proc.send_signal(sig)
    except OSError:
        pass


def pump(pipe, log_fh, echo):
    try:
        for line in iter(pipe.readline, ""):
            log_fh.write(line)
            log_fh.flush()
            if echo is not None:
                echo.write(line)
                echo.flush()
    except (OSError, ValueError):
        pass
    finally:
        try:
            pipe.close()
        except OSError:
            pass


class ChildRegistry:
    """本次入口自己拉起的子进程登记簿：只回收登记过的进程，绝不去盘点别人的进程。"""

    def __init__(self):
        self.entries: list = []

    def register(self, proc, group_name: str) -> dict:
        entry = {"group": group_name, "pid": proc.pid, "pgid": _pgid_of(proc),
                 "started_at": now_iso(), "reaped": False, "exit_status": None, "signal": None,
                 "proc": proc}
        self.entries.append(entry)
        return entry

    def live(self) -> list:
        return [entry for entry in self.entries if not entry["reaped"]]

    def public(self) -> list:
        return [{key: value for key, value in entry.items() if key != "proc"} for entry in self.entries]


class RunContext:
    def __init__(self, run_id: str, artifact_dir: Path, timeout: Optional[float], echo, quiet: bool):
        self.run_id = run_id
        self.artifact_dir = artifact_dir
        self.run_dir = artifact_dir / "runs" / run_id
        self.timeout = timeout
        self.echo = echo
        self.quiet = quiet
        self.registry = ChildRegistry()
        self.notes: list = []
        self.cleanup_seconds = 0.0  # 中断回收登记子进程的耗时（清理时间与执行时间分开呈现）

    def save_record(self, record: dict) -> None:
        write_json_atomic(self.run_dir / "record.json", record)

    def save_children(self) -> None:
        write_json_atomic(self.run_dir / "children.json", self.registry.public())

    def note(self, text: str) -> None:
        if text not in self.notes:
            self.notes.append(text)

    def abort(self, signum: int) -> None:
        """回收本次登记的子进程：先原信号，宽限期后升级；未登记的进程一律不动。"""
        cleanup_started = time.monotonic()
        for entry in self.registry.live():
            if entry["proc"].poll() is None:
                signal_group(entry["proc"], signum)
        deadline = time.monotonic() + KILL_GRACE_SECONDS
        for entry in self.registry.live():
            remaining = max(0.0, deadline - time.monotonic())
            try:
                entry["proc"].wait(timeout=remaining)
            except subprocess.TimeoutExpired:
                signal_group(entry["proc"], signal.SIGKILL)
                try:
                    entry["proc"].wait(timeout=KILL_GRACE_SECONDS)
                except subprocess.TimeoutExpired:
                    pass
            code = entry["proc"].poll()
            entry.update(reaped=code is not None, exit_status=code, signal=signal_name(code))
            if code is None:
                self.note("登记的 %s 子进程未能在宽限期内退出" % entry["group"])
        try:
            self.save_children()
        except OSError as exc:
            self.note("子进程回收后，登记簿写入失败：%s" % exc)
        # 组内异常和外层收尾可能先后调用；空回收不得覆盖已经发生的清理耗时。
        self.cleanup_seconds = round(
            self.cleanup_seconds + time.monotonic() - cleanup_started, 6)


def execute_group(group: Group, ctx: RunContext) -> dict:
    """执行一个组；资源敏感组先按隔离协议取锁，锁等待单独计时，超时不是通过。

    锁等待超时的组没有启动子进程：结论 timeout、退出码 124、note 写明等待的是哪个
    资源与当前持有者；等待过程绝不停止任何进程——占用者可能是别的检出的合法验证。
    """
    resources = list(group.resources)
    if not resources:
        result = _execute_group_body(group, ctx)
        result["lock"] = {"resources": [], "wait_seconds": 0.0}
        return result
    handle = None
    wait_started = time.monotonic()
    wait_limit = host_isolation.wait_limit()
    try:
        handle = host_isolation.acquire(resources, timeout=wait_limit)
    except Interrupted as exc:
        waited = round(time.monotonic() - wait_started, 6)
        note = "等待共享资源 %s 时收到 %s，本组没有执行" % (
            "、".join(resources), signal_name(-exc.signum) or exc.signum)
        return {"name": group.name, "outcome": INTERRUPTED,
                "exit_status": EXIT_INTERRUPTED_BASE + exc.signum,
                "signal": signal_name(-exc.signum), "interrupted_signum": exc.signum,
                "timed_out": False, "note": note, "counts": None,
                "cases": [], "seconds": {}, "timing": {},
                "lock": {"resources": resources, "wait_seconds": waited}}
    except host_isolation.LockTimeoutError as exc:
        holder = host_isolation.read_holder_note(exc.resource) or {}
        note = ("等待共享资源 %s 超过 %.1f 秒（当前持有者 pid %s），本组没有执行；"
                "锁等待超时不是通过" % (exc.resource, wait_limit,
                                        holder.get("pid", "未知")))
        ctx.note(note)
        return {"name": group.name, "outcome": TIMEOUT, "exit_status": EXIT_TIMEOUT,
                "signal": None, "timed_out": False, "note": note, "counts": None,
                "cases": [], "seconds": {}, "timing": {},
                "lock": {"resources": resources,
                         "wait_seconds": round(exc.waited_seconds, 6), "timeout": True}}
    try:
        result = _execute_group_body(group, ctx, lock_fds=handle.file_descriptors())
    finally:
        host_isolation.release(handle)
    result["lock"] = {"resources": resources, "wait_seconds": round(handle.waited_seconds, 6)}
    return result


def _execute_group_body(group: Group, ctx: RunContext, lock_fds: tuple = ()) -> dict:
    with contextlib.ExitStack() as cleanup_guard:
        result = {}
        started_wall = time.monotonic()
        missing = [name for name in group.requires if shutil.which(name) is None]
        if missing:
            return {"name": group.name, "outcome": UNAVAILABLE, "exit_status": None, "signal": None,
                    "timed_out": False, "note": "找不到可执行文件：%s；本组没有执行" % "、".join(missing),
                    "counts": None, "cases": [], "seconds": {}, "timing": {}}

        log_path = ctx.run_dir / ("%s.log" % group.name)
        cases_path = ctx.run_dir / ("%s.cases.json" % group.name)
        child_argv = list(group.argv)
        if group.counter == "unittest":
            child_argv += ["--cases-out", str(cases_path), "--run-id", ctx.run_id]

        interrupted_signum = None
        with open(log_path, "w", encoding="utf-8") as log_fh:
            proc, reader, registry_entry = None, None, None
            timed_out = False
            try:
                proc = subprocess.Popen(child_argv, cwd=str(REPO), env=dict(os.environ),
                                        stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                        text=True, encoding="utf-8", errors="replace", start_new_session=True,
                                        pass_fds=lock_fds)
                registry_entry = ctx.registry.register(proc, group.name)
                ctx.save_children()
                reader = threading.Thread(target=pump,
                                          args=(proc.stdout, log_fh, None if ctx.quiet else ctx.echo), daemon=True)
                reader.start()
                try:
                    code = proc.wait(timeout=ctx.timeout)
                except subprocess.TimeoutExpired:
                    timed_out = True
                    signal_group(proc, signal.SIGTERM)
                    try:
                        code = proc.wait(timeout=KILL_GRACE_SECONDS)
                    except subprocess.TimeoutExpired:
                        signal_group(proc, signal.SIGKILL)
                        code = proc.wait()
                reader.join(timeout=5)
            except Interrupted as exc:
                # 先让子进程在原日志流上完成中断摘要，再关闭文件。否则读线程会
                # 写入已关闭的文件，父入口也拿不到已经完成 / 中断 / 未运行的场景。
                interrupted_signum = exc.signum
                cleanup_guard.enter_context(shield_termination())
                if proc is not None and registry_entry is None:
                    registry_entry = ctx.registry.register(proc, group.name)
                ctx.abort(exc.signum)
                code = proc.poll() if proc is not None else None
                if reader is not None and reader.ident is not None:
                    reader.join(timeout=5)
            except Exception:
                # 记录写入等入口错误也须在日志关闭、资源锁释放之前回收自己的子进程。
                cleanup_guard.enter_context(shield_termination())
                ctx.abort(signal.SIGTERM)
                if reader is not None and reader.ident is not None:
                    reader.join(timeout=5)
                raise
            # 正常组收尾收到的首次取消要保留；已在回收中则仍屏蔽二次信号。
            # 捕获器直接写入待返回结果，既不丢已完成证据，也不吞掉用户取消。
            if interrupted_signum is None:
                cleanup_guard.enter_context(capture_termination(result))
            if registry_entry is not None:
                registry_entry.update(reaped=code is not None, exit_status=code, signal=signal_name(code))
            ctx.save_children()
        child_seconds = round(time.monotonic() - started_wall, 6)

        log_text = log_path.read_text(encoding="utf-8", errors="replace") if log_path.is_file() else ""
        counts, cases, seconds, load_error, evidence_missing = None, [], {}, None, None
        scenario_summary = None
        interrupted = False
        if group.counter == "unittest":
            if not cases_path.is_file():
                evidence_missing = "子进程没有留下计时证据（%s）" % cases_path.name
            else:
                try:
                    payload = json.loads(cases_path.read_text(encoding="utf-8"))
                except (OSError, ValueError) as exc:
                    evidence_missing = "计时证据读不出来：%s" % exc
                else:
                    if payload.get("schema") != CASES_SCHEMA:
                        evidence_missing = "计时证据 schema 不支持：%r" % payload.get("schema")
                    else:
                        counts = payload.get("counts") or {}
                        cases = payload.get("cases") or []
                        seconds = payload.get("seconds") or {}
                        load_error = payload.get("load_error") or payload.get("runner_error")
                        interrupted = payload.get("interrupted") is True
        else:
            counts = parse_log_counts(log_text)
            if counts is None:
                evidence_missing = "日志里没有「N/M 通过」摘要行"
            if group.scenarios:
                # 场景级记录来自子进程自己的执行清单；日志里没有时保持整组粒度的既有行为
                scenario_summary = parse_scenario_summary(log_text)

        outcome, exit_status = classify_group(group, code, timed_out, counts, load_error, evidence_missing,
                                              interrupted)
        if interrupted_signum is not None:
            outcome, exit_status = INTERRUPTED, EXIT_INTERRUPTED_BASE + interrupted_signum
        note = evidence_missing
        if interrupted_signum is not None and evidence_missing is not None:
            note = "本组没有拿到执行结果；%s" % evidence_missing
        if timed_out:
            note = "超过 %s 秒被入口终止" % ctx.timeout
        result.update({"name": group.name, "outcome": outcome, "exit_status": exit_status,
                "signal": (signal_name(-interrupted_signum) if interrupted_signum is not None else
                           signal_name(-int(signal.SIGINT)) if interrupted else signal_name(code)),
                "timed_out": timed_out, "note": note, "counts": counts,
                "cases": cases, "seconds": seconds,
                "scenario_summary": scenario_summary,
                "browser_environment": parse_browser_environment(log_text) if group.scenarios else None,
                "timing": {"child_seconds": child_seconds,
                           "case_seconds_total": seconds.get("case_total"),
                           "fixture_seconds_total": fixture_total(seconds),
                           "unattributed_seconds": seconds.get("unattributed")}})
        if interrupted_signum is not None:
            result["interrupted_signum"] = interrupted_signum
        return result


def fixture_total(seconds: dict) -> float:
    by_kind = seconds.get("gap_by_kind") or {}
    return round(by_kind.get("run-start", 0.0) + by_kind.get("class-boundary", 0.0)
                 + by_kind.get("run-end", 0.0), 6)


def classify_group(group: Group, code: Optional[int], timed_out: bool, counts: Optional[dict],
                   load_error: Optional[str], evidence_missing: Optional[str],
                   interrupted: bool = False) -> tuple:
    """把原始退出状态翻译成结论。空集合与全跳过都不是通过。"""
    if timed_out:
        return TIMEOUT, EXIT_TIMEOUT
    if code is None:
        return UNAVAILABLE, None
    if code < 0:
        return INTERRUPTED, EXIT_INTERRUPTED_BASE + (-code)
    if code == EXIT_INTERRUPTED_BASE + int(signal.SIGINT):
        # 子进程没有被信号击杀，而是按 128+SIGINT 约定自行退出（浏览器脚本的中断路径）：
        # 与被击杀同样视为中断，不冒充失败，更不冒充通过
        return INTERRUPTED, code
    if interrupted:
        return INTERRUPTED, EXIT_INTERRUPTED_BASE + int(signal.SIGINT)
    if evidence_missing is not None:
        return ERROR, EXIT_FAILED
    if group.counter == "unittest":
        if load_error or (counts or {}).get("errors") or (counts or {}).get("fixture_errors"):
            return ERROR, EXIT_FAILED
        if (counts or {}).get("failed") or (counts or {}).get("unexpected_successes"):
            return FAILED, EXIT_FAILED
        if not (counts or {}).get("cases"):
            if (counts or {}).get("fixture_skips"):
                return SKIPPED, EXIT_FAILED
            return EMPTY, EXIT_FAILED
        if (counts or {}).get("skipped") == (counts or {}).get("cases"):
            return SKIPPED, EXIT_FAILED
        return PASSED, EXIT_OK
    if not (counts or {}).get("total"):
        return EMPTY, EXIT_FAILED
    if (counts or {}).get("failed") or code != EXIT_OK:
        return FAILED, EXIT_FAILED
    return PASSED, EXIT_OK


def overall_exit(outcomes: list, groups: list) -> int:
    if any(outcome in (WRAPPER_ERROR, UNAVAILABLE) for outcome in outcomes):
        return EXIT_WRAPPER
    if TIMEOUT in outcomes:
        return EXIT_TIMEOUT
    for group in groups:
        if group.get("outcome") == INTERRUPTED and group.get("exit_status"):
            return group["exit_status"]
    if any(outcome in (ERROR, FAILED, SKIPPED, EMPTY) for outcome in outcomes):
        return EXIT_FAILED
    return EXIT_OK


def worst_outcome(outcomes: list) -> str:
    for candidate in OUTCOME_ORDER:
        if candidate in outcomes:
            return candidate
    return PASSED


def summarize(record: dict) -> str:
    parts = []
    for group in record["groups"]:
        if group.get("outcome") == REUSED:
            parts.append("%s reused 引用已有验证（run %s）"
                         % (group["name"], (group.get("reuse") or {}).get("source_run_id")))
            continue
        counts = group.get("counts") or {}
        if group.get("counter") == "unittest":
            detail = "用例 %s（通过 %s / 失败 %s / 错误 %s / 跳过 %s）" % (
                counts.get("cases", 0), counts.get("passed", 0), counts.get("failed", 0),
                counts.get("errors", 0), counts.get("skipped", 0))
        elif group.get("counter") == "log":
            detail = "检查 %s / %s" % (counts.get("passed", 0), counts.get("total", 0))
        else:
            detail = "—"
        parts.append("%s %s %s" % (group["name"], group.get("outcome") or WRAPPER_ERROR, detail))
    return "；".join(parts)


def trim_cases(cases: list) -> list:
    """记录里只留用例 ID、状态与耗时；失败信息只在非通过时带上。"""
    trimmed = []
    for case in cases:
        item = {"id": case.get("id"), "name": case.get("name"), "status": case.get("status"),
                "seconds": case.get("seconds")}
        if case.get("status") != PASSED:
            item["message"] = case.get("message")
        if case.get("subtest_failures") or case.get("subtest_errors") or case.get("subtest_skips"):
            item["subtest_failures"] = case.get("subtest_failures")
            item["subtest_errors"] = case.get("subtest_errors")
            item["subtest_skips"] = case.get("subtest_skips")
        trimmed.append(item)
    return trimmed


# ── 子命令 ────────────────────────────────────────────────────────────────

def artifact_root(out_dir: Optional[str]) -> Path:
    """产物根目录。相对路径先绝对化：主进程按调用方 cwd 解析，子进程固定在仓库根，
    不统一锚定的话计时证据会分裂成两处。"""
    if out_dir:
        return Path(out_dir).resolve()
    override = os.environ.get("AE_VERIFY_DIR")
    if override:
        return Path(override).resolve()
    return REPO / ARTIFACT_DIRNAME


def cmd_plan(args) -> int:
    # 与 run 一致：「plan unit-related tests/test_x.py」这类写法，第一个位置参数是
    # 组名时当组名用，不把组名混进选择器与等价 / 实际命令
    if args.group is None and args.selectors and args.selectors[0] in build_groups():
        args.group = [args.selectors.pop(0)]
    phase = getattr(args, "phase", None)
    if phase is not None and phase not in PHASES:
        raise UsageError("未知的开发阶段：%s（可选：%s）" % (phase, "、".join(PHASES)))
    groups = build_groups(args.selectors)
    selected = expand_groups(args.group or ["unit"], groups)
    # 选测计划（issue #55）：--json 与 --phase 时计算；解释范围，不启动任何产品测试
    selection = None
    if args.as_json or phase is not None:
        selection = build_selection_for(phase or DEFAULT_PHASE,
                                        resolve_baseline(getattr(args, "baseline", None)),
                                        snapshot_input())
    if args.as_json:
        payload = {
            "schema": PLAN_SCHEMA,
            "wrapper_version": WRAPPER_VERSION,
            "default_group": "unit",
            "requested": args.group or ["unit"],
            "selected": selected,
            "artifact_dir": ARTIFACT_DIRNAME,
            "isolation": {
                "resources": host_isolation.resource_declarations(),
                "order": host_isolation.resource_order(),
                "note": "资源锁按共享资源而非检出目录划分；锁只协调验证参与者，"
                        "不停止任何未登记进程；锁等待单独计时，超时不是通过",
            },
            "reuse_policy": {
                "schema": REUSE_SCHEMA,
                "policy_version": (verify_evidence.POLICY_VERSION if verify_evidence else None),
                "freshness_hours": (verify_evidence.FRESHNESS_HOURS if verify_evidence else None),
                "enabled_phases": list(verify_evidence.REUSE_PHASES) if verify_evidence else [],
                "gate_phases": list(verify_evidence.GATE_PHASES) if verify_evidence else ["pr", "ci"],
                "rule": "同输入指纹 + 同环境 + 范围覆盖 + 当前校验有效 + 未超保鲜窗口 + 版本一致"
                        "才复用（差异被证明仅在文档允许列表内不算输入变化）；"
                        "依赖宿主机状态的组（resources 非空）永远实际执行；"
                        "--no-reuse 强制全部重跑",
            },
            "groups": [{"name": group.name, "title": group.title, "purpose": group.purpose,
                        "equivalent": group.equivalent, "command": display_argv(group.argv),
                        "counter": group.counter, "granularity": group.granularity,
                        "selectors": group.selectors, "requires": list(group.requires),
                        "scenarios": group.scenarios, "expands": list(group.expands),
                        "auto_included": list(group.auto_included),
                        "resources": list(group.resources)}
                       for group in groups.values()],
        }
        if selection is not None:
            payload["selection"] = selection
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return EXIT_OK
    print("验证入口 verify.py（记录 schema %s，入口版本 %d）" % (SCHEMA, WRAPPER_VERSION))
    print("本次将执行：%s\n" % "、".join(selected))
    for group in groups.values():
        print("%s %-12s %s" % ("*" if group.name in selected else " ", group.name, group.title))
        print("    做什么：%s" % group.purpose)
        print("    等价命令：%s" % group.equivalent)
        if group.argv:
            print("    实际命令：%s" % " ".join(display_argv(group.argv)))
        if group.expands:
            print("    展开为：%s" % " → ".join(group.expands))
        print("    计数方式：%s；计时粒度：%s%s" % (
            group.counter, group.granularity,
            "（浏览器按场景计时，记录里带场景执行清单）" if group.scenarios else ""))
        if group.requires:
            print("    需要可执行文件：%s" % "、".join(group.requires))
        if group.selectors:
            print("    用法：python3 verify.py run unit-related tests/test_xxx.py")
        if group.auto_included:
            print("    自动附带：%s（全局词典一致性契约，独立跑相关组也不丢全局保护）"
                  % "、".join(group.auto_included))
        print("    共享资源：%s" % ("、".join(group.resources) if group.resources
                                    else "无（纯逻辑组，不参与资源锁）"))
        print()
    print("隔离：资源锁按同机同用户的共享资源划分（%s），跨 worktree 与独立 checkout 生效；"
          % "、".join(host_isolation.resource_order()))
    for name, entry in host_isolation.resource_declarations().items():
        state = "互斥" if entry["mutually_exclusive"] else "独立"
        print("      %-20s %s：%s" % (name, state, entry["why"]))
    print("      锁只协调验证参与者，不影响也不停止外部进程；锁等待单独计时，超时不是通过。")
    if verify_evidence is not None:
        print("证据复用：run 在 %s 阶段默认引用同输入、同环境、范围覆盖且当前有效的通过记录"
              % "、".join(verify_evidence.REUSE_PHASES))
        print("      （保鲜 %.0f 小时，同一检出内）；依赖宿主机状态的组永远实际执行，"
              "%s 门禁不引用；--no-reuse 强制全部重跑。"
              % (verify_evidence.FRESHNESS_HOURS, "、".join(verify_evidence.GATE_PHASES)))
    else:
        print("证据复用：策略模块加载失败（%s），全部组实际执行。" % _EVIDENCE_IMPORT_ERROR)
    print("记录写在 %s/runs/<run ID>/：record.json 是结构化记录，<组名>.log 是子进程原始输出，"
          "<组名>.cases.json 是用例级计时明细。" % ARTIFACT_DIRNAME)
    print("字段契约：docs/verify/record-contract.md；读取已有结果用 python3 verify.py report --latest")
    if selection is not None:
        print()
        print(render_selection_text(selection))
    return EXIT_OK


# ── 阶段选测与影子对照（issue #55） ────────────────────────────────────────

def build_selection_for(phase: str, baseline: dict, snapshot: dict, artifact_dir: Optional[Path] = None) -> dict:
    """构建选测计划并补上契约附带后的生效选择器；策略层不可用时保守回退。"""
    if verify_selection is None:
        return {"schema": "ae-verify-selection/1", "strategy_version": None,
                "strategy_source": None, "available": False,
                "reason": "策略模块加载失败（%s），影子建议不可用；实际执行不受影响"
                          % _SELECTION_IMPORT_ERROR,
                "phase": phase, "baseline": baseline, "changes": None,
                "fallback": {"kind": "broken_rules", "reason": "策略模块加载失败"},
                "matched_rules": [], "doc_exempt": [], "unknown_paths": [],
                "manifest_check": {"new_tests": [], "deleted_tests": [], "renamed_tests": [],
                                   "problems": []},
                "suggested": {"runs_tests": True, "groups": ["unit"], "selectors": [],
                              "selectors_effective": [], "contract_added": [], "scenarios": [],
                              "serve_check": False, "not_applicable": [],
                              "notes": ["回退完整检查：选测策略层不可用。"]}}
    selection = verify_selection.build_selection(snapshot, baseline, phase, REPO)
    suggested = selection.get("suggested")
    if suggested and suggested.get("selectors"):
        effective, added = expand_related_selectors(list(suggested["selectors"]))
        suggested["selectors_effective"] = display_argv(effective)
        suggested["contract_added"] = display_argv(added)
    try:
        import verify_report
        return verify_report.selection_guard(selection, artifact_dir or artifact_root(None))
    except Exception as exc:
        # 报告模块未安装不改变旧入口；已有守卫却无法读取时不能静默放行。
        if ((artifact_dir or artifact_root(None)) / "efficiency" / "selection-guard.json").exists():
            selection["policy_guard"] = {"active": True, "reason": "策略守卫加载失败：%s" % exc}
            selection["fallback"] = {"kind": "efficiency_guard", "reason": selection["policy_guard"]["reason"]}
            selection["suggested"] = {"runs_tests": True, "groups": ["all"], "selectors": [],
                                      "scenarios": ["*"], "serve_check": True}
        return selection


def compute_shadow(record: dict, selection: Optional[dict]) -> dict:
    """影子对照：建议范围与本次实际执行（完整检查）并列，失败落在建议范围外时保留候选遗漏。

    只有输入、环境、用例身份可对应的样本才用于遗漏判断；输入在运行中变化、
    快照不可用、运行没有正常结束、整次结论是超时 / 入口错误 / 依赖缺失、
    完整单元没有真实执行（空集合 / 全跳过 / 超时 / 加载错误）、实际执行不含
    完整单元时，明确标注「不可比较」，不产出遗漏判断。回退完整检查时建议本身
    就是完整检查，不产出遗漏判断，失败清单照常保留。
    """
    selection = selection or {}
    suggested = selection.get("suggested") or {}
    result = record.get("result") or {}
    shadow = {
        "schema": SHADOW_SCHEMA,
        "strategy_version": selection.get("strategy_version"),
        "comparable": False,
        "incomparable_reason": None,
        "run_outcome": result.get("outcome"),
        "usable_as_evidence": bool(result.get("usable_as_evidence")),
        "coverage": {"unit": False, "serve-check": False, "browser": False},
        "suggested_groups": list(suggested.get("groups") or []),
        "suggested_selectors": list(suggested.get("selectors_effective")
                                    or suggested.get("selectors") or []),
        "executed_groups": [group["name"] for group in record["groups"]
                            if group.get("started_at") is not None],
        "browser_planned_scenarios": list(suggested.get("scenarios") or []),
        "failures": [],
        "candidate_omissions": [],
        "rule_candidates": [],
        "attribution_note": "candidate_omissions 只在可比较且未回退时产出；"
                            "infrastructure 表示加载 / fixture 错误，selector-gap 表示失败"
                            "落在建议范围外；运行中改码由输入失效标注，平台差异由环境段区分",
    }
    executed = set(shadow["executed_groups"])
    shadow["coverage"] = {"unit": "unit" in executed,
                          "serve-check": "serve-check" in executed,
                          "browser": "browser" in executed}
    # 失败是本次执行的事实，不依赖计划是否可比较；先收集，再决定是否判断遗漏。
    for group in record["groups"]:
        if group.get("counter") == "unittest":
            for case in group.get("cases") or []:
                if case.get("status") in (FAILED, ERROR, "unexpected_success"):
                    shadow["failures"].append({
                        "group": group["name"], "target": case.get("id"),
                        "module": (case.get("id") or "").split(".")[0],
                        "status": case.get("status")})
        elif group["name"] == "serve-check" and (group.get("counts") or {}).get("failed"):
            shadow["failures"].append({"group": "serve-check", "target": "服务安全自检",
                                       "status": FAILED})
        else:
            for scenario in group.get("scenarios") or []:
                if scenario.get("status") in ("failed", "error"):
                    shadow["failures"].append({"group": "browser", "target": scenario.get("id"),
                                               "status": scenario.get("status")})
    reasons = []
    if not selection or not selection.get("available"):
        reasons.append("选测计划不可用（%s）" % (selection.get("reason") or "未知原因"))
    if record["input"].get("covers_final_input") is False:
        reasons.append("运行期间有效输入发生变化，开始与结束的输入不匹配")
    elif record["input"].get("covers_final_input") is None:
        reasons.append("输入快照不可用，无法确认观察针对同一输入")
    if record.get("state") != "complete":
        reasons.append("运行没有正常结束")
    run_outcome = shadow["run_outcome"]
    if run_outcome in (TIMEOUT, WRAPPER_ERROR, UNAVAILABLE, INTERRUPTED):
        reasons.append("整次运行结论是 %s（%s），没有正常通过的完整执行可对照"
                       % (run_outcome, VERDICT_LABELS.get(run_outcome, run_outcome)))
    if "unit" in executed:
        unit_outcome = next((group.get("outcome") for group in record["groups"]
                             if group["name"] == "unit"), None)
        if unit_outcome in (TIMEOUT, EMPTY, ERROR, SKIPPED):
            reasons.append("完整单元组的结论是 %s（%s），没有真实执行的用例可对照"
                           % (unit_outcome, VERDICT_LABELS.get(unit_outcome, unit_outcome)))
    if "unit" not in executed:
        reasons.append("实际执行不含完整单元，没有完整对照")
    if (record.get("evidence") or {}).get("integrity") == "degraded":
        reasons.append("执行证据完整性已降级，不能用于遗漏判断")
    if reasons:
        shadow["incomparable_reason"] = "；".join(reasons)
        return shadow
    shadow["comparable"] = True
    fallback = selection.get("fallback")
    if fallback is not None:
        # 回退模式下建议就是完整检查：没有「建议范围外」可言，失败清单照常保留
        shadow["notes"] = ["回退完整检查（%s），本次对照不产出遗漏判断" % fallback["kind"]]
        return shadow
    suggested_modules = set()
    for selector in shadow["suggested_selectors"]:
        prefix = _selector_module_prefix(selector)
        if prefix and "/" not in prefix:
            suggested_modules.add(prefix.split(".")[0])
    suggested_groups = set(shadow["suggested_groups"])
    all_suggested = "all" in suggested_groups
    unit_suggested = all_suggested or "unit" in suggested_groups
    serve_suggested = (all_suggested or "serve-check" in suggested_groups
                       or bool(suggested.get("serve_check")))
    suggested_scenarios = set(shadow["browser_planned_scenarios"])
    browser_suggested = (all_suggested or "browser" in suggested_groups
                         or "*" in suggested_scenarios)
    paths_hint = sorted({path for rule in selection.get("matched_rules") or []
                         for path in rule.get("paths") or []}
                        | set(selection.get("unknown_paths") or []))
    for failure in shadow["failures"]:
        if "module" in failure:
            module = failure["module"]
            if not unit_suggested and module not in suggested_modules:
                attribution = "infrastructure" if failure["status"] == ERROR else "selector-gap"
                shadow["candidate_omissions"].append({
                    "group": failure["group"], "target": failure["target"],
                    "attribution": attribution, "module": module,
                    "note": "失败用例的模块 %s 不在建议范围的选择器里" % module,
                    "paths_hint": paths_hint})
        elif failure["group"] == "serve-check":
            if not serve_suggested:
                shadow["candidate_omissions"].append({
                    "group": "serve-check", "target": "组级", "attribution": "selector-gap",
                    "note": "服务安全自检失败，但建议范围未包含该组",
                    "paths_hint": paths_hint})
        else:
            if not browser_suggested and failure["target"] not in suggested_scenarios:
                shadow["candidate_omissions"].append({
                    "group": "browser", "target": failure["target"],
                    "attribution": "selector-gap",
                    "note": "失败场景不在建议的场景清单里", "paths_hint": paths_hint})
    shadow["rule_candidates"] = [
        {"missing": item["target"], "group": item["group"],
         "suggestion": "把 %s 的失败纳入建议范围：为相关变更路径补映射规则或扩大现有规则"
                       "（受审查的策略变更，%s）"
                       % (item["group"], "、".join((item.get("paths_hint") or [])[:3]) or "相关路径")}
        for item in shadow["candidate_omissions"]]
    return shadow


def render_selection_text(selection: dict) -> str:
    """选测计划的人话解释：差异、命中规则、回退原因、建议范围与影子说明。"""
    baseline = selection.get("baseline") or {}
    lines = ["选测（策略版本 %s，来源 %s，阶段 %s，基线 %s）：" % (
        selection.get("strategy_version"), selection.get("strategy_source"),
        selection.get("phase"), (baseline.get("commit") or "不可解析")[:12])]
    changes = selection.get("changes")
    if changes is not None:
        counts = changes.get("counts") or {}
        lines.append("  有效差异：新增 %d、删除 %d、修改 %d、重命名 %d、未跟踪 %d"
                     "（staged %d、unstaged %d、untracked %d）" % (
                         len(changes.get("added") or []), len(changes.get("deleted") or []),
                         len(changes.get("modified") or []), len(changes.get("renamed") or []),
                         len(changes.get("untracked") or []), counts.get("staged", 0),
                         counts.get("unstaged", 0), counts.get("untracked", 0)))
    for rule in selection.get("matched_rules") or []:
        lines.append("  命中规则：%s —— %s（%s）" % (rule["id"], rule["why"], "、".join(rule["paths"])))
    for item in selection.get("doc_exempt") or []:
        lines.append("  文档豁免：%s（%s）" % (item["path"], item["category"]))
    if selection.get("unknown_paths"):
        lines.append("  未知路径：%s" % "、".join(selection["unknown_paths"]))
    for problem in (selection.get("manifest_check") or {}).get("problems") or []:
        lines.append("  清单检查：%s" % problem)
    fallback = selection.get("fallback")
    if fallback:
        lines.append("  回退完整检查（%s）：%s" % (fallback["kind"], fallback["reason"]))
    suggested = selection.get("suggested") or {}
    for note in suggested.get("notes") or []:
        lines.append("  - %s" % note)
    if suggested.get("runs_tests") is False:
        lines.append("  建议范围：本阶段不自动启动测试（不是「不用测」）。")
    else:
        parts = []
        if suggested.get("groups"):
            parts.append("、".join(suggested["groups"]))
        if suggested.get("selectors_effective"):
            parts.append(" ".join(suggested["selectors_effective"]))
        if suggested.get("contract_added"):
            parts.append("（自动附带契约：%s）" % "、".join(suggested["contract_added"]))
        lines.append("  建议范围：%s" % (" ".join(parts) or "（无）"))
        if suggested.get("scenarios"):
            lines.append("  浏览器场景（计划层对照）：%s" % "、".join(suggested["scenarios"]))
        if suggested.get("not_applicable"):
            lines.append("  不适用：%s（只有确认不适用的组才标注；影子模式实际执行不因此减少）"
                         % "、".join(suggested["not_applicable"]))
    lines.append("  影子模式：建议范围只记录不裁剪；run 把计划与实际执行对照写进记录与 shadow.json。")
    return "\n".join(lines)


# ── 证据复用与流程接入（issue #56） ────────────────────────────────────────

REUSE_SCHEMA = "ae-verify-reuse/1"


def build_reuse_plan(artifact_dir: Path, record: dict, groups: dict, selected: list,
                     bypass: bool = False) -> dict:
    """证据复用规划：决定哪些组引用仍有效的已有验证、哪些组实际执行。

    判定策略在 verify_evidence.py；这里负责组装与降级——策略模块不可用或规划
    自身失败时，全部组实际执行并把原因写进记录，绝不静默把旧记录当成通过。
    """
    group_meta = {name: {"resources": list(groups[name].resources),
                         "counter": groups[name].counter,
                         "selectors_effective": (record["context"].get("selectors_effective") or [])
                         if groups[name].selectors else []}
                  for name in selected}
    bypass_reason = "调用方显式绕过证据复用（--no-reuse）" if bypass else None
    if verify_evidence is None:
        return {"schema": REUSE_SCHEMA, "policy_version": None, "enabled": False,
                "reason": "复用策略模块加载失败（%s），保守全部实际执行" % _EVIDENCE_IMPORT_ERROR,
                "freshness_hours": None,
                "decisions": [{"group": name, "decision": "run",
                               "reason": "复用策略模块不可用，实际执行", "attempts": []}
                              for name in selected]}
    try:
        return verify_evidence.build_reuse_plan(
            artifact_dir=artifact_dir, record=record, requested=selected,
            group_meta=group_meta, prefix_of=_selector_module_prefix,
            load_record=load_record, repo=REPO, bypass_reason=bypass_reason)
    except Interrupted:
        raise  # 用户取消必须结束本次运行，不能当作策略损坏后继续执行。
    except Exception as exc:  # 复用规划出错不能拖垮验证：全部实际执行并写明原因
        return {"schema": REUSE_SCHEMA, "policy_version": verify_evidence.POLICY_VERSION,
                "enabled": False,
                "reason": "证据复用规划失败（%s: %s），保守全部实际执行"
                          % (type(exc).__name__, exc),
                "freshness_hours": None,
                "decisions": [{"group": name, "decision": "run",
                               "reason": "复用规划失败，实际执行", "attempts": []}
                              for name in selected]}


def reused_group_result(group: Group, decision: dict) -> dict:
    """引用有效证据的组结果：不启动子进程，也不伪装成刚执行。

    counts 留空（引用不重复计入通过数量），exit_status 为 0（验证由有效证据满足）；
    引用链（来源 run、时间、指纹、理由）单独放在 reuse 字段。
    """
    return {"name": group.name, "outcome": REUSED, "exit_status": EXIT_OK, "signal": None,
            "timed_out": False, "note": None, "counts": None, "cases": [], "seconds": {},
            "scenario_summary": None,
            "timing": {"child_seconds": 0.0, "case_seconds_total": 0.0,
                       "fixture_seconds_total": 0.0, "unattributed_seconds": 0.0},
            "lock": {"resources": list(group.resources), "wait_seconds": 0.0},
            "reuse": {"source_run_id": decision.get("source_run_id"),
                      "source_ended_at": decision.get("source_ended_at"),
                      "source_fingerprint": decision.get("source_fingerprint"),
                      "source_scope": decision.get("source_scope"),
                      "source_environment": decision.get("source_environment"),
                      "age_hours": decision.get("age_hours"),
                      "reason": decision.get("reason")}}


def validate_reuse_chain(record: dict, root: Path, problems: list, visiting: Optional[set] = None) -> list:
    """报告读取时重校验引用链：来源被篡改、缺失或失效时降级为不可用（issue #56）。

    落盘原件不改写；问题追加进 problems（与 report_validation.problems 同一列表），
    引用链失效时同步降级完整性与影子结论；来源的其他组也可能引用更早证据，
    读取来源时递归校验并拒绝循环。
    """
    chain_problems = []
    for group in record.get("groups") or []:
        if group.get("outcome") != REUSED:
            continue
        reference = group.get("reuse") or {}
        run_id = reference.get("source_run_id")
        if not isinstance(run_id, str) or not run_id or Path(run_id).name != run_id or run_id in (".", ".."):
            chain_problems.append("组 %s 标记为引用已有验证，但没有来源 run ID"
                                  % group.get("name"))
            continue
        source, source_problems = load_record(root / "runs" / run_id, visiting)
        if source is None or source_problems:
            chain_problems.append("引用的证据 run %s 当前校验不可用：%s"
                                  % (run_id, "；".join(source_problems) or "读不出来"))
            continue
        if (source.get("input") or {}).get("fingerprint") != reference.get("source_fingerprint"):
            chain_problems.append("引用的证据 run %s 输入指纹与引用时不一致" % run_id)
        source_group = next((item for item in source.get("groups") or []
                             if item.get("name") == group.get("name")), None)
        if source_group is None or source_group.get("outcome") != PASSED:
            chain_problems.append("引用的证据 run %s 的 %s 组不再是实际通过的执行"
                                  % (run_id, group.get("name")))
        if not (source.get("result") or {}).get("usable_as_evidence"):
            chain_problems.append("引用的证据 run %s 当前不能作为通过证据" % run_id)
        if verify_evidence is None:
            chain_problems.append("复用策略不可用，无法校验引用的证据 run %s" % run_id)
        else:
            try:
                reason = verify_evidence.reference_reject_reason(source, record, group, REPO,
                                                                 _selector_module_prefix)
            except Interrupted:
                raise
            except Exception as exc:
                reason = "引用有效性无法确认（%s）" % type(exc).__name__
            if reason:
                chain_problems.append("引用的证据 run %s 当前不满足复用条件：%s" % (run_id, reason))
    problems.extend(chain_problems)
    if chain_problems:
        degrade_evidence(record, problems)
    return chain_problems


def build_record(args, groups: dict, selected: list) -> dict:
    started_at = now_iso()
    snapshot = snapshot_input()
    baseline = resolve_baseline(args.baseline)
    selection = build_selection_for(args.phase, baseline, snapshot, artifact_root(args.out_dir))
    return {
        "schema": SCHEMA,
        "wrapper_version": WRAPPER_VERSION,
        "state": "running",
        "run_id": None,
        "started_at": started_at,
        "ended_at": None,
        "wrapper": {"name": "verify.py", "version": WRAPPER_VERSION, "schema": SCHEMA,
                    "argv": display_argv(list(sys.argv)), "cwd_mode": "repo-root",
                    "python": platform.python_version()},
        "context": {"phase": args.phase, "issue": args.issue, "pr": args.pr, "reason": args.reason,
                    "session": logical_session(),
                    "groups_requested": args.group, "groups_selected": selected,
                    "selectors": display_argv(args.selectors),
                    "selectors_effective": display_argv(
                        expand_related_selectors(list(args.selectors or []))[0]
                        if args.selectors else []),
                    "selection": selection},
        "groups": [{"name": name, "title": groups[name].title,
                    "command": display_argv(groups[name].argv),
                    "equivalent": groups[name].equivalent, "counter": groups[name].counter,
                    "granularity": groups[name].granularity,
                    "scenarios": [] if groups[name].scenarios else None,
                    "resources": list(groups[name].resources),
                    "lock": {"resources": list(groups[name].resources), "wait_seconds": None},
                    "started_at": None, "outcome": None, "exit_status": None} for name in selected],
        "commit": commit_summary(snapshot),
        "input": {
            "available": snapshot.get("available"),
            "reason": snapshot.get("reason"),
            "fingerprint": snapshot.get("fingerprint"),
            "final_fingerprint": None,
            "fingerprint_scope": snapshot.get("fingerprint_scope"),
            "baseline": baseline,
            "changed_since_baseline": changed_since_baseline(baseline.get("commit"), snapshot),
            "at_start": snapshot,
            "at_end": None,
            "changed_during_run": None,
            "covers_final_input": None,
            "stale_reason": None,
        },
        "environment": environment_summary(),
        "timing": {"queued_at": started_at, "started_at": None, "ended_at": None,
                   "queue_seconds": None, "wall_seconds": None, "wrapper_seconds": None,
                   "lock_wait_seconds": None, "cleanup_seconds": None,
                   "first_feedback_seconds": None,
                   "case_seconds_total": None, "fixture_seconds_total": None,
                   "unattributed_seconds": None,
                   "attribution_note": "case_seconds_total 是各用例耗时之和，不等于 wall_seconds；"
                                       "类 / 模块级开销计入 fixture_seconds_total，不摊到任何单个用例；"
                                       "锁等待单列为 lock_wait_seconds，不计入口开销，也不摊进执行耗时"},
        "result": {"outcome": None, "exit_status": None, "counts": {}, "summary": None,
                   "usable_as_evidence": False, "notes": []},
        "shadow": None,
        "reuse": None,
        "evidence": {"integrity": "ok", "record_path": "record.json", "logs": [], "cases": [],
                     "notes": []},
    }


def fill_result(record: dict, ctx: RunContext) -> None:
    outcomes = [group.get("outcome") or WRAPPER_ERROR for group in record["groups"]]
    counts = {"groups": sum(group.get("outcome") != REUSED for group in record["groups"]),
              "cases": 0, "checks": 0, "passed": 0, "failed": 0,
              "errors": 0, "skipped": 0, "expected_failures": 0, "unexpected_successes": 0,
              "subtest_failures": 0, "subtest_errors": 0, "subtest_skips": 0, "interrupted": 0,
              "fixture_errors": 0, "fixture_skips": 0}
    for group in record["groups"]:
        group_counts = group.get("counts") or {}
        if group.get("counter") == "unittest":
            for key in ("cases", "passed", "failed", "errors", "skipped", "expected_failures",
                        "unexpected_successes", "subtest_failures", "subtest_errors", "subtest_skips",
                        "interrupted", "fixture_errors", "fixture_skips"):
                counts[key] += group_counts.get(key) or 0
        elif group.get("counter") == "log":
            counts["checks"] += group_counts.get("total") or 0
            counts["passed"] += group_counts.get("passed") or 0
            counts["failed"] += group_counts.get("failed") or 0
    exit_status = overall_exit(outcomes, record["groups"])
    outcome = worst_outcome(outcomes)
    usable = (record["state"] == "complete" and outcome == PASSED and exit_status == EXIT_OK
              and record["input"].get("covers_final_input") is True
              and record["evidence"].get("integrity") == "ok")
    notes = list(record["result"].get("notes") or []) + list(ctx.notes)
    if record["input"].get("covers_final_input") is False:
        notes.append("运行期间有效输入发生变化：本次记录不能覆盖最终输入")
    if outcome == PASSED and not usable:
        notes.append("结论是通过，但记录不能作为通过证据")
    reused_names = [group["name"] for group in record["groups"] if group.get("outcome") == REUSED]
    if reused_names:
        notes.append("%d 个组引用已有验证、未重复执行：%s（引用不计入通过数量）"
                     % (len(reused_names), "、".join(reused_names)))
    record["result"].update(outcome=outcome, exit_status=exit_status, counts=counts,
                            summary=summarize(record), usable_as_evidence=usable, notes=notes)


def fill_evidence(record: dict, ctx: RunContext) -> None:
    logs, cases = [], []
    integrity = "ok"
    for group in record["groups"]:
        if group.get("outcome") == REUSED:
            continue  # 引用的组没有本地子进程产物；引用链在 reuse 段与报告里呈现
        log_path = ctx.run_dir / ("%s.log" % group["name"])
        if log_path.is_file():
            logs.append({"group": group["name"], "path": log_path.name,
                         "bytes": log_path.stat().st_size, "sha256": digest_file(log_path)})
        else:
            logs.append({"group": group["name"], "path": log_path.name, "missing": True})
            integrity = "degraded"
        if group.get("counter") == "unittest":
            cases_path = ctx.run_dir / ("%s.cases.json" % group["name"])
            if cases_path.is_file():
                cases.append({"group": group["name"], "path": cases_path.name,
                              "cases": len(group.get("cases") or [])})
            else:
                cases.append({"group": group["name"], "path": cases_path.name, "missing": True})
                integrity = "degraded"
    record["evidence"].update(integrity=integrity, logs=logs, cases=cases, notes=list(ctx.notes))


def cmd_run(args) -> int:
    if args.phase not in PHASES:
        raise UsageError("未知的开发阶段：%s（可选：%s）" % (args.phase, "、".join(PHASES)))
    # 「run unit …」「run unit-related tests/test_x.py」这类写法：第一个位置参数是组名时当组名用
    if args.group is None and args.selectors and args.selectors[0] in build_groups():
        args.group = [args.selectors.pop(0)]
    if args.selectors and args.group and args.group != ["unit-related"]:
        raise UsageError("选择器只能配合 --group unit-related 使用")
    if not args.group:
        args.group = ["unit-related"] if args.selectors else ["unit"]
    if "unit-related" in args.group and not args.selectors:
        raise UsageError("unit-related 需要显式选择器，例如 tests/test_core.py；"
                         "不给选择器就是空集合，入口不把它当成通过")
    groups = build_groups(args.selectors)
    selected = expand_groups(args.group, groups)
    ctx = RunContext(new_run_id(), artifact_root(args.out_dir), args.timeout,
                     sys.stderr if args.as_json else sys.stdout, args.quiet)
    record = build_record(args, groups, selected)
    guard = (record["context"]["selection"].get("policy_guard") or {})
    if guard.get("active"):
        selected = expand_groups(["all"], groups)
        record = build_record(args, groups, selected)
        record["result"]["notes"].append("效率守卫扩大实际执行范围到 all：%s" % guard["reason"])
        args.no_reuse = True
    record["run_id"] = ctx.run_id
    record["reuse"] = {"schema": REUSE_SCHEMA, "policy_version": None, "enabled": False,
                       "reason": "复用规划尚未完成", "freshness_hours": None, "decisions": []}
    queued_at = time.monotonic()
    try:
        ctx.run_dir.mkdir(parents=True, exist_ok=True)
        write_json_atomic(ctx.artifact_dir / "latest.json",
                          {"run_id": ctx.run_id, "started_at": record["started_at"]})
        ctx.save_record(record)
    except OSError as exc:
        print("入口自身失败：写不了验证记录（%s）" % exc, file=sys.stderr)
        return EXIT_WRAPPER

    install_signal_handlers()
    started = time.monotonic()
    interrupted_signum = None
    wrapper_error = None
    results = {}

    def merge_group_results() -> None:
        """把已完成的组结果合并进记录；没拿到结果的组才按中断 / 入口失败补齐。"""
        started_groups = {entry["group"]: entry["started_at"] for entry in ctx.registry.entries}
        record["timing"]["wall_seconds"] = round(time.monotonic() - started, 6)
        child_total = round(sum((item.get("timing") or {}).get("child_seconds") or 0
                                for item in results.values()), 6)
        lock_wait_total = round(sum((item.get("lock") or {}).get("wait_seconds") or 0
                                    for item in results.values()), 6)
        # 锁等待不是入口开销，也不是执行耗时：单独一列，不摊进 wrapper_seconds
        record["timing"]["wrapper_seconds"] = round(
            record["timing"]["wall_seconds"] - child_total - lock_wait_total, 6)
        for key in ("case_seconds_total", "fixture_seconds_total", "unattributed_seconds"):
            record["timing"][key] = round(sum((item.get("timing") or {}).get(key) or 0
                                              for item in results.values()), 6)
        record["timing"]["lock_wait_seconds"] = lock_wait_total
        record["timing"]["cleanup_seconds"] = round(getattr(ctx, "cleanup_seconds", 0.0), 6)
        record["timing"]["ended_at"] = now_iso()
        for group in record["groups"]:
            # 登记簿只收录确实启动的子进程；排队超时、缺依赖和取消后的计划组为空。
            group["started_at"] = started_groups.get(group["name"])
            if group.get("outcome") is not None:
                continue  # 收尾被打断后重跑时，已合并的组不重复覆盖
            item = results.get(group["name"])
            if item is not None:
                group.update(outcome=item["outcome"], exit_status=item["exit_status"],
                             signal=item["signal"], timed_out=item["timed_out"], note=item["note"],
                             counts=item["counts"], cases=trim_cases(item["cases"]),
                             seconds=item["seconds"], timing=item["timing"],
                             lock=item.get("lock") or group.get("lock"))
                if item.get("reuse") is not None:
                    group["reuse"] = item["reuse"]
                if item.get("scenario_summary") is not None:
                    # 浏览器组带场景摘要时填 groups[].scenarios，粒度细化到场景（issue #53）
                    group["scenarios"] = item["scenario_summary"]["scenarios"]
                    group["granularity"] = "scenario"
                if item.get("browser_environment") is not None:
                    group["browser_environment"] = item["browser_environment"]
                continue
            if interrupted_signum is not None:
                group.update(outcome=INTERRUPTED,
                             exit_status=EXIT_INTERRUPTED_BASE + interrupted_signum,
                             counts=None, cases=[], seconds={}, timing={},
                             note="本组没有拿到执行结果")
            else:
                group.update(outcome=WRAPPER_ERROR, exit_status=EXIT_WRAPPER, counts=None,
                             cases=[], seconds={}, timing={}, note="本组没有拿到执行结果")

    def seal_run(skip_end_snapshot: bool) -> None:
        """填齐结果与证据并落最终记录；收尾被信号打断时跳过结束快照。"""
        merge_group_results()
        if skip_end_snapshot and record["input"].get("at_end") is None:
            record["input"]["at_end"] = {"available": False,
                                         "reason": "收尾被信号打断，没有取到结束快照",
                                         "fingerprint": None}
            record["input"]["final_fingerprint"] = None
            record["input"]["changed_during_run"] = None
            record["input"]["covers_final_input"] = None
            record["input"]["stale_reason"] = "收尾被信号打断，没有结束快照"
        record["state"] = "aborted" if interrupted_signum is not None else "complete"
        if wrapper_error is not None:
            record["state"] = "complete"
        if interrupted_signum is not None:
            record["result"]["notes"] = list(record["result"].get("notes") or []) + [
                "入口收到 %s，已回收本次登记的子进程"
                % (signal_name(-interrupted_signum) or interrupted_signum)]
        fill_evidence(record, ctx)
        fill_result(record, ctx)
        if interrupted_signum is not None:
            # 先确定最终结论，再生成影子记录，避免收尾中断时两处结论不一致。
            record["result"].update(outcome=INTERRUPTED,
                                    exit_status=EXIT_INTERRUPTED_BASE + interrupted_signum,
                                    usable_as_evidence=False)
        # 影子对照（issue #55）：建议范围与本次实际执行并列；不改变结论与退出码
        try:
            record["shadow"] = compute_shadow(record, record["context"].get("selection"))
        except Exception as exc:  # 策略层问题不能吞掉门禁结果，只降级影子说明
            record["shadow"] = {"schema": SHADOW_SCHEMA, "comparable": False,
                                "incomparable_reason": "影子对照计算失败：%s: %s"
                                                       % (type(exc).__name__, exc),
                                "run_outcome": record["result"].get("outcome"),
                                "usable_as_evidence": False,
                                "candidate_omissions": [], "failures": [],
                                "rule_candidates": []}
        with shield_termination():
            _finish(record, ctx, args)

    sealed = False
    try:
        # 规划与保存同样受取消保护；规划耗时单独记入排队时间。
        record["reuse"] = build_reuse_plan(ctx.artifact_dir, record, groups, selected,
                                           bypass=bool(args.no_reuse))
        ctx.save_record(record)
        reuse_decisions = {item["group"]: item for item in record["reuse"]["decisions"]}
        record["timing"]["queue_seconds"] = round(time.monotonic() - queued_at, 6)
        record["timing"]["started_at"] = now_iso()
        started = time.monotonic()
        for name in selected:
            decision = reuse_decisions.get(name)
            if decision is not None and decision.get("decision") == "reuse":
                # 引用有效证据：不启动子进程，组结果标记 reused（G 项）
                results[name] = reused_group_result(groups[name], decision)
                continue
            results[name] = execute_group(groups[name], ctx)
            if record["timing"]["first_feedback_seconds"] is None:
                record["timing"]["first_feedback_seconds"] = round(time.monotonic() - queued_at, 6)
            if results[name].get("interrupted_signum") is not None:
                interrupted_signum = results[name]["interrupted_signum"]
                break  # 结果已完整收集；后续组不再执行，整次仍按入口中断落盘
    except Interrupted as exc:
        interrupted_signum = exc.signum
        with shield_termination():
            ctx.abort(exc.signum)
    except Exception as exc:  # 入口自身出错也要留下记录，并且不冒充产品失败
        wrapper_error = "%s: %s" % (type(exc).__name__, exc)
        with shield_termination():
            ctx.abort(signal.SIGTERM)

    if record["timing"]["started_at"] is None:
        record["timing"]["queue_seconds"] = round(time.monotonic() - queued_at, 6)
        record["timing"]["started_at"] = now_iso()
        started = time.monotonic()
    try:
        end_snapshot = snapshot_input()
        record["input"]["at_end"] = end_snapshot
        record["input"]["final_fingerprint"] = end_snapshot.get("fingerprint")
        if not end_snapshot.get("available") or not record["input"]["available"]:
            record["input"]["changed_during_run"] = None
            record["input"]["covers_final_input"] = None
            record["input"]["stale_reason"] = end_snapshot.get("reason") or record["input"]["reason"]
        else:
            changed = record["input"]["fingerprint"] != end_snapshot.get("fingerprint")
            record["input"]["changed_during_run"] = changed
            record["input"]["covers_final_input"] = not changed
            record["input"]["stale_reason"] = "运行期间输入指纹发生变化" if changed else None
        seal_run(skip_end_snapshot=False)
        sealed = True
    except Interrupted as exc:
        # 收尾段（结束快照、合并结果、落盘）被打断：照常回收登记的子进程、
        # 落最终记录并标 aborted，再按 128+信号退出
        interrupted_signum = exc.signum
        with shield_termination():
            ctx.abort(exc.signum)
            if not sealed:
                seal_run(skip_end_snapshot=True)
                sealed = True

    if interrupted_signum is not None:
        return EXIT_INTERRUPTED_BASE + interrupted_signum
    if wrapper_error is not None:
        print("入口自身失败：%s" % wrapper_error, file=sys.stderr)
        return EXIT_WRAPPER
    return record["result"]["exit_status"]


def _finish(record: dict, ctx: RunContext, args) -> None:
    """落最终记录；写不进去要明确报告，并且不抹掉子进程的退出码。"""
    record["ended_at"] = now_iso()
    try:
        append_jsonl(ctx.artifact_dir / "index.jsonl",
                     {"run_id": record["run_id"], "at": record["ended_at"], "state": record["state"],
                      "outcome": record["result"]["outcome"], "exit_status": record["result"]["exit_status"],
                      "groups": [group["name"] for group in record["groups"]],
                      "reused_groups": [group["name"] for group in record["groups"]
                                        if group.get("outcome") == REUSED],
                      "phase": record["context"]["phase"], "issue": record["context"]["issue"],
                      "pr": record["context"]["pr"]})
    except OSError as exc:
        record["result"]["notes"] = list(record["result"].get("notes") or []) + [
            "索引追加失败（不影响本次结论）：%s" % exc]
    try:
        ctx.save_record(record)
    except OSError as exc:
        message = "入口自身失败：最终记录写不进去（%s）" % exc
        print(message, file=sys.stderr)
        record["result"]["notes"] = list(record["result"].get("notes") or []) + [message]
        if record["result"].get("exit_status") == EXIT_OK:
            record["result"]["exit_status"] = EXIT_WRAPPER
        return
    shadow = record.get("shadow")
    if shadow is not None:
        try:
            write_json_atomic(ctx.run_dir / "shadow.json", shadow)
        except OSError as exc:
            record["result"]["notes"] = list(record["result"].get("notes") or []) + [
                "shadow.json 写入失败（影子数据只在 record.json 里）：%s" % exc]
    # 持续报告只读取已落盘的证据；分组产品资源已经释放，不改变原结论或 JSON。
    try:
        import verify_report
        events_path = ctx.artifact_dir / "ci-events.json"
        events = verify_report.read_events(events_path) if events_path.exists() else None
        summary = verify_report.rebuild(ctx.artifact_dir, verify_report.sources(ctx.artifact_dir, [], events),
                                        load_record, events=events)
        print("效率摘要：%s/efficiency/summary.md（样本 %d，累计执行 %.3f 秒，执行区间并集 %.3f 秒；%s）" % (
            _show(ctx.artifact_dir), summary["metrics"]["runs"], summary["metrics"]["execution_seconds"],
            summary["metrics"]["execution_union_seconds"], summary["review"]["reason"]),
            file=sys.stderr if args.as_json else sys.stdout)
    except Exception as exc:
        print("效率摘要缺口：%s（保留原始验证结论，可用 report --efficiency --rebuild 重建）" % exc,
              file=sys.stderr)
    if args.as_json:
        print(json.dumps(record, ensure_ascii=False, indent=2))
    else:
        print(render_report(record, root=ctx.artifact_dir))


def install_signal_handlers() -> None:
    def handler(signum, frame):
        raise Interrupted(signum)

    for name in ("SIGINT", "SIGTERM", "SIGHUP"):
        sig = getattr(signal, name, None)
        if sig is None:
            continue
        try:
            signal.signal(sig, handler)
        except (OSError, ValueError):
            pass


@contextlib.contextmanager
def capture_termination(result: dict):
    """正常组收尾时保留首次取消，等完整结果交回 cmd_run 后再结束整次运行。

    处理器只更新待返回的同一个对象；结果装配、日志落盘和上下文退出之间收到
    信号都能留下记录，不必抛出异常而丢掉已经完成的组。
    """
    def handler(signum, frame):
        if result.get("interrupted_signum") is None:
            result["interrupted_signum"] = signum

    installed = []
    for name in ("SIGINT", "SIGTERM", "SIGHUP"):
        sig = getattr(signal, name, None)
        if sig is None:
            continue
        try:
            previous = signal.signal(sig, handler)
        except (OSError, ValueError):
            continue
        installed.append((sig, previous))
    try:
        yield
    finally:
        for sig, previous in installed:
            try:
                signal.signal(sig, previous)
            except (OSError, ValueError):
                pass


@contextlib.contextmanager
def shield_termination():
    """回收子进程与落最终记录的关键窗口里，终止信号不再打断收尾。

    契约承诺入口被信号中断时先回收登记的子进程、写下最终记录再按 128+信号退出；
    如果回收的宽限等待或落盘本身能被第二个信号打断，这条承诺就守不住。
    """
    installed = []
    for name in ("SIGINT", "SIGTERM", "SIGHUP"):
        sig = getattr(signal, name, None)
        if sig is None:
            continue
        try:
            previous = signal.signal(sig, signal.SIG_IGN)
        except (OSError, ValueError):
            continue
        installed.append((sig, previous))
    try:
        yield
    finally:
        for sig, previous in installed:
            try:
                signal.signal(sig, previous)
            except (OSError, ValueError):
                pass


# ── 报告 ──────────────────────────────────────────────────────────────────

def degrade_evidence(record: dict, problems: list) -> None:
    """完整性、可用性与影子结论使用同一降级规则，保留原始执行结论。"""
    record.setdefault("result", {})["usable_as_evidence"] = False
    record.setdefault("evidence", {})["integrity"] = "degraded"
    shadow = record.get("shadow")
    if isinstance(shadow, dict):
        shadow.update(usable_as_evidence=False, comparable=False,
                      incomparable_reason="报告读取时证据校验失败：%s" % "；".join(problems),
                      candidate_omissions=[], rule_candidates=[])


def load_record(run_dir: Path, visiting: Optional[set] = None) -> tuple:
    """返回 (记录或 None, 问题列表)。半写、损坏、版本不认识的记录一律算证据不可用。"""
    key = str(run_dir.resolve())
    if key in (visiting or set()):
        return None, ["引用链出现循环：%s" % run_dir.name]
    visiting = set(visiting or ()) | {key}
    path = run_dir / "record.json"
    if not run_dir.is_dir():
        return None, ["找不到运行目录 %s" % run_dir.name]
    if not path.is_file():
        return None, ["%s 里没有 record.json：本次运行没有留下结束记录" % run_dir.name]
    try:
        record = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return None, ["record.json 读不出来（%s）：证据不可用" % exc]
    if not isinstance(record, dict):
        return None, ["record.json 不是对象：证据不可用"]
    schema = record.get("schema")
    if schema != SCHEMA:
        return None, ["不支持的记录 schema：%r（本入口支持 %s）" % (schema, SCHEMA)]
    problems = []
    if record.get("state") != "complete":
        problems.append("记录状态是 %r：本次运行没有正常结束" % record.get("state"))
    integrity = (record.get("evidence") or {}).get("integrity")
    if integrity not in (None, "ok"):
        problems.append("证据完整性 %r" % integrity)
    for log in (record.get("evidence") or {}).get("logs") or []:
        name = log.get("path") or ""
        if not name or os.sep in name or name in (".", ".."):
            problems.append("日志路径不合法：%r" % name)
            continue
        target = run_dir / name
        if log.get("missing") or not target.is_file():
            problems.append("缺少日志 %s" % name)
        elif log.get("sha256") and digest_file(target) != log["sha256"]:
            problems.append("日志 %s 校验和不一致" % name)
    record["report_validation"] = {"problems": problems}
    if problems:
        degrade_evidence(record, problems)
    validate_reuse_chain(record, run_dir.parent.parent, problems, visiting)
    return record, problems


def report_exit(record: Optional[dict], problems: list) -> int:
    if problems or not record:
        return EXIT_FAILED
    return EXIT_OK if (record.get("result") or {}).get("usable_as_evidence") else EXIT_FAILED


def cmd_report(args) -> int:
    if args.efficiency:
        if args.run or args.latest:
            raise UsageError("--efficiency 不与 --run / --latest 混用")
        import verify_report
        now = verify_report.instant(args.now) if args.now else None
        if args.now and now is None:
            raise UsageError("--now 需要带时区的 ISO 8601 时间")
        root = artifact_root(args.out_dir)
        events_path = Path(args.events) if args.events else root / "ci-events.json"
        events = verify_report.read_events(events_path) if args.events or events_path.exists() else None
        roots = verify_report.sources(root, args.source, events)
        summary = (verify_report.rebuild(root, roots, load_record, now=now, events=events) if args.rebuild else
                   verify_report.build_summary(roots, load_record, now=now, events=events))
        print(json.dumps(summary, ensure_ascii=False, indent=2) if args.as_json else verify_report.render(summary))
        return EXIT_FAILED if summary["gaps"] else EXIT_OK
    if args.rebuild or args.source or args.events or args.now:
        raise UsageError("历史报告选项需要 --efficiency")
    if args.run and args.latest:
        raise UsageError("--run 与 --latest 只能用一个")
    root = artifact_root(args.out_dir)
    run_id = args.run
    if not run_id:
        pointer = root / "latest.json"
        if not pointer.is_file():
            print("找不到 %s/latest.json：还没有可报告的验证记录" % _show(root), file=sys.stderr)
            return EXIT_FAILED
        try:
            run_id = json.loads(pointer.read_text(encoding="utf-8")).get("run_id")
        except (OSError, ValueError) as exc:
            print("latest.json 读不出来（%s）：证据不可用" % exc, file=sys.stderr)
            return EXIT_FAILED
    if not run_id:
        print("latest.json 里没有 run_id：证据不可用", file=sys.stderr)
        return EXIT_FAILED
    record, problems = load_record(root / "runs" / run_id)
    if args.as_json:
        print(json.dumps(record if record is not None
                         else {"schema": SCHEMA, "run_id": run_id, "problems": problems},
                         ensure_ascii=False, indent=2))
        return report_exit(record, problems)
    if record is None:
        print("证据不可用（run %s）：%s" % (run_id, "；".join(problems)), file=sys.stderr)
        return EXIT_FAILED
    print(render_report(record, problems, root=root))
    return report_exit(record, problems)


def _show(root: Path) -> str:
    try:
        return str(root.relative_to(REPO))
    except ValueError:
        return str(root)


def render_report(record: dict, problems: Optional[list] = None,
                  root: Optional[Path] = None) -> str:
    problems = list(problems or [])
    result = record.get("result") or {}
    lines = ["验证记录 %s · run %s" % (record.get("schema"), record.get("run_id")),
             "结论：%s（%s，退出码 %s）%s" % (
                 VERDICT_LABELS.get(result.get("outcome"), result.get("outcome")),
                 result.get("outcome"), result.get("exit_status"),
                 "；可作为通过证据" if result.get("usable_as_evidence") else "；不能作为通过证据")]
    if record.get("state") != "complete":
        lines.append("状态：%s（没有正常结束）" % record.get("state"))
    context = record.get("context") or {}
    lines.append("阶段：%s；issue %s；PR %s；理由：%s" % (
        context.get("phase"), context.get("issue") or "—", context.get("pr") or "—",
        context.get("reason") or "—"))
    commit = record.get("commit") or {}
    baseline = (record.get("input") or {}).get("baseline") or {}
    lines.append("提交：%s（%s）· 基线 %s（%s）· 分支 %s · 工作树%s" % (
        commit.get("short") or commit.get("head") or "—", commit.get("subject") or "—",
        (baseline.get("commit") or "—")[:12], baseline.get("source"),
        commit.get("branch") or "—", "有未提交改动" if commit.get("working_tree_dirty") else "干净"))
    info = record.get("input") or {}
    lines.append("输入：%s · 开始与结束%s%s" % (
        (info.get("fingerprint") or "不可用")[:23],
        "一致" if info.get("changed_during_run") is False else "不一致",
        " → 覆盖最终输入" if info.get("covers_final_input") else " → 不能覆盖最终输入"))
    if info.get("stale_reason"):
        lines.append("     %s" % info["stale_reason"])
    env = record.get("environment") or {}
    lines.append("环境：%s %s %s · %s %s · CI %s" % (
        (env.get("os") or {}).get("system"), (env.get("os") or {}).get("release"),
        (env.get("os") or {}).get("machine"), (env.get("python") or {}).get("implementation"),
        (env.get("python") or {}).get("version"), "是" if (env.get("ci") or {}).get("is_ci") else "否"))
    isolation = env.get("isolation") or {}
    if isolation:
        lines.append("隔离：%s 锁，位置来源 %s，跨 worktree 生效；锁只协调参与者，不停止外部进程" % (
            isolation.get("mechanism"), isolation.get("lock_dir_source")))
    timing = record.get("timing") or {}
    lines.append("耗时：排队 %s 秒，锁等待 %s 秒，墙钟 %s 秒，入口自身 %s 秒，清理 %s 秒" % (
        timing.get("queue_seconds"), timing.get("lock_wait_seconds"),
        timing.get("wall_seconds"), timing.get("wrapper_seconds"),
        timing.get("cleanup_seconds")))
    lines.append("     用例耗时合计 %s 秒，类 / 模块级开销 %s 秒，未归属 %s 秒（合计不是等待时间）" % (
        timing.get("case_seconds_total"), timing.get("fixture_seconds_total"),
        timing.get("unattributed_seconds")))
    lines.append("组：")
    for group in record.get("groups") or []:
        if group.get("outcome") == REUSED:
            reference = group.get("reuse") or {}
            lines.append("  %-11s %-11s 引用已有验证 ← run %s（%s 结束，%.1f 小时前）" % (
                group.get("name"), group.get("outcome"), reference.get("source_run_id"),
                reference.get("source_ended_at"), reference.get("age_hours") or 0.0))
            continue
        counts = group.get("counts") or {}
        if group.get("counter") == "unittest":
            detail = "用例 %s（通过 %s / 失败 %s / 错误 %s / 跳过 %s / 中断 %s / 子测试失败 %s）" % (
                counts.get("cases", 0), counts.get("passed", 0), counts.get("failed", 0),
                counts.get("errors", 0), counts.get("skipped", 0), counts.get("interrupted", 0),
                counts.get("subtest_failures", 0))
            if counts.get("fixture_errors") or counts.get("fixture_skips"):
                detail += "；类 / 模块准备清理：错误 %s / 跳过 %s" % (
                    counts.get("fixture_errors", 0), counts.get("fixture_skips", 0))
        elif group.get("counter") == "log":
            detail = "检查 %s / %s" % (counts.get("passed", 0), counts.get("total", 0))
        else:
            detail = "—"
        lines.append("  %-11s %-11s %s · %s 秒 · 粒度 %s" % (
            group.get("name"), group.get("outcome"), detail,
            (group.get("timing") or {}).get("child_seconds"), group.get("granularity")))
        lock = group.get("lock") or {}
        if lock.get("resources"):
            lines.append("     锁：%s，等待 %s 秒%s" % (
                "、".join(lock["resources"]), lock.get("wait_seconds"),
                "（等待超时，本组没有执行）" if lock.get("timeout") else ""))
        scenarios = group.get("scenarios") or []
        if scenarios:
            parts = []
            for item in scenarios:
                checks = item.get("checks") or {}
                parts.append("%s %s（%s/%s，%s 秒）" % (
                    item.get("id"), SCENARIO_LABELS.get(item.get("status"), item.get("status")),
                    checks.get("passed", 0), checks.get("total", 0), item.get("seconds")))
            lines.append("     场景：%s" % "；".join(parts))
        if group.get("note"):
            lines.append("     注意：%s" % group["note"])
    slow = sorted((case for group in record.get("groups") or [] for case in (group.get("cases") or [])),
                  key=lambda case: case.get("seconds") or 0, reverse=True)[:5]
    shadow = record.get("shadow")
    if shadow:
        suggested_parts = ["、".join(shadow.get("suggested_groups") or ["（无）"])]
        if shadow.get("suggested_selectors"):
            suggested_parts.append(" ".join(shadow["suggested_selectors"]))
        lines.append("影子对照（策略版本 %s）：建议 %s；实际执行 %s" % (
            shadow.get("strategy_version"), " ".join(suggested_parts),
            "、".join(shadow.get("executed_groups") or [])))
        if shadow.get("comparable"):
            coverage = shadow.get("coverage") or {}
            lines.append("  可比较：是；覆盖 完整单元 %s / 服务自检 %s / 浏览器 %s" % (
                "是" if coverage.get("unit") else "否", "是" if coverage.get("serve-check") else "否",
                "是" if coverage.get("browser") else "否"))
        else:
            lines.append("  不可比较：%s" % shadow.get("incomparable_reason"))
        if shadow.get("browser_planned_scenarios") and not (shadow.get("coverage") or {}).get("browser"):
            lines.append("  浏览器场景（计划层对照，未执行）：%s"
                         % "、".join(shadow["browser_planned_scenarios"]))
        for item in shadow.get("candidate_omissions") or []:
            lines.append("  候选遗漏（%s）：%s %s —— %s"
                         % (item.get("attribution"), item.get("group"), item.get("target"),
                            item.get("note")))
        for item in shadow.get("rule_candidates") or []:
            lines.append("  修正规则候选：%s" % item.get("suggestion"))
        for note in shadow.get("notes") or []:
            lines.append("  - %s" % note)
    reuse = record.get("reuse")
    if reuse:
        decisions = reuse.get("decisions") or []
        reused = [item for item in decisions if item.get("decision") == "reuse"]
        lines.append("证据复用（策略版本 %s，保鲜 %s 小时）：%s；复用 %d 组，实际执行 %d 组" % (
            reuse.get("policy_version"), reuse.get("freshness_hours"),
            "启用" if reuse.get("enabled") else "未启用（%s）" % (reuse.get("reason") or "—"),
            len(reused), len(decisions) - len(reused)))
        for item in decisions:
            if item.get("decision") == "reuse":
                lines.append("  引用已有验证：%s ← run %s（来源阶段 %s，结束于 %s）—— %s" % (
                    item.get("group"), item.get("source_run_id"), item.get("source_phase"),
                    item.get("source_ended_at"), item.get("reason")))
                lines.append("    来源输入：%s；已验证范围：%s" % (
                    item.get("source_fingerprint"), json.dumps(item.get("source_scope"), ensure_ascii=False)))
                lines.append("    来源环境：%s" % json.dumps(item.get("source_environment"), ensure_ascii=False))
            else:
                lines.append("  实际执行：%s —— %s" % (item.get("group"), item.get("reason")))
    if slow:
        lines.append("慢用例前 %d：" % len(slow))
        for index, case in enumerate(slow, start=1):
            lines.append("  %d. %s  %.3f 秒" % (index, case.get("id"), case.get("seconds") or 0))
    evidence = record.get("evidence") or {}
    lines.append("证据：%s · 记录 %s · 日志 %d/%d · 明细 %d/%d" % (
        evidence.get("integrity"), evidence.get("record_path"),
        sum(1 for log in evidence.get("logs") or [] if not log.get("missing")),
        len(evidence.get("logs") or []),
        sum(1 for item in evidence.get("cases") or [] if not item.get("missing")),
        len(evidence.get("cases") or [])))
    for note in list(result.get("notes") or []) + problems:
        lines.append("  - %s" % note)
    lines.append("记录目录：%s/runs/%s/" % (
        _show(Path(root) if root is not None else REPO / ARTIFACT_DIRNAME), record.get("run_id")))
    return "\n".join(lines)


# ── 命令行 ────────────────────────────────────────────────────────────────

def _add_help(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("-h", "--help", action="help", default=argparse.SUPPRESS, help="显示帮助")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="verify.py", add_help=False,
        description="统一验证入口：plan 解释范围，run 执行并记录，report 只读已有结果。")
    _add_help(parser)
    sub = parser.add_subparsers(dest="command", metavar="子命令")

    plan = sub.add_parser("plan", add_help=False, help="解释将执行哪些组与命令，不启动测试")
    _add_help(plan)
    plan.add_argument("--group", action="append", default=None, metavar="NAME",
                      help="验证组，可重复；缺省 unit")
    plan.add_argument("--phase", default=None, metavar="PHASE",
                      help="给出阶段后输出选测建议与回退原因：%s" % "、".join(PHASES))
    plan.add_argument("--baseline", default=None, metavar="SHA", help="选测用的基线提交")
    plan.add_argument("--json", action="store_true", dest="as_json", help="输出结构化计划")
    plan.add_argument("selectors", nargs="*", help=argparse.SUPPRESS)
    plan.set_defaults(handler=cmd_plan)

    run = sub.add_parser("run", add_help=False, help="执行验证组并留下结构化记录")
    _add_help(run)
    run.add_argument("--group", action="append", default=None, metavar="NAME",
                     help="验证组，可重复；缺省 unit（有选择器时缺省 unit-related）")
    run.add_argument("--issue", type=int, default=None, metavar="N", help="关联的 issue 编号")
    run.add_argument("--pr", type=int, default=None, metavar="N", help="关联的 PR 编号")
    run.add_argument("--phase", default=DEFAULT_PHASE, metavar="PHASE",
                     help="开发阶段：%s" % "、".join(PHASES))
    run.add_argument("--reason", default=None, metavar="TEXT", help="为什么跑这次验证")
    run.add_argument("--timeout", type=float, default=None, metavar="SECONDS",
                     help="单组超时；超时按 124 退出并回收子进程")
    run.add_argument("--baseline", default=None, metavar="SHA", help="显式指定基线提交")
    run.add_argument("--out-dir", default=None, metavar="DIR", help="验证产物目录，缺省 .verify/")
    run.add_argument("--quiet", action="store_true", help="不在终端回显子进程输出（仍写日志）")
    run.add_argument("--no-reuse", action="store_true",
                     help="绕过证据复用，全部组实际执行（独立复测用；绕过原因写进记录）")
    run.add_argument("--json", action="store_true", dest="as_json",
                     help="stdout 输出记录 JSON，子进程输出改走 stderr")
    run.add_argument("selectors", nargs="*", metavar="选择器", help="unit-related 的用例 / 模块")
    run.set_defaults(handler=cmd_run)

    report = sub.add_parser("report", add_help=False, help="只读已有记录，不启动产品测试")
    _add_help(report)
    report.add_argument("--run", default=None, metavar="ID", help="run ID；缺省读 latest.json")
    report.add_argument("--latest", action="store_true", help="读最近一次运行")
    report.add_argument("--out-dir", default=None, metavar="DIR", help="验证产物目录，缺省 .verify/")
    report.add_argument("--json", action="store_true", dest="as_json", help="输出记录与当前证据校验结果 JSON")
    report.add_argument("--efficiency", action="store_true", help="只读已有历史的效率摘要，不运行测试")
    report.add_argument("--rebuild", action="store_true", help="重建效率归档与增量回顾，不运行测试")
    report.add_argument("--source", action="append", default=[], metavar="DIR", help="额外记录根（已下载的 CI 构件），可重复")
    report.add_argument("--events", default=None, metavar="FILE", help="合并 PR 事件与远端数据缺口清单")
    report.add_argument("--now", default=None, metavar="ISO", help="报告参考时间（带时区；支持重放合成历史）")
    report.set_defaults(handler=cmd_report)
    return parser


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] == "_unit-runner":
        return cmd_unit_runner(argv[1:])
    parser = build_parser()
    args = parser.parse_args(argv)
    if getattr(args, "command", None) is None:
        parser.print_usage(sys.stderr)
        print("缺子命令：plan 解释范围，run 执行并记录，report 只读已有结果", file=sys.stderr)
        return EXIT_WRAPPER
    try:
        return args.handler(args)
    except UsageError as exc:
        print("入口自身失败：%s" % exc, file=sys.stderr)
        return EXIT_WRAPPER
    except Interrupted as exc:  # 兜底：漏出的中断也不能带裸 traceback、不能退出 1
        print("入口被信号 %s 打断" % (signal_name(-exc.signum) or exc.signum), file=sys.stderr)
        return EXIT_INTERRUPTED_BASE + exc.signum
    except KeyboardInterrupt:
        print("入口自身失败：被 Ctrl-C 打断", file=sys.stderr)
        return EXIT_INTERRUPTED_BASE + int(signal.SIGINT)


if __name__ == "__main__":
    sys.exit(main())
