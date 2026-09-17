"""
Job: Load canonical games, teams, players, and the provider ID crosswalk into the spine.
Reads: nflreadpy load_schedules/load_teams/load_players/load_ff_playerids;
       nflverse-data release timestamp.json for the schedules/teams/players tags
Writes: teams, games, players, player_id_crosswalk, source_freshness
Tier: T2
Phase: P1
"""

from __future__ import annotations

from typing import Any

import httpx
import nflreadpy as nfl
import polars as pl

from pipeline.core.base import Collector, RunContext
from pipeline.core.db import filter_changed, upsert_rows
from pipeline.core.freshness import get_last_value, set_last_value
from pipeline.core.hashing import hash_row

_TIMESTAMP_URL = "https://github.com/nflverse/nflverse-data/releases/download/{tag}/timestamp.json"
_TRACKED_TAGS = ("schedules", "teams", "players")

_SCHEDULE_REQUIRED = {"game_id", "season", "week", "game_type", "away_team", "home_team"}
_TEAM_REQUIRED = {"team_abbr", "team_name"}
_PLAYER_REQUIRED = {"gsis_id", "display_name"}
_FF_REQUIRED = {"gsis_id"}

_GAME_COLS = [
    "game_id",
    "season",
    "week",
    "season_type",
    "game_type",
    "gameday",
    "weekday",
    "gametime",
    "away_team",
    "home_team",
    "away_score",
    "home_score",
    "result",
    "total",
    "overtime",
    "roof",
    "surface",
    "temp",
    "wind",
    "away_qb_id",
    "home_qb_id",
    "away_rest",
    "home_rest",
    "div_game",
    "stadium_id",
    "stadium",
    "spread_line",
    "total_line",
    "old_game_id",
    "gsis",
    "nfl_detail_id",
    "pfr",
    "pff",
    "espn",
    "ftn",
]
_PLAYER_COLS = [
    "gsis_id",
    "display_name",
    "first_name",
    "last_name",
    "position",
    "position_group",
    "birth_date",
    "college_name",
    "height",
    "weight",
    "rookie_season",
    "last_season",
    "latest_team",
    "status",
]


def _fetch_timestamp(tag: str) -> str:
    resp = httpx.get(_TIMESTAMP_URL.format(tag=tag), follow_redirects=True, timeout=30)
    resp.raise_for_status()
    return str(resp.json()["last_updated"])


def _finalize(row: dict[str, Any], now: Any, hash_fields: list[str]) -> dict[str, Any]:
    row = dict(row)
    row["content_hash"] = hash_row({k: row.get(k) for k in hash_fields})
    row["updated_at"] = now
    return row


class IdSpineCollector(Collector):
    name = "id_spine"

    def __init__(self) -> None:
        self._live_timestamps: dict[str, str] = {}

    def should_run(self, ctx: RunContext) -> bool:
        changed = False
        for tag in _TRACKED_TAGS:
            live_value = _fetch_timestamp(tag)
            self._live_timestamps[tag] = live_value
            if get_last_value(ctx.conn, f"nflverse:{tag}") != live_value:
                changed = True
        return changed

    def fetch(self, ctx: RunContext) -> dict[str, pl.DataFrame]:
        return {
            "schedules": nfl.load_schedules(seasons=[ctx.season - 1, ctx.season]),
            "teams": nfl.load_teams(),
            "players": nfl.load_players(),
            "ff_playerids": nfl.load_ff_playerids(),
        }

    def validate(self, raw: dict[str, pl.DataFrame]) -> dict[str, pl.DataFrame]:
        schedules, teams, players, ff_ids = (
            raw["schedules"],
            raw["teams"],
            raw["players"],
            raw["ff_playerids"],
        )

        for df, required, name in (
            (schedules, _SCHEDULE_REQUIRED, "schedules"),
            (teams, _TEAM_REQUIRED, "teams"),
            (players, _PLAYER_REQUIRED, "players"),
            (ff_ids, _FF_REQUIRED, "ff_playerids"),
        ):
            missing = required - set(df.columns)
            if missing:
                raise ValueError(f"{name} missing expected columns: {missing}")

        schedules = schedules.filter(
            pl.col("game_id").is_not_null()
            & pl.col("away_team").is_not_null()
            & pl.col("home_team").is_not_null()
        ).with_columns(
            pl.when(pl.col("game_type") == "REG")
            .then(pl.lit("REG"))
            .otherwise(pl.lit("POST"))
            .alias("season_type"),
            # nflreadpy returns these as Int32 (0/1); games.overtime/div_game are boolean.
            pl.col("overtime").cast(pl.Boolean),
            pl.col("div_game").cast(pl.Boolean),
        )
        teams = teams.filter(pl.col("team_abbr").is_not_null())
        players = players.filter(pl.col("gsis_id").is_not_null())

        # Known trap (docs/sources.md): DynastyProcess's ff_playerids has a handful of
        # gsis_id collisions across unrelated retired/FA players. Keep the row with a
        # sleeper_id when there's a duplicate, since that's the only reason we join this
        # source at all; otherwise keep the first.
        ff_ids = (
            ff_ids.filter(pl.col("gsis_id").is_not_null())
            .with_columns(pl.col("sleeper_id").is_null().alias("_no_sleeper"))
            .sort(["gsis_id", "_no_sleeper"])
            .unique(subset=["gsis_id"], keep="first")
            .drop("_no_sleeper")
        )

        return {"schedules": schedules, "teams": teams, "players": players, "ff_playerids": ff_ids}

    def store(self, ctx: RunContext, validated: dict[str, pl.DataFrame]) -> int:
        conn = ctx.conn
        schedules, teams_df, players_df, ff_ids = (
            validated["schedules"],
            validated["teams"],
            validated["players"],
            validated["ff_playerids"],
        )
        total_written = 0

        team_rows = [
            _finalize(r, ctx.now, ["team_name", "team_nick", "team_conf", "team_division"])
            for r in teams_df.select(
                ["team_abbr", "team_name", "team_nick", "team_conf", "team_division"]
            ).to_dicts()
        ]
        team_rows = filter_changed(conn, "teams", "team_abbr", team_rows)
        total_written += upsert_rows(
            conn,
            "teams",
            team_rows,
            conflict_cols=["team_abbr"],
            update_cols=[
                "team_name",
                "team_nick",
                "team_conf",
                "team_division",
                "content_hash",
                "updated_at",
            ],
        )

        game_rows = [
            _finalize(r, ctx.now, [c for c in _GAME_COLS if c != "game_id"])
            for r in schedules.select(_GAME_COLS).to_dicts()
        ]
        game_rows = filter_changed(conn, "games", "game_id", game_rows)
        total_written += upsert_rows(
            conn,
            "games",
            game_rows,
            conflict_cols=["game_id"],
            update_cols=[c for c in _GAME_COLS if c != "game_id"] + ["content_hash", "updated_at"],
        )

        player_rows = [
            _finalize(r, ctx.now, [c for c in _PLAYER_COLS if c != "gsis_id"])
            for r in players_df.select(_PLAYER_COLS).rename({"gsis_id": "player_id"}).to_dicts()
        ]
        player_rows = filter_changed(conn, "players", "player_id", player_rows)
        total_written += upsert_rows(
            conn,
            "players",
            player_rows,
            conflict_cols=["player_id"],
            update_cols=[c for c in _PLAYER_COLS if c != "gsis_id"]
            + ["content_hash", "updated_at"],
        )

        crosswalk_df = players_df.select(
            ["gsis_id", "esb_id", "nfl_id", "pfr_id", "pff_id", "otc_id", "espn_id"]
        ).join(
            ff_ids.select(["gsis_id", "sleeper_id", "yahoo_id", "mfl_id"]),
            on="gsis_id",
            how="left",
        )
        crosswalk_cols = [
            "esb_id",
            "nfl_id",
            "pfr_id",
            "pff_id",
            "otc_id",
            "espn_id",
            "sleeper_id",
            "yahoo_id",
            "mfl_id",
        ]
        crosswalk_rows = [
            _finalize(r, ctx.now, crosswalk_cols)
            for r in crosswalk_df.rename({"gsis_id": "player_id"}).to_dicts()
        ]
        crosswalk_rows = filter_changed(conn, "player_id_crosswalk", "player_id", crosswalk_rows)
        total_written += upsert_rows(
            conn,
            "player_id_crosswalk",
            crosswalk_rows,
            conflict_cols=["player_id"],
            update_cols=crosswalk_cols + ["content_hash", "updated_at"],
        )

        for tag, value in self._live_timestamps.items():
            set_last_value(conn, f"nflverse:{tag}", value)

        return total_written
