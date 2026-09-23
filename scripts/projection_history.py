"""Shared helpers for the offline P5 model scripts (scripts/fit_projection_model.py and
scripts/backtest.py). Not a scheduled job and not a CLI.

- Loads historical REG games (identity plus final scores, nflverse closing lines, and
  location) and historical efficiency signals.
- Attaches outcomes and closing-line edges to walk-forward predictions.
- Computes the per-stability-bucket calibration that goes into
  pipeline/synthesis/model_coefficients.json.

Historical scores and lines are read here and only here, never by a scheduled L3 job
(CLAUDE.md layer rules). Model math lives in pipeline/synthesis/model.py. Nothing here
refits or re-derives it.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

import numpy as np
import polars as pl
import psycopg

from pipeline.synthesis.model import STABILITY_BUCKETS, stability_cutpoints, with_stability_bucket

BOOTSTRAP_N = 2000
BOOTSTRAP_SEED = 20260923

_GAMES_SCHEMA: dict[str, Any] = {
    "game_id": pl.Utf8,
    "season": pl.Int64,
    "week": pl.Int64,
    "home_team": pl.Utf8,
    "away_team": pl.Utf8,
    "location": pl.Utf8,
    "home_score": pl.Int64,
    "away_score": pl.Int64,
    "spread_line": pl.Float64,
    "total_line": pl.Float64,
}
_SIGNALS_SCHEMA: dict[str, Any] = {
    "season": pl.Int64,
    "week": pl.Int64,
    "team": pl.Utf8,
    "signal": pl.Utf8,
    "value": pl.Float64,
    "stability": pl.Float64,
}


def _frame(rows: list[tuple[Any, ...]], schema: dict[str, Any]) -> pl.DataFrame:
    if not rows:
        return pl.DataFrame(schema=schema)
    return pl.DataFrame(rows, schema=schema, orient="row")


def load_games(conn: psycopg.Connection, seasons: Sequence[int]) -> pl.DataFrame:
    """REG games for `seasons`. `spread_line` is home-positive (docs/sources.md) and is
    returned raw; `add_outcomes` converts it to the market's home-negative convention."""
    with conn.cursor() as cur:
        cur.execute(
            f"SELECT {', '.join(_GAMES_SCHEMA)} FROM games "
            "WHERE season_type = 'REG' AND season = ANY(%s) ORDER BY season, week, game_id",
            (list(seasons),),
        )
        rows = cur.fetchall()
    return _frame(rows, _GAMES_SCHEMA)


def load_efficiency_signals(
    conn: psycopg.Connection, seasons: Sequence[int], signal_names: Sequence[str]
) -> pl.DataFrame:
    with conn.cursor() as cur:
        cur.execute(
            f"SELECT {', '.join(_SIGNALS_SCHEMA)} FROM signals "
            "WHERE sector = 'efficiency' AND game_id IS NULL AND player_id IS NULL "
            "AND season = ANY(%s) AND signal = ANY(%s)",
            (list(seasons), list(signal_names)),
        )
        rows = cur.fetchall()
    return _frame(rows, _SIGNALS_SCHEMA)


def check_fit_seasons(
    seasons: Sequence[int], current_season: int, games: pl.DataFrame
) -> None:
    """Refuse to fit on the current season or later, or on any season with an unscored
    REG game (an incomplete season). Raises ValueError."""
    too_new = [s for s in seasons if s >= current_season]
    if too_new:
        raise ValueError(
            f"refusing: seasons {too_new} are >= the current season ({current_season})"
        )
    missing = [s for s in seasons if games.filter(pl.col("season") == s).height == 0]
    if missing:
        raise ValueError(f"refusing: no REG games stored for {missing}")
    unscored = games.filter(
        pl.col("season").is_in(list(seasons))
        & (pl.col("home_score").is_null() | pl.col("away_score").is_null())
    )
    if unscored.height:
        by_season = unscored.group_by("season").len().sort("season").rows()
        raise ValueError(f"refusing: unscored REG games (season, count): {by_season}")


def add_outcomes(pred: pl.DataFrame) -> pl.DataFrame:
    """Adds actual results, the closing line in market convention, edges and residuals.

    - `closing_spread_home = -spread_line`: nflverse's spread_line is home-positive,
      the market convention (and `projected_spread_home`) is home-negative.
    - `edge_spread = projected_spread_home - closing_spread_home`: negative means the
      model rates the home team higher than the close.
    - `ats_margin_home = margin_actual + closing_spread_home`: positive = home covered.
    - `ou_margin = total_actual - closing_total`: positive = over.
    """
    return pred.with_columns(
        (pl.col("home_score") - pl.col("away_score")).cast(pl.Float64).alias("margin_actual"),
        (pl.col("home_score") + pl.col("away_score")).cast(pl.Float64).alias("total_actual"),
        (-pl.col("spread_line")).alias("closing_spread_home"),
        pl.col("total_line").alias("closing_total"),
    ).with_columns(
        (pl.col("projected_spread_home") - pl.col("closing_spread_home")).alias("edge_spread"),
        (pl.col("projected_total") - pl.col("closing_total")).alias("edge_total"),
        (pl.col("margin_actual") + pl.col("closing_spread_home")).alias("ats_margin_home"),
        (pl.col("total_actual") - pl.col("closing_total")).alias("ou_margin"),
        (pl.col("margin_actual") - pl.col("margin_home")).alias("margin_resid"),
        (pl.col("total_actual") - pl.col("projected_total")).alias("total_resid"),
    )


def bootstrap_corr(
    x: np.ndarray, y: np.ndarray, *, n_boot: int = BOOTSTRAP_N, seed: int = BOOTSTRAP_SEED
) -> dict[str, float | int | None]:
    """Pearson r with a game-bootstrap 95% percentile CI (fixed seed, reproducible)."""
    x = np.asarray(x, dtype=float)
    y = np.asarray(y, dtype=float)
    n = int(x.size)
    if n < 3:
        return {"n": n, "corr": None, "ci_low": None, "ci_high": None}
    corr = float(np.corrcoef(x, y)[0, 1])
    rng = np.random.default_rng(seed)
    idx = rng.integers(0, n, size=(n_boot, n))
    xs, ys = x[idx], y[idx]
    xs = xs - xs.mean(axis=1, keepdims=True)
    ys = ys - ys.mean(axis=1, keepdims=True)
    denom = np.sqrt((xs**2).sum(axis=1) * (ys**2).sum(axis=1))
    boot = (xs * ys).sum(axis=1)[denom > 0] / denom[denom > 0]
    lo, hi = np.quantile(boot, [0.025, 0.975])
    return {"n": n, "corr": corr, "ci_low": float(lo), "ci_high": float(hi)}


def edge_validation(frame: pl.DataFrame) -> dict[str, dict[str, Any]]:
    """Did walk-forward edges predict results *against the closing line*?

    - spread: corr(-edge_spread, ats_margin_home). -edge_spread > 0 means the model
      likes the home side more than the close; ats_margin_home > 0 means home covered.
    - total: corr(edge_total, ou_margin).

    `validated` is true only if the bootstrap 95% CI lies entirely above 0 (the
    pre-registered rule in docs/phases/P5.md). Games without a closing line are dropped.
    """
    out: dict[str, dict[str, Any]] = {}
    for market, x_expr, y_col in (
        ("spread", -pl.col("edge_spread"), "ats_margin_home"),
        ("total", pl.col("edge_total"), "ou_margin"),
    ):
        pair = frame.select(x_expr.alias("x"), pl.col(y_col).alias("y")).drop_nulls()
        stats = bootstrap_corr(pair["x"].to_numpy(), pair["y"].to_numpy())
        ci_low = stats["ci_low"]
        out[market] = {**stats, "validated": ci_low is not None and ci_low > 0}
    return out


def rms(values: pl.Series) -> float | None:
    """Root-mean-square, not a demeaned SD: a bias widens the band instead of hiding."""
    v = values.drop_nulls().to_numpy().astype(float)
    return float(np.sqrt(np.mean(v**2))) if v.size else None


def calibrate(oos: pl.DataFrame) -> tuple[pl.DataFrame, dict[str, Any]]:
    """Per-stability-bucket calibration from walk-forward (out-of-sample) predictions
    that already went through `add_outcomes`. Returns the frame with `stability_bucket`
    added, and the payload for model_coefficients.json:
    tercile cutpoints of `stability_min`, then per bucket the game count, the RMS
    margin/total residual (the card's +/- band) and the edge validation."""
    cutpoints = stability_cutpoints(oos["stability_min"].drop_nulls().to_list())
    bucketed = with_stability_bucket(oos, cutpoints)
    buckets: dict[str, Any] = {}
    for bucket in STABILITY_BUCKETS:
        b = bucketed.filter(pl.col("stability_bucket") == bucket)
        buckets[bucket] = {
            "n_games": b.height,
            "stability_range": [b["stability_min"].min(), b["stability_min"].max()],
            "margin_sd": rms(b["margin_resid"]),
            "total_sd": rms(b["total_resid"]),
            "edge_validation": edge_validation(b),
        }
    payload = {
        "test_seasons": sorted(set(oos["season"].to_list())),
        "n_games": oos.height,
        "stability_cutpoints": list(cutpoints),
        "buckets": buckets,
    }
    return bucketed, payload
