"""
Job: Record each collector/analyst run to the agent_runs table.
Reads: nothing
Writes: agent_runs
Tier: n/a
Phase: P1
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any, Literal

import psycopg

RunStatus = Literal[
    "running",
    "success",
    "skipped_fresh",
    "skipped_no_prior",
    "skipped_no_injuries",
    "partial",
    "failed",
]


def start_run(conn: psycopg.Connection, agent: str) -> int:
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO agent_runs (agent, started_at, status) VALUES (%s, %s, 'running') "
            "RETURNING id",
            (agent, datetime.now(UTC)),
        )
        row = cur.fetchone()
        assert row is not None
        return row[0]


def finish_run(
    conn: psycopg.Connection,
    run_id: int,
    *,
    status: RunStatus,
    rows_written: int = 0,
    source_version: str | None = None,
    error: str | None = None,
    meta: dict[str, Any] | None = None,
) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE agent_runs
            SET finished_at = %s, status = %s, rows_written = %s,
                source_version = %s, error = %s, meta = %s
            WHERE id = %s
            """,
            (
                datetime.now(UTC),
                status,
                rows_written,
                source_version,
                error,
                json.dumps(meta or {}),
                run_id,
            ),
        )
