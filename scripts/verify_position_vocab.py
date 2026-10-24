"""Verification: the position code sets the analysts filter on, checked against the live
distinct values of the columns they filter, both ways. Exits 1 on any mismatch, so a
workflow step running it fails (CLAUDE.md, "A hardcoded vocabulary is checked against the
data it filters"; docs/phases/P7.md, open item 22).

- `players.position`: Availability impact's _OL_POSITIONS, _SECONDARY_POSITIONS,
  _REDISTRIBUTION_POSITIONS and _OTHER_POSITIONS together.
- `snaps.position`: Efficiency's _OL_SNAP_POSITIONS and _NON_OL_SNAP_POSITIONS together.

Fails on:
- a code in the data that no set knows (a new code would otherwise be dropped silently);
- a code in a set that the data doesn't have (a set that matches nothing, like "T" did);
- a NULL in the column (a player with no position is dropped from every count);
- a code in two sets of the same column.

Reads only the distinct values, one aggregate query per column. Imports the sets from
the analysts, never restates them.

Usage:
  uv run python scripts/verify_position_vocab.py
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pipeline.analysts import availability_impact as ai  # noqa: E402
from pipeline.analysts import efficiency as eff  # noqa: E402
from pipeline.core.db import get_connection  # noqa: E402

PARTITIONS: dict[str, dict[str, set[str]]] = {
    "players.position": {
        "availability_impact._OL_POSITIONS": ai._OL_POSITIONS,
        "availability_impact._SECONDARY_POSITIONS": ai._SECONDARY_POSITIONS,
        "availability_impact._REDISTRIBUTION_POSITIONS": ai._REDISTRIBUTION_POSITIONS,
        "availability_impact._OTHER_POSITIONS": ai._OTHER_POSITIONS,
    },
    "snaps.position": {
        "efficiency._OL_SNAP_POSITIONS": eff._OL_SNAP_POSITIONS,
        "efficiency._NON_OL_SNAP_POSITIONS": eff._NON_OL_SNAP_POSITIONS,
    },
}

# Internal constants only, never external input.
_QUERIES = {
    "players.position": "SELECT position, count(*) FROM players GROUP BY position",
    "snaps.position": "SELECT position, count(*) FROM snaps GROUP BY position",
}


def check(column: str, live: dict[str | None, int], sets: dict[str, set[str]]) -> list[str]:
    """Every problem with `sets` as the vocabulary of `column`, given its live
    {code: rows}. Empty means the sets and the data agree exactly."""
    problems: list[str] = []
    if None in live:
        problems.append(f"{column}: {live[None]} rows are NULL")
    names = list(sets)
    for i, a in enumerate(names):
        for b in names[i + 1 :]:
            for code in sorted(sets[a] & sets[b]):
                problems.append(f"{column}: {code!r} is in both {a} and {b}")
    known = set().union(*sets.values())
    for code in sorted(c for c in live if c is not None and c not in known):
        problems.append(f"{column}: {code!r} ({live[code]} rows) is in no set")
    for name, codes in sets.items():
        for code in sorted(codes - set(live)):
            problems.append(f"{column}: {code!r} in {name} matches no row")
    return problems


def main() -> int:
    problems: list[str] = []
    with get_connection() as conn, conn.cursor() as cur:
        for column, sets in PARTITIONS.items():
            cur.execute(_QUERIES[column])
            live = {code: n for code, n in cur.fetchall()}
            found = check(column, live, sets)
            problems += found
            print(f"{column}: {len(live)} distinct codes, {len(found)} problems")
    for p in problems:
        print(f"FAIL {p}", file=sys.stderr)
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
