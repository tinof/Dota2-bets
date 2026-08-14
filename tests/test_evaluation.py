from __future__ import annotations

import json

import pytest

from dota2bets import storage
from dota2bets.aliases import AliasResolver
from dota2bets.evaluation import (
    Prediction,
    closing_event,
    closing_lines,
    closing_probs,
    clv_report,
    devig_proportional,
    devig_shin,
    implied_probs,
    load_predictions,
    map_start,
    overround,
    resolve_outcome,
    shin_z,
)

SPIRIT = 7119388
AURORA = 111
SERIES = 555
MAP1 = 1_000_000
MAP2 = 1_004_000

ALIAS_YAML = """
teams:
  - team_id: 7119388
    name: Team Spirit
    aliases: [Spirit]
  - team_id: 111
    name: Aurora Gaming
    aliases: [Aurora]
"""


@pytest.fixture
def resolver():
    return AliasResolver.from_yaml(ALIAS_YAML)


def _match(match_id: int, start_time: int, radiant_win: int):
    return {
        "match_id": match_id,
        "series_id": SERIES,
        "series_type": 1,
        "start_time": start_time,
        "radiant_team_id": SPIRIT,
        "radiant_name": "Team Spirit",
        "dire_team_id": AURORA,
        "dire_name": "Aurora Gaming",
        "radiant_win": radiant_win,
    }


def _map_event(conn, event_id: str, parent: str | None):
    conn.execute(
        "INSERT INTO event_series_map (source, event_id, units, is_kills, parent_event_id, "
        "home_team_id, away_team_id, series_id, start_skew_s, resolved_at) "
        "VALUES ('pinnacle', ?, 'Regular', 0, ?, ?, ?, ?, 0, 1)",
        (event_id, parent, SPIRIT, AURORA, SERIES),
    )


def _snap(conn, quote, captured_at, **over):
    """Write one snapshot at an explicit time, bypassing change-detection."""
    q = quote(**over)
    q.setdefault("price_decimal", storage.american_to_decimal(q["price_american"]))
    q["captured_at"] = captured_at
    cols = list(q)
    conn.execute(
        f"INSERT INTO odds_snapshots ({','.join(cols)}) VALUES ({','.join('?' * len(cols))})",  # noqa: S608
        tuple(q[c] for c in cols),
    )


@pytest.fixture
def series_db(conn, quote):
    """Spirit 2-0 Aurora, priced under a pre-match parent event and a live child."""
    storage.upsert_matches(conn, [_match(1, MAP1, 1), _match(2, MAP2, 1)])
    _map_event(conn, "1", None)
    _map_event(conn, "2", "1")

    def line(event_id, selection, period, price, captured_at, **over):
        _snap(
            conn,
            quote,
            captured_at,
            event_id=event_id,
            line_key=f"{event_id}|Regular|moneyline|{period}|{selection}|",
            selection=selection,
            period=period,
            price_american=price,
            **over,
        )

    for period, horn in ((1, MAP1), (2, MAP2)):
        for offset, spirit, aurora in ((-1000, -150, 130), (-500, -160, 140), (-100, -170, 150)):
            line("1", "Spirit", period, spirit, horn + offset)
            line("1", "Aurora", period, aurora, horn + offset)
        # Post-horn quotes must never be picked as the close.
        line("1", "Spirit", period, -400, horn + 50)
        line("1", "Aurora", period, 300, horn + 50)
    # Series market (period 0) and a live child price just before the horn.
    for offset, spirit, aurora in ((-1000, -200, 170), (-100, -220, 180)):
        line("1", "Spirit", 0, spirit, MAP1 + offset)
        line("1", "Aurora", 0, aurora, MAP1 + offset)
    line("2", "Spirit", 1, -900, MAP1 - 10, is_live=1)
    line("2", "Aurora", 1, 600, MAP1 - 10, is_live=1)
    conn.commit()
    return conn


# --------------------------------------------------------------------------- de-vig


def test_overround_and_implied_probs():
    assert overround([1.5, 2.5]) == pytest.approx(1.0666667, abs=1e-6)
    assert implied_probs([1.5, 2.5]) == pytest.approx([0.6666667, 0.4], abs=1e-6)


def test_devig_proportional_pinned():
    assert devig_proportional([1.5, 2.5]) == pytest.approx([0.625, 0.375], abs=1e-6)
    assert devig_proportional([1.3, 3.9]) == pytest.approx([0.75, 0.25], abs=1e-6)
    assert devig_proportional([2.5, 3.2, 3.4]) == pytest.approx(
        [0.397370, 0.310446, 0.292184], abs=1e-5
    )


def test_devig_shin_pinned():
    assert devig_shin([1.5, 2.5]) == pytest.approx([0.633333, 0.366667], abs=1e-5)
    assert shin_z([1.5, 2.5]) == pytest.approx(0.0669856, abs=1e-6)
    assert devig_shin([1.9, 1.9]) == pytest.approx([0.5, 0.5], abs=1e-6)
    assert devig_shin([2.5, 3.2, 3.4]) == pytest.approx([0.397688, 0.310332, 0.291980], abs=1e-5)
    assert shin_z([2.5, 3.2, 3.4]) == pytest.approx(0.0033090, abs=1e-6)


def test_shin_equals_proportional_on_fair_book():
    """With no margin there is nothing to attribute to insiders, so both agree."""
    assert devig_shin([2.0, 2.0]) == pytest.approx(devig_proportional([2.0, 2.0]), abs=1e-9)
    assert shin_z([2.0, 2.0]) is None


def test_shin_shrinks_the_longshot():
    prop = devig_proportional([1.3, 3.9])
    shin = devig_shin([1.3, 3.9])
    assert shin[0] > prop[0]
    assert shin[1] < prop[1]


def test_shin_underround_falls_back():
    odds = [1.3, 5.5]
    assert overround(odds) < 1.0
    assert devig_shin(odds) == pytest.approx(devig_proportional(odds), abs=1e-9)


# ----------------------------------------------------------------------- closing line


def test_closing_event_picks_prematch_parent(series_db):
    assert closing_event(series_db, "pinnacle", SERIES) == "1"


def test_map_start_uses_match_order(series_db):
    assert map_start(series_db, SERIES, 0) == MAP1
    assert map_start(series_db, SERIES, 1) == MAP1
    assert map_start(series_db, SERIES, 2) == MAP2
    assert map_start(series_db, SERIES, 3) is None


def test_closing_line_is_last_open_before_map_start(series_db):
    line = closing_probs(series_db, "pinnacle", SERIES, 1)
    assert line is not None
    assert line.captured_at == MAP1 - 100
    assert line.prices["Spirit"] == pytest.approx(storage.american_to_decimal(-170))


def test_map2_uses_second_match_start(series_db):
    line = closing_probs(series_db, "pinnacle", SERIES, 2)
    assert line is not None
    assert line.bound == MAP2
    assert line.captured_at == MAP2 - 100


def test_closed_and_tombstone_rows_are_ignored(series_db, quote):
    for selection, price, status in (("Spirit", -300, "closed"), ("Aurora", 250, "gone")):
        _snap(
            series_db,
            quote,
            MAP1 - 20,
            event_id="1",
            selection=selection,
            period=1,
            price_american=price,
            status=status,
        )
    line = closing_probs(series_db, "pinnacle", SERIES, 1)
    assert line.captured_at == MAP1 - 100
    assert line.prices["Spirit"] == pytest.approx(storage.american_to_decimal(-170))


def test_live_child_rows_never_leak(series_db):
    """The live re-list quotes closer to the horn; it must not become the close."""
    line = closing_probs(series_db, "pinnacle", SERIES, 1)
    assert line.event_id == "1"
    assert line.prices["Spirit"] == pytest.approx(storage.american_to_decimal(-170))
    assert closing_lines(series_db, "pinnacle", "2", "moneyline", 1) == []


def test_placeholder_cutoff_does_not_bound(series_db, quote):
    """A far-future cutoff is a placeholder, so the horn still decides the close."""
    _snap(
        series_db,
        quote,
        MAP1 - 50,
        event_id="1",
        selection="Spirit",
        period=1,
        price_american=-175,
        cutoff_at="2030-01-01T00:00:00Z",
    )
    _snap(
        series_db,
        quote,
        MAP1 - 50,
        event_id="1",
        selection="Aurora",
        period=1,
        price_american=155,
        cutoff_at="2030-01-01T00:00:00Z",
    )
    line = closing_probs(series_db, "pinnacle", SERIES, 1)
    assert line.captured_at == MAP1 - 50
    assert line.prices["Spirit"] == pytest.approx(storage.american_to_decimal(-175))


def test_closing_probs_returns_none_when_unjoined(conn, quote):
    _map_event(conn, "9", None)
    _snap(conn, quote, 10, event_id="9", selection="Spirit")
    _snap(conn, quote, 10, event_id="9", selection="Aurora", price_american=130)
    assert closing_probs(conn, "pinnacle", SERIES, 0) is None


def test_closing_probs_devigs_to_one(series_db):
    line = closing_probs(series_db, "pinnacle", SERIES, 1)
    assert sum(line.selections.values()) == pytest.approx(1.0, abs=1e-9)
    assert line.overround > 1.0
    assert not line.stale_cutoff


# ------------------------------------------------------------------------ CLV report


def test_resolve_outcome_map_and_series(series_db, resolver):
    spirit = resolver.resolve("Spirit")
    aurora = resolver.resolve("Aurora")
    assert resolve_outcome(series_db, SERIES, 1, spirit) == (1, None)
    assert resolve_outcome(series_db, SERIES, 1, aurora) == (0, None)
    assert resolve_outcome(series_db, SERIES, 0, spirit) == (1, None)
    assert resolve_outcome(series_db, SERIES, 0, aurora) == (0, None)


def test_clv_report_scores_map_and_series(series_db, resolver):
    preds = [
        Prediction(SERIES, 1, "Spirit", 0.7, price_taken=1.6),
        Prediction(SERIES, 0, "Spirit", 0.8),
    ]
    report = clv_report(series_db, preds, resolver=resolver)
    assert report.n_scored == 2
    assert not report.skipped
    assert report.model_brier == pytest.approx((0.09 + 0.04) / 2, abs=1e-9)
    assert report.closing_brier is not None
    row = report.rows[0]
    assert row.outcome == 1
    assert row.clv == pytest.approx(1.6 * row.closing_prob - 1.0, abs=1e-9)
    assert row.edge_at_close == pytest.approx(0.7 - row.closing_prob, abs=1e-9)


def test_clv_report_skips_unresolvable(series_db, resolver):
    preds = [
        Prediction(SERIES, 1, "Nobody", 0.7),
        Prediction(SERIES, 3, "Spirit", 0.7),
        Prediction(SERIES, 1, "Spirit", 0.6),
    ]
    report = clv_report(series_db, preds, resolver=resolver)
    assert report.n_scored == 1
    reasons = {reason for _, reason in report.skipped}
    assert "unresolved selection" in reasons
    assert report.model_brier == pytest.approx(0.16, abs=1e-9)


def test_clv_report_scores_dire_side(series_db, resolver):
    report = clv_report(series_db, [Prediction(SERIES, 1, "Aurora", 0.4)], resolver=resolver)
    assert report.rows[0].outcome == 0
    assert report.rows[0].brier == pytest.approx(0.16, abs=1e-9)


def test_before_draft_bounds_earlier(series_db, resolver):
    late = closing_probs(series_db, "pinnacle", SERIES, 1)
    early = closing_probs(series_db, "pinnacle", SERIES, 1, before=MAP1 - 600)
    assert late.captured_at == MAP1 - 100
    assert early.captured_at == MAP1 - 1000


# ------------------------------------------------------------------------------ CLI


def test_load_predictions_rejects_unknown_fields(tmp_path):
    path = tmp_path / "preds.jsonl"
    path.write_text(json.dumps({"series_id": 1, "period": 0, "selection": "x", "oops": 1}) + "\n")
    with pytest.raises(ValueError, match="unknown prediction fields"):
        load_predictions(str(path))


def test_cmd_eval_end_to_end(series_db, tmp_path, capsys, monkeypatch):
    from dota2bets import cli

    db_path = series_db.execute("PRAGMA database_list").fetchone()[2]
    series_db.commit()
    preds = tmp_path / "preds.jsonl"
    preds.write_text(
        json.dumps(
            {
                "series_id": SERIES,
                "period": 1,
                "selection": "Spirit",
                "prob": 0.7,
                "price_taken": 1.6,
            }
        )
        + "\n"
    )
    monkeypatch.setattr(
        AliasResolver,
        "load",
        classmethod(lambda cls, path=None: AliasResolver.from_yaml(ALIAS_YAML)),
    )
    code = cli.main(["--db", db_path, "eval", "--predictions", str(preds)])
    out = capsys.readouterr().out
    assert code == 0
    assert "model Brier" in out
    assert "closing Brier" in out
    assert "mean CLV" in out
