from datetime import UTC, datetime
from pathlib import Path

import polars as pl
import pytest

from pipeline.collectors.nflverse_bulk import (
    _NGS_ALL_METRIC_COLS,
    _PFR_ALL_METRIC_COLS,
    NflverseBulkCollector,
    _build_depth,
    _build_ftn,
    _build_ngs,
    _build_pfr_advstats,
    _build_player_week,
    _build_snaps,
)

FIXTURES = Path(__file__).parent / "fixtures"

_NGS_TYPES = ("passing", "rushing", "receiving")
_PFR_TYPES = ("pass", "rush", "rec", "def")


def _load_raw() -> dict:
    return {
        "pbp": pl.read_parquet(FIXTURES / "nflreadpy_pbp_sample.parquet"),
        "player_stats": pl.read_parquet(FIXTURES / "nflreadpy_player_stats_sample.parquet"),
        "snap_counts": pl.read_parquet(FIXTURES / "nflreadpy_snap_counts_sample.parquet"),
        "ftn_charting": pl.read_parquet(FIXTURES / "nflreadpy_ftn_charting_sample.parquet"),
        "depth_charts": pl.read_parquet(FIXTURES / "nflreadpy_depth_charts_sample.parquet"),
        "ngs": {
            st: pl.read_parquet(FIXTURES / f"nflreadpy_nextgen_{st}_sample.parquet")
            for st in _NGS_TYPES
        },
        "pfr_advstats": {
            st: pl.read_parquet(FIXTURES / f"nflreadpy_pfr_advstats_{st}_sample.parquet")
            for st in _PFR_TYPES
        },
    }


def test_validate_produces_every_staged_table():
    validated = NflverseBulkCollector().validate(_load_raw())
    assert set(validated) == {
        "player_week",
        "team_week",
        "snaps",
        "ngs",
        "pfr_advstats",
        "ftn",
        "depth",
    }
    for df in validated.values():
        assert df.height > 0


def test_validate_raises_on_missing_columns():
    raw = _load_raw()
    raw["pbp"] = raw["pbp"].drop("epa")

    with pytest.raises(ValueError, match="epa"):
        NflverseBulkCollector().validate(raw)


def test_team_week_aggregation_is_internally_consistent():
    raw = _load_raw()
    team_week = NflverseBulkCollector().validate(raw)["team_week"]

    assert team_week.height == 2  # one full game, two teams, in the pbp fixture
    for row in team_week.to_dicts():
        assert row["plays"] > 0
        assert 0 <= row["success_count"] <= row["plays"]
        assert 0 <= row["explosive_count"] <= row["plays"]
        assert row["pass_plays"] + row["rush_plays"] == row["plays"]
        assert (
            row["down1_plays"] + row["down2_plays"] + row["down3_plays"] + row["down4_plays"]
            <= row["plays"]
        )
        assert row["drives"] > 0
        assert 0 <= row["three_and_out_drives"] <= row["drives"]
        assert 0 <= row["red_zone_tds"] <= row["red_zone_trips"] <= row["drives"]
        assert row["garbage_time_plays_excluded"] >= 0


def test_team_week_drops_preseason_rows():
    raw = _load_raw()
    fake_preseason = raw["pbp"].head(1).with_columns(pl.lit("PRE").alias("season_type"))
    raw["pbp"] = pl.concat([raw["pbp"], fake_preseason], how="vertical")

    team_week = NflverseBulkCollector().validate(raw)["team_week"]
    assert set(team_week["season_type"].to_list()) == {"REG"}


def test_player_week_drops_null_keys():
    player_week = _build_player_week(_load_raw()["player_stats"])
    assert player_week["player_id"].null_count() == 0
    assert player_week["game_id"].null_count() == 0


def test_ngs_stacks_stat_types_with_nulls_for_other_types():
    ngs_frames = _load_raw()["ngs"]
    ngs = _build_ngs(ngs_frames)

    assert set(ngs["stat_type"].to_list()) == set(_NGS_TYPES)
    passing_rows = ngs.filter(pl.col("stat_type") == "passing")
    assert passing_rows["completion_percentage_above_expectation"].null_count() == 0
    assert passing_rows["avg_separation"].null_count() == passing_rows.height
    assert set(ngs.columns) >= set(_NGS_ALL_METRIC_COLS)


def test_pfr_advstats_stacks_stat_types_with_nulls_for_other_types():
    pfr_frames = _load_raw()["pfr_advstats"]
    pfr = _build_pfr_advstats(pfr_frames)

    assert set(pfr["stat_type"].to_list()) == set(_PFR_TYPES)
    rush_rows = pfr.filter(pl.col("stat_type") == "rush")
    assert rush_rows["rushing_broken_tackles"].null_count() == 0
    assert rush_rows["def_pressures"].null_count() == rush_rows.height
    assert set(pfr.columns) >= set(_PFR_ALL_METRIC_COLS)


def test_snaps_derives_season_type_and_renames_opponent():
    snaps = _build_snaps(_load_raw()["snap_counts"])
    assert set(snaps["season_type"].to_list()) <= {"REG", "POST"}
    assert "opponent_team" in snaps.columns
    assert "opponent" not in snaps.columns


def test_ftn_renames_nflverse_ids():
    ftn = _build_ftn(_load_raw()["ftn_charting"])
    assert "game_id" in ftn.columns
    assert "play_id" in ftn.columns
    assert ftn["game_id"].null_count() == 0


def test_depth_keeps_only_latest_snapshot_per_team():
    depth_charts = pl.DataFrame(
        {
            "dt": [
                "2026-01-01T00:00:00Z",
                "2026-02-01T00:00:00Z",  # newer ARI snapshot
                "2026-01-15T00:00:00Z",  # only KC snapshot
            ],
            "team": ["ARI", "ARI", "KC"],
            "player_name": ["Old Starter", "New Starter", "Someone"],
            "espn_id": ["1", "2", "3"],
            "gsis_id": ["00-old", "00-new", "00-kc"],
            "pos_grp_id": ["1", "1", "1"],
            "pos_grp": ["Offense", "Offense", "Offense"],
            "pos_id": ["1", "1", "1"],
            "pos_name": ["Quarterback", "Quarterback", "Quarterback"],
            "pos_abb": ["QB", "QB", "QB"],
            "pos_slot": [1, 1, 1],
            "pos_rank": [1, 1, 1],
        }
    )

    depth = _build_depth(depth_charts)

    ari_row = depth.filter(pl.col("team") == "ARI").to_dicts()[0]
    assert ari_row["player_id"] == "00-new"
    assert ari_row["as_of"] == datetime(2026, 2, 1, tzinfo=UTC)
    assert depth.filter(pl.col("team") == "KC").height == 1
