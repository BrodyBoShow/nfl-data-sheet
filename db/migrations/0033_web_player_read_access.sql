-- P7 step 9: open the player tables to the web player view, read-only, column by column.
-- See docs/phases/P6.md §2 and docs/phases/P7.md step 9.
--
-- Same boundary as 0026: row scope in RLS policies, column scope in column grants, and
-- the `web` views are security_invoker, so they add no privilege of their own. anon can
-- read exactly the columns granted here, through the two views below, and nothing else
-- in these tables (no content_hash, no unshown metric, no other player column).
--
-- What the page shows (docs/phases/P7.md step 9, the shortlist round):
--   - season-to-date (_std) values for the shortlisted metrics, plus three one-column
--     swap candidates (scramble_rate, qb_hits_per_snap, missed_tackle_rate) so the page
--     can take a swap without another migration;
--   - each family's headline _pct and _l4, with the family samples and stability;
--   - usage shares for the season and the last game;
--   - the participation _hist columns with their span.
-- Player values are never dimmed (decided 2026-09-30); stability is shown as a number.
-- inputs_version isn't granted: it's a provenance string the page doesn't show, 329
-- characters per player_eff_week row (of ~2,100 bytes of JSON) and 134 per usage row
-- (measured 2026-10-01).

-- 1. The player tables: a column allow-list each, every row readable. -------------------

GRANT SELECT (
    player_id, season, week, season_type, game_id, team, position_group, as_of,
    usage_games_std, usage_stability,
    off_snap_share_std, off_snap_share_game,
    def_snap_share_std, def_snap_share_game,
    target_share_std, target_share_game,
    air_yards_share_std, air_yards_share_game,
    carry_share_std, carry_share_game,
    rz_target_share_std, rz_target_share_game,
    rz_carry_share_std, rz_carry_share_game
) ON player_usage_week TO anon;
CREATE POLICY player_usage_week_anon_read ON player_usage_week
    FOR SELECT TO anon USING (true);

GRANT SELECT (
    player_id, season, week, season_type, game_id, team, position_group, as_of,
    -- family samples and stability
    rec_targets_std, rec_targets_l4, rec_stability,
    rush_carries_std, rush_carries_l4, rush_stability,
    pass_dropbacks_std, pass_dropbacks_l4, pass_stability,
    def_snaps_std, def_snaps_l4, def_stability,
    -- receiving
    epa_per_target_std, epa_per_target_pct, epa_per_target_l4,
    rec_success_rate_std, yards_per_target_std, rec_adot_std, yac_oe_per_reception_std,
    avg_separation_std,
    -- passing
    epa_per_dropback_std, epa_per_dropback_pct, epa_per_dropback_l4,
    dropback_success_rate_std, cpoe_std, pass_adot_std, sack_rate_std, pressure_rate_std,
    avg_time_to_throw_std, scramble_rate_std,
    -- rushing
    epa_per_carry_std, epa_per_carry_pct, epa_per_carry_l4,
    rush_success_rate_std, stuff_rate_std, rush_explosive_rate_std,
    yards_before_contact_per_carry_std,
    -- defense (tackles_per_snap_pct is null while PFR stays gated; it's granted so open
    -- item 9 can bring the rank back as a data change, not a migration)
    tackles_per_snap_std, tackles_per_snap_pct, tackles_per_snap_l4,
    tfl_per_snap_std, pressures_per_snap_std, sacks_per_snap_std, qb_hits_per_snap_std,
    targets_per_snap_std, yards_per_target_allowed_std, missed_tackle_rate_std,
    -- participation history (one value per season, with its span)
    hist_span, rec_hist_n, pass_hist_n,
    epa_per_target_vs_man_hist, epa_per_target_vs_zone_hist,
    target_rate_vs_man_hist, target_rate_vs_zone_hist,
    epa_per_dropback_vs_man_hist, epa_per_dropback_vs_zone_hist
) ON player_eff_week TO anon;
CREATE POLICY player_eff_week_anon_read ON player_eff_week
    FOR SELECT TO anon USING (true);

-- 2. players: a name and a position, only for players the site can show. ----------------
-- The invoker views join players, so anon needs these three columns. The policy limits
-- rows to players with a player-table row, not every player nflverse has ever listed.
GRANT SELECT (player_id, display_name, position) ON players TO anon;
CREATE POLICY players_anon_read ON players FOR SELECT TO anon USING (
    EXISTS (SELECT 1 FROM player_usage_week u WHERE u.player_id = players.player_id)
    OR EXISTS (SELECT 1 FROM player_eff_week e WHERE e.player_id = players.player_id)
);

-- 3. The views. -------------------------------------------------------------------------
-- `next_week` is the player's next row in the same season (lead, no arithmetic). It lets
-- PostgREST select each player's latest row as of a week W without RPC:
--   week=lte.W & or=(next_week.is.null,next_week.gt.W)
-- (docs/signals.md: a row is written only for weeks the player played, so "as of W" is
-- the latest row with week <= W.)

CREATE VIEW web.player_usage WITH (security_invoker = true) AS
SELECT u.player_id, p.display_name, p.position, u.position_group,
       u.season, u.week, u.season_type, u.game_id, u.team,
       lead(u.week) OVER (PARTITION BY u.player_id, u.season ORDER BY u.week) AS next_week,
       u.usage_games_std, u.usage_stability,
       u.off_snap_share_std, u.off_snap_share_game,
       u.def_snap_share_std, u.def_snap_share_game,
       u.target_share_std, u.target_share_game,
       u.air_yards_share_std, u.air_yards_share_game,
       u.carry_share_std, u.carry_share_game,
       u.rz_target_share_std, u.rz_target_share_game,
       u.rz_carry_share_std, u.rz_carry_share_game,
       u.as_of
FROM public.player_usage_week u
LEFT JOIN public.players p ON p.player_id = u.player_id;

CREATE VIEW web.player_eff WITH (security_invoker = true) AS
SELECT e.player_id, p.display_name, p.position, e.position_group,
       e.season, e.week, e.season_type, e.game_id, e.team,
       lead(e.week) OVER (PARTITION BY e.player_id, e.season ORDER BY e.week) AS next_week,
       e.rec_targets_std, e.rec_targets_l4, e.rec_stability,
       e.rush_carries_std, e.rush_carries_l4, e.rush_stability,
       e.pass_dropbacks_std, e.pass_dropbacks_l4, e.pass_stability,
       e.def_snaps_std, e.def_snaps_l4, e.def_stability,
       e.epa_per_target_std, e.epa_per_target_pct, e.epa_per_target_l4,
       e.rec_success_rate_std, e.yards_per_target_std, e.rec_adot_std,
       e.yac_oe_per_reception_std, e.avg_separation_std,
       e.epa_per_dropback_std, e.epa_per_dropback_pct, e.epa_per_dropback_l4,
       e.dropback_success_rate_std, e.cpoe_std, e.pass_adot_std, e.sack_rate_std,
       e.pressure_rate_std, e.avg_time_to_throw_std, e.scramble_rate_std,
       e.epa_per_carry_std, e.epa_per_carry_pct, e.epa_per_carry_l4,
       e.rush_success_rate_std, e.stuff_rate_std, e.rush_explosive_rate_std,
       e.yards_before_contact_per_carry_std,
       e.tackles_per_snap_std, e.tackles_per_snap_pct, e.tackles_per_snap_l4,
       e.tfl_per_snap_std, e.pressures_per_snap_std, e.sacks_per_snap_std,
       e.qb_hits_per_snap_std, e.targets_per_snap_std, e.yards_per_target_allowed_std,
       e.missed_tackle_rate_std,
       e.hist_span, e.rec_hist_n, e.pass_hist_n,
       e.epa_per_target_vs_man_hist, e.epa_per_target_vs_zone_hist,
       e.target_rate_vs_man_hist, e.target_rate_vs_zone_hist,
       e.epa_per_dropback_vs_man_hist, e.epa_per_dropback_vs_zone_hist,
       e.as_of
FROM public.player_eff_week e
LEFT JOIN public.players p ON p.player_id = e.player_id;

-- No default ACL exists for schema web (0026), so each new view needs its own grant.
GRANT SELECT ON web.player_usage, web.player_eff TO anon;

NOTIFY pgrst, 'reload schema';
