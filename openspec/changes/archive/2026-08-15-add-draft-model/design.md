## Context

See proposal.md — Why. The design-relevant state:

- `ratings.rating_rows(conn, before)` already yields decided, team-identified maps in
  chronological order, and `backtest.walk_forward` already enforces predict-then-update
  over that stream for the Glicko-2 `RatingBook`. The draft model is a second and third
  stateful object folded through the same stream.
- `draft_events(match_id, ord, is_pick, hero_id, team)` is complete for all 64,900 matches.
  `team = 0` is radiant: verified empirically against `match_players.is_radiant` over
  320,699 picked-hero rows, with no contradicting row.
- `evaluation.clv_report` bounds the closing line at the map's OpenDota horn via
  `map_start`, and only against the parentless (pre-match) Pinnacle event. `Prediction`
  already carries optional `price_taken` and `placed_at`.
- The modelling code is pure standard library. No numpy anywhere in the repo.
- TI 2026 ends 23 August. A usable live path within the first day of work is worth more
  than a better model delivered after the event.

## Goals / Non-Goals

**Goals:**

- One chronological fold that advances ratings, draft statistics and combining weights
  together, so leak-freeness is enforced in a single place rather than per feature.
- A combined model whose untrained state is exactly the current ratings model, bounding
  the downside of the whole phase.
- An end-to-end live path — enter ten heroes, get a price against the open line, log the
  paper trade — before feature work is finished.

**Non-Goals:**

- Fetching drafts automatically from a live API. Manual entry of ten heroes takes about
  thirty seconds during a real draft; autofetch is a later enhancement, and OpenDota's
  live endpoint is delayed and unreliable on draft fields.
- Modelling kills or totals markets. Moneyline only.
- Real bet execution. Paper trades only.
- Per-player or lane-role features. `lane_role` is null until a replay is parsed.

## Decisions

**Freeze state at each map's horn, not at the series horn.** Phase 1's
`backtest._frozen_books` freezes at a series' first horn because a ratings model bets
pre-series. The draft model bets during map *k*'s draft, at which point maps 1..*k*−1 of
the same series are decided and public — and the still-open Pinnacle map-2/3 line has
seen them too. Since `rating_rows` is per-map chronological, the natural fold gives this
for free; no `_frozen_books` variant is needed for the backtest, and the live path
replays to "now", which includes earlier maps trivially. *Alternative rejected:* freezing
at the series horn, which would price map 3 without knowing the series is 1–1 — strictly
less information than the market has, and not what the window experiment measured.

**Exponential time decay of hero statistics rather than patch windows.** Every hero, pair
and matchup counter decays with a configurable half-life, applied lazily on read and
write against the timestamp — the same trick by which Glicko RD inflation *is* the decay
in `ratings.py`. This is one tunable knob instead of a patch-boundary table, and it
degrades gracefully across a balance patch instead of discarding history at a cliff.
*Alternative rejected for the baseline:* per-patch windows. `matches.patch` is exact and
available, so a patch-reset variant remains open as follow-on work if decay proves too
blunt.

**Shrinkage toward neutral, expressed as `(w + k·0.5)/(n + k)` and used as `rate − 0.5`.**
An unseen hero or pair contributes exactly zero, which is how the spec's "unseen
contributes nothing" requirement is satisfied structurally rather than by a special case.
Pair terms use the pair rate minus the mean of the two individual hero rates, so synergy
and counter features do not restate the single-hero feature. `k_pair` is large (order 100)
because 126 heroes give roughly 8,000 same-side pairs and 16,000 matchups — sparse enough
that unshrunk rates would be noise.

**An online logistic combiner initialised to the ratings model.** Weights start at
`[anchor = 1.0, everything else = 0]` with the intercept at the logit of the empirical
radiant win rate, so the cold model *is* Glicko plus side. Updates are one AdaGrad-scaled
gradient step per map, after prediction. This makes the "disabled features reproduce the
baseline" and "untrained combiner equals the ratings model" requirements nearly free, and
costs about twenty lines with no new dependency. *Alternative held in reserve:* periodic
IRLS refit every few thousand matches over the walk-forward-computed (hence still
leak-free) feature rows — a pure-Python 8×8 Newton solve — if the SGD weight trajectory
looks unstable over the full history.

**Baseline feature set kept to seven, all oriented toward radiant:** the Glicko logit
(anchor), hero win-rate difference, team-on-hero familiarity difference, synergy
difference, counter sum, first-draft-action indicator, and a both-teams-warm indicator
that lets the model discount its own anchor for teams with little history. Natural ranges
sit around ±1.5, so no standardisation layer is needed. Small is deliberate: with roughly
6,600 elite scoring rows, a wide feature set would fit noise, and each feature has to be
individually ablatable to satisfy the adoption rule.

**Hero constants as a committed package data file.** One manual fetch of OpenDota's
`/constants/heroes`, stored next to the code the way `aliases.yaml` already is. Needed at
command-parse time, works offline, no migration, no quota. Name resolution is exact —
localised name case-insensitively, the `npc_dota_hero_` short name, the numeric id, and a
small hand-written alias table for the entrenched community names. No fuzzy matching, for
the same reason `aliases.py` forbids it: a wrong resolution is silent and corrupts
everything downstream.

**Reuse the evaluation machinery unchanged.** Post-draft predictions evaluate through
`clv_report` as-is, because its closing bound is already the map horn and is therefore
post-draft by construction — that is the honest bar, since the market also saw the draft.
`--before-draft` continues to supply the pre-draft market as a reference. The only
additions are an optional `stake` field on `Prediction` and a settlement summary derived
from an existing report.

## Risks / Trade-offs

- **Draft features may not close the gap to the closing line.** Much of that 0.05 is
  roster news, form and order flow that a draft does not carry. → The anchor-initialised
  combiner bounds the downside to roughly the ratings model. Before assuming heroes are
  the answer, test whether recalibrating the anchor alone (intercept and slope only)
  already narrows the TI gap; if Glicko is miscalibrated at the tails that is free Brier.
  If hero and synergy terms are flat, the counter matrix is the most plausible remaining
  signal and gets tried alone with a shorter half-life. Worst case the live command still
  ships as an anchor-versus-market paper-trading tool — the pipeline is the durable asset.
- **The TI population is 70 rows.** Brier differences of ±0.03 are noise at that size. →
  The elite walk-forward (n ≈ 6,600) is the real gate; the event number is directional
  evidence, and both must be reported with their sizes per the spec.
- **Online SGD can drift on a long non-stationary history.** → Inspect the weight
  trajectory over the full fold as an explicit implementation step; the IRLS refit above
  is the prepared fallback.
- **A silent flip of the radiant/dire convention would invert every draft feature.** →
  The convention is asserted by a test cross-checking `draft_events.team` against
  `match_players.is_radiant`, not merely assumed from the empirical check done here.
- **A live quote may be stale by up to the poll interval.** The recorder polls at 60s. →
  The live command prints the observation age, warns hard past a freshness limit, and
  stores the observation time in the trade record so settlement is honest about it.
- **Replaying the full history per live pricing call costs 10–30 seconds.** → Acceptable
  during a draft window that stays open for minutes. A state cache keyed on the newest
  folded match is deliberately deferred, since a stale cache is a correctness risk and the
  latency is not binding.
- **Typing ten heroes under time pressure invites error.** → Strict validation of ten
  distinct heroes, refusal with the offending input named, and a rehearsal mode.

## Migration Plan

Additive throughout: no schema migration, no change to recording, enrichment or ratings.
The new model is reachable only through an explicit `--model draft` selection and a new
command, so the existing `backtest`, `predict` and `eval` paths keep producing today's
numbers. Rollback is dropping the selection flag. The draft feature set becomes the
default only if it clears both benchmarks, and that decision is recorded in the handoff
alongside the numbers and populations that produced it.
