-- Dedup state for pipeline/orchestration/auditor.py's send_alert calls. Without this,
-- a persistent condition (e.g. a stale table, an odds target that never got captured)
-- re-sends the identical Discord message on every ~15-min dispatcher tick until the
-- underlying issue is fixed -- both spammy and, before this migration, the reason a
-- long-stale-but-known warning made every single dispatcher run look freshly broken.
--
-- alert_key scopes each distinct check (e.g. 'freshness:id_spine', or
-- 'odds:2026:3' for a season/week-scoped odds check). A row is upserted with the exact
-- message sent so the next tick only re-sends if the message text actually changed
-- (e.g. one more missed target), and deleted once the check comes back healthy -- so a
-- later recurrence of the same condition (even with identical wording) alerts again
-- instead of staying silently suppressed forever.
CREATE TABLE auditor_alerts (
    alert_key text PRIMARY KEY,
    message text NOT NULL,
    last_sent_at timestamptz NOT NULL DEFAULT now()
);

ALTER TABLE auditor_alerts ENABLE ROW LEVEL SECURITY;
