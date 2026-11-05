"""分析存储：审阅目录下 analysis/<分析标识>/events.jsonl 的只追加事件日志。

- 事件只追加（0600），当前状态由事件日志推导，不落推导缓存；事件日志是唯一事实来源。
- 与批注存储共用审阅目录的锁（notes_store.locked）：搁住 / 解除源批注、移出待接收批次、
  结论转出批注与记录事件在同一把锁内完成。
- 轮次号在整份审阅内唯一（analysis/counter.json 分配），因为投递去重键里没有分析标识。
- 每个分析有单调递增的基线版本号（rev）：发起 +1，每次写回 +1；作答不改基线
  （Agent 领取期间审阅人作答不受影响，与「锁整个分析、作答除外」的语义一致）。
- 问答记录不截断。决策由系统根据审阅人的回答产生，不由 Agent 写入；
  结构提议经审阅人在轮次中确认后才进入问题树（不采纳的提议不再出现）。
- 稳定标识：分析 id、分析线（本票只有主线 main，后续票分线复用同一命名空间）、
  问题节点 n1…、决策 d1…（按事件重放顺序确定，重放同一日志得到同一批标识）、选项 id（Agent 提议内给定）。

与 notes_store 的分工：批注状态的 held / resolved / open 与批次快照的改写都通过
notes_store 的不加锁内部函数在本模块的锁内完成（同一次写入里保持两边一致）。

只用 Python 标准库。
"""
from __future__ import annotations

import contextlib
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
LINE_MAIN = "main"   # 分析线标识：本票只有主线；分线由后续票加入，标识在此命名空间内延续
HOLD_REASON = "分析中"
# 轮次状态：open 等审阅人作答 → submitted 已交出（在分析队列里）→ claimed Agent 处理中
#   → done 写回完成；void 作废（结案等后续票使用）
ROUND_OPEN = ("open", "submitted")   # 审阅人可作答的状态
MAX_PROPOSALS = 50   # 一轮写回最多提出的结构提议数（防误传超大输入；问答文本本身不设上限）
MAX_ROUND_KEEP = 0   # 0 = 不截断：问答记录全部保留
PROP_KEY_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
OPT_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,32}$")


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
    """问题节点是否处在当前选择枝上：父节点已决到挂载它的那个选项（原型 isActive，本票无断枝）。"""
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


def _decide(state: dict[str, Any], node: dict[str, Any], option: str, round_no: int) -> dict[str, Any]:
    """作答或重裁到某个选项：同一选项有旧决策就复用（同一标识），否则新建。决策由系统产生。"""
    for did in node["history"]:
        if state["decisions"][did]["option"] == option:
            node["decision"], node["status"] = did, "decided"
            return state["decisions"][did]
    did = f"d{state['seq']['decision'] + 1}"
    state["seq"]["decision"] += 1
    decision = {"id": did, "node": node["id"], "option": option, "label": node["options"][option],
                "round": round_no, "line": state["line"]}
    state["decisions"][did] = decision
    node["history"].append(did)
    node["decision"], node["status"] = did, "decided"
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


def _apply_answer(state: dict[str, Any], round_: dict[str, Any], key: str, value: dict[str, Any]) -> None:
    round_["answers"][key] = value
    if key.startswith("S:"):
        confirmed = round_.setdefault("proposalNodes", {})
        node_id = confirmed.get(key)
        if value["kind"] == "accept":
            round_["rejected"].discard(key)
            if node_id is None:
                confirmed[key] = _confirm_proposal(state, next(p for p in round_["proposals"] if "S:" + p["key"] == key))
            else:
                node = state["nodes"][node_id]
                node["status"] = "decided" if node.get("decision") else "pending"
        else:
            round_["rejected"].add(key)
            if node_id is not None:
                state["nodes"][node_id]["status"] = "void"
        return
    node_id = key[2:]
    node = state["nodes"][node_id]
    option = node["recommend"] if value["kind"] == "accept" else value["option"]
    _decide(state, node, option, round_["seq"])


def _frontier_question_items(state: dict[str, Any]) -> list[dict[str, Any]]:
    """当前前沿上待问的问题条目：已入树、未决、处在当前选择枝上的节点（含挂载条件未满足之外的全部）。"""
    return [{"key": "Q:" + nid, "type": "question", "node": nid}
            for nid in sorted(state["nodes"], key=lambda x: int(x[1:]))
            if state["nodes"][nid]["status"] == "pending" and _is_active(state, nid)]


def _round_items(state: dict[str, Any], round_: dict[str, Any]) -> list[dict[str, Any]]:
    """轮次的可作答条目：还没答、没被拒的结构提议，加上前沿上待问的问题。

    问题条目随状态动态出现（确认提议入树后同轮即可作答；没答的问题一直留到下一轮）。"""
    if round_["status"] in ("done", "void"):
        return [{"key": "S:" + p["key"], "type": "structure", "proposal": p} for p in round_["proposals"]]
    items = [{"key": "S:" + p["key"], "type": "structure", "proposal": p}
             for p in round_["proposals"]
             if "S:" + p["key"] not in round_["answers"] and "S:" + p["key"] not in round_["rejected"]]
    items.extend(_frontier_question_items(state))
    return items


def _replay(events: list[dict[str, Any]]) -> dict[str, Any]:
    first = events[0]
    state: dict[str, Any] = {
        "id": None, "schema": first["schema"], "status": "active", "rev": 0,
        "focus": first.get("focus", ""), "sourceNote": first.get("sourceNote"),
        "sourceCopy": first.get("sourceCopy") or {}, "sourceState": "held", "sourceResolvedNote": None,
        "line": LINE_MAIN, "seq": {"node": 0, "decision": 0, "draft": 0},
        "nodes": {}, "decisions": {}, "rounds": [], "drafts": [],
    }
    for event in events:
        kind = event.get("type")
        if kind == "started":
            state["id"] = event.get("id")
            state["rev"] = event.get("rev", 1)
        elif kind == "round-requested":
            state["rounds"].append({"seq": event["round"], "kind": event.get("kind", "next"),
                                    "status": "open", "proposals": event.get("items") or [],
                                    "answers": {}, "rejected": set(), "at": event.get("at"),
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
            state["rev"] = event.get("rev", state["rev"] + 1)
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
            state["sourceState"] = "resolved"
            state["sourceResolvedNote"] = event.get("note")
    for round_ in state["rounds"]:
        round_["rejected"] = sorted(round_["rejected"])
        round_["items"] = _round_items(state, round_)
    state.pop("seq", None)   # 内部分配器不外露
    return state


def _round(state: dict[str, Any], seq: int) -> dict[str, Any]:
    round_ = next((r for r in state["rounds"] if r["seq"] == seq), None)
    if round_ is None:
        raise store.NotFound(f"round#{seq}")
    return round_


def load(review_dir: Path, analysis_id: str) -> dict[str, Any]:
    """读某个分析的当前状态（由事件日志推导）。"""
    return _replay(_read_events(Path(review_dir), analysis_id))


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
                    "openRound": next((r["seq"] for r in state["rounds"] if r["status"] in ROUND_OPEN), None)})
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
        return load(review_dir, analysis_id), round_no


def answer(review_dir: Path, analysis_id: str, round_no: int, key: str, value: dict[str, Any], *,
           by: str = "page") -> dict[str, Any]:
    """在轮次中作答：确认 / 不采纳结构提议，或用推荐答案 / 选一个选项回答问题。

    回答按题目幂等（改主意就再答一次，取最后一次）；已交出的回答合并进同一轮。"""
    review_dir = Path(review_dir)
    with store.locked(review_dir):
        state = load(review_dir, analysis_id)
        round_ = _round(state, round_no)
        if round_["status"] not in ROUND_OPEN:
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
        if item is None:
            raise store.HandoffError(f"第 {round_no} 轮里没有 {key} 这一项", code="item-not-in-round",
                                     round=round_no, key=key)
        _check_answer_value(state, item, value)
        if round_["answers"].get(key) == value:
            return state
        _append_event(review_dir, analysis_id, {"type": "answered", "at": store.now_iso(), "by": by,
                                                "round": round_no, "key": key, "value": value})
        return load(review_dir, analysis_id)


def _check_answer_value(state: dict[str, Any], item: dict[str, Any], value: Any) -> None:
    if not isinstance(value, dict):
        raise store.NoteError("回答须为 {kind: ...} 对象")
    kind = value.get("kind")
    if item["type"] == "structure":
        if kind not in ("accept", "reject"):
            raise store.NoteError("结构提议的回答只能是 accept（同意）或 reject（不采纳）")
        return
    options = state["nodes"][item["node"]]["options"]
    if kind == "accept":
        return
    if kind == "option":
        if value.get("option") not in options:
            raise store.NoteError(f"option 须为该题的选项之一（{'、'.join(options)}）")
        return
    raise store.NoteError("本票的回答方式只有用推荐答案（accept）和选一个选项（option）")


def submit_round(review_dir: Path, analysis_id: str, round_no: int, *, by: str = "page",
                 authorize: Callable[[], None] | None = None) -> dict[str, Any]:
    """交出轮次：open → submitted（进入分析队列）。已交出的重复交出合并，不重复排队。"""
    review_dir = Path(review_dir)
    with store.locked(review_dir):
        if authorize is not None:
            authorize()
        state = load(review_dir, analysis_id)
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
        raise store.NoteError("proposals 须为结构提议列表")
    if len(proposals) > MAX_PROPOSALS:
        raise store.NoteError(f"一轮最多提出 {MAX_PROPOSALS} 个结构提议")
    out, seen = [], set()
    for raw in proposals:
        if not isinstance(raw, dict):
            raise store.NoteError("结构提议须为对象")
        key = raw.get("key")
        if not isinstance(key, str) or not PROP_KEY_RE.match(key) or key in seen:
            raise store.NoteError(f"结构提议的 key 须为不重复的 {PROP_KEY_RE.pattern}")
        seen.add(key)
        text = store.fix_text(raw.get("text")).strip()
        if not text:
            raise store.NoteError("问题文本不能为空")
        options_raw = raw.get("options")
        if not isinstance(options_raw, list) or not options_raw:
            raise store.NoteError("结构提议须带非空 options 列表")
        options, oids = {}, set()
        for o in options_raw:
            if not isinstance(o, dict) or not isinstance(o.get("id"), str) or not OPT_ID_RE.match(o.get("id", "")) \
                    or not isinstance(o.get("label"), str) or not o.get("label").strip():
                raise store.NoteError("选项须为 {id, label}，id 只允许字母数字、下划线和连字符")
            if o["id"] in oids:
                raise store.NoteError(f"选项 id 重复：{o['id']}")
            oids.add(o["id"])
            options[o["id"]] = store.fix_text(o["label"]).strip()
        recommend = raw.get("recommend")
        if recommend not in options:
            raise store.NoteError("recommend 须为 options 之一")
        parent = raw.get("parent")
        if parent is not None:
            if not isinstance(parent, dict) or not isinstance(parent.get("node"), str):
                raise store.NoteError("parent 须为 {node, option} 或 null")
            pnode = state["nodes"].get(parent["node"])
            if pnode is None:
                raise store.NoteError(f"挂在的问题 {parent['node']} 还不在问题树上（须先经确认）")
            if parent.get("option") not in pnode["options"]:
                raise store.NoteError(f"parent.option 须为 {parent['node']} 的选项之一")
        out.append({"key": key, "text": text,
                    "short": store.fix_text(raw.get("short")).strip()[:200] or text[:24],
                    "options": [{"id": k, "label": v} for k, v in options.items()],
                    "recommend": recommend, "parent": parent})
    return out


def writeback(review_dir: Path, analysis_id: str, round_no: int, base_rev: int, proposals: Any, *,
              note: str = "", by: str = "agent") -> dict[str, Any]:
    """Agent 写回轮次：校验领取时记录的基线版本，记下这一轮的说明，
    把没答的提议带进下一轮并附上新的结构提议；没有可作答的内容就不再开新轮。"""
    review_dir = Path(review_dir)
    with store.locked(review_dir):
        state = load(review_dir, analysis_id)
        round_ = _round(state, round_no)
        if round_["status"] != "claimed":
            raise store.HandoffError(f"第 {round_no} 轮没有被领取，不能写回（{round_['status']}）",
                                     code="round-not-claimed", round=round_no, status=round_["status"])
        if base_rev != state["rev"]:
            raise store.HandoffError(f"第 {round_no} 轮接手后分析已被改过（基线 {base_rev} → {state['rev']}），"
                                     "这次结果作废", code="base-rev-mismatch",
                                     round=round_no, expected=base_rev, actual=state["rev"])
        cleaned = _clean_proposals(proposals, state)
        new_rev = state["rev"] + 1
        _append_event(review_dir, analysis_id, {"type": "round-done", "at": store.now_iso(), "by": by,
                                                "round": round_no, "note": store.fix_text(note),
                                                "rev": new_rev})
        next_round = None
        carried = [p for p in round_["proposals"]
                   if "S:" + p["key"] not in round_["answers"] and "S:" + p["key"] not in set(round_["rejected"])]
        fresh = [p for p in cleaned if p["key"] not in {c["key"] for c in carried}]
        frontier = _frontier_question_items(load(review_dir, analysis_id))
        if carried or fresh or frontier:
            next_round = _next_round(review_dir)
            _append_event(review_dir, analysis_id, {"type": "round-requested", "at": store.now_iso(),
                                                    "by": by, "round": next_round, "kind": "next",
                                                    "items": carried + fresh})
        after = load(review_dir, analysis_id)
        return {"state": after, "nextRound": next_round}


def round_context(review_dir: Path, analysis_id: str, round_no: int) -> dict[str, Any]:
    """一轮的完整上下文：焦点问题、当前路径上的决策、本轮条目（含问题文本与选项、推荐答案）、
    审阅人原话回答、源批注原文副本。"""
    state = load(Path(review_dir), analysis_id)
    round_ = _round(state, round_no)
    decisions = [{"id": d["id"], "node": d["node"], "nodeShort": state["nodes"][d["node"]]["short"],
                  "question": state["nodes"][d["node"]]["text"], "option": d["option"], "label": d["label"],
                  "round": d["round"], "line": d["line"]}
                 for nid, node in sorted(state["nodes"].items(), key=lambda kv: int(kv[0][1:]))
                 if node.get("decision") and _is_active(state, nid)
                 for d in [state["decisions"][node["decision"]]]
                 if state["decisions"][node["decision"]]["line"] == state["line"]]
    items = []
    for item in round_["items"]:
        if item["type"] == "structure":
            p = item["proposal"]
            items.append({"key": item["key"], "type": "structure", "text": p["text"], "short": p["short"],
                          "options": [{"id": o["id"], "label": o["label"]} for o in p["options"]],
                          "recommend": p["recommend"], "parent": p.get("parent")})
        else:
            node = state["nodes"][item["node"]]
            items.append({"key": item["key"], "type": "question", "node": node["id"], "text": node["text"],
                          "short": node["short"],
                          "options": [{"id": k, "label": v} for k, v in node["options"].items()],
                          "recommend": node["recommend"]})
    return {"analysis": analysis_id, "focus": state["focus"], "line": state["line"],
            "round": round_no, "kind": round_["kind"], "status": round_["status"],
            "baseRev": round_.get("baseRev"), "decisions": decisions, "items": items,
            "answers": round_["answers"], "sourceCopy": state["sourceCopy"]}


def convert(review_dir: Path, analysis_id: str, node_id: str, *, text: str | None = None,
            by: str = "page") -> dict[str, Any]:
    """结论转批注草稿：先生成草稿（存在分析里），确认后才进入批注列表。"""
    review_dir = Path(review_dir)
    with store.locked(review_dir):
        state = load(review_dir, analysis_id)
        node = state["nodes"].get(node_id)
        if node is None or not node.get("decision") or not _is_active(state, node_id):
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
        draft = next((d for d in state["drafts"] if d["id"] == draft_id), None)
        if draft is None or draft["status"] != "pending":
            raise store.HandoffError(f"{draft_id} 不是等你确认的批注草稿",
                                     code="draft-not-pending", draft=draft_id)
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
                "basis": basis, "fromAnalysis": analysis_id}
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
