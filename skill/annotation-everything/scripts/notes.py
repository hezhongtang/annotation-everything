#!/usr/bin/env python3
"""批注命令行：给 Claude 读批注、标已处理、导入旧批注用。服务开着也能安全使用（共用同一把文件锁）。

用法：
  python3 notes.py show <审阅目录> [--all] [--page 7] [--json]   # 默认只列未处理，按页分组的 Markdown
  python3 notes.py resolve <审阅目录> <id> [<id> ...] --note "改了什么"
  python3 notes.py reopen <审阅目录> <id> [<id> ...]
  python3 notes.py import <审阅目录> <文件>   # 页面导出的 JSON，或旧版对照页「复制全部 / 下载」的 Markdown
  python3 notes.py wait <审阅目录> [--timeout 秒]   # 阻塞到用户点「交给 Agent」，输出提交内容与未处理批注
  python3 notes.py claim <审阅目录>   # 不等待：有等待接收的提交就接收（输出 JSON，退出码 0），没有返回 3
  python3 notes.py done <审阅目录> --summary "处理结果"   # 告诉页面这次提交已处理完
  python3 notes.py skills <审阅目录> [--set "名称=说明" ...] [--add ...] [--remove ...] [--clear]
                                                   # 维护技能目录：页面「技能」输入栏的候选

退出码：0 成功（wait：收到提交）；1 部分 id 不存在；2 参数或目录不对；3 wait 超时。
处理人默认记为 claude（在 Claude Code 里运行时）或 agent，可用 --by 指定。
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
import notes_store as store  # noqa: E402
from build_viewer import find_pages, resolve_sources, strip_tags, PAGE_HEAD  # noqa: E402
import match  # noqa: E402


def load_manifest(review_dir: Path) -> dict[str, Any]:
    f = review_dir / "review.json"
    try:
        return json.loads(f.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def default_agent() -> str:
    return "claude" if os.environ.get("CLAUDECODE") else "agent"


STATUS_ZH = {"pending": "等待接收", "claimed": "处理中", "done": "已处理"}


def handoff_lines(state: dict[str, Any] | None) -> list[str]:
    """「交给 Agent」提交的摘要行（附言是用户在页面上写给代理的话）。"""
    if not state or not state.get("latest"):
        return []
    h = state["latest"]
    line = (f"交给 Agent：最近一次是第 {h['seq']} 次提交，{STATUS_ZH.get(h.get('status'), h.get('status'))}"
            f"（提交于 {h.get('at')}，当时未处理 {h.get('openCount')} 条）")
    out = [line]
    if h.get("message"):
        out.append(f"用户附言：{h['message']}")
    if h.get("status") == "done" and h.get("summary"):
        out.append(f"处理结果：{h['summary']}")
    if state.get("pending") or state.get("claimed"):
        out.append(f"待接收 {state.get('pending', 0)} 次、处理中 {state.get('claimed', 0)} 次"
                   "（处理中却没有代理在跑，多半是上次处理中断：处理完用 notes.py done 收尾）")
    return out


def pin_numbers(notes: list[dict[str, Any]]) -> dict[str, int]:
    """与页面一致：同一页带标点的批注按出现顺序编号 1、2、3…"""
    out: dict[str, int] = {}
    count: dict[int, int] = {}
    for n in notes:
        if n.get("pin"):
            count[n["page"]] = count.get(n["page"], 0) + 1
            out[n["id"]] = count[n["page"]]
    return out


def side_texts(sources: dict[str, Path | None] | None) -> dict[str, dict[int, str]]:
    """两侧逐页纯文本：报告取各页 section 的文字，讲稿取各页 Markdown（去掉字段名与加粗符号）。读不到就返回空。"""
    out: dict[str, dict[int, str]] = {"报告": {}, "讲稿": {}}
    if not sources:
        return out
    try:
        if sources.get("report"):
            for sct in find_pages(sources["report"].read_text(encoding="utf-8")):
                out["报告"][sct["n"]] = strip_tags(re.sub(r"<(script|style)\b.*?</\1\s*>", " ", sct["html"], flags=re.S | re.I))
        if sources.get("script"):
            md = sources["script"].read_text(encoding="utf-8")
            heads = list(PAGE_HEAD.finditer(md))
            for k, h in enumerate(heads):
                body = md[h.end(): heads[k + 1].start() if k + 1 < len(heads) else len(md)]
                body = re.sub(r"\*\*(时长|讲法|过渡|可能被问)\*\*\s*[：:]", "\n", body).replace("**", "")
                out["讲稿"][int(h.group(1))] = h.group(2) + "\n" + body
    except (OSError, ValueError, SystemExit):
        pass
    return out


MATCH_ZH = {"exact": "相同", "similar": "相近"}


def style_scope_zh(st: dict[str, Any]) -> str:
    if st.get("scope") == "region" and isinstance(st.get("region"), dict):
        r = st["region"]
        return f"区域（x {r.get('x')}%，y {r.get('y')}%，宽 {r.get('w')}%，高 {r.get('h')}%）"
    return {"page": "整页", "pin": "标点处"}.get(st.get("scope"), "整页")


def pair_lines(n: dict[str, Any], texts: dict[str, dict[int, str]]) -> list[str]:
    """另一侧也要改的提示：用户勾选了「同时改」的直接列出；没勾选、但另一侧同一页有相同或相近句子的，作为提醒列出。"""
    pr = n.get("pair") if isinstance(n.get("pair"), dict) else None
    if pr:
        return [f"  ↔ 另一侧（{pr.get('side')}）也要改，对应句（{MATCH_ZH.get(pr.get('match'), '相近')}）：「{pr.get('text', '')}」"]
    side = n.get("target")
    if n.get("status") == "resolved" or side not in ("报告", "讲稿"):
        return []
    ed = n.get("edit") if isinstance(n.get("edit"), dict) else None
    probe = (ed or {}).get("from") or n.get("quote")
    other = "讲稿" if side == "报告" else "报告"
    hay = texts.get(other, {}).get(int(n.get("page", 0) or 0), "")
    got = match.counterpart(probe, hay) if probe else None
    if not got:
        return []
    return [f"  ↔ 提示：{other}第 {n.get('page')} 页有{MATCH_ZH[got[0]]}的句子「{got[1]}」，确认是否也要同步改（批注时没勾选同改）"]


def to_markdown(doc: dict[str, Any], manifest: dict[str, Any], *, include_resolved: bool, page: int | None,
                sources: dict[str, Path | None] | None = None, handoff: dict[str, Any] | None = None) -> str:
    texts = side_texts(sources)
    all_notes = doc["notes"]
    pins = pin_numbers(all_notes)
    titles = {p["n"]: p.get("title", "") for p in manifest.get("pages", [])}
    keys = {p["n"]: p.get("key", "") for p in manifest.get("pages", [])}
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
    head += handoff_lines(handoff)
    out = ["\n".join(head)]
    last = None
    for n in notes:
        if n["page"] != last:
            last = n["page"]
            out.append(f"\n## 第 {last} 页｜{titles.get(last) or n.get('title', '')}" + (f"（键 {keys[last]}）" if keys.get(last) else ""))
        tag = n.get("target") or "未指定"
        if n.get("pin"):
            tag += f"·标点 {pins.get(n['id'])}（x {n['pin']['x']}%，y {n['pin']['y']}%）"
        state = "【已处理】" if n.get("status") == "resolved" else ""
        ed = n.get("edit") if isinstance(n.get("edit"), dict) else None
        if ed and ed.get("op") == "delete":
            body = f"删除「{ed.get('from', '')}」"
        elif ed and ed.get("op") == "replace":
            body = f"「{ed.get('from', '')}」改为「{ed.get('to', '')}」"
        else:
            body = str(n.get("text", ""))
        if ed:
            tag += "·" + ("删除" if ed.get("op") == "delete" else "改写")
        st = n.get("style") if isinstance(n.get("style"), dict) else None
        if st:
            tag += "·风格优化·" + style_scope_zh(st)
        line = f"- `{n['id']}` {state}【{tag}】" + body.replace("\n", "\n  ")
        if n.get("quote") and not ed:
            line += f"\n  > 引用{('（' + n['quoteFrom'] + '）') if n.get('quoteFrom') else ''}：{n['quote']}"
        if n.get("status") == "resolved" and n.get("resolution"):
            line += f"\n  处理说明：{n['resolution']}"
        if n.get("orphan"):
            line += f"\n  ⚠ 这条批注原来所在的页面（键 {n.get('key')}）在新版报告里找不到，页码可能不对：先确认它对应哪一页"
        elif n.get("key") and keys.get(n.get("page")) and n["key"] != keys[n["page"]]:
            line += f"\n  ⚠ 批注记的页面键 {n['key']} 与第 {n['page']} 页（{keys[n['page']]}）不一致，重建对照页可自动校正"
        if n.get("anchor"):
            line += f"\n  位置线索：{n['anchor']}"
        if n.get("skills"):
            line += f"\n  技能：{'、'.join(n['skills'])}（处理这条前先调用这些技能）"
        for extra in pair_lines(n, texts):
            line += "\n" + extra
        out.append(line)
    if not notes:
        out.append("\n（没有符合条件的批注）")
    return "\n".join(out) + "\n"


# 旧版对照页导出的 Markdown：
# ## 第 7 页｜标题
# 1. 【报告·标点 1（x 12.3%，y 45.6%）】意见……
#    > 引用（报告）：……
DELETE_TEXT = store.DELETE_TEXT
REPLACE_PREFIX = store.REPLACE_PREFIX
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
            # 页面导出的快捷修改：正文是固定说法，引用是原文
            if cur["text"] == DELETE_TEXT:
                cur["edit"] = {"op": "delete", "from": cur["quote"]}
            elif cur["text"].startswith(REPLACE_PREFIX):
                cur["edit"] = {"op": "replace", "from": cur["quote"], "to": cur["text"][len(REPLACE_PREFIX):]}
        elif cur is not None and (m := re.match(r"^\s+处理说明：(.*)$", line)):
            cur["resolution"] = m.group(1)
        elif cur is not None and (m := re.match(r"^\s+风格优化：(整页|标点处|区域（x ([\d.]+)%，y ([\d.]+)%，宽 ([\d.]+)%，高 ([\d.]+)%）)$", line)):
            if m.group(2):
                cur["style"] = {"scope": "region", "region": {"x": float(m.group(2)), "y": float(m.group(3)),
                                                             "w": float(m.group(4)), "h": float(m.group(5))}}
            else:
                cur["style"] = {"scope": "page" if m.group(1) == "整页" else "pin"}
        elif cur is not None and (m := re.match(r"^\s+位置线索：(.*)$", line)):
            cur["anchor"] = m.group(1)
        elif cur is not None and (m := re.match(r"^\s+技能：(.*)$", line)):
            cur["skills"] = store.clean_skill_names(m.group(1))
        elif cur is not None and (m := re.match(r"^\s+↔ 另一侧（(报告|讲稿)(?:·(相同|相近))?）：(.*)$", line)):
            cur["pair"] = {"side": m.group(1), "match": "exact" if m.group(2) == "相同" else "similar", "text": m.group(3)}
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
    r.add_argument("--by", default=default_agent(), help="处理人，页面上显示为「已处理 · 处理人」")
    o = sub.add_parser("reopen", help="重新打开")
    o.add_argument("dir", type=Path)
    o.add_argument("ids", nargs="+")
    i = sub.add_parser("import", help="导入页面导出的 JSON 或旧版 Markdown")
    i.add_argument("dir", type=Path)
    i.add_argument("file", type=Path)
    w = sub.add_parser("wait", help="等待用户点「交给 Agent」：收到提交后标为处理中，输出提交内容与未处理批注")
    w.add_argument("dir", type=Path)
    w.add_argument("--timeout", type=float, default=0, help="最多等多少秒，0 表示一直等（默认）")
    w.add_argument("--interval", type=float, default=1.0, help="检查间隔秒数，默认 1")
    w.add_argument("--no-claim", action="store_true", help="只看不接收（不改状态）")
    w.add_argument("--by", default=default_agent(), help="接收人，页面上显示")
    sk = sub.add_parser("skills", help="查看或设置技能目录（页面「技能」输入栏的候选）")
    sk.add_argument("dir", type=Path)
    sk.add_argument("--set", nargs="*", metavar="名称=说明", help="整体替换为这些技能")
    sk.add_argument("--add", nargs="*", metavar="名称=说明", help="追加或更新这些技能")
    sk.add_argument("--remove", nargs="*", metavar="名称", help="移除这些技能")
    sk.add_argument("--clear", action="store_true", help="清空目录")
    cl = sub.add_parser("claim", help="不等待：接收最早一个等待中的提交（给自己轮询的代理用，别直接改 handoff.json）")
    cl.add_argument("dir", type=Path)
    cl.add_argument("--by", default=default_agent())
    dn = sub.add_parser("done", help="把提交标为已处理，页面会显示处理结果")
    dn.add_argument("dir", type=Path)
    dn.add_argument("--summary", default="", help="处理结果：改了几条、哪些留给用户定")
    dn.add_argument("--seq", type=int, help="提交序号；默认最近一个处理中的提交")
    dn.add_argument("--by", default=default_agent())
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
            print(json.dumps({**doc, "notes": notes, "handoff": store.handoff_state(d)}, ensure_ascii=False, indent=2))
        else:
            manifest = load_manifest(d)
            try:
                sources = resolve_sources(d, manifest) if manifest else None
            except SystemExit as e:
                print(f"警告：{e}", file=sys.stderr)
                sources = None
            print(to_markdown(doc, manifest, include_resolved=a.all, page=a.page, sources=sources,
                              handoff=store.handoff_state(d)), end="")
        return 0
    if a.cmd in ("resolve", "reopen"):
        status = "resolved" if a.cmd == "resolve" else "open"
        bad = 0
        for nid in a.ids:
            try:
                store.set_status(d, nid, status, resolution=getattr(a, "note", ""), by=getattr(a, "by", default_agent()))
                print(f"{nid} → {'已处理' if status == 'resolved' else '未处理'}")
            except store.NotFound:
                print(f"{nid}：没有这条批注", file=sys.stderr)
                bad += 1
        return 1 if bad else 0
    if a.cmd == "import":
        _doc, added, skipped, updated = store.import_notes(d, read_import(a.file), by="cli-import")
        print(f"导入完成：新增 {added} 条，更新 {updated} 条，跳过重复 {skipped} 条 → {d / store.NOTES_FILE}")
        return 0
    if a.cmd == "wait":
        return wait(d, a.timeout, a.interval, claim=not a.no_claim, by=a.by)
    if a.cmd == "skills":
        def parse(items: list[str] | None) -> list[dict[str, str]]:
            out = []
            for it in items or []:
                name, _, desc = it.partition("=")
                name = store.clean_skill_names(name)
                if name:
                    out.append({"name": name[0], "desc": desc.strip()[:120]})
            return out
        cat = [] if (a.clear or a.set is not None) else store.skills_catalog(d)
        for it in parse(a.set) + parse(a.add):
            cat = [c for c in cat if c["name"] != it["name"]] + [it]
        drop = set(store.clean_skill_names(a.remove or []))
        cat = [c for c in cat if c["name"] not in drop]
        if a.clear or a.set is not None or a.add or a.remove:
            store.skills_save(d, cat)
        used = sorted({x for n in store.load(d)["notes"] for x in (n.get("skills") or [])})
        print("技能目录（页面「技能」输入栏的候选）：" + ("；".join(f"{c['name']}" + (f"（{c['desc']}）" if c["desc"] else "")
                                                     for c in cat) or "（空）"))
        if used:
            print("批注里用到的技能：" + "、".join(used))
        return 0
    if a.cmd == "claim":
        item = store.handoff_claim(d, by=a.by)
        if item is None:
            print(json.dumps({"claimed": False}, ensure_ascii=False))
            return 3
        print(json.dumps({"claimed": True, **{k: v for k, v in item.items() if k != "openIds"}}, ensure_ascii=False))
        return 0
    if a.cmd == "done":
        try:
            item = store.handoff_done(d, seq=a.seq, summary=a.summary, by=a.by)
        except store.NotFound:
            print("没有待处理或处理中的提交（用户还没点「交给 Agent」，或已全部标为已处理）。", file=sys.stderr)
            return 1
        print(f"第 {item['seq']} 次提交 → 已处理。页面会显示处理结果。")
        return 0
    return 2


def wait(d: Path, timeout: float, interval: float, *, claim: bool, by: str) -> int:
    """阻塞等待「交给 Agent」提交。已经在等待中的提交（之前没人接收）会立即返回。"""
    state = store.handoff_state(d)
    if state.get("claimed"):
        print(f"提示：有 {state['claimed']} 次提交处于「处理中」却没有收尾（可能上次处理中断）。"
              "用 notes.py show 查看，处理完运行 notes.py done。", file=sys.stderr)
    deadline = time.monotonic() + timeout if timeout > 0 else None
    try:
        while True:
            try:
                doc = store.handoff_load(d)
            except (OSError, ValueError):
                doc = {"items": []}
            if any(i.get("status") == "pending" for i in doc["items"]):
                item = store.handoff_claim(d, by=by) if claim else \
                    next(i for i in doc["items"] if i.get("status") == "pending")
                if item is not None:
                    break
            if deadline is not None and time.monotonic() >= deadline:
                print(f"等待超时（{timeout:g} 秒），用户还没有点「交给 Agent」。")
                return 3
            time.sleep(max(0.2, interval))
    except KeyboardInterrupt:
        return 130
    manifest = load_manifest(d)
    try:
        sources = resolve_sources(d, manifest) if manifest else None
    except SystemExit as e:
        print(f"警告：{e}", file=sys.stderr)
        sources = None
    pages = "、".join(str(p) for p in item.get("pages") or []) or "无"
    print(f"# 收到「交给 Agent」第 {item['seq']} 次提交（{item.get('at')}）")
    if item.get("message"):
        print(f"用户附言：{item['message']}")
    need = sorted({x for n in store.load(d)["notes"] if n.get("status") != "resolved" for x in (n.get("skills") or [])})
    if need:
        print(f"这批批注指定了技能：{'、'.join(need)}（处理对应批注前先调用）")
    print(f"提交时未处理批注 {item.get('openCount')} 条，涉及第 {pages} 页。"
          + ("已标为「处理中」，页面同步显示。" if claim else "（--no-claim：未改状态）"))
    print(f"处理完：逐条 notes.py resolve {d} <id> --note \"…\"，重建对照页，最后 notes.py done {d} --summary \"…\"\n")
    print(to_markdown(store.load(d), manifest, include_resolved=False, page=None, sources=sources), end="")
    return 0


if __name__ == "__main__":
    sys.exit(main())
