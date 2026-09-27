-- P7 step 3: widen player_week with the defensive box-score counts the Player efficiency
-- analyst's defense family reads (docs/signals.md, "Player tables (Phase 7)").
-- load_player_stats already carries them; 0006 staged only offense columns. Names and
-- dtypes verified against tests/fixtures/nflreadpy_player_stats_sample.parquet
-- (2026-09-26): def_sacks is Float64 (half sacks), and the other four are Int32.
--
-- Why nflverse and not pfr_advstats for these: where both sources carry a count, the
-- player tables prefer nflverse (CC-BY 4.0, pbp-derived) over PFR/NGS
-- (provenance-basis display, docs/sources.md). PFR stays the source for what only it
-- has: pressures, blitzes, combined/missed tackles, and nearest-defender allowed stats.
--
-- Nullable. Existing rows stay null until the collector next re-fetches their season,
-- and because content_hash covers every stored column, that run rewrites every
-- 2025/2026 row once. ~0.4 MB/season, inside the L2 retention window.

ALTER TABLE player_week
    ADD COLUMN def_sacks double precision,
    ADD COLUMN def_qb_hits int,
    ADD COLUMN def_tackles_for_loss int,
    ADD COLUMN def_pass_defended int,
    ADD COLUMN def_fumbles_forced int;
