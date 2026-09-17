-- Enable Row Level Security on every table in the public schema, with NO policies.
-- With RLS on and zero policies, the anon/publishable key (used by Supabase client
-- libraries via PostgREST) can read or write nothing -- the default-deny state.
--
-- The pipeline is unaffected: it connects as the `postgres` role via SUPABASE_DB_URL,
-- which owns every table here and also carries the `bypassrls` attribute (confirmed live
-- 2026-09-17 via `SELECT rolname, rolbypassrls FROM pg_roles WHERE rolname = 'postgres'`
-- -> bypassrls = true). Table owners bypass RLS by default (Postgres only enforces it for
-- owners if `FORCE ROW LEVEL SECURITY` is also set, which this migration does NOT set),
-- so pipeline/core/db.py's writes are untouched either way.
--
-- Read-only policies for the Phase 6 web app (querying `signals` etc. via the anon key)
-- are deliberately deferred to P6 -- this migration only closes the current fully-open
-- exposure flagged by Supabase's security advisor.

ALTER TABLE schema_migrations ENABLE ROW LEVEL SECURITY;
ALTER TABLE teams ENABLE ROW LEVEL SECURITY;
ALTER TABLE games ENABLE ROW LEVEL SECURITY;
ALTER TABLE players ENABLE ROW LEVEL SECURITY;
ALTER TABLE player_id_crosswalk ENABLE ROW LEVEL SECURITY;
ALTER TABLE agent_runs ENABLE ROW LEVEL SECURITY;
ALTER TABLE signals ENABLE ROW LEVEL SECURITY;
ALTER TABLE projection_log ENABLE ROW LEVEL SECURITY;
ALTER TABLE source_freshness ENABLE ROW LEVEL SECURITY;
ALTER TABLE player_week ENABLE ROW LEVEL SECURITY;
ALTER TABLE team_week ENABLE ROW LEVEL SECURITY;
ALTER TABLE snaps ENABLE ROW LEVEL SECURITY;
ALTER TABLE ngs ENABLE ROW LEVEL SECURITY;
ALTER TABLE ftn ENABLE ROW LEVEL SECURITY;
ALTER TABLE pfr_advstats ENABLE ROW LEVEL SECURITY;
ALTER TABLE depth ENABLE ROW LEVEL SECURITY;
