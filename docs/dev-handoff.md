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

### Bulk history enrichment via Explorer (`dota2bets bulkfill`)

Enriching match history via per-match `/matches/{id}` calls took ~14h and burned API quota.
`dota2bets bulkfill` imports patch, drafts, players, teams, and rosters directly from OpenDota's
PostgreSQL endpoint (`/api/explorer`) in 14-day date slices.

**Verification against crawled data (~21.5k overlap):**
- **Draft events:** 21,231 / 21,231 matches (509,328 events) — **100.0% exact match** on `(ord, hero_id, is_pick, team)`.
- **Match players:** 21,555 / 21,555 matches (215,550 player rows) — **100.0% exact match** on `account_id`, `hero_id`, `is_radiant`, `kills`, `deaths`, `assists`, `gold_per_min`, `xp_per_min`.
- **Patch index:** 21,555 / 21,555 (**100.0%**) exact match — *after* the fix below.

**The patch trap, and why the overlap check nearly missed it.** Explorer's `match_patch`
lags a release: matches played in the ~48h after 7.41 shipped (2026-03-24 00:50 UTC) are
still labelled `'7.40'` months later. The first import trusted that label and *downgraded*
194 already-correct matches from patch 60 to 59, verified wrong against
`/matches/{id}` (which reports `patch: 60` for e.g. `8741426861`).

Sweeping every row rather than only the flagged 194 found **750 wrong values**, not 194:
the overlap control can only see the 21.5k matches the crawler had also fetched, and the
older boundaries (patches 54–57, May 2024 → May 2025) were bulk-only and therefore
unchecked. Boundary lag is the rule, not a one-off.

The fix is `opendota.resolve_patch()`: derive the patch from `start_time` against
`/constants/patch` release dates — what `/matches/{id}` itself does — and fall back to the
label only when the start time cannot place the match. Patch date ranges are now exact
(each patch starts on its release date, ends at the next) and the crawler overlap agrees on
every row. Regression test: `test_patch_resolves_by_start_time_when_the_label_lags`.

This mattered because `matches.patch` feeds the ratings' patch-transition feature: a stale
label shifts a team's transition by days, precisely where the feature fires.
- **Coverage:** **64,900 of 64,900 matches fully enriched (100.0%)**; 1,537,303 draft events and 649,000 player rows stored.
- `bulkfill` is the primary enrichment path; `detail` remains as the per-match fallback. Timeseries is optionally available via `--with-timeseries`.

### Phase 1b rating features: FINAL evaluation at 100% detail coverage

`ratings.py` and `backtest.py` contain two uncertainty-widening mechanisms:
1. **Per-patch rating windows (`patch_rd_boost`, `--patch-boost`):** inflates RD in quadrature on the first map of a newer patch.
2. **Roster-stability penalty (`roster_rd_boost`, `--roster-boost`):** inflates RD in proportion to changed players (stand-ins), preloaded via `ratings.load_lineups(conn)`.
3. Both features degrade gracefully to the Phase 1 baseline when data is absent or when configured as `None`.

**Ablation and evaluation results — FINAL (100% coverage, 64,900 matches, re-run after the
patch-boundary repair above; the pre-repair numbers were measured on 750 wrong patch values):**

- **Baseline (both `None`):** Elite walk-forward Brier `0.2266` / log-loss `0.6441` (n=6638);
  TI 2026 closing-market evaluation Brier `0.2993` (70 scored rows vs closing market Brier `0.2508`).
- **Patch boost ablation (`--patch-boost 60`):** Elite walk-forward Brier `0.2265` / log-loss `0.6439`.
- **Roster boost ablation (`--roster-boost 60`):** Elite walk-forward Brier `0.2263` / log-loss `0.6437`.
- **Tuning grid (16 configs on historical holdout before TI):** top is `patch_boost=none`,
  `roster_boost=30` at Brier `0.2255` / log-loss `0.6416`. Note `patch_boost=30` *ties* it to
  four decimals — on corrected data the patch feature contributes essentially nothing, and
  the entire historical gain is the roster term.
- **Event benchmark (`roster_boost=30` on TI 2026):** Brier degrades to `0.3031` vs baseline
  `0.2993` (closing line `0.2508`), on the same 70 scored rows.

**Adoption decision — FINAL: defaults remain `None`.**
Under the adoption rule (enable by default only if *both* historical elite Brier 0.2266 and
TI 2026 event Brier 0.2993 improve), the features are rejected for defaults: `roster_boost=30`
improves long-term history (0.2255 vs 0.2266) but adds noise in dense tournament play
(0.3031 vs 0.2993). The repair did not change this verdict — it was reached twice, on wrong
and then on correct data — but it did change *which* feature carries the historical gain.
This settles Phase 1b: team ratings adjustments alone cannot close the ~0.05 gap to market closing lines. Phase 2 (draft models) is the next milestone.

### Phase 2 draft model and live paper trading: FINAL evaluation

The draft model (`draft.py`) integrates team ratings with 7 radiant-oriented draft features:
1. **Hero pool win rates** with lazy exponential decay (`half_life_days=120`, default) and Bayesian shrinkage (`k_hero=20`).
2. **Team-on-hero familiarity** with team win rate shrinkage (`k_team_hero=10`).
3. **Same-side synergy pairs** with pair shrinkage (`k_pair=100`).
4. **Cross-side matchup counters** with pair shrinkage (`k_pair=100`).
5. **Draft order advantage** (opening first-action pick/ban indicator).
6. **Combiner warmness indicator** (both teams have >= `min_team_games` maps on record, default **5**).
7. **Anchor Glicko-2 logit** and historical Radiant intercept (+0.063 logit) combined via online AdaGrad logistic regression.

#### Measurement and evaluation results

- **Harness neutrality (Task 5.1):** `backtest --model draft --disable-draft` produces **Brier 0.2266 / log-loss 0.6441** on 6,638 matches (with 23 fallback maps), exactly replicating the Phase 1 ratings baseline.
- **Historical elite walk-forward (Task 5.2):** Draft model scores **Brier 0.2287 / log-loss
  0.6493** with default hyperparameters on 6,638 elite maps (23 fallback). This is *worse*
  than the 0.2266 ratings baseline — draft features do not help over a long, mixed history.
- **TI 2026 event evaluation (Task 7.1):** **Brier 0.2580 vs closing 0.2499**, on **50
  scored maps** of 55 priced (0 fallbacks; 60 of 110 prediction rows have no captured
  closing line).

  **Compare only on the same rows.** The ratings baseline's 0.2993 was measured over **70**
  rows, because `--model ratings` also emits period-0 series prices that the draft model
  never produces — and those series rows alone score 0.3425. Scored on the *identical 50
  map rows*, the comparison is:

  | Model | Brier on the same 50 TI rows |
  |---|---|
  | Ratings | 0.2820 |
  | **Draft** | **0.2580** |
  | Closing market | 0.2498 |

  So the draft model is worth ~0.024 Brier over ratings on maps it can price, and closes
  the gap to the market from 0.032 to 0.008 — a real gain, but roughly half the size the
  earlier 0.2618-vs-0.2993 framing implied. Never quote those two numbers against each
  other again; they are different populations.
- **Adoption decision (Task 7.4):** The draft model is available as `--model draft` on `backtest` and `predict` and powers `dota2bets bet`. Under the strict adoption rule, default CLI behaviour remains `--model ratings` for pre-match predictions where draft picks are not yet known.

#### Live paper-trading workflow (`dota2bets bet`)

During live series draft windows:
```bash
# Rehearsal mode (calculates probabilities and pricing without modifying trade log)
uv run dota2bets bet --radiant am cm juggernaut lina sven --dire axe bane pudge invoker sniper --radiant-team "Team Spirit" --dire-team "Aurora Gaming" --period 2 --rehearse

# Live trade (appends to data/paper_trades.jsonl with edge, Kelly sizing, price and quote time)
# Refuses to record when the quote is older than --freshness-limit (default 300s);
# --allow-stale overrides, which you should only do knowingly.
uv run dota2bets bet --radiant am cm juggernaut lina sven --dire axe bane pudge invoker sniper --radiant-team "Team Spirit" --dire-team "Aurora Gaming" --period 2

# Settle recorded paper trades against match outcomes and closing lines
uv run dota2bets bet --settle
```

## Next steps

1. Re-run `dota2bets resolve`, `predict` and `window_report.py` as TI progresses. Each finished series adds rows to the CLV comparison.
2. Execute live paper trading via `dota2bets bet` during upcoming TI playoff matches.

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
