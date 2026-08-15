"""Command line entry point.

dota2bets backfill --max-matches 500     # pro match summaries
dota2bets detail --limit 100             # drafts, players, per-minute series
dota2bets bulkfill                       # bulk import via Explorer SQL
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

from . import aliases, archive, backtest, draft, evaluation, heroes, ratings, storage
from .odds import build_fetchers
from .opendota import (
    OpenDotaClient,
    parse_explorer_rows,
    parse_match_detail,
    parse_match_summary,
)

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


def cmd_bulkfill(args: argparse.Namespace) -> int:
    """Import match details in bulk from OpenDota Explorer endpoint."""
    conn = storage.connect(args.db)
    signal.signal(signal.SIGINT, _handle_signal)
    signal.signal(signal.SIGTERM, _handle_signal)

    since_ts: int | None = None
    if args.since:
        since_ts = storage.parse_ts(args.since)

    if since_ts is not None:
        start_min = since_ts
    else:
        row = conn.execute(
            "SELECT MIN(start_time) FROM matches WHERE detail_fetched_at IS NULL"
        ).fetchone()
        start_min = int(row[0]) if row and row[0] is not None else None

    if start_min is None:
        print("No matches awaiting detail. Run `backfill` first.")
        return 0

    row_max = conn.execute(
        "SELECT MAX(start_time) FROM matches WHERE detail_fetched_at IS NULL"
    ).fetchone()
    start_max = int(row_max[0]) if row_max and row_max[0] is not None else int(time.time())
    total_end = start_max + 1

    slice_days = args.slice_days or 14
    base_slice_s = slice_days * 86400

    print(
        f"Bulkfill from {time.strftime('%Y-%m-%d', time.localtime(start_min))} "
        f"to {time.strftime('%Y-%m-%d', time.localtime(total_end))} "
        f"({slice_days}-day slices)..."
    )

    total_enriched = 0
    total_skipped = 0
    failed_slices = 0

    with OpenDotaClient(delay_s=args.delay) as client:
        patch_map = client.patch_index()
        patch_timeline = client.patch_timeline()
        lo = start_min
        while lo < total_end and not _stop:
            slice_s = base_slice_s
            success = False
            while slice_s >= 86400 and not _stop:
                hi = min(lo + slice_s, total_end)
                date_str = (
                    f"[{time.strftime('%Y-%m-%d', time.localtime(lo))} -> "
                    f"{time.strftime('%Y-%m-%d', time.localtime(hi))}]"
                )
                try:
                    if args.with_timeseries:
                        match_sql = (
                            "SELECT m.match_id, m.leagueid, m.series_id, m.series_type, "
                            "m.start_time, m.duration, m.radiant_team_id, "
                            "m.radiant_team_name AS radiant_name, m.dire_team_id, "
                            "m.dire_team_name AS dire_name, m.radiant_win, m.radiant_score, "
                            "m.dire_score, m.radiant_gold_adv, m.radiant_xp_adv, mp.patch "
                            "FROM matches m LEFT JOIN match_patch mp USING(match_id) "
                            f"WHERE m.start_time >= {lo} AND m.start_time < {hi}"
                        )
                    else:
                        match_sql = (
                            "SELECT m.match_id, m.leagueid, m.series_id, m.series_type, "
                            "m.start_time, m.duration, m.radiant_team_id, "
                            "m.radiant_team_name AS radiant_name, m.dire_team_id, "
                            "m.dire_team_name AS dire_name, m.radiant_win, m.radiant_score, "
                            "m.dire_score, mp.patch "
                            "FROM matches m LEFT JOIN match_patch mp USING(match_id) "
                            f"WHERE m.start_time >= {lo} AND m.start_time < {hi}"
                        )
                    raw_matches = client.explorer(match_sql)

                    pb_sql = (
                        "SELECT pb.match_id, pb.is_pick, pb.hero_id, pb.team, pb.ord "
                        "FROM picks_bans pb JOIN matches m USING(match_id) "
                        f"WHERE m.start_time >= {lo} AND m.start_time < {hi}"
                    )
                    raw_pb = client.explorer(pb_sql)

                    pm_sql = (
                        "SELECT pm.match_id, pm.player_slot, pm.account_id, pm.hero_id, "
                        "pm.kills, pm.deaths, pm.assists, pm.gold_per_min, pm.xp_per_min, "
                        "pm.lane_role "
                        "FROM player_matches pm JOIN matches m USING(match_id) "
                        f"WHERE m.start_time >= {lo} AND m.start_time < {hi}"
                    )
                    raw_pm = client.explorer(pm_sql)

                    known_rows = conn.execute(
                        "SELECT match_id FROM matches WHERE start_time >= ? AND start_time < ?",
                        (lo, hi),
                    ).fetchall()
                    known_ids = {r[0] for r in known_rows}

                    matches_filtered = [m for m in raw_matches if m.get("match_id") in known_ids]
                    skipped = len(raw_matches) - len(matches_filtered)
                    total_skipped += skipped

                    pb_filtered = [pb for pb in raw_pb if pb.get("match_id") in known_ids]
                    pm_filtered = [pm for pm in raw_pm if pm.get("match_id") in known_ids]

                    parsed = parse_explorer_rows(
                        matches_filtered,
                        pb_filtered,
                        pm_filtered,
                        patch_map,
                        patch_timeline=patch_timeline,
                    )

                    storage.upsert_matches(conn, parsed["match"])
                    storage.upsert_draft_events(conn, parsed["draft_events"])
                    storage.upsert_match_players(conn, parsed["players"])
                    if args.with_timeseries and parsed["timeseries"]:
                        storage.upsert_timeseries(conn, parsed["timeseries"])
                    storage.upsert_teams(conn, parsed["teams"])
                    for r in parsed["rosters"]:
                        storage.record_roster(
                            conn,
                            r["team_id"],
                            r["account_id"],
                            r["player_name"],
                            r["observed_at"],
                        )
                    conn.commit()

                    enriched_count = sum(
                        1 for m in parsed["match"] if m.get("detail_fetched_at") is not None
                    )
                    total_enriched += enriched_count
                    print(
                        f"  {date_str} {len(matches_filtered)} matches ({skipped} skipped), "
                        f"{len(pm_filtered)} players, {len(pb_filtered)} draft events "
                        f"-> {enriched_count} enriched",
                        flush=True,
                    )
                    lo = hi
                    success = True
                    break
                except Exception as exc:  # noqa: BLE001
                    if slice_s > 86400:
                        slice_s = slice_s // 2
                        log.warning(
                            "Slice %s failed (%s); retrying with halved width (%.1f days)",
                            date_str,
                            exc,
                            slice_s / 86400,
                        )
                    else:
                        log.error("Slice %s failed repeatedly (%s); skipping", date_str, exc)
                        failed_slices += 1
                        lo = hi
                        break
            if not success and slice_s < 86400:
                lo += base_slice_s

    cov = storage.enrichment_coverage(conn)
    print("\nBulkfill complete:")
    print(f"  Matches enriched this run: {total_enriched}")
    print(f"  Untracked matches skipped: {total_skipped}")
    print(f"  Failed slices:             {failed_slices}")
    print(f"  Total matches in db:       {cov['total']}")
    pct_p = (cov["with_patch"] / cov["total"] * 100) if cov["total"] else 0
    pct_pl = (cov["with_players"] / cov["total"] * 100) if cov["total"] else 0
    pct_d = (cov["with_draft"] / cov["total"] * 100) if cov["total"] else 0
    pct_det = (cov["with_detail"] / cov["total"] * 100) if cov["total"] else 0
    print(f"  With patch:                {cov['with_patch']} ({pct_p:.1f}%)")
    print(f"  With 10 players:           {cov['with_players']} ({pct_pl:.1f}%)")
    print(f"  With draft:                {cov['with_draft']} ({pct_d:.1f}%)")
    print(f"  Fully enriched:            {cov['with_detail']} ({pct_det:.1f}%)")
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
        patch_rd_boost=args.patch_boost,
        roster_rd_boost=args.roster_boost,
    )


def _draft_config_from_args(args: argparse.Namespace) -> draft.DraftConfig:
    return draft.DraftConfig(
        half_life_days=args.half_life_days,
        k_hero=args.k_hero,
        k_team_hero=args.k_team_hero,
        k_pair=args.k_pair,
        learning_rate=args.learning_rate,
        disabled=args.disable_draft,
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
    fb = f"  (fallback: {card.n_fallback})" if card.n_fallback > 0 else ""
    print(
        f"  {label:<12} n={card.n:<6} Brier {card.brier:.4f}  "
        f"log-loss {card.log_loss:.4f}  (coin flip {card.baseline_brier:.4f}){fb}"
    )


def cmd_backtest(args: argparse.Namespace) -> int:
    """Walk-forward the ratings or draft model over history, bounded before target event."""
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

    if args.model == "draft":
        r_cfg = _config_from_args(args)
        if args.tune:
            grid = backtest.default_draft_grid()
            print(
                f"tuning {len(grid)} draft configs on maps from {args.burn_in}, ranked by log-loss"
            )
            results = backtest.tune_draft(
                conn,
                grid,
                ratings_config=r_cfg,
                start_scoring=burn_in,
                end=end,
                score_leagues=score_leagues,
                min_games=args.min_games,
            )
            print(
                f"{'half_life':>10} {'k_pair':>7} {'lr':>6} {'n':>7} "
                f"{'fallback':>9} {'Brier':>8} {'log-loss':>9}"
            )
            for config, card in results:
                print(
                    f"{config.half_life_days:>9.0f}d {config.k_pair:>7.0f} "
                    f"{config.learning_rate:>6.2f} {card.n:>7} {card.n_fallback:>9} "
                    f"{card.brier:>8.4f} {card.log_loss:>9.4f}"
                )
            best = results[0][0]
            print(
                f"\nbest: half_life={best.half_life_days:.0f}d "
                f"k_pair={best.k_pair:.0f} lr={best.learning_rate}"
            )
            return 0

        d_cfg = _draft_config_from_args(args)
        card = backtest.walk_forward_draft(
            conn,
            draft_config=d_cfg,
            ratings_config=r_cfg,
            start_scoring=burn_in,
            end=end,
            score_leagues=score_leagues,
            min_games=args.min_games,
        )
        print(
            f"walk-forward from {args.burn_in} (predict-then-update, no lookahead)\n"
            f"  model: draft (half_life={d_cfg.half_life_days:.0f}d, k_hero={d_cfg.k_hero:.0f}, "
            f"k_pair={d_cfg.k_pair:.0f}, lr={d_cfg.learning_rate})"
        )
        _print_scorecard("history", card)
        return 0 if card.n else 1

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
        print(f"{'patch':>7} {'roster':>7} {'n':>7} {'Brier':>8} {'log-loss':>9}")
        for config, card in results:
            patch = "-" if config.patch_rd_boost is None else f"{config.patch_rd_boost:.0f}"
            roster = "-" if config.roster_rd_boost is None else f"{config.roster_rd_boost:.0f}"
            print(
                f"{patch:>7} {roster:>7} {card.n:>7} "
                f"{card.brier:>8.4f} {card.log_loss:>9.4f}"
            )
        best = results[0][0]
        patch_s = "none" if best.patch_rd_boost is None else f"{best.patch_rd_boost:.0f}"
        roster_s = "none" if best.roster_rd_boost is None else f"{best.roster_rd_boost:.0f}"
        print(f"\nbest: patch_boost={patch_s} roster_boost={roster_s}")
        return 0

    cfg = _config_from_args(args)
    card = backtest.walk_forward(
        conn,
        cfg,
        start_scoring=burn_in,
        end=end,
        score_leagues=score_leagues,
        min_games=args.min_games,
    )
    patch_s = "none" if cfg.patch_rd_boost is None else f"{cfg.patch_rd_boost:.0f}"
    roster_s = "none" if cfg.roster_rd_boost is None else f"{cfg.roster_rd_boost:.0f}"
    print(
        f"walk-forward from {args.burn_in} (predict-then-update, no lookahead)\n"
        f"  features: patch_boost={patch_s} roster_boost={roster_s}"
    )
    _print_scorecard("history", card)
    return 0 if card.n else 1


def cmd_predict(args: argparse.Namespace) -> int:
    """Freeze ratings before each series of an event and emit eval-ready predictions."""
    conn = storage.connect_ro(args.db)
    league_id = args.league_id or backtest.league_id_for(conn, args.league)
    if league_id is None:
        print(f"no matches found for league {args.league!r}")
        return 1

    r_cfg = _config_from_args(args)
    if args.model == "draft":
        d_cfg = _draft_config_from_args(args)
        result = backtest.event_predictions_draft(
            conn, league_id, draft_config=d_cfg, ratings_config=r_cfg, source=args.source
        )
        preds = result.predictions
        print(f"league {league_id} ({args.league}) -- per-map draft predictions")
        print(
            f"  model: draft (half_life={d_cfg.half_life_days:.0f}d, k_hero={d_cfg.k_hero:.0f}, "
            f"k_pair={d_cfg.k_pair:.0f}, lr={d_cfg.learning_rate})"
        )
        print(
            f"  population: {result.n_maps} priced maps "
            f"({result.n_fallback} fell back to ratings, no usable draft)"
        )
        if args.out:
            n = backtest.write_predictions(args.out, preds)
            print(f"\nwrote {n} predictions to {args.out}")
            print(f"  next: dota2bets eval --predictions {args.out}   <- the bar is closing Brier")
        return 0

    patch_s = "none" if r_cfg.patch_rd_boost is None else f"{r_cfg.patch_rd_boost:.0f}"
    roster_s = "none" if r_cfg.roster_rd_boost is None else f"{r_cfg.roster_rd_boost:.0f}"

    report = backtest.match_report(conn, league_id, r_cfg, source=args.source)
    print(f"league {league_id} ({args.league}) -- all decided maps, outcome-only")
    print(f"  features: patch_boost={patch_s} roster_boost={roster_s}")
    _print_scorecard("overall", report.overall)
    for day, card in report.by_day:
        _print_scorecard(day, card)

    preds = backtest.event_predictions(conn, league_id, r_cfg, source=args.source)
    if args.out:
        n = backtest.write_predictions(args.out, preds)
def cmd_bet(args: argparse.Namespace) -> int:
    """Price a live draft against the open market line and record a paper trade."""
    # Read-only: this runs while the recorder is polling, and it never writes the DB.
    conn = storage.connect_ro(args.db)
    resolver = aliases.AliasResolver.load(args.aliases)

    if getattr(args, "settle", False):
        log_path = Path(args.trades_log)
        if not log_path.exists():
            print(f"No trades log found at {log_path}")
            return 0
        trades = evaluation.load_predictions(str(log_path))
        if not trades:
            print(f"No trades in {log_path}")
            return 0
        report = evaluation.settle_paper_trades(
            conn, trades, source=args.source, method=args.method, resolver=resolver
        )
        print(
            f"Settled {report.n_settled} trades "
            f"({report.n_pending} pending, {report.n_unresolved} unresolved):"
        )
        header = (
            f"{'series':>9} {'p':>2} {'selection':<16} {'stake':>7} "
            f"{'price':>6} {'CLV':>7} {'status':<9} {'P&L':>8}"
        )
        print(header)
        print("-" * len(header))
        for t in report.trades:
            p = t.prediction
            stake_s = f"${p.stake:.2f}" if p.stake is not None else "-"
            price_s = f"{p.price_taken:.2f}" if p.price_taken is not None else "-"
            clv_s = f"{t.clv:+.3f}" if t.clv is not None else "-"
            pnl_s = f"${t.pnl:+.2f}" if t.pnl is not None else "-"
            print(
                f"{p.series_id:>9} {p.period:>2} {p.selection:<16} "
                f"{stake_s:>7} {price_s:>6} {clv_s:>7} {t.status:<9} {pnl_s:>8}"
            )
        print(f"\nTotal Staked: ${report.total_staked:.2f}")
        print(f"Realised P&L: ${report.realised_pnl:+.2f}")
        if report.mean_clv is not None:
            print(f"Mean CLV:     {report.mean_clv:+.4f}")
        if report.mean_edge_at_close is not None:
            print(f"Mean Edge:    {report.mean_edge_at_close:+.4f}")
        return 0

    # 1. Resolve teams
    rad_name_in = getattr(args, "radiant_team", None)
    dire_name_in = getattr(args, "dire_team", None)
    series_id = getattr(args, "series_id", None)

    team_rad: aliases.Team | None = None
    team_dire: aliases.Team | None = None

    if rad_name_in:
        team_rad = resolver.resolve(rad_name_in)
        if team_rad is None:
            print(f"Unrecognised radiant team: {rad_name_in!r}")
            return 1
    if dire_name_in:
        team_dire = resolver.resolve(dire_name_in)
        if team_dire is None:
            print(f"Unrecognised dire team: {dire_name_in!r}")
            return 1

    if series_id is None:
        if team_rad is None or team_dire is None:
            print("Must provide either --series-id or both --radiant-team and --dire-team")
            return 1
        row = conn.execute(
            "SELECT series_id FROM event_series_map "
            "WHERE source = ? AND ((home_team_id = ? AND away_team_id = ?) "
            "   OR (home_team_id = ? AND away_team_id = ?)) "
            "ORDER BY event_id DESC LIMIT 1",
            (args.source, team_rad.team_id, team_dire.team_id, team_dire.team_id, team_rad.team_id),
        ).fetchone()
        if row is None:
            row = conn.execute(
                "SELECT series_id FROM matches "
                "WHERE (radiant_team_id = ? AND dire_team_id = ?) "
                "   OR (radiant_team_id = ? AND dire_team_id = ?) "
                "ORDER BY start_time DESC LIMIT 1",
                (team_rad.team_id, team_dire.team_id, team_dire.team_id, team_rad.team_id),
            ).fetchone()
        if row is None:
            print(f"No series found between {team_rad.name} and {team_dire.name}")
            return 1
        series_id = int(row[0])
    else:
        if team_rad is None or team_dire is None:
            row = conn.execute(
                "SELECT radiant_team_id, dire_team_id, radiant_name, dire_name "
                "FROM matches WHERE series_id = ? LIMIT 1",
                (series_id,),
            ).fetchone()
            if row:
                if team_rad is None and row["radiant_team_id"]:
                    name_query = str(row["radiant_name"] or row["radiant_team_id"])
                    team_rad = resolver.resolve(name_query) or aliases.Team(
                        int(row["radiant_team_id"]), str(row["radiant_name"])
                    )
                if team_dire is None and row["dire_team_id"]:
                    name_query = str(row["dire_name"] or row["dire_team_id"])
                    team_dire = resolver.resolve(name_query) or aliases.Team(
                        int(row["dire_team_id"]), str(row["dire_name"])
                    )

    if team_rad is None or team_dire is None:
        print(f"Could not resolve teams for series {series_id}")
        return 1

    # 2. Resolve heroes
    rad_hero_inputs = getattr(args, "radiant", []) or []
    dire_hero_inputs = getattr(args, "dire", []) or []
    if len(rad_hero_inputs) != 5 or len(dire_hero_inputs) != 5:
        print(
            f"Draft requires exactly 5 radiant and 5 dire heroes "
            f"(got {len(rad_hero_inputs)} radiant, {len(dire_hero_inputs)} dire)"
        )
        return 1

    rad_heroes: list[heroes.Hero] = []
    for h_in in rad_hero_inputs:
        try:
            h = heroes.resolve_hero(h_in)
            rad_heroes.append(h)
        except heroes.UnknownHeroError as exc:
            print(f"Radiant hero error: {exc}")
            return 1

    dire_heroes: list[heroes.Hero] = []
    for h_in in dire_hero_inputs:
        try:
            h = heroes.resolve_hero(h_in)
            dire_heroes.append(h)
        except heroes.UnknownHeroError as exc:
            print(f"Dire hero error: {exc}")
            return 1

    all_hero_ids = [h.hero_id for h in rad_heroes] + [h.hero_id for h in dire_heroes]
    if len(set(all_hero_ids)) != 10:
        dups = [
            h.localized_name for h in rad_heroes + dire_heroes if all_hero_ids.count(h.hero_id) > 1
        ]
        print(f"A draft must name ten distinct heroes: duplicates found {list(set(dups))}")
        return 1

    first_action = None
    if getattr(args, "first_action", "unknown") == "radiant":
        first_action = True
    elif getattr(args, "first_action", "unknown") == "dire":
        first_action = False

    draft_rec = draft.DraftRecord(
        match_id=0,
        radiant_heroes=tuple(h.hero_id for h in rad_heroes),
        dire_heroes=tuple(h.hero_id for h in dire_heroes),
        first_action_radiant=first_action,
    )

    # 3. Replay ratings and draft model to now
    now = int(time.time())
    r_cfg = _config_from_args(args)
    d_cfg = _draft_config_from_args(args)

    book = ratings.RatingBook(r_cfg)
    draft_model = draft.DraftModel(d_cfg)
    drafts_by_match = draft.load_drafts(conn)
    lineups_by_match = (
        ratings.load_lineups(conn) if book.config.roster_rd_boost is not None else None
    )

    for row in ratings.rating_rows(conn, before=now):
        match_id = int(row["match_id"])
        at = int(row["start_time"])
        r_id = int(row["radiant_team_id"])
        d_id = int(row["dire_team_id"])
        patch = int(row["patch"]) if row["patch"] is not None else None
        m_lineups = lineups_by_match.get(match_id) if lineups_by_match else None
        d = drafts_by_match.get(match_id)

        p_g = book.win_prob(r_id, d_id, at, patch=patch, lineups=m_lineups)
        _, _, feats = draft_model.predict(r_id, d_id, at, p_g, d)
        r_win = bool(row["radiant_win"])
        draft_model.update(r_id, d_id, at, r_win, d, features=feats)
        ratings.apply_row(book, row, lineups=m_lineups)

    # 4. Model probability
    p_glicko = book.win_prob(team_rad.team_id, team_dire.team_id, now)
    prob_rad, is_fallback, feats = draft_model.predict(
        team_rad.team_id, team_dire.team_id, now, p_glicko, draft_rec
    )
    prob_dire = 1.0 - prob_rad

    print(f"\nSeries {series_id} Period {args.period}:")
    print(f"  Radiant ({team_rad.name}): {', '.join(h.localized_name for h in rad_heroes)}")
    print(f"  Dire    ({team_dire.name}): {', '.join(h.localized_name for h in dire_heroes)}")
    print(f"  Model win probability: Radiant {prob_rad:.3f} | Dire {prob_dire:.3f}")

    # 5. Fetch current open quote
    event_id = evaluation.closing_event(conn, args.source, series_id)
    if event_id is None:
        print(f"\nNo event found for series {series_id} on {args.source}. No trade recorded.")
        return 0

    # The newest row per line wins REGARDLESS of status: `odds_snapshots` is append-only,
    # so filtering on status='open' first would happily return a stale open row that a
    # later 'closed' (listed, not taking bets) or 'gone' (pulled) row has superseded.
    all_rows = conn.execute(
        "SELECT * FROM odds_snapshots "
        "WHERE source = ? AND event_id = ? AND market_type = 'moneyline' AND period = ? "
        "ORDER BY captured_at DESC, id DESC",
        (args.source, event_id, args.period),
    ).fetchall()

    latest_by_line: dict[str, sqlite3.Row] = {}
    for r in all_rows:
        key = str(r["line_key"])
        if key not in latest_by_line:
            latest_by_line[key] = r

    latest_by_sel: dict[str, sqlite3.Row] = {}
    superseded: dict[str, str] = {}
    for r in latest_by_line.values():
        sel = str(r["selection"])
        is_live = r["is_live"]
        if str(r["status"]) != "open" or (is_live is not None and int(is_live) != 0):
            superseded[sel] = str(r["status"])
            continue
        prev = latest_by_sel.get(sel)
        if prev is None or int(r["captured_at"]) > int(prev["captured_at"]):
            latest_by_sel[sel] = r

    if not latest_by_sel:
        detail = f" (latest rows: {superseded})" if superseded else ""
        print(
            f"\nNo open quote for series {series_id} period {args.period} on "
            f"{args.source}{detail}. No trade recorded."
        )
        return 0

    if len(latest_by_sel) < 2:
        detail = f" (superseded: {superseded})" if superseded else ""
        print(
            f"\nIncomplete open book for series {series_id} period {args.period}{detail}. "
            f"No trade recorded."
        )
        return 0

    # Age is the OLDEST leg: the traded price is only as fresh as the side we bet on, and
    # a book whose two legs were observed hours apart cannot be de-vigged against itself.
    quoted_at = min(int(r["captured_at"]) for r in latest_by_sel.values())
    age_s = now - quoted_at
    stamp_str = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(quoted_at))
    print(f"  Market quote: oldest leg captured {age_s}s ago ({stamp_str})")
    stale = age_s > args.freshness_limit
    if stale:
        print(f"  WARNING: Quote is {age_s}s old (freshness limit is {args.freshness_limit}s)!")

    # Match selections to teams
    selections = list(latest_by_sel.keys())
    prices = [float(latest_by_sel[s]["price_decimal"]) for s in selections]
    devigged_probs = evaluation.devig(prices, method=args.method)
    mkt_map = dict(zip(selections, devigged_probs, strict=True))
    price_map = dict(zip(selections, prices, strict=True))

    def _matches_team(sel: str, team: aliases.Team) -> bool:
        resolved = resolver.resolve(sel)
        return resolved is not None and resolved.team_id == team.team_id

    rad_sel = next((s for s in selections if _matches_team(s, team_rad)), None)
    dire_sel = next((s for s in selections if _matches_team(s, team_dire)), None)

    if not rad_sel or not dire_sel:
        print(
            f"  Could not match quoted selections {selections} "
            f"to teams {team_rad.name} / {team_dire.name}."
        )
        return 0

    mkt_p_rad = mkt_map[rad_sel]
    mkt_p_dire = mkt_map[dire_sel]
    price_rad = price_map[rad_sel]
    price_dire = price_map[dire_sel]

    edge_rad = prob_rad - mkt_p_rad
    edge_dire = prob_dire - mkt_p_dire

    print(
        f"\n  {rad_sel:<16} Price: {price_rad:.2f} | Mkt prob: {mkt_p_rad:.3f} | "
        f"Model: {prob_rad:.3f} | Edge: {edge_rad:+.3f}"
    )
    print(
        f"  {dire_sel:<16} Price: {price_dire:.2f} | Mkt prob: {mkt_p_dire:.3f} | "
        f"Model: {prob_dire:.3f} | Edge: {edge_dire:+.3f}"
    )

    # Determine trade
    chosen_sel: str | None = None
    chosen_prob: float = 0.0
    chosen_price: float = 0.0
    chosen_edge: float = 0.0

    if edge_rad > 0 and edge_rad >= edge_dire:
        chosen_sel, chosen_prob, chosen_price, chosen_edge = (
            rad_sel,
            prob_rad,
            price_rad,
            edge_rad,
        )
    elif edge_dire > 0:
        chosen_sel, chosen_prob, chosen_price, chosen_edge = (
            dire_sel,
            prob_dire,
            price_dire,
            edge_dire,
        )

    if chosen_sel is None or chosen_edge <= 0:
        print("\nNo positive edge found. Suggested stake: $0.00 (no trade)")
        return 0

    b = chosen_price - 1.0
    if b > 0:
        f_kelly = max(0.0, (chosen_prob * chosen_price - 1.0) / b)
    else:
        f_kelly = 0.0
    suggested_stake = min(args.max_stake, round(f_kelly * args.bankroll, 2))

    print(f"\nProposed Trade: {chosen_sel} @ {chosen_price:.2f}")
    print(
        f"  Edge: {chosen_edge:+.3f} | Kelly: {f_kelly:.3f} | "
        f"Suggested Stake: ${suggested_stake:.2f}"
    )

    if suggested_stake <= 0:
        print("Suggested stake is $0.00. No trade recorded.")
        return 0

    if getattr(args, "rehearse", False) or getattr(args, "dry_run", False):
        print("\n[REHEARSAL MODE] Trade log untouched.")
        return 0

    # The staleness gate blocks only the *record*: a price observed long ago was not
    # available to bet, so writing it would corrupt the CLV record this log exists for.
    # Reporting still happens above, so a rehearsal shows the full comparison either way.
    if stale and not args.allow_stale:
        print(
            f"\nRefusing to record a trade on a {age_s}s-old quote "
            f"(limit {args.freshness_limit}s). Pass --allow-stale to override."
        )
        return 0

    pred_rec = evaluation.Prediction(
        series_id=series_id,
        period=args.period,
        selection=chosen_sel,
        prob=chosen_prob,
        market_type="moneyline",
        price_taken=chosen_price,
        placed_at=now,
        stake=suggested_stake,
        quoted_at=quoted_at,
    )
    evaluation.append_prediction(args.trades_log, pred_rec)
    print(f"\nRecorded paper trade to {args.trades_log}")
    return 0


def _add_model_args(p: argparse.ArgumentParser) -> None:
    defaults = ratings.Glicko2Config()
    p.add_argument(
        "--model",
        choices=["ratings", "draft"],
        default="ratings",
        help="model family (default: ratings)",
    )
    p.add_argument("--tau", type=float, default=defaults.tau)
    p.add_argument(
        "--idle-days",
        type=float,
        default=None if defaults.idle_period_s is None else defaults.idle_period_s / 86400,
        help="days of idleness that inflate RD by one rating period (omit to disable)",
    )
    p.add_argument("--initial-rd", type=float, default=defaults.initial_rd)
    p.add_argument(
        "--patch-boost",
        type=float,
        default=defaults.patch_rd_boost,
        help="RD inflation points on first map of a new patch (default: disabled)",
    )
    p.add_argument(
        "--roster-boost",
        type=float,
        default=defaults.roster_rd_boost,
        help="RD inflation points per changed player in lineup (default: disabled)",
    )
    p.add_argument(
        "--half-life-days",
        type=float,
        default=draft.DEFAULT_HALF_LIFE_DAYS,
        help="half-life days for exponential decay of hero statistics (default: 120)",
    )
    p.add_argument(
        "--k-hero",
        type=float,
        default=draft.DEFAULT_K_HERO,
        help="Bayesian shrinkage for hero win rate (default: 20)",
    )
    p.add_argument(
        "--k-team-hero",
        type=float,
        default=draft.DEFAULT_K_TEAM_HERO,
        help="Bayesian shrinkage for team-hero familiarity (default: 10)",
    )
    p.add_argument(
        "--k-pair",
        type=float,
        default=draft.DEFAULT_K_PAIR,
        help="Bayesian shrinkage for synergy/matchup pairs (default: 100)",
    )
    p.add_argument(
        "--learning-rate",
        type=float,
        default=draft.DEFAULT_LEARNING_RATE,
        help="AdaGrad learning rate for draft combiner (default: 0.05)",
    )
    p.add_argument(
        "--disable-draft",
        action="store_true",
        help="disable draft features in draft model (reproduces ratings baseline)",
    )
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

    p = sub.add_parser("bulkfill", help="bulk import drafts/players/patches via Explorer")
    p.add_argument("--db", default=str(storage.DEFAULT_DB_PATH), help="SQLite path")
    p.add_argument("--slice-days", type=int, default=14, help="days per Explorer query slice")
    p.add_argument(
        "--since", default=None, help="start time/date (default: oldest match needing detail)"
    )
    p.add_argument(
        "--with-timeseries", action="store_true", help="import radiant gold/xp series"
    )
    p.add_argument("--delay", type=float, default=None, help="seconds between calls")
    p.set_defaults(func=cmd_bulkfill)

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

    p = sub.add_parser("bet", help="price a draft against live odds and log paper trade")
    _add_model_args(p)
    p.add_argument("--series-id", type=int, default=None, help="series id")
    p.add_argument(
        "--radiant-team", "--team-a", dest="radiant_team", default=None, help="Radiant team name"
    )
    p.add_argument(
        "--dire-team", "--team-b", dest="dire_team", default=None, help="Dire team name"
    )
    p.add_argument(
        "--period", type=int, default=1, help="map period (1 for map 1, 2 for map 2, etc.)"
    )
    p.add_argument("--radiant", nargs="+", default=[], help="5 radiant hero names or ids")
    p.add_argument("--dire", nargs="+", default=[], help="5 dire hero names or ids")
    p.add_argument(
        "--first-action",
        choices=["radiant", "dire", "unknown"],
        default="unknown",
        help="which side had first draft action (default: unknown)",
    )
    p.add_argument("--source", default="pinnacle")
    p.add_argument("--method", choices=["shin", "proportional"], default="shin")
    p.add_argument("--aliases", default=None, help="alias table (default: packaged aliases.yaml)")
    p.add_argument(
        "--max-stake", type=float, default=100.0, help="max suggested stake (default: 100)"
    )
    p.add_argument(
        "--bankroll", type=float, default=1000.0, help="total bankroll for Kelly (default: 1000)"
    )
    p.add_argument(
        "--freshness-limit",
        type=int,
        default=300,
        help="max acceptable quote age in seconds (default: 300)",
    )
    p.add_argument(
        "--trades-log", default="data/paper_trades.jsonl", help="path to paper trades log"
    )
    p.add_argument(
        "--allow-stale",
        action="store_true",
        help="record a trade even when the quote is older than --freshness-limit"
    )
    p.add_argument(
        "--rehearse",
        "--dry-run",
        action="store_true",
        dest="rehearse",
        help="rehearsal mode: log nothing",
    )
    p.add_argument("--settle", action="store_true", help="settle existing trades in trades log")
    p.set_defaults(func=cmd_bet)

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
