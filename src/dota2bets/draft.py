"""Draft-aware map win probability model and online combiner.

Contract and Timing Decisions:
-----------------------------
1. **Leak-Freeness Contract**:
   A map's own result CANNOT reach its own feature vector. Every map's draft price is
   computed strictly from the state of ratings, accumulated hero statistics, and combiner
   weights that existed *before* that map was played. Statistics and weights are only
   updated after the prediction has been recorded.

2. **Same-Series-Earlier-Maps Decision**:
   The draft model bets during map k's draft (periods 1, 2, or 3). At that moment, maps
   1..k-1 of the same series have already finished and are public knowledge. The Pinnacle
   market quoting map k has also seen them. Therefore, chronological replay naturally folds
   earlier maps of the series into ratings and draft statistics before map k is priced.
   This differs from pre-series ratings models (which freeze at map 1's horn) because
   the draft window is opened map-by-map.

3. **Decay and Shrinkage**:
   Hero, team-on-hero, pair synergy, and matchup counters undergo exponential idle-time
   decay. Reads (``DecayCounter.counts``) are PURE -- they compute the decay factor and
   return the decayed values without writing back, so pricing never mutates state. Only
   the write path (``add``/``decay_to``) advances ``last_time``. Do not "optimise"
   ``counts`` into a write-back cache: that would break the repeatability contract in 1.
   Unseen heroes/pairs contribute exactly 0.0 to features via Bayesian shrinkage towards a
   0.5 prior, (w + k*0.5)/(n + k) - 0.5, with pair/matchup corrections scaled by
   ``evidence_weight`` so that a never-observed pair adds nothing at all.

4. **Anchor-Initialised Online Combiner**:
   The combiner starts with weights [1.0, 0, 0, 0, 0, 0, 0] on [anchor_logit, hero_diff,
   team_hero_diff, synergy_diff, counter_sum, first_draft_action, both_teams_warm] and
   an intercept set to the historical radiant logit bias. An untrained combiner or a
   model with draft features disabled produces the Glicko-2 ratings probability (adjusted
   only for side advantage).
"""

from __future__ import annotations

import itertools
import math
import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

DEFAULT_HALF_LIFE_DAYS = 120.0
DEFAULT_K_HERO = 20.0
DEFAULT_K_TEAM_HERO = 10.0
DEFAULT_K_PAIR = 100.0
DEFAULT_LEARNING_RATE = 0.05
DEFAULT_INITIAL_INTERCEPT = 0.063  # logit(0.5157 empirical radiant win rate)


@dataclass(frozen=True)
class DraftConfig:
    """Tunables for draft statistics and combiner."""

    half_life_days: float = DEFAULT_HALF_LIFE_DAYS
    k_hero: float = DEFAULT_K_HERO
    k_team_hero: float = DEFAULT_K_TEAM_HERO
    k_pair: float = DEFAULT_K_PAIR
    learning_rate: float = DEFAULT_LEARNING_RATE
    disabled: bool = False
    min_team_games: int = 5
    initial_intercept: float = DEFAULT_INITIAL_INTERCEPT

    @property
    def half_life_s(self) -> float:
        return self.half_life_days * 86400.0


@dataclass
class DecayCounter:
    """Decaying win and total counters, updated lazily against timestamps."""

    wins: float = 0.0
    total: float = 0.0
    last_time: int | None = None

    def decay_to(self, at: int, half_life_s: float) -> None:
        """Apply exponential decay up to timestamp ``at``."""
        if self.last_time is not None and at > self.last_time and half_life_s > 0:
            factor = 0.5 ** ((at - self.last_time) / half_life_s)
            self.wins *= factor
            self.total *= factor
            self.last_time = at
        elif self.last_time is None:
            self.last_time = at

    def add(self, win: bool | float, at: int, half_life_s: float, weight: float = 1.0) -> None:
        """Decay to ``at``, then record a win/loss observation."""
        self.decay_to(at, half_life_s)
        if win:
            self.wins += weight
        self.total += weight
        self.last_time = at

    def counts(self, at: int, half_life_s: float) -> tuple[float, float]:
        """Return decayed (wins, total) as of timestamp ``at`` without mutating state."""
        if self.last_time is not None and at > self.last_time and half_life_s > 0:
            factor = 0.5 ** ((at - self.last_time) / half_life_s)
            return self.wins * factor, self.total * factor
        return self.wins, self.total


def shrunk_rate(w: float, n: float, k: float, prior: float = 0.5) -> float:
    """Shrink a win rate toward neutral prior: (w + k * prior) / (n + k)."""
    return (w + k * prior) / (n + k)


def evidence_weight(n: float, k: float) -> float:
    """How much a statistic with decayed count ``n`` is trusted: 0.0 at n=0, ->1 as n grows."""
    return n / (n + k)


def pair_key(h1: int, h2: int) -> tuple[int, int]:
    """Canonical symmetric pair key."""
    return (min(h1, h2), max(h1, h2))


@dataclass(frozen=True)
class DraftRecord:
    """Ten picked heroes and first action indicator for one match."""

    match_id: int
    radiant_heroes: tuple[int, ...]
    dire_heroes: tuple[int, ...]
    first_action_radiant: bool | None = None

    @property
    def is_complete(self) -> bool:
        """True if exactly 5 distinct radiant heroes and 5 distinct dire heroes."""
        return (
            len(self.radiant_heroes) == 5
            and len(self.dire_heroes) == 5
            and len(set(self.radiant_heroes)) == 5
            and len(set(self.dire_heroes)) == 5
            and len(set(self.radiant_heroes) & set(self.dire_heroes)) == 0
        )


def load_drafts(conn: sqlite3.Connection) -> dict[int, DraftRecord]:
    """Load picked heroes and first draft action per match from ``draft_events``.

    Only complete 5v5 drafts with 10 distinct heroes are returned.
    """
    cur = conn.execute(
        "SELECT match_id, ord, is_pick, hero_id, team FROM draft_events ORDER BY match_id, ord"
    )
    raw: dict[int, dict[str, Any]] = {}
    for m_id, _ord_val, is_pick, hero_id, team in cur:
        match_id = int(m_id)
        entry = raw.setdefault(
            match_id,
            {"rad_picks": [], "dire_picks": [], "first_action_team": None},
        )
        if entry["first_action_team"] is None:
            entry["first_action_team"] = int(team)
        if int(is_pick) == 1:
            if int(team) == 0:
                entry["rad_picks"].append(int(hero_id))
            elif int(team) == 1:
                entry["dire_picks"].append(int(hero_id))

    records: dict[int, DraftRecord] = {}
    for match_id, entry in raw.items():
        first_act = None
        if entry["first_action_team"] is not None:
            first_act = entry["first_action_team"] == 0
        rec = DraftRecord(
            match_id=match_id,
            radiant_heroes=tuple(entry["rad_picks"]),
            dire_heroes=tuple(entry["dire_picks"]),
            first_action_radiant=first_act,
        )
        if rec.is_complete:
            records[match_id] = rec
    return records


@dataclass
class DraftAccumulator:
    """Historical statistics for heroes, team-hero pairs, synergies, and matchups."""

    hero_stats: dict[int, DecayCounter] = field(default_factory=dict)
    team_hero_stats: dict[tuple[int, int], DecayCounter] = field(default_factory=dict)
    pair_stats: dict[tuple[int, int], DecayCounter] = field(default_factory=dict)
    matchup_stats: dict[tuple[int, int], DecayCounter] = field(default_factory=dict)
    team_games: dict[int, int] = field(default_factory=dict)

    def hero_net_rate(self, hero_id: int, at: int, config: DraftConfig) -> float:
        """Hero win rate minus 0.5, shrunk toward neutral."""
        counter = self.hero_stats.get(hero_id)
        w, n = counter.counts(at, config.half_life_s) if counter else (0.0, 0.0)
        return shrunk_rate(w, n, config.k_hero, 0.5) - 0.5

    def team_hero_net_rate(
        self, team_id: int, hero_id: int, at: int, config: DraftConfig
    ) -> float:
        """Team-on-hero familiarity rate minus 0.5, shrunk toward neutral."""
        counter = self.team_hero_stats.get((team_id, hero_id))
        w, n = counter.counts(at, config.half_life_s) if counter else (0.0, 0.0)
        return shrunk_rate(w, n, config.k_team_hero, 0.5) - 0.5

    def pair_synergy(self, h1: int, h2: int, at: int, config: DraftConfig) -> float:
        """Synergy between two heroes on the same side, net of individual hero win rates.

        The net-of-hero-rates correction is scaled by the pair's own decayed evidence.
        A pair never observed together carries no synergy information, so it must
        contribute exactly 0.0 -- subtracting the individual rates unconditionally would
        turn an unseen pair into a negated restatement of the hero win-rate feature.
        """
        counter = self.pair_stats.get(pair_key(h1, h2))
        w, n = counter.counts(at, config.half_life_s) if counter else (0.0, 0.0)
        pair_net = shrunk_rate(w, n, config.k_pair, 0.5) - 0.5
        evidence = evidence_weight(n, config.k_pair)
        r1 = self.hero_net_rate(h1, at, config)
        r2 = self.hero_net_rate(h2, at, config)
        return pair_net - evidence * 0.5 * (r1 + r2)

    def matchup_edge(self, h_rad: int, h_dire: int, at: int, config: DraftConfig) -> float:
        """Matchup advantage for h_rad over h_dire, net of individual hero win rates.

        Scaled by the matchup's own evidence for the same reason as ``pair_synergy``.
        """
        h_min, h_max = pair_key(h_rad, h_dire)
        counter = self.matchup_stats.get((h_min, h_max))
        w, n = counter.counts(at, config.half_life_s) if counter else (0.0, 0.0)
        # w is times h_min won against h_max
        p_min_vs_max = shrunk_rate(w, n, config.k_pair, 0.5)
        evidence = evidence_weight(n, config.k_pair)
        r_min = self.hero_net_rate(h_min, at, config)
        r_max = self.hero_net_rate(h_max, at, config)
        edge_min_over_max = (p_min_vs_max - 0.5) - evidence * (r_min - r_max)
        return edge_min_over_max if h_rad == h_min else -edge_min_over_max

    def extract_features(
        self,
        radiant_team_id: int,
        dire_team_id: int,
        at: int,
        p_glicko: float,
        draft: DraftRecord,
        config: DraftConfig,
    ) -> list[float]:
        """Extract the 7 radiant-oriented draft features."""
        # 1. Anchor logit (Glicko-2 probability converted to log-odds)
        clamped_p = min(max(p_glicko, 1e-6), 1.0 - 1e-6)
        anchor_logit = math.log(clamped_p / (1.0 - clamped_p))

        # 2. Hero win-rate difference (Radiant heroes sum minus Dire heroes sum)
        rad_hero_rates = [self.hero_net_rate(h, at, config) for h in draft.radiant_heroes]
        dire_hero_rates = [self.hero_net_rate(h, at, config) for h in draft.dire_heroes]
        hero_win_rate_diff = sum(rad_hero_rates) - sum(dire_hero_rates)

        # 3. Team-on-hero familiarity difference
        rad_team_hero = [
            self.team_hero_net_rate(radiant_team_id, h, at, config) for h in draft.radiant_heroes
        ]
        dire_team_hero = [
            self.team_hero_net_rate(dire_team_id, h, at, config) for h in draft.dire_heroes
        ]
        team_hero_diff = sum(rad_team_hero) - sum(dire_team_hero)

        # 4. Synergy difference
        rad_synergy = sum(
            self.pair_synergy(h1, h2, at, config)
            for h1, h2 in itertools.combinations(draft.radiant_heroes, 2)
        )
        dire_synergy = sum(
            self.pair_synergy(h1, h2, at, config)
            for h1, h2 in itertools.combinations(draft.dire_heroes, 2)
        )
        synergy_diff = rad_synergy - dire_synergy

        # 5. Counter sum (radiant hero vs dire hero for all 25 cross pairs)
        counter_sum = sum(
            self.matchup_edge(h_r, h_d, at, config)
            for h_r in draft.radiant_heroes
            for h_d in draft.dire_heroes
        )

        # 6. First draft action indicator (+1.0 for radiant first, -1.0 for dire first, 0.0 unknown)
        if draft.first_action_radiant is True:
            first_action = 1.0
        elif draft.first_action_radiant is False:
            first_action = -1.0
        else:
            first_action = 0.0

        # 7. Both teams warm indicator (1.0 if both teams have >= min_team_games)
        rad_games = self.team_games.get(radiant_team_id, 0)
        dire_games = self.team_games.get(dire_team_id, 0)
        is_warm = rad_games >= config.min_team_games and dire_games >= config.min_team_games
        both_warm = 1.0 if is_warm else 0.0

        return [
            anchor_logit,
            hero_win_rate_diff,
            team_hero_diff,
            synergy_diff,
            counter_sum,
            first_action,
            both_warm,
        ]

    def update(
        self,
        radiant_team_id: int,
        dire_team_id: int,
        at: int,
        radiant_win: bool,
        draft: DraftRecord | None,
        config: DraftConfig,
    ) -> None:
        """Fold decided map into accumulated statistics."""
        self.team_games[radiant_team_id] = self.team_games.get(radiant_team_id, 0) + 1
        self.team_games[dire_team_id] = self.team_games.get(dire_team_id, 0) + 1

        if draft is None or not draft.is_complete:
            return

        half_life = config.half_life_s

        # Hero stats
        for h in draft.radiant_heroes:
            self.hero_stats.setdefault(h, DecayCounter()).add(radiant_win, at, half_life)
        for h in draft.dire_heroes:
            self.hero_stats.setdefault(h, DecayCounter()).add(not radiant_win, at, half_life)

        # Team-hero stats
        for h in draft.radiant_heroes:
            self.team_hero_stats.setdefault((radiant_team_id, h), DecayCounter()).add(
                radiant_win, at, half_life
            )
        for h in draft.dire_heroes:
            self.team_hero_stats.setdefault((dire_team_id, h), DecayCounter()).add(
                not radiant_win, at, half_life
            )

        # Same-side pairs
        for h1, h2 in itertools.combinations(draft.radiant_heroes, 2):
            self.pair_stats.setdefault(pair_key(h1, h2), DecayCounter()).add(
                radiant_win, at, half_life
            )
        for h1, h2 in itertools.combinations(draft.dire_heroes, 2):
            self.pair_stats.setdefault(pair_key(h1, h2), DecayCounter()).add(
                not radiant_win, at, half_life
            )

        # Cross-side matchups
        for h_r in draft.radiant_heroes:
            for h_d in draft.dire_heroes:
                h_min, h_max = pair_key(h_r, h_d)
                # Did h_min beat h_max?
                if h_r == h_min:
                    min_won = radiant_win
                else:
                    min_won = not radiant_win
                self.matchup_stats.setdefault((h_min, h_max), DecayCounter()).add(
                    min_won, at, half_life
                )


@dataclass
class OnlineCombiner:
    """Online logistic regression layer with AdaGrad updates."""

    weights: list[float] = field(
        default_factory=lambda: [1.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0]
    )
    intercept: float = DEFAULT_INITIAL_INTERCEPT
    grad_sq_weights: list[float] = field(
        default_factory=lambda: [1.0, 1.0, 1.0, 1.0, 1.0, 1.0, 1.0]
    )
    grad_sq_intercept: float = 1.0
    epsilon: float = 1e-4

    def predict(self, features: Sequence[float]) -> float:
        """Combine features into win probability: p = 1 / (1 + exp(-z))."""
        z = self.intercept + sum(w * x for w, x in zip(self.weights, features, strict=True))
        z_clamped = min(max(z, -35.0), 35.0)
        return 1.0 / (1.0 + math.exp(-z_clamped))

    def update(
        self, features: Sequence[float], outcome: int, learning_rate: float
    ) -> None:
        """Perform one AdaGrad update step."""
        p = self.predict(features)
        err = p - float(outcome)

        # Update intercept
        g0 = err
        self.grad_sq_intercept += g0 * g0
        self.intercept -= (learning_rate / math.sqrt(self.grad_sq_intercept + self.epsilon)) * g0

        # Update feature weights
        for j in range(len(self.weights)):
            gj = err * features[j]
            self.grad_sq_weights[j] += gj * gj
            self.weights[j] -= (
                learning_rate / math.sqrt(self.grad_sq_weights[j] + self.epsilon)
            ) * gj


@dataclass
class DraftModel:
    """Full draft-aware model tracking accumulated statistics and combiner weights."""

    config: DraftConfig = field(default_factory=DraftConfig)
    accumulator: DraftAccumulator = field(default_factory=DraftAccumulator)
    combiner: OnlineCombiner = field(default_factory=OnlineCombiner)

    def __post_init__(self) -> None:
        self.combiner.intercept = self.config.initial_intercept

    def predict(
        self,
        radiant_team_id: int,
        dire_team_id: int,
        at: int,
        p_glicko: float,
        draft: DraftRecord | None,
    ) -> tuple[float, bool, list[float] | None]:
        """Produce Radiant win probability.

        Returns (prob, is_fallback, features).
        When draft features are disabled, returns (p_glicko, fallback, None).
        When draft is incomplete, falls back to ratings adjusted for side.
        """
        if self.config.disabled:
            is_fallback = draft is None or not draft.is_complete
            return p_glicko, is_fallback, None

        if draft is None or not draft.is_complete:
            # Fall back to anchor adjusted for side bias
            clamped_p = min(max(p_glicko, 1e-6), 1.0 - 1e-6)
            anchor_logit = math.log(clamped_p / (1.0 - clamped_p))
            z = self.combiner.intercept + self.combiner.weights[0] * anchor_logit
            z_clamped = min(max(z, -35.0), 35.0)
            prob = 1.0 / (1.0 + math.exp(-z_clamped))
            return prob, True, None

        features = self.accumulator.extract_features(
            radiant_team_id, dire_team_id, at, p_glicko, draft, self.config
        )
        prob = self.combiner.predict(features)
        return prob, False, features

    def update(
        self,
        radiant_team_id: int,
        dire_team_id: int,
        at: int,
        radiant_win: bool,
        draft: DraftRecord | None,
        features: Sequence[float] | None = None,
    ) -> None:
        """Advance accumulated statistics and combiner weights."""
        outcome = 1 if radiant_win else 0
        if not self.config.disabled and features is not None:
            self.combiner.update(features, outcome, self.config.learning_rate)

        self.accumulator.update(
            radiant_team_id, dire_team_id, at, radiant_win, draft, self.config
        )
