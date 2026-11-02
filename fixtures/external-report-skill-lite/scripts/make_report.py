#!/usr/bin/env python3
"""synthetic-brief-lite：合成外部生成技能的精简变体脚本（仅用于测试与评测，issue #74）。

代表「能力有限的外部方法」：版式自己的简报风格，但**不带来源标注、计算口径与
材料哈希**——这些局限写进记录的 gaps 与 method（如实披露），产物交给 AE 的
report_check.py 做同一验收，缺依据会被如实降级为不可交付。只用 Python 标准库。

用法：
    python3 make_report.py --materials 材料.md --out 输出目录 [--title 标题]

退出码：0 成功；2 材料里找不到需要的数字（如实失败，不补造事实）。
"""
from __future__ import annotations

import argparse
import datetime
import hashlib
import json
import re
import sys
from pathlib import Path

SKILL_NAME = "synthetic-brief-lite"
SKILL_VERSION = "0.3.0"

CHECK_IDS = (
    ("content.requirements", "content"), ("content.claims", "content"),
    ("content.fact-vs-inference", "content"), ("content.units-dates", "content"),
    ("content.numbers-charts", "content"), ("content.cross-page", "content"),
    ("content.script-consistency", "content"), ("content.open-questions", "content"),
    ("structure.flow", "structure"), ("structure.headings", "structure"),
    ("structure.claim-support", "structure"), ("structure.dup-gaps", "structure"),
    ("structure.length", "structure"), ("visual.pages", "visual"),
)

CSS = """
body{margin:0;font-family:"Helvetica Neue",Arial,sans-serif;background:#fff4e6}
.page{width:1280px;height:720px;padding:56px;box-sizing:border-box;position:relative;background:#fff}
.xs-banner{display:inline-block;background:#e8590c;color:#fff;padding:8px 18px;border-radius:6px;
 font-size:14px;letter-spacing:.08em;margin-bottom:24px}
.xs-kpi{font-size:56px;font-weight:700;color:#e8590c;margin:12px 0}
"""


def die(msg: str) -> None:
    print(f"{SKILL_NAME}: {msg}", file=sys.stderr)
    sys.exit(2)


def sha_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def main() -> int:
    ap = argparse.ArgumentParser(add_help=False)
    ap.add_argument("--materials", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--title", default="三季度简报（synthetic-brief-lite 合成）")
    args = ap.parse_args()
    src = Path(args.materials)
    try:
        text = src.read_text(encoding="utf-8")
    except OSError as exc:
        die(f"材料读不到：{exc}")
    cur = re.search(r"三季度收入\s*(\d+)\s*万元", text)
    prev = re.search(r"上年同期收入\s*(\d+)\s*万元", text)
    if not (cur and prev):
        die("材料里找不到「三季度收入 N 万元 / 上年同期收入 N 万元」")
    cur_v, prev_v = int(cur.group(1)), int(prev.group(1))
    pct = (cur_v - prev_v) / prev_v * 100
    pct_show = int(pct) if float(pct).is_integer() else round(pct, 1)
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)

    report = f"""<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
<title>{args.title}</title><style>{CSS}</style></head><body>
<section class="page" data-page="1" data-key="sum" id="page-sum">
<span class="xs-banner">SYNTHETIC-BRIEF-LITE v{SKILL_VERSION}</span>
<h2>结论：收入同比 +{pct_show}%</h2>
<p class="xs-kpi">+{pct_show}%</p>
<p>三季度收入 {cur_v} 万元，上年同期 {prev_v} 万元。</p>
<p class="ae-gap">lite 模式不提供来源标注与计算口径：结论依据需另行核对。</p>
</section>
<section class="page" data-page="2" data-key="next" id="page-next">
<span class="xs-banner">SYNTHETIC-BRIEF-LITE v{SKILL_VERSION}</span>
<h2>下一步</h2>
<p>补齐十月数据后复核趋势。</p>
<p class="ae-gap">十月周报缺失：材料未提供十月数据。</p>
</section></body></html>
"""
    script_md = f"""# 讲稿：{args.title}

### 第 1 页｜结论
**讲法**：
收入同比增长 {pct_show}%。

### 第 2 页｜下一步
**讲法**：
十月数据补齐后复核趋势。
"""
    report_path = out / "report.html"
    script_path = out / "script.md"
    record_path = out / "report-record.json"
    report_path.write_text(report, encoding="utf-8")
    script_path.write_text(script_md, encoding="utf-8")

    rel = src.name
    record = {
        "schema": "ae-report-record/1",
        "generatedAt": datetime.datetime.now().astimezone().isoformat(timespec="seconds"),
        "request": {"purpose": "季度经营汇报", "audience": "管理层",
                    "coreQuestions": ["收入为什么增长"], "length": "2 页", "script": "yes"},
        "sources": [{"path": rel, "status": "read", "coverage": "全文",
                     "note": "lite 模式不提供材料哈希"}],
        "method": {"kind": "external", "name": SKILL_NAME, "version": SKILL_VERSION,
                   "invocation": "实际运行本技能脚本 scripts/make_report.py --materials … --out … "
                                 "生成了 report.html / script.md / report-record.json",
                   "unavailable": ["来源标注", "计算口径与复算", "材料 sha256"],
                   "note": "精简变体：产物缺依据由同一核验入口如实降级"},
        "links": {"report": "report.html", "script": "script.md",
                  "sha256": {"report": sha_text(report), "script": sha_text(script_md)},
                  "sourceNote": f"{rel} 及同目录讲稿"},
        "findings": [{"claim": f"三季度收入同比增长 {pct_show}%", "type": "calculation",
                      "sources": [rel], "pages": ["sum"],
                      "note": "lite 模式不提供 refs / support / basis / calculation"}],
        "gaps": ["lite 模式不提供来源标注、计算口径与材料哈希：结论依据需另行核对",
                 "十月周报缺失：材料未提供十月数据"],
        "checks": [{"id": cid, "kind": kind, "status": "not_run", "scope": "全部", "note": ""}
                   for cid, kind in CHECK_IDS],
        "openItems": [],
        "verdict": {"draftComplete": True, "deliverable": None},
    }
    record_path.write_text(json.dumps(record, ensure_ascii=False, indent=2) + "\n",
                           encoding="utf-8")
    print(f"{SKILL_NAME} v{SKILL_VERSION}: wrote report.html script.md report-record.json "
          f"(style marker: xs-lite)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
