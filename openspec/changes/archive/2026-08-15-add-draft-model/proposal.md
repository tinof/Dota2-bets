## Why

Team ratings alone do not beat the market: Glicko-2 scores Brier 0.2993 on TI 2026 rows
against the closing line's 0.2508, and Phase 1b established that rating-level adjustments
cannot close that gap. The information the ratings never see is the draft — and the market
provably leaves map-2 and map-3 moneylines open through their drafts (11 of 11 observed
periods, quoted at the horn, median max stake $2,500). That window is the whole premise of
the project, 1.54M draft events are already stored and entirely unconsumed by any model,
and TI 2026 ends 23 August, so the live odds to test against expire in about a week.

## What Changes

- Add a draft-aware map-winner model: hero, synergy, counter and team-familiarity
  statistics accumulated strictly chronologically, combined with the existing Glicko-2
  probability by an online logistic layer that is initialised to reproduce the ratings
  model exactly.
- Add hero identity resolution (id ↔ name) so drafts can be entered and read by humans.
- Extend the backtest with a draft-model walk-forward and per-map event predictions,
  scored on the same elite population and against the same closing lines as Phase 1.
- Add a live command that prices a map from its ten picked heroes against the still-open
  market line, and records the result as a paper trade with the price and stake it would
  have taken.
- Extend evaluation to settle paper trades into realised profit and closing-line value.
- No change to how odds are recorded, how matches are enriched, or how ratings work.
  The draft model is additive; with its features disabled it reproduces the current
  numbers.

## Capabilities

### New Capabilities

- `draft-model`: turning a completed draft into a map win probability — what the model may
  know at prediction time, how unseen heroes and incomplete drafts are handled, and what
  must be true before draft features are adopted as the default.
- `paper-trading`: recording a model price against a live market line as a hypothetical
  bet, and settling those records into profit and closing-line value.

### Modified Capabilities

None. The `team-ratings` requirements are unchanged — the draft model consumes the rating
probability as an input and adds no requirement to it. `match-enrichment` already supplies
the draft events this change reads.

## Impact

- New: `src/dota2bets/draft.py`, `src/dota2bets/heroes.py`, packaged hero constants,
  `tests/test_draft.py`, `tests/test_heroes.py`.
- Modified: `src/dota2bets/backtest.py` (draft walk-forward, per-map event predictions,
  tuning), `src/dota2bets/evaluation.py` (stake field on a prediction, paper settlement),
  `src/dota2bets/cli.py` (`--model draft` on `backtest`/`predict`, new `bet` command).
- Read-only consumers of existing structures: `ratings.RatingBook`, `rating_rows`,
  `evaluation.closing_event` / `closing_lines`, `aliases.AliasResolver`, `draft_events`.
- No schema migration, no new dependency — the repo stays pure-stdlib for modelling.
- One manual fetch of OpenDota's hero constants is committed to the repo; no new runtime
  API dependency and no additional quota use.
