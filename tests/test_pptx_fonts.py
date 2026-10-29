"""PPTX 字体检查的命令行测试；文档和工具输出均为合成数据。"""
from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
import zipfile


ROOT = Path(__file__).resolve().parents[1]
CHECKER = ROOT / "skill" / "annotation-everything" / "scripts" / "check_pptx_fonts.py"


class FontCheckTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="ae-font-test-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.bin = self.root / "bin"
        self.bin.mkdir()
        self.pptx = self.root / "synthetic.pptx"
        self.env = {**os.environ, "HOME": str(self.root / "home"), "PATH": str(self.bin) + os.pathsep + os.defpath,
                    "PYTHONDONTWRITEBYTECODE": "1"}

    def tool(self, name: str, script: str):
        path = self.bin / name
        path.write_text("#!/bin/sh\n" + script + "\n", encoding="utf-8")
        path.chmod(0o755)

    def pptx_with(self, files: dict[str, str]):
        with zipfile.ZipFile(self.pptx, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            for name, content in files.items():
                archive.writestr(name, content)

    def run_check(self, *args: str):
        return subprocess.run([sys.executable, str(CHECKER), str(self.pptx), *map(str, args)], env=self.env,
                              capture_output=True, text=True, timeout=30)

    def test_lists_declared_fonts_found_and_not_found_without_extracting(self):
        self.pptx_with({"ppt/slides/slide1.xml": '''<p:sld xmlns:p="urn:p" xmlns:a="urn:a">
          <a:r><a:rPr><a:latin typeface="Synthetic Sans"/><a:ea typeface="Missing Family"/></a:rPr></a:r>
        </p:sld>'''})
        self.tool("fc-list", "printf '%s\\n' 'Synthetic Sans,Synthetic Sans Regular'")
        p = self.run_check()
        self.assertEqual(p.returncode, 1, p.stdout + p.stderr)
        self.assertIn("Synthetic Sans", p.stdout)
        self.assertIn("本次枚举找到", p.stdout)
        self.assertIn("Missing Family", p.stdout)
        self.assertIn("本次枚举未检出", p.stdout)
        self.assertIn("不能证明", p.stdout)
        self.assertFalse((self.root / "ppt").exists(), "不将压缩包解压到磁盘")

    def test_theme_reference_and_inherited_text_remain_unknown(self):
        self.pptx_with({"ppt/slides/slide1.xml": '''<p:sld xmlns:p="urn:p" xmlns:a="urn:a">
          <a:r><a:rPr/><a:t>合成文本</a:t></a:r><a:latin typeface="Synthetic Sans"/>
          <a:ea typeface="+mn-ea"/>
        </p:sld>'''})
        self.tool("fc-list", "printf '%s\\n' 'Synthetic Sans'")
        p = self.run_check()
        self.assertEqual(p.returncode, 1, p.stdout + p.stderr)
        self.assertIn("+mn-ea", p.stdout)
        self.assertIn("无法解析", p.stdout)
        self.assertIn("继承", p.stdout)

    def test_failed_font_enumeration_keeps_declarations_visible(self):
        self.pptx_with({"ppt/slides/slide1.xml": '<p:sld xmlns:p="urn:p" xmlns:a="urn:a"><a:latin typeface="Synthetic Sans"/></p:sld>'})
        self.tool("fc-list", "exit 7")
        p = self.run_check()
        self.assertEqual(p.returncode, 2, p.stdout + p.stderr)
        self.assertIn("Synthetic Sans", p.stdout)
        self.assertIn("无法确认", p.stdout)
        self.assertIn("枚举失败", p.stderr)

    def test_found_family_alias_returns_zero_with_rendering_caveat(self):
        self.pptx_with({"ppt/slideMasters/slideMaster1.xml": '<p:sld xmlns:p="urn:p" xmlns:a="urn:a"><a:latin typeface="合成无衬线"/></p:sld>',
                        "ppt/slideLayouts/slideLayout1.xml": '<p:sld xmlns:p="urn:p" xmlns:a="urn:a"><a:latin typeface="Synthetic Sans"/></p:sld>'})
        self.tool("fc-list", "printf '%s\\n' 'Synthetic Sans,合成无衬线'")
        p = self.run_check()
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertIn("合成无衬线：本次枚举找到", p.stdout)
        self.assertIn("Synthetic Sans：本次枚举找到", p.stdout)
        self.assertIn("不能证明", p.stdout)

    def test_oversized_or_unsafe_xml_is_rejected(self):
        self.pptx_with({"ppt/slides/slide1.xml": "<slide>" + " " * (8 * 1024 * 1024) + "</slide>"})
        self.tool("fc-list", "printf '%s\\n' 'Synthetic Sans'")
        p = self.run_check()
        self.assertEqual(p.returncode, 2, p.stdout + p.stderr)
        self.assertIn("过大", p.stderr)
        self.pptx_with({"ppt/slides/slide1.xml": '<!DOCTYPE x [<!ENTITY x "boom">]><x>&x;</x>'})
        p = self.run_check()
        self.assertEqual(p.returncode, 2, p.stdout + p.stderr)
        self.assertIn("不支持的 XML", p.stderr)
        with zipfile.ZipFile(self.pptx, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("ppt/slides/slide1.xml", '<!DOCTYPE x [<!ENTITY x "boom">]><x>&x;</x>'.encode("utf-16"))
        p = self.run_check()
        self.assertEqual(p.returncode, 2, p.stdout + p.stderr)
        self.assertIn("不支持的 XML", p.stderr)

    def test_missing_font_tool_is_reported_without_hiding_declarations(self):
        self.pptx_with({"ppt/slides/slide1.xml": '<p:sld xmlns:p="urn:p" xmlns:a="urn:a"><a:latin typeface="Synthetic Sans"/></p:sld>'})
        self.env["PATH"] = str(self.bin)
        p = self.run_check()
        self.assertEqual(p.returncode, 2, p.stdout + p.stderr)
        self.assertIn("Synthetic Sans：无法确认", p.stdout)
        self.assertIn("找不到 fc-list", p.stderr)


if __name__ == "__main__":
    unittest.main()
