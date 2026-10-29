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
import zipfile

PART = re.compile(r"^ppt/(?:slides/slide\d+|slideMasters/slideMaster\d+|slideLayouts/slideLayout\d+|theme/theme\d+)\.xml$")
MAX_PART_BYTES = 8 * 1024 * 1024
MAX_TOTAL_BYTES = 64 * 1024 * 1024
MAX_PARTS = 3000
TOOL_TIMEOUT = 10


class CheckError(Exception):
    pass


def declared_fonts(path: Path) -> tuple[list[str], list[str]]:
    declared: set[str] = set()
    unresolved: set[str] = set()
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
                    elif face:
                        declared.add(face)
                    if element.tag.rsplit("}", 1)[-1] == "t" and (element.text or "").strip():
                        run = parents.get(element)
                        props = next((child for child in (list(run) if run is not None else [])
                                      if child.tag.rsplit("}", 1)[-1] == "rPr"), None)
                        if props is None or not any((child.attrib.get("typeface") or "").strip() for child in props.iter()):
                            unresolved.add("部分文本的字体继承未解析")
    except (OSError, zipfile.BadZipFile, RuntimeError, zipfile.LargeZipFile) as e:
        raise CheckError(f"无法读取 PPTX：{e}") from e
    return sorted(declared, key=str.casefold), sorted(unresolved, key=str.casefold)


def normal(name: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", name).casefold().split())


def installed_fonts() -> set[str]:
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
    return fonts


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="检查 PPTX 声明字体与本机字体枚举")
    ap.add_argument("pptx", type=Path, help="待转换的 PPTX 文件")
    args = ap.parse_args(argv)
    try:
        fonts, unresolved = declared_fonts(args.pptx)
    except CheckError as e:
        print(f"错误：{e}", file=sys.stderr)
        return 2
    error = None
    try:
        installed = installed_fonts()
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
    if unresolved:
        print("无法解析的字体引用或继承：" + "、".join(unresolved))
    if not fonts and not unresolved:
        print("未发现显式字体声明，继承的字体无法确认。")
    print("本机字体枚举不能证明 LibreOffice 实际使用了这些字体；未使用的主题声明不代表页面缺字。")
    print("转换前核查未检出或未知项；补齐字体后重新转换，并逐页检查换行、间距和遮挡。")
    if error:
        print(f"错误：{error}", file=sys.stderr)
        return 2
    return 1 if missing or unresolved or not fonts else 0


if __name__ == "__main__":
    sys.exit(main())
