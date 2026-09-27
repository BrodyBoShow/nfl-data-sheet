"""
Job: Load nflverse bulk stats (pbp-derived team and player-game aggregates, player-week
     box+efficiency stats, snap counts, next-gen stats, FTN charting, PFR advanced stats,
     current depth charts, participation player-season aggregates) into staged tables for
     the Phase 2 and Phase 7 analysts. Never stores raw play-by-play or participation's
     per-play rows.
Reads: nflreadpy load_pbp/load_player_stats/load_snap_counts/load_nextgen_stats/
       load_ftn_charting/load_depth_charts/load_pfr_advstats/load_participation;
       nflverse-data release timestamp.json for each source's tag
Writes: player_week, team_week, snaps, ngs, ftn, pfr_advstats, depth, player_game_pbp,
        participation_player_season, source_freshness
Tier: T2
Phase: P2, extended P7
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

import httpx
import nflreadpy as nfl
import polars as pl

from pipeline.core.base import Collector, RunContext, WorkResult
from pipeline.core.db import filter_changed, upsert_rows
from pipeline.core.freshness import get_last_value, set_last_value
from pipeline.core.hashing import hash_row
from pipeline.core.team_aliases import TEAM_ABBR_ALIASES

_log = logging.getLogger(__name__)

_TIMESTAMP_URL = "https://github.com/nflverse/nflverse-data/releases/download/{tag}/timestamp.json"
# Real release tags, confirmed live against nflreadpy's own download path (docs/sources.md
# "IMPORTANT" note) -- several differ from the function/stat_type name, e.g. load_player_stats
# downloads from tag `stats_player`, not `player_stats`. The full set of tracked tags is
# every tag in `_DATASET_SOURCES`' values, defined below alongside the dataset-name mapping.

_SEASON_TYPES = ("REG", "POST")

# Maps each staged table to the release tag(s) it depends on -- used by --datasets to scope
# a run (fetch/validate/store/should_run) to just the tables a given analyst actually reads,
# e.g. Efficiency only needs team_week/player_week/snaps.
_DATASET_SOURCES: dict[str, tuple[str, ...]] = {
    "team_week": ("pbp",),
    "player_week": ("stats_player",),
    "snaps": ("snap_counts",),
    "ftn": ("ftn_charting",),
    "depth": ("depth_charts",),
    "ngs": ("nextgen_stats",),
    "pfr_advstats": ("pfr_advstats",),
    "player_game_pbp": ("pbp", "ftn_charting"),
    "participation_player_season": ("pbp_participation",),
}

# Freshness. A single-tag table is gated on its tag's `nflverse:{tag}` key, and storing it
# advances that key (the analysts read these keys for inputs_version). The two P7 tables
# instead record what they were built from in their own key, `nflverse:{dataset}` =
# "tag@ts;...;seasons=...", and are due whenever that string differs from live:
# - player_game_pbp reads two tags that other tables own (pbp: team_week; ftn_charting:
#   ftn), so a scoped `--datasets team_week` run advancing `nflverse:pbp` can't hide new
#   games from it. It's due when EITHER tag moved, and always refetches both, never joining
#   fresh pbp to an older FTN pull. A game FTN hasn't charted yet gets NULL ftn_* columns
#   (never zeros) and is rebuilt when FTN's tag moves.
# - participation_player_season's seasons part catches the rollover: when ctx.season moves
#   on, season-1 is a new file even though the tag last moved at the post-season release.
_BUILT_FROM_GATED = frozenset({"player_game_pbp", "participation_player_season"})
# Gated on its own tag only, outside the core all-or-nothing set: the participation release
# changes about once a year (docs/sources.md), so it's never refetched just because pbp
# moved. Its season-1 pbp join input is deliberately not part of its gate -- pbp's tag moves
# nightly in-season -- so a later correction to season-1 pbp reaches this table only on the
# next participation release or season rollover.
_INDEPENDENTLY_GATED = frozenset({"participation_player_season"})

# Garbage time = win probability for the team with the ball is a near-lock either way, but
# a pure WP threshold applied uniformly across the whole game is too aggressive in an
# early blowout: it can trigger as early as the 2nd quarter, discarding plays the trailing
# team still ran with a real game plan (verified live, 2025-2026: 38 of one team's 51
# eligible plays got cut this way, leaving 13 -- see docs/signals.md). Time-aware instead:
# Q1-Q2 is never garbage time (WP swings fastest and least meaningfully early); Q3 uses a
# tighter band (only a near-certain outcome counts); Q4/OT keeps the original band, where a
# comfortable-but-not-locked WP still means the outcome is realistically decided. Judgment
# call (docs/signals.md doesn't pin an exact definition) -- tunable once Phase 5's grader
# exists (docs/signals.md's "Debugging" note / `docs/architecture.md`'s GRADE ==> A_EFF).
_GARBAGE_TIME_WP_LOW = 0.05
_GARBAGE_TIME_WP_HIGH = 0.95
_GARBAGE_TIME_Q3_WP_LOW = 0.02
_GARBAGE_TIME_Q3_WP_HIGH = 0.98

# "Explosive play" thresholds -- the common analytics convention (20+ yard pass, 10+ yard
# rush), also a judgment call absent a pinned definition in docs/signals.md.
_EXPLOSIVE_PASS_YARDS = 20
_EXPLOSIVE_RUSH_YARDS = 10

_PLAYER_WEEK_COLS = [
    "player_id",
    "game_id",
    "season",
    "week",
    "season_type",
    "team",
    "opponent_team",
    "position",
    "position_group",
    "completions",
    "attempts",
    "passing_yards",
    "passing_tds",
    "passing_interceptions",
    "sacks_suffered",
    "passing_air_yards",
    "passing_yards_after_catch",
    "passing_epa",
    "passing_cpoe",
    "carries",
    "rushing_yards",
    "rushing_tds",
    "rushing_epa",
    "receptions",
    "targets",
    "receiving_yards",
    "receiving_tds",
    "receiving_air_yards",
    "receiving_yards_after_catch",
    "receiving_epa",
    "racr",
    "target_share",
    "air_yards_share",
    "wopr",
    # 0030: defensive box score (def_sacks Float64 for half sacks, the rest Int32)
    "def_sacks",
    "def_qb_hits",
    "def_tackles_for_loss",
    "def_pass_defended",
    "def_fumbles_forced",
]

_TEAM_WEEK_COUNT_COLS = [
    "plays",
    "success_count",
    "explosive_count",
    "garbage_time_plays_excluded",
    "pass_plays",
    "pass_success_count",
    "pass_explosive_count",
    "rush_plays",
    "rush_success_count",
    "rush_explosive_count",
    "down1_plays",
    "down1_success_count",
    "down2_plays",
    "down2_success_count",
    "down3_plays",
    "down3_success_count",
    "down4_plays",
    "down4_success_count",
    "drives",
    "three_and_out_drives",
    "red_zone_trips",
    "red_zone_tds",
    "points",
]

# Offense points scored per drive, from pbp's own fixed_drive_result (not the game
# scoreboard, so it can be garbage-time-filtered and offense-only like the rest of
# team_week's drive columns) -- verified live against 2023 pbp (docs/sources.md), which
# has exactly these ten fixed_drive_result values (incl. null). Anything not listed here
# scores 0: Punt/Turnover/Turnover on downs/Missed field goal/End of half score nothing;
# Safety and Opp touchdown are points for the OTHER team (2 and 6 respectively) that this
# per-team offense-drive schema has no clean place to attribute -- documented limitation,
# not fixed here (rare enough league-wide to not be worth re-architecting around).
_DRIVE_RESULT_POINTS: dict[str, int] = {"Touchdown": 6, "Field goal": 3}
_DRIVE_RESULT_KNOWN_ZERO = {
    "Punt",
    "Turnover",
    "Turnover on downs",
    "Missed field goal",
    "End of half",
    "Safety",
    "Opp touchdown",
}
_DRIVE_RESULT_KNOWN = set(_DRIVE_RESULT_POINTS) | _DRIVE_RESULT_KNOWN_ZERO

# Retired-franchise-code normalization (TEAM_ABBR_ALIASES, pipeline/core/team_aliases.py)
# -- without it, a team's prior-season join (pipeline/analysts/efficiency.py) would
# silently miss a relocated franchise's history. See that module for the verified-live
# detail on which historical seasons/sources still carry the old codes.
_TEAM_WEEK_SUM_COLS = [
    "epa_sum",
    "pass_epa_sum",
    "rush_epa_sum",
    "down1_epa_sum",
    "down2_epa_sum",
    "down3_epa_sum",
    "down4_epa_sum",
]
_TEAM_WEEK_COLS = (
    ["game_id", "season", "week", "season_type", "team", "opponent_team"]
    + _TEAM_WEEK_COUNT_COLS
    + _TEAM_WEEK_SUM_COLS
)

_NGS_STAT_TYPES = ("passing", "rushing", "receiving")
_NGS_METRIC_COLS_BY_TYPE = {
    "passing": [
        "completion_percentage_above_expectation",
        "aggressiveness",
        "avg_air_yards_differential",
        # 0027
        "avg_time_to_throw",
        "avg_completed_air_yards",
        "avg_intended_air_yards",
        "avg_air_yards_to_sticks",
        "max_completed_air_distance",
        "avg_air_distance",
        "max_air_distance",
        "attempts",
        "completions",
    ],
    "rushing": [
        "rush_yards_over_expected",
        "rush_yards_over_expected_per_att",
        "percent_attempts_gte_eight_defenders",
        # 0027
        "efficiency",
        "avg_time_to_los",
        "expected_rush_yards",
        "rush_pct_over_expected",
        "rush_attempts",
    ],
    "receiving": [
        "avg_separation",
        "avg_yac_above_expectation",
        # 0027 (avg_intended_air_yards is also a passing column; stat_type says whose)
        "avg_cushion",
        "percent_share_of_intended_air_yards",
        "catch_percentage",
        "avg_yac",
        "avg_expected_yac",
        "avg_intended_air_yards",
        "targets",
        "receptions",
    ],
}
_NGS_ALL_METRIC_COLS = sorted({c for cols in _NGS_METRIC_COLS_BY_TYPE.values() for c in cols})
# NGS's count columns are Int32 at the source (fixture-verified); every other metric is
# Float64. Null-filling another stat_type's columns has to keep each one's own dtype or the
# stat_type frames won't concat.
_NGS_INT_COLS = frozenset({"attempts", "completions", "rush_attempts", "targets", "receptions"})

_PFR_STAT_TYPES = ("pass", "rush", "rec", "def")
_PFR_METRIC_COLS_BY_TYPE = {
    "pass": [
        "passing_bad_throw_pct",
        "passing_drop_pct",
        "times_pressured_pct",
        "times_blitzed",
        "times_hurried",
        "times_hit",
        # 0027
        "passing_drops",
        "passing_bad_throws",
        "times_sacked",
        "times_pressured",
    ],
    "rush": [
        "rushing_yards_before_contact_avg",
        "rushing_yards_after_contact_avg",
        "rushing_broken_tackles",
        # 0027
        "carries",
        "rushing_yards_before_contact",
        "rushing_yards_after_contact",
    ],
    "rec": [
        "receiving_broken_tackles",
        "receiving_drop_pct",
        # 0027
        "receiving_drop",
        "receiving_int",
        "receiving_rat",
    ],
    "def": [
        "def_pressures",
        "def_missed_tackle_pct",
        "def_passer_rating_allowed",
        # 0027: PFR's nearest-defender charting, not a coverage assignment
        "def_ints",
        "def_targets",
        "def_completions_allowed",
        "def_completion_pct",
        "def_yards_allowed",
        "def_yards_allowed_per_cmp",
        "def_yards_allowed_per_tgt",
        "def_receiving_td_allowed",
        "def_adot",
        "def_air_yards_completed",
        "def_yards_after_catch",
        "def_times_blitzed",
        "def_times_hurried",
        "def_times_hitqb",
        "def_sacks",
        "def_tackles_combined",
        "def_missed_tackles",
    ],
}
_PFR_ALL_METRIC_COLS = sorted({c for cols in _PFR_METRIC_COLS_BY_TYPE.values() for c in cols})

_FTN_COLS = [
    "game_id",
    "play_id",
    "n_offense_backfield",
    "n_defense_box",
    "is_no_huddle",
    "is_motion",
    "is_play_action",
    "is_screen_pass",
    "is_rpo",
    "n_blitzers",
    "n_pass_rushers",
    # 0027
    "starting_hash",
    "qb_location",
    "read_thrown",
    "is_trick_play",
    "is_qb_out_of_pocket",
    "is_interception_worthy",
    "is_throw_away",
    "is_catchable_ball",
    "is_contested_ball",
    "is_created_reception",
    "is_drop",
    "is_qb_sneak",
    "is_qb_fault_sack",
]

# player_game_pbp (migration 0028): the column lists ARE the migration's -- a test parses
# 0028 and checks them. Every "*_sum" column is double precision, every other value an int.
# Thresholds match team_week where one exists (_EXPLOSIVE_*_YARDS).
_DEEP_AIR_YARDS = 20
_RED_ZONE_YARDLINE = 20
_GOAL_LINE_YARDLINE = 5
_STACKED_BOX = 8
# run_location x run_gap; run_gap is null on middle (fixture-verified), so mid is one cell.
_GAP_CELLS: dict[str, tuple[str, str | None]] = {
    "le": ("left", "end"),
    "lt": ("left", "tackle"),
    "lg": ("left", "guard"),
    "mid": ("middle", None),
    "rg": ("right", "guard"),
    "rt": ("right", "tackle"),
    "re": ("right", "end"),
}
_PASS_LOCATIONS = ("left", "middle", "right")

_PGP_KEY_COLS = ["game_id", "player_id", "season", "week", "season_type", "team", "opponent_team"]
_PGP_PASSER_COLS = [
    "dropbacks",
    "dropback_epa_sum",
    "dropback_success",
    "pass_attempts",
    "completions",
    "interceptions",
    "sacks",
    "scrambles",
    "pass_air_yards_sum",
    "pass_air_yards_n",
    "cpoe_sum",
    "cpoe_n",
    "deep_attempts",
]
_PGP_PASSER_FTN_COLS = [
    "ftn_charted_dropbacks",
    "ftn_pa_dropbacks",
    "ftn_pa_epa_sum",
    "ftn_blitzed_dropbacks",
    "ftn_blitzed_epa_sum",
    "ftn_out_of_pocket_dropbacks",
    "ftn_charted_attempts",
    "ftn_screen_attempts",
    "ftn_throwaways",
    "ftn_catchable_attempts",
    "ftn_int_worthy",
    "ftn_charted_sacks",
    "ftn_qb_fault_sacks",
]
_PGP_RUSHER_COLS = [
    "carries",
    "rush_epa_sum",
    "rush_success",
    "rush_yards",
    "rush_stuffs",
    "rush_explosive",
    "rush_first_downs",
    "rz_carries",
    "gl_carries",
    *[f"carries_{cell}" for cell in _GAP_CELLS],
    *[f"rush_epa_sum_{cell}" for cell in _GAP_CELLS],
    *[f"rush_success_{cell}" for cell in _GAP_CELLS],
]
_PGP_RUSHER_FTN_COLS = [
    "ftn_charted_carries",
    "ftn_stacked_box_carries",
    "ftn_stacked_box_epa_sum",
]
_PGP_RECEIVER_COLS = [
    "targets",
    "receptions",
    "rec_epa_sum",
    "rec_success",
    "rec_yards",
    "rec_air_yards_sum",
    "rec_air_yards_n",
    "rec_yac_sum",
    "rec_yac_oe_sum",
    "rec_yac_oe_n",
    "rec_first_downs",
    "rec_explosive",
    "deep_targets",
    "rz_targets",
    "ez_targets",
    *[f"targets_{loc}" for loc in _PASS_LOCATIONS],
    *[f"rec_epa_sum_{loc}" for loc in _PASS_LOCATIONS],
]
_PGP_RECEIVER_FTN_COLS = [
    "ftn_charted_targets",
    "ftn_charted_receptions",
    "ftn_catchable_targets",
    "ftn_catchable_receptions",
    "ftn_drops",
    "ftn_contested_targets",
    "ftn_contested_receptions",
    "ftn_created_receptions",
    "ftn_screen_targets",
    "ftn_pa_targets",
    "ftn_pa_rec_epa_sum",
]
_PLAYER_GAME_PBP_COLS = (
    _PGP_KEY_COLS
    + _PGP_PASSER_COLS
    + _PGP_PASSER_FTN_COLS
    + _PGP_RUSHER_COLS
    + _PGP_RUSHER_FTN_COLS
    + _PGP_RECEIVER_COLS
    + _PGP_RECEIVER_FTN_COLS
)
# pbp columns the player-keyed aggregations read, beyond validate's team_week set.
_PBP_PLAYER_REQUIRED = {
    "play_id",
    "season",
    "week",
    "season_type",
    "play_type",
    "play_deleted",
    "pass",
    "rush",
    "qb_kneel",
    "qb_spike",
    "two_point_attempt",
    "qb_dropback",
    "pass_attempt",
    "sack",
    "qb_scramble",
    "complete_pass",
    "interception",
    "passer_id",
    "rusher_id",
    "receiver_id",
    "yards_gained",
    "air_yards",
    "cpoe",
    "yards_after_catch",
    "xyac_mean_yardage",
    "yardline_100",
    "first_down_rush",
    "first_down_pass",
    "run_location",
    "run_gap",
    "pass_location",
}
_FTN_FLAG_COLS = [
    "is_play_action",
    "is_screen_pass",
    "is_qb_out_of_pocket",
    "is_throw_away",
    "is_catchable_ball",
    "is_contested_ball",
    "is_created_reception",
    "is_drop",
    "is_interception_worthy",
    "is_qb_fault_sack",
    "n_blitzers",
    "n_defense_box",
]
_META_EXAMPLE_IDS = 10

# participation_player_season (migration 0029). Every column is NOT NULL: season sums, so
# a player with no man-coverage targets has a real 0.
_PARTICIPATION_COLS = [
    "player_id",
    "season",
    "off_snaps",
    "off_dropbacks",
    "off_dropbacks_man",
    "off_dropbacks_zone",
    "targets_man",
    "targets_zone",
    "rec_epa_sum_man",
    "rec_epa_sum_zone",
    "pass_dropbacks_man",
    "pass_dropbacks_zone",
    "pass_epa_sum_man",
    "pass_epa_sum_zone",
    "def_snaps",
    "def_dropbacks",
    "def_dropbacks_man",
    "def_dropbacks_zone",
]
# Only what 0029 uses. `route` and `was_pressure` are deliberately absent: see
# _aggregate_participation_player_season's docstring.
_PARTICIPATION_REQUIRED = {
    "nflverse_game_id",
    "play_id",
    "offense_players",
    "defense_players",
    "defense_man_zone_type",
}
_COVERAGE_LABELS = {"MAN_COVERAGE": "man", "ZONE_COVERAGE": "zone"}


def _fetch_timestamp(tag: str) -> str:
    resp = httpx.get(_TIMESTAMP_URL.format(tag=tag), follow_redirects=True, timeout=30)
    resp.raise_for_status()
    return str(resp.json()["last_updated"])


def _derive_season_type(df: pl.DataFrame, game_type_col: str = "game_type") -> pl.DataFrame:
    return df.with_columns(
        pl.when(pl.col(game_type_col) == "REG")
        .then(pl.lit("REG"))
        .otherwise(pl.lit("POST"))
        .alias("season_type")
    )


def _garbage_time_expr() -> pl.Expr:
    """Time-aware: Q1-Q2 never garbage time; Q3 only a near-certain WP; Q4/OT the
    original band. Callers still wrap this in `.fill_null(False)` -- a null `qtr` or `wp`
    propagates to a null comparison here, same as before."""
    return (
        pl.when(pl.col("qtr") <= 2)
        .then(pl.lit(False))
        .when(pl.col("qtr") == 3)
        .then((pl.col("wp") < _GARBAGE_TIME_Q3_WP_LOW) | (pl.col("wp") > _GARBAGE_TIME_Q3_WP_HIGH))
        .otherwise((pl.col("wp") < _GARBAGE_TIME_WP_LOW) | (pl.col("wp") > _GARBAGE_TIME_WP_HIGH))
    )


def _normalize_team_abbr(df: pl.DataFrame, cols: list[str]) -> pl.DataFrame:
    """Map retired franchise codes (TEAM_ABBR_ALIASES, pipeline/core/team_aliases.py) to
    their current abbreviation in every listed column that's actually present."""
    present = [c for c in cols if c in df.columns]
    if not present:
        return df
    return df.with_columns(pl.col(c).replace(TEAM_ABBR_ALIASES) for c in present)


def _warn_on_unknown_drive_results(drives: pl.DataFrame) -> None:
    seen = set(drives["fixed_drive_result"].drop_nulls().unique().to_list())
    unknown = seen - _DRIVE_RESULT_KNOWN
    if unknown:
        _log.warning(
            "team_week points: unrecognized fixed_drive_result value(s) %s scored as 0 "
            "points -- update _DRIVE_RESULT_POINTS/_DRIVE_RESULT_KNOWN_ZERO if these are "
            "legitimate scoring outcomes",
            sorted(unknown),
        )


def _build_player_week(player_stats: pl.DataFrame) -> pl.DataFrame:
    return player_stats.filter(
        pl.col("player_id").is_not_null()
        & pl.col("game_id").is_not_null()
        & pl.col("season_type").is_in(_SEASON_TYPES)
    ).select(_PLAYER_WEEK_COLS)


def _aggregate_team_week(pbp: pl.DataFrame) -> pl.DataFrame:
    scrimmage = pbp.filter(
        (pl.col("play_deleted") != 1)
        & pl.col("epa").is_not_null()
        & ((pl.col("pass") == 1) | (pl.col("rush") == 1))
        # A penalty no-play still carries the original called play's pass/rush flags and
        # a non-null epa -- verified live (docs/sources.md): 1,622 no_play/qb_kneel/
        # qb_spike rows leaked into "plays" this way across 2025-2026. None of those are
        # real plays, so they're excluded from every split below (overall/pass/rush/down),
        # all of which derive from this one filtered frame. Drive-level aggregation below
        # is deliberately NOT filtered this way -- it uses every play in a drive regardless
        # of type, since drive_play_count/fixed_drive_result are drive-constant fields.
        & (pl.col("play_type") != "no_play")
        & (pl.col("qb_kneel") != 1)
        & (pl.col("qb_spike") != 1)
        & pl.col("posteam").is_not_null()
        & pl.col("season_type").is_in(_SEASON_TYPES)
    ).with_columns(
        _garbage_time_expr().fill_null(False).alias("_garbage"),
        (pl.col("pass") == 1).alias("_is_pass"),
        (
            ((pl.col("pass") == 1) & (pl.col("yards_gained") >= _EXPLOSIVE_PASS_YARDS))
            | ((pl.col("rush") == 1) & (pl.col("yards_gained") >= _EXPLOSIVE_RUSH_YARDS))
        )
        .fill_null(False)
        .alias("_explosive"),
    )

    keys = ["game_id", "season", "week", "season_type", "posteam", "defteam"]

    garbage_counts = (
        scrimmage.filter(pl.col("_garbage"))
        .group_by(["game_id", "posteam"])
        .agg(pl.len().alias("garbage_time_plays_excluded"))
    )

    clean = scrimmage.filter(~pl.col("_garbage"))

    overall = clean.group_by(keys).agg(
        pl.len().alias("plays"),
        pl.col("epa").sum().alias("epa_sum"),
        pl.col("success").sum().alias("success_count"),
        pl.col("_explosive").sum().alias("explosive_count"),
    )

    def _split(frame: pl.DataFrame, prefix: str) -> pl.DataFrame:
        return frame.group_by(["game_id", "posteam"]).agg(
            pl.len().alias(f"{prefix}_plays"),
            pl.col("epa").sum().alias(f"{prefix}_epa_sum"),
            pl.col("success").sum().alias(f"{prefix}_success_count"),
            pl.col("_explosive").sum().alias(f"{prefix}_explosive_count"),
        )

    pass_split = _split(clean.filter(pl.col("_is_pass")), "pass")
    rush_split = _split(clean.filter(~pl.col("_is_pass")), "rush")

    down_splits = [
        clean.filter(pl.col("down") == d)
        .group_by(["game_id", "posteam"])
        .agg(
            pl.len().alias(f"down{d}_plays"),
            pl.col("epa").sum().alias(f"down{d}_epa_sum"),
            pl.col("success").sum().alias(f"down{d}_success_count"),
        )
        for d in (1, 2, 3, 4)
    ]

    # Drive-level outcomes use ALL plays of a drive (not just pass/rush) since
    # drive_play_count/fixed_drive_result are drive-constant fields carried on every play,
    # including field goals/punts. A drive counts if any of its plays are non-garbage.
    drive_rows = pbp.filter(
        (pl.col("play_deleted") != 1)
        & pl.col("fixed_drive").is_not_null()
        & pl.col("posteam").is_not_null()
        & pl.col("season_type").is_in(_SEASON_TYPES)
    ).with_columns(_garbage_time_expr().fill_null(False).alias("_garbage"))

    drives = (
        drive_rows.group_by(["game_id", "posteam", "fixed_drive"])
        .agg(
            pl.col("drive_play_count").max().alias("drive_play_count"),
            pl.col("fixed_drive_result").first().alias("fixed_drive_result"),
            pl.col("yardline_100").min().alias("closest_yardline"),
            (~pl.col("_garbage")).any().alias("_competitive"),
        )
        .filter(pl.col("_competitive"))
    )
    _warn_on_unknown_drive_results(drives)

    drive_summary = drives.with_columns(
        pl.col("fixed_drive_result")
        .replace_strict(_DRIVE_RESULT_POINTS, default=0, return_dtype=pl.Int64)
        .alias("_drive_points")
    ).group_by(["game_id", "posteam"]).agg(
        pl.len().alias("drives"),
        pl.col("_drive_points").sum().alias("points"),
        ((pl.col("fixed_drive_result") == "Punt") & (pl.col("drive_play_count") == 3))
        .sum()
        .alias("three_and_out_drives"),
        (pl.col("closest_yardline") <= 20).sum().alias("red_zone_trips"),
        ((pl.col("closest_yardline") <= 20) & (pl.col("fixed_drive_result") == "Touchdown"))
        .sum()
        .alias("red_zone_tds"),
    )

    result = overall
    for other in (garbage_counts, pass_split, rush_split, *down_splits, drive_summary):
        result = result.join(other, on=["game_id", "posteam"], how="left")

    result = result.rename({"posteam": "team", "defteam": "opponent_team"}).with_columns(
        [pl.col(c).fill_null(0).cast(pl.Int64) for c in _TEAM_WEEK_COUNT_COLS]
        + [pl.col(c).fill_null(0.0) for c in _TEAM_WEEK_SUM_COLS]
    )
    return result.select(_TEAM_WEEK_COLS)


def _build_ngs(ngs_frames: dict[str, pl.DataFrame]) -> pl.DataFrame:
    parts = []
    for stat_type, df in ngs_frames.items():
        metric_cols = _NGS_METRIC_COLS_BY_TYPE[stat_type]
        part = (
            df.filter(pl.col("player_gsis_id").is_not_null())
            .select(["player_gsis_id", "season", "week", "season_type", "team_abbr", *metric_cols])
            .rename({"player_gsis_id": "player_id", "team_abbr": "team"})
            .with_columns(pl.col(c).cast(pl.Int64) for c in metric_cols if c in _NGS_INT_COLS)
        )
        missing = [c for c in _NGS_ALL_METRIC_COLS if c not in metric_cols]
        part = part.with_columns(
            [
                pl.lit(None, dtype=pl.Int64 if c in _NGS_INT_COLS else pl.Float64).alias(c)
                for c in missing
            ]
            + [pl.lit(stat_type).alias("stat_type")]
        )
        parts.append(
            part.select(
                ["player_id", "season", "week", "season_type", "stat_type", "team"]
                + _NGS_ALL_METRIC_COLS
            )
        )
    return pl.concat(parts, how="vertical")


def _build_pfr_advstats(pfr_frames: dict[str, pl.DataFrame]) -> pl.DataFrame:
    parts = []
    for stat_type, df in pfr_frames.items():
        metric_cols = _PFR_METRIC_COLS_BY_TYPE[stat_type]
        part = (
            _derive_season_type(df)
            .filter(pl.col("pfr_player_id").is_not_null())
            .select(
                [
                    "game_id",
                    "pfr_player_id",
                    "season",
                    "week",
                    "season_type",
                    "team",
                    "opponent",
                    *metric_cols,
                ]
            )
            .rename({"opponent": "opponent_team"})
        )
        missing = [c for c in _PFR_ALL_METRIC_COLS if c not in metric_cols]
        part = part.with_columns(
            [pl.lit(None, dtype=pl.Float64).alias(c) for c in missing]
            + [pl.lit(stat_type).alias("stat_type")]
        )
        parts.append(
            part.select(
                [
                    "game_id",
                    "pfr_player_id",
                    "season",
                    "week",
                    "season_type",
                    "stat_type",
                    "team",
                    "opponent_team",
                ]
                + _PFR_ALL_METRIC_COLS
            )
        )
    return _normalize_team_abbr(pl.concat(parts, how="vertical"), ["team", "opponent_team"])


def _build_snaps(snap_counts: pl.DataFrame) -> pl.DataFrame:
    built = (
        _derive_season_type(snap_counts)
        .filter(pl.col("pfr_player_id").is_not_null() & pl.col("game_id").is_not_null())
        .rename({"opponent": "opponent_team"})
        .with_columns(
            pl.col("offense_snaps").cast(pl.Int64),
            pl.col("defense_snaps").cast(pl.Int64),
            pl.col("st_snaps").cast(pl.Int64),
        )
        .select(
            "game_id",
            "pfr_player_id",
            "season",
            "week",
            "season_type",
            "team",
            "opponent_team",
            "position",
            "offense_snaps",
            "offense_pct",
            "defense_snaps",
            "defense_pct",
            "st_snaps",
            "st_pct",
        )
    )
    return _normalize_team_abbr(built, ["team", "opponent_team"])


def _build_ftn(ftn: pl.DataFrame) -> pl.DataFrame:
    return (
        ftn.filter(
            pl.col("nflverse_game_id").is_not_null() & pl.col("nflverse_play_id").is_not_null()
        )
        .rename({"nflverse_game_id": "game_id", "nflverse_play_id": "play_id"})
        .select(_FTN_COLS)
    )


def _build_depth(depth_charts: pl.DataFrame) -> pl.DataFrame:
    parsed = depth_charts.with_columns(
        pl.col("dt")
        .str.strptime(pl.Datetime(time_unit="us", time_zone="UTC"), "%Y-%m-%dT%H:%M:%SZ")
        .alias("as_of")
    )
    latest_per_team = parsed.group_by("team").agg(pl.col("as_of").max().alias("_latest"))
    current = parsed.join(latest_per_team, on="team").filter(pl.col("as_of") == pl.col("_latest"))
    return current.rename({"gsis_id": "player_id"}).select(
        "team", "pos_grp", "pos_abb", "pos_rank", "player_id", "as_of"
    )


def _player_play_scope(pbp: pl.DataFrame) -> pl.DataFrame:
    """The play scope of every player-keyed aggregate (0028's header): real scrimmage
    plays, garbage time INCLUDED. Unlike team_week, two-point tries are excluded
    explicitly, and there's no garbage-time filter -- player_week, ngs and pfr_advstats
    are all-play and can't be filtered, so every player rate shares one scope."""
    return pbp.filter(
        (pl.col("play_deleted") != 1)
        & pl.col("epa").is_not_null()
        & ((pl.col("pass") == 1) | (pl.col("rush") == 1))
        & (pl.col("play_type") != "no_play")
        & (pl.col("qb_kneel") != 1)
        & (pl.col("qb_spike") != 1)
        & (pl.col("two_point_attempt") != 1)
        & pl.col("posteam").is_not_null()
        & pl.col("season_type").is_in(_SEASON_TYPES)
    )


def _target_expr() -> pl.Expr:
    """A target: a pass attempt that wasn't a sack (0028). nflverse sets pass_attempt on
    sacks too (fixture: all 6 sacks have pass_attempt == 1 and a null receiver)."""
    return (pl.col("pass_attempt") == 1) & (pl.col("sack") == 0)


def _integral_play_id(df: pl.DataFrame, col: str, source: str) -> pl.DataFrame:
    """Cast a play_id column to Int64 for a (game_id, play_id) join, refusing anything that
    isn't an exact integer. The dtype differs by source and season (pbp Float64, FTN Int32,
    participation Float64 in 2025 and Int32 in 2022), and a silent float->int cast would
    truncate a fractional id into some other play's key."""
    dtype = df.schema[col]
    if dtype.is_integer():
        return df.with_columns(pl.col(col).cast(pl.Int64))
    if dtype.is_float():
        bad = df.filter(pl.col(col).is_not_null() & (pl.col(col) != pl.col(col).floor())).height
        if bad:
            raise ValueError(f"{source} {col}: {bad} non-integer value(s), refusing to join")
        return df.with_columns(pl.col(col).cast(pl.Int64))
    raise ValueError(f"{source} {col} has dtype {dtype}, expected an integer play id")


def _require_unique_plays(df: pl.DataFrame, source: str) -> None:
    """A duplicated (game_id, play_id) on the joined-in side fans the join out and double
    counts every column of that play, silently."""
    dupes = df.group_by("game_id", "play_id").len().filter(pl.col("len") > 1).height
    if dupes:
        raise ValueError(f"{source}: {dupes} duplicated (game_id, play_id) key(s)")


def _count(cond: pl.Expr) -> pl.Expr:
    return cond.fill_null(False).sum().cast(pl.Int64)


def _sum_where(value: str, cond: pl.Expr) -> pl.Expr:
    return pl.col(value).filter(cond.fill_null(False)).sum()


def _n_where(value: str, cond: pl.Expr) -> pl.Expr:
    """Rows meeting `cond` with a non-null `value`: the weight for a mean of `value`."""
    return pl.col(value).filter(cond.fill_null(False)).count().cast(pl.Int64)


def _passer_aggs() -> list[pl.Expr]:
    att = pl.col("_attempt")
    ftn = pl.col("_ftn")
    sack = pl.col("sack") == 1
    return [
        pl.len().cast(pl.Int64).alias("dropbacks"),
        pl.col("epa").sum().alias("dropback_epa_sum"),
        pl.col("success").sum().cast(pl.Int64).alias("dropback_success"),
        _count(att).alias("pass_attempts"),
        _count(pl.col("complete_pass") == 1).alias("completions"),
        _count(pl.col("interception") == 1).alias("interceptions"),
        _count(sack).alias("sacks"),
        _count(pl.col("qb_scramble") == 1).alias("scrambles"),
        _sum_where("air_yards", att).alias("pass_air_yards_sum"),
        _n_where("air_yards", att).alias("pass_air_yards_n"),
        _sum_where("cpoe", att).alias("cpoe_sum"),
        _n_where("cpoe", att).alias("cpoe_n"),
        _count(att & (pl.col("air_yards") >= _DEEP_AIR_YARDS)).alias("deep_attempts"),
        _count(ftn).alias("ftn_charted_dropbacks"),
        _count(ftn & pl.col("is_play_action")).alias("ftn_pa_dropbacks"),
        _sum_where("epa", ftn & pl.col("is_play_action")).alias("ftn_pa_epa_sum"),
        _count(ftn & (pl.col("n_blitzers") > 0)).alias("ftn_blitzed_dropbacks"),
        _sum_where("epa", ftn & (pl.col("n_blitzers") > 0)).alias("ftn_blitzed_epa_sum"),
        _count(ftn & pl.col("is_qb_out_of_pocket")).alias("ftn_out_of_pocket_dropbacks"),
        _count(ftn & att).alias("ftn_charted_attempts"),
        _count(ftn & att & pl.col("is_screen_pass")).alias("ftn_screen_attempts"),
        _count(ftn & att & pl.col("is_throw_away")).alias("ftn_throwaways"),
        _count(ftn & att & pl.col("is_catchable_ball")).alias("ftn_catchable_attempts"),
        _count(ftn & att & pl.col("is_interception_worthy")).alias("ftn_int_worthy"),
        _count(ftn & sack).alias("ftn_charted_sacks"),
        _count(ftn & sack & pl.col("is_qb_fault_sack")).alias("ftn_qb_fault_sacks"),
    ]


def _rusher_aggs() -> list[pl.Expr]:
    ftn = pl.col("_ftn")
    yards = pl.col("yards_gained")
    cells = {
        cell: (
            (pl.col("run_location") == loc)
            if gap is None
            else (pl.col("run_location") == loc) & (pl.col("run_gap") == gap)
        )
        for cell, (loc, gap) in _GAP_CELLS.items()
    }
    stacked = ftn & (pl.col("n_defense_box") >= _STACKED_BOX)
    return [
        pl.len().cast(pl.Int64).alias("carries"),
        pl.col("epa").sum().alias("rush_epa_sum"),
        pl.col("success").sum().cast(pl.Int64).alias("rush_success"),
        yards.sum().cast(pl.Int64).alias("rush_yards"),
        _count(yards <= 0).alias("rush_stuffs"),
        _count(yards >= _EXPLOSIVE_RUSH_YARDS).alias("rush_explosive"),
        _count(pl.col("first_down_rush") == 1).alias("rush_first_downs"),
        _count(pl.col("yardline_100") <= _RED_ZONE_YARDLINE).alias("rz_carries"),
        _count(pl.col("yardline_100") <= _GOAL_LINE_YARDLINE).alias("gl_carries"),
        *[_count(cond).alias(f"carries_{cell}") for cell, cond in cells.items()],
        *[_sum_where("epa", cond).alias(f"rush_epa_sum_{cell}") for cell, cond in cells.items()],
        *[
            _sum_where("success", cond).cast(pl.Int64).alias(f"rush_success_{cell}")
            for cell, cond in cells.items()
        ],
        _count(ftn).alias("ftn_charted_carries"),
        _count(stacked).alias("ftn_stacked_box_carries"),
        _sum_where("epa", stacked).alias("ftn_stacked_box_epa_sum"),
    ]


def _receiver_aggs() -> list[pl.Expr]:
    ftn = pl.col("_ftn")
    catch = pl.col("complete_pass") == 1
    locs = {loc: pl.col("pass_location") == loc for loc in _PASS_LOCATIONS}
    return [
        pl.len().cast(pl.Int64).alias("targets"),
        _count(catch).alias("receptions"),
        pl.col("epa").sum().alias("rec_epa_sum"),
        pl.col("success").sum().cast(pl.Int64).alias("rec_success"),
        pl.col("yards_gained").sum().cast(pl.Int64).alias("rec_yards"),
        pl.col("air_yards").sum().alias("rec_air_yards_sum"),
        pl.col("air_yards").count().cast(pl.Int64).alias("rec_air_yards_n"),
        _sum_where("yards_after_catch", catch).alias("rec_yac_sum"),
        _sum_where("_yac_oe", catch).alias("rec_yac_oe_sum"),
        _n_where("_yac_oe", catch).alias("rec_yac_oe_n"),
        _count(catch & (pl.col("first_down_pass") == 1)).alias("rec_first_downs"),
        _count(catch & (pl.col("yards_gained") >= _EXPLOSIVE_PASS_YARDS)).alias("rec_explosive"),
        _count(pl.col("air_yards") >= _DEEP_AIR_YARDS).alias("deep_targets"),
        _count(pl.col("yardline_100") <= _RED_ZONE_YARDLINE).alias("rz_targets"),
        _count(pl.col("air_yards") >= pl.col("yardline_100")).alias("ez_targets"),
        *[_count(cond).alias(f"targets_{loc}") for loc, cond in locs.items()],
        *[_sum_where("epa", cond).alias(f"rec_epa_sum_{loc}") for loc, cond in locs.items()],
        _count(ftn).alias("ftn_charted_targets"),
        _count(ftn & catch).alias("ftn_charted_receptions"),
        _count(ftn & pl.col("is_catchable_ball")).alias("ftn_catchable_targets"),
        _count(ftn & pl.col("is_catchable_ball") & catch).alias("ftn_catchable_receptions"),
        _count(ftn & pl.col("is_drop")).alias("ftn_drops"),
        _count(ftn & pl.col("is_contested_ball")).alias("ftn_contested_targets"),
        _count(ftn & pl.col("is_contested_ball") & catch).alias("ftn_contested_receptions"),
        _count(ftn & pl.col("is_created_reception") & catch).alias("ftn_created_receptions"),
        _count(ftn & pl.col("is_screen_pass")).alias("ftn_screen_targets"),
        _count(ftn & pl.col("is_play_action")).alias("ftn_pa_targets"),
        _sum_where("epa", ftn & pl.col("is_play_action")).alias("ftn_pa_rec_epa_sum"),
    ]


def _aggregate_player_game_pbp(
    pbp: pl.DataFrame, ftn_charting: pl.DataFrame
) -> tuple[pl.DataFrame, dict[str, Any]]:
    """One row per player per game from pbp joined to FTN charting (0028). Returns the
    table and join diagnostics for agent_runs.meta.

    Roles: passer = passer_id on a dropback (incl. sacks and scrambles), rusher =
    rusher_id on a designed run, receiver = receiver_id on a target. A role the player
    didn't hold in the game leaves its columns NULL -- never zero-filled -- which is also
    what holds the table near ~3 MB/season (docs/phases/P7.md).

    FTN: a play counts toward a role's ftn_* columns only if it joined an FTN row, and
    ftn_charted_* is every FTN rate's denominator, so a partially charted game gives
    rates over its charted plays. A game with no FTN rows at all (FTN charts within 48h)
    gets NULL ftn_* columns, never zeros. Both cases are counted in the meta.
    """
    scoped = _integral_play_id(_player_play_scope(pbp), "play_id", "pbp")
    ftn = _integral_play_id(
        ftn_charting.filter(
            pl.col("nflverse_game_id").is_not_null() & pl.col("nflverse_play_id").is_not_null()
        )
        .rename({"nflverse_game_id": "game_id", "nflverse_play_id": "play_id"})
        .select(["game_id", "play_id", *_FTN_FLAG_COLS]),
        "play_id",
        "ftn_charting",
    )
    _require_unique_plays(ftn, "ftn_charting")
    charted_games = ftn["game_id"].unique()

    plays = scoped.join(
        ftn.with_columns(pl.lit(True).alias("_ftn")), on=["game_id", "play_id"], how="left"
    ).with_columns(
        pl.col("_ftn").fill_null(False),
        pl.col("game_id").is_in(charted_games.implode()).alias("_ftn_game"),
        _target_expr().fill_null(False).alias("_attempt"),
        (pl.col("yards_after_catch") - pl.col("xyac_mean_yardage")).alias("_yac_oe"),
    )

    all_pbp_keys = _integral_play_id(pbp.select("game_id", "play_id"), "play_id", "pbp")
    unmatched = plays.filter(pl.col("_ftn_game") & ~pl.col("_ftn"))
    meta: dict[str, Any] = {
        "pgp_games": plays["game_id"].n_unique(),
        "pgp_ftn_uncharted_games": plays.filter(~pl.col("_ftn_game"))["game_id"].n_unique(),
        "pgp_ftn_unmatched_plays": unmatched.height,
        "pgp_ftn_unmatched_example_games": sorted(unmatched["game_id"].unique().to_list())[
            :_META_EXAMPLE_IDS
        ],
        "pgp_ftn_rows_without_pbp": ftn.join(
            all_pbp_keys, on=["game_id", "play_id"], how="anti"
        ).height,
    }

    identity_cols = ["game_id", "season", "week", "season_type", "posteam", "defteam"]
    roles = [
        (
            plays.filter((pl.col("qb_dropback") == 1) & pl.col("passer_id").is_not_null()),
            "passer_id",
            _passer_aggs(),
            _PGP_PASSER_FTN_COLS,
        ),
        (
            plays.filter((pl.col("rush") == 1) & pl.col("rusher_id").is_not_null()),
            "rusher_id",
            _rusher_aggs(),
            _PGP_RUSHER_FTN_COLS,
        ),
        (
            plays.filter(pl.col("_attempt") & pl.col("receiver_id").is_not_null()),
            "receiver_id",
            _receiver_aggs(),
            _PGP_RECEIVER_FTN_COLS,
        ),
    ]

    identity = (
        pl.concat(
            [
                frame.select(*identity_cols, pl.col(id_col).alias("player_id"))
                for frame, id_col, *_ in roles
            ],
            how="vertical",
        )
        .unique()
        .rename({"posteam": "team", "defteam": "opponent_team"})
    )
    if identity.height != identity.select("game_id", "player_id").n_unique():
        raise ValueError("player_game_pbp: a player maps to two teams in one game")

    result = identity
    for frame, id_col, aggs, ftn_cols in roles:
        agg = (
            frame.group_by("game_id", pl.col(id_col).alias("player_id"))
            .agg(*aggs, pl.col("_ftn_game").first())
            .with_columns(
                pl.when(pl.col("_ftn_game")).then(pl.col(c)).otherwise(None).alias(c)
                for c in ftn_cols
            )
            .drop("_ftn_game")
        )
        result = result.join(agg, on=["game_id", "player_id"], how="left")

    return result.select(_PLAYER_GAME_PBP_COLS).sort("game_id", "player_id"), meta


def _coverage_expr() -> pl.Expr:
    """'MAN_COVERAGE' -> man, 'ZONE_COVERAGE' -> zone, anything else -> null. 2025 marks
    an unlabeled play with '' and 2022 with null (fixtures); both are missing, never a
    label. An unknown non-empty value is also null here and reported by the caller."""
    return pl.col("defense_man_zone_type").replace_strict(
        _COVERAGE_LABELS, default=None, return_dtype=pl.String
    )


def _aggregate_participation_player_season(
    participation: pl.DataFrame, pbp: pl.DataFrame
) -> tuple[pl.DataFrame, dict[str, Any]]:
    """Per-player, per-season counts from participation joined to that season's pbp
    (0029). Historical only: every value derived from it is a `_hist` prior.

    Plays are participation rows that join an in-scope pbp play (_player_play_scope, the
    same scope as player_game_pbp). Man/zone splits count only labeled plays, so an
    unlabeled play is never read as either.

    Not read, on purpose (0029 uses neither): `route`, whose "none" is '' in 2025, not
    null; and `was_pressure`, which is False (2025) or null (2022) on plays that weren't
    dropbacks, so as a rate it must be filtered to dropbacks first. Anyone adding either
    column needs to handle that.
    """
    par = _integral_play_id(
        participation.select(sorted(_PARTICIPATION_REQUIRED)).rename(
            {"nflverse_game_id": "game_id"}
        ),
        "play_id",
        "participation",
    )
    _require_unique_plays(par, "participation")
    all_pbp_keys = _integral_play_id(pbp.select("game_id", "play_id"), "play_id", "pbp")
    scoped = _integral_play_id(_player_play_scope(pbp), "play_id", "pbp").select(
        "game_id",
        "play_id",
        "season",
        "epa",
        "passer_id",
        "receiver_id",
        (pl.col("qb_dropback") == 1).fill_null(False).alias("_dropback"),
        _target_expr().fill_null(False).alias("_attempt"),
    )
    plays = scoped.join(par, on=["game_id", "play_id"], how="inner").with_columns(
        _coverage_expr().alias("_coverage")
    )

    raw_label = pl.col("defense_man_zone_type")
    unknown = plays.filter(
        raw_label.is_not_null() & (raw_label != "") & ~raw_label.is_in(list(_COVERAGE_LABELS))
    )
    if unknown.height:
        _log.warning(
            "participation: unrecognized defense_man_zone_type value(s) %s counted as "
            "neither man nor zone",
            sorted(unknown["defense_man_zone_type"].unique().to_list()),
        )
    meta: dict[str, Any] = {
        "participation_rows_without_pbp": par.join(
            all_pbp_keys, on=["game_id", "play_id"], how="anti"
        ).height,
        "participation_scope_plays_without_participation": scoped.join(
            par, on=["game_id", "play_id"], how="anti"
        ).height,
        "participation_unknown_coverage_plays": unknown.height,
        "participation_unknown_coverage_labels": sorted(
            unknown["defense_man_zone_type"].unique().to_list()
        ),
    }

    man = pl.col("_coverage") == "man"
    zone = pl.col("_coverage") == "zone"
    dropback = pl.col("_dropback")

    def _on_field(players_col: str, prefix: str) -> pl.DataFrame:
        return (
            plays.select(
                "game_id",
                "play_id",
                "season",
                "_dropback",
                "_coverage",
                pl.col(players_col).str.split(";").alias("player_id"),
            )
            .explode("player_id", empty_as_null=True)
            .with_columns(pl.col("player_id").str.strip_chars())
            # 2022 has plays with players == '' (n_offense 0), which split to [''].
            .filter(pl.col("player_id").is_not_null() & (pl.col("player_id") != ""))
            .unique(["game_id", "play_id", "player_id"])
            .group_by("player_id", "season")
            .agg(
                pl.len().cast(pl.Int64).alias(f"{prefix}_snaps"),
                _count(dropback).alias(f"{prefix}_dropbacks"),
                _count(dropback & man).alias(f"{prefix}_dropbacks_man"),
                _count(dropback & zone).alias(f"{prefix}_dropbacks_zone"),
            )
        )

    receiving = (
        plays.filter(pl.col("_attempt") & pl.col("receiver_id").is_not_null())
        .group_by(pl.col("receiver_id").alias("player_id"), "season")
        .agg(
            _count(man).alias("targets_man"),
            _count(zone).alias("targets_zone"),
            _sum_where("epa", man).alias("rec_epa_sum_man"),
            _sum_where("epa", zone).alias("rec_epa_sum_zone"),
        )
    )
    passing = (
        plays.filter(dropback & pl.col("passer_id").is_not_null())
        .group_by(pl.col("passer_id").alias("player_id"), "season")
        .agg(
            _count(man).alias("pass_dropbacks_man"),
            _count(zone).alias("pass_dropbacks_zone"),
            _sum_where("epa", man).alias("pass_epa_sum_man"),
            _sum_where("epa", zone).alias("pass_epa_sum_zone"),
        )
    )
    parts = [
        _on_field("offense_players", "off"),
        _on_field("defense_players", "def"),
        receiving,
        passing,
    ]

    keys = pl.concat([p.select("player_id", "season") for p in parts], how="vertical").unique()
    result = keys
    for part in parts:
        result = result.join(part, on=["player_id", "season"], how="left")
    value_cols = [c for c in _PARTICIPATION_COLS if c not in ("player_id", "season")]
    result = result.with_columns(
        pl.col(c).fill_null(0.0 if "_sum_" in c else 0) for c in value_cols
    )
    return result.select(_PARTICIPATION_COLS).sort("player_id", "season"), meta


def _finalize(row: dict[str, Any], now: datetime, hash_fields: list[str]) -> dict[str, Any]:
    row = dict(row)
    row["content_hash"] = hash_row({k: row.get(k) for k in hash_fields})
    row["updated_at"] = now
    return row


def _upsert_staged(
    conn: Any,
    table: str,
    df: pl.DataFrame,
    cols: list[str],
    key_cols: list[str],
    now: datetime,
) -> int:
    value_cols = [c for c in cols if c not in key_cols]
    rows = [_finalize(r, now, value_cols) for r in df.select(cols).to_dicts()]
    rows = filter_changed(conn, table, key_cols, rows)
    return upsert_rows(
        conn,
        table,
        rows,
        conflict_cols=key_cols,
        update_cols=value_cols + ["content_hash", "updated_at"],
    )


def _resolve_pfr_player_ids(conn: Any, pfr_ids: list[str]) -> dict[str, str]:
    if not pfr_ids:
        return {}
    with conn.cursor() as cur:
        cur.execute(
            "SELECT pfr_id, player_id FROM player_id_crosswalk WHERE pfr_id = ANY(%s)",
            (pfr_ids,),
        )
        return dict(cur.fetchall())


class NflverseBulkCollector(Collector):
    name = "nflverse_bulk"

    def __init__(
        self,
        *,
        seasons_override: list[int] | None = None,
        datasets: set[str] | None = None,
    ) -> None:
        """`seasons_override` widens the default `[season - 1, season]` fetch (e.g. for
        a historical backfill). `datasets` scopes fetch/validate/store/should_run to a
        subset of the staged tables (e.g. `{"team_week", "player_week", "snaps"}` for
        what the Efficiency analyst reads) instead of all of them; `None` means all.
        """
        if datasets is not None:
            unknown = datasets - set(_DATASET_SOURCES)
            if unknown:
                raise ValueError(f"unknown dataset(s): {sorted(unknown)}")
        self._live_timestamps: dict[str, str] = {}
        # Set by should_run (what's stale), then fixed by fetch for validate/store. Both
        # stay None on a forced run, which builds every active dataset.
        self._due: set[str] | None = None
        self._building: set[str] | None = None
        self._run_meta: dict[str, Any] = {}
        self.seasons_override = seasons_override
        self.datasets = datasets

    def _active_datasets(self) -> set[str]:
        return self.datasets if self.datasets is not None else set(_DATASET_SOURCES)

    def _build_set(self) -> set[str]:
        return self._building if self._building is not None else self._active_datasets()

    def _fetch_seasons(self, ctx: RunContext) -> list[int]:
        return self.seasons_override or [ctx.season - 1, ctx.season]

    def _participation_seasons(self, ctx: RunContext) -> list[int]:
        """Completed seasons only: nflreadpy raises for the current one, and the release
        is published after each postseason (docs/sources.md)."""
        return [s for s in (self.seasons_override or [ctx.season - 1]) if s < ctx.season]

    def _built_from(self, dataset: str, ctx: RunContext) -> str:
        seasons = (
            self._participation_seasons(ctx)
            if dataset == "participation_player_season"
            else self._fetch_seasons(ctx)
        )
        parts = [f"{tag}@{self._live_timestamps[tag]}" for tag in _DATASET_SOURCES[dataset]]
        return ";".join([*parts, "seasons=" + ",".join(str(s) for s in seasons)])

    def _is_stale(self, ctx: RunContext, dataset: str) -> bool:
        if dataset in _BUILT_FROM_GATED:
            if dataset == "participation_player_season" and not self._participation_seasons(ctx):
                return False
            stored = get_last_value(ctx.conn, f"nflverse:{dataset}")
            return stored != self._built_from(dataset, ctx)
        (tag,) = _DATASET_SOURCES[dataset]
        return get_last_value(ctx.conn, f"nflverse:{tag}") != self._live_timestamps[tag]

    def should_run(self, ctx: RunContext) -> bool:
        """Core tables stay all-or-nothing, as before P7: if any one is stale, every
        active core table is refetched. participation_player_season is gated on its own
        (_INDEPENDENTLY_GATED). See _BUILT_FROM_GATED for the two-tag rule."""
        active = self._active_datasets()
        self._live_timestamps = {
            tag: _fetch_timestamp(tag)
            for tag in sorted({t for d in active for t in _DATASET_SOURCES[d]})
        }
        core = active - _INDEPENDENTLY_GATED
        due: set[str] = set()
        if any(self._is_stale(ctx, d) for d in sorted(core)):
            due |= core
        due |= {d for d in active & _INDEPENDENTLY_GATED if self._is_stale(ctx, d)}
        self._due = due
        return bool(due)

    def fetch(self, ctx: RunContext) -> dict[str, Any]:
        building = self._due if self._due is not None else self._active_datasets()
        self._building, self._due = building, None
        seasons = self._fetch_seasons(ctx)
        raw: dict[str, Any] = {}
        pbp_seasons: set[int] = set()
        if building & {"team_week", "player_game_pbp"}:
            pbp_seasons |= set(seasons)
        if "participation_player_season" in building:
            participation_seasons = self._participation_seasons(ctx)
            if participation_seasons:
                raw["participation"] = nfl.load_participation(seasons=participation_seasons)
                pbp_seasons |= set(participation_seasons)
        if pbp_seasons:
            raw["pbp"] = nfl.load_pbp(seasons=sorted(pbp_seasons))
        if "player_week" in building:
            raw["player_stats"] = nfl.load_player_stats(seasons=seasons)
        if "snaps" in building:
            raw["snap_counts"] = nfl.load_snap_counts(seasons=seasons)
        if building & {"ftn", "player_game_pbp"}:
            raw["ftn_charting"] = nfl.load_ftn_charting(seasons=seasons)
        if "depth" in building:
            raw["depth_charts"] = nfl.load_depth_charts(seasons=ctx.season)
        if "ngs" in building:
            raw["ngs"] = {
                stat_type: nfl.load_nextgen_stats(seasons=seasons, stat_type=stat_type)
                for stat_type in _NGS_STAT_TYPES
            }
        if "pfr_advstats" in building:
            raw["pfr_advstats"] = {
                stat_type: nfl.load_pfr_advstats(seasons=seasons, stat_type=stat_type)
                for stat_type in _PFR_STAT_TYPES
            }
        return raw

    def validate(self, raw: dict[str, Any]) -> dict[str, pl.DataFrame]:
        building = self._build_set()
        player_pbp = bool(building & {"player_game_pbp", "participation_player_season"})
        for name, required in (
            ("pbp", {"game_id", "posteam", "defteam", "epa", "success"}),
            ("pbp", _PBP_PLAYER_REQUIRED if player_pbp else set()),
            ("player_stats", {"player_id", "game_id", "season_type"}),
            ("snap_counts", {"pfr_player_id", "game_id"}),
            ("ftn_charting", {"nflverse_game_id", "nflverse_play_id"}),
            ("depth_charts", {"team", "dt", "gsis_id"}),
            ("participation", _PARTICIPATION_REQUIRED),
        ):
            if name not in raw:
                continue
            missing = required - set(raw[name].columns)
            if missing:
                raise ValueError(f"{name} missing expected columns: {missing}")

        # A dataset is built only if this run is building it AND its sources were fetched.
        # Raw keys alone aren't enough: pbp is fetched for player_game_pbp and participation
        # too, and a participation-only run's season-1 pbp must not rebuild team_week.
        def _has(dataset: str, *sources: str) -> bool:
            return dataset in building and all(s in raw for s in sources)

        self._run_meta = {}
        validated: dict[str, pl.DataFrame] = {}
        if _has("player_week", "player_stats"):
            validated["player_week"] = _build_player_week(raw["player_stats"])
        if _has("team_week", "pbp"):
            validated["team_week"] = _aggregate_team_week(raw["pbp"])
        if _has("player_game_pbp", "pbp", "ftn_charting"):
            validated["player_game_pbp"], meta = _aggregate_player_game_pbp(
                raw["pbp"], raw["ftn_charting"]
            )
            self._run_meta.update(meta)
        if _has("participation_player_season", "pbp", "participation"):
            validated["participation_player_season"], meta = _aggregate_participation_player_season(
                raw["participation"], raw["pbp"]
            )
            self._run_meta.update(meta)
        if _has("snaps", "snap_counts"):
            validated["snaps"] = _build_snaps(raw["snap_counts"])
        if _has("ngs", "ngs"):
            validated["ngs"] = _build_ngs(raw["ngs"])
        if _has("pfr_advstats", "pfr_advstats"):
            validated["pfr_advstats"] = _build_pfr_advstats(raw["pfr_advstats"])
        if _has("ftn", "ftn_charting"):
            validated["ftn"] = _build_ftn(raw["ftn_charting"])
        if _has("depth", "depth_charts"):
            validated["depth"] = _build_depth(raw["depth_charts"])
        return validated

    def store(self, ctx: RunContext, validated: dict[str, pl.DataFrame]) -> WorkResult:
        conn = ctx.conn
        total_written = 0

        if "player_week" in validated:
            player_week_rows = [
                _finalize(
                    r,
                    ctx.now,
                    [c for c in _PLAYER_WEEK_COLS if c not in ("player_id", "game_id")],
                )
                for r in validated["player_week"].to_dicts()
            ]
            player_week_rows = filter_changed(
                conn, "player_week", ["player_id", "game_id"], player_week_rows
            )
            total_written += upsert_rows(
                conn,
                "player_week",
                player_week_rows,
                conflict_cols=["player_id", "game_id"],
                update_cols=[c for c in _PLAYER_WEEK_COLS if c not in ("player_id", "game_id")]
                + ["content_hash", "updated_at"],
            )

        if "team_week" in validated:
            team_week_rows = [
                _finalize(r, ctx.now, [c for c in _TEAM_WEEK_COLS if c not in ("game_id", "team")])
                for r in validated["team_week"].to_dicts()
            ]
            team_week_rows = filter_changed(conn, "team_week", ["game_id", "team"], team_week_rows)
            total_written += upsert_rows(
                conn,
                "team_week",
                team_week_rows,
                conflict_cols=["game_id", "team"],
                update_cols=[c for c in _TEAM_WEEK_COLS if c not in ("game_id", "team")]
                + ["content_hash", "updated_at"],
            )

        if "ngs" in validated:
            ngs_cols = [
                "player_id",
                "season",
                "week",
                "season_type",
                "stat_type",
                "team",
            ] + _NGS_ALL_METRIC_COLS
            ngs_rows = [
                _finalize(
                    r,
                    ctx.now,
                    [
                        c
                        for c in ngs_cols
                        if c not in ("player_id", "season", "week", "season_type", "stat_type")
                    ],
                )
                for r in validated["ngs"].to_dicts()
            ]
            ngs_rows = filter_changed(
                conn, "ngs", ["player_id", "season", "week", "season_type", "stat_type"], ngs_rows
            )
            total_written += upsert_rows(
                conn,
                "ngs",
                ngs_rows,
                conflict_cols=["player_id", "season", "week", "season_type", "stat_type"],
                update_cols=[
                    c
                    for c in ngs_cols
                    if c not in ("player_id", "season", "week", "season_type", "stat_type")
                ]
                + ["content_hash", "updated_at"],
            )

        if "ftn" in validated:
            ftn_rows = [
                _finalize(r, ctx.now, [c for c in _FTN_COLS if c not in ("game_id", "play_id")])
                for r in validated["ftn"].to_dicts()
            ]
            ftn_rows = filter_changed(conn, "ftn", ["game_id", "play_id"], ftn_rows)
            total_written += upsert_rows(
                conn,
                "ftn",
                ftn_rows,
                conflict_cols=["game_id", "play_id"],
                update_cols=[c for c in _FTN_COLS if c not in ("game_id", "play_id")]
                + ["content_hash", "updated_at"],
            )

        if "pfr_advstats" in validated:
            pfr_df = validated["pfr_advstats"]
            pfr_ids = pfr_df["pfr_player_id"].unique().to_list()
            pfr_crosswalk = _resolve_pfr_player_ids(conn, pfr_ids)
            pfr_cols = [
                "game_id",
                "pfr_player_id",
                "season",
                "week",
                "season_type",
                "stat_type",
                "team",
                "opponent_team",
            ] + _PFR_ALL_METRIC_COLS
            pfr_rows = []
            for r in pfr_df.to_dicts():
                r = dict(r)
                r["player_id"] = pfr_crosswalk.get(r["pfr_player_id"])
                pfr_rows.append(
                    _finalize(
                        r,
                        ctx.now,
                        [c for c in pfr_cols if c not in ("game_id", "pfr_player_id", "stat_type")]
                        + ["player_id"],
                    )
                )
            pfr_rows = filter_changed(
                conn, "pfr_advstats", ["game_id", "pfr_player_id", "stat_type"], pfr_rows
            )
            total_written += upsert_rows(
                conn,
                "pfr_advstats",
                pfr_rows,
                conflict_cols=["game_id", "pfr_player_id", "stat_type"],
                update_cols=[
                    c for c in pfr_cols if c not in ("game_id", "pfr_player_id", "stat_type")
                ]
                + ["player_id", "content_hash", "updated_at"],
            )

        if "snaps" in validated:
            snaps_df = validated["snaps"]
            snap_pfr_ids = snaps_df["pfr_player_id"].unique().to_list()
            snap_crosswalk = _resolve_pfr_player_ids(conn, snap_pfr_ids)
            snap_cols = [
                "game_id",
                "pfr_player_id",
                "season",
                "week",
                "season_type",
                "team",
                "opponent_team",
                "position",
                "offense_snaps",
                "offense_pct",
                "defense_snaps",
                "defense_pct",
                "st_snaps",
                "st_pct",
            ]
            snap_rows = []
            for r in snaps_df.to_dicts():
                r = dict(r)
                r["player_id"] = snap_crosswalk.get(r["pfr_player_id"])
                snap_rows.append(
                    _finalize(
                        r,
                        ctx.now,
                        [c for c in snap_cols if c not in ("game_id", "pfr_player_id")]
                        + ["player_id"],
                    )
                )
            snap_rows = filter_changed(conn, "snaps", ["game_id", "pfr_player_id"], snap_rows)
            total_written += upsert_rows(
                conn,
                "snaps",
                snap_rows,
                conflict_cols=["game_id", "pfr_player_id"],
                update_cols=[c for c in snap_cols if c not in ("game_id", "pfr_player_id")]
                + ["player_id", "content_hash", "updated_at"],
            )

        if "depth" in validated:
            depth_cols = ["team", "pos_grp", "pos_abb", "pos_rank", "player_id", "as_of"]
            depth_rows = [
                _finalize(
                    r,
                    ctx.now,
                    [c for c in depth_cols if c not in ("team", "pos_grp", "pos_abb", "pos_rank")],
                )
                for r in validated["depth"].to_dicts()
            ]
            depth_rows = filter_changed(
                conn, "depth", ["team", "pos_grp", "pos_abb", "pos_rank"], depth_rows
            )
            total_written += upsert_rows(
                conn,
                "depth",
                depth_rows,
                conflict_cols=["team", "pos_grp", "pos_abb", "pos_rank"],
                update_cols=[
                    c for c in depth_cols if c not in ("team", "pos_grp", "pos_abb", "pos_rank")
                ]
                + ["content_hash", "updated_at"],
            )

        if "player_game_pbp" in validated:
            total_written += _upsert_staged(
                conn,
                "player_game_pbp",
                validated["player_game_pbp"],
                _PLAYER_GAME_PBP_COLS,
                ["game_id", "player_id"],
                ctx.now,
            )

        if "participation_player_season" in validated:
            total_written += _upsert_staged(
                conn,
                "participation_player_season",
                validated["participation_player_season"],
                _PARTICIPATION_COLS,
                ["player_id", "season"],
                ctx.now,
            )

        # Advance only what this run actually rebuilt. A tag's key moves when the one table
        # that owns it (a single-tag dataset) was stored; a _BUILT_FROM_GATED table records
        # its own built-from key. Nothing is recorded on a forced run (no live timestamps),
        # as before.
        for dataset in validated:
            tags = _DATASET_SOURCES[dataset]
            if not all(t in self._live_timestamps for t in tags):
                continue
            if len(tags) == 1:
                set_last_value(conn, f"nflverse:{tags[0]}", self._live_timestamps[tags[0]])
            if dataset in _BUILT_FROM_GATED:
                set_last_value(conn, f"nflverse:{dataset}", self._built_from(dataset, ctx))

        return WorkResult(total_written, dict(self._run_meta))
