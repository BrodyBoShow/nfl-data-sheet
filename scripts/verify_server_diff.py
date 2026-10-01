"""One-off script: verify pipeline/core/db.py's upsert_changed on the real Postgres engine.

Tests never open a live connection, so what Postgres does with the statement is checked
here, on TEMP tables in one transaction that is rolled back. Nothing is ever committed,
and no real table is touched.

Checks (each prints PASS/FAIL):
  1. An identical row written twice: updated_at doesn't move, nothing is returned, and the
     stored row isn't locked (xmax stays 0).
  2. A changed row: updated_at moves, the new value is stored, counted as updated.
  3. rows_changed equals what the removed filter_changed reports for the same input. That
     function is loaded verbatim from commit 6958a54 (`git show`), not re-implemented.
  4. A mixed batch (identical, changed, new, and a duplicate key) counts correctly, keeps
     the last duplicate, and reports one duplicate dropped.
Then two break demonstrations, which must each detect the break:
  5. The pre-filter removed: the conflict guard alone still stops the rewrite, but the
     row is now locked (the Postgres docs' "all rows will be locked").
  6. Both guards removed: updated_at moves on an identical row, which check 1 catches.

Usage:
  uv run python scripts/verify_server_diff.py
"""

from __future__ import annotations

import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pipeline.core.db import get_connection, upsert_changed, upsert_changed_sql  # noqa: E402

_ROOT = Path(__file__).resolve().parent.parent
_REMOVED_AT = "6958a54"
_T = "diff_verify"
_COLS = ["k", "v", "content_hash", "updated_at"]
_UPDATE = ["v", "content_hash", "updated_at"]
T0 = datetime(2026, 10, 1, tzinfo=UTC)


def _removed_filter_changed() -> Any:
    """filter_changed exactly as it last existed in production."""
    src = subprocess.run(
        ["git", "show", f"{_REMOVED_AT}:pipeline/core/db.py"],
        cwd=_ROOT,
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    ns: dict[str, Any] = {"__name__": "pipeline.core._removed_db", "__package__": "pipeline.core"}
    exec(compile(src, f"{_REMOVED_AT}:pipeline/core/db.py", "exec"), ns)
    return ns["filter_changed"]


def _row(k: str, v: int, h: str, minutes: int) -> dict[str, Any]:
    return {"k": k, "v": v, "content_hash": h, "updated_at": T0 + timedelta(minutes=minutes)}


def _stored(cur: Any, k: str) -> tuple[Any, ...] | None:
    cur.execute(f"SELECT v, content_hash, updated_at, xmax::text FROM {_T} WHERE k = %s", (k,))
    return cur.fetchone()


def main() -> int:
    filter_changed = _removed_filter_changed()
    results: list[tuple[str, bool, str]] = []

    def check(name: str, ok: bool, detail: str) -> None:
        results.append((name, ok, detail))

    with get_connection() as conn:
        try:
            with conn.cursor() as cur:
                cur.execute(
                    f"CREATE TEMP TABLE {_T} (k text PRIMARY KEY, v int, "
                    "content_hash text NOT NULL, updated_at timestamptz NOT NULL)"
                )

                # 1. identical row twice
                first = upsert_changed(conn, _T, [_row("a", 1, "h1", 1)], "k", _UPDATE)
                again = upsert_changed(conn, _T, [_row("a", 1, "h1", 2)], "k", _UPDATE)
                s = _stored(cur, "a")
                check(
                    "1 identical row: updated_at unchanged",
                    first.inserted == 1
                    and again.rows_changed == 0
                    and s is not None
                    and s[2] == T0 + timedelta(minutes=1)
                    and s[3] == "0",
                    f"first={first}, again={again}, "
                    f"stored updated_at={s and s[2]}, xmax={s and s[3]}",
                )

                # 2. changed row
                changed = upsert_changed(conn, _T, [_row("a", 2, "h2", 3)], "k", _UPDATE)
                s = _stored(cur, "a")
                check(
                    "2 changed row: updated_at moves, new value stored",
                    changed.updated == 1
                    and changed.inserted == 0
                    and s is not None
                    and (s[0], s[1], s[2]) == (2, "h2", T0 + timedelta(minutes=3)),
                    f"result={changed}, stored={s}",
                )

                # 3 + 4. mixed batch, against the removed filter_changed on the same state
                upsert_changed(conn, _T, [_row("b", 1, "hb", 4)], "k", _UPDATE)
                batch = [
                    _row("a", 2, "h2", 5),  # identical
                    _row("b", 9, "hb2", 5),  # changed
                    _row("c", 0, "hc0", 5),  # new, then superseded
                    _row("d", 4, "hd", 5),  # new
                    _row("c", 3, "hc", 5),  # duplicate key: this one wins
                ]
                deduped = list({r["k"]: r for r in batch}.values())
                old_count = len(filter_changed(conn, _T, "k", deduped))
                mixed = upsert_changed(conn, _T, batch, "k", _UPDATE)
                check(
                    "3 rows_changed equals the removed filter_changed",
                    mixed.rows_changed == old_count,
                    f"upsert_changed={mixed.rows_changed}, filter_changed={old_count}",
                )
                s_a, s_c = _stored(cur, "a"), _stored(cur, "c")
                check(
                    "4 mixed batch counts, last duplicate kept",
                    (mixed.inserted, mixed.updated, mixed.duplicates_dropped) == (2, 1, 1)
                    and s_c is not None
                    and (s_c[0], s_c[1]) == (3, "hc")
                    and s_a is not None
                    and s_a[2] == T0 + timedelta(minutes=3),
                    f"result={mixed}, stored c={s_c}, a.updated_at={s_a and s_a[2]}",
                )

                # 5. break: pre-filter removed. The guard still blocks the rewrite, but
                # the identical row gets locked.
                cur.execute(f"CREATE TEMP TABLE {_T}_src AS SELECT * FROM {_T} WHERE false")
                cur.execute(
                    f"INSERT INTO {_T}_src VALUES (%s, %s, %s, %s)",
                    ("d", 4, "hd", T0 + timedelta(minutes=6)),
                )
                cur.execute("SAVEPOINT no_prefilter")
                sql = upsert_changed_sql(_T, f"{_T}_src", _COLS, ["k"], _UPDATE)
                cur.execute(sql.replace("WHERE NOT EXISTS", "WHERE true OR NOT EXISTS"))
                guard_only = cur.fetchall()
                s = _stored(cur, "d")
                check(
                    "5 break: no pre-filter -> guard holds, row now locked",
                    guard_only == []
                    and s is not None
                    and s[2] == T0 + timedelta(minutes=5)
                    and s[3] != "0",
                    f"returned={guard_only}, updated_at={s and s[2]}, xmax={s and s[3]}",
                )
                cur.execute("ROLLBACK TO SAVEPOINT no_prefilter")

                # 6. break: both guards removed -> updated_at moves on identical content
                cur.execute("SAVEPOINT no_guards")
                broken = sql.replace("WHERE NOT EXISTS", "WHERE true OR NOT EXISTS").replace(
                    f"WHERE {_T}.content_hash IS DISTINCT FROM EXCLUDED.content_hash ", ""
                )
                cur.execute(broken)
                s = _stored(cur, "d")
                check(
                    "6 break: no guards -> check 1 would catch it",
                    s is not None and s[2] == T0 + timedelta(minutes=6),
                    f"identical row's updated_at moved to {s and s[2]}",
                )
                cur.execute("ROLLBACK TO SAVEPOINT no_guards")
        finally:
            conn.rollback()

    for name, ok, detail in results:
        print(f"{'PASS' if ok else 'FAIL'}  {name}\n      {detail}")
    print("Rolled back; nothing committed.")
    return 0 if all(ok for _, ok, _ in results) and len(results) == 6 else 1


if __name__ == "__main__":
    sys.exit(main())
