"""
Job: Define the Collector and Analyst base classes every pipeline job extends,
     and the shared run-and-log machinery both use.
Reads: nothing itself
Writes: agent_runs (via run())
Tier: n/a
Phase: P1
"""

from __future__ import annotations

import sys
from abc import ABC, abstractmethod
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import polars as pl
import psycopg

from . import logging as run_log
from .config import Settings, get_settings
from .db import get_connection


@dataclass(frozen=True)
class RunContext:
    """Threaded through every step of one run. `conn` is one open connection/
    transaction shared by should_run/fetch/validate/store (or the Analyst
    equivalents) — commit happens once, in `_execute`, after the whole run succeeds.
    """

    season: int
    week: int
    season_type: str
    now: datetime
    settings: Settings
    conn: psycopg.Connection


def _execute(
    *,
    name: str,
    season: int,
    week: int,
    season_type: str,
    is_ready: Callable[[RunContext], bool],
    do_work: Callable[[RunContext], int],
) -> None:
    settings = get_settings()
    with get_connection() as conn:
        run_id = run_log.start_run(conn, name)
        conn.commit()

        ctx = RunContext(
            season=season,
            week=week,
            season_type=season_type,
            now=datetime.now(UTC),
            settings=settings,
            conn=conn,
        )
        try:
            if not is_ready(ctx):
                run_log.finish_run(conn, run_id, status="skipped_fresh")
                conn.commit()
                return

            rows_written = do_work(ctx)
            conn.commit()
            run_log.finish_run(conn, run_id, status="success", rows_written=rows_written)
            conn.commit()
        except Exception as exc:
            conn.rollback()
            error = f"{type(exc).__name__}: {exc}"
            run_log.finish_run(conn, run_id, status="failed", error=error)
            conn.commit()
            print(f"[{name}] FAILED: {error.splitlines()[0]}", file=sys.stderr)


class Collector(ABC):
    """L1: fetch, validate, store. Never compute metrics.

    One collector per source family (see docs/architecture.md's L1 table). Contract:
    should_run(ctx) -> bool → fetch(ctx) -> raw → validate(raw) -> validated →
    store(ctx, validated) -> rows_written.
    """

    name: str

    @abstractmethod
    def should_run(self, ctx: RunContext) -> bool:
        """Freshness gate. Return False to skip this run (logged as skipped_fresh)."""

    @abstractmethod
    def fetch(self, ctx: RunContext) -> Any:
        """Call the external source only. No validation, no writes."""

    @abstractmethod
    def validate(self, raw: Any) -> Any:
        """Pydantic/Polars schema checks. Raise on unrecoverable bad data."""

    @abstractmethod
    def store(self, ctx: RunContext, validated: Any) -> int:
        """Write to Postgres via ctx.conn. Returns rows_written."""

    def run(self, *, season: int, week: int, season_type: str = "REG") -> None:
        _execute(
            name=self.name,
            season=season,
            week=week,
            season_type=season_type,
            is_ready=self.should_run,
            do_work=lambda ctx: self.store(ctx, self.validate(self.fetch(ctx))),
        )


class Analyst(ABC):
    """L2: read only stored tables, write only to `signals`.

    One analyst per sector (see docs/architecture.md's L2 table). Contract:
    inputs_ready(ctx) -> bool → compute(ctx) -> polars.DataFrame →
    write_signals(ctx, df) -> rows_written.
    """

    name: str

    @abstractmethod
    def inputs_ready(self, ctx: RunContext) -> bool:
        """Return False (logged as skipped_fresh) if required staged data isn't there yet."""

    @abstractmethod
    def compute(self, ctx: RunContext) -> pl.DataFrame:
        """Read staged tables via ctx.conn, return rows in `signals` shape."""

    @abstractmethod
    def write_signals(self, ctx: RunContext, df: pl.DataFrame) -> int:
        """Upsert df into `signals` via ctx.conn. Returns rows_written."""

    def run(self, *, season: int, week: int, season_type: str = "REG") -> None:
        _execute(
            name=self.name,
            season=season,
            week=week,
            season_type=season_type,
            is_ready=self.inputs_ready,
            do_work=lambda ctx: self.write_signals(ctx, self.compute(ctx)),
        )
