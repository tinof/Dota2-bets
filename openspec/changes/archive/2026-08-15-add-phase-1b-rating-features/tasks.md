## 1. Rating state and configuration

- [x] 1.1 Add `patch_rd_boost: float | None = None` and `roster_rd_boost: float | None = None`
      to `Glicko2Config`, documenting that `None` disables each feature and that the values are
      in rating points on the same scale as `initial_rd`
- [x] 1.2 Add `last_patch: int | None = None` and `lineup: frozenset[int] | None = None` to
      `TeamState`; confirm `_rate`'s `replace(state, ...)` carries both through a rating update
- [x] 1.3 Add a test that a default-constructed `Glicko2Config` has both boosts `None`, so the
      baseline stays the default

## 2. Patch-transition inflation

- [x] 2.1 Extend `RatingBook._inflated` with a keyword-only `patch: int | None`, widening RD in
      quadrature by `patch_rd_boost` when the boost is set, the patch is known, `last_patch` is
      known, and `patch > last_patch`; cap at `max_rd` as idle inflation does
- [x] 2.2 Thread `patch` through `win_prob` and `update_map` (keyword-only, default `None`), and
      persist `last_patch` in `update_map` only when the patch is known
- [x] 2.3 Test: a team's first map on a newer patch is priced closer to 0.5, and its second map
      on that patch gets no further widening
- [x] 2.4 Test: a first-seen patch is not a transition, and an older patch does not widen
- [x] 2.5 Test: a NULL-patch map neither triggers the boost nor clears `last_patch` — verify the
      59→NULL→59 sequence never fires and 59→NULL→60 fires exactly once
- [x] 2.6 Test: with `patch_rd_boost=None`, probabilities and states are bit-identical to the
      current baseline over a fixture history that carries patches

## 3. Roster-stability inflation

- [x] 3.1 Implement `ratings.load_lineups(conn)` returning `match_id → {team_id: frozenset}`
      from a single JOIN of `match_players` to `matches`, mapping `is_radiant` to the radiant or
      dire team id and dropping any side without exactly five known `account_id`s
- [x] 3.2 Extend `_inflated` with a keyword-only `lineup: frozenset[int] | None`, widening RD by
      `changed * roster_rd_boost` in quadrature where `changed = 5 - len(current & stored)`,
      only when both lineups are known; cap at `max_rd`
- [x] 3.3 Thread lineups through `win_prob` and `update_map` (a `lineups: dict[int, frozenset]`
      keyed by team id), persisting `lineup` in `update_map` only when known
- [x] 3.4 Test: widening is monotone in the number of substitutions (0 < 1 < 3 stand-ins move
      the price progressively toward 0.5), and an unchanged lineup is not penalised
- [x] 3.5 Test: a map with no recorded lineup applies no adjustment and does not erase the
      stored lineup
- [x] 3.6 Test: a substituted team's win moves its rating further than the same win with an
      unchanged lineup (the update-time widening)
- [x] 3.7 Test: `load_lineups` maps sides to the correct team ids and drops partially recorded
      sides, against a fixture database
- [x] 3.8 Test: `win_prob` with both features active leaves all stored state unchanged
      (leak-freeness guard)

## 4. Backtest plumbing

- [x] 4.1 Add `patch` to `rating_rows`'s SELECT; make `apply_row` read it defensively so the
      existing dict-based `_match()` fixtures without a `patch` key keep working
- [x] 4.2 Preload lineups in `walk_forward` only when `roster_rd_boost` is set, and pass patch
      and lineups at both the `win_prob` and `apply_row` call sites
- [x] 4.3 Add `patch` to `SeriesInfo` and freeze series prices in `_frozen_books` and
      `event_predictions` on the first map's patch and lineups
- [x] 4.4 Replace `default_grid()` with the 16-config product of
      `patch_rd_boost × roster_rd_boost ∈ (None, 30, 60, 100)²`, leaving `tau`, `idle_period_s`
      and `initial_rd` at Glickman's defaults; update the existing grid test
- [x] 4.5 Test: `walk_forward` with both boosts set, over a history carrying no patch and no
      lineup data, returns a `ScoreCard` identical to the both-`None` run (the key degradation
      regression)
- [x] 4.6 Test: a frozen series price is unaffected by a lineup change occurring at map 3

## 5. CLI

- [x] 5.1 Add `--patch-boost` and `--roster-boost` (float, default `None`) to `backtest` and
      `predict`, folded into the `Glicko2Config` those commands construct
- [x] 5.2 Print the active feature configuration alongside every scorecard, so a reported Brier
      always states both its population filter and its feature settings
- [x] 5.3 Run `uv run pytest` and `uv run ruff check` — all existing tests plus the new ones green

## 6. Evaluation (gated on the detail crawl finishing)

- [x] 6.1 Confirm the crawl is complete (`tail -1 data/detail.log`) and record the final
      coverage: matches with a patch, and scored rows with lineups on both sides
- [x] 6.2 Re-run the baseline `uv run dota2bets backtest` and confirm it still reads 0.2266 on
      the elite population — if it moved, the enriched data changed the row set, and that must
      be explained before any comparison is made
- [x] 6.3 Ablate each feature alone: `backtest --patch-boost 60` and `backtest --roster-boost 60`
- [x] 6.4 Run `uv run dota2bets backtest --tune` bounded before TI 2026, and record the ranking
- [x] 6.5 Run the winning configuration through `backtest`, then `predict --out
      data/ti2026_preds_1b.jsonl`, then `eval --predictions data/ti2026_preds_1b.jsonl`, all on
      the same day so the TI row set matches the 70-row baseline
- [x] 6.6 Apply the adoption rule: make a feature's boost the default only if both the elite
      Brier (0.2266) and the TI Brier (0.2993) improve; otherwise leave the defaults at `None`
- [x] 6.7 Update `docs/dev-handoff.md` with the outcome — including a negative result, the
      coverage it was measured on, and what it implies for Phase 2
