"""
Job: Manual CLI to run a single named collector/analyst outside the dispatcher's tick,
     for local debugging and one-off backfills (`uv run python -m pipeline.run <job>`).
Reads: nothing directly (delegates to the named job)
Writes: nothing directly (delegates to the named job)
Tier: n/a
Phase: P1
"""

from __future__ import annotations

import sys

import nflreadpy as nfl

from pipeline.collectors.id_spine import IdSpineCollector
from pipeline.collectors.nflverse_bulk import NflverseBulkCollector

_JOBS = {
    "id_spine": IdSpineCollector(),
    "nflverse_bulk": NflverseBulkCollector(),
}


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if len(argv) != 1 or argv[0] not in _JOBS:
        names = ", ".join(sorted(_JOBS))
        print("usage: uv run python -m pipeline.run <job>", file=sys.stderr)
        print(f"available jobs: {names}", file=sys.stderr)
        return 1

    job = _JOBS[argv[0]]
    result = job.run(season=nfl.get_current_season(), week=nfl.get_current_week())

    if result.status == "success":
        rows = f"{result.rows_written:,} rows written"
        print(f"{result.name}: success, {rows}, {result.duration_s:.1f}s")
    elif result.status == "skipped_fresh":
        print(f"{result.name}: skipped_fresh (no source changes)")
    else:
        print(f"{result.name}: {result.status} - {result.error}")

    return 0 if result.status in ("success", "skipped_fresh") else 1


if __name__ == "__main__":
    sys.exit(main())
