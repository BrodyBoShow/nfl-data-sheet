-- P7 step 3: participation_player_season -- per-player, per-season counts from nflverse
-- participation (load_participation, tag pbp_participation) joined to that season's pbp.
-- Staged by pipeline/collectors/nflverse_bulk.py. It's the source of every `_hist` value
-- in player_eff_week (docs/signals.md, "Player tables (Phase 7)").
--
-- Historical only. nflreadpy raises for the current season, and the release is published
-- after each postseason (docs/sources.md, "P7 research"). **Nothing here is current-season
-- behavior.** Every derived value ends in `_hist` and shows its season span.
--
-- Attribution by season, from the verbatim license (docs/sources.md): 2023+ is "FTN Data
-- via nflverse", 2022 and earlier "NFL NextGenStats via nflverse". The row's season says
-- which, so there's no provider column. The analysts use 2023+ only (the `_hist` window
-- is the three seasons before the current one), so v1 carries FTN attribution only.
--
-- Join and play scope: participation (nflverse_game_id, play_id) to pbp (game_id,
-- play_id). Participation play_id is Float64 in 2025 and Int32 in 2022 (fixtures), so
-- cast both sides to int. Plays are then limited to the same scrimmage scope as
-- player_game_pbp (0028). A participation play with no pbp match is dropped and counted
-- in the collector's agent_runs.meta, never guessed.
--
-- On-field sets come from offense_players / defense_players (`;`-separated gsis IDs). A
-- dropback is pbp qb_dropback == 1. Coverage labels come from defense_man_zone_type:
-- 'MAN_COVERAGE', 'ZONE_COVERAGE', or '' (unlabeled, ~51% of 2025 plays), with '' treated
-- as missing. Only labeled dropbacks enter the *_man / *_zone columns, so their sum is
-- the labeled count, and an unlabeled play is never read as either.
--
-- Storage: ~2,000 rows/season x ~17 values, ~0.5 MB/season. Exempt from L2 retention (it
-- *is* the prior). The 2026 `_hist` window needs 2023-2025 (a one-time 2023-2024
-- backfill), not the 2016-2024 range docs/phases/P7.md first costed.

CREATE TABLE participation_player_season (
    player_id text NOT NULL REFERENCES players (player_id),
    season int NOT NULL,

    -- offense: on-field (offense_players)
    off_snaps int NOT NULL,               -- on-field scrimmage plays
    off_dropbacks int NOT NULL,           -- on-field dropbacks
    off_dropbacks_man int NOT NULL,
    off_dropbacks_zone int NOT NULL,
    -- offense: as the pbp receiver (a target, same definition as 0028)
    targets_man int NOT NULL,
    targets_zone int NOT NULL,
    rec_epa_sum_man double precision NOT NULL,
    rec_epa_sum_zone double precision NOT NULL,
    -- offense: as the pbp passer (qb_dropback == 1)
    pass_dropbacks_man int NOT NULL,
    pass_dropbacks_zone int NOT NULL,
    pass_epa_sum_man double precision NOT NULL,
    pass_epa_sum_zone double precision NOT NULL,

    -- defense: on-field (defense_players)
    def_snaps int NOT NULL,
    def_dropbacks int NOT NULL,
    def_dropbacks_man int NOT NULL,
    def_dropbacks_zone int NOT NULL,

    content_hash text NOT NULL,
    updated_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (player_id, season)
);

CREATE INDEX participation_player_season_season_idx ON participation_player_season (season);

ALTER TABLE participation_player_season ENABLE ROW LEVEL SECURITY;
