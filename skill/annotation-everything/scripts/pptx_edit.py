#!/usr/bin/env python3
"""按审阅批注回写 PPTX：只改目标幻灯片/备注部件里的 a:t，保留其他部件字节与 ZIP 成员的压缩数据。

apply REVIEW --batch N / --id ID：处理报告侧普通文本形状与讲稿侧已有备注的快捷替换、删除。
报告侧按批注的幻灯片定位（元素身份 + 逻辑段落 + 码点偏移）落到具体 a:t，跨 run 选区保留
未触及文本及其属性；讲稿侧只在未使用外部讲稿时回写已有备注部件，按引用原文在备注正文
唯一命中定位。所有选择先整体校验；任何一条失败都不写。不会处理批注状态。

ops REVIEW --ops FILE：白名单几何与样式回写（v1 契约，见 references/formats）。操作文件是
{"v": 1, "ops": [...]}；每个操作按幻灯片键 + 元素身份（形状标识 + 证据摘要）逐目标预检：
geometry 用归一化坐标（unit=normalized）换算 EMU 改 a:off/a:ext，style 用 text/fill/line 三类
白名单属性写直接格式（rPr 属性与 solidFill、spPr solidFill、a:ln）。位置、尺寸、属性与单位
超出契约即拒绝；同一元素被多个操作命中拒绝；写入仍走快照 + 原子替换 + 写后重建核验闭环。
自动重叠/缩小/出界警告与视觉核验状态分开记录；视觉核验未执行不得视为通过。
"""
from __future__ import annotations

import hashlib
import io
import json
import math
import os
import re
import stat
import sys
import tempfile
import zipfile
import zlib
from dataclasses import dataclass
from functools import partial
from pathlib import Path
from xml.parsers import expat
from xml.sax.saxutils import escape as _attr_escape_raw

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

# ---------- 白名单几何与样式回写（ops 契约 v1，见 references/formats） ----------
OPS_VERSION = 1
GEOMETRY_EPS = 1e-4                      # 画布边界容差，与 notes_store 归一化区域一致
ASPECTS = ("geometry", "style")
OPS_SCOPES = ("text", "fill", "line")
GEOMETRY_UNITS = ("normalized",)
GEOMETRY_KEYS = ("x", "y", "width", "height")
TEXT_PROPS = ("bold", "italic", "underline", "size", "color")
FILL_PROPS = ("color",)
LINE_PROPS = ("color", "width")
SIZE_PT_MIN, SIZE_PT_MAX = 1.0, 4000.0   # a:rPr/@sz 以百分之一磅为单位（ST_TextFontSize 100..400000）
LINE_PT_MAX = 1584.0                     # a:ln/@w 以 EMU 为单位（ST_LineWidth 上限 20116800）
_EMU_PER_POINT = 12700
_SRGB_RE = re.compile(r"[0-9A-Fa-f]{6}\Z")
_DIGEST_RE = re.compile(r"[0-9a-f]{64}\Z")
OVERLAP_EPS = 1e-9

SPPR = P_ + "spPr"
XFRM = A_ + "xfrm"
OFF = A_ + "off"
EXT = A_ + "ext"
LN = A_ + "ln"
RPR = A_ + "rPr"
SRGB = A_ + "srgbClr"
FILL_KINDS = ("noFill", "solidFill", "gradFill", "blipFill", "pattFill", "grpFill")
LN_FILL_KINDS = ("noFill", "solidFill", "gradFill", "pattFill")
# 白名单外的直接子元素一律拒绝：不能证明改写安全的结构保持只读
SPPR_ALLOWED = frozenset(("xfrm", "prstGeom", "custGeom", "ln", "effectLst", "effectDag",
                          "scene3d", "sp3d", "extLst") + FILL_KINDS)
LN_ALLOWED = frozenset(("prstDash", "custDash", "round", "bevel", "miter",
                        "headEnd", "tailEnd", "extLst") + LN_FILL_KINDS)
RPR_ALLOWED = frozenset(("ln", "effectLst", "effectDag", "highlight", "uLnTx", "uLn", "uFillTx",
                         "uFill", "latin", "ea", "cs", "sym", "hlinkClick", "hlinkMouseOver",
                         "rtl", "extLst") + FILL_KINDS)
# 新元素的插入位置按 ECMA-376 子元素顺序：插到这些局部名之前，否则插到内容末尾
SPPR_BEFORE_FILL = ("ln", "effectLst", "effectDag", "scene3d", "sp3d", "extLst")
SPPR_BEFORE_LN = ("effectLst", "effectDag", "scene3d", "sp3d", "extLst")
LN_BEFORE_FILL = ("prstDash", "custDash", "round", "bevel", "miter", "headEnd", "tailEnd", "extLst")
RPR_BEFORE_FILL = ("effectLst", "effectDag", "highlight", "uLnTx", "uLn", "uFillTx", "uFill",
                   "latin", "ea", "cs", "sym", "hlinkClick", "hlinkMouseOver", "rtl", "extLst")


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


# ---------- 白名单几何与样式：字节级属性解析（只认顶层 p:sp，与能力矩阵同一口径） ----------
def _raw_tag_name(raw: bytes, start: int) -> bytes:
    """start 指向 '<'；返回起始标签里的原始限定名（保留文档自己的前缀）。"""
    end = start + 1
    while end < len(raw) and raw[end:end + 1] not in (b" ", b"\t", b"\n", b"\r", b">", b"/"):
        end += 1
    return raw[start + 1:end]


def _attr_bytes(value: str) -> bytes:
    return _attr_escape_raw(value, {'"': "&quot;"}).encode("utf-8")


def _open_tag(name: bytes, attrs: dict, empty: bool) -> bytes:
    parts = [b"<", name]
    for key, value in attrs.items():
        parts.append(b" " + str(key).encode("utf-8") + b'="' + _attr_bytes(str(value)) + b'"')
    parts.append(b"/>" if empty else b">")
    return b"".join(parts)


def _el(name: bytes, attrs: dict, children: bytes = b"") -> bytes:
    if not children:
        return _open_tag(name, attrs, True)
    return _open_tag(name, attrs, False) + children + b"</" + name + b">"


def _srgb_of(node: dict | None) -> str | None:
    return node.get("srgb") if node else None


def _int_attr(node: dict, key: str) -> int:
    value = node["attrs"].get(key, "")
    try:
        return int(value)
    except ValueError:
        raise EditError("structure", node["local"] + "@" + key) from None


class _PropsParser:
    """跟踪顶层 p:sp 的 xfrm/spPr/ln/rPr 字节跨度与当前值，供白名单操作做最小字节改写。

    与 _PartParser 同一套闸门（UTF-8、无 DTD、大小上限）与顶层形状口径；组合形状内部、
    图片等不在白名单内的结构只跳过不跟踪。名称一律以 UTF-8 字节为准。
    """

    def __init__(self, raw: bytes, part: str):
        self.raw, self.part = raw, part
        self.shapes: list[dict] = []
        self.stack: list[str] = []
        self.frames: list[dict | None] = []   # 与 stack 平行；跟踪中的元素节点
        self.a_prefix: bytes | None = None
        self.sp_tree_depth: int | None = None
        self.shape: dict | None = None
        self.shape_depth: int | None = None
        self.nv_depth: int | None = None
        self.sp_depth: int | None = None
        self.xfrm_depth: int | None = None
        self.ln_depth: int | None = None
        self.txbody_depth: int | None = None
        self.run_depth: int | None = None
        self.rpr_depth: int | None = None
        self.run: dict | None = None
        self.text_target: dict | None = None

    def _node(self, at: int, name: str, attrs: dict) -> dict:
        opening_end = de._tag_end(self.raw, at)
        empty = self.raw[opening_end - 2:opening_end] == b"/>"
        local = _local(name)
        return {"expat": name, "local": local, "start": at, "opening_end": opening_end,
                "stop": opening_end if empty else 0, "empty": empty,
                "content_end": opening_end if empty else 0, "attrs": dict(attrs),
                "children": [], "srgb": None, "fill": None}

    def _capture_prefix(self, at: int, name: str) -> None:
        local = name[len(A_):].encode("utf-8")
        raw_name = _raw_tag_name(self.raw, at)
        if raw_name.endswith(local) and len(raw_name) > len(local):
            self.a_prefix = raw_name[:len(raw_name) - len(local) - 1]
        else:
            self.a_prefix = b""   # 默认命名空间绑定 drawingml

    def _track_child(self, at: int, name: str, attrs: dict) -> dict:
        node = self._node(at, name, attrs)
        self.frames[-1] = node
        return node

    def declaration(self, _version, encoding, _standalone):
        if encoding and encoding.lower().replace("-", "") != "utf8":
            raise EditError("encoding")

    def doctype(self, *_args):
        raise EditError("xml", "DTD")

    def start(self, name, attrs):
        at = self.parser.CurrentByteIndex
        self.stack.append(name)
        self.frames.append(None)
        depth = len(self.stack)
        parent = self.stack[-2] if depth >= 2 else ""
        if self.a_prefix is None and name.startswith(A_):
            self._capture_prefix(at, name)
        if name == PSP_TREE:
            self.sp_tree_depth = depth
            return
        if self.shape is None:
            if name == PSP and self.sp_tree_depth is not None and depth == self.sp_tree_depth + 1:
                self.shape = {"sid": "", "order": len(self.shapes), "start": at, "stop": 0,
                              "sp_node": None, "children": [], "off": None, "ext": None,
                              "fill": None, "ln": None, "runs": []}
                self.shape_depth = depth
                self.nv_depth = self.sp_depth = self.xfrm_depth = self.ln_depth = None
                self.txbody_depth = self.run_depth = self.rpr_depth = None
            return
        if name == PSP and depth == self.shape_depth:
            return   # end 分支处理
        if name == PCNVPR and self.nv_depth is not None and depth == self.nv_depth + 1:
            if not self.shape["sid"]:
                self.shape["sid"] = attrs.get("id", "")
            return
        if depth == self.shape_depth + 1:
            if _local(name).startswith("nv") and parent == PSP:
                self.nv_depth = depth
            elif name == SPPR and self.sp_depth is None:
                self.sp_depth = depth
                node = self._track_child(at, name, attrs)
                self.shape["sp_node"] = node
            elif name == PTXBODY and self.txbody_depth is None:
                self.txbody_depth = depth
            return
        if self.sp_depth is not None and depth == self.sp_depth + 1:
            node = self._track_child(at, name, attrs)
            self.shape["children"].append(node)
            local = node["local"]
            if local in FILL_KINDS and self.shape["fill"] is None:
                self.shape["fill"] = node
            elif name == XFRM and self.xfrm_depth is None:
                self.xfrm_depth = depth
            elif name == LN and self.shape["ln"] is None:
                self.ln_depth = depth
                self.shape["ln"] = node
            return
        if self.xfrm_depth is not None and depth == self.xfrm_depth + 1:
            node = self._track_child(at, name, attrs)
            if name == OFF and self.shape["off"] is None:
                self.shape["off"] = node
            elif name == EXT and self.shape["ext"] is None:
                self.shape["ext"] = node
            return
        if self.ln_depth is not None and depth == self.ln_depth + 1:
            node = self._track_child(at, name, attrs)
            self.shape["ln"]["children"].append(node)
            if node["local"] in LN_FILL_KINDS and self.shape["ln"]["fill"] is None:
                self.shape["ln"]["fill"] = node
            return
        if self.txbody_depth is not None and depth == self.txbody_depth + 1 and name == AP:
            self.run_depth = None
            self.rpr_depth = None
            return
        if self.txbody_depth is not None and depth == self.txbody_depth + 2 and parent == AP \
                and name in (AR, AFLD):
            self.run_depth = depth
            self.rpr_depth = None
            self.run = {"open_end": de._tag_end(self.raw, at), "rpr": None, "text": ""}
            self.shape["runs"].append(self.run)
            return
        if self.run_depth is not None and depth == self.run_depth + 1:
            if name == RPR and self.run["rpr"] is None:
                node = self._track_child(at, name, attrs)
                self.run["rpr"] = node
                self.rpr_depth = depth
            elif name == AT:
                self.text_target = self.run
            return
        if self.rpr_depth is not None and depth == self.rpr_depth + 1:
            node = self._track_child(at, name, attrs)
            rpr = self.run["rpr"]
            rpr["children"].append(node)
            if node["local"] in FILL_KINDS and rpr["fill"] is None:
                rpr["fill"] = node
            return
        if name == SRGB and depth >= 2 and self.frames[-2] is not None:
            holder = self.frames[-2]
            if holder.get("srgb") is None:
                holder["srgb"] = attrs.get("val", "")

    def end(self, name):
        depth = len(self.stack)
        at = self.parser.CurrentByteIndex
        node = self.frames.pop()
        self.stack.pop()
        if node is not None:
            node["content_end"] = at
            node["stop"] = de._tag_end(self.raw, at)
        if name == AT:
            self.text_target = None
        if name == RPR:
            self.rpr_depth = None
        if name in (AR, AFLD) and depth == self.run_depth:
            self.run_depth = None
            self.run = None
        if name == XFRM and depth == self.xfrm_depth:
            self.xfrm_depth = None
        if name == LN and depth == self.ln_depth:
            self.ln_depth = None
        if name == SPPR and depth == self.sp_depth:
            self.sp_depth = self.xfrm_depth = self.ln_depth = None
        if name == PTXBODY and depth == self.txbody_depth:
            self.txbody_depth = None
        if name == PSP and self.shape is not None and depth == self.shape_depth:
            self.shape["stop"] = de._tag_end(self.raw, at)
            self.shapes.append(self.shape)
            self.shape = None
            self.nv_depth = self.sp_depth = self.xfrm_depth = self.ln_depth = None
            self.txbody_depth = self.run_depth = self.rpr_depth = None

    def text(self, value):
        if self.text_target is not None:
            self.text_target["text"] += value

    def parse(self) -> list[dict]:
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
        if self.stack:
            raise EditError("xml", "unbalanced")
        return self.shapes


def _parse_props(raw: bytes, part: str) -> tuple[list[dict], bytes]:
    """(形状, drawingml 命名空间前缀)。前缀取自部件里第一个 a: 元素，用于按文档前缀构造新元素。"""
    parser = _PropsParser(raw, part)
    try:
        shapes = parser.parse()
    except EditError as exc:
        raise EditError(exc.code, exc.detail or part, item=exc.item) from None
    if parser.a_prefix is None:
        raise EditError("structure", part, item=part)
    return shapes, parser.a_prefix


def _props_shape(shapes: list[dict], sid: str, item: str = "") -> dict:
    hits = [s for s in shapes if s["sid"] == sid]
    if len(hits) != 1:
        raise EditError("anchor", sid, item=item)
    return hits[0]


def _props_snapshot(shape: dict) -> dict:
    """值快照：只含白名单写得到的字段，供写后逐形状核对（未触及形状要求完全一致）。"""
    off = ext = None
    if shape["off"] is not None and shape["ext"] is not None:
        try:
            off = (_int_attr(shape["off"], "x"), _int_attr(shape["off"], "y"))
            ext = (_int_attr(shape["ext"], "cx"), _int_attr(shape["ext"], "cy"))
        except EditError:
            off = ext = None
    fill = None
    if shape["fill"] is not None:
        fill = (shape["fill"]["local"], _srgb_of(shape["fill"]))
    ln = None
    if shape["ln"] is not None:
        raw_w = shape["ln"]["attrs"].get("w")
        width = int(raw_w) if isinstance(raw_w, str) and re.fullmatch(r"-?\d+", raw_w) else None
        lfill = shape["ln"]["fill"]
        ln = (width, lfill["local"] if lfill else None, _srgb_of(lfill))
    runs = []
    for run in shape["runs"]:
        rpr = run["rpr"]
        runs.append((tuple(sorted(rpr["attrs"].items())) if rpr else (),
                     _srgb_of(rpr["fill"]) if rpr else None, run["text"]))
    return {"off": off, "ext": ext, "fill": fill, "ln": ln, "runs": runs}


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


# ---------- 白名单几何与样式：操作校验、换算与最小字节改写 ----------
def _qname(prefix: bytes, local: bytes) -> bytes:
    return prefix + b":" + local if prefix else local


def _no_bool_number(value: object) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _ops_target(v: object, oid: str) -> dict:
    """操作目标与批注交换的元素目标同一形状：key/id/digest 必填，未知键拒绝。"""
    if not isinstance(v, dict):
        raise EditError("ops", "target", item=oid)
    if set(v) - {"key", "id", "name", "kind", "z", "digest"}:
        raise EditError("ops", "target", item=oid)
    for field in ("key", "id", "digest"):
        value = v.get(field)
        if not isinstance(value, str) or not 0 < len(value.strip()) <= 120:
            raise EditError("ops", "target." + field, item=oid)
    if not _DIGEST_RE.fullmatch(v["digest"]):
        raise EditError("ops", "target.digest", item=oid)
    out = {k: v[k].strip() for k in ("key", "id")}
    out["digest"] = v["digest"]
    if v.get("name") is not None:
        if not isinstance(v["name"], str):
            raise EditError("ops", "target.name", item=oid)
        out["name"] = v["name"]
    if v.get("kind") is not None:
        if v["kind"] not in store.PPTX_EL_KINDS:
            raise EditError("ops", "target.kind", item=oid)
        out["kind"] = v["kind"]
    if v.get("z") is not None:
        if isinstance(v["z"], bool) or not isinstance(v["z"], int) or v["z"] < 0:
            raise EditError("ops", "target.z", item=oid)
        out["z"] = v["z"]
    return out


def _srgb_value(v: object, oid: str, where: str) -> str:
    if not isinstance(v, str) or not _SRGB_RE.fullmatch(v):
        raise EditError("value", where + " color", item=oid)
    return v.upper()


def _validate_ops(spec: object) -> list[dict]:
    """操作文件契约 v1：{"v": 1, "ops": [...]}。未知键、未知属性与非法取值一律保守拒绝。"""
    if not isinstance(spec, dict):
        raise EditError("ops", "root")
    version = spec.get("v")
    if isinstance(version, bool) or not isinstance(version, int) or version < 1:
        raise EditError("ops", "v")
    if version > OPS_VERSION:
        raise EditError("version", str(version))
    if set(spec) - {"v", "ops"}:
        raise EditError("ops", "root." + sorted(set(spec) - {"v", "ops"})[0])
    ops = spec.get("ops")
    if not isinstance(ops, list) or not ops or len(ops) > store.MAX_PPTX_ELEMENTS:
        raise EditError("ops", "ops")
    out: list[dict] = []
    seen_ids: set[str] = set()
    for i, raw_op in enumerate(ops):
        oid = f"op-{i + 1}"
        if not isinstance(raw_op, dict):
            raise EditError("ops", "ops[]", item=oid)
        unknown = set(raw_op) - {"id", "slide", "target", "aspect", "unit", "set", "scope", "props"}
        if unknown:
            raise EditError("ops", "ops[]." + sorted(unknown)[0], item=oid)
        if "id" in raw_op:
            value = raw_op["id"]
            if not isinstance(value, str) or not 0 < len(value.strip()) <= 120:
                raise EditError("ops", "ops[].id", item=oid)
            oid = value.strip()
        if oid in seen_ids:
            raise EditError("ops", "duplicate id", item=oid)
        seen_ids.add(oid)
        slide = raw_op.get("slide")
        if not isinstance(slide, str) or not 0 < len(slide.strip()) <= 120:
            raise EditError("ops", "ops[].slide", item=oid)
        aspect = raw_op.get("aspect")
        if aspect not in ASPECTS:
            raise EditError("ops", "aspect", item=oid)
        op: dict = {"id": oid, "slide": slide.strip(), "aspect": aspect,
                    "target": _ops_target(raw_op.get("target"), oid)}
        if aspect == "geometry":
            unit = raw_op.get("unit")
            if unit not in GEOMETRY_UNITS:
                raise EditError("unit", "normalized" if unit is None else str(unit), item=oid)
            values = raw_op.get("set")
            if not isinstance(values, dict) or not values:
                raise EditError("ops", "set", item=oid)
            bad = set(values) - set(GEOMETRY_KEYS)
            if bad:
                raise EditError("property", sorted(bad)[0], item=oid)
            for key, value in values.items():
                if not _no_bool_number(value):
                    raise EditError("value", "geometry " + key, item=oid)
            op["unit"], op["set"] = unit, dict(values)
        else:
            scope = raw_op.get("scope")
            if scope not in OPS_SCOPES:
                raise EditError("ops", "scope", item=oid)
            props = raw_op.get("props")
            if not isinstance(props, dict) or not props:
                raise EditError("ops", "props", item=oid)
            allowed = {"text": TEXT_PROPS, "fill": FILL_PROPS, "line": LINE_PROPS}[scope]
            bad = set(props) - set(allowed)
            if bad:
                raise EditError("property", sorted(bad)[0], item=oid)
            cleaned: dict = {}
            for key, value in props.items():
                if key in ("bold", "italic", "underline"):
                    if not isinstance(value, bool):
                        raise EditError("value", key, item=oid)
                elif key == "size":
                    if not _no_bool_number(value) or not SIZE_PT_MIN <= value <= SIZE_PT_MAX:
                        raise EditError("value", "size", item=oid)
                    value = round(value * 100) / 100.0
                elif key == "width":
                    if not _no_bool_number(value) or not 0 < value <= LINE_PT_MAX:
                        raise EditError("value", "width", item=oid)
                    value = round(value * _EMU_PER_POINT) / _EMU_PER_POINT
                else:
                    value = _srgb_value(value, oid, scope)
                cleaned[key] = value
            op["scope"], op["props"] = scope, cleaned
        out.append(op)
    return out


def _geometry_emu(op: dict, shape: dict, canvas_w: int, canvas_h: int) -> tuple[int, int, int, int]:
    """归一化坐标 → 源文件 EMU。未设置的维度保持当前值；越界与退化尺寸在换算后拒绝。"""
    if shape["off"] is None or shape["ext"] is None:
        raise EditError("structure", "xfrm")
    try:
        ox, oy = _int_attr(shape["off"], "x"), _int_attr(shape["off"], "y")
        ocx, ocy = _int_attr(shape["ext"], "cx"), _int_attr(shape["ext"], "cy")
    except EditError:
        raise EditError("structure", "xfrm") from None
    values = op["set"]
    x = values.get("x", ox / canvas_w)
    y = values.get("y", oy / canvas_h)
    width = values.get("width", ocx / canvas_w)
    height = values.get("height", ocy / canvas_h)
    for name, value in (("x", x), ("y", y)):
        if value < 0 or value > 1 + GEOMETRY_EPS:
            raise EditError("bounds", name, item=op["id"])
    for name, value in (("width", width), ("height", height)):
        if value <= 0:
            raise EditError("value", "geometry " + name, item=op["id"])
    if x + width > 1 + GEOMETRY_EPS or y + height > 1 + GEOMETRY_EPS:
        raise EditError("bounds", "canvas", item=op["id"])
    left, top = round(x * canvas_w), round(y * canvas_h)
    right, bottom = round((x + width) * canvas_w), round((y + height) * canvas_h)
    cx, cy = right - left, bottom - top
    if cx <= 0 or cy <= 0:
        raise EditError("value", "geometry emu", item=op["id"])
    return left, top, cx, cy


def _insert_point(children: list[dict], before: tuple[str, ...],
                  parent: dict) -> tuple[int, int, bool]:
    """(start, stop, whole)：插到 before 中最早的子元素之前，否则父元素内容末尾；空元素整节点重写。"""
    for child in children:
        if child["local"] in before:
            return child["start"], child["start"], False
    if parent["empty"]:
        return parent["start"], parent["stop"], True
    return parent["content_end"], parent["content_end"], False


def _with_children(raw: bytes, node: dict, addition: bytes) -> bytes:
    """把空元素节点改写成带子元素的形式；名称保留文档自己的前缀。"""
    name = _raw_tag_name(raw, node["start"])
    return _open_tag(name, node["attrs"], False) + addition + b"</" + name + b">"


def _fill_bytes(prefix: bytes, color: str) -> bytes:
    return _el(_qname(prefix, b"solidFill"), {}, _el(_qname(prefix, b"srgbClr"), {"val": color}))


def _check_children(children: list[dict], allowed: frozenset, where: str, oid: str) -> None:
    for child in children:
        if child["local"] not in allowed:
            raise EditError("structure", where + "/" + child["local"], item=oid)


def _tag_rewrite(raw: bytes, node: dict, attrs: dict) -> tuple[int, int, bytes]:
    return node["start"], node["opening_end"], _open_tag(_raw_tag_name(raw, node["start"]), attrs, node["empty"])


def _attr_contract(label: str, prior: object) -> object:
    """run 属性的契约读法：缺失为 None，"0"/"false"/"none" 为 False，其余取真值的枚举为 True，size 为磅。"""
    if label == "size":
        try:
            return round(int(prior) / 100.0, 2) if prior is not None else None
        except (TypeError, ValueError):
            return None
    if prior is None:
        return None
    if prior in ("0", "false", "none"):
        return False
    if prior in ("1", "true", "sng"):
        return True
    return prior


def _style_edits(op: dict, oid: str, raw: bytes, shape: dict, prefix: bytes)         -> tuple[list[tuple[int, int, bytes]], dict, list[dict]]:
    """白名单样式：只做直接格式的替换/插入；与当前直接格式一致且无字节变化时拒绝。"""
    scope, props = op["scope"], op["props"]
    spans: list[tuple[int, int, bytes]] = []
    if scope == "text":
        if not shape["runs"]:
            raise EditError("value", "text", item=oid)
        wanted: dict[str, str] = {}
        if "bold" in props:
            wanted["b"] = "1" if props["bold"] else "0"
        if "italic" in props:
            wanted["i"] = "1" if props["italic"] else "0"
        if "underline" in props:
            wanted["u"] = "sng" if props["underline"] else "none"
        if "size" in props:
            wanted["sz"] = str(int(round(props["size"] * 100)))
        color = props.get("color")
        expected_runs = []
        for run in shape["runs"]:
            rpr = run["rpr"]
            if rpr is None:
                body = _fill_bytes(prefix, color) if color else b""
                attrs = {k: wanted[k] for k in ("b", "i", "u", "sz") if k in wanted}
                spans.append((run["open_end"], run["open_end"],
                              _el(_qname(prefix, b"rPr"), attrs, body)))
                expected_runs.append((tuple(sorted(attrs.items())), color, run["text"]))
                continue
            new_attrs = dict(rpr["attrs"])
            new_attrs.update(wanted)
            if new_attrs != rpr["attrs"]:
                spans.append(_tag_rewrite(raw, rpr, new_attrs))
            new_color = _srgb_of(rpr["fill"]) if rpr["fill"] and rpr["fill"]["local"] == "solidFill" else None
            if color is not None and (rpr["fill"] is None or rpr["fill"]["local"] != "solidFill"
                                      or _srgb_of(rpr["fill"]) != color):
                _check_children(rpr["children"], RPR_ALLOWED, "rPr", oid)
                piece = _fill_bytes(prefix, color)
                if rpr["fill"] is not None:
                    spans.append((rpr["fill"]["start"], rpr["fill"]["stop"], piece))
                else:
                    start, stop, whole = _insert_point(rpr["children"], RPR_BEFORE_FILL, rpr)
                    spans.append((start, stop, _with_children(raw, rpr, piece) if whole else piece))
                new_color = color
            expected_runs.append((tuple(sorted(new_attrs.items())), new_color, run["text"]))
        if not spans:
            raise EditError("noChange", "text", item=oid)
        expected = {"runs": expected_runs}
        change_props = []
        for key in ("bold", "italic", "underline", "size", "color"):
            if key not in props:
                continue
            if key == "color":
                mapped = [_attr_contract(key, _srgb_of(r["rpr"]["fill"])
                                         if r["rpr"] and r["rpr"]["fill"]
                                         and r["rpr"]["fill"]["local"] == "solidFill" else None)
                          for r in shape["runs"]]
            else:
                source = {"bold": "b", "italic": "i", "underline": "u", "size": "sz"}[key]
                mapped = [_attr_contract(key, r["rpr"]["attrs"].get(source) if r["rpr"] else None)
                          for r in shape["runs"]]
            prior = mapped[0] if mapped and all(m == mapped[0] for m in mapped) else "mixed"
            change_props.append({"name": key, "unit": None if key in ("bold", "italic", "underline")
                                 else "pt" if key == "size" else "srgb", "from": prior, "to": props[key]})
        return spans, expected, change_props
    _check_children(shape["children"], SPPR_ALLOWED, "spPr", oid)
    if scope == "fill":
        color = props["color"]
        fill = shape["fill"]
        if fill is not None and fill["local"] == "solidFill" and _srgb_of(fill) == color:
            raise EditError("noChange", "fill", item=oid)
        prior = _srgb_of(fill) if fill is not None and fill["local"] == "solidFill" else None
        piece = _fill_bytes(prefix, color)
        if fill is not None:
            spans.append((fill["start"], fill["stop"], piece))
        else:
            start, stop, whole = _insert_point(shape["children"], SPPR_BEFORE_FILL, shape["sp_node"])
            spans.append((start, stop, _with_children(raw, shape["sp_node"], piece) if whole else piece))
        return spans, {"fill": ("solidFill", color)},             [{"name": "color", "unit": "srgb", "from": prior, "to": color}]
    color = props.get("color")
    width = int(round(props["width"] * _EMU_PER_POINT)) if "width" in props else None
    ln = shape["ln"]
    old = _props_snapshot(shape)["ln"] or (None, None, None)
    if ln is None:
        attrs = {"w": str(width)} if width is not None else {}
        body = _fill_bytes(prefix, color) if color else b""
        piece = _el(_qname(prefix, b"ln"), attrs, body)
        start, stop, whole = _insert_point(shape["children"], SPPR_BEFORE_LN, shape["sp_node"])
        spans.append((start, stop, _with_children(raw, shape["sp_node"], piece) if whole else piece))
        new_ln = (width, "solidFill" if color else None, color)
    else:
        lattrs = dict(ln["attrs"])
        if width is not None and lattrs.get("w") != str(width):
            lattrs["w"] = str(width)
            spans.append(_tag_rewrite(raw, ln, lattrs))
        if color is not None and (ln["fill"] is None or ln["fill"]["local"] != "solidFill"
                                  or _srgb_of(ln["fill"]) != color):
            _check_children(ln["children"], LN_ALLOWED, "a:ln", oid)
            piece = _fill_bytes(prefix, color)
            if ln["fill"] is not None:
                spans.append((ln["fill"]["start"], ln["fill"]["stop"], piece))
            else:
                start, stop, whole = _insert_point(ln["children"], LN_BEFORE_FILL, ln)
                spans.append((start, stop, _with_children(raw, ln, piece) if whole else piece))
        new_ln = (width if width is not None else old[0],
                  "solidFill" if color is not None else old[1],
                  color if color is not None else old[2])
    if not spans:
        raise EditError("noChange", "line", item=oid)
    change_props = []
    if width is not None:
        prior = old[0]
        change_props.append({"name": "width", "unit": "pt",
                             "from": round(prior / _EMU_PER_POINT, 4) if prior is not None else None,
                             "to": round(width / _EMU_PER_POINT, 4)})
    if color is not None:
        change_props.append({"name": "color", "unit": "srgb", "from": old[2], "to": color})
    return spans, {"ln": new_ln}, change_props


def _run_ops(raw: bytes, spec: object, model: dict):
    """整组白名单操作：逐目标预检（身份、能力、冲突）→ 逐部件计算字节改写 → 一次应用。

    任何一条失败都不写。返回 (changed_parts, changes, warnings, checkers)。
    """
    op_list = _validate_ops(spec)
    resolved, errors = [], []
    claimed: dict[str, str] = {}
    for op in op_list:
        oid = op["id"]
        try:
            got = pr.relocate({"slide": op["slide"], "elements": [op["target"]]}, model["slides"])
            if got["status"] != "ok":
                code = "ambiguous" if got["status"] == "ambiguous" else "anchor"
                raise EditError(code, got.get("why") or "", item=oid)
            slide = got["slide"]
            tgt = (got.get("targets") or [None])[0]
            el = next((x for x in slide["elements"] if tgt and x["key"] == tgt.get("key")), None)
            if el is None:
                raise EditError("anchor", item=oid)
            if el["key"] in claimed:
                raise EditError("conflict", claimed[el["key"]], item=oid)
            if op["aspect"] == "geometry" and not el["capabilities"]["geometryWrite"]:
                raise EditError("readonlyGeometry", el["kind"], item=oid)
            if op["aspect"] == "style" and not el["capabilities"]["styleWrite"]:
                raise EditError("readonlyStyle", el["kind"], item=oid)
            claimed[el["key"]] = oid
            resolved.append((op, slide, el))
        except EditError as exc:
            if not exc.item:
                exc.item = oid
            errors.append(exc)
    if errors:
        raise SelectionError(errors)

    raw_parts: dict[str, bytes] = {}
    parsed: dict[str, tuple[list[dict], bytes]] = {}
    spans_by_part: dict[str, list[tuple[int, int, bytes]]] = {}
    expected_by_part: dict[str, dict[str, dict]] = {}
    touched_by_part: dict[str, set[str]] = {}
    changes: list[dict] = []
    warnings: list[dict] = []
    canvas_w, canvas_h = int(model["canvas"]["width"]), int(model["canvas"]["height"])
    for op, slide, el in resolved:
        oid, part = op["id"], slide["part"]
        if part not in parsed:
            raw_parts[part] = _part_bytes(raw, part)
            parsed[part] = _parse_props(raw_parts[part], part)
        shapes, prefix = parsed[part]
        shape = _props_shape(shapes, str(el["id"]), item=oid)
        if shape["sp_node"] is None:
            raise EditError("structure", "spPr", item=oid)
        old = _props_snapshot(shape)
        change_props: list[dict] = []
        if op["aspect"] == "geometry":
            left, top, cx, cy = _geometry_emu(op, shape, canvas_w, canvas_h)
            if (left, top, cx, cy) == (old["off"][0], old["off"][1], old["ext"][0], old["ext"][1]):
                raise EditError("noChange", "geometry", item=oid)
            values = op["set"]
            if "x" in values or "y" in values:
                spans_by_part.setdefault(part, []).append(_tag_rewrite(
                    raw_parts[part], shape["off"], {**shape["off"]["attrs"],
                                                    "x": str(left), "y": str(top)}))
            if "width" in values or "height" in values:
                spans_by_part.setdefault(part, []).append(_tag_rewrite(
                    raw_parts[part], shape["ext"], {**shape["ext"]["attrs"],
                                                    "cx": str(cx), "cy": str(cy)}))
            names = {"x": ("x", old["off"][0], left), "y": ("y", old["off"][1], top),
                     "width": ("cx", old["ext"][0], cx), "height": ("cy", old["ext"][1], cy)}
            change_props = [{"name": names[k][0], "unit": "emu", "from": names[k][1], "to": names[k][2]}
                            for k in ("x", "y", "width", "height") if k in values]
            if ("width" in values and cx < old["ext"][0]) or ("height" in values and cy < old["ext"][1]):
                warnings.append({"id": oid, "code": "shrink", "page": slide["n"], "detail": ""})
            if left + cx > canvas_w or top + cy > canvas_h:
                warnings.append({"id": oid, "code": "outOfCanvas", "page": slide["n"], "detail": ""})
            nx, ny, nw, nh = left / canvas_w, top / canvas_h, cx / canvas_w, cy / canvas_h
            hits = []
            for other in slide["elements"]:
                if other["key"] == el["key"] or other.get("geometry") is None:
                    continue
                g = other["geometry"]["normalized"]
                ox = min(nx + nw, g["x"] + g["width"]) - max(nx, g["x"])
                oy = min(ny + nh, g["y"] + g["height"]) - max(ny, g["y"])
                if ox > OVERLAP_EPS and oy > OVERLAP_EPS:
                    hits.append(other["key"])
            if hits:
                warnings.append({"id": oid, "code": "overlap", "page": slide["n"],
                                 "detail": ", ".join(hits), "with": hits})
            expected_delta: dict = {"off": (left, top), "ext": (cx, cy)}
        else:
            spans, expected_delta, change_props = _style_edits(op, oid, raw_parts[part], shape, prefix)
            spans_by_part.setdefault(part, []).extend(spans)
        expected_by_part.setdefault(part, {})[shape["sid"]] = {**old, **expected_delta}
        touched_by_part.setdefault(part, set()).add(shape["sid"])
        changes.append({"id": oid, "kind": "shape", "slide": slide["key"], "page": slide["n"],
                        "part": part, "element": str(el["id"]), "aspect": op["aspect"],
                        "props": change_props})
    changed: dict[str, bytes] = {}
    for part, spans in spans_by_part.items():
        new = raw_parts[part]
        for start, stop, data in sorted(spans, key=lambda s: (s[0], s[1]), reverse=True):
            new = new[:start] + data + new[stop:]
        changed[part] = new
    checkers = {part: partial(_validate_props_part, part=part, expected=expected_by_part[part],
                              touched=touched_by_part[part]) for part in changed}
    return changed, changes, warnings, checkers


def _validate_props_part(old_raw: bytes, new_raw: bytes, part: str,
                         expected: dict[str, dict], touched: set[str]) -> None:
    """写后逐形状核对：触及形状匹配预期快照；未触及形状快照与字节切片都保持不变。"""
    old_shapes, _prefix = _parse_props(old_raw, part)
    new_shapes, _p2 = _parse_props(new_raw, part)
    if [s["sid"] for s in old_shapes] != [s["sid"] for s in new_shapes]:
        raise EditError("validation", part)
    for old_shape, new_shape in zip(old_shapes, new_shapes):
        sid = new_shape["sid"]
        if sid in touched:
            if _props_snapshot(new_shape) != expected.get(sid):
                raise EditError("validation", part)
        else:
            if _props_snapshot(old_shape) != _props_snapshot(new_shape):
                raise EditError("validation", part)
            if new_raw[new_shape["start"]:new_shape["stop"]] != old_raw[old_shape["start"]:old_shape["stop"]]:
                raise EditError("validation", part)



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
              checkers: dict[str, object]) -> None:
    """重开结果逐成员核对：未触及成员 CRC 与原值一致，改动成员字节交给部件级检查函数复核。"""
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
    for part, check in checkers.items():
        with zipfile.ZipFile(io.BytesIO(original)) as old:
            check(_member_bytes(old, part), changed[part])


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
              output: Path | None = None, dry_run: bool = False, ops: dict | None = None) -> dict:
    """公开入口：验证整组修改并写入（或预演），返回文件、备份、逐条修改与跳过原因；不改批注。

    batch/ids 走批注文本回写；ops 走白名单几何与样式回写（{"v": 1, "ops": [...]}），两者互斥。
    两条路径共用同一套源文件指纹、二进制快照与原子替换闭环，任一条失败整组不写。
    """
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
        raw = de._source_check(source, fingerprint)
        if ops is not None:
            if batch is not None or ids:
                raise EditError("arguments")
            model = _read_model(raw)
            changed, changes, warnings, checkers = _run_ops(raw, ops, model)
            pages = sorted({c["page"] for c in changes})
            result = {"source": str(source), "output": "", "dryRun": dry_run, "backup": None,
                      "ids": [c["id"] for c in changes], "pages": pages, "changes": changes,
                      "skipped": [], "warnings": warnings,
                      "visual": {"status": "notRun", "pages": pages}}
        else:
            edits, skipped = _selection(review, batch, ids, manifest,
                                        allow_notes=sources.get("script") is None)
            if not edits:
                raise EditError("empty")
            stale = [EditError("anchor", item=e["id"]) for e in edits
                     if e.get("anchor") and e["anchor"].get("fp") not in (None, "")
                     and e["anchor"]["fp"] != fingerprint]
            if stale:
                raise SelectionError(stale)
            model = _read_model(raw)
            changed, tasks, expected = _run_edits(raw, edits, model)
            checkers = {part: partial(_validate_part, part=part, mode=expected[part][0],
                                      expected=expected[part][1]) for part in changed}
            result = {"source": str(source), "output": "", "dryRun": dry_run,
                      "backup": None, "ids": [t.id for t in tasks],
                      "pages": sorted({t.page for t in tasks}),
                      "changes": [{"id": t.id, "kind": t.kind, "slide": t.slide, "page": t.page,
                                   "part": t.part, "para": t.para, "element": t.element} for t in tasks],
                      "skipped": skipped, "warnings": []}
        packed = raw
        for part, data in changed.items():
            packed = de.repack(packed, part, data)
        destination = Path(output).absolute() if output else source
        separate = destination != source
        if destination.is_symlink() or (separate and destination.exists()):
            raise EditError("output", str(destination))
        result["output"] = str(destination)
        if dry_run:
            _validate(io.BytesIO(packed), raw, changed, checkers)
            return result
        # 先留下可靠原件；后续写入、fsync、校验或 replace 失败都不删除备份。
        result["backup"] = str(de._backup(review, batch or 0, raw, writeback="pptx-writeback", suffix=".pptx"))
        fd, temp_name = tempfile.mkstemp(prefix="." + destination.name + "-", suffix=".tmp", dir=destination.parent)
        temp = Path(temp_name)
        try:
            with os.fdopen(fd, "wb") as fh:
                if not separate:
                    os.fchmod(fh.fileno(), stat.S_IMODE(source.stat().st_mode))
                fh.write(packed)
                fh.flush()
                os.fsync(fh.fileno())
            _validate(temp, raw, changed, checkers)
            de._source_check(source, fingerprint)   # PowerPoint 或别的进程在准备期间动过源文件就拒绝提交
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


def _load_ops_file(path: Path) -> dict:
    """读取操作文件（UTF-8 JSON，{"v": 1, "ops": [...]}）；解析失败按 ops 契约拒绝。"""
    try:
        text = Path(path).read_text(encoding="utf-8")
    except UnicodeDecodeError:
        raise EditError("ops", "utf-8") from None
    except OSError as exc:
        raise EditError("ops", str(exc)) from None
    try:
        spec = json.loads(text)
    except json.JSONDecodeError as exc:
        raise EditError("ops", "json " + str(exc)) from None
    if not isinstance(spec, dict):
        raise EditError("ops", "root")
    return spec


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
    ap.add_argument("command", choices=("apply", "ops"))
    ap.add_argument("review", type=Path)
    ap.add_argument("--batch", type=int, help=t("pptx.edit.batchHelp"))
    ap.add_argument("--id", action="append", dest="ids", help=t("pptx.edit.idHelp"))
    ap.add_argument("--ops", type=Path, help=t("pptx.ops.opsHelp"))
    ap.add_argument("--output", type=Path, help=t("pptx.edit.outputHelp"))
    ap.add_argument("--dry-run", action="store_true", help=t("pptx.edit.dryHelp"))
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--locale")
    a = ap.parse_args(argv)
    try:
        if a.command == "apply":
            if a.ops is not None:
                raise EditError("arguments")
            if a.batch is not None and a.batch < 1:
                raise EditError("arguments")
            if bool(a.ids) == (a.batch is not None):
                raise EditError("arguments")
            result = writeback(a.review, batch=a.batch, ids=a.ids, output=a.output, dry_run=a.dry_run)
        else:
            if a.batch is not None or a.ids or a.ops is None:
                raise EditError("arguments")
            result = writeback(a.review, output=a.output, dry_run=a.dry_run,
                               ops=_load_ops_file(a.ops))
        sep = ", " if language == "en" else "、"
        pages = sep.join(str(n) for n in result["pages"])
        if a.json:
            print(json.dumps(result, ensure_ascii=False))
        else:
            if a.command == "ops":
                for change in result["changes"]:
                    names = sep.join(p["name"] for p in change["props"])
                    print(t("pptx.ops.change", id=change["id"],
                            aspect=t("pptx.ops.aspect." + change["aspect"]), props=names))
                for warn in result["warnings"]:
                    detail = sep.join(warn["with"]) if warn.get("with") else warn.get("detail", "")
                    print(t("pptx.ops.warningLine", id=warn["id"],
                            text=t("pptx.ops.warning." + warn["code"], detail=detail)))
                if result.get("visual"):
                    print(t("pptx.ops.visual", pages=pages))
            print(t("pptx.ops.preview" if a.dry_run else "pptx.ops.done",
                    path=result["output"], pages=pages))
        if result["backup"] and not a.json:
            print(t("pptx.edit.backup", path=result["backup"]))
        return 0
    except (EditError, pr.PptxError, BuildError, OSError, ValueError, KeyError, zipfile.BadZipFile, zlib.error) as exc:
        errors = exc.errors if isinstance(exc, SelectionError) else [exc]
        for error in errors:
            code = error.code if isinstance(error, EditError) else "io"
            detail = error.detail if isinstance(error, EditError) else str(error)
            if code in ("readonly", "readonlyGeometry", "readonlyStyle"):
                detail = t("viewer.pptx.kind." + detail) if detail else detail
            message = t("pptx.edit.error." + code, detail=detail)
            print((f"{error.item}: " if isinstance(error, EditError) and error.item else "") + message, file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
