# Dota2-bets

Data foundation for modelling professional Dota 2 matches and measuring whether a model
can actually beat the betting market.

Phase 0 (this repo, so far) collects the two things every later phase needs: **pro-match
history with drafts and per-minute game state**, and **timestamped odds line history**.
Odds history is the scarce half — it cannot be bought retroactively, so recording starts
before modelling.

## Quick start

```bash
uv sync
uv run dota2bets backfill --max-matches 1000   # pro match summaries from OpenDota
uv run dota2bets detail --limit 200            # drafts, players, gold/xp series
uv run dota2bets record-odds                   # line recorder — leave running
uv run dota2bets status                        # what's in the database
```

Data lands in `data/dota2bets.sqlite` (gitignored). `record-odds` polls every 60s and
writes a snapshot only when a line actually moves, so it builds a line *history* rather
than a poll log. Leave it running for the whole of an event.

Optional env vars: `OPENDOTA_API_KEY` (raises rate limits), `ODDS_API_KEY` (enables the
`theoddsapi` source: `record-odds --sources pinnacle theoddsapi`).

## What's collected

| Table | Contents | Used by |
|---|---|---|
| `matches` | league, teams, patch, winner, duration | all phases |
| `draft_events` | every pick/ban in order, per team | draft model (Phase 2) |
| `match_players` | account, hero, role, per-match stats | player–hero familiarity features |
| `match_timeseries` | per-minute radiant gold/XP advantage | live model (Phase 3) |
| `rosters` | player↔team observations over time | roster-stability features |
| `odds_snapshots` | line history: price, limit, status, market, period | every evaluation |
| `event_series_map` | which series each bookmaker event prices | every evaluation |

**Sources.** [OpenDota](https://docs.opendota.com/) for matches (free, keyless,
rate-limited). [Pinnacle's public guest API](src/dota2bets/odds/pinnacle.py) for odds —
Pinnacle is the sharp reference book (low margin, high limits, doesn't limit winning
accounts), so its closing line is the benchmark a model must beat. The Odds API is
wired up as a second source for cross-book dispersion, i.e. finding the *soft* book to
bet into once the sharp line says there's value.

## Why this shape — research summary

Full write-up: [`docs/research.md`](docs/research.md). The short version:

- **Prediction accuracy rises sharply with game time** — roughly 58→71% pre-match with
  rich team/player priors, ~75% at draft in *pub* games, and up to ~94% deep into a
  live game. Headline numbers in the literature are mostly from public matchmaking; pro
  matches are harder because the teams are more evenly matched.
- **Accuracy is not edge.** Nothing in the literature demonstrates a profitable ML
  strategy in Dota 2 markets. The closest real evidence is football: a market-calibrated
  in-play model returned 4.5% ROI over 17,458 bets against Betfair. So models are
  evaluated here against **de-vigged bookmaker probabilities and closing-line value**,
  never against raw accuracy.
- **The best edge candidate is the draft→game-start window**: books must reprice on the
  most informative pre-game evidence within a couple of minutes, and it's a calibration
  race rather than a latency race.
- **There is no latency edge in Dota live betting.** Dota's spectator API exposes kills,
  gold and buildings in near-real-time to bookmakers and punters alike, so any live edge
  must come from better calibration.

## Roadmap

- **Phase 1** — Glicko-2/Bradley-Terry ratings per patch window + roster stability;
  scored by log loss/Brier and calibration against de-vigged closing odds.
- **Phase 2** — draft-stage model (hero synergy/counter embeddings, patch-specific hero
  priors, player–hero familiarity), measured as the *increment* over Phase 1 and
  backtested in the draft→start window.
- **Phase 3** — live win-probability model from the per-minute series, calibrated to the
  pre-match market price.
- **Phase 4** — fractional Kelly staking, continuous CLV monitoring, strict bankroll cap.

Real money only after a model shows positive closing-line value over a few hundred paper
bets.

## Development

```bash
uv run pytest        # parsers and storage, against recorded fixtures (no network)
uv run ruff check .
```

## Papers

`papers/` holds the reference PDFs cited in [`docs/research.md`](docs/research.md).
