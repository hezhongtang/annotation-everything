"""技能副本的内容指纹：保留目录和软链接，不跟随软链接。"""
from __future__ import annotations

import hashlib
import os
from pathlib import Path
import stat


def snapshot(root: Path) -> dict:
    result = {".": ["dir", stat.S_IMODE(root.stat().st_mode)]}

    def visit(directory: Path) -> None:
        with os.scandir(directory) as items:
            for item in sorted(items, key=lambda entry: entry.name):
                path = Path(item.path)
                rel = path.relative_to(root).as_posix()
                if rel == ".install-record.json" or item.name == ".DS_Store":
                    continue
                if item.is_symlink():
                    result[rel] = ["link", os.readlink(path)]
                elif item.is_dir(follow_symlinks=False):
                    if item.name != "__pycache__":
                        result[rel] = ["dir", stat.S_IMODE(item.stat(follow_symlinks=False).st_mode)]
                        visit(path)
                elif item.is_file(follow_symlinks=False) and path.suffix not in (".pyc", ".pyo"):
                    result[rel] = ["file", stat.S_IMODE(item.stat(follow_symlinks=False).st_mode),
                                   hashlib.sha256(path.read_bytes()).hexdigest()]
                else:
                    result[rel] = ["other"]

    visit(root)
    return result
