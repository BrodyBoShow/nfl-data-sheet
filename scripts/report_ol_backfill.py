"""One-off report: Phase B of P7 open item 22, row 3 (docs/phases/P7.md). Read-only.

Two modes:

- `preflight` (B1): the stored 2020-2021 Efficiency signals still equal the Phase A
  snapshot, every key and column. The 10-24 lesson: a plan that rests on stored state
  re-checks it first thing. Exits 1 on any difference.
- `after --since <UTC>` (the B2 report), after `scripts/backfill_efficiency.py --seasons
  2020-2021`:
  1. Rows changed by season and signal, against the snapshot, and the magnitude
     distribution.
  2. Parity against the measurement's patched signals (`scripts/measure_ol_codes.py`),
     which are what the fix was measured to produce.
  3. Item 7, the production-unit count: every team's `ol_factor` from the old backfill
     runs against the new ones, out of `agent_runs.meta`. Beside it, D (team-weeks whose
     `epa_per_play_off` stability moved, the 10-19 trace's unit), from the stored rows.

Nothing here recomputes a signal or a factor (CLAUDE.md's verification-script
convention): it compares what the production analyst stored and logged.

How a run is tied to its (season, week), since Efficiency's run meta doesn't carry them:
- **Old runs:** each stored week has one `as_of`, which falls inside exactly one
  Efficiency run's [started_at, finished_at] (checked live 2026-10-28: 35 of 35 weeks,
  35 distinct runs). The snapshot's `as_of` names the run.
- **New runs:** the backfill runs in order, 2020 weeks 1-17 then 2021 weeks 1-18, with
  the dispatcher off. So the runs since `--since` must be exactly 35, and each season's
  first run has no current O-line for any team (a week-1 run has no current-season
  games). Any week whose stored `as_of` moved must fall inside the run that order
  assigns it. Any mismatch stops the report.

Usage:
  uv run python scripts/report_ol_backfill.py preflight
  uv run python scripts/report_ol_backfill.py after --since 2026-10-28T03:00:00Z
"""

from __future__ import annotations

import argparse
import datetime as dt
import sys
from pathlib import Path
from typing import Any

import polars as pl

_REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO))

from pipeline.core.db import get_connection  # noqa: E402

SNAPSHOT = _REPO / ".cache/backups/2026-10-20_ol_vocab/efficiency_2020_2021.parquet"
PATCHED = _REPO / ".cache/backtest/ol_codes/patched_signals.parquet"
SEASONS = (2020, 2021)
KEY = ["season", "week", "team", "signal"]
COLS = ["value", "sample_n", "stability", "as_of"]
# A re-backfill that reproduces a value differs by ~5e-16 (model.py, fit_inputs_hash).
PARITY_TOL = 1e-9
# `signals.stability` is real, and reads back at ~6 significant digits on this connection
# (model.py, fit_inputs_hash): up to 5.3e-7 from the float32 the patched parquet holds.
# Stored-vs-snapshot compares exactly, because both are the same read path.
STAB_TOL = 1e-6

_STORED_SQL = (
    "SELECT season, week, team, signal, value, sample_n, stability, as_of "
    "FROM signals WHERE sector = 'efficiency' AND season = ANY(%s)"
)
_RUNS_SQL = (
    "SELECT id, started_at, finished_at, status, meta FROM agent_runs "
    "WHERE agent = 'efficiency' AND started_at >= %s ORDER BY id"
)
_RUN_AT_SQL = (
    "SELECT id, meta FROM agent_runs WHERE agent = 'efficiency' "
    "AND %s BETWEEN started_at AND finished_at"
)


def _stored(conn: Any) -> pl.DataFrame:
    with conn.cursor() as cur:
        cur.execute(_STORED_SQL, (list(SEASONS),))
        rows = cur.fetchall()
    return pl.DataFrame(
        rows,
        schema={
            "season": pl.Int64,
            "week": pl.Int64,
            "team": pl.String,
            "signal": pl.String,
            "value": pl.Float64,
            "sample_n": pl.Int64,
            "stability": pl.Float64,
            "as_of": pl.Datetime("us", "UTC"),
        },
        orient="row",
    )


def _snapshot() -> pl.DataFrame:
    return pl.read_parquet(SNAPSHOT).select([*KEY, *COLS])


def _joined(pre: pl.DataFrame, post: pl.DataFrame) -> tuple[pl.DataFrame, int, int]:
    """Inner join on KEY, plus the counts of keys only in pre and only in post."""
    only_pre = pre.join(post, on=KEY, how="anti").height
    only_post = post.join(pre, on=KEY, how="anti").height
    return pre.join(post, on=KEY, suffix="_post"), only_pre, only_post


def _differs(c: str) -> pl.Expr:
    a, b = pl.col(c), pl.col(f"{c}_post")
    return (a != b) | (a.is_null() != b.is_null())


def preflight() -> int:
    pre = _snapshot()
    with get_connection() as conn:
        conn.read_only = True
        post = _stored(conn)
    j, only_pre, only_post = _joined(pre, post)
    diffs = {c: j.filter(_differs(c)).height for c in COLS}
    print(f"snapshot {pre.height} rows, stored {post.height} rows")
    print(f"keys only in snapshot {only_pre}, only stored {only_post}")
    for c, n in diffs.items():
        print(f"  {c}: {n} rows differ")
    ok = only_pre == 0 and only_post == 0 and not any(diffs.values())
    print("PREFLIGHT OK: stored 2020-2021 == snapshot" if ok else "PREFLIGHT FAILED", flush=True)
    return 0 if ok else 1


def _quantiles(s: pl.Series) -> str:
    if s.len() == 0:
        return "n=0"
    q = [s.quantile(p) for p in (0.5, 0.9, 0.99)]
    return f"n={s.len()} median {q[0]:.2e} p90 {q[1]:.2e} p99 {q[2]:.2e} max {s.max():.2e}"


def _max_abs(s: pl.Series) -> float:
    m = s.abs().drop_nulls().max()
    return float(m) if m is not None else 0.0  # type: ignore[arg-type]


def _team_factors(meta: dict[str, Any]) -> dict[str, float]:
    return {t: float(m["ol_factor"]) for t, m in meta["teams"].items()}


def _no_current_ol(meta: dict[str, Any]) -> bool:
    return all(len(m["current_ol"]) == 0 for m in meta["teams"].values())


def after(since: dt.datetime) -> int:
    pre = _snapshot()
    with get_connection() as conn:
        conn.read_only = True
        post = _stored(conn)
        with conn.cursor() as cur:
            cur.execute(_RUNS_SQL, (since,))
            new_runs = cur.fetchall()
            old_meta: dict[tuple[int, int], dict[str, Any]] = {}
            for (season, week), g in pre.group_by(["season", "week"]):
                as_of = g["as_of"].unique()
                if as_of.len() != 1:
                    print(f"STOP: snapshot {season} wk {week} has {as_of.len()} as_of values")
                    return 1
                cur.execute(_RUN_AT_SQL, (as_of[0],))
                found = cur.fetchall()
                if len(found) != 1:
                    print(f"STOP: snapshot {season} wk {week} as_of matches {len(found)} runs")
                    return 1
                old_meta[(int(season), int(week))] = found[0][1]  # type: ignore[index]

    # 1. Changed rows.
    j, only_pre, only_post = _joined(pre, post)
    changed = j.filter(_differs("value") | _differs("stability") | _differs("sample_n"))
    print(f"\n== 1. Rows: snapshot {pre.height}, stored {post.height}, "
          f"only snapshot {only_pre}, only stored {only_post}, changed {changed.height}")
    for season in SEASONS:
        cs = changed.filter(pl.col("season") == season)
        total = j.filter(pl.col("season") == season).height
        print(f"\n{season}: {cs.height} of {total} rows changed")
        by_signal = cs.group_by("signal").len().sort("signal")
        for sig, n in by_signal.iter_rows():
            print(f"  {sig:<28} {n}")
        for side in ("_off", "_def"):
            s = cs.filter(pl.col("signal").str.ends_with(side))
            dv = (s["value_post"] - s["value"]).abs()
            ds = (s["stability_post"] - s["stability"]).abs()
            print(f"  {side} |dvalue| {_quantiles(dv)}")
            print(f"  {side} |dstab|  {_quantiles(ds)}")
        sample = cs.filter(_differs("sample_n")).height
        print(f"  sample_n changed: {sample} (expected 0: the code set doesn't touch samples)")

    # 2. Parity with the measurement.
    patched = pl.read_parquet(PATCHED).filter(pl.col("season").is_in(list(SEASONS)))
    pj = patched.join(post, on=KEY, how="left", suffix="_post")
    missing = pj.filter(pl.col("value_post").is_null() & pl.col("value").is_not_null()).height
    dv = _max_abs(pj["value_post"] - pj["value"])
    ds = _max_abs(pj["stability_post"] - pj["stability"])
    parity_ok = missing == 0 and dv <= PARITY_TOL and ds <= STAB_TOL
    in_fit = changed.join(patched.select(KEY), on=KEY, how="semi").height
    print(f"\nChanged rows among the measurement's signals: {in_fit} of {patched.height} "
          "(10-19 trace: 7,936)")
    print(f"\n== 2. Parity with the measurement's patched signals ({patched.height} rows, "
          f"2020-2021): missing {missing}, max |dvalue| {dv:.2e} (tol {PARITY_TOL:g}), "
          f"max |dstab| {ds:.2e} (tol {STAB_TOL:g}) -> "
          f"{'OK' if parity_ok else 'MISMATCH'}")

    # 3. Item 7.
    weeks = sorted(old_meta)
    print(f"\n== 3. Item 7: new runs since {since.isoformat()}: {len(new_runs)} "
          f"(expected {len(weeks)})")
    if len(new_runs) != len(weeks):
        print("STOP: run count doesn't match the backfill plan")
        return 1
    new_meta: dict[tuple[int, int], dict[str, Any]] = {}
    for (season, week), run in zip(weeks, new_runs, strict=True):
        run_id, started, finished, status, meta = run
        if status != "success":
            print(f"STOP: run {run_id} ({season} wk {week}) status {status}")
            return 1
        if _no_current_ol(meta) != (week == 1):
            print(f"STOP: run {run_id} assigned {season} wk {week}, but its current O-line "
                  f"{'is' if _no_current_ol(meta) else 'is not'} empty")
            return 1
        moved = post.filter(
            (pl.col("season") == season) & (pl.col("week") == week) & (pl.col("as_of") >= since)
        )["as_of"].unique()
        if moved.len() > 1 or (moved.len() == 1 and not started <= moved[0] <= finished):
            print(f"STOP: {season} wk {week}'s stored as_of isn't inside run {run_id}")
            return 1
        new_meta[(season, week)] = meta

    stab = j.filter(pl.col("signal") == "epa_per_play_off")
    moved_stab = stab.filter(_differs("stability")).select("season", "week", "team")
    d_set = {(s, w, t) for s, w, t in moved_stab.iter_rows()}
    for season in SEASONS:
        sw = [k for k in weeks if k[0] == season]
        factor_set: set[tuple[int, int, str]] = set()
        total = 0
        for key in sw:
            old, new = _team_factors(old_meta[key]), _team_factors(new_meta[key])
            if set(old) != set(new):
                print(f"STOP: {key} team sets differ between old and new runs")
                return 1
            total += len(old)
            factor_set |= {(key[0], key[1], t) for t in old if old[t] != new[t]}
        d_season = {k for k in d_set if k[0] == season}
        print(f"\n{season}: ol_factor changed in {len(factor_set)} of {total} as-of team-weeks "
              f"(REG, {len(sw)} weeks x teams)")
        print(f"  D (epa_per_play_off stability moved): {len(d_season)}")
        print(f"  same team-weeks: {factor_set == d_season} "
              f"(factor only {len(factor_set - d_season)}, D only {len(d_season - factor_set)})")
    print("\nThe 10-16 counts (119/672, 172/704) were game weeks, REG and POST, from a scratch "
          "script that wasn't kept. Compare the counts above against them by unit.")
    return 0 if parity_ok else 1


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="mode", required=True)
    sub.add_parser("preflight")
    a = sub.add_parser("after")
    a.add_argument("--since", required=True, help="UTC start of the B2 backfill, ISO 8601")
    args = parser.parse_args()
    if args.mode == "preflight":
        return preflight()
    since = dt.datetime.fromisoformat(args.since.replace("Z", "+00:00"))
    if since.tzinfo is None:
        print("refusing: --since needs a UTC offset", file=sys.stderr)
        return 1
    return after(since)


if __name__ == "__main__":
    sys.exit(main())
