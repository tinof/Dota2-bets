"""Walk-forward scoring of the ratings model, and predictions for a live event.

Two questions, deliberately kept apart:

**Is the model any good at all?** ``walk_forward`` predicts every decided map in the
history from the ratings that existed before it, then folds the result in. That is a
leak-free Brier and log-loss over tens of thousands of maps, against the only baseline
available without a market: the 0.5 coin flip. It is what the hyperparameters are tuned
on, and it is bounded *before* the target event so tuning can never see it.

**Is it good enough to bet?** That question is only answerable against the closing line,
so ``event_predictions`` emits ``evaluation.Prediction`` rows and hands them to
``dota2bets eval``. Raw accuracy is not reported anywhere on purpose.

The odds recorder came up part-way through TI 2026, so many played series have no
captured line at all. ``match_report`` therefore scores *every* decided map of the event
against its outcome, which uses the days the recorder missed; the CLV path uses the
subset that has a close. Neither number substitutes for the other.
"""

from __future__ import annotations

import json
import logging
import math
import sqlite3
import time
from collections.abc import Iterable, Iterator, Sequence
from dataclasses import dataclass, field

from .evaluation import Prediction
from .ratings import Glicko2Config, RatingBook, apply_row, rating_rows, series_win_prob

log = logging.getLogger(__name__)

#: Probabilities are clamped before the log so a confident miss costs a large finite
#: number rather than infinity.
_LOG_CLAMP = 1e-6

DEFAULT_LEAGUE_NAME = "The International 2026"

#: Substrings identifying the leagues worth *scoring* on. OpenDota's /proMatches is
#: dominated by low-tier grinder circuits -- "Destiny League" alone is 7.6k of 62k
#: matches -- whose results are close to coin flips, so an unfiltered Brier measures
#: mostly their noise and hides whether the model works where it would be bet. Those
#: matches still *train* the ratings (they carry real information about the teams that
#: play both circuits); they are simply not the population the model is judged on.
ELITE_LEAGUE_KEYWORDS = (
    "international",
    "major",
    "dreamleague",
    "esl one",
    "betboom",
    "pgl",
    "riyadh",
    "blast",
    "fissure",
    "elite league",
    "epl",
    "wallachia",
    "games of the future",
)


def elite_league_ids(conn: sqlite3.Connection) -> set[int]:
    """League ids whose name marks them as top-tier, by substring match."""
    return {
        int(row["league_id"])
        for row in conn.execute(
            "SELECT DISTINCT league_id, league_name FROM matches WHERE league_name IS NOT NULL"
        )
        if any(k in str(row["league_name"]).lower() for k in ELITE_LEAGUE_KEYWORDS)
    }


def _log_loss(prob: float, outcome: int) -> float:
    p = min(max(prob, _LOG_CLAMP), 1.0 - _LOG_CLAMP)
    return -math.log(p if outcome else 1.0 - p)


@dataclass
class ScoreCard:
    """Proper scores over a set of binary predictions."""

    n: int = 0
    brier: float | None = None
    log_loss: float | None = None
    #: Brier of always saying 0.5 -- the only baseline available without a market.
    baseline_brier: float = 0.25

    @classmethod
    def build(cls, scored: Sequence[tuple[float, int]]) -> ScoreCard:
        if not scored:
            return cls()
        return cls(
            n=len(scored),
            brier=sum((p - o) ** 2 for p, o in scored) / len(scored),
            log_loss=sum(_log_loss(p, o) for p, o in scored) / len(scored),
        )


def walk_forward(
    conn: sqlite3.Connection,
    config: Glicko2Config | None = None,
    *,
    start_scoring: int | None = None,
    end: int | None = None,
    score_leagues: set[int] | None = None,
    min_games: int = 0,
) -> ScoreCard:
    """Predict-then-update over the history; score only maps after ``start_scoring``.

    Every row trains the book. ``score_leagues`` and ``min_games`` narrow only what is
    *measured*: restricting the metric to top-tier leagues between established teams is
    what makes it reflect the matches a bet would actually be placed on.

    The burn-in exists because the first months are all cold-start defaults, where every
    prediction is 0.5 and the score measures nothing but how many new teams appeared.
    """
    book = RatingBook(config)
    scored: list[tuple[float, int]] = []
    for row in rating_rows(conn, before=end):
        at = int(row["start_time"])
        warm = (
            book.state(int(row["radiant_team_id"])).games >= min_games
            and book.state(int(row["dire_team_id"])).games >= min_games
        )
        in_scope = score_leagues is None or row["league_id"] in score_leagues
        if (start_scoring is None or at >= start_scoring) and in_scope and warm:
            prob = book.win_prob(int(row["radiant_team_id"]), int(row["dire_team_id"]), at)
            scored.append((prob, 1 if row["radiant_win"] else 0))
        apply_row(book, row)
    return ScoreCard.build(scored)


def tune(
    conn: sqlite3.Connection,
    grid: Iterable[Glicko2Config],
    *,
    start_scoring: int | None = None,
    end: int | None = None,
    score_leagues: set[int] | None = None,
    min_games: int = 0,
) -> list[tuple[Glicko2Config, ScoreCard]]:
    """Rank configs by walk-forward log-loss.

    ``end`` must be the target event's start: the grid is *bounded*, not merely filtered,
    so no config can be chosen using a map from the period being predicted.
    """
    results = [
        (
            config,
            walk_forward(
                conn,
                config,
                start_scoring=start_scoring,
                end=end,
                score_leagues=score_leagues,
                min_games=min_games,
            ),
        )
        for config in grid
    ]
    results.sort(key=lambda r: (r[1].log_loss is None, r[1].log_loss))
    return results


def default_grid() -> list[Glicko2Config]:
    """A small deterministic grid; each config is one cheap pass over the history."""
    return [
        Glicko2Config(tau=tau, idle_period_s=idle, initial_rd=rd)
        for tau in (0.3, 0.5, 0.8, 1.2)
        for idle in (14.0 * 86400, 30.0 * 86400, 60.0 * 86400, None)
        for rd in (350.0,)
    ]


# ------------------------------------------------------------------------ event series


@dataclass(frozen=True)
class SeriesInfo:
    series_id: int
    series_type: int | None
    start_time: int
    team_a: int
    team_b: int
    name_a: str
    name_b: str
    n_maps: int


def league_id_for(conn: sqlite3.Connection, name: str = DEFAULT_LEAGUE_NAME) -> int | None:
    """Exact league-name lookup. Never a LIKE: the qualifiers share the event's prefix
    ("The International 2026 - Regional Qualifier Europe") and belong in *training*."""
    row = conn.execute(
        "SELECT league_id FROM matches WHERE league_name = ? LIMIT 1", (name,)
    ).fetchone()
    return int(row[0]) if row else None


def _quoted_names(conn: sqlite3.Connection, series_id: int, source: str) -> dict[int, str]:
    """Team id -> the name the book quotes it under, from the resolved pre-match event.

    ``clv_report`` matches a prediction's selection against the quoted string, so a
    prediction naming "Team Spirit" against a book quoting "Spirit" is silently skipped.
    The join is by team id only: the book's home/away has no relation to radiant/dire.
    """
    row = conn.execute(
        "SELECT e.event_id, e.home_team_id, e.away_team_id, s.home, s.away "
        "FROM event_series_map e "
        "JOIN odds_snapshots s ON s.event_id = e.event_id AND s.source = e.source "
        "WHERE e.source = ? AND e.series_id = ? AND e.is_kills = 0 "
        "  AND e.parent_event_id IS NULL "
        "ORDER BY s.captured_at DESC LIMIT 1",
        (source, series_id),
    ).fetchone()
    if row is None:
        return {}
    names: dict[int, str] = {}
    if row["home_team_id"] is not None and row["home"]:
        names[int(row["home_team_id"])] = str(row["home"])
    if row["away_team_id"] is not None and row["away"]:
        names[int(row["away_team_id"])] = str(row["away"])
    return names


def event_series(
    conn: sqlite3.Connection, league_id: int, source: str = "pinnacle"
) -> list[SeriesInfo]:
    """Every real series of a league, oldest first, named as the book names them."""
    rows = conn.execute(
        "SELECT series_id, MIN(start_time) AS t0, COUNT(*) AS n_maps "
        "FROM matches WHERE league_id = ? AND series_id IS NOT NULL AND series_id > 0 "
        "  AND start_time IS NOT NULL "
        "GROUP BY series_id ORDER BY t0, series_id",
        (league_id,),
    ).fetchall()
    out: list[SeriesInfo] = []
    for row in rows:
        first = conn.execute(
            "SELECT series_type, radiant_team_id, dire_team_id, radiant_name, dire_name "
            "FROM matches WHERE series_id = ? AND start_time IS NOT NULL "
            "ORDER BY start_time LIMIT 1",
            (row["series_id"],),
        ).fetchone()
        if first is None or first["radiant_team_id"] is None or first["dire_team_id"] is None:
            continue
        team_a, team_b = int(first["radiant_team_id"]), int(first["dire_team_id"])
        quoted = _quoted_names(conn, int(row["series_id"]), source)
        out.append(
            SeriesInfo(
                series_id=int(row["series_id"]),
                series_type=first["series_type"],
                start_time=int(row["t0"]),
                team_a=team_a,
                team_b=team_b,
                # Fall back to OpenDota's name when the series has no resolved event: the
                # prediction then lands in eval's "no closing line" bucket, which is the
                # truth, rather than being scored against a mismatched quote.
                name_a=quoted.get(team_a) or str(first["radiant_name"] or team_a),
                name_b=quoted.get(team_b) or str(first["dire_name"] or team_b),
                n_maps=int(row["n_maps"]),
            )
        )
    return out


def _frozen_books(
    conn: sqlite3.Connection, series: Sequence[SeriesInfo], config: Glicko2Config | None
) -> Iterator[tuple[SeriesInfo, float]]:
    """Yield ``(series, p_map)`` with ratings frozen at each series' first horn.

    One cumulative pass, not one replay per series: the book only ever moves forward and
    the series are already time-ordered, so re-walking 62k rows per series would buy
    nothing but runtime.
    """
    book = RatingBook(config)
    rows = rating_rows(conn)
    pending = next(rows, None)
    for info in series:
        while pending is not None and int(pending["start_time"]) < info.start_time:
            apply_row(book, pending)
            pending = next(rows, None)
        yield info, book.win_prob(info.team_a, info.team_b, info.start_time)


def event_predictions(
    conn: sqlite3.Connection,
    league_id: int,
    config: Glicko2Config | None = None,
    source: str = "pinnacle",
) -> list[Prediction]:
    """Pre-match predictions for a league's series, ready for ``dota2bets eval``.

    Both sides of every market are emitted so the probabilities sum to one and eval can
    score whichever side the book quoted. Period 0 is the series price; period 1 is
    map 1. Periods 2 and 3 get the same per-map probability -- a ratings-only model has
    nothing map-specific to say -- and are reported with the caveat that a map 3 only
    exists at 1-1, a state the model does not condition on. Read p0 and p1 as the
    headline.
    """
    preds: list[Prediction] = []
    for info, p_map in _frozen_books(conn, event_series(conn, league_id, source), config):
        p_series = series_win_prob(p_map, info.series_type)
        if p_series is not None:
            preds.append(Prediction(info.series_id, 0, info.name_a, p_series))
            preds.append(Prediction(info.series_id, 0, info.name_b, 1.0 - p_series))
        for period in (1, 2, 3):
            if period > info.n_maps:
                break
            preds.append(Prediction(info.series_id, period, info.name_a, p_map))
            preds.append(Prediction(info.series_id, period, info.name_b, 1.0 - p_map))
    return preds


def write_predictions(path: str, predictions: Sequence[Prediction]) -> int:
    """Write predictions as JSONL for ``dota2bets eval``.

    One object per line and only ``Prediction`` fields: ``load_predictions`` rejects
    unknown keys, and ``storage.dump_json`` indents, which is not JSON *Lines*.
    """
    with open(path, "w", encoding="utf-8") as fh:
        for p in predictions:
            fh.write(
                json.dumps(
                    {
                        "series_id": p.series_id,
                        "period": p.period,
                        "selection": p.selection,
                        "prob": round(p.prob, 6),
                    }
                )
                + "\n"
            )
    return len(predictions)


@dataclass
class MatchReport:
    """Outcome-only scoring over every decided map of an event."""

    overall: ScoreCard = field(default_factory=ScoreCard)
    by_day: list[tuple[str, ScoreCard]] = field(default_factory=list)


def match_report(
    conn: sqlite3.Connection,
    league_id: int,
    config: Glicko2Config | None = None,
    source: str = "pinnacle",
) -> MatchReport:
    """Brier/log-loss over all of a league's decided maps, including odds-less days.

    Predictions are frozen at each *series* horn, not each map's, so a map-3 prediction
    does not quietly learn from maps 1 and 2 of the same series -- that is a bet placed
    before the series began.
    """
    series = event_series(conn, league_id, source)
    scored: list[tuple[float, int]] = []
    per_day: dict[str, list[tuple[float, int]]] = {}
    for info, p_map in _frozen_books(conn, series, config):
        maps = conn.execute(
            "SELECT start_time, radiant_team_id, dire_team_id, radiant_win FROM matches "
            "WHERE series_id = ? AND radiant_win IS NOT NULL AND start_time IS NOT NULL "
            "ORDER BY start_time",
            (info.series_id,),
        ).fetchall()
        for row in maps:
            if row["radiant_team_id"] == info.team_a:
                prob, outcome = p_map, 1 if row["radiant_win"] else 0
            elif row["dire_team_id"] == info.team_a:
                prob, outcome = p_map, 0 if row["radiant_win"] else 1
            else:
                # A series grouping that swaps in a third team is corrupt upstream; skip
                # rather than score a prediction about teams that were not playing.
                log.warning("series %s map %s: neither team matches", info.series_id, row[0])
                continue
            scored.append((prob, outcome))
            day = _day(int(row["start_time"]))
            per_day.setdefault(day, []).append((prob, outcome))
    return MatchReport(
        overall=ScoreCard.build(scored),
        by_day=[(day, ScoreCard.build(rows)) for day, rows in sorted(per_day.items())],
    )


def _day(epoch: int) -> str:
    return time.strftime("%Y-%m-%d", time.localtime(epoch))
