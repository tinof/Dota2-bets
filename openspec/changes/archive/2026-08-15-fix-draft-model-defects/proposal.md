## Why

Verification of the archived `add-draft-model` change found three defects that the test
suite passed straight over. Two of them sit on the live money path and would have
corrupted the CLV record that Phase 2 exists to produce; the third was quietly cancelling
the draft model's own signal. All were verified against the real database, not only in
tests, and all are now fixed.

## What Changes

- **A pair or matchup statistic with no observations now contributes exactly zero.** It
  previously returned the negated individual hero rates, which turned the synergy and
  counter features into inverted copies of the hero win-rate feature. On the same 50 TI
  rows this moved the draft model from Brier 0.2618 to **0.2580** (ratings 0.2820,
  closing market 0.2498).
- **`bet` no longer quotes a market that has closed or been pulled.** It takes the newest
  snapshot per line regardless of status and then requires that row to be open and
  non-live. In the current database 436 of 486 non-open moneyline lines still carry an
  earlier `open` row that the old query would have traded.
- **Quote age is measured from the oldest leg, and a stale quote is refused by default.**
  Age was taken from the freshest leg, so a two-hour-old price on the traded side printed
  as seconds old and skipped the warning. Recording a stale trade now requires
  `--allow-stale`.
- The observed quote time is stored on each trade (`quoted_at`), the event prediction path
  reports its population and fallback count, `"map not played"` settles as pending rather
  than unresolved, `bet` opens the database read-only, and the anchor fallback uses the
  trained anchor weight rather than a hard-coded 1.0.
- Documentation corrected: the previously reported TI improvement compared 50 draft rows
  against 70 ratings rows. Both `CLAUDE.md` and `docs/dev-handoff.md` now carry the
  same-row comparison and warn against repeating the mistake.

## Capabilities

### New Capabilities

None. This change repairs behaviour that the existing specs already required.

### Modified Capabilities

- `paper-trading`: the staleness requirement tightens from "refuse or prominently warn" to
  refusing by default with an explicit override, and the available-price requirement now
  pins that a later `closed`/`gone` observation supersedes an earlier `open` one and that
  the observation time is stored.

## Impact

- Modified: `src/dota2bets/draft.py` (evidence-scaled pair corrections, docstring),
  `src/dota2bets/cli.py` (`bet` quote selection, staleness gate, read-only handle),
  `src/dota2bets/evaluation.py` (`quoted_at`, pending classification),
  `src/dota2bets/backtest.py` (event-path population reporting).
- Tests: 195 → 203. Eight added, covering unseen-pair zeroing, evidence scaling, state
  immutability under repeated pricing, closed-after-open refusal, stale refusal,
  oldest-leg age, and append-preserves-earlier-records. One vacuous test guard removed.
- `event_predictions_draft` now returns `DraftEventPredictions` instead of a bare list.
- No schema migration, no new dependency, no change to recording or enrichment.
