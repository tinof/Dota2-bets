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
