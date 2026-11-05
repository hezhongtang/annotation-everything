"""#114 页面公开呈现与交互：历史只读、自然线名和检查点。"""
from __future__ import annotations

import html
import json
import re
import unittest

from tests.test_analysis_page import interaction_program


def history_state():
    node = {"id": "n_private", "text": "合成口径该如何核对？", "short": "核对口径", "status": "decided",
            "options": {"o_private": "全年"}, "recommend": "o_private", "decision": "d_private"}
    context = {"analysis": "a_private", "round": 2, "line": "main", "status": "done", "historical": True,
               "note": "Agent 原文 <script>第二行</script>",
               "items": [{"key": "Q:n_private", "type": "question", "node": "n_private", "text": node["text"],
                          "options": [{"id": "o_private", "label": "全年"}], "recommend": "o_private"}],
               "answeredItems": [], "answers": {"Q:n_private": {"kind": "accept", "verbatim": "采用全年原话"}},
               "writeback": {"proposals": [], "assumptions": [], "facts": [{"text": "合成底稿确认为120", "source": "合成底稿"}], "updates": []}}
    snap = {"focus": "合成焦点", "nodes": {"n_private": node},
            "decisions": {"d_private": {"id": "d_private", "label": "全年", "round": 2}},
            "conclusions": ["d_private"], "unresolved": [], "parked": [],
            "assumptions": {"a_private": {"text": "两期范围相同", "confirmed": {"main": "confirmed"}}}, "facts": {}}
    return {"id": "a_private", "focus": "合成焦点", "status": "active", "line": "line_private",
            "sourceState": "none", "sourceCopy": {}, "nodes": {"n_private": node},
            "decisions": snap["decisions"], "drafts": [], "protection": {"locked": False},
            "lines": {"main": {"id": "main", "ordinal": 1, "from": None},
                      "line_private": {"id": "line_private", "ordinal": 2, "from": "cp_private"}},
            "checkpoints": [{"id": "cp_private", "line": "main", "round": 2, "auto": False, "sealed": True, "snapshot": snap}],
            "rounds": [{"seq": 2, "line": "main", "status": "done", "context": context, "answers": context["answers"]},
                       {"seq": 3, "line": "line_private", "status": "open", "items": [], "answers": {}}]}


def render_history(state, locale="en", selection=None):
    setup = """
      let analysisState=STATE,analysisBusy=false,analysisRenderKey='',analysisHistorySelection=SELECTION;
      const analyses=[],analysisSelected=analysisState.id,analysisShown=()=>true,analysisRouteLabels=()=>{},analysisDraftFor=()=>({});
    """.replace("STATE", json.dumps(state, ensure_ascii=False)).replace("SELECTION", json.dumps(selection, ensure_ascii=False))
    return interaction_program([
        ("// ---------- Analysis view helpers ----------", "// ---------- Analysis view DOM ----------"),
        ("function renderAnalysis(){", "async function analysisAction("),
    ], setup, "renderAnalysis();return Object.fromEntries(['analysisBody','analysisManage','analysisMeta','analysisTimeline'].map(id=>[id,$(id).innerHTML||$(id).textContent]));", locale)


class AnalysisHistoryPageTests(unittest.TestCase):
    def test_history_protection_pending_close_and_new_errors_are_natural_in_both_languages(self):
        for locale in ("en", "zh-CN"):
            state = history_state()
            state["protection"]["locked"] = True
            view = render_history(state, locale)
            for op in ("fork", "switch-line"):
                self.assertRegex(view["analysisTimeline"], r'data-analysis-history-act="' + op + r'"[^>]* disabled')
            self.assertNotIn('data-analysis-history-act="seal"', view["analysisManage"])
            state["protection"]["locked"] = False
            state["rounds"][-1]["status"] = "submitted"
            pending = render_history(state, locale)
            self.assertRegex(pending["analysisManage"], r'data-analysis-history-act="close"[^>]* disabled')
            self.assertIn("must finish" if locale == "en" else "处理完后", pending["analysisManage"])
            state["status"] = "closed"
            closed = render_history(state, locale)
            self.assertIn('data-analysis-history-act="reopen"', closed["analysisManage"])
            for op in ("answer", "redecide", "convert", "confirm", "release"):
                self.assertNotIn('data-analysis-act="' + op + '"', closed["analysisBody"])
            result = interaction_program([
                ("// ---------- Analysis view helpers ----------", "// ---------- Analysis view DOM ----------"),
            ], "const analysisBusy=false;", "return ['checkpoint-not-found','line-not-found','analysis-pending-rounds'].map(code=>analysisErrorText({code,status:400,message:'内部 private'}));", locale)
            for message in result:
                self.assertNotIn("viewer.", message)
                self.assertNotIn("private", message)
                self.assertNotIn("内部", message)

    def test_closed_analysis_opens_readonly_and_polling_keeps_history_and_answer_drafts(self):
        state = history_state()
        closed = {**state, "id": "closed_private", "status": "closed"}
        setup = """
          let analysisState=STATE,analysisSelected=analysisState.id,analysisHistorySelection={analysis:analysisState.id,kind:'round',id:2},analysisHistoryRequest=0,analysisBusy=false,analysisRequest=0,analysisRenderKey='',analysisLoading=false,analysisContext=null,analysisQueue={};
          let analyses=[{id:analysisState.id,status:'active'},{id:'closed_private',status:'closed'}];
          const FILE=false,analysisSupported=true,mode='server',SS={set(){}},RID='review',location={hash:'#analysis'};
          const calls=[],analysisAnswerDrafts=new Map([['live-round',{text:'尚未保存的完整原话'}]]);let stashed=0;
          const analysisShown=()=>true,analysisRoute=()=>{},renderAnalysis=()=>{},analysisRouteLabels=()=>{},stashAnalysisInputs=()=>stashed++;
          const api=async(method,path,body)=>{calls.push({method,path,body});return path==='/api/analysis'?{analyses}:{analysis:CLOSED,context:null}};
        """.replace("STATE", json.dumps(state, ensure_ascii=False)).replace("CLOSED", json.dumps(closed, ensure_ascii=False))
        value = interaction_program([
            ("// ---------- Analysis view helpers ----------", "// ---------- Analysis view DOM ----------"),
            ("async function loadAnalyses(){", "function analysisRouteLabels(){"),
            ("async function openAnalysis(", "function openNewAnalysis("),
        ], setup, """
          await openAnalysis('closed_private');const cleared=analysisHistorySelection;
          analysisHistorySelection={analysis:'closed_private',kind:'round',id:2,context:{note:'冻结原文'}};
          await loadAnalyses();return {calls,cleared,selected:analysisSelected,status:analysisState.status,history:analysisHistorySelection,drafts:Array.from(analysisAnswerDrafts.values()),stashed};
        """)
        self.assertTrue(all(call["method"] == "GET" for call in value["calls"]))
        self.assertIsNone(value["cleared"])
        self.assertEqual(value["selected"], "closed_private")
        self.assertEqual(value["status"], "closed")
        self.assertEqual(value["history"]["context"]["note"], "冻结原文")
        self.assertEqual(value["drafts"], [{"text": "尚未保存的完整原话"}])
        self.assertEqual(value["stashed"], 1)

    def test_timeline_clicks_read_history_without_switching_and_send_exact_action_targets(self):
        state = history_state()
        context = state["rounds"][0].pop("context")
        setup = """
          let analysisState=STATE,analysisSelected=analysisState.id,analysisHistorySelection=null,analysisHistoryRequest=0,analysisBusy=false,analysisRequest=0,analysisRenderKey='';
          const FILE=false,analysisSupported=true,mode='server',receiver={bindingId:'binding'},SS={set(){}},RID='review';
          const calls=[],notices=[];let stashed=0;
          const renderAnalysis=()=>{},analysisRouteLabels=()=>{},stashAnalysisInputs=()=>stashed++,resync=async()=>{},loadAnalyses=async()=>{},toast=text=>notices.push(text);
          const api=async(method,path,body)=>{calls.push({method,path,body});return method==='GET'?{context:CONTEXT}:{}};
        """.replace("STATE", json.dumps(state, ensure_ascii=False)).replace("CONTEXT", json.dumps(context, ensure_ascii=False))
        value = interaction_program([
            ("// ---------- Analysis view helpers ----------", "// ---------- Analysis view DOM ----------"),
            ("// ---------- Analysis history interactions ----------", "// ---------- Analysis history events ----------"),
            ("// ---------- Analysis history events ----------", "// ---------- Analysis history events end ----------"),
            ("async function analysisAction(", "async function startAnalysis("),
        ], setup, """
          const click=dataset=>({target:{closest(selector){return selector==='[data-analysis-status]'?null:{dataset,disabled:false}}}});
          await $('analysisTimeline').onclick(click({historyRound:'2'}));
          const selected=analysisHistorySelection,stillCurrent=analysisState.line;
          await $('analysisTimeline').onclick(click({historyCheckpoint:'cp_private'}));const checkpoint=analysisHistorySelection.kind;
          await $('analysisManage').onclick(click({historyCurrent:''}));const back=analysisHistorySelection;
          await $('analysisTimeline').onclick(click({analysisHistoryAct:'seal',checkpoint:'cp_private'}));
          await $('analysisTimeline').onclick(click({analysisHistoryAct:'fork',checkpoint:'cp_private'}));
          await $('analysisTimeline').onclick(click({analysisHistoryAct:'switch-line',line:'main'}));
          await $('analysisManage').onclick(click({analysisHistoryAct:'close'}));
          analysisState.status='closed';await $('analysisManage').onclick(click({analysisHistoryAct:'reopen'}));
          return {calls,stashed,selected,stillCurrent,checkpoint,back,notices};
        """)
        self.assertEqual(value["stillCurrent"], "line_private")
        self.assertEqual(value["selected"]["context"]["note"], context["note"])
        self.assertEqual(value["checkpoint"], "checkpoint")
        self.assertIsNone(value["back"])
        self.assertGreaterEqual(value["stashed"], 1)
        self.assertEqual(value["calls"][0]["method"], "GET")
        self.assertIn("round=2", value["calls"][0]["path"])
        writes = value["calls"][1:]
        self.assertEqual([x["path"] for x in writes], ["/api/analysis/seal", "/api/analysis/fork", "/api/analysis/switch-line", "/api/analysis/close", "/api/analysis/reopen"])
        self.assertEqual(writes[0]["body"]["checkpoint"], "cp_private")
        self.assertEqual(writes[1]["body"]["checkpoint"], "cp_private")
        self.assertEqual(writes[2]["body"]["line"], "main")
        self.assertTrue(all(x["body"]["receiverBindingId"] == "binding" for x in writes))
        self.assertTrue(all("viewer." not in text for text in value["notices"]))

    def test_history_and_closed_state_cannot_submit_or_mutate_answers_but_can_reopen(self):
        state = history_state()
        setup = """
          let analysisState=STATE,analysisSelected=analysisState.id,analysisHistorySelection={analysis:analysisState.id,kind:'round',id:2},analysisBusy=false,analysisRequest=0,analysisRenderKey='';
          const FILE=false,analysisSupported=true,mode='server',receiver={bindingId:'binding'},SS={set(){}},RID='review';
          const calls=[],notices=[],renderAnalysis=()=>{},analysisRouteLabels=()=>{},resync=async()=>{},loadAnalyses=async()=>{},toast=text=>notices.push(text);
          const api=async(method,path,body)=>{calls.push({method,path,body});return {}};
        """.replace("STATE", json.dumps(state, ensure_ascii=False))
        result = interaction_program([
            ("// ---------- Analysis view helpers ----------", "// ---------- Analysis view DOM ----------"),
            ("async function analysisAction(", "async function startAnalysis("),
            ("function submitAnalysis(){", '$("annotationMode").onclick='),
        ], setup, """
          await submitAnalysis();await analysisAction('answer',{round:2,key:'Q:n_private',value:{kind:'accept'}});
          analysisHistorySelection=null;analysisState.status='closed';
          await submitAnalysis();await analysisAction('convert',{node:'n_private'});await analysisAction('reopen');
          return {calls,notices};
        """)
        self.assertEqual([call["path"] for call in result["calls"]], ["/api/analysis/reopen"])
        self.assertEqual(result["calls"][0]["body"]["receiverBindingId"], "binding")
        self.assertTrue(all("viewer." not in notice and "_private" not in notice for notice in result["notices"]))

    def test_timeline_shows_natural_lines_forks_and_checkpoint_actions(self):
        for locale in ("en", "zh-CN"):
            view = render_history(history_state(), locale)
            self.assertIn("Second line" if locale == "en" else "第二条线", view["analysisMeta"])
            self.assertIn("Main line" if locale == "en" else "主线", view["analysisTimeline"])
            self.assertIn("Checkpoint after round 2" if locale == "en" else "检查点（第 2 轮后）", view["analysisTimeline"])
            for action in ("seal", "close"):
                self.assertIn('data-analysis-history-act="' + action + '"', view["analysisManage"])
            self.assertIn('data-analysis-history-act="fork"', view["analysisTimeline"])
            self.assertIn('data-analysis-history-act="switch-line"', view["analysisTimeline"])
            timeline=view["analysisTimeline"]
            self.assertLess(timeline.index("Round 2" if locale=="en" else "第 2 轮"), timeline.index("Checkpoint after round 2" if locale=="en" else "检查点（第 2 轮后）"))
            self.assertLess(timeline.index("Checkpoint after round 2" if locale=="en" else "检查点（第 2 轮后）"), timeline.index("Second line" if locale=="en" else "第二条线"))
            visible = html.unescape(re.sub(r"<[^>]*>", "", " ".join(view.values())))
            self.assertNotIn("_private", visible)

    def test_checkpoint_detail_shows_sealed_decisions_pending_parked_and_assumptions(self):
        state = history_state()
        snap = state["checkpoints"][0]["snapshot"]
        snap["nodes"]["pending_private"] = {"text": "尚未回答 <b>完整问题</b>", "status": "pending", "options": {"x": "待核对选项"}}
        snap["nodes"]["parked_private"] = {"text": "另一个搁置问题", "status": "parked", "options": {}}
        snap["unresolved"] = ["pending_private"]
        snap["parked"] = ["parked_private"]
        snap["pendingProposals"] = [{"key": "p_private", "text": "尚未确认的合成结构提议", "options": [{"id": "o_private", "label": "建议保留"}], "recommend": "o_private"}]
        selection = {"analysis": state["id"], "kind": "checkpoint", "id": "cp_private"}
        for locale in ("en", "zh-CN"):
            body = render_history(state, locale, selection)["analysisBody"]
            for text in ("全年", "尚未回答 &lt;b&gt;完整问题&lt;/b&gt;", "另一个搁置问题", "两期范围相同", "尚未确认的合成结构提议", "建议保留"):
                self.assertIn(text, body)
            self.assertIn("Decided in round 2" if locale == "en" else "来自第 2 轮", body)
            self.assertNotIn('data-analysis-act=', body)
            self.assertNotIn("<b>", body)
            self.assertNotIn("_private", html.unescape(re.sub(r"<[^>]*>", "", body)))

    def test_history_round_shows_original_text_readonly_and_keeps_its_own_answers(self):
        state = history_state()
        context = state["rounds"][0]["context"]
        state["nodes"]["n_private"]["text"] = "未来才改变的问题"
        state["nodes"]["n_private"]["options"] = {"future_private": "未来的新选项"}
        selection = {"analysis": state["id"], "kind": "round", "id": 2, "context": context}
        for locale in ("en", "zh-CN"):
            view = render_history(state, locale, selection)
            body = view["analysisBody"]
            self.assertIn("合成口径该如何核对？", body)
            self.assertIn("采用全年原话", body)
            self.assertIn("Agent 原文 &lt;script&gt;第二行&lt;/script&gt;", body)
            self.assertIn("合成底稿确认为120", body)
            self.assertNotIn("未来", body)
            self.assertNotIn('data-analysis-act=', body)
            self.assertNotIn("<textarea", body)
            self.assertNotIn("<script>", body)
            self.assertIn('data-history-current', view["analysisManage"])
            visible = html.unescape(re.sub(r"<[^>]*>", "", body))
            self.assertNotIn("_private", visible)
