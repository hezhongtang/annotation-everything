"""全局词典一致性契约：无参数全词典检查的唯一权威位置（issue #51）。

完整单元组（unit，discover tests）经本模块把整个仓库的中英词典完整扫描一次；
unit-related 局部组由 verify.py 自动带上本模块，独立运行相关组也不丢全局保护。
各功能模块只保留自己独有的 key、占位符、复数与调用位置断言；校验器对合成
错误输入的行为测试在 tests/test_i18n.py 的 CheckCatalogTests，不是这里的重复项。
"""
from __future__ import annotations

import json
import re
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "skill" / "annotation-everything" / "scripts"
sys.path.insert(0, str(SCRIPTS))

import i18n  # noqa: E402


class RepoCatalogContractTests(unittest.TestCase):
    def test_shipped_catalogs_stay_consistent(self):
        """中英词典 key、占位符与复数分支必须一致——全仓库只在这里扫一次。"""
        self.assertEqual(i18n.check_catalogs(), [], "中英词典 key、占位符与复数分支必须一致")


class ContractGuardTests(unittest.TestCase):
    """守住「一个权威契约」本身：真的会失败、失败后能恢复、只扫一次。"""

    def _run_contract_case(self) -> unittest.TestResult:
        suite = unittest.TestSuite(
            [RepoCatalogContractTests("test_shipped_catalogs_stay_consistent")])
        result = unittest.TestResult()
        suite.run(result)
        return result

    def _write_locales(self, base: Path, extra_zh: dict, extra_en: dict) -> None:
        """写一对合成词典；两侧不一致的部分由调用方决定。"""
        base.mkdir(parents=True, exist_ok=True)
        zh = {"locale": "zh-CN", "messages": {"a": "甲"}}
        en = {"locale": "en", "messages": {"a": "A"}}
        zh["messages"].update(extra_zh)
        en["messages"].update(extra_en)
        (base / "zh-CN.json").write_text(
            json.dumps(zh, ensure_ascii=False), encoding="utf-8")
        (base / "en.json").write_text(
            json.dumps(en, ensure_ascii=False), encoding="utf-8")

    def test_contract_fails_on_a_synthetic_inconsistent_dictionary(self):
        """合成的不一致词典必须让契约失败；词典修好后同一用例恢复通过。

        失败与恢复都在合成词典上验证，不再多扫一次真实仓库——真实仓库的
        通过态由本模块的契约用例本身在同一个套件里证明。
        """
        with tempfile.TemporaryDirectory(prefix="ae-contract-test-") as name:
            locales = Path(name) / "locales"
            self._write_locales(locales, extra_zh={"probe.key": "探针"}, extra_en={})
            original = i18n._LOCALES_DIR
            i18n._LOCALES_DIR = locales
            try:
                result = self._run_contract_case()
                self.assertFalse(result.wasSuccessful(), "不一致的合成词典没让契约失败")
                self.assertTrue(result.failures, "契约失败要是断言失败，不是报错")
                # 修复：把缺失的 key 补进英文，恢复两侧一致
                self._write_locales(locales, extra_zh={"probe.key": "探针"},
                                    extra_en={"probe.key": "probe"})
                result = self._run_contract_case()
                self.assertTrue(result.wasSuccessful(), "词典修好后契约没有恢复通过")
            finally:
                i18n._LOCALES_DIR = original

    def test_this_module_is_the_only_no_arg_full_catalog_scan(self):
        """无参数的完整词典扫描只允许出现在本模块，防止重复扫描悄悄长回来。"""
        pattern = re.compile(r"check_catalogs\(\s*\)")
        offenders = []
        for path in sorted((ROOT / "tests").glob("test*.py")):
            if path.name == Path(__file__).name:
                continue
            if pattern.search(path.read_text(encoding="utf-8")):
                offenders.append(path.name)
        self.assertEqual(offenders, [], "无参数 check_catalogs() 只能出现在本模块")


if __name__ == "__main__":
    unittest.main()
