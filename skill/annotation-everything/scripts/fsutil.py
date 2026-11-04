"""文件写入的公共小工具：只依赖标准库，不导入本目录的其他模块。"""
from __future__ import annotations

import contextlib
import os
import tempfile
from pathlib import Path


def atomic_write(path: Path, text: str, *, prefix: str, fsync: bool = True) -> None:
    """原子写入文本：同目录临时文件（mkstemp 建出的文件是 0600）+ fsync + os.replace。

    fsync=False 用于频繁刷新、丢了也能重建的文件（如等待标记），省掉每次落盘的等待。

    读方不会读到写了一半的文件；写入或替换失败时删掉临时文件，再把原异常抛出。
    """
    fd, tmp = tempfile.mkstemp(prefix=prefix, suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
            if fsync:
                fh.flush()
                os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(tmp)
        raise
