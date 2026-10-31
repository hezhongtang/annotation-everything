"""#31 的页面与命令行约束：技能选择、常驻技能走共享词典，存储值与调用名不变。

只检查契约，不绑定 DOM 拼法：渲染路径必须从词典取消息，范围存储值、导出协议
和技能调用名保持原样。真实行为由 tests/e2e_browser.py 的浏览器回归覆盖。
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "skill" / "annotation-everything" / "scripts"
TEMPLATE = SCRIPTS / "viewer_template.html"
FIXTURE = ROOT / "fixtures" / "sample"
sys.path.insert(0, str(SCRIPTS))

import build_viewer  # noqa: E402
import i18n  # noqa: E402
import notes_store as store  # noqa: E402
import serve  # noqa: E402

_CJK = re.compile(r"[一-鿿]")

# #31 新增的消息：key → 英文是否按计数分复数。
# viewer.skill.* 是技能输入栏与候选，viewer.always.* 是常驻技能，cli.skills.* 是命令行。
SKILL_KEYS = {
    "viewer.skill.label": False,
    "viewer.skill.labelTitle": False,
    "viewer.skill.placeholder": False,
    "viewer.skill.editPlaceholder": False,
    "viewer.skill.candidates": False,
    "viewer.skill.recommended": False,
    "viewer.skill.used": False,
    "viewer.skill.showAll": True,
    "viewer.skill.noMatch": False,
    "viewer.skill.none": False,
    "viewer.skill.noteTitle": False,
    "viewer.skill.alwaysChip": False,
    "viewer.skill.alwaysChipTitle": False,
    "viewer.always.button": False,
    "viewer.always.buttonTitle": False,
    "viewer.always.buttonList": False,
    "viewer.always.listSep": False,
    "viewer.always.scopeItem": False,
    "viewer.always.scope.all": False,
    "viewer.always.scope.report": False,
    "viewer.always.scope.script": False,
    "viewer.always.scope.style": False,
    "viewer.always.dialogTitle": False,
    "viewer.always.dialogHint": False,
    "viewer.always.add": False,
    "viewer.always.addTitle": False,
    "viewer.always.save": False,
    "viewer.always.rowPlaceholder": False,
    "viewer.always.removeRow": False,
    "viewer.always.removeRowAria": False,
    "viewer.always.tooMany": True,
    "viewer.always.noServer": False,
    "viewer.always.oldServer": False,
    "viewer.always.saveFailed": False,
    "viewer.always.noReply": False,
    "viewer.always.changed": False,
    "viewer.always.saved": True,
    "viewer.always.cleared": False,
    "viewer.always.importOffer": False,
    "viewer.always.importNone": False,
    "viewer.always.imported": False,
    "cli.skills.catalog": False,
    "cli.skills.catalogEmpty": False,
    "cli.skills.item": False,
    "cli.skills.listSep": False,
    "cli.skills.used": False,
    "cli.skills.always": False,
    "cli.skills.alwaysNone": False,
    "cli.skills.badScope": False,
    "cli.skills.warnUnavailable": False,
    "cli.skills.agentAlways": False,
    "cli.skills.agentSkills": False,
    "cli.skills.agentItem": False,
    "cli.skills.standingSource": False,
    "cli.skills.ownSource": False,
}

# 常驻技能范围的存储值：界面只改显示，这三种写法在中英文下必须一致
ALWAYS_STORAGE_SCOPES = ("all", "报告", "讲稿", "style")

# 语言自己的名字按它自己的文字写，不算未翻译
LANGUAGE_NAMES = ("locale.name.zh-CN", "locale.name.en")


def _between(text: str, start: str, end: str) -> str:
    a = text.find(start)
    b = text.find(end, a + 1) if a >= 0 else -1
    if a < 0 or b < 0:
        raise AssertionError(f"页面里找不到 {start!r} 到 {end!r} 这一段")
    return text[a:b]


def _uses(region: str, key: str) -> bool:
    """渲染路径从词典取这条消息：JS 动态片段直接调 tMsg，静态标记用 data-* 声明。"""
    return (f'tMsg("{key}"' in region
            or f'data-msg="{key}"' in region
            or f'data-title="{key}"' in region
            or f'data-aria="{key}"' in region
            or f'data-ph="{key}"' in region)


def _page_source() -> str:
    return TEMPLATE.read_text(encoding="utf-8")


def _wired_sources() -> str:
    """页面脚本之外，命令行与生成侧也要用上这些消息。"""
    return (_page_source()
            + (ROOT / "skill" / "annotation-everything" / "scripts" / "notes.py").read_text(encoding="utf-8")
            + (ROOT / "skill" / "annotation-everything" / "scripts" / "build_viewer.py").read_text(encoding="utf-8"))


def tmpdir() -> Path:
    return Path(tempfile.mkdtemp())


def run_skills(review: Path, *args: str, env: dict[str, str] | None = None):
    base = {k: v for k, v in os.environ.items() if k not in ("AE_LOCALE", "LC_ALL", "LC_MESSAGES", "LANG")}
    base.update(env or {})
    base["CLAUDECODE"] = ""
    base["AE_HOME"] = str(tmpdir())
    return subprocess.run([sys.executable, str(SCRIPTS / "notes.py"), "skills", str(review), *args],
                          capture_output=True, text=True, env=base, timeout=60)


class SkillCatalogTests(unittest.TestCase):
    """#31 要用的消息在两种语言里都齐，英文不含中文，且都真的接进了界面或命令行。"""

    def test_every_skill_key_exists_in_both_catalogs(self):
        for tag in i18n.SUPPORTED:
            messages = i18n.load_catalog(tag)["messages"]
            self.assertEqual(sorted(set(SKILL_KEYS) - set(messages)), [], f"{tag} 缺少消息")

    def test_english_values_carry_no_chinese(self):
        en = i18n.load_catalog("en")["messages"]
        chinese = sorted(k for k, v in en.items()
                         if _CJK.search(v) and k not in LANGUAGE_NAMES)
        self.assertEqual(chinese, [], "英文消息里混了中文（语言自身的名字除外）")

    def test_counted_english_messages_have_both_plural_branches(self):
        en = i18n.load_catalog("en")["messages"]
        for key, counted in SKILL_KEYS.items():
            if not counted:
                continue
            for suffix in ("one", "other"):
                self.assertIn(f"{key}.{suffix}", en, f"英文计数消息缺 {key}.{suffix}")
            for count in (0, 1, 2):
                text = i18n.translate(i18n.load_catalog("en"), key, count=count, limit=20)
                self.assertIn(str(count), text, f"{key} 在 count={count} 时没换上数字")

    def test_chinese_counted_messages_have_no_plural_branches(self):
        zh = i18n.load_catalog("zh-CN")["messages"]
        branched = sorted(k for k in SKILL_KEYS if SKILL_KEYS[k] and f"{k}.one" in zh)
        self.assertEqual(branched, [], "中文不该有英文式复数分支")

    def test_catalogs_stay_self_consistent(self):
        self.assertEqual(i18n.check_catalogs(), [])

    def test_every_skill_key_is_wired_in(self):
        page = _wired_sources()
        unwired = sorted(k for k in SKILL_KEYS
                         if f'tMsg("{k}"' not in page and f'data-msg="{k}"' not in page
                         and f'data-title="{k}"' not in page and f'data-aria="{k}"' not in page
                         and f'data-ph="{k}"' not in page and f'"{k}"' not in page)
        self.assertEqual(unwired, [], "这些消息加了却没有任何地方用")
    def test_skill_keys_do_not_collide_with_other_issues(self):
        """前缀各自独立：#30 的 viewer.* 条目一个都没动。"""
        zh = i18n.load_catalog("zh-CN")["messages"]
        for key in ("viewer.target", "viewer.add", "viewer.styleBtn", "viewer.edit",
                    "viewer.markPin", "cli.show.count", "cli.status.open"):
            self.assertIn(key, zh, f"不属于 #31 的消息被删掉了：{key}")


class SkillInputTests(unittest.TestCase):
    """技能输入栏、候选与推荐标记从词典取，搜索仍覆盖名称和原始说明。"""

    def setUp(self):
        self.source = _page_source()
        self.form = _between(self.source, '<div class="form" id="nform">', '<div id="selbar">')
        self.render = _between(self.source, "function renderSkillList(", "function chooseSkill(")

    def test_skill_label_and_input_read_from_the_catalog(self):
        for key in ("viewer.skill.label", "viewer.skill.labelTitle", "viewer.skill.placeholder"):
            self.assertTrue(_uses(self.form, key), key)
        self.assertNotIn('title="处理这条批注时让 Agent 调用的技能（skill），多个用逗号分隔（K）"', self.form)
        self.assertNotIn('placeholder="可选，如 humanizer-zh"', self.form)

    def test_candidate_listbox_and_recommendation_read_from_the_catalog(self):
        self.assertTrue(_uses(self.source, "viewer.skill.candidates"), "viewer.skill.candidates")
        self.assertNotIn('aria-label="技能候选"', self.source)
        for key in ("viewer.skill.recommended", "viewer.skill.used", "viewer.skill.showAll",
                    "viewer.skill.noMatch", "viewer.skill.none"):
            self.assertIn(f'"{key}"', self.render, f"候选列表没走词典：{key}")
        self.assertIn('tMsg(q?"viewer.skill.noMatch":"viewer.skill.none")', self.render,
                      "空结果要按有没有查询分开说")
        for literal in (">建议<", "查看全部技能（", "desc:\"用过\""):
            self.assertNotIn(literal, self.render, f"候选列表里还写死了 {literal}")

    def test_search_matches_original_name_and_description(self):
        """搜索命中目录里的原始名称与原始说明，不经过任何翻译副本。"""
        self.assertIn("String(c.name).toLocaleLowerCase().includes(q)", self.render)
        self.assertIn("String(c.desc).toLocaleLowerCase().includes(q)", self.render)
        filtered = _between(self.render, "const matching=", "const ranked=")
        for literal in ("tMsg(c.name", "tMsg(c.desc", "translate(", "localizedName", "localizedDesc"):
            self.assertNotIn(literal, filtered, f"搜索路径改了技能的原始名称或说明：{literal}")

    def test_chosen_skill_name_is_written_back_verbatim(self):
        choose = _between(self.source, "function chooseSkill(", "$(\"skillList\").addEventListener")
        self.assertIn("c.name", choose, "选择候选时按目录里的原名写回输入框")
        for literal in ("tMsg(c.name", "localizedName"):
            self.assertNotIn(literal, choose, f"实际调用名被显示翻译改写了：{literal}")

    def test_plugin_qualified_names_survive_the_catalog_round_trip(self):
        """插件技能带命名空间（market:sample），页面与服务都不改写它。"""
        review = tmpdir() / "plugin"
        review.mkdir()
        (review / "skills.json").write_text(json.dumps({"version": 1, "always": [], "skills": [
            {"name": "sample:sample", "desc": "合成插件技能"},
            {"name": "润色（中文）", "desc": "混合语言说明 mixed-language"},
            {"name": 'quote"and<tag>', "desc": "特殊字符 <>&\"'"},
        ]}, ensure_ascii=False), encoding="utf-8")
        page = (build_viewer.build(FIXTURE / "report.html", None, review, no_scan_skills=True)
                and (review / "index.html").read_text(encoding="utf-8"))
        for name in ("sample:sample", "润色（中文）", 'quote"and<tag>'):
            self.assertIn(name.replace('"', '\\"'), page.replace('\\"', '\\"'), f"页面丢了技能名 {name}")
        self.assertEqual([x["name"] for x in store.skills_catalog(review)],
                         ["sample:sample", "润色（中文）", 'quote"and<tag>'],
                         "技能目录不被显示语言改写")

    def test_edit_form_skills_placeholder_reads_from_the_catalog(self):
        note = _between(self.source, "function noteHtml(", "// 把正在编辑的那张卡片")
        self.assertIn('tMsg("viewer.skill.editPlaceholder")', note)
        self.assertNotIn('placeholder="技能（可选，逗号分隔）"', note)

    def test_note_skill_chips_read_from_the_catalog(self):
        note = _between(self.source, "function noteHtml(", "// 把正在编辑的那张卡片")
        for key in ("viewer.skill.noteTitle", "viewer.skill.alwaysChip", "viewer.skill.alwaysChipTitle"):
            self.assertIn(f'tMsg("{key}"', note, f"卡片上的技能标签没走词典：{key}")
        for literal in ("常驻 ", "title=\"常驻技能（", "title=\"处理这条批注时调用的技能\""):
            self.assertNotIn(literal, note, f"卡片里还写死了 {literal}")


class AlwaysScopeTests(unittest.TestCase):
    """常驻技能范围：显示标签随语言变，存储值与匹配口径不变。"""

    def setUp(self):
        self.source = _page_source()
        self.row = _between(self.source, "function alwaysRow(", "let alwaysOpenSig")
        self.split = _between(self.source, "function splitAlways(", "async function setAlways(")
        self.match = _between(self.source, "const alwaysFor=", "const parseSkills=")

    def test_scope_select_is_built_from_storage_values(self):
        self.assertIn("const ALWAYS_SCOPES=", self.source, "范围选项要走存储值列表")
        self.assertIn("ALWAYS_SCOPES.map(", self.row, "范围下拉由存储值生成")
        self.assertIn("alwaysLabel(k)", self.row, "范围下拉的显示文字走词典")
        self.assertNotIn("Object.entries(ALWAYS_ZH)", self.row)

    def test_scope_labels_map_to_the_same_storage_values_in_both_languages(self):
        for scope in ALWAYS_STORAGE_SCOPES:
            self.assertIn(f'"{scope}"', self.source, f"存储值 {scope} 不能从页面里消失")
        labels = _between(self.source, "const ALWAYS_LABEL=", "const ALWAYS_EXPORT=")
        for scope, key in (("all", "viewer.always.scope.all"), ("报告", "viewer.always.scope.report"),
                           ("讲稿", "viewer.always.scope.script"), ("style", "viewer.always.scope.style")):
            self.assertIn(f'{scope}:"{key}"', labels.replace(" ", ""), f"{scope} 的显示标签没接词典")
        for tag in i18n.SUPPORTED:
            messages = i18n.load_catalog(tag)["messages"]
            self.assertEqual([messages[f"viewer.always.scope.{s}"] for s in ("all", "report", "script", "style")],
                             [messages["viewer.always.scope.all"], messages["viewer.always.scope.report"],
                              messages["viewer.always.scope.script"], messages["viewer.always.scope.style"]])

    def test_scope_matching_keeps_storage_values(self):
        for value in ("报告", "讲稿"):
            self.assertIn(f'a.scope==="{value}"', self.match, f"范围匹配不能用显示值：{value}")
        self.assertIn('a.scope==="all"', self.match)
        self.assertIn('a.scope==="style"', self.match)
        self.assertIn('t.includes("报告")', self.match)
        self.assertIn('t.includes("讲稿")', self.match)

    def test_export_protocol_stays_fixed_chinese(self):
        export = _between(self.source, "const ALWAYS_EXPORT=", "let alwaysSkills=")
        self.assertIn('{all:"全部批注",报告:"报告",讲稿:"讲稿",style:"风格优化"}', export.replace(" ", ""),
                      "导出的常驻技能范围是协议值，必须固定中文")
        self.assertIn("ALWAYS_EXPORT[a.scope]", _between(self.source, "function exportText(", "function parseMd("))

    def test_lenient_scope_read_maps_unknown_to_all(self):
        self.assertIn("ALWAYS_SCOPES.includes(a.scope)?a.scope:\"all\"", self.split.replace(" ", ""))
        self.assertNotIn("ALWAYS_ZH[a.scope]?a.scope", self.split)

    def test_button_and_button_title_read_from_the_catalog(self):
        self.assertIn('data-msg="viewer.always.button"', self.form_region(), "常驻技能按钮的可见文字")
        self.assertIn('tMsg("viewer.always.buttonTitle"', _between(self.source, "function updateAlwaysBtn(", "function alwaysRow("))
        self.assertIn('tMsg("viewer.always.buttonList"', _between(self.source, "function updateAlwaysBtn(", "function alwaysRow("))

    def test_button_title_reads_as_one_sentence_in_both_languages(self):
        """名单和说明两段拼起来，英文不能粘成一句（"…).Every"）。"""
        for tag, tail in (("en", " Every annotation asks"), ("zh-CN", "每条批注（按范围）都让 Agent 先调用")):
            catalog = i18n.load_catalog(tag)
            raw = catalog["messages"]

            def render(msg_key: str, **params: object) -> str:
                # translate() 的第二个位置参数名叫 key，{key} 占位符只能走 interpolate
                return i18n.interpolate(raw[msg_key], params)

            item = render("viewer.always.scopeItem", name="humanizer-zh",
                          scope=render("viewer.always.scope.report"))
            title = (render("viewer.always.buttonList", list=item)
                     + render("viewer.always.buttonTitle", key="G"))
            self.assertIn(tail, title, tag)
            self.assertNotIn(").Every", title, tag)
        self.assertNotIn('title="常驻技能：这份审阅里每条批注（按范围）都让 Agent 先调用', self.source)
        self.assertNotIn("常驻技能：\"+alwaysSkills.map", self.source)

    def form_region(self):
        return _between(self.source, '<div class="form" id="nform">', '<div id="selbar">')


class LocaleSwitchTests(unittest.TestCase):
    """切换语言只改显示：不写盘、不动技能目录、不改范围存储值。"""

    def setUp(self):
        self.source = _page_source()
        self.apply = _between(self.source, "function applyLocale()", '$("localePick").addEventListener')

    def test_locale_change_refreshes_skill_and_always_surfaces(self):
        self.assertIn("updateAlwaysBtn()", self.apply, "切语言要重写常驻技能按钮的提示")
        self.assertIn("renderSkillList()", self.apply, "切语言要重画打开着的候选列表")
        form = _between(self.source, '<div class="form" id="nform">', '<div id="selbar">')
        self.assertIn('data-msg="viewer.skill.label"', form, "技能标签是声明式本地化")

    def test_locale_change_never_writes_skills_or_always(self):
        for literal in ('api("POST","/api/skills/always"', "LS.set(K_ALWAYS", "skills_save", "always_save"):
            self.assertNotIn(literal, self.apply, f"切语言不该写技能数据：{literal}")

    def test_no_always_chinese_map_is_left_behind(self):
        self.assertNotIn("ALWAYS_ZH", self.source, "常驻技能范围标签统一走 alwaysLabel()")
        self.assertIn("const alwaysLabel=", self.source)


class AlwaysDialogTests(unittest.TestCase):
    """常驻技能弹窗的增删、保存与导入提示。"""

    def setUp(self):
        self.source = _page_source()
        self.dialog = _between(self.source, '<div class="modal" id="always">', '<div class="modal" id="batches">')

    def test_dialog_copy_reads_from_the_catalog(self):
        for key in ("viewer.always.dialogTitle", "viewer.always.dialogHint", "viewer.always.add",
                    "viewer.always.addTitle", "viewer.always.save", "viewer.cancel"):
            self.assertTrue(_uses(self.dialog, key), key)
        for literal in ('title="添加一个常驻技能（在名称输入框按回车）"',
                        'title="这份审阅里，范围内的每一条批注都会要求 Agent'):
            self.assertNotIn(literal, self.dialog, f"弹窗里还写死了 {literal}")

    def test_row_controls_read_from_the_catalog(self):
        for key in ("viewer.always.rowPlaceholder", "viewer.always.removeRow", "viewer.always.removeRowAria"):
            self.assertTrue(_uses(self.row_region(), key), key)
        for literal in ('placeholder="技能名，如 humanizer-zh"', 'title="去掉这一个"', 'aria-label="去掉"'):
            self.assertNotIn(literal, self.row_region(), f"行控件里还写死了 {literal}")

    def row_region(self):
        return _between(self.source, "function alwaysRow(", "let alwaysOpenSig")

    def test_limit_and_failure_prompts_read_from_the_catalog(self):
        save = _between(self.source, "async function setAlways(", "async function saveAlways(")
        for key in ("viewer.always.tooMany", "viewer.always.noServer", "viewer.always.oldServer",
                    "viewer.always.saveFailed", "viewer.always.noReply"):
            self.assertIn(f'tMsg("{key}"', save, f"保存失败提示没走词典：{key}")
        for literal in ("常驻技能最多 20 个", "服务没连上：常驻技能暂时保存不了",
                        "本地服务是旧版本，不支持常驻技能", "没能保存常驻技能：", "本地服务没有应答，请稍后再试"):
            self.assertNotIn(literal, save, f"保存提示里还写死了 {literal}")

    def test_overwrite_confirm_and_result_toasts_read_from_the_catalog(self):
        region = _between(self.source, "async function saveAlways(", "$(\"alwaysSave\").onclick")
        self.assertIn('tMsg("viewer.always.changed")', region)
        self.assertIn('tMsg("viewer.always.saved"', region)
        self.assertIn('tMsg("viewer.always.cleared")', region)
        for literal in ("你打开设置之后，常驻技能被改过", "已保存 ", "已清空常驻技能"):
            self.assertNotIn(literal, region, f"保存结果里还写死了 {literal}")

    def test_import_offer_reads_from_the_catalog(self):
        region = _between(self.source, "async function offerAlways(", "$(\"alwaysSave\").onclick")
        for key in ("viewer.always.importOffer", "viewer.always.importNone", "viewer.always.imported",
                    "viewer.always.scopeItem", "viewer.always.listSep"):
            self.assertIn(f'tMsg("{key}"', region, f"导入提示没走词典：{key}")
        for literal in ("导入的内容里带了常驻技能：", "（无）", "已采用导入的常驻技能"):
            self.assertNotIn(literal, region, f"导入提示里还写死了 {literal}")

    def test_import_parser_keeps_the_chinese_protocol(self):
        """导入按导出的中文协议解析，不随界面语言改。"""
        region = _between(self.source, "function importAlways(", "async function offerAlways(")
        self.assertIn("全部批注", region)
        self.assertIn("风格优化", region)


class CliSkillsTests(unittest.TestCase):
    """notes.py skills：中英文输出、范围字面量、错误提示与三方一致。"""

    def setUp(self):
        self.out = tmpdir() / "review"
        build_viewer.build(FIXTURE / "report.html", FIXTURE / "script.md", self.out, no_scan_skills=True)

    def test_chinese_stays_the_default(self):
        r = run_skills(self.out, "--always", "humanizer-zh=讲稿", "frontend-design=风格优化", "checker")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("常驻技能（每条批注按范围先调用）：humanizer-zh（讲稿）、frontend-design（风格优化）、checker（全部批注）", r.stdout)
        self.assertIn("技能目录（页面「技能」输入栏的候选）：", r.stdout)

    def test_english_output_uses_the_same_storage_values(self):
        run_skills(self.out, "--always", "humanizer-zh=讲稿")
        r = run_skills(self.out, "--locale", "en")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("Standing skills (called first for every annotation, by scope): "
                      "humanizer-zh (Speaker notes)", r.stdout)
        self.assertIn("Skill catalog (candidates for the page's skill box): (empty)", r.stdout)
        self.assertNotIn("常驻技能", r.stdout)
        self.assertEqual(store.always_skills(self.out), [{"name": "humanizer-zh", "scope": "讲稿"}],
                         "英文输出不改存储值")

    def test_english_scope_tokens_map_to_the_same_storage_values(self):
        r = run_skills(self.out, "--locale", "en", "--always", "a=report", "b=script", "c=all", "d=style")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(store.always_skills(self.out),
                         [{"name": "a", "scope": "报告"}, {"name": "b", "scope": "讲稿"},
                          {"name": "c", "scope": "all"}, {"name": "d", "scope": "style"}])
        chinese = run_skills(self.out, "--always", "a=报告", "b=讲稿", "c=全部", "d=风格优化")
        self.assertEqual(chinese.returncode, 0, chinese.stdout + chinese.stderr)
        self.assertEqual(store.always_skills(self.out),
                         [{"name": "a", "scope": "报告"}, {"name": "b", "scope": "讲稿"},
                          {"name": "c", "scope": "all"}, {"name": "d", "scope": "style"}])

    def test_environment_beats_review_and_review_beats_default(self):
        manifest = json.loads((self.out / "review.json").read_text(encoding="utf-8"))
        manifest["locale"] = "en"
        (self.out / "review.json").write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")
        self.assertIn("Standing skills", run_skills(self.out).stdout)
        self.assertIn("常驻技能", run_skills(self.out, env={"AE_LOCALE": "zh-CN"}).stdout)
        self.assertIn("Standing skills", run_skills(self.out, "--locale", "en", env={"AE_LOCALE": "zh-CN"}).stdout)

    def test_bad_locale_is_diagnosed_and_changes_nothing(self):
        before = (self.out / "skills.json").read_text(encoding="utf-8") if (self.out / "skills.json").exists() else ""
        r = run_skills(self.out, "--locale", "fr")
        self.assertEqual(r.returncode, 2)
        self.assertIn("不支持的语言", r.stderr)
        after = (self.out / "skills.json").read_text(encoding="utf-8") if (self.out / "skills.json").exists() else ""
        self.assertEqual(before, after)

    def test_bad_scope_message_is_localized_and_actionable(self):
        r = run_skills(self.out, "--always-add", "x=哪里")
        self.assertEqual(r.returncode, 2)
        self.assertIn("常驻技能范围「哪里」不认识，可选：", r.stderr)
        for token in ("全部批注", "报告", "讲稿", "风格优化"):
            self.assertIn(token, r.stderr)
        en = run_skills(self.out, "--locale", "en", "--always-add", "x=nowhere")
        self.assertEqual(en.returncode, 2)
        self.assertIn("I do not recognise the standing-skill scope", en.stderr)
        self.assertIn("All annotations", en.stderr)
        self.assertIn("Speaker notes", en.stderr)
        self.assertNotIn("常驻技能范围", en.stderr)
        # 候选必须全是当前语言可键入的字面量：英文提示里不能混中文，也不能漏掉 report / script
        choices = en.stderr.split("Choose one of: ", 1)[1].strip()
        self.assertNotRegex(choices, _CJK)
        for token in ("all", "report", "script", "style"):
            self.assertIn(token, choices)
        self.assertFalse((self.out / "skills.json").exists(), "参数报错时不写盘")

    def test_bad_scope_choices_stay_english_when_the_user_typed_chinese(self):
        """渲染后的消息也要守「英文不含中文」：词典 value 干净不等于拼出来干净。
        用户自己打的范围原样回显，那段不算；候选列表必须是当前语言可键入的 token。"""
        r = run_skills(self.out, "--locale", "en", "--always-add", "x=哪里")
        self.assertEqual(r.returncode, 2)
        self.assertIn("I do not recognise the standing-skill scope “哪里”", r.stderr)
        self.assertIn("report (Report)", r.stderr)
        self.assertIn("script (Speaker notes)", r.stderr)
        self.assertNotIn("报告 (Report)", r.stderr, "英文候选里混进了中文存储值")
        self.assertNotIn("讲稿 (Speaker notes)", r.stderr, "英文候选里混进了中文存储值")

    def test_too_many_message_is_localized(self):
        r = run_skills(self.out, "--always", *[f"s{k}" for k in range(20)])
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        over = run_skills(self.out, "--always-add", "one-more")
        self.assertEqual(over.returncode, 2)
        self.assertIn("常驻技能最多 20 个（现在 21 个）", over.stderr)
        en = run_skills(self.out, "--locale", "en", "--always-add", "another")
        self.assertEqual(en.returncode, 2)
        self.assertIn("Standing skills are capped at 20 (you have 21)", en.stderr)
        self.assertNotIn("常驻技能", en.stderr)

    def test_duplicates_are_deduped_and_keep_one_entry(self):
        r = run_skills(self.out, "--always", "dup=all", "dup=all", "dup=报告")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(store.always_skills(self.out),
                         [{"name": "dup", "scope": "all"}, {"name": "dup", "scope": "报告"}])
        listed = run_skills(self.out, "--locale", "en")
        self.assertEqual(listed.stdout.count("dup"), 2, "同名不同范围各留一项，重复项只算一次")

    def test_lenient_read_strict_write_is_kept(self):
        (self.out / "skills.json").write_text(json.dumps(
            {"skills": None, "always": ["humanizer-zh", {"name": "dataviz", "scope": "报告"}, 3, {"x": 1}]},
            ensure_ascii=False), encoding="utf-8")
        self.assertEqual(store.always_skills(self.out),
                         [{"name": "humanizer-zh", "scope": "all"}, {"name": "dataviz", "scope": "报告"}],
                         "坏项跳过、好项保留，字符串当全部批注")
        r = run_skills(self.out, "--set", "x=y")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        with self.assertRaises(store.NoteError):
            store.always_save(self.out, ["humanizer-zh"])


class AgentSnapshotTests(unittest.TestCase):
    """页面、CLI 与 Agent 读到的适用技能一致；语言不影响技能快照。"""

    def setUp(self):
        self.out = tmpdir() / "review"
        build_viewer.build(FIXTURE / "report.html", FIXTURE / "script.md", self.out,
                           locale="en", no_scan_skills=True)
        store.add(self.out, {"page": 1, "text": "两侧都改", "target": "报告和讲稿",
                             "skills": ["sample:sample", "润色（中文）"]})
        store.add(self.out, {"page": 2, "text": "整页风格", "target": "报告", "style": {"scope": "page"}})

    def test_both_sided_annotation_gets_both_sides_standing_skills(self):
        run_skills(self.out, "--always", "humanizer-zh=讲稿", "checker=报告", "stylish=style", "every=all")
        always = store.always_skills(self.out)
        both = store.load(self.out)["notes"][0]
        names = [n for n, _ in store.skills_for(both, always)]
        self.assertEqual(names, ["humanizer-zh", "checker", "every", "sample:sample", "润色（中文）"])
        style_note = store.load(self.out)["notes"][1]
        self.assertEqual([n for n, _ in store.skills_for(style_note, always)], ["checker", "stylish", "every"])

    def test_snapshot_is_identical_in_both_languages(self):
        run_skills(self.out, "--always", "humanizer-zh=讲稿", "checker=报告")
        always = store.always_skills(self.out)
        note = store.load(self.out)["notes"][0]
        zh = store.skills_for(note, always)
        self.assertEqual([n for n, _ in zh], ["humanizer-zh", "checker", "sample:sample", "润色（中文）"])
        for locale in i18n.SUPPORTED:
            catalog = i18n.load_catalog(locale)
            label = {s: i18n.translate(catalog, f"viewer.always.scope.{k}")
                     for s, k in (("all", "all"), ("报告", "report"), ("讲稿", "script"), ("style", "style"))}
            self.assertEqual([label[a["scope"]] for a in store.always_for(note, always)],
                             ["讲稿", "报告"] if locale == "zh-CN" else ["Speaker notes", "Report"])
            self.assertEqual([n for n, _ in store.skills_for(note, always)],
                             [n for n, _ in zh], "适用技能名单不随语言变")

    def test_show_preserves_applicable_skills_and_sources_in_both_languages(self):
        run_skills(self.out, "--always", "humanizer-zh=讲稿", "checker=报告", "stylish=style", "every=all",
                   "sample:sample=报告", "sample:sample=讲稿")
        before = (self.out / "notes.json").read_bytes()
        for locale in i18n.SUPPORTED:
            result = subprocess.run(
                [sys.executable, str(SCRIPTS / "notes.py"), "show", str(self.out), "--locale", locale],
                capture_output=True, text=True, timeout=30)
            self.assertEqual(result.returncode, 0, result.stderr)
            # Read the output given to the agent, not only the matching helper's result.
            blocks = re.split(r"\n- `", result.stdout)[1:]
            self.assertEqual(len(blocks), 2)
            for name in ("humanizer-zh", "checker", "every", "sample:sample", "润色（中文）"):
                self.assertIn(name, blocks[0])
            self.assertNotIn("stylish", blocks[0])
            self.assertEqual(blocks[0].count("sample:sample"), 1, "同名多范围及本条指定仍只调用一次")
            for name in ("checker", "stylish", "every", "sample:sample"):
                self.assertIn(name, blocks[1])
            self.assertNotIn("humanizer-zh", blocks[1])
            self.assertNotIn("润色（中文）", blocks[1])
            if locale == "en":
                self.assertIn("Standing skills required by the user", result.stdout)
                self.assertIn("humanizer-zh (standing · Speaker notes)", blocks[0])
                self.assertIn("润色（中文） (specified for this annotation)", blocks[0])
                self.assertIn("call these before handling this annotation", blocks[0])
            else:
                self.assertIn("常驻技能（用户要求", result.stdout)
                self.assertIn("humanizer-zh（常驻·讲稿）", blocks[0])
        self.assertEqual((self.out / "notes.json").read_bytes(), before, "阅读不修改批注与时间戳")

    def test_rebuild_keeps_manual_catalog_and_always(self):
        run_skills(self.out, "--add", "manual=手工说明", "--always", "manual=报告")
        build_viewer.build(FIXTURE / "report.html", FIXTURE / "script.md", self.out,
                           locale="zh-CN", no_scan_skills=True)
        self.assertEqual([x["name"] for x in store.skills_catalog(self.out)], ["manual"])
        self.assertEqual(store.always_skills(self.out), [{"name": "manual", "scope": "报告"}])
        page = (self.out / "index.html").read_text(encoding="utf-8")
        self.assertIn('"manual"', page)

    def test_large_mixed_catalog_survives_all_three_readers(self):
        catalog = [{"name": f"skill-{n:03d}", "desc": f"合成说明 {n}"} for n in range(205)]
        catalog += [{"name": "sample:sample", "desc": "插件技能 plugin skill"},
                    {"name": "混合 mixed", "desc": "中英混合说明 with English words"},
                    {"name": 'odd"<>&', "desc": "特殊字符 <b>&amp;</b>"}]
        (self.out / "skills.json").write_text(json.dumps(
            {"version": 1, "skills": catalog, "always": []}, ensure_ascii=False), encoding="utf-8")
        build_viewer.build(FIXTURE / "report.html", FIXTURE / "script.md", self.out,
                           locale="en", no_scan_skills=True)
        page = (self.out / "index.html").read_text(encoding="utf-8")
        for item in catalog:
            self.assertIn(item["name"].replace('"', '\\"'), page, f"页面丢了候选 {item['name']}")
        self.assertEqual(len(store.skills_catalog(self.out)), len(catalog), "服务侧目录不截断")
        r = run_skills(self.out, "--locale", "en")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        for item in catalog[-3:]:
            self.assertIn(item["name"], r.stdout, f"命令行输出丢了候选 {item['name']}")
        run_skills(self.out, "--always", *[x["name"] for x in catalog[:20]])
        self.assertEqual(len(store.always_skills(self.out)), 20)
        store.handoff_submit(self.out, "")
        self.assertEqual(len(store.handoff_load(self.out)["items"][-1]["alwaysSkills"]), 20,
                         "提交时记下当时的常驻技能")


class AlwaysApiTests(unittest.TestCase):
    """/api/skills/always 的既有错误码契约不因本地化而变。"""

    def setUp(self):
        self.out = tmpdir() / "review"
        build_viewer.build(FIXTURE / "report.html", FIXTURE / "script.md", self.out, no_scan_skills=True)
        srv = serve.ReviewServer(self.out, 0, log_to_file=False)
        self.addCleanup(srv.shutdown)
        self.addCleanup(srv.server_close)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        self.port = srv.port
        self.headers = {"Host": f"127.0.0.1:{srv.port}", "Origin": f"http://127.0.0.1:{srv.port}",
                        "Content-Type": "application/json"}

    def post(self, body):
        return serve._req(self.port, "POST", "/api/skills/always", self.headers, body)

    def test_over_limit_keeps_the_existing_note_error_code(self):
        """超限走既有的 bad-note，原文不变：调用方按状态码分支，不靠新代码。"""
        status, _, raw = self.post({"always": [{"name": f"s{k}", "scope": "all"} for k in range(21)]})
        self.assertEqual(status, 400)
        self.assertEqual(json.loads(raw)["code"], "bad-note")
        self.assertEqual(json.loads(raw)["error"], "常驻技能最多 20 个（现在 21 个）")

    def test_non_list_keeps_the_existing_note_error_code(self):
        status, _, raw = self.post({"always": "humanizer-zh"})
        self.assertEqual((status, json.loads(raw)["code"]), (400, "bad-note"))

    def test_missing_key_keeps_the_existing_bad_request_code(self):
        status, _, raw = self.post({})
        self.assertEqual((status, json.loads(raw)["code"]), (400, "bad-request"))

    def test_unknown_scope_is_lenient_on_read_and_kept_as_all(self):
        status, _, raw = self.post({"always": [{"name": "x", "scope": "奇怪"}]})
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(raw)["alwaysSkills"], [{"name": "x", "scope": "all"}])


class BuildWarningTests(unittest.TestCase):
    """技能来源不可访问时，生成侧给出当前语言的提示，并保留此前的记录。"""

    def setUp(self):
        self.root = tmpdir()
        self.home = self.root / "home"
        self.home.mkdir()
        self.sources = self.root / "sources"
        (self.sources / "writer").mkdir(parents=True)
        (self.sources / "writer" / "SKILL.md").write_text(
            "---\nname: writer\ndescription: 合成说明\n---\n", encoding="utf-8")
        self.review = self.root / "review"
        self.env = {**os.environ, "HOME": str(self.home), "AE_HOME": str(self.root / "ledger"),
                    "CODEX_HOME": str(self.home / ".codex"),
                    "CLAUDE_SKILLS_DIR": str(self.home / ".claude" / "skills")}

    def build(self, *extra: str) -> subprocess.CompletedProcess:
        return subprocess.run([sys.executable, str(SCRIPTS / "build_viewer.py"), *extra],
                              capture_output=True, text=True, env=self.env, timeout=60)

    def test_unavailable_source_warns_in_the_current_language(self):
        first = self.build("--report", str(FIXTURE / "report.html"), "--out", str(self.review),
                           "--skills-from", str(self.sources))
        self.assertEqual(first.returncode, 0, first.stdout + first.stderr)
        self.sources.rename(self.root / "gone")
        zh = self.build("--rebuild", str(self.review))
        self.assertEqual(zh.returncode, 0, zh.stdout + zh.stderr)
        self.assertIn("有技能来源不可访问，保留此前该来源的记录", zh.stderr)
        self.assertEqual([x["name"] for x in store.skills_catalog(self.review)], ["writer"],
                         "来源不可访问时保留此前的记录")
        en = self.build("--rebuild", str(self.review), "--locale", "en")
        self.assertEqual(en.returncode, 0, en.stdout + en.stderr)
        self.assertIn("A skill source is not reachable; keeping its previous entries", en.stderr)
        self.assertNotIn("有技能来源不可访问", en.stderr)
        self.assertEqual([x["name"] for x in store.skills_catalog(self.review)], ["writer"])


if __name__ == "__main__":
    unittest.main()
