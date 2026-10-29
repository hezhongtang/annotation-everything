"""PPTX 字体检查的命令行测试；文档和工具输出均为合成数据。"""
from __future__ import annotations

import os
from pathlib import Path
import shutil
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

    THEME_WITH_SCRIPT_FALLBACKS = '''<a:theme xmlns:a="urn:a"><a:themeElements><a:fontScheme name="合成">
      <a:majorFont><a:latin typeface="Synthetic Sans"/><a:ea typeface=""/><a:cs typeface=""/>
        <a:font script="Jpan" typeface="Synthetic Jpan Fallback"/><a:font script="Arab" typeface="Synthetic Arab Fallback"/>
      </a:majorFont>
      <a:minorFont><a:latin typeface="Synthetic Sans"/><a:ea typeface=""/><a:cs typeface=""/>
        <a:font script="Hans" typeface="Synthetic Hans Fallback"/>
      </a:minorFont>
    </a:fontScheme></a:themeElements></a:theme>'''

    def test_theme_script_fallbacks_are_listed_without_counting_as_missing(self):
        self.pptx_with({"ppt/theme/theme1.xml": self.THEME_WITH_SCRIPT_FALLBACKS,
                        "ppt/slides/slide1.xml": '''<p:sld xmlns:p="urn:p" xmlns:a="urn:a">
          <a:r><a:rPr><a:latin typeface="Synthetic Sans"/><a:ea typeface="Synthetic Sans"/></a:rPr><a:t>合成文本</a:t></a:r></p:sld>'''})
        self.tool("fc-list", "printf '%s\\n' 'Synthetic Sans' 'Synthetic Hans Fallback'")
        p = self.run_check()
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertIn("Synthetic Sans：本次枚举找到", p.stdout)
        self.assertNotIn("Synthetic Jpan Fallback：", p.stdout)
        self.assertIn("主题按文字脚本的备选字体 3 项", p.stdout)
        self.assertIn("本次枚举未检出 2 项：Synthetic Arab Fallback、Synthetic Jpan Fallback", p.stdout)

    def test_theme_script_fallback_in_pdf_matches_by_name_without_being_required(self):
        self.pptx_with({"ppt/theme/theme1.xml": self.THEME_WITH_SCRIPT_FALLBACKS,
                        "ppt/slides/slide1.xml": '''<p:sld xmlns:p="urn:p" xmlns:a="urn:a">
          <a:r><a:rPr><a:latin typeface="Synthetic Sans"/><a:ea typeface="Synthetic Sans"/></a:rPr><a:t>合成文本</a:t></a:r></p:sld>'''})
        self.tool("fc-list", "printf '%s\\n' 'Synthetic Sans' 'Synthetic Hans Fallback'")
        pdf = self.root / "synthetic.pdf"
        pdf.write_bytes(b"%PDF synthetic")
        self.tool("pdffonts", "cat <<'EOF'\n"
                  "name                                 type              encoding         emb sub uni object ID\n"
                  "------------------------------------ ----------------- ---------------- --- --- --- ---------\n"
                  "ABCDEF+Synthetic Sans                TrueType          WinAnsi          yes yes yes      1  0\n"
                  "GHIJKL+Synthetic Hans Fallback       TrueType          Identity-H       yes yes yes      2  0\nEOF")
        p = self.run_check("--pdf", pdf)
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertIn("GHIJKL+Synthetic Hans Fallback", p.stdout)
        self.assertNotIn("未按名称对应的 PPTX 声明", p.stdout)

    def test_non_latin_text_without_explicit_east_asian_font_remains_unknown(self):
        self.pptx_with({"ppt/theme/theme1.xml": self.THEME_WITH_SCRIPT_FALLBACKS,
                        "ppt/slides/slide1.xml": '''<p:sld xmlns:p="urn:p" xmlns:a="urn:a">
          <a:r><a:rPr><a:latin typeface="Synthetic Sans"/></a:rPr><a:t>Synthetic 合成文本</a:t></a:r>
          <a:r><a:rPr><a:latin typeface="Synthetic Sans"/></a:rPr><a:t>Latin only – “quoted”</a:t></a:r></p:sld>'''})
        self.tool("fc-list", "printf '%s\\n' 'Synthetic Sans'")
        p = self.run_check()
        self.assertEqual(p.returncode, 1, p.stdout + p.stderr)
        self.assertIn("部分非拉丁文字依赖主题的东亚或复杂文种字体", p.stdout)

    def test_each_script_needs_its_own_explicit_slot(self):
        self.tool("fc-list", "printf '%s\\n' 'Synthetic Sans'")
        for props, text in (('<a:ea typeface="Synthetic Sans"/>', "مرحبا"), ('<a:cs typeface="Synthetic Sans"/>', "合成")):
            self.pptx_with({"ppt/theme/theme1.xml": self.THEME_WITH_SCRIPT_FALLBACKS,
                            "ppt/slides/slide1.xml": f'''<p:sld xmlns:p="urn:p" xmlns:a="urn:a">
              <a:r><a:rPr><a:latin typeface="Synthetic Sans"/>{props}</a:rPr><a:t>{text}</a:t></a:r></p:sld>'''})
            p = self.run_check()
            self.assertEqual(p.returncode, 1, p.stdout + p.stderr)
            self.assertIn("部分非拉丁文字依赖主题的东亚或复杂文种字体", p.stdout)

    def test_emoji_and_private_use_symbols_do_not_need_theme_script_fonts(self):
        self.pptx_with({"ppt/theme/theme1.xml": self.THEME_WITH_SCRIPT_FALLBACKS,
                        "ppt/slides/slide1.xml": '''<p:sld xmlns:p="urn:p" xmlns:a="urn:a">
          <a:r><a:rPr><a:latin typeface="Synthetic Sans"/></a:rPr><a:t>Launch \U0001F680 \uF0A7 ✓</a:t></a:r></p:sld>'''})
        self.tool("fc-list", "printf '%s\\n' 'Synthetic Sans'")
        p = self.run_check()
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)

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

    def test_pdf_font_evidence_shows_subset_embedding_and_uncertainty(self):
        self.pptx_with({"ppt/slides/slide1.xml": '<p:sld xmlns:p="urn:p" xmlns:a="urn:a"><a:latin typeface="SyntheticSans"/></p:sld>'})
        self.tool("fc-list", "printf '%s\\n' 'SyntheticSans'")
        pdf = self.root / "synthetic.pdf"
        pdf.write_bytes(b"%PDF synthetic")
        self.tool("pdffonts", "cat <<'EOF'\n"
                  "name                                 type              encoding         emb sub uni object ID\n"
                  "------------------------------------ ----------------- ---------------- --- --- --- ---------\n"
                  "ABCDEF+SyntheticSans                 TrueType          WinAnsi          yes yes yes      1  0\n"
                  "MysteryFallback                      TrueType          WinAnsi          no  no  yes      2  0\nEOF")
        p = self.run_check("--pdf", pdf)
        self.assertEqual(p.returncode, 1, p.stdout + p.stderr)
        self.assertIn("ABCDEF+SyntheticSans", p.stdout)
        self.assertIn("emb=yes", p.stdout)
        self.assertIn("sub=yes", p.stdout)
        self.assertIn("MysteryFallback", p.stdout)
        self.assertIn("emb=no", p.stdout)
        self.assertIn("待人工核查", p.stdout)
        self.assertIn("不能证明", p.stdout)

    @unittest.skipUnless(shutil.which("pdffonts"), "未安装 pdffonts")
    def test_real_pdffonts_reads_a_synthetic_pdf(self):
        self.pptx_with({"ppt/slides/slide1.xml": '<p:sld xmlns:p="urn:p" xmlns:a="urn:a"><a:latin typeface="Helvetica"/></p:sld>'})
        self.tool("fc-list", "printf '%s\\n' 'Helvetica'")
        stream = b"BT /F1 12 Tf 72 720 Td (Synthetic) Tj ET\n"
        objects = [b"<< /Type /Catalog /Pages 2 0 R >>", b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
                   b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>",
                   b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
                   b"<< /Length " + str(len(stream)).encode() + b" >>\nstream\n" + stream + b"endstream"]
        raw = bytearray(b"%PDF-1.4\n")
        offsets = [0]
        for number, obj in enumerate(objects, 1):
            offsets.append(len(raw))
            raw.extend(f"{number} 0 obj\n".encode() + obj + b"\nendobj\n")
        xref = len(raw)
        raw.extend(f"xref\n0 {len(offsets)}\n0000000000 65535 f \n".encode())
        raw.extend(b"".join(f"{offset:010d} 00000 n \n".encode() for offset in offsets[1:]))
        raw.extend(f"trailer\n<< /Size {len(offsets)} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode())
        pdf = self.root / "synthetic-real.pdf"
        pdf.write_bytes(raw)
        self.env["PATH"] = str(self.bin) + os.pathsep + str(Path(shutil.which("pdffonts")).parent) + os.pathsep + os.defpath
        p = self.run_check("--pdf", pdf)
        self.assertEqual(p.returncode, 1, p.stdout + p.stderr)
        self.assertIn("Helvetica", p.stdout)
        self.assertIn("emb=no", p.stdout)
        self.assertIn("待人工核查", p.stdout)

    def test_pdf_without_font_resources_requires_manual_review(self):
        self.pptx_with({"ppt/slides/slide1.xml": '<p:sld xmlns:p="urn:p" xmlns:a="urn:a"><a:latin typeface="SyntheticSans"/></p:sld>'})
        self.tool("fc-list", "printf '%s\\n' 'SyntheticSans'")
        self.tool("pdffonts", "printf '%s\\n' 'name                                 type              encoding         emb sub uni object ID' '------------------------------------ ----------------- ---------------- --- --- --- ---------'")
        pdf = self.root / "synthetic.pdf"
        pdf.write_bytes(b"%PDF synthetic")
        p = self.run_check("--pdf", pdf)
        self.assertEqual(p.returncode, 1, p.stdout + p.stderr)
        self.assertIn("无可枚举字体资源", p.stdout)
        self.assertIn("轮廓化", p.stdout)
        self.assertIn("待人工核查", p.stdout)

    def test_missing_pdf_tool_returns_error_after_font_enumeration(self):
        self.pptx_with({"ppt/slides/slide1.xml": '<p:sld xmlns:p="urn:p" xmlns:a="urn:a"><a:latin typeface="SyntheticSans"/></p:sld>'})
        self.tool("fc-list", "printf '%s\\n' 'SyntheticSans'")
        self.env["PATH"] = str(self.bin)
        pdf = self.root / "synthetic.pdf"
        pdf.write_bytes(b"%PDF synthetic")
        p = self.run_check("--pdf", pdf)
        self.assertEqual(p.returncode, 2, p.stdout + p.stderr)
        self.assertIn("SyntheticSans：本次枚举找到", p.stdout)
        self.assertIn("找不到 pdffonts", p.stderr)

    def test_style_name_difference_is_not_claimed_as_an_exact_match(self):
        self.pptx_with({"ppt/slides/slide1.xml": '<p:sld xmlns:p="urn:p" xmlns:a="urn:a"><a:latin typeface="SyntheticSans"/></p:sld>'})
        self.tool("fc-list", "printf '%s\\n' 'SyntheticSans'")
        self.tool("pdffonts", "cat <<'EOF'\n"
                  "name                                 type              encoding         emb sub uni object ID\n"
                  "------------------------------------ ----------------- ---------------- --- --- --- ---------\n"
                  "ABCDEF+SyntheticSans-Bold            TrueType          WinAnsi          yes yes yes      1  0\nEOF")
        pdf = self.root / "synthetic.pdf"
        pdf.write_bytes(b"%PDF synthetic")
        p = self.run_check("--pdf", pdf)
        self.assertEqual(p.returncode, 1, p.stdout + p.stderr)
        self.assertIn("SyntheticSans-Bold", p.stdout)
        self.assertIn("待人工核查", p.stdout)
        self.assertNotIn("按名称对应声明", p.stdout)

    def test_pdf_tool_timeout_is_reported(self):
        self.pptx_with({"ppt/slides/slide1.xml": '<p:sld xmlns:p="urn:p" xmlns:a="urn:a"><a:latin typeface="SyntheticSans"/></p:sld>'})
        self.tool("fc-list", "printf '%s\\n' 'SyntheticSans'")
        self.tool("pdffonts", "exec sleep 2")
        pdf = self.root / "synthetic.pdf"
        pdf.write_bytes(b"%PDF synthetic")
        run = '''
import sys
sys.path.insert(0, sys.argv[1])
import check_pptx_fonts
check_pptx_fonts.TOOL_TIMEOUT = 0.5
sys.exit(check_pptx_fonts.main([sys.argv[2], "--pdf", sys.argv[3]]))
'''
        p = subprocess.run([sys.executable, "-c", run, str(CHECKER.parent), str(self.pptx), str(pdf)],
                           env=self.env, capture_output=True, text=True, timeout=10)
        self.assertEqual(p.returncode, 2, p.stdout + p.stderr)
        self.assertIn("SyntheticSans：本次枚举找到", p.stdout)
        self.assertIn("PDF 字体检查失败或超时", p.stderr)

    def test_subset_prefix_is_not_removed_when_subsetting_is_not_confirmed(self):
        self.pptx_with({"ppt/slides/slide1.xml": '<p:sld xmlns:p="urn:p" xmlns:a="urn:a"><a:latin typeface="SyntheticSans"/></p:sld>'})
        self.tool("fc-list", "printf '%s\\n' 'SyntheticSans'")
        self.tool("pdffonts", "cat <<'EOF'\n"
                  "name                                 type              encoding         emb sub uni object ID\n"
                  "------------------------------------ ----------------- ---------------- --- --- --- ---------\n"
                  "ABCDEF+SyntheticSans                 TrueType          WinAnsi          yes no  yes      1  0\nEOF")
        pdf = self.root / "synthetic.pdf"
        pdf.write_bytes(b"%PDF synthetic")
        p = self.run_check("--pdf", pdf)
        self.assertEqual(p.returncode, 1, p.stdout + p.stderr)
        self.assertIn("待人工核查", p.stdout)
        self.assertNotIn("按名称对应声明", p.stdout)

    def test_pdf_tool_warning_prevents_a_clean_result(self):
        self.pptx_with({"ppt/slides/slide1.xml": '<p:sld xmlns:p="urn:p" xmlns:a="urn:a"><a:latin typeface="SyntheticSans"/></p:sld>'})
        self.tool("fc-list", "printf '%s\\n' 'SyntheticSans'")
        self.tool("pdffonts", "cat <<'EOF'\n"
                  "name                                 type              encoding         emb sub uni object ID\n"
                  "------------------------------------ ----------------- ---------------- --- --- --- ---------\n"
                  "ABCDEF+SyntheticSans                 TrueType          WinAnsi          yes yes yes      1  0\nEOF\n"
                  "printf '%s\\n' 'synthetic warning' >&2")
        pdf = self.root / "synthetic.pdf"
        pdf.write_bytes(b"%PDF synthetic")
        p = self.run_check("--pdf", pdf)
        self.assertEqual(p.returncode, 1, p.stdout + p.stderr)
        self.assertIn("警告", p.stderr)
        self.assertIn("待人工核查", p.stderr)

    def test_font_enumeration_warning_prevents_a_clean_result(self):
        self.pptx_with({"ppt/slides/slide1.xml": '<p:sld xmlns:p="urn:p" xmlns:a="urn:a"><a:latin typeface="SyntheticSans"/></p:sld>'})
        self.tool("fc-list", "printf '%s\\n' 'SyntheticSans'; printf '%s\\n' 'synthetic warning' >&2")
        p = self.run_check()
        self.assertEqual(p.returncode, 1, p.stdout + p.stderr)
        self.assertIn("警告", p.stderr)
        self.assertIn("待人工核查", p.stderr)


if __name__ == "__main__":
    unittest.main()
