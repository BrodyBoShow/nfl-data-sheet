"""One-off script: show what the Usage and Player efficiency analysts would write, without
writing it (P7 step 6). Calls the analysts' own fetch and build functions (CLAUDE.md
verification rule) and prints row counts, column coverage against the migrations, and one
player's rows beside his raw player_game_pbp games, for a hand check of the arithmetic.

Read-only: never calls run(), write_signals() or any upsert; rolls back its connection.

Usage:
  uv run python scripts/inspect_player_tables.py --season 2026 --week 3
  uv run python scripts/inspect_player_tables.py --season 2026 --week 3 --player 00-0036900
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

import polars as pl

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pipeline.analysts import player_efficiency as eff  # noqa: E402
from pipeline.analysts import usage  # noqa: E402
from pipeline.core.db import get_connection  # noqa: E402
from pipeline.core.player_tables import UNHASHED_COLS  # noqa: E402

pl.Config.set_tbl_cols(20)
pl.Config.set_tbl_width_chars(200)
pl.Config.set_tbl_hide_column_data_types(True)
pl.Config.set_tbl_hide_dataframe_shape(True)


def _coverage(df: pl.DataFrame, columns: list[str], name: str) -> None:
    produced = [c for c in columns if c not in UNHASHED_COLS]
    missing = [c for c in produced if c not in df.columns]
    print(f"\n{name}: {df.height} rows, weeks {sorted(df['week'].unique().to_list())}")
    print(f"  migration columns not produced: {missing or 'none'}")
    by_week = df.group_by("week").len().sort("week")
    print(f"  rows by week: {dict(zip(by_week['week'], by_week['len'], strict=True))}")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--season", type=int, required=True)
    ap.add_argument("--week", type=int, required=True)
    ap.add_argument("--player", help="gsis id to spot-check (default: the week's top target)")
    args = ap.parse_args()

    with get_connection() as conn:
        t = time.monotonic()
        snaps, pgp, positions = usage._fetch(conn, args.season, args.week)
        u = usage.build_usage_rows(snaps, pgp, positions)
        print(f"usage built in {time.monotonic() - t:.1f}s")
        t = time.monotonic()
        inp = eff._fetch(conn, args.season, args.week)
        e = eff.build_eff_rows(inp, args.season)
        print(f"player_efficiency built in {time.monotonic() - t:.1f}s")
        conn.rollback()

    _coverage(u, usage.COLUMNS, "player_usage_week")
    _coverage(e, eff.COLUMNS, "player_eff_week")

    print("\nlast week, among rows holding the family's role this season:")
    last = e.filter(pl.col("week") == e["week"].max())
    for fam, (prefix, _) in eff._FAMILY_SAMPLE.items():
        held = last.filter(pl.col(f"{prefix}_std").is_not_null())
        cols = [f"{m.name}_std" for m in eff.METRICS if m.family == fam]
        pct_cols = [f"{m.name}_pct" for m in eff.METRICS if m.family == fam]
        std = held.select(pl.mean_horizontal(pl.col(c).is_not_null() for c in cols)).mean()
        pct = held.select(pl.mean_horizontal(pl.col(c).is_not_null() for c in pct_cols)).mean()
        head = eff.HEADLINE[fam]
        ranked = held.filter(pl.col(f"{head}_pct").is_not_null()).height
        stab = sorted(
            v for v in held[f"{eff._FAMILY_PREFIX[fam]}_stability"].to_list() if v is not None
        )
        mid = stab[len(stab) // 2] if stab else float("nan")
        lo, hi = (stab[0], stab[-1]) if stab else (float("nan"), float("nan"))
        print(
            f"  {fam:<9} rows {held.height:4d}  metrics non-null: _std {std.item():.2f} "
            f"_pct {pct.item():.2f}  headline ranked {ranked:4d}  stability median "
            f"{mid:.2f} (min {lo:.2f}, max {hi:.2f})"
        )

    pgp_all = inp.pgp.filter(pl.col("season") == args.season)
    pid = (
        args.player
        or (
            pgp_all.filter(pl.col("week") == args.week).sort(
                "targets", descending=True, nulls_last=True
            )["player_id"][0]
        )
    )
    print(f"\nspot check: {pid}")
    raw = (
        pgp_all.filter(pl.col("player_id") == pid)
        .sort("week")
        .select(
            "week",
            "game_id",
            "targets",
            "receptions",
            "rec_epa_sum",
            "rec_yards",
            "carries",
            "rush_epa_sum",
            "dropbacks",
            "dropback_epa_sum",
        )
    )
    print("raw player_game_pbp:")
    print(raw)
    print("player_usage_week:")
    print(
        u.filter(pl.col("player_id") == pid).select(
            "week",
            "usage_games_std",
            "off_snap_share_game",
            "target_share_game",
            "target_share_l4",
            "target_share_std",
            "target_share_wow",
            "target_share_pct",
            "usage_stability",
        )
    )
    print("player_eff_week (receiving):")
    print(
        e.filter(pl.col("player_id") == pid).select(
            "week",
            "position_group",
            "rec_targets_game",
            "rec_targets_std",
            "epa_per_target_game",
            "epa_per_target_l4",
            "epa_per_target__cur",
            "epa_per_target__prior",
            "epa_per_target__league",
            "epa_per_target_std",
            "epa_per_target_pct",
            "rec_stability",
            "hist_span",
            "epa_per_target_vs_man_hist",
        )
    )


if __name__ == "__main__":
    main()
