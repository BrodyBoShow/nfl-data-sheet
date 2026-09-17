"""One-off script: estimate the year-over-year reliability factor r for each Efficiency
metric/side, by correlating full-season opponent-adjusted ratings across adjacent
2018-2025 seasons. Calls the SAME production rating machinery the analyst's own
prior-season solve uses (_solve_ratings, _league_avg, _METRIC_CONFIG) instead of
reimplementing the math -- see CLAUDE.md's verification-script convention.

Method: for each season, solve each metric's opponent-adjusted offense/defense ratings
independently (the same "plain solve" _solve_ratings path used for a prior-season solve
in production -- k_metric shrinkage included, since that's the same kind of value that
actually gets fed into _blend as `prior`). For each metric/side, correlate season-N
ratings against season-N+1 ratings across teams, per adjacent pair and pooled across all
pairs. The pooled, [0,1]-clipped r is the candidate reliability factor.

Not part of the pipeline. Read-only against the database; prints a report only, writes
nothing.

Usage:
  uv run python scripts/estimate_reliability.py
  uv run python scripts/estimate_reliability.py --seasons 2018-2025
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import polars as pl
import psycopg

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pipeline.analysts.efficiency import (  # noqa: E402
    _METRIC_CONFIG,
    _TEAM_WEEK_SCHEMA,
    _TEAM_WEEK_SELECT_COLS,
    MetricConfig,
    _league_avg,
    _rows_to_df,
    _solve_ratings,
)
from pipeline.core.db import get_connection  # noqa: E402

_MIN_TEAMS_FOR_CORRELATION = 5
_SHRINKAGE_WEIGHT = 0.5
_ZERO_SIGNAL_METRICS = {"epa_per_play_down4", "success_rate_down4"}


def _fetch_season_team_week(conn: psycopg.Connection, season: int) -> pl.DataFrame:
    cols = ["season", "week", "season_type", "team", "opponent_team"] + _TEAM_WEEK_SELECT_COLS
    with conn.cursor() as cur:
        cur.execute(
            f"SELECT {', '.join(cols)} FROM team_week WHERE season = %s AND season_type = 'REG'",
            (season,),
        )
        rows = cur.fetchall()
    return _rows_to_df(rows, _TEAM_WEEK_SCHEMA)


_TeamRatings = dict[str, tuple[float | None, float | None]]


def _season_ratings(df: pl.DataFrame, metric: MetricConfig) -> _TeamRatings:
    """team -> (off_rating, def_rating) for one season, one metric."""
    league_avg = _league_avg(df, metric.numerator_col, metric.denominator_col)
    if league_avg is None:
        return {}
    ratings = _solve_ratings(
        df, metric.numerator_col, metric.denominator_col, league_avg, metric.k_metric
    )
    return {t: (r.off, r.def_) for t, r in ratings.items()}


def _pearson(a: list[float], b: list[float]) -> float | None:
    if len(a) < _MIN_TEAMS_FOR_CORRELATION:
        return None
    result = pl.DataFrame({"a": a, "b": b}).select(pl.corr("a", "b")).item()
    return None if result is None else float(result)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--seasons", default="2018-2025")
    args = parser.parse_args()
    start, end = (int(x) for x in args.seasons.split("-"))
    seasons = list(range(start, end + 1))
    pairs = list(zip(seasons, seasons[1:], strict=False))

    with get_connection() as conn:
        season_team_week = {s: _fetch_season_team_week(conn, s) for s in seasons}

    for s in seasons:
        print(f"season {s}: {season_team_week[s]['team'].n_unique()} teams staged")
    print(f"\nadjacent pairs: {pairs}\n")

    reliability: dict[str, float] = {}
    flags: list[str] = []

    for metric in _METRIC_CONFIG:
        season_ratings = {s: _season_ratings(season_team_week[s], metric) for s in seasons}

        side_pooled: dict[str, float | None] = {}
        for side, idx in (("off", 0), ("def", 1)):
            pair_results: list[tuple[int, int, float | None, int]] = []
            pooled_a: list[float] = []
            pooled_b: list[float] = []

            for s1, s2 in pairs:
                r1, r2 = season_ratings[s1], season_ratings[s2]
                a: list[float] = []
                b: list[float] = []
                for t in sorted(set(r1) & set(r2)):
                    va, vb = r1[t][idx], r2[t][idx]
                    if va is not None and vb is not None:
                        a.append(va)
                        b.append(vb)
                pair_r = _pearson(a, b)
                pair_results.append((s1, s2, pair_r, len(a)))
                if pair_r is not None:
                    pooled_a.extend(a)
                    pooled_b.extend(b)

            pooled_r = _pearson(pooled_a, pooled_b)
            clipped = None if pooled_r is None else max(0.0, min(1.0, pooled_r))
            side_pooled[side] = clipped

            key = f"{metric.name}_{side}"
            print(f"{key}:")
            for s1, s2, pair_r, n in pair_results:
                pair_str = f"{pair_r:.3f}" if pair_r is not None else f"n/a (n={n})"
                print(f"  {s1}->{s2}: r={pair_str}")
            pooled_str = f"{pooled_r:.3f}" if pooled_r is not None else "n/a"
            clipped_str = f"{clipped:.3f}" if clipped is not None else "n/a"
            print(f"  pooled r={pooled_str} -> clipped reliability={clipped_str}\n")

            if clipped is not None:
                reliability[key] = clipped

        off_r, def_r = side_pooled.get("off"), side_pooled.get("def")
        if off_r is not None and off_r < 0.2:
            flags.append(f"{metric.name}: offense r={off_r:.3f} < 0.2")
        if off_r is not None and def_r is not None and def_r > off_r:
            flags.append(f"{metric.name}: defense r={def_r:.3f} > offense r={off_r:.3f}")

    print("=" * 70)
    print("Flags for review (not wired in automatically):")
    if flags:
        for f in flags:
            print(f"  - {f}")
    else:
        print("  none")

    print("\nAll clipped pooled (raw) reliability values:")
    for key, val in reliability.items():
        print(f"  {key}: {val:.3f}")

    # Shrink each side's raw estimates toward that side's own mean -- 7 season-pairs of
    # 32 teams is a small sample, so a single metric's pooled r is itself a noisy
    # estimate. epa_per_play_down4/success_rate_down4 are excluded from both the mean
    # and the shrinkage (held at exactly 0) -- their negative raw pooled r (before
    # clipping) is a real finding (4th-down attempts are too rare a denominator for any
    # year-over-year signal to survive), not noise to shrink away.
    print("\n" + "=" * 70)
    print(
        f"Shrinkage: r_final = {_SHRINKAGE_WEIGHT} * r_raw + "
        f"{1 - _SHRINKAGE_WEIGHT} * r_side_mean"
    )
    print(f"(excluding {sorted(_ZERO_SIGNAL_METRICS)} from the mean and from shrinkage itself)\n")

    final: dict[str, float] = {}
    for side in ("off", "def"):
        side_vals = [
            v
            for k, v in reliability.items()
            if k.endswith(f"_{side}") and k[: -len(f"_{side}")] not in _ZERO_SIGNAL_METRICS
        ]
        side_mean = sum(side_vals) / len(side_vals)
        print(f"{side} mean (excluding zero-signal metrics): {side_mean:.4f}")
        for metric in _METRIC_CONFIG:
            key = f"{metric.name}_{side}"
            if metric.name in _ZERO_SIGNAL_METRICS:
                final[key] = 0.0
                continue
            raw = reliability[key]
            final[key] = _SHRINKAGE_WEIGHT * raw + (1 - _SHRINKAGE_WEIGHT) * side_mean

    print(f"\n{'metric_side':<28}{'raw':>8}{'final':>8}")
    for metric in _METRIC_CONFIG:
        for side in ("off", "def"):
            key = f"{metric.name}_{side}"
            print(f"{key:<28}{reliability[key]:>8.4f}{final[key]:>8.4f}")

    print("\nMetricConfig-ready values (name, reliability_off, reliability_def, "
          "reliability_off_raw, reliability_def_raw):")
    for metric in _METRIC_CONFIG:
        print(
            f'  ("{metric.name}", '
            f"{final[f'{metric.name}_off']:.4f}, {final[f'{metric.name}_def']:.4f}, "
            f"{reliability[f'{metric.name}_off']:.4f}, {reliability[f'{metric.name}_def']:.4f}),"
        )


if __name__ == "__main__":
    main()
