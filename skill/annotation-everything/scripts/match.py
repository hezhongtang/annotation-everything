"""报告 ↔ 讲稿对应句查找：用户选中一侧的文字时，找另一侧同一页里相同或相近的句子，提醒「另一侧也要改」。

与页面里的 JavaScript 版本算法一致：
1. 去掉空白、统一全半角标点后，整段原样出现在另一侧 → 相同；
2. 否则按句切分另一侧，按字的二元组重合度打分，共有数字（12%、2 个百分点……）加分，过阈值 → 相似。
只在同一页内找，误报少。只用标准库。
"""
from __future__ import annotations

import re

PUNCT = str.maketrans({"，": ",", "。": ".", "．": ".", "：": ":", "；": ";", "（": "(", "）": ")", "“": '"', "”": '"',
                       "「": '"', "」": '"', "『": '"', "』": '"', "‘": "'", "’": "'", "％": "%", "！": "!", "？": "?"})
NUM = re.compile(r"\d+(?:\.\d+)?(?:%|个百分点|百分点|元|万|亿|倍|家|人|分钟|秒|页|个)?")
THRESHOLD = 0.45


def norm(s: str) -> str:
    return re.sub(r"\s+", "", str(s)).translate(PUNCT).lower()


def bigrams(s: str) -> set[str]:
    t = re.sub(r"[^\w%.]", "", norm(s))
    return {t[k:k + 2] for k in range(len(t) - 1)}


def numbers(s: str) -> list[str]:
    return NUM.findall(norm(s))


def sentences(text: str) -> list[str]:
    parts = re.split(r"(?<=[。！？!?；;\n])", text)
    return [p.strip() for p in parts if len(p.strip()) >= 2]


def counterpart(text: str, hay: str) -> tuple[str, str] | None:
    """在 hay 里找 text 的对应句，返回 (match, 句子)，match 为 exact / similar；找不到返回 None。"""
    if not text or not hay or not hay.strip():
        return None
    nt = norm(text)
    if len(nt) >= 4 and nt in norm(hay):
        return "exact", text
    a, na = bigrams(text), numbers(text)
    if len(a) < 2:
        return None
    best: tuple[float, str] | None = None
    for s in sentences(hay):
        b = bigrams(s)
        inter = len(a & b)
        jac = inter / (len(a) + len(b) - inter or 1)
        cover = inter / len(a)
        sn = numbers(s)
        shared = sum(1 for x in na if x in sn)
        score = max(jac, cover * 0.8) + (0.15 * min(shared, 2) if shared else 0)
        if best is None or score > best[0]:
            best = (score, s)
    if best and best[0] >= THRESHOLD:
        return "similar", best[1][:300]
    return None
