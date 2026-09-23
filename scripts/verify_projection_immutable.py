"""One-off script: verify migration 0023's immutability trigger on projection_log.

Inserts one test row (for a game with no lock yet) and tries an UPDATE and a DELETE on
it. Each must raise "projection_log is immutable". Then it rolls back everything and
confirms no test row remains. Nothing is ever committed.

Usage:
  uv run python scripts/verify_projection_immutable.py
"""

from __future__ import annotations

import sys
from pathlib import Path

import psycopg

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pipeline.core.db import get_connection  # noqa: E402

_TAG = "immutability-test"

# Every NOT NULL column without a default, per migrations 0004 + 0023.
_INSERT = """
INSERT INTO projection_log (game_id, season, week, locked_at, inputs_version, kickoff,
                            lock_lead_hours, model_version, stability_min, stability_bucket)
SELECT game_id, season, week, now(), %(tag)s, now(), 0, %(tag)s, 0, 'low'
FROM games WHERE game_id NOT IN (SELECT game_id FROM projection_log)
ORDER BY game_id LIMIT 1
"""
_ATTEMPTS = {
    "UPDATE": "UPDATE projection_log SET week = week WHERE inputs_version = %(tag)s",
    "DELETE": "DELETE FROM projection_log WHERE inputs_version = %(tag)s",
}


def main() -> int:
    ok = True
    with get_connection() as conn:
        try:
            with conn.cursor() as cur:
                cur.execute(_INSERT, {"tag": _TAG})
                if cur.rowcount != 1:
                    print("FAIL: could not insert a test row")
                    return 1
                print("inserted 1 test row (uncommitted)")
                for op, sql in _ATTEMPTS.items():
                    cur.execute("SAVEPOINT attempt")
                    try:
                        cur.execute(sql, {"tag": _TAG})
                    except psycopg.errors.RaiseException as exc:
                        cur.execute("ROLLBACK TO SAVEPOINT attempt")
                        print(f"PASS: {op} blocked -- {exc.diag.message_primary}")
                    else:
                        ok = False
                        print(f"FAIL: {op} was not blocked ({cur.rowcount} row(s) affected)")
        finally:
            conn.rollback()

        with conn.cursor() as cur:
            cur.execute(
                "SELECT count(*) FROM projection_log WHERE inputs_version = %s", (_TAG,)
            )
            row = cur.fetchone()
            left = row[0] if row else -1
        conn.rollback()

    if left != 0:
        ok = False
        print(f"FAIL: {left} test row(s) remain after rollback")
    else:
        print("rolled back: 0 test rows remain")
    print("PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
