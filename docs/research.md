# Research notes: where the edge plausibly is in Dota 2 betting

Written to answer three questions: what a public tournament Monte Carlo simulation adds
over bookmaker pricing, what machine learning can do at the draft stage and in live play,
and which of those is worth building.

## 1. The Reddit TI 2026 Monte Carlo

The [post](https://www.reddit.com/r/DotA2/comments/1vcu5ds/) combined Bradley-Terry/Glicko-2
ratings from 2026 series results, strength-of-schedule weighting, a "Roster Stability
Index" (0.75–0.98) penalising stand-ins, and 100,000 simulations of Valve's exact 5-round
Swiss format with non-rematch rules. Headline outputs: PARIVISION 41.6% to finish 4-0,
Team Resilience 52.9% to finish 0-4.

**It produced no information bookmakers lack, and it was miscalibrated.** A
[rebuttal post](https://www.reddit.com/r/DotA2/comments/1verabd/) showed the 41.6% figure
implies roughly an 80% win probability in every round including against top seeds, where
a realistic ceiling is 20–22%, and that several inputs were factually wrong. Bookmakers
run the same model class — a match-probability engine feeding a tournament-structure
simulation — with better calibration and their own order flow on top.

The *structure* is nonetheless the right idea. Converting match probabilities into
tournament-structure probabilities (exact records, advancement, bracket paths) is how you
price outright and exotic markets, which attract less bookmaker attention and thinner
liquidity than match odds. The Redditor's architecture was sound; the inputs weren't.

## 2. What the literature actually shows

### Accuracy by prediction stage

| Stage | Reported accuracy | Source |
|---|---|---|
| Pre-match, basic | ~58.7% | Yang et al., [1701.03162](https://arxiv.org/abs/1701.03162) |
| Pre-match, rich player/team priors | **71.5%** | same, 78k matches |
| Draft complete (pub games) | **75.1%** | Do et al., [2108.02799](https://arxiv.org/abs/2108.02799) |
| Live, 40 minutes in | **93.7%** | Yang et al. |
| Live, LSTM on game-state telemetry | ~93% | Akhmedov & Phan, [2106.01782](https://arxiv.org/abs/2106.01782) |

Two important caveats. First, most of these use *public matchmaking* data, where much of
the signal is skill variance between players — that variance largely disappears between
professional teams, so pro-match numbers run materially lower. Hodge et al.
([1711.06498](https://arxiv.org/abs/1711.06498), and the IEEE ToG follow-up deployed with
ESL broadcasts) is the canonical pro-match live predictor and the right reference point.

Second, some headline numbers are not predictions at all. Birant & Birant
(Entropy 25:28, 2023) report ~94% on League of Legends — but from *post-game* statistics
such as tower kills and gold earned, which leak the outcome. Do not use it as a
benchmark. Its transferable contributions are methodological: representing a team as a
permutation-invariant bag of five player vectors beat flat feature concatenation by 4–13
percentage points, and training within patch/season windows matters because the metagame
invalidates old data.

Drafting itself is well-studied and clearly modelable: JueWuDraft
([2012.10171](https://arxiv.org/abs/2012.10171)), DraftRec
([2204.12750](https://arxiv.org/abs/2204.12750), includes 50k Dota 2 matches), Art of
Drafting ([1806.10130](https://arxiv.org/abs/1806.10130)) and hero synergy/counter
embeddings ([1803.10402](https://arxiv.org/abs/1803.10402)).

### Accuracy is not edge

The betting-market literature is the part that decides whether any of this makes money.

- Mature markets are largely efficient (Constantinou,
  [2003.09384](https://arxiv.org/abs/2003.09384); Fischer & Heuer,
  [2408.08331](https://arxiv.org/abs/2408.08331)). The question is market *softness*, not
  model accuracy — a model can profit through selective value betting without beating the
  bookmaker's overall log loss.
- In-play markets are reactive, not anticipatory: bookmaker odds and betting stakes show
  no adjustment before goals in the Bundesliga
  ([2505.21275](https://arxiv.org/abs/2505.21275)).
- The strongest positive evidence anywhere is Clegg, Song & Cartlidge
  ([2605.16066](https://arxiv.org/abs/2605.16066)): a Weibull accelerated-failure-time
  model **calibrated to Betfair's kick-off prices** and fed in-play covariates achieved
  **4.5% ROI (Sharpe 5.94) over 17,458 bets** against Betfair in-play football. The
  method — anchor to the market's pre-match price, then add in-game information — is the
  template for a live Dota model.
- Esports specifically: Li, Xiao, Li & Chen (2024) document bias and inefficiency across
  LoL, CS, Dota 2 and King of Glory, with long-shot bias varying by match format and
  region. Industry data (Oddin, 2025) puts CS2 markets close to traditional-sport
  maturity while **Dota 2 is losing betting share and is less mature and less liquid** —
  softer, but with lower limits.
- **No published study demonstrates a profitable ML strategy in Dota 2 markets.** The
  edge hypothesis rests on market immaturity plus the football in-play precedent, and has
  to be validated with closing-line value before any real stake.

### The live-latency question

A common assumption is that live esports betting is a race against broadcast delay. For
Dota 2 that is wrong in both directions. Dota's in-game spectator API exposes the
betting-critical state — kill score, gold, buildings — in near-real-time to bookmakers
and punters alike, unlike League of Legends where official data is gated through a single
distributor. So there is no latency edge to capture, but also no structural disadvantage:
live edge, if it exists, comes purely from calibration quality. Execution risk is real
though — operators suspend markets during teamfights, and live odds can move within
seconds during a base race.

## 3. Ranked opportunities

1. **The draft→game-start window.** Between draft completion and the horn, books have a
   couple of minutes to reprice a match on its most informative pre-game evidence: this
   draft, on this patch, by these teams. Many reprice slowly, crudely, or just widen the
   margin. This is a calibration race, not a latency race.
2. **Tournament-structure and outright markets** — the corrected version of the Reddit
   simulation: a well-calibrated match model plus a correct Swiss/bracket simulation,
   aimed at low-attention markets.
3. **Live betting at 5–10 minutes.** The strongest raw signal, and the football precedent
   suggests in-play calibration edges exist even against an exchange. Harder market:
   wider margins, suspensions, professionally calibrated operator feeds.
4. **Pre-match ratings.** Rarely enough edge alone against a closing line, but it is the
   required backbone for all of the above and the cheapest thing to validate.

**The binding constraint is odds history.** Without timestamped lines you cannot tell an
accurate model from a profitable one, and esports odds history is scarce and expensive.
That is why Phase 0 records odds before any model exists.

## Avoid

Tier-3 matches are the softest markets but carry serious match-fixing risk (the "322"
problem). Softness there is not edge, it is adverse selection.
