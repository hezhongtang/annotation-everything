#!/usr/bin/env python3
"""生成「报告 + 讲稿」左右对照审阅页（单文件 index.html），可逐页批注。

左侧：报告逐页 HTML（Shadow DOM 渲染，文字可选中）；右侧：同页讲稿（可省略）。
方向键翻页，显示本页时长与累计用时；可选中文字引用、在报告上点位置标点、写批注。

批注保存位置：
- 用 serve.py 启动（http://127.0.0.1:端口/）：每次增删改即写入审阅目录 notes.json / notes.jsonl；
  服务断开时新改动暂存浏览器，恢复后自动补传。
- 直接双击打开 index.html（file://）：批注存浏览器 localStorage，用「复制全部 / 下载」交给 Claude。

输入约定：
- 报告 HTML：逐页 <section class="page" data-page="N" data-key="唯一键">…</section>；<style> 与本地
  <link rel="stylesheet"> 会一并带上；本地图片、字体等相对路径资源会内联成 data URI（单个上限 10 MB）。
  板块导航优先从报告目录推断（<a data-range-start="键" data-range-end="键"> 内含 .toc-label / .toc-title），
  也可用 --sections "开篇:1-6,主线一:7-21" 指定。
- 讲稿 Markdown（可选）：中文每页以「### 第 N 页｜标题」开头，英文每页以「### Page N｜Title」开头（同一份
  讲稿里可以混用，识别与界面语言无关）；字段中文写 **时长** / **讲法** / **过渡** / **可能被问**，英文写
  **Duration** / **Talking points** / **Transition** / **Likely questions**（冒号全角半角都可以，
  「可能被问」逐行写「问：……答：……」或「Q: … A: …」）。首个页头之前的内容作为「说明」浮层。
  页头认不出来、页码重复或字段重复时给出当前语言的诊断并退出，不把错输入当成空讲稿。

用法：
  python3 build_viewer.py --report 报告.html [--script 讲稿.md] --out 审阅目录 [--title 标题] [--name 审阅名] [--sections ...]
  python3 build_viewer.py --rebuild 审阅目录          # 按 review.json 记录的源文件与参数重建
输出 <out>/index.html 与 <out>/review.json（审阅标识、页码、标题、源文件路径）；不会动已有的 notes.json。
只用 Python 标准库。
"""
from __future__ import annotations

import argparse
import base64
import html
import json
import mimetypes
import os
import re
import secrets
import sys
from datetime import datetime
from pathlib import Path
from urllib.parse import unquote

sys.path.insert(0, str(Path(__file__).resolve().parent))
import fsutil  # noqa: E402

PAGE_HEAD = re.compile(r"^###\s*第\s*(\d+)\s*页\s*[｜|]\s*(.*)$", re.M)
PAGE_HEAD_EN = re.compile(r"^###\s*page\s*(\d+)\s*[｜|]\s*(.*)$", re.M | re.I)
# 以 ### 开头、紧跟页码标记，但不是合法页头的行：认不出来时指出来，不并进上一页或前言
HEAD_LIKE = re.compile(r"^#{3,}\s*(?:第\s*\d+\s*页?|page\s*\d+)", re.M | re.I)
# 字段名行：先抓出来，再判断认不认得；认不出的按正文保留（旧讲稿里的加粗引导句不会丢）
FIELD_LABEL = re.compile(r"^\*\*([^*：:]+)\*\*\s*[：:]", re.M)
FIELD_ALIASES = {   # 规范化字段名 → 认得的写法（大小写、空格多少不限）
    "时长": ("时长", "duration", "time"),
    "讲法": ("讲法", "talking points", "talking point", "talk"),
    "过渡": ("过渡", "transition"),
    "可能被问": ("可能被问", "likely questions", "likely question", "q&a", "questions"),
}
FIELD_BY_LABEL = {alias: name for name, aliases in FIELD_ALIASES.items() for alias in aliases}
# 问答行：「问：……答：……」与「Q: … A: …」。答的标记取先出现的那个；英文 A 紧贴字母或汉字时
# 不算标记（「套餐A：」「plan A:」都是正文），只有前面是空白、标点或行首才算。
QA_ANSWER = re.compile(r"答\s*[：:]|(?<!\w)A\s*[：:]")
QA_QUESTION = re.compile(r"^\s*(?:问|Q)\s*[：:]")
DURATION_UNIT = re.compile(r"([\d.]+)\s*(分钟|分|秒|min|minute|sec|second|s)", re.I)
MINUTE_UNITS = frozenset({"分钟", "分", "min", "minute"})
ATTR = re.compile(r"""([\w:.-]+)\s*=\s*(?:"([^"]*)"|'([^']*)'|([^\s"'>]+))""")
SECTION_TAG = re.compile(r"<(/?)section\b[^>]*>", re.I)
MASKED = re.compile(r"<!--.*?-->|<script\b[^>]*>.*?</script\s*>|<style\b[^>]*>.*?</style\s*>", re.S | re.I)
MAX_ASSET = 10 * 1024 * 1024
TEMPLATE = Path(__file__).resolve().parent / "viewer_template.html"
EXTRA_MIME = {".woff2": "font/woff2", ".woff": "font/woff", ".otf": "font/otf", ".ttf": "font/ttf",
              ".svg": "image/svg+xml", ".webp": "image/webp", ".avif": "image/avif"}
APP = "annotation-everything"


def esc(s: str) -> str:
    return html.escape(s.strip())


def inline(s: str) -> str:
    s = esc(s)
    s = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", s)
    return re.sub(r"`(.+?)`", r"<code>\1</code>", s)


def strip_tags(s: str) -> str:
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", s))).strip()


def attrs_of(tag: str) -> dict[str, str]:
    return {m.group(1).lower(): html.unescape(next(g for g in m.groups()[1:] if g is not None))
            for m in ATTR.finditer(tag)}


def md_block(text: str) -> str:
    """说明区用的极简 Markdown：标题、列表、表格、段落。"""
    out: list[str] = []
    table: list[str] = []

    def flush() -> None:
        if table:
            rows = [r for r in table if not re.match(r"^\|[\s\-|:]+\|$", r)]
            cells = [[inline(c) for c in r.strip().strip("|").split("|")] for r in rows]
            head = "".join(f"<th>{c}</th>" for c in cells[0])
            body = "".join("<tr>" + "".join(f"<td>{c}</td>" for c in r) + "</tr>" for r in cells[1:])
            out.append(f"<table><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table>")
            table.clear()

    for line in text.splitlines():
        if line.startswith("|"):
            table.append(line)
            continue
        flush()
        if line.startswith("# "):
            out.append(f"<h2>{inline(line[2:])}</h2>")
        elif line.startswith("## "):
            out.append(f"<h3>{inline(line[3:])}</h3>")
        elif line.startswith("- "):
            out.append(f"<li>{inline(line[2:])}</li>")
        elif line.strip() and line.strip() != "---":
            out.append(f"<p>{inline(line)}</p>")
    flush()
    return re.sub(r"((?:<li>.*?</li>)+)", r"<ul>\1</ul>", "".join(out))


def duration_seconds(dur: str) -> tuple[int, bool]:
    """时长字段 → (秒数, 是否至少成功读出一段)。写错的数字（「1.2.3 分钟」）跳过，不让生成器崩掉。"""
    total, read = 0.0, False
    for v, unit in DURATION_UNIT.findall(dur):
        try:
            total += float(v) * (60 if unit.lower() in MINUTE_UNITS else 1)
        except ValueError:
            continue
        read = True
    return round(total), read


def seconds(dur: str) -> int:
    return duration_seconds(dur)[0]


class ScriptError(Exception):
    """讲稿格式问题：messages 是已经本地化的诊断，调用方转成 SystemExit 报给作者。"""

    def __init__(self, messages: list[str]):
        super().__init__("\n".join(messages))
        self.messages = messages


class BuildError(SystemExit):
    """可复用库函数（报告分页、板块参数、源文件查找）的报错：调用方捕它来降级，
    不用再捕 SystemExit——那会连调用方自己的 sys.exit 一起吞掉。继承 SystemExit：
    库函数直接作为命令行跑时，退出码与 stderr 和以前完全一样。"""


def page_heads(script: str) -> list[re.Match]:
    """中英页头按出现顺序合并：`### 第 N 页｜标题` 与 `### Page N｜Title` 可以在同一份讲稿里混用。

    两种写法的 group(1) 都是页码、group(2) 都是标题。
    """
    return sorted((*PAGE_HEAD.finditer(script), *PAGE_HEAD_EN.finditer(script)), key=lambda m: m.start())


def field_name(label: str) -> str | None:
    """字段名归一：中文原名或英文写法（大小写、空格不限）→ 规范中文名；认不出返回 None。"""
    return FIELD_BY_LABEL.get(" ".join(label.split()).lower())


def line_no(text: str, pos: int) -> int:
    return text.count("\n", 0, pos) + 1


def line_of(text: str, pos: int) -> str:
    """pos 所在的整行（从 pos 到行尾）。"""
    end = text.find("\n", pos)
    return text[pos: len(text) if end < 0 else end]


def clip(text: str, limit: int = 60) -> str:
    return text if len(text) <= limit else text[:limit] + "…"


def join_lines(locale: str | None, lines: list[int]) -> str:
    """诊断里的行号列表：英文用逗号加空格，中文用顿号。"""
    return (", " if ui_locale(locale) == "en" else "、").join(str(x) for x in lines)


def qa_pair(line: str) -> tuple[str, str]:
    """一行问答拆成 (问, 答)：答的标记取先出现的那个，问的标记在开头时才去掉。"""
    m = QA_ANSWER.search(line)
    q, a = (line[:m.start()], line[m.end():]) if m else (line, "")
    return QA_QUESTION.sub("", q, count=1).strip(), a.strip()


def ui_locale(locale: str | None) -> str:
    """本次生成用的具体语言：auto 按系统语言，没有配置（旧审阅、旧调用）保持中文。"""
    import i18n
    try:
        return i18n.resolve_locale(locale, system_languages=i18n.system_languages())
    except i18n.LocaleError:
        return i18n.DEFAULT_LOCALE


def diag(locale: str | None, key: str, **params: object) -> str:
    """按当前语言取一条诊断文案。"""
    import i18n
    return i18n.translate(i18n.catalog(ui_locale(locale)), key, **params)


def parse_script(script: str, locale: str | None = None, path: str = "") -> tuple[str, str, list[dict], list[str]]:
    """返回 (H1 标题, 说明浮层 HTML, 逐页讲稿, 格式警告)。

    页头与字段的中英文写法都认，与界面语言无关：locale 只决定诊断用哪种语言说，path 只用在诊断里。
    认不出的字段名按正文保留（旧讲稿不会丢内容），重复字段采用最后一次的值，都给出警告；
    认不出的页头和重复的页码是格式错误，抛 ScriptError。
    """
    heads = page_heads(script)
    if not heads:
        raise ScriptError([diag(locale, "build.scriptNoPages", path=path, expected=diag(locale, "formats.pageHead"))])
    problems: list[str] = []
    starts = {h.start() for h in heads}
    for m in HEAD_LIKE.finditer(script):
        if m.start() in starts:
            continue
        problems.append(diag(locale, "build.scriptBadHead", path=path, line=line_no(script, m.start()),
                             text=clip(line_of(script, m.start()).strip()), expected=diag(locale, "formats.pageHead")))
    seen: dict[int, list[int]] = {}
    for h in heads:
        seen.setdefault(int(h.group(1)), []).append(line_no(script, h.start()))
    for n, lines in sorted(seen.items()):
        if len(lines) > 1:
            problems.append(diag(locale, "build.scriptDuplicatePage", path=path, page=n,
                                 lines=join_lines(locale, lines)))
    if problems:
        raise ScriptError(problems)
    intro = script[: heads[0].start()]
    h1 = next((ln[2:].strip() for ln in intro.splitlines() if ln.startswith("# ")), "")
    pages, warnings = [], []
    for i, h in enumerate(heads):
        body = script[h.end(): heads[i + 1].start() if i + 1 < len(heads) else len(script)]
        base = h.end()   # body 里的偏移换算回整份讲稿的行号
        page_no = int(h.group(1))
        fields: dict[str, str] = {}
        known: list[tuple[re.Match, str]] = []
        for m in FIELD_LABEL.finditer(body):
            name = field_name(m.group(1))
            if name is None:
                warnings.append(diag(locale, "build.scriptUnknownField", page=page_no,
                                     line=line_no(script, base + m.start()), field=m.group(1).strip(),
                                     known=diag(locale, "formats.fields")))
                continue
            known.append((m, name))
        placed: dict[str, list[int]] = {}
        labels: dict[str, str] = {}
        for j, (m, name) in enumerate(known):
            end = known[j + 1][0].start() if j + 1 < len(known) else len(body)
            here = line_no(script, base + m.start())
            if name in fields:   # 重复字段：后面的值生效，并指出前面几次出现在哪几行
                placed[name].append(here)
                warnings.append(diag(locale, "build.scriptDuplicateField", page=page_no,
                                     field=labels[name], lines=join_lines(locale, placed[name])))
            else:
                placed[name] = [here]
                labels[name] = m.group(1).strip()   # 诊断里用作者写的字段名，中英各按原样
            fields[name] = body[m.end():end].strip()
        # 第一个字段之前的内容也按「讲法」显示：写错的字段名、漏掉的加粗都不会让文字凭空消失
        lead = body[: known[0][0].start()].strip() if known else ""
        talk_src = (lead + "\n" + fields.get("讲法", "")) if known else body.strip()
        talk = "".join(f"<p>{inline(ln)}</p>" for ln in talk_src.splitlines() if ln.strip() and ln.strip() != "---")
        qa = []
        for ln in fields.get("可能被问", "").splitlines():
            ln = ln.strip().lstrip("-*•").strip()
            if not ln:
                continue
            q, a = qa_pair(ln)
            qa.append({"q": inline(q), "a": inline(a)})
        dur = fields.get("时长", "").strip()
        sec, read = duration_seconds(dur)
        if dur and not read and re.search(r"\d", dur):
            # 有时长字段、里面也有数字，却一段都没读出来：写法看不懂，别静默按 0 秒算
            warnings.append(diag(locale, "build.scriptBadDuration", page=page_no, text=clip(dur)))
        pages.append({"n": int(h.group(1)), "title": esc(h.group(2)), "dur": esc(dur), "sec": sec,
                      "talk": talk, "next": inline(fields.get("过渡", "")), "qa": qa})
    return h1, md_block(intro), pages, warnings


# ---------- 报告解析 ----------
def find_pages(doc: str, *, locale: str | None = None) -> list[dict]:
    """按嵌套层级找出 class 含 page 且带 data-page 的 section（注释、脚本、样式里的标签不算）。"""
    masked = MASKED.sub(lambda m: " " * len(m.group(0)), doc)
    stack: list[tuple[int, dict[str, str], bool]] = []
    pages: list[dict] = []
    for m in SECTION_TAG.finditer(masked):
        if not m.group(1):
            attrs = attrs_of(doc[m.start():m.end()])
            is_page = "page" in attrs.get("class", "").split() and "data-page" in attrs
            if is_page and any(p for _s, _a, p in stack):
                raise BuildError(diag(locale, "build.report.nested", page=attrs["data-page"]))
            stack.append((m.start(), attrs, is_page))
        elif stack:
            start, attrs, is_page = stack.pop()
            if is_page:
                pages.append({"attrs": attrs, "html": doc[start:m.end()]})
    unclosed = [a.get("data-page") for _s, a, p in stack if p]
    if unclosed:
        raise BuildError(diag(locale, "build.report.unclosed", pages=unclosed))
    out, seen = [], set()
    for p in pages:
        a = p["attrs"]
        try:
            n = int(a["data-page"].strip())
        except ValueError:
            raise BuildError(diag(locale, "build.report.pageNumber", page=repr(a["data-page"]))) from None
        if n in seen:
            raise BuildError(diag(locale, "build.report.duplicate", page=n))
        seen.add(n)
        key = a.get("data-key") or a.get("id", "").removeprefix("page-") or f"p{n}"
        hm = re.search(r"<h[1-3][^>]*>(.*?)</h[1-3]>", p["html"], re.S)
        out.append({"n": n, "key": key, "stable": stable_key(key, bool(a.get("data-key"))), "html": p["html"],
                    "title": strip_tags(hm.group(1)) if hm else key})
    return out


def stable_key(key: str, explicit: bool = True) -> bool:
    """页面键能不能用来在重排页序后找回原页：由页码生成的（p3、page-3、3）不算。"""
    return explicit and not re.fullmatch(r"(?:p|page)?[-_]?\d+", str(key), re.I)


def infer_sections(doc: str, sects: list[dict]) -> list[list]:
    """从报告目录推断板块：<a data-range-start data-range-end> 内的 .toc-label / .toc-title。"""
    by_key = {s["key"]: s["n"] for s in sects}
    cands, seen = [], set()
    for m in re.finditer(r"<a\b([^>]*\bdata-range-start\s*=[^>]*)>(.*?)</a>", doc, re.S):
        attrs = attrs_of(m.group(1))
        a, b = by_key.get(attrs.get("data-range-start", "")), by_key.get(attrs.get("data-range-end", ""))
        if a is None or b is None:
            continue
        label = re.search(r'class="[^"]*\btoc-label\b[^"]*"[^>]*>(.*?)<', m.group(2), re.S)
        title = re.search(r'class="[^"]*\btoc-title\b[^"]*"[^>]*>(.*?)<', m.group(2), re.S)
        name = " ".join(strip_tags(x.group(1)) for x in (label, title) if x) or strip_tags(m.group(2))[:20]
        lo, hi = min(a, b), max(a, b)
        if (lo, hi) in seen:
            continue
        seen.add((lo, hi))
        cands.append({"name": name, "lo": lo, "hi": hi, "labelled": bool(label)})
    # 目录页的主条目带 .toc-label；板块导航页里的子链接不带，有主条目时只取主条目
    if any(c["labelled"] for c in cands):
        cands = [c for c in cands if c["labelled"]]
    # 只保留最外层范围，嵌套在别的范围里的子板块不进导航条
    top = [c for c in cands if not any(o is not c and o["lo"] <= c["lo"] and c["hi"] <= o["hi"]
                                       and (o["lo"], o["hi"]) != (c["lo"], c["hi"]) for o in cands)]
    return [[c["name"], c["lo"], c["hi"]] for c in sorted(top, key=lambda c: c["lo"])]


def parse_sections_arg(arg: str, *, locale: str | None = None) -> list[list]:
    out = []
    for part in re.split(r"[,，]", arg):
        part = part.strip()
        if not part:
            continue
        m = re.match(r"^(.+?)[:：]\s*(\d+)\s*(?:[-–~]\s*(\d+))?$", part)
        if not m:
            raise BuildError(diag(locale, "build.report.sections", part=repr(part)))
        a = int(m.group(2))
        out.append([m.group(1).strip(), a, int(m.group(3) or a)])
    return out


# ---------- 资源内联 ----------
ASSET_TYPES = ("image/", "font/", "audio/", "video/")


class Assets:
    """把本地相对路径资源换成 data URI；外链、找不到、超出允许目录或类型不对的文件记警告后跳过。

    只内联 root（默认报告所在目录）之内的图片、字体、音视频；样式表另行处理。
    这样报告里写错的路径（例如 ../../.ssh/id_rsa）不会被悄悄塞进要分享出去的单文件页面。
    """

    def __init__(self, root: Path, *, locale: str | None = None) -> None:
        self.locale = locale
        self.root = root.resolve()
        self.cache: dict[Path, str] = {}
        self.warnings: list[str] = []
        self.total = 0

    def local(self, ref: str, base: Path) -> Path | None:
        """解析本地引用；超出允许目录时记警告并返回 None。"""
        path = (base / unquote(ref.split("#", 1)[0].split("?", 1)[0])).resolve()
        if not path.is_relative_to(self.root):
            self.warn(diag(self.locale, "build.asset.outside", ref=ref[:120], root=self.root))
            return None
        return path

    def uri(self, ref: str, base: Path) -> str | None:
        r = ref.strip()
        low = r.lower()
        if not r or low.startswith(("data:", "#", "mailto:", "javascript:", "blob:", "about:")):
            return None
        if low.startswith(("http:", "https:", "//")):
            self.warn(diag(self.locale, "build.asset.external", ref=r[:120]))
            return None
        path = self.local(r, base)
        if path is None:
            return None
        if path in self.cache:
            return self.cache[path]
        if not path.is_file():
            self.warn(diag(self.locale, "build.asset.missing", ref=r[:120], base=base))
            return None
        mime = EXTRA_MIME.get(path.suffix.lower()) or mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        if not mime.startswith(ASSET_TYPES):
            self.warn(diag(self.locale, "build.asset.type", mime=mime, ref=r[:120]))
            return None
        size = path.stat().st_size
        if size > MAX_ASSET:
            self.warn(diag(self.locale, "build.asset.large", limit=MAX_ASSET // 1024 // 1024, path=path))
            return None
        data = f"data:{mime};base64," + base64.b64encode(path.read_bytes()).decode()
        self.cache[path] = data
        self.total += size
        return data

    def warn(self, msg: str) -> None:
        if msg not in self.warnings:
            self.warnings.append(msg)

    def css(self, css: str, base: Path) -> str:
        def rep(m: re.Match) -> str:
            got = self.uri(m.group(2), base)
            return f"url(\"{got}\")" if got else m.group(0)
        return re.sub(r"""url\(\s*(["']?)(.*?)\1\s*\)""", rep, css)

    def html(self, frag: str, base: Path) -> str:
        def attr(m: re.Match) -> str:
            got = self.uri(html.unescape(m.group(3)), base)
            return f"{m.group(1)}{m.group(2)}{got}{m.group(2)}" if got else m.group(0)

        def srcset(m: re.Match) -> str:
            parts = []
            for cand in m.group(3).split(","):
                bits = cand.strip().split(None, 1)
                if not bits:
                    continue
                got = self.uri(html.unescape(bits[0]), base)
                parts.append(" ".join([got or bits[0], *bits[1:]]))
            return f"{m.group(1)}{m.group(2)}{', '.join(parts)}{m.group(2)}"

        frag = re.sub(r"""(\s(?:src|poster)\s*=\s*)(["'])(.*?)\2""", attr, frag, flags=re.I)
        frag = re.sub(r"""(<image\b[^>]*?\s(?:xlink:)?href\s*=\s*)(["'])(.*?)\2""", attr, frag, flags=re.I)
        frag = re.sub(r"""(\ssrcset\s*=\s*)(["'])(.*?)\2""", srcset, frag, flags=re.I)
        return self.css(frag, base)


# ---------- 样式：适配 Shadow DOM ----------
SEL_ROOT = re.compile(r"""(?<![\w.#\-\]\)"'=])(:root|html|body)((?:\.[\w-]+|#[\w-]+|\[[^\]]*\])*)(?![\w-])""")
NESTING_AT = {"media", "supports", "layer", "container", "document", "scope"}


def _skip(css: str, i: int) -> int:
    """i 处若是注释或字符串，返回其后位置；否则原样返回。"""
    if css.startswith("/*", i):
        j = css.find("*/", i + 2)
        return len(css) if j < 0 else j + 2
    if css[i] in "\"'":
        q, j = css[i], i + 1
        while j < len(css) and css[j] != q:
            j += 2 if css[j] == "\\" else 1
        return j + 1
    return i


def _block_end(css: str, i: int) -> int:
    """i 指向 '{' 之后；返回与之匹配的 '}' 的位置。"""
    depth = 1
    while i < len(css):
        j = _skip(css, i)
        if j != i:
            i = j
            continue
        if css[i] == "{":
            depth += 1
        elif css[i] == "}":
            depth -= 1
            if depth == 0:
                return i
        i += 1
    return len(css)


SEL_MASK = re.compile(r"""\\.|"(?:\\.|[^"\\])*"|'(?:\\.|[^'\\])*'|\[[^\]]*\]|::(?:part|slotted)\([^)]*\)""")


def _rewrite_selector(prelude: str) -> str:
    """只改写真正的 html / body / :root 选择器：引号、属性选择器、转义字符、::part() 里的同名文字不动。"""
    masked = SEL_MASK.sub(lambda m: m.group(0)[0] + "_" * (len(m.group(0)) - 1), prelude)
    out, last = [], 0
    for m in SEL_ROOT.finditer(masked):
        tag, suf = m.group(1), prelude[m.start(2):m.end(2)]
        out.append(prelude[last:m.start()])
        out.append("#ae-stage" + suf if tag == "body" else (f":host({suf})" if suf else ":host"))
        last = m.end()
    out.append(prelude[last:])
    return "".join(out)


ROOT_FS = re.compile(r"(?:^|[};])\s*((?:html|:root)\s*(?:,[^{};]*)?)\{([^}]*)\}")
SAFE_FS = re.compile(r"^\d+(?:\.\d+)?(?:px|%|pt|em|rem)$")


def root_font_size(css: str) -> str | None:
    """报告在 html / :root 上设的字号。Shadow DOM 里 rem 按外层文档的根字号算，生成时把它搬到外层。"""
    css = re.sub(r"/\*.*?\*/", "", css, flags=re.S)
    found = None
    for m in ROOT_FS.finditer(css):
        sel = [x.strip() for x in m.group(1).split(",")]
        if not any(x in ("html", ":root") for x in sel):
            continue
        for d in re.finditer(r"font-size\s*:\s*([^;!}]+)", m.group(2)):
            v = d.group(1).strip()
            if SAFE_FS.match(v):
                found = v
    return found


def shadow_css(css: str) -> str:
    """报告样式放进 Shadow DOM 后，:root / html 不再匹配：改写成 :host，body 改写成 #stage。"""
    out, i, start = [], 0, 0
    while i < len(css):
        j = _skip(css, i)
        if j != i:
            i = j
            continue
        ch = css[i]
        if ch == ";" or ch == "}":
            out.append(css[start:i + 1])
            start = i = i + 1
            continue
        if ch == "{":
            prelude = css[start:i]
            end = _block_end(css, i + 1)
            inner = css[i + 1:end]
            at = re.match(r"\s*@([\w-]+)", prelude)
            if at:
                body = shadow_css(inner) if at.group(1).lower() in NESTING_AT else inner
                out.append(prelude + "{" + body + "}")
            else:
                out.append(_rewrite_selector(prelude) + "{" + inner + "}")
            start = i = end + 1
            continue
        i += 1
    out.append(css[start:])
    return "".join(out)


def report_parts(doc: str, report: Path, assets: Assets) -> tuple[str, str, list[dict]]:
    """取报告的 <title>、样式（含本地样式表）与逐页 section，并内联本地资源。"""
    base = report.resolve().parent
    title_m = re.search(r"<title>(.*?)</title>", doc, re.S | re.I)
    css_parts = []
    # 去掉注释与脚本后，按出现顺序收集 <link rel=stylesheet> 与 <style>（保持原来的层叠顺序）
    visible = MASKED.sub(lambda m: m.group(0) if m.group(0).lower().startswith("<style") else " " * len(m.group(0)), doc)
    for m in re.finditer(r"<link\b[^>]*>|<style\b[^>]*>(.*?)</style\s*>", visible, re.S | re.I):
        if m.group(0).lower().startswith("<style"):
            css_parts.append(assets.css(m.group(1), base))
            continue
        a = attrs_of(m.group(0))
        if "stylesheet" not in a.get("rel", "").lower().split() or not a.get("href"):
            continue
        href = a["href"]
        if href.lower().startswith(("http:", "https:", "//")):
            assets.warn(diag(assets.locale, "build.asset.externalCss", ref=href[:120]))
            continue
        f = assets.local(href, base)
        if f is None:
            continue
        if f.is_file():
            css_parts.append(assets.css(f.read_text(encoding="utf-8", errors="replace"), f.parent))
        else:
            assets.warn(diag(assets.locale, "build.asset.missingCss", ref=href))
    sects = find_pages(doc, locale=assets.locale)
    for s in sects:
        s["html"] = assets.html(s["html"], base)
    raw_css = "\n".join(css_parts)
    assets.root_fs = root_font_size(raw_css)
    return (strip_tags(title_m.group(1)) if title_m else ""), shadow_css(raw_css), sects


# ---------- 输出 ----------
def _js(obj: object) -> str:
    """嵌入 <script> 的 JSON：转义 </ 与 <!-- 防止提前结束脚本。"""
    return json.dumps(obj, ensure_ascii=False).replace("</", "<\\/").replace("<!--", "<\\!--")


def page_csp(page_html: str) -> str:
    """与 serve.py 的响应头一致：只放行页面自身的内联脚本（按 sha256）。用于 file:// 打开时。

    哈希算法与 serve.py 的响应头共用一份；这一版不带 frame-ancestors、没有脚本时也不兜底。
    """
    hashes = fsutil.script_hashes(page_html)
    return (f"default-src 'none'; script-src {hashes}; style-src 'unsafe-inline'; img-src data: blob:; "
            "font-src data:; media-src data: blob:; connect-src 'self'; base-uri 'none'; form-action 'none'; "
            "object-src 'none'")


def write_private(path: Path, text: str) -> None:
    """原子写入（临时文件 + os.replace），权限 0600：服务读到的不会是写了一半的文件。"""
    import fsutil
    fsutil.atomic_write(path, text, prefix="." + path.name + "-", fsync=False)


def read_skills(review_dir: Path) -> list[dict]:
    """审阅目录里代理写入的技能目录（skills.json），嵌进页面供「技能」输入栏做候选（本地文件模式也能用）。"""
    import notes_store
    return notes_store.skills_catalog(review_dir)


def read_always(review_dir: Path) -> list[dict]:
    """常驻技能（skills.json 的 always），嵌进页面：本地文件模式没有服务时也能看到、导出。"""
    import notes_store   # 与服务、命令行同一套宽松读取：坏项跳过、好项保留
    return notes_store.always_skills(review_dir)


def resolve_sources(review_dir: Path, manifest: dict | None = None, *, locale: str | None = None) -> dict[str, Path | None]:
    """按 review.json 找源文件：先用相对审阅目录的路径（目录树整体复制后仍指向副本），再用绝对路径。"""
    import ledger
    m = manifest if manifest is not None else ledger.read_manifest(review_dir)
    problem = ledger.unsupported_format(m)
    if problem:   # 更高版本写下的报告格式：明确拒绝，不按普通文本读
        raise BuildError(problem)
    if "sourcesRel" in m:  # 0.1 版格式：sources 是绝对路径
        rel, absolute = m.get("sourcesRel") or {}, m.get("sources") or {}
    else:
        rel, absolute = m.get("sources") or {}, m.get("sourcesAbs") or {}
    out: dict[str, Path | None] = {}
    for k in ("report", "script"):
        cand = [review_dir / rel[k]] if rel.get(k) else []
        cand += [Path(absolute[k])] if absolute.get(k) else []
        hit = next((c.resolve() for c in cand if c.is_file()), None)
        if hit is None and cand:
            tried = "、".join(str(c) for c in cand)
            raise BuildError(diag(locale, "build.source.missing", kind=diag(locale, "cli.show.source." + k), paths=tried))
        out[k] = hit
    return out


def pptx_parts(report: Path, locale: str | None) -> tuple[str, str, list[dict], dict]:
    """PPTX 报告 → (标题, 样式, 逐页 section, PPTX 元数据)。渲染走本机工具，缺工具或
    转换失败时报明确的原因，不按文本读，也不静默给空白页。"""
    import pptx_report
    try:
        r = pptx_report.parts(report)
    except pptx_report.PptxReportError as exc:
        raise BuildError(diag(locale, f"build.pptx.{exc.code}", path=report, detail=exc.detail)) from None
    sects = [{"n": pg["n"], "key": pg["key"], "stable": True, "html": pg["html"], "title": pg["title"],
              "w": pg["w"], "h": pg["h"], "orient": pg["orient"]} for pg in r["pages"]]
    return r["title"], r["css"], sects, r["meta"]


def pptx_warnings(meta: dict, locale: str | None) -> list[str]:
    """PPT 构建诊断 → 当前语言的提示（渲染证据、字体与未核验项）。"""
    out = [diag(locale, "build.pptx.rendered", pages=meta["renderer"]["pages"])]
    if meta.get("fonts", {}).get("warning") or meta.get("fonts", {}).get("installed") != "ok":
        out.append(diag(locale, "build.pptx.fonts"))
    unverified = meta.get("unverified") or []
    if unverified:
        out.append(diag(locale, "build.pptx.unverified", items="、".join(
            diag(locale, f"build.pptx.unverified.{k}") for k in unverified)))
    return out


def _pptx_view_element(e: dict) -> dict:
    """元素模型瘦身到交互与定位所需：身份、几何、能力、文本段落与 run 边界（样式细节不进页面）。"""
    paragraphs = [{"n": p["index"], "text": p["text"], "digest": p["digest"],
                   "runs": [{"start": r["start"], "end": r["end"]} for r in p["runs"]]}
                  for p in e.get("paragraphs") or [] if p.get("text")]
    out = {"key": e["key"], "id": str(e.get("id") or ""), "name": e.get("name") or "", "kind": e["kind"],
           "z": e.get("z", 0), "geo": (e.get("geometry") or {}).get("normalized"), "cap": e["capabilities"],
           "digest": e.get("evidenceDigest") or ""}
    if paragraphs:
        out["paragraphs"] = paragraphs
    return out


def pptx_view(meta: dict | None) -> dict | None:
    """对照页用的 PPTX 摘要：渲染信息、字体证据、未核验项与逐页元素模型（交互与幻灯片定位的依据）。"""
    import pptx_report
    if meta is None:
        return None
    return {"v": pptx_report.FORMAT_VERSION, "fp": meta.get("fingerprint") or "",
            "renderer": meta.get("renderer") or {}, "fonts": meta.get("fonts") or {},
            "slides": [{"key": s["key"], "n": s["n"], "notes": bool(s.get("notes")),
                        "elements": [_pptx_view_element(e) for e in s.get("elements") or []]}
                       for s in meta.get("slides", [])],
            "unverified": meta.get("unverified") or []}


def docx_parts(report: Path, locale: str | None) -> tuple[str, str, list[dict], dict, str]:
    """DOCX 报告 → (标题, 样式, 逐页 section, DOCX 元数据, 文档语言)。读不了时报明确的原因，不按文本读。"""
    import docx_review
    try:
        r = docx_review.render(report)
    except docx_review.DocxError as exc:
        raise BuildError(diag(locale, f"build.docx.{exc.code}", path=report, detail=exc.detail)) from None
    sects = [{"n": pg["n"], "key": pg["key"], "stable": True, "html": pg["html"], "title": pg["title"],
              "w": pg["w"], "h": pg["h"], "orient": pg["orient"], "label": pg["label"]} for pg in r["pages"]]
    meta = {"pagination": r["pagination"], "appPages": r["appPages"], "application": r["application"],
            "pages": len(sects), "diagnostics": r["diagnostics"], "fingerprint": r["fingerprint"],
            "anchorVersion": docx_review.ANCHOR_VERSION,
            "_paragraphs": r["paragraphs"]}   # 只给本次重建的重映射用，不写进 review.json
    return r["title"], shadow_css(r["css"]), sects, meta, r["lang"]


def docx_warnings(meta: dict, locale: str | None) -> list[str]:
    """DOCX 的分页可信度与支持矩阵诊断 → 当前语言的提示（只列有限制的结构，正常显示的不刷屏）。"""
    import docx_review
    out = []
    if meta["pagination"] != "verified":
        out.append(diag(locale, f"build.docx.pagination.{meta['pagination']}", pages=meta["pages"],
                        app=meta.get("appPages") or "?", application=meta.get("application") or "?"))
    sep = ", " if ui_locale(locale) == "en" else "、"
    for d in meta["diagnostics"]:
        if d["code"] in docx_review.INFO_KINDS or d["code"] == "estimatedPages":
            continue
        pages = sep.join(str(x) for x in d["pages"][:8]) + ("…" if len(d["pages"]) > 8 else "")
        extra = (" · " + sep.join(d["examples"][:3])) if d["code"] in ("unknown", "numbering", "object") and d["examples"] else ""
        out.append(diag(locale, "build.docx.diagLine", text=diag(locale, f"docx.diag.{d['code']}"), count=d["count"],
                        pages=pages or "-") + extra)
    return out


def file_label(name: str, rid: str) -> str:
    """导出文件名里的审阅标签：审阅名（只留文件名安全的字符，最多 40 字）+ 标识前 4 位，两份审阅同名也分得开。"""
    slug = re.sub(r"[^\w.-]+", "_", name).strip("._")[:40] or "审阅"
    return f"{slug}_{rid[:4]}"


def build(report: Path, script: Path | None, out: Path, title: str | None = None,
          sections_arg: str | None = None, no_sections: bool = False, lenient: bool = False,
          asset_root: Path | None = None, name: str | None = None,
          skills_from: list[Path] | None = None, no_scan_skills: bool = False,
          locale: str | None = None) -> dict:
    import i18n
    import ledger
    preview = ledger.read_manifest(out) if out.exists() else {}
    try:
        # 调用方没写 --locale 时沿用已有 review.json；两边都没有则不写入（旧审阅保持中文）
        locale_choice = i18n.canonical_choice(i18n.effective_choice(locale, os.environ.get("AE_LOCALE"), preview.get("locale")))
    except i18n.LocaleError as exc:
        raise SystemExit(f"错误：{exc}") from exc
    # 生成侧的提示按本次选择的语言写；没选就是中文（旧审阅、旧调用）。auto 在这里定成具体语言
    warn_catalog = i18n.catalog(
        i18n.resolve_locale(locale_choice, system_languages=i18n.system_languages()))
    assets = Assets(asset_root or report.resolve().parent, locale=locale_choice)
    assets.root_fs = None
    import docx_review
    import pptx_report
    docx_meta: dict | None = None
    docx_paragraphs: list[dict] = []
    docx_lang = ""
    pptx_meta: dict | None = None
    if pptx_report.is_pptx(report):
        # PPTX 直接审阅：渲染图负责视觉、结构化元素模型负责定位；不按 UTF-8 文本读
        doc = ""
        rtitle, rcss, sects, pptx_meta = pptx_parts(report, locale_choice)
        for w in pptx_warnings(pptx_meta, locale_choice):
            assets.warn(w)
    elif pptx_report.is_ppt(report):
        raise BuildError(diag(locale_choice, "build.pptx.legacyPpt", path=report))
    elif docx_review.is_word_like(report):
        # Word 文档直接渲染：不按 UTF-8 文本读，也不过 HTML 资源内联（正文里的 url(...) 只是文字）
        doc = ""
        rtitle, rcss, sects, docx_meta, docx_lang = docx_parts(report, locale_choice)
        docx_paragraphs = docx_meta.pop("_paragraphs")
        for w in docx_warnings(docx_meta, locale_choice):
            assets.warn(w)
    else:
        try:
            doc = report.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            raise BuildError(diag(locale_choice, "build.report.notText", path=report)) from None
        rtitle, rcss, sects = report_parts(doc, report, assets)
    if not sects:
        raise SystemExit(diag(locale_choice, "build.report.noPages", path=report))
    h1, intro, spages = ("", "", [])
    script_warnings: list[str] = []
    if script is not None:
        try:
            h1, intro, spages, script_warnings = parse_script(script.read_text(encoding="utf-8"),
                                                              locale_choice, str(script))
        except ScriptError as exc:
            raise SystemExit("\n".join(exc.messages)) from None
    rnums = [s["n"] for s in sects]
    if script is not None:
        snums = [p["n"] for p in spages]
        if snums != rnums:
            only_r = sorted(set(rnums) - set(snums))
            only_s = sorted(set(snums) - set(rnums))
            detail = "".join(x for x in (
                diag(locale_choice, "build.diff.onlyReport", pages=only_r) if only_r else "",
                diag(locale_choice, "build.diff.onlyScript", pages=only_s) if only_s else "",
                diag(locale_choice, "build.diff.order") if not only_r and not only_s else "") if x)
            msg = diag(locale_choice, "build.pageMismatch", report=len(rnums), script=len(snums), detail=detail)
            if not lenient:
                # 讲稿里的格式警告一起给出：只报页码不一致，作者不知道该先修哪一处
                for w in script_warnings:
                    print(diag(locale_choice, "cli.notes.warning", reason=w), file=sys.stderr)
                raise SystemExit(msg + diag(locale_choice, "build.pageMismatchLenient"))
            print(diag(locale_choice, "cli.notes.warning", reason=msg), file=sys.stderr)
        for w in script_warnings:
            assets.warn(w)
    by_n = {p["n"]: p for p in spages}
    pages = []
    for s in sects:
        p = by_n.get(s["n"]) or {"n": s["n"], "title": esc(s["title"]), "dur": "", "sec": 0, "talk": "", "next": "", "qa": []}
        pg = {**p, "key": s["key"], "stable": s["stable"]}
        if docx_meta is not None:   # 每页自己的尺寸与 Word 页码：舞台按它显示，不套 1280×720
            pg.update(w=s["w"], h=s["h"], label=s["label"], orient=s["orient"])
        if pptx_meta is not None:   # PPT 页同样按幻灯片原始尺寸与方向显示
            pg.update(w=s["w"], h=s["h"], orient=s["orient"])
        pages.append(pg)
    if no_sections:
        sections: list[list] = []
    elif sections_arg:
        sections = parse_sections_arg(sections_arg, locale=locale_choice)
    elif docx_meta is not None or pptx_meta is not None:
        sections = []
    else:
        sections = infer_sections(doc, sects)
    final_title = title or rtitle or h1 or report.stem
    if not intro:
        # 讲稿没有前言时用系统默认简介：句子走词典，页面切语言时由 applyLocale 重写
        intro = (f'<h2>{esc(final_title)}</h2>'
                 f'<p data-msg="viewer.introDefault">'
                 f'{esc(i18n.translate(i18n.catalog(ui_locale(locale_choice)), "viewer.introDefault"))}</p>')
    out.mkdir(parents=True, exist_ok=True)
    if not no_scan_skills:
        import notes_store
        import skill_sources
        skill_sources.set_locale(i18n.resolve_locale(locale_choice, system_languages=i18n.system_languages()))
        try:
            results, skill_warnings = skill_sources.scan(skills_from if skills_from is not None else skill_sources.default_roots())
            for warning in skill_warnings:
                assets.warn(warning)
            if skills_from is not None and any(items is None for _key, items, _complete in results):
                assets.warn(i18n.translate(warn_catalog, "cli.skills.warnUnavailable"))
            if any(items is not None for _key, items, _complete in results) or (out / notes_store.SKILL_SOURCES_FILE).exists():
                _catalog, retained = notes_store.skills_merge_scan(out, results)
                if retained and skills_from is None:
                    assets.warn(i18n.translate(warn_catalog, "cli.skills.warnUnavailable"))
        except (OSError, notes_store.NoteError) as e:
            assets.warn(str(e))
    old = ledger.read_manifest(out)
    old_keys = {int(pg["n"]): str(pg["key"]) for pg in old.get("pages", [])
                if isinstance(pg, dict) and "n" in pg and pg.get("key") and pg.get("stable", stable_key(pg["key"]))}
    # 审阅标识：页面据此确认连上的是这份审阅的服务；重建时沿用。目录是从别处复制来的，就换一个新的
    rid = old.get("reviewId") if ledger.RID.fullmatch(str(old.get("reviewId", ""))) else None
    # 原目录还在、且标识相同才算复制；整个目录被移走（原目录已不在）则沿用标识，浏览器里的暂存不丢
    copied = rid is not None and ledger.copied_from(out, old) is not None
    if copied:
        assets.warn(diag(locale_choice, "build.source.copied", path=old["reviewDir"]))
    if rid is None or copied:
        rid = secrets.token_hex(8)
    # 审阅名：给人看的名字（导出文件名、台账列表都用它），默认取标题；不要求唯一
    review_name = (name or "").strip() or final_title
    ns = file_label(review_name, rid)
    image_pages = sum(bool(re.search(r"<img\b", s["html"], re.I)) for s in sects)
    skill_context = {"report": report.name, "script": script.name if script else "",
                     "kind": "PPT 幻灯片" if pptx_meta is not None else
                     "Word 文档" if docx_meta is not None else
                     "图片 幻灯片" if image_pages >= (len(sects) + 1) // 2 else "HTML 报告"}
    import notes_store
    import i18n
    catalogs = {tag: i18n.catalog(tag)["messages"] for tag in i18n.SUPPORTED}
    report_html = re.search(r"<html\b[^>]*>", doc, re.I)
    report_language = attrs_of(report_html.group()).get("lang", "") if report_html else docx_lang
    values = {"TITLE": html.escape(final_title), "INTRO": intro, "PAGES": _js(pages), "SECTIONS": _js(sections),
              "NS": _js(ns), "NAME": _js(review_name), "RID": _js(rid), "RCSS": _js(rcss), "SECT": _js([s["html"] for s in sects]),
              "ROOTFS": assets.root_fs or "100%", "SKILLS": _js(read_skills(out)), "ALWAYS": _js(read_always(out)),
              "SKILL_CONTEXT": _js(skill_context), "FULL_CHECK_COMPAT": _js("【本批提交选项】" + notes_store.FULL_CHECK_TEXT),
              "CATALOGS": _js(catalogs), "LOCALE": _js(locale_choice), "REPORT_LANG": _js(report_language),
              "DOCX": _js(docx_view(docx_meta)), "PPTX": _js(pptx_view(pptx_meta))}
    # 一次性替换，避免内容里恰好出现占位符文字时被二次替换；CSP 要等脚本定稿后再算哈希
    page_html = re.sub(r"__(TITLE|INTRO|PAGES|SECTIONS|NS|NAME|RID|RCSS|SECT|ROOTFS|SKILLS|ALWAYS|SKILL_CONTEXT|FULL_CHECK_COMPAT|CATALOGS|LOCALE|REPORT_LANG|DOCX|PPTX)__", lambda m: values[m.group(1)],
                       TEMPLATE.read_text(encoding="utf-8"))
    page_html = page_html.replace("__CSP__", page_csp(page_html), 1)
    write_private(out / "index.html", page_html)
    rr, rs = report.resolve(), (script.resolve() if script else None)
    manifest = {
        "app": APP, "kind": "deck", "reviewId": rid, "reviewDir": str(out.resolve()), "title": final_title, "name": review_name,
        "namespace": ns,
        "builtAt": datetime.now().astimezone().isoformat(timespec="seconds"),
        # 相对审阅目录的路径优先：整棵目录树被移动或复制后仍指向同一处的源文件
        "sources": {"report": os.path.relpath(rr, out.resolve()), "script": os.path.relpath(rs, out.resolve()) if rs else None},
        "sourcesAbs": {"report": str(rr), "script": str(rs) if rs else None},
        "build": {"title": title, "name": (name or "").strip() or None, "sections": sections_arg, "noSections": no_sections, "lenient": lenient,
                  "assetRoot": str(asset_root.resolve()) if asset_root else None,
                  "skillsFrom": [str(x.expanduser().absolute()) for x in skills_from] if skills_from is not None else None,
                  "noScanSkills": no_scan_skills},
        "sections": sections,
        "pages": [{"n": p["n"], "key": p["key"], "stable": p["stable"], "title": html.unescape(p["title"]),
                   "section": next((s[0] for s in sections if s[1] <= p["n"] <= s[2]), ""),
                   **({"w": p["w"], "h": p["h"], "orient": p["orient"],
                       "label": p["label"]} if "w" in p and "label" in p else
                      {"w": p["w"], "h": p["h"], "orient": p["orient"]} if "w" in p else {})}
                  for p in pages],
    }
    if docx_meta is not None:
        manifest["format"] = {"report": "docx", "version": docx_review.FORMAT_VERSION}
        manifest["docx"] = docx_meta
    if pptx_meta is not None:
        manifest["format"] = {"report": "pptx", "version": pptx_report.FORMAT_VERSION}
        manifest["pptx"] = pptx_meta
    if locale_choice is not None:
        manifest["locale"] = locale_choice
        manifest["build"]["locale"] = locale_choice
    write_private(out / "review.json", json.dumps(manifest, ensure_ascii=False, indent=2) + "\n")
    # 已有批注：按页面键校正页码（重排、增删页之后批注跟着原来的页走）
    key_count: dict[str, int] = {}
    for pg in pages:
        key_count[pg["key"]] = key_count.get(pg["key"], 0) + 1
    dup = {k for k, c in key_count.items() if c > 1}
    if dup:
        assets.warn(diag(locale_choice, "build.key.duplicate", keys=sorted(dup)[:5]))
    unstable = [pg["n"] for pg in pages if not pg["stable"]]
    if unstable:
        assets.warn(diag(locale_choice, "build.key.unstable", n=len(unstable), pages=unstable[:5]))
    remap = {"moved": [], "orphans": [], "keyed": 0}
    if (out / "notes.json").exists():
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        import notes_store
        # DOCX：带段落定位的批注先按段落找回现在所在的页（页首会随 Word 重排换段，页面键管不住）
        docx_ctx = None if docx_meta is None else {
            "paragraphs": docx_paragraphs, "fingerprint": docx_meta["fingerprint"],
            "keys": {pg["n"]: pg["key"] for pg in pages if pg["key"] not in dup}}
        remap = notes_store.remap_pages(out, old_keys, {pg["key"]: pg["n"] for pg in pages if pg["stable"]},
                                        {pg["n"]: html.unescape(pg["title"]) for pg in pages}, dup, docx=docx_ctx)
        for mv in remap["moved"]:
            assets.warn(diag(locale_choice, "build.note.moved", id=mv["id"], pageKey=mv["key"], old=mv["from"], new=mv["to"]))
        for o in remap["orphans"]:
            if o.get("reason"):   # 按段落定位没找到：原因在下面的 DOCX 诊断里说
                continue
            assets.warn(diag(locale_choice, "build.note.orphan", id=o["id"], page=o["page"], pageKey=o["key"]))
        for x in remap.get("docx", []):
            if x["status"] != "ok":
                assets.warn(diag(locale_choice, f"build.note.docx.{x['status']}", id=x["id"], page=x["page"]))
                continue
            if x["why"]:
                assets.warn(diag(locale_choice, f"build.note.docx.{x['why']}", id=x["id"], page=x["page"]))
            if x["changed"]:
                assets.warn(diag(locale_choice, "build.note.docx.changed", id=x["id"], page=x["page"]))
    register_review(rid, out, review_name, assets)
    if assets.total > 40 * 1024 * 1024:
        assets.warn(diag(locale_choice, "build.asset.total", size=assets.total // 1024 // 1024))
    return {"pages": len(pages), "sections": len(sections), "seconds": sum(p["sec"] for p in pages),
            "title": final_title, "name": review_name, "warnings": assets.warnings, "reviewId": rid, "remap": remap,
            "locale": locale_choice, "docx": docx_meta}


def docx_view(meta: dict | None) -> dict | None:
    """对照页用的 DOCX 摘要：分页可信度与有限制的结构（文案由页面按当前语言取词典）。"""
    if meta is None:
        return None
    import docx_review
    return {"v": docx_review.FORMAT_VERSION, "pagination": meta["pagination"], "appPages": meta.get("appPages"),
            # 批注定位：页面写入的定位字段版本，以及写下定位时的源文件指纹
            "anchor": docx_review.ANCHOR_VERSION, "fp": meta.get("fingerprint") or "",
            "application": meta.get("application") or "",
            "diags": [{k: d[k] for k in ("code", "count", "pages", "display", "writeback")}
                      for d in meta["diagnostics"] if d["code"] not in docx_review.INFO_KINDS]}


def register_review(rid: str, out: Path, review_name: str, assets: Assets) -> None:
    """登记到本机审阅台账；第一次登记时台账里已有同名的别的审阅就提醒（不拦）。台账出错只警告。"""
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import ledger
    try:
        known = ledger.get(rid) is not None
    except (ledger.LedgerError, OSError):
        known = True
    warn = ledger.try_register(rid, out)
    if warn:
        assets.warn(warn)
        return
    if known:   # 重名只在第一次登记时提醒，之后每次重建不再刷屏
        return
    try:
        same = [(k, e["dir"]) for k, e in ledger.entries().items()
                if k != rid and ledger.entry_state(k, e) == "ok"
                and ledger.review_name(ledger.read_manifest(Path(e["dir"]))) == review_name]
    except (ledger.LedgerError, OSError):
        return
    for k, d in same[:3]:
        assets.warn(diag(assets.locale, "build.review.sameName", name=review_name, id=k[:8], path=d))


def main(argv: list[str] | None = None) -> int:
    import i18n
    import ledger
    argv = sys.argv[1:] if argv is None else argv
    pre = argparse.ArgumentParser(add_help=False)
    pre.add_argument("--locale")
    pre.add_argument("--rebuild", type=Path)
    pre.add_argument("--out", type=Path)
    requested, _ = pre.parse_known_args(argv)
    previous = ledger.read_manifest(requested.rebuild or requested.out) if requested.rebuild or requested.out else {}
    try:
        language = i18n.cli_locale(requested.locale, stored=previous.get("locale"))
    except i18n.LocaleError as exc:
        language = i18n.normalize_locale(os.environ.get("AE_LOCALE")) or i18n.DEFAULT_LOCALE
        print(diag(language, "cli.notes.invalidLocale", name=exc.params.get("locale", "")), file=sys.stderr)
        return 2
    t = i18n.Translator(language)
    ledger.set_locale(language)
    ap = argparse.ArgumentParser(prog="build_viewer.py", description=t("build.cli.description"))
    ap.add_argument("--rebuild", type=Path, metavar=t("build.cli.reviewDir"), help=t("build.cli.rebuild"))
    ap.add_argument("--report", "--html", dest="report", type=Path, help=t("build.cli.report"))
    ap.add_argument("--script", type=Path, help=t("build.cli.script"))
    ap.add_argument("--out", type=Path, help=t("build.cli.out"))
    ap.add_argument("--title", help=t("build.cli.title"))
    ap.add_argument("--name", help=t("build.cli.name"))
    ap.add_argument("--sections", help=t("build.cli.sections"))
    ap.add_argument("--no-sections", action="store_true", help=t("build.cli.noSections"))
    ap.add_argument("--lenient", action="store_true", help=t("build.cli.lenient"))
    ap.add_argument("--asset-root", type=Path, help=t("build.cli.assetRoot"))
    ap.add_argument("--skills-from", action="extend", nargs="+", type=Path, metavar=t("build.cli.directory"), help=t("build.cli.skillsFrom"))
    ap.add_argument("--no-scan-skills", action="store_true", help=t("build.cli.noScan"))
    ap.add_argument("--locale", help=t("build.cli.locale"))
    a = ap.parse_args(argv)
    if a.rebuild:
        m = ledger.read_manifest(a.rebuild)
        if not m:
            ap.error(t("build.cli.missingManifest", path=a.rebuild / "review.json"))
        src, opt = resolve_sources(a.rebuild, m, locale=language), m.get("build") or {}
        a.out, a.report, a.script = a.rebuild, src["report"], src["script"]
        a.title = a.title or opt.get("title")
        a.name = a.name or opt.get("name")
        a.sections = a.sections or opt.get("sections")
        a.no_sections = a.no_sections or bool(opt.get("noSections"))
        a.lenient = a.lenient or bool(opt.get("lenient"))
        a.asset_root = a.asset_root or (Path(opt["assetRoot"]) if opt.get("assetRoot") else None)
        explicit_sources = a.skills_from is not None
        if not explicit_sources and opt.get("skillsFrom") is not None:
            a.skills_from = [Path(x) for x in opt["skillsFrom"]]
        if not explicit_sources:
            a.no_scan_skills = a.no_scan_skills or bool(opt.get("noScanSkills"))
    elif not (a.report and a.out):
        ap.error(t("build.cli.missingArgs"))
    r = build(a.report, a.script, a.out, a.title, a.sections, a.no_sections, a.lenient, a.asset_root,
              name=a.name, skills_from=a.skills_from, no_scan_skills=a.no_scan_skills, locale=a.locale)
    for w in r["warnings"]:
        print(t("cli.notes.warning", reason=w), file=sys.stderr)
    mins = r["seconds"] // 60
    print(t("build.cli.success", path=a.out / "index.html", pages=r["pages"], sections=r["sections"])
          + (t("build.cli.duration", minutes=mins) if r["seconds"] else "")
          + "\n" + t("build.cli.identity", name=r["name"], id=r["reviewId"])
          + ("\n" + t("build.cli.language", locale=r["locale"]) if r.get("locale") else "")
          + ("\n" + t("build.cli.docx", pagination=t(f"docx.pagination.{r['docx']['pagination']}"))
             if r.get("docx") else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
