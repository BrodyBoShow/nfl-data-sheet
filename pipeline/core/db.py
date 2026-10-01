"""
Job: Own the single Supabase Postgres connection factory and generic upsert/delete
     helpers.
Reads: SUPABASE_DB_URL
Writes: nothing itself — callers execute their own statements against the connection
Tier: n/a
Phase: P1
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any

import psycopg

from .config import get_settings


@contextmanager
def get_connection():
    """Yield a fresh connection. Caller owns transaction control (commit/rollback)."""
    settings = get_settings()
    conn = psycopg.connect(settings.supabase_db_url)
    try:
        yield conn
    finally:
        conn.close()


def upsert_rows(
    conn: psycopg.Connection,
    table: str,
    rows: Iterable[Mapping[str, Any]],
    conflict_cols: Iterable[str],
    update_cols: Iterable[str],
) -> int:
    """INSERT ... ON CONFLICT DO UPDATE for a batch of rows. Returns rows attempted.

    `table`/`conflict_cols`/`update_cols` are always internal constants supplied by our
    own collector/analyst code, never external input — safe to interpolate directly.
    """
    rows = list(rows)
    if not rows:
        return 0

    columns = list(rows[0].keys())
    col_list = ", ".join(columns)
    placeholders = ", ".join(f"%({c})s" for c in columns)
    conflict_list = ", ".join(conflict_cols)
    update_list = ", ".join(f"{c} = EXCLUDED.{c}" for c in update_cols)

    query = (
        f"INSERT INTO {table} ({col_list}) VALUES ({placeholders}) "
        f"ON CONFLICT ({conflict_list}) DO UPDATE SET {update_list}"
    )

    with conn.cursor() as cur:
        cur.executemany(query, rows)
    return len(rows)


def delete_rows(
    conn: psycopg.Connection,
    table: str,
    where: str,
    params: Iterable[Any],
) -> int:
    """`DELETE FROM {table} WHERE {where}`, with `params` bound positionally against
    `where`'s `%s` placeholders. `table`/`where` are always internal constants supplied
    by our own analyst/collector code, never external input — safe to interpolate
    directly. Returns rows deleted.

    Exists for the general "signals an analyst stops emitting must not persist forever"
    problem: `upsert_rows` only inserts/updates the rows it's given, it never removes a
    row a prior run wrote that this run's logic no longer produces (a signal whose
    emission criteria narrowed, a player who's no longer eligible, a team that no longer
    exists). A `write_signals` that wants a run's output to be authoritative for its
    scope calls this first, scoped to what that analyst itself could have written (its
    own `sector` + the `season`/`week` it just computed + its own known signal names),
    then upserts the fresh set — see `pipeline/analysts/availability_impact.py`.
    """
    with conn.cursor() as cur:
        cur.execute(delete_statement(table, where), tuple(params))
        return cur.rowcount


def delete_statement(table: str, where: str) -> str:
    """The exact SQL `delete_rows` runs, so a caller can EXPLAIN that same statement
    without executing it (the retention job's dry run does)."""
    return f"DELETE FROM {table} WHERE {where}"


@dataclass(frozen=True)
class ChangedUpsert:
    """What `upsert_changed` wrote. `rows_changed` (inserted + updated) is the count the
    old client-side hash diff produced. `duplicates_dropped` is how many input rows lost
    to a later row with the same primary key. `returned` holds the `returning` columns of
    every changed row, in no particular order."""

    inserted: int
    updated: int
    duplicates_dropped: int
    returned: list[tuple[Any, ...]] = field(default_factory=list)

    @property
    def rows_changed(self) -> int:
        return self.inserted + self.updated

    def meta(self) -> dict[str, int]:
        """The per-table entry a run records in its `meta`."""
        return {"rows_changed": self.rows_changed, "duplicates_dropped": self.duplicates_dropped}


# The per-call staging table. Dropped after each call, and ON COMMIT DROP if a call fails.
_STAGE = "_upsert_changed_stage"


def upsert_changed_sql(
    table: str,
    source: str,
    cols: Sequence[str],
    pk_cols: Sequence[str],
    update_cols: Sequence[str],
    update_exprs: Mapping[str, str] | None = None,
    returning: Sequence[str] = (),
) -> str:
    """The hash-diffed upsert `upsert_changed` runs, reading rows from `source`.

    Two clauses look redundant. Neither is; keep both.
    - `WHERE NOT EXISTS (... content_hash = src.content_hash)` is the optimization. It
      drops identical rows before they reach ON CONFLICT. The Postgres docs: "Only rows
      for which this expression returns true will be updated, although all rows will be
      locked when the ON CONFLICT DO UPDATE action is taken." Without it, every identical
      row would be locked until commit and write WAL on every run.
    - `DO UPDATE ... WHERE content_hash IS DISTINCT FROM` is the correctness guarantee. A
      concurrent writer can store this exact content between the probe and the insert.
      The WHERE keeps that row from being rewritten, so `updated_at` still moves only when
      content changes. The gate spec's auditor check depends on that (docs/phases/P7.md,
      step 9, the input gate spec, item 5). IS DISTINCT FROM, so a NULL hash on either side
      still compares.

    `RETURNING (xmax = 0)` is true for an inserted row and false for an updated one.
    A row the WHERE skips isn't returned at all."""
    update_exprs = update_exprs or {}
    col_list = ", ".join(cols)
    same_key = " AND ".join(f"cur.{c} = src.{c}" for c in pk_cols)
    sets = ", ".join(f"{c} = {update_exprs.get(c, f'EXCLUDED.{c}')}" for c in update_cols)
    returned = "".join(f", {c}" for c in returning)
    return (
        f"INSERT INTO {table} ({col_list}) "
        f"SELECT {col_list} FROM {source} AS src "
        f"WHERE NOT EXISTS (SELECT 1 FROM {table} AS cur "
        f"WHERE {same_key} AND cur.content_hash = src.content_hash) "
        f"ON CONFLICT ({', '.join(pk_cols)}) DO UPDATE SET {sets} "
        f"WHERE {table}.content_hash IS DISTINCT FROM EXCLUDED.content_hash "
        f"RETURNING ({table}.xmax = 0){returned}"
    )


def upsert_changed(
    conn: psycopg.Connection,
    table: str,
    rows: Iterable[Mapping[str, Any]],
    pk_cols: str | Sequence[str],
    update_cols: Sequence[str],
    *,
    update_exprs: Mapping[str, str] | None = None,
    returning: Sequence[str] = (),
) -> ChangedUpsert:
    """Upsert only the rows whose `content_hash` differs from what's stored, deciding that
    in Postgres. No stored hash comes back to the client, so the diff costs no egress.

    The rows go up by COPY into a temp table, then one INSERT ... SELECT does the diff
    (`upsert_changed_sql`). Every row needs every `pk_cols` column and `content_hash`.
    The insert columns are the first row's keys, as with `upsert_rows`.
    `update_exprs` replaces `EXCLUDED.<col>` for a column that keeps its stored value
    (the grader's `result_first_seen_at`).

    Duplicate primary keys in one batch: the last row wins, the result the old per-row
    upsert gave. One statement can't update a row twice, so the earlier rows are dropped
    first and counted in `duplicates_dropped`, never silently.

    `table`, the column names and `update_exprs` are always internal constants, never
    external input, so they're safe to interpolate."""
    pk = [pk_cols] if isinstance(pk_cols, str) else list(pk_cols)
    latest: dict[tuple[Any, ...], Mapping[str, Any]] = {}
    offered = 0
    for row in rows:
        offered += 1
        latest[tuple(row[c] for c in pk)] = row
    if not latest:
        return ChangedUpsert(0, 0, 0)
    deduped = list(latest.values())
    cols = list(deduped[0].keys())
    if "content_hash" not in cols:
        raise ValueError(f"{table}: rows carry no content_hash, so nothing can be diffed")

    col_list = ", ".join(cols)
    with conn.cursor() as cur:
        cur.execute(
            f"CREATE TEMP TABLE {_STAGE} ON COMMIT DROP AS "
            f"SELECT {col_list} FROM {table} WITH NO DATA"
        )
        with cur.copy(f"COPY {_STAGE} ({col_list}) FROM STDIN") as copy:
            for row in deduped:
                copy.write_row([row[c] for c in cols])
        cur.execute(
            upsert_changed_sql(table, _STAGE, cols, pk, update_cols, update_exprs, returning)
        )
        returned = cur.fetchall()
        cur.execute(f"DROP TABLE {_STAGE}")

    inserted = sum(1 for r in returned if r[0])
    return ChangedUpsert(
        inserted=inserted,
        updated=len(returned) - inserted,
        duplicates_dropped=offered - len(deduped),
        returned=[tuple(r[1:]) for r in returned],
    )
