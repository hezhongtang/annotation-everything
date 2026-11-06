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
    def test_old_service_upgrade_notice_survives_the_connection_poll(self):
        setup = """
          let mode='offline',rev=null,busy=0,agentDelivery=null,receiver=null,archiveBusy=false,srvOld=false;
          let srvNoStages=false,srvNoFullCheck=false,stopServiceSupported=false,idx0=null,queueSnap=null,resumeReady=false;
          let analysisSupported=false,analysisChecked=false,analysisUpgradeShown=false,analysisRenderKey='';
          let features=['marks','stages','full-check'];const FILE=false,document={hidden:false};
          const pend={get:()=>[]},failed={get:()=>[]},hasFeature=(data,name)=>data.features.includes(name);
          const getNotes=async()=>({notes:[],rev:1,features}),setDoc=data=>{rev=data.rev};
          const toast=text=>{$('toast').textContent=text};
          const migrateLegacyQueue=()=>{},setCurrentArchive=()=>{},updateMode=()=>{},refreshList=()=>{};
          const renderHand=()=>{},renderResume=()=>{},analysisRoute=()=>{};
        """
        for locale in ("en", "zh-CN"):
            actual = interaction_program([
                ("let handPollKey=", "// ---------- 服务不在时"),
                ("function setAnalysisSupport(data){", "async function loadAnalyses("),
            ], setup, """
              const notices=[];
              for(const [base,supported] of [[null,false],[1,false],[1,true]]){
                rev=base;mode='offline';analysisSupported=true;analysisUpgradeShown=false;
                features=['marks','stages','full-check',...(supported?['analysis']:[])];
                $('toast').textContent='';await poll();notices.push($('toast').textContent);
                await poll();notices.push($('toast').textContent);
              }
              return notices;
            """, locale)
            messages = i18n.load_catalog(locale)["messages"]
            expected = [messages["viewer.analysis.upgrade"], messages["viewer.analysis.upgrade"],
                        messages["viewer.offline.reconnected"]]
            self.assertEqual(actual, [message for message in expected for _ in range(2)])

    def test_pending_annotations_sync_without_overwriting_the_old_service_upgrade_notice(self):
        setup = """
          const APP='annotation-everything',RID='synthetic-review',FILE=false,navigator={},document={hidden:false};
          const storage=new Map(),copy=value=>JSON.parse(JSON.stringify(value));
          const LS={get:(key,fallback)=>copy(storage.has(key)?storage.get(key):fallback),set:(key,value)=>{storage.set(key,copy(value));return true}};
          const K={pending:'pending',legacyPending:'legacy',failed:'failed'},MAX_FAILS=3;
          let mode='offline',rev=1,busy=0,agentDelivery=null,receiver=null,archiveBusy=false,srvOld=false;
          let srvNoStages=false,srvNoFullCheck=false,stopServiceSupported=false,idx0=null,resumeReady=false,handQueued=false;
          let analysisSupported=true,analysisChecked=false,analysisUpgradeShown=false,analysisRenderKey='';
          let notes=[],remote=[],remoteRev=1,supported=false;const writes=[],notices=[];
          const api=async(method,path,body)=>{
            if(method==='POST'&&path==='/api/notes'){writes.push(copy(body));remote.push(copy(body));remoteRev++}
            else if(method!=='GET'||path!=='/api/notes')throw new Error('unexpected network request');
            return {app:APP,reviewId:RID,notes:copy(remote),rev:remoteRev,features:['marks','stages','full-check',...(supported?['analysis']:[])]};
          };
          const toast=text=>{$('toast').textContent=text;notices.push(text)},setDoc=data=>{rev=data.rev;notes=data.notes};
          const updateMode=()=>{},setCurrentArchive=()=>{},refreshList=()=>{},renderHand=()=>{},renderResume=()=>{},analysisRoute=()=>{};
        """
        pending = [{"kind": "add", "opId": "op" + str(number),
                    "note": {"id": "note" + str(number), "page": 1, "text": text}}
                   for number, text in enumerate(("合成离线原话 <b>完整保留</b>", "第二条合成离线批注"), 1)]
        for locale in ("en", "zh-CN"):
            actual = interaction_program([
                ("const sameReview=", "function mirror()"),
                ("function applyLocal(op){", "const FULL_CHECK_COMPAT="),
                ("function send(op,", "// 一次轮询里队列"),
                ("let queueSnap=null;", "// 轮询带回的"),
                ("let handPollKey=", "// ---------- 服务不在时"),
                ("function setAnalysisSupport(data){", "async function loadAnalyses("),
            ], setup, """
              const outcomes=[],pending=PENDING;
              for(const enabled of [false,true]){
                supported=enabled;mode='offline';rev=1;remoteRev=1;remote=[];notes=[];writes.length=0;notices.length=0;
                analysisSupported=true;analysisUpgradeShown=false;LS.set(K.pending,pending);
                await poll();const afterSync=$('toast').textContent;
                await poll();outcomes.push({supported,afterSync,afterPoll:$('toast').textContent,
                  pending:pend.get(),writes:copy(writes),remote:copy(remote),notes:copy(notes),notices:copy(notices)});
              }
              return outcomes;
            """.replace("PENDING", json.dumps(pending, ensure_ascii=False)), locale)
            messages = i18n.load_catalog(locale)["messages"]
            for outcome in actual:
                self.assertEqual(outcome["pending"], [])
                expected_notes = [item["note"] for item in pending]
                for key in ("writes", "remote", "notes"):
                    self.assertEqual(outcome[key], expected_notes)
                expected = messages["viewer.offline.retried" if outcome["supported"] else "viewer.analysis.upgrade"]
                self.assertEqual(outcome["afterSync"], expected)
                self.assertEqual(outcome["afterPoll"], expected)
                self.assertEqual(outcome["notices"].count(expected), 1)

    def test_free_text_and_external_permission_drafts_survive_poll_and_failed_write(self):
        state = {"id": "private-analysis", "focus": "合成问题", "status": "active", "line": "main",
                 "sourceCopy": {}, "nodes": {"n1": {"id": "n1", "text": "口径？", "short": "口径", "status": "pending",
                 "options": {"a": "全年"}, "recommend": "a"}}, "decisions": {}, "drafts": [],
                 "rounds": [{"seq": 2, "status": "open", "answers": {}, "items": [{"key": "Q:n1", "type": "question", "node": "n1"}]}]}
        setup = """
          let analysisState=STATE,analysisBusy=false,analysisRequest=0,analysisRenderKey='';const analyses=[],analysisSelected=analysisState.id;
          const FILE=false,analysisSupported=true,mode='server',receiver=null,analysisAnswerDrafts=new Map();
          const analysisShown=()=>true,analysisRouteLabels=()=>{},resync=async()=>{},loadAnalyses=async()=>{},toast=()=>{};
          const api=async()=>{throw Object.assign(new Error('网络失败'),{status:0})};
        """.replace("STATE", json.dumps(state, ensure_ascii=False))
        for locale in ("en", "zh-CN"):
            value = interaction_program([
                ("// ---------- Analysis view helpers ----------", "// ---------- Analysis view DOM ----------"),
                ("function analysisDraftKey(", "function analysisShown("),
                ("function renderAnalysis(){", "async function startAnalysis("),
            ], setup, """
                $('analysisBody').querySelectorAll=()=>[
                  {dataset:{analysisKey:'Q:n1'},hasAttribute:()=>true,value:'逐月核对 <b>完整原话</b>'},
                  {dataset:{analysisKey:'Q:n1'},hasAttribute:()=>false,checked:true}];
                stashAnalysisInputs();renderAnalysis();const polled=$('analysisBody').innerHTML;
                await analysisAction('answer',{round:2,key:'Q:n1',value:{kind:'text',text:'逐月核对 <b>完整原话</b>'}});
                return {polled,failed:$('analysisBody').innerHTML};
            """, locale)
            for rendered in value.values():
                self.assertIn("逐月核对 &lt;b&gt;完整原话&lt;/b&gt;", rendered)
                self.assertIn("checked", rendered)

    def test_structure_change_is_described_without_internal_ids(self):
        state = {"nodes": {"n_private": {"short": "统计口径"}}}
        value = render_expression("analysisQuestionHtml(state,{status:'open'},{key:'S:split',type:'structure',proposal:{key:'split',op:'split',targets:['n_private'],replacements:[{text:'收入口径',options:[{id:'private-option',label:'合成口径'}]}]}})", state)
        visible = html.unescape(re.sub(r"<[^>]*>", "", value))
        self.assertIn("Split question: 统计口径", visible)
        self.assertIn("收入口径", visible)
        self.assertNotIn("private", visible)

    def test_lifecycle_answers_and_assumptions_show_original_text_safely(self):
        state = {"nodes": {"n1": {"id": "n1", "text": "合成问题 <script>", "short": "合成问题",
                                 "options": {"a": "合成推荐"}, "recommend": "a"}},
                 "assumptions": {"a1": {"id": "a1", "text": "合成前提 <b>", "confirmed": {"main": "pending"}}}}
        expr = "analysisQuestionHtml(state,{seq:2,status:'open'}, {key:'Q:n1',type:'question',node:'n1'}, {text:'逐月核对 <b>',allowExternal:true})"
        value = render_expression(expr, state)
        for kind in ("accept", "option", "text", "park", "verify"):
            self.assertIn('data-kind="' + kind + '"', value)
        self.assertIn("逐月核对 &lt;b&gt;", value)
        self.assertIn('data-analysis-external', value)
        self.assertIn("checked", value)
        self.assertNotIn("<script>", value)
        candidate = render_expression("analysisQuestionHtml(state,{seq:2,status:'open'}, {key:'A:a1',type:'assumption',assumption:'a1'})", state)
        self.assertIn("Disagree", candidate)
        self.assertIn("合成前提 &lt;b&gt;", candidate)
        self.assertNotIn('data-kind="verify"', candidate)
        label = render_expression("analysisAnswerLabel(state,{},'Q:n1',{kind:'text',text:'完整原话'})", state)
        self.assertIn("完整原话", label)

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
        for code in ["round-not-answerable", "note-in-progress", "item-not-in-round", "analysis-invalid-input",
                     "focus-required", "page-not-found", "structure-target-changed", "analysis-closed", "future-error"]:
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
