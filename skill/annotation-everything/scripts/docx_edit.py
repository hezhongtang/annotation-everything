#!/usr/bin/env python3
"""按审阅批注回写 DOCX：只改目标 w:t，保留其他 XML 字节与 ZIP 成员的压缩数据。

apply REVIEW --batch N / --id ID：处理快捷删除与替换。
replace REVIEW --paragraph N --from OLD --to NEW：Agent 核对自由批注后的单条替换。
段落序号从 0 开始。所有选择先整体校验；任何一条失败都不写。不会处理批注状态。
"""
from __future__ import annotations

import copy
import hashlib
import io
import json
import os
import posixpath
import re
import stat
import struct
import sys
import tempfile
import zipfile
import zlib
from dataclasses import dataclass, field
from pathlib import Path
from xml.parsers import expat
from xml.sax.saxutils import escape

import docx_review as dr
import i18n
import ledger
import notes_store as store
from build_viewer import BuildError, resolve_sources

MAX_PACKAGE = 256 * 1024 * 1024
MAX_EXPANDED = 512 * 1024 * 1024
W = dr.W_NS + "}"
W14 = dr.W14[1:]
ERROR_KEYS = {
    i18n.CLI_ERROR_INVALID_CHOICE: "docx.edit.cli.invalidChoice",
    i18n.CLI_ERROR_MISSING_VALUE: "docx.edit.cli.missingValue",
    i18n.CLI_ERROR_UNKNOWN_ARGUMENT: "docx.edit.cli.unknownArgument",
    i18n.CLI_ERROR_MISSING_ARGUMENT: "docx.edit.cli.missingArgument",
    i18n.CLI_ERROR_GENERIC: "docx.edit.cli.arguments",
    i18n.CLI_ERROR_EMPTY_LOCALE: "docx.edit.cli.emptyLocale",
    i18n.CLI_ERROR_INVALID_LOCALE: "docx.edit.cli.invalidLocale",
}


class EditError(ValueError):
    def __init__(self, code: str, detail: str = "", *, item: str = ""):
        self.code, self.detail, self.item = code, detail, item
        super().__init__(f"{code}: {detail}")


class SelectionError(EditError):
    def __init__(self, errors: list[EditError]):
        self.errors = errors
        super().__init__("selection")


@dataclass
class TextElement:
    start: int
    opening_end: int
    stop: int
    offset: int
    qname: bytes
    empty: bool
    text: str = ""


@dataclass
class Paragraph:
    n: int
    pid: str | None
    text: str = ""
    elements: list[TextElement] = field(default_factory=list)
    barriers: list[int] = field(default_factory=list)


def _tag_end(raw: bytes, start: int) -> int:
    quote = 0
    for n in range(start, len(raw)):
        ch = raw[n]
        if quote:
            if ch == quote:
                quote = 0
        elif ch in (34, 39):
            quote = ch
        elif ch == 62:
            return n + 1
    raise EditError("xml", "unterminated tag")


def parse_document(raw: bytes) -> list[Paragraph]:
    """Expat 的 CurrentByteIndex 是源字节位置；实体、CDATA、BOM 都不以字符串长度猜位置。"""
    if len(raw) > dr.MAX_PART:
        raise EditError("size")
    if raw.startswith((b"\xff\xfe", b"\xfe\xff")) or b"\0" in raw[:100]:
        raise EditError("encoding")
    parser = expat.ParserCreate(namespace_separator="}")
    paras: list[Paragraph] = []
    stack: list[Paragraph] = []
    active: list[TextElement] = []

    def declaration(_version, encoding, _standalone):
        if encoding and encoding.lower().replace("-", "") != "utf8":
            raise EditError("encoding")

    def doctype(*_args):
        raise EditError("xml", "DTD")

    def start(name, attrs):
        if active:
            raise EditError("xml", "nested element inside text")
        if name == W + "p":
            pid = attrs.get(W14 + "paraId", "")
            p = Paragraph(len(paras), pid.upper() if dr.PARA_ID.fullmatch(pid) else None)
            paras.append(p)
            stack.append(p)
        if not stack:
            return
        p = stack[-1]
        if name == W + "t":
            at = parser.CurrentByteIndex
            end = _tag_end(raw, at)
            tag = raw[at:end]
            qname = re.match(rb"<([^\s/>]+)", tag).group(1)
            node = TextElement(at, end, end, len(p.text), qname, tag.rstrip().endswith(b"/>"))
            p.elements.append(node)
            active.append(node)
        elif name in (W + "tab", W + "ptab", W + "noBreakHyphen", W + "softHyphen", W + "sym"):
            text = {W + "tab": "\t", W + "ptab": "\t", W + "noBreakHyphen": "‑", W + "softHyphen": "­"}.get(name)
            if text is None:
                try:
                    text = chr(int(attrs.get(W + "char", ""), 16))
                    text = dr.BULLET_MAP.get(text, text)
                except (ValueError, OverflowError):
                    text = ""
            p.text += text
        elif name in (W + "br", W + "cr") and attrs.get(W + "type") != "page":
            p.barriers.append(len(p.text))   # DOM 文本偏移不计 <br>，但不能跨过它删除或替换

    def end(name):
        if name == W + "t" and active:
            node = active.pop()
            if not node.empty:
                node.stop = _tag_end(raw, parser.CurrentByteIndex)
        if name == W + "p":
            stack.pop()

    def text(value):
        if active and stack:
            active[-1].text += value
            stack[-1].text += value

    parser.XmlDeclHandler = declaration
    parser.StartDoctypeDeclHandler = doctype
    parser.StartElementHandler, parser.EndElementHandler = start, end
    parser.CharacterDataHandler = text
    try:
        parser.Parse(raw, True)
    except expat.ExpatError as exc:
        raise EditError("xml", str(exc)) from None
    return paras


def _xml_text(text: str) -> bytes:
    if any(not (c in "\t\n\r" or 0x20 <= ord(c) <= 0xD7FF or 0xE000 <= ord(c) <= 0xFFFD
                or 0x10000 <= ord(c) <= 0x10FFFF) for c in text):
        raise EditError("characters")
    return escape(text).replace("\r", "&#13;").encode("utf-8")


def _element_bytes(raw: bytes, node: TextElement, text: str) -> bytes:
    opening = raw[node.start:node.opening_end]
    # 目标元素总是显式 preserve；其他属性、前缀以及其他元素不动。
    space = rb"\bxml:space\s*=\s*(['\"])[^'\"]*\1"
    if re.search(space, opening):
        opening = re.sub(space, b'xml:space="preserve"', opening)
    else:
        at = opening.rfind(b"/>") if node.empty else len(opening) - 1
        opening = opening[:at] + b' xml:space="preserve"' + opening[at:]
    if node.empty:
        opening = opening[:opening.rfind(b"/>")] + b">"
    return opening + _xml_text(text) + b"</" + node.qname + b">"


def _fold(text: str) -> tuple[str, list[int]]:
    positions = [i for i, char in enumerate(text) if not char.isspace()]
    return "".join(text[i] for i in positions), positions


def _matches(text: str, before: str) -> list[tuple[int, int]]:
    if not before:
        raise EditError("empty")
    out, at = [], text.find(before)
    while at >= 0:
        out.append((at, at + len(before)))
        at = text.find(before, at + 1)
    if out:
        return out
    flat, positions = _fold(text)
    want, _ = _fold(before)
    if not want:
        return []
    at = flat.find(want)
    while at >= 0:
        out.append((positions[at], positions[at + len(want) - 1] + 1))
        at = flat.find(want, at + 1)
    return out


def _locate(paras: list[Paragraph], edit: dict, restrictions: dict[int, set[str]]) -> tuple[Paragraph, int, int]:
    anchor = edit.get("anchor")
    if anchor is not None:
        if not dr.usable_anchor(anchor) or anchor.get("changed"):
            raise EditError("anchor")
        if anchor.get("endPara"):
            raise EditError("range")
        ref = anchor["para"]
    else:
        ref = {"n": edit.get("paragraph"), "id": edit.get("para_id")}
    n, pid, digest = ref.get("n"), ref.get("id"), ref.get("digest")
    if not isinstance(n, int) or isinstance(n, bool) or n < 0:
        raise EditError("anchor")
    candidates = [p for p in paras if pid and p.pid == str(pid).upper()]
    if len(candidates) != 1:
        candidates = [p for p in paras if digest and dr.text_digest(p.text) == digest]
        ordinal = next((p for p in candidates if p.n == n), None)
        if ordinal:
            candidates = [ordinal]
        if not digest:
            candidates = [p for p in paras if p.n == n]
    if not candidates and n < len(paras) and restrictions.get(n):
        # 公式等可见文字不全来自 w:t，摘要可能不同；先说明结构限制，不把它误报为定位失效。
        raise EditError("readonly", ",".join(sorted(restrictions[n])))
    if len(candidates) != 1:
        raise EditError("ambiguous" if candidates else "notFound")
    p = candidates[0]
    if restrictions.get(p.n):
        raise EditError("readonly", ",".join(sorted(restrictions[p.n])))
    if digest and dr.text_digest(p.text) != digest:
        raise EditError("anchor")
    preferred = (anchor.get("start"), anchor.get("end")) if anchor else (None, None)
    if all(isinstance(x, int) and not isinstance(x, bool) for x in preferred):
        start, end = preferred
        if not 0 <= start < end <= len(p.text):
            raise EditError("anchor")
        selected = p.text[start:end]
        if selected != edit["from"] and (not _fold(selected)[0] or _fold(selected)[0] != _fold(edit["from"])[0]):
            raise EditError("notFound")
        # 页面会折叠选中文字的空白；先在明确选区里核对，不能改到段内另一处恰好精确匹配的文字。
        matches = [preferred]
    else:
        matches = _matches(p.text, edit["from"])
    if len(matches) != 1:
        raise EditError("ambiguous" if matches else "notFound")
    start, end = matches[0]
    # 非 w:t 字符（制表符、符号等）和换行结构不在本次字节修改能力内。
    covered = sum(max(0, min(end, t.offset + len(t.text)) - max(start, t.offset)) for t in p.elements)
    if covered != end - start or any(start < b < end for b in p.barriers):
        raise EditError("range")
    return p, start, end


def edit_document(raw: bytes, edits: list[dict], restrictions: dict[int, set[str]]) -> tuple[bytes, dict[int, str]]:
    paras = parse_document(raw)
    grouped: dict[int, list[tuple[int, int, str, str]]] = {}
    errors = []
    for edit in edits:
        try:
            if not isinstance(edit.get("from"), str) or not isinstance(edit.get("to"), str):
                raise EditError("arguments")
            _xml_text(edit["to"])
            p, start, end = _locate(paras, edit, restrictions)
            previous = grouped.setdefault(p.n, [])
            if any(start < b and a < end for a, b, _text, _id in previous):
                raise EditError("overlap")
            previous.append((start, end, edit["to"], edit.get("id", "")))
        except EditError as exc:
            exc.item = edit.get("id", "")
            errors.append(exc)
    if errors:
        raise SelectionError(errors)
    changes, expected = [], {}
    for n, replacements in grouped.items():
        p = paras[n]
        target = p.text
        for start, end, value, _id in sorted(replacements, reverse=True):
            target = target[:start] + value + target[end:]
        expected[n] = target
        for node in p.elements:
            value = node.text
            for start, end, replacement, _id in sorted(replacements, reverse=True):
                lo, hi = max(0, start - node.offset), min(len(node.text), end - node.offset)
                if lo < hi:
                    insert = replacement if node.offset <= start < node.offset + len(node.text) else ""
                    value = value[:lo] + insert + value[hi:]
            if value != node.text:
                changes.append((node.start, node.stop, _element_bytes(raw, node, value)))
    for start, stop, value in sorted(changes, reverse=True):
        raw = raw[:start] + value + raw[stop:]
    return raw, expected


def _read_part(zf: zipfile.ZipFile, name: str) -> bytes:
    if zf.getinfo(name).file_size > dr.MAX_PART:
        raise EditError("size", name)
    with zf.open(name) as fh:
        data = fh.read(dr.MAX_PART + 1)
    if len(data) > dr.MAX_PART:
        raise EditError("size", name)
    return data


def _part_name(base: str, target: str) -> str:
    name = posixpath.normpath(posixpath.join(base, target.lstrip("/"))) if not target.startswith("/") else target[1:]
    if name.startswith("../") or "\\" in name or ":" in name:
        raise EditError("package", name)
    return name


def _package_parts(zf: zipfile.ZipFile) -> tuple[str, str, str]:
    main = "word/document.xml"
    if "_rels/.rels" in zf.namelist():
        for rel in dr.parse_xml(_read_part(zf, "_rels/.rels"), "_rels/.rels"):
            if rel.get("Type", "").endswith("/officeDocument"):
                if rel.get("TargetMode") == "External":
                    raise EditError("package")
                main = _part_name("", rel.get("Target", ""))
    base, name = posixpath.split(main)
    settings, styles = posixpath.join(base, "settings.xml"), posixpath.join(base, "styles.xml")
    rels = posixpath.join(base, "_rels", name + ".rels")
    if rels in zf.namelist():
        for rel in dr.parse_xml(_read_part(zf, rels), rels):
            kind = rel.get("Type", "").rsplit("/", 1)[-1]
            if kind in ("settings", "styles"):
                if rel.get("TargetMode") == "External":
                    raise EditError("package")
                part = _part_name(base, rel.get("Target", ""))
                if kind == "settings":
                    settings = part
                else:
                    styles = part
    return main, settings, styles


def _gates(zf: zipfile.ZipFile, allow_untracked: bool) -> tuple[str, bytes, dict[int, set[str]], bool]:
    infos = zf.infolist()
    names = zf.namelist()
    if len(set(names)) != len(names) or any(i.flag_bits & 1 for i in infos):
        raise EditError("package")
    if sum(i.file_size for i in infos) > MAX_EXPANDED:
        raise EditError("size")
    if any(n.lower().startswith("_xmlsignatures/") for n in names):
        raise EditError("signed")
    for name in names:
        if name == "[Content_Types].xml" or name.endswith(".rels"):
            root = dr.parse_xml(_read_part(zf, name), name)
            if any("digital-signature" in str(v).lower() or "digitalsignature" in str(v).lower()
                   for el in root.iter() for v in el.attrib.values()):
                raise EditError("signed")
    main, settings, styles = _package_parts(zf)
    raw = _read_part(zf, main)
    parse_document(raw)   # 明确拒绝 UTF-16、DTD 和坏 XML，再交给只读结构检查
    root = dr.parse_xml(raw, main)
    if root.tag != dr.W + "document" or root.find(dr.W + "body") is None:
        raise EditError("package")
    tracked = False
    if settings in names:
        config = dr.parse_xml(_read_part(zf, settings), settings)
        protection = config.find(dr.W + "documentProtection")
        if protection is not None and protection.get(dr.W + "enforcement", "1").lower() not in ("0", "false", "off"):
            raise EditError("protected")
        tracking = config.find(dr.W + "trackRevisions")
        tracked = tracking is not None and tracking.get(dr.W + "val", "1").lower() not in ("0", "false", "off")
        if tracked and not allow_untracked:
            raise EditError("tracked")
    style_root = dr.parse_xml(_read_part(zf, styles), styles) if styles in names else None
    return main, raw, dr.writeback_restrictions(root, style_root), tracked


def _directory(raw: bytes, infos: list) -> tuple[int, int, list[bytes]]:
    # ZIP32 的目录记录只改相对偏移；ZIP64 / 分卷没有可靠的同字节重封装支持，明确拒绝。
    end = len(raw)
    eocd = raw.rfind(b"PK\x05\x06", max(0, end - 65557))
    while eocd >= 0 and (eocd + 22 > end or eocd + 22 + int.from_bytes(raw[eocd + 20:eocd + 22], "little") != end):
        eocd = raw.rfind(b"PK\x05\x06", max(0, end - 65557), eocd)
    if eocd < 0:
        raise EditError("package")
    disk, cd_disk, on_disk, count, size, start = struct.unpack_from("<4H2I", raw, eocd + 4)
    if disk or cd_disk or count != on_disk or count == 65535 or start + size != eocd or count != len(infos):
        raise EditError("package")
    records, at = [], start
    for info in infos:
        if raw[at:at + 4] != b"PK\x01\x02" or at + 46 > eocd:
            raise EditError("package")
        nlen, xlen, clen = struct.unpack_from("<3H", raw, at + 28)
        stop = at + 46 + nlen + xlen + clen
        if stop > eocd or struct.unpack_from("<I", raw, at + 42)[0] != info.header_offset:
            raise EditError("package")
        if max(info.file_size, info.compress_size, info.header_offset) >= 0xFFFFFFFF:
            raise EditError("package")
        records.append(raw[at:stop])
        at = stop
    if at != eocd:
        raise EditError("package")
    return start, eocd, records


def repack(raw: bytes, main: str, document: bytes) -> bytes:
    """其余 local header、压缩流、data descriptor 原样复制；中央目录仅调整必需的偏移字段。"""
    with zipfile.ZipFile(io.BytesIO(raw)) as source:
        infos = source.infolist()
        cd_start, eocd, records = _directory(raw, infos)
        target = copy.copy(source.getinfo(main))
        if target.compress_type not in (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED):
            raise EditError("package")
        stream = io.BytesIO()
        with zipfile.ZipFile(stream, "w") as replacement:
            replacement.writestr(target, document)
        new_raw = stream.getvalue()
        with zipfile.ZipFile(io.BytesIO(new_raw)) as replacement:
            new_start, _end, new_records = _directory(new_raw, replacement.infolist())
        ordered = sorted(infos, key=lambda i: i.header_offset)
        if not ordered or ordered[0].header_offset != 0 or len({i.header_offset for i in infos}) != len(infos):
            raise EditError("package")
        chunks, offsets, length = [], {}, 0
        for i, info in enumerate(ordered):
            stop = ordered[i + 1].header_offset if i + 1 < len(ordered) else cd_start
            if info.header_offset >= stop or raw[info.header_offset:info.header_offset + 4] != b"PK\x03\x04":
                raise EditError("package")
            offsets[info.filename] = length
            chunk = new_raw[:new_start] if info.filename == main else raw[info.header_offset:stop]
            chunks.append(chunk)
            length += len(chunk)
        new_cd = []
        for info, record in zip(infos, records):
            entry = bytearray(new_records[0] if info.filename == main else record)
            struct.pack_into("<I", entry, 42, offsets[info.filename])
            new_cd.append(bytes(entry))
        directory = b"".join(new_cd)
        tail = bytearray(raw[eocd:])
        struct.pack_into("<II", tail, 12, len(directory), length)
        return b"".join(chunks) + directory + tail


def _validate(path: Path | io.BytesIO, original: bytes, main: str, document: bytes, expected: dict[int, str]) -> None:
    with zipfile.ZipFile(io.BytesIO(original)) as old, zipfile.ZipFile(path) as new:
        if new.namelist() != old.namelist() or new.comment != old.comment:
            raise EditError("validation")
        for info in new.infolist():
            prior = old.getinfo(info.filename)
            if info.filename != main and (info.CRC, info.file_size, info.compress_size) != (prior.CRC, prior.file_size, prior.compress_size):
                raise EditError("validation", info.filename)
            # 重开后实际解压、检查 CRC，不只比较目录里声称的值。
            with new.open(info) as fh:
                while fh.read(1 << 20):
                    pass
        actual = _read_part(new, main)
        if actual != document:
            raise EditError("validation", main)
        paras = parse_document(actual)
        old_paras = parse_document(_read_part(old, main))
        if len(paras) != len(old_paras) or any(p.text != expected.get(p.n, old_paras[p.n].text) for p in paras):
            raise EditError("validation", main)


def _source_check(source: Path, fingerprint: str) -> bytes:
    if source.is_symlink() or not source.is_file():
        raise EditError("source")
    # Word 的 owner file 按平台可能截去原文件名开头的两个字符，两种都检查。
    for name in ("~$" + source.name, "~$" + source.name[2:]):
        if source.with_name(name).exists():
            raise EditError("locked")
    if source.stat().st_size > MAX_PACKAGE:
        raise EditError("size")
    raw = source.read_bytes()
    if not fingerprint or hashlib.sha256(raw).hexdigest() != fingerprint:
        raise EditError("fingerprint")
    return raw


def _backup(review: Path, batch: int, raw: bytes) -> Path:
    directory = review
    for name in (".snapshots", f"batch-{batch}", "docx-writeback"):
        directory = directory / name
        if directory.is_symlink():
            raise EditError("source", str(directory))
        directory.mkdir(exist_ok=True, mode=0o700)
        os.chmod(directory, 0o700)
    fd, name = tempfile.mkstemp(prefix="original-", suffix=".docx", dir=directory)
    with os.fdopen(fd, "wb") as fh:
        fh.write(raw)
        fh.flush()
        os.fsync(fh.fileno())
    return Path(name)


def _selection(review: Path, batch: int | None, ids: list[str] | None) -> list[dict]:
    doc = store.load(review)
    if batch is not None:
        item = next((i for i in store.handoff_load(review)["items"] if i.get("seq") == batch), None)
        if item is None:
            raise EditError("batch", str(batch))
        ids = item.get("openIds", [])
    selected, errors = [], []
    by_id = {n["id"]: n for n in doc["notes"]}
    for nid in dict.fromkeys(ids or []):
        note = by_id.get(nid)
        if note is None:
            errors.append(EditError("note", item=nid))
            continue
        if batch is not None and (note.get("status") == "resolved" or not note.get("edit")):
            continue
        ed, a = note.get("edit") or {}, note.get("docx")
        if (note.get("status", "open") != "open" or note.get("orphan") or note.get("hold")
                or ed.get("op") not in ("delete", "replace") or note.get("quoteFrom", "报告") != "报告"
                or note.get("target", "报告") not in ("报告", "报告和讲稿")
                or not isinstance(ed.get("from"), str) or not ed.get("from")):
            errors.append(EditError("note", item=nid))
        elif not dr.usable_anchor(a):
            errors.append(EditError("anchor", item=nid))
        else:
            selected.append({"id": nid, "anchor": a, "from": ed["from"],
                             "to": "" if ed["op"] == "delete" else ed.get("to", "")})
    if errors:
        raise SelectionError(errors)
    return selected


def writeback(review: Path, *, batch: int | None = None, ids: list[str] | None = None,
              replacement: dict | None = None, output: Path | None = None,
              dry_run: bool = False, allow_untracked: bool = False) -> dict:
    """公开入口：验证整组修改并写入（或预演），返回文件、备份、逐条修改和警告；不改批注。"""
    review = Path(review).resolve()
    manifest = ledger.read_manifest(review)
    if (not manifest or ledger.unsupported_format(manifest)
            or (manifest.get("format") or {}).get("report") != "docx"):
        raise EditError("review")
    try:
        source = resolve_sources(review, manifest)["report"]
    except BuildError:
        raise EditError("source") from None
    if source is None:
        raise EditError("source")
    fingerprint = (manifest.get("docx") or {}).get("fingerprint", "")
    with store.locked(review):
        if replacement is not None and batch is not None and not any(i.get("seq") == batch for i in store.handoff_load(review)["items"]):
            raise EditError("batch", str(batch))
        edits = [replacement] if replacement is not None else _selection(review, batch, ids)
        if not edits:
            raise EditError("empty")
        raw = _source_check(source, fingerprint)
        stale = [EditError("anchor", item=e.get("id", "")) for e in edits
                 if e.get("anchor") and e["anchor"].get("fp") != fingerprint]
        if stale:
            raise SelectionError(stale)
        with zipfile.ZipFile(io.BytesIO(raw)) as zf:
            main, xml, restrictions, tracked = _gates(zf, allow_untracked)
        document, expected = edit_document(xml, edits, restrictions)
        packed = repack(raw, main, document)
        destination = Path(output).absolute() if output else source
        separate = destination != source
        if destination.is_symlink() or (separate and destination.exists()):
            raise EditError("output", str(destination))
        result = {"source": str(source), "output": str(destination), "dryRun": dry_run,
                  "backup": None, "ids": [e.get("id", "") for e in edits],
                  "paragraphs": sorted(expected), "warnings": ["untracked"] if tracked else []}
        if dry_run:
            _validate(io.BytesIO(packed), raw, main, document, expected)
            return result
        # 先留下可靠原件；后续写入、fsync、校验或 replace 失败都不删除备份。
        result["backup"] = str(_backup(review, batch or 0, raw))
        fd, temp_name = tempfile.mkstemp(prefix="." + destination.name + "-", suffix=".tmp", dir=destination.parent)
        temp = Path(temp_name)
        try:
            with os.fdopen(fd, "wb") as fh:
                if not separate:
                    os.fchmod(fh.fileno(), stat.S_IMODE(source.stat().st_mode))
                fh.write(packed)
                fh.flush()
                os.fsync(fh.fileno())
            _validate(temp, raw, main, document, expected)
            _source_check(source, fingerprint)   # Word 或别的进程在准备期间动过源文件就拒绝提交
            if separate and destination.exists():
                raise EditError("output", str(destination))
            os.replace(temp, destination)
        finally:
            temp.unlink(missing_ok=True)
        return result


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    try:
        language = i18n.cli_locale(i18n.explicit_locale(argv))
    except i18n.LocaleError as exc:
        print(i18n.locale_error_message(exc, ERROR_KEYS, i18n.Translator(i18n.DEFAULT_LOCALE)), file=sys.stderr)
        return 2
    pre = i18n.LocalizedParser(add_help=False, keys=ERROR_KEYS, translate=i18n.Translator(language))
    pre.add_argument("command", nargs="?")
    pre.add_argument("review", nargs="?")
    pre.add_argument("--locale")
    known, _ = pre.parse_known_args(argv)
    manifest = ledger.read_manifest(Path(known.review)) if known.review else {}
    try:
        language = i18n.cli_locale(known.locale, stored=manifest.get("locale"))
    except i18n.LocaleError as exc:
        print(i18n.locale_error_message(exc, ERROR_KEYS, i18n.Translator(i18n.DEFAULT_LOCALE)), file=sys.stderr)
        return 2
    t = i18n.Translator(language)
    ap = i18n.LocalizedParser(prog="docx_edit.py", description=t("docx.edit.help"), keys=ERROR_KEYS, translate=t)
    ap.add_argument("command", choices=("apply", "replace"))
    ap.add_argument("review", type=Path)
    ap.add_argument("--batch", type=int, help=t("docx.edit.batchHelp"))
    ap.add_argument("--id", action="append", dest="ids", help=t("docx.edit.idHelp"))
    ap.add_argument("--paragraph", type=int, help=t("docx.edit.paragraphHelp"))
    ap.add_argument("--para-id")
    ap.add_argument("--from", dest="before")
    ap.add_argument("--to", dest="after")
    ap.add_argument("--output", type=Path, help=t("docx.edit.outputHelp"))
    ap.add_argument("--dry-run", action="store_true", help=t("docx.edit.dryHelp"))
    ap.add_argument("--allow-untracked", action="store_true", help=t("docx.edit.untrackedHelp"))
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--locale")
    a = ap.parse_args(argv)
    try:
        replacement = None
        if a.batch is not None and a.batch < 1:
            raise EditError("arguments")
        if a.command == "apply":
            if bool(a.ids) == (a.batch is not None) or any(x is not None for x in (a.paragraph, a.para_id, a.before, a.after)):
                raise EditError("arguments")
        else:
            if a.ids or a.paragraph is None or a.paragraph < 0 or not a.before or a.after is None:
                raise EditError("arguments")
            replacement = {"paragraph": a.paragraph, "para_id": a.para_id, "from": a.before, "to": a.after}
        result = writeback(a.review, batch=a.batch, ids=a.ids, replacement=replacement, output=a.output,
                           dry_run=a.dry_run, allow_untracked=a.allow_untracked)
        for warning in result["warnings"]:
            print(t("docx.edit." + warning), file=sys.stderr)
        print(json.dumps(result, ensure_ascii=False) if a.json else t(
            "docx.edit.preview" if a.dry_run else "docx.edit.done", path=result["output"],
            paragraphs=", ".join(str(n) for n in result["paragraphs"])))
        if result["backup"] and not a.json:
            print(t("docx.edit.backup", path=result["backup"]))
        return 0
    except (EditError, dr.DocxError, BuildError, OSError, ValueError, KeyError, zipfile.BadZipFile, zlib.error) as exc:
        errors = exc.errors if isinstance(exc, SelectionError) else [exc]
        for error in errors:
            code = error.code if isinstance(error, EditError) else "io"
            detail = error.detail if isinstance(error, EditError) else str(error)
            if code == "readonly":
                detail = (", " if language == "en" else "、").join(t("docx.kind." + k) for k in detail.split(","))
            message = t("docx.edit.error." + code, detail=detail)
            print((f"{error.item}: " if isinstance(error, EditError) and error.item else "") + message, file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
