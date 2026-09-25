"""One-off script: pull each nflverse-bulk source live and save a trimmed fixture.

Not part of the pipeline — run manually when a source's shape needs re-verifying.
Usage: uv run python scripts/make_nflverse_bulk_fixtures.py [--only participation]
(`--only participation` regenerates just the P7 participation fixtures, leaving the P2
ones untouched.)
"""

import argparse
from pathlib import Path

import nflreadpy as nfl
import polars as pl

FIXTURES = Path(__file__).resolve().parent.parent / "tests" / "fixtures"


def participation() -> None:
    """P7. Two samples, because the schema changed between providers:
    - 2025 (FTN-sourced): every play of the SAME game as nflreadpy_pbp_sample.parquet, so
      tests can join participation onto pbp. Float64 play_id, `''` for "none" in
      route/defense_man_zone_type, full-position personnel strings.
    - 2022 (NGS-sourced): head(20). Int32 play_id, short personnel strings
      ("1 RB, 1 TE, 3 WR"), no names/positions/numbers columns.
    nflreadpy refuses the current season (post-season release only)."""
    pbp_game = pl.read_parquet(FIXTURES / "nflreadpy_pbp_sample.parquet")["game_id"][0]
    part_2025 = nfl.load_participation(seasons=[2025])
    sample_2025 = part_2025.filter(pl.col("nflverse_game_id") == pbp_game)
    sample_2025.write_parquet(FIXTURES / "nflreadpy_participation_sample.parquet")
    part_2022 = nfl.load_participation(seasons=[2022])
    part_2022.head(20).write_parquet(FIXTURES / "nflreadpy_participation_2022_sample.parquet")
    print(f"participation 2025 (game {pbp_game}):", sample_2025.shape, "from full", part_2025.shape)
    print("participation 2022 sample:", (20, part_2022.width), "from full", part_2022.shape)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--only", choices=["participation"])
    args = parser.parse_args()
    FIXTURES.mkdir(parents=True, exist_ok=True)
    if args.only == "participation":
        participation()
        return

    pbp = nfl.load_pbp(seasons=[2025])
    one_game = pbp["game_id"][0]
    pbp_sample = pbp.filter(pl.col("game_id") == one_game)
    pbp_sample.write_parquet(FIXTURES / "nflreadpy_pbp_sample.parquet")

    player_stats = nfl.load_player_stats(seasons=[2025])
    player_stats.head(20).write_parquet(FIXTURES / "nflreadpy_player_stats_sample.parquet")

    team_stats = nfl.load_team_stats(seasons=[2025])
    team_stats.head(20).write_parquet(FIXTURES / "nflreadpy_team_stats_sample.parquet")

    snap_counts = nfl.load_snap_counts(seasons=[2025])
    snap_counts.head(20).write_parquet(FIXTURES / "nflreadpy_snap_counts_sample.parquet")

    ftn = nfl.load_ftn_charting(seasons=[2025])
    ftn.head(20).write_parquet(FIXTURES / "nflreadpy_ftn_charting_sample.parquet")

    depth = nfl.load_depth_charts(seasons=[2025])
    depth.head(20).write_parquet(FIXTURES / "nflreadpy_depth_charts_sample.parquet")

    rosters = nfl.load_rosters(seasons=[2025])
    rosters.head(20).write_parquet(FIXTURES / "nflreadpy_rosters_sample.parquet")

    for stat_type in ("passing", "rushing", "receiving"):
        ngs = nfl.load_nextgen_stats(seasons=[2025], stat_type=stat_type)
        ngs.head(15).write_parquet(FIXTURES / f"nflreadpy_nextgen_{stat_type}_sample.parquet")

    for stat_type in ("pass", "rush", "rec", "def"):
        pfr = nfl.load_pfr_advstats(seasons=[2025], stat_type=stat_type)
        pfr.head(15).write_parquet(FIXTURES / f"nflreadpy_pfr_advstats_{stat_type}_sample.parquet")

    participation()

    print("pbp (one game):", pbp_sample.shape, "from full", pbp.shape)
    print("player_stats sample:", (20, player_stats.width), "from full", player_stats.shape)
    print("team_stats sample:", (20, team_stats.width), "from full", team_stats.shape)
    print("snap_counts sample:", (20, snap_counts.width), "from full", snap_counts.shape)
    print("ftn_charting sample:", (20, ftn.width), "from full", ftn.shape)
    print("depth_charts sample:", (20, depth.width), "from full", depth.shape)
    print("rosters sample:", (20, rosters.width), "from full", rosters.shape)


if __name__ == "__main__":
    main()
