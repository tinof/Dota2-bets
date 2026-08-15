"""Tests for draft statistics, features, and online combiner in draft.py."""

from __future__ import annotations

import math
import sqlite3

from dota2bets.draft import (
    DecayCounter,
    DraftAccumulator,
    DraftConfig,
    DraftModel,
    DraftRecord,
    OnlineCombiner,
    load_drafts,
    pair_key,
    shrunk_rate,
)


def test_3_1_leak_freeness_and_first_match() -> None:
    """3.1 Leak-freeness: a match's own result cannot reach its own feature vector;

    the first-ever match yields an all-zero draft feature vector.
    """
    model = DraftModel(DraftConfig(disabled=False))
    draft = DraftRecord(
        match_id=1,
        radiant_heroes=(1, 2, 3, 4, 5),
        dire_heroes=(6, 7, 8, 9, 10),
        first_action_radiant=None,
    )
    # First match prediction
    prob1, is_fallback, feats = model.predict(
        radiant_team_id=100,
        dire_team_id=200,
        at=1000,
        p_glicko=0.5,
        draft=draft,
    )
    assert not is_fallback
    assert feats is not None
    # feats: [anchor_logit, hero_diff, team_hero_diff, synergy_diff,
    #         counter_sum, first_action, both_warm]
    # At cold start:
    assert math.isclose(feats[0], 0.0, abs_tol=1e-5)  # logit(0.5) = 0
    assert math.isclose(feats[1], 0.0, abs_tol=1e-5)  # hero diff
    assert math.isclose(feats[2], 0.0, abs_tol=1e-5)  # team hero diff
    assert math.isclose(feats[3], 0.0, abs_tol=1e-5)  # synergy diff
    assert math.isclose(feats[4], 0.0, abs_tol=1e-5)  # counter sum
    assert math.isclose(feats[5], 0.0, abs_tol=1e-5)  # first action
    assert math.isclose(feats[6], 0.0, abs_tol=1e-5)  # both warm

    # Even if radiant wins match 1, predicting match 1 again before update is identical
    prob1_repeat, _, _ = model.predict(100, 200, 1000, 0.5, draft)
    assert math.isclose(prob1, prob1_repeat)

    # Now update match 1 as radiant_win=True
    model.update(100, 200, 1000, radiant_win=True, draft=draft, features=feats)

    # Match 2 (different match at later time) now has non-zero hero features for radiant heroes
    _, _, feats2 = model.predict(100, 200, 2000, 0.5, draft)
    assert feats2 is not None
    assert feats2[1] > 0.0  # radiant heroes won their first game, so hero_diff > 0


def test_3_2_decay_arithmetic() -> None:
    """3.2 Decay arithmetic across several half-lives; recent evidence outweighs old."""
    half_life_s = 100.0
    counter = DecayCounter()
    # Hero won 10 games at t=0
    for _ in range(10):
        counter.add(win=True, at=0, half_life_s=half_life_s)

    # At t=0: 10 wins, 10 total
    w, n = counter.counts(0, half_life_s)
    assert math.isclose(w, 10.0)
    assert math.isclose(n, 10.0)

    # At t=100 (1 half-life): 5 wins, 5 total
    w, n = counter.counts(100, half_life_s)
    assert math.isclose(w, 5.0)
    assert math.isclose(n, 5.0)

    # At t=300 (3 half-lives): 1.25 wins, 1.25 total
    w, n = counter.counts(300, half_life_s)
    assert math.isclose(w, 1.25)
    assert math.isclose(n, 1.25)

    # Recent evidence test: hero won 10 long ago (t=0), but lost 2 recently (t=500)
    # At t=500, old 10 wins decayed to 10 * (0.5**5) = 10/32 = 0.3125
    # Then 2 losses added at t=500
    counter.add(win=False, at=500, half_life_s=half_life_s, weight=2.0)
    w_now, n_now = counter.counts(500, half_life_s)
    assert math.isclose(w_now, 0.3125)
    assert math.isclose(n_now, 2.3125)
    # Win rate is 0.3125 / 2.3125 ≈ 0.135 (recent losing record dominates old winning record)
    assert w_now / n_now < 0.2


def test_3_3_shrinkage_unseen_and_lopsided() -> None:
    """3.3 Shrinkage: an unseen hero contributes exactly zero;

    a single lopsided result stays near neutral.
    """
    k = 20.0
    # Unseen hero: w=0, n=0
    rate_unseen = shrunk_rate(0.0, 0.0, k, 0.5)
    assert math.isclose(rate_unseen, 0.5)
    assert math.isclose(rate_unseen - 0.5, 0.0)

    # Single win: w=1, n=1
    rate_1w = shrunk_rate(1.0, 1.0, k, 0.5)
    # (1 + 10) / (1 + 20) = 11 / 21 = 0.5238
    assert math.isclose(rate_1w, 11.0 / 21.0)
    assert abs(rate_1w - 0.5) < 0.03  # close to neutral 0.5, far from 1.0


def test_3_4_pair_symmetry_and_matchup_orientation() -> None:
    """3.4 Same-side pair key symmetry and cross-side matchup orientation."""
    assert pair_key(10, 25) == (10, 25)
    assert pair_key(25, 10) == (10, 25)

    config = DraftConfig()
    acc = DraftAccumulator()

    # Seed matchup: hero 10 and 25 both have 50% win rate overall (5-5),
    # but hero 10 beat hero 25 10 times head-to-head
    for _ in range(5):
        # Hero 10 won
        acc.hero_stats.setdefault(10, DecayCounter()).add(True, 100, config.half_life_s)
        # Hero 10 lost
        acc.hero_stats.setdefault(10, DecayCounter()).add(False, 100, config.half_life_s)
        # Hero 25 won
        acc.hero_stats.setdefault(25, DecayCounter()).add(True, 100, config.half_life_s)
        # Hero 25 lost
        acc.hero_stats.setdefault(25, DecayCounter()).add(False, 100, config.half_life_s)

    # In head-to-head, hero 10 won against 25 10 times
    for _ in range(10):
        acc.matchup_stats.setdefault(pair_key(10, 25), DecayCounter()).add(
            True, 100, config.half_life_s
        )

    # Hero 10 vs 25 advantage for radiant (Hero 10 on Radiant, 25 on Dire)
    edge_10_vs_25 = acc.matchup_edge(10, 25, 100, config)
    # Hero 25 vs 10 advantage for radiant (Hero 25 on Radiant, 10 on Dire)
    edge_25_vs_10 = acc.matchup_edge(25, 10, 100, config)

    assert edge_10_vs_25 > 0.0
    assert math.isclose(edge_10_vs_25, -edge_25_vs_10)


def test_3_5_team_on_hero_no_leak() -> None:
    """3.5 Team-on-hero familiarity does not leak between teams."""
    config = DraftConfig()
    acc = DraftAccumulator()

    # Team 100 plays Hero 1 and wins 5 times
    draft = DraftRecord(1, (1, 2, 3, 4, 5), (6, 7, 8, 9, 10), True)
    for t in range(5):
        acc.update(100, 200, t * 100, radiant_win=True, draft=draft, config=config)

    # Team 100 has high familiarity on Hero 1
    t100_rate = acc.team_hero_net_rate(100, 1, 500, config)
    # Team 300 (which never played Hero 1) has 0 familiarity
    t300_rate = acc.team_hero_net_rate(300, 1, 500, config)

    assert t100_rate > 0.1
    assert math.isclose(t300_rate, 0.0)


def test_3_6_first_draft_action_with_bans() -> None:
    """3.6 First draft action extracted correctly when the draft opens with bans."""
    conn = sqlite3.connect(":memory:")
    conn.execute(
        "CREATE TABLE draft_events (match_id INT, ord INT, is_pick INT, hero_id INT, team INT)"
    )
    # Draft opens with ban for Dire (team=1, is_pick=0, ord=0)
    # Then radiant ban (ord=1)
    # Then picks (ord=2..11)
    conn.execute("INSERT INTO draft_events VALUES (1, 0, 0, 101, 1)")
    conn.execute("INSERT INTO draft_events VALUES (1, 1, 0, 102, 0)")
    for i in range(5):
        conn.execute(f"INSERT INTO draft_events VALUES (1, {2+i}, 1, {1+i}, 0)")
        conn.execute(f"INSERT INTO draft_events VALUES (1, {7+i}, 1, {6+i}, 1)")

    drafts = load_drafts(conn)
    assert 1 in drafts
    rec = drafts[1]
    assert rec.is_complete
    # First action was ord=0 with team=1 (Dire), so first_action_radiant is False
    assert rec.first_action_radiant is False


def test_3_7_incomplete_draft_fallback() -> None:
    """3.7 A side with four picks is excluded from statistics and prices via anchor fallback."""
    # 4 picks for radiant, 5 for dire
    rec = DraftRecord(1, (1, 2, 3, 4), (6, 7, 8, 9, 10), True)
    assert not rec.is_complete

    model = DraftModel(DraftConfig(disabled=False))
    p, is_fallback, feats = model.predict(100, 200, 1000, 0.6, rec)
    assert is_fallback
    assert feats is None
    # Returns anchor probability adjusted for side bias
    anchor_logit = math.log(0.6 / 0.4)
    expected_p = 1.0 / (1.0 + math.exp(-(model.combiner.intercept + anchor_logit)))
    assert math.isclose(p, expected_p)


def test_3_8_combiner_untrained_and_learning() -> None:
    """3.8 Untrained combiner equals ratings probability adjusted for side;

    disabled config never moves a non-anchor weight;
    combiner learns on synthetic stream.
    """
    config = DraftConfig(disabled=False, initial_intercept=0.063)
    model = DraftModel(config)

    draft = DraftRecord(1, (1, 2, 3, 4, 5), (6, 7, 8, 9, 10), None)
    p, is_fallback, feats = model.predict(100, 200, 1000, 0.5, draft)
    # Untrained combiner: equal teams p=0.5 -> logit=0 -> p = sigmoid(0.063)
    expected = 1.0 / (1.0 + math.exp(-0.063))
    assert math.isclose(p, expected, rel_tol=1e-4)

    # Test disabled config never moves weights
    disabled_model = DraftModel(DraftConfig(disabled=True))
    p_dis, _, _ = disabled_model.predict(100, 200, 1000, 0.7, draft)
    assert math.isclose(p_dis, 0.7)
    disabled_model.update(100, 200, 1000, radiant_win=True, draft=draft, features=[0.0] * 7)
    assert disabled_model.combiner.weights == [1.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0]

    # Test learning on synthetic stream: radiant always wins when hero 1 is on radiant
    learning_combiner = OnlineCombiner(
        weights=[1.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0],
        intercept=0.0,
    )
    # Strong positive hero_diff feature = +1.0, outcome = 1
    for _ in range(40):
        learning_combiner.update([0.0, 1.0, 0.0, 0.0, 0.0, 0.0, 0.0], outcome=1, learning_rate=0.1)
    # Feature 1 (hero_diff) weight should have grown positive
    assert learning_combiner.weights[1] > 0.5


def test_3_9_pin_radiant_convention() -> None:
    """3.9 Pin the radiant convention:

    draft_events.team = 0 agrees with match_players.is_radiant.
    """
    conn = sqlite3.connect(":memory:")
    conn.execute(
        "CREATE TABLE draft_events (match_id INT, ord INT, is_pick INT, hero_id INT, team INT)"
    )
    conn.execute(
        "CREATE TABLE match_players (match_id INT, player_slot INT, hero_id INT, is_radiant INT)"
    )
    # Match where radiant picks hero 1..5, dire picks hero 6..10
    for i in range(5):
        conn.execute(f"INSERT INTO draft_events VALUES (1, {i}, 1, {1+i}, 0)")
        conn.execute(f"INSERT INTO match_players VALUES (1, {i}, {1+i}, 1)")
        conn.execute(f"INSERT INTO draft_events VALUES (1, {5+i}, 1, {6+i}, 1)")
        conn.execute(f"INSERT INTO match_players VALUES (1, {5+i}, {6+i}, 0)")

    # Assert that heroes picked with team=0 match players with is_radiant=1
    cur = conn.execute(
        "SELECT d.hero_id FROM draft_events d "
        "JOIN match_players p ON p.match_id = d.match_id AND p.hero_id = d.hero_id "
        "WHERE d.team = 0 AND p.is_radiant = 1"
    )
    radiant_matches = cur.fetchall()
    assert len(radiant_matches) == 5


def _trained_accumulator(config: DraftConfig) -> tuple[DraftAccumulator, int]:
    """Radiant 1-5 beating dire 6-10 repeatedly, so every seen stat is well established."""
    acc = DraftAccumulator()
    base = 1_700_000_000
    for i in range(40):
        acc.update(
            1, 2, base + i * 86400, True,
            DraftRecord(i, (1, 2, 3, 4, 5), (6, 7, 8, 9, 10)), config,
        )
    return acc, base + 60 * 86400


def test_unseen_pair_and_matchup_contribute_exactly_zero() -> None:
    """A never-observed pair carries no information, so it must add exactly 0.0.

    Subtracting the individual hero rates unconditionally turned an unseen pair into a
    negated restatement of the hero win-rate feature -- the two fought each other.
    """
    config = DraftConfig()
    acc, at = _trained_accumulator(config)

    # Hero 999 has never been played, and no pair involving it has ever been observed.
    assert acc.hero_net_rate(999, at, config) == 0.0
    assert acc.pair_synergy(1, 999, at, config) == 0.0
    assert acc.matchup_edge(999, 6, at, config) == 0.0
    assert acc.matchup_edge(1, 999, at, config) == 0.0

    # Observed pairs still carry signal, so this is not a blanket zeroing.
    assert acc.pair_synergy(1, 2, at, config) != 0.0
    assert acc.matchup_edge(1, 6, at, config) != 0.0


def test_pair_correction_scales_with_evidence() -> None:
    """The net-of-hero-rates correction grows with the pair's own decayed evidence."""
    config = DraftConfig(k_pair=10.0)
    acc = DraftAccumulator()
    base = 1_700_000_000
    acc.update(
        1, 2, base, True, DraftRecord(1, (1, 2, 3, 4, 5), (6, 7, 8, 9, 10)), config
    )
    thin = abs(acc.pair_synergy(1, 2, base + 86400, config))
    for i in range(1, 60):
        acc.update(
            1, 2, base + i * 86400, True,
            DraftRecord(i, (1, 2, 3, 4, 5), (6, 7, 8, 9, 10)), config,
        )
    thick = abs(acc.pair_synergy(1, 2, base + 61 * 86400, config))
    assert thick > thin


def test_pricing_does_not_mutate_accumulated_state() -> None:
    """Reads are pure: decay is computed, never written back.

    A write-back cache in the counts helper would make a price depend on how many times
    it had been asked for, and on the order predictions were requested.
    """
    import copy

    config = DraftConfig()
    acc, at = _trained_accumulator(config)
    draft = DraftRecord(99, (1, 2, 3, 4, 5), (6, 7, 8, 9, 10))

    before = copy.deepcopy(acc)
    first = acc.extract_features(1, 2, at, 0.0, draft, config)
    second = acc.extract_features(1, 2, at, 0.0, draft, config)

    assert first == second
    assert acc.hero_stats == before.hero_stats
    assert acc.pair_stats == before.pair_stats
    assert acc.matchup_stats == before.matchup_stats
    assert acc.team_hero_stats == before.team_hero_stats


def test_an_unseen_hero_does_not_move_the_pair_derived_features() -> None:
    """Swapping in a never-played hero must not shift the pair-derived features."""
    config = DraftConfig()
    acc, at = _trained_accumulator(config)

    seen = acc.extract_features(
        1, 2, at, 0.0, DraftRecord(99, (1, 2, 3, 4, 5), (6, 7, 8, 9, 10)), config
    )
    swapped = acc.extract_features(
        1, 2, at, 0.0, DraftRecord(99, (1, 2, 3, 4, 999), (6, 7, 8, 9, 10)), config
    )
    # Hero 5's own win rate legitimately leaves the hero term...
    assert swapped[1] != seen[1]
    # ...but 999 contributes nothing through pairs it has never appeared in.
    for h in (1, 2, 3, 4):
        assert acc.pair_synergy(h, 999, at, config) == 0.0
    for h in (6, 7, 8, 9, 10):
        assert acc.matchup_edge(999, h, at, config) == 0.0
