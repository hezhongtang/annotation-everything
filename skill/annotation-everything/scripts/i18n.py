"""中英 locale 适配层：共享 JSON 词典的定位、读取、查找、插值与受限复数，以及命令行
共用的语言样板（取 --locale、翻译 argparse 英文报错、报告非法语言选择）。

只用 Python 标准库。浏览器一侧把同一套语义内嵌在 viewer_template.html 的
tMsg 里，不另开资源路由。tests/test_i18n.py 与 tests/test_review_page_i18n.py
对照同一批消息核对。

能力边界：语义 key、显式 locale 选择、固定规则兜底、受控命名占位符、
中英非负整数计数复数，以及上面那份命令行样板。不做 ICU / MessageFormat、消息
嵌套、序数或任意表达式。
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from functools import partial
from pathlib import Path
from typing import Callable, Mapping, Sequence

SUPPORTED = ("zh-CN", "en")
AUTO = "auto"
# 未配置语言时的默认：旧审阅与旧调用保持中文
DEFAULT_LOCALE = "zh-CN"
# auto 匹配不到任何受支持语言时回退英文
AUTO_FALLBACK = "en"
# 命令行共用的语言环境变量名（显式参数优先于它）
ENV_VAR = "AE_LOCALE"

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


_ONE: dict[tuple[Path, str], dict] = {}


def catalog(locale: str, root: Path | None = None) -> dict:
    """带缓存的 load_catalog：同一进程里每种语言的词典只读一次文件。

    只读所要的那一种语言，失败面与 load_catalog 相同；返回的词典是共享缓存，调用方不得修改。
    """
    tag = normalize_locale(locale)
    if not tag:
        raise CatalogError(f"没有 {locale} 的词典资源（可选：{'、'.join(SUPPORTED)}）")
    base = locales_dir(root)
    full = _CACHE.get(base)
    if full is not None:
        return full[tag]
    cached = _ONE.get((base, tag))
    if cached is None:
        cached = _ONE[(base, tag)] = load_catalog(tag, root=root)
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
        # 计数消息必须分支齐全：英文分 one / other，中文不分。少了分支会静默退化成
        # "1 annotations"，所以这里当成资源错误，而不是留给运行时发现。
        for key in sorted(bases(tag)):
            if "count" not in bases(tag)[key]:
                continue
            if tag == "en":
                for suffix in _BRANCH_SUFFIXES:
                    if f"{key}.{suffix}" not in messages:
                        problems.append(f"en 的计数消息 {key} 缺少 {key}.{suffix} 分支")
            else:
                extra = [s for s in _BRANCH_SUFFIXES if f"{key}.{s}" in messages]
                if extra:
                    problems.append(
                        f"{tag} 的计数消息 {key} 不该有复数分支："
                        + ", ".join(f"{key}.{s}" for s in extra))
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


def cli_choice(explicit: object, environ: Mapping[str, str] | None = None,
               stored: object = None) -> object:
    """命令行的语言选择：显式参数 > 环境变量 AE_LOCALE > 审阅保存值。

    三级都没有时返回 None，由 resolve_locale 兜底成中文（旧调用行为不变）。
    """
    env = os.environ if environ is None else environ
    return effective_choice(explicit, env.get(ENV_VAR), stored)


def cli_locale(explicit: object, environ: Mapping[str, str] | None = None,
               stored: object = None) -> str:
    """按共同契约解析命令行的语言；非法选择抛 LocaleError（不静默改值）。"""
    return resolve_locale(cli_choice(explicit, environ, stored),
                          system_languages=system_languages(environ))


def explicit_locale(argv) -> object:
    """从命令行里取出 --locale 的值（写在子命令前后都认，`--` 之后不看）；没写返回 None。"""
    argv = list(argv or [])
    for index, token in enumerate(argv):
        if token == "--":
            break
        if token == "--locale":
            return argv[index + 1] if index + 1 < len(argv) else ""
        if token.startswith("--locale="):
            return token.partition("=")[2]
    return None


# argparse 按 CPython 的英文原文报错。措辞随 Python 版本变，匹配规则只保留这一份，
# 各入口不各自抄一份——升级后要改只改这里。
_INVALID_CHOICE = re.compile(r"^(?:argument \S+: )?invalid choice: '([^']*)' \(choose from .*\)$")
_MISSING_VALUE = re.compile(r"^argument (\S+): expected one argument$")
_UNKNOWN_ARGUMENT = re.compile(r"^unrecognized arguments: (.+)$")

# argparse 报错的类别。入口按自己原来认的那几类传消息 key，没列的类别退回通用消息
CLI_ERROR_INVALID_CHOICE = "invalid-choice"
CLI_ERROR_MISSING_VALUE = "missing-value"
CLI_ERROR_UNKNOWN_ARGUMENT = "unknown-argument"
CLI_ERROR_MISSING_ARGUMENT = "missing-argument"
CLI_ERROR_GENERIC = "generic"
# 语言选择本身的问题：空值和非法值分开说明
CLI_ERROR_EMPTY_LOCALE = "empty-locale"
CLI_ERROR_INVALID_LOCALE = "invalid-locale"


def classify_cli_error(message: str) -> tuple[str, dict]:
    """把 argparse 的英文报错归成已知类别，并给出该类消息要用的参数。

    返回 (类别, 参数)。认不出来的归到 CLI_ERROR_GENERIC，参数是去掉首尾空白后的
    原文（detail），由调用方补上下文。
    """
    text = str(message).strip()
    match = _INVALID_CHOICE.match(text)
    if match:
        return CLI_ERROR_INVALID_CHOICE, {"name": match.group(1)}
    match = _MISSING_VALUE.match(text)
    if match:
        return CLI_ERROR_MISSING_VALUE, {"name": match.group(1)}
    match = _UNKNOWN_ARGUMENT.match(text)
    if match:
        return CLI_ERROR_UNKNOWN_ARGUMENT, {"detail": match.group(1)}
    if text.startswith("the following arguments are required:"):
        return CLI_ERROR_MISSING_ARGUMENT, {}
    return CLI_ERROR_GENERIC, {"detail": text}


def translate_cli_error(message: str, keys: Mapping[str, str],
                        translate: Callable[..., str]) -> str:
    """按入口自己的消息 key 表，把 argparse 的英文报错换成本地化的说法。

    keys 把类别（CLI_ERROR_*）映射到消息 key，必须含 CLI_ERROR_GENERIC 一项。
    某个类别没列出来就退回通用消息、用原文补上下文——入口因此只认自己原来认的
    类型，输出与逐份抄写时一致。
    """
    text = str(message).strip()
    category, params = classify_cli_error(text)
    key = keys.get(category)
    if key is None:
        return translate(keys[CLI_ERROR_GENERIC], detail=text)
    return translate(key, **params)


class LocalizedParser(argparse.ArgumentParser):
    """帮助、用法与错误消息都走当前语言的 argparse 解析器；参数名与合法取值保持原文。

    keys 是入口这一侧的消息 key 表（见 translate_cli_error），translate 按当前
    语言查词典。报错时先打印用法，再打印本地化说明，退出码 2。
    """

    def __init__(self, *args, keys: Mapping[str, str],
                 translate: Callable[..., str], **kwargs):
        super().__init__(*args, **kwargs)
        self.keys = keys
        self.translate = translate

    def add_subparsers(self, **kwargs):
        # add_subparsers 默认用 type(self) 建子解析器；把自己的 key 表与译文一起
        # 传下去，子命令的报错才不会退回 argparse 的英文原文
        kwargs.setdefault("parser_class",
                          partial(type(self), keys=self.keys, translate=self.translate))
        return super().add_subparsers(**kwargs)

    def error(self, message: str):
        self.print_usage(sys.stderr)
        print(translate_cli_error(message, self.keys, self.translate), file=sys.stderr)
        sys.exit(2)


def locale_error_message(exc: Exception, keys: Mapping[str, str],
                         translate: Callable[..., str], name: object = None) -> str:
    """语言选择为空或非法时的一句话；按异常里带的值区分空值和非法值。

    keys 需要 CLI_ERROR_EMPTY_LOCALE 与 CLI_ERROR_INVALID_LOCALE 两项。此时没有
    可用语言，调用方先把界面语言退回默认中文，再打印这里返回的句子。name 缺省时
    用异常里带的值；调用方另有取值方式（例如按用户原样输入回显）时显式给出。
    """
    params = getattr(exc, "params", {}) or {}
    value = params.get("locale") if isinstance(params, dict) else None
    if isinstance(value, str) and not value.strip():
        return translate(keys[CLI_ERROR_EMPTY_LOCALE])
    if name is None:
        name = "" if value is None else str(value)
    return translate(keys[CLI_ERROR_INVALID_LOCALE], name=name)


class ModuleLocale:
    """单个模块自己的界面语言状态（每个模块各持一个实例，不跨模块共享）。

    调用方已经把 --locale、AE_LOCALE、审阅 locale 解析完时用 set(具体 locale) 定住，
    之后不再被 AE_LOCALE 覆盖——否则同一进程里两种来源不一致，会出现中英混排。
    未定住时 refresh() 按 AE_LOCALE 重新解析；未设置或选择非法时保持中文。
    """

    def __init__(self) -> None:
        self.locale = DEFAULT_LOCALE
        self.pinned = False

    def set(self, locale: object = None) -> None:
        """定界面语言。locale 为 None 时按 AE_LOCALE 重新解析；给了具体 locale 就定住。"""
        if locale is None:
            self.pinned = False
            try:
                self.locale = cli_locale(None)
            except LocaleError:
                self.locale = DEFAULT_LOCALE
        else:
            self.locale, self.pinned = str(locale), True

    def refresh(self) -> None:
        """没有外部定过语言时，才按 AE_LOCALE 刷新。"""
        if not self.pinned:
            self.set(None)

    def __call__(self, key: str, **params: object) -> str:
        """按当前语言取一条消息；缺译时回退另一种语言。"""
        return Translator(self.locale)(key, **params)


class Translator:
    """一种语言的消息查找：缺译回退另一种语言，并记录回退过的 key。

    资源整体缺失或损坏时抛 CatalogError，不静默给空词典——命令要么能本地化，
    要么明确失败，不能输出半套语言。
    """

    def __init__(self, locale: object, root: Path | None = None):
        tag = normalize_locale(locale) or DEFAULT_LOCALE
        catalogs = load_all(root=root)
        self.locale = tag if tag in catalogs else DEFAULT_LOCALE
        self.catalog = catalogs[self.locale]
        self.catalogs = catalogs
        self.missing: list[str] = []

    def __call__(self, key: str, *, count: int | None = None, **params: object) -> str:
        def on_missing(used: str) -> None:
            self.missing.append(used)

        return translate(self.catalog, key, catalogs=self.catalogs,
                         on_missing=on_missing, count=count, **params)
