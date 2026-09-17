"""
Job: Own the single Supabase Postgres connection factory and a generic upsert helper.
Reads: SUPABASE_DB_URL
Writes: nothing itself — callers execute their own statements against the connection
Tier: n/a
Phase: P1
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from contextlib import contextmanager
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


def _chunks(rows: list[dict[str, Any]], size: int) -> Iterable[list[dict[str, Any]]]:
    for i in range(0, len(rows), size):
        yield rows[i : i + size]


def filter_changed(
    conn: psycopg.Connection,
    table: str,
    pk_cols: str | Iterable[str],
    rows: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Drop rows whose `content_hash` already matches what's stored (hash-diff gate).

    Each row must include every column in `pk_cols` and a `content_hash` key. `table`/
    `pk_cols` are always internal constants, never external input. `pk_cols` is a single
    column name for a simple key, or a sequence of column names for a composite key.
    Batches the lookup at 1000 rows/query to stay well under Postgres's bind-parameter
    limit for wide composite keys.
    """
    if not rows:
        return []
    cols = [pk_cols] if isinstance(pk_cols, str) else list(pk_cols)
    col_list = ", ".join(cols)

    existing: dict[tuple[Any, ...], Any] = {}
    with conn.cursor() as cur:
        for batch in _chunks(rows, 1000):
            pk_tuples = [tuple(r[c] for c in cols) for r in batch]
            values_sql = ", ".join(f"({', '.join(['%s'] * len(cols))})" for _ in pk_tuples)
            flat_params = [v for pk in pk_tuples for v in pk]
            cur.execute(
                f"SELECT {col_list}, content_hash FROM {table} "
                f"WHERE ({col_list}) IN (VALUES {values_sql})",
                flat_params,
            )
            for row in cur.fetchall():
                existing[tuple(row[:-1])] = row[-1]

    return [r for r in rows if existing.get(tuple(r[c] for c in cols)) != r["content_hash"]]
