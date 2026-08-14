"""Entity resolution: bookmaker names -> OpenDota teams -> series."""

from __future__ import annotations

import gzip
import json

import pytest

from dota2bets import aliases, storage

RESOLVER = aliases.AliasResolver.load()

# One TI series: two maps, sides swapped between them, as OpenDota records it.
SERIES_START = 1786613552
MATCHES = [
    {
        "match_id": 1,
        "series_id": 1130066,
        "league_name": "The International 2026",
        "start_time": SERIES_START,
        "duration": 3227,
        "radiant_team_id": 10136357,
        "radiant_name": "Nigma Galaxy ",
        "dire_team_id": 2586976,
        "dire_name": "OG",
        "radiant_win": 1,
    },
    {
        "match_id": 2,
        "series_id": 1130066,
        "league_name": "The International 2026",
        "start_time": SERIES_START + 4919,
        "duration": 4461,
        "radiant_team_id": 2586976,
        "radiant_name": "OG",
        "dire_team_id": 10136357,
        "dire_name": "Nigma Galaxy ",
        "radiant_win": 0,
    },
]


@pytest.fixture
def series_conn(conn, quote):
    storage.upsert_matches(conn, MATCHES)
    conn.commit()
    return conn


def _event(conn, quote, event_id, home, away, start, **over):
    storage.insert_odds_snapshots(
        conn,
        [
            quote(
                event_id=event_id,
                line_key=f"{event_id}|k",
                home=home,
                away=away,
                start_time=start,
                **over,
            )
        ],
    )


def test_resolves_canonical_and_alias_names():
    assert RESOLVER.resolve("Team Spirit").team_id == 7119388
    assert RESOLVER.resolve("Spirit").team_id == 7119388
    assert RESOLVER.resolve("LGD").team_id == RESOLVER.resolve("LGD Gaming").team_id


def test_strips_the_kills_pseudo_event_suffix():
    assert aliases.split_kills("Spirit (Kills)") == ("Spirit", True)
    assert aliases.split_kills("Spirit") == ("Spirit", False)
    assert RESOLVER.resolve("Spirit (Kills)").team_id == 7119388


def test_matching_ignores_case_and_stray_whitespace():
    """OpenDota's own names carry artifacts -- `"Nigma Galaxy "` is real data."""
    assert RESOLVER.resolve("Nigma Galaxy ").team_id == 10136357
    assert RESOLVER.resolve("  team   VISION ").team_id == 9572001


def test_unknown_names_resolve_to_none_rather_than_guessing():
    assert RESOLVER.resolve("Some Tier 3 Squad") is None
    assert RESOLVER.resolve(None) is None
    assert RESOLVER.resolve("") is None


def test_alias_table_rejects_a_name_claimed_by_two_teams():
    with pytest.raises(ValueError, match="both"):
        aliases.AliasResolver.from_yaml(
            "teams:\n"
            "  - {team_id: 1, name: Team One, aliases: [Spirit]}\n"
            "  - {team_id: 2, name: Team Two, aliases: [spirit]}\n"
        )


def test_event_joins_the_series_with_the_nearest_start(series_conn, quote):
    """Pinnacle's start time is the scheduled slot, not when map one began."""
    storage.upsert_matches(
        series_conn,
        [{**MATCHES[0], "match_id": 9, "series_id": 999, "start_time": SERIES_START + 86400}],
    )
    _event(series_conn, quote, "100", "Nigma Galaxy", "OG", "2026-08-13T09:45:00Z")
    aliases.resolve_events(series_conn, RESOLVER)
    row = series_conn.execute("SELECT * FROM event_series_map WHERE event_id='100'").fetchone()
    assert row["series_id"] == 1130066
    assert row["home_team_id"] == 10136357
    assert abs(row["start_skew_s"]) < aliases.DEFAULT_TOLERANCE_S


def test_a_series_further_away_than_the_tolerance_is_not_joined(series_conn, quote):
    _event(series_conn, quote, "101", "Nigma Galaxy", "OG", "2026-09-30T04:30:00Z")
    report = aliases.resolve_events(series_conn, RESOLVER)
    row = series_conn.execute("SELECT * FROM event_series_map WHERE event_id='101'").fetchone()
    assert row["series_id"] is None
    assert report.unjoined_events


def test_live_child_inherits_its_parents_series(series_conn, quote):
    """A live re-listing's own start time can be over an hour from the real one."""
    _event(series_conn, quote, "200", "Nigma Galaxy", "OG", "2026-08-13T09:45:00Z")
    _event(series_conn, quote, "201", "Nigma Galaxy", "OG", "2026-11-01T00:00:00Z", is_live=1)
    aliases.resolve_events(series_conn, RESOLVER, parent_links={"201": "200"})
    child = series_conn.execute("SELECT * FROM event_series_map WHERE event_id='201'").fetchone()
    assert child["parent_event_id"] == "200"
    assert child["series_id"] == 1130066


def test_kills_events_are_flagged_and_still_resolve(series_conn, quote):
    _event(series_conn, quote, "300", "Nigma Galaxy (Kills)", "OG (Kills)", "2026-08-13T09:45:00Z")
    aliases.resolve_events(series_conn, RESOLVER)
    row = series_conn.execute("SELECT * FROM event_series_map WHERE event_id='300'").fetchone()
    assert row["is_kills"] == 1
    assert row["series_id"] == 1130066


def test_unresolved_names_are_reported_with_their_snapshot_counts(series_conn, quote):
    _event(series_conn, quote, "400", "Mystery Team", "OG", "2026-08-13T09:45:00Z")
    report = aliases.resolve_events(series_conn, RESOLVER)
    assert ("Mystery Team", 1) in report.unresolved_names
    assert "Mystery Team" in report.format()


def test_resolving_twice_replaces_rather_than_duplicates(series_conn, quote):
    _event(series_conn, quote, "500", "Nigma Galaxy", "OG", "2026-08-13T09:45:00Z")
    first = aliases.resolve_events(series_conn, RESOLVER)
    second = aliases.resolve_events(series_conn, RESOLVER)
    n = series_conn.execute("SELECT COUNT(*) FROM event_series_map").fetchone()[0]
    assert n == first.events == second.events == 1


def test_dry_run_writes_nothing(series_conn, quote):
    _event(series_conn, quote, "600", "Nigma Galaxy", "OG", "2026-08-13T09:45:00Z")
    aliases.resolve_events(series_conn, RESOLVER, dry_run=True)
    assert series_conn.execute("SELECT COUNT(*) FROM event_series_map").fetchone()[0] == 0


def test_parent_links_are_read_out_of_the_archive(tmp_path):
    day = tmp_path / "pinnacle" / "2026-08-14"
    day.mkdir(parents=True)
    payload = {"matchups": [{"id": 2, "parentId": 1}, {"id": 1, "parentId": None}], "markets": []}
    with gzip.open(day / "1786700000.json.gz", "wt", encoding="utf-8") as fh:
        json.dump(payload, fh)
    assert aliases.parent_links_from_archive(tmp_path) == {"2": "1"}


def test_the_odds_series_view_carries_the_join(series_conn, quote):
    _event(series_conn, quote, "700", "Nigma Galaxy", "OG", "2026-08-13T09:45:00Z")
    aliases.resolve_events(series_conn, RESOLVER)
    row = series_conn.execute("SELECT * FROM v_odds_series WHERE event_id='700'").fetchone()
    assert row["series_id"] == 1130066
    assert row["selection"] == "Spirit"


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("2026-08-14T02:00:00Z", 1786672800),
        ("2026-08-14T02:00:00+00:00", 1786672800),
        (1786672800, 1786672800),
        ("1786672800", 1786672800),
        (None, None),
        ("not a time", None),
    ],
)
def test_parse_ts_accepts_both_stored_representations(value, expected):
    assert storage.parse_ts(value) == expected
