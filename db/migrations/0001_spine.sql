-- Canonical spine: teams, games, players, and the provider ID crosswalk.
-- Every other table foreign-keys here. See docs/architecture.md and CLAUDE.md
-- "Canonical keys". Populated by pipeline/collectors/id_spine.py (Phase 1).

CREATE TABLE teams (
    team_abbr text PRIMARY KEY,       -- nflverse abbreviation, e.g. 'KC'
    team_name text NOT NULL,
    team_nick text,
    team_conf text,
    team_division text,
    content_hash text NOT NULL,
    updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE games (
    game_id text PRIMARY KEY,         -- nflverse format, e.g. '2026_02_KC_BUF'
    season int NOT NULL,
    week int NOT NULL,
    season_type text NOT NULL CHECK (season_type IN ('REG', 'POST')),
    game_type text NOT NULL,          -- raw nflverse subtype: REG, WC, DIV, CON, SB
    gameday date,
    weekday text,
    gametime text,                    -- local kickoff time as given by nflverse; not yet a timestamptz
    away_team text NOT NULL REFERENCES teams (team_abbr),
    home_team text NOT NULL REFERENCES teams (team_abbr),
    away_score int,
    home_score int,
    result int,
    total int,
    overtime boolean,
    roof text,
    surface text,
    temp real,
    wind real,
    away_qb_id text,
    home_qb_id text,
    away_rest int,
    home_rest int,
    div_game boolean,
    stadium_id text,
    stadium text,
    spread_line real,
    total_line real,
    -- game-level provider crosswalk (nflverse carries these on the schedule row itself)
    old_game_id text,
    gsis text,
    nfl_detail_id text,
    pfr text,
    pff text,
    espn text,
    ftn text,
    content_hash text NOT NULL,
    updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX games_season_week_idx ON games (season, week);

CREATE TABLE players (
    player_id text PRIMARY KEY,       -- gsis_id, the canonical player key
    display_name text NOT NULL,
    first_name text,
    last_name text,
    position text,
    position_group text,
    birth_date date,
    college_name text,
    height real,
    weight real,
    rookie_season int,
    last_season int,
    latest_team text REFERENCES teams (team_abbr),
    status text,
    content_hash text NOT NULL,
    updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX players_latest_team_idx ON players (latest_team);

-- Other providers' IDs live only here (KICKOFF.md Canonical keys). Sourced from both
-- load_players (esb/nfl/pfr/pff/otc/espn) and load_ff_playerids (adds sleeper/yahoo/mfl).
CREATE TABLE player_id_crosswalk (
    player_id text PRIMARY KEY REFERENCES players (player_id),
    esb_id text,
    nfl_id text,
    pfr_id text,
    pff_id text,
    otc_id text,
    espn_id text,
    sleeper_id text,
    yahoo_id text,
    mfl_id text,
    content_hash text NOT NULL,
    updated_at timestamptz NOT NULL DEFAULT now()
);
