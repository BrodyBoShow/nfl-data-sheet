-- P7 step 3: player_game_pbp -- player-keyed counts and sums from nflverse pbp joined to
-- FTN charting, one row per player per game. Staged by pipeline/collectors/nflverse_bulk.py
-- under two tags (pbp, ftn_charting). Aggregates only, never play-level rows (CLAUDE.md).
--
-- The column set is exactly the inputs of the Usage and Player efficiency registry entries
-- (docs/signals.md, "Player tables (Phase 7)"). Source fields and value domains verified
-- 2026-09-26 against tests/fixtures/nflreadpy_pbp_sample.parquet and
-- nflreadpy_ftn_charting_sample.parquet.
--
-- Play scope (every column): pbp rows with play_deleted != 1, a non-null epa,
-- pass == 1 or rush == 1, play_type != 'no_play', qb_kneel != 1, qb_spike != 1,
-- two_point_attempt != 1, season_type REG/POST. Garbage time is NOT excluded, unlike
-- team_week: every other player-level source (player_week, ngs, pfr_advstats) is
-- all-play and can't be filtered, so all player rates share one play scope.
--
-- Roles. A player can hold several in one game, and one row covers all of them:
--   passer    passer_id on qb_dropback == 1. Dropbacks include sacks and scrambles (on a
--             scramble passer_id is set and rusher_id is null; fixture-verified).
--   rusher    rusher_id on rush == 1: designed runs only. Scrambles are pass == 1.
--   receiver  receiver_id on pass_attempt == 1 AND sack == 0: a target.
-- A role's columns are NULL when the player held no plays in that role in this game (a
-- sourced "no plays", 1 bit each), and counts within a held role are real zeros.
-- ftn_* columns are additionally NULL when the game has no FTN charting yet (FTN charts
-- within 48h). ftn_charted_* counts the role's plays that joined an FTN row, and is the
-- denominator for every FTN rate, never the pbp count.
--
-- pbp play_id is Float64 and FTN's nflverse_play_id Int32 (fixtures); cast before the join
-- on (game_id, play_id) = (nflverse_game_id, nflverse_play_id).
--
-- Thresholds match team_week where one exists: explosive rush >= 10 yards, explosive
-- reception >= 20 yards. Deep = air_yards >= 20. Red zone = yardline_100 <= 20, goal line
-- = yardline_100 <= 5. End-zone target = air_yards >= yardline_100.
--
-- Storage: ~330 rows/week; with absent roles NULL, ~3 MB/season, inside the L2 retention
-- window ([season-1, season], docs/phases/P7.md "Retention policies").

CREATE TABLE player_game_pbp (
    game_id text NOT NULL REFERENCES games (game_id),
    player_id text NOT NULL REFERENCES players (player_id),
    season int NOT NULL,
    week int NOT NULL,
    season_type text NOT NULL CHECK (season_type IN ('REG', 'POST')),
    team text NOT NULL REFERENCES teams (team_abbr),            -- posteam
    opponent_team text NOT NULL REFERENCES teams (team_abbr),   -- defteam

    -- passer (qb_dropback == 1) -----------------------------------------------------
    dropbacks int,
    dropback_epa_sum double precision,
    dropback_success int,
    pass_attempts int,            -- pass_attempt == 1 AND sack == 0
    completions int,              -- complete_pass == 1
    interceptions int,            -- interception == 1
    sacks int,                    -- sack == 1
    scrambles int,                -- qb_scramble == 1
    pass_air_yards_sum double precision,   -- attempts with non-null air_yards
    pass_air_yards_n int,
    cpoe_sum double precision,    -- attempts with non-null cpoe
    cpoe_n int,
    deep_attempts int,            -- attempts with air_yards >= 20
    -- passer, FTN
    ftn_charted_dropbacks int,
    ftn_pa_dropbacks int,                  -- is_play_action
    ftn_pa_epa_sum double precision,
    ftn_blitzed_dropbacks int,             -- n_blitzers > 0
    ftn_blitzed_epa_sum double precision,
    ftn_out_of_pocket_dropbacks int,       -- is_qb_out_of_pocket
    ftn_charted_attempts int,
    ftn_screen_attempts int,               -- is_screen_pass
    ftn_throwaways int,                    -- is_throw_away
    ftn_catchable_attempts int,            -- is_catchable_ball
    ftn_int_worthy int,                    -- is_interception_worthy
    ftn_charted_sacks int,
    ftn_qb_fault_sacks int,                -- is_qb_fault_sack

    -- rusher (rush == 1) ------------------------------------------------------------
    carries int,
    rush_epa_sum double precision,
    rush_success int,
    rush_yards int,               -- sum of yards_gained
    rush_stuffs int,              -- yards_gained <= 0
    rush_explosive int,           -- yards_gained >= 10
    rush_first_downs int,         -- first_down_rush == 1
    rz_carries int,
    gl_carries int,
    -- rush by gap: run_location left/middle/right x run_gap end/tackle/guard (null on
    -- middle), 7 cells. Carries with a null run_location are in `carries` only.
    carries_le int,
    carries_lt int,
    carries_lg int,
    carries_mid int,
    carries_rg int,
    carries_rt int,
    carries_re int,
    rush_epa_sum_le double precision,
    rush_epa_sum_lt double precision,
    rush_epa_sum_lg double precision,
    rush_epa_sum_mid double precision,
    rush_epa_sum_rg double precision,
    rush_epa_sum_rt double precision,
    rush_epa_sum_re double precision,
    rush_success_le int,
    rush_success_lt int,
    rush_success_lg int,
    rush_success_mid int,
    rush_success_rg int,
    rush_success_rt int,
    rush_success_re int,
    -- rusher, FTN
    ftn_charted_carries int,
    ftn_stacked_box_carries int,           -- n_defense_box >= 8
    ftn_stacked_box_epa_sum double precision,

    -- receiver (targets) ------------------------------------------------------------
    targets int,
    receptions int,               -- complete_pass == 1
    rec_epa_sum double precision,
    rec_success int,
    rec_yards int,                -- sum of yards_gained on targets
    rec_air_yards_sum double precision,    -- targets with non-null air_yards
    rec_air_yards_n int,
    rec_yac_sum double precision,          -- yards_after_catch on receptions
    rec_yac_oe_sum double precision,       -- yards_after_catch - xyac_mean_yardage,
    rec_yac_oe_n int,                      --   receptions where both are non-null
    rec_first_downs int,          -- complete_pass == 1 AND first_down_pass == 1
    rec_explosive int,            -- complete_pass == 1 AND yards_gained >= 20
    deep_targets int,             -- air_yards >= 20
    rz_targets int,
    ez_targets int,
    -- by pass_location: a field-location split, never slot/perimeter alignment
    -- (docs/phases/P7.md). Targets with a null pass_location are in `targets` only.
    targets_left int,
    targets_middle int,
    targets_right int,
    rec_epa_sum_left double precision,
    rec_epa_sum_middle double precision,
    rec_epa_sum_right double precision,
    -- receiver, FTN
    ftn_charted_targets int,
    ftn_charted_receptions int,
    ftn_catchable_targets int,             -- is_catchable_ball
    ftn_catchable_receptions int,
    ftn_drops int,                         -- is_drop
    ftn_contested_targets int,             -- is_contested_ball
    ftn_contested_receptions int,
    ftn_created_receptions int,            -- is_created_reception
    ftn_screen_targets int,                -- is_screen_pass
    ftn_pa_targets int,                    -- is_play_action
    ftn_pa_rec_epa_sum double precision,

    content_hash text NOT NULL,
    updated_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (game_id, player_id)
);

CREATE INDEX player_game_pbp_season_week_idx ON player_game_pbp (season, week);
CREATE INDEX player_game_pbp_player_season_idx ON player_game_pbp (player_id, season);

ALTER TABLE player_game_pbp ENABLE ROW LEVEL SECURITY;
