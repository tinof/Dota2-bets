# Dev handoff

State as of 2026-08-13, end of the Phase 0 session. TI 2026 runs 10–23 Aug, so roughly
ten days of the best odds-capture window of the year remain at the time of writing.

Read [`research.md`](research.md) for why the project is shaped this way; this file is
only about where the code stands and what to do next.

## Where things stand

One commit (`de68260`, "Phase 0: pro-match ingestion and odds line recorder"), clean tree,
29 tests passing, ruff clean. Everything below has been exercised against the live APIs.

```
src/dota2bets/
  storage.py            SQLite schema, idempotent upserts, odds change-detection
  opendota.py           OpenDota client + parsers (fetch and parse kept separate)
  odds/base.py          the quote-dict contract every source normalises into
  odds/pinnacle.py      Pinnacle guest API — working, the sharp reference line
  odds/theoddsapi.py    written but never run; needs ODDS_API_KEY
  cli.py                backfill / detail / record-odds / status
tests/                  parser + storage tests against recorded fixtures, no network
```

Database contents right now (`data/dota2bets.sqlite`, gitignored):

| | count | note |
|---|---|---|
| matches | 300 | spans only ~2.5 weeks — nowhere near enough to train on |
| matches with detail | 8 | drafts, players, per-minute gold/XP all verified correct |
| odds snapshots | 386 | **one capture instant only**, from two `--once` runs |
| odds events | 9 | all TI 2026 |

## What is proven, and what is not

Proven against live data: OpenDota ingestion (24 pick/bans per match with 10 picks, as
captains mode requires; per-minute gold/XP series; rosters), Pinnacle pre-match capture
across moneyline/spread/total/team_total for both series and per-map periods, and the
change-detection path (a second poll wrote 0 new rows and skipped all 386 unchanged).

**Not proven — treat as the first thing to verify:**

- **Live capture has never run.** `is_live` is 0 on every row in the database. The
  recorder has only ever executed in `--once` mode against pre-match markets. Nothing
  guarantees the in-play path works until it is watched during an actual game.
- **The recorder has never run continuously.** No durable service exists yet.
- `theoddsapi.py` is untested against the real API and its Dota 2 coverage is unconfirmed.

## The finding that should drive the next session

The project's core thesis is that there is an exploitable window between draft completion
and game start. The captured data puts that partly in doubt and partly in play:

- Every **map-1 and series-level** market has `cutoff_at` exactly equal to the scheduled
  series start (a round `02:00:00`). Taken literally, those markets close *before* the
  map-1 draft, so the draft window would not exist for map 1 on Pinnacle.
- But the one series already in progress (LGD vs Resilience, started 09:45) carried a
  **map-3 moneyline with `cutoff_at = 14:32:48.807`** — a precise sub-second timestamp
  about five hours after the series began. Scheduled cutoffs are round numbers; this is
  the signature of a market closed by a real event, which is consistent with later-map
  markets staying open through their drafts and closing at the horn.

If that reading is right, the target is **maps 2 and 3 of an in-progress series, not map
1** — still most maps at a Bo3 event, but it changes what to watch. Settling this needs
observation during a live series, and TI is the opportunity.

**The experiment:** while a series is live, check whether its map-2/map-3 markets stay
`status = open` during the draft, and whether prices move once picks lock. Answer decides
whether Phase 2 proceeds as designed or retargets toward live betting.

## Priorities for next session

1. **Make the recorder durable and verify live capture.** A launchd job so it survives
   terminal close and sleep, and a tighter interval than 60s while games are on. Then
   confirm rows land with `is_live = 1`. This has a hard deadline — TI odds history
   cannot be reconstructed afterwards.
2. **Run the window experiment** above against the accumulated data.
3. **Backfill history properly** (unblocked, can run in parallel). See the rate-limit
   constraint below — size it before starting rather than discovering the ceiling
   mid-crawl.
4. **Phase 1 ratings** (Glicko-2/Bradley-Terry per patch window, roster stability),
   scored by log loss/Brier and calibration against de-vigged closing odds — never raw
   accuracy.

## Constraints and gotchas worth not rediscovering

**OpenDota rate limits are the backfill bottleneck.** Match detail costs one API call per
match, throttled at 1.2s, against a free-tier ceiling around 2,000 calls/day — so a
year-scale backfill is roughly a week of polite crawling. Either register an
`OPENDOTA_API_KEY` or move detail fetching to STRATZ GraphQL, which can batch many
matches per request. `iter_pro_matches` paging is cheap; only `detail` is expensive.

**Pinnacle returns moneyline selections two different ways.** Some payloads use
`designation: home/away`, others a numeric `participantId` (assigned in participant
order). `normalise` resolves both to team names deliberately — if that is ever
"simplified" to pass through the raw designation, the same market will produce two
different `line_key`s and change-detection will silently break, turning the line history
into a poll log. `test_line_key_is_stable_across_price_moves` guards the related case.

**Summary re-ingestion must not clobber detail.** `upsert_matches` merges with
`COALESCE` rather than `INSERT OR REPLACE`, because a later `/proMatches` sweep would
otherwise null out `patch` and `detail_fetched_at` on already-detailed matches — which
would silently re-queue them for expensive refetching. Covered by
`test_summary_reingest_preserves_fetched_detail`.

**Most quoted lines are "Kills" markets.** 336 of 386 captured lines have
`units = 'Kills'` (kill handicaps and totals) rather than win markets. That count is
inflated by alternate lines, so it reflects quoted depth rather than money. Still worth
acting on: draft composition predicts *kill volume* much more directly than it predicts
*who wins* — a teamfight lineup versus a split-push lineup is a strong prior on total
kills while both drafts can be near-even on win probability. Phase 2 should predict kill
totals alongside win probability rather than treating them as an afterthought.

**`lane_role` is null on unparsed matches.** It only populates once OpenDota has parsed
the replay. Don't build features assuming it is always present.

**Odds evaluation, always.** Compare against de-vigged bookmaker probabilities and track
closing-line value. Accuracy alone cannot distinguish an accurate model from a profitable
one, and no published study demonstrates a profitable ML strategy in Dota 2 markets.

**Avoid tier-3 matches.** Softest markets, but match-fixing risk makes that softness
adverse selection rather than edge.

## Commands

```bash
uv sync
uv run pytest                                   # no network needed
uv run dota2bets backfill --max-matches 1000
uv run dota2bets detail --limit 200             # slow; see rate limits above
uv run dota2bets record-odds                    # 60s poll; leave running
uv run dota2bets record-odds --once             # single cycle, for debugging
uv run dota2bets status
```

Env vars: `OPENDOTA_API_KEY` (raises rate limits), `ODDS_API_KEY` (enables
`record-odds --sources pinnacle theoddsapi`).

## Open question for the user

`papers/thesis.pdf` is not a Dota 2 document — it is a 2006 Waikato MSc thesis on
multi-instance learning algorithms, presumably background for the multi-instance-learning
esports paper rather than a source in its own right. Confirm whether a different thesis
was meant to be there.
