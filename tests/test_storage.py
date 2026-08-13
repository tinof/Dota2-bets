"""Storage round-trips, idempotency and odds change-detection."""

from __future__ import annotations

import pytest

from dota2bets import storage
from dota2bets.opendota import parse_match_detail, parse_match_summary


def quote(**over):
    q = {
        "source": "pinnacle",
        "line_key": "1|Regular|moneyline|0|Spirit|",
        "event_id": "1",
        "league": "Dota 2 - The International",
        "home": "Spirit",
        "away": "Aurora",
        "start_time": "2026-08-14T02:00:00Z",
        "market_type": "moneyline",
        "period": 0,
        "units": "Regular",
        "selection": "Spirit",
        "points": None,
        "price_american": -150,
        "limit_amount": 500,
        "is_live": 0,
        "status": "open",
        "cutoff_at": "2026-08-14T02:00:00Z",
    }
    q.update(over)
    return q


@pytest.mark.parametrize(
    ("american", "expected"),
    [(100, 2.0), (-100, 2.0), (200, 3.0), (-200, 1.5), (None, None)],
)
def test_american_to_decimal(american, expected):
    assert storage.american_to_decimal(american) == expected


def test_detail_round_trips(conn, match_detail):
    p = parse_match_detail(match_detail)
    storage.upsert_matches(conn, [p["match"]])
    storage.upsert_draft_events(conn, p["draft_events"])
    storage.upsert_match_players(conn, p["players"])
    storage.upsert_timeseries(conn, p["timeseries"])
    conn.commit()

    s = storage.summary(conn)
    assert s["matches"] == 1
    assert s["draft_events"] == len(p["draft_events"])
    assert s["match_players"] == 10
    assert s["timeseries_points"] == len(p["timeseries"])


def test_reingesting_detail_is_idempotent(conn, match_detail):
    p = parse_match_detail(match_detail)
    for _ in range(2):
        storage.upsert_matches(conn, [p["match"]])
        storage.upsert_draft_events(conn, p["draft_events"])
        storage.upsert_timeseries(conn, p["timeseries"])
    conn.commit()
    s = storage.summary(conn)
    assert s["matches"] == 1
    assert s["draft_events"] == len(p["draft_events"])


def test_summary_reingest_preserves_fetched_detail(conn, match_detail, pro_matches):
    """A later /proMatches sweep must not wipe patch/detail_fetched_at."""
    p = parse_match_detail(match_detail)
    storage.upsert_matches(conn, [p["match"]])
    summary_row = parse_match_summary({**match_detail, "leagueid": match_detail["leagueid"]})
    storage.upsert_matches(conn, [summary_row])
    conn.commit()

    row = conn.execute(
        "SELECT * FROM matches WHERE match_id=?", (match_detail["match_id"],)
    ).fetchone()
    assert row["detail_fetched_at"] is not None
    assert row["patch"] == match_detail["patch"]
    assert storage.matches_needing_detail(conn) == []


def test_matches_needing_detail_finds_summary_only_rows(conn, pro_matches):
    storage.upsert_matches(conn, [parse_match_summary(m) for m in pro_matches])
    conn.commit()
    assert len(storage.matches_needing_detail(conn)) == len(pro_matches)


def test_unchanged_odds_are_not_rewritten(conn):
    written, skipped = storage.insert_odds_snapshots(conn, [quote()], captured_at=100)
    assert (written, skipped) == (1, 0)
    written, skipped = storage.insert_odds_snapshots(conn, [quote()], captured_at=160)
    assert (written, skipped) == (0, 1)


def test_price_move_writes_a_new_snapshot(conn):
    storage.insert_odds_snapshots(conn, [quote()], captured_at=100)
    written, _ = storage.insert_odds_snapshots(conn, [quote(price_american=-165)], captured_at=160)
    assert written == 1
    rows = conn.execute(
        "SELECT price_american, price_decimal FROM odds_snapshots ORDER BY captured_at"
    ).fetchall()
    assert [r["price_american"] for r in rows] == [-150, -165]
    assert rows[0]["price_decimal"] == pytest.approx(1.6667, abs=1e-4)


def test_limit_and_status_changes_are_tracked(conn):
    storage.insert_odds_snapshots(conn, [quote()], captured_at=100)
    written, _ = storage.insert_odds_snapshots(conn, [quote(status="closed")], captured_at=160)
    assert written == 1
    written, _ = storage.insert_odds_snapshots(
        conn, [quote(status="closed", limit_amount=50)], captured_at=220
    )
    assert written == 1


def test_distinct_lines_are_independent_series(conn):
    a = quote()
    b = quote(line_key="1|Regular|moneyline|0|Aurora|", selection="Aurora", price_american=130)
    written, _ = storage.insert_odds_snapshots(conn, [a, b], captured_at=100)
    assert written == 2
    assert storage.summary(conn)["odds_lines"] == 2
