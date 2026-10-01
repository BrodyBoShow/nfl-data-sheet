"""One-off script: compare two projected-primary rules per position group
(docs/phases/P9.md, D5). Run it after the 2026-10-02..05 calibration window, never during.

- Last game's primary (the shipped rule): the team's primary in its previous game.
- Most `_l4` snap share: the latest as-of row per player before the game, on that team,
  with the highest `_l4` share in the group.

The truth is the game's actual primary: the most snaps at the position group in that game
(D2). Offense groups use offensive snaps, DL/LB defensive snaps (D3). OL and DB are left
out: they show no single primary (D4).

The rows come from the Usage analyst's own `_fetch` and `build_usage_rows`, so this
compares rules on exactly what `player_usage_week` holds. Nothing is re-derived and
nothing is written. A primary is ranked by the `_game` share, which ranks the same as snap
count within a team-game (one denominator). Ties break on player_id.

Scope: REG only. A team's first game of a season has no previous game, so it's left out
for both rules (D8: week 1 gets no projection). Both rules are scored on the same games.

Output per group and season: n, each rule's hit rate with a Wilson 95% CI, and the paired
per-game difference (`_l4` minus last game) with a bootstrap 95% CI. D5 switches rules
only if `_l4` wins and that CI's lower bound is above 0.

Usage:
  uv run python scripts/compare_primary_rules.py [--seasons 2025,2026]
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import polars as pl

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pipeline.core.stats import bootstrap_mean_ci, wilson_ci  # noqa: E402

# D3/D4: the single-primary groups, and which side's snap share decides each.
GROUP_SIDE = {"QB": "off", "RB": "off", "TE": "off", "WR": "off", "DL": "def", "LB": "def"}

_KEYS = ["season", "team", "week", "position_group"]


def _with_side_cols(rows: pl.DataFrame) -> pl.DataFrame:
    """Single-primary REG rows, with the group's `_game`/`_l4` share as `share_game`/
    `share_l4`."""
    rows = rows.filter(
        (pl.col("season_type") == "REG") & pl.col("position_group").is_in(list(GROUP_SIDE))
    )
    side = pl.col("position_group").replace_strict(GROUP_SIDE, return_dtype=pl.Utf8)
    return rows.with_columns(
        pl.when(side == "off")
        .then(pl.col("off_snap_share_game"))
        .otherwise(pl.col("def_snap_share_game"))
        .alias("share_game"),
        pl.when(side == "off")
        .then(pl.col("off_snap_share_l4"))
        .otherwise(pl.col("def_snap_share_l4"))
        .alias("share_l4"),
    )


def _top(df: pl.DataFrame, by: str, keys: list[str]) -> pl.DataFrame:
    """The highest `by` per `keys` (nulls never win), ties broken by player_id."""
    return (
        df.filter(pl.col(by).is_not_null() & (pl.col(by) > 0))
        .sort([*keys, by, "player_id"], descending=[*[False] * len(keys), True, False])
        .group_by(keys, maintain_order=True)
        .first()
        .select(*keys, "player_id")
    )


def actual_primaries(rows: pl.DataFrame) -> pl.DataFrame:
    """(season, team, week, position_group, player_id): each team-game's actual primary."""
    return _top(_with_side_cols(rows), "share_game", _KEYS)


def project_last_game(rows: pl.DataFrame) -> pl.DataFrame:
    """Last game's primary: for each team game after its first, the primary of the team's
    previous game. Keyed by the game being projected."""
    actual = actual_primaries(rows)
    games = rows.select("season", "team", "week").unique().sort("season", "team", "week")
    prev = games.with_columns(
        pl.col("week").shift(1).over(["season", "team"]).alias("prev_week")
    ).drop_nulls("prev_week")
    return prev.join(
        actual.rename({"week": "prev_week"}), on=["season", "team", "prev_week"]
    ).select(*_KEYS, "player_id")


def project_l4(rows: pl.DataFrame) -> pl.DataFrame:
    """Most `_l4` share: for each team game after its first, each player's latest row
    before that week whose team is this team (the web's as-of rule), highest `_l4` share
    in the group."""
    side = _with_side_cols(rows)
    games = rows.select("season", "team", "week").unique()
    first = games.group_by("season", "team").agg(pl.col("week").min().alias("first_week"))
    games = games.join(first, on=["season", "team"]).filter(pl.col("week") > pl.col("first_week"))
    out = []
    for (season, week), g in games.group_by("season", "week"):
        latest = (
            side.filter((pl.col("season") == season) & (pl.col("week") < week))
            .sort("week")
            .group_by("player_id", maintain_order=True)
            .last()
            .join(g.select("team"), on="team")
            .with_columns(pl.lit(week).cast(pl.Int64).alias("week"))
        )
        out.append(_top(latest, "share_l4", _KEYS))
    if not out:
        return pl.DataFrame(schema={**{k: pl.Int64 for k in _KEYS}, "player_id": pl.Utf8})
    return pl.concat(out)


def score(rows: pl.DataFrame) -> list[dict[str, object]]:
    """Per (season, group): both rules on the same games, against the actual primary."""
    actual = actual_primaries(rows).rename({"player_id": "actual"})
    last = project_last_game(rows).rename({"player_id": "last"})
    l4 = project_l4(rows).rename({"player_id": "l4"})
    games = actual.join(last, on=_KEYS).join(l4, on=_KEYS, how="left")
    games = games.with_columns(
        (pl.col("last") == pl.col("actual")).cast(pl.Int8).alias("hit_last"),
        (pl.col("l4") == pl.col("actual")).fill_null(False).cast(pl.Int8).alias("hit_l4"),
    )
    out: list[dict[str, object]] = []
    for (season, group), g in games.group_by("season", "position_group"):
        n = g.height
        hl, h4 = int(g["hit_last"].sum()), int(g["hit_l4"].sum())
        diff = bootstrap_mean_ci((g["hit_l4"] - g["hit_last"]).to_numpy().astype(np.float64))
        out.append(
            {
                "season": season,
                "group": group,
                "n": n,
                "last_game": (hl / n, *wilson_ci(hl, n)),
                "l4": (h4 / n, *wilson_ci(h4, n)),
                "diff_l4_minus_last": (diff["mean"], diff["ci_low"], diff["ci_high"]),
            }
        )
    return sorted(out, key=lambda r: (r["season"], r["group"]))  # type: ignore[arg-type, return-value]


def _fmt(t: tuple[float | None, float | None, float | None]) -> str:
    v, lo, hi = t
    if v is None:
        return "n/a"
    return f"{v:.3f} [{lo:.3f}, {hi:.3f}]" if lo is not None else f"{v:.3f}"


def main() -> int:
    from pipeline.analysts.usage import _fetch, build_usage_rows
    from pipeline.core.db import get_connection

    seasons = [2025, 2026]
    if "--seasons" in sys.argv:
        seasons = [int(s) for s in sys.argv[sys.argv.index("--seasons") + 1].split(",")]
    frames = []
    with get_connection() as conn:
        conn.read_only = True
        for season in seasons:
            snaps, pgp, positions = _fetch(conn, season, 99)
            frames.append(build_usage_rows(snaps, pgp, positions))
        conn.rollback()
    results = score(pl.concat(frames, how="diagonal_relaxed"))

    print("season group    n  last game [95% CI]       l4 share [95% CI]        l4 - last [95% CI]")
    for r in results:
        d = r["diff_l4_minus_last"]
        verdict = "l4 wins" if d[1] is not None and d[1] > 0 else "no switch"  # type: ignore[index]
        print(
            f"{r['season']}  {r['group']:<4} {r['n']:>4}  {_fmt(r['last_game']):<24} "  # type: ignore[arg-type]
            f"{_fmt(r['l4']):<24} {_fmt(d):<26} {verdict}"  # type: ignore[arg-type]
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
