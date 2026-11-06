"""#113 页面公开呈现与命令接缝；只替换浏览器 DOM 和网络边界。"""
from __future__ import annotations

import html
import json
import re
import unittest

from tests.test_analysis_page import interaction_program, render_expression


def protection_state():
    return {
        "id": "private-analysis", "focus": "合成口径", "status": "active", "line": "main",
        "sourceNote": "private-source", "sourceState": "held", "sourceCopy": {"text": "源原文"},
        "protection": {"locked": True, "round": 2},
        "nodes": {"n1": {"id": "n1", "text": "采用哪个口径？", "short": "口径", "status": "decided",
                          "options": {"a": "全年", "b": "季度"}, "recommend": "a", "decision": "d1"}},
        "decisions": {"d1": {"id": "d1", "option": "a", "label": "全年"}},
        "drafts": [{"id": "private-draft", "text": "待确认的结论", "status": "pending"}],
        "rounds": [{"seq": 2, "status": "claimed", "items": [{"key": "Q:n1", "type": "question", "node": "n1"}], "answers": {}}],
    }


def render_page(state, locale="en"):
    setup = """
      let analysisState=STATE,analysisBusy=false,analysisRenderKey='';
      const analyses=[{id:analysisState.id,focus:analysisState.focus,status:'active'},
        {id:'private-other',focus:'另一分析',status:'paused'}],analysisSelected=analysisState.id;
      const analysisShown=()=>true,analysisRouteLabels=()=>{},analysisDraftFor=()=>({});
    """.replace("STATE", json.dumps(state, ensure_ascii=False))
    return interaction_program([
        ("// ---------- Analysis view helpers ----------", "// ---------- Analysis view DOM ----------"),
        ("function renderAnalysis(){", "async function analysisAction("),
    ], setup, """
      renderAnalysis();return Object.fromEntries(['analysisBody','analysisTree','analysisManage','analysisList','analysisTimeline']
        .map(id=>[id,$(id).innerHTML]));
    """, locale)


class AnalysisProtectionPageTests(unittest.TestCase):
    def test_claimed_round_answers_stay_enabled_while_same_analysis_changes_are_protected(self):
        state = protection_state()
        state["assumptions"] = {"a1": {"text": "合成前提", "confirmed": {"main": "pending"}}}
        state["rounds"][0]["items"].extend([
            {"key": "A:a1", "type": "assumption", "assumption": "a1"},
            {"key": "S:proposal", "type": "structure", "proposal": {"key": "proposal", "text": "补充合成问题"}},
        ])
        for locale in ("en", "zh-CN"):
            page = render_page(state, locale)
            body = page["analysisBody"]
            for kind in ("accept", "option", "text", "park", "verify"):
                buttons = re.findall(r'<button[^>]*data-kind="' + kind + r'"[^>]*>', body)
                self.assertTrue(buttons, kind)
                self.assertTrue(all(" disabled" not in button for button in buttons), kind)
            for action in ("release", "convert", "confirm"):
                self.assertRegex(body, r'data-analysis-act="' + action + r'"[^>]* disabled')
            self.assertNotIn(" disabled", page["analysisManage"])
            self.assertNotIn(" disabled", page["analysisList"])
            visible = html.unescape(re.sub(r"<[^>]*>", "", body))
            self.assertIn("continue answering" if locale == "en" else "仍可作答", visible)

    def test_tree_keeps_detached_decisions_readonly_and_redecides_with_existing_original_options(self):
        state = protection_state()
        state["protection"]["locked"] = False
        state["rounds"] = []
        state["nodes"]["n1"]["options"]["text-private"] = "按月核对 <b>完整原话</b>"
        state["nodes"]["n2"] = {"id": "n2", "text": "原答案下的问题", "short": "旧口径核验", "status": "decided",
                                  "parent": {"node": "n1", "option": "b"}, "decision": "d2", "options": {"old": "旧结论"}}
        state["decisions"]["d2"] = {"id": "d2", "option": "old", "label": "断枝的原结论"}
        state["nodes"]["n3"] = {"id": "n3", "text": "已作废问题", "short": "作废问题", "status": "void"}
        state["nodes"]["n4"] = {"id": "n4", "text": "作废后代", "short": "作废后代", "status": "decided",
                                  "parent": {"node": "n3", "option": "a"}, "decision": "d2"}
        for locale in ("en", "zh-CN"):
            page = render_page(state, locale)
            self.assertIn("Detached branches" if locale == "en" else "断枝", page["analysisTree"])
            self.assertIn("Current choices" if locale == "en" else "现在的选择", page["analysisTree"])
            self.assertIn("旧口径核验", page["analysisTree"])
            self.assertNotIn("作废", page["analysisTree"] + page["analysisBody"])
            detached = re.search(r'<article[^>]*data-analysis-node="n2"[^>]*>.*?</article>', page["analysisBody"], re.S)
            self.assertIsNotNone(detached)
            self.assertIn("断枝的原结论", detached.group())
            self.assertNotIn('data-analysis-act=', detached.group())
            current = re.search(r'<article[^>]*data-analysis-node="n1"[^>]*>.*?</article>', page["analysisBody"], re.S).group()
            self.assertRegex(current, r'data-analysis-act="redecide"[^>]*data-option="text-private"')
            self.assertIn("按月核对 &lt;b&gt;完整原话&lt;/b&gt;", current)
            visible = html.unescape(re.sub(r"<[^>]*>", "", current))
            self.assertNotIn("private", visible)

    def test_claimed_update_item_shows_escaped_old_and_new_text_and_uses_round_answer_controls(self):
        state = protection_state()
        state["updates"] = {"u-private": {"id": "u-private", "noteId": "private-note", "beforeText": "原文 <img src=x>",
                                              "text": "新文 <script>完整原话</script>", "status": "pending"}}
        item = {"key": "U:u-private", "type": "update", "update": "u-private"}
        state["rounds"][0]["items"] = [item]
        for locale in ("en", "zh-CN"):
            body = render_page(state, locale)["analysisBody"]
            self.assertIn("原文 &lt;img src=x&gt;", body)
            self.assertIn("新文 &lt;script&gt;完整原话&lt;/script&gt;", body)
            self.assertNotIn("<script>", body)
            controls = re.findall(r'<button[^>]*data-key="U:u-private"[^>]*>', body)
            self.assertEqual(2, len(controls))
            self.assertTrue(all(" disabled" not in button for button in controls))
            for kind in ("text", "park", "verify"):
                self.assertNotIn('data-kind="' + kind + '"', body)
            label = render_expression("analysisAnswerLabel(state,state.rounds[0],'U:u-private',{kind:'accept'})", state, locale)
            self.assertIn("新文 <script>完整原话</script>", label)
            self.assertNotIn("private", label)

    def test_changed_conclusion_notes_offer_three_actions_and_preserve_all_old_text(self):
        state = protection_state()
        state["linkedNotes"] = [{"id": "private-note", "status": "held", "hold": "结论已变", "tracking": True,
                                  "text": "当前批注 <img>", "analysisHistory": [{"text": "旧文一 <script>"}, {"text": "旧文二\n完整原话"}]}]
        for locked in (True, False):
            state["protection"]["locked"] = locked
            state["rounds"] = []
            for locale in ("en", "zh-CN"):
                body = render_page(state, locale)["analysisBody"]
                for action in ("update-note", "keep-note", "delete-note"):
                    control = re.search(r'<button[^>]*data-analysis-act="' + action + r'"[^>]*>', body)
                    self.assertIsNotNone(control, action)
                    self.assertEqual(locked, " disabled" in control.group())
                    self.assertIn('data-note-id="private-note"', control.group())
                self.assertIn("当前批注 &lt;img&gt;", body)
                self.assertIn("旧文一 &lt;script&gt;", body)
                self.assertIn("旧文二\n完整原话", body)
                self.assertIn("stop tracking" if locale == "en" else "停止跟踪", body)

    def test_new_page_actions_send_exact_targets_and_delete_needs_named_confirmation(self):
        state = protection_state()
        state["protection"]["locked"] = False
        state["linkedNotes"] = [{"id": "private-note", "text": "要删除的合成原文 <b>"}]
        setup = """
          let analysisState=STATE,analysisBusy=false;const calls=[],prompts=[];
          let confirmed=false;const confirm=text=>{prompts.push(text);return confirmed};
          const analysisAction=async(op,body)=>calls.push({op,body}),analysisDraftFor=()=>({}),toast=()=>{};
          const location={hash:'#analysis'};
        """.replace("STATE", json.dumps(state, ensure_ascii=False))
        value = interaction_program([
            ("// ---------- Analysis view helpers ----------", "// ---------- Analysis view DOM ----------"),
            ('$("analysisBody").onclick=', '$("analysisBody").oninput='),
        ], setup, """
          const click=dataset=>$('analysisBody').onclick({target:{closest:()=>({dataset,disabled:false})}});
          await click({analysisAct:'redecide',node:'n1',option:'b'});
          await click({analysisAct:'update-note',noteId:'private-note'});
          await click({analysisAct:'keep-note',noteId:'private-note'});
          await click({analysisAct:'delete-note',noteId:'private-note'});const beforeConfirm=calls.length;
          confirmed=true;await click({analysisAct:'delete-note',noteId:'private-note'});
          return {calls,prompts,beforeConfirm};
        """)
        self.assertEqual(3, value["beforeConfirm"])
        self.assertEqual([
            {"op": "redecide", "body": {"node": "n1", "option": "b"}},
            {"op": "update-note", "body": {"noteId": "private-note"}},
            {"op": "keep-note", "body": {"noteId": "private-note"}},
            {"op": "delete-note", "body": {"noteId": "private-note"}},
        ], value["calls"])
        self.assertEqual(2, len(value["prompts"]))
        for prompt in value["prompts"]:
            self.assertIn("要删除的合成原文 <b>", prompt)
            self.assertNotIn("private-note", prompt)

    def test_protection_errors_and_void_reasons_use_current_language_and_explain_refresh(self):
        codes = ["analysis-locked", "bad-option", "draft-basis-changed", "note-not-changed", "bad-updates", "updates-required", "update-not-pending", "update-source-changed"]
        state = protection_state()
        state["rounds"] = [{"seq": 2, "status": "void", "answers": {}, "voidReason": "base-rev-mismatch"}]
        for locale in ("en", "zh-CN"):
            errors = render_expression("""[CODES].map(code=>analysisErrorText({code,status:409,message:'内部 private-analysis'}))""".replace("[CODES]", json.dumps(codes)), state, locale)
            self.assertEqual(len(codes), len(set(errors)))
            for error in errors:
                self.assertNotIn("viewer.", error)
                self.assertNotIn("private", error)
                self.assertNotIn("内部", error)
            stale = render_expression("analysisErrorText({code:'round-not-answerable',status:409})", state, locale)
            self.assertIn("refresh" if locale == "en" else "刷新", stale.lower())
            timeline = render_page(state, locale)["analysisTimeline"]
            self.assertNotIn("base-rev-mismatch", timeline)
            self.assertIn("new round" if locale == "en" else "重新出题", timeline)

    def test_changed_annotation_card_routes_to_analysis_instead_of_bypassing_protection(self):
        setup = """
          const isDone=x=>x.status==='resolved',isHeld=x=>x.status==='held',isDraft=()=>false,inFlight=()=>false;
          const editingId=EDITING,editOf=()=>null,analysisSupported=true,FILE=false,hasMark=()=>false;
          const targetLabel=()=>'',alwaysFor=()=>[],editBuf={'private-note':'未保存草稿'},P=[];
        """
        note = {"id": "private-note", "status": "held", "hold": "结论已变", "text": "合成批注", "heldBy": "private-analysis"}
        for editing in (None, "private-note"):
            for locale in ("en", "zh-CN"):
                rendered = interaction_program([
                    ("function noteHtml(x){", "// 把正在编辑的那张卡片"),
                ], setup.replace("EDITING", json.dumps(editing)), "return noteHtml(" + json.dumps(note, ensure_ascii=False) + ");", locale)
                self.assertIn("Conclusion changed" if locale == "en" else "结论已变", rendered)
                self.assertIn('data-act="analysis-open"', rendered)
                self.assertNotIn('data-act="del"', rendered)
                self.assertNotIn('data-act="edit"', rendered)
                self.assertNotIn("<textarea", rendered)
                if editing:
                    self.assertIn('data-act="cancel"', rendered)

    def test_mutation_transport_keeps_binding_and_locked_analysis_still_accepts_answers_and_focus_switch(self):
        setup = """
          let analysisState=STATE,analysisSelected=analysisState.id,analysisBusy=false,analysisRequest=0,analysisRenderKey='';
          const FILE=false,analysisSupported=true,mode='server',receiver={bindingId:'test-binding'},SS={set(){}},RID='test-review';
          const calls=[],notices=[],renderAnalysis=()=>{},analysisRouteLabels=()=>{},resync=async()=>{},loadAnalyses=async()=>{},toast=text=>notices.push(text);
          const api=async(method,path,body)=>{calls.push({method,path,body});return {}};
        """.replace("STATE", json.dumps(protection_state(), ensure_ascii=False))
        value = interaction_program([
            ("// ---------- Analysis view helpers ----------", "// ---------- Analysis view DOM ----------"),
            ("async function analysisAction(", "async function startAnalysis("),
        ], setup, """
          await analysisAction('keep-note',{noteId:'private-note'});
          await analysisAction('answer',{round:2,key:'Q:n1',value:{kind:'option',option:'b'}});
          await analysisAction('activate',{analysis:'private-other'});
          analysisState.protection.locked=false;
          await analysisAction('redecide',{node:'n1',option:'b'});
          return {calls,notices};
        """)
        self.assertEqual(["/api/analysis/answer", "/api/analysis/activate", "/api/analysis/redecide"], [call["path"] for call in value["calls"]])
        for call in value["calls"]:
            self.assertEqual("POST", call["method"])
            self.assertEqual("test-binding", call["body"]["receiverBindingId"])
        self.assertEqual("private-other", value["calls"][1]["body"]["analysis"])
        for notice in value["notices"]:
            self.assertNotIn("viewer.", notice)


if __name__ == "__main__":
    unittest.main()
