"""One-off script: snapshot pg_stat_statements counters at an egress calibration window
boundary (docs/phases/P6.md, Open 1, "Calibration window").

Read-only. Fetches counters only, no query text: text is most of the bytes, and the end
snapshot is diffed against the start by key, so text is needed only for the statements
that moved, fetched after the window closes. Writes data/calibration/<label>.json
(gitignored):
  taken_at, stats_reset, dealloc, own_bytes (this snapshot's own result, in the same
  per-row model as P7 step 9 (b), so its egress can be counted), and per
  (role, queryid, toplevel): calls, rows.

Usage:
  uv run python scripts/calibration_snapshot.py start
  uv run python scripts/calibration_snapshot.py end
  uv run python scripts/calibration_snapshot.py test   (outside a window only)
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pipeline.core.db import get_connection  # noqa: E402

_OUT = Path(__file__).resolve().parent.parent / "data" / "calibration"

_INFO_SQL = "SELECT now(), stats_reset, dealloc FROM extensions.pg_stat_statements_info"
_SQL = """
SELECT r.rolname, s.queryid, s.toplevel, s.calls, s.rows
FROM extensions.pg_stat_statements s
JOIN pg_roles r ON r.oid = s.userid
"""


def _row_bytes(row: tuple[object, ...]) -> int:
    """P7 step 9 (b)'s model: 7 + 4 per field + each field's text."""
    return 7 + 4 * len(row) + sum(len(str(v).encode()) for v in row if v is not None)


def main() -> int:
    if len(sys.argv) != 2 or sys.argv[1] not in ("start", "end", "test"):
        print(__doc__, file=sys.stderr)
        return 1
    label = sys.argv[1]
    path = _OUT / f"{label}.json"
    if path.exists():
        print(f"{path} already exists; refusing to overwrite a boundary snapshot", file=sys.stderr)
        return 1

    with get_connection() as conn:
        conn.read_only = True
        with conn.cursor() as cur:
            # One transaction, so now() is the same instant for both reads.
            cur.execute(_INFO_SQL)
            info = cur.fetchone()
            cur.execute(_SQL)
            rows = cur.fetchall()
        conn.rollback()

    assert info is not None
    taken_at, stats_reset, dealloc = info
    snapshot = {
        "label": label,
        "taken_at": taken_at.isoformat(),
        "stats_reset": stats_reset.isoformat(),
        "dealloc": dealloc,
        "statements": len(rows),
        "own_bytes": _row_bytes(info) + sum(_row_bytes(r) for r in rows),
        "counters": [
            {"role": r[0], "queryid": r[1], "toplevel": r[2], "calls": r[3], "rows": r[4]}
            for r in rows
        ],
    }
    _OUT.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(snapshot, indent=1), encoding="utf-8")
    print(
        f"{label}: {snapshot['taken_at']} | {len(rows)} statements | stats_reset "
        f"{snapshot['stats_reset']} | dealloc {dealloc} | own result {snapshot['own_bytes']:,} B"
        f" -> {path}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
