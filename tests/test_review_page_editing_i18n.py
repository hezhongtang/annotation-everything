"""#30 的页面侧约束：编辑、标注与页面骨架走共享词典，存储值和快捷修改说法不变。

只检查契约，不绑定 DOM 拼法：渲染路径必须从词典取消息，存储值、协议标记和
属于其他子 issue 的界面（#31 技能、#32 提交与批次、#35 导出入）保持原样。
真实行为由 tests/e2e_browser.py 的浏览器回归覆盖。
"""
from __future__ import annotations

import re
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "skill" / "annotation-everything" / "scripts"
TEMPLATE = SCRIPTS / "viewer_template.html"
sys.path.insert(0, str(SCRIPTS))

import i18n  # noqa: E402

_CJK = re.compile(r"[一-鿿]")

# #30 覆盖的界面消息：key → 英文是否按计数分复数。
# 批注卡片、页面骨架、标注与风格、选中与引用、帮助与离线提示归 #30；
# 技能选择（#31）、交给 Agent 与处理记录（#32）、导出入（#35）不在内。
EDITING_KEYS = {
    # 卡片动作与确认
    "viewer.resolve": False,
    "viewer.reopen": False,
    "viewer.finalize": False,
    "viewer.returnForRedo": False,
    "viewer.finalizeTitle": False,
    "viewer.remark": False,
    "viewer.remarkAgain": False,
    "viewer.remarkTitle": False,
    "viewer.unpin": False,
    "viewer.add": False,
    "viewer.quote": False,
    "viewer.quoteLine": False,
    "viewer.quoteFrom": False,
    # 列表与已处理
    "viewer.pageNotes": False,
    "viewer.allResolved": False,
    "viewer.hideResolved": True,
    "viewer.showResolved": True,
    # 卡片上的状态 chip（批次语义由 #32 在弹窗里展开）
    "viewer.chip.resolved": False,
    "viewer.chip.drafted": False,
    "viewer.chip.inFlight": False,
    "viewer.chip.inFlightTitle": False,
    # 编辑表单
    "viewer.moveTo": False,
    "viewer.anchorLabel": False,
    "viewer.orphanWarning": False,
    # 快捷修改的显示（存储说法不变）
    "viewer.quickDelete": False,
    "viewer.quickReplace": False,
    # 引用与双侧同步
    "viewer.pairCancel": False,
    "viewer.pairSame": False,
    "viewer.pairSimilar": False,
    "viewer.pairLabel": False,
    "viewer.pairLine": False,
    "viewer.pairEcho": False,
    # 选中文字后的操作条
    "viewer.sel.delete": False,
    "viewer.sel.replace": False,
    "viewer.sel.comment": False,
    "viewer.sel.deleteTitle": False,
    "viewer.sel.replaceTitle": False,
    "viewer.sel.commentTitle": False,
    "viewer.sel.confirmDelete": False,
    "viewer.pairTitle": False,
    # 在报告上标注
    "viewer.markPin": False,
    "viewer.markPinTitle": False,
    "viewer.markHint": False,
    "viewer.markRehint": False,
    "viewer.markToast": False,
    "viewer.markRemarkToast": False,
    "viewer.markNewRegion": False,
    "viewer.markNewPin": False,
    "viewer.markRemoveRegion": False,
    "viewer.markRemovePin": False,
    "viewer.markKind.region": False,
    "viewer.markKind.pin": False,
    # 风格优化
    "viewer.styleBtn": False,
    "viewer.styleBtnTitle": False,
    "viewer.styleScope": False,
    "viewer.stylePage": False,
    "viewer.styleMark": False,
    "viewer.style.page": False,
    "viewer.style.region": False,
    "viewer.style.pin": False,
    "viewer.styleChip": False,
    "viewer.styleCancel": False,
    "viewer.styleCancelAria": False,
    "viewer.stylePlaceholder": False,
    # 标注弹窗
    "viewer.popClose": False,
    "viewer.popUnset": False,
    "viewer.popResolved": False,
    # 页码与讲稿栏
    "viewer.pageStat": False,
    "viewer.timeStat": False,
    "viewer.scriptTalk": False,
    "viewer.scriptNext": False,
    "viewer.scriptQA": False,
    "viewer.scriptEmpty": False,
    "viewer.jumpLabel": False,
    "viewer.jumpNotes": True,
    "viewer.jumpInFlight": True,
    "viewer.jumpDraft": True,
    "viewer.count.open": False,
    "viewer.count.inFlight": False,
    "viewer.count.draft": False,
    # 页头
    "viewer.prev": False,
    "viewer.prevTitle": False,
    "viewer.next": False,
    "viewer.nextTitle": False,
    "viewer.help": False,
    "viewer.helpTitle": False,
    "viewer.reload": False,
    "viewer.reloadTitle": False,
    "viewer.legacy": False,
    "viewer.legacyTitle": False,
    "viewer.stray": False,
    "viewer.strayTitle": False,
    "viewer.seeList": False,
    "viewer.seeListTitle": False,
    # 改写对话框
    "viewer.repTitle": False,
    "viewer.repOk": False,
    # 说明
    "viewer.introDefault": False,
    # 断线与补传
    "viewer.offline.stashed": False,
    "viewer.offline.newStashed": False,
    "viewer.offline.retried": False,
    "viewer.offline.reconnected": False,
    "viewer.offline.noServer": False,
    # 连接状态徽标（服务不匹配、旧服务等由 #32 展开）
    "viewer.mode.offline": False,
    "viewer.mode.file": False,
    "viewer.mode.offlineTitle": False,
}

# 语言自己的名字按它自己的文字写，不算未翻译
LANGUAGE_NAMES = ("locale.name.zh-CN", "locale.name.en")

# 存储值与协议标记：只改显示，不改写进 notes.json 的值
STORAGE_VALUES = ("报告", "讲稿", "报告和讲稿")
QUICK_MARKERS = ("删除引用的这段文字", "改为：")

# 散落在模板里、属于其他子 issue 的中文。#30 不动它们，只保证自己范围内没有漏网之鱼。
OTHER_ISSUE_LITERALS = (
    "技能", "常驻技能", "可选，如 humanizer-zh",          # #31
    "交给 Agent", "处理记录", "全部批注", "导入…", "清空",  # #32 / #35
    "复制全部（粘贴给 Agent）", "下载 Markdown", "下载 JSON",
    "粘贴之前「复制全部」的文字", "在这里粘贴批注文字或 JSON",
    "这批做到：",
    "允许派发子代理", "全量勾稽（检查全文前后一致性）",
    "背后的本地服务是旧版本", "本地服务是旧版本，不支持全量勾稽",
    "初改说明", "处理说明",
)


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


def _source() -> str:
    return TEMPLATE.read_text(encoding="utf-8")


class CatalogTests(unittest.TestCase):
    """#30 要用的消息在两种语言里都齐，英文不含中文。"""

    def test_every_editing_key_exists_in_both_catalogs(self):
        for tag in i18n.SUPPORTED:
            messages = i18n.load_catalog(tag)["messages"]
            missing = sorted(k for k in EDITING_KEYS if k not in messages)
            self.assertEqual(missing, [], f"{tag} 缺少消息")

    def test_english_values_carry_no_chinese(self):
        en = i18n.load_catalog("en")["messages"]
        chinese = sorted(k for k, v in en.items()
                         if _CJK.search(v) and k not in LANGUAGE_NAMES)
        self.assertEqual(chinese, [], "英文消息里混了中文（语言自身的名字除外）")

    def test_counted_english_messages_have_both_plural_branches(self):
        en = i18n.load_catalog("en")["messages"]
        for key, counted in EDITING_KEYS.items():
            if not counted:
                continue
            for suffix in ("one", "other"):
                self.assertIn(f"{key}.{suffix}", en, f"英文计数消息缺 {key}.{suffix}")
            for count in (0, 1, 2):
                text = i18n.translate(i18n.load_catalog("en"), key, count=count)
                self.assertIn(str(count), text, f"{key} 在 count={count} 时没换上数字")

    def test_chinese_counted_messages_have_no_plural_branches(self):
        zh = i18n.load_catalog("zh-CN")["messages"]
        branched = sorted(k for k in EDITING_KEYS if EDITING_KEYS[k] and f"{k}.one" in zh)
        self.assertEqual(branched, [], "中文不该有英文式复数分支")

    def test_catalogs_stay_self_consistent(self):
        self.assertEqual(i18n.check_catalogs(), [])

    def test_every_editing_key_is_wired_into_the_page(self):
        builder = (ROOT / "skill" / "annotation-everything" / "scripts" / "build_viewer.py").read_text(encoding="utf-8")
        page = _source() + builder
        unwired = sorted(k for k in EDITING_KEYS
                         if f'tMsg("{k}"' not in page and f'data-msg="{k}"' not in page
                         and f'data-title="{k}"' not in page and f'data-aria="{k}"' not in page
                         and f'data-ph="{k}"' not in page and f'"{k}"' not in page)
        self.assertEqual(unwired, [], "这些消息加了却没有任何地方用")


class CardActionTests(unittest.TestCase):
    """批注卡片的动作、确认与状态 chip 从词典取，中文不再写死在渲染路径里。"""

    def setUp(self):
        self.note = _between(_source(), "function noteHtml(", "// 把正在编辑的那张卡片")
        self.list = _between(_source(), "function refreshList(", "// 「批注」按钮")

    def test_actions_read_from_the_catalog(self):
        for key in ("viewer.save", "viewer.edit", "viewer.delete", "viewer.cancel",
                    "viewer.finalize", "viewer.returnForRedo",
                    "viewer.unpin", "viewer.quickDelete"):
            self.assertTrue(_uses(self.note, key), f"卡片动作没走词典：{key}")
        # 标注按钮与 resolve / reopen 按状态二选一，是计算 key
        for key in ("viewer.resolve", "viewer.reopen", "viewer.remark", "viewer.remarkAgain",
                    "viewer.quickReplace"):
            self.assertIn(f'"{key}"', self.note, f"卡片动作没走词典：{key}")

    def test_delete_confirmation_reads_from_the_catalog(self):
        source = _source()
        self.assertNotIn('confirm("删除这条批注？")', source, "删除确认还写死了中文")
        self.assertEqual(source.count('confirm(tMsg("viewer.confirmDelete"))'), 2,
                         "卡片和标注弹窗两处删除确认都要走词典")

    def test_action_titles_read_from_the_catalog(self):
        for key in ("viewer.finalizeTitle", "viewer.remarkTitle"):
            self.assertTrue(_uses(self.note, key), key)

    def test_status_chips_read_from_the_catalog(self):
        for key in ("viewer.chip.resolved", "viewer.chip.drafted", "viewer.chip.inFlight"):
            self.assertTrue(_uses(self.note, key), key)

    def test_no_hardcoded_action_labels(self):
        for literal in (">保存<", ">编辑<", ">删除<", ">取消<", ">重新打开<",
                        ">标为已处理<", ">定稿<", ">退回重改<", ">去掉标注<",
                        ">重新标注<", ">在报告上标注<"):
            self.assertNotIn(literal, self.note, f"卡片里还写死了 {literal}")

    def test_no_hardcoded_chip_labels(self):
        for literal in ("已处理", "已初改·待复核", "处理中", "· 删除", "· 改写",
                        "· 风格优化", "·整页"):
            self.assertNotIn(literal, self.note, f"卡片里还写死了 {literal}")

    def test_list_labels_read_from_the_catalog(self):
        for key in ("viewer.pageNotes", "viewer.allResolved", "viewer.empty"):
            self.assertTrue(_uses(self.list, key), key)
        for key in ("viewer.hideResolved", "viewer.showResolved"):
            self.assertIn(f'"{key}"', self.list, f"已处理的显隐没走词典：{key}")
            self.assertIn('{count:nDone}', self.list, f"{key} 要带上条数")
        for literal in ("本页批注", "全部已处理", "暂无批注。", "隐藏已处理（", "显示已处理（"):
            self.assertNotIn(literal, self.list, f"列表里还写死了 {literal}")

    def test_editing_form_reads_from_the_catalog(self):
        for key in ("viewer.moveTo", "viewer.page", "viewer.anchorLabel",
                    "viewer.orphanWarning"):
            self.assertTrue(_uses(self.note, key), key)
        for literal in ("移到 ", "位置：", "原来所在的页面在新版报告里找不到",
                        "第 ", " 页</option>"):
            self.assertNotIn(literal, self.note, f"编辑表单里还写死了 {literal}")

    def test_status_chip_titles_read_from_the_catalog(self):
        self.assertIn('tMsg("viewer.chip.inFlightTitle")', self.note)
        self.assertNotIn("已交给 Agent，正在处理", self.note)

    def test_storage_values_survive_in_the_render_path(self):
        compares = (_between(_source(), "function targetLabel(", "function msgParams(")
                    + _between(_source(), "const alwaysFor=", "const parseSkills="))
        for value in STORAGE_VALUES:
            self.assertIn(f'"{value}"', compares, f"存储值 {value} 不能从比较路径里消失")
        self.assertIn("targetLabel(x.target)", self.note)


class SelectionAndPairTests(unittest.TestCase):
    """选中文字后的操作条、改写对话框和双侧同步提示。"""

    def setUp(self):
        self.source = _source()

    def test_selection_buttons_read_from_the_catalog(self):
        bar = _between(self.source, '<div id="selbar">', '<div id="pinpop"')
        for key in ("viewer.sel.delete", "viewer.sel.replace", "viewer.sel.comment"):
            self.assertTrue(_uses(bar, key), key)
        for literal in ('title="删除选中的文字（D）"', 'title="把选中的文字改为…（R）"',
                        'title="以选中文字为引用写批注（C）"'):
            self.assertNotIn(literal, bar, f"操作条里还写死了 {literal}")

    def test_confirm_delete_prompt_reads_from_the_catalog(self):
        arm = _between(self.source, "function armDelete(", "function ")
        self.assertTrue(_uses(arm, "viewer.sel.confirmDelete"), "viewer.sel.confirmDelete")
        self.assertNotIn("确认删除？再按 D 或 ↵", arm)

    def test_pair_row_reads_from_the_catalog(self):
        row = _between(self.source, '<label id="pairRow"', "</label>")
        self.assertTrue(_uses(row, "viewer.pairLabel"), "viewer.pairLabel")
        self.assertTrue(_uses(row, "viewer.pairTitle"), "viewer.pairTitle")
        self.assertNotIn("另一侧同一页有相同或相近的句子", row)

    def test_quote_chip_and_pair_display_read_from_the_catalog(self):
        quote = _between(self.source, "function showQuote(", "function setQuote(")
        for key in ("viewer.quoteLine", "viewer.pairCancel", "viewer.pairEcho"):
            self.assertTrue(_uses(quote, key), key)
        for literal in ("取消</a>", "对应：", "（同时改）", "）："):
            self.assertNotIn(literal, quote, f"引用条里还写死了 {literal}")
        self.assertIn("targetLabel(quotePair.side)", quote, "引用条的侧名要过显示映射")
        note = _between(self.source, "function noteHtml(", "// 把正在编辑的那张卡片")
        for key in ("viewer.pairSame", "viewer.pairSimilar"):
            self.assertIn(f'"{key}"', note, f"卡片里还写死了对应句的措辞：{key}")
        for literal in ("（同一句）", "（相近）"):
            self.assertNotIn(literal, note, f"卡片里还写死了 {literal}")
        self.assertIn('tMsg("viewer.pairLine"', note, "对应句要整句进词典")
        self.assertIn("targetLabel(x.pair.side)", note, "对应句的侧名要过显示映射")

    def test_rewrite_dialog_reads_from_the_catalog(self):
        box = _between(self.source, '<div id="repbox">', "</main>")
        for key in ("viewer.repTitle", "viewer.repOk", "viewer.cancel"):
            self.assertTrue(_uses(box, key), key)
        self.assertNotIn('<p class="hint">把选中的文字改为：</p>', box,
                         "改写框的标题没挂 data-msg")

    def test_blockquote_prefix_reads_from_the_catalog(self):
        note = _between(_source(), "function noteHtml(", "// 把正在编辑的那张卡片")
        self.assertTrue(_uses(note, "viewer.quoteFrom"), "viewer.quoteFrom")
        self.assertNotIn('+"："', note, "引用来源的分隔符写死在标记里")

    def test_style_fallback_stays_a_protocol_value(self):
        """没写方向时的正文是导出/导入据此还原的协议值，固定中文，不随界面语言改写。"""
        source = _source()
        self.assertIn('text="风格优化："+STYLE_EXPORT[scope]', source)
        for tag in i18n.SUPPORTED:
            self.assertNotIn("viewer.styleFallback", i18n.load_catalog(tag)["messages"],
                             f"{tag} 不该再用显示文案生成存储正文")

    def test_quick_edit_markers_stay_untouched(self):
        for marker in QUICK_MARKERS:
            self.assertIn(f'"{marker}"', self.source, f"协议标记 {marker} 不能改")
        self.assertNotIn('DEL_TEXT=tMsg', self.source)
        self.assertNotIn('REP_PREFIX=tMsg', self.source)


class MarkAndStyleTests(unittest.TestCase):
    """在报告上标注与风格优化的范围语义。"""

    def setUp(self):
        self.source = _source()

    def test_mark_button_reads_from_the_catalog(self):
        pin = _between(self.source, '<button data-act="pin"', "</button>")
        self.assertTrue(_uses(pin, "viewer.markPin"), "viewer.markPin")
        self.assertTrue(_uses(pin, "viewer.markPinTitle"), "viewer.markPinTitle")
        self.assertNotIn("在报告上标注：单击放一个点，拖动框选一块区域（P）", pin)

    def test_mark_mode_prompts_read_from_the_catalog(self):
        mode = _between(self.source, "function setMarkMode(", "function showQuote(")
        for key in ("viewer.markToast", "viewer.markRemarkToast"):
            self.assertTrue(_uses(mode, key), key)
        # 按钮标签的刷写抽成了 markBtnLabel，applyLocale 也复用它
        label = _between(self.source, "function markBtnLabel(", "function setMarkMode(")
        for key in ("viewer.markPin", "viewer.markHint", "viewer.markRehint"):
            self.assertTrue(_uses(label, key), key)
        self.assertIn("markBtnLabel()", mode, "setMarkMode 要复用标签刷写")
        for literal in ("单击放点，拖动框选…", "重新标注…", "在报告上标注",
                        "在左侧报告上单击放一个点，或按住拖动框选一块区域（Esc 取消）",
                        "给这条批注标注：在左侧报告上单击放点，或拖动框选（Esc 取消）"):
            self.assertNotIn(literal, mode, f"标注提示里还写死了 {literal}")

    def test_mark_kind_labels_read_from_the_catalog(self):
        note = _between(self.source, "function noteHtml(", "// 把正在编辑的那张卡片")
        for key in ("viewer.markKind.region", "viewer.markKind.pin"):
            self.assertIn(f'"{key}"', note, f"标注种类没走词典：{key}")
        self.assertNotIn('regionOf(x)?"框选":"标点"', self.source)

    def test_style_button_and_menu_read_from_the_catalog(self):
        btn = _between(self.source, '<button data-act="style"', "</button>")
        self.assertTrue(_uses(btn, "viewer.styleBtn"), "viewer.styleBtn")
        self.assertTrue(_uses(btn, "viewer.styleBtnTitle"), "viewer.styleBtnTitle")
        self.assertNotIn("风格优化：整页 / 标注处（S）", btn)

        menu = _between(self.source, '<div id="stylemenu"', '<div id="repbox">')
        for key in ("viewer.styleScope", "viewer.stylePage", "viewer.styleMark"):
            self.assertTrue(_uses(menu, key), key)
        for literal in ('<p class="hint">风格优化的范围：</p>',
                        '<button data-sm="page">整页（当前这一页）<kbd>1</kbd></button>',
                        '<button data-sm="mark">标注处（单击放点或拖动框选）<kbd>2</kbd></button>'):
            self.assertNotIn(literal, menu, f"风格菜单里还写死了 {literal}")

    def test_style_scope_labels_read_from_the_catalog(self):
        for key in ("viewer.style.page", "viewer.style.region", "viewer.style.pin"):
            self.assertIn(f'"{key}"', self.source, f"风格范围标签没走词典：{key}")
        self.assertNotIn('const STYLE_ZH={page:"整页",region:"框选处",pin:"标点处"}', self.source)
        self.assertIn('const STYLE_LABEL', self.source, "风格范围标签要走映射表")
        self.assertIn('const STYLE_EXPORT={page:"整页",region:"框选处",pin:"标点处"}', self.source,
                      "导出的风格写法是协议值，必须固定中文")

    def test_style_chip_read_from_the_catalog(self):
        chip = _between(self.source, "function updateStyleChip(", "function describe(")
        for key in ("viewer.styleChip", "viewer.styleCancel", "viewer.styleCancelAria",
                    "viewer.stylePlaceholder"):
            self.assertTrue(_uses(chip, key), key)
        for literal in ("风格优化 · ", "取消风格优化（标注保留）",
                        "想要的风格方向（可不写）：例如更简洁、对齐、配色统一、突出重点……"):
            self.assertNotIn(literal, chip, f"风格条里还写死了 {literal}")

    def test_style_scope_suffix_on_cards_read_from_the_catalog(self):
        note = _between(self.source, "function noteHtml(", "// 把正在编辑的那张卡片")
        self.assertTrue(_uses(note, "viewer.styleBtn"), "viewer.styleBtn")
        self.assertIn("styleLabel(x.style.scope)", note, "卡片上的风格范围要走映射表")
        for literal in (" · 风格优化", "·整页"):
            self.assertNotIn(literal, note, f"卡片里还写死了 {literal}")

    def test_pin_popup_read_from_the_catalog(self):
        pop = _between(self.source, "function openPinPop(", '$("pinpop").addEventListener')
        for key in ("viewer.popClose", "viewer.popUnset", "viewer.popResolved",
                    "viewer.markNewRegion", "viewer.markNewPin",
                    "viewer.markRemoveRegion", "viewer.markRemovePin",
                    "viewer.popResolved"):
            self.assertTrue(_uses(pop, key), key)
        self.assertIn("targetLabel(x.target)", pop, "弹窗里的对象名走显示映射")
        for literal in ("新框选的区域：写好意见后点「添加批注」。拖动可移动，拖右下角可调大小。",
                        "新标点：写好意见后点「添加批注」。拖动可调整位置。",
                        "移除这个框", "移除标点", ">关闭<", "未指定", "· 已处理"):
            self.assertNotIn(literal, pop, f"标注弹窗里还写死了 {literal}")


class PageAndScriptTests(unittest.TestCase):
    """页码、统计行、讲稿栏与问答标签。"""

    def setUp(self):
        self.source = _source()
        self.render = _between(self.source, "function render()", "function go(k)")

    def test_page_stat_reads_from_the_catalog(self):
        for key in ("viewer.pageStat", "viewer.timeStat"):
            self.assertTrue(_uses(self.render, key), key)
        for literal in ("第 ", " 页", "讲到本页结束约 ", " / 合计 "):
            self.assertNotIn(literal, self.render, f"统计行里还写死了 {literal}")

    def test_script_column_labels_read_from_the_catalog(self):
        for key in ("viewer.page", "viewer.scriptTalk", "viewer.scriptNext",
                    "viewer.scriptQA", "viewer.scriptEmpty"):
            self.assertTrue(_uses(self.render, key), key)
        for literal in ("讲法", "过渡", "可能被问", "（本页无讲稿）", " 页</span>"):
            self.assertNotIn(literal, self.render, f"讲稿栏里还写死了 {literal}")

    def test_jump_dropdown_reads_from_the_catalog(self):
        counts = _between(self.source, "function updateCounts(", "function setMarkMode(")
        for key in ("viewer.page", "viewer.jumpNotes", "viewer.jumpInFlight",
                    "viewer.jumpDraft"):
            self.assertTrue(_uses(counts, key), key)
        for literal in ("第 ", " 页", "批注 ", "处理中 ", "待复核 "):
            self.assertNotIn(literal, counts, f"页码下拉里还写死了 {literal}")

    def test_count_badges_read_from_the_catalog(self):
        counts = _between(self.source, "function updateCounts(", "function setMarkMode(")
        for key in ("viewer.count.open", "viewer.count.inFlight", "viewer.count.draft"):
            self.assertTrue(_uses(counts, key), key)
        for literal in ("未处理（还没交给 Agent）", "已交给 Agent、正在处理的批注",
                        "已初改、等你看过后请求复核或定稿的批注"):
            self.assertNotIn(literal, counts, f"角标提示里还写死了 {literal}")

    def test_jump_aria_label_reads_from_the_catalog(self):
        self.assertTrue(_uses(self.source, "viewer.jumpLabel"), "viewer.jumpLabel")
        self.assertNotIn('aria-label="跳到页"', self.source)


class HeaderAndHelpTests(unittest.TestCase):
    """页头控件、说明与默认简介。"""

    def setUp(self):
        self.source = _source()

    def test_header_controls_read_from_the_catalog(self):
        header = _between(self.source, "<header>", "</header>")
        for key in ("viewer.prev", "viewer.prevTitle", "viewer.next", "viewer.nextTitle",
                    "viewer.help", "viewer.helpTitle", "viewer.reload",
                    "viewer.reloadTitle", "viewer.legacy", "viewer.legacyTitle",
                    "viewer.stray", "viewer.strayTitle"):
            self.assertTrue(_uses(header, key), key)
        # 界面语言的名字由 #29 的 applyLocale 显式设置，这里只确认静态标记仍是中文初始值
        self.assertIn("界面语言", header)
        self.assertIn('tMsg("locale.switcher")',
                      _between(self.source, "function applyLocale()", '$("localePick").addEventListener'))
        for literal in ('aria-label="跳到页"', 'title="上一页（←）"', 'title="下一页（→）"',
                        'title="使用说明与快捷键（H）"', 'title="Agent 已按批注重建对照页"',
                        'title="浏览器里发现这份稿件之前版本的批注"',
                        'title="这个地址下还暂存着别的审阅没保存的改动（旧标签页留下的），可下载后导回那份审阅"'):
            self.assertNotIn(literal, header, f"页头里还写死了 {literal}")

    def test_see_list_button_reads_from_the_catalog(self):
        form = _between(self.source, '<div class="form" id="nform">', '<div id="selbar">')
        for key in ("viewer.markPinTitle", "viewer.styleBtnTitle", "viewer.seeList",
                    "viewer.seeListTitle"):
            self.assertTrue(_uses(form, key), key)
        for literal in ('title="在报告上标注：单击放一个点，拖动框选一块区域（P）"',
                        'title="风格优化：整页 / 标注处（S）"', 'title="看本页已有的批注"'):
            self.assertNotIn(literal, form, f"输入栏里还写死了 {literal}")

    def test_help_body_has_both_languages(self):
        help_box = _between(self.source, '<div class="modal" id="help">', "<script>")
        self.assertIn('data-ae-lang="zh-CN"', help_box)
        self.assertIn('data-ae-lang="en"', help_box)
        self.assertIn("快捷键：", help_box, "中文说明不能丢")
        self.assertIn("Shortcuts:", help_box, "英文说明要在")

    def test_default_intro_reads_from_the_catalog(self):
        builder = (ROOT / "skill" / "annotation-everything" / "scripts" / "build_viewer.py").read_text(encoding="utf-8")
        self.assertIn('data-msg="viewer.introDefault"', builder, "默认简介要挂 data-msg，切语言时才会重写")
        self.assertNotIn("左侧报告、右侧讲稿与批注。选中文字可作为引用", _source())
        self.assertNotIn("左侧报告、右侧讲稿与批注。选中文字可作为引用", builder)


class OfflineHintTests(unittest.TestCase):
    """断线、暂存与补传的提示用当前语言，不把未确认的提交说成已保存。"""

    def setUp(self):
        self.source = _source()

    def test_offline_hints_read_from_the_catalog(self):
        for key in ("viewer.offline.stashed", "viewer.offline.newStashed",
                    "viewer.offline.retried", "viewer.offline.reconnected",
                    "viewer.offline.noServer", "viewer.mode.offline",
                    "viewer.mode.file", "viewer.mode.offlineTitle"):
            self.assertTrue(_uses(self.source, key), key)
        for literal in ("本地服务断开：改动暂存浏览器，恢复后自动补传",
                        "本地服务断开：新改动暂存浏览器",
                        "已补传暂存的改动", "已重新连上本地服务",
                        "没连上本地服务：新批注暂存浏览器。确认 serve.py 在运行",
                        "新改动暂存在本浏览器；重新运行 serve.py 后会自动补传"):
            self.assertNotIn(literal, self.source, f"离线提示里还写死了 {literal}")

    def test_offline_mode_badge_reads_from_the_catalog(self):
        badge = _between(self.source, "function updateMode(", "function foundSets(")
        self.assertTrue(_uses(badge, "viewer.mode.offline"), "viewer.mode.offline")
        self.assertTrue(_uses(badge, "viewer.mode.file"), "viewer.mode.file")
        self.assertTrue(_uses(badge, "viewer.mode.offlineTitle"), "viewer.mode.offlineTitle")
        for literal in ("● 服务未连接", "○ 本地文件模式"):
            self.assertNotIn(literal, badge, f"状态徽标里还写死了 {literal}")

    def test_offline_hints_never_claim_a_save_that_did_not_happen(self):
        for literal in ("已保存", "已安全保存", "保存成功"):
            self.assertNotIn(literal, _between(self.source, "function flush(", "function "))

    def test_no_editing_message_is_hardcoded_in_an_attribute(self):
        zh = i18n.load_catalog("zh-CN")["messages"]
        allowed = {"viewer.textPlaceholder"}   # #ntext 的初始占位符，applyLocale 每次都会重写
        for key in sorted(EDITING_KEYS):
            value = zh.get(key, "")
            if key in allowed or not value or not _CJK.search(value):
                continue
            for attr in ("title", "aria-label", "placeholder"):
                self.assertNotIn(f'{attr}="{value}"', self.source,
                                 f"{key} 的中文还写死在 {attr} 里")

    def test_every_constant_table_used_is_declared(self):
        """改名后漏改引用只是运行时的 ReferenceError，端到端才暴露，太贵；这里提前查。"""
        source = _source()
        declared: set[str] = set()
        for stmt in re.findall(r"\b(?:const|let|var)\s+([^;]+);", source):
            for part in stmt.split(","):
                head = re.match(r"\s*([A-Za-z_$][A-Za-z0-9_$]*)", part)
                if head:
                    declared.add(head.group(1))
        declared |= set(re.findall(r"\bfunction\s+([A-Za-z_$][A-Za-z0-9_$]*)", source))
        declared |= set(re.findall(r"__([A-Z_]+)__", source))
        used = set(re.findall(r"\b([A-Z][A-Z0-9_]{2,})\b", source))
        host = {"JSON", "CSS", "URL", "DOM", "HTML", "HTTP", "GET", "POST", "PATCH",
                "DELETE", "SELECT", "INPUT", "TEXTAREA", "CJK", "CSP", "ROOTFS"}
        self.assertEqual(sorted(used - declared - host), [],
                         "页面里用了没声明的常量，跑起来会是 ReferenceError")

    def test_locale_switch_refreshes_in_flight_controls(self):
        """风格优化、标注模式、删除待确认和连接徽标进行中切语言，都要按当前语言重刷。"""
        apply = _between(self.source, "function applyLocale()", '$("localePick").addEventListener')
        for call in ("markBtnLabel()", "armDelete(delArmed)", "updateStyleChip()", "updateMode()"):
            self.assertIn(call, apply, f"切语言时没有重刷 {call}")
        self.assertIn("armDelete(delArmed)", apply,
                      "删除待确认是两步确认，切语言后不能把「再按一次」的提示弄丢")

    def test_static_markup_tags_are_balanced(self):
        """说明正文按语言分成两块后最容易漏 </div>；漏了浏览器会自行补救，但英文块会被
        嵌套进中文块，切语言时一起被藏掉。"""
        body = _source()
        body = body[body.find("<body>"):body.find("<script>")]
        for tag in ("div", "span", "button", "select", "label", "p"):
            opens = len(re.findall(rf"<{tag}\b", body))
            closes = len(re.findall(rf"</{tag}>", body))
            self.assertEqual(opens, closes, f"<{tag}> 开合不平衡（{opens} 开 / {closes} 合）")
        depth = 0
        for m in re.finditer(r"<div\b|</div>", body):
            depth += 1 if m.group(0).startswith("<div") else -1
            self.assertGreaterEqual(depth, 0, "</div> 比 <div> 先出现")
        self.assertEqual(depth, 0, "静态标记的 div 没有全部闭合")

    def test_other_issues_literals_are_left_alone(self):
        """#31 / #32 / #35 的中文仍在，证明 #30 没有越界改掉它们。"""
        for literal in OTHER_ISSUE_LITERALS:
            self.assertIn(literal, self.source, f"不属于 #30 的文案被改掉了：{literal}")


if __name__ == "__main__":
    unittest.main()
