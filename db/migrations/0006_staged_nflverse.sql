-- Staged nflverse bulk tables for Phase 2 (Efficiency, Usage and role, Matchup history
-- analysts). Aggregates only -- never raw play-by-play (see CLAUDE.md hard constraint).
-- Populated by pipeline/collectors/nflverse_bulk.py. See docs/sources.md "Verified
-- shapes (P2 nflverse bulk)" for the real nflreadpy column names these are derived from.

-- One row per player per game, mostly pass-through from load_player_stats (nflverse
-- already computes target_share/air_yards_share/wopr/epa at this grain).
CREATE TABLE player_week (
    player_id text NOT NULL REFERENCES players (player_id),
    game_id text NOT NULL REFERENCES games (game_id),
    season int NOT NULL,
    week int NOT NULL,
    season_type text NOT NULL CHECK (season_type IN ('REG', 'POST')),
    team text NOT NULL REFERENCES teams (team_abbr),
    opponent_team text NOT NULL REFERENCES teams (team_abbr),
    position text,
    position_group text,
    completions int,
    attempts int,
    passing_yards int,
    passing_tds int,
    passing_interceptions int,
    sacks_suffered int,
    passing_air_yards int,
    passing_yards_after_catch int,
    passing_epa double precision,
    passing_cpoe double precision,
    carries int,
    rushing_yards int,
    rushing_tds int,
    rushing_epa double precision,
    receptions int,
    targets int,
    receiving_yards int,
    receiving_tds int,
    receiving_air_yards int,
    receiving_yards_after_catch int,
    receiving_epa double precision,
    racr double precision,
    target_share double precision,
    air_yards_share double precision,
    wopr double precision,
    content_hash text NOT NULL,
    updated_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (player_id, game_id)
);

CREATE INDEX player_week_season_week_idx ON player_week (season, week);

-- One row per team per game, offense-side only. Opponent-adjustment (Efficiency analyst)
-- is computed by self-joining on opponent_team across the league, not by storing a
-- separate defensive row. plays/epa_sum/success_count/explosive_count are pbp-derived,
-- garbage-time plays excluded (see pipeline/collectors/nflverse_bulk.py for the filter);
-- the analyst divides sums by counts to get rates -- this table stores counts, not rates.
CREATE TABLE team_week (
    game_id text NOT NULL REFERENCES games (game_id),
    season int NOT NULL,
    week int NOT NULL,
    season_type text NOT NULL CHECK (season_type IN ('REG', 'POST')),
    team text NOT NULL REFERENCES teams (team_abbr),
    opponent_team text NOT NULL REFERENCES teams (team_abbr),
    plays int NOT NULL,
    epa_sum double precision NOT NULL,
    success_count int NOT NULL,
    explosive_count int NOT NULL,
    garbage_time_plays_excluded int NOT NULL,
    pass_plays int NOT NULL,
    pass_epa_sum double precision NOT NULL,
    pass_success_count int NOT NULL,
    pass_explosive_count int NOT NULL,
    rush_plays int NOT NULL,
    rush_epa_sum double precision NOT NULL,
    rush_success_count int NOT NULL,
    rush_explosive_count int NOT NULL,
    down1_plays int NOT NULL,
    down1_epa_sum double precision NOT NULL,
    down1_success_count int NOT NULL,
    down2_plays int NOT NULL,
    down2_epa_sum double precision NOT NULL,
    down2_success_count int NOT NULL,
    down3_plays int NOT NULL,
    down3_epa_sum double precision NOT NULL,
    down3_success_count int NOT NULL,
    down4_plays int NOT NULL,
    down4_epa_sum double precision NOT NULL,
    down4_success_count int NOT NULL,
    drives int NOT NULL,
    three_and_out_drives int NOT NULL,
    red_zone_trips int NOT NULL,
    red_zone_tds int NOT NULL,
    content_hash text NOT NULL,
    updated_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (game_id, team)
);

CREATE INDEX team_week_season_week_idx ON team_week (season, week);
CREATE INDEX team_week_opponent_idx ON team_week (opponent_team);

-- load_snap_counts is keyed by pfr_player_id, not gsis_id -- player_id is resolved
-- through player_id_crosswalk.pfr_id at store time and left null when it doesn't
-- resolve (never guessed).
CREATE TABLE snaps (
    game_id text NOT NULL REFERENCES games (game_id),
    pfr_player_id text NOT NULL,
    player_id text REFERENCES players (player_id),
    season int NOT NULL,
    week int NOT NULL,
    season_type text NOT NULL CHECK (season_type IN ('REG', 'POST')),
    team text NOT NULL REFERENCES teams (team_abbr),
    opponent_team text NOT NULL REFERENCES teams (team_abbr),
    position text,
    offense_snaps int,
    offense_pct real,
    defense_snaps int,
    defense_pct real,
    st_snaps int,
    st_pct real,
    content_hash text NOT NULL,
    updated_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (game_id, pfr_player_id)
);

-- load_nextgen_stats, one release shared by three stat_types with mostly disjoint
-- columns -- stored as one table with nullable type-specific columns rather than three
-- tables, since the union of "advanced tracking" fields Usage/Efficiency care about is
-- small. player_gsis_id is direct, no crosswalk join needed.
CREATE TABLE ngs (
    player_id text NOT NULL REFERENCES players (player_id),
    season int NOT NULL,
    week int NOT NULL,
    season_type text NOT NULL CHECK (season_type IN ('REG', 'POST')),
    stat_type text NOT NULL CHECK (stat_type IN ('passing', 'rushing', 'receiving')),
    team text REFERENCES teams (team_abbr),
    completion_percentage_above_expectation double precision,
    aggressiveness double precision,
    avg_air_yards_differential double precision,
    rush_yards_over_expected double precision,
    rush_yards_over_expected_per_att double precision,
    percent_attempts_gte_eight_defenders double precision,
    avg_separation double precision,
    avg_yac_above_expectation double precision,
    content_hash text NOT NULL,
    updated_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (player_id, season, week, season_type, stat_type)
);

-- load_ftn_charting is play-level charting (distinct source from nflverse pbp, ~29 cols
-- vs. pbp's 372) meant to be joined onto games by play -- the Scheme sector's (Phase 7)
-- main input, staged here since it's the same collector. Not subject to the "never raw
-- pbp" rule, which targets nflverse's pbp release specifically.
CREATE TABLE ftn (
    game_id text NOT NULL REFERENCES games (game_id),
    play_id int NOT NULL,
    n_offense_backfield int,
    n_defense_box int,
    is_no_huddle boolean,
    is_motion boolean,
    is_play_action boolean,
    is_screen_pass boolean,
    is_rpo boolean,
    n_blitzers int,
    n_pass_rushers int,
    content_hash text NOT NULL,
    updated_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (game_id, play_id)
);

-- load_pfr_advstats (4 stat_types: pass/rush/rec/def, mostly disjoint columns, same
-- pattern as `ngs`) -- named in this phase's collector scope but not consumed by any
-- Phase 2 analyst yet; staged for Efficiency/Scheme refinements in later phases. Keyed
-- by pfr_player_id like `snaps`, resolved through player_id_crosswalk.pfr_id. Only the
-- columns judged signal-relevant are kept, not the full ~29-column raw shape -- see
-- docs/sources.md for the full verified column list per stat_type.
CREATE TABLE pfr_advstats (
    game_id text NOT NULL REFERENCES games (game_id),
    pfr_player_id text NOT NULL,
    player_id text REFERENCES players (player_id),
    season int NOT NULL,
    week int NOT NULL,
    season_type text NOT NULL CHECK (season_type IN ('REG', 'POST')),
    stat_type text NOT NULL CHECK (stat_type IN ('pass', 'rush', 'rec', 'def')),
    team text NOT NULL REFERENCES teams (team_abbr),
    opponent_team text NOT NULL REFERENCES teams (team_abbr),
    passing_bad_throw_pct double precision,
    passing_drop_pct double precision,
    times_pressured_pct double precision,
    -- source columns are Float64 in nflreadpy (not integer counts) -- verified live, not
    -- guessed; see docs/sources.md.
    times_blitzed double precision,
    times_hurried double precision,
    times_hit double precision,
    rushing_yards_before_contact_avg double precision,
    rushing_yards_after_contact_avg double precision,
    rushing_broken_tackles double precision,
    receiving_broken_tackles double precision,
    receiving_drop_pct double precision,
    def_pressures double precision,
    def_missed_tackle_pct double precision,
    def_passer_rating_allowed double precision,
    content_hash text NOT NULL,
    updated_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (game_id, pfr_player_id, stat_type)
);

-- load_depth_charts is scraped near-daily (221 distinct `dt` values/season, not one row
-- per player per week) -- storing every snapshot would multiply row count ~30x for no
-- signal value. This table holds only the LATEST snapshot per team/position slot as of
-- the most recent collector run; it answers "who is QB1 right now" for the Efficiency
-- analyst's starting-QB-change discount (docs/signals.md "Prior blending"), not "who was
-- QB1 in week 3" -- historical depth is out of scope for Phase 2.
CREATE TABLE depth (
    team text NOT NULL REFERENCES teams (team_abbr),
    pos_grp text NOT NULL,
    pos_abb text NOT NULL,
    pos_rank int NOT NULL,
    player_id text REFERENCES players (player_id),
    as_of timestamptz NOT NULL,
    content_hash text NOT NULL,
    updated_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (team, pos_grp, pos_abb, pos_rank)
);
