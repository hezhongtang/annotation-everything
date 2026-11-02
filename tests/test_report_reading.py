"""材料读取登记与缺口披露的契约测试（issue #71）。

report_check.py 的 `record.reading` 检查钉住核验记录里读取登记的确定规则：
`sources[].status` 只认 `read` / `partial` / `unreadable`；`partial` 必须写明
实际读取范围与原因，`unreadable` 必须写明原因；`locations` 存在时必须是字符
串列表（PDF 页码、Word 段落、工作表与单元格等定位线索）；全部实质材料不可
读时初稿不能宣布生成完成。读取范围、定位线索与缺口以结构化结果和可读摘要
披露——格式被接受不等于内容已读完。

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

# 一份由合成办公材料提取结果编成的报告：三个结论页各对应一种材料，
# .ae-source 写材料内定位（PDF 页码 / Word 段落 / 工作表单元格）。
REPORT = """<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><title>合成季度办公材料汇报</title>
<style>.page{width:1280px;height:720px;padding:56px;box-sizing:border-box;font-family:sans-serif}</style></head><body>
<section class="page" data-page="1" data-key="rev" id="page-rev"><h2>收入结论</h2>
<p>三季度收入 112 万元，同比 +12%。</p>
<p class="ae-source">q3-review.pdf 第 2 页</p></section>
<section class="page" data-page="2" data-key="plan" id="page-plan"><h2>渠道计划</h2>
<p>四季度新增两家门店。</p>
<p class="ae-source">sales-plan.docx 第 3 段</p></section>
<section class="page" data-page="3" data-key="tbl" id="page-tbl"><h2>渠道明细</h2>
<p>华东区 9 月销售 38 万元（空白单元格表示未统计，0 表示确认为零）。</p>
<p class="ae-source">channel-sales.xlsx Sheet1!B2:B9</p>
<div class="ae-gap">渠道表 10 月公式列未读取</div></section>
</body></html>
"""

SCRIPT = """# 讲稿：合成季度办公材料汇报

### 第 1 页｜收入结论
**讲法**：
收入同比 +12%。

### 第 2 页｜渠道计划
**讲法**：
四季度新增两家门店。

### 第 3 页｜渠道明细
**讲法**：
华东 9 月 38 万。
"""


def sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def make_reading_record(sources: list | None = None, gaps: list | None = None,
                        **overrides) -> dict:
    """一份结构完整、读取登记可替换的最小核验记录。"""
    if sources is None:
        sources = [
            {"path": "materials/q3-review.pdf", "status": "read",
             "coverage": "全部 4 页文本层", "locations": ["q3-review.pdf 第 1–4 页"], "note": ""},
            {"path": "materials/sales-plan.docx", "status": "read",
             "coverage": "正文全部 12 段", "locations": ["sales-plan.docx 第 1–12 段"], "note": ""},
            {"path": "materials/channel-sales.xlsx", "status": "partial",
             "coverage": "Sheet1!A1:C9 已读；Sheet2 公式区未读取",
             "locations": ["channel-sales.xlsx Sheet1!A1:C9"],
             "reason": "宿主无公式重算工具，Sheet2 公式无缓存结果", "note": ""},
        ]
    if gaps is None:
        gaps = ["channel-sales.xlsx Sheet2 公式区未读取"]
    record = {
        "schema": RECORD_SCHEMA,
        "generatedAt": "2026-11-02T09:00:00+08:00",
        "request": {"purpose": "季度经营汇报", "audience": "管理层",
                    "coreQuestions": ["三季度收入为什么增长"], "length": "3 页", "script": "yes"},
        "sources": sources,
        "method": {"kind": "builtin", "name": "annotation-everything 内置范式",
                   "version": "0.9.0", "unavailable": []},
        "links": {"report": "report.html", "script": "script.md",
                  "sha256": {"report": sha(REPORT), "script": sha(SCRIPT)},
                  "sourceNote": "materials/ 及同目录讲稿"},
        "findings": [{"claim": "三季度收入同比增长 12%", "type": "calculation",
                      "sources": ["materials/q3-review.pdf"],
                      "locations": ["q3-review.pdf 第 2 页"], "pages": ["rev"],
                      "calculation": {"op": "percent-change", "inputs": {"a": 100, "b": 112},
                                      "expected": 12},
                      "note": "收入 112 对上年 100"}],
        "gaps": gaps,
        "checks": [
            {"id": "content.requirements", "kind": "content", "status": "not_run",
             "scope": "全部需求", "note": ""},
            {"id": "visual.pages", "kind": "visual", "status": "not_run",
             "scope": "全部 3 页", "note": ""},
        ],
        "openItems": [{"item": "渠道表 10 月数据待补", "blocking": False}],
        "verdict": {"draftComplete": True, "deliverable": None},
    }
    record.update(overrides)
    return record


def write_case(d: Path, record: dict | None = None) -> dict:
    d.mkdir(parents=True, exist_ok=True)
    paths = {"report": d / "report.html", "script": d / "script.md"}
    paths["report"].write_text(REPORT, encoding="utf-8")
    paths["script"].write_text(SCRIPT, encoding="utf-8")
    if record is not None:
        paths["record"] = d / "report-record.json"
        paths["record"].write_text(json.dumps(record, ensure_ascii=False, indent=2) + "\n",
                                   encoding="utf-8")
    return paths


def run_check(paths: dict, *, locale: str | None = None) -> tuple[int, dict | None, str]:
    argv = [str(paths["report"]), "--script", str(paths["script"])]
    if paths.get("record"):
        argv += ["--record", str(paths["record"])]
    if locale:
        argv += ["--locale", locale]
    argv.append("--json")
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = rc.main(argv)
    payload = json.loads(out.getvalue())
    return code, payload, out.getvalue()


def check_by_id(payload: dict, cid: str) -> dict:
    for item in payload["checks"]:
        if item["id"] == cid:
            return item
    raise AssertionError(f"检查结果里没有 {cid}：{[c['id'] for c in payload['checks']]}")


class ReadingRegistrationTests(unittest.TestCase):
    """读取登记：状态枚举、披露完整性与定位线索类型。"""

    def test_mixed_statuses_pass(self):
        """read / partial 混合并如实披露：读取登记通过，初稿成立。"""
        d = Path(tempfile.mkdtemp())
        paths = write_case(d, make_reading_record())
        code, payload, _ = run_check(paths, locale="zh-CN")
        self.assertEqual(code, 0)
        self.assertEqual(check_by_id(payload, "record.reading")["status"], "pass")
        self.assertTrue(payload["verdict"]["draftComplete"])

    def test_reading_check_in_order(self):
        """record.reading 是程序检查的固定一项；没有记录时不适用。"""
        self.assertIn("record.reading", rc.Checks.ORDER)
        d = Path(tempfile.mkdtemp())
        paths = write_case(d, None)
        code, payload, _ = run_check(paths, locale="zh-CN")
        self.assertEqual(code, 0)
        self.assertEqual(check_by_id(payload, "record.reading")["status"], "not_applicable")

    def test_empty_sources_not_applicable(self):
        """材料只在对话里（sources 为空）时读取登记不适用，不算失败。"""
        d = Path(tempfile.mkdtemp())
        paths = write_case(d, make_reading_record(sources=[]))
        code, payload, _ = run_check(paths, locale="zh-CN")
        self.assertEqual(code, 0)
        self.assertEqual(check_by_id(payload, "record.reading")["status"], "not_applicable")

    def test_missing_status_fails(self):
        d = Path(tempfile.mkdtemp())
        sources = [{"path": "materials/q3-review.pdf", "coverage": "全部"}]
        paths = write_case(d, make_reading_record(sources=sources))
        code, payload, _ = run_check(paths, locale="zh-CN")
        self.assertEqual(code, 1)
        item = check_by_id(payload, "record.reading")
        self.assertEqual(item["status"], "fail")
        self.assertEqual(item["code"], "record.source-status")
        self.assertIn("q3-review.pdf", item["detail"])

    def test_unknown_status_fails(self):
        """格式被打开不代表已读：status 必须用既定枚举，不能自造「opened」。"""
        d = Path(tempfile.mkdtemp())
        sources = [{"path": "materials/q3-review.pdf", "status": "opened",
                    "coverage": "文件能打开"}]
        paths = write_case(d, make_reading_record(sources=sources))
        code, payload, _ = run_check(paths, locale="zh-CN")
        self.assertEqual(code, 1)
        item = check_by_id(payload, "record.reading")
        self.assertEqual(item["code"], "record.source-status")
        self.assertIn("opened", item["detail"])

    def test_partial_requires_range_and_reason(self):
        """partial 只写状态不写范围与原因：核验记录不能证明未读部分已知。"""
        d = Path(tempfile.mkdtemp())
        sources = [{"path": "materials/scan.pdf", "status": "partial"}]
        paths = write_case(d, make_reading_record(sources=sources))
        code, payload, _ = run_check(paths, locale="zh-CN")
        self.assertEqual(code, 1)
        item = check_by_id(payload, "record.reading")
        self.assertEqual(item["code"], "record.source-field")
        self.assertIn("scan.pdf", item["detail"])
        self.assertIn("coverage", item["detail"])
        self.assertIn("reason", item["detail"])

    def test_partial_reason_alone_not_enough(self):
        """partial 有原因但没写实际读取范围：仍算披露不完整。"""
        d = Path(tempfile.mkdtemp())
        sources = [{"path": "materials/scan.pdf", "status": "partial",
                    "reason": "第 3–4 页为扫描页"}]
        paths = write_case(d, make_reading_record(sources=sources))
        code, payload, _ = run_check(paths, locale="zh-CN")
        self.assertEqual(code, 1)
        self.assertEqual(check_by_id(payload, "record.reading")["code"], "record.source-field")

    def test_unreadable_requires_reason(self):
        d = Path(tempfile.mkdtemp())
        sources = [{"path": "materials/legacy.doc", "status": "unreadable"}]
        paths = write_case(d, make_reading_record(sources=sources))
        code, payload, _ = run_check(paths, locale="zh-CN")
        self.assertEqual(code, 1)
        item = check_by_id(payload, "record.reading")
        self.assertEqual(item["code"], "record.source-field")
        self.assertIn("reason", item["detail"])
        self.assertNotIn("coverage", item["detail"])   # unreadable 不要求范围

    def test_unreadable_full_disclosure_passes(self):
        """单份材料不可读但有原因与缺口：登记本身成立（初稿是否成立见切片 2）。"""
        d = Path(tempfile.mkdtemp())
        sources = [{"path": "materials/q3-review.pdf", "status": "read",
                    "coverage": "全部 4 页文本层"},
                   {"path": "materials/legacy.doc", "status": "unreadable",
                    "reason": "宿主无 .doc 旧格式解析工具"}]
        paths = write_case(d, make_reading_record(sources=sources,
                                                  gaps=["legacy.doc 无法读取"]))
        code, payload, _ = run_check(paths, locale="zh-CN")
        self.assertEqual(code, 0)
        self.assertEqual(check_by_id(payload, "record.reading")["status"], "pass")
        self.assertTrue(payload["verdict"]["draftComplete"])

    def test_locations_must_be_string_list(self):
        """定位线索存在时必须是字符串列表：页码、段落、单元格要能逐条核对。"""
        d = Path(tempfile.mkdtemp())
        sources = [{"path": "materials/q3-review.pdf", "status": "read",
                    "coverage": "全部", "locations": "第 1–4 页"}]
        paths = write_case(d, make_reading_record(sources=sources))
        code, payload, _ = run_check(paths, locale="zh-CN")
        self.assertEqual(code, 1)
        item = check_by_id(payload, "record.reading")
        self.assertEqual(item["code"], "record.source-field")
        self.assertIn("locations", item["detail"])

    def test_non_dict_entry_fails(self):
        d = Path(tempfile.mkdtemp())
        paths = write_case(d, make_reading_record(sources=["materials/q3-review.pdf"]))
        code, payload, _ = run_check(paths, locale="zh-CN")
        self.assertEqual(code, 1)
        self.assertEqual(check_by_id(payload, "record.reading")["status"], "fail")


if __name__ == "__main__":
    unittest.main()
