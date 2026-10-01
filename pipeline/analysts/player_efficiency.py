"""
Job: Compute each player's receiving, rushing, passing and defense rate stats per game,
     over his last 4 games and season to date (prior-blended), with league percentiles
     and participation-derived multi-season priors (_hist).
Reads: player_game_pbp, snaps, pfr_advstats, ngs, player_week (def_* only),
       participation_player_season, players (position_group)
Writes: player_eff_week
Tier: T2
Phase: P7
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, NamedTuple

import polars as pl
import psycopg

from pipeline.core.base import Analyst, RunContext, WorkResult
from pipeline.core.freshness import get_last_value
from pipeline.core.player_tables import (
    as_of_percentiles,
    blend_exprs,
    check_one_game_per_week,
    column_list,
    finalize_rows,
    played_weeks,
    ratio,
    window_frame,
    windowed_sums,
    write_player_rows,
)

TABLE = "player_eff_week"
_MIGRATION = Path(__file__).resolve().parents[2] / "db" / "migrations" / "0032_player_eff_week.sql"
COLUMNS = column_list(_MIGRATION.read_text(encoding="utf-8"))

FAMILIES = ("receiving", "rushing", "passing", "defense")
# The family sample and headline metric (docs/signals.md, "Family stability").
_FAMILY_PREFIX = {"receiving": "rec", "rushing": "rush", "passing": "pass", "defense": "def"}
_FAMILY_SAMPLE = {
    "receiving": ("rec_targets", "targets"),
    "rushing": ("rush_carries", "carries"),
    "passing": ("pass_dropbacks", "dropbacks"),
    "defense": ("def_snaps", "defense_snaps"),
}
HEADLINE = {
    "receiving": "epa_per_target",
    "rushing": "epa_per_carry",
    "passing": "epa_per_dropback",
    "defense": "tackles_per_snap",
}
# Groups outside the estimate borrow the family's primary group (docs/signals.md).
PRIMARY_GROUP = {"receiving": "WR", "rushing": "RB", "passing": "QB", "defense": "DB"}

# Percentile minimum per game played, per position group (docs/signals.md, "_pct";
# decided 2026-09-29). Only QB passing (15) is derived; QB rushing (2) is a judgment call;
# the rest are labeled guesses. Keys absent here use the family default.
MIN_PER_GAME: dict[tuple[str, str], float] = {("rushing", "QB"): 2.0}
MIN_PER_GAME_DEFAULT = {"receiving": 3.0, "rushing": 6.0, "passing": 15.0, "defense": 20.0}

# Defense percentiles are gated by source (P7 step 7, decided 2026-09-30). A missing
# player_week row reads as zero (1,172 of 1,172 2025 defender-games without one had no pbp
# credit on any play), so def_pw metrics rank. PFR drops rows at the source for defenders
# who recorded stats (59.6% of 2025's missing def rows had pbp credits), so neither
# reading is right and PFR-derived defense _pct stays null. Values are written either way.
DEFENSE_PCT_GATED_SOURCES = frozenset({"def_pfr", "pfr_def"})

# _hist window: the three completed seasons before the current one (docs/phases/P7.md).
_HIST_SEASONS = 3


class Metric(NamedTuple):
    family: str
    name: str
    source: str
    num: pl.Expr
    den: pl.Expr


c = pl.col


def _metrics() -> list[Metric]:
    """The registry's formulas (docs/signals.md, "Player tables (Phase 7)"), in 0032's
    column order. scripts/estimate_player_reliability.py estimated k and r for exactly
    these numerator/denominator pairs."""
    m: list[Metric] = []

    def add(family: str, name: str, num: pl.Expr, den: pl.Expr, source: str = "pgp") -> None:
        m.append(Metric(family, name, source, num, den))

    R, U, P, D = "receiving", "rushing", "passing", "defense"
    add(R, "epa_per_target", c("rec_epa_sum"), c("targets"))
    add(R, "rec_success_rate", c("rec_success"), c("targets"))
    add(R, "catch_rate", c("receptions"), c("targets"))
    add(R, "yards_per_target", c("rec_yards"), c("targets"))
    add(R, "rec_adot", c("rec_air_yards_sum"), c("rec_air_yards_n"))
    add(R, "yac_per_reception", c("rec_yac_sum"), c("receptions"))
    add(R, "yac_oe_per_reception", c("rec_yac_oe_sum"), c("rec_yac_oe_n"))
    add(R, "rec_first_down_rate", c("rec_first_downs"), c("targets"))
    add(R, "rec_explosive_rate", c("rec_explosive"), c("targets"))
    add(R, "deep_target_rate", c("deep_targets"), c("targets"))
    for loc in ("left", "middle", "right"):
        add(R, f"epa_per_target_{loc}", c(f"rec_epa_sum_{loc}"), c(f"targets_{loc}"))
    add(R, "catchable_catch_rate", c("ftn_catchable_receptions"), c("ftn_catchable_targets"))
    add(R, "drop_rate", c("ftn_drops"), c("ftn_catchable_targets"))
    add(R, "contested_target_rate", c("ftn_contested_targets"), c("ftn_charted_targets"))
    add(R, "contested_catch_rate", c("ftn_contested_receptions"), c("ftn_contested_targets"))
    add(R, "created_reception_rate", c("ftn_created_receptions"), c("ftn_charted_receptions"))
    add(R, "screen_target_rate", c("ftn_screen_targets"), c("ftn_charted_targets"))
    add(R, "epa_per_target_play_action", c("ftn_pa_rec_epa_sum"), c("ftn_pa_targets"))
    add(
        R,
        "broken_tackles_per_reception",
        c("receiving_broken_tackles"),
        c("receptions"),
        "pgp_pfr_rec",
    )
    add(R, "avg_separation", c("avg_separation") * c("targets"), c("targets"), "ngs_rec")
    add(R, "avg_cushion", c("avg_cushion") * c("targets"), c("targets"), "ngs_rec")

    add(U, "epa_per_carry", c("rush_epa_sum"), c("carries"))
    add(U, "rush_success_rate", c("rush_success"), c("carries"))
    add(U, "yards_per_carry", c("rush_yards"), c("carries"))
    add(U, "stuff_rate", c("rush_stuffs"), c("carries"))
    add(U, "rush_explosive_rate", c("rush_explosive"), c("carries"))
    add(U, "rush_first_down_rate", c("rush_first_downs"), c("carries"))
    cells = ("le", "lt", "lg", "mid", "rg", "rt", "re")
    for cell in cells:
        add(U, f"gap_share_{cell}", c(f"carries_{cell}"), c("carries"))
    for cell in cells:
        add(U, f"epa_per_carry_{cell}", c(f"rush_epa_sum_{cell}"), c(f"carries_{cell}"))
    for cell in cells:
        add(U, f"rush_success_rate_{cell}", c(f"rush_success_{cell}"), c(f"carries_{cell}"))
    add(U, "stacked_box_rate", c("ftn_stacked_box_carries"), c("ftn_charted_carries"))
    add(U, "epa_per_carry_stacked_box", c("ftn_stacked_box_epa_sum"), c("ftn_stacked_box_carries"))
    add(
        U,
        "yards_before_contact_per_carry",
        c("rushing_yards_before_contact"),
        c("carries"),
        "pfr_rush",
    )
    add(
        U,
        "yards_after_contact_per_carry",
        c("rushing_yards_after_contact"),
        c("carries"),
        "pfr_rush",
    )
    add(U, "broken_tackles_per_carry", c("rushing_broken_tackles"), c("carries"), "pfr_rush")
    add(U, "ryoe_per_carry", c("rush_yards_over_expected"), c("rush_attempts"), "ngs_rush")
    add(
        U,
        "avg_time_to_los",
        c("avg_time_to_los") * c("rush_attempts"),
        c("rush_attempts"),
        "ngs_rush",
    )

    add(P, "epa_per_dropback", c("dropback_epa_sum"), c("dropbacks"))
    add(P, "dropback_success_rate", c("dropback_success"), c("dropbacks"))
    add(P, "cpoe", c("cpoe_sum"), c("cpoe_n"))
    add(P, "pass_adot", c("pass_air_yards_sum"), c("pass_air_yards_n"))
    add(P, "sack_rate", c("sacks"), c("dropbacks"))
    add(P, "scramble_rate", c("scrambles"), c("dropbacks"))
    add(P, "int_rate", c("interceptions"), c("pass_attempts"))
    add(P, "deep_attempt_rate", c("deep_attempts"), c("pass_attempts"))
    add(P, "play_action_rate", c("ftn_pa_dropbacks"), c("ftn_charted_dropbacks"))
    add(P, "epa_per_dropback_play_action", c("ftn_pa_epa_sum"), c("ftn_pa_dropbacks"))
    add(P, "blitzed_rate", c("ftn_blitzed_dropbacks"), c("ftn_charted_dropbacks"))
    add(P, "epa_per_dropback_vs_blitz", c("ftn_blitzed_epa_sum"), c("ftn_blitzed_dropbacks"))
    add(P, "out_of_pocket_rate", c("ftn_out_of_pocket_dropbacks"), c("ftn_charted_dropbacks"))
    add(P, "screen_rate", c("ftn_screen_attempts"), c("ftn_charted_attempts"))
    add(P, "throwaway_rate", c("ftn_throwaways"), c("ftn_charted_attempts"))
    add(
        P,
        "catchable_rate",
        c("ftn_catchable_attempts"),
        c("ftn_charted_attempts") - c("ftn_throwaways"),
    )
    add(P, "int_worthy_rate", c("ftn_int_worthy"), c("ftn_charted_attempts"))
    add(P, "qb_fault_sack_share", c("ftn_qb_fault_sacks"), c("ftn_charted_sacks"))
    add(P, "pressure_rate", c("times_pressured"), c("dropbacks"), "pgp_pfr_pass")
    add(P, "pressure_to_sack_rate", c("times_sacked"), c("times_pressured"), "pfr_pass")
    for col in ("avg_time_to_throw", "aggressiveness", "avg_air_yards_to_sticks"):
        add(P, col, c(col) * c("attempts"), c("attempts"), "ngs_pass")

    snaps_ = c("defense_snaps")
    add(D, "tackles_per_snap", c("def_tackles_combined"), snaps_, "def_pfr")
    add(
        D,
        "missed_tackle_rate",
        c("def_missed_tackles"),
        c("def_tackles_combined") + c("def_missed_tackles"),
        "pfr_def",
    )
    add(D, "tfl_per_snap", c("def_tackles_for_loss"), snaps_, "def_pw")
    add(D, "sacks_per_snap", c("def_sacks"), snaps_, "def_pw")
    add(D, "qb_hits_per_snap", c("def_qb_hits"), snaps_, "def_pw")
    add(D, "pressures_per_snap", c("def_pressures"), snaps_, "def_pfr")
    add(D, "blitzes_per_snap", c("def_times_blitzed"), snaps_, "def_pfr")
    add(D, "forced_fumbles_per_snap", c("def_fumbles_forced"), snaps_, "def_pw")
    add(D, "pass_defended_per_snap", c("def_pass_defended"), snaps_, "def_pw")
    add(D, "targets_per_snap", c("def_targets"), snaps_, "def_pfr")
    add(D, "completion_pct_allowed", c("def_completions_allowed"), c("def_targets"), "pfr_def")
    add(D, "yards_per_target_allowed", c("def_yards_allowed"), c("def_targets"), "pfr_def")
    add(
        D,
        "yac_allowed_per_completion",
        c("def_yards_after_catch"),
        c("def_completions_allowed"),
        "pfr_def",
    )
    add(D, "adot_allowed", c("def_adot") * c("def_targets"), c("def_targets"), "pfr_def")
    add(D, "td_rate_allowed", c("def_receiving_td_allowed"), c("def_targets"), "pfr_def")
    add(D, "int_rate_on_targets", c("def_ints"), c("def_targets"), "pfr_def")
    return m


METRICS = _metrics()

# k (shrinkage, in the metric's own denominator units) and r (year-over-year reliability,
# r_slope clipped to [0, 1]) per (metric, position group). Generated by
# `scripts/estimate_player_reliability.py recommend` (2026-09-29) from the same rows as
# docs/signals.md's "k and r by metric and position group" table, which says for each
# entry whether it's pinned by the data or a judgment call. A test holds the two equal.
_K_R: dict[tuple[str, str], tuple[float, float]] = {
    ("epa_per_target", "WR"): (177, 0.79),
    ("epa_per_target", "TE"): (141, 0.94),
    ("epa_per_target", "RB"): (189, 0.72),
    ("rec_success_rate", "WR"): (100, 0.79),
    ("rec_success_rate", "TE"): (130, 0.90),
    ("rec_success_rate", "RB"): (126, 0.58),
    ("catch_rate", "WR"): (64, 0.81),
    ("catch_rate", "TE"): (128, 0.76),
    ("catch_rate", "RB"): (99, 0.50),
    ("yards_per_target", "WR"): (154, 0.77),
    ("yards_per_target", "TE"): (118, 1.00),
    ("yards_per_target", "RB"): (150, 0.71),
    ("rec_adot", "WR"): (19, 0.87),
    ("rec_adot", "TE"): (22, 0.81),
    ("rec_adot", "RB"): (18, 0.79),
    ("yac_per_reception", "WR"): (53, 0.91),
    ("yac_per_reception", "TE"): (48, 0.91),
    ("yac_per_reception", "RB"): (80, 0.77),
    ("yac_oe_per_reception", "WR"): (111, 0.99),
    ("yac_oe_per_reception", "TE"): (71, 0.96),
    ("yac_oe_per_reception", "RB"): (274, 1.00),
    ("rec_first_down_rate", "WR"): (113, 0.72),
    ("rec_first_down_rate", "TE"): (99, 0.87),
    ("rec_first_down_rate", "RB"): (130, 0.93),
    ("rec_explosive_rate", "WR"): (185, 0.69),
    ("rec_explosive_rate", "TE"): (142, 1.00),
    ("rec_explosive_rate", "RB"): (402, 0.00),
    ("deep_target_rate", "WR"): (35, 0.85),
    ("deep_target_rate", "TE"): (58, 0.67),
    ("deep_target_rate", "RB"): (48, 0.71),
    ("epa_per_target_left", "WR"): (177, 0.57),
    ("epa_per_target_left", "TE"): (141, 0.98),
    ("epa_per_target_left", "RB"): (189, 0.00),
    ("epa_per_target_middle", "WR"): (177, 0.50),
    ("epa_per_target_middle", "TE"): (141, 0.00),
    ("epa_per_target_middle", "RB"): (189, 0.00),
    ("epa_per_target_right", "WR"): (177, 1.00),
    ("epa_per_target_right", "TE"): (141, 0.00),
    ("epa_per_target_right", "RB"): (189, 0.00),
    ("catchable_catch_rate", "WR"): (64, 0.84),
    ("catchable_catch_rate", "TE"): (128, 0.00),
    ("catchable_catch_rate", "RB"): (99, 0.40),
    ("drop_rate", "WR"): (64, 0.78),
    ("drop_rate", "TE"): (128, 0.00),
    ("drop_rate", "RB"): (99, 0.44),
    ("contested_target_rate", "WR"): (82, 0.73),
    ("contested_target_rate", "TE"): (120, 0.83),
    ("contested_target_rate", "RB"): (468, 0.00),
    ("contested_catch_rate", "WR"): (64, 0.00),
    ("contested_catch_rate", "TE"): (128, 0.00),
    ("contested_catch_rate", "RB"): (99, 0.00),
    ("created_reception_rate", "WR"): (74, 0.78),
    ("created_reception_rate", "TE"): (13871, 0.00),
    ("created_reception_rate", "RB"): (13114, 0.00),
    ("screen_target_rate", "WR"): (19, 0.76),
    ("screen_target_rate", "TE"): (39, 0.93),
    ("screen_target_rate", "RB"): (31, 0.54),
    ("epa_per_target_play_action", "WR"): (177, 0.00),
    ("epa_per_target_play_action", "TE"): (141, 0.00),
    ("epa_per_target_play_action", "RB"): (189, 0.00),
    ("broken_tackles_per_reception", "WR"): (169, 0.00),
    ("broken_tackles_per_reception", "TE"): (351, 0.00),
    ("broken_tackles_per_reception", "RB"): (169, 0.00),
    ("avg_separation", "WR"): (35, 0.87),
    ("avg_separation", "TE"): (67, 0.93),
    ("avg_separation", "RB"): (35, 0.00),
    ("avg_cushion", "WR"): (50, 0.70),
    ("avg_cushion", "TE"): (164, 0.76),
    ("avg_cushion", "RB"): (50, 0.00),
    ("epa_per_carry", "RB"): (215, 0.51),
    ("epa_per_carry", "QB"): (23, 0.67),
    ("rush_success_rate", "RB"): (196, 0.57),
    ("rush_success_rate", "QB"): (22, 0.74),
    ("yards_per_carry", "RB"): (241, 0.63),
    ("yards_per_carry", "QB"): (13, 0.95),
    ("stuff_rate", "RB"): (251, 0.57),
    ("stuff_rate", "QB"): (10, 0.78),
    ("rush_explosive_rate", "RB"): (258, 0.70),
    ("rush_explosive_rate", "QB"): (24, 1.00),
    ("rush_first_down_rate", "RB"): (116, 0.61),
    ("rush_first_down_rate", "QB"): (19, 0.83),
    ("gap_share_le", "RB"): (56, 0.74),
    ("gap_share_le", "QB"): (16, 1.00),
    ("epa_per_carry_le", "RB"): (215, 0.57),
    ("epa_per_carry_le", "QB"): (23, 0.00),
    ("rush_success_rate_le", "RB"): (196, 0.00),
    ("rush_success_rate_le", "QB"): (22, 0.00),
    ("gap_share_lt", "RB"): (111, 0.60),
    ("gap_share_lt", "QB"): (43, 0.87),
    ("epa_per_carry_lt", "RB"): (215, 0.93),
    ("epa_per_carry_lt", "QB"): (23, 0.00),
    ("rush_success_rate_lt", "RB"): (196, 0.00),
    ("rush_success_rate_lt", "QB"): (22, 0.00),
    ("gap_share_lg", "RB"): (99, 0.73),
    ("gap_share_lg", "QB"): (57, 0.00),
    ("epa_per_carry_lg", "RB"): (215, 0.00),
    ("epa_per_carry_lg", "QB"): (23, 0.00),
    ("rush_success_rate_lg", "RB"): (196, 0.54),
    ("rush_success_rate_lg", "QB"): (22, 0.00),
    ("gap_share_mid", "RB"): (41, 0.80),
    ("gap_share_mid", "QB"): (9.2, 0.96),
    ("epa_per_carry_mid", "RB"): (215, 0.73),
    ("epa_per_carry_mid", "QB"): (23, 0.54),
    ("rush_success_rate_mid", "RB"): (132, 0.60),
    ("rush_success_rate_mid", "QB"): (11, 0.75),
    ("gap_share_rg", "RB"): (76, 0.80),
    ("gap_share_rg", "QB"): (43, 0.69),
    ("epa_per_carry_rg", "RB"): (215, 0.29),
    ("epa_per_carry_rg", "QB"): (23, 0.00),
    ("rush_success_rate_rg", "RB"): (196, 0.00),
    ("rush_success_rate_rg", "QB"): (22, 0.00),
    ("gap_share_rt", "RB"): (110, 0.63),
    ("gap_share_rt", "QB"): (150, 0.00),
    ("epa_per_carry_rt", "RB"): (215, 0.00),
    ("epa_per_carry_rt", "QB"): (23, 0.00),
    ("rush_success_rate_rt", "RB"): (196, 0.00),
    ("rush_success_rate_rt", "QB"): (22, 0.00),
    ("gap_share_re", "RB"): (61, 0.74),
    ("gap_share_re", "QB"): (14, 0.97),
    ("epa_per_carry_re", "RB"): (215, 0.00),
    ("epa_per_carry_re", "QB"): (23, 0.00),
    ("rush_success_rate_re", "RB"): (196, 0.00),
    ("rush_success_rate_re", "QB"): (22, 0.59),
    ("stacked_box_rate", "RB"): (113, 0.41),
    ("stacked_box_rate", "QB"): (16, 0.62),
    ("epa_per_carry_stacked_box", "RB"): (215, 0.00),
    ("epa_per_carry_stacked_box", "QB"): (23, 0.00),
    ("yards_before_contact_per_carry", "RB"): (229, 0.67),
    ("yards_before_contact_per_carry", "QB"): (26, 0.92),
    ("yards_after_contact_per_carry", "RB"): (265, 0.79),
    ("yards_after_contact_per_carry", "QB"): (30, 0.80),
    ("broken_tackles_per_carry", "RB"): (613, 0.87),
    ("broken_tackles_per_carry", "QB"): (175, 0.00),
    ("ryoe_per_carry", "RB"): (661, 0.00),
    ("avg_time_to_los", "RB"): (45, 0.62),
    ("epa_per_dropback", "QB"): (199, 0.81),
    ("dropback_success_rate", "QB"): (178, 0.81),
    ("cpoe", "QB"): (233, 0.91),
    ("pass_adot", "QB"): (213, 0.66),
    ("sack_rate", "QB"): (189, 0.72),
    ("scramble_rate", "QB"): (67, 0.94),
    ("int_rate", "QB"): (757, 0.54),
    ("deep_attempt_rate", "QB"): (360, 0.63),
    ("play_action_rate", "QB"): (155, 0.35),
    ("epa_per_dropback_play_action", "QB"): (199, 1.00),
    ("blitzed_rate", "QB"): (1347, 0.00),
    ("epa_per_dropback_vs_blitz", "QB"): (199, 0.56),
    ("out_of_pocket_rate", "QB"): (73, 0.95),
    ("screen_rate", "QB"): (221, 0.38),
    ("throwaway_rate", "QB"): (283, 1.00),
    ("catchable_rate", "QB"): (475, 0.88),
    ("int_worthy_rate", "QB"): (531, 0.64),
    ("qb_fault_sack_share", "QB"): (215, 0.00),
    ("pressure_rate", "QB"): (246, 0.73),
    ("pressure_to_sack_rate", "QB"): (90, 0.69),
    ("avg_time_to_throw", "QB"): (81, 0.72),
    ("aggressiveness", "QB"): (387, 0.77),
    ("avg_air_yards_to_sticks", "QB"): (258, 0.64),
    ("tackles_per_snap", "DL"): (378, 0.83),
    ("tackles_per_snap", "LB"): (93, 0.95),
    ("tackles_per_snap", "DB"): (265, 0.86),
    ("pressures_per_snap", "DL"): (397, 0.92),
    ("pressures_per_snap", "LB"): (84, 0.96),
    ("pressures_per_snap", "DB"): (603, 0.87),
    ("blitzes_per_snap", "DL"): (80, 0.79),
    ("blitzes_per_snap", "LB"): (153, 0.65),
    ("blitzes_per_snap", "DB"): (92, 0.64),
    ("targets_per_snap", "DL"): (303, 0.67),
    ("targets_per_snap", "LB"): (76, 0.96),
    ("targets_per_snap", "DB"): (143, 0.87),
    # player_week numerators: re-estimated 2026-09-30 under the zero reading (P7 step 7).
    ("tfl_per_snap", "DL"): (827, 0.92),
    ("tfl_per_snap", "LB"): (859, 1.00),
    ("tfl_per_snap", "DB"): (1504, 0.91),
    ("sacks_per_snap", "DL"): (595, 0.96),
    ("sacks_per_snap", "LB"): (347, 1.00),
    ("sacks_per_snap", "DB"): (10343, 0.00),
    ("qb_hits_per_snap", "DL"): (237, 0.92),
    ("qb_hits_per_snap", "LB"): (163, 0.99),
    ("qb_hits_per_snap", "DB"): (1287, 1.00),
    ("forced_fumbles_per_snap", "DL"): (5262, 0.00),
    ("forced_fumbles_per_snap", "LB"): (5262, 0.00),
    ("forced_fumbles_per_snap", "DB"): (5262, 0.00),
    ("pass_defended_per_snap", "DL"): (840, 0.77),
    ("pass_defended_per_snap", "LB"): (1980, 1.00),
    ("pass_defended_per_snap", "DB"): (1017, 0.94),
    ("missed_tackle_rate", "DL"): (51, 0.56),
    ("missed_tackle_rate", "LB"): (141, 0.72),
    ("missed_tackle_rate", "DB"): (158, 0.91),
    ("completion_pct_allowed", "DL"): (204, 0.00),
    ("completion_pct_allowed", "LB"): (204, 0.42),
    ("completion_pct_allowed", "DB"): (112, 0.70),
    ("yards_per_target_allowed", "DL"): (10, 0.52),
    ("yards_per_target_allowed", "LB"): (283, 0.00),
    ("yards_per_target_allowed", "DB"): (104, 0.73),
    ("yac_allowed_per_completion", "DL"): (5.4, 0.34),
    ("yac_allowed_per_completion", "LB"): (275, 0.00),
    ("yac_allowed_per_completion", "DB"): (163, 1.00),
    ("adot_allowed", "DL"): (103, 0.00),
    ("adot_allowed", "LB"): (44, 0.67),
    ("adot_allowed", "DB"): (31, 0.64),
    ("td_rate_allowed", "DL"): (5, 0.00),
    ("td_rate_allowed", "LB"): (208, 0.00),
    ("td_rate_allowed", "DB"): (171, 0.62),
    ("int_rate_on_targets", "DL"): (1409, 0.00),
    ("int_rate_on_targets", "LB"): (1409, 0.00),
    ("int_rate_on_targets", "DB"): (1409, 0.00),
}


def k_r(metric: Metric, group: str | None) -> tuple[float, float]:
    """The (k, r) for this metric and position group. A group the estimate didn't cover
    borrows the family's primary group (docs/signals.md, "Prior blend")."""
    key = (metric.name, group or "")
    if key in _K_R:
        return _K_R[key]
    return _K_R[(metric.name, PRIMARY_GROUP[metric.family])]


def min_per_game(family: str, group: str | None) -> float:
    return MIN_PER_GAME.get((family, group or ""), MIN_PER_GAME_DEFAULT[family])


# --------------------------------------------------------------------------------------
# Inputs
# --------------------------------------------------------------------------------------


class EffInputs(NamedTuple):
    """Staged rows for the current season (weeks <= the run's week) and the prior season,
    all REG/POST, keyed player_id/season/week. ngs excludes its week-0 season rows."""

    pgp: pl.DataFrame
    snaps: pl.DataFrame
    pfr: pl.DataFrame
    ngs: pl.DataFrame
    player_week: pl.DataFrame
    participation: pl.DataFrame
    positions: pl.DataFrame


def _sources(inp: EffInputs) -> dict[str, pl.DataFrame]:
    """One frame per metric source, each at most one row per player per week."""
    key = ["player_id", "season", "week"]
    pfr = {t: inp.pfr.filter(c("stat_type") == t) for t in ("pass", "rush", "rec", "def")}
    for t, df in pfr.items():
        check_one_game_per_week(df, f"pfr_advstats {t}")
    check_one_game_per_week(inp.pgp, "player_game_pbp")
    snaps_def = inp.snaps.filter(c("defense_snaps") > 0).select(*key, "defense_snaps")
    out = {
        "pgp": inp.pgp,
        "pgp_pfr_rec": inp.pgp.join(pfr["rec"].select(*key, "receiving_broken_tackles"), on=key),
        "pgp_pfr_pass": inp.pgp.join(pfr["pass"].select(*key, "times_pressured"), on=key),
        "pfr_rush": pfr["rush"],
        "pfr_pass": pfr["pass"],
        "pfr_def": pfr["def"],
        "ngs_rec": inp.ngs.filter(c("stat_type") == "receiving"),
        "ngs_rush": inp.ngs.filter(c("stat_type") == "rushing"),
        "ngs_pass": inp.ngs.filter(c("stat_type") == "passing"),
        # PFR: only games with a PFR def row. That's a known upward bias on every per-snap
        # rate, and why these metrics' _pct stays gated (DEFENSE_PCT_GATED_SOURCES).
        "def_pfr": snaps_def.join(pfr["def"].drop("defense_snaps", strict=False), on=key),
        # player_week: every game with defense snaps, a missing row read as zero.
        "def_pw": snaps_def.join(inp.player_week, on=key, how="left").with_columns(
            c(col).fill_null(0) for col in _PW_DEF_COLS
        ),
    }
    for name in ("ngs_rec", "ngs_rush", "ngs_pass"):
        check_one_game_per_week(out[name], name)
    return out


def _metric_values(sources: dict[str, pl.DataFrame]) -> pl.DataFrame:
    """(player_id, season, week) with `<metric>__n`/`__d` for every metric: that game's
    numerator and denominator, both null unless the denominator is positive and the
    numerator present (a zero denominator contributes to no window)."""
    frames = []
    for src, df in sources.items():
        ms = [m for m in METRICS if m.source == src]
        if not ms:
            continue
        exprs = []
        for m in ms:
            valid = m.den.is_not_null() & (m.den > 0) & m.num.is_not_null()
            exprs += [
                pl.when(valid).then(m.num.cast(pl.Float64)).otherwise(None).alias(f"{m.name}__n"),
                pl.when(valid).then(m.den.cast(pl.Float64)).otherwise(None).alias(f"{m.name}__d"),
            ]
        frames.append(df.select("player_id", "season", "week", *exprs))
    out = frames[0]
    for f in frames[1:]:
        out = out.join(f, on=["player_id", "season", "week"], how="full", coalesce=True)
    return out


# --------------------------------------------------------------------------------------
# Pure computation
# --------------------------------------------------------------------------------------


def build_eff_rows(inp: EffInputs, season: int) -> pl.DataFrame:
    """Every player_eff_week row for `season` (weeks as passed in), one per player per
    week with a nonzero sample in at least one family. Each row reads only games up to
    its own week (windowed_sums), plus the whole prior season for its prior."""

    def this(df: pl.DataFrame) -> pl.DataFrame:
        return df.filter(c("season") == season)

    def last(df: pl.DataFrame) -> pl.DataFrame:
        # The prior is last season's regular season, like Efficiency's prior solve.
        return df.filter((c("season") == season - 1) & (c("season_type") == "REG"))

    cur = inp._replace(
        pgp=this(inp.pgp),
        snaps=this(inp.snaps),
        pfr=this(inp.pfr),
        ngs=this(inp.ngs),
        player_week=this(inp.player_week),
    )
    prior = inp._replace(
        pgp=last(inp.pgp),
        snaps=last(inp.snaps),
        pfr=last(inp.pfr),
        ngs=last(inp.ngs),
        player_week=last(inp.player_week),
    )

    values = _metric_values(_sources(cur))
    samples = cur.pgp.select("player_id", "week", "targets", "carries", "dropbacks").join(
        cur.snaps.filter(c("defense_snaps") > 0).select("player_id", "week", "defense_snaps"),
        on=["player_id", "week"],
        how="full",
        coalesce=True,
    )
    played = played_weeks(
        cur.snaps.filter((c("offense_snaps") + c("defense_snaps") + c("st_snaps")) > 0),
        cur.pgp,
    )
    windows = window_frame(played)
    row_keys = (
        samples.filter(
            (c("targets").fill_null(0) > 0)
            | (c("carries").fill_null(0) > 0)
            | (c("dropbacks").fill_null(0) > 0)
            | (c("defense_snaps").fill_null(0) > 0)
        )
        .select("player_id", "week")
        .unique()
    )
    windows = windows.join(row_keys, on=["player_id", "week"])

    value_cols = [col for m in METRICS for col in (f"{m.name}__n", f"{m.name}__d")]
    sums = windowed_sums(values, windows, value_cols)
    fam_sums = windowed_sums(samples, windows, ["targets", "carries", "dropbacks", "defense_snaps"])

    rows = (
        windows.join(_identity(cur), on=["player_id", "week"], how="left")
        .join(cur.positions, on="player_id", how="left")
        .join(sums, on=["player_id", "week"])
        .join(fam_sums, on=["player_id", "week"])
        .with_columns(pl.lit(season).alias("season"))
    )
    rows = _attach_league(rows, values, cur.positions)
    rows = _attach_prior(rows, prior, cur.positions)

    for m in METRICS:
        k_expr, r_expr = _per_group(m)
        rows = rows.with_columns(
            ratio(c(f"{m.name}__n_std"), c(f"{m.name}__d_std")).alias(f"{m.name}__cur"),
            ratio(c(f"{m.name}__n_game"), c(f"{m.name}__d_game")).alias(f"{m.name}_game"),
            ratio(c(f"{m.name}__n_l4"), c(f"{m.name}__d_l4")).alias(f"{m.name}_l4"),
            k_expr.alias(f"{m.name}__k"),
        ).with_columns(
            _prior_value(m).alias(f"{m.name}__prior"),
        )
        value, w_cur, w_prior = blend_exprs(
            cur=f"{m.name}__cur",
            n=f"{m.name}__d_std",
            prior=f"{m.name}__prior",
            league=f"{m.name}__league",
            k=c(f"{m.name}__k"),
            r=r_expr,
        )
        rows = rows.with_columns(
            value.alias(f"{m.name}_std"),
            w_cur.alias(f"{m.name}__wcur"),
            w_prior.alias(f"{m.name}__wprior"),
        )

    rows = _attach_family_columns(rows)
    rows = _attach_percentiles(rows)
    rows = _attach_hist(rows, inp.participation, season)
    return rows.sort("player_id", "week")


def _identity(cur: EffInputs) -> pl.DataFrame:
    """game_id/team/season_type for each player-week: the snaps row, or the pgp row where
    snaps has none."""
    ident = ["player_id", "week", "game_id", "team", "season_type"]
    snaps = cur.snaps.select(ident)
    pgp = cur.pgp.select(ident).join(
        snaps.select("player_id", "week"), on=["player_id", "week"], how="anti"
    )
    return pl.concat([snaps, pgp], how="vertical").unique(["player_id", "week"])


def _attach_league(
    rows: pl.DataFrame, values: pl.DataFrame, positions: pl.DataFrame
) -> pl.DataFrame:
    """`<m>__league`: the season-to-date Σ/Σ over the player's position_group, through the
    row's own week."""
    cols = [col for m in METRICS for col in (f"{m.name}__n", f"{m.name}__d")]
    by_group = (
        values.join(positions, on="player_id", how="inner")
        .group_by("position_group", "week")
        .agg(c(col).fill_null(0).sum() for col in cols)
        .sort("position_group", "week")
        .with_columns(c(col).cum_sum().over("position_group") for col in cols)
        .with_columns(
            ratio(c(f"{m.name}__n"), c(f"{m.name}__d")).alias(f"{m.name}__league") for m in METRICS
        )
        .select("position_group", "week", *[f"{m.name}__league" for m in METRICS])
        .rename({"week": "_lweek"})
        .sort("_lweek")
    )
    rows_i = rows.with_row_index("_i").sort("week")
    joined = rows_i.join_asof(
        by_group,
        left_on="week",
        right_on="_lweek",
        by="position_group",
        strategy="backward",
        check_sortedness=False,  # both sides are sorted on the join key just above
    )
    return joined.sort("_i").drop("_i", "_lweek")


def _attach_prior(rows: pl.DataFrame, prior: EffInputs, positions: pl.DataFrame) -> pl.DataFrame:
    """Prior-season totals per player (`<m>__pn`, `<m>__pd`, wherever he played) and the
    prior season's league value for his current group (`<m>__pleague`)."""
    if prior.pgp.height == 0 and prior.snaps.height == 0:
        return rows.with_columns(
            pl.lit(None, dtype=pl.Float64).alias(f"{m.name}__{s}")
            for m in METRICS
            for s in ("pn", "pd", "pleague")
        )
    pv = _metric_values(_sources(prior))
    cols = [col for m in METRICS for col in (f"{m.name}__n", f"{m.name}__d")]
    per_player = pv.group_by("player_id").agg(c(col).sum() for col in cols)
    league = (
        pv.join(positions, on="player_id", how="inner")
        .group_by("position_group")
        .agg(c(col).fill_null(0).sum() for col in cols)
        .select(
            "position_group",
            *[
                ratio(c(f"{m.name}__n"), c(f"{m.name}__d")).alias(f"{m.name}__pleague")
                for m in METRICS
            ],
        )
    )
    per_player = per_player.rename(
        {f"{m.name}__{s}": f"{m.name}__p{s}" for m in METRICS for s in ("n", "d")}
    )
    return rows.join(per_player, on="player_id", how="left").join(
        league, on="position_group", how="left"
    )


def _per_group(m: Metric) -> tuple[pl.Expr, pl.Expr]:
    groups = {g for (name, g) in _K_R if name == m.name}
    fallback = _K_R[(m.name, PRIMARY_GROUP[m.family])]
    k_map = {g: float(_K_R[(m.name, g)][0]) for g in groups}
    r_map = {g: float(_K_R[(m.name, g)][1]) for g in groups}
    g = c("position_group").fill_null("")
    return (
        g.replace_strict(k_map, default=float(fallback[0]), return_dtype=pl.Float64),
        g.replace_strict(r_map, default=float(fallback[1]), return_dtype=pl.Float64),
    )


def _prior_value(m: Metric) -> pl.Expr:
    """Last season's ratio shrunk with the same k toward last season's group league value
    (docs/signals.md: the prior is shrunk, matching Efficiency). Null without a prior
    sample -- then w_prior = 0 and the weight goes to the league."""
    pn, pd, pl_ = c(f"{m.name}__pn"), c(f"{m.name}__pd"), c(f"{m.name}__pleague")
    k = c(f"{m.name}__k")
    return (
        pl.when(pd.is_not_null() & (pd > 0) & pn.is_not_null() & pl_.is_not_null())
        .then((pn + k * pl_) / (pd + k))
        .otherwise(None)
    )


def _attach_family_columns(rows: pl.DataFrame) -> pl.DataFrame:
    """Family sample counts (null, never 0, when the player never held the role in that
    window) and family stability: w_cur + w_prior * n_prior/(n_prior + k) of the headline
    -- the share of its _std that isn't league average (docs/signals.md)."""
    exprs = []
    for fam, (prefix, col) in _FAMILY_SAMPLE.items():
        for w, dtype in (("std", pl.Int32), ("game", pl.Int16), ("l4", pl.Int16)):
            v = c(f"{col}_{w}")
            exprs.append(pl.when(v > 0).then(v).otherwise(None).cast(dtype).alias(f"{prefix}_{w}"))
        h = HEADLINE[fam]
        pd, k = c(f"{h}__pd").fill_null(0), c(f"{h}__k")
        exprs.append(
            (c(f"{h}__wcur") + c(f"{h}__wprior") * pd / (pd + k)).alias(
                f"{_FAMILY_PREFIX[fam]}_stability"
            )
        )
    return rows.with_columns(exprs)


def _attach_percentiles(rows: pl.DataFrame) -> pl.DataFrame:
    """`<m>_pct` among players meeting their position group's family minimum per game
    played. A metric from a source in DEFENSE_PCT_GATED_SOURCES stays null."""
    specs = []
    elig_exprs = []
    for fam, (prefix, _) in _FAMILY_SAMPLE.items():
        minimum = c("position_group").replace_strict(
            {g: v for (f, g), v in MIN_PER_GAME.items() if f == fam},
            default=MIN_PER_GAME_DEFAULT[fam],
            return_dtype=pl.Float64,
        )
        elig_exprs.append(
            (c(f"{prefix}_std").fill_null(0) >= minimum * c("games_std")).alias(f"_elig_{fam}")
        )
        specs += [
            (f"{m.name}_std", f"_elig_{fam}", f"{m.name}_pct")
            for m in METRICS
            if m.family == fam and m.source not in DEFENSE_PCT_GATED_SOURCES
        ]
    rows = as_of_percentiles(rows.with_columns(elig_exprs), specs)
    missing = [f"{m.name}_pct" for m in METRICS if f"{m.name}_pct" not in rows.columns]
    return rows.with_columns(pl.lit(None, dtype=pl.Int16).alias(p) for p in missing)


def _attach_hist(rows: pl.DataFrame, participation: pl.DataFrame, season: int) -> pl.DataFrame:
    """Participation `_hist` columns: sums over the up-to-three completed seasons before
    `season` that participation_player_season holds. hist_span names them. Constant
    within a season, never blended, never current-season behavior."""
    part = participation.filter(c("season").is_between(season - _HIST_SEASONS, season - 1))
    hist_cols = [
        "hist_span",
        "rec_hist_n",
        "epa_per_target_vs_man_hist",
        "epa_per_target_vs_zone_hist",
        "target_rate_vs_man_hist",
        "target_rate_vs_zone_hist",
        "pass_hist_n",
        "epa_per_dropback_vs_man_hist",
        "epa_per_dropback_vs_zone_hist",
    ]
    if part.height == 0:
        return rows.with_columns(pl.lit(None).alias(h) for h in hist_cols)
    seasons = sorted(part["season"].unique().to_list())
    first, last = seasons[0], seasons[-1]
    span = str(first) if first == last else f"{first}-{last}"
    s = part.group_by("player_id").agg(
        c(col).sum() for col in part.columns if col not in ("player_id", "season")
    )
    rec_n = c("off_dropbacks_man") + c("off_dropbacks_zone")
    pass_n = c("pass_dropbacks_man") + c("pass_dropbacks_zone")
    h = s.select(
        "player_id",
        pl.lit(span).alias("hist_span"),
        pl.when(rec_n > 0).then(rec_n).otherwise(None).cast(pl.Int32).alias("rec_hist_n"),
        ratio(c("rec_epa_sum_man"), c("targets_man")).alias("epa_per_target_vs_man_hist"),
        ratio(c("rec_epa_sum_zone"), c("targets_zone")).alias("epa_per_target_vs_zone_hist"),
        ratio(c("targets_man"), c("off_dropbacks_man")).alias("target_rate_vs_man_hist"),
        ratio(c("targets_zone"), c("off_dropbacks_zone")).alias("target_rate_vs_zone_hist"),
        pl.when(pass_n > 0).then(pass_n).otherwise(None).cast(pl.Int32).alias("pass_hist_n"),
        ratio(c("pass_epa_sum_man"), c("pass_dropbacks_man")).alias("epa_per_dropback_vs_man_hist"),
        ratio(c("pass_epa_sum_zone"), c("pass_dropbacks_zone")).alias(
            "epa_per_dropback_vs_zone_hist"
        ),
    )
    return rows.join(h, on="player_id", how="left")


# --------------------------------------------------------------------------------------
# I/O
# --------------------------------------------------------------------------------------

_PGP_COLS = sorted(
    {
        "player_id",
        "season",
        "week",
        "season_type",
        "game_id",
        "team",
        "targets",
        "carries",
        "dropbacks",
        *[
            n
            for m in METRICS
            if m.source.startswith("pgp")
            for n in m.num.meta.root_names() + m.den.meta.root_names()
            if n not in ("receiving_broken_tackles", "times_pressured")
        ],
    }
)
_PFR_COLS = [
    "receiving_broken_tackles",
    "times_pressured",
    "times_sacked",
    "carries",
    "rushing_yards_before_contact",
    "rushing_yards_after_contact",
    "rushing_broken_tackles",
    "def_tackles_combined",
    "def_missed_tackles",
    "def_pressures",
    "def_times_blitzed",
    "def_targets",
    "def_completions_allowed",
    "def_yards_allowed",
    "def_yards_after_catch",
    "def_adot",
    "def_receiving_td_allowed",
    "def_ints",
]
_NGS_COLS = [
    "avg_separation",
    "avg_cushion",
    "targets",
    "rush_yards_over_expected",
    "rush_attempts",
    "avg_time_to_los",
    "avg_time_to_throw",
    "aggressiveness",
    "avg_air_yards_to_sticks",
    "attempts",
]
_PW_DEF_COLS = [
    "def_tackles_for_loss",
    "def_sacks",
    "def_qb_hits",
    "def_fumbles_forced",
    "def_pass_defended",
]
_PART_COLS = [
    "off_dropbacks_man",
    "off_dropbacks_zone",
    "targets_man",
    "targets_zone",
    "rec_epa_sum_man",
    "rec_epa_sum_zone",
    "pass_dropbacks_man",
    "pass_dropbacks_zone",
    "pass_epa_sum_man",
    "pass_epa_sum_zone",
]
_INPUTS_VERSION_KEYS = (
    "nflverse:player_game_pbp",
    "nflverse:snap_counts",
    "nflverse:pfr_advstats",
    "nflverse:nextgen_stats",
    "nflverse:stats_player",
    "nflverse:participation_player_season",
)


def _query(conn: psycopg.Connection, sql: str, params: tuple[Any, ...]) -> pl.DataFrame:
    with conn.cursor() as cur:
        cur.execute(sql, params)
        names = [d.name for d in cur.description or []]
        rows = cur.fetchall()
    if not rows:
        return pl.DataFrame(schema={n: pl.Utf8 if n in _TEXT else pl.Float64 for n in names})
    return pl.DataFrame(rows, schema=names, orient="row", infer_schema_length=None)


_TEXT = {"player_id", "game_id", "team", "season_type", "stat_type", "position_group"}


def _fetch(conn: psycopg.Connection, season: int, through_week: int) -> EffInputs:
    window = "((season = %s AND week <= %s) OR season = %s) AND season_type IN ('REG','POST')"
    params = (season, through_week, season - 1)
    pgp = _query(conn, f"SELECT {', '.join(_PGP_COLS)} FROM player_game_pbp WHERE {window}", params)
    snaps = _query(
        conn,
        "SELECT player_id, game_id, season, week, season_type, team, "
        "offense_snaps, defense_snaps, st_snaps FROM snaps "
        f"WHERE player_id IS NOT NULL AND {window}",
        params,
    )
    pfr = _query(
        conn,
        "SELECT player_id, season, week, season_type, stat_type, "
        f"{', '.join(_PFR_COLS)} FROM pfr_advstats "
        f"WHERE player_id IS NOT NULL AND {window}",
        params,
    )
    ngs = _query(
        conn,
        "SELECT player_id, season, week, season_type, stat_type, "
        f"{', '.join(_NGS_COLS)} FROM ngs WHERE week > 0 AND {window}",
        params,
    )
    pw = _query(
        conn,
        "SELECT player_id, season, week, season_type, "
        f"{', '.join(_PW_DEF_COLS)} FROM player_week WHERE {window}",
        params,
    )
    part = _query(
        conn,
        f"SELECT player_id, season, {', '.join(_PART_COLS)} "
        "FROM participation_player_season WHERE season BETWEEN %s AND %s",
        (season - _HIST_SEASONS, season - 1),
    )
    ids = sorted(set(pgp["player_id"].to_list()) | set(snaps["player_id"].to_list()))
    positions = _query(
        conn, "SELECT player_id, position_group FROM players WHERE player_id = ANY(%s)", (ids,)
    )
    return EffInputs(pgp, snaps, pfr, ngs, pw, part, positions)


def _inputs_version(conn: psycopg.Connection) -> str:
    return ",".join(
        f"{k.split(':', 1)[1]}@{get_last_value(conn, k) or 'unknown'}" for k in _INPUTS_VERSION_KEYS
    )


class PlayerEfficiencyAnalyst(Analyst):
    name = "player_efficiency"
    # Writes player_eff_week, not signals (CLAUDE.md layer rules): no signal names, and a
    # sector of its own so the dispatcher's distinct-sector invariant holds.
    sector = "player_efficiency"
    signal_names: frozenset[str] = frozenset()

    _rows: list[dict[str, Any]]
    _meta: dict[str, Any]

    def inputs_ready(self, ctx: RunContext) -> bool | str:
        with ctx.conn.cursor() as cur:
            cur.execute(
                "SELECT count(*) FROM player_game_pbp WHERE season = %s AND week <= %s",
                (ctx.season, ctx.week),
            )
            row = cur.fetchone()
        return bool(row and row[0])

    def compute(self, ctx: RunContext) -> pl.DataFrame:
        inp = _fetch(ctx.conn, ctx.season, ctx.week)
        df = build_eff_rows(inp, ctx.season)
        known = c("player_id").is_in(inp.positions["player_id"].implode())
        unknown = df.filter(~known).height
        df = df.filter(known & c("game_id").is_not_null())
        self._rows = finalize_rows(
            df,
            COLUMNS,
            {"as_of": ctx.now, "inputs_version": _inputs_version(ctx.conn), "updated_at": ctx.now},
        )
        self._meta = {
            "weeks": sorted(df["week"].unique().to_list()),
            "rows": len(self._rows),
            "skipped_not_in_players": unknown,
            "defense_pct_gated_sources": sorted(DEFENSE_PCT_GATED_SOURCES),
            "hist_span": df["hist_span"].drop_nulls().first() if df.height else None,
        }
        return df

    def _delete_stale_signals(self, ctx: RunContext) -> int:
        """No signals rows to clean: stale player_eff_week rows are removed in
        write_signals (docs/signals.md, "Which weeks a run writes")."""
        return 0

    def write_signals(self, ctx: RunContext, df: pl.DataFrame) -> WorkResult:
        deleted, upserted = write_player_rows(ctx.conn, TABLE, ctx.season, ctx.week, self._rows)
        return WorkResult(
            upserted.rows_changed,
            {**self._meta, "stale_rows_deleted": deleted, "upserts": {TABLE: upserted.meta()}},
        )
