"""从限定目录的技能 frontmatter 读取元数据，不执行技能正文。

扫描警告的人读文案按界面语言变（`--locale` 或 `AE_LOCALE`，默认中文）；来源路径、
技能名与系统错误类型保持原文。调用方（ae.py / build_viewer）把已解析的语言用
set_locale() 传下来；没有传时按 AE_LOCALE 兜底。调用方只负责打印警告。
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))
import i18n  # noqa: E402

MAX_SKILL_BYTES = 2 * 1024 * 1024
MAX_FRONTMATTER = 32768

# 当前界面语言：按共同契约从 AE_LOCALE 解析；未设置时保持中文（旧行为不变）
_LOCALE = i18n.DEFAULT_LOCALE
# 调用方（ae.py）已经把 --locale、AE_LOCALE、审阅 locale 解析完时定好语言，
# 之后不再被 AE_LOCALE 覆盖——否则同一进程里两种来源不一致，会出现中英混排
_PINNED = False


def T(key: str, **params: object) -> str:
    """按当前语言取一条消息；缺译时回退另一种语言（见 locales/README.md 的契约）。"""
    return i18n.Translator(_LOCALE)(key, **params)


def set_locale(locale: object = None) -> None:
    """定界面语言。locale 为 None 时按 AE_LOCALE 重新解析；给了具体 locale 就定住。"""
    global _LOCALE, _PINNED
    if locale is None:
        _PINNED = False
        try:
            _LOCALE = i18n.cli_locale(None)
        except i18n.LocaleError:
            _LOCALE = i18n.DEFAULT_LOCALE
    else:
        _LOCALE, _PINNED = str(locale), True


def _refresh_locale() -> None:
    """没有外部定过语言时，才按 AE_LOCALE 刷新；非法选择保持中文，不让扫描失败。"""
    if not _PINNED:
        set_locale(None)


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
            return None, T("skill.err.tooBig")
        with path.open(encoding="utf-8-sig") as f:
            lines = f.read(MAX_FRONTMATTER).splitlines()
    except (OSError, UnicodeError) as e:
        return None, T("skill.err.unreadable", type=type(e).__name__)
    if not lines or lines[0].strip() != "---":
        return None, T("skill.err.noFrontmatter")
    data: dict[str, str] = {}
    index = 1
    while index < len(lines):
        line = lines[index]
        if line == "---":
            name, desc = data.get("name", "").strip(), data.get("description", "").strip()
            if name and desc:
                if len(name) > 64:
                    return None, T("skill.err.nameTooLong")
                return {"name": name, "desc": desc}, None
            return None, T("skill.err.noNameOrDesc")
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
    return None, T("skill.err.frontmatterTruncated")


def _version_key(path: Path):
    parts = tuple((1, int(s)) if s.isdigit() else (0, s.lower()) for s in re.split(r"(\d+)", path.name))
    return path.parent.parent.name, path.parent.name, parts


def _skill_files(root: Path) -> list[Path]:
    _refresh_locale()   # 也可能被 scan() 之外直接调用，语言要先定好
    if root.is_file():
        if root.name != "SKILL.md":
            raise ValueError(T("skill.err.notSkillFile"))
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
    _refresh_locale()
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
                    if item and root.name == "cache" and root.parent.name == "plugins":
                        parts = path.relative_to(root).parts
                        if len(parts) >= 6 and parts[3] == "skills" and ":" not in item["name"]:
                            qualified = f"{parts[1]}:{item['name']}"
                            if len(qualified) > 64:
                                item, error = None, T("skill.err.qualifiedTooLong")
                            else:
                                item["name"] = qualified
                except (OSError, RuntimeError) as e:
                    item, error = None, T("skill.err.unreadable", type=type(e).__name__)
                if error:
                    complete = False
                    warnings.append(T("skill.warn.file", path=path, detail=error))
                elif item:
                    found.setdefault(item["name"], item)
            results.append((key, list(found.values()), complete))
        except (OSError, RuntimeError, ValueError) as e:
            key = hashlib.sha256(str(root.absolute()).encode("utf-8")).hexdigest()
            results.append((key, None, False))
            warnings.append(T("skill.warn.root", root=root, type=type(e).__name__))
    return results, warnings
