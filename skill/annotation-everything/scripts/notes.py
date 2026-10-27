#!/usr/bin/env python3
"""批注命令行：给 Claude 读批注、标已处理、导入旧批注用。服务开着也能安全使用（共用同一把文件锁）。

用法：
  python3 notes.py show <审阅目录> [--all] [--page 7] [--json]   # 默认只列未处理，按页分组的 Markdown
  python3 notes.py resolve <审阅目录> <id> [<id> ...] --note "改了什么"
  python3 notes.py reopen <审阅目录> <id> [<id> ...]
  python3 notes.py import <审阅目录> <文件>   # 页面导出的 JSON，或旧版对照页「复制全部 / 下载」的 Markdown
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
import notes_store as store  # noqa: E402
from build_viewer import resolve_sources  # noqa: E402


def load_manifest(review_dir: Path) -> dict[str, Any]:
    f = review_dir / "review.json"
    try:
        return json.loads(f.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def pin_numbers(notes: list[dict[str, Any]]) -> dict[str, int]:
    """与页面一致：同一页带标点的批注按出现顺序编号 1、2、3…"""
    out: dict[str, int] = {}
    count: dict[int, int] = {}
    for n in notes:
        if n.get("pin"):
            count[n["page"]] = count.get(n["page"], 0) + 1
            out[n["id"]] = count[n["page"]]
    return out


def to_markdown(doc: dict[str, Any], manifest: dict[str, Any], *, include_resolved: bool, page: int | None,
                sources: dict[str, Path | None] | None = None) -> str:
    all_notes = doc["notes"]
    pins = pin_numbers(all_notes)
    titles = {p["n"]: p.get("title", "") for p in manifest.get("pages", [])}
    notes = [n for n in all_notes if (include_resolved or n.get("status") != "resolved")
             and (page is None or n.get("page") == page)]
    notes.sort(key=lambda n: (int(n.get("page", 0) or 0), str(n.get("at", ""))))
    n_open = sum(n.get("status") != "resolved" for n in all_notes)
    head = [f"# 批注 · {manifest.get('title') or '对照页'}",
            f"共 {len(all_notes)} 条（未处理 {n_open}，已处理 {len(all_notes) - n_open}）；"
            f"以下列出 {'全部' if include_resolved else '未处理'} {len(notes)} 条。rev {doc.get('rev')}",
            "标点坐标为相对页面左上角的百分比（横向 x、纵向 y）。"]
    if sources:  # 已按 review.json 解析成实际路径（相对审阅目录优先），改稿就改这里列出的文件
        head.append("源文件（改稿改这里）：" + "；".join(f"{'报告' if k == 'report' else '讲稿'} = {v}"
                                                   for k, v in sources.items() if v))
    out = ["\n".join(head)]
    last = None
    for n in notes:
        if n["page"] != last:
            last = n["page"]
            out.append(f"\n## 第 {last} 页｜{titles.get(last) or n.get('title', '')}")
        tag = n.get("target") or "未指定"
        if n.get("pin"):
            tag += f"·标点 {pins.get(n['id'])}（x {n['pin']['x']}%，y {n['pin']['y']}%）"
        state = "【已处理】" if n.get("status") == "resolved" else ""
        line = f"- `{n['id']}` {state}【{tag}】" + str(n.get("text", "")).replace("\n", "\n  ")
        if n.get("quote"):
            line += f"\n  > 引用{('（' + n['quoteFrom'] + '）') if n.get('quoteFrom') else ''}：{n['quote']}"
        if n.get("status") == "resolved" and n.get("resolution"):
            line += f"\n  处理说明：{n['resolution']}"
        out.append(line)
    if not notes:
        out.append("\n（没有符合条件的批注）")
    return "\n".join(out) + "\n"


# 旧版对照页导出的 Markdown：
# ## 第 7 页｜标题
# 1. 【报告·标点 1（x 12.3%，y 45.6%）】意见……
#    > 引用（报告）：……
LEGACY_PAGE = re.compile(r"^## 第 (\d+) 页｜(.*)$")
LEGACY_ITEM = re.compile(r"^\d+\. 【([^】·]*)(?:·标点 \d+（x ([\d.]+)%，y ([\d.]+)%）)?】(.*)$")
LEGACY_QUOTE = re.compile(r"^\s+> 引用(?:（([^）]*)）)?：(.*)$")


def parse_legacy_markdown(text: str) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    page, title, cur = None, "", None
    for line in text.splitlines():
        if m := LEGACY_PAGE.match(line):
            page, title, cur = int(m.group(1)), m.group(2).strip(), None
            continue
        if page is None:
            continue
        if m := LEGACY_ITEM.match(line):
            text, status = m.group(4), "open"
            if text.startswith("【已处理】"):
                text, status = text[len("【已处理】"):], "resolved"
            cur = {"page": page, "title": title, "target": m.group(1), "text": text, "status": status,
                   "pin": {"x": float(m.group(2)), "y": float(m.group(3))} if m.group(2) else None}
            items.append(cur)
        elif cur is not None and (m := LEGACY_QUOTE.match(line)):
            cur["quoteFrom"], cur["quote"] = m.group(1) or "", m.group(2)
        elif cur is not None and (m := re.match(r"^\s+处理说明：(.*)$", line)):
            cur["resolution"] = m.group(1)
        elif cur is not None and line.startswith("   ") and line.strip():
            cur["text"] += "\n" + line[3:]
    return items


def read_import(path: Path) -> list[dict[str, Any]]:
    text = path.read_text(encoding="utf-8")
    stripped = text.lstrip()
    if stripped.startswith("{") or stripped.startswith("["):
        data = json.loads(text)
        return data.get("notes", []) if isinstance(data, dict) else data
    items = parse_legacy_markdown(text)
    if not items:
        raise SystemExit(f"{path} 里没找到批注：既不是 JSON，也不是对照页导出的 Markdown 格式")
    return items


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="notes.py", description="读写审阅目录里的批注")
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("show", help="列出批注（默认只列未处理）")
    s.add_argument("dir", type=Path)
    s.add_argument("--all", action="store_true", help="包含已处理")
    s.add_argument("--page", type=int)
    s.add_argument("--json", action="store_true", help="输出原始 JSON")
    r = sub.add_parser("resolve", help="标记为已处理")
    r.add_argument("dir", type=Path)
    r.add_argument("ids", nargs="+")
    r.add_argument("--note", default="", help="处理说明：改了哪里、怎么改的（页面上会显示）")
    o = sub.add_parser("reopen", help="重新打开")
    o.add_argument("dir", type=Path)
    o.add_argument("ids", nargs="+")
    i = sub.add_parser("import", help="导入页面导出的 JSON 或旧版 Markdown")
    i.add_argument("dir", type=Path)
    i.add_argument("file", type=Path)
    a = ap.parse_args(argv)
    d = a.dir.resolve()
    # 打错目录时不要悄悄新建一个空审阅目录
    if not d.is_dir() or not any((d / f).exists() for f in ("review.json", "index.html", store.NOTES_FILE)):
        print(f"错误：{d} 不是审阅目录（里面没有 review.json / index.html / notes.json）。", file=sys.stderr)
        return 2

    if a.cmd == "show":
        doc = store.load(d)
        if a.json:
            notes = [n for n in doc["notes"] if (a.all or n.get("status") != "resolved")
                     and (a.page is None or n.get("page") == a.page)]
            print(json.dumps({**doc, "notes": notes}, ensure_ascii=False, indent=2))
        else:
            manifest = load_manifest(d)
            try:
                sources = resolve_sources(d, manifest) if manifest else None
            except SystemExit as e:
                print(f"警告：{e}", file=sys.stderr)
                sources = None
            print(to_markdown(doc, manifest, include_resolved=a.all, page=a.page, sources=sources), end="")
        return 0
    if a.cmd in ("resolve", "reopen"):
        status = "resolved" if a.cmd == "resolve" else "open"
        bad = 0
        for nid in a.ids:
            try:
                store.set_status(d, nid, status, resolution=getattr(a, "note", ""), by="claude")
                print(f"{nid} → {'已处理' if status == 'resolved' else '未处理'}")
            except store.NotFound:
                print(f"{nid}：没有这条批注", file=sys.stderr)
                bad += 1
        return 1 if bad else 0
    if a.cmd == "import":
        _doc, added, skipped, updated = store.import_notes(d, read_import(a.file), by="cli-import")
        print(f"导入完成：新增 {added} 条，更新 {updated} 条，跳过重复 {skipped} 条 → {d / store.NOTES_FILE}")
        return 0
    return 2


if __name__ == "__main__":
    sys.exit(main())
