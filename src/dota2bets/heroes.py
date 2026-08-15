"""Hero identity and name resolution for Dota 2.

Resolves hero inputs from numeric IDs, localized names, internal short names,
or community aliases into strongly-typed Hero records. Unknown names raise with
the unrecognised input named and close-match suggestions.
"""

from __future__ import annotations

import difflib
import json
from collections.abc import Sequence
from dataclasses import dataclass
from importlib.resources import files
from pathlib import Path
from typing import Any


class UnknownHeroError(KeyError):
    """Raised when a hero identifier or name cannot be resolved."""

    def __init__(self, query: str | int, suggestions: Sequence[str] = ()) -> None:
        self.query = query
        self.suggestions = tuple(suggestions)
        msg = f"Unknown hero: {query!r}"
        if suggestions:
            msg += f" (did you mean: {', '.join(suggestions)}?)"
        super().__init__(msg)


@dataclass(frozen=True)
class Hero:
    """A Dota 2 hero definition."""

    hero_id: int
    name: str  # e.g. "npc_dota_hero_antimage"
    localized_name: str  # e.g. "Anti-Mage"


def _norm(s: str) -> str:
    """Normalise hero names for comparison: strip whitespace, lowercase, remove hyphens."""
    return "".join(c for c in s.casefold() if c.isalnum())


# Community aliases mapping normalized query -> canonical localized_name or hero_id
ALIASES: dict[str, str] = {
    "aa": "Ancient Apparition",
    "alch": "Alchemist",
    "am": "Anti-Mage",
    "bara": "Spirit Breaker",
    "bat": "Batrider",
    "beast": "Beastmaster",
    "bh": "Bounty Hunter",
    "brew": "Brewmaster",
    "bristle": "Bristleback",
    "brood": "Broodmother",
    "cent": "Centaur Warrunner",
    "centaur": "Centaur Warrunner",
    "ck": "Chaos Knight",
    "clock": "Clockwerk",
    "cm": "Crystal Maiden",
    "cotl": "Keeper of the Light",
    "cw": "Clockwerk",
    "dark seer": "Dark Seer",
    "dawn": "Dawnbreaker",
    "dk": "Dragon Knight",
    "dp": "Death Prophet",
    "drow": "Drow Ranger",
    "ds": "Dark Seer",
    "dw": "Dark Willow",
    "ench": "Enchantress",
    "es": "Earthshaker",
    "et": "Elder Titan",
    "furion": "Nature's Prophet",
    "fv": "Faceless Void",
    "grim": "Grimstroke",
    "gyro": "Gyrocopter",
    "invo": "Invoker",
    "kaolin": "Earth Spirit",
    "kotl": "Keeper of the Light",
    "kunkka": "Kunkka",
    "lc": "Legion Commander",
    "leoric": "Wraith King",
    "ls": "Lifestealer",
    "mag": "Magnus",
    "mk": "Monkey King",
    "naix": "Lifestealer",
    "necro": "Necrophos",
    "np": "Nature's Prophet",
    "od": "Outworld Destroyer",
    "omni": "Omniknight",
    "pa": "Phantom Assassin",
    "pl": "Phantom Lancer",
    "potm": "Mirana",
    "qop": "Queen of Pain",
    "rhasta": "Shadow Shaman",
    "sb": "Spirit Breaker",
    "sd": "Shadow Demon",
    "sf": "Shadow Fiend",
    "shaker": "Earthshaker",
    "sk": "Sand King",
    "sky": "Skywrath Mage",
    "snap": "Snapfire",
    "spec": "Spectre",
    "ss": "Shadow Shaman",
    "storm": "Storm Spirit",
    "ta": "Templar Assassin",
    "tb": "Terrorblade",
    "timber": "Timbersaw",
    "tide": "Tidehunter",
    "tp": "Treant Protector",
    "treant": "Treant Protector",
    "troll": "Troll Warlord",
    "underlord": "Underlord",
    "venge": "Vengeful Spirit",
    "veno": "Venomancer",
    "voker": "Invoker",
    "void": "Faceless Void",
    "wd": "Witch Doctor",
    "wind": "Windranger",
    "wisp": "Io",
    "wk": "Wraith King",
    "wr": "Windranger",
    "ww": "Winter Wyvern",
    "willow": "Dark Willow",
    "zeus": "Zeus",
    "zuus": "Zeus",
}


class HeroRegistry:
    """Registry of known Dota 2 heroes with multiple lookup methods."""

    def __init__(self, heroes: Sequence[Hero]) -> None:
        self._heroes = list(heroes)
        self._by_id: dict[int, Hero] = {h.hero_id: h for h in self._heroes}
        self._by_norm: dict[str, Hero] = {}

        for h in self._heroes:
            # Map normalized localized_name, e.g. "antimage" -> Hero(1, ...)
            self._by_norm[_norm(h.localized_name)] = h
            # Map normalized internal name, e.g. "npcdotaheroantimage" -> Hero(1, ...)
            self._by_norm[_norm(h.name)] = h
            # Map short internal name (strip "npc_dota_hero_")
            short_name = h.name.removeprefix("npc_dota_hero_")
            self._by_norm[_norm(short_name)] = h

        for alias, target in ALIASES.items():
            norm_alias = _norm(alias)
            if norm_alias not in self._by_norm:
                norm_target = _norm(target)
                if norm_target in self._by_norm:
                    self._by_norm[norm_alias] = self._by_norm[norm_target]

    @property
    def heroes(self) -> Sequence[Hero]:
        return list(self._heroes)

    def get_by_id(self, hero_id: int) -> Hero | None:
        return self._by_id.get(hero_id)

    def resolve(self, query: int | str) -> Hero:
        """Resolve a hero by numeric ID, localized name, internal name, or alias."""
        if isinstance(query, int):
            hero = self._by_id.get(query)
            if hero is not None:
                return hero
            raise UnknownHeroError(query)

        query_str = str(query).strip()
        if query_str.isdigit():
            hero = self._by_id.get(int(query_str))
            if hero is not None:
                return hero
            raise UnknownHeroError(query)

        norm_q = _norm(query_str)
        if norm_q in self._by_norm:
            return self._by_norm[norm_q]

        # Find close matches across localized names and aliases
        candidates = [h.localized_name for h in self._heroes] + list(ALIASES.keys())
        matches = difflib.get_close_matches(query_str, candidates, n=3, cutoff=0.5)
        # Deduplicate matches preserving order
        dedup_matches: list[str] = []
        for m in matches:
            canonical = m
            norm_m = _norm(m)
            if norm_m in self._by_norm:
                canonical = self._by_norm[norm_m].localized_name
            if canonical not in dedup_matches:
                dedup_matches.append(canonical)
        raise UnknownHeroError(query, dedup_matches)


_DEFAULT_REGISTRY: HeroRegistry | None = None


def load_heroes(path: Path | str | None = None) -> HeroRegistry:
    """Load hero definitions from JSON file."""
    if path is not None:
        raw_text = Path(path).read_text(encoding="utf-8")
    else:
        raw_text = (files("dota2bets") / "heroes.json").read_text(encoding="utf-8")
    data: list[dict[str, Any]] = json.loads(raw_text)
    heroes = [
        Hero(hero_id=int(item["id"]), name=item["name"], localized_name=item["localized_name"])
        for item in data
    ]
    return HeroRegistry(heroes)


def get_default_registry() -> HeroRegistry:
    """Get or create singleton default registry."""
    global _DEFAULT_REGISTRY
    if _DEFAULT_REGISTRY is None:
        _DEFAULT_REGISTRY = load_heroes()
    return _DEFAULT_REGISTRY


def resolve_hero(query: int | str, registry: HeroRegistry | None = None) -> Hero:
    """Convenience helper to resolve a hero."""
    reg = registry or get_default_registry()
    return reg.resolve(query)


def hero_by_id(hero_id: int, registry: HeroRegistry | None = None) -> Hero | None:
    """Convenience helper to look up a hero by ID."""
    reg = registry or get_default_registry()
    return reg.get_by_id(hero_id)
