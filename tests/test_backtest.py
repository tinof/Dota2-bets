from __future__ import annotations

import pytest

from dota2bets import storage
from dota2bets.aliases import AliasResolver
from dota2bets.backtest import (
    ScoreCard,
    default_draft_grid,
    default_grid,
    elite_league_ids,
    event_predictions,
    event_predictions_draft,
    event_series,
    league_id_for,
    match_report,
    tune,
    tune_draft,
    walk_forward,
    walk_forward_draft,
    write_predictions,
)
from dota2bets.draft import DraftConfig
from dota2bets.evaluation import clv_report, load_predictions
from dota2bets.ratings import Glicko2Config

SPIRIT = 7119388
AURORA = 111
LEAGUE = 19719
LEAGUE_NAME = "The International 2026"
QUALIFIER = 19892

HOUR = 3600
DAY = 86400
MAP1 = 1_000_000
MAP2 = 1_004_000

ALIAS_YAML = """
teams:
  - team_id: 7119388
    name: Team Spirit
    aliases: [Spirit]
  - team_id: 111
    name: Aurora Gaming
    aliases: [Aurora]
"""


@pytest.fixture
def resolver():
    return AliasResolver.from_yaml(ALIAS_YAML)


def _match(match_id, start_time, radiant_win, series_id, **over):
    row = {
        "match_id": match_id,
        "league_id": LEAGUE,
        "league_name": LEAGUE_NAME,
        "series_id": series_id,
        "series_type": 1,
        "start_time": start_time,
        "radiant_team_id": SPIRIT,
        "radiant_name": "Team Spirit",
        "dire_team_id": AURORA,
        "dire_name": "Aurora Gaming",
        "radiant_win": radiant_win,
    }
    row.update(over)
    return row


@pytest.fixture
def ti_db(conn, quote):
    """One TI series (Spirit 2-0 Aurora), priced under a resolved pre-match event.

    Mirrors ``series_db`` in test_evaluation.py so a prediction produced here can be fed
    straight through the real ``clv_report``.
    """
    storage.upsert_matches(conn, [_match(1, MAP1, 1, 555), _match(2, MAP2, 1, 555)])
    conn.execute(
        "INSERT INTO event_series_map (source, event_id, units, is_kills, parent_event_id, "
        "home_team_id, away_team_id, series_id, start_skew_s, resolved_at) "
        "VALUES ('pinnacle', '1', 'Regular', 0, NULL, ?, ?, 555, 0, 1)",
        (SPIRIT, AURORA),
    )

    def line(selection, period, price, captured_at):
        q = quote(
            event_id="1",
            line_key=f"1|Regular|moneyline|{period}|{selection}|",
            selection=selection,
            period=period,
            price_american=price,
        )
        q["price_decimal"] = storage.american_to_decimal(price)
        q["captured_at"] = captured_at
        cols = list(q)
        conn.execute(
            f"INSERT INTO odds_snapshots ({','.join(cols)}) "  # noqa: S608
            f"VALUES ({','.join('?' * len(cols))})",
            tuple(q[c] for c in cols),
        )

    for period, horn in ((0, MAP1), (1, MAP1), (2, MAP2)):
        line("Spirit", period, -150, horn - 100)
        line("Aurora", period, 130, horn - 100)
    conn.commit()
    return conn


# ------------------------------------------------------------------------ score cards


def test_scorecard_is_empty_without_rows():
    card = ScoreCard.build([])
    assert card.n == 0
    assert card.brier is None


def test_scorecard_pins():
    card = ScoreCard.build([(0.8, 1), (0.4, 0)])
    assert card.brier == pytest.approx((0.04 + 0.16) / 2)
    assert card.log_loss == pytest.approx(0.36698, abs=1e-4)


def test_a_confident_miss_costs_a_finite_amount():
    """Clamping is what stops one wrong certainty from making the metric infinite."""
    card = ScoreCard.build([(1.0, 0)])
    assert card.log_loss > 10
    assert card.log_loss < float("inf")


# --------------------------------------------------------------------- walk forward


def test_walk_forward_beats_the_coin_flip_on_a_one_sided_history(conn):
    storage.upsert_matches(conn, [_match(i, i * DAY, 1, i) for i in range(1, 60)])
    card = walk_forward(conn)
    assert card.n == 59
    assert card.brier < card.baseline_brier


def test_walk_forward_scores_only_after_the_burn_in(conn):
    storage.upsert_matches(conn, [_match(i, i * DAY, 1, i) for i in range(1, 21)])
    assert walk_forward(conn, start_scoring=10 * DAY).n == 11


def test_walk_forward_respects_the_holdout_bound(conn):
    """The bound must exclude the holdout entirely -- tuning cannot see what it predicts."""
    storage.upsert_matches(conn, [_match(i, i * DAY, 1, i) for i in range(1, 21)])
    assert walk_forward(conn, end=10 * DAY).n == 9


def test_walk_forward_never_learns_from_the_map_it_is_scoring(conn):
    """One match: the only honest prediction is the prior, 0.5."""
    storage.upsert_matches(conn, [_match(1, DAY, 1, 1)])
    assert walk_forward(conn).brier == pytest.approx(0.25)


def test_tune_ranks_by_log_loss(conn):
    storage.upsert_matches(conn, [_match(i, i * DAY, 1, i) for i in range(1, 40)])
    grid = [Glicko2Config(tau=0.3), Glicko2Config(tau=1.2)]
    results = tune(conn, grid)
    assert len(results) == 2
    assert results[0][1].log_loss <= results[1][1].log_loss


def test_default_grid_is_small_and_distinct():
    grid = default_grid()
    assert len(grid) == 16
    assert len(set(grid)) == 16
    for cfg in grid:
        assert cfg.tau == 0.5
        assert cfg.idle_period_s == 30.0 * 86400.0
        assert cfg.initial_rd == 350.0


def test_walk_forward_degrades_identically_without_data(conn):
    storage.upsert_matches(conn, [_match(i, i * DAY, 1, i) for i in range(1, 30)])
    card_none = walk_forward(conn, Glicko2Config())
    card_boosts = walk_forward(conn, Glicko2Config(patch_rd_boost=60.0, roster_rd_boost=60.0))
    assert card_boosts.n == card_none.n
    assert card_boosts.brier == pytest.approx(card_none.brier)
    assert card_boosts.log_loss == pytest.approx(card_none.log_loss)


def test_frozen_series_price_unaffected_by_map3_lineup_change(conn):
    storage.upsert_matches(
        conn,
        [
            _match(1, MAP1, 1, 555),
            _match(2, MAP2, 0, 555),
            _match(3, MAP2 + HOUR, 1, 555),
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
        for i in range(5)
    ] + [
        {"match_id": 2, "player_slot": 128 + i, "account_id": 200 + i, "is_radiant": 0}
        for i in range(5)
    ] + [
        {"match_id": 3, "player_slot": i, "account_id": 900 + i, "is_radiant": 1}
        for i in range(5)
    ] + [
        {"match_id": 3, "player_slot": 128 + i, "account_id": 200 + i, "is_radiant": 0}
        for i in range(5)
    ]
    storage.upsert_match_players(conn, players)

    config = Glicko2Config(roster_rd_boost=60.0)
    preds = event_predictions(conn, LEAGUE, config=config)
    p1 = [p.prob for p in preds if p.selection == "Team Spirit" and p.period == 1][0]
    p3 = [p.prob for p in preds if p.selection == "Team Spirit" and p.period == 3][0]
    assert p1 == pytest.approx(p3)


def test_elite_league_ids_matches_on_name(conn):
    storage.upsert_matches(
        conn,
        [
            _match(1, MAP1, 1, 1),
            _match(2, MAP2, 1, 2, league_id=99, league_name="Destiny League"),
        ],
    )
    assert elite_league_ids(conn) == {LEAGUE}


def test_scoring_filter_narrows_the_metric_but_not_the_training(conn):
    """Tier-3 maps still teach the ratings; they just are not what the model is judged on."""
    storage.upsert_matches(
        conn,
        [_match(i, i * DAY, 1, i, league_id=99, league_name="Destiny League") for i in range(1, 30)]
        + [_match(50, 50 * DAY, 1, 50)],
    )
    card = walk_forward(conn, score_leagues={LEAGUE})
    assert card.n == 1
    # The single scored map was predicted from the 29 that trained the book.
    assert card.brier < 0.25


def test_min_games_excludes_cold_start_teams(conn):
    storage.upsert_matches(conn, [_match(i, i * DAY, 1, i) for i in range(1, 21)])
    assert walk_forward(conn, min_games=5).n == 15


# -------------------------------------------------------------------- event handling


def test_league_lookup_is_exact_not_a_prefix(conn):
    """The qualifiers share the event's name prefix and belong in training, not holdout."""
    storage.upsert_matches(
        conn,
        [
            _match(1, MAP1, 1, 555),
            _match(2, MAP2, 1, 556, league_id=QUALIFIER, league_name=f"{LEAGUE_NAME} - Qualifier"),
        ],
    )
    assert league_id_for(conn, LEAGUE_NAME) == LEAGUE


def test_event_series_uses_the_name_the_book_quotes(ti_db):
    """A prediction naming "Team Spirit" against a book quoting "Spirit" scores nothing."""
    series = event_series(ti_db, LEAGUE)
    assert [s.series_id for s in series] == [555]
    assert {series[0].name_a, series[0].name_b} == {"Spirit", "Aurora"}


def test_event_series_falls_back_to_opendota_names_when_unresolved(conn):
    storage.upsert_matches(conn, [_match(1, MAP1, 1, 555)])
    series = event_series(conn, LEAGUE)
    assert {series[0].name_a, series[0].name_b} == {"Team Spirit", "Aurora Gaming"}


def test_event_series_skips_placeholder_series_ids(conn):
    storage.upsert_matches(conn, [_match(1, MAP1, 1, 0), _match(2, MAP2, 1, 555)])
    assert [s.series_id for s in event_series(conn, LEAGUE)] == [555]


# ---------------------------------------------------------------------- predictions


def test_predictions_are_two_sided_and_sum_to_one(ti_db):
    preds = event_predictions(ti_db, LEAGUE)
    by_period: dict[int, list[float]] = {}
    for p in preds:
        by_period.setdefault(p.period, []).append(p.prob)
    assert set(by_period) == {0, 1, 2}
    for probs in by_period.values():
        assert sum(probs) == pytest.approx(1.0)


def test_no_period_zero_for_a_format_with_no_winner(conn):
    storage.upsert_matches(conn, [_match(1, MAP1, 1, 555, series_type=3)])
    assert all(p.period != 0 for p in event_predictions(conn, LEAGUE))


def test_series_price_is_not_the_map_price(conn):
    """A Bo3 favourite is more likely to take the series than any single map."""
    storage.upsert_matches(
        conn,
        [_match(i, i * DAY, 1, i, league_id=QUALIFIER, league_name="Q") for i in range(1, 30)]
        + [_match(100, 100 * DAY, None, 555)],
    )
    preds = {(p.period, p.selection): p.prob for p in event_predictions(conn, LEAGUE)}
    assert preds[(0, "Team Spirit")] > preds[(1, "Team Spirit")] > 0.5


def test_predictions_round_trip_through_eval(ti_db, resolver, tmp_path):
    """The real bar: the file this writes must actually score in ``clv_report``."""
    preds = event_predictions(ti_db, LEAGUE)
    path = tmp_path / "preds.jsonl"
    write_predictions(str(path), preds)
    loaded = load_predictions(str(path))
    report = clv_report(ti_db, loaded, resolver=resolver)
    assert report.n_scored > 0
    assert not [r for _, r in report.skipped if r == "selection not quoted"]
    assert report.closing_brier is not None


# -------------------------------------------------------------------- match report


def test_match_report_scores_every_decided_map(ti_db):
    report = match_report(ti_db, LEAGUE)
    assert report.overall.n == 2
    assert report.by_day


def test_match_report_covers_maps_with_no_odds_at_all(conn):
    """The recorder came up mid-event; those days are still outcomes worth scoring."""
    storage.upsert_matches(conn, [_match(1, MAP1, 1, 555), _match(2, MAP2, 0, 556)])
    assert match_report(conn, LEAGUE).overall.n == 2


def test_match_report_freezes_before_the_series(conn):
    """Every map of a series shares one pre-series probability -- no in-series learning."""
    storage.upsert_matches(conn, [_match(1, MAP1, 1, 555), _match(2, MAP2, 0, 555)])
    report = match_report(conn, LEAGUE)
    assert report.overall.n == 2
    # Spirit won one and lost one from an identical 0.5 prior.
    assert report.overall.brier == pytest.approx(0.25)


# ------------------------------------------------------------------ draft backtest


def test_disabled_draft_walk_forward_reproduces_ratings_baseline(conn):
    """Disabled-draft walk-forward produces exact same ScoreCard as ratings walk-forward."""
    storage.upsert_matches(
        conn,
        [
            _match(1, 1 * DAY, 1, 10),
            _match(2, 2 * DAY, 0, 11),
            _match(3, 3 * DAY, 1, 12),
        ],
    )
    ratings_card = walk_forward(conn)
    draft_card = walk_forward_draft(conn, draft_config=DraftConfig(disabled=True))
    assert ratings_card.n == draft_card.n
    assert ratings_card.brier == pytest.approx(draft_card.brier)
    assert ratings_card.log_loss == pytest.approx(draft_card.log_loss)


def test_walk_forward_draft_bound_enforced(conn):
    """Rows after the bound cannot change a scorecard."""
    storage.upsert_matches(
        conn,
        [
            _match(1, 100, 1, 1),
            _match(2, 200, 0, 2),
            _match(3, 300, 1, 3),
        ],
    )
    card_bounded = walk_forward_draft(conn, end=250)
    assert card_bounded.n == 2


def test_event_predictions_draft_two_sided_sum_to_one_and_period_indices(ti_db):
    """Per-map predictions emit both sides summing to one with correct period indices."""
    result = event_predictions_draft(ti_db, LEAGUE)
    preds = result.predictions
    assert len(preds) > 0
    # The population is reported alongside the predictions, never left implicit.
    assert result.n_maps == len(preds) // 2
    assert 0 <= result.n_fallback <= result.n_maps
    by_period: dict[int, list[float]] = {}
    for p in preds:
        by_period.setdefault(p.period, []).append(p.prob)
    # Draft predictions are per-map (periods 1, 2)
    assert set(by_period.keys()) == {1, 2}
    for _period, probs in by_period.items():
        assert len(probs) == 2
        assert sum(probs) == pytest.approx(1.0)


def test_tune_draft_ranks_configs(conn):
    """tune_draft searches grid and ranks by log-loss."""
    storage.upsert_matches(
        conn,
        [
            _match(1, 100, 1, 1),
            _match(2, 200, 0, 2),
        ],
    )
    grid = default_draft_grid()[:3]
    results = tune_draft(conn, grid)
    assert len(results) == 3
    assert all(isinstance(res[1], ScoreCard) for res in results)
    # Ranked ascending by log-loss
    losses = [r[1].log_loss for r in results if r[1].log_loss is not None]
    assert losses == sorted(losses)
