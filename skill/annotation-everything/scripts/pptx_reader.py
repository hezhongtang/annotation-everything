"""只读 PPTX OOXML 包结构；不解压、不执行嵌入内容、不修改源文件。"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import posixpath
import xml.etree.ElementTree as ET
import zipfile
import zlib


FORMAT_VERSION = 1
CAPABILITY_VERSION = 1
# 批注 pptx 定位字段的版本（note["pptx"]["v"]）：幻灯片定位以这里的元素身份模型为事实源
ANCHOR_VERSION = 1
P = "http://schemas.openxmlformats.org/presentationml/2006/main"
A = "http://schemas.openxmlformats.org/drawingml/2006/main"
R = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
PKG_REL = "http://schemas.openxmlformats.org/package/2006/relationships"


class PptxError(ValueError):
    def __init__(self, code: str, part: str = "", detail: str = ""):
        message = code + (f": {part}" if part else "") + (f": {detail}" if detail else "")
        super().__init__(message)
        self.code, self.part, self.detail = code, part, detail


def _q(namespace: str, name: str) -> str:
    return "{" + namespace + "}" + name


def _digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


MAX_MEMBERS = 4096
MAX_MEMBER_BYTES = 64 * 1024 * 1024
MAX_XML_BYTES = 8 * 1024 * 1024
MAX_TOTAL_BYTES = 512 * 1024 * 1024
MAX_COMPRESSION_RATIO = 1000


def _validate_archive(archive: zipfile.ZipFile) -> None:
    infos = archive.infolist()
    if len(infos) > MAX_MEMBERS:
        raise PptxError("tooLarge", "", "member count")
    seen = set()
    total = 0
    for info in infos:
        name = info.filename
        drive = name.split("/", 1)[0]
        if (not name or name.startswith("/") or "\\" in name or "\x00" in name
                or ".." in name.split("/") or len(drive) == 2 and drive[1] == ":" and drive[0].isalpha()):
            raise PptxError("unsafePath", name)
        if name in seen:
            raise PptxError("duplicateMember", name)
        seen.add(name)
        if info.file_size > MAX_MEMBER_BYTES or (name.endswith((".xml", ".rels")) and
                info.file_size > MAX_XML_BYTES):
            raise PptxError("tooLarge", name)
        total += info.file_size
        if total > MAX_TOTAL_BYTES:
            raise PptxError("tooLarge", name, "total uncompressed size")
        if info.flag_bits & 0x1:
            raise PptxError("encrypted", name)
        if info.compress_type not in (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED):
            raise PptxError("unsupportedCompression", name)
        if info.file_size >= 1024 * 1024 and (info.compress_size == 0 or
                info.file_size / info.compress_size > MAX_COMPRESSION_RATIO):
            raise PptxError("compressionRatio", name)


def _validate_local_headers(path: Path, archive: zipfile.ZipFile) -> None:
    regions = []
    try:
        with path.open("rb") as source:
            for info in archive.infolist():
                source.seek(info.header_offset)
                header = source.read(30)
                if len(header) != 30 or header[:4] != b"PK\x03\x04":
                    raise PptxError("badCompression", info.filename, "local header")
                name_length = int.from_bytes(header[26:28], "little")
                extra_length = int.from_bytes(header[28:30], "little")
                encoding = "utf-8" if info.flag_bits & 0x800 else "cp437"
                try:
                    local_name = source.read(name_length).decode(encoding)
                except UnicodeDecodeError:
                    raise PptxError("badCompression", info.filename, "local name") from None
                if local_name != info.filename or int.from_bytes(header[8:10], "little") != info.compress_type:
                    raise PptxError("badCompression", info.filename, "local metadata")
                start = info.header_offset + 30 + name_length + extra_length
                end = start + info.compress_size
                if end > archive.start_dir:
                    raise PptxError("badCompression", info.filename, "member extent")
                regions.append((start, end, info.filename))
    except OSError as exc:
        raise PptxError("notZip", str(path), type(exc).__name__) from None
    regions.sort()
    for previous, current in zip(regions, regions[1:]):
        if current[0] < previous[1]:
            raise PptxError("badCompression", current[2], "overlapping member data")


def _xml(archive: zipfile.ZipFile, name: str) -> ET.Element:
    info = archive.NameToInfo.get(name)
    if info is None:
        raise PptxError("missingPart", name)
    if info.file_size > MAX_XML_BYTES:
        raise PptxError("tooLarge", name)

    class NoDTDBuilder(ET.TreeBuilder):
        def doctype(self, _name, _pubid, _system):
            raise PptxError("dtd", name)

    try:
        data = archive.read(name)
        return ET.fromstring(data, parser=ET.XMLParser(target=NoDTDBuilder()))
    except PptxError:
        raise
    except ET.ParseError as exc:
        raise PptxError("badXml", name, str(exc)) from None
    except (OSError, zipfile.BadZipFile, RuntimeError, NotImplementedError, EOFError, zlib.error) as exc:
        raise PptxError("notZip", name, type(exc).__name__) from None


def _target(source: str, target: str) -> str:
    if not target or "\\" in target:
        raise PptxError("unsafePath", target)
    resolved = target.lstrip("/") if target.startswith("/") else posixpath.normpath(
        posixpath.join(posixpath.dirname(source), target))
    if resolved.startswith("../") or resolved == ".." or resolved.startswith("/"):
        raise PptxError("unsafePath", target)
    return resolved


def _relations(archive: zipfile.ZipFile, source: str) -> dict[str, dict[str, object]]:
    name = posixpath.join(posixpath.dirname(source), "_rels", posixpath.basename(source) + ".rels")
    if source == "":
        name = "_rels/.rels"
    root = _xml(archive, name)
    result: dict[str, dict[str, object]] = {}
    for rel in root.findall(_q(PKG_REL, "Relationship")):
        rid = rel.get("Id", "")
        if not rid or rid in result:
            raise PptxError("invalidPackage", name, "duplicate relationship id")
        external = rel.get("TargetMode") == "External"
        target = rel.get("Target", "")
        result[rid] = {
            "type": rel.get("Type", ""),
            "target": target if external else _target(source, target),
            "external": external,
        }
    return result


def _run_style(run: ET.Element) -> tuple[dict[str, object], str]:
    props = run.find(_q(A, "rPr"))
    if props is None:
        return {}, _digest("")
    style: dict[str, object] = {}
    for source, target in (("b", "bold"), ("i", "italic"), ("u", "underline"),
                           ("sz", "size"), ("lang", "language")):
        if source in props.attrib:
            value: object = props.attrib[source]
            if source in ("b", "i"):
                value = value not in ("0", "false", "off")
            elif source == "sz":
                try:
                    value = int(value)
                except ValueError:
                    value = None
            style[target] = value
    latin = props.find(_q(A, "latin"))
    if latin is not None and latin.get("typeface"):
        style["typeface"] = latin.get("typeface")
    color = props.find(_q(A, "solidFill") + "/" + _q(A, "srgbClr"))
    if color is not None and color.get("val"):
        style["color"] = color.get("val")
    return style, hashlib.sha256(ET.tostring(props, encoding="utf-8")).hexdigest()


def _paragraph(element: ET.Element, index: int) -> dict:
    text = ""
    runs = []
    for child in element:
        # <a:br/> 是段落内换行，占一个 Unicode 偏移并保留为 run 边界，
        # 否则字符偏移与引用原文对不上。
        if child.tag == _q(A, "br"):
            start = len(text)
            text += "\n"
            style, style_digest = _run_style(child)
            runs.append({"start": start, "end": len(text), "text": "\n",
                         "style": style, "styleDigest": style_digest})
        elif child.tag in (_q(A, "r"), _q(A, "fld")):
            value = child.findtext(_q(A, "t"), default="")
            start = len(text)
            text += value
            style, style_digest = _run_style(child)
            runs.append({"start": start, "end": len(text), "text": value,
                         "style": style, "styleDigest": style_digest})
    return {"index": index, "text": text, "digest": _digest(text), "runs": runs}


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _rect(shape: ET.Element) -> tuple[int, int, int, int] | None:
    transform = shape.find(_q(P, "spPr") + "/" + _q(A, "xfrm"))
    if transform is None:
        transform = shape.find(_q(P, "grpSpPr") + "/" + _q(A, "xfrm"))
    if transform is None:
        transform = shape.find(_q(P, "xfrm"))
    if transform is None:
        return None
    try:
        offset = transform.find(_q(A, "off"))
        extent = transform.find(_q(A, "ext"))
        x, y = int(offset.get("x", "")), int(offset.get("y", ""))
        cx, cy = int(extent.get("cx", "")), int(extent.get("cy", ""))
    except (AttributeError, ValueError):
        return None
    return (x, y, cx, cy) if cx >= 0 and cy >= 0 else None


def _number(value: float) -> int | float:
    return int(value) if value.is_integer() else value


def _geometry(rect: tuple[int, int, int, int] | None, transform: tuple[float, float, float, float],
              width: int, height: int) -> dict | None:
    if rect is None:
        return None
    x, y, cx, cy = rect
    sx, sy, tx, ty = transform
    left, top = x * sx + tx, y * sy + ty
    right, bottom = (x + cx) * sx + tx, (y + cy) * sy + ty
    return {
        "source": {"x": _number(left), "y": _number(top), "cx": _number(right - left),
                   "cy": _number(bottom - top), "unit": "emu"},
        "normalized": {"x": left / width, "y": top / height,
                       "width": (right - left) / width, "height": (bottom - top) / height},
    }


def _child_transform(group: ET.Element, parent: tuple[float, float, float, float]) -> tuple[float, float, float, float] | None:
    rect = _rect(group)
    transform = group.find(_q(P, "grpSpPr") + "/" + _q(A, "xfrm"))
    if rect is None or transform is None:
        return None
    try:
        child_offset = transform.find(_q(A, "chOff"))
        child_extent = transform.find(_q(A, "chExt"))
        ox, oy = int(child_offset.get("x", "")), int(child_offset.get("y", ""))
        ccx, ccy = int(child_extent.get("cx", "")), int(child_extent.get("cy", ""))
    except (AttributeError, ValueError):
        return None
    x, y, cx, cy = rect
    if ccx <= 0 or ccy <= 0:
        return None
    psx, psy, ptx, pty = parent
    sx, sy = psx * cx / ccx, psy * cy / ccy
    return sx, sy, psx * x + ptx - ox * sx, psy * y + pty - oy * sy


def _props(shape: ET.Element) -> ET.Element | None:
    for child in shape:
        if _local(child.tag).startswith("nv"):
            for prop in child:
                if _local(prop.tag) == "cNvPr":
                    return prop
    return None


def _references(shape: ET.Element, relations: dict[str, dict[str, object]]) -> list[dict]:
    result = []
    seen = set()
    for node in shape.iter():
        for attribute, rid in node.attrib.items():
            if not attribute.startswith("{" + R + "}") or rid in seen or rid not in relations:
                continue
            seen.add(rid)
            rel = relations[rid]
            result.append({"id": rid, "type": rel["type"], "target": rel["target"], "external": rel["external"]})
    return result


def _kind(shape: ET.Element, references: list[dict]) -> tuple[str, str]:
    local = _local(shape.tag)
    if local == "sp":
        return "shape", ""
    if local == "grpSp":
        return "group", ""
    if local == "cxnSp":
        return "connector", ""
    if local == "pic":
        media = any(_local(node.tag) in ("videoFile", "audioFile") for node in shape.iter())
        media = media or any(str(ref["type"]).endswith(("/video", "/audio", "/media")) for ref in references)
        return ("media" if media else "picture"), ""
    data = shape.find(_q(A, "graphic") + "/" + _q(A, "graphicData"))
    uri = data.get("uri", "") if data is not None else ""
    if local == "graphicFrame":
        if uri == A + "/table" or data is not None and data.find(_q(A, "tbl")) is not None:
            return "table", uri
        if "/chart" in uri:
            return "chart", uri
        if "/diagram" in uri:
            return "smartArt", uri
        # 只认官方 OLE uri 与 oleObj 结构，子串匹配会把 console 之类误判成 OLE。
        if uri in (P + "/ole", A + "/oleobject") or any(_local(node.tag) == "oleObj" for node in shape.iter()):
            return "ole", uri
    return "unknown", uri or local


def _paragraphs(parent: ET.Element, path: str) -> list[dict]:
    return [_paragraph(p, index) for index, p in enumerate(parent.findall(path))]


def _capabilities(kind: str, geometry: dict | None, shape: ET.Element, parent: str | None) -> dict:
    has_text = shape.find(_q(P, "txBody")) is not None
    unsupported = (shape.find(".//" + _q(A, "extLst") + "/" + _q(A, "ext")) is not None
                   or any(_local(node.tag) in ("AlternateContent", "fld") for node in shape.iter()))
    ordinary = kind == "shape" and parent is None and not unsupported
    return {
        "display": True,
        "select": geometry is not None,
        "locate": geometry is not None,
        "textWrite": ordinary and has_text,
        "geometryWrite": ordinary and geometry is not None,
        "styleWrite": ordinary,
    }


def _evidence_digest(name: str, description: str, kind: str, text_digest: str, geometry: dict | None) -> str:
    evidence = {"name": name, "description": description, "kind": kind, "textDigest": text_digest,
                "geometry": geometry["normalized"] if geometry is not None else None}
    return _digest(json.dumps(evidence, ensure_ascii=False, sort_keys=True, separators=(",", ":")))


def _unique_key(shape_id: str, evidence: str, elements: list[dict]) -> str:
    base = "e" + shape_id if shape_id else "x" + evidence[:12]
    used = {element["key"] for element in elements}
    if base not in used:
        return base
    candidate = base + "~" + evidence[:12]
    suffix = 2
    while candidate in used:
        candidate = base + "~" + evidence[:12] + "~" + str(suffix)
        suffix += 1
    return candidate


def _elements(slide: ET.Element, width: int, height: int, relations: dict[str, dict[str, object]] | None = None,
              slide_key: str = "", part: str = "", diagnostics: list[dict] | None = None) -> list[dict]:
    relations = relations or {}
    diagnostics = diagnostics if diagnostics is not None else []
    tree = slide.find(_q(P, "cSld") + "/" + _q(P, "spTree"))
    if tree is None:
        # cSld/spTree 是 ECMA-376 必需结构；缺失时不能静默给出空元素页。
        raise PptxError("invalidPackage", part, "missing cSld/spTree")
    result = []

    def visit(shape: ET.Element, parent: str | None, depth: int,
              transform: tuple[float, float, float, float] | None) -> None:
        props = _props(shape)
        if props is None:
            return
        shape_id = props.get("id", "")
        references = _references(shape, relations)
        kind, detail = _kind(shape, references)
        geometry = _geometry(_rect(shape), transform, width, height) if transform is not None else None
        paragraphs = _paragraphs(shape, _q(P, "txBody") + "/" + _q(A, "p"))
        cells = []
        if kind == "table":
            for row, table_row in enumerate(shape.findall(".//" + _q(A, "tr"))):
                for column, cell in enumerate(table_row.findall(_q(A, "tc"))):
                    cell_paragraphs = _paragraphs(cell, _q(A, "txBody") + "/" + _q(A, "p"))
                    cell_text = "\n".join(p["text"] for p in cell_paragraphs)
                    cells.append({"row": row, "column": column, "text": cell_text, "digest": _digest(cell_text),
                                  "paragraphs": cell_paragraphs,
                                  "capabilities": {"display": True, "select": True, "locate": True,
                                                   "textWrite": False, "geometryWrite": False, "styleWrite": False}})
            text = "\n".join(cell["text"] for cell in cells)
        elif kind == "unknown":
            paragraphs = _paragraphs(shape, ".//" + _q(A, "p"))
            text = "\n".join(p["text"] for p in paragraphs)
        else:
            text = "\n".join(p["text"] for p in paragraphs)
        evidence_text = text
        if kind == "group":
            evidence_text = "\n".join("".join(t.text or "" for t in p.iter(_q(A, "t")))
                                      for p in shape.findall(".//" + _q(A, "p")))
        evidence = _evidence_digest(props.get("name", ""), props.get("descr", ""), kind,
                                    _digest(evidence_text), geometry)
        key = _unique_key(shape_id, evidence, result)
        element = {
            "id": shape_id,
            "key": key,
            "name": props.get("name", ""),
            "description": props.get("descr", ""),
            "kind": kind,
            "z": len(result),
            "parent": parent,
            "depth": depth,
            "geometry": geometry,
            "text": text,
            "textDigest": _digest(text),
            "evidenceDigest": evidence,
            "paragraphs": paragraphs,
            "references": references,
            "capabilities": _capabilities(kind, geometry, shape, parent),
        }
        if cells:
            element["cells"] = cells
        result.append(element)
        if kind == "unknown":
            diagnostics.append({"code": "unknownStructure", "part": part, "slide": slide_key,
                                "element": key, "detail": detail})
        if kind == "group":
            child_transform = _child_transform(shape, transform) if transform is not None else None
            child_start = len(result)
            for child in shape:
                if child.tag in (_q(P, "sp"), _q(P, "grpSp"), _q(P, "pic"), _q(P, "graphicFrame"), _q(P, "cxnSp")):
                    visit(child, key, depth + 1, child_transform)
            group_text = "\n".join(e["text"] for e in result[child_start:] if e["text"])
            element["text"] = group_text
            element["textDigest"] = _digest(group_text)

    for node in (list(tree) if tree is not None else []):
        if node.tag in (_q(P, "sp"), _q(P, "grpSp"), _q(P, "pic"), _q(P, "graphicFrame"), _q(P, "cxnSp")):
            visit(node, None, 0, (1.0, 1.0, 0.0, 0.0))
        elif _props(node) is not None:
            visit(node, None, 0, (1.0, 1.0, 0.0, 0.0))

    counts = {}
    for element in result:
        if element["id"]:
            counts[element["id"]] = counts.get(element["id"], 0) + 1
    occurrences = {}
    for element in result:
        shape_id = element["id"]
        ambiguous = not shape_id or counts.get(shape_id, 0) > 1
        if not ambiguous:
            continue
        for capability in ("locate", "textWrite", "geometryWrite", "styleWrite"):
            element["capabilities"][capability] = False
        for cell in element.get("cells", []):
            cell["capabilities"]["locate"] = False
        if not shape_id:
            diagnostics.append({"code": "missingShapeId", "part": part, "slide": slide_key,
                                "element": element["key"], "detail": ""})
            continue
        occurrences[shape_id] = occurrences.get(shape_id, 0) + 1
        if occurrences[shape_id] == 2:
            diagnostics.append({"code": "duplicateShapeId", "part": part, "slide": slide_key,
                                "element": element["key"], "detail": shape_id})
    return result


def _optional_relations(archive: zipfile.ZipFile, source: str) -> dict[str, dict[str, object]]:
    name = posixpath.join(posixpath.dirname(source), "_rels", posixpath.basename(source) + ".rels")
    return _relations(archive, source) if name in archive.NameToInfo else {}


def _notes(archive: zipfile.ZipFile, relations: dict[str, dict[str, object]], slide_key: str,
           diagnostics: list[dict]) -> dict | None:
    found = next(((rid, rel) for rid, rel in relations.items() if rel["type"] == R + "/notesSlide"), None)
    if found is None:
        return None
    rid, rel = found
    part = str(rel["target"])
    try:
        if rel["external"]:
            raise PptxError("invalidPackage", part, "external notes")
        root = _xml(archive, part)
        if root.tag != _q(P, "notes"):
            raise PptxError("invalidPackage", part, "not speaker notes")
    except PptxError as exc:
        if exc.code not in ("badXml", "missingPart", "invalidPackage"):
            raise
        diagnostic = {"code": exc.code, "part": part, "slide": slide_key, "element": "", "detail": exc.detail}
        diagnostics.append(diagnostic)
        return {"status": "error", "relationshipId": rid, "part": part, "detail": exc.detail,
                "capabilities": {"display": False, "select": False, "locate": False,
                                 "textWrite": False, "geometryWrite": False, "styleWrite": False}}
    shapes = []
    for shape in root.findall(".//" + _q(P, "sp")):
        placeholder = shape.find(_q(P, "nvSpPr") + "/" + _q(P, "nvPr") + "/" + _q(P, "ph"))
        kind = placeholder.get("type", "body") if placeholder is not None else ""
        if kind == "body":
            shapes.append(shape)
    if not shapes:
        for shape in root.findall(".//" + _q(P, "sp")):
            placeholder = shape.find(_q(P, "nvSpPr") + "/" + _q(P, "nvPr") + "/" + _q(P, "ph"))
            kind = placeholder.get("type", "") if placeholder is not None else ""
            if kind not in ("sldImg", "sldNum", "hdr", "ftr", "dt"):
                shapes.append(shape)
    paragraphs = []
    for shape in shapes:
        for paragraph in shape.findall(_q(P, "txBody") + "/" + _q(A, "p")):
            paragraphs.append(_paragraph(paragraph, len(paragraphs)))
    text = "\n".join(p["text"] for p in paragraphs)
    return {"status": "available", "relationshipId": rid, "part": part,
            "text": text, "digest": _digest(text), "paragraphs": paragraphs,
            "capabilities": {"display": True, "select": True, "locate": True,
                             "textWrite": True, "geometryWrite": False, "styleWrite": False}}


def read(path: Path) -> dict:
    """读取源 PPTX，返回版本化的幻灯片结构；输入错误统一抛出 PptxError。"""
    path = Path(path)
    try:
        digest = hashlib.sha256()
        with path.open("rb") as source:
            for chunk in iter(lambda: source.read(1 << 20), b""):
                digest.update(chunk)
    except OSError as exc:
        raise PptxError("unreadable", str(path), str(exc)) from None
    try:
        with zipfile.ZipFile(path) as archive:
            _validate_archive(archive)
            _validate_local_headers(path, archive)
            content_types = _xml(archive, "[Content_Types].xml")
            if _local(content_types.tag) != "Types":
                raise PptxError("invalidPackage", "[Content_Types].xml", "not content types")
            root_rels = _relations(archive, "")
            main = next((rel for rel in root_rels.values()
                         if rel["type"] == R + "/officeDocument" and not rel["external"]), None)
            if main is None:
                raise PptxError("missingPart", "ppt/presentation.xml")
            main_part = str(main["target"])
            presentation = _xml(archive, main_part)
            if presentation.tag != _q(P, "presentation"):
                raise PptxError("invalidPackage", main_part, "not a presentation")
            presentation_rels = _relations(archive, main_part)
            size = presentation.find(_q(P, "sldSz"))
            try:
                width, height = int(size.get("cx", "")), int(size.get("cy", ""))
            except (AttributeError, ValueError):
                raise PptxError("invalidPackage", main_part, "invalid slide size") from None
            if width <= 0 or height <= 0:
                raise PptxError("invalidPackage", main_part, "invalid slide size")
            slides = []
            diagnostics = []
            seen = set()
            for n, item in enumerate(presentation.findall(_q(P, "sldIdLst") + "/" + _q(P, "sldId")), 1):
                slide_id = item.get("id", "")
                rid = item.get(_q(R, "id"), "")
                if not slide_id or slide_id in seen or rid not in presentation_rels:
                    raise PptxError("invalidPackage", main_part, "invalid slide list")
                seen.add(slide_id)
                rel = presentation_rels[rid]
                if rel["external"] or rel["type"] != R + "/slide":
                    raise PptxError("invalidPackage", main_part, "invalid slide relationship")
                part = str(rel["target"])
                slide = _xml(archive, part)
                if slide.tag != _q(P, "sld"):
                    raise PptxError("invalidPackage", part, "not a slide")
                slide_key = "s" + slide_id
                relations = _optional_relations(archive, part)
                elements = _elements(slide, width, height, relations, slide_key, part, diagnostics)
                slides.append({"n": n, "id": slide_id, "key": slide_key, "relationshipId": rid, "part": part,
                               "elements": elements, "notes": _notes(archive, relations, slide_key, diagnostics)})
    except PptxError:
        raise
    except (OSError, zipfile.BadZipFile, RuntimeError, NotImplementedError, EOFError, zlib.error) as exc:
        raise PptxError("notZip", str(path), type(exc).__name__) from None
    return {
        "formatVersion": FORMAT_VERSION,
        "capabilityVersion": CAPABILITY_VERSION,
        "fingerprint": digest.hexdigest(),
        "canvas": {
            "width": width,
            "height": height,
            "unit": "emu",
            "orientation": "landscape" if width > height else "portrait" if height > width else "square",
        },
        "slides": slides,
        "diagnostics": diagnostics,
    }
