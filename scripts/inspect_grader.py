"""One-off script: show what the grader would write right now. It calls
ProjectionGrader.compute() itself, never a hand-written reconstruction (CLAUDE.md's note
on verification scripts). It prints:

- one line per in-scope game: grade status, the lock-line parity check, CLV (own and
  nflverse) and the pick results against both lines
- every grade_summary row that isn't `insufficient_n`, plus the lock rate

Not part of the pipeline. Read-only: compute() only reads, and the transaction is rolled
back regardless. compute() doesn't read projection_grades, so this runs before migration
0025 is applied.

Usage:
  uv run python scripts/inspect_grader.py
  uv run python scripts/inspect_grader.py --at 2026-09-25T12:00Z   # pretend "now"
  uv run python scripts/inspect_grader.py --all-summary            # include insufficient_n
"""

from __future__ import annotations

import argparse
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pipeline.core.base import RunContext  # noqa: E402
from pipeline.core.config import get_settings  # noqa: E402
from pipeline.core.db import get_connection  # noqa: E402
from pipeline.orchestration.grader import ProjectionGrader  # noqa: E402


def _fmt(v: Any) -> str:
    if v is None:
        return "-"
    if isinstance(v, float):
        return f"{v:+.2f}"
    return str(v)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--at", help="ISO timestamp to use as now (UTC)")
    parser.add_argument("--all-summary", action="store_true")
    args = parser.parse_args()
    now = (
        datetime.fromisoformat(args.at.replace("Z", "+00:00")) if args.at else datetime.now(UTC)
    )

    with get_connection() as conn:
        try:
            ctx = RunContext(0, 0, "REG", now, get_settings(), conn)
            result = ProjectionGrader().compute(ctx)
        finally:
            conn.rollback()

    print(f"now {now.isoformat()}  meta: {result.meta}\n")
    print(f"{'game':<18} {'status':<16} {'parity':<6} {'clv_own_s':>9} {'clv_nflv_s':>10} "
          f"{'clv_nflv_t':>10} {'ats_lock':<8} {'ou_lock':<8} {'ats_nflv':<8} last_card")
    for r in result.grades:
        print(f"{r['game_id']:<18} {r['grade_status']:<16} {_fmt(r['lock_line_parity']):<6} "
              f"{_fmt(r['clv_own_spread']):>9} {_fmt(r['clv_nflv_spread']):>10} "
              f"{_fmt(r['clv_nflv_total']):>10} {_fmt(r['ats_lock']):<8} "
              f"{_fmt(r['ou_lock']):<8} {_fmt(r['ats_nflv']):<8} "
              f"{_fmt(r['last_card_status'])}")

    print("\nsummary:")
    for r in result.summary:
        if not args.all_summary and r["verdict"] == "insufficient_n" \
                and r["metric"] != "lock_rate":
            continue
        ci = f"[{_fmt(r['ci_low'])}, {_fmt(r['ci_high'])}]"
        print(f"  {r['slice']:<32} {r['metric']:<28} n={r['n']:<5} "
              f"{_fmt(r['estimate']):>7} {ci:<18} {r['verdict']}")


if __name__ == "__main__":
    main()
