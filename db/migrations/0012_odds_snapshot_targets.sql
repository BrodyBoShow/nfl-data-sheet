-- P4 odds collector: scheduling state only. Tracks this season/week's odds-snapshot
-- target calendar (see pipeline/collectors/odds_schedule.py) and whether each target
-- has been captured yet, so should_run() decides from stored state vs. absolute time
-- (per pipeline/orchestration/dispatcher.py's module docstring), never from elapsed
-- time or tick count -- dispatcher ticks are irregular (observed 1 of ~18 expected
-- ticks over 3 hours), so a target's window has to stay open across however many (or
-- few) ticks land inside it, and "already captured" has to survive any number of
-- ticks landing after that.
--
-- This is deliberately separate from an odds_snapshots table (the actual line data),
-- which isn't created yet -- The Odds API is still UNVERIFIED (docs/sources.md), so its
-- response shape/field names aren't sourced. This table only needs to know WHEN a
-- credit was spent and on which target, not WHAT the call returned.

CREATE TABLE odds_snapshot_targets (
    season int NOT NULL,
    week int NOT NULL,
    target_id text NOT NULL,
    scheduled_for timestamptz NOT NULL,
    deadline timestamptz NOT NULL,
    credits_estimate int NOT NULL,
    status text NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'captured', 'missed')),
    captured_at timestamptz,
    credits_spent int,
    missed_reason text,
    updated_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (season, week, target_id)
);

-- Every should_run() tick scans this week's pending rows -- keep that scan cheap.
CREATE INDEX odds_snapshot_targets_pending_idx ON odds_snapshot_targets (season, week)
    WHERE status = 'pending';

ALTER TABLE odds_snapshot_targets ENABLE ROW LEVEL SECURITY;
