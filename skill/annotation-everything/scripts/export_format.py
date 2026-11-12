"""批注摘要的交换格式：带版本与语言标记的 Markdown，以及配套的结构化 JSON 备份。

页面（`viewer_template.html`）和命令行（`notes.py`）各有一份实现，遵守同一份规范：

- 第一行是格式标记，声明格式版本与内容语言。解析按标记选文法，与导入界面的语言无关；
  普通界面文案改动不会改变已发布的交换文法（文法里的字面量写死在本模块，不从 locales 词典取）。
- 中文文法就是 0.3 起就有的那份（`## 第 N 页｜…`、`1. 【…】`），只多一个标记行，
  所以旧工具读新的中文导出与读旧导出没有区别。
- 英文文法是中文文法的对照翻译，只翻译系统包装文字；用户写的正文、引用、处理说明、
  技能名称保持原文，存储值（`报告` / `讲稿` / `all` / `style`、`open` / `drafted` / `resolved`）
  在解析时还原成中文原值，因此中文导出和英文导出落库的结果一致。
- 时间戳写本地时间的 `YYYY-MM-DD HH:MM:SS`，文件名里的时间写紧凑 UTC；都不随界面语言改变格式。
- 坐标、页码、编号都是十进制数字，不加千分位、不换算单位。

往返范围（与 `references/formats.md` 一致）：页码与页标题、对象、正文（含多行）、状态、
点与框的坐标、引用与引用来源、处理说明、风格优化范围、位置线索、DOCX 段落定位（v1）、
PPT 幻灯片定位（v1）、本条指定的技能、另一侧对应句，以及开头的常驻技能一行。
痕迹、孤立页警告、页面键不一致警告、「常驻·范围」这类来源标签不进交换格式（它们在
`notes.py show` 的阅读输出里）。
"""
from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any

import notes_store as store
import pptx_reader

FORMAT_VERSION = 1
LANGS = ("zh-CN", "en")

MARKER_RE = re.compile(r"^<!--\s*annotation-everything\s+export\s+v(\d+)\s+lang=([A-Za-z0-9_-]+)\s*-->\s*$")

# 快捷修改的固定说法：正文是这两句时按结构化 edit 还原（值固定中文，两种语言共用）
DELETE_TEXT = store.DELETE_TEXT
REPLACE_PREFIX = store.REPLACE_PREFIX

# 存储值 → 英文显示名。只用于导出的可读标签，导入时按同一张表还原，不从界面词典取。
TARGET_EN = {"报告": "Report", "讲稿": "Script", "报告和讲稿": "Report and script"}
SCOPE_EN = {"all": "All notes", "报告": "Report", "讲稿": "Script", "style": "Style"}
STYLE_EN = {"page": "Whole page", "pin": "Pin", "region": "Region"}
MATCH_EN = {"exact": "identical", "similar": "similar"}

TARGET_ZH = {v: k for k, v in TARGET_EN.items()}
SCOPE_ZH = {"All notes": "all", "Report": "报告", "Script": "讲稿", "Style": "style"}
STYLE_ZH = {v: k for k, v in STYLE_EN.items()}
MATCH_ZH = {"identical": "exact", "similar": "similar"}

# 常驻技能范围：中文用法与 0.3 以来一致（ALWAYS_ZH），英文用上表的对照名。
ALWAYS_ZH_LABEL = store.ALWAYS_ZH
ALWAYS_ZH_REV = {"全部批注": "all", "全部": "all", "报告": "报告", "讲稿": "讲稿", "风格优化": "style"}

HEAD_NOTE = "报告上的标注：标点是一个点，框选是一块区域；坐标为相对页面左上角的百分比（横向 x、纵向 y，宽、高）。"
HEAD_NOTE_EN = ("Marks on the report: a pin is a point and a region is a box; coordinates are percentages "
                "from the top-left corner of the page (x across, y down, width, height).")

# 中文标注格式（标点、框选）与风格标签：导出 Markdown 与 notes.py show 的阅读输出共用这一份定义
# （show 的中文侧从这里取，英文侧仍走界面词典）。改这里的间距或标点会同时改变两边，正是目的：
# 代理对照着读导出内容时，看到的标注写法与 show 里完全一致。
PIN_ZH = "·标点 {no}（x {x}%，y {y}%）"
REGION_ZH = "·框选 {no}（x {x}%，y {y}%，宽 {w}%，高 {h}%）"
STYLE_LABEL_ZH = {"page": "整页", "pin": "标点处", "region": "框选处"}

# DOCX 段落定位一行（只有 DOCX 审阅的批注才有；已发布的版本都没有 DOCX 审阅，不存在旧工具读这行的情况）。
# 存储值 → 标签：定位种类与定位来源。更高版本的定位只写版本号，完整内容在 JSON 备份里。
DOCX_KIND_ZH = {"quote": "引用", "pin": "标点", "region": "框选"}
DOCX_VIA_ZH = {"selection": "选区", "text": "文本匹配", "point": "标注处"}
DOCX_VIA_EN = {"selection": "selection", "text": "text match", "point": "mark"}
_PID = r"([0-9A-Fa-f]{1,8}|-)"
_DG = r"([0-9a-f]{12})"

# PPT 幻灯片定位一行（只有 PPT 审阅的批注才有；与 DOCX 段落定位同层并列）。
# 存储值 → 标签：定位种类与定位来源；元素目标只写身份（key、形状标识、类型、层序、证据摘要、
# 只读限制），不写元素名——名称会变、不是身份，重定位时刷新成当前版本，与 DOCX 段落定位
# 不携带段落文本同理。更高版本的定位只写版本号，完整内容在 JSON 备份里。
PPTX_KIND_ZH = {"page": "整页", "elements": "元素", "text": "文本", "point": "点", "region": "区域"}
PPTX_VIA_ZH = {"thumb": "页缩略图", "click": "点选", "selection": "选区", "text": "文本匹配",
               "point": "标注点", "region": "框选"}
PPTX_VIA_EN = {"thumb": "thumbnail", "click": "click", "selection": "selection", "text": "text match",
               "point": "point", "region": "region"}
PPTX_RO_ZH = {"text": "文本", "geometry": "几何", "style": "样式"}
PPTX_RO_ZH_REV = {v: k for k, v in PPTX_RO_ZH.items()}
_PPTX_FP = r"([0-9a-f]{16,64})"

# 对象为空时的固定字面量：文法的一部分，两种语言一样，导入时原样成为 target（与 0.3 以来一致）
UNSET_TARGET = "未指定"

UNKNOWN_FORMAT = ("既不是 JSON，也不是能识别的批注 Markdown。新版导出第一行是格式标记"
                  "（annotation-everything export v1 lang=…）；旧版中文导出（## 第 N 页｜…）"
                  "没有标记也能读，别的文字不按批注处理。")


class FormatError(Exception):
    """导出内容读不了：格式版本或内容语言不认识，或既不是 JSON 也不是能读的 Markdown。"""

    def __init__(self, message: str, *, code: str = "unknown", **params: object):
        super().__init__(message)
        self.code = code
        self.params = params


class Grammar:
    """一种内容语言的导出与导入文法。

    正则按格式版本固定。中文那一份与 0.3 以来的旧格式逐字相同（旧工具照样能读），
    英文那一份是它的对照翻译。
    """

    def __init__(self, lang: str) -> None:
        if lang not in LANGS:
            raise FormatError(f"不支持的内容语言「{lang}」，可选：{'、'.join(LANGS)}", code="language", language=lang)
        self.lang = lang
        en = lang == "en"
        if en:
            self.page = re.compile(r"^##\s*Page\s+(\d+)\s*\|\s*(.*)$")
            # 方括号后容忍零个或一个空格：正文为空时行尾那个空格被工具去掉也不丢整条
            self.item = re.compile(r"^(\d+)\. \[([^\]·]*)(?: · Pin \d+ \(x ([\d.]+)%, y ([\d.]+)%\))?"
                                   r"(?: · Region \d+ \(x ([\d.]+)%, y ([\d.]+)%, w ([\d.]+)%, h ([\d.]+)%\))?\]\s?(.*)$")
            self.quote = re.compile(r"^\s+> Quote(?: \(([^)]*)\))?: (.*)$")
            self.resolution = re.compile(r"^\s+(Resolution|Draft note): (.*)$")
            self.style = re.compile(r"^\s+Style: (Whole page|Pin|Region"
                                    r"|Region \(x ([\d.]+)%, y ([\d.]+)%, w ([\d.]+)%, h ([\d.]+)%\))$")
            self.anchor = re.compile(r"^\s+Position hint: (.*)$")
            self.skills = re.compile(r"^\s+Skills: (.*)$")
            self.pair = re.compile(r"^\s+↔ Other side \((Report|Script)(?: · (identical|similar))?\): (.*)$")
            self.docx = re.compile(r"^\s+DOCX location: v(\d+)(.*)$")
            self.docx_body = re.compile(
                r"^ (quote|pin|region) · paragraph (\d+) \(paraId " + _PID + r", digest " + _DG + r"\)"
                r"(?: · chars (\d+)(?:–(\d+)| onward))?"
                r"(?: · ends in paragraph (\d+) \(paraId " + _PID + r", digest " + _DG + r"\))?"
                r"(?: · run (\d+))?(?: · source (selection|text match|mark))?"
                r"(?: · fingerprint ([0-9a-f]{16,64}))?( · paragraph changed)?$")
            self.pptx = re.compile(r"^\s+PPT location: v(\d+)(.*)$")
            self.pptx_el = re.compile(
                r"([\w~][^\s(,;]*) \(id ([^,()]+), ([a-z]+), z (\d+), digest ([0-9a-f]{64})"
                r"(?:, read-only ([a-z]+(?:, [a-z]+)*))?\)")
            self.pptx_body = re.compile(
                r"^ (?P<kind>page|elements|text|point|region) · slide (?P<slide>\S+)"
                r"(?: · targets (?P<targets>.+?))?"
                r"(?: · paragraph (?P<para>\d+) \(digest (?P<paraDigest>[0-9a-f]{64})\))?"
                r"(?: · chars (?P<start>\d+)–(?P<end>\d+))?"
                r"(?: · runs (?P<runs>\d+–\d+(?:, \d+–\d+)*))?"
                r"(?: · point x (?P<px>[\d.]+)%, y (?P<py>[\d.]+)%)?"
                r"(?: · region x (?P<rx>[\d.]+)%, y (?P<ry>[\d.]+)%, width (?P<rw>[\d.]+)%, height (?P<rh>[\d.]+)%)?"
                r" · source (?P<via>thumbnail|click|selection|text match|point|region)"
                r"(?: · fingerprint (?P<fp>" + _PPTX_FP + r"))?"
                r"(?P<changed> · text changed)?"
                r"(?: · quote (?P<quote>.*))?$")
            self.always = re.compile(r"^Persistent skills \([^)]*\): (.+)$", re.M)
            self.always_item = re.compile(r"^(.+?) \((All notes|Report|Script|Style)\)$")
            self.always_sep = ", "
        else:
            self.page = re.compile(r"^## 第 (\d+) 页｜(.*)$")
            self.item = re.compile(r"^(\d+)\. 【([^】·]*)(?:·标点 \d+（x ([\d.]+)%，y ([\d.]+)%）)?"
                                   r"(?:·框选 \d+（x ([\d.]+)%，y ([\d.]+)%，宽 ([\d.]+)%，高 ([\d.]+)%）)?】(.*)$")
            self.quote = re.compile(r"^\s+> 引用(?:（([^）]*)）)?：(.*)$")
            self.resolution = re.compile(r"^\s+(处理|初改)说明：(.*)$")
            self.style = re.compile(r"^\s+风格优化：(整页|标点处|框选处"
                                    r"|区域（x ([\d.]+)%，y ([\d.]+)%，宽 ([\d.]+)%，高 ([\d.]+)%）)$")
            self.anchor = re.compile(r"^\s+位置线索：(.*)$")
            self.skills = re.compile(r"^\s+技能：(.*)$")
            self.pair = re.compile(r"^\s+↔ 另一侧（(报告|讲稿)(?:·(相同|相近))?）：(.*)$")
            self.docx = re.compile(r"^\s+DOCX 定位：v(\d+)(.*)$")
            self.docx_body = re.compile(
                r"^ (引用|标点|框选) · 段落 (\d+)（paraId " + _PID + r"，摘要 " + _DG + r"）"
                r"(?: · 字符 (\d+)(?:–(\d+)| 起))?"
                r"(?: · 止于段落 (\d+)（paraId " + _PID + r"，摘要 " + _DG + r"）)?"
                r"(?: · 运行 (\d+))?(?: · 来源 (选区|文本匹配|标注处))?"
                r"(?: · 指纹 ([0-9a-f]{16,64}))?( · 段落已改动)?$")
            self.pptx = re.compile(r"^\s+PPT 定位：v(\d+)(.*)$")
            self.pptx_el = re.compile(
                r"([\w~][^\s（、]*)（标识 ([^，()]+)，([a-z]+)，第 (\d+) 层，摘要 ([0-9a-f]{64})"
                r"(?:，只读 ([^）]+))?" + r"）")
            self.pptx_body = re.compile(
                r"^ (?P<kind>整页|元素|文本|点|区域) · 幻灯片 (?P<slide>\S+)"
                r"(?: · 目标 (?P<targets>.+?))?"
                r"(?: · 段落 (?P<para>\d+)（摘要 (?P<paraDigest>[0-9a-f]{64})）)?"
                r"(?: · 字符 (?P<start>\d+)–(?P<end>\d+))?"
                r"(?: · 运行 (?P<runs>\d+–\d+(?:、\d+–\d+)*))?"
                r"(?: · 点 x (?P<px>[\d.]+)%，y (?P<py>[\d.]+)%)?"
                r"(?: · 区域 x (?P<rx>[\d.]+)%，y (?P<ry>[\d.]+)%，宽 (?P<rw>[\d.]+)%，高 (?P<rh>[\d.]+)%)?"
                r" · 来源 (?P<via>页缩略图|点选|选区|文本匹配|标注点|框选)"
                r"(?: · 指纹 (?P<fp>" + _PPTX_FP + r"))?"
                r"(?P<changed> · 文字已改动)?"
                r"(?: · 引用 (?P<quote>.*))?$")
            self.always = re.compile(r"^常驻技能（[^）]*）：(.+)$", re.M)
            self.always_item = re.compile(r"^(.+?)（(全部批注|全部|报告|讲稿|风格优化)）$")
            self.always_sep = "、"
        self.state_prefix = {"resolved": "【已处理】" if not en else "[Resolved] ",
                             "drafted": "【已初改·待复核】" if not en else "[Drafted] "}
        self.status = {prefix: state for state, prefix in self.state_prefix.items()}   # 导入：前缀 → 状态
        self.indent = "   "

    # ---------- 导入 ----------
    def parse(self, text: str) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        page: int | None = None
        title = ""
        cur: dict[str, Any] | None = None
        for line in text.splitlines():
            if m := self.page.match(line):
                page, title, cur = int(m.group(1)), m.group(2).strip(), None
                continue
            if page is None:
                continue
            if m := self.item.match(line):
                body, status = m.group(9), "open"
                for prefix, st in self.status.items():
                    if body.startswith(prefix):
                        body, status = body[len(prefix):], st
                        break
                cur = {"page": page, "title": title, "target": self._target_in(m.group(2)),
                       "text": body, "status": status,
                       "pin": {"x": float(m.group(3)), "y": float(m.group(4))} if m.group(3) else None}
                if m.group(5):
                    cur["region"] = {k: float(m.group(g)) for k, g in zip(("x", "y", "w", "h"), (5, 6, 7, 8))}
                items.append(cur)
                continue
            if cur is None:
                continue
            if m := self.quote.match(line):
                cur["quoteFrom"], cur["quote"] = self._side_in(m.group(1) or ""), m.group(2)
                # 页面导出的快捷修改：正文是固定说法，引用是原文
                if cur["text"] == DELETE_TEXT:
                    cur["edit"] = {"op": "delete", "from": cur["quote"]}
                elif cur["text"].startswith(REPLACE_PREFIX):
                    cur["edit"] = {"op": "replace", "from": cur["quote"], "to": cur["text"][len(REPLACE_PREFIX):]}
                continue
            if m := self.resolution.match(line):
                cur["resolution"] = m.group(2)
                continue
            if m := self.style.match(line):
                if m.group(2):   # 0.3 导出的格式：框写在风格优化这一行
                    cur["region"] = {"x": float(m.group(2)), "y": float(m.group(3)),
                                     "w": float(m.group(4)), "h": float(m.group(5))}
                cur["style"] = {"scope": self._scope_style_in(m.group(1))}
                continue
            if m := self.anchor.match(line):
                cur["anchor"] = m.group(1)
                continue
            if m := self.docx.match(line):   # 读不懂的（更高版本、被改坏的）整行略过，不并进正文
                got = self._docx_in(int(m.group(1)), m.group(2))
                if got:
                    cur["docx"] = got
                continue
            if m := self.pptx.match(line):
                version = int(m.group(1))
                got = self._pptx_in(version, m.group(2))
                if got:
                    cur["pptx"] = got
                elif version > pptx_reader.ANCHOR_VERSION:
                    # 未知的新版本定位：保留版本信息，不当作无定位静默导入（与存储层高版本
                    # 保留语义一致）。md 行只带版本号，完整内容在 JSON 备份里；show 据此列出
                    # 「不可用于回写、请升级工具」的提示。读不懂的其他整行仍略过，不并进正文。
                    cur["pptx"] = {"v": version}
                continue
            if m := self.skills.match(line):
                body = re.sub(r"(——处理这条前先调用这些技能|（处理这条前先调用这些技能）)$", "", m.group(1).strip())
                own = [x for x in re.split(r"[,，、;；]+", body) if not re.search(r"（常驻·[^）]*）$", x.strip())]
                cur["skills"] = store.clean_skill_names([re.sub(r"（这条指定）$", "", x.strip()) for x in own])
                continue
            if m := self.pair.match(line):
                cur["pair"] = {"side": self._side_in(m.group(1)),
                               "match": MATCH_ZH.get(m.group(2), "similar") if self.lang == "en"
                               else ("exact" if m.group(2) == "相同" else "similar"),
                               "text": m.group(3)}
                continue
            if line.startswith(self.indent) and line.strip():
                cur["text"] += "\n" + line[len(self.indent):]
        return items

    def _docx_in(self, version: int, body: str) -> dict[str, Any] | None:
        m = self.docx_body.match(body) if version == 1 else None
        if not m:
            return None
        kinds = {v: k for k, v in DOCX_KIND_ZH.items()} if self.lang != "en" else {k: k for k in DOCX_KIND_ZH}
        vias = {v: k for k, v in (DOCX_VIA_EN if self.lang == "en" else DOCX_VIA_ZH).items()}

        def para(n: str, pid: str, dg: str) -> dict[str, Any]:
            return {"id": None if pid == "-" else pid.upper(), "n": int(n), "digest": dg}

        out: dict[str, Any] = {"v": 1, "kind": kinds[m.group(1)], "via": vias.get(m.group(11) or "", "selection")}
        if m.group(12):
            out["fp"] = m.group(12)
        out["para"] = para(m.group(2), m.group(3), m.group(4))
        if m.group(5):
            out["start"] = int(m.group(5))
        if m.group(6):
            out["end"] = int(m.group(6))
        if m.group(7):
            out["endPara"] = para(m.group(7), m.group(8), m.group(9))
        if m.group(10):
            out["run"] = int(m.group(10))
        if m.group(13):
            out["changed"] = True
        return out

    def _pptx_in(self, version: int, body: str) -> dict[str, Any] | None:
        m = self.pptx_body.match(body) if version == 1 else None
        if not m:
            return None
        zh = self.lang != "en"
        kinds = {v: k for k, v in PPTX_KIND_ZH.items()} if zh else {k: k for k in PPTX_KIND_ZH}
        vias = {v: k for k, v in (PPTX_VIA_EN if self.lang == "en" else PPTX_VIA_ZH).items()}
        out: dict[str, Any] = {"v": 1, "kind": kinds[m.group("kind")], "slide": m.group("slide")}
        raw = m.group("targets")
        if raw:
            els = []
            for em in self.pptx_el.finditer(raw):
                el: dict[str, Any] = {"key": em.group(1), "id": em.group(2), "name": "", "kind": em.group(3),
                                      "z": int(em.group(4)), "digest": em.group(5)}
                if em.group(6):
                    ro_rev = {} if self.lang == "en" else PPTX_RO_ZH_REV
                    el["ro"] = [ro_rev.get(x.strip(), x.strip())
                                for x in em.group(6).split("," if self.lang == "en" else "、") if x.strip()]
                els.append(el)
            out["elements"] = els
        if m.group("para") is not None:
            text: dict[str, Any] = {"para": int(m.group("para")), "paraDigest": m.group("paraDigest"),
                                    "quote": m.group("quote") or ""}
            if m.group("start") is not None:
                text["start"], text["end"] = int(m.group("start")), int(m.group("end"))
                runs = []
                for piece in (m.group("runs") or "").split("," if self.lang == "en" else "、"):
                    if "–" in piece:
                        s, e = piece.split("–", 1)
                        runs.append({"start": int(s), "end": int(e)})
                text["runs"] = runs
            out["text"] = text
        if m.group("px") is not None:
            out["point"] = {"x": round(float(m.group("px")) / 100, 4), "y": round(float(m.group("py")) / 100, 4)}
        if m.group("rx") is not None:
            out["region"] = {k: round(float(m.group(g)) / 100, 4)
                             for k, g in (("x", "rx"), ("y", "ry"), ("w", "rw"), ("h", "rh"))}
        if m.group("via"):
            out["via"] = vias[m.group("via")]
        if m.group("fp"):
            out["fp"] = m.group("fp")
        if m.group("changed"):
            out["changed"] = True
        return out

    def _target_in(self, label: str) -> str:
        return TARGET_ZH.get(label, label) if self.lang == "en" else label

    def _side_in(self, label: str) -> str:
        if self.lang != "en" or not label:
            return label
        return {"Report": "报告", "Script": "讲稿"}.get(label, label)

    def _scope_style_in(self, label: str) -> str:
        return STYLE_ZH.get(label, "region") if self.lang == "en" else \
            {"整页": "page", "标点处": "pin"}.get(label, "region")

    # ---------- 导出 ----------
    def marker(self) -> str:
        return f"<!-- annotation-everything export v{FORMAT_VERSION} lang={self.lang} -->"

    def page_head(self, page: int, title: str) -> str:
        return f"## Page {page} | {title}" if self.lang == "en" else f"## 第 {page} 页｜{title}"

    def tag(self, note: dict[str, Any], no: int | None) -> str:
        """【对象·标注】或 [Target · mark]：对象名与标注标签按语言，坐标数字不变。

        对象为空时写固定字面量 `未指定`（两种语言一样，它是文法的一部分，与 0.3 以来一致）。
        """
        raw = str(note.get("target") or "") or UNSET_TARGET
        target = TARGET_EN.get(raw, raw) if self.lang == "en" else raw
        out = target
        if note.get("pin"):
            p = note["pin"]
            out += (f" · Pin {no} (x {p['x']}%, y {p['y']}%)" if self.lang == "en"
                    else PIN_ZH.format(no=no, x=p["x"], y=p["y"]))
        rg = store.region_of(note)
        if rg:
            out += (f" · Region {no} (x {rg.get('x')}%, y {rg.get('y')}%, w {rg.get('w')}%, h {rg.get('h')}%)"
                    if self.lang == "en"
                    else REGION_ZH.format(no=no, **{k: rg.get(k) for k in ("x", "y", "w", "h")}))
        return f"[{out}]" if self.lang == "en" else f"【{out}】"

    def head(self, *, title: str, total: int, open_count: int, pages: int, exported_at: str) -> list[str]:
        if self.lang == "en":
            return [f"# Annotations · {title}",
                    f"Exported {exported_at}; {total} {_plural(total, 'note')} "
                    f"({open_count} open) across {pages} {_plural(pages, 'page')}",
                    HEAD_NOTE_EN]
        return [f"# 批注 · {title}",
                f"导出时间：{exported_at}；共 {total} 条（未处理 {open_count}），涉及 {pages} 页",
                HEAD_NOTE]

    def always_head(self, always: list[dict[str, str]]) -> str:
        parts = []
        for a in always:
            scope = str(a.get("scope") or "all")
            label = SCOPE_EN.get(scope, scope) if self.lang == "en" else ALWAYS_ZH_LABEL.get(scope, scope)
            parts.append(f"{a.get('name', '')} ({label})" if self.lang == "en"
                         else f"{a.get('name', '')}（{label}）")
        body = (", " if self.lang == "en" else "、").join(parts)
        lead = "Persistent skills (run before every note within scope): " if self.lang == "en" \
            else "常驻技能（每条批注按范围处理前都先调用）："
        return lead + body

    def quote_line(self, quote: str, quote_from: str) -> str:
        src = (TARGET_EN.get(quote_from, quote_from) if self.lang == "en" else quote_from)
        if self.lang == "en":
            return f"   > Quote ({src}): {_one_line(quote)}" if src else f"   > Quote: {_one_line(quote)}"
        return f"   > 引用（{src}）：{_one_line(quote)}" if src else f"   > 引用：{_one_line(quote)}"

    def resolution_line(self, resolution: str, status: str) -> str:
        if self.lang == "en":
            label = "Draft note" if status == "drafted" else "Resolution"
            return f"   {label}: {_one_line(resolution)}"
        return f"   {'初改说明' if status == 'drafted' else '处理说明'}：{_one_line(resolution)}"

    def style_line(self, style: dict[str, Any]) -> str:
        """只写范围标签；坐标已在标题行里（0.3 那种「区域（x…）」写法只读不写）。"""
        scope = str(style.get("scope") or "page")
        label = STYLE_EN.get(scope, "Whole page") if self.lang == "en" else \
            STYLE_LABEL_ZH.get(scope, "整页")
        return f"   Style: {label}" if self.lang == "en" else f"   风格优化：{label}"

    def anchor_line(self, anchor: str) -> str:
        return f"   Position hint: {_one_line(anchor)}" if self.lang == "en" else f"   位置线索：{_one_line(anchor)}"

    def docx_line(self, a: dict[str, Any]) -> str:
        """DOCX 段落定位：v1 逐项写出（可还原）；更高版本只写版本号。"""
        en = self.lang == "en"
        lead = "   DOCX location: " if en else "   DOCX 定位："
        if a.get("v") != 1 or not isinstance(a.get("para"), dict):
            return lead + (f"v{a.get('v')} · newer than this tool supports; the JSON backup keeps it in full" if en
                           else f"v{a.get('v')} · 高于本工具支持的版本，完整内容见 JSON 备份")

        def para(p: dict[str, Any]) -> str:
            pid = p.get("id") or "-"
            return (f"{p.get('n')} (paraId {pid}, digest {p.get('digest')})" if en
                    else f"{p.get('n')}（paraId {pid}，摘要 {p.get('digest')}）")

        kind = str(a.get("kind"))
        parts = [f"v1 {kind if en else DOCX_KIND_ZH.get(kind, kind)}",
                 ("paragraph " if en else "段落 ") + para(a["para"])]
        if a.get("start") is not None:
            tail = f"–{a['end']}" if a.get("end") is not None else (" onward" if en else " 起")
            parts.append(("chars " if en else "字符 ") + f"{a['start']}{tail}")
        if isinstance(a.get("endPara"), dict):
            parts.append(("ends in paragraph " if en else "止于段落 ") + para(a["endPara"]))
        if a.get("run") is not None:
            parts.append(("run " if en else "运行 ") + str(a["run"]))
        via = str(a.get("via"))
        parts.append(("source " if en else "来源 ") + (DOCX_VIA_EN if en else DOCX_VIA_ZH).get(via, via))
        if a.get("fp"):
            parts.append(("fingerprint " if en else "指纹 ") + str(a["fp"]))
        if a.get("changed"):
            parts.append("paragraph changed" if en else "段落已改动")
        return lead + " · ".join(parts)

    def pptx_line(self, a: dict[str, Any]) -> str:
        """PPT 幻灯片定位：v1 逐项写出（可还原）；更高版本只写版本号。
        元素目标只写身份不写名称（名称会变、不是身份，重定位时刷新），引用原文放行尾。"""
        en = self.lang == "en"
        lead = "   PPT location: " if en else "   PPT 定位："
        if a.get("v") != 1 or not isinstance(a.get("slide"), str) or not a.get("slide"):
            return lead + (f"v{a.get('v')} · newer than this tool supports; the JSON backup keeps it in full" if en
                           else f"v{a.get('v')} · 高于本工具支持的版本，完整内容见 JSON 备份")
        parts = [f"v1 {a['kind'] if en else PPTX_KIND_ZH.get(a['kind'], a['kind'])}",
                 ("slide " if en else "幻灯片 ") + str(a["slide"])]
        els = a.get("elements") or []
        if els:
            items = []
            for e in els:
                body = (f"{e.get('key')} (id {e.get('id')}, {e.get('kind')}, z {e.get('z', 0)},"
                        f" digest {e.get('digest')}" if en else
                        f"{e.get('key')}（标识 {e.get('id')}，{e.get('kind')}，第 {e.get('z', 0)} 层，"
                        f"摘要 {e.get('digest')}")
                if e.get("ro"):
                    ro = (", " if en else "、").join(x if en else PPTX_RO_ZH.get(x, x) for x in e["ro"])
                    body += (f", read-only {ro}" if en else f"，只读 {ro}")
                items.append(body + (")" if en else "）"))
            parts.append(("targets " if en else "目标 ") + ("; " if en else "、").join(items))
        text = a.get("text") if isinstance(a.get("text"), dict) else None
        if text is not None:
            parts.append((f"paragraph {text.get('para', 0)} (digest {text.get('paraDigest', '')})" if en
                          else f"段落 {text.get('para', 0)}（摘要 {text.get('paraDigest', '')}）"))
            if text.get("start") is not None and text.get("end") is not None:
                parts.append(("chars " if en else "字符 ") + f"{text['start']}–{text['end']}")
                runs = [r for r in (text.get("runs") or []) if isinstance(r, dict)]
                if runs:
                    joined = (", " if en else "、").join(f"{r.get('start', 0)}–{r.get('end', 0)}" for r in runs)
                    parts.append(("runs " if en else "运行 ") + joined)
        if isinstance(a.get("point"), dict):
            p = a["point"]
            px, py = round(p.get("x", 0) * 100), round(p.get("y", 0) * 100)
            parts.append(f"point x {px}%, y {py}%" if en else f"点 x {px}%，y {py}%")
        if isinstance(a.get("region"), dict):
            rg = a["region"]
            vals = {k: round(rg.get(k, 0) * 100) for k in ("x", "y", "w", "h")}
            parts.append((f"region x {vals['x']}%, y {vals['y']}%, width {vals['w']}%, height {vals['h']}%" if en
                          else f"区域 x {vals['x']}%，y {vals['y']}%，宽 {vals['w']}%，高 {vals['h']}%"))
        via = str(a.get("via"))
        parts.append(("source " if en else "来源 ") + (PPTX_VIA_EN if en else PPTX_VIA_ZH).get(via, via))
        if a.get("fp"):
            parts.append(("fingerprint " if en else "指纹 ") + str(a["fp"]))
        if a.get("changed"):
            parts.append("text changed" if en else "文字已改动")
        line = lead + " · ".join(parts)
        if text is not None and text.get("quote"):
            line += (" · quote " if en else " · 引用 ") + _one_line(text["quote"])
        return line

    def skills_line(self, skills: list[str]) -> str:
        sep = ", " if self.lang == "en" else "、"
        return f"   Skills: {sep.join(skills)}" if self.lang == "en" else f"   技能：{sep.join(skills)}"

    def pair_line(self, pair: dict[str, Any]) -> str:
        side = str(pair.get("side") or "")
        side = TARGET_EN.get(side, side) if self.lang == "en" else side
        match = MATCH_EN.get(str(pair.get("match") or "similar"), "similar") if self.lang == "en" \
            else {"exact": "相同", "similar": "相近"}.get(str(pair.get("match")), "相近")
        if self.lang == "en":
            return f"   ↔ Other side ({side} · {match}): {_one_line(pair.get('text', ''))}"
        return f"   ↔ 另一侧（{side}·{match}）：{_one_line(pair.get('text', ''))}"


_GRAMMARS = {lang: Grammar(lang) for lang in LANGS}


def _plural(count: int, word: str) -> str:
    return word if count == 1 else word + "s"


def _one_line(text: object) -> str:
    return str(text if text is not None else "").replace("\n", " ")


def _body(text: object) -> str:
    return str(text if text is not None else "").replace("\n", "\n   ")


def mark_numbers(notes: list[dict[str, Any]]) -> dict[str, int]:
    """与页面一致：同一页带标注（点或框）的批注按出现顺序编号 1、2、3…"""
    out: dict[str, int] = {}
    count: dict[int, int] = {}
    for n in notes:
        if n.get("pin") or store.region_of(n):
            count[n["page"]] = count.get(n["page"], 0) + 1
            out[n["id"]] = count[n["page"]]
    return out


# ---------- 标记 ----------
def read_marker(text: str) -> tuple[int, str]:
    """读第一行（忽略空行与 BOM）的格式标记。没有标记时返回 (0, "")，表示旧中文格式。"""
    for line in str(text or "").lstrip("\ufeff").splitlines():
        if not line.strip():
            continue
        m = MARKER_RE.match(line.strip())
        if not m:
            return 0, ""
        return int(m.group(1)), m.group(2)
    return 0, ""


def parse_markdown(text: str) -> tuple[list[dict[str, Any]], list[dict[str, str]] | None]:
    """按格式标记解析 Markdown 导出，返回 (批注, 常驻技能)。

    常驻技能只有导出里带了那一行才不是 None。没有标记时按旧中文格式读（0.3 以来的导出没有标记）；
    没有标记又读不出任何批注时算「无法识别」，交给调用方按自己的方式报错。
    """
    version, lang = read_marker(text)
    if version == 0:
        items = parse_zh(text)
        if not items:
            raise FormatError(UNKNOWN_FORMAT)
        return items, parse_always(text, "zh-CN")
    if version != FORMAT_VERSION:
        raise FormatError(f"这个导出用的是格式版本 v{version}，当前只支持 v{FORMAT_VERSION}："
                          "请把 notes.py 和对照页都升级到同一版本再导入", code="version", version=version,
                          supported=FORMAT_VERSION)
    g = _GRAMMARS.get(lang)
    if g is None:
        raise FormatError(f"这个导出的内容语言是「{lang}」，当前只支持 {'、'.join(LANGS)}", code="language", language=lang)
    return g.parse(text), parse_always(text, lang)


def parse_zh(text: str) -> list[dict[str, Any]]:
    """旧中文格式（也等于新的中文文法）：没有标记时按它读。"""
    return _GRAMMARS["zh-CN"].parse(text)


def parse_always(text: str, lang: str = "zh-CN") -> list[dict[str, str]] | None:
    """导出开头「常驻技能（…）：a（讲稿）、b（全部批注）」那一行；没有这行返回 None。"""
    g = _GRAMMARS.get(lang)
    if g is None:
        raise FormatError(f"不支持的内容语言「{lang}」，可选：{'、'.join(LANGS)}", code="language", language=lang)
    m = g.always.search(text)
    if not m:
        return None
    out: list[dict[str, str]] = []
    for part in re.split(re.escape(g.always_sep), m.group(1)):
        mm = g.always_item.match(part.strip())
        if mm:
            scope = SCOPE_ZH.get(mm.group(2), mm.group(2)) if lang == "en" else ALWAYS_ZH_REV.get(mm.group(2), "all")
            out.append({"name": mm.group(1), "scope": scope})
    return out


# ---------- 导出 ----------
def render(notes: list[dict[str, Any]], *, title: str, page_titles: dict[int, str] | None = None,
           always: list[dict[str, str]] | None = None, lang: str = "zh-CN",
           exported_at: str | None = None, include_resolved: bool = True,
           page: int | None = None) -> str:
    """渲染成交换用的 Markdown。notes 先按（页码, 时间）排序，与页面导出一致。"""
    g = _GRAMMARS[lang]
    titles = page_titles or {}
    picked = [n for n in notes if (include_resolved or n.get("status") != "resolved")
              and (page is None or n.get("page") == page)]
    picked.sort(key=lambda n: (int(n.get("page", 0) or 0), str(n.get("at", ""))))
    marks = mark_numbers(notes)
    by: dict[int, list[dict[str, Any]]] = {}
    for n in picked:
        by.setdefault(int(n.get("page", 0) or 0), []).append(n)
    # 「未处理」与页面一致：已初改·待复核也还没定稿，算在里面（这行只给人看，不参与解析）
    open_count = sum(1 for n in notes if n.get("status", "open") != "resolved")
    lines = [g.marker()]
    lines += g.head(title=title, total=len(notes), open_count=open_count, pages=len(by),
                    exported_at=exported_at or exported_stamp())
    if always:
        lines.append(g.always_head(always))
    for pg in sorted(by):
        group = by[pg]
        lines.append("")
        lines.append(g.page_head(pg, str(titles.get(pg) or group[0].get("title") or "")))
        for k, n in enumerate(group):
            body = str(n.get("text", ""))
            gap = " " if lang == "en" else ""   # 英文的方括号后面要空格才读得顺
            lines.append(f"{k + 1}. {g.tag(n, marks.get(n['id']))}{gap}{g.state_prefix.get(n.get('status'), '')}"
                         + _body(body))
            if n.get("quote"):
                lines.append(g.quote_line(str(n["quote"]), str(n.get("quoteFrom") or "")))
            if n.get("status") in ("resolved", "drafted") and n.get("resolution"):
                lines.append(g.resolution_line(str(n["resolution"]), str(n["status"])))
            st = n.get("style") if isinstance(n.get("style"), dict) else None
            if st and st.get("scope"):
                lines.append(g.style_line(st))
            if n.get("anchor"):
                lines.append(g.anchor_line(str(n["anchor"])))
            if isinstance(n.get("docx"), dict):
                lines.append(g.docx_line(n["docx"]))
            if isinstance(n.get("pptx"), dict):
                lines.append(g.pptx_line(n["pptx"]))
            if n.get("skills"):
                lines.append(g.skills_line(list(n["skills"])))
            pr = n.get("pair") if isinstance(n.get("pair"), dict) else None
            if pr and pr.get("text"):
                lines.append(g.pair_line(pr))
    return "\n".join(lines) + "\n"


def json_export(*, review_id: str, name: str, namespace: str, notes: list[dict[str, Any]],
                always: list[dict[str, str]] | None, lang: str) -> dict[str, Any]:
    """结构化备份：整份 notes 原样带上，协议值不改写；lang 是新增的元信息，旧消费者按 key 读取即可忽略。"""
    return {"version": 1, "reviewId": review_id, "name": name, "namespace": namespace,
            "lang": lang, "notes": notes, "alwaysSkills": always or []}

def file_prefix(lang: str) -> str:
    return "annotations" if lang == "en" else "批注"


def default_filename(namespace: str, lang: str, suffix: str = "md", stamp: str | None = None) -> str:
    """文件名保留审阅识别（namespace）与机器可读的 UTC 时间；只有按语言换的前缀会变。"""
    return f"{file_prefix(lang)}_{namespace}_{stamp or file_stamp()}.{suffix}"


def exported_stamp(when: datetime | None = None) -> str:
    """导出时间：本地时间的 YYYY-MM-DD HH:MM:SS，两种语言同一格式（不带地区格式）。"""
    return (when or datetime.now()).strftime("%Y-%m-%d %H:%M:%S")


def file_stamp(when: datetime | None = None) -> str:
    """文件名里的时间：紧凑 UTC（YYYYMMDDHHMM），与页面 stamp12() 一致。"""
    return (when or datetime.now(timezone.utc)).strftime("%Y%m%d%H%M")
