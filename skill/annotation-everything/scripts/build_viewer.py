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
- 讲稿 Markdown（可选）：每页以「### 第 N 页｜标题」开头，字段 **时长** / **讲法** / **过渡** / **可能被问**
  （「可能被问」逐行写「问：……答：……」）。首个「### 第」之前的内容作为「说明」浮层。

用法：
  python3 build_viewer.py --report 报告.html [--script 讲稿.md] --out 审阅目录 [--title 标题] [--sections ...]
  python3 build_viewer.py --rebuild 审阅目录          # 按 review.json 记录的源文件与参数重建
输出 <out>/index.html 与 <out>/review.json（审阅标识、页码、标题、源文件路径）；不会动已有的 notes.json。
只用 Python 标准库。
"""
from __future__ import annotations

import argparse
import base64
import hashlib
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

PAGE_HEAD = re.compile(r"^###\s*第\s*(\d+)\s*页\s*[｜|]\s*(.*)$", re.M)
FIELD = re.compile(r"^\*\*(时长|讲法|过渡|可能被问)\*\*\s*[：:]", re.M)
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


def seconds(dur: str) -> int:
    total = 0.0
    for v, unit in re.findall(r"([\d.]+)\s*(分钟|分|秒|min|s)", dur):
        total += float(v) * (60 if unit in ("分钟", "分", "min") else 1)
    return round(total)


def parse_script(script: str) -> tuple[str, str, list[dict]]:
    """返回 (H1 标题, 说明浮层 HTML, 逐页讲稿)。"""
    heads = list(PAGE_HEAD.finditer(script))
    intro = script[: heads[0].start()] if heads else script
    h1 = next((ln[2:].strip() for ln in intro.splitlines() if ln.startswith("# ")), "")
    pages = []
    for i, h in enumerate(heads):
        body = script[h.end(): heads[i + 1].start() if i + 1 < len(heads) else len(script)]
        fields: dict[str, str] = {}
        fs = list(FIELD.finditer(body))
        for j, f in enumerate(fs):
            fields[f.group(1)] = body[f.end(): fs[j + 1].start() if j + 1 < len(fs) else len(body)].strip()
        talk_src = fields.get("讲法", "") if fs else body.strip()
        talk = "".join(f"<p>{inline(ln)}</p>" for ln in talk_src.splitlines() if ln.strip() and ln.strip() != "---")
        qa = []
        for ln in fields.get("可能被问", "").splitlines():
            ln = ln.strip().lstrip("-").strip()
            if not ln:
                continue
            q, _, a = ln.partition("答：")
            qa.append({"q": inline(q.removeprefix("问：")), "a": inline(a)})
        dur = fields.get("时长", "").strip()
        pages.append({"n": int(h.group(1)), "title": esc(h.group(2)), "dur": esc(dur), "sec": seconds(dur),
                      "talk": talk, "next": inline(fields.get("过渡", "")), "qa": qa})
    return h1, md_block(intro), pages


# ---------- 报告解析 ----------
def find_pages(doc: str) -> list[dict]:
    """按嵌套层级找出 class 含 page 且带 data-page 的 section（注释、脚本、样式里的标签不算）。"""
    masked = MASKED.sub(lambda m: " " * len(m.group(0)), doc)
    stack: list[tuple[int, dict[str, str], bool]] = []
    pages: list[dict] = []
    for m in SECTION_TAG.finditer(masked):
        if not m.group(1):
            attrs = attrs_of(doc[m.start():m.end()])
            is_page = "page" in attrs.get("class", "").split() and "data-page" in attrs
            if is_page and any(p for _s, _a, p in stack):
                raise SystemExit(f"报告结构不对：第 {attrs['data-page']} 页的 section.page 嵌在另一页里面")
            stack.append((m.start(), attrs, is_page))
        elif stack:
            start, attrs, is_page = stack.pop()
            if is_page:
                pages.append({"attrs": attrs, "html": doc[start:m.end()]})
    unclosed = [a.get("data-page") for _s, a, p in stack if p]
    if unclosed:
        raise SystemExit(f"报告结构不对：第 {unclosed} 页的 <section> 没有闭合")
    out, seen = [], set()
    for p in pages:
        a = p["attrs"]
        try:
            n = int(a["data-page"].strip())
        except ValueError:
            raise SystemExit(f"报告结构不对：data-page={a['data-page']!r} 不是整数页码") from None
        if n in seen:
            raise SystemExit(f"报告结构不对：页码 {n} 重复")
        seen.add(n)
        key = a.get("data-key") or a.get("id", "").removeprefix("page-") or f"p{n}"
        hm = re.search(r"<h[1-3][^>]*>(.*?)</h[1-3]>", p["html"], re.S)
        out.append({"n": n, "key": key, "html": p["html"], "title": strip_tags(hm.group(1)) if hm else key})
    return out


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


def parse_sections_arg(arg: str) -> list[list]:
    out = []
    for part in re.split(r"[,，]", arg):
        part = part.strip()
        if not part:
            continue
        m = re.match(r"^(.+?)[:：]\s*(\d+)\s*(?:[-–~]\s*(\d+))?$", part)
        if not m:
            raise SystemExit(f"--sections 格式不对：{part!r}，应为 名称:起-止，用逗号分隔")
        a = int(m.group(2))
        out.append([m.group(1).strip(), a, int(m.group(3) or a)])
    return out


# ---------- 资源内联 ----------
class Assets:
    """把本地相对路径资源换成 data URI；外链与找不到的文件记警告。"""

    def __init__(self) -> None:
        self.cache: dict[Path, str] = {}
        self.warnings: list[str] = []
        self.total = 0

    def uri(self, ref: str, base: Path) -> str | None:
        r = ref.strip()
        low = r.lower()
        if not r or low.startswith(("data:", "#", "mailto:", "javascript:", "blob:", "about:")):
            return None
        if low.startswith(("http:", "https:", "//")):
            self.warn(f"外链资源不会加载（对照页禁止联网）：{r[:120]}")
            return None
        path = (base / unquote(r.split("#", 1)[0].split("?", 1)[0])).resolve()
        if path in self.cache:
            return self.cache[path]
        if not path.is_file():
            self.warn(f"找不到本地资源：{r[:120]}（相对 {base}）")
            return None
        size = path.stat().st_size
        if size > MAX_ASSET:
            self.warn(f"资源超过 {MAX_ASSET // 1024 // 1024} MB，未内联：{path}")
            return None
        mime = EXTRA_MIME.get(path.suffix.lower()) or mimetypes.guess_type(path.name)[0] or "application/octet-stream"
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


def _rewrite_selector(prelude: str) -> str:
    def rep(m: re.Match) -> str:
        tag, suf = m.group(1), m.group(2)
        if tag == "body":
            return "#stage" + suf
        return f":host({suf})" if suf else ":host"
    return SEL_ROOT.sub(rep, prelude)


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
    for m in re.finditer(r"<link\b[^>]*>", doc, re.I):
        a = attrs_of(m.group(0))
        if "stylesheet" not in a.get("rel", "").lower().split() or not a.get("href"):
            continue
        href = a["href"]
        if href.lower().startswith(("http:", "https:", "//")):
            assets.warn(f"外链样式表不会加载（对照页禁止联网）：{href[:120]}")
            continue
        f = (base / unquote(href.split("?", 1)[0])).resolve()
        if f.is_file():
            css_parts.append(assets.css(f.read_text(encoding="utf-8", errors="replace"), f.parent))
        else:
            assets.warn(f"找不到样式表：{href}")
    head = MASKED.sub(lambda m: m.group(0) if m.group(0).lower().startswith("<style") else "", doc)
    css_parts += [assets.css(c, base) for c in re.findall(r"<style[^>]*>(.*?)</style\s*>", head, re.S | re.I)]
    sects = find_pages(doc)
    for s in sects:
        s["html"] = assets.html(s["html"], base)
    return (strip_tags(title_m.group(1)) if title_m else ""), shadow_css("\n".join(css_parts)), sects


# ---------- 输出 ----------
def _js(obj: object) -> str:
    """嵌入 <script> 的 JSON：转义 </ 与 <!-- 防止提前结束脚本。"""
    return json.dumps(obj, ensure_ascii=False).replace("</", "<\\/").replace("<!--", "<\\!--")


def page_csp(page_html: str) -> str:
    """与 serve.py 的响应头一致：只放行页面自身的内联脚本（按 sha256）。用于 file:// 打开时。"""
    hashes = " ".join("'sha256-" + base64.b64encode(hashlib.sha256(s.encode("utf-8")).digest()).decode() + "'"
                      for s in re.findall(r"<script>(.*?)</script>", page_html, re.S))
    return (f"default-src 'none'; script-src {hashes}; style-src 'unsafe-inline'; img-src data: blob:; "
            "font-src data:; media-src data: blob:; connect-src 'self'; base-uri 'none'; form-action 'none'; "
            "object-src 'none'")


def write_private(path: Path, text: str) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(text)
    os.chmod(path, 0o600)


def read_manifest(review_dir: Path) -> dict:
    try:
        m = json.loads((review_dir / "review.json").read_text(encoding="utf-8"))
        return m if isinstance(m, dict) else {}
    except (OSError, ValueError):
        return {}


def resolve_sources(review_dir: Path, manifest: dict | None = None) -> dict[str, Path | None]:
    """按 review.json 找源文件：先用相对审阅目录的路径（目录树整体复制后仍指向副本），再用绝对路径。"""
    m = manifest if manifest is not None else read_manifest(review_dir)
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
            raise SystemExit(f"找不到{'报告' if k == 'report' else '讲稿'}源文件（试过：{tried}）。"
                             "源文件改名或移动了的话，用 --report/--script/--out 重新生成。")
        out[k] = hit
    return out


def build(report: Path, script: Path | None, out: Path, title: str | None = None,
          sections_arg: str | None = None, no_sections: bool = False, lenient: bool = False) -> dict:
    doc = report.read_text(encoding="utf-8")
    assets = Assets()
    rtitle, rcss, sects = report_parts(doc, report, assets)
    if not sects:
        raise SystemExit(f"{report} 里没找到 <section class=\"page\" data-page=\"N\"> 逐页结构")
    h1, intro, spages = ("", "", [])
    if script is not None:
        h1, intro, spages = parse_script(script.read_text(encoding="utf-8"))
        if not spages:
            raise SystemExit(f"{script} 里没找到「### 第 N 页｜标题」页头")
    rnums = [s["n"] for s in sects]
    if script is not None:
        snums = [p["n"] for p in spages]
        if snums != rnums:
            only_r = sorted(set(rnums) - set(snums))
            only_s = sorted(set(snums) - set(rnums))
            msg = (f"讲稿页码与报告页序不一致：报告 {len(rnums)} 页，讲稿 {len(snums)} 页"
                   + (f"；报告有、讲稿缺：{only_r}" if only_r else "")
                   + (f"；讲稿有、报告缺：{only_s}" if only_s else "")
                   + ("；页序不同" if not only_r and not only_s else ""))
            if not lenient:
                raise SystemExit(msg + "。修正后重跑，或加 --lenient 以报告页序为准（缺讲稿的页显示为空）。")
            print("警告：" + msg, file=sys.stderr)
    by_n = {p["n"]: p for p in spages}
    pages = []
    for s in sects:
        p = by_n.get(s["n"]) or {"n": s["n"], "title": esc(s["title"]), "dur": "", "sec": 0, "talk": "", "next": "", "qa": []}
        pages.append({**p, "key": s["key"]})
    if no_sections:
        sections: list[list] = []
    elif sections_arg:
        sections = parse_sections_arg(sections_arg)
    else:
        sections = infer_sections(doc, sects)
    final_title = title or rtitle or h1 or report.stem
    if not intro:
        intro = f"<h2>{esc(final_title)}</h2><p>左侧报告、右侧讲稿与批注。选中文字可作为引用，点「在报告上标点」可标位置。</p>"
    out.mkdir(parents=True, exist_ok=True)
    old = read_manifest(out)
    # 审阅标识：页面据此确认连上的是这份审阅的服务；重建时沿用
    rid = old.get("reviewId") if re.fullmatch(r"[0-9a-f]{16}", str(old.get("reviewId", ""))) else secrets.token_hex(8)
    ns = re.sub(r"[^\w.-]+", "_", out.resolve().name)
    values = {"TITLE": html.escape(final_title), "INTRO": intro, "PAGES": _js(pages), "SECTIONS": _js(sections),
              "NS": _js(ns), "RID": _js(rid), "RCSS": _js(rcss), "SECT": _js([s["html"] for s in sects])}
    # 一次性替换，避免内容里恰好出现占位符文字时被二次替换；CSP 要等脚本定稿后再算哈希
    page_html = re.sub(r"__(TITLE|INTRO|PAGES|SECTIONS|NS|RID|RCSS|SECT)__", lambda m: values[m.group(1)],
                       TEMPLATE.read_text(encoding="utf-8"))
    page_html = page_html.replace("__CSP__", page_csp(page_html), 1)
    write_private(out / "index.html", page_html)
    rr, rs = report.resolve(), (script.resolve() if script else None)
    manifest = {
        "app": APP, "kind": "deck", "reviewId": rid, "title": final_title, "namespace": ns,
        "builtAt": datetime.now().astimezone().isoformat(timespec="seconds"),
        # 相对审阅目录的路径优先：整棵目录树被移动或复制后仍指向同一处的源文件
        "sources": {"report": os.path.relpath(rr, out.resolve()), "script": os.path.relpath(rs, out.resolve()) if rs else None},
        "sourcesAbs": {"report": str(rr), "script": str(rs) if rs else None},
        "build": {"title": title, "sections": sections_arg, "noSections": no_sections, "lenient": lenient},
        "sections": sections,
        "pages": [{"n": p["n"], "key": p["key"], "title": html.unescape(p["title"]),
                   "section": next((s[0] for s in sections if s[1] <= p["n"] <= s[2]), "")} for p in pages],
    }
    write_private(out / "review.json", json.dumps(manifest, ensure_ascii=False, indent=2) + "\n")
    if assets.total > 40 * 1024 * 1024:
        assets.warn(f"内联资源共 {assets.total // 1024 // 1024} MB，页面较大，打开可能偏慢")
    return {"pages": len(pages), "sections": len(sections), "seconds": sum(p["sec"] for p in pages),
            "title": final_title, "warnings": assets.warnings, "reviewId": rid}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="build_viewer.py", description="生成报告 + 讲稿对照审阅页")
    ap.add_argument("--rebuild", type=Path, metavar="审阅目录", help="按该目录 review.json 记录的源文件与参数重建")
    ap.add_argument("--report", "--html", dest="report", type=Path, help="报告 HTML（逐页 section.page）")
    ap.add_argument("--script", type=Path, help="讲稿 Markdown（可选）")
    ap.add_argument("--out", type=Path, help="审阅目录：写 index.html 与 review.json，批注也存这里")
    ap.add_argument("--title", help="页面标题；默认取报告 <title>，其次讲稿一级标题")
    ap.add_argument("--sections", help='板块导航，如 "开篇:1-6,主线一:7-21"；默认从报告目录推断')
    ap.add_argument("--no-sections", action="store_true", help="不显示板块导航")
    ap.add_argument("--lenient", action="store_true", help="讲稿与报告页码不一致时只警告，以报告页序为准")
    a = ap.parse_args(argv)
    if a.rebuild:
        m = read_manifest(a.rebuild)
        if not m:
            ap.error(f"读不到 {a.rebuild / 'review.json'}，不是本工具生成的审阅目录？")
        src, opt = resolve_sources(a.rebuild, m), m.get("build") or {}
        a.out, a.report, a.script = a.rebuild, src["report"], src["script"]
        a.title = a.title or opt.get("title")
        a.sections = a.sections or opt.get("sections")
        a.no_sections = a.no_sections or bool(opt.get("noSections"))
        a.lenient = a.lenient or bool(opt.get("lenient"))
    elif not (a.report and a.out):
        ap.error("需要 --report 与 --out（或用 --rebuild 审阅目录）")
    r = build(a.report, a.script, a.out, a.title, a.sections, a.no_sections, a.lenient)
    for w in r["warnings"]:
        print("警告：" + w, file=sys.stderr)
    mins = r["seconds"] // 60
    print(f"wrote {a.out / 'index.html'}：{r['pages']} 页，板块 {r['sections']} 个"
          + (f"，讲稿合计约 {mins} 分钟" if r["seconds"] else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
