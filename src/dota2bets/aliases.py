"""Join bookmaker odds events to OpenDota teams and series.

Pinnacle publishes short team names ("Spirit", "LGD") while OpenDota stores registered
ones ("Team Spirit", "LGD Gaming"), and it lists kills markets as parallel pseudo-events
("Spirit (Kills)"). Until an odds event can be pointed at the series it prices, nothing
downstream is computable -- no closing-line value, no backtest -- so this is the gate in
front of every evaluation.

The mapping is a checked-in table rather than fuzzy string matching: a wrong join is
silent and corrupts every number derived from it, so an unknown name is reported for a
human to curate instead of being guessed at.

Resolution has two independent parts:

* **name -> team**, from ``aliases.yaml`` (exact, curated).
* **event -> series**, by team pair plus nearest start time. Pinnacle's ``startTime``
  is the *scheduled* slot and drifts from when the first map actually began, in either
  direction and by over an hour, so this is a nearest-match within a tolerance rather
  than an equality join.

Pinnacle also issues a second event id for the same series when it goes live, and a
third for kills markets, linking them by ``parentId``. That field never reached
``odds_snapshots``, but the raw archive kept it, so children inherit their parent's
series and map-2/3 prices recorded under a live child stay attached to the right series.
"""

from __future__ import annotations

import gzip
import json
import logging
import re
import sqlite3
import time
from collections.abc import Iterator, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from .storage import parse_ts

log = logging.getLogger(__name__)

DEFAULT_TOLERANCE_S = 8 * 3600
_KILLS_RE = re.compile(r"\s*\(kills\)\s*$", re.IGNORECASE)


@dataclass(frozen=True)
class Team:
    team_id: int
    name: str


def _norm(name: str) -> str:
    """Collapse whitespace and case so `"Nigma Galaxy "` matches `"nigma galaxy"`."""
    return " ".join(name.split()).casefold()


def split_kills(name: str) -> tuple[str, bool]:
    """Strip Pinnacle's `(Kills)` pseudo-event suffix, reporting whether it was there."""
    stripped = _KILLS_RE.sub("", name)
    return stripped, stripped != name


class AliasResolver:
    """Maps bookmaker team names onto OpenDota team ids."""

    def __init__(self, teams: Sequence[Team], index: dict[str, Team]) -> None:
        self.teams = list(teams)
        self._index = index

    @classmethod
    def load(cls, path: Path | str | None = None) -> AliasResolver:
        if path is None:
            from importlib.resources import files

            text = (files("dota2bets") / "aliases.yaml").read_text(encoding="utf-8")
        else:
            text = Path(path).read_text(encoding="utf-8")
        return cls.from_yaml(text)

    @classmethod
    def from_yaml(cls, text: str) -> AliasResolver:
        doc = yaml.safe_load(text) or {}
        teams: list[Team] = []
        index: dict[str, Team] = {}
        for entry in doc.get("teams") or []:
            team = Team(int(entry["team_id"]), str(entry["name"]).strip())
            teams.append(team)
            for name in (team.name, *(entry.get("aliases") or [])):
                key = _norm(str(name))
                clash = index.get(key)
                if clash is not None and clash.team_id != team.team_id:
                    raise ValueError(
                        f"alias {name!r} maps to both {clash.name} ({clash.team_id}) "
                        f"and {team.name} ({team.team_id})"
                    )
                index[key] = team
        return cls(teams, index)

    def resolve(self, name: str | None) -> Team | None:
        if not name:
            return None
        base, _ = split_kills(name)
        return self._index.get(_norm(base))

    def resolve_pair(
        self, home: str | None, away: str | None
    ) -> tuple[Team | None, Team | None, bool]:
        """Resolve both sides, reporting whether this is a kills pseudo-event."""
        is_kills = any(split_kills(n)[1] for n in (home, away) if n)
        return self.resolve(home), self.resolve(away), is_kills


def parent_links_from_archive(archive_root: Path | str, source: str = "pinnacle") -> dict[str, str]:
    """Map child event id -> parent event id, read out of the raw payload archive.

    `normalise` drops Pinnacle's `parentId`, so the archive is the only record of which
    live and kills events belong to which parent series.
    """
    links: dict[str, str] = {}
    root = Path(archive_root) / source
    for path in sorted(root.glob("*/*.json.gz")):
        try:
            with gzip.open(path, "rt", encoding="utf-8") as fh:
                payload = json.load(fh)
        except (OSError, ValueError) as exc:
            log.warning("skipping unreadable archive %s: %s", path, exc)
            continue
        for matchup in payload.get("matchups") or []:
            parent_id = matchup.get("parentId")
            if parent_id is not None:
                links[str(matchup["id"])] = str(parent_id)
    return links


@dataclass
class ResolveReport:
    """What `resolve_events` managed to join, and everything a human must look at."""

    events: int = 0
    mapped_teams: int = 0
    mapped_series: int = 0
    unresolved_names: list[tuple[str, int]] = field(default_factory=list)
    unjoined_events: list[tuple[str, str, str]] = field(default_factory=list)
    ambiguous: list[tuple[str, str, str]] = field(default_factory=list)

    def format(self) -> str:
        lines = [
            f"events           {self.events}",
            f"teams resolved   {self.mapped_teams}",
            f"series joined    {self.mapped_series}",
        ]
        if self.unresolved_names:
            lines.append("\nUnresolved names (add to aliases.yaml):")
            lines += [f"  {name:32} {n} snapshots" for name, n in self.unresolved_names]
        if self.unjoined_events:
            lines.append("\nEvents with no matching series (backfill may be behind):")
            lines += [f"  {eid:12} {h} vs {a}" for eid, h, a in self.unjoined_events]
        if self.ambiguous:
            lines.append("\nAmbiguous: same pair meets twice inside the tolerance:")
            lines += [f"  {eid:12} {h} vs {a}" for eid, h, a in self.ambiguous]
        if not (self.unresolved_names or self.unjoined_events or self.ambiguous):
            lines.append("\nNothing unresolved.")
        return "\n".join(lines)


def _events(conn: sqlite3.Connection, source: str) -> Iterator[sqlite3.Row]:
    yield from conn.execute(
        "SELECT source, event_id, units, home, away, MIN(start_time) start_time, "
        "       COUNT(*) snapshots "
        "FROM odds_snapshots WHERE source = ? GROUP BY source, event_id, units, home, away "
        "ORDER BY event_id",
        (source,),
    ).fetchall()


def _candidate_series(
    conn: sqlite3.Connection, home_id: int, away_id: int
) -> list[tuple[int, int]]:
    """(series_id, first map start) for every series these two teams played."""
    rows = conn.execute(
        "SELECT series_id, MIN(start_time) s0 FROM matches "
        "WHERE series_id IS NOT NULL AND ("
        "  (radiant_team_id = ? AND dire_team_id = ?) OR "
        "  (radiant_team_id = ? AND dire_team_id = ?)) "
        "GROUP BY series_id",
        (home_id, away_id, away_id, home_id),
    ).fetchall()
    return [(r["series_id"], r["s0"]) for r in rows if r["s0"] is not None]


def resolve_events(
    conn: sqlite3.Connection,
    resolver: AliasResolver,
    parent_links: dict[str, str] | None = None,
    source: str = "pinnacle",
    tolerance_s: int = DEFAULT_TOLERANCE_S,
    dry_run: bool = False,
) -> ResolveReport:
    """Populate `event_series_map` from the odds events currently in the database.

    Idempotent: re-running replaces rows rather than accumulating them, so it is safe to
    run repeatedly as the backfill catches up with the events being recorded.
    """
    parent_links = parent_links or {}
    report = ResolveReport()
    unresolved: dict[str, int] = {}
    rows: list[tuple[Any, ...]] = []
    series_by_event: dict[str, int] = {}
    now = int(time.time())

    events = list(_events(conn, source))
    # Parents first, so a child can inherit a series its own start time might miss.
    events.sort(key=lambda e: (e["event_id"] in parent_links, e["event_id"]))

    for ev in events:
        report.events += 1
        event_id = ev["event_id"]
        home, away, is_kills = resolver.resolve_pair(ev["home"], ev["away"])
        for raw, team in ((ev["home"], home), (ev["away"], away)):
            if raw and team is None:
                base, _ = split_kills(raw)
                unresolved[base] = unresolved.get(base, 0) + ev["snapshots"]
        if home and away:
            report.mapped_teams += 1

        parent_id = parent_links.get(event_id)
        series_id: int | None = None
        skew: int | None = None

        if home and away:
            start = parse_ts(ev["start_time"])
            candidates = _candidate_series(conn, home.team_id, away.team_id)
            if candidates and start is not None:
                candidates.sort(key=lambda c: abs(c[1] - start))
                near = [c for c in candidates if abs(c[1] - start) <= tolerance_s]
                if near:
                    series_id, s0 = near[0]
                    skew = start - s0
                    if len(near) > 1:
                        report.ambiguous.append((event_id, ev["home"], ev["away"]))
            elif candidates:
                # No usable start time; only safe when the pair met exactly once.
                if len(candidates) == 1:
                    series_id = candidates[0][0]

        # A live or kills child inherits its parent's series: Pinnacle re-lists the same
        # series under a new id, and the child's own start time can be an hour off.
        if series_id is None and parent_id in series_by_event:
            series_id = series_by_event[parent_id]

        if series_id is not None:
            series_by_event[event_id] = series_id
            report.mapped_series += 1
        elif home and away:
            report.unjoined_events.append((event_id, ev["home"], ev["away"]))

        rows.append(
            (
                source,
                event_id,
                ev["units"],
                int(is_kills),
                parent_id,
                home.team_id if home else None,
                away.team_id if away else None,
                series_id,
                skew,
                now,
            )
        )

    report.unresolved_names = sorted(unresolved.items(), key=lambda kv: -kv[1])
    if not dry_run:
        conn.executemany(
            "INSERT OR REPLACE INTO event_series_map ("
            "source, event_id, units, is_kills, parent_event_id, home_team_id, away_team_id, "
            "series_id, start_skew_s, resolved_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
            rows,
        )
        conn.commit()
    return report
