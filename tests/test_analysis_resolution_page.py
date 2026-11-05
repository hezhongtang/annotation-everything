"""#115 分析收尾：通过真实JS呈现、事件和传输接缝验证。"""
from __future__ import annotations

import html
from html.parser import HTMLParser
import json
import re
import unittest

from tests.test_analysis_page import SCRIPTS, interaction_program, render_expression
from tests.test_review_page_i18n import _runtime_source
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
    def test_analysis_panel_toggle_renders_its_shortcut_in_text_and_title(self):
        class PanelButtonParser(HTMLParser):
            def handle_starttag(self, tag, attrs):
                values = dict(attrs)
                if tag == "button" and values.get("aria-controls") == "analysisPanelLists":
                    self.buttons.append(values)

        parser = PanelButtonParser()
        parser.buttons = []
        parser.feed((SCRIPTS / "viewer_template.html").read_text())
        self.assertEqual(len(parser.buttons), 1)
        for locale, expected in (("en", "Toggle bottom panel (\\)"), ("zh-CN", "切换底部面板（\\）")):
            setup = "const attributes=" + json.dumps(parser.buttons) + ";const runtime=" + json.dumps(_runtime_source()) + ";"
            value = interaction_program([
                ("function msgParams(", "function applyLocale()"),
            ], setup, r"""
              const nodes=attributes.map(attrs=>({attrs,dataset:{msg:attrs['data-msg'],title:attrs['data-title'],aria:attrs['data-aria']},
                attributes:Object.entries(attrs).map(([name,value])=>({name,value})),setAttribute(name,value){this.attrs[name]=value}}));
              const scope={querySelectorAll:selector=>nodes.filter(node=>Object.hasOwn(node.attrs,selector.slice(1,-1)))};
              const localize=new Function('CATALOGS','uiLocale','scope',runtime+'\n'+msgParams.toString()+'\n'+applyMessages.toString()+
                '\napplyMessages(scope);return scope.querySelectorAll("[data-title]").map(node=>({text:node.textContent||node.attrs["aria-label"],title:node.title}));');
              return localize({[uiLocale]:M},uiLocale,scope)[0];
            """, locale)
            self.assertEqual(value, {"text": expected, "title": expected})

    def test_current_round_keeps_all_five_saved_answer_kinds_editable_without_redeciding(self):
        scenarios = []
        for status in ("open", "submitted", "claimed"):
            for kind in ("accept", "option", "text", "park", "verify"):
                state = protection_state()
                state["protection"]["locked"] = status == "claimed"
                round_ = state["rounds"][0]
                round_.update(status=status, order=47, line="main", items=[])
                value = {"kind": kind}
                if kind == "option":
                    value["option"] = "a"
                elif kind == "text":
                    value["text"] = "保存的完整原话 <b>"
                elif kind == "verify":
                    value["allowExternal"] = True
                if kind in ("park", "verify"):
                    state["nodes"]["n1"].update(status="parked" if kind == "park" else "verifying", decision=None)
                round_["answers"] = {"Q:n1": value}
                item = {"key": "Q:n1", "type": "question", "node": "n1", "text": "采用哪个口径？"}
                context = {"analysis": state["id"], "round": 2, "line": "main", "historical": False,
                           "answeredItems": [item, item]}
                scenarios.append({"state": state, "context": context, "kind": kind})
        for locale in ("en", "zh-CN"):
            setup = """
              let analysisState=null,analysisContext=null,analysisBusy=false,analysisRenderKey='',analysisSelected=null;
              const analyses=[],calls=[],analysisAnswerDrafts=new Map();
              const analysisShown=()=>true,analysisRouteLabels=()=>{},toast=()=>{};
              const analysisAction=async(op,body)=>calls.push({op,body});
              const scenarios=SCENARIOS;
            """.replace("SCENARIOS", json.dumps(scenarios, ensure_ascii=False))
            values = interaction_program([
                ("// ---------- Analysis view helpers ----------", "// ---------- Analysis view DOM ----------"),
                ("function analysisDraftKey(", "function stashAnalysisInputs("),
                ("function renderAnalysis(){", "async function analysisAction("),
                ('$("analysisBody").onclick=', '$("analysisBody").oninput='),
            ], setup, r"""
              const results=[];
              for(const scenario of scenarios){
                analysisState=scenario.state;analysisContext=null;analysisSelected=analysisState.id;
                analysisRenderKey='';analysisAnswerDrafts.clear();renderAnalysis();
                analysisContext=scenario.context;renderAnalysis();
                const body=$('analysisBody').innerHTML,cards=body.match(/<article[^>]*data-analysis-item="Q:n1"[^>]*>[\s\S]*?<\/article>/g)||[];
                const card=cards[0]||'',kind=scenario.kind;
                const tag=(card.match(new RegExp('<button[^>]*data-kind="'+kind+'"[^>]*>'))||[])[0];
                const dataset={analysisAct:'answer',key:'Q:n1',kind};
                if(kind==='option')dataset.option='b';
                if(kind==='text')analysisAnswerDrafts.set(analysisDraftKey(analysisState,analysisState.rounds[0],'Q:n1'),{text:'修改后的完整原话 <b>'});
                if(kind==='verify')analysisAnswerDrafts.set(analysisDraftKey(analysisState,analysisState.rounds[0],'Q:n1'),{allowExternal:false});
                const before=calls.length;
                if(tag)await $('analysisBody').onclick({target:{closest:()=>({dataset,disabled:tag.includes(' disabled')})}});
                results.push({card,count:cards.length,call:calls.length>before?calls.at(-1):null,
                  kind,status:analysisState.rounds[0].status,order:analysisState.rounds[0].order});
              }
              return results;
            """, locale)
            for value in values:
                self.assertEqual(value["count"], 1, value)
                expected = {"kind": value["kind"]}
                if value["kind"] == "option":
                    expected["option"] = "b"
                elif value["kind"] == "text":
                    expected["text"] = "修改后的完整原话 <b>"
                    self.assertIn("保存的完整原话 &lt;b&gt;", value["card"])
                elif value["kind"] == "verify":
                    expected["allowExternal"] = False
                    self.assertIn(" checked", value["card"])
                self.assertEqual(value["call"], {"op": "answer", "body": {"round": 2, "key": "Q:n1", "value": expected}})
                self.assertEqual(value["order"], 47)

    def test_old_line_can_reconfirm_its_same_answer_when_it_needs_review(self):
        state = resolution_state()
        state["protection"]["locked"] = False
        state["rounds"] = []
        for locale in ("en", "zh-CN"):
            for needs_review in (False, True):
                state["nodes"]["n1"]["needsReview"] = needs_review
                body = render_resolution(state, locale)["analysisBody"]
                button = re.search(r'<button[^>]*data-analysis-act="redecide"[^>]*data-option="a"[^>]*>', body)
                self.assertIsNotNone(button)
                self.assertEqual(not needs_review, " disabled" in button.group())
        setup = """
          let analysisState=STATE,analysisBusy=false;const calls=[];
          const analysisAction=async(op,body)=>calls.push({op,body}),toast=()=>{};
        """.replace("STATE", json.dumps(state, ensure_ascii=False))
        value = interaction_program([
            ("// ---------- Analysis view helpers ----------", "// ---------- Analysis view DOM ----------"),
            ('$("analysisBody").onclick=', '$("analysisBody").oninput='),
        ], setup, """
          const body=analysisDecisionHtml(analysisState,analysisState.nodes.n1);
          const tag=body.match(/<button[^>]*data-analysis-act="redecide"[^>]*data-option="a"[^>]*>/)[0];
          await $('analysisBody').onclick({target:{closest:()=>({dataset:{analysisAct:'redecide',node:'n1',option:'a'},disabled:tag.includes(' disabled')})}});
          return calls;
        """)
        self.assertEqual(value, [{"op": "redecide", "body": {"node": "n1", "option": "a"}}])

    def test_current_round_renders_pending_and_saved_items_in_stable_type_order(self):
        state = resolution_state()
        state.update(nodes={}, decisions={}, assumptions={}, deadEnds={}, updates={}, drafts=[])
        items, proposals = {}, []
        for suffix in ("z", "a"):
            node, assumption, end, update, proposal = [prefix + suffix for prefix in ("n_", "a_", "e_", "u_", "p_")]
            state["nodes"][node] = {"id": node, "text": "合成待答问题 " + suffix, "status": "pending",
                                     "options": {"yes": "合成推荐"}, "recommend": "yes"}
            state["assumptions"][assumption] = {"id": assumption, "text": "合成前提 " + suffix, "refuted": False}
            state["deadEnds"][end] = {"id": end, "kind": "option", "node": node, "option": "yes",
                                      "reason": "合成无路可走原因 " + suffix,
                                      "status": "pending" if suffix == "z" else "confirmed"}
            state["updates"][update] = {"id": update, "text": "合成批注新文 " + suffix, "status": "pending"}
            structure = {"key": proposal, "text": "合成结构提议 " + suffix}
            proposals.append(structure)
            items[suffix] = [{"key": "Q:" + node, "type": "question", "node": node},
                             {**structure, "key": "S:" + proposal, "type": "structure"},
                             {"key": "A:" + assumption, "type": "assumption", "assumption": assumption},
                             {"key": "U:" + update, "type": "update", "update": update},
                             {"key": "D:" + end, "type": "deadend", "deadEnd": end}]
        state["lines"]["main"]["nodes"] = state["nodes"]
        round_ = state["rounds"][0]
        round_.update(proposals=proposals, items=items["z"],
                      answers={item["key"]: {"kind": "reject" if item["type"] == "update" else "accept"}
                               for item in items["a"]})
        context = {"analysis": state["id"], "round": round_["seq"], "historical": False,
                   "answeredItems": items["a"] + items["z"] + items["a"]}
        setup = """
          let analysisState=STATE,analysisContext=CONTEXT,analysisBusy=false,analysisRenderKey='';
          const analyses=[],analysisSelected=analysisState.id,analysisShown=()=>true,analysisRouteLabels=()=>{},analysisDraftFor=()=>({});
        """.replace("STATE", json.dumps(state, ensure_ascii=False)).replace("CONTEXT", json.dumps(context, ensure_ascii=False))
        for locale in ("en", "zh-CN"):
            values = interaction_program([
                ("// ---------- Analysis view helpers ----------", "// ---------- Analysis view DOM ----------"),
                ("function renderAnalysis(){", "async function analysisAction("),
            ], setup, r"""
              return ['open','submitted','claimed'].map(status=>{
                analysisState.rounds[0].status=status;analysisState.protection.locked=status==='claimed';
                renderAnalysis();return [...$('analysisBody').innerHTML.matchAll(/data-analysis-item="([^"]+)"/g)].map(match=>match[1]);
              });
            """, locale)
            for actual in values:
                self.assertEqual(actual, ["D:e_z", "D:e_a", "U:u_z", "U:u_a", "A:a_z", "A:a_a",
                                          "S:p_z", "S:p_a", "Q:n_z", "Q:n_a"])

    def test_saved_round_items_stay_unique_and_confirmed_updates_and_history_stay_readonly(self):
        state = resolution_state()
        state["protection"]["locked"] = False
        state["updates"] = {"u1": {"id": "u1", "text": "已确认的新批注原文", "status": "confirmed"}}
        state["deadEnds"]["e_private"]["status"] = "confirmed"
        proposal = {"key": "extra", "text": "合成补充问题", "short": "补充问题",
                    "options": [{"id": "yes", "label": "补充核验"}], "recommend": "yes"}
        items = [{"key": "Q:n1", "type": "question", "node": "n1"},
                 {"key": "A:a_private", "type": "assumption", "assumption": "a_private"},
                 {**proposal, "key": "S:extra", "type": "structure"},
                 {"key": "D:e_private", "type": "deadend", "deadEnd": "e_private"},
                 {"key": "U:u1", "type": "update", "update": "u1"}]
        round_ = state["rounds"][0]
        round_.update(status="submitted", proposals=[proposal], items=[items[0]],
                      answers={item["key"]: {"kind": "accept"} for item in items})
        context = {"analysis": state["id"], "round": round_["seq"], "line": "main", "answeredItems": items}
        setup = """
          let analysisState=STATE,analysisContext=CONTEXT,analysisBusy=false,analysisRenderKey='',analysisHistorySelection=null;
          const analyses=[],analysisSelected=analysisState.id,analysisShown=()=>true,analysisRouteLabels=()=>{},analysisDraftFor=()=>({});
        """.replace("STATE", json.dumps(state, ensure_ascii=False)).replace("CONTEXT", json.dumps(context, ensure_ascii=False))
        value = interaction_program([
            ("// ---------- Analysis view helpers ----------", "// ---------- Analysis view DOM ----------"),
            ("function renderAnalysis(){", "async function analysisAction("),
        ], setup, """
          renderAnalysis();const current=$('analysisBody').innerHTML;
          analysisHistorySelection={analysis:analysisState.id,kind:'round',id:2,context:{...analysisContext,historical:true,
            status:'done',items:[],answers:analysisState.rounds[0].answers,note:'冻结原话',writeback:{}}};
          renderAnalysis();return {current,history:$('analysisBody').innerHTML};
        """)
        for key in ("Q:n1", "A:a_private", "S:extra", "D:e_private"):
            self.assertEqual(value["current"].count('data-analysis-item="' + key + '"'), 1)
        self.assertIn("合成补充问题", value["current"])
        self.assertNotIn('data-key="U:u1"', value["current"])
        self.assertIn("已确认的新批注原文", value["current"])
        self.assertIn("冻结原话", value["history"])
        self.assertNotIn('data-analysis-act=', value["history"])

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
