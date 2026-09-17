"""One-off script: diagnose two suspicious 2026 week-1 team_week.plays counts (CLE=13,
NO=87) by breaking raw pbp down through the exact filter stages `_aggregate_team_week`
(pipeline/collectors/nflverse_bulk.py) applies, and checking two specific hypotheses:
overtime inflating a team's play count, and more than one game_id getting scoped into
the same team/week (a fan-out bug rather than a filter-threshold problem).

Not part of the pipeline -- pbp is never stored in Postgres (aggregates only), so this
fetches it live and prints a report; it changes nothing. Read-only against the database
too (an optional cross-check against the currently-stored team_week row).

Usage:
  uv run python scripts/inspect_play_filter.py
  uv run python scripts/inspect_play_filter.py --season 2026 --week 1 --team CLE --team NO
  uv run python scripts/inspect_play_filter.py --no-db-check
"""

from __future__ import annotations

import argparse
from collections import Counter

import nflreadpy as nfl
import polars as pl

_GARBAGE_TIME_WP_LOW = 0.05
_GARBAGE_TIME_WP_HIGH = 0.95


def _garbage_mask() -> pl.Expr:
    return (pl.col("wp") < _GARBAGE_TIME_WP_LOW) | (pl.col("wp") > _GARBAGE_TIME_WP_HIGH)


def _db_check(team: str, game_id: str) -> None:
    try:
        from pipeline.core.db import get_connection
    except Exception as exc:  # pragma: no cover - diagnostic convenience only
        print(f"    (skipping DB cross-check: {exc})")
        return
    try:
        with get_connection() as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT plays, garbage_time_plays_excluded FROM team_week "
                "WHERE game_id = %s AND team = %s",
                (game_id, team),
            )
            row = cur.fetchone()
    except Exception as exc:  # pragma: no cover - diagnostic convenience only
        print(f"    (skipping DB cross-check: {exc})")
        return
    if row is None:
        print(f"    stored team_week row: none found for game_id={game_id} team={team}")
    else:
        print(f"    stored team_week row: plays={row[0]}, garbage_time_plays_excluded={row[1]}")


def _inspect_team_week(
    pbp: pl.DataFrame, team: str, season: int, week: int, db_check: bool
) -> None:
    team_week_rows = pbp.filter(
        (pl.col("posteam") == team) & (pl.col("season") == season) & (pl.col("week") == week)
    )
    game_ids = sorted(team_week_rows["game_id"].unique().to_list())
    print(f"\n=== {team} {season} week {week} ===")
    print(f"distinct game_id(s) for this team/week in raw pbp: {game_ids}")
    if len(game_ids) != 1:
        print(f"  ** SCOPING FLAG: expected exactly 1 game_id, found {len(game_ids)} **")

    for game_id in game_ids:
        game_rows = pbp.filter(pl.col("game_id") == game_id)
        team_rows = game_rows.filter(pl.col("posteam") == team)

        not_deleted = team_rows.filter(pl.col("play_deleted") != 1)
        scrimmage = not_deleted.filter(
            pl.col("epa").is_not_null() & ((pl.col("pass") == 1) | (pl.col("rush") == 1))
        )
        garbage = scrimmage.filter(_garbage_mask())
        clean = scrimmage.filter(~_garbage_mask())
        excluded = not_deleted.filter(
            ~(pl.col("epa").is_not_null() & ((pl.col("pass") == 1) | (pl.col("rush") == 1)))
        )

        included_play_types = Counter(scrimmage["play_type"].fill_null("<null>").to_list())
        excluded_play_types = Counter(excluded["play_type"].fill_null("<null>").to_list())
        ot_in_final = clean.filter(
            (pl.col("qtr") >= 5) | (pl.col("game_half") == "Overtime")
        ).height

        print(f"\n  game_id={game_id}")
        print(f"    total pbp rows for this game (both teams, every play type): {game_rows.height}")
        print(f"    this team's rows (posteam == {team}, every play type): {team_rows.height}")
        print(f"    play_deleted==1 dropped: {team_rows.height - not_deleted.height}")
        print(f"    pass-or-rush w/ non-null epa (the 'plays' filter, pre-garbage-time): "
              f"{scrimmage.height}")
        print(f"      garbage-time excluded (wp<{_GARBAGE_TIME_WP_LOW} or "
              f">{_GARBAGE_TIME_WP_HIGH}): {garbage.height}")
        print(f"      final 'plays' count (should match team_week.plays): {clean.height}")
        print(f"      of which occurred in overtime (qtr>=5 or game_half=='Overtime'): "
              f"{ot_in_final}")
        print(f"    play_type breakdown of the INCLUDED set (should be only pass/run): "
              f"{dict(included_play_types)}")
        print(f"    play_type breakdown of the EXCLUDED set (special teams, no_play, "
              f"qb_kneel, qb_spike, etc.): {dict(excluded_play_types)}")

        if db_check:
            _db_check(team, game_id)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--season", type=int, default=2026)
    parser.add_argument("--week", type=int, default=1)
    parser.add_argument("--team", action="append", default=None, help="repeatable")
    parser.add_argument("--no-db-check", action="store_true")
    args = parser.parse_args()
    teams = args.team or ["CLE", "NO"]

    pbp = nfl.load_pbp(seasons=[args.season])
    for team in teams:
        _inspect_team_week(pbp, team, args.season, args.week, db_check=not args.no_db_check)


if __name__ == "__main__":
    main()
