"""从限定目录的技能 frontmatter 读取元数据，不执行技能正文。"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re

MAX_SKILL_BYTES = 2 * 1024 * 1024
MAX_FRONTMATTER = 32768


def _value(raw: str) -> str:
    raw = raw.strip()
    if raw.startswith('"') and raw.endswith('"'):
        try:
            return str(json.loads(raw))
        except ValueError:
            return raw[1:-1]
    if raw.startswith("'") and raw.endswith("'"):
        return raw[1:-1].replace("''", "'")
    return raw


def _metadata(path: Path) -> tuple[dict[str, str] | None, str | None]:
    try:
        if path.stat().st_size > MAX_SKILL_BYTES:
            return None, "文件过大"
        with path.open(encoding="utf-8-sig") as f:
            lines = f.read(MAX_FRONTMATTER).splitlines()
    except (OSError, UnicodeError) as e:
        return None, f"无法读取：{type(e).__name__}"
    if not lines or lines[0].strip() != "---":
        return None, "缺少 frontmatter"
    data: dict[str, str] = {}
    index = 1
    while index < len(lines):
        line = lines[index]
        if line == "---":
            name, desc = data.get("name", "").strip(), data.get("description", "").strip()
            if name and desc:
                return {"name": name[:64], "desc": desc}, None
            return None, "缺少名称或说明"
        key, sep, value = line.partition(":")
        if sep and key in ("name", "description"):
            if value.strip() in (">", ">-", ">+", "|", "|-", "|+"):
                parts = []
                index += 1
                while index < len(lines) and (not lines[index].strip() or lines[index][:1].isspace()):
                    parts.append(lines[index].strip())
                    index += 1
                data[key] = " ".join(x for x in parts if x)
                continue
            data[key] = _value(value)
        index += 1
    return None, "frontmatter 不完整或过大"


def _version_key(path: Path):
    parts = tuple((1, int(s)) if s.isdigit() else (0, s.lower()) for s in re.split(r"(\d+)", path.name))
    return path.parent.parent.name, path.parent.name, parts


def _skill_files(root: Path) -> list[Path]:
    if root.is_file():
        if root.name != "SKILL.md":
            raise ValueError("来源文件须为 SKILL.md")
        return [root]
    if (root / "SKILL.md").is_file():
        return [root / "SKILL.md"]
    if root.name == "cache" and root.parent.name == "plugins":
        # 缓存按 marketplace / plugin / version / skills 组织；每个插件只看最新目录。
        newest: dict[tuple[str, str], Path] = {}
        for version in sorted(root.glob("*/*/*"), key=_version_key):
            if version.is_dir() and (version / "skills").is_dir():
                newest[(version.parent.parent.name, version.parent.name)] = version
        return [file for version in newest.values() for file in sorted((version / "skills").glob("*/SKILL.md"))]
    return [child / "SKILL.md" for child in sorted(root.iterdir())
            if child.is_dir() and (child / "SKILL.md").is_file()]


def default_roots() -> list[Path]:
    home = Path.home()
    codex = Path(os.environ.get("CODEX_HOME") or home / ".codex")
    claude = Path(os.environ.get("CLAUDE_SKILLS_DIR") or home / ".claude" / "skills")
    return [home / ".agents" / "skills", claude, home / ".zcode" / "skills", codex / "skills",
            codex / "plugins" / "cache", home / ".claude" / "plugins" / "cache"]


def scan(roots: list[Path]) -> tuple[list[tuple[str, list[dict[str, str]] | None, bool]], list[str]]:
    """分来源扫描；不完整来源保留旧结果，来源路径不写进状态文件。"""
    results: list[tuple[str, list[dict[str, str]] | None, bool]] = []
    warnings: list[str] = []
    for root in roots:
        root = root.expanduser()
        try:
            key = hashlib.sha256(str(root.absolute()).encode("utf-8")).hexdigest()
            if not root.exists():
                results.append((key, None, False))
                continue
            files = _skill_files(root)
            found: dict[str, dict[str, str]] = {}
            seen: set[Path] = set()
            complete = True
            for path in files:
                try:
                    resolved = path.resolve()
                    if resolved in seen:
                        continue
                    seen.add(resolved)
                    item, error = _metadata(path)
                except (OSError, RuntimeError) as e:
                    item, error = None, f"无法读取：{type(e).__name__}"
                if error:
                    complete = False
                    warnings.append(f"技能文件 {path}：{error}；保留该来源的既有记录")
                elif item:
                    found.setdefault(item["name"], item)
            results.append((key, list(found.values()), complete))
        except (OSError, RuntimeError, ValueError) as e:
            key = hashlib.sha256(str(root.absolute()).encode("utf-8")).hexdigest()
            results.append((key, None, False))
            warnings.append(f"技能来源 {root} 不可访问：{type(e).__name__}；保留既有记录")
    return results, warnings
