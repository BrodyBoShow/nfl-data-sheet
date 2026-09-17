"""One-off script: show each team's QB-continuity inputs (current starter, prior leader,
prior attempts, share, continuity, qb_factor) by calling the SAME production functions
EfficiencyAnalyst.compute() uses for the real qb_factor calculation -- current-starter
resolution, prior-attempts lookup, and the qb_factor formula itself are all imported from
pipeline.analysts.efficiency, never reimplemented in hand-written SQL. An earlier ad-hoc
SQL reconstruction of this same question had a season-unconstrained join that silently
mixed 2025 and 2026 rows together (misidentifying CLE's and NYG's current starters); this
script exists so that mistake can't happen again -- see CLAUDE.md's note on verification
scripts.

The one exception is "prior leader" (the prior season's most-attempts passer) -- shown
for context only. Production has no such concept any more (the continuity-share formula
compares the current starter's own attempts to the team's total, not to a single "leader"
identity), so there's no production function to call for it; it's computed locally here
and never feeds into qb_factor.

Not part of the pipeline. Read-only.

Usage:
  uv run python scripts/inspect_qb_continuity.py --season 2026 --week 2
  uv run python scripts/inspect_qb_continuity.py --season 2026 --week 2 --team CLE --team NYG
"""

from __future__ import annotations

import argparse
import sys
from datetime import UTC, datetime
from pathlib import Path

import polars as pl

# `pipeline` is only importable with the project root on sys.path -- pytest inserts this
# automatically for tests, but a plain `python scripts/x.py` invocation only puts this
# script's own directory there, so it has to be added explicitly.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pipeline.analysts.efficiency import (  # noqa: E402
    _QB_FULL_CONTINUITY_SHARE,
    _depth_fallback_allowed,
    _fetch_depth,
    _fetch_player_week_qb,
    _filter_current_season,
    _prior_season_qb_attempts,
    _prior_season_team_total_attempts,
    _qb_change_factor,
    _resolve_current_qb,
)
from pipeline.core.base import RunContext  # noqa: E402
from pipeline.core.config import get_settings  # noqa: E402
from pipeline.core.db import get_connection  # noqa: E402


def _prior_leader(prior_pw: pl.DataFrame, team: str) -> tuple[str | None, float]:
    """Context only, not part of qb_factor -- see module docstring."""
    team_prior = prior_pw.filter(pl.col("team") == team)
    if team_prior.height == 0:
        return None, 0.0
    totals = (
        team_prior.group_by("player_id")
        .agg(pl.col("attempts").fill_null(0).sum().alias("attempts"))
        .sort(["attempts", "player_id"], descending=[True, False])
    )
    return totals["player_id"][0], float(totals["attempts"][0])


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--season", type=int, required=True)
    parser.add_argument("--week", type=int, required=True)
    parser.add_argument("--team", action="append", default=None, help="repeatable; default all")
    args = parser.parse_args()

    settings = get_settings()
    with get_connection() as conn:
        ctx = RunContext(
            season=args.season,
            week=args.week,
            season_type="REG",
            now=datetime.now(UTC),
            settings=settings,
            conn=conn,
        )
        prior_season = args.season - 1

        player_week_qb = _fetch_player_week_qb(conn, args.season, prior_season)
        current_pw = _filter_current_season(
            player_week_qb.filter(pl.col("season") == args.season), args.season, args.week
        )
        prior_pw = player_week_qb.filter(
            (pl.col("season") == prior_season) & (pl.col("season_type") == "REG")
        )
        depth_df = _fetch_depth(conn) if _depth_fallback_allowed(ctx) else None

        teams = args.team or sorted(current_pw["team"].drop_nulls().unique().to_list())

        player_ids = set(current_pw["player_id"].drop_nulls().to_list()) | set(
            prior_pw["player_id"].drop_nulls().to_list()
        )
        names: dict[str, str] = {}
        if player_ids:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT player_id, display_name FROM players WHERE player_id = ANY(%s)",
                    (list(player_ids),),
                )
                names = dict(cur.fetchall())

        for team in teams:
            cur_qb = _resolve_current_qb(ctx, current_pw, depth_df, team)
            team_prior_total = _prior_season_team_total_attempts(prior_pw, team)
            cur_qb_prior_attempts = (
                _prior_season_qb_attempts(prior_pw, cur_qb) if cur_qb is not None else 0.0
            )
            qb_factor = _qb_change_factor(cur_qb, cur_qb_prior_attempts, team_prior_total)

            share = (
                min(1.0, cur_qb_prior_attempts / team_prior_total) if team_prior_total > 0 else None
            )
            continuity = min(1.0, share / _QB_FULL_CONTINUITY_SHARE) if share is not None else None

            leader_id, leader_attempts = _prior_leader(prior_pw, team)

            def _label(pid: str | None) -> str:
                if pid is None:
                    return "unknown"
                return f"{pid} ({names.get(pid, '?')})"

            print(f"\n{team}")
            print(f"  current starter:  {_label(cur_qb)}")
            print(f"  prior leader:     {_label(leader_id)}, {leader_attempts:g} attempts")
            print(f"  current starter's prior attempts (any team): {cur_qb_prior_attempts:g}")
            print(f"  team prior total attempts: {team_prior_total:g}")
            print(f"  share: {share if share is None else round(share, 4)}")
            print(f"  continuity: {continuity if continuity is None else round(continuity, 4)}")
            print(f"  qb_factor: {round(qb_factor, 4)}")


if __name__ == "__main__":
    main()
