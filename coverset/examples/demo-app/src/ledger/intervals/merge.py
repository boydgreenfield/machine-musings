"""Merge overlapping integer intervals."""


def merge_intervals(intervals: list[tuple[int, int]]) -> list[tuple[int, int]]:
    """Merge overlapping ``(start, end)`` intervals, given in order of their start.

    Each interval that starts before the previous merged interval ends is folded into it.
    """
    merged: list[tuple[int, int]] = []
    for start, end in intervals:
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    return merged
