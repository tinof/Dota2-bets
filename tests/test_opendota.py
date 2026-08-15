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


def test_explorer_returns_rows_and_encodes_sql(monkeypatch):
    from dota2bets.opendota import OpenDotaClient

    client = OpenDotaClient(api_key="test_key", delay_s=0)
    seen_requests = []

    class Resp:
        def __init__(self, data):
            self._data = data
            self.status_code = 200

        def json(self):
            return self._data

        def raise_for_status(self):
            pass

    def fake_get(path, params=None):
        seen_requests.append((path, params))
        return Resp({"rows": [{"match_id": 12345}], "err": None})

    monkeypatch.setattr(client._client, "get", fake_get)
    rows = client.explorer("SELECT * FROM matches WHERE match_id = 12345")
    assert rows == [{"match_id": 12345}]
    assert len(seen_requests) == 1
    path, params = seen_requests[0]
    assert path == "/explorer"
    assert params["sql"] == "SELECT * FROM matches WHERE match_id = 12345"
    assert params["api_key"] == "test_key"


def test_explorer_raises_on_err(monkeypatch):
    import pytest

    from dota2bets.opendota import OpenDotaClient

    client = OpenDotaClient(delay_s=0)

    class Resp:
        def __init__(self, data):
            self._data = data
            self.status_code = 200

        def json(self):
            return self._data

        def raise_for_status(self):
            pass

    def fake_get(path, params=None):
        return Resp({"rows": [], "err": "syntax error at or near 'INVALID'"})

    monkeypatch.setattr(client._client, "get", fake_get)
    with pytest.raises(RuntimeError, match="syntax error"):
        client.explorer("INVALID SQL")


def test_patch_index_maps_names_to_ids(monkeypatch):
    from dota2bets.opendota import OpenDotaClient

    client = OpenDotaClient(delay_s=0)
    calls = []

    class Resp:
        def __init__(self, data):
            self._data = data
            self.status_code = 200

        def json(self):
            return self._data

        def raise_for_status(self):
            pass

    def fake_get(path, params=None):
        calls.append(path)
        return Resp([
            {"id": 0, "name": "6.70"},
            {"id": 59, "name": "7.40"},
            {"id": 60, "name": "7.41"},
        ])

    monkeypatch.setattr(client._client, "get", fake_get)
    pmap = client.patch_index()
    assert pmap == {"6.70": 0, "7.40": 59, "7.41": 60}
    # Second call uses cached index
    pmap2 = client.patch_index()
    assert pmap2 == pmap
    assert len(calls) == 1


def test_patch_resolves_by_start_time_when_the_label_lags():
    """Explorer's match_patch lagged 7.41's release by two days.

    Those 194 matches are exactly the ones the ratings' patch-transition feature reads,
    so trusting the stale label shifted a transition by two days. The start time is
    authoritative, as it is for /matches/{id}.
    """
    from dota2bets.opendota import build_patch_timeline, resolve_patch

    constants = [
        {"id": 59, "name": "7.40", "date": "2025-12-16T00:50:40.281Z"},
        {"id": 60, "name": "7.41", "date": "2026-03-24T00:50:59.580Z"},
    ]
    timeline = build_patch_timeline(constants)
    patch_map = {"7.40": 59, "7.41": 60}

    # Real match 8741426861: started 19 minutes after 7.41 shipped, labelled '7.40'.
    assert resolve_patch(1774314551, "7.40", patch_map, timeline) == 60
    # A match genuinely before the release keeps the older patch.
    assert resolve_patch(1774313000, "7.40", patch_map, timeline) == 59
    # With no timeline the label is still honoured.
    assert resolve_patch(1774314551, "7.40", patch_map, None) == 59
    # An unresolvable label yields no patch rather than a guess.
    assert resolve_patch(None, "9.99", patch_map, timeline) is None


def test_parse_explorer_rows_matches_detail(
    match_detail, explorer_matches, explorer_picks_bans, explorer_player_matches
):
    from dota2bets.opendota import parse_explorer_rows, parse_match_detail

    patch_map = {"7.41": 60}
    bulk = parse_explorer_rows(
        explorer_matches,
        explorer_picks_bans,
        explorer_player_matches,
        patch_map,
        fetched_at=1000,
    )
    detail = parse_match_detail(match_detail, fetched_at=1000)

    # Match row: core fields agree
    b_match = bulk["match"][0]
    d_match = detail["match"]
    assert b_match["match_id"] == d_match["match_id"]
    assert b_match["patch"] == d_match["patch"] == 60
    assert b_match["detail_fetched_at"] == 1000
    assert b_match["radiant_win"] == d_match["radiant_win"]
    assert b_match["radiant_team_id"] == d_match["radiant_team_id"]
    assert b_match["dire_team_id"] == d_match["dire_team_id"]
    assert b_match["radiant_name"] == d_match["radiant_name"]
    assert b_match["dire_name"] == d_match["dire_name"]
    assert b_match["pre_game_duration"] is None

    # Draft events: same (ord, hero_id, is_pick, team)
    b_draft = [(e["ord"], e["hero_id"], e["is_pick"], e["team"]) for e in bulk["draft_events"]]
    d_draft = [(e["ord"], e["hero_id"], e["is_pick"], e["team"]) for e in detail["draft_events"]]
    assert b_draft == d_draft

    # Players: match on slot, hero, is_radiant, k/d/a/gpm/xpm
    b_players = sorted(bulk["players"], key=lambda p: p["player_slot"])
    d_players = sorted(detail["players"], key=lambda p: p["player_slot"])
    assert len(b_players) == len(d_players) == 10
    for bp, dp in zip(b_players, d_players, strict=True):
        assert bp["player_slot"] == dp["player_slot"]
        assert bp["account_id"] == dp["account_id"]
        assert bp["hero_id"] == dp["hero_id"]
        assert bp["is_radiant"] == dp["is_radiant"]
        assert bp["kills"] == dp["kills"]
        assert bp["deaths"] == dp["deaths"]
        assert bp["assists"] == dp["assists"]
        assert bp["gold_per_min"] == dp["gold_per_min"]
        assert bp["xp_per_min"] == dp["xp_per_min"]
        assert bp["player_name"] is None

    # Teams match
    assert len(bulk["teams"]) == len(detail["teams"]) == 2
    assert {t["team_id"] for t in bulk["teams"]} == {t["team_id"] for t in detail["teams"]}


def test_parse_explorer_rows_derives_is_radiant():
    from dota2bets.opendota import parse_explorer_rows

    players = [
        {"match_id": 1, "player_slot": 0, "hero_id": 1},
        {"match_id": 1, "player_slot": 4, "hero_id": 2},
        {"match_id": 1, "player_slot": 128, "hero_id": 3},
        {"match_id": 1, "player_slot": 132, "hero_id": 4},
    ]
    parsed = parse_explorer_rows([{"match_id": 1}], [], players, {})
    assert [p["is_radiant"] for p in parsed["players"]] == [1, 1, 0, 0]


def test_parse_explorer_rows_unresolvable_patch_leaves_null():
    from dota2bets.opendota import parse_explorer_rows

    matches = [{"match_id": 1, "patch": "7.99"}]
    parsed = parse_explorer_rows(matches, [], [], {"7.41": 60}, fetched_at=1000)
    assert parsed["match"][0]["patch"] is None
    # Unresolved patch leaves detail_fetched_at None
    assert parsed["match"][0]["detail_fetched_at"] is None


def test_parse_explorer_rows_missing_draft_empty_list():
    from dota2bets.opendota import parse_explorer_rows

    matches = [{"match_id": 1, "patch": "7.41"}]
    # 10 players, no draft events
    players = [{"match_id": 1, "player_slot": i, "hero_id": i + 1} for i in range(10)]
    parsed = parse_explorer_rows(matches, [], players, {"7.41": 60}, fetched_at=1000)
    assert parsed["draft_events"] == []
    assert parsed["match"][0]["detail_fetched_at"] == 1000


def test_parse_explorer_rows_partial_players_not_marked_fetched():
    from dota2bets.opendota import parse_explorer_rows

    matches = [{"match_id": 1, "patch": "7.41"}]
    # only 8 players
    players = [{"match_id": 1, "player_slot": i, "hero_id": i + 1} for i in range(8)]
    parsed = parse_explorer_rows(matches, [], players, {"7.41": 60}, fetched_at=1000)
    assert parsed["match"][0]["detail_fetched_at"] is None


