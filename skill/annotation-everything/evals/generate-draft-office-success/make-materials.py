#!/usr/bin/env python3
"""生成合成办公材料（仅合成数据；只用 Python 标准库）。

产物写入参数指定的目录（缺省 materials/）：
- q3-review.pdf       两页文本层 PDF（ASCII 文本，任何文本提取器都能读）
- sales-plan.docx     最小 OOXML Word 文档（正文段落）
- channel-sales.xlsx  最小 OOXML 表格（Sheet1，含空白单元格与数值零）
- brief.md            汇报需求简报（需求六项齐全）

所有数字都是编造的合成口径，只用于技能评测，不代表任何真实业务。
"""
from __future__ import annotations

import io
import sys
import zipfile
from pathlib import Path
from xml.sax.saxutils import escape


def build_pdf(pages: list[list[str]]) -> bytes:
    """最小文本层 PDF：每页若干行 ASCII 文本，带正确 xref。"""
    objs: list[bytes] = []

    def add(body: bytes) -> int:
        objs.append(body)
        return len(objs)

    catalog = add(b"")
    pages_obj = add(b"")
    font = add(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")
    page_ids = []
    for lines in pages:
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
    """cell 取值：以 `s:` 开头按内联字符串，`f:` 开头按公式，其余按数字；None 为空白。"""
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
- **核心问题**：三季度收入为什么增长；四季度扩张计划能不能落地。
- **篇幅**：4 页左右。
- **材料范围**：materials/ 下的三份文件（PDF 回顾、Word 计划、Excel 渠道表，全部）。
- **讲稿**：不需要。
"""


def main() -> int:
    out = Path(sys.argv[1] if len(sys.argv) > 1 else "materials")
    out.mkdir(parents=True, exist_ok=True)
    files = {
        "q3-review.pdf": build_pdf([
            ["Q3 Business Review (synthetic data)",
             "Q3 revenue: 1.12M CNY. Same period last year: 1.00M CNY.",
             "Net margin: 14%. No one-off items this quarter."],
            ["Growth drivers (synthetic data)",
             "3 new stores opened in Q3, adding 0.09M CNY revenue.",
             "Delivery channel share: 38% of Q3 revenue.",
             "Coffee bean cost rose 9% year on year."],
        ]),
        "sales-plan.docx": build_docx([
            "四季度销售计划（合成数据）",
            "一、新开两家门店：静安嘉里中心店与虹桥天地店，计划 11 月中旬开业。",
            "二、会员储值目标 50 万元，重点放在企业客户。",
            "三、咖啡豆采购锁价至明年 3 月，锁价量覆盖四季度用量的 80%。",
            "四、外卖渠道维持 35% 以上的占比目标。",
        ]),
        "channel-sales.xlsx": build_xlsx([
            ["s:区域", "s:9 月销售（万元）", "s:10 月销售（万元）"],
            ["s:华东", "38", "0"],
            ["s:华北", "21", None],
            ["s:华南", "12", "5"],
            [None, None, None],
            ["s:注：空白表示尚未统计；0 表示确认为零。", None, None],
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
