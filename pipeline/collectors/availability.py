"""
Job: Fetch player injury/roster-designation status from ESPN and Sleeper, resolve to
     canonical player_id via the ID crosswalk (read-only), and write a change-log row
     only on first appearance, a tracked-field change, or a source clearing a player
     after two consecutive missed polls (see pipeline/core/injury_changelog.py).
Reads: ESPN injuries endpoint, Sleeper players endpoint, player_id_crosswalk, players,
       teams, games (for season/week resolution), injury_presence (own prior state)
Writes: injuries, injury_presence
Tier: T1
Phase: P3
"""

from __future__ import annotations

import json
import re
from datetime import datetime
from typing import Any

import httpx
import psycopg

from pipeline.core.base import Collector, RunContext, WorkResult
from pipeline.core.db import delete_rows, filter_changed, upsert_rows
from pipeline.core.freshness import get_last_value, set_last_value
from pipeline.core.hashing import hash_row
from pipeline.core.injury_changelog import (
    build_cleared_row,
    build_presence_rows,
    decide_injury_row,
    detect_cleared,
    is_source_outage,
)
from pipeline.core.schedule import resolve_season_week
from pipeline.core.team_aliases import normalize_team_abbr

_ESPN_URL = "https://site.web.api.espn.com/apis/site/v2/sports/football/nfl/injuries"
_SLEEPER_URL = "https://api.sleeper.app/v1/players/nfl"
_SLEEPER_FRESHNESS_KEY = "sleeper:availability"  # distinct from id_spine's own
# "sleeper:crosswalk_enrich" key -- sharing one key would mean whichever collector runs
# first "claims" the day's fetch and the other silently loses its Sleeper read, which
# would break this collector's actual injury-data job (see docs/sources.md).

# Primary: the ESPN athlete numeric id embedded in a player-card link href, e.g.
# ".../nfl/player/_/id/3051775/andrew-billings". Fallback: the same id is also embedded
# in the headshot image href, e.g. ".../headshots/nfl/players/full/3051775.png" --
# verified live to agree with the link-derived id on all 800 entries in today's feed.
_ESPN_ID_LINK_RE = re.compile(r"/id/(\d+)/")
_ESPN_ID_HEADSHOT_RE = re.compile(r"/(\d+)\.\w+$")

_INJURIES_COLS = [
    "player_id",
    "source",
    "source_player_id",
    "season",
    "week",
    "season_type",
    "team",
    "designation",
    "body_part",
    "notes",
    "raw",
    "as_of",
    "is_cleared",
]
_INJURIES_PK = ["source", "source_player_id", "as_of"]


def _parse_espn_timestamp(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _extract_espn_source_player_id(athlete: dict[str, Any]) -> tuple[str, bool]:
    """Returns (source_player_id, extraction_failed). Never guesses -- if both paths
    fail, the caller falls back to a synthetic `unmatched:<espn injury id>` id so the
    row is still stored, never silently dropped."""
    for link in athlete.get("links") or []:
        m = _ESPN_ID_LINK_RE.search(link.get("href") or "")
        if m:
            return m.group(1), False
    headshot_href = (athlete.get("headshot") or {}).get("href") or ""
    m = _ESPN_ID_HEADSHOT_RE.search(headshot_href)
    if m:
        return m.group(1), False
    return "", True


def _espn_raw_subtree(
    *,
    designation: str | None,
    body_part: str | None,
    notes: str | None,
    inj: dict[str, Any],
    athlete: dict[str, Any],
    team: str | None,
) -> dict[str, Any]:
    """Compact trimmed subtree for reprocessing -- drops athlete.links/headshot/team.logos
    (the actual bloat, ~11.7KB/row average measured live) while keeping everything else,
    including a stripped athlete stub, so parsing can be redone later without a re-fetch."""
    return {
        "designation": designation,
        "body_part": body_part,
        "notes": notes,
        "type": inj.get("type"),
        "details": inj.get("details"),
        "source": inj.get("source"),
        "athlete": {
            "displayName": athlete.get("displayName"),
            "position": (athlete.get("position") or {}).get("abbreviation"),
            "team": team,
            "status": (athlete.get("status") or {}).get("name"),
        },
    }


def _parse_espn_rows(espn_json: dict[str, Any]) -> tuple[list[dict[str, Any]], int]:
    rows: list[dict[str, Any]] = []
    extraction_failed = 0

    for team_block in espn_json.get("injuries") or []:
        for inj in team_block.get("injuries") or []:
            athlete = inj.get("athlete") or {}
            source_player_id, failed = _extract_espn_source_player_id(athlete)
            if failed:
                extraction_failed += 1
                source_player_id = f"unmatched:{inj.get('id')}"

            team = normalize_team_abbr((athlete.get("team") or {}).get("abbreviation"))
            designation = inj.get("status")
            body_part = (inj.get("details") or {}).get("type")
            notes = inj.get("longComment") or None
            if not notes:
                items = (athlete.get("notes") or {}).get("items") or []
                if items:
                    notes = items[0].get("text") or None

            rows.append(
                {
                    "source": "espn",
                    "source_player_id": source_player_id,
                    "team": team,
                    "designation": designation,
                    "body_part": body_part,
                    "notes": notes,
                    "raw": _espn_raw_subtree(
                        designation=designation,
                        body_part=body_part,
                        notes=notes,
                        inj=inj,
                        athlete=athlete,
                        team=team,
                    ),
                }
            )
    return rows, extraction_failed


def _parse_sleeper_rows(sleeper_json: dict[str, Any]) -> list[dict[str, Any]]:
    """Scoped to this collector's job (availability, not a general roster tracker):
    only players with a reported injury_status who are on a current roster."""
    rows: list[dict[str, Any]] = []
    for source_player_id, p in sleeper_json.items():
        if not p.get("injury_status") or not p.get("team"):
            continue
        team = p.get("team")  # already matches nflverse -- no remap needed
        designation = p.get("injury_status")
        body_part = p.get("injury_body_part")
        notes = p.get("injury_notes")
        self_gsis = (p.get("gsis_id") or "").strip() or None
        self_espn = str(p["espn_id"]) if p.get("espn_id") else None
        rows.append(
            {
                "source": "sleeper",
                "source_player_id": source_player_id,
                "team": team,
                "designation": designation,
                "body_part": body_part,
                "notes": notes,
                "self_gsis": self_gsis,
                "self_espn": self_espn,
                "raw": {
                    "designation": designation,
                    "body_part": body_part,
                    "notes": notes,
                    "practice_participation": p.get("practice_participation"),
                    "practice_description": p.get("practice_description"),
                    "status": p.get("status"),
                    "team": team,
                },
            }
        )
    return rows


def _resolve_player_ids(
    crosswalk_by_espn_id: dict[str, str],
    crosswalk_by_sleeper_id: dict[str, str],
    players_by_gsis_id: dict[str, str],
    rows: list[dict[str, Any]],
) -> dict[tuple[str, str], str | None]:
    """Read-only, exact-ID resolution only -- never name matching (a name+team fallback
    was tested and rejected: it produced a real collision between two different players
    both named "Blake Miller" on the same team). ESPN rows resolve via
    crosswalk.espn_id. Sleeper rows try crosswalk.sleeper_id, then Sleeper's own
    self-reported gsis_id against players, then Sleeper's own self-reported espn_id
    against crosswalk.espn_id, in that order. Unresolved stays None -- the caller stores
    the row anyway (player_id null), it's never dropped."""
    resolved: dict[tuple[str, str], str | None] = {}
    for row in rows:
        source = row["source"]
        source_player_id = row["source_player_id"]
        if source == "espn":
            resolved[(source, source_player_id)] = crosswalk_by_espn_id.get(source_player_id)
            continue

        player_id = crosswalk_by_sleeper_id.get(source_player_id)
        if player_id is None and row.get("self_gsis"):
            player_id = players_by_gsis_id.get(row["self_gsis"])
        if player_id is None and row.get("self_espn"):
            player_id = crosswalk_by_espn_id.get(row["self_espn"])
        resolved[(source, source_player_id)] = player_id
    return resolved


def _finalize(row: dict[str, Any], now: datetime, hash_fields: list[str]) -> dict[str, Any]:
    row = dict(row)
    row["content_hash"] = hash_row({k: row.get(k) for k in hash_fields})
    row["updated_at"] = now
    return row


def _fetch_crosswalk_by_espn_id(conn: psycopg.Connection, espn_ids: list[str]) -> dict[str, str]:
    if not espn_ids:
        return {}
    with conn.cursor() as cur:
        cur.execute(
            "SELECT espn_id, player_id FROM player_id_crosswalk WHERE espn_id = ANY(%s)",
            (espn_ids,),
        )
        return dict(cur.fetchall())


def _fetch_crosswalk_by_sleeper_id(
    conn: psycopg.Connection, sleeper_ids: list[str]
) -> dict[str, str]:
    if not sleeper_ids:
        return {}
    with conn.cursor() as cur:
        cur.execute(
            "SELECT sleeper_id, player_id FROM player_id_crosswalk WHERE sleeper_id = ANY(%s)",
            (sleeper_ids,),
        )
        return dict(cur.fetchall())


def _fetch_players_by_gsis_id(conn: psycopg.Connection, gsis_ids: list[str]) -> dict[str, str]:
    if not gsis_ids:
        return {}
    with conn.cursor() as cur:
        cur.execute("SELECT player_id FROM players WHERE player_id = ANY(%s)", (gsis_ids,))
        return {row[0]: row[0] for row in cur.fetchall()}


def _fetch_presence_state(
    conn: psycopg.Connection, source: str
) -> dict[str, dict[str, Any]]:
    """{source_player_id: {consecutive_misses, last_seen_at}} currently tracked for ONE
    source in injury_presence. consecutive_misses feeds detect_cleared's prior_counts;
    last_seen_at is carried forward unchanged for any id that's still missing this poll
    (a miss doesn't move it -- only an actual sighting does). Absent from the dict means
    never tracked (0 misses, no prior sighting to carry forward)."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT source_player_id, consecutive_misses, last_seen_at "
            "FROM injury_presence WHERE source = %s",
            (source,),
        )
        rows = cur.fetchall()
    return {
        r[0]: {"consecutive_misses": r[1], "last_seen_at": r[2]}
        for r in rows
    }


def _fetch_last_state_by_source(
    conn: psycopg.Connection, source: str
) -> dict[str, dict[str, Any]]:
    """Latest stored row per source_player_id for ONE source -- unscoped by season/week
    (a player's last change may predate the current week; season/week is a write-time
    label, not a validity window -- see docs/phases/P3.md). Serves both the change/no-op
    decision (via decide_injury_row) and, restricted by the caller to is_cleared=false
    entries, the "who was last known active" set detect_cleared needs."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT DISTINCT ON (source_player_id)
                source_player_id, player_id, team, designation, body_part, notes, is_cleared
            FROM injuries
            WHERE source = %s
            ORDER BY source_player_id, as_of DESC
            """,
            (source,),
        )
        rows = cur.fetchall()
    return {
        r[0]: {
            "player_id": r[1],
            "team": r[2],
            "designation": r[3],
            "body_part": r[4],
            "notes": r[5],
            "is_cleared": r[6],
        }
        for r in rows
    }


class AvailabilityCollector(Collector):
    name = "availability"

    def should_run(self, ctx: RunContext) -> bool:
        # ESPN status is time-sensitive; the dispatcher calendar already caps calls to
        # its own Wed/Fri cadence. Sleeper's own ~1/day throttle is enforced in fetch().
        return True

    def fetch(self, ctx: RunContext) -> dict[str, Any]:
        espn_resp = httpx.get(_ESPN_URL, timeout=30)
        espn_resp.raise_for_status()
        espn_json = espn_resp.json()

        sleeper_json = None
        today = ctx.now.date().isoformat()
        if get_last_value(ctx.conn, _SLEEPER_FRESHNESS_KEY) != today:
            sleeper_resp = httpx.get(_SLEEPER_URL, timeout=60)
            sleeper_resp.raise_for_status()
            sleeper_json = sleeper_resp.json()

        return {"espn": espn_json, "sleeper": sleeper_json}

    def validate(self, raw: dict[str, Any]) -> dict[str, Any]:
        espn_json = raw.get("espn")
        if not espn_json or "injuries" not in espn_json or "timestamp" not in espn_json:
            raise ValueError("espn missing expected keys: {'injuries', 'timestamp'}")

        sleeper_json = raw.get("sleeper")
        if sleeper_json is not None and not isinstance(sleeper_json, dict):
            raise ValueError("sleeper payload is not a dict")

        espn_rows, espn_id_extraction_failed = _parse_espn_rows(espn_json)
        sleeper_rows = _parse_sleeper_rows(sleeper_json) if sleeper_json else []

        return {
            "espn_timestamp": _parse_espn_timestamp(espn_json["timestamp"]),
            "rows": espn_rows + sleeper_rows,
            "espn_id_extraction_failed": espn_id_extraction_failed,
            "sleeper_fetched": sleeper_json is not None,
        }

    def store(self, ctx: RunContext, validated: dict[str, Any]) -> WorkResult:
        conn = ctx.conn
        rows = validated["rows"]
        sleeper_fetched = validated["sleeper_fetched"]

        espn_season, espn_week, espn_season_type = resolve_season_week(
            conn, validated["espn_timestamp"]
        )
        sleeper_season, sleeper_week, sleeper_season_type = resolve_season_week(conn, ctx.now)

        espn_ids = sorted(
            {
                r["source_player_id"]
                for r in rows
                if r["source"] == "espn" and not r["source_player_id"].startswith("unmatched:")
            }
        )
        self_espn_ids = sorted(
            {r["self_espn"] for r in rows if r["source"] == "sleeper" and r.get("self_espn")}
        )
        sleeper_ids = sorted({r["source_player_id"] for r in rows if r["source"] == "sleeper"})
        self_gsis_ids = sorted(
            {r["self_gsis"] for r in rows if r["source"] == "sleeper" and r.get("self_gsis")}
        )

        crosswalk_by_espn_id = _fetch_crosswalk_by_espn_id(conn, espn_ids + self_espn_ids)
        crosswalk_by_sleeper_id = _fetch_crosswalk_by_sleeper_id(conn, sleeper_ids)
        players_by_gsis_id = _fetch_players_by_gsis_id(conn, self_gsis_ids)

        resolved = _resolve_player_ids(
            crosswalk_by_espn_id, crosswalk_by_sleeper_id, players_by_gsis_id, rows
        )

        unresolved_espn = 0
        unresolved_sleeper = 0
        finalized: list[dict[str, Any]] = []
        for row in rows:
            player_id = resolved.get((row["source"], row["source_player_id"]))
            if player_id is None:
                if row["source"] == "espn":
                    unresolved_espn += 1
                else:
                    unresolved_sleeper += 1

            if row["source"] == "espn":
                season, week, season_type = espn_season, espn_week, espn_season_type
            else:
                season, week, season_type = sleeper_season, sleeper_week, sleeper_season_type

            finalized.append(
                {
                    "player_id": player_id,
                    "source": row["source"],
                    "source_player_id": row["source_player_id"],
                    "season": season,
                    "week": week,
                    "season_type": season_type,
                    "team": row["team"],
                    "designation": row["designation"],
                    "body_part": row["body_part"],
                    "notes": row["notes"],
                    "raw": json.dumps(row["raw"], sort_keys=True),
                    "as_of": ctx.now,
                }
            )

        to_write: list[dict[str, Any]] = []
        counts = {"first_seen": 0, "changed": 0, "cleared": 0, "skipped_unchanged": 0}
        outage_guard: dict[str, dict[str, int]] = {}

        for source in ("espn", "sleeper"):
            if source == "sleeper" and not sleeper_fetched:
                # Sleeper wasn't polled this run -- an empty present_this_poll here must
                # never be read as "everyone on Sleeper disappeared".
                continue

            source_rows = [r for r in finalized if r["source"] == source]
            last_state = _fetch_last_state_by_source(conn, source)

            present_this_poll: set[str] = set()
            for row in source_rows:
                source_player_id = row["source_player_id"]
                present_this_poll.add(source_player_id)
                decision = decide_injury_row(last_state.get(source_player_id), row)
                if decision is None:
                    counts["skipped_unchanged"] += 1
                    continue
                counts[decision] += 1
                to_write.append({**row, "is_cleared": False})

            active_from_db = {
                sid: state for sid, state in last_state.items() if not state["is_cleared"]
            }
            if is_source_outage(len(active_from_db), len(present_this_poll)):
                # A truncated/outage response must never mark the whole active roster
                # cleared -- that would silently re-add everyone as "new" first_seen rows
                # on the next good poll. Skip clearance for this source this run; the
                # auditor alerts on agent_runs.meta['outage_guard'].
                outage_guard[source] = {
                    "active": len(active_from_db),
                    "seen": len(present_this_poll),
                }
                continue

            season, week, season_type = (
                (espn_season, espn_week, espn_season_type)
                if source == "espn"
                else (sleeper_season, sleeper_week, sleeper_season_type)
            )
            presence_state = _fetch_presence_state(conn, source)
            prior_counts = {
                sid: state["consecutive_misses"] for sid, state in presence_state.items()
            }
            to_clear, updated_counts = detect_cleared(
                active_from_db, present_this_poll, prior_counts
            )
            for source_player_id in to_clear:
                prior_state = active_from_db[source_player_id]
                cleared_row = build_cleared_row(
                    prior_state,
                    source=source,
                    source_player_id=source_player_id,
                    player_id=prior_state["player_id"],
                    season=season,
                    week=week,
                    season_type=season_type,
                    as_of=ctx.now,
                )
                cleared_row["raw"] = json.dumps(cleared_row["raw"], sort_keys=True)
                to_write.append(cleared_row)
                counts["cleared"] += 1

            presence_rows = [
                {**row, "source": source}
                for row in build_presence_rows(updated_counts, presence_state, ctx.now)
            ]
            if presence_rows:
                upsert_rows(
                    conn,
                    "injury_presence",
                    presence_rows,
                    conflict_cols=["source", "source_player_id"],
                    update_cols=["last_seen_at", "consecutive_misses"],
                )
            if to_clear:
                delete_rows(
                    conn,
                    "injury_presence",
                    "source = %s AND source_player_id = ANY(%s)",
                    (source, to_clear),
                )

        injuries_hash_fields = [c for c in _INJURIES_COLS if c not in _INJURIES_PK]
        injuries_rows = [_finalize(r, ctx.now, injuries_hash_fields) for r in to_write]
        # A pass-through here since as_of is unique per run -- every row is "new" by PK,
        # so this never actually drops anything, but it's kept for the same reason every
        # other collector runs writes through it: a uniform store() shape. The real dedup
        # now happens above, in decide_injury_row, before rows are even built.
        injuries_rows = filter_changed(conn, "injuries", _INJURIES_PK, injuries_rows)
        written = upsert_rows(
            conn,
            "injuries",
            injuries_rows,
            conflict_cols=_INJURIES_PK,
            update_cols=injuries_hash_fields + ["content_hash", "updated_at"],
        )

        if sleeper_fetched:
            set_last_value(conn, _SLEEPER_FRESHNESS_KEY, ctx.now.date().isoformat())

        meta: dict[str, Any] = {
            "unresolved_espn": unresolved_espn,
            "unresolved_sleeper": unresolved_sleeper,
            "espn_id_extraction_failed": validated["espn_id_extraction_failed"],
            "sleeper_fetched": sleeper_fetched,
            **counts,
        }
        if outage_guard:
            meta["outage_guard"] = outage_guard

        return WorkResult(written, meta=meta)
