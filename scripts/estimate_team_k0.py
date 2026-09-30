"""One-off script: estimate team k0 for every Efficiency metric/side over 2018-2025 and
compare each split's k0 with its parent's (P2 open item 3, the untested team half).

k0 is the sample, in the metric's denominator units, at which a team's own season rate is
half signal: sigma_e^2 (per-unit game-to-game noise) over tau^2 (true between-team
variance). It's what `k_metric` should be if it's sized by measurement. The estimator is
the player one (`scripts/estimate_player_reliability.py`'s `_player_seasons`/`_k0`: one-way
random effects on per-game aggregates, pooled across seasons), with a team-side standing
in for a player.

Opponent adjustment uses Efficiency's own machinery. Each season gets a plain
`_solve_ratings` with k = 0 (unshrunk, as `verify_efficiency_r_slope.py` does). Each game
row is then adjusted the way the solver adjusts it, `n + d * (league - opponent_rating)`:
  - offense side: the opponent's defense rating
  - defense side: the opponent's offense rating
The opponent rating is its full-season rating, including this game. That's a small
dependence, and the rating's own estimation noise adds a little game-to-game noise. Both
push k0 up slightly, and they hit the parent and its splits alike.

Also prints the unadjusted (raw) k0, to separate the two effects, and raw 2025 alone,
which anchors to the 2026-09-29 raw check (354 for `epa_per_play`, 260 for
`epa_per_play_pass`, on `team_week` before the try filter).

Read-only against the database (team_week, REG only); prints a report, writes nothing.

Usage:
  uv run python scripts/estimate_team_k0.py
  uv run python scripts/estimate_team_k0.py --seasons 2018-2025
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import numpy as np
import polars as pl

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from estimate_player_reliability import _boot_weights, _k0, _player_seasons  # noqa: E402
from estimate_reliability import _fetch_season_team_week  # noqa: E402

from pipeline.analysts.efficiency import (  # noqa: E402
    _METRIC_CONFIG,
    MetricConfig,
    _league_avg,
    _solve_ratings,
)
from pipeline.core.db import get_connection  # noqa: E402

_BOOT_REPS = 200

# Each split's parent: same plays, subset by play type or down.
_PARENT = {
    "epa_per_play_pass": "epa_per_play",
    "epa_per_play_rush": "epa_per_play",
    "epa_per_play_down1": "epa_per_play",
    "epa_per_play_down2": "epa_per_play",
    "epa_per_play_down3": "epa_per_play",
    "epa_per_play_down4": "epa_per_play",
    "success_rate_pass": "success_rate",
    "success_rate_rush": "success_rate",
    "success_rate_down1": "success_rate",
    "success_rate_down2": "success_rate",
    "success_rate_down3": "success_rate",
    "success_rate_down4": "success_rate",
    "explosive_rate_pass": "explosive_rate",
    "explosive_rate_rush": "explosive_rate",
}


def _side_rows(df: pl.DataFrame, metric: MetricConfig, adjust: bool) -> dict[str, pl.DataFrame]:
    """Per team-game (numerator, denominator) for each side, opponent-adjusted or raw."""
    num, den = metric.numerator_col, metric.denominator_col
    rows = df.select(
        "season",
        "week",
        "team",
        "opponent_team",
        pl.col(num).cast(pl.Float64).alias("n"),
        pl.col(den).cast(pl.Float64).alias("d"),
    )
    off_adj = def_adj = pl.lit(0.0)
    if adjust:
        league = _league_avg(df, num, den)
        if league is None:
            return {}
        ratings = _solve_ratings(df, num, den, league, 0.0)
        off_r = {t: r.off for t, r in ratings.items() if r.off is not None}
        def_r = {t: r.def_ for t, r in ratings.items() if r.def_ is not None}
        # Offense row: this team's offense vs the opponent's defense.
        off_adj = pl.col("d") * (
            league - pl.col("opponent_team").replace_strict(def_r, default=league)
        )
        # Defense row (the opponent's defense on this same row) vs this team's offense.
        def_adj = pl.col("d") * (league - pl.col("team").replace_strict(off_r, default=league))
    return {
        "off": rows.select(
            "season",
            "week",
            pl.col("team").alias("player_id"),
            (pl.col("n") + off_adj).alias("n"),
            "d",
        ),
        "def": rows.select(
            "season",
            "week",
            pl.col("opponent_team").alias("player_id"),
            (pl.col("n") + def_adj).alias("n"),
            "d",
        ),
    }


def _estimate(frames: list[pl.DataFrame], rng: np.random.Generator) -> tuple[float, float, float]:
    ps = _player_seasons(pl.concat(frames), pl.col("n"), pl.col("d"))
    k0 = _k0(ps)[2]
    ids = ps["player_id"].to_numpy()
    boot = [_k0(ps, _boot_weights(ids, rng))[2] for _ in range(_BOOT_REPS)]
    lo, hi = np.percentile(boot, [5, 95])
    return k0, float(lo), float(hi)


def _fmt(x: float) -> str:
    return "inf" if not np.isfinite(x) or x > 1e6 else f"{x:.0f}"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--seasons", default="2018-2025")
    args = parser.parse_args()
    start, end = (int(x) for x in args.seasons.split("-"))
    seasons = list(range(start, end + 1))
    with get_connection() as conn:
        data = {s: _fetch_season_team_week(conn, s) for s in seasons}

    rng = np.random.default_rng(20260930)
    results: dict[tuple[str, str], tuple[float, float, float]] = {}
    print(
        f"{'metric_side':<26}{'k_metric':>9}{'adj k0':>8}{'adj 90%':>14}"
        f"{'raw k0':>8}{'raw 2025':>9}{'parent adj k0':>14}{'split/parent':>13}"
    )
    for metric in _METRIC_CONFIG:
        adj = {s: _side_rows(df, metric, adjust=True) for s, df in data.items()}
        raw = {s: _side_rows(df, metric, adjust=False) for s, df in data.items()}
        for side in ("off", "def"):
            k0, lo, hi = _estimate([adj[s][side] for s in seasons if adj[s]], rng)
            results[(metric.name, side)] = (k0, lo, hi)
            raw_k0 = _k0(
                _player_seasons(
                    pl.concat([raw[s][side] for s in seasons]), pl.col("n"), pl.col("d")
                )
            )[2]
            raw_2025 = (
                _k0(_player_seasons(raw[2025][side], pl.col("n"), pl.col("d")))[2]
                if 2025 in raw
                else float("nan")
            )
            parent = _PARENT.get(metric.name)
            pk0 = results[(parent, side)][0] if parent else float("nan")
            ratio = k0 / pk0 if parent and np.isfinite(k0) and np.isfinite(pk0) else float("nan")
            print(
                f"{metric.name + '_' + side:<26}{metric.k_metric:>9.0f}{_fmt(k0):>8}"
                f"{f'[{_fmt(lo)}-{_fmt(hi)}]':>14}{_fmt(raw_k0):>8}{_fmt(raw_2025):>9}"
                f"{(_fmt(pk0) if parent else '-'):>14}"
                f"{(f'{ratio:.2f}' if np.isfinite(ratio) else '-'):>13}"
            )


if __name__ == "__main__":
    main()
