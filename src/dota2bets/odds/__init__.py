"""Pluggable odds sources."""

from __future__ import annotations

from typing import Any

from .base import OddsFetcher
from .pinnacle import PinnacleFetcher
from .theoddsapi import TheOddsApiFetcher

FETCHERS = {
    "pinnacle": PinnacleFetcher,
    "theoddsapi": TheOddsApiFetcher,
}

__all__ = ["FETCHERS", "OddsFetcher", "PinnacleFetcher", "TheOddsApiFetcher", "build_fetchers"]


def build_fetchers(names: list[str], **kwargs: Any) -> list[OddsFetcher]:
    """Instantiate the named fetchers, skipping any that cannot be configured."""
    out: list[OddsFetcher] = []
    for name in names:
        try:
            out.append(FETCHERS[name](**kwargs.get(name, {})))
        except KeyError:
            raise SystemExit(
                f"Unknown odds source {name!r}. Available: {', '.join(FETCHERS)}"
            ) from None
        except RuntimeError as exc:
            print(f"[skip] {name}: {exc}")
    return out
