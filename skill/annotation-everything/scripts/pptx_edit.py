#!/usr/bin/env python3
"""按审阅批注回写 PPTX：只改目标幻灯片/备注部件里的 a:t，保留其他部件字节与 ZIP 成员的压缩数据。

apply REVIEW --batch N / --id ID：处理报告侧普通文本形状与讲稿侧已有备注的快捷替换、删除。
报告侧按批注的幻灯片定位（元素身份 + 逻辑段落 + 码点偏移）落到具体 a:t，跨 run 选区保留
未触及文本及其属性；讲稿侧只在未使用外部讲稿时回写已有备注部件，按引用原文在备注正文
唯一命中定位。所有选择先整体校验；任何一条失败都不写。不会处理批注状态。
"""
from __future__ import annotations

import hashlib
import io
import json
import os
import stat
import sys
import tempfile
import zipfile
import zlib
from dataclasses import dataclass
from pathlib import Path
from xml.parsers import expat

import docx_edit as de   # 与 DOCX 回写共用同一套字节级 ZIP 重封装、转义与空白折叠匹配
import i18n
import ledger
import notes_store as store
import pptx_reader as pr
from build_viewer import BuildError, resolve_sources
from docx_edit import EditError, SelectionError   # 同一错误契约：code / detail / item / errors

ERROR_KEYS = {
    i18n.CLI_ERROR_INVALID_CHOICE: "pptx.edit.cli.invalidChoice",
    i18n.CLI_ERROR_MISSING_VALUE: "pptx.edit.cli.missingValue",
    i18n.CLI_ERROR_UNKNOWN_ARGUMENT: "pptx.edit.cli.unknownArgument",
    i18n.CLI_ERROR_MISSING_ARGUMENT: "pptx.edit.cli.missingArgument",
    i18n.CLI_ERROR_GENERIC: "pptx.edit.cli.arguments",
    i18n.CLI_ERROR_EMPTY_LOCALE: "pptx.edit.cli.emptyLocale",
    i18n.CLI_ERROR_INVALID_LOCALE: "pptx.edit.cli.invalidLocale",
}

A_ = pr.A + "}"   # expat 命名空间分隔后的名字形如「uri}local」（docx_edit.W 同一口径）
P_ = pr.P + "}"
AT = A_ + "t"
AR = A_ + "r"
AFLD = A_ + "fld"
ABR = A_ + "br"
AP = A_ + "p"
PSP = P_ + "sp"
PSP_TREE = P_ + "spTree"
PTXBODY = P_ + "txBody"
PNVSP = P_ + "nvSpPr"
PNVPR = P_ + "nvPr"
PPH = P_ + "ph"
PCNVPR = P_ + "cNvPr"
NOTES_EXCLUDED_PH = ("sldImg", "sldNum", "hdr", "ftr", "dt")


class Piece:
    """段落里的一个 a:t：字节位置与段内码点偏移，供原样重封与偏移换算。"""

    __slots__ = ("start", "opening_end", "stop", "offset", "qname", "empty", "text")

    def __init__(self, start: int, opening_end: int, offset: int, qname: bytes, empty: bool):
        self.start, self.opening_end, self.stop = start, opening_end, opening_end
        self.offset, self.qname, self.empty, self.text = offset, qname, empty, ""


class Para:
    __slots__ = ("n", "text", "pieces", "barriers")

    def __init__(self, n: int):
        self.n, self.text, self.pieces, self.barriers = n, "", [], []


class Shape:
    __slots__ = ("sid", "order", "ph_present", "ph_type", "txbody_done", "paras")

    def __init__(self, order: int):
        self.sid, self.order = "", order
        self.ph_present, self.ph_type = False, None   # ph 的 type 属性：缺失是 None，与 reader 口径一致
        self.txbody_done = False                       # 只认第一个 txBody，与读取层 find 口径一致
        self.paras: list[Para] = []


def _local(name: str) -> str:
    return name.rsplit("}", 1)[-1]


class _PartParser:
    """按读取层同一套文本模型解析部件：a:p 直属 a:r/a:fld 的第一个 a:t 是 run 文本，a:br 占一个换行偏移。

    slide 模式只收 spTree 顶层 p:sp（与能力矩阵的普通文本形状一致）；notes 模式收全部 .//p:sp
    并按 reader 的占位符规则在容器外归页。名称与文本一律以 UTF-8 字节为准。
    """

    def __init__(self, raw: bytes, part: str, mode: str):
        self.raw, self.part, self.mode = raw, part, mode
        self.shapes: list[Shape] = []
        self.stack: list[str] = []
        self.active: Piece | None = None
        self.active_para: Para | None = None
        self.shape_stack: list[tuple[Shape, int]] = []
        self.sp_tree_depth: int | None = None
        self.nv_depth: int | None = None
        self.txbody_depth: int | None = None
        self.run_depth: int | None = None
        self.run_offset = 0
        self.run_piece_done = False

    # -- 部件闸门 -------------------------------------------------------
    def declaration(self, _version, encoding, _standalone):
        if encoding and encoding.lower().replace("-", "") != "utf8":
            raise EditError("encoding")

    def doctype(self, *_args):
        raise EditError("xml", "DTD")

    # -- 结构跟踪 -------------------------------------------------------
    def _current_shape(self):
        return self.shape_stack[-1][0] if self.shape_stack else None

    def _top_level_shape(self, depth: int) -> bool:
        return (self.mode == "slide" and self.sp_tree_depth is not None
                and depth == self.sp_tree_depth + 1)

    def start(self, name, attrs):
        if self.active is not None:
            raise EditError("xml", "nested element inside text")
        self.stack.append(name)
        depth = len(self.stack)
        parent = self.stack[-2] if depth >= 2 else ""
        if name == PSP_TREE:
            self.sp_tree_depth = depth
        if name == PSP:
            if self.mode == "notes" or self._top_level_shape(depth):
                shape = Shape(len(self.shapes))
                self.shapes.append(shape)
                self.shape_stack.append((shape, depth))
                self.nv_depth = self.txbody_depth = self.run_depth = None
                self.run_piece_done = False
        elif name == PCNVPR and self.nv_depth is not None and depth == self.nv_depth + 1:
            shape = self._current_shape()
            if shape is not None and not shape.sid:
                shape.sid = attrs.get("id", "")
        elif name == PPH and depth >= 3 and parent == PNVPR and self.stack[-3] == PNVSP:
            shape = self._current_shape()
            if shape is not None and not shape.ph_present:
                shape.ph_present, shape.ph_type = True, attrs.get("type")
        if self.shape_stack and depth == self.shape_stack[-1][1] + 1:
            shape = self.shape_stack[-1][0]
            if name == PTXBODY:
                if not shape.txbody_done:
                    shape.txbody_done = True
                    self.txbody_depth = depth
            elif _local(name).startswith("nv") and parent == PSP:
                self.nv_depth = depth
        if (self.txbody_depth is not None and self.txbody_depth + 1 == depth
                and name == AP):
            shape = self._current_shape()
            if shape is not None:
                shape.paras.append(Para(len(shape.paras)))
                self.run_depth = None
        elif name in (AR, AFLD) and self.txbody_depth is not None and depth == self.txbody_depth + 2 \
                and parent == AP:
            shape = self._current_shape()
            para = shape.paras[-1] if shape and shape.paras else None
            if para is not None:
                self.run_depth = depth
                self.run_offset = len(para.text)
                self.run_piece_done = False
        elif name == ABR and self.txbody_depth is not None and depth == self.txbody_depth + 2 \
                and parent == AP:
            shape = self._current_shape()
            para = shape.paras[-1] if shape and shape.paras else None
            if para is not None:
                para.barriers.append(len(para.text))
                para.text += "\n"
        if name == AT and self.run_depth is not None and depth == self.run_depth + 1 and not self.run_piece_done:
            para = self._current_shape().paras[-1] if self._current_shape() and self._current_shape().paras else None
            if para is not None:
                at = self.parser.CurrentByteIndex
                end = de._tag_end(self.raw, at)
                tag = self.raw[at:end]
                piece = Piece(at, end, self.run_offset,
                              _local_tag(tag), tag.rstrip().endswith(b"/>"))
                para.pieces.append(piece)
                self.active, self.active_para = piece, para
                self.run_piece_done = True

    def end(self, name):
        depth = len(self.stack)
        if name == AT and self.active is not None:
            self.active.stop = de._tag_end(self.raw, self.parser.CurrentByteIndex)
            # 段落文本与读取层同一口径：run 文本 = 其第一个 a:t 的全部字符数据
            self.active_para.text += self.active.text
            self.active, self.active_para = None, None
        if name == AP and self.txbody_depth is not None and depth == self.txbody_depth + 2:
            self.run_depth = None
        if name in (AR, AFLD) and depth == self.run_depth:
            self.run_depth = None
        if name == PTXBODY and depth == self.txbody_depth:
            self.txbody_depth = None
        if name == PSP and self.shape_stack and depth == self.shape_stack[-1][1]:
            self.shape_stack.pop()
            self.nv_depth = self.txbody_depth = self.run_depth = None
            self.run_piece_done = False
        self.stack.pop()

    def text(self, value):
        if self.active is not None:
            self.active.text += value

    def parse(self) -> list[Shape]:
        if len(self.raw) > pr.MAX_XML_BYTES:
            raise EditError("size", self.part)
        if self.raw.startswith((b"\xff\xfe", b"\xfe\xff")) or b"\0" in self.raw[:100]:
            raise EditError("encoding")
        parser = expat.ParserCreate(namespace_separator="}")
        self.parser = parser
        parser.XmlDeclHandler = self.declaration
        parser.StartDoctypeDeclHandler = self.doctype
        parser.StartElementHandler, parser.EndElementHandler = self.start, self.end
        parser.CharacterDataHandler = self.text
        try:
            parser.Parse(self.raw, True)
        except expat.ExpatError as exc:
            raise EditError("xml", str(exc)) from None
        return self.shapes


def _local_tag(tag: bytes) -> bytes:
    head = tag[1:]
    stop = head.find(b">")
    if stop >= 0:
        head = head[:stop]
    name = head.split(b" ", 1)[0].rstrip(b"/")
    return name


def _parse_part(raw: bytes, part: str, mode: str) -> list[Shape]:
    try:
        return _PartParser(raw, part, mode).parse()
    except EditError as exc:
        raise EditError(exc.code, exc.detail or part, item=exc.item) from None


def _notes_paragraphs(shapes: list[Shape]) -> list[Para]:
    """读取层 _notes 的同一套占位符规则：先收 body 占位符，没有再排除页码等辅助形状。"""
    body = [s for s in shapes if s.ph_present and (s.ph_type if s.ph_type is not None else "body") == "body"]
    if not body:
        body = [s for s in shapes
                if not s.ph_present or (s.ph_type or "") not in NOTES_EXCLUDED_PH]
    return [p for s in body for p in s.paras]


def _read_model(raw: bytes) -> dict:
    """在已核对字节的临时副本上跑读取层：包结构闸门 + 幻灯片/元素/备注模型。"""
    with tempfile.TemporaryDirectory(prefix="ae-pptx-edit-") as td:
        spool = Path(td) / "source.pptx"
        spool.write_bytes(raw)
        try:
            return pr.read(spool)
        except pr.PptxError as exc:
            raise _package_error(exc) from None


def _package_error(exc: pr.PptxError) -> EditError:
    code = {"badXml": "xml", "dtd": "xml", "tooLarge": "size", "unreadable": "source"}.get(exc.code, "package")
    detail = exc.detail or exc.part
    if exc.code == "dtd":
        detail = "DTD"
    return EditError(code, detail)


def _member_bytes(zf: zipfile.ZipFile, part: str) -> bytes:
    try:
        info = zf.getinfo(part)
    except KeyError:
        raise EditError("package", part) from None
    if info.file_size > pr.MAX_XML_BYTES:
        raise EditError("size", part)
    return zf.read(part)


def _part_bytes(raw: bytes, part: str) -> bytes:
    with zipfile.ZipFile(io.BytesIO(raw)) as zf:
        return _member_bytes(zf, part)


@dataclass
class Task:
    id: str
    kind: str            # shape / notes
    part: str
    mode: str
    container: int       # slide 模式是部件内顶层形状序号；notes 固定 0
    para: int
    start: int
    end: int
    to: str
    slide: str = ""
    page: int = 0
    element: str = ""


def _locate_span(p: Para, frm: str, offsets: tuple[int, int] | None) -> tuple[int, int]:
    if offsets is not None:
        start, end = offsets
        if (isinstance(start, bool) or isinstance(end, bool)
                or not isinstance(start, int) or not isinstance(end, int)
                or not 0 <= start < end <= len(p.text)):
            raise EditError("anchor")
        selected = p.text[start:end]
        if selected != frm and (not de._fold(selected)[0] or de._fold(selected)[0] != de._fold(frm)[0]):
            raise EditError("notFound")
        return start, end
    matches = de._matches(p.text, frm)
    if len(matches) != 1:
        raise EditError("ambiguous" if matches else "notFound")
    return matches[0]


def _check_span(p: Para, start: int, end: int) -> None:
    # a:br 占一个偏移但不是 a:t 文本：跨过它的范围不能落到字节上。
    if any(start < b < end for b in p.barriers):
        raise EditError("range")
    covered = sum(max(0, min(end, pc.offset + len(pc.text)) - max(start, pc.offset)) for pc in p.pieces)
    if covered != end - start:
        raise EditError("range")


def _shape_by_id(shapes: list[Shape], sid: str) -> tuple[Shape, int]:
    hits = [(s, i) for i, s in enumerate(shapes) if s.sid == sid]
    if len(hits) != 1:
        raise EditError("anchor", sid)
    return hits[0]


def edit_parts(raw_parts: dict[str, bytes], edits: list[dict], model: dict) -> tuple[dict[str, bytes], list[Task], dict]:
    """定位并落实整组修改：先逐条定位（任何一条失败都不写），再按段落组应用并重封部件字节。"""
    slides_by_key = {s["key"]: s for s in model["slides"]}
    parsed: dict[str, tuple[str, list[Shape]]] = {}

    def shapes_for(part: str, mode: str) -> list[Shape]:
        if part not in parsed:
            parsed[part] = (mode, _parse_part(raw_parts[part], part, mode))
        return parsed[part][1]

    tasks: list[Task] = []
    for e in edits:
        if e["side"] == "shape":
            a = e["anchor"]
            if a.get("changed"):
                raise EditError("anchor", item=e["id"])
            got = pr.relocate(a, model["slides"])
            if got["status"] != "ok":
                code = "ambiguous" if got["status"] == "ambiguous" else "anchor"
                raise EditError(code, got.get("why") or "", item=e["id"])
            slide = got["slide"]
            target = (got.get("targets") or [None])[0]
            el = next((x for x in slide["elements"] if target and x["key"] == target.get("key")), None)
            if el is None:
                raise EditError("anchor", item=e["id"])
            if not el["capabilities"]["textWrite"]:
                raise EditError("readonly", el["kind"], item=e["id"])
            text = a.get("text") or {}
            shapes = shapes_for(slide["part"], "slide")
            shape, container = _shape_by_id(shapes, str(el["id"]))
            para_n = int(got["text"]["para"]) if got.get("text") else 0
            if not 0 <= para_n < len(shape.paras):
                raise EditError("anchor", item=e["id"])
            p = shape.paras[para_n]
            if hashlib.sha256(p.text.encode("utf-8")).hexdigest() != text.get("paraDigest"):
                raise EditError("anchor", item=e["id"])
            offsets = None
            if all(isinstance(text.get(k), int) and not isinstance(text.get(k), bool)
                   for k in ("start", "end")):
                offsets = (text["start"], text["end"])
            start, end = _locate_span(p, e["from"], offsets)
            _check_span(p, start, end)
            tasks.append(Task(e["id"], "shape", slide["part"], "slide", container, para_n,
                              start, end, e["to"], slide["key"], slide["n"], str(el["id"])))
        else:
            slide = slides_by_key.get(e.get("slide_key") or "")
            if slide is None:
                raise EditError("anchor", item=e["id"])
            notes = slide.get("notes")
            if not isinstance(notes, dict) or notes.get("status") != "available":
                raise EditError("notes", item=e["id"])
            shapes = shapes_for(notes["part"], "notes")
            paras = _notes_paragraphs(shapes)
            found = [(i, m) for i, p in enumerate(paras) for m in de._matches(p.text, e["from"])]
            if len(found) != 1:
                raise EditError("ambiguous" if found else "notFound", item=e["id"])
            para_n, (start, end) = found[0]
            _check_span(paras[para_n], start, end)
            tasks.append(Task(e["id"], "notes", notes["part"], "notes", 0, para_n,
                              start, end, e["to"], slide["key"], slide["n"], ""))
    grouped: dict[tuple[str, int, int], list[Task]] = {}
    for t in tasks:
        group = grouped.setdefault((t.part, t.container, t.para), [])
        if any(t.start < b and a < t.end for a, b, _to, _id in
               ((x.start, x.end, x.to, x.id) for x in group)):
            raise EditError("overlap", item=t.id)
        group.append(t)
    changed: dict[str, bytes] = {}
    expected: dict[str, tuple[str, dict[int, dict[int, str]]]] = {}
    for part, (mode, shapes) in parsed.items():
        raw = raw_parts[part]
        part_changes: list[tuple[int, int, bytes]] = []
        exp: dict[int, dict[int, str]] = {}
        for (ppart, container, _para_n), items in grouped.items():
            if ppart != part:
                continue
            if mode == "slide":
                paras = shapes[container].paras
            else:
                paras = _notes_paragraphs(shapes)
            p = paras[_para_n]
            target = p.text
            for t in sorted(items, key=lambda x: x.start, reverse=True):
                target = target[:t.start] + t.to + target[t.end:]
            exp.setdefault(container, {})[_para_n] = target
            for piece in p.pieces:
                value = piece.text
                for t in sorted(items, key=lambda x: x.start, reverse=True):
                    lo, hi = max(0, t.start - piece.offset), min(len(piece.text), t.end - piece.offset)
                    if lo < hi:
                        insert = t.to if piece.offset <= t.start < piece.offset + len(piece.text) else ""
                        value = value[:lo] + insert + value[hi:]
                if value != piece.text:
                    part_changes.append((piece.start, piece.stop, de._element_bytes(raw, piece, value)))
        for start, stop, value in sorted(part_changes, reverse=True):
            raw = raw[:start] + value + raw[stop:]
        changed[part] = raw
        expected[part] = (mode, exp)
    return changed, tasks, expected


def _validate_part(old_raw: bytes, new_raw: bytes, part: str, mode: str,
                   expected: dict[int, dict[int, str]]) -> None:
    old_shapes = _parse_part(old_raw, part, mode)
    new_shapes = _parse_part(new_raw, part, mode)
    if len(old_shapes) != len(new_shapes) or any(o.sid != n.sid for o, n in zip(old_shapes, new_shapes)):
        raise EditError("validation", part)
    pairs = ([(o.paras, n.paras, i) for i, (o, n) in enumerate(zip(old_shapes, new_shapes))] if mode == "slide"
             else [(_notes_paragraphs(old_shapes), _notes_paragraphs(new_shapes), 0)])
    for old_paras, new_paras, i in pairs:
        if len(old_paras) != len(new_paras):
            raise EditError("validation", part)
        for j, (o, n) in enumerate(zip(old_paras, new_paras)):
            if n.text != expected.get(i, {}).get(j, o.text):
                raise EditError("validation", part)


def _validate(path: Path | io.BytesIO, original: bytes, changed: dict[str, bytes],
              expected: dict[str, tuple[str, dict[int, dict[int, str]]]]) -> None:
    """重开结果逐成员核对：未触及成员 CRC 与原值一致，改动成员字节与预期一致，再按文本模型复核。"""
    with zipfile.ZipFile(io.BytesIO(original)) as old, zipfile.ZipFile(path) as new:
        if new.namelist() != old.namelist() or new.comment != old.comment:
            raise EditError("validation")
        for info in new.infolist():
            prior = old.getinfo(info.filename)
            if info.filename in changed:
                if new.read(info) != changed[info.filename]:
                    raise EditError("validation", info.filename)
            elif (info.CRC, info.file_size, info.compress_size) != (prior.CRC, prior.file_size, prior.compress_size):
                raise EditError("validation", info.filename)
            # 重开后实际解压、检查 CRC，不只比较目录里声称的值。
            with new.open(info) as fh:
                while fh.read(1 << 20):
                    pass
    for part, data in changed.items():
        mode, exp = expected[part]
        with zipfile.ZipFile(io.BytesIO(original)) as old:
            _validate_part(_member_bytes(old, part), data, part, mode, exp)


def _source_check(source: Path, fingerprint: str) -> bytes:
    if source.is_symlink() or not source.is_file():
        raise EditError("source")
    # PowerPoint 的 owner file 与 Word 一样可能截去原文件名开头两个字符，两种都检查。
    for name in ("~$" + source.name, "~$" + source.name[2:]):
        if source.with_name(name).exists():
            raise EditError("locked")
    if source.stat().st_size > de.MAX_PACKAGE:
        raise EditError("size")
    raw = source.read_bytes()
    if not fingerprint or hashlib.sha256(raw).hexdigest() != fingerprint:
        raise EditError("fingerprint")
    return raw


def _backup(review: Path, batch: int, raw: bytes) -> Path:
    directory = review
    for name in (".snapshots", f"batch-{batch}", "pptx-writeback"):
        directory = directory / name
        if directory.is_symlink():
            raise EditError("source", str(directory))
        directory.mkdir(exist_ok=True, mode=0o700)
        os.chmod(directory, 0o700)
    fd, name = tempfile.mkstemp(prefix="original-", suffix=".pptx", dir=directory)
    with os.fdopen(fd, "wb") as fh:
        fh.write(raw)
        fh.flush()
        os.fsync(fh.fileno())
    return Path(name)


def _slide_key_of(note: dict, manifest: dict) -> str:
    key = note.get("key")
    if isinstance(key, str) and key:
        return key
    page = int(note.get("page", 0) or 0)
    for pg in manifest.get("pages") or []:
        if isinstance(pg, dict) and pg.get("n") == page and isinstance(pg.get("key"), str):
            return pg["key"]
    return ""


def _selection(review: Path, batch: int | None, ids: list[str] | None, manifest: dict,
               allow_notes: bool) -> tuple[list[dict], list[dict]]:
    """挑出要处理的批注：批次模式跳过已处理/无快捷修改的项并说明原因；其余不合格直接报错。"""
    doc = store.load(review)
    skipped: list[dict] = []
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
            skipped.append({"id": nid, "reason": "resolved" if note.get("status") == "resolved" else "noEdit"})
            continue
        ed, qf = note.get("edit") or {}, note.get("quoteFrom", "报告")
        side = "notes" if qf == "讲稿" else "shape" if qf == "报告" else None
        if (note.get("status", "open") != "open" or note.get("orphan") or note.get("hold")
                or side is None or ed.get("op") not in ("delete", "replace")
                or not isinstance(ed.get("from"), str) or not ed.get("from")):
            errors.append(EditError("note", item=nid))
            continue
        to = "" if ed["op"] == "delete" else ed.get("to", "")
        if side == "shape":
            if note.get("target", "报告") not in ("报告", "报告和讲稿"):
                errors.append(EditError("note", item=nid))
                continue
            a = note.get("pptx")
            if not store.usable_pptx_anchor(a) or a.get("kind") != "text":
                errors.append(EditError("anchor", item=nid))
                continue
            selected.append({"id": nid, "side": "shape", "anchor": a, "from": ed["from"], "to": to})
        else:
            if note.get("target", "讲稿") not in ("讲稿", "报告和讲稿"):
                errors.append(EditError("note", item=nid))
                continue
            if not allow_notes:
                errors.append(EditError("notes", item=nid))
                continue
            selected.append({"id": nid, "side": "notes", "from": ed["from"], "to": to,
                             "slide_key": _slide_key_of(note, manifest)})
    if errors:
        raise SelectionError(errors)
    return selected, skipped


def writeback(review: Path, *, batch: int | None = None, ids: list[str] | None = None,
              output: Path | None = None, dry_run: bool = False) -> dict:
    """公开入口：验证整组修改并写入（或预演），返回文件、备份、逐条修改与跳过原因；不改批注。"""
    review = Path(review).resolve()
    manifest = ledger.read_manifest(review)
    if (not manifest or ledger.unsupported_format(manifest)
            or (manifest.get("format") or {}).get("report") != "pptx"):
        raise EditError("review")
    try:
        sources = resolve_sources(review, manifest)
    except BuildError:
        raise EditError("source") from None
    source = sources.get("report")
    if source is None:
        raise EditError("source")
    fingerprint = (manifest.get("pptx") or {}).get("fingerprint", "")
    with store.locked(review):
        edits, skipped = _selection(review, batch, ids, manifest,
                                    allow_notes=sources.get("script") is None)
        if not edits:
            raise EditError("empty")
        raw = _source_check(source, fingerprint)
        stale = [EditError("anchor", item=e["id"]) for e in edits
                 if e.get("anchor") and e["anchor"].get("fp") not in (None, "")
                 and e["anchor"]["fp"] != fingerprint]
        if stale:
            raise SelectionError(stale)
        model = _read_model(raw)
        changed, tasks, expected = _run_edits(raw, edits, model)
        packed = raw
        for part, data in changed.items():
            packed = de.repack(packed, part, data)
        destination = Path(output).absolute() if output else source
        separate = destination != source
        if destination.is_symlink() or (separate and destination.exists()):
            raise EditError("output", str(destination))
        result = {"source": str(source), "output": str(destination), "dryRun": dry_run,
                  "backup": None, "ids": [t.id for t in tasks], "pages": sorted({t.page for t in tasks}),
                  "changes": [{"id": t.id, "kind": t.kind, "slide": t.slide, "page": t.page,
                               "part": t.part, "para": t.para, "element": t.element} for t in tasks],
                  "skipped": skipped, "warnings": []}
        if dry_run:
            _validate(io.BytesIO(packed), raw, changed, expected)
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
            _validate(temp, raw, changed, expected)
            _source_check(source, fingerprint)   # PowerPoint 或别的进程在准备期间动过源文件就拒绝提交
            if separate and destination.exists():
                raise EditError("output", str(destination))
            os.replace(temp, destination)
        finally:
            temp.unlink(missing_ok=True)
        return result


def _run_edits(raw: bytes, edits: list[dict], model: dict):
    """按批注目标读取涉及的部件字节并执行整组定位与应用。"""
    slides_by_key = {s["key"]: s for s in model["slides"]}
    needed: set[str] = set()
    for e in edits:
        if e["side"] == "shape":
            got = pr.relocate(e["anchor"], model["slides"])
            if got["status"] == "ok" and got.get("slide"):
                needed.add(got["slide"]["part"])
        else:
            slide = slides_by_key.get(e.get("slide_key") or "")
            if slide is not None and isinstance(slide.get("notes"), dict) \
                    and slide["notes"].get("status") == "available":
                needed.add(slide["notes"]["part"])
    raw_parts = {part: _part_bytes(raw, part) for part in needed}
    return edit_parts(raw_parts, edits, model)


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
    ap = i18n.LocalizedParser(prog="pptx_edit.py", description=t("pptx.edit.help"), keys=ERROR_KEYS, translate=t)
    ap.add_argument("command", choices=("apply",))
    ap.add_argument("review", type=Path)
    ap.add_argument("--batch", type=int, help=t("pptx.edit.batchHelp"))
    ap.add_argument("--id", action="append", dest="ids", help=t("pptx.edit.idHelp"))
    ap.add_argument("--output", type=Path, help=t("pptx.edit.outputHelp"))
    ap.add_argument("--dry-run", action="store_true", help=t("pptx.edit.dryHelp"))
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--locale")
    a = ap.parse_args(argv)
    try:
        if a.batch is not None and a.batch < 1:
            raise EditError("arguments")
        if bool(a.ids) == (a.batch is not None):
            raise EditError("arguments")
        result = writeback(a.review, batch=a.batch, ids=a.ids, output=a.output, dry_run=a.dry_run)
        sep = ", " if language == "en" else "、"
        pages = sep.join(str(n) for n in result["pages"])
        print(json.dumps(result, ensure_ascii=False) if a.json else t(
            "pptx.edit.preview" if a.dry_run else "pptx.edit.done", path=result["output"], pages=pages))
        if result["backup"] and not a.json:
            print(t("pptx.edit.backup", path=result["backup"]))
        return 0
    except (EditError, pr.PptxError, BuildError, OSError, ValueError, KeyError, zipfile.BadZipFile, zlib.error) as exc:
        errors = exc.errors if isinstance(exc, SelectionError) else [exc]
        for error in errors:
            code = error.code if isinstance(error, EditError) else "io"
            detail = error.detail if isinstance(error, EditError) else str(error)
            if code == "readonly":
                detail = t("viewer.pptx.kind." + detail) if detail else detail
            message = t("pptx.edit.error." + code, detail=detail)
            print((f"{error.item}: " if isinstance(error, EditError) and error.item else "") + message, file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
