-- teams (from nflreadpy's load_teams()) is a static "every code ever used" reference
-- table -- 36 rows, not 32: every current code plus the retired OAK/SD/STL/LAR aliases
-- (verified live, docs/phases/P2.md's deviations). Without a way to tell them apart,
-- any future code enumerating "the current 32 teams" via `SELECT * FROM teams` would
-- silently include four dead codes. is_active is set explicitly by id_spine on every
-- run (pipeline/collectors/id_spine.py's _RETIRED_TEAM_CODES), not left to this
-- migration's one-time backfill.
ALTER TABLE teams ADD COLUMN is_active boolean NOT NULL DEFAULT true;
UPDATE teams SET is_active = false WHERE team_abbr IN ('OAK', 'SD', 'STL', 'LAR');
