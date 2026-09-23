-- P5 synthesizer: projection_log becomes the immutable record of locked pre-kickoff
-- claims. Adds the lock context the grader needs, and makes "immutable" a database
-- guarantee (a trigger that raises on UPDATE/DELETE/TRUNCATE) instead of the app-level
-- rule 0004 described. Grades go in their own table, never an UPDATE here.
--
-- The table had 0 rows when this was written (verified 2026-09-23), so the NOT NULL
-- additions need no default or backfill.

ALTER TABLE projection_log
    ADD COLUMN kickoff timestamptz NOT NULL,          -- kickoff the lock was taken against
    ADD COLUMN lock_lead_hours real NOT NULL,         -- (kickoff - locked_at) in hours
    ADD COLUMN model_version text NOT NULL,           -- model_coefficients.json model_version
    ADD COLUMN stability_min real NOT NULL,           -- min of the 4 input stabilities
    ADD COLUMN stability_bucket text NOT NULL
        CHECK (stability_bucket IN ('low', 'mid', 'high')),
    ADD COLUMN spread_sd real,                        -- the bucket's +/- band, spread
    ADD COLUMN total_sd real,                         -- the bucket's +/- band, total
    ADD COLUMN market_status smallint;                -- Market analyst code at lock; null = no market rows

CREATE FUNCTION projection_log_immutable() RETURNS trigger
    LANGUAGE plpgsql
    SET search_path = ''
AS $$
BEGIN
    RAISE EXCEPTION 'projection_log is immutable: % is not allowed', TG_OP;
END;
$$;

CREATE TRIGGER projection_log_no_update_delete
    BEFORE UPDATE OR DELETE ON projection_log
    FOR EACH ROW EXECUTE FUNCTION projection_log_immutable();

CREATE TRIGGER projection_log_no_truncate
    BEFORE TRUNCATE ON projection_log
    FOR EACH STATEMENT EXECUTE FUNCTION projection_log_immutable();
