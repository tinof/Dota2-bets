## Context

See `proposal.md` — Why. The design constraints come from the existing code, not from new
requirements:

- `RatingBook.win_prob` inflates *copies* of team state; `update_map` is the only mutator.
  Both go through one private helper, `_inflated(team_id, at)`, which today grows RD for
  idle time. That helper is the single natural seam for any further uncertainty widening.
- `_rate()` returns `replace(state, ...)`, so fields added to `TeamState` survive a rating
  update automatically — no per-field plumbing needed inside the Glicko arithmetic.
- The whole history must stay a single chronological pass over ~62k maps (`rating_rows` →
  `apply_row`), because that pass is what makes every prediction strictly pre-horn. Any
  per-map database lookup would turn one pass into 62k queries.
- Data coverage is partial and will stay partial: `matches.patch` and `match_players` are
  populated for 17,921 of 64,900 matches while the detail crawl runs. Patches 58, 59 and 60
  are represented, so patch transitions are already observable in the enriched slice.
  `match_players.account_id` has no NULLs so far.
- `Glicko2Config` is a frozen dataclass already threaded through `walk_forward`, `tune`,
  `_frozen_books` and the CLI.

## Goals / Non-Goals

**Goals:**

- Add both features at the one seam (`_inflated`) so prediction and learning stay consistent
  by construction rather than by two parallel code paths kept in sync by hand.
- Keep the historical replay O(n) with the features on.
- Make "feature disabled" and "feature enabled but data absent" both bit-identical to the
  Phase 1 baseline, and prove it with a test rather than by inspection.

**Non-Goals:**

- Re-tuning `tau`, `idle_period_s` or `initial_rd`. That grid was flat to the fourth
  decimal; the grid budget moves to the new knobs.
- Hero/draft features. That is Phase 2 and reads the `draft_events` table, not `match_players`.
- Player-level ratings. A per-player rating system would subsume the roster feature, but it
  is a much larger change and this one has to be measurable first.
- Backfilling patch data for the pre-crawl history by inference from dates.

## Decisions

### RD inflation on patch transition, not per-patch rating tables

`TeamState` gains `last_patch: int | None`. In `_inflated`, when the current map's patch is
known, the team has a stored patch, and the current patch is greater, RD grows in quadrature
by `patch_rd_boost`, capped at `max_rd` exactly as idle inflation is:

```python
phi = math.sqrt(phi * phi + (boost / GLICKO_SCALE) ** 2)
phi = min(phi, self.config.max_rd / GLICKO_SCALE)
```

`update_map` persists `last_patch` only when the patch is known.

*Alternative rejected:* separate rating books per patch. It discards cross-patch information
(most teams' strength is largely patch-invariant), it has no answer for the ~72% of rows
whose patch is NULL, and it multiplies cold starts — the opposite of what a 62k-row history
is for. Treating a patch as an information shock keeps one book and reuses the mechanism the
system already trusts for idle time.

*Leak-freeness:* the live patch is public days before any horn, so conditioning on the
current map's patch uses no post-horn information.

### Roster changes widen RD at prediction *and* update, with no mu shrink

Lineup is `frozenset` of the five `account_id`s a team fielded, stored on `TeamState`.
With both the current and stored lineups known, `c = 5 - len(current & stored)` changed
players widen RD by `c * roster_rd_boost` in quadrature, capped at `max_rd`.

Applying it at update time as well is deliberate: a widened RD makes the Glicko step larger,
so a rebuilt roster's first results move its rating faster — which is the behavioural point
of the feature, not a side effect.

*Alternative rejected:* shrinking `mu` toward the mean. `win_prob` already pulls toward 0.5
through the combined RD, so shrinking `mu` too would double-count the same doubt, and it
would corrupt the rating itself rather than expressing uncertainty about it.

*Alternative rejected:* comparing against a rolling window of recent lineups rather than the
single last one. It is more robust to a one-off substitution but needs a per-team history and
a decay rule — more knobs to tune on data we have not yet measured. Start with last-lineup;
the ablation will say whether the feature is worth refining.

### Absent data is unknown, never a default

Neither feature fires when its input is missing on either side of the comparison, and a map
with missing data never overwrites what was stored. Concretely: a NULL patch neither triggers
the boost nor clears `last_patch`; a map with no recorded lineup leaves `lineup` intact.

The alternative — treating "no recorded patch" as "same patch" — would silently assert
stability across a two-year window where the field is mostly NULL, and would then fire a
spurious boost on the first enriched row. Treating a first-seen patch as a transition would
fire the boost once for every team at the crawl boundary, which is an artefact of our data
collection, not of the game.

### Lineups preloaded once into a dict

New `ratings.load_lineups(conn) -> dict[int, dict[int, frozenset[int]]]`, keyed
`match_id → {team_id: lineup}`, built from one JOIN of `match_players` to `matches` mapping
`is_radiant` to `radiant_team_id` / `dire_team_id`, grouping in Python and dropping any side
without exactly five known account ids. Called once per book build, and only when
`roster_rd_boost is not None`, so a roster-off tuning pass costs nothing.

At ~62k matches × 10 players this is a few hundred thousand rows — well within memory, and it
keeps the replay a single pass.

### Two config fields rather than a new feature-config object

`Glicko2Config` gains `patch_rd_boost: float | None = None` and
`roster_rd_boost: float | None = None`. `None` means disabled and is the default, so every
existing construction of `Glicko2Config` keeps its current behaviour untouched. A separate
`FeatureConfig` would have to be threaded through `walk_forward`, `tune`, `_frozen_books`,
`event_predictions` and the CLI in parallel with the config that already goes there.

### Threading through the backtest

`rating_rows`'s SELECT adds `patch`. `win_prob`, `update_map` and `apply_row` gain
keyword-only optional feature arguments, all defaulting to `None`. `apply_row` reads the
patch defensively (`row["patch"] if ... else None`) because existing tests pass plain dicts
built by the `_match()` helper.

`_frozen_books` freezes a series price at the first map's horn, so it conditions on map one's
patch and lineup; `SeriesInfo` gains `patch`. A roster change between map 1 and map 3 does not
retroactively move the frozen series price — that is correct, since the price was quoted
before those maps existed.

`default_grid()` becomes the 16-config product of
`patch_rd_boost × roster_rd_boost ∈ (None, 30, 60, 100)²` at Glickman's defaults for the rest.
The boost magnitudes are in rating points, on the same scale as `initial_rd = 350`, so 30–100
spans "a mild nudge" to "roughly a third of a fresh team's uncertainty".

## Risks / Trade-offs

- **Evaluation on a partially enriched history.** The crawl runs newest-first, so the enriched
  slice is recent and elite-biased; features will look better on it than on the full history.
  → Report all Phase 1b numbers with their coverage, and treat any measurement taken before
  the crawl finishes as provisional. The go/no-go comparison waits for full coverage.
- **Two features tuned on one grid invite overfitting**, particularly with only 70 TI rows on
  the event benchmark. → 16 configs is a deliberately small grid; the tuning pass is bounded
  strictly before the event; adoption requires both benchmarks to move, which a noise-fitted
  config is unlikely to manage.
- **Roster data is thinner than patch data**, since the five-known-ids rule drops any partially
  recorded side. → Graceful degradation is a spec requirement, not a fallback; the ablation
  reports how many scored rows actually had lineups on both sides.
- **A widened RD at update time makes ratings noisier**, which could hurt the elite benchmark
  even where the feature helps prediction. → That is exactly what the per-feature ablation is
  for; `None` remains the default until the numbers say otherwise.
- **The features may simply not work.** The gap to the market is 0.05 Brier and the handoff
  already predicts that ratings-level features will not close it. → A negative result is a
  legitimate outcome of this change: it is recorded in the handoff, the defaults stay `None`,
  and Phase 2 proceeds with the seam and the data loader already built.

## Migration Plan

No data migration and no schema change. The features are off by default, so merging changes no
existing behaviour; enabling them is a config or CLI flag. Rollback is setting both boosts back
to `None`.
