"""One-off script: show what the Environment analyst would write right now -- per game in
its window: weather_status, roof/surface codes, the headline wind shape, and each team's
rest/travel/timezone -- by calling EnvironmentAnalyst.compute() itself, never a
hand-written SQL reconstruction (CLAUDE.md's note on verification scripts).

Not part of the pipeline. Read-only: compute() only reads, and the transaction is rolled
back regardless.

Usage:
  uv run python scripts/inspect_environment.py
  uv run python scripts/inspect_environment.py --game 2026_03_ATL_GB
"""

from __future__ import annotations

import argparse
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

# `pipeline` is only importable with the project root on sys.path (see
# scripts/inspect_qb_continuity.py).
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pipeline.analysts.environment import EnvironmentAnalyst  # noqa: E402
from pipeline.core.base import RunContext  # noqa: E402
from pipeline.core.config import get_settings  # noqa: E402
from pipeline.core.db import get_connection  # noqa: E402

_GAME_COLS = [
    "weather_status",
    "venue_roof_code",
    "surface_code",
    "wind_direction_mode",
    "wind_speed_mph",
    "wind_along_field_mph",
    "wind_crosswind_mph",
    "temperature_f",
    "precip_total_in",
    "weather_lead_hours",
    "weather_forecast_domain",
    "venue_elevation_m",
]
_TEAM_COLS = [
    "rest_days",
    "rest_diff",
    "travel_miles",
    "tz_shift_hours",
    "tz_offset_diff_raw_hours",
]


def _fmt(v: Any) -> str:
    if v is None:
        return "-"
    return f"{v:.1f}" if isinstance(v, float) and v != int(v) else f"{v:g}"


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--game", action="append", default=[])
    args = parser.parse_args()

    analyst = EnvironmentAnalyst()
    with get_connection() as conn:
        try:
            ctx = RunContext(0, 0, "REG", datetime.now(UTC), get_settings(), conn)
            rows = analyst.compute(ctx).to_dicts()
        finally:
            conn.rollback()

    by_game: dict[str, dict[str, Any]] = {}
    for r in rows:
        g = by_game.setdefault(r["game_id"], {"week": r["week"], "game": {}, "teams": {}})
        if r["team"] is None:
            g["game"][r["signal"]] = r["value"]
        else:
            g["teams"].setdefault(r["team"], {})[r["signal"]] = r["value"]

    print(f"window games: {len(by_game)}  meta: {analyst._meta}")
    for game_id in sorted(by_game):
        if args.game and game_id not in args.game:
            continue
        g = by_game[game_id]
        print(f"\n{game_id} (week {g['week']})")
        print("  " + "  ".join(f"{c}={_fmt(g['game'].get(c))}" for c in _GAME_COLS))
        for team, sig in sorted(g["teams"].items()):
            print(f"  {team:>3}: " + "  ".join(f"{c}={_fmt(sig.get(c))}" for c in _TEAM_COLS))


if __name__ == "__main__":
    main()
