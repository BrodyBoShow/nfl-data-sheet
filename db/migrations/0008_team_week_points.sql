-- Adds offense points scored per team-game, derived from pbp drive results (not the
-- game scoreboard) so it can be garbage-time-filtered and offense-only, matching the
-- rest of team_week's drive-level columns. See pipeline/collectors/nflverse_bulk.py's
-- drive_summary aggregation and docs/signals.md's points_per_drive registry entry for
-- what's included/excluded (PATs/2-point tries and safeties are not counted).
ALTER TABLE team_week ADD COLUMN points int;
