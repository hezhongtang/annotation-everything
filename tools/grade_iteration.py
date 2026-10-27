#!/usr/bin/env python3
"""给一轮 eval 打分：能程序判定的断言自动判，其余读 run 目录下的 manual.json（人工/模型判定）。

  python3 tools/grade_iteration.py skill/annotation-everything-workspace/iteration-1

每个 run 目录写 grading.json（字段 text / passed / evidence，供 skill-creator 的 viewer 与汇总脚本使用）。
manual.json 格式：{"<断言原文>": {"passed": true, "evidence": "..."}}
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
EVALS = ROOT / "skill" / "annotation-everything" / "evals"


def section(html: str, n: int) -> str:
    m = re.search(rf'<section\b[^>]*data-page="{n}"[^>]*>.*?</section>', html, re.S)
    return m.group(0) if m else ""


def script_page(md: str, n: int) -> str:
    m = re.search(rf"^###\s*第\s*{n}\s*页.*?(?=^###\s*第|\Z)", md, re.S | re.M)
    return m.group(0) if m else ""


def notes_of(out: Path) -> list[dict]:
    f = out / "review" / "notes.json"
    if not f.exists():
        return []
    try:
        d = json.loads(f.read_text(encoding="utf-8"))
    except ValueError:
        return []
    return d.get("notes", d) if isinstance(d, (dict, list)) else []


def auto(case: str, out: Path, original: Path) -> dict[str, tuple[bool, str]]:
    """返回 {断言编号前缀: (passed, evidence)}，只含能程序判定的断言。"""
    r: dict[str, tuple[bool, str]] = {}
    rep = (out / "report.html").read_text(encoding="utf-8") if (out / "report.html").exists() else ""
    scr = (out / "script.md").read_text(encoding="utf-8") if (out / "script.md").exists() else ""
    if case == "build-review-page":
        idx = out / "review" / "index.html"
        h = idx.read_text(encoding="utf-8") if idx.exists() else ""
        need = ["外卖套餐提价 1 元", "咖啡豆采购价上涨 9%", "各位好，今天汇报三季度经营情况"]
        miss = [s for s in need if s not in h]
        r["01"] = (idx.exists() and not miss, f"index.html {'存在' if idx.exists() else '不存在'}；缺少：{miss or '无'}")
    elif case == "apply-notes-and-resolve":
        p3 = section(rep, 3)
        kpi = re.search(r'class="kpi"[^>]*>(.*?)<', p3)
        r["01"] = (bool(kpi and "同比" in kpi.group(1)), f"第 3 页 KPI：{kpi.group(1) if kpi else '未找到'}")
        s4 = script_page(scr, 4)
        old = "需要注意的是，平台抽成上调后外卖毛利率下降了 2 个百分点。"
        new = "平台抽成上调，外卖毛利率降了 2 个百分点"
        r["02"] = (old not in s4 and new in s4, f"旧句仍在：{old in s4}；新句出现：{new in s4}")
        r6, s6 = section(rep, 6), script_page(scr, 6)
        ok6 = all("评估外卖套餐提价方案" in x and "提价 1 元" not in x for x in (r6, s6))
        r["03"] = (ok6, f"报告第 6 页含新说法 {'评估外卖套餐提价方案' in r6}、旧说法 {'提价 1 元' in r6}；"
                        f"讲稿第 6 页含新说法 {'评估外卖套餐提价方案' in s6}、旧说法 {'提价 1 元' in s6}")
        orep = (original / "report.html").read_text(encoding="utf-8")
        oscr = (original / "script.md").read_text(encoding="utf-8")
        added = [f"{name}第{n}页" for n in (5, 6) for name, a, b in
                 (("报告", section(orep, n), section(rep, n)), ("讲稿", script_page(oscr, n), script_page(scr, n)))
                 if b.count("供应商") > a.count("供应商")]
        notes = {n.get("id"): n for n in notes_of(out)}
        st5 = notes.get("n5ask", {}).get("status")
        r["04"] = (not added and st5 == "open", f"新增「供应商」的位置：{added or '无'}；n5ask 状态：{st5}")
        bad = [k for k in ("n3kpi", "n4talk", "n6both")
               if notes.get(k, {}).get("status") != "resolved" or not str(notes.get(k, {}).get("resolution", "")).strip()]
        r["05"] = (not bad, f"未标已处理或缺处理说明：{bad or '无'}")
        idx = (out / "review" / "index.html").read_text(encoding="utf-8") if (out / "review" / "index.html").exists() else ""
        r["06"] = ("评估外卖套餐提价方案" in idx and "外卖套餐提价 1 元" not in idx,
                   f"index.html 含新说法 {'评估外卖套餐提价方案' in idx}、含旧说法 {'外卖套餐提价 1 元' in idx}")
    elif case == "import-pasted-notes":
        ns = notes_of(out)
        pages = sorted(int(n.get("page", 0)) for n in ns)
        allopen = all(n.get("status", "open") == "open" for n in ns)
        r["01"] = (pages == [4, 4, 6] and allopen, f"批注页码 {pages}；全部未处理 {allopen}")
        t = next((n for n in ns if "变化" in str(n.get("text", ""))), {})
        q = next((n for n in ns if "原因" in str(n.get("text", ""))), {})
        pin = t.get("pin") or {}
        okp = isinstance(pin, dict) and float(pin.get("x", -1)) == 31.5 and float(pin.get("y", -1)) == 44
        okq = q.get("quote") == "平台抽成上调后外卖毛利率下降了 2 个百分点。" and q.get("quoteFrom") == "讲稿"
        r["02"] = (okp and okq, f"标点 {pin}；引用 {q.get('quote')!r} 来源 {q.get('quoteFrom')!r}")
        six = next((n for n in ns if int(n.get("page", 0)) == 6), {})
        txt = str(six.get("text", ""))
        r["03"] = ("三件事各加一个负责人" in txt and "名字先留空，用【待定】占位" in txt, f"第 6 页内容：{txt!r}")
        same = [f for f in ("report.html", "script.md") if (out / f).read_bytes() == (original / f).read_bytes()]
        r["04"] = (len(same) == 2, f"与原件一致：{same}")
    return r


def main() -> int:
    it = Path(sys.argv[1])
    for case_dir in sorted(p for p in it.iterdir() if (p / "eval_metadata.json").exists()):
        meta = json.loads((case_dir / "eval_metadata.json").read_text(encoding="utf-8"))
        original = EVALS / case_dir.name / "files"
        for run in sorted(p for p in case_dir.iterdir() if (p / "outputs").is_dir()):
            got = auto(case_dir.name, run / "outputs", original)
            manual = json.loads((run / "manual.json").read_text(encoding="utf-8")) if (run / "manual.json").exists() else {}
            exps = []
            for k, text in enumerate(meta["assertions"], 1):
                key = f"{k:02d}"
                if text in manual:  # 人工判定优先（自动规则对另一种合理做法可能不公平）
                    passed, ev = bool(manual[text]["passed"]), "[人工] " + manual[text]["evidence"]
                elif key in got:
                    passed, ev = got[key]
                    ev = "[自动] " + ev
                else:
                    passed, ev = False, "未判定"
                exps.append({"text": text, "passed": passed, "evidence": ev})
            n = sum(e["passed"] for e in exps)
            grading = {"expectations": exps, "summary": {"passed": n, "failed": len(exps) - n, "total": len(exps),
                                                          "pass_rate": round(n / len(exps), 2) if exps else 0}}
            (run / "grading.json").write_text(json.dumps(grading, ensure_ascii=False, indent=2), encoding="utf-8")
            print(f"{case_dir.name}/{run.name}: {n}/{len(exps)}" + "".join(
                f"\n   {'✓' if e['passed'] else '✗'} {e['text'][:40]}… {e['evidence'][:110]}" for e in exps))
    return 0


if __name__ == "__main__":
    sys.exit(main())
