"""
Job: Compute injury-driven availability-impact signals -- snap-share redistribution,
     replacement depth-order delta, OL/secondary cluster counts, and practice-trend risk
     -- for players and teams with reported injuries this week.
Reads: injuries, snaps (staged), depth (staged), players, teams
Writes: signals (sector='availability')
Tier: T1
Phase: P3
"""

from __future__ import annotations

from typing import Any

import polars as pl
import psycopg

from pipeline.core.base import Analyst, RunContext, WorkResult
from pipeline.core.db import upsert_rows
from pipeline.core.freshness import get_last_value

# players.position values (nflreadpy convention) -- not depth.pos_abb's side-specific
# slots (LT/RT/...), since not every injured player has a depth-chart entry but every
# player has a position on the players table.
_OL_POSITIONS = {"C", "G", "T", "OL"}
_SECONDARY_POSITIONS = {"CB", "S", "DB", "FS", "SS"}
_REDISTRIBUTION_POSITIONS = {"WR", "RB", "TE"}

_HEALTHY_DESIGNATIONS = {None, "Active"}

# Ordinal severity for practice_trend_risk -- an unrecognized designation string (a new
# value neither source's vocabulary has produced before) contributes no direction, not a
# guess, since we can't say whether it's better or worse without fabricating a ranking.
_DESIGNATION_SEVERITY: dict[str, int] = {
    "Active": 0,
    "Questionable": 1,
    "Doubtful": 2,
    "Out": 3,
    "Injured Reserve": 4,
    "IR": 4,
    "PUP": 4,
}

_INPUTS_VERSION_TAGS = ("snap_counts", "depth_charts")

_SIGNAL_SCHEMA: dict[str, Any] = {
    "game_id": pl.Utf8,
    "season": pl.Int64,
    "week": pl.Int64,
    "team": pl.Utf8,
    "player_id": pl.Utf8,
    "sector": pl.Utf8,
    "signal": pl.Utf8,
    "value": pl.Float64,
    "league_pct": pl.Float32,
    "sample_n": pl.Int64,
    "stability": pl.Float32,
    "as_of": pl.Datetime,
    "inputs_version": pl.Utf8,
}
_SIGNAL_COLS = list(_SIGNAL_SCHEMA)


# --------------------------------------------------------------------------------------
# Pure computation helpers (no DB access -- unit-tested against synthetic data)
# --------------------------------------------------------------------------------------


def _build_redistribution_roster(
    flagged: list[dict[str, Any]],
    player_positions: dict[str, tuple[str | None, str | None]],
    snap_share: dict[str, float],
) -> list[dict[str, Any]]:
    """Builds the roster rows _compute_redistribution needs: the currently-flagged
    skill-position players (team taken from their injuries row, fresher than
    players.latest_team) plus every healthy player in the same (team, position) group
    that has at least one flagged teammate."""
    injured_ids = {r["player_id"] for r in flagged}
    relevant_groups: set[tuple[str | None, str | None]] = set()
    roster: list[dict[str, Any]] = []

    for r in flagged:
        position = player_positions.get(r["player_id"], (None, None))[1]
        if position not in _REDISTRIBUTION_POSITIONS:
            continue
        team = r["team"]
        relevant_groups.add((team, position))
        roster.append(
            {
                "player_id": r["player_id"],
                "team": team,
                "position": position,
                "snap_share": snap_share.get(r["player_id"]),
                "injured": True,
            }
        )

    for player_id, (team, position) in player_positions.items():
        if player_id in injured_ids:
            continue
        if (team, position) not in relevant_groups:
            continue
        roster.append(
            {
                "player_id": player_id,
                "team": team,
                "position": position,
                "snap_share": snap_share.get(player_id),
                "injured": False,
            }
        )
    return roster


def _compute_redistribution(roster: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """For each flagged player in a redistribution-eligible group with a known snap
    share, redistributes it proportionally across healthy teammates (same team+position)
    by their own snap share. Gains from multiple flagged teammates in the same group are
    summed per healthy player, since signals has one row per (player, signal) -- never
    emits two rows for the same key in one run."""
    at_risk: dict[str, float] = {}
    gains: dict[str, float] = {}

    by_group: dict[tuple[str | None, str | None], list[dict[str, Any]]] = {}
    for r in roster:
        by_group.setdefault((r["team"], r["position"]), []).append(r)

    for members in by_group.values():
        injured_members = [m for m in members if m["injured"] and m.get("snap_share") is not None]
        healthy_members = [
            m for m in members if not m["injured"] and m.get("snap_share") is not None
        ]
        healthy_total = sum(m["snap_share"] for m in healthy_members)
        for inj in injured_members:
            at_risk[inj["player_id"]] = inj["snap_share"]
            if healthy_total <= 0:
                continue
            for h in healthy_members:
                gain = inj["snap_share"] * (h["snap_share"] / healthy_total)
                gains[h["player_id"]] = gains.get(h["player_id"], 0.0) + gain

    rows = [
        {"player_id": pid, "signal": "snap_share_at_risk", "value": v} for pid, v in at_risk.items()
    ]
    rows += [
        {"player_id": pid, "signal": "snap_share_redistribution_gain", "value": v}
        for pid, v in gains.items()
    ]
    return rows


def _build_depth_groups(
    flagged: list[dict[str, Any]], depth_rows: list[dict[str, Any]]
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Splits depth rows into (flagged, healthy) using the flagged player_id set."""
    flagged_ids = {r["player_id"] for r in flagged}
    flagged_depth = [d for d in depth_rows if d["player_id"] in flagged_ids]
    healthy_depth = [d for d in depth_rows if d["player_id"] not in flagged_ids]
    return flagged_depth, healthy_depth


def _compute_depth_rank_delta(
    flagged_depth: list[dict[str, Any]], healthy_depth: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Depth-chart ORDER only, not a performance-quality estimate -- see
    docs/signals.md's replacement_depth_rank_delta entry. For each flagged player with a
    depth entry, finds the lowest-ranked healthy teammate at the same (team, pos_abb)
    slot ranked below them; emits the rank delta on that teammate. No candidate (no
    healthy teammate ranked below, or no depth entry for the flagged player at all) ->
    no signal, not a guess."""
    healthy_by_group: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for h in healthy_depth:
        healthy_by_group.setdefault((h["team"], h["pos_abb"]), []).append(h)

    out: list[dict[str, Any]] = []
    for inj in flagged_depth:
        candidates = [
            h
            for h in healthy_by_group.get((inj["team"], inj["pos_abb"]), [])
            if h["pos_rank"] > inj["pos_rank"]
        ]
        if not candidates:
            continue
        backup = min(candidates, key=lambda h: h["pos_rank"])
        out.append(
            {
                "player_id": backup["player_id"],
                "signal": "replacement_depth_rank_delta",
                "value": float(backup["pos_rank"] - inj["pos_rank"]),
            }
        )
    return out


def _build_flagged_positions(
    flagged: list[dict[str, Any]], player_positions: dict[str, tuple[str | None, str | None]]
) -> list[dict[str, Any]]:
    out = []
    for r in flagged:
        position = player_positions.get(r["player_id"], (None, None))[1]
        if position is None:
            continue
        out.append({"team": r["team"], "position": position})
    return out


def _compute_cluster_counts(flagged_positions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Team-level raw counts (not a pre-baked boolean/threshold) of currently-flagged
    players in the OL/secondary position groups -- "cluster" stays a display/consumer
    decision. Only emits for teams with at least one flagged player in that group."""
    ol_counts: dict[str, int] = {}
    sec_counts: dict[str, int] = {}
    for r in flagged_positions:
        if r["team"] is None:
            continue
        if r["position"] in _OL_POSITIONS:
            ol_counts[r["team"]] = ol_counts.get(r["team"], 0) + 1
        elif r["position"] in _SECONDARY_POSITIONS:
            sec_counts[r["team"]] = sec_counts.get(r["team"], 0) + 1

    rows = [
        {"team": t, "signal": "ol_cluster_count", "value": float(c)} for t, c in ol_counts.items()
    ]
    rows += [
        {"team": t, "signal": "secondary_cluster_count", "value": float(c)}
        for t, c in sec_counts.items()
    ]
    return rows


def _build_trend_sequences(
    rows: list[tuple[str, str, str | None, Any]],
) -> dict[str, list[str]]:
    """rows: (player_id, source, designation, as_of), ordered by player_id, source,
    as_of ASC. Picks whichever source has more snapshots for a player this week (ESPN
    wins ties, since its designation vocabulary is more structured), and returns that
    source's chronological designation sequence."""
    by_player_source: dict[tuple[str, str], list[str]] = {}
    for player_id, source, designation, _as_of in rows:
        if designation is None:
            continue
        by_player_source.setdefault((player_id, source), []).append(designation)

    by_player: dict[str, dict[str, list[str]]] = {}
    for (player_id, source), seq in by_player_source.items():
        by_player.setdefault(player_id, {})[source] = seq

    sequences: dict[str, list[str]] = {}
    for player_id, sources in by_player.items():
        espn_seq = sources.get("espn", [])
        sleeper_seq = sources.get("sleeper", [])
        sequences[player_id] = espn_seq if len(espn_seq) >= len(sleeper_seq) else sleeper_seq
    return sequences


def _compute_trend_risk(sequences: dict[str, list[str]]) -> list[dict[str, Any]]:
    """+1 per step that worsens, -1 per step that improves, 0 if flat, an unrecognized
    designation, or only one snapshot. sample_n = number of snapshots seen. The honest
    substitute for a real Wed/Thu/Fri participation grid, which neither ESPN nor Sleeper
    provides (docs/sources.md) -- an observed-designation trend, not an estimate of
    actual practice participation."""
    rows = []
    for player_id, seq in sequences.items():
        score = 0
        for prev, curr in zip(seq, seq[1:], strict=False):
            prev_sev = _DESIGNATION_SEVERITY.get(prev)
            curr_sev = _DESIGNATION_SEVERITY.get(curr)
            if prev_sev is None or curr_sev is None:
                continue
            if curr_sev > prev_sev:
                score += 1
            elif curr_sev < prev_sev:
                score -= 1
        rows.append(
            {
                "player_id": player_id,
                "signal": "practice_trend_risk",
                "value": float(score),
                "sample_n": len(seq),
            }
        )
    return rows


def _signal_row(
    *,
    player_id: str | None,
    team: str | None,
    signal: str,
    value: float,
    sample_n: int | None,
    season: int,
    week: int,
    as_of: Any,
    inputs_version: str,
) -> dict[str, Any]:
    """Casts every field to its _SIGNAL_SCHEMA dtype explicitly -- e.g. `value` is
    always a Python float here even though `_compute_cluster_counts` counts are whole
    numbers, and `sample_n` is always an int or exactly None, never left for Polars to
    infer from whichever rows happen to come first when six different producers' output
    gets concatenated (see _build_signals_frame)."""
    return {
        "game_id": None,
        "season": int(season),
        "week": int(week),
        "team": team,
        "player_id": player_id,
        "sector": "availability",
        "signal": signal,
        "value": float(value),
        "league_pct": None,
        "sample_n": int(sample_n) if sample_n is not None else None,
        "stability": None,
        "as_of": as_of,
        "inputs_version": inputs_version,
    }


def _build_signals_frame(
    *,
    redistribution_rows: list[dict[str, Any]],
    depth_delta_rows: list[dict[str, Any]],
    cluster_rows: list[dict[str, Any]],
    trend_rows: list[dict[str, Any]],
    season: int,
    week: int,
    as_of: Any,
    inputs_version: str,
) -> pl.DataFrame:
    """Combines every signal-producing helper's output into one signals-shaped frame.
    Constructed with the full _SIGNAL_SCHEMA (name -> dtype), not just column names --
    Polars' row-oriented list-of-dicts constructor otherwise infers each column's dtype
    from only the first `infer_schema_length` rows and raises a ComputeError on append
    the moment a later row doesn't match (e.g. cluster_rows' counts arriving after many
    redistribution/depth rows, or the first non-null `sample_n` arriving after many
    all-null ones from the other three signal types) -- an explicit schema casts every
    row to the declared dtype at construction instead, so a genuine mismatch fails
    loudly right there rather than partway through building the frame."""
    rows: list[dict[str, Any]] = []
    for r in redistribution_rows + depth_delta_rows + trend_rows:
        rows.append(
            _signal_row(
                player_id=r["player_id"],
                team=None,
                signal=r["signal"],
                value=r["value"],
                sample_n=r.get("sample_n"),
                season=season,
                week=week,
                as_of=as_of,
                inputs_version=inputs_version,
            )
        )
    for r in cluster_rows:
        rows.append(
            _signal_row(
                player_id=None,
                team=r["team"],
                signal=r["signal"],
                value=r["value"],
                sample_n=None,
                season=season,
                week=week,
                as_of=as_of,
                inputs_version=inputs_version,
            )
        )

    if rows:
        return pl.DataFrame(rows, schema=_SIGNAL_SCHEMA)
    return pl.DataFrame(schema=_SIGNAL_SCHEMA)


# --------------------------------------------------------------------------------------
# DB I/O (thin -- feeds the pure functions above)
# --------------------------------------------------------------------------------------


def _fetch_current_injuries(
    conn: psycopg.Connection, season: int, week: int
) -> list[dict[str, Any]]:
    """One row per player_id: prefers an ESPN-sourced snapshot over Sleeper when both
    exist for that player this week (ESPN's designation vocabulary is more structured)."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT DISTINCT ON (player_id) player_id, team, designation
            FROM injuries
            WHERE season = %s AND week = %s AND player_id IS NOT NULL
            ORDER BY player_id, (source = 'espn') DESC, as_of DESC
            """,
            (season, week),
        )
        rows = cur.fetchall()
    return [{"player_id": r[0], "team": r[1], "designation": r[2]} for r in rows]


def _fetch_week_injury_sequences(
    conn: psycopg.Connection, season: int, week: int
) -> dict[str, list[str]]:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT player_id, source, designation, as_of
            FROM injuries
            WHERE season = %s AND week = %s AND player_id IS NOT NULL
            ORDER BY player_id, source, as_of ASC
            """,
            (season, week),
        )
        rows = cur.fetchall()
    return _build_trend_sequences(rows)


def _fetch_players_by_team(
    conn: psycopg.Connection, teams: list[str]
) -> dict[str, tuple[str | None, str | None]]:
    if not teams:
        return {}
    with conn.cursor() as cur:
        cur.execute(
            "SELECT player_id, latest_team, position FROM players WHERE latest_team = ANY(%s)",
            (teams,),
        )
        return {r[0]: (r[1], r[2]) for r in cur.fetchall()}


def _fetch_snap_share(conn: psycopg.Connection, season: int, week: int) -> dict[str, float]:
    """Mean offense_pct for current-season weeks strictly before `week`; players with no
    current-season games yet fall back to their last-available prior-season average (no
    reliability-tuned blend -- P3.md scopes this analyst to raw snap shares as an interim
    baseline, not the elaborate blend efficiency.py uses for its own sector)."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT player_id, avg(offense_pct) FROM snaps
            WHERE player_id IS NOT NULL AND season = %s AND week < %s
                AND offense_pct IS NOT NULL
            GROUP BY player_id
            """,
            (season, week),
        )
        current: dict[str, float | None] = dict(cur.fetchall())
        cur.execute(
            """
            SELECT player_id, avg(offense_pct) FROM snaps
            WHERE player_id IS NOT NULL AND season = %s AND offense_pct IS NOT NULL
            GROUP BY player_id
            """,
            (season - 1,),
        )
        prior: dict[str, float | None] = dict(cur.fetchall())
    merged = dict(prior)
    merged.update(current)
    return {pid: float(v) for pid, v in merged.items() if v is not None}


def _fetch_depth(conn: psycopg.Connection) -> list[dict[str, Any]]:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT team, pos_abb, pos_rank, player_id FROM depth WHERE player_id IS NOT NULL"
        )
        rows = cur.fetchall()
    return [{"team": r[0], "pos_abb": r[1], "pos_rank": r[2], "player_id": r[3]} for r in rows]


def _build_inputs_version(conn: psycopg.Connection) -> str:
    return ",".join(
        f"{tag}@{get_last_value(conn, f'nflverse:{tag}') or 'unknown'}"
        for tag in _INPUTS_VERSION_TAGS
    )


class AvailabilityImpactAnalyst(Analyst):
    name = "availability_impact"

    def inputs_ready(self, ctx: RunContext) -> bool | str:
        with ctx.conn.cursor() as cur:
            cur.execute(
                "SELECT count(*) FROM injuries WHERE season = %s AND week = %s",
                (ctx.season, ctx.week),
            )
            row = cur.fetchone()
            count = row[0] if row else 0
        return True if count > 0 else "skipped_no_injuries"

    def compute(self, ctx: RunContext) -> pl.DataFrame:
        conn = ctx.conn
        current_injuries = _fetch_current_injuries(conn, ctx.season, ctx.week)
        flagged = [r for r in current_injuries if r["designation"] not in _HEALTHY_DESIGNATIONS]

        teams_with_flagged = sorted({r["team"] for r in flagged if r["team"]})
        player_positions = _fetch_players_by_team(conn, teams_with_flagged)
        snap_share = _fetch_snap_share(conn, ctx.season, ctx.week)
        depth_rows = _fetch_depth(conn)
        sequences = _fetch_week_injury_sequences(conn, ctx.season, ctx.week)

        roster = _build_redistribution_roster(flagged, player_positions, snap_share)
        redistribution_rows = _compute_redistribution(roster)

        flagged_depth, healthy_depth = _build_depth_groups(flagged, depth_rows)
        depth_delta_rows = _compute_depth_rank_delta(flagged_depth, healthy_depth)

        flagged_positions = _build_flagged_positions(flagged, player_positions)
        cluster_rows = _compute_cluster_counts(flagged_positions)

        flagged_ids = {r["player_id"] for r in flagged}
        trend_rows = _compute_trend_risk(
            {pid: seq for pid, seq in sequences.items() if pid in flagged_ids}
        )

        return _build_signals_frame(
            redistribution_rows=redistribution_rows,
            depth_delta_rows=depth_delta_rows,
            cluster_rows=cluster_rows,
            trend_rows=trend_rows,
            season=ctx.season,
            week=ctx.week,
            as_of=ctx.now,
            inputs_version=_build_inputs_version(conn),
        )

    def write_signals(self, ctx: RunContext, df: pl.DataFrame) -> WorkResult:
        rows = df.to_dicts()
        if not rows:
            return WorkResult(0)
        conflict_cols = [
            "season",
            "week",
            "COALESCE(game_id, '')",
            "COALESCE(team, '')",
            "COALESCE(player_id, '')",
            "sector",
            "signal",
        ]
        identity_cols = ("season", "week", "game_id", "team", "player_id", "sector", "signal")
        update_cols = [c for c in _SIGNAL_COLS if c not in identity_cols]
        rows_written = upsert_rows(
            ctx.conn, "signals", rows, conflict_cols=conflict_cols, update_cols=update_cols
        )
        return WorkResult(rows_written)
