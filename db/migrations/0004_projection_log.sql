-- Immutable log of locked pre-kickoff projections, graded after the game (Phase 5).
-- PROVISIONAL: only the ID spine and signals table are exercised by Phase 1-4 code.
-- This schema will very likely be revisited when the synthesizer is actually built in
-- P5 — `card` exists precisely so we don't have to guess the full edge-card shape now.
CREATE TABLE projection_log (
    id bigserial PRIMARY KEY,
    game_id text NOT NULL REFERENCES games (game_id),
    season int NOT NULL,
    week int NOT NULL,
    locked_at timestamptz NOT NULL,
    market_spread real,
    market_total real,
    projected_spread real,
    projected_total real,
    edge_spread real,
    edge_total real,
    inputs_version text NOT NULL,
    card jsonb NOT NULL DEFAULT '{}'::jsonb,  -- full edge card payload (signals used, etc.)
    created_at timestamptz NOT NULL DEFAULT now()
);

-- One locked projection per game. Application code must never UPDATE a row here after
-- insert — "immutable" per docs/architecture.md is an app-level rule, not a DB trigger.
CREATE UNIQUE INDEX projection_log_game_unique ON projection_log (game_id);
