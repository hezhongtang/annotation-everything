"""单元测试：中英 locale 适配层与共享 JSON 词典。只用标准库。"""
from __future__ import annotations

import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "skill" / "annotation-everything" / "scripts"
LOCALES = ROOT / "skill" / "annotation-everything" / "locales"
sys.path.insert(0, str(SCRIPTS))

import i18n  # noqa: E402


def tmpdir() -> Path:
    return Path(tempfile.mkdtemp())


class NormalizeLocaleTests(unittest.TestCase):
    def test_simplified_chinese_aliases(self):
        for tag in ("zh", "ZH", "zh-CN", "zh_cn", "zh-Hans", "zh-hans-cn", "zh-Hans-CN", "zh_CN.UTF-8"):
            self.assertEqual(i18n.normalize_locale(tag), "zh-CN", tag)

    def test_english_aliases(self):
        for tag in ("en", "EN", "en-US", "en_us", "en-GB", "en-GB-oxendict", "en_US.UTF-8"):
            self.assertEqual(i18n.normalize_locale(tag), "en", tag)

    def test_unsupported_and_traditional_return_none(self):
        for tag in ("zh-TW", "zh-Hant", "zh-Hant-TW", "fr", "ja", "ko", "", None, "  ", "x"):
            self.assertIsNone(i18n.normalize_locale(tag), tag)

    def test_whitespace_and_case_tolerated(self):
        self.assertEqual(i18n.normalize_locale("  en-US  "), "en")


class ResolveLocaleTests(unittest.TestCase):
    def test_no_configuration_falls_back_to_chinese(self):
        """旧审阅与旧调用都没有语言设置：保持中文。"""
        self.assertEqual(i18n.resolve_locale(None), "zh-CN")
        self.assertEqual(i18n.resolve_locale(None, system_languages=["en-US"]), "zh-CN")

    def test_explicit_choice_wins(self):
        self.assertEqual(i18n.resolve_locale("en"), "en")
        self.assertEqual(i18n.resolve_locale("zh-CN"), "zh-CN")
        self.assertEqual(i18n.resolve_locale("zh-Hans"), "zh-CN")

    def test_auto_matches_system_languages_in_order(self):
        self.assertEqual(i18n.resolve_locale("auto", system_languages=["ja", "zh-CN", "en"]), "zh-CN")
        self.assertEqual(i18n.resolve_locale("auto", system_languages=["fr-FR", "en-GB"]), "en")
        self.assertEqual(i18n.resolve_locale("auto", system_languages=[]), "en")
        self.assertEqual(i18n.resolve_locale("auto", system_languages=["zh-TW"]), "en")

    def test_unsupported_choice_raises_with_code(self):
        with self.assertRaises(i18n.LocaleError) as cm:
            i18n.resolve_locale("fr")
        self.assertEqual(cm.exception.code, "unsupported-locale")
        self.assertEqual(cm.exception.params, {"locale": "fr"})

    def test_auto_is_case_insensitive(self):
        self.assertEqual(i18n.resolve_locale("AUTO", system_languages=["en"]), "en")


class InterpolateTests(unittest.TestCase):
    def test_named_placeholders(self):
        self.assertEqual(i18n.interpolate("第 {page} 页：{text}", {"page": 3, "text": "改标题"}),
                         "第 3 页：改标题")

    def test_escaped_braces_stay_literal(self):
        self.assertEqual(i18n.interpolate("{{count}} 与 {count}", {"count": 2}), "{count} 与 2")

    def test_missing_parameter_raises(self):
        with self.assertRaises(i18n.MessageFormatError):
            i18n.interpolate("第 {page} 页", {})

    def test_extra_parameters_ignored(self):
        self.assertEqual(i18n.interpolate("{a}", {"a": 1, "b": 2}), "1")

    def test_placeholders_lists_names(self):
        self.assertEqual(i18n.placeholders("{page} / {name} / {{x}}"), {"page", "name"})


class CatalogTests(unittest.TestCase):
    def test_repo_catalogs_exist_for_both_languages(self):
        for locale in i18n.SUPPORTED:
            self.assertTrue((LOCALES / f"{locale}.json").is_file(), locale)

    def test_load_catalog_returns_messages(self):
        cat = i18n.load_catalog("zh-CN")
        self.assertEqual(cat["locale"], "zh-CN")
        self.assertIsInstance(cat["messages"], dict)
        self.assertTrue(cat["messages"])

    def test_load_catalog_from_installed_copy(self):
        """技能副本里同样能读到资源（安装后运行的路径）。"""
        copy = tmpdir()
        shutil.copytree(LOCALES, copy / "locales")
        cat = i18n.load_catalog("en", root=copy)
        self.assertEqual(cat["locale"], "en")
        self.assertTrue(cat["messages"])

    def test_missing_resource_raises_clear_error(self):
        with self.assertRaises(i18n.CatalogError):
            i18n.load_catalog("en", root=tmpdir())

    def test_broken_json_raises_clear_error(self):
        root = tmpdir()
        (root / "locales").mkdir()
        (root / "locales" / "zh-CN.json").write_text("{ 坏了", encoding="utf-8")
        with self.assertRaises(i18n.CatalogError):
            i18n.load_catalog("zh-CN", root=root)

    def test_locale_tag_must_match_file(self):
        root = tmpdir()
        (root / "locales").mkdir()
        (root / "locales" / "en.json").write_text(json.dumps({"locale": "zh-CN", "messages": {}}),
                                                  encoding="utf-8")
        with self.assertRaises(i18n.CatalogError):
            i18n.load_catalog("en", root=root)


class TranslateTests(unittest.TestCase):
    def setUp(self):
        self.zh = i18n.load_catalog("zh-CN")
        self.en = i18n.load_catalog("en")

    def test_plain_lookup(self):
        key = next(iter(self.zh["messages"]))
        self.assertEqual(i18n.translate(self.zh, key), self.zh["messages"][key])

    def test_english_plural_branches(self):
        key = self._plural_key()
        self.assertIn(f"{key}.one", self.en["messages"])
        self.assertIn(f"{key}.other", self.en["messages"])
        for count, suffix in ((1, "one"), (0, "other"), (2, "other")):
            template = self.en["messages"][f"{key}.{suffix}"]
            self.assertEqual(i18n.translate(self.en, key, count=count),
                             i18n.interpolate(template, {"count": count}))
        self.assertNotEqual(i18n.translate(self.en, key, count=1),
                            i18n.translate(self.en, key, count=2))

    def test_chinese_uses_base_message_for_every_count(self):
        """中文不需要无意义的复数分支：0 / 1 / 2 都用同一条基础消息，只替换计数。"""
        key = self._plural_key()
        self.assertNotIn(f"{key}.one", self.zh["messages"])
        self.assertNotIn(f"{key}.other", self.zh["messages"])
        template = self.zh["messages"][key]
        for count in (0, 1, 2):
            self.assertEqual(i18n.translate(self.zh, key, count=count),
                             i18n.interpolate(template, {"count": count}))

    def test_missing_key_falls_back_to_other_language_and_records(self):
        seen: list[str] = []
        cat = {"locale": "zh-CN", "messages": {"only.here": "只有中文"}}
        self.assertEqual(i18n.translate(cat, "only.here", on_missing=seen.append), "只有中文")
        self.assertEqual(seen, [])

        other = i18n.load_catalog("en")
        key = next(k for k in other["messages"] if k not in cat["messages"])
        text = i18n.translate(cat, key, catalogs=i18n.load_all(), on_missing=seen.append)
        self.assertEqual(text, other["messages"][key])
        self.assertEqual(seen, [key])

    def _plural_key(self):
        return next(k for k in self.en["messages"] if f"{k}.other" in self.en["messages"])


class CheckCatalogTests(unittest.TestCase):
    def setUp(self):
        self.root = tmpdir()

    def _write(self, name, payload):
        (self.root / "locales").mkdir(exist_ok=True)
        (self.root / "locales" / name).write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    def test_consistent_catalogs_report_no_problems(self):
        self._write("zh-CN.json", {"locale": "zh-CN", "messages": {"a": "甲", "n": "{count} 条"}})
        self._write("en.json", {"locale": "en", "messages": {
            "a": "A", "n": "{count} items", "n.one": "{count} item", "n.other": "{count} items"}})
        self.assertEqual(i18n.check_catalogs(root=self.root), [])

    def test_missing_key_is_reported(self):
        self._write("zh-CN.json", {"locale": "zh-CN", "messages": {"a": "甲", "b": "乙"}})
        self._write("en.json", {"locale": "en", "messages": {"a": "A"}})
        problems = i18n.check_catalogs(root=self.root)
        self.assertTrue(any("b" in p and "en" in p for p in problems), problems)

    def test_placeholder_mismatch_is_reported(self):
        self._write("zh-CN.json", {"locale": "zh-CN", "messages": {"a": "第 {page} 页"}})
        self._write("en.json", {"locale": "en", "messages": {"a": "page {num}"}})
        problems = i18n.check_catalogs(root=self.root)
        self.assertTrue(any("page" in p for p in problems), problems)

    def test_incomplete_plural_branch_is_reported(self):
        self._write("zh-CN.json", {"locale": "zh-CN", "messages": {"n": "{count} 条"}})
        self._write("en.json", {"locale": "en", "messages": {
            "n": "{count} items", "n.one": "{count} item"}})
        problems = i18n.check_catalogs(root=self.root)
        self.assertTrue(any("n.other" in p for p in problems), problems)

    def test_counted_english_message_without_branches_is_reported(self):
        """英文计数消息一个分支都没有时，运行时会静默显示 "1 annotations"。"""
        self._write("zh-CN.json", {"locale": "zh-CN", "messages": {"n": "{count} 条"}})
        self._write("en.json", {"locale": "en", "messages": {"n": "{count} items"}})
        problems = i18n.check_catalogs(root=self.root)
        self.assertTrue(any("n.one" in p for p in problems), problems)

    def test_chinese_message_with_plural_branches_is_reported(self):
        self._write("zh-CN.json", {"locale": "zh-CN", "messages": {
            "n": "{count} 条", "n.one": "{count} 条"}})
        self._write("en.json", {"locale": "en", "messages": {
            "n": "{count} items", "n.one": "{count} item", "n.other": "{count} items"}})
        problems = i18n.check_catalogs(root=self.root)
        self.assertTrue(any("n.one" in p and "zh-CN" in p for p in problems), problems)

    def test_non_counted_message_needs_no_branches(self):
        self._write("zh-CN.json", {"locale": "zh-CN", "messages": {"a": "第 {page} 页"}})
        self._write("en.json", {"locale": "en", "messages": {"a": "Page {page}"}})
        self.assertEqual(i18n.check_catalogs(root=self.root), [])

    def test_wrong_locale_tag_is_reported(self):
        self._write("zh-CN.json", {"locale": "en", "messages": {"a": "甲"}})
        self._write("en.json", {"locale": "en", "messages": {"a": "A"}})
        problems = i18n.check_catalogs(root=self.root)
        self.assertTrue(any("locale" in p for p in problems), problems)

    def test_repo_catalogs_pass_the_check(self):
        self.assertEqual(i18n.check_catalogs(), [])


if __name__ == "__main__":
    unittest.main()
