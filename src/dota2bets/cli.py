"""Command line entry point.

dota2bets backfill --max-matches 500     # pro match summaries
dota2bets detail --limit 100             # drafts, players, per-minute series
dota2bets record-odds                    # long-running line recorder
dota2bets resolve                        # join odds events to teams and series
dota2bets eval                           # closing lines, de-vig, CLV/Brier
dota2bets backtest                       # walk-forward score of the ratings model
dota2bets predict --out preds.jsonl      # pre-match predictions for a live event
dota2bets status                         # what is in the database
"""

from __future__ import annotations

import argparse
import logging
import signal
import sqlite3
import sys
import time
from pathlib import Path
from types import FrameType

from . import aliases, archive, backtest, evaluation, ratings, storage
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
    cursor = args.before_match_id
    if cursor is None and args.resume:
        cursor = storage.oldest_match_id(conn)
        print(
            f"Resuming below match {cursor}." if cursor else "Nothing stored yet; starting fresh."
        )
    with OpenDotaClient(delay_s=args.delay) as client:
        batch = []
        for m in client.iter_pro_matches(max_matches=args.max_matches, before_match_id=cursor):
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
    ok = failed = 0
    with OpenDotaClient(delay_s=args.delay) as client:
        eta = len(match_ids) * client.delay_s
        print(f"Fetching detail for {len(match_ids)} matches (~{eta:.0f}s)...")
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


def cmd_eval(args: argparse.Namespace) -> int:
    """Closing lines and, given predictions, the CLV and Brier they earned."""
    conn = storage.connect_ro(args.db)
    resolver = aliases.AliasResolver.load(args.aliases)

    if not args.predictions:
        series = [
            r[0]
            for r in conn.execute(
                "SELECT DISTINCT series_id FROM event_series_map "
                "WHERE source = ? AND series_id IS NOT NULL ORDER BY series_id",
                (args.source,),
            )
        ]
        print(f"{len(series)} resolved series; closing {args.market} probs ({args.method} de-vig)")
        shown = 0
        for series_id in series:
            for period in (0, 1, 2, 3):
                line = evaluation.closing_probs(
                    conn,
                    args.source,
                    series_id,
                    period,
                    market_type=args.market,
                    method=args.method,
                    before=None,
                )
                if line is None:
                    continue
                shown += 1
                probs = "  ".join(
                    f"{name} {p:.3f} @{line.prices[name]:.2f}"
                    for name, p in sorted(line.selections.items(), key=lambda kv: -kv[1])
                )
                flag = "  [stale cutoff]" if line.stale_cutoff else ""
                print(
                    f"  {series_id} p{period}  vig {line.overround - 1:+.3f}  {probs}"
                    f"  ({time.strftime('%m-%d %H:%M', time.localtime(line.captured_at))}){flag}"
                )
        if not shown:
            print("  no closing lines yet (needs a played map and a resolved event)")
        return 0

    predictions = evaluation.load_predictions(args.predictions)
    report = evaluation.clv_report(
        conn,
        predictions,
        source=args.source,
        method=args.method,
        resolver=resolver,
        before_draft_s=args.before_draft,
    )
    if report.rows:
        header = (
            f"{'series':>9} {'p':>2} {'selection':<16} {'model':>6} "
            f"{'close':>6} {'edge':>7} {'CLV':>7} {'win':>3}"
        )
        print(header)
        print("-" * len(header))
        for r in report.rows:
            clv = f"{r.clv:+.3f}" if r.clv is not None else "     -"
            print(
                f"{r.prediction.series_id:>9} {r.prediction.period:>2} "
                f"{r.prediction.selection:<16} {r.prediction.prob:>6.3f} "
                f"{r.closing_prob:>6.3f} {r.edge_at_close:>+7.3f} {clv:>7} "
                f"{r.outcome:>3}"
            )
    print(f"\nscored {report.n_scored} of {report.n_scored + len(report.skipped)} predictions")
    if report.n_scored:
        print(f"  model Brier      {report.model_brier:.4f}")
        print(f"  closing Brier    {report.closing_brier:.4f}   <- the bar to beat")
        print(f"  mean edge close  {report.mean_edge_at_close:+.4f}")
        if report.mean_clv is not None:
            print(f"  mean CLV         {report.mean_clv:+.4f}")
    if report.skipped:
        print("\nskipped:")
        counts: dict[str, int] = {}
        for _, reason in report.skipped:
            counts[reason] = counts.get(reason, 0) + 1
        for reason, n in sorted(counts.items(), key=lambda kv: -kv[1]):
            print(f"  {n:>4}  {reason}")
    return 0 if report.n_scored else 1


def _config_from_args(args: argparse.Namespace) -> ratings.Glicko2Config:
    return ratings.Glicko2Config(
        tau=args.tau,
        idle_period_s=None if args.idle_days is None else args.idle_days * 86400.0,
        initial_rd=args.initial_rd,
    )


def _holdout_start(conn: sqlite3.Connection, league: str) -> int | None:
    """First horn of the target event -- the walk-forward must never cross it."""
    row = conn.execute(
        "SELECT MIN(start_time) FROM matches WHERE league_name = ?", (league,)
    ).fetchone()
    return int(row[0]) if row and row[0] is not None else None


def _print_scorecard(label: str, card: backtest.ScoreCard) -> None:
    if not card.n:
        print(f"  {label:<12} no scored maps")
        return
    print(
        f"  {label:<12} n={card.n:<6} Brier {card.brier:.4f}  "
        f"log-loss {card.log_loss:.4f}  (coin flip {card.baseline_brier:.4f})"
    )


def cmd_backtest(args: argparse.Namespace) -> int:
    """Walk-forward the ratings model over history, bounded before the target event."""
    conn = storage.connect_ro(args.db)
    burn_in = int(time.mktime(time.strptime(args.burn_in, "%Y-%m-%d")))
    end = _holdout_start(conn, args.league)
    if end is None:
        log.warning("no matches for league %r; scoring the whole history", args.league)
    else:
        print(f"holdout: nothing at or after {time.strftime('%Y-%m-%d', time.localtime(end))}")

    score_leagues = None if args.score == "all" else backtest.elite_league_ids(conn)
    scope = "all leagues" if score_leagues is None else f"{len(score_leagues)} top-tier leagues"
    print(f"scoring on {scope}, teams with >= {args.min_games} prior maps")

    if args.tune:
        grid = backtest.default_grid()
        print(f"tuning {len(grid)} configs on maps from {args.burn_in}, ranked by log-loss")
        results = backtest.tune(
            conn,
            grid,
            start_scoring=burn_in,
            end=end,
            score_leagues=score_leagues,
            min_games=args.min_games,
        )
        print(f"{'tau':>5} {'idle_d':>7} {'rd0':>5} {'n':>7} {'Brier':>8} {'log-loss':>9}")
        for config, card in results:
            idle = "-" if config.idle_period_s is None else f"{config.idle_period_s / 86400:.0f}"
            print(
                f"{config.tau:>5.1f} {idle:>7} {config.initial_rd:>5.0f} {card.n:>7} "
                f"{card.brier:>8.4f} {card.log_loss:>9.4f}"
            )
        best = results[0][0]
        print(
            f"\nbest: tau={best.tau} idle_days="
            f"{'none' if best.idle_period_s is None else best.idle_period_s / 86400:.0f}"
            f" initial_rd={best.initial_rd:.0f}"
        )
        return 0

    card = backtest.walk_forward(
        conn,
        _config_from_args(args),
        start_scoring=burn_in,
        end=end,
        score_leagues=score_leagues,
        min_games=args.min_games,
    )
    print(f"walk-forward from {args.burn_in} (predict-then-update, no lookahead)")
    _print_scorecard("history", card)
    return 0 if card.n else 1


def cmd_predict(args: argparse.Namespace) -> int:
    """Freeze ratings before each series of an event and emit eval-ready predictions."""
    conn = storage.connect_ro(args.db)
    league_id = args.league_id or backtest.league_id_for(conn, args.league)
    if league_id is None:
        print(f"no matches found for league {args.league!r}")
        return 1
    config = _config_from_args(args)

    report = backtest.match_report(conn, league_id, config, source=args.source)
    print(f"league {league_id} ({args.league}) -- all decided maps, outcome-only")
    _print_scorecard("overall", report.overall)
    for day, card in report.by_day:
        _print_scorecard(day, card)

    preds = backtest.event_predictions(conn, league_id, config, source=args.source)
    if args.out:
        n = backtest.write_predictions(args.out, preds)
        print(f"\nwrote {n} predictions to {args.out}")
        print(f"  next: dota2bets eval --predictions {args.out}   <- the bar is closing Brier")
    return 0


def _add_model_args(p: argparse.ArgumentParser) -> None:
    defaults = ratings.Glicko2Config()
    p.add_argument("--tau", type=float, default=defaults.tau)
    p.add_argument(
        "--idle-days",
        type=float,
        default=None if defaults.idle_period_s is None else defaults.idle_period_s / 86400,
        help="days of idleness that inflate RD by one rating period (omit to disable)",
    )
    p.add_argument("--initial-rd", type=float, default=defaults.initial_rd)
    p.add_argument("--league", default=backtest.DEFAULT_LEAGUE_NAME, help="exact league_name")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="dota2bets", description=__doc__)
    parser.add_argument("--db", default=str(storage.DEFAULT_DB_PATH), help="SQLite path")
    parser.add_argument("-v", "--verbose", action="store_true")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("backfill", help="store pro match summaries from OpenDota")
    p.add_argument("--max-matches", type=int, default=500)
    p.add_argument("--league-id", type=int, default=None, help="restrict to one league")
    p.add_argument(
        "--resume",
        action="store_true",
        help="continue below the oldest stored match instead of re-walking from newest",
    )
    p.add_argument("--before-match-id", type=int, default=None, help="explicit paging cursor")
    p.add_argument("--delay", type=float, default=None, help="seconds between calls")
    p.set_defaults(func=cmd_backfill)

    p = sub.add_parser("detail", help="fetch drafts/players/series for stored matches")
    p.add_argument("--limit", type=int, default=100)
    p.add_argument("--delay", type=float, default=None, help="seconds between calls")
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

    p = sub.add_parser("eval", help="closing lines, de-vig, and CLV/Brier for predictions")
    p.add_argument("--predictions", default=None, help="JSONL of model predictions")
    p.add_argument("--source", default="pinnacle")
    p.add_argument("--market", default="moneyline")
    p.add_argument("--method", choices=["shin", "proportional"], default="shin")
    p.add_argument("--aliases", default=None, help="alias table (default: packaged aliases.yaml)")
    p.add_argument(
        "--before-draft",
        type=int,
        default=None,
        help="seconds before a map's horn to cut the closing line at (pre-draft evaluation)",
    )
    p.set_defaults(func=cmd_eval)

    p = sub.add_parser("backtest", help="walk-forward score of the ratings model")
    _add_model_args(p)
    p.add_argument("--tune", action="store_true", help="grid search, ranked by log-loss")
    p.add_argument(
        "--score",
        choices=["elite", "all"],
        default="elite",
        help="which leagues to measure on; all rows train either way",
    )
    p.add_argument(
        "--min-games",
        type=int,
        default=20,
        help="only score matches where both teams have at least this many prior maps",
    )
    p.add_argument("--burn-in", default="2024-10-01", help="start scoring after this date")
    p.set_defaults(func=cmd_backtest)

    p = sub.add_parser("predict", help="pre-match predictions for a league, for eval")
    _add_model_args(p)
    p.add_argument("--league-id", type=int, default=None, help="overrides --league")
    p.add_argument("--source", default="pinnacle")
    p.add_argument("--out", default=None, help="write predictions JSONL here")
    p.set_defaults(func=cmd_predict)

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
