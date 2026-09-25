-- P7 step 3: widen the staged ngs / pfr_advstats / ftn tables to the free, already-
-- verified columns 0006 left out (inventory: docs/phases/P7.md "Source inventory";
-- column names and dtypes verified against tests/fixtures/nflreadpy_{nextgen_*,
-- pfr_advstats_*,ftn_charting}_sample.parquet on 2026-09-25).
--
-- Why counts, not just rates: the P7 analysts build season-to-date values, and a season
-- rate is sum(numerator) / sum(denominator), never a mean of per-game percentages. So
-- each rate family gets the counts it needs as weights (NGS attempts/targets/rush
-- attempts; PFR drops, bad throws, pressures, contact yards, targets, completions, ...).
--
-- Every new column is nullable. Existing rows stay null until the collector next
-- re-fetches their season (it fetches [season-1, season]). Because content_hash covers
-- every stored column, that first run rewrites every 2025/2026 row once.
--
-- Storage (docs/phases/P7.md "Storage design"): ~2.3 MB/season, inside the L2 retention
-- window (staged player tables keep [season-1, season]).

-- ngs: one table for all three stat_types (0006), so each column is null outside its
-- own stat_type. avg_intended_air_yards exists in both passing and receiving; the row's
-- stat_type says which. Source dtypes: Float64 averages, Int32 counts.
ALTER TABLE ngs
    -- passing
    ADD COLUMN avg_time_to_throw double precision,
    ADD COLUMN avg_completed_air_yards double precision,
    ADD COLUMN avg_intended_air_yards double precision,
    ADD COLUMN avg_air_yards_to_sticks double precision,
    ADD COLUMN max_completed_air_distance double precision,
    ADD COLUMN avg_air_distance double precision,
    ADD COLUMN max_air_distance double precision,
    ADD COLUMN attempts int,
    ADD COLUMN completions int,
    -- rushing
    ADD COLUMN efficiency double precision,
    ADD COLUMN avg_time_to_los double precision,
    ADD COLUMN expected_rush_yards double precision,
    ADD COLUMN rush_pct_over_expected double precision,
    ADD COLUMN rush_attempts int,
    -- receiving
    ADD COLUMN avg_cushion double precision,
    ADD COLUMN percent_share_of_intended_air_yards double precision,
    ADD COLUMN catch_percentage double precision,
    ADD COLUMN avg_yac double precision,
    ADD COLUMN avg_expected_yac double precision,
    ADD COLUMN targets int,
    ADD COLUMN receptions int;

-- pfr_advstats: every source column is Float64 in nflreadpy, counts included (0006's
-- note), so all are double precision. Null outside their stat_type. passing_drops and
-- receiving_drop appear in both `pass` and `rec` rows in the source (fixture-verified);
-- the row's stat_type says whose drops they are. No position column exists in `def`;
-- join snaps.position (docs/sources.md).
ALTER TABLE pfr_advstats
    -- pass
    ADD COLUMN passing_drops double precision,
    ADD COLUMN passing_bad_throws double precision,
    ADD COLUMN times_sacked double precision,
    ADD COLUMN times_pressured double precision,
    -- rush
    ADD COLUMN carries double precision,
    ADD COLUMN rushing_yards_before_contact double precision,
    ADD COLUMN rushing_yards_after_contact double precision,
    -- rec
    ADD COLUMN receiving_drop double precision,
    ADD COLUMN receiving_int double precision,
    ADD COLUMN receiving_rat double precision,
    -- def: PFR's nearest-defender charting, NOT a coverage assignment
    -- (docs/phases/P7.md "Coverage: who covered whom")
    ADD COLUMN def_ints double precision,
    ADD COLUMN def_targets double precision,
    ADD COLUMN def_completions_allowed double precision,
    ADD COLUMN def_completion_pct double precision,
    ADD COLUMN def_yards_allowed double precision,
    ADD COLUMN def_yards_allowed_per_cmp double precision,
    ADD COLUMN def_yards_allowed_per_tgt double precision,
    ADD COLUMN def_receiving_td_allowed double precision,
    ADD COLUMN def_adot double precision,
    ADD COLUMN def_air_yards_completed double precision,
    ADD COLUMN def_yards_after_catch double precision,
    ADD COLUMN def_times_blitzed double precision,
    ADD COLUMN def_times_hurried double precision,
    ADD COLUMN def_times_hitqb double precision,
    ADD COLUMN def_sacks double precision,
    ADD COLUMN def_tackles_combined double precision,
    ADD COLUMN def_missed_tackles double precision;

-- ftn: the 13 charting columns 0006 didn't keep (nflreadr dictionary_ftn_charting.csv).
-- read_thrown codes: '0' first read (2023+ only; NA in 2022), '1', '2', 'CHK' checkdown,
-- 'DES' designed, 'SD' scramble drill. qb_location: U/S/P. starting_hash: L/M/R.
ALTER TABLE ftn
    ADD COLUMN starting_hash text,
    ADD COLUMN qb_location text,
    ADD COLUMN read_thrown text,
    ADD COLUMN is_trick_play boolean,
    ADD COLUMN is_qb_out_of_pocket boolean,
    ADD COLUMN is_interception_worthy boolean,
    ADD COLUMN is_throw_away boolean,
    ADD COLUMN is_catchable_ball boolean,
    ADD COLUMN is_contested_ball boolean,
    ADD COLUMN is_created_reception boolean,
    ADD COLUMN is_drop boolean,
    ADD COLUMN is_qb_sneak boolean,
    ADD COLUMN is_qb_fault_sack boolean;
