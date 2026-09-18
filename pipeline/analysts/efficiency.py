"""
Job: Compute opponent-adjusted, prior-season-blended offense/defense efficiency signals
     per team per week (EPA/play, success rate, explosive rate, points/drive,
     three-and-out rate, red-zone TD rate; overall/pass/rush/down splits wherever
     team_week has the columns to support them). Point-in-time: only ever uses
     current-season rows strictly before the requested week, so the same code backfills
     a past week without leaking later weeks into it.
Reads: team_week, player_week (QB rows only), snaps (O-line rows only), depth (live
       current-week runs only -- see _depth_fallback_allowed)
Writes: signals
Tier: T2
Phase: P2
"""

from __future__ import annotations

import logging
from typing import Any, NamedTuple

import nflreadpy as nfl
import polars as pl
import psycopg

from pipeline.core.base import Analyst, RunContext, WorkResult
from pipeline.core.db import upsert_rows
from pipeline.core.freshness import get_last_value

_log = logging.getLogger(__name__)

# Judgment-call constants (documented, pinned so behavior is deterministic and testable
# -- see docs/signals.md). All flagged tunable once Phase 5's grader exists
# (docs/architecture.md's GRADE ==> A_EFF feedback arrow); not decided/tuned here.
QB_CHANGE_DISCOUNT = 0.6
OL_MIN_FACTOR = 0.7
_SOLVER_TOLERANCE = 1e-6
_SOLVER_MAX_ITERATIONS = 200

# snaps.position uses C/G/T for teams PFR breaks out by individual line spot, but some
# teams' snap counts lump every lineman under a generic "OL" tag instead (verified live,
# docs/sources.md: ARI/CHI/JAX/LA have zero C/G/T rows in both 2025 and 2026 snap_counts
# -- 100% of their O-line snaps are tagged "OL"). Omitting "OL" here silently emptied
# _ol_group for exactly those teams every time they were the *prior*-season side (the
# live-week depth-chart fallback masked it for the *current* week only, producing "5
# players current, prior=set()"). depth.pos_abb uses side-specific slots instead --
# verified live (docs/sources.md). depth.pos_grp is NOT offense/defense (it's a
# formation label, e.g. "Base 4-3 D") and must never be used to filter for O-line.
_OL_SNAP_POSITIONS = {"C", "G", "T", "OL"}
_OL_DEPTH_SLOTS = {"C", "LG", "LT", "RG", "RT"}

_INPUTS_VERSION_TAGS = ("pbp", "stats_player", "snap_counts", "depth_charts")


class MetricConfig(NamedTuple):
    name: str
    numerator_col: str
    denominator_col: str
    k_metric: float
    qb_sensitivity: float
    ol_sensitivity: float
    # Year-over-year reliability r (see docs/signals.md's "Prior-blend reliability r"
    # section for the full method) -- multiplies prior_discount in _blend, alongside the
    # QB/OL discount for offense. `_raw` is the pooled measured value before shrinkage;
    # the un-suffixed field is what actually feeds the blend. Both default to 1.0 (full
    # trust, today's pre-estimation behavior) for any MetricConfig built without them,
    # e.g. test helpers.
    reliability_off: float = 1.0
    reliability_def: float = 1.0
    reliability_off_raw: float = 1.0
    reliability_def_raw: float = 1.0


# Estimated by scripts/estimate_reliability.py -- see docs/signals.md for the method,
# and re-run once each season completes (a full season's worth of new pairs shifts the
# pooled estimate).
_RELIABILITY_ESTIMATED_FROM_SEASONS = "2018-2025"
_RELIABILITY_ESTIMATED_ON = "2026-09-17"

# Full registry entries (formula/filters/sample_n) live in docs/signals.md -- this is
# just the (numerator, denominator, shrinkage, discount-sensitivity, reliability) config
# each of the 20 base metrics needs. qb_sensitivity/ol_sensitivity scale how much the
# offense-side QB-change/OL-continuity discount applies to that metric (0 = no effect,
# 1 = full effect) -- e.g. a QB change shouldn't discount a pure rush split the way it
# discounts a pass split. Defense never uses these (see _offense_discount's caller).
_METRIC_CONFIG: list[MetricConfig] = [
    MetricConfig(
        "epa_per_play", "epa_sum", "plays", 200, 0.5, 0.5,
        reliability_off=0.3608, reliability_def=0.2262,
        reliability_off_raw=0.3960, reliability_def_raw=0.2482,
    ),
    MetricConfig(
        "epa_per_play_pass", "pass_epa_sum", "pass_plays", 120, 1.0, 0.5,
        reliability_off=0.3701, reliability_def=0.2050,
        reliability_off_raw=0.4146, reliability_def_raw=0.2058,
    ),
    MetricConfig(
        "epa_per_play_rush", "rush_epa_sum", "rush_plays", 120, 0.0, 1.0,
        reliability_off=0.2938, reliability_def=0.1986,
        reliability_off_raw=0.2620, reliability_def_raw=0.1930,
    ),
    MetricConfig(
        "epa_per_play_down1", "down1_epa_sum", "down1_plays", 50, 0.5, 0.5,
        reliability_off=0.2780, reliability_def=0.1994,
        reliability_off_raw=0.2305, reliability_def_raw=0.1947,
    ),
    MetricConfig(
        "epa_per_play_down2", "down2_epa_sum", "down2_plays", 50, 0.5, 0.5,
        reliability_off=0.3217, reliability_def=0.1807,
        reliability_off_raw=0.3178, reliability_def_raw=0.1573,
    ),
    MetricConfig(
        "epa_per_play_down3", "down3_epa_sum", "down3_plays", 50, 0.5, 0.5,
        reliability_off=0.3078, reliability_def=0.1810,
        reliability_off_raw=0.2901, reliability_def_raw=0.1578,
    ),
    MetricConfig(
        # 0.0/0.0, not shrunk toward the side mean like every other metric here --
        # 4th-down attempts are too rare a denominator for any year-over-year signal to
        # survive (raw pooled r was NEGATIVE, -0.168/-0.211, before clipping). Real
        # finding, not noise -- see docs/signals.md.
        "epa_per_play_down4", "down4_epa_sum", "down4_plays", 50, 0.5, 0.5,
        reliability_off=0.0, reliability_def=0.0,
        reliability_off_raw=0.0, reliability_def_raw=0.0,
    ),
    MetricConfig(
        "success_rate", "success_count", "plays", 200, 0.5, 0.5,
        reliability_off=0.3690, reliability_def=0.2232,
        reliability_off_raw=0.4124, reliability_def_raw=0.2422,
    ),
    MetricConfig(
        "success_rate_pass", "pass_success_count", "pass_plays", 120, 1.0, 0.5,
        reliability_off=0.3789, reliability_def=0.2367,
        reliability_off_raw=0.4322, reliability_def_raw=0.2692,
    ),
    MetricConfig(
        "success_rate_rush", "rush_success_count", "rush_plays", 120, 0.0, 1.0,
        reliability_off=0.3354, reliability_def=0.1946,
        reliability_off_raw=0.3452, reliability_def_raw=0.1851,
    ),
    MetricConfig(
        "success_rate_down1", "down1_success_count", "down1_plays", 50, 0.5, 0.5,
        reliability_off=0.2807, reliability_def=0.1795,
        reliability_off_raw=0.2359, reliability_def_raw=0.1547,
    ),
    MetricConfig(
        "success_rate_down2", "down2_success_count", "down2_plays", 50, 0.5, 0.5,
        reliability_off=0.3446, reliability_def=0.2107,
        reliability_off_raw=0.3637, reliability_def_raw=0.2173,
    ),
    MetricConfig(
        "success_rate_down3", "down3_success_count", "down3_plays", 50, 0.5, 0.5,
        reliability_off=0.3267, reliability_def=0.1729,
        reliability_off_raw=0.3279, reliability_def_raw=0.1417,
    ),
    MetricConfig(
        # See epa_per_play_down4 above -- same reasoning, same rare denominator.
        "success_rate_down4", "down4_success_count", "down4_plays", 50, 0.5, 0.5,
        reliability_off=0.0, reliability_def=0.0,
        reliability_off_raw=0.0, reliability_def_raw=0.0,
    ),
    MetricConfig(
        "explosive_rate", "explosive_count", "plays", 200, 0.5, 0.5,
        reliability_off=0.3168, reliability_def=0.2422,
        reliability_off_raw=0.3080, reliability_def_raw=0.2803,
    ),
    MetricConfig(
        "explosive_rate_pass", "pass_explosive_count", "pass_plays", 120, 1.0, 0.5,
        reliability_off=0.2838, reliability_def=0.1830,
        reliability_off_raw=0.2421, reliability_def_raw=0.1618,
    ),
    MetricConfig(
        # Flagged for review: raw defense r (0.3249) exceeds raw offense r (0.3181) by
        # 0.007 -- accepted as within estimation noise for 7 season-pairs, not a sign
        # something's backwards (docs/signals.md).
        "explosive_rate_rush", "rush_explosive_count", "rush_plays", 120, 0.0, 1.0,
        reliability_off=0.3218, reliability_def=0.2645,
        reliability_off_raw=0.3181, reliability_def_raw=0.3249,
    ),
    MetricConfig(
        "points_per_drive", "points", "drives", 15, 0.5, 0.5,
        reliability_off=0.3601, reliability_def=0.2184,
        reliability_off_raw=0.3947, reliability_def_raw=0.2327,
    ),
    MetricConfig(
        "three_and_out_rate", "three_and_out_drives", "drives", 15, 0.5, 0.5,
        reliability_off=0.3483, reliability_def=0.1888,
        reliability_off_raw=0.3710, reliability_def_raw=0.1734,
    ),
    MetricConfig(
        # Flagged for review: raw offense r (0.1980) sits just under the 0.2 threshold --
        # accepted as within estimation noise, not treated as zero-signal like down4
        # (docs/signals.md).
        "red_zone_td_rate", "red_zone_tds", "red_zone_trips", 8, 0.5, 0.5,
        reliability_off=0.2618, reliability_def=0.1695,
        reliability_off_raw=0.1980, reliability_def_raw=0.1349,
    ),
]

_TEAM_WEEK_SELECT_COLS = sorted(
    {c for m in _METRIC_CONFIG for c in (m.numerator_col, m.denominator_col)}
)


def _team_week_col_dtype(col: str) -> Any:
    return pl.Float64 if col.endswith("_sum") else pl.Int64


_TEAM_WEEK_SCHEMA: dict[str, Any] = {
    "season": pl.Int64,
    "week": pl.Int64,
    "season_type": pl.Utf8,
    "team": pl.Utf8,
    "opponent_team": pl.Utf8,
    **{c: _team_week_col_dtype(c) for c in _TEAM_WEEK_SELECT_COLS},
}
_PLAYER_WEEK_QB_SCHEMA: dict[str, Any] = {
    "player_id": pl.Utf8,
    "team": pl.Utf8,
    "season": pl.Int64,
    "week": pl.Int64,
    "season_type": pl.Utf8,
    "attempts": pl.Int64,
}
_SNAPS_OL_SCHEMA: dict[str, Any] = {
    "player_id": pl.Utf8,
    "team": pl.Utf8,
    "season": pl.Int64,
    "week": pl.Int64,
    "season_type": pl.Utf8,
    "position": pl.Utf8,
    "offense_snaps": pl.Int64,
}
_DEPTH_SCHEMA: dict[str, Any] = {
    "team": pl.Utf8,
    "pos_abb": pl.Utf8,
    "pos_rank": pl.Int64,
    "player_id": pl.Utf8,
}

_SIGNAL_SCHEMA: dict[str, Any] = {
    "game_id": pl.Utf8,
    "season": pl.Int64,
    "week": pl.Int64,
    "team": pl.Utf8,
    "player_id": pl.Utf8,
    "sector": pl.Utf8,
    "signal": pl.Utf8,
    "value": pl.Float64,
    "league_pct": pl.Float32,
    "sample_n": pl.Int64,
    "stability": pl.Float32,
    "as_of": pl.Datetime,
    "inputs_version": pl.Utf8,
}
_SIGNAL_COLS = list(_SIGNAL_SCHEMA)

# Every signal name this analyst can ever write -- derived from _METRIC_CONFIG rather
# than hardcoded, so it can't drift from _build_metric_signal_rows's own
# f"{metric.name}_{side}" naming. Assigned to the Analyst.signal_names class attribute
# below, which the base class uses to delete exactly this set (scoped to
# sector/season/week) before each write -- see pipeline/core/base.py's
# _delete_stale_signals.
_SIGNAL_NAMES = frozenset(
    f"{metric.name}_{side}" for metric in _METRIC_CONFIG for side in ("off", "def")
)


# --------------------------------------------------------------------------------------
# Point-in-time filtering (no leakage, backfill- and postseason-ready)
# --------------------------------------------------------------------------------------


def _filter_current_season(df: pl.DataFrame, season: int, before_week: int) -> pl.DataFrame:
    """Current-season rows strictly before `before_week`.

    No season_type branching is needed: verified live (docs/sources.md) that nflverse's
    POST week numbers continue incrementing from the regular season (e.g. 2023 REG runs
    1-18, POST runs 19-22), not restart at 1 -- so a plain `week < before_week` on a
    postseason week already includes the entire regular season plus any earlier
    postseason games, exactly the intended behavior, with no explicit OR-branch needed.
    """
    return df.filter((pl.col("season") == season) & (pl.col("week") < before_week))


# --------------------------------------------------------------------------------------
# Opponent-adjustment solver
# --------------------------------------------------------------------------------------


class RatingResult(NamedTuple):
    off: float | None
    off_n: float
    def_: float | None
    def_n: float


class BlendResult(NamedTuple):
    value: float | None
    w_cur: float
    w_prior: float
    w_league: float


def _league_avg(df: pl.DataFrame, numerator_col: str, denominator_col: str) -> float | None:
    if df.height == 0:
        return None
    total_d = df[denominator_col].sum()
    if not total_d:
        return None
    return float(df[numerator_col].sum()) / float(total_d)


def _recenter(
    ratings: dict[str, float], totals: dict[str, float], league_avg: float
) -> dict[str, float]:
    total_d = sum(totals.get(t, 0.0) for t in ratings)
    if total_d <= 0:
        return ratings
    weighted_mean = sum(ratings[t] * totals.get(t, 0.0) for t in ratings) / total_d
    shift = league_avg - weighted_mean
    return {t: v + shift for t, v in ratings.items()}


def _blend(
    *,
    current: float | None,
    prior: float | None,
    league_avg: float,
    n_cur: float,
    k_metric: float,
    prior_discount: float,
) -> BlendResult:
    """Three-way current/prior/league weighting. `stability = w_cur + w_prior`: a
    discount lowers `w_prior` and hands its share to `w_league`, never to `w_cur`, so a
    QB/OL change (or a genuinely missing prior, `prior_discount` forced to 0) shows up
    as lower stability, not as more trust in the current season than its sample earns.
    """
    denom = n_cur + k_metric
    w_cur = (n_cur / denom) if denom > 0 else 0.0
    if prior is None:
        prior_discount = 0.0
    w_prior = (1.0 - w_cur) * prior_discount
    w_league = 1.0 - w_cur - w_prior
    value = (
        w_cur * (current if current is not None else 0.0)
        + w_prior * (prior if prior is not None else 0.0)
        + w_league * league_avg
    )
    return BlendResult(value, w_cur, w_prior, w_league)


def _solve_ratings(
    df: pl.DataFrame,
    numerator_col: str,
    denominator_col: str,
    league_avg: float,
    k_metric: float,
    *,
    prior_off: dict[str, float] | None = None,
    prior_def: dict[str, float] | None = None,
    offense_discount: dict[str, float] | None = None,
) -> dict[str, RatingResult]:
    """Solve offense/defense ratings jointly by fixed-point iteration.

    Prior-season call (`prior_off`/`prior_def` omitted): a plain solve. `k_metric`
    pseudo-plays of league-average value shrink each team's own rating toward the
    league average within the update itself.

    Current-season call (`prior_off`/`prior_def` given, from a completed prior-season
    solve): no internal shrinkage of a team's own rating -- instead, each iteration
    references an opponent's `_blend()`-ed rating (this solve's own in-progress rating,
    the opponent's prior rating, and the league average) rather than its raw
    current-season-only rating. This is the only stabilization current-season ratings
    get; the final signal value is blended again, once, by the caller -- shrinkage never
    happens twice for the same team/metric.
    """
    is_current = prior_off is not None

    if df.height == 0:
        return {}

    off_agg = df.group_by("team").agg(
        pl.col(numerator_col).sum().alias("n"), pl.col(denominator_col).sum().alias("d")
    )
    def_agg = df.group_by("opponent_team").agg(
        pl.col(numerator_col).sum().alias("n"), pl.col(denominator_col).sum().alias("d")
    )
    off_totals_nd = {r["team"]: (r["n"], r["d"]) for r in off_agg.to_dicts()}
    def_totals_nd = {r["opponent_team"]: (r["n"], r["d"]) for r in def_agg.to_dicts()}
    off_totals_d = {t: d for t, (_, d) in off_totals_nd.items()}
    def_totals_d = {t: d for t, (_, d) in def_totals_nd.items()}

    games = df.select("team", "opponent_team", numerator_col, denominator_col).to_dicts()
    games_by_team: dict[str, list[dict[str, Any]]] = {}
    games_by_opponent: dict[str, list[dict[str, Any]]] = {}
    for g in games:
        games_by_team.setdefault(g["team"], []).append(g)
        games_by_opponent.setdefault(g["opponent_team"], []).append(g)

    active_off = {t for t, d in off_totals_d.items() if d > 0}
    active_def = {t for t, d in def_totals_d.items() if d > 0}

    off_rating: dict[str, float] = dict.fromkeys(active_off, league_avg)
    def_rating: dict[str, float] = dict.fromkeys(active_def, league_avg)

    def _reference(
        team: str,
        ratings: dict[str, float],
        prior: dict[str, float] | None,
        totals_d: dict[str, float],
        discount: dict[str, float] | None,
    ) -> float:
        if prior is None:
            return ratings.get(team, league_avg)
        pd = 1.0 if discount is None else discount.get(team, 1.0)
        return _blend(
            current=ratings.get(team),
            prior=prior.get(team),
            league_avg=league_avg,
            n_cur=totals_d.get(team, 0.0),
            k_metric=k_metric,
            prior_discount=pd,
        ).value or league_avg

    for _ in range(_SOLVER_MAX_ITERATIONS):
        new_off: dict[str, float] = {}
        for team in active_off:
            total_n = 0.0
            total_d = 0.0
            for g in games_by_team.get(team, []):
                ref_def = _reference(g["opponent_team"], def_rating, prior_def, def_totals_d, None)
                total_n += g[numerator_col] + g[denominator_col] * (league_avg - ref_def)
                total_d += g[denominator_col]
            if is_current:
                new_off[team] = total_n / total_d if total_d > 0 else league_avg
            else:
                new_off[team] = (total_n + k_metric * league_avg) / (total_d + k_metric)

        new_def: dict[str, float] = {}
        for team in active_def:
            total_n = 0.0
            total_d = 0.0
            for g in games_by_opponent.get(team, []):
                ref_off = _reference(
                    g["team"], off_rating, prior_off, off_totals_d, offense_discount
                )
                total_n += g[numerator_col] + g[denominator_col] * (league_avg - ref_off)
                total_d += g[denominator_col]
            if is_current:
                new_def[team] = total_n / total_d if total_d > 0 else league_avg
            else:
                new_def[team] = (total_n + k_metric * league_avg) / (total_d + k_metric)

        new_off = _recenter(new_off, off_totals_d, league_avg)
        new_def = _recenter(new_def, def_totals_d, league_avg)

        max_delta = max(
            [abs(new_off[t] - off_rating[t]) for t in active_off]
            + [abs(new_def[t] - def_rating[t]) for t in active_def],
            default=0.0,
        )
        off_rating, def_rating = new_off, new_def
        if max_delta < _SOLVER_TOLERANCE:
            break

    return {
        t: RatingResult(
            off_rating.get(t),
            off_totals_d.get(t, 0.0),
            def_rating.get(t),
            def_totals_d.get(t, 0.0),
        )
        for t in active_off | active_def
    }


# --------------------------------------------------------------------------------------
# QB-change / OL-continuity discount factors (offense only)
# --------------------------------------------------------------------------------------


def _current_season_starting_qb(player_week_current: pl.DataFrame, team: str) -> str | None:
    """Most-attempts starter in the team's single most recent game so far this season."""
    team_qbs = player_week_current.filter(pl.col("team") == team)
    if team_qbs.height == 0:
        return None
    most_recent_week = team_qbs["week"].max()
    candidates = (
        team_qbs.filter(pl.col("week") == most_recent_week)
        .with_columns(pl.col("attempts").fill_null(0))
        .sort(["attempts", "player_id"], descending=[True, False])
    )
    return candidates["player_id"][0] if candidates.height else None


def _prior_season_team_total_attempts(player_week_prior: pl.DataFrame, team: str) -> float:
    """Every pass attempt thrown by any of the team's QBs across the full prior season."""
    team_qbs = player_week_prior.filter(pl.col("team") == team)
    if team_qbs.height == 0:
        return 0.0
    return float(team_qbs["attempts"].fill_null(0).sum())


def _prior_season_qb_attempts(player_week_prior: pl.DataFrame, player_id: str) -> float:
    """This QB's own prior-season pass attempts, summed across every team they played for
    (not just the current one) -- so a traded veteran's continuity share counts their
    attempts from the old team too."""
    rows = player_week_prior.filter(pl.col("player_id") == player_id)
    if rows.height == 0:
        return 0.0
    return float(rows["attempts"].fill_null(0).sum())


def _ol_group(snaps_df: pl.DataFrame, team: str) -> set[str]:
    """Top-5 player_ids by summed offense_snaps among O-line positions."""
    team_ol = snaps_df.filter(pl.col("team") == team)
    if team_ol.height == 0:
        return set()
    totals = (
        team_ol.group_by("player_id")
        .agg(pl.col("offense_snaps").fill_null(0).sum().alias("offense_snaps"))
        .sort(["offense_snaps", "player_id"], descending=[True, False])
        .head(5)
    )
    return set(totals["player_id"].to_list())


def _depth_fallback_allowed(ctx: RunContext) -> bool:
    """True only when (ctx.season, ctx.week) exactly matches the live current
    (season, week) -- not a date-window check. `depth` holds only the latest scraped
    snapshot, no history, so it must never be consulted for a historical backfill week,
    nor for a same-season re-run of an earlier week once the live week has moved past
    it (which a date window alone wouldn't catch). get_current_week(use_date=True) is
    used deliberately: the default use_date=False path calls load_schedules(), a
    network fetch not allowed from L2 code; use_date=True is pure local date math (a
    "rough approximation" per its own docstring -- acceptable here since this only
    gates a discount-factor fallback source, not a precise calculation).
    """
    return ctx.season == nfl.get_current_season() and ctx.week == nfl.get_current_week(
        use_date=True
    )


def _depth_qb(depth_df: pl.DataFrame, team: str) -> str | None:
    row = depth_df.filter(
        (pl.col("team") == team) & (pl.col("pos_abb") == "QB") & (pl.col("pos_rank") == 1)
    )
    if row.height == 0:
        return None
    return row["player_id"][0]


def _depth_ol_group(depth_df: pl.DataFrame, team: str) -> set[str]:
    rows = depth_df.filter(
        (pl.col("team") == team)
        & pl.col("pos_abb").is_in(_OL_DEPTH_SLOTS)
        & (pl.col("pos_rank") == 1)
        & pl.col("player_id").is_not_null()
    )
    return set(rows["player_id"].to_list())


def _resolve_current_qb(
    ctx: RunContext, player_week_current: pl.DataFrame, depth_df: pl.DataFrame | None, team: str
) -> str | None:
    qb = _current_season_starting_qb(player_week_current, team)
    if qb is not None:
        return qb
    if depth_df is not None and _depth_fallback_allowed(ctx):
        return _depth_qb(depth_df, team)
    return None


def _resolve_current_ol_group(
    ctx: RunContext, snaps_current: pl.DataFrame, depth_df: pl.DataFrame | None, team: str
) -> set[str]:
    group = _ol_group(snaps_current, team)
    if group:
        return group
    if depth_df is not None and _depth_fallback_allowed(ctx):
        return _depth_ol_group(depth_df, team)
    return set()


_QB_FULL_CONTINUITY_SHARE = 0.5


def _qb_change_factor(
    current_qb: str | None,
    current_qb_prior_attempts: float,
    team_prior_total_attempts: float,
) -> float:
    """Continuity-share discount, not a binary same/different starter flag: a starter who
    missed half the prior season to injury (or was traded in from elsewhere) isn't
    penalized like a brand-new starter just because a backup led the team in attempts.

    `share` = this season's starter's own prior-season pass attempts -- summed across
    ANY team they played for, so a traded veteran gets full credit -- divided by the
    *current* team's prior-season total attempts, capped at 1 (a starter who threw more
    passes elsewhere last season than this team's whole QB room combined still only earns
    full continuity, not bonus credit). `continuity = min(1, share / 0.5)`: reaching half
    of the team's prior passing workload already earns full trust (1.0 = no discount);
    below that it scales linearly down to a brand-new starter's `share = 0`, which
    reduces to the same `QB_CHANGE_DISCOUNT` floor as before.

    1.0 = no discount (unknown starter, or no prior-season team data -- never guessed).
    Mid-season QB changes aren't specially handled: this only compares year-over-year
    workload, so a benched/injured QB replaced mid-current-season isn't caught until
    enough of the new starter's own games accumulate (documented gap, docs/signals.md).
    """
    if current_qb is None or team_prior_total_attempts <= 0:
        _log.warning(
            "efficiency: QB-change discount skipped (unknown starter or no prior team "
            "attempts) -- current=%s team_prior_total_attempts=%s",
            current_qb,
            team_prior_total_attempts,
        )
        return 1.0
    share = min(1.0, current_qb_prior_attempts / team_prior_total_attempts)
    continuity = min(1.0, share / _QB_FULL_CONTINUITY_SHARE)
    return 1.0 - (1.0 - QB_CHANGE_DISCOUNT) * (1.0 - continuity)


def _ol_continuity_factor(current_group: set[str], prior_group: set[str]) -> float:
    if not current_group or not prior_group:
        _log.warning(
            "efficiency: OL-continuity discount skipped (unknown group) -- current=%s prior=%s",
            current_group,
            prior_group,
        )
        return 1.0
    continuity_ratio = len(current_group & prior_group) / 5
    return OL_MIN_FACTOR + (1.0 - OL_MIN_FACTOR) * continuity_ratio


def _offense_discount(
    qb_factor: float, ol_factor: float, qb_sensitivity: float, ol_sensitivity: float
) -> float:
    return (1.0 - qb_sensitivity * (1.0 - qb_factor)) * (1.0 - ol_sensitivity * (1.0 - ol_factor))


# --------------------------------------------------------------------------------------
# Signal-row assembly
# --------------------------------------------------------------------------------------


def _build_metric_signal_rows(
    *,
    metric: MetricConfig,
    current_tw: pl.DataFrame,
    prior_tw: pl.DataFrame,
    teams: list[str],
    offense_discount: dict[str, float],
    season: int,
    week: int,
    as_of: Any,
    inputs_version: str,
) -> list[dict[str, Any]]:
    league_avg_prior = _league_avg(prior_tw, metric.numerator_col, metric.denominator_col)
    prior_ratings: dict[str, RatingResult] = {}
    if league_avg_prior is not None:
        prior_ratings = _solve_ratings(
            prior_tw,
            metric.numerator_col,
            metric.denominator_col,
            league_avg_prior,
            metric.k_metric,
        )

    league_avg_cur = _league_avg(current_tw, metric.numerator_col, metric.denominator_col)
    league_avg_for_blend = league_avg_cur if league_avg_cur is not None else league_avg_prior

    current_ratings: dict[str, RatingResult] = {}
    if league_avg_cur is not None:
        prior_off = {t: r.off for t, r in prior_ratings.items() if r.off is not None}
        prior_def = {t: r.def_ for t, r in prior_ratings.items() if r.def_ is not None}
        current_ratings = _solve_ratings(
            current_tw,
            metric.numerator_col,
            metric.denominator_col,
            league_avg_cur,
            metric.k_metric,
            prior_off=prior_off,
            prior_def=prior_def,
            offense_discount=offense_discount,
        )

    rows: list[dict[str, Any]] = []
    for team in teams:
        cur = current_ratings.get(team)
        pri = prior_ratings.get(team)
        for side, discount in (
            ("off", offense_discount[team] * metric.reliability_off),
            ("def", metric.reliability_def),
        ):
            cur_val = (cur.off if side == "off" else cur.def_) if cur else None
            cur_n = (cur.off_n if side == "off" else cur.def_n) if cur else 0.0
            pri_val = (pri.off if side == "off" else pri.def_) if pri else None

            if cur_val is None and pri_val is None:
                value, sample_n, stability = None, 0, 0.0
            else:
                blend = _blend(
                    current=cur_val,
                    prior=pri_val,
                    league_avg=league_avg_for_blend or 0.0,
                    n_cur=cur_n,
                    k_metric=metric.k_metric,
                    prior_discount=discount,
                )
                value, sample_n, stability = blend.value, int(cur_n), blend.w_cur + blend.w_prior

            rows.append(
                {
                    "game_id": None,
                    "season": season,
                    "week": week,
                    "team": team,
                    "player_id": None,
                    "sector": "efficiency",
                    "signal": f"{metric.name}_{side}",
                    "value": value,
                    "league_pct": None,
                    "sample_n": sample_n,
                    "stability": stability,
                    "as_of": as_of,
                    "inputs_version": inputs_version,
                }
            )
    return rows


def _rows_to_df(rows: list[tuple[Any, ...]], schema: dict[str, Any]) -> pl.DataFrame:
    if not rows:
        return pl.DataFrame(schema=schema)
    return pl.DataFrame(rows, schema=schema, orient="row")


def _fetch_team_week(conn: psycopg.Connection, season: int, prior_season: int) -> pl.DataFrame:
    cols = ["season", "week", "season_type", "team", "opponent_team"] + _TEAM_WEEK_SELECT_COLS
    with conn.cursor() as cur:
        cur.execute(
            f"SELECT {', '.join(cols)} FROM team_week WHERE season IN (%s, %s)",
            (season, prior_season),
        )
        rows = cur.fetchall()
    return _rows_to_df(rows, _TEAM_WEEK_SCHEMA)


def _fetch_player_week_qb(conn: psycopg.Connection, season: int, prior_season: int) -> pl.DataFrame:
    cols = list(_PLAYER_WEEK_QB_SCHEMA)
    with conn.cursor() as cur:
        cur.execute(
            f"SELECT {', '.join(cols)} FROM player_week "
            "WHERE position = 'QB' AND season IN (%s, %s)",
            (season, prior_season),
        )
        rows = cur.fetchall()
    return _rows_to_df(rows, _PLAYER_WEEK_QB_SCHEMA)


def _fetch_snaps_ol(conn: psycopg.Connection, season: int, prior_season: int) -> pl.DataFrame:
    cols = list(_SNAPS_OL_SCHEMA)
    with conn.cursor() as cur:
        cur.execute(
            f"SELECT {', '.join(cols)} FROM snaps "
            "WHERE position = ANY(%s) AND player_id IS NOT NULL AND season IN (%s, %s)",
            (list(_OL_SNAP_POSITIONS), season, prior_season),
        )
        rows = cur.fetchall()
    return _rows_to_df(rows, _SNAPS_OL_SCHEMA)


def _fetch_depth(conn: psycopg.Connection) -> pl.DataFrame:
    cols = list(_DEPTH_SCHEMA)
    with conn.cursor() as cur:
        cur.execute(f"SELECT {', '.join(cols)} FROM depth")
        rows = cur.fetchall()
    return _rows_to_df(rows, _DEPTH_SCHEMA)


def _build_inputs_version(conn: psycopg.Connection) -> str:
    return ",".join(
        f"{tag}@{get_last_value(conn, f'nflverse:{tag}') or 'unknown'}"
        for tag in _INPUTS_VERSION_TAGS
    )


class EfficiencyAnalyst(Analyst):
    name = "efficiency"
    sector = "efficiency"
    signal_names = _SIGNAL_NAMES

    # Stashed by compute() for write_signals() to hand to WorkResult.meta -- compute()
    # always runs immediately before write_signals() within one Analyst.run() call (see
    # base.py), so this is safe despite being instance state rather than a return value;
    # threading it through compute()'s -> pl.DataFrame contract isn't an option without
    # widening that contract too, which wasn't part of this change.
    _last_run_meta: dict[str, Any]

    def inputs_ready(self, ctx: RunContext) -> bool | str:
        """Gates on the prior season existing, not the current one -- a week-1 run is
        valid and expected to produce prior+league-only signals (see _blend)."""
        with ctx.conn.cursor() as cur:
            cur.execute(
                "SELECT count(*) FROM team_week WHERE season = %s AND season_type = 'REG'",
                (ctx.season - 1,),
            )
            row = cur.fetchone()
            count = row[0] if row else 0
        return True if count > 0 else "skipped_no_prior"

    def compute(self, ctx: RunContext) -> pl.DataFrame:
        conn = ctx.conn
        prior_season = ctx.season - 1

        team_week_all = _fetch_team_week(conn, ctx.season, prior_season)
        current_tw = _filter_current_season(
            team_week_all.filter(pl.col("season") == ctx.season), ctx.season, ctx.week
        )
        prior_tw = team_week_all.filter(
            (pl.col("season") == prior_season) & (pl.col("season_type") == "REG")
        )

        teams = sorted(
            set(team_week_all["team"].to_list()) | set(team_week_all["opponent_team"].to_list())
        )

        player_week_qb = _fetch_player_week_qb(conn, ctx.season, prior_season)
        current_pw = _filter_current_season(
            player_week_qb.filter(pl.col("season") == ctx.season), ctx.season, ctx.week
        )
        prior_pw = player_week_qb.filter(
            (pl.col("season") == prior_season) & (pl.col("season_type") == "REG")
        )

        snaps_ol = _fetch_snaps_ol(conn, ctx.season, prior_season)
        current_snaps = _filter_current_season(
            snaps_ol.filter(pl.col("season") == ctx.season), ctx.season, ctx.week
        )
        prior_snaps = snaps_ol.filter(
            (pl.col("season") == prior_season) & (pl.col("season_type") == "REG")
        )

        depth_df = _fetch_depth(conn) if _depth_fallback_allowed(ctx) else None

        qb_factor: dict[str, float] = {}
        ol_factor: dict[str, float] = {}
        team_meta: dict[str, dict[str, Any]] = {}
        for team in teams:
            cur_qb = _resolve_current_qb(ctx, current_pw, depth_df, team)
            team_prior_total_attempts = _prior_season_team_total_attempts(prior_pw, team)
            cur_qb_prior_attempts = (
                _prior_season_qb_attempts(prior_pw, cur_qb) if cur_qb is not None else 0.0
            )
            qb_factor[team] = _qb_change_factor(
                cur_qb, cur_qb_prior_attempts, team_prior_total_attempts
            )

            cur_ol = _resolve_current_ol_group(ctx, current_snaps, depth_df, team)
            prior_ol = _ol_group(prior_snaps, team)
            ol_factor[team] = _ol_continuity_factor(cur_ol, prior_ol)

            team_meta[team] = {
                "qb_factor": round(qb_factor[team], 4),
                "current_qb": cur_qb,
                "current_qb_prior_attempts": cur_qb_prior_attempts,
                "team_prior_total_attempts": team_prior_total_attempts,
                "ol_factor": round(ol_factor[team], 4),
                "ol_overlap": len(cur_ol & prior_ol),
                "current_ol": sorted(cur_ol),
                "prior_ol": sorted(prior_ol),
            }

        self._last_run_meta = {"teams": team_meta}

        inputs_version = _build_inputs_version(conn)

        rows: list[dict[str, Any]] = []
        for metric in _METRIC_CONFIG:
            offense_discount = {
                t: _offense_discount(
                    qb_factor[t], ol_factor[t], metric.qb_sensitivity, metric.ol_sensitivity
                )
                for t in teams
            }
            rows.extend(
                _build_metric_signal_rows(
                    metric=metric,
                    current_tw=current_tw,
                    prior_tw=prior_tw,
                    teams=teams,
                    offense_discount=offense_discount,
                    season=ctx.season,
                    week=ctx.week,
                    as_of=ctx.now,
                    inputs_version=inputs_version,
                )
            )

        if rows:
            return pl.DataFrame(rows, schema=_SIGNAL_COLS)
        return pl.DataFrame(schema=_SIGNAL_SCHEMA)

    def write_signals(self, ctx: RunContext, df: pl.DataFrame) -> WorkResult:
        meta = self._last_run_meta
        rows = df.to_dicts()
        if not rows:
            return WorkResult(0, meta)
        conflict_cols = [
            "season",
            "week",
            "COALESCE(game_id, '')",
            "COALESCE(team, '')",
            "COALESCE(player_id, '')",
            "sector",
            "signal",
        ]
        identity_cols = ("season", "week", "game_id", "team", "player_id", "sector", "signal")
        update_cols = [c for c in _SIGNAL_COLS if c not in identity_cols]
        rows_written = upsert_rows(
            ctx.conn, "signals", rows, conflict_cols=conflict_cols, update_cols=update_cols
        )
        return WorkResult(rows_written, meta)
