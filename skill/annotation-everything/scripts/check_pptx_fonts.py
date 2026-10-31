#!/usr/bin/env python3
"""对照 PPTX 字体声明与本机字体枚举；不修改或解压源文件。

语言：`--locale auto|zh-CN|en` 优先于环境变量 AE_LOCALE；都没有则中文，只有 auto 才读
LC_ALL / LC_MESSAGES / LANG。字体名、来源路径与系统原始错误保持原文，只补本地化上下文。
"""
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

sys.path.insert(0, str(Path(__file__).resolve().parent))
import i18n  # noqa: E402

PART = re.compile(r"^ppt/(?:slides/slide\d+|slideMasters/slideMaster\d+|slideLayouts/slideLayout\d+|theme/theme\d+)\.xml$")
MAX_PART_BYTES = 8 * 1024 * 1024
MAX_TOTAL_BYTES = 64 * 1024 * 1024
MAX_PARTS = 3000
TOOL_TIMEOUT = 10

# 当前界面语言：main() 按共同契约解析后设置；未设置前保持中文（旧调用行为不变）
_LOCALE = i18n.DEFAULT_LOCALE


def T(key: str, **params: object) -> str:
    """按当前语言取一条消息；缺译时回退另一种语言（见 locales/README.md 的契约）。"""
    return i18n.Translator(_LOCALE)(key, **params)


def set_locale(locale: str) -> None:
    global _LOCALE
    _LOCALE = locale


def explicit_locale(argv) -> object:
    """从命令行里取出 --locale 的值；没写返回 None。"""
    argv = list(argv or [])
    for index, token in enumerate(argv):
        if token == "--":
            break
        if token == "--locale":
            return argv[index + 1] if index + 1 < len(argv) else ""
        if token.startswith("--locale="):
            return token.partition("=")[2]
    return None


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
                raise CheckError(T("font.err.pptxNone"))
            if len(parts) > MAX_PARTS or sum(info.file_size for info in parts) > MAX_TOTAL_BYTES:
                raise CheckError(T("font.err.pptxTooBig"))
            for info in parts:
                if info.file_size > MAX_PART_BYTES:
                    raise CheckError(T("font.err.partTooBig", name=info.filename))
                data = archive.read(info)
                if b"\x00" in data:
                    raise CheckError(T("font.err.partEncoding", name=info.filename))
                if b"<!DOCTYPE" in data.upper() or b"<!ENTITY" in data.upper():
                    raise CheckError(T("font.err.partDoctype", name=info.filename))
                try:
                    xml = ET.fromstring(data)
                except ET.ParseError as e:
                    raise CheckError(T("font.err.partParse", name=info.filename, detail=e)) from e
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
                            unresolved.add(T("font.unresolved.inherit"))
                        elif any(not faces.get(slot) for slot in _theme_slots(element.text)):
                            unresolved.add(T("font.unresolved.slot"))
    except (OSError, zipfile.BadZipFile, RuntimeError, zipfile.LargeZipFile, NotImplementedError, EOFError, zlib.error) as e:
        raise CheckError(T("font.err.pptxUnreadable", detail=e)) from e
    by_name = lambda names: sorted(names, key=str.casefold)  # noqa: E731
    return by_name(declared), by_name(unresolved), by_name(fallbacks - declared)


def normal(name: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", name).casefold().split())


def installed_fonts() -> tuple[set[str], bool]:
    try:
        result = subprocess.run(["fc-list", "--format=%{family}\\n"], capture_output=True, text=True,
                                errors="replace", timeout=TOOL_TIMEOUT, check=False)
    except FileNotFoundError as e:
        raise CheckError(T("font.err.noFcList")) from e
    except (OSError, subprocess.TimeoutExpired) as e:
        raise CheckError(T("font.err.fcListFailed", detail=e)) from e
    if result.returncode != 0:
        raise CheckError(T("font.err.fcListExit"))
    fonts = {normal(name) for line in result.stdout.splitlines() for name in line.split(",") if name.strip()}
    if not fonts:
        raise CheckError(T("font.err.fcListEmpty"))
    return fonts, bool(result.stderr.strip())


def pdf_fonts(path: Path) -> tuple[list[dict[str, str]], bool]:
    if not path.is_file():
        raise CheckError(T("font.err.pdfMissing"))
    try:
        result = subprocess.run(["pdffonts", str(path.resolve())], capture_output=True, text=True,
                                errors="replace", timeout=TOOL_TIMEOUT, check=False)
    except FileNotFoundError as e:
        raise CheckError(T("font.err.noPdffonts")) from e
    except (OSError, subprocess.TimeoutExpired) as e:
        raise CheckError(T("font.err.pdfFailed", detail=e)) from e
    if result.returncode != 0:
        raise CheckError(T("font.err.pdfUnreadable"))
    lines = result.stdout.splitlines()
    if len(lines) < 2 or not lines[0].lstrip().startswith("name"):
        raise CheckError(T("font.err.pdfHeader"))
    cols = list(re.finditer(r"-+", lines[1]))
    if len(cols) < 4:
        raise CheckError(T("font.err.pdfColumns"))
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
    print(T("font.pdfTitle"))
    if not rows:
        print(T("font.pdfEmpty"))
        return True
    declared_by_name = {normal(name): name for name in [*declared, *fallbacks]}
    matched: set[str] = set()
    review = False
    for row in rows:
        if "raw" in row:
            print(T("font.pdfRaw", raw=row['raw']))
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
        verdict = T("font.pdfVerdict.known" if known and row["emb"] == "yes" else "font.pdfVerdict.review")
        print(T("font.pdfRow", name=name, type=row['type'], encoding=row['encoding'],
                emb=row['emb'], sub=row['sub'], uni=row['uni'], verdict=verdict))
    unmatched = [name for name in declared if normal(name) not in matched]
    if unmatched:
        print(T("font.pdfUnmatched", names=T("cli.ae.listSeparator").join(unmatched)))
        review = True
    print(T("font.caveat"))
    return review


class LocalizedParser(argparse.ArgumentParser):
    """帮助、用法与错误消息都走当前语言；参数名与合法取值保持原文。"""

    def error(self, message: str):
        self.print_usage(sys.stderr)
        text = str(message).strip()
        match = re.match(r"^argument (\S+): expected one argument$", text)
        if match:
            print(T("font.err.missingValue", name=match.group(1)), file=sys.stderr)
        elif text.startswith("the following arguments are required:"):
            print(T("font.err.missingArgument"), file=sys.stderr)
        elif text.startswith("unrecognized arguments:"):
            print(T("font.err.unknownArgument",
                    detail=text.partition(":")[2].strip()), file=sys.stderr)
        else:
            print(T("font.err.generic", detail=text), file=sys.stderr)
        sys.exit(2)


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    # 先按共同契约定语言，再建解析器：帮助、参数说明与错误消息才能用同一种语言
    try:
        set_locale(i18n.cli_locale(explicit_locale(argv)))
    except i18n.LocaleError as exc:
        set_locale(i18n.DEFAULT_LOCALE)
        params = getattr(exc, "params", {}) or {}
        value = params.get("locale") if isinstance(params, dict) else None
        if isinstance(value, str) and not value.strip():
            print(T("font.err.emptyLocale"), file=sys.stderr)
        else:
            print(T("font.err.invalidLocale", name="" if value is None else str(value)), file=sys.stderr)
        return 2
    ap = LocalizedParser(description=T("font.usage"), add_help=False)
    ap.add_argument("-h", "--help", action="help", default=argparse.SUPPRESS, help=T("font.opt.help"))
    ap.add_argument("--locale", metavar="LANG", help=T("font.opt.locale"))
    ap.add_argument("pptx", type=Path, help=T("font.arg.pptx"))
    ap.add_argument("--pdf", type=Path, help=T("font.arg.pdf"))
    args = ap.parse_args(argv)
    try:
        fonts, unresolved, fallbacks = declared_fonts(args.pptx)
    except CheckError as e:
        print(T("font.error", detail=e), file=sys.stderr)
        return 2
    error = None
    font_warning = False
    try:
        installed, font_warning = installed_fonts()
    except CheckError as e:
        installed, error = None, e
    print(T("font.declaredTitle"))
    missing = []
    for font in fonts:
        found = installed is not None and normal(font) in installed
        if installed is not None and not found:
            missing.append(font)
        result = (T("font.declared.unknown") if installed is None
                  else T("font.declared.found" if found else "font.declared.missing"))
        print(T("font.declaredRow", font=font, result=result))
    if fallbacks:
        head = T("font.themeHead", count=len(fallbacks))
        if installed is None:
            print(head + T("font.themeUnknown"))
        else:
            absent = [font for font in fallbacks if normal(font) not in installed]
            print(head + (T("font.themeMissing", count=len(absent),
                            names=T("font.listSeparator").join(absent)) if absent
                          else T("font.themeAllFound")))
    if unresolved:
        print(T("font.unresolvedHead", names=T("font.listSeparator").join(unresolved)))
    if not fonts and not unresolved:
        print(T("font.noDeclared"))
    print(T("font.caveat2"))
    print(T("font.next"))
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
        print(T("font.warn"), file=sys.stderr)
    if pdf_warning:
        print(T("font.warnPdf"), file=sys.stderr)
    if error:
        print(T("font.error", detail=error), file=sys.stderr)
    if pdf_error:
        print(T("font.error", detail=pdf_error), file=sys.stderr)
    if error or pdf_error:
        return 2
    return 1 if missing or unresolved or not fonts or pdf_review or font_warning or pdf_warning else 0


if __name__ == "__main__":
    sys.exit(main())
