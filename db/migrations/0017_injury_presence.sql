-- Tracks each source's most recent sighting of a currently-active player, purely to
-- support 2-consecutive-miss clearance (pipeline/core/injury_changelog.py): a player
-- only clears from `injuries` after being absent from TWO consecutive polls of the same
-- source, not one -- a single blip (a truncated response, a mid-poll glitch) must never
-- immediately clear someone. This is NOT a history table -- it's upserted in place, one
-- row per (source, source_player_id) currently tracked as active by that source, and the
-- row is deleted the moment that player actually clears. Bounded in size (~1,500 rows,
-- roughly the size of the active roster across both sources), never grows unbounded the
-- way an append-only table would.

CREATE TABLE injury_presence (
    source text NOT NULL CHECK (source IN ('espn', 'sleeper')),
    source_player_id text NOT NULL,
    last_seen_at timestamptz NOT NULL,
    consecutive_misses integer NOT NULL DEFAULT 0,
    PRIMARY KEY (source, source_player_id)
);

ALTER TABLE injury_presence ENABLE ROW LEVEL SECURITY;
