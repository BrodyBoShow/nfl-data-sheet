-- nflverse schedules' `location` column: 'Home' or 'Neutral'. Verified live 2026-09-23
-- via nflreadpy load_schedules (42 'Neutral' games across 2019-2025: international REG
-- games, every Super Bowl, one WC). The spine had no neutral-site flag until now; the P5
-- synthesizer reads it to drop the home-field term at neutral sites. Populated by
-- pipeline/collectors/id_spine.py. Nullable: rows stay null until id_spine re-runs.
ALTER TABLE games
    ADD COLUMN location text CHECK (location IN ('Home', 'Neutral'));
