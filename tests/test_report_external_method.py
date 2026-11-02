"""外部生成技能共用验收的契约测试（issue #74，决策 14 与测试决策 4/5）。

四块内容：
- `record.method` 声明契约：实际生成方法与可得版本保留在核验记录；外部方法必须
  写实际调用依据（invocation）——扫描到候选或名字出现不算已采用；用户指定但未
  采用的技能与方法能力缺口如实披露。核验入口的 `record.method` 程序检查钉住
  这些确定规则，摘要与结构化结果如实披露方法信息。
- 内外产物判定一致性：相同缺陷（不受支持的结论、坏页面、未执行检查、缺依据）
  在 builtin 与 external 方法声明下得到相同的程序结果与交付判定——外部方法
  不放宽验收。
- 受控合成外部技能的端到端（离线部分）：fixtures/ 下的合成技能脚本实际运行
  生成产物，走同一核验入口与对照页生成；浏览器渲染由 e2e 场景
  `external-method-loop` 覆盖。实际 Agent 行为评测由 evals/generate-method-*
  定义并单独运行，不与确定性测试混淆。
- 接入约定随技能发布：中英生成指引写明方法选择路由与能力不足的处理，工作流
  与 SKILL.md 可达，发现路由用例覆盖中英两语言的指定技能说法。

只用标准库与合成数据：python3 -m unittest discover tests
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "skill" / "annotation-everything" / "scripts"
SKILL_SRC = ROOT / "skill" / "annotation-everything"
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
VISUAL_CHECK_IDS = ("visual.pages",)

MATERIAL = ("# 三季度经营笔记（合成）\n\n- 三季度收入 112 万元\n- 上年同期收入 100 万元\n"
            "- 外卖渠道占比 38%\n- 十月周报缺失，十月趋势暂无法核对\n")

REPORT = """<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><title>合成季度回顾</title>
<style>.page{width:1280px;height:720px;padding:56px;box-sizing:border-box;font-family:sans-serif}
.kpi{font-size:48px;font-weight:700}</style></head><body>
<section class="page" data-page="1" data-key="sum" id="page-sum"><h2>结论</h2><p class="kpi">收入同比 +12%</p>
<p>新开 3 家门店贡献主要增量。</p><p class="ae-source">materials/notes.md 第 2 行</p></section>
<section class="page" data-page="2" data-key="next" id="page-next"><h2>下一步</h2><p>两件事。</p></section>
</body></html>
"""

SCRIPT = """# 讲稿：合成季度回顾

### 第 1 页｜结论
**讲法**：
先说结论：收入同比增长 12%。

### 第 2 页｜下一步
**讲法**：
两件事。
"""

EXTERNAL_METHOD = {"kind": "external", "name": "synthetic-brief-kit", "version": "1.4.2",
                   "invocation": "运行技能脚本 make_report.py 生成 report.html 与记录",
                   "unavailable": [], "note": ""}
BUILTIN_METHOD = {"kind": "builtin", "name": "annotation-everything 内置范式",
                  "version": "0.9.0", "unavailable": []}


def tmpdir() -> Path:
    return Path(tempfile.mkdtemp())


def all_checks(status: str) -> list[dict]:
    pages = [{"page": n, "checked": True, "issues": []} for n in (1, 2)]
    return [{"id": cid, "kind": "visual" if cid in VISUAL_CHECK_IDS else
             ("structure" if cid in STRUCTURE_CHECK_IDS else "content"),
             "status": status, "scope": "全部", "note": "合成契约数据",
             **({"pages": pages} if cid in VISUAL_CHECK_IDS else {})}
            for cid in CONTENT_CHECK_IDS + STRUCTURE_CHECK_IDS + VISUAL_CHECK_IDS]


def sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def make_record(method: dict | None = None, **overrides) -> dict:
    links = {"report": "report.html", "script": "script.md",
             "sha256": {"report": sha(REPORT), "script": sha(SCRIPT)},
             "sourceNote": "materials/notes.md"}
    record = {
        "schema": RECORD_SCHEMA,
        "generatedAt": "2026-11-02T09:00:00+08:00",
        "request": {"purpose": "季度经营汇报", "audience": "管理层",
                    "coreQuestions": ["三季度收入为什么增长"], "length": "2 页", "script": "yes"},
        "sources": [{"path": "materials/notes.md", "status": "read",
                     "coverage": "全文", "sha256": sha(MATERIAL), "note": ""}],
        "method": dict(BUILTIN_METHOD) if method is None else method,
        "links": links,
        "findings": [{"claim": "三季度收入同比增长 12%", "type": "calculation",
                      "sources": ["materials/notes.md"], "pages": ["sum"],
                      "refs": [{"path": "materials/notes.md", "locator": "第 2 行",
                                "quote": "三季度收入 112 万元", "found": True}],
                      "support": "supported",
                      "basis": "对比材料两期收入原文，并按声明输入复算 percent-change",
                      "calculation": {"op": "percent-change", "inputs": {"a": 100, "b": 112},
                                      "expected": 12, "unit": "万元",
                                      "period": "2025Q3 对 2024Q3",
                                      "definition": "对上年同期收入的增长百分比",
                                      "inputLocations": {
                                          "a": "materials/notes.md 第 3 行：上年同期收入 100 万元",
                                          "b": "materials/notes.md 第 2 行：三季度收入 112 万元"}},
                      "note": "收入 112 对上年 100"}],
        "gaps": ["十月周报缺失"],
        "checks": all_checks("not_run"),
        "openItems": [],
        "verdict": {"draftComplete": True, "deliverable": None},
    }
    record.update(overrides)
    return record


def compliant_record(method: dict | None = None, **overrides) -> dict:
    record = make_record(method=method, checks=all_checks("pass"))
    record["verdict"] = {"draftComplete": True, "deliverable": True}
    record.update(overrides)
    return record


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
    import io
    from contextlib import redirect_stdout, redirect_stderr
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


def outline(payload: dict) -> dict:
    """与语言无关的判定骨架：检查项 (id, status, code)、覆盖与交付判定。"""
    return {"checks": [(c["id"], c["status"], c["code"]) for c in payload["checks"]],
            "coverage": payload["coverage"],
            "coverageCapped": payload.get("coverageCapped"),
            "draftComplete": payload["verdict"]["draftComplete"],
            "deliverable": payload["verdict"]["deliverable"]}


# ── 切片 A：record.method 声明契约 ──────────────────────────────────────────

class MethodDeclarationTests(unittest.TestCase):
    """实际生成方法与可得版本保留在记录里；外部方法必须有实际调用依据；
    指定未采用与能力缺口如实披露。"""

    def test_builtin_declaration_passes(self):
        d = tmpdir()
        paths = write_case(d, record=make_record())
        code, payload, _, _ = run_check(paths, locale="zh-CN", argv_extra=["--json"])
        self.assertEqual(code, 0)
        self.assertEqual(check_by_id(payload, "record.method")["status"], "pass")
        self.assertTrue(payload["verdict"]["draftComplete"])

    def test_external_declaration_with_invocation_passes(self):
        d = tmpdir()
        paths = write_case(d, record=make_record(method=EXTERNAL_METHOD))
        code, payload, _, _ = run_check(paths, locale="zh-CN", argv_extra=["--json"])
        self.assertEqual(code, 0)
        self.assertEqual(check_by_id(payload, "record.method")["status"], "pass")

    def test_missing_kind_fails(self):
        d = tmpdir()
        paths = write_case(d, record=make_record(method={"name": "某技能"}))
        code, payload, _, _ = run_check(paths, locale="zh-CN", argv_extra=["--json"])
        self.assertEqual(code, 1)
        item = check_by_id(payload, "record.method")
        self.assertEqual(item["status"], "fail")
        self.assertEqual(item["code"], "record.method-unknown")
        self.assertFalse(payload["verdict"]["deliverable"])
        self.assertTrue(payload["verdict"]["draftComplete"], "方法声明缺失不挡初稿进审阅")

    def test_unknown_kind_fails(self):
        d = tmpdir()
        paths = write_case(d, record=make_record(
            method={"kind": "remote-magic", "name": "某技能"}))
        code, payload, _, _ = run_check(paths, locale="zh-CN", argv_extra=["--json"])
        self.assertEqual(code, 1)
        self.assertEqual(check_by_id(payload, "record.method")["code"], "record.method-unknown")

    def test_external_without_name_fails(self):
        d = tmpdir()
        paths = write_case(d, record=make_record(
            method={"kind": "external", "invocation": "运行了脚本"}))
        code, payload, _, _ = run_check(paths, locale="zh-CN", argv_extra=["--json"])
        self.assertEqual(code, 1)
        self.assertEqual(check_by_id(payload, "record.method")["code"], "record.method-missing")

    def test_external_without_invocation_fails(self):
        """不能只凭扫描目录或名称宣称已调用：外部方法缺实际调用说明就是程序失败。"""
        method = dict(EXTERNAL_METHOD)
        del method["invocation"]
        d = tmpdir()
        paths = write_case(d, record=make_record(method=method))
        code, payload, _, _ = run_check(paths, locale="zh-CN", argv_extra=["--json"])
        self.assertEqual(code, 1)
        item = check_by_id(payload, "record.method")
        self.assertEqual(item["code"], "record.method-invocation")
        self.assertIn("synthetic-brief-kit", item["detail"])

    def test_bad_optional_field_types_fail(self):
        method = dict(EXTERNAL_METHOD, unavailable="没有", version=1.4,
                      requested=["a"], note=7)
        d = tmpdir()
        paths = write_case(d, record=make_record(method=method))
        code, payload, _, _ = run_check(paths, locale="zh-CN", argv_extra=["--json"])
        self.assertEqual(code, 1)
        item = check_by_id(payload, "record.method")
        self.assertEqual(item["code"], "record.method-bad")
        for field in ("unavailable", "version", "requested", "note"):
            self.assertIn(field, item["detail"])

    def test_structured_result_and_summary_disclose_method(self):
        record = make_record(method=dict(EXTERNAL_METHOD, requested="fancy-deck-maker",
                                         note="指定的技能未安装",
                                         unavailable=["无导出能力"]))
        d = tmpdir()
        paths = write_case(d, record=record)
        _, payload, _, _ = run_check(paths, locale="zh-CN", argv_extra=["--json"])
        method = payload["record"]["method"]
        self.assertEqual(method["kind"], "external")
        self.assertEqual(method["name"], "synthetic-brief-kit")
        self.assertEqual(method["version"], "1.4.2")
        self.assertEqual(method["requested"], "fancy-deck-maker")
        self.assertEqual(method["unavailable"], ["无导出能力"])
        _, _, zh, _ = run_check(paths, locale="zh-CN")
        self.assertIn("synthetic-brief-kit", zh)
        self.assertIn("fancy-deck-maker", zh)
        self.assertIn("未被实际采用", zh)
        self.assertIn("无导出能力", zh)
        _, _, en, _ = run_check(paths, locale="en")
        self.assertIn("synthetic-brief-kit", en)
        self.assertIn("fancy-deck-maker", en)
        self.assertIn("not the one actually used", en)

    def test_external_without_version_warns(self):
        """版本信息可得时应写明；缺失给警告而不是失败。"""
        method = dict(EXTERNAL_METHOD)
        del method["version"]
        d = tmpdir()
        paths = write_case(d, record=make_record(method=method))
        _, payload, _, _ = run_check(paths, locale="zh-CN", argv_extra=["--json"])
        self.assertEqual(check_by_id(payload, "record.method")["status"], "pass")
        self.assertTrue(any("method.version" in w for w in payload["warnings"]),
                        payload["warnings"])

    def test_no_record_has_no_method_check(self):
        d = tmpdir()
        paths = write_case(d, script=None, record=None)
        code, payload, _, _ = run_check(paths, locale="zh-CN", argv_extra=["--json"])
        self.assertEqual(code, 0)
        self.assertEqual(check_by_id(payload, "record.method")["status"], "not_applicable")


# ── 切片 B：内外产物判定一致性 ──────────────────────────────────────────────

class BuiltinExternalParityTests(unittest.TestCase):
    """相同缺陷在 builtin 与 external 方法声明下判定一致：不受支持的结论、
    坏页面、未执行检查与缺依据都不能因方法不同而通过。"""

    def test_same_defect_same_verdict_for_both_methods(self):
        """对每种相同缺陷，交换 method 声明不改变程序结果与交付判定。"""
        defect_fixes = {
            "unsupported-claim": lambda r: r["findings"][0].update(
                {"support": "unsupported",
                 "basis": "材料只说华东增长，报告写成全国增长"}),
            "unlocatable-ref": lambda r: r["findings"][0]["refs"][0].update({"found": False}),
            "visual-issue-declared-pass": lambda r: [
                item.update({"pages": [{"page": 1, "checked": True, "issues": ["第 1 页溢出"]},
                                       {"page": 2, "checked": True, "issues": []}]})
                for item in r["checks"] if item["id"] == "visual.pages"],
            "checks-not-run": lambda r: r.update(checks=all_checks("not_run"),
                                                 verdict={"draftComplete": True,
                                                          "deliverable": True}),
            "missing-basis": lambda r: (r["findings"][0].pop("refs"),
                                        r["findings"][0].pop("support"),
                                        r["findings"][0].pop("basis"),
                                        r["findings"][0].pop("calculation"),
                                        r["sources"][0].pop("sha256")),
            "calc-mismatch": lambda r: r["findings"][0]["calculation"].update({"expected": 99}),
        }
        for name, fix in defect_fixes.items():
            with self.subTest(defect=name):
                twins = {}
                for label, method in (("builtin", BUILTIN_METHOD), ("external", EXTERNAL_METHOD)):
                    record = make_record(method=dict(method))
                    fix(record)
                    d = tmpdir()
                    paths = write_case(d, record=record)
                    code, payload, _, _ = run_check(paths, locale="zh-CN", argv_extra=["--json"])
                    twins[label] = (code, outline(payload))
                self.assertEqual(twins["builtin"], twins["external"],
                                 f"缺陷 {name} 在内外方法下的判定不一致")

    def test_compliant_external_record_reaches_deliverable(self):
        """外部产物达到同一验收时同样可交付：验收相同，不是只对外部更严。"""
        d = tmpdir()
        paths = write_case(d, record=compliant_record(method=EXTERNAL_METHOD))
        code, payload, _, _ = run_check(paths, locale="zh-CN", argv_extra=["--json"])
        self.assertEqual(code, 0)
        self.assertTrue(payload["verdict"]["deliverable"])
        self.assertEqual(payload["coverage"]["content"], "covered")


# ── 切片 C：受控合成外部技能端到端（离线部分） ──────────────────────────────

def installed_ae_copy(tmp: Path) -> tuple[Path, dict]:
    """ae.py install 装进合成 HOME 的临时副本，返回 (scripts 目录, 环境变量)。"""
    home = tmp / "home"
    skills = home / ".claude" / "skills"
    env = {**os.environ, "HOME": str(home), "CLAUDE_SKILLS_DIR": str(skills)}
    done = subprocess.run([sys.executable, str(ROOT / "ae.py"), "install"], env=env,
                          capture_output=True, text=True, timeout=120)
    assert done.returncode == 0, done.stdout + done.stderr
    installed = skills / "annotation-everything"
    return installed / "scripts", {**os.environ, "HOME": str(home),
                                   "CLAUDE_SKILLS_DIR": str(skills),
                                   "AE_HOME": str(tmp / "ae-home")}


class SyntheticExternalSkillTests(unittest.TestCase):
    """fixtures/ 的合成外部技能实际运行 → 产物走同一核验入口与对照页生成。

    浏览器渲染与批注流程由 e2e 场景 external-method-loop 覆盖；这里验证离线
    部分全链路（选定方法 → 生成报告及依据 → 相同核验 → 对照页可生成）。"""

    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp(prefix="ae-ext-skill-"))
        cls.scripts, cls.env = installed_ae_copy(cls.tmp)

    def run_skill(self, fixture: str, out: Path) -> subprocess.CompletedProcess:
        skill_dir = self.tmp / "installed-skills" / fixture
        shutil.copytree(ROOT / "fixtures" / fixture, skill_dir, dirs_exist_ok=True)
        materials = out / "materials" / "notes.md"
        materials.parent.mkdir(parents=True, exist_ok=True)
        materials.write_text(MATERIAL, encoding="utf-8")
        return subprocess.run(
            [sys.executable, str(skill_dir / "scripts" / "make_report.py"),
             "--materials", str(materials), "--out", str(out)],
            env=self.env, capture_output=True, text=True, timeout=60)

    def test_specified_external_skill_generates_and_passes_same_entry(self):
        """明确指定且可用的外部技能被实际调用：产物走同一核验入口。"""
        out = self.tmp / "case-good"
        done = self.run_skill("external-report-skill", out)
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        # 实际调用的可观察效果：stdout 报告了产物，版式标记与来源标注出现在报告里
        self.assertIn("xs-banner", done.stdout)
        report = (out / "report.html").read_text(encoding="utf-8")
        self.assertIn("xs-banner", report, "外部技能自己的版式标记出现在产物里")
        self.assertIn("ae-source", report, "外部产物保留依据标注语义类")
        # 记录声明外部方法 + 实际调用依据 + 版本
        record = json.loads((out / "report-record.json").read_text(encoding="utf-8"))
        self.assertEqual(record["method"]["kind"], "external")
        self.assertEqual(record["method"]["name"], "synthetic-brief-kit")
        self.assertTrue(record["method"].get("invocation", "").strip())
        # 同一核验入口：同一记录契约、同一判定语义
        paths = {"report": out / "report.html", "script": out / "script.md",
                 "record": out / "report-record.json"}
        code, payload, _, _ = run_check(paths, locale="zh-CN", argv_extra=["--json"])
        self.assertEqual(code, 0, json.dumps(payload["checks"], ensure_ascii=False))
        self.assertTrue(payload["verdict"]["draftComplete"])
        self.assertFalse(payload["verdict"]["deliverable"], "内容与视觉核验未执行不支持可交付")
        self.assertEqual(payload["record"]["method"]["name"], "synthetic-brief-kit")
        # 对照页照常生成（浏览器渲染在 e2e 场景验证）
        review = out / "brief-review"
        built = subprocess.run(
            [sys.executable, str(self.scripts / "build_viewer.py"),
             "--report", str(paths["report"]), "--script", str(paths["script"]),
             "--out", str(review), "--name", "外部技能简报", "--no-scan-skills"],
            env=self.env, capture_output=True, text=True, timeout=60)
        self.assertEqual(built.returncode, 0, built.stdout + built.stderr)
        review_json = json.loads((review / "review.json").read_text(encoding="utf-8"))
        self.assertEqual([{"n": p["n"], "key": p["key"]} for p in review_json["pages"]],
                         [{"n": 1, "key": "sum"}, {"n": 2, "key": "next"}])
        self.assertTrue((review / "index.html").is_file())

    def test_limited_external_product_discloses_gaps_not_deliverable(self):
        """外部产物缺依据：缺口如实列出、覆盖不显示为通过、不可交付。"""
        out = self.tmp / "case-lite"
        done = self.run_skill("external-report-skill-lite", out)
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        paths = {"report": out / "report.html", "script": out / "script.md",
                 "record": out / "report-record.json"}
        code, payload, _, _ = run_check(paths, locale="zh-CN", argv_extra=["--json"])
        self.assertEqual(code, 0, "缺依据是缺口披露，不是程序失败")
        self.assertFalse(payload["verdict"]["deliverable"])
        self.assertNotEqual(payload["coverage"]["content"], "covered")
        self.assertIn("结论依据需另行核对", payload["gaps"][0])
        self.assertTrue(any("sha256" in w or "哈希" in w for w in payload["warnings"]),
                        f"缺哈希要有警告：{payload['warnings']}")

    def test_parity_twin_of_lite_record_is_identical(self):
        """lite 记录换成 builtin 方法声明（其余字段相同）判定完全一致。"""
        out = self.tmp / "case-lite"
        self.run_skill("external-report-skill-lite", out)
        record = json.loads((out / "report-record.json").read_text(encoding="utf-8"))
        _, external_payload, _, _ = run_check(
            {"report": out / "report.html", "script": out / "script.md",
             "record": out / "report-record.json"},
            locale="zh-CN", argv_extra=["--json"])
        twin = dict(record)
        twin["method"] = dict(BUILTIN_METHOD)
        d = tmpdir()
        shutil.copy(out / "report.html", d / "report.html")
        shutil.copy(out / "script.md", d / "script.md")
        paths = {"report": d / "report.html", "script": d / "script.md",
                 "record": d / "report-record.json"}
        paths["record"].write_text(json.dumps(twin, ensure_ascii=False, indent=2) + "\n",
                                   encoding="utf-8")
        _, builtin_payload, _, _ = run_check(paths, locale="zh-CN", argv_extra=["--json"])
        self.assertEqual(outline(builtin_payload), outline(external_payload),
                         "相同缺陷的内外产物判定必须一致")


# ── 切片 D：接入约定随技能发布（中英资源契约） ──────────────────────────────

_installed: list[Path] = []


def installed_copy() -> Path:
    if _installed:
        return _installed[0]
    scripts, _ = installed_ae_copy(Path(tempfile.mkdtemp(prefix="ae-ext-res-")))
    _installed.append(scripts.parent)
    return _installed[0]


class ExternalMethodResourceTests(unittest.TestCase):
    """外部接入约定和能力不足时的处理说明随技能发布，中英同步、入口可达。"""

    @classmethod
    def setUpClass(cls):
        cls.copy = installed_copy()
        cls.zh_repo = (SKILL_SRC / "references" / "generation.zh-CN.md").read_text(encoding="utf-8")
        cls.en_repo = (SKILL_SRC / "references" / "generation.en.md").read_text(encoding="utf-8")
        cls.zh_copy = (cls.copy / "references" / "generation.zh-CN.md").read_text(encoding="utf-8")
        cls.en_copy = (cls.copy / "references" / "generation.en.md").read_text(encoding="utf-8")

    ZH_ANCHORS = ("外部生成技能", "invocation", "requested", "unavailable",
                  "不自动搜索", "record.method")
    EN_ANCHORS = ("external generation skill", "invocation", "requested", "unavailable",
                  "Never automatically search for or install remote skills", "record.method")

    def test_guides_carry_external_method_section(self):
        for text, anchors, label in ((self.zh_repo, self.ZH_ANCHORS, "中文"),
                                     (self.en_repo, self.EN_ANCHORS, "英文")):
            for anchor in anchors:
                self.assertIn(anchor, text, f"{label}生成指引缺外部方法锚点 {anchor}")

    def test_installed_copy_matches_repo_guides(self):
        self.assertEqual(self.zh_copy, self.zh_repo, "安装副本的中文指引与仓库一致")
        self.assertEqual(self.en_copy, self.en_repo, "安装副本的英文指引与仓库一致")

    def test_method_field_semantics_cover_invocation_and_requested(self):
        """字段语义说明外部调用依据、指定未采用与能力缺口的写法。"""
        for text, label in ((self.zh_repo, "中文"), (self.en_repo, "英文")):
            bullet = re.search(r"[-*] `method`", text)
            self.assertIsNotNone(bullet, f"{label}指引有 method 字段语义")
            blob = text[bullet.start():bullet.start() + 1600]
            self.assertIn("invocation", blob, f"{label}指引说明 invocation")
            self.assertIn("requested", blob, f"{label}指引说明 requested")

    def test_workflow_routes_to_method_choice(self):
        """中英工作流的生成入口写明生成方法选择。"""
        for name, anchor in (("workflow.zh-CN.md", "外部生成技能"),
                             ("workflow.en.md", "external generation skill")):
            text = (SKILL_SRC / "references" / name).read_text(encoding="utf-8")
            self.assertIn("generation.", text, f"{name} 链到生成指引")
            self.assertIn(anchor, text, f"{name} 提及生成方法选择")

    def test_skill_entry_points_to_method_choice(self):
        front_and_body = (SKILL_SRC / "SKILL.md").read_text(encoding="utf-8")
        self.assertIn("外部", front_and_body)
        self.assertIn("external", front_and_body.lower())

    def test_discovery_cases_cover_external_skill_routing(self):
        cases = json.loads((SKILL_SRC / "evals" / "discovery-cases.json").read_text(encoding="utf-8"))
        actions = {(c.get("action"), c.get("workflow")) for c in cases["cases"]}
        self.assertIn(("generate-with-external-skill", "references/generation.zh-CN.md"),
                      actions, "中文指定外部技能的路由用例")
        self.assertIn(("generate-with-external-skill", "references/generation.en.md"),
                      actions, "英文指定外部技能的路由用例")

    def test_synthetic_skills_are_self_contained(self):
        """合成技能只依赖标准库、声明自己不是真实远程技能。"""
        for fixture in ("external-report-skill", "external-report-skill-lite"):
            base = ROOT / "fixtures" / fixture
            skill_md = (base / "SKILL.md").read_text(encoding="utf-8")
            self.assertIn("合成", skill_md, f"{fixture} 声明自己受控合成")
            script = (base / "scripts" / "make_report.py").read_text(encoding="utf-8")
            self.assertNotIn("urllib", script)
            self.assertNotIn("requests", script)
            self.assertIn("invocation", script, f"{fixture} 把实际调用写进记录")


class ChecksOrderContractTests(unittest.TestCase):
    """检查项只追加：record.method 进入固定顺序，既有检查 id 不变。"""

    def test_method_check_appended_after_existing_ids(self):
        """检查项只追加：method 在 revision（#75）之前进入固定顺序，既有 id 不变。"""
        self.assertEqual(rc.Checks.ORDER[-2], "record.method")
        for cid in ("report.parse", "record.checks", "record.claims", "record.reading",
                    "record.sources", "record.visual", "record.length"):
            self.assertIn(cid, rc.Checks.ORDER)
        self.assertEqual(len(rc.Checks.ORDER), 15)

    def test_method_kinds_closed_enum(self):
        self.assertEqual(rc.METHOD_KINDS, ("builtin", "external"))


if __name__ == "__main__":
    unittest.main()
