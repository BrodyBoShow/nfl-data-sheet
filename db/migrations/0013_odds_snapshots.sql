-- P4 odds collector: the actual line-data append log, now that the source is VERIFIED
-- (docs/sources.md). One row per (event, bookmaker, poll) -- append-only, same
-- reasoning as injuries (0011_availability.sql): the market analyst needs open-vs-
-- current line movement over the week, so a repeated observation is a real data point,
-- not a duplicate to collapse. Unlike injuries, polls here are already budget-gated to
-- a handful of scheduled targets per week (odds_snapshot_targets), not every dispatcher
-- tick, so this doesn't carry the same redundant-row-volume concern.
--
-- Every bookmaker is stored, not one reference book -- movement across books, and
-- disagreement between them, is itself signal (see odds_consensus,
-- 0014_odds_consensus.sql, for the derived median/range the Market analyst reads
-- instead of recomputing it from every row here). This also keeps line-shopping data
-- (which book had the best number, when) for anything that wants it later.
--
-- source_event_id is The Odds API's own opaque event id (verified live: not comparable
-- to nflverse's game_id). game_id is resolved separately (via commence_time + team-name
-- match against `games`) and left null if that resolution fails -- never dropped, never
-- guessed, per CLAUDE.md's sourced-data-only rule, same as injuries.player_id.
--
-- home_team/away_team here are nflverse team_abbr, resolved from the API's full team
-- name via teams.team_name -- also left null on resolution failure rather than storing
-- a guessed code.

CREATE TABLE odds_snapshots (
    source_event_id text NOT NULL,
    bookmaker text NOT NULL,
    game_id text REFERENCES games (game_id),
    target_id text NOT NULL,
    season int NOT NULL,
    week int NOT NULL,
    commence_time timestamptz NOT NULL,
    home_team text REFERENCES teams (team_abbr),
    away_team text REFERENCES teams (team_abbr),
    h2h_home_price int,
    h2h_away_price int,
    spread_home_point real,
    spread_home_price int,
    spread_away_point real,
    spread_away_price int,
    total_point real,
    total_over_price int,
    total_under_price int,
    markets_raw jsonb NOT NULL,
    as_of timestamptz NOT NULL,
    content_hash text NOT NULL,
    updated_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (source_event_id, bookmaker, as_of)
);

CREATE INDEX odds_snapshots_game_idx ON odds_snapshots (game_id) WHERE game_id IS NOT NULL;
CREATE INDEX odds_snapshots_season_week_idx ON odds_snapshots (season, week);

ALTER TABLE odds_snapshots ENABLE ROW LEVEL SECURITY;
