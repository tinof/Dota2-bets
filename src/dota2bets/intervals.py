"""Turning change-only snapshots back into intervals of time.

The recorder writes a row only when something about a line changed, so a status holds
from its row until the next row for the same line. Every question the window experiment
asks -- was this market biddable during that draft, how long was it pulled -- is really
a question about those implied intervals, so the reconstruction lives here where it can
be tested without a database.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass

OPEN_STATUS = "open"


@dataclass(frozen=True)
class Interval:
    start: int
    end: int

    @property
    def length(self) -> int:
        return max(0, self.end - self.start)


def open_intervals(
    rows: Sequence[tuple[int, str | None]], until: int, open_status: str = OPEN_STATUS
) -> list[Interval]:
    """Spans during which one line was open, from its (captured_at, status) history.

    `until` bounds the final span: a line still open at the last poll is open only as far
    as we actually looked, and claiming otherwise would invent coverage.
    """
    intervals: list[Interval] = []
    start: int | None = None
    for captured_at, status in sorted(rows, key=lambda r: r[0]):
        if status == open_status:
            if start is None:
                start = captured_at
        elif start is not None:
            intervals.append(Interval(start, captured_at))
            start = None
    if start is not None and until > start:
        intervals.append(Interval(start, until))
    return intervals


def merge(intervals: Iterable[Interval]) -> list[Interval]:
    """Union of possibly overlapping intervals, so time is never counted twice."""
    ordered = sorted(intervals, key=lambda i: i.start)
    out: list[Interval] = []
    for iv in ordered:
        if iv.length == 0:
            continue
        if out and iv.start <= out[-1].end:
            out[-1] = Interval(out[-1].start, max(out[-1].end, iv.end))
        else:
            out.append(iv)
    return out


def overlap(intervals: Iterable[Interval], window: Interval) -> int:
    """Seconds of `window` covered by `intervals`."""
    return sum(
        max(0, min(iv.end, window.end) - max(iv.start, window.start)) for iv in merge(intervals)
    )


def coverage(intervals: Iterable[Interval], window: Interval) -> float | None:
    """Fraction of `window` covered, or None for a window with no duration."""
    if window.length <= 0:
        return None
    return overlap(intervals, window) / window.length


def gaps(intervals: Iterable[Interval], window: Interval) -> list[Interval]:
    """The uncovered stretches inside `window` -- when the market was not biddable."""
    out: list[Interval] = []
    cursor = window.start
    for iv in merge(intervals):
        if iv.end <= window.start or iv.start >= window.end:
            continue
        if iv.start > cursor:
            out.append(Interval(cursor, min(iv.start, window.end)))
        cursor = max(cursor, iv.end)
    if cursor < window.end:
        out.append(Interval(cursor, window.end))
    return [iv for iv in out if iv.length > 0]
