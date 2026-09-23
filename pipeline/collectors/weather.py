"""
Job: Fetch the Open-Meteo hourly forecast for each game whose weather-snapshot target is
     due (pipeline/collectors/weather_schedule.py decides) and store it append-only,
     after a venue guard confirms the game's stadium row is the right place to fetch for.
Reads: Open-Meteo /v1/forecast, games, stadiums, weather_snapshot_targets
Writes: weather_snapshots, weather_snapshot_targets (capture/skip/miss bookkeeping)
Tier: T1
Phase: P4
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from typing import Any, Literal

import httpx
import psycopg

from pipeline.collectors import weather_schedule
from pipeline.collectors.weather_schedule import Target
from pipeline.core.base import Collector, RunContext, WorkResult
from pipeline.core.db import filter_changed, upsert_rows
from pipeline.core.hashing import hash_row

_FORECAST_URL = "https://api.open-meteo.com/v1/forecast"

# Open-Meteo variable -> weather_snapshots column. Exactly 10 variables: more than 10
# makes a request count as >1 call against the free tier (docs/sources.md).
_VARIABLES: dict[str, str] = {
    "temperature_2m": "temperature_2m_f",
    "apparent_temperature": "apparent_temperature_f",
    "precipitation": "precipitation_in",
    "precipitation_probability": "precipitation_probability_pct",
    "rain": "rain_in",
    "snowfall": "snowfall_in",
    "weather_code": "weather_code",
    "wind_speed_10m": "wind_speed_10m_mph",
    "wind_gusts_10m": "wind_gusts_10m_mph",
    "wind_direction_10m": "wind_direction_10m_deg",
}

# hourly_units as verified live 2026-09-23 with the request params below. Asserted in
# validate() -- a unit change is schema drift, not something to store and hope.
_EXPECTED_UNITS: dict[str, str] = {
    "time": "iso8601",
    "temperature_2m": "°F",
    "apparent_temperature": "°F",
    "precipitation": "inch",
    "precipitation_probability": "%",
    "rain": "inch",
    "snowfall": "inch",
    "weather_code": "wmo code",
    "wind_speed_10m": "mp/h",
    "wind_gusts_10m": "mp/h",
    "wind_direction_10m": "°",
}

# A null in any of these anywhere in the game window means the target is skipped as
# no_forecast_data -- nothing stored, nothing filled. Other variables may be null.
_REQUIRED_VARIABLES = ("temperature_2m", "wind_speed_10m", "wind_direction_10m")

# Hours stored per snapshot: H..H+4 for a kickoff in hour H. Precip/gusts are
# preceding-hour aggregates, so H+4 covers the game's last hour (docs/sources.md).
WINDOW_HOURS = 5

SkipReason = Literal[
    "fixed_roof", "roof_closed", "name_mismatch", "unknown_stadium", "no_forecast_data"
]

_ROW_COLS = [
    "game_id",
    "target_id",
    "stadium_id",
    "season",
    "week",
    "kickoff",
    "as_of",
    "lead_hours",
    "model_regime_break",
    "valid_time",
    "hour_offset",
    "requested_lat",
    "requested_lon",
    "grid_lat",
    "grid_lon",
    "grid_elevation_m",
    *_VARIABLES.values(),
]
_ROW_PK = ["game_id", "as_of", "valid_time"]


@dataclass(frozen=True)
class GameVenue:
    game_id: str
    season: int
    week: int
    stadium_id: str | None
    stadium_name: str | None  # games.stadium, the string the name guard checks
    roof: str | None  # games.roof for this game (null pre-game at retractable venues)


@dataclass(frozen=True)
class Stadium:
    stadium_id: str
    known_names: tuple[str, ...]
    lat: float
    lon: float
    roof_type: str


@dataclass(frozen=True)
class VenueDecision:
    fetch: bool
    skip_reason: SkipReason | None = None
    roof_conflict: bool = False  # open venue, but games.roof claims dome/closed


@dataclass
class _Planned:
    target: Target
    game: GameVenue
    stadium: Stadium | None
    decision: VenueDecision


@dataclass
class _Plan:
    items: list[_Planned] = field(default_factory=list)


class NoForecastData(Exception):
    """Open-Meteo has no usable hourly data for this game window (HTTP 400 out of range,
    or nulls in a required variable). The target is skipped, never filled in."""


def resolve_venue(game: GameVenue, stadium: Stadium | None) -> VenueDecision:
    """Pure. The name guard runs before the roof rule: a game whose stadium name doesn't
    match its stadium_id's known names (e.g. 2026_05_PHI_JAX, JAX00 but "Tottenham
    Hotspur Stadium") is never fetched with that row's coords, whatever its roof."""
    if stadium is None:
        return VenueDecision(False, "unknown_stadium")
    if game.stadium_name is None or game.stadium_name not in stadium.known_names:
        return VenueDecision(False, "name_mismatch")
    if stadium.roof_type == "fixed":
        return VenueDecision(False, "fixed_roof")
    if stadium.roof_type == "retractable" and game.roof == "closed":
        return VenueDecision(False, "roof_closed")
    # Retractable with roof null/open is fetched -- nflverse leaves games.roof null
    # pre-game, so the Environment analyst labels these "if roof open". An open venue
    # nflverse calls dome/closed (MCG, Stade de France, Munich) is fetched: the structural
    # roof_type wins, and the conflict is surfaced for the auditor.
    conflict = stadium.roof_type == "open" and game.roof in ("dome", "closed")
    return VenueDecision(True, None, conflict)


def game_window(kickoff: dt.datetime) -> list[dt.datetime]:
    """Pure. The WINDOW_HOURS UTC forecast hours stored for a kickoff: its hour H
    (kickoff floored to the hour) through H + WINDOW_HOURS - 1."""
    start = kickoff.astimezone(dt.UTC).replace(minute=0, second=0, microsecond=0)
    return [start + dt.timedelta(hours=i) for i in range(WINDOW_HOURS)]


def _hour_param(t: dt.datetime) -> str:
    return t.strftime("%Y-%m-%dT%H:%M")


def parse_forecast(body: dict[str, Any], expected_times: list[dt.datetime]) -> dict[str, Any]:
    """Pure. Validates one Open-Meteo response for one game window and returns
    {grid_lat, grid_lon, grid_elevation_m, hours: [ {valid_time, <column>: value, ...} ]}.
    Raises ValueError on schema drift (units, missing keys, unexpected hours) and
    NoForecastData when a required variable is null anywhere in the window."""
    units = body.get("hourly_units") or {}
    for var, unit in _EXPECTED_UNITS.items():
        if units.get(var) != unit:
            raise ValueError(
                f"Open-Meteo unit for {var!r} is {units.get(var)!r}, expected {unit!r}"
            )

    hourly = body.get("hourly")
    if not isinstance(hourly, dict) or "time" not in hourly:
        raise ValueError("Open-Meteo response has no hourly.time")
    missing = [v for v in _VARIABLES if v not in hourly]
    if missing:
        raise ValueError(f"Open-Meteo response missing hourly variables: {missing}")

    # With timezone=UTC the times are naive ISO strings with no 'Z' (verified live) --
    # attach UTC explicitly, never let them be read as local time.
    times = [dt.datetime.fromisoformat(t).replace(tzinfo=dt.UTC) for t in hourly["time"]]
    if times != expected_times:
        raise ValueError(f"Open-Meteo returned hours {times}, expected {expected_times}")

    for var in _REQUIRED_VARIABLES:
        if any(v is None for v in hourly[var]):
            raise NoForecastData(f"null {var} in game window")

    hours = []
    for i, valid_time in enumerate(times):
        row: dict[str, Any] = {"valid_time": valid_time, "hour_offset": i}
        for var, col in _VARIABLES.items():
            row[col] = hourly[var][i]
        hours.append(row)

    return {
        "grid_lat": body["latitude"],
        "grid_lon": body["longitude"],
        "grid_elevation_m": body.get("elevation"),
        "hours": hours,
    }


def build_rows(
    planned_target: Target,
    game: GameVenue,
    stadium: Stadium,
    parsed: dict[str, Any],
    as_of: dt.datetime,
) -> list[dict[str, Any]]:
    """Pure. One weather_snapshots row per forecast hour, content-hashed."""
    lead_hours = round((planned_target.kickoff - as_of).total_seconds() / 3600, 2)
    hash_fields = [c for c in _ROW_COLS if c not in _ROW_PK]
    rows = []
    for hour in parsed["hours"]:
        row: dict[str, Any] = {
            "game_id": game.game_id,
            "target_id": planned_target.target_id,
            "stadium_id": stadium.stadium_id,
            "season": game.season,
            "week": game.week,
            "kickoff": planned_target.kickoff,
            "as_of": as_of,
            "lead_hours": lead_hours,
            "model_regime_break": (
                planned_target.target_id in weather_schedule.MODEL_REGIME_BREAK_TARGETS
            ),
            "requested_lat": stadium.lat,
            "requested_lon": stadium.lon,
            "grid_lat": parsed["grid_lat"],
            "grid_lon": parsed["grid_lon"],
            "grid_elevation_m": parsed["grid_elevation_m"],
            **hour,
        }
        row["content_hash"] = hash_row({k: row[k] for k in hash_fields})
        row["updated_at"] = as_of
        rows.append(row)
    return rows


def _load_games(conn: psycopg.Connection, game_ids: list[str]) -> dict[str, GameVenue]:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT game_id, season, week, stadium_id, stadium, roof FROM games "
            "WHERE game_id = ANY(%s)",
            (game_ids,),
        )
        return {row[0]: GameVenue(*row) for row in cur.fetchall()}


def _load_stadiums(conn: psycopg.Connection, stadium_ids: list[str]) -> dict[str, Stadium]:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT stadium_id, known_names, lat, lon, roof_type FROM stadiums "
            "WHERE stadium_id = ANY(%s)",
            (stadium_ids,),
        )
        return {
            sid: Stadium(sid, tuple(names), lat, lon, roof)
            for sid, names, lat, lon, roof in cur.fetchall()
        }


class WeatherCollector(Collector):
    name = "weather"

    def __init__(self) -> None:
        self._plan = _Plan()

    def should_run(self, ctx: RunContext) -> bool:
        conn = ctx.conn
        weather_schedule.ensure_targets(conn, weather_schedule.upcoming_games(conn, ctx.now))
        decision = weather_schedule.decide(
            weather_schedule.load_open_pending(conn, ctx.now), ctx.now
        )
        for target, reason in decision.missed:
            weather_schedule.record_missed(conn, target, reason, ctx.now)

        self._plan = _Plan()
        if not decision.due:
            return False

        games = _load_games(conn, sorted({t.game_id for t in decision.due}))
        stadiums = _load_stadiums(
            conn, sorted({g.stadium_id for g in games.values() if g.stadium_id})
        )
        for target in decision.due:
            game = games[target.game_id]
            stadium = stadiums.get(game.stadium_id) if game.stadium_id else None
            self._plan.items.append(_Planned(target, game, stadium, resolve_venue(game, stadium)))
        return True

    def fetch(self, ctx: RunContext) -> list[dict[str, Any]]:
        """One request per game to fetch -- each game needs its own start/end hour, and a
        failure on one must not sink the rest. Returns per-item outcomes; only a
        non-400 HTTP/transport error is recorded as an error (target stays pending and
        is retried on a later tick while its window is still open)."""
        results: list[dict[str, Any]] = []
        for item in self._plan.items:
            if not item.decision.fetch:
                results.append({"item": item})
                continue
            assert item.stadium is not None
            window = game_window(item.target.kickoff)
            params: dict[str, str | float] = {
                "latitude": item.stadium.lat,
                "longitude": item.stadium.lon,
                "hourly": ",".join(_VARIABLES),
                "wind_speed_unit": "mph",
                "temperature_unit": "fahrenheit",
                "precipitation_unit": "inch",
                "timezone": "UTC",
                "start_hour": _hour_param(window[0]),
                "end_hour": _hour_param(window[-1]),
            }
            try:
                resp = httpx.get(_FORECAST_URL, params=params, timeout=20)
            except httpx.HTTPError as exc:
                results.append({"item": item, "error": f"{type(exc).__name__}: {exc}"})
                continue
            if resp.status_code == 400:
                # Verified live: out-of-range start_hour -> 400 {"reason": ..., "error": true}
                results.append({"item": item, "no_data": resp.text[:300]})
            elif resp.status_code != 200:
                results.append({"item": item, "error": f"HTTP {resp.status_code}"})
            else:
                results.append({"item": item, "body": resp.json(), "window": window})
        return results

    def validate(self, raw: list[dict[str, Any]]) -> list[dict[str, Any]]:
        out = []
        for r in raw:
            if "body" in r:
                try:
                    r = {**r, "parsed": parse_forecast(r["body"], r["window"])}
                except NoForecastData as exc:
                    r = {**r, "no_data": str(exc)}
            out.append(r)
        return out

    def store(self, ctx: RunContext, validated: list[dict[str, Any]]) -> WorkResult:
        conn = ctx.conn
        rows: list[dict[str, Any]] = []
        captured: list[str] = []
        skipped: dict[str, list[str]] = {}
        unresolved: list[dict[str, Any]] = []
        roof_conflicts: list[str] = []
        fetch_errors: list[dict[str, str]] = []

        for r in validated:
            item: _Planned = r["item"]
            label = f"{item.target.game_id}:{item.target.target_id}"
            reason: str | None = item.decision.skip_reason
            if reason in ("name_mismatch", "unknown_stadium"):
                unresolved.append(
                    {
                        "game_id": item.game.game_id,
                        "stadium_id": item.game.stadium_id,
                        "games_stadium": item.game.stadium_name,
                        "reason": reason,
                    }
                )
            if item.decision.roof_conflict:
                roof_conflicts.append(item.game.game_id)

            if "error" in r:
                fetch_errors.append({"target": label, "error": r["error"]})
                continue
            if "no_data" in r:
                reason = "no_forecast_data"
            if reason is not None:
                weather_schedule.record_skipped(conn, item.target, reason, ctx.now)
                skipped.setdefault(reason, []).append(label)
                continue

            assert item.stadium is not None
            rows += build_rows(item.target, item.game, item.stadium, r["parsed"], ctx.now)
            captured.append(label)

        # Pass-through like odds_snapshots: as_of is unique per run, so every row is new.
        rows = filter_changed(conn, "weather_snapshots", _ROW_PK, rows)
        hash_fields = [c for c in _ROW_COLS if c not in _ROW_PK]
        written = upsert_rows(
            conn,
            "weather_snapshots",
            rows,
            conflict_cols=_ROW_PK,
            update_cols=hash_fields + ["content_hash", "updated_at"],
        )
        # Mark captured only after the rows are written in this same transaction.
        for r in validated:
            item = r["item"]
            if "parsed" in r:
                weather_schedule.record_captured(conn, item.target, ctx.now)

        return WorkResult(
            written,
            meta={
                "due": len(validated),
                "captured": captured,
                "skipped": skipped,
                "unresolved": unresolved,
                "roof_conflicts": sorted(set(roof_conflicts)),
                "fetch_errors": fetch_errors,
            },
        )
