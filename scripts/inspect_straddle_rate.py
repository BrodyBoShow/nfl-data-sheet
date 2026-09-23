"""One-off script: how often does `spread_key_straddle` fire? It computes the Market
analyst's own `key_straddle()` for every stored odds capture of a season, using the
analyst's own capture loader. Nothing is reimplemented (CLAUDE.md's note on
verification scripts).

A capture counts only with 2+ books carrying a spread, matching the signal (no row
with fewer). Captures are grouped by poll time (`as_of`), and each poll shows its games,
straddles, rate, and median book range.

Not part of the pipeline. Read-only.

Usage:
  uv run python scripts/inspect_straddle_rate.py
  uv run python scripts/inspect_straddle_rate.py --season 2026
"""

from __future__ import annotations

import argparse
import statistics
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pipeline.analysts.market import _load_captures, key_straddle  # noqa: E402
from pipeline.core.db import get_connection  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--season", type=int, default=2026)
    args = parser.parse_args()

    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT DISTINCT c.game_id FROM odds_consensus c JOIN games g USING (game_id) "
                "WHERE g.season = %s",
                (args.season,),
            )
            game_ids = [r[0] for r in cur.fetchall()]
        captures = _load_captures(conn, game_ids)
        conn.rollback()

    by_poll: dict[str, list[tuple[bool, float | None]]] = {}
    for caps in captures.values():
        for cap in caps:
            spreads = [b.spread_home for b in cap.books if b.spread_home is not None]
            if len(spreads) < 2:
                continue
            by_poll.setdefault(cap.as_of.strftime("%Y-%m-%d %H:%M"), []).append(
                (key_straddle(spreads), cap.spread_range)
            )

    print(f"season {args.season}: {len(game_ids)} games with captures\n")
    print(f"{'poll (UTC)':<18}{'games':>6}{'straddle':>9}{'rate':>7}{'median range':>14}")
    total = fired = 0
    for poll in sorted(by_poll):
        rows = by_poll[poll]
        n, k = len(rows), sum(s for s, _ in rows)
        ranges = [r for _, r in rows if r is not None]
        med = statistics.median(ranges) if ranges else None
        total, fired = total + n, fired + k
        med_str = f"{med:.2f}" if med is not None else "-"
        print(f"{poll:<18}{n:>6}{k:>9}{k / n:>7.0%}{med_str:>14}")
    if total:
        print(f"\nall captures: {fired}/{total} = {fired / total:.0%}")


if __name__ == "__main__":
    main()
