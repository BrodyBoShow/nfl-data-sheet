-- P4 odds collector: one derived consensus row per (event, poll), computed by the same
-- odds collector run that writes odds_snapshots (0013) -- a mechanical aggregation of
-- that poll's own just-fetched per-bookmaker rows, not a cross-source metric, so it
-- stays inside L1 the same way transactions (0011_availability.sql) derives from that
-- run's own injuries rows rather than crossing into L2. The Market analyst (P4, not
-- built yet) reads this instead of recomputing a median across 8 books itself from raw
-- odds_snapshots every time.
--
-- consensus_*_point is the median of that market's point (the line itself -- spread
-- points/total points) across every bookmaker in this poll that carries the market.
-- *_point_range is max-min across those same books -- the disagreement signal: null
-- when fewer than 2 books have the market, since disagreement isn't measurable from one
-- observation. *_book_count is how many books the median/range were computed from, same
-- pattern as signals.sample_n.
--
-- No h2h consensus here on purpose -- median of American moneyline prices isn't a
-- meaningful stat the way median spread/total points are (it'd need converting to
-- implied probability first); that's real Market-analyst work, deferred to P4's
-- actual build, not invented here.

CREATE TABLE odds_consensus (
    source_event_id text NOT NULL,
    game_id text REFERENCES games (game_id),
    target_id text NOT NULL,
    season int NOT NULL,
    week int NOT NULL,
    commence_time timestamptz NOT NULL,
    home_team text REFERENCES teams (team_abbr),
    away_team text REFERENCES teams (team_abbr),
    consensus_spread_point real,
    spread_point_range real,
    spread_book_count int NOT NULL,
    consensus_total_point real,
    total_point_range real,
    total_book_count int NOT NULL,
    as_of timestamptz NOT NULL,
    content_hash text NOT NULL,
    updated_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (source_event_id, as_of)
);

CREATE INDEX odds_consensus_game_idx ON odds_consensus (game_id) WHERE game_id IS NOT NULL;
CREATE INDEX odds_consensus_season_week_idx ON odds_consensus (season, week);

ALTER TABLE odds_consensus ENABLE ROW LEVEL SECURITY;
