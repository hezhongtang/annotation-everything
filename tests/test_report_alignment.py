"""初稿生成前对齐信息密度、视觉风格、导航与核验记录扩展的契约测试（issue #169）。

覆盖：
- Checks.ORDER 尾部追加 `record.alignment` 与 `report.navigation`，既有 15 项顺序不变；
- `request.alignment`、`request.qa`、`deviations` 校验（来源标注、委派依据、问答工具
  可用性/不可用原因、异步待确认项、内部冲突不写全部满足、偏离披露与摘要呈现）；
- 与未包含新增字段的旧版 `ae-report-record/1` 保持兼容（判不适用、摘要如实标未记录偏好）。
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "skill" / "annotation-everything" / "scripts"
sys.path.insert(0, str(SCRIPTS))
os.environ["AE_HOME"] = tempfile.mkdtemp(prefix="ae-test-home-")
os.environ.pop("AE_LOCALE", None)

import report_check as rc  # noqa: E402

RECORD_SCHEMA = "ae-report-record/1"
CONTENT_IDS = (
    "content.requirements",
    "content.claims",
    "content.fact-vs-inference",
    "content.units-dates",
    "content.numbers-charts",
    "content.cross-page",
    "content.script-consistency",
    "content.open-questions",
)
STRUCTURE_IDS = (
    "structure.flow",
    "structure.headings",
    "structure.claim-support",
    "structure.dup-gaps",
    "structure.length",
)
VISUAL_IDS = ("visual.pages",)

MATERIAL = "# 季度经营简记（合成）\n\n- 三季度收入 112 万元\n- 上年同期收入 100 万元\n"

REPORT = """<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><title>合成对齐测试</title>
<style>.page{width:1280px;height:720px;padding:56px;box-sizing:border-box;font-family:sans-serif}</style></head><body>
<section class="page" data-page="1" data-key="sum" id="page-sum"><h2>总结论</h2><p>三季度收入 112 万元。</p></section>
<section class="page" data-page="2" data-key="ev" id="page-ev"><h2>关键证据</h2><p>上年同期收入 100 万元。</p></section>
</body></html>
"""

SCRIPT = """# 讲稿：合成对齐测试

### 第 1 页｜总结论
**讲法**：
先讲总览结论。

### 第 2 页｜关键证据
**讲法**：
对照去年同期基数。
"""


def tmpdir() -> Path:
    return Path(tempfile.mkdtemp())


def sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def all_checks(status: str = "pass", *, overrides: dict[str, str] | None = None, page_count: int = 2) -> list[dict]:
    ovr = overrides or {}
    pages = [{"page": n, "checked": True, "issues": []} for n in range(1, page_count + 1)]
    items = []
    for cid in CONTENT_IDS + STRUCTURE_IDS + VISUAL_IDS:
        kind = "visual" if cid in VISUAL_IDS else ("structure" if cid in STRUCTURE_IDS else "content")
        st = ovr.get(cid, status)
        entry = {"id": cid, "kind": kind, "status": st, "scope": "全部", "note": "合成数据"}
        if cid in VISUAL_IDS:
            entry["pages"] = pages
        items.append(entry)
    return items


def make_record(report: str = REPORT, script: str | None = SCRIPT, **overrides) -> dict:
    sha_map = {"report": sha(report)}
    links: dict = {"report": "report.html", "sha256": sha_map, "sourceNote": "materials/notes.md"}
    if script is not None:
        links["script"] = "script.md"
        sha_map["script"] = sha(script)
    rec = {
        "schema": RECORD_SCHEMA,
        "generatedAt": "2026-11-16T09:00:00+08:00",
        "request": {
            "purpose": "管理层季度汇报",
            "audience": "管理层",
            "coreQuestions": ["收入增长来源"],
            "length": "2 页",
            "script": "yes" if script is not None else "no",
        },
        "sources": [
            {
                "path": "materials/notes.md",
                "status": "read",
                "coverage": "全文",
                "sha256": sha(MATERIAL),
                "note": "",
            }
        ],
        "method": {
            "kind": "builtin",
            "name": "annotation-everything 内置范式",
            "version": "0.9.0",
            "unavailable": [],
        },
        "links": links,
        "findings": [
            {
                "claim": "三季度收入 112 万元",
                "type": "fact",
                "sources": ["materials/notes.md"],
                "pages": ["sum"],
                "refs": [
                    {"path": "materials/notes.md", "locator": "第 3 行", "quote": "三季度收入 112 万元", "found": True}
                ],
                "support": "supported",
                "basis": "直接引自材料第 3 行",
            }
        ],
        "gaps": [],
        "checks": all_checks("pass"),
        "openItems": [],
        "verdict": {"draftComplete": True, "deliverable": True},
    }
    rec.update(overrides)
    return rec


def write_case(d: Path, *, report: str = REPORT, script: str | None = SCRIPT, record: dict | None = None) -> dict:
    d.mkdir(parents=True, exist_ok=True)
    (d / "materials").mkdir(exist_ok=True)
    (d / "materials" / "notes.md").write_bytes(MATERIAL.encode("utf-8"))
    paths = {"report": d / "report.html"}
    paths["report"].write_bytes(report.encode("utf-8"))
    if script is not None:
        paths["script"] = d / "script.md"
        paths["script"].write_bytes(script.encode("utf-8"))
    if record is not None:
        paths["record"] = d / "report-record.json"
        paths["record"].write_bytes((json.dumps(record, ensure_ascii=False, indent=2) + "\n").encode("utf-8"))
    return paths


def run_check(paths: dict, *, locale: str = "zh-CN", json_out: bool = True) -> tuple[int, dict | None, str, str]:
    argv = [str(paths["report"])]
    if paths.get("script"):
        argv += ["--script", str(paths["script"])]
    if paths.get("record"):
        argv += ["--record", str(paths["record"])]
    argv += ["--locale", locale]
    if json_out:
        argv += ["--json"]
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = rc.main(argv)
    text = out.getvalue()
    payload = json.loads(text) if json_out else None
    return code, payload, text, err.getvalue()


def by_id(payload: dict, cid: str) -> dict:
    for item in payload["checks"]:
        if item["id"] == cid:
            return item
    raise AssertionError(f"未找到检查项 {cid}: {payload['checks']}")


class TestRecordAlignment(unittest.TestCase):
    def test_check_order_appends_alignment_and_navigation(self):
        expected_prefix = (
            "report.parse",
            "report.pageKeys",
            "script.pages",
            "record.structure",
            "record.freshness",
            "record.pageRefs",
            "record.calculations",
            "record.checks",
            "record.claims",
            "record.reading",
            "record.sources",
            "record.visual",
            "record.length",
            "record.method",
            "record.revision",
        )
        self.assertEqual(rc.Checks.ORDER[:15], expected_prefix)
        self.assertEqual(rc.Checks.ORDER[15:], ("record.alignment", "report.navigation", "record.deliverables"))

    def test_legacy_record_without_alignment_remains_compatible(self):
        paths = write_case(tmpdir(), record=make_record())
        code, payload, _, _ = run_check(paths, locale="zh-CN", json_out=True)
        self.assertEqual(code, 0)
        self.assertEqual(by_id(payload, "record.alignment")["status"], "not_applicable")
        self.assertEqual(by_id(payload, "report.navigation")["status"], "not_applicable")
        self.assertTrue(payload["verdict"]["draftComplete"])
        self.assertTrue(payload["verdict"]["deliverable"])
        self.assertFalse(payload["record"]["alignment"]["recorded"])

        _, _, zh_text, _ = run_check(paths, locale="zh-CN", json_out=False)
        _, _, en_text, _ = run_check(paths, locale="en", json_out=False)
        self.assertIn("未记录偏好", zh_text)
        self.assertIn("not recorded", en_text.lower())

    def test_valid_alignment_sources_and_qa_pass(self):
        rec = make_record()
        rec["request"]["alignment"] = {
            "density": {"value": "standalone", "source": "user", "status": "confirmed"},
            "visual": {
                "value": "clean-editorial",
                "source": "delegated",
                "status": "confirmed",
                "basis": "用户委派由 Agent 决定，采用克制报告排版默认样式",
            },
            "navigation": {
                "value": "conclusion-to-evidence",
                "source": "reused",
                "status": "confirmed",
                "reason": "复用历史季度汇报模板约束",
            },
        }
        rec["request"]["qa"] = {
            "used": True,
            "tool": "ask_question",
            "asked": ["density", "visual", "navigation"],
            "unanswered": [],
        }
        paths = write_case(tmpdir(), record=rec)
        code, payload, _, _ = run_check(paths, locale="zh-CN", json_out=True)
        self.assertEqual(code, 0)
        self.assertEqual(by_id(payload, "record.alignment")["status"], "pass")
        align = payload["record"]["alignment"]
        self.assertTrue(align["recorded"])
        self.assertEqual(align["dimensions"]["density"]["source"], "user")
        self.assertEqual(align["dimensions"]["visual"]["source"], "delegated")
        self.assertTrue(payload["verdict"]["deliverable"])

    def test_delegated_requires_basis_and_inferred_requires_reason(self):
        rec1 = make_record()
        rec1["request"]["alignment"] = {"visual": {"value": "minimal", "source": "delegated", "status": "confirmed"}}
        code1, p1, _, _ = run_check(write_case(tmpdir(), record=rec1), json_out=True)
        self.assertEqual(code1, 1)
        self.assertEqual(by_id(p1, "record.alignment")["status"], "fail")

        rec2 = make_record()
        rec2["request"]["alignment"] = {"density": {"value": "deep", "source": "inferred", "status": "pending"}}
        code2, p2, _, _ = run_check(write_case(tmpdir(), record=rec2), json_out=True)
        self.assertEqual(code2, 1)
        self.assertEqual(by_id(p2, "record.alignment")["status"], "fail")

        rec3 = make_record()
        rec3["request"]["alignment"] = {"density": {"value": "deep", "source": "bad-source", "status": "confirmed"}}
        code3, p3, _, _ = run_check(write_case(tmpdir(), record=rec3), json_out=True)
        self.assertEqual(code3, 1)
        self.assertEqual(by_id(p3, "record.alignment")["status"], "fail")

    def test_qa_unavailable_or_skipped_validation(self):
        # 问答工具不可用但缺少具体原因 -> fail
        rec_missing_reason = make_record()
        rec_missing_reason["request"]["alignment"] = {
            "density": {
                "value": "standalone",
                "source": "default",
                "status": "pending",
                "reason": "未询问用户，暂按独立阅读默认",
            }
        }
        rec_missing_reason["request"]["qa"] = {"used": False, "available": False}
        code1, p1, _, _ = run_check(write_case(tmpdir(), record=rec_missing_reason), json_out=True)
        self.assertEqual(code1, 1)
        self.assertEqual(by_id(p1, "record.alignment")["status"], "fail")

        # 有不可用原因 -> record.alignment 通过并披露待确认默认方案
        rec_with_reason = make_record()
        rec_with_reason["request"]["alignment"] = {
            "density": {
                "value": "standalone",
                "source": "default",
                "status": "pending",
                "reason": "按默认独立阅读方案生成",
            }
        }
        rec_with_reason["request"]["qa"] = {
            "used": False,
            "available": False,
            "unavailableReason": "当前宿主通道无交互式问答工具",
            "unanswered": ["density"],
        }
        rec_with_reason["verdict"] = {"draftComplete": True, "deliverable": False}
        paths_ok = write_case(tmpdir(), record=rec_with_reason)
        code2, p2, _, _ = run_check(paths_ok, json_out=True)
        self.assertEqual(code2, 0)
        self.assertEqual(by_id(p2, "record.alignment")["status"], "pass")
        self.assertEqual(p2["record"]["alignment"]["qa"]["unavailableReason"], "当前宿主通道无交互式问答工具")

        # 明明未禁用也没不可用原因却跳过问答留下 default/inferred -> fail
        rec_skipped = make_record()
        rec_skipped["request"]["alignment"] = {
            "density": {"value": "standalone", "source": "default", "status": "pending", "reason": "自行假定默认"}
        }
        rec_skipped["request"]["qa"] = {"used": False}
        code3, p3, _, _ = run_check(write_case(tmpdir(), record=rec_skipped), json_out=True)
        self.assertEqual(code3, 1)
        self.assertEqual(by_id(p3, "record.alignment")["status"], "fail")

    def test_async_unanswered_pending_allows_draft_blocks_deliverable(self):
        rec = make_record()
        rec["request"]["alignment"] = {
            "density": {"value": "standalone", "source": "user", "status": "confirmed"},
            "visual": {
                "value": "warm-editorial",
                "source": "default",
                "status": "pending",
                "reason": "异步问答超时未回复，按默认方案继续生成，待审阅确认",
            },
        }
        rec["request"]["qa"] = {
            "used": True,
            "tool": "ask_question",
            "asked": ["density", "visual"],
            "unanswered": ["visual"],
        }
        rec["verdict"] = {"draftComplete": True, "deliverable": False}
        paths = write_case(tmpdir(), record=rec)
        code, p, _, _ = run_check(paths, locale="zh-CN", json_out=True)
        self.assertEqual(code, 0)
        self.assertTrue(p["verdict"]["draftComplete"])
        self.assertFalse(p["verdict"]["deliverable"])
        self.assertEqual(p["record"]["alignment"]["pending"], ["visual"])

        _, _, zh_text, _ = run_check(paths, locale="zh-CN", json_out=False)
        _, _, en_text, _ = run_check(paths, locale="en", json_out=False)
        self.assertIn("待确认", zh_text)
        self.assertIn("visual", zh_text)
        self.assertIn("pending", en_text.lower())

    def test_unresolved_conflict_blocks_covered_and_deliverable(self):
        # 情况 A：诚实标注冲突且未将 content.requirements 标为 pass
        rec_honest = make_record(checks=all_checks("pass", overrides={"content.requirements": "not_run"}))
        rec_honest["request"]["alignment"] = {
            "density": {
                "value": "deep",
                "source": "user",
                "status": "conflict",
                "reason": "同时要求极高细节与极少页数，尚未确认取舍",
            },
        }
        rec_honest["verdict"] = {"draftComplete": True, "deliverable": False}
        code1, p1, _, _ = run_check(write_case(tmpdir(), record=rec_honest), json_out=True)
        self.assertEqual(code1, 0)
        self.assertEqual(by_id(p1, "record.alignment")["status"], "pass")
        self.assertNotEqual(p1["coverage"]["content"], "covered")
        self.assertFalse(p1["verdict"]["deliverable"])
        self.assertEqual(p1["record"]["alignment"]["conflicts"], ["density"])

        # 情况 B：存在未解决冲突却把 content.requirements 宣称为 pass -> 程序检查失败且不能 covered/deliverable
        rec_dishonest = make_record(checks=all_checks("pass"))
        rec_dishonest["request"]["alignment"] = {
            "density": {
                "value": "deep",
                "source": "user",
                "status": "conflict",
                "reason": "高密度与页数上限矛盾尚未解决",
            },
        }
        code2, p2, _, _ = run_check(write_case(tmpdir(), record=rec_dishonest), json_out=True)
        self.assertEqual(code2, 1)
        self.assertEqual(by_id(p2, "record.alignment")["status"], "fail")
        self.assertNotEqual(p2["coverage"]["content"], "covered")
        self.assertFalse(p2["verdict"]["deliverable"])

    def test_unanswered_qa_blocks_delivery_without_pending_dimension(self):
        for alignment in (
            None,
            {
                "visual": {"value": "clean", "source": "user", "status": "confirmed"},
            },
        ):
            with self.subTest(alignment=alignment):
                rec = make_record()
                if alignment is not None:
                    rec["request"]["alignment"] = alignment
                rec["request"]["qa"] = {
                    "used": True,
                    "asked": ["visual"],
                    "unanswered": ["visual", "visual"],
                }
                paths = write_case(tmpdir(), record=rec)
                code, payload, _, _ = run_check(paths)
                self.assertEqual(code, 0)
                self.assertTrue(payload["verdict"]["draftComplete"])
                self.assertFalse(payload["verdict"]["deliverable"])
                self.assertEqual(payload["record"]["alignment"]["pending"], ["visual"])
                for locale, label in (("zh-CN", "待确认"), ("en", "pending")):
                    _, _, summary, _ = run_check(paths, locale=locale, json_out=False)
                    pending_lines = [line for line in summary.splitlines() if label in line.lower()]
                    self.assertTrue(any("visual" in line for line in pending_lines), summary)

    def test_answered_qa_without_dimensions_does_not_block_delivery(self):
        rec = make_record()
        rec["request"]["qa"] = {"used": True, "asked": ["visual"], "unanswered": []}
        code, payload, _, _ = run_check(write_case(tmpdir(), record=rec))
        self.assertEqual(code, 0)
        self.assertTrue(payload["verdict"]["deliverable"])

    def test_deviations_disclosed_and_block_full_satisfaction(self):
        rec = make_record()
        rec["request"]["alignment"] = {
            "visual": {
                "value": "dense-table",
                "source": "user",
                "status": "deviated",
                "reason": "单页放不下双宽表，拆为两页避免溢出",
            },
        }
        rec["deviations"] = [{"item": "visual.table-layout", "reason": "单页容量不足以同时放下两张宽表，拆页展示"}]
        paths = write_case(tmpdir(), record=rec)
        code, p, _, _ = run_check(paths, locale="zh-CN", json_out=True)
        self.assertEqual(code, 0)
        self.assertTrue(p["verdict"]["draftComplete"])
        self.assertFalse(p["verdict"]["deliverable"])
        self.assertEqual(p["coverage"]["content"], "partial")
        self.assertEqual(len(p["record"]["alignment"]["deviations"]), 1)

        _, _, zh_text, _ = run_check(paths, locale="zh-CN", json_out=False)
        _, _, en_text, _ = run_check(paths, locale="en", json_out=False)
        self.assertIn("偏离", zh_text)
        self.assertIn("visual.table-layout", zh_text)
        self.assertIn("deviation", en_text.lower())

    def test_deviated_dimension_renders_without_top_level_list(self):
        rec = make_record()
        rec["request"]["alignment"] = {
            "visual": {"value": "dense-table", "source": "user", "status": "deviated", "reason": "拆分表格页避免溢出"},
        }
        paths = write_case(tmpdir(), record=rec)
        _, _, zh_text, _ = run_check(paths, locale="zh-CN", json_out=False)
        _, _, en_text, _ = run_check(paths, locale="en", json_out=False)
        self.assertIn("偏离", zh_text)
        self.assertIn("拆分表格页避免溢出", zh_text)
        self.assertIn("deviation", en_text.lower())


REPORT_WITH_SUPPLEMENTS = """<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><title>含补充页报告</title>
<style>.page{width:1280px;height:720px;padding:56px;box-sizing:border-box;font-family:sans-serif}</style></head><body>
<section class="page" data-page="1" data-key="sum" id="page-sum"><h2>总结论</h2><p>三季度收入 112 万元。</p></section>
<section class="page" data-page="2" data-key="ev" id="page-ev"><h2>关键证据</h2><p>上年同期收入 100 万元。</p></section>
<section class="page" data-page="3" data-key="sup-detail" id="page-sup-detail" data-kind="supplement"><h2>补充口径表</h2><p>明细计算口径说明。</p></section>
</body></html>
"""

SCRIPT_3 = """# 讲稿：含补充页报告

### 第 1 页｜总结论
**讲法**：
先讲总览结论。

### 第 2 页｜关键证据
**讲法**：
对照去年同期基数。

### 第 3 页｜补充口径表
**讲法**：
说明计算口径补充。
"""


class TestLengthSpecAndSupplements(unittest.TestCase):
    def test_hard_upper_bound_accepts_fewer_pages_and_rejects_excess(self):
        for length in ("最多 6 页", "6 页", "at most 6 pages", "6 pages"):
            for count, passes in ((4, True), (6, True), (7, False)):
                with self.subTest(length=length, count=count):
                    rec = make_record()
                    rec["request"].update(
                        {
                            "length": length,
                            "lengthSpec": {"nature": "hard", "includeSupplements": False},
                        }
                    )
                    pages = [{"kind": ""} for _ in range(count)] + [{"kind": "supplement"}]
                    checks = rc.Checks("zh-CN")
                    info = rc.check_record_length(rec, pages, checks)
                    self.assertEqual(info["inRange"], passes)
                    self.assertEqual(info["countedPages"], count)
                    self.assertEqual(info["expected"], "≤6")

    def test_length_override_range_and_legacy_exact_constraints(self):
        cases = (
            ("6 页", None, False),
            ("6 页", {"nature": "hard", "pages": "最多 4 页"}, True),
            ("6 页", {"nature": "hard", "pages": "5–6 页"}, False),
            ("6 页", {"nature": "hard", "pages": "4–6 页"}, True),
            ("6 页", {"nature": "reference"}, False),
        )
        for length, spec, in_range in cases:
            with self.subTest(spec=spec):
                rec = make_record()
                rec["request"]["length"] = length
                if spec is not None:
                    rec["request"]["lengthSpec"] = spec
                info = rc.check_record_length(rec, [{"kind": ""}] * 4, rc.Checks("zh-CN"))
                self.assertEqual(info["inRange"], in_range)

    def test_hard_limit_excluding_supplements_passes_when_main_pages_fit(self):
        rec = make_record(report=REPORT_WITH_SUPPLEMENTS, script=SCRIPT_3, checks=all_checks("pass", page_count=3))
        rec["request"]["length"] = "2 页"
        rec["request"]["lengthSpec"] = {"nature": "hard", "includeSupplements": False}
        paths = write_case(tmpdir(), report=REPORT_WITH_SUPPLEMENTS, script=SCRIPT_3, record=rec)
        code, p, _, _ = run_check(paths, locale="zh-CN", json_out=True)
        self.assertEqual(code, 0)
        self.assertEqual(by_id(p, "record.length")["status"], "pass")
        length_info = p["record"]["length"]
        self.assertEqual(length_info["totalPages"], 3)
        self.assertEqual(length_info["mainPages"], 2)
        self.assertEqual(length_info["supplementPages"], 1)
        self.assertEqual(length_info["countedPages"], 2)
        self.assertFalse(length_info["includeSupplements"])

    def test_hard_limit_including_supplements_fails_when_total_exceeds(self):
        rec = make_record(report=REPORT_WITH_SUPPLEMENTS, script=SCRIPT_3, checks=all_checks("pass", page_count=3))
        rec["request"]["length"] = "2 页"
        rec["request"]["lengthSpec"] = {"nature": "hard", "includeSupplements": True}
        paths = write_case(tmpdir(), report=REPORT_WITH_SUPPLEMENTS, script=SCRIPT_3, record=rec)
        code, p, _, _ = run_check(paths, locale="zh-CN", json_out=True)
        self.assertEqual(code, 1)
        self.assertEqual(by_id(p, "record.length")["status"], "fail")
        self.assertEqual(p["record"]["length"]["countedPages"], 3)

    def test_reference_target_warns_without_failing_program_check(self):
        rec = make_record(report=REPORT_WITH_SUPPLEMENTS, script=SCRIPT_3, checks=all_checks("pass", page_count=3))
        rec["request"]["length"] = "2 页"
        rec["request"]["lengthSpec"] = {"nature": "reference", "includeSupplements": True}
        paths = write_case(tmpdir(), report=REPORT_WITH_SUPPLEMENTS, script=SCRIPT_3, record=rec)
        code, p, _, _ = run_check(paths, locale="zh-CN", json_out=True)
        self.assertEqual(code, 0)
        self.assertEqual(by_id(p, "record.length")["status"], "pass")
        self.assertTrue(any("参考" in w or "reference" in w.lower() for w in p["warnings"]))
        self.assertEqual(p["record"]["length"]["nature"], "reference")
        self.assertFalse(p["record"]["length"]["inRange"])

    def test_invalid_length_spec_fails_record_length(self):
        rec = make_record(report=REPORT_WITH_SUPPLEMENTS, script=SCRIPT_3, checks=all_checks("pass", page_count=3))
        rec["request"]["lengthSpec"] = {"nature": "unknown-mode", "includeSupplements": "nope"}
        paths = write_case(tmpdir(), report=REPORT_WITH_SUPPLEMENTS, script=SCRIPT_3, record=rec)
        code, p, _, _ = run_check(paths, locale="zh-CN", json_out=True)
        self.assertEqual(code, 1)
        self.assertEqual(by_id(p, "record.length")["status"], "fail")


NAV_REPORT_OK = """<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><title>导航闭环报告</title>
<style>.page{width:1280px;height:720px;padding:56px;box-sizing:border-box;font-family:sans-serif}</style></head><body>
<section class="page" data-page="1" data-key="toc" id="page-toc">
  <h2>目录</h2>
  <a href="#page-sum" data-range-start="sum" data-range-end="ev-detail"><span class="toc-label">第一部分</span><span class="toc-title"><strong>结论与证据</strong></span></a>
</section>
<section class="page" data-page="2" data-key="sum" id="page-sum">
  <h2>核心结论</h2>
  <p><a href="#page-ev-detail">查看证据详情</a> · <a data-page-ref="sup-method">补充口径</a></p>
</section>
<section class="page" data-page="3" data-key="ev-detail" id="page-ev-detail">
  <h2>关键证据</h2>
  <p><a href="#page-sum">返回对应结论</a></p>
</section>
<section class="page" data-page="4" data-key="sup-method" id="page-sup-method" data-kind="supplement">
  <h2>补充口径</h2>
  <p><a href="#page-sum">返回对应分析</a></p>
</section>
</body></html>
"""

NAV_SCRIPT_OK = """# 讲稿：导航闭环报告

### 第 1 页｜目录
**讲法**：目录总览。

### 第 2 页｜核心结论
**讲法**：先说核心结论。

### 第 3 页｜关键证据
**讲法**：给出关键证据。

### 第 4 页｜补充口径
**讲法**：补充计算口径。
"""


class TestReportNavigation(unittest.TestCase):
    def test_href_requires_anchor_in_the_corresponding_target_page(self):
        link = '<a href="#page-ev">Evidence</a>'
        linked = REPORT.replace("</section>", link + "</section>", 1)
        cases = (
            (linked.replace(' id="page-ev"', ""), "fail"),
            (linked.replace('id="page-ev"', 'id="wrong-id"'), "fail"),
            (
                linked.replace(' id="page-ev"', "").replace("</section>", '<span id="page-ev"></span></section>', 1),
                "fail",
            ),
            (linked.replace('id="page-sum"', 'id="page-ev"'), "fail"),
            (
                linked.replace('id="page-ev"', 'id="wrong-id"').replace(
                    "<h2>关键证据</h2>", '<!-- <span id="page-ev"></span> --><h2>关键证据</h2>'
                ),
                "fail",
            ),
            (
                linked.replace(' id="page-ev"', "").replace("<h2>关键证据</h2>", '<h2 id="page-ev">关键证据</h2>'),
                "pass",
            ),
            (
                linked.replace(' id="page-ev"', "").replace(
                    "<h2>关键证据</h2>", '<a name="page-ev"></a><h2>关键证据</h2>'
                ),
                "pass",
            ),
        )
        for report, expected in cases:
            with self.subTest(report=report):
                rec = make_record(report=report)
                code, payload, _, _ = run_check(write_case(tmpdir(), report=report, record=rec))
                self.assertEqual(code, 1 if expected == "fail" else 0)
                self.assertEqual(by_id(payload, "report.navigation")["status"], expected)
                self.assertEqual(payload["verdict"]["deliverable"], expected == "pass")

    def test_data_page_ref_fallback_does_not_require_html_fragment(self):
        report = REPORT.replace(' id="page-ev"', "").replace(
            "</section>", '<a data-page-ref="ev">Evidence</a></section>', 1
        )
        rec = make_record(report=report)
        code, payload, _, _ = run_check(write_case(tmpdir(), report=report, record=rec))
        self.assertEqual(code, 0)
        self.assertEqual(by_id(payload, "report.navigation")["status"], "pass")

    def test_non_rendered_anchor_text_is_ignored(self):
        anchor = '<a href="#page-removed" data-page-ref="ghost" data-range-start="ghost">old</a>'
        fragments = (
            f"<!-- {anchor} -->",
            f"<script>const oldLink = '{anchor}';</script>",
            f"<style>.note::before {{ content: '{anchor}'; }}</style>",
            f"<textarea>{anchor}</textarea>",
            f"<template><template>{anchor}</template>{anchor}</template>",
        )
        for fragment in fragments:
            with self.subTest(fragment=fragment):
                for real_link, expected in (("", "not_applicable"), ('<a href="#page-ev">Evidence</a>', "pass")):
                    report = REPORT.replace("</section>", fragment + real_link + "</section>", 1)
                    rec = make_record(report=report)
                    code, payload, _, _ = run_check(write_case(tmpdir(), report=report, record=rec))
                    self.assertEqual(code, 0)
                    self.assertEqual(by_id(payload, "report.navigation")["status"], expected)
                    self.assertTrue(payload["verdict"]["deliverable"])

    def test_anchor_attributes_with_greater_than_character_are_parsed(self):
        report = REPORT.replace("</section>", '<a title="A > B" href="#page-missing">Evidence</a></section>', 1)
        rec = make_record(report=report)
        code, payload, _, _ = run_check(write_case(tmpdir(), report=report, record=rec))
        self.assertEqual(code, 1)
        self.assertEqual(by_id(payload, "report.navigation")["code"], "report.nav-broken-link")

    def test_bidirectional_links_and_toc_ranges_pass(self):
        rec = make_record(report=NAV_REPORT_OK, script=NAV_SCRIPT_OK, checks=all_checks("pass", page_count=4))
        rec["request"]["length"] = "4 页"
        rec["findings"][0]["pages"] = ["sum"]
        paths = write_case(tmpdir(), report=NAV_REPORT_OK, script=NAV_SCRIPT_OK, record=rec)
        code, p, _, _ = run_check(paths, locale="zh-CN", json_out=True)
        self.assertEqual(code, 0)
        self.assertEqual(by_id(p, "report.navigation")["status"], "pass")
        self.assertTrue(p["verdict"]["deliverable"])

    def test_broken_href_or_data_page_ref_fails(self):
        bad_href = NAV_REPORT_OK.replace('href="#page-ev-detail"', 'href="#page-missing-page"')
        paths1 = write_case(tmpdir(), report=bad_href)
        code1, p1, _, _ = run_check(paths1, locale="zh-CN", json_out=True)
        self.assertEqual(code1, 1)
        self.assertEqual(by_id(p1, "report.navigation")["status"], "fail")
        self.assertEqual(by_id(p1, "report.navigation")["code"], "report.nav-broken-link")
        self.assertIn("missing-page", by_id(p1, "report.navigation")["detail"])

        bad_ref = NAV_REPORT_OK.replace('data-page-ref="sup-method"', 'data-page-ref="ghost-key"')
        paths2 = write_case(tmpdir(), report=bad_ref)
        code2, p2, _, _ = run_check(paths2, locale="en", json_out=True)
        self.assertEqual(code2, 1)
        self.assertEqual(by_id(p2, "report.navigation")["status"], "fail")
        self.assertEqual(by_id(p2, "report.navigation")["code"], "report.nav-broken-link")
        self.assertIn("ghost-key", by_id(p2, "report.navigation")["detail"])

    def test_broken_or_inverted_range_links_fail(self):
        missing_end = NAV_REPORT_OK.replace('data-range-end="ev-detail"', 'data-range-end="unknown-end"')
        code1, p1, _, _ = run_check(write_case(tmpdir(), report=missing_end), json_out=True)
        self.assertEqual(code1, 1)
        self.assertEqual(by_id(p1, "report.navigation")["status"], "fail")
        self.assertEqual(by_id(p1, "report.navigation")["code"], "report.nav-bad-range")

        inverted = NAV_REPORT_OK.replace(
            'data-range-start="sum" data-range-end="ev-detail"',
            'data-range-start="ev-detail" data-range-end="sum"',
        )
        code2, p2, _, _ = run_check(write_case(tmpdir(), report=inverted), json_out=True)
        self.assertEqual(code2, 1)
        self.assertEqual(by_id(p2, "report.navigation")["status"], "fail")
        self.assertEqual(by_id(p2, "report.navigation")["code"], "report.nav-bad-range")


class TestViewerNavigationAndRebuild(unittest.TestCase):
    def test_infer_sections_nested_tags_and_rebuild_after_reorder(self):
        import build_viewer as bv
        import notes_store as ns

        work = tmpdir()
        rep = work / "report.html"
        scr = work / "script.md"
        out = work / "review"
        rep.write_bytes(NAV_REPORT_OK.encode("utf-8"))
        scr.write_bytes(NAV_SCRIPT_OK.encode("utf-8"))

        res1 = bv.build(rep, scr, out, no_scan_skills=True, locale="zh-CN")
        self.assertGreaterEqual(res1["sections"], 1)
        html1 = (out / "index.html").read_text(encoding="utf-8")
        self.assertIn('id="prevBack"', html1)
        self.assertIn("data-page-ref", html1)
        manifest1 = json.loads((out / "review.json").read_text(encoding="utf-8"))
        self.assertIn(["第一部分 结论与证据", 2, 3], manifest1["sections"])

        # 新增一条落于 sum（第 2 页）的批注，随后将 sum 与 ev-detail 调换顺序并 rebuild
        ns.add(
            out,
            {
                "page": 2,
                "key": "sum",
                "title": "核心结论",
                "target": "报告",
                "text": "保留结论跳转链接",
            },
        )
        reordered = """<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><title>导航闭环报告</title>
<style>.page{width:1280px;height:720px;padding:56px;box-sizing:border-box;font-family:sans-serif}</style></head><body>
<section class="page" data-page="1" data-key="toc" id="page-toc">
  <h2>目录</h2>
  <a href="#page-ev-detail" data-range-start="ev-detail" data-range-end="sum"><span class="toc-label">第一部分</span><span class="toc-title"><strong>证据与结论</strong></span></a>
</section>
<section class="page" data-page="2" data-key="ev-detail" id="page-ev-detail">
  <h2>关键证据</h2>
  <p><a href="#page-sum">返回对应结论</a></p>
</section>
<section class="page" data-page="3" data-key="sum" id="page-sum">
  <h2>核心结论</h2>
  <p><a href="#page-ev-detail">查看证据详情</a></p>
</section>
<section class="page" data-page="4" data-key="sup-method" id="page-sup-method" data-kind="supplement">
  <h2>补充口径</h2>
  <p><a href="#page-sum">返回对应分析</a></p>
</section>
</body></html>
"""
        reordered_script = """# 讲稿：导航闭环报告

### 第 1 页｜目录
**讲法**：目录总览。

### 第 2 页｜关键证据
**讲法**：先看证据。

### 第 3 页｜核心结论
**讲法**：再给结论。

### 第 4 页｜补充口径
**讲法**：补充口径。
"""
        rep.write_bytes(reordered.encode("utf-8"))
        scr.write_bytes(reordered_script.encode("utf-8"))
        bv.main(["--rebuild", str(out), "--no-scan-skills", "--locale", "zh-CN"])
        manifest2 = json.loads((out / "review.json").read_text(encoding="utf-8"))
        self.assertIn(["第一部分 证据与结论", 2, 3], manifest2["sections"])
        moved = [n for n in ns.load(out)["notes"] if n.get("key") == "sum"][0]
        self.assertEqual(moved["page"], 3)
        self.assertEqual(moved["key"], "sum")


class TestSkillDocsAndStylesContract(unittest.TestCase):
    """技能文档、基础样式、安装副本与 README 同步契约（issue #169）。"""

    ZH_DIMENSIONS = (
        "用途与预期行动",
        "读者与阅读方式",
        "核心问题与内容优先级",
        "材料范围与证据口径",
        "信息密度与篇幅约束",
        "视觉风格与参考依据",
        "导航与查找任务",
        "使用环境与交付形式",
        "讲稿与首次审阅重点",
    )
    EN_DIMENSIONS = (
        "Purpose & expected action",
        "Audience & reading mode",
        "Core questions & content priority",
        "Material scope & evidence standards",
        "Information density & length constraints",
        "Visual style & references",
        "Navigation & lookup tasks",
        "Runtime environment & delivery format",
        "Speaker script & initial review focus",
    )

    @classmethod
    def setUpClass(cls):
        sys.path.insert(0, str(ROOT))
        import ae

        target = tmpdir() / "skills-root"
        target.mkdir(parents=True, exist_ok=True)
        out = io.StringIO()
        with redirect_stdout(out):
            rc_code = ae.main(["install", "--target", str(target)])
        assert rc_code == 0, out.getvalue()
        cls.copy = target / "annotation-everything"

    def test_generation_guides_cover_nine_dimensions_density_visual_navigation(self):
        zh = (self.copy / "references" / "generation.zh-CN.md").read_text(encoding="utf-8")
        en = (self.copy / "references" / "generation.en.md").read_text(encoding="utf-8")
        for dim in self.ZH_DIMENSIONS:
            self.assertIn(dim, zh, f"中文生成指引缺少维度：{dim}")
        for dim in self.EN_DIMENSIONS:
            self.assertIn(dim, en, f"英文生成指引缺少维度：{dim}")
        for anchor in (
            "演示为主",
            "独立阅读",
            "深入审阅",
            'data-kind="supplement"',
            "代表性内页校准",
            "request.alignment",
            "request.lengthSpec",
            "request.qa",
            "deviations",
            "record.alignment",
            "report.navigation",
            "Shadow DOM",
            "待确认",
            "问答工具",
        ):
            self.assertIn(anchor, zh, f"中文生成指引缺少锚点：{anchor}")
        for anchor in (
            "presentation-led",
            "standalone reading",
            "deep review",
            'data-kind="supplement"',
            "representative inner-page calibration",
            "request.alignment",
            "request.lengthSpec",
            "request.qa",
            "deviations",
            "record.alignment",
            "report.navigation",
            "Shadow DOM",
            "pending",
            "Q&A tool",
        ):
            self.assertIn(anchor, en, f"英文生成指引缺少锚点：{anchor}")

    def test_agent_mechanisms_formats_workflow_css_and_readme_sync(self):
        mech_zh = (self.copy / "references" / "agent-mechanisms.zh-CN.md").read_text(encoding="utf-8")
        mech_en = (self.copy / "references" / "agent-mechanisms.en.md").read_text(encoding="utf-8")
        self.assertIn("问答", mech_zh)
        self.assertIn("Q&A", mech_en)

        fmt_zh = (self.copy / "references" / "formats.md").read_text(encoding="utf-8")
        fmt_en = (self.copy / "references" / "formats.en.md").read_text(encoding="utf-8")
        for tok in ('data-kind="supplement"', "data-page-ref"):
            self.assertIn(tok, fmt_zh)
            self.assertIn(tok, fmt_en)

        wf_zh = (self.copy / "references" / "workflow.zh-CN.md").read_text(encoding="utf-8")
        wf_en = (self.copy / "references" / "workflow.en.md").read_text(encoding="utf-8")
        self.assertIn("九维度", wf_zh)
        self.assertIn("nine", wf_en.lower())

        css = (self.copy / "references" / "report-base.css").read_text(encoding="utf-8")
        self.assertIn(".ae-supplement", css)
        import build_viewer as bv

        self.assertIn(".ae-supplement", bv.shadow_css(css))

        readme_zh = (ROOT / "README.zh-CN.md").read_text(encoding="utf-8")
        readme_en = (ROOT / "README.md").read_text(encoding="utf-8")
        self.assertIn("test_report_alignment.py", readme_zh)
        self.assertIn("test_report_alignment.py", readme_en)
        self.assertIn("演示为主", readme_zh)
        self.assertIn("presentation-led", readme_en)

    def test_verify_selection_builtin_map_is_clean(self):
        sys.path.insert(0, str(ROOT))
        import verify_selection

        problems = verify_selection.validate_builtin_map(ROOT)
        self.assertEqual(problems, [], f"verify_selection 存在未映射条目：{problems}")


if __name__ == "__main__":
    unittest.main()
