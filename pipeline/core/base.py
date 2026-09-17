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
import time
from abc import ABC, abstractmethod
from collections.abc import Callable
from dataclasses import dataclass, field
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


@dataclass(frozen=True)
class WorkResult:
    """What `Collector.store`/`Analyst.write_signals` hand back to `_execute`: the row
    count (as before) plus optional per-run metadata for `agent_runs.meta` (e.g. per-team
    discount factors) -- most implementations just return `WorkResult(rows_written)` and
    leave `meta` at its default `{}`.
    """

    rows_written: int
    meta: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class RunResult:
    """What a `Collector`/`Analyst` run did, for callers that want to report on it
    (e.g. `pipeline/run.py`'s one-line summary) without re-querying `agent_runs`.
    """

    name: str
    status: run_log.RunStatus
    rows_written: int
    duration_s: float
    error: str | None = None


def _resolve_skip_status(ready_result: bool | str) -> run_log.RunStatus | None:
    """`None` means proceed. Otherwise, the exact status to log and return.

    `is_ready` returning `True` proceeds, `False` is the generic "nothing changed"
    skip (`skipped_fresh`), and any other string is that exact status verbatim (e.g.
    `EfficiencyAnalyst.inputs_ready`'s `"skipped_no_prior"`) — a more specific reason
    than a routine freshness-gate skip.
    """
    if ready_result is True:
        return None
    if isinstance(ready_result, str):
        return ready_result  # type: ignore[return-value]
    return "skipped_fresh"


def _execute(
    *,
    name: str,
    season: int,
    week: int,
    season_type: str,
    is_ready: Callable[[RunContext], bool | str],
    do_work: Callable[[RunContext], WorkResult],
) -> RunResult:
    settings = get_settings()
    started = time.monotonic()
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
            skip_status = _resolve_skip_status(is_ready(ctx))
            if skip_status is not None:
                run_log.finish_run(conn, run_id, status=skip_status)
                conn.commit()
                return RunResult(name, skip_status, 0, time.monotonic() - started)

            result = do_work(ctx)
            conn.commit()
            run_log.finish_run(
                conn,
                run_id,
                status="success",
                rows_written=result.rows_written,
                meta=result.meta,
            )
            conn.commit()
            return RunResult(name, "success", result.rows_written, time.monotonic() - started)
        except Exception as exc:
            conn.rollback()
            error = f"{type(exc).__name__}: {exc}"
            run_log.finish_run(conn, run_id, status="failed", error=error)
            conn.commit()
            print(f"[{name}] FAILED: {error.splitlines()[0]}", file=sys.stderr)
            return RunResult(name, "failed", 0, time.monotonic() - started, error=error)


class Collector(ABC):
    """L1: fetch, validate, store. Never compute metrics.

    One collector per source family (see docs/architecture.md's L1 table). Contract:
    should_run(ctx) -> bool → fetch(ctx) -> raw → validate(raw) -> validated →
    store(ctx, validated) -> WorkResult.
    """

    name: str

    @abstractmethod
    def should_run(self, ctx: RunContext) -> bool | str:
        """Freshness gate. `False` skips as `skipped_fresh`; a string skips as that
        exact status instead, for a more specific reason than a routine freshness skip.
        """

    @abstractmethod
    def fetch(self, ctx: RunContext) -> Any:
        """Call the external source only. No validation, no writes."""

    @abstractmethod
    def validate(self, raw: Any) -> Any:
        """Pydantic/Polars schema checks. Raise on unrecoverable bad data."""

    @abstractmethod
    def store(self, ctx: RunContext, validated: Any) -> WorkResult:
        """Write to Postgres via ctx.conn. Returns WorkResult(rows_written, meta)."""

    def run(
        self, *, season: int, week: int, season_type: str = "REG", force: bool = False
    ) -> RunResult:
        return _execute(
            name=self.name,
            season=season,
            week=week,
            season_type=season_type,
            is_ready=(lambda ctx: True) if force else self.should_run,
            do_work=lambda ctx: self.store(ctx, self.validate(self.fetch(ctx))),
        )


class Analyst(ABC):
    """L2: read only stored tables, write only to `signals`.

    One analyst per sector (see docs/architecture.md's L2 table). Contract:
    inputs_ready(ctx) -> bool → compute(ctx) -> polars.DataFrame →
    write_signals(ctx, df) -> WorkResult.
    """

    name: str

    @abstractmethod
    def inputs_ready(self, ctx: RunContext) -> bool | str:
        """`False` skips as `skipped_fresh` if required staged data isn't there yet; a
        string skips as that exact status instead (e.g. `"skipped_no_prior"`)."""

    @abstractmethod
    def compute(self, ctx: RunContext) -> pl.DataFrame:
        """Read staged tables via ctx.conn, return rows in `signals` shape."""

    @abstractmethod
    def write_signals(self, ctx: RunContext, df: pl.DataFrame) -> WorkResult:
        """Upsert df into `signals` via ctx.conn. Returns WorkResult(rows_written, meta)."""

    def run(
        self, *, season: int, week: int, season_type: str = "REG", force: bool = False
    ) -> RunResult:
        return _execute(
            name=self.name,
            season=season,
            week=week,
            season_type=season_type,
            is_ready=(lambda ctx: True) if force else self.inputs_ready,
            do_work=lambda ctx: self.write_signals(ctx, self.compute(ctx)),
        )
