## 1. Unseen pairs must contribute nothing

- [x] 1.1 Add `evidence_weight(n, k)` and scale the net-of-hero-rates correction in
      `pair_synergy` and `matchup_edge` by it
- [x] 1.2 Correct the module docstring: reads are pure, and unseen pairs contribute exactly
      zero via evidence scaling
- [x] 1.3 Use the trained anchor weight in the incomplete-draft fallback instead of a
      hard-coded 1.0
- [x] 1.4 Tests: unseen pair and matchup return exactly 0.0 while observed ones still carry
      signal; the correction grows with evidence; repeated pricing leaves the accumulator
      byte-identical; an unseen hero moves no pair-derived feature

## 2. Never trade an unavailable market

- [x] 2.1 Select the newest snapshot per `line_key` regardless of status, then require it
      to be open and non-live
- [x] 2.2 Report the superseding status when refusing, so the operator sees why
- [x] 2.3 Test: a `closed` row newer than the last `open` row records nothing and reports
      the reason

## 3. Staleness must be measured and enforced

- [x] 3.1 Compute quote age from the oldest leg used, not the freshest
- [x] 3.2 Refuse to record past `--freshness-limit`, overridable with `--allow-stale`,
      gated at the write so rehearsals still report
- [x] 3.3 Store the observed quote time as `quoted_at` on the trade record
- [x] 3.4 Tests: stale quote records nothing without the override; one fresh leg does not
      mask a stale one; a recorded trade carries `quoted_at`

## 4. Report populations and settle honestly

- [x] 4.1 Return `DraftEventPredictions` (predictions, map count, fallback count) from the
      event path and print the population in `predict --model draft`
- [x] 4.2 Classify `"map not played"` as pending rather than unresolved
- [x] 4.3 Open the database read-only in `bet`, matching the other read commands
- [x] 4.4 Tests: population is reported alongside event predictions; settlement counts an
      unplayed map as pending; appending preserves earlier records; remove the vacuous
      `if "Proposed Trade" in out:` guard

## 5. Re-measure and correct the record

- [x] 5.1 Confirm harness neutrality still reproduces Brier 0.2266 / log-loss 0.6441
- [x] 5.2 Re-run the elite walk-forward (0.2287, essentially unchanged) and the TI event
      evaluation (0.2580 vs 0.2618 before, on the same 50 rows)
- [x] 5.3 Correct `docs/dev-handoff.md` and `CLAUDE.md`: state the same-row comparison
      (ratings 0.2820, draft 0.2580, closing 0.2498) and record why the 0.2618-vs-0.2993
      framing was wrong
- [x] 5.4 Record the new gotchas in `CLAUDE.md`: evidence-scaled pair corrections, pure
      reads, latest-row-wins, oldest-leg age
- [x] 5.5 `uv run ruff check .` clean and the full suite green (203 tests)
- [x] 5.6 Verify the fixes against the live database, not only in tests

## 6. Follow-on (not part of this change)

- [ ] 6.1 Re-tune `k_pair`, half-life and learning rate — the locked values were chosen
      against the broken features, and feature magnitudes have changed by two orders of
      magnitude. Keep the tuning bound before the event.
- [ ] 6.2 Investigate why the draft model still underperforms ratings on the elite
      walk-forward (0.2287 vs 0.2266) now that the collinearity is ruled out
