"""PPTX 渲染辅助契约测试（issue #180）：可选工具探测、逐页图片与渲染清单。

接缝：pptx_render.render_deck（.pptx → 逐页 PNG + ae-pptx-render/1 清单）与
它的 CLI。工具缺失与失败样例用假可执行文件合成，不依赖本机真实 LibreOffice；
只用 Python 标准库。
"""
from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "skill" / "annotation-everything" / "scripts"
sys.path.insert(0, str(SCRIPTS))
os.environ.setdefault("AE_HOME", tempfile.mkdtemp(prefix="ae-test-home-"))

import pptx_render as pr_render  # noqa: E402
import pptx_build as pb  # noqa: E402


def deck_spec() -> dict:
    """最小成功规格（内联合成数据，避免跨测试文件导入）。"""
    return {
        "schema": "ae-pptx-deck/1",
        "title": "合成演示文稿",
        "theme": {
            "colors": {"dk1": "1F2933", "lt1": "FFFFFF", "dk2": "3E4C59", "lt2": "F5F7FA",
                       "accent1": "2B6CB0", "accent2": "2C7A7B", "accent3": "B7791F",
                       "accent4": "6B46C1", "accent5": "C05621", "accent6": "975FA8",
                       "hlink": "2B6CB0", "folHlink": "805AD5"},
            "fonts": {"major": {"latin": "Synthetic Display", "ea": "合成标题字体"},
                      "minor": {"latin": "Synthetic Text", "ea": "合成正文字体"}},
        },
        "slides": [
            {"key": "cover", "layout": "title", "title": "合成封面", "subtitle": "副标题"},
            {"key": "sum", "layout": "titleAndContent", "title": "要点页",
             "body": ["第一条要点", {"text": "二级要点", "level": 1}, "第二条要点"]},
        ],
    }

FAKE_PNG = (b"\x89PNG\r\n\x1a\n" + b"\x00\x00\x00\x0dIHDR"
            + (4).to_bytes(4, "big") + (4).to_bytes(4, "big") + b"rest")
FAKE_PDF = b"%PDF-1.4 synthetic"


def fake_soffice(source: Path) -> Path:
    """假 soffice：--version 报版本；--convert-to pdf 在 --outdir 落一个假 PDF。"""
    script = source / "soffice"
    script.write_text(
        "#!/bin/sh\n"
        'if [ "$1" = "--version" ]; then echo "LibreOffice 24.8.4.2"; exit 0; fi\n'
        'outdir=""\nsrc=""\n'
        'for arg in "$@"; do\n'
        '  case "$arg" in\n'
        '    --*) : ;;\n'
        '    *) if [ -z "$src" ] && [ -f "$arg" ]; then src="$arg";\n'
        '       elif [ -d "$arg" ]; then outdir="$arg"; fi ;;\n'
        '  esac\n'
        "done\n"
        'base=$(basename "${src%.*}")\n'
        'python3 -c "import sys; open(sys.argv[1], \'wb\').write(\'%PDF-1.4 synthetic\')" '
        '"$outdir/$base.pdf"\n'
        "exit 0\n")
    script.chmod(script.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return script


def fake_pdftoppm(source: Path, pages: int = 2) -> Path:
    """假 pdftoppm：-v 报版本；转换时按 -r 之外的参数产出 N 页假 PNG。"""
    script = source / "pdftoppm"
    lines = [
        "#!/bin/sh",
        'if [ "$1" = "-v" ] || [ "$1" = "--version" ]; then echo "pdftoppm version 24.08.0"; exit 0; fi',
        'prefix=""',
        'prev=""',
        'for arg in "$@"; do',
        '  case "$prev" in',
        '    -r|-png|-gray) : ;;',
        '  esac',
        '  case "$arg" in',
        '    -*) : ;;',
        '    *) prefix="$arg" ;;',
        '  esac',
        '  prev="$arg"',
        "done",
        'python3 - "$prefix" <<\'PYEOF\'',
        "import sys, pathlib",
        f"blob = {FAKE_PNG!r}",
        "prefix = sys.argv[1]",
        f"for page in range(1, {pages} + 1):",
        '    pathlib.Path(f"{prefix}-{page:02d}.png").write_bytes(blob)',
        "PYEOF",
        "exit 0",
    ]
    script.write_text("\n".join(lines) + "\n")
    script.chmod(script.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return script


class PptxRenderTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="ae-pptx-render-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.bin_dir = self.root / "bin"
        self.bin_dir.mkdir()
        self._old_env = {k: os.environ.get(k) for k in ("AE_SOFFICE", "AE_PDFTOPPM")}

    def tearDown(self):
        for key, value in self._old_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    def _deck(self, name="deck.pptx") -> Path:
        out = self.root / name
        pb.build_deck(deck_spec(), out)
        return out

    def _set_env(self, soffice=None, pdftoppm=None):
        if soffice is None:
            os.environ.pop("AE_SOFFICE", None)
        else:
            os.environ["AE_SOFFICE"] = str(soffice)
        if pdftoppm is None:
            os.environ.pop("AE_PDFTOPPM", None)
        else:
            os.environ["AE_PDFTOPPM"] = str(pdftoppm)

    def test_render_produces_pages_and_manifest(self):
        """两个假工具齐全：产出逐页 PNG 与绑定文件哈希的渲染清单。"""
        fake_soffice(self.bin_dir)
        fake_pdftoppm(self.bin_dir)
        self._set_env(soffice=self.bin_dir / "soffice",
                      pdftoppm=self.bin_dir / "pdftoppm")
        deck = self._deck()
        out_dir = self.root / "pages"
        manifest = pr_render.render_deck(deck, out_dir)

        self.assertEqual(manifest["schema"], "ae-pptx-render/1")
        self.assertEqual(manifest["status"], "ok")
        self.assertEqual(manifest["renderer"]["soffice"]["version"], "24.8.4.2")
        self.assertEqual(manifest["renderer"]["pdftoppm"]["version"], "24.08.0")
        self.assertEqual([p["page"] for p in manifest["pages"]], [1, 2])
        self.assertEqual(manifest["pptxSha256"],
                         __import__("hashlib").sha256(deck.read_bytes()).hexdigest())
        for entry in manifest["pages"]:
            image = out_dir / entry["image"]
            self.assertTrue(image.is_file())
            import hashlib
            self.assertEqual(entry["sha256"],
                             hashlib.sha256(image.read_bytes()).hexdigest())

    def test_missing_soffice_reports_unavailable(self):
        """缺 soffice：状态 unavailable，诊断写明缺什么、怎么补。"""
        self._set_env(soffice=self.bin_dir / "不存在的soffice")
        deck = self._deck()
        manifest = pr_render.render_deck(deck, self.root / "pages2")

        self.assertEqual(manifest["status"], "unavailable")
        self.assertEqual(manifest["pages"], [])
        self.assertTrue(any("soffice" in line for line in manifest["diagnosis"]))

    def test_reused_output_directory_contains_only_current_pages(self):
        fake_soffice(self.bin_dir)
        fake_pdftoppm(self.bin_dir, pages=12)
        self._set_env(soffice=self.bin_dir / "soffice", pdftoppm=self.bin_dir / "pdftoppm")
        deck = self._deck()
        out_dir = self.root / "pages"
        first = pr_render.render_deck(deck, out_dir)
        self.assertEqual(len(first["pages"]), 12)
        (out_dir / "notes.txt").write_text("保留", encoding="utf-8")
        fake_pdftoppm(self.bin_dir, pages=2)
        second = pr_render.render_deck(deck, out_dir)
        self.assertEqual(second["status"], "ok")
        self.assertEqual([p["page"] for p in second["pages"]], [1, 2])
        self.assertEqual(sorted(p.name for p in out_dir.glob("page-*.png")),
                         [p["image"] for p in second["pages"]])
        self.assertEqual((out_dir / "notes.txt").read_text(encoding="utf-8"), "保留")

    def test_successful_tool_without_new_pages_cannot_reuse_old_output(self):
        fake_soffice(self.bin_dir)
        fake_pdftoppm(self.bin_dir)
        self._set_env(soffice=self.bin_dir / "soffice", pdftoppm=self.bin_dir / "pdftoppm")
        deck = self._deck()
        out_dir = self.root / "pages"
        self.assertEqual(pr_render.render_deck(deck, out_dir)["status"], "ok")
        fake_pdftoppm(self.bin_dir, pages=0)
        result = pr_render.render_deck(deck, out_dir)
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["pages"], [])

    def test_missing_pdftoppm_reports_unavailable(self):
        """缺 pdftoppm：同样 unavailable 并指名工具。"""
        fake_soffice(self.bin_dir)
        self._set_env(soffice=self.bin_dir / "soffice",
                      pdftoppm=self.bin_dir / "不存在的pdftoppm")
        deck = self._deck()
        manifest = pr_render.render_deck(deck, self.root / "pages3")

        self.assertEqual(manifest["status"], "unavailable")
        self.assertTrue(any("pdftoppm" in line for line in manifest["diagnosis"]))

    def test_conversion_without_pdf_is_failed(self):
        """soffice 转换没产出 PDF：状态 failed，不再进入逐页环节。"""
        script = self.bin_dir / "soffice-empty"
        script.write_text("#!/bin/sh\nexit 0\n")
        script.chmod(script.stat().st_mode | stat.S_IXUSR)
        fake_pdftoppm(self.bin_dir)
        self._set_env(soffice=script, pdftoppm=self.bin_dir / "pdftoppm")
        deck = self._deck()
        manifest = pr_render.render_deck(deck, self.root / "pages4")

        self.assertEqual(manifest["status"], "failed")
        self.assertEqual(manifest["pages"], [])

    def test_cli_contract(self):
        """CLI：--json 输出清单；工具缺失退出 1。"""
        fake_soffice(self.bin_dir)
        fake_pdftoppm(self.bin_dir)
        deck = self._deck("cli.pptx")
        out_dir = self.root / "cli-pages"
        env = {**os.environ, "AE_SOFFICE": str(self.bin_dir / "soffice"),
               "AE_PDFTOPPM": str(self.bin_dir / "pdftoppm")}
        done = subprocess.run(
            [sys.executable, str(SCRIPTS / "pptx_render.py"), str(deck),
             "--out-dir", str(out_dir), "--json"],
            capture_output=True, text=True, timeout=60, env=env)
        self.assertEqual(done.returncode, 0, done.stderr)
        payload = json.loads(done.stdout)
        self.assertEqual(payload["schema"], "ae-pptx-render/1")
        self.assertEqual(payload["status"], "ok")

        env_missing = {**env, "AE_SOFFICE": str(self.bin_dir / "不存在的soffice"),
                       "AE_PDFTOPPM": str(self.bin_dir / "不存在的pdftoppm")}
        done2 = subprocess.run(
            [sys.executable, str(SCRIPTS / "pptx_render.py"), str(deck),
             "--out-dir", str(self.root / "cli-pages2"), "--json"],
            capture_output=True, text=True, timeout=60, env=env_missing)
        self.assertEqual(done2.returncode, 1)
        payload2 = json.loads(done2.stdout)
        self.assertEqual(payload2["status"], "unavailable")


if __name__ == "__main__":
    unittest.main()
