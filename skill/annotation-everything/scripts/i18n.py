"""中英 locale 适配层：共享 JSON 词典的定位、读取、查找、插值与受限复数。

只用 Python 标准库。浏览器一侧把同一套语义内嵌在 viewer_template.html 的
tMsg 里，不另开资源路由。tests/test_i18n.py 与 tests/test_review_page_i18n.py
对照同一批消息核对。

能力边界：语义 key、显式 locale 选择、固定规则兜底、受控命名占位符、
中英非负整数计数复数。不做 ICU / MessageFormat、消息嵌套、序数或任意表达式。
"""
from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Callable, Mapping, Sequence

SUPPORTED = ("zh-CN", "en")
AUTO = "auto"
# 未配置语言时的默认：旧审阅与旧调用保持中文
DEFAULT_LOCALE = "zh-CN"
# auto 匹配不到任何受支持语言时回退英文
AUTO_FALLBACK = "en"

_BRANCH_SUFFIXES = ("one", "other")

_LOCALES_DIR = Path(__file__).resolve().parent.parent / "locales"

# 简中只认 zh、zh-Hans、zh-CN 及其组合。繁体（Hant / TW / HK / MO）和其他语言返回 None。
# 英文任意地区或变体（如 en-GB-oxendict）共用 en 资源，不因此承诺第二种英文词典。
_ZH_SIMPLIFIED = frozenset({"hans", "cn"})
_ZH_TRADITIONAL = frozenset({"hant", "tw", "hk", "mo"})


class I18nError(Exception):
    """适配层自身错误的基类。"""


class LocaleError(I18nError):
    """调用方给了无法解析的语言选择。"""

    def __init__(self, code: str, message: str, **params: object):
        super().__init__(message)
        self.code = code
        self.params = params


class CatalogError(I18nError):
    """词典资源缺失、读不出来或自带的 locale 标记不符。"""


class MessageFormatError(I18nError):
    """消息里的占位符与传入参数对不上。"""


class MissingMessageError(I18nError):
    """语义 key 在所用词典里没有，兜底词典里也没有。"""

    def __init__(self, key: str):
        super().__init__(f"词典里没有消息 {key}")
        self.key = key


def normalize_locale(tag: object) -> str | None:
    """把语言标签归一到 SUPPORTED 中的具体 locale；不认识就返回 None。"""
    if not isinstance(tag, str):
        return None
    key = tag.strip().lower().replace("_", "-")
    # 环境变量常带编码后缀，如 zh_CN.UTF-8；浏览器标签没有这一段。
    key = key.split(".", 1)[0]
    parts = [part for part in key.split("-") if part]
    if not parts:
        return None
    language = parts[0]
    if language == "en":
        return "en"
    if language != "zh":
        return None
    rest = set(parts[1:])
    if rest & _ZH_TRADITIONAL or not rest <= _ZH_SIMPLIFIED:
        return None
    return "zh-CN"


def canonical_choice(value: object) -> str | None:
    """把要存进 review.json 的语言选择收成 auto、zh-CN 或 en。

    None 表示没配置（旧审阅）。空字符串和其他值都是非法配置，不静默当成中文。
    """
    if value is None:
        return None
    if not isinstance(value, str):
        raise LocaleError("unsupported-locale", f"无法识别的语言选择：{value!r}", locale=value)
    text = value.strip()
    if not text:
        raise LocaleError("unsupported-locale", "语言选择是空的", locale=text)
    if text.lower() == AUTO:
        return AUTO
    tag = normalize_locale(text)
    if tag is None:
        raise LocaleError("unsupported-locale",
                          f"不支持的语言：{text}（可选：{'、'.join(SUPPORTED)} 或 {AUTO}）",
                          locale=text)
    return tag


def effective_choice(explicit: object, environ_value: object, stored: object) -> object:
    """显式参数优先于环境变量，环境变量优先于审阅里保存的选择。

    空白字符串视为没设置，继续看下一级。三级都没有时返回 None（旧调用，保持中文）。
    """
    for value in (explicit, environ_value, stored):
        if isinstance(value, str):
            if value.strip():
                return value.strip()
            continue
        if value is not None:
            return value
    return None


def resolve_locale(choice: object, *, system_languages: Sequence[str] = ()) -> str:
    """按契约把语言选择解析成具体 locale。

    choice 为 None 表示没有配置（旧审阅、旧调用）→ 中文；
    具体 locale 或别名 → 该 locale；
    auto → 按 system_languages 顺序匹配，匹配不到回退英文；
    其他值 → LocaleError。
    """
    if choice is None:
        return DEFAULT_LOCALE
    if not isinstance(choice, str):
        raise LocaleError("unsupported-locale", f"无法识别的语言选择：{choice!r}", locale=choice)
    text = choice.strip()
    hit = normalize_locale(text)
    if hit:
        return hit
    if text.lower() == AUTO:
        for raw in system_languages:
            matched = normalize_locale(raw)
            if matched:
                return matched
        return AUTO_FALLBACK
    raise LocaleError("unsupported-locale",
                      f"不支持的语言：{text}（可选：{'、'.join(SUPPORTED)} 或 {AUTO}）",
                      locale=text)


_PLACEHOLDER = re.compile(r"\{\{|\}\}|\{([A-Za-z_][A-Za-z0-9_]*)\}")


def placeholders(text: str) -> set[str]:
    """消息里出现的命名占位符；`{{` 与 `}}` 是字面花括号，不算占位符。"""
    return {m.group(1) for m in _PLACEHOLDER.finditer(text) if m.group(1)}


def interpolate(text: str, params: Mapping[str, object]) -> str:
    """替换命名占位符。完整句子整体翻译，变量位置由译文自己决定。"""

    def sub(match: re.Match) -> str:
        token = match.group(0)
        if token == "{{":
            return "{"
        if token == "}}":
            return "}"
        name = match.group(1)
        if name not in params:
            raise MessageFormatError(f"消息 {text!r} 缺少参数 {name}")
        value = params[name]
        return value if isinstance(value, str) else str(value)

    return _PLACEHOLDER.sub(sub, text)


def plural_category(locale: str, count: int) -> str:
    """中英整数计数复数：英文分 one / other，中文不分。"""
    if locale == "en":
        return "one" if count == 1 else "other"
    return "other"


def _is_branch(key: str, messages: Mapping[str, str]) -> bool:
    head, _, suffix = key.rpartition(".")
    return suffix in _BRANCH_SUFFIXES and head in messages


def select_key(catalog: Mapping, key: str, count: int | None) -> str:
    """按计数挑复数分支；该语言没有分支时用基础消息。"""
    if count is None:
        return key
    messages = catalog.get("messages") or {}
    branch = f"{key}.{plural_category(catalog.get('locale', DEFAULT_LOCALE), count)}"
    return branch if branch in messages else key


def locales_dir(root: Path | None = None) -> Path:
    return (root / "locales") if root is not None else _LOCALES_DIR


_CACHE: dict[Path, dict[str, dict]] = {}


def load_catalog(locale: str, root: Path | None = None) -> dict:
    """读一种语言的词典。资源缺失或损坏时抛 CatalogError，不静默给空词典。"""
    tag = normalize_locale(locale)
    if not tag:
        raise CatalogError(f"没有 {locale} 的词典资源（可选：{'、'.join(SUPPORTED)}）")
    path = locales_dir(root) / f"{tag}.json"
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise CatalogError(f"读不到词典 {path}：{exc}") from exc
    try:
        data = json.loads(raw)
    except ValueError as exc:
        raise CatalogError(f"词典 {path} 不是合法 JSON：{exc}") from exc
    if not isinstance(data, dict) or not isinstance(data.get("messages"), dict):
        raise CatalogError(f"词典 {path} 缺少 messages 对象")
    if data.get("locale") != tag:
        raise CatalogError(f"词典 {path} 的 locale 标记是 {data.get('locale')!r}，应为 {tag!r}")
    return data


def load_all(root: Path | None = None) -> dict[str, dict]:
    base = locales_dir(root)
    cached = _CACHE.get(base)
    if cached is None:
        cached = {tag: load_catalog(tag, root=root) for tag in SUPPORTED}
        _CACHE[base] = cached
    return cached


def translate(catalog: Mapping, key: str, *, catalogs: Mapping[str, Mapping] | None = None,
              on_missing: Callable[[str], None] | None = None,
              count: int | None = None, **params: object) -> str:
    """在指定词典里查一条消息并插值。

    count 只用于挑选复数分支，同时作为名为 count 的占位符。当前词典没有这条
    消息时，按 catalogs 里其余语言的顺序兜底，并调用 on_missing(key)。
    全部都没有则抛 MissingMessageError。
    """
    if count is not None and (isinstance(count, bool) or not isinstance(count, int)):
        raise MessageFormatError(f"count 必须是整数，收到 {count!r}")
    if count is not None:
        params = {**params, "count": count}
    messages = catalog.get("messages") or {}
    real_key = select_key(catalog, key, count)
    if real_key in messages:
        return interpolate(messages[real_key], params)
    locale = catalog.get("locale")
    for tag, other in (catalogs or {}).items():
        if tag == locale:
            continue
        other_messages = other.get("messages") or {}
        chosen = select_key(other, key, count)
        if chosen not in other_messages:
            continue
        if on_missing is not None:
            on_missing(key)
        return interpolate(other_messages[chosen], params)
    raise MissingMessageError(key)


def check_catalogs(root: Path | None = None) -> list[str]:
    """快速资源检查：跨语言 key 一致、占位符一致、复数分支完整、locale 标记正确。"""
    problems: list[str] = []
    catalogs: dict[str, dict] = {}
    for tag in SUPPORTED:
        try:
            catalogs[tag] = load_catalog(tag, root=root)
        except CatalogError as exc:
            problems.append(str(exc))
    if len(catalogs) != len(SUPPORTED):
        return problems

    def bases(tag: str) -> dict[str, set[str]]:
        messages = catalogs[tag]["messages"]
        return {k: placeholders(v) for k, v in messages.items()
                if not _is_branch(k, messages)}

    reference = bases(SUPPORTED[0])
    for tag in SUPPORTED[1:]:
        current = bases(tag)
        for key in sorted(set(reference) - set(current)):
            problems.append(f"{tag} 缺少消息 {key}（{SUPPORTED[0]} 里有）")
        for key in sorted(set(current) - set(reference)):
            problems.append(f"{tag} 多出消息 {key}（{SUPPORTED[0]} 里没有）")
        for key in sorted(set(reference) & set(current)):
            if reference[key] != current[key]:
                problems.append(
                    f"{key} 的占位符不一致：{SUPPORTED[0]} 是 {sorted(reference[key])}，"
                    f"{tag} 是 {sorted(current[key])}")

    for tag in SUPPORTED:
        messages = catalogs[tag]["messages"]
        for key in sorted(k for k in messages if _is_branch(k, messages)):
            head, _, suffix = key.rpartition(".")
            missing = [s for s in _BRANCH_SUFFIXES if f"{head}.{s}" not in messages]
            if missing:
                problems.append(
                    f"{tag} 的复数分支不完整：{head} 缺 "
                    + ", ".join(f"{head}.{s}" for s in missing))
            for branch in _BRANCH_SUFFIXES:
                full = f"{head}.{branch}"
                if full in messages:
                    want = placeholders(messages[head])
                    got = placeholders(messages[full])
                    if want != got:
                        problems.append(
                            f"{tag} 的 {full} 占位符与 {head} 不一致："
                            f"{sorted(want)} 对 {sorted(got)}")
    return problems


def system_languages(env: Mapping[str, str] | None = None) -> list[str]:
    """auto 模式读取的系统语言偏好：LC_ALL → LC_MESSAGES → LANG。

    只有 auto 才读这里；没有配置的调用保持中文，不跟随终端。
    """
    env = os.environ if env is None else env
    found: list[str] = []
    for name in ("LC_ALL", "LC_MESSAGES", "LANG"):
        value = (env.get(name) or "").strip()
        if value and value not in ("C", "POSIX"):
            found.append(value)
    return found
