-- P4 weather: append-only Open-Meteo forecast log (source VERIFIED 2026-09-23,
-- docs/sources.md). One row per (game, fetch, forecast hour): each captured target stores
-- the 5 hourly rows H..H+4 for a kickoff in hour H -- precipitation/rain/snowfall and
-- gusts are preceding-hour aggregates, so the H+4 row is what covers the game's last hour.
-- Append-only for the same reason as odds_snapshots: how the forecast moves toward kickoff
-- is the point, so a repeat observation is a data point, not a duplicate.
--
-- Column names carry height and unit on purpose. wind_* are the model's EXTERIOR 10 m
-- open-terrain estimate for the grid cell, not field-level wind -- inside a bowl the field
-- wind is lower and swirls, non-linearly, with no free source to correct it. Consumers
-- (Environment analyst, UI) must present them as a relative indicator only.
--
-- model_regime_break: true on t48 rows. At US venues t48 is past HRRR's reach (GFS) while
-- t36 onward is HRRR, so t48 -> t36 change is partly a model switch, not a weather change.
-- Movement comparisons start at t36; t48 is a standalone early look. Flagged by target
-- (every venue), not inferred per venue -- international model switch points unverified.
--
-- Nothing here is ever filled in: a target whose response had nulls in a wind/temperature
-- field is skipped (weather_snapshot_targets.skip_reason = 'no_forecast_data') and stores
-- no rows. Nulls in the remaining fields (e.g. wind_gusts_10m ends 6h before the other
-- variables at the far horizon) are stored as null.

CREATE TABLE weather_snapshots (
    game_id text NOT NULL REFERENCES games (game_id),
    target_id text NOT NULL,
    stadium_id text NOT NULL REFERENCES stadiums (stadium_id),
    season int NOT NULL,
    week int NOT NULL,
    kickoff timestamptz NOT NULL,
    as_of timestamptz NOT NULL,               -- when we fetched
    lead_hours real NOT NULL,                 -- (kickoff - as_of) in hours, actual not planned
    model_regime_break boolean NOT NULL,
    valid_time timestamptz NOT NULL,          -- the forecast hour (Open-Meteo `time`, UTC)
    hour_offset smallint NOT NULL CHECK (hour_offset BETWEEN 0 AND 4),  -- valid_time - H
    -- where the forecast is for: requested stadium coords vs. the model grid cell
    -- Open-Meteo actually answered with (verified ~1 km apart at Lambeau)
    requested_lat double precision NOT NULL,
    requested_lon double precision NOT NULL,
    grid_lat double precision NOT NULL,
    grid_lon double precision NOT NULL,
    grid_elevation_m real,
    temperature_2m_f real,
    apparent_temperature_f real,
    precipitation_in real,                    -- preceding-hour sum
    precipitation_probability_pct smallint,   -- preceding hour
    rain_in real,                             -- preceding-hour sum
    snowfall_in real,                         -- preceding-hour sum
    weather_code smallint,                    -- WMO code
    wind_speed_10m_mph real,                  -- exterior 10 m estimate, instantaneous
    wind_gusts_10m_mph real,                  -- exterior 10 m estimate, preceding-hour max
    wind_direction_10m_deg smallint CHECK (wind_direction_10m_deg BETWEEN 0 AND 360),
    content_hash text NOT NULL,
    updated_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (game_id, as_of, valid_time),
    FOREIGN KEY (game_id, target_id) REFERENCES weather_snapshot_targets (game_id, target_id)
);

CREATE INDEX weather_snapshots_season_week_idx ON weather_snapshots (season, week);

ALTER TABLE weather_snapshots ENABLE ROW LEVEL SECURITY;
