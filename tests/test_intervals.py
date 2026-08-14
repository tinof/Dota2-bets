"""Reconstructing open/closed spans from change-only snapshots."""

from __future__ import annotations

from dota2bets.intervals import Interval, coverage, gaps, merge, open_intervals, overlap


def test_a_status_holds_until_the_next_snapshot():
    """The recorder writes only on change, so `open` at t=0 means open until it isn't."""
    rows = [(0, "open"), (100, "closed"), (200, "open")]
    assert open_intervals(rows, until=300) == [Interval(0, 100), Interval(200, 300)]


def test_a_line_still_open_at_the_last_poll_stops_at_the_last_poll():
    """Claiming coverage past where we looked would invent it."""
    assert open_intervals([(0, "open")], until=50) == [Interval(0, 50)]


def test_a_tombstone_ends_the_open_span():
    rows = [(0, "open"), (60, "gone")]
    assert open_intervals(rows, until=999) == [Interval(0, 60)]


def test_rows_out_of_order_are_still_read_chronologically():
    rows = [(200, "open"), (0, "open"), (100, "closed")]
    assert open_intervals(rows, until=300) == [Interval(0, 100), Interval(200, 300)]


def test_merge_unions_overlapping_spans_so_time_is_counted_once():
    spans = [Interval(0, 100), Interval(50, 150), Interval(400, 500)]
    assert merge(spans) == [Interval(0, 150), Interval(400, 500)]


def test_overlap_and_coverage_are_clipped_to_the_window():
    spans = [Interval(0, 100)]
    assert overlap(spans, Interval(50, 250)) == 50
    assert coverage(spans, Interval(50, 250)) == 0.25
    assert coverage(spans, Interval(0, 100)) == 1.0


def test_coverage_of_an_empty_window_is_undefined_rather_than_zero():
    assert coverage([Interval(0, 10)], Interval(5, 5)) is None


def test_gaps_are_the_stretches_the_market_was_not_biddable():
    spans = [Interval(0, 40), Interval(80, 200)]
    assert gaps(spans, Interval(0, 120)) == [Interval(40, 80)]
    assert gaps([], Interval(0, 10)) == [Interval(0, 10)]
    assert gaps(spans, Interval(0, 40)) == []
