import re
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import polars as pl
import pytest
from polars.testing import assert_frame_equal

from pipeline.collectors import nflverse_bulk
from pipeline.collectors.nflverse_bulk import (
    _FTN_COLS,
    _NGS_ALL_METRIC_COLS,
    _PARTICIPATION_COLS,
    _PFR_ALL_METRIC_COLS,
    _PGP_PASSER_COLS,
    _PGP_PASSER_FTN_COLS,
    _PGP_RECEIVER_COLS,
    _PGP_RECEIVER_FTN_COLS,
    _PGP_RUSHER_COLS,
    _PGP_RUSHER_FTN_COLS,
    _PLAYER_GAME_PBP_COLS,
    _PLAYER_WEEK_COLS,
    NflverseBulkCollector,
    _aggregate_participation_player_season,
    _aggregate_player_game_pbp,
    _aggregate_team_week,
    _build_depth,
    _build_ftn,
    _build_ngs,
    _build_pfr_advstats,
    _build_player_week,
    _build_snaps,
    _garbage_time_expr,
    _player_play_scope,
)
from pipeline.core.base import RunContext, WorkResult
from pipeline.core.db import ChangedUpsert
from pipeline.core.team_aliases import TEAM_ABBR_ALIASES

FIXTURES = Path(__file__).parent / "fixtures"
MIGRATIONS = Path(__file__).parent.parent / "db" / "migrations"

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
        "participation": pl.read_parquet(FIXTURES / "nflreadpy_participation_sample.parquet"),
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
        "player_game_pbp",
        "participation_player_season",
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
    # Counts stay ints across the stat_type concat (Int32 at source), not floats.
    for count_col in ("attempts", "rush_attempts", "targets"):
        assert ngs.schema[count_col] == pl.Int64
    assert passing_rows["attempts"].null_count() == 0
    assert passing_rows["targets"].null_count() == passing_rows.height


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
    two_point_attempt: int | None = 0,
    extra_point_attempt: int | None = 0,
    yardline_100: int = 10,
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
        "two_point_attempt": two_point_attempt,
        "extra_point_attempt": extra_point_attempt,
        "fixed_drive": fixed_drive,
        "drive_play_count": 1,
        "fixed_drive_result": fixed_drive_result,
        "yardline_100": yardline_100,
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


def test_two_point_try_excluded_from_plays_like_player_game_pbp():
    # A two-point try carries pass/rush flags and a non-null epa (all 130 in 2025 did),
    # so it reached "plays" until 2026-09-30 (P2 open item 1).
    pbp = pl.DataFrame(
        [
            _synthetic_drive_row(1, "Touchdown", yardline_100=30),
            _synthetic_drive_row(1, "Touchdown", two_point_attempt=1, yardline_100=2),
        ]
    )
    row = _aggregate_team_week(pbp).filter(pl.col("team") == "AA").to_dicts()[0]
    assert row["plays"] == 1
    assert row["pass_plays"] == 1
    assert row["garbage_time_plays_excluded"] == 0


def test_try_plays_dont_make_a_long_td_drive_a_red_zone_trip():
    # The try sits inside the 20 (PATs mostly at the 15, two-point tries mostly at the 2).
    # Counting it set closest_yardline, so TD drives read as red-zone trips and TDs: 2025
    # REG 1,962 trips vs 1,636.
    pat = {"is_pass": False, "extra_point_attempt": 1, "yardline_100": 15}
    pbp = pl.DataFrame(
        [
            _synthetic_drive_row(1, "Touchdown", yardline_100=60),
            {**_synthetic_drive_row(1, "Touchdown", **pat), "rush": 0, "play_type": "extra_point"},
            _synthetic_drive_row(2, "Touchdown", yardline_100=60),
            _synthetic_drive_row(2, "Touchdown", two_point_attempt=1, yardline_100=2),
        ]
    )
    row = _aggregate_team_week(pbp).filter(pl.col("team") == "AA").to_dicts()[0]
    assert row["drives"] == 2
    assert row["red_zone_trips"] == 0
    assert row["red_zone_tds"] == 0
    assert row["points"] == 12


def test_a_try_alone_under_a_fixed_drive_is_not_a_drive():
    # After a return TD the try can sit under a fixed_drive with no other play by that
    # posteam: a phantom drive (65 in 2025, 1.2% of points_per_drive's denominator).
    pbp = pl.DataFrame(
        [
            _synthetic_drive_row(1, "Touchdown", yardline_100=60),
            {
                **_synthetic_drive_row(2, "Opp touchdown", is_pass=False, extra_point_attempt=1),
                "rush": 0,
                "play_type": "extra_point",
            },
        ]
    )
    row = _aggregate_team_week(pbp).filter(pl.col("team") == "AA").to_dicts()[0]
    assert row["drives"] == 1


def test_null_try_flags_keep_marker_rows_in_drives():
    # GAME/END QUARTER/no_play marker rows have null try flags (1,511 in 2025). The drive
    # filter keeps them, as it always did; only a flag of 1 marks a try.
    marker = {
        **_synthetic_drive_row(2, "Punt", two_point_attempt=None, extra_point_attempt=None),
        "pass": 0,
        "play_type": None,
    }
    pbp = pl.DataFrame([_synthetic_drive_row(1, "Touchdown"), marker])
    row = _aggregate_team_week(pbp).filter(pl.col("team") == "AA").to_dicts()[0]
    assert row["drives"] == 2
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


# --- P7: migration contract (0027-0030) ----------------------------------------------------
# The migrations define exactly what the collector must produce, so these parse the SQL
# rather than restating the column lists.

_SQL_TO_POLARS = {"int": {pl.Int32, pl.Int64}, "double": {pl.Float64}, "text": {pl.String}}
_BOOKKEEPING_COLS = {"content_hash", "updated_at"}


def _sql_without_comments(name: str) -> str:
    (path,) = MIGRATIONS.glob(f"{name}_*.sql")
    return "\n".join(line.split("--")[0] for line in path.read_text().splitlines())


def _created_columns(name: str) -> dict[str, str]:
    sql = _sql_without_comments(name)
    body = sql[sql.index("(", sql.index("CREATE TABLE")) + 1 : sql.index("PRIMARY KEY")]
    cols = {}
    for line in body.splitlines():
        parts = line.strip().rstrip(",").split()
        if parts:
            cols[parts[0]] = parts[1]
    return {c: t for c, t in cols.items() if c not in _BOOKKEEPING_COLS}


def _added_columns(name: str) -> dict[str, dict[str, str]]:
    sql = _sql_without_comments(name)
    return {
        table: dict(re.findall(r"ADD COLUMN (\w+) (\w+)", block))
        for table, block in re.findall(r"ALTER TABLE (\w+)(.*?);", sql, re.S)
    }


def _assert_dtypes_match(df: pl.DataFrame, sql_cols: dict[str, str]) -> None:
    wrong = {
        c: (t, df.schema[c]) for c, t in sql_cols.items() if df.schema[c] not in _SQL_TO_POLARS[t]
    }
    assert not wrong, wrong


def test_player_game_pbp_columns_are_exactly_migration_0028():
    sql_cols = _created_columns("0028")
    assert set(_PLAYER_GAME_PBP_COLS) == set(sql_cols)
    assert len(_PLAYER_GAME_PBP_COLS) == len(set(_PLAYER_GAME_PBP_COLS))
    df, _ = _aggregate_player_game_pbp(_pbp(), _ftn())
    assert df.columns == _PLAYER_GAME_PBP_COLS
    _assert_dtypes_match(df, sql_cols)


def test_participation_player_season_columns_are_exactly_migration_0029():
    sql_cols = _created_columns("0029")
    assert set(_PARTICIPATION_COLS) == set(sql_cols)
    df, _ = _aggregate_participation_player_season(_participation(), _pbp())
    assert df.columns == _PARTICIPATION_COLS
    _assert_dtypes_match(df, sql_cols)


def test_widened_columns_are_staged_per_migrations_0027_and_0030():
    added = _added_columns("0027") | _added_columns("0030")
    raw = _load_raw()
    built = {
        "ngs": _build_ngs(raw["ngs"]),
        "pfr_advstats": _build_pfr_advstats(raw["pfr_advstats"]),
        "ftn": _build_ftn(raw["ftn_charting"]),
        "player_week": _build_player_week(raw["player_stats"]),
    }
    staged = {
        "ngs": set(_NGS_ALL_METRIC_COLS),
        "pfr_advstats": set(_PFR_ALL_METRIC_COLS),
        "ftn": set(_FTN_COLS),
        "player_week": set(_PLAYER_WEEK_COLS),
    }
    assert set(added) == set(built)
    for table, cols in added.items():
        assert set(cols) <= staged[table], (table, set(cols) - staged[table])
        # FTN booleans are `boolean` in SQL; everything else maps through _SQL_TO_POLARS.
        typed = {c: t for c, t in cols.items() if t != "boolean"}
        _assert_dtypes_match(built[table], typed)
        for c in set(cols) - set(typed):
            assert built[table].schema[c] == pl.Boolean, c


# --- P7: player_game_pbp -------------------------------------------------------------------


def _pbp() -> pl.DataFrame:
    return pl.read_parquet(FIXTURES / "nflreadpy_pbp_sample.parquet")


def _ftn() -> pl.DataFrame:
    return pl.read_parquet(FIXTURES / "nflreadpy_ftn_charting_sample.parquet")


def _participation() -> pl.DataFrame:
    return pl.read_parquet(FIXTURES / "nflreadpy_participation_sample.parquet")


def _pgp(pbp: pl.DataFrame | None = None, ftn: pl.DataFrame | None = None) -> pl.DataFrame:
    return _aggregate_player_game_pbp(
        _pbp() if pbp is None else pbp, _ftn() if ftn is None else ftn
    )[0]


def _role_totals(df: pl.DataFrame) -> tuple[int, int, int]:
    row = df.select(pl.col("dropbacks").sum(), pl.col("carries").sum(), pl.col("targets").sum())
    return row.row(0)


def test_player_game_pbp_play_scope_matches_the_fixtures_measured_counts():
    # Independent anchors (docs/phases/P7.md step 3, fixture gap): the game has 128 in-scope
    # scrimmage plays, every one either a dropback (84) or a designed run (44); of 73 non-sack
    # pass attempts, 3 have no receiver, leaving 70 targets.
    assert _role_totals(_pgp()) == (84, 44, 70)


def _clone_play(pbp: pl.DataFrame, play_id: float, new_id: float, change: dict) -> pl.DataFrame:
    return pbp.filter(pl.col("play_id") == play_id).with_columns(
        pl.lit(new_id, dtype=pl.Float64).alias("play_id"),
        *[pl.lit(v, dtype=pbp.schema[k]).alias(k) for k, v in change.items()],
    )


_EXCLUDED_PLAYS = {
    "two_point_attempt": {"two_point_attempt": 1.0},
    "qb_kneel": {"qb_kneel": 1.0},
    "qb_spike": {"qb_spike": 1.0},
    "no_play": {"play_type": "no_play"},
    "play_deleted": {"play_deleted": 1.0},
    "null_epa": {"epa": None},
}


@pytest.mark.parametrize("change", _EXCLUDED_PLAYS.values(), ids=_EXCLUDED_PLAYS.keys())
def test_player_game_pbp_excludes_non_scrimmage_plays(change):
    """Each clone is a real in-scope play (a completed target and a designed run) with
    exactly one flag changed, so only that flag can be what keeps it out."""
    pbp = _pbp()
    scoped = _player_play_scope(pbp)
    target_play = scoped.filter(
        (pl.col("complete_pass") == 1) & pl.col("receiver_id").is_not_null()
    )["play_id"][0]
    run_play = scoped.filter(pl.col("rush") == 1)["play_id"][0]
    baseline = _pgp(pbp)

    def with_clones(change: dict) -> pl.DataFrame:
        clones = [
            _clone_play(pbp, target_play, 90001.0, change),
            _clone_play(pbp, run_play, 90002.0, change),
        ]
        return _pgp(pl.concat([pbp, *clones], how="vertical"))

    # Control: unchanged clones ARE counted, so the clone mechanism itself can't be what
    # makes the excluded version disappear.
    assert _role_totals(with_clones({})) == (85, 45, 71)
    assert_frame_equal(with_clones(change), baseline)


def test_player_game_pbp_includes_garbage_time():
    pbp = _pbp()
    garbage = _player_play_scope(pbp).filter(_garbage_time_expr().fill_null(False))
    assert garbage.height == 6  # real Q3/Q4 plays in the fixture game

    without = pbp.join(garbage.select("play_id"), on="play_id", how="anti")
    full, less = _role_totals(_pgp(pbp)), _role_totals(_pgp(without))
    assert (full[0] + full[1]) - (less[0] + less[1]) == 6

    # Cross-check against the scope that does exclude it: team_week drops the same 6.
    assert _aggregate_team_week(pbp)["garbage_time_plays_excluded"].sum() == 6


_ROLE_COLS = {
    "passer": _PGP_PASSER_COLS + _PGP_PASSER_FTN_COLS,
    "rusher": _PGP_RUSHER_COLS + _PGP_RUSHER_FTN_COLS,
    "receiver": _PGP_RECEIVER_COLS + _PGP_RECEIVER_FTN_COLS,
}


def test_player_game_pbp_absent_roles_are_null_not_zero():
    df = _pgp()
    for row in df.to_dicts():
        held = {role: row[cols[0]] is not None for role, cols in _ROLE_COLS.items()}
        assert any(held.values()), row["player_id"]
        for role, cols in _ROLE_COLS.items():
            values = [row[c] for c in cols]
            if held[role]:
                # The fixture game is FTN-charted, so a held role has every column.
                assert None not in values, (row["player_id"], role)
            else:
                assert values == [None] * len(values), (row["player_id"], role)

    # A pure receiver: every passer and rusher column NULL.
    receiver = df.filter(pl.col("player_id") == "00-0031236").to_dicts()[0]
    assert receiver["targets"] == 4
    assert all(receiver[c] is None for c in _ROLE_COLS["passer"] + _ROLE_COLS["rusher"])
    # A QB who was never targeted: receiver columns NULL, while a held role's zero is 0.
    qb = df.filter(pl.col("player_id") == "00-0035228").to_dicts()[0]
    assert all(qb[c] is None for c in _ROLE_COLS["receiver"])
    assert qb["interceptions"] == 0
    # Scrambles are dropbacks, never carries (rusher_id is null on them).
    assert qb["scrambles"] == 3
    assert qb["carries"] == 4


def test_play_id_join_rejects_a_fractional_pbp_play_id():
    pbp = _pbp().with_columns(
        pl.when(pl.col("play_id") == 115.0)
        .then(115.5)
        .otherwise(pl.col("play_id"))
        .alias("play_id")
    )
    with pytest.raises(ValueError, match="non-integer"):
        _aggregate_player_game_pbp(pbp, _ftn())


def test_play_id_join_rejects_an_unexpected_ftn_play_id_dtype():
    ftn = _ftn().with_columns(pl.col("nflverse_play_id").cast(pl.String))
    with pytest.raises(ValueError, match="dtype"):
        _aggregate_player_game_pbp(_pbp(), ftn)


def test_play_id_join_rejects_duplicated_ftn_plays():
    ftn = pl.concat([_ftn(), _ftn().head(1)], how="vertical")
    with pytest.raises(ValueError, match="duplicated"):
        _aggregate_player_game_pbp(_pbp(), ftn)


def test_player_game_pbp_ignores_play_id_dtype_differences():
    baseline = _pgp()
    as_int_pbp = _pgp(pbp=_pbp().with_columns(pl.col("play_id").cast(pl.Int32)))
    as_float_ftn = _pgp(ftn=_ftn().with_columns(pl.col("nflverse_play_id").cast(pl.Float64)))
    assert_frame_equal(as_int_pbp, baseline)
    assert_frame_equal(as_float_ftn, baseline)


_FTN_COLS_ALL_ROLES = _PGP_PASSER_FTN_COLS + _PGP_RUSHER_FTN_COLS + _PGP_RECEIVER_FTN_COLS


def test_uncharted_game_gets_null_ftn_columns_and_full_pbp_columns():
    """pbp fresh, FTN not caught up: the game's pbp columns are written and every ftn_*
    column is NULL -- never 0, which would read as 'charted, no play action'."""
    baseline = _pgp()
    df, meta = _aggregate_player_game_pbp(_pbp(), _ftn().head(0))
    pbp_cols = [c for c in _PLAYER_GAME_PBP_COLS if c not in _FTN_COLS_ALL_ROLES]
    assert_frame_equal(df.select(pbp_cols), baseline.select(pbp_cols))
    assert df.select(_FTN_COLS_ALL_ROLES).null_count().sum_horizontal().item() == (
        df.height * len(_FTN_COLS_ALL_ROLES)
    )
    assert meta["pgp_ftn_uncharted_games"] == 1
    assert meta["pgp_ftn_unmatched_plays"] == 0


def test_partially_charted_game_uses_charted_plays_as_the_ftn_denominator():
    pbp = _pbp()
    dropped = _player_play_scope(pbp)["play_id"].head(20).cast(pl.Int32)
    ftn = _ftn().filter(~pl.col("nflverse_play_id").is_in(dropped.implode()))
    df, meta = _aggregate_player_game_pbp(pbp, ftn)

    assert _role_totals(df) == (84, 44, 70)  # pbp columns unaffected
    charted = df.select(
        pl.col("ftn_charted_dropbacks").sum() + pl.col("ftn_charted_carries").sum()
    ).item()
    assert charted == 128 - 20
    assert df.filter(pl.col("ftn_charted_dropbacks") > pl.col("dropbacks")).height == 0
    assert meta["pgp_ftn_unmatched_plays"] == 20
    assert meta["pgp_ftn_unmatched_example_games"] == ["2025_01_ARI_NO"]
    assert meta["pgp_ftn_uncharted_games"] == 0


# --- P7: participation_player_season -------------------------------------------------------


def _pps(participation: pl.DataFrame) -> tuple[pl.DataFrame, dict]:
    return _aggregate_participation_player_season(participation, _pbp())


def _labeled_pass_dropbacks(df: pl.DataFrame) -> int:
    return df.select(
        pl.col("pass_dropbacks_man").sum() + pl.col("pass_dropbacks_zone").sum()
    ).item()


def _relabel(participation: pl.DataFrame, play_ids: pl.Series, label: str | None) -> pl.DataFrame:
    return participation.with_columns(
        pl.when(pl.col("play_id").is_in(play_ids.implode()))
        .then(pl.lit(label, dtype=pl.String))
        .otherwise(pl.col("defense_man_zone_type"))
        .alias("defense_man_zone_type")
    )


def _dropback_play_ids(n: int) -> pl.Series:
    return _player_play_scope(_pbp()).filter(pl.col("qb_dropback") == 1)["play_id"].head(n)


def test_participation_counts_on_field_plays_and_labeled_dropbacks():
    df, meta = _pps(_participation())
    totals = df.select(pl.all().exclude("player_id", "season").sum()).to_dicts()[0]
    assert totals["off_snaps"] == 128 * 11  # every in-scope play, 11 on offense
    assert totals["off_dropbacks"] == 84 * 11
    assert _labeled_pass_dropbacks(df) == 84  # all 84 fixture dropbacks are labeled
    assert totals["targets_man"] + totals["targets_zone"] == 70
    assert meta["participation_rows_without_pbp"] == 0
    assert meta["participation_scope_plays_without_participation"] == 0


def test_participation_empty_string_and_null_labels_are_unlabeled_not_coverage():
    baseline, _ = _pps(_participation())
    plays = _dropback_play_ids(10)
    as_empty, _ = _pps(_relabel(_participation(), plays, ""))  # the 2025 form
    as_null, _ = _pps(_relabel(_participation(), plays, None))  # the 2022 form

    assert _labeled_pass_dropbacks(as_empty) == _labeled_pass_dropbacks(baseline) - 10
    assert as_empty["off_dropbacks"].sum() == baseline["off_dropbacks"].sum()
    assert_frame_equal(as_empty, as_null)


def test_participation_unknown_label_counts_as_neither_and_is_reported():
    baseline, _ = _pps(_participation())
    df, meta = _pps(_relabel(_participation(), _dropback_play_ids(5), "COVER_X"))
    assert _labeled_pass_dropbacks(df) == _labeled_pass_dropbacks(baseline) - 5
    assert meta["participation_unknown_coverage_plays"] == 5
    assert meta["participation_unknown_coverage_labels"] == ["COVER_X"]


def test_participation_empty_player_list_yields_no_blank_player_id():
    # 2022 has plays with offense_players == '' (n_offense 0), which split to [''].
    baseline, _ = _pps(_participation())
    play = _dropback_play_ids(1)
    participation = _participation().with_columns(
        pl.when(pl.col("play_id").is_in(play.implode()))
        .then(pl.lit(""))
        .otherwise(pl.col("offense_players"))
        .alias("offense_players")
    )
    df, _ = _pps(participation)
    assert df.filter(pl.col("player_id") == "").height == 0
    assert df["off_snaps"].sum() == baseline["off_snaps"].sum() - 11


def test_participation_does_not_read_route_or_was_pressure():
    baseline, _ = _pps(_participation())
    df, _ = _pps(_participation().drop("route", "was_pressure"))
    assert_frame_equal(df, baseline)


def test_participation_handles_the_2022_int32_play_id():
    baseline, _ = _pps(_participation())
    as_int, _ = _pps(_participation().with_columns(pl.col("play_id").cast(pl.Int32)))
    assert_frame_equal(as_int, baseline)
    # The real 2022 fixture (different schema, a game not in the pbp fixture) goes through
    # the same path; none of its rows match pbp, and that's counted, not guessed.
    participation_2022 = pl.read_parquet(FIXTURES / "nflreadpy_participation_2022_sample.parquet")
    df, meta = _pps(participation_2022)
    assert df.height == 0
    assert meta["participation_rows_without_pbp"] == participation_2022.height


# --- P7: two-tag freshness ------------------------------------------------------------------


class _FakeNflverse:
    """Stands in for the network (timestamp.json, nflreadpy) and the DB (source_freshness,
    upserts). Loaders return the fixtures whatever seasons are asked for, and record the
    call so a test can see what was fetched."""

    def __init__(self, monkeypatch, live: dict[str, str]):
        self.live = dict(live)
        self.freshness: dict[str, str] = {}
        self.calls: list[tuple[str, list[int] | int]] = []
        self.upserted: dict[str, int] = {}
        # upsert_changed: rows offered per table, how many leading rows to treat as
        # unchanged (not written), and how many duplicate-key rows to report dropped, per
        # table. Nothing is unchanged or duplicated unless a test says so.
        self.offered: dict[str, int] = {}
        self.unchanged: dict[str, int] = {}
        self.duplicates: dict[str, int] = {}
        self.result: WorkResult | None = None
        raw = _load_raw()

        def loader(name: str, frame):
            def load(seasons, stat_type=None):
                self.calls.append((name, seasons))
                return frame[stat_type] if stat_type else frame

            return load

        fake_nfl = SimpleNamespace(
            load_pbp=loader("load_pbp", raw["pbp"]),
            load_player_stats=loader("load_player_stats", raw["player_stats"]),
            load_snap_counts=loader("load_snap_counts", raw["snap_counts"]),
            load_ftn_charting=loader("load_ftn_charting", raw["ftn_charting"]),
            load_depth_charts=loader("load_depth_charts", raw["depth_charts"]),
            load_nextgen_stats=loader("load_nextgen_stats", raw["ngs"]),
            load_pfr_advstats=loader("load_pfr_advstats", raw["pfr_advstats"]),
            load_participation=loader("load_participation", raw["participation"]),
        )

        def upsert_changed(conn, table, rows, pk_cols, update_cols):
            # Stands in for the server-side diff: the first `unchanged[table]` rows match
            # what's stored, the rest are written.
            rows = list(rows)
            self.offered[table] = self.offered.get(table, 0) + len(rows)
            changed = len(rows[self.unchanged.get(table, 0) :])
            self.upserted[table] = self.upserted.get(table, 0) + changed
            return ChangedUpsert(
                inserted=changed, updated=0, duplicates_dropped=self.duplicates.get(table, 0)
            )

        monkeypatch.setattr(nflverse_bulk, "nfl", fake_nfl)
        monkeypatch.setattr(nflverse_bulk, "_fetch_timestamp", lambda tag: self.live[tag])
        monkeypatch.setattr(nflverse_bulk, "get_last_value", lambda c, k: self.freshness.get(k))
        monkeypatch.setattr(
            nflverse_bulk, "set_last_value", lambda c, k, v: self.freshness.__setitem__(k, v)
        )

        monkeypatch.setattr(nflverse_bulk, "upsert_changed", upsert_changed)
        monkeypatch.setattr(nflverse_bulk, "_resolve_pfr_player_ids", lambda c, ids: {})

    def fetched(self, name: str) -> list:
        return [seasons for called, seasons in self.calls if called == name]

    def cycle(self, collector: NflverseBulkCollector, season: int = 2026) -> set[str] | None:
        """One run as Collector.run drives it. Returns what was stored, or None if skipped."""
        self.calls.clear()
        self.upserted.clear()
        self.offered.clear()
        self.result = None
        ctx = RunContext(
            season=season,
            week=3,
            season_type="REG",
            now=datetime(2026, 9, 27, tzinfo=UTC),
            settings=None,  # type: ignore[arg-type]
            conn=None,  # type: ignore[arg-type]
        )
        if not collector.should_run(ctx):
            return None
        validated = collector.validate(collector.fetch(ctx))
        self.result = collector.store(ctx, validated)
        return set(validated)


_TAGS = (
    "pbp",
    "stats_player",
    "snap_counts",
    "ftn_charting",
    "depth_charts",
    "nextgen_stats",
    "pfr_advstats",
    "pbp_participation",
)
_CORE = {
    "team_week",
    "player_week",
    "snaps",
    "ftn",
    "depth",
    "ngs",
    "pfr_advstats",
    "player_game_pbp",
}


@pytest.fixture
def fake(monkeypatch) -> _FakeNflverse:
    fake = _FakeNflverse(monkeypatch, {tag: "t1" for tag in _TAGS})
    assert fake.cycle(NflverseBulkCollector()) == _CORE | {"participation_player_season"}
    return fake


def test_freshness_first_run_records_tag_keys_and_built_from_keys(fake):
    for tag in _TAGS:
        assert fake.freshness[f"nflverse:{tag}"] == "t1"
    assert fake.freshness["nflverse:player_game_pbp"] == "pbp@t1;ftn_charting@t1;seasons=2025,2026"
    assert (
        fake.freshness["nflverse:participation_player_season"]
        == "pbp_participation@t1;seasons=2025"
    )
    assert fake.cycle(NflverseBulkCollector()) is None  # nothing moved: skipped


@pytest.mark.parametrize("moved", ["ftn_charting", "pbp"])
def test_freshness_either_player_game_pbp_tag_rebuilds_it_from_both(fake, moved):
    fake.live[moved] = "t2"
    stored = fake.cycle(NflverseBulkCollector())
    assert stored == _CORE  # participation's own tag didn't move
    assert fake.fetched("load_pbp") == [[2025, 2026]]
    assert fake.fetched("load_ftn_charting") == [[2025, 2026]]  # both, fresh, same run
    assert fake.fetched("load_participation") == []
    pbp_ts, ftn_ts = ("t2", "t1") if moved == "pbp" else ("t1", "t2")
    assert fake.freshness["nflverse:player_game_pbp"] == (
        f"pbp@{pbp_ts};ftn_charting@{ftn_ts};seasons=2025,2026"
    )


def test_freshness_scoped_team_week_run_cannot_hide_new_pbp_from_player_game_pbp(fake):
    fake.live["pbp"] = "t2"
    assert fake.cycle(NflverseBulkCollector(datasets={"team_week"})) == {"team_week"}
    assert fake.freshness["nflverse:pbp"] == "t2"  # team_week owns the tag key

    # Every tag key now matches live, but player_game_pbp was built from pbp@t1.
    stored = fake.cycle(NflverseBulkCollector())
    assert stored is not None and "player_game_pbp" in stored
    assert fake.freshness["nflverse:player_game_pbp"].startswith("pbp@t2;")


def test_freshness_scoped_player_game_pbp_run_leaves_the_owners_stale(fake):
    fake.live["pbp"] = "t2"
    fake.live["ftn_charting"] = "t2"
    stored = fake.cycle(NflverseBulkCollector(datasets={"player_game_pbp"}))
    assert stored == {"player_game_pbp"}
    assert fake.freshness["nflverse:pbp"] == "t1"
    assert fake.freshness["nflverse:ftn_charting"] == "t1"

    stored = fake.cycle(NflverseBulkCollector())
    assert stored is not None and {"team_week", "ftn"} <= stored


def test_freshness_participation_alone_fetches_season_minus_one_pbp_only(fake):
    fake.live["pbp_participation"] = "t2"
    stored = fake.cycle(NflverseBulkCollector())
    assert stored == {"participation_player_season"}
    assert fake.fetched("load_participation") == [[2025]]
    assert fake.fetched("load_pbp") == [[2025]]
    assert fake.fetched("load_ftn_charting") == []
    assert set(fake.upserted) == {"participation_player_season"}  # team_week not rebuilt


def test_freshness_season_rollover_refetches_participation_without_a_tag_change(fake):
    stored = fake.cycle(NflverseBulkCollector(), season=2027)
    assert stored is not None and "participation_player_season" in stored
    assert fake.fetched("load_participation") == [[2026]]
    assert (
        fake.freshness["nflverse:participation_player_season"]
        == "pbp_participation@t1;seasons=2026"
    )


@pytest.mark.parametrize(
    ("dataset", "override", "loader", "backfill_key", "live_key", "live_seasons"),
    [
        (
            "participation_player_season",
            [2023, 2024],
            "load_participation",
            "pbp_participation@t1;seasons=2023,2024",
            "pbp_participation@t1;seasons=2025",
            [2025],
        ),
        (
            "player_game_pbp",
            [2018],
            "load_pbp",
            "pbp@t1;ftn_charting@t1;seasons=2018",
            "pbp@t1;ftn_charting@t1;seasons=2025,2026",
            [2025, 2026],
        ),
    ],
)
def test_freshness_seasons_run_without_force_cannot_stop_a_built_from_tables_refresh(
    fake, dataset, override, loader, backfill_key, live_key, live_seasons
):
    """The two _BUILT_FROM_GATED tables are exempt from P7 open item 6's trap. Item 6: a
    `--seasons` run without `--force` on a table gated on a season-blind tag-only key
    (`nflverse:{tag}`) is skipped_fresh when the tag hasn't moved. When it has moved, the run
    advances the key, and the scheduled run then skips the live seasons silently.

    These two tables' keys name the seasons they were built from. So a scoped `--seasons` run
    without `--force` still runs, moves only its own key, and the next scheduled run sees the
    seasons differ and rebuilds the live seasons. P7 item 11's backfill
    (`--datasets participation_player_season --seasons 2023-2024`) relies on this.

    Pinned because making these keys season-blind looks like a harmless simplification, and
    it would silently reintroduce item 6's hazard here.

    Break demonstration (2026-10-07): with `_built_from` returning only the tag part (no
    `;seasons=`), both cases fail at the first assert. The backfill is skipped_fresh
    (`cycle` returns None), which is item 6's trap.
    """
    before = dict(fake.freshness)

    # Tag unchanged: a tag-only-keyed table would be skipped_fresh here. This one runs.
    assert fake.cycle(NflverseBulkCollector(seasons_override=override, datasets={dataset})) == {
        dataset
    }
    assert fake.fetched(loader) == [override]
    assert fake.freshness[f"nflverse:{dataset}"] == backfill_key
    # Only its own built-from key moved. pbp is loaded, but team_week owns nflverse:pbp.
    assert {k for k in before if fake.freshness[k] != before[k]} == {f"nflverse:{dataset}"}

    # The next scheduled run isn't stopped: the seasons differ, so the live seasons rebuild.
    stored = fake.cycle(NflverseBulkCollector())
    assert stored is not None and dataset in stored
    assert fake.fetched(loader) == [live_seasons]
    assert fake.freshness[f"nflverse:{dataset}"] == live_key
    assert fake.cycle(NflverseBulkCollector()) is None  # and then it settles


def test_freshness_participation_seasons_run_is_gated_on_its_built_from_key_not_its_tag(fake):
    """Item 6's mechanism does reach participation's tag key. When the tag has moved, a
    `--seasons 2023-2024` run advances `nflverse:pbp_participation` to live. The gate reads
    the built-from key, which still names 2023,2024, so the scheduled run refetches 2025
    anyway.

    Break demonstration (2026-10-07): with the same season-blind `_built_from`, the backfill
    still runs, because the tag moved. But the scheduled run after it is skipped (`stored` is
    None), and 2025 is never refetched. That's item 6's trap in its moved-tag form.
    """
    fake.live["pbp_participation"] = "t2"
    backfill = NflverseBulkCollector(
        seasons_override=[2023, 2024], datasets={"participation_player_season"}
    )
    assert fake.cycle(backfill) == {"participation_player_season"}
    assert fake.freshness["nflverse:pbp_participation"] == "t2"

    stored = fake.cycle(NflverseBulkCollector())
    assert stored is not None and "participation_player_season" in stored
    assert fake.fetched("load_participation") == [[2025]]
    assert (
        fake.freshness["nflverse:participation_player_season"]
        == "pbp_participation@t2;seasons=2025"
    )


def test_run_meta_logs_live_tags_and_which_datasets_own_key_moved(fake):
    """Only pbp moved, so every core table is rebuilt (all-or-nothing), but only the two
    built from pbp report their own key moving. That split is what the analysts'
    freshness-gate decision needs (docs/phases/P7.md, step 9)."""
    fake.live["pbp"] = "t2"
    assert fake.cycle(NflverseBulkCollector()) == _CORE
    assert fake.result is not None
    logged = fake.result.meta["datasets"]
    assert set(logged) == _CORE
    assert logged["team_week"] == {
        "live": {"pbp": "t2"},
        "key_moved": True,
        "rows_changed": fake.upserted["team_week"],
        "duplicates_dropped": 0,
    }
    assert logged["player_game_pbp"]["live"] == {"pbp": "t2", "ftn_charting": "t1"}
    assert logged["player_game_pbp"]["key_moved"] is True
    for dataset in _CORE - {"team_week", "player_game_pbp"}:
        assert logged[dataset]["key_moved"] is False, dataset
    assert fake.result.meta["pgp_ftn_unmatched_plays"] == 0  # existing meta kept


def test_run_meta_duplicates_dropped_is_logged_per_dataset(fake):
    """Duplicate-key rows dropped before the diff are recorded per dataset, next to
    rows_changed, and are zero where nothing was dropped."""
    fake.live["snap_counts"] = "t2"
    fake.duplicates = {"snaps": 4, "depth": 1}
    assert fake.cycle(NflverseBulkCollector()) == _CORE
    assert fake.result is not None
    logged = fake.result.meta["datasets"]
    assert logged["snaps"]["duplicates_dropped"] == 4
    assert logged["depth"]["duplicates_dropped"] == 1
    for dataset in _CORE - {"snaps", "depth"}:
        assert logged[dataset]["duplicates_dropped"] == 0, dataset


def test_run_meta_rows_changed_is_what_the_hash_diff_wrote(fake):
    """rows_changed counts rows after the hash diff, per dataset: a marker that moves with
    unchanged content must log 0, not the rows fetched."""
    fake.live["snap_counts"] = "t2"
    fake.unchanged = {"snaps": 3, "team_week": 10**9}  # 3 snaps rows and all of team_week
    assert fake.cycle(NflverseBulkCollector()) == _CORE
    assert fake.result is not None
    logged = fake.result.meta["datasets"]
    assert fake.offered["snaps"] > 3
    assert logged["snaps"]["rows_changed"] == fake.offered["snaps"] - 3
    assert fake.offered["team_week"] > 0
    assert logged["team_week"]["rows_changed"] == 0
    for dataset in _CORE:
        assert logged[dataset]["rows_changed"] == fake.upserted.get(dataset, 0), dataset
    assert fake.result.rows_written == sum(d["rows_changed"] for d in logged.values())


def test_forced_run_builds_everything_and_records_nothing(monkeypatch):
    fake = _FakeNflverse(monkeypatch, {tag: "t1" for tag in _TAGS})
    collector = NflverseBulkCollector()
    ctx = RunContext(
        season=2026,
        week=3,
        season_type="REG",
        now=datetime(2026, 9, 27, tzinfo=UTC),
        settings=None,  # type: ignore[arg-type]
        conn=None,  # type: ignore[arg-type]
    )
    validated = collector.validate(collector.fetch(ctx))  # no should_run, as with force
    result = collector.store(ctx, validated)
    assert set(validated) == _CORE | {"participation_player_season"}
    assert fake.freshness == {}
    assert result.meta["pgp_ftn_unmatched_plays"] == 0
    # No live timestamps and no staleness decision on a forced run: logged as unknown.
    for logged in result.meta["datasets"].values():
        assert logged["key_moved"] is None
        assert set(logged["live"].values()) == {None}
