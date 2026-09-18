-- Availability (Phase 3): injury/roster-designation snapshots from ESPN + Sleeper, and
-- the transactions derived by diffing them run over run. Both append-only logs -- the
-- primary key includes a timestamp component, so an unchanged designation observed
-- again on a later snapshot is still stored (a meaningful data point for
-- practice_trend_risk), not skipped as a duplicate. Populated by
-- pipeline/collectors/availability.py.
--
-- Neither ESPN's injuries feed nor Sleeper's player dump carries its own "week" --
-- season/week here are derived from the games table (pipeline/core/schedule.py's
-- resolve_season_week), never trusted from a run's ctx.week.
--
-- player_id is null when the row's provider id doesn't (yet) resolve through
-- player_id_crosswalk -- never dropped, never guessed; resolved retroactively as the
-- crosswalk improves (see pipeline/collectors/id_spine.py's Sleeper enrichment). The one
-- other null-player_id case is source_player_id = 'unmatched:<espn injury id>', when
-- ESPN's athlete id can't be extracted from either the links[] or headshot fallback path.

CREATE TABLE injuries (
    player_id text REFERENCES players (player_id),
    source text NOT NULL CHECK (source IN ('espn', 'sleeper')),
    source_player_id text NOT NULL,
    season int NOT NULL,
    week int NOT NULL,
    season_type text NOT NULL CHECK (season_type IN ('REG', 'POST')),
    team text REFERENCES teams (team_abbr),
    designation text,
    body_part text,
    notes text,
    raw jsonb NOT NULL,
    as_of timestamptz NOT NULL,
    content_hash text NOT NULL,
    updated_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (source, source_player_id, as_of)
);

CREATE INDEX injuries_player_idx ON injuries (player_id) WHERE player_id IS NOT NULL;
CREATE INDEX injuries_season_week_idx ON injuries (season, week);

CREATE TABLE transactions (
    player_id text REFERENCES players (player_id),
    source text NOT NULL CHECK (source IN ('espn', 'sleeper')),
    source_player_id text NOT NULL,
    season int NOT NULL,
    week int NOT NULL,
    transaction_type text NOT NULL CHECK (transaction_type IN ('team_change', 'designation_change')),
    from_value text,
    to_value text,
    detected_at timestamptz NOT NULL,
    content_hash text NOT NULL,
    updated_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (source, source_player_id, transaction_type, detected_at)
);

CREATE INDEX transactions_player_idx ON transactions (player_id) WHERE player_id IS NOT NULL;

-- 0007_enable_rls.sql only covered tables that existed at the time -- these two new
-- tables need the same default-deny treatment (see that migration for the full
-- rationale: postgres role bypasses RLS, anon/publishable key gets zero access).
ALTER TABLE injuries ENABLE ROW LEVEL SECURITY;
ALTER TABLE transactions ENABLE ROW LEVEL SECURITY;

-- Distinct skip status for AvailabilityImpactAnalyst.inputs_ready(): no injuries stored
-- yet for this season/week, separate from skipped_fresh's routine "nothing changed
-- upstream" meaning -- same pattern as 0009_agent_runs_skip_reason.sql's
-- skipped_no_prior. See pipeline/core/logging.py's RunStatus.
ALTER TABLE agent_runs DROP CONSTRAINT agent_runs_status_check;
ALTER TABLE agent_runs ADD CONSTRAINT agent_runs_status_check
    CHECK (status IN ('running', 'success', 'skipped_fresh', 'skipped_no_prior', 'skipped_no_injuries', 'partial', 'failed'));
