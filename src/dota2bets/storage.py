"""SQLite storage for pro-match data and odds snapshots.

Odds are append-only, but a snapshot is only written when the price actually moved
(see :func:`insert_odds_snapshot`), so the table stays a line-history rather than a
poll-log. Everything is keyed so that re-running ingestion is idempotent.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import time
from collections.abc import Iterable, Sequence
from datetime import datetime
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

DEFAULT_DB_PATH = Path("data/dota2bets.sqlite")

SCHEMA = """
CREATE TABLE IF NOT EXISTS matches (
    match_id           INTEGER PRIMARY KEY,
    league_id          INTEGER,
    league_name        TEXT,
    series_id          INTEGER,
    series_type        INTEGER,
    start_time         INTEGER,
    duration           INTEGER,
    pre_game_duration  INTEGER,
    patch              INTEGER,
    radiant_team_id    INTEGER,
    radiant_name       TEXT,
    dire_team_id       INTEGER,
    dire_name          TEXT,
    radiant_win        INTEGER,
    radiant_score      INTEGER,
    dire_score         INTEGER,
    detail_fetched_at  INTEGER
);
CREATE INDEX IF NOT EXISTS idx_matches_start ON matches (start_time);
CREATE INDEX IF NOT EXISTS idx_matches_league ON matches (league_id);
CREATE INDEX IF NOT EXISTS idx_matches_detail ON matches (detail_fetched_at);

-- One row per pick/ban, in draft order. `team` is 0 = radiant, 1 = dire (OpenDota convention).
CREATE TABLE IF NOT EXISTS draft_events (
    match_id  INTEGER NOT NULL,
    ord       INTEGER NOT NULL,
    is_pick   INTEGER NOT NULL,
    hero_id   INTEGER NOT NULL,
    team      INTEGER NOT NULL,
    PRIMARY KEY (match_id, ord)
);

CREATE TABLE IF NOT EXISTS match_players (
    match_id     INTEGER NOT NULL,
    player_slot  INTEGER NOT NULL,
    account_id   INTEGER,
    player_name  TEXT,
    hero_id      INTEGER,
    is_radiant   INTEGER,
    lane_role    INTEGER,
    kills        INTEGER,
    deaths       INTEGER,
    assists      INTEGER,
    gold_per_min INTEGER,
    xp_per_min   INTEGER,
    PRIMARY KEY (match_id, player_slot)
);
CREATE INDEX IF NOT EXISTS idx_players_account ON match_players (account_id);

-- Per-minute team-level advantage series; the backbone of the live model (Phase 3).
CREATE TABLE IF NOT EXISTS match_timeseries (
    match_id          INTEGER NOT NULL,
    minute            INTEGER NOT NULL,
    radiant_gold_adv  INTEGER,
    radiant_xp_adv    INTEGER,
    PRIMARY KEY (match_id, minute)
);

CREATE TABLE IF NOT EXISTS teams (
    team_id    INTEGER PRIMARY KEY,
    name       TEXT,
    last_seen  INTEGER
);

CREATE TABLE IF NOT EXISTS rosters (
    team_id      INTEGER NOT NULL,
    account_id   INTEGER NOT NULL,
    player_name  TEXT,
    observed_at  INTEGER NOT NULL,
    PRIMARY KEY (team_id, account_id, observed_at)
);

-- Line history. `line_key` identifies a single continuously-priced selection so that
-- change-detection and later closing-line lookups are cheap.
CREATE TABLE IF NOT EXISTS odds_snapshots (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    source           TEXT    NOT NULL,
    line_key         TEXT    NOT NULL,
    event_id         TEXT    NOT NULL,
    league           TEXT,
    home             TEXT,
    away             TEXT,
    start_time       TEXT,
    market_type      TEXT    NOT NULL,
    period           INTEGER,
    units            TEXT,
    selection        TEXT    NOT NULL,
    points           REAL,
    price_american   REAL,
    price_decimal    REAL,
    limit_amount     REAL,
    is_live          INTEGER,
    status           TEXT,
    cutoff_at        TEXT,
    captured_at      INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_odds_linekey ON odds_snapshots (source, line_key, captured_at);
CREATE INDEX IF NOT EXISTS idx_odds_event ON odds_snapshots (source, event_id);
CREATE INDEX IF NOT EXISTS idx_odds_captured ON odds_snapshots (captured_at);

-- Which series each bookmaker event prices (see `aliases.py`). Materialised rather than
-- a view because the series join is a nearest-start match against a tolerance, and
-- because it must stay stable once curated: recomputing it per query would let a later
-- backfill silently move a join that a stored evaluation already depended on.
CREATE TABLE IF NOT EXISTS event_series_map (
    source           TEXT    NOT NULL,
    event_id         TEXT    NOT NULL,
    units            TEXT,
    is_kills         INTEGER NOT NULL,
    parent_event_id  TEXT,
    home_team_id     INTEGER,
    away_team_id     INTEGER,
    series_id        INTEGER,
    start_skew_s     INTEGER,
    resolved_at      INTEGER NOT NULL,
    PRIMARY KEY (source, event_id)
);
CREATE INDEX IF NOT EXISTS idx_event_series ON event_series_map (series_id);

CREATE VIEW IF NOT EXISTS v_odds_series AS
SELECT o.*, e.series_id, e.is_kills, e.parent_event_id, e.home_team_id, e.away_team_id
FROM odds_snapshots o
JOIN event_series_map e ON e.source = o.source AND e.event_id = o.event_id;
"""


def american_to_decimal(price: float | None) -> float | None:
    """Convert American odds to decimal. Pinnacle publishes American prices."""
    if price is None:
        return None
    if price > 0:
        return 1.0 + price / 100.0
    if price < 0:
        return 1.0 + 100.0 / abs(price)
    return None


def parse_ts(value: str | int | float | None) -> int | None:
    """Epoch seconds from either representation the database holds.

    Match times are stored as epoch ints (OpenDota), odds times as ISO-8601 text
    (Pinnacle, sometimes `Z`-suffixed and sometimes `+00:00`), and the two are compared
    constantly -- cutoffs against map starts, closing lines against kick-off -- so the
    conversion lives in one place.
    """
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        return int(value)
    text = value.strip()
    if text.isdigit():
        return int(text)
    try:
        return int(datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp())
    except ValueError:
        log.warning("unparseable timestamp %r", value)
        return None


def connect(db_path: Path | str = DEFAULT_DB_PATH) -> sqlite3.Connection:
    """Open (creating if needed) the database and ensure the schema exists."""
    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    # The recorder and a backfill crawl are expected to run at the same time.
    conn.execute("PRAGMA busy_timeout=5000")
    conn.executescript(SCHEMA)
    conn.commit()
    return conn


def _upsert(conn: sqlite3.Connection, table: str, rows: Sequence[dict[str, Any]]) -> int:
    if not rows:
        return 0
    cols = list(rows[0].keys())
    placeholders = ",".join("?" * len(cols))
    sql = f"INSERT OR REPLACE INTO {table} ({','.join(cols)}) VALUES ({placeholders})"  # noqa: S608
    conn.executemany(sql, [tuple(r[c] for c in cols) for r in rows])
    return len(rows)


def upsert_matches(conn: sqlite3.Connection, rows: Sequence[dict[str, Any]]) -> int:
    """Insert match summaries without clobbering detail already fetched.

    `INSERT OR REPLACE` would null out the detail columns when a summary row for an
    already-detailed match is re-ingested, so summary fields are updated explicitly.
    """
    if not rows:
        return 0
    cols = list(rows[0].keys())
    updatable = [c for c in cols if c != "match_id"]
    sql = (
        f"INSERT INTO matches ({','.join(cols)}) VALUES ({','.join('?' * len(cols))}) "  # noqa: S608
        f"ON CONFLICT(match_id) DO UPDATE SET "
        + ",".join(f"{c}=COALESCE(excluded.{c},{c})" for c in updatable)
    )
    conn.executemany(sql, [tuple(r[c] for c in cols) for r in rows])
    return len(rows)


def upsert_draft_events(conn: sqlite3.Connection, rows: Sequence[dict[str, Any]]) -> int:
    return _upsert(conn, "draft_events", rows)


def upsert_match_players(conn: sqlite3.Connection, rows: Sequence[dict[str, Any]]) -> int:
    return _upsert(conn, "match_players", rows)


def upsert_timeseries(conn: sqlite3.Connection, rows: Sequence[dict[str, Any]]) -> int:
    return _upsert(conn, "match_timeseries", rows)


def upsert_teams(conn: sqlite3.Connection, rows: Sequence[dict[str, Any]]) -> int:
    return _upsert(conn, "teams", rows)


def record_roster(
    conn: sqlite3.Connection,
    team_id: int,
    account_id: int,
    player_name: str | None,
    observed_at: int,
) -> None:
    conn.execute(
        "INSERT OR IGNORE INTO rosters (team_id, account_id, player_name, observed_at) "
        "VALUES (?,?,?,?)",
        (team_id, account_id, player_name, observed_at),
    )


_ODDS_COLS = (
    "source",
    "line_key",
    "event_id",
    "league",
    "home",
    "away",
    "start_time",
    "market_type",
    "period",
    "units",
    "selection",
    "points",
    "price_american",
    "price_decimal",
    "limit_amount",
    "is_live",
    "status",
    "cutoff_at",
    "captured_at",
)

# A snapshot is re-written only when one of these differs from the previous snapshot.
# `cutoff_at` belongs here: Pinnacle pushes a market's cutoff forward as a series
# progresses, and that push is the signal for whether a map stays biddable through its
# draft. Dropping it because the price happened not to move would lose the measurement.
_CHANGE_COLS = ("price_american", "points", "limit_amount", "status", "is_live", "cutoff_at")

# Written when a previously-open line vanishes from a poll. A bookmaker pulling a market
# (suspension during a fight, or the map going off the board) is a real, timed event, and
# without a row for it "suspended" is indistinguishable from "unchanged".
TOMBSTONE_STATUS = "gone"


def insert_odds_snapshots(
    conn: sqlite3.Connection, quotes: Iterable[dict[str, Any]], captured_at: int | None = None
) -> tuple[int, int]:
    """Append quotes whose price/limit/status changed since the last snapshot.

    Returns ``(written, skipped_unchanged)``.
    """
    captured_at = captured_at or int(time.time())
    written = skipped = 0
    for q in quotes:
        row = dict(q)
        row.setdefault("captured_at", captured_at)
        row.setdefault("price_decimal", american_to_decimal(row.get("price_american")))
        prev = conn.execute(
            "SELECT * FROM odds_snapshots WHERE source=? AND line_key=? "
            "ORDER BY captured_at DESC, id DESC LIMIT 1",
            (row["source"], row["line_key"]),
        ).fetchone()
        if prev is not None and all(prev[c] == row.get(c) for c in _CHANGE_COLS):
            skipped += 1
            continue
        conn.execute(
            f"INSERT INTO odds_snapshots ({','.join(_ODDS_COLS)}) "  # noqa: S608
            f"VALUES ({','.join('?' * len(_ODDS_COLS))})",
            tuple(row.get(c) for c in _ODDS_COLS),
        )
        written += 1
    conn.commit()
    return written, skipped


def open_lines(conn: sqlite3.Connection, source_prefix: str) -> list[sqlite3.Row]:
    """Latest snapshot of every line for `source_prefix` that has not been tombstoned.

    The prefix match covers sources that fan out per bookmaker (``theoddsapi:<book>``).
    """
    return conn.execute(
        "SELECT * FROM ("
        "  SELECT *, ROW_NUMBER() OVER ("
        "    PARTITION BY source, line_key ORDER BY captured_at DESC, id DESC"
        "  ) rn FROM odds_snapshots WHERE source LIKE ?"
        ") WHERE rn = 1 AND status IS NOT ?",
        (f"{source_prefix}%", TOMBSTONE_STATUS),
    ).fetchall()


def write_tombstones(
    conn: sqlite3.Connection,
    source_prefix: str,
    seen_line_keys: Iterable[str],
    captured_at: int | None = None,
) -> int:
    """Mark lines that were open but are absent from this poll as gone.

    Carries the last-known identity of the line forward with a null price so the gap is
    queryable as an interval. A line that comes back is written normally on the next
    poll, because its status differs from the tombstone.
    """
    captured_at = captured_at or int(time.time())
    seen = set(seen_line_keys)
    written = 0
    for prev in open_lines(conn, source_prefix):
        if prev["line_key"] in seen:
            continue
        row = {c: prev[c] for c in _ODDS_COLS}
        row.update(
            {
                "status": TOMBSTONE_STATUS,
                "price_american": None,
                "price_decimal": None,
                "limit_amount": None,
                "captured_at": captured_at,
            }
        )
        conn.execute(
            f"INSERT INTO odds_snapshots ({','.join(_ODDS_COLS)}) "  # noqa: S608
            f"VALUES ({','.join('?' * len(_ODDS_COLS))})",
            tuple(row[c] for c in _ODDS_COLS),
        )
        written += 1
    conn.commit()
    return written


def matches_needing_detail(conn: sqlite3.Connection, limit: int = 100) -> list[int]:
    """Match ids whose summary is stored but whose draft/timeseries detail is not."""
    rows = conn.execute(
        "SELECT match_id FROM matches WHERE detail_fetched_at IS NULL "
        "ORDER BY start_time DESC LIMIT ?",
        (limit,),
    ).fetchall()
    return [r["match_id"] for r in rows]


def summary(conn: sqlite3.Connection) -> dict[str, Any]:
    """Row counts and coverage, for the CLI `status` command."""

    def count(sql: str) -> int:
        return conn.execute(sql).fetchone()[0]

    out = {
        "matches": count("SELECT COUNT(*) FROM matches"),
        "matches_with_detail": count(
            "SELECT COUNT(*) FROM matches WHERE detail_fetched_at IS NOT NULL"
        ),
        "draft_events": count("SELECT COUNT(*) FROM draft_events"),
        "match_players": count("SELECT COUNT(*) FROM match_players"),
        "timeseries_points": count("SELECT COUNT(*) FROM match_timeseries"),
        "odds_snapshots": count("SELECT COUNT(*) FROM odds_snapshots"),
        "odds_lines": count("SELECT COUNT(DISTINCT line_key) FROM odds_snapshots"),
        "odds_events": count("SELECT COUNT(DISTINCT event_id) FROM odds_snapshots"),
    }
    span = conn.execute("SELECT MIN(captured_at), MAX(captured_at) FROM odds_snapshots").fetchone()
    out["odds_first_capture"] = span[0]
    out["odds_last_capture"] = span[1]
    return out


def dump_json(obj: Any) -> str:
    return json.dumps(obj, indent=2, sort_keys=True, default=str)
