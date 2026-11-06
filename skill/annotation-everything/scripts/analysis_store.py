"""分析存储：审阅目录下 analysis/<分析标识>/events.jsonl 的只追加事件日志。

- 事件只追加（0600），当前状态由事件日志推导，不落推导缓存；事件日志是唯一事实来源。
- 与批注存储共用审阅目录的锁（notes_store.locked）：搁住 / 解除源批注、移出待接收批次、
  结论转出批注与记录事件在同一把锁内完成。
- 轮次号在整份审阅内唯一（analysis/counter.json 分配），因为投递去重键里没有分析标识。
- 每个分析有单调递增的基线版本号（rev）：发起 +1，每次写回 +1；作答不改基线
  （Agent 领取期间审阅人作答不受影响，与「锁整个分析、作答除外」的语义一致）。
- 问答记录不截断。决策由系统根据审阅人的回答产生，不由 Agent 写入；
  结构提议经审阅人在轮次中确认后才进入问题树（不采纳的提议不再出现）。
- 稳定标识：分析 id、分析线（main / line2…）、检查点 cp1…、
  问题节点 n1…、决策 d1…（按事件重放顺序确定，重放同一日志得到同一批标识）、选项 id（Agent 提议内给定）。

与 notes_store 的分工：批注状态的 held / resolved / open 与批次快照的改写都通过
notes_store 的不加锁内部函数在本模块的锁内完成（同一次写入里保持两边一致）。

只用 Python 标准库。
"""
from __future__ import annotations

import contextlib
import copy
import json
import re
from pathlib import Path
from typing import Any, Callable

import notes_store as store
import ledger

ANALYSIS_DIR = "analysis"
EVENTS_FILE = "events.jsonl"
COUNTER_FILE = "counter.json"
SCHEMA = 1
LINE_MAIN = "main"   # 主线与后续分析线共用分析内的实体命名空间。
HOLD_REASON = "分析中"
# 轮次状态：open 等审阅人作答 → submitted 已交出（在分析队列里）→ claimed Agent 处理中
#   → done 写回完成；void 作废（结案等后续票使用）
ROUND_OPEN = ("open", "submitted")   # 结构变化可作废的未领取轮次
ROUND_ANSWERABLE = ("open", "submitted", "claimed")
MAX_PROPOSALS = 50   # 一轮写回最多提出的结构提议数（防误传超大输入；问答文本本身不设上限）
MAX_ROUND_KEEP = 0   # 0 = 不截断：问答记录全部保留
PROP_KEY_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
OPT_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,32}$")


class AnalysisInputError(store.HandoffError):
    """可本地化的输入错误；内部诊断不直接作为页面文案。"""

    def __init__(self, message: str):
        super().__init__(message, code="analysis-invalid-input")


def _analysis_path(review_dir: Path, *parts: str) -> Path:
    """分析数据只使用当前审阅内的真实路径，不跟随目录或文件符号链接。"""
    root = Path(review_dir).resolve() / ANALYSIS_DIR
    path = root.joinpath(*parts)
    try:
        safe = root.resolve() == root and path.resolve() == path
    except (OSError, RuntimeError) as error:
        raise store.NoteError("分析数据路径不可用") from error
    if not safe:
        raise store.NoteError("分析数据路径不能使用符号链接或跨出当前审阅目录")
    return path


def _events_path(review_dir: Path, analysis_id: str) -> Path:
    if not isinstance(analysis_id, str) or store.ID_RE.fullmatch(analysis_id) is None:
        raise store.NoteError("分析标识只允许 1–64 个字母、数字、下划线或连字符")
    return _analysis_path(review_dir, analysis_id, EVENTS_FILE)


def _append_event(review_dir: Path, analysis_id: str, event: dict[str, Any]) -> None:
    store.append_private(_events_path(review_dir, analysis_id),
                         json.dumps(event, ensure_ascii=False, allow_nan=False))


def _read_events(review_dir: Path, analysis_id: str) -> list[dict[str, Any]]:
    path = _events_path(review_dir, analysis_id)
    if not path.exists():
        raise store.NotFound(f"analysis#{analysis_id}")
    events = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            events.append(json.loads(line))
    if not events or events[0].get("type") != "started" or not isinstance(events[0].get("schema"), int):
        raise ValueError(f"{path} 结构不对：缺少带 schema 的发起事件")
    if events[0]["schema"] > SCHEMA:
        raise ValueError(f"{path} 由更高版本写入（schema {events[0]['schema']}），本版本读不了")
    return events


def _counter(review_dir: Path) -> dict[str, Any]:
    path = _analysis_path(review_dir, COUNTER_FILE)
    if path.exists():
        value = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(value, dict) or not isinstance(value.get("roundSeq"), int):
            raise ValueError(f"{path} 结构不对")
        return value
    return {"version": 1, "roundSeq": 0}


def _save_counter(review_dir: Path, counter: dict[str, Any]) -> None:
    root = _analysis_path(review_dir)
    root.mkdir(mode=0o700, exist_ok=True)
    store._atomic_write(_analysis_path(review_dir, COUNTER_FILE), json.dumps(counter, ensure_ascii=False) + "\n")


def _next_round(review_dir: Path) -> int:
    counter = _counter(review_dir)
    counter["roundSeq"] = int(counter["roundSeq"]) + 1
    _save_counter(review_dir, counter)
    return counter["roundSeq"]


def _next_submit_order(review_dir: Path) -> int:
    """审阅内共享的提交序号：改稿批次与分析轮次按它比交出先后，一次只处理一项。

    秒级时间戳分不出同一秒内的先后，两个队列共用一个单调序号（记在 handoff.json 顶层）；
    旧数据没有序号时，比较方退回时间戳。调用方须已持有审阅目录锁。"""
    doc = store.handoff_load(review_dir)
    doc["submitSeq"] = int(doc.get("submitSeq", 0)) + 1
    store._handoff_commit(review_dir, doc, {"op": "submit-order", "seq": doc["submitSeq"]})
    return doc["submitSeq"]


# ---------- 推导：事件日志 → 当前状态 ----------

def _is_active(state: dict[str, Any], node_id: str) -> bool:
    """问题节点是否处在当前线选择枝上：父节点已决到挂载它的那个选项。"""
    node = state["nodes"].get(node_id)
    if node is None or node.get("status") == "void":
        return False
    parent = node.get("parent")
    if not parent:
        return True
    if not _is_active(state, parent["node"]):
        return False
    pn = state["nodes"].get(parent["node"])
    decision = state["decisions"].get(pn.get("decision") or "") if pn else None
    return bool(decision) and decision["option"] == parent["option"]


def _decide(state: dict[str, Any], node: dict[str, Any], option: str, round_no: int | None) -> dict[str, Any]:
    """作答或重裁到某个选项：同一选项有旧决策就复用（同一标识），否则新建。决策由系统产生。"""
    for did, decision in state["decisions"].items():
        if (decision["node"] == node["id"] and decision["option"] == option
                and not any((state["assumptions"].get(aid) or {}).get("refuted")
                            for aid in decision.get("assumptions", []))):
            if did not in node["history"]:
                node["history"].append(did)
            node["decision"], node["status"] = did, "decided"
            node.pop("reopened", None)
            return decision
    did = f"d{state['seq']['decision'] + 1}"
    state["seq"]["decision"] += 1
    decision = {"id": did, "node": node["id"], "option": option, "label": node["options"][option],
                "round": round_no, "line": state["line"], "assumptions": []}
    state["decisions"][did] = decision
    node["history"].append(did)
    node["decision"], node["status"] = did, "decided"
    node.pop("reopened", None)
    return decision


def _confirm_proposal(state: dict[str, Any], proposal: dict[str, Any]) -> str:
    """结构提议确认后进入问题树：分配稳定节点标识（按确认顺序，重放确定）。"""
    nid = f"n{state['seq']['node'] + 1}"
    state["seq"]["node"] += 1
    options = {o["id"]: o["label"] for o in proposal["options"]}
    state["nodes"][nid] = {"id": nid, "text": proposal["text"], "short": proposal.get("short") or proposal["text"][:24],
                           "options": options, "recommend": proposal["recommend"],
                           "parent": proposal.get("parent"), "status": "pending",
                           "decision": None, "history": []}
    return nid


def _suspend_node(node: dict[str, Any]) -> None:
    if node["status"] != "void":
        node["inactiveStatus"] = node["status"]
        node["status"] = "void"


def _restore_node(node: dict[str, Any]) -> None:
    node["status"] = node.pop("inactiveStatus", "decided" if node.get("decision") else "pending")


def _share_options(state: dict[str, Any], node_id: str) -> dict[str, str]:
    """同一问题的选项身份跨线共享；选择哪个选项仍由各线节点投影保存。"""
    options: dict[str, str] = {}
    nodes = [line["nodes"][node_id] for line in state["lines"].values() if node_id in line["nodes"]]
    for node in nodes:
        for oid, label in node["options"].items():
            if oid in options and options[oid] != label:
                raise ValueError("同一问题的选项标识对应了不同原文")
            options[oid] = label
    for node in nodes:
        node["options"] = options
    return options


def _apply_structure_change(state: dict[str, Any], round_: dict[str, Any], key: str,
                            proposal: dict[str, Any], accepted: bool) -> None:
    """拆合/作废只改变当前线的投影；旧节点、决策和已确认的新节点标识一直保留。"""
    created = round_.setdefault("structureNodes", {})
    if accepted:
        for target in proposal["targets"]:
            _suspend_node(state["nodes"][target])
        if key not in created:
            created[key] = [_confirm_proposal(state, replacement) for replacement in proposal["replacements"]]
        else:
            for nid in created[key]:
                _restore_node(state["nodes"][nid])
    elif key in created:
        for target in proposal["targets"]:
            _restore_node(state["nodes"][target])
        for nid in created[key]:
            _suspend_node(state["nodes"][nid])


def _apply_answer(state: dict[str, Any], round_: dict[str, Any], key: str, value: dict[str, Any]) -> None:
    previous = round_["answers"].get(key)
    round_["answers"][key] = value
    if key.startswith("D:"):
        state["deadEnds"][key[2:]]["status"] = "confirmed" if value["kind"] == "accept" else "rejected"
        return
    if key.startswith("U:"):
        draft = state["updates"][key[2:]]
        draft["status"] = "confirmed" if value["kind"] == "accept" else "rejected"
        state["updateRequests"][draft["noteId"]]["status"] = "confirmed" if value["kind"] == "accept" else "requested"
        return
    if key.startswith("A:"):
        state["assumptions"][key[2:]]["confirmed"][round_.get("line", LINE_MAIN)] = (
            "confirmed" if value["kind"] == "accept" else "rejected")
        return
    if key.startswith("S:"):
        # 原话允许修订；相同判断不能再次恢复节点，覆盖后来搁置等状态。
        if previous is not None and previous["kind"] == value["kind"]:
            return
        proposal = next(p for p in round_["proposals"] if "S:" + p["key"] == key)
        change = proposal.get("op", "add") != "add"
        confirmed = round_.setdefault("proposalNodes", {})
        node_id = confirmed.get(key)
        if value["kind"] == "accept":
            round_["rejected"].discard(key)
            state["rejectedProposals"] = [p for p in state["rejectedProposals"] if "S:" + p["key"] != key]
            if change:
                _apply_structure_change(state, round_, key, proposal, True)
            elif node_id is None:
                confirmed[key] = _confirm_proposal(state, next(p for p in round_["proposals"] if "S:" + p["key"] == key))
            else:
                _restore_node(state["nodes"][node_id])
        else:
            round_["rejected"].add(key)
            if proposal not in state["rejectedProposals"]:
                state["rejectedProposals"].append(proposal)
            if change:
                _apply_structure_change(state, round_, key, proposal, False)
            elif node_id is not None:
                _suspend_node(state["nodes"][node_id])
        return
    node_id = key[2:]
    node = state["nodes"][node_id]
    if value["kind"] in ("park", "verify"):
        node["status"] = "parked" if value["kind"] == "park" else "verifying"
        node["decision"] = None
        return
    if value["kind"] == "text":
        _share_options(state, node_id)
        # 原话也是可重裁的稳定选项；相同原话复用同一选项与决策。
        option = next((oid for oid, text in node["options"].items()
                       if oid.startswith("text-") and text == value["text"]), None)
        if option is None:
            index = 1
            while f"text-{index}" in node["options"]:
                index += 1
            option = f"text-{index}"
            node["options"][option] = value["text"]
    else:
        option = node["recommend"] if value["kind"] == "accept" else value["option"]
    _decide(state, node, option, round_["seq"])


def _frontier_question_items(state: dict[str, Any]) -> list[dict[str, Any]]:
    """当前前沿上待问的问题条目：已入树、未决、处在当前选择枝上的节点（含挂载条件未满足之外的全部）。"""
    return [{"key": "Q:" + nid, "type": "question", "node": nid}
            for nid in sorted(state["nodes"], key=lambda x: int(x[1:]))
            if state["nodes"][nid]["status"] == "pending" and _is_active(state, nid)]


def _round_items(state: dict[str, Any], round_: dict[str, Any]) -> list[dict[str, Any]]:
    """按死局、更新、假设、结构、前沿问题组合本轮条目，保留有效预填原答。

    问题条目随状态动态出现（确认提议入树后同轮即可作答；没答的问题一直留到下一轮）。"""
    if round_["status"] in ("done", "void"):
        return [{"key": "S:" + p["key"], "type": "structure", "proposal": p} for p in round_["proposals"]]
    items = [{"key": "D:" + end["id"], "type": "deadend", "deadEnd": end["id"]}
             for end in state["deadEnds"].values()
             if end["line"] == round_.get("line", LINE_MAIN) and end["status"] == "pending"]
    items.extend({"key": "U:" + draft["id"], "type": "update", "update": draft["id"]}
             for draft in state.get("updates", {}).values()
             if draft.get("line", LINE_MAIN) == round_.get("line", LINE_MAIN) and _update_valid(state, draft)
             and "U:" + draft["id"] not in round_["answers"])
    items.extend([{"key": "A:" + aid, "type": "assumption", "assumption": aid}
             for aid, assumption in state["assumptions"].items()
             if not assumption["refuted"] and assumption["confirmed"].get(round_.get("line", LINE_MAIN), "pending") == "pending"])
    items.extend({"key": "S:" + p["key"], "type": "structure", "proposal": p}
             for p in round_["proposals"]
             if "S:" + p["key"] not in round_["answers"] and "S:" + p["key"] not in round_["rejected"])
    items.extend(_frontier_question_items(state))
    keys = {item["key"] for item in items}
    for item in round_.get("carriedItems", []):
        if item["key"] not in keys and _carry_item_valid(state, item, round_["answers"].get(item["key"])):
            items.append(item)
    priority = {"deadend": 0, "update": 1, "assumption": 2, "structure": 3, "question": 4}
    return sorted(items, key=lambda item: priority[item["type"]])


def _replay(events: list[dict[str, Any]]) -> dict[str, Any]:
    first = events[0]
    state: dict[str, Any] = {
        "id": None, "schema": first["schema"], "status": "active", "rev": 0,
        "focus": first.get("focus", ""), "sourceNote": first.get("sourceNote"),
        "sourceCopy": first.get("sourceCopy") or {},
        "sourceState": "held" if first.get("sourceNote") else "none", "sourceResolvedNote": None,
        "line": LINE_MAIN, "seq": {"node": 0, "decision": 0, "draft": 0},
        "nodes": {}, "decisions": {}, "rounds": [], "drafts": [],
        "carry": {}, "pendingProposals": {}, "updateRequests": {}, "updates": {}, "rejections": [],
        "assumptions": {}, "facts": {}, "rejectedProposals": [], "checkpoints": [], "deadEnds": {},
    }
    state["lines"] = {LINE_MAIN: {"id": LINE_MAIN, "ordinal": 1, "from": None, "nodes": state["nodes"]}}
    current_line = LINE_MAIN
    for event in events:
        kind = event.get("type")
        event_line = event.get("line", current_line)
        if "round" in event and kind != "round-requested":
            event_line = _round(state, event["round"]).get("line", LINE_MAIN)
        if event_line in state["lines"]:
            state["line"] = event_line
            state["nodes"] = state["lines"][event_line]["nodes"]
        if kind == "started":
            state["id"] = event.get("id")
            state["rev"] = event.get("rev", 1)
        elif kind == "status-changed":
            state["status"] = event["status"]
        elif kind == "round-requested":
            state["rounds"].append({"seq": event["round"], "kind": event.get("kind", "next"),
                                    "line": event.get("line", LINE_MAIN),
                                    "status": "open", "proposals": event.get("items") or [],
                                    "answers": event.get("answers") or {}, "rejected": set(), "at": event.get("at"),
                                    "carriedItems": event.get("carriedItems") or [],
                                    "proposalNodes": event.get("proposalNodes") or {},
                                    "structureNodes": event.get("structureNodes") or {},
                                    "order": event.get("order"),
                                    "baseRev": None, "claimedBy": None, "note": None})
            if event.get("queued"):
                state["rounds"][-1]["status"] = "submitted"
        elif kind == "round-submitted":
            _round(state, event["round"])["status"] = "submitted"
            _round(state, event["round"])["at"] = event.get("at")
            _round(state, event["round"])["order"] = event.get("order")
        elif kind == "round-claimed":
            round_ = _round(state, event["round"])
            round_.update(status="claimed", baseRev=event.get("baseRev"), claimedBy=event.get("by"))
        elif kind == "round-done":
            round_ = _round(state, event["round"])
            round_.update(status="done", note=event.get("note"))
            if event.get("context") is not None:
                round_["context"] = copy.deepcopy(event["context"])
            state["rev"] = event.get("rev", state["rev"] + 1)
            for key, value in round_["answers"].items():
                if key.startswith("Q:") and value.get("kind") == "verify":
                    node = state["nodes"].get(key[2:])
                    if node and node["status"] == "verifying":
                        node["status"] = "pending"
            for candidate in event.get("assumptions", []):
                assumption = next((a for a in state["assumptions"].values() if a["key"] == candidate["key"]), None)
                if assumption is None:
                    aid = f"a{len(state['assumptions']) + 1}"
                    assumption = {"id": aid, **candidate, "confirmed": {}, "refuted": False}
                    state["assumptions"][aid] = assumption
                assumption["confirmed"].setdefault(round_.get("line", LINE_MAIN), "pending")
                for did in candidate["decisions"]:
                    if did not in assumption["decisions"]:
                        assumption["decisions"].append(did)
                    dependencies = state["decisions"][did].setdefault("assumptions", [])
                    if assumption["id"] not in dependencies:
                        dependencies.append(assumption["id"])
            for fact in event.get("facts", []):
                if not any(f["key"] == fact["key"] for f in state["facts"].values()):
                    fid = f"f{len(state['facts']) + 1}"
                    state["facts"][fid] = {"id": fid, **fact, "round": event["round"]}
            for end in event.get("deadEnds", []):
                state["deadEnds"].setdefault(end["id"], copy.deepcopy(end))
        elif kind == "round-voided":
            round_ = _round(state, event["round"])
            round_.update(status="void", voidReason=event.get("reason"))
            if event.get("context") is not None:
                round_["context"] = copy.deepcopy(event["context"])
            state["carry"].setdefault(round_.get("line", LINE_MAIN), {}).update(event.get("carry") or {})
            state["pendingProposals"].setdefault(round_.get("line", LINE_MAIN), {}).update(
                {p["key"]: p for p in event.get("proposals") or []})
        elif kind == "carry-consumed":
            state["carry"].pop(event.get("line", LINE_MAIN), None)
            state["pendingProposals"].pop(event.get("line", LINE_MAIN), None)
        elif kind == "writeback-rejected":
            state["rejections"].append(event)
        elif kind == "checkpoint-recorded":
            state["checkpoints"].append(copy.deepcopy(event["checkpoint"]))
            state["rev"] = event.get("rev", state["rev"])
        elif kind == "checkpoint-sealed":
            next(c for c in state["checkpoints"] if c["id"] == event["checkpoint"])["sealed"] = True
            state["rev"] = event["rev"]
        elif kind == "line-forked":
            checkpoint = next(c for c in state["checkpoints"] if c["id"] == event["checkpoint"])
            state["lines"][event["line"]] = {
                "id": event["line"], "ordinal": len(state["lines"]) + 1,
                "from": checkpoint["id"], "at": event.get("at"),
                "nodes": copy.deepcopy(checkpoint["snapshot"]["nodes"]),
            }
            for nid in state["lines"][event["line"]]["nodes"]:
                _share_options(state, nid)
            for aid, assumption in checkpoint["snapshot"]["assumptions"].items():
                state["assumptions"][aid]["confirmed"][event["line"]] = (
                    assumption["confirmed"].get(checkpoint["line"], "pending"))
            state["pendingProposals"][event["line"]] = {
                p["key"]: copy.deepcopy(p) for p in checkpoint["snapshot"].get("pendingProposals", [])}
            if event.get("carry"):
                state["carry"][event["line"]] = copy.deepcopy(event["carry"])
            current_line = event["line"]
            state["rev"] = event["rev"]
        elif kind == "assumption-refuted":
            state["assumptions"][event["assumption"]]["refuted"] = True
            for end in state["deadEnds"].values():
                if end["status"] in ("pending", "confirmed") and event["assumption"] in end["assumptions"]:
                    end.update(status="review", releasedBy=event["assumption"])
            if event.get("checkpoint"):
                state["lines"][event["line"]].setdefault("basedOnRefuted", []).append(event["assumption"])
            state["rev"] = event["rev"]
        elif kind == "decisions-reopened":
            for entry in event["nodes"]:
                node = state["nodes"][entry["node"]]
                node["reopened"] = {**entry, "assumption": event["assumption"], "reason": "assumption-refuted"}
                node["decision"], node["status"] = None, "pending"
        elif kind == "line-switched":
            current_line = event["line"]
            state["rev"] = event["rev"]
        elif kind == "analysis-closed":
            state["status"] = "closed"
            state["rev"] = event["rev"]
        elif kind == "analysis-reopened":
            state["status"] = "active"
            state["rev"] = event["rev"]
        elif kind == "redecided":
            _decide(state, state["nodes"][event["node"]], event["option"], None)
            state["rev"] = event["rev"]
        elif kind == "note-update-requested":
            state["updateRequests"][event["noteId"]] = {**event, "status": "requested"}
            state["rev"] = event["rev"]
        elif kind == "note-update-drafted":
            draft = event["draft"]
            state["updates"][draft["id"]] = draft
            state["updateRequests"][draft["noteId"]].update(status="drafted", draft=draft["id"])
        elif kind in ("note-kept", "note-deleted"):
            if event["noteId"] in state["updateRequests"]:
                state["updateRequests"][event["noteId"]]["status"] = "cancelled"
        elif kind == "answered":
            _apply_answer(state, _round(state, event["round"]), event["key"], event.get("value") or {})
        elif kind == "source-released":
            state["sourceState"] = "released"
        elif kind == "converted":
            state["drafts"].append(event["draft"])
        elif kind == "convert-confirmed":
            for draft in state["drafts"]:
                if draft["id"] == event["draft"]:
                    draft["status"], draft["note"] = "confirmed", event.get("note")
            if state["sourceNote"]:
                state["sourceState"] = "resolved"
                state["sourceResolvedNote"] = event.get("note")
        state["line"] = current_line
        state["nodes"] = state["lines"][current_line]["nodes"]
    for round_ in state["rounds"]:
        round_["rejected"] = sorted(round_["rejected"])
        round_["items"] = _round_items(_line_view(state, round_.get("line", LINE_MAIN)), round_)
    state.pop("seq", None)   # 内部分配器不外露
    return state


def _line_view(state: dict[str, Any], line: str) -> dict[str, Any]:
    """供本轮规则读取所属线的投影；实体注册表与分析级版本仍共享。"""
    return {**state, "line": line, "nodes": state["lines"][line]["nodes"]}


def _round(state: dict[str, Any], seq: int) -> dict[str, Any]:
    round_ = next((r for r in state["rounds"] if r["seq"] == seq), None)
    if round_ is None:
        raise store.NotFound(f"round#{seq}")
    return round_


def load(review_dir: Path, analysis_id: str) -> dict[str, Any]:
    """读某个分析的当前状态（由事件日志推导）。"""
    state = _replay(_read_events(Path(review_dir), analysis_id))
    claimed = next((r for r in state["rounds"] if r["status"] == "claimed"), None)
    state["protection"] = {"locked": claimed is not None, "round": claimed["seq"] if claimed else None}
    for line in state["lines"]:
        view = _line_view(state, line)
        for node in view["nodes"].values():
            node["branch"] = node_branch(view, node["id"])
            node["needsReview"] = _decision_needs_review(view, node.get("decision"))
    return state


def _analyses_ids(review_dir: Path) -> list[str]:
    root = _analysis_path(review_dir)
    if not root.is_dir():
        return []
    ids = sorted(p.parent.name for p in root.glob(f"*/{EVENTS_FILE}"))
    for analysis_id in ids:
        _events_path(review_dir, analysis_id)
    return ids


def list_analyses(review_dir: Path) -> list[dict[str, Any]]:
    """列出审阅里的分析与当前摘要。"""
    out = []
    for aid in _analyses_ids(Path(review_dir)):
        state = load(Path(review_dir), aid)
        out.append({"id": aid, "focus": state["focus"], "status": state["status"],
                    "sourceNote": state["sourceNote"], "sourceState": state["sourceState"],
                    "rev": state["rev"], "line": state["line"],
                    "rounds": len(state["rounds"]),
                    "openRound": next((r["seq"] for r in state["rounds"]
                                       if r.get("line", LINE_MAIN) == state["line"] and r["status"] in ROUND_OPEN), None)})
    return out


def _review_pages(review_dir: Path) -> list[dict[str, Any]]:
    pages = ledger.read_manifest(review_dir).get("pages")
    return [page for page in pages if isinstance(page, dict)] if isinstance(pages, list) else []


def _stable_page(page: dict[str, Any]) -> bool:
    key = page.get("key")
    return (isinstance(key, str) and bool(key) and page.get("stable") is not False
            and re.fullmatch(r"(?:p|page)?[-_]?\d+", key, re.I) is None)


def _source_location(review_dir: Path, source: dict[str, Any]) -> dict[str, Any]:
    """转出与确认时按当前页面映射定位，原文副本保持发起时的页码与标题。"""
    location = {"page": source.get("page", 1), "title": source.get("title", "")}
    key = source.get("key")
    if not key:
        return location
    location["key"] = key
    if re.fullmatch(r"(?:p|page)?[-_]?\d+", key, re.I):
        return location   # 页码生成的键与既有批注一样，不用于重定位。
    matches = [page for page in _review_pages(review_dir) if page.get("key") == key]
    if (len(matches) == 1 and _stable_page(matches[0]) and isinstance(matches[0].get("n"), int)
            and not isinstance(matches[0]["n"], bool) and matches[0]["n"] > 0):
        location.update(page=matches[0]["n"], title=matches[0].get("title") or location["title"])
    else:
        location["orphan"] = True
    return location


# ---------- 写操作（都在批注存储的同一把锁内） ----------

def _activate_locked(review_dir: Path, analysis_id: str, *, by: str) -> None:
    """焦点切换不改变分析内容或基线；暂停分析的轮次仍在共享队列中。"""
    for row in list_analyses(review_dir):
        status = "active" if row["id"] == analysis_id else "paused"
        if row["status"] in ("active", "paused") and row["status"] != status:
            _append_event(review_dir, row["id"], {"type": "status-changed", "at": store.now_iso(),
                                                 "by": by, "status": status})


def activate(review_dir: Path, analysis_id: str, *, by: str = "page") -> dict[str, Any]:
    """将审阅焦点切到这项分析，不作废、重排或解锁任何轮次。"""
    review_dir = Path(review_dir)
    with store.locked(review_dir):
        state = load(review_dir, analysis_id)
        if state["status"] not in ("active", "paused"):
            raise store.HandoffError("这项分析已经结案，请先重新打开", code="analysis-closed")
        _activate_locked(review_dir, analysis_id, by=by)
        return load(review_dir, analysis_id)


def pause(review_dir: Path, analysis_id: str, *, by: str = "page") -> dict[str, Any]:
    """暂停焦点，已交出的轮次照常处理。"""
    review_dir = Path(review_dir)
    with store.locked(review_dir):
        state = load(review_dir, analysis_id)
        if state["status"] == "active":
            _append_event(review_dir, analysis_id, {"type": "status-changed", "at": store.now_iso(),
                                                   "by": by, "status": "paused"})
        return load(review_dir, analysis_id)


def start_question(review_dir: Path, focus: str, *, page: int | None = None, by: str = "page",
                   authorize: Callable[[], None] | None = None) -> tuple[dict[str, Any], int]:
    """从页或空白焦点发起；不创建或搁住批注，页面引用保存稳定键。"""
    review_dir = Path(review_dir)
    with store.locked(review_dir):
        if authorize is not None:
            authorize()
        if not isinstance(focus, str) or not focus.strip():
            raise store.HandoffError("请写下要分析的问题", code="focus-required")
        source = {"text": store.fix_text(focus)}
        if page is not None:
            if not isinstance(page, int) or isinstance(page, bool) or page < 1:
                raise store.HandoffError("请选择审阅中的页面", code="page-not-found")
            pages = _review_pages(review_dir)
            entry = next((p for p in pages if p.get("n") == page), None)
            if entry is None:
                raise store.HandoffError("请选择审阅中的页面", code="page-not-found")
            source.update(page=page, title=entry.get("title", ""))
            if _stable_page(entry) and sum(p.get("key") == entry["key"] for p in pages) == 1:
                source["key"] = entry["key"]
        aid = store.new_id()
        root = _events_path(review_dir, aid).parent
        _counter(review_dir)
        root.mkdir(mode=0o700, parents=True, exist_ok=True)
        seq = _next_round(review_dir)
        at = store.now_iso()
        _append_event(review_dir, aid, {"type": "started", "schema": SCHEMA, "at": at, "id": aid,
                                       "rev": 1, "focus": source["text"], "sourceCopy": source,
                                       "sourceNote": None, "by": by})
        _append_event(review_dir, aid, {"type": "round-requested", "at": at, "by": by,
                                       "round": seq, "kind": "opening", "items": [],
                                       "queued": True, "order": _next_submit_order(review_dir)})
        _activate_locked(review_dir, aid, by=by)
        return load(review_dir, aid), seq


def suggest(review_dir: Path, note_id: str, *, reason: str = "", by: str = "agent",
            authorize: Callable[[], None] | None = None) -> dict[str, Any]:
    """Agent 仅标记建议；不会创建分析、搁住或交出批注。"""
    review_dir = Path(review_dir)
    with store.locked(review_dir):
        if authorize is not None:
            authorize()
        doc = store.load(review_dir)
        note = store._find(doc, note_id)
        if note.get("status", "open") != "open":
            raise store.HandoffError("只能对未处理批注建议展开分析", code="suggest-note-not-open")
        if not isinstance(reason, str):
            raise AnalysisInputError("建议原因须为文本")
        note["analysisSuggestion"] = {"reason": store.fix_text(reason), "at": store.now_iso(), "by": by}
        note["updatedAt"] = store.now_iso()
        store._commit(review_dir, doc, {"op": "analysis-suggested", "by": by, "id": note_id})
        return note


def start(review_dir: Path, note_id: str, *, by: str = "page",
          authorize: Callable[[], None] | None = None) -> tuple[dict[str, Any], int]:
    """从未处理批注发起分析：批注搁住（分析中）、从待接收批次移出（整批空了作废），
    自动请求开题（第一轮直接入分析队列）。返回 (分析状态, 开题轮次号)。"""
    review_dir = Path(review_dir)
    with store.locked(review_dir):
        if authorize is not None:
            authorize()
        analysis_id = store.new_id()
        root = _events_path(review_dir, analysis_id).parent
        _counter(review_dir)   # 在改动批注与批次前拒绝越界或损坏的计数器。
        doc = store.load(review_dir)
        note = store._find(doc, note_id)
        status = note.get("status", "open")
        if status != "open":
            raise store.HandoffError(f"{note_id} 不是未处理状态（{status}），不能从它发起分析",
                                     code="note-not-open", id=note_id, status=status)
        handoff = store.handoff_load(review_dir)
        removed: dict[str, Any] = {}
        for item in handoff["items"]:
            ids = item.get("openIds") or []
            if note_id not in ids:
                continue
            if item.get("status") in ("claimed", "reviewing"):
                raise store.HandoffError(f"{note_id} 在第 {item.get('seq')} 批里，{item.get('claimedBy') or 'Agent'} "
                                         "已经在改了，不能从它发起分析", code="note-in-progress",
                                         id=note_id, seq=item.get("seq"), by=item.get("claimedBy"))
            if item.get("status") == "pending":
                item["openIds"] = [x for x in ids if x != note_id]
                item["openCount"] = len(item["openIds"])
                item["pages"] = sorted({int(n.get("page", 0) or 0) for n in doc["notes"]
                                        if n.get("id") in item["openIds"]})
                if not item["openIds"]:
                    item["status"] = "void"
                removed = {"seq": item.get("seq"), "voided": item["status"] == "void"}
        if removed:
            store._handoff_commit(review_dir, handoff, {"op": "handoff-update", "by": by,
                                                        "seq": removed["seq"], "removed": note_id,
                                                        "voided": removed["voided"]})
        note["status"] = "held"
        note["hold"] = HOLD_REASON
        note["heldBy"] = analysis_id
        note["updatedAt"] = store.now_iso()
        store._commit(review_dir, doc, {"op": "held", "by": by, "id": note_id,
                                        "analysis": analysis_id, "reason": HOLD_REASON})
        root.mkdir(mode=0o700, parents=True, exist_ok=True)
        round_no = _next_round(review_dir)
        source_copy = {k: note.get(k) for k in ("page", "title", "target", "text", "quote", "quoteFrom", "key") if note.get(k) is not None}
        if not source_copy.get("key"):
            pages = _review_pages(review_dir)
            page = next((page for page in pages if page.get("n") == note.get("page") and _stable_page(page)), None)
            if page is not None and sum(other.get("key") == page["key"] for other in pages) == 1:
                source_copy["key"] = page["key"]
        at = store.now_iso()
        _append_event(review_dir, analysis_id, {"type": "started", "schema": SCHEMA, "at": at, "id": analysis_id,
                                                "rev": 1, "focus": note.get("text", ""), "by": by,
                                                "sourceNote": note_id, "sourceCopy": source_copy})
        _append_event(review_dir, analysis_id, {"type": "round-requested", "at": at, "by": by,
                                                "round": round_no, "kind": "opening", "items": [],
                                                "queued": True, "order": _next_submit_order(review_dir)})
        _activate_locked(review_dir, analysis_id, by=by)
        return load(review_dir, analysis_id), round_no


def answer(review_dir: Path, analysis_id: str, round_no: int, key: str, value: dict[str, Any], *,
           by: str = "page") -> dict[str, Any]:
    """在轮次中作答：确认 / 不采纳结构提议，或用推荐答案 / 选一个选项回答问题。

    回答按题目幂等（改主意就再答一次，取最后一次）；已交出的回答合并进同一轮。"""
    review_dir = Path(review_dir)
    with store.locked(review_dir):
        displayed = load(review_dir, analysis_id)
        _guard_open(displayed)
        state = displayed
        round_ = _round(state, round_no)
        state = _line_view(state, round_.get("line", LINE_MAIN))
        if round_["status"] not in ROUND_ANSWERABLE:
            raise store.HandoffError(f"第 {round_no} 轮{('已被 ' + str(round_.get('claimedBy')) + ' 接手') if round_['status'] == 'claimed' else '已经处理完了'}"
                                     "，不能再作答", code="round-not-answerable",
                                     round=round_no, status=round_["status"])
        item = next((i for i in round_["items"] if i["key"] == key), None)
        if item is None and key in round_["answers"]:
            if key.startswith("S:"):
                proposal = next((p for p in round_["proposals"] if "S:" + p["key"] == key), None)
                if proposal is not None:
                    item = {"key": key, "type": "structure", "proposal": proposal}
            elif key.startswith("Q:") and _is_active(state, key[2:]):
                item = {"key": key, "type": "question", "node": key[2:]}
            elif key.startswith("U:") and key[2:] in state["updates"]:
                item = {"key": key, "type": "update", "update": key[2:]}
            elif key.startswith("A:") and key[2:] in state["assumptions"]:
                item = {"key": key, "type": "assumption", "assumption": key[2:]}
            elif key.startswith("D:") and key[2:] in state["deadEnds"]:
                item = {"key": key, "type": "deadend", "deadEnd": key[2:]}
        if item is None:
            raise store.HandoffError(f"第 {round_no} 轮里没有 {key} 这一项", code="item-not-in-round",
                                     round=round_no, key=key)
        _check_answer_value(state, item, value)
        if item["type"] == "question":
            _guard_dead_option(state, item["node"], value)
        if item["type"] == "structure" and value["kind"] == "accept":
            proposal = item["proposal"]
            already = round_["answers"].get(key, {}).get("kind") == "accept"
            if not already and any(not _is_active(state, nid) for nid in proposal.get("targets", [])):
                raise store.HandoffError("提议涉及的问题已变化，请刷新后重试", code="structure-target-changed")
        if value.get("kind") == "verify":
            value = {**value, "allowExternal": value.get("allowExternal", False)}
        elif value.get("kind") == "text":
            value = {**value, "text": store.fix_text(value["text"])}
        if round_["answers"].get(key) == value:
            return displayed
        if item["type"] == "update":
            draft = state["updates"][item["update"]]
            if draft["status"] == "confirmed":
                raise store.HandoffError("这份新文本已经确认，请通过新的更新请求继续修改", code="update-not-pending")
            if value["kind"] == "accept":
                _confirm_update_locked(review_dir, state, draft, by)
        _append_event(review_dir, analysis_id, {"type": "answered", "at": store.now_iso(), "by": by,
                                                "round": round_no, "key": key, "value": value})
        after = load(review_dir, analysis_id)
        _sync_notes_locked(review_dir, after, by)
        return after


def _check_answer_value(state: dict[str, Any], item: dict[str, Any], value: Any) -> None:
    if not isinstance(value, dict):
        raise AnalysisInputError("回答须为 {kind: ...} 对象")
    kind = value.get("kind")
    if item["type"] in ("structure", "assumption", "update", "deadend"):
        if kind not in ("accept", "reject"):
            raise AnalysisInputError("结构提议的回答只能是 accept（同意）或 reject（不采纳）")
        if item["type"] == "deadend" and any(state["assumptions"][aid]["refuted"]
                                              for aid in state["deadEnds"][item["deadEnd"]]["assumptions"]):
            raise store.HandoffError("这项死局的依据已被推翻，请刷新后复核", code="dead-end-obsolete")
        return
    if kind == "text":
        if not isinstance(value.get("text"), str) or not value["text"].strip():
            raise AnalysisInputError("自由文本回答不能为空")
        return
    if kind == "park":
        return
    if kind == "verify":
        if "allowExternal" in value and not isinstance(value["allowExternal"], bool):
            raise AnalysisInputError("allowExternal 须为明确的布尔值")
        return
    options = state["nodes"][item["node"]]["options"]
    if kind == "accept":
        return
    if kind == "option":
        if not isinstance(value.get("option"), str) or value["option"] not in options:
            raise AnalysisInputError(f"option 须为该题的选项之一（{'、'.join(options)}）")
        return
    raise AnalysisInputError("回答方式须为 accept、option、text、park 或 verify")


def submit_round(review_dir: Path, analysis_id: str, round_no: int, *, by: str = "page",
                 authorize: Callable[[], None] | None = None) -> dict[str, Any]:
    """交出轮次：open → submitted（进入分析队列）。已交出的重复交出合并，不重复排队。"""
    review_dir = Path(review_dir)
    with store.locked(review_dir):
        if authorize is not None:
            authorize()
        state = load(review_dir, analysis_id)
        _guard_open(state)
        round_ = _round(state, round_no)
        if round_["status"] == "submitted":
            return state   # 已在排队：这次修改合并进去
        if round_["status"] != "open":
            raise store.HandoffError(f"第 {round_no} 轮不能再交出（{round_['status']}）",
                                     code="round-not-answerable", round=round_no, status=round_["status"])
        _append_event(review_dir, analysis_id, {"type": "round-submitted", "at": store.now_iso(),
                                                "by": by, "round": round_no,
                                                "order": _next_submit_order(review_dir)})
        return load(review_dir, analysis_id)


def queue(review_dir: Path) -> list[dict[str, Any]]:
    """分析队列里等着的轮次（跨分析，按交出先后）。"""
    out = []
    for aid in _analyses_ids(Path(review_dir)):
        for round_ in load(Path(review_dir), aid)["rounds"]:
            if round_["status"] == "submitted":
                out.append({"analysis": aid, "seq": round_["seq"], "kind": round_["kind"],
                            "at": round_["at"], "order": round_.get("order")})
    return sorted(out, key=lambda x: (x["order"] if isinstance(x.get("order"), int) else store._ts(x["at"]), x["seq"]))


def delivery_items(review_dir: Path) -> list[dict[str, Any]]:
    """全部轮次的 {seq, status}（投递判定用：已领取 / 已处理的轮次不再投）。"""
    out = []
    for aid in _analyses_ids(Path(review_dir)):
        for round_ in load(Path(review_dir), aid)["rounds"]:
            out.append({"seq": round_["seq"], "status": round_["status"]})
    return out


def has_claimed_round(review_dir: Path) -> bool:
    """有没有被 Agent 领取、还没写回的分析轮次。"""
    return any(any(r["status"] == "claimed" for r in load(Path(review_dir), aid)["rounds"])
               for aid in _analyses_ids(Path(review_dir)))


def find_by_round(review_dir: Path, round_no: int) -> tuple[str, dict[str, Any]]:
    """按轮次号找分析（轮次号在整份审阅内唯一）。"""
    for aid in _analyses_ids(Path(review_dir)):
        state = load(Path(review_dir), aid)
        if any(r["seq"] == round_no for r in state["rounds"]):
            return aid, state
    raise store.NotFound(f"round#{round_no}")


def agent_busy(review_dir: Path) -> bool:
    """Agent 手上是否已有处理中的项（分析轮次或改稿批次）。"""
    for aid in _analyses_ids(Path(review_dir)):
        if any(r["status"] == "claimed" for r in load(Path(review_dir), aid)["rounds"]):
            return True
    return any(item.get("status") in ("claimed", "reviewing") for item in store.handoff_load(review_dir)["items"])


def claim(review_dir: Path, *, by: str = "agent", seq: int | None = None,
          authorize: Callable[[], None] | None = None) -> dict[str, Any] | None:
    """领取最早一个等着轮次（可指定轮次号）。Agent 一次只处理一项：已有处理中的轮次或批次时拒绝。"""
    review_dir = Path(review_dir)
    with store.locked(review_dir):
        if authorize is not None:
            authorize()
        if agent_busy(review_dir):
            raise store.HandoffError("Agent 手上已有一项在处理，一次只处理一项",
                                     code="agent-busy")
        waiting = queue(review_dir)
        if seq is not None:
            waiting = [x for x in waiting if x["seq"] == seq]
        if not waiting:
            return None
        target = waiting[0]
        state = load(review_dir, target["analysis"])
        _append_event(review_dir, target["analysis"],
                      {"type": "round-claimed", "at": store.now_iso(), "by": by,
                       "round": target["seq"], "baseRev": state["rev"]})
        after = load(review_dir, target["analysis"])
        return {"analysis": target["analysis"], **_round(after, target["seq"])}


def _clean_proposals(proposals: Any, state: dict[str, Any]) -> list[dict[str, Any]]:
    if not isinstance(proposals, list):
        raise AnalysisInputError("proposals 须为结构提议列表")
    if len(proposals) > MAX_PROPOSALS:
        raise AnalysisInputError(f"一轮最多提出 {MAX_PROPOSALS} 个结构提议")
    out, seen, targeted = [], set(), set()
    for raw in proposals:
        if not isinstance(raw, dict):
            raise AnalysisInputError("结构提议须为对象")
        key = raw.get("key")
        if not isinstance(key, str) or not PROP_KEY_RE.fullmatch(key) or key in seen:
            raise AnalysisInputError(f"结构提议的 key 须为不重复的 {PROP_KEY_RE.pattern}")
        seen.add(key)
        op = raw.get("op", "add")
        if op not in ("add", "split", "merge", "void"):
            raise AnalysisInputError("结构提议 op 须为 add、split、merge 或 void")
        if op != "add":
            targets = raw.get("targets")
            if not isinstance(targets, list) or not targets or any(
                    not isinstance(nid, str) or not _is_active(state, nid) for nid in targets):
                raise AnalysisInputError("结构变更须引用当前选择枝上的 targets")
            if len(set(targets)) != len(targets) or targeted.intersection(targets):
                raise AnalysisInputError("一轮结构变更不能重复引用同一个问题")
            targeted.update(targets)
            replacements = raw.get("replacements", [])
            if not isinstance(replacements, list) or any(not isinstance(p, dict) or p.get("op", "add") != "add" for p in replacements):
                raise AnalysisInputError("replacements 须为新增问题提议列表")
            if ((op == "split" and (len(targets) != 1 or len(replacements) < 2)) or
                    (op == "merge" and (len(targets) < 2 or len(replacements) != 1)) or
                    (op == "void" and replacements)):
                raise AnalysisInputError("拆分须一题变多题，合并须多题变一题，作废不带替代问题")
            replacements = _clean_proposals(replacements, state)
            for replacement in replacements:
                parent = replacement.get("parent")
                while parent:
                    if parent["node"] in targets:
                        raise AnalysisInputError("替代问题不能挂在即将作废的问题枝下")
                    parent = state["nodes"][parent["node"]].get("parent")
            out.append({"key": key, "op": op, "targets": targets,
                        "text": store.fix_text(raw.get("text")), "replacements": replacements,
                        "targetNames": [state["nodes"][nid]["short"] for nid in targets]})
            continue
        text = store.fix_text(raw.get("text")).strip()
        if not text:
            raise AnalysisInputError("问题文本不能为空")
        options_raw = raw.get("options")
        if not isinstance(options_raw, list) or not options_raw:
            raise AnalysisInputError("结构提议须带非空 options 列表")
        options, oids = {}, set()
        for o in options_raw:
            if not isinstance(o, dict) or not isinstance(o.get("id"), str) or not OPT_ID_RE.fullmatch(o.get("id", "")) \
                    or not isinstance(o.get("label"), str) or not o.get("label").strip():
                raise AnalysisInputError("选项须为 {id, label}，id 只允许字母数字、下划线和连字符")
            if o["id"] in oids:
                raise AnalysisInputError(f"选项 id 重复：{o['id']}")
            oids.add(o["id"])
            options[o["id"]] = store.fix_text(o["label"]).strip()
        recommend = raw.get("recommend")
        if not isinstance(recommend, str) or recommend not in options:
            raise AnalysisInputError("recommend 须为 options 之一")
        parent = raw.get("parent")
        if parent is not None:
            if not isinstance(parent, dict) or not isinstance(parent.get("node"), str):
                raise AnalysisInputError("parent 须为 {node, option} 或 null")
            pnode = state["nodes"].get(parent["node"])
            if pnode is None:
                raise AnalysisInputError(f"挂在的问题 {parent['node']} 还不在问题树上（须先经确认）")
            if not isinstance(parent.get("option"), str) or parent["option"] not in pnode["options"]:
                raise AnalysisInputError(f"parent.option 须为 {parent['node']} 的选项之一")
        out.append({"key": key, "text": text,
                    "short": store.fix_text(raw.get("short")).strip()[:200] or text[:24],
                    "options": [{"id": k, "label": v} for k, v in options.items()],
                    "recommend": recommend, "parent": parent})
    return out


def _proposal_signature(proposal: dict[str, Any]) -> str:
    if proposal.get("op", "add") == "add":
        shape = {k: proposal.get(k) for k in ("text", "options", "recommend", "parent")}
    else:
        shape = {k: proposal.get(k) for k in ("op", "targets")}
        shape["replacements"] = [_proposal_signature(p) for p in proposal.get("replacements", [])]
    return json.dumps(shape, ensure_ascii=False, sort_keys=True)


def _clean_evidence(values: Any, state: dict[str, Any], *, assumptions: bool) -> list[dict[str, Any]]:
    if values is None:
        return []
    if not isinstance(values, list) or len(values) > MAX_PROPOSALS:
        raise AnalysisInputError("假设或事实须为列表，每轮最多50项")
    out, seen = [], set()
    registry = state["assumptions"] if assumptions else state["facts"]
    for value in values:
        if not isinstance(value, dict):
            raise AnalysisInputError("假设或事实须为对象")
        key, text = value.get("key"), value.get("text")
        if not isinstance(key, str) or not PROP_KEY_RE.fullmatch(key) or key in seen:
            raise AnalysisInputError("假设或事实的 key 须为不重复的稳定标识")
        if not isinstance(text, str) or not text.strip():
            raise AnalysisInputError("假设或事实的原文不能为空")
        seen.add(key)
        clean = {"key": key, "text": store.fix_text(text)}
        if assumptions:
            decisions = value.get("decisions")
            if not isinstance(decisions, list) or not decisions or any(
                    not isinstance(did, str) or did not in state["decisions"] for did in decisions):
                raise AnalysisInputError("候选假设须依附已经存在的 decisions")
            clean["decisions"] = list(dict.fromkeys(decisions))
        else:
            for field in ("source", "node"):
                if field in value:
                    if not isinstance(value[field], str):
                        raise AnalysisInputError("事实的来源或问题标识须为文本")
                    clean[field] = store.fix_text(value[field])
            if "node" in clean and clean["node"] not in state["nodes"]:
                raise AnalysisInputError("事实引用的问题不存在")
        old = next((v for v in registry.values() if v["key"] == key), None)
        if old and any(old.get(k) != v for k, v in clean.items() if not (assumptions and k == "decisions")):
            raise AnalysisInputError("同一稳定标识的假设或事实不能改写原文")
        if assumptions and old and old["refuted"] and any(did not in old["decisions"] for did in clean["decisions"]):
            raise AnalysisInputError("已推翻的假设不能新增决策依赖")
        out.append(clean)
    return out


def _clean_dead_ends(values: Any, state: dict[str, Any], round_no: int) -> list[dict[str, Any]]:
    if values is None:
        return []
    if not isinstance(values, list) or len(values) > MAX_PROPOSALS:
        raise store.HandoffError("deadEnds 须为死局提议列表，每轮最多50项", code="bad-dead-ends")
    out, seen = [], set()
    next_id = len(state["deadEnds"]) + 1
    for value in values:
        if not isinstance(value, dict):
            raise store.HandoffError("死局提议须为对象", code="bad-dead-ends")
        key, reason, assumptions = value.get("key"), value.get("reason"), value.get("assumptions")
        node = state["nodes"].get(value.get("node")) if isinstance(value.get("node"), str) else None
        if (not isinstance(key, str) or not PROP_KEY_RE.fullmatch(key) or key in seen
                or not isinstance(reason, str) or not reason.strip()
                or not isinstance(assumptions, list)
                or any(not isinstance(aid, str) or aid not in state["assumptions"]
                       or state["assumptions"][aid]["refuted"] for aid in assumptions)
                or value.get("kind") not in ("option", "line")):
            raise store.HandoffError("死局须提供稳定key、目标选项、理由与已存在的假设", code="bad-dead-ends")
        seen.add(key)
        clean = {"key": key, "kind": value["kind"],
                 "reason": store.fix_text(reason), "assumptions": list(dict.fromkeys(assumptions))}
        if value["kind"] == "option":
            if node is None or not isinstance(value.get("option"), str) or value["option"] not in node["options"]:
                raise store.HandoffError("请选择问题树中已有的选项", code="bad-dead-ends")
            clean.update(node=node["id"], option=value["option"])
        else:
            if not isinstance(value.get("targetLine"), str) or value["targetLine"] not in state["lines"]:
                raise store.HandoffError("请选择已有的分析线", code="bad-dead-ends")
            clean["targetLine"] = value["targetLine"]
        old = next((end for end in state["deadEnds"].values() if end["key"] == key), None)
        if old is not None:
            if any(old.get(k) != v for k, v in clean.items()):
                raise store.HandoffError("同一死局标识不能改写目标或理由", code="bad-dead-ends")
            out.append(old)
        else:
            out.append({"id": f"e{next_id}",
                        **clean, "line": state["line"], "round": round_no, "status": "pending"})
            next_id += 1
    return out


def _dead_blocks(state: dict[str, Any], end: dict[str, Any]) -> bool:
    return end["status"] == "confirmed" and not any(state["assumptions"][aid]["refuted"] for aid in end["assumptions"])


def _guard_dead_option(state: dict[str, Any], node_id: str, value: dict[str, Any]) -> None:
    if value.get("kind") not in ("accept", "option", "text"):
        return
    node = state["nodes"][node_id]
    option = node["recommend"] if value.get("kind") == "accept" else value.get("option")
    for end in state["deadEnds"].values():
        same_text = value.get("kind") == "text" and value.get("text", "").strip() == node["options"].get(end.get("option"))
        if (end["kind"] == "option" and end["node"] == node_id and _dead_blocks(state, end)
                and (end["option"] == option or same_text)):
            raise store.HandoffError("这条路已经确认为死局；请先复核其依据", code="dead-end-option",
                                     reason=end["reason"], assumptions=[state["assumptions"][aid]["text"] for aid in end["assumptions"]])


def _guard_dead_line(state: dict[str, Any], line: str) -> None:
    for end in state["deadEnds"].values():
        if end["kind"] == "line" and end["targetLine"] == line and _dead_blocks(state, end):
            raise store.HandoffError("这条分析线已经确认为死局；请先复核其依据", code="dead-end-line",
                                     reason=end["reason"], assumptions=[state["assumptions"][aid]["text"] for aid in end["assumptions"]])


def writeback(review_dir: Path, analysis_id: str, round_no: int, base_rev: int, proposals: Any, *,
              note: str = "", by: str = "agent", assumptions: Any = None,
              facts: Any = None, updates: Any = None, dead_ends: Any = None,
              authorize: Callable[[], None] | None = None) -> dict[str, Any]:
    """Agent 写回轮次：校验领取时记录的基线版本，记下这一轮的说明，
    把没答的提议带进下一轮并附上新的结构提议；没有可作答的内容就不再开新轮。"""
    review_dir = Path(review_dir)
    with store.locked(review_dir):
        if authorize is not None:
            authorize()
        state = load(review_dir, analysis_id)
        round_ = _round(state, round_no)
        if round_["status"] != "claimed":
            raise store.HandoffError(f"第 {round_no} 轮没有被领取，不能写回（{round_['status']}）",
                                     code="round-not-claimed", round=round_no, status=round_["status"])
        state = _line_view(state, round_.get("line", LINE_MAIN))
        if base_rev != state["rev"]:
            _record(review_dir, analysis_id, "writeback-rejected", by, round=round_no,
                    expected=base_rev, actual=state["rev"], line=round_.get("line", LINE_MAIN))
            _record(review_dir, analysis_id, "round-voided", by, round=round_no,
                    reason="base-rev-mismatch", carry=_answer_carry(state, round_),
                    proposals=_unanswered_proposals(round_),
                    context=_historical_context(review_dir, state, round_, "void", "base-rev-mismatch"))
            refreshed = _enqueue_refresh(review_dir, load(review_dir, analysis_id),
                                          round_.get("line", LINE_MAIN), "refresh", by)
            raise store.HandoffError(f"第 {round_no} 轮接手后分析已被改过（基线 {base_rev} → {state['rev']}），"
                                     "这次结果作废，已请求重新出题", code="base-rev-mismatch",
                                     round=round_no, expected=base_rev, actual=state["rev"], nextRound=refreshed)
        cleaned = _clean_proposals(proposals, state)
        cleaned = [p for p in cleaned if not any(p["key"] == old["key"] or
                   _proposal_signature(p) == _proposal_signature(old)
                   for old in state["rejectedProposals"])]
        candidates = _clean_evidence(assumptions, state, assumptions=True)
        findings = _clean_evidence(facts, state, assumptions=False)
        drafted_updates = _clean_updates(review_dir, state, round_, updates)
        ends = _clean_dead_ends(dead_ends, state, round_no)
        history = _historical_context(review_dir, state, round_, "done")
        history["note"] = store.fix_text(note)
        history["writeback"] = {"proposals": cleaned, "assumptions": candidates,
                                "facts": findings, "updates": drafted_updates, "deadEnds": ends}
        new_rev = state["rev"] + 1
        _append_event(review_dir, analysis_id, {"type": "round-done", "at": store.now_iso(), "by": by,
                                                "round": round_no, "note": store.fix_text(note),
                                                "rev": new_rev, "assumptions": candidates, "facts": findings,
                                                "context": history, "deadEnds": ends})
        for draft in drafted_updates:
            _record(review_dir, analysis_id, "note-update-drafted", by, draft=draft, round=round_no)
        next_round = None
        line = round_.get("line", LINE_MAIN)
        pending = {**state.get("pendingProposals", {}).get(line, {}),
                   **{p["key"]: p for p in _unanswered_proposals(round_)}}
        carried = [p for key, p in pending.items() if "S:" + key not in round_["answers"]]
        fresh = [p for p in cleaned if p["key"] not in {c["key"] for c in carried}]
        current = _line_view(load(review_dir, analysis_id), line)
        frontier = _frontier_question_items(current)
        pending_assumptions = any(not a["refuted"] and a["confirmed"].get(line, "pending") == "pending"
                                  for a in current["assumptions"].values())
        combined = carried + fresh
        prefilled = _prefill(current, line, combined, round_)
        if (combined or frontier or pending_assumptions or prefilled["answers"]
                or any(_update_valid(current, draft) for draft in current["updates"].values())
                or any(end["status"] == "pending" and end["line"] == line for end in current["deadEnds"].values())):
            next_round = _next_round(review_dir)
            _append_event(review_dir, analysis_id, {"type": "round-requested", "at": store.now_iso(),
                                                    "by": by, "round": next_round, "kind": "next",
                                                    "line": line, "items": combined, **prefilled})
        _record(review_dir, analysis_id, "carry-consumed", by, line=line)
        _checkpoint_locked(review_dir, analysis_id, by, auto=True, round_no=round_no, line=line)
        after = load(review_dir, analysis_id)
        return {"state": after, "nextRound": next_round}


def round_context(review_dir: Path, analysis_id: str, round_no: int) -> dict[str, Any]:
    """一轮的完整上下文：焦点问题、当前路径上的决策、本轮条目（含问题文本与选项、推荐答案）、
    审阅人原话回答、源批注原文副本。"""
    state = load(Path(review_dir), analysis_id)
    round_ = _round(state, round_no)
    if round_.get("context") is not None and round_["status"] in ("done", "void"):
        return copy.deepcopy(round_["context"])
    state = _line_view(state, round_.get("line", LINE_MAIN))
    return _round_context_data(Path(review_dir), state, round_)


def _round_context_data(review_dir: Path, state: dict[str, Any], round_: dict[str, Any]) -> dict[str, Any]:
    analysis_id, round_no = state["id"], round_["seq"]
    decisions = [{"id": d["id"], "node": d["node"], "nodeShort": state["nodes"][d["node"]]["short"],
                  "question": state["nodes"][d["node"]]["text"], "option": d["option"], "label": d["label"],
                  "round": d["round"], "line": d["line"], "assumptions": d.get("assumptions", [])}
                 for nid, node in sorted(state["nodes"].items(), key=lambda kv: int(kv[0][1:]))
                 if node.get("decision") and _is_active(state, nid)
                 for d in [state["decisions"][node["decision"]]]]
    items = []
    current_items = list(round_["items"])
    missing_answers = []
    seen = {item["key"] for item in current_items}
    for key in round_["answers"]:
        if key in seen:
            continue
        if key.startswith("S:"):
            proposal = next((p for p in round_["proposals"] if "S:" + p["key"] == key), None)
            if proposal:
                missing_answers.append({"key": key, "type": "structure", "proposal": proposal})
        elif key.startswith("Q:") and key[2:] in state["nodes"]:
            missing_answers.append({"key": key, "type": "question", "node": key[2:]})
        elif key.startswith("A:") and key[2:] in state["assumptions"]:
            missing_answers.append({"key": key, "type": "assumption", "assumption": key[2:]})
        elif key.startswith("U:") and key[2:] in state["updates"]:
            missing_answers.append({"key": key, "type": "update", "update": key[2:]})
        elif key.startswith("D:") and key[2:] in state["deadEnds"]:
            missing_answers.append({"key": key, "type": "deadend", "deadEnd": key[2:]})
    for item in current_items + missing_answers:
        if item["type"] == "deadend":
            end = state["deadEnds"][item["deadEnd"]]
            node = state["nodes"].get(end.get("node")) or {}
            items.append({**end, "key": item["key"], "type": "deadend", "deadEnd": end["id"],
                          "targetText": node.get("short", ""),
                          "targetOrdinal": (state["lines"].get(end.get("targetLine")) or {}).get("ordinal"),
                          "optionLabel": node.get("options", {}).get(end.get("option"), ""),
                          "assumptionTexts": [state["assumptions"][aid]["text"] for aid in end["assumptions"]]})
        elif item["type"] == "update":
            draft = state["updates"][item["update"]]
            items.append({"key": item["key"], "type": "update", "update": draft["id"],
                          "noteId": draft["noteId"], "text": draft["text"], "beforeText": draft["beforeText"],
                          "basis": draft["basis"]})
        elif item["type"] == "assumption":
            assumption = state["assumptions"][item["assumption"]]
            items.append({"key": item["key"], "type": "assumption", "assumption": assumption["id"],
                          "text": assumption["text"], "decisions": assumption["decisions"],
                          "status": assumption["confirmed"].get(round_.get("line", LINE_MAIN), "pending")})
        elif item["type"] == "structure":
            p = item["proposal"]
            items.append({**p, "key": item["key"], "type": "structure", "short": p.get("short", ""),
                          "options": [{"id": o["id"], "label": o["label"]} for o in p.get("options", [])],
                          "recommend": p.get("recommend"), "parent": p.get("parent")})
        else:
            node = state["nodes"][item["node"]]
            items.append({"key": item["key"], "type": "question", "node": node["id"], "text": node["text"],
                          "short": node["short"],
                          "options": [{"id": k, "label": v} for k, v in node["options"].items()],
                          "recommend": node["recommend"]})
    assumptions = [{**a, "status": a["confirmed"].get(round_.get("line", LINE_MAIN), "pending")}
                   for a in state["assumptions"].values()]
    verification = [{"node": key[2:], "question": state["nodes"][key[2:]]["text"],
                     "allowExternal": value.get("allowExternal") is True}
                    for key, value in round_["answers"].items()
                    if key.startswith("Q:") and value.get("kind") == "verify"]
    return {"analysis": analysis_id, "focus": state["focus"], "line": round_.get("line", LINE_MAIN),
            "round": round_no, "kind": round_["kind"], "status": round_["status"],
            "note": round_.get("note"), "historical": False,
            "baseRev": round_.get("baseRev"), "decisions": decisions, "items": items[:len(current_items)],
            "answeredItems": [i for i in items if i["key"] in round_["answers"]],
            "answers": round_["answers"], "sourceCopy": state["sourceCopy"],
            "updateRequests": _update_context(Path(review_dir), state, round_.get("line", LINE_MAIN)),
            "protection": state["protection"],
            "assumptions": assumptions, "facts": list(state["facts"].values()),
            "deadEnds": list(state["deadEnds"].values()),
            "verificationRequests": verification,
            "parked": [n for n in state["nodes"].values() if n["status"] == "parked" and _is_active(state, n["id"])]}


def _historical_context(review_dir: Path, state: dict[str, Any], round_: dict[str, Any],
                        status: str, reason: str | None = None) -> dict[str, Any]:
    context = _round_context_data(review_dir, _line_view(state, round_["line"]), round_)
    context.update(status=status, historical=True, protection={"locked": False, "round": None})
    if reason:
        context["voidReason"] = reason
    return copy.deepcopy(context)


def convert(review_dir: Path, analysis_id: str, node_id: str, *, text: str | None = None,
            by: str = "page") -> dict[str, Any]:
    """结论转批注草稿：先生成草稿（存在分析里），确认后才进入批注列表。"""
    review_dir = Path(review_dir)
    with store.locked(review_dir):
        state = load(review_dir, analysis_id)
        guard_unlocked(state)
        node = state["nodes"].get(node_id)
        if node is None or not decision_in_force(state, node.get("decision")):
            raise store.HandoffError(f"{node_id} 还没有生效的结论，不能转成批注",
                                     code="node-not-decided", node=node_id)
        decision = state["decisions"][node["decision"]]
        draft_id = f"c{len(state['drafts']) + 1}"
        draft = {"id": draft_id, "status": "pending",
                 **_source_location(review_dir, state["sourceCopy"]),
                 "text": store.fix_text(text).strip()
                 or f"按分析结论：{node['short']}用「{decision['label']}」",
                 "basis": {"analysis": analysis_id, "decision": decision["id"], "node": node_id,
                           "option": decision["option"], "label": decision["label"], "line": state["line"]}}
        _append_event(review_dir, analysis_id, {"type": "converted", "at": store.now_iso(), "by": by,
                                                "draft": draft})
        return draft


def confirm_convert(review_dir: Path, analysis_id: str, draft_id: str, *, by: str = "page") -> dict[str, Any]:
    """确认草稿：批注进入批注列表（未处理，记录来源分析与依据决策），
    源批注标为已处理并指向分析（同一次写入里完成）。"""
    review_dir = Path(review_dir)
    with store.locked(review_dir):
        state = load(review_dir, analysis_id)
        guard_unlocked(state)
        draft = next((d for d in state["drafts"] if d["id"] == draft_id), None)
        if draft is None or draft["status"] != "pending":
            raise store.HandoffError(f"{draft_id} 不是等你确认的批注草稿",
                                     code="draft-not-pending", draft=draft_id)
        if not decision_in_force(state, draft["basis"]["decision"]):
            raise store.HandoffError("草稿依据的结论已经变化，请按当前结论重新转出", code="draft-basis-changed")
        doc = store.load(review_dir)
        nid = store.new_id()
        basis = {"analysis": draft["basis"]["analysis"], "decision": draft["basis"]["decision"],
                 "node": draft["basis"]["node"], "line": draft["basis"]["line"]}
        location = _source_location(review_dir, {**state["sourceCopy"],
                                    **{key: draft[key] for key in ("page", "title", "key") if key in draft}})
        note = {"id": nid, **location,
                "target": state["sourceCopy"].get("target", ""), "text": draft["text"],
                "quote": "", "quoteFrom": "", "pin": None,
                "status": "open", "at": store.now_iso(),
                "basis": basis, "fromAnalysis": analysis_id, "tracking": True}
        doc["notes"].append(note)
        source = next((n for n in doc["notes"] if n.get("id") == state["sourceNote"]), None)
        resolved_source = False
        if source is not None and source.get("status") == "held" and source.get("heldBy") == analysis_id:
            store._mark_resolved(source, by=by, resolution="分析结论转出批注")
            source["analysisId"] = analysis_id
            source.pop("hold", None)
            source.pop("heldBy", None)
            trail = source.get("trail") if isinstance(source.get("trail"), list) else []
            trail.append({"at": store.now_iso(), "by": by, "action": "resolved",
                          "note": "分析结论转出批注"})
            source["trail"] = trail[-store.TRAIL_KEEP:]
            resolved_source = True
        store._commit(review_dir, doc, {"op": "add", "by": by, "id": nid, "note": note,
                                        "analysis": analysis_id, "resolvedSource": resolved_source})
        _append_event(review_dir, analysis_id, {"type": "convert-confirmed", "at": store.now_iso(),
                                                "by": by, "draft": draft_id, "note": nid})
        return note


def release_source(review_dir: Path, analysis_id: str, *, by: str = "page") -> dict[str, Any]:
    """解除源批注的搁住：回到未处理，可以再被交给 Agent 改稿或再发起分析。"""
    review_dir = Path(review_dir)
    with store.locked(review_dir):
        state = load(review_dir, analysis_id)
        guard_unlocked(state)
        if state["sourceState"] != "held":
            raise store.HandoffError("源批注的搁住已经解除或已随结论转出处理",
                                     code="source-not-held", analysis=analysis_id)
        doc = store.load(review_dir)
        note = store._find(doc, state["sourceNote"])
        if note.get("status") != "held" or note.get("heldBy") != analysis_id:
            raise store.HandoffError("源批注已不在「搁住 · 分析中」状态，不能在这里解除",
                                     code="source-not-held", analysis=analysis_id)
        note["status"] = "open"
        note.pop("hold", None)
        note.pop("heldBy", None)
        note["updatedAt"] = store.now_iso()
        store._commit(review_dir, doc, {"op": "released", "by": by, "id": note["id"],
                                        "analysis": analysis_id})
        _append_event(review_dir, analysis_id, {"type": "source-released", "at": store.now_iso(), "by": by})
        return note


# ---------- 分析级保护与重裁（与后续分析线扩展共用） ----------
def guard_unlocked(state: dict[str, Any]) -> None:
    """只锁住这一分析，不影响审阅里其他分析的焦点与数据。调用方须持有审阅目录锁。"""
    _guard_open(state)
    if any(round_["status"] == "claimed" for round_ in state["rounds"]):
        raise store.HandoffError("Agent 正在处理这一分析；写回前只能查看和作答", code="analysis-locked")


def _record(review_dir: Path, analysis_id: str, event_type: str, by: str, **fields: Any) -> None:
    _append_event(review_dir, analysis_id, {"type": event_type, "at": store.now_iso(), "by": by, **fields})


def _snapshot(state: dict[str, Any]) -> dict[str, Any]:
    """封存领域投影；不递归嵌入轮次、旧检查点或分析线。"""
    pending = dict(state.get("pendingProposals", {}).get(state["line"], {}))
    for round_ in state["rounds"]:
        if round_.get("line", LINE_MAIN) == state["line"] and round_["status"] in ROUND_ANSWERABLE:
            pending.update({p["key"]: p for p in _unanswered_proposals(round_)})
    return copy.deepcopy({
        "focus": state["focus"], "nodes": state["nodes"], "decisions": state["decisions"],
        "assumptions": state["assumptions"], "facts": state["facts"],
        "pendingProposals": list(pending.values()),
        "conclusions": [node["decision"] for node in state["nodes"].values()
                        if decision_in_force(state, node.get("decision"))],
        "unresolved": [node["id"] for node in state["nodes"].values()
                       if node["status"] in ("pending", "verifying") and _is_active(state, node["id"])],
        "parked": [node["id"] for node in state["nodes"].values()
                   if node["status"] == "parked" and _is_active(state, node["id"])],
    })


def _checkpoint_locked(review_dir: Path, analysis_id: str, by: str, *, auto: bool,
                       round_no: int | None = None, sealed: bool = False,
                       line: str | None = None) -> dict[str, Any]:
    state = load(review_dir, analysis_id)
    state = _line_view(state, line or state["line"])
    checkpoint = {"id": f"cp{len(state['checkpoints']) + 1}", "line": state["line"],
                  "round": round_no, "auto": auto, "sealed": sealed,
                  "at": store.now_iso(), "by": by, "snapshot": _snapshot(state)}
    _record(review_dir, analysis_id, "checkpoint-recorded", by, checkpoint=checkpoint,
            rev=state["rev"] if auto else state["rev"] + 1)
    return checkpoint


def _guard_open(state: dict[str, Any]) -> None:
    if state["status"] == "closed":
        raise store.HandoffError("这项分析已结案，请先重新打开", code="analysis-closed")


def seal_checkpoint(review_dir: Path, analysis_id: str, checkpoint_id: str | None = None, *,
                    by: str = "page", authorize: Callable[[], None] | None = None) -> dict[str, Any]:
    """封存当前状态，或把已有自动检查点标为封存；快照内容保持不变。"""
    review_dir = Path(review_dir)
    with store.locked(review_dir):
        if authorize is not None:
            authorize()
        state = load(review_dir, analysis_id)
        _guard_open(state)
        guard_unlocked(state)
        if checkpoint_id is None:
            last = next((r["seq"] for r in reversed(state["rounds"])
                         if r["line"] == state["line"] and r["status"] == "done"), None)
            _checkpoint_locked(review_dir, analysis_id, by, auto=False, round_no=last, sealed=True)
        else:
            checkpoint = next((c for c in state["checkpoints"] if c["id"] == checkpoint_id), None)
            if checkpoint is None:
                raise store.HandoffError("没有找到这个检查点，请刷新后查看", code="checkpoint-not-found")
            if not checkpoint["sealed"]:
                _record(review_dir, analysis_id, "checkpoint-sealed", by,
                        checkpoint=checkpoint_id, rev=state["rev"] + 1)
        return load(review_dir, analysis_id)


def fork_line(review_dir: Path, analysis_id: str, checkpoint_id: str, *, by: str = "page",
              authorize: Callable[[], None] | None = None) -> dict[str, Any]:
    """从检查点分线并切换；已有实体保留身份，只复制该线的节点投影。"""
    review_dir = Path(review_dir)
    with store.locked(review_dir):
        if authorize is not None:
            authorize()
        state = load(review_dir, analysis_id)
        _guard_open(state)
        guard_unlocked(state)
        checkpoint = next((c for c in state["checkpoints"] if c["id"] == checkpoint_id), None)
        if checkpoint is None:
            raise store.HandoffError("没有找到这个检查点，请刷新后查看", code="checkpoint-not-found")
        _guard_dead_line(state, checkpoint["line"])
        line = f"line{len(state['lines']) + 1}"
        _void_open_rounds(review_dir, state, state["line"], "line-forked", by)
        _record(review_dir, analysis_id, "line-forked", by, checkpoint=checkpoint_id,
                line=line, rev=state["rev"] + 1)
        after = load(review_dir, analysis_id)
        _sync_notes_locked(review_dir, after, by)
        _enqueue_refresh(review_dir, after, line, "refresh", by)
        return load(review_dir, analysis_id)


def switch_line(review_dir: Path, analysis_id: str, line: str, *, by: str = "page",
                authorize: Callable[[], None] | None = None) -> dict[str, Any]:
    """回到目标线的最新投影，保留其他线已经交出的轮次。"""
    review_dir = Path(review_dir)
    with store.locked(review_dir):
        if authorize is not None:
            authorize()
        state = load(review_dir, analysis_id)
        _guard_open(state)
        guard_unlocked(state)
        if not isinstance(line, str) or line not in state["lines"]:
            raise store.HandoffError("没有找到这条分析线，请刷新后查看", code="line-not-found")
        _guard_dead_line(state, line)
        if line != state["line"]:
            _record(review_dir, analysis_id, "line-switched", by, line=line, rev=state["rev"] + 1)
            _sync_notes_locked(review_dir, load(review_dir, analysis_id), by)
        return load(review_dir, analysis_id)


def refute_assumption(review_dir: Path, analysis_id: str, assumption_id: str, *, by: str = "page",
                      authorize: Callable[[], None] | None = None) -> dict[str, Any]:
    """推翻前记录快照；当前选择有直接依赖时分线重问，旧推理保持原样。"""
    review_dir = Path(review_dir)
    with store.locked(review_dir):
        if authorize is not None:
            authorize()
        state = load(review_dir, analysis_id)
        guard_unlocked(state)
        assumption = state["assumptions"].get(assumption_id) if isinstance(assumption_id, str) else None
        if assumption is None:
            raise store.HandoffError("没有找到这个假设，请刷新后查看", code="assumption-not-found")
        if assumption["refuted"]:
            return state
        affected = []
        for node in state["nodes"].values():
            decision = state["decisions"].get(node.get("decision"))
            if decision and _is_active(state, node["id"]) and assumption_id in decision.get("assumptions", []):
                affected.append({"node": node["id"], "decision": decision["id"], "answer": decision["label"]})
        checkpoint = None
        if affected:
            checkpoint = _checkpoint_locked(review_dir, analysis_id, by, auto=True)
        pending = any(r["line"] == state["line"] and r["status"] in ROUND_OPEN for r in state["rounds"])
        _void_open_rounds(review_dir, state, state["line"], "assumption-refuted", by)
        _record(review_dir, analysis_id, "assumption-refuted", by, assumption=assumption_id,
                line=state["line"], checkpoint=checkpoint["id"] if checkpoint else None, rev=state["rev"] + 1)
        if affected:
            line = f"line{len(state['lines']) + 1}"
            current = load(review_dir, analysis_id)
            _record(review_dir, analysis_id, "line-forked", by, checkpoint=checkpoint["id"],
                    line=line, carry=current["carry"].get(state["line"], {}), rev=current["rev"] + 1)
            _record(review_dir, analysis_id, "decisions-reopened", by, line=line,
                    assumption=assumption_id, nodes=affected)
        after = load(review_dir, analysis_id)
        _sync_notes_locked(review_dir, after, by)
        if affected or pending:
            _enqueue_refresh(review_dir, after, after["line"], "refute", by)
        return load(review_dir, analysis_id)


def close_analysis(review_dir: Path, analysis_id: str, *, by: str = "page",
                   authorize: Callable[[], None] | None = None) -> dict[str, Any]:
    """没有排队或处理中轮次时结案，作废未交出轮次并封存最后状态。"""
    review_dir = Path(review_dir)
    with store.locked(review_dir):
        if authorize is not None:
            authorize()
        state = load(review_dir, analysis_id)
        if state["status"] == "closed":
            return state
        guard_unlocked(state)
        if any(r["status"] == "submitted" for r in state["rounds"]):
            raise store.HandoffError("还有已交出的分析轮次，请等处理完再结案", code="analysis-pending-rounds")
        for line in state["lines"]:
            _void_open_rounds(review_dir, _line_view(state, line), line, "analysis-closed", by)
        _checkpoint_locked(review_dir, analysis_id, by, auto=False, sealed=True)
        _record(review_dir, analysis_id, "analysis-closed", by, rev=state["rev"] + 1)
        return load(review_dir, analysis_id)


def reopen_analysis(review_dir: Path, analysis_id: str, *, by: str = "page",
                    authorize: Callable[[], None] | None = None) -> dict[str, Any]:
    """重新打开已结案分析并自动排队出题；已有请求时不重复。"""
    review_dir = Path(review_dir)
    with store.locked(review_dir):
        if authorize is not None:
            authorize()
        state = load(review_dir, analysis_id)
        if state["status"] != "closed":
            return state
        _record(review_dir, analysis_id, "analysis-reopened", by, rev=state["rev"] + 1)
        _activate_locked(review_dir, analysis_id, by=by)
        _enqueue_refresh(review_dir, load(review_dir, analysis_id), state["line"], "reopen", by)
        return load(review_dir, analysis_id)


def _answer_carry(state: dict[str, Any], round_: dict[str, Any]) -> dict[str, Any]:
    out = {}
    for key, value in round_["answers"].items():
        item = next((item for item in round_["items"] if item["key"] == key), None)
        if key.startswith("S:"):
            proposed = next((p for p in round_["proposals"] if "S:" + p["key"] == key), None)
            if proposed is not None:
                item = {"key": key, "type": "structure", "proposal": proposed,
                        "confirmedNode": round_.get("proposalNodes", {}).get(key),
                        "structureApplied": key in round_.get("structureNodes", {}),
                        "structureNodes": round_.get("structureNodes", {}).get(key, [])}
        elif key.startswith("Q:") and key[2:] in state["nodes"]:
            node = state["nodes"][key[2:]]
            item = {"key": key, "type": "question", "node": node["id"],
                    "text": node["text"], "options": node["options"]}
        elif key.startswith("D:") and key[2:] in state["deadEnds"]:
            item = {"key": key, "type": "deadend", "deadEnd": key[2:]}
        elif key.startswith("U:") and key[2:] in state["updates"]:
            item = {"key": key, "type": "update", "update": key[2:]}
        elif key.startswith("A:") and key[2:] in state["assumptions"]:
            item = {"key": key, "type": "assumption", "assumption": key[2:]}
        if item is not None:
            out[key] = {"value": value, "item": item}
    return out


def _void_open_rounds(review_dir: Path, state: dict[str, Any], line: str, reason: str, by: str) -> None:
    state = _line_view(state, line)
    for round_ in state["rounds"]:
        if round_.get("line", LINE_MAIN) == line and round_["status"] in ROUND_OPEN:
            _record(review_dir, state["id"], "round-voided", by, round=round_["seq"], reason=reason,
                    carry=_answer_carry(state, round_), proposals=_unanswered_proposals(round_),
                    context=_historical_context(review_dir, state, round_, "void", reason))


def _enqueue_refresh(review_dir: Path, state: dict[str, Any], line: str, kind: str, by: str) -> int:
    state = _line_view(state, line)
    waiting = next((r for r in state["rounds"] if r.get("line", LINE_MAIN) == line
                    and r["status"] == "submitted"), None)
    if waiting is not None:
        return waiting["seq"]
    number = _next_round(review_dir)
    proposals = list(state.get("pendingProposals", {}).get(line, {}).values())
    _record(review_dir, state["id"], "round-requested", by, round=number, line=line, kind=kind,
            items=proposals, queued=True, order=_next_submit_order(review_dir), **_prefill(state, line, proposals))
    return number


def _unanswered_proposals(round_: dict[str, Any]) -> list[dict[str, Any]]:
    return [p for p in round_["proposals"] if "S:" + p["key"] not in round_["answers"]
            and "S:" + p["key"] not in round_["rejected"]]


def _carry_question_valid(state: dict[str, Any], item: dict[str, Any], value: Any) -> bool:
    if item.get("type") != "question" or not isinstance(value, dict):
        return False
    node = state["nodes"].get(item.get("node"))
    if (node is None or not _is_active(state, node["id"]) or node["text"] != item.get("text")
            or node.get("status") == "parked" or node.get("reopened")):
        return False
    try:
        _check_answer_value(state, item, value)
        _guard_dead_option(state, node["id"], value)
    except (store.NoteError, KeyError, TypeError):
        return False
    decision = state["decisions"].get(node.get("decision"))
    if decision:
        if value.get("kind") == "accept":
            return decision["option"] == node["recommend"]
        if value.get("kind") == "option":
            return decision["option"] == value.get("option")
        if value.get("kind") == "text":
            return decision["label"] == value.get("text")
    return value.get("kind") != "verify" or node.get("status") == "verifying"


def _carry_item_valid(state: dict[str, Any], item: dict[str, Any], value: Any) -> bool:
    if item.get("type") == "question":
        return _carry_question_valid(state, item, value)
    if not isinstance(value, dict) or value.get("kind") not in ("accept", "reject"):
        return False
    expected = "confirmed" if value["kind"] == "accept" else "rejected"
    if item["type"] == "assumption":
        assumption = state["assumptions"].get(item.get("assumption"))
        return bool(assumption and not assumption["refuted"]
                    and assumption["confirmed"].get(state["line"]) == expected)
    if item["type"] == "deadend":
        end = state["deadEnds"].get(item.get("deadEnd"))
        return bool(end and end["status"] == expected
                    and not any(state["assumptions"][aid]["refuted"] for aid in end["assumptions"]))
    if item["type"] == "update":
        draft = state["updates"].get(item.get("update"))
        return bool(draft and draft["status"] == expected and decision_in_force(state, draft["basis"]["decision"]))
    return False


def _prefill(state: dict[str, Any], line: str, proposals: list[dict[str, Any]],
             round_: dict[str, Any] | None = None) -> dict[str, Any]:
    state = _line_view(state, line)
    carried = dict(state.get("carry", {}).get(line, {}))
    if carried and round_ is not None:
        carried.update(_answer_carry(state, round_))   # 领取期间的最后原答优先。
    out: dict[str, Any] = {"answers": {}, "carriedItems": [], "proposalNodes": {}, "structureNodes": {}}
    for key, entry in carried.items():
        item, value = entry["item"], entry["value"]
        if _carry_item_valid(state, item, value):
            out["answers"][key] = value
            out["carriedItems"].append(item)
        elif item.get("type") == "structure":
            if not _carry_structure_valid(state, item, value):
                continue
            proposed = next((p for p in proposals if "S:" + p["key"] == key), None)
            if proposed is None:
                proposed = item["proposal"]
                proposals.append(proposed)
            if proposed == item.get("proposal"):
                out["answers"][key] = value
                if item.get("confirmedNode"):
                    out["proposalNodes"][key] = item["confirmedNode"]
                if item.get("structureApplied"):
                    out["structureNodes"][key] = item["structureNodes"]
    return out


def _carry_structure_valid(state: dict[str, Any], item: dict[str, Any], value: Any) -> bool:
    """已确认结构是恢复元数据，不是再次执行的提议；作废目标不能用 isActive 过滤。"""
    if not isinstance(value, dict) or value.get("kind") != "accept":
        return False
    proposed = item["proposal"]
    if proposed.get("op", "add") == "add":
        node = state["nodes"].get(item.get("confirmedNode"))
        return node is not None and node.get("status") != "void"
    return (item.get("structureApplied") is True
            and all(state["nodes"].get(nid, {}).get("status") == "void" for nid in proposed.get("targets", []))
            and all(nid in state["nodes"] and state["nodes"][nid].get("status") != "void"
                    for nid in item.get("structureNodes", [])))


def redecide(review_dir: Path, analysis_id: str, node_id: str, option: str, *, by: str = "page",
             authorize: Callable[[], None] | None = None) -> dict[str, Any]:
    """改选当前选择枝上的已决问题；旧决策与原选项下的子树保留，重新请求出题。"""
    review_dir = Path(review_dir)
    with store.locked(review_dir):
        if authorize is not None:
            authorize()
        state = load(review_dir, analysis_id)
        guard_unlocked(state)
        node = state["nodes"].get(node_id)
        if node is None or not node.get("decision") or not _is_active(state, node_id):
            raise store.HandoffError("只能改选当前选择枝上已经作答的问题", code="node-not-decided")
        if not isinstance(option, str) or option not in node["options"]:
            raise store.HandoffError("请选择这个问题已有的选项", code="bad-option")
        previous = state["decisions"][node["decision"]]
        if previous["option"] == option and not any(state["assumptions"][aid]["refuted"]
                                                     for aid in previous.get("assumptions", [])):
            return state
        _guard_dead_option(state, node_id, {"kind": "option", "option": option})
        _record(review_dir, analysis_id, "redecided", by, node=node_id, option=option,
                line=state["line"], rev=state["rev"] + 1)
        _void_open_rounds(review_dir, state, state["line"], "redecided", by)
        after = load(review_dir, analysis_id)
        _sync_notes_locked(review_dir, after, by)
        _enqueue_refresh(review_dir, after, state["line"], "refresh", by)
        return load(review_dir, analysis_id)


def node_branch(state: dict[str, Any], node_id: str) -> str:
    """当前选择与断枝分开，作废提议及其后代不冒充断枝。"""
    current, seen = node_id, set()
    while current:
        if current in seen or current not in state["nodes"]:
            return "void"
        seen.add(current)
        node = state["nodes"][current]
        if node.get("status") == "void":
            return "void"
        current = (node.get("parent") or {}).get("node")
    return "current" if _is_active(state, node_id) else "detached"


def decision_in_force(state: dict[str, Any], decision_id: str | None) -> bool:
    decision = state["decisions"].get(decision_id)
    if decision is None:
        return False
    node = state["nodes"].get(decision["node"])
    if node is None or node.get("decision") != decision_id or not _is_active(state, decision["node"]):
        return False
    return not _decision_needs_review(state, decision_id)


def _decision_needs_review(state: dict[str, Any], decision_id: str | None) -> bool:
    """当前投影上的决策是否因自身或上游依赖的假设被推翻而待复核。"""
    decision = state["decisions"].get(decision_id)
    if decision is None:
        return False
    node = state["nodes"].get(decision["node"])
    current = node
    while current is not None:
        chosen = state["decisions"].get(current.get("decision")) or {}
        if any((state.get("assumptions", {}).get(aid) or {}).get("refuted")
               for aid in chosen.get("assumptions", [])):
            return True
        current = state["nodes"].get((current.get("parent") or {}).get("node"))
    return False


def _sync_notes_locked(review_dir: Path, state: dict[str, Any], by: str) -> None:
    """同锁同步未处理批注和待领取快照；不抽走正在改的批注，不回改已处理稿件。"""
    doc, handoff = store.load(review_dir), store.handoff_load(review_dir)
    in_progress = {nid for item in handoff["items"] if item.get("status") in ("claimed", "reviewing")
                   for nid in item.get("openIds") or []}
    changed, changed_batches = [], False
    for note in doc["notes"]:
        basis = note.get("basis") or {}
        if (not isinstance(basis, dict) or basis.get("analysis") != state["id"]
                or note.get("tracking") is False or note["id"] in in_progress):
            continue
        eligible = note.get("status", "open") == "open" or (note.get("status") == "held" and note.get("hold") == "结论已变")
        if not eligible:
            continue
        valid = decision_in_force(state, basis.get("decision"))
        if not valid and note.get("status", "open") == "open":
            note.update(status="held", hold="结论已变", heldBy=state["id"], updatedAt=store.now_iso())
            for item in handoff["items"]:
                if item.get("status") not in ("pending", "review") or note["id"] not in (item.get("openIds") or []):
                    continue
                item["openIds"] = [nid for nid in item["openIds"] if nid != note["id"]]
                item["openCount"] = len(item["openIds"])
                item["pages"] = sorted({n["page"] for n in doc["notes"] if n["id"] in item["openIds"]})
                if not item["openIds"]:
                    item["status"] = "void"
                changed_batches = True
            changed.append(note["id"])
        elif valid and note.get("status") == "held":
            note.update(status="open", updatedAt=store.now_iso())
            note.pop("hold", None)
            note.pop("heldBy", None)
            changed.append(note["id"])
    if changed_batches:
        store._handoff_commit(review_dir, handoff, {"op": "analysis-hold", "by": by, "analysis": state["id"]})
    if changed:
        store._commit(review_dir, doc, {"op": "analysis-sync", "by": by, "analysis": state["id"], "ids": changed})


def sync_derived_notes(review_dir: Path, analysis_id: str, *, by: str = "analysis") -> dict[str, Any]:
    """分析线等后续操作可在变更完成后公开请求同步；此入口自己持锁。"""
    review_dir = Path(review_dir)
    with store.locked(review_dir):
        state = load(review_dir, analysis_id)
        _sync_notes_locked(review_dir, state, by)
        return state


def _changed_note(doc: dict[str, Any], state: dict[str, Any], note_id: str) -> dict[str, Any]:
    note = store._find(doc, note_id)
    if (note.get("status") != "held" or note.get("hold") != "结论已变" or note.get("tracking") is False
            or (note.get("basis") or {}).get("analysis") != state["id"]):
        raise store.HandoffError("这条批注已不在等待处理的「结论已变」状态", code="note-not-changed")
    return note


def request_update(review_dir: Path, analysis_id: str, note_id: str, *, by: str = "page",
                   authorize: Callable[[], None] | None = None) -> dict[str, Any]:
    review_dir = Path(review_dir)
    with store.locked(review_dir):
        if authorize is not None:
            authorize()
        state = load(review_dir, analysis_id)
        guard_unlocked(state)
        note = _changed_note(store.load(review_dir), state, note_id)
        node = state["nodes"].get(note["basis"]["node"])
        if node is None or not decision_in_force(state, node.get("decision")):
            raise store.HandoffError("当前选择上还没有可用于更新的结论，请先回答这个问题", code="node-not-decided")
        previous = state["updateRequests"].get(note_id)
        if previous and previous["status"] == "requested" and previous.get("decision") == node["decision"]:
            return state
        _record(review_dir, analysis_id, "note-update-requested", by, noteId=note_id, node=node["id"],
                decision=node["decision"], line=state["line"], text=note["text"], rev=state["rev"] + 1)
        _void_open_rounds(review_dir, state, state["line"], "update-requested", by)
        _enqueue_refresh(review_dir, load(review_dir, analysis_id), state["line"], "update", by)
        return load(review_dir, analysis_id)


def _update_valid(state: dict[str, Any], draft: dict[str, Any]) -> bool:
    request = state.get("updateRequests", {}).get(draft["noteId"]) or {}
    return (draft["status"] == "pending" and request.get("status") == "drafted" and request.get("draft") == draft["id"]
            and decision_in_force(state, draft["basis"]["decision"]))


def _update_context(review_dir: Path, state: dict[str, Any], line: str) -> list[dict[str, Any]]:
    doc = store.load(review_dir)
    out = []
    for request in state["updateRequests"].values():
        if request["status"] != "requested" or request.get("line", LINE_MAIN) != line:
            continue
        try:
            note = _changed_note(doc, state, request["noteId"])
        except (store.HandoffError, store.NotFound):
            continue
        node = state["nodes"].get(request["node"])
        if node is not None and decision_in_force(state, node.get("decision")):
            decision = state["decisions"][node["decision"]]
            out.append({"noteId": note["id"], "text": note["text"], "node": node["id"],
                        "question": node["text"], "decision": decision["id"], "answer": decision["label"]})
    return out


def _clean_updates(review_dir: Path, state: dict[str, Any], round_: dict[str, Any], updates: Any) -> list[dict[str, Any]]:
    if updates is None:
        updates = []
    if not isinstance(updates, list):
        raise store.HandoffError("updates 须为批注新文本的列表", code="bad-updates")
    requested = {request["noteId"]: request for request in _update_context(review_dir, state, round_.get("line", LINE_MAIN))}
    doc, out, seen = store.load(review_dir), [], set()
    for raw in updates:
        if (not isinstance(raw, dict) or not isinstance(raw.get("noteId"), str)
                or raw["noteId"] not in requested or raw["noteId"] in seen
                or not isinstance(raw.get("text"), str) or not raw["text"].strip()):
            raise store.HandoffError("批注更新必须对应本轮待起草的批注，并提供不重复的非空新文本", code="bad-updates")
        request = requested[raw["noteId"]]
        if raw.get("node", request["node"]) != request["node"]:
            raise store.HandoffError("新文本的依据问题与更新请求不一致", code="bad-updates")
        note = _changed_note(doc, state, raw["noteId"])
        seen.add(note["id"])
        out.append({"id": f"u{len(state['updates']) + len(out) + 1}", "noteId": note["id"],
                    "text": store.fix_text(raw["text"]), "beforeText": note["text"], "oldBasis": note["basis"],
                    "basis": {"analysis": state["id"], "node": request["node"], "decision": request["decision"],
                              "line": round_.get("line", LINE_MAIN)},
                    "line": round_.get("line", LINE_MAIN), "status": "pending"})
    if set(requested) - seen:
        raise store.HandoffError("本轮还有需要起草新文本的批注，请在 updates 中补齐", code="updates-required")
    return out


def _confirm_update_locked(review_dir: Path, state: dict[str, Any], draft: dict[str, Any], by: str) -> None:
    if not _update_valid(state, draft):
        raise store.HandoffError("这份批注新文本或其依据已经变化，请刷新后查看", code="update-not-pending")
    doc = store.load(review_dir)
    note = _changed_note(doc, state, draft["noteId"])
    if note["text"] != draft["beforeText"] or note["basis"] != draft["oldBasis"]:
        raise store.HandoffError("原批注在起草后被修改过，请重新请求更新", code="update-source-changed")
    history = list(note.get("analysisHistory") or [])
    history.append({"text": note["text"], "basis": note["basis"], "at": store.now_iso(), "by": by})
    note.update(text=draft["text"], basis=draft["basis"], status="open", tracking=True,
                updatedAt=store.now_iso(), analysisHistory=history)
    note.pop("hold", None)
    note.pop("heldBy", None)
    store._commit(review_dir, doc, {"op": "analysis-update", "by": by, "id": note["id"], "draft": draft["id"]})


def _note_choice(review_dir: Path, analysis_id: str, note_id: str, choice: str, by: str,
                 authorize: Callable[[], None] | None) -> dict[str, Any]:
    review_dir = Path(review_dir)
    with store.locked(review_dir):
        if authorize is not None:
            authorize()
        state = load(review_dir, analysis_id)
        guard_unlocked(state)
        doc = store.load(review_dir)
        note = _changed_note(doc, state, note_id)
        if choice == "keep":
            note.update(status="open", tracking=False, updatedAt=store.now_iso())
            note.pop("hold", None)
            note.pop("heldBy", None)
        else:
            doc["notes"] = [item for item in doc["notes"] if item["id"] != note_id]
        store._commit(review_dir, doc, {"op": "analysis-" + choice, "by": by, "id": note_id})
        _record(review_dir, analysis_id, "note-kept" if choice == "keep" else "note-deleted", by,
                noteId=note_id, text=note["text"])
        return note


def keep_note(review_dir: Path, analysis_id: str, note_id: str, *, by: str = "page",
              authorize: Callable[[], None] | None = None) -> dict[str, Any]:
    """明确保留原文后停止跟踪；历史依据保留，不再随分析变化搁住。"""
    return _note_choice(review_dir, analysis_id, note_id, "keep", by, authorize)


def delete_note(review_dir: Path, analysis_id: str, note_id: str, *, by: str = "page",
                authorize: Callable[[], None] | None = None) -> dict[str, Any]:
    return _note_choice(review_dir, analysis_id, note_id, "delete", by, authorize)


def linked_notes(review_dir: Path, analysis_id: str) -> list[dict[str, Any]]:
    return [note for note in store.load(Path(review_dir))["notes"]
            if isinstance(note.get("basis"), dict) and note["basis"].get("analysis") == analysis_id]


def full_check_context(review_dir: Path) -> dict[str, Any]:
    """四类只读待确认资料；语义矛盾交给 Agent 核对，绝不借此改注或回改已处理稿。"""
    review_dir = Path(review_dir)
    states = {aid: load(review_dir, aid) for aid in _analyses_ids(review_dir)}
    summaries = []
    for state in states.values():
        conclusions = []
        for node in state["nodes"].values():
            if decision_in_force(state, node.get("decision")):
                decision = state["decisions"][node["decision"]]
                conclusions.append({"node": node["id"], "decision": decision["id"],
                                    "question": node["text"], "answer": decision["label"],
                                    "assumptions": decision.get("assumptions", [])})
        summaries.append({"analysis": state["id"], "focus": state["focus"], "line": state["line"],
                          "status": state["status"], "conclusions": conclusions})
    changed, resolved = [], []
    for note in store.load(review_dir)["notes"]:
        basis = note.get("basis") or {}
        if not isinstance(basis, dict) or note.get("tracking") is False:
            continue
        state = states.get(basis.get("analysis"))
        if state is None:
            continue
        item = {"id": note["id"], "text": note["text"], "page": note["page"], "basis": basis}
        if note.get("status") == "held" and note.get("hold") == "结论已变":
            changed.append(item)
        if note.get("status") == "resolved" and not decision_in_force(state, basis.get("decision")):
            resolved.append(item)
    return {"analyses": summaries, "checks": [
        {"kind": "changed-notes", "notes": changed},
        {"kind": "resolved-changed-basis", "notes": resolved},
        {"kind": "report-conflicts", "analyses": summaries},
        {"kind": "analysis-conflicts", "analyses": summaries},
    ]}
