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
import copy
import json
import math
import os
import re
import secrets
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterator

import docx_review
import fsutil
import ledger
import pptx_reader
import video_review

NOTES_FILE = "notes.json"
LOG_FILE = "notes.jsonl"
LOCK_FILE = ".notes.lock"
SCHEMA_VERSION = 1
STATUSES = ("open", "drafted", "resolved", "held")
# drafted：两段流程（先初改、后复核）里初改已完成、等待复核或用户定稿；页面显示「已初改·待复核」
# held：搁住（暂不交给 Agent 改稿），hold 记原因（分析中 / 结论已变）、heldBy 记引出分析。
#   只能由分析存储在发起分析 / 解除搁住时设置，手动改状态走不到这里；
#   提交快照只收「未处理」，搁住的批注天然不进批次。旧版工具导入时按未知状态退回未处理。
TRAIL_KEEP = 50  # 每条批注保留的处理痕迹条数（谁、哪个阶段、做了什么、理由）
# 页面可写的字段；id / at / status 相关字段由存储层或专门操作维护
EDITABLE = (
    "page",
    "title",
    "target",
    "text",
    "quote",
    "quoteFrom",
    "pin",
    "region",
    "edit",
    "pair",
    "skills",
    "style",
    "anchor",
    "key",
    "orphan",
    "docx",
    "pptx",
    "video",
)
# key：页面键（报告 section 的 data-key），重排页序后据此把批注挪回正确的页；orphan：重建后原页面已不存在
# docx：DOCX 审阅的段落定位（带版本号 v），见 _clean_docx；重建时先按它把批注归到段落现在所在的页，
#   页面键只用于没有段落定位的批注。HTML 报告的批注没有这个字段。
# pptx：PPT 审阅的幻灯片定位（带版本号 v），见 _clean_pptx：幻灯片键 + 有序元素目标 / 文本选区 /
#   归一化点或区域坐标 + 源文件指纹 + 定位来源与只读限制。与 docx 并列、互不影响；
#   PPT 页的页面键就是幻灯片键，重建按页面键找回。HTML / DOCX 批注没有这个字段。
# 报告上的标注：pin = 点 {x, y}，region = 框 {x, y, w, h}，都是相对页面的百分比。页面新写的批注只带其中一种
# （新建、重新标注时两个字段一起发，另一种为 null）；拖动只改被拖的那一种。
# 风格优化的范围跟着标注走：有框 = region（框选处），有点 = pin（标点处），都没有 = page（整页）。
# 0.3 及更早只有风格优化能框选，框存在 style.region 里；读入时挪到顶层 region，不改原来的范围、不删点。
STYLE_SCOPES = ("page", "region", "pin")
# 处理这条批注时需要调用的技能（skill / 工具名），由用户在页面上指定，代理处理前先调用
MAX_SKILLS = 10
SKILLS_FILE = "skills.json"
SKILL_SOURCES_FILE = ".skill-sources.json"
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


class TextLimitError(NoteError):
    """字段过长：保留旧错误文本，提供字段与上限用于界面本地化。"""

    def __init__(self, field: str, limit: int):
        super().__init__(f"{field} 过长（上限 {limit} 字）")
        self.field, self.limit = field, limit


class HandoffError(NoteError):
    """交接业务拒绝：保留旧异常文本，附稳定代码与参数供 CLI 本地化。"""

    def __init__(self, message: str, *, code: str, **params: Any):
        super().__init__(message)
        self.code, self.params = code, params


class AlwaysLimitError(NoteError):
    """常驻技能超过上限。带上限与当前数量，调用方好用当前语言提示。"""

    def __init__(self, limit: int, count: int):
        super().__init__(f"常驻技能最多 {limit} 个（现在 {count} 个）")
        self.limit = limit
        self.count = count


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


def pre_read(value: Any, missing: Any, read: Callable[[], Any]) -> Any:
    """预读参数的取值，让「读一次、多方复用」不改变各函数自己的降级结果：

    - 数据（调用方已经读好的）：直接用；
    - 值为 missing（None 等）：调用方没给，自己读；
    - 传读取时的异常实例：那次读取失败了，重新抛出——由本函数原有的 except 按同一降级结果处理。
    """
    if isinstance(value, Exception):
        raise value
    return read() if value is missing else value


def _require_dir(review_dir: Path) -> None:
    if not review_dir.is_dir():
        raise FileNotFoundError(f"审阅目录不存在：{review_dir}")


@contextlib.contextmanager
def locked(review_dir: Path, *, deadline: float | None = None) -> Iterator[None]:
    _require_dir(review_dir)
    with fsutil.flocked(review_dir / LOCK_FILE, exclusive=True, deadline=deadline):
        yield


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
            _settle_marks(n)  # 0.3 旧数据：框从 style.region 挪到顶层 region（下次写盘时落盘）
    return doc


def _atomic_write(path: Path, text: str) -> None:
    fsutil.atomic_write(path, text, prefix=".notes-")


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
    return (
        s.encode("utf-8", "surrogatepass").decode("utf-8", "replace")
        if any("\ud800" <= c <= "\udfff" for c in s)
        else s
    )


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
    if not all(math.isfinite(t) for t in (x, y, w, h)) or not (
        0 <= x <= 100 and 0 <= y <= 100 and 0 < w <= 100 and 0 < h <= 100
    ):
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
            raise TextLimitError("edit.to", MAX_TEXT)
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


def skills_catalog(review_dir: Path, *, doc: dict[str, Any] | None = None) -> list[dict[str, str]]:
    """审阅目录里的技能目录（代理写入的可用技能，页面据此给出候选）。读不到就返回空。

    doc：调用方已读好的 skills.json（一次请求里只读一次时传入）；不传就自己读。
    """
    return _catalog_items(skills_doc(review_dir).get("skills") if doc is None else doc.get("skills"))


# 常驻技能：对这份审阅的每条批注（按范围）都要先调用的技能，给需要长期约束的代理用。范围：
# all 全部批注 / 报告 / 讲稿（对象含该侧，「报告和讲稿」两侧都算）/ style 风格优化。存在 skills.json 的 always 里。
ALWAYS_SCOPES = ("all", "报告", "讲稿", "style")
ALWAYS_ZH = {"all": "全部批注", "报告": "报告", "讲稿": "讲稿", "style": "风格优化"}
MAX_ALWAYS = 20


def skills_doc(review_dir: Path) -> dict[str, Any]:
    """审阅目录里的技能文档（skills.json）；读不到或结构不对就返回空。"""
    try:
        doc = json.loads((review_dir / SKILLS_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return doc if isinstance(doc, dict) else {}


def _skills_doc_for_write(review_dir: Path) -> dict[str, Any]:
    path = review_dir / SKILLS_FILE
    if not path.exists():
        return {}
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        raise NoteError(f"技能目录无法读取，未覆盖：{e}") from e
    if not isinstance(doc, dict) or (doc.get("skills") is not None and not isinstance(doc.get("skills"), list)):
        raise NoteError("技能目录结构不正确，未覆盖")
    return doc


def _catalog_items(value: Any) -> list[dict[str, str]]:
    if not isinstance(value, list):
        return []
    return [
        {"name": fix_text(x["name"]).strip()[:64], "desc": fix_text(x.get("desc", "")).strip()}
        for x in value
        if isinstance(x, dict) and isinstance(x.get("name"), str) and x["name"].strip()
    ]


def _skill_sources_state(review_dir: Path, current: list[dict[str, str]]) -> dict[str, Any]:
    path = review_dir / SKILL_SOURCES_FILE
    if not path.exists():
        return {"version": 1, "manual": current, "excluded": [], "sources": {}, "generated": current}
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        raise NoteError(f"技能来源记录无法读取，未覆盖：{e}") from e

    def valid_items(items: Any) -> bool:
        return isinstance(items, list) and all(
            isinstance(x, dict) and isinstance(x.get("name"), str) and isinstance(x.get("desc"), str) for x in items
        )

    if (
        not isinstance(state, dict)
        or state.get("version") != 1
        or not valid_items(state.get("manual"))
        or not isinstance(state.get("excluded"), list)
        or not all(isinstance(x, str) for x in state["excluded"])
        or not isinstance(state.get("sources"), dict)
        or not all(isinstance(k, str) and valid_items(v) for k, v in state["sources"].items())
        or not valid_items(state.get("generated"))
    ):
        raise NoteError("技能来源记录结构不正确，未覆盖")
    return state


def _reconcile_skill_edits(state: dict[str, Any], current: list[dict[str, str]]) -> None:
    """保留直接编辑 skills.json 或旧工具写回的改动。"""
    expected = {x["name"]: x["desc"] for x in _catalog_items(state["generated"])}
    actual = {x["name"]: x["desc"] for x in current}
    manual = {x["name"]: x for x in _catalog_items(state["manual"])}
    excluded = {str(x) for x in state["excluded"]}
    for item in current:
        if item["name"] not in expected or expected[item["name"]] != item["desc"]:
            manual[item["name"]] = item
            excluded.discard(item["name"])
    for name in expected.keys() - actual.keys():
        manual.pop(name, None)
        excluded.add(name)
    state["manual"], state["excluded"] = list(manual.values()), sorted(excluded)


def _save_catalog_state(
    review_dir: Path, doc: dict[str, Any], state: dict[str, Any], catalog: list[dict[str, str]]
) -> None:
    state["generated"] = catalog
    state_path = review_dir / SKILL_SOURCES_FILE
    try:
        previous = state_path.read_text(encoding="utf-8")
    except FileNotFoundError:
        previous = None
    _atomic_write(state_path, _dumps(state, indent=2) + "\n")
    try:
        _atomic_write(
            review_dir / SKILLS_FILE,
            _dumps(
                {"version": 1, "skills": catalog, "always": _always_items(doc.get("always"), strict=False)}, indent=2
            )
            + "\n",
        )
    except BaseException as error:
        try:
            if previous is None:
                state_path.unlink(missing_ok=True)
            else:
                _atomic_write(state_path, previous)
        except OSError as restore_error:
            raise NoteError(f"技能目录写入失败，来源记录也无法恢复：{restore_error}") from error
        raise


def skills_merge_scan(
    review_dir: Path, results: list[tuple[str, list[dict[str, str]] | None, bool]]
) -> tuple[list[dict[str, str]], bool]:
    """在锁内合并扫描与最新的手工目录；失败来源沿用上次结果。"""
    with locked(review_dir):
        doc = _skills_doc_for_write(review_dir)
        current = _catalog_items(doc.get("skills"))
        state = _skill_sources_state(review_dir, current)
        _reconcile_skill_edits(state, current)
        previous = state["sources"]
        retained = any(items is None and bool(_catalog_items(previous.get(key))) for key, items, _complete in results)
        sources = {}
        for key, items, complete in results:
            if items is None:
                sources[key] = _catalog_items(previous.get(key))
            elif complete:
                sources[key] = _catalog_items(items)
            else:
                merged = {x["name"]: x for x in _catalog_items(previous.get(key))}
                merged.update({x["name"]: x for x in _catalog_items(items)})
                sources[key] = list(merged.values())
        state["sources"] = sources
        excluded = set(state["excluded"])
        catalog = _catalog_items(state["manual"])
        seen = {x["name"] for x in catalog}
        for key, _items, _complete in results:
            for item in state["sources"][key]:
                if item["name"] not in excluded and item["name"] not in seen:
                    catalog.append(item)
                    seen.add(item["name"])
        _save_catalog_state(review_dir, doc, state, catalog)
        return catalog, retained


def skills_update(
    review_dir: Path,
    *,
    set_to: list[dict[str, str]] | None = None,
    add: list[dict[str, str]] | None = None,
    remove: list[str] | None = None,
    clear: bool = False,
    always_set: Any = None,
    always_add: Any = None,
    always_remove: list[str] | None = None,
    always_clear: bool = False,
) -> list[dict[str, str]]:
    """在锁内修改技能目录并记录手工覆盖及移除。"""
    with locked(review_dir):
        doc = _skills_doc_for_write(review_dir)
        current = _catalog_items(doc.get("skills"))
        state = _skill_sources_state(review_dir, current)
        _reconcile_skill_edits(state, current)
        manual = {x["name"]: x for x in _catalog_items(state["manual"])}
        excluded = {str(x) for x in state["excluded"]}
        if clear or set_to is not None:
            chosen: list[dict[str, str]] = []
            for item in _catalog_items(set_to or []):
                chosen = [x for x in chosen if x["name"] != item["name"]] + [item]
            kept = {x["name"] for x in chosen}
            excluded.update(x["name"] for x in current if x["name"] not in kept)
            manual = {x["name"]: x for x in chosen}
            catalog = chosen
        else:
            catalog = current
        for item in _catalog_items(add or []):
            catalog = [x for x in catalog if x["name"] != item["name"]] + [item]
            manual[item["name"]] = item
            excluded.discard(item["name"])
        gone = {re.sub(r"\s+", " ", fix_text(x)).strip()[:64] for x in remove or [] if fix_text(x).strip()}
        catalog = [x for x in catalog if x["name"] not in gone]
        for name in gone:
            manual.pop(name, None)
        excluded.update(gone)
        excluded.difference_update(x["name"] for x in manual.values())
        state["manual"], state["excluded"] = list(manual.values()), sorted(excluded)
        if always_set is not None or always_add or always_remove or always_clear:
            doc["always"] = _updated_always(
                _always_items(doc.get("always"), strict=False),
                set_to=always_set,
                add=always_add,
                remove=always_remove,
                clear=always_clear,
            )
        _save_catalog_state(review_dir, doc, state, catalog)
        return catalog


_SKILL_SEP = r"[,，、;；\n]+"


def _always_items(v: Any, *, strict: bool) -> list[dict[str, str]]:
    """把常驻技能名单整理成 [{name, scope}]：一项里写了几个名字（逗号、顿号等分隔）就拆成几项同范围的；
    同名同范围去重。strict（页面 / 命令行的输入）遇到坏项报错、超过上限报错；
    非 strict（读文件）跳过坏项、字符串项当「全部批注」，保证一个坏项不让整份名单失效。"""
    if v in (None, "", []):
        return []
    if not isinstance(v, list):
        if strict:
            raise NoteError("常驻技能须为列表")
        return []
    out: list[dict[str, str]] = []
    for it in v:
        if isinstance(it, str) and not strict:
            it = {"name": it, "scope": "all"}
        if not isinstance(it, dict) or not isinstance(it.get("name"), str):
            if strict:
                raise NoteError("常驻技能的每一项须为 {name, scope}")
            continue
        sc = it.get("scope") if it.get("scope") in ALWAYS_SCOPES else "all"
        for nm in re.split(_SKILL_SEP, it["name"]):
            name = re.sub(r"\s+", " ", fix_text(nm)).strip()[:64]
            e = {"name": name, "scope": sc}
            if name and e not in out:
                out.append(e)
    if len(out) > MAX_ALWAYS:
        if strict:
            raise AlwaysLimitError(MAX_ALWAYS, len(out))
        out = out[:MAX_ALWAYS]
    return out


def clean_always(v: Any) -> list[dict[str, str]]:
    """页面 / 命令行给的常驻技能（严格校验）。"""
    return _always_items(v, strict=True)


def _updated_always(
    cur: list[dict[str, str]],
    *,
    set_to: Any = None,
    add: Any = None,
    remove: list[str] | None = None,
    clear: bool = False,
) -> list[dict[str, str]]:
    add_items = clean_always(add or [])
    base_items = clean_always(set_to) if set_to is not None else None
    gone = {re.sub(r"\s+", " ", fix_text(x)).strip()[:64] for r in remove or [] for x in re.split(_SKILL_SEP, r)} - {""}
    out = [] if clear else (base_items if base_items is not None else cur)
    out = [x for x in out if x["name"] not in gone]
    return clean_always(out + [x for x in add_items if x not in out])


def always_skills(review_dir: Path, *, doc: dict[str, Any] | None = None) -> list[dict[str, str]]:
    """doc：调用方已读好的 skills.json（一次请求里只读一次时传入）；不传就自己读。"""
    return _always_items(skills_doc(review_dir).get("always") if doc is None else doc.get("always"), strict=False)


def _write_skills_doc(review_dir: Path, *, always: Any = None, update: Any = None) -> list[dict[str, str]]:
    """在文件锁内读改写 skills.json，技能目录与常驻技能互不覆盖。update(当前常驻) → 新常驻。"""
    with locked(review_dir):  # locked 先确认审阅目录存在
        doc = _skills_doc_for_write(review_dir)
        cur = _always_items(doc.get("always"), strict=False)
        new = update(cur) if update else (always if always is not None else cur)
        cat = [x for x in doc.get("skills", []) if isinstance(x, dict)] if isinstance(doc.get("skills"), list) else []
        _atomic_write(review_dir / SKILLS_FILE, _dumps({"version": 1, "skills": cat, "always": new}, indent=2) + "\n")
        return new


def skills_save(review_dir: Path, items: list[dict[str, str]]) -> None:
    skills_update(review_dir, set_to=items)


def always_save(review_dir: Path, items: Any) -> list[dict[str, str]]:
    return _write_skills_doc(review_dir, always=clean_always(items))


def always_update(
    review_dir: Path, *, set_to: Any = None, add: Any = None, remove: list[str] | None = None, clear: bool = False
) -> list[dict[str, str]]:
    """命令行增删常驻技能：在锁内读最新名单，先删（按名字，所有范围）后加，避免和页面同时保存时丢更新。"""

    def apply(cur: list[dict[str, str]]) -> list[dict[str, str]]:
        return _updated_always(cur, set_to=set_to, add=add, remove=remove, clear=clear)

    return _write_skills_doc(review_dir, update=apply)


def always_for(note: dict[str, Any], always: list[dict[str, str]]) -> list[dict[str, str]]:
    """这条批注适用的常驻技能。"""
    tgt = str(note.get("target") or "")
    is_style = isinstance(note.get("style"), dict) and bool(note["style"].get("scope"))
    hit = {"all": True, "报告": "报告" in tgt, "讲稿": "讲稿" in tgt, "style": is_style}
    return [a for a in always if hit.get(a["scope"])]


def skills_for(note: dict[str, Any], always: list[dict[str, str]]) -> list[tuple[str, str]]:
    """这条批注处理前要调用的全部技能：[(名称, 来源)]，来源为「常驻·范围」或「这条指定」；同名只列一次。"""
    out: list[tuple[str, str]] = []
    for a in always_for(note, always):
        if all(a["name"] != n for n, _ in out):
            out.append((a["name"], f"常驻·{ALWAYS_ZH[a['scope']]}"))
    for nm in note.get("skills") or []:
        if all(nm != n for n, _ in out):
            out.append((nm, "这条指定"))
    return out


# ---------- DOCX 段落定位 ----------
DOCX_KINDS = ("quote", "pin", "region")  # 定位的是引用选区、点标注还是框选
DOCX_VIA = ("selection", "text", "point")  # 定位来源：浏览器选区 / 按文本唯一匹配反查 / 标注坐标处的段落
MAX_DOCX_RAW = 4000  # 更高版本的定位原样保留时的大小上限（JSON 字符数）
_FP_RE = re.compile(r"[0-9a-f]{16,64}")
_DIGEST_RE = re.compile(r"[0-9a-f]{12}")
_MAX_OFFSET = 10_000_000


def _nat(v: Any, name: str) -> int:
    if isinstance(v, bool) or not isinstance(v, int) or not 0 <= v <= _MAX_OFFSET:
        raise NoteError(f"docx.{name} 须为非负整数")
    return v


def _clean_docx_para(v: Any, name: str) -> dict[str, Any]:
    if not isinstance(v, dict):
        raise NoteError(f"docx.{name} 须为对象")
    pid = v.get("id")
    if pid in (None, ""):
        pid = None
    elif not isinstance(pid, str) or not docx_review.PARA_ID.fullmatch(pid):
        raise NoteError(f"docx.{name}.id 不是合法的 paraId")
    else:
        pid = pid.upper()
    if not isinstance(v.get("digest"), str) or not _DIGEST_RE.fullmatch(v["digest"]):
        raise NoteError(f"docx.{name}.digest 不是合法的段落摘要")
    return {"id": pid, "n": _nat(v.get("n"), f"{name}.n"), "digest": v["digest"]}


def _clean_docx(v: Any) -> dict[str, Any] | None:
    """DOCX 段落定位。v1：{"v", "kind", "via", "fp"?, "para": {"id", "n", "digest"}, "start"?, "end"?,
    "endPara"?, "run"?, "changed"?}——fp 是写下定位时源文件的指纹；start / end 是选区在段落内的字符偏移
    （Unicode 码点；跨段落时 end 落在 endPara 里）；run 是文字运行序号，只作提示；changed 表示重建后
    段落文字已变。版本更高的字段读不懂：原样保留（限大小），不清洗、不用于定位。"""
    if v in (None, "", {}):
        return None
    if not isinstance(v, dict):
        raise NoteError("docx 须为对象")
    ver = v.get("v")
    if isinstance(ver, bool) or not isinstance(ver, int) or ver < 1:
        raise NoteError("docx.v 须为正整数")
    if ver > docx_review.ANCHOR_VERSION:
        raw = _dumps(v)
        if len(raw) > MAX_DOCX_RAW:
            raise NoteError("docx 定位过大")
        return json.loads(raw)
    if v.get("kind") not in DOCX_KINDS:
        raise NoteError(f"docx.kind 须为 {DOCX_KINDS} 之一")
    if v.get("via") not in DOCX_VIA:
        raise NoteError(f"docx.via 须为 {DOCX_VIA} 之一")
    out: dict[str, Any] = {"v": ver, "kind": v["kind"], "via": v["via"]}
    if v.get("fp") not in (None, ""):
        if not isinstance(v["fp"], str) or not _FP_RE.fullmatch(v["fp"]):
            raise NoteError("docx.fp 不是合法的源文件指纹")
        out["fp"] = v["fp"]
    out["para"] = _clean_docx_para(v.get("para"), "para")
    for k in ("start", "end"):
        if v.get(k) is not None:
            out[k] = _nat(v[k], k)
    if "end" in out and "start" not in out:
        raise NoteError("docx.end 须与 docx.start 一起给出")
    if v.get("endPara") is not None:
        out["endPara"] = _clean_docx_para(v["endPara"], "endPara")
    elif "end" in out and out["end"] < out["start"]:
        raise NoteError("docx.end 不能小于 docx.start")
    if v.get("run") is not None:
        out["run"] = _nat(v["run"], "run")
    if v.get("changed") is True:
        out["changed"] = True
    return out


# ---------- PPT 幻灯片定位（与 DOCX 段落定位并列的字段，见 GLOSSARY「幻灯片定位」） ----------
PPTX_KINDS = ("page", "elements", "text", "point", "region")  # 定位整页、元素（可多选）、文本选区、点或区域
PPTX_VIA = {
    "page": ("thumb",),
    "elements": ("click",),
    "text": ("selection", "text"),
    "point": ("point",),
    "region": ("region",),
}
# 定位来源：页缩略图选整页 / 元素点选（含多选）/ 浏览器选区 / 按文本唯一匹配反查 / 标注坐标
PPTX_EL_KINDS = ("shape", "group", "connector", "picture", "media", "table", "chart", "smartArt", "ole", "unknown")
PPTX_RO = ("text", "geometry", "style")  # 能力矩阵里声明不可自动回写的方面（只读元素）
MAX_PPTX_ELEMENTS = 32  # 一次批注最多指向的元素目标数
MAX_PPTX_RUNS = 64  # 文本选区保留的样式边界（run 边界）条数
MAX_PPTX_QUOTE = 600  # 文本选区引用原文的长度上限（字符）
MAX_PPTX_RAW = 8000  # 更高版本的定位原样保留时的大小上限（JSON 字符数）
_PPTX_DIGEST_RE = re.compile(r"[0-9a-f]{64}")  # pptx_reader 的摘要一律是 SHA-256 十六进制全串


def usable_pptx_anchor(a: object) -> bool:
    """本版本读得懂的 pptx 定位：v1 且带幻灯片键与 kind。更高版本的原样保留但不参与重定位。"""
    return (
        isinstance(a, dict)
        and not isinstance(a.get("v"), bool)
        and a.get("v") == pptx_reader.ANCHOR_VERSION
        and isinstance(a.get("slide"), str)
        and bool(a["slide"])
        and a.get("kind") in PPTX_KINDS
    )


def _pptx_nat(v: Any, name: str) -> int:
    if isinstance(v, bool) or not isinstance(v, int) or not 0 <= v <= _MAX_OFFSET:
        raise NoteError(f"pptx.{name} 须为非负整数")
    return v


def _pptx_unit(v: Any, name: str) -> float:
    """相对幻灯片画布的归一化坐标分量：0..1 的有限数。"""
    if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) or not 0 <= v <= 1:
        raise NoteError(f"pptx.{name} 须为 0 到 1 之间的归一化坐标")
    return round(float(v), 4)


def _clean_pptx_element(v: Any) -> dict[str, Any]:
    if not isinstance(v, dict):
        raise NoteError("pptx.elements[] 须为对象")
    key, eid, name = v.get("key"), v.get("id"), v.get("name", "")
    if not isinstance(key, str) or not 0 < len(key.strip()) <= 120:
        raise NoteError("pptx.elements[].key 须为元素键")
    if not isinstance(eid, str) or not 0 < len(eid.strip()) <= 120:
        raise NoteError("pptx.elements[].id 须为形状标识")
    if name in (None, ""):
        name = ""
    elif not isinstance(name, str):
        raise NoteError("pptx.elements[].name 须为字符串")
    if v.get("kind") not in PPTX_EL_KINDS:
        raise NoteError(f"pptx.elements[].kind 须为 {PPTX_EL_KINDS} 之一")
    if not isinstance(v.get("digest"), str) or not _PPTX_DIGEST_RE.fullmatch(v["digest"]):
        raise NoteError("pptx.elements[].digest 不是合法的证据摘要")
    out = {
        "key": key.strip(),
        "id": eid.strip(),
        "name": name[:MAX_SHORT],
        "kind": v["kind"],
        "z": _pptx_nat(v.get("z"), "elements[].z"),
        "digest": v["digest"],
    }
    ro = v.get("ro")
    if ro not in (None, "", []):
        if (
            not isinstance(ro, list)
            or not ro
            or len(ro) > len(PPTX_RO)
            or any(x not in PPTX_RO for x in ro)
            or len(set(ro)) != len(ro)
        ):
            raise NoteError(f"pptx.elements[].ro 须为 {PPTX_RO} 的非空子集")
        out["ro"] = ro
    return out


def _clean_pptx_text(v: Any) -> dict[str, Any]:
    if not isinstance(v, dict):
        raise NoteError("pptx.text 须为对象")
    quote = v.get("quote", "")
    if not isinstance(quote, str) or not quote.strip():
        raise NoteError("pptx.text.quote 须为引用原文")
    if not isinstance(v.get("paraDigest"), str) or not _PPTX_DIGEST_RE.fullmatch(v["paraDigest"]):
        raise NoteError("pptx.text.paraDigest 不是合法的段落摘要")
    out = {
        "para": _pptx_nat(v.get("para"), "text.para"),
        "paraDigest": v["paraDigest"],
        "quote": quote[:MAX_PPTX_QUOTE],
    }
    # start / end / runs 成对给出：选区与段内唯一反查带字符偏移与样式边界；
    # 段内重复文字反查只记元素与逻辑段落，不猜偏移
    has_range = any(k in v for k in ("start", "end"))
    if has_range:
        if "start" not in v or "end" not in v:
            raise NoteError("pptx.text.start 与 pptx.text.end 须一起给出")
        start, end = _pptx_nat(v["start"], "text.start"), _pptx_nat(v["end"], "text.end")
        if end < start:
            raise NoteError("pptx.text.end 不能小于 pptx.text.start")
        out["start"], out["end"] = start, end
        runs = v.get("runs")
        if runs in (None, []):
            raise NoteError("pptx.text.runs 须与字符偏移一起给出")
        if not isinstance(runs, list) or len(runs) > MAX_PPTX_RUNS:
            raise NoteError(f"pptx.text.runs 最多 {MAX_PPTX_RUNS} 条")
        cleaned = []
        for r in runs:
            if not isinstance(r, dict):
                raise NoteError("pptx.text.runs[] 须为对象")
            rs, re_ = _pptx_nat(r.get("start"), "text.runs[].start"), _pptx_nat(r.get("end"), "text.runs[].end")
            if re_ < rs:
                raise NoteError("pptx.text.runs[].end 不能小于 start")
            cleaned.append({"start": rs, "end": re_})
        out["runs"] = cleaned
    elif v.get("runs") is not None:
        raise NoteError("pptx.text.runs 须与字符偏移一起给出")
    return out


def _clean_pptx(v: Any) -> dict[str, Any] | None:
    """PPT 幻灯片定位。v1：{"v", "slide", "kind", "via", "fp"?, "elements"?, "text"?, "point"?, "region"?}——
    slide 是持久幻灯片键；elements 是有序元素目标列表（每个带读取层的稳定身份与只读限制），
    多元素批注不可以用一个区域坐标代替目标身份；text 是元素内逻辑段落的 Unicode 码点选区，
    run 边界只用于保留样式，不作为唯一定位；point / region 是相对幻灯片画布的归一化坐标。
    版本更高的字段读不懂：原样保留（限大小），不清洗、不用于定位。"""
    if v in (None, "", {}):
        return None
    if not isinstance(v, dict):
        raise NoteError("pptx 须为对象")
    ver = v.get("v")
    if isinstance(ver, bool) or not isinstance(ver, int) or ver < 1:
        raise NoteError("pptx.v 须为正整数")
    if ver > pptx_reader.ANCHOR_VERSION:
        raw = _dumps(v)
        if len(raw) > MAX_PPTX_RAW:
            raise NoteError("pptx 定位过大")
        return json.loads(raw)
    kind, via = v.get("kind"), v.get("via")
    if kind not in PPTX_KINDS:
        raise NoteError(f"pptx.kind 须为 {PPTX_KINDS} 之一")
    if via not in PPTX_VIA[kind]:
        raise NoteError(f"pptx.via 须为 {PPTX_VIA[kind]} 之一")
    slide = v.get("slide")
    if not isinstance(slide, str) or not 0 < len(slide.strip()) <= 120:
        raise NoteError("pptx.slide 须为幻灯片键")
    out: dict[str, Any] = {"v": ver, "slide": slide.strip(), "kind": kind, "via": via}
    if v.get("fp") not in (None, ""):
        if not isinstance(v["fp"], str) or not _FP_RE.fullmatch(v["fp"]):
            raise NoteError("pptx.fp 不是合法的源文件指纹")
        out["fp"] = v["fp"]
    elements = v.get("elements")
    if elements not in (None, "", []):
        if not isinstance(elements, list) or not 1 <= len(elements) <= MAX_PPTX_ELEMENTS:
            raise NoteError(f"pptx.elements 须为 1 到 {MAX_PPTX_ELEMENTS} 个目标的有序列表")
        out["elements"] = [_clean_pptx_element(e) for e in elements]
    if v.get("text") is not None:
        if kind != "text" or len(out.get("elements") or []) != 1:
            raise NoteError("pptx.text 只属于落在一个元素上的文本定位")
        out["text"] = _clean_pptx_text(v["text"])
    elif kind == "text":
        raise NoteError("pptx.text 须与文本定位一起给出")
    if v.get("point") is not None:
        if kind != "point":
            raise NoteError("pptx.point 只属于点定位")
        p = v["point"]
        if not isinstance(p, dict) or set(p) != {"x", "y"}:
            raise NoteError("pptx.point 须为 {{x, y}}")
        out["point"] = {"x": _pptx_unit(p.get("x"), "point.x"), "y": _pptx_unit(p.get("y"), "point.y")}
    elif kind == "point":
        raise NoteError("pptx.point 须与点定位一起给出")
    if v.get("region") is not None:
        if kind != "region":
            raise NoteError("pptx.region 只属于区域定位")
        rg = v["region"]
        if not isinstance(rg, dict) or set(rg) - {"x", "y", "w", "h"}:
            raise NoteError("pptx.region 须为 {{x, y, w, h}}")
        region = {k: _pptx_unit(rg.get(k), f"region.{k}") for k in ("x", "y", "w", "h")}
        if region["w"] <= 0 or region["h"] <= 0:
            raise NoteError("pptx.region 的宽高须为正")
        if region["x"] + region["w"] > 1.0001 or region["y"] + region["h"] > 1.0001:
            raise NoteError("pptx.region 不能超出幻灯片画布")
        out["region"] = region
    elif kind == "region":
        raise NoteError("pptx.region 须与区域定位一起给出")
    if kind == "page" and "elements" in out:
        raise NoteError("整页定位不携带元素目标列表")
    if kind == "elements" and "elements" not in out:
        raise NoteError("元素定位须携带元素目标列表")
    if kind == "text" and "elements" not in out:
        raise NoteError("文本定位须携带所在元素")
    if v.get("changed") is True:  # 重建后元素文字已变的标记（偏移与指纹仍是旧版本的），与 docx.changed 同义
        out["changed"] = True
    return out


def _clean_style(v: Any) -> dict[str, Any] | None:
    if v in (None, "", {}):
        return None
    if not isinstance(v, dict) or v.get("scope") not in STYLE_SCOPES:
        raise NoteError(f"style.scope 须为 {STYLE_SCOPES} 之一")
    out: dict[str, Any] = {"scope": v["scope"]}
    if v.get("region") not in (None, "", {}):  # 0.3 旧格式：框存在 style.region 里；由 _settle_marks 挪到顶层
        out["region"] = _clean_region(v["region"], "style.region")
    return out


def _clean_trail(v: Any) -> list[dict[str, Any]]:
    """处理痕迹（导入时带过来的）：只留认得的字段，最多 TRAIL_KEEP 条。"""
    out = []
    for e in v if isinstance(v, list) else []:
        if isinstance(e, dict) and isinstance(e.get("action"), str):
            out.append(
                {
                    k: (fix_text(e[k])[:MAX_TEXT] if isinstance(e[k], str) else e[k])
                    for k in ("at", "by", "stage", "batch", "action", "note")
                    if e.get(k) not in (None, "") and isinstance(e[k], (str, int))
                }
            )
    return out[-TRAIL_KEEP:]


def _merge_trails(a: Any, b: Any) -> list[dict[str, Any]]:
    seen, out = set(), []
    for e in _clean_trail(a) + _clean_trail(b):
        k = (e.get("at"), e.get("action"), e.get("by"), e.get("batch"))
        if k not in seen:
            seen.add(k)
            out.append(e)
    out.sort(key=lambda e: _ts(e.get("at")))
    return out[-TRAIL_KEEP:]


def _trail(target: dict[str, Any], /, **entry: Any) -> None:
    """在批注的处理痕迹里追加一条（谁、哪个阶段、第几批、做了什么、理由）。"""
    t = target.get("trail") if isinstance(target.get("trail"), list) else []
    t.append({"at": now_iso(), **{k: v for k, v in entry.items() if v not in (None, "")}})
    target["trail"] = t[-TRAIL_KEEP:]


def _video_int(value: Any, name: str, *, minimum: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise NoteError(f"video.{name} 须为不小于 {minimum} 的整数")
    return value


def _video_number(value: Any, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise NoteError(f"video.{name} 须为有限数字")
    return float(value)


def _clean_video_mark(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict) or value.get("kind") not in ("point", "region"):
        raise NoteError("video.mark 须为归一化点或区域")
    kind = value["kind"]
    names = ("x", "y") if kind == "point" else ("x", "y", "width", "height")
    if set(value) != {"kind", *names}:
        raise NoteError("video.mark 含无法识别的字段")
    mark = {"kind": kind, **{name: _video_number(value[name], f"mark.{name}") for name in names}}
    if any(mark[name] < 0 or mark[name] > 1 for name in ("x", "y")):
        raise NoteError("video.mark 坐标须在 0 至 1 之间")
    if kind == "region":
        if mark["width"] <= 0 or mark["height"] <= 0:
            raise NoteError("video.mark 区域宽高须大于 0")
        if mark["x"] + mark["width"] > 1 or mark["y"] + mark["height"] > 1:
            raise NoteError("video.mark 区域不能超出实际视频画面")
    return mark


def _clean_video(value: Any) -> dict[str, Any] | None:
    if value is None:
        return None
    if not isinstance(value, dict):
        raise NoteError("video 须为定位对象")
    version = value.get("v")
    if isinstance(version, bool) or not isinstance(version, int) or version < 1:
        raise NoteError("video.v 须为正整数")
    if version != 1:
        # 与 DOCX / PPT 定位一致：更高版本原样保留，但不能按本版本的帧规则解释。
        try:
            encoded = _dumps(value, sort_keys=True)
        except (TypeError, ValueError) as exc:
            raise NoteError("video 含无法序列化的内容") from exc
        if len(encoded) > 4000:
            raise NoteError("video 高版本定位过长")
        return json.loads(encoded)
    if not isinstance(value.get("versionId"), str) or not re.fullmatch(r"V[1-9][0-9]*", value["versionId"]):
        raise NoteError("video.versionId 无效")
    fps = _video_number(value.get("fps"), "fps")
    if fps <= 0:
        raise NoteError("video.fps 须大于 0")
    timebase = value.get("timebase")
    if not isinstance(timebase, dict) or set(timebase) != {"num", "den"}:
        raise NoteError("video.timebase 须为 {num, den}")
    num = _video_int(timebase.get("num"), "timebase.num", minimum=1)
    den = _video_int(timebase.get("den"), "timebase.den", minimum=1)
    if not math.isclose(fps, den / num, rel_tol=1e-9, abs_tol=1e-9):
        raise NoteError("video.fps 与 timebase 不一致")
    frames = _video_int(value.get("frames"), "frames", minimum=1)
    width = _video_int(value.get("videoWidth"), "videoWidth", minimum=1)
    height = _video_int(value.get("videoHeight"), "videoHeight", minimum=1)
    has_frame = "frame" in value
    has_start, has_end = "startFrame" in value, "endFrame" in value
    if has_frame == (has_start or has_end) or has_start != has_end:
        raise NoteError("video 须恰好使用 frame 或 startFrame/endFrame 其中一种定位")
    out = {
        "v": 1,
        "versionId": value["versionId"],
        "fps": fps,
        "timebase": {"num": num, "den": den},
        "frames": frames,
        "videoWidth": width,
        "videoHeight": height,
    }
    if has_frame:
        frame = _video_int(value.get("frame"), "frame")
        if frame >= frames:
            raise NoteError("video.frame 超出已登记帧数")
        out["frame"] = frame
    else:
        start = _video_int(value.get("startFrame"), "startFrame")
        end = _video_int(value.get("endFrame"), "endFrame")
        if start >= end or end > frames:
            raise NoteError("video 区间须满足 0 <= startFrame < endFrame <= frames")
        out["startFrame"], out["endFrame"] = start, end
    if value.get("mark") is not None:
        out["mark"] = _clean_video_mark(value["mark"])
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
        elif k == "docx":
            v = _clean_docx(v)
        elif k == "pptx":
            v = _clean_pptx(v)
        elif k == "video":
            v = _clean_video(v)
        else:
            v = fix_text(v)
            if k in ("text", "quote"):
                if len(v) > MAX_TEXT:
                    raise TextLimitError(k, MAX_TEXT)
            else:  # 标题、对象等短字段：超长就截断，不拒绝（否则这一页永远加不上批注）
                v = v[:MAX_SHORT]
        out[k] = v
    st = out.get("style")
    if isinstance(st, dict) and "region" in st:  # 0.3 页面的改动（含离线队列补传）：框写在 style.region 里
        reg = st.pop("region")
        out.setdefault("region", reg)
    if not partial:
        if "page" not in out:
            raise NoteError("缺少 page")
        if not out.get("text", "").strip():
            raise NoteError("批注内容为空")
        if isinstance(out.get("video"), dict) and out["video"].get("v") == 1 and out["page"] != 1:
            raise NoteError("视频批注的兼容 page 固定为 1")
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
    note = {
        "id": nid,
        "title": "",
        "target": "",
        "quote": "",
        "quoteFrom": "",
        "pin": None,
        **fields,
        "status": raw.get("status") if raw.get("status") in STATUSES else "open",
        "at": _clean_time(raw.get("at")) or now_iso(),
    }
    if note["status"] in ("resolved", "drafted"):
        for k in ("resolution", "resolvedAt", "resolvedBy", "draftedAt", "draftedBy"):
            if raw.get(k) not in (None, ""):
                note[k] = str(raw[k])[:MAX_TEXT]
    for k, st in (("draftBatch", "drafted"), ("resolvedBatch", "resolved")):
        v = raw.get(k)
        if note["status"] == st and isinstance(v, int) and not isinstance(v, bool) and v > 0:
            note[k] = v
    trail = _clean_trail(raw.get("trail"))
    if trail:
        note["trail"] = trail
    if _clean_time(raw.get("updatedAt")):
        note["updatedAt"] = raw["updatedAt"]
    _settle_marks(note)
    if note.get("docx") is None:
        note.pop("docx", None)
    if note.get("pptx") is None:
        note.pop("pptx", None)
    if note.get("video") is None:
        note.pop("video", None)
    return note


# _new_note 能写出的全部字段。导入时表外的键是认不出的扩展字段（更高版本写入的），
# 原样保留、不清洗：本版本降级读写也不能把将来的数据丢掉。
NOTE_FIELDS = frozenset(EDITABLE) | {
    "id",
    "at",
    "status",
    "updatedAt",
    "trail",
    "resolution",
    "resolvedAt",
    "resolvedBy",
    "draftedAt",
    "draftedBy",
    "draftBatch",
    "resolvedBatch",
    "videoReview",
}


def _extra_fields(source: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in source.items() if k not in NOTE_FIELDS}


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


def update(
    review_dir: Path, note_id: str, data: dict[str, Any], *, by: str = "page"
) -> tuple[dict[str, Any], dict[str, Any]]:
    fields = _clean_fields(data, partial=True)
    if "text" in fields and not fields["text"].strip():
        raise NoteError("批注内容为空")
    with locked(review_dir):
        doc = load(review_dir)
        note = _find(doc, note_id)
        ed = note.get("edit") if isinstance(note.get("edit"), dict) else None
        if ed and "text" in fields and "edit" not in fields:  # 旧版页面只改 text：同步到快捷修改的结构上
            if ed.get("op") == "replace" and fields["text"].startswith(REPLACE_PREFIX):
                fields["edit"] = {**ed, "to": fields["text"][len(REPLACE_PREFIX) :]}
            elif fields["text"] not in (DELETE_TEXT,) and not fields["text"].startswith(REPLACE_PREFIX):
                fields["edit"] = None  # 改成了普通意见
        # 人工确认归属（去掉 orphan）或挪到别的页：以这次决定为准。本版本读得懂的段落定位不再保留，
        # 否则下次重建又会按段落把批注挪回去或重新标为孤立；之后这条批注按页面键走。
        moved = "page" in fields and fields["page"] != note.get("page")
        if (
            "docx" not in fields
            and (moved or fields.get("orphan") is False)
            and docx_review.usable_anchor(note.get("docx"))
        ):
            fields["docx"] = None
        if "pptx" not in fields and (moved or fields.get("orphan") is False) and usable_pptx_anchor(note.get("pptx")):
            fields["pptx"] = None
        if isinstance(fields.get("video"), dict) and fields["video"].get("v") == 1:
            final_page = fields.get("page", note.get("page"))
            if final_page != 1:
                raise NoteError("视频批注的兼容 page 固定为 1")
        current_video = note.get("video")
        if isinstance(current_video, dict):
            if "video" in fields and fields["video"] != current_video:
                raise NoteError("video 原版本定位不可变")
            fields.pop("video", None)
            if "page" in fields and fields["page"] != 1:
                raise NoteError("视频批注不能移出兼容 page=1")
        note.update(fields)
        _settle_marks(note, fields)
        if note.get("edit") is None:
            note.pop("edit", None)
        if note.get("docx") is None:
            note.pop("docx", None)
        if note.get("pptx") is None:
            note.pop("pptx", None)
        if note.get("video") is None:
            note.pop("video", None)
        note["updatedAt"] = now_iso()
        return _commit(review_dir, doc, {"op": "update", "by": by, "id": note_id, "fields": fields}), note


RESOLVE_FIELDS = ("resolvedAt", "resolvedBy", "resolution", "resolvedBatch")
DRAFT_FIELDS = ("draftedAt", "draftedBy", "draftBatch")


IN_FLIGHT = ("pending", "claimed", "review", "reviewing")  # 已交给 Agent、还没停下或定稿的批次


def _items(review_dir: Path) -> list[dict[str, Any]]:
    try:
        return handoff_load(review_dir)["items"]
    except (OSError, ValueError):
        return []


def _batch_for(items: list[dict[str, Any]], note: dict[str, Any], seq: int | None = None) -> dict[str, Any] | None:
    """这条批注此刻归哪一批处理。指定 seq 就用那一批（须在途）；否则按批注的归属找，而不是猜「最新一批」：
    待复核的初改 → 它的初改批次；提交时包含它的在途批次（复核中 > 处理中 > 等待复核 > 等待接收，同级取最早）；
    都没有、且恰好只有一批在处理中 / 复核中 → 那一批。"""
    if seq is not None:
        it = _find_batch(items, seq)
        if it.get("status") not in IN_FLIGHT:
            raise HandoffError(
                f"第 {seq} 批不在处理中（{it.get('status')}）",
                code="batch-not-active",
                seq=seq,
                status=it.get("status"),
            )
        return it
    db = note.get("draftBatch")
    if note.get("status") == "drafted" and db is not None:
        it = next((i for i in items if int(i.get("seq", 0)) == db and i.get("status") in IN_FLIGHT), None)
        if it:
            return it
    order = {"reviewing": 0, "claimed": 1, "review": 2, "pending": 3}
    cands = [i for i in items if i.get("status") in order and note.get("id") in (i.get("openIds") or [])]
    if cands:
        return min(cands, key=lambda i: (order[i["status"]], int(i.get("seq", 0))))
    active = [i for i in items if i.get("status") in ("claimed", "reviewing")]
    return active[0] if len(active) == 1 else None


def two_stage(item: dict[str, Any] | None) -> bool:
    """这批是否走两段（先初改、后复核）：用户要求停在待复核，或允许派发子代理（可能由子代理接力初改与复核）。"""
    return bool(item) and (item.get("target") == "review" or bool(item.get("allowSubagents")))


def _stage_of(item: dict[str, Any] | None) -> str | None:
    if not item:
        return None
    return "review" if item.get("status") in ("review", "reviewing") else "draft"


def _mark_resolved(note: dict[str, Any], *, by: str, resolution: str = "", batch: int | None = None) -> None:
    for k in DRAFT_FIELDS[:2]:
        note.pop(k, None)
    note.pop("resolvedBatch", None)
    note.update(status="resolved", resolvedAt=now_iso(), resolvedBy=by)
    if resolution:
        note["resolution"] = fix_text(resolution)[:MAX_TEXT]
    if batch is not None:
        note["resolvedBatch"] = int(batch)


def _is_video_review(manifest: dict[str, Any] | None = None, note: dict[str, Any] | None = None) -> bool:
    if isinstance(manifest, dict):
        fmt = manifest.get("format")
        if (isinstance(fmt, dict) and fmt.get("report") == "video") or isinstance(manifest.get("video"), dict):
            return True
    if isinstance(note, dict) and (isinstance(note.get("video"), dict) or isinstance(note.get("videoReview"), dict)):
        return True
    return False


def _video_batch_seq(note: dict[str, Any], vr: dict[str, Any] | None = None) -> int | None:
    vr_obj = (
        vr if isinstance(vr, dict) else (note.get("videoReview") if isinstance(note.get("videoReview"), dict) else None)
    )
    for raw in (
        vr_obj.get("decisionBatch") if vr_obj else None,
        vr_obj.get("batch") if vr_obj else None,
        note.get("draftBatch"),
        note.get("resolvedBatch"),
    ):
        if isinstance(raw, int) and not isinstance(raw, bool) and raw >= 1:
            return raw
    return None


def _record_video_decision_history(
    vr: dict[str, Any],
    *,
    action: str,
    decision: str,
    state: str,
    by: str,
    now: str,
    batch: int | None,
    reason: str = "",
    reply: str = "",
) -> dict[str, Any]:
    history = list(vr.get("history") or []) if isinstance(vr.get("history"), list) else []
    entry: dict[str, Any] = {
        "source": "decision",
        "action": action,
        "decision": decision,
        "state": state,
        "at": now,
        "by": by,
    }
    if batch is not None:
        entry["batch"] = batch
    if vr.get("versionId"):
        entry["versionId"] = vr["versionId"]
    if isinstance(vr.get("target"), dict):
        entry["target"] = copy.deepcopy(vr["target"])
    if action == "approve" and vr.get("summary"):
        entry["summary"] = vr["summary"]
    if reason:
        entry["returnReason"] = reason
    if reply:
        entry["reply"] = reply
    if action == "reply" and vr.get("question"):
        entry["question"] = vr["question"]
    history.append(entry)
    vr["history"] = history[-VIDEO_HISTORY_KEEP:]
    return entry


def _record_video_approval(note: dict[str, Any], *, by: str, batch: int | None, now: str) -> None:
    vr = note.get("videoReview")
    if not isinstance(vr, dict):
        return
    batch_seq = batch if batch is not None else _video_batch_seq(note, vr)
    vr["state"] = "approved"
    vr["decision"] = "approved"
    if batch_seq is not None:
        vr["decisionBatch"] = batch_seq
    vr["decidedAt"] = now
    vr["decidedBy"] = by
    _record_video_decision_history(
        vr,
        action="approve",
        decision="approved",
        state="approved",
        by=by,
        now=now,
        batch=batch_seq,
    )


def _mark_open(
    note: dict[str, Any],
    *,
    return_reason: str = "",
    by: str = "page",
    batch: int | None = None,
) -> None:
    prev = note.get("status", "open")
    vr = note.get("videoReview")
    if isinstance(vr, dict) and (prev in ("drafted", "resolved") or vr.get("state") == "pending_review"):
        now = now_iso()
        batch_seq = batch if batch is not None else _video_batch_seq(note, vr)
        clean_reason = fix_text(return_reason).strip()[:MAX_TEXT] if return_reason else ""
        vr["state"] = "returned"
        vr["decision"] = "returned"
        if batch_seq is not None:
            vr["decisionBatch"] = batch_seq
        vr["decidedAt"] = now
        vr["decidedBy"] = by
        if clean_reason:
            vr["returnReason"] = clean_reason
        _record_video_decision_history(
            vr,
            action="return",
            decision="returned",
            state="returned",
            by=by,
            now=now,
            batch=batch_seq,
            reason=clean_reason,
        )
    note["status"] = "open"
    for k in RESOLVE_FIELDS + DRAFT_FIELDS:
        note.pop(k, None)


def set_status(
    review_dir: Path,
    note_id: str,
    status: str,
    *,
    resolution: str = "",
    by: str = "page",
    seq: int | None = None,
    authorize: Callable[[], None] | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """标已处理 / 重新打开。代理在两段流程的初改阶段标已处理时，记为「已初改·待复核」（drafted），
    复核通过或用户定稿后才算已处理。每次都在批注的处理痕迹里追加一条。"""
    if status not in STATUSES:
        raise NoteError(f"status 须为 {STATUSES} 之一")
    if status == "held":
        raise NoteError("「搁住」由分析发起时自动设置，不能手动标记；解除走分析的解除入口")
    if status == "drafted" and by == "page":
        raise NoteError("「已初改·待复核」只能由代理在初改时标记")
    with locked(review_dir):
        if authorize is not None:
            authorize()
        manifest = ledger.read_manifest(review_dir)
        doc = load(review_dir)
        note = _find(doc, note_id)
        prev = note.get("status", "open")
        if prev == "held":
            raise HandoffError(
                f"{note_id} 已被分析搁住（{note.get('hold')}），不在改稿流程里；解除搁住或确认结论转出，都走分析的操作",
                code="note-held",
                id=note_id,
            )
        is_video = _is_video_review(manifest, note)
        if status == "resolved" and is_video:
            if by != "page":
                raise HandoffError(
                    "视频批注只能由审阅人在页面复核通过，Agent 不能直接标为已处理",
                    code="video-manual-approval-required",
                    id=note_id,
                )
            if prev == "resolved":
                return doc, note
            ready, reason = video_review.review_readiness(note, manifest, review_dir=review_dir)
            if not ready:
                raise HandoffError(
                    f"视频批注 {note_id} 尚未满足人工复核通过条件（{reason}）",
                    code="video-not-ready",
                    id=note_id,
                    reason=reason,
                )
        if status == "open" and is_video and prev == "resolved":
            vr = note.get("videoReview")
            if isinstance(vr, dict) and vr.get("decision") == "approved":
                raise HandoffError(
                    f"视频批注 {note_id} 已有人工通过决定，不能重新打开或覆盖",
                    code="note-already-decided",
                    id=note_id,
                )
        items = _items(review_dir) if by != "page" else []
        cur = _batch_for(items, note, seq) if by != "page" else None
        db = note.get("draftBatch")
        if (
            cur
            and prev == "drafted"
            and db is not None
            and db != int(cur.get("seq", 0))
            and any(int(i.get("seq", 0)) == db and i.get("status") != "done" for i in items)
        ):
            raise HandoffError(
                f"{note_id} 是第 {db} 批的初改，在等复核或用户定稿：不要在第 {cur['seq']} 批里处理它",
                code="other-draft-batch",
                id=note_id,
                draft=db,
                seq=cur["seq"],
            )
        stage, seq = _stage_of(cur), (int(cur.get("seq", 0)) if cur else None)
        if status == "resolved" and stage == "draft" and two_stage(cur):
            status = "drafted"
        trail_batch = seq
        if status == "drafted":
            for k in RESOLVE_FIELDS:
                note.pop(k, None)
            note.update(status="drafted", draftedAt=now_iso(), draftedBy=by)
            if resolution:
                note["resolution"] = fix_text(resolution)[:MAX_TEXT]
            if seq is not None:
                note["draftBatch"] = seq
            action = "drafted"
        elif status == "resolved":
            video_batch = _video_batch_seq(note) if (is_video and seq is None) else seq
            _mark_resolved(note, by=by, resolution=resolution, batch=video_batch)
            if is_video and isinstance(note.get("videoReview"), dict):
                _record_video_approval(note, by=by, batch=video_batch, now=note["resolvedAt"])
            trail_batch = video_batch
            action = "accepted" if (by == "page" and prev == "drafted") else "resolved"
        else:
            video_batch = _video_batch_seq(note) if (is_video and seq is None) else seq
            _mark_open(note, return_reason=resolution, by=by, batch=video_batch)
            trail_batch = video_batch
            action = "reopened"
        _trail(
            note,
            by=by,
            stage=stage or ("page" if by == "page" else None),
            batch=trail_batch,
            action=action,
            note=fix_text(resolution)[:MAX_TEXT] if resolution else None,
        )
        op = {"resolved": "resolve", "drafted": "draft"}.get(status, "reopen")
        return _commit(review_dir, doc, {"op": op, "by": by, "id": note_id, "resolution": resolution}), note


REVIEW_VERDICTS = ("ok", "revise", "return")


def review(
    review_dir: Path,
    note_id: str,
    verdict: str,
    *,
    note: str = "",
    by: str = "agent",
    seq: int | None = None,
    authorize: Callable[[], None] | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """复核结论：ok 通过（定为已处理）、revise 复核者直接再改（写明改了什么）、return 退回给用户拍板（重新打开，写明原因）。"""
    if verdict not in REVIEW_VERDICTS:
        raise NoteError(f"复核结论须为 {REVIEW_VERDICTS} 之一")
    text = fix_text(note).strip()[:MAX_TEXT]
    if verdict != "ok" and not text:
        raise HandoffError("再改 / 退回要写明理由", code="review-reason-required")
    with locked(review_dir):
        if authorize is not None:
            authorize()
        manifest = ledger.read_manifest(review_dir)
        doc = load(review_dir)
        n = _find(doc, note_id)
        if verdict != "return" and _is_video_review(manifest, n) and by != "page":
            raise HandoffError(
                "视频批注只能由审阅人在页面复核通过，不能由 Agent 复核直接通过",
                code="video-manual-approval-required",
                id=note_id,
            )
        if n.get("status", "open") == "open":
            raise HandoffError(
                f"{note_id} 还没有被处理过（未处理），没有可复核的改动：直接改完用 notes.py resolve",
                code="note-not-drafted",
                id=note_id,
            )
        items = _items(review_dir)
        if seq is None:  # 按批注归属找复核批次：初改 → 它的初改批次；已定的 → 本批复核时定的那一批
            seq = n.get("draftBatch") if n.get("status") == "drafted" else n.get("resolvedBatch")
        cur = next((i for i in items if seq is not None and int(i.get("seq", 0)) == seq), None)
        if not cur or cur.get("status") != "reviewing":
            raise HandoffError(
                f"{note_id} 不在正在复核的批次里"
                + (f"（第 {seq} 批现在是「{cur.get('status')}」）" if cur else "")
                + "：先 notes.py claim --stage review 接收复核，复核只针对那一批的初改",
                code="note-not-reviewing",
                id=note_id,
                seq=seq,
                status=cur.get("status") if cur else None,
            )
        if not (
            (n.get("status") == "drafted" and n.get("draftBatch") == seq)
            or (n.get("status") == "resolved" and n.get("resolvedBatch") == seq)
        ):
            raise HandoffError(
                f"{note_id} 不属于第 {seq} 批的复核范围", code="note-outside-review", id=note_id, seq=seq
            )
        if verdict == "return":
            _mark_open(n, return_reason=text)
        else:
            _mark_resolved(n, by=by, resolution=text if verdict == "revise" else "", batch=seq)
            if verdict == "ok" and not n.get("resolution"):
                n.pop("resolution", None)
        _trail(
            n,
            by=by,
            stage="review",
            batch=seq,
            action={"ok": "approved", "revise": "revised", "return": "returned"}[verdict],
            note=text or None,
        )
        return _commit(review_dir, doc, {"op": "review", "by": by, "id": note_id, "verdict": verdict, "note": text}), n


def delete(review_dir: Path, note_id: str, *, by: str = "page") -> dict[str, Any]:
    with locked(review_dir):
        doc = load(review_dir)
        note = _find(doc, note_id)
        doc["notes"] = [n for n in doc["notes"] if n.get("id") != note_id]
        return _commit(review_dir, doc, {"op": "delete", "by": by, "id": note_id, "note": note})


def dedup_key(n: dict[str, Any]) -> tuple:
    """内容相同的判定：页码、正文、引用、标注及 DOCX / PPT 定位相同。同一页不同位置（不同元素）
    写同样的话不算重复——引用相同不等于目标相同。"""
    pin = n.get("pin") or None
    pin_t = (round(float(pin["x"]), 1), round(float(pin["y"]), 1)) if isinstance(pin, dict) and "x" in pin else None
    rg = region_of(n)
    rg_t = tuple(round(float(rg.get(k, 0) or 0), 1) for k in ("x", "y", "w", "h")) if rg else None
    st_t = bool(isinstance(n.get("style"), dict) and n["style"].get("scope"))
    docx_t = _dumps(n["docx"], sort_keys=True) if isinstance(n.get("docx"), dict) else None
    pptx = n.get("pptx")
    if isinstance(pptx, dict) and pptx.get("v") == 1 and isinstance(pptx.get("elements"), list):
        # 展示名不属于元素身份，Markdown 也不携带；未知版本仍按完整内容比较。
        pptx = {
            **pptx,
            "elements": [
                {k: v for k, v in e.items() if k != "name"} if isinstance(e, dict) else e for e in pptx["elements"]
            ],
        }
    pptx_t = _dumps(pptx, sort_keys=True) if isinstance(pptx, dict) else None
    video_t = _dumps(n["video"], sort_keys=True) if isinstance(n.get("video"), dict) else None
    return (
        int(n.get("page", 0)),
        str(n.get("target", "")).strip(),
        str(n.get("text", "")).strip(),
        str(n.get("quote", "")).strip(),
        pin_t,
        rg_t,
        st_t,
        docx_t,
        pptx_t,
        video_t,
    )


def _stamp(n: dict[str, Any]) -> float:
    """批注最后一次变化的时间（updatedAt / resolvedAt / draftedAt / at / 最后一条痕迹中最晚的），解析失败当作 0。"""
    best = 0.0
    trail = n.get("trail") if isinstance(n.get("trail"), list) else []
    last_t = trail[-1].get("at") if trail and isinstance(trail[-1], dict) else None
    for k, v in [(k, n.get(k)) for k in ("updatedAt", "resolvedAt", "draftedAt", "at")] + [("trail", last_t)]:
        if not isinstance(v, str):
            continue
        try:
            dt = datetime.fromisoformat(v.replace("Z", "+00:00"))
            best = max(best, (dt if dt.tzinfo else dt.astimezone()).timestamp())
        except ValueError:
            continue
    return best


def _reconcile_edit(raw: dict[str, Any]) -> None:
    """导入时以用户看得见的正文为准：快捷修改的结构（edit）从正文重新推导。

    导出里的 text 与 edit 本就是一体的（正文就是「删除引用的这段文字」或「改为：…」），
    用户按导出改正文时，只改得到正文；不同步的话结构里会留着改之前的内容，
    之后照结构落笔就写回了旧文字。与 update() 对旧版页面的处理同一套规则。
    """
    ed = raw.get("edit")
    text = raw.get("text")
    if not isinstance(ed, dict) or not isinstance(text, str):
        return
    if ed.get("op") not in ("delete", "replace") or not isinstance(ed.get("from"), str):
        return
    if text == DELETE_TEXT:
        raw["edit"] = {"op": "delete", "from": ed["from"]}
    elif text.startswith(REPLACE_PREFIX):
        raw["edit"] = {"op": "replace", "from": ed["from"], "to": text[len(REPLACE_PREFIX) :]}
    else:
        raw.pop("edit", None)


def import_notes(
    review_dir: Path, items: list[dict[str, Any]], *, by: str = "import"
) -> tuple[dict[str, Any], int, int, int]:
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
            _reconcile_edit(raw)
            fields = _clean_fields(raw, partial=False)
            nid = raw.get("id") if valid_id(raw.get("id")) else None
            if nid and nid in by_id:
                cur = by_id[nid]
                if "videoReview" in raw and raw["videoReview"] != cur.get("videoReview"):
                    raise NoteError("导入不能改变已有批注的 videoReview 处理记录")
                if _stamp(raw) > _stamp(cur):
                    fresh = _new_note(raw, fields, nid)
                    fresh["at"] = cur.get("at", fresh["at"])
                    merged_trail = _merge_trails(cur.get("trail"), fresh.get("trail"))  # 痕迹只追加：合并去重
                    if merged_trail:
                        fresh["trail"] = merged_trail
                    if (
                        fresh["status"] == "resolved"
                        and cur.get("resolvedBatch") is not None
                        and fresh.get("resolvedAt") == cur.get("resolvedAt")
                    ):
                        fresh["resolvedBatch"] = cur["resolvedBatch"]  # 同一次处理：保留批次号
                    fresh.update({**_extra_fields(cur), **_extra_fields(raw)})  # 扩展字段：库里的保底，导入方的覆盖
                    if isinstance(cur.get("video"), dict) and fresh.get("video") != cur["video"]:
                        raise NoteError("导入不能改变已有批注的 video 原版本定位")
                    if isinstance(cur.get("videoReview"), dict):
                        fresh["videoReview"] = copy.deepcopy(cur["videoReview"])
                    cur.clear()
                    cur.update(fresh)
                    updated += 1
                else:
                    skipped += 1
                continue
            if dedup_key(fields) in keys:
                skipped += 1
                continue
            note = {**_extra_fields(raw), **_new_note(raw, fields, nid or new_id())}
            doc["notes"].append(note)
            by_id[note["id"]] = note
            keys.add(dedup_key(note))
            added += 1
        if added or updated:
            doc = _commit(
                review_dir, doc, {"op": "import", "by": by, "added": added, "updated": updated, "skipped": skipped}
            )
        return doc, added, skipped, updated


# ---------- 交给 Agent：提交记录 ----------
# 批注是具体修改意见（随写随存）；「提交」是用户表示「这一批好了，交给代理处理」的信号。
# 提交存 handoff.json，状态 pending（等待接收）→ claimed（代理已接收、处理中）→ done（已处理）。
# 任何代理都可以：跑 notes.py wait 阻塞等待，或自己轮询 handoff.json。
HANDOFF_FILE = "handoff.json"
HANDOFF_KEEP = 200  # 处理记录（批次）保留条数
MAX_MSG = 4000
FULL_CHECK_TEXT = "全量勾稽：逐页检查整份报告和讲稿的口径、数据及前后引用；有依据的同步修正，不确定或未检查的部分要说明"


def full_check_message(message: str) -> str:
    return message + ("\n\n" if message else "") + "【本批提交选项】" + FULL_CHECK_TEXT


def handoff_user_message(item: dict[str, Any]) -> str:
    """新版保留原始附言；旧服务若更新了 message，以最新的 message 为准。"""
    text = item.get("userMessage")
    message = str(item.get("message") or "")
    expected = full_check_message(text) if isinstance(text, str) and item.get("fullCheck") is True else text
    return text if isinstance(text, str) and message == expected else message


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


def batch_resolved(
    notes: list[dict[str, Any]], item: dict[str, Any], end: str | None = None, next_start: str | None = None
) -> list[dict[str, Any]]:
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
        if (
            n.get("resolvedBy") not in (None, "", "page")
            and t
            and t >= start
            and (stop is None or t <= stop)
            and (nxt is None or t < nxt)
        ):
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


def _open_ids(notes: list[dict[str, Any]]) -> set[Any]:
    return {n.get("id") for n in notes if n.get("status", "open") == "open"}


def _video_batch_stats(
    notes: list[dict[str, Any]], item: dict[str, Any], in_flight: set[Any] | None = None
) -> dict[str, Any]:
    seq = int(item.get("seq", 0) or 0)
    by_id = {n.get("id"): n for n in notes if isinstance(n, dict)}
    open_ids = [nid for nid in (item.get("openIds") or []) if nid in by_id]
    active_flight = (
        in_flight if in_flight is not None else (set(open_ids) if item.get("status") in IN_FLIGHT else set())
    )
    pending_review: list[str] = []
    needs_reply: list[str] = []
    needs_relocate: list[str] = []
    missing_evidence: list[str] = []
    failed: list[str] = []
    returned: list[str] = []
    approved: list[str] = []
    for nid in open_ids:
        n = by_id[nid]
        vr = n.get("videoReview") if isinstance(n.get("videoReview"), dict) else None
        st = n.get("status", "open")
        if st == "resolved" and n.get("resolvedBatch") == seq:
            approved.append(nid)
        elif st == "drafted" and n.get("draftBatch") == seq and (vr is None or vr.get("state") == "pending_review"):
            pending_review.append(nid)
        elif vr is not None and vr.get("batch") == seq:
            vst = vr.get("state")
            if vst == "needs_reply":
                needs_reply.append(nid)
            elif vst == "needs_relocate":
                needs_relocate.append(nid)
            elif vst == "missing_evidence":
                missing_evidence.append(nid)
            elif vst == "failed":
                failed.append(nid)
            elif vst == "returned":
                returned.append(nid)
    unsubmitted = [
        n["id"]
        for n in notes
        if isinstance(n, dict)
        and n.get("id")
        and n.get("status", "open") == "open"
        and not isinstance(n.get("videoReview"), dict)
        and n["id"] not in active_flight
    ]
    return {
        "pendingReviewIds": pending_review,
        "pendingReviewCount": len(pending_review),
        "needsReplyIds": needs_reply,
        "needsReplyCount": len(needs_reply),
        "needsRelocateIds": needs_relocate,
        "needsRelocateCount": len(needs_relocate),
        "missingEvidenceIds": missing_evidence,
        "missingEvidenceCount": len(missing_evidence),
        "failedIds": failed,
        "failedCount": len(failed),
        "returnedIds": returned,
        "returnedCount": len(returned),
        "approvedIds": approved,
        "approvedCount": len(approved),
        "unsubmittedIds": unsubmitted,
        "unsubmittedCount": len(unsubmitted),
    }


def _batch_stats(
    notes: list[dict[str, Any]],
    item: dict[str, Any],
    end: str | None,
    next_start: str | None = None,
    open_now: set[Any] | None = None,
    in_flight: set[Any] | None = None,
) -> dict[str, Any]:
    """open_now：此刻仍未处理的批注 id；一次算多批时由调用方算好传入，不传就现算。"""
    got = batch_resolved(notes, item, end, next_start)
    if open_now is None:
        open_now = _open_ids(notes)
    seq = int(item.get("seq", 0) or 0)
    drafted = [n.get("id") for n in notes if n.get("status") == "drafted" and n.get("draftBatch") == seq]
    stats = {
        "resolvedIds": [n.get("id") for n in got],
        "resolvedCount": len(got),
        "draftedIds": drafted,
        "draftedCount": len(drafted),
        "resolvedPages": sorted({int(n.get("page", 0) or 0) for n in got}),
        "leftOpen": sum(1 for x in item.get("openIds") or [] if x in open_now),
    }
    if (
        item.get("videoReceipts")
        or item.get("videoVersionId")
        or item.get("videoFailure")
        or item.get("videoStats") is not None
    ):
        stats["videoStats"] = _video_batch_stats(notes, item, in_flight=in_flight)
    return stats


def _in_flight_ids(items: list[dict[str, Any]], notes: list[dict[str, Any]]) -> set[Any]:
    flight = [i for i in items if i.get("status") in IN_FLIGHT]
    by_id = {n.get("id"): n for n in notes}

    def returned(nid: str, seq: int) -> bool:  # 复核时退回给用户的：不再算「处理中」
        t = [e for e in (by_id.get(nid) or {}).get("trail") or [] if isinstance(e, dict)]
        return bool(t) and t[-1].get("action") == "returned" and t[-1].get("batch") == seq

    return {x for i in flight for x in (i.get("openIds") or []) if not returned(x, int(i.get("seq", 0)))}


def handoff_state(review_dir: Path, *, full: bool = True, handoff: Any = None, notes: Any = None) -> dict[str, Any]:
    """给页面和 status 用的摘要：最近一批、各状态计数，以及（full 时）全部批次 history（处理记录，新的在前，
    不含提交时的批注 id 清单）。页面轮询用 full=False，打开处理记录时再取全量。

    已处理的批次带收尾时记下的「这批解决了哪些批注」；0.3 留下的旧批次没有记，按时间窗补算并标 estimated；
    处理中的批次按当前进度实时算（live）。

    handoff / notes：调用方已读好的批次记录与批注（一次请求里只读一次时传入，见 serve.py 的请求快照）；
    不传就自己读，读取失败时的降级结果与原来一致。
    """
    try:
        doc = pre_read(handoff, None, lambda: handoff_load(review_dir))
    except ValueError:
        return {"error": "handoff.json 无法解析"}
    items = doc["items"]
    try:
        notes = pre_read(notes, None, lambda: load(review_dir)["notes"])
    except (OSError, ValueError):
        notes = []
    manifest = ledger.read_manifest(review_dir)
    in_flight_set = _in_flight_ids(items, notes)
    history = []
    open_now = _open_ids(notes)
    last = len(items) - 1
    # 新的在前；pos 是批次在 items 里的位置，下一批（pos + 1）的接收时间是时间窗上界（同 _next_start）
    for pos in range(last, (0 if full else max(last, 0)) - 1, -1):
        it = items[pos]
        next_start = items[pos + 1].get("claimedAt") if pos < last else None
        h = {k: v for k, v in it.items() if k != "openIds"}
        h["userMessage"] = handoff_user_message(it)
        if it.get("status") in ("claimed", "drafted", "review", "reviewing"):
            h.update(_batch_stats(notes, it, None, next_start, open_now, in_flight=in_flight_set), live=True)
        elif it.get("status") == "done" and "resolvedIds" not in it:
            h.update(
                _batch_stats(notes, it, it.get("doneAt"), next_start, open_now, in_flight=in_flight_set), estimated=True
            )
        elif it.get("status") == "done" and (it.get("videoReceipts") or it.get("videoStats") is not None):
            h["videoStats"] = _video_batch_stats(notes, it, in_flight=in_flight_set)
        history.append(h)

    out = {
        "latest": history[0] if history else None,
        "total": len(items),
        # 已交给 Agent、还在路上的批注（页面「批注」按钮据此显示「处理中」而不是「未处理」）
        "inFlightIds": sorted(in_flight_set),
        # 在途与停在待复核的批次及用户要求（show / 接手中断的代理据此知道要做到哪一步、能不能派子代理）
        "open": [
            {
                **{k: i.get(k) for k in ("seq", "status", "target", "allowSubagents", "fullCheck", "message")},
                "userMessage": handoff_user_message(i),
            }
            for i in items
            if i.get("status") in IN_FLIGHT + ("drafted",)
        ],
        **{st: sum(i.get("status") == st for i in items) for st in BATCH_STATUSES},
        "finalizable": _finalizable(notes, items, manifest=manifest, review_dir=review_dir),
    }
    if full:
        out["history"] = history
    return out


TARGETS = ("final", "review")  # 这批做到哪一步：定稿 / 已初改·待复核（由用户在「交给 Agent」时选）
BATCH_STATUSES = ("pending", "claimed", "drafted", "review", "reviewing", "done", "void")
# pending 等待接收 → claimed 初改中（或单段流程的处理中）→ drafted 已初改·待复核（停下等用户）
#   → review 等待复核接收 → reviewing 复核中 → done 已定稿；drafted / review 也可由用户直接定稿
#   void 作废：批注全部被分析移出后整批作废，不再被领取（分析存储在共用锁内改写）


class Unreviewed(HandoffError):
    """复核收尾时还有没给结论的初改批注。"""

    def __init__(self, ids: list[str], seq: int):
        super().__init__(
            f"第 {seq} 批还有 {len(ids)} 条初改没给复核结论：{'、'.join(ids)}。"
            f"逐条 notes.py review，或确认都可以后加 --accept-rest 一并通过",
            code="unreviewed",
            ids=ids,
            seq=seq,
        )
        self.ids, self.seq = ids, seq


def _open_snapshot(notes: list[dict[str, Any]]) -> dict[str, Any]:
    open_notes = [n for n in notes if n.get("status", "open") == "open"]
    return {
        "openCount": len(open_notes),
        "openIds": [n.get("id") for n in open_notes],
        "pages": sorted({int(n.get("page", 0) or 0) for n in open_notes}),
    }


def _selected_snapshot(notes: list[dict[str, Any]], note_ids: list[Any]) -> dict[str, Any]:
    """选择性提交的快照：只收指定的未处理批注（去重保序）；选了不存在或不在处理的 id 就拒绝。"""
    by_id = {n.get("id"): n for n in notes}
    seen: set[Any] = set()
    ids: list[Any] = []
    for nid in note_ids:
        note = by_id.get(nid)
        if note is None:
            raise HandoffError(f"批注 {nid} 不存在", code="note-missing", noteId=nid)
        if note.get("status", "open") != "open":
            raise HandoffError(f"批注 {nid} 不是未处理状态，不能选进这一批", code="note-not-open", noteId=nid)
        if nid not in seen:
            seen.add(nid)
            ids.append(nid)
    if not ids:
        raise HandoffError("选择性提交至少要选一条未处理批注", code="note-not-open")
    return {
        "openCount": len(ids),
        "openIds": ids,
        "pages": sorted({int(by_id[i].get("page", 0) or 0) for i in ids}),
        "selected": True,
    }


def handoff_submit(
    review_dir: Path,
    message: str = "",
    *,
    target: str = "final",
    allow_subagents: bool = False,
    full_check: bool | None = None,
    by: str = "page",
    authorize: Callable[[], None] | None = None,
    note_ids: list[str] | None = None,
    submit_id: str | None = None,
) -> tuple[dict[str, Any], dict[str, Any], bool]:
    """记一次提交（一批），快照当时的未处理批注。上一次提交还没被代理接收时合并进去（刷新快照、附言与选项），不重复排队。

    target：final 做到定稿 / review 做到「已初改·待复核」后停下；allow_subagents：是否允许代理派发子代理；
    full_check：处理后是否检查整份材料的前后一致性；缺省时保留待接收批次的原选项。
    note_ids：选择性提交（视频审阅）只交所选批注，缺省仍是全部未处理批注（文档审阅沿用）；
    submit_id：这一次提交操作的幂等键——网络重试带同一键时，等待中的批次照常合并刷新，
    已被接收或收尾的批次原样返回（merged=True，不写任何更改），不产生新批次。"""
    if note_ids is not None and (not isinstance(note_ids, list) or not all(isinstance(x, str) and x for x in note_ids)):
        raise HandoffError("note_ids 须为批注 id 列表", code="bad-note-ids")
    if submit_id is not None and (not isinstance(submit_id, str) or not 1 <= len(submit_id) <= 64):
        raise HandoffError("submit_id 须为 1 至 64 个字符", code="bad-submit-id")
    message = fix_text(message)[:MAX_MSG]
    if target not in TARGETS:
        raise NoteError(f"target 须为 {TARGETS} 之一")
    with locked(review_dir):
        if authorize is not None:
            authorize()
        notes = load(review_dir)["notes"]
        doc = handoff_load(review_dir)
        items = doc["items"]
        existing = next((i for i in items if submit_id is not None and i.get("submitId") == submit_id), None)
        if existing is not None and existing.get("status") != "pending":
            # 陈旧提交的重试：批次已被接收或收尾，按原样返回，不新增也不改写。
            return doc, existing, True
        latest = items[-1] if items else None
        merge_into = (
            existing if existing is not None else latest if latest and latest.get("status") == "pending" else None
        )
        full_check_value = (
            merge_into.get("fullCheck") is True
            if merge_into and merge_into.get("status") == "pending" and full_check is None
            else full_check is True
        )
        # 旧版 wait 只输出 message；把勾选要求也放进去，避免仍在等待的旧进程漏掉。
        legacy_message = full_check_message(message) if full_check_value else message
        snap = {
            "at": now_iso(),
            "by": by,
            "message": legacy_message,
            "userMessage": message,
            "target": target,
            "allowSubagents": bool(allow_subagents),
            "fullCheck": full_check_value,
            "alwaysSkills": always_skills(review_dir),
            **(_selected_snapshot(notes, note_ids) if note_ids is not None else _open_snapshot(notes)),
        }
        if submit_id is not None:
            snap["submitId"] = submit_id
        if merge_into is not None:
            merge_into.update(snap)
            merge_into["updates"] = int(merge_into.get("updates", 0)) + 1
            item, merged = merge_into, True
        else:
            doc["seq"] = int(doc.get("seq", 0)) + 1
            doc["submitSeq"] = int(doc.get("submitSeq", 0)) + 1
            item, merged = {"seq": doc["seq"], "status": "pending", "order": doc["submitSeq"], **snap}, False
            doc["items"].append(item)
        _handoff_commit(
            review_dir,
            doc,
            {
                "op": "handoff-update" if merged else "handoff",
                "by": by,
                "seq": item["seq"],
                "openCount": item["openCount"],
                "target": target,
            },
        )
        return doc, item, merged


def _find_batch(items: list[dict[str, Any]], seq: int) -> dict[str, Any]:
    item = next((i for i in items if int(i.get("seq", 0)) == seq), None)
    if item is None:
        raise NotFound(f"handoff#{seq}")
    return item


def _stage_entry(item: dict[str, Any], stage: str) -> dict[str, Any]:
    """这批某个阶段的记录（没有就补一个：代理没先接收就收尾、或 0.4 以前的批次）。"""
    stages = item.setdefault("stages", [])
    for e in reversed(stages):
        if e.get("stage") == stage:
            return e
    e = {"stage": stage}
    if stage == "draft" and item.get("claimedAt"):
        e.update(by=item.get("claimedBy"), claimedAt=item.get("claimedAt"))
    stages.append(e)
    return e


# 接收批次：阶段 → 该阶段可接收的批次状态（notes.py wait 共用同一张表）
CLAIM_STATUSES = {"draft": ("pending",), "review": ("review",), "any": ("pending", "review")}


def handoff_claim(
    review_dir: Path,
    *,
    by: str = "agent",
    stage: str = "any",
    seq: int | None = None,
    authorize: Callable[[], None] | None = None,
) -> dict[str, Any] | None:
    """接收最早一个等着的批次：stage=draft 只接新提交（→ 初改中），review 只接请求复核的（→ 复核中），any 都接。
    返回的批次带 stage 字段说明是哪个阶段；没有则 None。"""
    want = CLAIM_STATUSES.get(stage)
    if want is None:
        raise NoteError("stage 须为 draft / review / any")
    with locked(review_dir):
        if authorize is not None:
            authorize()
        doc = handoff_load(review_dir)
        item = next(
            (i for i in doc["items"] if i.get("status") in want and (seq is None or int(i.get("seq", 0)) == seq)), None
        )
        if item is None:
            return None
        now = now_iso()
        if item["status"] == "pending":
            item.update({"status": "claimed", "claimedAt": now, "claimedBy": by})
            item.setdefault("stages", []).append({"stage": "draft", "by": by, "claimedAt": now})
            st = "draft"
        else:
            item["status"] = "reviewing"
            item.setdefault("stages", []).append({"stage": "review", "by": by, "claimedAt": now})
            st = "review"
        _handoff_commit(review_dir, doc, {"op": "handoff-claim", "by": by, "seq": item["seq"], "stage": st})
        return {**item, "userMessage": handoff_user_message(item), "stage": st}


def _finish(
    item: dict[str, Any], notes: list[dict[str, Any]] | None, items: list[dict[str, Any]], by: str, summary: str
) -> None:
    item.update({"status": "done", "doneAt": now_iso(), "doneBy": by})
    if summary:
        item["summary"] = summary
    if notes is not None:
        item.update(_batch_stats(notes, item, item["doneAt"], _next_start(items, item)))


def _load_notes_or_none(review_dir: Path) -> dict[str, Any] | None:
    try:
        return load(review_dir)
    except (OSError, ValueError):
        return None


def handoff_done(
    review_dir: Path,
    *,
    seq: int | None = None,
    summary: str = "",
    by: str = "agent",
    mode: str | None = None,
    accept_rest: bool = False,
    authorize: Callable[[], None] | None = None,
) -> dict[str, Any]:
    """收尾当前阶段。不给 seq 时取最近一个处理中（初改中 / 复核中）的，没有就取最近一个等待接收的。

    初改阶段：用户选了「待复核」（target=review）→ 停在 drafted 等用户；选了定稿 → 直接定稿（done）；
    允许派发子代理且 mode=draft → 进入「等待复核」（review），由复核者接收。复核阶段：给完结论后定稿。"""
    summary = fix_text(summary)[:MAX_MSG]
    if mode not in (None, "draft", "final"):
        raise NoteError("mode 须为 draft / final")
    with locked(review_dir):
        if authorize is not None:
            authorize()
        manifest = ledger.read_manifest(review_dir)
        is_video = _is_video_review(manifest)
        doc = handoff_load(review_dir)
        items = doc["items"]
        if seq is not None:
            item = _find_batch(items, seq)
        else:
            active = [i for i in items if i.get("status") in ("claimed", "reviewing")]
            if len(active) > 1:
                raise HandoffError(
                    "同时有 "
                    + "、".join(
                        f"第 {i['seq']} 批（{'复核中' if i['status'] == 'reviewing' else '处理中'}）" for i in active
                    )
                    + "：用 --seq 指定收尾哪一批",
                    code="ambiguous-batches",
                    batches=[(i["seq"], i["status"]) for i in active],
                )
            item = active[0] if active else next((i for i in reversed(items) if i.get("status") == "pending"), None)
            if item is None:
                raise NotFound("没有待处理的提交")
        status = item.get("status")
        ndoc = _load_notes_or_none(review_dir)
        notes = ndoc["notes"] if ndoc else None
        n_seq = int(item["seq"])
        mine = [n for n in notes or [] if n.get("status") == "drafted" and n.get("draftBatch") == n_seq]
        changed = False
        if status in ("pending", "claimed"):
            target = item.get("target", "final")
            if is_video:
                if mode == "final":
                    raise HandoffError(
                        "视频批注必须由审阅人在页面复核通过，不能通过 done --final 直接定稿",
                        code="video-manual-approval-required",
                        seq=n_seq,
                    )
                if mine:
                    to = "drafted"
                elif mode == "draft":
                    if not item.get("allowSubagents"):
                        raise HandoffError(
                            f"第 {n_seq} 批由你直接处理到定稿（用户没有允许派发子代理）：直接 notes.py done",
                            code="subagents-forbidden",
                            seq=n_seq,
                        )
                    to = "review"
                else:
                    to = "done"
            elif target == "review":
                if mode == "final":
                    raise HandoffError(
                        f"用户要求第 {n_seq} 批做到「已初改·待复核」为止：不能直接定稿（用户可在页面上定稿）",
                        code="final-forbidden",
                        seq=n_seq,
                    )
                to = "drafted"
            elif mode == "draft":
                if not item.get("allowSubagents"):
                    raise HandoffError(
                        f"第 {n_seq} 批由你直接处理到定稿（用户没有允许派发子代理）：直接 notes.py done",
                        code="subagents-forbidden",
                        seq=n_seq,
                    )
                to = "review"
            else:
                to = "done"
            e = _stage_entry(item, "draft")
            e.update(doneAt=now_iso(), doneBy=by, draftedIds=[n["id"] for n in mine])
            if summary:
                e["summary"] = summary
            if to == "done":
                for n in mine:  # 单段流程（或允许子代理但直接定稿）：初改即定稿（视频审阅前面已保证 mine 为空）
                    _mark_resolved(n, by=by, resolution=n.get("resolution", ""), batch=n_seq)
                    _trail(n, by=by, stage="draft", batch=n_seq, action="finalized")
                    changed = True
                _finish(item, notes, items, by, summary)
            else:
                item["status"] = to
                if to == "review":
                    doc["submitSeq"] = int(doc.get("submitSeq", 0)) + 1
                    item.update(order=doc["submitSeq"], reviewRequestedAt=now_iso())
                item["draftedCount"] = len(mine)
                if is_video and notes is not None:
                    item.update(_batch_stats(notes, item, None))
                if summary:
                    item["summary"] = summary
        elif status == "reviewing":
            if is_video:
                raise HandoffError(
                    "视频批注必须由审阅人在页面复核通过，不能在 Agent 复核阶段直接定稿",
                    code="video-manual-approval-required",
                    seq=n_seq,
                )
            if mine and not accept_rest:
                raise Unreviewed([n["id"] for n in mine], n_seq)
            for n in mine:
                _mark_resolved(n, by=by, resolution=n.get("resolution", ""), batch=n_seq)
                _trail(n, by=by, stage="review", batch=n_seq, action="approved", note="复核收尾时一并通过")
                changed = True
            e = _stage_entry(item, "review")
            e.update(doneAt=now_iso(), doneBy=by, **_review_counts(notes or [], n_seq))
            if summary:
                e["summary"] = summary
            _finish(item, notes, items, by, summary)
        elif status in ("drafted", "review"):
            raise HandoffError(
                f"第 {n_seq} 批在等复核：要复核先 notes.py claim --stage review（或用户在页面请求复核）；"
                "要直接定稿由用户在页面上点「直接定稿」",
                code="review-not-claimed",
                seq=n_seq,
            )
        else:
            raise HandoffError(f"第 {n_seq} 批已经定稿了", code="already-done", seq=n_seq)
        if changed:
            _commit(review_dir, ndoc, {"op": "finalize", "by": by, "seq": n_seq, "ids": [n["id"] for n in mine]})
        _handoff_commit(
            review_dir,
            doc,
            {
                "op": "handoff-done",
                "by": by,
                "seq": n_seq,
                "status": item["status"],
                "resolvedCount": item.get("resolvedCount"),
                "leftOpen": item.get("leftOpen"),
            },
        )
        return item


def _review_counts(notes: list[dict[str, Any]], seq: int) -> dict[str, int]:
    """复核阶段的结论计数：从批注痕迹里数这批复核阶段的 approved / revised / returned / resolved。"""
    c = {"approved": 0, "revised": 0, "returned": 0, "resolved": 0}
    for n in notes:  # 每条只算这批复核阶段的最后一个动作（改过主意的不重复计）
        last = None
        for e in n.get("trail") or []:
            if isinstance(e, dict) and e.get("stage") == "review" and e.get("batch") == seq and e.get("action") in c:
                last = e["action"]
        if last:
            c[last] += 1
    return c


def handoff_request_review(
    review_dir: Path,
    seq: int,
    message: str = "",
    *,
    allow_subagents: bool = False,
    by: str = "page",
    authorize: Callable[[], None] | None = None,
) -> dict[str, Any]:
    """用户看过初改，请代理复核：drafted → review（等待复核接收）。"""
    with locked(review_dir):
        if authorize is not None:
            authorize()
        doc = handoff_load(review_dir)
        item = _find_batch(doc["items"], seq)
        if item.get("status") not in ("drafted", "review"):
            raise NoteError(f"第 {seq} 批不在「已初改·待复核」状态")
        if item["status"] != "review":
            doc["submitSeq"] = int(doc.get("submitSeq", 0)) + 1
            item["order"] = doc["submitSeq"]
        item.update(
            {
                "status": "review",
                "reviewRequestedAt": now_iso(),
                "reviewRequestedBy": by,
                "reviewMessage": fix_text(message)[:MAX_MSG],
                "allowSubagents": bool(allow_subagents),
            }
        )
        _handoff_commit(review_dir, doc, {"op": "handoff-review", "by": by, "seq": seq})
        return item


def _finalizable(
    notes: list[dict[str, Any]],
    items: list[dict[str, Any]],
    *,
    manifest: dict[str, Any] | None = None,
    review_dir: Path | None = None,
) -> dict[str, Any]:
    """跨批初改快照；复用请求内已读数据，不另读文件。"""
    batches = {item["seq"]: item for item in items}
    groups: dict[int, list[str]] = {}
    skipped = []
    manifest = manifest or {}
    for note in notes:
        if note.get("status") != "drafted":
            continue
        seq = note.get("draftBatch")
        status = batches.get(seq, {}).get("status")
        if status in ("drafted", "review"):
            if _is_video_review(manifest, note):
                ready, reason = video_review.review_readiness(note, manifest, review_dir=review_dir)
                if not ready:
                    skipped.append({"id": note["id"], "batch": seq, "reason": "unready", "detail": reason})
                    continue
            groups.setdefault(seq, []).append(note["id"])
        else:
            skipped.append(
                {
                    "id": note["id"],
                    "batch": seq,
                    "reason": status if status in ("claimed", "reviewing") else "unavailable",
                }
            )
    return {
        "count": sum(len(ids) for ids in groups.values()),
        "batches": [{"seq": seq, "ids": ids} for seq, ids in sorted(groups.items())],
        "skipped": skipped,
    }


def _sync_batch_for_video_decision(
    review_dir: Path,
    handoff_doc: dict[str, Any],
    ndoc: dict[str, Any],
    *,
    batch_seq: int | None,
    by: str,
    op: str,
    note_id: str,
) -> dict[str, Any]:
    if batch_seq is None:
        return handoff_doc
    item = next((i for i in handoff_doc["items"] if int(i.get("seq", 0) or 0) == batch_seq), None)
    if item is None:
        return handoff_doc
    in_flight_set = _in_flight_ids(handoff_doc["items"], ndoc["notes"])
    mine = [n for n in ndoc["notes"] if n.get("status") == "drafted" and n.get("draftBatch") == batch_seq]
    item["draftedIds"] = [n["id"] for n in mine]
    item["draftedCount"] = len(mine)
    if item.get("status") in ("drafted", "review") and not mine:
        _finish(item, ndoc["notes"], handoff_doc["items"], by, "")
        item["videoStats"] = _video_batch_stats(ndoc["notes"], item, in_flight=in_flight_set)
    elif item.get("status") == "done":
        open_now = _open_ids(ndoc["notes"])
        item["leftOpen"] = sum(1 for x in item.get("openIds") or [] if x in open_now)
        item["videoStats"] = _video_batch_stats(ndoc["notes"], item, in_flight=in_flight_set)
    else:
        item["videoStats"] = _video_batch_stats(ndoc["notes"], item, in_flight=in_flight_set)
    return _handoff_commit(
        review_dir,
        handoff_doc,
        {"op": op, "by": by, "seq": batch_seq, "id": note_id},
    )


def _finalize_batch(
    review_dir: Path,
    doc: dict[str, Any],
    ndoc: dict[str, Any],
    item: dict[str, Any],
    mine: list[dict[str, Any]],
    by: str,
    *,
    manifest: dict[str, Any] | None = None,
) -> None:
    """调用方已持批注锁；单批与跨批定稿共用处理痕迹和阶段记录。"""
    seq = item["seq"]
    manifest = manifest if manifest is not None else ledger.read_manifest(review_dir)
    for note in mine:
        _mark_resolved(note, by=by, resolution=note.get("resolution", ""), batch=seq)
        if _is_video_review(manifest, note) and isinstance(note.get("videoReview"), dict):
            _record_video_approval(note, by=by, batch=seq, now=note["resolvedAt"])
        _trail(note, by=by, stage="page", batch=seq, action="accepted")
    if mine:
        _commit(review_dir, ndoc, {"op": "finalize", "by": by, "seq": seq, "ids": [n["id"] for n in mine]})
    item.setdefault("stages", []).append({"stage": "accept", "by": by, "at": now_iso(), "count": len(mine)})
    remaining_drafted = any(n.get("status") == "drafted" and n.get("draftBatch") == seq for n in ndoc["notes"])
    in_flight_set = _in_flight_ids(doc["items"], ndoc["notes"])
    if remaining_drafted:
        item["status"] = "drafted"
        item.update(_batch_stats(ndoc["notes"], item, None, in_flight=in_flight_set))
    else:
        _finish(item, ndoc["notes"], doc["items"], by, "")
        if _is_video_review(manifest) or item.get("videoReceipts") or item.get("videoStats") is not None:
            item["videoStats"] = _video_batch_stats(ndoc["notes"], item, in_flight=in_flight_set)
    _handoff_commit(review_dir, doc, {"op": "handoff-finalize", "by": by, "seq": seq, "count": len(mine)})


def handoff_finalize(review_dir: Path, seq: int, *, by: str = "page") -> dict[str, Any]:
    """用户看过初改觉得可以，跳过复核直接定稿：这批的初改批注全部定为已处理。"""
    with locked(review_dir):
        manifest = ledger.read_manifest(review_dir)
        if _is_video_review(manifest) and by != "page":
            raise HandoffError(
                "视频批注只能由审阅人在页面复核通过，Agent 不能直接定稿",
                code="video-manual-approval-required",
                seq=seq,
            )
        doc = handoff_load(review_dir)
        item = _find_batch(doc["items"], seq)
        if item.get("status") not in ("drafted", "review"):
            raise NoteError(f"第 {seq} 批不在「已初改·待复核」状态")
        ndoc = load(review_dir)
        mine = [n for n in ndoc["notes"] if n.get("status") == "drafted" and n.get("draftBatch") == seq]
        for note in mine:
            if _is_video_review(manifest, note):
                ready, reason = video_review.review_readiness(note, manifest, review_dir=review_dir)
                if not ready:
                    raise HandoffError(
                        f"视频批注 {note['id']} 尚未满足人工复核通过条件（{reason}）",
                        code="video-not-ready",
                        id=note["id"],
                        seq=seq,
                        reason=reason,
                    )
        _finalize_batch(review_dir, doc, ndoc, item, mine, by, manifest=manifest)
        return item


def handoff_finalize_all(review_dir: Path, selections: dict[int, set[str]]) -> dict[str, Any]:
    """只定稿审阅人确认过的初改；与 Agent 接收复核共用同一把写锁。"""
    with locked(review_dir):
        manifest = ledger.read_manifest(review_dir)
        doc, ndoc = handoff_load(review_dir), load(review_dir)
        snapshot = _finalizable(ndoc["notes"], doc["items"], manifest=manifest, review_dir=review_dir)
        skipped = {entry["id"]: entry for entry in snapshot["skipped"]}
        notes = {note["id"]: note for note in ndoc["notes"]}
        items = {item["seq"]: item for item in doc["items"]}
        is_video = _is_video_review(manifest) or any(_is_video_review(manifest, n) for n in ndoc["notes"])
        finalized, batches = [], []
        for group in snapshot["batches"]:
            seq, ids = group["seq"], group["ids"]
            confirmed = selections.get(seq, set())
            if is_video:
                picked = [nid for nid in ids if nid in confirmed]
                for nid in ids:
                    if nid not in confirmed:
                        skipped[nid] = {"id": nid, "batch": seq, "reason": "unconfirmed"}
                if not picked:
                    continue
                _finalize_batch(
                    review_dir,
                    doc,
                    ndoc,
                    items[seq],
                    [notes[nid] for nid in picked],
                    "page",
                    manifest=manifest,
                )
                finalized.extend(picked)
                batches.append(seq)
            else:
                # 确认后新增的初改不能被顺带定稿，也不能留下已完成批次里的初改。
                if not set(ids).issubset(confirmed):
                    reason = "changed" if confirmed else "unconfirmed"
                    skipped.update({nid: {"id": nid, "batch": seq, "reason": reason} for nid in ids})
                    continue
                _finalize_batch(
                    review_dir,
                    doc,
                    ndoc,
                    items[seq],
                    [notes[nid] for nid in ids],
                    "page",
                    manifest=manifest,
                )
                finalized.extend(ids)
                batches.append(seq)
        for seq, ids in selections.items():
            for nid in ids:
                if nid not in finalized and nid not in skipped:
                    note = notes.get(nid)
                    if (
                        is_video
                        and note is not None
                        and _is_video_review(manifest, note)
                        and note.get("status") != "resolved"
                    ):
                        ready, detail = video_review.review_readiness(note, manifest, review_dir=review_dir)
                        if not ready:
                            skipped[nid] = {"id": nid, "batch": seq, "reason": "unready", "detail": detail}
                            continue
                    skipped[nid] = {"id": nid, "batch": seq, "reason": "changed"}
        return {
            "finalizedCount": len(finalized),
            "finalizedIds": finalized,
            "batches": batches,
            "skippedCount": len(skipped),
            "skipped": list(skipped.values()),
        }


VIDEO_RECEIPT_OUTCOMES = ("fixed", "deleted", "question", "needs_relocate", "failed")
VIDEO_HISTORY_KEEP = 50


def _receipt_str(value: Any, *, limit: int = MAX_TEXT) -> str:
    if not isinstance(value, str):
        return ""
    return fix_text(value).strip()[:limit]


def _normalize_video_receipt(
    receipt: dict[str, Any] | list[dict[str, Any]] | None,
    *,
    seq: int | None,
    review_id: str | None,
    receipt_id: str | None,
    movie: Path | None,
    render_error: str | None,
    summary: str,
) -> dict[str, Any]:
    raw_notes: Any = []
    if receipt is not None:
        if isinstance(receipt, list):
            raw_notes = receipt
        elif isinstance(receipt, dict):
            if "noteId" in receipt and "notes" not in receipt:
                raw_notes = [receipt]
            else:
                raw_notes = receipt.get("notes", [])
            if seq is None and receipt.get("seq") is not None:
                seq = receipt.get("seq")
            if review_id is None and isinstance(receipt.get("reviewId"), str):
                review_id = receipt.get("reviewId")
            if receipt_id is None and isinstance(receipt.get("receiptId"), str):
                receipt_id = receipt.get("receiptId")
            if not summary and isinstance(receipt.get("summary"), str):
                summary = receipt.get("summary") or ""
            if render_error is None and isinstance(receipt.get("renderError"), str):
                render_error = receipt.get("renderError")
            if movie is None and isinstance(receipt.get("movie"), str) and receipt["movie"].strip():
                movie = Path(receipt["movie"])
        else:
            raise HandoffError("video-receipt 回执体须为对象或列表", code="bad-receipt")

    if not isinstance(raw_notes, list):
        raise HandoffError("video-receipt 的 notes 须为列表", code="bad-receipt")
    if seq is not None and (isinstance(seq, bool) or not isinstance(seq, int) or seq < 1):
        raise HandoffError("seq 须为正整数", code="bad-receipt")

    rid_key = _receipt_str(receipt_id, limit=128) if receipt_id is not None else f"vr-{new_id()}"
    if receipt_id is not None and not rid_key:
        raise HandoffError("receiptId 不能为空", code="bad-receipt")

    clean_summary = fix_text(summary)[:MAX_MSG]
    clean_render_err = _receipt_str(render_error, limit=2000) if render_error is not None else ""
    if render_error is not None and not clean_render_err:
        raise HandoffError("renderError 不能为空", code="bad-receipt")
    if movie is not None and clean_render_err:
        raise HandoffError("不能同时提交新成片与渲染失败原因", code="receipt-conflict")

    entries: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    for raw in raw_notes:
        if not isinstance(raw, dict):
            raise HandoffError("回执条目须为对象", code="bad-receipt")
        nid = raw.get("noteId")
        if not isinstance(nid, str) or not valid_id(nid):
            raise HandoffError("回执条目的 noteId 无效", code="bad-receipt")
        if nid in seen_ids:
            raise HandoffError(f"同一次回执不能重复包含批注 {nid}", code="duplicate-receipt-note", id=nid)
        seen_ids.add(nid)
        outcome = raw.get("outcome")
        if outcome not in VIDEO_RECEIPT_OUTCOMES:
            raise HandoffError(
                f"回执条目的 outcome 须为 {' / '.join(VIDEO_RECEIPT_OUTCOMES)} 之一",
                code="bad-receipt",
                id=nid,
            )
        item_summary = _receipt_str(raw.get("summary") if raw.get("summary") is not None else raw.get("resolution"))
        question = _receipt_str(raw.get("question") if raw.get("question") is not None else raw.get("summary"))
        relocate_reason = _receipt_str(
            raw.get("relocateReason")
            if raw.get("relocateReason") is not None
            else raw.get("reason")
            if raw.get("reason") is not None
            else raw.get("summary")
        )
        failure_text = _receipt_str(
            raw.get("failure")
            if raw.get("failure") is not None
            else raw.get("reason")
            if raw.get("reason") is not None
            else raw.get("summary")
        )
        if outcome == "question" and not question:
            raise HandoffError(f"批注 {nid} 的 question 回执缺少提问内容", code="bad-receipt", id=nid)
        if outcome == "needs_relocate" and not relocate_reason:
            raise HandoffError(f"批注 {nid} 的 needs_relocate 回执缺少说明", code="bad-receipt", id=nid)
        if outcome == "failed" and not (failure_text or clean_render_err):
            raise HandoffError(f"批注 {nid} 的 failed 回执缺少失败原因", code="bad-receipt", id=nid)
        if clean_render_err and outcome in ("fixed", "deleted"):
            raise HandoffError("渲染失败时不能交回 fixed / deleted 回执", code="receipt-render-failed", id=nid)
        version_id = raw.get("versionId")
        if version_id is not None and (
            not isinstance(version_id, str) or not video_review.VERSION_ID.fullmatch(version_id)
        ):
            raise HandoffError(f"批注 {nid} 的 versionId 无效", code="bad-receipt", id=nid)
        entries.append(
            {
                "noteId": nid,
                "outcome": outcome,
                "summary": item_summary,
                "target": raw.get("target"),
                "versionId": version_id,
                "question": question,
                "relocateReason": relocate_reason,
                "failure": failure_text or clean_render_err,
            }
        )

    return {
        "seq": seq,
        "reviewId": review_id,
        "receiptId": rid_key,
        "movie": Path(movie) if movie is not None else None,
        "renderError": clean_render_err,
        "summary": clean_summary,
        "entries": entries,
    }


def _find_claimed_batch(items: list[dict[str, Any]], seq: int | None) -> dict[str, Any]:
    if seq is not None:
        return _find_batch(items, seq)
    active = [i for i in items if i.get("status") == "claimed"]
    if len(active) > 1:
        raise HandoffError(
            "同时有 " + "、".join(f"第 {i['seq']} 批（处理中）" for i in active) + "：用 --seq 指定交回哪一批",
            code="ambiguous-batches",
            batches=[(i["seq"], i["status"]) for i in active],
        )
    if not active:
        raise HandoffError("当前没有已领取处理中的批次", code="batch-not-claimed")
    return active[0]


def _find_receipt_replay(item: dict[str, Any], receipt_id: str) -> dict[str, Any] | None:
    for rec in item.get("videoReceipts") or []:
        if isinstance(rec, dict) and rec.get("receiptId") == receipt_id:
            return rec
    return None


def _validate_receipt_note(note: dict[str, Any], nid: str, item: dict[str, Any], n_seq: int) -> None:
    if nid not in (item.get("openIds") or []):
        raise HandoffError(f"批注 {nid} 不在第 {n_seq} 批的提交范围内", code="note-not-in-batch", id=nid, seq=n_seq)
    if note.get("status") == "held":
        raise HandoffError(f"批注 {nid} 已被分析搁住，不能写入回执", code="note-held", id=nid)
    vr = note.get("videoReview") if isinstance(note.get("videoReview"), dict) else None
    if note.get("status") == "resolved" or (vr and vr.get("decision") == "approved"):
        raise HandoffError(f"批注 {nid} 已有人工决定，不能被回执覆盖", code="note-already-decided", id=nid)
    if vr and vr.get("decision") in ("returned", "replied"):
        dec_batch = vr.get("decisionBatch") or vr.get("batch")
        if isinstance(dec_batch, int) and not isinstance(dec_batch, bool) and n_seq <= dec_batch:
            raise HandoffError(
                f"批注 {nid} 已有人工决定，拒绝同批或更早批次的陈旧回执",
                code="stale-receipt",
                id=nid,
                seq=n_seq,
            )
    batches: list[Any] = [note.get("draftBatch"), note.get("resolvedBatch"), vr.get("batch") if vr else None]
    if vr:
        batches.extend(h.get("batch") for h in (vr.get("history") or []) if isinstance(h, dict))
    batches.extend(t.get("batch") for t in (note.get("trail") or []) if isinstance(t, dict))
    if any(isinstance(b, int) and not isinstance(b, bool) and b > n_seq for b in batches):
        raise HandoffError(f"批注 {nid} 已有更新批次的处理记录，拒绝陈旧回执", code="stale-receipt", id=nid, seq=n_seq)


def _apply_video_receipt_to_note(
    note: dict[str, Any],
    *,
    entry: dict[str, Any],
    n_seq: int,
    receipt_id: str,
    by: str,
    now: str,
    version_meta: dict[str, Any] | None,
    cleaned_target: dict[str, Any] | None,
) -> dict[str, Any]:
    outcome = entry["outcome"]
    vr = (
        copy.deepcopy(note.get("videoReview")) if isinstance(note.get("videoReview"), dict) else {"v": 1, "history": []}
    )
    history = vr.get("history") if isinstance(vr.get("history"), list) else []
    version_id = version_meta["versionId"] if version_meta else entry.get("versionId")

    if outcome in ("fixed", "deleted"):
        if cleaned_target is not None and entry["summary"] and version_meta is not None:
            state = "pending_review"
            for k in RESOLVE_FIELDS:
                note.pop(k, None)
            note.update(
                status="drafted",
                draftedAt=now,
                draftedBy=by,
                draftBatch=n_seq,
                resolution=entry["summary"],
            )
            trail_action = "drafted"
            trail_note = entry["summary"]
        else:
            state = "missing_evidence"
            note["status"] = "open"
            for k in RESOLVE_FIELDS + DRAFT_FIELDS:
                note.pop(k, None)
            trail_action = "missing_evidence"
            trail_note = entry["summary"] or None
    elif outcome == "question":
        state = "needs_reply"
        note["status"] = "open"
        for k in RESOLVE_FIELDS + DRAFT_FIELDS:
            note.pop(k, None)
        trail_action = "question"
        trail_note = entry["question"]
    elif outcome == "needs_relocate":
        state = "needs_relocate"
        note["status"] = "open"
        for k in RESOLVE_FIELDS + DRAFT_FIELDS:
            note.pop(k, None)
        trail_action = "relocate"
        trail_note = entry["relocateReason"]
    else:
        state = "failed"
        note["status"] = "open"
        for k in RESOLVE_FIELDS + DRAFT_FIELDS:
            note.pop(k, None)
        trail_action = "failed"
        trail_note = entry["failure"]

    hist_entry: dict[str, Any] = {
        "batch": n_seq,
        "receiptId": receipt_id,
        "at": now,
        "by": by,
        "outcome": outcome,
        "state": state,
    }
    if version_id:
        hist_entry["versionId"] = version_id
    if entry.get("summary"):
        hist_entry["summary"] = entry["summary"]
    if cleaned_target is not None:
        hist_entry["target"] = cleaned_target
    if outcome == "question":
        hist_entry["question"] = entry["question"]
    if outcome == "needs_relocate":
        hist_entry["relocateReason"] = entry["relocateReason"]
    if outcome == "failed":
        hist_entry["failure"] = entry["failure"]
    history.append(hist_entry)

    new_vr: dict[str, Any] = {
        "v": 1,
        "state": state,
        "batch": n_seq,
        "receiptId": receipt_id,
        "outcome": outcome,
        "by": by,
        "at": now,
        "history": history[-VIDEO_HISTORY_KEEP:],
    }
    if version_id:
        new_vr["versionId"] = version_id
    if entry.get("summary"):
        new_vr["summary"] = entry["summary"]
    if cleaned_target is not None:
        new_vr["target"] = cleaned_target
    if outcome == "question":
        new_vr["question"] = entry["question"]
    if outcome == "needs_relocate":
        new_vr["relocateReason"] = entry["relocateReason"]
    if outcome == "failed":
        new_vr["failure"] = entry["failure"]
    if isinstance(vr.get("relocatedFrom"), dict):
        new_vr["relocatedFrom"] = copy.deepcopy(vr["relocatedFrom"])
    note["videoReview"] = new_vr
    note["updatedAt"] = now
    _trail(note, by=by, stage="draft", batch=n_seq, action=trail_action, note=trail_note)
    return hist_entry


def _replay_receipt_response(
    manifest: dict[str, Any], item: dict[str, Any], rec: dict[str, Any], notes: list[dict[str, Any]]
) -> dict[str, Any]:
    ver = None
    if rec.get("versionId"):
        try:
            ver = video_review.registered_version(manifest, str(rec["versionId"]))
        except video_review.VideoError:
            ver = None
    by_id = {n.get("id"): n for n in notes}
    touched = [
        by_id[it["noteId"]] for it in rec.get("items") or [] if isinstance(it, dict) and it.get("noteId") in by_id
    ]
    return {
        "ok": True,
        "replayed": True,
        "seq": int(item["seq"]),
        "status": item.get("status"),
        "receiptId": rec.get("receiptId"),
        "version": ver,
        "failure": rec.get("failure"),
        "batch": copy.deepcopy(item),
        "notes": copy.deepcopy(touched),
    }


def video_receipt(
    review_dir: Path,
    *,
    seq: int | None = None,
    review_id: str | None = None,
    receipt_id: str | None = None,
    movie: Path | None = None,
    render_error: str | None = None,
    receipt: dict[str, Any] | list[dict[str, Any]] | None = None,
    summary: str = "",
    finish: bool = True,
    by: str = "agent",
    authorize: Callable[[], None] | None = None,
) -> dict[str, Any]:
    """视频审阅回执：不嵌套文件锁，先校验批注与批次，再在 metadata_lock 登记新版/失败，最后写回批注与批次。"""
    actor = str(by or "").strip()[:200] or "agent"
    spec = _normalize_video_receipt(
        receipt,
        seq=seq,
        review_id=review_id,
        receipt_id=receipt_id,
        movie=movie,
        render_error=render_error,
        summary=summary,
    )
    req_seq = spec["seq"]
    req_rid = spec["reviewId"]
    rid_key = spec["receiptId"]
    req_movie = spec["movie"]
    req_render_err = spec["renderError"]
    req_summary = spec["summary"]
    entries = spec["entries"]

    # Phase 1: 在 .notes.lock 下预检批次、权限、幂等、目标批注与（可选）源位置范围
    probed_meta: dict[str, Any] | None = None
    probe_error: video_review.VideoError | None = None
    with locked(review_dir):
        if authorize is not None:
            authorize()
        manifest = ledger.read_manifest(review_dir)
        if not _is_video_review(manifest):
            raise HandoffError("只有视频审阅支持 video-receipt", code="no-video")
        if req_rid is not None and manifest.get("reviewId") != req_rid:
            raise HandoffError("审阅标识不匹配，拒绝写入其他审阅", code="review-mismatch")
        doc = handoff_load(review_dir)
        ndoc = load(review_dir)
        item = _find_claimed_batch(doc["items"], req_seq)
        n_seq = int(item["seq"])
        if replay := _find_receipt_replay(item, rid_key):
            return _replay_receipt_response(manifest, item, replay, ndoc["notes"])
        if item.get("status") != "claimed":
            raise HandoffError(
                f"第 {n_seq} 批不在处理中（{item.get('status')}），不能写入回执",
                code="batch-not-claimed",
                seq=n_seq,
                status=item.get("status"),
            )
        by_id = {n.get("id"): n for n in ndoc["notes"]}
        for entry in entries:
            nid = entry["noteId"]
            if nid not in by_id:
                raise NotFound(nid)
            _validate_receipt_note(by_id[nid], nid, item, n_seq)

        # 预检新版成片与目标帧范围，避免提交了无效坐标后还要写半截状态
        if req_movie is not None:
            try:
                probed_meta = video_review.probe(req_movie)
            except video_review.VideoError as exc:
                probe_error = exc
            else:
                next_vid = f"V{video_review._next_version_number(manifest['video'])}"
                candidate_meta = {**probed_meta, "versionId": next_vid}
                for entry in entries:
                    if entry["outcome"] in ("fixed", "deleted") and entry["target"] is not None:
                        target_vid = entry.get("versionId") or entry["target"].get("versionId") or next_vid
                        try:
                            target_meta = (
                                candidate_meta
                                if target_vid == next_vid
                                else video_review.registered_version(manifest, str(target_vid))
                            )
                            video_review.clean_receipt_target(entry["target"], target_meta)
                        except video_review.VideoError as exc:
                            raise HandoffError(str(exc), code="invalid-receipt-target", id=entry["noteId"]) from None
        elif not req_render_err:
            for entry in entries:
                if entry["outcome"] in ("fixed", "deleted") and entry["target"] is not None:
                    target_vid = (
                        entry.get("versionId")
                        or (entry["target"].get("versionId") if isinstance(entry["target"], dict) else None)
                        or item.get("videoVersionId")
                    )
                    if target_vid:
                        try:
                            target_meta = video_review.registered_version(manifest, str(target_vid))
                            video_review.clean_receipt_target(entry["target"], target_meta)
                        except video_review.VideoError as exc:
                            raise HandoffError(str(exc), code="invalid-receipt-target", id=entry["noteId"]) from None

        lock_token = (
            int(ndoc.get("rev", 0)),
            doc.get("updatedAt"),
            item.get("status"),
            item.get("claimedAt"),
            item.get("updates"),
            len(doc["items"]),
        )

    # Phase 2: 释放 .notes.lock 后，在 metadata_lock 下登记新版或记录失败
    registered_version: dict[str, Any] | None = None
    recorded_failure: dict[str, Any] | None = None
    if req_render_err:
        recorded_failure = video_review.record_failure(
            review_dir,
            batch=n_seq,
            by=actor,
            reason=req_render_err,
            review_id=req_rid,
            receipt_id=rid_key,
            stage="render",
        )
    elif probe_error is not None:
        recorded_failure = video_review.record_failure(
            review_dir,
            batch=n_seq,
            by=actor,
            reason=str(probe_error),
            review_id=req_rid,
            receipt_id=rid_key,
            stage="probe",
        )
    elif req_movie is not None:
        try:
            registered_version = video_review.register_version(
                review_dir,
                req_movie,
                batch=n_seq,
                by=actor,
                review_id=req_rid,
                receipt_id=rid_key,
            )
        except video_review.VideoError as exc:
            probe_error = exc
            recorded_failure = video_review.record_failure(
                review_dir,
                batch=n_seq,
                by=actor,
                reason=str(exc),
                review_id=req_rid,
                receipt_id=rid_key,
                stage="probe",
            )

    # Phase 3: 重取 .notes.lock 二次核验无并发篡改后写回 notes.json 与 handoff.json
    with locked(review_dir):
        if authorize is not None:
            authorize()
        manifest = ledger.read_manifest(review_dir)
        if not _is_video_review(manifest):
            raise HandoffError("只有视频审阅支持 video-receipt", code="no-video")
        if req_rid is not None and manifest.get("reviewId") != req_rid:
            raise HandoffError("审阅标识不匹配，拒绝写入其他审阅", code="review-mismatch")
        doc = handoff_load(review_dir)
        ndoc = load(review_dir)
        item = _find_batch(doc["items"], n_seq)
        if replay := _find_receipt_replay(item, rid_key):
            return _replay_receipt_response(manifest, item, replay, ndoc["notes"])
        current_token = (
            int(ndoc.get("rev", 0)),
            doc.get("updatedAt"),
            item.get("status"),
            item.get("claimedAt"),
            item.get("updates"),
            len(doc["items"]),
        )
        if current_token != lock_token or item.get("status") != "claimed":
            raise HandoffError("批注或批次状态在交回过程中发生了并发变化，请重试", code="receipt-conflict", seq=n_seq)

        now = now_iso()
        by_id = {n.get("id"): n for n in ndoc["notes"]}

        if req_render_err or probe_error is not None:
            fail_reason = req_render_err or str(probe_error)
            fail_entries = list(entries)
            if not fail_entries:
                for nid in item.get("openIds") or []:
                    note = by_id.get(nid)
                    if isinstance(note, dict) and note.get("status", "open") == "open":
                        fail_entries.append({"noteId": nid, "outcome": "failed", "failure": fail_reason})
            rec_items = []
            touched_notes = []
            for entry in fail_entries:
                note = by_id[entry["noteId"]]
                _validate_receipt_note(note, entry["noteId"], item, n_seq)
                rec_entry = _apply_video_receipt_to_note(
                    note,
                    entry={**entry, "outcome": "failed", "failure": entry.get("failure") or fail_reason},
                    n_seq=n_seq,
                    receipt_id=rid_key,
                    by=actor,
                    now=now,
                    version_meta=None,
                    cleaned_target=None,
                )
                rec_items.append({"noteId": entry["noteId"], **rec_entry})
                touched_notes.append(note)
            item["videoFailure"] = recorded_failure
            item.setdefault("videoReceipts", []).append(
                {
                    "receiptId": rid_key,
                    "at": now,
                    "by": actor,
                    "versionId": None,
                    "summary": req_summary,
                    "failure": recorded_failure,
                    "items": rec_items,
                }
            )
            in_flight_set = {
                x for it in doc["items"] if it.get("status") in IN_FLIGHT for x in (it.get("openIds") or [])
            }
            item["videoStats"] = _video_batch_stats(ndoc["notes"], item, in_flight=in_flight_set)
            if req_summary:
                item["summary"] = req_summary
            if touched_notes:
                _commit(
                    review_dir,
                    ndoc,
                    {"op": "video-receipt-failed", "by": actor, "seq": n_seq, "receiptId": rid_key},
                )
            _handoff_commit(
                review_dir,
                doc,
                {"op": "handoff-video-failure", "by": actor, "seq": n_seq, "receiptId": rid_key},
            )
            if probe_error is not None:
                raise HandoffError(
                    f"新版成片不可播放或无法识别：{probe_error}",
                    code="video-unplayable",
                    seq=n_seq,
                )
            return {
                "ok": True,
                "replayed": False,
                "seq": n_seq,
                "status": item["status"],
                "receiptId": rid_key,
                "version": None,
                "failure": recorded_failure,
                "batch": copy.deepcopy(item),
                "notes": copy.deepcopy(touched_notes),
            }

        rec_items = []
        touched_notes = []
        for entry in entries:
            nid = entry["noteId"]
            note = by_id[nid]
            _validate_receipt_note(note, nid, item, n_seq)
            version_meta = None
            cleaned_target = None
            if entry["outcome"] in ("fixed", "deleted"):
                target_vid = (
                    entry.get("versionId")
                    or (entry["target"].get("versionId") if isinstance(entry["target"], dict) else None)
                    or (registered_version["versionId"] if registered_version else item.get("videoVersionId"))
                )
                if target_vid:
                    try:
                        version_meta = (
                            registered_version
                            if registered_version and registered_version["versionId"] == target_vid
                            else video_review.registered_version(manifest, str(target_vid))
                        )
                    except video_review.VideoError as exc:
                        raise HandoffError(str(exc), code="invalid-receipt-target", id=nid) from None
                if entry["target"] is not None and version_meta is not None:
                    try:
                        cleaned_target = video_review.clean_receipt_target(entry["target"], version_meta)
                    except video_review.VideoError as exc:
                        raise HandoffError(str(exc), code="invalid-receipt-target", id=nid) from None
            rec_entry = _apply_video_receipt_to_note(
                note,
                entry=entry,
                n_seq=n_seq,
                receipt_id=rid_key,
                by=actor,
                now=now,
                version_meta=version_meta,
                cleaned_target=cleaned_target,
            )
            rec_items.append({"noteId": nid, **rec_entry})
            touched_notes.append(note)

        if registered_version is not None:
            item["videoVersionId"] = registered_version["versionId"]
            item.pop("videoFailure", None)
        item.setdefault("videoReceipts", []).append(
            {
                "receiptId": rid_key,
                "at": now,
                "by": actor,
                "versionId": registered_version["versionId"] if registered_version else item.get("videoVersionId"),
                "summary": req_summary,
                "failure": None,
                "items": rec_items,
            }
        )
        if req_summary:
            item["summary"] = req_summary

        mine = [n for n in ndoc["notes"] if n.get("status") == "drafted" and n.get("draftBatch") == n_seq]
        item["draftedIds"] = [n["id"] for n in mine]
        item["draftedCount"] = len(mine)
        stage_entry = _stage_entry(item, "draft")
        stage_entry.update(draftedIds=[n["id"] for n in mine])
        if req_summary:
            stage_entry["summary"] = req_summary

        if finish:
            stage_entry.update(doneAt=now, doneBy=actor)
            if mine:
                item["status"] = "drafted"
                item["resolvedIds"] = []
                item["resolvedCount"] = 0
                item["leftOpen"] = sum(1 for x in item.get("openIds") or [] if x in _open_ids(ndoc["notes"]))
            else:
                _finish(item, ndoc["notes"], doc["items"], actor, req_summary)
        in_flight_set = {x for it in doc["items"] if it.get("status") in IN_FLIGHT for x in (it.get("openIds") or [])}
        item["videoStats"] = _video_batch_stats(ndoc["notes"], item, in_flight=in_flight_set)

        if touched_notes:
            _commit(
                review_dir,
                ndoc,
                {
                    "op": "video-receipt",
                    "by": actor,
                    "seq": n_seq,
                    "receiptId": rid_key,
                    "ids": [n["id"] for n in touched_notes],
                },
            )
        _handoff_commit(
            review_dir,
            doc,
            {
                "op": "handoff-video-receipt",
                "by": actor,
                "seq": n_seq,
                "receiptId": rid_key,
                "status": item["status"],
                "versionId": item.get("videoVersionId"),
            },
        )
        return {
            "ok": True,
            "replayed": False,
            "seq": n_seq,
            "status": item["status"],
            "receiptId": rid_key,
            "version": registered_version,
            "failure": None,
            "batch": copy.deepcopy(item),
            "notes": copy.deepcopy(touched_notes),
        }


VIDEO_DECISION_ACTIONS = ("approve", "return", "reply")


def _check_expected_video_context(
    note_id: str,
    *,
    cur_batch: int | None,
    cur_version: str | None,
    expected_batch: int | None,
    expected_version: str | None,
    decided: bool = False,
) -> None:
    code = "note-already-decided" if decided else "stale-decision"
    if expected_batch is not None and cur_batch != expected_batch:
        raise HandoffError(
            f"批注 {note_id} 的批次已变化（期望第 {expected_batch} 批，当前第 {cur_batch or '—'} 批），请刷新后再操作",
            code=code,
            id=note_id,
            expected=expected_batch,
            actual=cur_batch,
        )
    if expected_version is not None and cur_version != expected_version:
        raise HandoffError(
            f"批注 {note_id} 的版本已变化（期望 {expected_version}，当前 {cur_version or '—'}），请刷新后再操作",
            code=code,
            id=note_id,
            expected=expected_version,
            actual=cur_version,
        )


def video_decision(
    review_dir: Path,
    note_id: str,
    *,
    action: str,
    reason: str = "",
    reply: str = "",
    expected_batch: int | None = None,
    expected_version: str | None = None,
    by: str = "page",
    authorize: Callable[[], None] | None = None,
) -> dict[str, Any]:
    """视频批注的人工决定：approve 通过 / return 退回写明原因 / reply 答复 Agent 提问（不自动提交）。"""
    if action not in VIDEO_DECISION_ACTIONS:
        raise HandoffError(
            f"视频人工决定 action 须为 {' / '.join(VIDEO_DECISION_ACTIONS)} 之一",
            code="bad-video-decision",
            id=note_id,
        )
    if expected_batch is not None and (
        isinstance(expected_batch, bool) or not isinstance(expected_batch, int) or expected_batch < 1
    ):
        raise HandoffError("expected_batch 须为正整数", code="bad-video-decision", id=note_id)
    if expected_version is not None and (
        not isinstance(expected_version, str) or not video_review.VERSION_ID.fullmatch(expected_version)
    ):
        raise HandoffError("expected_version 无效", code="bad-video-decision", id=note_id)
    actor = str(by or "").strip()[:200] or "page"
    if action == "approve" and actor != "page":
        raise HandoffError(
            "视频批注只能由审阅人在页面复核通过，Agent 不能直接标为已处理",
            code="video-manual-approval-required",
            id=note_id,
        )
    clean_reason = fix_text(reason).strip()[:MAX_TEXT]
    clean_reply = fix_text(reply).strip()[:MAX_TEXT]
    if action == "return" and not clean_reason:
        raise HandoffError("退回视频批注需要写明退回原因", code="video-return-reason-required", id=note_id)
    if action == "reply" and not clean_reply:
        raise HandoffError("答复 Agent 提问时内容不能为空", code="video-reply-required", id=note_id)

    with locked(review_dir):
        if authorize is not None:
            authorize()
        manifest = ledger.read_manifest(review_dir)
        ndoc = load(review_dir)
        note = _find(ndoc, note_id)
        if not _is_video_review(manifest, note):
            raise HandoffError("只有视频批注支持视频人工复核决定", code="no-video", id=note_id)
        if note.get("status") == "held":
            raise HandoffError(f"{note_id} 已被分析搁住，不能执行复核决定", code="note-held", id=note_id)
        handoff_doc = handoff_load(review_dir)
        vr = note.get("videoReview") if isinstance(note.get("videoReview"), dict) else None
        is_approved = note.get("status") == "resolved" or (isinstance(vr, dict) and vr.get("decision") == "approved")

        if is_approved:
            if action != "approve":
                raise HandoffError(
                    f"批注 {note_id} 已有人工通过决定，不能被退回或改写",
                    code="note-already-decided",
                    id=note_id,
                )
            rec_batch = _video_batch_seq(note, vr)
            rec_version = (vr.get("versionId") if isinstance(vr, dict) else None) or (
                (note.get("video") or {}).get("versionId") if isinstance(note.get("video"), dict) else None
            )
            _check_expected_video_context(
                note_id,
                cur_batch=rec_batch,
                cur_version=rec_version,
                expected_batch=expected_batch,
                expected_version=expected_version,
                decided=True,
            )
            return {
                "ok": True,
                "replayed": True,
                "doc": copy.deepcopy(ndoc),
                "note": copy.deepcopy(note),
                "handoff": handoff_state(review_dir, full=False, handoff=handoff_doc, notes=ndoc["notes"]),
            }

        if not isinstance(vr, dict):
            raise HandoffError(
                f"视频批注 {note_id} 尚未交回新版复核结果",
                code="video-not-ready",
                id=note_id,
                reason="unsubmitted",
            )

        cur_batch = _video_batch_seq(note, vr)
        batch_item = (
            next((i for i in handoff_doc["items"] if int(i.get("seq", 0) or 0) == cur_batch), None)
            if cur_batch is not None
            else None
        )
        cur_version = vr.get("versionId") or (batch_item.get("videoVersionId") if batch_item else None)
        if action != "approve" and not cur_version and isinstance(manifest.get("video"), dict):
            cur_version = manifest["video"].get("currentVersionId")

        if action == "approve":
            _check_expected_video_context(
                note_id,
                cur_batch=cur_batch,
                cur_version=cur_version,
                expected_batch=expected_batch,
                expected_version=expected_version,
            )
            ready, not_ready_reason = video_review.review_readiness(note, manifest, review_dir=review_dir)
            if not ready:
                raise HandoffError(
                    f"视频批注 {note_id} 尚未满足人工复核通过条件（{not_ready_reason}）",
                    code="video-not-ready",
                    id=note_id,
                    reason=not_ready_reason,
                )
            now = now_iso()
            _mark_resolved(note, by=actor, resolution=note.get("resolution", ""), batch=cur_batch)
            _record_video_approval(note, by=actor, batch=cur_batch, now=now)
            note["updatedAt"] = now
            _trail(note, by=actor, stage="page" if actor == "page" else None, batch=cur_batch, action="accepted")
            ndoc = _commit(
                review_dir,
                ndoc,
                {"op": "video-decision", "by": actor, "id": note_id, "action": "approve", "batch": cur_batch},
            )
            handoff_doc = _sync_batch_for_video_decision(
                review_dir,
                handoff_doc,
                ndoc,
                batch_seq=cur_batch,
                by=actor,
                op="handoff-video-approve",
                note_id=note_id,
            )
            return {
                "ok": True,
                "replayed": False,
                "doc": copy.deepcopy(ndoc),
                "note": copy.deepcopy(note),
                "handoff": handoff_state(review_dir, full=False, handoff=handoff_doc, notes=ndoc["notes"]),
            }

        if action == "return":
            if (
                vr.get("state") == "returned"
                and vr.get("decision") == "returned"
                and vr.get("returnReason") == clean_reason
                and note.get("status", "open") == "open"
            ):
                _check_expected_video_context(
                    note_id,
                    cur_batch=cur_batch,
                    cur_version=cur_version,
                    expected_batch=expected_batch,
                    expected_version=expected_version,
                )
                return {
                    "ok": True,
                    "replayed": True,
                    "doc": copy.deepcopy(ndoc),
                    "note": copy.deepcopy(note),
                    "handoff": handoff_state(review_dir, full=False, handoff=handoff_doc, notes=ndoc["notes"]),
                }
            _check_expected_video_context(
                note_id,
                cur_batch=cur_batch,
                cur_version=cur_version,
                expected_batch=expected_batch,
                expected_version=expected_version,
            )
            now = now_iso()
            vr["state"] = "returned"
            vr["decision"] = "returned"
            if cur_batch is not None:
                vr["decisionBatch"] = cur_batch
            vr["decidedAt"] = now
            vr["decidedBy"] = actor
            vr["returnReason"] = clean_reason
            _record_video_decision_history(
                vr,
                action="return",
                decision="returned",
                state="returned",
                by=actor,
                now=now,
                batch=cur_batch,
                reason=clean_reason,
            )
            note["status"] = "open"
            for k in RESOLVE_FIELDS + DRAFT_FIELDS:
                note.pop(k, None)
            note["videoReview"] = vr
            note["updatedAt"] = now
            _trail(
                note,
                by=actor,
                stage="page" if actor == "page" else None,
                batch=cur_batch,
                action="returned",
                note=clean_reason,
            )
            ndoc = _commit(
                review_dir,
                ndoc,
                {"op": "video-decision", "by": actor, "id": note_id, "action": "return", "batch": cur_batch},
            )
            handoff_doc = _sync_batch_for_video_decision(
                review_dir,
                handoff_doc,
                ndoc,
                batch_seq=cur_batch,
                by=actor,
                op="handoff-video-return",
                note_id=note_id,
            )
            return {
                "ok": True,
                "replayed": False,
                "doc": copy.deepcopy(ndoc),
                "note": copy.deepcopy(note),
                "handoff": handoff_state(review_dir, full=False, handoff=handoff_doc, notes=ndoc["notes"]),
            }

        # action == "reply"
        if note.get("status") == "drafted" or vr.get("state") == "pending_review":
            raise HandoffError(
                f"视频批注 {note_id} 当前不在待答复状态",
                code="video-not-awaiting-reply",
                id=note_id,
            )
        if vr.get("decision") == "replied" and vr.get("reply") == clean_reply:
            _check_expected_video_context(
                note_id,
                cur_batch=cur_batch,
                cur_version=cur_version,
                expected_batch=expected_batch,
                expected_version=expected_version,
            )
            return {
                "ok": True,
                "replayed": True,
                "doc": copy.deepcopy(ndoc),
                "note": copy.deepcopy(note),
                "handoff": handoff_state(review_dir, full=False, handoff=handoff_doc, notes=ndoc["notes"]),
            }
        _check_expected_video_context(
            note_id,
            cur_batch=cur_batch,
            cur_version=cur_version,
            expected_batch=expected_batch,
            expected_version=expected_version,
        )
        now = now_iso()
        vr["reply"] = clean_reply
        vr["repliedAt"] = now
        vr["repliedBy"] = actor
        vr["decision"] = "replied"
        if cur_batch is not None:
            vr["decisionBatch"] = cur_batch
        vr["decidedAt"] = now
        vr["decidedBy"] = actor
        _record_video_decision_history(
            vr,
            action="reply",
            decision="replied",
            state=str(vr.get("state") or "needs_reply"),
            by=actor,
            now=now,
            batch=cur_batch,
            reply=clean_reply,
        )
        note["videoReview"] = vr
        note["updatedAt"] = now
        _trail(
            note,
            by=actor,
            stage="page" if actor == "page" else None,
            batch=cur_batch,
            action="replied",
            note=clean_reply,
        )
        ndoc = _commit(
            review_dir,
            ndoc,
            {"op": "video-decision", "by": actor, "id": note_id, "action": "reply", "batch": cur_batch},
        )
        return {
            "ok": True,
            "replayed": False,
            "doc": copy.deepcopy(ndoc),
            "note": copy.deepcopy(note),
            "handoff": handoff_state(review_dir, full=False, handoff=handoff_doc, notes=ndoc["notes"]),
        }


def relocate_video_note(
    review_dir: Path,
    note_id: str,
    *,
    target: Any,
    version_id: str | None = None,
    summary: str | None = None,
    reason: str | None = None,
    by: str = "page",
    authorize: Callable[[], None] | None = None,
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    """审阅人人工校对或补齐视频批注的新版位置（在 .notes.lock 内完成，绝不嵌套 metadata_lock）。"""
    actor = str(by or "").strip()[:200] or "page"
    if version_id is not None and (
        not isinstance(version_id, str) or not video_review.VERSION_ID.fullmatch(version_id)
    ):
        raise HandoffError("目标版本标识无效", code="invalid-receipt-target", id=note_id)
    if not isinstance(target, dict):
        raise HandoffError("回执位置须为对象", code="invalid-receipt-target", id=note_id)

    with locked(review_dir):
        if authorize is not None:
            authorize()
        manifest = ledger.read_manifest(review_dir)
        if not _is_video_review(manifest):
            raise HandoffError("只有视频审阅支持视频重定位", code="no-video")
        ndoc = load(review_dir)
        note = _find(ndoc, note_id)
        if not _is_video_review(manifest, note):
            raise HandoffError("只有视频批注支持视频重定位", code="no-video", id=note_id)
        if note.get("status") == "held":
            raise HandoffError(f"{note_id} 已被分析搁住，不能重定位", code="note-held", id=note_id)
        vr = note.get("videoReview") if isinstance(note.get("videoReview"), dict) else None
        if note.get("status") == "resolved" or (vr and vr.get("decision") == "approved"):
            raise HandoffError(f"批注 {note_id} 已有人工决定，不能重定位", code="note-already-decided", id=note_id)

        if version_id is not None:
            if target.get("versionId") is not None and target.get("versionId") != version_id:
                raise HandoffError(
                    f"回执位置 versionId（{target.get('versionId')}）与目标版本 {version_id} 不符",
                    code="invalid-receipt-target",
                    id=note_id,
                )
            target_vid = version_id
        else:
            target_vid = (
                target.get("versionId")
                or (vr.get("versionId") if vr else None)
                or ((manifest.get("video") or {}).get("currentVersionId"))
            )
        try:
            version_meta = video_review.registered_version(manifest, str(target_vid or ""))
            cleaned_target = video_review.clean_receipt_target(target, version_meta)
        except video_review.VideoError as exc:
            raise HandoffError(str(exc), code="invalid-receipt-target", id=note_id) from None

        clean_summary = _receipt_str(summary) if summary is not None else ""
        existing_summary = _receipt_str(vr.get("summary")) if vr else ""
        final_summary = clean_summary if summary is not None else existing_summary
        clean_reason = _receipt_str(reason) if reason is not None else ""

        now = now_iso()
        prev_vr = copy.deepcopy(vr) if vr else None
        prev_state = (
            prev_vr.get("state")
            if prev_vr
            else ("missing_evidence" if note.get("status") == "drafted" else "unsubmitted")
        )
        prev_ver = (
            prev_vr.get("versionId")
            if prev_vr and prev_vr.get("versionId")
            else ((note.get("video") or {}).get("versionId") if isinstance(note.get("video"), dict) else None)
        )
        prev_target = (
            copy.deepcopy(prev_vr.get("target")) if prev_vr and isinstance(prev_vr.get("target"), dict) else None
        )
        prev_reason = prev_vr.get("relocateReason") if prev_vr else None
        prev_summary = prev_vr.get("summary") if prev_vr else None
        batch_seq = (
            prev_vr.get("batch")
            if prev_vr and isinstance(prev_vr.get("batch"), int) and not isinstance(prev_vr.get("batch"), bool)
            else (
                note.get("draftBatch")
                if isinstance(note.get("draftBatch"), int) and not isinstance(note.get("draftBatch"), bool)
                else None
            )
        )
        had_batch = (batch_seq is not None) or (note.get("status") == "drafted")
        next_state = "pending_review" if final_summary else "missing_evidence"

        history = list(prev_vr.get("history") or []) if prev_vr and isinstance(prev_vr.get("history"), list) else []
        hist_entry: dict[str, Any] = {
            "source": "relocate",
            "action": "relocate",
            "at": now,
            "by": actor,
            "state": next_state,
            "versionId": version_meta["versionId"],
            "target": cleaned_target,
            "previousState": prev_state,
        }
        if batch_seq is not None:
            hist_entry["batch"] = batch_seq
        if prev_ver:
            hist_entry["previousVersionId"] = prev_ver
        if prev_target is not None:
            hist_entry["previousTarget"] = prev_target
        if prev_reason:
            hist_entry["previousReason"] = prev_reason
        if final_summary:
            hist_entry["summary"] = final_summary
        if clean_reason:
            hist_entry["reason"] = clean_reason
        history.append(hist_entry)

        if prev_vr and isinstance(prev_vr.get("relocatedFrom"), dict):
            relocated_from = copy.deepcopy(prev_vr["relocatedFrom"])
        else:
            relocated_from = {"state": prev_state, "at": now, "by": actor}
            if prev_ver:
                relocated_from["versionId"] = prev_ver
            if prev_target is not None:
                relocated_from["target"] = prev_target
            if prev_reason:
                relocated_from["relocateReason"] = prev_reason
            if prev_summary:
                relocated_from["summary"] = prev_summary
            if prev_vr and prev_vr.get("question"):
                relocated_from["question"] = prev_vr["question"]
            if prev_vr and prev_vr.get("failure"):
                relocated_from["failure"] = prev_vr["failure"]
            if prev_vr and prev_vr.get("returnReason"):
                relocated_from["returnReason"] = prev_vr["returnReason"]

        new_vr: dict[str, Any] = {
            "v": 1,
            "state": next_state,
            "versionId": version_meta["versionId"],
            "target": cleaned_target,
            "by": actor,
            "at": now,
            "relocatedFrom": relocated_from,
            "history": history[-VIDEO_HISTORY_KEEP:],
        }
        if batch_seq is not None:
            new_vr["batch"] = batch_seq
        if prev_vr and prev_vr.get("receiptId"):
            new_vr["receiptId"] = prev_vr["receiptId"]
        if prev_vr and prev_vr.get("outcome"):
            new_vr["outcome"] = prev_vr["outcome"]
        if final_summary:
            new_vr["summary"] = final_summary
        if clean_reason:
            new_vr["relocatedReason"] = clean_reason

        if next_state == "pending_review" and had_batch:
            for k in RESOLVE_FIELDS:
                note.pop(k, None)
            note.update(status="drafted", draftedAt=now, draftedBy=actor, resolution=final_summary)
            if batch_seq is not None:
                note["draftBatch"] = batch_seq
        else:
            note["status"] = "open"
            for k in RESOLVE_FIELDS + DRAFT_FIELDS:
                note.pop(k, None)

        note["videoReview"] = new_vr
        note["updatedAt"] = now
        trail_note = clean_reason or final_summary or None
        _trail(
            note,
            by=actor,
            stage="page" if actor == "page" else None,
            batch=batch_seq,
            action="relocated",
            note=trail_note,
        )
        ndoc = _commit(
            review_dir,
            ndoc,
            {
                "op": "video-relocate",
                "by": actor,
                "id": note_id,
                "versionId": version_meta["versionId"],
                "state": next_state,
            },
        )
        handoff_doc = handoff_load(review_dir)
        if batch_seq is not None:
            item = next(
                (i for i in handoff_doc["items"] if int(i.get("seq", 0) or 0) == batch_seq),
                None,
            )
            if item is not None:
                mine = [n for n in ndoc["notes"] if n.get("status") == "drafted" and n.get("draftBatch") == batch_seq]
                in_flight_set = {
                    x for it in handoff_doc["items"] if it.get("status") in IN_FLIGHT for x in (it.get("openIds") or [])
                }
                item["draftedIds"] = [n["id"] for n in mine]
                item["draftedCount"] = len(mine)
                if item.get("status") == "done" and mine:
                    item["status"] = "drafted"
                    item["resolvedIds"] = []
                    item["resolvedCount"] = 0
                    item["leftOpen"] = sum(1 for x in item.get("openIds") or [] if x in _open_ids(ndoc["notes"]))
                item["videoStats"] = _video_batch_stats(ndoc["notes"], item, in_flight=in_flight_set)
                handoff_doc = _handoff_commit(
                    review_dir,
                    handoff_doc,
                    {
                        "op": "handoff-video-relocate",
                        "by": actor,
                        "seq": batch_seq,
                        "id": note_id,
                        "state": next_state,
                    },
                )
        return ndoc, note, handoff_doc


# ---------- 重建后校正页码：DOCX 先按段落，其余按页面键 ----------
def _remap_docx(n: dict[str, Any], ctx: dict[str, Any], new_titles: dict[int, str], result: dict[str, Any]) -> bool:
    """按段落定位把一条批注归到段落现在所在的页；返回这条批注有没有被改动。"""
    anchor, page = n["docx"], int(n.get("page", 0) or 0)
    got = docx_review.relocate(anchor, ctx["paragraphs"])
    before = _dumps(n)
    if got["status"] == "ok":
        to = got["page"]
        if to != page:
            result["moved"].append({"id": n.get("id"), "from": page, "to": to, "key": ctx["keys"].get(to, "")})
            n["page"] = to
        if to in new_titles:  # 页码没变，页首也可能换了段：标题与键一起跟上
            n["title"] = new_titles[to]
        if ctx["keys"].get(to):
            n["key"] = ctx["keys"][to]
        n.pop("orphan", None)
        anchor["para"]["n"] = got["para"]["n"]
        if "endPara" in got:
            anchor["endPara"]["n"] = got["endPara"]["n"]
        if got["changed"]:
            anchor["changed"] = True  # 段落还在但文字变了：偏移与摘要仍是旧版本的，指纹不更新
        else:
            anchor.pop("changed", None)
            if ctx.get("fingerprint"):
                anchor["fp"] = ctx["fingerprint"]
    else:
        n["orphan"] = True
        result["orphans"].append({"id": n.get("id"), "page": page, "key": n.get("key"), "reason": got["status"]})
    touched = _dumps(n) != before
    # 需要说明的情况只在这条批注这次确实被改动时说一遍；找不到的每次都说，直到用户确认
    if got["status"] != "ok" or (touched and (got["why"] or got["changed"])):
        result["docx"].append(
            {
                "id": n.get("id"),
                "status": got["status"],
                "how": got["how"],
                "why": got["why"],
                "changed": got["changed"],
                "page": n.get("page"),
            }
        )
    return touched


def _remap_pptx(n: dict[str, Any], ctx: dict[str, Any], new_titles: dict[int, str], result: dict[str, Any]) -> bool:
    """按幻灯片定位把一条批注归到目标现在所在的页；返回这条批注有没有被改动。"""
    anchor, page = n["pptx"], int(n.get("page", 0) or 0)
    got = pptx_reader.relocate(anchor, ctx["slides"])
    before = _dumps(n)
    if got["status"] == "ok":
        to = got["page"]
        if to != page:
            result["moved"].append({"id": n.get("id"), "from": page, "to": to, "key": ctx["keys"].get(to, "")})
            n["page"] = to
        if to in new_titles:  # 页码没变，标题也可能换了
            n["title"] = new_titles[to]
        if ctx["keys"].get(to):
            n["key"] = ctx["keys"][to]
        n.pop("orphan", None)
        if got["targets"]:  # 元素身份刷新成当前版本（重命名、顺序与文本变化后仍指同一对象）
            anchor["elements"] = got["targets"]
        if got["text"] is not None:
            anchor["text"]["para"] = got["text"]["para"]
        if got["changed"]:
            anchor["changed"] = True  # 元素还在但文字变了：偏移与指纹仍是旧版本的
        else:
            anchor.pop("changed", None)
            if ctx.get("fingerprint"):
                anchor["fp"] = ctx["fingerprint"]
    else:
        n["orphan"] = True
        result["orphans"].append({"id": n.get("id"), "page": page, "key": n.get("key"), "reason": got["why"]})
    touched = _dumps(n) != before
    # 需要说明的情况只在这条批注这次确实被改动时说一遍；找不到的每次都说，直到用户确认
    if got["status"] != "ok" or (touched and got["changed"]):
        result["pptx"].append(
            {
                "id": n.get("id"),
                "status": got["status"],
                "why": got["why"],
                "changed": got["changed"],
                "page": n.get("page"),
            }
        )
    return touched


def remap_pages(
    review_dir: Path,
    old_keys: dict[int, str],
    new_pages: dict[str, int],
    new_titles: dict[int, str],
    ambiguous: set[str] | None = None,
    docx: dict[str, Any] | None = None,
    pptx: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """生成对照页时调用：old_keys 是上一版 review.json 的「页码 → 键」，new_pages 是新版的「键 → 页码」。

    DOCX 审阅另传 docx = {"paragraphs": 新版段落索引, "fingerprint": 源文件指纹, "keys": {页码: 键}}：
    带段落定位（本版本读得懂的 docx 字段）的批注先按段落找回现在所在的页，同步 page / key / title；
    段落找不到或无法确认时标 orphan、页码不动，不再回落到页面键（页面是排版产物，页首会换段）。
    PPT 审阅另传 pptx = {"slides": 新版幻灯片模型, "fingerprint": 源文件指纹, "keys": {页码: 键}}：
    带幻灯片定位（本版本读得懂的 pptx 字段）的批注按稳定幻灯片和元素身份找回目标（元素重命名、
    顺序与文本变化后仍指同一对象），目标消失或证据冲突时标 orphan、页码不动，不猜相近对象。
    其余批注按页面键：
    - 没有 key 的旧批注，按上一版的页码补上 key（批注是对着上一版写的）；
    - 有 key 且新版里还在：页码改成新页码；
    - key 在新版里找不到：标 orphan，页码不动，等用户或代理确认。
    返回 {"moved": [...], "orphans": [...], "keyed": n, "docx": [...], "pptx": [按幻灯片定位时需要说明的情况]}。
    """
    ambiguous = ambiguous or set()
    result: dict[str, Any] = {"moved": [], "orphans": [], "keyed": 0, "docx": [], "pptx": []}
    if not (review_dir / NOTES_FILE).exists():
        return result
    with locked(review_dir):
        doc = load(review_dir)
        changed = False
        for n in doc["notes"]:
            if pptx is not None and usable_pptx_anchor(n.get("pptx")):
                changed = _remap_pptx(n, pptx, new_titles, result) or changed
                continue
            if docx is not None and docx_review.usable_anchor(n.get("docx")):
                changed = _remap_docx(n, docx, new_titles, result) or changed
                continue
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
            _commit(
                review_dir,
                doc,
                {
                    "op": "remap",
                    "by": "build",
                    "moved": len(result["moved"]),
                    "orphans": len(result["orphans"]),
                    "keyed": result["keyed"],
                },
            )
    return result
