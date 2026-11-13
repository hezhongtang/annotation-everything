"""工单 #161 的文档契约：总览首页的 ADR、术语表、双语 references 与 README 同步。

只读仓库文件，不起服务、不占共享资源；总览首页的文档要点漂移时在这里失败。
锚点是各文档承诺过的要点（单实例、保留端口、--under、更新与卸载边界、能力探测），
不是实现细节；改文档时同步维护这里的锚点。
"""

import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SKILL = ROOT / "skill" / "annotation-everything"


def read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


class OverviewHomeAdrTests(unittest.TestCase):
    """新 ADR 记录重开独立总览服务的决定与边界，并与 ADR 0006 互相指向。"""

    @classmethod
    def setUpClass(cls):
        matches = sorted((ROOT / "docs" / "adr").glob("0008-*.md"))
        cls.name = matches[0].name if matches else ""
        cls.text = read(matches[0]) if matches else ""

    def test_new_adr_exists_and_is_accepted(self):
        self.assertTrue(self.name, "缺少 docs/adr/0008-*.md（总览首页 ADR）")
        self.assertIn("status: accepted", self.text, "新 ADR 应标 accepted")

    def test_adr_records_single_instance_and_reuse(self):
        self.assertIn("只运行一个", self.text, "每份台账只运行一个首页实例")
        self.assertIn("相同启动配置", self.text, "相同启动配置复用当前实例")

    def test_adr_records_retained_port_coordination(self):
        self.assertIn("保留端口", self.text, "首页保留端口记在台账")
        self.assertIn("专属端口", self.text, "与审阅专属端口协调")
        self.assertIn("临时顺延", self.text, "端口被占时本次顺延并警告")

    def test_adr_records_under_scope(self):
        self.assertIn("--under", self.text)
        self.assertIn("scope=workspace", self.text, "首页拒绝工作区范围并提示 --under")

    def test_adr_records_update_and_uninstall_boundaries(self):
        self.assertIn("更新锁", self.text, "首页运行期间沿用服务的更新锁")
        self.assertIn("update.py", self.text)
        self.assertIn("--stop-servers", self.text, "卸载先核对身份停止运行中的首页")

    def test_adrs_point_at_each_other(self):
        self.assertIn("0006", self.text, "新 ADR 指向 ADR 0006")
        adr6 = read(ROOT / "docs" / "adr" / "0006-review-overview-crosses-reviews.md")
        self.assertIn("0008", adr6, "ADR 0006 要指向重开该选项的新 ADR")

    def test_adr_0006_keeps_its_conclusions(self):
        adr6 = read(ROOT / "docs" / "adr" / "0006-review-overview-crosses-reviews.md")
        self.assertIn("审阅总览可以跨审阅读取、归档、启动和停止", adr6)
        self.assertIn("单独的总览服务", adr6, "0006 记录的历史取舍保留")


class GlossaryOverviewHomeTests(unittest.TestCase):
    """术语表补总览首页术语（含 Avoid 列表），并修订「审阅总览」定义。"""

    @classmethod
    def setUpClass(cls):
        cls.text = read(ROOT / "GLOSSARY.md")

    def _entry(self, term: str) -> str:
        match = re.search(rf"\*\*{re.escape(term)}\*\*:\n(.+?)(?=\n\n|\Z)", self.text, re.S)
        self.assertIsNotNone(match, f"术语表缺少「{term}」条目")
        return match.group(0)

    def test_overview_home_term_with_avoid_list(self):
        self.assertIn("_Avoid_:", self._entry("总览首页"))

    def test_review_overview_definition_covers_both_hosts(self):
        entry = self._entry("审阅总览")
        self.assertIn("总览首页", entry, "定义要覆盖独立首页宿主，不再隐含寄生在某个审阅上")
        self.assertIn("对照页", entry, "定义要保留从审阅对照页进入的宿主")


class WorkflowOverviewHomeDocsTests(unittest.TestCase):
    """双语 workflow 写清总览首页入口、保留地址与顺延、复用、--under、退出与能力探测。"""

    ZH_ANCHORS = (
        "serve.py overview",
        "不用先启动任何审阅",
        "从任一运行中的对照页进总览",   # 旧路径继续有效
        "保留地址",
        "临时顺延",
        "相同启动配置",
        "--under",
        "scope=workspace",
        "Ctrl+C",
        "--stop-servers",
        "update.py",
        "grep -q overview",
        "exit 1",
    )
    EN_ANCHORS = (
        "serve.py",
        "overview",
        "no review has to be running",
        "from any running review page",
        "reserved address",
        "falls back temporarily",
        "same start configuration",
        "--under",
        "scope=workspace",
        "Ctrl+C",
        "--stop-servers",
        "update.py",
        "grep -q overview",
        "exit 1",
    )

    def test_zh_workflow_anchors(self):
        text = read(SKILL / "references" / "workflow.zh-CN.md")
        missing = [a for a in self.ZH_ANCHORS if a not in text]
        self.assertEqual(missing, [], "中文流程缺少总览首页要点")

    def test_en_workflow_anchors(self):
        text = read(SKILL / "references" / "workflow.en.md")
        missing = [a for a in self.EN_ANCHORS if a not in text]
        self.assertEqual(missing, [], "英文流程缺少总览首页要点")

    def test_both_workflows_link_agent_mechanisms(self):
        for name in ("workflow.zh-CN.md", "workflow.en.md"):
            text = read(SKILL / "references" / name)
            self.assertIn("agent-mechanisms", text, f"{name} 的首页钩子规则要链到机制文档")

    def test_exit_bullet_keeps_the_reserved_port_record(self):
        """退出语义与英文版、ADR 0008 一致：只清运行状态，保留端口记录留下。"""
        zh = read(SKILL / "references" / "workflow.zh-CN.md")
        en = read(SKILL / "references" / "workflow.en.md")
        self.assertIn("退出时清理运行状态，保留端口记录", zh,
                      "中文退出语义要与英文版、ADR 0008 一致：保留端口记录不清理")
        self.assertNotIn("清理运行状态、保留端口记录", zh,
                         "顿号连接会读成连端口记录一起清理，与 serve.py 行为相反")
        self.assertIn("keeps the reserved-port record", en)


class AgentMechanismsProbeTests(unittest.TestCase):
    """双语 agent-mechanisms 指向能力探测，并沿用 #159 的接收规则。"""

    def test_both_languages_point_to_the_capability_probe(self):
        zh = read(SKILL / "references" / "agent-mechanisms.zh-CN.md")
        en = read(SKILL / "references" / "agent-mechanisms.en.md")
        self.assertIn("能力探测", zh)
        self.assertIn("capability probe", en)

    def test_existing_handoff_rules_stay(self):
        zh = read(SKILL / "references" / "agent-mechanisms.zh-CN.md")
        en = read(SKILL / "references" / "agent-mechanisms.en.md")
        self.assertIn("overview-hook-conflict", zh)
        self.assertIn("overview-hook-conflict", en)
        self.assertIn("积压", zh, "启动前的积压仍按现有规则领取")
        self.assertIn("not replayed through the hook", en)


class ReadmeOverviewHomeSyncTests(unittest.TestCase):
    """两份 README 同步总览首页命令，内容与结构保持一致。"""

    @classmethod
    def setUpClass(cls):
        cls.zh = read(ROOT / "README.zh-CN.md")
        cls.en = read(ROOT / "README.md")

    def test_command_reference_covers_overview_and_under(self):
        for text, label in ((self.zh, "中文 README"), (self.en, "英文 README")):
            self.assertIn("serve.py overview", text, f"{label} 命令一览缺 serve.py overview")
            self.assertIn("--under", text, f"{label} 命令一览缺 --under")

    def test_features_mention_the_home(self):
        self.assertIn("不用先运行任何审阅", self.zh, "中文「功能」格提到首页入口")
        self.assertIn("without any running review", self.en, "English features cell mentions the home")

    def test_overview_section_describes_the_home(self):
        self.assertIn("总览首页", self.zh, "中文「审阅总览与归档」描述独立首页")
        self.assertIn("overview home", self.en, "English overview section describes the home")

    def test_project_structure_lines_follow_the_new_commands(self):
        for text, label in ((self.zh, "中文"), (self.en, "英文")):
            self.assertIn("forget / check / overview", text, f"{label}项目结构的 serve.py 行")
            self.assertIn("总览首页的保留端口" if label == "中文" else "reserved port",
                          text, f"{label}项目结构的 ledger.py 行")

    def test_interface_language_bullet_includes_overview(self):
        for text in (self.zh, self.en):
            self.assertIn("`check` / `overview`", text, "界面语言子命令清单补 overview")


class VersionBadgeConsistencyTests(unittest.TestCase):
    """版本徽章、两版头图与 serve.py 的 VERSION 常量一致（本次不发版）。"""

    def test_version_sources_agree(self):
        version = re.search(r'^VERSION = "([^"]+)"', read(SKILL / "scripts" / "serve.py"), re.M)
        self.assertIsNotNone(version, "serve.py 有 VERSION 常量")
        version = version.group(1)
        self.assertRegex(version, r"\d+\.\d+\.\d+")
        for name in ("README.md", "README.zh-CN.md"):
            text = read(ROOT / name)
            self.assertRegex(text, rf'alt="(?:version|版本) {version}"',
                             f"{name} 徽章 alt 版本号")
            self.assertIn(f"-{version}-1f3a5f", text, f"{name} 徽章 URL 版本号")
        for svg in ("banner.zh.svg", "banner.en.svg"):
            self.assertIn(version, read(ROOT / "docs" / "assets" / svg), f"{svg} 头图版本号")


if __name__ == "__main__":
    unittest.main()
