"""One-off script: pull each P2 nflverse-bulk source live and save a trimmed fixture.

Not part of the pipeline — run manually when a source's shape needs re-verifying.
Usage: uv run python scripts/make_nflverse_bulk_fixtures.py
"""

from pathlib import Path

import nflreadpy as nfl
import polars as pl

FIXTURES = Path(__file__).resolve().parent.parent / "tests" / "fixtures"


def main() -> None:
    FIXTURES.mkdir(parents=True, exist_ok=True)

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

    print("pbp (one game):", pbp_sample.shape, "from full", pbp.shape)
    print("player_stats sample:", (20, player_stats.width), "from full", player_stats.shape)
    print("team_stats sample:", (20, team_stats.width), "from full", team_stats.shape)
    print("snap_counts sample:", (20, snap_counts.width), "from full", snap_counts.shape)
    print("ftn_charting sample:", (20, ftn.width), "from full", ftn.shape)
    print("depth_charts sample:", (20, depth.width), "from full", depth.shape)
    print("rosters sample:", (20, rosters.width), "from full", rosters.shape)


if __name__ == "__main__":
    main()
