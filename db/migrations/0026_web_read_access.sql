-- P6 step 0: close the anon surface, then open exactly what the web app reads.
-- See docs/phases/P6.md §2.
--
-- Before this migration, anon and authenticated held full grants (arwdDxtm) on every
-- public table, and postgres's default ACL gave them the same on every new table,
-- sequence, and function. RLS default-deny (0007) was the only thing in the way, and
-- injuries_backup_pre_changelog had RLS off, so it was readable and writable with the
-- anon key.
--
-- The pipeline is unaffected. It connects as `postgres` via SUPABASE_DB_URL (the pooler
-- user postgres.<ref>). That role owns every table here and has bypassrls (see 0007).
-- Nothing in the repo uses the REST API.
--
-- Boundary: row scope lives in RLS policies and column scope in column grants. The `web`
-- views are security_invoker, so they add no privilege of their own. Even if `public`
-- were exposed through the Data API again, anon would still see only these columns and
-- rows.
--
-- Order: apply this migration BEFORE setting Data API -> Exposed schemas to `web`. The
-- schema has to exist first.

-- 1. Close. -------------------------------------------------------------------------

ALTER TABLE injuries_backup_pre_changelog ENABLE ROW LEVEL SECURITY;

REVOKE ALL ON ALL TABLES IN SCHEMA public FROM anon, authenticated;
REVOKE ALL ON ALL SEQUENCES IN SCHEMA public FROM anon, authenticated;
-- Not from PUBLIC, so trigger functions (projection_log_immutable) are untouched.
REVOKE ALL ON ALL FUNCTIONS IN SCHEMA public FROM anon, authenticated;

-- Future objects created by postgres (every migration here) start closed.
-- supabase_admin has the same permissive default ACL in public, and postgres can't alter
-- another role's defaults. It only applies to objects supabase_admin itself creates, and
-- every table here is owned by postgres.
ALTER DEFAULT PRIVILEGES FOR ROLE postgres IN SCHEMA public
    REVOKE ALL ON TABLES FROM anon, authenticated;
ALTER DEFAULT PRIVILEGES FOR ROLE postgres IN SCHEMA public
    REVOKE ALL ON SEQUENCES FROM anon, authenticated;
ALTER DEFAULT PRIVILEGES FOR ROLE postgres IN SCHEMA public
    REVOKE ALL ON FUNCTIONS FROM anon, authenticated;

-- anon keeps USAGE on schema public. The invoker views below need it to resolve their
-- base tables, and with zero table grants beyond the three below it reads nothing else.

-- 2. Open three tables, read-only. ------------------------------------------------------

-- games: exactly L3's identity/schedule columns (CLAUDE.md). Scores, lines, and results
-- are unreadable at the grant level.
GRANT SELECT (game_id, season, week, home_team, away_team, gameday, gametime, location)
    ON games TO anon;
CREATE POLICY games_anon_read ON games FOR SELECT TO anon USING (true);

GRANT SELECT (game_id, season, week, kickoff, projection_status, projected_spread,
              projected_total, edge_spread, edge_total, locked, card, as_of, inputs_version)
    ON matchup_cards TO anon;
CREATE POLICY matchup_cards_anon_read ON matchup_cards FOR SELECT TO anon USING (true);

-- Player rows stay closed in v1. Their team is null, and L3 can't name them
-- (docs/phases/P6.md §8 Q2).
GRANT SELECT (season, week, game_id, team, player_id, sector, signal, value, league_pct,
              sample_n, stability, as_of, inputs_version)
    ON signals TO anon;
CREATE POLICY signals_anon_read ON signals FOR SELECT TO anon USING (player_id IS NULL);

-- 3. The web schema: the only schema exposed through the Data API. -------------------

CREATE SCHEMA web;
GRANT USAGE ON SCHEMA web TO anon;

CREATE VIEW web.games WITH (security_invoker = true) AS
SELECT game_id, season, week, home_team, away_team, gameday, gametime, location
FROM public.games;

CREATE VIEW web.weeks WITH (security_invoker = true) AS
SELECT g.season,
       g.week,
       min(g.gameday) AS first_gameday,
       max(g.gameday) AS last_gameday,
       count(*)::int AS n_games,
       count(c.game_id)::int AS n_cards
FROM public.games g
LEFT JOIN public.matchup_cards c ON c.game_id = g.game_id
GROUP BY g.season, g.week;

-- Flattened for the week view. jsonb extraction and casts only, no arithmetic. Code
-- fields go through numeric because the card stores some of them as floats ("1.0").
CREATE VIEW web.week_cards WITH (security_invoker = true) AS
SELECT game_id,
       season,
       week,
       kickoff,
       projection_status,
       locked,
       as_of,
       projected_spread,
       projected_total,
       card ->> 'projection_status_label' AS projection_status_label,
       card -> 'uncertainty' ->> 'stability_bucket' AS stability_bucket,
       (card -> 'uncertainty' ->> 'stability_min')::double precision AS stability_min,
       (card -> 'market' ->> 'status')::numeric::smallint AS market_status,
       (card -> 'edge' -> 'vs_current' ->> 'market_spread')::double precision
           AS market_spread_latest,
       (card -> 'edge' -> 'vs_current' ->> 'market_total')::double precision
           AS market_total_latest,
       (card -> 'edge' -> 'at_lock' ->> 'market_spread')::double precision
           AS lock_market_spread,
       (card -> 'edge' -> 'at_lock' ->> 'market_total')::double precision
           AS lock_market_total,
       (card -> 'lock' ->> 'locked_at')::timestamptz AS locked_at,
       (card -> 'lock' ->> 'locks_from')::timestamptz AS locks_from,
       (card -> 'context' -> 'environment' -> 'game' ->> 'weather_status')::numeric::smallint
           AS weather_status,
       (card -> 'context' -> 'environment' -> 'game' ->> 'venue_roof_code')::numeric::smallint
           AS venue_roof_code,
       (card -> 'context' -> 'environment' -> 'game' ->> 'temperature_f')::double precision
           AS temperature_f,
       (card -> 'context' -> 'environment' -> 'game' ->> 'wind_speed_mph')::double precision
           AS wind_speed_mph
FROM public.matchup_cards;

CREATE VIEW web.cards WITH (security_invoker = true) AS
SELECT game_id, season, week, kickoff, projection_status, locked, card, as_of, inputs_version
FROM public.matchup_cards;

CREATE VIEW web.signals WITH (security_invoker = true) AS
SELECT season, week, game_id, team, player_id, sector, signal, value, league_pct,
       sample_n, stability, as_of, inputs_version
FROM public.signals;

-- No default ACL exists for schema web, so a view added later needs its own GRANT.
GRANT SELECT ON ALL TABLES IN SCHEMA web TO anon;

NOTIFY pgrst, 'reload schema';
