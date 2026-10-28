"""批注存储：审阅目录下的 notes.json（全量）+ notes.jsonl（追加日志）。

服务端（serve.py）和命令行（notes.py）都通过这里读写，保证：
- 每次改动按「操作」应用到磁盘上的最新内容，页面和 Claude 同时改也不会互相覆盖；
- 写 notes.json 用临时文件 + os.replace 原子替换，断电或中途退出不会写坏；
- 改动在同一把文件锁（fcntl.flock）下完成；
- 每次改动追加一行到 notes.jsonl，可追溯谁在何时改了什么；
- 新建的文件只有本用户可读写（0600），因为批注里可能有被审阅材料的原文。

只用 Python 标准库。
"""
from __future__ import annotations

import contextlib
import fcntl
import json
import math
import os
import re
import secrets
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

NOTES_FILE = "notes.json"
LOG_FILE = "notes.jsonl"
LOCK_FILE = ".notes.lock"
SCHEMA_VERSION = 1
STATUSES = ("open", "resolved")
# 页面可写的字段；id / at / status 相关字段由存储层或专门操作维护
EDITABLE = ("page", "title", "target", "text", "quote", "quoteFrom", "pin", "region", "edit", "pair", "skills", "style",
            "anchor", "key", "orphan")
# key：页面键（报告 section 的 data-key），重排页序后据此把批注挪回正确的页；orphan：重建后原页面已不存在
# 报告上的标注：pin = 点 {x, y}，region = 框 {x, y, w, h}，都是相对页面的百分比。页面新写的批注只带其中一种
# （新建、重新标注时两个字段一起发，另一种为 null）；拖动只改被拖的那一种。
# 风格优化的范围跟着标注走：有框 = region（框选处），有点 = pin（标点处），都没有 = page（整页）。
# 0.3 及更早只有风格优化能框选，框存在 style.region 里；读入时挪到顶层 region，不改原来的范围、不删点。
STYLE_SCOPES = ("page", "region", "pin")
# 处理这条批注时需要调用的技能（skill / 工具名），由用户在页面上指定，代理处理前先调用
MAX_SKILLS = 10
SKILLS_FILE = "skills.json"
# 另一侧的对应句：用户在一侧选中文字时，页面在同一页另一侧找到的相同 / 相近句子，并勾选了「同时改」
PAIR_SIDES = ("报告", "讲稿")
PAIR_MATCH = ("exact", "similar")
# 快捷修改：用户选中原文后直接给出的精确改动（删除 / 改为）。text 里另有一句人能读的说明，旧工具照样能看懂
EDIT_OPS = ("delete", "replace")
DELETE_TEXT = "删除引用的这段文字"
REPLACE_PREFIX = "改为："
MAX_TEXT = 20_000
MAX_SHORT = 200
# id 会进入页面的 HTML 属性和 URL 路径，只允许这几类字符，其余一律重新生成
ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


class NoteError(ValueError):
    """请求内容不合法（返回 400）。"""


class NotFound(KeyError):
    """批注不存在（返回 404）。"""


def now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def new_id() -> str:
    return format(int(time.time() * 1000), "x") + secrets.token_hex(3)


def valid_id(x: Any) -> bool:
    return isinstance(x, str) and bool(ID_RE.match(x))


def empty_doc() -> dict[str, Any]:
    return {"version": SCHEMA_VERSION, "rev": 0, "updatedAt": None, "notes": []}


def _require_dir(review_dir: Path) -> None:
    if not review_dir.is_dir():
        raise FileNotFoundError(f"审阅目录不存在：{review_dir}")


@contextlib.contextmanager
def locked(review_dir: Path) -> Iterator[None]:
    _require_dir(review_dir)
    fd = os.open(review_dir / LOCK_FILE, os.O_RDWR | os.O_CREAT, 0o600)
    with os.fdopen(fd, "r+") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(fh, fcntl.LOCK_UN)


def load(review_dir: Path) -> dict[str, Any]:
    """读 notes.json；文件不存在返回空文档。文件损坏时抛错，不静默覆盖。"""
    path = review_dir / NOTES_FILE
    if not path.exists():
        return empty_doc()
    doc = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(doc, dict) or not isinstance(doc.get("notes"), list):
        raise ValueError(f"{path} 结构不对：缺少 notes 列表")
    doc.setdefault("rev", 0)
    doc.setdefault("version", SCHEMA_VERSION)
    for n in doc["notes"]:
        n.setdefault("status", "open")
        if isinstance(n, dict):
            _settle_marks(n)   # 0.3 旧数据：框从 style.region 挪到顶层 region（下次写盘时落盘）
    return doc


def _atomic_write(path: Path, text: str) -> None:
    fd, tmp = tempfile.mkstemp(prefix=".notes-", suffix=".tmp", dir=path.parent)  # mkstemp 建出的文件是 0600
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(tmp)
        raise


def append_private(path: Path, line: str) -> None:
    """追加一行；文件不存在时以 0600 新建。"""
    fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
    with os.fdopen(fd, "a", encoding="utf-8") as fh:
        fh.write(line if line.endswith("\n") else line + "\n")


def _dumps(obj: Any, **kw: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, allow_nan=False, **kw)


def _append_log(review_dir: Path, entry: dict[str, Any]) -> None:
    append_private(review_dir / LOG_FILE, _dumps(entry))


def fix_text(v: Any) -> str:
    """转成字符串并修掉孤立的 UTF-16 代理（半个 emoji 等），否则写盘时会抛 UnicodeEncodeError。"""
    s = "" if v is None else str(v)
    return s.encode("utf-8", "surrogatepass").decode("utf-8", "replace") if any("\ud800" <= c <= "\udfff" for c in s) else s


def _clean_pin(pin: Any) -> dict[str, float] | None:
    if pin in (None, "", {}):
        return None
    if not isinstance(pin, dict):
        raise NoteError("pin 须为 {x, y} 或 null")
    try:
        x, y = float(pin["x"]), float(pin["y"])
    except (KeyError, TypeError, ValueError) as e:
        raise NoteError("pin 须含数值 x、y（百分比）") from e
    if not (math.isfinite(x) and math.isfinite(y) and 0 <= x <= 100 and 0 <= y <= 100):
        raise NoteError("pin 坐标须在 0–100 之间")
    return {"x": round(x, 1), "y": round(y, 1)}


def _clean_region(v: Any, name: str = "region") -> dict[str, float] | None:
    if v in (None, "", {}):
        return None
    if not isinstance(v, dict):
        raise NoteError(f"{name} 须为 {{x, y, w, h}} 或 null")
    try:
        x, y, w, h = (float(v[k]) for k in ("x", "y", "w", "h"))
    except (TypeError, KeyError, ValueError) as e:
        raise NoteError(f"{name} 须含数值 x、y、w、h（百分比）") from e
    if not all(math.isfinite(t) for t in (x, y, w, h)) or not (0 <= x <= 100 and 0 <= y <= 100 and 0 < w <= 100 and 0 < h <= 100):
        raise NoteError(f"{name} 超出页面范围")
    return {"x": round(x, 1), "y": round(y, 1), "w": round(min(w, 100 - x), 1), "h": round(min(h, 100 - y), 1)}


def region_of(n: dict[str, Any]) -> dict[str, Any] | None:
    """批注在报告上框选的范围；兼容 0.3 存在 style.region 里的旧数据。"""
    r = n.get("region")
    if isinstance(r, dict):
        return r
    st = n.get("style")
    return st["region"] if isinstance(st, dict) and isinstance(st.get("region"), dict) else None


def _settle_marks(note: dict[str, Any], fields: dict[str, Any] | None = None) -> None:
    """整理标注。

    - 0.3 旧数据的框（style.region）挪到顶层 region；原来的风格范围、点都原样保留（不丢用户当时的选择）。
    - 这次改动动了标注时，风格优化的范围跟着标注走；只有「整页」且这次没有同时给出点和框（只是拖动）时保持整页。
    """
    st = note.get("style")
    if isinstance(st, dict) and isinstance(st.get("region"), dict):
        if not note.get("region"):
            note["region"] = st["region"]
        note["style"] = st = {k: v for k, v in st.items() if k != "region"}
    if not note.get("region"):
        note.pop("region", None)
    if fields and isinstance(st, dict) and st.get("scope") and ("pin" in fields or "region" in fields):
        if ("pin" in fields and "region" in fields) or st["scope"] != "page":
            note["style"] = {"scope": "region" if note.get("region") else "pin" if note.get("pin") else "page"}


def _clean_edit(v: Any) -> dict[str, str] | None:
    if v in (None, "", {}):
        return None
    if not isinstance(v, dict) or v.get("op") not in EDIT_OPS:
        raise NoteError(f"edit.op 须为 {EDIT_OPS} 之一")
    src = fix_text(v.get("from"))
    if not src.strip() or len(src) > MAX_TEXT:
        raise NoteError("edit.from 须为选中的原文（非空）")
    out = {"op": v["op"], "from": src}
    if v["op"] == "replace":
        to = fix_text(v.get("to"))
        if len(to) > MAX_TEXT:
            raise NoteError("edit.to 过长")
        out["to"] = to
    return out


def _clean_pair(v: Any) -> dict[str, str] | None:
    if v in (None, "", {}):
        return None
    if not isinstance(v, dict) or v.get("side") not in PAIR_SIDES:
        raise NoteError(f"pair.side 须为 {PAIR_SIDES} 之一")
    text = fix_text(v.get("text"))
    if not text.strip() or len(text) > MAX_TEXT:
        raise NoteError("pair.text 须为另一侧的对应文字（非空）")
    return {"side": v["side"], "text": text, "match": v.get("match") if v.get("match") in PAIR_MATCH else "similar"}


def clean_skill_names(v: Any) -> list[str]:
    """技能名单：接受列表或逗号 / 顿号分隔的字符串；去空白、去重，最多 10 个，每个最长 64 字。"""
    if v in (None, "", []):
        return []
    items = v if isinstance(v, list) else re.split(r"[,，、;；\n]+", str(v))
    out: list[str] = []
    for x in items:
        name = re.sub(r"\s+", " ", fix_text(x)).strip()[:64]
        if name and name not in out:
            out.append(name)
    return out[:MAX_SKILLS]


def skills_catalog(review_dir: Path) -> list[dict[str, str]]:
    """审阅目录里的技能目录（代理写入的可用技能，页面据此给出候选）。读不到就返回空。"""
    try:
        doc = json.loads((review_dir / SKILLS_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    out = []
    for it in doc.get("skills", []) if isinstance(doc, dict) else []:
        if isinstance(it, dict) and str(it.get("name", "")).strip():
            out.append({"name": fix_text(it["name"]).strip()[:64], "desc": fix_text(it.get("desc", "")).strip()[:120]})
    return out[:200]


def skills_save(review_dir: Path, items: list[dict[str, str]]) -> None:
    _require_dir(review_dir)
    _atomic_write(review_dir / SKILLS_FILE, _dumps({"version": 1, "skills": items[:200]}, indent=2) + "\n")


def _clean_style(v: Any) -> dict[str, Any] | None:
    if v in (None, "", {}):
        return None
    if not isinstance(v, dict) or v.get("scope") not in STYLE_SCOPES:
        raise NoteError(f"style.scope 须为 {STYLE_SCOPES} 之一")
    out: dict[str, Any] = {"scope": v["scope"]}
    if v.get("region") not in (None, "", {}):   # 0.3 旧格式：框存在 style.region 里；由 _settle_marks 挪到顶层
        out["region"] = _clean_region(v["region"], "style.region")
    return out


def _clean_time(v: Any) -> str | None:
    if isinstance(v, str) and 0 < len(v) <= 64:
        return v
    return None


def _clean_fields(data: dict[str, Any], *, partial: bool) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for k in EDITABLE:
        if k not in data:
            continue
        v = data[k]
        if k == "page":
            if isinstance(v, bool) or not isinstance(v, (int, str)):
                raise NoteError("page 须为页码")
            try:
                v = int(v)
            except ValueError as e:
                raise NoteError("page 须为页码") from e
            if not 0 < v < 100_000:
                raise NoteError("page 超出范围")
        elif k == "pin":
            v = _clean_pin(v)
        elif k == "region":
            v = _clean_region(v)
        elif k == "edit":
            v = _clean_edit(v)
        elif k == "pair":
            v = _clean_pair(v)
        elif k == "skills":
            v = clean_skill_names(v)
        elif k == "style":
            v = _clean_style(v)
        elif k == "anchor":
            v = fix_text(v)[:300]
        elif k == "key":
            v = fix_text(v).strip()[:120]
        elif k == "orphan":
            v = bool(v)
        else:
            v = fix_text(v)
            if k in ("text", "quote"):
                if len(v) > MAX_TEXT:
                    raise NoteError(f"{k} 过长（上限 {MAX_TEXT} 字）")
            else:  # 标题、对象等短字段：超长就截断，不拒绝（否则这一页永远加不上批注）
                v = v[:MAX_SHORT]
        out[k] = v
    st = out.get("style")
    if isinstance(st, dict) and "region" in st:   # 0.3 页面的改动（含离线队列补传）：框写在 style.region 里
        reg = st.pop("region")
        out.setdefault("region", reg)
    if not partial:
        if "page" not in out:
            raise NoteError("缺少 page")
        if not out.get("text", "").strip():
            raise NoteError("批注内容为空")
    return out


def _commit(review_dir: Path, doc: dict[str, Any], log: dict[str, Any]) -> dict[str, Any]:
    doc["rev"] = int(doc.get("rev", 0)) + 1
    doc["updatedAt"] = now_iso()
    doc["version"] = SCHEMA_VERSION
    _atomic_write(review_dir / NOTES_FILE, _dumps(doc, indent=2) + "\n")
    _append_log(review_dir, {"ts": doc["updatedAt"], "rev": doc["rev"], **log})
    return doc


def _find(doc: dict[str, Any], note_id: str) -> dict[str, Any]:
    for n in doc["notes"]:
        if n.get("id") == note_id:
            return n
    raise NotFound(note_id)


def _new_note(raw: dict[str, Any], fields: dict[str, Any], nid: str) -> dict[str, Any]:
    note = {"id": nid, "title": "", "target": "", "quote": "", "quoteFrom": "", "pin": None, **fields,
            "status": raw.get("status") if raw.get("status") in STATUSES else "open",
            "at": _clean_time(raw.get("at")) or now_iso()}
    if note["status"] == "resolved":
        for k in ("resolution", "resolvedAt", "resolvedBy"):
            if raw.get(k) not in (None, ""):
                note[k] = str(raw[k])[:MAX_TEXT]
    if _clean_time(raw.get("updatedAt")):
        note["updatedAt"] = raw["updatedAt"]
    _settle_marks(note)
    return note


def add(review_dir: Path, data: dict[str, Any], *, by: str = "page") -> tuple[dict[str, Any], dict[str, Any]]:
    """新增一条。带合法 id 且已存在时视为重复提交（离线补传重试），原样返回、不再写入。"""
    fields = _clean_fields(data, partial=False)
    with locked(review_dir):
        doc = load(review_dir)
        nid = data.get("id")
        if valid_id(nid):
            for n in doc["notes"]:
                if n.get("id") == nid:
                    return doc, n
        else:
            nid = new_id()
        note = _new_note({"at": data.get("at")}, fields, nid)
        doc["notes"].append(note)
        return _commit(review_dir, doc, {"op": "add", "by": by, "id": nid, "note": note}), note


def update(review_dir: Path, note_id: str, data: dict[str, Any], *, by: str = "page") -> tuple[dict[str, Any], dict[str, Any]]:
    fields = _clean_fields(data, partial=True)
    if "text" in fields and not fields["text"].strip():
        raise NoteError("批注内容为空")
    with locked(review_dir):
        doc = load(review_dir)
        note = _find(doc, note_id)
        ed = note.get("edit") if isinstance(note.get("edit"), dict) else None
        if ed and "text" in fields and "edit" not in fields:   # 旧版页面只改 text：同步到快捷修改的结构上
            if ed.get("op") == "replace" and fields["text"].startswith(REPLACE_PREFIX):
                fields["edit"] = {**ed, "to": fields["text"][len(REPLACE_PREFIX):]}
            elif fields["text"] not in (DELETE_TEXT,) and not fields["text"].startswith(REPLACE_PREFIX):
                fields["edit"] = None   # 改成了普通意见
        note.update(fields)
        _settle_marks(note, fields)
        if note.get("edit") is None:
            note.pop("edit", None)
        note["updatedAt"] = now_iso()
        return _commit(review_dir, doc, {"op": "update", "by": by, "id": note_id, "fields": fields}), note


def set_status(review_dir: Path, note_id: str, status: str, *, resolution: str = "", by: str = "page") -> tuple[dict[str, Any], dict[str, Any]]:
    if status not in STATUSES:
        raise NoteError(f"status 须为 {STATUSES} 之一")
    with locked(review_dir):
        doc = load(review_dir)
        note = _find(doc, note_id)
        note["status"] = status
        if status == "resolved":
            note["resolvedAt"] = now_iso()
            note["resolvedBy"] = by
            if resolution:
                note["resolution"] = fix_text(resolution)[:MAX_TEXT]
            note.pop("resolvedBatch", None)
            if by != "page":   # 代理标的：记下当时处理中的批次，处理记录据此统计每批解决了哪些
                try:
                    cur = next((i for i in reversed(handoff_load(review_dir)["items"]) if i.get("status") == "claimed"), None)
                except (OSError, ValueError):
                    cur = None
                if cur:
                    note["resolvedBatch"] = int(cur.get("seq", 0))
        else:
            for k in ("resolvedAt", "resolvedBy", "resolution", "resolvedBatch"):
                note.pop(k, None)
        op = "resolve" if status == "resolved" else "reopen"
        return _commit(review_dir, doc, {"op": op, "by": by, "id": note_id, "resolution": resolution}), note


def delete(review_dir: Path, note_id: str, *, by: str = "page") -> dict[str, Any]:
    with locked(review_dir):
        doc = load(review_dir)
        note = _find(doc, note_id)
        doc["notes"] = [n for n in doc["notes"] if n.get("id") != note_id]
        return _commit(review_dir, doc, {"op": "delete", "by": by, "id": note_id, "note": note})


def dedup_key(n: dict[str, Any]) -> tuple:
    """内容相同的判定：页码、对象、正文、引用、标注（点 / 框）、是否风格优化都相同。同一页不同位置写同样的话不算重复。"""
    pin = n.get("pin") or None
    pin_t = (round(float(pin["x"]), 1), round(float(pin["y"]), 1)) if isinstance(pin, dict) and "x" in pin else None
    rg = region_of(n)
    rg_t = tuple(round(float(rg.get(k, 0) or 0), 1) for k in ("x", "y", "w", "h")) if rg else None
    st_t = bool(isinstance(n.get("style"), dict) and n["style"].get("scope"))
    return (int(n.get("page", 0)), str(n.get("target", "")).strip(), str(n.get("text", "")).strip(),
            str(n.get("quote", "")).strip(), pin_t, rg_t, st_t)


def _stamp(n: dict[str, Any]) -> float:
    """批注最后一次变化的时间（取 updatedAt / resolvedAt / at 中最晚的），解析失败当作 0。"""
    best = 0.0
    for k in ("updatedAt", "resolvedAt", "at"):
        v = n.get(k)
        if not isinstance(v, str):
            continue
        try:
            dt = datetime.fromisoformat(v.replace("Z", "+00:00"))
            best = max(best, (dt if dt.tzinfo else dt.astimezone()).timestamp())
        except ValueError:
            continue
    return best


def import_notes(review_dir: Path, items: list[dict[str, Any]], *, by: str = "import") -> tuple[dict[str, Any], int, int, int]:
    """批量导入（旧版导出、离线模式积压等）。返回 (文档, 新增, 跳过, 更新)。

    - 同 id 已存在：导入方更新得更晚（编辑过、标过已处理）就覆盖内容与状态，否则跳过；
    - 内容相同（见 dedup_key）：跳过；
    - id 不合法或缺失：重新生成。
    """
    if not isinstance(items, list):
        raise NoteError("导入内容须为批注列表")
    with locked(review_dir):
        doc = load(review_dir)
        by_id = {n.get("id"): n for n in doc["notes"]}
        keys = {dedup_key(n) for n in doc["notes"]}
        added = skipped = updated = 0
        for raw in items:
            if not isinstance(raw, dict):
                raise NoteError("导入项须为对象")
            fields = _clean_fields(raw, partial=False)
            nid = raw.get("id") if valid_id(raw.get("id")) else None
            if nid and nid in by_id:
                cur = by_id[nid]
                if _stamp(raw) > _stamp(cur):
                    fresh = _new_note(raw, fields, nid)
                    fresh["at"] = cur.get("at", fresh["at"])
                    if fresh["status"] == "resolved" and cur.get("resolvedBatch") is not None \
                            and fresh.get("resolvedAt") == cur.get("resolvedAt"):
                        fresh["resolvedBatch"] = cur["resolvedBatch"]   # 同一次处理：保留批次号
                    cur.clear()
                    cur.update(fresh)
                    updated += 1
                else:
                    skipped += 1
                continue
            if dedup_key(fields) in keys:
                skipped += 1
                continue
            note = _new_note(raw, fields, nid or new_id())
            doc["notes"].append(note)
            by_id[note["id"]] = note
            keys.add(dedup_key(note))
            added += 1
        if added or updated:
            doc = _commit(review_dir, doc, {"op": "import", "by": by, "added": added, "updated": updated, "skipped": skipped})
        return doc, added, skipped, updated


# ---------- 交给 Agent：提交记录 ----------
# 批注是具体修改意见（随写随存）；「提交」是用户表示「这一批好了，交给代理处理」的信号。
# 提交存 handoff.json，状态 pending（等待接收）→ claimed（代理已接收、处理中）→ done（已处理）。
# 任何代理都可以：跑 notes.py wait 阻塞等待，或自己轮询 handoff.json。
HANDOFF_FILE = "handoff.json"
HANDOFF_KEEP = 200   # 处理记录（批次）保留条数
MAX_MSG = 4000


def handoff_load(review_dir: Path) -> dict[str, Any]:
    path = review_dir / HANDOFF_FILE
    if not path.exists():
        return {"version": 1, "seq": 0, "items": []}
    doc = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(doc, dict) or not isinstance(doc.get("items"), list):
        raise ValueError(f"{path} 结构不对：缺少 items 列表")
    doc.setdefault("seq", max((int(i.get("seq", 0)) for i in doc["items"]), default=0))
    return doc


def _handoff_commit(review_dir: Path, doc: dict[str, Any], log: dict[str, Any]) -> dict[str, Any]:
    doc["version"] = 1
    doc["updatedAt"] = now_iso()
    doc["items"] = doc["items"][-HANDOFF_KEEP:]
    _atomic_write(review_dir / HANDOFF_FILE, _dumps(doc, indent=2) + "\n")
    _append_log(review_dir, {"ts": doc["updatedAt"], **log})
    return doc


def batch_resolved(notes: list[dict[str, Any]], item: dict[str, Any], end: str | None = None,
                   next_start: str | None = None) -> list[dict[str, Any]]:
    """这一批里被代理标为已处理的批注。标的时候记了批次号（resolvedBatch）就按批次号；
    没记的（0.3 旧数据、代理没先接收）按时间窗：从接收（没接收就从提交）起，到收尾、且早于下一批被接收，
    由页面以外（代理）标的。下一批的接收时间是开区间上界，相邻两批不会重复计数。"""
    seq = int(item.get("seq", 0) or 0)
    start, stop = _ts(item.get("claimedAt") or item.get("at")), _ts(end) if end else None
    nxt = _ts(next_start) if next_start else None
    out = []
    for n in notes:
        if n.get("status") != "resolved":
            continue
        if n.get("resolvedBatch") is not None:
            if n["resolvedBatch"] == seq:
                out.append(n)
            continue
        t = _ts(n.get("resolvedAt"))
        if n.get("resolvedBy") not in (None, "", "page") and t and t >= start \
                and (stop is None or t <= stop) and (nxt is None or t < nxt):
            out.append(n)
    return out


def _ts(v: Any) -> float:
    """ISO 时间转时间戳（兼容 Z 结尾与带时区偏移的写法）；解析不了返回 0。"""
    try:
        return datetime.fromisoformat(str(v).replace("Z", "+00:00")).timestamp()
    except (TypeError, ValueError):
        return 0.0


def _next_start(items: list[dict[str, Any]], item: dict[str, Any]) -> str | None:
    """下一批被接收的时间（时间窗推算的上界）；下一批还没被接收则没有上界。"""
    k = next((j for j, it in enumerate(items) if it is item), -1)
    return items[k + 1].get("claimedAt") if 0 <= k < len(items) - 1 else None


def _batch_stats(notes: list[dict[str, Any]], item: dict[str, Any], end: str | None,
                 next_start: str | None = None) -> dict[str, Any]:
    got = batch_resolved(notes, item, end, next_start)
    open_now = {n.get("id") for n in notes if n.get("status") != "resolved"}
    return {"resolvedIds": [n.get("id") for n in got], "resolvedCount": len(got),
            "resolvedPages": sorted({int(n.get("page", 0) or 0) for n in got}),
            "leftOpen": sum(1 for x in item.get("openIds") or [] if x in open_now)}


def handoff_state(review_dir: Path, *, full: bool = True) -> dict[str, Any]:
    """给页面和 status 用的摘要：最近一批、各状态计数，以及（full 时）全部批次 history（处理记录，新的在前，
    不含提交时的批注 id 清单）。页面轮询用 full=False，打开处理记录时再取全量。

    已处理的批次带收尾时记下的「这批解决了哪些批注」；0.3 留下的旧批次没有记，按时间窗补算并标 estimated；
    处理中的批次按当前进度实时算（live）。
    """
    try:
        doc = handoff_load(review_dir)
    except ValueError:
        return {"error": "handoff.json 无法解析"}
    items = doc["items"]
    try:
        notes = load(review_dir)["notes"]
    except (OSError, ValueError):
        notes = []
    history = []
    for it in reversed(items if full else items[-1:]):
        h = {k: v for k, v in it.items() if k != "openIds"}
        if it.get("status") == "claimed":
            h.update(_batch_stats(notes, it, None, _next_start(items, it)), live=True)
        elif it.get("status") == "done" and "resolvedIds" not in it:
            h.update(_batch_stats(notes, it, it.get("doneAt"), _next_start(items, it)), estimated=True)
        history.append(h)
    out = {"latest": history[0] if history else None, "total": len(items),
           "pending": sum(i.get("status") == "pending" for i in items),
           "claimed": sum(i.get("status") == "claimed" for i in items),
           "done": sum(i.get("status") == "done" for i in items)}
    if full:
        out["history"] = history
    return out


def handoff_submit(review_dir: Path, message: str = "", *, by: str = "page") -> tuple[dict[str, Any], dict[str, Any], bool]:
    """记一次提交，快照当时的未处理批注。上一次提交还没被代理接收时合并进去（刷新快照与附言），不重复排队。"""
    message = fix_text(message)[:MAX_MSG]
    with locked(review_dir):
        notes = [n for n in load(review_dir)["notes"] if n.get("status") != "resolved"]
        doc = handoff_load(review_dir)
        snap = {"at": now_iso(), "by": by, "message": message, "openCount": len(notes),
                "openIds": [n.get("id") for n in notes],
                "pages": sorted({int(n.get("page", 0) or 0) for n in notes})}
        latest = doc["items"][-1] if doc["items"] else None
        if latest and latest.get("status") == "pending":
            latest.update(snap)
            latest["updates"] = int(latest.get("updates", 0)) + 1
            item, merged = latest, True
        else:
            doc["seq"] = int(doc.get("seq", 0)) + 1
            item, merged = {"seq": doc["seq"], "status": "pending", **snap}, False
            doc["items"].append(item)
        _handoff_commit(review_dir, doc, {"op": "handoff-update" if merged else "handoff", "by": by,
                                          "seq": item["seq"], "openCount": item["openCount"]})
        return doc, item, merged


def handoff_claim(review_dir: Path, *, by: str = "agent") -> dict[str, Any] | None:
    """代理接收最早一个等待中的提交（状态改为处理中）；没有则返回 None。"""
    with locked(review_dir):
        doc = handoff_load(review_dir)
        item = next((i for i in doc["items"] if i.get("status") == "pending"), None)
        if item is None:
            return None
        item.update({"status": "claimed", "claimedAt": now_iso(), "claimedBy": by})
        _handoff_commit(review_dir, doc, {"op": "handoff-claim", "by": by, "seq": item["seq"]})
        return item


def handoff_done(review_dir: Path, *, seq: int | None = None, summary: str = "", by: str = "agent") -> dict[str, Any]:
    """标记提交已处理。不给 seq 时取最近一个处理中的，没有就取最近一个等待中的。"""
    with locked(review_dir):
        doc = handoff_load(review_dir)
        items = doc["items"]
        if seq is not None:
            item = next((i for i in items if int(i.get("seq", 0)) == seq), None)
        else:
            item = next((i for i in reversed(items) if i.get("status") == "claimed"), None) or \
                   next((i for i in reversed(items) if i.get("status") == "pending"), None)
        if item is None:
            raise NotFound(f"handoff#{seq}" if seq is not None else "没有待处理的提交")
        item.update({"status": "done", "doneAt": now_iso(), "doneBy": by, "summary": fix_text(summary)[:MAX_MSG]})
        try:   # 处理记录：这批解决了哪些批注（notes.json 读不了也照样收尾，只是不记统计）
            item.update(_batch_stats(load(review_dir)["notes"], item, item["doneAt"], _next_start(items, item)))
        except (OSError, ValueError):
            pass
        _handoff_commit(review_dir, doc, {"op": "handoff-done", "by": by, "seq": item["seq"],
                                          "resolvedCount": item.get("resolvedCount"), "leftOpen": item.get("leftOpen")})
        return item


# ---------- 重排页序后按页面键校正页码 ----------
def remap_pages(review_dir: Path, old_keys: dict[int, str], new_pages: dict[str, int], new_titles: dict[int, str],
                ambiguous: set[str] | None = None) -> dict[str, Any]:
    """生成对照页时调用：old_keys 是上一版 review.json 的「页码 → 键」，new_pages 是新版的「键 → 页码」。

    - 没有 key 的旧批注，按上一版的页码补上 key（批注是对着上一版写的）；
    - 有 key 且新版里还在：页码改成新页码；
    - key 在新版里找不到：标 orphan，页码不动，等用户或代理确认。
    返回 {"moved": [...], "orphans": [...], "keyed": n}。
    """
    ambiguous = ambiguous or set()
    result: dict[str, Any] = {"moved": [], "orphans": [], "keyed": 0}
    if not (review_dir / NOTES_FILE).exists():
        return result
    with locked(review_dir):
        doc = load(review_dir)
        changed = False
        for n in doc["notes"]:
            page = int(n.get("page", 0) or 0)
            if not n.get("key") and page in old_keys:
                n["key"] = old_keys[page]
                result["keyed"] += 1
                changed = True
            key = n.get("key")
            # 重复的键、由页码生成的键（p3、page-3）都不能用来找回原页：保持不动
            if not key or key in ambiguous or re.fullmatch(r"(?:p|page)?[-_]?\d+", str(key), re.I):
                continue
            if key in new_pages:
                if new_pages[key] != page:
                    result["moved"].append({"id": n.get("id"), "from": page, "to": new_pages[key], "key": key})
                    n["page"] = new_pages[key]
                    n["title"] = new_titles.get(new_pages[key], n.get("title", ""))
                    changed = True
                if n.pop("orphan", None):
                    changed = True
            elif not n.get("orphan"):
                n["orphan"] = True
                result["orphans"].append({"id": n.get("id"), "page": page, "key": key})
                changed = True
            else:
                result["orphans"].append({"id": n.get("id"), "page": page, "key": key})
        if changed:
            _commit(review_dir, doc, {"op": "remap", "by": "build", "moved": len(result["moved"]),
                                      "orphans": len(result["orphans"]), "keyed": result["keyed"]})
    return result
