"""
Job: Show, read-only, where a Rams injured player falls out of Availability impact, by
     calling the analyst's own functions (CLAUDE.md: verification calls production code).
     Then the same with the injury rows' team normalized in memory, to preview D19.
Reads: injuries, injury_presence, players, snaps, depth, games (via the analyst's helpers)
Writes: nothing (the transaction is rolled back)
Tier: OD
Phase: P8 (D19 trace, docs/phases/P8.md)

    uv run python scripts/inspect_rams_availability.py [--season 2026 --week 6]
"""

from __future__ import annotations

import sys
from collections import Counter
from pathlib import Path

# `pipeline` is only importable with the project root on sys.path (see
# scripts/inspect_qb_continuity.py).
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pipeline.analysts.availability_impact as ai  # noqa: E402
from pipeline.core.db import get_connection  # noqa: E402
from pipeline.core.team_aliases import TEAM_ABBR_ALIASES  # noqa: E402


def _signals(flagged, player_positions, snap_share):
    roster = ai._build_redistribution_roster(flagged, player_positions, snap_share)
    redistribution = ai._compute_redistribution(roster)
    clusters = ai._compute_cluster_counts(ai._build_flagged_positions(flagged, player_positions))
    return roster, redistribution, clusters


def main(argv: list[str]) -> None:
    season = int(argv[argv.index("--season") + 1]) if "--season" in argv else 2026
    week = int(argv[argv.index("--week") + 1]) if "--week" in argv else 6
    with get_connection() as conn:
        try:
            current = ai._fetch_current_injuries(conn, season, week, "REG")
            for r in current:
                r["category"] = ai._classify_designation(r["designation"])
            flagged = [r for r in current if r["category"] != ai._CATEGORY_HEALTHY]
            rams = [r for r in flagged if r["team"] in ("LA", "LAR")]
            print(f"season {season} week {week}: {len(flagged)} flagged players; "
                  f"injury-row team codes among Rams: {Counter(r['team'] for r in rams)}")
            print(f"TEAM_ABBR_ALIASES has LAR: {'LAR' in TEAM_ABBR_ALIASES}")

            teams = sorted({r["team"] for r in flagged if r["team"]})
            player_positions = ai._fetch_players_by_team(conn, teams)
            snap_share = ai._fetch_snap_share(conn, season, week)
            print(f"\nteams passed to _fetch_players_by_team: 'LAR' in list: {'LAR' in teams}, "
                  f"'LA' in list: {'LA' in teams}")
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT player_id, latest_team, position FROM players "
                    "WHERE player_id = ANY(%s)",
                    ([r["player_id"] for r in rams],),
                )
                in_players = {p: (t, pos) for p, t, pos in cur.fetchall()}
            print("\nRams flagged players (injury team | players.latest_team | position | "
                  "found by _fetch_players_by_team):")
            for r in sorted(rams, key=lambda r: r["player_id"]):
                lt, pos = in_players.get(r["player_id"], ("<no players row>", None))
                print(f"  {r['player_id']}  injury.team={r['team']}  players.latest_team={lt}  "
                      f"position={pos}  found={r['player_id'] in player_positions}  "
                      f"designation={r['designation']}  driving_source={r['source']}  "
                      f"espn_stale_since={r.get('espn_stale_since')}")

            before = _signals(flagged, player_positions, snap_share)

            # In-memory preview of D19: normalize the injury rows' team. No write.
            aliases = {**TEAM_ABBR_ALIASES, "LAR": "LA"}
            norm = [{**r, "team": aliases.get(r["team"], r["team"])} for r in flagged]
            teams_n = sorted({r["team"] for r in norm if r["team"]})
            pp_n = ai._fetch_players_by_team(conn, teams_n)
            after = _signals(norm, pp_n, snap_share)

            def rams_view(sig):
                roster, redistribution, clusters = sig
                rams_ids = {p for p, (t, _) in {**player_positions, **pp_n}.items() if t == "LA"}
                rams_ids |= {r["player_id"] for r in rams}
                return (
                    sum(1 for x in roster if x["team"] in ("LA", "LAR")),
                    sum(1 for x in redistribution if x["player_id"] in rams_ids),
                    [c for c in clusters if c["team"] in ("LA", "LAR")],
                )

            print("\nRams: (roster rows, redistribution signals, cluster rows)")
            print(f"  as stored (LAR):        {rams_view(before)}")
            print(f"  normalized in memory:   {rams_view(after)}")

            def others(sig):
                _, redistribution, clusters = sig
                rams_ids = {r["player_id"] for r in rams} | {
                    p for p, (t, _) in pp_n.items() if t == "LA"}
                return (
                    sorted((x["player_id"], x["signal"], round(x["value"], 9))
                           for x in redistribution if x["player_id"] not in rams_ids),
                    sorted((c["team"], c["signal"], c["value"])
                           for c in clusters if c["team"] not in ("LA", "LAR")),
                )

            print(f"\nevery other team's redistribution and cluster output identical: "
                  f"{others(before) == others(after)}")
        finally:
            conn.rollback()


if __name__ == "__main__":
    main(sys.argv[1:])
