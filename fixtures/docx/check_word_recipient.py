#!/usr/bin/env python3
"""只读核对 make_word_native.py 合成合同的表格、旧修订和讨论。"""

from __future__ import annotations

import argparse
from pathlib import Path
from xml.etree import ElementTree as ET
from zipfile import BadZipFile, ZipFile

W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
W14 = "{http://schemas.microsoft.com/office/word/2010/wordml}"
W15 = "{http://schemas.microsoft.com/office/word/2012/wordml}"
OLD_REVISIONS = [
    ("收件方式为邮件，应于五日内回复。", [("del", "甲", "传真"), ("ins", "甲", "邮件")]),
    ("验收范围包括样机。", [("del", "甲", "及备用件")]),
    ("共同登记样机编号。", [("ins", "甲", "共同"), ("del", "甲", "分别")]),
]
OLD_REPLY = "收到，请按验收安排复核时限。"
TEXT = W + "t"
DELETED_TEXT = W + "delText"


def projected(node, skip=None):
    if node.tag == skip:
        return ""
    if node.tag in (TEXT, DELETED_TEXT):
        return node.text or ""
    return "".join(projected(child, skip) for child in node)


def revisions(node):
    return [
        (item.tag[len(W) :], item.get(W + "author", "").strip(), projected(item))
        for item in node.iter()
        if item.tag in (W + "ins", W + "del")
    ]


def referenced_text(cell, cid):
    inside = False
    text = []
    marks = []
    for node in cell.iter():
        if node.get(W + "id") == cid and node.tag in (W + "commentRangeStart", W + "commentRangeEnd"):
            inside = node.tag == W + "commentRangeStart"
            marks.append("start" if inside else "end")
        if inside and node.tag == TEXT:
            text.append(node.text or "")
    references = sum(node.get(W + "id") == cid for node in cell.iter(W + "commentReference"))
    return "".join(text) if marks == ["start", "end"] and references == 1 else None


def check(path, expected, extra_replies):
    with ZipFile(path) as package:
        document = ET.fromstring(package.read("word/document.xml"))
        comments = ET.fromstring(package.read("word/comments.xml"))
        links = ET.fromstring(package.read("word/commentsExtended.xml"))
    (row,) = document.findall(f"{W}body/{W}tbl/{W}tr")
    target, protected = row.findall(W + "tc")
    old = projected(target, W + "ins")
    new = projected(target, W + "del")
    changes = revisions(target)
    before, after = {
        "original": ("验收异议", "验收异议"),
        "tracked": ("验收异议", "交付异议"),
        "accepted": ("交付异议", "交付异议"),
        "rejected": ("验收异议", "验收异议"),
    }[expected]
    if expected == "tracked":
        deleted = "".join(content for kind, _, content in changes if kind == "del")
        inserted = "".join(content for kind, _, content in changes if kind == "ins")
        authors = {author for _, author, _ in changes}
        target_marks_ok = (deleted, inserted) == ("验收", "交付") and len(authors) == 1 and "" not in authors
    else:
        target_marks_ok = not changes
    body_paragraphs = document.findall(f"{W}body/{W}p")
    legacy = []
    for paragraph in body_paragraphs:
        original_author_changes = [change for change in revisions(paragraph) if change[1] == "甲"]
        if original_author_changes:
            legacy.append((projected(paragraph, W + "del"), original_author_changes))
    authors_text = [(c, c.get(W + "author", "").strip(), projected(c)) for c in comments.findall(W + "comment")]
    (parent,) = [c for c, author, text in authors_text if (author, text) == ("乙", "此处的五日是否足够完成复核？")]

    def paragraph_id(comment):
        return comment.findall(W + "p")[-1].get(W14 + "paraId")

    parent_pid = paragraph_id(parent)
    parents = {link.get(W15 + "paraId"): link.get(W15 + "paraIdParent") for link in links}
    replies = [
        (author, text) for c, author, text in authors_text if parent_pid and parents.get(paragraph_id(c)) == parent_pid
    ]
    checks = {
        f"第一格 Original={old!r}、Current={new!r}，与 {expected} 对应": (old, new) == (before, after)
        and target_marks_ok,
        "第二格保留应于五日内回复，且无文字修订": projected(protected) == "应于五日内回复" and not revisions(protected),
        "正文同名词仍在收件方式原段": any(
            projected(p, W + "del") == "收件方式为邮件，应于五日内回复。" for p in body_paragraphs
        ),
        "甲的五项旧修订文字和署名均在": legacy == OLD_REVISIONS,
        "乙的原话仍引用第二格的五日": referenced_text(protected, parent.get(W + "id")) == "五日",
        "甲的既有回复仍属乙的原线程": bool(parent_pid and parent_pid in parents and parents[parent_pid] is None)
        and ("甲", OLD_REPLY) in replies,
    }
    for reply in extra_replies:
        checks[f"追加回复 {reply!r} 在原线程内只出现一次且署名可读"] = (
            reply != OLD_REPLY and sum(1 for author, text in replies if author and text == reply) == 1
        )
    return checks


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("document", type=Path)
    parser.add_argument("--expect", choices=("original", "tracked", "accepted", "rejected"), required=True)
    parser.add_argument("--reply", action="append", default=[], help="原线程中追加的合成回复全文，可重复指定")
    args = parser.parse_args()
    try:
        checks = check(args.document, args.expect, args.reply)
    except (BadZipFile, ET.ParseError, KeyError, IndexError, ValueError, OSError) as exc:
        print(f"无法核对合成合同结构或原批注：{exc}")
        return 1
    for name, ok in checks.items():
        print(f"{'通过' if ok else '失败'}　{name}")
    return 0 if all(checks.values()) else 1


if __name__ == "__main__":
    raise SystemExit(main())
