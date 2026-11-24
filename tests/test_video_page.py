"""视频对照页快速契约（#192）：帧定位、黑边坐标与交换链路。

只执行 viewer_template.html 中的纯函数并检查公开 DOM 接线；完整浏览器、编解码和声音
不在本模块伪装验收。
"""

from __future__ import annotations

import json
import unittest
from pathlib import Path

from tests.test_analysis_page import interaction_program

ROOT = Path(__file__).resolve().parents[1]
TEMPLATE = ROOT / "skill" / "annotation-everything" / "scripts" / "viewer_template.html"
START = "// ---------- 视频审阅纯函数 ----------"
END = "// ---------- 视频审阅 DOM ----------"


def run_video(expression: str):
    return interaction_program([(START, END)], "let videoRangeEnd=null;", expression)


def frame_anchor():
    return {
        "v": 1,
        "versionId": "V1",
        "frame": 96,
        "fps": 30,
        "timebase": {"num": 1, "den": 30},
        "frames": 180,
        "videoWidth": 640,
        "videoHeight": 360,
        "mark": {"kind": "region", "x": 0.1, "y": 0.8, "width": 0.5, "height": 0.1},
    }


def compare_ui(expression):
    """视频、DOM 和服务端返回值使用合成数据，控件接线与页面生成代码一致。"""
    return interaction_program(
        [
            (START, END),
            ("// ---------- 视频对照与重定位 ----------", "// ---------- 批注区："),
            ("let videoApproveSel=", "function finalizeSkipText"),
        ],
        """
        const version=id=>({versionId:id,url:'/'+id,frames:60,fps:30,timebase:{num:1,den:30},width:160,height:90});
        const VIDEO={currentVersionId:'V2',versions:['V1','V2'].map(version)},videoManifest=()=>VIDEO;
        const makeNote=id=>({id,status:'drafted',text:id,video:{versionId:'V1',frame:6},
          videoReview:{state:'pending_review',batch:1,versionId:'V2',summary:'First receipt',target:{kind:'frame',frame:15}}});
        let notes=[makeNote('caption'),makeNote('music')],mode='server';
        const calls=[],api=async (method,path,body)=>{calls.push({path,body});return {}},toast=()=>{},renderFinalizeAll=()=>{};
        class Control extends EventTarget {
          constructor(){super();this.style={};this.value='';this.textContent='';this.readyState=2;this.paused=true}
          click(){if(!this.disabled&&!this.hidden)return this.onclick?.()}
          getAttribute(key){return this[key]}
          removeAttribute(key){delete this[key]}
          load(){if(this.onloadeddata)queueMicrotask(()=>this.onloadeddata?.())}
          get currentTime(){return this.time||0}
          set currentTime(value){this.time=value;if(this.onseeked)queueMicrotask(()=>this.onseeked?.())}
          pause(){this.paused=true}
        }
        const controls=new Map(),get=id=>{
          if(!controls.has(id))controls.set(id,new Control());return controls.get(id)};
        const sr={getElementById:get},document={createElement:()=>new Control()};
        const videoLayout=()=>{},videoFindVersion=id=>VIDEO.versions.find(v=>v.versionId===id);
        """,
        """
        setVideoServerFeatures({features:['video-decisions','video-evidence-revision']});
        videoCompareInit();
        videoCompareOpen(notes[0]);
        const replace=(revision,frame)=>{
          notes[0]={...notes[0],videoReview:{...notes[0].videoReview,evidenceRevision:revision,
            summary:'Replacement '+frame,target:{kind:'frame',frame}}};
          videoCompareSync();
        };
        """
        + expression,
    )


class VideoCompareEvidenceTests(unittest.TestCase):
    def test_question_without_a_target_keeps_reply_and_displayed_version_during_other_receipt(self):
        result = compare_ui(
            """
            videoCompareClose();
            notes[0].videoReview={state:'needs_reply',question:'Which frame?',evidenceRevision:'question'};
            videoCompareOpen(notes[0]);
            get('videoCompareReplyInput').value='Keep frame six 请保留';
            VIDEO.versions.push(version('V3'));VIDEO.currentVersionId='V3';
            notes[1].videoReview={...notes[1].videoReview,versionId:'V3',evidenceRevision:'music'};
            videoCompareSync();
            return [get('videoCompareReplyInput').value,get('videoCompareAfterInfo').textContent];
            """,
        )
        self.assertEqual(result, ["Keep frame six 请保留", "V2"])

    def test_new_receipt_first_opened_after_a_return_needs_no_extra_confirmation(self):
        result = compare_ui(
            """
            notes[0].videoReview={...notes[0].videoReview,state:'returned',decision:'returned',
              decisionInputRevision:'legacy',evidenceRevision:'returned',returnReason:'Please shift the title'};
            videoCompareSync();
            const returnNotice=!get('videoCompareEvidenceNotice').hidden;
            videoCompareClose();
            notes[0].videoReview={...makeNote('caption').videoReview,evidenceRevision:'next-batch',batch:2};
            videoCompareOpen(notes[0]);
            const approve=get('videoCompareApprove');
            return {returnNotice,canApprove:!approve.hidden&&!approve.disabled};
            """,
        )
        self.assertEqual(result, {"returnNotice": False, "canApprove": True})

    def test_card_and_group_approval_wait_for_the_displayed_replacement(self):
        result = compare_ui(
            """
            const picked=()=>videoSelectedBatches({batches:[{seq:1,ids:['caption','music']}]}).batches[0]?.ids;
            videoSelectAllReady();const initial=picked();
            replace('second',24);
            videoCompareClose();videoSelectAllReady();
            const waiting=picked();
            await submitVideoDecision('caption','approve');
            videoCompareOpen(notes[0]);
            const after=get('videoCompareAfter'),ack=get('videoCompareAcknowledge');
            after.readyState=1;after.dispatchEvent(new Event('emptied'));
            await ack.click();const loading=get('videoCompareApprove').disabled;
            after.readyState=2;after.dispatchEvent(new Event('loadeddata'));await ack.click();
            videoSelectAllReady();const confirmed=picked();
            await submitVideoDecision('caption','approve');
            return {initial,waiting,loading,confirmed,calls};
            """,
        )
        self.assertEqual(result["initial"], ["caption", "music"])
        self.assertEqual(result["waiting"], ["music"])
        self.assertTrue(result["loading"])
        self.assertEqual(result["confirmed"], ["caption", "music"])
        self.assertEqual([call["body"]["expectedEvidence"] for call in result["calls"]], ["second"])

    def test_unrelated_receipts_and_replays_preserve_the_open_edit_and_playback(self):
        result = compare_ui(
            """
            get('videoCompareToggleRelocate').click();
            const summary=get('videoRelocateSummary'),player=get('videoCompareAfter');
            summary.value='Unfinished 剪口';player.currentTime=1.1;player.paused=false;
            const state=()=>[summary.value,player.currentTime,player.paused,get('videoCompareApprove').disabled];
            notes=JSON.parse(JSON.stringify(notes));
            videoCompareSync();const replay=state();
            VIDEO.versions.push(version('V3'));VIDEO.currentVersionId='V3';
            notes[1].videoReview={...notes[1].videoReview,versionId:'V3',evidenceRevision:'music-update'};
            videoCompareSync();return {replay,other:state()};
            """,
        )
        expected = ["Unfinished 剪口", 1.1, False, False]
        self.assertEqual(result["replay"], expected)
        self.assertEqual(result["other"], expected)

    def test_replaced_evidence_needs_a_fresh_confirmation_for_each_revision(self):
        result = compare_ui(
            """
            const approve=get('videoCompareApprove'),notice=get('videoCompareEvidenceNotice'),ack=get('videoCompareAcknowledge');
            const canApprove=()=>!approve.hidden&&!approve.disabled;
            const opened=canApprove();
            replace('second',24);
            const replacement={summary:get('videoCompareAfterReason').textContent,
              warning:get('videoCompareEvidenceMessage').textContent,notice:!notice.hidden,blocked:!canApprove()};
            await approve.click();
            await ack.click();
            const reviewed=canApprove();
            videoCompareSync();
            const replayed=canApprove();
            replace('third',30);
            const renewed=!canApprove();
            await approve.click();
            await ack.click();await approve.click();
            return {opened,replacement,reviewed,replayed,renewed,calls};
            """,
        )
        self.assertTrue(result["opened"])
        self.assertTrue(result["replacement"]["blocked"])
        self.assertTrue(result["replacement"]["notice"])
        self.assertIn("updated", result["replacement"]["warning"].lower())
        self.assertIn("Replacement 24", result["replacement"]["summary"])
        self.assertTrue(result["reviewed"] and result["replayed"] and result["renewed"])
        self.assertEqual(
            result["calls"],
            [
                {
                    "path": "/api/notes/caption/video-decision",
                    "body": {
                        "action": "approve",
                        "expectedEvidence": "third",
                        "expectedVersion": "V2",
                        "expectedBatch": 1,
                    },
                }
            ],
        )


class VideoPagePureTests(unittest.TestCase):
    def test_shortcuts_leave_shadow_form_keys_to_the_control(self):
        result = interaction_program(
            [
                (
                    'document.addEventListener("keydown",e=>{\n  if(overviewShown())return;',
                    "// ---------- Analysis view helpers ----------",
                )
            ],
            """
            let onKey, markMode=false, remarkId=null, focus=false, i=0;
            const document={addEventListener:(_,f)=>onKey=f,querySelector:()=>null};
            const overviewShown=()=>false,analysisShown=()=>false;
            const toggleFocus=()=>{focus=!focus},setMarkMode=on=>{markMode=on},go=k=>{i=k};
            """,
            """
            const host={tagName:'DIV'};
            const key=(name,origin,target=host)=>{
              let consumed=false;
              onKey({key:name,target,composedPath:()=>[origin,target],preventDefault(){consumed=true}});
              return consumed;
            };
            const input={tagName:'INPUT'},select={tagName:'SELECT'};
            const form=[key('f',input),key('p',input),key('ArrowLeft',input),key('ArrowRight',select)];
            const documentInput={tagName:'TEXTAREA'};
            const documentKey=key('p',documentInput,documentInput);
            const during=[focus,markMode,i];
            const outside=key('p',host);
            return {form,documentKey,during,outside,markMode};
            """,
        )
        self.assertEqual(result["form"], [False, False, False, False])
        self.assertFalse(result["documentKey"])
        self.assertEqual(result["during"], [False, False, 0])
        self.assertTrue(result["outside"] and result["markMode"])

    def test_submit_retry_shows_original_batch_stage_and_document_merges_keep_their_message(self):
        notices = interaction_program(
            [("function onSent(op,r)", "function onRejected"), ("const BATCH_STATUS=", "const DELIVERY_LABEL=")],
            "const pend={get:()=>[]},setHand=()=>{},notices=[],toast=text=>notices.push(text);let handQueued=true;",
            """
            const handoff={latest:{seq:2,status:"pending"}};
            const op={kind:"handoff",submitId:"first"};
            for(const status of ["pending","claimed","done"])onSent(op,{seq:1,status,merged:true,handoff});
            onSent(op,{seq:1,merged:true,handoff});
            onSent({kind:"handoff"},{seq:2,merged:true,handoff});
            return notices;
            """,
            locale="zh-CN",
        )
        for text, stage in zip(notices, ("等待接收", "处理中", "已处理")):
            self.assertIn("第 1 批", text)
            self.assertIn(stage, text)
        self.assertIn("第 1 批", notices[3])
        self.assertNotIn("等待接收", notices[3])
        self.assertNotIn("还没接收", notices[3])
        self.assertIn("已更新这次提交", notices[4])

    def test_reconnect_keeps_the_claimed_batch_notice_after_flushing_the_submit_queue(self):
        actual = interaction_program(
            [
                ("const sameReview=", "function mirror()"),
                ("function send(op,", "// 总览和命令行"),
                ("let handPollKey=", "function applyServer("),
                ("async function poll(){", "// ---------- 服务不在时"),
                ("const BATCH_STATUS=", "const DELIVERY_LABEL="),
            ],
            """
            const APP="ae",RID="synthetic",FILE=false,DOCX=null,PPTX=null,VIDEO=true,navigator={},document={hidden:false};
            const storage=new Map(),K={pending:"p",legacyPending:"l",failed:"f"};
            const LS={get:(k,v)=>storage.get(k)??v,set:(k,v)=>{storage.set(k,v);return true}};
            let busy=0,mode="offline",rev=1,srvNoVideo=false,srvNoFullCheck=false,analysisUpgradePoll=null,handQueued=true;
            const toast=text=>{$("toast").textContent=text},renderDocxBlock=()=>{},renderPptxBlock=()=>{},renderVideoBlock=()=>{};
            const updateMode=()=>{},setHand=()=>{},applyServer=()=>{},posts=[];
            const doc={app:APP,reviewId:RID,notes:[],features:["video","selective-handoff","video-handoff-snapshot",
              "video-source","video-receipt","video-decisions","video-evidence-revision","full-check"]};
            const api=async(method,path,body)=>{
              if(method==="GET"&&path==="/api/notes")return doc;
              if(method==="POST"&&path==="/api/handoff"){
                posts.push(body.submitId);
                return {doc,seq:1,merged:true,status:"claimed",handoff:{latest:{seq:2,status:"pending"}}};
              }
              throw new Error("unexpected request");
            };
            """,
            """
            pend.push({kind:"handoff",opId:"op1",noteIds:["n1"],submitId:"lost-response"});
            await poll();return [posts,pend.get(),$("toast").textContent];
            """,
            locale="zh-CN",
        )
        self.assertEqual(actual[:2], [["lost-response"], []])
        self.assertIn("第 1 批", actual[2])
        self.assertIn("处理中", actual[2])

    def test_microsecond_playback_clock_preserves_frame_boundaries(self):
        actual = run_video(
            "const clock={frames:180,timebase:{num:1,den:30}};"
            "return [0.533333,1.833333,5.966666,5.966665,5.9666661,3.999999,4,4.02].map(t=>videoTimeToFrame(t,clock));"
        )
        self.assertEqual(actual, [16, 55, 179, 178, 178, 119, 120, 120])

    def test_clean_video_and_dedup_key_keep_the_whole_immutable_location(self):
        anchor = frame_anchor()
        actual = run_video(
            "const a=" + json.dumps(anchor) + ",b={...a,frame:95};"
            "const cleaned=cleanVideo(a);"
            "return {cleaned,different:videoKey(a)!==videoKey(b),same:videoKey(a)===videoKey(cleaned),"
            "future:cleanVideo({v:2,versionId:'V8',opaque:{keep:true}})};"
        )
        self.assertEqual(actual["cleaned"], anchor)
        self.assertTrue(actual["different"])
        self.assertTrue(actual["same"])
        self.assertEqual(actual["future"], {"v": 2, "versionId": "V8", "opaque": {"keep": True}})

    def test_anchor_factory_uses_registered_version_dimensions_and_frame_clock(self):
        manifest = {
            "currentVersionId": "V1",
            "versions": [
                {
                    "versionId": "V1",
                    "fps": 30,
                    "timebase": {"num": 1, "den": 30},
                    "frames": 180,
                    "width": 640,
                    "height": 360,
                }
            ],
        }
        actual = run_video(
            "const manifest=" + json.dumps(manifest) + ";"
            "return {frame:videoMakeAnchor(manifest,{frame:96},{kind:'point',x:.2,y:.3}),"
            "range:videoMakeAnchor(manifest,{startFrame:120,endFrame:180},null)};"
        )
        self.assertEqual(actual["frame"]["frame"], 96)
        self.assertEqual(actual["frame"]["videoWidth"], 640)
        self.assertEqual(actual["frame"]["mark"], {"kind": "point", "x": 0.2, "y": 0.3})
        self.assertEqual(actual["range"]["startFrame"], 120)
        self.assertEqual(actual["range"]["endFrame"], 180)
        self.assertNotIn("mark", actual["range"])

    def test_object_fit_black_bars_never_change_normalized_video_coordinates(self):
        actual = run_video(
            "const wide=videoContentRect(1000,800,640,360),tall=videoContentRect(500,800,640,360);"
            "return {wide,tall,"
            "point:videoNormalizedPoint(500,400,{left:0,top:0},wide),"
            "letterbox:videoNormalizedPoint(500,50,{left:0,top:0},wide),"
            "resized:videoNormalizedPoint(250,400,{left:0,top:0},tall)};"
        )
        self.assertEqual(actual["wide"], {"x": 0, "y": 118.75, "width": 1000, "height": 562.5})
        self.assertEqual(actual["tall"], {"x": 0, "y": 259.375, "width": 500, "height": 281.25})
        self.assertEqual(actual["point"], {"x": 0.5, "y": 0.5})
        self.assertIsNone(actual["letterbox"])
        self.assertEqual(actual["resized"], {"x": 0.5, "y": 0.5})

    def test_seek_frame_96_and_play_half_open_four_to_six_seconds_with_sound(self):
        actual = run_video(
            "const calls=[],player={currentTime:0,muted:true,pause(){calls.push('pause')},play(){calls.push('play');return Promise.resolve()}};"
            "videoSeekFrame(player,96,30);const seek=player.currentTime;"
            "await videoPlayInterval(player,{startFrame:120,endFrame:180,fps:30});"
            "player.currentTime=5.99;videoRangeTick(player);const before=calls.slice();"
            "player.currentTime=6;videoRangeTick(player);"
            "return {seek,rangeStart:4,before,after:calls,muted:player.muted,stoppedAt:player.currentTime};"
        )
        self.assertAlmostEqual(actual["seek"], 3.2)
        self.assertEqual(actual["before"], ["pause", "pause", "play"])
        self.assertEqual(actual["after"], ["pause", "pause", "play", "pause"])
        self.assertFalse(actual["muted"])
        self.assertEqual(actual["stoppedAt"], 6)

    def test_main_player_keeps_range_across_preview_and_draws_marks_on_native_seeks(self):
        actual = interaction_program(
            [(START, "// ---------- 视频对照与重定位 ----------")],
            """
            const versions=["V1","V2"].map(versionId=>({versionId,url:"/"+versionId,fps:30,
              timebase:{num:1,den:30},frames:180,width:640,height:360}));
            const VIDEO={versions,currentVersionId:"V2"}, liveVideo=VIDEO;
            let videoActiveVersionId="V2";
            const saves=[],messages=[],calls=[],boxes=new Map();
            const toast=m=>messages.push(m),saveDraft=()=>saves.push(JSON.stringify(videoDraft));
            class Box extends EventTarget {
              constructor(){super();this.style={};this.children=[]}
              set innerHTML(_){this.children=[]}
              appendChild(el){this.children.push(el)}
            }
            const sr={getElementById(id){if(!boxes.has(id))boxes.set(id,new Box());return boxes.get(id)}};
            const document={createElement:()=>new Box()};
            const mark={kind:"point",x:.2,y:.3};
            const notes=[{id:"m",video:{versionId:"V2",frame:30,mark}}];
            const isDone=()=>false,renderVideoSource=()=>{},videoCompareInit=()=>{};
            const player=sr.getElementById("videoPlayer");
            Object.assign(player,{readyState:1,paused:true,currentTime:0,getAttribute(k){return this[k]},
              load(){this.readyState=0},pause(){this.paused=true;calls.push("pause")},
              async play(){this.paused=false;calls.push("play")},requestVideoFrameCallback(fn){this.onFrame=fn}});
            """,
            """
            videoInit();
            const get=id=>sr.getElementById(id);
            player.currentTime=4;get("videoSetStart").onclick();
            player.currentTime=179/30;get("videoSetEnd").onclick();
            player.currentTime=1;player.dispatchEvent(new Event("seeked"));
            const atMark=get("videoMarkLayer").children.length;
            const input=get("videoFrame");sr.activeElement=input;input.value="17";
            player.onFrame(0,{mediaTime:2});
            const pastMark=get("videoMarkLayer").children.length,editing=input.value;
            sr.activeElement=null;player.onFrame(0,{mediaTime:1});
            const playingMark=[get("videoMarkLayer").children.length,input.value];
            get("videoPlayRange").onclick();
            const preview=[videoDraft.startFrame,videoDraft.endFrame,player.currentTime,player.paused];
            calls.length=0;
            videoShowNote({id:"old",video:{versionId:"V1",startFrame:15,endFrame:30}});
            const loading=calls.slice();
            videoShowNote({id:"latest",video:{versionId:"V1",startFrame:45,endFrame:90}});
            player.readyState=1;player.dispatchEvent(new Event("loadedmetadata"));
            const loaded=[...calls,player.currentTime];
            player.currentTime=3.1;player.dispatchEvent(new Event("timeupdate"));
            loaded.push(player.paused,player.currentTime);
            videoShowNote({id:"missing",video:{versionId:"V3",frame:1}});
            const missing=videoComparePair({video:{versionId:"V1",frame:0},
              videoReview:{versionId:"V3",target:{kind:"frame",versionId:"V3",frame:1}}},VIDEO);
            return {atMark,pastMark,editing,playingMark,preview,loading,loaded,missingUrl:missing.after.url,messages,src:player.src};
            """,
        )
        self.assertEqual((actual["atMark"], actual["pastMark"]), (1, 0))
        self.assertEqual(actual["editing"], "17")
        self.assertEqual(actual["playingMark"], [1, 30])
        self.assertEqual(actual["preview"], [120, 180, 4, False])
        self.assertEqual(actual["loading"], [])
        self.assertEqual(actual["loaded"], ["pause", "play", 1.5, True, 3])
        self.assertIsNone(actual["missingUrl"])
        self.assertIn("V3", actual["messages"][0])
        self.assertEqual(actual["src"], "/V1")

    def test_markdown_line_round_trip_keeps_video_json(self):
        anchor = frame_anchor()
        actual = run_video(
            "const a=" + json.dumps(anchor) + ";"
            "return ['zh-CN','en'].map(lang=>{const line=videoExportLine(a,lang==='en');"
            "return {line,parsed:videoImportLine(line,lang==='en')}});"
        )
        for item in actual:
            self.assertEqual(item["parsed"], anchor)
        self.assertIn("视频定位：", actual[0]["line"])
        self.assertIn("Video location: ", actual[1]["line"])

    def test_selective_handoff_range_defaults_to_all_and_keeps_order(self):
        """全选也固定提交范围，保存后新增的批注不进入本次提交。"""
        actual = run_video(
            "const avail=['n1','n2','n3','n4','n5'];"
            "return {untouched:handoffNoteIds(avail,null),"
            "allTicked:handoffNoteIds(avail,new Set(avail)),"
            "three:handoffNoteIds(avail,new Set(['n4','n1','n5'])),"
            "none:handoffNoteIds(avail,new Set())};"
        )
        self.assertEqual(actual["untouched"], [])
        self.assertEqual(actual["allTicked"], ["n1", "n2", "n3", "n4", "n5"])
        self.assertEqual(actual["three"], ["n1", "n4", "n5"])
        self.assertEqual(actual["none"], [])

    def test_video_receipt_state_readiness_targets_counts_and_card_html(self):
        manifest = {
            "currentVersionId": "V2",
            "versions": [
                {
                    "versionId": "V1",
                    "fps": 30,
                    "timebase": {"num": 1, "den": 30},
                    "frames": 180,
                    "width": 640,
                    "height": 360,
                },
                {
                    "versionId": "V2",
                    "fps": 30,
                    "timebase": {"num": 1, "den": 30},
                    "frames": 192,
                    "width": 640,
                    "height": 360,
                },
            ],
            "lastFailure": {"batch": 1, "reason": "ffmpeg exit 1", "receiptId": "r0"},
        }
        notes = [
            {
                "id": "n1",
                "status": "drafted",
                "video": frame_anchor(),
                "videoReview": {
                    "state": "pending_review",
                    "versionId": "V2",
                    "summary": "片头+12帧并上移字幕",
                    "target": {"kind": "frame", "versionId": "V2", "frame": 108},
                },
            },
            {
                "id": "n2",
                "status": "open",
                "video": frame_anchor(),
                "videoReview": {"state": "needs_reply", "question": "要降噪还是换音乐？"},
            },
            {
                "id": "n3",
                "status": "open",
                "video": frame_anchor(),
                "videoReview": {"state": "needs_relocate", "relocateReason": "原片段已被整段剪掉"},
            },
            {
                "id": "n4",
                "status": "drafted",
                "video": frame_anchor(),
                "videoReview": {"state": "missing_evidence", "versionId": "V2", "summary": "缺 target"},
            },
            {
                "id": "n5",
                "status": "open",
                "video": frame_anchor(),
                "videoReview": {"state": "failed", "failure": "字号超出边界"},
            },
            {"id": "n6", "status": "open", "video": frame_anchor()},
            {"id": "n7", "status": "resolved", "video": frame_anchor()},
            {
                "id": "n8",
                "status": "drafted",
                "video": frame_anchor(),
                "videoReview": {
                    "state": "pending_review",
                    "versionId": "V2",
                    "summary": "补剪口上下文",
                    "target": {"kind": "cut", "versionId": "V2", "frame": 120, "contextStart": 108, "contextEnd": 132},
                },
            },
        ]
        for lang in ("zh-CN", "en"):
            actual = interaction_program(
                [(START, END)],
                "let videoRangeEnd=null;",
                "const manifest="
                + json.dumps(manifest, ensure_ascii=False)
                + ",notes="
                + json.dumps(notes, ensure_ascii=False)
                + ";"
                "return {"
                "states:notes.map(videoNoteState),"
                "ready:notes.map(n=>videoReceiptReady(n,manifest)),"
                "rangeWithFrame:videoReceiptReady({...notes[0],video:{startFrame:120,endFrame:180,versionId:'V1'}},manifest),"
                "oobReady:videoReceiptReady({...notes[0],videoReview:{...notes[0].videoReview,target:{kind:'frame',versionId:'V2',frame:192}}},manifest),"
                "noVerReady:videoReceiptReady({...notes[0],videoReview:{...notes[0].videoReview,versionId:'V9',target:{kind:'frame',versionId:'V9',frame:1}}},manifest),"
                "counts:videoStatusCounts(notes),"
                "cutText:videoTargetText(notes[7].videoReview.target,'V2',manifest),"
                "cards:notes.map(n=>videoReceiptCardHtml(n,manifest))"
                "};",
                locale=lang,
            )
            self.assertEqual(
                actual["states"],
                [
                    "pending_review",
                    "needs_reply",
                    "needs_relocate",
                    "missing_evidence",
                    "failed",
                    "unsubmitted",
                    "approved",
                    "pending_review",
                ],
            )
            self.assertEqual(actual["ready"], [True, False, False, False, False, False, False, True])
            self.assertFalse(actual["oobReady"])
            self.assertFalse(actual["rangeWithFrame"])
            self.assertFalse(actual["noVerReady"])
            self.assertEqual(
                actual["counts"],
                {
                    "pending_review": 2,
                    "needs_reply": 1,
                    "needs_relocate": 1,
                    "missing_evidence": 1,
                    "failed": 1,
                    "returned": 0,
                    "unsubmitted": 1,
                    "approved": 1,
                },
            )
            self.assertIn("120", actual["cutText"])
            self.assertIn("108", actual["cutText"])
            self.assertIn("132", actual["cutText"])
            self.assertIn('data-act="video-jump-orig"', actual["cards"][0])
            self.assertIn('data-act="video-jump-target"', actual["cards"][0])
            self.assertIn("片头+12帧并上移字幕", actual["cards"][0])
            self.assertIn("V2", actual["cards"][0])
            self.assertIn("要降噪还是换音乐？", actual["cards"][1])
            self.assertIn("原片段已被整段剪掉", actual["cards"][2])
            self.assertIn("字号超出边界", actual["cards"][4])


class VideoPageWiringTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.source = TEMPLATE.read_text(encoding="utf-8")

    def test_composer_drafts_queue_and_exchange_are_wired_to_video_field(self):
        for fragment in (
            "const VIDEO=__VIDEO__",
            "note.video=videoDraft",
            "video:videoDraft",
            "videoDraft=d.video",
            "cleanVideo(x.video)",
            "videoKey(x.video)",
            "g.videoLine(x.video)",
            "line.match(g.video)",
            'hasFeature(d,"video")',
            "videoBlocked()",
        ):
            with self.subTest(fragment=fragment):
                self.assertIn(fragment, self.source)

    def test_player_stage_and_note_navigation_are_wired(self):
        for fragment in (
            'id="videoPlayer"',
            'id="videoMarkLayer"',
            'id="videoSetStart"',
            'id="videoSetEnd"',
            'id="videoPlayRange"',
            "videoShowNote(x)",
            "videoDrawMarks()",
            "videoLayout()",
            "videoEl().getBoundingClientRect()",
            "videoSourceHint",
        ):
            with self.subTest(fragment=fragment):
                self.assertIn(fragment, self.source)

    def test_selective_handoff_and_source_project_are_wired(self):
        """提交弹窗的勾选列表、幂等键与源工程关联入口都接上服务契约。"""
        for fragment in (
            'id="handPick"',
            'id="hPickOld"',
            'id="handSource"',
            'id="vsrc"',
            'id="videoSourceBtn"',
            "renderHandPick()",
            "handoffNoteIds(avail,handPickSel)",
            'hasFeature(d,"selective-handoff")',
            'hasFeature(d,"video-source")',
            '"/api/video/source-project"',
            '"/api/video/source-project/confirm"',
            "body.noteIds=op.noteIds",
            "body.submitId=op.submitId",
        ):
            with self.subTest(fragment=fragment):
                self.assertIn(fragment, self.source)

    def test_video_receipt_versions_failures_and_card_controls_are_wired(self):
        for fragment in (
            'id="videoVersionPick"',
            'id="videoFailureBanner"',
            "let liveVideo=VIDEO",
            "if(VIDEO&&d.video)",
            "renderVideoVersions()",
            "renderVideoFailure()",
            "videoShowTarget(",
            "videoReceiptCardHtml(",
            "videoStatusCounts(",
            'unready:"unready"',
            'data-act="video-jump-orig"',
            'data-act="video-jump-target"',
            "videoReceiptReady(x,videoManifest())",
        ):
            with self.subTest(fragment=fragment):
                self.assertIn(fragment, self.source)

    def test_review_fixes_for_i18n_old_service_blocking_compare_close_and_rework_title(self):
        for fragment in (
            'data-msg="viewer.video.source.fieldFrames"',
            'tMsg("viewer.video.source.needRootEntry")',
            'tMsg("viewer.video.versionAria")',
            'tMsg("viewer.video.frameAria")',
            "videoCompareRefreshLocale()",
            'if(typeof videoCompareClose==="function")videoCompareClose();',
            '["needs_reply","returned","failed"].includes(st)',
            "docxBlocked()||pptxBlocked()||videoBlocked()",
            'hasPrior?"viewer.hand.pickTitleRework":"viewer.hand.pickTitle"',
        ):
            with self.subTest(fragment=fragment):
                self.assertIn(fragment, self.source)
        self.assertNotIn('aria-label="Video version"', self.source)
        self.assertNotIn('aria-label="Frame"', self.source)
        self.assertNotIn('{message:"root / entry"}', self.source)


def playback_page(expression, locale="en"):
    return interaction_program(
        [("// ---------- 视频审阅纯函数 ----------", "// ---------- 批注区：")],
        """
        const version=id=>({versionId:id,url:'/'+id,frames:180,fps:30,timebase:{num:1,den:30},
          width:640,height:360,duration:6});
        const VIDEO={currentVersionId:'V1',versions:['V1','V2'].map(version)},liveVideo=VIDEO,FILE=false;
        let videoActiveVersionId='V1',mode='server';
        const notes=[],isDone=()=>false,renderVideoSource=()=>{},saveDraft=()=>{},toast=()=>{};
        class Control extends EventTarget {
          constructor(){super();this.style={};this.hidden=false;this.value='';this.textContent='';this.clientWidth=400;this.clientHeight=400;}
          getAttribute(key){return this[key]??null}
          setAttribute(key,value){this[key]=value}
          appendChild(){}
          click(){if(!this.disabled&&!this.hidden)return this.onclick?.()}
          change(value){this.value=value;return this.onchange?.({target:this})}
        }
        class Player extends Control {
          constructor(){super();this.currentTime=0;this.paused=true;this.readyState=0;this.error=null;}
          get currentTime(){return this.time}
          set currentTime(seconds){this.time=Math.floor(seconds*1e6)/1e6}
          emit(name){this.dispatchEvent(new Event(name))}
          load(){this.error=null;this.readyState=0;this.paused=true;this.currentTime=0}
          metadata(width=640,height=360){
            this.videoWidth=width;this.videoHeight=height;this.readyState=1;this.emit('loadedmetadata');
          }
          pause(){if(!this.paused){this.paused=true;queueMicrotask(()=>this.emit('pause'))}}
          async play(){if(this.paused){this.paused=false;this.emit('play')}}
        }
        const controls=new Map(),players=['videoPlayer','videoCompareBefore','videoCompareAfter'];
        const get=id=>{
          if(!controls.has(id))controls.set(id,players.includes(id)?new Player():new Control());
          return controls.get(id);
        };
        const sr={getElementById:get},document={createElement:()=>new Control()};
        const player=get('videoPlayer'),before=get('videoCompareBefore'),after=get('videoCompareAfter');
        const range={id:'cue',status:'drafted',text:'cue',video:{v:1,versionId:'V1',startFrame:12,endFrame:30},
          videoReview:{state:'pending_review',versionId:'V2',target:{kind:'range',startFrame:15,endFrame:45}}};
        notes.push(range);
        """,
        "videoInit();\n" + expression,
        locale=locale,
    )


class VideoPlaybackTests(unittest.TestCase):
    def test_dimension_notice_names_the_displayed_cut_and_explains_mark_and_annotation_dimensions(self):
        for locale, decoded, registered in (("en", "decoded", "registered"), ("zh-CN", "解码", "登记")):
            with self.subTest(locale=locale):
                notices = playback_page(
                    """
                    const box=get('videoError'),messages=[];
                    for(const id of ['V1','V2']){
                      get('videoVersionPick').change(id);player.metadata(320,240);
                      messages.push([box.hidden,box.textContent]);
                    }
                    const layer=get('videoMarkLayer').style,draft=videoDraftAt();
                    const geometry=[layer.top,layer.height,draft.videoWidth,draft.videoHeight];
                    player.metadata();
                    const matchingHidden=box.hidden;
                    videoCompareOpen(range);
                    before.metadata(320,240);after.metadata(320,240);
                    for(const side of ['Before','After']){
                      const hint=get('videoCompare'+side+'MediaNotice');messages.push([hint.hidden,hint.textContent]);
                    }
                    return {messages,matchingHidden,geometry};
                    """,
                    locale,
                )
                for version, (hidden, message) in zip(("V1", "V2", "V1", "V2"), notices["messages"]):
                    self.assertFalse(hidden)
                    self.assertIn(version, message)
                    self.assertRegex(message, decoded + r"[^.。]*320 × 240")
                    self.assertRegex(message, registered + r"[^.。]*640 × 360")
                self.assertTrue(notices["matchingHidden"])
                self.assertEqual(notices["geometry"], ["50px", "300px", 640, 360])

    def test_native_pause_and_scrub_release_the_range_on_each_player(self):
        result = playback_page("""
            player.metadata();
            const observations={};
            for(const side of ['Main','Before','After']){
              const el=side==='Main'?player:get('videoCompare'+side),end=side==='After'?1.5:1;
              if(side!=='Main'){videoCompareOpen(range);before.metadata();after.metadata()}
              const seek=seconds=>{
                el.seeking=true;el.currentTime=seconds;el.emit('seeking');
                el.seeking=false;el.emit('seeked');el.emit('timeupdate');
              };
              const start=async()=>{
                if(side==='Main')videoShowNote(range);
                else get('videoComparePlay'+side).click();
                seek(el.currentTime);await Promise.resolve();
              };
              const crossed=()=>{el.currentTime=2.5;el.emit('timeupdate');return [el.paused,el.currentTime]};
              await start();el.currentTime=end+.1;el.emit('timeupdate');const normal=[el.paused,el.currentTime];
              await start();el.pause();await Promise.resolve();await el.play();const paused=crossed();
              await start();seek(.8);const scrubbed=crossed();
              await start();el.pause();seek(3);const pausedScrub=el.currentTime;
              observations[side]={normal,paused,scrubbed,pausedScrub};
            }
            return observations;
        """)
        for side, end in (("Main", 1), ("Before", 1), ("After", 1.5)):
            with self.subTest(side=side):
                self.assertEqual(
                    result[side],
                    {"normal": [True, end], "paused": [False, 2.5], "scrubbed": [False, 2.5], "pausedScrub": 3},
                )

    def test_late_metadata_cannot_play_a_closed_replaced_or_paused_comparison(self):
        result = playback_page("""
            const replacement={...range,id:'replacement',video:{versionId:'V2',frame:54},
              videoReview:{...range.videoReview,versionId:'V1',target:{kind:'frame',frame:57}}};
            notes.push(replacement);
            const observations={};
            for(const side of ['Before','After']){
              const el=get('videoCompare'+side),starts=[];
              el.addEventListener('play',()=>starts.push(el.src));
              const play=()=>get('videoComparePlay'+side).click();
              videoCompareOpen(range);play();get('videoCompareClose').click();el.metadata();
              const closed=[el.paused,el.currentTime,starts.splice(0)];
              el.load();videoCompareOpen(range);play();get('videoCompareClose').click();
              videoCompareOpen(replacement);el.metadata();
              const reopened=[el.paused,el.currentTime,starts.splice(0)];
              el.load();videoCompareOpen(range);play();videoCompareOpen(replacement);el.metadata();
              const switched=[el.paused,el.currentTime,starts.splice(0)];
              el.load();videoCompareOpen(range);play();get('videoComparePause'+side).click();el.metadata();
              const stopped=[el.paused,el.currentTime,starts.splice(0)];
              get('videoCompareClose').click();el.load();
              observations[side]={closed,reopened,switched,stopped};
            }
            videoShowNote(range);videoCompareOpen(range);player.metadata();
            return {observations,mainPaused:player.paused};
        """)
        for side, frame_time, range_start in (("Before", 1.8, 0.4), ("After", 1.9, 0.5)):
            with self.subTest(side=side):
                expected = [True, frame_time, []]
                self.assertEqual(
                    result["observations"][side],
                    {
                        "closed": [True, 0, []],
                        "reopened": expected,
                        "switched": expected,
                        "stopped": [True, range_start, []],
                    },
                )
        self.assertTrue(result["mainPaused"])

    def test_interval_endpoint_uses_the_same_decodable_frame_as_the_readout_and_annotation(self):
        result = playback_page("""
            range.video.endFrame=179;range.videoReview.target.endFrame=179;
            player.metadata();videoShowNote(range);
            const stop=el=>{el.currentTime=5.99;el.emit('timeupdate');el.emit('seeked')};
            stop(player);
            const main=[player.currentTime,get('videoFrame').value,videoDraftAt().frame];
            videoCompareOpen(range);before.metadata();after.metadata();
            const compare={};
            for(const side of ['Before','After']){
              const el=get('videoCompare'+side);
              el.readyState=2;el.emit('loadeddata');await get('videoComparePlay'+side).click();
              stop(el);compare[side]=[el.currentTime,get('videoCompare'+side+'Frame').textContent];
            }
            get('videoCompareToggleRelocate').click();get('videoRelocateUseFrame').click();
            return {main,compare,relocated:get('videoRelocateFrame').value};
        """)
        # 30 fps 的第 179 帧在 5.966666…秒开始。解码器能在该帧内表示的第一微秒是 5.966667 秒。
        self.assertEqual(result["main"], [5.966667, 179, 179])
        self.assertEqual(result["compare"], dict.fromkeys(("Before", "After"), [5.966667, "Current frame 179"]))
        self.assertEqual(result["relocated"], 179)

    def test_failed_media_can_be_retried_in_place_or_from_the_same_location(self):
        result = playback_page("""
            const fail=el=>{el.readyState=0;el.error={code:2};el.emit('error')};
            const recover=el=>{
              if(el.error)return false;
              el.metadata();el.readyState=2;el.emit('loadeddata');return true;
            };
            const v2=VIDEO.versions.pop();renderVideoVersions();recover(player);fail(player);
            const retry=get('videoRetry'),single=[get('videoVersionPick').hidden,!retry.hidden&&retry.textContent];
            retry.click();single.push(recover(player),retry.hidden,get('videoError').hidden);
            fail(player);videoShowNote(range);recover(player);
            const jump=[player.src,player.currentTime,player.paused,!!player.error];
            VIDEO.versions.push(v2);videoCompareOpen(range);recover(before);recover(after);
            const comparison={};
            for(const side of ['Before','After']){
              const el=get('videoCompare'+side);
              fail(el);videoCompareOpen(range);const reopened=[recover(el),el.currentTime,el.paused];
              fail(el);get('videoComparePlay'+side).click();const played=[recover(el),el.currentTime,el.paused];
              comparison[side]={reopened,played};
            }
            return {single,jump,comparison};
        """)
        self.assertEqual(result["single"], [True, "Retry playback", True, True, True])
        self.assertEqual(result["jump"], ["/V1", 0.4, False, False])
        for side, start in (("Before", 0.4), ("After", 0.5)):
            self.assertEqual(
                result["comparison"][side], {"reopened": [True, start, True], "played": [True, start, False]}
            )


if __name__ == "__main__":
    unittest.main()
