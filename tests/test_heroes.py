"""Tests for hero identification and resolution in heroes.py."""

from __future__ import annotations

import pytest

from dota2bets.heroes import (
    Hero,
    HeroRegistry,
    UnknownHeroError,
    get_default_registry,
    hero_by_id,
    load_heroes,
    resolve_hero,
)


def test_custom_registry() -> None:
    hero = Hero(hero_id=999, name="npc_dota_hero_custom", localized_name="Custom Hero")
    reg = HeroRegistry([hero])
    assert reg.resolve(999).localized_name == "Custom Hero"
    assert reg.resolve("custom hero").hero_id == 999


def test_default_registry_loads() -> None:
    reg = get_default_registry()
    assert len(reg.heroes) >= 120
    am = reg.get_by_id(1)
    assert am is not None
    assert am.localized_name == "Anti-Mage"
    assert am.name == "npc_dota_hero_antimage"

    reg2 = load_heroes()
    assert len(reg2.heroes) == len(reg.heroes)


def test_resolve_by_numeric_id() -> None:
    hero = resolve_hero(1)
    assert hero.hero_id == 1
    assert hero.localized_name == "Anti-Mage"

    # As string numeric
    hero2 = resolve_hero("1")
    assert hero2.hero_id == 1


def test_resolve_by_localized_name() -> None:
    # Exact case
    assert resolve_hero("Anti-Mage").hero_id == 1
    # Lowercase with hyphen
    assert resolve_hero("anti-mage").hero_id == 1
    # Without hyphen / spaces
    assert resolve_hero("anti mage").hero_id == 1
    assert resolve_hero("Crystal Maiden").hero_id == 5
    assert resolve_hero("crystal maiden").hero_id == 5


def test_resolve_by_internal_name() -> None:
    # Full npc name
    assert resolve_hero("npc_dota_hero_antimage").hero_id == 1
    assert resolve_hero("npc_dota_hero_crystal_maiden").hero_id == 5
    # Short internal name
    assert resolve_hero("antimage").hero_id == 1
    assert resolve_hero("crystal_maiden").hero_id == 5


def test_resolve_by_aliases() -> None:
    assert resolve_hero("am").hero_id == 1
    assert resolve_hero("cm").hero_id == 5
    assert resolve_hero("kotl").hero_id == 90
    assert resolve_hero("furion").hero_id == 53
    assert resolve_hero("qop").hero_id == 39
    assert resolve_hero("sf").hero_id == 11
    assert resolve_hero("potm").hero_id == 9


def test_hero_by_id() -> None:
    assert hero_by_id(1) is not None
    assert hero_by_id(1).localized_name == "Anti-Mage"
    assert hero_by_id(999999) is None


def test_unknown_name_raises_and_names_input() -> None:
    with pytest.raises(UnknownHeroError) as exc_info:
        resolve_hero("nonexistent_hero_xyz")
    assert "nonexistent_hero_xyz" in str(exc_info.value)
    assert exc_info.value.query == "nonexistent_hero_xyz"


def test_unknown_id_raises() -> None:
    with pytest.raises(UnknownHeroError) as exc_info:
        resolve_hero(99999)
    assert "99999" in str(exc_info.value)


def test_suggestions_on_typo() -> None:
    with pytest.raises(UnknownHeroError) as exc_info:
        resolve_hero("Anti-Meg")
    msg = str(exc_info.value)
    assert "Anti-Mage" in msg
