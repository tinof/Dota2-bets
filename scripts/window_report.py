#!/usr/bin/env python3
"""Is a map-2/3 market still biddable while that map is being drafted?

That question decides the shape of the whole project. The research says the best edge
candidate is the draft->game-start window: the book has to reprice on the most
informative pre-game evidence there is, and it is a calibration race rather than a
latency race. But a window you cannot bet into is not an edge, and in a series the
map-2 and map-3 markets are exactly the ones a book is most likely to pull while the
previous map is still running. So before any modelling effort goes into drafts, this
measures what actually happened: when each period's market was open, when its cutoff
moved, when it was pulled, and what it was priced at last.

If those markets stay open through their drafts, Phase 2 proceeds as designed. If they
do not, the draft model has to retarget toward live-anchored betting instead.

    uv run python scripts/window_report.py
    uv run python scripts/window_report.py --series 1130278 --format tsv

Reads the database read-only, so it is safe to run while the recorder is polling.
"""

from __future__ import annotations

import argparse
import os
import sqlite3
import statistics
import sys
import time
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from dota2bets.intervals import Interval, coverage, gaps, merge, open_intervals  # noqa: E402
from dota2bets.storage import DEFAULT_DB_PATH, parse_ts  # noqa: E402

BIDDABLE_THRESHOLD = 0.9


@dataclass
class PeriodRow:
    period: int
    first_seen: int | None
    cutoff_moves: int
    cutoff_at_start: int | None
    gap_coverage: float | None
    draft_coverage: float | None
    pulls: int
    pulled_seconds: int
    open_at_start: bool
    last_close_before_start: int | None
    last_prices: dict[str, float]
    last_limit: float | None
    observed: bool = True


def connect_ro(db_path: str) -> sqlite3.Connection:
    """Open the live database without disturbing the recorder.

    Never copy the .sqlite file to read it: in WAL mode the recent commits live in the
    -wal sidecar and a plain copy reads stale.
    """
    conn = sqlite3.connect(f"file:{os.path.abspath(db_path)}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def series_maps(conn: sqlite3.Connection, series_id: int) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT match_id, start_time, duration, radiant_name, dire_name, radiant_win "
        "FROM matches WHERE series_id = ? ORDER BY start_time",
        (series_id,),
    ).fetchall()


def recorded_series(conn: sqlite3.Connection, source: str) -> list[int]:
    rows = conn.execute(
        "SELECT DISTINCT series_id FROM event_series_map "
        "WHERE source = ? AND series_id IS NOT NULL ORDER BY series_id",
        (source,),
    ).fetchall()
    return [r["series_id"] for r in rows]


def period_snapshots(conn: sqlite3.Connection, series_id: int, period: int) -> list[sqlite3.Row]:
    """Every moneyline snapshot for one map of one series.

    Unions the pre-match event with the live child Pinnacle re-lists it under, because
    once a series is running the map-2/3 prices are recorded against the child.
    """
    return conn.execute(
        "SELECT captured_at, line_key, selection, status, price_decimal, limit_amount, "
        "       cutoff_at, is_live "
        "FROM v_odds_series "
        "WHERE series_id = ? AND period = ? AND market_type = 'moneyline' AND is_kills = 0 "
        "ORDER BY captured_at, id",
        (series_id, period),
    ).fetchall()


def analyse_period(
    period: int, rows: list[sqlite3.Row], window: Interval, draft: Interval, until: int
) -> PeriodRow:
    by_line: dict[str, list[tuple[int, str | None]]] = {}
    for r in rows:
        by_line.setdefault(r["line_key"], []).append((r["captured_at"], r["status"]))
    # "Biddable" means at least one side of the moneyline was quotable at that moment.
    open_spans = merge(
        [iv for hist in by_line.values() for iv in open_intervals(hist, until=until)]
    )

    # Only cutoffs published while the market was still ahead of its map mean anything:
    # Pinnacle parks a far-future placeholder on a period whose map has not been reached.
    cutoffs: list[int] = []
    for r in rows:
        if r["captured_at"] > window.end:
            break
        c = parse_ts(r["cutoff_at"])
        if c is not None and (not cutoffs or c != cutoffs[-1]):
            cutoffs.append(c)

    pulled = gaps(open_spans, window)
    open_at_start = any(iv.start <= window.end < iv.end for iv in open_spans)
    closes = [iv.end for iv in open_spans if iv.end <= window.end]

    # An open market at a token limit is not a window anyone can bet into, so carry the
    # maximum stake alongside the price.
    last_prices: dict[str, float] = {}
    last_limit: float | None = None
    for r in rows:
        if r["status"] == "open" and r["price_decimal"] and r["captured_at"] <= window.end:
            last_prices[r["selection"]] = r["price_decimal"]
            last_limit = r["limit_amount"]

    first_seen = min((r["captured_at"] for r in rows), default=None)
    return PeriodRow(
        period=period,
        first_seen=first_seen,
        cutoff_moves=max(0, len(cutoffs) - 1),
        cutoff_at_start=cutoffs[-1] - window.end if cutoffs else None,
        gap_coverage=coverage(open_spans, window),
        draft_coverage=coverage(open_spans, draft),
        pulls=len(pulled),
        pulled_seconds=sum(g.length for g in pulled),
        open_at_start=open_at_start,
        last_close_before_start=(window.end - max(closes)) if closes else None,
        last_prices=last_prices,
        last_limit=last_limit,
        # Recording that only began after the map did says nothing about the market;
        # counting it as 0% would report a gap in the book that was a gap in us.
        observed=first_seen is not None and first_seen <= window.end,
    )


def _pct(x: float | None) -> str:
    return "-" if x is None else f"{100 * x:.1f}%"


def _mins(x: int | None) -> str:
    return "-" if x is None else f"{x / 60:+.1f}m"


def _stamp(t: int | None) -> str:
    return "-" if not t else time.strftime("%m-%d %H:%M", time.gmtime(t)) + "Z"


def report_series(
    conn: sqlite3.Connection, series_id: int, draft_window: int, until: int
) -> tuple[list[str], list[PeriodRow]]:
    maps = series_maps(conn, series_id)
    if not maps:
        return [], []
    finished = [m for m in maps if m["radiant_win"] is not None]
    teams = f"{maps[0]['radiant_name']} vs {maps[0]['dire_name']}"
    lines = [f"\n## Series {series_id} — {teams}", ""]
    for i, m in enumerate(maps, 1):
        end = (m["start_time"] or 0) + (m["duration"] or 0)
        lines.append(
            f"- map {i}: {time.strftime('%H:%M', time.gmtime(m['start_time']))}"
            f"–{time.strftime('%H:%M', time.gmtime(end))}Z"
            f" ({(m['duration'] or 0) // 60}m), "
            + ("unfinished" if m["radiant_win"] is None else "finished")
        )
    lines += [
        "",
        "| map | first seen | cutoff moves | open% pre-map | open% draft | "
        "pulls | at map start | last prices | max stake |",
        "|---|---|---|---|---|---|---|---|---|",
    ]

    out: list[PeriodRow] = []
    for i, m in enumerate(finished, 1):
        rows = period_snapshots(conn, series_id, i)
        if not rows:
            continue
        start = m["start_time"]
        prev_end = None
        if i > 1:
            prev = finished[i - 2]
            prev_end = (prev["start_time"] or 0) + (prev["duration"] or 0)
        window_start = prev_end if prev_end else min(r["captured_at"] for r in rows)
        window = Interval(window_start, start)
        # The draft runs in the minutes before the horn, which is what OpenDota's
        # start_time marks -- so the draft window ends where the map begins.
        draft = Interval(max(window_start, start - draft_window), start)
        pr = analyse_period(i, rows, window, draft, until)
        out.append(pr)
        prices = ", ".join(f"{k} {v:.2f}" for k, v in sorted(pr.last_prices.items()))
        if not pr.observed:
            lines.append(
                f"| {i} | {_stamp(pr.first_seen)} | - | not recorded yet | - | - | - | - | - |"
            )
            continue
        closed_at = _mins(-pr.last_close_before_start) if pr.last_close_before_start else "?"
        at_start = "still open" if pr.open_at_start else f"closed {closed_at}"
        lines.append(
            f"| {i} | {_stamp(pr.first_seen)} "
            f"| {pr.cutoff_moves} "
            f"| {_pct(pr.gap_coverage)} | {_pct(pr.draft_coverage)} "
            f"| {pr.pulls} ({pr.pulled_seconds // 60}m) "
            f"| {at_start} | {prices or '-'} "
            f"| {'-' if pr.last_limit is None else f'{pr.last_limit:,.0f}'} |"
        )
    return lines, out


def summarise(all_rows: list[PeriodRow]) -> list[str]:
    all_rows = [r for r in all_rows if r.observed]
    lines = ["\n## Verdict — are later maps biddable through their drafts?", ""]
    lines.append(
        "| map | periods | median open% in draft | share ≥90% | still open at start "
        "| median max stake |"
    )
    lines.append("|---|---|---|---|---|---|")
    for period in sorted({r.period for r in all_rows}):
        rows = [r for r in all_rows if r.period == period]
        cov = [r.draft_coverage for r in rows if r.draft_coverage is not None]
        if not cov:
            continue
        share = sum(1 for c in cov if c >= BIDDABLE_THRESHOLD) / len(cov)
        still_open = sum(1 for r in rows if r.open_at_start) / len(rows)
        limits = [r.last_limit for r in rows if r.last_limit]
        lines.append(
            f"| {period} | {len(cov)} | {_pct(statistics.median(cov))} "
            f"| {100 * share:.0f}% | {100 * still_open:.0f}% "
            f"| {'-' if not limits else f'{statistics.median(limits):,.0f}'} |"
        )
    lines += [
        "",
        "Caveats: the draft window is taken as the minutes before OpenDota's `start_time`,"
        " which marks the horn rather than the start of the draft, so its bounds are an"
        " approximation. `open` means Pinnacle quoted the map's moneyline; the max-stake"
        " column is what says whether that quote was worth anything.",
    ]
    later = [r for r in all_rows if r.period > 1 and r.draft_coverage is not None]
    if later:
        share = sum(1 for r in later if r.draft_coverage >= BIDDABLE_THRESHOLD) / len(later)
        lines += [
            "",
            f"Maps 2+ open through ≥{100 * BIDDABLE_THRESHOLD:.0f}% of their draft window: "
            f"{100 * share:.0f}% of {len(later)} periods.",
            (
                "Phase 2's draft model has a window to bet into."
                if share >= 0.5
                else "The draft window is mostly unbettable — retarget toward "
                "live-anchored betting before building the draft model."
            ),
        ]
    return lines


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    p.add_argument("--db", default=str(DEFAULT_DB_PATH))
    p.add_argument("--series", type=int, action="append", help="limit to these series ids")
    p.add_argument("--source", default="pinnacle")
    p.add_argument(
        "--draft-window",
        type=int,
        default=600,
        help="seconds before a map's start treated as its draft",
    )
    p.add_argument("--format", choices=["md", "tsv"], default="md")
    args = p.parse_args(argv)

    conn = connect_ro(args.db)
    until = conn.execute("SELECT MAX(captured_at) FROM odds_snapshots").fetchone()[0] or 0
    series = args.series or recorded_series(conn, args.source)
    if not series:
        print("No resolved series. Run `dota2bets resolve` first.")
        return 1

    body: list[str] = [
        "# Window experiment — map markets through their drafts",
        "",
        f"Source: {args.source}. Draft window: {args.draft_window}s before each map's start.",
        "Pre-map window runs from the previous map's end (or first sighting) to this map's start.",
    ]
    all_rows: list[PeriodRow] = []
    for series_id in series:
        lines, rows = report_series(conn, series_id, args.draft_window, until)
        if rows:
            body += lines
            all_rows += rows
    if not all_rows:
        print("No finished series with recorded map markets yet.")
        return 1
    body += summarise(all_rows)

    if args.format == "tsv":
        print("series_period\tfirst_seen\tcutoff_moves\tgap_cov\tdraft_cov\tpulls\tpulled_s")
        for r in all_rows:
            print(
                f"{r.period}\t{r.first_seen}\t{r.cutoff_moves}\t{r.gap_coverage}\t"
                f"{r.draft_coverage}\t{r.pulls}\t{r.pulled_seconds}"
            )
    else:
        print("\n".join(body))
    return 0


if __name__ == "__main__":
    sys.exit(main())
