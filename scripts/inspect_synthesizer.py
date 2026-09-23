"""One-off script: show what the matchup synthesizer would write right now. It calls
MatchupSynthesizer.compute() itself, never a hand-written reconstruction (CLAUDE.md's
note on verification scripts). Per game in the window it shows:

- projection_status
- projected spread/total
- the stability bucket (no per-game band: outcome noise is ~13 points for every game)
- the market line and the edge vs. it, with flags
- whether this run would lock the game

Not part of the pipeline. Read-only: compute() only reads, and the transaction is rolled
back regardless. Needs migrations 0023/0024 applied (compute() reads projection_log's
new columns).

Usage:
  uv run python scripts/inspect_synthesizer.py
  uv run python scripts/inspect_synthesizer.py --game 2026_03_KC_MIA   # full card JSON
  uv run python scripts/inspect_synthesizer.py --at 2026-09-27T14:00Z  # pretend "now"
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pipeline.core.base import RunContext  # noqa: E402
from pipeline.core.config import get_settings  # noqa: E402
from pipeline.core.db import get_connection  # noqa: E402
from pipeline.synthesis.synthesizer import MatchupSynthesizer  # noqa: E402


def _fmt(v: Any, signed: bool = True) -> str:
    if v is None:
        return "-"
    if not isinstance(v, float):
        return str(v)
    return f"{v:+.1f}" if signed else f"{v:.1f}"


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--game", action="append", default=[])
    parser.add_argument("--at", help="ISO timestamp to use as now (UTC)")
    args = parser.parse_args()
    now = (
        datetime.fromisoformat(args.at.replace("Z", "+00:00")) if args.at else datetime.now(UTC)
    )

    with get_connection() as conn:
        try:
            ctx = RunContext(0, 0, "REG", now, get_settings(), conn)
            result = MatchupSynthesizer().compute(ctx)
        finally:
            conn.rollback()

    print(f"now {now.isoformat()}  meta: {json.dumps(result.meta)}\n")
    locking = set(result.meta["locking"])
    print(f"{'game':<18}{'st':>3}{'spread':>8}{'total':>7}{'bucket':>7}"
          f"{'mkt sp':>8}{'edge':>7}{'mkt tot':>9}{'edge':>7}  flags / lock")
    for row in result.cards:
        card = json.loads(row["card"])
        unc = card["uncertainty"] or {}
        edge = card["edge"]["vs_current"]
        flags = [k for k, v in edge["flags"].items() if v]
        lock = "LOCKING" if row["game_id"] in locking else (
            "locked" if card["lock"]["locked"] else "")
        print(
            f"{row['game_id']:<18}{row['projection_status']:>3}"
            f"{_fmt(row['projected_spread']):>8}{_fmt(row['projected_total'], False):>7}"
            f"{unc.get('stability_bucket') or '-':>7}"
            f"{_fmt(edge['market_spread']):>8}{_fmt(edge['spread']):>7}"
            f"{_fmt(edge['market_total'], False):>9}{_fmt(edge['total']):>7}  "
            f"{','.join(flags)} {lock}"
        )
        if row["game_id"] in args.game:
            print(json.dumps(card, indent=2))


if __name__ == "__main__":
    main()
