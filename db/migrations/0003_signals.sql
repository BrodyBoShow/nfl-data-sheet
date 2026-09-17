-- The single output table for every L2 analyst. See docs/signals.md for the full
-- schema description and the signal registry.
CREATE TABLE signals (
    id bigserial PRIMARY KEY,
    game_id text REFERENCES games (game_id),   -- null for season-level signals
    season int NOT NULL,
    week int NOT NULL,
    team text,                                  -- null for player-only signals
    player_id text REFERENCES players (player_id), -- null for team signals
    sector text NOT NULL
        CHECK (sector IN ('efficiency', 'usage', 'matchup', 'scheme', 'availability',
                           'environment', 'market')),
    signal text NOT NULL,                       -- snake_case, e.g. epa_per_dropback_adj
    value double precision,
    league_pct real,
    sample_n int,
    stability real,
    as_of timestamptz NOT NULL,
    inputs_version text NOT NULL
);

CREATE INDEX signals_lookup_idx ON signals (season, week, sector, signal);
CREATE INDEX signals_player_idx ON signals (player_id) WHERE player_id IS NOT NULL;
CREATE INDEX signals_team_idx ON signals (team) WHERE team IS NOT NULL;

-- Nulls in game_id/team/player_id must not defeat the dedupe key (a season-level team
-- signal and a game-level team signal are different rows). COALESCE to sentinels rather
-- than relying on NULLS NOT DISTINCT, which needs Postgres 15+.
CREATE UNIQUE INDEX signals_unique_key ON signals (
    season, week,
    COALESCE(game_id, ''), COALESCE(team, ''), COALESCE(player_id, ''),
    sector, signal
);
