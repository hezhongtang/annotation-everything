"""报告产物核验入口的表驱动契约测试（issue #70，#72 演进）。

公开入口是技能副本里的 `scripts/report_check.py`：接收报告、可选讲稿与可选核验记录，
输出结构化结果与可读摘要。这里钉住它的对外契约：检查项 id 与状态、核验记录
`ae-report-record/1` 的结构校验、损坏 / 未知版本 / 讲稿不对应 / 过期记录 / 计算
不一致 / 空检查集合的明确报错，以及「程序检查通过不自动形成可交付结论」的判定语义。
结构化字段（id、status、code、verdict 字段）语言无关；detail 与摘要按 locale 本地化。

#72 演进（全部走可选字段的追加式演进，schema 仍是 ae-report-record/1）：
- `findings[].refs` / `support` / `basis` 分别表达引用是否可定位、材料是否支持
  表述与检查依据；「引用存在但结论不受支持」的反例不能判为内容核验通过。
- `calculation` 增加口径四要素（unit / period / definition / inputLocations），
  声明不全标为待核实；复算不一致时定位到结论与报告页。
- `sources[].sha256` 关联材料内容；材料变化后旧核验结果标为 unverified。
- 内容核验清单补齐（含 content.script-consistency），可交付要求内容清单逐项
  通过；缺支持声明、口径不明的计算与未哈希的材料都会阻止 covered。

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

# #72 内容核验清单（决策 10）：八个内容项 + 视觉项；#73 增加结构清单与视觉逐页证据，
# 可交付要求内容、结构、视觉三类覆盖全部 covered。
CONTENT_CHECK_IDS = ("content.requirements", "content.claims", "content.fact-vs-inference",
                     "content.units-dates", "content.numbers-charts", "content.cross-page",
                     "content.script-consistency", "content.open-questions")
STRUCTURE_CHECK_IDS = ("structure.flow", "structure.headings", "structure.claim-support",
                       "structure.dup-gaps", "structure.length")
VISUAL_CHECK_IDS = ("visual.pages",)

# 合成材料原文：用于 sources[].sha256 的新鲜度用例。
MATERIAL = "# 三季度笔记\n\n三季度收入 112 万元，上年同期 100 万元。\n"


def tmpdir() -> Path:
    return Path(tempfile.mkdtemp())


def all_checks(status: str) -> list[dict]:
    """完整核验清单（内容 + 结构 + 视觉）的逐项声明；status 用于构造 pass / not_run 记录。

    #73 起视觉项声明 pass 必须带逐页证据（checks[].pages）；本 helper 用 status 统一
    构造时，视觉项在 pass 下携带覆盖全部报告页的证据。"""
    pages = [{"page": n, "checked": True, "issues": []} for n in (1, 2)]
    return [{"id": cid, "kind": "visual" if cid in VISUAL_CHECK_IDS else
             ("structure" if cid in STRUCTURE_CHECK_IDS else "content"),
             "status": status, "scope": "全部", "note": "合成契约数据",
             **({"pages": pages} if cid in VISUAL_CHECK_IDS else {})}
            for cid in CONTENT_CHECK_IDS + STRUCTURE_CHECK_IDS + VISUAL_CHECK_IDS]


REPORT = """<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><title>合成季度回顾</title>
<style>.page{width:1280px;height:720px;padding:56px;box-sizing:border-box;font-family:sans-serif}
.kpi{font-size:48px;font-weight:700}</style></head><body>
<section class="page" data-page="1" data-key="sum" id="page-sum"><h2>结论</h2><p class="kpi">收入同比 +12%</p>
<p>新开 3 家门店贡献主要增量。</p></section>
<section class="page" data-page="2" data-key="next" id="page-next"><h2>下一步</h2><p>两件事。</p></section>
</body></html>
"""

REPORT_EN = REPORT.replace("合成季度回顾", "Synthetic Quarterly Review").replace("收入同比 +12%", "Revenue +12% YoY")

SCRIPT = """# 讲稿：合成季度回顾

### 第 1 页｜结论
**讲法**：
先说结论：收入同比增长 12%。

### 第 2 页｜下一步
**讲法**：
两件事。
"""

SCRIPT_EN = """# Script: Synthetic Quarterly Review

### Page 1｜Conclusion
**Talking points**:
Conclusion first: revenue up 12% YoY.

### Page 2｜Next steps
**Talking points**:
Two things.
"""


def sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def make_record(report_text: str = REPORT, script_text: str | None = SCRIPT,
                report_name: str = "report.html", **overrides) -> dict:
    """一份结构完整的最小核验记录（#72 形态）；检查结果缺省全部 not_run。"""
    links = {
        "report": report_name,
        "script": "script.md" if script_text is not None else None,
        "sha256": {"report": sha(report_text),
                   "script": sha(script_text) if script_text is not None else None},
        "sourceNote": "materials/q3-notes.md 及同目录讲稿",
    }
    record = {
        "schema": RECORD_SCHEMA,
        "generatedAt": "2026-11-02T09:00:00+08:00",
        "request": {"purpose": "季度经营汇报", "audience": "管理层",
                    "coreQuestions": ["三季度收入为什么增长"], "length": "2 页", "script": "yes"},
        "sources": [{"path": "materials/q3-notes.md", "status": "read",
                     "coverage": "全文", "note": ""}],
        "method": {"kind": "builtin", "name": "annotation-everything 内置范式",
                   "version": "0.9.0", "unavailable": []},
        "links": links,
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
        "checks": all_checks("not_run"),
        "openItems": [{"item": "十月周报待补", "blocking": False}],
        "verdict": {"draftComplete": True, "deliverable": None},
    }
    record.update(overrides)
    return record


def compliant_record(**overrides) -> dict:
    """内容清单逐项通过、结论支持映射完备、计算口径齐全的可交付形态记录。"""
    record = make_record(checks=all_checks("pass"))
    record["sources"][0]["sha256"] = sha(MATERIAL)
    record["verdict"] = {"draftComplete": True, "deliverable": True}
    record.update(overrides)
    return record


def write_case(d: Path, *, report: str = REPORT, script: str | None = SCRIPT,
               record: dict | None = None, record_text: str | None = None,
               report_name: str = "report.html", material: bool = True) -> dict:
    """把合成产物写进临时目录；返回各文件路径。"""
    d.mkdir(parents=True, exist_ok=True)
    if material and not (d / "materials" / "q3-notes.md").exists():
        (d / "materials").mkdir(exist_ok=True)
        (d / "materials" / "q3-notes.md").write_text(MATERIAL, encoding="utf-8")
    paths = {"report": d / report_name}
    paths["report"].write_text(report, encoding="utf-8")
    if script is not None:
        paths["script"] = d / "script.md"
        paths["script"].write_text(script, encoding="utf-8")
    if record_text is not None:
        paths["record"] = d / "report-record.json"
        paths["record"].write_text(record_text, encoding="utf-8")
    elif record is not None:
        paths["record"] = d / "report-record.json"
        paths["record"].write_text(json.dumps(record, ensure_ascii=False, indent=2) + "\n",
                                   encoding="utf-8")
    return paths


def run_check(paths: dict, *, locale: str | None = None, argv_extra: list[str] | None = None
              ) -> tuple[int, dict | None, str, str]:
    """在进程内调用公开入口：返回 (退出码, JSON 结果或 None, 摘要 stdout, stderr)。"""
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
    payload = None
    if "--json" in (argv_extra or []):
        payload = json.loads(text)
    return code, payload, text, err.getvalue()


def check_by_id(payload: dict, cid: str) -> dict:
    for item in payload["checks"]:
        if item["id"] == cid:
            return item
    raise AssertionError(f"检查结果里没有 {cid}：{payload['checks']}")


class ValidInputsTests(unittest.TestCase):
    """有效输入：程序检查通过、初稿成立；内容与视觉核验未执行不能算通过。"""

    def test_valid_full_set(self):
        d = tmpdir()
        paths = write_case(d, record=make_record())
        code, payload, text, _ = run_check(paths, locale="zh-CN", argv_extra=["--json"])
        self.assertEqual(code, 0)
        self.assertEqual(payload["schema"], CHECK_SCHEMA)
        self.assertTrue(payload["verdict"]["draftComplete"])
        self.assertFalse(payload["verdict"]["deliverable"])
        self.assertEqual(payload["record"]["version"], RECORD_SCHEMA)
        self.assertTrue(payload["record"]["fresh"])
        self.assertEqual(payload["coverage"]["content"], "not_run")
        self.assertEqual(payload["coverage"]["visual"], "not_run")
        self.assertEqual(check_by_id(payload, "report.parse")["status"], "pass")
        self.assertEqual(check_by_id(payload, "report.pageKeys")["status"], "pass")
        self.assertEqual(check_by_id(payload, "script.pages")["status"], "pass")
        self.assertEqual(check_by_id(payload, "record.calculations")["status"], "pass")
        # 交付被挡下的原因要写明内容与视觉核验未执行
        self.assertTrue(any("视觉" in r or "visual" in r for r in payload["verdict"]["reasons"]),
                        payload["verdict"]["reasons"])
        # 未执行的检查不能出现在任何「通过」的展示里
        self.assertNotIn("content.requirements", [c["id"] for c in payload["checks"]])

    def test_summary_is_localized(self):
        d = tmpdir()
        paths = write_case(d, record=make_record())
        _, _, zh, _ = run_check(paths, locale="zh-CN")
        self.assertIn("初稿", zh)
        self.assertIn("核验", zh)
        _, _, en, _ = run_check(paths, locale="en")
        self.assertIn("Draft", en)
        self.assertIn("not", en.lower() or en)   # 英文摘要说明未执行项

    def test_report_only_no_record(self):
        d = tmpdir()
        paths = write_case(d, script=None)
        code, payload, text, _ = run_check(paths, locale="zh-CN", argv_extra=["--json"])
        self.assertEqual(code, 0)
        self.assertEqual(check_by_id(payload, "script.pages")["status"], "not_applicable")
        self.assertTrue(payload["verdict"]["draftComplete"])
        self.assertFalse(payload["verdict"]["deliverable"])
        self.assertIsNone(payload["record"]["version"])
        self.assertFalse(payload["record"]["fresh"])
        self.assertEqual(payload["coverage"]["content"], "not_run")
        # 没有记录时，交付结论必须说明内容与视觉核验没有执行依据
        self.assertTrue(any("记录" in r for r in payload["verdict"]["reasons"]),
                        payload["verdict"]["reasons"])

    def test_english_materials(self):
        d = tmpdir()
        paths = write_case(d, report=REPORT_EN, script=SCRIPT_EN,
                           record=make_record(report_text=REPORT_EN, script_text=SCRIPT_EN))
        code, payload, _, _ = run_check(paths, locale="en", argv_extra=["--json"])
        self.assertEqual(code, 0)
        self.assertTrue(payload["verdict"]["draftComplete"])


class ScriptCorrespondenceTests(unittest.TestCase):
    """讲稿对应：页码与报告页序一一对应；不对应时明确列出。"""

    def test_missing_script_page_fails(self):
        d = tmpdir()
        short = SCRIPT.split("### 第 2 页")[0]
        paths = write_case(d, script=short, record=make_record(script_text=short))
        code, payload, text, _ = run_check(paths, locale="zh-CN", argv_extra=["--json"])
        self.assertEqual(code, 1)
        item = check_by_id(payload, "script.pages")
        self.assertEqual(item["status"], "fail")
        self.assertEqual(item["code"], "script.page-mismatch")
        self.assertIn("2", item["detail"])
        self.assertFalse(payload["verdict"]["draftComplete"])

    def test_extra_script_page_fails(self):
        d = tmpdir()
        extra = SCRIPT + "\n### 第 3 页｜多出来的页\n**讲法**：\n不该有。\n"
        paths = write_case(d, script=extra, record=make_record(script_text=extra))
        code, payload, _, _ = run_check(paths, locale="zh-CN", argv_extra=["--json"])
        self.assertEqual(code, 1)
        self.assertEqual(check_by_id(payload, "script.pages")["status"], "fail")


class ScriptDeclarationConsistencyTests(unittest.TestCase):
    """记录声明讲稿哈希与核验调用输入的一致性（规格 #69 审查 P2-1）。

    记录在 links.sha256.script 里声明了讲稿哈希，就是声明了「讲稿」这份产物；
    核验调用漏传 --script 时，讲稿哈希不比对、逐页对应不查、讲稿关联检查不计入
    覆盖总数，其余全部通过后可交付判定就静默成立。语义决策：已声明产物的检查
    没有执行 → 拦可交付（不拦初稿），原因进 verdict.reasons，摘要给出显式警告；
    没有声明哈希、真无讲稿的既有适用性规则不变。讲稿关联两项声明了 pass 而无讲稿
    的既有程序失败规则（record.check-not-applicable）保持原样。
    """

    @staticmethod
    def declared_script_record(*, with_scoped: bool) -> dict:
        """声明讲稿哈希的可交付形态记录；with_scoped 控制 checks 是否声明讲稿关联两项。"""
        checks = [c for c in all_checks("pass")
                  if with_scoped or c["id"] not in rc.SCRIPT_SCOPED_CHECKS]
        return compliant_record(checks=checks)

    def test_declared_script_hash_without_script_blocks_deliverable(self):
        """核心静默场景：讲稿哈希已声明、讲稿关联两项未声明，其余全 pass——
        修复前其余 pass 后可交付静默成立；修复后拦可交付并显式警告。"""
        d = tmpdir()
        paths = write_case(d, script=None,
                           record=self.declared_script_record(with_scoped=False))
        code, payload, _, _ = run_check(paths, locale="zh-CN", argv_extra=["--json"])
        self.assertEqual(code, 0, "讲稿缺失拦可交付判定，不是程序检查失败")
        self.assertFalse(payload["verdict"]["deliverable"],
                         "声明了讲稿哈希而未收到讲稿：已声明产物的检查未执行")
        self.assertTrue(payload["verdict"]["draftComplete"], "拦可交付，不拦初稿")
        reasons = "；".join(payload["verdict"]["reasons"])
        self.assertIn("讲稿", reasons, payload["verdict"]["reasons"])
        self.assertTrue(any("讲稿" in w for w in payload["warnings"]),
                        payload["warnings"])
        _, _, summary, _ = run_check(paths, locale="zh-CN")
        self.assertIn("讲稿", summary, "可读摘要里要有显式警告")
        self.assertIn("报告可交付：否", summary)
        self.assertIn("初稿生成：已完成", summary)

    def test_declared_scoped_pass_still_fails_with_warning(self):
        """讲稿关联两项声明 pass 而无讲稿：既有程序失败规则不动，另给显式警告。"""
        d = tmpdir()
        paths = write_case(d, script=None,
                           record=self.declared_script_record(with_scoped=True))
        code, payload, _, _ = run_check(paths, locale="zh-CN", argv_extra=["--json"])
        self.assertEqual(code, 1)
        self.assertFalse(payload["verdict"]["deliverable"])
        self.assertEqual(check_by_id(payload, "record.checks")["code"],
                         "record.check-not-applicable")
        self.assertTrue(any("讲稿" in w for w in payload["warnings"]),
                        payload["warnings"])

    def test_declared_script_hash_warning_is_localized(self):
        """核心静默场景在英文下同样拦可交付并给出英文警告。"""
        d = tmpdir()
        paths = write_case(d, script=None,
                           record=self.declared_script_record(with_scoped=False))
        code, payload, _, _ = run_check(paths, locale="en", argv_extra=["--json"])
        self.assertEqual(code, 0)
        self.assertFalse(payload["verdict"]["deliverable"])
        self.assertTrue(any("script" in w.lower() for w in payload["warnings"]),
                        payload["warnings"])
        self.assertTrue(any("script" in r.lower() for r in payload["verdict"]["reasons"]),
                        payload["verdict"]["reasons"])

    def test_undeclared_no_script_keeps_applicability_rules(self):
        """真无讲稿（无哈希声明）：适用性规则不变，可交付判定照常成立。"""
        d = tmpdir()
        record = compliant_record(
            request={"purpose": "季度经营汇报", "audience": "管理层",
                     "coreQuestions": ["三季度收入为什么增长"], "length": "2 页",
                     "script": "no"},
            links={"report": "report.html", "script": None,
                   "sha256": {"report": sha(REPORT), "script": None},
                   "sourceNote": "materials/q3-notes.md"},
            checks=[c for c in all_checks("pass")
                    if c["id"] not in rc.SCRIPT_SCOPED_CHECKS])
        paths = write_case(d, script=None, record=record)
        code, payload, _, _ = run_check(paths, locale="zh-CN", argv_extra=["--json"])
        self.assertEqual(code, 0)
        self.assertEqual(check_by_id(payload, "script.pages")["status"], "not_applicable")
        self.assertEqual(payload["coverage"]["content"], "covered",
                         "讲稿关联项不计入覆盖总数的规则不变")
        self.assertTrue(payload["verdict"]["deliverable"],
                        "无声明、真无讲稿的可交付判定不受影响")
        self.assertFalse(any("讲稿" in w for w in payload["warnings"]),
                         "真无讲稿不产生讲稿声明警告")


class RecordStructureTests(unittest.TestCase):
    """损坏记录、未知版本与缺失字段的明确报错。"""

    def test_broken_record(self):
        d = tmpdir()
        paths = write_case(d, record_text="{ not json")
        code, payload, text, _ = run_check(paths, locale="zh-CN", argv_extra=["--json"])
        self.assertEqual(code, 1)
        item = check_by_id(payload, "record.structure")
        self.assertEqual(item["status"], "fail")
        self.assertEqual(item["code"], "record.broken")
        self.assertTrue(item["detail"].strip())

    def test_unknown_record_version(self):
        d = tmpdir()
        record = make_record()
        record["schema"] = "ae-report-record/2"
        paths = write_case(d, record=record)
        code, payload, _, _ = run_check(paths, locale="zh-CN", argv_extra=["--json"])
        self.assertEqual(code, 1)
        item = check_by_id(payload, "record.structure")
        self.assertEqual(item["code"], "record.version-unknown")
        self.assertIn("2", item["detail"])
        self.assertFalse(payload["verdict"]["deliverable"])

    def test_missing_required_field(self):
        d = tmpdir()
        record = make_record()
        del record["links"]
        paths = write_case(d, record=record)
        code, payload, _, _ = run_check(paths, locale="zh-CN", argv_extra=["--json"])
        self.assertEqual(code, 1)
        self.assertEqual(check_by_id(payload, "record.structure")["code"], "record.field-missing")
        self.assertIn("links", check_by_id(payload, "record.structure")["detail"])

    def test_references_missing_report_page(self):
        d = tmpdir()
        record = make_record()
        record["findings"][0]["pages"] = ["ghost"]
        paths = write_case(d, record=record)
        code, payload, _, _ = run_check(paths, locale="zh-CN", argv_extra=["--json"])
        self.assertEqual(code, 1)
        item = check_by_id(payload, "record.pageRefs")
        self.assertEqual(item["status"], "fail")
        self.assertEqual(item["code"], "record.page-missing")
        self.assertIn("ghost", item["detail"])
        self.assertFalse(payload["verdict"]["draftComplete"])


class FreshnessTests(unittest.TestCase):
    """证据新鲜度：记录对应旧产物时不沿用其结论。"""

    def test_stale_record(self):
        d = tmpdir()
        record = make_record()          # 哈希对应 REPORT
        paths = write_case(d, report=REPORT.replace("两件事", "三件事"), record=record)
        code, payload, _, _ = run_check(paths, locale="zh-CN", argv_extra=["--json"])
        self.assertEqual(code, 1)
        self.assertFalse(payload["record"]["fresh"])
        item = check_by_id(payload, "record.freshness")
        self.assertEqual(item["status"], "fail")
        self.assertEqual(item["code"], "record.stale")
        # 过期记录的检查结果不能为当前产物背书
        self.assertEqual(payload["coverage"]["content"], "unverified")
        self.assertEqual(payload["coverage"]["visual"], "unverified")
        self.assertFalse(payload["verdict"]["deliverable"])


class ClaimSupportTests(unittest.TestCase):
    """结论支持映射（#72）：引用可定位、材料支持表述与检查依据分开表达；
    「引用存在但结论不受支持」的反例不能被判为内容核验通过。"""

    def test_unsupported_claim_cannot_pass_content(self):
        d = tmpdir()
        record = compliant_record()
        # 反例：引用还在（refs 齐全且可定位），但材料不支持该表述
        record["findings"][0]["support"] = "unsupported"
        paths = write_case(d, record=record)
        code, payload, text, _ = run_check(paths, locale="zh-CN", argv_extra=["--json"])
        self.assertEqual(code, 1)
        item = check_by_id(payload, "record.claims")
        self.assertEqual(item["status"], "fail")
        self.assertEqual(item["code"], "record.claim-unsupported")
        self.assertIn("收入", item["detail"], "报错要定位到具体结论")
        self.assertEqual(payload["coverage"]["content"], "uncovered")
        self.assertFalse(payload["verdict"]["deliverable"])
        self.assertIn("引用存在不等于结论受支持", item["detail"])
        # 摘要与 JSON 同源：zh 摘要同样点名「材料不支持该表述」
        _, _, zh_text, _ = run_check(paths, locale="zh-CN")
        self.assertIn("材料不支持该表述", zh_text)

    def test_unlocatable_reference_fails_supported_claim(self):
        d = tmpdir()
        record = compliant_record()
        record["findings"][0]["refs"][0]["found"] = False
        paths = write_case(d, record=record)
        code, payload, _, _ = run_check(paths, locale="zh-CN", argv_extra=["--json"])
        self.assertEqual(code, 1)
        item = check_by_id(payload, "record.claims")
        self.assertEqual(item["code"], "record.claim-unlocatable")
        self.assertEqual(payload["coverage"]["content"], "uncovered")
        self.assertFalse(payload["verdict"]["deliverable"])

    def test_bad_support_value_fails(self):
        d = tmpdir()
        record = compliant_record()
        record["findings"][0]["support"] = "surely-true"
        paths = write_case(d, record=record)
        code, payload, _, _ = run_check(paths, locale="zh-CN", argv_extra=["--json"])
        self.assertEqual(code, 1)
        self.assertEqual(check_by_id(payload, "record.claims")["code"], "record.claim-bad")

    def test_malformed_refs_fail(self):
        d = tmpdir()
        record = compliant_record()
        record["findings"][0]["refs"] = [{"path": 3}]
        paths = write_case(d, record=record)
        code, payload, _, _ = run_check(paths, locale="zh-CN", argv_extra=["--json"])
        self.assertEqual(code, 1)
        self.assertEqual(check_by_id(payload, "record.claims")["code"], "record.claim-bad")

    def test_missing_support_caps_content_not_run(self):
        d = tmpdir()
        record = compliant_record()
        del record["findings"][0]["support"]
        paths = write_case(d, record=record)
        code, payload, text, _ = run_check(paths, locale="zh-CN", argv_extra=["--json"])
        self.assertEqual(code, 0, "缺支持声明是披露缺口，不是程序检查失败")
        self.assertEqual(payload["coverage"]["content"], "not_run")
        self.assertFalse(payload["verdict"]["deliverable"])
        self.assertTrue(payload["warnings"], "缺支持声明的结论要有待核实警示")
        self.assertIn("待核实", text)

    def test_unverified_support_caps_content_not_run(self):
        d = tmpdir()
        record = compliant_record()
        record["findings"][0]["support"] = "unverified"
        paths = write_case(d, record=record)
        code, payload, _, _ = run_check(paths, locale="zh-CN", argv_extra=["--json"])
        self.assertEqual(code, 0)
        self.assertEqual(payload["coverage"]["content"], "not_run")
        self.assertFalse(payload["verdict"]["deliverable"])

    def test_partial_support_caps_content_partial(self):
        d = tmpdir()
        record = compliant_record()
        record["findings"][0]["support"] = "partial"
        paths = write_case(d, record=record)
        code, payload, _, _ = run_check(paths, locale="zh-CN", argv_extra=["--json"])
        self.assertEqual(code, 0)
        self.assertEqual(payload["coverage"]["content"], "partial")
        self.assertFalse(payload["verdict"]["deliverable"])

    def test_unfound_reference_with_partial_support_caps_not_run(self):
        d = tmpdir()
        record = compliant_record()
        record["findings"][0]["support"] = "partial"
        record["findings"][0]["refs"][0]["found"] = False
        paths = write_case(d, record=record)
        _, payload, _, _ = run_check(paths, locale="zh-CN", argv_extra=["--json"])
        self.assertEqual(payload["coverage"]["content"], "not_run")

    def test_record_without_findings_cannot_cover_content(self):
        d = tmpdir()
        paths = write_case(d, record=compliant_record(findings=[]))
        code, payload, _, _ = run_check(paths, locale="zh-CN", argv_extra=["--json"])
        self.assertEqual(code, 0)
        self.assertEqual(payload["coverage"]["content"], "not_run")
        self.assertFalse(payload["verdict"]["deliverable"])


class ReviewBoundaryTests(unittest.TestCase):
    """公开核验入口不能因错误记录崩溃，或用无法核对的证据支持交付。"""

    def setUp(self):
        self.directory = tmpdir()
        material = self.directory / "materials" / "q3-notes.md"
        material.parent.mkdir()
        material.write_text(MATERIAL, encoding="utf-8")

    def check(self, record):
        paths = write_case(self.directory, record=record)
        return run_check(paths, locale="zh-CN", argv_extra=["--json"])

    def test_sum_uses_numeric_inputs_and_reports_mismatch(self):
        for expected, status in ((212, "pass"), (213, "fail")):
            with self.subTest(expected=expected):
                record = compliant_record()
                record["findings"][0]["calculation"].update(op="sum", expected=expected)
                code, payload, _, _ = self.check(record)
                self.assertEqual(code, int(status == "fail"))
                self.assertEqual(check_by_id(payload, "record.calculations")["status"], status)

    def test_malformed_nested_mappings_return_structured_error(self):
        for field, value in (("hash", None), ("hash", []), ("hash", "bad"),
                             ("source", []), ("source", {}), ("source", "materials/\0.md"),
                             ("citations", 1), ("check", []), ("unavailable", 1)):
            with self.subTest(field=field, value=value):
                record = compliant_record()
                if field == "hash":
                    record["links"]["sha256"] = value
                elif field == "source":
                    record["sources"][0]["path"] = value
                elif field == "check":
                    record["checks"][0]["id"] = value
                elif field == "unavailable":
                    record["method"]["unavailable"] = value
                else:
                    record["findings"][0]["sources"] = value
                code, payload, _, _ = self.check(record)
                self.assertEqual(code, 1)
                self.assertFalse(payload["verdict"]["deliverable"])

    def test_supported_claim_needs_refs_sources_and_checking_basis(self):
        for field in ("refs", "sources", "basis"):
            for value in (None, [], ""):
                with self.subTest(field=field, value=value):
                    record = compliant_record()
                    if value is None:
                        del record["findings"][0][field]
                    else:
                        record["findings"][0][field] = value
                    _, payload, _, _ = self.check(record)
                    self.assertFalse(payload["verdict"]["deliverable"])
                    self.assertNotEqual(payload["coverage"]["content"], "covered")

    def test_reference_must_match_its_registered_source(self):
        record = compliant_record()
        record["findings"][0]["refs"][0]["path"] = "materials/unregistered.md"
        code, payload, _, _ = self.check(record)
        self.assertEqual(code, 1)
        self.assertEqual(check_by_id(payload, "record.claims")["code"], "record.claim-unlocatable")
        self.assertFalse(payload["verdict"]["deliverable"])

    def test_lost_source_preserves_draft_but_invalidates_delivery(self):
        record = compliant_record()
        paths = write_case(self.directory, record=record)
        _, before, _, _ = run_check(paths, locale="zh-CN", argv_extra=["--json"])
        self.assertTrue(before["verdict"]["deliverable"])
        (self.directory / "materials" / "q3-notes.md").unlink()
        code, after, _, _ = run_check(paths, locale="zh-CN", argv_extra=["--json"])
        self.assertEqual(code, 0)
        self.assertTrue(after["verdict"]["draftComplete"])
        self.assertEqual(after["coverage"]["content"], "unverified")
        self.assertFalse(after["verdict"]["deliverable"])

    def test_supplied_script_needs_matching_hash(self):
        for absent in (True, False):
            with self.subTest(absent=absent):
                record = compliant_record()
                if absent:
                    del record["links"]["sha256"]["script"]
                else:
                    record["links"]["sha256"]["script"] = None
                _, payload, _, _ = self.check(record)
                self.assertTrue(payload["verdict"]["draftComplete"])
                self.assertFalse(payload["verdict"]["deliverable"])
                self.assertFalse(payload["record"]["fresh"])

    def test_calculations_reject_nonfinite_and_out_of_range_numbers(self):
        for field in ("input", "expected"):
            for value in (float("nan"), float("inf"), 10 ** 1000, True):
                with self.subTest(field=field, value=repr(value)[:20]):
                    record = compliant_record()
                    calculation = record["findings"][0]["calculation"]
                    if field == "input":
                        calculation["inputs"]["a"] = value
                    else:
                        calculation["expected"] = value
                    code, payload, _, _ = self.check(record)
                    self.assertEqual(code, 1)
                    self.assertEqual(check_by_id(payload, "record.calculations")["code"], "record.calc-bad")


class CalculationTests(unittest.TestCase):
    """明确声明输入与口径的可复算数值：复算一致通过，不一致明确报错。"""

    def test_matching_calculation_passes(self):
        d = tmpdir()
        paths = write_case(d, record=make_record())
        code, payload, _, _ = run_check(paths, locale="zh-CN", argv_extra=["--json"])
        self.assertEqual(code, 0)
        self.assertEqual(check_by_id(payload, "record.calculations")["status"], "pass")

    def test_mismatched_calculation_fails(self):
        d = tmpdir()
        record = make_record()
        record["findings"][0]["calculation"]["expected"] = 15
        paths = write_case(d, record=record)
        code, payload, _, _ = run_check(paths, locale="zh-CN", argv_extra=["--json"])
        self.assertEqual(code, 1)
        item = check_by_id(payload, "record.calculations")
        self.assertEqual(item["code"], "record.calc-mismatch")
        self.assertIn("15", item["detail"])
        self.assertFalse(payload["verdict"]["deliverable"])

    def test_unsupported_operator_fails(self):
        d = tmpdir()
        record = make_record()
        record["findings"][0]["calculation"] = {"op": "pow", "inputs": {"a": 2, "b": 10},
                                                "expected": 1024}
        paths = write_case(d, record=record)
        code, payload, _, _ = run_check(paths, locale="zh-CN", argv_extra=["--json"])
        self.assertEqual(code, 1)
        self.assertEqual(check_by_id(payload, "record.calculations")["code"],
                         "record.calc-unsupported-op")


class CalculationBasisTests(unittest.TestCase):
    """派生数值的口径四要素（#72）：单位、期间、口径、输入位置；声明不全的
    计算明确标为待核实并阻止内容核验 covered；复算不一致定位到结论与报告页。"""

    def test_complete_basis_passes_without_pending_warning(self):
        d = tmpdir()
        paths = write_case(d, record=compliant_record())
        code, payload, text, _ = run_check(paths, locale="zh-CN", argv_extra=["--json"])
        self.assertEqual(code, 0)
        self.assertEqual(check_by_id(payload, "record.calculations")["status"], "pass")
        self.assertNotIn("待核实", text)
        self.assertTrue(payload["verdict"]["deliverable"])

    def test_missing_unit_marks_pending(self):
        d = tmpdir()
        record = compliant_record()
        del record["findings"][0]["calculation"]["unit"]
        paths = write_case(d, record=record)
        code, payload, text, _ = run_check(paths, locale="zh-CN", argv_extra=["--json"])
        self.assertEqual(code, 0, "口径声明不全是待核实，不是程序检查失败")
        self.assertEqual(check_by_id(payload, "record.calculations")["status"], "pass")
        self.assertEqual(payload["coverage"]["content"], "partial")
        self.assertFalse(payload["verdict"]["deliverable"])
        self.assertIn("待核实", text)

    def test_missing_input_location_marks_pending(self):
        d = tmpdir()
        record = compliant_record()
        del record["findings"][0]["calculation"]["inputLocations"]["b"]
        paths = write_case(d, record=record)
        _, payload, text, _ = run_check(paths, locale="zh-CN", argv_extra=["--json"])
        self.assertEqual(payload["coverage"]["content"], "partial")
        self.assertFalse(payload["verdict"]["deliverable"])
        self.assertIn("待核实", text)

    def test_blank_definition_marks_pending(self):
        d = tmpdir()
        record = compliant_record()
        record["findings"][0]["calculation"]["definition"] = "   "
        paths = write_case(d, record=record)
        _, payload, _, _ = run_check(paths, locale="zh-CN", argv_extra=["--json"])
        self.assertEqual(payload["coverage"]["content"], "partial")
        self.assertFalse(payload["verdict"]["deliverable"])

    def test_pending_does_not_block_draft(self):
        d = tmpdir()
        record = compliant_record()
        del record["findings"][0]["calculation"]["period"]
        paths = write_case(d, record=record)
        _, payload, _, _ = run_check(paths, locale="zh-CN", argv_extra=["--json"])
        self.assertTrue(payload["verdict"]["draftComplete"],
                        "有明确缺口的初稿仍可进入审阅")

    def test_mismatch_locates_claim_and_page(self):
        d = tmpdir()
        record = compliant_record()
        record["findings"][0]["calculation"]["expected"] = 15
        paths = write_case(d, record=record)
        code, payload, _, _ = run_check(paths, locale="zh-CN", argv_extra=["--json"])
        self.assertEqual(code, 1)
        item = check_by_id(payload, "record.calculations")
        self.assertEqual(item["code"], "record.calc-mismatch")
        self.assertIn("15", item["detail"])
        self.assertIn("sum", item["detail"], "要定位到报告页")
        self.assertIn("收入", item["detail"], "要定位到具体结论")


class SourceAssociationTests(unittest.TestCase):
    """材料关联（#72）：结论引用的材料必须登记；sources[].sha256 与报告旁的
    材料内容对应，材料变化后旧结果标为 unverified；无法对应的明确提示。"""

    def test_citing_unregistered_source_fails(self):
        d = tmpdir()
        record = compliant_record()
        record["findings"][0]["sources"] = ["materials/other.md"]
        paths = write_case(d, record=record)
        code, payload, _, _ = run_check(paths, locale="zh-CN", argv_extra=["--json"])
        self.assertEqual(code, 1)
        item = check_by_id(payload, "record.sources")
        self.assertEqual(item["status"], "fail")
        self.assertEqual(item["code"], "record.source-missing")
        self.assertIn("materials/other.md", item["detail"])

    def test_stale_material_unverifies_content(self):
        d = tmpdir()
        (d / "materials").mkdir()
        (d / "materials" / "q3-notes.md").write_text(MATERIAL, encoding="utf-8")
        paths = write_case(d, record=compliant_record())
        code, payload, _, _ = run_check(paths, locale="zh-CN", argv_extra=["--json"])
        self.assertEqual(code, 0)
        self.assertEqual(check_by_id(payload, "record.sources")["status"], "pass")
        # 材料内容变化：旧核验结果不能为新材料背书
        (d / "materials" / "q3-notes.md").write_text(MATERIAL + "\n四季度：待补充\n",
                                                     encoding="utf-8")
        code, payload, _, _ = run_check(paths, locale="zh-CN", argv_extra=["--json"])
        self.assertEqual(code, 1)
        item = check_by_id(payload, "record.sources")
        self.assertEqual(item["code"], "record.source-stale")
        self.assertIn("materials/q3-notes.md", item["detail"])
        self.assertEqual(payload["coverage"]["content"], "unverified")
        self.assertFalse(payload["verdict"]["deliverable"])

    def test_matching_material_hash_passes(self):
        d = tmpdir()
        (d / "materials").mkdir()
        (d / "materials" / "q3-notes.md").write_text(MATERIAL, encoding="utf-8")
        paths = write_case(d, record=compliant_record())
        code, payload, _, _ = run_check(paths, locale="zh-CN", argv_extra=["--json"])
        self.assertEqual(code, 0)
        self.assertEqual(check_by_id(payload, "record.sources")["status"], "pass")
        self.assertTrue(payload["verdict"]["deliverable"])

    def test_unresolved_material_warns_only(self):
        d = tmpdir()
        # compliant_record 声明了 sha256，但报告旁没有该材料文件：无法对应要提示，
        # 不能静默通过，也不由本入口武断失败（材料不在检查边界内）
        paths = write_case(d, record=compliant_record(), material=False)
        code, payload, text, _ = run_check(paths, locale="zh-CN", argv_extra=["--json"])
        self.assertEqual(code, 0)
        self.assertEqual(check_by_id(payload, "record.sources")["status"], "pass")
        self.assertTrue(any("materials/q3-notes.md" in w for w in payload["warnings"]),
                        payload["warnings"])
        self.assertIn("无法核对", text)

    def test_missing_hash_caps_not_run(self):
        d = tmpdir()
        record = compliant_record()
        del record["sources"][0]["sha256"]
        paths = write_case(d, record=record)
        code, payload, text, _ = run_check(paths, locale="zh-CN", argv_extra=["--json"])
        self.assertEqual(code, 0)
        self.assertEqual(payload["coverage"]["content"], "not_run")
        self.assertFalse(payload["verdict"]["deliverable"])
        self.assertIn("sha256", text)

    def test_bad_source_status_fails(self):
        d = tmpdir()
        record = compliant_record()
        record["sources"][0]["status"] = "skimmed"
        paths = write_case(d, record=record)
        code, payload, _, _ = run_check(paths, locale="zh-CN", argv_extra=["--json"])
        self.assertEqual(code, 1)
        self.assertEqual(check_by_id(payload, "record.sources")["code"], "record.source-bad")


class RecordChecksTests(unittest.TestCase):
    """记录的检查集合：非空、id 可识别、内容与视觉两类都有声明。"""

    def test_empty_checks_fail(self):
        d = tmpdir()
        paths = write_case(d, record=make_record(checks=[]))
        code, payload, _, _ = run_check(paths, locale="zh-CN", argv_extra=["--json"])
        self.assertEqual(code, 1)
        item = check_by_id(payload, "record.checks")
        self.assertEqual(item["status"], "fail")
        self.assertEqual(item["code"], "record.checks-empty")

    def test_unknown_check_id_fails(self):
        d = tmpdir()
        record = make_record(checks=[
            {"id": "magic.auto-review", "kind": "content", "status": "pass", "scope": "", "note": ""},
            {"id": "visual.pages", "kind": "visual", "status": "not_run", "scope": "", "note": ""},
        ])
        paths = write_case(d, record=record)
        code, payload, _, _ = run_check(paths, locale="zh-CN", argv_extra=["--json"])
        self.assertEqual(code, 1)
        item = check_by_id(payload, "record.checks")
        self.assertEqual(item["code"], "record.check-unknown")
        self.assertIn("magic.auto-review", item["detail"])

    def test_missing_visual_declaration_fails(self):
        d = tmpdir()
        record = make_record(checks=[
            {"id": "content.requirements", "kind": "content", "status": "not_run",
             "scope": "", "note": ""},
        ])
        paths = write_case(d, record=record)
        code, payload, _, _ = run_check(paths, locale="zh-CN", argv_extra=["--json"])
        self.assertEqual(code, 1)
        self.assertEqual(check_by_id(payload, "record.checks")["code"], "record.checks-empty")

    def test_bad_check_status_fails(self):
        d = tmpdir()
        record = make_record(checks=[
            {"id": "content.requirements", "kind": "content", "status": "done",
             "scope": "", "note": ""},
            {"id": "visual.pages", "kind": "visual", "status": "not_run", "scope": "", "note": ""},
        ])
        paths = write_case(d, record=record)
        code, payload, _, _ = run_check(paths, locale="zh-CN", argv_extra=["--json"])
        self.assertEqual(code, 1)
        self.assertEqual(check_by_id(payload, "record.checks")["code"], "record.check-status")


class DeliverableVerdictTests(unittest.TestCase):
    """可交付结论：记录完整覆盖且声明可交付时才被复核支持；缺任何一环都不支持。"""

    @staticmethod
    def passed_record(**overrides) -> dict:
        record = compliant_record()
        record.update(overrides)
        return record

    def test_full_coverage_supports_declared_deliverable(self):
        d = tmpdir()
        paths = write_case(d, record=self.passed_record())
        code, payload, _, _ = run_check(paths, locale="zh-CN", argv_extra=["--json"])
        self.assertEqual(code, 0)
        self.assertEqual(payload["coverage"]["content"], "covered")
        self.assertEqual(payload["coverage"]["visual"], "covered")
        self.assertTrue(payload["verdict"]["deliverable"])

    def test_deliverable_claim_without_visual_not_supported(self):
        d = tmpdir()
        record = self.passed_record()
        for item in record["checks"]:
            if item["id"] == "visual.pages":
                item["status"] = "not_run"
        record["verdict"] = {"draftComplete": True, "deliverable": True}
        paths = write_case(d, record=record)
        code, payload, _, _ = run_check(paths, locale="zh-CN", argv_extra=["--json"])
        self.assertEqual(code, 0)
        self.assertEqual(payload["coverage"]["visual"], "not_run")
        self.assertFalse(payload["verdict"]["deliverable"])
        self.assertTrue(any("视觉" in r for r in payload["verdict"]["reasons"]),
                        payload["verdict"]["reasons"])

    def test_blocking_open_item_prevents_deliverable(self):
        d = tmpdir()
        record = self.passed_record()
        record["openItems"] = [{"item": "核心口径二选一待用户拍板", "blocking": True}]
        paths = write_case(d, record=record)
        code, payload, _, _ = run_check(paths, locale="zh-CN", argv_extra=["--json"])
        self.assertEqual(code, 0)
        self.assertFalse(payload["verdict"]["deliverable"])

    def test_failed_content_check_blocks(self):
        d = tmpdir()
        record = self.passed_record()
        for item in record["checks"]:
            if item["id"] == "content.requirements":
                item.update({"status": "fail", "scope": "核心问题 2",
                             "note": "报告未回应第二个核心问题"})
        record["verdict"] = {"draftComplete": True, "deliverable": False}
        paths = write_case(d, record=record)
        code, payload, _, _ = run_check(paths, locale="zh-CN", argv_extra=["--json"])
        self.assertEqual(code, 0)
        self.assertEqual(payload["coverage"]["content"], "uncovered")
        self.assertFalse(payload["verdict"]["deliverable"])
        self.assertFalse(payload["record"]["declaredDeliverable"])

    def test_record_not_run_shown_as_not_run(self):
        d = tmpdir()
        paths = write_case(d, record=make_record())
        _, payload, _, _ = run_check(paths, locale="en", argv_extra=["--json"])
        # 未执行就是未执行：coverage 不是 covered，摘要不能把 not_run 说成通过
        self.assertNotEqual(payload["coverage"]["content"], "covered")
        self.assertNotEqual(payload["coverage"]["visual"], "covered")


class ChecklistCoverageTests(unittest.TestCase):
    """内容核验清单（#72）：可交付要求内置清单逐项通过；只声明子集、或有失败项，
    都不算内容核验通过。清单 id 只追加（含 #72 新增的 script-consistency）。"""

    def test_partial_content_checklist_cannot_cover(self):
        d = tmpdir()
        record = compliant_record(checks=[
            {"id": "content.requirements", "kind": "content", "status": "pass",
             "scope": "全部需求", "note": ""},
            {"id": "visual.pages", "kind": "visual", "status": "pass",
             "scope": "全部 2 页", "note": "",
             "pages": [{"page": 1, "checked": True}, {"page": 2, "checked": True}]},
        ])
        paths = write_case(d, record=record)
        code, payload, _, _ = run_check(paths, locale="zh-CN", argv_extra=["--json"])
        self.assertEqual(code, 0)
        self.assertEqual(payload["coverage"]["content"], "partial")
        self.assertEqual(payload["coverageCounts"]["content"],
                         {"done": 1, "total": len(CONTENT_CHECK_IDS), "failed": 0})
        self.assertFalse(payload["verdict"]["deliverable"])

    def test_full_checklist_declared_pass_is_covered(self):
        d = tmpdir()
        paths = write_case(d, record=compliant_record())
        _, payload, _, _ = run_check(paths, locale="zh-CN", argv_extra=["--json"])
        self.assertEqual(payload["coverage"]["content"], "covered")
        self.assertEqual(payload["coverageCounts"]["content"],
                         {"done": len(CONTENT_CHECK_IDS), "total": len(CONTENT_CHECK_IDS),
                          "failed": 0})
        self.assertEqual(payload["coverageCounts"]["visual"], {"done": 1, "total": 1, "failed": 0})

    def test_script_consistency_check_is_known(self):
        self.assertIn("content.script-consistency", CONTENT_CHECK_IDS)
        d = tmpdir()
        record = compliant_record()
        self.assertIn("content.script-consistency", [c["id"] for c in record["checks"]])
        paths = write_case(d, record=record)
        _, payload, _, _ = run_check(paths, locale="zh-CN", argv_extra=["--json"])
        self.assertEqual(check_by_id(payload, "record.checks")["status"], "pass")

    def test_failed_check_shows_failed_count(self):
        d = tmpdir()
        record = compliant_record()
        for item in record["checks"]:
            if item["id"] == "content.units-dates":
                item.update({"status": "fail", "scope": "咖啡豆成本",
                             "note": "单位口径未在报告标注"})
        paths = write_case(d, record=record)
        _, payload, _, _ = run_check(paths, locale="zh-CN", argv_extra=["--json"])
        self.assertEqual(payload["coverage"]["content"], "uncovered")
        self.assertEqual(payload["coverageCounts"]["content"]["failed"], 1)
        self.assertFalse(payload["verdict"]["deliverable"])


class SummaryDisclosureTests(unittest.TestCase):
    """摘要披露（#72）：检查范围、结果、缺口及记录位置出自同一份结构化结果。"""

    def test_summary_discloses_gaps_open_items_and_record_path(self):
        d = tmpdir()
        record = compliant_record()
        record["gaps"] = ["十月的周报缺失", "渠道明细只有汇总"]
        record["openItems"] = [{"item": "提价幅度待定", "blocking": False}]
        paths = write_case(d, record=record)
        _, payload, _, _ = run_check(paths, locale="zh-CN", argv_extra=["--json"])
        self.assertEqual(payload["record"]["gaps"], ["十月的周报缺失", "渠道明细只有汇总"])
        self.assertEqual(payload["record"]["openItems"], {"n": 1, "blocking": 0})
        _, _, zh, _ = run_check(paths, locale="zh-CN")
        self.assertIn("记录披露的缺口", zh)
        self.assertIn("十月的周报缺失", zh)
        self.assertIn("记录待办", zh)
        self.assertIn("记录文件：report-record.json", zh)
        _, _, en, _ = run_check(paths, locale="en")
        self.assertIn("Gaps disclosed by the record", en)
        self.assertIn("Record file: report-record.json", en)
        self.assertIn("Open items in the record", en)

    def test_blocking_count_disclosed(self):
        d = tmpdir()
        record = compliant_record()
        record["openItems"] = [{"item": "核心口径二选一", "blocking": True},
                               {"item": "附录页码", "blocking": False}]
        paths = write_case(d, record=record)
        _, payload, _, _ = run_check(paths, locale="zh-CN", argv_extra=["--json"])
        self.assertEqual(payload["record"]["openItems"], {"n": 2, "blocking": 1})
        _, _, zh, _ = run_check(paths, locale="zh-CN")
        self.assertIn("阻塞 1 项", zh)


class LegacyRecordCompatTests(unittest.TestCase):
    """兼容（#69 决策 15）：#70 形态的旧记录仍可校验、初稿成立，只是到不了可交付。"""

    def test_legacy_record_valid_draft_complete_not_deliverable(self):
        d = tmpdir()
        legacy = {
            "schema": RECORD_SCHEMA,
            "generatedAt": "2026-11-02T09:00:00+08:00",
            "request": {"purpose": "季度经营汇报"},
            "sources": [{"path": "materials/q3-notes.md", "status": "read",
                         "coverage": "全文", "note": ""}],
            "method": {"kind": "builtin", "name": "annotation-everything 内置范式",
                       "version": "0.9.0", "unavailable": []},
            "links": {"report": "report.html", "script": "script.md",
                      "sha256": {"report": sha(REPORT), "script": sha(SCRIPT)},
                      "sourceNote": ""},
            "findings": [{"claim": "三季度收入同比增长 12%", "type": "calculation",
                          "sources": ["materials/q3-notes.md"], "pages": ["sum"],
                          "calculation": {"op": "percent-change", "inputs": {"a": 100, "b": 112},
                                          "expected": 12},
                          "note": ""}],
            "gaps": [],
            "checks": [{"id": "content.requirements", "kind": "content", "status": "not_run",
                        "scope": "", "note": ""},
                       {"id": "visual.pages", "kind": "visual", "status": "not_run",
                        "scope": "", "note": ""}],
            "openItems": [],
            "verdict": {"draftComplete": True, "deliverable": None},
        }
        paths = write_case(d, record=legacy)
        code, payload, _, _ = run_check(paths, locale="zh-CN", argv_extra=["--json"])
        self.assertEqual(code, 0, "旧形态记录不引入新的程序检查失败")
        self.assertEqual(check_by_id(payload, "record.structure")["status"], "pass")
        self.assertTrue(payload["verdict"]["draftComplete"])
        self.assertFalse(payload["verdict"]["deliverable"])
        self.assertEqual(payload["coverage"]["content"], "not_run")
        self.assertEqual(check_by_id(payload, "record.checks")["status"], "pass")


class ReportFormatTests(unittest.TestCase):
    """报告本身的格式检查。"""

    def test_duplicate_page_numbers_fail(self):
        d = tmpdir()
        dup = REPORT.replace('data-page="2" data-key="next"', 'data-page="1" data-key="next"')
        paths = write_case(d, report=dup, script=None)
        code, payload, _, _ = run_check(paths, locale="zh-CN", argv_extra=["--json"])
        self.assertEqual(code, 1)
        self.assertEqual(check_by_id(payload, "report.parse")["status"], "fail")
        self.assertFalse(payload["verdict"]["draftComplete"])

    def test_parse_failure_detail_is_the_generator_diagnostic(self):
        """#97：报告解析失败时 detail 仍是生成器那条诊断的原文（捕获点只换成 BuildError）。"""
        d = tmpdir()
        dup = REPORT.replace('data-page="2" data-key="next"', 'data-page="1" data-key="next"')
        paths = write_case(d, report=dup, script=None)
        code, payload, _, _ = run_check(paths, locale="zh-CN", argv_extra=["--json"])
        self.assertEqual(code, 1)
        self.assertEqual(check_by_id(payload, "report.parse")["detail"], "报告结构不对：页码 1 重复")

    def test_derived_page_key_warns_but_draft_stands(self):
        d = tmpdir()
        derived = REPORT.replace('data-key="sum" id="page-sum"', 'id="page-sum"') \
                        .replace('data-key="next" id="page-next"', 'id="page-next"')
        paths = write_case(d, report=derived, script=None)
        code, payload, _, _ = run_check(paths, locale="zh-CN", argv_extra=["--json"])
        self.assertEqual(code, 0)
        self.assertTrue(payload["warnings"])
        self.assertTrue(payload["verdict"]["draftComplete"])
        # 由页码生成的键参与不了重排校正：不阻塞初稿，但写进交付阻塞之外的说明
        self.assertFalse(payload["verdict"]["deliverable"])

    def test_no_pages_at_all_fails(self):
        d = tmpdir()
        paths = write_case(d, report="<html><body><p>不是逐页报告</p></body></html>", script=None)
        code, payload, _, _ = run_check(paths, locale="zh-CN", argv_extra=["--json"])
        self.assertEqual(code, 1)
        self.assertEqual(check_by_id(payload, "report.parse")["status"], "fail")
        self.assertFalse(payload["verdict"]["draftComplete"])


class CliContractTests(unittest.TestCase):
    """命令行契约：JSON schema、退出码、语言无关字段、摘要本地化。"""

    def test_missing_report_exit_2(self):
        d = tmpdir()
        code, _, _, err = run_check({"report": d / "nope.html"}, locale="zh-CN")
        self.assertEqual(code, 2)
        self.assertTrue(err.strip())

    def test_bad_locale_exit_2(self):
        d = tmpdir()
        paths = write_case(d)
        code, _, _, err = run_check(paths, locale="fr")
        self.assertEqual(code, 2)

    def test_structured_fields_language_invariant(self):
        d = tmpdir()
        paths = write_case(d, record=make_record())
        _, zh, _, _ = run_check(paths, locale="zh-CN", argv_extra=["--json"])
        _, en, _, _ = run_check(paths, locale="en", argv_extra=["--json"])
        self.assertEqual([c["id"] for c in zh["checks"]], [c["id"] for c in en["checks"]])
        self.assertEqual([c["status"] for c in zh["checks"]], [c["status"] for c in en["checks"]])
        self.assertEqual(zh["coverage"], en["coverage"])
        self.assertEqual(zh["coverageCounts"], en["coverageCounts"])
        self.assertEqual(zh["coverageCapped"], en["coverageCapped"])
        self.assertEqual(zh["record"]["gaps"], en["record"]["gaps"])
        self.assertEqual(zh["record"]["openItems"], en["record"]["openItems"])
        self.assertEqual(zh["verdict"]["draftComplete"], en["verdict"]["draftComplete"])
        self.assertEqual(zh["verdict"]["deliverable"], en["verdict"]["deliverable"])
        # reason 的具体条数可以随语言一致：两条路径都有理由且非空
        self.assertTrue(zh["verdict"]["reasons"] and en["verdict"]["reasons"])

    def test_summary_mentions_record_and_next_step(self):
        d = tmpdir()
        paths = write_case(d, record=make_record())
        _, _, zh, _ = run_check(paths, locale="zh-CN")
        self.assertIn("初稿", zh)
        self.assertIn("报告可交付", zh)      # 明确区分两个判定
        self.assertIn("未执行", zh)


if __name__ == "__main__":
    unittest.main()
