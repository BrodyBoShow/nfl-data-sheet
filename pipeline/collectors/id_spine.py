"""
Job: Load canonical games, teams, players, and the provider ID crosswalk into the spine.
     Also fills in player_id_crosswalk.sleeper_id gaps (fill-null-only, from Sleeper's
     own self-reported gsis_id) -- still one job, maintaining the crosswalk, not a second
     one; see docs/sources.md's Availability section for why this lives here rather than
     in the Availability collector, which only reads the crosswalk.
Reads: nflreadpy load_schedules/load_teams/load_players/load_ff_playerids;
       nflverse-data release timestamp.json for the schedules/teams/players tags;
       Sleeper players endpoint (crosswalk enrichment only)
Writes: teams, games, players, player_id_crosswalk, source_freshness
Tier: T2
Phase: P1
"""

from __future__ import annotations

from typing import Any

import httpx
import nflreadpy as nfl
import polars as pl

from pipeline.core.base import Collector, RunContext, WorkResult
from pipeline.core.db import filter_changed, upsert_rows
from pipeline.core.freshness import get_last_value, set_last_value
from pipeline.core.hashing import hash_row

_TIMESTAMP_URL = "https://github.com/nflverse/nflverse-data/releases/download/{tag}/timestamp.json"
_TRACKED_TAGS = ("schedules", "teams", "players")

_SLEEPER_URL = "https://api.sleeper.app/v1/players/nfl"
# Distinct from pipeline/collectors/availability.py's own "sleeper:availability" key --
# sharing one key would mean whichever collector runs first "claims" the day's fetch and
# the other silently loses its Sleeper read. Worst case with separate keys: 2 Sleeper
# fetches/day total across the system, trivial for a free, CDN-cached, unauthenticated
# endpoint (docs/sources.md).
_SLEEPER_CROSSWALK_FRESHNESS_KEY = "sleeper:crosswalk_enrich"

# load_teams() is a static "every code ever used" reference table -- verified live
# (docs/phases/P2.md): 36 rows, not 32, every current code plus these four retired
# franchise aliases. Any code enumerating "the current 32 teams" must filter
# `WHERE is_active` rather than `SELECT * FROM teams` (see CLAUDE.md).
_RETIRED_TEAM_CODES = frozenset({"OAK", "SD", "STL", "LAR"})

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
    # 'Home' or 'Neutral' (verified live 2026-09-23: 42 Neutral games 2019-2025 --
    # international REG games, Super Bowls, one WC). The only sourced neutral-site flag;
    # the synthesizer (P5) uses it to drop the home-field term.
    "location",
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


def _build_sleeper_crosswalk_updates(
    sleeper_players: dict[str, Any], current_sleeper_id_by_player: dict[str, str | None]
) -> list[tuple[str, str]]:
    """Pure selection logic (no DB access) -- which (player_id, sleeper_id) pairs should
    be written, fill-null-only. `current_sleeper_id_by_player` maps a candidate gsis_id
    to whatever player_id_crosswalk.sleeper_id currently holds for that player_id; a
    gsis_id absent from it means the player isn't in the crosswalk at all (never create a
    row here, only enrich an existing one). Never overwrites a non-null value --
    load_ff_playerids() (the crosswalk's normal sleeper_id source) lags current-season
    rookies/UDFAs and some veteran backups; Sleeper's own dump often already knows their
    gsis_id directly (measured live, docs/sources.md's Availability section)."""
    updates: list[tuple[str, str]] = []
    for sleeper_id, p in sleeper_players.items():
        gsis_id = (p.get("gsis_id") or "").strip()
        if not gsis_id:
            continue
        if gsis_id not in current_sleeper_id_by_player:
            continue
        if current_sleeper_id_by_player[gsis_id] is not None:
            continue
        updates.append((gsis_id, sleeper_id))
    return updates


def _fetch_current_sleeper_ids(conn: Any, gsis_ids: list[str]) -> dict[str, str | None]:
    if not gsis_ids:
        return {}
    with conn.cursor() as cur:
        cur.execute(
            "SELECT player_id, sleeper_id FROM player_id_crosswalk WHERE player_id = ANY(%s)",
            (gsis_ids,),
        )
        return dict(cur.fetchall())


def _enrich_sleeper_crosswalk(conn: Any, sleeper_players: dict[str, Any]) -> int:
    """Returns the number of crosswalk rows actually updated."""
    candidate_gsis_ids = sorted(
        {
            (p.get("gsis_id") or "").strip()
            for p in sleeper_players.values()
            if (p.get("gsis_id") or "").strip()
        }
    )
    current = _fetch_current_sleeper_ids(conn, candidate_gsis_ids)
    updates = _build_sleeper_crosswalk_updates(sleeper_players, current)
    if not updates:
        return 0
    with conn.cursor() as cur:
        cur.executemany(
            "UPDATE player_id_crosswalk SET sleeper_id = %s "
            "WHERE player_id = %s AND sleeper_id IS NULL",
            [(sleeper_id, player_id) for player_id, sleeper_id in updates],
        )
    return len(updates)


class IdSpineCollector(Collector):
    name = "id_spine"

    def __init__(self, *, seasons_override: list[int] | None = None) -> None:
        """`seasons_override` widens the default `[season - 1, season]` schedules/games
        fetch (e.g. for a historical backfill) -- `games` is the only one of this
        collector's four tables that's season-scoped at all. `teams`/`players`/
        `ff_playerids` each fetch nflreadpy's one wholesale, no-season-argument reference
        table regardless (docs/sources.md: `load_teams()` alone already returns every
        code ever used, current and retired), so a historical `games` range needs no
        corresponding change to them.
        """
        self._live_timestamps: dict[str, str] = {}
        self.seasons_override = seasons_override

    def should_run(self, ctx: RunContext) -> bool:
        changed = False
        for tag in _TRACKED_TAGS:
            live_value = _fetch_timestamp(tag)
            self._live_timestamps[tag] = live_value
            if get_last_value(ctx.conn, f"nflverse:{tag}") != live_value:
                changed = True
        return changed

    def fetch(self, ctx: RunContext) -> dict[str, Any]:
        seasons = self.seasons_override or [ctx.season - 1, ctx.season]

        sleeper_players = None
        today = ctx.now.date().isoformat()
        if get_last_value(ctx.conn, _SLEEPER_CROSSWALK_FRESHNESS_KEY) != today:
            resp = httpx.get(_SLEEPER_URL, timeout=60)
            resp.raise_for_status()
            sleeper_players = resp.json()

        return {
            "schedules": nfl.load_schedules(seasons=seasons),
            "teams": nfl.load_teams(),
            "players": nfl.load_players(),
            "ff_playerids": nfl.load_ff_playerids(),
            "sleeper_players": sleeper_players,
        }

    def validate(self, raw: dict[str, Any]) -> dict[str, Any]:
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

        sleeper_players = raw.get("sleeper_players")
        if sleeper_players is not None and not isinstance(sleeper_players, dict):
            raise ValueError("sleeper_players payload is not a dict")

        return {
            "schedules": schedules,
            "teams": teams,
            "players": players,
            "ff_playerids": ff_ids,
            "sleeper_players": sleeper_players,
        }

    def store(self, ctx: RunContext, validated: dict[str, Any]) -> WorkResult:
        conn = ctx.conn
        schedules, teams_df, players_df, ff_ids = (
            validated["schedules"],
            validated["teams"],
            validated["players"],
            validated["ff_playerids"],
        )
        total_written = 0

        team_rows = [
            _finalize(
                {**r, "is_active": r["team_abbr"] not in _RETIRED_TEAM_CODES},
                ctx.now,
                ["team_name", "team_nick", "team_conf", "team_division", "is_active"],
            )
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
                "is_active",
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

        sleeper_players = validated.get("sleeper_players")
        sleeper_crosswalk_filled = 0
        if sleeper_players:
            sleeper_crosswalk_filled = _enrich_sleeper_crosswalk(conn, sleeper_players)
            set_last_value(conn, _SLEEPER_CROSSWALK_FRESHNESS_KEY, ctx.now.date().isoformat())

        return WorkResult(
            total_written, meta={"sleeper_crosswalk_filled": sleeper_crosswalk_filled}
        )
