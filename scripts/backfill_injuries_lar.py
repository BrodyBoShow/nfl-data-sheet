"""
Job: One-off backfill (P8 D19, step 1 only): rewrite injuries.team 'LAR' -> 'LA' on the
     rows written before the alias, with content_hash recomputed by the Availability
     collector's own _finalize. raw is untouched and keeps the source's 'LAR'.
Reads: injuries; Availability impact's inputs (via its own compute), for the check
Writes: injuries (team, content_hash, updated_at) -- only with --commit
Tier: OD
Phase: P8

In one transaction: Availability impact's compute() for the given week, the rewrite, and
compute() again. The rewrite commits only with --commit AND only if the two outputs differ
in Rams rows alone (LA team rows, or players whose players.latest_team is LA). Otherwise
it rolls back. It writes no signal: the next Availability impact run does that.

    uv run python scripts/backfill_injuries_lar.py [--season 2026 --week 6] [--commit]
"""

from __future__ import annotations

import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

# `pipeline` is only importable with the project root on sys.path (see
# scripts/inspect_qb_continuity.py).
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pipeline.analysts.availability_impact import AvailabilityImpactAnalyst  # noqa: E402
from pipeline.collectors.availability import (  # noqa: E402
    _INJURIES_COLS,
    _INJURIES_PK,
    _finalize,
)
from pipeline.core.base import RunContext  # noqa: E402
from pipeline.core.config import get_settings  # noqa: E402
from pipeline.core.db import get_connection  # noqa: E402

_FROM, _TO = "LAR", "LA"
_EXPECTED_ROWS = 363  # counted 2026-10-15 (P8 D19); a different count stops the run


def _outputs(ctx: RunContext) -> dict[tuple[Any, ...], tuple[Any, ...]]:
    df = AvailabilityImpactAnalyst().compute(ctx)
    return {
        (r["team"], r["player_id"], r["signal"]): (r["value"], r["sample_n"])
        for r in df.to_dicts()
    }


def main(argv: list[str]) -> int:
    season = int(argv[argv.index("--season") + 1]) if "--season" in argv else 2026
    week = int(argv[argv.index("--week") + 1]) if "--week" in argv else 6
    commit = "--commit" in argv
    now = datetime.now(UTC)
    hash_fields = [c for c in _INJURIES_COLS if c not in _INJURIES_PK]

    with get_connection() as conn:
        ctx = RunContext(season, week, "REG", now, get_settings(), conn)
        try:
            before = _outputs(ctx)

            with conn.cursor() as cur:
                cur.execute(
                    f"SELECT {', '.join(_INJURIES_COLS)} FROM injuries WHERE team = %s",
                    (_FROM,),
                )
                rows = [dict(zip(_INJURIES_COLS, r, strict=True)) for r in cur.fetchall()]
            print(f"injuries rows with team {_FROM}: {len(rows)}")
            if len(rows) != _EXPECTED_ROWS:
                print(f"expected {_EXPECTED_ROWS}; stopping, nothing written")
                conn.rollback()
                return 1

            updates = []
            for r in rows:
                fixed = _finalize({**r, "team": _TO}, now, hash_fields)
                updates.append((_TO, fixed["content_hash"], fixed["updated_at"],
                                r["source"], r["source_player_id"], r["as_of"]))
            with conn.cursor() as cur:
                cur.executemany(
                    "UPDATE injuries SET team = %s, content_hash = %s, updated_at = %s "
                    "WHERE source = %s AND source_player_id = %s AND as_of = %s "
                    "AND team = 'LAR'",
                    updates,
                )
                cur.execute("SELECT count(*) FROM injuries WHERE team = %s", (_FROM,))
                left = cur.fetchone()[0]
                cur.execute(
                    "SELECT count(*) FROM injuries WHERE team = %s "
                    "AND (raw::text LIKE %s)",
                    (_TO, f'%"{_FROM}"%'),
                )
                raw_kept = cur.fetchone()[0]
            print(f"rewritten: {len(updates)}; rows still {_FROM}: {left}; "
                  f"rewritten rows whose raw still mentions {_FROM}: {raw_kept}")

            after = _outputs(ctx)

            with conn.cursor() as cur:
                cur.execute("SELECT player_id FROM players WHERE latest_team = %s", (_TO,))
                rams = {r[0] for r in cur.fetchall()}

            def is_rams(key: tuple[Any, ...]) -> bool:
                team, player_id, _ = key
                return team == _TO or player_id in rams

            changed = {k for k in before.keys() | after.keys() if before.get(k) != after.get(k)}
            others = sorted((k for k in changed if not is_rams(k)), key=str)
            added = sorted((k for k in changed if k not in before), key=str)
            removed = sorted((k for k in changed if k not in after), key=str)
            print(f"\ncompute() rows: before {len(before)}, after {len(after)}")
            print(f"changed keys: {len(changed)} (added {len(added)}, removed {len(removed)}, "
                  f"value changed {len(changed) - len(added) - len(removed)})")
            print(f"changed keys outside the Rams: {len(others)}")
            for k in others[:20]:
                print(f"  {k}: {before.get(k)} -> {after.get(k)}")
            print("Rams team-level rows after:",
                  sorted((k[2], after[k][0]) for k in after if k[0] == _TO))
            print("Rams player-level signals added:",
                  sorted({k[2] for k in added if k[1] in rams}))

            ok = left == 0 and not others
            if commit and ok:
                conn.commit()
                print("\nCOMMITTED")
            else:
                conn.rollback()
                why = "dry run" if ok else "check failed"
                print(f"\nrolled back ({why}); nothing written")
            return 0 if ok else 1
        except BaseException:
            conn.rollback()
            raise


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
