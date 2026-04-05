from __future__ import annotations

from src.text_segments import segment_ranges, slice_segments


def test_segment_ranges_single_segment():
    assert segment_ranges(full_length=100, segment_size=15000, overlap=400) == [(0, 100)]


def test_segment_ranges_overlap():
    r = segment_ranges(full_length=25000, segment_size=15000, overlap=500)
    assert r[0] == (0, 15000)
    assert r[1][0] == 14500
    assert r[-1][1] == 25000


def test_slice_segments_content():
    text = "a" * 100
    parts = slice_segments(text, segment_size=40, overlap=5)
    assert "".join(p[2] for p in parts) != text  # 有重叠，简单拼接不等于原文
    assert parts[0][2] == "a" * 40
    covered = set()
    for a, b, _ in parts:
        covered.update(range(a, b))
    assert covered == set(range(100))
