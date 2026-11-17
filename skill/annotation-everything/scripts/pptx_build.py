#!/usr/bin/env python3
"""内置 PPTX 制作器（issue #180）：把结构化 deck 规格写成可编辑的 .pptx 演示文稿。

这是随技能发布的制作能力（issue #180）：生成推理与内容判断由宿主 Agent 执行，
本入口只负责把确认过的内容组织结果（`ae-pptx-deck/1` 规格）写成 OOXML 包——
可编辑标题、正文与普通形状、有效命名版式与占位符、主题色与标题/正文字体、
幻灯片章节，以及母版承载的页脚、标志与页码。只用 Python 标准库。

构建是确定性的：同样的规格得到同样的字节（固定 ZIP 时间戳、固定内部 ID 与
GUID），同一份内容重建前后哈希不变，核验记录据此判断证据是否仍然适用。

用法：
    python3 pptx_build.py --deck deck.json --out deck.pptx [--locale zh-CN] [--json]

退出码：0 构建成功；2 规格、输入或用法错误（不做部分写入：规格校验全部通过
后才落盘）。

边界：不读原始材料、不做内容判断；幻灯片身份从 deck key 确定性派生，重排
页序不挪身份。渲染与逐页看图见 generation-pptx 指引，核验记录字段见
report-record 的 deliverables 约定。
"""
from __future__ import annotations

import argparse
import hashlib
import io
import json
import math
import re
import struct
import sys
import uuid
import zlib
import zipfile
from pathlib import Path
from xml.sax.saxutils import escape as _xml_escape

sys.path.insert(0, str(Path(__file__).resolve().parent))

import fsutil
import i18n

DECK_SCHEMA = "ae-pptx-deck/1"
BUILD_SCHEMA = "ae-pptx-build/1"

# 确定性构建：固定时间戳与内部常量 ID，保证同规格同字节。
FIXED_TIME = (2026, 1, 1, 0, 0, 0)
FIXED_STAMP = "2026-01-01T00:00:00Z"

P = "http://schemas.openxmlformats.org/presentationml/2006/main"
A = "http://schemas.openxmlformats.org/drawingml/2006/main"
C = "http://schemas.openxmlformats.org/drawingml/2006/chart"
R = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
PKG_REL = "http://schemas.openxmlformats.org/package/2006/relationships"
CT = "http://schemas.openxmlformats.org/package/2006/content-types"

CT_PRESENTATION = "application/vnd.openxmlformats-officedocument.presentationml.presentation.main+xml"
CT_SLIDE = "application/vnd.openxmlformats-officedocument.presentationml.slide+xml"
CT_MASTER = "application/vnd.openxmlformats-officedocument.presentationml.slideMaster+xml"
CT_LAYOUT = "application/vnd.openxmlformats-officedocument.presentationml.slideLayout+xml"
CT_THEME = "application/vnd.openxmlformats-officedocument.theme+xml"
CT_CHART = "application/vnd.openxmlformats-officedocument.drawingml.chart+xml"
CT_NOTES_MASTER = "application/vnd.openxmlformats-officedocument.presentationml.notesMaster+xml"
CT_NOTES_SLIDE = "application/vnd.openxmlformats-officedocument.presentationml.notesSlide+xml"
CT_CORE = "application/vnd.openxmlformats-package.core-properties+xml"
CT_APP = "application/vnd.openxmlformats-officedocument.extended-properties+xml"

# 命名版式表：key 是 deck 规格里的 layout 值，name 是最终文件的版式名，
# type 是 OOXML 版式类型，placeholders 声明该版式承载的占位符。
LAYOUTS: dict[str, dict] = {
    "title": {"type": "title", "name": "标题幻灯片", "placeholders": ("ctrTitle", "subTitle")},
    "sectionHeader": {"type": "secHead", "name": "节标题", "placeholders": ("title", "body")},
    "titleAndContent": {"type": "obj", "name": "标题和内容", "placeholders": ("title", "body")},
    "titleOnly": {"type": "titleOnly", "name": "仅标题", "placeholders": ("title",)},
    "blank": {"type": "blank", "name": "空白", "placeholders": ()},
}
LAYOUT_ORDER = ("title", "sectionHeader", "titleAndContent", "titleOnly", "blank")
TITLE_LAYOUTS = ("title", "sectionHeader", "titleAndContent", "titleOnly")
BODY_LAYOUTS = ("sectionHeader", "titleAndContent")

# 主题色槽位（deck 规格写法与 schemeClr 取值一一对应）。
COLOR_SLOTS = ("dk1", "lt1", "dk2", "lt2", "accent1", "accent2", "accent3",
               "accent4", "accent5", "accent6", "hlink", "folHlink")

KEY_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
HEX_RE = re.compile(r"^[0-9A-Fa-f]{6}$")

# 幻灯片身份从 deck key 确定性派生：重排页序不挪身份，已有审阅定位不受重建影响。
SLIDE_ID_BASE = 256
SLIDE_ID_SPAN = 2 ** 31 - SLIDE_ID_BASE - 1024
SLIDE_ID_PROBE_STEP = 7919

MASTER_ID = 2147483648
LAYOUT_ID_BASE = 2147483649
NOTES_MASTER_ID = 2147483648 + 64

# 页码字段的固定 GUID（确定性构建的一部分）。
_FLD_SLIDENUM_GUID = "{5BD2E6AB-1AF4-4D41-9A79-9ED000000001}"
_DEFAULT_SIZE = (12192000, 6858000)
_NOTES_SIZE = (6858000, 9144000)
_LOGO_MARGIN = 457200
_LOGO_WIDTH = 600000

_BULLET_CHARS = ("•", "–", "▪", "•", "–", "»", "▪", "•", "–")

_UUID_NAMESPACE = uuid.UUID("6ba7b810-9dad-11d1-80b4-00c04fd430c8")


class DeckSpecError(Exception):
    """deck 规格错误；code 语言无关，文案随 locale 在 CLI 层渲染。"""

    def __init__(self, code: str, **params: object):
        super().__init__(code)
        self.code = code
        self.params = params


def _esc(value: object) -> str:
    return _xml_escape(str(value), {'"': "&quot;"})


# ── 规格校验 ────────────────────────────────────────────────────────────────

def _need(condition: bool, code: str, **params: object) -> None:
    if not condition:
        raise DeckSpecError(code, **params)


def _str_field(container: dict, field: str, *, code: str) -> str:
    value = container.get(field)
    _need(isinstance(value, str) and value.strip(), code, field=field)
    return str(value)


def _validate_theme(raw: object) -> dict:
    _need(isinstance(raw, dict), "themeRequired")
    colors = raw.get("colors")
    _need(isinstance(colors, dict), "themeRequired")
    unknown = [k for k in colors if k not in COLOR_SLOTS]
    _need(not unknown, "colorSlot", slots="、".join(str(k) for k in unknown))
    missing = [k for k in COLOR_SLOTS if k not in colors]
    _need(not missing, "themeColorsMissing", slots="、".join(missing))
    bad = [k for k in COLOR_SLOTS
           if not (isinstance(colors[k], str) and HEX_RE.match(colors[k]))]
    _need(not bad, "hexColor", slots="、".join(bad))
    fonts = raw.get("fonts")
    _need(isinstance(fonts, dict), "fontsRequired")
    out_fonts: dict[str, dict] = {}
    for role in ("major", "minor"):
        entry = fonts.get(role)
        _need(isinstance(entry, dict), "fontsRequired")
        out_fonts[role] = {"latin": _str_field(entry, "latin", code="fontField").strip(),
                           "ea": _str_field(entry, "ea", code="fontField").strip()}
    return {"colors": {k: str(colors[k]).upper() for k in COLOR_SLOTS}, "fonts": out_fonts}


def _validate_size(raw: object) -> tuple[int, int]:
    if raw is None:
        return _DEFAULT_SIZE
    _need(isinstance(raw, dict), "sizeBad")
    width, height = raw.get("width"), raw.get("height")
    for value in (width, height):
        _need(isinstance(value, int) and not isinstance(value, bool)
              and 0 < value <= 2 ** 31, "sizeBad")
    return int(width), int(height)


def _image_size(data: bytes) -> tuple[int, int, str]:
    """返回 (宽, 高, 扩展名)；只接受 PNG 与 JPEG，其余格式明确拒绝。"""
    if data[:8] == b"\x89PNG\r\n\x1a\n" and len(data) >= 24:
        width, height = struct.unpack(">II", data[16:24])
        return width, height, "png"
    if data[:2] == b"\xff\xd8":
        index = 2
        while index + 9 < len(data):
            if data[index] != 0xFF:
                index += 1
                continue
            marker = data[index + 1]
            if marker in (0xD8, 0x01) or 0xD0 <= marker <= 0xD7:
                index += 2
                continue
            length = struct.unpack(">H", data[index + 2:index + 4])[0]
            if 0xC0 <= marker <= 0xCF and marker not in (0xC4, 0xC8, 0xCC):
                height, width = struct.unpack(">HH", data[index + 5:index + 9])
                return width, height, "jpg"
            index += 2 + length
    raise DeckSpecError("logoFormat")


def _validate_master(raw: object, base_dir: Path) -> dict:
    if raw is None:
        return {"footer": False, "footerText": "", "pageNumber": False, "logo": None}
    _need(isinstance(raw, dict), "masterBad")
    footer, page_number = raw.get("footer", False), raw.get("pageNumber", False)
    _need(isinstance(footer, bool) and isinstance(page_number, bool), "masterBad")
    footer_text = raw.get("footerText", "")
    _need(isinstance(footer_text, str), "masterBad")
    logo_raw = raw.get("logo")
    logo = None
    if logo_raw is not None:
        _need(isinstance(logo_raw, dict), "masterBad")
        path_raw = logo_raw.get("path")
        _need(isinstance(path_raw, str) and path_raw.strip(), "logoMissing", path=str(path_raw))
        path = Path(path_raw)
        if not path.is_absolute():
            path = base_dir / path
        try:
            data = path.read_bytes()
        except OSError:
            raise DeckSpecError("logoMissing", path=path_raw) from None
        width, height, ext = _image_size(data)
        _need(width > 0 and height > 0, "logoSize")
        name = logo_raw.get("name")
        _need(isinstance(name, str) or name is None, "masterBad")
        logo = {"data": data, "ext": ext, "width": width, "height": height,
                "name": str(name) if name else "标志"}
    return {"footer": bool(footer), "footerText": str(footer_text),
            "pageNumber": bool(page_number), "logo": logo}


def _validate_body_items(raw: object) -> list[dict]:
    if raw is None:
        return []
    _need(isinstance(raw, list), "bodyBad")
    items: list[dict] = []
    for entry in raw:
        if isinstance(entry, str):
            _need(bool(entry.strip()), "bodyBad")
            items.append({"text": entry, "level": 0, "bold": False})
            continue
        _need(isinstance(entry, dict), "bodyBad")
        text = entry.get("text")
        _need(isinstance(text, str) and bool(text.strip()), "bodyBad")
        level = entry.get("level", 0)
        _need(isinstance(level, int) and not isinstance(level, bool)
              and 0 <= level <= 8, "bodyLevel", level=level)
        bold = entry.get("bold", False)
        _need(isinstance(bold, bool), "bodyBad")
        items.append({"text": text, "level": int(level), "bold": bool(bold)})
    return items


def _validate_shapes(raw: object, canvas: tuple[int, int]) -> list[dict]:
    if raw is None:
        return []
    _need(isinstance(raw, list), "shapeBad")
    width, height = canvas
    shapes: list[dict] = []
    for entry in raw:
        _need(isinstance(entry, dict), "shapeBad")
        kind = entry.get("shape")
        _need(kind in ("rect", "ellipse"), "shapeBad", shape=str(kind))
        geo = {}
        for field in ("x", "y", "cx", "cy"):
            value = entry.get(field)
            _need(isinstance(value, int) and not isinstance(value, bool),
                  "shapeBad", field=field)
            geo[field] = int(value)
        _need(geo["cx"] > 0 and geo["cy"] > 0, "shapeBad", field="cx/cy")
        _need(0 <= geo["x"] and 0 <= geo["y"]
              and geo["x"] + geo["cx"] <= width and geo["y"] + geo["cy"] <= height,
              "shapeOutside", x=geo["x"], y=geo["y"], cx=geo["cx"], cy=geo["cy"])
        fill = entry.get("fill", "accent1")
        _need(fill in COLOR_SLOTS, "colorSlot", slots=str(fill))
        line = entry.get("line")
        line_out = None
        if line is not None:
            _need(isinstance(line, dict), "shapeBad")
            color = line.get("color")
            _need(color in COLOR_SLOTS, "colorSlot", slots=str(color))
            line_width = line.get("width", 12700)
            _need(isinstance(line_width, int) and not isinstance(line_width, bool)
                  and 0 < line_width <= 3175000, "lineWidth", width=line_width)
            line_out = {"color": color, "width": int(line_width)}
        text = entry.get("text", "")
        _need(isinstance(text, str), "shapeBad")
        name = entry.get("name")
        _need(isinstance(name, str) or name is None, "shapeBad")
        shapes.append({"shape": str(kind), **geo, "fill": str(fill), "line": line_out,
                       "text": text, "name": str(name) if name else ""})
    return shapes


def _validate_charts(raw: object, canvas: tuple[int, int]) -> list[dict]:
    """首版图表：column / bar / line / pie 原生图表，保留可编辑数据。"""
    if raw is None:
        return []
    _need(isinstance(raw, list), "chartBad")
    width, height = canvas
    charts: list[dict] = []
    for entry in raw:
        _need(isinstance(entry, dict), "chartBad")
        kind = entry.get("chart")
        _need(kind in ("column", "bar", "line", "pie"), "chartUnknown", chart=str(kind))
        geo = {}
        for field in ("x", "y", "cx", "cy"):
            value = entry.get(field)
            _need(isinstance(value, int) and not isinstance(value, bool),
                  "chartBad", field=field)
            geo[field] = int(value)
        _need(geo["cx"] > 0 and geo["cy"] > 0, "chartBad", field="cx/cy")
        _need(0 <= geo["x"] and 0 <= geo["y"]
              and geo["x"] + geo["cx"] <= width and geo["y"] + geo["cy"] <= height,
              "shapeOutside", x=geo["x"], y=geo["y"], cx=geo["cx"], cy=geo["cy"])
        categories = entry.get("categories")
        _need(isinstance(categories, list) and bool(categories), "chartDataBad")
        _need(all(isinstance(item, str) and item.strip() for item in categories),
              "chartDataBad")
        series_raw = entry.get("series")
        _need(isinstance(series_raw, list) and bool(series_raw), "chartDataBad")
        _need(len(series_raw) <= 25, "chartTooManySeries", n=len(series_raw))
        series: list[dict] = []
        for item in series_raw:
            _need(isinstance(item, dict), "chartDataBad")
            name = _str_field(item, "name", code="chartDataBad")
            values = item.get("values")
            _need(isinstance(values, list) and len(values) == len(categories),
                  "chartCountMismatch", categories=len(categories),
                  values=len(values) if isinstance(values, list) else -1)
            for value in values:
                _need(isinstance(value, (int, float)) and not isinstance(value, bool)
                      and math.isfinite(value), "chartDataBad")
            series.append({"name": name, "values": [_num(v) for v in values]})
        name = entry.get("name")
        _need(isinstance(name, str) or name is None, "chartBad")
        title = entry.get("title")
        _need(isinstance(title, str) or title is None, "chartBad")
        legend = entry.get("legend", "none")
        _need(legend in ("none", "right", "bottom", "top"), "chartLegendBad", legend=str(legend))
        labels = entry.get("labels", "none")
        if kind == "pie":
            _need(labels in ("none", "value", "percent"), "chartLabelsBad", labels=str(labels))
        elif labels == "percent":
            raise DeckSpecError("labelsPercentNeedsPie", labels=str(labels))
        else:
            _need(labels in ("none", "value"), "chartLabelsBad", labels=str(labels))
        charts.append({"chart": str(kind), "name": str(name) if name else "",
                       **geo, "categories": [str(c) for c in categories],
                       "series": series,
                       "title": str(title) if title else "",
                       "legend": str(legend), "labels": str(labels)})
    return charts


def _validate_sections(raw: object, keys: list[str]) -> list[dict]:
    if raw is None:
        return [{"name": "全部幻灯片", "keys": list(keys)}]
    _need(isinstance(raw, list) and bool(raw), "sectionsBad")
    seen: dict[str, str] = {}
    sections: list[dict] = []
    for entry in raw:
        _need(isinstance(entry, dict), "sectionsBad")
        name = _str_field(entry, "name", code="sectionsBad")
        section_keys = entry.get("keys")
        _need(isinstance(section_keys, list) and bool(section_keys), "sectionsBad")
        for key in section_keys:
            _need(isinstance(key, str) and key in keys, "sectionUnknownKey", key=str(key))
            first = seen.get(key)
            _need(first is None, "sectionDuplicateSlide", key=str(key),
                  sections=f"{first}、{name}")
            seen[key] = name
        sections.append({"name": name, "keys": [str(k) for k in section_keys]})
    missing = [k for k in keys if k not in seen]
    _need(not missing, "sectionMissingSlides", keys="、".join(missing))
    return sections


def validate_spec(spec: object, base_dir: Path | None = None) -> dict:
    """校验并归一化 deck 规格；返回构建器使用的规范化结构，失败抛 DeckSpecError。"""
    base_dir = Path(base_dir) if base_dir is not None else Path.cwd()
    _need(isinstance(spec, dict), "specType")
    _need(spec.get("schema") == DECK_SCHEMA, "schema", schema=str(spec.get("schema")))
    title = _str_field(spec, "title", code="titleRequired")
    theme = _validate_theme(spec.get("theme"))
    size = _validate_size(spec.get("size"))
    master = _validate_master(spec.get("master"), base_dir)
    slides_raw = spec.get("slides")
    _need(isinstance(slides_raw, list) and bool(slides_raw), "slidesEmpty")
    seen_keys: set[str] = set()
    slides: list[dict] = []
    for entry in slides_raw:
        _need(isinstance(entry, dict), "specType")
        key = entry.get("key")
        _need(isinstance(key, str) and KEY_RE.match(key), "slideKey", key=str(key))
        _need(key not in seen_keys, "slideKeyDup", key=str(key))
        seen_keys.add(str(key))
        layout = entry.get("layout")
        _need(layout in LAYOUTS, "layoutUnknown", layout=str(layout))
        title_text = entry.get("title")
        if layout in TITLE_LAYOUTS:
            _need(isinstance(title_text, str) and bool(title_text.strip()),
                  "slideTitleRequired", key=str(key))
        else:
            _need(title_text is None or (isinstance(title_text, str) and not title_text.strip()),
                  "slideTitleUnexpected", key=str(key), layout=str(layout))
        subtitle = entry.get("subtitle")
        if layout == "title":
            _need(subtitle is None or isinstance(subtitle, str), "bodyBad")
        else:
            _need(subtitle is None, "subtitleOnlyTitle", key=str(key))
        body = _validate_body_items(entry.get("body"))
        _need(not body or layout in BODY_LAYOUTS, "bodyLayoutMismatch",
              key=str(key), layout=str(layout))
        hide_master = entry.get("hideMasterShapes", False)
        _need(isinstance(hide_master, bool), "specType")
        notes = entry.get("notes")
        _need(notes is None or (isinstance(notes, str) and bool(notes.strip())), "notesEmpty",
              key=str(key))
        slides.append({
            "key": str(key), "layout": str(layout),
            "title": str(title_text).strip()
            if isinstance(title_text, str) and title_text.strip() else "",
            "subtitle": str(subtitle) if isinstance(subtitle, str) else "",
            "body": body, "shapes": _validate_shapes(entry.get("shapes"), size),
            "charts": _validate_charts(entry.get("charts"), size),
            "hideMasterShapes": bool(hide_master),
            "notes": str(notes) if isinstance(notes, str) else "",
        })
    sections = _validate_sections(spec.get("sections"), keys=[s["key"] for s in slides])
    return {"schema": DECK_SCHEMA, "title": title.strip(), "theme": theme, "size": size,
            "master": master, "slides": slides, "sections": sections}


# ── XML 构建 ────────────────────────────────────────────────────────────────

def _slide_ids(keys: list[str]) -> dict[str, int]:
    """deck key → 确定且互不冲突的幻灯片 ID（重排页序不改变身份）。"""
    assigned: dict[str, int] = {}
    taken: set[int] = set()
    for key in keys:
        base = zlib.crc32(key.encode("utf-8"))
        for attempt in range(1000):
            candidate = SLIDE_ID_BASE + (base + attempt * SLIDE_ID_PROBE_STEP) % SLIDE_ID_SPAN
            if candidate not in taken:
                assigned[key] = candidate
                taken.add(candidate)
                break
        else:
            raise DeckSpecError("slideKeyDup", key=key)
    return assigned


def _guid(seed: str) -> str:
    return "{%s}" % str(uuid.uuid5(_UUID_NAMESPACE, seed)).upper()


def _color_ref(slot: str) -> str:
    return f'<a:schemeClr val="{_esc(slot)}"/>'


def _text_body(paragraphs: list[str]) -> str:
    return f"<p:txBody><a:bodyPr/><a:lstStyle/>{''.join(paragraphs)}</p:txBody>"


def _para(text: str, *, level: int = 0, bold: bool = False, size: int | None = None,
          color_slot: str | None = None) -> str:
    props = 'lang="zh-CN" dirty="0"'
    if bold:
        props += ' b="1"'
    if size:
        props += f' sz="{size}"'
    inner = ""
    if color_slot:
        inner += f'<a:solidFill>{_color_ref(color_slot)}</a:solidFill>'
    props_child = f"<a:rPr {props}>{inner}</a:rPr>" if inner else f"<a:rPr {props}/>"
    lvl = f'<a:pPr lvl="{level}"/>' if level else ""
    return (f"<a:p>{lvl}<a:r>{props_child}"
            f"<a:t xml:space=\"preserve\">{_esc(text)}</a:t></a:r></a:p>")


def _geom(x: int, y: int, cx: int, cy: int) -> str:
    return (f"<a:xfrm><a:off x=\"{x}\" y=\"{y}\"/>"
            f"<a:ext cx=\"{cx}\" cy=\"{cy}\"/></a:xfrm>")


def _placeholder_sp(shape_id: int, name: str, ph_type: str, idx: int | None,
                   paragraphs: list[str], geometry: str = "",
                   *, slidenum_field: bool = False) -> str:
    ph = f'type="{ph_type}"' + (f' idx="{idx}"' if idx is not None else "")
    if slidenum_field:
        # 母版上的页码字段：PowerPoint 渲染为实际页号。
        tx_body = (f'<p:txBody><a:bodyPr/><a:lstStyle/><a:fld id="{_FLD_SLIDENUM_GUID}" '
                   'type="slidenum"><a:rPr lang="zh-CN"/><a:t>1</a:t></a:fld></p:txBody>')
    else:
        tx_body = _text_body(paragraphs)
    return (f'<p:sp><p:nvSpPr><p:cNvPr id="{shape_id}" name="{_esc(name)}"/><p:cNvSpPr/>'
            f'<p:nvPr><p:ph {ph}/></p:nvPr></p:nvSpPr>'
            f"<p:spPr>{geometry}</p:spPr>{tx_body}</p:sp>")


def _theme_xml(theme: dict) -> str:
    colors = theme["colors"]
    slots = "".join(f'<a:{k}><a:srgbClr val="{v}"/></a:{k}>' for k, v in colors.items())
    fonts = theme["fonts"]

    def font_entry(role: str) -> str:
        return (f'<a:latin typeface="{_esc(fonts[role]["latin"])}"/>'
                f'<a:ea typeface="{_esc(fonts[role]["ea"])}"/><a:cs typeface=""/>')

    solid = '<a:solidFill><a:schemeClr val="phClr"/></a:solidFill>'
    fills = solid * 3
    lines = "".join(
        f'<a:ln w="{width}" cap="flat" cmpd="sng" algn="ctr">{solid}'
        '<a:prstDash val="solid"/></a:ln>' for width in (6350, 12700, 19050))
    effects = '<a:effectStyle><a:effectLst/></a:effectStyle>' * 3
    name = "AE 主题"
    return f'''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<a:theme xmlns:a="{A}" name="{name}"><a:themeElements>
<a:clrScheme name="{name}">{slots}</a:clrScheme>
<a:fontScheme name="{name}"><a:majorFont>{font_entry("major")}</a:majorFont><a:minorFont>{font_entry("minor")}</a:minorFont></a:fontScheme>
<a:fmtScheme name="{name}"><a:fillStyleLst>{fills}</a:fillStyleLst><a:lnStyleLst>{lines}</a:lnStyleLst><a:effectStyleLst>{effects}</a:effectStyleLst><a:bgFillStyleLst>{fills}</a:bgFillStyleLst></a:fmtScheme>
</a:themeElements></a:theme>'''


def _master_xml(master: dict, size: tuple[int, int]) -> tuple[str, str]:
    """返回 (母版 XML, 母版 rels XML)；页脚、页码与标志按规格落在母版上。"""
    width, height = size
    shapes: list[str] = []
    rels: list[tuple[str, str, str]] = []
    shapes.append(_placeholder_sp(
        2, "标题占位符", "title", None, [],
        _geom(800000, 400000, width - 1600000, 1200000)))
    shapes.append(_placeholder_sp(
        3, "内容占位符", "body", 1, [],
        _geom(800000, 1800000, width - 1600000, height - 2400000)))
    next_id = 4
    hf_attrs: list[str] = []
    if master["footer"]:
        text = master["footerText"]
        para = (_para(text, size=900, color_slot="tx1") if text
                else '<a:p><a:endParaRPr lang="zh-CN"/></a:p>')
        shapes.append(_placeholder_sp(
            next_id, "页脚占位符", "ftr", None, [para],
            _geom(800000, height - 500000, 3600000, 360000)))
        hf_attrs.append('ftr="1"')
        next_id += 1
    if master["pageNumber"]:
        shapes.append(_placeholder_sp(
            next_id, "页码占位符", "sldNum", None, [],
            _geom(width - 1600000, height - 500000, 800000, 360000),
            slidenum_field=True))
        hf_attrs.append('sldNum="1"')
        next_id += 1
    if master["logo"]:
        logo = master["logo"]
        cx = _LOGO_WIDTH
        cy = max(1, int(logo["height"] * _LOGO_WIDTH / logo["width"]))
        x = width - cx - _LOGO_MARGIN
        y = height - cy - _LOGO_MARGIN
        shapes.append(
            f'<p:pic><p:nvPicPr><p:cNvPr id="{next_id}" name="{_esc(logo["name"])}" '
            f'descr="{_esc(logo["name"])}"/><p:cNvPicPr/><p:nvPr/></p:nvPicPr>'
            '<p:blipFill><a:blip r:embed="rIdLogo"/><a:stretch><a:fillRect/></a:stretch>'
            '</p:blipFill>'
            f'<p:spPr>{_geom(x, y, cx, cy)}'
            '<a:prstGeom prst="rect"><a:avLst/></a:prstGeom></p:spPr></p:pic>')
        rels.append(("rIdLogo", R + "/image", f"../media/logo.{logo['ext']}"))
    hf = f"<p:hf {' '.join(hf_attrs)}/>" if hf_attrs else ""
    xml = f'''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<p:sldMaster xmlns:p="{P}" xmlns:a="{A}" xmlns:r="{R}"><p:cSld name="AE 母版"><p:spTree>
<p:nvGrpSpPr><p:cNvPr id="1" name=""/><p:cNvGrpSpPr/><p:nvPr/></p:nvGrpSpPr><p:grpSpPr/>
{''.join(shapes)}
</p:spTree></p:cSld>
<p:clrMap bg1="lt1" tx1="dk1" bg2="lt2" tx2="dk2" accent1="accent1" accent2="accent2" accent3="accent3" accent4="accent4" accent5="accent5" accent6="accent6" hlink="hlink" folHlink="folHlink"/>
<p:sldLayoutIdLst>{{layouts}}</p:sldLayoutIdLst>
<p:txStyles><p:titleStyle><a:lvl1pPr><a:defRPr sz="3600" b="0"><a:solidFill><a:schemeClr val="tx1"/></a:solidFill><a:latin typeface="+mj-lt"/><a:ea typeface="+mj-ea"/></a:defRPr></a:lvl1pPr></p:titleStyle>
<p:bodyStyle><a:lvl1pPr marL="228600" indent="-228600"><a:buFont typeface="Arial"/><a:buChar char="•"/><a:defRPr sz="2000"><a:solidFill><a:schemeClr val="tx1"/></a:solidFill><a:latin typeface="+mn-lt"/><a:ea typeface="+mn-ea"/></a:defRPr></a:lvl1pPr><a:lvl2pPr marL="457200" indent="-228600"><a:buFont typeface="Arial"/><a:buChar char="–"/><a:defRPr sz="1800"><a:solidFill><a:schemeClr val="tx1"/></a:solidFill><a:latin typeface="+mn-lt"/><a:ea typeface="+mn-ea"/></a:defRPr></a:lvl2pPr></p:bodyStyle>
<p:otherStyle><a:lvl1pPr><a:defRPr sz="1800"/></a:lvl1pPr></p:otherStyle></p:txStyles>
{hf}</p:sldMaster>'''
    rels.append(("rIdTheme", R + "/theme", "../theme/theme1.xml"))
    return xml, _rels_xml(rels)


def _layout_xml(layout_key: str, size: tuple[int, int]) -> str:
    entry = LAYOUTS[layout_key]
    width, height = size
    shapes: list[str] = []
    next_id = 2
    if "ctrTitle" in entry["placeholders"]:
        shapes.append(_placeholder_sp(
            next_id, "标题占位符", "ctrTitle", None, [],
            _geom(800000, 2200000, width - 1600000, 1600000)))
        next_id += 1
    if "title" in entry["placeholders"]:
        if layout_key == "sectionHeader":
            geometry = _geom(800000, 2400000, width - 1600000, 1200000)
        else:
            geometry = _geom(800000, 400000, width - 1600000, 1200000)
        shapes.append(_placeholder_sp(next_id, "标题占位符", "title", None, [], geometry))
        next_id += 1
    if "subTitle" in entry["placeholders"]:
        shapes.append(_placeholder_sp(
            next_id, "副标题占位符", "subTitle", 1, [],
            _geom(800000, 4000000, width - 1600000, 1000000)))
        next_id += 1
    if "body" in entry["placeholders"]:
        if layout_key == "sectionHeader":
            geometry = _geom(800000, 3800000, width - 1600000, 1600000)
        else:
            geometry = _geom(800000, 1800000, width - 1600000, height - 2400000)
        shapes.append(_placeholder_sp(next_id, "内容占位符", "body", 1, [], geometry))
    return f'''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<p:sldLayout xmlns:p="{P}" xmlns:a="{A}" xmlns:r="{R}" type="{entry["type"]}" preserve="1">
<p:cSld name="{_esc(entry["name"])}"><p:spTree>
<p:nvGrpSpPr><p:cNvPr id="1" name=""/><p:cNvGrpSpPr/><p:nvPr/></p:nvGrpSpPr><p:grpSpPr/>
{''.join(shapes)}
</p:spTree></p:cSld><p:clrMapOvr><a:masterClrMapping/></p:clrMapOvr></p:sldLayout>'''


def _custom_shape(sp_id: int, shape: dict) -> str:
    geometry = _geom(shape["x"], shape["y"], shape["cx"], shape["cy"])
    prst = "rect" if shape["shape"] == "rect" else "ellipse"
    props = (f"<p:spPr>{geometry}"
             f'<a:prstGeom prst="{prst}"><a:avLst/></a:prstGeom>'
             f'<a:solidFill>{_color_ref(shape["fill"])}</a:solidFill>')
    if shape["line"]:
        props += (f'<a:ln w="{shape["line"]["width"]}">'
                  f'<a:solidFill>{_color_ref(shape["line"]["color"])}</a:solidFill></a:ln>')
    props += "</p:spPr>"
    name = shape["name"] or f"形状 {sp_id}"
    paragraphs = [_para(shape["text"], color_slot="lt1")] if shape["text"] else []
    return (f'<p:sp><p:nvSpPr><p:cNvPr id="{sp_id}" name="{_esc(name)}"/><p:cNvSpPr/><p:nvPr/>'
            f"</p:nvSpPr>{props}{_text_body(paragraphs)}</p:sp>")


def _slide_xml(slide: dict, chart_frames: list[tuple[str, dict]]) -> str:
    entry = LAYOUTS[slide["layout"]]
    shapes: list[str] = []
    next_id = 2
    if "ctrTitle" in entry["placeholders"]:
        paragraphs = [_para(slide["title"])] if slide["title"] else []
        shapes.append(_placeholder_sp(next_id, "标题占位符", "ctrTitle", None, paragraphs))
        next_id += 1
    elif "title" in entry["placeholders"]:
        paragraphs = [_para(slide["title"])] if slide["title"] else []
        shapes.append(_placeholder_sp(next_id, "标题占位符", "title", None, paragraphs))
        next_id += 1
    if "subTitle" in entry["placeholders"] and slide["subtitle"]:
        shapes.append(_placeholder_sp(
            next_id, "副标题占位符", "subTitle", 1,
            [_para(slide["subtitle"], size=2000)]))
        next_id += 1
    if "body" in entry["placeholders"] and slide["body"]:
        paragraphs = [_para(item["text"], level=min(item["level"], len(_BULLET_CHARS) - 1),
                            bold=item["bold"]) for item in slide["body"]]
        shapes.append(_placeholder_sp(next_id, "内容占位符", "body", 1, paragraphs))
        next_id += 1
    for shape in slide["shapes"]:
        shapes.append(_custom_shape(next_id, shape))
        next_id += 1
    for rid, chart in chart_frames:
        name = chart["name"] or "图表"
        shapes.append(
            f'<p:graphicFrame><p:nvGraphicFramePr><p:cNvPr id="{next_id}" '
            f'name="{_esc(name)}"/><p:cNvGraphicFramePr/><p:nvPr/></p:nvGraphicFramePr>'
            f'<p:xfrm><a:off x="{chart["x"]}" y="{chart["y"]}"/>'
            f'<a:ext cx="{chart["cx"]}" cy="{chart["cy"]}"/></p:xfrm>'
            f'<a:graphic><a:graphicData uri="{C}">'
            f'<c:chart xmlns:c="{C}" r:id="{rid}"/></a:graphicData></a:graphic></p:graphicFrame>')
        next_id += 1
    hide = ' showMasterSp="0"' if slide["hideMasterShapes"] else ""
    slide_name = slide["title"] or slide["key"]
    return f'''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<p:sld xmlns:p="{P}" xmlns:a="{A}" xmlns:r="{R}"{hide}><p:cSld name="{_esc(slide_name)}"><p:spTree>
<p:nvGrpSpPr><p:cNvPr id="1" name=""/><p:cNvGrpSpPr/><p:nvPr/></p:nvGrpSpPr><p:grpSpPr/>
{''.join(shapes)}
</p:spTree></p:cSld><p:clrMapOvr><a:masterClrMapping/></p:clrMapOvr></p:sld>'''


def _content_types(overrides: list[tuple[str, str]], defaults: list[tuple[str, str]]) -> str:
    default_entries = "".join(
        f'<Default Extension="{ext}" ContentType="{ctype}"/>' for ext, ctype in defaults)
    override_entries = "".join(
        f'<Override PartName="/{name}" ContentType="{ctype}"/>' for name, ctype in overrides)
    return f'''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Types xmlns="{CT}">{default_entries}{override_entries}</Types>'''


def _rels_xml(entries: list[tuple[str, str, str]]) -> str:
    body = "".join(
        f'<Relationship Id="{rid}" Type="{rtype}" Target="{_esc(target)}"/>'
        for rid, rtype, target in entries)
    return f'''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="{PKG_REL}">{body}</Relationships>'''


def _core_props(title: str) -> str:
    return (f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
            f'<cp:coreProperties xmlns:cp="http://schemas.openxmlformats.org/package/2006/metadata/core-properties" '
            f'xmlns:dc="http://purl.org/dc/elements/1.1/" '
            f'xmlns:dcterms="http://purl.org/dc/terms/" '
            f'xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">'
            f"<dc:title>{_esc(title)}</dc:title><dc:creator>annotation-everything</dc:creator>"
            f'<dcterms:created xsi:type="dcterms:W3CDTF">{FIXED_STAMP}</dcterms:created>'
            f'<dcterms:modified xsi:type="dcterms:W3CDTF">{FIXED_STAMP}</dcterms:modified>'
            "</cp:coreProperties>")


def _app_props(slides: int) -> str:
    return (f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
            f'<Properties xmlns="http://schemas.openxmlformats.org/officeDocument/2006/extended-properties">'
            f"<Application>annotation-everything</Application><Slides>{slides}</Slides>"
            "</Properties>")


def _sections_ext(sections: list[dict], slide_ids: dict[str, int]) -> str:
    parts = "".join(
        f'<p14:section name="{_esc(section["name"])}" id="{_guid("section:" + section["name"])}">'
        '<p14:sldIdLst>'
        + "".join(f'<p14:sldId id="{slide_ids[key]}"/>' for key in section["keys"])
        + "</p14:sldIdLst></p14:section>"
        for section in sections)
    return ('<p:extLst><p:ext uri="{521415D9-36F7-43DF-B3DD-418300879F6D}">'
            '<p14:sectionLst xmlns:p14="http://schemas.microsoft.com/office/powerpoint/2010/main">'
            f"{parts}</p14:sectionLst></p:ext></p:extLst>")


def _num(value: object) -> str:
    """数值写成 OOXML 缓存文本：整数不带小数点，浮点保留原值。"""
    if isinstance(value, int):
        return str(value)
    if float(value).is_integer():
        return str(int(value))
    return repr(float(value))


def _col_letter(index: int) -> str:
    """内嵌工作簿的系列列号（0 → B，最多到 Z）。"""
    return chr(66 + index)


def _chart_xml(chart: dict, index: int) -> str:
    """单张原生图表部件：轴引用、数据缓存与主题引用配色。"""
    kind = chart["chart"]
    count = len(chart["categories"])
    ax_a, ax_b = 100000000 + index * 2, 100000000 + index * 2 + 1
    sers: list[str] = []
    for order, series in enumerate(chart["series"]):
        col = _col_letter(order)
        cat_pts = "".join(f'<c:pt idx="{i}"><c:v>{_esc(v)}</c:v></c:pt>'
                          for i, v in enumerate(chart["categories"]))
        val_pts = "".join(f'<c:pt idx="{i}"><c:v>{v}</c:v></c:pt>'
                          for i, v in enumerate(series["values"]))
        dlbls = ""
        if chart["labels"] != "none":
            show_val = "1" if chart["labels"] == "value" else "0"
            show_percent = "1" if chart["labels"] == "percent" else "0"
            dlbls = (f"<c:dLbls><c:showLegendKey val=\"0\"/><c:showVal val=\"{show_val}\"/>"
                     '<c:showCatName val="0"/><c:showSerName val="0"/>'
                     f'<c:showPercent val="{show_percent}"/><c:showBubbleSize val="0"/></c:dLbls>')
        ser = (f'<c:ser><c:idx val="{order}"/><c:order val="{order}"/>'
               f'<c:tx><c:strRef><c:f>Sheet1!${col}$1</c:f><c:strCache>'
               f'<c:ptCount val="1"/><c:pt idx="0"><c:v>{_esc(series["name"])}</c:v></c:pt>'
               f'</c:strCache></c:strRef></c:tx>{dlbls}'
               f'<c:cat><c:strRef><c:f>Sheet1!$A$2:$A${count + 1}</c:f><c:strCache>'
               f'<c:ptCount val="{count}"/>{cat_pts}</c:strCache></c:strRef></c:cat>'
               f'<c:val><c:numRef><c:f>Sheet1!${col}$2:${col}${count + 1}</c:f>'
               f'<c:numCache><c:formatCode>General</c:formatCode>'
               f'<c:ptCount val="{count}"/>{val_pts}</c:numCache></c:numRef></c:val>')
        if kind == "line":
            ser += '<c:smooth val="0"/>'
        ser += "</c:ser>"
        sers.append(ser)
    sers_xml = "".join(sers)
    if kind in ("column", "bar"):
        direction = "col" if kind == "column" else "bar"
        cat_pos, val_pos = ("b", "l") if kind == "column" else ("l", "b")
        group = (f'<c:barChart><c:barDir val="{direction}"/><c:grouping val="clustered"/>'
                 f'<c:varyColors val="0"/>{sers_xml}<c:gapWidth val="150"/>'
                 f'<c:axId val="{ax_a}"/><c:axId val="{ax_b}"/></c:barChart>')
        axes = (
            f'<c:catAx><c:axId val="{ax_a}"/><c:scaling><c:orientation val="minMax"/></c:scaling>'
            f'<c:delete val="0"/><c:axPos val="{cat_pos}"/><c:majorTickMark val="out"/>'
            f'<c:minorTickMark val="none"/><c:tickLblPos val="nextTo"/>'
            f'<c:crossAx val="{ax_b}"/><c:crosses val="autoZero"/><c:auto val="1"/>'
            f'<c:lblAlgn val="ctr"/><c:lblOffset val="100"/><c:noMultiLvlLbl val="0"/></c:catAx>'
            f'<c:valAx><c:axId val="{ax_b}"/><c:scaling><c:orientation val="minMax"/></c:scaling>'
            f'<c:delete val="0"/><c:axPos val="{val_pos}"/><c:majorGridlines/>'
            f'<c:numFmt formatCode="General" sourceLinked="0"/>'
            f'<c:majorTickMark val="out"/><c:minorTickMark val="none"/>'
            f'<c:tickLblPos val="nextTo"/><c:crossAx val="{ax_a}"/>'
            '<c:crosses val="autoZero"/></c:valAx>')
    elif kind == "line":
        group = (f'<c:lineChart><c:grouping val="standard"/><c:varyColors val="0"/>'
                 f'{sers_xml}<c:marker val="0"/>'
                 f'<c:axId val="{ax_a}"/><c:axId val="{ax_b}"/></c:lineChart>')
        axes = (
            f'<c:catAx><c:axId val="{ax_a}"/><c:scaling><c:orientation val="minMax"/></c:scaling>'
            f'<c:delete val="0"/><c:axPos val="b"/><c:majorTickMark val="out"/>'
            f'<c:minorTickMark val="none"/><c:tickLblPos val="nextTo"/>'
            f'<c:crossAx val="{ax_b}"/><c:crosses val="autoZero"/><c:auto val="1"/>'
            f'<c:lblAlgn val="ctr"/><c:lblOffset val="100"/><c:noMultiLvlLbl val="0"/></c:catAx>'
            f'<c:valAx><c:axId val="{ax_b}"/><c:scaling><c:orientation val="minMax"/></c:scaling>'
            f'<c:delete val="0"/><c:axPos val="l"/><c:majorGridlines/>'
            f'<c:numFmt formatCode="General" sourceLinked="0"/>'
            f'<c:majorTickMark val="out"/><c:minorTickMark val="none"/>'
            f'<c:tickLblPos val="nextTo"/><c:crossAx val="{ax_a}"/>'
            '<c:crosses val="autoZero"/></c:valAx>')
    else:
        dlbls = ""
        if chart["labels"] != "none":
            show_val = "1" if chart["labels"] == "value" else "0"
            show_percent = "1" if chart["labels"] == "percent" else "0"
            dlbls = (f"<c:dLbls><c:showLegendKey val=\"0\"/><c:showVal val=\"{show_val}\"/>"
                     '<c:showCatName val="0"/><c:showSerName val="0"/>'
                     f'<c:showPercent val="{show_percent}"/><c:showBubbleSize val="0"/></c:dLbls>')
        group = (f'<c:pieChart><c:varyColors val="1"/>{sers_xml}{dlbls}'
                 '<c:firstSliceAng val="0"/></c:pieChart>')
        axes = ""
    title_block = ""
    if chart["title"]:
        title_block = (f'<c:title><c:tx><c:rich><a:bodyPr/><a:lstStyle/><a:p>'
                       f'<a:pPr><a:defRPr sz="1400"/></a:pPr>'
                       f'<a:r><a:rPr lang="zh-CN" sz="1400"/><a:t>{_esc(chart["title"])}</a:t></a:r>'
                       f'</a:p></c:rich></c:tx><c:overlay val="0"/></c:title>')
    legend_block = ""
    if chart["legend"] != "none":
        pos = {"right": "r", "bottom": "b", "top": "t"}[chart["legend"]]
        legend_block = f'<c:legend><c:legendPos val="{pos}"/><c:overlay val="0"/></c:legend>'
    return (f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
            f'<c:chartSpace xmlns:c="{C}" xmlns:a="{A}" xmlns:r="{R}"><c:chart>'
            f"{title_block}<c:autoTitleDeleted val=\"{0 if chart['title'] else 1}\"/>"
            f"<c:plotArea><c:layout/>{group}{axes}</c:plotArea>{legend_block}"
            '<c:plotVisOnly val="1"/><c:dispBlanksAs val="gap"/></c:chart>'
            '<c:externalData r:id="rId1" autoUpdate="0"/></c:chartSpace>')


def _chart_workbook(chart: dict) -> dict[str, str]:
    """图表的内嵌工作簿：Sheet1 的类目与各系列数值，保证数据可编辑。"""
    header_cells = ['<c r="A1" t="inlineStr"><is><t>类别</t></is></c>']
    for order, series in enumerate(chart["series"]):
        col = _col_letter(order)
        header_cells.append(f'<c r="{col}1" t="inlineStr"><is><t>{_esc(series["name"])}</t></is></c>')
    rows = [f'<row r="1">{"".join(header_cells)}</row>']
    for index, category in enumerate(chart["categories"]):
        cells = [f'<c r="A{index + 2}" t="inlineStr"><is><t>{_esc(category)}</t></is></c>']
        for order, series in enumerate(chart["series"]):
            col = _col_letter(order)
            cells.append(f'<c r="{col}{index + 2}"><v>{series["values"][index]}</v></c>')
        rows.append(f'<row r="{index + 2}">{"".join(cells)}</row>')
    return {
        "[Content_Types].xml":
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
            '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
            '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
            '<Default Extension="xml" ContentType="application/xml"/>'
            '<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
            '<Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
            "</Types>",
        "_rels/.rels":
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/>'
            "</Relationships>",
        "xl/workbook.xml":
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
            '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
            'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
            '<sheets><sheet name="Sheet1" sheetId="1" r:id="rId1"/></sheets></workbook>',
        "xl/_rels/workbook.xml.rels":
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/>'
            "</Relationships>",
        "xl/worksheets/sheet1.xml":
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
            '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
            f"<sheetData>{''.join(rows)}</sheetData></worksheet>",
    }


def _notes_slide_xml(notes: str) -> str:
    """备注部件：讲稿按行成段，只挂在对应幻灯片上。"""
    paragraphs = [_para(line) for line in
                  [line for line in notes.split("\n") if line.strip()]]
    shapes = [_placeholder_sp(2, "幻灯片图像", "sldImg", None, [],
                              _geom(685800, 457200, 5486400, 3086100)),
              _placeholder_sp(3, "备注正文", "body", 1, paragraphs,
                              _geom(685800, 3800000, 5486400, 4800000))]
    return (f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
            f'<p:notes xmlns:p="{P}" xmlns:a="{A}"><p:cSld name="备注"><p:spTree>'
            "<p:nvGrpSpPr><p:cNvPr id=\"1\" name=\"\"/><p:cNvGrpSpPr/><p:nvPr/></p:nvGrpSpPr><p:grpSpPr/>"
            f"{''.join(shapes)}"
            "</p:spTree></p:cSld><p:clrMapOvr><a:masterClrMapping/></p:clrMapOvr></p:notes>")


def _notes_master_xml() -> str:
    shapes = [_placeholder_sp(2, "幻灯片图像", "sldImg", None, [],
                              _geom(685800, 457200, 5486400, 3086100)),
              _placeholder_sp(3, "备注正文", "body", 1, [],
                              _geom(685800, 3800000, 5486400, 4800000)),
              _placeholder_sp(4, "页码占位符", "sldNum", None, [],
                              _geom(6000000, 8700000, 400000, 300000))]
    return (f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
            f'<p:notesMaster xmlns:p="{P}" xmlns:a="{A}" xmlns:r="{R}">'
            '<p:cSld name="备注母版"><p:spTree>'
            "<p:nvGrpSpPr><p:cNvPr id=\"1\" name=\"\"/><p:cNvGrpSpPr/><p:nvPr/></p:nvGrpSpPr><p:grpSpPr/>"
            f"{''.join(shapes)}</p:spTree></p:cSld>"
            '<p:clrMap bg1="lt1" tx1="dk1" bg2="lt2" tx2="dk2" accent1="accent1" accent2="accent2" accent3="accent3" accent4="accent4" accent5="accent5" accent6="accent6" hlink="hlink" folHlink="folHlink"/>'
            '<p:notesStyle><a:lvl1pPr><a:defRPr sz="1200"/></a:lvl1pPr></p:notesStyle>'
            "</p:notesMaster>")


def write_package(out: Path, parts: dict[str, str | bytes]) -> Path:
    """确定性写包：固定成员顺序、时间戳与权限，同规格同字节。"""
    out = Path(out)
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as archive:
        for name in sorted(parts):
            content = parts[name]
            data = content.encode("utf-8") if isinstance(content, str) else content
            info = zipfile.ZipInfo(name, FIXED_TIME)
            info.external_attr = 0o600 << 16
            archive.writestr(info, data)
    return out


def build_deck(spec: object, out: Path, *, base_dir: Path | None = None) -> dict:
    """把 deck 规格写成 .pptx；返回构建摘要（schema、文件名、sha256、页数、标题）。"""
    spec = validate_spec(spec, base_dir=base_dir)
    size = spec["size"]
    slides = spec["slides"]

    used_layouts = [key for key in LAYOUT_ORDER
                    if any(s["layout"] == key for s in slides)]
    layout_parts = {key: f"slideLayout{index + 1}.xml"
                    for index, key in enumerate(used_layouts)}
    layout_rids = {key: f"rIdLayout{index + 1}"
                   for index, key in enumerate(used_layouts)}
    layout_id_entries = "".join(
        f'<p:sldLayoutId id="{LAYOUT_ID_BASE + index}" r:id="{layout_rids[key]}"/>'
        for index, key in enumerate(used_layouts))

    slide_ids = _slide_ids([s["key"] for s in slides])
    slide_rids = {s["key"]: f"rIdSlide{index + 1}" for index, s in enumerate(slides)}

    master_xml, master_rels = _master_xml(spec["master"], size)
    master_xml = master_xml.replace("{layouts}", layout_id_entries)
    master_rels = master_rels.replace(
        "</Relationships>",
        "".join(f'<Relationship Id="{layout_rids[key]}" Type="{R}/slideLayout" '
                f'Target="../slideLayouts/{layout_parts[key]}"/>'
                for key in used_layouts) + "</Relationships>")

    pres_rels: list[tuple[str, str, str]] = [
        ("rIdMaster", R + "/slideMaster", "slideMasters/slideMaster1.xml")]
    pres_slide_entries = "".join(
        f'<p:sldId id="{slide_ids[s["key"]]}" r:id="{slide_rids[s["key"]]}"/>'
        for s in slides)

    overrides: list[tuple[str, str]] = [
        ("ppt/presentation.xml", CT_PRESENTATION),
        ("ppt/slideMasters/slideMaster1.xml", CT_MASTER),
        ("ppt/theme/theme1.xml", CT_THEME),
        ("docProps/core.xml", CT_CORE),
        ("docProps/app.xml", CT_APP),
    ]
    overrides += [(f"ppt/slideLayouts/{part}", CT_LAYOUT) for part in layout_parts.values()]

    parts: dict[str, str | bytes] = {
        "docProps/core.xml": _core_props(spec["title"]),
        "docProps/app.xml": _app_props(len(slides)),
        "ppt/theme/theme1.xml": _theme_xml(spec["theme"]),
        "ppt/slideMasters/slideMaster1.xml": master_xml,
        "ppt/slideMasters/_rels/slideMaster1.xml.rels": master_rels,
    }
    for key in used_layouts:
        parts[f"ppt/slideLayouts/{layout_parts[key]}"] = _layout_xml(key, size)
        parts[f"ppt/slideLayouts/_rels/{layout_parts[key]}.rels"] = _rels_xml(
            [("rId1", R + "/slideMaster", "../slideMasters/slideMaster1.xml")])
    chart_counter = 0
    has_charts = False
    has_notes = any(s["notes"] for s in slides)
    for index, slide in enumerate(slides, 1):
        overrides.append((f"ppt/slides/slide{index}.xml", CT_SLIDE))
        rel_entries: list[tuple[str, str, str]] = [
            (slide_rids[slide["key"]], R + "/slideLayout",
             f"../slideLayouts/{layout_parts[slide['layout']]}")]
        chart_frames: list[tuple[str, dict]] = []
        for chart in slide["charts"]:
            chart_counter += 1
            has_charts = True
            rid = f"rIdChart{chart_counter}"
            overrides.append((f"ppt/charts/chart{chart_counter}.xml", CT_CHART))
            parts[f"ppt/charts/chart{chart_counter}.xml"] = _chart_xml(chart, chart_counter)
            parts[f"ppt/charts/_rels/chart{chart_counter}.xml.rels"] = _rels_xml(
                [("rId1", R + "/package", f"../embeddings/data{chart_counter}.xlsx")])
            for name, content in _chart_workbook(chart).items():
                parts[f"ppt/charts/workbook{chart_counter}/{name}"] = content
            rel_entries.append((rid, R + "/chart", f"../charts/chart{chart_counter}.xml"))
            chart_frames.append((rid, chart))
        if slide["notes"]:
            parts[f"ppt/notesSlides/notesSlide{index}.xml"] = _notes_slide_xml(slide["notes"])
            parts[f"ppt/notesSlides/_rels/notesSlide{index}.xml.rels"] = _rels_xml([
                ("rId1", R + "/notesMaster", "../notesMasters/notesMaster1.xml"),
                ("rId2", R + "/slide", f"../slides/slide{index}.xml"),
            ])
            overrides.append((f"ppt/notesSlides/notesSlide{index}.xml", CT_NOTES_SLIDE))
            rel_entries.append(("rIdNotes", R + "/notesSlide",
                                f"../notesSlides/notesSlide{index}.xml"))
        parts[f"ppt/slides/slide{index}.xml"] = _slide_xml(slide, chart_frames)
        parts[f"ppt/slides/_rels/slide{index}.xml.rels"] = _rels_xml(rel_entries)
        pres_rels.append((slide_rids[slide["key"]], R + "/slide", f"slides/slide{index}.xml"))

    if has_notes:
        parts["ppt/notesMasters/notesMaster1.xml"] = _notes_master_xml()
        parts["ppt/notesMasters/_rels/notesMaster1.xml.rels"] = _rels_xml(
            [("rId1", R + "/theme", "../theme/theme1.xml")])
        overrides.append(("ppt/notesMasters/notesMaster1.xml", CT_NOTES_MASTER))
        pres_rels.insert(1, ("rIdNotesMaster", R + "/notesMaster",
                             "notesMasters/notesMaster1.xml"))

    if spec["master"]["logo"]:
        logo = spec["master"]["logo"]
        parts[f"ppt/media/logo.{logo['ext']}"] = logo["data"]

    # 图表的内嵌工作簿是嵌套的 xlsx 包：先单独打包，再作为部件放进外层包。
    workbook_numbers = sorted({int(name.split("/")[2].removeprefix("workbook"))
                               for name in parts if name.startswith("ppt/charts/workbook")})
    for chart_number in workbook_numbers:
        prefix = f"ppt/charts/workbook{chart_number}/"
        inner = {key: parts.pop(key) for key in sorted(parts)
                 if key.startswith(prefix)}
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
            for member in sorted(inner):
                content = inner[member]
                data = content.encode("utf-8") if isinstance(content, str) else content
                info = zipfile.ZipInfo(member.removeprefix(f"ppt/charts/workbook{chart_number}/"),
                                       FIXED_TIME)
                info.external_attr = 0o600 << 16
                archive.writestr(info, data)
        parts[f"ppt/embeddings/data{chart_number}.xlsx"] = buffer.getvalue()

    notes_master_entry = (f'<p:notesMasterId id="{NOTES_MASTER_ID}" r:id="rIdNotesMaster"/>'
                          if has_notes else "")
    presentation = f'''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<p:presentation xmlns:p="{P}" xmlns:a="{A}" xmlns:r="{R}"><p:sldMasterIdLst><p:sldMasterId id="{MASTER_ID}" r:id="rIdMaster"/></p:sldMasterIdLst><p:notesMasterIdLst>{notes_master_entry}</p:notesMasterIdLst><p:sldIdLst>{pres_slide_entries}</p:sldIdLst><p:sldSz cx="{size[0]}" cy="{size[1]}"/><p:notesSz cx="{_NOTES_SIZE[0]}" cy="{_NOTES_SIZE[1]}"/>{_sections_ext(spec["sections"], slide_ids)}</p:presentation>'''
    parts["ppt/presentation.xml"] = presentation
    parts["ppt/_rels/presentation.xml.rels"] = _rels_xml(pres_rels)

    defaults = [("rels", "application/vnd.openxmlformats-package.relationships+xml"),
                ("xml", "application/xml")]
    if has_charts:
        defaults.append(("xlsx", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"))
    if spec["master"]["logo"]:
        ext = spec["master"]["logo"]["ext"]
        defaults.append((ext, f"image/{'jpeg' if ext == 'jpg' else ext}"))
    parts["_rels/.rels"] = _rels_xml([
        ("rId1", R + "/officeDocument", "ppt/presentation.xml"),
        ("rId2", "http://schemas.openxmlformats.org/package/2006/relationships/metadata/core-properties",
         "docProps/core.xml"),
        ("rId3", R + "/extended-properties", "docProps/app.xml"),
    ])
    parts["[Content_Types].xml"] = _content_types(overrides, defaults)

    out = Path(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    write_package(out, parts)
    digest = hashlib.sha256(out.read_bytes()).hexdigest()
    return {"schema": BUILD_SCHEMA, "file": out.name, "sha256": digest,
            "pages": len(slides), "title": spec["title"]}


# ── 命令行 ──────────────────────────────────────────────────────────────────

def _t(loc: str, key: str, **params: object) -> str:
    return i18n.translate(i18n.catalog(loc), key, **params)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="把 ae-pptx-deck/1 规格写成可编辑的 .pptx（内置 PPTX 制作器）")
    parser.add_argument("--deck", required=True, help="deck 规格 JSON 文件")
    parser.add_argument("--out", required=True, help="输出 .pptx 文件")
    parser.add_argument("--locale")
    parser.add_argument("--json", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    fsutil.configure_stdio()
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        loc = i18n.resolve_locale(args.locale, system_languages=i18n.system_languages())
    except i18n.LocaleError as exc:
        print(_t("zh-CN", "pptxbuild.badLocale", locale=exc.params.get("locale", "")),
              file=sys.stderr)
        print(_t("en", "pptxbuild.badLocale", locale=exc.params.get("locale", "")),
              file=sys.stderr)
        return 2
    try:
        raw = Path(args.deck).read_text(encoding="utf-8")
    except OSError as exc:
        print(_t(loc, "pptxbuild.err.deckRead", path=args.deck,
                 detail=exc.strerror or ""), file=sys.stderr)
        return 2
    try:
        spec = json.loads(raw)
    except ValueError as exc:
        print(_t(loc, "pptxbuild.err.deckJson", path=args.deck, detail=str(exc)),
              file=sys.stderr)
        return 2
    try:
        summary = build_deck(spec, Path(args.out), base_dir=Path(args.deck).resolve().parent)
    except DeckSpecError as exc:
        print(_t(loc, "pptxbuild.err." + exc.code, **exc.params), file=sys.stderr)
        return 2
    if args.json:
        print(json.dumps(summary, ensure_ascii=False))
    else:
        print(_t(loc, "pptxbuild.ok", file=summary["file"], pages=summary["pages"],
                 sha256=summary["sha256"][:12]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
