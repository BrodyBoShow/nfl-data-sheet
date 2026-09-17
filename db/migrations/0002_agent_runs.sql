-- L0 bookkeeping: one row per collector/analyst run. See CLAUDE.md "Data contracts".
CREATE TABLE agent_runs (
    id bigserial PRIMARY KEY,
    agent text NOT NULL,
    started_at timestamptz NOT NULL,
    finished_at timestamptz,
    status text NOT NULL DEFAULT 'running'
        CHECK (status IN ('running', 'success', 'skipped_fresh', 'partial', 'failed')),
    rows_written int NOT NULL DEFAULT 0,
    source_version text,
    error text,
    meta jsonb NOT NULL DEFAULT '{}'::jsonb
);

CREATE INDEX agent_runs_agent_started_idx ON agent_runs (agent, started_at DESC);
