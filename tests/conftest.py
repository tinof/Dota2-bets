from __future__ import annotations

import json
from pathlib import Path

import pytest

FIXTURES = Path(__file__).parent / "fixtures"


def load(name: str):
    return json.loads((FIXTURES / name).read_text())


@pytest.fixture
def match_detail():
    return load("opendota_match.json")


@pytest.fixture
def pro_matches():
    return load("opendota_promatches.json")


@pytest.fixture
def pinnacle_payload():
    return load("pinnacle.json")


@pytest.fixture
def conn(tmp_path):
    from dota2bets import storage

    c = storage.connect(tmp_path / "test.sqlite")
    yield c
    c.close()


@pytest.fixture
def quote():
    """Factory for a normalised odds quote; pass keywords to override fields."""

    def _make(**over):
        q = {
            "source": "pinnacle",
            "line_key": "1|Regular|moneyline|0|Spirit|",
            "event_id": "1",
            "league": "Dota 2 - The International",
            "home": "Spirit",
            "away": "Aurora",
            "start_time": "2026-08-14T02:00:00Z",
            "market_type": "moneyline",
            "period": 0,
            "units": "Regular",
            "selection": "Spirit",
            "points": None,
            "price_american": -150,
            "limit_amount": 500,
            "is_live": 0,
            "status": "open",
            "cutoff_at": "2026-08-14T02:00:00Z",
        }
        q.update(over)
        return q

    return _make
