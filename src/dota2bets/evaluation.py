"""Score predictions against the closing market, never against raw accuracy.

A Dota model that calls 60% of matches correctly sounds good and means nothing: the
favourite wins about that often, and the book already knew. The only questions that
decide whether a bet was worth placing are whether the price beat the *closing* price
(CLV) and whether the probability was better calibrated than the closing probability
(Brier, on the same rows). This module computes both, so every model built later is
graded the same way from its first prediction.

Two things have to be right or the numbers lie:

**The de-vig.** A book's quoted probabilities sum to more than one. Removing that
overround proportionally assumes the margin is spread evenly, which it is not --
books load the longshot. Shin's model instead assumes the margin comes from insider
money and backs out the insider fraction ``z``, which shifts probability toward the
favourite. Both are provided; ``shin`` is the default.

**The closing line itself.** It must come from the pre-match event, bounded by the map's
actual horn:

* Pinnacle re-lists a series under a *new, live* event id when it goes live, so the
  price nearest kick-off under that child is a live price, not a close. Only the
  parentless event in ``event_series_map`` is the pre-match one.
* A period's ``cutoff_at`` is a far-future placeholder until its map is next, so it
  cannot bound anything; when a cutoff is real, Pinnacle flips the rows to ``closed``,
  which the ``status='open'`` filter already drops. The bound is therefore
  ``matches.start_time`` from OpenDota -- never Pinnacle's ``start_time``, which is the
  scheduled slot and drifts by over an hour.
"""

from __future__ import annotations

import json
import logging
import math
import sqlite3
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from typing import Any

from .aliases import AliasResolver, Team, _norm
from .storage import parse_ts

log = logging.getLogger(__name__)

#: OpenDota `series_type` -> map wins needed to take the series. Type 3 (best-of-two)
#: is deliberately absent: it can end 1-1, so there is no series winner to resolve.
#: Defaulting it to 1 would crown the map-1 winner, silently scoring a series bet that
#: never settled.
WINS_NEEDED = {0: 1, 1: 2, 2: 3}


# --------------------------------------------------------------------------- de-vig


def overround(decimal_odds: Sequence[float]) -> float:
    """The book total ``S = sum(1/o)``; the amount above 1.0 is the margin."""
    return sum(1.0 / o for o in decimal_odds)


def implied_probs(decimal_odds: Sequence[float]) -> list[float]:
    """Raw ``1/o`` per outcome -- deliberately *not* normalised."""
    return [1.0 / o for o in decimal_odds]


def devig_proportional(decimal_odds: Sequence[float]) -> list[float]:
    """Scale the raw probabilities to sum to one, spreading the margin evenly."""
    total = overround(decimal_odds)
    return [p / total for p in implied_probs(decimal_odds)]


def _shin_probs(pi: Sequence[float], s: float, z: float) -> list[float]:
    return [(math.sqrt(z * z + 4.0 * (1.0 - z) * p * p / s) - z) / (2.0 * (1.0 - z)) for p in pi]


def shin_z(decimal_odds: Sequence[float], max_iter: int = 100) -> float | None:
    """The implied insider fraction, or None for a book with no margin to remove.

    ``sum(p_i(z))`` decreases strictly in z on [0, 0.5), so a bisection converges.
    """
    pi = implied_probs(decimal_odds)
    s = sum(pi)
    if s <= 1.0:
        return None
    lo, hi = 0.0, 0.5
    for _ in range(max_iter):
        mid = (lo + hi) / 2.0
        if sum(_shin_probs(pi, s, mid)) > 1.0:
            lo = mid
        else:
            hi = mid
    return (lo + hi) / 2.0


def devig_shin(decimal_odds: Sequence[float], max_iter: int = 100) -> list[float]:
    """Shin de-vig: attributes the margin to insider money, favouring the favourite.

    An underround book (``S <= 1``) has no margin for the model to explain, so this
    degrades to the proportional result rather than failing.
    """
    z = shin_z(decimal_odds, max_iter=max_iter)
    if z is None:
        log.warning("underround book (S<=1); falling back to proportional de-vig")
        return devig_proportional(decimal_odds)
    pi = implied_probs(decimal_odds)
    probs = _shin_probs(pi, sum(pi), z)
    total = sum(probs)
    return [p / total for p in probs]


def devig(decimal_odds: Sequence[float], method: str = "shin") -> list[float]:
    if method == "proportional":
        return devig_proportional(decimal_odds)
    if method == "shin":
        return devig_shin(decimal_odds)
    raise ValueError(f"unknown de-vig method {method!r}")


# ----------------------------------------------------------------------- closing line


@dataclass(frozen=True)
class ClosingLine:
    """The last biddable pre-match quote for one market, with the vig removed."""

    series_id: int
    period: int
    event_id: str
    market_type: str
    points: float | None
    selections: dict[str, float]
    prices: dict[str, float]
    overround: float
    z: float | None
    captured_at: int
    bound: int
    stale_cutoff: bool


def closing_event(
    conn: sqlite3.Connection, source: str, series_id: int, is_kills: int = 0
) -> str | None:
    """The pre-match event for a series -- the parentless one, never a live re-list."""
    rows = conn.execute(
        "SELECT event_id FROM event_series_map "
        "WHERE source = ? AND series_id = ? AND is_kills = ? AND parent_event_id IS NULL "
        "ORDER BY event_id",
        (source, series_id, is_kills),
    ).fetchall()
    if not rows:
        return None
    if len(rows) > 1:
        log.warning(
            "series %s has %d parentless %s events; using %s",
            series_id,
            len(rows),
            source,
            rows[0][0],
        )
    return str(rows[0][0])


def map_start(conn: sqlite3.Connection, series_id: int, period: int) -> int | None:
    """Horn time of the map a period prices: period 0 = the series, period k = map k."""
    if period == 0:
        row = conn.execute(
            "SELECT MIN(start_time) FROM matches WHERE series_id = ? AND start_time IS NOT NULL",
            (series_id,),
        ).fetchone()
        return int(row[0]) if row and row[0] is not None else None
    row = conn.execute(
        "SELECT start_time FROM matches WHERE series_id = ? AND start_time IS NOT NULL "
        "ORDER BY start_time LIMIT 1 OFFSET ?",
        (series_id, period - 1),
    ).fetchone()
    return int(row[0]) if row else None


def closing_lines(
    conn: sqlite3.Connection,
    source: str,
    event_id: str,
    market_type: str,
    period: int,
    before: int | None = None,
) -> list[sqlite3.Row]:
    """Last open, non-live snapshot per (selection, points) strictly before ``before``.

    ``points`` is in the partition so alternate totals lines never collapse together.
    ``before=None`` leaves the query unbounded and is for tests; callers pass the horn.
    """
    return conn.execute(
        "SELECT * FROM ("
        "  SELECT *, ROW_NUMBER() OVER ("
        "    PARTITION BY selection, points ORDER BY captured_at DESC, id DESC) rn"
        "  FROM odds_snapshots"
        "  WHERE source = ? AND event_id = ? AND market_type = ? AND period = ?"
        "    AND status = 'open' AND (is_live IS NULL OR is_live = 0)"
        "    AND (? IS NULL OR captured_at < ?)"
        ") WHERE rn = 1",
        (source, event_id, market_type, period, before, before),
    ).fetchall()


def closing_probs(
    conn: sqlite3.Connection,
    source: str,
    series_id: int,
    period: int,
    market_type: str = "moneyline",
    method: str = "shin",
    before: int | None = None,
    is_kills: int = 0,
    points: float | None = None,
) -> ClosingLine | None:
    """De-vigged closing probabilities for one series/period, or None if unavailable."""
    event_id = closing_event(conn, source, series_id, is_kills=is_kills)
    if event_id is None:
        return None
    horn = map_start(conn, series_id, period)
    bounds = [b for b in (horn, before) if b is not None]
    if not bounds:
        return None
    bound = min(bounds)
    rows = [
        r
        for r in closing_lines(conn, source, event_id, market_type, period, bound)
        if r["price_decimal"]
    ]
    # A totals market quotes many alternate lines at once; each `points` group is its
    # own two-sided book and only de-vigs correctly on its own.
    groups: dict[Any, list[sqlite3.Row]] = {}
    for row in rows:
        groups.setdefault(row["points"], []).append(row)
    if points is not None:
        rows = groups.get(points, [])
    elif len(groups) > 1:
        # The main line is the one the book quoted most recently.
        rows = max(groups.values(), key=lambda g: max(int(r["captured_at"]) for r in g))
    if len(rows) < 2:
        return None
    prices = {str(r["selection"]): float(r["price_decimal"]) for r in rows}
    names = list(prices)
    odds = [prices[n] for n in names]
    probs = devig(odds, method=method)
    stale = any(
        (parse_ts(r["cutoff_at"]) or 0) < int(r["captured_at"]) for r in rows if r["cutoff_at"]
    )
    return ClosingLine(
        series_id=series_id,
        period=period,
        event_id=event_id,
        market_type=market_type,
        points=rows[0]["points"],
        selections=dict(zip(names, probs, strict=True)),
        prices=prices,
        overround=overround(odds),
        z=shin_z(odds),
        captured_at=max(int(r["captured_at"]) for r in rows),
        bound=bound,
        stale_cutoff=stale,
    )


# ------------------------------------------------------------------------ CLV report


@dataclass(frozen=True)
class Prediction:
    """One model call, as it would have been acted on."""

    series_id: int
    period: int
    selection: str
    prob: float
    market_type: str = "moneyline"
    price_taken: float | None = None
    placed_at: int | None = None


@dataclass(frozen=True)
class BetResult:
    prediction: Prediction
    closing_prob: float | None
    closing_price: float | None
    edge_at_close: float | None
    clv: float | None
    outcome: int | None
    brier: float | None


@dataclass
class ClvReport:
    rows: list[BetResult] = field(default_factory=list)
    n_scored: int = 0
    model_brier: float | None = None
    closing_brier: float | None = None
    mean_edge_at_close: float | None = None
    mean_clv: float | None = None
    skipped: list[tuple[Prediction, str]] = field(default_factory=list)


def _mean(values: Sequence[float]) -> float | None:
    return sum(values) / len(values) if values else None


def _series_matches(conn: sqlite3.Connection, series_id: int) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT match_id, series_type, start_time, radiant_team_id, dire_team_id, radiant_win "
        "FROM matches WHERE series_id = ? AND start_time IS NOT NULL ORDER BY start_time",
        (series_id,),
    ).fetchall()


def _map_outcome(row: sqlite3.Row, team: Team) -> int | None:
    if row["radiant_win"] is None:
        return None
    if row["radiant_team_id"] == team.team_id:
        return 1 if row["radiant_win"] else 0
    if row["dire_team_id"] == team.team_id:
        return 0 if row["radiant_win"] else 1
    return None


def resolve_outcome(
    conn: sqlite3.Connection, series_id: int, period: int, team: Team
) -> tuple[int | None, str | None]:
    """Did ``team`` win this map (or series)? Returns ``(outcome, skip_reason)``."""
    maps = _series_matches(conn, series_id)
    if not maps:
        return None, "no matches for series"
    if period > 0:
        if period > len(maps):
            return None, "map not played"
        outcome = _map_outcome(maps[period - 1], team)
        if outcome is None:
            return None, "map undecided or team not in match"
        return outcome, None
    needed = WINS_NEEDED.get(maps[0]["series_type"] or 0)
    if needed is None:
        return None, "series format has no winner"
    wins = 0
    others = 0
    for row in maps:
        result = _map_outcome(row, team)
        if result == 1:
            wins += 1
        elif result == 0:
            others += 1
    if wins >= needed:
        return 1, None
    if others >= needed:
        return 0, None
    return None, "series undecided"


def _lookup_prob(mapping: dict[str, float], selection: str) -> float | None:
    target = _norm(selection)
    for name, value in mapping.items():
        if _norm(name) == target:
            return value
    return None


def clv_report(
    conn: sqlite3.Connection,
    predictions: Iterable[Prediction],
    source: str = "pinnacle",
    method: str = "shin",
    resolver: AliasResolver | None = None,
    before_draft_s: int | None = None,
) -> ClvReport:
    """Score predictions against closing lines: CLV, and Brier against the same rows.

    ``closing_brier`` is computed over the identical scored subset -- it is the bar the
    model has to clear, and mixing denominators would make it meaningless.
    """
    resolver = resolver or AliasResolver.load()
    report = ClvReport()
    cache: dict[tuple[int, int, str], ClosingLine | None] = {}
    model_briers: list[float] = []
    closing_briers: list[float] = []
    edges: list[float] = []
    clvs: list[float] = []

    for pred in predictions:
        team = resolver.resolve(pred.selection)
        if team is None:
            report.skipped.append((pred, "unresolved selection"))
            continue
        key = (pred.series_id, pred.period, pred.market_type)
        if key not in cache:
            before = None
            if before_draft_s is not None:
                horn = map_start(conn, pred.series_id, pred.period)
                before = horn - before_draft_s if horn is not None else None
            cache[key] = closing_probs(
                conn,
                source,
                pred.series_id,
                pred.period,
                market_type=pred.market_type,
                method=method,
                before=before,
            )
        line = cache[key]
        if line is None:
            report.skipped.append((pred, "no closing line"))
            continue
        closing_prob = _lookup_prob(line.selections, pred.selection)
        if closing_prob is None:
            report.skipped.append((pred, "selection not quoted"))
            continue
        closing_price = _lookup_prob(line.prices, pred.selection)
        outcome, reason = resolve_outcome(conn, pred.series_id, pred.period, team)
        if outcome is None:
            report.skipped.append((pred, reason or "unresolved outcome"))
            continue

        edge = pred.prob - closing_prob
        clv = pred.price_taken * closing_prob - 1.0 if pred.price_taken else None
        brier = (pred.prob - outcome) ** 2
        report.rows.append(
            BetResult(
                prediction=pred,
                closing_prob=closing_prob,
                closing_price=closing_price,
                edge_at_close=edge,
                clv=clv,
                outcome=outcome,
                brier=brier,
            )
        )
        model_briers.append(brier)
        closing_briers.append((closing_prob - outcome) ** 2)
        edges.append(edge)
        if clv is not None:
            clvs.append(clv)

    report.n_scored = len(report.rows)
    report.model_brier = _mean(model_briers)
    report.closing_brier = _mean(closing_briers)
    report.mean_edge_at_close = _mean(edges)
    report.mean_clv = _mean(clvs)
    return report


def load_predictions(path: str) -> list[Prediction]:
    """Read a JSONL predictions file, one object per bet."""
    fields = {f for f in Prediction.__dataclass_fields__}
    out: list[Prediction] = []
    with open(path, encoding="utf-8") as fh:
        for lineno, raw in enumerate(fh, 1):
            text = raw.strip()
            if not text:
                continue
            obj: dict[str, Any] = json.loads(text)
            unknown = set(obj) - fields
            if unknown:
                raise ValueError(f"{path}:{lineno}: unknown prediction fields {sorted(unknown)}")
            out.append(Prediction(**obj))
    return out
