"""运行时脚本的公共小工具：原子写入、文件锁、CSP 脚本哈希。只依赖标准库，不导入本目录的其他模块。"""
from __future__ import annotations

import base64
import contextlib
import fcntl
import hashlib
import os
import re
import tempfile
from pathlib import Path
from typing import Iterator


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


@contextlib.contextmanager
def flocked(path: Path, *, exclusive: bool = False, nonblocking: bool = False,
            nofollow: bool = False) -> Iterator[int]:
    """打开锁文件、按模式 flock、退出时解锁：文件锁只有一个实现，各处只差显式参数。

    exclusive：独占（LOCK_EX）还是共享（LOCK_SH）；nonblocking：拿不到锁立刻抛
    BlockingIOError，否则等待；nofollow：拒绝跟随符号链接（O_NOFOLLOW）——按各处原来的
    行为，只有台账的更新协调锁为真，其余为假（放行符号链接的兼容问题另行评估）。
    锁文件以 0600 新建；yield 文件描述符，调用方不需要时可以忽略。
    锁的粒度与持有范围由调用方的 with 块决定，这里不加也不减。
    """
    flags = os.O_RDWR | os.O_CREAT | (getattr(os, "O_NOFOLLOW", 0) if nofollow else 0)
    fd = os.open(path, flags, 0o600)
    with os.fdopen(fd, "r+") as fh:
        mode = fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH
        fcntl.flock(fh, mode | (fcntl.LOCK_NB if nonblocking else 0))
        try:
            yield fh.fileno()
        finally:
            fcntl.flock(fh, fcntl.LOCK_UN)


INLINE_SCRIPT = re.compile(r"<script>(.*?)</script>", re.S)


def script_hashes(html_text: str) -> str:
    """页面里内联脚本的 sha256 列表（CSP 的 script-src 用）：服务响应头与页面 meta 共用这一份算法。

    两个出口各有一条自己的策略串（响应头版带 frame-ancestors 与无脚本时的兜底，meta 版都没有），
    但哈希算法、匹配的正则与 base64 编码只在这里算一次。
    """
    return " ".join("'sha256-" + base64.b64encode(hashlib.sha256(s.encode("utf-8")).digest()).decode() + "'"
                    for s in INLINE_SCRIPT.findall(html_text))
