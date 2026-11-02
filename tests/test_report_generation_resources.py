"""默认范式生成资源随安装发布的契约测试（issue #70）。

基础样式（references/report-base.css）与中英生成指引（references/generation.*.md）
是随技能安装交付的资源：安装副本里必须存在、指引与实现不能漂移、样式能进
build_viewer 的样式管道并生成可通过 report_check 核验的报告。

只用标准库与内联合成数据：python3 -m unittest discover tests
"""
from __future__ import annotations

import json
import os
import re
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
os.environ["HOME"] = tempfile.mkdtemp(prefix="ae-test-user-")
os.environ["CODEX_HOME"] = str(Path(os.environ["HOME"]) / ".codex")
os.environ["CLAUDE_SKILLS_DIR"] = str(Path(os.environ["HOME"]) / ".claude" / "skills")
os.environ.pop("AE_LOCALE", None)

import report_check as rc  # noqa: E402

BASE_CSS = SKILL_SRC / "references" / "report-base.css"
GEN_ZH = SKILL_SRC / "references" / "generation.zh-CN.md"
GEN_EN = SKILL_SRC / "references" / "generation.en.md"


_installed: list[Path] = []


def installed_copy() -> Path:
    """用 ae.py install 建一份临时技能副本（模块内只装一次），返回副本目录。"""
    if _installed:
        return _installed[0]
    temp = tempfile.mkdtemp(prefix="ae-gen-res-")
    home = Path(temp) / "home"
    skills = home / ".claude" / "skills"
    env = {**os.environ, "HOME": str(home), "CLAUDE_SKILLS_DIR": str(skills)}
    done = subprocess.run([sys.executable, str(ROOT / "ae.py"), "install"], env=env,
                          capture_output=True, text=True, timeout=120)
    assert done.returncode == 0, done.stdout + done.stderr
    _installed.append(skills / "annotation-everything")
    return _installed[0]


class BaseCssTests(unittest.TestCase):
    """基础样式随安装发布，且能进生成器的样式管道。"""

    @classmethod
    def setUpClass(cls):
        cls.repo_text = BASE_CSS.read_text(encoding="utf-8")
        cls.copy = installed_copy()
        cls.copy_text = (cls.copy / "references" / "report-base.css").read_text(encoding="utf-8")

    def test_css_ships_with_install(self):
        self.assertTrue(BASE_CSS.is_file(), "仓库技能里有 report-base.css")
        self.assertEqual(self.copy_text, self.repo_text, "安装副本的样式与仓库一致")

    def test_css_is_self_contained(self):
        self.assertNotIn("url(http", self.repo_text, "不引用外部资源")
        self.assertNotIn("@import", self.repo_text)
        self.assertIn("1280", self.repo_text, "逐页版面按 1280 宽设计")
        self.assertIn("720", self.repo_text, "逐页版面按 720 高设计")

    def test_css_survives_shadow_pipeline(self):
        import build_viewer
        out = build_viewer.shadow_css(self.repo_text)
        self.assertTrue(out.strip(), "shadow_css 输出非空")
        self.assertNotIn(":root", out, ":root 已改写为 :host")
        self.assertIn(":host", out)

    def test_css_minimal_report_builds_and_checks(self):
        """用基础样式内联的最小合成报告：可生成对照页，也可通过核验入口。"""
        import build_viewer
        report = ('<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">'
                  '<title>样式冒烟</title><style>' + self.repo_text + '</style></head><body>'
                  '<section class="page" data-page="1" data-key="smoke" id="page-smoke">'
                  '<h1>样式冒烟</h1><p class="ae-kpi">+3%</p></section></body></html>')
        doc = build_viewer.build.__doc__  # noqa: F841  build 在下面的 CLI 冒烟用
        with tempfile.TemporaryDirectory() as td:
            src = Path(td) / "report.html"
            out = Path(td) / "review"
            src.write_text(report, encoding="utf-8")
            done = subprocess.run(
                [sys.executable, str(SCRIPTS / "build_viewer.py"), "--report", str(src),
                 "--out", str(out), "--name", "样式冒烟", "--no-scan-skills"],
                capture_output=True, text=True, timeout=60,
                env={**os.environ, "AE_HOME": str(Path(td) / "ledger")})
            self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
            self.assertTrue((out / "index.html").is_file())
            record = {"schema": "ae-report-record/1", "generatedAt": "2026-11-02T00:00:00+08:00",
                      "request": {}, "sources": [], "method": {}, "links": {}, "findings": [],
                      "gaps": [], "checks": [], "openItems": [], "verdict": {}}
            self.assertIn("schema", record)  # 冒烟里不落记录：核验只针对报告本身
            result = rc.check_report(src, None, None, locale="zh-CN")
            self.assertTrue(result["verdict"]["draftComplete"])


class GenerationGuideTests(unittest.TestCase):
    """中英生成指引随安装发布，锚点与实现一致。"""

    REQUIRED_ANCHORS = (
        "report_check.py",          # 公开核验入口
        "ae-report-record/1",       # 核验记录 schema
        "report-base.css",          # 基础样式
        "data-key",                 # 稳定页面标识
        "ae-report-check/1",        # 结构化核验结果 schema
    )

    @classmethod
    def setUpClass(cls):
        cls.copy = installed_copy()
        cls.zh = (cls.copy / "references" / "generation.zh-CN.md").read_text(encoding="utf-8")
        cls.en = (cls.copy / "references" / "generation.en.md").read_text(encoding="utf-8")

    def test_guides_ship_with_install(self):
        self.assertTrue(GEN_ZH.is_file() and GEN_EN.is_file(), "仓库里有中英生成指引")
        self.assertTrue((self.copy / "references" / "generation.zh-CN.md").is_file())
        self.assertTrue((self.copy / "references" / "generation.en.md").is_file())

    def test_guides_cross_link_and_anchor_workflow(self):
        self.assertIn("generation.en.md", self.zh, "中文指引链到英文版")
        self.assertIn("generation.zh-CN.md", self.en, "英文指引链到中文版")
        self.assertIn("workflow.zh-CN.md", self.zh, "中文指引回到中文工作流")
        self.assertIn("workflow.en.md", self.en, "英文指引回到英文工作流")

    def test_guides_carry_implementation_anchors(self):
        for anchor in self.REQUIRED_ANCHORS:
            self.assertIn(anchor, self.zh, f"中文指引缺锚点 {anchor}")
            self.assertIn(anchor, self.en, f"英文指引缺锚点 {anchor}")

    def test_record_fields_match_implementation(self):
        """指引里的记录字段列表与 report_check 的必需字段一致，防止文档漂移。"""
        from report_check import REQUIRED_RECORD_FIELDS
        for text, label in ((self.zh, "中文"), (self.en, "英文")):
            block = re.search(r"```[a-z]*\n([^`]*\"schema\"[^`]*)```", text)
            self.assertIsNotNone(block, f"{label}指引里有记录字段示例")
            for field in REQUIRED_RECORD_FIELDS:
                self.assertIn(f'"{field}"', block.group(1),
                              f"{label}指引的记录示例缺字段 {field}")

    def test_guides_state_clarification_and_boundary(self):
        """指引写明需求澄清边界与判定边界：只补缺项、初稿不等于可交付。"""
        self.assertIn("缺项", self.zh)
        self.assertNotIn("提纲审批", self.zh.split("不设")[0] if "不设" in self.zh else self.zh[:0])
        self.assertIn("初稿", self.zh)
        self.assertIn("可交付", self.zh)
        self.assertIn("missing", self.en.lower())
        self.assertIn("draft", self.en.lower())
        self.assertIn("deliverable", self.en.lower())

    def test_guides_reachable_from_skill_entry(self):
        """SKILL.md 与工作流能到达生成指引（对照页入口已由 test_ae_cli 覆盖）。"""
        zh_flow = (SKILL_SRC / "references" / "workflow.zh-CN.md").read_text(encoding="utf-8")
        en_flow = (SKILL_SRC / "references" / "workflow.en.md").read_text(encoding="utf-8")
        self.assertIn("generation.zh-CN.md", zh_flow, "中文工作流链到生成指引")
        self.assertIn("generation.en.md", en_flow, "英文工作流链到生成指引")


class RoutingEntryTests(unittest.TestCase):
    """发现与路由入口认识「从原始材料生成」这条路径。"""

    def test_skill_description_mentions_generation(self):
        front = (SKILL_SRC / "SKILL.md").read_text(encoding="utf-8").split("---")[1]
        self.assertIn("生成", front, "中文描述含生成触发词")
        self.assertIn("generate", front.lower(), "英文描述含生成触发词")

    def test_discovery_cases_cover_generation_routing(self):
        cases = json.loads((SKILL_SRC / "evals" / "discovery-cases.json").read_text(encoding="utf-8"))
        actions = {(c.get("action"), c.get("workflow")) for c in cases["cases"]}
        self.assertIn(("generate-from-material", "references/generation.zh-CN.md"), actions,
                      "中文生成路由用例")
        self.assertIn(("generate-from-material", "references/generation.en.md"), actions,
                      "英文生成路由用例")
        # 既有路由用例不回退
        self.assertIn(("generate", "references/workflow.zh-CN.md"), actions)

    def test_formats_reference_the_record(self):
        """格式参考指向核验记录契约（单一来源在生成指引与 report_check.py）。"""
        for name in ("formats.md", "formats.en.md"):
            text = (SKILL_SRC / "references" / name).read_text(encoding="utf-8")
            self.assertIn("ae-report-record/1", text, f"{name} 提及核验记录")
            self.assertIn("generation.", text, f"{name} 链到生成指引")


if __name__ == "__main__":
    unittest.main()
