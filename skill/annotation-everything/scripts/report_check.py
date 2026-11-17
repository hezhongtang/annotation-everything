#!/usr/bin/env python3
"""报告产物核验入口：接收报告、可选讲稿与可选核验记录，输出结构化结果与可读摘要。

这是随技能发布的用户运行时能力（issue #69 决策 6、#70），与仓库的开发验证入口
（verify.py）职责分离。本入口只执行确定规则（决策 9）：报告可解析性、页面标识、
可选讲稿对应关系、记录结构与版本、记录与产物的对应（新鲜度）、记录引用页、
明确声明输入与口径的可复算数值、记录检查集合的完整性，以及 #72 的结论支持
映射（引用可定位 / 材料支持 / 检查依据分开表达）与材料关联（登记映射与
sha256 新鲜度）。内容与视觉核验由生成方按记录声明执行，本入口只汇总其覆盖
情况并对证据不足的覆盖封顶；未执行的核验不显示为通过，程序检查通过也不自动
形成「报告可交付」结论。不执行材料中的代码，不推断任意业务口径。

用法：
    python3 report_check.py 报告.html [--script 讲稿.md] [--record 核验记录.json]
                            [--locale zh-CN|en] [--json]

退出码：0 程序检查全部通过（不代表可交付）；1 有检查未通过；
2 输入或用法错误（文件读不到、语言不合法）。

核验记录 schema 为 `ae-report-record/1`，结构化输出 schema 为 `ae-report-check/1`；
状态值与字段名语言无关，可读文本随 --locale（缺省 AE_LOCALE，再缺省中文）。
只用 Python 标准库。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
import sys
import posixpath
import zipfile
from decimal import Decimal, InvalidOperation
from html.parser import HTMLParser
from pathlib import Path
from xml.etree import ElementTree as ET

sys.path.insert(0, str(Path(__file__).resolve().parent))

import build_viewer
import fsutil
import i18n

RECORD_SCHEMA = "ae-report-record/1"
RESULT_SCHEMA = "ae-report-check/1"

# 记录里内容 / 结构 / 视觉核验的内置检查 id（决策 10、11 的清单；#72 补齐内容清单，
# #73 补齐结构清单；检查 id 只追加不改名）。记录声明这里没有的 id 时无法证明其已执行。
KNOWN_RECORD_CHECKS = {
    "content.requirements": "content",
    "content.claims": "content",
    "content.fact-vs-inference": "content",
    "content.units-dates": "content",
    "content.numbers-charts": "content",
    "content.cross-page": "content",
    "content.script-consistency": "content",
    "content.open-questions": "content",
    "structure.flow": "structure",
    "structure.headings": "structure",
    "structure.claim-support": "structure",
    "structure.dup-gaps": "structure",
    "structure.length": "structure",
    "visual.pages": "visual",
}
# 讲稿关联的检查项（#73 适用性规则）：没有讲稿时这些项不计入覆盖总数，
# 也不要求声明；声明了 pass 就是虚假的已检查，按程序检查失败暴露。
SCRIPT_SCOPED_CHECKS = ("content.script-consistency", "structure.length")
# 记录 method.kind 的封闭取值（#74 决策 14）：实际生成方法必须声明类别与名称；
# 外部方法还须写实际调用依据（invocation）——扫描到候选或名字出现不算已采用。
METHOD_KINDS = ("builtin", "external")
ALIGNMENT_SOURCES = ("user", "reused", "delegated", "default", "inferred")
ALIGNMENT_STATUSES = ("confirmed", "pending", "conflict", "deviated")
CHECK_STATUSES = ("pass", "fail", "not_run", "unavailable")
# sources[].status 的封闭取值：材料读取登记（#69 决策 4）——格式被接受不等于内容
# 已读完，按实际读到的范围登记（#71 读取登记、#72 材料关联共用）。
SOURCE_STATUSES = ("read", "partial", "unreadable")
REQUIRED_RECORD_FIELDS = ("schema", "generatedAt", "request", "sources", "method",
                          "links", "findings", "gaps", "checks", "openItems", "verdict")

# findings[].support 的封闭取值：材料是否支持表述（决策 12：引用存在不等于受支持）。
SUPPORT_STATUSES = ("supported", "partial", "unsupported", "unverified")
# 覆盖状态的严重度序：封顶取更差值；unverified 由新鲜度或材料失配直接给出。
CAP_SEVERITY = {"covered": 0, "partial": 1, "not_run": 2, "uncovered": 3, "unverified": 4}

# 可复算运算（决策 9）：op 是封闭枚举，输入是明确数字，不执行材料中的代码。
CALC_OPS = {
    "sum": lambda v: sum(v.values()),
    "difference": lambda v: v["a"] - v["b"],
    "product": lambda v: v["a"] * v["b"],
    "ratio": lambda v: v["a"] / v["b"],
    "percent-change": lambda v: (v["b"] - v["a"]) / v["a"] * 100,
}


def t(loc: str, key: str, **params: object) -> str:
    return i18n.translate(i18n.catalog(loc), key, **params)


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


# ── 单项检查的收集器 ────────────────────────────────────────────────────────

class Checks:
    """按固定顺序收集检查结果；id 与 status 语言无关，detail 本地化。"""

    ORDER = ("report.parse", "report.pageKeys", "script.pages", "record.structure",
             "record.freshness", "record.pageRefs", "record.calculations", "record.checks",
             "record.claims", "record.reading", "record.sources",
             "record.visual", "record.length", "record.method", "record.revision",
             "record.alignment", "report.navigation", "record.deliverables")

    def __init__(self, locale: str):
        self.locale = locale
        self.warnings: list[str] = []
        self.items: dict[str, dict] = {cid: {"id": cid, "status": "not_applicable",
                                             "code": None, "detail": ""}
                                       for cid in self.ORDER}

    def name(self, cid: str) -> str:
        return t(self.locale, "check.item." + cid)

    def add_warning(self, key: str, **params: object) -> None:
        self.warnings.append(t(self.locale, key, **params))

    def set(self, cid: str, state: str, *, code: str | None = None, **params: object) -> None:
        key = {"report.parse": "check.err.reportParse",
               "report.pageKeys": "check.err.reportParse",
               "script.pages": ("check.err.scriptParse" if code == "script.parse"
                                else "check.err.scriptMismatch"),
               "record.structure": {"record.broken": "check.err.recordBroken",
                                    "record.version-unknown": "check.err.recordVersionUnknown",
                                    "record.field-missing": "check.err.recordFieldMissing",
                                    "record.bad-type": "check.err.recordBadType"}.get(
                                        code, "check.err.recordBroken"),
               "record.freshness": "check.err.stale",
               "record.pageRefs": "check.err.pageMissing",
               "record.calculations": {"record.calc-mismatch": "check.err.calcMismatch",
                                       "record.calc-unsupported-op": "check.err.calcUnsupportedOp",
                                       "record.calc-bad": "check.err.calcBad"}.get(
                                           code, "check.err.calcBad"),
               "record.checks": {"record.checks-empty": "check.err.checksEmpty",
                                 "record.check-unknown": "check.err.checkUnknown",
                                 "record.check-status": "check.err.checkStatus",
                                 "record.check-not-applicable": "check.err.checkNotApplicable"}.get(
                                     code, "check.err.checksEmpty"),
               "record.claims": {"record.claim-unsupported": "check.err.claimUnsupported",
                                 "record.claim-unlocatable": "check.err.claimUnlocatable",
                                 "record.claim-bad": "check.err.claimBad"}.get(
                                     code, "check.err.claimBad"),
               "record.sources": {"record.source-missing": "check.err.sourceMissing",
                                  "record.source-stale": "check.err.sourceStale",
                                  "record.source-bad": "check.err.sourceBad"}.get(
                                      code, "check.err.sourceBad"),
               "record.reading": {"record.source-status": "check.err.sourceStatus",
                                  "record.source-field": "check.err.sourceField",
                                  "record.reading-blocked": "check.err.readingBlocked"}.get(
                                      code, "check.err.sourceStatus"),
               "record.visual": {"record.visual-evidence": "check.err.visualEvidence",
                                 "record.visual-coverage": "check.err.visualCoverage",
                                 "record.visual-unchecked": "check.err.visualUnchecked",
                                 "record.visual-issue": "check.err.visualIssue",
                                 "record.visual-bad": "check.err.visualBad"}.get(
                                     code, "check.err.visualBad"),
               "record.length": {"record.length-mismatch": "check.err.lengthMismatch",
                                 "record.length-bad": "check.err.lengthBad"}.get(
                                     code, "check.err.lengthMismatch"),
               "record.method": {"record.method-unknown": "check.err.methodUnknown",
                                 "record.method-missing": "check.err.methodMissing",
                                 "record.method-bad": "check.err.methodBad",
                                 "record.method-invocation": "check.err.methodInvocation"}.get(
                                     code, "check.err.methodBad"),
               "record.revision": {"record.revision-bad": "check.err.revisionBad",
                                    "record.revision-unknown": "check.err.revisionUnknown",
                                    "record.revision-unrechecked": "check.err.revisionUnrechecked",
                                    "record.revision-pending": "check.err.revisionPending"}.get(
                                        code, "check.err.revisionBad"),
               "record.alignment": {"record.alignment-bad": "check.err.alignmentBad",
                                    "record.alignment-qa-bad": "check.err.alignmentQaBad",
                                    "record.alignment-conflict-pass": "check.err.alignmentConflictPass"}.get(
                                        code, "check.err.alignmentBad"),
               "record.deliverables": {
                   "record.deliverable-bad": "check.err.deliverableBad",
                   "record.deliverable-missing": "check.err.deliverableMissing",
                   "record.deliverable-stale": "check.err.deliverableStale",
                   "record.deliverable-invocation": "check.err.deliverableInvocation",
                   "record.deliverable-pages": "check.err.deliverablePages",
                   "record.deliverable-visual": "check.err.deliverableVisual",
                   "record.deliverable-consistency": "check.err.deliverableConsistency",
                   "record.deliverable-openissues": "check.err.deliverableOpenIssues",
                   "record.deliverable-powerpoint": "check.err.deliverablePowerpoint",
                   "record.deliverable-status": "check.err.deliverableStatus"}.get(
                       code, "check.err.deliverableBad"),
               "report.navigation": {"report.nav-broken-link": "check.err.navBrokenLink",
                                     "report.nav-bad-range": "check.err.navBadRange"}.get(
                                         code, "check.err.navBrokenLink"),
               }[cid]
        detail = t(self.locale, key, **params) if params else ""
        self.items[cid] = {"id": cid, "status": state, "code": code, "detail": detail}

    def failed(self) -> list[dict]:
        return [c for c in self.items.values() if c["status"] == "fail"]


# ── 报告与讲稿（复用 build_viewer 的解析约定） ──────────────────────────────

def check_report_and_script(report_doc: str, script_text: str | None, c: Checks) -> tuple[list[dict], bool]:
    """返回 (页面列表, 产物自洽)。页面列表给后续检查用。"""
    draft_ok = True
    pages: list[dict] = []
    try:
        pages = build_viewer.find_pages(report_doc, locale=c.locale)
    except build_viewer.BuildError as exc:
        c.set("report.parse", "fail", detail=str(exc))
        draft_ok = False
    else:
        c.set("report.parse", "pass")
    if not pages and c.items["report.parse"]["status"] != "fail":
        c.set("report.parse", "fail", detail=t(c.locale, "check.err.noPages"))
        draft_ok = False
    if pages:
        keys = [p["key"] for p in pages]
        dupes = sorted({k for k in keys if keys.count(k) > 1})
        if dupes:
            c.set("report.pageKeys", "fail", detail="、".join(dupes))
            draft_ok = False
        else:
            c.set("report.pageKeys", "pass")
        # find_pages 已返回每页的 stable 布尔（stable_key 的判定结果）
        derived = [p["key"] for p in pages if not p["stable"]]
        if derived:
            c.add_warning("check.warn.derivedKeys", keys="、".join(derived))
    if script_text is None:
        return pages, draft_ok
    try:
        _h1, _intro, script_pages, _warnings = build_viewer.parse_script(script_text, locale=c.locale)
    except build_viewer.ScriptError as exc:
        c.set("script.pages", "fail", code="script.parse", detail=" / ".join(exc.messages))
        return pages, False
    report_ns = [p["n"] for p in pages]
    script_ns = [p["n"] for p in script_pages]
    if report_ns != script_ns:
        missing = [n for n in report_ns if n not in script_ns]
        extra = [n for n in script_ns if n not in report_ns]
        parts = []
        if missing:
            parts.append(t(c.locale, "build.diff.onlyReport", pages="、".join(map(str, missing))).lstrip("；; "))
        if extra:
            parts.append(t(c.locale, "build.diff.onlyScript", pages="、".join(map(str, extra))).lstrip("；; "))
        c.set("script.pages", "fail", code="script.page-mismatch",
              detail=t(c.locale, "build.pageMismatch", report=len(report_ns),
                       script=len(script_ns), detail="".join(parts)))
        draft_ok = False
    else:
        c.set("script.pages", "pass")
    return pages, draft_ok


# ── 核验记录 ────────────────────────────────────────────────────────────────

def load_record(record_path: Path | None, c: Checks) -> tuple[dict | None, bool]:
    """读记录并校验结构；返回 (记录或 None, 记录可用)。"""
    if record_path is None:
        return None, False
    try:
        raw = record_path.read_bytes()
    except OSError as exc:
        c.set("record.structure", "fail", code="record.broken", reason=str(exc))
        return None, False
    try:
        record = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError, RecursionError) as exc:
        c.set("record.structure", "fail", code="record.broken", reason=str(exc))
        return None, False
    if not isinstance(record, dict):
        c.set("record.structure", "fail", code="record.broken", reason="top level is not an object")
        return None, False
    version = record.get("schema")
    if version != RECORD_SCHEMA:
        c.set("record.structure", "fail", code="record.version-unknown",
              version=version if isinstance(version, str) else repr(version),
              supported=RECORD_SCHEMA)
        return None, False
    missing = [f for f in REQUIRED_RECORD_FIELDS if f not in record]
    if missing:
        c.set("record.structure", "fail", code="record.field-missing",
              fields="、".join(missing))
        return None, False
    type_errors = []
    if "summary" in record and not isinstance(record["summary"], str):
        type_errors.append("summary")
    links = record["links"]
    if not isinstance(links, dict) or not isinstance(links.get("report"), str) \
            or not isinstance(links.get("sha256"), dict) \
            or not isinstance(links.get("sha256", {}).get("report"), str):
        type_errors.append("links{report,sha256.report}")
    for field, want in (("request", dict), ("method", dict), ("verdict", dict),
                        ("sources", list), ("findings", list), ("gaps", list),
                        ("checks", list), ("openItems", list)):
        if not isinstance(record[field], want):
            type_errors.append(field)
    if isinstance(record["sources"], list) and any(
            isinstance(source, dict) and (not isinstance(source.get("path"), str)
                                         or "\0" in source["path"])
            for source in record["sources"]):
        type_errors.append("sources[].path")
    if isinstance(record["checks"], list) and any(
            not isinstance(item, dict) or not isinstance(item.get("id"), str)
            for item in record["checks"]):
        type_errors.append("checks[].id")
    if isinstance(record["findings"], list):
        for finding in record["findings"]:
            if not isinstance(finding, dict):
                type_errors.append("findings[]")
                continue
            for field in ("sources", "pages"):
                if field in finding and (not isinstance(finding[field], list)
                        or any(not isinstance(item, str) for item in finding[field])):
                    type_errors.append("findings[]." + field)
    if type_errors:
        c.set("record.structure", "fail", code="record.bad-type", fields="、".join(type_errors))
        return None, False
    c.set("record.structure", "pass")
    return record, True


def check_freshness(record: dict | None, report_sha: str, script_sha: str | None,
                    c: Checks) -> bool:
    """记录里的产物哈希对不对得上当前文件（传入当前文件的 sha256，没有讲稿为 None）；
    返回记录是否新鲜（无记录为 False）。"""
    if record is None:
        return False
    links = record["links"]
    changed = []
    if links["sha256"]["report"] != report_sha:
        changed.append(str(links.get("report") or "report"))
    script_hash = links["sha256"].get("script")
    if script_sha is not None and script_hash != script_sha:
        changed.append(str(links.get("script") or "script"))
    if changed:
        c.set("record.freshness", "fail", code="record.stale", items="、".join(changed))
        return False
    c.set("record.freshness", "pass")
    return True


def script_declared_but_missing(record: dict | None, script_present: bool) -> bool:
    """记录声明了讲稿哈希而核验调用未收到讲稿文件（#69 审查 P2-1）。

    links.sha256.script 是对「讲稿」这份产物的声明；声明了而本次核验没有输入，
    讲稿哈希比对与逐页对应都没有执行——已声明产物的检查缺席，拦可交付并显式
    警告。没有声明哈希、真无讲稿的适用性规则（SCRIPT_SCOPED_CHECKS 不计入
    覆盖总数）不受影响。"""
    if record is None or script_present:
        return False
    script_hash = record["links"]["sha256"].get("script")
    return isinstance(script_hash, str) and bool(script_hash.strip())


def check_page_refs(record: dict | None, pages: list[dict], c: Checks) -> bool:
    if record is None:
        return True
    keys = {p["key"] for p in pages}
    referenced: list[str] = []
    for f in record["findings"]:
        if isinstance(f, dict) and isinstance(f.get("pages"), list):
            referenced.extend(x for x in f["pages"] if isinstance(x, str))
    missing = sorted({k for k in referenced if k not in keys})
    if missing:
        c.set("record.pageRefs", "fail", code="record.page-missing", keys="、".join(missing))
        return False
    c.set("record.pageRefs", "pass")
    return True


def calc_basis_missing(calc: dict) -> bool:
    """口径四要素（#72）：单位、期间、口径说明、输入位置（覆盖每个输入键）。
    任一缺失即视为口径不明 → 待核实。"""
    for field in ("unit", "period", "definition"):
        v = calc.get(field)
        if not (isinstance(v, str) and v.strip()):
            return True
    locs = calc.get("inputLocations")
    if not (isinstance(locs, dict) and locs):
        return True
    inputs = calc.get("inputs")
    keys = inputs.keys() if isinstance(inputs, dict) else ()
    for k in keys:
        v = locs.get(k)
        if not (isinstance(v, str) and v.strip()):
            return True
    return False


def check_calculations(record: dict | None, c: Checks) -> list[str]:
    """复算记录里明确声明输入与口径的数值；不执行材料中的代码。

    返回口径不明的计算标签列表（待核实）：约定支持、输入完整的计算复算并
    定位不一致；声明不全的标为待核实，不参与可交付，也不阻塞初稿。"""
    pending: list[str] = []
    if record is None:
        return pending
    failures = 0
    for f in record["findings"]:
        if not isinstance(f, dict):
            continue
        calc = f.get("calculation")
        if calc is None:
            continue
        claim = str(f.get("claim") or f.get("claim", ""))[:40] or "?"
        if not isinstance(calc, dict) or not isinstance(calc.get("op"), str) \
                or not isinstance(calc.get("inputs"), dict) \
                or not isinstance(calc.get("expected"), (int, float)) \
                or isinstance(calc.get("expected"), bool):
            c.set("record.calculations", "fail", code="record.calc-bad",
                  claim=claim, reason="malformed declaration")
            failures += 1
            continue
        op = CALC_OPS.get(calc["op"])
        if op is None:
            c.set("record.calculations", "fail", code="record.calc-unsupported-op",
                  claim=claim, op=calc["op"], supported="、".join(sorted(CALC_OPS)))
            failures += 1
            continue
        try:
            if not calc["inputs"] or any(isinstance(v, bool) or not isinstance(v, (int, float))
                                         for v in calc["inputs"].values()):
                raise ValueError("inputs are not all numbers")
            values = {k: float(v) for k, v in calc["inputs"].items()}
            expected = float(calc["expected"])
            if not math.isfinite(expected) or not all(math.isfinite(v) for v in values.values()):
                raise ValueError("nonfinite number")
        except (TypeError, ValueError, OverflowError):
            c.set("record.calculations", "fail", code="record.calc-bad",
                  claim=claim, reason="inputs and expected must be finite numbers")
            failures += 1
            continue
        try:
            actual = op(values)
        except (KeyError, ZeroDivisionError, OverflowError):
            c.set("record.calculations", "fail", code="record.calc-bad",
                  claim=claim, reason="missing or degenerate inputs")
            failures += 1
            continue
        if not math.isfinite(actual):
            c.set("record.calculations", "fail", code="record.calc-bad",
                  claim=claim, reason="calculation result is not finite")
            failures += 1
        elif abs(actual - expected) > 1e-6 * max(1.0, abs(expected)):
            shown = int(actual) if float(actual).is_integer() else round(actual, 4)
            pages = "、".join(str(p) for p in f.get("pages") or [] if isinstance(p, (str, int)))
            c.set("record.calculations", "fail", code="record.calc-mismatch",
                  claim=claim, actual=shown, expected=calc["expected"], pages=pages)
            failures += 1
        elif calc_basis_missing(calc):
            pending.append(claim)
    if pending:
        c.add_warning("check.warn.calcPending", items="、".join(dict.fromkeys(pending)))
    if failures:
        return pending
    c.set("record.calculations", "pass")
    return pending


# ── 结论支持映射（#72：引用可定位 / 材料支持 / 检查依据分开表达） ─────────────

def claim_label(f: dict) -> str:
    """结论的稳定短标签：id 语言无关，detail 里用于定位到具体结论。"""
    return str(f.get("claim") or "?")[:40]


def join_claims(items: list[str]) -> str:
    return "、".join(dict.fromkeys(items))


def valid_ref(r: object) -> bool:
    """refs[] 条目：path + locator 必填，quote / found 可选但类型要对。"""
    if not isinstance(r, dict):
        return False
    if not isinstance(r.get("path"), str) or not r["path"]:
        return False
    if not isinstance(r.get("locator"), str) or not r["locator"]:
        return False
    if "found" in r and not isinstance(r["found"], bool):
        return False
    return not ("quote" in r and not isinstance(r["quote"], str))


def analyze_claims(record: dict) -> dict:
    """对 findings 做纯分析，供结论检查与覆盖封顶共用；不产生检查结果。"""
    info = {"bad": [], "unsupported": [], "unlocatable": [], "missing_support": [],
            "unverified": [], "partial": [], "unfound_ref": [], "no_hash": [],
            "missing_evidence": []}
    sources = {s.get("path"): s for s in record["sources"] if isinstance(s, dict)}
    for f in record["findings"]:
        if not isinstance(f, dict):
            continue
        claim = claim_label(f)
        support = f.get("support")
        refs = f.get("refs")
        if support is not None and support not in SUPPORT_STATUSES:
            info["bad"].append(claim)
            continue
        if refs is not None and (not isinstance(refs, list)
                                 or any(not valid_ref(r) for r in refs)):
            info["bad"].append(claim)
            continue
        cited = f.get("sources") or []
        basis = f.get("basis")
        if not refs or not cited or not isinstance(basis, str) or not basis.strip():
            info["missing_evidence"].append(claim)
        if isinstance(refs, list) and any(r["path"] not in cited or r["path"] not in sources
                                          for r in refs):
            info["unlocatable" if support == "supported" else "unfound_ref"].append(claim)
        if support is None:
            info["missing_support"].append(claim)
        elif support == "unsupported":
            info["unsupported"].append(claim)
        elif support == "unverified":
            info["unverified"].append(claim)
        elif support == "partial":
            info["partial"].append(claim)
        if isinstance(refs, list) and any(isinstance(r, dict) and r.get("found") is False
                                          for r in refs):
            if support == "supported":
                info["unlocatable"].append(claim)
            else:
                info["unfound_ref"].append(claim)
        for p in f.get("sources") or []:
            s = sources.get(p) if isinstance(p, str) else None
            if not isinstance(s, dict) or not (isinstance(s.get("sha256"), str) and s["sha256"]):
                info["no_hash"].append(claim)
                break
    return info


def check_record_claims(info: dict, c: Checks) -> bool:
    """结论支持映射检查：support 取值合法、声明 supported 的引用可定位、
    没有「材料不支持表述」的结论。缺口类（缺声明、未核实）走警告与覆盖封顶，
    不算程序检查失败——初稿仍可进入审阅。"""
    if info["bad"]:
        c.set("record.claims", "fail", code="record.claim-bad",
              claims=join_claims(info["bad"]), reason="refs or support malformed")
        return False
    if info["unsupported"]:
        c.set("record.claims", "fail", code="record.claim-unsupported",
              claims=join_claims(info["unsupported"]))
        return False
    if info["unlocatable"]:
        c.set("record.claims", "fail", code="record.claim-unlocatable",
              claims=join_claims(info["unlocatable"]))
        return False
    c.set("record.claims", "pass")
    pending = info["missing_support"] + info["unverified"] + info["missing_evidence"]
    if pending:
        c.add_warning("check.warn.claimsPending", items=join_claims(pending))
    return True


def content_cap(record: dict | None, info: dict | None, calc_pending: list[str] | None = None,
                source_stale: bool = False) -> str | None:
    """内容核验覆盖的封顶：证据不足时，记录里声明的 pass 不能算 covered。

    uncovered > not_run > partial 取更差值；材料内容对不上（source_stale）直接
    unverified——过期证据不能为新内容背书。"""
    if record is None:
        return None
    if source_stale:
        return "unverified"
    cap: str | None = None

    def lower(level: str) -> None:
        nonlocal cap
        if cap is None or CAP_SEVERITY[level] > CAP_SEVERITY[cap]:
            cap = level

    if not [f for f in record["findings"] if isinstance(f, dict)]:
        lower("not_run")
    if info:
        if info["bad"] or info["unsupported"] or info["unlocatable"]:
            lower("uncovered")
        if (info["missing_support"] or info["unverified"] or info["unfound_ref"]
                or info["no_hash"] or info["missing_evidence"]):
            lower("not_run")
        if info["partial"]:
            lower("partial")
    if calc_pending:
        lower("partial")
    return cap


# ── 材料关联（#72：结论引用的材料登记在案，sha256 对应材料内容） ─────────────

def check_record_sources(record: dict, base_dir: Path, info: dict, c: Checks) -> bool:
    """材料关联检查：登记项合法、结论引用的材料已登记、sha256 与报告旁材料一致。

    返回是否存在过期或无法对应的材料：旧核验结果均标为 unverified。
    哈希声明了但报告旁找不到文件 → 提示无法对应（材料不在本入口的检查边界内，
    不能武断失败，但也不能静默通过）；结论引用的材料没有 sha256 → 警告并封顶。"""
    entries = record["sources"]
    if not entries:
        # 材料只在对话里（#71）：没有登记就没有可对应的材料，读取登记判不适用，
        # 关联检查同样不适用，不把缺登记伪装成已核验。
        c.set("record.sources", "not_applicable")
        return False
    bad = []
    for s in entries:
        if not isinstance(s, dict) \
                or not (isinstance(s.get("path"), str) and s["path"]) \
                or s.get("status") not in SOURCE_STATUSES:
            bad.append(str(s.get("path") or "?") if isinstance(s, dict) else "?")
    if bad:
        c.set("record.sources", "fail", code="record.source-bad", items="、".join(bad))
        return False
    registered = {s["path"] for s in entries if isinstance(s, dict)}
    cited = [p for f in record["findings"] if isinstance(f, dict)
             for p in f.get("sources") or [] if isinstance(p, str)]
    missing = sorted({p for p in cited if p not in registered})
    if missing:
        c.set("record.sources", "fail", code="record.source-missing",
              items="、".join(missing))
        return False
    stale: list[str] = []
    unresolved: list[str] = []
    for s in entries:
        if not isinstance(s, dict):
            continue
        h = s.get("sha256")
        if not (isinstance(h, str) and h):
            continue
        try:
            current = sha256_bytes((base_dir / s["path"]).read_bytes())
        except OSError:
            unresolved.append(s["path"])
            continue
        if current != h:
            stale.append(s["path"])
    if stale:
        c.set("record.sources", "fail", code="record.source-stale",
              items="、".join(stale))
        return True
    if unresolved:
        c.add_warning("check.warn.sourceUnresolved",
                      items="、".join(dict.fromkeys(unresolved)))
    if info["no_hash"]:
        c.add_warning("check.warn.sourceNoHash",
                      items=join_claims(sorted(dict.fromkeys(info["no_hash"]))))
    c.set("record.sources", "pass")
    return bool(unresolved)


def check_record_checks(record: dict | None, c: Checks,
                        script_present: bool = True) -> bool:
    """记录检查集合：非空、id 可识别、状态有效、内容与视觉两类都有声明。

    讲稿关联项（SCRIPT_SCOPED_CHECKS，#73 适用性规则）：无讲稿时不要求声明、
    不计入覆盖总数；声明了 pass 属于虚假的已检查，程序检查失败。"""
    if record is None:
        return True
    checks = record["checks"]
    if not checks:
        c.set("record.checks", "fail", code="record.checks-empty",
              reason=t(c.locale, "check.coverage.not_run"))
        return False
    unknown, bad_status = [], []
    kinds: set[str] = set()
    for item in checks:
        if not isinstance(item, dict) or not isinstance(item.get("id"), str):
            c.set("record.checks", "fail", code="record.check-status",
                  id="?", status="malformed")
            return False
        if item["id"] not in KNOWN_RECORD_CHECKS:
            unknown.append(item["id"])
        status = item.get("status")
        if status not in CHECK_STATUSES:
            bad_status.append(f"{item['id']}={status!r}")
        elif item["id"] in KNOWN_RECORD_CHECKS:
            kinds.add(KNOWN_RECORD_CHECKS[item["id"]])
    if unknown:
        c.set("record.checks", "fail", code="record.check-unknown",
              ids="、".join(sorted(unknown)))
        return False
    if bad_status:
        c.set("record.checks", "fail", code="record.check-status",
              id=bad_status[0].split("=")[0], status=bad_status[0].split("=", 1)[1])
        return False
    if not script_present:
        false_checked = [item["id"] for item in checks if isinstance(item, dict)
                         and item.get("id") in SCRIPT_SCOPED_CHECKS
                         and item.get("status") == "pass"]
        if false_checked:
            c.set("record.checks", "fail", code="record.check-not-applicable",
                  ids="、".join(false_checked))
            return False
    missing_kinds = {"content", "visual"} - kinds
    if missing_kinds:
        c.set("record.checks", "fail", code="record.checks-empty",
              reason="、".join(sorted(missing_kinds)))
        return False
    c.set("record.checks", "pass")
    return True


def check_reading(record: dict | None, c: Checks) -> dict | None:
    """读取登记检查（issue #71）：状态枚举、缺口披露完整、定位线索类型。

    返回读取汇总（语言无关的计数与未读明细）；没有记录时返回 None。
    全部实质材料都不可读时判 record.reading-blocked——空结构或虚构内容
    不能当成初稿生成完成。格式被接受不等于内容已读完。
    """
    if record is None:
        return None
    sources = record["sources"]
    if not sources:
        c.set("record.reading", "not_applicable")
        return {"read": 0, "partial": 0, "unreadable": 0, "unread": []}
    counts = {"read": 0, "partial": 0, "unreadable": 0}
    unread: list[dict] = []
    malformed = False
    for entry in sources:
        if not isinstance(entry, dict) or not isinstance(entry.get("path"), str) \
                or not entry["path"].strip():
            c.set("record.reading", "fail", code="record.source-status", entry="?",
                  status="?")
            malformed = True
            continue
        path = entry["path"]
        status = entry.get("status")
        if status not in SOURCE_STATUSES:
            c.set("record.reading", "fail", code="record.source-status", entry=path,
                  status=repr(status))
            malformed = True
            continue
        counts[status] += 1
        coverage = entry.get("coverage")
        reason = entry.get("reason")
        locations = entry.get("locations")
        bad = []
        if status == "partial" and not (isinstance(coverage, str) and coverage.strip()):
            bad.append("coverage")
        if status in ("partial", "unreadable") \
                and not (isinstance(reason, str) and reason.strip()):
            bad.append("reason")
        if locations is not None and (not isinstance(locations, list)
                                      or not all(isinstance(x, str) and x.strip()
                                                 for x in locations)):
            bad.append("locations")
        if bad:
            c.set("record.reading", "fail", code="record.source-field",
                  entry=path, fields="、".join(bad))
            malformed = True
            continue
        if status != "read":
            detail = "；".join(x for x in (coverage, reason)
                              if isinstance(x, str) and x.strip())
            unread.append({"path": path, "status": status, "detail": detail})
    if malformed:
        return {"read": counts["read"], "partial": counts["partial"],
                "unreadable": counts["unreadable"], "unread": unread}
    if counts["read"] == 0 and counts["partial"] == 0 and counts["unreadable"] > 0:
        c.set("record.reading", "fail", code="record.reading-blocked",
              n=counts["unreadable"])
        return {"read": 0, "partial": 0, "unreadable": counts["unreadable"],
                "unread": unread}
    c.set("record.reading", "pass")
    return {"read": counts["read"], "partial": counts["partial"],
            "unreadable": counts["unreadable"], "unread": unread}


def check_record_visual(record: dict, pages: list[dict], c: Checks) -> None:
    """视觉证据一致性（#73 决策 11）：声明与证据对得上，不符要能暴露。

    visual.pages 声明 pass 时必须有逐页证据结构（checks[].pages）：覆盖报告的
    全部页面、每页 checked=true、没有未解决的问题、页码与报告对应。核验入口
    不渲染页面——逐页检查由生成方（宿主 Agent 或测试中的浏览器）执行，这里只
    做确定规则判定；如实记录的 fail / not_run / unavailable 不产生程序失败，
    覆盖情况由 coverage_for 如实降级，「程序检查通过」不代替视觉检查通过。
    """
    if not pages:
        c.set("record.visual", "not_applicable")
        return
    by_id = {x.get("id"): x for x in record["checks"] if isinstance(x, dict)}
    item = by_id.get("visual.pages")
    if not isinstance(item, dict):
        c.set("record.visual", "not_applicable")
        return
    status = item.get("status")
    evidence = item.get("pages")
    if evidence is None:
        if status == "pass":
            c.set("record.visual", "fail", code="record.visual-evidence",
                  id="visual.pages")
        else:
            c.set("record.visual", "not_applicable")
        return
    if not isinstance(evidence, list) or not evidence:
        c.set("record.visual", "fail", code="record.visual-bad",
              detail=t(c.locale, "check.err.visualBadNotList"))
        return
    report_ns = sorted(p["n"] for p in pages)
    entry_ns: list[int] = []
    unchecked: list[int] = []
    issue_items: list[str] = []
    for entry in evidence:
        page = entry.get("page") if isinstance(entry, dict) else None
        checked = entry.get("checked") if isinstance(entry, dict) else None
        if not isinstance(page, int) or isinstance(page, bool) \
                or not isinstance(checked, bool):
            c.set("record.visual", "fail", code="record.visual-bad",
                  detail=t(c.locale, "check.err.visualBadEntry"))
            return
        issues = entry.get("issues")
        if issues is not None and (not isinstance(issues, list)
                                   or not all(isinstance(x, str) and x.strip()
                                              for x in issues)):
            c.set("record.visual", "fail", code="record.visual-bad",
                  detail=t(c.locale, "check.err.visualBadEntry"))
            return
        entry_ns.append(page)
        if checked:
            if issues:
                issue_items.append(f"{page}: " + "；".join(issues))
        else:
            unchecked.append(page)
    dupes = sorted({n for n in entry_ns if entry_ns.count(n) > 1})
    if dupes:
        c.set("record.visual", "fail", code="record.visual-bad",
              detail=t(c.locale, "check.err.visualDupPages",
                       pages="、".join(map(str, dupes))))
        return
    missing = sorted(set(report_ns) - set(entry_ns))
    extra = sorted(set(entry_ns) - set(report_ns))
    if missing or extra:
        parts = []
        if missing:
            parts.append(t(c.locale, "check.err.visualMissingPages",
                           pages="、".join(map(str, missing))))
        if extra:
            parts.append(t(c.locale, "check.err.visualExtraPages",
                           pages="、".join(map(str, extra))))
        c.set("record.visual", "fail", code="record.visual-coverage",
              detail="；".join(parts))
        return
    if status == "pass":
        if unchecked:
            c.set("record.visual", "fail", code="record.visual-unchecked",
                  pages="、".join(map(str, sorted(unchecked))))
            return
        if issue_items:
            c.set("record.visual", "fail", code="record.visual-issue",
                  items="；".join(issue_items))
            return
    c.set("record.visual", "pass")


# request.length 的页数约束（#73）：支持「N 页」「N pages」与「N–M 页」区间；
# 只认整数页数，时长约束（如「10 分钟」）不折算页数，留给清单项判断。
LENGTH_RANGE_RE = re.compile(r"(\d+)\s*(?:-|–|—|~|至|到)\s*(\d+)\s*(?:页|pages?\b)",
                             re.IGNORECASE)
LENGTH_RE = re.compile(r"(\d+)\s*(?:页|pages?\b)", re.IGNORECASE)


def parse_length_pages(text: object) -> tuple[int, int] | None:
    """从 request.length 提取页数约束；返回 (下限, 上限)，解析不了返回 None。"""
    if not isinstance(text, str):
        return None
    m = LENGTH_RANGE_RE.search(text)
    if m:
        lo, hi = int(m.group(1)), int(m.group(2))
        return (lo, hi) if lo <= hi else None
    m = LENGTH_RE.search(text)
    if m:
        n = int(m.group(1))
        return n, n
    return None


def check_record_length(record: dict | None, pages: list[dict], c: Checks) -> dict | None:
    """篇幅约束（#73 / #169）：支持硬上限与参考目标、补充页是否计入。

    可解析且硬约束不一致是程序检查失败（需求与产物的确定矛盾）；参考目标超出仅
    警告且结构化披露；只有时长约束或没有记录时判不适用。"""
    if record is None or not pages:
        c.set("record.length", "not_applicable")
        return None
    req = record["request"]
    spec = req.get("lengthSpec")
    nature = "hard"
    include_supplements = True
    raw_pages = req.get("length")
    if "lengthSpec" in req:
        if not isinstance(spec, dict):
            c.set("record.length", "fail", code="record.length-bad",
                  detail="lengthSpec must be an object")
            return None
        if "nature" in spec:
            if spec["nature"] not in ("hard", "reference"):
                c.set("record.length", "fail", code="record.length-bad",
                      detail=f"unsupported nature {spec['nature']!r}")
                return None
            nature = spec["nature"]
        if "includeSupplements" in spec:
            if not isinstance(spec["includeSupplements"], bool):
                c.set("record.length", "fail", code="record.length-bad",
                      detail="includeSupplements must be boolean")
                return None
            include_supplements = spec["includeSupplements"]
        if "pages" in spec:
            if not isinstance(spec["pages"], str):
                c.set("record.length", "fail", code="record.length-bad",
                      detail="lengthSpec.pages must be string")
                return None
            raw_pages = spec["pages"]
    supp_count = sum(1 for p in pages if p.get("kind") == "supplement")
    main_count = len(pages) - supp_count
    counted = len(pages) if include_supplements else main_count
    constraint = parse_length_pages(raw_pages)
    if constraint is None:
        c.set("record.length", "not_applicable")
        return None
    lo, hi = constraint
    expected = str(lo) if lo == hi else f"{lo}–{hi}"
    # 新字段将单一页数声明为硬上限；显式区间保留下限，旧记录仍精确比对。
    if spec is not None and nature == "hard" and not LENGTH_RANGE_RE.search(raw_pages):
        lo = 0
        expected = f"≤{hi}"
    in_range = lo <= counted <= hi
    info = {
        "totalPages": len(pages),
        "mainPages": main_count,
        "supplementPages": supp_count,
        "countedPages": counted,
        "nature": nature,
        "includeSupplements": include_supplements,
        "expected": expected,
        "inRange": in_range,
    }
    if in_range:
        c.set("record.length", "pass")
    elif nature == "reference":
        c.add_warning("check.warn.lengthReferenceDrift", actual=counted, expected=expected)
        c.set("record.length", "pass")
    else:
        c.set("record.length", "fail", code="record.length-mismatch",
              actual=counted, expected=expected)
    return info


def check_record_revision(record: dict | None, c: Checks) -> dict | None:
    """改稿维护声明（#75 决策 8、15）：可选 revision 字段表达本次改稿核验信息。

    字段可选——初次生成的记录没有 revision，判不适用；没有记录同样不适用。
    声明了就要能对上：basis 写影响范围判定依据，rechecked 列出本次重新核验的
    检查项（必须在记录里有实际检查结果），pending 列出无法确认影响范围的待核验
    项（scope 范围 + reason 原因）。返回规范化后的 revision；没有则 None。"""
    if record is None:
        c.set("record.revision", "not_applicable")
        return None
    rev = record.get("revision")
    if rev is None:
        c.set("record.revision", "not_applicable")
        return None
    if not isinstance(rev, dict):
        c.set("record.revision", "fail", code="record.revision-bad", detail="revision is not an object")
        return None
    basis = rev.get("basis")
    rechecked = rev.get("rechecked")
    pending = rev.get("pending")
    ok_basis = isinstance(basis, str) and bool(basis.strip())
    ok_rechecked = isinstance(rechecked, list) \
        and all(isinstance(x, str) for x in rechecked)
    ok_pending = isinstance(pending, list) and all(
        isinstance(x, dict) for x in pending)
    if not (ok_basis and ok_rechecked and ok_pending):
        c.set("record.revision", "fail", code="record.revision-bad",
              detail="revision needs basis, rechecked (list of known check ids) "
                     "and pending ({scope, reason}) entries")
        return None
    unknown = [x for x in rechecked if x not in KNOWN_RECORD_CHECKS]
    if unknown:
        c.set("record.revision", "fail", code="record.revision-unknown",
              ids="、".join(dict.fromkeys(unknown)))
        return None
    # 声明重新核验过的项必须在记录里有实际检查结果（pass / fail）；仍是 not_run
    # 或 unavailable 就是「声明核验了但没有结果」，如实暴露。
    by_id = {x.get("id"): x for x in record["checks"] if isinstance(x, dict)}
    unrechecked = [x for x in dict.fromkeys(rechecked)
                   if by_id.get(x, {}).get("status") not in ("pass", "fail")]
    if unrechecked:
        c.set("record.revision", "fail", code="record.revision-unrechecked",
              ids="、".join(unrechecked))
        return None
    if not all(isinstance(x.get("scope"), str) and bool(x["scope"].strip())
               and isinstance(x.get("reason"), str) and bool(x["reason"].strip())
               for x in pending):
        c.set("record.revision", "fail", code="record.revision-pending",
              detail="pending entries need non-empty scope and reason")
        return None
    c.set("record.revision", "pass")
    return {"basis": basis.strip(), "rechecked": list(dict.fromkeys(rechecked)),
            "pending": [{"scope": x["scope"].strip(), "reason": x["reason"].strip()}
                        for x in pending]}


def check_record_method(record: dict | None, c: Checks) -> None:
    """实际生成方法声明（issue #74 决策 14）：kind / name 必须写明，外部方法
    必须带实际调用依据（invocation）——扫描到候选或名字出现不算已采用；用户
    指定但未实际采用（requested + note）与能力缺口（unavailable）如实披露。
    版本信息可得时应写明：外部方法缺 version 给警告不失败。方法声明缺失不挡
    初稿进审阅，但记录不能借它宣称使用了别的方法或已满足其能力要求。"""
    if record is None:
        c.set("record.method", "not_applicable")
        return
    method = record["method"]
    kind = method.get("kind")
    if not isinstance(kind, str) or kind not in METHOD_KINDS:
        c.set("record.method", "fail", code="record.method-unknown",
              kind=repr(kind) if kind is not None else "缺失",
              supported="、".join(METHOD_KINDS))
        return
    name = method.get("name")
    if not isinstance(name, str) or not name.strip():
        c.set("record.method", "fail", code="record.method-missing", field="name")
        return
    bad = []
    for field in ("version", "requested", "note"):
        value = method.get(field)
        if value is not None and not isinstance(value, str):
            bad.append(field)
    invocation = method.get("invocation")
    if invocation is not None and not isinstance(invocation, str):
        bad.append("invocation")
    unavailable = method.get("unavailable")
    if unavailable is not None and (not isinstance(unavailable, list)
                                    or not all(isinstance(x, str) and x.strip()
                                               for x in unavailable)):
        bad.append("unavailable")
    if bad:
        c.set("record.method", "fail", code="record.method-bad", fields="、".join(bad))
        return
    # 空串等同未说明；唯一例外是外部方法的 invocation——宣称采用了外部技能
    # 就必须有实际调用依据，空的 invocation 等于没有依据。
    if kind == "external" and not (isinstance(invocation, str) and invocation.strip()):
        c.set("record.method", "fail", code="record.method-invocation", name=name)
        return
    if kind == "external" and not (isinstance(method.get("version"), str)
                                   and method["version"].strip()):
        c.add_warning("check.warn.methodNoVersion", name=name)
    c.set("record.method", "pass")


def _text(value: object) -> str:
    return value.strip() if isinstance(value, str) else ""


def _non_empty_val(value: object) -> bool:
    if isinstance(value, str):
        return bool(value.strip())
    return isinstance(value, bool) or (isinstance(value, (dict, list)) and bool(value))


def _normalize_dimensions(raw: object) -> tuple[dict[str, dict], str | None]:
    if not isinstance(raw, dict):
        return {}, "request.alignment must be an object"
    out: dict[str, dict] = {}
    for key, entry in raw.items():
        if not isinstance(key, str) or not key.strip() or not isinstance(entry, dict):
            return {}, f"invalid alignment entry for {key!r}"
        value = entry.get("value")
        source = entry.get("source")
        status = entry.get("status")
        if not _non_empty_val(value):
            return {}, f"{key}: missing value"
        if source not in ALIGNMENT_SOURCES:
            return {}, f"{key}: unsupported source {source!r}"
        if status not in ALIGNMENT_STATUSES:
            return {}, f"{key}: unsupported status {status!r}"
        basis = _text(entry.get("basis"))
        note = _text(entry.get("note"))
        reason = _text(entry.get("reason"))
        if source == "delegated" and not (basis or note):
            return {}, f"{key}: delegated alignment requires basis or note"
        if (source in ("inferred", "default") or status in ("pending", "conflict", "deviated")) \
                and not (reason or basis or note):
            return {}, f"{key}: {source}/{status} alignment requires reason"
        norm: dict = {"value": value, "source": source, "status": status}
        for fname, fval in (("reason", reason), ("basis", basis), ("note", note)):
            if fval:
                norm[fname] = fval
        out[key.strip()] = norm
    return out, None


def _normalize_qa(raw: object) -> tuple[dict | None, str | None]:
    if not isinstance(raw, dict):
        return None, "request.qa must be an object"
    used = raw.get("used")
    if not isinstance(used, bool):
        return None, "request.qa.used must be boolean"
    available = raw.get("available")
    if available is not None and not isinstance(available, bool):
        return None, "request.qa.available must be boolean"
    tool = raw.get("tool")
    if tool is not None and not isinstance(tool, str):
        return None, "request.qa.tool must be string"
    unavailable_reason = raw.get("unavailableReason")
    if unavailable_reason is not None and not isinstance(unavailable_reason, str):
        return None, "request.qa.unavailableReason must be string"
    lists: dict[str, list[str]] = {}
    for key in ("asked", "unanswered"):
        val = raw.get(key)
        if val is not None and (not isinstance(val, list)
                                or not all(isinstance(x, str) and x.strip() for x in val)):
            return None, f"request.qa.{key} must be a list of non-empty strings"
        lists[key] = [x.strip() for x in val] if isinstance(val, list) else []
    reason_text = _text(unavailable_reason)
    if available is False and not reason_text:
        return None, "unavailable Q&A tool requires non-empty unavailableReason"
    return {
        "used": used,
        "available": available,
        "tool": _text(tool) or None,
        "unavailableReason": reason_text or None,
        "asked": lists["asked"],
        "unanswered": lists["unanswered"],
    }, None


def _normalize_deviations(raw: object) -> tuple[list[dict], str | None]:
    if not isinstance(raw, list):
        return [], "deviations must be a list"
    out: list[dict] = []
    for entry in raw:
        if not isinstance(entry, dict):
            return [], "each deviation entry must be an object"
        item = _text(entry.get("item"))
        reason = _text(entry.get("reason"))
        if not item or not reason:
            return [], "deviation entry requires non-empty item and reason"
        impact = entry.get("impact")
        if impact is not None and not isinstance(impact, str):
            return [], "deviation impact must be string"
        norm = {"item": item, "reason": reason}
        if _text(impact):
            norm["impact"] = _text(impact)
        out.append(norm)
    return out, None


def check_record_alignment(record: dict | None, c: Checks) -> dict | None:
    """生成前对齐偏好、问答使用与偏离披露核验（issue #169）。

    历史记录未包含 request.alignment / request.qa / deviations 时判不适用并如实
    返回 recorded=False，保持向后兼容；声明后校验来源与状态字段，未解决冲突
    不能与 content.requirements=pass 并存。"""
    if record is None:
        c.set("record.alignment", "not_applicable")
        return None
    req = record["request"]
    if "alignment" not in req and "qa" not in req and "deviations" not in record:
        c.set("record.alignment", "not_applicable")
        return {"recorded": False, "dimensions": {}, "pending": [],
                "conflicts": [], "deviations": [], "qa": None}
    dimensions: dict[str, dict] = {}
    if "alignment" in req:
        dimensions, err = _normalize_dimensions(req["alignment"])
        if err:
            c.set("record.alignment", "fail", code="record.alignment-bad", detail=err)
            return None
    qa_info = None
    if "qa" in req:
        qa_info, qa_err = _normalize_qa(req["qa"])
        if qa_err:
            c.set("record.alignment", "fail", code="record.alignment-qa-bad", detail=qa_err)
            return None
    deviations: list[dict] = []
    if "deviations" in record:
        deviations, dev_err = _normalize_deviations(record["deviations"])
        if dev_err:
            c.set("record.alignment", "fail", code="record.alignment-bad", detail=dev_err)
            return None
    needs_qa = any(d["source"] in ("default", "inferred") or d["status"] == "pending"
                   for d in dimensions.values())
    if qa_info is not None and qa_info["used"] is False \
            and not qa_info.get("unavailableReason") and needs_qa:
        c.set("record.alignment", "fail", code="record.alignment-qa-bad",
              detail="Q&A skipped without unavailableReason despite inferred/default preferences")
        return None
    conflicts = [k for k, v in dimensions.items() if v["status"] == "conflict"]
    pending = [k for k, v in dimensions.items() if v["status"] == "pending"]
    if qa_info:
        pending = list(dict.fromkeys(pending + qa_info["unanswered"]))
    align_info = {"recorded": bool(dimensions), "dimensions": dimensions, "pending": pending,
                  "conflicts": conflicts, "deviations": deviations, "qa": qa_info}
    by_id = {x.get("id"): x for x in record["checks"] if isinstance(x, dict)}
    if conflicts and by_id.get("content.requirements", {}).get("status") == "pass":
        c.set("record.alignment", "fail", code="record.alignment-conflict-pass",
              items="、".join(conflicts))
        return align_info
    dev_pairs = _alignment_deviation_pairs(deviations, dimensions)
    if dev_pairs:
        c.add_warning("check.warn.alignmentDeviated", items="、".join(k for k, _ in dev_pairs))
    c.set("record.alignment", "pass")
    return align_info


def check_record_deliverables(record: dict, base_dir: Path, c: Checks) -> list[dict]:
    """额外交付（issue #180）：核对记录里的 PPTX 证据链并派生各项可交付判定。

    程序只核对证据的完整性与一致性：产物存在、哈希新鲜、调用依据、页面覆盖、
    图片证据绑定、pass 与未解决问题的矛盾、跨格式数字对应，以及实测声明的
    依据。诚实记录的未完成（unavailable / not_run）不是程序失败——它在摘要里
    如实披露缺口。返回每项的派生信息供摘要与结构化结果使用。
    """
    items = record.get("deliverables")
    if items is None:
        return []
    problems: list[dict] = []
    infos: list[dict] = []
    if not isinstance(items, list):
        c.set("record.deliverables", "fail", code="record.deliverable-bad")
        return infos

    detail_keys = {
        "record.deliverable-bad": "check.err.deliverableBad",
        "record.deliverable-missing": "check.err.deliverableMissing",
        "record.deliverable-stale": "check.err.deliverableStale",
        "record.deliverable-invocation": "check.err.deliverableInvocation",
        "record.deliverable-pages": "check.err.deliverablePages",
        "record.deliverable-visual": "check.err.deliverableVisual",
        "record.deliverable-consistency": "check.err.deliverableConsistency",
        "record.deliverable-openissues": "check.err.deliverableOpenIssues",
        "record.deliverable-powerpoint": "check.err.deliverablePowerpoint",
        "record.deliverable-status": "check.err.deliverableStatus",
    }

    def fail(code: str, part: str = "", slide: int | None = None, **params: object) -> None:
        problems.append({"code": code, "part": part, "slide": slide,
                         "detail_key": detail_keys.get(code, "check.err.deliverableBad"),
                         "params": params})

    for index, item in enumerate(items, 1):
        problem_start = len(problems)
        label = f"deliverables[{index}]"
        if not isinstance(item, dict):
            fail("record.deliverable-bad", label)
            continue
        kind = item.get("kind")
        file_name = item.get("file")
        required = item.get("required", False)
        require_powerpoint = item.get("requirePowerpoint", False)
        info = {"kind": kind if isinstance(kind, str) else "?",
                "file": file_name if isinstance(file_name, str) and file_name else None,
                "required": bool(required), "requirePowerpoint": bool(require_powerpoint),
                "deliverable": False, "powerpoint": None, "openIssues": 0,
                "checks": {}, "note": item.get("note", "")}
        infos.append(info)
        if kind != "pptx":
            fail("record.deliverable-bad", label, kind=str(kind))
            continue
        # 结构与枚举：三项检查与实测状态都用 pass / fail / not_run / unavailable
        checks = item.get("checks")
        statuses: dict[str, str] = {}
        if file_name:
            if not isinstance(checks, dict):
                fail("record.deliverable-bad", label)
                continue
            for name in ("file", "visual", "consistency"):
                entry = checks.get(name)
                status = entry.get("status") if isinstance(entry, dict) else None
                if status not in CHECK_STATUSES:
                    fail("record.deliverable-bad", label, field=name)
                    status = "not_run"
                statuses[name] = status
            info["checks"] = dict(statuses)
        else:
            # 没有有效产物：如实声明状态并给出原因与可继续的条件
            status = item.get("status")
            if status not in CHECK_STATUSES:
                fail("record.deliverable-bad", label, field="status")
            note = item.get("note")
            resume = item.get("resume")
            has_reason = isinstance(note, str) and bool(note.strip()) or \
                isinstance(resume, str) and bool(resume.strip())
            if not has_reason:
                fail("record.deliverable-status", label)
            if checks:
                fail("record.deliverable-bad", label, field="checks")
            open_issues = [x for x in item.get("openIssues", [])
                           if isinstance(x, str)] if isinstance(item.get("openIssues"), list) else []
            info["openIssues"] = len(open_issues)
        if file_name is None:
            continue
        powerpoint = item.get("powerpoint")
        ppt_status = None
        if isinstance(powerpoint, dict):
            ppt_status = powerpoint.get("status")
            info["powerpoint"] = ppt_status if ppt_status in CHECK_STATUSES else None
            if ppt_status not in CHECK_STATUSES:
                fail("record.deliverable-powerpoint", label, status=str(ppt_status))
            elif ppt_status == "pass" and (
                    not str(powerpoint.get("os", "")).strip()
                    or not str(powerpoint.get("version", "")).strip()):
                fail("record.deliverable-powerpoint", label)
        else:
            fail("record.deliverable-powerpoint", label)

        # 产物与哈希：旧通过声明不适用于已改变的文件
        part_path = base_dir / str(file_name)
        declared_sha = item.get("sha256")
        actual_sha = None
        try:
            actual_sha = sha256_bytes(part_path.read_bytes())
        except OSError:
            fail("record.deliverable-missing", str(file_name))
            continue
        if declared_sha != actual_sha:
            fail("record.deliverable-stale", str(file_name))
        build = item.get("build")
        if not isinstance(build, dict) or not str(build.get("tool", "")).strip() \
                or not str(build.get("invocation", "")).strip():
            fail("record.deliverable-invocation", str(file_name))
        pages = item.get("pages")
        texts, actual_pages = _pptx_slide_texts(part_path)
        if not isinstance(pages, int) or isinstance(pages, bool) or pages <= 0:
            fail("record.deliverable-pages", str(file_name), field="pages")
            pages = 0
        elif pages != actual_pages:
            fail("record.deliverable-pages", str(file_name),
                 declared=pages, actual=actual_pages)
        if statuses.get("visual") == "pass":
            render = item.get("render")
            if not isinstance(render, dict) or render.get("pptxSha256") != actual_sha:
                fail("record.deliverable-stale", str(file_name), field="render.pptxSha256")
            evidence = checks["visual"].get("pages")
            seen: set[int] = set()
            if not isinstance(evidence, list):
                fail("record.deliverable-visual", str(file_name), field="pages")
                evidence = []
            for entry in evidence:
                if not isinstance(entry, dict):
                    fail("record.deliverable-visual", str(file_name), field="page")
                    continue
                number = entry.get("page")
                issues = entry.get("issues", [])
                image = entry.get("image")
                image_sha = entry.get("sha256")
                if not isinstance(number, int) or not (1 <= number <= (pages or 0)):
                    fail("record.deliverable-pages", str(file_name), page=number)
                    continue
                seen.add(number)
                if entry.get("checked") is not True:
                    fail("record.deliverable-visual", str(file_name), page=number)
                if isinstance(issues, list) and issues:
                    fail("record.deliverable-visual", str(file_name), page=number,
                         issues=len(issues))
                image_path = base_dir / str(image) if isinstance(image, str) and image else None
                if image_path is None or not image_path.is_file():
                    fail("record.deliverable-visual", str(file_name), page=number,
                         field="image")
                elif not isinstance(image_sha, str) or \
                        sha256_bytes(image_path.read_bytes()) != image_sha:
                    fail("record.deliverable-visual", str(file_name), page=number,
                         field="sha256")
            missing = [n for n in range(1, (pages or 0) + 1) if n not in seen]
            if missing:
                fail("record.deliverable-visual", str(file_name), missing="、".join(map(str, missing)))
        if statuses.get("consistency") == "pass":
            mappings = checks["consistency"].get("findings")
            claims = {f.get("claim"): f for f in record.get("findings", [])
                      if isinstance(f, dict) and isinstance(f.get("claim"), str)}
            mapped: set[str] = set()
            if not isinstance(mappings, list):
                fail("record.deliverable-consistency", str(file_name), field="findings")
                mappings = []
            for entry in mappings:
                if not isinstance(entry, dict):
                    fail("record.deliverable-consistency", str(file_name), field="finding")
                    continue
                claim = entry.get("claim")
                mapped.add(claim if isinstance(claim, str) else "")
                page_list = entry.get("pages")
                if not isinstance(page_list, list) or not page_list:
                    fail("record.deliverable-consistency", str(file_name), claim=str(claim))
                    continue
                page_tokens: set[str] = set()
                for number in page_list:
                    if not isinstance(number, int) or not (1 <= number <= (pages or 0)):
                        fail("record.deliverable-pages", str(file_name), page=number)
                    else:
                        page_tokens |= _numeric_tokens(texts.get(number, ""))
                for token in _claim_numbers(claims.get(claim, {})):
                    if token not in page_tokens:
                        fail("record.deliverable-consistency", str(file_name),
                             page=page_list if isinstance(page_list, list) and page_list else None,
                             claim=str(claim), token=token)
            unmapped = sorted(set(claims) - mapped)
            if unmapped:
                fail("record.deliverable-consistency", str(file_name),
                     missing="、".join(unmapped))
        open_issues = item.get("openIssues")
        if isinstance(open_issues, list):
            info["openIssues"] = len(open_issues)
            if open_issues and any(status == "pass" for status in statuses.values()):
                fail("record.deliverable-openissues", str(file_name))
        no_failures = len(problems) == problem_start
        info["deliverable"] = bool(
            no_failures
            and statuses.get("file") == "pass" and statuses.get("visual") == "pass"
            and statuses.get("consistency") == "pass"
            and not info["openIssues"]
            and (not require_powerpoint or info["powerpoint"] == "pass"))
    rendered = [{"code": p["code"], "part": p["part"], "slide": p["slide"],
                 "detail": t(c.locale, p["detail_key"], part=p["part"], **p["params"])}
                for p in problems]
    c.set("record.deliverables", "fail" if problems else "pass",
          code=problems[0]["code"] if problems else None,
          **({"part": problems[0]["part"]} if problems else {}))
    entry = c.items["record.deliverables"]
    entry["problems"] = rendered
    return infos


def _alignment_deviation_pairs(deviations: list[dict] | None,
                               dimensions: dict | None) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = [(d["item"], d["reason"]) for d in (deviations or [])]
    seen = {k for k, _ in out}
    for dim, info in (dimensions or {}).items():
        if info.get("status") == "deviated" and dim not in seen:
            reason = info.get("reason") or info.get("basis") or info.get("note") or ""
            out.append((dim, reason))
            seen.add(dim)
    return out


class _NavigationLinkParser(HTMLParser):
    """仅收集实际锚点，忽略注释、原始文本元素和未实例化的模板。"""

    CDATA_CONTENT_ELEMENTS = ("script", "style", "textarea", "title")

    def __init__(self) -> None:
        super().__init__()
        self.links: list[dict[str, str | None]] = []
        self.anchors: set[str] = set()
        self.template_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "template":
            self.template_depth += 1
        if self.template_depth:
            return
        attributes = dict(attrs)
        if attributes.get("id"):
            self.anchors.add(attributes["id"])
        if tag == "a":
            self.links.append(attributes)
            if attributes.get("name"):
                self.anchors.add(attributes["name"])

    def handle_endtag(self, tag: str) -> None:
        if tag == "template" and self.template_depth:
            self.template_depth -= 1


def check_report_navigation(pages: list[dict], c: Checks) -> None:
    """跨页导航与目录章节范围核验（issue #169）。

    检查 #page-<key>、data-page-ref 与 data-range-start/end：引用缺失页面键、
    href 的真实锚点不在对应页面，或目录起始页晚于结束页时判定程序检查失败；
    完全未声明此类链接时判不适用。"""
    if not pages:
        c.set("report.navigation", "not_applicable")
        return
    pos_by_key = {p["key"]: idx for idx, p in enumerate(pages)}
    found_nav = False
    broken_refs: list[str] = []
    bad_ranges: list[str] = []
    parsed_pages: list[_NavigationLinkParser] = []
    anchor_pages: dict[str, set[str]] = {}
    for p in pages:
        parser = _NavigationLinkParser()
        parser.feed(p["html"])
        parser.close()
        parsed_pages.append(parser)
        for anchor in parser.anchors:
            anchor_pages.setdefault(anchor, set()).add(p["key"])
    for parser in parsed_pages:
        for attrs in parser.links:
            href = (attrs.get("href") or "").strip()
            if href.lower().startswith("#page-"):
                found_nav = True
                target_key = href[6:].strip()
                if not target_key or target_key not in pos_by_key \
                        or anchor_pages.get(href[1:]) != {target_key}:
                    broken_refs.append(href)
            if "data-page-ref" in attrs:
                found_nav = True
                page_ref = (attrs["data-page-ref"] or "").strip()
                if not page_ref or page_ref not in pos_by_key:
                    broken_refs.append(f"data-page-ref={page_ref or '?'}")
            if "data-range-start" in attrs or "data-range-end" in attrs:
                found_nav = True
                start_k = (attrs.get("data-range-start") or "").strip()
                end_k = (attrs.get("data-range-end") or "").strip()
                if not start_k or not end_k or start_k not in pos_by_key \
                        or end_k not in pos_by_key or pos_by_key[start_k] > pos_by_key[end_k]:
                    bad_ranges.append(f"{start_k or '?'}..{end_k or '?'}")
    if not found_nav:
        c.set("report.navigation", "not_applicable")
        return
    if broken_refs:
        c.set("report.navigation", "fail", code="report.nav-broken-link",
              refs="、".join(dict.fromkeys(broken_refs)))
        return
    if bad_ranges:
        c.set("report.navigation", "fail", code="report.nav-bad-range",
              items="、".join(dict.fromkeys(bad_ranges)))
        return
    c.set("report.navigation", "pass")


def _local_tag(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _pptx_slide_texts(path: Path) -> tuple[dict[int, str], int]:
    """提取 PPTX 每张幻灯片的文本（含图表数据值），按 sldIdLst 顺序 1 起编页。

    供跨格式一致性比对使用；读取失败返回空映射——调用方按「一致性无法验证」
    处理，不让报告检查因附加产物损坏而崩溃。
    """
    try:
        with zipfile.ZipFile(path) as archive:
            presentation = ET.fromstring(archive.read("ppt/presentation.xml"))
            rels_root = ET.fromstring(archive.read("ppt/_rels/presentation.xml.rels"))
            targets = {rel.get("Id", ""): rel.get("Target", "")
                       for rel in rels_root if _local_tag(rel.tag) == "Relationship"}
            slide_parts = []
            for node in presentation.iter():
                if _local_tag(node.tag) != "sldId":
                    continue
                rid = node.get("{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id", "")
                target = targets.get(rid, "")
                if target:
                    base = _posix_norm("ppt/presentation.xml", target)
                    slide_parts.append(base)
            texts: dict[int, str] = {}
            for page, part in enumerate(slide_parts, 1):
                chunks: list[str] = []
                slide_root = ET.fromstring(archive.read(part))
                for element in slide_root.iter():
                    if _local_tag(element.tag) == "t" and (element.text or ""):
                        chunks.append(element.text)
                slide_rels_name = posixpath.join(posixpath.dirname(part), "_rels",
                                                 posixpath.basename(part) + ".rels")
                if slide_rels_name in archive.NameToInfo:
                    slide_rels = ET.fromstring(archive.read(slide_rels_name))
                    for rel in slide_rels:
                        if _local_tag(rel.tag) != "Relationship":
                            continue
                        rtype = rel.get("Type", "")
                        target = rel.get("Target", "")
                        if rtype.endswith("/chart") and target and rel.get("TargetMode") != "External":
                            chart_part = _posix_norm(part, target)
                            chart_root = ET.fromstring(archive.read(chart_part))
                            for element in chart_root.iter():
                                if _local_tag(element.tag) == "v" and (element.text or ""):
                                    chunks.append(element.text)
                texts[page] = "\n".join(chunks)
            return texts, len(slide_parts)
    except (OSError, zipfile.BadZipFile, KeyError, ET.ParseError):
        return {}, 0


def _posix_norm(source: str, target: str) -> str:
    """以 posixpath 解析包内关系目标（与 pptx_reader 的定位约定一致）。"""
    if target.startswith("/"):
        return target.lstrip("/")
    return posixpath.normpath(posixpath.join(posixpath.dirname(source), target))


_NUMBER_TOKEN = re.compile(r"[+-]?[0-9]+(?:[0-9,，]*[0-9])?(?:\.[0-9]+)?")


def _numeric_tokens(text: str) -> set[str]:
    """抽取文本里的数字令牌，归一化成可比较的 Decimal 文本（12 与 12.0 相等）。"""
    tokens: set[str] = set()
    for match in _NUMBER_TOKEN.finditer(text.replace("，", ",").replace("−", "-")):
        raw = match.group(0).replace(",", "")
        try:
            tokens.add(str(Decimal(raw).normalize()))
        except (InvalidOperation, ValueError):
            continue
    return tokens


def _claim_numbers(finding: dict) -> set[str]:
    """结论声明里的关键数字：声明文本 + 声明口径的计算期望值。"""
    tokens = _numeric_tokens(str(finding.get("claim", "")))
    calculation = finding.get("calculation")
    if isinstance(calculation, dict) and calculation.get("expected") is not None:
        try:
            tokens.add(str(Decimal(str(calculation["expected"])).normalize()))
        except (InvalidOperation, ValueError):
            pass
    return tokens


def coverage_for(record: dict | None, fresh: bool, kind: str,
                 script_present: bool = True) -> tuple[str, int, int, int]:
    """汇总一类核验的覆盖情况；状态语义语言无关，返回 (状态, 通过数, 总数, 失败数)。

    总数按内置清单的全部条目计（决策 10：可交付要求清单逐项检查），记录未声明的
    条目视为未执行；讲稿关联项在无讲稿时不计入总数（#73 适用性规则）。
    covered 全部通过；partial 有通过也有未完成；uncovered 有失败；
    not_run 未执行；unverified 无法证明（记录对不上当前产物）。
    """
    if record is None:
        return "not_run", 0, 0, 0
    if not fresh:
        return "unverified", 0, 0, 0
    known_ids = [cid for cid, k in KNOWN_RECORD_CHECKS.items() if k == kind]
    if not script_present:
        known_ids = [cid for cid in known_ids if cid not in SCRIPT_SCOPED_CHECKS]
    by_id = {x.get("id"): x for x in record["checks"] if isinstance(x, dict)}
    total = len(known_ids)
    passed = sum(1 for cid in known_ids if by_id.get(cid, {}).get("status") == "pass")
    failed = sum(1 for cid in known_ids if by_id.get(cid, {}).get("status") == "fail")
    if passed == total:
        return "covered", passed, total, failed
    if failed:
        return "uncovered", passed, total, failed
    if passed:
        return "partial", passed, total, failed
    return "not_run", 0, total, failed


# ── 汇总判定 ────────────────────────────────────────────────────────────────

def _alignment_dev_names(alignment: dict | None) -> list[str]:
    if not alignment:
        return []
    return [k for k, _ in _alignment_deviation_pairs(
        alignment.get("deviations"), alignment.get("dimensions"))]


def summarize(record: dict | None, fresh: bool, checks: Checks, draft_ok: bool,
              record_ok: bool, claims_info: dict | None = None,
              calc_pending: list[str] | None = None, source_stale: bool = False,
              reading: dict | None = None, script_present: bool = True,
              revision: dict | None = None, alignment: dict | None = None,
              length_info: dict | None = None,
              deliverables: list[dict] | None = None) -> dict:
    reasons: list[str] = []
    failed_program = [item for item in checks.failed() if item["id"] != "record.deliverables"]
    # 全部实质材料不可读：空结构或虚构内容不能当作初稿生成完成（issue #71）。
    reading_blocked = any(f["id"] == "record.reading" and f["code"] == "record.reading-blocked"
                          for f in failed_program)
    draft_complete = draft_ok and not reading_blocked \
        and not [f for f in failed_program
                 if f["id"] in ("report.parse", "report.pageKeys",
                                "script.pages", "record.pageRefs")]
    coverage_raw = {kind: coverage_for(record, fresh, kind, script_present)
                    for kind in ("content", "structure", "visual")}
    coverage = {k: v[0] for k, v in coverage_raw.items()}
    coverage_counts = {k: {"done": v[1], "total": v[2], "failed": v[3]}
                       for k, v in coverage_raw.items()}
    # 覆盖封顶：记录声明了 pass，但结论支持、计算口径或材料对应的证据不足时，
    # 内容与结构核验不能显示为 covered（决策 8、12；#72；#73 把封顶同样用于结构类，
    # 因为 structure.claim-support 与结论支持映射共用同一份证据）。
    cap = content_cap(record, claims_info, calc_pending, source_stale)
    capped: dict[str, bool] = {"content": False, "structure": False, "visual": False}
    for kind in ("content", "structure"):
        if cap is not None and CAP_SEVERITY[cap] > CAP_SEVERITY[coverage[kind]]:
            capped[kind] = True
            coverage[kind] = cap
    dev_names = _alignment_dev_names(alignment)
    if alignment and (alignment.get("conflicts") or dev_names):
        if CAP_SEVERITY["partial"] > CAP_SEVERITY[coverage["content"]]:
            capped["content"] = True
            coverage["content"] = "partial"
    declared = None if record is None else record["verdict"].get("deliverable")
    declared = declared if isinstance(declared, bool) else None
    # 记录声明了讲稿哈希而核验未收到讲稿（#69 审查 P2-1）：讲稿检查缺席，
    # 不能沿用「其余检查都通过」的支持去成立可交付；显式警告在检查阶段给出。
    script_missing = script_declared_but_missing(record, script_present)
    deliverable = draft_complete and record is not None and fresh and record_ok \
        and not failed_program
    if deliverable:
        deliverable = all(coverage[k] == "covered"
                          for k in ("content", "structure", "visual"))
        if deliverable:
            blocking = [x.get("item") for x in record["openItems"]
                        if isinstance(x, dict) and x.get("blocking") is True
                        and isinstance(x.get("item"), str)]
            if blocking:
                deliverable = False
                reasons.append(t(checks.locale, "check.reason.blocking",
                                 items="；".join(blocking)))
            # 改稿待核验项（#75 决策 8）：影响范围没有确认之前，不沿用可交付判定。
            if deliverable and revision and revision.get("pending"):
                deliverable = False
            # 对齐待确认、内部冲突或偏离未关闭（#169）：确认前不判可交付。
            if deliverable and alignment and (
                    alignment.get("pending") or alignment.get("conflicts") or dev_names):
                deliverable = False
            # 声明讲稿哈希而未收到讲稿：已声明产物的检查未执行，拦可交付。
            if deliverable and script_missing:
                deliverable = False
                reasons.append(t(checks.locale, "check.reason.scriptDeclaredMissing"))
        else:
            not_run = [k for k, v in coverage.items() if v != "covered"]
            reasons.append(t(checks.locale, "check.reason.notRun",
                             items="、".join(t(checks.locale, "check.coverage." + k)
                                             for k in not_run)))
        if deliverable and declared is not True:
            deliverable = False
            reasons.append(t(checks.locale, "check.reason.declaredNo"))
    else:
        if record is None:
            reasons.append(t(checks.locale, "check.reason.recordMissing"))
        elif not fresh:
            reasons.append(t(checks.locale, "check.reason.stale"))
        elif not record_ok:
            reasons.append(t(checks.locale, "check.reason.recordChecks"))
        if failed_program:
            if reading_blocked:
                reasons.append(t(checks.locale, "check.reason.readingBlocked"))
            reasons.append(t(checks.locale, "check.reason.programFailed",
                             items="、".join(checks.name(f["id"]) for f in failed_program)))
    # 待核验范围与原因始终披露（#75）：即使整体已因其他原因不可交付，
    # 用户也要知道哪些范围没有核验、为什么。
    if revision and revision.get("pending"):
        sep = "；" if checks.locale == "zh-CN" else "; "
        items = sep.join(f"{x['scope']}（{x['reason']}）" if checks.locale == "zh-CN"
                         else f"{x['scope']} ({x['reason']})"
                         for x in revision["pending"])
        reasons.append(t(checks.locale, "check.reason.revisionPending", items=items))
    # 额外交付缺口（issue #180）：用户明确要求的 PPTX 未完成时保留整项请求缺口，
    # 不影响 HTML 自己的可交付判定，也不把已通过的 HTML 检查记成失败。
    gap_items = [f"{d['file'] or d['note'] or d['kind']}"
                 for d in (deliverables or [])
                 if d.get("required") and not d.get("deliverable")]
    if gap_items:
        reasons.append(t(checks.locale, "check.reason.deliverablesGap",
                         items="、".join(gap_items)))
    if alignment:
        sep = "；" if checks.locale == "zh-CN" else "; "
        if alignment.get("conflicts"):
            reasons.append(t(checks.locale, "check.reason.alignmentConflict",
                             items=sep.join(alignment["conflicts"])))
        if alignment.get("pending"):
            reasons.append(t(checks.locale, "check.reason.alignmentPending",
                             items=sep.join(alignment["pending"])))
        if dev_names:
            reasons.append(t(checks.locale, "check.reason.alignmentDeviated",
                             items=sep.join(dev_names)))
    scopes: list[dict] = []
    if record is not None:
        for item in record["checks"]:
            if isinstance(item, dict) and item.get("status") == "pass" \
                    and isinstance(item.get("id"), str) \
                    and isinstance(item.get("scope"), str) and item["scope"].strip():
                scopes.append({"id": item["id"], "scope": item["scope"].strip()})
    # 实际生成方法（#74）：从记录原样带出，供摘要与结构化结果如实披露。
    method_info = None
    if record is not None:
        method = record["method"]
        unavailable = method.get("unavailable")
        method_info = {"kind": method.get("kind"), "name": method.get("name"),
                       "version": method.get("version"),
                       "requested": method.get("requested"),
                       "invocation": method.get("invocation"),
                       "unavailable": [x for x in unavailable if isinstance(x, str)]
                       if isinstance(unavailable, list) else []}
    return {
        "checks": [checks.items[cid] for cid in Checks.ORDER],
        "warnings": list(checks.warnings),
        "record": {"version": None if record is None else record["schema"],
                   "fresh": bool(record is not None and fresh),
                   "method": method_info,
                   "declaredDeliverable": declared,
                   "declaredDraftComplete": None if record is None
                   else bool(record["verdict"].get("draftComplete")),
                   "gaps": [] if record is None
                   else [str(g) for g in record["gaps"] if isinstance(g, str)],
                   "openItems": {"n": 0, "blocking": 0} if record is None
                   else {"n": sum(1 for x in record["openItems"] if isinstance(x, dict)),
                         "blocking": sum(1 for x in record["openItems"]
                                         if isinstance(x, dict) and x.get("blocking") is True)},
                   "scopes": scopes,
                   "revision": revision,
                   "alignment": alignment,
                   "length": length_info},
        "deliverables": deliverables or [],
        "coverage": coverage,
        "coverageCounts": coverage_counts,
        "coverageCapped": capped,
        "reading": reading,
        "gaps": [g for g in record["gaps"] if isinstance(g, str) and g.strip()]
        if record is not None else [],
        "verdict": {"draftComplete": draft_complete, "deliverable": deliverable,
                    "reasons": reasons},
    }


# ── 公开接口 ────────────────────────────────────────────────────────────────

def check_report(report_path: Path, script_path: Path | None = None,
                 record_path: Path | None = None, locale: str | None = None) -> dict:
    """检查一套报告产物；返回结构化结果（字段名与状态值语言无关）。"""
    loc = i18n.resolve_locale(locale, system_languages=i18n.system_languages())
    c = Checks(loc)
    import docx_review
    import pptx_report
    if docx_review.is_word_like(report_path):
        # 报告检查针对生成的逐页 HTML；Word 文档直接审阅时不按文本解码，明确拒绝
        raise UsageError(t(loc, "check.docxUnsupported", path=report_path))
    if pptx_report.is_pptx(report_path) or pptx_report.is_ppt(report_path):
        # PPT 同理：直接审阅走 build_viewer，本入口不按文本解码演示文稿
        raise UsageError(t(loc, "check.pptxUnsupported", path=report_path))
    try:
        report_bytes = report_path.read_bytes()
    except OSError as exc:
        raise UsageError(t(loc, "check.fileMissing", path=report_path)) from exc
    report_doc = report_bytes.decode("utf-8", errors="replace")
    script_bytes = None
    script_text = None
    if script_path is not None:
        try:
            script_bytes = script_path.read_bytes()
        except OSError as exc:
            raise UsageError(t(loc, "check.fileMissing", path=script_path)) from exc
        script_text = script_bytes.decode("utf-8", errors="replace")

    pages, draft_ok = check_report_and_script(report_doc, script_text, c)
    if script_text is not None and c.items["script.pages"]["status"] == "not_applicable":
        c.set("script.pages", "pass")   # 能解析且页序一致时已 set(pass)，这里兜底
    check_report_navigation(pages, c)
    record, record_ok = load_record(record_path, c)
    report_sha = sha256_bytes(report_bytes)
    script_sha = None if script_bytes is None else sha256_bytes(script_bytes)
    fresh = check_freshness(record, report_sha, script_sha, c)
    claims_info = None
    calc_pending: list[str] = []
    source_stale = False
    reading = None
    revision = None
    alignment = None
    length_info = None
    deliverables = None
    if record is not None:
        claims_info = analyze_claims(record)
        check_page_refs(record, pages, c)
        calc_pending = check_calculations(record, c)
        check_record_claims(claims_info, c)
        check_record_method(record, c)
        source_stale = check_record_sources(record, report_path.parent, claims_info, c)
        check_record_checks(record, c, script_present=script_text is not None)
        reading = check_reading(record, c)
        check_record_visual(record, pages, c)
        length_info = check_record_length(record, pages, c)
        revision = check_record_revision(record, c)
        alignment = check_record_alignment(record, c)
        deliverables = check_record_deliverables(record, report_path.parent, c)
        if script_declared_but_missing(record, script_text is not None):
            c.add_warning("check.warn.scriptDeclaredMissing")
    result = summarize(record, fresh, c, draft_ok, record_ok, claims_info=claims_info,
                       calc_pending=calc_pending, source_stale=source_stale, reading=reading,
                       script_present=script_text is not None, revision=revision,
                       alignment=alignment, length_info=length_info,
                       deliverables=deliverables)
    result["schema"] = RESULT_SCHEMA
    result["inputs"] = {
        "report": {"path": report_path.name, "sha256": report_sha},
        "script": None if script_path is None
        else {"path": script_path.name, "sha256": script_sha},
        "record": None if record_path is None
        else {"path": record_path.name},
    }
    result["locale"] = loc
    return result


def _format_labeled_reasons(loc: str, pairs: list[tuple[str, str]]) -> str:
    sep = "；" if loc == "zh-CN" else "; "
    return sep.join(
        f"{k}（{r}）" if (loc == "zh-CN" and r) else (f"{k} ({r})" if r else k)
        for k, r in pairs)


def _render_alignment_summary(loc: str, al: dict) -> list[str]:
    out: list[str] = []
    if not al.get("recorded") and not al.get("deviations") and not al.get("qa"):
        out.append(t(loc, "check.alignment.unrecorded"))
        return out
    sep = "、" if loc == "zh-CN" else ", "
    dims = al.get("dimensions") or {}
    if dims:
        summary = sep.join(f"{k}:{v['source']}/{v['status']}" for k, v in dims.items())
        out.append(t(loc, "check.alignment.line", items=summary))
    qa = al.get("qa") or {}
    if qa.get("unavailableReason"):
        out.append("  " + t(loc, "check.alignment.qaUnavailableLine",
                            reason=qa["unavailableReason"]))
    if al.get("pending"):
        pairs = [(k, dims.get(k, {}).get("reason", "")) for k in al["pending"]]
        out.append("  " + t(loc, "check.alignment.pendingLine",
                            items=_format_labeled_reasons(loc, pairs)))
    if al.get("conflicts"):
        pairs = [(k, dims[k].get("reason", "")) for k in al["conflicts"] if k in dims]
        out.append("  " + t(loc, "check.alignment.conflictLine",
                            items=_format_labeled_reasons(loc, pairs)))
    dev_pairs = _alignment_deviation_pairs(al.get("deviations"), dims)
    if dev_pairs:
        out.append("  " + t(loc, "check.alignment.deviationsLine",
                            items=_format_labeled_reasons(loc, dev_pairs)))
    return out


def render_summary(result: dict) -> str:
    """由同一份结构化结果产生可读摘要，避免两份相互矛盾的事实来源（决策 7）。"""
    loc = result["locale"]
    lines: list[str] = [t(loc, "check.title")]
    v = result["verdict"]
    lines.append(t(loc, "check.draft.complete" if v["draftComplete"]
                   else "check.draft.incomplete"))
    failed = [c for c in result["checks"] if c["status"] == "fail"]
    passed = [c for c in result["checks"] if c["status"] == "pass"]
    if failed:
        lines.append(t(loc, "check.program.failed", failed=len(failed)))
    else:
        lines.append(t(loc, "check.program.pass", n=len(passed)))
    for c in result["checks"]:
        if c["status"] == "pass":
            lines.append("  ✓ " + t(loc, "check.line.pass", item=t(loc, "check.item." + c["id"])))
        elif c["status"] == "fail":
            lines.append("  ✗ " + t(loc, "check.line.fail", item=t(loc, "check.item." + c["id"]),
                                    detail=c["detail"]))
        else:
            if c["id"] == "script.pages":
                reason = t(loc, "check.na")
            elif c["id"] == "report.navigation":
                reason = t(loc, "check.naNav")
            else:
                reason = t(loc, "check.naRecord")
            lines.append("  — " + t(loc, "check.line.na", item=t(loc, "check.item." + c["id"]),
                                    reason=reason))
    for w in result["warnings"]:
        lines.append("  ⚠ " + w)
    lines.append(t(loc, "check.coverage.label"))
    capped = result.get("coverageCapped", {})
    counts = result.get("coverageCounts", {})
    for kind in ("content", "structure", "visual"):
        state = result["coverage"][kind]
        label = t(loc, "check.coverage." + kind)
        if capped.get(kind):
            word = t(loc, "check.coverage.capped")
        else:
            n = counts.get(kind, {})
            word = t(loc, "check.coverage." + state, done=n.get("done", 0),
                     total=n.get("total", 0), failed=n.get("failed", 0))
        lines.append(f"  · {label}：{word}" if loc == "zh-CN" else f"  · {label}: {word}")
    scopes = [s["scope"] for s in result.get("record", {}).get("scopes", [])
              if isinstance(s, dict) and s.get("scope")]
    if scopes:
        joined = "、".join(dict.fromkeys(scopes)) if loc == "zh-CN" \
            else ", ".join(dict.fromkeys(scopes))
        lines.append("  " + t(loc, "check.scope.line", items=joined))
    # 改稿维护声明（#75）：本次重新核验的范围与待核验范围，摘要如实披露。
    rev = result.get("record", {}).get("revision")
    if rev:
        sep = "、" if loc == "zh-CN" else ", "
        names = [t(loc, "check.check." + cid) for cid in rev.get("rechecked", [])]
        if names:
            lines.append("  " + t(loc, "check.revision.line", items=sep.join(names)))
        if rev.get("pending"):
            pending_sep = "；" if loc == "zh-CN" else "; "
            items = pending_sep.join(f"{x['scope']}（{x['reason']}）" if loc == "zh-CN"
                                     else f"{x['scope']} ({x['reason']})"
                                     for x in rev["pending"])
            lines.append("  " + t(loc, "check.revision.pendingLine", items=items))
    reading = result.get("reading")
    if reading and (reading["read"] or reading["partial"] or reading["unreadable"]):
        lines.append(t(loc, "check.reading.line", read=reading["read"],
                       partial=reading["partial"], unreadable=reading["unreadable"]))
        if reading["unread"]:
            items = "；".join(f"{e['path']} — {e['detail']}" if e.get("detail") else e["path"]
                              for e in reading["unread"])
            lines.append("  " + t(loc, "check.reading.unread", items=items))
    if result.get("gaps"):
        lines.append("  " + t(loc, "check.gaps.line", gaps="；".join(result["gaps"])))
    # 实际生成方法与能力缺口的如实披露（issue #74）：只有声明了 requested /
    # unavailable 才出现对应行，没有就不渲染，不制造虚假说明。
    rec_method = result.get("record", {}).get("method")
    if rec_method:
        kind = rec_method.get("kind")
        kind_label = t(loc, "check.method.kind." + kind) if kind in ("builtin", "external") \
            else str(kind)
        lines.append(t(loc, "check.method.line", name=rec_method.get("name") or "?",
                       kind=kind_label))
        if rec_method.get("requested"):
            note = rec_method.get("note")
            lines.append("  " + t(loc, "check.method.requested",
                                  requested=rec_method["requested"],
                                  note=note.strip() if isinstance(note, str) and note.strip()
                                  else t(loc, "check.method.noReason")))
        if rec_method.get("unavailable"):
            lines.append("  " + t(loc, "check.method.unavailable",
                                  items="；".join(rec_method["unavailable"])))
    rec = result["record"]
    rec_align = rec.get("alignment")
    if rec_align is not None:
        lines.extend(_render_alignment_summary(loc, rec_align))
    rec_len = rec.get("length")
    if rec_len and (rec_len["supplementPages"] > 0 or rec_len["nature"] != "hard"
                    or not rec_len["includeSupplements"]):
        lines.append("  " + t(loc, "check.length.line",
                              counted=rec_len["countedPages"],
                              total=rec_len["totalPages"],
                              main=rec_len["mainPages"],
                              supplement=rec_len["supplementPages"],
                              expected=rec_len["expected"],
                              nature=t(loc, "check.length.nature." + rec_len["nature"])))
    rec_input = (result.get("inputs") or {}).get("record")
    if rec_input:
        lines.append(t(loc, "check.record.path", path=rec_input["path"]))
    if rec["version"] is None:
        rec_line = t(loc, "check.record.line", version=t(loc, "check.record.missing"),
                     fresh="")
        # fresh 留空时中英模板都留下尾随分隔符（「，」/「, 」），两种一起剥掉
        lines.append(rec_line.rstrip("，, "))
    else:
        lines.append(t(loc, "check.record.line", version=rec["version"],
                       fresh=t(loc, "check.record.fresh" if rec["fresh"] else "check.record.stale")))
        oi = rec["openItems"]
        lines.append(t(loc, "check.openItems.line", n=oi["n"], blocking=oi["blocking"]))
    for d in result.get("deliverables") or []:
        label = d.get("file") or d.get("note") or d.get("kind") or "?"
        lines.append("  " + t(loc, "check.deliverables.line", file=label,
                              deliverable=t(loc, "check.deliverables.yes"
                                            if d.get("deliverable")
                                            else "check.deliverables.no"),
                              powerpoint=d.get("powerpoint") or "not_run"))
    lines.append(t(loc, "check.deliverable.yes" if v["deliverable"]
                   else "check.deliverable.no"))
    for r in v["reasons"]:
        lines.append(f"  - {r}")
    lines.append(t(loc, "check.next" if v["draftComplete"] else "check.next.blocked"))
    return "\n".join(lines)


class UsageError(Exception):
    """输入或用法错误：退出码 2。"""


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("report", nargs="?")
    parser.add_argument("--script")
    parser.add_argument("--record")
    parser.add_argument("--locale")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("-h", "--help", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    fsutil.configure_stdio()
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        if args.help:
            print(t("zh-CN", "check.usage"))
            print(t("en", "check.usage"))
            return 0
        if not args.report:
            parser.error("report")
        i18n.resolve_locale(args.locale, system_languages=i18n.system_languages())
    except i18n.LocaleError as exc:
        print(t("zh-CN", "check.badLocale", locale=exc.params.get("locale", "")), file=sys.stderr)
        return 2
    try:
        result = check_report(Path(args.report),
                              Path(args.script) if args.script else None,
                              Path(args.record) if args.record else None,
                              locale=args.locale)
    except UsageError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print(render_summary(result))
    return 1 if any(c["status"] == "fail" for c in result["checks"]) else 0


if __name__ == "__main__":
    sys.exit(main())
