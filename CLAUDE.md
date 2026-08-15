# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

Everything runs through `uv` — the venv is not activated by default.

```bash
uv sync && uv run pytest          # full suite; recorded fixtures, no network
uv run pytest -k test_name        # single test
uv run ruff check .               # lint (line-length 100; E,F,I,UP,B)
uv run dota2bets <subcommand>     # CLI entrypoint (src/dota2bets/cli.py)
uv run python scripts/window_report.py
```

Anything that hits OpenDota needs the key loaded per-shell first:

```bash
set -a; . ./.env; set +a          # OPENDOTA_API_KEY — gitignored, never commit it
```

## Data and DB rules

- **Never `cp` `data/dota2bets.sqlite`.** It is WAL-mode and the recorder is usually
  running, so a plain copy reads stale. Open read-only via `storage.connect_ro`.
- `data/` is gitignored. Predictions, logs and DB backups belong there — never commit them.
- `upsert_matches` merges with `COALESCE`, not `INSERT OR REPLACE`. A summary re-ingest
  must not null `patch` / `detail_fetched_at` and re-queue expensive refetches.
- `matches.patch` comes from `opendota.resolve_patch()` (start_time vs `/constants/patch`),
  **not** from Explorer's `match_patch` label, which lags a release by ~48h.
- `bulkfill` (Explorer/SQL) is the primary enrichment path; `detail` is the per-match
  fallback and costs API quota — one billed call per match past the 50k/month free tier.
  Don't loop it needlessly.
- `lane_role` is null until OpenDota parses the replay.

## Odds recorder

`com.dota2bets.recorder` runs as a launchd agent (`deploy/com.dota2bets.recorder.plist`).
`kill -TERM` on a `uv run` wrapper does **not** reach the Python child — kill the
`.venv/bin/dota2bets` PID or use `launchctl kickstart -k gui/$(id -u)/com.dota2bets.recorder`.

- Only tombstone after a *successful* fetch. A failed poll means we know nothing.
- `status='closed'` (listed, not taking bets) is not a tombstone (`gone`, vanished from the poll).
- `normalise` must resolve every Pinnacle selection to a stable `line_key` — moneylines
  arrive as either `designation` or numeric `participantId`, and team totals need
  `side` folded into the selection or the two teams collide.
- Never run against the real Odds API in tests; `ODDS_API_KEY` gates that source.

## Entity resolution

Team-name matching in `aliases.py` is **exact** against `aliases.yaml`
(whitespace-collapsed, case-insensitive, `(Kills)` stripped) — never fuzzy. A wrong join
is silent and corrupts everything downstream. When `dota2bets resolve` reports an unknown
name, add it to `aliases.yaml`; do not add fallback matching.

## Evaluating models

- Models are graded against **de-vigged closing market probabilities and CLV**, never raw
  accuracy. `evaluation.py` / `dota2bets eval`.
- **Always state the population a Brier was scored on.** The same ratings score 0.2449
  over the whole match pool and 0.2266 over top-tier matches between established teams.
  An unfiltered number on this dataset is meaningless — ~79% of matches are tier-3 noise.
- `--score elite` narrows the *metric* only. Tier-3 matches still train the rating book;
  training on elite-only is measurably worse.
- Baselines on TI 2026 — **the two models are scored on different row sets by default.**
  `--model ratings` emits period-0 series prices (70 scored rows); `--model draft` prices
  maps only (50 scored rows). Compare on the shared 50 rows or not at all:
  - On the same 50 map rows: ratings 0.2820, **draft 0.2580**, closing 0.2498
  - Ratings over its own 70 rows: 0.2993 vs closing 0.2508 (the extra 20 series rows
    score 0.3425, which is what made the draft model look better than it is)
  - Historical elite walk-forward: ratings **0.2266** vs draft **0.2287** on 6,638 maps —
    the draft model is *worse* here, which is why the default stays `--model ratings`

## Draft model and live paper trading

- Pure Python standard library modelling constraint: no numpy/scipy dependencies.
- Radiant convention: `team = 0` in `draft_events` is Radiant (`match_players.is_radiant = 1`); `team = 1` is Dire.
- Hero lookup: `heroes.resolve_hero(name_or_id)` resolves against `heroes.json` with `difflib` suggestions on typo.
- Incomplete drafts (<5 radiant or <5 dire picks) or cold start fall back to anchor logit shifted by `initial_intercept` (+0.063 logit for historical 51.57% Radiant win rate).
- **A pair/matchup statistic with no observations must contribute exactly 0.** The
  net-of-hero-rates correction is scaled by `evidence_weight(n, k)`; without that scaling
  an unseen pair returns `-0.5*(r1+r2)`, turning the synergy and counter features into
  negated copies of the hero win-rate feature.
- **Reads of `DecayCounter` are pure.** `counts()` computes decay without writing back, so
  pricing never mutates state. Do not turn it into a write-back cache.
- **`bet` takes the newest snapshot per `line_key` regardless of status, then requires it
  to be open.** `odds_snapshots` is append-only, so filtering on `status='open'` first
  returns a stale open row that a later `closed`/`gone` row has superseded — in the current
  DB that is 436 of 486 non-open moneyline lines.
- **Quote age is the oldest leg, not the freshest.** One leg quoted seconds ago must not
  mask the other being hours old, and the two legs are de-vigged against each other.
- Paper trading: `dota2bets bet --radiant <5 heroes> --dire <5 heroes> --radiant-team <team> --dire-team <team>`
  - Uses `--rehearse` / `--dry-run` to price and test without modifying the trade log.
  - Settle paper trades with `dota2bets bet --settle`.

## Planning workflow

This repo uses OpenSpec (`openspec/`, `/opsx:*` commands). For non-trivial changes,
propose a change there rather than inventing an ad-hoc plan.

## Longer context

@docs/dev-handoff.md — current state, what changed last session, and the full gotcha list.
@docs/research.md — why the project is shaped this way (what the literature does and
doesn't support).
