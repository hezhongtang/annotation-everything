#!/usr/bin/env python3
"""DOCX 直接进入审阅：把 Word 文档按原分页渲染成逐页 HTML，供 build_viewer.py 生成对照页。

不经过手工 HTML 重排：
- 分页依据 Word 保存时写下的渲染分页（w:lastRenderedPageBreak）、硬分页符、段前分页与分节符。
  分页可信度分三级：verified（有渲染分页且与 docProps/app.xml 的页数一致）、unverified（有渲染分页
  但页数对不上或没有页数）、estimated（没有渲染分页，只按显式分页切，不是 Word 页码）。
- 页面尺寸、方向与页边距取自各节 w:sectPr（A4 纵向就是 794×1123 px），不改成横向舞台；
  内容比页高时页面向下延伸，不截断。
- 段落、表格（合并单元格、底纹、边框、重复表头、跨页行）、页眉页脚（PAGE / NUMPAGES / SECTIONPAGES
  按实际页码与 pgNumType）、高亮、删除线、字形、列表编号与图片按原样或近似画出；文字留在 DOM 里，可选中。
- 正文段落带 data-docx-p（document.xml 里 w:p 的文档先序编号）、data-docx-pid（w14:paraId）与
  data-docx-d（段落文本摘要）；跨页段落的续片另带 data-docx-o（本片在段落里的起始字符偏移）。
  文字运行带 data-docx-r（运行在所属段落里的序号）。批注定位字段（ANCHOR_VERSION）据此写入，
  重建时由 relocate() 按段落找回批注现在所在的页。
- Word 原生修订、批注、文本框、域、脚注、公式、内容控件等结构按支持矩阵（SUPPORT）显示并记诊断，
  涉及它们的段落标为只读（data-docx-ro），不静默丢弃，也不假装能回写。

DOCX 多来自他人，按不可信输入处理：颜色只收 6 位十六进制，字体名过滤掉能跳出 CSS 的字符，
XML 部件限大小并拒绝 DTD，数值不合法时按缺省值处理。只用 Python 标准库。
"""
from __future__ import annotations

import base64
import hashlib
import html
import math
import posixpath
import re
import zipfile
import zlib
from contextlib import contextmanager
from html.parser import HTMLParser
from pathlib import Path
from xml.etree import ElementTree as ET

FORMAT_VERSION = 1           # review.json 的 format.version：DOCX 审阅元数据的版本
ANCHOR_VERSION = 1           # 批注 docx 定位字段的版本（note["docx"]["v"]）
W_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
STRICT_NS = "http://purl.oclc.org/ooxml/wordprocessingml/main"
W = "{%s}" % W_NS
W14 = "{http://schemas.microsoft.com/office/word/2010/wordml}"
R = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}"
WP = "{http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing}"
A = "{http://schemas.openxmlformats.org/drawingml/2006/main}"
MC = "{http://schemas.openxmlformats.org/markup-compatibility/2006}"
V = "{urn:schemas-microsoft-com:vml}"
M = "{http://schemas.openxmlformats.org/officeDocument/2006/math}"
REL = "{http://schemas.openxmlformats.org/package/2006/relationships}"
EP = "{http://schemas.openxmlformats.org/officeDocument/2006/extended-properties}"
OLE_MAGIC = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"
MAX_MEDIA = 10 * 1024 * 1024
MAX_PART = 64 * 1024 * 1024  # 单个 XML 部件解压后的上限
MAX_COUNTER = 9999           # 列表编号起始值与计数的上限
TWIP_PX = 96 / 1440          # 1 twip = 1/1440 英寸
EMU_PX = 96 / 914400         # 1 EMU = 1/914400 英寸
HEX6 = re.compile(r"[0-9A-Fa-f]{6}")
PARA_ID = re.compile(r"[0-9A-Fa-f]{1,8}")
BREAK = "\x00BREAK\x00"
DIAG_MARK = re.compile(r"\x00DOCX_DIAG:([A-Za-z]+)\x00")
HIGHLIGHT = {"yellow": "#ffff00", "green": "#00ff00", "cyan": "#00ffff", "magenta": "#ff00ff", "blue": "#0000ff",
             "red": "#ff0000", "darkBlue": "#00008b", "darkCyan": "#008b8b", "darkGreen": "#006400",
             "darkMagenta": "#800080", "darkRed": "#8b0000", "darkYellow": "#808000", "darkGray": "#a9a9a9",
             "lightGray": "#d3d3d3", "black": "#000000", "white": "#ffffff"}
JC = {"center": "center", "right": "right", "end": "right", "both": "justify", "distribute": "justify",
      "left": "left", "start": "left"}
CN_DIGITS = "零一二三四五六七八九"
BULLET_MAP = {"": "•", "": "▪", "": "❖", "": "➢", "": "✓", "o": "◦", "": "•"}
NUM_FORMATS = ("decimal", "lowerLetter", "upperLetter", "lowerRoman", "upperRoman", "chineseCounting",
               "chineseCountingThousand", "ideographTraditional", "decimalEnclosedCircle", "decimalZero",
               "none", "bullet")

# 支持矩阵：结构 → (显示, 回写)。显示：exact 原样 / approx 近似 / placeholder 占位 / hidden 不显示；
# 回写：yes 可以 / no 所在段落只读 / na 不适用（不是可改的正文）。诊断、对照页提示与文档共用这一份。
SUPPORT = {
    "estimatedPages": ("approx", "na"), "revision": ("approx", "no"), "formatRevision": ("approx", "no"),
    "comment": ("placeholder", "yes"), "textbox": ("approx", "no"), "field": ("approx", "no"),
    "footnote": ("placeholder", "na"), "math": ("approx", "no"), "contentControl": ("approx", "no"),
    "hidden": ("hidden", "no"), "object": ("placeholder", "no"), "mediaTooLarge": ("placeholder", "na"),
    "floating": ("approx", "na"), "columns": ("approx", "na"), "rowSplit": ("approx", "na"),
    "numbering": ("approx", "na"), "unknown": ("hidden", "no"),
    "table": ("approx", "yes"), "list": ("approx", "yes"), "image": ("approx", "na"),
    "header": ("approx", "na"), "footer": ("approx", "na"),
    "text": ("exact", "yes"), "highlight": ("exact", "yes"), "strike": ("exact", "yes"),
}
DIAG_ORDER = tuple(SUPPORT)
# 读到就让所在段落只读的结构：回写会破坏它们，或者改了也不能保证 Word 里一致
READONLY_KINDS = frozenset(k for k, (_d, w) in SUPPORT.items() if w == "no")
# 只用来说明「有这种结构、显示正常」，不算限制：命令行不逐条提示
INFO_KINDS = frozenset({"table", "list", "image", "header", "footer", "text", "highlight", "strike"})


class DocxError(ValueError):
    """不是能读的 DOCX。code 给本地化诊断用：unreadable / notZip / legacyDoc / encrypted / strict /
    noDocument / badXml / tooLarge / dtd / internal。"""

    def __init__(self, code: str, detail: str = ""):
        super().__init__(f"{code}: {detail}" if detail else code)
        self.code, self.detail = code, detail


def is_word_like(path: Path) -> bool:
    """看起来是 Word 文件（含旧版 .doc）：生成器据此走 DOCX 分支，不按 UTF-8 文本读。"""
    return Path(path).suffix.lower() in (".docx", ".docm", ".dotx", ".doc", ".dot")


def _num(v: str | None, default: float = 0.0, lo: float = -1e7, hi: float = 1e7) -> float:
    """XML 里的数值：读不出、非有限值都按缺省值，超出范围截到边界。"""
    try:
        x = float(v) if v not in (None, "") else default
    except (TypeError, ValueError):
        return default
    if not math.isfinite(x):
        return default
    return min(max(x, lo), hi)


def _int(v: str | None, default: int = 0, lo: int = -10_000_000, hi: int = 10_000_000) -> int:
    return int(_num(v, default, lo, hi))


def _on(el: ET.Element | None) -> bool | None:
    """开关型属性（w:b、w:strike……）：没写返回 None，写了按 w:val 判断。"""
    if el is None:
        return None
    return el.get(W + "val", "true").lower() not in ("0", "false", "off", "none")


def _esc(s: str) -> str:
    return html.escape(s, quote=True)


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def hexcolor(v: str | None) -> str | None:
    """颜色只收 6 位十六进制（防止属性值跳出 style 注入样式）。"""
    return f"#{v}" if v and HEX6.fullmatch(v) else None


def font_name(v: str) -> str:
    return re.sub(r"[^\w \-.\u0080-￿]", "", v)[:64].strip()


def para_id(p: ET.Element) -> str | None:
    v = p.get(W14 + "paraId")
    return v.upper() if v and PARA_ID.fullmatch(v) else None


def own_runs(p: ET.Element):
    """段落自己的文字运行（文档先序），不含嵌在文本框里的下一级段落的运行。运行归属于离它最近的祖先 w:p。"""
    stack = list(reversed(list(p)))
    while stack:
        el = stack.pop()
        if el.tag == W + "p":
            continue
        if el.tag == W + "r":
            yield el
        stack.extend(reversed(list(el)))


def parse_xml(data: bytes, name: str) -> ET.Element:
    if len(data) > MAX_PART:
        raise DocxError("tooLarge", name)

    class NoDTDBuilder(ET.TreeBuilder):
        def doctype(self, _name, _pubid, _system):
            # 由 XML 解析器识别编码，并在解析内部子集、展开实体之前拒绝 DTD。
            raise DocxError("dtd", name)

    try:
        return ET.fromstring(data, parser=ET.XMLParser(target=NoDTDBuilder()))
    except ET.ParseError as exc:
        raise DocxError("badXml", f"{name}: {exc}") from None


class Package:
    """DOCX 压缩包：主文档、样式、编号、设置、关系、页眉页脚、批注、媒体与页数。读完即关闭 zip 句柄。"""

    def __init__(self, path: Path):
        self.path = Path(path)
        try:
            with self.path.open("rb") as fh:
                head = fh.read(8)
        except OSError as exc:
            raise DocxError("unreadable", str(exc)) from None
        if head == OLE_MAGIC:
            # OLE 复合文档：旧版 .doc，或者加密的 DOCX（外面套了一层 EncryptedPackage）
            raw = self.path.read_bytes()
            raise DocxError("encrypted" if "EncryptedPackage".encode("utf-16-le") in raw else "legacyDoc")
        try:
            sha = hashlib.sha256()
            with self.path.open("rb") as fh:
                for chunk in iter(lambda: fh.read(1 << 20), b""):
                    sha.update(chunk)
        except OSError as exc:
            raise DocxError("unreadable", str(exc)) from None
        self.fingerprint = sha.hexdigest()   # 源文件指纹：批注据此判断是不是对着当前版本写的
        try:
            zf = zipfile.ZipFile(self.path)
        except (zipfile.BadZipFile, OSError) as exc:
            raise DocxError("notZip", str(exc)) from None
        with zf:
            self._zip = zf
            self.infos = {i.filename: i for i in zf.infolist()}
            self.main = self._main_part()
            self.root = self.xml(self.main)
            if self.root.tag.startswith("{%s}" % STRICT_NS):
                raise DocxError("strict")
            if self.root.find(W + "body") is None:
                raise DocxError("noDocument", self.main)
            self.rels = self.relations(self.main)
            base = posixpath.dirname(self.main)
            self.styles = self.xml(posixpath.join(base, "styles.xml"), optional=True)
            self.numbering = self.xml(posixpath.join(base, "numbering.xml"), optional=True)
            self.settings = self.xml(posixpath.join(base, "settings.xml"), optional=True)
            self.app = self.xml("docProps/app.xml", optional=True)
            self.core = self.xml("docProps/core.xml", optional=True)
            self.parts: dict[str, tuple[ET.Element | None, dict]] = {}
            self.comments: dict[str, tuple[str, str]] = {}
            for rel in self.rels.values():
                if rel["external"]:
                    continue
                if rel["type"].endswith(("/header", "/footer")):
                    self.parts[rel["target"]] = (self.xml(rel["target"], optional=True), self.relations(rel["target"]))
                elif rel["type"].endswith("/comments"):
                    croot = self.xml(rel["target"], optional=True)
                    for c in (croot.iter(W + "comment") if croot is not None else ()):
                        text = " ".join("".join(t.text or "" for t in p.iter(W + "t")) for p in c.iter(W + "p")).strip()
                        self.comments[c.get(W + "id", "")] = (c.get(W + "author", "")[:80], text[:500])
            self.media: dict[str, bytes | None] = {}
            for name, info in self.infos.items():
                if "/media/" in name:
                    self.media[name] = self._read(name) if info.file_size <= MAX_MEDIA else None
        self._zip = None

    def _main_part(self) -> str:
        if "_rels/.rels" in self.infos:
            root = self.xml("_rels/.rels")
            for r in root.iter(REL + "Relationship"):
                if r.get("Type", "").endswith("/officeDocument"):
                    return r.get("Target", "").lstrip("/")
        return "word/document.xml"

    def _read(self, name: str) -> bytes:
        """读一个 zip 成员；压缩流损坏（截断、位翻转）按 notZip 拒绝，不抛裸 zlib.error。"""
        try:
            return self._zip.read(name)
        except (zipfile.BadZipFile, zlib.error) as exc:
            raise DocxError("notZip", f"{name}: {type(exc).__name__}") from None

    def xml(self, name: str, optional: bool = False) -> ET.Element | None:
        info = self.infos.get(name)
        if info is None:
            if optional:
                return None
            raise DocxError("noDocument", name)
        if info.file_size > MAX_PART:
            raise DocxError("tooLarge", name)
        return parse_xml(self._read(name), name)

    def relations(self, part: str) -> dict[str, dict]:
        d, f = posixpath.split(part)
        root = self.xml(posixpath.join(d, "_rels", f + ".rels"), optional=True)
        out = {}
        for r in (root.iter(REL + "Relationship") if root is not None else ()):
            target, external = r.get("Target", ""), r.get("TargetMode") == "External"
            if not external:
                target = target.lstrip("/") if target.startswith("/") else posixpath.normpath(posixpath.join(d, target))
            out[r.get("Id", "")] = {"type": r.get("Type", ""), "target": target, "external": external}
        return out

    def app_info(self) -> tuple[int | None, str]:
        """docProps/app.xml 记的页数与保存程序（Word 保存时写下；别的工具常常不写或写错）。"""
        if self.app is None:
            return None, ""
        pages, app, ver = (self.app.find(EP + k) for k in ("Pages", "Application", "AppVersion"))
        n = _int(pages.text, -1) if pages is not None and pages.text else -1
        name = " ".join(x for x in ((app.text or "").strip() if app is not None else "",
                                     (ver.text or "").strip() if ver is not None else "") if x)
        return (n if n > 0 else None), name[:80]


class Styles:
    """段落 / 字符 / 表格样式按 basedOn 链合并；docDefaults 打底。只取渲染需要的属性。"""

    def __init__(self, root: ET.Element | None):
        self.by_id: dict[str, ET.Element] = {}
        self.default_para: str | None = None
        self.default_table: str | None = None
        self.doc_rpr: list[ET.Element] = []
        self.doc_ppr: list[ET.Element] = []
        if root is None:
            return
        dd = root.find(W + "docDefaults")
        if dd is not None:
            self.doc_rpr = list(dd.iter(W + "rPr"))
            self.doc_ppr = list(dd.iter(W + "pPr"))
        for s in root.iter(W + "style"):
            sid = s.get(W + "styleId", "")
            self.by_id[sid] = s
            if s.get(W + "default") in ("1", "true"):
                if s.get(W + "type") == "paragraph":
                    self.default_para = sid
                elif s.get(W + "type") == "table":
                    self.default_table = sid

    def chain(self, sid: str | None) -> list[ET.Element]:
        """样式及其 basedOn 祖先，按从远到近排列（近的覆盖远的）。"""
        out, seen = [], set()
        while sid and sid in self.by_id and sid not in seen and len(seen) < 50:
            seen.add(sid)
            s = self.by_id[sid]
            out.append(s)
            b = s.find(W + "basedOn")
            sid = b.get(W + "val") if b is not None else None
        return list(reversed(out))

    def props(self, sid: str | None, kind: str) -> list[ET.Element]:
        return [x for s in self.chain(sid) for x in [s.find(W + kind)] if x is not None]

    def name(self, sid: str | None) -> str:
        s = self.by_id.get(sid or "")
        n = s.find(W + "name") if s is not None else None
        return (n.get(W + "val", "") if n is not None else "") or (sid or "")


def merged(props: list[ET.Element | None]) -> dict[str, ET.Element]:
    """一组 rPr / pPr / tblBorders 按顺序合并：后面的同名子元素覆盖前面的。"""
    out: dict[str, ET.Element] = {}
    for p in props:
        if p is None:
            continue
        for ch in p:
            out[_local(ch.tag)] = ch
    return out


def writeback_restrictions(root: ET.Element, style_root: ET.Element | None) -> dict[int, set[str]]:
    """页面和回写命令共用的段落限制；包含外层结构、跨段域及从样式继承的隐藏文字。"""
    paragraphs = list(root.iter(W + "p"))
    styles = Styles(style_root)
    indices = {id(p): n for n, p in enumerate(paragraphs)}
    reasons: dict[int, set[str]] = {n: set() for n in indices.values()}
    kinds = {**{W + t: "revision" for t in ("ins", "del", "moveFrom", "moveTo", "cellIns", "cellDel", "cellMerge")},
             **{W + t: "field" for t in ("fldChar", "fldSimple", "instrText")},
             W + "txbxContent": "textbox", W + "sdt": "contentControl", W + "object": "object",
             M + "oMath": "math", M + "oMathPara": "math"}
    field_depth = 0
    revision_ranges: set[str] = set()
    inline_parents = {W + t for t in ("p", "hyperlink", "smartTag", "customXml", "sdtContent", "ins", "del", "moveFrom", "moveTo")}
    inline_known = SKIP_INLINE | {W + t for t in ("r", "hyperlink", "smartTag", "customXml", "sdt", "ins", "del", "moveFrom", "moveTo", "fldSimple", "commentRangeStart", "commentRangeEnd", "p")} | {M + "oMath", M + "oMathPara"}
    run_known = SKIP_RUN | {W + t for t in ("t", "delText", "instrText", "fldChar", "tab", "ptab", "br", "cr", "lastRenderedPageBreak", "noBreakHyphen", "softHyphen", "sym", "drawing", "pict", "footnoteReference", "endnoteReference", "commentReference")}
    pending = [(root, set(), (), None)]
    while pending:
        el, inherited, parents, parent_tag = pending.pop()
        own = set(inherited)
        kind = kinds.get(el.tag)
        if el.tag.startswith(W) and _local(el.tag).endswith("Change"):
            kind = "formatRevision"
        if el.tag.startswith(W) and re.fullmatch(r"(?:moveFrom|moveTo|customXml(?:Ins|Del|MoveFrom|MoveTo))Range(?:Start|End)", _local(el.tag)):
            kind = "revision"
            key = _local(el.tag).removesuffix("Start").removesuffix("End") + el.get(W + "id", "")
            if _local(el.tag).endswith("Start"):
                revision_ranges.add(key)
            else:
                revision_ranges.discard(key)
        if ((parent_tag in inline_parents and el.tag not in inline_known)
                or (parent_tag == W + "r" and el.tag not in run_known)):
            own.add("unknown")
        if kind:
            own.add(kind)
        # 表格 / 行 / 单元格的修订属性是段落的前置兄弟，仍影响里面的段落。
        prop_tag = {W + "tbl": "tblPr", W + "tr": "trPr", W + "tc": "tcPr"}.get(el.tag)
        props = el.find(W + prop_tag) if prop_tag else None
        if el.tag == W + "tbl":
            table_style = props.find(W + "tblStyle") if props is not None else None
            sid = table_style.get(W + "val") if table_style is not None else styles.default_table
            # 条件表格样式的实际应用受行列位置等影响；包含隐藏设置时整表保守只读。
            for style in styles.chain(sid):
                if any(_local(prop.tag) in ("vanish", "webHidden", "specVanish") and _on(prop) is True
                       for prop in style.iter()):
                    own.add("hidden")
        for prop in props.iter() if props is not None else ():
            if prop.tag in kinds:
                own.add(kinds[prop.tag])
            if prop.tag.startswith(W) and _local(prop.tag).endswith("Change"):
                own.add("formatRevision")
        if el.tag == W + "p":
            n = indices[id(el)]
            parents = (*parents, n)
            if field_depth:
                reasons[n].add("field")
            if revision_ranges:
                reasons[n].add("revision")
        for n in parents:
            reasons[n].update(own)
        if el.tag == W + "fldChar":
            mode = el.get(W + "fldCharType")
            if mode == "begin":
                field_depth += 1
            elif mode == "end":
                field_depth = max(0, field_depth - 1)
        pending.extend((child, own, parents, el.tag) for child in reversed(list(el)))
    for n, p in enumerate(paragraphs):
        ppr = p.find(W + "pPr")
        ps = ppr.find(W + "pStyle") if ppr is not None else None
        sid = ps.get(W + "val") if ps is not None else styles.default_para
        base = merged([*styles.doc_rpr, *styles.props(sid, "rPr")])
        for r in own_runs(p):
            rpr = r.find(W + "rPr")
            rs = rpr.find(W + "rStyle") if rpr is not None else None
            rp = {**base, **merged([*styles.props(rs.get(W + "val") if rs is not None else None, "rPr"), rpr])}
            if any(_on(rp.get(k)) is True for k in ("vanish", "webHidden", "specVanish")):
                reasons[n].add("hidden")
    return {n: codes for n, codes in reasons.items() if codes}


def roman(n: int) -> str:
    vals = ((1000, "m"), (900, "cm"), (500, "d"), (400, "cd"), (100, "c"), (90, "xc"), (50, "l"), (40, "xl"),
            (10, "x"), (9, "ix"), (5, "v"), (4, "iv"), (1, "i"))
    out = ""
    for v, s in vals:
        while n >= v:
            out, n = out + s, n - v
    return out


def letters(n: int) -> str:
    n = min(max(n, 1), 26 * 10)
    return chr(ord("a") + (n - 1) % 26) * ((n - 1) // 26 + 1)


def chinese(n: int) -> str:
    if n < 10:
        return CN_DIGITS[max(n, 0)]
    if n < 20:
        return "十" + (CN_DIGITS[n % 10] if n % 10 else "")
    if n < 100:
        return CN_DIGITS[n // 10] + "十" + (CN_DIGITS[n % 10] if n % 10 else "")
    return str(n)


def fmt_number(n: int, fmt: str) -> str:
    return {"decimal": str(n), "lowerLetter": letters(n), "upperLetter": letters(n).upper(),
            "lowerRoman": roman(n), "upperRoman": roman(n).upper(), "chineseCounting": chinese(n),
            "chineseCountingThousand": chinese(n), "ideographTraditional": "甲乙丙丁戊己庚辛壬癸"[(n - 1) % 10],
            "decimalEnclosedCircle": chr(0x2460 + n - 1) if 0 < n <= 20 else str(n),
            "decimalZero": f"{n:02d}", "none": ""}.get(fmt, str(n))


class Numbering:
    """列表编号：同一 abstractNum 的各个 num 共用计数，startOverride 在该 num 首次使用时重置；
    上级前进时下级重新开始。编号文字只显示、不可选中、不回写。"""

    def __init__(self, root: ET.Element | None, styles: Styles):
        self.abstract: dict[str, ET.Element] = {}
        self.nums: dict[str, tuple[str, dict[int, int]]] = {}
        self.counters: dict[str, dict[int, int]] = {}
        self.overridden: set[tuple[str, int]] = set()
        self.styles = styles
        if root is None:
            return
        for a in root.iter(W + "abstractNum"):
            self.abstract[a.get(W + "abstractNumId", "")] = a
        for n in root.iter(W + "num"):
            aid = n.find(W + "abstractNumId")
            starts = {}
            for ov in n.findall(W + "lvlOverride"):
                so = ov.find(W + "startOverride")
                if so is not None:
                    starts[_int(ov.get(W + "ilvl"), 0, 0, 8)] = _int(so.get(W + "val"), 1, 0, MAX_COUNTER)
            self.nums[n.get(W + "numId", "")] = (aid.get(W + "val", "") if aid is not None else "", starts)

    def abstract_of(self, num_id: str, depth: int = 0) -> str | None:
        """numId → abstractNumId；numStyleLink 指向样式里的 numPr 时跟着找过去。"""
        aid, _ = self.nums.get(num_id, ("", {}))
        a = self.abstract.get(aid)
        if a is None:
            return None
        link = a.find(W + "numStyleLink")
        if link is not None:
            st = self.styles.by_id.get(link.get(W + "val", ""))
            np_ = st.find(W + "pPr/" + W + "numPr/" + W + "numId") if st is not None else None
            if np_ is None or depth >= 5:
                return None
            return self.abstract_of(np_.get(W + "val", ""), depth + 1)
        return aid

    def level(self, aid: str, ilvl: int) -> ET.Element | None:
        a = self.abstract.get(aid)
        if a is None:
            return None
        return next((lv for lv in a.findall(W + "lvl") if _int(lv.get(W + "ilvl"), -1) == ilvl), None)

    def label(self, num_id: str, ilvl: int) -> tuple[str, str | None]:
        """返回 (编号文字, 问题)；问题是解析不出的编号或不认识的格式，给诊断用。"""
        if num_id in ("", "0"):
            return "", None
        aid = self.abstract_of(num_id)
        lv = self.level(aid, ilvl) if aid is not None else None
        if lv is None:
            return "", f"numId={num_id[:12]} ilvl={ilvl}"
        _aid, overrides = self.nums.get(num_id, ("", {}))
        cnt = self.counters.setdefault(aid, {})

        def start(level: int) -> int:
            if level in overrides:
                return overrides[level]
            el = self.level(aid, level)
            s = el.find(W + "start") if el is not None else None
            return _int(s.get(W + "val") if s is not None else None, 1, 0, MAX_COUNTER)

        if ilvl in overrides and (num_id, ilvl) not in self.overridden:
            self.overridden.add((num_id, ilvl))
            cnt[ilvl] = overrides[ilvl]
        else:
            cnt[ilvl] = min(cnt[ilvl] + 1, MAX_COUNTER) if ilvl in cnt else start(ilvl)
        for deeper in [k for k in cnt if k > ilvl]:
            del cnt[deeper]
        fmt_el, text_el = lv.find(W + "numFmt"), lv.find(W + "lvlText")
        fmt = fmt_el.get(W + "val", "decimal") if fmt_el is not None else "decimal"
        text = text_el.get(W + "val", "") if text_el is not None else ""
        problem = None if fmt in NUM_FORMATS else fmt[:40]
        if fmt == "bullet":
            return BULLET_MAP.get(text, text)[:8], problem
        legal = _on(lv.find(W + "isLgl")) is True

        def sub(m: re.Match) -> str:
            k = int(m.group(1)) - 1
            el = self.level(aid, k)
            f = el.find(W + "numFmt") if el is not None else None
            kf = "decimal" if legal else (f.get(W + "val", "decimal") if f is not None else "decimal")
            return fmt_number(cnt.get(k, start(k)), kf)
        return re.sub(r"%(\d)", sub, text)[:40], problem


class Section:
    """一节的页面几何、页码设置与页眉页脚引用。"""

    def __init__(self, sp: ET.Element | None, prev: "Section | None" = None):
        def g(el, k, d, lo=0.0, hi=31680.0):   # 22 英寸以内
            return _num(el.get(W + k) if el is not None else None, d, lo, hi)
        sz = sp.find(W + "pgSz") if sp is not None else None
        mar = sp.find(W + "pgMar") if sp is not None else None
        self.w = max(g(sz, "w", 11906), 1440) * TWIP_PX
        self.h = max(g(sz, "h", 16838), 1440) * TWIP_PX
        orient = sz.get(W + "orient") if sz is not None else None
        self.orient = "landscape" if (orient == "landscape" or self.w > self.h) else "portrait"
        self.margin = {k: abs(_num(mar.get(W + k) if mar is not None else None,
                                   1440 if k in ("top", "bottom") else 1800, -15840, 15840)) * TWIP_PX
                       for k in ("top", "right", "bottom", "left")}
        self.header_dist = g(mar, "header", 851) * TWIP_PX
        self.footer_dist = g(mar, "footer", 992) * TWIP_PX
        t = sp.find(W + "type") if sp is not None else None
        self.type = t.get(W + "val", "nextPage") if t is not None else "nextPage"
        self.title_pg = (_on(sp.find(W + "titlePg")) is True) if sp is not None else False
        self.refs: dict[tuple[str, str], str] = dict(prev.refs) if prev else {}
        for kind in ("header", "footer"):
            for ref in (sp.findall(W + kind + "Reference") if sp is not None else []):
                self.refs[(kind, ref.get(W + "type", "default"))] = ref.get(R + "id", "")
        cols = sp.find(W + "cols") if sp is not None else None
        self.cols = _int(cols.get(W + "num") if cols is not None else None, 1, 1, 45)
        pn = sp.find(W + "pgNumType") if sp is not None else None
        self.pg_start = _int(pn.get(W + "start"), 1, 0, 1_000_000) if pn is not None and pn.get(W + "start") else None
        self.pg_fmt = (pn.get(W + "fmt") if pn is not None else None) or "decimal"

    def same_geometry(self, other: "Section") -> bool:
        return abs(self.w - other.w) < 1 and abs(self.h - other.h) < 1


SKIP_BLOCK = frozenset(W + t for t in ("sectPr", "tcPr", "trPr", "tblPr", "tblGrid", "bookmarkStart", "bookmarkEnd",
                                       "proofErr", "permStart", "permEnd", "customXmlInsRangeStart",
                                       "customXmlInsRangeEnd", "customXmlDelRangeStart", "customXmlDelRangeEnd",
                                       "moveFromRangeStart", "moveFromRangeEnd", "moveToRangeStart", "moveToRangeEnd",
                                       "sdtPr", "sdtEndPr", "tblPrEx"))
SKIP_INLINE = frozenset(W + t for t in ("pPr", "bookmarkStart", "bookmarkEnd", "proofErr", "permStart", "permEnd",
                                        "moveFromRangeStart", "moveFromRangeEnd", "moveToRangeStart", "moveToRangeEnd",
                                        "customXmlInsRangeStart", "customXmlInsRangeEnd", "customXmlDelRangeStart",
                                        "customXmlDelRangeEnd", "customXmlMoveFromRangeStart",
                                        "customXmlMoveFromRangeEnd", "customXmlMoveToRangeStart",
                                        "customXmlMoveToRangeEnd", "sdtPr", "sdtEndPr"))
SKIP_RUN = frozenset(W + t for t in ("rPr", "footnoteRef", "endnoteRef", "separator", "continuationSeparator",
                                     "annotationRef", "dayShort", "monthShort", "yearShort", "dayLong", "monthLong",
                                     "yearLong", "pgNum", "lastRenderedPageBreak_"))


class Renderer:
    """把主文档渲染成逐页 HTML。"""

    def __init__(self, pkg: Package):
        self.pkg = pkg
        self.styles = Styles(pkg.styles)
        self.writeback_ro = writeback_restrictions(pkg.root, pkg.styles)
        self.numbering = Numbering(pkg.numbering, self.styles)
        self.even_odd = pkg.settings is not None and _on(pkg.settings.find(W + "evenAndOddHeaders")) is True
        self.diag: dict[str, dict] = {}       # 代码 → {"keys", "count", "pages", "examples"}
        self.pindex: dict[int, int] = {}
        self.rindex: dict[int, int] = {}
        for i, p in enumerate(pkg.root.iter(W + "p")):
            self.pindex[id(p)] = i
            for j, r in enumerate(own_runs(p)):
                self.rindex[id(r)] = j
        self.has_rendered = self._body_has_rendered()
        self.fields: list[dict] = []           # 复杂域的嵌套栈：{"instr", "phase": instr / result / replaced, "id"}
        self.comment_open: list[str] = []
        self.part = "body"                     # body / header / footer / textbox
        self.rels = pkg.rels                   # 当前部件的关系表（图片、链接按它找）
        self.block_ro: set[str] = set()        # 块级内容控件等外层结构给段落带来的只读原因
        self.block_wrap: tuple = ()           # 表格行、单元格修订传给内部文字的样式与提示
        self.block_notes = ""                  # 跟随各片段、在最终分页后登记的诊断标记
        self.pages: list[dict] = []
        self.media: dict[str, str] = {}        # 部件路径 → CSS 类名（同一张图全文只内联一次）
        self.media_css: list[str] = []
        self.part_cache: dict[str, str] = {}
        self.sect: Section | None = None

    def _body_has_rendered(self) -> bool:
        """正文里有没有渲染分页标记（文本框和 mc:Fallback 里的不算）。"""
        stack = [self.pkg.root.find(W + "body")]
        while stack:
            el = stack.pop()
            for ch in el:
                if ch.tag == W + "lastRenderedPageBreak":
                    return True
                if ch.tag not in (W + "txbxContent", MC + "Fallback"):
                    stack.append(ch)
        return False

    # ---------- 诊断 ----------
    def note(self, code: str, key: object = None, example: str = "") -> str:
        """同一结构只计一次；返回随内容分页的内部标记，assemble 再登记页码并移除标记。"""
        d = self.diag.setdefault(code, {"keys": set(), "count": 0, "pages": set(), "examples": []})
        k = key if key is not None else object()
        if k not in d["keys"]:
            d["keys"].add(k)
            d["count"] += 1
        ex = (example or "").strip()[:80]
        if ex and ex not in d["examples"] and len(d["examples"]) < 5:
            d["examples"].append(ex)
        return f"\x00DOCX_DIAG:{code}\x00" if self.part in ("body", "textbox") else ""

    # ---------- 分页 ----------
    def new_page(self, opened_by: str) -> None:
        self.pages.append({"sect": self.sect, "blocks": [], "notes": "", "content": 0, "opened": opened_by, "first": None})

    def brk(self, kind: str) -> None:
        """硬分页保留空白页；段前分页与渲染标记在空页上不重复翻页。"""
        page = self.pages[-1]
        if page["content"] or (kind == "explicit" and page["opened"] != "rendered"):
            self.new_page(kind)
        elif kind == "explicit":
            page["opened"] = "explicit"   # 渲染标记只抵消同一处的一个硬分页符

    def emit(self, block: str, first: dict | None = None) -> None:
        page = self.pages[-1]
        page["blocks"].append(block)
        page["content"] += 1
        if page["first"] is None and first is not None and first.get("pi") is not None:
            page["first"] = first

    # ---------- 只读预检 ----------
    def ro_scan(self, p: ET.Element) -> set[str]:
        """整段扫一遍：段落里只要有修订、域、文本框、公式等结构，所有片段都只读（不等渲染到那里才知道）。"""
        out = set(self.block_ro)
        out.update(self.writeback_ro.get(self.pindex.get(id(p)), ()))
        if self.fields:
            out.add("field")                      # 跨段的域（目录等）还没结束
        for el in p.iter():
            t = el.tag
            if t in (W + "ins", W + "del", W + "moveFrom", W + "moveTo"):
                out.add("revision")
            elif t in (W + "rPrChange", W + "pPrChange", W + "sectPrChange"):
                out.add("formatRevision")
            elif t in (W + "fldChar", W + "fldSimple", W + "instrText"):
                out.add("field")
            elif t == W + "txbxContent":
                out.add("textbox")
            elif t in (M + "oMath", M + "oMathPara"):
                out.add("math")
            elif t == W + "sdt":
                out.add("contentControl")
            elif t == W + "object":
                out.add("object")
            elif t == W + "vanish" and _on(el):
                out.add("hidden")
        return out

    # ---------- 段落 ----------
    def para_props(self, p: ET.Element) -> tuple[dict, dict, str | None]:
        ppr = p.find(W + "pPr")
        ps = ppr.find(W + "pStyle") if ppr is not None else None
        sid = ps.get(W + "val") if ps is not None else self.styles.default_para
        pp = merged([*self.styles.doc_ppr, *self.styles.props(sid, "pPr"), ppr])
        rp = merged([*self.styles.doc_rpr, *self.styles.props(sid, "rPr")])
        return pp, rp, sid

    def para_style(self, pp: dict, cont: bool) -> str:
        css = []
        jc = pp.get("jc")
        if jc is not None and jc.get(W + "val") in JC:
            css.append(f"text-align:{JC[jc.get(W + 'val')]}")
        sp = pp.get("spacing")
        if sp is not None:
            if not cont and sp.get(W + "before") and sp.get(W + "beforeAutospacing") not in ("1", "true"):
                css.append(f"margin-top:{_num(sp.get(W + 'before'), 0, 0, 31680) * TWIP_PX:.1f}px")
            if sp.get(W + "after") and sp.get(W + "afterAutospacing") not in ("1", "true"):
                css.append(f"margin-bottom:{_num(sp.get(W + 'after'), 0, 0, 31680) * TWIP_PX:.1f}px")
            line, rule = sp.get(W + "line"), sp.get(W + "lineRule", "auto")
            if line:
                if rule == "auto":
                    css.append(f"line-height:{_num(line, 240, 24, 2400) / 240 * 1.15:.3f}")
                else:
                    css.append(f"line-height:{_num(line, 240, 1, 31680) * TWIP_PX:.1f}px")
        ind = pp.get("ind")
        if ind is not None:
            left = _num(ind.get(W + "left") or ind.get(W + "start"), 0, -31680, 31680)
            right = _num(ind.get(W + "right") or ind.get(W + "end"), 0, -31680, 31680)
            first = _num(ind.get(W + "firstLine"), 0, 0, 31680) - _num(ind.get(W + "hanging"), 0, 0, 31680)
            if left:
                css.append(f"margin-left:{left * TWIP_PX:.1f}px")
            if right:
                css.append(f"margin-right:{right * TWIP_PX:.1f}px")
            if first and not cont:
                css.append(f"text-indent:{first * TWIP_PX:.1f}px")
        shd = pp.get("shd")
        if shd is not None and hexcolor(shd.get(W + "fill")):
            css.append(f"background:{hexcolor(shd.get(W + 'fill'))}")
        return ";".join(css)

    def run_style(self, rp: dict) -> tuple[str, list[str]]:
        css, cls = [], []
        if _on(rp.get("b")):
            css.append("font-weight:700")
        if _on(rp.get("i")):
            css.append("font-style:italic")
        deco = []
        u = rp.get("u")
        if u is not None and u.get(W + "val", "single") != "none":
            deco.append("underline")
            if u.get(W + "val") == "double":
                css.append("text-decoration-style:double")
        if _on(rp.get("strike")):
            deco.append("line-through")
            cls.append("docx-strike")
        if _on(rp.get("dstrike")):
            deco.append("line-through")
            css.append("text-decoration-style:double")
            cls.append("docx-strike")
        if deco:
            css.append("text-decoration-line:" + " ".join(dict.fromkeys(deco)))
        c = rp.get("color")
        if c is not None and hexcolor(c.get(W + "val")):
            css.append(f"color:{hexcolor(c.get(W + 'val'))}")
        sz = rp.get("sz")
        if sz is not None:
            css.append(f"font-size:{_num(sz.get(W + 'val'), 21, 2, 3276) / 2:g}pt")
        hl = rp.get("highlight")
        if hl is not None and hl.get(W + "val") in HIGHLIGHT:
            css.append(f"background-color:{HIGHLIGHT[hl.get(W + 'val')]}")
            cls.append("docx-hl")
        shd = rp.get("shd")
        if shd is not None and hexcolor(shd.get(W + "fill")) and hl is None:
            css.append(f"background-color:{hexcolor(shd.get(W + 'fill'))}")
        va = rp.get("vertAlign")
        if va is not None and va.get(W + "val") in ("superscript", "subscript"):
            css.append("vertical-align:" + ("super" if va.get(W + "val") == "superscript" else "sub") + ";font-size:smaller")
        if _on(rp.get("caps")):
            css.append("text-transform:uppercase")
        if _on(rp.get("smallCaps")):
            css.append("font-variant:small-caps")
        f = rp.get("rFonts")
        if f is not None:
            fams = [x for x in dict.fromkeys(font_name(f.get(W + k) or "") for k in ("eastAsia", "ascii", "hAnsi")) if x]
            if fams:
                css.append("font-family:" + ",".join(f"'{x}'" for x in fams) + ",var(--docx-fallback)")
        return ";".join(css), cls

    def paragraph(self, p: ET.Element, *, paginate: bool) -> list:
        """段落 → [("html", 片段, 首段信息) | ("break", 种类) | ("section", sectPr)]。

        分页标记前已有文字时，段落拆成首片与续片；分页标记出现在任何文字之前，表示整段从新页开始。
        只有一个分页符、没有文字的段落留在上一页（Word 里分页符和段落标记显示在分页之前）。
        """
        pp, base_rp, sid = self.para_props(p)
        ro = self.ro_scan(p) if self.part == "body" else set()
        tokens: list = []
        inline: list[str] = []
        pi = self.pindex.get(id(p)) if self.part == "body" else None
        pid = para_id(p) if self.part == "body" else None
        started = False
        prefix = ""
        notes = self.block_notes
        ppr = p.find(W + "pPr")
        for ch in (ppr.iter() if ppr is not None else ()):
            if ch.tag in (W + "pPrChange", W + "sectPrChange"):
                notes += self.note("formatRevision", id(ch))
            elif ch.tag in (W + "ins", W + "del", W + "moveFrom", W + "moveTo"):   # 段落标记的插入 / 删除
                notes += self.note("revision", ch.get(W + "id") or id(ch), ch.get(W + "author", ""))
            elif ch.tag == W + "rPrChange":
                notes += self.note("formatRevision", id(ch))

        def flush() -> None:
            nonlocal inline, started
            cont = started
            body = ("" if cont else prefix) + "".join(inline)
            attrs = self.para_attrs(pi, pid, pp, sid, cont, ro)
            tokens.append(("html", f"<div {attrs}>{notes}{body or '<br>'}</div>",
                           {"pi": pi, "pid": pid, "cont": cont, "empty": not inline}))
            inline, started = [], True

        if paginate and _on(pp.get("pageBreakBefore")):
            tokens.append(("break", "before"))
        num = pp.get("numPr")
        if num is not None:
            ilvl_el, nid_el = num.find(W + "ilvl"), num.find(W + "numId")
            text, problem = self.numbering.label(nid_el.get(W + "val", "") if nid_el is not None else "",
                                                 _int(ilvl_el.get(W + "val") if ilvl_el is not None else None, 0, 0, 8))
            if problem:
                notes += self.note("numbering", problem, problem)
            if text:
                notes += self.note("list", nid_el.get(W + "val", "") if nid_el is not None else "")
                prefix = (f'<span class="docx-num" aria-hidden="true">{_esc(text)}</span>'
                          '<span class="docx-tab docx-num" aria-hidden="true">\t</span>')
        for kind, payload in self.inline(p, base_rp, paginate, self.block_wrap):
            if kind == "html":
                inline.append(payload)
            elif paginate:
                if inline:
                    flush()
                tokens.append(("break", payload))
        if inline or not started:
            flush()
        # 只有分页符的空段：段落标记留在分页之前那一页
        if (len(tokens) == 2 and tokens[0] == ("break", "explicit") and tokens[1][0] == "html"
                and tokens[1][2]["empty"] and not prefix):
            tokens.reverse()
        sect = pp.get("sectPr")
        if sect is not None and paginate:
            tokens.append(("section", sect))
        return tokens

    def para_attrs(self, pi, pid, pp, sid, cont, ro) -> str:
        cls = ["docx-p"]
        if cont:
            cls.append("docx-cont")
        m = re.match(r"(?:heading|标题)\s*(\d)", self.styles.name(sid).lower())
        ol = pp.get("outlineLvl")
        level = int(m.group(1)) if m else (_int(ol.get(W + "val"), 9, 0, 9) + 1 if ol is not None else 0)
        if 0 < level <= 6:
            cls.append(f"docx-h docx-h{level}")
        if self.part != "body":
            cls.append("docx-part-text")
        attrs = [f'class="{" ".join(cls)}"']
        if pi is not None:
            attrs.append(f'data-docx-p="{pi}"')
        if pid:
            attrs.append(f'data-docx-pid="{pid}"')
        if ro:
            attrs.append(f'data-docx-ro="{_esc(",".join(sorted(ro)))}"')
        style = self.para_style(pp, cont)
        if style:
            attrs.append(f'style="{_esc(style)}"')
        return " ".join(attrs)

    def inline(self, parent: ET.Element, base_rp: dict, paginate: bool, wrap: tuple, notes: str = ""):
        """段落内的子元素 → ("html", 片段) / ("break", 种类)。

        修订、链接、域不用独立的开闭标签包起来（段内分页会把它们切坏），而是把类名和提示传给每个运行。
        wrap 是 ((类名, 提示), ...)。
        """
        for el in parent:
            tag = el.tag
            if tag == W + "r":
                yield from self.run(el, base_rp, paginate, wrap, notes)
            elif tag in (W + "ins", W + "moveTo", W + "del", W + "moveFrom"):
                ins = tag in (W + "ins", W + "moveTo")
                mark = self.note("revision", el.get(W + "id") or id(el), el.get(W + "author", ""))
                title = f"{'+' if ins else '−'} {el.get(W + 'author', '')[:80]} {el.get(W + 'date', '')[:40]}".strip()
                yield from self.inline(el, base_rp, paginate, wrap + (("docx-ins" if ins else "docx-del", title),), notes + mark)
            elif tag == W + "hyperlink":
                rel = self.rels.get(el.get(R + "id", ""), {})
                title = rel.get("target", "") or el.get(W + "anchor", "")
                yield from self.inline(el, base_rp, paginate, wrap + (("docx-link", title[:200]),), notes)
            elif tag == W + "fldSimple":
                instr = el.get(W + "instr", "")
                mark = self.note("field", id(el), instr)
                special = self.page_field(instr)
                if special:
                    yield ("html", notes + mark + special)
                else:
                    yield from self.inline(el, base_rp, paginate, wrap + (("docx-field", instr.strip()[:200]),), notes + mark)
            elif tag in (W + "smartTag", W + "customXml"):
                yield from self.inline(el, base_rp, paginate, wrap, notes)
            elif tag == W + "sdt":
                mark = self.note("contentControl", id(el))
                content = el.find(W + "sdtContent")
                if content is not None:
                    yield from self.inline(content, base_rp, paginate, wrap, notes + mark)
            elif tag == W + "commentRangeStart":
                self.comment_open.append(el.get(W + "id", ""))
            elif tag == W + "commentRangeEnd":
                self.close_comment(el)
            elif tag in (M + "oMath", M + "oMathPara"):
                mark = self.note("math", id(el))
                text = "".join(t.text or "" for t in el.iter(M + "t"))
                yield ("html", notes + mark + f'<span class="docx-math" title="OMML">{_esc(text)}</span>')
            elif tag in SKIP_INLINE:
                continue
            else:
                yield ("html", notes + self.note("unknown", _local(tag), _local(tag)))

    def close_comment(self, el: ET.Element) -> None:
        cid = el.get(W + "id", "")
        if cid in self.comment_open:
            self.comment_open.remove(cid)

    @staticmethod
    def page_field(instr: str) -> str | None:
        word = instr.strip().split(" ", 1)[0].upper() if instr.strip() else ""
        if word in ("PAGE", "NUMPAGES", "SECTIONPAGES"):
            return f'<span class="docx-field" data-docx-field="{word}">\x00{word}\x00</span>'
        return None

    def run(self, r: ET.Element, base_rp: dict, paginate: bool, wrap: tuple, notes: str = ""):
        rpr = r.find(W + "rPr")
        rs = rpr.find(W + "rStyle") if rpr is not None else None
        rp = dict(base_rp)
        rp.update(merged([*self.styles.props(rs.get(W + "val") if rs is not None else None, "rPr"), rpr]))
        if rpr is not None and rpr.find(W + "rPrChange") is not None:
            notes += self.note("formatRevision", id(rpr))
        hidden = _on(rp.get("vanish")) is True
        css, cls = self.run_style(rp)
        if self.comment_open:
            cls.append("docx-cmt")
            for cid in self.comment_open:
                notes += self.note("comment", cid)
        parts: list = []
        in_result = [False]
        self.run_children(list(r), parts, in_result)
        if hidden and any(k != "break" and v for k, v in parts):
            notes += self.note("hidden", id(r))
        ri = self.rindex.get(id(r)) if self.part == "body" else None
        if in_result[0]:
            cls.append("docx-field")
        cls.extend(c for c, _t in wrap)
        attrs = [f'class="{" ".join(["docx-r", *dict.fromkeys(cls)])}"']
        titles = [t for _c, t in wrap if t]
        if titles:
            attrs.append(f'title="{_esc(" · ".join(titles))}"')
        if css:
            attrs.append(f'style="{_esc(css)}"')
        if hidden:
            attrs.append("hidden")
        if ri is not None:
            attrs.append(f'data-docx-r="{ri}"')
        open_tag = notes + f"<span {' '.join(attrs)}>"
        buf: list[str] = []
        for kind, val in parts:
            if kind == "text":
                buf.append(_esc(val))
            elif kind == "html":
                buf.append(val)
            elif paginate:
                if buf:
                    yield ("html", open_tag + "".join(buf) + "</span>")
                    buf = []
                yield ("break", val)
        if buf:
            yield ("html", open_tag + "".join(buf) + "</span>")

    def run_children(self, children: list, parts: list, in_result: list) -> None:
        for ch in children:
            tag = ch.tag
            first_part = len(parts)
            if tag == W + "fldChar":
                t = ch.get(W + "fldCharType")
                if t == "begin":
                    self.fields.append({"instr": "", "phase": "instr", "id": id(ch)})
                elif t == "separate" and self.fields:
                    f = self.fields[-1]
                    self.note("field", f["id"], f["instr"])
                    outer_ok = all(x["phase"] == "result" for x in self.fields[:-1])
                    special = self.page_field(f["instr"])
                    if special:
                        f["phase"] = "replaced"
                        if outer_ok:
                            marks = "".join(self.note("field", x["id"], x["instr"]) for x in self.fields)
                            parts.append(("html", marks + special))
                    else:
                        f["phase"] = "result"
                elif t == "end" and self.fields:
                    f = self.fields.pop()
                    self.note("field", f["id"], f["instr"])
                continue
            if tag == W + "instrText":
                if self.fields and self.fields[-1]["phase"] == "instr":
                    self.fields[-1]["instr"] = (self.fields[-1]["instr"] + (ch.text or ""))[:500]
                continue
            if tag == MC + "AlternateContent":
                # 能显示 Choice（绘图、VML）就用它，否则退回 Fallback 里的普通内容（如符号字体的表情）
                choice, fallback = ch.find(MC + "Choice"), ch.find(MC + "Fallback")
                usable = choice is not None and all(x.tag in (W + "drawing", W + "pict", W + "rPr") for x in choice)
                pick = choice if usable else (fallback if fallback is not None else choice)
                self.run_children(list(pick) if pick is not None else [], parts, in_result)
                continue
            if any(f["phase"] != "result" for f in self.fields):
                continue                              # 域的指令部分，或已换成页码的结果
            if self.fields:
                in_result[0] = True
            if tag in (W + "t", W + "delText"):
                parts.append(("text", ch.text or ""))
            elif tag in (W + "tab", W + "ptab"):
                parts.append(("html", '<span class="docx-tab">\t</span>'))
            elif tag == W + "br":
                parts.append(("break", "explicit") if ch.get(W + "type") == "page" else ("html", "<br>"))
            elif tag == W + "cr":
                parts.append(("html", "<br>"))
            elif tag == W + "lastRenderedPageBreak":
                if self.part == "body":
                    parts.append(("break", "rendered"))
            elif tag == W + "noBreakHyphen":
                parts.append(("html", '<span class="docx-nontext">‑</span>'))
            elif tag == W + "softHyphen":
                parts.append(("html", '<span class="docx-nontext">­</span>'))
            elif tag == W + "sym":
                try:
                    c = chr(int(ch.get(W + "char", ""), 16))
                except (ValueError, OverflowError):
                    c = ""
                if 0xD800 <= ord(c) <= 0xDFFF:
                    c = ""   # 孤立代理编码不进 UTF-8，也不出现在页面文字里
                parts.append(("html", '<span class="docx-nontext">' + _esc(BULLET_MAP.get(c, c)) + '</span>'))
            elif tag == W + "drawing":
                parts.append(("html", self.drawing(ch)))
            elif tag == W + "pict":
                parts.append(("html", self.vml(ch)))
            elif tag in (W + "footnoteReference", W + "endnoteReference"):
                kind = "fn" if tag == W + "footnoteReference" else "en"
                nid = ch.get(W + "id", "")[:12]
                mark = self.note("footnote", kind + nid)
                parts.append(("html", mark + f'<sup class="docx-mark" data-docx-note="{kind}{_esc(nid)}">[{_esc(nid)}]</sup>'))
            elif tag == W + "commentReference":
                cid = ch.get(W + "id", "")
                author, text = self.pkg.comments.get(cid, ("", ""))
                mark = self.note("comment", cid)
                tip = (author + "：" if author else "") + text
                parts.append(("html", mark + f'<sup class="docx-mark docx-comment" title="{_esc(tip)}">[W{_esc(cid[:12])}]</sup>'))
            elif tag == W + "object":
                mark = self.note("object", id(ch))
                parts.append(("html", mark + '<span class="docx-mark docx-object">[OLE]</span>'))
            elif tag == W + "ruby":
                base = ch.find(W + "rubyBase")
                text = "".join(t.text or "" for t in (base.iter(W + "t") if base is not None else ()))
                parts.append(("html", self.note("unknown", "ruby", "ruby") + _esc(text)))
            elif tag in SKIP_RUN:
                continue
            else:
                parts.append(("html", self.note("unknown", _local(tag), _local(tag))))
            if self.fields:
                marks = "".join(self.note("field", f["id"], f["instr"]) for f in self.fields)
                for i in range(first_part, len(parts)):
                    kind, value = parts[i]
                    if kind != "break":
                        parts[i] = ("html", marks + (_esc(value) if kind == "text" else value))

    def drawing(self, d: ET.Element) -> str:
        anchor = d.find(WP + "anchor")
        notes = ""
        if anchor is not None:
            notes += self.note("floating", id(d))
        box = anchor if anchor is not None else d.find(WP + "inline")
        ext = box.find(WP + "extent") if box is not None else None
        w = _num(ext.get("cx") if ext is not None else None, 0, 0, 1e9) * EMU_PX
        h = _num(ext.get("cy") if ext is not None else None, 0, 0, 1e9) * EMU_PX
        boxes = top_textboxes(d)
        if boxes:
            return notes + "".join(self.textbox(t, w) for t in boxes)
        blip = next(d.iter(A + "blip"), None)
        if blip is not None:
            return notes + self.image(blip.get(R + "embed", ""), w, h)
        return notes + self.note("object", id(d)) + '<span class="docx-mark docx-object">[drawing]</span>'

    def vml(self, pict: ET.Element) -> str:
        boxes = top_textboxes(pict)
        if boxes:
            return "".join(self.textbox(t, 0) for t in boxes)
        img = next(pict.iter(V + "imagedata"), None)
        if img is not None:
            return self.image(img.get(R + "id", ""), 0, 0)
        return self.note("object", id(pict)) + '<span class="docx-mark docx-object">[VML]</span>'

    def textbox(self, txbx: ET.Element, width: float) -> str:
        """文本框：内容在锚点处以内联框显示（位置不保真），只读，不带定位属性。"""
        mark = self.note("textbox", id(txbx))
        outer = self.part
        self.part = "textbox" if outer == "body" else outer
        try:
            inner = self.blocks_inline(txbx)
        finally:
            self.part = outer
        style = f' style="width:{min(width, 2000):.1f}px"' if width else ""
        return mark + f'<span class="docx-txbx" data-docx-ro="textbox"{style}>{inner}</span>'

    def image(self, rid: str, w: float, h: float) -> str:
        """图片：按部件路径去重，数据只在 CSS 里出现一次（页眉 logo 不随页数重复内联）。"""
        rel = self.rels.get(rid)
        size = f"width:{min(w, 4000):.1f}px;height:{min(h, 4000):.1f}px" if w and h else "width:96px;height:72px"
        placeholder = f'<span class="docx-mark docx-missing" style="display:inline-block;{size}">[image]</span>'
        if not rel or rel["external"]:
            return self.note("object", rid or "image", "external image" if rel else rid) + placeholder
        target = rel["target"]
        cls = self.media.get(target)
        if cls is None:
            data = self.pkg.media.get(target)
            ext = target.rsplit(".", 1)[-1].lower()
            mime = {"png": "image/png", "jpg": "image/jpeg", "jpeg": "image/jpeg", "gif": "image/gif",
                    "bmp": "image/bmp", "webp": "image/webp"}.get(ext)
            if data is None and target in self.pkg.media:
                return self.note("mediaTooLarge", target, target) + placeholder
            if data is None or not mime:
                return self.note("object", target, target) + placeholder
            cls = f"docx-m{len(self.media) + 1}"
            self.media[target] = cls
            uri = f"data:{mime};base64," + base64.b64encode(data).decode()
            self.media_css.append(f'section.docx-page .{cls}{{background-image:url("{uri}")}}')
        return self.note("image", target) + f'<span class="docx-img {cls}" role="img" style="{size}"></span>'

    def blocks_inline(self, container: ET.Element) -> str:
        """不分页的块级内容（文本框、页眉页脚、嵌套表格）。"""
        out = []
        for seg in self.blocks(container, paginate=False):
            out.extend(seg)
        return "".join(out)

    def blocks(self, container: ET.Element, *, paginate: bool) -> list[list[str]]:
        """单元格等容器里的块，按分页标记切成几段：[[第一页的块], [第二页的块], ...]。"""
        segs: list[list[str]] = [[]]
        for el in container:
            if el.tag == W + "p":
                for tok in self.paragraph(el, paginate=paginate):
                    if tok[0] == "html":
                        segs[-1].append(tok[1])
                    elif tok[0] == "break":
                        segs.append([])
            elif el.tag == W + "tbl":
                segs[-1].extend(part for part, _ in self.table(el, paginate=False) if part != BREAK)
            elif el.tag == W + "sdt":
                mark = self.note("contentControl", id(el))
                content = el.find(W + "sdtContent")
                if content is not None:
                    outer = self.block_ro, self.block_notes
                    self.block_ro = self.block_ro | {"contentControl"}
                    self.block_notes += mark
                    try:
                        inner = self.blocks(content, paginate=paginate)
                    finally:
                        self.block_ro, self.block_notes = outer
                    segs[-1].extend(inner[0])
                    segs.extend(inner[1:])
            elif el.tag == W + "customXml":
                inner = self.blocks(el, paginate=paginate)
                segs[-1].extend(inner[0])
                segs.extend(inner[1:])
            elif el.tag == W + "commentRangeEnd":
                self.close_comment(el)
            elif el.tag == W + "commentRangeStart":
                self.comment_open.append(el.get(W + "id", ""))
            elif el.tag in SKIP_BLOCK:
                continue
            else:
                segs[-1].append(self.note("unknown", _local(el.tag), _local(el.tag)))
        return segs

    # ---------- 表格 ----------
    @contextmanager
    def structural_revisions(self, *props):
        """表格属性中的修订作用于内部全部段落、嵌套表格及跨页片段。"""
        outer = self.block_ro, self.block_wrap, self.block_notes
        try:
            for prop in props:
                for rv in (prop if prop is not None else ()):
                    tag = _local(rv.tag)
                    if rv.tag not in {W + name for name in ("ins", "del", "cellIns", "cellDel", "cellMerge",
                                                            "tcPrChange", "trPrChange", "tblPrChange")}:
                        continue
                    code = "formatRevision" if tag.endswith("PrChange") else "revision"
                    self.block_ro = self.block_ro | {code}
                    self.block_notes += self.note(code, rv.get(W + "id") or id(rv), rv.get(W + "author", ""))
                    cls, symbol = {"ins": ("docx-ins", "+"), "cellIns": ("docx-ins", "+"),
                                   "del": ("docx-del", "−"), "cellDel": ("docx-del", "−")}.get(tag, ("docx-revision", "Δ"))
                    title = f"{symbol} {rv.get(W + 'author', '')[:80]} {rv.get(W + 'date', '')[:40]}".strip()
                    self.block_wrap += ((cls, title),)
            yield
        finally:
            self.block_ro, self.block_wrap, self.block_notes = outer

    def table(self, tbl: ET.Element, *, paginate: bool):
        """表格 → [(html, 首段信息) | (BREAK, None)]。

        跨页的行按单元格里的分页标记切成几段，各段落在各自的页上；表头行（tblHeader）在续表里重复。
        """
        notes = self.block_notes + self.note("table", id(tbl))
        tpr = tbl.find(W + "tblPr")
        ts = tpr.find(W + "tblStyle") if tpr is not None else None
        sid = ts.get(W + "val") if ts is not None else self.styles.default_table
        tprops = merged([*self.styles.props(sid, "tblPr"), tpr])
        tborders = tprops.get("tblBorders")
        grid = [_num(g.get(W + "w"), 0, 0, 31680) * TWIP_PX for g in tbl.findall(W + "tblGrid/" + W + "gridCol")]
        rows: list[dict] = []
        for tr in tbl.findall(W + "tr"):
            trpr = tr.find(W + "trPr")
            header = trpr is not None and _on(trpr.find(W + "tblHeader")) is True
            cells, col = [], 0
            gb = trpr.find(W + "gridBefore") if trpr is not None else None
            if gb is not None and _int(gb.get(W + "val"), 0, 0, 63):
                span = _int(gb.get(W + "val"), 0, 0, 63)
                cells.append({"col": col, "span": span, "vmerge": None, "css": "border:none", "segs": [[]]})
                col += span
            for tc in tr.findall(W + "tc"):
                tcpr = tc.find(W + "tcPr")
                span_el = tcpr.find(W + "gridSpan") if tcpr is not None else None
                span = _int(span_el.get(W + "val") if span_el is not None else None, 1, 1, 63)
                vm = tcpr.find(W + "vMerge") if tcpr is not None else None
                vmerge = None if vm is None else ("restart" if vm.get(W + "val") == "restart" else "continue")
                css = []
                shd = tcpr.find(W + "shd") if tcpr is not None else None
                if shd is not None and hexcolor(shd.get(W + "fill")):
                    css.append(f"background:{hexcolor(shd.get(W + 'fill'))}")
                tcb = tcpr.find(W + "tcBorders") if tcpr is not None else None
                css.extend(border_css(merged([tborders, tcb])))
                va = tcpr.find(W + "vAlign") if tcpr is not None else None
                if va is not None:
                    css.append("vertical-align:" + {"center": "middle", "bottom": "bottom"}.get(va.get(W + "val", ""), "top"))
                fp = tc.find(W + "p")
                first = ({"pi": self.pindex.get(id(fp)), "pid": para_id(fp), "cont": False}
                         if fp is not None and self.part == "body" else None)
                with self.structural_revisions(tpr, trpr, tcpr):
                    segs = self.blocks(tc, paginate=paginate)
                cells.append({"col": col, "span": span, "vmerge": vmerge, "css": ";".join(css),
                              "segs": segs, "first": first})
                col += span
            # 行从新页开始：凡有分页标记的单元格，标记都在最前面（第一段是空的）。Word 往往只在第一格
            # 写标记，其余格没有标记，这些格的内容随行一起落到新页上。
            split = [c for c in cells if len(c["segs"]) > 1]
            starts_new = bool(split) and all(not c["segs"][0] for c in split)
            if starts_new:
                for c in cells:
                    if len(c["segs"]) == 1:
                        c["segs"] = [[], *c["segs"]]
            nseg = max((len(c["segs"]) for c in cells), default=1)
            first = next((c.get("first") for c in cells if c.get("first")), None)
            begin = 1 if starts_new else 0
            row_note = self.note("rowSplit", id(tr)) if nseg - begin > 1 else ""
            for k in range(begin, nseg):
                seg_cells = [dict(c, html="".join(c["segs"][k]) if k < len(c["segs"]) else "") for c in cells]
                rows.append({"cells": seg_cells, "header": header and nseg == 1, "break": k > 0, "notes": row_note,
                             "first": first if k == begin or not first else {**first, "cont": True}})
        colgroup = ("<colgroup>" + "".join(f'<col style="width:{w:.1f}px">' for w in grid) + "</colgroup>") if grid else ""
        width = sum(grid)
        tstyle = f' style="width:{min(width, 4000):.1f}px"' if width else ""
        headers = [r for r in rows if r["header"]]
        parts: list[tuple[str, dict | None]] = []
        cur: list[dict] = []

        def close():
            if cur:
                first = next((r["first"] for r in cur if not r.get("repeat")), None)
                parts.append((notes + f'<table class="docx-table"{tstyle}>{colgroup}<tbody>{rows_html(cur)}</tbody></table>', first))

        for row in rows:
            if row["break"] and paginate:
                if any(not r.get("repeat") for r in cur):
                    close()
                    parts.append((BREAK, None))
                    cur = [dict(h, repeat=True) for h in headers if h is not row]
                elif not cur:
                    parts.append((BREAK, None))
            cur.append(row)
        close()
        return parts

    # ---------- 页眉页脚 ----------
    def part_html(self, sect: Section, kind: str, page_in_sect: int, page_no: int) -> str:
        if page_in_sect == 1 and sect.title_pg:
            typ = "first"
        elif self.even_odd and page_no % 2 == 0:
            typ = "even"
        else:
            typ = "default"
        rel = self.pkg.rels.get(sect.refs.get((kind, typ)) or "")
        if not rel or rel["external"]:
            return ""
        target = rel["target"]
        if target in self.part_cache:
            return self.part_cache[target]
        root, rels = self.pkg.parts.get(target, (None, {}))
        if root is None:
            return ""
        outer = (self.part, self.rels, self.fields, self.comment_open)
        self.part, self.rels, self.fields, self.comment_open = kind, rels, [], []
        self.note(kind, target)
        try:
            out = self.blocks_inline(root)
        finally:
            self.part, self.rels, self.fields, self.comment_open = outer
        self.part_cache[target] = out
        return out

    # ---------- 主流程 ----------
    def render(self) -> list[dict]:
        body = self.pkg.root.find(W + "body")
        sections: list[Section] = []
        for sp in [s for s in (p.find(W + "pPr/" + W + "sectPr") for p in body_paragraphs(body)) if s is not None] \
                + [body.find(W + "sectPr")]:
            sections.append(Section(sp, sections[-1] if sections else None))
        si = 0
        self.sect = sections[0]
        self.new_page("start")
        if not self.has_rendered:
            self.pages[-1]["notes"] += self.note("estimatedPages", "doc")
        for el in self.body_blocks(body):
            if el.tag == W + "p":
                for tok in self.paragraph(el, paginate=True):
                    if tok[0] == "html":
                        self.emit(tok[1], tok[2])
                    elif tok[0] == "break":
                        if tok[1] != "rendered" or self.has_rendered:
                            self.brk(tok[1])
                    elif tok[0] == "section":
                        si += 1
                        prev, self.sect = self.sect, sections[min(si, len(sections) - 1)]
                        # 连续分节符留在本页，只换之后各页的页眉页脚；纸张大小或方向变了必须换页
                        if self.sect.type in ("continuous", "nextColumn") and self.sect.same_geometry(prev):
                            continue
                        if self.pages[-1]["content"]:
                            self.new_page("section")
                        else:
                            self.pages[-1]["sect"] = self.sect
            elif el.tag == W + "tbl":
                for part, first in self.table(el, paginate=True):
                    if part == BREAK:
                        self.brk("table")
                    else:
                        self.emit(part, first)
            elif el.tag == W + "commentRangeEnd":
                self.close_comment(el)
            elif el.tag == W + "commentRangeStart":
                self.comment_open.append(el.get(W + "id", ""))
            else:
                self.pages[-1]["notes"] += self.note("unknown", _local(el.tag), _local(el.tag))
        while len(self.pages) > 1 and not self.pages[-1]["content"]:
            self.pages.pop()
        return self.assemble()

    def body_blocks(self, container: ET.Element):
        """正文的块级元素；块级内容控件与 customXml 拆开，里面的段落带上只读原因。"""
        for el in container:
            if el.tag == W + "sdt":
                mark = self.note("contentControl", id(el))
                content = el.find(W + "sdtContent")
                if content is not None:
                    outer = self.block_ro, self.block_notes
                    self.block_ro = self.block_ro | {"contentControl"}
                    self.block_notes += mark
                    try:
                        yield from self.body_blocks(content)
                    finally:
                        self.block_ro, self.block_notes = outer
            elif el.tag == W + "customXml":
                yield from self.body_blocks(el)
            elif el.tag in SKIP_BLOCK:
                continue
            else:
                yield el

    def assemble(self) -> list[dict]:
        total = len(self.pages)
        sect_pages: dict[int, int] = {}
        for page in self.pages:
            sect_pages[id(page["sect"])] = sect_pages.get(id(page["sect"]), 0) + 1
        out, seen_keys = [], {}
        prev_sect, in_sect, shown = None, 0, 0
        for n, page in enumerate(self.pages, 1):
            sect = page["sect"]
            if sect is not prev_sect:
                in_sect = 0
                if sect.pg_start is not None:
                    shown = sect.pg_start - 1
                prev_sect = sect
            in_sect += 1
            shown += 1
            if sect.cols > 1:
                page["notes"] += self.note("columns", id(sect))

            def locate(match):
                self.diag[match.group(1)]["pages"].add(n)
                return ""

            DIAG_MARK.sub(locate, page["notes"])
            page["blocks"] = [DIAG_MARK.sub(locate, block) for block in page["blocks"]]
            header = self.part_html(sect, "header", in_sect, n)
            footer = self.part_html(sect, "footer", in_sect, n)
            key = page_key(page["first"], page["blocks"])
            seen_keys[key] = seen_keys.get(key, 0) + 1
            if seen_keys[key] > 1:
                key = f"{key}-{seen_keys[key]}"
            m = sect.margin
            style = (f"width:{sect.w:.1f}px;min-height:{sect.h:.1f}px;"
                     f"padding:{m['top']:.1f}px {m['right']:.1f}px {m['bottom']:.1f}px {m['left']:.1f}px")
            body = "".join(page["blocks"])
            parts = []
            if header:
                parts.append(f'<div class="docx-header" style="top:{sect.header_dist:.1f}px;left:{m["left"]:.1f}px;'
                             f'right:{m["right"]:.1f}px">{header}</div>')
            parts.append(f'<div class="docx-body">{body}</div>')
            if footer:
                parts.append(f'<div class="docx-footer" style="bottom:{sect.footer_dist:.1f}px;left:{m["left"]:.1f}px;'
                             f'right:{m["right"]:.1f}px">{footer}</div>')
            label = fmt_number(shown, sect.pg_fmt) or str(shown)
            inner = ("".join(parts).replace("\x00PAGE\x00", _esc(label)).replace("\x00NUMPAGES\x00", str(total))
                     .replace("\x00SECTIONPAGES\x00", str(sect_pages[id(sect)])))
            out.append({"n": n, "key": key, "title": page_title(body) or str(n), "w": round(sect.w, 1),
                        "h": round(sect.h, 1), "orient": sect.orient, "label": label,
                        "html": (f'<section class="page docx-page docx-{sect.orient}" data-page="{n}" '
                                 f'data-key="{_esc(key)}" style="{style}">{inner}</section>')})
        return out


def body_paragraphs(container: ET.Element):
    """正文层（含块级内容控件里）的段落：只有它们的 sectPr 结束一节。"""
    for el in container:
        if el.tag == W + "p":
            yield el
        elif el.tag == W + "sdt":
            content = el.find(W + "sdtContent")
            if content is not None:
                yield from body_paragraphs(content)
        elif el.tag == W + "customXml":
            yield from body_paragraphs(el)


def top_textboxes(el: ET.Element) -> list[ET.Element]:
    """一个绘图 / VML 里的全部文本框（不含文本框里再嵌的文本框）。"""
    out, stack = [], list(reversed(list(el)))
    while stack:
        x = stack.pop()
        if x.tag == W + "txbxContent":
            out.append(x)
            continue
        stack.extend(reversed(list(x)))
    return out


def border_css(b: dict) -> list[str]:
    """边框：insideH / insideV 先写，四边后写覆盖它们；单元格的 tcBorders 已在合并时覆盖表格的同名边。"""
    out = []
    for side, names in (("h", ("insideH",)), ("v", ("insideV",)), ("top", ("top",)), ("right", ("right", "end")),
                        ("bottom", ("bottom",)), ("left", ("left", "start"))):
        el = next((b[n] for n in names if n in b), None)
        if el is None:
            continue
        val = el.get(W + "val", "single")
        if val in ("nil", "none"):
            spec = "none"
        else:
            sz = max(_num(el.get(W + "sz"), 4, 0, 96) / 8 * 96 / 72, 1)
            color = hexcolor(el.get(W + "color")) or "#000"
            style = "double" if val == "double" else "dashed" if "dash" in val else "dotted" if "dot" in val else "solid"
            spec = f"{sz:.1f}px {style} {color}"
        if side == "h":
            out += [f"border-top:{spec}", f"border-bottom:{spec}"]
        elif side == "v":
            out += [f"border-left:{spec}", f"border-right:{spec}"]
        else:
            out.append(f"border-{side}:{spec}")
    return out


def rows_html(rows: list[dict]) -> str:
    """一段连续行 → <tr>。vMerge 只并进本段里以 restart（或段首续格）开头的格，重复的表头行不参与合并；
    重复的表头行去掉定位属性（同一段落不在两页上都能定位）。"""
    origin: dict[int, dict] = {}
    spans: dict[int, int] = {}
    skip: set[int] = set()
    for row in rows:
        here = set()
        for c in row["cells"]:
            here.add(c["col"])
            if row.get("repeat"):
                origin.pop(c["col"], None)
            elif c["vmerge"] == "continue" and c["col"] in origin:
                o = origin[c["col"]]
                spans[id(o)] = spans.get(id(o), 1) + 1
                skip.add(id(c))
            elif c["vmerge"] in ("restart", "continue"):
                origin[c["col"]] = c
            else:
                origin.pop(c["col"], None)
        for col in [k for k in origin if k not in here]:
            origin.pop(col)
    out = []
    for row in rows:
        tds = []
        for c in row["cells"]:
            if id(c) in skip:
                continue
            a = []
            if c["span"] > 1:
                a.append(f'colspan="{c["span"]}"')
            if spans.get(id(c), 1) > 1:
                a.append(f'rowspan="{spans[id(c)]}"')
            if c["css"]:
                a.append(f'style="{_esc(c["css"])}"')
            body = c.get("html", "")
            if row.get("repeat"):
                body = re.sub(r'\sdata-docx-(?:p|pid|r)="[^"]*"', "", body)
            tds.append(f"<td {' '.join(a)}>{body or '<br>'}</td>")
        cls = (["docx-th"] if row["header"] else []) + (["docx-repeat"] if row.get("repeat") else [])
        attr = f' class="{" ".join(cls)}"' if cls else ""
        out.append(row.get("notes", "") + f"<tr{attr}>{''.join(tds)}</tr>")
    return "".join(out)


_DECOR = re.compile(r'<(span|sup)\b[^>]*class="[^"]*docx-(?:num|mark)[^"]*"[^>]*>.*?</\1>', re.S)


def visible_text(html_frag: str) -> str:
    """页面片段的正文文字：去掉编号、标记等不可选中的装饰。"""
    body = _DECOR.sub("", html_frag)
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", body))).strip()


def page_lines(html_frag: str) -> str:
    """页面正文按段落分行的纯文字（批次 diff 用）。"""
    body = _DECOR.sub("", html_frag)
    body = re.sub(r"</div>|<br>|</tr>", "\n", body)
    lines = (re.sub(r"[ \t]+", " ", html.unescape(re.sub(r"<[^>]+>", " ", x))).strip() for x in body.split("\n"))
    return "\n".join(x for x in lines if x)


def page_key(first: dict | None, blocks: list[str]) -> str:
    """稳定页面键：页首段落的 w14:paraId（续页加 ~c）；没有 paraId 时用页首正文的摘要（不含自动编号）。

    页面增删、重排后同一段内容开头的页仍拿到同一个键，批注跟着原页走。
    """
    if first and first.get("pid"):
        return f"w{first['pid']}" + ("~c" if first.get("cont") else "")
    text = re.sub(r"\s+", "", visible_text("".join(blocks[:3])))[:200]
    return "t" + hashlib.sha1(text.encode("utf-8")).hexdigest()[:10]


def page_title(body: str) -> str:
    """页标题：本页第一个标题段落；没有就取正文开头（跳过续表重复的表头行）。"""
    m = re.search(r'<div class="docx-p[^"]*docx-h[^"]*"[^>]*>(.*?)</div>', body, re.S)
    if m:
        return visible_text(m.group(1))[:60]
    return visible_text(re.sub(r'<tr class="[^"]*docx-repeat[^"]*">.*?</tr>', " ", body, flags=re.S))[:60]


# ---------- 段落索引与批注定位 ----------
_VOID = frozenset({"br", "img", "hr", "col", "wbr", "input", "meta", "link"})
_PARA_ATTR = re.compile(r'data-docx-p="(\d+)"(?: data-docx-pid="[^"]*")?')


def text_digest(text: str) -> str:
    """段落完整正文的 SHA-1 前 12 位；保留空白，以免把已变化的字符偏移当作仍然有效。"""
    return hashlib.sha1(text.encode("utf-8", "surrogatepass")).hexdigest()[:12]


class _ParaScan(HTMLParser):
    """按文档顺序收集一页里各段落片段的正文文字（不含自动编号、标记等不可选中的装饰）。

    与页面里的算法一致：片段文字 = 段落 div 内全部文本节点依次相连，跳过 .docx-num / .docx-mark。
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.stack: list[object] = []          # 每个未闭合元素：片段记录（dict）/ "decor" / None
        self.frags: list[dict] = []

    def handle_starttag(self, tag, attrs):
        if tag in _VOID:
            return
        a = dict(attrs)
        cls = (a.get("class") or "").split()
        if tag == "div" and a.get("data-docx-p", "").isdigit():
            frag = {"n": int(a["data-docx-p"]), "id": a.get("data-docx-pid") or None, "text": []}
            self.frags.append(frag)
            self.stack.append(frag)
        else:
            self.stack.append("decor" if "docx-num" in cls or "docx-mark" in cls else None)

    def handle_endtag(self, tag):
        if tag not in _VOID and self.stack:
            self.stack.pop()

    def handle_data(self, data):
        if "decor" in self.stack:
            return
        for item in reversed(self.stack):
            if isinstance(item, dict):
                item["text"].append(data)
                return


def index_paragraphs(pages: list[dict]) -> list[dict]:
    """给各页的段落片段补上 data-docx-d / data-docx-o，并返回段落索引。

    索引每项 {"n": 段落序号, "id": paraId 或 None, "digest": 文本摘要, "frags": [{"page", "start"}]}，
    按首次出现的顺序排列；frags 是这段落落在各页上的片段及其起始字符偏移（按 Unicode 码点计）。
    """
    scans = []
    paras: dict[int, dict] = {}
    for pg in pages:
        scan = _ParaScan()
        scan.feed(pg["html"])
        scan.close()
        for frag in scan.frags:
            text = "".join(frag["text"])
            p = paras.setdefault(frag["n"], {"n": frag["n"], "id": frag["id"], "text": "", "frags": []})
            frag["start"] = len(p["text"])
            p["frags"].append({"page": pg["n"], "start": frag["start"]})
            p["text"] += text
        scans.append(scan.frags)
    for p in paras.values():
        p["digest"] = text_digest(p.pop("text"))
    for pg, frags in zip(pages, scans):
        it = iter(frags)

        def mark(m: re.Match) -> str:
            frag = next(it, None)
            if frag is None or str(frag["n"]) != m.group(1):   # 与扫描结果对不上：不写属性，不猜
                return m.group(0)
            extra = f' data-docx-d="{paras[frag["n"]]["digest"]}"'
            if frag["start"]:
                extra += f' data-docx-o="{frag["start"]}"'
            return m.group(0) + extra

        pg["html"] = _PARA_ATTR.sub(mark, pg["html"])
    return [{"n": p["n"], "id": p["id"], "digest": p["digest"], "frags": p["frags"]} for p in paras.values()]


def usable_anchor(a: object) -> bool:
    """批注的 docx 定位是不是本版本读得懂、能用来找段落的（版本更高或结构不全的原样保留、不使用）。"""
    if not isinstance(a, dict) or a.get("v") != ANCHOR_VERSION or isinstance(a.get("v"), bool):
        return False
    para = a.get("para")
    return (isinstance(para, dict) and isinstance(para.get("n"), int) and not isinstance(para.get("n"), bool)
            and isinstance(para.get("digest"), str))


def relocate(anchor: dict, paragraphs: list[dict]) -> dict:
    """按批注的段落定位，在当前版本的段落索引里找回这段落。

    paraId 在当前文档里唯一时直接用它；没有 paraId、paraId 重复或已不存在时，用段落序号与文本摘要互证：
    摘要唯一即认定（序号可以因前面增删段落而变），摘要有多处相同时要求序号也一致，否则算不确定。
    跨段范围独立核对 endPara；任一端失效或起止顺序不再成立时不猜测页码。
    返回 {"status": ok / ambiguous / missing, "how": paraId / digest / ordinal / None,
    "why": None / noId / duplicateId / idGone（为什么没用 paraId）, "para": 索引项或 None,
    "page": 现在所在页或 None, "changed": 起止段落文字是否已变（只在按 paraId 找到时可能为 True）}，
    跨段范围成功时另带 endPara 索引项。
    """
    got = _relocate_para(anchor, paragraphs)
    end = anchor.get("endPara")
    if got["status"] != "ok" or not isinstance(end, dict):
        return got
    last = _relocate_para({"para": end, "start": anchor.get("end", 0)}, paragraphs)
    if last["status"] != "ok":
        return {**got, "status": last["status"], "page": None, "why": last["why"],
                "changed": got["changed"] or last["changed"]}
    if last["para"]["n"] <= got["para"]["n"]:
        return {**got, "status": "ambiguous", "page": None}
    got["endPara"] = last["para"]
    got["changed"] = got["changed"] or last["changed"]
    if not got["why"] and last["why"]:
        got["why"], got["how"] = last["why"], last["how"]
    return got


def _relocate_para(anchor: dict, paragraphs: list[dict]) -> dict:
    """核对一个端点所在的段落；跨段范围的两个端点用同一条匹配规则。"""
    para = anchor["para"]
    pid, n, dg = para.get("id"), para["n"], para["digest"]
    why = None
    if pid:
        same = [p for p in paragraphs if p["id"] == pid]
        if len(same) == 1:
            return _located(anchor, same[0], "paraId", None, same[0]["digest"] != dg)
        why = "duplicateId" if same else "idGone"
    else:
        why = "noId"
    same = [p for p in paragraphs if p["digest"] == dg]
    if len(same) == 1:
        return _located(anchor, same[0], "digest", why, False)
    hit = next((p for p in same if p["n"] == n), None)
    if hit is not None:
        return _located(anchor, hit, "ordinal", why, False)
    return {"status": "ambiguous" if same else "missing", "how": None, "why": why, "para": None, "page": None,
            "changed": False}


def _located(anchor: dict, para: dict, how: str, why: str | None, changed: bool) -> dict:
    # 文字已编辑的跨页段落无法再用旧偏移判断是哪一片；首片点/框没有显式 start，也不能猜。
    if changed and len({frag["page"] for frag in para["frags"]}) > 1:
        return {"status": "ambiguous", "how": how, "why": why, "para": para, "page": None, "changed": True}
    start = anchor.get("start")
    start = start if isinstance(start, int) and not isinstance(start, bool) and start > 0 else 0
    page = para["frags"][0]["page"]
    for frag in para["frags"]:      # 跨页段落：落在起始偏移所在的那一片上
        if frag["start"] <= start:
            page = frag["page"]
    return {"status": "ok", "how": how, "why": why, "para": para, "page": page, "changed": changed}


BASE_CSS = """
:host{--docx-fallback:"Times New Roman","SimSun","Songti SC",serif}
section.docx-page{position:relative;box-sizing:border-box;background:#fff;color:#000;font-family:var(--docx-fallback);
  font-size:10.5pt;line-height:1.15;overflow:visible;text-align:left}
section.docx-page .docx-body{position:relative}
section.docx-page .docx-p{white-space:pre-wrap;overflow-wrap:break-word;margin:0;min-height:1em}
section.docx-page .docx-cont{text-indent:0!important}
section.docx-page .docx-h{font-weight:700}
section.docx-page .docx-tab{display:inline-block;min-width:2em;white-space:pre}
section.docx-page .docx-num,section.docx-page .docx-mark{user-select:none;-webkit-user-select:none}
section.docx-page .docx-mark{color:#666;font-size:.75em}
section.docx-page .docx-comment{color:#b26b00;cursor:help}
section.docx-page .docx-cmt{background-image:linear-gradient(transparent 85%,rgba(255,170,0,.55) 85%)}
section.docx-page .docx-ins{color:#1a6e32;text-decoration-line:underline;text-decoration-color:#1a6e32}
section.docx-page .docx-del{color:#b42318;text-decoration-line:line-through;text-decoration-color:#b42318}
section.docx-page .docx-revision{background-color:#fff1c2;text-decoration-line:underline;text-decoration-style:dotted}
section.docx-page .docx-field{background-color:rgba(0,0,0,.05)}
section.docx-page .docx-link{color:#0b57d0;text-decoration-line:underline}
section.docx-page .docx-txbx{display:inline-block;vertical-align:top;border:1px dashed #8a8f98;padding:4px 6px;margin:2px;white-space:normal}
section.docx-page .docx-header,section.docx-page .docx-footer{position:absolute;font-size:9pt;color:#333}
section.docx-page table.docx-table{border-collapse:collapse;table-layout:fixed;margin:2px 0;max-width:100%}
section.docx-page table.docx-table td{padding:2px 6px;vertical-align:top;overflow-wrap:break-word}
section.docx-page .docx-img{display:inline-block;vertical-align:bottom;max-width:100%;background-size:100% 100%;background-repeat:no-repeat}
section.docx-page .docx-math{font-family:"Cambria Math",serif;font-style:italic}
"""


def render(path: Path) -> dict:
    """渲染 DOCX：{"title", "css", "pages", "diagnostics", "pagination", "appPages", "application", "lang",
    "paragraphs", "fingerprint"}。paragraphs 见 index_paragraphs()；fingerprint 是源文件的 SHA-256。

    pagination：verified / unverified / estimated（见模块说明）。diagnostics 按支持矩阵的顺序排列，
    每项 {"code", "count", "display", "writeback", "pages", "examples"}。
    """
    pkg = Package(path)
    try:
        r = Renderer(pkg)
        pages = r.render()
        paragraphs = index_paragraphs(pages)
    except DocxError:
        raise
    except (RecursionError, MemoryError, ValueError, KeyError, IndexError, TypeError, AttributeError) as exc:
        raise DocxError("internal", f"{type(exc).__name__}: {exc}"[:200]) from None
    app_pages, application = pkg.app_info()
    if not r.has_rendered:
        pagination = "estimated"
    elif app_pages == len(pages):
        pagination = "verified"
    else:
        pagination = "unverified"
    title = ""
    if pkg.core is not None:
        t = next((x for x in pkg.core.iter() if _local(x.tag) == "title"), None)
        title = (t.text or "").strip()[:200] if t is not None else ""
    lang = ""
    if pkg.styles is not None:
        lg = next(pkg.styles.iter(W + "lang"), None)
        if lg is not None:
            lang = lg.get(W + "eastAsia") or lg.get(W + "val") or ""
    diags = []
    for code in sorted(r.diag, key=lambda c: DIAG_ORDER.index(c) if c in DIAG_ORDER else len(DIAG_ORDER)):
        d = r.diag[code]
        display, writeback = SUPPORT.get(code, ("hidden", "no"))
        diags.append({"code": code, "count": d["count"], "display": display, "writeback": writeback,
                      "pages": sorted(d["pages"]), "examples": d["examples"]})
    return {"title": title, "css": BASE_CSS + "\n".join(r.media_css), "pages": pages, "diagnostics": diags,
            "pagination": pagination, "appPages": app_pages, "application": application,
            "lang": re.sub(r"[^A-Za-z0-9-]", "", lang)[:20], "paragraphs": paragraphs,
            "fingerprint": pkg.fingerprint}


def page_texts(path: Path) -> dict[int, str]:
    """逐页正文纯文字（对应句查找用）。"""
    return {p["n"]: visible_text(p["html"]) for p in render(path)["pages"]}
