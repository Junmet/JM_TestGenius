"""
长文档分段：用于分段大纲（overlap 减少边界处测试点丢失）。
"""
from __future__ import annotations


def segment_ranges(*, full_length: int, segment_size: int, overlap: int) -> list[tuple[int, int]]:
    """
    返回半开区间 [start, end) 列表，覆盖 [0, full_length)。
    segment_size：每段最大长度；overlap：相邻段重叠字符数（>=0）。
    """
    if full_length <= 0:
        return []
    if segment_size <= 0:
        raise ValueError("segment_size 必须为正")
    ov = max(0, min(overlap, segment_size - 1))
    ranges: list[tuple[int, int]] = []
    start = 0
    while start < full_length:
        end = min(full_length, start + segment_size)
        ranges.append((start, end))
        if end >= full_length:
            break
        start = end - ov
        if start < 0:
            start = 0
        if ranges and start <= ranges[-1][0]:
            start = ranges[-1][1]
    return ranges


def slice_segments(text: str, segment_size: int, overlap: int) -> list[tuple[int, int, str]]:
    """返回 (start, end, chunk_text)。"""
    n = len(text)
    specs = segment_ranges(full_length=n, segment_size=segment_size, overlap=overlap)
    return [(a, b, text[a:b]) for a, b in specs]
