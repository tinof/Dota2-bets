"""Glicko-2 team ratings from match summaries, and the map/series probabilities they imply.

Phase 1 needs a number to put in front of the closing line. The constraint that picks the
model is not accuracy, it is *leak-freeness*: every prediction has to be made from the
state of the world strictly before the map's horn, for tens of thousands of historical
maps as well as for the handful being bet. Glicko-2 is online by construction, so one
chronological pass yields a pre-match prediction for every match in O(n). A
Bradley-Terry fit with time decay would have to be re-solved at every prediction point,
which is the same model badly implemented.

Two properties matter more here than the rating arithmetic:

**RD inflation is the time decay.** Rating deviation grows while a team is idle, so a
roster that has not played for three months is correctly treated as unknown rather than
as its old self. This is why no separate half-life parameter appears.

**Patch windows are not available yet.** ``matches.patch`` is NULL on every
summary-only row (only the detail crawl fills it), so research.md's per-patch framing
cannot be built from this table today. Idle-time inflation is the substitute; per-patch
resets and roster stability are Phase 1b, once detail coverage exists.

Ratings are per *map*, never per series: a map is the atomic binary outcome the odds
market also prices, and a best-of-three is three observations rather than one.
"""

from __future__ import annotations

import math
import sqlite3
from collections.abc import Iterator, Sequence
from dataclasses import dataclass, replace

#: Glicko-2 works in a compressed scale; this converts to and from Elo-like points.
GLICKO_SCALE = 173.7178

DEFAULT_RATING = 1500.0

#: Convergence tolerance for the volatility iteration, per Glickman's paper.
EPSILON = 1e-6

#: OpenDota ``series_type`` -> maps needed to win. 3 (best-of-two) is deliberately
#: absent: a Bo2 can end 1-1, so it has no series winner to predict.
SERIES_WINS_NEEDED = {0: 1, 1: 2, 2: 3}


@dataclass(frozen=True)
class Glicko2Config:
    """Tunables. Defaults are Glickman's, pending the walk-forward grid search."""

    initial_rating: float = DEFAULT_RATING
    initial_rd: float = 350.0
    initial_vol: float = 0.06
    #: Constrains how much volatility can move per update; smaller = steadier ratings.
    tau: float = 0.5
    #: Seconds of idleness that count as one rating period of RD inflation.
    #: ``None`` disables inflation entirely (useful as a tuning control).
    idle_period_s: float | None = 30.0 * 86400.0
    #: RD is capped here so a long-absent team never becomes *less* known than a new one.
    max_rd: float = 350.0


@dataclass
class TeamState:
    """One team's rating in Glicko-2 internal units (mu, phi) plus its volatility."""

    mu: float
    phi: float
    sigma: float
    last_played: int | None = None
    games: int = 0

    @property
    def rating(self) -> float:
        return self.mu * GLICKO_SCALE + DEFAULT_RATING

    @property
    def rd(self) -> float:
        return self.phi * GLICKO_SCALE


def _g(phi: float) -> float:
    return 1.0 / math.sqrt(1.0 + 3.0 * phi * phi / (math.pi * math.pi))


def _expected(mu: float, opp_mu: float, opp_phi: float) -> float:
    return 1.0 / (1.0 + math.exp(-_g(opp_phi) * (mu - opp_mu)))


def _new_volatility(phi: float, sigma: float, v: float, delta: float, tau: float) -> float:
    """Glickman's illinois-variant root find for the updated volatility."""
    a = math.log(sigma * sigma)
    phi_sq = phi * phi
    delta_sq = delta * delta

    def f(x: float) -> float:
        ex = math.exp(x)
        denom = phi_sq + v + ex
        return ex * (delta_sq - denom) / (2.0 * denom * denom) - (x - a) / (tau * tau)

    lo = a
    if delta_sq > phi_sq + v:
        hi = math.log(delta_sq - phi_sq - v)
    else:
        k = 1
        while f(a - k * tau) < 0:
            k += 1
        hi = a - k * tau
        lo, hi = hi, a
    f_lo, f_hi = f(lo), f(hi)
    while abs(hi - lo) > EPSILON:
        mid = lo + (lo - hi) * f_lo / (f_hi - f_lo)
        f_mid = f(mid)
        if f_mid * f_hi <= 0:
            lo, f_lo = hi, f_hi
        else:
            f_lo /= 2.0
        hi, f_hi = mid, f_mid
    return math.exp(hi / 2.0)


def _rate(state: TeamState, games: Sequence[tuple[float, float, float]], tau: float) -> TeamState:
    """One Glicko-2 rating period for ``state`` against ``(opp_mu, opp_phi, score)`` games.

    Kept separate from :meth:`RatingBook.update_map` so the single-game online path and
    Glickman's multi-game worked example run through identical arithmetic.
    """
    if not games:
        # No result to learn from: only uncertainty grows, handled by the caller.
        return state
    v_inv = 0.0
    delta_sum = 0.0
    for opp_mu, opp_phi, score in games:
        e = _expected(state.mu, opp_mu, opp_phi)
        g = _g(opp_phi)
        v_inv += g * g * e * (1.0 - e)
        delta_sum += g * (score - e)
    v = 1.0 / v_inv
    delta = v * delta_sum
    sigma = _new_volatility(state.phi, state.sigma, v, delta, tau)
    phi_star = math.sqrt(state.phi * state.phi + sigma * sigma)
    phi = 1.0 / math.sqrt(1.0 / (phi_star * phi_star) + 1.0 / v)
    mu = state.mu + phi * phi * delta_sum
    return replace(state, mu=mu, phi=phi, sigma=sigma, games=state.games + len(games))


class RatingBook:
    """Mutable team ratings, advanced strictly in chronological order.

    ``win_prob`` never mutates stored state -- it inflates *copies* to the query time --
    so predicting a match cannot change the ratings the next prediction sees.
    """

    def __init__(self, config: Glicko2Config | None = None) -> None:
        self.config = config or Glicko2Config()
        self.states: dict[int, TeamState] = {}
        self.last_update: int | None = None

    def _default(self) -> TeamState:
        c = self.config
        return TeamState(
            mu=(c.initial_rating - DEFAULT_RATING) / GLICKO_SCALE,
            phi=c.initial_rd / GLICKO_SCALE,
            sigma=c.initial_vol,
        )

    def state(self, team_id: int) -> TeamState:
        """The stored state for a team, creating an unrated default on first sight."""
        if team_id not in self.states:
            self.states[team_id] = self._default()
        return self.states[team_id]

    def _inflated(self, team_id: int, at: int) -> TeamState:
        """A copy of the team's state with RD grown for the time it has sat idle."""
        s = self.state(team_id)
        period = self.config.idle_period_s
        if period is None or s.last_played is None or at <= s.last_played:
            return s
        periods = (at - s.last_played) / period
        phi = math.sqrt(s.phi * s.phi + s.sigma * s.sigma * periods)
        return replace(s, phi=min(phi, self.config.max_rd / GLICKO_SCALE))

    def win_prob(self, team_a: int, team_b: int, at: int) -> float:
        """P(team_a wins one map against team_b), as known at time ``at``.

        Both deviations enter the comparison, so a match between two poorly-known teams
        is pulled toward 0.5 rather than trusting a rating built on three games.
        """
        a = self._inflated(team_a, at)
        b = self._inflated(team_b, at)
        phi = math.sqrt(a.phi * a.phi + b.phi * b.phi)
        return 1.0 / (1.0 + math.exp(-_g(phi) * (a.mu - b.mu)))

    def update_map(self, at: int, winner_id: int, loser_id: int) -> None:
        """Fold one decided map into both teams' ratings."""
        w = self._inflated(winner_id, at)
        loser = self._inflated(loser_id, at)
        new_w = _rate(w, [(loser.mu, loser.phi, 1.0)], self.config.tau)
        new_l = _rate(loser, [(w.mu, w.phi, 0.0)], self.config.tau)
        self.states[winner_id] = replace(new_w, last_played=at)
        self.states[loser_id] = replace(new_l, last_played=at)
        self.last_update = at


def series_win_prob(p_map: float, series_type: int | None) -> float | None:
    """Series win probability from a per-map probability, assuming independent maps.

    Returns ``None`` for a best-of-two, which can end 1-1 and therefore has no winner
    to price. Map independence is an approximation -- momentum and in-series adaptation
    are real -- but it is the honest one for a model that only knows team strength.
    """
    needed = SERIES_WINS_NEEDED.get(series_type if series_type is not None else 0)
    if needed is None:
        return None
    if needed == 1:
        return p_map
    # P(first to `needed` wins) over a best-of-(2*needed-1).
    total = 2 * needed - 1
    q = 1.0 - p_map
    return sum(math.comb(total, k) * p_map**k * q ** (total - k) for k in range(needed, total + 1))


def rating_rows(
    conn: sqlite3.Connection, before: int | None = None, after: int | None = None
) -> Iterator[sqlite3.Row]:
    """Decided, team-identified maps in chronological order.

    The ``before`` bound is strict, matching ``evaluation.map_start``'s horn semantics:
    a map starting exactly at the cut is *not* visible to a prediction made for it.
    Rows missing a team id (about 3k of 65k, mostly unregistered stacks) carry no
    rating information and are skipped rather than pooled into a fake team.
    """
    yield from conn.execute(
        "SELECT match_id, start_time, radiant_team_id, dire_team_id, radiant_win, "
        "       league_id, series_id, series_type "
        "FROM matches "
        "WHERE radiant_team_id IS NOT NULL AND dire_team_id IS NOT NULL "
        "  AND radiant_win IS NOT NULL AND start_time IS NOT NULL "
        "  AND (? IS NULL OR start_time >= ?) "
        "  AND (? IS NULL OR start_time < ?) "
        "ORDER BY start_time, match_id",
        (after, after, before, before),
    )


def apply_row(book: RatingBook, row: sqlite3.Row) -> None:
    """Fold one ``rating_rows`` row into the book, resolving radiant/dire to win/loss."""
    radiant, dire = int(row["radiant_team_id"]), int(row["dire_team_id"])
    if row["radiant_win"]:
        book.update_map(int(row["start_time"]), radiant, dire)
    else:
        book.update_map(int(row["start_time"]), dire, radiant)


def replay(
    conn: sqlite3.Connection, config: Glicko2Config | None = None, before: int | None = None
) -> RatingBook:
    """Ratings as of ``before`` -- every decided map strictly earlier, in order."""
    book = RatingBook(config)
    for row in rating_rows(conn, before=before):
        apply_row(book, row)
    return book
