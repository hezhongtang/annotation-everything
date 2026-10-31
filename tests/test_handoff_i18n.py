"""#32 的契约：提交、初改复核与处理记录在中英两种语言下同一含义，旧数据与旧服务照旧能读。

只查契约与 CLI 行为，不绑定 DOM 拼法；真实页面行为由 tests/e2e_browser.py 的
handoff_i18n_checks 覆盖。结构化选项（做到定稿 / 停在待复核、子代理、全量勾稽）是
协议字段，不随显示语言改写；旧固定兼容提示与原始附言保持原文。
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
TEMPLATE = SCRIPTS / "viewer_template.html"
sys.path.insert(0, str(SCRIPTS))

import i18n  # noqa: E402
import notes as notes_cli  # noqa: E402
import notes_store as store  # noqa: E402

_CJK = re.compile(r"[一-鿿]")

# #32 新增的消息：key → 英文是否按计数分复数。
HANDOFF_KEYS = {
    # 提交弹窗
    "viewer.hand.btn": False, "viewer.hand.btnTitle": False, "viewer.hand.cancel": False,
    "viewer.hand.title": False, "viewer.hand.submit": False, "viewer.hand.reviewTitle": False,
    "viewer.hand.requestReview": False, "viewer.hand.targetLabel": False,
    "viewer.hand.targetFinal": False, "viewer.hand.targetFinalTitle": False,
    "viewer.hand.targetReview": False, "viewer.hand.targetReviewTitle": False,
    "viewer.hand.subagents": False, "viewer.hand.subagentsTitle": False,
    "viewer.hand.fullCheck": False, "viewer.hand.fullCheckTitle": False,
    "viewer.hand.fullCheckOld": False, "viewer.hand.oldServer": False,
    "viewer.hand.infoOpen": True, "viewer.hand.infoNone": False, "viewer.hand.infoReview": True,
    "viewer.hand.alwaysLine": False, "viewer.hand.alwaysItem": False, "viewer.hand.listSep": False,
    "viewer.hand.scope.all": False, "viewer.hand.scope.report": False,
    "viewer.hand.scope.script": False, "viewer.hand.scope.style": False,
    "viewer.hand.msgPlaceholder": False, "viewer.hand.reviewMsgPlaceholder": False,
    "viewer.hand.help": False, "viewer.hand.last": False, "viewer.hand.lastCount": False,
    "viewer.hand.lastSummary": False, "viewer.hand.hist": False, "viewer.hand.histTitle": False,
    "viewer.hand.state.queued": False, "viewer.hand.state.queuedTitle": False,
    "viewer.hand.state.pending": False, "viewer.hand.state.claimed": False,
    "viewer.hand.state.claimedResolved": False, "viewer.hand.state.claimedDrafted": False,
    "viewer.hand.state.drafted": False, "viewer.hand.state.review": False,
    "viewer.hand.state.reviewing": False, "viewer.hand.state.done": False,
    "viewer.hand.state.doneCount": False, "viewer.hand.stateTitle.done": False,
    "viewer.hand.stateTitle.none": False, "viewer.hand.stateTitle.note": False,
    "viewer.hand.stateTitle.hint": False, "viewer.hand.drafts.review": False,
    "viewer.hand.drafts.drafted": False, "viewer.hand.drafts.reviewBtn": False,
    "viewer.hand.drafts.finalize": False, "viewer.hand.toast.claimed": False,
    "viewer.hand.toast.drafted": False, "viewer.hand.toast.reviewing": False,
    "viewer.hand.toast.done": False, "viewer.hand.toast.doneSummary": False,
    "viewer.hand.toast.sent": False, "viewer.hand.toast.merged": False,
    "viewer.hand.toast.queuedMismatch": False, "viewer.hand.toast.queuedOffline": False,
    "viewer.hand.toast.legacy": False, "viewer.hand.toast.failed": False,
    "viewer.hand.toast.reviewRequested": False, "viewer.hand.toast.finalized": False,
    "viewer.hand.toast.confirmFinalize": False, "viewer.hand.toast.actionFailed": False,
    "viewer.hand.toast.actionLegacy": False, "viewer.hand.toast.needServer": False,
    "viewer.hand.toast.fullCheckOld": False, "viewer.hand.toast.fullCheckStashed": False,
    "viewer.hand.toast.fullCheckUnconfirmed": False, "viewer.hand.toast.fileMode": False,
    # 处理记录
    "viewer.batch.title": False, "viewer.batch.close": False, "viewer.batch.loading": False,
    "viewer.batch.empty": False, "viewer.batch.sum": True, "viewer.batch.sumCounts": False,
    "viewer.batch.sumMore": False, "viewer.batch.status.pending": False,
    "viewer.batch.status.claimed": False, "viewer.batch.status.drafted": False,
    "viewer.batch.status.review": False, "viewer.batch.status.reviewing": False,
    "viewer.batch.status.done": False, "viewer.batch.stage.draft": False,
    "viewer.batch.stage.review": False, "viewer.batch.stage.single": False,
    "viewer.batch.item": False, "viewer.batch.itemWho": False, "viewer.batch.whoPage": False,
    "viewer.batch.resolved": False, "viewer.batch.resolvedLeft": False,
    "viewer.batch.resolvedLive": False, "viewer.batch.resolvedEstimated": False,
    "viewer.batch.submitted": False, "viewer.batch.submittedPages": False,
    "viewer.batch.submittedClaim": False, "viewer.batch.submittedDone": False,
    "viewer.batch.requirement": False, "viewer.batch.reqReview": False,
    "viewer.batch.reqFinal": False, "viewer.batch.reqSub": False, "viewer.batch.reqNoSub": False,
    "viewer.batch.reqFull": False, "viewer.batch.userMessage": False,
    "viewer.batch.reviewMessage": False, "viewer.batch.summary": False,
    "viewer.batch.noSummary": False, "viewer.batch.stageLine": False,
    "viewer.batch.stageDone": False, "viewer.batch.stageLive": False,
    "viewer.batch.stageDrafted": False, "viewer.batch.stageReviewed": False,
    "viewer.batch.stageSummary": False, "viewer.batch.accept": False,
    "viewer.batch.draftedList": True, "viewer.batch.resolvedList": True,
    "viewer.batch.noteLine": False, "viewer.batch.draftNote": False,
    "viewer.batch.resolutionNote": False, "viewer.batch.reopened": False,
    "viewer.batch.deleted": False, "viewer.batch.fileMode": False,
    "viewer.batch.trailLabel": False, "viewer.batch.trail.entry": False,
    "viewer.batch.trail.batch": False, "viewer.batch.trail.note": False,
    "viewer.batch.trail.whoPage": False, "viewer.batch.trail.action.drafted": False,
    "viewer.batch.trail.action.resolved": False, "viewer.batch.trail.action.finalized": False,
    "viewer.batch.trail.action.approved": False, "viewer.batch.trail.action.revised": False,
    "viewer.batch.trail.action.returned": False, "viewer.batch.trail.action.reopened": False,
    "viewer.batch.trail.action.accepted": False,
    # 回到上次
    "viewer.resume.lost": True, "viewer.resume.lastNote": False, "viewer.resume.none": False,
    "viewer.resume.pending": False, "viewer.resume.claimed": False,
    "viewer.resume.drafted": False, "viewer.resume.review": False,
    "viewer.resume.reviewing": False, "viewer.resume.doneMark": False, "viewer.resume.doneRest": False,
    "viewer.resume.doneCount": False, "viewer.resume.doneSummary": False,
    "viewer.resume.go": False, "viewer.resume.noLast": False,
    # 服务态徽标与提示条
    "viewer.mode.server": False, "viewer.mode.serverPlain": False,
    "viewer.mode.serverTitle": False, "viewer.mode.old": False, "viewer.mode.oldTitle": False,
    "viewer.mode.mismatch": False, "viewer.mode.mismatchTitle": False,
    "viewer.mode.queued": False, "viewer.mode.failed": False,
    "viewer.mode.barOtherReview": False, "viewer.mode.barOtherProgram": False,
    "viewer.mode.barNoService": False, "viewer.mode.barHead": False,
    "viewer.mode.barQueued": False, "viewer.mode.barCopy": False,
    "viewer.mode.barCopied": False, "viewer.mode.barClose": False,
    # Agent CLI：提交与要求
    "cli.hand.itemSep": False, "cli.hand.agentFallback": False, "cli.hand.noPages": False,
    "cli.hand.latest": True, "cli.hand.userMessage": False, "cli.hand.resolvedCount": True,
    "cli.hand.alwaysHead": False, "cli.hand.noteMissing": False, "cli.hand.noteNoBatch": False,
    "cli.hand.noteFailed": False,
    "cli.hand.summary": False, "cli.hand.history": True, "cli.hand.inFlight": False,
    "cli.hand.drafted": True, "cli.hand.open": False, "cli.hand.unclaimed": False,
    "cli.hand.target.review": False, "cli.hand.target.final": False,
    "cli.hand.subagents.ok": False, "cli.hand.subagents.no": False,
    "cli.hand.fullCheck": False, "cli.hand.waitHint": True, "cli.hand.timeout": False,
    "cli.hand.subLine.ok": False, "cli.hand.subLine.no": False, "cli.hand.reviewHead": False,
    "cli.hand.reviewNote": False, "cli.hand.submitNote": False, "cli.hand.draftSummary": False,
    "cli.hand.reviewerBrief": False, "cli.hand.head": False, "cli.hand.alwaysLine": False,
    "cli.hand.alwaysItem": False, "cli.hand.moreSkills": False, "cli.hand.openCount": True,
    "cli.hand.claimedNote": False, "cli.hand.noClaimNote": False, "cli.hand.endReview": False,
    "cli.hand.endFinal": False, "cli.hand.finishLine": False, "cli.hand.otherBatch": False,
    "cli.hand.otherBatchItem": False, "cli.hand.doneDrafted": True,
    "cli.hand.doneStop": False, "cli.hand.doneReview": False, "cli.hand.doneFinal": True,
    "cli.hand.doneLeft": True, "cli.hand.doneNone": False, "cli.hand.doneMissing": False,
    "cli.hand.doneRefused": False, "cli.hand.reviewResult": False, "cli.hand.markResult": False,
    # Agent CLI：处理记录与对比
    "cli.batch.status.pending": False, "cli.batch.status.claimed": False,
    "cli.batch.status.drafted": False, "cli.batch.status.review": False,
    "cli.batch.status.reviewing": False, "cli.batch.status.done": False,
    "cli.batch.title": True, "cli.batch.counts": False, "cli.batch.countsMore": True,
    "cli.batch.item": False, "cli.batch.who": False,
    "cli.batch.submitted": True, "cli.batch.pages": False, "cli.batch.updates": True,
    "cli.batch.requirement": False, "cli.batch.alwaysLine": False,
    "cli.batch.claimedAt": False, "cli.batch.doneAt": False,
    "cli.batch.acceptLine": True, "cli.batch.stage": False, "cli.batch.stageClaim": False,
    "cli.batch.stageDone": False, "cli.batch.stageLive": False,
    "cli.batch.stageDrafted": True, "cli.batch.stageReviewed": False,
    "cli.batch.stageSummary": False, "cli.batch.stageName.draft": False,
    "cli.batch.stageName.review": False, "cli.batch.stageName.single": False,
    "cli.batch.reviewNote": False, "cli.batch.resolved": True, "cli.batch.resolvedPages": False,
    "cli.batch.leftOpen": True, "cli.batch.live": False, "cli.batch.estimated": False,
    "cli.batch.summary": False, "cli.batch.drafted": True, "cli.batch.draftedItem": False,
    "cli.batch.noteLine": False, "cli.batch.resolution": False, "cli.batch.trail": False,
    "cli.batch.deleted": False, "cli.batch.reopened": False, "cli.batch.empty": False,
    "cli.batch.trailEntry": False, "cli.batch.trailBatch": False, "cli.batch.trailNote": False,
    "cli.batch.trail.whoPage": False, "cli.batch.trail.action.drafted": False,
    "cli.batch.trail.action.resolved": False, "cli.batch.trail.action.finalized": False,
    "cli.batch.trail.action.approved": False, "cli.batch.trail.action.revised": False,
    "cli.batch.trail.action.returned": False, "cli.batch.trail.action.reopened": False,
    "cli.batch.trail.action.accepted": False, "cli.batch.diffNoSnapshot": False,
    "cli.batch.diffNoBatch": False, "cli.batch.diffHead": False,
    "cli.batch.diffRangeNow": False, "cli.batch.diffRangeDraft": False,
    "cli.batch.diffMissing": False, "cli.batch.diffPage": False, "cli.batch.diffKey": False,
    "cli.batch.diffRemoved": False, "cli.batch.diffNone": False,
    "cli.batch.diffBefore": False, "cli.batch.diffAfter": False,
}

# 存储值、协议写法与旧固定兼容提示：不随语言改写，页面和 CLI 都要原样保留。
FIXED_LITERALS = (
    store.FULL_CHECK_TEXT,
    "【本批提交选项】",
    "报告", "讲稿", "报告和讲稿", "未指定",
)


def tmpdir() -> Path:
    return Path(tempfile.mkdtemp())


def source() -> str:
    return TEMPLATE.read_text(encoding="utf-8")


def between(text: str, start: str, end: str) -> str:
    a = text.find(start)
    b = text.find(end, a + 1) if a >= 0 else -1
    if a < 0 or b < 0:
        raise AssertionError(f"页面里找不到 {start!r} 到 {end!r} 这一段")
    return text[a:b]


def uses(region: str, key: str) -> bool:
    """渲染路径从词典取这条消息：JS 动态片段直接调 tMsg，静态标记用 data-* 声明。"""
    return (f'tMsg("{key}"' in region or f'data-msg="{key}"' in region
            or f'data-title="{key}"' in region or f'data-aria="{key}"' in region
            or f'data-ph="{key}"' in region)


def run_cli(*args: str, env: dict[str, str] | None = None, timeout: float = 40):
    base = {k: v for k, v in os.environ.items() if k not in ("AE_LOCALE", "LC_ALL", "LC_MESSAGES", "LANG")}
    base.update(env or {})
    base["CLAUDECODE"] = ""
    return subprocess.run([sys.executable, str(SCRIPTS / "notes.py"), *args],
                          capture_output=True, text=True, env=base, timeout=timeout)


class CatalogTests(unittest.TestCase):
    def test_every_handoff_key_exists_in_both_catalogs(self):
        for tag in i18n.SUPPORTED:
            messages = i18n.load_catalog(tag)["messages"]
            missing = sorted(k for k in HANDOFF_KEYS if k not in messages)
            self.assertEqual(missing, [], f"{tag} 缺少消息")

    def test_english_values_carry_no_chinese(self):
        en = i18n.load_catalog("en")["messages"]
        chinese = sorted(k for k, v in en.items() if _CJK.search(v) and not k.startswith("locale.name"))
        self.assertEqual(chinese, [], "英文消息里混了中文（语言自身的名字除外）")

    def test_counted_english_messages_have_both_plural_branches(self):
        en = i18n.load_catalog("en")["messages"]
        for key, counted in HANDOFF_KEYS.items():
            if not counted:
                continue
            for suffix in ("one", "other"):
                self.assertIn(f"{key}.{suffix}", en, f"英文计数消息缺 {key}.{suffix}")
            for count in (0, 1, 2):
                params = {name: str(count) for name in i18n.placeholders(en[key])}
                params.pop("count", None)
                text = i18n.translate(i18n.load_catalog("en"), key, count=count, **params)
                self.assertIn(str(count), text, f"{key} 在 count={count} 时没换上数字")
                self.assertNotIn("{count}", text, f"{key} 在 count={count} 时还剩占位符")

    def test_chinese_counted_messages_have_no_plural_branches(self):
        zh = i18n.load_catalog("zh-CN")["messages"]
        branched = sorted(k for k in HANDOFF_KEYS if HANDOFF_KEYS[k] and f"{k}.one" in zh)
        self.assertEqual(branched, [], "中文不该有英文式复数分支")

    def test_catalogs_stay_self_consistent(self):
        self.assertEqual(i18n.check_catalogs(), [])

    def test_every_key_is_wired_into_the_page_or_the_cli(self):
        page = source() + (ROOT / "skill" / "annotation-everything" / "scripts" / "notes.py").read_text(encoding="utf-8")
        unwired = sorted(k for k in HANDOFF_KEYS if f'"{k}"' not in page)
        self.assertEqual(unwired, [], "这些消息加了却没有任何地方用")

    def test_fixed_compat_and_storage_literals_stay_in_the_catalog(self):
        """旧固定兼容提示和存储值按原文进词典，旧工具与旧导入才认得出来。"""
        zh = i18n.load_catalog("zh-CN")["messages"]
        self.assertEqual(zh["cli.hand.fullCheck"], "；" + store.FULL_CHECK_TEXT)
        for value in ("报告", "讲稿", "报告和讲稿"):
            self.assertIn(value, zh.values())


class StructuredOptionTests(unittest.TestCase):
    """结构化选项不随显示语言改变：同一份提交在两种语言下说的是同一件事。"""

    def setUp(self):
        self.d = tmpdir()
        (self.d / "index.html").write_text("x")
        store.add(self.d, {"page": 2, "text": "口径要对齐"})
        self.item = {"seq": 1, "status": "pending", "target": "review",
                     "allowSubagents": True, "fullCheck": True}

    def test_requirement_text_keeps_every_constraint_in_both_languages(self):
        for lang, marks in (("zh-CN", ("做到「已初改·待复核」后停下等用户", "；允许派发子代理", "；" + store.FULL_CHECK_TEXT)),
                            ("en", ("Stop at “drafted, waiting for review”", "you may dispatch subagents",
                                    "whole report and the speaker notes", "evidence", "uncertain or you did not check"))):
            text = notes_cli.target_text(self.item, lang)
            for mark in marks:
                self.assertIn(mark, text, f"{lang} 的要求里少了 {mark!r}：{text}")

    def test_structured_fields_are_the_same_regardless_of_language(self):
        for lang in ("zh-CN", "en"):
            with self.subTest(lang=lang):
                _h, item, _m = store.handoff_submit(self.d, "先改第 2 页", target="review",
                                                    allow_subagents=True, full_check=True)
                self.assertEqual((item["target"], item["allowSubagents"], item["fullCheck"]),
                                 ("review", True, True))
                self.assertEqual(notes_cli.target_text(item, lang), notes_cli.target_text(self.item, lang))

    def test_legacy_item_without_new_fields_still_reads_as_final_and_no_subagents(self):
        text = notes_cli.target_text({"seq": 1}, "en")
        self.assertIn("Take it through to finalized", text)
        self.assertIn("no subagents", text)
        self.assertNotIn("full check", text)

    def test_full_check_compat_text_is_untouched(self):
        """旧服务把选项拼进 message，旧代理据此识别；这句话改一个字节，旧数据就还原不回来。"""
        self.assertEqual(store.FULL_CHECK_TEXT,
                         "全量勾稽：逐页检查整份报告和讲稿的口径、数据及前后引用；"
                         "有依据的同步修正，不确定或未检查的部分要说明")
        self.assertEqual(store.full_check_message("先改第 2 页"),
                         "先改第 2 页\n\n【本批提交选项】" + store.FULL_CHECK_TEXT)

    def test_original_note_survives_a_legacy_service_update(self):
        _h, item, _m = store.handoff_submit(self.d, "先改第 2 页", full_check=True)
        # 旧服务把选项拼进 message：认得出来，还原成用户写的原文
        legacy = dict(item, message=store.full_check_message(item["userMessage"]))
        self.assertEqual(store.handoff_user_message(legacy), "先改第 2 页")
        # 旧服务还截断了 message：以最新的 message 为准，不假装还原
        cut = dict(item, message=store.full_check_message(item["userMessage"])[:20])
        self.assertEqual(store.handoff_user_message(cut), cut["message"])
        # 0.3 的旧批次只有 message，没有结构化字段
        old = {"seq": 1, "message": "旧服务写的附言", "status": "pending"}
        self.assertEqual(store.handoff_user_message(old), "旧服务写的附言")

    def test_page_posts_the_same_body_in_both_languages(self):
        body = between(source(), 'if(op.kind==="handoff"){const message=', "return api(\"POST\",\"/api/handoff\"")
        self.assertIn("target:op.target", body)
        self.assertIn("allowSubagents:!!op.allowSubagents", body)
        self.assertNotIn("uiLocale", body, "提交的协议字段不能跟着界面语言变")


class CliFlowTests(unittest.TestCase):
    """关键状态在两种语言下都走得完、核对得上；执行者名称、处理说明和历史摘要保持原文。"""

    def setUp(self):
        self.d = tmpdir()
        (self.d / "index.html").write_text("x")
        self.a = store.add(self.d, {"page": 2, "text": "口径要对齐"})[1]["id"]
        self.b = store.add(self.d, {"page": 3, "text": "表格加单位"})[1]["id"]

    def test_agent_reads_the_batch_in_english(self):
        store.handoff_submit(self.d, "先改第 2 页", target="review", allow_subagents=True, full_check=True)
        got = run_cli("wait", str(self.d), "--timeout", "5", "--locale", "en")
        out = got.stdout
        self.assertEqual(got.returncode, 0, got.stderr)
        self.assertIn("# Handed to the agent: batch 1", out)
        self.assertIn("Note from the user: 先改第 2 页", out)          # 原始附言保持原文
        self.assertIn("Stop at “drafted, waiting for review”", out)
        self.assertIn("you may dispatch subagents", out)
        self.assertIn("full check: go through the whole report and the speaker notes", out)
        self.assertIn("evidence", out)
        self.assertIn("uncertain or you did not check", out)
        self.assertIn("At submit time 2 annotations were open, on pages 2, 3.", out)
        self.assertIn("Marked “in progress”", out)
        self.assertIn(f"notes.py resolve {self.d.resolve()} <id> --seq 1", out)   # 命令本身不翻译

    def test_english_wait_keeps_action_context_in_draft_and_review(self):
        """按审阅保存的语言交接时，初改与复核都拿到完整定位、同改要求和技能归属。"""
        (self.d / "review.json").write_text(json.dumps({"locale": "en", "pages": [
            {"n": 2, "title": "合成图表", "key": "chart"}]}), encoding="utf-8")
        region = store.add(self.d, {
            "page": 2, "key": "chart", "target": "报告", "text": "对齐这里", "anchor": "右下角图例",
            "region": {"x": 10, "y": 20, "w": 30, "h": 40}, "style": {"scope": "region"},
            "skills": ["layout-check"],
            "pair": {"side": "讲稿", "text": "对应句保持原文 <&>", "match": "similar"},
        })[1]["id"]
        pin = store.add(self.d, {"page": 2, "text": "调整字号", "target": "报告",
                                 "pin": {"x": 90, "y": 80}})[1]["id"]
        store.always_save(self.d, [{"name": "style-check", "scope": "style"}])
        store.handoff_submit(self.d, "按标注处理", target="review", allow_subagents=True)
        expected = ("Region 1 (x 10.0%, y 20.0%, w 30.0%, h 40.0%)", "Style · selected area",
                    "Pin 2 (x 90.0%, y 80.0%)", "Position hint: 右下角图例", "layout-check",
                    "style-check", "specified for this annotation", "standing", "Also change the other side",
                    "Speaker notes", "对应句保持原文 <&>", "percentages relative to the top-left")
        for stage, who in (("draft", "drafter"), ("review", "reviewer")):
            got = run_cli("wait", str(self.d), "--timeout", "5", "--stage", stage, "--by", who)
            self.assertEqual(got.returncode, 0, got.stderr)
            for text in expected:
                self.assertIn(text, got.stdout, f"{stage}: missing {text}")
            if stage == "draft":
                store.set_status(self.d, region, "drafted", by=who, resolution="图例已对齐")
                store.set_status(self.d, pin, "drafted", by=who, resolution="字号已调整")
                store.handoff_done(self.d, by=who, summary="完成合成标注")
                store.handoff_request_review(self.d, 1)
            else:
                self.assertIn("Draft note: 图例已对齐", got.stdout)

    def test_english_show_keeps_structured_edits(self):
        for op, before, after, text in (("delete", "应删原文", "", "删除引用的这段文字"),
                                         ("replace", "旧口径", "新口径", "改为：新口径")):
            store.add(self.d, {"page": 2, "text": text, "target": "报告", "quote": before,
                              "quoteFrom": "报告", "edit": {"op": op, "from": before, "to": after}})
        got = run_cli("show", str(self.d), "--locale", "en")
        self.assertEqual(got.returncode, 0, got.stderr)
        self.assertIn('[Report · Delete] Delete “应删原文”', got.stdout)
        self.assertIn('[Report · Replace] Replace “旧口径” with “新口径”', got.stdout)

    def test_english_show_keeps_legacy_marks_and_page_warnings(self):
        doc = {"rev": 1, "notes": [
            {"id": "done", "page": 2, "text": "已处理", "status": "resolved", "pin": {"x": 1, "y": 2}},
            {"id": "legacy", "page": 2, "key": "old-page", "text": "保留旧标注", "target": "报告",
             "pin": {"x": 3, "y": 4}, "style": {"scope": "region", "region": {"x": 5, "y": 6, "w": 7, "h": 8}},
             "orphan": True},
            {"id": "moved", "page": 2, "key": "wrong-page", "text": "确认页码", "target": "报告"},
        ]}
        out = notes_cli.to_markdown(doc, {"pages": [{"n": 2, "key": "new-page"}]},
                                    include_resolved=False, page=2, locale="en")
        for text in ("Pin 2 (x 3%, y 4%)", "Region 2 (x 5%, y 6%, w 7%, h 8%)",
                     "original page (key old-page) is missing", "wrong-page does not match page 2 (new-page)"):
            self.assertIn(text, out)
        self.assertNotIn("`done`", out)

    def test_english_show_distinguishes_suggested_counterpart_from_required_edit(self):
        script = self.d / "script.md"
        sentence = "Revenue increased by ten percent."
        script.write_text(f"### Page 2 | Synthetic\n**Talking points**: {sentence}\n", encoding="utf-8")
        note = {"id": "quote", "page": 2, "target": "报告", "text": "核对口径", "quote": sentence}
        out = notes_cli.to_markdown({"rev": 1, "notes": [note]}, {}, include_resolved=False,
                                    page=None, locale="en", sources={"script": script})
        self.assertIn(sentence, out)
        self.assertIn("Hint: Speaker notes, page 2", out)
        self.assertIn("Confirm whether to update it too (change both sides was not selected)", out)
        self.assertNotIn("Also change the other side", out)

    def test_chinese_output_keeps_the_shipped_wording(self):
        store.handoff_submit(self.d, "先改第 2 页", target="review", allow_subagents=True, full_check=True)
        got = run_cli("wait", str(self.d), "--timeout", "5")
        out = got.stdout
        self.assertEqual(got.returncode, 0, got.stderr)
        self.assertIn("# 收到「交给 Agent」第 1 批", out)
        self.assertIn("用户附言：先改第 2 页", out)
        self.assertIn("要求：做到「已初改·待复核」后停下等用户；允许派发子代理；" + store.FULL_CHECK_TEXT, out)
        self.assertIn("提交时未处理批注 2 条，涉及第 2、3 页。", out)

    def test_locale_priority_matches_show(self):
        """--locale 优先于 AE_LOCALE，再优先于审阅里保存的选择（与 show 同一套）。"""
        manifest = {"locale": "en"}
        (self.d / "review.json").write_text(json.dumps(manifest), encoding="utf-8")
        store.handoff_submit(self.d, "第一条")
        by_review = run_cli("wait", str(self.d), "--timeout", "5", "--no-claim")
        self.assertEqual(by_review.returncode, 0, by_review.stderr)
        self.assertIn("# Handed to the agent", by_review.stdout)
        store.handoff_submit(self.d, "第二条")
        by_flag = run_cli("wait", str(self.d), "--timeout", "5", "--no-claim", "--locale", "zh-CN")
        self.assertIn("# 收到「交给 Agent」", by_flag.stdout)
        store.handoff_submit(self.d, "第三条")
        by_env = run_cli("wait", str(self.d), "--timeout", "5", "--no-claim", env={"AE_LOCALE": "zh-CN"})
        self.assertIn("# 收到「交给 Agent」", by_env.stdout)
        bad = run_cli("wait", str(self.d), "--timeout", "5", "--no-claim", "--locale", "fr")
        self.assertEqual(bad.returncode, 2)
        self.assertIn("不支持的语言", bad.stderr)

    def test_draft_then_review_round_trip_in_english(self):
        store.handoff_submit(self.d, "先改", target="review", allow_subagents=True)
        run_cli("wait", str(self.d), "--timeout", "5", "--locale", "en", "--by", "sonnet-初改")
        store.set_status(self.d, self.a, "drafted", resolution="把口径改成同比", by="sonnet-初改")
        done = run_cli("done", str(self.d), "--locale", "en", "--by", "sonnet-初改",
                        "--summary", "把口径改成同比")
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertIn("Batch 1 → draft complete: 1 annotation drafted and waiting for review.", done.stdout)
        self.assertIn("Stopping here as the user asked", done.stdout)
        store.handoff_request_review(self.d, 1, "重点看第 2 页")
        review = run_cli("wait", str(self.d), "--timeout", "5", "--stage", "review", "--locale", "en",
                         "--by", "opus-复核")
        self.assertEqual(review.returncode, 0, review.stderr)
        self.assertIn("# Review requested: batch 1 (the draft was made by sonnet-初改)", review.stdout)
        self.assertIn("Note from the user (with the review request): 重点看第 2 页", review.stdout)
        self.assertIn("Note from the user (at submit time): 先改", review.stdout)
        self.assertIn("Draft summary: 把口径改成同比", review.stdout)     # 摘要保持原文
        self.assertIn("You are the reviewer", review.stdout)
        verdict = run_cli("review", str(self.d), self.a, "--ok", "--locale", "en")
        self.assertEqual(verdict.returncode, 0, verdict.stderr)
        self.assertIn(f"{self.a} → Approved on review (Resolved)", verdict.stdout)
        store.set_status(self.d, self.b, "resolved", by="opus-复核")
        final = run_cli("done", str(self.d), "--locale", "en", "--by", "opus-复核")
        self.assertIn("Batch 1 → handled: this batch resolved 2", final.stdout)

    def test_return_verdict_and_resolve_are_localized(self):
        store.handoff_submit(self.d, "先改", target="review", allow_subagents=True)
        run_cli("wait", str(self.d), "--timeout", "5", "--locale", "en", "--by", "sonnet-初改")
        store.set_status(self.d, self.a, "drafted", by="sonnet-初改")
        run_cli("done", str(self.d), "--by", "sonnet-初改")
        store.handoff_request_review(self.d, 1)
        run_cli("wait", str(self.d), "--timeout", "5", "--stage", "review", "--locale", "en", "--by", "opus-复核")
        back = run_cli("review", str(self.d), self.a, "--return", "--note", "要用户拍板", "--locale", "en")
        self.assertIn(f"{self.a} → Sent back to the user (Open)", back.stdout)
        mark = run_cli("resolve", str(self.d), self.b, "--note", "用户自己定了", "--locale", "en")
        self.assertIn(f"{self.b} → Resolved", mark.stdout)
        show = run_cli("show", str(self.d), "--all", "--locale", "en")
        self.assertIn("用户自己定了", show.stdout)                        # 处理说明保持原文

    def test_batches_history_in_both_languages(self):
        store.handoff_submit(self.d, "先改 2、3 页", target="review", allow_subagents=True, full_check=True)
        run_cli("wait", str(self.d), "--timeout", "5", "--locale", "en", "--by", "sonnet-初改")
        store.set_status(self.d, self.a, "drafted", resolution="改成同比", by="sonnet-初改")
        run_cli("done", str(self.d), "--by", "sonnet-初改", "--summary", "初改 1 条")
        en = run_cli("batches", str(self.d), "--locale", "en")
        self.assertEqual(en.returncode, 0, en.stderr)
        self.assertIn("# Processing history · 1 batch", en.stdout)
        self.assertIn("## Batch 1 · drafted, waiting for review (sonnet-初改)", en.stdout)
        self.assertIn("Submitted:", en.stdout)
        self.assertIn("Note from the user: 先改 2、3 页", en.stdout)
        self.assertIn("Asks for: Stop at “drafted, waiting for review” and wait for the user", en.stdout)
        self.assertIn("you may dispatch subagents", en.stdout)
        self.assertIn("full check", en.stdout)
        self.assertIn("Draft: sonnet-初改 claimed", en.stdout)
        self.assertIn("drafted 1", en.stdout)
        self.assertIn("Summary: 初改 1 条", en.stdout)
        self.assertIn(f"1 annotation drafted and waiting for review: `{self.a}`", en.stdout)
        zh = run_cli("batches", str(self.d))
        self.assertIn("# 处理记录 · 共 1 批（", zh.stdout)
        self.assertIn("## 第 1 批 · 已初改·待复核（sonnet-初改）", zh.stdout)
        self.assertIn("要求：做到「已初改·待复核」后停下等用户；允许派发子代理；" + store.FULL_CHECK_TEXT, zh.stdout)
        self.assertIn("初改：sonnet-初改 接收 ", zh.stdout)
        self.assertIn("处理摘要：初改 1 条", zh.stdout)

    def test_last_limits_history_and_old_batches_still_read(self):
        store.handoff_submit(self.d, "")
        run_cli("wait", str(self.d), "--timeout", "5", "--locale", "en")
        store.handoff_done(self.d, by="claude", summary="第一批收尾")
        store.handoff_submit(self.d, "第二批")
        run_cli("wait", str(self.d), "--timeout", "5", "--locale", "en")
        store.handoff_done(self.d, by="claude", summary="第二批收尾")
        got = run_cli("batches", str(self.d), "--last", "1", "--locale", "en")
        self.assertIn("2 batches", got.stdout)
        self.assertIn("Batch 2", got.stdout)
        self.assertNotIn("## Batch 1", got.stdout)
        legacy = {"version": 1, "seq": 3, "items": [{"seq": 3, "status": "done", "at": "2026-10-27T10:00:00+08:00",
                                                     "claimedAt": "2026-10-27T10:01:00+08:00",
                                                     "doneAt": "2026-10-27T10:05:00+08:00", "openCount": 1,
                                                     "pages": [2], "summary": "旧版收尾", "message": "旧附言"}]}
        (self.d / store.HANDOFF_FILE).write_text(json.dumps(legacy), encoding="utf-8")
        old = run_cli("batches", str(self.d), "--locale", "en")
        self.assertIn("## Batch 3 · handled", old.stdout)
        self.assertIn("# Processing history · 1 batch", old.stdout)
        self.assertIn("Note from the user: 旧附言", old.stdout)          # 旧数据没有 userMessage
        self.assertIn("Summary: 旧版收尾", old.stdout)
        self.assertNotIn("Asks for:", old.stdout, "旧数据没有结构化选项，不要凭空补一句要求")

    def test_parallel_batches_keep_their_stages_straight(self):
        first = store.add(self.d, {"page": 2, "text": "第一批 A"})[1]["id"]
        second = store.add(self.d, {"page": 3, "text": "第二批 B"})[1]["id"]
        store.handoff_submit(self.d, "第一批", target="review")
        store.handoff_claim(self.d, by="A")
        store.set_status(self.d, first, "drafted", by="A")
        store.handoff_done(self.d, by="A")
        store.handoff_submit(self.d, "第二批", allow_subagents=True)
        store.handoff_claim(self.d, by="B")
        out = run_cli("show", str(self.d), "--all", "--locale", "en").stdout
        self.assertIn("Handed to the agent: the latest is batch 2, in progress", out)
        self.assertIn("1 batch drafted and waiting for review", out)
        self.assertIn("Batch 1 (drafted, waiting for review) asks for:", out)
        self.assertIn("Batch 2 (in progress) asks for:", out)
        self.assertIn("0 waiting to be claimed, 1 in progress, 0 waiting for review, 0 under review", out)

    def test_full_check_line_has_no_dangling_separator_in_either_language(self):
        """R1：复核分支单独打印全量勾稽要求，两种语言都不能带着前导分隔符。"""
        store.handoff_submit(self.d, "先改", target="review", allow_subagents=True, full_check=True)
        run_cli("wait", str(self.d), "--timeout", "5", "--by", "sonnet-初改")
        store.set_status(self.d, self.a, "drafted", by="sonnet-初改")
        run_cli("done", str(self.d), "--by", "sonnet-初改", "--summary", "初改 1 条")
        store.handoff_request_review(self.d, 1, "重点看第 2 页")
        for lang, marks in (("zh-CN", (store.FULL_CHECK_TEXT,)), ("en", ("full check: go through", "evidence", "uncertain"))):
            out = run_cli("wait", str(self.d), "--timeout", "5", "--no-claim", "--stage", "review",
                          "--locale", lang).stdout
            line = next((ln for ln in out.splitlines() if "勾稽" in ln or "full check" in ln), "")
            self.assertTrue(line and not line.startswith(("；", ";", " ")), f"{lang} 的全量勾稽行带着前导分隔符：{line!r}")
            for mark in marks:
                self.assertIn(mark, line, f"{lang} 少了约束 {mark!r}")

    def test_failures_of_mark_and_review_are_localized_too(self):
        """R5：退回与标记的失败输出不能在同一条命令里混语言。"""
        store.handoff_submit(self.d, "先改", target="review", allow_subagents=True)
        run_cli("wait", str(self.d), "--timeout", "5", "--by", "sonnet-初改")
        store.set_status(self.d, self.a, "drafted", by="sonnet-初改")
        run_cli("done", str(self.d), "--by", "sonnet-初改")
        store.handoff_request_review(self.d, 1)
        run_cli("wait", str(self.d), "--timeout", "5", "--stage", "review", "--by", "opus-复核")
        miss = run_cli("review", str(self.d), "nope", "--ok", "--locale", "en")
        self.assertEqual(miss.returncode, 1)
        self.assertEqual(miss.stderr.strip(), "nope: no such annotation", miss.stderr)
        ok = run_cli("review", str(self.d), self.a, "--ok", "--locale", "en")
        self.assertIn("Approved on review", ok.stdout)
        self.assertNotIn("没有这条批注", ok.stdout + ok.stderr)
        mark = run_cli("resolve", str(self.d), "missing", "--locale", "en")
        self.assertEqual(mark.stderr.strip(), "missing: no such annotation", mark.stderr)
        zh = run_cli("resolve", str(self.d), "missing")
        self.assertEqual(zh.stderr.strip(), "missing：没有这条批注", zh.stderr)

    def test_english_show_keeps_the_standing_skills_line(self):
        """R8：读英文的代理也要看到常驻技能约束（名称与范围值保持原样）。"""
        store.always_save(self.d, store.clean_always([{"name": "术语表", "scope": "报告"},
                                                      {"name": "humanizer-zh", "scope": "all"}]))
        store.handoff_submit(self.d, "先改")
        en = run_cli("show", str(self.d), "--all", "--locale", "en").stdout
        self.assertIn("Standing skills required by the user: call them before handling each annotation within scope", en)
        self.assertIn("术语表 (Report), humanizer-zh (All annotations)", en)
        zh = run_cli("show", str(self.d), "--all").stdout
        self.assertIn("常驻技能（用户要求：每条批注处理前都按范围先调用，下面每条的「技能」里已列出）："
                      "术语表（报告）、humanizer-zh（全部批注）", zh)

    def test_single_stage_and_forbidden_subagents_read_the_same_in_both_languages(self):
        """单阶段、禁止子代理：停止条件在两种语言下都说清，不多也不少。"""
        store.handoff_submit(self.d, "只改第 2 页")
        en = run_cli("wait", str(self.d), "--timeout", "5", "--no-claim", "--locale", "en").stdout
        self.assertIn("Asks for: Take it through to finalized", en)
        self.assertIn("no subagents — the main agent of this session handles it in person", en)
        self.assertNotIn("full check", en)
        self.assertIn("The annotations are marked resolved right away", en)
        store.handoff_submit(self.d, "第二批")
        zh = run_cli("wait", str(self.d), "--timeout", "5", "--no-claim").stdout
        self.assertIn("要求：做到定稿；不允许派发子代理（由当前会话的主代理亲自处理）", zh)
        self.assertNotIn("全量勾稽", zh)
        self.assertIn("批注随即定为已处理", zh)
        # 单段流程的处理记录不叫「初改」
        store.handoff_claim(self.d, by="claude")
        store.set_status(self.d, self.a, "resolved", by="claude")
        store.handoff_done(self.d, by="claude", summary="改了 1 条")
        hist = run_cli("batches", str(self.d), "--last", "1", "--locale", "en").stdout
        self.assertIn("Handling: claude claimed", hist)
        self.assertNotIn("Draft: ", hist)
        zh_hist = run_cli("batches", str(self.d), "--last", "1").stdout
        self.assertIn("处理：claude 接收", zh_hist)
        self.assertNotIn("初改：", zh_hist)

    def test_diff_output_is_localized(self):
        store.handoff_submit(self.d, "先改")
        run_cli("wait", str(self.d), "--timeout", "5", "--locale", "en")
        empty = run_cli("diff", str(self.d), "--batch", "9", "--locale", "en")
        self.assertEqual(empty.returncode, 1)
        self.assertIn("Batch 9 has no source-file snapshot.", empty.stderr)


class PageContractTests(unittest.TestCase):
    """提交弹窗、处理记录、恢复提示与服务态徽标都从词典取，存储值和协议写法不动。"""

    def setUp(self):
        self.source = source()

    def test_hand_dialog_reads_from_the_catalog(self):
        dialog = between(self.source, '<div class="modal" id="hand">', '<div class="modal" id="always">')
        for key in ("viewer.hand.cancel", "viewer.hand.targetLabel", "viewer.hand.targetFinal",
                    "viewer.hand.targetReview", "viewer.hand.subagents", "viewer.hand.fullCheck",
                    "viewer.hand.help", "viewer.hand.hist"):
            self.assertTrue(uses(dialog, key), f"提交弹窗没走词典：{key}")
        for key in ("viewer.hand.targetFinalTitle", "viewer.hand.targetReviewTitle",
                    "viewer.hand.subagentsTitle", "viewer.hand.fullCheckTitle", "viewer.hand.histTitle"):
            self.assertIn(f'data-title="{key}"', dialog, f"提交弹窗的提示没走词典：{key}")
        # data-msg 元素里按 #30 的约定保留中文初始文字；写死在属性里才算没走词典
        for attr, literal in (("title", "这一批批注写好了"), ("title", "不勾选时由当前会话的主代理亲自处理"),
                              ("title", "处理批注后检查整份报告和讲稿"), ("placeholder", "给 Agent 的附言（可选）"),
                              ("placeholder", "给复核者的附言（可选）")):
            self.assertNotIn(f'{attr}="{literal}', dialog, f"提交弹窗的 {attr} 里还写死了中文")

    def test_hand_button_and_state_badge_read_from_the_catalog(self):
        button = between(self.source, '<button id="handBtn"', "</button>")
        self.assertTrue(uses(button, "viewer.hand.btn"), "viewer.hand.btn")
        self.assertIn('data-title="viewer.hand.btnTitle"', button)
        self.assertNotIn('title="这一批批注写好了', button)
        badge = between(self.source, "function renderHand()", "let handMode=")
        for key in ("viewer.hand.state.queued", "viewer.hand.state.queuedTitle",
                    "viewer.hand.state.pending", "viewer.hand.state.claimed",
                    "viewer.hand.state.drafted", "viewer.hand.state.review",
                    "viewer.hand.state.reviewing", "viewer.hand.state.done"):
            self.assertIn(f'"{key}"', badge, f"提交状态徽标没走词典：{key}")
        for literal in ("交给 Agent（待补传）", "已交给 Agent · 第 ", "Agent 处理中 · 第 ",
                        "Agent 已处理 · 第 ", "点击查看处理记录（J）"):
            self.assertNotIn(literal, badge, f"状态徽标里还写死了 {literal}")

    def test_batches_dialog_reads_from_the_catalog(self):
        dialog = between(self.source, '<div class="modal" id="batches">', '<div id="toast">')
        self.assertTrue(uses(dialog, "viewer.batch.title"), "viewer.batch.title")
        self.assertTrue(uses(dialog, "viewer.batch.close"), "viewer.batch.close")
        render = between(self.source, "function renderBatches()", "function openBatches()")
        for key in ("viewer.batch.loading", "viewer.batch.empty", "viewer.batch.sum",
                    "viewer.batch.requirement", "viewer.batch.summary", "viewer.batch.noSummary",
                    "viewer.batch.draftedList", "viewer.batch.resolvedList",
                    "viewer.batch.draftNote", "viewer.batch.resolutionNote",
                    "viewer.batch.reopened", "viewer.batch.deleted", "viewer.batch.whoPage"):
            self.assertIn(f'"{key}"', render, f"处理记录没走词典：{key}")
        # 批次状态与阶段名在模块级的映射表里，提交弹窗和处理记录共用
        for key in ("viewer.batch.status.pending", "viewer.batch.status.claimed",
                    "viewer.batch.status.drafted", "viewer.batch.status.review",
                    "viewer.batch.status.reviewing", "viewer.batch.status.done",
                    "viewer.batch.stage.draft", "viewer.batch.stage.review"):
            self.assertIn(f'"{key}"', self.source, f"批次状态没走词典：{key}")
        for literal in ("处理记录", "正在读取处理记录…", "还没有交给 Agent 的记录。",
                        "已初改·待复核", "解决 ", "处理摘要：", "（Agent 没写处理摘要）",
                        "请 Agent 复核", "直接定稿", "（这条批注已删除）",
                        "第 ", " 页 · ", "接收 ", "收尾 ", "提交 ", "你直接定稿"):
            self.assertNotIn(literal, render, f"处理记录里还写死了 {literal}")
        self.assertNotIn('title="每次交给 Agent 一批', self.source, "处理记录按钮的提示还写死在属性里")

    def test_trail_and_resume_read_from_the_catalog(self):
        trail = between(self.source, "const TRAIL_ACTION=", "const nowIso=")
        self.assertIn('"viewer.batch.trail.entry"', trail)
        self.assertIn('e.by==="claude"?"Claude"', trail,
                      "痕迹里的执行者名称要与处理记录头部同一套显示规则")
        self.assertIn('"viewer.batch.trail.action.drafted"', trail)
        self.assertNotIn("ACT_ZH", trail, "痕迹动作还有一张写死中文的表")
        self.assertNotIn("const ACT_ZH=", self.source)
        resume = between(self.source, "function renderResume()", "function goLast()")
        for key in ("viewer.resume.lost", "viewer.resume.lastNote", "viewer.resume.none",
                    "viewer.resume.pending", "viewer.resume.claimed", "viewer.resume.drafted",
                    "viewer.resume.review", "viewer.resume.reviewing", "viewer.resume.doneMark",
                    "viewer.resume.doneRest", "viewer.resume.doneCount", "viewer.resume.doneSummary",
                    "viewer.resume.go"):
            self.assertIn(f'"{key}"', resume, f"恢复提示没走词典：{key}")
        for literal in ("条批注的原页面在新版报告里找不到", "上次批注到第 ", "还没交给 Agent（未处理 ",
                        "批等待 Agent 接收", "Agent 正在处理第 ", "条待复核", "跳到第 ", "还没有批注"):
            self.assertNotIn(literal, resume, f"恢复提示里还写死了 {literal}")

    def test_service_badge_and_banner_read_from_the_catalog(self):
        badge = between(self.source, "function updateMode(", "function foundSets(")
        for key in ("viewer.mode.server", "viewer.mode.serverPlain", "viewer.mode.serverTitle",
                    "viewer.mode.old", "viewer.mode.oldTitle", "viewer.mode.mismatch",
                    "viewer.mode.mismatchTitle", "viewer.mode.queued", "viewer.mode.failed"):
            self.assertIn(f'"{key}"', badge, f"服务态徽标没走词典：{key}")
        for literal in ("● 已连接本地服务", "● 旧版服务", "● 服务不匹配", "（待补传 ", "（未能保存 ",
                        "每次改动自动写入审阅目录的 notes.json"):
            self.assertNotIn(literal, badge, f"服务态徽标里还写死了 {literal}")
        self.assertNotIn("const OLD_SERVER_HELP=", self.source, "旧版服务提示还有一份写死中文的副本")
        self.assertNotIn("const MISMATCH_HELP=", self.source, "服务不匹配提示还有一份写死中文的副本")
        bar = between(self.source, "function renderSrvBar()", "function syncSrvBar()")
        for key in ("viewer.mode.barOtherReview", "viewer.mode.barOtherProgram",
                    "viewer.mode.barNoService", "viewer.mode.barHead", "viewer.mode.barQueued"):
            self.assertIn(f'"{key}"', bar, f"提示条没走词典：{key}")
        for literal in ("端口上现在是另一份审阅", "端口上是别的程序", "端口上没有服务在运行",
                        "本审阅《", "的服务没在这里", "项改动会在本审阅的服务"):
            self.assertNotIn(literal, bar, f"提示条里还写死了 {literal}")
        banner = between(self.source, '<div class="resume" id="srvBar"', "</div>")
        self.assertTrue(uses(banner, "viewer.mode.barCopy"), "viewer.mode.barCopy")
        self.assertIn('data-title="viewer.mode.barClose"', banner)
        self.assertNotIn('title="关闭提示"', banner)

    def test_submit_and_batch_toasts_read_from_the_catalog(self):
        for fn, keys in (("function onSent(", ("viewer.hand.toast.sent", "viewer.hand.toast.merged")),
                         ("function onRejected(", ("viewer.hand.toast.legacy", "viewer.hand.toast.failed")),
                         ("function setHand(", ("viewer.hand.toast.claimed", "viewer.hand.toast.drafted",
                                                "viewer.hand.toast.reviewing", "viewer.hand.toast.done")),
                         ("function batchAction(", ("viewer.hand.toast.needServer",
                                                    "viewer.hand.toast.actionLegacy",
                                                    "viewer.hand.toast.actionFailed")),
                         ("function finalizeBatch(", ("viewer.hand.toast.confirmFinalize",
                                                      "viewer.hand.toast.finalized")),
                         ("async function doFlush(", ("viewer.hand.toast.fullCheckStashed",
                                                      "viewer.hand.toast.fullCheckUnconfirmed"))):
            end = "async function resync(" if fn == "async function doFlush(" else "function "
            region = between(self.source, fn, end)
            for key in keys:
                self.assertIn(f'"{key}"', region, f"{fn} 没走词典：{key}")
        for literal in ("已交给 Agent，等待接收", "已更新这次提交（Agent 还没接收）",
                        "Agent 已接收，正在处理", "批初改完成：", "Agent 已开始复核",
                        "Agent 已处理完第 ", "没能交给 Agent：", "已请求复核第 ", " 批已定稿",
                        "跳过复核，把第 ", "要连着本地服务才能操作批次",
                        "本地服务不支持修改全量勾稽", "服务未确认全量勾稽选项"):
            self.assertNotIn(literal, self.source, f"提交 / 批次提示里还写死了 {literal}")

    def test_offline_and_full_check_hints_do_not_claim_a_save(self):
        """没确认的提交不能说成已保存；断线与旧服务的提示要说清状态和恢复方式。"""
        flush = between(self.source, "async function doFlush(", "async function resync(")
        for literal in ("已保存", "已安全保存", "保存成功"):
            self.assertNotIn(literal, flush)
        for key in ("viewer.hand.toast.fullCheckOld", "viewer.hand.toast.fullCheckStashed",
                    "viewer.hand.toast.fullCheckUnconfirmed", "viewer.hand.toast.queuedOffline",
                    "viewer.hand.toast.queuedMismatch", "viewer.hand.toast.fileMode"):
            self.assertIn(f'"{key}"', self.source, f"没走词典：{key}")
        self.assertIn('"viewer.hand.toast.fullCheckOld"', between(self.source, "$(\"doHand\").onclick", "// ---------- 导出"))

    def test_language_switch_repaints_in_flight_handoff_state(self):
        apply = between(self.source, "function applyLocale()", '$("localePick").addEventListener')
        for call in ("renderHand()", "renderResume()", "updateMode()", "renderBatches()"):
            self.assertIn(call, apply, f"切语言时没有重刷 {call}")
        self.assertIn('$("hand").classList.contains("on")', apply, "弹窗开着时要按当前语言重画")
        self.assertIn('$("batches").classList.contains("on")', apply, "处理记录开着时要按当前语言重画")

    def test_repainting_the_dialog_keeps_the_declarative_markup(self):
        """R2：paintHand 只改常驻标签元素的文字；整体替换 innerHTML 会把 data-msg 标记弄丢。"""
        self.assertIn('<span id="doHandLabel" data-msg="viewer.hand.submit">', self.source)
        paint = between(self.source, "function paintHand(", "function openHand(")
        self.assertIn('$("doHandLabel").textContent=', paint)
        self.assertNotIn('$("doHand").innerHTML', paint, "重画时不能整体替换按钮内容")

    def test_repainting_the_dialog_does_not_resubmit(self):
        """切语言只是重画：不提交、不改权限、不请求新 API。"""
        paint = between(self.source, "function paintHand(", "function openHand(")
        for literal in ("api(", "mutate(", "LS.set(HOPT", "requestReview(", "finalizeBatch("):
            self.assertNotIn(literal, paint, f"重画提交弹窗时不能 {literal}")
        self.assertIn("function paintHand(", self.source, "提交弹窗的文字要能单独重画")

    def test_static_markup_stays_balanced(self):
        body = self.source[self.source.find("<body>"):self.source.find("<script>")]
        for tag in ("div", "span", "button", "select", "label", "p"):
            opens = len(re.findall(rf"<{tag}\b", body))
            closes = len(re.findall(rf"</{tag}>", body))
            self.assertEqual(opens, closes, f"<{tag}> 开合不平衡（{opens} 开 / {closes} 合）")


if __name__ == "__main__":
    unittest.main()
