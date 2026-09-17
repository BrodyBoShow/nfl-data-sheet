"""
Job: Load nflverse bulk stats (pbp-derived team aggregates, player-week box+efficiency
     stats, snap counts, next-gen stats, FTN charting, PFR advanced stats, current depth
     charts) into staged tables for the Phase 2 analysts. Never stores raw play-by-play.
Reads: nflreadpy load_pbp/load_player_stats/load_snap_counts/load_nextgen_stats/
       load_ftn_charting/load_depth_charts/load_pfr_advstats;
       nflverse-data release timestamp.json for each source's tag
Writes: player_week, team_week, snaps, ngs, ftn, pfr_advstats, depth, source_freshness
Tier: T2
Phase: P2
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

import httpx
import nflreadpy as nfl
import polars as pl

from pipeline.core.base import Collector, RunContext
from pipeline.core.db import filter_changed, upsert_rows
from pipeline.core.freshness import get_last_value, set_last_value
from pipeline.core.hashing import hash_row

_log = logging.getLogger(__name__)

_TIMESTAMP_URL = "https://github.com/nflverse/nflverse-data/releases/download/{tag}/timestamp.json"
# Real release tags, confirmed live against nflreadpy's own download path (docs/sources.md
# "IMPORTANT" note) -- several differ from the function/stat_type name, e.g. load_player_stats
# downloads from tag `stats_player`, not `player_stats`. The full set of tracked tags is
# `set(_DATASET_SOURCES.values())`, defined below alongside the dataset-name mapping.

_SEASON_TYPES = ("REG", "POST")

# Maps each staged table to the release tag it depends on (a subset of _TRACKED_TAGS) --
# used by --datasets to scope a run (fetch/validate/store/should_run) to just the tables
# a given analyst actually reads, e.g. Efficiency only needs team_week/player_week/snaps.
_DATASET_SOURCES: dict[str, str] = {
    "team_week": "pbp",
    "player_week": "stats_player",
    "snaps": "snap_counts",
    "ftn": "ftn_charting",
    "depth": "depth_charts",
    "ngs": "nextgen_stats",
    "pfr_advstats": "pfr_advstats",
}

# Garbage time = win probability for the team with the ball is a near-lock either way.
# Judgment call (docs/signals.md doesn't pin an exact definition) -- simple, deterministic,
# and uses a column nflverse already computes rather than hand-tuned score/time thresholds.
_GARBAGE_TIME_WP_LOW = 0.05
_GARBAGE_TIME_WP_HIGH = 0.95

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

# snap_counts/pfr_advstats (both PFR-sourced) still carry retired franchise codes for
# historical seasons that pbp/player_stats/nextgen_stats already normalize -- verified
# live (docs/sources.md): 2019 snap_counts/pfr_advstats show 'OAK', 2016 snap_counts
# shows 'SD', 2015 snap_counts shows 'STL', while pbp/player_stats/nextgen_stats already
# show the current code for the same seasons/games. Without normalizing here, a team's
# prior-season join (pipeline/analysts/efficiency.py) would silently miss a relocated
# franchise's history.
_TEAM_ABBR_ALIASES: dict[str, str] = {"OAK": "LV", "SD": "LAC", "STL": "LA"}
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
    ],
    "rushing": [
        "rush_yards_over_expected",
        "rush_yards_over_expected_per_att",
        "percent_attempts_gte_eight_defenders",
    ],
    "receiving": ["avg_separation", "avg_yac_above_expectation"],
}
_NGS_ALL_METRIC_COLS = sorted({c for cols in _NGS_METRIC_COLS_BY_TYPE.values() for c in cols})

_PFR_STAT_TYPES = ("pass", "rush", "rec", "def")
_PFR_METRIC_COLS_BY_TYPE = {
    "pass": [
        "passing_bad_throw_pct",
        "passing_drop_pct",
        "times_pressured_pct",
        "times_blitzed",
        "times_hurried",
        "times_hit",
    ],
    "rush": [
        "rushing_yards_before_contact_avg",
        "rushing_yards_after_contact_avg",
        "rushing_broken_tackles",
    ],
    "rec": ["receiving_broken_tackles", "receiving_drop_pct"],
    "def": ["def_pressures", "def_missed_tackle_pct", "def_passer_rating_allowed"],
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
]


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
    return (pl.col("wp") < _GARBAGE_TIME_WP_LOW) | (pl.col("wp") > _GARBAGE_TIME_WP_HIGH)


def _normalize_team_abbr(df: pl.DataFrame, cols: list[str]) -> pl.DataFrame:
    """Map retired franchise codes (_TEAM_ABBR_ALIASES) to their current abbreviation
    in every listed column that's actually present."""
    present = [c for c in cols if c in df.columns]
    if not present:
        return df
    return df.with_columns(pl.col(c).replace(_TEAM_ABBR_ALIASES) for c in present)


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
        )
        missing = [c for c in _NGS_ALL_METRIC_COLS if c not in metric_cols]
        part = part.with_columns(
            [pl.lit(None, dtype=pl.Float64).alias(c) for c in missing]
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


def _finalize(row: dict[str, Any], now: datetime, hash_fields: list[str]) -> dict[str, Any]:
    row = dict(row)
    row["content_hash"] = hash_row({k: row.get(k) for k in hash_fields})
    row["updated_at"] = now
    return row


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
        subset of the 7 staged tables (e.g. `{"team_week", "player_week", "snaps"}` for
        what the Efficiency analyst reads) instead of all of them; `None` means all.
        """
        if datasets is not None:
            unknown = datasets - set(_DATASET_SOURCES)
            if unknown:
                raise ValueError(f"unknown dataset(s): {sorted(unknown)}")
        self._live_timestamps: dict[str, str] = {}
        self.seasons_override = seasons_override
        self.datasets = datasets

    def _active_datasets(self) -> set[str]:
        return self.datasets if self.datasets is not None else set(_DATASET_SOURCES)

    def should_run(self, ctx: RunContext) -> bool:
        active_tags = {_DATASET_SOURCES[d] for d in self._active_datasets()}
        changed = False
        for tag in active_tags:
            live_value = _fetch_timestamp(tag)
            self._live_timestamps[tag] = live_value
            if get_last_value(ctx.conn, f"nflverse:{tag}") != live_value:
                changed = True
        return changed

    def fetch(self, ctx: RunContext) -> dict[str, Any]:
        seasons = self.seasons_override or [ctx.season - 1, ctx.season]
        active = self._active_datasets()
        raw: dict[str, Any] = {}
        if "team_week" in active:
            raw["pbp"] = nfl.load_pbp(seasons=seasons)
        if "player_week" in active:
            raw["player_stats"] = nfl.load_player_stats(seasons=seasons)
        if "snaps" in active:
            raw["snap_counts"] = nfl.load_snap_counts(seasons=seasons)
        if "ftn" in active:
            raw["ftn_charting"] = nfl.load_ftn_charting(seasons=seasons)
        if "depth" in active:
            raw["depth_charts"] = nfl.load_depth_charts(seasons=ctx.season)
        if "ngs" in active:
            raw["ngs"] = {
                stat_type: nfl.load_nextgen_stats(seasons=seasons, stat_type=stat_type)
                for stat_type in _NGS_STAT_TYPES
            }
        if "pfr_advstats" in active:
            raw["pfr_advstats"] = {
                stat_type: nfl.load_pfr_advstats(seasons=seasons, stat_type=stat_type)
                for stat_type in _PFR_STAT_TYPES
            }
        return raw

    def validate(self, raw: dict[str, Any]) -> dict[str, pl.DataFrame]:
        for name, required in (
            ("pbp", {"game_id", "posteam", "defteam", "epa", "success"}),
            ("player_stats", {"player_id", "game_id", "season_type"}),
            ("snap_counts", {"pfr_player_id", "game_id"}),
            ("ftn_charting", {"nflverse_game_id", "nflverse_play_id"}),
            ("depth_charts", {"team", "dt", "gsis_id"}),
        ):
            if name not in raw:
                continue
            missing = required - set(raw[name].columns)
            if missing:
                raise ValueError(f"{name} missing expected columns: {missing}")

        validated: dict[str, pl.DataFrame] = {}
        if "player_stats" in raw:
            validated["player_week"] = _build_player_week(raw["player_stats"])
        if "pbp" in raw:
            validated["team_week"] = _aggregate_team_week(raw["pbp"])
        if "snap_counts" in raw:
            validated["snaps"] = _build_snaps(raw["snap_counts"])
        if "ngs" in raw:
            validated["ngs"] = _build_ngs(raw["ngs"])
        if "pfr_advstats" in raw:
            validated["pfr_advstats"] = _build_pfr_advstats(raw["pfr_advstats"])
        if "ftn_charting" in raw:
            validated["ftn"] = _build_ftn(raw["ftn_charting"])
        if "depth_charts" in raw:
            validated["depth"] = _build_depth(raw["depth_charts"])
        return validated

    def store(self, ctx: RunContext, validated: dict[str, pl.DataFrame]) -> int:
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
                        [
                            c
                            for c in pfr_cols
                            if c not in ("game_id", "pfr_player_id", "stat_type")
                        ]
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

        for tag, value in self._live_timestamps.items():
            set_last_value(conn, f"nflverse:{tag}", value)

        return total_written
