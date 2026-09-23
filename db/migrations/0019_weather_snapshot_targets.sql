-- P4 weather: per-game snapshot scheduling state, the weather analogue of
-- odds_snapshot_targets (0012). should_run() decides from stored state vs. absolute time,
-- never elapsed time or tick count -- dispatcher ticks are irregular (GitHub drops many
-- scheduled runs), so a window has to stay open across however many ticks land in it,
-- and "already captured" has to survive any number landing after.
--
-- Unlike odds (one call covers the whole slate), weather is one call per game, so targets
-- are per (game_id, target_id). target_id is the lead time before kickoff:
--   t48, t36, t24, t18, t12, t6, t4, t2, t1, t0  (10 per game, weighted late)
-- Each window opens at kickoff - lead and closes when the next-later target opens (t0:
-- kickoff to kickoff + 1h), so windows never overlap and a late tick can't mislabel a
-- snapshot; a target whose window closes unfired is 'missed' and stays missed. Every
-- stored row carries its actual lead_hours regardless (weather_snapshots).
--
-- 'skipped' is a decision not to fetch, with the reason recorded -- distinct from
-- 'missed' (wanted to fetch, no tick landed in time):
--   fixed_roof        stadiums.roof_type = 'fixed'
--   roof_closed       retractable venue and games.roof = 'closed' for this game
--   name_mismatch     games.stadium not in stadiums.known_names for its stadium_id --
--                     the auditor alerts on these (never fetched with stored coords)
--   unknown_stadium   games.stadium_id has no stadiums row -- also alerted
--   no_forecast_data  Open-Meteo returned HTTP 400 (out of range) or nulls in a
--                     wind/temperature field for the game window; nothing stored,
--                     nothing filled in

CREATE TABLE weather_snapshot_targets (
    game_id text NOT NULL REFERENCES games (game_id),
    target_id text NOT NULL CHECK (target_id IN (
        't48', 't36', 't24', 't18', 't12', 't6', 't4', 't2', 't1', 't0'
    )),
    season int NOT NULL,
    week int NOT NULL,
    stadium_id text,                          -- as on the game row; may lack a stadiums row
    kickoff timestamptz NOT NULL,
    scheduled_for timestamptz NOT NULL,       -- window opens
    deadline timestamptz NOT NULL,            -- window closes
    status text NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending', 'captured', 'missed', 'skipped')),
    captured_at timestamptz,
    missed_reason text CHECK (missed_reason IN ('deadline_passed', 'superseded')),
    skip_reason text CHECK (skip_reason IN (
        'fixed_roof', 'roof_closed', 'name_mismatch', 'unknown_stadium', 'no_forecast_data'
    )),
    updated_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (game_id, target_id),
    CHECK (scheduled_for < deadline),
    CHECK ((status = 'missed') = (missed_reason IS NOT NULL)),
    CHECK ((status = 'skipped') = (skip_reason IS NOT NULL)),
    CHECK ((status = 'captured') = (captured_at IS NOT NULL))
);

-- Every should_run() tick scans pending rows whose window could be open now.
CREATE INDEX weather_snapshot_targets_pending_idx ON weather_snapshot_targets (scheduled_for)
    WHERE status = 'pending';
CREATE INDEX weather_snapshot_targets_season_week_idx ON weather_snapshot_targets (season, week);

ALTER TABLE weather_snapshot_targets ENABLE ROW LEVEL SECURITY;
