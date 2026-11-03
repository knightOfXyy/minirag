"""压力评测共用的小型文本定位工具（评审 r2 F11：去重复实现）。"""

from __future__ import annotations


def occurrences(text: str, needle: str) -> list[int]:
    """``needle`` 在 ``text`` 中每一次字面出现的起始下标（空串返回空）。"""
    if not needle:
        return []
    out: list[int] = []
    start = 0
    while True:
        idx = text.find(needle, start)
        if idx < 0:
            return out
        out.append(idx)
        start = idx + len(needle)
