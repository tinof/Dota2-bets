## 1. Explorer client

- [x] 1.1 Add `OpenDotaClient.explorer(sql: str) -> list[dict]` — GET `/api/explorer` with
      the SQL url-encoded, reusing the existing session, throttle and api-key plumbing;
      raise when the response body's `err` is non-null; return `rows`
- [x] 1.2 Add `OpenDotaClient.patch_index() -> dict[str, int]` from `/api/constants/patch`,
      mapping version name (`'7.41'`) to integer id (`60`), fetched once per run
- [x] 1.3 Tests with stubbed transport: `explorer` returns rows on success, raises on `err`,
      url-encodes SQL correctly; `patch_index` builds the name→id map

## 2. Explorer row parser

- [x] 2.1 Add `opendota.parse_explorer_rows(matches, picks_bans, player_matches, patch_map,
      *, fetched_at)` returning the SAME dict-of-lists shape as `parse_match_detail`
      (`match`, `draft_events`, `players`, `timeseries`, `teams`, `rosters`)
- [x] 2.2 Derive `is_radiant` as `player_slot < 128`; map `gold_per_min`/`xp_per_min`/
      `lane_role`/K/D/A straight across; leave `player_name` NULL (Explorer has no name in
      `player_matches`) and `pre_game_duration` NULL (absent from Explorer)
- [x] 2.3 Resolve patch via `patch_map`; an unresolvable version name leaves patch NULL
- [x] 2.4 Build `teams` rows from `radiant_team_id/name` + `dire_team_id/name`
- [x] 2.5 Capture real Explorer response shapes as JSON fixtures under `tests/fixtures/`
      (small slice of each of the three result sets) — no live network in tests
- [x] 2.6 Tests: row shapes match `parse_match_detail` for the same match; `is_radiant`
      derivation; unresolvable patch → NULL; missing draft → empty list not an error

## 3. Bulk import

- [x] 3.1 Add `cmd_bulkfill` in `cli.py`: iterate `start_time` slices (14-day default) over
      the range of matches lacking detail, running the three queries per slice
- [x] 3.2 Filter to match ids already in our `matches` table; count and report skips
      (Explorer has ~1,461 in-window matches we do not track)
- [x] 3.3 Write via the existing `storage.upsert_*` helpers and `record_roster`; commit per
      slice; check the existing `_stop` flag between slices for graceful interrupt
- [x] 3.4 Set `detail_fetched_at` only when patch resolved AND 10 players AND (draft rows
      present OR the match has no draft); otherwise leave NULL so the match stays eligible
- [x] 3.5 On slice failure (timeout/oversized), halve the slice width and retry; on repeated
      failure log and continue to the next slice
- [x] 3.6 Import timeseries in a separate pass behind `--with-timeseries` (default off)
- [x] 3.7 Print per-slice row counts and a final coverage report: matches enriched, skipped,
      failed, and totals with patch / players / draft
- [x] 3.8 Register the `bulkfill` subparser (`--db`, `--slice-days`, `--since`,
      `--with-timeseries`, `--delay`) next to `detail`
- [x] 3.9 Tests against a tmp SQLite db with stubbed Explorer responses: correct table
      mapping, unknown-match skip, idempotent re-run, partial match not marked fetched,
      existing summary columns preserved after import

## 4. Cross-source verification (the control for this change)

- [x] 4.1 Run `bulkfill` limited to matches the per-match crawl ALREADY enriched (~21,555),
      into a scratch copy of the database — never the live one
- [x] 4.2 Compare bulk vs crawled on that overlap: `patch` equal for every match, draft event
      counts and `(ord, hero_id, is_pick, team)` tuples equal, `match_players` rows equal on
      `account_id`/`hero_id`/`is_radiant`/K/D/A/gpm/xpm
- [x] 4.3 Any mismatch blocks the change — investigate and fix the parser before importing
      into the live database. Record the comparison result in `docs/dev-handoff.md`
- [x] 4.4 `uv run pytest` and `uv run ruff check` green

## 5. Run the import and unblock Phase 1b

- [x] 5.1 Back up `data/dota2bets.sqlite` before the first live run
- [x] 5.2 Run `uv run dota2bets bulkfill` over the full history; confirm the coverage report
      shows ~62.9k matches with patch, players and draft
- [x] 5.3 Spot-check 3 imported matches against their opendota.com match pages (patch, draft
      order, lineups)
- [x] 5.4 Execute tasks 6.1–6.7 of `add-phase-1b-rating-features` at full coverage: baseline
      re-confirm (0.2266 elite), both ablations, bounded `--tune`, then `predict`/`eval`
      same-day against TI 0.2993 / market 0.2508, and apply the adoption rule
- [x] 5.5 Replace the provisional Phase 1b section of `docs/dev-handoff.md` with the final
      result, and re-check tasks 6.x of that change
- [x] 5.6 Update `docs/dev-handoff.md` on the new data path: `bulkfill` is the primary
      enrichment route, `detail` remains the per-match fallback, and timeseries is available
      via `--with-timeseries` when Phase 2 wants it
