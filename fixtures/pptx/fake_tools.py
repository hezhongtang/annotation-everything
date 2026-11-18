"""为 PPTX 管线测试生成可直接启动的合成工具。"""
import os
import sys
from pathlib import Path


def write_python_tool(bin_dir: Path, name: str, body: str) -> Path:
    if os.name == "nt":
        (bin_dir / f"{name}.py").write_text(body + "\n", encoding="utf-8")
        launcher = bin_dir / f"{name}.cmd"
        launcher.write_bytes(f'@echo off\r\n"{sys.executable}" "%~dp0{name}.py" %*\r\n'.encode("utf-8"))
        return launcher
    launcher = bin_dir / name
    launcher.write_text(f"#!{sys.executable}\n{body}\n", encoding="utf-8")
    launcher.chmod(0o755)
    return launcher
