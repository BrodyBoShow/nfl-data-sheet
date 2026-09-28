-- P7 step 3: player_eff_week -- the Player efficiency analyst's
-- (pipeline/analysts/player_efficiency.py) per-player output. The second of the two player
-- tables allowed outside `signals` (CLAUDE.md layer rules; contract and metric registry:
-- docs/signals.md, "Player tables" and "Player tables (Phase 7)"). The column list IS the
-- registry: add a metric here only together with its registry entry.
--
-- As-of rows: a row for week W exists only for a player with a nonzero sample in at least
-- one family in week W (targets, carries, dropbacks, or defensive snaps). Read "as of week
-- W" as each player's latest row with week <= W in that season.
--
-- Per metric, one line each below: <m>_std (season to date, prior-blended), <m>_game (this
-- row's game, raw), <m>_l4 (last 4 games played, raw), <m>_pct (smallint 0-100, league
-- percentile of _std within position_group, among players meeting the family minimum).
-- Every window is sum(numerator) / sum(denominator) over its games, never a mean of
-- per-game rates. NULL = not sourced or no sample, never zero-filled.
--
-- Per family: the sample count in the same three windows plus a stability (0-1, the
-- signals meaning: w_cur + w_prior of the family's headline metric).
--
-- `_hist` columns are participation-derived multi-season tendencies. They're a single value
-- each, constant within a season, never current-season behavior; hist_span names the
-- seasons (e.g. '2023-2025') and the UI shows it beside every _hist value.
--
-- Attribution: PFR-derived columns (pfr_advstats) credit Sports Reference, NGS-derived
-- columns (ngs) credit NFL Next Gen Stats, FTN-derived columns (ftn_*) and _hist columns
-- carry "FTN Data via nflverse" (docs/sources.md, nflverse bulk -> License). The registry
-- entry of each metric names its source.
--
-- Not readable by anon (0026's default privileges; RLS default-deny). The web player view
-- (P7 step 9) brings its own grant migration. Retention: nothing deletes from or collapses
-- this table. L4 (keeping only each player's latest row of a completed season) is deferred
-- to P7 step 7, because it would destroy point-in-time weekly history that can't be
-- recomputed (docs/phases/P7.md, "Retention policies"). pipeline/orchestration/retention.py
-- lists this table as not deleted.

CREATE TABLE player_eff_week (
    player_id text NOT NULL REFERENCES players (player_id),
    season int NOT NULL,
    week int NOT NULL,
    season_type text NOT NULL CHECK (season_type IN ('REG', 'POST')),
    game_id text NOT NULL REFERENCES games (game_id),   -- the game _game comes from
    team text NOT NULL REFERENCES teams (team_abbr),
    position_group text,                                -- players.position_group
    as_of timestamptz NOT NULL,
    inputs_version text NOT NULL,

    -- family samples and stability ----------------------------------------------------
    rec_targets_std int, rec_targets_game smallint, rec_targets_l4 smallint, rec_stability real,
    rush_carries_std int, rush_carries_game smallint, rush_carries_l4 smallint, rush_stability real,
    pass_dropbacks_std int, pass_dropbacks_game smallint, pass_dropbacks_l4 smallint, pass_stability real,
    def_snaps_std int, def_snaps_game smallint, def_snaps_l4 smallint, def_stability real,

    -- receiving (23 metrics; sample rec_targets) --------------------------------------
    epa_per_target_std real, epa_per_target_game real, epa_per_target_l4 real, epa_per_target_pct smallint,
    rec_success_rate_std real, rec_success_rate_game real, rec_success_rate_l4 real, rec_success_rate_pct smallint,
    catch_rate_std real, catch_rate_game real, catch_rate_l4 real, catch_rate_pct smallint,
    yards_per_target_std real, yards_per_target_game real, yards_per_target_l4 real, yards_per_target_pct smallint,
    rec_adot_std real, rec_adot_game real, rec_adot_l4 real, rec_adot_pct smallint,
    yac_per_reception_std real, yac_per_reception_game real, yac_per_reception_l4 real, yac_per_reception_pct smallint,
    yac_oe_per_reception_std real, yac_oe_per_reception_game real, yac_oe_per_reception_l4 real, yac_oe_per_reception_pct smallint,
    rec_first_down_rate_std real, rec_first_down_rate_game real, rec_first_down_rate_l4 real, rec_first_down_rate_pct smallint,
    rec_explosive_rate_std real, rec_explosive_rate_game real, rec_explosive_rate_l4 real, rec_explosive_rate_pct smallint,
    deep_target_rate_std real, deep_target_rate_game real, deep_target_rate_l4 real, deep_target_rate_pct smallint,
    epa_per_target_left_std real, epa_per_target_left_game real, epa_per_target_left_l4 real, epa_per_target_left_pct smallint,
    epa_per_target_middle_std real, epa_per_target_middle_game real, epa_per_target_middle_l4 real, epa_per_target_middle_pct smallint,
    epa_per_target_right_std real, epa_per_target_right_game real, epa_per_target_right_l4 real, epa_per_target_right_pct smallint,
    catchable_catch_rate_std real, catchable_catch_rate_game real, catchable_catch_rate_l4 real, catchable_catch_rate_pct smallint,
    drop_rate_std real, drop_rate_game real, drop_rate_l4 real, drop_rate_pct smallint,
    contested_target_rate_std real, contested_target_rate_game real, contested_target_rate_l4 real, contested_target_rate_pct smallint,
    contested_catch_rate_std real, contested_catch_rate_game real, contested_catch_rate_l4 real, contested_catch_rate_pct smallint,
    created_reception_rate_std real, created_reception_rate_game real, created_reception_rate_l4 real, created_reception_rate_pct smallint,
    screen_target_rate_std real, screen_target_rate_game real, screen_target_rate_l4 real, screen_target_rate_pct smallint,
    epa_per_target_play_action_std real, epa_per_target_play_action_game real, epa_per_target_play_action_l4 real, epa_per_target_play_action_pct smallint,
    broken_tackles_per_reception_std real, broken_tackles_per_reception_game real, broken_tackles_per_reception_l4 real, broken_tackles_per_reception_pct smallint,
    avg_separation_std real, avg_separation_game real, avg_separation_l4 real, avg_separation_pct smallint,
    avg_cushion_std real, avg_cushion_game real, avg_cushion_l4 real, avg_cushion_pct smallint,

    -- rushing (34 metrics; sample rush_carries) ---------------------------------------
    epa_per_carry_std real, epa_per_carry_game real, epa_per_carry_l4 real, epa_per_carry_pct smallint,
    rush_success_rate_std real, rush_success_rate_game real, rush_success_rate_l4 real, rush_success_rate_pct smallint,
    yards_per_carry_std real, yards_per_carry_game real, yards_per_carry_l4 real, yards_per_carry_pct smallint,
    stuff_rate_std real, stuff_rate_game real, stuff_rate_l4 real, stuff_rate_pct smallint,
    rush_explosive_rate_std real, rush_explosive_rate_game real, rush_explosive_rate_l4 real, rush_explosive_rate_pct smallint,
    rush_first_down_rate_std real, rush_first_down_rate_game real, rush_first_down_rate_l4 real, rush_first_down_rate_pct smallint,
    gap_share_le_std real, gap_share_le_game real, gap_share_le_l4 real, gap_share_le_pct smallint,
    gap_share_lt_std real, gap_share_lt_game real, gap_share_lt_l4 real, gap_share_lt_pct smallint,
    gap_share_lg_std real, gap_share_lg_game real, gap_share_lg_l4 real, gap_share_lg_pct smallint,
    gap_share_mid_std real, gap_share_mid_game real, gap_share_mid_l4 real, gap_share_mid_pct smallint,
    gap_share_rg_std real, gap_share_rg_game real, gap_share_rg_l4 real, gap_share_rg_pct smallint,
    gap_share_rt_std real, gap_share_rt_game real, gap_share_rt_l4 real, gap_share_rt_pct smallint,
    gap_share_re_std real, gap_share_re_game real, gap_share_re_l4 real, gap_share_re_pct smallint,
    epa_per_carry_le_std real, epa_per_carry_le_game real, epa_per_carry_le_l4 real, epa_per_carry_le_pct smallint,
    epa_per_carry_lt_std real, epa_per_carry_lt_game real, epa_per_carry_lt_l4 real, epa_per_carry_lt_pct smallint,
    epa_per_carry_lg_std real, epa_per_carry_lg_game real, epa_per_carry_lg_l4 real, epa_per_carry_lg_pct smallint,
    epa_per_carry_mid_std real, epa_per_carry_mid_game real, epa_per_carry_mid_l4 real, epa_per_carry_mid_pct smallint,
    epa_per_carry_rg_std real, epa_per_carry_rg_game real, epa_per_carry_rg_l4 real, epa_per_carry_rg_pct smallint,
    epa_per_carry_rt_std real, epa_per_carry_rt_game real, epa_per_carry_rt_l4 real, epa_per_carry_rt_pct smallint,
    epa_per_carry_re_std real, epa_per_carry_re_game real, epa_per_carry_re_l4 real, epa_per_carry_re_pct smallint,
    rush_success_rate_le_std real, rush_success_rate_le_game real, rush_success_rate_le_l4 real, rush_success_rate_le_pct smallint,
    rush_success_rate_lt_std real, rush_success_rate_lt_game real, rush_success_rate_lt_l4 real, rush_success_rate_lt_pct smallint,
    rush_success_rate_lg_std real, rush_success_rate_lg_game real, rush_success_rate_lg_l4 real, rush_success_rate_lg_pct smallint,
    rush_success_rate_mid_std real, rush_success_rate_mid_game real, rush_success_rate_mid_l4 real, rush_success_rate_mid_pct smallint,
    rush_success_rate_rg_std real, rush_success_rate_rg_game real, rush_success_rate_rg_l4 real, rush_success_rate_rg_pct smallint,
    rush_success_rate_rt_std real, rush_success_rate_rt_game real, rush_success_rate_rt_l4 real, rush_success_rate_rt_pct smallint,
    rush_success_rate_re_std real, rush_success_rate_re_game real, rush_success_rate_re_l4 real, rush_success_rate_re_pct smallint,
    stacked_box_rate_std real, stacked_box_rate_game real, stacked_box_rate_l4 real, stacked_box_rate_pct smallint,
    epa_per_carry_stacked_box_std real, epa_per_carry_stacked_box_game real, epa_per_carry_stacked_box_l4 real, epa_per_carry_stacked_box_pct smallint,
    yards_before_contact_per_carry_std real, yards_before_contact_per_carry_game real, yards_before_contact_per_carry_l4 real, yards_before_contact_per_carry_pct smallint,
    yards_after_contact_per_carry_std real, yards_after_contact_per_carry_game real, yards_after_contact_per_carry_l4 real, yards_after_contact_per_carry_pct smallint,
    broken_tackles_per_carry_std real, broken_tackles_per_carry_game real, broken_tackles_per_carry_l4 real, broken_tackles_per_carry_pct smallint,
    ryoe_per_carry_std real, ryoe_per_carry_game real, ryoe_per_carry_l4 real, ryoe_per_carry_pct smallint,
    avg_time_to_los_std real, avg_time_to_los_game real, avg_time_to_los_l4 real, avg_time_to_los_pct smallint,

    -- passing (23 metrics; sample pass_dropbacks) -------------------------------------
    epa_per_dropback_std real, epa_per_dropback_game real, epa_per_dropback_l4 real, epa_per_dropback_pct smallint,
    dropback_success_rate_std real, dropback_success_rate_game real, dropback_success_rate_l4 real, dropback_success_rate_pct smallint,
    cpoe_std real, cpoe_game real, cpoe_l4 real, cpoe_pct smallint,
    pass_adot_std real, pass_adot_game real, pass_adot_l4 real, pass_adot_pct smallint,
    sack_rate_std real, sack_rate_game real, sack_rate_l4 real, sack_rate_pct smallint,
    scramble_rate_std real, scramble_rate_game real, scramble_rate_l4 real, scramble_rate_pct smallint,
    int_rate_std real, int_rate_game real, int_rate_l4 real, int_rate_pct smallint,
    deep_attempt_rate_std real, deep_attempt_rate_game real, deep_attempt_rate_l4 real, deep_attempt_rate_pct smallint,
    play_action_rate_std real, play_action_rate_game real, play_action_rate_l4 real, play_action_rate_pct smallint,
    epa_per_dropback_play_action_std real, epa_per_dropback_play_action_game real, epa_per_dropback_play_action_l4 real, epa_per_dropback_play_action_pct smallint,
    blitzed_rate_std real, blitzed_rate_game real, blitzed_rate_l4 real, blitzed_rate_pct smallint,
    epa_per_dropback_vs_blitz_std real, epa_per_dropback_vs_blitz_game real, epa_per_dropback_vs_blitz_l4 real, epa_per_dropback_vs_blitz_pct smallint,
    out_of_pocket_rate_std real, out_of_pocket_rate_game real, out_of_pocket_rate_l4 real, out_of_pocket_rate_pct smallint,
    screen_rate_std real, screen_rate_game real, screen_rate_l4 real, screen_rate_pct smallint,
    throwaway_rate_std real, throwaway_rate_game real, throwaway_rate_l4 real, throwaway_rate_pct smallint,
    catchable_rate_std real, catchable_rate_game real, catchable_rate_l4 real, catchable_rate_pct smallint,
    int_worthy_rate_std real, int_worthy_rate_game real, int_worthy_rate_l4 real, int_worthy_rate_pct smallint,
    qb_fault_sack_share_std real, qb_fault_sack_share_game real, qb_fault_sack_share_l4 real, qb_fault_sack_share_pct smallint,
    pressure_rate_std real, pressure_rate_game real, pressure_rate_l4 real, pressure_rate_pct smallint,
    pressure_to_sack_rate_std real, pressure_to_sack_rate_game real, pressure_to_sack_rate_l4 real, pressure_to_sack_rate_pct smallint,
    avg_time_to_throw_std real, avg_time_to_throw_game real, avg_time_to_throw_l4 real, avg_time_to_throw_pct smallint,
    aggressiveness_std real, aggressiveness_game real, aggressiveness_l4 real, aggressiveness_pct smallint,
    avg_air_yards_to_sticks_std real, avg_air_yards_to_sticks_game real, avg_air_yards_to_sticks_l4 real, avg_air_yards_to_sticks_pct smallint,

    -- defense (16 metrics; sample def_snaps) ------------------------------------------
    tackles_per_snap_std real, tackles_per_snap_game real, tackles_per_snap_l4 real, tackles_per_snap_pct smallint,
    missed_tackle_rate_std real, missed_tackle_rate_game real, missed_tackle_rate_l4 real, missed_tackle_rate_pct smallint,
    tfl_per_snap_std real, tfl_per_snap_game real, tfl_per_snap_l4 real, tfl_per_snap_pct smallint,
    sacks_per_snap_std real, sacks_per_snap_game real, sacks_per_snap_l4 real, sacks_per_snap_pct smallint,
    qb_hits_per_snap_std real, qb_hits_per_snap_game real, qb_hits_per_snap_l4 real, qb_hits_per_snap_pct smallint,
    pressures_per_snap_std real, pressures_per_snap_game real, pressures_per_snap_l4 real, pressures_per_snap_pct smallint,
    blitzes_per_snap_std real, blitzes_per_snap_game real, blitzes_per_snap_l4 real, blitzes_per_snap_pct smallint,
    forced_fumbles_per_snap_std real, forced_fumbles_per_snap_game real, forced_fumbles_per_snap_l4 real, forced_fumbles_per_snap_pct smallint,
    pass_defended_per_snap_std real, pass_defended_per_snap_game real, pass_defended_per_snap_l4 real, pass_defended_per_snap_pct smallint,
    targets_per_snap_std real, targets_per_snap_game real, targets_per_snap_l4 real, targets_per_snap_pct smallint,
    completion_pct_allowed_std real, completion_pct_allowed_game real, completion_pct_allowed_l4 real, completion_pct_allowed_pct smallint,
    yards_per_target_allowed_std real, yards_per_target_allowed_game real, yards_per_target_allowed_l4 real, yards_per_target_allowed_pct smallint,
    yac_allowed_per_completion_std real, yac_allowed_per_completion_game real, yac_allowed_per_completion_l4 real, yac_allowed_per_completion_pct smallint,
    adot_allowed_std real, adot_allowed_game real, adot_allowed_l4 real, adot_allowed_pct smallint,
    td_rate_allowed_std real, td_rate_allowed_game real, td_rate_allowed_l4 real, td_rate_allowed_pct smallint,
    int_rate_on_targets_std real, int_rate_on_targets_game real, int_rate_on_targets_l4 real, int_rate_on_targets_pct smallint,

    -- participation _hist (single values; see header) --------------------------------
    hist_span text,
    rec_hist_n int,              -- labeled on-field dropbacks behind the receiving _hist values
    epa_per_target_vs_man_hist real,
    epa_per_target_vs_zone_hist real,
    target_rate_vs_man_hist real,
    target_rate_vs_zone_hist real,
    pass_hist_n int,             -- labeled dropbacks as passer
    epa_per_dropback_vs_man_hist real,
    epa_per_dropback_vs_zone_hist real,

    content_hash text NOT NULL,
    updated_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (player_id, season, week),
    CHECK (rec_stability IS NULL OR rec_stability BETWEEN 0 AND 1),
    CHECK (rush_stability IS NULL OR rush_stability BETWEEN 0 AND 1),
    CHECK (pass_stability IS NULL OR pass_stability BETWEEN 0 AND 1),
    CHECK (def_stability IS NULL OR def_stability BETWEEN 0 AND 1)
);

CREATE INDEX player_eff_week_season_week_idx ON player_eff_week (season, week);
CREATE INDEX player_eff_week_team_idx ON player_eff_week (season, team, week);

ALTER TABLE player_eff_week ENABLE ROW LEVEL SECURITY;
