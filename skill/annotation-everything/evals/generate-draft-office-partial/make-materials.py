#!/usr/bin/env python3
"""生成「部分可读」的合成办公材料（仅合成数据；只用 Python 标准库）。

产物写入参数指定的目录（缺省 materials/）：
- board-deck.pdf     4 页 PDF：第 1–2 页有文本层；第 3–4 页只有图像块（无文本操作符），
                     没有文本提取/OCR 能力时读不到这两页
- ops-notes.docx     Word 正文可读，但成本数字只在「下图」里（正文不含成本数值）
- store-sales.xlsx   Sheet1 的 9 月列是数值；10 月列是公式且没有缓存结果（不重算读不到值）
- brief.md           汇报需求简报（需求六项齐全）

所有数字都是编造的合成口径，只用于技能评测，不代表任何真实业务。
"""
from __future__ import annotations

import io
import sys
import zipfile
from pathlib import Path
from xml.sax.saxutils import escape


def build_pdf(pages: list[list[str] | None]) -> bytes:
    """pages 元素为文本行列表（文本页）或 None（扫描页：只画灰块，无文本操作符）。"""
    objs: list[bytes] = []

    def add(body: bytes) -> int:
        objs.append(body)
        return len(objs)

    catalog = add(b"")
    pages_obj = add(b"")
    font = add(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")
    page_ids = []
    for lines in pages:
        if lines is None:
            stream = b"0.92 0.92 0.92 rg 0 0 612 792 re f 0 rg"
        else:
            content = []
            y = 760
            for line in lines:
                esc = line.replace("\\", r"\\").replace("(", r"\(").replace(")", r"\)")
                content.append(f"BT /F1 16 Tf 72 {y} Td ({esc}) Tj ET".encode("ascii"))
                y -= 30
            stream = b"\n".join(content)
        cont = add(b"<< /Length " + str(len(stream)).encode() + b" >>\nstream\n"
                   + stream + b"\nendstream")
        page = add(b"<< /Type /Page /Parent " + str(pages_obj).encode() + b" 0 R "
                   b"/MediaBox [0 0 612 792] /Contents " + str(cont).encode() + b" 0 R "
                   b"/Resources << /Font << /F1 " + str(font).encode() + b" 0 R >> >> >>")
        page_ids.append(page)
    objs[catalog - 1] = f"<< /Type /Catalog /Pages {pages_obj} 0 R >>".encode()
    kids = " ".join(f"{p} 0 R" for p in page_ids)
    objs[pages_obj - 1] = (f"<< /Type /Pages /Kids [{kids}] "
                           f"/Count {len(page_ids)} >>").encode()
    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for i, body in enumerate(objs, 1):
        offsets.append(len(out))
        out += f"{i} 0 obj\n".encode() + body + b"\nendobj\n"
    xref = len(out)
    out += f"xref\n0 {len(objs) + 1}\n".encode() + b"0000000000 65535 f \n"
    for off in offsets:
        out += f"{off:010d} 00000 n \n".encode()
    out += (f"trailer\n<< /Size {len(objs) + 1} /Root {catalog} 0 R >>\n"
            f"startxref\n{xref}\n%%EOF\n").encode()
    return bytes(out)


def build_docx(paragraphs: list[str]) -> bytes:
    ct = ("<?xml version=\"1.0\" encoding=\"UTF-8\" standalone=\"yes\"?>\n"
          "<Types xmlns=\"http://schemas.openxmlformats.org/package/2006/content-types\">"
          "<Default Extension=\"rels\" ContentType=\"application/vnd.openxmlformats-package.relationships+xml\"/>"
          "<Default Extension=\"xml\" ContentType=\"application/xml\"/>"
          "<Override PartName=\"/word/document.xml\" ContentType=\"application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml\"/>"
          "</Types>")
    rels = ("<?xml version=\"1.0\" encoding=\"UTF-8\" standalone=\"yes\"?>\n"
            "<Relationships xmlns=\"http://schemas.openxmlformats.org/package/2006/relationships\">"
            "<Relationship Id=\"rId1\" Type=\"http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument\" Target=\"word/document.xml\"/>"
            "</Relationships>")
    body = "".join(
        f"<w:p><w:r><w:t xml:space=\"preserve\">{escape(p)}</w:t></w:r></w:p>"
        for p in paragraphs)
    doc = ("<?xml version=\"1.0\" encoding=\"UTF-8\" standalone=\"yes\"?>\n"
           "<w:document xmlns:w=\"http://schemas.openxmlformats.org/wordprocessingml/2006/main\">"
           f"<w:body>{body}</w:body></w:document>")
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml", ct)
        z.writestr("_rels/.rels", rels)
        z.writestr("word/document.xml", doc)
    return buf.getvalue()


def build_xlsx(sheet_rows: list[list[str | None]]) -> bytes:
    ct = ("<?xml version=\"1.0\" encoding=\"UTF-8\" standalone=\"yes\"?>\n"
          "<Types xmlns=\"http://schemas.openxmlformats.org/package/2006/content-types\">"
          "<Default Extension=\"rels\" ContentType=\"application/vnd.openxmlformats-package.relationships+xml\"/>"
          "<Default Extension=\"xml\" ContentType=\"application/xml\"/>"
          "<Override PartName=\"/xl/workbook.xml\" ContentType=\"application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml\"/>"
          "<Override PartName=\"/xl/worksheets/sheet1.xml\" ContentType=\"application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml\"/>"
          "</Types>")
    rels = ("<?xml version=\"1.0\" encoding=\"UTF-8\" standalone=\"yes\"?>\n"
            "<Relationships xmlns=\"http://schemas.openxmlformats.org/package/2006/relationships\">"
            "<Relationship Id=\"rId1\" Type=\"http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument\" Target=\"xl/workbook.xml\"/>"
            "</Relationships>")
    wb = ("<?xml version=\"1.0\" encoding=\"UTF-8\" standalone=\"yes\"?>\n"
          "<workbook xmlns=\"http://schemas.openxmlformats.org/spreadsheetml/2006/main\" "
          "xmlns:r=\"http://schemas.openxmlformats.org/officeDocument/2006/relationships\">"
          "<sheets><sheet name=\"Sheet1\" sheetId=\"1\" r:id=\"rId1\"/></sheets></workbook>")
    wb_rels = ("<?xml version=\"1.0\" encoding=\"UTF-8\" standalone=\"yes\"?>\n"
               "<Relationships xmlns=\"http://schemas.openxmlformats.org/package/2006/relationships\">"
               "<Relationship Id=\"rId1\" Type=\"http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet\" Target=\"worksheets/sheet1.xml\"/>"
               "</Relationships>")
    rows_xml = []
    for r, row in enumerate(sheet_rows, 1):
        cells = []
        for c, cell in enumerate(row, 1):
            ref = f"{chr(64 + c)}{r}"
            if cell is None:
                continue
            if cell.startswith("s:"):
                cells.append(f"<c r=\"{ref}\" t=\"inlineStr\"><is><t>{escape(cell[2:])}</t></is></c>")
            elif cell.startswith("f:"):
                cells.append(f"<c r=\"{ref}\"><f>{escape(cell[2:])}</f></c>")
            else:
                cells.append(f"<c r=\"{ref}\"><v>{cell}</v></c>")
        rows_xml.append(f"<row r=\"{r}\">" + "".join(cells) + "</row>")
    sheet = ("<?xml version=\"1.0\" encoding=\"UTF-8\" standalone=\"yes\"?>\n"
             "<worksheet xmlns=\"http://schemas.openxmlformats.org/spreadsheetml/2006/main\">"
             "<sheetData>" + "".join(rows_xml) + "</sheetData></worksheet>")
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml", ct)
        z.writestr("_rels/.rels", rels)
        z.writestr("xl/workbook.xml", wb)
        z.writestr("xl/_rels/workbook.xml.rels", wb_rels)
        z.writestr("xl/worksheets/sheet1.xml", sheet)
    return buf.getvalue()


BRIEF = """# 汇报需求（合成）

> 合成数据，仅用于评测。这份简报回答了需求六项。

- **用途**：季度经营汇报会。
- **读者**：公司管理层。
- **核心问题**：四季度增长能不能持续（需要 10 月经营情况支撑）。
- **篇幅**：3–4 页。
- **材料范围**：materials/ 下的三份文件（董事会材料、运营笔记、门店销售表，全部）。
- **讲稿**：不需要。
"""


def main() -> int:
    out = Path(sys.argv[1] if len(sys.argv) > 1 else "materials")
    out.mkdir(parents=True, exist_ok=True)
    files = {
        "board-deck.pdf": build_pdf([
            ["Q3 Board Deck (synthetic data)",
             "Q3 revenue: 1.12M CNY, up 12% from 1.00M last year.",
             "Two new stores opened in Q3."],
            ["Outlook (text layer)",
             "Growth depends on October performance.",
             "October and cost details follow in scanned exhibits."],
            None,   # 第 3 页：十月经营数据（扫描，无文本层）
            None,   # 第 4 页：成本结构（扫描，无文本层）
        ]),
        "ops-notes.docx": build_docx([
            "四季度运营笔记（合成数据）",
            "两家新店装修进度 60%，预计 11 月中旬开业。",
            "10 月起周末延长营业 1 小时。",
            "成本情况见下图（成本结构图），图中含咖啡豆与人力占比的具体数字。",
            "咖啡豆锁价谈判仍在进行，结果待定。",
        ]),
        "store-sales.xlsx": build_xlsx([
            ["s:门店", "s:9 月销售（万元）", "s:10 月销售（万元）"],
            ["s:门店A", "30", "f:Sheet3!B2*1.1"],
            ["s:门店B", "18", "f:Sheet3!B3*1.1"],
            [None, None, None],
            ["s:注：10 月列为公式，引用的 Sheet3 在另一份工作簿里，本文件没有其数据。", None, None],
        ]),
        "brief.md": BRIEF,
    }
    for name, data in files.items():
        path = out / name
        if isinstance(data, bytes):
            path.write_bytes(data)
        else:
            path.write_text(data, encoding="utf-8")
        print(f"wrote {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
