#!/usr/bin/env python3
"""生成桌面 Word 审阅用的两份合成合同。用法：python3 make_word_native.py 输出目录"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from xml.sax.saxutils import escape

from make_sample import CONTENT_TYPES, CORE, DOC_RELS, NS, Doc, body_xml, package

W15 = "http://schemas.microsoft.com/office/word/2012/wordml"
CID = "http://schemas.microsoft.com/office/word/2016/wordml/cid"
CEX = "http://schemas.microsoft.com/office/word/2018/wordml/cex"
STAMP = "2026-01-02T09:00:00Z"
LINE = '<w:spacing w:before="0" w:after="0" w:line="510" w:lineRule="exact"/>'
TAIL = "签收即生效"
TAIL_REPLACEMENT = (
    "双方完成核验并签字之日起生效。签收人员应先逐项核对交付清单、样机编号和封签，双方在记录中分别注明实收数量、测试结果和缺件。"
    "如发现标记不清、附件缺失或设备损伤，收货方应在当天将照片和情况说明交给发货方。"
    "发货方确认补交时间后，收货方复验补交材料，在原记录中注明验收完成日期，并将双方确认的清单各存一份"
)

# 自然分页。以下每条独占一行；末尾保留一个可在 Word 中扩写的普通段落。
TERMS = (
    "01　交付场地设在合成样机实验室。",
    "02　每件样机使用独立的示例编号。",
    "03　发货人按附件清单逐件装箱。",
    "04　箱内应附包装清单和使用说明。",
    "05　包装外应标明运送方向。",
    "06　到货时由收货人核对封签。",
    "07　两方分别记录到货时间。",
    "08　清点时以实收数量为准。",
    "09　数量有差异时双方共同复核。",
    "10　缺件记录应注明样机编号。",
    "11　运行测试由双方共同见证。",
    "12　测试结果登记在交接单中。",
    "13　首次验收应保留实测记录。",
    "14　需要复验的样机单独保管。",
    "15　复验结论须由双方确认。",
    "16　附件与样机应一并交还。",
    "17　经确认的记录由双方各存一份。",
    "18　所有往来材料均使用本合同编号。",
)
NOTES = (
    ("0", "01000001", "02000001", "乙", "2026-01-02T10:00:00Z", "此处的五日是否足够完成复核？"),
    ("1", "01000002", "02000002", "甲", "2026-01-02T11:00:00Z", "收到，请按验收安排复核时限。"),
)


def change(kind: str, number: int, text: str) -> str:
    run = Doc.r(text) if kind == "ins" else f"<w:r><w:delText>{escape(text)}</w:delText></w:r>"
    return f'<w:{kind} w:id="{number}" w:author="甲" w:date="{STAMP}">{run}</w:{kind}>'


def contract(label: str) -> str:
    d = Doc(False, True)
    clause = '<w:widowControl w:val="0"/>' + LINE
    title = d.r("样机交接协议", '<w:b/><w:sz w:val="32"/>')
    deadline = (
        d.r("应于")
        + '<w:commentRangeStart w:id="0"/><w:commentRangeStart w:id="1"/>'
        + d.r("五日")
        + '<w:commentRangeEnd w:id="1"/><w:commentRangeEnd w:id="0"/>'
        + '<w:r><w:commentReference w:id="0"/></w:r><w:r><w:commentReference w:id="1"/></w:r>'
        + d.r("内回复")
    )
    table = (
        '<w:tbl><w:tblPr><w:tblStyle w:val="TableGrid"/><w:tblW w:w="8400" w:type="dxa"/></w:tblPr>'
        '<w:tblGrid><w:gridCol w:w="3000"/><w:gridCol w:w="5400"/></w:tblGrid><w:tr>'
        '<w:tc><w:tcPr><w:tcW w:w="3000" w:type="dxa"/></w:tcPr>'
        + d.p(d.r("验收异议"), clause)
        + '</w:tc><w:tc><w:tcPr><w:tcW w:w="5400" w:type="dxa"/></w:tcPr>'
        + d.p(deadline, clause)
        + "</w:tc></w:tr></w:tbl>"
    )
    paragraphs = [
        d.p(title, clause),
        d.p(d.r(f"合同编号 AE-214-{label}　｜　合成审阅样例"), clause),
        d.p(d.r("合成甲方和合成乙方约定，按下列条件交接测试样机。"), clause),
        d.p(
            d.r("收件方式为") + change("del", 2, "传真") + change("ins", 3, "邮件") + d.r("，应于五日内回复。"), clause
        ),
        d.p(d.r("验收范围包括样机") + change("del", 4, "及备用件") + d.r("。"), clause),
        d.p(change("ins", 5, "共同") + change("del", 6, "分别") + d.r("登记样机编号。"), clause),
        d.p(d.r("异议处理时限"), clause),
        table,
        *(d.p(d.r(text), clause) for text in TERMS),
        d.p(d.r("本合同自" + TAIL + "。"), clause),
        d.p(d.r("交接单"), clause),
        d.p(d.r("交付物　合成样机一件、装箱单一份、使用说明一册。"), clause),
        d.p(d.r("交付方　合成甲方样机组"), clause),
        d.p(d.r("接收方　合成乙方验收组"), clause),
        d.p(d.r("约定交付时间　2026 年 1 月 12 日"), clause),
    ]
    section = (
        '<w:sectPr><w:pgSz w:w="11906" w:h="16838"/>'
        '<w:pgMar w:top="1440" w:right="1440" w:bottom="1440" w:left="1440" '
        'w:header="720" w:footer="720" w:gutter="0"/></w:sectPr>'
    )
    return body_xml("".join(paragraphs), section)


def comment_parts() -> dict:
    comments, extended, ids, extensible, people = [], [], [], [], []
    for cid, pid, durable, author, date, text in NOTES:
        comments.append(
            f'<w:comment w:id="{cid}" w:author="{author}" w:initials="{author}" w:date="{date}">'
            f'<w:p w14:paraId="{pid}" w14:textId="77777777">{Doc.r(text)}</w:p></w:comment>'
        )
        parent = f' w15:paraIdParent="{NOTES[0][1]}"' if cid == "1" else ""
        extended.append(f'<w15:commentEx w15:paraId="{pid}"{parent} w15:done="0"/>')
        ids.append(f'<cid:commentId cid:paraId="{pid}" cid:durableId="{durable}"/>')
        extensible.append(f'<cex:commentExtensible cex:durableId="{durable}" cex:dateUtc="{date}"/>')
        people.append(f'<w15:person w15:author="{author}"/>')
    return {
        "word/comments.xml": f"<w:comments {NS}>{''.join(comments)}</w:comments>",
        "word/commentsExtended.xml": f'<w15:commentsEx xmlns:w15="{W15}">{"".join(extended)}</w15:commentsEx>',
        "word/commentsIds.xml": f'<cid:commentsIds xmlns:cid="{CID}">{"".join(ids)}</cid:commentsIds>',
        "word/commentsExtensible.xml": (
            f'<cex:commentsExtensible xmlns:cex="{CEX}">{"".join(extensible)}</cex:commentsExtensible>'
        ),
        "word/people.xml": f'<w15:people xmlns:w15="{W15}">{"".join(people)}</w15:people>',
    }


def build(out: Path, label: str) -> Path:
    relationships = {
        "settings": "http://schemas.openxmlformats.org/officeDocument/2006/relationships/settings",
        "commentsExtended": "http://schemas.microsoft.com/office/2011/relationships/commentsExtended",
        "commentsIds": "http://schemas.microsoft.com/office/2016/09/relationships/commentsIds",
        "commentsExtensible": "http://schemas.microsoft.com/office/2018/08/relationships/commentsExtensible",
        "people": "http://schemas.microsoft.com/office/2011/relationships/people",
    }
    rels = "".join(
        f'<Relationship Id="rId{name}" Type="{uri}" Target="{name}.xml"/>' for name, uri in relationships.items()
    )
    types = "".join(
        f'<Override PartName="/word/{name}.xml" '
        f'ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.{name}+xml"/>'
        for name in relationships
    )
    parts = {
        **comment_parts(),
        "[Content_Types].xml": CONTENT_TYPES.replace("</Types>", types + "</Types>"),
        "word/_rels/document.xml.rels": DOC_RELS.replace("</Relationships>", rels + "</Relationships>"),
        "word/settings.xml": (
            f'<w:settings {NS}><w:revisionView w:markup="true" w:comments="true" w:insDel="true"/>'
            '<w:trackRevisions/><w:compat><w:compatSetting w:name="compatibilityMode" '
            'w:uri="http://schemas.microsoft.com/office/word" w:val="15"/></w:compat></w:settings>'
        ),
        "docProps/core.xml": CORE.replace("合成采购框架协议（示例）", f"样机交接协议 AE-214-{label}"),
        "docProps/app.xml": (
            '<Properties xmlns="http://schemas.openxmlformats.org/officeDocument/2006/extended-properties">'
            "<Application>AE synthetic fixture</Application></Properties>"
        ),
    }
    return package(out, contract(label), extra=parts)


def prepare_review(source: Path, label: str) -> Path:
    scripts = Path(__file__).resolve().parents[2] / "skill" / "annotation-everything" / "scripts"
    if str(scripts) not in sys.path:
        sys.path.insert(0, str(scripts))
    import build_viewer
    import docx_review
    import notes_store

    review = source.parent / "review"
    build_viewer.build(source, None, review, name=f"合成合同 {label}", no_scan_skills=True)
    index = docx_review.render(source)
    for full, old, new, reason in (
        ("应于五日内回复", "五日", "七日", "表格中的验收异议需要多两日复核。"),
        (f"本合同自{TAIL}。", TAIL, TAIL_REPLACEMENT, "写明交接单生效所需的核验程序。"),
    ):
        (para,) = [p for p in index["paragraphs"] if p["digest"] == docx_review.text_digest(full)]
        start = full.index(old)
        notes_store.add(
            review,
            {
                "page": para["frags"][0]["page"],
                "target": "报告",
                "quoteFrom": "报告",
                "quote": old,
                "text": reason,
                "docx": {
                    "v": 1,
                    "kind": "quote",
                    "via": "text",
                    "fp": index["fingerprint"],
                    "para": {k: para[k] for k in ("n", "id", "digest")},
                    "start": start,
                    "end": start + len(old),
                },
                "edit": {"op": "replace", "from": old, "to": new},
            },
            by="fixture",
        )
    return review


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path)
    parser.add_argument("--prepare-reviews", action="store_true", help="同时创建两份 AE 审阅，各含两条快捷替换批注")
    args = parser.parse_args()
    for label in ("A", "B"):
        path = args.output / label / "contract.docx"
        if path.exists() or (args.prepare_reviews and (path.parent / "review").exists()):
            parser.error(f"{path.parent} 已有合同或审阅，请使用新的输出目录。")
    for label in ("A", "B"):
        source = build(args.output / label / "contract.docx", label)
        print(source)
        if args.prepare_reviews:
            print(prepare_review(source, label))


if __name__ == "__main__":
    main()
