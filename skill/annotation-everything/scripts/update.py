#!/usr/bin/env python3
"""检查来源仓库的正式标签并更新当前技能副本，不改来源检出或审阅数据。"""
from __future__ import annotations

import argparse
import contextlib
import ctypes
import datetime
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import uuid

import i18n
import ledger
import content_manifest
import fsutil

SKILL = "annotation-everything"
RECORD = ".install-record.json"
INTERVAL = 24 * 60 * 60
VERSION = re.compile(r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)$")


class UpdateError(Exception):
    """面向用户的稳定原因；不输出可能包含凭据的 Git 错误。"""

    def __init__(self, key: str, **params):
        self.key = key
        self.params = params


class LocalizedParser(argparse.ArgumentParser):
    def __init__(self, translate, **kwargs):
        self.translate = translate
        super().__init__(**kwargs)

    def error(self, message):
        self.print_usage(sys.stderr)
        choice = re.match(r"^argument \S+: invalid choice: '([^']*)'", message)
        missing = re.match(r"^argument (\S+): expected one argument", message)
        unknown = re.match(r"^unrecognized arguments: (.+)", message)
        if choice:
            detail = self.translate("cli.update.invalidMode", name=choice.group(1))
        elif missing:
            detail = self.translate("cli.ae.err.missingValue", name=missing.group(1))
        elif unknown:
            detail = self.translate("cli.ae.err.unknownArgument", detail=unknown.group(1))
        elif message.startswith("the following arguments are required:"):
            detail = self.translate("cli.update.missingMode")
        else:
            detail = self.translate("cli.ae.err.generic", detail=message)
        print(detail, file=sys.stderr)
        self.exit(2)


def update_storage(copy: Path) -> Path:
    key = hashlib.sha256(os.fsencode(str(copy))).hexdigest()[:24]
    data = Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share").expanduser()
    preferred = (data / SKILL / "updates" / key).resolve()
    if preferred == copy or copy in preferred.parents or preferred in copy.parents:
        # --target 可以指向任意目录，包括 XDG_DATA_HOME。
        return copy.parent / (".annotation-everything-updates-" + key)
    return preferred


def version(value):
    match = VERSION.fullmatch(value) if isinstance(value, str) else None
    return tuple(int(part) for part in match.groups()) if match else None


def git(repo: Path, *args: str, timeout: int = 15) -> str:
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}
    try:
        result = subprocess.run(["git", "-C", str(repo), *args], stdin=subprocess.DEVNULL,
                                capture_output=True, text=True, timeout=timeout, env=env)
    except (OSError, subprocess.TimeoutExpired):
        raise UpdateError("cli.update.network") from None
    if result.returncode:
        raise UpdateError("cli.update.network")
    return result.stdout.strip()


def read_record(copy: Path) -> dict:
    try:
        record = json.loads((copy / RECORD).read_text(encoding="utf-8"))
        if not isinstance(record, dict) or not isinstance(record.get("repo"), str) or not record["repo"]:
            raise ValueError
        if version(record.get("version")) is None:
            raise ValueError
        return record
    except (OSError, ValueError, TypeError):
        raise UpdateError("cli.update.noRecord") from None


def latest_release(repo: Path):
    refs = {}
    for line in git(repo, "ls-remote", "--tags", "origin").splitlines():
        fields = line.split("\t")
        if len(fields) != 2 or not fields[1].startswith("refs/tags/v"):
            continue
        ref = fields[1][len("refs/tags/"):]
        peeled = ref.endswith("^{}")
        tag = ref[:-3] if peeled else ref
        parsed = version(tag[1:])
        if parsed is not None and re.fullmatch(r"[0-9a-f]{40,64}", fields[0]):
            entry = refs.setdefault(tag, {})
            entry["commit" if peeled else "object"] = fields[0]
    if not refs:
        raise UpdateError("cli.update.noRelease")
    tag = max(refs, key=lambda item: version(item[1:]))
    return tag, refs[tag].get("commit") or refs[tag]["object"]


def remote_url(repo: Path) -> str:
    url = git(repo, "remote", "get-url", "origin")
    if not url:
        raise UpdateError("cli.update.network")
    # Git resolves relative local remote paths against the source repository.
    if not os.path.isabs(url) and ":" not in url:
        url = str((repo / url).resolve())
    return url


def has_running_review() -> bool:
    """已有审阅服务仍在运行时推迟替换，避免旧进程读到新版资源。"""
    try:
        entries = ledger.entries()
    except (ledger.LedgerError, OSError):
        raise UpdateError("cli.update.cannotVerifyServices") from None
    for entry in entries.values():
        status = ledger.status_file(entry["dir"])
        try:
            info = json.loads(status.read_text(encoding="utf-8"))
        except FileNotFoundError:
            continue
        except (OSError, ValueError):
            raise UpdateError("cli.update.cannotVerifyServices") from None
        if not isinstance(info, dict) or info.get("app") != SKILL:
            raise UpdateError("cli.update.cannotVerifyServices")
        pid = info.get("pid")
        if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
            raise UpdateError("cli.update.cannotVerifyServices")
        if ledger.pid_alive(pid):
            return True
    return False


def process_cwd(pid: int):
    if sys.platform == "darwin":
        try:
            result = subprocess.run(["lsof", "-a", "-p", str(pid), "-d", "cwd", "-Fn"],
                                    capture_output=True, text=True, timeout=10)
        except (OSError, subprocess.TimeoutExpired):
            return None
        return next((line[1:] for line in result.stdout.splitlines() if line.startswith("n")), None)
    try:
        return os.readlink(f"/proc/{pid}/cwd")
    except OSError:
        return None


def has_legacy_process(copy: Path, record: dict) -> bool:
    """检查没有新版共享锁的旧服务与批注进程；命令只在本机读取，不回显。"""
    try:
        done = subprocess.run(["ps", "-ww", "-eo", "pid=,command="], stdin=subprocess.DEVNULL,
                              capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        raise UpdateError("cli.update.cannotVerifyServices") from None
    if done.returncode:
        raise UpdateError("cli.update.cannotVerifyServices")
    names = ("serve.py", "notes.py")
    target_paths = {copy / "scripts" / name for name in names}
    roots = [Path(__file__).parent.parent]
    links = record.get("links", [])
    if isinstance(links, list):
        roots.extend(Path(item["path"]) for item in links
                     if isinstance(item, dict) and isinstance(item.get("path"), str))
    aliases = {str(root / "scripts" / name) for root in [copy, *roots] for name in names}
    aliases |= {path.removeprefix("/private") for path in aliases if path.startswith("/private/var/")}
    for line in done.stdout.splitlines():
        fields = line.strip().split(None, 1)
        if len(fields) != 2 or not fields[0].isdigit() or int(fields[0]) == os.getpid():
            continue
        command = fields[1]
        try:
            words = shlex.split(command)
        except ValueError:
            if any(alias in command for alias in aliases):
                return True
            continue
        if not words or not re.fullmatch(r"python(?:\d+(?:\.\d+)*)?|pypy\d*", Path(words[0]).name.lower()):
            continue
        index = 1
        while index < len(words) and words[index].startswith("-"):
            if words[index].startswith(("-c", "-m")) or words[index] == "-":
                break
            index += 2 if words[index] in ("-W", "-X") else 1
        if index >= len(words) or words[index].startswith("-"):
            continue
        if any(re.search(r"(?:^|\s)" + re.escape(alias) + r"(?:\s|$)", command) for alias in aliases):
            return True
        script = Path(words[index])
        if script.name not in names:
            continue
        if not script.is_absolute():
            cwd = process_cwd(int(fields[0]))
            if cwd is None:
                raise UpdateError("cli.update.cannotVerifyServices")
            script = Path(cwd) / script
        if script.resolve() in target_paths:
            return True
    return False


def clone_release(repo: Path, cache: Path, tag: str, commit: str) -> Path:
    release = cache / ("release-" + commit)
    if release.exists():
        if git(release, "rev-parse", "HEAD") != commit:
            raise UpdateError("cli.update.badRelease")
        if not git(release, "status", "--porcelain", "--ignored", "--untracked-files=all"):
            return release
        # 不删除缓存里的未知或忽略文件；另取一份干净的正式版本。
        release = cache / ("release-" + commit + "-" + uuid.uuid4().hex)
    with tempfile.TemporaryDirectory(prefix=".fetch-", dir=cache) as temp:
        checkout = Path(temp) / "repo"
        git(repo, "clone", "--quiet", "--no-local", "--depth", "1", "--branch", tag,
            "--", remote_url(repo), str(checkout), timeout=60)
        if git(checkout, "rev-parse", "HEAD") != commit:
            raise UpdateError("cli.update.badRelease")
        os.replace(checkout, release)
    return release


def copy_tracked_skill(release: Path, stage: Path) -> None:
    """只复制 Git 提交里的普通文件，不带上缓存中被忽略的用户文件。"""
    prefix = f"skill/{SKILL}/"
    files = git(release, "ls-files", "-z", "--", prefix).split("\0")
    stage.mkdir()
    for name in filter(None, files):
        if not name.startswith(prefix):
            raise UpdateError("cli.update.badRelease")
        relative = Path(name[len(prefix):])
        if relative.is_absolute() or ".." in relative.parts:
            raise UpdateError("cli.update.badRelease")
        source = release / name
        if source.is_symlink() or not source.is_file():
            raise UpdateError("cli.update.badRelease")
        target = stage / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)


def exchange_directories(first: Path, second: Path) -> None:
    """使用操作系统的原子交换；不支持时失败，绝不先移走技能副本。"""
    libc = ctypes.CDLL(None, use_errno=True)
    try:
        if sys.platform == "darwin":
            exchange = libc.renamex_np
            exchange.argtypes = (ctypes.c_char_p, ctypes.c_char_p, ctypes.c_uint)
            args = (os.fsencode(first), os.fsencode(second), 2)  # RENAME_SWAP
        elif sys.platform.startswith("linux"):
            exchange = libc.renameat2
            exchange.argtypes = (ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint)
            args = (-100, os.fsencode(first), -100, os.fsencode(second), 2)  # AT_FDCWD, RENAME_EXCHANGE
        else:
            raise AttributeError
    except AttributeError:
        raise UpdateError("cli.update.noAtomicSwap") from None
    exchange.restype = ctypes.c_int
    if exchange(*args) != 0:
        raise UpdateError("cli.update.noAtomicSwap")


def install_release(copy: Path, record: dict, release: Path, tag: str, commit: str) -> None:
    source = release / "skill" / SKILL
    try:
        serve = (source / "scripts" / "serve.py").read_text(encoding="utf-8")
    except OSError:
        raise UpdateError("cli.update.badRelease") from None
    match = re.search(r'^VERSION = "([^"]+)"', serve, re.M)
    required = ("SKILL.md", "scripts/update.py", "scripts/content_manifest.py", "scripts/i18n.py", "scripts/ledger.py",
                "scripts/fsutil.py", "locales/zh-CN.json", "locales/en.json")
    if any(not (source / item).is_file() for item in required) or not match or match.group(1) != tag[1:]:
        raise UpdateError("cli.update.badRelease")
    expected = record.get("tree")
    if not isinstance(expected, dict):
        previous = Path(record["repo"]) / "skill" / SKILL
        if not previous.is_dir():
            raise UpdateError("cli.update.modified", latest=tag[1:])
        expected = content_manifest.snapshot(previous)
    if content_manifest.snapshot(copy) != expected:
        raise UpdateError("cli.update.modified", latest=tag[1:])

    temp = Path(tempfile.mkdtemp(prefix=".ae-update-", dir=copy.parent))
    exchange_started = False
    try:
        stage = temp / SKILL
        copy_tracked_skill(release, stage)
        os.chmod(stage, stat.S_IMODE(copy.stat().st_mode))
        updated = {**record, "repo": str(release), "commit": commit, "version": tag[1:],
                   "installed_at": datetime.datetime.now().astimezone().isoformat(timespec="seconds"),
                   "tree": content_manifest.snapshot(stage)}
        updated.pop("files", None)
        (stage / RECORD).write_text(json.dumps(updated, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        if content_manifest.snapshot(copy) != expected:
            raise UpdateError("cli.update.modified", latest=tag[1:])
        # 一旦开始交换就保留临时目录：它可能持有旧副本及并发写入的内容。
        exchange_started = True
        exchange_directories(copy, stage)
        try:
            old_tree = content_manifest.snapshot(stage)
        except OSError:
            raise UpdateError("cli.update.concurrentChanges", path=stage, latest=tag[1:]) from None
        if old_tree != expected:
            raise UpdateError("cli.update.concurrentChanges", path=stage, latest=tag[1:])
    finally:
        if not exchange_started:
            shutil.rmtree(temp, ignore_errors=True)


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    choice = i18n.explicit_locale(argv)
    try:
        locale = i18n.cli_locale(choice)
    except i18n.LocaleError:
        default = i18n.Translator(i18n.DEFAULT_LOCALE)
        print(default("cli.ae.err.invalidLocale", name=choice or ""), file=sys.stderr)
        return 2
    translate = i18n.Translator(locale)
    parser = LocalizedParser(translate, prog="update.py", description=translate("cli.update.description"),
                             add_help=False)
    parser.add_argument("-h", "--help", action="help", help=translate("cli.ae.opt.help"))
    parser.add_argument("mode", choices=("auto", "check", "now"), metavar="MODE",
                        help=translate("cli.update.modeHelp"))
    parser.add_argument("--locale", metavar="LANG", help=translate("cli.ae.opt.locale"))
    args = parser.parse_args(argv)
    copy = Path(__file__).resolve().parent.parent
    if args.mode == "auto" and os.environ.get("AE_AUTO_UPDATE") == "0":
        return 0
    try:
        record = read_record(copy)
        repo = Path(record["repo"])
        if not (repo / "skill" / SKILL).is_dir():
            raise UpdateError("cli.update.noRecord")
        # 这个检出也是 doctor / repair 的来源，不能放在可能被系统随时清理的缓存目录。
        cache = update_storage(copy)
        cache.mkdir(mode=0o700, parents=True, exist_ok=True)
        stack = contextlib.ExitStack()
        try:
            stack.enter_context(fsutil.flocked(cache / "lock", exclusive=True, nonblocking=True))
        except BlockingIOError:
            if args.mode != "auto":
                print(translate("cli.update.busy"))
            return 0
        with stack:
            state_file = cache / "state.json"
            try:
                state = json.loads(state_file.read_text(encoding="utf-8"))
                last_attempt = float(state.get("last_attempt", 0))
            except (OSError, ValueError, TypeError, AttributeError):
                last_attempt = 0
            now = time.time()
            if args.mode == "auto" and 0 <= now - last_attempt < INTERVAL:
                return 0
            if args.mode == "auto":
                state_file.write_text(json.dumps({"last_attempt": now}), encoding="utf-8")
            tag, commit = latest_release(repo)
            if version(tag[1:]) <= version(record["version"]):
                if args.mode != "auto":
                    print(translate("cli.update.current", version=record["version"]))
                return 0
            if args.mode == "check":
                print(translate("cli.update.available", installed=record["version"], latest=tag[1:]))
                return 0
            release = clone_release(repo, cache, tag, commit)
            with contextlib.ExitStack() as stack:
                try:
                    stack.enter_context(ledger.update_lock(exclusive=True, nonblocking=True))
                except BlockingIOError:
                    raise UpdateError("cli.update.running", latest=tag[1:]) from None
                except OSError:
                    raise UpdateError("cli.update.cannotVerifyServices") from None
                if has_running_review() or has_legacy_process(copy, record):
                    raise UpdateError("cli.update.running", latest=tag[1:])
                install_release(copy, record, release, tag, commit)
            print(translate("cli.update.installed", installed=record["version"], latest=tag[1:]))
            return 0
    except UpdateError as exc:
        if exc.key == "cli.update.concurrentChanges":
            print(translate("cli.update.installed", installed=record["version"], latest=exc.params["latest"]))
        if args.mode != "auto" or exc.key != "cli.update.noRecord" or (copy / RECORD).exists():
            print(translate(exc.key, **exc.params), file=sys.stderr)
        return 0 if args.mode == "auto" else 1
    except (OSError, ValueError):
        print(translate("cli.update.failed"), file=sys.stderr)
        return 0 if args.mode == "auto" else 1


if __name__ == "__main__":
    sys.exit(main())
