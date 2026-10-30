"""页面里的 tMsg 与 Python translate 使用同一套语义。

浏览器函数内嵌在 viewer_template.html，生成后仍是单文件，不另开资源路由。
有 node 或 macOS 自带的 JavaScriptCore 时直接执行这段函数；没有 JS 引擎的
环境只检查函数还在，避免 CI 因缺少运行时而失败。本机和端到端测试会真正执行。
"""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "skill" / "annotation-everything" / "scripts"
TEMPLATE = SCRIPTS / "viewer_template.html"
sys.path.insert(0, str(SCRIPTS))

import i18n  # noqa: E402

_START = "const missingKeys=[];"
_END = "function targetLabel("


def _runtime_source() -> str:
    text = TEMPLATE.read_text(encoding="utf-8")
    start = text.find(_START)
    end = text.find(_END)
    if start < 0 or end < 0 or start > end:
        raise AssertionError("页面脚本缺少与 Python 对齐的 tMsg 运行时")
    return text[start:end]


def _engine() -> list[str] | None:
    node = shutil.which("node")
    if node:
        return [node]
    if sys.platform == "darwin" and shutil.which("osascript"):
        return ["osascript", "-l", "JavaScript"]
    return None


def _run_js(catalogs: dict, cases: list[dict]) -> list[dict]:
    engine = _engine()
    if engine is None:
        raise unittest.SkipTest("没有 JS 引擎，跳过页面函数执行")
    epilogue = "console.log(JSON.stringify(__out));" if engine[0].endswith("node") else "__out"
    source = "\n".join([
        "const CATALOGS = " + json.dumps(catalogs, ensure_ascii=False) + ";",
        "let uiLocale = 'zh-CN';",
        _runtime_source(),
        "var __cases = " + json.dumps(cases, ensure_ascii=False) + ";",
        "var __out = __cases.map(function(c){",
        "  uiLocale = c.locale;",
        "  var before = missingKeys.length;",
        "  var text = tMsg(c.key, c.params);",
        "  return {text: text, recorded: missingKeys.slice(before)};",
        "});",
        epilogue,
    ])
    path = Path(tempfile.mkstemp(suffix=".js")[1])
    path.write_text(source, encoding="utf-8")
    try:
        proc = subprocess.run([*engine, str(path)], capture_output=True, text=True, timeout=30)
    finally:
        path.unlink(missing_ok=True)
    if proc.returncode != 0:
        raise AssertionError(proc.stderr or proc.stdout)
    return json.loads(proc.stdout.strip())


# 对照页的运行时测试。类名不用 glossary 排除的 viewer。
SHIPPED_ZH_PLACEHOLDER = (
    "写修改意见（C）。可先选中报告或讲稿里的文字：直接删除（D）、改为（R），"
    "或作为引用写批注；也可以先「在报告上标注」（单击放点、拖动框选）。"
)


def _between(text: str, start: str, end: str) -> str:
    a = text.find(start)
    b = text.find(end)
    if a < 0 or b < 0 or a > b:
        raise AssertionError(f"页面脚本里找不到 {start!r} … {end!r}")
    return text[a:b]


class ReviewPageRuntimeTests(unittest.TestCase):
    def test_template_embeds_plural_runtime(self):
        source = _runtime_source()
        self.assertIn("function pluralCat(locale, count)", source)
        self.assertIn("function tMsg(key, params)", source)
        self.assertIn("Number.isInteger(params.count)", source)
        self.assertIn('count===1?"one":"other"', source)

    def test_real_catalog_counts_match_python(self):
        catalogs = {tag: i18n.load_catalog(tag)["messages"] for tag in i18n.SUPPORTED}
        cases = []
        expected = []
        for locale in i18n.SUPPORTED:
            catalog = i18n.load_catalog(locale)
            for count in (0, 1, 2):
                cases.append({"locale": locale, "key": "cli.show.count", "params": {"count": count}})
                expected.append(i18n.translate(catalog, "cli.show.count", count=count))
            cases.append({"locale": locale, "key": "viewer.page", "params": {"page": 3}})
            expected.append(i18n.translate(catalog, "viewer.page", page=3))
        got = [item["text"] for item in _run_js(catalogs, cases)]
        self.assertEqual(got, expected)
        # 每语言 3 个计数加 1 条带 {page} 的消息。英文 1 不能被 "1 annotations" 这种前缀误判放过。
        en_one = got[i18n.SUPPORTED.index("en") * 4 + 1]
        self.assertIn("1 annotation", en_one)
        self.assertNotIn("1 annotations", en_one)

    def test_synthetic_rules_match_python(self):
        zh = {"locale": "zh-CN", "messages": {
            "n": "{count} 条",
            "greet": "你好 {name}",
            "brace": "{{count}} 与 {count}",
            "only.zh": "只有中文 {name}",
        }}
        en = {"locale": "en", "messages": {
            "n": "{count} items",
            "n.one": "{count} item",
            "n.other": "{count} items",
            "greet": "Hello {name}",
            "brace": "{{count}} and {count}",
            "only.en": "English only {name}",
        }}
        bundled = {"zh-CN": zh, "en": en}
        page_cats = {tag: cat["messages"] for tag, cat in bundled.items()}
        specs = [
            ("zh-CN", "n", {"count": 0}, {}),
            ("zh-CN", "n", {"count": 1}, {}),
            ("zh-CN", "n", {"count": 2}, {}),
            ("en", "n", {"count": 0}, {}),
            ("en", "n", {"count": 1}, {}),
            ("en", "n", {"count": 2}, {}),
            ("en", "n", {"count": -1}, {}),
            ("zh-CN", "greet", {"name": "甲"}, {}),
            ("en", "brace", {"count": 2}, {}),
            ("zh-CN", "only.en", {"name": "甲"}, {"record": "only.en"}),
            ("en", "only.zh", {"name": "甲"}, {"record": "only.zh"}),
            ("en", "missing.key", {}, {"raw": True}),
        ]
        cases = [{"locale": loc, "key": key, "params": params} for loc, key, params, _ in specs]
        got = _run_js(page_cats, cases)
        for item, (locale, key, params, flag) in zip(got, specs):
            if flag.get("raw"):
                self.assertEqual(item["text"], key)
                self.assertEqual(item["recorded"], [])
                continue
            seen: list[str] = []
            text = i18n.translate(bundled[locale], key, catalogs=bundled, on_missing=seen.append, **params)
            self.assertEqual(item["text"], text, key)
            self.assertEqual(item["recorded"], seen)

    def test_bool_and_missing_placeholder_do_not_break_the_page(self):
        """页面不把 true 当成 1，缺参数时保留花括号，避免一条坏调用打断界面。"""
        catalogs = {"zh-CN": {"n": "{count} 条"}, "en": {
            "n": "{count} items", "n.one": "{count} item", "n.other": "{count} items",
            "greet": "Hello {name}",
        }}
        got = _run_js(catalogs, [
            {"locale": "en", "key": "n", "params": {"count": True}},
            {"locale": "en", "key": "greet", "params": {}},
        ])
        self.assertEqual(got[0]["text"], "true items")
        self.assertNotEqual(got[0]["text"], "1 item")
        self.assertEqual(got[1]["text"], "Hello {name}")
        self.assertEqual(got[0]["recorded"], [])

    def test_chinese_catalog_keeps_shipped_composer_labels(self):
        zh = i18n.load_catalog("zh-CN")["messages"]
        self.assertEqual(zh["viewer.target"], "对象")
        self.assertEqual(zh["viewer.textPlaceholder"], SHIPPED_ZH_PLACEHOLDER)
        self.assertEqual(zh["viewer.add"], "添加批注")
        self.assertEqual(zh["viewer.empty"], "暂无批注。")
        self.assertEqual(zh["viewer.quote"], "引用")

    def test_both_languages_read_composer_and_quote_labels_from_the_catalog(self):
        text = TEMPLATE.read_text(encoding="utf-8")
        apply = _between(text, "function applyLocale()", '$("localePick").addEventListener')
        self.assertIn('tMsg("viewer.add")', apply)
        self.assertIn('tMsg("viewer.target")', apply)
        self.assertIn('tMsg("viewer.textPlaceholder")', apply)
        self.assertIn("targetLabel(opt.value)", apply)
        self.assertNotIn('"添加批注"', apply)
        self.assertNotIn('"对象"', apply)
        self.assertNotIn("zhPlaceholder", apply)
        self.assertNotIn(":opt.value", apply)
        note = _between(text, "function noteHtml(", "function stashEdit(")
        self.assertIn("targetLabel(x.target)", note)
        self.assertIn("targetLabel(x.quoteFrom)", note)
        self.assertNotIn('uiLocale==="en"?targetLabel', note)
        self.assertNotIn("escH(x.quoteFrom)", note)
        refresh = _between(text, "function refreshList(", "function counts(")
        self.assertIn('tMsg("viewer.empty")', refresh)
        self.assertNotIn("暂无批注。", refresh)
        chip = _between(text, "function showQuote()", "function setQuote(")
        self.assertIn('tMsg("viewer.quote")', chip)
        self.assertIn("targetLabel(quoteFrom)", chip)
        self.assertNotIn("引用（", chip)


if __name__ == "__main__":
    unittest.main()
