"""
Job: Define the P5 projection model -- build per-game features from efficiency signals,
     fit the points regression, predict spread/total, and bucket games by input
     stability. Pure functions only: no DB access, no I/O.
Reads: nothing (callers pass `games` identity rows and `signals` rows as frames)
Writes: nothing
Tier: n/a
Phase: P5

The model (pre-registered in docs/phases/P5.md, fixed before any backtest output):

    pts_i = alpha + beta_off * (off_i - mean_off) + beta_def * (def_j - mean_def)
            + gamma * h_i + e

- One row per team per game (2 per game). `off_i` is team i's `<base>_off` signal and
  `def_j` its opponent's `<base>_def` signal (allowed, so higher = worse defense), both
  at the game's own (season, week) -- i.e. "entering this week", point-in-time.
- `mean_off`/`mean_def`: unweighted mean of that signal over every team with a value at
  that (season, week), so features are centered week by week.
- `h_i` = +0.5 home, -0.5 away, 0 for both at a neutral site (`games.location`). With
  this coding gamma is home-field advantage in points of margin and cancels out of the
  total.
- Target: actual points scored, never the closing line.

Outputs: margin = pts_home - pts_away, projected_spread_home = -margin (market
convention, negative = home favored), projected_total = pts_home + pts_away.

The synthesizer, scripts/fit_projection_model.py and scripts/backtest.py all import
this module, so there is exactly one implementation of the features, fit and prediction.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import polars as pl

from pipeline.analysts.efficiency import _METRIC_CONFIG, OL_MIN_FACTOR, QB_CHANGE_DISCOUNT
from pipeline.core.definitions import EFFICIENCY_DEFINITION, TEAM_INPUTS_DEFINITION
from pipeline.core.team_aliases import normalize_team_abbr

# p5-v2 (2026-09-30): the re-fit on signals rebuilt with try plays excluded and the QB/OL
# discounts firing, and the first version whose model file carries `fit_inputs_hash`.
MODEL_VERSION = "p5-v2"

# fit_inputs_hash rounding. A re-backfill that reproduces the same signals differs by
# ~5e-16 in `value` (the backtest's parity check), so that mustn't move the hash. Measured
# 2026-09-30 against a 124-week production recompute: 0 of 7,936 rows differ at 9 decimals.
_FIT_HASH_VALUE_DECIMALS = 9

# h for the home side at a home venue; the away side gets -HOME_H, both get 0 at a
# neutral site. See the module docstring for why 0.5 (gamma = HFA in points of margin).
HOME_H = 0.5

STABILITY_BUCKETS = ("low", "mid", "high")


@dataclass(frozen=True)
class ModelSpec:
    """Which efficiency bases feed the regression. Each base contributes one offense
    slope (`<base>_off` of the team) and one defense slope (`<base>_def` of the
    opponent) -- the same `_off`/`_def` suffix pairing the Efficiency analyst writes."""

    name: str
    bases: tuple[str, ...]

    @property
    def signal_names(self) -> tuple[str, ...]:
        return tuple(f"{b}_{unit}" for b in self.bases for unit in ("off", "def"))

    @property
    def coef_names(self) -> tuple[str, ...]:
        return (
            "alpha",
            *(f"beta_off:{b}" for b in self.bases),
            *(f"beta_def:{b}" for b in self.bases),
            "gamma",
        )


# The pre-registered production model.
PRIMARY_SPEC = ModelSpec("epa_per_play", ("epa_per_play",))
# Benchmark: intercept plus home-field term only.
HFA_ONLY_SPEC = ModelSpec("hfa_only", ())


def efficiency_fingerprint(spec: ModelSpec) -> str:
    """Hash of how this spec's features are defined, from code alone:
    - each base's full MetricConfig (k, sensitivities, reliability r);
    - the QB/OL discount constants;
    - the definition labels of the code that builds and computes them
      (`pipeline/core/definitions.py`, pinned to that source by tests/test_definitions.py).

    If any of these changes, live features no longer sit on the scale the model was fit
    on, and the synthesizer must refuse to project until a re-backfill + refit. It moves
    the moment such code is deployed, before any re-backfill.

    It can't see data-only changes: newly staged seasons, a re-backfill, upstream
    revisions. `fit_inputs_hash` covers those."""
    configs = {m.name: m._asdict() for m in _METRIC_CONFIG if m.name in spec.bases}
    missing = sorted(set(spec.bases) - set(configs))
    if missing:
        raise ValueError(f"no efficiency MetricConfig for bases {missing}")
    payload = {
        "metrics": configs,
        "QB_CHANGE_DISCOUNT": QB_CHANGE_DISCOUNT,
        "OL_MIN_FACTOR": OL_MIN_FACTOR,
        "TEAM_INPUTS_DEFINITION": TEAM_INPUTS_DEFINITION,
        "EFFICIENCY_DEFINITION": EFFICIENCY_DEFINITION,
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode("utf-8")).hexdigest()[:16]


def fit_inputs_hash(signals: pl.DataFrame, spec: ModelSpec, seasons: Sequence[int]) -> str:
    """Hash of the exact stored feature signals a fit reads: every `spec.signal_names`
    row in `seasons`, as (season, week, team, signal, value).

    `stability` is left out on purpose. It's a deterministic function of the same sample
    sizes, k and r as `value`, so a real input change moves `value` too, and k/r are in
    `efficiency_fingerprint`. And it can't be hashed robustly: it reads back from the
    float4 column at ~6 significant digits, up to 5.3e-7 from a fresh compute. Measured
    2026-09-30: 7,003 of 7,936 rows differ from a production recompute even at float4
    precision, so any rounding grid flips somewhere across ~7.9k rows.

    The fit script writes it to the model file. The synthesizer recomputes it from
    `signals` every run and refuses to project on a mismatch. So any change to the fit
    window's stored signals after the fit makes the model stale, whatever caused it:
    a re-backfill after a definition change, newly staged inputs switching the QB/OL
    discounts on, or r/k changes.

    Rounded (see _FIT_HASH_*) so a re-backfill that reproduces the same values doesn't
    move it. Row order doesn't matter. Extra rows outside the spec's signals or
    `seasons` are ignored, so callers can pass a wider frame."""
    rows = (
        signals.filter(
            pl.col("signal").is_in(list(spec.signal_names))
            & pl.col("season").is_in(list(seasons))
        )
        .select("season", "week", "team", "signal", "value")
        .sort("season", "week", "team", "signal")
    )

    def _value(v: float | None) -> str:
        if v is None:
            return "null"
        return f"{round(float(v), _FIT_HASH_VALUE_DECIMALS):.{_FIT_HASH_VALUE_DECIMALS}f}"

    digest = hashlib.sha256()
    for season, week, team, signal, value in rows.iter_rows():
        digest.update(f"{season}|{week}|{team}|{signal}|{_value(value)}\n".encode())
    return digest.hexdigest()[:16]


# --------------------------------------------------------------------------------------
# Features
# --------------------------------------------------------------------------------------


def _feature_col(side: str, unit: str, base: str) -> str:
    """Centered value, e.g. `home_off__epa_per_play`."""
    return f"{side}_{unit}__{base}"


def feature_cols(spec: ModelSpec) -> list[str]:
    return [
        _feature_col(side, unit, b)
        for side in ("home", "away")
        for b in spec.bases
        for unit in ("off", "def")
    ]


def build_game_frame(
    games: pl.DataFrame, signals: pl.DataFrame, spec: ModelSpec
) -> pl.DataFrame:
    """One row per game with both sides' features.

    `games`: game_id, season, week, home_team, away_team, location; any other columns
    are carried through untouched. Team codes are normalized with `normalize_team_abbr`
    before joining (games keeps raw `OAK` for 2018-19; signals hold `LV`).
    `signals`: season, week, team, signal, value, stability -- efficiency, team scope.

    Adds per side/unit/base: the centered feature (`home_off__<b>`), the raw value
    (`home_off_raw__<b>`) and its stability (`home_off_stab__<b>`); plus `h_home`,
    `stability_min` (min over every input stability -- the weakest input bounds trust)
    and `features_complete`. A game missing any input keeps nulls; it's never filled.
    """
    location = games["location"]
    if location.null_count() or not location.is_in(["Home", "Neutral"]).all():
        raise ValueError("every game needs location 'Home' or 'Neutral' (games.location)")

    sig = signals.filter(pl.col("signal").is_in(spec.signal_names)).select(
        "season", "week", "team", "signal", "value", pl.col("stability").cast(pl.Float64)
    )
    week_means = (
        sig.filter(pl.col("value").is_not_null())
        .group_by("season", "week", "signal")
        .agg(pl.col("value").mean().alias("week_mean"))
    )
    sig = sig.join(week_means, on=["season", "week", "signal"], how="left").with_columns(
        (pl.col("value") - pl.col("week_mean")).alias("centered")
    )

    out = games.with_columns(
        pl.col("home_team")
        .map_elements(normalize_team_abbr, return_dtype=pl.Utf8)
        .alias("_home_norm"),
        pl.col("away_team")
        .map_elements(normalize_team_abbr, return_dtype=pl.Utf8)
        .alias("_away_norm"),
        pl.when(pl.col("location") == "Neutral")
        .then(0.0)
        .otherwise(HOME_H)
        .alias("h_home"),
    )

    stab_cols: list[str] = []
    for side, team_col in (("home", "_home_norm"), ("away", "_away_norm")):
        for b in spec.bases:
            for unit in ("off", "def"):
                col = _feature_col(side, unit, b)
                stab_cols.append(f"{side}_{unit}_stab__{b}")
                one = sig.filter(pl.col("signal") == f"{b}_{unit}").select(
                    "season",
                    "week",
                    pl.col("team").alias(team_col),
                    pl.col("centered").alias(col),
                    pl.col("value").alias(f"{side}_{unit}_raw__{b}"),
                    pl.col("stability").alias(f"{side}_{unit}_stab__{b}"),
                )
                out = out.join(one, on=["season", "week", team_col], how="left")

    if out.height != games.height:
        raise ValueError("duplicate efficiency signal rows for a (season, week, team, signal)")

    checks = [pl.col(c).is_not_null() for c in feature_cols(spec) + stab_cols]
    # A spec with no bases (the HFA-only benchmark) has no inputs to be missing.
    complete = pl.all_horizontal(checks) if checks else pl.lit(True)
    stability_min = (
        pl.min_horizontal([pl.col(c) for c in stab_cols]) if stab_cols else pl.lit(None)
    )
    return out.with_columns(
        complete.alias("features_complete"),
        pl.when(complete).then(stability_min).otherwise(None).cast(pl.Float64)
        .alias("stability_min"),
    ).drop("_home_norm", "_away_norm")


def to_team_rows(game_frame: pl.DataFrame, spec: ModelSpec) -> pl.DataFrame:
    """Two regression rows per complete game (fit only -- needs home_score/away_score):
    the home offense vs. the away defense, and the away offense vs. the home defense."""
    g = game_frame.filter(pl.col("features_complete"))
    home = g.select(
        "game_id",
        "season",
        "week",
        pl.col("h_home").alias("h"),
        *(pl.col(_feature_col("home", "off", b)).alias(f"x_off__{b}") for b in spec.bases),
        *(pl.col(_feature_col("away", "def", b)).alias(f"x_def__{b}") for b in spec.bases),
        pl.col("home_score").cast(pl.Float64).alias("pts"),
    )
    away = g.select(
        "game_id",
        "season",
        "week",
        (-pl.col("h_home")).alias("h"),
        *(pl.col(_feature_col("away", "off", b)).alias(f"x_off__{b}") for b in spec.bases),
        *(pl.col(_feature_col("home", "def", b)).alias(f"x_def__{b}") for b in spec.bases),
        pl.col("away_score").cast(pl.Float64).alias("pts"),
    )
    return pl.concat([home, away])


# --------------------------------------------------------------------------------------
# Fit and predict
# --------------------------------------------------------------------------------------


@dataclass(frozen=True)
class FitResult:
    spec_name: str
    coef: dict[str, float]
    se: dict[str, float]  # cluster-robust by game_id (CR1)
    n_games: int
    n_rows: int
    resid_sd: float  # team-points residual SD, df-corrected


def _design(team_rows: pl.DataFrame, spec: ModelSpec) -> np.ndarray:
    cols = [np.ones(team_rows.height)]
    cols += [team_rows[f"x_off__{b}"].to_numpy().astype(float) for b in spec.bases]
    cols += [team_rows[f"x_def__{b}"].to_numpy().astype(float) for b in spec.bases]
    cols.append(team_rows["h"].to_numpy().astype(float))
    return np.column_stack(cols)


def fit(team_rows: pl.DataFrame, spec: ModelSpec) -> FitResult:
    """OLS on team-game rows, standard errors clustered by game_id: a game's two rows
    share its pace/weather/officiating noise, so they aren't independent observations."""
    if team_rows["pts"].null_count():
        raise ValueError("fit rows contain a null score")
    x = _design(team_rows, spec)
    y = team_rows["pts"].to_numpy().astype(float)
    n, k = x.shape
    _, cluster = np.unique(team_rows["game_id"].to_numpy(), return_inverse=True)
    n_clusters = int(cluster.max()) + 1 if n else 0
    if n <= k or n_clusters < 2:
        raise ValueError(f"not enough rows to fit {k} coefficients (rows={n})")

    beta, *_ = np.linalg.lstsq(x, y, rcond=None)
    resid = y - x @ beta
    xtx_inv = np.linalg.inv(x.T @ x)
    cluster_scores = np.zeros((n_clusters, k))
    np.add.at(cluster_scores, cluster, x * resid[:, None])
    meat = cluster_scores.T @ cluster_scores
    correction = (n_clusters / (n_clusters - 1)) * ((n - 1) / (n - k))
    cov = correction * xtx_inv @ meat @ xtx_inv

    names = spec.coef_names
    return FitResult(
        spec_name=spec.name,
        coef={name: float(v) for name, v in zip(names, beta, strict=True)},
        se={name: float(v) for name, v in zip(names, np.sqrt(np.diag(cov)), strict=True)},
        n_games=n_clusters,
        n_rows=n,
        resid_sd=float(np.sqrt(resid @ resid / (n - k))),
    )


def predict(coef: Mapping[str, float], game_frame: pl.DataFrame, spec: ModelSpec) -> pl.DataFrame:
    """Adds pts_home, pts_away, margin_home, projected_spread_home, projected_total.
    Null wherever `features_complete` is false -- never a partial projection."""

    def pts(own: str, opp: str, h: pl.Expr) -> pl.Expr:
        expr = pl.lit(coef["alpha"]) + pl.lit(coef["gamma"]) * h
        for b in spec.bases:
            expr = expr + pl.lit(coef[f"beta_off:{b}"]) * pl.col(_feature_col(own, "off", b))
            expr = expr + pl.lit(coef[f"beta_def:{b}"]) * pl.col(_feature_col(opp, "def", b))
        return pl.when(pl.col("features_complete")).then(expr).otherwise(None)

    out = game_frame.with_columns(
        pts("home", "away", pl.col("h_home")).alias("pts_home"),
        pts("away", "home", -pl.col("h_home")).alias("pts_away"),
    )
    return out.with_columns(
        (pl.col("pts_home") - pl.col("pts_away")).alias("margin_home"),
    ).with_columns(
        (-pl.col("margin_home")).alias("projected_spread_home"),
        (pl.col("pts_home") + pl.col("pts_away")).alias("projected_total"),
    )


def walk_forward(
    game_frame: pl.DataFrame, spec: ModelSpec, test_seasons: Sequence[int]
) -> tuple[pl.DataFrame, dict[int, FitResult]]:
    """Expanding-window out-of-sample predictions: test season S uses coefficients fit
    only on the frame's seasons strictly before S. Returns the test games' predictions
    (complete games only, with `train_through` = last training season) and each fit."""
    seasons = sorted(set(game_frame["season"].to_list()))
    parts: list[pl.DataFrame] = []
    fits: dict[int, FitResult] = {}
    for test in test_seasons:
        train = [s for s in seasons if s < test]
        if not train:
            raise ValueError(f"no training seasons before {test}")
        assert max(train) < test  # never a season's own outcomes
        rows = to_team_rows(game_frame.filter(pl.col("season").is_in(train)), spec)
        fits[test] = fit(rows, spec)
        test_games = game_frame.filter(
            (pl.col("season") == test) & pl.col("features_complete")
        )
        parts.append(
            predict(fits[test].coef, test_games, spec).with_columns(
                pl.lit(max(train)).alias("train_through")
            )
        )
    return pl.concat(parts), fits


# --------------------------------------------------------------------------------------
# The committed coefficients file (written by scripts/fit_projection_model.py)
# --------------------------------------------------------------------------------------

COEFFICIENTS_PATH = Path(__file__).resolve().parent / "model_coefficients.json"


@dataclass(frozen=True)
class BucketCalibration:
    margin_sd: float | None
    total_sd: float | None
    edge_validated_spread: bool
    edge_validated_total: bool


@dataclass(frozen=True)
class ModelFile:
    model_version: str
    spec_name: str
    bases: tuple[str, ...]
    fit_seasons: tuple[int, ...]
    efficiency_fingerprint: str
    # None for a file written before p5-v2; the synthesizer treats that as stale.
    fit_inputs_hash: str | None
    coef: dict[str, float]
    stability_cutpoints: tuple[float, float]
    buckets: dict[str, BucketCalibration]


def load_model_file(path: Path = COEFFICIENTS_PATH) -> ModelFile:
    data = json.loads(path.read_text(encoding="utf-8"))
    cal = data["calibration"]
    c1, c2 = cal["stability_cutpoints"]
    return ModelFile(
        model_version=data["model_version"],
        spec_name=data["spec"]["name"],
        bases=tuple(data["spec"]["bases"]),
        fit_seasons=tuple(data["fit_seasons"]),
        efficiency_fingerprint=data["efficiency_fingerprint"],
        fit_inputs_hash=data.get("fit_inputs_hash"),
        coef={name: float(c["value"]) for name, c in data["coefficients"].items()},
        stability_cutpoints=(float(c1), float(c2)),
        buckets={
            name: BucketCalibration(
                margin_sd=b["margin_sd"],
                total_sd=b["total_sd"],
                edge_validated_spread=bool(b["edge_validation"]["spread"]["validated"]),
                edge_validated_total=bool(b["edge_validation"]["total"]["validated"]),
            )
            for name, b in cal["buckets"].items()
        },
    )


# --------------------------------------------------------------------------------------
# Stability buckets
# --------------------------------------------------------------------------------------


def stability_cutpoints(values: Sequence[float]) -> tuple[float, float]:
    """Tercile cutpoints of game stability (`stability_min`) -- set from data, not
    judgment."""
    arr = np.asarray(values, dtype=float)
    if arr.size == 0:
        raise ValueError("no stability values")
    q = np.quantile(arr, [1 / 3, 2 / 3])
    return float(q[0]), float(q[1])


def stability_bucket(s: float, cutpoints: tuple[float, float]) -> str:
    low_hi, mid_hi = cutpoints
    if s < low_hi:
        return "low"
    if s < mid_hi:
        return "mid"
    return "high"


def with_stability_bucket(frame: pl.DataFrame, cutpoints: tuple[float, float]) -> pl.DataFrame:
    low_hi, mid_hi = cutpoints
    s = pl.col("stability_min")
    return frame.with_columns(
        pl.when(s.is_null())
        .then(None)
        .when(s < low_hi)
        .then(pl.lit("low"))
        .when(s < mid_hi)
        .then(pl.lit("mid"))
        .otherwise(pl.lit("high"))
        .alias("stability_bucket")
    )
