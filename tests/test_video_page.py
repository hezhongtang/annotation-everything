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


class VideoPagePureTests(unittest.TestCase):
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
            const preview=[videoDraft.startFrame,videoDraft.endFrame,player.currentTime,videoRangeEnd];
            calls.length=0;
            videoShowNote({id:"old",video:{versionId:"V1",startFrame:15,endFrame:30}});
            const loading=calls.slice();
            videoShowNote({id:"latest",video:{versionId:"V1",startFrame:45,endFrame:90}});
            player.readyState=1;player.dispatchEvent(new Event("loadedmetadata"));
            const loaded=[...calls,player.currentTime,videoRangeEnd];
            videoShowNote({id:"missing",video:{versionId:"V3",frame:1}});
            const missing=videoComparePair({video:{versionId:"V1",frame:0},
              videoReview:{versionId:"V3",target:{kind:"frame",versionId:"V3",frame:1}}},VIDEO);
            return {atMark,pastMark,editing,playingMark,preview,loading,loaded,missingUrl:missing.after.url,messages,src:player.src};
            """,
        )
        self.assertEqual((actual["atMark"], actual["pastMark"]), (1, 0))
        self.assertEqual(actual["editing"], "17")
        self.assertEqual(actual["playingMark"], [1, 30])
        self.assertEqual(actual["preview"], [120, 180, 4, 6])
        self.assertEqual(actual["loading"], [])
        self.assertEqual(actual["loaded"], ["pause", "play", 1.5, 3])
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


if __name__ == "__main__":
    unittest.main()
