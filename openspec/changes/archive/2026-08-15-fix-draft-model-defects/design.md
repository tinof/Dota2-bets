## Context

See proposal.md — Why. Three defects found while verifying the archived `add-draft-model`
change, all confirmed against the live database rather than only in tests:

- `pair_synergy` and `matchup_edge` subtracted the two heroes' individual rates
  unconditionally. With no observations of the pair the shrunk pair term is 0, so the
  function returned `-0.5*(r1+r2)` — a value driven entirely by a feature the model
  already had. Measured: `pair_synergy(1, 999) = -0.155` for a hero never played.
- `bet` selected `WHERE status='open' ... ORDER BY captured_at DESC`. `odds_snapshots` is
  append-only with one row per status change, so the newest *open* row survives forever
  even after a `closed` or `gone` row supersedes it.
- Quote age used `max(captured_at)` across both selections, i.e. the freshest leg.

## Goals / Non-Goals

**Goals:**

- Make the fixes structural rather than special-cased, so the same class of bug cannot
  reappear: shrinkage that yields exactly zero, and a "latest row wins" rule that matches
  how the recorder already tombstones.
- Pin each fix with a test that fails on the old code.

**Non-Goals:**

- Re-tuning the model. The evidence fix changes feature magnitudes substantially, so the
  locked hyperparameters are now stale; re-tuning is follow-on work, not part of a repair.
- Revisiting the adoption decision. The elite benchmark is unchanged at 0.2287 against the
  0.2266 baseline, so the default correctly stays `--model ratings`.

## Decisions

**Scale the net-of-hero-rates correction by `evidence_weight(n, k) = n/(n+k)` rather than
special-casing `n == 0`.** The correction exists so pair terms do not restate the hero
term; that argument only holds to the extent the pair has been observed. A threshold would
leave a discontinuity and still overweight a pair seen twice; the ratio degrades smoothly
and is zero at zero by construction, which is what the spec requires. *Alternative
rejected:* `if n == 0: return 0.0`, which fixes the reported symptom and leaves the
near-collinearity for thinly observed pairs.

**Take the newest snapshot per `line_key`, then test its status.** This is the rule
`storage` already uses when deciding whether a line is tombstoned, so the live path and the
recorder now agree on what "available" means. Filtering by status inside the query is what
allowed a superseded row to win. *Alternative rejected:* excluding lines that have any
later non-open row, which would be equivalent but re-derives a rule that already exists.

**Put the staleness gate at the recording step, not before the report.** The spec requires
a rehearsal to perform every step of pricing and reporting while writing nothing; refusing
early would have made a stale quote unrehearsable. Reporting always happens; only the write
is gated, and `--allow-stale` overrides it.

**Store `quoted_at` separately from `placed_at`.** They answer different questions — when
the decision was made, and when the price was seen. Collapsing them would make it
impossible to audit after the fact how stale a recorded price was.

**Return `DraftEventPredictions` from the event path instead of a bare list.** A score is
meaningless without its population, and the event path is exactly where the headline TI
number is produced. Making the count part of the return type means a caller cannot report
the number without having been handed the population.

## Risks / Trade-offs

- **The locked hyperparameters were tuned against the broken features.** Feature
  magnitudes fell from roughly 3–12 to roughly 0.05, so `k_pair`, the half-life and the
  learning rate are no longer chosen for the model that now exists. → Recorded as
  follow-on work; the pre-TI tuning bound still applies when it is redone.
- **`--allow-stale` can be habituated.** A flag that is always passed is not a guard. →
  The refusal names the age and the limit each time, and `quoted_at` is stored so an
  overridden trade remains auditable at settlement.
- **The elite benchmark did not improve** (0.2287, essentially unchanged). The
  collinearity was not what made the draft model worse over long history, so the
  underlying question of why it underperforms there is still open.
- **`event_predictions_draft`'s return type changed**, breaking any external caller. →
  Only two call sites exist, both in-repo, both updated.
