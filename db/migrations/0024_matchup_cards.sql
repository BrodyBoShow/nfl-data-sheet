-- P5 synthesizer output: one matchup card per game in the synthesizer's window. Mutable
-- until kickoff (hash-diffed through filter_changed), then frozen: the synthesizer never
-- writes a game at or after its kickoff. The locked claim itself lives in the immutable
-- projection_log. The card only mirrors it.
--
-- Not stored in `signals` because signals are L2 output, and an L3 sector would blur
-- the layer rule. Not computed at read time in P6 because that would reimplement the
-- model in TypeScript. See docs/phases/P5.md.

CREATE TABLE matchup_cards (
    game_id text PRIMARY KEY REFERENCES games (game_id),
    season int NOT NULL,
    week int NOT NULL,
    kickoff timestamptz NOT NULL,
    -- 1 projected, 2 awaiting efficiency for this (season, week), 3 an input is null,
    -- 4 model stale (efficiency fingerprint mismatch), 5 in-sample season
    projection_status smallint NOT NULL CHECK (projection_status BETWEEN 1 AND 5),
    projected_spread real,           -- home-negative market convention
    projected_total real,
    edge_spread real,                -- vs. the current market line, not the lock
    edge_total real,
    locked boolean NOT NULL DEFAULT false,
    card jsonb NOT NULL,
    as_of timestamptz NOT NULL,
    inputs_version text NOT NULL,
    content_hash text NOT NULL,
    updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX matchup_cards_season_week_idx ON matchup_cards (season, week);

-- Default-deny for the anon key, like every other table (0007). The pipeline connects
-- as the owner and bypasses RLS. P6 adds read policies.
ALTER TABLE matchup_cards ENABLE ROW LEVEL SECURITY;
