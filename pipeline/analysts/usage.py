"""
Job: Compute each player's usage shares (snap, target, air-yards, carry, dropback,
     red-zone, end-zone, goal-line) per game, over his last 4 games and season to date,
     with week-over-week deltas and league percentiles.
Reads: snaps, player_game_pbp, players (position_group)
Writes: player_usage_week
Tier: T2
Phase: P7
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import polars as pl
import psycopg

from pipeline.core.base import Analyst, RunContext, WorkResult
from pipeline.core.freshness import get_last_value
from pipeline.core.player_tables import (
    as_of_percentiles,
    check_one_game_per_week,
    column_list,
    finalize_rows,
    ratio,
    window_frame,
    windowed_sums,
    write_player_rows,
)

TABLE = "player_usage_week"
_MIGRATION = (
    Path(__file__).resolve().parents[2] / "db" / "migrations" / "0031_player_usage_week.sql"
)
COLUMNS = column_list(_MIGRATION.read_text(encoding="utf-8"))

# usage_stability = games / (games + K_USAGE). The largest median k0 in games across the
# usage shares, rounded (RB target share, 2.13 over 25 seasons), so stability never
# overstates how settled a share is (docs/signals.md, "Family stability"; 2026-09-29).
K_USAGE = 2.0

# Team snaps per side are recovered from PFR's own percentages: the median of
# snaps / pct over the team's players at >= this pct on that side (docs/signals.md,
# Usage family). No stored column holds team snaps, and nobody is at 100% in many games.
_TEAM_SNAP_MIN_PCT = 0.50
# PFR pct is a 0-1 fraction (verified live 2026-09-29: a 75-snap starter reads 1.0).
# A value past this means the scale changed under us; stop rather than divide by it.
_PCT_SCALE_MAX = 1.0 + 1e-6

_SIDES = {
    "off": ("offense_snaps", "offense_pct"),
    "def": ("defense_snaps", "defense_pct"),
    "st": ("st_snaps", "st_pct"),
}
_PGP_COUNTS = [
    "targets",
    "rec_air_yards_sum",
    "carries",
    "dropbacks",
    "rz_targets",
    "ez_targets",
    "rz_carries",
    "gl_carries",
]

# metric -> (player numerator column, denominator column). Denominators named team_* are
# the team's totals in that game; targets_per_off_snap divides by the player's own snaps.
METRICS: dict[str, tuple[str, str]] = {
    "off_snap_share": ("offense_snaps", "team_off_snaps"),
    "def_snap_share": ("defense_snaps", "team_def_snaps"),
    "st_snap_share": ("st_snaps", "team_st_snaps"),
    "target_share": ("targets", "team_targets"),
    "air_yards_share": ("rec_air_yards_sum", "team_rec_air_yards_sum"),
    "carry_share": ("carries", "team_carries"),
    "dropback_share": ("dropbacks", "team_dropbacks"),
    "rz_target_share": ("rz_targets", "team_rz_targets"),
    "ez_target_share": ("ez_targets", "team_ez_targets"),
    "rz_carry_share": ("rz_carries", "team_rz_carries"),
    "gl_carry_share": ("gl_carries", "team_gl_carries"),
    "targets_per_off_snap": ("targets", "offense_snaps"),
}

_SNAPS_SCHEMA: dict[str, Any] = {
    "player_id": pl.Utf8,
    "game_id": pl.Utf8,
    "season": pl.Int64,
    "week": pl.Int64,
    "season_type": pl.Utf8,
    "team": pl.Utf8,
    "offense_snaps": pl.Int64,
    "offense_pct": pl.Float64,
    "defense_snaps": pl.Int64,
    "defense_pct": pl.Float64,
    "st_snaps": pl.Int64,
    "st_pct": pl.Float64,
}
_PGP_SCHEMA: dict[str, Any] = {
    "player_id": pl.Utf8,
    "game_id": pl.Utf8,
    "week": pl.Int64,
    "team": pl.Utf8,
    **{c: (pl.Float64 if c.endswith("_sum") else pl.Int64) for c in _PGP_COUNTS},
}
_INPUTS_VERSION_KEYS = ("nflverse:snap_counts", "nflverse:player_game_pbp")


# --------------------------------------------------------------------------------------
# Pure computation
# --------------------------------------------------------------------------------------


def check_pct_scale(snaps: pl.DataFrame) -> None:
    """PFR pct must be a 0-1 fraction. On a 0-100 scale, snaps/pct would recover team
    snaps 100x too small, and every snap share would read 100x too large."""
    worst = snaps.select(
        pl.max_horizontal(pl.col("offense_pct"), pl.col("defense_pct"), pl.col("st_pct")).max()
    ).item()
    if worst is not None and worst > _PCT_SCALE_MAX:
        raise ValueError(f"snaps pct above 1 ({worst}): PFR pct is no longer a 0-1 fraction")


def team_snaps(snaps: pl.DataFrame) -> pl.DataFrame:
    """(game_id, team, team_off_snaps, team_def_snaps, team_st_snaps): per side, the
    rounded median of snaps / pct over the team's players at >= 50% on that side. Null
    when nobody reached 50% on that side in that game."""
    out = snaps.select("game_id", "team").unique()
    for side, (snap_col, pct_col) in _SIDES.items():
        rec = (
            snaps.filter(pl.col(pct_col) >= _TEAM_SNAP_MIN_PCT)
            .group_by("game_id", "team")
            .agg((pl.col(snap_col) / pl.col(pct_col)).median().round(0).alias(f"team_{side}_snaps"))
        )
        out = out.join(rec, on=["game_id", "team"], how="left")
    return out


def build_usage_rows(
    snaps: pl.DataFrame, pgp: pl.DataFrame, positions: pl.DataFrame
) -> pl.DataFrame:
    """Every player_usage_week row for one season, for every week in `snaps` (the caller
    passes weeks <= the run's week). One row per player per game he took a snap in.

    A player with a snaps row but no player_game_pbp row had no target, carry or
    dropback in that game: pbp credits every in-scope play, so his usage numerators are a
    sourced 0, not a gap. Team totals are summed over every player_game_pbp row of that
    team-game, so plays with no attributed player (a throwaway) are in no total.
    """
    played = snaps.filter(
        (pl.col("offense_snaps") + pl.col("defense_snaps") + pl.col("st_snaps")) > 0
    )
    check_pct_scale(played)
    check_one_game_per_week(played, "snaps")

    team_tot = pgp.group_by("game_id", "team").agg(
        pl.col(c).fill_null(0).sum().alias(f"team_{c}") for c in _PGP_COUNTS
    )
    games = (
        played.join(pgp.drop("week", "team"), on=["player_id", "game_id"], how="left")
        .with_columns(pl.col(c).fill_null(0) for c in _PGP_COUNTS)
        .join(team_tot, on=["game_id", "team"], how="left")
        .join(team_snaps(played), on=["game_id", "team"], how="left")
    )

    # A game whose denominator is 0 or unknown contributes to neither side of a window.
    value_cols: list[str] = []
    for m, (num, den) in METRICS.items():
        valid = pl.col(den).is_not_null() & (pl.col(den) != 0)
        games = games.with_columns(
            pl.when(valid).then(pl.col(num).cast(pl.Float64)).otherwise(None).alias(f"{m}__n"),
            pl.when(valid).then(pl.col(den).cast(pl.Float64)).otherwise(None).alias(f"{m}__d"),
        )
        value_cols += [f"{m}__n", f"{m}__d"]

    windows = window_frame(games.select("player_id", "week"))
    sums = windowed_sums(games, windows, value_cols)
    rows = (
        games.select("player_id", "season", "week", "season_type", "game_id", "team")
        .join(windows, on=["player_id", "week"])
        .join(sums, on=["player_id", "week"])
        .join(positions, on="player_id", how="left")
        .sort("player_id", "week")
    )
    for m in METRICS:
        rows = rows.with_columns(
            ratio(pl.col(f"{m}__n_{w}"), pl.col(f"{m}__d_{w}")).alias(f"{m}_{w}")
            for w in ("std", "game", "l4")
        ).with_columns(
            (pl.col(f"{m}_game") - pl.col(f"{m}_game").shift(1).over("player_id")).alias(f"{m}_wow")
        )
    # Usage's population is every player with a non-null _std (no volume minimum).
    rows = as_of_percentiles(
        rows.with_columns(pl.lit(True).alias("_all")),
        [(f"{m}_std", "_all", f"{m}_pct") for m in METRICS],
    ).drop("_all")
    return rows.with_columns(
        pl.col("games_std").cast(pl.Int16).alias("usage_games_std"),
        pl.col("games_l4").cast(pl.Int16).alias("usage_games_l4"),
        (pl.col("games_std") / (pl.col("games_std") + K_USAGE)).alias("usage_stability"),
    ).sort("player_id", "week")


# --------------------------------------------------------------------------------------
# I/O
# --------------------------------------------------------------------------------------


def _frame(rows: list[tuple[Any, ...]], schema: dict[str, Any]) -> pl.DataFrame:
    return pl.DataFrame(rows, schema=schema, orient="row") if rows else pl.DataFrame(schema=schema)


def _fetch(conn: psycopg.Connection, season: int, through_week: int) -> tuple[pl.DataFrame, ...]:
    with conn.cursor() as cur:
        cur.execute(
            f"SELECT {', '.join(_SNAPS_SCHEMA)} FROM snaps WHERE season = %s AND week <= %s "
            "AND player_id IS NOT NULL AND season_type IN ('REG', 'POST')",
            (season, through_week),
        )
        snaps = _frame(cur.fetchall(), _SNAPS_SCHEMA)
        cur.execute(
            f"SELECT {', '.join(_PGP_SCHEMA)} FROM player_game_pbp "
            "WHERE season = %s AND week <= %s",
            (season, through_week),
        )
        pgp = _frame(cur.fetchall(), _PGP_SCHEMA)
        cur.execute(
            "SELECT player_id, position_group FROM players WHERE player_id = ANY(%s)",
            (snaps["player_id"].unique().to_list(),),
        )
        positions = _frame(cur.fetchall(), {"player_id": pl.Utf8, "position_group": pl.Utf8})
    return snaps, pgp, positions


def _inputs_version(conn: psycopg.Connection) -> str:
    return ",".join(
        f"{k.split(':', 1)[1]}@{get_last_value(conn, k) or 'unknown'}" for k in _INPUTS_VERSION_KEYS
    )


class UsageAnalyst(Analyst):
    name = "usage"
    # Writes player_usage_week, not signals (CLAUDE.md layer rules), so it claims no
    # signal names and signals cleanup doesn't apply; see _delete_stale_signals.
    sector = "usage"
    signal_names: frozenset[str] = frozenset()

    _rows: list[dict[str, Any]]
    _meta: dict[str, Any]

    def inputs_ready(self, ctx: RunContext) -> bool | str:
        with ctx.conn.cursor() as cur:
            cur.execute(
                "SELECT count(*) FROM snaps WHERE season = %s AND week <= %s",
                (ctx.season, ctx.week),
            )
            row = cur.fetchone()
        return bool(row and row[0])

    def compute(self, ctx: RunContext) -> pl.DataFrame:
        snaps, pgp, positions = _fetch(ctx.conn, ctx.season, ctx.week)
        df = build_usage_rows(snaps, pgp, positions)
        # players is the FK target; a snaps player missing from it can't be written.
        known = pl.col("player_id").is_in(positions["player_id"].implode())
        unknown = df.filter(~known).height
        df = df.filter(known)
        self._rows = finalize_rows(
            df,
            COLUMNS,
            {"as_of": ctx.now, "inputs_version": _inputs_version(ctx.conn), "updated_at": ctx.now},
        )
        self._meta = {
            "weeks": sorted(df["week"].unique().to_list()),
            "rows": len(self._rows),
            "skipped_not_in_players": unknown,
        }
        return df

    def _delete_stale_signals(self, ctx: RunContext) -> int:
        """No signals rows to clean: this analyst's stale rows live in its own table and
        are removed in write_signals (docs/signals.md, "Which weeks a run writes")."""
        return 0

    def write_signals(self, ctx: RunContext, df: pl.DataFrame) -> WorkResult:
        deleted, upserted = write_player_rows(ctx.conn, TABLE, ctx.season, ctx.week, self._rows)
        return WorkResult(
            upserted.rows_changed,
            {**self._meta, "stale_rows_deleted": deleted, "upserts": {TABLE: upserted.meta()}},
        )
