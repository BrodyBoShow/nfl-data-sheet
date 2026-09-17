-- Adds a distinct skip status for "an analyst's required historical input doesn't exist
-- yet" (e.g. the Efficiency analyst has no prior season to blend from), separate from
-- skipped_fresh's routine "nothing changed upstream" meaning. See pipeline/core/
-- logging.py's RunStatus and pipeline/core/base.py's is_ready contract.
ALTER TABLE agent_runs DROP CONSTRAINT agent_runs_status_check;
ALTER TABLE agent_runs ADD CONSTRAINT agent_runs_status_check
    CHECK (status IN ('running', 'success', 'skipped_fresh', 'skipped_no_prior', 'partial', 'failed'));
