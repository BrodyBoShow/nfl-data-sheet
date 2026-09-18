from datetime import UTC, datetime
from pathlib import Path

import polars as pl
import pytest

from pipeline.collectors.nflverse_bulk import (
    _NGS_ALL_METRIC_COLS,
    _PFR_ALL_METRIC_COLS,
    NflverseBulkCollector,
    _aggregate_team_week,
    _build_depth,
    _build_ftn,
    _build_ngs,
    _build_pfr_advstats,
    _build_player_week,
    _build_snaps,
)
from pipeline.core.team_aliases import TEAM_ABBR_ALIASES

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


def _synthetic_drive_row(
    fixed_drive: int,
    fixed_drive_result: str,
    *,
    wp: float = 0.5,
    is_pass: bool = True,
    qtr: int = 4,
    play_type: str | None = None,
    qb_kneel: int = 0,
    qb_spike: int = 0,
) -> dict:
    return {
        "game_id": "2099_01_AA_BB",
        "season": 2099,
        "week": 1,
        "season_type": "REG",
        "posteam": "AA",
        "defteam": "BB",
        "play_deleted": 0,
        "epa": 1.0,
        "success": 1,
        "pass": 1 if is_pass else 0,
        "rush": 0 if is_pass else 1,
        "yards_gained": 5,
        "down": 1,
        "qtr": qtr,
        "wp": wp,
        "play_type": play_type or ("pass" if is_pass else "run"),
        "qb_kneel": qb_kneel,
        "qb_spike": qb_spike,
        "fixed_drive": fixed_drive,
        "drive_play_count": 1,
        "fixed_drive_result": fixed_drive_result,
        "yardline_100": 10,
    }


def test_points_maps_drive_results_and_excludes_garbage_time():
    pbp = pl.DataFrame(
        [
            _synthetic_drive_row(1, "Touchdown"),
            _synthetic_drive_row(2, "Field goal"),
            _synthetic_drive_row(3, "Touchdown", wp=0.97),  # garbage time, excluded
            _synthetic_drive_row(4, "Safety"),
            _synthetic_drive_row(5, "Opp touchdown"),
            _synthetic_drive_row(6, "Punt"),
        ]
    )
    team_week = _aggregate_team_week(pbp)
    row = team_week.filter(pl.col("team") == "AA").to_dicts()[0]
    # Touchdown (6) + Field goal (3); garbage-time TD, Safety, Opp touchdown, and Punt
    # all score 0 for this team's offense.
    assert row["points"] == 9


def test_points_defaults_unrecognized_drive_result_to_zero():
    pbp = pl.DataFrame([_synthetic_drive_row(1, "Some future result nflverse hasn't used yet")])
    team_week = _aggregate_team_week(pbp)
    assert team_week.filter(pl.col("team") == "AA").to_dicts()[0]["points"] == 0


def test_no_play_penalty_row_excluded_from_plays_despite_pass_flag():
    # A penalty no-play still carries the original called play's pass=1 and a non-null
    # epa -- verified live, this leaked 1,622 rows into "plays" across 2025-2026 before
    # this fix (docs/sources.md).
    pbp = pl.DataFrame(
        [
            _synthetic_drive_row(1, "Touchdown", play_type="no_play"),
            _synthetic_drive_row(2, "Field goal"),  # a real pass, play_type="pass"
        ]
    )
    team_week = _aggregate_team_week(pbp)
    row = team_week.filter(pl.col("team") == "AA").to_dicts()[0]
    assert row["plays"] == 1


def test_qb_kneel_and_spike_excluded_even_if_pass_rush_flag_ever_lets_them_through():
    pbp = pl.DataFrame(
        [
            _synthetic_drive_row(1, "Touchdown", qb_kneel=1),
            _synthetic_drive_row(2, "Field goal", qb_spike=1),
            _synthetic_drive_row(3, "Safety"),  # the one real play
        ]
    )
    team_week = _aggregate_team_week(pbp)
    row = team_week.filter(pl.col("team") == "AA").to_dicts()[0]
    assert row["plays"] == 1


def test_garbage_time_q1_q2_never_excluded_even_at_extreme_win_probability():
    pbp = pl.DataFrame(
        [
            _synthetic_drive_row(1, "Touchdown", wp=0.99, qtr=1),
            _synthetic_drive_row(2, "Field goal", wp=0.01, qtr=2),
        ]
    )
    team_week = _aggregate_team_week(pbp)
    row = team_week.filter(pl.col("team") == "AA").to_dicts()[0]
    assert row["plays"] == 2
    assert row["garbage_time_plays_excluded"] == 0


def test_garbage_time_q3_uses_tighter_band_than_q4():
    pbp = pl.DataFrame(
        [
            # Inside the old 0.05/0.95 band but outside Q3's tighter 0.02/0.98 -- kept.
            _synthetic_drive_row(1, "Touchdown", wp=0.97, qtr=3),
            # Beyond Q3's tighter band -- still excluded.
            _synthetic_drive_row(2, "Field goal", wp=0.99, qtr=3),
        ]
    )
    team_week = _aggregate_team_week(pbp)
    row = team_week.filter(pl.col("team") == "AA").to_dicts()[0]
    assert row["plays"] == 1
    assert row["garbage_time_plays_excluded"] == 1


def test_garbage_time_q4_and_ot_keep_the_original_band():
    pbp = pl.DataFrame(
        [
            _synthetic_drive_row(1, "Touchdown", wp=0.5, qtr=4),  # kept, so team has a row
            _synthetic_drive_row(2, "Field goal", wp=0.97, qtr=4),  # excluded, as before
            _synthetic_drive_row(3, "Safety", wp=0.97, qtr=5),  # OT, excluded too
        ]
    )
    team_week = _aggregate_team_week(pbp)
    row = team_week.filter(pl.col("team") == "AA").to_dicts()[0]
    assert row["plays"] == 1
    assert row["garbage_time_plays_excluded"] == 2


@pytest.mark.parametrize(("old_code", "current_code"), sorted(TEAM_ABBR_ALIASES.items()))
def test_snaps_normalizes_retired_team_codes(old_code, current_code):
    """Every code in TEAM_ABBR_ALIASES that differs from nflverse's current
    abbreviation must be normalized on both the team and opponent_team columns."""
    snap_counts = pl.DataFrame(
        {
            "game_type": ["REG"],
            "game_id": [f"2019_01_{old_code}_DEN"],
            "pfr_player_id": ["SomeGuy00"],
            "season": [2019],
            "week": [1],
            "team": [old_code],
            "opponent": [old_code],
            "position": ["QB"],
            "offense_snaps": [60],
            "offense_pct": [1.0],
            "defense_snaps": [0],
            "defense_pct": [0.0],
            "st_snaps": [0],
            "st_pct": [0.0],
        }
    )
    snaps = _build_snaps(snap_counts)
    row = snaps.to_dicts()[0]
    assert row["team"] == current_code
    assert row["opponent_team"] == current_code


def test_team_abbr_aliases_covers_expected_codes():
    """Guards against drift on the shared alias table (pipeline/core/team_aliases.py) --
    three retired-franchise PFR codes plus ESPN's WSH quirk, not a second copy per
    collector (see pipeline/collectors/availability.py, which also imports this)."""
    assert TEAM_ABBR_ALIASES == {
        "OAK": "LV",
        "SD": "LAC",
        "STL": "LA",
        "WSH": "WAS",
    }


def test_datasets_scoping_limits_fetch_validate_store():
    collector = NflverseBulkCollector(datasets={"team_week"})
    raw = _load_raw()
    # Only pass through the raw source team_week actually needs, mirroring what a
    # --datasets-scoped fetch() would produce.
    validated = collector.validate({"pbp": raw["pbp"]})
    assert set(validated) == {"team_week"}


def test_datasets_rejects_unknown_name():
    with pytest.raises(ValueError, match="unknown dataset"):
        NflverseBulkCollector(datasets={"not_a_real_table"})


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
