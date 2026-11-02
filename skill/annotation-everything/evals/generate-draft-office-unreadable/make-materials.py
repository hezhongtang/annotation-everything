#!/usr/bin/env python3
"""生成「全部不可读」的合成办公材料（仅合成数据；只用 Python 标准库）。

产物写入参数指定的目录（缺省 materials/）：三份纯扫描 PDF（每页只有灰块，
没有任何文本操作符），宿主没有 OCR / 文本提取能力时读不到任何内容：
- scan-weekly-09.pdf   九月周报（扫描）
- scan-weekly-10.pdf   十月周报（扫描）
- scan-board-memo.pdf  董事会备忘录（扫描）

所有数字口径都是编造的合成设定，只用于技能评测，不代表任何真实业务。
"""
from __future__ import annotations

import sys
from pathlib import Path


def build_scanned_pdf() -> bytes:
    """每页只有灰块、无文本操作符的最小 PDF；文本提取结果为空。"""
    objs: list[bytes] = []

    def add(body: bytes) -> int:
        objs.append(body)
        return len(objs)

    catalog = add(b"")
    pages_obj = add(b"")
    stream = b"0.92 0.92 0.92 rg 0 0 612 792 re f 0 rg"
    page_ids = []
    for _ in range(2):
        cont = add(b"<< /Length " + str(len(stream)).encode() + b" >>\nstream\n"
                   + stream + b"\nendstream")
        page = add(b"<< /Type /Page /Parent " + str(pages_obj).encode() + b" 0 R "
                   b"/MediaBox [0 0 612 792] /Contents " + str(cont).encode()
                   + b" 0 R >>")
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


def main() -> int:
    out = Path(sys.argv[1] if len(sys.argv) > 1 else "materials")
    out.mkdir(parents=True, exist_ok=True)
    for name in ("scan-weekly-09.pdf", "scan-weekly-10.pdf", "scan-board-memo.pdf"):
        path = out / name
        path.write_bytes(build_scanned_pdf())
        print(f"wrote {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
