"""
Job: Compute per-game environment signals -- weather, venue, rest/travel/timezone --
     for every game kicking off in a rolling window around now, from stored tables.
Reads: games, stadiums, weather_snapshot_targets, weather_snapshots, source_freshness
Writes: signals (sector='environment')
Tier: T1
Phase: P4

Scope is per game, not per dispatcher week (docs/signals.md, "Environment sector"):
weather is a property of a game, and a Thursday game's snapshots are captured before
nflreadpy's current week advances. So compute() covers every game with
now - _LOOKBACK < kickoff <= now + _LOOKAHEAD, whatever ctx.week says; each row carries
its game's own season/week, and stale-row cleanup is scoped to this run's game_ids
(_delete_stale_signals below overrides the base class's ctx.season/ctx.week scope, which
would miss next week's games and wipe finished games' frozen rows).

Wind is Open-Meteo's exterior 10 m estimate, never field-level wind (0020's header) --
every wind signal is a relative indicator. Speed-only is the primary shape; the
along-field/crosswind split is an add-on for hours >= _DIRECTION_MIN_MPH at venues with a
reviewed field_bearing (docs/phases/P4.md).
"""

from __future__ import annotations

import datetime as dt
import logging
import math
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any
from zoneinfo import ZoneInfo

import polars as pl
import psycopg

from pipeline.core.base import Analyst, RunContext, WorkResult
from pipeline.core.db import delete_rows, upsert_rows
from pipeline.core.freshness import get_last_value
from pipeline.core.schedule import kickoff_utc
from pipeline.core.venue import GameVenue, Stadium, VenueDecision, resolve_venue

_log = logging.getLogger(__name__)

SECTOR = "environment"

# Game window, relative to now. The lookback runs past "unfinished" on purpose: it keeps a
# game in scope long enough for weather_status to settle 3 -> 4 after its last target
# (t2) closes at kickoff + 1h. Headline weather values can't change after kickoff
# (post-kickoff captures are excluded), so extra re-runs only update status.
_LOOKBACK = dt.timedelta(hours=24)
_LOOKAHEAD = dt.timedelta(days=7)

# Below this sustained speed, wind direction is noise (docs/phases/P4.md, decided
# 2026-09-23) -- no along-field/crosswind split for that hour. Gusts don't count.
_DIRECTION_MIN_MPH = 8.0

_EARTH_RADIUS_MI = 3958.8

# HRRR CONUS grid, from NOAA's HRRR_conus.domain.txt (docs/sources.md): Lambert conformal,
# sphere R = 6370 km (WRF), true latitude 38.5N, standard longitude 97.5W, centered on
# 38.5N 97.5W, 1799 x 1059 mass points at 3 km. Projecting the file's four corner points
# with these parameters lands exactly on the grid's extents -- that's the check.
_HRRR_R_M = 6_370_000.0
_HRRR_TRUELAT = math.radians(38.5)
_HRRR_LON0 = math.radians(-97.5)
_HRRR_HALF_X_M = (1799 - 1) / 2 * 3000.0
_HRRR_HALF_Y_M = (1059 - 1) / 2 * 3000.0

# Numeric codes -- signals.value is numeric-only (same convention as
# availability_category). Tables mirrored in docs/signals.md.
STATUS_FORECAST = 1.0
STATUS_INDOOR = 2.0
STATUS_AWAITING = 3.0
STATUS_MISSED = 4.0
STATUS_VENUE_UNRESOLVED = 5.0
STATUS_NOT_TRACKED = 6.0

WIND_MODE_SPEED_ONLY_LOW = 1.0  # bearing exists, no hour reaches _DIRECTION_MIN_MPH
WIND_MODE_SPEED_ONLY_NO_BEARING = 2.0  # stadiums.field_bearing is null
WIND_MODE_SPLIT = 3.0

ROOF_FIXED = 1.0
ROOF_RETRACTABLE_CLOSED = 2.0
ROOF_RETRACTABLE_OPEN_OR_UNKNOWN = 3.0  # labeled "if roof open"
ROOF_OPEN_AIR = 4.0

_SURFACE_CODE: dict[str, float] = {
    "grass": 1.0,
    "fieldturf": 2.0,
    "matrixturf": 2.0,
    "sportturf": 2.0,
    "a_turf": 2.0,
    "astroturf": 2.0,
}

DOMAIN_HRRR = 1.0
DOMAIN_UNVERIFIED = 2.0

_INDOOR_SKIPS = {"fixed_roof", "roof_closed"}
_UNRESOLVED_SKIPS = {"name_mismatch", "unknown_stadium"}

_WEATHER_SIGNALS = frozenset(
    {
        "wind_speed_mph",
        "wind_gust_max_mph",
        "wind_direction_mode",
        "wind_along_field_mph",
        "wind_crosswind_mph",
        "temperature_f",
        "apparent_temperature_f",
        "precip_total_in",
        "snowfall_total_in",
        "precip_prob_max_pct",
        "weather_lead_hours",
        "weather_model_regime_break",
        "weather_forecast_domain",
        "venue_elevation_m",
    }
)
_SIGNAL_NAMES = _WEATHER_SIGNALS | frozenset(
    {
        "weather_status",
        "venue_roof_code",
        "surface_code",
        "rest_days",
        "rest_diff",
        "travel_miles",
        "tz_shift_hours",
        "tz_offset_diff_raw_hours",
    }
)

_SIGNAL_SCHEMA: dict[str, Any] = {
    "game_id": pl.Utf8,
    "season": pl.Int64,
    "week": pl.Int64,
    "team": pl.Utf8,
    "player_id": pl.Utf8,
    "sector": pl.Utf8,
    "signal": pl.Utf8,
    "value": pl.Float64,
    "league_pct": pl.Float32,
    "sample_n": pl.Int64,
    "stability": pl.Float32,
    "as_of": pl.Datetime(time_zone="UTC"),
    "inputs_version": pl.Utf8,
}
_SIGNAL_COLS = list(_SIGNAL_SCHEMA)


@dataclass(frozen=True)
class Game:
    game_id: str
    season: int
    week: int
    kickoff: dt.datetime  # UTC
    home_team: str
    away_team: str
    home_rest: int | None
    away_rest: int | None
    roof: str | None
    surface: str | None
    stadium_id: str | None
    stadium_name: str | None


@dataclass(frozen=True)
class Venue:
    """A stadiums row, as far as this analyst needs it."""

    stadium_id: str
    known_names: tuple[str, ...]
    lat: float
    lon: float
    roof_type: str
    field_bearing: float | None
    tz: str | None

    def as_guard_stadium(self) -> Stadium:
        return Stadium(self.stadium_id, self.known_names, self.lat, self.lon, self.roof_type)


@dataclass(frozen=True)
class Snapshot:
    """One captured weather_snapshots fetch (all its forecast hours)."""

    as_of: dt.datetime
    lead_hours: float
    model_regime_break: bool
    grid_elevation_m: float | None
    hours: tuple[dict[str, Any], ...]  # keyed by weather_snapshots column names


# --------------------------------------------------------------------------------------
# Pure helpers (no DB access -- unit-tested against synthetic data)
# --------------------------------------------------------------------------------------


def in_window(kickoff: dt.datetime, now: dt.datetime) -> bool:
    return now - _LOOKBACK < kickoff <= now + _LOOKAHEAD


def pick_snapshot(snapshots: Sequence[Snapshot]) -> Snapshot | None:
    """The headline snapshot: the latest one captured before kickoff (lead_hours >= 0).
    A t2 capture taken after kickoff is excluded, so a pre-game card never shows a
    post-kickoff fetch -- and so headline values stop changing at kickoff. t48 is
    eligible (the only forecast when Sunday cards are drafted Friday); it's labeled by
    weather_model_regime_break, not dropped."""
    eligible = [s for s in snapshots if s.lead_hours >= 0]
    return max(eligible, key=lambda s: s.as_of) if eligible else None


def weather_status(
    decision: VenueDecision,
    target_rows: Sequence[tuple[str, str | None]],  # (status, skip_reason)
    headline: Snapshot | None,
    kickoff: dt.datetime,
    now: dt.datetime,
) -> float:
    """Structural facts first (venue unresolved, indoor), then capture state. A dome is
    known from stadiums alone, so it never depends on target rows existing; "missed" is
    only ever inferred from targets -- the two can't collide."""
    if decision.skip_reason in _UNRESOLVED_SKIPS:
        return STATUS_VENUE_UNRESOLVED
    if decision.skip_reason in _INDOOR_SKIPS or any(
        skip == "roof_closed" for _, skip in target_rows
    ):
        return STATUS_INDOOR
    if headline is not None:
        return STATUS_FORECAST
    if not target_rows:
        # Beyond the collector's 54h horizon (targets not created yet), or a game played
        # before the collector existed / while it wasn't running.
        return STATUS_AWAITING if kickoff > now else STATUS_NOT_TRACKED
    if kickoff <= now:
        # No pre-kickoff capture can happen any more (post-kickoff captures never become
        # the headline), whatever the targets still say.
        return STATUS_MISSED
    if any(status == "pending" for status, _ in target_rows):
        return STATUS_AWAITING
    return STATUS_MISSED


def venue_roof_code(
    venue: Venue, games_roof: str | None, target_rows: Sequence[tuple[str, str | None]]
) -> float:
    if venue.roof_type == "fixed":
        return ROOF_FIXED
    if venue.roof_type == "retractable":
        if games_roof == "closed" or any(skip == "roof_closed" for _, skip in target_rows):
            return ROOF_RETRACTABLE_CLOSED
        return ROOF_RETRACTABLE_OPEN_OR_UNKNOWN
    return ROOF_OPEN_AIR


def surface_code(surface: str | None) -> float | None:
    """None for null, '' (3 rows since 2024) or an unrecognized string -- never guessed."""
    return _SURFACE_CODE.get(surface) if surface else None


def in_hrrr_domain(lat: float, lon: float) -> bool:
    """Whether (lat, lon) lies on NOAA's HRRR CONUS grid, by forward Lambert conformal
    projection with the grid's own parameters (see the _HRRR_* constants). Every US venue
    is >= 300 km inside it; every international venue (Mexico City included) is outside.
    Outside means Open-Meteo's best_match picks a model whose behavior hasn't been
    verified (docs/phases/P4.md) -- lower confidence, not a different number."""
    if lat <= 0:
        return False  # the projection is meaningless for the southern hemisphere
    n = math.sin(_HRRR_TRUELAT)
    f = math.cos(_HRRR_TRUELAT) * math.tan(math.pi / 4 + _HRRR_TRUELAT / 2) ** n / n
    rho0 = _HRRR_R_M * f / math.tan(math.pi / 4 + _HRRR_TRUELAT / 2) ** n
    rho = _HRRR_R_M * f / math.tan(math.pi / 4 + math.radians(lat) / 2) ** n
    theta = n * (math.radians(lon) - _HRRR_LON0)
    x = rho * math.sin(theta)
    y = rho0 - rho * math.cos(theta)
    return abs(x) <= _HRRR_HALF_X_M and abs(y) <= _HRRR_HALF_Y_M


def wind_components(
    speed_mph: float, direction_deg: float, field_bearing: float
) -> tuple[float, float]:
    """(along-field, crosswind) magnitudes of the exterior wind estimate. Absolute values:
    the field is symmetric (bearing in [0, 180)) and teams switch ends every quarter, so
    head- vs tailwind has no game-level meaning -- and "from" vs "to" direction doesn't
    matter for the same reason."""
    delta = math.radians(direction_deg - field_bearing)
    return abs(speed_mph * math.cos(delta)), abs(speed_mph * math.sin(delta))


def _mean(values: list[float]) -> float:
    return sum(values) / len(values)


def _col(hours: Sequence[dict[str, Any]], col: str, offsets: range) -> list[Any] | None:
    """Values of `col` at exactly `offsets`, or None if any offset is missing or null --
    a partial sum/max/mean would silently read as less rain/wind/cold than forecast."""
    by_offset = {h["hour_offset"]: h.get(col) for h in hours}
    values = [by_offset.get(o) for o in offsets]
    return None if any(v is None for v in values) else values


# Instantaneous variables (temperature, sustained wind, direction) are valid at the hour
# mark, so the game spans offsets 0..4. Preceding-hour aggregates (precip, snow, gusts,
# precip probability) at offset k cover hour k-1..k, so the game is offsets 1..4 --
# offset 0 would describe the hour before kickoff (docs/sources.md).
_INSTANT = range(0, 5)
_PRECEDING = range(1, 5)


def weather_values(
    snapshot: Snapshot, field_bearing: float | None
) -> list[tuple[str, float, int | None]]:
    """(signal, value, sample_n) for one headline snapshot. wind_speed_mph and
    wind_direction_mode are always present (the collector never stores a snapshot with
    null sustained wind/direction/temperature); everything else is omitted rather than
    computed from a partial window."""
    hours = snapshot.hours
    out: list[tuple[str, float, int | None]] = []

    speeds = _col(hours, "wind_speed_10m_mph", _INSTANT)
    if speeds is not None:
        out.append(("wind_speed_mph", _mean(speeds), len(speeds)))

    gusts = [
        h["wind_gusts_10m_mph"]
        for h in hours
        if h["hour_offset"] in _PRECEDING and h.get("wind_gusts_10m_mph") is not None
    ]
    if gusts:
        out.append(("wind_gust_max_mph", max(gusts), len(gusts)))

    # Speed-only is the base shape; the split is added only when it means something.
    if field_bearing is None:
        out.append(("wind_direction_mode", WIND_MODE_SPEED_ONLY_NO_BEARING, None))
    else:
        qualifying = [
            wind_components(h["wind_speed_10m_mph"], h["wind_direction_10m_deg"], field_bearing)
            for h in hours
            if h["hour_offset"] in _INSTANT
            and h.get("wind_speed_10m_mph") is not None
            and h.get("wind_direction_10m_deg") is not None
            and h["wind_speed_10m_mph"] >= _DIRECTION_MIN_MPH
        ]
        if not qualifying:
            out.append(("wind_direction_mode", WIND_MODE_SPEED_ONLY_LOW, None))
        else:
            out.append(("wind_direction_mode", WIND_MODE_SPLIT, None))
            n = len(qualifying)
            out.append(("wind_along_field_mph", _mean([a for a, _ in qualifying]), n))
            out.append(("wind_crosswind_mph", _mean([c for _, c in qualifying]), n))

    for col, signal in (
        ("temperature_2m_f", "temperature_f"),
        ("apparent_temperature_f", "apparent_temperature_f"),
    ):
        values = _col(hours, col, _INSTANT)
        if values is not None:
            out.append((signal, _mean(values), len(values)))

    for col, signal in (
        ("precipitation_in", "precip_total_in"),
        ("snowfall_in", "snowfall_total_in"),
    ):
        values = _col(hours, col, _PRECEDING)
        if values is not None:
            out.append((signal, float(sum(values)), len(values)))

    probs = _col(hours, "precipitation_probability_pct", _PRECEDING)
    if probs is not None:
        out.append(("precip_prob_max_pct", float(max(probs)), len(probs)))

    out.append(("weather_lead_hours", float(snapshot.lead_hours), None))
    out.append(("weather_model_regime_break", 1.0 if snapshot.model_regime_break else 0.0, None))
    if snapshot.grid_elevation_m is not None:
        out.append(("venue_elevation_m", float(snapshot.grid_elevation_m), None))
    return out


def home_venues(
    counts: Sequence[tuple[int, str, str, int]],  # (season, home_team, stadium_id, games)
) -> tuple[dict[tuple[int, str], str], list[dict[str, Any]]]:
    """Each team's home venue per season: the stadium_id it plays most of its REG home
    games at. games has no neutral-site flag; in 2026 every team's regular home stadium
    wins 8-1 (JAX 7-1) over its one international/neutral "home" game. A tie is never
    broken by guessing -- that team gets no home venue (and no travel/tz rows), and the
    tie is returned for agent_runs.meta."""
    by_team: dict[tuple[int, str], list[tuple[str, int]]] = {}
    for season, team, stadium_id, n in counts:
        by_team.setdefault((season, team), []).append((stadium_id, n))
    venues: dict[tuple[int, str], str] = {}
    ties: list[dict[str, Any]] = []
    for key, options in by_team.items():
        options.sort(key=lambda o: o[1], reverse=True)
        if len(options) > 1 and options[0][1] == options[1][1]:
            ties.append({"season": key[0], "team": key[1], "options": options})
            continue
        venues[key] = options[0][0]
    return venues, ties


def haversine_miles(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * _EARTH_RADIUS_MI * math.asin(math.sqrt(a))


def tz_offset_diff_hours(home_tz: str, venue_tz: str, at: dt.datetime) -> float:
    """Raw UTC-offset difference, venue minus home, both evaluated at the kickoff instant
    (so DST -- and Arizona's lack of it -- come out right). Positive = the team traveled
    east. Unbounded: LA at Melbourne is +17."""

    def offset(tz: str) -> float:
        delta = at.astimezone(ZoneInfo(tz)).utcoffset()
        assert delta is not None
        return delta.total_seconds() / 3600

    return offset(venue_tz) - offset(home_tz)


def wrap_tz_shift(raw_hours: float) -> float:
    """Wrap to [-12, +12): the body-clock shift, directly comparable across games. LA at
    Melbourne's raw +17 becomes -7 (the same shift, westward). Never clipped: a London or
    Munich trip keeps its full 5-9 hours."""
    return (raw_hours + 12) % 24 - 12


def team_values(
    game: Game,
    game_venue: Venue | None,  # None when the venue guard failed
    home_venue_ids: dict[tuple[int, str], str],
    venues: dict[str, Venue],
) -> list[tuple[str, str, float]]:
    """(team, signal, value) for both teams of one game."""
    out: list[tuple[str, str, float]] = []
    rest = {game.home_team: game.home_rest, game.away_team: game.away_rest}
    for team, opp in ((game.home_team, game.away_team), (game.away_team, game.home_team)):
        own_rest, opp_rest = rest[team], rest[opp]
        if own_rest is not None:
            out.append((team, "rest_days", float(own_rest)))
            if opp_rest is not None:
                out.append((team, "rest_diff", float(own_rest - opp_rest)))

        home_id = home_venue_ids.get((game.season, team))
        home = venues.get(home_id) if home_id else None
        if game_venue is None or home is None:
            continue
        out.append(
            (
                team,
                "travel_miles",
                haversine_miles(home.lat, home.lon, game_venue.lat, game_venue.lon),
            )
        )
        if home.tz and game_venue.tz:
            raw = tz_offset_diff_hours(home.tz, game_venue.tz, game.kickoff)
            out.append((team, "tz_shift_hours", wrap_tz_shift(raw)))
            out.append((team, "tz_offset_diff_raw_hours", raw))
    return out


def _signal_row(
    game: Game,
    team: str | None,
    signal: str,
    value: float,
    sample_n: int | None,
    as_of: dt.datetime,
    inputs_version: str,
) -> dict[str, Any]:
    return {
        "game_id": game.game_id,
        "season": int(game.season),  # the game's own season/week, never ctx's
        "week": int(game.week),
        "team": team,
        "player_id": None,
        "sector": SECTOR,
        "signal": signal,
        "value": float(value),
        "league_pct": None,
        "sample_n": int(sample_n) if sample_n is not None else None,
        "stability": None,
        "as_of": as_of,
        "inputs_version": inputs_version,
    }


def build_rows(
    *,
    games: Sequence[Game],
    venues: dict[str, Venue],
    targets: dict[str, list[tuple[str, str | None]]],
    snapshots: dict[str, list[Snapshot]],
    home_venue_ids: dict[tuple[int, str], str],
    now: dt.datetime,
    base_version: str,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Every signal row for `games`, plus run meta. Pure."""
    rows: list[dict[str, Any]] = []
    status_counts: Counter[str] = Counter()
    unresolved: list[str] = []
    unrecognized_surface: dict[str, str | None] = {}

    for game in games:
        venue = venues.get(game.stadium_id) if game.stadium_id else None
        decision = resolve_venue(
            GameVenue(
                game.game_id, game.season, game.week, game.stadium_id, game.stadium_name, game.roof
            ),
            venue.as_guard_stadium() if venue else None,
        )
        resolved = venue if decision.skip_reason not in _UNRESOLVED_SKIPS else None
        game_targets = targets.get(game.game_id, [])
        headline = pick_snapshot(snapshots.get(game.game_id, []))
        status = weather_status(decision, game_targets, headline, game.kickoff, now)
        status_counts[str(int(status))] += 1

        def add(
            team: str | None,
            signal: str,
            value: float,
            n: int | None,
            version: str,
            game: Game = game,  # bound per iteration, not late
        ) -> None:
            rows.append(_signal_row(game, team, signal, value, n, now, version))

        add(None, "weather_status", status, None, base_version)

        if resolved is None:
            unresolved.append(game.game_id)
        else:
            add(
                None,
                "venue_roof_code",
                venue_roof_code(resolved, game.roof, game_targets),
                None,
                base_version,
            )

        surface = surface_code(game.surface)
        if surface is not None:
            add(None, "surface_code", surface, None, base_version)
        else:
            unrecognized_surface[game.game_id] = game.surface

        if status == STATUS_FORECAST and headline is not None and resolved is not None:
            version = f"weather@{headline.as_of.astimezone(dt.UTC).isoformat()}"
            for signal, value, n in weather_values(headline, resolved.field_bearing):
                add(None, signal, value, n, version)
            domain = (
                DOMAIN_HRRR if in_hrrr_domain(resolved.lat, resolved.lon) else DOMAIN_UNVERIFIED
            )
            add(None, "weather_forecast_domain", domain, None, version)

        for team, signal, value in team_values(game, resolved, home_venue_ids, venues):
            add(team, signal, value, None, base_version)

    meta = {
        "games": len(games),
        "weather_status_counts": dict(status_counts),
        "unresolved_venues": unresolved,
        "unrecognized_surface": unrecognized_surface,
    }
    return rows, meta


# --------------------------------------------------------------------------------------
# DB I/O (thin -- feeds the pure functions above)
# --------------------------------------------------------------------------------------


def load_window_games(conn: psycopg.Connection, now: dt.datetime) -> list[Game]:
    """Games with now - _LOOKBACK < kickoff <= now + _LOOKAHEAD. games has no kickoff
    timestamp column, so this narrows by ET gameday first (with a day of slack each side)
    and filters exactly on kickoff_utc -- the same approach as
    weather_schedule.upcoming_games."""
    start_day = (now - _LOOKBACK - dt.timedelta(days=1)).date()
    end_day = (now + _LOOKAHEAD + dt.timedelta(days=1)).date()
    with conn.cursor() as cur:
        cur.execute(
            "SELECT game_id, season, week, gameday, gametime, home_team, away_team, "
            "home_rest, away_rest, roof, surface, stadium_id, stadium FROM games "
            "WHERE gameday BETWEEN %s AND %s AND gametime IS NOT NULL ORDER BY game_id",
            (start_day, end_day),
        )
        rows = cur.fetchall()
    games = []
    for r in rows:
        kickoff = kickoff_utc(r[3], r[4])
        if in_window(kickoff, now):
            games.append(Game(r[0], r[1], r[2], kickoff, *r[5:]))
    return games


def _load_venues(conn: psycopg.Connection) -> dict[str, Venue]:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT stadium_id, known_names, lat, lon, roof_type, field_bearing, tz FROM stadiums"
        )
        return {
            r[0]: Venue(r[0], tuple(r[1]), r[2], r[3], r[4], r[5], r[6]) for r in cur.fetchall()
        }


def _load_home_venue_counts(
    conn: psycopg.Connection, seasons: list[int]
) -> list[tuple[int, str, str, int]]:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT season, home_team, stadium_id, count(*) FROM games "
            "WHERE season = ANY(%s) AND season_type = 'REG' AND stadium_id IS NOT NULL "
            "GROUP BY season, home_team, stadium_id",
            (seasons,),
        )
        return cur.fetchall()


def _load_targets(
    conn: psycopg.Connection, game_ids: list[str]
) -> dict[str, list[tuple[str, str | None]]]:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT game_id, status, skip_reason FROM weather_snapshot_targets "
            "WHERE game_id = ANY(%s)",
            (game_ids,),
        )
        out: dict[str, list[tuple[str, str | None]]] = {}
        for game_id, status, skip in cur.fetchall():
            out.setdefault(game_id, []).append((status, skip))
        return out


_HOUR_COLS: tuple[str, ...] = (
    "hour_offset",
    "temperature_2m_f",
    "apparent_temperature_f",
    "precipitation_in",
    "precipitation_probability_pct",
    "snowfall_in",
    "wind_speed_10m_mph",
    "wind_gusts_10m_mph",
    "wind_direction_10m_deg",
)


def _load_snapshots(conn: psycopg.Connection, game_ids: list[str]) -> dict[str, list[Snapshot]]:
    cols = _HOUR_COLS
    with conn.cursor() as cur:
        cur.execute(
            "SELECT game_id, as_of, lead_hours, model_regime_break, grid_elevation_m, "
            "hour_offset, temperature_2m_f, apparent_temperature_f, precipitation_in, "
            "precipitation_probability_pct, snowfall_in, wind_speed_10m_mph, "
            "wind_gusts_10m_mph, wind_direction_10m_deg "
            "FROM weather_snapshots WHERE game_id = ANY(%s) "
            "ORDER BY game_id, as_of, hour_offset",
            (game_ids,),
        )
        rows = cur.fetchall()
    grouped: dict[tuple[str, dt.datetime], list[Any]] = {}
    for r in rows:
        grouped.setdefault((r[0], r[1]), []).append(r)
    out: dict[str, list[Snapshot]] = {}
    for (game_id, as_of), group in grouped.items():
        first = group[0]
        hours = tuple(dict(zip(cols, r[5:], strict=True)) for r in group)
        out.setdefault(game_id, []).append(
            Snapshot(as_of, float(first[2]), bool(first[3]), first[4], hours)
        )
    return out


def _base_version(conn: psycopg.Connection) -> str:
    schedules = get_last_value(conn, "nflverse:schedules") or "unknown"
    stadiums = get_last_value(conn, "stadiums_csv")
    return f"schedules@{schedules},stadiums_csv@{stadiums[:12] if stadiums else 'unknown'}"


class EnvironmentAnalyst(Analyst):
    name = "environment"
    sector = SECTOR
    signal_names = _SIGNAL_NAMES

    def __init__(self) -> None:
        # The game_ids compute() covered -- _delete_stale_signals scopes to exactly these.
        # Same state-on-self pattern as WeatherCollector._plan.
        self._game_ids: list[str] = []
        self._meta: dict[str, Any] = {}

    def inputs_ready(self, ctx: RunContext) -> bool | str:
        # No game in the window (offseason, bye stretch) is a routine "nothing to do",
        # logged as skipped_fresh -- not worth a new agent_runs status + migration.
        return bool(load_window_games(ctx.conn, ctx.now))

    def compute(self, ctx: RunContext) -> pl.DataFrame:
        conn = ctx.conn
        games = load_window_games(conn, ctx.now)
        self._game_ids = [g.game_id for g in games]
        if not games:
            self._meta = {"games": 0}
            return pl.DataFrame(schema=_SIGNAL_SCHEMA)

        venues = _load_venues(conn)
        home_venue_ids, ties = home_venues(
            _load_home_venue_counts(conn, sorted({g.season for g in games}))
        )
        rows, meta = build_rows(
            games=games,
            venues=venues,
            targets=_load_targets(conn, self._game_ids),
            snapshots=_load_snapshots(conn, self._game_ids),
            home_venue_ids=home_venue_ids,
            now=ctx.now,
            base_version=_base_version(conn),
        )
        if ties:
            _log.warning("environment: home-venue ties, no travel/tz rows: %s", ties)
        self._meta = {**meta, "home_venue_ties": ties}
        return (
            pl.DataFrame(rows, schema=_SIGNAL_SCHEMA)
            if rows
            else pl.DataFrame(schema=_SIGNAL_SCHEMA)
        )

    def _delete_stale_signals(self, ctx: RunContext) -> int:
        """Overrides the base class's (sector, ctx.season, ctx.week) scope with
        (sector, this run's game_ids): the window spans dispatcher weeks, so a week-scoped
        delete would miss next week's games and wipe finished games' frozen rows. Still
        restricted to this analyst's own sector + signal_names, so it can't reach another
        analyst's rows."""
        if not self._game_ids:
            return 0
        return delete_rows(
            ctx.conn,
            "signals",
            "sector = %s AND signal = ANY(%s) AND game_id = ANY(%s)",
            (self.sector, sorted(self.signal_names), self._game_ids),
        )

    def write_signals(self, ctx: RunContext, df: pl.DataFrame) -> WorkResult:
        rows = df.to_dicts()
        if not rows:
            return WorkResult(0, self._meta)
        conflict_cols = [
            "season",
            "week",
            "COALESCE(game_id, '')",
            "COALESCE(team, '')",
            "COALESCE(player_id, '')",
            "sector",
            "signal",
        ]
        identity_cols = ("season", "week", "game_id", "team", "player_id", "sector", "signal")
        update_cols = [c for c in _SIGNAL_COLS if c not in identity_cols]
        written = upsert_rows(
            ctx.conn, "signals", rows, conflict_cols=conflict_cols, update_cols=update_cols
        )
        return WorkResult(written, self._meta)
