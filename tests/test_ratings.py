from __future__ import annotations

import math

import pytest

from dota2bets import storage
from dota2bets.ratings import (
    GLICKO_SCALE,
    Glicko2Config,
    RatingBook,
    TeamState,
    _rate,
    rating_rows,
    replay,
    series_win_prob,
)

SPIRIT = 7119388
AURORA = 111
FALCONS = 222

HOUR = 3600
DAY = 86400


def _match(match_id: int, start_time: int, radiant_win: int, radiant=SPIRIT, dire=AURORA, **over):
    row = {
        "match_id": match_id,
        "league_id": 19719,
        "league_name": "The International 2026",
        "series_id": match_id // 10,
        "series_type": 1,
        "start_time": start_time,
        "radiant_team_id": radiant,
        "dire_team_id": dire,
        "radiant_win": radiant_win,
    }
    row.update(over)
    return row


def _internal(rating: float, rd: float, vol: float = 0.06) -> TeamState:
    return TeamState(mu=(rating - 1500.0) / GLICKO_SCALE, phi=rd / GLICKO_SCALE, sigma=vol)


# ------------------------------------------------------------------- the arithmetic


def test_glickman_worked_example():
    """The published worked example: 1500/RD200 beats 1400, loses to 1550 and 1700.

    Pinning this is what makes the volatility root-find trustworthy; it is the part of
    Glicko-2 that is easy to get subtly wrong and impossible to spot from the outputs.
    """
    player = _internal(1500.0, 200.0)
    games = [
        (_internal(1400.0, 30.0), 1.0),
        (_internal(1550.0, 100.0), 0.0),
        (_internal(1700.0, 300.0), 0.0),
    ]
    result = _rate(player, [(o.mu, o.phi, s) for o, s in games], tau=0.5)
    assert result.rating == pytest.approx(1464.06, abs=0.05)
    assert result.rd == pytest.approx(151.52, abs=0.05)
    assert result.sigma == pytest.approx(0.05999, abs=1e-5)


def test_win_prob_is_symmetric_and_ordered():
    book = RatingBook()
    book.states[SPIRIT] = _internal(1700.0, 50.0)
    book.states[AURORA] = _internal(1400.0, 50.0)
    p = book.win_prob(SPIRIT, AURORA, at=0)
    assert p > 0.5
    assert p + book.win_prob(AURORA, SPIRIT, at=0) == pytest.approx(1.0)


def test_uncertainty_pulls_toward_a_coin_flip():
    """A rating gap between two barely-known teams must not be believed."""
    known = RatingBook()
    known.states[SPIRIT] = _internal(1700.0, 30.0)
    known.states[AURORA] = _internal(1400.0, 30.0)
    unknown = RatingBook()
    unknown.states[SPIRIT] = _internal(1700.0, 350.0)
    unknown.states[AURORA] = _internal(1400.0, 350.0)
    assert unknown.win_prob(SPIRIT, AURORA, 0) < known.win_prob(SPIRIT, AURORA, 0)


def test_equal_ratings_give_half():
    book = RatingBook()
    assert book.win_prob(SPIRIT, AURORA, at=0) == pytest.approx(0.5)


# ------------------------------------------------------------------- idle inflation


def test_idle_time_inflates_rd_but_never_past_the_cap():
    book = RatingBook(Glicko2Config(idle_period_s=30 * DAY, max_rd=350.0))
    book.update_map(0, SPIRIT, AURORA)
    fresh = book.state(SPIRIT).rd
    assert book._inflated(SPIRIT, 90 * DAY).rd > fresh
    assert book._inflated(SPIRIT, 900 * DAY).rd > book._inflated(SPIRIT, 90 * DAY).rd
    assert book._inflated(SPIRIT, 10_000_000 * DAY).rd == pytest.approx(350.0)


def test_predicting_does_not_mutate_stored_state():
    """A prediction that aged the book would leak into the next prediction."""
    book = RatingBook()
    book.update_map(0, SPIRIT, AURORA)
    before = book.state(SPIRIT)
    book.win_prob(SPIRIT, AURORA, at=500 * DAY)
    assert book.state(SPIRIT) == before


def test_disabling_inflation_freezes_rd():
    book = RatingBook(Glicko2Config(idle_period_s=None))
    book.update_map(0, SPIRIT, AURORA)
    assert book._inflated(SPIRIT, 900 * DAY).rd == pytest.approx(book.state(SPIRIT).rd)


def test_winning_raises_the_winner_and_lowers_the_loser():
    book = RatingBook()
    book.update_map(0, SPIRIT, AURORA)
    assert book.state(SPIRIT).rating > 1500.0 > book.state(AURORA).rating
    # Beating someone is also informative about *how well known* you are.
    assert book.state(SPIRIT).rd < 350.0


# ---------------------------------------------------------------------- series maths


@pytest.mark.parametrize(
    ("series_type", "expected"),
    [(0, 0.6), (1, 0.648), (2, 0.68256)],
)
def test_series_win_prob_pins(series_type, expected):
    assert series_win_prob(0.6, series_type) == pytest.approx(expected)


def test_best_of_two_has_no_winner():
    """A Bo2 can end 1-1. Defaulting it to first-to-1 would score a bet that never settled."""
    assert series_win_prob(0.6, 3) is None


def test_a_coin_flip_stays_a_coin_flip_in_any_format():
    for series_type in (0, 1, 2):
        assert series_win_prob(0.5, series_type) == pytest.approx(0.5)


def test_longer_series_favour_the_favourite():
    assert series_win_prob(0.6, 2) > series_win_prob(0.6, 1) > series_win_prob(0.6, 0)


# ------------------------------------------------------------------------ the replay


def test_rating_rows_are_chronological_and_skip_unusable_matches(conn):
    storage.upsert_matches(
        conn,
        [
            _match(3, 3 * HOUR, 1),
            _match(1, 1 * HOUR, 0),
            _match(2, 2 * HOUR, None),  # undecided
            _match(4, 4 * HOUR, 1, radiant=None),  # unregistered stack
        ],
    )
    assert [r["match_id"] for r in rating_rows(conn)] == [1, 3]


def test_replay_bound_is_strict(conn):
    """A map starting exactly at the cut is the one being predicted -- it must be unseen."""
    storage.upsert_matches(conn, [_match(1, 1 * HOUR, 1), _match(2, 2 * HOUR, 1)])
    at_bound = replay(conn, before=2 * HOUR)
    only_first = replay(conn, before=2 * HOUR - 1)
    assert at_bound.state(SPIRIT) == only_first.state(SPIRIT)
    assert at_bound.state(SPIRIT).games == 1


def test_replay_reflects_a_consistent_winner(conn):
    storage.upsert_matches(conn, [_match(i, i * HOUR, 1) for i in range(1, 11)])
    book = replay(conn)
    assert book.state(SPIRIT).rating > 1600.0
    assert book.win_prob(SPIRIT, AURORA, at=11 * HOUR) > 0.8


def test_replay_resolves_dire_wins(conn):
    storage.upsert_matches(conn, [_match(i, i * HOUR, 0) for i in range(1, 6)])
    book = replay(conn)
    assert book.state(AURORA).rating > book.state(SPIRIT).rating


def test_transitivity_carries_through_a_third_team(conn):
    """Ratings must move strength between teams that never met -- that is the point."""
    rows = [_match(i, i * HOUR, 1, radiant=SPIRIT, dire=AURORA) for i in range(1, 9)]
    rows += [_match(20 + i, (20 + i) * HOUR, 1, radiant=AURORA, dire=FALCONS) for i in range(1, 9)]
    storage.upsert_matches(conn, rows)
    book = replay(conn)
    assert book.win_prob(SPIRIT, FALCONS, at=100 * HOUR) > 0.5


def test_ratings_stay_finite_over_a_long_streak(conn):
    storage.upsert_matches(conn, [_match(i, i * DAY, 1) for i in range(1, 200)])
    book = replay(conn)
    assert math.isfinite(book.state(SPIRIT).rating)
    assert 0.0 < book.win_prob(SPIRIT, AURORA, at=300 * DAY) < 1.0
