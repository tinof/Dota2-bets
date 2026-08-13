# Dev handoff

State as of 2026-08-13 20:35 local. TI 2026 runs 10–23 Aug, so the odds-capture window
has about ten days left. Read [`research.md`](research.md) for why the project is shaped
this way; this file is only about where the code stands and what to do next.

## Status: the recorder is live

`com.dota2bets.recorder` is installed as a launchd agent and **running now**, polling
Pinnacle every 60s (20s while a game is live) into `data/dota2bets.sqlite`, logging to
`data/recorder.log`. Verified: it respawns after `kill -9` (KeepAlive), writes line
history across polls, tombstones vanished markets, and archives raw payloads.

```bash
launchctl print gui/$(id -u)/com.dota2bets.recorder | grep -E 'state|pid'
tail -f data/recorder.log
launchctl bootout gui/$(id -u)/com.dota2bets.recorder     # stop
```

It does **not** prevent sleep. For unattended capture keep the Mac on mains power and
run `caffeinate -s`, or disable sleep for the event.

Commits: `de68260` (Phase 0), `fd31fda` (handoff), `ff32908` (recorder fidelity),
plus a formatting commit. 41 tests passing, ruff clean.

## What changed this session, and why it mattered

An audit found the earlier plan's ordering wrong: it would have run the recorder
unattended for a week collecting data that **could not answer the question the project
exists to answer**. Three defects, all now fixed in `ff32908`:

1. **`cutoff_at` was not in `storage._CHANGE_COLS`.** Pinnacle pushes a market's cutoff
   forward as a series progresses — that push is precisely the map-2/3 window signal.
   It was being discarded whenever the price happened not to move.
2. **Market removal wrote nothing.** A pulled market (suspension during a fight, or a
   map going off the board) was indistinguishable from "unchanged". Now
   `write_tombstones` writes a `status='gone'` row, and a line returning is recorded
   even at an unchanged price.
3. **No raw archive.** The normaliser sits in front of an unofficial API that already
   changed shape once. Payloads are now gzipped to `data/raw/<source>/<date>/` on change
   plus an hourly heartbeat, so a normalisation bug found later can be fixed by
   reprocessing rather than losing unrepeatable history.

Also: adaptive poll interval, `PRAGMA busy_timeout=5000` so the recorder and a backfill
crawl can run concurrently, and httpx per-request logging quieted.

## Still unproven

- **The live path has never executed.** No `is_live=1` row exists yet, because no match
  was live during testing. First live TI series: confirm `is_live=1` rows appear, the
  interval tightens to 20s, and tombstones appear during teamfight suspensions.
- `theoddsapi` source is written but never run against the real API.

## Next steps

**1. Entity resolution — gates every evaluation.** Pinnacle says `Spirit`, `LGD`,
`Vici`, `Resilience`; OpenDota says `Team Spirit`, `LGD Gaming`, `Vici Gaming`, `Team
Resilience`. Kills markets appear as parallel pseudo-events (`Spirit (Kills)`). Until an
alias table joins odds events to `matches` rows — and periods to maps within a series —
no CLV, no backtest, nothing is computable. Plan: `src/dota2bets/aliases.py` plus a
checked-in `aliases.yaml`, curated for the 16 TI teams first, with an unresolved-name
report.

**2. Window-experiment report.** `scripts/window_report.py`: per finished TI series,
timeline each period — when the cutoff moved, when lines vanished/reappeared, what
prices did between map starts. Answers whether map-2/3 markets stay biddable through
their drafts. If yes, Phase 2 proceeds as designed; if no, the draft model retargets
toward live-anchored betting (Clegg-style market calibration) and exchange venues.

**3. History backfill** (parallel; safe now that `busy_timeout` is set). Detail coverage
is 8/300 matches spanning 2.5 weeks — training data is effectively absent. Register
`OPENDOTA_API_KEY` or move to STRATZ GraphQL batching; target 12–18 months.

**4. Evaluation harness before any model.** De-vig (proportional + Shin), CLV joining
model probabilities to the last pre-cutoff snapshot. Then Phase 1 ratings score against
it from day one — never raw accuracy.

## Gotchas worth not rediscovering

**Reading the DB while the recorder runs: never `cp` the `.sqlite` file alone.** WAL mode
keeps recent commits in the `-wal` sidecar, so a plain copy reads stale — this produced a
false "the recorder wrote nothing" panic mid-session. Open the real path read-only
instead:

```python
sqlite3.connect(f"file:{os.path.abspath('data/dota2bets.sqlite')}?mode=ro", uri=True)
```

**`kill -TERM` on a `uv run` wrapper does not reach the Python child.** A manual test
recorder survived its kill and kept writing alongside the launchd service for several
minutes. Kill the `.venv/bin/dota2bets` PID, or use Ctrl-C. Check for strays with
`pgrep -fl "dota2bets record-odds"` — expect exactly one `uv` + one python pair.

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
a ~2,000/day free-tier ceiling, so a year-scale backfill is roughly a week of crawling.
`iter_pro_matches` paging is cheap; only `detail` is expensive.

**`lane_role` is null until OpenDota parses the replay** — don't assume it is present.

**Kills markets are a secondary target, not a pivot.** They were 336 of 386 captured
lines, but that is alternate-line quoted depth, not money. Worth modelling in Phase 2
(draft composition predicts kill volume more directly than it predicts the winner);
not worth reorganising around.

**Avoid tier-3 matches** — softest markets, but match-fixing risk makes that softness
adverse selection rather than edge.

## Commands

```bash
uv sync && uv run pytest                        # no network needed
uv run dota2bets backfill --max-matches 1000
uv run dota2bets detail --limit 200             # slow; see rate limits
uv run dota2bets record-odds --once             # single cycle, for debugging
uv run dota2bets status
```

Env: `OPENDOTA_API_KEY` (raises rate limits), `ODDS_API_KEY` (enables
`--sources pinnacle theoddsapi`).

## Open question for the user

`papers/thesis.pdf` is not a Dota 2 document — it is a 2006 Waikato MSc thesis on
multi-instance learning, presumably background for the multi-instance-learning esports
paper. Confirm whether a different thesis was meant to be there.
