## Why

The Phase 1 Glicko-2 book prices team strength and nothing else: it scores Brier 0.2993
against the closing market's 0.2508 on the same 70 TI 2026 rows. Two of the things the
market prices and the model does not — a balance patch landing, and a team fielding a
stand-in — are now available in our own data, because the OpenDota detail crawl has filled
`matches.patch` and `match_players` for 17,921 matches spanning patches 58, 59 and 60.
Detail coverage was the binding constraint on Phase 1; it no longer is, so the two features
the handoff scheduled as Phase 1b can be built.

## What Changes

- **Per-patch rating windows.** A team's first map on a patch newer than the one it last
  played inflates its rating deviation, the same way idle time already does. A new patch is
  an information shock, so the book becomes less certain and learns faster from what follows.
- **Roster-stability penalty.** A lineup is the five `account_id`s a team fields. When the
  current lineup differs from that team's last known lineup, rating deviation widens in
  proportion to the number of changed players, both when pricing the map and when updating
  from its result.
- Both features are **off by default** (`None`), and both **degrade to the exact Phase 1
  baseline** wherever patch or lineup data is missing — which is most of the 64,900-match
  history until the crawl finishes. Unknown is treated as unknown, never as "same patch" or
  "stable roster".
- `dota2bets backtest` and `dota2bets predict` gain `--patch-boost` / `--roster-boost` so
  each feature can be ablated on its own, and `backtest --tune` grids the two boost values
  instead of re-tuning `tau` and the idle period (that grid was flat to the fourth decimal).
- Adoption is conditional, not assumed: a feature ships enabled by default only if it
  improves **both** the elite walk-forward Brier (0.2266) and the TI Brier (0.2993). This
  proposal covers building and measuring the features; if neither moves both numbers, the
  defaults stay `None` and the negative result is recorded.

## Capabilities

### New Capabilities
- `team-ratings`: the Glicko-2 rating book that turns decided pro maps into pre-match win
  probabilities — how ratings carry forward, how uncertainty widens over idle time, patch
  changes and roster changes, and the leak-freeness and graceful-degradation rules that
  every rating input must obey.

### Modified Capabilities
<!-- None. This is the repository's first OpenSpec change, so no spec exists yet to modify. -->

## Impact

- `src/dota2bets/ratings.py` — `Glicko2Config` gains two boost knobs; `TeamState` gains
  `last_patch` and `lineup`; the shared inflation path gains the two widenings; new
  `load_lineups()` reader.
- `src/dota2bets/backtest.py` — `rating_rows` selects `patch`; the walk-forward, tuning,
  frozen-series and event-prediction paths thread patch and lineup through; `default_grid()`
  changes shape.
- `src/dota2bets/cli.py` — two new optional flags on `backtest` and `predict`.
- `tests/test_ratings.py`, `tests/test_backtest.py` — new coverage, principally the
  regression that proves an all-`None` configuration reproduces Phase 1 exactly.
- No schema migration: `matches.patch` and `match_players` already exist and are populated
  by the running crawl. No new dependencies. No change to the recorder or the evaluation
  harness, so odds capture is untouched.
- Evaluation is gated on the detail crawl finishing (~62,900 matches; 17,921 done at the
  time of writing). The implementation and its tests are not gated — they run against
  synthetic fixtures.
