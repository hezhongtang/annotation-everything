#!/usr/bin/env python3
"""生成 PPTX 读取层用的可复现合成材料，只使用 Python 标准库。"""
from __future__ import annotations

import argparse
from pathlib import Path
import zipfile
from xml.sax.saxutils import escape


FIXED_TIME = (2026, 1, 1, 0, 0, 0)
P = "http://schemas.openxmlformats.org/presentationml/2006/main"
A = "http://schemas.openxmlformats.org/drawingml/2006/main"
R = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
PKG_REL = "http://schemas.openxmlformats.org/package/2006/relationships"
CT = "http://schemas.openxmlformats.org/package/2006/content-types"


def text_run(text: str, properties: str = '<a:rPr lang="zh-CN"/>') -> str:
    return f'<a:r>{properties}<a:t xml:space="preserve">{escape(text)}</a:t></a:r>'


def slide_xml(text: str = "", paragraphs: str = "") -> str:
    body = paragraphs or f"<a:p>{text_run(text)}</a:p>"
    return f'''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<p:sld xmlns:p="{P}" xmlns:a="{A}" xmlns:r="{R}"><p:cSld><p:spTree>
<p:nvGrpSpPr><p:cNvPr id="1" name=""/><p:cNvGrpSpPr/><p:nvPr/></p:nvGrpSpPr><p:grpSpPr/>
<p:sp><p:nvSpPr><p:cNvPr id="2" name="合成标题"/><p:cNvSpPr txBox="1"/><p:nvPr/></p:nvSpPr>
<p:spPr><a:xfrm><a:off x="914400" y="914400"/><a:ext cx="5486400" cy="914400"/></a:xfrm></p:spPr>
<p:txBody><a:bodyPr/><a:lstStyle/>{body}</p:txBody></p:sp>
</p:spTree></p:cSld><p:clrMapOvr><a:overrideClrMapping bg1="lt1" tx1="dk1" bg2="lt2" tx2="dk2" accent1="accent1" accent2="accent2" accent3="accent3" accent4="accent4" accent5="accent5" accent6="accent6" hlink="hlink" folHlink="folHlink"/></p:clrMapOvr></p:sld>'''


def graphic_frame(shape_id: int, name: str, uri: str, content: str) -> str:
    return (f'<p:graphicFrame><p:nvGraphicFramePr><p:cNvPr id="{shape_id}" name="{name}"/>'
            '<p:cNvGraphicFramePr/><p:nvPr/></p:nvGraphicFramePr>'
            '<p:xfrm><a:off x="0" y="0"/><a:ext cx="1000000" cy="1000000"/></p:xfrm>'
            f'<a:graphic><a:graphicData uri="{uri}">{content}</a:graphicData></a:graphic></p:graphicFrame>')


def element_slide_xml() -> str:
    table = graphic_frame(10, "合成表格", A + "/table", '<a:tbl><a:tblGrid/><a:tr h="400000"><a:tc><a:txBody><a:bodyPr/><a:lstStyle/><a:p>'
                          + text_run("合成单元格") + '</a:p></a:txBody><a:tcPr/></a:tc><a:tc><a:txBody><a:bodyPr/><a:p>'
                          + text_run("第二列") + '</a:p></a:txBody></a:tc></a:tr><a:tr h="400000"><a:tc><a:txBody><a:bodyPr/><a:p>'
                          + text_run("第二行") + '</a:p></a:txBody></a:tc></a:tr></a:tbl>')
    picture = '''<p:pic><p:nvPicPr><p:cNvPr id="11" name="合成图片" descr="合成图片替代文本"/><p:cNvPicPr/><p:nvPr/></p:nvPicPr><p:blipFill><a:blip r:embed="rIdImage"/></p:blipFill><p:spPr><a:xfrm><a:off x="1000000" y="0"/><a:ext cx="1000000" cy="1000000"/></a:xfrm></p:spPr></p:pic>'''
    group = '''<p:grpSp><p:nvGrpSpPr><p:cNvPr id="12" name="合成组合"/><p:cNvGrpSpPr/><p:nvPr/></p:nvGrpSpPr><p:grpSpPr><a:xfrm><a:off x="1219200" y="685800"/><a:ext cx="6096000" cy="3429000"/><a:chOff x="100" y="200"/><a:chExt cx="1000" cy="1000"/></a:xfrm></p:grpSpPr><p:sp><p:nvSpPr><p:cNvPr id="13" name="组合内文本"/><p:cNvSpPr txBox="1"/><p:nvPr/></p:nvSpPr><p:spPr><a:xfrm><a:off x="600" y="700"/><a:ext cx="500" cy="500"/></a:xfrm></p:spPr><p:txBody><a:bodyPr/><a:lstStyle/><a:p><a:r><a:t>组合内文本</a:t></a:r></a:p></p:txBody></p:sp></p:grpSp>'''
    chart = graphic_frame(14, "合成图表", A + "/chart", '<c:chart xmlns:c="http://schemas.openxmlformats.org/drawingml/2006/chart" r:id="rIdChart"/>')
    smartart = graphic_frame(15, "合成流程图", A + "/diagram", '<dgm:relIds xmlns:dgm="http://schemas.openxmlformats.org/drawingml/2006/diagram" r:dm="rIdDiagram"/>')
    media = '''<p:pic><p:nvPicPr><p:cNvPr id="16" name="合成媒体"/><p:cNvPicPr/><p:nvPr><a:videoFile r:link="rIdVideo"/></p:nvPr></p:nvPicPr><p:blipFill><a:blip r:embed="rIdImage"/></p:blipFill><p:spPr><a:xfrm><a:off x="5000000" y="0"/><a:ext cx="1000000" cy="1000000"/></a:xfrm></p:spPr></p:pic>'''
    ole = graphic_frame(17, "合成OLE", A + "/oleobject", '<p:oleObj r:id="rIdOle"/>')
    unknown = graphic_frame(18, "未知扩展", "urn:synthetic:unknown", '<a:p>' + text_run("未知合成文本") + '</a:p>')
    return f'''<?xml version="1.0" encoding="UTF-8"?>
<p:sld xmlns:p="{P}" xmlns:a="{A}" xmlns:r="{R}"><p:cSld><p:spTree><p:nvGrpSpPr><p:cNvPr id="1" name=""/><p:cNvGrpSpPr/><p:nvPr/></p:nvGrpSpPr><p:grpSpPr/>
{table}{picture}{group}{chart}{smartart}{media}{ole}{unknown}</p:spTree></p:cSld></p:sld>'''


def styled_slide_xml() -> str:
    """带既有填充/线条/run 样式、占位符与无 xfrm 形状的合成页：几何与样式回写矩阵用。

    id 2：自带 solidFill、a:ln（含 prstDash）与带 rPr 的 run；id 3：带 xfrm 的占位符；
    id 4：没有 xfrm（几何继承自布局，不可写）。
    """
    return f'''<?xml version="1.0" encoding="UTF-8"?>
<p:sld xmlns:p="{P}" xmlns:a="{A}" xmlns:r="{R}"><p:cSld><p:spTree>
<p:nvGrpSpPr><p:cNvPr id="1" name=""/><p:cNvGrpSpPr/><p:nvPr/></p:nvGrpSpPr><p:grpSpPr/>
<p:sp><p:nvSpPr><p:cNvPr id="2" name="合成标题"/><p:cNvSpPr txBox="1"/><p:nvPr/></p:nvSpPr>
<p:spPr><a:xfrm><a:off x="914400" y="914400"/><a:ext cx="5486400" cy="914400"/></a:xfrm><a:prstGeom prst="rect"><a:avLst/></a:prstGeom><a:solidFill><a:srgbClr val="4477AA"/></a:solidFill><a:ln w="19050"><a:solidFill><a:srgbClr val="112233"/></a:solidFill><a:prstDash val="dash"/></a:ln></p:spPr>
<p:txBody><a:bodyPr/><a:lstStyle/><a:p><a:r><a:rPr b="1" sz="1800"><a:solidFill><a:srgbClr val="AA0000"/></a:solidFill><a:latin typeface="Synthetic Sans"/></a:rPr><a:t>标题甲</a:t></a:r><a:r><a:t>标题乙</a:t></a:r></a:p></p:txBody></p:sp>
<p:sp><p:nvSpPr><p:cNvPr id="3" name="合成占位符"/><p:cNvSpPr/><p:nvPr><p:ph type="title"/></p:nvPr></p:nvSpPr>
<p:spPr><a:xfrm><a:off x="0" y="0"/><a:ext cx="3000000" cy="600000"/></a:xfrm></p:spPr>
<p:txBody><a:bodyPr/><a:lstStyle/><a:p><a:r><a:t>占位</a:t></a:r></a:p></p:txBody></p:sp>
<p:sp><p:nvSpPr><p:cNvPr id="4" name="无变换"/><p:cNvSpPr txBox="1"/><p:nvPr/></p:nvSpPr>
<p:spPr><a:prstGeom prst="rect"><a:avLst/></a:prstGeom></p:spPr>
<p:txBody><a:bodyPr/><a:lstStyle/><a:p><a:r><a:t>没有变换</a:t></a:r></a:p></p:txBody></p:sp>
</p:spTree></p:cSld></p:sld>'''


def sample_parts(portrait: bool = False) -> dict[str, str | bytes]:
    rich = ("<a:p>" + text_run("相同", '<a:rPr b="1"><a:latin typeface="Synthetic Sans"/></a:rPr>')
            + text_run("😀&<", '<a:rPr i="1" sz="1800"/>') + "</a:p><a:p>" + text_run("相同") + "</a:p>")
    width, height = (6858000, 12192000) if portrait else (12192000, 6858000)
    slides = {
        "ppt/slides/slide10.xml": slide_xml(paragraphs=rich),
        "ppt/slides/slide2.xml": element_slide_xml(),
    }
    overrides = "".join(
        f'<Override PartName="/{name}" ContentType="application/vnd.openxmlformats-officedocument.presentationml.slide+xml"/>'
        for name in slides
    )
    return {
        "[Content_Types].xml": f'''<?xml version="1.0" encoding="UTF-8"?>
<Types xmlns="{CT}"><Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/><Default Extension="xml" ContentType="application/xml"/>
<Override PartName="/ppt/presentation.xml" ContentType="application/vnd.openxmlformats-officedocument.presentationml.presentation.main+xml"/>{overrides}</Types>''',
        "_rels/.rels": f'''<?xml version="1.0" encoding="UTF-8"?>
<Relationships xmlns="{PKG_REL}"><Relationship Id="rIdDeck" Type="{R}/officeDocument" Target="ppt/presentation.xml"/></Relationships>''',
        "ppt/presentation.xml": f'''<?xml version="1.0" encoding="UTF-8"?>
<p:presentation xmlns:p="{P}" xmlns:r="{R}"><p:sldIdLst><p:sldId id="260" r:id="rIdSecond"/><p:sldId id="257" r:id="rIdFirst"/></p:sldIdLst><p:sldSz cx="{width}" cy="{height}"/></p:presentation>''',
        "ppt/_rels/presentation.xml.rels": f'''<?xml version="1.0" encoding="UTF-8"?>
<Relationships xmlns="{PKG_REL}"><Relationship Id="rIdFirst" Type="{R}/slide" Target="slides/slide2.xml"/><Relationship Id="rIdSecond" Type="{R}/slide" Target="slides/slide10.xml"/></Relationships>''',
        **slides,
        "ppt/slides/_rels/slide10.xml.rels": f'''<?xml version="1.0" encoding="UTF-8"?>
<Relationships xmlns="{PKG_REL}"><Relationship Id="rIdNotes" Type="{R}/notesSlide" Target="../notesSlides/notesSlide1.xml"/></Relationships>''',
        "ppt/notesSlides/notesSlide1.xml": f'''<?xml version="1.0" encoding="UTF-8"?>
<p:notes xmlns:p="{P}" xmlns:a="{A}"><p:cSld><p:spTree><p:nvGrpSpPr><p:cNvPr id="1" name=""/><p:cNvGrpSpPr/><p:nvPr/></p:nvGrpSpPr><p:grpSpPr/><p:sp><p:nvSpPr><p:cNvPr id="2" name="合成备注正文"/><p:cNvSpPr/><p:nvPr><p:ph type="body"/></p:nvPr></p:nvSpPr><p:txBody><a:bodyPr/><a:lstStyle/><a:p>{text_run("合成备注😀")}</a:p><a:p>{text_run("第二段")}</a:p></p:txBody></p:sp><p:sp><p:nvSpPr><p:cNvPr id="3" name="页码"/><p:cNvSpPr/><p:nvPr><p:ph type="sldNum"/></p:nvPr></p:nvSpPr><p:txBody><a:bodyPr/><a:p>{text_run("1")}</a:p></p:txBody></p:sp></p:spTree></p:cSld></p:notes>''',
        "ppt/slides/_rels/slide2.xml.rels": f'''<?xml version="1.0" encoding="UTF-8"?>
<Relationships xmlns="{PKG_REL}"><Relationship Id="rIdImage" Type="{R}/image" Target="../media/poster.png"/><Relationship Id="rIdChart" Type="{R}/chart" Target="../charts/chart1.xml"/><Relationship Id="rIdDiagram" Type="{R}/diagramData" Target="../diagrams/data1.xml"/><Relationship Id="rIdVideo" Type="{R}/video" Target="../media/synthetic.mp4"/><Relationship Id="rIdOle" Type="{R}/oleObject" Target="../embeddings/synthetic.bin"/></Relationships>''',
        "ppt/media/poster.png": b"synthetic image; not parsed",
        "ppt/media/synthetic.mp4": b"synthetic media; not executed",
        "ppt/charts/chart1.xml": b"synthetic chart; not parsed",
        "ppt/diagrams/data1.xml": b"synthetic diagram; not parsed",
        "ppt/embeddings/synthetic.bin": b"synthetic embedding; not executed",
    }


def write_package(out: Path, parts: dict[str, str | bytes]) -> Path:
    out = Path(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(out, "w") as archive:
        for name, content in parts.items():
            data = content.encode("utf-8") if isinstance(content, str) else content
            info = zipfile.ZipInfo(name, FIXED_TIME)
            info.external_attr = 0o600 << 16
            archive.writestr(info, data, zipfile.ZIP_DEFLATED)
    return out


def build_sample(out: Path, portrait: bool = False) -> Path:
    return write_package(out, sample_parts(portrait=portrait))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("out", nargs="?", type=Path, default=Path(__file__).with_name("sample.pptx"))
    args = parser.parse_args()
    print(build_sample(args.out))


if __name__ == "__main__":
    main()
