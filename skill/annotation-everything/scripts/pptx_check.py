#!/usr/bin/env python3
"""PPTX 文件层检查器（issue #180）：对本票生成范围内的包结构与关键部件做确定规则检查。

检查覆盖：必要部件与关系目标、内容类型与部件一致、幻灯片/版式/母版引用有效、
占位符对应、幻灯片清单一致、首版四类原生图表的关键结构（轴引用、标签位置、
数据缓存与内嵌工作簿、主题引用配色）、主题部件完整、章节覆盖。失败输出检查项、
包内部件路径、可确定的幻灯片位置与原因。

这是最低要求的文件层检查，不是通用 OOXML 验证器：ZIP 能打开、XML 能解析不代表
通过；同时通过也不承诺覆盖任意 OOXML 错误——PowerPoint 实机验收另行执行。
只用 Python 标准库。

用法：
    python3 pptx_check.py deck.pptx [--locale zh-CN|en] [--json]

退出码：0 全部检查通过；1 有检查问题；2 输入或用法错误（文件读不到、语言不合法）。
"""
from __future__ import annotations

import argparse
import hashlib
import io
import json
import posixpath
import re
import sys
import zipfile
from decimal import Decimal, InvalidOperation
from pathlib import Path
from xml.etree import ElementTree as ET

sys.path.insert(0, str(Path(__file__).resolve().parent))

import fsutil
import i18n
import pptx_build

RESULT_SCHEMA = "ae-pptx-check/1"

P = "http://schemas.openxmlformats.org/presentationml/2006/main"
A = "http://schemas.openxmlformats.org/drawingml/2006/main"
C = "http://schemas.openxmlformats.org/drawingml/2006/chart"
R = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
PKG_REL = "http://schemas.openxmlformats.org/package/2006/relationships"
CT = "http://schemas.openxmlformats.org/package/2006/content-types"
P14 = "http://schemas.microsoft.com/office/powerpoint/2010/main"
S = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"

CT_PRESENTATION = "application/vnd.openxmlformats-officedocument.presentationml.presentation.main+xml"
CT_SLIDE = "application/vnd.openxmlformats-officedocument.presentationml.slide+xml"
CT_MASTER = "application/vnd.openxmlformats-officedocument.presentationml.slideMaster+xml"
CT_LAYOUT = "application/vnd.openxmlformats-officedocument.presentationml.slideLayout+xml"
CT_THEME = "application/vnd.openxmlformats-officedocument.theme+xml"
CT_CHART = "application/vnd.openxmlformats-officedocument.drawingml.chart+xml"
CT_NOTES_MASTER = "application/vnd.openxmlformats-officedocument.presentationml.notesMaster+xml"
CT_NOTES_SLIDE = "application/vnd.openxmlformats-officedocument.presentationml.notesSlide+xml"
CT_WORKBOOK = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"
CT_WORKSHEET = "application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"

# 检查项固定顺序；id 语言无关，可读名称随 locale。
CHECK_ORDER = ("package.required-parts", "package.relationships", "package.content-types",
               "slide.list", "slide.layout-refs", "slide.placeholders",
               "chart.structure", "chart.theme-fill", "theme.parts", "sections.coverage")

# 主题色槽位与制作器共享同一份声明（防漂移；制作器写、检查器读）。
THEME_SLOTS = pptx_build.COLOR_SLOTS
SUPPORTED_CHART_GROUPS = ("barChart", "lineChart", "pieChart")
AXIS_ELEMENTS = ("catAx", "valAx")

MAX_XML_BYTES = 32 * 1024 * 1024


class CheckInputError(Exception):
    """输入或用法错误：退出码 2。"""


class _Problem(Exception):
    def __init__(self, check: str, code: str, part: str = "", slide: int | None = None,
                 **params: object):
        super().__init__(code)
        self.check = check
        self.code = code
        self.part = part
        self.slide = slide
        self.params = params

    def as_dict(self, loc: str) -> dict:
        return {"check": self.check, "code": self.code, "part": self.part,
                "slide": self.slide,
                "reason": _t(loc, "pptxcheck.reason." + self.code, **self.params)}


def _t(loc: str, key: str, **params: object) -> str:
    return i18n.translate(i18n.catalog(loc), key, **params)


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _q(namespace: str, name: str) -> str:
    return f"{{{namespace}}}{name}"


def _xml(archive: zipfile.ZipFile, name: str) -> ET.Element:
    info = archive.NameToInfo.get(name)
    if info is None:
        raise KeyError(name)
    if info.file_size > MAX_XML_BYTES:
        raise _Problem("package.required-parts", "partTooLarge", part=name)
    try:
        return ET.fromstring(archive.read(name))
    except ET.ParseError as exc:
        raise _Problem("package.required-parts", "badXml", part=name, detail=str(exc)) from exc


def _target(source: str, target: str) -> str:
    if not target or "\\" in target:
        return ""
    if target.startswith("/"):
        return target.lstrip("/")
    return posixpath.normpath(posixpath.join(posixpath.dirname(source), target))


def _rels_name(source: str) -> str:
    if source == "":
        return "_rels/.rels"
    return posixpath.join(posixpath.dirname(source), "_rels",
                          posixpath.basename(source) + ".rels")


def _read_rels(archive: zipfile.ZipFile, source: str) -> dict[str, dict]:
    """读取一份 rels：rId → {type, target, external}；rels 缺失视为空。"""
    name = _rels_name(source)
    if name not in archive.NameToInfo:
        return {}
    root = _xml(archive, name)
    out: dict[str, dict] = {}
    for rel in root.findall(_q(PKG_REL, "Relationship")):
        external = rel.get("TargetMode") == "External"
        target = rel.get("Target", "")
        out[rel.get("Id", "")] = {
            "type": rel.get("Type", ""),
            "target": "" if external else _target(source, target),
            "raw_target": target,
            "external": external,
        }
    return out


def _ref_ids(element: ET.Element) -> set[str]:
    """收集部件 XML 里通过 r:id / r:embed / r:link 引用的关系 ID。"""
    found: set[str] = set()
    for node in element.iter():
        for attr in (_q(R, "id"), _q(R, "embed"), _q(R, "link")):
            value = node.get(attr)
            if value:
                found.add(value)
    return found


def _placeholders(sld_like: ET.Element) -> set[tuple[str, int | None]]:
    """收集幻灯片/版式的占位符集合；idx 缺省按 None 比对。"""
    found: set[tuple[str, int | None]] = set()
    for ph in sld_like.iter(_q(P, "ph")):
        idx = ph.get("idx")
        found.add((ph.get("type", "body"), int(idx) if idx else None))
    return found


def _workbook_cells(book: zipfile.ZipFile) -> dict[str, dict[str, tuple[str, str]]]:
    """沿工作簿关系读取工作表；仅接受可由图表引用的真实单元格。"""
    types = _xml(book, "[Content_Types].xml")
    if types.tag != _q(CT, "Types"):
        raise ValueError("content types")
    overrides = {node.get("PartName", "").lstrip("/"): node.get("ContentType")
                 for node in types.findall(_q(CT, "Override"))}
    defaults = {node.get("Extension", "").lower(): node.get("ContentType")
                for node in types.findall(_q(CT, "Default"))}

    def part_xml(part: str, ctype: str, tag: str) -> ET.Element:
        if overrides.get(part, defaults.get(part.rsplit(".", 1)[-1].lower())) != ctype:
            raise ValueError("content type")
        root = _xml(book, part)
        if root.tag != _q(S, tag):
            raise ValueError("part root")
        return root

    main = [rel for rel in _read_rels(book, "").values()
            if rel["type"] == R + "/officeDocument" and not rel["external"]]
    if len(main) != 1:
        raise ValueError("workbook relationship")
    part = main[0]["target"]
    workbook = part_xml(part, CT_WORKBOOK, "workbook")
    rels = _read_rels(book, part)
    strings = []
    for rel in rels.values():
        if rel["type"] == R + "/sharedStrings" and not rel["external"]:
            root = _xml(book, rel["target"])
            strings = ["".join(node.itertext()) for node in root.findall(_q(S, "si"))]
    sheets = {}
    for sheet in workbook.findall(_q(S, "sheets") + "/" + _q(S, "sheet")):
        name = sheet.get("name", "")
        rel = rels.get(sheet.get(_q(R, "id")), {})
        if not name or name in sheets or rel.get("type") != R + "/worksheet" or rel.get("external"):
            raise ValueError("worksheet relationship")
        root = part_xml(rel["target"], CT_WORKSHEET, "worksheet")
        data = root.find(_q(S, "sheetData"))
        if data is None:
            raise ValueError("worksheet data")
        cells = {}
        for cell in data.iter(_q(S, "c")):
            address = cell.get("r", "")
            if not re.fullmatch(r"[A-Z]+[1-9][0-9]*", address) or address in cells:
                raise ValueError("cell address")
            kind = cell.get("t", "n")
            value = cell.findtext(_q(S, "v"), "")
            if kind == "inlineStr":
                value = "".join(node.text or "" for node in cell.iter(_q(S, "t")))
            elif kind == "s":
                index = int(value)
                if not 0 <= index < len(strings):
                    raise ValueError("shared string")
                value = strings[index]
            cells[address] = (kind, value)
        sheets[name] = cells
    if not sheets:
        raise ValueError("no worksheets")
    return sheets


def _chart_reference_matches(ref: ET.Element, sheets: dict) -> bool:
    """比较引用范围、缓存索引和工作表单元格，覆盖系列名、类目与数值。"""
    formula = ref.findtext(_q(C, "f"), "")
    match = re.fullmatch(r"('(?:[^']|'')+'|[^!]+)!\$?([A-Z]+)\$?([1-9][0-9]*)"
                         r"(?::\$?([A-Z]+)\$?([1-9][0-9]*))?", formula)
    if match is None:
        return False
    sheet, first_col, first_row, last_col, last_row = match.groups()
    if sheet.startswith("'") and sheet.endswith("'"):
        sheet = sheet[1:-1].replace("''", "'")

    def column_number(label: str) -> int:
        value = 0
        for char in label:
            value = value * 26 + ord(char) - ord("A") + 1
        return value

    def column_label(value: int) -> str:
        label = ""
        while value:
            value, rest = divmod(value - 1, 26)
            label = chr(ord("A") + rest) + label
        return label

    c1, c2 = column_number(first_col), column_number(last_col or first_col)
    r1, r2 = int(first_row), int(last_row or first_row)
    if sheet not in sheets or not (1 <= c1 <= c2 <= 16384 and 1 <= r1 <= r2 <= 1048576) \
            or (c1 != c2 and r1 != r2):
        return False
    numeric = ref.tag == _q(C, "numRef")
    cache = ref.find(_q(C, "numCache" if numeric else "strCache"))
    if cache is None:
        return False
    points = cache.findall(_q(C, "pt"))
    count = max(c2 - c1, r2 - r1) + 1
    count_node = cache.find(_q(C, "ptCount"))
    if len(points) != count or count_node is None or count_node.get("val") != str(count):
        return False
    try:
        indices = [int(point.get("idx", "")) for point in points]
        if set(indices) != set(range(count)):
            return False
        for index, point in zip(indices, points):
            address = column_label(c1 + (index if c1 != c2 else 0)) + str(r1 + (index if r1 != r2 else 0))
            cell = sheets[sheet].get(address)
            value = point.findtext(_q(C, "v"), "")
            if cell is None:
                return False
            if numeric:
                actual, expected = Decimal(cell[1]), Decimal(value)
                if cell[0] != "n" or not actual.is_finite() or not expected.is_finite() or actual != expected:
                    return False
            elif cell[1] != value:
                return False
    except (ValueError, InvalidOperation):
        return False
    return True


class _Checker:
    def __init__(self, path: Path, loc: str):
        self.path = path
        self.loc = loc
        self.problems: list[_Problem] = []
        self.scopes: dict[str, list[str]] = {}
        self._content_types_root = None
        self._pres_rels: dict[str, dict] = {}
        self._slide_parts: list[str] = []
        self._slide_ids: set[str] = set()
        self._part_types: dict[str, str] = {}
        self.sha256 = hashlib.sha256(path.read_bytes()).hexdigest()

    def add(self, check: str, code: str, part: str = "", slide: int | None = None,
            **params: object) -> None:
        self.problems.append(_Problem(check, code, part, slide, **params))

    def scope(self, check: str, item: str) -> None:
        self.scopes.setdefault(check, []).append(item)

    def run(self) -> dict:
        slides_count = 0
        try:
            archive = zipfile.ZipFile(self.path)
        except (zipfile.BadZipFile, OSError):
            self.add("package.required-parts", "notZip", part=str(self.path.name))
            return self._result(slides=0)
        try:
            with archive:
                names = set(archive.NameToInfo)
                slides_count = self._required_parts(archive, names)
                if "ppt/presentation.xml" in names:
                    self._relationships(archive, names)
                    self._content_types(archive, names)
                    self._text_bodies(archive, names)
                    self._slides(archive, names)
                    self._charts(archive, names)
                    self._theme(archive, names)
                    self._sections(archive, names)
        except _Problem as exc:
            self.problems.append(exc)
        except KeyError as exc:
            self.add("package.required-parts", "partMissing", part=str(exc.args[0]))
        except ET.ParseError as exc:
            self.add("package.required-parts", "badXml", part="", detail=str(exc))
        return self._result(slides=slides_count)

    # ── 各检查项 ────────────────────────────────────────────────────────

    def _required_parts(self, archive: zipfile.ZipFile, names: set[str]) -> int:
        for required in ("[Content_Types].xml", "_rels/.rels", "ppt/presentation.xml",
                         "ppt/slideMasters/slideMaster1.xml", "ppt/theme/theme1.xml"):
            if required not in names:
                self.add("package.required-parts", "partMissing", part=required)
        content_types = _xml(archive, "[Content_Types].xml") if "[Content_Types].xml" in names \
            else None
        root_rels = _read_rels(archive, "")
        main = next((rel for rel in root_rels.values()
                     if rel["type"] == R + "/officeDocument" and not rel["external"]), None)
        if main is None or main["target"] != "ppt/presentation.xml":
            self.add("package.required-parts", "officeDocumentMissing",
                     part=str(main["raw_target"]) if main else "")
            return 0
        self._content_types_root = content_types
        presentation = _xml(archive, "ppt/presentation.xml")
        if _local(presentation.tag) != "presentation":
            self.add("package.required-parts", "notPresentation", part="ppt/presentation.xml")
            return 0
        pres_rels = _read_rels(archive, "ppt/presentation.xml")
        self._pres_rels = pres_rels
        notes = presentation.find(_q(P, "notesMasterIdLst"))
        if notes is not None:
            entries = notes.findall(_q(P, "notesMasterId"))
            if len(entries) != 1 or set(entries[0].attrib) != {_q(R, "id")} \
                    or pres_rels.get(entries[0].get(_q(R, "id")), {}).get("type") != R + "/notesMaster":
                self.add("package.required-parts", "notesMasterListBad", part="ppt/presentation.xml")
        slide_parts: list[str] = []
        seen_ids: set[str] = set()
        slide_nodes = presentation.findall(_q(P, "sldIdLst") + "/" + _q(P, "sldId"))
        for node in slide_nodes:
            slide_id = node.get("id", "")
            rid = node.get(_q(R, "id"), "")
            rel = pres_rels.get(rid)
            if not slide_id or slide_id in seen_ids or rel is None or rel["external"] \
                    or rel["type"] != R + "/slide":
                self.add("slide.list", "slideListBad", part="ppt/presentation.xml")
                continue
            seen_ids.add(slide_id)
            slide_parts.append(rel["target"])
        self._slide_parts = slide_parts
        self._slide_ids = seen_ids
        if not slide_parts:
            self.add("package.required-parts", "noSlides", part="ppt/presentation.xml")
        return len(slide_parts)

    def _relationships(self, archive: zipfile.ZipFile, names: set[str]) -> None:
        for name in sorted(names):
            if not name.endswith(".rels"):
                continue
            source = "" if name == "_rels/.rels" else \
                posixpath.join(posixpath.dirname(posixpath.dirname(name)),
                               posixpath.basename(name)[:-5])
            root = _xml(archive, name)
            for rel in root.findall(_q(PKG_REL, "Relationship")):
                if rel.get("TargetMode") == "External":
                    continue
                target = _target(source, rel.get("Target", ""))
                if not target or target not in names:
                    self.add("package.relationships", "relTargetMissing",
                             part=name, target=rel.get("Target", ""))

    def _content_types(self, archive: zipfile.ZipFile, names: set[str]) -> None:
        root = self._content_types_root
        if root is None or _local(root.tag) != "Types":
            self.add("package.content-types", "contentTypesBroken",
                     part="[Content_Types].xml")
            return
        defaults = {node.get("Extension", "").lower(): node.get("ContentType", "")
                    for node in root.findall(_q(CT, "Default"))}
        overrides = {node.get("PartName", "").lstrip("/"): node.get("ContentType", "")
                     for node in root.findall(_q(CT, "Override"))}
        expected = {
            "ppt/presentation.xml": CT_PRESENTATION,
            "ppt/theme/theme1.xml": CT_THEME,
        }
        for part in self._slide_parts:
            expected[part] = CT_SLIDE
        # 家族规则：生成范围内的部件目录有确定的内容类型，写错即 mismatch
        family = (("ppt/theme/", CT_THEME), ("ppt/slideLayouts/", CT_LAYOUT), ("ppt/slideMasters/", CT_MASTER),
                  ("ppt/charts/chart", CT_CHART), ("ppt/notesSlides/", CT_NOTES_SLIDE),
                  ("ppt/notesMasters/", CT_NOTES_MASTER))
        for part in names:
            if not part.endswith(".xml"):
                continue
            for prefix, family_ctype in family:
                if part.startswith(prefix):
                    expected.setdefault(part, family_ctype)
        relationship_types = {R + "/" + suffix: ctype for suffix, ctype in (
            ("slide", CT_SLIDE), ("slideMaster", CT_MASTER), ("slideLayout", CT_LAYOUT),
            ("notesSlide", CT_NOTES_SLIDE), ("notesMaster", CT_NOTES_MASTER),
            ("chart", CT_CHART), ("theme", CT_THEME))}
        for rel_name in sorted(names):
            if not rel_name.endswith(".rels"):
                continue
            source = "" if rel_name == "_rels/.rels" else posixpath.join(
                posixpath.dirname(posixpath.dirname(rel_name)), posixpath.basename(rel_name)[:-5])
            for rel in _read_rels(archive, source).values():
                if not rel["external"] and rel["type"] in relationship_types:
                    expected[rel["target"]] = relationship_types[rel["type"]]
        self._part_types = expected
        for part in sorted(names):
            actual = overrides.get(part, defaults.get(part.rsplit(".", 1)[-1].lower(), ""))
            if part in expected and actual != expected[part]:
                self.add("package.content-types", "contentTypeMismatch", part=part,
                         expected=expected[part], actual=actual)
        # 每个部件的扩展名都要有 Default 或 Override，媒体与内嵌包靠 Default 登记
        for part in sorted(names):
            if part.endswith((".xml", ".rels")) or part in overrides:
                continue
            ext = part.rsplit(".", 1)[-1].lower()
            if ext not in defaults:
                self.add("package.content-types", "extensionUnregistered", part=part)
        # 版式与母版/备注部件按内容类型存在性统计
        layouts = [p for p, ctype in overrides.items() if ctype == CT_LAYOUT]
        masters = [p for p, ctype in overrides.items() if ctype == CT_MASTER]
        if not layouts:
            self.add("slide.layout-refs", "noLayouts", part="ppt/presentation.xml")
        if not masters:
            self.add("slide.layout-refs", "noMaster", part="ppt/presentation.xml")

    def _text_bodies(self, archive: zipfile.ZipFile, names: set[str]) -> None:
        prefixes = ("ppt/slides/", "ppt/slideLayouts/", "ppt/slideMasters/",
                    "ppt/notesSlides/", "ppt/notesMasters/")
        page_numbers = {part: n for n, part in enumerate(self._slide_parts, 1)}
        for part in sorted(names):
            if part.startswith(prefixes) and part.endswith(".xml"):
                root = _xml(archive, part)
                for body in root.iter(_q(P, "txBody")):
                    if body.find(_q(A, "p")) is None:
                        self.add("package.required-parts", "textBodyEmpty", part=part,
                                 slide=page_numbers.get(part))
                        break

    def _slides(self, archive: zipfile.ZipFile, names: set[str]) -> None:
        layout_cache: dict[str, set[tuple[str, int | None]] | None] = {}
        for index, part in enumerate(self._slide_parts, 1):
            if part not in names:
                self.add("slide.list", "slidePartMissing", part=part, slide=index)
                continue
            slide_rels = _read_rels(archive, part)
            for rid in _ref_ids(_xml(archive, part)):
                if rid not in slide_rels:
                    self.add("package.relationships", "refUnknown", part=part,
                             slide=index, rid=rid)
            layout_rels = [rel for rel in slide_rels.values()
                           if rel["type"] == R + "/slideLayout"]
            if not layout_rels:
                self.add("slide.layout-refs", "slideLayoutMissing", part=part, slide=index)
                continue
            layout_part = layout_rels[0]["target"]
            if layout_part not in names:
                self.add("slide.layout-refs", "slideLayoutMissing", part=part, slide=index)
                continue
            layout_root = _xml(archive, layout_part)
            if _local(layout_root.tag) != "sldLayout":
                self.add("slide.layout-refs", "layoutPartBad", part=layout_part, slide=index)
                continue
            name_attr = ""
            c_sld = layout_root.find(_q(P, "cSld"))
            if c_sld is not None:
                name_attr = c_sld.get("name", "")
            if not name_attr.strip():
                self.add("slide.layout-refs", "layoutUnnamed", part=layout_part, slide=index)
            master_rels = [rel for rel in _read_rels(archive, layout_part).values()
                           if rel["type"] == R + "/slideMaster"]
            if not master_rels:
                self.add("slide.layout-refs", "layoutMasterMissing", part=layout_part)
            if layout_part not in layout_cache:
                shape_ids = [node.get("id") for node in layout_root.iter(_q(P, "cNvPr"))]
                if len(shape_ids) != len(set(shape_ids)):
                    self.add("slide.layout-refs", "shapeIdDuplicate", part=layout_part, slide=index)
                layout_cache[layout_part] = _placeholders(layout_root)
            known = layout_cache[layout_part]
            for ph_type, ph_idx in _placeholders(_xml(archive, part)) - (known or set()):
                self.add("slide.placeholders", "placeholderUnknown", part=part, slide=index,
                         ph=ph_type + (f" idx={ph_idx}" if ph_idx is not None else ""))
            self.scope("slide.layout-refs", f"{part} → {layout_part}")

    def _charts(self, archive: zipfile.ZipFile, names: set[str]) -> None:
        root = self._content_types_root
        if root is None:
            return
        chart_parts = {node.get("PartName", "").lstrip("/")
                       for node in root.findall(_q(CT, "Override"))
                       if node.get("ContentType") == CT_CHART}
        chart_parts.update(part for part, ctype in self._part_types.items() if ctype == CT_CHART)
        for part in sorted(chart_parts):
            self.scope("chart.structure", part)
            if part not in names:
                self.add("chart.structure", "partMissing", part=part)
                continue
            root_elem = _xml(archive, part)
            chart_rels = _read_rels(archive, part)
            for rid in _ref_ids(root_elem):
                if rid not in chart_rels:
                    self.add("package.relationships", "refUnknown", part=part, rid=rid)
            space = root_elem.find(_q(C, "chart"))
            plot_area = None if space is None else space.find(_q(C, "plotArea"))
            if plot_area is None:
                self.add("chart.structure", "chartPlotMissing", part=part)
                continue
            groups = [child for child in plot_area
                      if _local(child.tag) in SUPPORTED_CHART_GROUPS]
            if len(groups) != 1:
                self.add("chart.structure", "chartTypeBad", part=part,
                         found=",".join(_local(c.tag) for c in groups))
                continue
            group = groups[0]
            kind = _local(group.tag)
            axes = {child_local: child for child_local, child in
                    ((_local(c.tag), c) for c in plot_area)
                    if child_local in AXIS_ELEMENTS}
            group_ax_ids = [node.get("val", "") for node in group.findall(_q(C, "axId"))]
            if kind == "pieChart":
                if group_ax_ids or axes:
                    self.add("chart.structure", "chartPieHasAxes", part=part)
            else:
                axis_ids: set[str] = set()
                for entry in axes.values():
                    for node in entry.findall(_q(C, "axId")):
                        axis_ids.add(node.get("val", ""))
                if len(group_ax_ids) != 2 or not set(group_ax_ids) <= axis_ids \
                        or set(axes) != {"catAx", "valAx"}:
                    self.add("chart.structure", "chartAxisMissing", part=part)
                else:
                    for entry in axes.values():
                        if entry.find(_q(C, "tickLblPos")) is None:
                            self.add("chart.structure", "chartTickLblMissing", part=part)
                            break
                    cross = [node.get("val", "") for entry in axes.values()
                             for node in entry.findall(_q(C, "crossAx"))]
                    if sorted(cross) != sorted(group_ax_ids):
                        self.add("chart.structure", "chartCrossAxBad", part=part)
            for ser in group.findall(_q(C, "ser")):
                self._chart_series(part, ser)
            data = root_elem.find(_q(C, "externalData"))
            rel = chart_rels.get(data.get(_q(R, "id")), {}) if data is not None else {}
            if rel.get("type") != R + "/package" or rel.get("external"):
                self.add("chart.structure", "chartDataMissing", part=part)
            elif rel["target"] not in names:
                self.add("chart.structure", "relTargetMissing", part=part,
                         target=rel["raw_target"])
            else:
                self._chart_workbook(archive, rel["target"], root_elem, part)

    def _chart_workbook(self, archive: zipfile.ZipFile, workbook_part: str,
                        chart: ET.Element, chart_part: str) -> None:
        try:
            if archive.getinfo(workbook_part).file_size > MAX_XML_BYTES:
                raise ValueError("workbook size")
            with zipfile.ZipFile(io.BytesIO(archive.read(workbook_part))) as book:
                sheets = _workbook_cells(book)
        except (zipfile.BadZipFile, KeyError, ValueError, OSError, _Problem):
            self.add("chart.structure", "chartWorkbookInvalid", part=workbook_part)
            return
        for ref in chart.iter():
            if ref.tag in (_q(C, "strRef"), _q(C, "numRef")) and not _chart_reference_matches(ref, sheets):
                self.add("chart.structure", "chartWorkbookMismatch", part=chart_part,
                         formula=ref.findtext(_q(C, "f"), ""))

    def _chart_series(self, part: str, ser: ET.Element) -> None:
        cat_cache = ser.find(_q(C, "cat") + "/" + _q(C, "strRef") + "/" + _q(C, "strCache"))
        val_cache = ser.find(_q(C, "val") + "/" + _q(C, "numRef") + "/" + _q(C, "numCache"))
        if cat_cache is None or val_cache is None:
            self.add("chart.structure", "chartCacheBad", part=part)
            return
        cat_points = cat_cache.findall(_q(C, "pt"))
        val_points = val_cache.findall(_q(C, "pt"))
        cat_node = cat_cache.find(_q(C, "ptCount"))
        val_node = val_cache.find(_q(C, "ptCount"))
        cat_count = cat_node.get("val", "") if cat_node is not None else ""
        val_count = val_node.get("val", "") if val_node is not None else ""
        if cat_count != str(len(cat_points)) or val_count != str(len(val_points)) \
                or cat_count != val_count:
            self.add("chart.structure", "chartCacheMismatch", part=part,
                     cat=cat_count, val=val_count)
        for sp_pr in ser.findall(_q(C, "spPr")):
            if sp_pr.findall(f".//{_q(A, 'srgbClr')}"):
                self.add("chart.theme-fill", "chartFixedColor", part=part)

    def _theme(self, archive: zipfile.ZipFile, names: set[str]) -> None:
        seen: set[str] = set()
        masters = sorted(part for part in names if part.endswith(".xml")
                         and part.startswith(("ppt/slideMasters/", "ppt/notesMasters/")))
        for master_part in masters:
            theme_rels = [rel for rel in _read_rels(archive, master_part).values()
                          if rel["type"] == R + "/theme"]
            if not theme_rels or theme_rels[0]["target"] not in names:
                self.add("theme.parts", "themeMissing", part=master_part)
                continue
            target = theme_rels[0]["target"]
            if target in seen:
                self.add("theme.parts", "themeShared", part=master_part)
            seen.add(target)
            self._theme_part(archive, target)

    def _theme_part(self, archive: zipfile.ZipFile, part: str) -> None:
        theme = _xml(archive, part)
        self.scope("theme.parts", part)
        clr = theme.find(_q(A, "themeElements") + "/" + _q(A, "clrScheme"))
        if clr is None:
            self.add("theme.parts", "themeSlotMissing",
                     part=part, slots="全部")
        else:
            slots = {_local(child.tag) for child in clr}
            missing = [slot for slot in THEME_SLOTS if slot not in slots]
            if missing:
                self.add("theme.parts", "themeSlotMissing",
                         part=part, slots="、".join(missing))
        fonts = theme.find(_q(A, "themeElements") + "/" + _q(A, "fontScheme"))
        if fonts is None:
            self.add("theme.parts", "fontSchemeMissing", part=part)
            return
        for role in ("majorFont", "minorFont"):
            entry = fonts.find(_q(A, role))
            if entry is None or entry.find(_q(A, "latin")) is None \
                    or entry.find(_q(A, "ea")) is None \
                    or not (entry.find(_q(A, "latin")).get("typeface", "").strip()
                            and entry.find(_q(A, "ea")).get("typeface", "").strip()):
                self.add("theme.parts", "fontSchemeMissing",
                         part=part, role=role)

    def _sections(self, archive: zipfile.ZipFile, names: set[str]) -> None:
        presentation = _xml(archive, "ppt/presentation.xml")
        ext_list = presentation.find(_q(P, "extLst"))
        section_list = None
        if ext_list is not None:
            for ext in ext_list.findall(_q(P, "ext")):
                found = ext.find(_q(P14, "sectionLst"))
                if found is not None:
                    section_list = found
        if section_list is None:
            self.add("sections.coverage", "sectionsAbsent", part="ppt/presentation.xml")
            return
        covered: dict[str, str] = {}
        for section in section_list.findall(_q(P14, "section")):
            name = section.get("name", "")
            for node in section.findall(_q(P14, "sldIdLst") + "/" + _q(P14, "sldId")):
                slide_id = node.get("id", "")
                if slide_id not in self._slide_ids:
                    self.add("sections.coverage", "sectionSlideUnknown",
                             part="ppt/presentation.xml", section=name, id=slide_id)
                elif slide_id in covered:
                    self.add("sections.coverage", "sectionSlideDuplicate",
                             part="ppt/presentation.xml", id=slide_id,
                             sections=f"{covered[slide_id]}、{name}")
                else:
                    covered[slide_id] = name
        missing = sorted(self._slide_ids - set(covered))
        if missing:
            self.add("sections.coverage", "sectionSlideMissing",
                     part="ppt/presentation.xml", ids="、".join(missing))

    # ── 结果 ────────────────────────────────────────────────────────────

    def _result(self, slides: int) -> dict:
        by_check: dict[str, list[_Problem]] = {}
        for problem in self.problems:
            by_check.setdefault(problem.check, []).append(problem)
        checks = []
        for cid in CHECK_ORDER:
            found = by_check.get(cid, [])
            scope = "、".join(self.scopes.get(cid, []))
            checks.append({
                "id": cid,
                "status": "fail" if found else "pass",
                "scope": scope,
                "note": "" if not found else _t(self.loc, "pptxcheck.problemCount",
                                                count=len(found)),
            })
        return {"schema": RESULT_SCHEMA, "file": self.path.name, "sha256": self.sha256,
                "slides": slides, "ok": not self.problems, "checks": checks,
                "problems": [p.as_dict(self.loc) for p in self.problems]}


def check_deck(path: Path, locale: str | None = None) -> dict:
    """检查一份 PPTX 的文件层有效性；返回 ae-pptx-check/1 结构化结果。"""
    path = Path(path)
    loc = i18n.resolve_locale(locale, system_languages=i18n.system_languages())
    if not path.is_file():
        raise CheckInputError(_t(loc, "pptxcheck.fileMissing", path=str(path)))
    return _Checker(path, loc).run()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="检查 PPTX 包结构与关键部件（issue #180 文件层检查）")
    parser.add_argument("pptx", help="要检查的 .pptx 文件")
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
        print(_t("zh-CN", "pptxcheck.badLocale", locale=exc.params.get("locale", "")),
              file=sys.stderr)
        print(_t("en", "pptxcheck.badLocale", locale=exc.params.get("locale", "")),
              file=sys.stderr)
        return 2
    try:
        result = check_deck(Path(args.pptx), locale=loc)
    except CheckInputError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    if args.json:
        print(json.dumps(result, ensure_ascii=False))
    else:
        header = _t(loc, "pptxcheck.summary", file=result["file"],
                    verdict=_t(loc, "pptxcheck.verdict.ok" if result["ok"]
                               else "pptxcheck.verdict.fail"),
                    slides=result["slides"])
        print(header)
        for check in result["checks"]:
            status = _t(loc, "pptxcheck.status." + check["status"])
            print(f"  [{status}] {check['id']} {check['scope']} {check['note']}".rstrip())
        for problem in result["problems"]:
            location = f"（{problem['part']}）" if loc == "zh-CN" else f" ({problem['part']})"
            slide = f" slide {problem['slide']}" if problem["slide"] else ""
            print(f"  - {problem['check']}{location}{slide}: {problem['reason']}")
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
