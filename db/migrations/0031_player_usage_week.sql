-- P7 step 3: player_usage_week -- the Usage and role analyst's (pipeline/analysts/usage.py)
-- per-player output. One of the two player tables allowed outside `signals` (CLAUDE.md
-- layer rules; contract and metric registry: docs/signals.md, "Player tables" and
-- "Player tables (Phase 7)"). The column list IS the registry: add a metric here only
-- together with its registry entry.
--
-- As-of rows: a row for week W exists only for a player who took a snap in week W. Read
-- "as of week W" as each player's latest row with week <= W in that season.
--
-- Per metric: <m>_std (season to date over the player's games), <m>_game (this row's
-- game), <m>_l4 (last 4 games played), <m>_pct (smallint 0-100, league percentile of
-- _std within position_group), <m>_wow (_game minus the player's previous game's _game;
-- usage only). NULL = not sourced or no sample, never zero-filled. Usage values are
-- observed shares, not prior-blended.
--
-- Not readable by anon: new tables start closed (0026's default privileges) and RLS is
-- default-deny. The web player view (P7 step 9) brings its own grant migration.
-- Retention L4 (pipeline/orchestration/retention.py): a completed season keeps only each
-- player's latest row.

CREATE TABLE player_usage_week (
    player_id text NOT NULL REFERENCES players (player_id),
    season int NOT NULL,
    week int NOT NULL,
    season_type text NOT NULL CHECK (season_type IN ('REG', 'POST')),
    game_id text NOT NULL REFERENCES games (game_id),   -- the game _game/_wow come from
    team text NOT NULL REFERENCES teams (team_abbr),
    position_group text,                                -- players.position_group
    as_of timestamptz NOT NULL,
    inputs_version text NOT NULL,

    -- family sample: games played (a snaps row with any snap), and trust
    usage_games_std smallint NOT NULL,
    usage_games_l4 smallint NOT NULL,
    usage_stability real,

    off_snap_share_std real,
    off_snap_share_game real,
    off_snap_share_l4 real,
    off_snap_share_pct smallint,
    off_snap_share_wow real,

    def_snap_share_std real,
    def_snap_share_game real,
    def_snap_share_l4 real,
    def_snap_share_pct smallint,
    def_snap_share_wow real,

    st_snap_share_std real,
    st_snap_share_game real,
    st_snap_share_l4 real,
    st_snap_share_pct smallint,
    st_snap_share_wow real,

    target_share_std real,
    target_share_game real,
    target_share_l4 real,
    target_share_pct smallint,
    target_share_wow real,

    air_yards_share_std real,
    air_yards_share_game real,
    air_yards_share_l4 real,
    air_yards_share_pct smallint,
    air_yards_share_wow real,

    carry_share_std real,
    carry_share_game real,
    carry_share_l4 real,
    carry_share_pct smallint,
    carry_share_wow real,

    dropback_share_std real,
    dropback_share_game real,
    dropback_share_l4 real,
    dropback_share_pct smallint,
    dropback_share_wow real,

    rz_target_share_std real,
    rz_target_share_game real,
    rz_target_share_l4 real,
    rz_target_share_pct smallint,
    rz_target_share_wow real,

    ez_target_share_std real,
    ez_target_share_game real,
    ez_target_share_l4 real,
    ez_target_share_pct smallint,
    ez_target_share_wow real,

    rz_carry_share_std real,
    rz_carry_share_game real,
    rz_carry_share_l4 real,
    rz_carry_share_pct smallint,
    rz_carry_share_wow real,

    gl_carry_share_std real,
    gl_carry_share_game real,
    gl_carry_share_l4 real,
    gl_carry_share_pct smallint,
    gl_carry_share_wow real,

    targets_per_off_snap_std real,
    targets_per_off_snap_game real,
    targets_per_off_snap_l4 real,
    targets_per_off_snap_pct smallint,
    targets_per_off_snap_wow real,

    content_hash text NOT NULL,
    updated_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (player_id, season, week),
    CHECK (usage_games_l4 BETWEEN 1 AND 4),
    CHECK (usage_stability IS NULL OR usage_stability BETWEEN 0 AND 1)
);

CREATE INDEX player_usage_week_season_week_idx ON player_usage_week (season, week);
CREATE INDEX player_usage_week_team_idx ON player_usage_week (season, team, week);

ALTER TABLE player_usage_week ENABLE ROW LEVEL SECURITY;
