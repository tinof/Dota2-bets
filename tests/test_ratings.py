from __future__ import annotations

import math
from dataclasses import replace

import pytest

from dota2bets import storage
from dota2bets.ratings import (
    GLICKO_SCALE,
    Glicko2Config,
    RatingBook,
    TeamState,
    _rate,
    load_lineups,
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


def test_default_config_has_no_boosts():
    cfg = Glicko2Config()
    assert cfg.patch_rd_boost is None
    assert cfg.roster_rd_boost is None


def test_rate_preserves_state_fields():
    state = TeamState(
        mu=0.0,
        phi=1.0,
        sigma=0.06,
        last_played=1234,
        games=5,
        last_patch=59,
        lineup=frozenset({1, 2, 3, 4, 5}),
    )
    rated = _rate(
        state,
        [(_internal(1500.0, 200.0).mu, _internal(1500.0, 200.0).phi, 1.0)],
        tau=0.5,
    )
    assert rated.last_played == 1234
    assert rated.games == 6
    assert rated.last_patch == 59
    assert rated.lineup == frozenset({1, 2, 3, 4, 5})


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


# ----------------------------------------------------------------- patch transitions


def test_patch_transition_prices_closer_to_half_and_subsequent_maps_unwidened():
    config = Glicko2Config(patch_rd_boost=60.0)
    book = RatingBook(config)
    book.states[SPIRIT] = _internal(1700.0, 50.0)
    book.states[SPIRIT].last_patch = 59
    book.states[AURORA] = _internal(1400.0, 50.0)
    book.states[AURORA].last_patch = 59

    # Baseline on same patch
    p_same = book.win_prob(SPIRIT, AURORA, at=0, patch=59)
    assert p_same > 0.5

    # First map on newer patch 60
    p_new = book.win_prob(SPIRIT, AURORA, at=0, patch=60)
    assert 0.5 < p_new < p_same

    # Play map on patch 60
    book.update_map(0, SPIRIT, AURORA, patch=60)
    assert book.state(SPIRIT).last_patch == 60
    assert book.state(AURORA).last_patch == 60

    # Second map on patch 60 gets no further widening
    inf_second = book._inflated(SPIRIT, 0, patch=60)
    inf_unpatched = book._inflated(SPIRIT, 0, patch=None)
    assert inf_second.rd == pytest.approx(inf_unpatched.rd)


def test_first_seen_patch_and_older_patch_do_not_widen():
    config = Glicko2Config(patch_rd_boost=60.0)
    book = RatingBook(config)
    book.states[SPIRIT] = _internal(1700.0, 50.0)
    book.states[AURORA] = _internal(1400.0, 50.0)

    # First seen patch (last_patch is None)
    inf_first = book._inflated(SPIRIT, 0, patch=60)
    assert inf_first.rd == pytest.approx(50.0)

    # Set last_patch to 60
    book.states[SPIRIT].last_patch = 60
    # Older patch (59 < 60)
    inf_older = book._inflated(SPIRIT, 0, patch=59)
    assert inf_older.rd == pytest.approx(50.0)


def test_null_patch_does_not_trigger_or_clear_last_patch():
    config = Glicko2Config(patch_rd_boost=60.0)
    book = RatingBook(config)
    book.states[SPIRIT] = _internal(1700.0, 50.0)
    book.states[AURORA] = _internal(1400.0, 50.0)

    # Set initial patch to 59
    book.update_map(0, SPIRIT, AURORA, patch=59)
    assert book.state(SPIRIT).last_patch == 59

    # 59 -> NULL map: does not clear last_patch
    book.update_map(10, SPIRIT, AURORA, patch=None)
    assert book.state(SPIRIT).last_patch == 59

    # NULL -> 59: same patch, does not fire
    inf_59 = book._inflated(SPIRIT, 20, patch=59)
    inf_none = book._inflated(SPIRIT, 20, patch=None)
    assert inf_59.rd == pytest.approx(inf_none.rd)

    # 59 -> 60: fires boost
    inf_60 = book._inflated(SPIRIT, 20, patch=60)
    assert inf_60.rd > inf_none.rd


def test_stale_older_patch_does_not_rearm_the_transition_boost():
    config = Glicko2Config(patch_rd_boost=60.0)
    book = RatingBook(config)
    book.states[SPIRIT] = _internal(1700.0, 50.0)
    book.states[AURORA] = _internal(1400.0, 50.0)

    book.update_map(0, SPIRIT, AURORA, patch=60)
    # A stale row carrying an older patch must not roll the tracker back to 59...
    book.update_map(10, SPIRIT, AURORA, patch=59)
    assert book.state(SPIRIT).last_patch == 60

    # ...or the next map on 60 would fire the boost a second time.
    inf_60 = book._inflated(SPIRIT, 20, patch=60)
    assert inf_60.rd == pytest.approx(book.state(SPIRIT).rd)


def test_patch_disabled_is_bit_identical_to_baseline(conn):
    rows = [
        _match(1, 1 * HOUR, 1, patch=58),
        _match(2, 2 * HOUR, 1, patch=59),
        _match(3, 3 * HOUR, 0, patch=60),
    ]
    storage.upsert_matches(conn, rows)

    book_default = replay(conn, Glicko2Config())
    book_boost = replay(conn, Glicko2Config(patch_rd_boost=None))

    assert book_default.state(SPIRIT).mu == book_boost.state(SPIRIT).mu
    assert book_default.state(SPIRIT).phi == book_boost.state(SPIRIT).phi
    assert book_default.state(AURORA).mu == book_boost.state(AURORA).mu
    assert book_default.state(AURORA).phi == book_boost.state(AURORA).phi


# ----------------------------------------------------------------- roster stability


def test_roster_widening_is_monotone_in_substitutions_and_zero_for_unchanged():
    config = Glicko2Config(roster_rd_boost=30.0)
    book = RatingBook(config)
    book.states[SPIRIT] = _internal(1700.0, 50.0)
    book.states[SPIRIT].lineup = frozenset({1, 2, 3, 4, 5})
    book.states[AURORA] = _internal(1400.0, 50.0)
    book.states[AURORA].lineup = frozenset({11, 12, 13, 14, 15})

    lineup_0 = {SPIRIT: frozenset({1, 2, 3, 4, 5}), AURORA: frozenset({11, 12, 13, 14, 15})}
    lineup_1 = {SPIRIT: frozenset({1, 2, 3, 4, 99}), AURORA: frozenset({11, 12, 13, 14, 15})}
    lineup_3 = {SPIRIT: frozenset({1, 2, 97, 98, 99}), AURORA: frozenset({11, 12, 13, 14, 15})}

    p0 = book.win_prob(SPIRIT, AURORA, at=0, lineups=lineup_0)
    p1 = book.win_prob(SPIRIT, AURORA, at=0, lineups=lineup_1)
    p3 = book.win_prob(SPIRIT, AURORA, at=0, lineups=lineup_3)

    assert p0 > p1 > p3 > 0.5
    assert book._inflated(SPIRIT, 0, lineup=frozenset({1, 2, 3, 4, 5})).rd == pytest.approx(50.0)


def test_null_lineup_does_not_adjust_or_erase_stored_lineup():
    config = Glicko2Config(roster_rd_boost=30.0)
    book = RatingBook(config)
    book.states[SPIRIT] = _internal(1700.0, 50.0)
    book.states[SPIRIT].lineup = frozenset({1, 2, 3, 4, 5})
    book.states[AURORA] = _internal(1400.0, 50.0)

    # Predict with no lineups
    p_null = book.win_prob(SPIRIT, AURORA, at=0, lineups=None)
    assert p_null > 0.5

    # Update map with no lineups
    book.update_map(0, SPIRIT, AURORA, lineups=None)
    assert book.state(SPIRIT).lineup == frozenset({1, 2, 3, 4, 5})

    # Predict again with original lineup
    p_orig = book.win_prob(
        SPIRIT,
        AURORA,
        at=10,
        lineups={SPIRIT: frozenset({1, 2, 3, 4, 5}), AURORA: frozenset({11, 12, 13, 14, 15})},
    )
    assert p_orig > 0.5
    inf_orig = book._inflated(SPIRIT, 10, lineup=frozenset({1, 2, 3, 4, 5}))
    inf_unadjusted = book._inflated(SPIRIT, 10, lineup=None)
    assert inf_orig.rd == pytest.approx(inf_unadjusted.rd)


def test_substituted_team_win_moves_rating_further():
    config = Glicko2Config(roster_rd_boost=60.0)

    # Book 1: unchanged lineup
    book1 = RatingBook(config)
    book1.states[SPIRIT] = _internal(1500.0, 100.0)
    book1.states[SPIRIT].lineup = frozenset({1, 2, 3, 4, 5})
    book1.states[AURORA] = _internal(1500.0, 100.0)
    book1.states[AURORA].lineup = frozenset({11, 12, 13, 14, 15})

    lineup_clean = {SPIRIT: frozenset({1, 2, 3, 4, 5}), AURORA: frozenset({11, 12, 13, 14, 15})}
    book1.update_map(0, SPIRIT, AURORA, lineups=lineup_clean)

    # Book 2: substituted lineup (3 stand-ins)
    book2 = RatingBook(config)
    book2.states[SPIRIT] = _internal(1500.0, 100.0)
    book2.states[SPIRIT].lineup = frozenset({1, 2, 3, 4, 5})
    book2.states[AURORA] = _internal(1500.0, 100.0)
    book2.states[AURORA].lineup = frozenset({11, 12, 13, 14, 15})

    lineup_subbed = {SPIRIT: frozenset({1, 2, 97, 98, 99}), AURORA: frozenset({11, 12, 13, 14, 15})}
    book2.update_map(0, SPIRIT, AURORA, lineups=lineup_subbed)

    # Substituted team win moves rating further
    assert book2.state(SPIRIT).rating > book1.state(SPIRIT).rating


def test_load_lineups_maps_teams_and_drops_partial_sides(conn):
    # Match 1: 5 radiant (SPIRIT), 5 dire (AURORA)
    # Match 2: 4 radiant (SPIRIT), 5 dire (AURORA)
    # Match 3: 5 radiant (no team_id), 5 dire (FALCONS)
    storage.upsert_matches(
        conn,
        [
            _match(1, 1 * HOUR, 1, radiant=SPIRIT, dire=AURORA),
            _match(2, 2 * HOUR, 1, radiant=SPIRIT, dire=AURORA),
            _match(3, 3 * HOUR, 1, radiant=None, dire=FALCONS),
        ],
    )
    players = [
        {"match_id": 1, "player_slot": i, "account_id": 100 + i, "is_radiant": 1}
        for i in range(5)
    ] + [
        {"match_id": 1, "player_slot": 128 + i, "account_id": 200 + i, "is_radiant": 0}
        for i in range(5)
    ] + [
        {"match_id": 2, "player_slot": i, "account_id": 100 + i, "is_radiant": 1}
        for i in range(4)
    ] + [
        {"match_id": 2, "player_slot": 128 + i, "account_id": 200 + i, "is_radiant": 0}
        for i in range(5)
    ] + [
        {"match_id": 3, "player_slot": i, "account_id": 300 + i, "is_radiant": 1}
        for i in range(5)
    ] + [
        {"match_id": 3, "player_slot": 128 + i, "account_id": 400 + i, "is_radiant": 0}
        for i in range(5)
    ]

    storage.upsert_match_players(conn, players)

    lineups = load_lineups(conn)
    assert 1 in lineups
    assert lineups[1][SPIRIT] == frozenset({100, 101, 102, 103, 104})
    assert lineups[1][AURORA] == frozenset({200, 201, 202, 203, 204})

    assert 2 in lineups
    assert SPIRIT not in lineups[2]
    assert lineups[2][AURORA] == frozenset({200, 201, 202, 203, 204})

    assert 3 in lineups
    assert FALCONS in lineups[3]
    assert len(lineups[3]) == 1


def test_win_prob_leak_freeness_leaves_stored_state_unchanged():
    config = Glicko2Config(patch_rd_boost=60.0, roster_rd_boost=60.0)
    book = RatingBook(config)
    book.states[SPIRIT] = _internal(1700.0, 50.0)
    book.states[SPIRIT].last_patch = 59
    book.states[SPIRIT].lineup = frozenset({1, 2, 3, 4, 5})
    book.states[AURORA] = _internal(1400.0, 50.0)
    book.states[AURORA].last_patch = 59
    book.states[AURORA].lineup = frozenset({11, 12, 13, 14, 15})

    before_s = replace(book.state(SPIRIT))
    before_a = replace(book.state(AURORA))

    p = book.win_prob(
        SPIRIT,
        AURORA,
        at=100 * DAY,
        patch=60,
        lineups={SPIRIT: frozenset({1, 2, 3, 4, 99}), AURORA: frozenset({11, 12, 13, 14, 99})},
    )
    assert 0.0 < p < 1.0
    assert book.state(SPIRIT) == before_s
    assert book.state(AURORA) == before_a
