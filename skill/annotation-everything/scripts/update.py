#!/usr/bin/env python3
"""检查来源仓库的正式标签并更新当前技能副本，不改来源检出或审阅数据。"""
from __future__ import annotations

import argparse
import datetime
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import time

import i18n
import ledger

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


def explicit_locale(argv):
    for index, token in enumerate(argv):
        if token == "--":
            break
        if token == "--locale":
            return argv[index + 1] if index + 1 < len(argv) else ""
        if token.startswith("--locale="):
            return token.partition("=")[2]
    return None


def version(value):
    match = VERSION.fullmatch(value) if isinstance(value, str) else None
    return tuple(int(part) for part in match.groups()) if match else None


def digests(root: Path) -> dict:
    result = {}
    for path in sorted(root.rglob("*")):
        rel = path.relative_to(root)
        if ("__pycache__" in rel.parts[:-1] or rel.name in {RECORD, ".DS_Store"}
                or rel.suffix in {".pyc", ".pyo"} or not path.is_file()):
            continue
        result[rel.as_posix()] = hashlib.sha256(path.read_bytes()).hexdigest()
    return result


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
        status = Path(entry["dir"]) / ".annotation-server.json"
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
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            continue
        except PermissionError:
            pass
        return True
    return False


def clone_release(repo: Path, cache: Path, tag: str, commit: str) -> Path:
    release = cache / ("release-" + commit)
    if release.exists():
        if git(release, "rev-parse", "HEAD") != commit or git(release, "status", "--porcelain"):
            raise UpdateError("cli.update.badRelease")
        return release
    with tempfile.TemporaryDirectory(prefix=".fetch-", dir=cache) as temp:
        checkout = Path(temp) / "repo"
        git(repo, "clone", "--quiet", "--no-local", "--depth", "1", "--branch", tag,
            "--", remote_url(repo), str(checkout), timeout=60)
        if git(checkout, "rev-parse", "HEAD") != commit:
            raise UpdateError("cli.update.badRelease")
        os.replace(checkout, release)
    return release


def install_release(copy: Path, record: dict, release: Path, tag: str, commit: str) -> None:
    source = release / "skill" / SKILL
    try:
        serve = (source / "scripts" / "serve.py").read_text(encoding="utf-8")
    except OSError:
        raise UpdateError("cli.update.badRelease") from None
    match = re.search(r'^VERSION = "([^"]+)"', serve, re.M)
    required = ("SKILL.md", "scripts/update.py", "scripts/i18n.py", "scripts/ledger.py",
                "locales/zh-CN.json", "locales/en.json")
    if any(not (source / item).is_file() for item in required) or not match or match.group(1) != tag[1:]:
        raise UpdateError("cli.update.badRelease")
    expected = record.get("files")
    if not isinstance(expected, dict):
        previous = Path(record["repo"]) / "skill" / SKILL
        if not previous.is_dir():
            raise UpdateError("cli.update.modified", latest=tag[1:])
        expected = digests(previous)
    if digests(copy) != expected:
        raise UpdateError("cli.update.modified", latest=tag[1:])

    temp = Path(tempfile.mkdtemp(prefix=".ae-update-", dir=copy.parent))
    preserve_backup = False
    try:
        stage = temp / SKILL
        shutil.copytree(source, stage, ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "*.pyo", ".DS_Store"))
        updated = {**record, "repo": str(release), "commit": commit, "version": tag[1:],
                   "installed_at": datetime.datetime.now().astimezone().isoformat(timespec="seconds"),
                   "files": digests(stage)}
        (stage / RECORD).write_text(json.dumps(updated, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        if digests(copy) != expected:
            raise UpdateError("cli.update.modified", latest=tag[1:])
        backup = temp / "previous"
        os.replace(copy, backup)
        try:
            os.replace(stage, copy)
        except BaseException:
            try:
                os.replace(backup, copy)
            except OSError:
                preserve_backup = True
                raise UpdateError("cli.update.recover", path=backup) from None
            raise
    finally:
        if not preserve_backup:
            shutil.rmtree(temp, ignore_errors=True)


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    choice = explicit_locale(argv)
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
        cache_root = Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share").expanduser()
        key = hashlib.sha256(os.fsencode(str(copy))).hexdigest()[:24]
        cache = cache_root / SKILL / "updates" / key
        cache.mkdir(mode=0o700, parents=True, exist_ok=True)
        lock_fd = os.open(cache / "lock", os.O_CREAT | os.O_RDWR, 0o600)
        with os.fdopen(lock_fd, "r+") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                if args.mode != "auto":
                    print(translate("cli.update.busy"))
                return 0
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
            if has_running_review():
                raise UpdateError("cli.update.running", latest=tag[1:])
            release = clone_release(repo, cache, tag, commit)
            if has_running_review():
                raise UpdateError("cli.update.running", latest=tag[1:])
            install_release(copy, record, release, tag, commit)
            print(translate("cli.update.installed", installed=record["version"], latest=tag[1:]))
            return 0
    except UpdateError as exc:
        if args.mode != "auto" or exc.key != "cli.update.noRecord" or (copy / RECORD).exists():
            print(translate(exc.key, **exc.params), file=sys.stderr)
        return 0 if args.mode == "auto" else 1
    except (OSError, ValueError):
        print(translate("cli.update.failed"), file=sys.stderr)
        return 0 if args.mode == "auto" else 1


if __name__ == "__main__":
    sys.exit(main())
