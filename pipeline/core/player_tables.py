"""
Job: Shared window, blend, percentile, hashing and stale-row helpers for the two wide
     player tables (player_usage_week, player_eff_week). No I/O beyond the one scoped
     delete helper; each analyst owns its own reads and writes.
Reads: nothing itself
Writes: nothing itself (delete_stale_player_rows runs the caller's scoped delete)
Tier: n/a
Phase: P7
"""

from __future__ import annotations

import math
from collections.abc import Iterable
from typing import Any

import polars as pl
import psycopg

from .db import delete_rows, filter_changed, upsert_rows
from .hashing import hash_row

_ROUND_DIGITS = 6

# The row-shape columns every player-table row carries besides its metrics (0031/0032).
KEY_COLS = ["player_id", "season", "week"]
IDENTITY_COLS = ["season_type", "game_id", "team", "position_group"]
# Written, but not hashed: a run whose only change is when it ran, or which nflverse
# timestamps it read, rewrites nothing (filter_changed compares content_hash).
UNHASHED_COLS = frozenset({"as_of", "inputs_version", "content_hash", "updated_at"})


# --------------------------------------------------------------------------------------
# Games played and windows
# --------------------------------------------------------------------------------------


def check_one_game_per_week(df: pl.DataFrame, source: str) -> None:
    """Every window keys a player's games by week. Two rows for one player in one week
    (a second game, or two provider IDs crosswalked to one gsis ID) would silently
    double that week's values, so stop instead."""
    dupes = df.group_by("player_id", "week").len().filter(pl.col("len") > 1).height
    if dupes:
        raise ValueError(f"{source}: {dupes} player-week(s) with more than one row")


def played_weeks(snaps_weeks: pl.DataFrame, source_weeks: pl.DataFrame) -> pl.DataFrame:
    """(player_id, week) for every game the player played: a `snaps` row, plus the rare
    player_game_pbp row with no snaps row (8 player-games in 2025-2026, measured
    2026-09-29 -- a crosswalk gap), so that game still counts as played."""
    return (
        pl.concat(
            [snaps_weeks.select("player_id", "week"), source_weeks.select("player_id", "week")],
            how="vertical",
        )
        .unique()
        .sort("player_id", "week")
    )


def window_frame(played: pl.DataFrame) -> pl.DataFrame:
    """Per played (player_id, week): games played to date, and the first week of the
    last-4-games window (this game included). _l4 is the player's last 4 games
    *played*, not his last 4 games with a nonzero value of some metric."""
    return (
        played.sort("player_id", "week")
        .with_columns(
            pl.int_range(1, pl.len() + 1).over("player_id").alias("games_std"),
            pl.col("week").shift(3).over("player_id").alias("l4_from_week"),
        )
        .with_columns(
            pl.col("l4_from_week").fill_null(pl.col("week").min().over("player_id")),
            pl.min_horizontal(pl.col("games_std"), pl.lit(4)).alias("games_l4"),
        )
    )


def windowed_sums(values: pl.DataFrame, windows: pl.DataFrame, cols: Iterable[str]) -> pl.DataFrame:
    """Sum each column of `values` (keyed player_id, week; nulls count as nothing) over
    three windows for every row of `windows` (player_id, week, l4_from_week):
    `<c>_game` = that week, `<c>_l4` = weeks [l4_from_week, week], `<c>_std` = weeks <=
    week. Only weeks <= the row's own week are ever read: no later game can leak into an
    earlier row. Sums are left as-is (0 where nothing was summed); callers decide what a
    zero denominator means."""
    cols = list(cols)
    v = (
        values.select("player_id", "week", *cols)
        .group_by("player_id", "week")
        .agg(pl.col(c).fill_null(0).sum() for c in cols)
        .sort("player_id", "week")
    )
    cum = v.with_columns(pl.col(c).cum_sum().over("player_id").alias(f"{c}__cum") for c in cols)
    keys = ["player_id", "week"]
    at = _asof_cum(windows.select(*keys, pl.col("week").alias("_upto")), cum, cols)
    before_l4 = _asof_cum(
        windows.select(*keys, (pl.col("l4_from_week") - 1).alias("_upto")), cum, cols
    )
    # Every piece is joined on (player_id, week), never lined up by row position.
    out = (
        windows.select(keys)
        .join(at.rename({f"{c}__cum": f"{c}_std" for c in cols}), on=keys, how="left")
        .join(before_l4.rename({f"{c}__cum": f"{c}__before" for c in cols}), on=keys, how="left")
        .join(v.rename({c: f"{c}_game" for c in cols}), on=keys, how="left")
    )
    return out.with_columns(
        [(pl.col(f"{c}_std") - pl.col(f"{c}__before")).alias(f"{c}_l4") for c in cols]
        + [pl.col(f"{c}_game").fill_null(0) for c in cols]
    ).drop(f"{c}__before" for c in cols)


def _asof_cum(left: pl.DataFrame, cum: pl.DataFrame, cols: list[str]) -> pl.DataFrame:
    """(player_id, week, <c>__cum...): each player's cumulative sums as of `left._upto`
    (backward: his last value row with week <= _upto), 0 where he has none that early."""
    right = cum.select(
        "player_id", pl.col("week").alias("_vweek"), *[f"{c}__cum" for c in cols]
    ).sort("_vweek")
    joined = left.sort("_upto").join_asof(
        right,
        left_on="_upto",
        right_on="_vweek",
        by="player_id",
        strategy="backward",
        check_sortedness=False,  # both sides are sorted on the join key just above
    )
    return joined.select("player_id", "week", *[pl.col(f"{c}__cum").fill_null(0) for c in cols])


def ratio(num: pl.Expr, den: pl.Expr) -> pl.Expr:
    """num / den, null when den is 0 or null: a window with no sample is null, never 0."""
    return pl.when(den.is_not_null() & (den != 0)).then(num / den).otherwise(None)


# --------------------------------------------------------------------------------------
# Prior blend (Efficiency's three-way form; docs/signals.md "Prior blend")
# --------------------------------------------------------------------------------------


def blend_exprs(
    *, cur: str, n: str, prior: str, league: str, k: pl.Expr | float, r: pl.Expr | float
) -> tuple[pl.Expr, pl.Expr, pl.Expr]:
    """Vectorized Efficiency `_blend` (pipeline/analysts/efficiency.py): w_cur =
    n/(n+k), w_prior = (1-w_cur)*r where a prior exists (else 0), value = w_cur*cur +
    w_prior*prior + w_league*league. `k`/`r` may be per-row expressions (they vary by
    position group). Null value when n is 0 -- no current sample, no value
    (docs/signals.md, "Nulls"). A test holds this equal to `_blend` itself."""
    k_e = k if isinstance(k, pl.Expr) else pl.lit(float(k))
    r_e = r if isinstance(r, pl.Expr) else pl.lit(float(r))
    w_cur = pl.col(n) / (pl.col(n) + k_e)
    w_prior = pl.when(pl.col(prior).is_not_null()).then((1.0 - w_cur) * r_e).otherwise(0.0)
    value = (
        w_cur * pl.col(cur)
        + w_prior * pl.col(prior).fill_null(0.0)
        + (1.0 - w_cur - w_prior) * pl.col(league)
    )
    has_sample = pl.col(n).is_not_null() & (pl.col(n) > 0) & pl.col(cur).is_not_null()
    return (
        pl.when(has_sample).then(value).otherwise(None),
        pl.when(has_sample).then(w_cur).otherwise(None),
        pl.when(has_sample).then(w_prior).otherwise(None),
    )


def shrunk_prior(num: float | None, den: float | None, league: float | None, k: float) -> Any:
    """Last season's ratio shrunk toward last season's group league value with the same
    k (docs/signals.md: the prior is shrunk, matching Efficiency's k-shrunk plain solve).
    None without a prior-season sample."""
    if den is None or den <= 0 or num is None or league is None:
        return None
    return (num + k * league) / (den + k)


# --------------------------------------------------------------------------------------
# League percentile (docs/signals.md, "_pct")
# --------------------------------------------------------------------------------------


def as_of_percentiles(rows: pl.DataFrame, specs: list[tuple[str, str, str]]) -> pl.DataFrame:
    """Add one percentile column per (value, eligible, out) spec. For each row at week
    W, `out` is the rank of `value` among the latest rows as of W of every player in the
    same position_group whose latest row is `eligible` with a non-null `value`:
    round(100 * (below + 0.5 * equal) / population). A player who didn't play in W is
    still in W's population through his latest earlier row. Ineligible rows, and rows
    without a position_group, get null."""
    if not specs:
        return rows
    cols = sorted({c for v, e, _ in specs for c in (v, e)})
    base = rows.select("player_id", "week", "position_group", *cols)
    parts = []
    for w in sorted(rows["week"].unique().to_list()):
        latest = (
            base.filter(pl.col("week") <= w)
            .sort("week")
            .group_by("player_id")
            .last()
            .filter(pl.col("position_group").is_not_null())
        )
        ranked = latest.with_columns(
            _pct_expr(value, eligible).alias(out) for value, eligible, out in specs
        ).filter(pl.col("week") == w)
        parts.append(ranked.select("player_id", "week", *[o for _, _, o in specs]))
    pct = pl.concat(parts)
    return rows.join(pct, on=["player_id", "week"], how="left")


def _pct_expr(value: str, eligible: str) -> pl.Expr:
    in_pop = pl.col(eligible).fill_null(False) & pl.col(value).is_not_null()
    v = pl.when(in_pop).then(pl.col(value)).otherwise(None)
    return (
        (
            100.0
            * (v.rank("average").over("position_group") - 0.5)
            / in_pop.sum().over("position_group")
        )
        .round(0)
        .cast(pl.Int16)
    )


# --------------------------------------------------------------------------------------
# Rows out: rounding, hashing, scoped stale delete
# --------------------------------------------------------------------------------------


def finalize_rows(
    df: pl.DataFrame, columns: list[str], extra: dict[str, Any]
) -> list[dict[str, Any]]:
    """Order columns as the migration does, round floats (group_by sums run in no fixed
    order, and an unrounded last digit would churn content_hash), and hash everything
    but UNHASHED_COLS."""
    df = df.with_columns(
        pl.col(c).round(_ROUND_DIGITS)
        for c, t in df.schema.items()
        if t in (pl.Float32, pl.Float64)
    )
    rows = []
    for r in df.to_dicts():
        row = {c: _clean(r.get(c)) for c in columns if c not in UNHASHED_COLS}
        row["content_hash"] = hash_row(row)
        row.update(extra)
        rows.append(row)
    return rows


def _clean(v: Any) -> Any:
    if isinstance(v, float) and (math.isnan(v) or math.isinf(v)):
        return None
    return v


def delete_stale_player_rows(
    conn: psycopg.Connection,
    table: str,
    season: int,
    through_week: int,
    keep: Iterable[tuple[str, int]],
) -> int:
    """Delete this table's rows for `season`, weeks <= `through_week`, whose (player_id,
    week) this run didn't produce. Never touches another season or a later week: a
    run's scope is exactly what it just recomputed (CLAUDE.md, "delete only within your
    own current scope and rewrite it in the same run"). `table` is an internal constant."""
    keep = list(keep)
    return delete_rows(
        conn,
        table,
        "season = %s AND week <= %s AND NOT EXISTS ("
        "SELECT 1 FROM unnest(%s::text[], %s::int[]) AS k(pid, wk) "
        "WHERE k.pid = player_id AND k.wk = week)",
        (season, through_week, [p for p, _ in keep], [w for _, w in keep]),
    )


def write_player_rows(
    conn: psycopg.Connection,
    table: str,
    season: int,
    through_week: int,
    rows: list[dict[str, Any]],
) -> tuple[int, int]:
    """The scoped stale delete, then a hash-diffed upsert, in the caller's transaction.
    Returns (deleted, written)."""
    deleted = delete_stale_player_rows(
        conn, table, season, through_week, ((r["player_id"], r["week"]) for r in rows)
    )
    changed = filter_changed(conn, table, KEY_COLS, rows)
    written = upsert_rows(
        conn,
        table,
        changed,
        conflict_cols=KEY_COLS,
        update_cols=[col for col in (changed[0] if changed else {}) if col not in KEY_COLS],
    )
    return deleted, written


def column_list(migration_sql: str) -> list[str]:
    """The column names a CREATE TABLE statement declares, in order: the migration is
    the column contract, and each analyst's tests compare its output to it."""
    body = migration_sql.split("CREATE TABLE", 1)[1].split("(", 1)[1]
    cols: list[str] = []
    for raw in body.splitlines():
        line = raw.split("--", 1)[0].strip()
        if line.startswith(")"):
            break
        if not line or line.upper().startswith(("PRIMARY KEY", "CHECK")):
            continue
        for part in line.split(","):
            name = part.strip().split(" ", 1)[0]
            if name and name.isidentifier():
                cols.append(name)
    return cols
