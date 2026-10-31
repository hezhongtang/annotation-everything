"""浏览器场景注册表与选择调度的测试（issue #53）。

只测无浏览器依赖的调度层：场景注册表完整性、旧参数别名、选择去重与
冲突拒绝、执行计划与清单来自同一份注册表。真实浏览器行为在
tests/e2e_browser.py 的端到端层验证，这里不启动浏览器。
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

TESTS = Path(__file__).resolve().parent
sys.path.insert(0, str(TESTS))

import e2e_scenarios as reg  # noqa: E402


# 改造前无参数完整执行的场景顺序（tests/e2e_browser.py 旧 main() 的行为矩阵）。
# 新增场景时在注册表登记后同步更新这里，保持全量模式可解释。
FULL_ORDER = (
    "installed-delivery",
    "locale-live-controls",
    "skill-search",
    "skill-recommendations",
    "skill-i18n",
    "full-check",
    "locale-loop",
    "english-script",
    "handoff-i18n",
    "export-import",
    "file-mode",
    "css-fidelity",
    "server-mode",
)

# 旧局部参数的原定目标：只跑各自的场景组，不再附带安装交付与语言控件。
LEGACY_TARGETS = {
    "--delivery-only": ("installed-delivery",),
    "--full-check-only": ("full-check",),
    "--handoff-only": ("handoff-i18n",),
    "--skills-only": ("skill-search", "skill-recommendations", "skill-i18n"),
}


class RegistryTests(unittest.TestCase):
    def test_registry_covers_full_mode_order_and_every_scenario_is_described(self):
        self.assertEqual(reg.SCENARIO_IDS, FULL_ORDER)
        for scenario in reg.SCENARIOS:
            self.assertTrue(scenario.id, "场景要有稳定 ID")
            self.assertEqual(scenario.id, scenario.id.lower())
            self.assertTrue(scenario.title.strip(), "%s 缺行为说明" % scenario.id)
            self.assertTrue(scenario.fixture.strip(), "%s 缺 fixture 声明" % scenario.id)
            self.assertTrue(scenario.isolation.strip(), "%s 缺隔离声明" % scenario.id)
            self.assertTrue(scenario.full_required, "%s 当前应全量必选" % scenario.id)

    def test_ids_are_unique(self):
        ids = [scenario.id for scenario in reg.SCENARIOS]
        self.assertEqual(len(ids), len(set(ids)))

    def test_scenario_by_id(self):
        scenario = reg.scenario_by_id("server-mode")
        self.assertEqual(scenario.id, "server-mode")
        with self.assertRaises(KeyError):
            reg.scenario_by_id("no-such")


class SelectionTests(unittest.TestCase):
    def test_no_arguments_select_full_mode(self):
        request = reg.resolve_request([])
        self.assertEqual(request.mode, "full")
        self.assertEqual(request.plan, FULL_ORDER)
        self.assertFalse(request.listing)
        self.assertEqual(request.source, "default")

    def test_only_selects_deduped_scenarios_in_registry_order(self):
        request = reg.resolve_request(["--only", "server-mode,full-check,server-mode"])
        self.assertEqual(request.mode, "partial")
        self.assertEqual(request.plan, ("full-check", "server-mode"))
        self.assertEqual(request.source, "--only")

    def test_repeated_only_flags_are_merged(self):
        request = reg.resolve_request(["--only", "skill-i18n", "--only", "full-check"])
        self.assertEqual(request.plan, ("skill-i18n", "full-check"))

    def test_single_scenario_plan_has_only_the_target(self):
        request = reg.resolve_request(["--only", "skill-i18n"])
        self.assertEqual(request.plan, ("skill-i18n",))

    def test_unknown_scenario_is_rejected_with_recovery_guidance(self):
        with self.assertRaises(reg.UsageError) as ctx:
            reg.resolve_request(["--only", "skill-i18n,no-such"])
        message = str(ctx.exception)
        self.assertIn("no-such", message)
        self.assertIn("--list", message)
        self.assertIn("server-mode", message, "错误信息里要列出可用场景")

    def test_empty_selection_is_rejected(self):
        for argv in (["--only", ""], ["--only", ","], ["--only", " , "]):
            with self.assertRaises(reg.UsageError) as ctx:
                reg.resolve_request(argv)
            self.assertIn("空", str(ctx.exception))

    def test_mixing_only_with_legacy_flag_is_a_conflict(self):
        with self.assertRaises(reg.UsageError) as ctx:
            reg.resolve_request(["--only", "full-check", "--skills-only"])
        self.assertIn("冲突", str(ctx.exception))

    def test_multiple_legacy_flags_conflict(self):
        with self.assertRaises(reg.UsageError) as ctx:
            reg.resolve_request(["--delivery-only", "--handoff-only"])
        self.assertIn("冲突", str(ctx.exception))

    def test_unknown_argument_is_rejected(self):
        with self.assertRaises(reg.UsageError) as ctx:
            reg.resolve_request(["--wat"])
        self.assertIn("--wat", str(ctx.exception))

    def test_positional_argument_is_rejected(self):
        with self.assertRaises(reg.UsageError):
            reg.resolve_request(["full-check"])


class LegacyAliasTests(unittest.TestCase):
    def test_registry_declares_the_legacy_flags(self):
        self.assertEqual(reg.LEGACY_FLAGS, LEGACY_TARGETS)

    def test_every_legacy_flag_resolves_to_its_target_scenarios_only(self):
        for flag, targets in LEGACY_TARGETS.items():
            with self.subTest(flag=flag):
                request = reg.resolve_request([flag])
                self.assertEqual(request.mode, "partial")
                self.assertEqual(request.plan, targets, "别名只执行原定目标，不多不少")
                self.assertEqual(request.source, flag)
                # 改造前所有旧参数都会先跑语言控件场景：别名不得再夹带
                self.assertNotIn("locale-live-controls", request.plan)
                for target in targets:
                    if target != "installed-delivery":
                        self.assertNotIn("installed-delivery", request.plan,
                                         "除 --delivery-only 外不得夹带安装交付")

    def test_alias_notes_are_attached_to_the_matching_scenarios(self):
        for flag, targets in LEGACY_TARGETS.items():
            for target in targets:
                self.assertIn(flag, reg.scenario_by_id(target).aliases,
                              "%s 应声明别名 %s" % (target, flag))


class ListingTests(unittest.TestCase):
    def test_listing_names_every_scenario_with_metadata(self):
        text = reg.format_listing()
        self.assertIn("浏览器场景", text)
        for scenario in reg.SCENARIOS:
            self.assertIn(scenario.id, text)
            self.assertIn(scenario.title, text)
            self.assertIn(scenario.fixture, text)
            self.assertIn(scenario.isolation, text)
            self.assertIn("全量必选", text)
        self.assertIn("--delivery-only", text, "清单里要能看到旧参数别名")

    def test_plan_lines_come_from_the_registry(self):
        request = reg.resolve_request(["--only", "skill-i18n,full-check"])
        text = reg.format_plan(request)
        self.assertIn("full-check", text)
        self.assertIn("skill-i18n", text)
        self.assertIn("部分", text)
        full = reg.resolve_request([])
        self.assertIn("全部", reg.format_plan(full))


class RunnerConsistencyTests(unittest.TestCase):
    def test_matching_runner_set_passes(self):
        self.assertIsNone(reg.ensure_runners(set(FULL_ORDER)))

    def test_missing_runner_is_reported(self):
        problem = reg.ensure_runners(set(FULL_ORDER) - {"server-mode"})
        self.assertIsNotNone(problem)
        self.assertIn("server-mode", problem)

    def test_unregistered_runner_is_reported(self):
        problem = reg.ensure_runners(set(FULL_ORDER) | {"ghost"})
        self.assertIsNotNone(problem)
        self.assertIn("ghost", problem)
        self.assertIn("注册", problem)


if __name__ == "__main__":
    unittest.main()
