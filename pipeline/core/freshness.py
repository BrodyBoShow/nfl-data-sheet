"""
Job: Track each external source's last-seen freshness marker (e.g. a release
     timestamp) so collectors can skip refetching when nothing changed upstream.
Reads: source_freshness
Writes: source_freshness
Tier: n/a
Phase: P1
"""

from __future__ import annotations

import psycopg


def get_last_value(conn: psycopg.Connection, source: str) -> str | None:
    with conn.cursor() as cur:
        cur.execute("SELECT last_value FROM source_freshness WHERE source = %s", (source,))
        row = cur.fetchone()
        return row[0] if row else None


def set_last_value(conn: psycopg.Connection, source: str, value: str) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO source_freshness (source, last_value, checked_at)
            VALUES (%s, %s, now())
            ON CONFLICT (source) DO UPDATE
            SET last_value = EXCLUDED.last_value, checked_at = EXCLUDED.checked_at
            """,
            (source, value),
        )
