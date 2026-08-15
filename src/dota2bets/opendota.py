"""OpenDota client and parsers for professional Dota 2 matches.

The free tier is keyless but rate limited (60 calls/min, 2000/day). Set
``OPENDOTA_API_KEY`` to raise those limits. Parsing is kept separate from fetching so
the parsers can be tested against recorded fixtures without network access.
"""

from __future__ import annotations

import logging
import os
import time
from collections.abc import Iterator, Sequence
from datetime import datetime
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
        self._patch_index: dict[str, int] | None = None
        self._patch_timeline: list[tuple[int, int]] | None = None

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

    def explorer(self, sql: str) -> list[dict[str, Any]]:
        """Query the OpenDota PostgreSQL Explorer endpoint."""
        data = self.get("/explorer", sql=sql)
        if isinstance(data, dict) and data.get("err"):
            raise RuntimeError(f"OpenDota Explorer query failed: {data['err']}")
        if isinstance(data, dict) and "rows" in data:
            return data["rows"]
        if isinstance(data, list):
            return data
        return []

    def patch_index(self) -> dict[str, int]:
        """Fetch patch constants and map version name (e.g. '7.41') to integer id (e.g. 60)."""
        if self._patch_index is None:
            data = self.get("/constants/patch")
            self._patch_index = {
                p["name"]: p["id"]
                for p in data
                if isinstance(p, dict) and "name" in p and "id" in p
            }
            self._patch_timeline = build_patch_timeline(data)
        return self._patch_index

    def patch_timeline(self) -> list[tuple[int, int]]:
        """Patch releases as ``(released_at, patch_id)``, oldest first.

        The Explorer ``match_patch`` table can lag a release by days: matches played
        after 7.41 shipped were still labelled '7.40' months later. `/matches/{id}`
        assigns the patch by start time instead, so we do the same and treat the name
        only as a fallback.
        """
        if self._patch_timeline is None:
            self.patch_index()
        return self._patch_timeline or []


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


def build_patch_timeline(constants: Sequence[dict[str, Any]]) -> list[tuple[int, int]]:
    """Turn `/constants/patch` into ``(released_at, patch_id)`` pairs, oldest first."""
    timeline: list[tuple[int, int]] = []
    for p in constants:
        if not isinstance(p, dict) or "id" not in p or not p.get("date"):
            continue
        try:
            released = int(
                datetime.fromisoformat(str(p["date"]).replace("Z", "+00:00")).timestamp()
            )
        except ValueError:
            continue
        timeline.append((released, p["id"]))
    timeline.sort()
    return timeline


def resolve_patch(
    start_time: int | None,
    patch_name: Any,
    patch_map: dict[str, int],
    patch_timeline: Sequence[tuple[int, int]] | None = None,
) -> int | None:
    """The patch a match was played on, preferring its start time over its label.

    Explorer's `match_patch` lags a release — matches played in the two days after
    7.41 shipped are still labelled '7.40'. Since the patch column feeds the ratings'
    patch-transition feature, a stale label shifts a transition by days. `/matches/{id}`
    derives the patch from the start time, so we do too, and fall back to the label
    only when the start time cannot place the match.
    """
    if isinstance(patch_name, int):
        return patch_name
    if start_time and patch_timeline:
        latest: int | None = None
        for released, patch_id in patch_timeline:
            if start_time >= released:
                latest = patch_id
            else:
                break
        if latest is not None:
            return latest
    if isinstance(patch_name, str):
        return patch_map.get(patch_name)
    return None


def parse_explorer_rows(
    matches: Sequence[dict[str, Any]],
    picks_bans: Sequence[dict[str, Any]],
    player_matches: Sequence[dict[str, Any]],
    patch_map: dict[str, int],
    *,
    patch_timeline: Sequence[tuple[int, int]] | None = None,
    fetched_at: int | None = None,
) -> dict[str, Any]:
    """Parse Explorer query results into rows for each table.

    Returns a dict with keys ``match``, ``draft_events``, ``players``, ``timeseries``,
    ``teams`` and ``rosters``, where ``match`` is a list of match rows.
    """
    fetched_at = fetched_at or int(time.time())

    pb_by_match: dict[int, list[dict[str, Any]]] = {}
    for pb in picks_bans:
        mid = pb.get("match_id")
        if mid is not None and pb.get("hero_id") is not None:
            pb_by_match.setdefault(mid, []).append(pb)

    pm_by_match: dict[int, list[dict[str, Any]]] = {}
    for pm in player_matches:
        mid = pm.get("match_id")
        if mid is not None:
            pm_by_match.setdefault(mid, []).append(pm)

    match_rows: list[dict[str, Any]] = []
    draft_events: list[dict[str, Any]] = []
    players: list[dict[str, Any]] = []
    timeseries: list[dict[str, Any]] = []
    teams: list[dict[str, Any]] = []
    rosters: list[dict[str, Any]] = []

    for m in matches:
        match_id = m["match_id"]
        patch_idx = resolve_patch(
            m.get("start_time"), m.get("patch"), patch_map, patch_timeline
        )

        m_players = pm_by_match.get(match_id, [])
        m_pb = pb_by_match.get(match_id, [])

        is_complete = (patch_idx is not None) and (len(m_players) == 10)
        detail_fetched = fetched_at if is_complete else None

        league_id = m.get("leagueid") if m.get("league_id") is None else m.get("league_id")
        match_rows.append(
            {
                "match_id": match_id,
                "league_id": league_id,
                "league_name": m.get("league_name") or m.get("name"),
                "series_id": m.get("series_id"),
                "series_type": m.get("series_type"),
                "start_time": m.get("start_time"),
                "duration": m.get("duration"),
                "pre_game_duration": None,
                "patch": patch_idx,
                "radiant_team_id": m.get("radiant_team_id"),
                "radiant_name": m.get("radiant_name"),
                "dire_team_id": m.get("dire_team_id"),
                "dire_name": m.get("dire_name"),
                "radiant_win": _as_int_bool(m.get("radiant_win")),
                "radiant_score": m.get("radiant_score"),
                "dire_score": m.get("dire_score"),
                "detail_fetched_at": detail_fetched,
            }
        )

        for i, pb in enumerate(m_pb):
            draft_events.append(
                {
                    "match_id": match_id,
                    "ord": pb.get("order") if pb.get("order") is not None else pb.get("ord", i),
                    "is_pick": int(bool(pb.get("is_pick"))),
                    "hero_id": pb["hero_id"],
                    "team": pb.get("team"),
                }
            )

        for p in m_players:
            slot = p.get("player_slot", 0)
            is_radiant = 1 if slot < 128 else 0
            players.append(
                {
                    "match_id": match_id,
                    "player_slot": slot,
                    "account_id": p.get("account_id"),
                    "player_name": None,
                    "hero_id": p.get("hero_id"),
                    "is_radiant": is_radiant,
                    "lane_role": p.get("lane_role"),
                    "kills": p.get("kills"),
                    "deaths": p.get("deaths"),
                    "assists": p.get("assists"),
                    "gold_per_min": p.get("gold_per_min"),
                    "xp_per_min": p.get("xp_per_min"),
                }
            )
            team_id = m.get("radiant_team_id") if is_radiant else m.get("dire_team_id")
            if team_id and p.get("account_id"):
                rosters.append(
                    {
                        "team_id": team_id,
                        "account_id": p["account_id"],
                        "player_name": None,
                        "observed_at": m.get("start_time") or fetched_at,
                    }
                )

        gold = m.get("radiant_gold_adv") or []
        xp = m.get("radiant_xp_adv") or []
        for i in range(max(len(gold), len(xp))):
            timeseries.append(
                {
                    "match_id": match_id,
                    "minute": i,
                    "radiant_gold_adv": gold[i] if i < len(gold) else None,
                    "radiant_xp_adv": xp[i] if i < len(xp) else None,
                }
            )

        for tid, name in (
            (m.get("radiant_team_id"), m.get("radiant_name")),
            (m.get("dire_team_id"), m.get("dire_name")),
        ):
            if tid:
                teams.append(
                    {
                        "team_id": tid,
                        "name": name,
                        "last_seen": m.get("start_time") or fetched_at,
                    }
                )

    return {
        "match": match_rows,
        "draft_events": draft_events,
        "players": players,
        "timeseries": timeseries,
        "teams": teams,
        "rosters": rosters,
    }


def _as_int_bool(v: Any) -> int | None:
    return None if v is None else int(bool(v))
