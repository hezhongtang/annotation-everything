#!/usr/bin/env python3
"""PPTX 渲染辅助（issue #180）：调用可选本机工具把演示文稿渲染成逐页图片。

这是逐页视觉检查的前半段：探测本机的 LibreOffice（soffice）与 poppler
（pdftoppm），把 .pptx 转成 PDF 再转成逐页 PNG，并写出 `ae-pptx-render/1`
渲染清单（工具与版本、逐页图片哈希、被渲染文件的 sha256）。看图由宿主
Agent 执行——生成图片或跑完检测不等于已经逐页看图。

工具是可选依赖：缺失或失败时如实给状态与诊断，不动 HTML 审阅；探测顺序为
AE_SOFFICE / AE_PDFTOPPM 环境变量，再 PATH，再 macOS 的 LibreOffice 安装
位置。只用 Python 标准库。

用法：
    python3 pptx_render.py deck.pptx --out-dir pages/ [--locale zh-CN] [--json]

退出码：0 渲染成功；1 工具缺失或转换失败；2 输入或用法错误。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import fsutil
import i18n

RENDER_SCHEMA = "ae-pptx-render/1"

# 渲染 DPI：150 在 1280×720 画布上得到清晰可读的逐页图。
RENDER_DPI = 150
_SOFFICE_MAC_CANDIDATES = (
    "/Applications/LibreOffice.app/Contents/MacOS/soffice",
)
_IMAGE_RE = re.compile(r"page-(\d+)\.png$")


def _t(loc: str, key: str, **params: object) -> str:
    return i18n.translate(i18n.catalog(loc), key, **params)


def _version_of(binary: Path) -> str:
    """从 --version 首行解析出数字版本（如 24.8.4.2）；解析不出返回整行。"""
    try:
        done = subprocess.run([str(binary), "--version"], capture_output=True,
                              text=True, timeout=60)
    except (OSError, subprocess.TimeoutExpired):
        return ""
    first = (done.stdout or done.stderr).strip().splitlines()
    if not first:
        return ""
    matched = re.search(r"\d+(\.\d+)+", first[0])
    return matched.group(0) if matched else first[0].strip()


def _probe(env_var: str, name: str, mac_candidates: tuple[str, ...] = ()) -> Path | None:
    override = os.environ.get(env_var, "").strip()
    if override:
        candidate = Path(override)
        return candidate if candidate.is_file() else None
    found = shutil.which(name)
    if found:
        return Path(found)
    for path in mac_candidates:
        candidate = Path(path)
        if candidate.is_file():
            return candidate
    return None


def _diagnose_missing(loc: str, which: str, env_var: str) -> str:
    return _t(loc, "pptxrender.reason.missingTool", tool=which, env=env_var)


def render_deck(path: Path, out_dir: Path, locale: str | None = None) -> dict:
    """渲染演示文稿为逐页 PNG；返回 ae-pptx-render/1 清单（含不可用状态）。"""
    path = Path(path)
    loc = i18n.resolve_locale(locale, system_languages=i18n.system_languages())
    pptx_sha = hashlib.sha256(path.read_bytes()).hexdigest()
    manifest: dict = {"schema": RENDER_SCHEMA, "file": path.name,
                      "pptxSha256": pptx_sha, "status": "unavailable",
                      "renderer": {}, "pages": [], "diagnosis": [], "outDir": out_dir.name}
    soffice = _probe("AE_SOFFICE", "soffice", _SOFFICE_MAC_CANDIDATES)
    pdftoppm = _probe("AE_PDFTOPPM", "pdftoppm")
    if soffice is None:
        manifest["diagnosis"].append(_diagnose_missing(loc, "soffice", "AE_SOFFICE"))
    else:
        version = _version_of(soffice)
        manifest["renderer"]["soffice"] = {"name": "LibreOffice",
                                           "version": version, "path": soffice.name}
    if pdftoppm is None:
        manifest["diagnosis"].append(_diagnose_missing(loc, "pdftoppm", "AE_PDFTOPPM"))
    else:
        version = _version_of(pdftoppm)
        manifest["renderer"]["pdftoppm"] = {"name": "poppler pdftoppm",
                                            "version": version, "path": pdftoppm.name}
    if soffice is None or pdftoppm is None:
        return manifest

    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="ae-pptx-render-") as workdir:
        work = Path(workdir)
        profile = (work / "lo-profile").as_uri()
        done = subprocess.run(
            [str(soffice), "--headless", f"-env:UserInstallation={profile}",
             "--convert-to", "pdf", "--outdir", str(work), str(path)],
            capture_output=True, text=True, timeout=300)
        pdf = work / (path.stem + ".pdf")
        if not pdf.is_file():
            manifest["status"] = "failed"
            detail = (done.stderr or done.stdout or "").strip().splitlines()
            manifest["diagnosis"].append(_t(
                loc, "pptxrender.reason.conversionFailed",
                detail=detail[0][:200] if detail else ""))
            return manifest
        render = subprocess.run(
            [str(pdftoppm), "-png", "-r", str(RENDER_DPI), str(pdf),
             str(out_dir / "page")],
            capture_output=True, text=True, timeout=300)
    if render.returncode != 0:
        manifest["status"] = "failed"
        manifest["diagnosis"].append(_t(loc, "pptxrender.reason.pageRenderFailed",
                                        code=render.returncode))
        return manifest
    pages = []
    for image in sorted(out_dir.glob("page-*.png")):
        matched = _IMAGE_RE.search(image.name)
        if not matched:
            continue
        pages.append({"page": int(matched.group(1)),
                      "image": image.name,
                      "sha256": hashlib.sha256(image.read_bytes()).hexdigest()})
    pages.sort(key=lambda entry: entry["page"])
    if not pages:
        manifest["status"] = "failed"
        manifest["diagnosis"].append(_t(loc, "pptxrender.reason.noPages"))
        return manifest
    manifest["status"] = "ok"
    manifest["pages"] = pages
    return manifest


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="把 PPTX 渲染成逐页 PNG 并写渲染清单（可选工具：soffice + pdftoppm）")
    parser.add_argument("pptx", help="要渲染的 .pptx 文件")
    parser.add_argument("--out-dir", required=True, help="逐页图片输出目录")
    parser.add_argument("--locale")
    parser.add_argument("--json", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    fsutil.configure_stdio()
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        loc = i18n.resolve_locale(args.locale, system_languages=i18n.system_languages())
    except i18n.LocaleError as exc:
        print(_t("zh-CN", "pptxrender.badLocale", locale=exc.params.get("locale", "")),
              file=sys.stderr)
        print(_t("en", "pptxrender.badLocale", locale=exc.params.get("locale", "")),
              file=sys.stderr)
        return 2
    path = Path(args.pptx)
    if not path.is_file():
        print(_t(loc, "pptxrender.fileMissing", path=str(path)), file=sys.stderr)
        return 2
    manifest = render_deck(path, Path(args.out_dir), locale=loc)
    if args.json:
        print(json.dumps(manifest, ensure_ascii=False))
    else:
        print(_t(loc, "pptxrender.summary", file=manifest["file"],
                 status=manifest["status"], pages=len(manifest["pages"])))
        for line in manifest["diagnosis"]:
            print(f"  - {line}")
    return 0 if manifest["status"] == "ok" else 1


if __name__ == "__main__":
    sys.exit(main())
