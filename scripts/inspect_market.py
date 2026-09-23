"""One-off script: show what the Market analyst would write right now -- per game in its
window: market_status, open/current lines, movement and the book-set guard, disagreement,
key numbers, and each team's implied total and no-vig win probability -- by calling
MarketAnalyst.compute() itself, never a hand-written SQL reconstruction (CLAUDE.md's note
on verification scripts).

Not part of the pipeline. Read-only: compute() only reads, and the transaction is rolled
back regardless.

Usage:
  uv run python scripts/inspect_market.py
  uv run python scripts/inspect_market.py --game 2026_03_KC_MIA
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

from pipeline.analysts.market import MarketAnalyst  # noqa: E402
from pipeline.core.base import RunContext  # noqa: E402
from pipeline.core.config import get_settings  # noqa: E402
from pipeline.core.db import get_connection  # noqa: E402

_GAME_COLS = [
    "market_status",
    "market_own_week_captures",
    "market_open_basis",
    "market_open_lead_hours",
    "market_current_lead_hours",
    "spread_home_open",
    "spread_home_current",
    "spread_home_move",
    "spread_book_set_changed",
    "spread_book_count_open",
    "spread_book_count_current",
    "spread_book_range",
    "spread_key_crossings",
    "spread_key_straddle",
    "total_open",
    "total_current",
    "total_move",
    "total_book_set_changed",
    "total_book_range",
]
_TEAM_COLS = ["implied_team_total", "win_prob_novig"]


def _fmt(v: Any) -> str:
    if v is None:
        return "-"
    return f"{v:.3f}".rstrip("0").rstrip(".") if isinstance(v, float) else str(v)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--game", action="append", default=[])
    args = parser.parse_args()

    analyst = MarketAnalyst()
    with get_connection() as conn:
        try:
            ctx = RunContext(0, 0, "REG", datetime.now(UTC), get_settings(), conn)
            rows = analyst.compute(ctx).to_dicts()
        finally:
            conn.rollback()

    by_game: dict[str, dict[str, Any]] = {}
    for r in rows:
        g = by_game.setdefault(
            r["game_id"],
            {"week": r["week"], "version": r["inputs_version"], "game": {}, "teams": {}},
        )
        if r["team"] is None:
            g["game"][r["signal"]] = r["value"]
        else:
            g["teams"].setdefault(r["team"], {})[r["signal"]] = r["value"]

    print(f"window games: {len(by_game)}  meta: {analyst._meta}")
    for game_id in sorted(by_game):
        if args.game and game_id not in args.game:
            continue
        g = by_game[game_id]
        print(f"\n{game_id} (week {g['week']})  {g['version']}")
        present = [c for c in _GAME_COLS if c in g["game"]]
        print("  " + "  ".join(f"{c}={_fmt(g['game'][c])}" for c in present))
        for team, sig in sorted(g["teams"].items()):
            print(f"  {team:>3}: " + "  ".join(f"{c}={_fmt(sig.get(c))}" for c in _TEAM_COLS))


if __name__ == "__main__":
    main()
