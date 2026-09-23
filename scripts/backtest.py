"""One-off script: walk-forward backtest of the P5 projection model against nflverse
closing lines. Writes docs/backtest_report.md. Reproduces from this one command.

- **Walk-forward, expanding window.** Test season S uses coefficients fit only on
  seasons before S (asserted in pipeline/synthesis/model.py's walk_forward).
  Out-of-sample seasons are 2020-2025. 2019 is training-only because 2018 has no
  efficiency signals.
- **Benchmarks.** HFA-only (intercept + home-field term, same walk-forward) and the
  closing line itself.
- **Splits.** Stability bucket, week range, |edge| bucket. Also reports per-season
  gamma, per-season total bias, and the weeks 1-4 vs. 5+ beta diagnostic.
- **Variants, comparison only.** A pass/rush-split model and a points_per_drive model.
  Neither is adopted this phase.
- **r-leak sensitivity.** Re-estimates reliability r from the 2018->2019 pair only,
  calling scripts/estimate_reliability.py's own function. Recomputes point-in-time
  epa_per_play signals for 2019-2025 with those r values by running the production
  EfficiencyAnalyst.compute() with its metric config swapped. That's a scratch run:
  nothing is written to the database, and results are cached under .cache/backtest/.
  It then re-runs the walk-forward and reports both runs side by side.
  - Before the scratch run, a parity check recomputes one stored week with the
    production config. It must match the stored signals, which proves the scratch
    path reproduces production.

Every model computation is imported from pipeline/synthesis/model.py and
scripts/projection_history.py. Nothing is reimplemented (CLAUDE.md's
verification-script convention). The report states results only. Its interpretation
section is left for review.

Reads: games, signals, team_week/player_week/snaps (via the analyst, for the
sensitivity run). Writes: docs/backtest_report.md, .cache/backtest/*.parquet.

Usage:
  uv run python scripts/backtest.py
  uv run python scripts/backtest.py --skip-r-sensitivity
  uv run python scripts/backtest.py --refresh-r-cache
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import io
import json
import logging
import sys
import time
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import nflreadpy as nfl
import numpy as np
import polars as pl
import psycopg

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pipeline.analysts import efficiency  # noqa: E402
from pipeline.analysts.efficiency import EfficiencyAnalyst, MetricConfig  # noqa: E402
from pipeline.core.base import RunContext  # noqa: E402
from pipeline.core.config import get_settings  # noqa: E402
from pipeline.core.db import get_connection  # noqa: E402
from pipeline.synthesis.model import (  # noqa: E402
    COEFFICIENTS_PATH,
    HFA_ONLY_SPEC,
    MODEL_VERSION,
    PRIMARY_SPEC,
    STABILITY_BUCKETS,
    FitResult,
    ModelSpec,
    build_game_frame,
    efficiency_fingerprint,
    fit,
    to_team_rows,
    walk_forward,
)
from scripts.estimate_reliability import (  # noqa: E402
    ReliabilityEstimate,
    _fetch_season_team_week,
    estimate_reliability,
)
from scripts.projection_history import (  # noqa: E402
    BOOTSTRAP_N,
    BOOTSTRAP_SEED,
    add_outcomes,
    calibrate,
    check_fit_seasons,
    edge_validation,
    load_efficiency_signals,
    load_games,
)

_REPO = Path(__file__).resolve().parent.parent
REPORT_PATH = _REPO / "docs" / "backtest_report.md"
CACHE_DIR = _REPO / ".cache" / "backtest"

VARIANT_SPECS = (
    ModelSpec("pass_rush_split", ("epa_per_play_pass", "epa_per_play_rush")),
    ModelSpec("points_per_drive", ("points_per_drive",)),
)
WEEK_RANGES = ((1, 4), (5, 8), (9, 12), (13, 99))
EDGE_BUCKETS: tuple[tuple[float, float | None], ...] = (
    (0, 1), (1, 2), (2, 3), (3, 4), (4, None)
)
DIAGNOSTIC_EARLY_WEEKS = (1, 4)
R_SENSITIVITY_SEASONS = [2018, 2019]
PARITY_WEEK = (2021, 10)
PARITY_TOLERANCE = 1e-9


# --------------------------------------------------------------------------------------
# Formatting
# --------------------------------------------------------------------------------------


def _fmt(v: Any, nd: int = 2) -> str:
    if v is None:
        return "—"
    if isinstance(v, bool):
        return "yes" if v else "no"
    if isinstance(v, int):
        return str(v)
    return f"{v:.{nd}f}"


def _table(headers: Sequence[str], rows: Sequence[Sequence[Any]], nd: int = 2) -> list[str]:
    out = ["| " + " | ".join(headers) + " |", "|" + "---|" * len(headers)]
    out += ["| " + " | ".join(_fmt(c, nd) if not isinstance(c, str) else c for c in r) + " |"
            for r in rows]
    return out + [""]


def _num(x: Any) -> float | None:
    return None if x is None else float(x)


# --------------------------------------------------------------------------------------
# Metrics (on frames that went through add_outcomes)
# --------------------------------------------------------------------------------------


def _errors(frame: pl.DataFrame, margin_col: str, total_col: str) -> dict[str, Any]:
    """Margin and total errors, each over the games that have that prediction (a game
    missing only its closing total still counts toward the closing-spread margin)."""
    out: dict[str, Any] = {}
    for key, actual, pred in (("margin", "margin_actual", margin_col),
                              ("total", "total_actual", total_col)):
        r = frame.select((pl.col(actual) - pl.col(pred)).alias("r")).drop_nulls()["r"]
        v = r.to_numpy().astype(float)
        out[f"{key}_n"] = v.size
        out[f"{key}_mae"] = float(np.mean(np.abs(v))) if v.size else None
        out[f"{key}_rmse"] = float(np.sqrt(np.mean(v**2))) if v.size else None
        out[f"{key}_bias"] = float(np.mean(-v)) if v.size else None  # projected - actual
    return out


def _model_errors(frame: pl.DataFrame) -> dict[str, Any]:
    return _errors(frame, "margin_home", "projected_total")


def _edge_cells(ev: dict[str, Any]) -> list[Any]:
    return [
        ev["corr"],
        f"[{_fmt(ev['ci_low'], 3)}, {_fmt(ev['ci_high'], 3)}]",
        ev["n"],
        ev["validated"],
    ]


def _edge_bucket_rows(frame: pl.DataFrame, signed_edge: pl.Expr, outcome: str) -> list[list[Any]]:
    """Signed edge > 0 = the model's side is home (spread) / over (total). A pick wins
    when signed edge and outcome share a sign; outcome 0 is a push; zero edge is no pick."""
    d = (
        frame.select(signed_edge.alias("e"), pl.col(outcome).alias("o"))
        .drop_nulls()
        .filter(pl.col("e") != 0)
    )
    rows: list[list[Any]] = []
    for lo, hi in EDGE_BUCKETS:
        cond = pl.col("e").abs() >= lo
        if hi is not None:
            cond = cond & (pl.col("e").abs() < hi)
        b = d.filter(cond)
        pushes = b.filter(pl.col("o") == 0).height
        decided = b.filter(pl.col("o") != 0)
        wins = decided.filter(pl.col("e") * pl.col("o") > 0).height
        label = f"{lo}–{hi}" if hi is not None else f"{lo}+"
        rate = wins / decided.height if decided.height else None
        rows.append([label, b.height, decided.height, wins, pushes, rate])
    return rows


def _week_range_filter(lo: int, hi: int) -> pl.Expr:
    return (pl.col("week") >= lo) & (pl.col("week") <= hi)


def _week_label(lo: int, hi: int) -> str:
    return f"{lo}–{hi}" if hi < 99 else f"{lo}+"


def _coef_cells(f: FitResult, names: Sequence[str]) -> list[Any]:
    cells: list[Any] = []
    for n in names:
        cells += [f.coef[n], f.se[n]]
    return cells


# --------------------------------------------------------------------------------------
# r-leak sensitivity (scratch efficiency run, nothing written to the DB)
# --------------------------------------------------------------------------------------


def _scratch_efficiency(
    conn: psycopg.Connection,
    season_weeks: Sequence[tuple[int, int]],
    metrics: Sequence[MetricConfig],
    *,
    progress: bool = False,
) -> pl.DataFrame:
    """Runs the production EfficiencyAnalyst.compute() for each (season, week) with its
    metric config temporarily swapped for `metrics`. compute() only reads; write_signals
    is never called, and the transaction is rolled back."""
    analyst = EfficiencyAnalyst()
    settings = get_settings()
    original = efficiency._METRIC_CONFIG
    eff_log = logging.getLogger(efficiency.__name__)
    old_level = eff_log.level
    eff_log.setLevel(logging.ERROR)  # the per-team "discount skipped" warnings are expected
    efficiency._METRIC_CONFIG = list(metrics)
    parts: list[pl.DataFrame] = []
    started = time.monotonic()
    try:
        for i, (season, week) in enumerate(season_weeks, start=1):
            ctx = RunContext(
                season=season, week=week, season_type="REG",
                now=datetime.now(UTC), settings=settings, conn=conn,
            )
            parts.append(
                analyst.compute(ctx).select(
                    "season", "week", "team", "signal", "value",
                    pl.col("stability").cast(pl.Float64),
                )
            )
            if progress and (i == len(season_weeks) or week == 1):
                print(f"  scratch efficiency [{i}/{len(season_weeks)}] {season} wk{week} "
                      f"({time.monotonic() - started:.0f}s)", flush=True)
    finally:
        efficiency._METRIC_CONFIG = original
        eff_log.setLevel(old_level)
        conn.rollback()
    return pl.concat(parts)


def _parity_check(
    conn: psycopg.Connection, stored: pl.DataFrame, metrics: Sequence[MetricConfig]
) -> dict[str, Any]:
    """Scratch-compute one stored week with the PRODUCTION config and compare it to the
    stored signals. The scratch path must reproduce production exactly, or the
    sensitivity comparison would measure the harness, not r."""
    season, week = PARITY_WEEK
    scratch = _scratch_efficiency(conn, [PARITY_WEEK], metrics)
    names = [f"{m.name}_{side}" for m in metrics for side in ("off", "def")]
    ref = stored.filter(
        (pl.col("season") == season) & (pl.col("week") == week) & pl.col("signal").is_in(names)
    )
    joined = scratch.join(ref, on=["season", "week", "team", "signal"], how="full",
                          suffix="_stored")
    unmatched = joined.filter(pl.col("value").is_null() | pl.col("value_stored").is_null())
    max_value = _num(joined.select((pl.col("value") - pl.col("value_stored")).abs().max()).item())
    max_stab = _num(
        joined.select((pl.col("stability") - pl.col("stability_stored")).abs().max()).item()
    )
    ok = unmatched.height == 0 and max_value is not None and max_value <= PARITY_TOLERANCE
    return {"week": PARITY_WEEK, "rows": joined.height, "unmatched": unmatched.height,
            "max_value_diff": max_value, "max_stability_diff": max_stab, "ok": ok}


def _r_sensitivity(
    conn: psycopg.Connection, stored: pl.DataFrame, refresh: bool
) -> dict[str, Any]:
    with contextlib.redirect_stdout(io.StringIO()):
        team_weeks = {s: _fetch_season_team_week(conn, s) for s in R_SENSITIVITY_SEASONS}
        estimates = estimate_reliability(team_weeks, R_SENSITIVITY_SEASONS)

    production = {m.name: m for m in efficiency._METRIC_CONFIG}
    prod_metrics = [production[b] for b in PRIMARY_SPEC.bases]
    patched = [
        production[b]._replace(
            reliability_off=estimates[b].off,
            reliability_def=estimates[b].def_,
            reliability_off_raw=estimates[b].off_raw,
            reliability_def_raw=estimates[b].def_raw,
        )
        for b in PRIMARY_SPEC.bases
    ]

    print("r sensitivity: parity check against stored signals ...", flush=True)
    parity = _parity_check(conn, stored, prod_metrics)
    if not parity["ok"]:
        raise RuntimeError(f"scratch efficiency path does not reproduce production: {parity}")

    season_weeks = sorted(stored.select("season", "week").unique().rows())
    key_src = json.dumps({"metrics": [m._asdict() for m in patched], "weeks": season_weeks},
                         sort_keys=True)
    key = hashlib.sha256(key_src.encode("utf-8")).hexdigest()[:12]
    path = CACHE_DIR / f"r_sensitivity_{key}.parquet"
    reused = path.exists() and not refresh
    if reused:
        print(f"r sensitivity: reusing {path.relative_to(_REPO)}", flush=True)
        scratch = pl.read_parquet(path)
    else:
        print(f"r sensitivity: recomputing {len(season_weeks)} weeks ...", flush=True)
        scratch = _scratch_efficiency(conn, season_weeks, patched, progress=True)
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        scratch.write_parquet(path)
    return {
        "estimates": {b: estimates[b] for b in PRIMARY_SPEC.bases},
        "production": {b: production[b] for b in PRIMARY_SPEC.bases},
        "parity": parity,
        "signals": scratch,
        "cache": path.relative_to(_REPO).as_posix(),
        "reused_cache": reused,
    }


# --------------------------------------------------------------------------------------
# Report
# --------------------------------------------------------------------------------------

_METHODOLOGY = """## Methodology

**Model (pre-registered, `pipeline/synthesis/model.py`).** One regression on team-game
rows (2 per game):
`pts_i = α + β_off·(off_i − mean_off) + β_def·(def_j − mean_def) + γ·h_i`.
- `off_i` is team i's `epa_per_play_off` and `def_j` its opponent's `epa_per_play_def`,
  both the point-in-time "entering this week" efficiency signals at the game's own
  (season, week).
- Features are centered on that week's unweighted mean across teams.
- `h` is +0.5 home, −0.5 away, and 0 at a neutral site (`games.location`), so γ is
  home-field advantage in points of margin.
- The target is actual points scored, never the line.
- Outputs:
  - margin = pts_home − pts_away
  - projected_spread_home = −margin (market convention)
  - total = pts_home + pts_away
- Standard errors are cluster-robust by game (CR1).

**Walk-forward.**
- Each test season is predicted with coefficients fit only on earlier seasons
  (expanding window).
- 2019 is training-only. 2018 has no efficiency signals, because it has no staged prior
  season.
- REG games only.

**Benchmarks.**
- *HFA-only*: α + γ·h, same walk-forward. Its total is the training mean total.
- *Closing line*: nflverse `spread_line`/`total_line`. `spread_line` is home-positive,
  so it is the line's expected home margin directly. For edges it is converted to the
  market convention, `closing_spread_home = −spread_line`.

**Edges.**
- `edge_spread = projected_spread_home − closing_spread_home`. Negative means the model
  rates home higher than the close.
- `edge_total = projected_total − closing_total`.
- Edge validity is `corr(−edge_spread, home margin + closing_spread_home)`, and for
  totals `corr(edge_total, total − closing_total)`.
  - The 95% CI is a game bootstrap ({boot_n} resamples, seed {boot_seed}).
  - `validated` means the CI lies entirely above 0.
- ATS/O-U tables:
  - A pick is the model's side of the close.
  - Pushes are counted separately.
  - Zero-edge games are not picks.

**Stability.**
- Game stability `s_g` is the min of the 4 input stabilities. Each stability is
  `w_cur + w_prior` from the efficiency blend.
- Buckets are the terciles of `s_g` over the out-of-sample games.
- A bucket's band SD is the RMS out-of-sample residual (not demeaned). Coverage is the
  share of games with |residual| ≤ that SD. Because the SDs come from the same residuals,
  coverage checks the band's shape, not a further out-of-sample calibration.
"""

_LIMITATIONS = """## Limitations (disclosed before results were seen)

1. **Parameter-level look-ahead.**
   - Efficiency's reliability `r` was estimated from 2018–2025 rating pairs, so it
     overlaps the test seasons.
   - It measures year-over-year rating correlation, not ratings vs. outcomes or lines.
   - The sensitivity section above quantifies it.
   - `k_metric` and the garbage-time thresholds are 2026 judgment calls, not fits to
     outcomes.
2. **Week-1 asymmetry.**
   - A historical week 1 has no current-season QB (no depth-chart history), so
     `qb_factor = 1.0`.
   - Live week 1 uses the depth chart, so backtest week 1 is slightly *less* informed
     than production.
3. **Corrected data.** The backfill used today's stat-corrected `team_week`, not what
   was known that Tuesday.
4. **Timestamps.** Backfilled rows carry the backfill's `as_of`/`inputs_version`. The fit
   keys only on (season, week).
5. **Line source mismatch.** The backtest uses nflverse closing lines. Live edges use our
   own consensus median at lock time.
6. **Book-disagreement flags can't be backtested.**
   - `edge_within_book_range`, `spread_key_straddle`, and `single_book_market` need
     per-book lines.
   - A historical close is a single number.
7. **Single intercept.** One α can't follow season-level scoring shifts. See per-season
   total bias.
"""


def _render(ctx: dict[str, Any]) -> str:
    L: list[str] = []
    seasons: list[int] = ctx["seasons"]
    test_seasons: list[int] = ctx["test_seasons"]
    oos: pl.DataFrame = ctx["oos"]
    cal: dict[str, Any] = ctx["calibration"]
    full: FitResult = ctx["full"]
    names = PRIMARY_SPEC.coef_names

    L += [
        "# P5 backtest report",
        "",
        f"Generated by `uv run python scripts/backtest.py` on {ctx['generated']} (UTC).",
        f"Model `{MODEL_VERSION}`, spec `{PRIMARY_SPEC.name}`, efficiency fingerprint "
        f"`{ctx['fingerprint']}`.",
        f"Fit seasons {seasons[0]}–{seasons[-1]}; out-of-sample seasons "
        f"{test_seasons[0]}–{test_seasons[-1]}. REG games only.",
        "",
        _METHODOLOGY.format(boot_n=BOOTSTRAP_N, boot_seed=BOOTSTRAP_SEED),
    ]

    # Data
    L += ["## Data", ""]
    rows = []
    frame: pl.DataFrame = ctx["frame"]
    for s in seasons:
        f = frame.filter(pl.col("season") == s)
        rows.append([
            str(s), f.height, f.filter(~pl.col("features_complete")).height,
            f.filter(pl.col("location") == "Neutral").height,
            f.filter(pl.col("spread_line").is_null() | pl.col("total_line").is_null()).height,
        ])
    L += _table(["Season", "REG games", "Incomplete features", "Neutral site",
                 "No closing line"], rows)

    # Full-sample fit
    L += ["## Full-sample fit (the coefficients the synthesizer uses)", ""]
    L += [f"Fit on {full.n_games} games ({full.n_rows} team-game rows). Residual SD "
          f"(team points): {_fmt(full.resid_sd)}.", ""]
    L += _table(["Coefficient", "Estimate", "SE (clustered)"],
                [[n, full.coef[n], full.se[n]] for n in names], nd=4)
    L += [f"`pipeline/synthesis/model_coefficients.json`: {ctx['coef_file_status']}.", ""]

    # Walk-forward fits
    L += ["## Walk-forward coefficients", ""]
    headers = ["Test season", "Trained on", "Games"]
    for n in names:
        headers += [n, "se"]
    rows = []
    for s in test_seasons:
        f = ctx["wf_fits"][s]
        rows.append([str(s), f"{seasons[0]}–{s - 1}", f.n_games, *_coef_cells(f, names)])
    L += _table(headers, rows, nd=3)

    # Accuracy
    L += ["## Out-of-sample accuracy", "", "### Margin (home − away)", ""]
    margin_rows, total_rows = [], []
    for label, f in [(str(s), oos.filter(pl.col("season") == s)) for s in test_seasons] + [
        ("**All**", oos)
    ]:
        m = _model_errors(f)
        h = _errors(f, "hfa_margin", "hfa_total")
        c = _errors(f, "spread_line", "total_line")
        margin_rows.append([label, m["margin_n"], m["margin_mae"], m["margin_rmse"],
                            h["margin_mae"], h["margin_rmse"], c["margin_n"], c["margin_mae"],
                            c["margin_rmse"]])
        total_rows.append([label, m["total_n"], m["total_mae"], m["total_rmse"], m["total_bias"],
                           h["total_mae"], h["total_rmse"], c["total_n"], c["total_mae"],
                           c["total_rmse"]])
    L += _table(["Season", "n", "Model MAE", "Model RMSE", "HFA-only MAE", "HFA-only RMSE",
                 "Close n", "Close MAE", "Close RMSE"], margin_rows)
    L += ["### Total", "", "Bias = mean(projected − actual).", ""]
    L += _table(["Season", "n", "Model MAE", "Model RMSE", "Model bias", "Mean-only MAE",
                 "Mean-only RMSE", "Close n", "Close MAE", "Close RMSE"], total_rows)

    # Stability buckets
    bucketed: pl.DataFrame = ctx["bucketed"]
    c1, c2 = cal["stability_cutpoints"]
    L += ["## By stability bucket", "",
          f"Tercile cutpoints of `s_g`: low < {_fmt(c1, 4)} ≤ mid < {_fmt(c2, 4)} ≤ high.", ""]
    rows = []
    for b in STABILITY_BUCKETS:
        info = cal["buckets"][b]
        f = bucketed.filter(pl.col("stability_bucket") == b)
        m = _model_errors(f)
        cov_m = _num(f.select((pl.col("margin_resid").abs() <= info["margin_sd"]).mean()).item())
        cov_t = _num(f.select((pl.col("total_resid").abs() <= info["total_sd"]).mean()).item())
        lo, hi = info["stability_range"]
        rows.append([b, info["n_games"], f"{_fmt(lo, 3)}–{_fmt(hi, 3)}", m["margin_mae"],
                     info["margin_sd"], cov_m, m["total_mae"], info["total_sd"], cov_t])
    L += _table(["Bucket", "n", "s_g range", "Margin MAE", "Margin band SD", "Margin coverage",
                 "Total MAE", "Total band SD", "Total coverage"], rows)

    # Week ranges
    L += ["## By week range", ""]
    rows = []
    for lo, hi in WEEK_RANGES:
        f = oos.filter(_week_range_filter(lo, hi))
        m = _model_errors(f)
        c = _errors(f, "spread_line", "total_line")
        ev = edge_validation(f)
        rows.append([_week_label(lo, hi), m["margin_n"], m["margin_mae"], c["margin_mae"],
                     m["total_mae"], c["total_mae"], m["total_bias"],
                     ev["spread"]["corr"], ev["total"]["corr"]])
    L += _table(["Weeks", "n", "Margin MAE", "Close margin MAE", "Total MAE", "Close total MAE",
                 "Total bias", "Spread edge r", "Total edge r"], rows)

    # Edges
    L += ["## Edge vs. closing line", ""]
    for market in ("spread", "total"):
        rows = []
        overall = edge_validation(oos)[market]
        rows.append(["All", *_edge_cells(overall)])
        for b in STABILITY_BUCKETS:
            rows.append([f"bucket {b}", *_edge_cells(cal["buckets"][b]["edge_validation"][market])])
        for s in test_seasons:
            rows.append([str(s), *_edge_cells(
                edge_validation(oos.filter(pl.col("season") == s))[market]
            )])
        L += [f"### {market.capitalize()}: correlation", ""]
        L += _table(["Slice", "r", "95% CI", "n", "Validated"], rows, nd=3)

    L += ["### Spread picks by |edge| (ATS vs. close)", ""]
    L += _table(["|edge| pts", "Picks", "Decided", "Wins", "Pushes", "Win rate"],
                _edge_bucket_rows(oos, -pl.col("edge_spread"), "ats_margin_home"), nd=3)
    L += ["### Total picks by |edge| (O/U vs. close)", ""]
    L += _table(["|edge| pts", "Picks", "Decided", "Wins", "Pushes", "Win rate"],
                _edge_bucket_rows(oos, pl.col("edge_total"), "ou_margin"), nd=3)

    L += ["`edge_validated` flags written to the coefficients file, per bucket:", ""]
    L += _table(["Bucket", "Spread", "Total"],
                [[b, cal["buckets"][b]["edge_validation"]["spread"]["validated"],
                  cal["buckets"][b]["edge_validation"]["total"]["validated"]]
                 for b in STABILITY_BUCKETS])

    # Per-season gamma
    L += ["## Per-season home-field advantage (in-sample, each season fit alone)", ""]
    L += _table(["Season", "Games", "γ", "γ se", "α", "α se"],
                [[str(s), f.n_games, f.coef["gamma"], f.se["gamma"], f.coef["alpha"],
                  f.se["alpha"]] for s, f in ctx["season_fits"].items()])

    # Shrinkage-calibration diagnostic
    lo, hi = DIAGNOSTIC_EARLY_WEEKS
    L += ["## Shrinkage-calibration diagnostic (in-sample, all fit seasons)", "",
          f"β fit separately on weeks {lo}–{hi} and weeks {hi + 1}+. The result is "
          "feedback for the efficiency blend's `k`/`r` and is not applied in the "
          "synthesizer.", ""]
    headers = ["Weeks", "Games"]
    for n in names:
        headers += [n, "se"]
    L += _table(headers, [[label, f.n_games, *_coef_cells(f, names)]
                          for label, f in ctx["diagnostic_fits"]], nd=3)

    # Variants
    L += ["## Variant comparison (not adopted)", "",
          "All specs are evaluated on the same games (complete features in every spec), "
          "with the same walk-forward.", ""]
    rows = []
    for spec_name, vo in ctx["variants"]:
        m = _model_errors(vo)
        ev = edge_validation(vo)
        rows.append([spec_name, m["margin_n"], m["margin_mae"], m["margin_rmse"], m["total_mae"],
                     m["total_rmse"], ev["spread"]["corr"],
                     f"[{_fmt(ev['spread']['ci_low'], 3)}, {_fmt(ev['spread']['ci_high'], 3)}]",
                     ev["total"]["corr"],
                     f"[{_fmt(ev['total']['ci_low'], 3)}, {_fmt(ev['total']['ci_high'], 3)}]"])
    L += _table(["Spec", "n", "Margin MAE", "Margin RMSE", "Total MAE", "Total RMSE",
                 "Spread edge r", "CI", "Total edge r", "CI"], rows, nd=3)

    # r sensitivity
    L += ["## Reliability-leak sensitivity", ""]
    rs = ctx.get("r_sensitivity")
    if rs is None:
        L += ["Skipped (`--skip-r-sensitivity`).", ""]
    else:
        p = rs["parity"]
        L += [
            f"r re-estimated from the {R_SENSITIVITY_SEASONS[0]}→{R_SENSITIVITY_SEASONS[1]} "
            "pair only, by `scripts/estimate_reliability.py`'s own method (pooled r, "
            "clipped, shrunk toward its side's mean, down4 pinned at 0). "
            "`epa_per_play` signals for every fit week were recomputed with it by the "
            "production `EfficiencyAnalyst.compute()` in a scratch run (nothing written; "
            f"cached at `{rs['cache']}`, reused: {_fmt(rs['reused_cache'])}).",
            "",
            f"Parity check (production config, {p['week'][0]} week {p['week'][1]}, vs. "
            f"stored signals): {p['rows']} rows, {p['unmatched']} unmatched, max |Δvalue| "
            f"{p['max_value_diff']:.2e}, max |Δstability| {p['max_stability_diff']:.2e}.",
            "",
        ]
        rows = []
        for b in PRIMARY_SPEC.bases:
            prod: MetricConfig = rs["production"][b]
            est: ReliabilityEstimate = rs["estimates"][b]
            rows.append([f"{b}_off", prod.reliability_off_raw, prod.reliability_off,
                         est.off_raw, est.off])
            rows.append([f"{b}_def", prod.reliability_def_raw, prod.reliability_def,
                         est.def_raw, est.def_])
        L += _table(["Signal", "Production raw r (2018–2025)", "Production r",
                     "2018→2019 raw r", "2018→2019 r"], rows, nd=4)

        base_full, sens_full = full, ctx["sens_full"]
        L += ["### Full-sample coefficients", ""]
        L += _table(["Coefficient", "Production r", "se", "2018→2019 r", "se"],
                    [[n, base_full.coef[n], base_full.se[n], sens_full.coef[n],
                      sens_full.se[n]] for n in names], nd=4)

        base_oos, sens_oos = ctx["sens_pair"]
        bm, sm = _model_errors(base_oos), _model_errors(sens_oos)
        be, se = edge_validation(base_oos), edge_validation(sens_oos)
        L += ["### Out-of-sample walk-forward, same games", ""]
        metric_rows = [
            ["Games", bm["margin_n"], sm["margin_n"]],
            ["Margin MAE", bm["margin_mae"], sm["margin_mae"]],
            ["Margin RMSE", bm["margin_rmse"], sm["margin_rmse"]],
            ["Total MAE", bm["total_mae"], sm["total_mae"]],
            ["Total RMSE", bm["total_rmse"], sm["total_rmse"]],
            ["Spread edge r", be["spread"]["corr"], se["spread"]["corr"]],
            ["Spread edge r CI low", be["spread"]["ci_low"], se["spread"]["ci_low"]],
            ["Total edge r", be["total"]["corr"], se["total"]["corr"]],
            ["Total edge r CI low", be["total"]["ci_low"], se["total"]["ci_low"]],
        ]
        L += _table(["Metric", "Production r", "2018→2019 r"], metric_rows, nd=4)
        diff = base_oos.join(
            sens_oos.select("game_id", pl.col("projected_spread_home").alias("s_sp"),
                            pl.col("projected_total").alias("s_tot")),
            on="game_id",
        ).select(
            (pl.col("projected_spread_home") - pl.col("s_sp")).abs().alias("d_sp"),
            (pl.col("projected_total") - pl.col("s_tot")).abs().alias("d_tot"),
        )
        L += _table(["Per-game |Δ projection|", "Mean", "Max"], [
            ["Spread", _num(diff["d_sp"].mean()), _num(diff["d_sp"].max())],
            ["Total", _num(diff["d_tot"].mean()), _num(diff["d_tot"].max())],
        ], nd=3)

    L += [_LIMITATIONS]
    L += ["## Interpretation", "", "_Pending review. Not written until the results above "
          "have been read._", ""]
    return "\n".join(L)


def _coef_file_status(full: FitResult, fingerprint: str, seasons: list[int]) -> str:
    if not COEFFICIENTS_PATH.exists():
        return "not found (run scripts/fit_projection_model.py)"
    data = json.loads(COEFFICIENTS_PATH.read_text(encoding="utf-8"))
    same = (
        data.get("efficiency_fingerprint") == fingerprint
        and data.get("fit_seasons") == seasons
        and all(abs(data["coefficients"][k]["value"] - v) < 1e-9 for k, v in full.coef.items())
    )
    return "matches this run" if same else "**differs from this run** (refit needed)"


# --------------------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------------------


def _season_range(text: str) -> list[int]:
    start, end = (int(x) for x in text.split("-"))
    return list(range(start, end + 1))


def _oos(frame: pl.DataFrame, spec: ModelSpec, test_seasons: list[int]) -> pl.DataFrame:
    pred, _ = walk_forward(frame, spec, test_seasons)
    return add_outcomes(pred)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--seasons", default="2019-2025")
    parser.add_argument("--test-seasons", default="2020-2025")
    parser.add_argument("--out", type=Path, default=REPORT_PATH)
    parser.add_argument("--skip-r-sensitivity", action="store_true")
    parser.add_argument("--refresh-r-cache", action="store_true",
                        help="recompute the scratch efficiency run even if cached")
    args = parser.parse_args()
    seasons = _season_range(args.seasons)
    test_seasons = _season_range(args.test_seasons)
    if not set(test_seasons) <= set(seasons) or min(test_seasons) <= min(seasons):
        print("refusing: test seasons must be inside --seasons and after its first season",
              file=sys.stderr)
        return 1

    all_signals = sorted(
        {n for spec in (PRIMARY_SPEC, *VARIANT_SPECS) for n in spec.signal_names}
    )
    with get_connection() as conn:
        games = load_games(conn, seasons)
        signals = load_efficiency_signals(conn, seasons, all_signals)
        try:
            check_fit_seasons(seasons, nfl.get_current_season(), games)
        except ValueError as exc:
            print(exc, file=sys.stderr)
            return 1
        r_sens = None if args.skip_r_sensitivity else _r_sensitivity(
            conn, signals, args.refresh_r_cache
        )

    print("fitting and walking forward ...", flush=True)
    frame = build_game_frame(games, signals, PRIMARY_SPEC)
    full = fit(to_team_rows(frame, PRIMARY_SPEC), PRIMARY_SPEC)
    pred, wf_fits = walk_forward(frame, PRIMARY_SPEC, test_seasons)
    hfa = _oos(build_game_frame(games, signals, HFA_ONLY_SPEC), HFA_ONLY_SPEC, test_seasons)
    oos = add_outcomes(pred).join(
        hfa.select("game_id", pl.col("margin_home").alias("hfa_margin"),
                   pl.col("projected_total").alias("hfa_total")),
        on="game_id", how="left",
    )
    bucketed, calibration = calibrate(oos)

    season_fits = {
        s: fit(to_team_rows(frame.filter(pl.col("season") == s), PRIMARY_SPEC), PRIMARY_SPEC)
        for s in seasons
    }
    lo, hi = DIAGNOSTIC_EARLY_WEEKS
    diagnostic_fits = [
        (f"{lo}–{hi}", fit(to_team_rows(frame.filter(_week_range_filter(lo, hi)), PRIMARY_SPEC),
                           PRIMARY_SPEC)),
        (f"{hi + 1}+", fit(to_team_rows(frame.filter(pl.col("week") > hi), PRIMARY_SPEC),
                           PRIMARY_SPEC)),
    ]

    variant_frames = {
        spec.name: (spec, build_game_frame(games, signals, spec))
        for spec in (PRIMARY_SPEC, *VARIANT_SPECS)
    }
    common: set[str] = set.intersection(*(
        set(f.filter(pl.col("features_complete"))["game_id"].to_list())
        for _, f in variant_frames.values()
    ))
    variants = [
        (name, _oos(f.filter(pl.col("game_id").is_in(sorted(common))), spec, test_seasons))
        for name, (spec, f) in variant_frames.items()
    ]

    ctx: dict[str, Any] = {
        "generated": datetime.now(UTC).strftime("%Y-%m-%d %H:%M"),
        "fingerprint": efficiency_fingerprint(PRIMARY_SPEC),
        "seasons": seasons,
        "test_seasons": test_seasons,
        "frame": frame,
        "full": full,
        "wf_fits": wf_fits,
        "oos": oos,
        "bucketed": bucketed,
        "calibration": calibration,
        "season_fits": season_fits,
        "diagnostic_fits": diagnostic_fits,
        "variants": variants,
        "coef_file_status": _coef_file_status(
            full, efficiency_fingerprint(PRIMARY_SPEC), seasons
        ),
    }

    if r_sens is not None:
        sens_frame = build_game_frame(games, r_sens["signals"], PRIMARY_SPEC)
        sens_oos = _oos(sens_frame, PRIMARY_SPEC, test_seasons)
        shared = sorted(set(oos["game_id"].to_list()) & set(sens_oos["game_id"].to_list()))
        ctx["r_sensitivity"] = r_sens
        ctx["sens_full"] = fit(to_team_rows(sens_frame, PRIMARY_SPEC), PRIMARY_SPEC)
        ctx["sens_pair"] = (
            oos.filter(pl.col("game_id").is_in(shared)),
            sens_oos.filter(pl.col("game_id").is_in(shared)),
        )

    with args.out.open("w", encoding="utf-8", newline="\n") as f:
        f.write(_render(ctx))
    print(f"wrote {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
