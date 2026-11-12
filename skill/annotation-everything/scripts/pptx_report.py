"""PPTX 直接构建：安全读取层（pptx_reader）+ 本机渲染 + 字体证据 → 对照页 parts。

源 PPTX 始终是事实源：结构、定位与回写能力只来自 OOXML 元素模型；渲染图是
显示产物，不进入任何回写依据。渲染走本机 LibreOffice → Poppler 管线，工具
缺失、执行失败、超时、页数不符或输出为空时明确失败，不静默退回纯文本页面。
"""
from __future__ import annotations

import base64
import html
import re
import subprocess
import tempfile
from pathlib import Path

import pptx_reader as pr

# review.json 的 format.version：PPT 审阅元数据的版本
FORMAT_VERSION = 1
# 一页幻灯片的 EMU 转 96dpi 舞台像素：1 英寸 = 914400 EMU = 96 px
EMU_PER_PX = 9525

SOFFICE_TIMEOUT = 180   # 大 deck 的 PDF 转换较慢，超时按分钟计
RASTER_TIMEOUT = 60
SOFFICE_CANDIDATES = ("/Applications/LibreOffice.app/Contents/MacOS/soffice",)
RENDER_TOOL = "libreoffice+poppler"


class PptxReportError(ValueError):
    """构建不了的 PPTX 报告。code 给本地化诊断用：noRenderer / noRasterizer / convertFailed /
    rasterFailed / emptyOutput / pageCount / timeout，或 pptx_reader 的错误码原样透传。"""

    def __init__(self, code: str, detail: str = ""):
        super().__init__(f"{code}: {detail}" if detail else code)
        self.code, self.detail = code, detail


def is_pptx(path: Path) -> bool:
    """大小写不敏感的 .pptx：生成器据此走 PPT 分支，不按 UTF-8 文本读。"""
    return Path(path).suffix.lower() == ".pptx"


def is_ppt(path: Path) -> bool:
    """旧版二进制 .ppt：不进 PPT 分支，提示先转换为 .pptx。"""
    return Path(path).suffix.lower() == ".ppt"


def find_soffice() -> str | None:
    """本机 LibreOffice 可执行文件：先看 PATH，再试常见安装位置；没有返回 None。"""
    from shutil import which
    found = which("soffice")
    if found:
        return found
    for candidate in SOFFICE_CANDIDATES:
        if Path(candidate).is_file():
            return candidate
    return None


def find_rasterizer() -> str | None:
    from shutil import which
    return which("pdftoppm")


def render_slides(pptx: Path, workdir: Path) -> list[bytes]:
    """本机渲染每张幻灯片：soffice 转 PDF，pdftoppm 出每页 PNG（字节按页序返回）。

    真实工具链；确定性测试替换本函数注入受控图像。调用方负责 workdir 的创建与清理。
    """
    pptx, workdir = Path(pptx), Path(workdir)
    soffice = find_soffice()
    if not soffice:
        raise PptxReportError("noRenderer")
    try:
        done = subprocess.run(
            [soffice, "--headless", "--norestore", "--convert-to", "pdf", "--outdir", str(workdir), str(pptx)],
            capture_output=True, text=True, errors="replace", timeout=SOFFICE_TIMEOUT, check=False)
    except subprocess.TimeoutExpired:
        raise PptxReportError("timeout", "soffice") from None
    except OSError as exc:
        raise PptxReportError("convertFailed", type(exc).__name__) from None
    pdf = workdir / (Path(pptx).stem + ".pdf")
    if done.returncode != 0 or not pdf.is_file():
        raise PptxReportError("convertFailed", (done.stderr or done.stdout or "").strip()[:200])

    rasterizer = find_rasterizer()
    if not rasterizer:
        raise PptxReportError("noRasterizer")
    prefix = workdir / "slide"
    try:
        done = subprocess.run([rasterizer, "-png", "-r", "96", str(pdf), str(prefix)],
                              capture_output=True, text=True, errors="replace",
                              timeout=RASTER_TIMEOUT, check=False)
    except subprocess.TimeoutExpired:
        raise PptxReportError("timeout", "pdftoppm") from None
    except OSError as exc:
        raise PptxReportError("rasterFailed", type(exc).__name__) from None
    if done.returncode != 0:
        raise PptxReportError("rasterFailed", (done.stderr or "").strip()[:200])
    pages = sorted(workdir.glob("slide-*.png"), key=lambda p: _page_no(p.name))
    if not pages:
        raise PptxReportError("emptyOutput")
    return [page.read_bytes() for page in pages]


def _page_no(name: str) -> int:
    numbers = re.findall(r"\d+", name)
    return int(numbers[-1]) if numbers else 0


def font_evidence(pptx: Path, pdf: Path | None) -> dict:
    """转换前声明字体检查 + 转换后 PDF 字体证据（数据来自 check_pptx_fonts 的受控解析）。

    名称一致只说明清单对得上：review 保持 True 与否由证据决定，视觉一致永远不在这里自动证明。
    """
    import check_pptx_fonts as fonts

    evidence: dict = {"declared": [], "missing": [], "unresolved": [], "fallbacks": [],
                      "installed": "ok", "pdf": None, "review": False, "warning": False}
    declared: list[str] = []
    fallbacks: list[str] = []
    try:
        declared, evidence["unresolved"], fallbacks = fonts.declared_fonts(Path(pptx))
    except fonts.CheckError as exc:
        evidence["installed"] = "failed"
        evidence["warning"] = True
        evidence["error"] = str(exc)
    installed = None
    try:
        installed, warning = fonts.installed_fonts()
    except fonts.CheckError as exc:
        evidence["installed"] = "failed"
        evidence["warning"] = True
        evidence["error"] = str(exc)
    else:
        if warning:
            evidence["warning"] = True
    evidence["fallbacks"] = fallbacks
    evidence["declared"] = [{"font": name, "found": installed is not None and fonts.normal(name) in installed}
                            for name in declared]
    if installed is not None:
        evidence["missing"] = [name for name in declared if fonts.normal(name) not in installed]
    if evidence["missing"] or evidence["unresolved"] or not declared or evidence["installed"] != "ok":
        evidence["review"] = True
    if pdf is not None:
        try:
            rows, warning = fonts.pdf_fonts(Path(pdf))
        except fonts.CheckError as exc:
            evidence["pdf"] = {"error": str(exc)}
            evidence["review"] = True
            evidence["warning"] = True
        else:
            evidence["pdf"] = {"rows": rows, "warning": warning}
            if warning or _pdf_fonts_need_review(rows, declared, fallbacks):
                evidence["review"] = True
    return evidence


def _pdf_fonts_need_review(rows: list[dict], declared: list[str], fallbacks: list[str]) -> bool:
    import check_pptx_fonts as fonts

    if not rows:
        return True
    known = {fonts.normal(name) for name in [*declared, *fallbacks]}
    matched = set()
    for row in rows:
        if "raw" in row:
            return True
        base = re.sub(r"^[A-Z]{6}\+", "", row["name"]) if row["sub"] == "yes" else row["name"]
        key = fonts.normal(base)
        if key in known:
            matched.add(key)
        if key not in known or row["emb"] != "yes":
            return True
    return any(fonts.normal(name) not in matched for name in declared)


def _px(emu: int) -> int | float:
    value = emu / EMU_PER_PX
    return int(value) if value.is_integer() else round(value, 2)


def _slide_title(slide: dict) -> str:
    """页面标题：第一个有文本的元素名，回退到文本首行；都没有就按页码。"""
    for element in slide["elements"]:
        if element.get("name"):
            return str(element["name"])[:60]
        text = (element.get("text") or "").strip()
        if text:
            return text.splitlines()[0][:60]
    return ""


def _section_html(slide: dict, image: bytes, width, height, title: str) -> str:
    uri = "data:image/png;base64," + base64.b64encode(image).decode("ascii")
    return (f'<section class="page pptx-page" data-slide="{html.escape(slide["key"])}" '
            f'style="width:{width}px;height:{height}px">'
            f'<img class="pptx-render" src="{uri}" alt="{html.escape(title)}">'
            f"</section>")


def parts(report: Path) -> dict:
    """PPTX → {"title", "css", "pages", "meta"}；渲染与字体证据在内部分别经过
    render_slides / font_evidence（可受控替换）。输入错误抛 PptxReportError。"""
    report = Path(report)
    try:
        model = pr.read(report)
    except pr.PptxError as exc:
        raise PptxReportError(exc.code, exc.detail or exc.part) from None
    slides = model["slides"]
    with tempfile.TemporaryDirectory(prefix="ae-pptx-render-") as raw:
        workdir = Path(raw)
        images = render_slides(report, workdir)
        if len(images) != len(slides):
            raise PptxReportError("pageCount", f"rendered={len(images)} slides={len(slides)}")
        # 渲染 PDF 留在受控临时目录里供字体证据读取；目录随即清理，产物不落盘到审阅目录
        fonts = font_evidence(report, _rendered_pdf(workdir, report.stem))
    canvas = model["canvas"]
    width, height = _px(canvas["width"]), _px(canvas["height"])
    pages = []
    for slide, image in zip(slides, images):
        title = _slide_title(slide)
        pages.append({"n": slide["n"], "key": slide["key"], "stable": True,
                      "html": _section_html(slide, image, width, height, title),
                      "title": title, "w": width, "h": height, "orient": canvas["orientation"]})
    meta = {
        "formatVersion": model["formatVersion"], "capabilityVersion": model["capabilityVersion"],
        "fingerprint": model["fingerprint"], "canvas": canvas,
        "renderer": {"tool": RENDER_TOOL, "pages": len(images), "status": "ok"},
        "fonts": fonts,
        "diagnostics": model["diagnostics"],
        "slides": [{"n": s["n"], "key": s["key"], "part": s["part"],
                    "notes": bool(s["notes"] and s["notes"].get("status") == "available"),
                    "elements": s["elements"]} for s in slides],
        "unverified": ["visual"] + (["fonts"] if fonts.get("warning") or fonts.get("installed") != "ok" else []),
    }
    title = pages[0]["title"] if pages else ""
    return {"title": title, "css": "", "pages": pages, "meta": meta}


def _rendered_pdf(workdir: Path, stem: str) -> Path | None:
    pdf = workdir / (stem + ".pdf")
    return pdf if pdf.is_file() else None


def slide_texts(path: Path) -> dict[int, str]:
    """逐页纯文本（元素名与文字，供对应句与 diff 用）：结构模型取文，不经渲染图。"""
    model = pr.read(Path(path))
    out: dict[int, str] = {}
    for slide in model["slides"]:
        lines = []
        for element in slide["elements"]:
            for value in (element.get("name"), element.get("text")):
                if value and str(value).strip():
                    lines.append(str(value))
        out[slide["n"]] = "\n".join(lines)
    return out


def slide_parts(path: Path) -> list[tuple[str, int, str]]:
    """按幻灯片稳定键切页：[(k:页面键, 页码, 文本)]（notes 的 diff / 快照切分口径）。"""
    model = pr.read(Path(path))
    texts = slide_texts(Path(path))
    return [(f"k:{s['key']}", s["n"], texts[s["n"]]) for s in model["slides"]]


def notes_by_slide(path: Path) -> dict[int, str]:
    """逐页演讲者备注正文（未提供外部讲稿时进讲稿侧）；没有备注的页不在结果里。"""
    model = pr.read(Path(path))
    return {s["n"]: s["notes"]["text"] for s in model["slides"]
            if isinstance(s.get("notes"), dict) and s["notes"].get("status") == "available"
            and s["notes"].get("text")}
