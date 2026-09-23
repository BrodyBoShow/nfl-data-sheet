"""One-off script: backfill point-in-time Efficiency signals for completed historical
seasons (P5 prerequisite -- the projection model is fit and backtested on these).

For each season, runs the PRODUCTION `EfficiencyAnalyst().run(season, week, force=True)`
once per regular-season week, 1..last REG week staged in `team_week`. Nothing is
reimplemented here (CLAUDE.md's verification-script convention). Each run writes the same
rows a live run would have: week-N signals built only from weeks 1..N-1 of that season
plus the completed prior season. `tests/test_efficiency.py`'s
`test_compute_ignores_future_weeks_end_to_end` pins that guarantee.

REG weeks only: postseason games are excluded from the P5 fit/backtest (see
docs/phases/P5.md), so their entering-week signals aren't needed.

Refuses the current season -- backfill is for completed seasons; live weeks come from the
dispatcher. Stops at the first run that doesn't succeed. Every run is idempotent (upsert
plus the analyst's own stale-row delete), so re-running the whole range after a failure is
safe.

Writes: signals (via the analyst), agent_runs (one row per week).

Usage:
  uv run python scripts/backfill_efficiency.py --seasons 2019-2025 --dry-run
  uv run python scripts/backfill_efficiency.py --seasons 2019-2025
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

import nflreadpy as nfl

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pipeline.analysts.efficiency import EfficiencyAnalyst  # noqa: E402
from pipeline.core.db import get_connection  # noqa: E402


def _last_reg_week_by_season(seasons: list[int]) -> dict[int, int]:
    """Last REG week staged in team_week per season -- read from the data, not assumed
    (17 weeks through 2020, 18 from 2021)."""
    with get_connection() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT season, max(week) FROM team_week "
            "WHERE season_type = 'REG' AND season = ANY(%s) GROUP BY season",
            (seasons,),
        )
        return {season: week for season, week in cur.fetchall()}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--seasons", default="2019-2025")
    parser.add_argument(
        "--dry-run", action="store_true", help="print the (season, week) plan and exit"
    )
    args = parser.parse_args()
    start, end = (int(x) for x in args.seasons.split("-"))
    seasons = list(range(start, end + 1))

    current_season = nfl.get_current_season()
    if any(s >= current_season for s in seasons):
        print(
            f"refusing: {args.seasons} includes the current season ({current_season}) or "
            "later -- backfill completed seasons only",
            file=sys.stderr,
        )
        return 1

    last_week = _last_reg_week_by_season(seasons)
    missing = [s for s in seasons if s not in last_week]
    if missing:
        print(f"refusing: no REG team_week rows staged for {missing}", file=sys.stderr)
        return 1

    plan = [(s, w) for s in seasons for w in range(1, last_week[s] + 1)]
    for s in seasons:
        print(f"season {s}: weeks 1-{last_week[s]}")
    print(f"{len(plan)} runs planned")
    if args.dry_run:
        return 0

    analyst = EfficiencyAnalyst()
    total_rows = 0
    started = time.monotonic()
    for i, (season, week) in enumerate(plan, start=1):
        result = analyst.run(season=season, week=week, force=True)
        total_rows += result.rows_written
        print(
            f"[{i}/{len(plan)}] {season} wk{week:>2}: {result.status} "
            f"rows={result.rows_written} {result.duration_s:.1f}s",
            flush=True,
        )
        if result.status != "success":
            print(f"stopping: {result.error or result.status}", file=sys.stderr)
            return 1

    elapsed = time.monotonic() - started
    print(f"done: {len(plan)} runs, {total_rows} rows written, {elapsed / 60:.1f} min")
    return 0


if __name__ == "__main__":
    sys.exit(main())
