# Dev handoff

State as of 2026-08-14 13:00 local. TI 2026 runs 10–23 Aug, so the odds-capture window
has about nine days left. Read [`research.md`](research.md) for why the project is shaped
this way; this file is only about where the code stands and what to do next.

## Status: recording, resolving, and reporting

`com.dota2bets.recorder` is installed as a launchd agent and **running**, polling
Pinnacle every 60s (20s while a game is live) into `data/dota2bets.sqlite`, logging to
`data/recorder.log`.

```bash
launchctl print gui/$(id -u)/com.dota2bets.recorder | grep -E 'state|pid'
tail -f data/recorder.log
launchctl kickstart -k gui/$(id -u)/com.dota2bets.recorder   # restart (picks up code changes)
launchctl bootout gui/$(id -u)/com.dota2bets.recorder        # stop
```

It does **not** prevent sleep. For unattended capture keep the Mac on mains power and
run `caffeinate -s`, or disable sleep for the event.

**The three things the last handoff listed as unproven are now proven in the data.** The
live path executed during TI: `is_live=1` rows exist, the interval tightens to ~20s while
a game runs, and tombstones appear during live play (market pulls while `is_live=1`), so
suspensions are being recorded rather than silently blurred into "unchanged".

## What changed this session

### A live bug: `team_total` line keys were colliding

Pinnacle puts the *team* of a team-total market on the market as `side: home|away`, while
the prices only carry `designation: over|under`. `normalise` dropped `side`, so both
teams' totals at the same points collapsed onto one `line_key` — two independent markets
overwriting each other's history, ~78 colliding keys per payload, visible as a permanent
"4 new" every cycle in the recorder log. `selection` is now `"<team>:<over|under>"`, which
also keeps the key stable the way the moneyline convention does. After restarting the
recorder the log settled to "0 new, 274 unchanged".

Kills team-total history recorded before this fix is thrashed. It is repairable from the
raw archive (coverage there is dense precisely *because* the thrash forced archive
writes) — write `scripts/reprocess_archive.py` if that history turns out to matter.

### Entity resolution (`aliases.py` + `aliases.yaml` + `dota2bets resolve`)

Odds events now join to OpenDota series. `resolve` writes `event_series_map` and the
`v_odds_series` view joins it to `odds_snapshots`. Current run: **36 of 36 events
resolved to teams, 34 joined to a series, zero unresolved names** (the two unjoined were
a match not yet played).

Name matching is exact against a curated table — whitespace-collapsed, case-insensitive,
with `(Kills)` stripped — never fuzzy. A wrong join is silent and corrupts every number
downstream, so an unknown name is *reported for a human* instead of guessed at. Add the
team to `aliases.yaml` when `resolve` reports one.

Two structural facts drove the design, both recoverable only from the raw archive:

* **Pinnacle re-lists a series under a new event id when it goes live**, and again for
  kills markets, linked by `parentId` — a field `normalise` drops. Children inherit their
  parent's series, so map-2/3 prices quoted under a live child stay attached correctly.
* **Its `startTime` is the scheduled slot**, drifting up to ~1.2h from when map one
  actually began, in both directions. The series join is therefore team-pair plus
  *nearest* start within a tolerance, never equality.

### The window experiment has an answer

`scripts/window_report.py` (reads the DB read-only; safe while recording).

**Map-2 and map-3 moneylines stay open through their drafts — 11 of 11 observed periods
at 100% coverage, still quoted at the horn, at a median max stake of $2,500 (up to
$10,000).** Markets do get pulled mid-series — one map-2 line was gone for 23 minutes
while map 1 ran — but they return before the next draft.

So the Phase 2 draft model has a real window to bet into, and the retarget toward
live-anchored betting is **not** needed. That was the open strategic question; it is
closed for now, on nine series of TI data. Re-run the report as more series finish.

### History backfill

2,000 pro-match summaries ingested; a detail crawl (drafts, players, per-minute series)
is running in the background against the keyless API at ~1 match/second.

**Sourcing decision.** OpenDota's free tier is keyless at 50,000 calls/month and 60/min;
a registered key is the *paid premium tier* (payment method required) — the earlier
"register `OPENDOTA_API_KEY` to raise limits" note was optimistic about it being free.
STRATZ is genuinely free (Steam login: 10,000 calls/day, ~100 matches batched per
GraphQL call) but needs a new client, parser and fixtures against a different schema.

Recommendation: **pay for an OpenDota key** for a 12–18 month backfill (~13h of crawling,
zero new code on an already-tested pipeline). The keyless tier is fine for keeping up
with TI day to day, but a year-scale backfill would consume most of a month's quota.
Switch to STRATZ only if the key's pricing disappoints at signup.

## Next steps

1. **Evaluation harness before any model.** De-vig (proportional + Shin) and CLV joining
   model probabilities to the last pre-cutoff snapshot from the *pre-match* event, never
   the live child. Sketch in the approved plan at
   `~/.claude/plans/regarding-the-docs-dev-handoff-md-how-linked-squirrel.md`.
2. **Finish the backfill** once the key decision is made. Detail coverage is the binding
   constraint on Phase 1 — training data is still effectively absent.
3. **Phase 1 ratings** (Glicko-2/Bradley-Terry per patch window + roster stability),
   scored against the harness from day one — never raw accuracy.
4. Re-run `dota2bets resolve` and `window_report.py` as TI progresses; both are
   idempotent and cheap.

## Gotchas worth not rediscovering

**Reading the DB while the recorder runs: never `cp` the `.sqlite` file alone.** WAL mode
keeps recent commits in the `-wal` sidecar, so a plain copy reads stale — this produced a
false "the recorder wrote nothing" panic. Open the real path read-only instead
(`window_report.connect_ro` does this):

```python
sqlite3.connect(f"file:{os.path.abspath('data/dota2bets.sqlite')}?mode=ro", uri=True)
```

**`kill -TERM` on a `uv run` wrapper does not reach the Python child.** Kill the
`.venv/bin/dota2bets` PID, use Ctrl-C, or `launchctl kickstart -k` for the service. Check
for strays with `pgrep -fl "dota2bets record-odds"` — expect exactly one `uv` + one
python pair.

**A period's `cutoff_at` is a placeholder until its map is next.** Pinnacle parks a
far-future cutoff (hours out, sometimes days) on a period whose map has not been reached,
then pulls it in as the map approaches. Only cutoffs published while the market is still
ahead of its map mean anything; `window_report` ignores the rest.

**`status='closed'` is not the same as a tombstone.** `gone` means the line vanished from
the poll; `closed` means Pinnacle still lists it but is not taking bets. Both are
un-biddable, only one is a pull.

**Only tombstone after a successful fetch.** A failed poll means we know nothing about
those lines, which is not the same as them being pulled. `cmd_record_odds` skips the
fetcher on exception; keep it that way.

**Pinnacle returns moneyline selections two ways** — `designation: home/away` in some
payloads, numeric `participantId` (assigned in participant order) in others. `normalise`
deliberately resolves both to team names; passing the raw designation through instead
would produce two different `line_key`s for one market and silently break change
detection. Guarded by `test_line_key_is_stable_across_price_moves`.

**Summary re-ingestion must not clobber detail.** `upsert_matches` merges with `COALESCE`
rather than `INSERT OR REPLACE`, or a later `/proMatches` sweep nulls `patch` and
`detail_fetched_at` and silently re-queues expensive refetches.

**OpenDota rate limits are the backfill bottleneck**: one call per match at 1.2s against
50,000 calls/month keyless. `iter_pro_matches` paging is cheap; only `detail` is
expensive.

**`lane_role` is null until OpenDota parses the replay** — don't assume it is present.

**Kills markets are a secondary target, not a pivot.** They dominate captured line count,
but that is alternate-line quoted depth, not money. Worth modelling in Phase 2 (draft
composition predicts kill volume more directly than it predicts the winner); not worth
reorganising around.

**Avoid tier-3 matches** — softest markets, but match-fixing risk makes that softness
adverse selection rather than edge.

## Commands

```bash
uv sync && uv run pytest                        # 70 tests, no network needed
uv run dota2bets backfill --max-matches 2000
uv run dota2bets detail --limit 2000             # slow; see rate limits
uv run dota2bets resolve                         # join odds events to teams/series
uv run dota2bets record-odds --once              # single cycle, for debugging
uv run dota2bets status
uv run python scripts/window_report.py           # the draft-window experiment
```

Env: `OPENDOTA_API_KEY` (paid tier), `ODDS_API_KEY` (enables `--sources pinnacle
theoddsapi`; still never run against the real API).

## Settled questions

`papers/thesis.pdf` (a 2006 Waikato MSc thesis on multi-instance learning) is deliberate
background reading for the multi-instance-learning esports paper, not a misplaced file.
