# Dev handoff

State as of 2026-08-14 15:00 local. TI 2026 runs 10–23 Aug, so the odds-capture window
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

### History backfill: keyed, resumable, and running

**An OpenDota API key is now in use.** It lives in `.env` (gitignored) as
`OPENDOTA_API_KEY` — never commit it, and load it per-shell:

```bash
set -a; . ./.env; set +a
```

With a key the client paces itself at 0.12s instead of the keyless 1.2s, and the
40,000-summary crawl that previously took hours (and died) finished in about 90 seconds.

Two fixes made the crawl completable, both prompted by a real 429 crash at ~24,900
summaries:

* **Backoff doubles to five minutes and honours `Retry-After`.** The old linear 5/10/15s
  schedule gave up after 30 seconds, which a sustained throttle simply outlasts — and the
  exception killed a crawl that had no way to resume.
* **`backfill --resume`** continues below the oldest stored `match_id`
  (`storage.oldest_match_id`) instead of re-walking from the newest match. `detail` was
  always resumable — it commits per match and re-queries what is missing.

Current state: **64,900 match summaries** (Apr 2024 → now, over two years) and a keyed
`detail` crawl working through ~62,900 of them. Measured throughput is **~75 matches per
minute — about 14 hours** — with no 429s and no failures. That rate is bound by OpenDota's
per-request latency (~0.8s), *not* by our delay, so the printed ETA (which assumes the
delay dominates) is optimistic by an order of magnitude. The key permits 1200 calls/min,
so a handful of concurrent workers would cut this to ~1.5h; not worth threading a working
serial pipeline for a one-off overnight crawl, but that is the lever if it is ever needed
again. It runs under `caffeinate -s` and survives a session ending; check progress with
`tail -1 data/detail.log`.

**Watch the quota:** the key includes 50,000 calls/month free, then bills per call. One
detail call per match means this crawl alone exceeds the free allowance by roughly 13,000
calls (about $1–2). Cheap, but not free — don't loop it needlessly.

### Earlier sourcing note (superseded by the key above)

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

### The evaluation harness exists (`evaluation.py` + `dota2bets eval`)

Every future model is graded against the closing market from its first prediction — CLV
and Brier *against the closing probability on the same rows*, never raw accuracy.

`devig_proportional` / `devig_shin` (+ `shin_z`) remove the vig; Shin is the default
because books load the longshot and proportional de-vig pretends they don't. Reference
values are pinned in tests, including the favourite-longshot direction check.

The closing line is the part that is easy to get silently wrong, so:

* **The bound is `matches.start_time` (OpenDota's horn), never `cutoff_at` and never
  Pinnacle's `start_time`.** The earlier design said "before min(cutoff, start)"; that is
  wrong under the placeholder gotcha below — the min is a no-op while the cutoff is parked
  far-future, and a *genuine* cutoff flips rows to `status='closed'`, which the open filter
  already drops. `cutoff_at` survives only as a `stale_cutoff` warning flag.
* **Only the parentless event is the pre-match one** (`closing_event`), plus an `is_live=0`
  predicate in `closing_lines`. Both filters are independent, so an archive gap that hides
  a parent link degrades to "no closing line", never to a live price masquerading as a close.
* Totals de-vig **per `points` group** — alternate lines are separate two-sided books, and
  pooling them would produce nonsense probabilities.

Live output, cross-checked against `window_report` on series 1130278 (identical prices):

```
1130278 p1  vig +0.049  Spirit 0.566 @1.69  Aurora 0.434 @2.18
```

Predictions are JSONL of `Prediction` fields (`series_id`, `period`, `selection`, `prob`,
optional `price_taken`); `--before-draft N` cuts the close N seconds before the horn for
pre-draft evaluation. 92 tests pass.

### Phase 1 ratings exist, and the TI backtest has a number

`ratings.py` (Glicko-2) and `backtest.py` (walk-forward, tuning, event predictions),
wired as `dota2bets backtest` and `dota2bets predict`.

**The headline: model Brier 0.2993 vs closing Brier 0.2508 on the same 70 TI rows.** The
model does not beat the market. That was the expected result for team strength alone, and
it is the number every later model has to improve on. Separately, over all 55 played TI
maps (including 10–12 Aug, which the recorder missed entirely) it scores Brier 0.2337
against a 0.25 coin flip.

**Glicko-2, not Bradley-Terry.** The binding constraint is leak-freeness, not fit quality:
an online update yields a strictly pre-match prediction for all 62k historical maps in one
pass, where a time-decayed BT must be refit at every prediction point. RD inflation over
idle time *is* the decay, so there is no separate half-life. Per-patch training was
impossible anyway — `matches.patch` is NULL on every summary-only row.

**Most of the match pool is unpredictable, and it was hiding the model.** OpenDota's
`/proMatches` is dominated by low-tier grinder circuits — Destiny League alone is 7,645 of
62,000 matches — and an unfiltered walk-forward reads Brier 0.2449, a 1.3% edge on a coin
flip. A plain Elo baseline scored the same 2%, which is what ruled out an implementation
bug rather than a data problem. Restricted to top-tier leagues between teams with 20+
prior maps, the *same ratings* score 0.2266, a 9.4% edge.

Those tier-3 maps still **train** the book — training on elite-only is measurably worse,
because teams cross between circuits — they are simply not the population to be judged on.
So `--score elite` (the default) narrows the metric only, never the training set. Read any
unfiltered Brier on this dataset as meaningless.

The tuning grid is flat to the fourth decimal across `tau` and idle period, so Glickman's
defaults stand instead of a fitted value that would really be noise. Don't spend more time
tuning these; the gains are in features, not constants.

Also fixed: `evaluation.WINS_NEEDED` let `series_type=3` (best-of-two) fall through to
first-to-1, crowning the map-1 winner of a series that can end 1-1 and never settles. 2,676
such matches exist in the DB. Not triggered at TI, where every series is a Bo3.

Predictions land in gitignored `data/`, so they are never committed:

```bash
uv run dota2bets backtest --tune          # grid search on the elite subset
uv run dota2bets predict --out data/ti2026_preds.jsonl
uv run dota2bets eval --predictions data/ti2026_preds.jsonl
```

## Next steps

1. **Let the detail crawl finish** (~14h from 2026-08-14 14:30 local; resumable — just
   re-run `dota2bets detail` with the key loaded if it stops). Detail coverage was the
   binding constraint on Phase 1; after this it no longer is.
2. **Phase 1b, once detail lands**: per-patch rating windows and a roster-stability
   penalty (both need `match_players`, hence the crawl). Add them as *features on top of*
   the Glicko baseline and re-run the same two commands — the 0.2266 elite Brier and the
   0.2993 TI Brier are the numbers to beat, and any change that does not move both is not
   an improvement.
3. **The gap to close is 0.05 Brier, and ratings alone will not close it.** The market
   prices roster news, patch reads and draft; the model prices none of them. The draft
   model (Phase 2) is the one with a real shot, and `window_report.py` already proved
   map-2/3 moneylines stay open through their drafts at $2,500 median max stake.
4. Re-run `dota2bets resolve`, `predict` and `window_report.py` as TI progresses; all are
   idempotent and cheap. Each finished series adds rows to the CLV comparison.

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

**Read paths use `storage.connect_ro`** (moved out of `window_report.py`, which now imports
it). `dota2bets eval` opens the DB read-only, so it is safe while the recorder polls.

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
adverse selection rather than edge. They are also 79% of the match table and close to
coin flips, so any metric averaged over the whole pool is measuring their noise; see the
Phase 1 section on `--score elite`.

**A model result on this data is not interpretable without saying which population it was
scored on.** Same ratings, same code: 0.2449 Brier over everything, 0.2266 over top-tier
matches between established teams. Always state the filter alongside the number.

## Commands

```bash
uv sync && uv run pytest                        # 138 tests, no network needed
set -a; . ./.env; set +a                          # load OPENDOTA_API_KEY (gitignored)
uv run dota2bets backfill --max-matches 40000 --resume
uv run dota2bets detail --limit 5000             # one call per match; watch the quota
uv run dota2bets resolve                         # join odds events to teams/series
uv run dota2bets record-odds --once              # single cycle, for debugging
uv run dota2bets eval                            # closing de-vigged probs per series
uv run dota2bets eval --predictions preds.jsonl  # CLV + Brier vs the closing Brier
uv run dota2bets backtest --tune                 # walk-forward grid search (elite subset)
uv run dota2bets predict --out data/preds.jsonl  # pre-match predictions for TI
uv run dota2bets status
uv run python scripts/window_report.py           # the draft-window experiment
```

Env: `OPENDOTA_API_KEY` (set, in gitignored `.env`), `ODDS_API_KEY` (enables `--sources pinnacle
theoddsapi`; still never run against the real API).

## Settled questions

`papers/thesis.pdf` (a 2006 Waikato MSc thesis on multi-instance learning) is deliberate
background reading for the multi-instance-learning esports paper, not a misplaced file.
