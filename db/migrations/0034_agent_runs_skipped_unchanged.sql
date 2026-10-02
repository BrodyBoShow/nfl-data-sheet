-- Distinct skip status for the per-analyst input gate (pipeline/core/input_gate.py,
-- docs/phases/P7.md step 9, the gate spec, item 5): the analyst's inputs and code match
-- its last success for this season/week, so it didn't run. Kept apart from
-- skipped_fresh so gate skips are countable on their own, and a wrong skip is never
-- mixed into routine freshness skips. Same pattern as 0009 and 0011. See
-- pipeline/core/logging.py's RunStatus.
ALTER TABLE agent_runs DROP CONSTRAINT agent_runs_status_check;
ALTER TABLE agent_runs ADD CONSTRAINT agent_runs_status_check
    CHECK (status IN ('running', 'success', 'skipped_fresh', 'skipped_no_prior', 'skipped_no_injuries', 'skipped_unchanged', 'partial', 'failed'));
