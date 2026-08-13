"""Suspension/removal tracking and the raw-payload archive.

A bookmaker pulling a market is a timed event we need in the record; without these rows
"suspended during the draft" is indistinguishable from "price didn't move".
"""

from __future__ import annotations

from dota2bets import archive, storage


def history(conn, line_key="1|Regular|moneyline|0|Spirit|"):
    return [
        (r["status"], r["price_american"], r["captured_at"])
        for r in conn.execute(
            "SELECT status, price_american, captured_at FROM odds_snapshots "
            "WHERE line_key=? ORDER BY captured_at, id",
            (line_key,),
        )
    ]


def test_vanished_line_is_tombstoned(conn, quote):
    storage.insert_odds_snapshots(conn, [quote()], captured_at=100)
    assert storage.write_tombstones(conn, "pinnacle", [], captured_at=160) == 1
    assert history(conn) == [("open", -150, 100), ("gone", None, 160)]


def test_present_line_is_not_tombstoned(conn, quote):
    storage.insert_odds_snapshots(conn, [quote()], captured_at=100)
    written = storage.write_tombstones(conn, "pinnacle", [quote()["line_key"]], captured_at=160)
    assert written == 0


def test_tombstone_is_not_repeated_while_line_stays_gone(conn, quote):
    storage.insert_odds_snapshots(conn, [quote()], captured_at=100)
    storage.write_tombstones(conn, "pinnacle", [], captured_at=160)
    assert storage.write_tombstones(conn, "pinnacle", [], captured_at=220) == 0
    assert len(history(conn)) == 2


def test_line_returning_after_suspension_is_recorded(conn, quote):
    """The full suspend-then-resume cycle a teamfight should produce."""
    storage.insert_odds_snapshots(conn, [quote()], captured_at=100)
    storage.write_tombstones(conn, "pinnacle", [], captured_at=160)
    written, _ = storage.insert_odds_snapshots(conn, [quote(price_american=-180)], captured_at=220)
    assert written == 1
    assert history(conn) == [("open", -150, 100), ("gone", None, 160), ("open", -180, 220)]


def test_line_returning_at_an_unchanged_price_is_still_recorded(conn, quote):
    """Otherwise the market would look like it never came back."""
    storage.insert_odds_snapshots(conn, [quote()], captured_at=100)
    storage.write_tombstones(conn, "pinnacle", [], captured_at=160)
    written, _ = storage.insert_odds_snapshots(conn, [quote()], captured_at=220)
    assert written == 1
    assert [h[0] for h in history(conn)] == ["open", "gone", "open"]


def test_tombstone_preserves_line_identity(conn, quote):
    storage.insert_odds_snapshots(conn, [quote()], captured_at=100)
    storage.write_tombstones(conn, "pinnacle", [], captured_at=160)
    row = conn.execute("SELECT * FROM odds_snapshots WHERE status='gone'").fetchone()
    assert (row["home"], row["away"], row["selection"]) == ("Spirit", "Aurora", "Spirit")
    assert row["market_type"] == "moneyline"
    assert row["price_decimal"] is None


def test_tombstones_are_scoped_to_their_source(conn, quote):
    other = quote(source="theoddsapi:bet365", line_key="other")
    storage.insert_odds_snapshots(conn, [quote(), other], captured_at=100)
    # Pinnacle's poll returning nothing must not tombstone another book's lines.
    assert storage.write_tombstones(conn, "pinnacle", [], captured_at=160) == 1
    assert history(conn, "other") == [("open", -150, 100)]


def test_source_prefix_covers_per_book_fanout(conn, quote):
    a = quote(source="theoddsapi:bet365", line_key="a")
    b = quote(source="theoddsapi:unibet", line_key="b")
    storage.insert_odds_snapshots(conn, [a, b], captured_at=100)
    assert storage.write_tombstones(conn, "theoddsapi", ["a"], captured_at=160) == 1


def test_cutoff_change_is_recorded(conn, quote):
    """The map-2/3 window signal: Pinnacle pushing a cutoff without moving the price."""
    storage.insert_odds_snapshots(conn, [quote()], captured_at=100)
    written, skipped = storage.insert_odds_snapshots(
        conn, [quote(cutoff_at="2026-08-14T02:41:07.221000+00:00")], captured_at=160
    )
    assert (written, skipped) == (1, 0)


def test_open_lines_excludes_tombstoned(conn, quote):
    storage.insert_odds_snapshots(conn, [quote()], captured_at=100)
    assert len(storage.open_lines(conn, "pinnacle")) == 1
    storage.write_tombstones(conn, "pinnacle", [], captured_at=160)
    assert storage.open_lines(conn, "pinnacle") == []


def test_archive_round_trips(tmp_path):
    payload = {"matchups": [{"id": 1}], "markets": [{"matchupId": 1}]}
    path = archive.write_payload(tmp_path, "pinnacle", 1786600000, payload)
    assert path.exists()
    assert archive.read_payload(path) == payload


def test_archive_path_is_partitioned_by_day(tmp_path):
    path = archive.archive_path(tmp_path, "pinnacle", 1786600000)
    assert path.parent.name == "2026-08-13"
    assert path.name == "1786600000.json.gz"
