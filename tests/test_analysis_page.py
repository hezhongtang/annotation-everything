"""分析页面的可执行呈现契约：自然叫法、原文转义与双语文案。

真实切换、作答与提交由浏览器冒烟场景覆盖；这里执行同一批纯呈现函数。
"""
from __future__ import annotations

import html
import json
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "skill/annotation-everything/scripts"
sys.path.insert(0, str(SCRIPTS))
import i18n


def interaction_program(parts, setup, expression, locale="en"):
    """执行页面实际交互函数，只替换浏览器提供的 DOM / 网络边界。"""
    node = shutil.which("node")
    if not node:
        raise unittest.SkipTest("没有 JavaScript 引擎")
    source = (SCRIPTS / "viewer_template.html").read_text()
    chunks = []
    for start, end in parts:
        a, b = source.index(start), source.index(end, source.index(start))
        chunks.append(source[a:b])
    messages = i18n.load_catalog(locale)["messages"]
    program = "\n".join([
        "const M=" + json.dumps(messages, ensure_ascii=False) + ";",
        "const uiLocale=" + json.dumps(locale) + ";",
        "const tMsg=(k,p={})=>String(M[k]??k).replace(/\\{(\\w+)\\}/g,(_,n)=>String(p[n]??''));",
        "const escH=s=>String(s??'').replace(/[&<>\"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','\"':'&quot;',\"'\":'&#39;'}[c]));",
        "const elements=new Map(); const $=id=>{if(!elements.has(id))elements.set(id,{innerHTML:'',textContent:'',setAttribute(){},classList:{contains(){return false},toggle(){},remove(){}}});return elements.get(id)};",
        setup, *chunks,
        "(async()=>{console.log(JSON.stringify(await (async()=>{" + expression + "})()))})().catch(e=>{console.error(e);process.exitCode=1});",
    ])
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "interaction.js"
        path.write_text(program)
        result = subprocess.run([node, str(path)], capture_output=True, text=True, timeout=15)
    if result.returncode:
        raise AssertionError(result.stderr)
    return json.loads(result.stdout)


def render_expression(expression, state, locale="en"):
    node = shutil.which("node")
    if not node:
        raise unittest.SkipTest("没有 JavaScript 引擎")
    source = (SCRIPTS / "viewer_template.html").read_text()
    start = source.find("// ---------- Analysis view helpers ----------")
    end = source.find("// ---------- Analysis view DOM ----------")
    if start < 0 or end <= start:
        raise AssertionError("分析页面缺少可执行的呈现函数")
    messages = i18n.load_catalog(locale)["messages"]
    program = "\n".join([
        "const M=" + json.dumps(messages, ensure_ascii=False) + ";",
        "const uiLocale=" + json.dumps(locale) + "; let analysisBusy=false;",
        "const tMsg=(k,p={})=>String(M[k]??k).replace(/\\{(\\w+)\\}/g,(_,n)=>String(p[n]??''));",
        "const escH=s=>String(s??'').replace(/[&<>\"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','\"':'&quot;',\"'\":'&#39;'}[c]));",
        source[start:end],
        "const state=" + json.dumps(state, ensure_ascii=False) + ";",
        "console.log(JSON.stringify(" + expression + "));",
    ])
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "render.js"
        path.write_text(program)
        result = subprocess.run([node, str(path)], capture_output=True, text=True, timeout=15)
    if result.returncode:
        raise AssertionError(result.stderr)
    return json.loads(result.stdout)


class AnalysisPageTests(unittest.TestCase):
    def test_analysis_focus_and_actual_shortcuts_do_not_enter_revision(self):
        setup = """
          let focusMode=false,keepFocusOnExit=false;const visible=new Set(),calls={analysis:0,revision:0};
          let handler=null,modalOpen=false;const modalLayer={classList:{remove(){modalOpen=false}}};const document={body:{classList:{toggle(k,on){on?visible.add(k):visible.delete(k)}}},
            documentElement:{},fullscreenElement:null,querySelector(){return modalOpen?modalLayer:null},querySelectorAll(){return modalOpen?[modalLayer]:[]},
            addEventListener(type,fn){if(type==='keydown')handler=fn}};
          const overviewShown=()=>false,analysisShown=()=>true,closeComposer=()=>{},fit=()=>{};
          const submitAnalysis=()=>calls.analysis++,openHand=()=>calls.revision++,drawerOpen=()=>true,setDrawer=()=>{};
        """
        value = interaction_program([
            ("function applyFocus(on)", 'document.addEventListener("fullscreenchange"'),
            ('document.addEventListener("keydown",e=>{\n  if(overviewShown())return;', "// ---------- Analysis view helpers ----------"),
        ], setup, """
            await toggleFocus(true);const entered=visible.has('focus-mode');await toggleFocus(false);
            const event=key=>({key,target:{tagName:'BODY'},preventDefault(){}});
            handler(event('a'));handler(event('c'));handler(event('p'));modalOpen=true;handler(event('Escape'));
            return {entered,exited:!visible.has('focus-mode'),modalClosed:!modalOpen,...calls};
        """)
        self.assertEqual(value, {"entered": True, "exited": True, "modalClosed": True, "analysis": 1, "revision": 0})

    def test_rendered_tree_and_conversion_exclude_void_proposal_descendants(self):
        state = {"id": "private-analysis", "focus": "最初的问题", "sourceCopy": {}, "sourceState": "released",
                 "nodes": {"n1": {"id": "n1", "text": "被拒的问题", "short": "已作废提议", "status": "void", "parent": None, "decision": "d1"},
                           "n2": {"id": "n2", "text": "子问题", "short": "作废提议的子问题", "status": "decided", "parent": {"node": "n1", "option": "a"}, "decision": "d2"}},
                 "decisions": {"d1": {"option": "a", "label": "旧结论"}, "d2": {"option": "a", "label": "子结论"}}, "rounds": [], "drafts": []}
        setup = "const analysisBusy=false,analysisState=" + json.dumps(state, ensure_ascii=False) + "; let analysisRenderKey='';const analyses=[],analysisSelected=analysisState.id,analysisShown=()=>true,analysisRouteLabels=()=>{};"
        value = interaction_program([
            ("// ---------- Analysis view helpers ----------", "// ---------- Analysis view DOM ----------"),
            ("function renderAnalysis(){", "async function analysisAction("),
        ], setup, "renderAnalysis();return {tree:$('analysisTree').innerHTML,body:$('analysisBody').innerHTML};")
        self.assertNotIn("已作废提议", value["tree"])
        self.assertNotIn("作废提议的子问题", value["tree"])
        self.assertNotIn('data-analysis-act="convert"', value["body"])

    def test_action_errors_use_current_language_without_private_identifiers(self):
        parts = [('// ---------- Analysis view helpers ----------', '// ---------- Analysis view DOM ----------'),
                 ('async function analysisAction(', 'async function startAnalysis(')]
        for code in ["round-not-answerable", "note-in-progress", "item-not-in-round", "future-error"]:
            with self.subTest(code=code):
                setup = """
                    let analysisBusy=false,analysisRequest=0,analysisRenderKey='',analysisState=null;
                    const FILE=false,analysisSupported=true,mode='server',analysisSelected='private-analysis',receiver=null;
                    let notice=''; const renderAnalysis=()=>{},analysisRouteLabels=()=>{},resync=async()=>{},loadAnalyses=async()=>{},toast=t=>notice=t;
                    const api=async()=>{throw Object.assign(new Error('内部 private-note n_private analysis_private'),{status:400,code:CODE})};
                """.replace("CODE", json.dumps(code))
                value = interaction_program(parts, setup, "await analysisAction('answer',{round:1});return {error:$('analysisError').textContent,notice};")
                self.assertNotIn("private", value["error"] + value["notice"])
                self.assertNotIn("内部", value["error"] + value["notice"])
                self.assertNotIn("viewer.analysis.", value["error"] + value["notice"])
                self.assertTrue(value["error"])

    def test_question_escapes_original_text_and_hides_internal_identifiers(self):
        state = {"nodes": {"n_private": {"id": "n_private", "text": '<img src=x onerror="bad()">口径？',
                  "short": "口径", "recommend": "Y_private", "options": {"Y_private": "年度", "Q": "季度"},
                  "parent": None, "status": "pending", "decision": None}}, "decisions": {},
                 "rounds": [{"seq": 7, "status": "open", "items": [{"key": "Q:n_private", "type": "question", "node": "n_private"}],
                             "answers": {}, "proposals": []}]}
        rendered = render_expression("analysisQuestionHtml(state,state.rounds[0],state.rounds[0].items[0])", state)
        self.assertNotIn("<img", rendered)
        self.assertIn("&lt;img", rendered)
        visible = html.unescape(re.sub(r"<[^>]*>", "", rendered))
        self.assertIn("年度", visible)
        self.assertIn("Use recommended answer", visible)
        self.assertNotIn("n_private", visible)
        self.assertNotIn("Y_private", visible)

    def test_answer_history_uses_question_and_answer_text(self):
        state = {"nodes": {"n1": {"text": "如何核验？", "short": "核验方式", "recommend": "annual", "options": {"annual": "年度材料", "quarter": "季度材料"}}},
                 "rounds": [{"proposals": [{"key": "p1", "text": "先确定口径", "short": "确定口径"}]}]}
        value = render_expression("[analysisAnswerLabel(state,state.rounds[0],'S:p1',{kind:'accept'}),analysisAnswerLabel(state,state.rounds[0],'Q:n1',{kind:'option',option:'quarter'})]", state)
        self.assertIn("先确定口径", value[0])
        self.assertIn("季度材料", value[1])
        self.assertNotIn("S:p1", " ".join(value))
        self.assertNotIn("Q:n1", " ".join(value))

    def test_question_controls_follow_current_language(self):
        state = {"nodes": {}, "decisions": {}, "rounds": [{"seq": 2, "status": "open", "answers": {},
                 "items": [{"key": "S:p1", "type": "structure", "proposal": {"key": "p1", "text": "比较谁？", "short": "对比对象", "recommend": "a", "options": [{"id": "a", "label": "同业"}]}}]}]}
        zh = render_expression("analysisQuestionHtml(state,state.rounds[0],state.rounds[0].items[0])", state, "zh-CN")
        en = render_expression("analysisQuestionHtml(state,state.rounds[0],state.rounds[0].items[0])", state, "en")
        self.assertIn("确认提议", zh)
        self.assertIn("Confirm proposal", en)
        self.assertIn("比较谁？", zh)
        self.assertIn("比较谁？", en)


if __name__ == "__main__":
    unittest.main()
