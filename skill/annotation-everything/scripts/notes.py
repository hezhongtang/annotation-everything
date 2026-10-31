#!/usr/bin/env python3
"""批注命令行：给 Claude 读批注、标已处理、导入旧批注用。服务开着也能安全使用（共用同一把文件锁）。

用法：
  python3 notes.py show <审阅目录> [--all] [--page 7] [--json]   # 默认只列未处理，按页分组的 Markdown
  python3 notes.py resolve <审阅目录> <id> [<id> ...] --note "改了什么"
  python3 notes.py reopen <审阅目录> <id> [<id> ...]
  python3 notes.py import <审阅目录> <文件> [--force]   # 页面导出的 JSON（含别处救回的未保存改动），或旧版 Markdown；
                                                       # JSON 带的审阅标识与目录不同时拒绝，确认要导就加 --force
  python3 notes.py wait <审阅目录> [--timeout 秒]   # 阻塞到用户点「交给 Agent」，输出提交内容与未处理批注
  python3 notes.py claim <审阅目录>   # 不等待：有等待接收的提交就接收（输出 JSON，退出码 0），没有返回 3
  python3 notes.py done <审阅目录> --summary "处理摘要"   # 收尾这一批：记下解决了哪些、页面显示摘要
  python3 notes.py batches <审阅目录> [--last 3] [--json]   # 处理记录：每批解决了几条、处理摘要
  python3 notes.py review <审阅目录> <id> ... --ok | --revise | --return [--note "理由"]   # 复核结论
  python3 notes.py diff <审阅目录> [--batch N] [--to current] [--page 7]   # 看初改动了源文件的哪些地方
  python3 notes.py skills <审阅目录> [--set "名称=说明" ...] [--add ...] [--remove ...] [--clear]
                    [--always "名称=范围" ...] [--always-add ...] [--always-remove 名称 ...] [--always-clear]
                                                   # 常驻技能：每条批注（按范围：全部/报告/讲稿/风格优化）都先调用
                                                   # 维护技能目录：页面「技能」输入栏的候选

<审阅目录> 处也可以写审阅标识（或至少 4 位前缀），从本机审阅台账查目录。

退出码：0 成功（wait：收到提交）；1 部分 id 不存在；2 参数或目录不对；3 wait 超时。
处理人默认记为 claude（在 Claude Code 里运行时）或 agent，可用 --by 指定。
"""
from __future__ import annotations

import argparse
import contextlib
import json
import os
import re
import signal
import sys
import time
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
import i18n  # noqa: E402
import ledger  # noqa: E402
import notes_store as store  # noqa: E402
from build_viewer import (FIELD_LABEL, field_name, find_pages, page_heads,  # noqa: E402
                          resolve_sources, strip_tags, write_private)
import difflib  # noqa: E402
import match  # noqa: E402


def load_manifest(review_dir: Path) -> dict[str, Any]:
    return ledger.read_manifest(review_dir)   # 读不了或不是 JSON 对象都返回 {}


def default_agent() -> str:
    return "claude" if os.environ.get("CLAUDECODE") else "agent"


# 批次状态、痕迹动作、阶段与批注状态：存储值 → 界面消息 key（存储值本身不随语言改写）
STATUS_KEY = {"pending": "cli.batch.status.pending", "claimed": "cli.batch.status.claimed",
              "drafted": "cli.batch.status.drafted", "review": "cli.batch.status.review",
              "reviewing": "cli.batch.status.reviewing", "done": "cli.batch.status.done"}
NOTE_STATUS_KEY = {"open": "cli.status.open", "drafted": "cli.status.drafted", "resolved": "cli.status.resolved"}
ACTION_KEY = {"drafted": "cli.batch.trail.action.drafted", "resolved": "cli.batch.trail.action.resolved",
              "finalized": "cli.batch.trail.action.finalized", "approved": "cli.batch.trail.action.approved",
              "revised": "cli.batch.trail.action.revised", "returned": "cli.batch.trail.action.returned",
              "reopened": "cli.batch.trail.action.reopened", "accepted": "cli.batch.trail.action.accepted"}
STAGE_KEY = {"draft": "cli.batch.stageName.draft", "review": "cli.batch.stageName.review"}
# 输出按当前语言写的命令（其余命令不读界面语言：claim 只输出 JSON，导入与技能设置另有归属）
LOCALIZED_CMDS = ("show", "wait", "batches", "done", "review", "diff", "resolve", "reopen")
# 常驻技能的范围：存储值 → 界面显示的消息 key（存储值本身不随语言改写）
ALWAYS_SCOPE_KEY = {"all": "viewer.hand.scope.all", "报告": "viewer.hand.scope.report",
                    "讲稿": "viewer.hand.scope.script", "style": "viewer.hand.scope.style"}


def cli_language(choice: object, review_dir: Path) -> str:
    """命令行语言：--locale 优先于 AE_LOCALE，再优先于审阅里保存的选择，都没有时中文。"""
    stored = load_manifest(review_dir).get("locale")
    return i18n.resolve_locale(
        i18n.effective_choice(choice, os.environ.get("AE_LOCALE"), stored if isinstance(stored, str) else None),
        system_languages=i18n.system_languages())


def cli_text(lang: str | None):
    """按当前语言取命令行文案；缺译时回退到另一种语言（词典自检保证两种语言齐备）。"""
    catalogs = i18n.load_all()   # 读一次缓存住：处理记录可能几百批，不能每条消息都读一遍文件
    cat = cli_catalog(lang)

    def t(msg: str, **params: object) -> str:
        return i18n.translate(cat, msg, catalogs=catalogs, **params)

    return t


def cli_catalog(lang: str | None) -> dict:
    """按语言取词典（读一次缓存住）；语言不在支持列表时退回中文。"""
    catalogs = i18n.load_all()
    return catalogs.get(lang or i18n.DEFAULT_LOCALE) or catalogs[i18n.DEFAULT_LOCALE]


def label(t, key: str, fallback: object = "") -> str:
    """词典里有这条消息才用；旧数据里的未知取值按原文显示，不猜。"""
    try:
        return t(key)
    except i18n.MissingMessageError:
        return str(fallback or "")


def always_text(t, items: object) -> str:
    """常驻技能摘要：名称保持原文，范围只改显示（导出与存储的写法不变）。"""
    out = []
    for x in items or []:
        if not isinstance(x, dict):
            continue
        scope = ALWAYS_SCOPE_KEY.get(x.get("scope"))
        out.append(t("cli.hand.alwaysItem", name=str(x.get("name") or ""),
                     scope=label(t, scope) if scope else str(x.get("scope") or "")))
    return t("cli.hand.itemSep").join(out)


def trail_text(n: dict[str, Any], last: int = 4, lang: str | None = None) -> str:
    """处理痕迹（最近几步）：谁、哪个阶段、做了什么、理由。执行者名称与理由保持原文。"""
    t = cli_text(lang)
    parts = []
    for e in (n.get("trail") or [])[-last:]:
        if not isinstance(e, dict):
            continue
        who = t("cli.batch.trail.whoPage") if e.get("by") == "page" else str(e.get("by") or "")
        action = label(t, ACTION_KEY.get(e.get("action"), ""), e.get("action"))
        batch = t("cli.batch.trailBatch", batch=e["batch"]) if e.get("batch") else ""
        note = t("cli.batch.trailNote", note=str(e.get("note"))[:80]) if e.get("note") else ""
        parts.append(t("cli.batch.trailEntry", action=action, who=who, batch=batch, note=note))
    return " → ".join(parts)


def target_text(h: dict[str, Any], lang: str | None = None) -> str:
    """这批的要求：做到定稿还是停在待复核、能不能派子代理、要不要全量勾稽。"""
    t = cli_text(lang)
    out = t("cli.hand.target.review" if h.get("target") == "review" else "cli.hand.target.final")
    out += t("cli.hand.subagents.ok" if h.get("allowSubagents") else "cli.hand.subagents.no")
    if h.get("fullCheck"):
        out += t("cli.hand.fullCheck")
    return out


def handoff_lines(state: dict[str, Any] | None, lang: str | None = None) -> list[str]:
    """「交给 Agent」提交的摘要行（附言是用户在页面上写给代理的话）。"""
    t = cli_text(lang)
    if not state or not state.get("latest"):
        return []
    h = state["latest"]
    out = [t("cli.hand.latest", seq=h["seq"], status=label(t, STATUS_KEY.get(h.get("status"), ""), h.get("status")),
             at=h.get("at"), count=h.get("openCount"))]
    if message := store.handoff_user_message(h):
        out.append(t("cli.hand.userMessage", message=message))
    if h.get("status") == "done":
        out.append(t("cli.hand.resolvedCount", count=h.get("resolvedCount", 0))
                   + (t("cli.hand.summary", summary=h["summary"]) if h.get("summary") else ""))
    if state.get("total"):
        out.append(t("cli.hand.history", count=state["total"]))
    if state.get("pending") or state.get("claimed") or state.get("review") or state.get("reviewing"):
        out.append(t("cli.hand.inFlight", pending=state.get("pending", 0), claimed=state.get("claimed", 0),
                     review=state.get("review", 0), reviewing=state.get("reviewing", 0)))
    if state.get("drafted"):
        out.append(t("cli.hand.drafted", count=state["drafted"]))
    for b in state.get("open") or []:
        out.append(t("cli.hand.open", seq=b["seq"], status=label(t, STATUS_KEY.get(b.get("status"), ""), b.get("status")),
                     target=target_text(b, lang))
                   + (t("cli.hand.unclaimed") if b.get("status") in ("pending", "review") else ""))
    return out


def mark_numbers(notes: list[dict[str, Any]]) -> dict[str, int]:
    """与页面一致：同一页带标注（点或框）的批注按出现顺序编号 1、2、3…"""
    out: dict[str, int] = {}
    count: dict[int, int] = {}
    for n in notes:
        if n.get("pin") or store.region_of(n):
            count[n["page"]] = count.get(n["page"], 0) + 1
            out[n["id"]] = count[n["page"]]
    return out


def batches_text(state: dict[str, Any], notes: list[dict[str, Any]], *, limit: int | None = None,
                 lang: str | None = None) -> str:
    """处理记录：每次「交给 Agent」算一批，列出提交、接收、收尾、这批解决了哪些批注和处理摘要（新的在前）。"""
    t = cli_text(lang)
    sep = t("cli.hand.itemSep")
    hist = state.get("history") or []
    by_id = {n.get("id"): n for n in notes}
    counts = t("cli.batch.counts", done=state.get("done", 0), claimed=state.get("claimed", 0),
               pending=state.get("pending", 0))
    for k in ("drafted", "review", "reviewing"):
        if state.get(k):
            counts += t("cli.batch.countsMore", status=label(t, STATUS_KEY.get(k, ""), k), count=state[k])
    out = [t("cli.batch.title", total=len(hist), counts=counts, count=len(hist))]
    for h in hist[:limit] if limit else hist:
        who = h.get("doneBy") or h.get("claimedBy") or ""
        out.append(t("cli.batch.item", seq=h.get("seq"),
                     status=label(t, STATUS_KEY.get(h.get("status"), ""), h.get("status")),
                     who=t("cli.batch.who", who=who) if who else ""))
        out.append(t("cli.batch.submitted", at=h.get("at"), count=h.get("openCount"))
                   + (t("cli.batch.pages", pages=sep.join(map(str, h.get("pages") or []))) if h.get("pages") else "")
                   + (t("cli.batch.updates", count=h["updates"]) if h.get("updates") else ""))
        if message := store.handoff_user_message(h):
            out.append(t("cli.hand.userMessage", message=message))
        if "target" in h:
            out.append(t("cli.batch.requirement", target=target_text(h, lang)))
        if h.get("alwaysSkills"):
            out.append(t("cli.batch.alwaysLine", list=always_text(t, h["alwaysSkills"])))
        stages = [e for e in h.get("stages") or [] if isinstance(e, dict)]
        if not stages and h.get("claimedAt"):
            out.append(t("cli.batch.claimedAt", at=h["claimedAt"])
                       + (t("cli.batch.doneAt", at=h["doneAt"]) if h.get("doneAt") else ""))
        two = store.two_stage(h)
        for e in stages:
            if e.get("stage") == "accept":
                out.append(t("cli.batch.acceptLine", count=e.get("count", 0), at=e.get("at")))
                continue
            # 单段流程不叫「初改」，就叫处理
            name = (label(t, STAGE_KEY.get(e.get("stage"), ""), e.get("stage")) if two
                    or e.get("stage") != "draft" else t("cli.batch.stageName.single"))
            line = t("cli.batch.stage", stage=name, by=e.get("by") or "") + t("cli.batch.stageClaim", at=e.get("claimedAt") or "—")
            line += t("cli.batch.stageDone", at=e["doneAt"]) if e.get("doneAt") else t("cli.batch.stageLive")
            if two and e.get("stage") == "draft" and "draftedIds" in e and e.get("doneAt"):
                line += t("cli.batch.stageDrafted", count=len(e["draftedIds"]))
            if e.get("stage") == "review" and e.get("doneAt"):
                line += t("cli.batch.stageReviewed", ok=e.get("approved", 0), revised=e.get("revised", 0),
                          returned=e.get("returned", 0), resolved=e.get("resolved", 0))
            out.append(line)
            if e.get("summary") and e["summary"] != h.get("summary"):
                out.append(t("cli.batch.stageSummary", stage=name, summary=e["summary"]))
        if h.get("reviewMessage"):
            out.append(t("cli.batch.reviewNote", message=h["reviewMessage"]))
        if "resolvedCount" in h:
            pages = sep.join(map(str, h.get("resolvedPages") or []))
            note = t("cli.batch.live") if h.get("live") else t("cli.batch.estimated") if h.get("estimated") else ""
            out.append(t("cli.batch.resolved", count=h["resolvedCount"])
                       + (t("cli.batch.resolvedPages", pages=pages) if pages else "")
                       + (t("cli.batch.leftOpen", count=h["leftOpen"]) if h.get("leftOpen") else "") + note)
        if h.get("summary"):
            out.append(t("cli.batch.summary", summary=h["summary"]))
        if h.get("draftedCount"):
            out.append(t("cli.batch.drafted", count=h["draftedCount"],
                         list=sep.join(t("cli.batch.draftedItem", id=x) for x in h.get("draftedIds") or [])))
        for nid in h.get("resolvedIds") or []:
            n = by_id.get(nid)
            if not n:
                out.append(t("cli.batch.deleted", id=nid))
                continue
            body = str(n.get("text", "")).replace("\n", " ")
            line = t("cli.batch.noteLine", id=nid, page=n.get("page"),
                     target=_en_target(cli_catalog(lang), n.get("target")),
                     body=body[:60] + ("…" if len(body) > 60 else ""))
            if n.get("status") != "resolved":
                line += t("cli.batch.reopened")
            elif n.get("resolution"):
                line += t("cli.batch.resolution", text=n["resolution"])
            if n.get("trail"):
                line += t("cli.batch.trail", text=trail_text(n, lang=lang))
            out.append(line)
    if not hist:
        out.append(t("cli.batch.empty"))
    return "\n".join(out) + "\n"


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
            heads = page_heads(md)   # 中英页头都认，与界面语言无关
            for k, h in enumerate(heads):
                body = md[h.end(): heads[k + 1].start() if k + 1 < len(heads) else len(md)]
                # 只删认得出来的字段名：认不出的按正文显示（与对照页一致），不然对应句提示会对不上
                body = FIELD_LABEL.sub(lambda m: "\n" if field_name(m.group(1)) else m.group(0), body).replace("**", "")
                out["讲稿"][int(h.group(1))] = h.group(2) + "\n" + body
    except (OSError, ValueError, SystemExit):
        pass
    return out


MATCH_ZH = {"exact": "相同", "similar": "相近"}


STYLE_ZH = {"page": "整页", "pin": "标点处", "region": "框选处"}


def mark_tag(n: dict[str, Any], no: int | None) -> str:
    """报告上的标注：点写坐标，框写左上角坐标与宽高（都是相对页面的百分比）。"""
    out = ""   # 0.3 留下的批注可能点和框都有：两个都列出
    if n.get("pin"):
        out += f"·标点 {no}（x {n['pin']['x']}%，y {n['pin']['y']}%）"
    rg = store.region_of(n)
    if rg:
        out += f"·框选 {no}（x {rg.get('x')}%，y {rg.get('y')}%，宽 {rg.get('w')}%，高 {rg.get('h')}%）"
    return out


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


def _en_target(cat: dict, value: object) -> str:
    key = {"报告": "viewer.target.report", "讲稿": "viewer.target.script",
           "报告和讲稿": "viewer.target.both"}.get(str(value or ""))
    if key:
        return i18n.translate(cat, key)
    if value:
        return str(value)
    return i18n.translate(cat, "viewer.target.unset")


def _to_markdown_en(doc: dict[str, Any], manifest: dict[str, Any], *, include_resolved: bool, page: int | None,
                    sources: dict[str, Path | None] | None = None, handoff: dict[str, Any] | None = None,
                    only: set[str] | None = None, always: list[dict[str, str]] | None = None) -> str:
    """英文阅读输出。批注原文、引用原文和 id 不翻译；JSON 导出仍走原始对象。"""
    cat = i18n.load_catalog("en")

    def t(key: str, **params: object) -> str:
        return i18n.translate(cat, key, **params)

    all_notes = doc["notes"]
    titles = {p["n"]: p.get("title", "") for p in manifest.get("pages", [])}
    keys = {p["n"]: p.get("key", "") for p in manifest.get("pages", [])}
    notes = [n for n in all_notes if (include_resolved or n.get("status") != "resolved")
             and (page is None or n.get("page") == page) and (only is None or n.get("id") in only)]
    notes.sort(key=lambda n: (int(n.get("page", 0) or 0), str(n.get("at", ""))))
    n_open = sum(n.get("status", "open") == "open" for n in all_notes)
    n_draft = sum(n.get("status") == "drafted" for n in all_notes)
    n_resolved = len(all_notes) - n_open - n_draft
    title = manifest.get("title") or "Review"
    counts = [t("cli.show.open", count=n_open)]
    if n_draft:
        counts.append(t("cli.show.drafted", count=n_draft))
    counts.append(t("cli.show.resolved", count=n_resolved))
    head = [
        "# " + t("cli.show.heading", title=str(title)),
        t("cli.show.count", count=len(all_notes))
        + " (" + ", ".join(counts) + "); "
        + t("cli.show.listing", count=len(notes)) + f". rev {doc.get('rev')}",
    ]
    if sources:
        parts = []
        for kind, path in sources.items():
            if not path:
                continue
            label = t("cli.show.source.report" if kind == "report" else "cli.show.source.script")
            parts.append(f"{label} = {path}")
        if parts:
            head.append(t("cli.show.sources", list="; ".join(parts)))
    if always:
        head.append(t("cli.hand.alwaysHead", list=always_text(t, always)))
    head += handoff_lines(handoff, "en")   # 提交与要求：与页面同一含义，原始附言保持原文
    out = ["\n".join(head)]
    last = None
    for n in notes:
        if n["page"] != last:
            last = n["page"]
            page_title = titles.get(last) or n.get("title", "")
            suffix = f" (key {keys[last]})" if keys.get(last) else ""
            out.append(f"\n## {t('cli.show.pageWord', page=last)} | {page_title}{suffix}")
        state = ""
        if n.get("status") == "resolved":
            state = f"[{t('cli.status.resolved')}] "
        elif n.get("status") == "drafted":
            state = f"[{t('cli.status.drafted')}] "
        body = str(n.get("text", ""))
        line = f"- `{n['id']}` {state}[{_en_target(cat, n.get('target'))}] " + body.replace("\n", "\n  ")
        if n.get("quote"):
            origin = f" ({_en_target(cat, n['quoteFrom'])})" if n.get("quoteFrom") else ""
            line += f"\n  > {t('cli.show.quote')}{origin}: {n['quote']}"
        # 处理说明与痕迹保持原文：Agent 要能看出上一批改了什么、谁改的
        if n.get("status") in ("resolved", "drafted") and n.get("resolution"):
            line += t("cli.batch.resolution", text=n["resolution"])
        if n.get("trail"):
            line += t("cli.batch.trail", text=trail_text(n, lang="en"))
        out.append(line)
    if not notes:
        out.append("\n" + t("cli.show.emptyList"))
    return "\n".join(out) + "\n"


def to_markdown(doc: dict[str, Any], manifest: dict[str, Any], *, include_resolved: bool, page: int | None,
                sources: dict[str, Path | None] | None = None, handoff: dict[str, Any] | None = None,
                only: set[str] | None = None, always: list[dict[str, str]] | None = None,
                locale: str | None = None) -> str:
    lang = locale if locale in i18n.SUPPORTED else i18n.DEFAULT_LOCALE
    if lang == "en":
        return _to_markdown_en(doc, manifest, include_resolved=include_resolved, page=page, sources=sources,
                               handoff=handoff, only=only, always=always)
    t2 = cli_text(lang)
    texts = side_texts(sources)
    all_notes = doc["notes"]
    marks = mark_numbers(all_notes)
    titles = {p["n"]: p.get("title", "") for p in manifest.get("pages", [])}
    keys = {p["n"]: p.get("key", "") for p in manifest.get("pages", [])}
    notes = [n for n in all_notes if (include_resolved or n.get("status") != "resolved")
             and (page is None or n.get("page") == page) and (only is None or n.get("id") in only)]
    notes.sort(key=lambda n: (int(n.get("page", 0) or 0), str(n.get("at", ""))))
    n_open = sum(n.get("status", "open") == "open" for n in all_notes)
    n_draft = sum(n.get("status") == "drafted" for n in all_notes)
    head = [f"# 批注 · {manifest.get('title') or '对照页'}",
            f"共 {len(all_notes)} 条（未处理 {n_open}，" + (f"已初改·待复核 {n_draft}，" if n_draft else "")
            + f"已处理 {len(all_notes) - n_open - n_draft}）；"
            f"以下列出 {'全部' if include_resolved else '未处理'} {len(notes)} 条。rev {doc.get('rev')}",
            "报告上的标注：标点是一个点，框选是一块区域；坐标为相对页面左上角的百分比（横向 x、纵向 y，宽、高）。"]
    if sources:  # 已按 review.json 解析成实际路径（相对审阅目录优先），改稿就改这里列出的文件
        head.append("源文件（改稿改这里）：" + "；".join(f"{'报告' if k == 'report' else '讲稿'} = {v}"
                                                   for k, v in sources.items() if v))
    if always:
        head.append(t2("cli.hand.alwaysHead", list=always_text(t2, always)))
    head += handoff_lines(handoff, lang)
    out = ["\n".join(head)]
    last = None
    for n in notes:
        if n["page"] != last:
            last = n["page"]
            out.append(f"\n## 第 {last} 页｜{titles.get(last) or n.get('title', '')}" + (f"（键 {keys[last]}）" if keys.get(last) else ""))
        tag = n.get("target") or "未指定"
        tag += mark_tag(n, marks.get(n["id"]))
        state = {"resolved": "【已处理】", "drafted": "【已初改·待复核】"}.get(n.get("status"), "")
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
            tag += "·风格优化·" + STYLE_ZH.get(st.get("scope"), "整页")
        line = f"- `{n['id']}` {state}【{tag}】" + body.replace("\n", "\n  ")
        if n.get("quote") and not ed:
            line += f"\n  > 引用{('（' + n['quoteFrom'] + '）') if n.get('quoteFrom') else ''}：{n['quote']}"
        if n.get("status") in ("resolved", "drafted") and n.get("resolution"):
            line += f"\n  {'初改说明' if n.get('status') == 'drafted' else '处理说明'}：{n['resolution']}"
        if n.get("trail"):
            line += f"\n  痕迹：{trail_text(n)}"
        if n.get("orphan"):
            line += f"\n  ⚠ 这条批注原来所在的页面（键 {n.get('key')}）在新版报告里找不到，页码可能不对：先确认它对应哪一页"
        elif n.get("key") and keys.get(n.get("page")) and n["key"] != keys[n["page"]]:
            line += f"\n  ⚠ 批注记的页面键 {n['key']} 与第 {n['page']} 页（{keys[n['page']]}）不一致，重建对照页可自动校正"
        if n.get("anchor"):
            line += f"\n  位置线索：{n['anchor']}"
        sk = store.skills_for(n, always or []) if n.get("status") != "resolved" else [(x, "这条指定") for x in n.get("skills") or []]
        if sk:
            line += "\n  技能：" + "、".join(f"{nm}（{src}）" for nm, src in sk) + "——处理这条前先调用这些技能"
        for extra in pair_lines(n, texts):
            line += "\n" + extra
        out.append(line)
    if not notes:
        out.append("\n（没有符合条件的批注）")
    return "\n".join(out) + "\n"


# 旧版对照页导出的 Markdown：
# ## 第 7 页｜标题
# 1. 【报告·标点 1（x 12.3%，y 45.6%）】意见……
# 2. 【报告·框选 2（x 10%，y 20%，宽 30%，高 15%）】意见……
#    > 引用（报告）：……
DELETE_TEXT = store.DELETE_TEXT
REPLACE_PREFIX = store.REPLACE_PREFIX
LEGACY_PAGE = re.compile(r"^## 第 (\d+) 页｜(.*)$")
LEGACY_ITEM = re.compile(r"^\d+\. 【([^】·]*)(?:·标点 \d+（x ([\d.]+)%，y ([\d.]+)%）)?"
                         r"(?:·框选 \d+（x ([\d.]+)%，y ([\d.]+)%，宽 ([\d.]+)%，高 ([\d.]+)%）)?】(.*)$")
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
            text, status = m.group(8), "open"
            if text.startswith("【已处理】"):
                text, status = text[len("【已处理】"):], "resolved"
            elif text.startswith("【已初改·待复核】"):
                text, status = text[len("【已初改·待复核】"):], "drafted"
            cur = {"page": page, "title": title, "target": m.group(1), "text": text, "status": status,
                   "pin": {"x": float(m.group(2)), "y": float(m.group(3))} if m.group(2) else None}
            if m.group(4):
                cur["region"] = {k: float(m.group(g)) for k, g in zip(("x", "y", "w", "h"), (4, 5, 6, 7))}
            items.append(cur)
        elif cur is not None and (m := LEGACY_QUOTE.match(line)):
            cur["quoteFrom"], cur["quote"] = m.group(1) or "", m.group(2)
            # 页面导出的快捷修改：正文是固定说法，引用是原文
            if cur["text"] == DELETE_TEXT:
                cur["edit"] = {"op": "delete", "from": cur["quote"]}
            elif cur["text"].startswith(REPLACE_PREFIX):
                cur["edit"] = {"op": "replace", "from": cur["quote"], "to": cur["text"][len(REPLACE_PREFIX):]}
        elif cur is not None and (m := re.match(r"^\s+(?:处理|初改)说明：(.*)$", line)):
            cur["resolution"] = m.group(1)
        elif cur is not None and (m := re.match(r"^\s+风格优化：(整页|标点处|框选处|区域（x ([\d.]+)%，y ([\d.]+)%，宽 ([\d.]+)%，高 ([\d.]+)%）)$", line)):
            if m.group(2):   # 0.3 导出的格式：框写在风格优化这一行
                cur["region"] = {"x": float(m.group(2)), "y": float(m.group(3)), "w": float(m.group(4)), "h": float(m.group(5))}
            cur["style"] = {"scope": {"整页": "page", "标点处": "pin"}.get(m.group(1), "region")}
        elif cur is not None and (m := re.match(r"^\s+位置线索：(.*)$", line)):
            cur["anchor"] = m.group(1)
        elif cur is not None and (m := re.match(r"^\s+技能：(.*)$", line)):
            body = re.sub(r"(——处理这条前先调用这些技能|（处理这条前先调用这些技能）)$", "", m.group(1).strip())
            own = [x for x in re.split(r"[,，、;；]+", body) if not re.search(r"（常驻·[^）]*）$", x.strip())]
            cur["skills"] = store.clean_skill_names([re.sub(r"（这条指定）$", "", x.strip()) for x in own])
        elif cur is not None and (m := re.match(r"^\s+↔ 另一侧（(报告|讲稿)(?:·(相同|相近))?）：(.*)$", line)):
            cur["pair"] = {"side": m.group(1), "match": "exact" if m.group(2) == "相同" else "similar", "text": m.group(3)}
        elif cur is not None and line.startswith("   ") and line.strip():
            cur["text"] += "\n" + line[3:]
    return items


ALWAYS_ZH_REV = {"全部批注": "all", "全部": "all", "报告": "报告", "讲稿": "讲稿", "风格优化": "style"}


def parse_always_header(text: str) -> list[dict[str, str]] | None:
    """页面导出的文字开头「常驻技能（…）：a（讲稿）、b（全部批注）」；没有这行返回 None。"""
    m = re.search(r"^常驻技能（[^）]*）：(.+)$", text, re.M)
    if not m:
        return None
    out = []
    for part in re.split(r"、", m.group(1)):
        mm = re.match(r"^(.+?)（(全部批注|全部|报告|讲稿|风格优化)）$", part.strip())
        if mm:
            out.append({"name": mm.group(1), "scope": ALWAYS_ZH_REV[mm.group(2)]})
    return out


def read_import(path: Path) -> tuple[list[dict[str, Any]], list[dict[str, str]] | None, str | None]:
    """返回 (批注, 常驻技能, 来源审阅标识)。常驻技能只有导出里带了才不是 None（本地文件模式设的常驻技能随之带过来）；
    来源审阅标识取自页面导出的 JSON（reviewId），用来防止导进别的审阅。"""
    text = path.read_text(encoding="utf-8")
    stripped = text.lstrip()
    if stripped.startswith("{") or stripped.startswith("["):
        data = json.loads(text)
        if isinstance(data, dict):
            al = data.get("alwaysSkills")
            rid = data.get("reviewId")
            return (data.get("notes", []), (al if isinstance(al, list) else None),
                    rid if isinstance(rid, str) and ledger.RID.fullmatch(rid) else None)
        return data, None, None
    items = parse_legacy_markdown(text)
    if not items:
        raise SystemExit(f"{path} 里没找到批注：既不是 JSON，也不是对照页导出的 Markdown 格式")
    return items, parse_always_header(text), None


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="notes.py", description="读写审阅目录里的批注")
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("show", help="列出批注（默认只列未处理）")
    s.add_argument("dir", type=str, help="审阅目录或审阅标识")
    s.add_argument("--all", action="store_true", help="包含已处理")
    s.add_argument("--page", type=int)
    s.add_argument("--json", action="store_true", help="输出原始 JSON")
    s.add_argument("--locale", help="界面语言：auto、zh-CN 或 en。优先于 AE_LOCALE 和审阅设置")
    s.add_argument("--batch", type=int, help="只看第 N 批涉及的批注（提交时未处理的、这批初改或处理过的）")
    r = sub.add_parser("resolve", help="标记为已处理（两段流程的初改阶段记为「已初改·待复核」）")
    r.add_argument("dir", type=str, help="审阅目录或审阅标识")
    r.add_argument("ids", nargs="+")
    r.add_argument("--note", default="", help="处理说明：改了哪里、怎么改的（页面上会显示）")
    r.add_argument("--by", default=default_agent(), help="处理人，页面上显示为「已处理 · 处理人」")
    r.add_argument("--seq", type=int, help="记在第几批名下；默认按批注归属自动找（同时有几批在处理时建议写明）")
    r.add_argument("--locale", help="界面语言：auto、zh-CN 或 en。优先于 AE_LOCALE 和审阅设置")
    o = sub.add_parser("reopen", help="重新打开")
    o.add_argument("dir", type=str, help="审阅目录或审阅标识")
    o.add_argument("ids", nargs="+")
    i = sub.add_parser("import", help="导入页面导出的 JSON 或旧版 Markdown")
    i.add_argument("dir", type=str, help="审阅目录或审阅标识")
    i.add_argument("file", type=Path)
    i.add_argument("--force", action="store_true", help="文件属于另一份审阅（审阅标识不同）时也导入")
    w = sub.add_parser("wait", help="等待用户点「交给 Agent」：收到提交后标为处理中，输出提交内容与未处理批注")
    w.add_argument("dir", type=str, help="审阅目录或审阅标识")
    w.add_argument("--timeout", type=float, default=0, help="最多等多少秒，0 表示一直等（默认）")
    w.add_argument("--interval", type=float, default=1.0, help="检查间隔秒数，默认 1")
    w.add_argument("--no-claim", action="store_true", help="只看不接收（不改状态）")
    w.add_argument("--by", default=default_agent(), help="接收人，页面上显示")
    w.add_argument("--stage", choices=("any", "draft", "review"), default="any",
                   help="等哪种：draft 新提交（初改）、review 请求复核的、any 都等（默认）")
    w.add_argument("--locale", help="界面语言：auto、zh-CN 或 en。优先于 AE_LOCALE 和审阅设置")
    sk = sub.add_parser("skills", help="查看或设置技能目录（页面「技能」输入栏的候选）与常驻技能（每条批注按范围先调用）")
    sk.add_argument("dir", type=str, help="审阅目录或审阅标识")
    sk.add_argument("--set", nargs="*", metavar="名称=说明", help="整体替换为这些技能")
    sk.add_argument("--add", nargs="*", metavar="名称=说明", help="追加或更新这些技能")
    sk.add_argument("--remove", nargs="*", metavar="名称", help="移除这些技能")
    sk.add_argument("--clear", action="store_true", help="清空目录")
    sk.add_argument("--always", nargs="*", metavar="名称=范围",
                    help="整体设置常驻技能；范围：全部（默认）/ 报告 / 讲稿 / 风格优化")
    sk.add_argument("--always-add", nargs="*", metavar="名称=范围", help="追加常驻技能")
    sk.add_argument("--always-remove", nargs="*", metavar="名称", help="去掉这些常驻技能（所有范围）")
    sk.add_argument("--always-clear", action="store_true", help="清空常驻技能")
    cl = sub.add_parser("claim", help="不等待：接收最早一个等待中的提交（给自己轮询的代理用，别直接改 handoff.json）")
    cl.add_argument("dir", type=str, help="审阅目录或审阅标识")
    cl.add_argument("--by", default=default_agent())
    cl.add_argument("--stage", choices=("any", "draft", "review"), default="any")
    cl.add_argument("--seq", type=int, help="只接这一批")
    dn = sub.add_parser("done", help="收尾当前阶段：初改（按用户要求停在待复核或定稿）或复核（定稿）")
    dn.add_argument("dir", type=str, help="审阅目录或审阅标识")
    dn.add_argument("--summary", default="", help="处理结果：改了几条、哪些留给用户定")
    dn.add_argument("--seq", type=int, help="提交序号；默认最近一个处理中的提交")
    dn.add_argument("--by", default=default_agent())
    g = dn.add_mutually_exclusive_group()
    g.add_argument("--draft", action="store_true", help="初改完成、交给复核（仅当用户允许派发子代理，由复核者接力时用）")
    g.add_argument("--final", action="store_true", help="初改阶段直接定稿（用户要求停在待复核时不允许）")
    dn.add_argument("--accept-rest", action="store_true", help="复核收尾时，没给结论的初改一并通过")
    dn.add_argument("--locale", help="界面语言：auto、zh-CN 或 en。优先于 AE_LOCALE 和审阅设置")
    rv = sub.add_parser("review", help="复核结论：--ok 通过 / --revise 复核者再改 / --return 退回给用户拍板")
    rv.add_argument("dir", type=str, help="审阅目录或审阅标识")
    rv.add_argument("ids", nargs="+")
    vg = rv.add_mutually_exclusive_group(required=True)
    vg.add_argument("--ok", action="store_const", const="ok", dest="verdict")
    vg.add_argument("--revise", action="store_const", const="revise", dest="verdict")
    vg.add_argument("--return", action="store_const", const="return", dest="verdict")
    rv.add_argument("--note", default="", help="理由：再改了什么 / 为什么要用户拍板（再改、退回必填）")
    rv.add_argument("--by", default=default_agent())
    rv.add_argument("--seq", type=int, help="复核的批次；默认按批注的初改批次")
    rv.add_argument("--locale", help="界面语言：auto、zh-CN 或 en。优先于 AE_LOCALE 和审阅设置")
    df = sub.add_parser("diff", help="看初改动了源文件的哪些地方（按页列出）")
    df.add_argument("dir", type=str, help="审阅目录或审阅标识")
    df.add_argument("--batch", type=int, help="第几批；默认最近一个有快照的批次")
    df.add_argument("--to", choices=("draft", "current"), default="draft",
                    help="对比到：draft 初改收尾时（默认，没有就到当前）/ current 当前文件")
    df.add_argument("--page", type=int, help="只看第 N 页")
    df.add_argument("--locale", help="界面语言：auto、zh-CN 或 en。优先于 AE_LOCALE 和审阅设置")
    bt = sub.add_parser("batches", help="处理记录：每次「交给 Agent」一批，各批解决了哪些批注、处理摘要")
    bt.add_argument("dir", type=str, help="审阅目录或审阅标识")
    bt.add_argument("--last", type=int, help="只看最近几批")
    bt.add_argument("--json", action="store_true", help="输出原始 JSON")
    bt.add_argument("--locale", help="界面语言：auto、zh-CN 或 en。优先于 AE_LOCALE 和审阅设置")
    a = ap.parse_args(argv)
    try:
        d = ledger.resolve_dir(a.dir).resolve()   # 也接受审阅标识（或至少 4 位前缀）：从台账查目录
    except (ledger.LedgerError, OSError) as e:
        print(f"错误：{e}", file=sys.stderr)
        return 2
    # 打错目录时不要悄悄新建一个空审阅目录
    if not d.is_dir() or not any((d / f).exists() for f in ("review.json", "index.html", store.NOTES_FILE)):
        print(f"错误：{d} 不是审阅目录（里面没有 review.json / index.html / notes.json）。", file=sys.stderr)
        return 2
    warn = ledger.try_register(load_manifest(d).get("reviewId"), d)   # 顺带登记到审阅台账（目录搬家后更新路径）
    if warn:
        print(f"警告：{warn}", file=sys.stderr)

    # 提交、处理记录与批注的输出按当前语言写：--locale 优先于 AE_LOCALE，再优先于审阅里保存的选择。
    # 只输出 JSON 的 claim、以及导入与技能设置不在此列：语言选择非法时它们照旧工作
    lang = i18n.DEFAULT_LOCALE
    if a.cmd in LOCALIZED_CMDS:
        try:
            lang = cli_language(getattr(a, "locale", None), d)
        except i18n.LocaleError as exc:
            print(f"错误：{exc}", file=sys.stderr)
            return 2

    if a.cmd == "show":
        doc = store.load(d)
        manifest = load_manifest(d)
        only = batch_note_ids(d, doc["notes"], a.batch) if a.batch else None
        if a.json:
            notes = [n for n in doc["notes"] if (a.all or n.get("status") != "resolved")
                     and (a.page is None or n.get("page") == a.page) and (only is None or n.get("id") in only)]
            print(json.dumps({**doc, "notes": notes, "handoff": store.handoff_state(d), "alwaysSkills": store.always_skills(d)},
                             ensure_ascii=False, indent=2))
        else:
            try:
                sources = resolve_sources(d, manifest) if manifest else None
            except SystemExit as e:
                print(f"警告：{e}", file=sys.stderr)
                sources = None
            print(to_markdown(doc, manifest, include_resolved=a.all or bool(a.batch), page=a.page, sources=sources,
                              handoff=store.handoff_state(d, full=False), only=only, always=store.always_skills(d),
                              locale=lang), end="")
        return 0
    if a.cmd in ("resolve", "reopen"):
        status = "resolved" if a.cmd == "resolve" else "open"
        t = cli_text(lang)
        bad = 0
        for nid in a.ids:
            try:
                _doc, n = store.set_status(d, nid, status, resolution=getattr(a, "note", ""), by=getattr(a, "by", default_agent()),
                                           seq=getattr(a, "seq", None))
                print(t("cli.hand.markResult", id=nid,
                        status=label(t, NOTE_STATUS_KEY.get(n.get("status"), ""), n.get("status"))))
            except store.NotFound as e:
                print(t("cli.hand.noteMissing", id=nid) if str(e).strip(chr(39)) == nid
                      else t("cli.hand.noteNoBatch", id=nid, why=e), file=sys.stderr)
                bad += 1
            except store.NoteError as e:
                print(t("cli.hand.noteFailed", id=nid, why=e), file=sys.stderr)
                bad += 1
        return 1 if bad else 0
    if a.cmd == "import":
        items, always, src_rid = read_import(a.file)
        mine = load_manifest(d).get("reviewId")
        if src_rid and mine and src_rid != mine and not a.force:
            other = ""
            try:
                e = ledger.get(src_rid)
                other = f"；台账里它在 {e['dir']}" if e else ""
            except (ledger.LedgerError, OSError):
                pass
            print(f"错误：这个文件属于另一份审阅（标识 {src_rid}），不是 {d}（标识 {mine}）{other}。"
                  "请导入到它自己的审阅目录；确认就要导进这里，加 --force。", file=sys.stderr)
            return 2
        _doc, added, skipped, updated = store.import_notes(d, items, by="cli-import")
        print(f"导入完成：新增 {added} 条，更新 {updated} 条，跳过重复 {skipped} 条 → {d / store.NOTES_FILE}")
        if always is not None:
            try:
                new = store.clean_always(always)
            except store.NoteError as e:
                print(f"导出里的常驻技能没能读懂（{e}），请按导出开头那行用 notes.py skills --always 设置", file=sys.stderr)
                return 0
            cur = store.always_skills(d)
            if not cur or cur == new:
                store.always_save(d, new)
                print("常驻技能（随导出带来，已写入 skills.json）：" + ("、".join(f"{x['name']}（{store.ALWAYS_ZH[x['scope']]}）" for x in new) or "（无）"))
            else:
                print("注意：导出里的常驻技能与审阅目录里的不同，未自动覆盖。导出里是："
                      + "、".join(f"{x['name']}（{store.ALWAYS_ZH[x['scope']]}）" for x in new)
                      + "。确认以导出为准就运行 notes.py skills 审阅目录 --always \"名称=范围\" …", file=sys.stderr)
        return 0
    if a.cmd == "wait":
        return wait(d, a.timeout, a.interval, claim=not a.no_claim, by=a.by, stage=a.stage, lang=lang)
    if a.cmd == "skills":
        def parse(items: list[str] | None) -> list[dict[str, str]]:
            out = []
            for it in items or []:
                name, _, desc = it.partition("=")
                name = store.clean_skill_names(name)
                if name:
                    out.append({"name": name[0], "desc": desc.strip()})
            return out
        scope_of = {"全部": "all", "全部批注": "all", "all": "all", "报告": "报告", "讲稿": "讲稿", "风格优化": "style", "style": "style"}

        def parse_always(items: list[str] | None) -> list[dict[str, str]]:
            out = []
            for it in items or []:
                name, _, sc = it.partition("=")
                if sc.strip() and sc.strip() not in scope_of:
                    raise ValueError(f"常驻技能范围「{sc}」不认识，可选：全部 / 报告 / 讲稿 / 风格优化")
                out.append({"name": name, "scope": scope_of[sc.strip() or "全部"]})
            return out
        try:   # 先把参数全部解析、校验完，再写盘：出错时什么都不改
            set_al, add_al = (parse_always(a.always) if a.always is not None else None), parse_always(a.always_add)
            store.clean_always((set_al or []) + add_al)
        except (ValueError, store.NoteError) as e:
            print(f"错误：{e}", file=sys.stderr)
            return 2
        catalog_change = bool(a.clear or a.set is not None or a.add or a.remove)
        always_change = bool(set_al is not None or add_al or a.always_remove or a.always_clear)
        if catalog_change:
            try:
                cat = store.skills_update(d, set_to=parse(a.set) if a.set is not None else None,
                                          add=parse(a.add), remove=a.remove, clear=a.clear, always_set=set_al,
                                          always_add=add_al, always_remove=a.always_remove, always_clear=a.always_clear)
            except (OSError, store.NoteError) as e:
                print(f"错误：{e}", file=sys.stderr)
                return 2
        else:
            cat = store.skills_catalog(d)
        if always_change and not catalog_change:
            try:
                store.always_update(d, set_to=set_al, add=add_al, remove=a.always_remove, clear=a.always_clear)
            except store.NoteError as e:
                print(f"错误：{e}", file=sys.stderr)
                return 2
        used = sorted({x for n in store.load(d)["notes"] for x in (n.get("skills") or [])})
        print("技能目录（页面「技能」输入栏的候选）：" + ("；".join(f"{c['name']}" + (f"（{c['desc']}）" if c["desc"] else "")
                                                     for c in cat) or "（空）"))
        if used:
            print("批注里用到的技能：" + "、".join(used))
        alw = store.always_skills(d)
        print("常驻技能（每条批注按范围先调用）：" + ("、".join(f"{x['name']}（{store.ALWAYS_ZH[x['scope']]}）" for x in alw) or "（无）"))
        return 0
    if a.cmd == "claim":
        item = store.handoff_claim(d, by=a.by, stage=a.stage, seq=a.seq)
        if item is None:
            print(json.dumps({"claimed": False}, ensure_ascii=False))
            return 3
        if item["stage"] == "draft":
            snapshot(d, item, "base")
        print(json.dumps({"claimed": True, **{k: v for k, v in item.items() if k != "openIds"}}, ensure_ascii=False))
        return 0
    if a.cmd == "done":
        t = cli_text(lang)
        try:
            item = store.handoff_done(d, seq=a.seq, summary=a.summary, by=a.by,
                                      mode="draft" if a.draft else "final" if a.final else None, accept_rest=a.accept_rest)
        except store.NotFound:
            print(t("cli.hand.doneMissing"), file=sys.stderr)
            return 1
        except store.NoteError as e:
            print(t("cli.hand.doneRefused", why=e), file=sys.stderr)
            return 1
        if item["status"] in ("drafted", "review"):
            snapshot(d, item, "draft")
            print(t("cli.hand.doneDrafted", seq=item["seq"], count=item.get("draftedCount", 0))
                  + (t("cli.hand.doneStop") if item["status"] == "drafted"
                     else t("cli.hand.doneReview", dir=d)))
            return 0
        print(t("cli.hand.doneFinal", seq=item["seq"], count=item.get("resolvedCount", 0),
                left=t("cli.hand.doneLeft", count=item["leftOpen"]) if item.get("leftOpen") else ""))
        if not item.get("resolvedCount"):
            print(t("cli.hand.doneNone"), file=sys.stderr)
        return 0
    if a.cmd == "review":
        t = cli_text(lang)
        bad = 0
        for nid in a.ids:
            try:
                _doc, n = store.review(d, nid, a.verdict, note=a.note, by=a.by, seq=a.seq)
                print(t("cli.hand.reviewResult", id=nid,
                        action=label(t, ACTION_KEY[{"ok": "approved", "revise": "revised", "return": "returned"}[a.verdict]]),
                        status=label(t, NOTE_STATUS_KEY.get(n.get("status"), ""), n.get("status"))))
            except store.NotFound:
                print(t("cli.hand.noteMissing", id=nid), file=sys.stderr)
                bad += 1
            except store.NoteError as e:
                print(t("cli.hand.noteFailed", id=nid, why=e), file=sys.stderr)
                bad += 1
        return 1 if bad else 0
    if a.cmd == "diff":
        return diff_cmd(d, a.batch, a.to, a.page, lang=lang)
    if a.cmd == "batches":
        state = store.handoff_state(d)
        if state.get("error"):
            print(f"错误：{state['error']}", file=sys.stderr)
            return 1
        if a.json:
            print(json.dumps(state, ensure_ascii=False, indent=2))
        else:
            print(batches_text(state, store.load(d)["notes"], limit=a.last, lang=lang), end="")
        return 0
    return 2


def batch_note_ids(d: Path, notes: list[dict[str, Any]], seq: int) -> set[str]:
    """第 N 批涉及的批注：提交时未处理的，加上这批初改或处理过的。"""
    try:
        item = next((i for i in store.handoff_load(d)["items"] if int(i.get("seq", 0)) == seq), {})
    except (OSError, ValueError):
        item = {}
    ids = set(item.get("openIds") or [])
    for n in notes:
        if seq in (n.get("draftBatch"), n.get("resolvedBatch")) or any(
                isinstance(e, dict) and e.get("batch") == seq for e in n.get("trail") or []):
            ids.add(n.get("id"))
    return ids


# ---------- 源文件快照：初改接收时存一份（base），初改收尾时再存一份（draft），复核者据此只看改动 ----------
SNAP_DIR = ".snapshots"
SNAP_KEEP = 30


def _sources(d: Path) -> dict[str, Path | None] | None:
    manifest = load_manifest(d)
    try:
        return resolve_sources(d, manifest) if manifest else None
    except SystemExit:
        return None


SNAP_MAX = 50 * 1024 * 1024


def snapshot(d: Path, item: dict[str, Any], label: str) -> Path | None:
    """两段批次：把报告与讲稿源文件存进审阅目录 .snapshots/batch-N/<label>/（仅本机、目录 0700、文件 0600）。
    单段批次不存。读不到源文件、遇到符号链接或其他错误只警告，不影响批次状态（批次已经接收 / 收尾了）。"""
    if not store.two_stage(item):
        return None
    try:
        return _snapshot(d, int(item["seq"]), label)
    except (OSError, ValueError) as e:
        print(f"警告：源文件快照没存成（{type(e).__name__}: {e}）；notes.py diff 将看不到这批的改动，其他不受影响。", file=sys.stderr)
        return None


def _snapshot(d: Path, seq: int, label: str) -> Path | None:
    sources = _sources(d)
    if not sources:
        return None
    root = d / SNAP_DIR
    out = root / f"batch-{seq}" / label
    for p in (root, root / f"batch-{seq}", out):
        if p.is_symlink():
            raise ValueError(f"{p} 是符号链接，拒绝写入")
        p.mkdir(exist_ok=True)
        os.chmod(p, 0o700)
    for kind, src in sources.items():
        if src and src.is_file():
            if src.stat().st_size > SNAP_MAX:
                raise ValueError(f"{src} 超过 {SNAP_MAX // 1024 // 1024} MB")
            write_private(out / kind, src.read_text(encoding="utf-8"))
    olds = sorted((p for p in root.glob("batch-*") if p.name[6:].isdigit()), key=lambda p: int(p.name[6:]))
    for p in olds[:-SNAP_KEEP]:   # 只留最近几十批
        for f in sorted(p.rglob("*"), reverse=True):
            f.unlink() if f.is_file() else f.rmdir()
        p.rmdir()
    return out


def _parts(kind: str, text: str) -> list[tuple[str, int, str]]:
    """把源文件按页切开：[(匹配键, 页码, 内容)]。报告按 data-key（没有就页码），讲稿按页码；切不开就整份。"""
    try:
        if kind == "report":
            return [(f"k:{p['key']}" if p.get("key") else f"n:{p['n']}", int(p["n"]), p["html"]) for p in find_pages(text)]
        heads = page_heads(text)   # 中英页头都认，与界面语言无关
        if heads:
            return [(f"n:{h.group(1)}", int(h.group(1)), text[h.start(): heads[k + 1].start() if k + 1 < len(heads) else len(text)])
                    for k, h in enumerate(heads)]
    except (SystemExit, ValueError):
        pass
    return [("all", 0, text)]


def diff_cmd(d: Path, seq: int | None, to: str, page: int | None, lang: str | None = None) -> int:
    t = cli_text(lang)
    root = d / SNAP_DIR
    if seq is None:   # 最近一个有接收时快照的批次
        have = sorted((int(p.name[6:]) for p in root.glob("batch-*") if p.name[6:].isdigit() and (p / "base").is_dir()), reverse=True)
        if not have:
            print(t("cli.batch.diffNoSnapshot"), file=sys.stderr)
            return 1
        seq = have[0]
    base = root / f"batch-{seq}" / "base"
    if not base.is_dir():
        print(t("cli.batch.diffNoBatch", seq=seq), file=sys.stderr)
        return 1
    after_dir = root / f"batch-{seq}" / "draft"
    use_current = to == "current" or not after_dir.is_dir()
    sources = _sources(d) or {}
    side = {"report": "cli.show.source.report", "script": "cli.show.source.script"}
    print(t("cli.batch.diffHead", seq=seq,
            range=t("cli.batch.diffRangeNow" if use_current else "cli.batch.diffRangeDraft")))
    any_change = False
    for kind in ("report", "script"):
        if not (base / kind).is_file():
            continue
        before = (base / kind).read_text(encoding="utf-8")
        src = sources.get(kind) if use_current else after_dir / kind
        if not src or not Path(src).is_file():
            print(t("cli.batch.diffMissing", side=label(t, side[kind], kind)))
            continue
        after = Path(src).read_text(encoding="utf-8")
        if before == after:
            continue
        old = {k: (n, t) for k, n, t in _parts(kind, before)}
        for key, n, text in _parts(kind, after):
            if page is not None and n != page:
                continue
            prev = old.pop(key, (n, ""))[1]
            if prev != text:
                any_change = True
                print(t("cli.batch.diffPage", side=label(t, side[kind], kind), page=n,
                        pageKey=t("cli.batch.diffKey", pageKey=key[2:]) if key.startswith("k:") else ""))
                print("\n".join(difflib.unified_diff(prev.splitlines(), text.splitlines(),
                                                     t("cli.batch.diffBefore"), t("cli.batch.diffAfter"),
                                                     lineterm="", n=2)))
        for key, (n, text) in old.items():
            if page is None or n == page:
                any_change = True
                print(t("cli.batch.diffRemoved", side=label(t, side[kind], kind), page=n))
    if not any_change:
        print(t("cli.batch.diffNone"))
    return 0


def _drop_wait_mark(mark: Path | None) -> None:
    """删掉自己留下的标记（文件名带本进程 pid，不会删到别的 wait 的）。"""
    if mark is not None:
        with contextlib.suppress(OSError):
            mark.unlink()


def wait(d: Path, timeout: float, interval: float, *, claim: bool, by: str, stage: str = "any",
         lang: str | None = None) -> int:
    """阻塞等待「交给 Agent」提交。已经在等待中的提交（之前没人接收）会立即返回。"""
    t = cli_text(lang)
    sep = t("cli.hand.itemSep")
    state = store.handoff_state(d, full=False)
    if state.get("claimed") or state.get("reviewing"):
        print(t("cli.hand.waitHint", count=state.get("claimed", 0) + state.get("reviewing", 0)), file=sys.stderr)
    want = {"draft": ("pending",), "review": ("review",), "any": ("pending", "review")}[stage]
    deadline = time.monotonic() + timeout if timeout > 0 else None
    mark: Path | None = ledger.wait_file(d, os.getpid())   # 让 serve.py list 看得出「有代理在等这份审阅」
    try:
        write_private(mark, json.dumps({"pid": os.getpid(), "stage": stage, "since": store.now_iso()}) + "\n")
    except OSError:
        mark = None

    def _stop(*_a: Any) -> None:   # 后台任务被杀（SIGTERM / SIGHUP）也要走 finally 删标记
        raise KeyboardInterrupt
    old_handlers = {}
    for sig in (signal.SIGTERM, signal.SIGHUP):
        with contextlib.suppress(ValueError, OSError):   # 不在主线程时设不了，靠 pid 存活判断兜底
            old_handlers[sig] = signal.signal(sig, _stop)
    try:
        while True:
            try:
                doc = store.handoff_load(d)
            except (OSError, ValueError):
                doc = {"items": []}
            if any(i.get("status") in want for i in doc["items"]):
                if claim:
                    item = store.handoff_claim(d, by=by, stage=stage)
                else:
                    item = next(i for i in doc["items"] if i.get("status") in want)
                    item = {**item, "stage": "review" if item.get("status") == "review" else "draft"}
                if item is not None:
                    break
            if deadline is not None and time.monotonic() >= deadline:
                print(t("cli.hand.timeout", seconds=f"{timeout:g}"))
                return 3
            time.sleep(max(0.2, interval))
    except KeyboardInterrupt:
        return 130
    finally:
        _drop_wait_mark(mark)
        for sig, h in old_handlers.items():
            with contextlib.suppress(ValueError, OSError):
                signal.signal(sig, h)
    manifest = load_manifest(d)
    try:
        sources = resolve_sources(d, manifest) if manifest else None
    except SystemExit as e:
        print(f"警告：{e}", file=sys.stderr)
        sources = None
    if claim and item["stage"] == "draft":
        snapshot(d, item, "base")
    sub_line = t("cli.hand.subLine.ok" if item.get("allowSubagents") else "cli.hand.subLine.no")
    if item["stage"] == "review":
        print(t("cli.hand.reviewHead", seq=item["seq"], by=item.get("claimedBy") or t("cli.hand.agentFallback")))
        if item.get("reviewMessage"):
            print(t("cli.hand.reviewNote", message=item["reviewMessage"]))
        if message := store.handoff_user_message(item):
            print(t("cli.hand.submitNote", message=message))
        if item.get("summary"):
            print(t("cli.hand.draftSummary", summary=item["summary"]))
        print(sub_line)
        if item.get("fullCheck"):
            print(t("cli.hand.fullCheck").lstrip("；;").lstrip())
        print(t("cli.hand.reviewerBrief", dir=d, seq=item["seq"]) + "\n")
        doc = store.load(d)
        print(to_markdown(doc, manifest, include_resolved=False, page=None, sources=sources,
                          only=batch_note_ids(d, doc["notes"], item["seq"]), always=store.always_skills(d),
                          locale=lang), end="")
        return 0
    pages = sep.join(str(p) for p in item.get("pages") or []) or t("cli.hand.noPages")
    print(t("cli.hand.head", seq=item["seq"], at=item.get("at")))
    if message := store.handoff_user_message(item):
        print(t("cli.hand.userMessage", message=message))
    print(t("cli.batch.requirement", target=target_text(item, lang)))
    print(sub_line)
    alw = store.always_skills(d)
    if alw:
        print(t("cli.hand.alwaysLine", list=always_text(t, alw)))
    need = sorted({x for n in store.load(d)["notes"] if n.get("status", "open") == "open" for x in (n.get("skills") or [])})
    if need:
        print(t("cli.hand.moreSkills", list=sep.join(need)))
    print(t("cli.hand.openCount", count=item.get("openCount"), pages=pages)
          + (t("cli.hand.claimedNote") if claim else t("cli.hand.noClaimNote")))
    end_hint = t("cli.hand.endReview" if item.get("target") == "review" else "cli.hand.endFinal")
    print(t("cli.hand.finishLine", dir=d, seq=item["seq"], end=end_hint) + "\n")
    doc = store.load(d)
    mine = {n["id"] for n in doc["notes"] if n.get("id") in set(item.get("openIds") or []) and n.get("status", "open") == "open"}
    others = [n for n in doc["notes"] if n.get("id") in set(item.get("openIds") or []) and n.get("status") == "drafted"]
    if others:   # 提交后被别的批次初改了的：等那一批复核，不要动
        print(t("cli.hand.otherBatch", list=sep.join(
            t("cli.hand.otherBatchItem", id=n["id"], batch=n.get("draftBatch")) for n in others)) + "\n")
    print(to_markdown(doc, manifest, include_resolved=False, page=None, sources=sources, only=mine,
                      always=alw, locale=lang), end="")
    return 0


if __name__ == "__main__":
    sys.exit(main())
