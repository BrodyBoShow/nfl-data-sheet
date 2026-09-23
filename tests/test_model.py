"""P5 projection model (pipeline/synthesis/model.py) and the offline history helpers
(scripts/projection_history.py). Synthetic frames only -- no DB, no live calls."""

from __future__ import annotations

import numpy as np
import polars as pl
import pytest

from pipeline.synthesis import model
from pipeline.synthesis.model import (
    HFA_ONLY_SPEC,
    PRIMARY_SPEC,
    build_game_frame,
    efficiency_fingerprint,
    fit,
    predict,
    stability_bucket,
    stability_cutpoints,
    to_team_rows,
    walk_forward,
    with_stability_bucket,
)
from scripts.projection_history import (
    add_outcomes,
    bootstrap_corr,
    check_fit_seasons,
    edge_validation,
)

_TEAMS = ["AAA", "BBB", "CCC", "DDD", "EEE", "FFF", "GGG", "HHH"]
_TRUE = {"alpha": 22.0, "beta_off:epa_per_play": 40.0, "beta_def:epa_per_play": 30.0,
         "gamma": 2.0}


def _signals(season: int, week: int, values: dict[str, tuple[float, float]],
             stability: float = 0.5) -> list[dict]:
    rows = []
    for team, (off, def_) in values.items():
        for signal, v in (("epa_per_play_off", off), ("epa_per_play_def", def_)):
            rows.append({"season": season, "week": week, "team": team, "signal": signal,
                         "value": v, "stability": stability})
    return rows


def _synthetic(seasons: list[int], weeks: int = 6, noise: float = 0.0, seed: int = 1,
               neutral_every: int = 0) -> tuple[pl.DataFrame, pl.DataFrame]:
    """Games whose scores follow the model exactly (plus optional noise)."""
    rng = np.random.default_rng(seed)
    game_rows, sig_rows = [], []
    for season in seasons:
        for week in range(1, weeks + 1):
            vals = {t: (float(rng.normal(0, 0.1)), float(rng.normal(0, 0.1))) for t in _TEAMS}
            sig_rows += _signals(season, week, vals, stability=0.2 + 0.1 * week)
            mean_off = float(np.mean([v[0] for v in vals.values()]))
            mean_def = float(np.mean([v[1] for v in vals.values()]))
            order = list(rng.permutation(_TEAMS))
            for i in range(0, len(order), 2):
                home, away = order[i], order[i + 1]
                neutral = neutral_every and (len(game_rows) % neutral_every == 0)
                h = 0.0 if neutral else 0.5
                pts = [
                    _TRUE["alpha"]
                    + _TRUE["beta_off:epa_per_play"] * (vals[own][0] - mean_off)
                    + _TRUE["beta_def:epa_per_play"] * (vals[opp][1] - mean_def)
                    + _TRUE["gamma"] * hh + float(rng.normal(0, noise))
                    for own, opp, hh in ((home, away, h), (away, home, -h))
                ]
                game_rows.append({
                    "game_id": f"{season}_{week:02d}_{away}_{home}", "season": season,
                    "week": week, "home_team": home, "away_team": away,
                    "location": "Neutral" if neutral else "Home",
                    "home_score": pts[0], "away_score": pts[1],
                    "spread_line": 0.0, "total_line": 44.0,
                })
    return pl.DataFrame(game_rows), pl.DataFrame(sig_rows)


def test_fit_recovers_known_coefficients():
    games, signals = _synthetic([2019, 2020], neutral_every=5)
    frame = build_game_frame(games, signals, PRIMARY_SPEC)
    result = fit(to_team_rows(frame, PRIMARY_SPEC), PRIMARY_SPEC)
    for name, true in _TRUE.items():
        assert result.coef[name] == pytest.approx(true, abs=1e-8)
    assert result.n_games == games.height
    assert result.n_rows == 2 * games.height


def test_fit_with_noise_is_close_and_has_finite_clustered_se():
    games, signals = _synthetic([2019, 2020, 2021], weeks=17, noise=10.0, neutral_every=7)
    frame = build_game_frame(games, signals, PRIMARY_SPEC)
    result = fit(to_team_rows(frame, PRIMARY_SPEC), PRIMARY_SPEC)
    assert result.coef["alpha"] == pytest.approx(22.0, abs=1.5)
    assert all(np.isfinite(v) and v > 0 for v in result.se.values())


def test_clustered_se_does_not_shrink_when_rows_are_duplicated_within_game():
    """Duplicating every row inside its own game adds no information; clustered SEs
    must stay (almost) put, whereas naive OLS SEs would fall by ~1/sqrt(2)."""
    games, signals = _synthetic([2019, 2020], weeks=10, noise=8.0, neutral_every=6)
    rows = to_team_rows(build_game_frame(games, signals, PRIMARY_SPEC), PRIMARY_SPEC)
    once = fit(rows, PRIMARY_SPEC)
    twice = fit(pl.concat([rows, rows]), PRIMARY_SPEC)
    for name in once.coef:
        assert twice.coef[name] == pytest.approx(once.coef[name], abs=1e-9)
        assert twice.se[name] == pytest.approx(once.se[name], rel=0.02)


def test_neutral_site_drops_gamma_from_margin():
    games, signals = _synthetic([2019], weeks=1)
    games = games.with_columns(pl.lit("Neutral").alias("location"))
    frame = build_game_frame(games, signals, PRIMARY_SPEC)
    a = predict({**_TRUE, "gamma": 0.0}, frame, PRIMARY_SPEC)
    b = predict({**_TRUE, "gamma": 9.0}, frame, PRIMARY_SPEC)
    assert (frame["h_home"] == 0.0).all()
    assert a["margin_home"].to_list() == pytest.approx(b["margin_home"].to_list())
    assert a["projected_total"].to_list() == pytest.approx(b["projected_total"].to_list())


def test_hfa_sign_and_spread_convention():
    """Identical teams at a home venue: margin = +gamma, the home team is favored, so
    projected_spread_home = -gamma (market convention, negative = home favored). gamma
    cancels out of the total."""
    games = pl.DataFrame([{"game_id": "2019_01_BBB_AAA", "season": 2019, "week": 1,
                           "home_team": "AAA", "away_team": "BBB", "location": "Home"}])
    signals = pl.DataFrame(_signals(2019, 1, {"AAA": (0.05, -0.02), "BBB": (0.05, -0.02)}))
    out = predict(_TRUE, build_game_frame(games, signals, PRIMARY_SPEC), PRIMARY_SPEC)
    assert out["margin_home"][0] == pytest.approx(_TRUE["gamma"])
    assert out["projected_spread_home"][0] == pytest.approx(-_TRUE["gamma"])
    assert out["projected_total"][0] == pytest.approx(2 * _TRUE["alpha"])


def test_features_are_centered_on_the_weeks_mean():
    games = pl.DataFrame([{"game_id": "2019_01_BBB_AAA", "season": 2019, "week": 1,
                           "home_team": "AAA", "away_team": "BBB", "location": "Home"}])
    signals = pl.DataFrame(_signals(2019, 1, {"AAA": (0.3, 0.1), "BBB": (0.1, -0.1)}))
    frame = build_game_frame(games, signals, PRIMARY_SPEC)
    assert frame["home_off__epa_per_play"][0] == pytest.approx(0.1)
    assert frame["away_def__epa_per_play"][0] == pytest.approx(-0.1)
    assert frame["home_off_raw__epa_per_play"][0] == pytest.approx(0.3)


def test_oak_in_games_joins_lv_signals():
    games = pl.DataFrame([{"game_id": "2019_01_DEN_OAK", "season": 2019, "week": 1,
                           "home_team": "OAK", "away_team": "DEN", "location": "Home"}])
    signals = pl.DataFrame(_signals(2019, 1, {"LV": (0.1, 0.0), "DEN": (0.0, 0.1)}))
    frame = build_game_frame(games, signals, PRIMARY_SPEC)
    assert frame["features_complete"][0]
    assert frame["home_team"][0] == "OAK"  # identity column untouched


def test_missing_signal_leaves_nulls_never_fills():
    games = pl.DataFrame([{"game_id": "2019_01_BBB_AAA", "season": 2019, "week": 1,
                           "home_team": "AAA", "away_team": "BBB", "location": "Home"}])
    signals = pl.DataFrame(_signals(2019, 1, {"AAA": (0.1, 0.0)}))  # BBB missing
    frame = build_game_frame(games, signals, PRIMARY_SPEC)
    out = predict(_TRUE, frame, PRIMARY_SPEC)
    assert not frame["features_complete"][0]
    assert frame["stability_min"][0] is None
    assert out["projected_spread_home"][0] is None


def test_stability_min_is_the_weakest_input():
    games = pl.DataFrame([{"game_id": "2019_01_BBB_AAA", "season": 2019, "week": 1,
                           "home_team": "AAA", "away_team": "BBB", "location": "Home"}])
    rows = _signals(2019, 1, {"AAA": (0.1, 0.0), "BBB": (0.0, 0.1)}, stability=0.8)
    rows[3]["stability"] = 0.3  # BBB's defense
    frame = build_game_frame(games, pl.DataFrame(rows), PRIMARY_SPEC)
    assert frame["stability_min"][0] == pytest.approx(0.3)


def test_missing_location_raises():
    games = pl.DataFrame([{"game_id": "g", "season": 2019, "week": 1, "home_team": "AAA",
                           "away_team": "BBB", "location": None}],
                         schema_overrides={"location": pl.Utf8})
    with pytest.raises(ValueError, match="location"):
        build_game_frame(games, pl.DataFrame(_signals(2019, 1, {})), PRIMARY_SPEC)


def test_walk_forward_never_trains_on_the_test_season():
    games, signals = _synthetic([2019, 2020, 2021], noise=5.0)
    frame = build_game_frame(games, signals, PRIMARY_SPEC)
    pred, fits = walk_forward(frame, PRIMARY_SPEC, [2020, 2021])
    # Scrambling 2021's scores must not change 2021's predictions.
    scrambled = frame.with_columns(
        pl.when(pl.col("season") == 2021).then(pl.col("home_score") + 50)
        .otherwise(pl.col("home_score")).alias("home_score")
    )
    pred2, _ = walk_forward(scrambled, PRIMARY_SPEC, [2020, 2021])
    assert pred["projected_spread_home"].to_list() == pred2["projected_spread_home"].to_list()
    assert fits[2020].n_games == games.filter(pl.col("season") == 2019).height
    assert set(pred.filter(pl.col("season") == 2021)["train_through"].to_list()) == {2020}


def test_walk_forward_refuses_a_first_season_test():
    games, signals = _synthetic([2019, 2020])
    frame = build_game_frame(games, signals, PRIMARY_SPEC)
    with pytest.raises(ValueError, match="no training seasons"):
        walk_forward(frame, PRIMARY_SPEC, [2019])


def test_hfa_only_spec_builds_and_fits():
    games, signals = _synthetic([2019, 2020], neutral_every=4)
    frame = build_game_frame(games, signals, HFA_ONLY_SPEC)
    assert frame["features_complete"].all()
    result = fit(to_team_rows(frame, HFA_ONLY_SPEC), HFA_ONLY_SPEC)
    assert set(result.coef) == {"alpha", "gamma"}


def test_stability_buckets():
    cut = stability_cutpoints([0.1, 0.2, 0.3, 0.4, 0.5, 0.6])
    assert cut[0] < cut[1]
    assert stability_bucket(0.05, cut) == "low"
    assert stability_bucket(cut[0], cut) == "mid"
    assert stability_bucket(cut[1], cut) == "high"
    frame = with_stability_bucket(pl.DataFrame({"stability_min": [0.05, 0.35, 0.9, None]}), cut)
    assert frame["stability_bucket"].to_list() == ["low", "mid", "high", None]


def test_fingerprint_changes_with_efficiency_config(monkeypatch):
    before = efficiency_fingerprint(PRIMARY_SPEC)
    original = model._METRIC_CONFIG
    patched = [m._replace(reliability_off=0.99) if m.name == "epa_per_play" else m
               for m in original]
    monkeypatch.setattr(model, "_METRIC_CONFIG", patched)
    assert efficiency_fingerprint(PRIMARY_SPEC) != before
    monkeypatch.setattr(model, "_METRIC_CONFIG", original)
    assert efficiency_fingerprint(PRIMARY_SPEC) == before
    monkeypatch.setattr(model, "QB_CHANGE_DISCOUNT", 0.5)
    assert efficiency_fingerprint(PRIMARY_SPEC) != before
    with pytest.raises(ValueError, match="no efficiency MetricConfig"):
        efficiency_fingerprint(model.ModelSpec("x", ("not_a_metric",)))


# --- scripts/projection_history.py ---------------------------------------------------


def test_add_outcomes_negates_home_positive_spread_line():
    """nflverse spread_line +3 = home favored by 3 = market -3. A model spread of -5
    likes home 2 points more than the close: edge_spread = -2."""
    pred = pl.DataFrame({
        "home_score": [27], "away_score": [20], "spread_line": [3.0], "total_line": [45.0],
        "margin_home": [5.0], "projected_spread_home": [-5.0], "projected_total": [47.0],
    })
    out = add_outcomes(pred)
    assert out["closing_spread_home"][0] == -3.0
    assert out["edge_spread"][0] == pytest.approx(-2.0)
    assert out["ats_margin_home"][0] == pytest.approx(4.0)  # won by 7, laid 3
    assert out["edge_total"][0] == pytest.approx(2.0)
    assert out["ou_margin"][0] == pytest.approx(2.0)
    assert out["margin_resid"][0] == pytest.approx(2.0)


def test_edge_validation_sign():
    rng = np.random.default_rng(0)
    edge = rng.normal(0, 3, 400)
    frame = pl.DataFrame({
        "edge_spread": edge,
        "ats_margin_home": -edge + rng.normal(0, 1, 400),  # -edge predicts covers
        "edge_total": edge,
        "ou_margin": rng.normal(0, 1, 400),  # unrelated
    })
    ev = edge_validation(frame)
    assert ev["spread"]["validated"]
    assert ev["spread"]["corr"] > 0.9
    assert not ev["total"]["validated"]


def test_bootstrap_corr_is_reproducible():
    x = np.arange(50, dtype=float)
    y = x + np.sin(x) * 10
    a, b = bootstrap_corr(x, y), bootstrap_corr(x, y)
    assert a == b
    assert a["ci_low"] <= a["corr"] <= a["ci_high"]


def test_check_fit_seasons_refuses_current_and_unscored():
    games = pl.DataFrame({"season": [2024, 2025], "home_score": [20, None],
                          "away_score": [17, None]})
    with pytest.raises(ValueError, match="current season"):
        check_fit_seasons([2024, 2026], 2026, games)
    with pytest.raises(ValueError, match="unscored"):
        check_fit_seasons([2024, 2025], 2026, games)
    with pytest.raises(ValueError, match="no REG games"):
        check_fit_seasons([2023, 2024], 2026, games)
    check_fit_seasons([2024], 2026, games)
