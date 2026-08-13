"""Pinnacle odds via the public guest API that powers their own web client.

Pinnacle is the reference book for this project: low margin, high limits and no
limiting of winning accounts, which makes its closing line the benchmark any model
has to beat. Both pre-match and in-play prices come through the same endpoints.

Two endpoints are combined per poll:
  * ``/sports/{id}/matchups``        -> events, participants, league, start time
  * ``/sports/{id}/markets/straight`` -> moneyline / spread / total prices and limits
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

log = logging.getLogger(__name__)

BASE_URL = "https://guest.api.arcadia.pinnacle.com/0.1"
# Public key embedded in Pinnacle's own web client.
GUEST_API_KEY = "CmX2KcMrXuFmNg6YFbmTxE0y9CIrOi0R"
ESPORTS_SPORT_ID = 12
DEFAULT_LEAGUE_FILTER = "dota"


class PinnacleFetcher:
    """Fetch and normalise Dota 2 markets from Pinnacle's guest API."""

    name = "pinnacle"

    def __init__(
        self,
        sport_id: int = ESPORTS_SPORT_ID,
        league_filter: str = DEFAULT_LEAGUE_FILTER,
        timeout: float = 30.0,
    ) -> None:
        self.sport_id = sport_id
        self.league_filter = league_filter.lower()
        self._client = httpx.Client(
            base_url=BASE_URL,
            timeout=timeout,
            headers={"X-API-Key": GUEST_API_KEY, "User-Agent": "dota2bets/0.1"},
        )

    def close(self) -> None:
        self._client.close()

    def _get(self, path: str, **params: Any) -> Any:
        resp = self._client.get(path, params=params)
        resp.raise_for_status()
        return resp.json()

    def raw(self) -> tuple[list[dict], list[dict]]:
        matchups = self._get(f"/sports/{self.sport_id}/matchups", withSpecials="false")
        markets = self._get(f"/sports/{self.sport_id}/markets/straight")
        return matchups, markets

    def fetch_with_raw(self) -> tuple[list[dict[str, Any]], Any]:
        """Quotes plus the exact payload they came from, for the archive."""
        matchups, markets = self.raw()
        quotes = normalise(matchups, markets, league_filter=self.league_filter)
        return quotes, {"matchups": matchups, "markets": markets}

    def fetch(self) -> list[dict[str, Any]]:
        return self.fetch_with_raw()[0]


def _participant_names(matchup: dict[str, Any]) -> tuple[str | None, str | None]:
    home = away = None
    for p in matchup.get("participants") or []:
        if p.get("alignment") == "home":
            home = p.get("name")
        elif p.get("alignment") == "away":
            away = p.get("name")
    return home, away


def _ordered_participants(matchup: dict[str, Any]) -> list[str | None]:
    return [
        p.get("name")
        for p in sorted(matchup.get("participants") or [], key=lambda p: p.get("order", 0))
    ]


def normalise(
    matchups: list[dict[str, Any]],
    markets: list[dict[str, Any]],
    league_filter: str = DEFAULT_LEAGUE_FILTER,
) -> list[dict[str, Any]]:
    """Join matchups with their straight markets and flatten into quote dicts.

    Moneyline prices reference a ``participantId`` that the matchups endpoint does not
    publish; Pinnacle assigns those ids in participant order, so ascending id maps to
    ascending ``order``. Spreads and totals use an explicit ``designation`` instead.
    """
    index: dict[int, dict[str, Any]] = {}
    for m in matchups:
        league = (m.get("league") or {}).get("name") or ""
        if league_filter and league_filter not in league.lower():
            continue
        index[m["id"]] = m

    quotes: list[dict[str, Any]] = []
    for market in markets:
        matchup = index.get(market.get("matchupId"))
        if matchup is None:
            continue

        home, away = _participant_names(matchup)
        ordered = _ordered_participants(matchup)
        league = (matchup.get("league") or {}).get("name")
        period = market.get("period")
        mtype = market.get("type")
        limit = next(
            (
                lim.get("amount")
                for lim in market.get("limits") or []
                if lim.get("type") == "maxRiskStake"
            ),
            None,
        )
        # Moneyline participant ids ascend with participant order.
        ml_ids = sorted(
            {
                p["participantId"]
                for p in market.get("prices") or []
                if p.get("participantId") is not None
            }
        )

        for price in market.get("prices") or []:
            selection = price.get("designation")
            if selection in ("home", "away"):
                # Resolve to the team name so the key stays stable even when Pinnacle
                # switches between designation- and participantId-style payloads.
                selection = (home if selection == "home" else away) or selection
            elif selection is None and price.get("participantId") is not None:
                idx = ml_ids.index(price["participantId"])
                selection = ordered[idx] if idx < len(ordered) else str(price["participantId"])
            if selection is None:
                continue
            points = price.get("points")
            # Include points in the key so each alternate line is its own series.
            line_key = "|".join(
                str(x)
                for x in (
                    matchup["id"],
                    matchup.get("units") or "Regular",
                    mtype,
                    period,
                    selection,
                    "" if points is None else points,
                )
            )
            quotes.append(
                {
                    "source": "pinnacle",
                    "line_key": line_key,
                    "event_id": str(matchup["id"]),
                    "league": league,
                    "home": home,
                    "away": away,
                    "start_time": matchup.get("startTime"),
                    "market_type": mtype,
                    "period": period,
                    "units": matchup.get("units"),
                    "selection": selection,
                    "points": points,
                    "price_american": price.get("price"),
                    "limit_amount": limit,
                    "is_live": int(bool(matchup.get("isLive"))),
                    "status": market.get("status"),
                    "cutoff_at": market.get("cutoffAt"),
                }
            )
    return quotes
