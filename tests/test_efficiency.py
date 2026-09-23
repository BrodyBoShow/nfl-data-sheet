from datetime import UTC, datetime

import polars as pl
import pytest
from polars.testing import assert_frame_equal

from pipeline.analysts.efficiency import (
    _OL_SNAP_POSITIONS,
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
    _ol_group,
    _prior_season_qb_attempts,
    _prior_season_team_total_attempts,
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


_E2E_TEAMS = ("AA", "BB", "CC", "DD")
_E2E_BASE_EPA = {"AA": 0.10, "BB": -0.05, "CC": 0.02, "DD": -0.08}


def _e2e_pairings(week):
    if week % 3 == 0:
        return [("AA", "DD"), ("BB", "CC")]
    if week % 2 == 0:
        return [("AA", "CC"), ("BB", "DD")]
    return [("AA", "BB"), ("CC", "DD")]


def _e2e_team_week(season, weeks, epa_shift=0.0):
    rows = []
    for w in weeks:
        for a, b in _e2e_pairings(w):
            for team, opp in ((a, b), (b, a)):
                epa = (_E2E_BASE_EPA[team] + 0.01 * w + epa_shift) * 60
                rows.append(
                    _tw_row(team, opp, w, season=season, epa_sum=epa, pass_epa_sum=epa * 0.6,
                            rush_epa_sum=epa * 0.4, points=21 + int(epa))
                )
    return rows


def _e2e_qb(season, weeks, starter_override=None):
    starter_override = starter_override or {}
    rows = []
    for w in weeks:
        for team in _E2E_TEAMS:
            starter = starter_override.get(team, f"{team}_QB1")
            rows.append((starter, team, season, w, "REG", 35))
            if starter != f"{team}_QB1":
                rows.append((f"{team}_QB1", team, season, w, "REG", 3))
    return rows


def _e2e_ol(season, weeks, group_override=None, snaps=60):
    group_override = group_override or {}
    rows = []
    for w in weeks:
        for team in _E2E_TEAMS:
            prefix = group_override.get(team, f"{team}_OL")
            for i in range(5):
                rows.append((f"{prefix}{i}", team, season, w, "REG", "T", snaps))
    return rows


def _run_compute_with(monkeypatch, *, week, team_week, qb, ol, depth=()):
    import pipeline.analysts.efficiency as eff

    tw_df = pl.DataFrame(team_week, schema=eff._TEAM_WEEK_SCHEMA)
    qb_df = pl.DataFrame(qb, schema=eff._PLAYER_WEEK_QB_SCHEMA, orient="row")
    ol_df = pl.DataFrame(ol, schema=eff._SNAPS_OL_SCHEMA, orient="row")
    depth_df = pl.DataFrame(list(depth), schema=eff._DEPTH_SCHEMA, orient="row")
    monkeypatch.setattr(eff, "_fetch_team_week", lambda conn, s, p: tw_df)
    monkeypatch.setattr(eff, "_fetch_player_week_qb", lambda conn, s, p: qb_df)
    monkeypatch.setattr(eff, "_fetch_snaps_ol", lambda conn, s, p: ol_df)
    monkeypatch.setattr(eff, "_fetch_depth", lambda conn: depth_df)
    monkeypatch.setattr(eff, "_build_inputs_version", lambda conn: "test")
    # A historical backfill week is never the live week, so the real function returns
    # False there. Stubbed only to avoid its nflreadpy date lookup, not to change behavior.
    monkeypatch.setattr(eff, "_depth_fallback_allowed", lambda ctx: False)

    analyst = eff.EfficiencyAnalyst()
    df = analyst.compute(_make_ctx(season=2099, week=week))
    return df.drop("as_of").sort(["signal", "team"]), analyst._last_run_meta


def test_compute_ignores_future_weeks_end_to_end(monkeypatch):
    """The full compute() path, not just _filter_current_season: team_week, the QB-change
    factor, and the O-line-continuity factor must all ignore weeks >= the target week.
    This is the guarantee the 2019-2025 backfill (P5) rests on: a week-N signal may only
    see games from weeks 1..N-1, or the backtest is worthless."""
    prior_tw = _e2e_team_week(2098, range(1, 7))
    prior_qb = _e2e_qb(2098, range(1, 7))
    prior_ol = _e2e_ol(2098, range(1, 7))

    past_tw = _e2e_team_week(2099, range(1, 4))
    past_qb = _e2e_qb(2099, range(1, 4))
    past_ol = _e2e_ol(2099, range(1, 4))

    # Planted future (weeks 4-6): a large EPA swing, a brand-new AA starting QB with no
    # prior attempts (would drop AA's qb_factor to 0.6 if leaked), and an entirely new AA
    # O-line with far more snaps (would take over the top-5 group if leaked).
    future_tw = _e2e_team_week(2099, range(4, 7), epa_shift=0.5)
    future_qb = _e2e_qb(2099, range(4, 7), starter_override={"AA": "AA_QB2"})
    future_ol = _e2e_ol(2099, range(4, 7), group_override={"AA": "AA_NEWOL"}, snaps=500)

    without_future = _run_compute_with(
        monkeypatch, week=4,
        team_week=prior_tw + past_tw, qb=prior_qb + past_qb, ol=prior_ol + past_ol,
    )
    with_future = _run_compute_with(
        monkeypatch, week=4,
        team_week=prior_tw + past_tw + future_tw,
        qb=prior_qb + past_qb + future_qb,
        ol=prior_ol + past_ol + future_ol,
    )

    # Not bit-exact: a larger input frame changes polars' internal summation order, which
    # moves values by ~1 ULP (measured 7e-18 on values ~0.02). atol=1e-12 is still ~11
    # orders of magnitude below what the planted 0.5-EPA future shift would produce.
    assert_frame_equal(
        without_future[0], with_future[0], check_exact=False, rel_tol=0, abs_tol=1e-12
    )
    assert without_future[1] == with_future[1]
    assert with_future[1]["teams"]["AA"]["current_qb"] == "AA_QB1"

    # Control: the planted rows are material. At week 7 they're legitimately in scope and
    # change both values and AA's resolved QB, so the equality above isn't vacuous.
    week7_past_only = _run_compute_with(
        monkeypatch, week=7,
        team_week=prior_tw + past_tw, qb=prior_qb + past_qb, ol=prior_ol + past_ol,
    )
    week7_with_future = _run_compute_with(
        monkeypatch, week=7,
        team_week=prior_tw + past_tw + future_tw,
        qb=prior_qb + past_qb + future_qb,
        ol=prior_ol + past_ol + future_ol,
    )
    assert not week7_past_only[0].equals(week7_with_future[0])
    assert week7_with_future[1]["teams"]["AA"]["current_qb"] == "AA_QB2"


def test_historical_week_one_never_reads_depth(monkeypatch):
    """Disclosed P5 backtest asymmetry: a historical week 1 has no current-season games,
    and depth holds only today's snapshot, so the current QB/O-line resolve to unknown
    (qb_factor = ol_factor = 1.0), never to today's depth chart."""
    depth = [("AA", "QB", 1, "TODAYS_QB")] + [
        ("AA", slot, 1, f"TODAYS_{slot}") for slot in ("LT", "LG", "C", "RG", "RT")
    ]
    _, meta = _run_compute_with(
        monkeypatch, week=1,
        team_week=_e2e_team_week(2098, range(1, 7)) + _e2e_team_week(2099, range(1, 4)),
        qb=_e2e_qb(2098, range(1, 7)),
        ol=_e2e_ol(2098, range(1, 7)),
        depth=depth,
    )
    assert meta["teams"]["AA"]["current_qb"] is None
    assert meta["teams"]["AA"]["current_ol"] == []
    assert meta["teams"]["AA"]["qb_factor"] == 1.0
    assert meta["teams"]["AA"]["ol_factor"] == 1.0


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


def test_qb_change_factor_full_season_starter_no_discount():
    # Team's only QB last season, same starter this season -- full continuity share.
    factor = _qb_change_factor("QB1", current_qb_prior_attempts=500, team_prior_total_attempts=500)
    assert factor == 1.0


def test_qb_change_factor_fifty_percent_injury_season_earns_full_continuity():
    # min(1, share/0.5) already reaches full continuity at exactly a 50% share -- a
    # starter who missed half the season to injury isn't penalized like a new starter.
    factor = _qb_change_factor("QB1", current_qb_prior_attempts=250, team_prior_total_attempts=500)
    assert factor == 1.0
    # Below 50% share the discount partially applies (e.g. a 30% share).
    factor = _qb_change_factor("QB1", current_qb_prior_attempts=150, team_prior_total_attempts=500)
    expected_continuity = min(1.0, (150 / 500) / 0.5)
    assert factor == pytest.approx(1.0 - (1.0 - QB_CHANGE_DISCOUNT) * (1.0 - expected_continuity))


def test_qb_change_factor_traded_veteran_gets_full_credit_from_other_team():
    # QB1 threw 400 attempts for a DIFFERENT team last season; the current team's own
    # prior-season total was only 300 (a different backup led it). Share is capped at 1
    # rather than crediting the veteran beyond the current team's whole QB room.
    factor = _qb_change_factor("QB1", current_qb_prior_attempts=400, team_prior_total_attempts=300)
    assert factor == 1.0


def test_qb_change_factor_rookie_with_zero_prior_attempts_gets_full_discount():
    # Brand-new starter, zero prior-season attempts anywhere -- share=0, continuity=0,
    # reduces to the same floor the old binary "different starter" case used.
    factor = _qb_change_factor("QB1", current_qb_prior_attempts=0, team_prior_total_attempts=500)
    assert factor == pytest.approx(QB_CHANGE_DISCOUNT)


def test_qb_change_factor_unknown_starter_or_no_team_data_defaults_to_no_discount():
    factor = _qb_change_factor(None, current_qb_prior_attempts=0, team_prior_total_attempts=500)
    assert factor == 1.0
    factor = _qb_change_factor("QB1", current_qb_prior_attempts=0, team_prior_total_attempts=0)
    assert factor == 1.0


def test_prior_season_team_total_attempts_sums_all_qbs_on_team():
    prior_pw = pl.DataFrame(
        {
            "player_id": ["QB1", "QB2", "QB3"],
            "team": ["KC", "KC", "BUF"],
            "attempts": [400, 100, 300],
        }
    )
    assert _prior_season_team_total_attempts(prior_pw, "KC") == 500.0
    assert _prior_season_team_total_attempts(prior_pw, "BUF") == 300.0
    assert _prior_season_team_total_attempts(prior_pw, "MIA") == 0.0


def test_prior_season_qb_attempts_sums_across_every_team_the_qb_played_for():
    # QB1 was traded mid-season: some attempts for KC, the rest for BUF after the trade.
    prior_pw = pl.DataFrame(
        {
            "player_id": ["QB1", "QB1", "QB2"],
            "team": ["KC", "BUF", "KC"],
            "attempts": [200, 200, 100],
        }
    )
    assert _prior_season_qb_attempts(prior_pw, "QB1") == 400.0
    assert _prior_season_qb_attempts(prior_pw, "QB2") == 100.0
    assert _prior_season_qb_attempts(prior_pw, "QB3") == 0.0


def test_ol_continuity_factor_full_vs_no_overlap():
    group = {"a", "b", "c", "d", "e"}
    assert _ol_continuity_factor(group, group) == 1.0
    disjoint = {"f", "g", "h", "i", "j"}
    assert _ol_continuity_factor(group, disjoint) == OL_MIN_FACTOR


def test_ol_snap_positions_includes_generic_ol_tag():
    """Regression guard: ARI/CHI/JAX/LA report 100% of their O-line snaps under the
    generic "OL" position in snap_counts, with zero C/G/T rows at all (verified live,
    docs/sources.md). Dropping "OL" from this set silently empties _ol_group for those
    teams every time they're the *prior*-season side."""
    assert _OL_SNAP_POSITIONS == {"C", "G", "T", "OL"}


def test_ol_group_builds_from_generic_ol_position_only():
    """A team whose snap_counts rows are entirely position="OL" (no C/G/T at all) must
    still yield a 5-player group -- this is the exact shape that produced "OL-continuity
    discount skipped (unknown group) -- current=... prior=set()" for ARI/CHI/JAX/LA."""
    snaps_df = pl.DataFrame(
        {
            "player_id": ["p1", "p2", "p3", "p4", "p5", "p6"],
            "team": ["ARI"] * 6,
            "offense_snaps": [60, 58, 55, 50, 45, 10],
        }
    ).filter(pl.col("team").is_in({"ARI"}))
    # Simulates what _fetch_snaps_ol's SQL now returns once _OL_SNAP_POSITIONS includes
    # "OL": every row already passed the position filter, so _ol_group only groups/sorts.
    group = _ol_group(snaps_df, "ARI")
    assert group == {"p1", "p2", "p3", "p4", "p5"}


def test_ol_group_empty_when_team_has_no_snaps_rows():
    snaps_df = pl.DataFrame(
        {"player_id": ["p1"], "team": ["KC"], "offense_snaps": [60]}
    )
    assert _ol_group(snaps_df, "ARI") == set()


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


def test_reliability_scales_prior_weight_independently_for_offense_and_defense():
    current = pl.DataFrame([_tw_row("AA", "BB", 1), _tw_row("BB", "AA", 1)])
    prior = pl.DataFrame([_tw_row("AA", "BB", 1), _tw_row("BB", "AA", 1)])
    teams = ["AA", "BB"]

    def _stability_for(reliability_off: float, reliability_def: float, side: str) -> float:
        metric = MetricConfig(
            "epa_per_play",
            "epa_sum",
            "plays",
            200,
            0.5,
            0.5,
            reliability_off=reliability_off,
            reliability_def=reliability_def,
        )
        rows = _build_metric_signal_rows(
            metric=metric,
            current_tw=current,
            prior_tw=prior,
            teams=teams,
            offense_discount={"AA": 1.0, "BB": 1.0},
            season=2099,
            week=2,
            as_of=datetime(2099, 1, 1, tzinfo=UTC),
            inputs_version="test",
        )
        return next(
            r["stability"] for r in rows if r["team"] == "AA" and r["signal"].endswith(f"_{side}")
        )

    # Dropping reliability_off to 0 shrinks w_prior (handing that weight to the league
    # average, never to w_cur -- same rule QB/OL discounts already follow), so offense
    # stability drops. Defense is untouched since its own reliability stayed at 1.0.
    assert _stability_for(0.0, 1.0, "off") < _stability_for(1.0, 1.0, "off")
    # Same check for the defense side, which had no reliability concept at all before
    # this change (prior_discount was hardcoded to 1.0).
    assert _stability_for(1.0, 0.0, "def") < _stability_for(1.0, 1.0, "def")


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
