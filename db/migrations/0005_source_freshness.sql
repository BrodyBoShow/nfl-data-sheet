-- Generic freshness-gate state, used by any collector that can check a cheap upstream
-- marker (e.g. nflverse's timestamp.json) before doing a full fetch. Not in the original
-- P1 file list — added while implementing the ID spine collector, which needs it.
-- See docs/sources.md "IMPORTANT — freshness gate is on us, not nflreadpy".
CREATE TABLE source_freshness (
    source text PRIMARY KEY,
    last_value text NOT NULL,
    checked_at timestamptz NOT NULL DEFAULT now()
);
