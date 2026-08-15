## 1. Hero identity

- [x] 1.1 Fetch OpenDota `/constants/heroes` once and commit the trimmed result (id,
      localised name, `npc_dota_hero_` name) as packaged data alongside `aliases.yaml`
- [x] 1.2 Add `heroes.py`: resolve a hero from numeric id, localised name
      (case-insensitive), short name, or a small hand-written alias table; raise naming
      the unrecognised input, with close-match suggestions; reverse lookup by id
- [x] 1.3 Add `tests/test_heroes.py`: each resolution form, alias table, unknown name
      raises and names the input

## 2. Draft statistics and features

- [x] 2.1 Add `draft.py` with the frozen config dataclass (decay half-life, the three
      shrinkage constants, learning rate, disable switch)
- [x] 2.2 Implement the decaying counter used by every statistic (lazy decay on read and
      write against a timestamp) and the shrunk-rate helper
- [x] 2.3 Implement the accumulator over hero, team-on-hero, same-side pair and
      cross-side matchup statistics, updated only after prediction
- [x] 2.4 Implement `load_drafts`: one pass over picked draft events plus the
      first-action row per match, grouped and validated to five distinct heroes per side
- [x] 2.5 Implement the seven-feature vector, radiant-oriented, with pair terms expressed
      net of the individual hero rates
- [x] 2.6 Implement the online logistic combiner: anchor-initialised weights, side
      intercept, predict and learn separated, incomplete drafts falling back to the
      anchor and flagged as such
- [x] 2.7 Document the leak-freeness contract and the same-series-earlier-maps decision in
      the module docstring, matching how `ratings.py` documents its choices

## 3. Draft model tests

- [x] 3.1 Leak-freeness: a match's own result cannot reach its own feature vector; the
      first-ever match yields an all-zero draft feature vector
- [x] 3.2 Decay arithmetic across several half-lives; recent evidence outweighs old
- [x] 3.3 Shrinkage: an unseen hero contributes exactly zero; a single lopsided result
      stays near neutral
- [x] 3.4 Same-side pair key symmetry and cross-side matchup orientation
- [x] 3.5 Team-on-hero familiarity does not leak between teams
- [x] 3.6 First draft action extracted correctly when the draft opens with bans
- [x] 3.7 A side with four picks is excluded from statistics and prices via the anchor
      fallback
- [x] 3.8 The untrained combiner equals the ratings probability adjusted for side; the
      disabled config never moves a non-anchor weight; the combiner learns the right
      direction on a separable synthetic stream
- [x] 3.9 Pin the radiant convention: `draft_events.team = 0` agrees with
      `match_players.is_radiant` on a synthetic match

## 4. Backtest integration

- [x] 4.1 Add the combined walk-forward advancing ratings, draft statistics and combiner
      through one chronological pass, honouring the existing scoring-population and
      minimum-games filters
- [x] 4.2 Report the scored population, its size, the configuration, and the count of
      anchor-fallback maps alongside every score
- [x] 4.3 Add per-map event predictions for a league, emitting both sides before folding
      each map, with the map's index within its series as the period
- [x] 4.4 Add the tuning grid over half-life, pair shrinkage and learning rate, bounded to
      end before the evaluated event's first horn
- [x] 4.5 Wire `--model draft` and its knobs onto `backtest` and `predict`, leaving the
      existing default paths producing today's numbers
- [x] 4.6 Tests: disabled-draft walk-forward reproduces the ratings baseline; rows after
      the bound cannot change a scorecard; per-map predictions emit both sides summing to
      one with correct period indices

## 5. First measurement

- [x] 5.1 Run the disabled-draft elite walk-forward and confirm it reproduces Brier 0.2266
      (harness neutrality — if it does not, stop and fix the fold before trusting anything)
- [x] 5.2 Run the draft-enabled elite walk-forward; record Brier, log-loss, n and fallback
      count against the 0.2266 baseline
- [x] 5.3 Inspect the combiner's weight trajectory over the full history for drift; if
      unstable, switch to the periodic refit described in design.md

## 6. Live paper trading

- [x] 6.1 Add the `stake` field to the prediction record and an append-only writer
- [x] 6.2 Add the `bet` command: resolve teams via the existing alias resolver and heroes
      via `heroes.py`, require ten distinct heroes, replay history to now, print the model
      probability
- [x] 6.3 Fetch the current open line for the map's period from the parentless pre-match
      event, de-vig it, and print the market probability, the offered price, the
      observation age, the edge and the suggested bounded stake
- [x] 6.4 Warn hard when the quote is older than the freshness limit; record nothing when
      no open quote exists, and say why
- [x] 6.5 Append the paper trade with the taken price, stake and timestamp to the ignored
      data location; add a rehearsal mode that writes nothing
- [x] 6.6 Add settlement reporting over trades carrying a price: count, staked, realised
      profit, closing-line value, with pending trades reported as pending
- [x] 6.7 Tests: rehearsal leaves the log byte-for-byte unchanged; a real run appends a
      well-formed record whose price matches the seeded snapshot; no open quote records
      nothing; settlement arithmetic on a hand-built report
- [x] 6.8 Rehearse `bet` against a live TI series before relying on it under time pressure

## 7. Event evaluation and adoption

- [x] 7.1 Produce per-map TI predictions with the draft model and evaluate them against
      the closing line; record Brier, n and fallback count against the 0.2993 model and
      0.2508 closing baselines
- [x] 7.2 Record the pre-draft market reference number for the same rows
- [x] 7.3 Run the tuning grid bounded before TI and lock the configuration
- [x] 7.4 Apply the adoption rule: enable draft features by default only if both the elite
      and event benchmarks improve; record the decision either way
- [x] 7.5 If the combined feature set fails, ablate individually — anchor recalibration
      alone first, then the matchup term alone — and record each result

## 8. Close out

- [x] 8.1 `uv run ruff check .` and the full suite green, no network needed
- [x] 8.2 Update `docs/dev-handoff.md` with the numbers, their populations, the adoption
      decision, and the live paper-trading workflow
- [x] 8.3 Update `CLAUDE.md` with the draft-model gotchas worth not rediscovering
- [x] 8.4 Sync the delta specs into the main specs and archive the change
