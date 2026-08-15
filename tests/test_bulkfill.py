from __future__ import annotations

import argparse

from dota2bets import cli, storage


def test_bulkfill_parser_args():
    parser = cli.build_parser()
    args = parser.parse_args([
        "bulkfill",
        "--slice-days",
        "7",
        "--since",
        "2024-04-01",
        "--with-timeseries",
        "--delay",
        "0.5",
    ])
    assert args.command == "bulkfill"
    assert args.slice_days == 7
    assert args.since == "2024-04-01"
    assert args.with_timeseries is True
    assert args.delay == 0.5


def test_bulkfill_no_matches_awaiting_detail(tmp_path, capsys):
    db_path = tmp_path / "test.sqlite"
    conn = storage.connect(db_path)
    conn.close()

    args = argparse.Namespace(
        db=str(db_path),
        slice_days=14,
        since=None,
        with_timeseries=False,
        delay=None,
    )
    rc = cli.cmd_bulkfill(args)
    assert rc == 0
    captured = capsys.readouterr()
    assert "No matches awaiting detail" in captured.out


def test_bulkfill_imports_and_enriches_matches(
    tmp_path, monkeypatch, explorer_matches, explorer_picks_bans, explorer_player_matches
):
    db_path = tmp_path / "test.sqlite"
    conn = storage.connect(db_path)

    # Insert a match summary in our DB awaiting detail
    conn.execute(
        "INSERT INTO matches ("
        "  match_id, start_time, league_name, radiant_name, dire_name, detail_fetched_at"
        ") VALUES (?, ?, ?, ?, ?, NULL)",
        (8943332564, 1786618550, "Original League Name", "LGD Gaming", "Team Resilience"),
    )
    conn.commit()
    conn.close()

    # Explorer returns our match PLUS an untracked match 999999
    untracked_match = {
        "match_id": 999999,
        "start_time": 1786618550,
        "patch": "7.41",
    }
    untracked_pb = [{"match_id": 999999, "is_pick": True, "hero_id": 1, "team": 0, "ord": 0}]
    untracked_pm = [
        {"match_id": 999999, "player_slot": i, "account_id": 100 + i, "hero_id": i + 1}
        for i in range(10)
    ]

    def fake_explorer(self, sql):
        if "picks_bans" in sql:
            return explorer_picks_bans + untracked_pb
        if "player_matches" in sql:
            return explorer_player_matches + untracked_pm
        return explorer_matches + [untracked_match]

    monkeypatch.setattr(cli.OpenDotaClient, "explorer", fake_explorer)
    monkeypatch.setattr(cli.OpenDotaClient, "patch_index", lambda self: {"7.41": 60})

    args = argparse.Namespace(
        db=str(db_path),
        slice_days=14,
        since=None,
        with_timeseries=True,
        delay=None,
    )
    rc = cli.cmd_bulkfill(args)
    assert rc == 0

    conn = storage.connect_ro(db_path)
    # Check match was enriched and original summary fields preserved
    m = conn.execute("SELECT * FROM matches WHERE match_id = 8943332564").fetchone()
    assert m["patch"] == 60
    assert m["detail_fetched_at"] is not None
    assert m["league_name"] == "The International 2026"  # Updated from explorer or coalesced
    assert m["radiant_win"] == 1

    # Check draft events
    draft = conn.execute(
        "SELECT * FROM draft_events WHERE match_id = 8943332564 ORDER BY ord"
    ).fetchall()
    assert len(draft) == 24

    # Check players
    players = conn.execute(
        "SELECT * FROM match_players WHERE match_id = 8943332564 ORDER BY player_slot"
    ).fetchall()
    assert len(players) == 10
    assert sum(p["is_radiant"] for p in players) == 5

    # Check timeseries
    ts = conn.execute("SELECT * FROM match_timeseries WHERE match_id = 8943332564").fetchall()
    assert len(ts) == 4

    # Check untracked match was skipped
    assert conn.execute("SELECT COUNT(*) FROM matches WHERE match_id = 999999").fetchone()[0] == 0
    assert (
        conn.execute("SELECT COUNT(*) FROM draft_events WHERE match_id = 999999").fetchone()[0]
        == 0
    )
    conn.close()


def test_bulkfill_idempotent_rerun(
    tmp_path, monkeypatch, explorer_matches, explorer_picks_bans, explorer_player_matches
):
    db_path = tmp_path / "test.sqlite"
    conn = storage.connect(db_path)
    conn.execute(
        "INSERT INTO matches (match_id, start_time, detail_fetched_at) VALUES (?, ?, NULL)",
        (8943332564, 1786618550),
    )
    conn.commit()
    conn.close()

    def fake_explorer(self, sql):
        if "picks_bans" in sql:
            return explorer_picks_bans
        if "player_matches" in sql:
            return explorer_player_matches
        return explorer_matches

    monkeypatch.setattr(cli.OpenDotaClient, "explorer", fake_explorer)
    monkeypatch.setattr(cli.OpenDotaClient, "patch_index", lambda self: {"7.41": 60})

    args = argparse.Namespace(
        db=str(db_path),
        slice_days=14,
        since=None,
        with_timeseries=False,
        delay=None,
    )
    # First run
    assert cli.cmd_bulkfill(args) == 0
    # Second run with explicit since
    args.since = "1786618550"
    assert cli.cmd_bulkfill(args) == 0

    conn = storage.connect_ro(db_path)
    assert (
        conn.execute("SELECT COUNT(*) FROM draft_events WHERE match_id = 8943332564").fetchone()[0]
        == 24
    )
    assert (
        conn.execute("SELECT COUNT(*) FROM match_players WHERE match_id = 8943332564").fetchone()[0]
        == 10
    )
    conn.close()


def test_bulkfill_partial_match_not_marked_fetched(tmp_path, monkeypatch):
    db_path = tmp_path / "test.sqlite"
    conn = storage.connect(db_path)
    conn.execute(
        "INSERT INTO matches (match_id, start_time, detail_fetched_at) VALUES (?, ?, NULL)",
        (12345, 1786618550),
    )
    conn.commit()
    conn.close()

    # Unresolvable patch
    def fake_explorer(self, sql):
        if "player_matches" in sql:
            return [
                {"match_id": 12345, "player_slot": i, "account_id": 10 + i, "hero_id": i + 1}
                for i in range(10)
            ]
        if "picks_bans" in sql:
            return []
        return [{"match_id": 12345, "start_time": 1786618550, "patch": "9.99"}]

    monkeypatch.setattr(cli.OpenDotaClient, "explorer", fake_explorer)
    monkeypatch.setattr(cli.OpenDotaClient, "patch_index", lambda self: {"7.41": 60})

    args = argparse.Namespace(
        db=str(db_path),
        slice_days=14,
        since=None,
        with_timeseries=False,
        delay=None,
    )
    assert cli.cmd_bulkfill(args) == 0

    conn = storage.connect_ro(db_path)
    m = conn.execute("SELECT * FROM matches WHERE match_id = 12345").fetchone()
    assert m["patch"] is None
    assert m["detail_fetched_at"] is None
    count = conn.execute(
        "SELECT COUNT(*) FROM match_players WHERE match_id = 12345"
    ).fetchone()[0]
    assert count == 10
    conn.close()
