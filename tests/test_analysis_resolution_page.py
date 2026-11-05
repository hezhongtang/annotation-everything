"""#115 分析收尾：通过真实JS呈现、事件和传输接缝验证。"""
from __future__ import annotations

import html
import json
import re
import unittest

from tests.test_analysis_page import interaction_program, render_expression
from tests.test_analysis_protection_page import protection_state


def resolution_state():
    state = protection_state()
    state["assumptions"] = {"a_private": {"id": "a_private", "text": "合成可比前提 <script>",
                                           "confirmed": {"main": "confirmed"}, "refuted": False}}
    state["deadEnds"] = {"e_private": {"id": "e_private", "key": "candidate_private", "kind": "option",
                                        "node": "n1", "option": "b", "reason": "季度不可比 <img>",
                                        "assumptions": ["a_private"], "line": "main", "status": "pending"}}
    state["lines"] = {"main": {"id": "main", "ordinal": 1, "nodes": state["nodes"]}}
    state["rounds"][0]["items"] = [{"key": "D:e_private", "type": "deadend", "deadEnd": "e_private"}]
    return state


def render_resolution(state, locale="en"):
    setup = """
      let analysisState=STATE,analysisBusy=false,analysisRenderKey='';
      const analyses=[{id:analysisState.id,focus:analysisState.focus,status:analysisState.status}],analysisSelected=analysisState.id;
      const analysisShown=()=>true,analysisRouteLabels=()=>{},analysisDraftFor=()=>({});
    """.replace("STATE", json.dumps(state, ensure_ascii=False))
    return interaction_program([
        ("// ---------- Analysis view helpers ----------", "// ---------- Analysis view DOM ----------"),
        ("function renderAnalysis(){", "async function analysisAction("),
    ], setup, """
      renderAnalysis();return Object.fromEntries(['analysisBody','analysisTree','analysisAssumptions','analysisTimeline']
        .map(id=>[id,$(id).innerHTML]));
    """, locale)


class AnalysisResolutionPageTests(unittest.TestCase):
    def test_claimed_dead_end_proposal_shows_reason_dependencies_and_enabled_confirmation(self):
        for locale in ("en", "zh-CN"):
            view = render_resolution(resolution_state(), locale)
            body = view["analysisBody"]
            self.assertIn("季度不可比 &lt;img&gt;", body)
            self.assertIn("合成可比前提 &lt;script&gt;", body)
            self.assertIn("季度", body)
            controls = re.findall(r'<button[^>]*data-key="D:e_private"[^>]*>', body)
            self.assertEqual(len(controls), 2)
            self.assertTrue(all(" disabled" not in control for control in controls))
            self.assertNotIn('data-kind="text"', body)
            visible = html.unescape(re.sub(r"<[^>]*>", "", body))
            self.assertNotIn("private", visible)
            self.assertNotIn("<script>", body)
            label = render_expression("analysisAnswerLabel(state,state.rounds[0],'D:e_private',{kind:'accept'})", resolution_state(), locale)
            self.assertIn("季度", label)
            self.assertNotIn("private", label)

    def test_refutation_and_review_states_are_natural_readonly_when_locked_and_send_exact_target(self):
        state = resolution_state()
        state["nodes"]["n1"]["needsReview"] = True
        state["lines"]["main"]["basedOnRefuted"] = ["a_private"]
        state["deadEnds"]["e_private"]["status"] = "review"
        for locale in ("en", "zh-CN"):
            for locked, closed in ((True, False), (False, False), (False, True)):
                state["protection"]["locked"] = locked
                state["status"] = "closed" if closed else "active"
                view = render_resolution(state, locale)
                button = re.search(r'<button[^>]*data-analysis-refute="a_private"[^>]*>', view["analysisAssumptions"])
                self.assertIsNotNone(button)
                self.assertEqual(locked or closed, " disabled" in button.group())
                self.assertIn("needs review" if locale == "en" else "待复核", view["analysisTree"].lower())
                self.assertIn("refuted assumption" if locale == "en" else "已推翻的假设", view["analysisTimeline"])
                article = re.search(r'<article[^>]*data-analysis-node="n1"[^>]*>.*?</article>', view["analysisBody"], re.S).group()
                self.assertNotIn('data-analysis-act="convert"', article)
                self.assertIn("Released" if locale == "en" else "已解除", view["analysisAssumptions"])
                self.assertNotIn("private", html.unescape(re.sub(r"<[^>]*>", "", " ".join(view.values()))))
        setup = """
          let analysisState=STATE,analysisBusy=false;const calls=[],notices=[];
          const analysisAction=async(op,body)=>calls.push({op,body}),toast=text=>notices.push(text);
        """.replace("STATE", json.dumps(resolution_state(), ensure_ascii=False))
        result = interaction_program([
            ("// ---------- Analysis view helpers ----------", "// ---------- Analysis view DOM ----------"),
            ("function analysisResolutionClick(", "// ---------- Analysis resolution events end ----------"),
        ], setup, """
          await analysisResolutionClick({target:{closest:()=>({dataset:{analysisRefute:'a_private'},disabled:false})}});
          analysisState.protection.locked=false;
          await analysisResolutionClick({target:{closest:()=>({dataset:{analysisRefute:'a_private'},disabled:false})}});
          return {calls,notices};
        """)
        self.assertEqual(result["calls"], [{"op": "refute", "body": {"assumption": "a_private"}}])
        self.assertTrue(result["notices"])

    def test_dead_end_errors_keep_safe_params_and_use_the_current_page_language(self):
        for locale in ("en", "zh-CN"):
            value = interaction_program([
                ("// ---------- Analysis view helpers ----------", "// ---------- Analysis view DOM ----------"),
                ("async function api(", "// 只认同一份审阅的服务"),
            ], """
              const APP='annotation-everything',RID='review',REQ_TIMEOUT=5000,wellFormed=value=>value;
              const fetch=async()=>({ok:false,status:409,json:async()=>({app:APP,code:'dead-end-option',error:'服务内部 private-id',params:{reason:'合成路径缺少可比范围',assumptions:['合成两期口径一致']}})});
            """, """
              try{await api('POST','/api/analysis/redecide',{})}catch(error){
                return {message:analysisErrorText(error),malformed:analysisErrorText({status:409,code:'dead-end-line',params:{reason:{private:1},assumptions:[42,null]}})}
              }
            """, locale)
            self.assertIn("合成路径缺少可比范围", value["message"])
            self.assertIn("合成两期口径一致", value["message"])
            self.assertIn("refute" if locale == "en" else "推翻", value["message"].lower())
            for text in value.values():
                self.assertNotIn("private", text)
                self.assertNotIn("viewer.", text)
                self.assertNotIn("[object Object]", text)

    def test_successful_actions_render_actual_changes_and_keep_refutation_sentence_translatable(self):
        before = resolution_state()
        before["protection"]["locked"] = False
        after = json.loads(json.dumps(before))
        after["line"] = "line_private"
        after["lines"]["line_private"] = {"id": "line_private", "ordinal": 2, "nodes": after["nodes"]}
        after["assumptions"]["a_private"]["refuted"] = True
        for locale in ("en", "zh-CN"):
            setup = """
              let analysisState=BEFORE,analysisSelected=analysisState.id,analysisBusy=false,analysisRequest=0,analysisRenderKey='',analysisLastChange=null;
              const FILE=false,analysisSupported=true,mode='server',receiver={bindingId:'test-binding'},SS={set(){}},RID='test-review';
              const notices=[],renderAnalysis=()=>{},analysisRouteLabels=()=>{},resync=async()=>{},loadAnalyses=async()=>{},toast=text=>notices.push(text);
              const api=async()=>({analysis:AFTER});
            """.replace("BEFORE", json.dumps(before, ensure_ascii=False)).replace("AFTER", json.dumps(after, ensure_ascii=False))
            value = interaction_program([
                ("// ---------- Analysis view helpers ----------", "// ---------- Analysis view DOM ----------"),
                ("async function analysisAction(", "async function startAnalysis("),
            ], setup, """
              await analysisAction('refute',{assumption:'a_private'});
              const html=analysisChangeHtml(analysisState);
              const ops=['start','answer','submit','redecide','convert','confirm','release','activate','pause','update-note','keep-note','delete-note','seal','fork','switch-line','close','reopen'];
              return {html,notices,others:ops.map(op=>{const change=analysisChangeData(op,analysisState,analysisState,{key:'Q:n1',value:{kind:'accept'},node:'n1'});return tMsg(change.key,change.params)})};
            """, locale)
            self.assertIn("合成可比前提 &lt;script&gt;", value["html"])
            self.assertIn("Second line" if locale == "en" else "第二条线", value["html"])
            self.assertIn("refuted" if locale == "en" else "推翻", value["html"].lower())
            self.assertNotIn("private", html.unescape(re.sub(r"<[^>]*>", "", value["html"])))
            self.assertTrue(all("viewer." not in text and text.strip() for text in value["others"]))

    def test_page_cards_remap_stable_keys_and_preview_embedded_page_without_changing_selection(self):
        state = resolution_state()
        state["sourceCopy"] = {"key": "deleted_private", "page": 1, "text": "旧源原话"}
        state["linkedNotes"] = [{"id": "note_private", "key": "kept_private", "page": 1, "text": "合成批注"}]
        setup = """
          let analysisState=STATE,analysisBusy=false,i=1;const notices=[],toast=text=>notices.push(text);
          const P=[{n:1,key:'other_private',title:'另一合成页'},{n:3,key:'kept_private',title:'保留的合成页'}];
          const SECT=['<section>另一合成页</section>','<section>正确的内嵌合成报告</section>'],RCSS='.page{color:navy}';
          const root={innerHTML:'',getElementById(id){this.nodes??={};return this.nodes[id]??=( {style:{}} )}};
          const host=$('analysisPageHost');host.attachShadow=()=>host.shadowRoot=root;
          $('analysisPageWrap').getBoundingClientRect=()=>({width:640,height:400});
          $('analysisPageDialog').classList={add(){this.open=true},contains(){return !!this.open},remove(){this.open=false}};
          $('host').lang='zh-CN';
        """.replace("STATE", json.dumps(state, ensure_ascii=False))
        value = interaction_program([
            ("// ---------- Analysis view helpers ----------", "// ---------- Analysis view DOM ----------"),
            ("// ---------- Analysis page preview interactions ----------", "// ---------- Analysis page preview interactions end ----------"),
        ], setup, """
          const cards=analysisPageCardsHtml(analysisState);
          openAnalysisPage(3);
          return {cards,html:root.innerHTML,title:$('analysisPageTitle').textContent,i,line:analysisState.line,lang:host.lang,
            scale:root.getElementById('ae-stage').style.transform};
        """)
        self.assertIn('data-analysis-page="3"', value["cards"])
        self.assertNotIn('data-analysis-page="1"', value["cards"])
        self.assertIn("unavailable", value["cards"].lower())
        self.assertIn("正确的内嵌合成报告", value["html"])
        self.assertNotIn("<iframe", value["html"])
        self.assertIn("保留的合成页", value["title"])
        self.assertEqual((value["i"], value["line"], value["lang"]), (1, "main", "zh-CN"))
        self.assertEqual(value["scale"], "scale(0.5)")
        self.assertNotIn("private", html.unescape(re.sub(r"<[^>]*>", "", value["cards"])))

    def test_reopened_questions_and_historical_dead_end_text_keep_the_original_reason(self):
        state = resolution_state()
        state["nodes"]["n1"].update(status="pending", decision=None,
                                    reopened={"answer": "原合成答案 <img>", "assumption": "a_private"})
        state["rounds"][0]["items"] = [{"key": "Q:n1", "type": "question", "node": "n1"}]
        context = {"round": 9, "line": "main", "historical": True,
                   "items": [{"key": "D:old_private", "type": "deadend", "kind": "line", "targetLine": "line_private",
                              "targetOrdinal": 2, "reason": "当时的死局理由 <script>", "assumptionTexts": ["当时的假设原话"]}],
                   "answers": {"D:old_private": {"kind": "accept", "verbatim": "确认的完整原话"}},
                   "writeback": {"deadEnds": [{"kind": "option", "node": "n1", "option": "b", "reason": "Agent 当时的提议原文"}]}}
        for locale in ("en", "zh-CN"):
            body = render_resolution(state, locale)["analysisBody"]
            self.assertIn("原合成答案 &lt;img&gt;", body)
            self.assertIn("合成可比前提 &lt;script&gt;", body)
            self.assertIn("reopened" if locale == "en" else "重问", body.lower())
            history = render_expression("analysisRoundHistoryHtml(state," + json.dumps(context, ensure_ascii=False) + ")", state, locale)
            for text in ("当时的死局理由 &lt;script&gt;", "当时的假设原话", "确认的完整原话", "Agent 当时的提议原文"):
                self.assertIn(text, history)
            self.assertIn("Second line" if locale == "en" else "第二条线", history)
            self.assertNotIn("private", html.unescape(re.sub(r"<[^>]*>", "", history)))
            self.assertNotIn('data-analysis-act=', history)

    def test_change_sentences_report_note_effects_and_do_not_resolve_a_released_source(self):
        state = resolution_state()
        state.update(sourceNote="source_private", sourceState="released",
                     linkedNotes=[{"id": "held_private", "status": "open"}, {"id": "restored_private", "status": "held"}])
        after = json.loads(json.dumps(state))
        after["linkedNotes"] = [{"id": "held_private", "status": "held", "hold": "结论已变"},
                                {"id": "restored_private", "status": "open"}]
        for locale in ("en", "zh-CN"):
            text = render_expression("analysisChangeText(analysisChangeData('confirm',state," + json.dumps(after, ensure_ascii=False) + "))", state, locale)
            self.assertIn("revision submission" if locale == "en" else "改稿", text)
            self.assertIn("1 annotation(s) held" if locale == "en" else "1 条批注", text)
            self.assertIn("1 restored" if locale == "en" else "1 条恢复", text)
            self.assertNotIn("source annotation resolved" if locale == "en" else "源批注", text)
if __name__ == "__main__":
    unittest.main()
