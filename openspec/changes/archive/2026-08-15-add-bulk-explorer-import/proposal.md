## Why

Enriching the 62,916-match history one `/matches/{id}` call at a time takes ~14 hours and
burns paid API quota; it is currently paused at 21,555 matches, and it is the only thing
blocking the Phase 1b evaluation and Phase 2's draft features. OpenDota exposes the same
data through `/api/explorer`, an arbitrary-SQL endpoint over their Postgres. Measured
against the live endpoint today, one query returned **102,195 draft rows in 1.4 seconds** —
the entire remaining backfill is roughly 80 queries and a few minutes instead of 41,000
requests and nine hours.

The question that prompted this was whether an existing published dataset (Kaggle or
similar) could replace the crawl. Investigated: the only credible Kaggle candidate is
bwandowando's "Dota 2 Pro League Matches 2016–2026", which is itself scraped from OpenDota,
lands on the maintainer's update schedule, and arrives in a foreign file layout. Everything
else found is public-matchmaking data or 2016-era (patch 6.88 against today's 7.41 — no
transferable draft signal). So the answer is yes to bulk import, but from the upstream
source the datasets are built from rather than from a mirror of it.

## What Changes

- The OpenDota client gains an `explorer(sql)` method for the SQL endpoint.
- A new `bulkfill` command imports patch, drafts, players, teams, rosters and per-minute
  series in date-sliced bulk queries, writing through the **existing** upsert helpers into
  the **existing** tables. No schema change; the destination rows are byte-compatible with
  what the per-match crawl already produces.
- `matches.patch` gains a translation step: Explorer reports patch as a version *name*
  (`'7.41'`) while `/matches/{id}` — and therefore our column and the Phase 1b feature —
  uses OpenDota's integer patch *index* (60). The import resolves names to indices via
  `/api/constants/patch`. Getting this wrong would silently corrupt the patch feature, so
  it is specified rather than left to the implementation.
- `pre_game_duration` is not exposed by Explorer and will stay NULL for bulk-imported
  matches. It is unused anywhere in the codebase; noted rather than worked around.
- The per-match `detail` command is unchanged and stays available — bulk import is an
  additional path, not a replacement, and the two must agree on what a "detailed" match is.

## Capabilities

### New Capabilities
- `match-enrichment`: how the pro-match history is enriched beyond summaries — what counts
  as a fully-detailed match, the guarantees an import must uphold (idempotence,
  resumability, no clobbering of existing data, correct patch representation), and the
  requirement that any import path produce data indistinguishable from any other.

### Modified Capabilities
<!-- None. `team-ratings` consumes this data but none of its requirements change. -->

## Impact

- `src/dota2bets/opendota.py` — new `explorer()` method; a patch-constants fetch and
  name→index resolver.
- `src/dota2bets/cli.py` — new `cmd_bulkfill` and its subparser, alongside `cmd_detail`.
- `src/dota2bets/storage.py` — reuses `upsert_matches` (already COALESCE-based, so it will
  not clobber), `upsert_draft_events`, `upsert_match_players`, `upsert_timeseries`,
  `upsert_teams`, `record_roster`. A coverage helper may be added for the summary output.
- `tests/` — fixture-based tests from response shapes captured today; no live network.
- No database migration, no new dependencies.
- Unblocks: the deferred Phase 1b evaluation (tasks 6.1–6.7 of
  `add-phase-1b-rating-features`) and Phase 2's draft model.
- Risk accepted: `/api/explorer` is a public endpoint with no documented SLA or rate limit,
  and is heavier per call than the REST endpoints. The per-match crawl remains the fallback.
