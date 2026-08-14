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


def test_rate_limit_backoff_is_exponential_and_honours_retry_after(monkeypatch):
    """A sustained throttle must not end a long crawl after a few seconds of waiting."""
    from dota2bets import opendota

    slept: list[float] = []
    monkeypatch.setattr(opendota.time, "sleep", slept.append)
    client = opendota.OpenDotaClient(api_key=None, delay_s=0)
    calls = {"n": 0}

    class Resp:
        def __init__(self, code, headers=None):
            self.status_code = code
            self.headers = headers or {}

        def json(self):
            return {"ok": True}

        def raise_for_status(self):
            return None

    def fake_get(path, params=None):
        calls["n"] += 1
        if calls["n"] == 1:
            return Resp(429)
        if calls["n"] == 2:
            return Resp(429, {"Retry-After": "90"})
        return Resp(200)

    monkeypatch.setattr(client._client, "get", fake_get)
    assert client.get("/proMatches") == {"ok": True}
    assert slept[0] == opendota.BACKOFF_BASE_S
    # Retry-After wins when it asks for longer than the doubling schedule would wait.
    assert slept[1] == 90


def test_keyed_client_uses_the_faster_delay():
    from dota2bets.opendota import KEYED_DELAY_S, OpenDotaClient

    assert OpenDotaClient(api_key="k").delay_s == KEYED_DELAY_S
    assert OpenDotaClient(api_key="k", delay_s=2.0).delay_s == 2.0


def test_iter_pro_matches_resumes_below_a_cursor(monkeypatch):
    """Restarting a crawl must continue past what is stored, not re-walk it."""
    from dota2bets.opendota import OpenDotaClient

    client = OpenDotaClient(api_key=None, delay_s=0)
    seen_cursors: list[int | None] = []

    def fake_pro_matches(less_than_match_id=None):
        seen_cursors.append(less_than_match_id)
        if less_than_match_id is None or less_than_match_id > 200:
            base = less_than_match_id or 300
            return [{"match_id": base - 1}, {"match_id": base - 2}]
        return []

    monkeypatch.setattr(client, "pro_matches", fake_pro_matches)
    ids = [m["match_id"] for m in client.iter_pro_matches(max_matches=4, before_match_id=250)]
    assert seen_cursors[0] == 250
    assert ids == [249, 248, 247, 246]
