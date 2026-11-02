"""改稿更新核验与既有报告兼容的契约测试（issue #75）。

report_check.py 的 `record.revision` 检查与记录级可选 `revision` 字段钉住
#69 决策 8、13、15 的确定规则：改稿后的记录可以声明本次重新核验的影响范围
（rechecked）与无法确认范围的待核验项（pending，带 scope 与 reason）；声明
重新核验过的项在记录里必须有实际检查结果，无法确认影响范围时如实披露待核验
范围与原因、不能伪称核验完成。核验关联保持可选：没有 revision 的旧记录、
没有记录的旧报告都不受影响，直接进入审阅。旧的可交付判定在报告变化后由
新鲜度检查拦下，受影响项重新核验、未受影响证据保留后才可能恢复。

只用标准库与内联合成数据：python3 -m unittest discover tests
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
os.environ.setdefault("AE_HOME", tempfile.mkdtemp(prefix="ae-test-home-"))
os.environ.pop("AE_LOCALE", None)   # 语言由各用例显式给定

import report_check as rc  # noqa: E402

RECORD_SCHEMA = "ae-report-record/1"
CHECK_SCHEMA = "ae-report-check/1"

CONTENT_CHECK_IDS = ("content.requirements", "content.claims", "content.fact-vs-inference",
                     "content.units-dates", "content.numbers-charts", "content.cross-page",
                     "content.script-consistency", "content.open-questions")
STRUCTURE_CHECK_IDS = ("structure.flow", "structure.headings", "structure.claim-support",
                       "structure.dup-gaps", "structure.length")

MATERIAL = "# 三季度笔记\n\n三季度收入 112 万元，上年同期 100 万元。\n"


def tmpdir() -> Path:
    return Path(tempfile.mkdtemp())


def sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


REPORT = """<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><title>合成季度回顾</title>
<style>.page{width:1280px;height:720px;padding:56px;box-sizing:border-box;font-family:sans-serif}
.kpi{font-size:48px;font-weight:700}</style></head><body>
<section class="page" data-page="1" data-key="sum" id="page-sum"><h2>结论</h2><p class="kpi">收入同比 +12%</p>
<p>新开 3 家门店贡献主要增量。</p></section>
<section class="page" data-page="2" data-key="rev" id="page-rev"><h2>收入结构</h2><p>外卖占比 38%。</p></section>
<section class="page" data-page="3" data-key="next" id="page-next"><h2>下一步</h2><p>两件事。</p></section>
</body></html>
"""

SCRIPT = """# 讲稿：合成季度回顾

### 第 1 页｜结论
**讲法**：
先说结论：收入同比增长 12%。

### 第 2 页｜收入结构
**讲法**：
外卖占比继续上升。

### 第 3 页｜下一步
**讲法**：
两件事。
"""


def visual_evidence(pages: list | None = None, *, status: str = "pass") -> list[dict]:
    item = {"id": "visual.pages", "kind": "visual", "status": status,
            "scope": "全部 3 页（1280×720 实际渲染）", "note": "逐页渲染检查"}
    if pages is not None:
        item["pages"] = pages
    return item


def full_pages(*, checked: bool = True, issues: list | None = None) -> list[dict]:
    entries = []
    for n in (1, 2, 3):
        entry = {"page": n, "checked": checked, "viewport": "1280x720"}
        if issues:
            entry["issues"] = issues
        entries.append(entry)
    return entries


def all_checks(*, content: str = "pass", structure: str = "pass",
               visual: dict | None = None) -> list[dict]:
    items = [{"id": cid, "kind": "content", "status": content,
              "scope": "全部", "note": "合成契约数据"}
             for cid in CONTENT_CHECK_IDS]
    items += [{"id": cid, "kind": "structure", "status": structure,
               "scope": "全部页", "note": "合成契约数据"}
              for cid in STRUCTURE_CHECK_IDS]
    if visual is None:
        visual = visual_evidence(full_pages())
    items.append(visual)
    return items


def make_record(script_text: str = SCRIPT, **overrides) -> dict:
    record = {
        "schema": RECORD_SCHEMA,
        "generatedAt": "2026-11-02T09:00:00+08:00",
        "request": {"purpose": "季度经营汇报", "audience": "管理层",
                    "coreQuestions": ["三季度收入为什么增长"], "length": "3 页",
                    "script": "yes"},
        "sources": [{"path": "materials/q3-notes.md", "status": "read",
                     "coverage": "全文", "sha256": sha(MATERIAL), "note": ""}],
        "method": {"kind": "builtin", "name": "annotation-everything 内置范式",
                   "version": "0.9.0", "unavailable": []},
        "links": {"report": "report.html", "script": "script.md",
                  "sha256": {"report": sha(REPORT), "script": sha(script_text)},
                  "sourceNote": "materials/q3-notes.md 及同目录讲稿"},
        "findings": [{"claim": "三季度收入同比增长 12%", "type": "calculation",
                      "sources": ["materials/q3-notes.md"], "pages": ["sum"],
                      "refs": [{"path": "materials/q3-notes.md", "locator": "§1 收入",
                                "quote": "去年同期为 100 万元", "found": True}],
                      "support": "supported",
                      "basis": "对比材料 §1 两期收入原文，并按声明输入复算 percent-change",
                      "calculation": {"op": "percent-change", "inputs": {"a": 100, "b": 112},
                                      "expected": 12,
                                      "unit": "万元", "period": "2025Q3 对 2024Q3",
                                      "definition": "对上年同期收入的增长百分比",
                                      "inputLocations": {
                                          "a": "materials/q3-notes.md §1：去年同期 100 万元",
                                          "b": "materials/q3-notes.md §1：三季度收入 112 万元"}},
                      "note": "收入 112 对上年 100，四舍五入到整数"}],
        "gaps": ["十月的周报缺失"],
        "checks": all_checks(content="not_run", structure="not_run",
                             visual=visual_evidence(status="not_run")),
        "openItems": [{"item": "十月周报待补", "blocking": False}],
        "verdict": {"draftComplete": True, "deliverable": None},
    }
    record.update(overrides)
    return record


def compliant_record(**overrides) -> dict:
    record = make_record(checks=all_checks())
    record["verdict"] = {"draftComplete": True, "deliverable": True}
    record.update(overrides)
    return record


def revision(**overrides) -> dict:
    """改稿维护声明：默认声明重新核验了数字与跨页一致性，无待核验项。"""
    data = {"basis": "改动落在第 1 页收入数字，讲稿第 1 页同步；核对相关引用与跨页口径",
            "rechecked": ["content.numbers-charts", "content.cross-page"],
            "pending": []}
    data.update(overrides)
    return data


def write_case(d: Path, *, report: str = REPORT, script: str | None = SCRIPT,
               record: dict | None = None) -> dict:
    d.mkdir(parents=True, exist_ok=True)
    paths = {"report": d / "report.html"}
    paths["report"].write_text(report, encoding="utf-8")
    if script is not None:
        paths["script"] = d / "script.md"
        paths["script"].write_text(script, encoding="utf-8")
    if record is not None:
        paths["record"] = d / "report-record.json"
        paths["record"].write_text(json.dumps(record, ensure_ascii=False, indent=2) + "\n",
                                   encoding="utf-8")
    return paths


def run_check(paths: dict, *, locale: str | None = None,
              argv_extra: list[str] | None = None) -> tuple[int, dict | None, str, str]:
    argv = [str(paths["report"])]
    if paths.get("script"):
        argv += ["--script", str(paths["script"])]
    if paths.get("record"):
        argv += ["--record", str(paths["record"])]
    if locale:
        argv += ["--locale", locale]
    if argv_extra:
        argv += argv_extra
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = rc.main(argv)
    text = out.getvalue()
    payload = json.loads(text) if "--json" in (argv_extra or []) else None
    return code, payload, text, err.getvalue()


def check_by_id(payload: dict, cid: str) -> dict:
    for item in payload["checks"]:
        if item["id"] == cid:
            return item
    raise AssertionError(f"检查结果里没有 {cid}：{payload['checks']}")


class RevisionEntryTests(unittest.TestCase):
    """切片 1：record.revision 检查存在；没有 revision 的旧记录不受影响。"""

    def test_check_order_appends_revision_at_tail(self):
        self.assertEqual(rc.Checks.ORDER[-1], "record.revision")
        self.assertIn("record.revision", rc.Checks.ORDER)

    def test_legacy_record_without_revision_is_not_applicable(self):
        """#70–#73 形态的旧记录没有 revision 字段：判不适用，不产生失败。"""
        paths = write_case(tmpdir() / "legacy", record=compliant_record())
        code, payload, text, _ = run_check(paths, locale="zh-CN", argv_extra=["--json"])
        self.assertEqual(code, 0)
        item = check_by_id(payload, "record.revision")
        self.assertEqual(item["status"], "not_applicable")
        self.assertTrue(payload["verdict"]["deliverable"],
                        "旧记录的可交付判定不受新检查影响")

    def test_revision_declaration_passes(self):
        """改稿维护声明齐备：检查通过，不影响其他检查与可交付判定。"""
        record = compliant_record(revision=revision())
        paths = write_case(tmpdir() / "declared", record=record)
        code, payload, _, _ = run_check(paths, locale="zh-CN", argv_extra=["--json"])
        self.assertEqual(code, 0)
        item = check_by_id(payload, "record.revision")
        self.assertEqual(item["status"], "pass")
        self.assertTrue(payload["verdict"]["deliverable"])
        self.assertEqual(payload["record"]["revision"]["rechecked"],
                         ["content.numbers-charts", "content.cross-page"])

    def test_no_record_report_stays_reviewable(self):
        """没有记录的旧报告：初稿判定不因缺记录受阻，不被迫补建记录。"""
        paths = write_case(tmpdir() / "norecord", record=None)
        code, payload, text, _ = run_check(paths, locale="zh-CN", argv_extra=["--json"])
        self.assertEqual(code, 0)
        self.assertTrue(payload["verdict"]["draftComplete"])
        self.assertFalse(payload["verdict"]["deliverable"])
        item = check_by_id(payload, "record.revision")
        self.assertEqual(item["status"], "not_applicable")
        _, _, summary, _ = run_check(paths, locale="zh-CN")
        self.assertIn("初稿生成：已完成", summary)
        self.assertIn("报告可交付：否", summary)


if __name__ == "__main__":
    unittest.main()
