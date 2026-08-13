"""Pinnacle normalisation tests against a recorded payload (no network)."""

from __future__ import annotations

from dota2bets.odds.pinnacle import normalise
from dota2bets.odds.theoddsapi import _decimal_to_american
from dota2bets.odds.theoddsapi import normalise as normalise_odds_api


def test_normalise_produces_quotes(pinnacle_payload):
    quotes = normalise(pinnacle_payload["matchups"], pinnacle_payload["markets"])
    assert quotes
    assert {q["source"] for q in quotes} == {"pinnacle"}
    assert all("dota" in q["league"].lower() for q in quotes)


def test_moneyline_selections_resolve_to_team_names(pinnacle_payload):
    quotes = normalise(pinnacle_payload["matchups"], pinnacle_payload["markets"])
    ml = [q for q in quotes if q["market_type"] == "moneyline"]
    assert ml, "fixture should contain moneyline markets"
    for q in ml:
        # Never fall back to a bare participant id.
        assert not q["selection"].isdigit()
        assert q["selection"] in (q["home"], q["away"])


def test_totals_keep_designation_and_points(pinnacle_payload):
    quotes = normalise(pinnacle_payload["matchups"], pinnacle_payload["markets"])
    totals = [q for q in quotes if q["market_type"] == "total"]
    if totals:
        assert {q["selection"] for q in totals} <= {"over", "under"}
        assert all(q["points"] is not None for q in totals)


def test_line_keys_are_unique_per_poll(pinnacle_payload):
    quotes = normalise(pinnacle_payload["matchups"], pinnacle_payload["markets"])
    keys = [q["line_key"] for q in quotes]
    assert len(keys) == len(set(keys))


def test_line_key_is_stable_across_price_moves(pinnacle_payload):
    """The key must not embed the price, or change-detection would never match."""
    first = normalise(pinnacle_payload["matchups"], pinnacle_payload["markets"])
    moved = [
        {**m, "prices": [{**p, "price": (p.get("price") or 0) - 10} for p in m["prices"]]}
        for m in pinnacle_payload["markets"]
    ]
    second = normalise(pinnacle_payload["matchups"], moved)
    assert {q["line_key"] for q in first} == {q["line_key"] for q in second}


def test_non_dota_leagues_are_filtered_out(pinnacle_payload):
    quotes = normalise(
        pinnacle_payload["matchups"], pinnacle_payload["markets"], league_filter="counter-strike"
    )
    assert quotes == []


def test_markets_without_a_known_matchup_are_dropped(pinnacle_payload):
    orphan = [{**pinnacle_payload["markets"][0], "matchupId": 999999999}]
    assert normalise(pinnacle_payload["matchups"], orphan) == []


def test_odds_api_normalise_flattens_bookmakers():
    events = [
        {
            "id": "abc",
            "sport_title": "Dota 2",
            "home_team": "Spirit",
            "away_team": "Aurora",
            "commence_time": "2026-08-14T02:00:00Z",
            "bookmakers": [
                {
                    "key": "pinnacle",
                    "markets": [
                        {
                            "key": "h2h",
                            "outcomes": [
                                {"name": "Spirit", "price": 1.67},
                                {"name": "Aurora", "price": 2.30},
                            ],
                        }
                    ],
                }
            ],
        }
    ]
    quotes = normalise_odds_api(events)
    assert len(quotes) == 2
    assert quotes[0]["source"] == "theoddsapi:pinnacle"
    assert quotes[0]["price_decimal"] == 1.67
    assert len({q["line_key"] for q in quotes}) == 2


def test_decimal_to_american_round_trip():
    assert _decimal_to_american(2.0) == 100
    assert _decimal_to_american(3.0) == 200
    assert _decimal_to_american(1.5) == -200
    assert _decimal_to_american(None) is None
