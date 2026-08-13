"""Fetcher interface shared by all odds sources.

Every source normalises its payload into the same flat quote dict so that
`storage.insert_odds_snapshots` and every downstream model can stay source-agnostic.

Quote keys: source, line_key, event_id, league, home, away, start_time, market_type,
period, units, selection, points, price_american, limit_amount, is_live, status,
cutoff_at.

`line_key` must uniquely and *stably* identify one continuously-priced selection across
polls -- it is what change-detection and closing-line lookups join on.
"""

from __future__ import annotations

from typing import Any, Protocol


class OddsFetcher(Protocol):
    """A source of Dota 2 odds."""

    name: str

    def fetch(self) -> list[dict[str, Any]]:
        """Return the current quotes for all available Dota 2 markets."""
        ...

    def close(self) -> None: ...
