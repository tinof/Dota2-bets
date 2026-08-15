## Context

Everything below was measured against the live `/api/explorer` endpoint while writing this
document, not inferred:

| Probe | Result |
|---|---|
| `matches` in our window (`start_time >= 1712000000`) | 66,361 (we track 64,900) |
| `match_patch` joined to that window | 66,361 — 100% patch coverage |
| `player_matches` joined to that window | 663,610 rows = 10 per match |
| One 2-month `picks_bans` slice | 102,195 rows, 7.2 MB, **1.4 s** |
| Available tables | `matches`, `match_patch`, `picks_bans`, `player_matches`, `teams`, `team_match`, `notable_players`, `leagues`, `heroes`, `items`, `public_matches`, `team_rating` |
| `matches` columns of interest | `radiant_team_id/name`, `dire_team_id/name`, `radiant_gold_adv`, `radiant_xp_adv`, `radiant_win`, `start_time`, `leagueid` |
| `pre_game_duration` | **absent** from Explorer's `matches` |
| `match_patch.patch` | version *name* string (`'7.41'`), not the index |
| `/api/constants/patch` | 61-element list; index 60 → `{'name': '7.41', 'id': 60}` |

Existing code this builds on (all reusable as-is):

- `storage.upsert_matches` already uses `ON CONFLICT ... COALESCE(excluded.c, c)`, so writing
  a partial match row cannot null out existing columns — the "never destroys data"
  requirement is satisfied by the helper we already have.
- `storage.upsert_draft_events` / `upsert_match_players` / `upsert_timeseries` /
  `upsert_teams` are `INSERT OR REPLACE` on natural primary keys → idempotent by
  construction. `storage.record_roster` handles roster history.
- `opendota.parse_match_detail` defines the canonical row shapes (`match`, `draft_events`,
  `players`, `timeseries`, `teams`, `rosters`). The bulk path must emit the same dicts.
- `storage.matches_needing_detail` drives resumability off `detail_fetched_at IS NULL`.
- `cli._handle_signal` / `_stop` is the established graceful-interrupt pattern (`cmd_detail`
  checks `_stop` each iteration).

## Goals / Non-Goals

**Goals**

- Cut the remaining enrichment from ~9h to minutes without changing a single stored value's
  meaning.
- Reuse the existing parse/upsert layer rather than introducing a second row vocabulary.
- Keep the per-match crawl working and correct as a fallback.

**Non-Goals**

- Removing or deprecating `cmd_detail`.
- Schema changes or a migration.
- Importing `public_matches` (pub games — wrong population for this project).
- Building a general-purpose Explorer query interface; this is one import job.
- **Extending the history before Apr 2024** — deliberately deferred, see below.

### Deferred: Explorer holds ~4x our history

Measured while designing this change: Explorer's `matches` table has **251,395 pro matches
back to 2012** (2023: 30,502 · 2020: 22,384 · 2016: 11,524), of which **185,034 pre-Apr-2024
matches carry a patch**, and draft coverage runs ~97% every year back to 2013. Our window
starts Apr 2024 only because that is how far the `proMatches` summary backfill paginated —
not because more does not exist. The same is true of the deepest Kaggle alternative
(bwandowando, 2016–2026), which is scraped from this same source.

Using any of it would need a *summary* import from Explorer's `matches` table, not just
enrichment — a separate capability from this one, which by spec only enriches matches
already tracked.

Deferred by decision, not oversight. The ~38k matches from 2023–Q1 2024 are the genuinely
attractive slice (adjacent patches, current-era rosters); pre-2023 is weak for a draft model
because hero pools and meta turn over every patch, and only modestly useful for ratings
because 2014's teams barely intersect 2026's roster pool. Revisit if Phase 2 turns out to be
data-starved.

## Decisions

### Slice by `start_time`, three queries per slice

Each slice runs `picks_bans`, `player_matches`, and a combined `matches`+`match_patch`
query, each `JOIN matches m USING(match_id)` with `m.start_time >= lo AND m.start_time < hi`.
Slice width starts at 14 days (~7 MB / 100k rows measured for 60 days of `picks_bans`, so
14 days is comfortable) and the loop halves the width and retries on timeout or oversized
response.

*Why time slices rather than `match_id IN (...)`:* our unfetched ids number ~41k; an `IN`
list of that size produces a URL far past any practical limit, and chunking to ~500 ids per
request costs 80+ requests per table with no benefit over date ranges. Time is also how the
upstream table is naturally ordered, and it gives a resumable cursor for free.

*Why not one query for the whole window:* a single `picks_bans` pull over 2.3 years would be
~1.4 M rows / ~100 MB in one response, with nothing committed until it lands. Slicing bounds
the blast radius of any one failure and satisfies the incremental-commit requirement.

### Patch: resolve name → index via `/api/constants/patch`, fail closed

`match_patch.patch` is `'7.41'`; our column and the Phase 1b `patch_rd_boost` feature use the
integer index `60`. Fetch `/api/constants/patch` once per run and build `{name: id}`. An
unresolvable name stores NULL and leaves the match not-fully-enriched.

This is the single most dangerous detail in the change: storing `'7.41'` or a positional
guess into an INTEGER column would silently produce wrong patch transitions, and the Phase 1b
feature would then be measured on corrupt input without failing any test. Hence it is a spec
requirement, not just an implementation note. A verification task compares bulk-imported
patch values against crawler-imported ones on the ~21.5k matches that already have both.

### `is_radiant` derived from `player_slot`

Explorer's `player_matches` has no `isRadiant`; the convention is `player_slot < 128`.
`parse_match_detail` gets this from the API's own `isRadiant` field. Deriving it is exact,
not a heuristic, and the overlap check above will confirm it on real rows.

### Reuse `parse_match_detail`'s row shapes via a new sibling parser

Add `opendota.parse_explorer_rows(...)` that turns the three Explorer result sets into the
same dict-of-lists `parse_match_detail` returns, then feed the existing upsert helpers. One
parser per source, one writer for both — so the "interchangeable sources" requirement is
enforced by construction rather than by keeping two writers in sync.

### Mark `detail_fetched_at` only on complete matches

Set it for a match only when patch resolved AND 10 player rows AND (draft rows present OR
the match genuinely has no draft). Anything short leaves it NULL so `matches_needing_detail`
picks it up later, from either path.

*Trade-off:* a handful of matches may bounce between paths. That is preferable to marking a
match done and having Phase 2 silently train on a missing draft.

### Timeseries imported, but last and optionally

`radiant_gold_adv`/`radiant_xp_adv` are large array columns and nothing currently consumes
`match_timeseries`. Import them in their own slice pass behind a `--with-timeseries` flag
(default off) so the common run stays fast, and the data is available when Phase 2 wants it.

## Risks / Trade-offs

- **`/api/explorer` has no documented SLA or rate limit** and is heavier per call than REST.
  → Keep the existing client throttle, ~80 requests total, halve-and-retry on failure, and
  keep `cmd_detail` as the fallback. If Explorer disappears, nothing is lost but speed.
- **Explorer's Postgres may lag or differ from the REST payload.** → The overlap check on
  ~21.5k already-crawled matches is the control: patch, draft counts and player rows must
  agree. Disagreement blocks the change rather than being papered over.
- **Explorer has ~1,461 in-window matches we do not track** (66,361 vs 64,900). Skipping them
  is required by spec; the count is logged. They are likely leagues our summary crawl filters
  out, and adding them would change the scored population mid-flight — deliberately out of
  scope here.
- **`pre_game_duration` will be NULL for bulk-imported matches.** Confirmed unused across
  `src/` and `tests/`; recorded so a future consumer is not surprised.
- **Silent partial success** is the failure mode that would hurt most (a slice quietly
  returning fewer rows). → Per-slice row counts are logged and the final coverage report is
  a spec requirement.
