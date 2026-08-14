"""OpenDota client and parsers for professional Dota 2 matches.

The free tier is keyless but rate limited (60 calls/min, 2000/day). Set
``OPENDOTA_API_KEY`` to raise those limits. Parsing is kept separate from fetching so
the parsers can be tested against recorded fixtures without network access.
"""

from __future__ import annotations

import logging
import os
import time
from collections.abc import Iterator
from typing import Any

import httpx

log = logging.getLogger(__name__)

BASE_URL = "https://api.opendota.com/api"
# Free tier allows 60 calls/minute; stay comfortably under it.
DEFAULT_DELAY_S = 1.2
# A key raises the ceiling to 1200/min, so a keyed crawl need not creep.
KEYED_DELAY_S = 0.12
MAX_ATTEMPTS = 7
BACKOFF_BASE_S = 5
BACKOFF_CAP_S = 300


class OpenDotaClient:
    """Thin, politely-throttled OpenDota REST client."""

    def __init__(
        self,
        api_key: str | None = None,
        delay_s: float | None = None,
        timeout: float = 30.0,
    ) -> None:
        self.api_key = api_key or os.environ.get("OPENDOTA_API_KEY")
        if delay_s is None:
            delay_s = KEYED_DELAY_S if self.api_key else DEFAULT_DELAY_S
        self.delay_s = delay_s
        self._client = httpx.Client(
            base_url=BASE_URL, timeout=timeout, headers={"User-Agent": "dota2bets/0.1"}
        )
        self._last_call = 0.0

    def __enter__(self) -> OpenDotaClient:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def close(self) -> None:
        self._client.close()

    def _throttle(self) -> None:
        elapsed = time.monotonic() - self._last_call
        if elapsed < self.delay_s:
            time.sleep(self.delay_s - elapsed)
        self._last_call = time.monotonic()

    def get(self, path: str, **params: Any) -> Any:
        if self.api_key:
            params["api_key"] = self.api_key
        for attempt in range(MAX_ATTEMPTS):
            self._throttle()
            resp = self._client.get(path, params=params)
            if resp.status_code == 429:
                # A sustained throttle outlasts a linear backoff: a long crawl that
                # raises here has to restart, so back off far enough to ride it out.
                wait = min(BACKOFF_CAP_S, BACKOFF_BASE_S * 2**attempt)
                retry_after = resp.headers.get("Retry-After")
                if retry_after and retry_after.isdigit():
                    wait = max(wait, int(retry_after))
                log.warning(
                    "OpenDota rate limited (attempt %d/%d); sleeping %ss",
                    attempt + 1,
                    MAX_ATTEMPTS,
                    wait,
                )
                time.sleep(wait)
                continue
            resp.raise_for_status()
            return resp.json()
        resp.raise_for_status()

    def pro_matches(self, less_than_match_id: int | None = None) -> list[dict[str, Any]]:
        """One page (~100) of pro match summaries, newest first."""
        params = {}
        if less_than_match_id is not None:
            params["less_than_match_id"] = less_than_match_id
        return self.get("/proMatches", **params)

    def iter_pro_matches(
        self, max_matches: int = 500, before_match_id: int | None = None
    ) -> Iterator[dict[str, Any]]:
        """Page backwards through pro matches until `max_matches` are yielded.

        `before_match_id` resumes an interrupted crawl from where it stopped instead of
        re-walking the pages already stored.
        """
        seen = 0
        cursor: int | None = before_match_id
        while seen < max_matches:
            page = self.pro_matches(less_than_match_id=cursor)
            if not page:
                return
            for m in page:
                yield m
                seen += 1
                if seen >= max_matches:
                    return
            cursor = min(m["match_id"] for m in page)

    def match(self, match_id: int) -> dict[str, Any]:
        return self.get(f"/matches/{match_id}")


def parse_match_summary(m: dict[str, Any]) -> dict[str, Any]:
    """Row for `matches` from a /proMatches entry (no detail columns)."""
    return {
        "match_id": m["match_id"],
        "league_id": m.get("leagueid"),
        "league_name": m.get("league_name"),
        "series_id": m.get("series_id"),
        "series_type": m.get("series_type"),
        "start_time": m.get("start_time"),
        "duration": m.get("duration"),
        "radiant_team_id": m.get("radiant_team_id"),
        "radiant_name": m.get("radiant_name"),
        "dire_team_id": m.get("dire_team_id"),
        "dire_name": m.get("dire_name"),
        "radiant_win": _as_int_bool(m.get("radiant_win")),
        "radiant_score": m.get("radiant_score"),
        "dire_score": m.get("dire_score"),
    }


def parse_match_detail(d: dict[str, Any], fetched_at: int | None = None) -> dict[str, Any]:
    """Split a /matches/{id} payload into rows for each table.

    Returns a dict with keys ``match``, ``draft_events``, ``players``, ``timeseries``,
    ``teams`` and ``rosters``.
    """
    fetched_at = fetched_at or int(time.time())
    match_id = d["match_id"]

    match_row = parse_match_summary(
        {**d, "league_name": (d.get("league") or {}).get("name") or d.get("league_name")}
    )
    match_row.update(
        {
            "patch": d.get("patch"),
            "pre_game_duration": d.get("pre_game_duration"),
            "detail_fetched_at": fetched_at,
        }
    )

    draft_events = [
        {
            "match_id": match_id,
            "ord": pb.get("order", i),
            "is_pick": int(bool(pb.get("is_pick"))),
            "hero_id": pb["hero_id"],
            "team": pb.get("team"),
        }
        for i, pb in enumerate(d.get("picks_bans") or [])
        if pb.get("hero_id") is not None
    ]

    players = []
    rosters = []
    for p in d.get("players") or []:
        players.append(
            {
                "match_id": match_id,
                "player_slot": p.get("player_slot"),
                "account_id": p.get("account_id"),
                "player_name": p.get("name") or p.get("personaname"),
                "hero_id": p.get("hero_id"),
                "is_radiant": _as_int_bool(p.get("isRadiant")),
                "lane_role": p.get("lane_role"),
                "kills": p.get("kills"),
                "deaths": p.get("deaths"),
                "assists": p.get("assists"),
                "gold_per_min": p.get("gold_per_min"),
                "xp_per_min": p.get("xp_per_min"),
            }
        )
        team_id = d.get("radiant_team_id") if p.get("isRadiant") else d.get("dire_team_id")
        if team_id and p.get("account_id"):
            rosters.append(
                {
                    "team_id": team_id,
                    "account_id": p["account_id"],
                    "player_name": p.get("name") or p.get("personaname"),
                    "observed_at": d.get("start_time") or fetched_at,
                }
            )

    gold = d.get("radiant_gold_adv") or []
    xp = d.get("radiant_xp_adv") or []
    timeseries = [
        {
            "match_id": match_id,
            "minute": i,
            "radiant_gold_adv": gold[i] if i < len(gold) else None,
            "radiant_xp_adv": xp[i] if i < len(xp) else None,
        }
        for i in range(max(len(gold), len(xp)))
    ]

    teams = [
        {"team_id": tid, "name": name, "last_seen": d.get("start_time") or fetched_at}
        for tid, name in (
            (d.get("radiant_team_id"), d.get("radiant_name")),
            (d.get("dire_team_id"), d.get("dire_name")),
        )
        if tid
    ]

    return {
        "match": match_row,
        "draft_events": draft_events,
        "players": players,
        "timeseries": timeseries,
        "teams": teams,
        "rosters": rosters,
    }


def _as_int_bool(v: Any) -> int | None:
    return None if v is None else int(bool(v))
