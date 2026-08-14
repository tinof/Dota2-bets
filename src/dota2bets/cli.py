"""Command line entry point.

dota2bets backfill --max-matches 500     # pro match summaries
dota2bets detail --limit 100             # drafts, players, per-minute series
dota2bets record-odds                    # long-running line recorder
dota2bets resolve                        # join odds events to teams and series
dota2bets status                         # what is in the database
"""

from __future__ import annotations

import argparse
import logging
import signal
import sys
import time
from pathlib import Path
from types import FrameType

from . import aliases, archive, storage
from .odds import build_fetchers
from .opendota import OpenDotaClient, parse_match_detail, parse_match_summary

log = logging.getLogger("dota2bets")

_stop = False


def _handle_signal(signum: int, frame: FrameType | None) -> None:
    global _stop
    _stop = True
    print("\nStopping after current cycle...", flush=True)


def cmd_backfill(args: argparse.Namespace) -> int:
    """Page through /proMatches and store match summaries."""
    conn = storage.connect(args.db)
    stored = 0
    with OpenDotaClient() as client:
        batch = []
        for m in client.iter_pro_matches(max_matches=args.max_matches):
            if args.league_id and m.get("leagueid") != args.league_id:
                continue
            batch.append(parse_match_summary(m))
            if len(batch) >= 100:
                stored += storage.upsert_matches(conn, batch)
                conn.commit()
                print(f"  stored {stored} match summaries...", flush=True)
                batch = []
        if batch:
            stored += storage.upsert_matches(conn, batch)
    conn.commit()
    print(f"Stored/updated {stored} match summaries.")
    return 0


def cmd_detail(args: argparse.Namespace) -> int:
    """Fetch full detail (draft, players, gold/xp series) for matches missing it."""
    conn = storage.connect(args.db)
    match_ids = storage.matches_needing_detail(conn, limit=args.limit)
    if not match_ids:
        print("No matches awaiting detail. Run `backfill` first.")
        return 0
    print(f"Fetching detail for {len(match_ids)} matches (~{args.limit * 1.2:.0f}s)...")
    ok = failed = 0
    with OpenDotaClient() as client:
        for i, match_id in enumerate(match_ids, 1):
            if _stop:
                break
            try:
                parsed = parse_match_detail(client.match(match_id))
            except Exception as exc:  # noqa: BLE001 - one bad match must not stop the run
                log.warning("match %s failed: %s", match_id, exc)
                failed += 1
                continue
            storage.upsert_matches(conn, [parsed["match"]])
            storage.upsert_draft_events(conn, parsed["draft_events"])
            storage.upsert_match_players(conn, parsed["players"])
            storage.upsert_timeseries(conn, parsed["timeseries"])
            storage.upsert_teams(conn, parsed["teams"])
            for r in parsed["rosters"]:
                storage.record_roster(
                    conn, r["team_id"], r["account_id"], r["player_name"], r["observed_at"]
                )
            conn.commit()
            ok += 1
            if i % 25 == 0:
                print(f"  {i}/{len(match_ids)}", flush=True)
    print(f"Detail stored for {ok} matches ({failed} failed).")
    return 0


def cmd_record_odds(args: argparse.Namespace) -> int:
    """Poll odds sources on a loop, writing a snapshot whenever a line moves."""
    conn = storage.connect(args.db)
    fetchers = build_fetchers(args.sources)
    if not fetchers:
        print("No usable odds sources configured.")
        return 1

    signal.signal(signal.SIGINT, _handle_signal)
    signal.signal(signal.SIGTERM, _handle_signal)

    print(
        f"Recording {', '.join(f.name for f in fetchers)} every {args.interval}s "
        f"into {args.db} (Ctrl-C to stop)."
    )
    cycles = 0
    last_archived: dict[str, int] = {}
    try:
        while not _stop:
            captured_at = int(time.time())
            total_written = total_skipped = total_gone = 0
            any_live = False
            for fetcher in fetchers:
                try:
                    quotes, raw = fetcher.fetch_with_raw()
                except Exception as exc:  # noqa: BLE001 - keep the loop alive
                    log.warning("%s fetch failed: %s", fetcher.name, exc)
                    continue
                written, skipped = storage.insert_odds_snapshots(conn, quotes, captured_at)
                # Only tombstone after a successful fetch; a failed poll means we know
                # nothing about those lines, which is not the same as them being pulled.
                gone = storage.write_tombstones(
                    conn, fetcher.name, (q["line_key"] for q in quotes), captured_at
                )
                # Archive payloads that changed something, plus an hourly heartbeat.
                # Archiving every poll would cost gigabytes across an event for copies
                # of a market that never moved.
                due = captured_at - last_archived.get(fetcher.name, 0) >= args.archive_heartbeat
                if not args.no_archive and (written or gone or due):
                    try:
                        archive.write_payload(args.archive_root, fetcher.name, captured_at, raw)
                        last_archived[fetcher.name] = captured_at
                    except OSError as exc:
                        log.warning("archiving %s failed: %s", fetcher.name, exc)
                total_written += written
                total_skipped += skipped
                total_gone += gone
                any_live |= any(q.get("is_live") for q in quotes)
            cycles += 1
            stamp = time.strftime("%H:%M:%S", time.localtime(captured_at))
            print(
                f"[{stamp}] cycle {cycles}: {total_written} new, {total_skipped} unchanged, "
                f"{total_gone} gone{' [LIVE]' if any_live else ''}",
                flush=True,
            )
            if args.once:
                break
            # Live markets move in seconds; pre-match lines drift over minutes.
            delay = args.live_interval if any_live else args.interval
            for _ in range(delay):
                if _stop:
                    break
                time.sleep(1)
    finally:
        for fetcher in fetchers:
            fetcher.close()
        conn.close()
    return 0


def cmd_resolve(args: argparse.Namespace) -> int:
    """Join recorded odds events to the series they price."""
    conn = storage.connect(args.db)
    resolver = aliases.AliasResolver.load(args.aliases)
    links = aliases.parent_links_from_archive(args.archive_root, args.source)
    print(f"{len(resolver.teams)} teams in the alias table, {len(links)} parent links archived.")
    report = aliases.resolve_events(
        conn,
        resolver,
        parent_links=links,
        source=args.source,
        tolerance_s=args.tolerance,
        dry_run=args.dry_run,
    )
    print(report.format())
    if args.dry_run:
        print("\n(dry run: event_series_map not written)")
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    conn = storage.connect(args.db)
    s = storage.summary(conn)
    for key in ("odds_first_capture", "odds_last_capture"):
        if s[key]:
            s[key] = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(s[key]))
    print(f"Database: {Path(args.db).resolve()}")
    for k, v in s.items():
        print(f"  {k:22} {v}")
    rows = conn.execute(
        "SELECT league, market_type, COUNT(*) n, COUNT(DISTINCT event_id) events "
        "FROM odds_snapshots GROUP BY league, market_type ORDER BY n DESC LIMIT 10"
    ).fetchall()
    if rows:
        print("\nOdds coverage:")
        for r in rows:
            print(
                f"  {r['league']:32} {r['market_type']:12} {r['n']:6} snaps / {r['events']} events"
            )
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="dota2bets", description=__doc__)
    parser.add_argument("--db", default=str(storage.DEFAULT_DB_PATH), help="SQLite path")
    parser.add_argument("-v", "--verbose", action="store_true")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("backfill", help="store pro match summaries from OpenDota")
    p.add_argument("--max-matches", type=int, default=500)
    p.add_argument("--league-id", type=int, default=None, help="restrict to one league")
    p.set_defaults(func=cmd_backfill)

    p = sub.add_parser("detail", help="fetch drafts/players/series for stored matches")
    p.add_argument("--limit", type=int, default=100)
    p.set_defaults(func=cmd_detail)

    p = sub.add_parser("record-odds", help="poll odds sources and record line movements")
    p.add_argument("--sources", nargs="+", default=["pinnacle"])
    p.add_argument("--interval", type=int, default=60, help="seconds between polls")
    p.add_argument(
        "--live-interval", type=int, default=20, help="seconds between polls while a game is live"
    )
    p.add_argument("--archive-root", default=str(archive.DEFAULT_ARCHIVE_ROOT))
    p.add_argument("--no-archive", action="store_true", help="skip raw payload archiving")
    p.add_argument(
        "--archive-heartbeat",
        type=int,
        default=3600,
        help="archive an unchanged payload at least this often (seconds)",
    )
    p.add_argument("--once", action="store_true", help="single cycle then exit")
    p.set_defaults(func=cmd_record_odds)

    p = sub.add_parser("resolve", help="map odds events to OpenDota teams and series")
    p.add_argument("--source", default="pinnacle")
    p.add_argument("--archive-root", default=str(archive.DEFAULT_ARCHIVE_ROOT))
    p.add_argument("--aliases", default=None, help="alias table (default: packaged aliases.yaml)")
    p.add_argument(
        "--tolerance",
        type=int,
        default=aliases.DEFAULT_TOLERANCE_S,
        help="max seconds between an event's scheduled start and a series' first map",
    )
    p.add_argument("--dry-run", action="store_true", help="report only, write nothing")
    p.set_defaults(func=cmd_resolve)

    p = sub.add_parser("status", help="show database contents")
    p.set_defaults(func=cmd_status)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    if not args.verbose:
        # One request line per poll per endpoint would dominate a long recorder log.
        logging.getLogger("httpx").setLevel(logging.WARNING)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
