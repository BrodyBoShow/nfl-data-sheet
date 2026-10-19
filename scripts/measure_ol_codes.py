"""One-off script: measure P7 open item 22, row 3, Efficiency's O-line code set missing
2020's combo codes, on every /method number and on the current week's cards. Nothing is
written to the database.

What it does, all through production code (CLAUDE.md's verification-script convention):
1. **Patched signals.** Re-runs the production `EfficiencyAnalyst.compute()` for every
   stored fit week (2019-2025) with `efficiency._OL_SNAP_POSITIONS` widened by the 2020
   codes, through `scripts/backtest.py`'s own scratch path (`_scratch_efficiency`, read
   only, rolled back). Every metric the backtest reads is recomputed.
   - It refuses to go on if any row outside 2020-2021 differs from the stored signals, or
     if any row is unmatched. Either would mean the scratch path no longer reproduces
     production, and the measurement wouldn't isolate the code set.
   - A changed row takes the scratch value. Its stability is rounded through float32,
     the way `signals.stability` (real) stores it. Unchanged rows stay as stored.
2. **Both backtests.** Runs `scripts/backtest.py`'s `main()` twice, on the stored signals
   ("before") and the patched ones ("after"). The O-line set is swapped for the "after"
   run, so its r-sensitivity parity check compares a patched recompute to the patched
   signals.
   - Each run gets its own cache directory, so the normal r-sensitivity cache isn't
     touched.
   - The "before" run should reproduce `docs/backtest_report.md`, apart from its
     generated time.
3. **Both model files.** Runs `scripts/fit_projection_model.py`'s `main()` the same two
   ways. "Before" should reproduce `model_coefficients.json`, apart from `fitted_on`.
4. **/method's numbers.** Builds the site's summary from each (report, model file) pair
   with `web/scripts/backtest-content.mjs`'s own `buildSummary`, so the published,
   before and after columns are what /method would print.
5. **Today's cards.** Projects every game of the latest week with a matchup card under
   both model files, through the synthesizer's own `_predictions` and
   `_uncertainty_block`, beside the stored card.
   - The synthesizer's staleness check is bypassed for the "after" model, deliberately:
     its `fit_inputs_hash` covers the patched signals, which aren't stored.

Writes only under .cache/backtest/ol_codes/: the two reports, the two model files, both
summaries, the patched signals and result.json. Run time: measured on the first run,
not estimated here.

Usage:
  uv run python scripts/measure_ol_codes.py
  uv run python scripts/measure_ol_codes.py --skip-r-sensitivity
"""

from __future__ import annotations

import argparse
import contextlib
import json
import subprocess
import sys
import time
from collections.abc import Generator
from pathlib import Path
from typing import Any
from unittest import mock

import polars as pl

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pipeline.analysts import efficiency  # noqa: E402
from pipeline.core.db import get_connection  # noqa: E402
from pipeline.core.schedule import kickoff_utc  # noqa: E402
from pipeline.synthesis.model import (  # noqa: E402
    COEFFICIENTS_PATH,
    PRIMARY_SPEC,
    ModelFile,
    load_model_file,
)
from pipeline.synthesis.synthesizer import (  # noqa: E402
    WindowGame,
    _load_team_week_signals,
    _predictions,
    _uncertainty_block,
)
from scripts import backtest, fit_projection_model  # noqa: E402
from scripts.projection_history import load_efficiency_signals  # noqa: E402

_REPO = Path(__file__).resolve().parent.parent
OUT = backtest.CACHE_DIR / "ol_codes"
PUBLISHED_REPORT = backtest.REPORT_PATH
PUBLISHED_COEFFICIENTS = COEFFICIENTS_PATH

# The O-line codes in snaps.position that _OL_SNAP_POSITIONS misses: 2020 only, measured
# 2026-10-16 and 2026-10-18 (docs/phases/P7.md, open item 22, row 3).
EXTRA_OL_CODES = frozenset({"OT", "OG", "C/G", "G/T", "G/C", "T/G", "G/OT"})
AFFECTED_SEASONS = {2020, 2021}  # 2020 holds the codes; 2021 reads 2020 as its prior
SEASONS = "2019-2025"
STABILITY_TOLERANCE = 1e-6  # signals.stability is real (float4)

_KEY = ["season", "week", "team", "signal"]


@contextlib.contextmanager
def ol_codes(patched: bool) -> Generator[None]:
    """Efficiency's O-line set, widened by the 2020 codes while `patched`."""
    original = efficiency._OL_SNAP_POSITIONS
    efficiency._OL_SNAP_POSITIONS = set(original) | EXTRA_OL_CODES if patched else original
    try:
        yield
    finally:
        efficiency._OL_SNAP_POSITIONS = original


def _signal_names() -> list[str]:
    specs = (PRIMARY_SPEC, *backtest.VARIANT_SPECS)
    return sorted({n for spec in specs for n in spec.signal_names})


def _seasons() -> list[int]:
    start, end = (int(x) for x in SEASONS.split("-"))
    return list(range(start, end + 1))


# --------------------------------------------------------------------------------------
# 1. Patched signals
# --------------------------------------------------------------------------------------


def patched_signals(stored: pl.DataFrame) -> tuple[pl.DataFrame, dict[str, Any]]:
    production = {m.name: m for m in efficiency._METRIC_CONFIG}
    bases = sorted({b for spec in (PRIMARY_SPEC, *backtest.VARIANT_SPECS) for b in spec.bases})
    metrics = [production[b] for b in bases]
    weeks = sorted(stored.select("season", "week").unique().rows())
    print(f"1. patched efficiency: {len(weeks)} weeks, {len(metrics)} metrics ...", flush=True)
    with get_connection() as conn, ol_codes(patched=True):
        scratch = backtest._scratch_efficiency(conn, weeks, metrics, progress=True)
    scratch = scratch.with_columns(
        pl.col("season").cast(pl.Int64), pl.col("week").cast(pl.Int64)
    ).filter(pl.col("signal").is_in(stored["signal"].unique().to_list()))

    # Presence markers, not null values, say which side a row came from: a stored row can
    # carry a null value (CLAUDE.md, "Missing row ≠ NULL column").
    joined = scratch.with_columns(pl.lit(True).alias("in_scratch")).join(
        stored.with_columns(pl.lit(True).alias("in_stored")),
        on=_KEY, how="full", suffix="_stored", coalesce=True,
    ).with_columns(
        (pl.col("in_scratch").is_null() | pl.col("in_stored").is_null()).alias("unmatched"),
        (pl.col("value") - pl.col("value_stored")).abs().alias("dv"),
        (pl.col("stability") - pl.col("stability_stored")).abs().alias("ds"),
    ).with_columns(
        ((pl.col("dv") > backtest.PARITY_TOLERANCE) | (pl.col("ds") > STABILITY_TOLERANCE))
        .fill_null(False)
        .alias("changed")
    )
    unmatched = joined.filter(pl.col("unmatched"))
    if unmatched.height:
        raise RuntimeError(f"{unmatched.height} unmatched rows, e.g. {unmatched.head(5).rows()}")
    outside = joined.filter(pl.col("changed") & ~pl.col("season").is_in(list(AFFECTED_SEASONS)))
    if outside.height:
        raise RuntimeError(
            f"{outside.height} rows outside {sorted(AFFECTED_SEASONS)} differ from stored: the "
            f"scratch path doesn't reproduce production. e.g. {outside.head(5).rows()}"
        )

    changed = joined.filter(pl.col("changed"))
    patched = joined.select(
        *_KEY,
        pl.when(pl.col("changed")).then(pl.col("value")).otherwise(pl.col("value_stored"))
        .alias("value"),
        pl.when(pl.col("changed"))
        .then(pl.col("stability").cast(pl.Float32).cast(pl.Float64))
        .otherwise(pl.col("stability_stored"))
        .alias("stability"),
    ).sort(_KEY)
    by_signal = (
        changed.group_by("season", "signal")
        .agg(pl.len().alias("rows"), pl.col("dv").max().alias("max_dvalue"),
             pl.col("ds").max().alias("max_dstability"))
        .sort("season", "signal")
    )
    weeks_changed = changed.select("season", "week").unique().height
    team_weeks_changed = changed.select("season", "week", "team").unique().height
    print(f"   {changed.height} of {joined.height} rows changed, all in "
          f"{sorted(AFFECTED_SEASONS)}: {team_weeks_changed} team-weeks over "
          f"{weeks_changed} weeks", flush=True)
    return patched, {
        "rows_total": joined.height,
        "rows_changed": changed.height,
        "team_weeks_changed": team_weeks_changed,
        "by_season_signal": by_signal.to_dicts(),
    }


# --------------------------------------------------------------------------------------
# 2-3. Backtest and model file, before and after
# --------------------------------------------------------------------------------------


def _loader(signals: pl.DataFrame):
    def load(_conn: Any, seasons: Any, names: Any) -> pl.DataFrame:
        return signals.filter(
            pl.col("season").is_in(list(seasons)) & pl.col("signal").is_in(list(names))
        )
    return load


def run_backtest(label: str, signals: pl.DataFrame, patched: bool, skip_r: bool) -> Path:
    out = OUT / label
    out.mkdir(parents=True, exist_ok=True)
    report = out / "backtest_report.md"
    argv = ["backtest.py", "--seasons", SEASONS, "--out", str(report), "--refresh-r-cache"]
    if skip_r:
        argv.append("--skip-r-sensitivity")
    print(f"2. backtest, {label} ...", flush=True)
    with (
        ol_codes(patched),
        mock.patch.object(backtest, "load_efficiency_signals", _loader(signals)),
        mock.patch.object(backtest, "CACHE_DIR", out / "cache"),
        mock.patch.object(sys, "argv", argv),
    ):
        if backtest.main() != 0:
            raise RuntimeError(f"backtest ({label}) failed")
    return report


def run_fit(label: str, signals: pl.DataFrame) -> Path:
    out = OUT / label / "model_coefficients.json"
    print(f"3. model file, {label} ...", flush=True)
    with (
        mock.patch.object(fit_projection_model, "load_efficiency_signals", _loader(signals)),
        mock.patch.object(sys, "argv", ["fit_projection_model.py", "--seasons", SEASONS,
                                        "--out", str(out)]),
    ):
        if fit_projection_model.main() != 0:
            raise RuntimeError(f"fit ({label}) failed")
    return out


# --------------------------------------------------------------------------------------
# 4. /method's numbers, through the site's own summary builder
# --------------------------------------------------------------------------------------

_NODE = """
import { readFileSync } from "node:fs";
import { pathToFileURL } from "node:url";
const [mod, report, coef] = process.argv.slice(1);
const { buildSummary, tableUnder } = await import(pathToFileURL(mod).href);
const md = readFileSync(report, "utf8");
const s = buildSummary(md, JSON.parse(readFileSync(coef, "utf8")));
const all = tableUnder(md, "### Margin (home − away)").find((r) => r.Season === "**All**");
s.margin_rmse = { model: Number(all["Model RMSE"]), close: Number(all["Close RMSE"]) };
process.stdout.write(JSON.stringify(s));
"""


def method_summary(report: Path, coefficients: Path) -> dict[str, Any]:
    mod = _REPO / "web" / "scripts" / "backtest-content.mjs"
    done = subprocess.run(
        ["node", "--input-type=module", "-e", _NODE, str(mod), str(report), str(coefficients)],
        capture_output=True, text=True, encoding="utf-8", check=False,
    )
    if done.returncode != 0:
        raise RuntimeError(f"buildSummary failed on {report}: {done.stderr}")
    summary: dict[str, Any] = json.loads(done.stdout)
    coef = json.loads(coefficients.read_text(encoding="utf-8"))
    summary["calibration"] = coef["calibration"]
    return summary


def _method_rows(s: dict[str, Any]) -> dict[str, float | str]:
    """Every figure /method and the status line print, plus the bucket total SDs the
    cards carry."""
    b = s["buckets"]
    cal = s["calibration"]["buckets"]
    rows: dict[str, float | str] = {
        "margin MAE, model": s["margin_mae"]["model"],
        "margin MAE, closing line": s["margin_mae"]["close"],
        "margin RMSE, model": s["margin_rmse"]["model"],
        "margin RMSE, closing line": s["margin_rmse"]["close"],
        "spread edge r": s["spread_edge_corr"]["r"],
        "spread edge r, 95% CI": "[{}, {}]".format(*s["spread_edge_corr"]["ci"]),
        "spread edge validated": str(s["spread_edge_corr"]["validated"]),
        "validated buckets": ", ".join(s["validated_buckets"]) or "none",
        "outcome noise, 'about N points' (min bucket margin SD)": min(
            v["margin_sd"] for v in b.values()
        ),
        "stability floor": s["stability_floor"],
        "stability cutpoint low/mid": s["calibration"]["stability_cutpoints"][0],
        "stability cutpoint mid/high": s["calibration"]["stability_cutpoints"][1],
    }
    for name in ("low", "mid", "high"):
        rows[f"{name}: games"] = float(b[name]["n_games"])
        rows[f"{name}: margin SD"] = b[name]["margin_sd"]
        rows[f"{name}: total SD"] = cal[name]["total_sd"]
        rows[f"{name}: spread r"] = b[name]["spread"]["corr"]
        rows[f"{name}: total r"] = b[name]["total"]["corr"]
    m = s["model"]
    for k in ("alpha", "beta_off", "beta_def", "gamma"):
        rows[f"coef {k}"] = m[k]["value"]
        rows[f"coef {k} SE"] = m[k]["se"]
    return rows


# --------------------------------------------------------------------------------------
# 5. Today's cards
# --------------------------------------------------------------------------------------


def card_week(conn: Any) -> tuple[int, int]:
    """The latest week whose cards carry a projection. A later week's cards can exist
    before its efficiency signals do (awaiting efficiency), with nothing to compare."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT season, week FROM matchup_cards WHERE projected_spread IS NOT NULL "
            "ORDER BY season DESC, week DESC LIMIT 1"
        )
        row = cur.fetchone()
    if row is None:
        raise RuntimeError("no matchup card carries a projection")
    return int(row[0]), int(row[1])


def cards(models: dict[str, ModelFile]) -> dict[str, Any]:
    with get_connection() as conn:
        season, week = card_week(conn)
        with conn.cursor() as cur:
            cur.execute(
                "SELECT game_id, season, week, home_team, away_team, gameday, gametime, location "
                "FROM games WHERE season = %s AND week = %s ORDER BY game_id", (season, week),
            )
            games = [
                WindowGame(g, s, w, h, a, kickoff_utc(gd, gt), loc)
                for g, s, w, h, a, gd, gt, loc in cur.fetchall()
            ]
            cur.execute(
                "SELECT game_id, projected_spread, projected_total, locked FROM matchup_cards "
                "WHERE season = %s AND week = %s", (season, week),
            )
            stored = {r[0]: r[1:] for r in cur.fetchall()}
        efficiency_rows = _load_team_week_signals(conn, "efficiency", {(season, week)})
        conn.rollback()

    out: list[dict[str, Any]] = []
    preds = {label: _predictions(games, efficiency_rows, m) for label, m in models.items()}
    for g in games:
        row: dict[str, Any] = {"game_id": g.game_id}
        st = stored.get(g.game_id)
        row["stored_spread"], row["stored_total"], row["locked"] = st if st else (None, None, None)
        for label, m in models.items():
            p = preds[label].get(g.game_id)
            # The synthesizer's own guard (projection_status): no projection without
            # complete features.
            if p is None or not p["features_complete"]:
                row[f"{label}_status"] = "input null"
                continue
            u = _uncertainty_block(p, m)
            row[f"{label}_spread"] = p["projected_spread_home"]
            row[f"{label}_total"] = p["projected_total"]
            row[f"{label}_bucket"] = u["stability_bucket"]
            row[f"{label}_margin_sd"] = u["outcome_noise"]["margin_rms"]
        out.append(row)
    return {"season": season, "week": week, "games": out}


# --------------------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------------------


def _f(v: Any, nd: int = 3) -> str:
    return f"{v:.{nd}f}" if isinstance(v, float) else str(v)


def _pair(row: dict[str, Any], label: str) -> str:
    s, t = row.get(f"{label}_spread"), row.get(f"{label}_total")
    return "—" if s is None else f"{s:.2f}/{t:.2f}"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--skip-r-sensitivity", action="store_true",
                        help="both backtests skip their r-sensitivity section")
    args = parser.parse_args()
    sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[union-attr]  # "—", "Δ" on Windows
    started = time.monotonic()
    OUT.mkdir(parents=True, exist_ok=True)

    with get_connection() as conn:
        stored = load_efficiency_signals(conn, _seasons(), _signal_names())
    patched, change = patched_signals(stored)
    patched.write_parquet(OUT / "patched_signals.parquet")

    reports = {
        "before": run_backtest("before", stored, False, args.skip_r_sensitivity),
        "after": run_backtest("after", patched, True, args.skip_r_sensitivity),
    }
    coefficients = {"before": run_fit("before", stored), "after": run_fit("after", patched)}

    summaries = {"published": method_summary(PUBLISHED_REPORT, PUBLISHED_COEFFICIENTS)}
    for label in ("before", "after"):
        summaries[label] = method_summary(reports[label], coefficients[label])
    for label, s in summaries.items():
        (OUT / f"summary_{label}.json").write_text(json.dumps(s, indent=2), encoding="utf-8")
    rows = {label: _method_rows(s) for label, s in summaries.items()}

    card_result = cards({label: load_model_file(path) for label, path in coefficients.items()})

    print("\n4. /method, published vs. before vs. after")
    print(f"{'figure':<58} {'published':>12} {'before':>12} {'after':>12} {'after-before':>13}")
    for k in rows["published"]:
        p, b, a = rows["published"][k], rows["before"][k], rows["after"][k]
        d = _f(a - b, 4) if isinstance(a, float) and isinstance(b, float) else ""
        print(f"{k:<58} {_f(p):>12} {_f(b):>12} {_f(a):>12} {d:>13}")

    print(f"\n5. cards, {card_result['season']} week {card_result['week']} "
          "(spread is home, market convention)")
    print(f"{'game':<18} {'locked':>6} {'stored':>14} {'before':>14} {'after':>14} "
          f"{'Δspread':>8} {'Δtotal':>7} {'bucket':>11} {'margin SD':>13}")
    for r in card_result["games"]:
        ds = dt = ""
        if r.get("after_spread") is not None and r.get("before_spread") is not None:
            ds = f"{r['after_spread'] - r['before_spread']:+.2f}"
            dt = f"{r['after_total'] - r['before_total']:+.2f}"
        bucket = f"{r.get('before_bucket', '—')}>{r.get('after_bucket', '—')}"
        sd = (f"{r['before_margin_sd']:.2f}>{r['after_margin_sd']:.2f}"
              if r.get("after_margin_sd") is not None else "—")
        stored_pair = ("—" if r["stored_spread"] is None
                       else f"{float(r['stored_spread']):.2f}/{float(r['stored_total']):.2f}")
        print(f"{r['game_id']:<18} {str(r['locked']):>6} {stored_pair:>14} "
              f"{_pair(r, 'before'):>14} {_pair(r, 'after'):>14} {ds:>8} {dt:>7} "
              f"{bucket:>11} {sd:>13}")

    result = {
        "change": change,
        "method": rows,
        "cards": card_result,
        "reports": {k: v.relative_to(_REPO).as_posix() for k, v in reports.items()},
        "coefficients": {k: v.relative_to(_REPO).as_posix() for k, v in coefficients.items()},
        "seconds": round(time.monotonic() - started),
    }
    (OUT / "result.json").write_text(json.dumps(result, indent=2, default=str), encoding="utf-8")
    print(f"\nwrote {(OUT / 'result.json').relative_to(_REPO).as_posix()} "
          f"({result['seconds']}s)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
