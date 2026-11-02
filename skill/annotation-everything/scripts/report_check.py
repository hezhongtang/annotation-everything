#!/usr/bin/env python3
"""报告产物核验入口：接收报告、可选讲稿与可选核验记录，输出结构化结果与可读摘要。

这是随技能发布的用户运行时能力（issue #69 决策 6、#70），与仓库的开发验证入口
（verify.py）职责分离。本入口只执行确定规则（决策 9）：报告可解析性、页面标识、
可选讲稿对应关系、记录结构与版本、记录与产物的对应（新鲜度）、记录引用页、
明确声明输入与口径的可复算数值、记录检查集合的完整性。内容与视觉核验由生成方
按记录声明执行，本入口只汇总其覆盖情况；未执行的核验不显示为通过，程序检查
通过也不自动形成「报告可交付」结论。

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
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import build_viewer
import i18n

RECORD_SCHEMA = "ae-report-record/1"
RESULT_SCHEMA = "ae-report-check/1"
SUPPORTED_LOCALES = ("zh-CN", "en")

# 记录里内容 / 视觉核验的内置检查 id（决策 10、11 的清单；本切片钉住语义，
# 后续切片只追加）。记录声明这里没有的 id 时无法证明其已执行。
KNOWN_RECORD_CHECKS = {
    "content.requirements": "content",
    "content.claims": "content",
    "content.fact-vs-inference": "content",
    "content.units-dates": "content",
    "content.numbers-charts": "content",
    "content.cross-page": "content",
    "content.open-questions": "content",
    "visual.pages": "visual",
}
CHECK_STATUSES = ("pass", "fail", "not_run", "unavailable")
REQUIRED_RECORD_FIELDS = ("schema", "generatedAt", "request", "sources", "method",
                          "links", "findings", "gaps", "checks", "openItems", "verdict")

# 可复算运算（决策 9）：op 是封闭枚举，输入是明确数字，不执行材料中的代码。
CALC_OPS = {
    "sum": lambda v: sum(v),
    "difference": lambda v: v["a"] - v["b"],
    "product": lambda v: v["a"] * v["b"],
    "ratio": lambda v: v["a"] / v["b"],
    "percent-change": lambda v: (v["b"] - v["a"]) / v["a"] * 100,
}


def t(loc: str, key: str, **params: object) -> str:
    return i18n.translate(i18n.load_catalog(loc), key, **params)


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


# ── 单项检查的收集器 ────────────────────────────────────────────────────────

class Checks:
    """按固定顺序收集检查结果；id 与 status 语言无关，detail 本地化。"""

    ORDER = ("report.parse", "report.pageKeys", "script.pages", "record.structure",
             "record.freshness", "record.pageRefs", "record.calculations", "record.checks")

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
                                 "record.check-status": "check.err.checkStatus"}.get(
                                     code, "check.err.checksEmpty"),
               }[cid]
        detail = t(self.locale, key, **params) if params else ""
        self.items[cid] = {"id": cid, "status": state, "code": code, "detail": detail}

    def warn(self, key: str, **params: object) -> str:
        return t(self.locale, key, **params)

    def failed(self) -> list[dict]:
        return [c for c in self.items.values() if c["status"] == "fail"]


# ── 报告与讲稿（复用 build_viewer 的解析约定） ──────────────────────────────

def check_report_and_script(report_doc: str, script_text: str | None, c: Checks) -> tuple[list[dict], bool]:
    """返回 (页面列表, 产物自洽)。页面列表给后续检查用。"""
    draft_ok = True
    pages: list[dict] = []
    try:
        pages = build_viewer.find_pages(report_doc, locale=c.locale)
    except SystemExit as exc:
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
    except (UnicodeDecodeError, ValueError) as exc:
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
    links = record["links"]
    if not isinstance(links, dict) or not isinstance(links.get("report"), str) \
            or not isinstance(links.get("sha256", {}).get("report"), str):
        type_errors.append("links{report,sha256.report}")
    for field, want in (("request", dict), ("method", dict), ("verdict", dict),
                        ("sources", list), ("findings", list), ("gaps", list),
                        ("checks", list), ("openItems", list)):
        if not isinstance(record[field], want):
            type_errors.append(field)
    if type_errors:
        c.set("record.structure", "fail", code="record.bad-type", fields="、".join(type_errors))
        return None, False
    c.set("record.structure", "pass")
    return record, True


def check_freshness(record: dict | None, report_bytes: bytes, script_bytes: bytes | None,
                    c: Checks) -> bool:
    """记录里的产物哈希对不对得上当前文件；返回记录是否新鲜（无记录为 False）。"""
    if record is None:
        return False
    links = record["links"]
    changed = []
    if links["sha256"]["report"] != sha256_bytes(report_bytes):
        changed.append(str(links.get("report") or "report"))
    script_hash = links["sha256"].get("script")
    if script_bytes is not None and script_hash is not None \
            and script_hash != sha256_bytes(script_bytes):
        changed.append(str(links.get("script") or "script"))
    if changed:
        c.set("record.freshness", "fail", code="record.stale", items="、".join(changed))
        return False
    c.set("record.freshness", "pass")
    return True


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


def check_calculations(record: dict | None, c: Checks) -> bool:
    """复算记录里明确声明输入与口径的数值；不执行材料中的代码。"""
    if record is None:
        return True
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
            values = {k: float(v) for k, v in calc["inputs"].items()}
        except (TypeError, ValueError):
            c.set("record.calculations", "fail", code="record.calc-bad",
                  claim=claim, reason="inputs are not all numbers")
            failures += 1
            continue
        try:
            actual = op(values)
        except (KeyError, ZeroDivisionError):
            c.set("record.calculations", "fail", code="record.calc-bad",
                  claim=claim, reason="missing or degenerate inputs")
            failures += 1
            continue
        expected = float(calc["expected"])
        if not math.isfinite(actual) or abs(actual - expected) > 1e-6 * max(1.0, abs(expected)):
            shown = int(actual) if float(actual).is_integer() else round(actual, 4)
            c.set("record.calculations", "fail", code="record.calc-mismatch",
                  claim=claim, actual=shown, expected=calc["expected"])
            failures += 1
    if failures:
        return False
    c.set("record.calculations", "pass")
    return True


def check_record_checks(record: dict | None, c: Checks) -> bool:
    """记录检查集合：非空、id 可识别、状态有效、内容与视觉两类都有声明。"""
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
    missing_kinds = {"content", "visual"} - kinds
    if missing_kinds:
        c.set("record.checks", "fail", code="record.checks-empty",
              reason="、".join(sorted(missing_kinds)))
        return False
    c.set("record.checks", "pass")
    return True


def coverage_for(record: dict | None, fresh: bool, kind: str) -> tuple[str, int, int]:
    """汇总一类核验的覆盖情况；状态语义语言无关。

    covered 全部通过；partial 有通过也有未完成；uncovered 有失败；
    not_run 未执行（没有记录，或记录里没有该项通过结果）；
    unverified 无法证明（记录对不上当前产物）。
    """
    if record is None:
        return "not_run", 0, 0
    if not fresh:
        return "unverified", 0, 0
    items = [x for x in record["checks"] if isinstance(x, dict)
             and x.get("id") in KNOWN_RECORD_CHECKS
             and KNOWN_RECORD_CHECKS[x["id"]] == kind]
    total = len(items)
    passed = sum(1 for x in items if x.get("status") == "pass")
    failed = sum(1 for x in items if x.get("status") == "fail")
    if passed == total:
        return "covered", passed, total
    if failed:
        return "uncovered", passed, total
    if passed:
        return "partial", passed, total
    return "not_run", 0, total


# ── 汇总判定 ────────────────────────────────────────────────────────────────

def summarize(record: dict | None, fresh: bool, checks: Checks, draft_ok: bool,
              record_ok: bool) -> dict:
    reasons: list[str] = []
    failed_program = checks.failed()
    draft_complete = draft_ok and not [f for f in failed_program
                                       if f["id"] in ("report.parse", "report.pageKeys",
                                                      "script.pages", "record.pageRefs")]
    coverage = {"content": coverage_for(record, fresh, "content")[0],
                "visual": coverage_for(record, fresh, "visual")[0]}
    declared = None if record is None else record["verdict"].get("deliverable")
    declared = declared if isinstance(declared, bool) else None
    deliverable = draft_complete and record is not None and fresh and record_ok \
        and not failed_program
    if deliverable:
        deliverable = coverage["content"] == "covered" and coverage["visual"] == "covered"
        if deliverable:
            blocking = [x.get("item") for x in record["openItems"]
                        if isinstance(x, dict) and x.get("blocking") is True
                        and isinstance(x.get("item"), str)]
            if blocking:
                deliverable = False
                reasons.append(t(checks.locale, "check.reason.blocking",
                                 items="；".join(blocking)))
        else:
            not_run = [k for k, v in coverage.items() if v != "covered"]
            reasons.append(t(checks.locale, "check.reason.notRun",
                             items="、".join(t(checks.locale,
                                              "check.coverage.content" if k == "content"
                                              else "check.coverage.visual") for k in not_run)))
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
            reasons.append(t(checks.locale, "check.reason.programFailed",
                             items="、".join(checks.name(f["id"]) for f in failed_program)))
    return {
        "checks": [checks.items[cid] for cid in Checks.ORDER],
        "warnings": list(checks.warnings),
        "record": {"version": None if record is None else record["schema"],
                   "fresh": bool(record is not None and fresh),
                   "declaredDeliverable": declared,
                   "declaredDraftComplete": None if record is None
                   else bool(record["verdict"].get("draftComplete"))},
        "coverage": coverage,
        "verdict": {"draftComplete": draft_complete, "deliverable": deliverable,
                    "reasons": reasons},
    }


# ── 公开接口 ────────────────────────────────────────────────────────────────

def check_report(report_path: Path, script_path: Path | None = None,
                 record_path: Path | None = None, locale: str | None = None) -> dict:
    """检查一套报告产物；返回结构化结果（字段名与状态值语言无关）。"""
    loc = i18n.resolve_locale(locale, system_languages=i18n.system_languages())
    c = Checks(loc)
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
    record, record_ok = load_record(record_path, c)
    fresh = check_freshness(record, report_bytes, script_bytes, c)
    if record is not None:
        check_page_refs(record, pages, c)
        check_calculations(record, c)
        check_record_checks(record, c)
    result = summarize(record, fresh, c, draft_ok, record_ok)
    result["schema"] = RESULT_SCHEMA
    result["inputs"] = {
        "report": {"path": report_path.name, "sha256": sha256_bytes(report_bytes)},
        "script": None if script_path is None
        else {"path": script_path.name, "sha256": sha256_bytes(script_bytes)},
        "record": None if record_path is None
        else {"path": record_path.name},
    }
    result["locale"] = loc
    return result


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
            reason = t(loc, "check.na") if c["id"] == "script.pages" else t(loc, "check.naRecord")
            lines.append("  — " + t(loc, "check.line.na", item=t(loc, "check.item." + c["id"]),
                                    reason=reason))
    for w in result["warnings"]:
        lines.append("  ⚠ " + w)
    lines.append(t(loc, "check.coverage.label"))
    for kind in ("content", "visual"):
        state = result["coverage"][kind]
        label = t(loc, "check.coverage." + kind)
        word = t(loc, "check.coverage." + state)
        lines.append(f"  · {label}：{word}" if loc == "zh-CN" else f"  · {label}: {word}")
    rec = result["record"]
    if rec["version"] is None:
        rec_line = t(loc, "check.record.line", version=t(loc, "check.record.missing"),
                     fresh="")
        lines.append(rec_line.rstrip("，"))
    else:
        lines.append(t(loc, "check.record.line", version=rec["version"],
                       fresh=t(loc, "check.record.fresh" if rec["fresh"] else "check.record.stale")))
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
