"""One-off script: check whether Efficiency's recorded year-over-year reliability r
(scripts/estimate_reliability.py, a pooled correlation of k-shrunk season ratings) matches
r_slope, the coefficient the blend actually multiplies (P7 step 6, 2026-09-29).

The blend predicts this season's rating as league + r * (prior - league), where `prior`
is last season's k-shrunk plain solve. The r that makes that prediction unbiased is the
slope of next season's unshrunk rating on this season's shrunk rating. r_corr equals
that slope only when season ratings carry little sampling noise. This script measures
both on the same pairs, with Efficiency's own machinery (`_season_ratings`,
`_solve_ratings`, `_league_avg`, `_METRIC_CONFIG`), never a reimplementation.

Read-only against the database (team_week); prints a report, writes nothing.

Usage:
  uv run python scripts/verify_efficiency_r_slope.py
  uv run python scripts/verify_efficiency_r_slope.py --seasons 2018-2025
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import numpy as np
import polars as pl

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from estimate_reliability import _fetch_season_team_week, _season_ratings  # noqa: E402

from pipeline.analysts.efficiency import (  # noqa: E402
    _METRIC_CONFIG,
    MetricConfig,
    _league_avg,
    _solve_ratings,
)
from pipeline.core.db import get_connection  # noqa: E402

_BOOT_REPS = 1000


def _unshrunk_ratings(
    df: pl.DataFrame, metric: MetricConfig
) -> dict[str, tuple[float | None, float | None]]:
    """The same plain solve with k = 0: each team's own opponent-adjusted rate."""
    league = _league_avg(df, metric.numerator_col, metric.denominator_col)
    if league is None:
        return {}
    ratings = _solve_ratings(df, metric.numerator_col, metric.denominator_col, league, 0.0)
    return {t: (r.off, r.def_) for t, r in ratings.items() if r.off is not None}


def _pairs(
    data: dict[int, pl.DataFrame], seasons: list[int], metric: MetricConfig, idx: int
) -> pl.DataFrame:
    rows = []
    for s1, s2 in zip(seasons, seasons[1:], strict=False):
        l1 = _league_avg(data[s1], metric.numerator_col, metric.denominator_col)
        l2 = _league_avg(data[s2], metric.numerator_col, metric.denominator_col)
        if l1 is None or l2 is None:
            continue
        shrunk1, shrunk2 = _season_ratings(data[s1], metric), _season_ratings(data[s2], metric)
        raw2 = _unshrunk_ratings(data[s2], metric)
        for team in sorted(set(shrunk1) & set(shrunk2) & set(raw2)):
            a, b, c = shrunk1[team][idx], shrunk2[team][idx], raw2[team][idx]
            if a is None or b is None or c is None:
                continue
            rows.append({"team": team, "x": a - l1, "y_shrunk": b - l2, "y_raw": c - l2})
    return pl.DataFrame(rows)


def _stats(x: np.ndarray, ys: np.ndarray, yr: np.ndarray) -> tuple[float, float]:
    corr = float(np.corrcoef(x, ys)[0, 1])
    xc = x - x.mean()
    slope = float((xc * (yr - yr.mean())).sum() / (xc**2).sum())
    return corr, slope


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--seasons", default="2018-2025")
    args = parser.parse_args()
    start, end = (int(x) for x in args.seasons.split("-"))
    seasons = list(range(start, end + 1))
    with get_connection() as conn:
        data = {s: _fetch_season_team_week(conn, s) for s in seasons}

    rng = np.random.default_rng(20260929)
    print(
        f"{'metric_side':<28}{'recorded raw':>13}{'r_corr':>8}{'r_slope':>9}"
        f"{'slope 90%':>16}{'ratio':>7}"
    )
    ratios = []
    for metric in _METRIC_CONFIG:
        for side, idx, recorded in (
            ("off", 0, metric.reliability_off_raw),
            ("def", 1, metric.reliability_def_raw),
        ):
            p = _pairs(data, seasons, metric, idx)
            x, ys, yr = (p[c].to_numpy() for c in ("x", "y_shrunk", "y_raw"))
            corr, slope = _stats(x, ys, yr)
            teams = p["team"].to_numpy()
            uniq = np.unique(teams)
            boot = []
            for _ in range(_BOOT_REPS):
                pick = rng.choice(uniq, size=len(uniq), replace=True)
                sel = np.concatenate([np.flatnonzero(teams == t) for t in pick])
                boot.append(_stats(x[sel], ys[sel], yr[sel])[1])
            lo, hi = np.percentile(boot, [5, 95])
            ratio = slope / corr if corr > 0 else float("nan")
            if metric.name not in ("epa_per_play_down4", "success_rate_down4"):
                ratios.append(ratio)
            print(
                f"{metric.name + '_' + side:<28}{recorded:>13.3f}{corr:>8.3f}{slope:>9.3f}"
                f"{f'[{lo:.2f}, {hi:.2f}]':>16}{ratio:>7.2f}"
            )
    print(
        f"\nr_slope / r_corr, excluding down4: median {np.median(ratios):.2f}, "
        f"range {min(ratios):.2f}-{max(ratios):.2f}"
    )


if __name__ == "__main__":
    main()
