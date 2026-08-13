"""Parser tests against recorded OpenDota payloads (no network)."""

from __future__ import annotations

from dota2bets.opendota import parse_match_detail, parse_match_summary


def test_parse_summary_maps_core_fields(pro_matches):
    row = parse_match_summary(pro_matches[0])
    assert row["match_id"] == pro_matches[0]["match_id"]
    assert row["league_id"] == pro_matches[0]["leagueid"]
    assert row["radiant_win"] in (0, 1)


def test_summary_has_no_detail_columns(pro_matches):
    """Summaries must not carry detail keys, or upsert would overwrite fetched detail."""
    row = parse_match_summary(pro_matches[0])
    assert "detail_fetched_at" not in row
    assert "patch" not in row


def test_parse_detail_splits_all_tables(match_detail):
    p = parse_match_detail(match_detail, fetched_at=1000)

    assert p["match"]["match_id"] == match_detail["match_id"]
    assert p["match"]["detail_fetched_at"] == 1000
    assert p["match"]["patch"] == match_detail["patch"]
    assert p["match"]["league_name"] == match_detail["league"]["name"]

    assert len(p["draft_events"]) == len(match_detail["picks_bans"])
    assert len(p["players"]) == 10
    assert len(p["timeseries"]) == len(match_detail["radiant_gold_adv"])
    assert len(p["teams"]) == 2


def test_draft_events_preserve_order_and_pick_flag(match_detail):
    events = parse_match_detail(match_detail)["draft_events"]
    orders = [e["ord"] for e in events]
    assert orders == sorted(orders)
    assert orders[0] == 0
    assert {e["is_pick"] for e in events} <= {0, 1}
    # A captains-mode draft has exactly ten picks.
    assert sum(e["is_pick"] for e in events) == 10
    assert {e["team"] for e in events} == {0, 1}


def test_players_split_by_side(match_detail):
    players = parse_match_detail(match_detail)["players"]
    assert sum(p["is_radiant"] for p in players) == 5
    assert all(p["hero_id"] for p in players)


def test_timeseries_is_minute_indexed(match_detail):
    ts = parse_match_detail(match_detail)["timeseries"]
    assert [t["minute"] for t in ts] == list(range(len(ts)))
    assert ts[0]["radiant_gold_adv"] == match_detail["radiant_gold_adv"][0]


def test_rosters_link_players_to_their_own_team(match_detail):
    p = parse_match_detail(match_detail)
    radiant = {
        r["account_id"] for r in p["rosters"] if r["team_id"] == match_detail["radiant_team_id"]
    }
    radiant_players = {
        pl["account_id"] for pl in p["players"] if pl["is_radiant"] and pl["account_id"]
    }
    assert radiant == radiant_players
