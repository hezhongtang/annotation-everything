#!/usr/bin/env python3
"""生成 DOCX 审阅用的合成样例（全部内容虚构，只用标准库）。

默认产物 sample.docx 模拟 Word 保存后的文件：带 w:lastRenderedPageBreak 渲染分页与 w14:paraId，
共 5 页——A4 纵向 4 页（长条款段内跨页、带重复表头的跨页表格、高亮、删除线、原生修订、
Word 批注、文本框、域、页眉页脚）加 1 页 A4 横向附件。

  python3 fixtures/docx/make_sample.py [输出路径] [--no-rendered] [--no-para-ids]

--no-rendered 去掉渲染分页标记（模拟非 Word 工具导出的文件），--no-para-ids 去掉段落 id。
"""
from __future__ import annotations

import argparse
import zipfile
from pathlib import Path
from xml.sax.saxutils import escape

NS = ('xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main" '
      'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships" '
      'xmlns:w14="http://schemas.microsoft.com/office/word/2010/wordml" '
      'xmlns:mc="http://schemas.openxmlformats.org/markup-compatibility/2006" '
      'xmlns:v="urn:schemas-microsoft-com:vml" mc:Ignorable="w14"')
FIXED_TIME = (2026, 1, 1, 0, 0, 0)
LONG = ("乙方应按照本协议约定的时间、地点和质量标准交付合成示例货物，并提供必要的安装指导与培训服务。"
        "交付前乙方须完成出厂检验，检验记录随货提供；甲方在收货后十个工作日内完成验收，逾期未提出书面异议的视为验收合格。")


class Doc:
    def __init__(self, rendered: bool, para_ids: bool):
        self.rendered, self.para_ids, self.n = rendered, para_ids, 0

    def pid(self) -> str:
        self.n += 1
        return f' w14:paraId="{0x1A000000 + self.n:08X}" w14:textId="77777777"' if self.para_ids else ""

    def lrpb(self) -> str:
        return "<w:lastRenderedPageBreak/>" if self.rendered else ""

    @staticmethod
    def r(text: str, rpr: str = "") -> str:
        return f'<w:r>{f"<w:rPr>{rpr}</w:rPr>" if rpr else ""}<w:t xml:space="preserve">{escape(text)}</w:t></w:r>'

    def p(self, runs: str, ppr: str = "") -> str:
        return f'<w:p{self.pid()}>{f"<w:pPr>{ppr}</w:pPr>" if ppr else ""}{runs}</w:p>'

    def h(self, text: str, level: int = 1, page_start: bool = False) -> str:
        lead = f"<w:r>{self.lrpb()}</w:r>" if page_start and self.rendered else ""
        return self.p(lead + self.r(text), f'<w:pStyle w:val="Heading{level}"/>')

    def cell(self, text: str, width: int, extra: str = "", rpr: str = "") -> str:
        return (f'<w:tc><w:tcPr><w:tcW w:w="{width}" w:type="dxa"/>{extra}</w:tcPr>'
                f'{self.p(self.r(text, rpr))}</w:tc>')


def document(d: Doc) -> str:
    body = []
    # ---- 第 1 页 ----
    body.append(d.p(d.r("合成采购框架协议（示例）"), '<w:pStyle w:val="Title"/><w:jc w:val="center"/>'))
    body.append(d.p(d.r("本协议为测试用合成文本，") + d.r("关键条款已用黄色高亮", '<w:highlight w:val="yellow"/>')
                    + d.r("，作废表述保留为") + d.r("删除线文字", "<w:strike/>") + d.r("。")))
    body.append(d.h("一、交付与验收"))
    for k in range(1, 4):
        body.append(d.p(d.r(f"第 {k} 条示例条款：" + LONG), '<w:numPr><w:ilvl w:val="0"/><w:numId w:val="1"/></w:numPr>'))
    # 长条款：在段落中间跨到第 2 页
    body.append(d.p(d.r("第 4 条长条款：" + LONG * 2) + f"<w:r>{d.lrpb()}<w:t xml:space=\"preserve\">"
                    + escape("（续）本条在第 2 页继续：乙方对交付货物提供二十四个月质保，质保期内免费维修或更换。")
                    + "</w:t></w:r>"))
    # ---- 第 2 页 ----
    body.append(d.h("二、价格条款"))
    body.append(d.p(d.r("价格明细见下表，单位为合成货币：")))
    hdr = '<w:tr><w:trPr><w:tblHeader/></w:trPr>' + d.cell("项目", 3000, '<w:shd w:val="clear" w:fill="D9E2F3"/>', "<w:b/>") \
        + d.cell("单价", 2000, '<w:shd w:val="clear" w:fill="D9E2F3"/>', "<w:b/>") \
        + d.cell("说明", 4000, '<w:shd w:val="clear" w:fill="D9E2F3"/>', "<w:b/>") + "</w:tr>"
    rows = [hdr]
    for k in range(1, 6):
        note = d.cell("含运费", 4000, "", '<w:highlight w:val="green"/>') if k == 2 else d.cell(f"合成说明 {k}", 4000)
        if k == 4 and d.rendered:   # 第 4 行起到第 3 页
            first = (f'<w:tc><w:tcPr><w:tcW w:w="3000" w:type="dxa"/></w:tcPr>'
                     f'{d.p("<w:r><w:lastRenderedPageBreak/></w:r>" + d.r(f"示例货物 {k}"))}</w:tc>')
        else:
            first = d.cell(f"示例货物 {k}", 3000)
        rows.append("<w:tr>" + first + d.cell(f"{k * 100}.00", 2000) + note + "</w:tr>")
    body.append('<w:tbl><w:tblPr><w:tblStyle w:val="TableGrid"/><w:tblW w:w="9000" w:type="dxa"/></w:tblPr>'
                '<w:tblGrid><w:gridCol w:w="3000"/><w:gridCol w:w="2000"/><w:gridCol w:w="4000"/></w:tblGrid>'
                + "".join(rows) + "</w:tbl>")
    # ---- 第 3 页（接表格） ----
    body.append(d.p(d.r("付款方式：")
                    + '<w:del w:id="1" w:author="合成审阅人" w:date="2026-01-02T00:00:00Z"><w:r><w:delText>货到付款</w:delText></w:r></w:del>'
                    + '<w:ins w:id="2" w:author="合成审阅人" w:date="2026-01-02T00:00:00Z"><w:r><w:t>验收合格后三十日内付款</w:t></w:r></w:ins>'
                    + d.r("。")))
    body.append(d.p('<w:commentRangeStart w:id="0"/>' + d.r("违约金按未履行部分的百分之五计算。")
                    + '<w:commentRangeEnd w:id="0"/><w:r><w:commentReference w:id="0"/></w:r>'))
    body.append(d.p(d.r("提示框：")
                    + '<w:r><w:pict><v:shape style="width:200pt;height:40pt"><v:textbox><w:txbxContent>'
                    + d.p(d.r("文本框内的合成提示文字"))
                    + "</w:txbxContent></v:textbox></v:shape></w:pict></w:r>"))
    body.append(d.p(d.r("签订日期：") + '<w:fldSimple w:instr=" DATE \\@ &quot;yyyy-MM-dd&quot; ">'
                    + d.r("2026-01-01") + "</w:fldSimple>"))
    body.append(d.p(d.r("补充约定：双方另行签署的补充协议与本协议具有同等效力。")
                    + '<w:r><w:br w:type="page"/></w:r>'))
    # ---- 第 4 页 ----
    body.append(d.h("三、签署", page_start=True))
    body.append(d.p(d.r("甲方（盖章）：合成甲方有限公司　　乙方（盖章）：合成乙方有限公司")))
    portrait = ('<w:sectPr><w:headerReference w:type="default" r:id="rIdHeader"/>'
                '<w:footerReference w:type="default" r:id="rIdFooter"/>'
                '<w:pgSz w:w="11906" w:h="16838"/>'
                '<w:pgMar w:top="1440" w:right="1800" w:bottom="1440" w:left="1800" w:header="851" w:footer="992" w:gutter="0"/>'
                "</w:sectPr>")
    body.append(d.p(d.r("（以下无正文）"), portrait))
    # ---- 第 5 页：横向附件 ----
    body.append(d.h("附件：价格明细（横向）", page_start=True))
    body.append(d.p(d.r("横向页面用于宽表格，合成数据仅供测试。")))
    landscape = ('<w:sectPr><w:headerReference w:type="default" r:id="rIdHeader"/>'
                 '<w:footerReference w:type="default" r:id="rIdFooter"/>'
                 '<w:pgSz w:w="16838" w:h="11906" w:orient="landscape"/>'
                 '<w:pgMar w:top="1440" w:right="1440" w:bottom="1440" w:left="1440" w:header="851" w:footer="992" w:gutter="0"/>'
                 "</w:sectPr>")
    return f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n<w:document {NS}><w:body>{"".join(body)}{landscape}</w:body></w:document>'


def header(d: Doc) -> str:
    body = d.p(d.r("合成示例 · 仅供测试"), '<w:jc w:val="right"/>')
    return f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n<w:hdr {NS}>{body}</w:hdr>'


def footer(d: Doc) -> str:
    runs = (d.r("第 ") + '<w:r><w:fldChar w:fldCharType="begin"/></w:r><w:r><w:instrText xml:space="preserve"> PAGE </w:instrText></w:r>'
            '<w:r><w:fldChar w:fldCharType="separate"/></w:r>' + d.r("1") + '<w:r><w:fldChar w:fldCharType="end"/></w:r>'
            + d.r(" 页 / 共 ") + '<w:fldSimple w:instr=" NUMPAGES ">' + d.r("1") + "</w:fldSimple>" + d.r(" 页"))
    body = d.p(runs, '<w:jc w:val="center"/>')
    return f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n<w:ftr {NS}>{body}</w:ftr>'


STYLES = f'''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<w:styles {NS}><w:docDefaults><w:rPrDefault><w:rPr><w:rFonts w:ascii="Times New Roman" w:eastAsia="SimSun"/><w:sz w:val="21"/><w:lang w:val="en-US" w:eastAsia="zh-CN"/></w:rPr></w:rPrDefault>
<w:pPrDefault><w:pPr><w:spacing w:after="120" w:line="300" w:lineRule="auto"/></w:pPr></w:pPrDefault></w:docDefaults>
<w:style w:type="paragraph" w:default="1" w:styleId="Normal"><w:name w:val="Normal"/></w:style>
<w:style w:type="paragraph" w:styleId="Title"><w:name w:val="Title"/><w:basedOn w:val="Normal"/><w:rPr><w:b/><w:sz w:val="36"/></w:rPr></w:style>
<w:style w:type="paragraph" w:styleId="Heading1"><w:name w:val="heading 1"/><w:basedOn w:val="Normal"/><w:pPr><w:spacing w:before="240"/><w:outlineLvl w:val="0"/></w:pPr><w:rPr><w:b/><w:sz w:val="28"/></w:rPr></w:style>
<w:style w:type="table" w:styleId="TableGrid"><w:name w:val="Table Grid"/><w:tblPr><w:tblBorders><w:top w:val="single" w:sz="4" w:color="000000"/><w:left w:val="single" w:sz="4" w:color="000000"/><w:bottom w:val="single" w:sz="4" w:color="000000"/><w:right w:val="single" w:sz="4" w:color="000000"/><w:insideH w:val="single" w:sz="4" w:color="000000"/><w:insideV w:val="single" w:sz="4" w:color="000000"/></w:tblBorders></w:tblPr></w:style>
</w:styles>'''
NUMBERING = f'''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<w:numbering {NS}><w:abstractNum w:abstractNumId="0"><w:lvl w:ilvl="0"><w:start w:val="1"/><w:numFmt w:val="decimal"/><w:lvlText w:val="%1."/><w:pPr><w:ind w:left="420" w:hanging="420"/></w:pPr></w:lvl></w:abstractNum>
<w:num w:numId="1"><w:abstractNumId w:val="0"/></w:num></w:numbering>'''
COMMENTS = f'''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<w:comments {NS}><w:comment w:id="0" w:author="合成审阅人" w:date="2026-01-02T00:00:00Z"><w:p><w:r><w:t>请确认违约金比例（合成批注）</w:t></w:r></w:p></w:comment></w:comments>'''
CONTENT_TYPES = '''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"><Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/><Default Extension="xml" ContentType="application/xml"/>
<Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>
<Override PartName="/word/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.styles+xml"/>
<Override PartName="/word/numbering.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.numbering+xml"/>
<Override PartName="/word/comments.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.comments+xml"/>
<Override PartName="/word/header1.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.header+xml"/>
<Override PartName="/word/footer1.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.footer+xml"/>
<Override PartName="/docProps/core.xml" ContentType="application/vnd.openxmlformats-package.core-properties+xml"/>
<Override PartName="/docProps/app.xml" ContentType="application/vnd.openxmlformats-officedocument.extended-properties+xml"/></Types>'''
ROOT_RELS = '''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/>
<Relationship Id="rId2" Type="http://schemas.openxmlformats.org/package/2006/relationships/metadata/core-properties" Target="docProps/core.xml"/>
<Relationship Id="rId3" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/extended-properties" Target="docProps/app.xml"/></Relationships>'''
DOC_RELS = '''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rIdStyles" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/>
<Relationship Id="rIdNumbering" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/numbering" Target="numbering.xml"/>
<Relationship Id="rIdComments" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/comments" Target="comments.xml"/>
<Relationship Id="rIdHeader" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/header" Target="header1.xml"/>
<Relationship Id="rIdFooter" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/footer" Target="footer1.xml"/></Relationships>'''
CORE = '''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<cp:coreProperties xmlns:cp="http://schemas.openxmlformats.org/package/2006/metadata/core-properties" xmlns:dc="http://purl.org/dc/elements/1.1/"><dc:title>合成采购框架协议（示例）</dc:title></cp:coreProperties>'''


SAMPLE_PAGES = 5   # sample.docx 在 Word 里的页数（4 页 A4 纵向 + 1 页 A4 横向）


def app_xml(pages: int | None) -> str:
    """docProps/app.xml：Word 保存时写下页数与程序名；pages=None 表示不写页数（模拟别的工具）。"""
    count = f"<Pages>{pages}</Pages>" if pages is not None else ""
    return ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n<Properties xmlns='
            '"http://schemas.openxmlformats.org/officeDocument/2006/extended-properties">'
            f"<Application>Microsoft Office Word</Application><AppVersion>16.0000</AppVersion>{count}</Properties>")


def package(out: Path, document_xml: str, *, app_pages: int | None = None, header_xml: str | None = None,
            footer_xml: str | None = None, extra: dict | None = None) -> Path:
    """把一份 document.xml 包成 DOCX（样式、编号、批注、页眉页脚沿用样例）。测试用它造单一结构的小文档。"""
    d = Doc(False, False)
    parts = {"[Content_Types].xml": CONTENT_TYPES, "_rels/.rels": ROOT_RELS, "docProps/core.xml": CORE,
             "docProps/app.xml": app_xml(app_pages),
             "word/_rels/document.xml.rels": DOC_RELS, "word/document.xml": document_xml,
             "word/styles.xml": STYLES, "word/numbering.xml": NUMBERING, "word/comments.xml": COMMENTS,
             "word/header1.xml": header_xml or header(d), "word/footer1.xml": footer_xml or footer(d)}
    parts.update(extra or {})   # 测试用：覆盖或追加部件（图片、关系表、设置等）
    out = Path(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        for name, text in parts.items():
            data = text if isinstance(text, bytes) else text.encode("utf-8")
            z.writestr(zipfile.ZipInfo(name, FIXED_TIME), data, zipfile.ZIP_DEFLATED)
    return out


A4_SECT = '<w:sectPr><w:pgSz w:w="11906" w:h="16838"/></w:sectPr>'


def body_xml(inner: str, sect: str = "") -> str:
    """测试用：正文片段包成完整的 document.xml。"""
    return (f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n<w:document {NS}><w:body>{inner}'
            f"{sect or A4_SECT}</w:body></w:document>")


def build_docx(out: Path, *, rendered: bool = True, para_ids: bool = True, app_pages: int | None = SAMPLE_PAGES) -> Path:
    d = Doc(rendered, para_ids)
    return package(out, document(d), app_pages=app_pages if rendered else None, header_xml=header(d), footer_xml=footer(d))


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("out", nargs="?", type=Path, default=Path(__file__).with_name("sample.docx"))
    ap.add_argument("--no-rendered", action="store_true")
    ap.add_argument("--no-para-ids", action="store_true")
    a = ap.parse_args()
    print(build_docx(a.out, rendered=not a.no_rendered, para_ids=not a.no_para_ids))


if __name__ == "__main__":
    main()
