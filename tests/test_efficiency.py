from datetime import UTC, datetime

import polars as pl
import pytest

from pipeline.analysts.efficiency import (
    OL_MIN_FACTOR,
    QB_CHANGE_DISCOUNT,
    MetricConfig,
    _blend,
    _build_metric_signal_rows,
    _depth_fallback_allowed,
    _filter_current_season,
    _league_avg,
    _offense_discount,
    _ol_continuity_factor,
    _qb_change_factor,
    _solve_ratings,
)
from pipeline.core.base import RunContext


def _tw_row(team, opponent, week, **overrides):
    row = {
        "season": 2099,
        "week": week,
        "season_type": "REG",
        "team": team,
        "opponent_team": opponent,
        "plays": 60,
        "epa_sum": 0.0,
        "success_count": 25,
        "explosive_count": 5,
        "pass_plays": 35,
        "pass_epa_sum": 0.0,
        "pass_success_count": 15,
        "pass_explosive_count": 3,
        "rush_plays": 25,
        "rush_epa_sum": 0.0,
        "rush_success_count": 10,
        "rush_explosive_count": 2,
        "down1_plays": 20,
        "down1_epa_sum": 0.0,
        "down1_success_count": 10,
        "down2_plays": 15,
        "down2_epa_sum": 0.0,
        "down2_success_count": 7,
        "down3_plays": 15,
        "down3_epa_sum": 0.0,
        "down3_success_count": 6,
        "down4_plays": 2,
        "down4_epa_sum": 0.0,
        "down4_success_count": 1,
        "drives": 10,
        "three_and_out_drives": 3,
        "red_zone_trips": 3,
        "red_zone_tds": 2,
        "points": 21,
    }
    row.update(overrides)
    return row


def _make_ctx(*, season=2099, week=5, season_type="REG"):
    return RunContext(
        season=season,
        week=week,
        season_type=season_type,
        now=datetime(2099, 10, 1, tzinfo=UTC),
        settings=None,  # type: ignore[arg-type]
        conn=None,  # type: ignore[arg-type]
    )


# --------------------------------------------------------------------------------------
# 1. Point-in-time filtering
# --------------------------------------------------------------------------------------


def test_no_leakage_extra_future_weeks_dont_change_output():
    rows_1_to_3 = [_tw_row("AA", "BB", w) for w in (1, 2, 3)]
    rows_1_to_6 = rows_1_to_3 + [_tw_row("AA", "BB", w) for w in (4, 5, 6)]

    a = _filter_current_season(pl.DataFrame(rows_1_to_3), 2099, 4)
    b = _filter_current_season(pl.DataFrame(rows_1_to_6), 2099, 4)

    assert a.sort("week").equals(b.sort("week"))
    assert set(a["week"].to_list()) == {1, 2, 3}


def test_postseason_current_season_includes_all_reg_plus_earlier_post():
    reg = [_tw_row("AA", "BB", w, season_type="REG") for w in range(1, 19)]
    post = [_tw_row("AA", "BB", 19, season_type="POST")]
    df = pl.DataFrame(reg + post)

    # nflverse POST weeks continue incrementing from REG (verified live) -- week 20
    # here means "entering the divisional round", after wildcard week 19.
    result = _filter_current_season(df, 2099, 20)
    assert result.height == 19
    assert set(result["season_type"].to_list()) == {"REG", "POST"}


# --------------------------------------------------------------------------------------
# Solver
# --------------------------------------------------------------------------------------


def test_prior_solver_converges_and_satisfies_fixed_point():
    df = pl.DataFrame(
        [
            _tw_row("AA", "BB", 1, epa_sum=10.0, plays=50),
            _tw_row("BB", "AA", 1, epa_sum=-10.0, plays=50),
            _tw_row("AA", "CC", 2, epa_sum=5.0, plays=50),
            _tw_row("CC", "AA", 2, epa_sum=-5.0, plays=50),
            _tw_row("BB", "CC", 3, epa_sum=0.0, plays=50),
            _tw_row("CC", "BB", 3, epa_sum=0.0, plays=50),
        ]
    )
    league_avg = _league_avg(df, "epa_sum", "plays")
    assert league_avg is not None
    ratings = _solve_ratings(df, "epa_sum", "plays", league_avg, k_metric=10.0)

    assert set(ratings) == {"AA", "BB", "CC"}
    # AA outscored two different opponents -- should end up above league average.
    assert ratings["AA"].off is not None and ratings["AA"].off > league_avg


def test_prior_solver_shrinks_tiny_sample_toward_league_average():
    df = pl.DataFrame(
        [
            _tw_row("AA", "BB", 1, success_count=60, plays=60),  # 100% success, 1 game
            _tw_row("BB", "AA", 1, success_count=0, plays=60),
            *[_tw_row("CC", "DD", w, success_count=30, plays=60) for w in range(1, 10)],
            *[_tw_row("DD", "CC", w, success_count=30, plays=60) for w in range(1, 10)],
        ]
    )
    league_avg = _league_avg(df, "success_count", "plays")
    assert league_avg is not None
    ratings = _solve_ratings(df, "success_count", "plays", league_avg, k_metric=200.0)

    raw_rate = 60 / 60
    assert ratings["AA"].off is not None
    assert abs(ratings["AA"].off - league_avg) < abs(raw_rate - league_avg)


def test_current_solver_zero_sample_team_is_unrated_not_crashed():
    current = pl.DataFrame([_tw_row("AA", "BB", 3, epa_sum=5.0, plays=60)])
    prior_off = {"AA": 0.1, "BB": -0.1, "CC": 0.0}
    prior_def = {"AA": -0.1, "BB": 0.1, "CC": 0.0}
    league_avg = _league_avg(current, "epa_sum", "plays")
    assert league_avg is not None

    ratings = _solve_ratings(
        current,
        "epa_sum",
        "plays",
        league_avg,
        k_metric=0.0,
        prior_off=prior_off,
        prior_def=prior_def,
        offense_discount={"AA": 1.0, "BB": 1.0, "CC": 1.0},
    )
    assert "CC" not in ratings
    assert "AA" in ratings and "BB" in ratings


def test_current_solver_has_no_internal_shrinkage():
    # Same tiny-sample dataset as the prior-solver shrinkage test above, same k_metric.
    # Prior-mode (plain solve) shrinks AA's OWN thin sample toward league_avg via
    # +k_metric pseudo-plays. Current-mode (prior_off/prior_def given) never adds that
    # term to AA's own total -- its only stabilization is via the opponent's (BB's)
    # blended reference -- so it should end up measurably closer to AA's raw 60/60 rate.
    df = pl.DataFrame(
        [
            _tw_row("AA", "BB", 1, success_count=60, plays=60),
            _tw_row("BB", "AA", 1, success_count=0, plays=60),
            *[_tw_row("CC", "DD", w, success_count=30, plays=60) for w in range(1, 10)],
            *[_tw_row("DD", "CC", w, success_count=30, plays=60) for w in range(1, 10)],
        ]
    )
    league_avg = _league_avg(df, "success_count", "plays")
    assert league_avg is not None
    k = 200.0

    prior_style = _solve_ratings(df, "success_count", "plays", league_avg, k_metric=k)

    teams = {"AA", "BB", "CC", "DD"}
    neutral_prior = dict.fromkeys(teams, league_avg)
    current_style = _solve_ratings(
        df,
        "success_count",
        "plays",
        league_avg,
        k_metric=k,
        prior_off=neutral_prior,
        prior_def=neutral_prior,
        offense_discount=dict.fromkeys(teams, 1.0),
    )

    raw_rate = 1.0  # AA went 60/60
    assert current_style["AA"].off is not None and prior_style["AA"].off is not None
    assert abs(current_style["AA"].off - raw_rate) < abs(prior_style["AA"].off - raw_rate)


# --------------------------------------------------------------------------------------
# Blend
# --------------------------------------------------------------------------------------


def test_blend_weights_are_monotonic_and_sum_to_one():
    prev_w_cur = -1.0
    for n_cur in (0, 10, 50, 200, 1000):
        result = _blend(
            current=1.0, prior=0.0, league_avg=0.5, n_cur=n_cur, k_metric=100.0, prior_discount=1.0
        )
        assert result.w_cur >= prev_w_cur
        assert abs(result.w_cur + result.w_prior + result.w_league - 1.0) < 1e-9
        prev_w_cur = result.w_cur


def test_blend_missing_prior_sends_share_to_league_not_current():
    with_prior = _blend(
        current=1.0, prior=0.5, league_avg=0.0, n_cur=10, k_metric=100.0, prior_discount=1.0
    )
    without_prior = _blend(
        current=1.0, prior=None, league_avg=0.0, n_cur=10, k_metric=100.0, prior_discount=1.0
    )
    assert without_prior.w_cur == with_prior.w_cur
    assert without_prior.w_prior == 0.0
    assert without_prior.w_league > with_prior.w_league


def test_week_one_prior_only():
    result = _blend(
        current=None, prior=7.0, league_avg=3.0, n_cur=0, k_metric=200.0, prior_discount=1.0
    )
    assert result.w_cur == 0.0
    assert result.w_league == 0.0
    assert result.value == pytest.approx(7.0)


# --------------------------------------------------------------------------------------
# QB / OL discount factors and sensitivity
# --------------------------------------------------------------------------------------


def test_qb_change_factor_same_vs_different():
    assert _qb_change_factor("00-1", "00-1") == 1.0
    assert _qb_change_factor("00-1", "00-2") == QB_CHANGE_DISCOUNT


def test_qb_change_factor_unknown_defaults_to_no_discount():
    assert _qb_change_factor(None, "00-2") == 1.0
    assert _qb_change_factor("00-1", None) == 1.0


def test_ol_continuity_factor_full_vs_no_overlap():
    group = {"a", "b", "c", "d", "e"}
    assert _ol_continuity_factor(group, group) == 1.0
    disjoint = {"f", "g", "h", "i", "j"}
    assert _ol_continuity_factor(group, disjoint) == OL_MIN_FACTOR


def test_offense_discount_sensitivity_scales_the_effect():
    # Full sensitivity passes the raw (changed) factor straight through.
    assert _offense_discount(QB_CHANGE_DISCOUNT, 1.0, 1.0, 0.0) == pytest.approx(QB_CHANGE_DISCOUNT)
    # Zero sensitivity makes the same QB change a complete no-op for this metric.
    assert _offense_discount(QB_CHANGE_DISCOUNT, 1.0, 0.0, 0.0) == pytest.approx(1.0)


def test_stability_drops_only_for_sensitive_metrics_on_qb_change():
    current = pl.DataFrame(
        [_tw_row(t, o, w) for t, o, w in [("AA", "BB", 1), ("BB", "AA", 1)]]
    )
    prior = pl.DataFrame(
        [_tw_row(t, o, w) for t, o, w in [("AA", "BB", 1), ("BB", "AA", 1)]]
    )
    teams = ["AA", "BB"]

    pass_metric = MetricConfig("epa_per_play_pass", "pass_epa_sum", "pass_plays", 120, 1.0, 0.5)
    rush_metric = MetricConfig("epa_per_play_rush", "rush_epa_sum", "rush_plays", 120, 0.0, 1.0)

    def _stability_for(metric, qb_changed):
        qb_factor = QB_CHANGE_DISCOUNT if qb_changed else 1.0
        discount = {
            t: _offense_discount(qb_factor, 1.0, metric.qb_sensitivity, metric.ol_sensitivity)
            for t in teams
        }
        rows = _build_metric_signal_rows(
            metric=metric,
            current_tw=current,
            prior_tw=prior,
            teams=teams,
            offense_discount=discount,
            season=2099,
            week=2,
            as_of=datetime(2099, 1, 1, tzinfo=UTC),
            inputs_version="test",
        )
        return next(
            r["stability"] for r in rows if r["team"] == "AA" and r["signal"].endswith("_off")
        )

    pass_same = _stability_for(pass_metric, qb_changed=False)
    pass_changed = _stability_for(pass_metric, qb_changed=True)
    rush_same = _stability_for(rush_metric, qb_changed=False)
    rush_changed = _stability_for(rush_metric, qb_changed=True)

    assert pass_changed < pass_same
    assert rush_changed == pytest.approx(rush_same)


# --------------------------------------------------------------------------------------
# Signal-row assembly
# --------------------------------------------------------------------------------------


def test_zero_data_both_seasons_writes_null_row_not_omitted():
    metric = MetricConfig("epa_per_play_down4", "down4_epa_sum", "down4_plays", 50, 0.5, 0.5)
    empty = pl.DataFrame(
        schema={
            "season": pl.Int64,
            "week": pl.Int64,
            "season_type": pl.Utf8,
            "team": pl.Utf8,
            "opponent_team": pl.Utf8,
            "down4_epa_sum": pl.Float64,
            "down4_plays": pl.Int64,
        }
    )
    rows = _build_metric_signal_rows(
        metric=metric,
        current_tw=empty,
        prior_tw=empty,
        teams=["AA"],
        offense_discount={"AA": 1.0},
        season=2099,
        week=1,
        as_of=datetime(2099, 1, 1, tzinfo=UTC),
        inputs_version="test",
    )
    assert len(rows) == 2  # off + def
    for row in rows:
        assert row["value"] is None
        assert row["sample_n"] == 0
        assert row["stability"] == 0.0


def test_build_metric_signal_rows_shape():
    current = pl.DataFrame([_tw_row("AA", "BB", 1), _tw_row("BB", "AA", 1)])
    prior = pl.DataFrame([_tw_row("AA", "BB", 1), _tw_row("BB", "AA", 1)])
    metric = MetricConfig("epa_per_play", "epa_sum", "plays", 200, 0.5, 0.5)
    rows = _build_metric_signal_rows(
        metric=metric,
        current_tw=current,
        prior_tw=prior,
        teams=["AA", "BB"],
        offense_discount={"AA": 1.0, "BB": 1.0},
        season=2099,
        week=2,
        as_of=datetime(2099, 1, 1, tzinfo=UTC),
        inputs_version="test",
    )
    assert {r["signal"] for r in rows} == {"epa_per_play_off", "epa_per_play_def"}
    assert {r["team"] for r in rows} == {"AA", "BB"}
    for row in rows:
        assert row["sector"] == "efficiency"
        assert row["game_id"] is None
        assert row["player_id"] is None
        if row["value"] is not None:
            assert row["value"] == row["value"]  # not NaN


# --------------------------------------------------------------------------------------
# Depth fallback guard
# --------------------------------------------------------------------------------------


def test_depth_fallback_requires_exact_season_and_week_match(monkeypatch):
    import pipeline.analysts.efficiency as eff

    monkeypatch.setattr(eff.nfl, "get_current_season", lambda: 2099)
    monkeypatch.setattr(eff.nfl, "get_current_week", lambda use_date=False: 5)

    assert _depth_fallback_allowed(_make_ctx(season=2099, week=5)) is True
    assert _depth_fallback_allowed(_make_ctx(season=2099, week=1)) is False
    assert _depth_fallback_allowed(_make_ctx(season=2098, week=5)) is False
