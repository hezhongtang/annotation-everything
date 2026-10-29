#!/usr/bin/env python3
"""对照 PPTX 字体声明与本机字体枚举；不修改或解压源文件。"""
from __future__ import annotations

import argparse
from pathlib import Path
import re
import subprocess
import sys
import unicodedata
import xml.etree.ElementTree as ET
import zlib
import zipfile

PART = re.compile(r"^ppt/(?:slides/slide\d+|slideMasters/slideMaster\d+|slideLayouts/slideLayout\d+|theme/theme\d+)\.xml$")
MAX_PART_BYTES = 8 * 1024 * 1024
MAX_TOTAL_BYTES = 64 * 1024 * 1024
MAX_PARTS = 3000
TOOL_TIMEOUT = 10


class CheckError(Exception):
    pass


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


# 复杂文种（cs 槽）：希伯来、阿拉伯、叙利亚、南亚、东南亚文字及阿拉伯/希伯来表现形式
_CS_RANGES = ((0x0590, 0x10FF), (0x1780, 0x18AF), (0xFB1D, 0xFDFF), (0xFE70, 0xFEFF))
# 不占东亚/复杂文种槽：拉丁扩展与通用标点符号、私用区（常配 <a:sym>）、变体选择符、emoji
_NO_SLOT_RANGES = ((0x1D00, 0x2E7F), (0xE000, 0xF8FF), (0xFE00, 0xFE0F), (0x1F000, 0x1FAFF), (0xF0000, 0x10FFFF))


def _theme_slots(text: str) -> set[str]:
    """返回文本会用到的主题字体槽：ea（东亚）或 cs（复杂文种）；拉丁、希腊、西里尔不计。"""
    slots = set()
    for ch in text:
        o = ord(ch)
        if o < 0x0590 or any(lo <= o <= hi for lo, hi in _NO_SLOT_RANGES):
            continue
        slots.add("cs" if any(lo <= o <= hi for lo, hi in _CS_RANGES) else "ea")
    return slots


def declared_fonts(path: Path) -> tuple[list[str], list[str], list[str]]:
    """返回（显式声明字体, 无法解析项, 主题按文字脚本的备选字体）。"""
    declared: set[str] = set()
    unresolved: set[str] = set()
    fallbacks: set[str] = set()
    try:
        with zipfile.ZipFile(path) as archive:
            parts = [info for info in archive.infolist() if PART.fullmatch(info.filename)]
            if not parts:
                raise CheckError("PPTX 中没有可读取的幻灯片、母版、布局或主题 XML")
            if len(parts) > MAX_PARTS or sum(info.file_size for info in parts) > MAX_TOTAL_BYTES:
                raise CheckError("PPTX 字体相关 XML 过多或过大")
            for info in parts:
                if info.file_size > MAX_PART_BYTES:
                    raise CheckError(f"PPTX 中的 {info.filename} 过大")
                data = archive.read(info)
                if b"\x00" in data:
                    raise CheckError(f"PPTX 中的 {info.filename} 包含不支持的 XML 编码")
                if b"<!DOCTYPE" in data.upper() or b"<!ENTITY" in data.upper():
                    raise CheckError(f"PPTX 中的 {info.filename} 包含不支持的 XML 声明")
                try:
                    xml = ET.fromstring(data)
                except ET.ParseError as e:
                    raise CheckError(f"PPTX 中的 {info.filename} 无法解析：{e}") from e
                parents = {child: parent for parent in xml.iter() for child in parent}
                for element in xml.iter():
                    face = (element.attrib.get("typeface") or "").strip()
                    if face.startswith("+"):
                        unresolved.add(face)
                    elif face and _local(element.tag) == "font" and element.attrib.get("script"):
                        # 主题字体方案里的 <a:font script=…>：只在对应文字出现且未另行指定时才用
                        fallbacks.add(face)
                    elif face:
                        declared.add(face)
                    if _local(element.tag) == "t" and (element.text or "").strip():
                        run = parents.get(element)
                        props = next((child for child in (list(run) if run is not None else [])
                                      if _local(child.tag) == "rPr"), None)
                        faces = {_local(child.tag): (child.attrib.get("typeface") or "").strip()
                                 for child in (props.iter() if props is not None else ())}
                        if not any(faces.values()):
                            unresolved.add("部分文本的字体继承未解析")
                        elif any(not faces.get(slot) for slot in _theme_slots(element.text)):
                            unresolved.add("部分非拉丁文字依赖主题的东亚或复杂文种字体")
    except (OSError, zipfile.BadZipFile, RuntimeError, zipfile.LargeZipFile, NotImplementedError, EOFError, zlib.error) as e:
        raise CheckError(f"无法读取 PPTX：{e}") from e
    by_name = lambda names: sorted(names, key=str.casefold)  # noqa: E731
    return by_name(declared), by_name(unresolved), by_name(fallbacks - declared)


def normal(name: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", name).casefold().split())


def installed_fonts() -> tuple[set[str], bool]:
    try:
        result = subprocess.run(["fc-list", "--format=%{family}\\n"], capture_output=True, text=True,
                                errors="replace", timeout=TOOL_TIMEOUT, check=False)
    except FileNotFoundError as e:
        raise CheckError("找不到 fc-list，无法枚举字体；请安装 fontconfig 后重试") from e
    except (OSError, subprocess.TimeoutExpired) as e:
        raise CheckError(f"字体枚举失败或超时：{e}") from e
    if result.returncode != 0:
        raise CheckError("fc-list 字体枚举失败，请检查工具和本机字体配置")
    fonts = {normal(name) for line in result.stdout.splitlines() for name in line.split(",") if name.strip()}
    if not fonts:
        raise CheckError("fc-list 字体枚举为空，无法确认本机字体")
    return fonts, bool(result.stderr.strip())


def pdf_fonts(path: Path) -> tuple[list[dict[str, str]], bool]:
    if not path.is_file():
        raise CheckError("PDF 文件不存在或无法读取")
    try:
        result = subprocess.run(["pdffonts", str(path.resolve())], capture_output=True, text=True,
                                errors="replace", timeout=TOOL_TIMEOUT, check=False)
    except FileNotFoundError as e:
        raise CheckError("找不到 pdffonts，无法检查 PDF 字体；请安装 poppler 后重试") from e
    except (OSError, subprocess.TimeoutExpired) as e:
        raise CheckError(f"PDF 字体检查失败或超时：{e}") from e
    if result.returncode != 0:
        raise CheckError("pdffonts 无法读取 PDF，请检查文件和工具")
    lines = result.stdout.splitlines()
    if len(lines) < 2 or not lines[0].lstrip().startswith("name"):
        raise CheckError("无法识别 pdffonts 的输出，请手工核查")
    cols = list(re.finditer(r"-+", lines[1]))
    if len(cols) < 4:
        raise CheckError("无法识别 pdffonts 的表头，请手工核查")
    rows = []
    for raw in lines[2:]:
        raw = "".join(ch if ch == "\t" or ord(ch) >= 32 and ord(ch) != 127 else " " for ch in raw)
        if not raw.strip():
            continue
        status = re.search(r"\s+(yes|no)\s+(yes|no)\s+(yes|no)\s+\d+\s+\d+\s*$", raw, re.I)
        name_end, type_start, type_end, enc_start, enc_end = cols[0].end(), cols[1].start(), cols[1].end(), cols[2].start(), cols[2].end()
        if (not status or len(raw) <= type_start or raw[name_end:type_start].strip()
                or raw[type_end:enc_start].strip() or raw[enc_end:cols[3].start()].strip()):
            rows.append({"raw": raw.strip()})
            continue
        rows.append({"name": raw[:name_end].strip(), "type": raw[type_start:type_end].strip(),
                     "encoding": raw[enc_start:enc_end].strip(), "emb": status.group(1).lower(),
                     "sub": status.group(2).lower(), "uni": status.group(3).lower()})
    return rows, bool(result.stderr.strip())


def report_pdf(rows: list[dict[str, str]], declared: list[str], fallbacks: list[str] = ()) -> bool:
    """输出 PDF 证据；返回是否需要人工核查。主题备选字体可用于对应名称，但不要求出现。"""
    print("PDF 字体证据（pdffonts）：")
    if not rows:
        print("- 无可枚举字体资源；可能是图片或轮廓化文字，待人工核查。")
        return True
    declared_by_name = {normal(name): name for name in [*declared, *fallbacks]}
    matched: set[str] = set()
    review = False
    for row in rows:
        if "raw" in row:
            print(f"- {row['raw']}：输出列无法确认，待人工核查")
            review = True
            continue
        name = row["name"]
        base = re.sub(r"^[A-Z]{6}\+", "", name) if row["sub"] == "yes" else name
        key = normal(base)
        known = key in declared_by_name
        if known:
            matched.add(key)
        if not known or row["emb"] != "yes":
            review = True
        verdict = "按名称对应声明" if known and row["emb"] == "yes" else "待人工核查"
        print(f"- {name} | type={row['type']} | encoding={row['encoding']} | emb={row['emb']} | sub={row['sub']} | uni={row['uni']} | {verdict}")
    unmatched = [name for name in declared if normal(name) not in matched]
    if unmatched:
        print("未按名称对应的 PPTX 声明：" + "、".join(unmatched) + "；可能未使用、名称不同或被替换，待人工核查。")
        review = True
    print("名称一致也不能证明所有字形未被替换；别名、样式、轮廓化和图片内容都需要人工核查。")
    return review


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="检查 PPTX 声明字体与本机字体枚举")
    ap.add_argument("pptx", type=Path, help="待转换的 PPTX 文件")
    ap.add_argument("--pdf", type=Path, help="可选：转换后的 PDF，使用 pdffonts 列出字体证据")
    args = ap.parse_args(argv)
    try:
        fonts, unresolved, fallbacks = declared_fonts(args.pptx)
    except CheckError as e:
        print(f"错误：{e}", file=sys.stderr)
        return 2
    error = None
    font_warning = False
    try:
        installed, font_warning = installed_fonts()
    except CheckError as e:
        installed, error = None, e
    print("PPTX 字体声明：")
    missing = []
    for font in fonts:
        found = installed is not None and normal(font) in installed
        if installed is not None and not found:
            missing.append(font)
        result = "无法确认" if installed is None else ("本次枚举找到" if found else "本次枚举未检出（可能未使用）")
        print(f"- {font}：{result}")
    if fallbacks:
        head = f"主题按文字脚本的备选字体 {len(fallbacks)} 项（仅在对应文字出现且未另行指定字体时使用，不计入未检出）："
        if installed is None:
            print(head + "无法确认")
        else:
            absent = [font for font in fallbacks if normal(font) not in installed]
            print(head + (f"本次枚举未检出 {len(absent)} 项：" + "、".join(absent) if absent else "本次枚举全部找到"))
    if unresolved:
        print("无法解析的字体引用或继承：" + "、".join(unresolved))
    if not fonts and not unresolved:
        print("未发现显式字体声明，继承的字体无法确认。")
    print("本机字体枚举不能证明 LibreOffice 实际使用了这些字体；未使用的主题声明不代表页面缺字。")
    print("转换前核查未检出或未知项；补齐字体后重新转换，并逐页检查换行、间距和遮挡。")
    pdf_review = False
    pdf_error = None
    pdf_warning = False
    if args.pdf:
        try:
            rows, pdf_warning = pdf_fonts(args.pdf)
            pdf_review = report_pdf(rows, fonts, fallbacks)
        except CheckError as e:
            pdf_error = e
    if font_warning:
        print("警告：字体枚举工具输出警告，结果可能不完整，待人工核查。", file=sys.stderr)
    if pdf_warning:
        print("警告：PDF 字体工具输出警告，结果可能不完整，待人工核查。", file=sys.stderr)
    if error:
        print(f"错误：{error}", file=sys.stderr)
    if pdf_error:
        print(f"错误：{pdf_error}", file=sys.stderr)
    if error or pdf_error:
        return 2
    return 1 if missing or unresolved or not fonts or pdf_review or font_warning or pdf_warning else 0


if __name__ == "__main__":
    sys.exit(main())
