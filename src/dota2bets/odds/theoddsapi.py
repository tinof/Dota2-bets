"""The Odds API fetcher — aggregates many books, so it measures market *dispersion*.

Pinnacle gives the sharp reference line; this gives the soft books to bet into. Needs a
key (free tier: 500 requests/month) in ``ODDS_API_KEY``. Dota 2 coverage is
intermittent, so `fetch` returns an empty list rather than raising when the sport is
not currently listed.
"""

from __future__ import annotations

import logging
import os
from typing import Any

import httpx

log = logging.getLogger(__name__)

BASE_URL = "https://api.the-odds-api.com/v4"
SPORT_KEY = "esports_dota2"


class TheOddsApiFetcher:
    """Fetch Dota 2 h2h odds across all books The Odds API covers."""

    name = "theoddsapi"

    def __init__(
        self,
        api_key: str | None = None,
        regions: str = "eu,uk",
        markets: str = "h2h",
        timeout: float = 30.0,
    ) -> None:
        self.api_key = api_key or os.environ.get("ODDS_API_KEY")
        if not self.api_key:
            raise RuntimeError(
                "ODDS_API_KEY is not set. Get a free key at https://the-odds-api.com "
                "and export it, or run the recorder with --sources pinnacle only."
            )
        self.regions = regions
        self.markets = markets
        self._client = httpx.Client(
            base_url=BASE_URL, timeout=timeout, headers={"User-Agent": "dota2bets/0.1"}
        )

    def close(self) -> None:
        self._client.close()

    def available_sports(self) -> list[str]:
        resp = self._client.get("/sports", params={"apiKey": self.api_key, "all": "true"})
        resp.raise_for_status()
        return [s["key"] for s in resp.json()]

    def raw(self) -> list[dict[str, Any]]:
        resp = self._client.get(
            f"/sports/{SPORT_KEY}/odds",
            params={
                "apiKey": self.api_key,
                "regions": self.regions,
                "markets": self.markets,
                "oddsFormat": "decimal",
            },
        )
        if resp.status_code == 404:
            log.warning("The Odds API has no active %s markets right now", SPORT_KEY)
            return []
        resp.raise_for_status()
        remaining = resp.headers.get("x-requests-remaining")
        if remaining is not None:
            log.info("The Odds API quota remaining: %s", remaining)
        return resp.json()

    def fetch(self) -> list[dict[str, Any]]:
        return normalise(self.raw())


def normalise(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Flatten The Odds API event payloads into quote dicts (one per book/outcome)."""
    quotes: list[dict[str, Any]] = []
    for ev in events:
        home, away = ev.get("home_team"), ev.get("away_team")
        for book in ev.get("bookmakers") or []:
            for market in book.get("markets") or []:
                for outcome in market.get("outcomes") or []:
                    name = outcome.get("name")
                    points = outcome.get("point")
                    line_key = "|".join(
                        str(x)
                        for x in (
                            book.get("key"),
                            ev.get("id"),
                            market.get("key"),
                            name,
                            "" if points is None else points,
                        )
                    )
                    quotes.append(
                        {
                            "source": f"theoddsapi:{book.get('key')}",
                            "line_key": line_key,
                            "event_id": str(ev.get("id")),
                            "league": ev.get("sport_title"),
                            "home": home,
                            "away": away,
                            "start_time": ev.get("commence_time"),
                            "market_type": market.get("key"),
                            "period": 0,
                            "units": "Regular",
                            "selection": name,
                            "points": points,
                            # This source is queried in decimal; store both consistently.
                            "price_american": _decimal_to_american(outcome.get("price")),
                            "price_decimal": outcome.get("price"),
                            "limit_amount": None,
                            "is_live": None,
                            "status": None,
                            "cutoff_at": ev.get("commence_time"),
                        }
                    )
    return quotes


def _decimal_to_american(price: float | None) -> float | None:
    if not price or price <= 1:
        return None
    if price >= 2:
        return round((price - 1) * 100, 2)
    return round(-100 / (price - 1), 2)
