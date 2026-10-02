"""
Job: Define the Collector, Analyst, Synthesizer and Grader base classes every pipeline job
     extends, and the shared run-and-log machinery they use.
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
from typing import Any, ClassVar, Protocol

import polars as pl
import psycopg

from . import logging as run_log
from .config import Settings, get_settings
from .db import delete_rows, get_connection


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


@dataclass(frozen=True)
class Readiness:
    """A readiness answer that carries `agent_runs.meta`, for a check that has to record
    why it decided (the input gate, `pipeline/core/input_gate.py`). `status` None
    proceeds; any other status skips as exactly that status. `meta` is logged however the
    run ends: on the skip, merged into a success's meta, and on a failure."""

    status: run_log.RunStatus | None
    meta: dict[str, Any] = field(default_factory=dict)


ReadyResult = bool | str | Readiness


def _resolve_skip_status(ready_result: ReadyResult) -> run_log.RunStatus | None:
    """`None` means proceed. Otherwise, the exact status to log and return.

    `is_ready` returning `True` proceeds, `False` is the generic "nothing changed"
    skip (`skipped_fresh`), and any other string is that exact status verbatim (e.g.
    `EfficiencyAnalyst.inputs_ready`'s `"skipped_no_prior"`) — a more specific reason
    than a routine freshness-gate skip. A `Readiness` is its own `status`.
    """
    if isinstance(ready_result, Readiness):
        return ready_result.status
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
    is_ready: Callable[[RunContext], ReadyResult],
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
        # A Readiness's meta (the gate's key and decision) is logged however the run ends.
        ready_meta: dict[str, Any] = {}
        try:
            ready = is_ready(ctx)
            if isinstance(ready, Readiness):
                ready_meta = ready.meta
            skip_status = _resolve_skip_status(ready)
            if skip_status is not None:
                run_log.finish_run(conn, run_id, status=skip_status, meta=ready_meta)
                conn.commit()
                return RunResult(name, skip_status, 0, time.monotonic() - started)

            result = do_work(ctx)
            conn.commit()
            run_log.finish_run(
                conn,
                run_id,
                status="success",
                rows_written=result.rows_written,
                meta={**result.meta, **ready_meta},
            )
            conn.commit()
            return RunResult(name, "success", result.rows_written, time.monotonic() - started)
        except Exception as exc:
            conn.rollback()
            error = f"{type(exc).__name__}: {exc}"
            run_log.finish_run(conn, run_id, status="failed", error=error, meta=ready_meta)
            conn.commit()
            print(f"[{name}] FAILED: {error.splitlines()[0]}", file=sys.stderr)
            return RunResult(name, "failed", 0, time.monotonic() - started, error=error)


class InputGateCheck(Protocol):
    """What `Analyst.run` calls on a gated analyst (`pipeline/core/input_gate.py`'s
    `InputGate`). A Protocol so this module doesn't import the gate, which imports it."""

    def check(self, ctx: RunContext, *, force: bool) -> Readiness: ...


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
    write_signals(ctx, df) -> WorkResult. `sector` and `signal_names` are declared by
    every subclass (not just informal convention) so `run()` can delete this analyst's
    own stale rows before each write -- see `_delete_stale_signals`.
    """

    name: str
    sector: str
    signal_names: frozenset[str]
    # The per-analyst input gate (docs/phases/P7.md, step 9, the gate spec). None for an
    # ungated analyst, which runs exactly as before.
    input_gate: ClassVar[InputGateCheck | None] = None

    @abstractmethod
    def inputs_ready(self, ctx: RunContext) -> bool | str:
        """`False` skips as `skipped_fresh` if required staged data isn't there yet; a
        string skips as that exact status instead (e.g. `"skipped_no_prior"`)."""

    def _ready(self, ctx: RunContext, force: bool) -> ReadyResult:
        """`inputs_ready` first, as before, then the input gate if the analyst has one.

        The gate is called here, not from `inputs_ready`, because `--force` never calls
        `inputs_ready`, and the gate still has to take its key on a forced run (spec item
        4). A forced run skips `inputs_ready`'s no-data check and the gate's comparison."""
        if not force:
            ready = self.inputs_ready(ctx)
            if ready is not True:
                return ready
        if self.input_gate is None:
            return True
        return self.input_gate.check(ctx, force=force)

    @abstractmethod
    def compute(self, ctx: RunContext) -> pl.DataFrame:
        """Read staged tables via ctx.conn, return rows in `signals` shape."""

    @abstractmethod
    def write_signals(self, ctx: RunContext, df: pl.DataFrame) -> WorkResult:
        """Upsert df into `signals` via ctx.conn. Returns WorkResult(rows_written, meta).
        Stale-row cleanup for this analyst's own (sector, signal_names) scope already
        happened in run() before this is called (see _delete_stale_signals) --
        implementations only need to handle the upsert itself."""

    def _delete_stale_signals(self, ctx: RunContext) -> int:
        """Deletes every `signals` row this analyst could have written for
        (ctx.season, ctx.week) that this run's write_signals is about to replace.
        Scoped to `sector` + `signal_names`, so it can never reach another analyst's
        rows (a different sector) or a different signal name -- even a hypothetical
        future analyst sharing this one's sector would be untouched, since its own
        signal names wouldn't be in this analyst's `signal_names` set. Without this,
        `upsert_rows` alone only ever inserts/updates the rows it's given -- a row a
        prior run wrote that this run's (possibly narrower) logic no longer produces
        would persist forever. Every Analyst gets this automatically via run(); no
        subclass needs to call it or reimplement it (see pipeline/core/db.py's
        delete_rows docstring for the general rationale)."""
        return delete_rows(
            ctx.conn,
            "signals",
            "sector = %s AND season = %s AND week = %s AND signal = ANY(%s)",
            (self.sector, ctx.season, ctx.week, sorted(self.signal_names)),
        )

    def run(
        self, *, season: int, week: int, season_type: str = "REG", force: bool = False
    ) -> RunResult:
        def do_work(ctx: RunContext) -> WorkResult:
            df = self.compute(ctx)
            deleted = self._delete_stale_signals(ctx)
            result = self.write_signals(ctx, df)
            meta = {**result.meta, "stale_signals_deleted": deleted}
            return WorkResult(result.rows_written, meta)

        return _execute(
            name=self.name,
            season=season,
            week=week,
            season_type=season_type,
            is_ready=lambda ctx: self._ready(ctx, force),
            do_work=do_work,
        )


class Synthesizer(ABC):
    """L3: reads `signals`, plus the spine's `games` table for identity and schedule
    only (CLAUDE.md layer rules), and writes its own output tables, never `signals`.

    Contract: inputs_ready(ctx) -> bool | str → compute(ctx) -> Any →
    write(ctx, computed) -> WorkResult. Runs through the same `_execute` as collectors
    and analysts, so `agent_runs` logging and failure-swallowing are identical. There is
    no signals stale-row delete, because a synthesizer owns no signals.
    """

    name: str

    @abstractmethod
    def inputs_ready(self, ctx: RunContext) -> bool | str:
        """`False` skips as `skipped_fresh`; a string skips as that exact status."""

    @abstractmethod
    def compute(self, ctx: RunContext) -> Any:
        """Read via ctx.conn and build everything to write. No writes."""

    @abstractmethod
    def write(self, ctx: RunContext, computed: Any) -> WorkResult:
        """Write via ctx.conn. Returns WorkResult(rows_written, meta)."""

    def run(
        self, *, season: int, week: int, season_type: str = "REG", force: bool = False
    ) -> RunResult:
        return _execute(
            name=self.name,
            season=season,
            week=week,
            season_type=season_type,
            is_ready=(lambda ctx: True) if force else self.inputs_ready,
            do_work=lambda ctx: self.write(ctx, self.compute(ctx)),
        )


class Retention(ABC):
    """L0: the retention job, the only job that deletes data by age, on a season horizon
    (CLAUDE.md). Other jobs delete only within their own current scope, and rewrite it in
    the same run. `delete` is False unless the caller passes it explicitly: a bare
    instance, a bare CLI invocation, or any caller that forgets the argument plans and
    reports and deletes nothing.

    Same contract and `_execute` as `Grader`: inputs_ready(ctx) -> bool | str →
    compute(ctx) -> Any → write(ctx, computed) -> WorkResult. `write` deletes only when
    `self.delete` is True.
    """

    name: str

    def __init__(self, *, delete: bool = False) -> None:
        self.delete = delete

    @abstractmethod
    def inputs_ready(self, ctx: RunContext) -> bool | str:
        """`False` skips as `skipped_fresh`; a string skips as that exact status."""

    @abstractmethod
    def compute(self, ctx: RunContext) -> Any:
        """Read via ctx.conn and build the deletion plan. No writes."""

    @abstractmethod
    def write(self, ctx: RunContext, computed: Any) -> WorkResult:
        """Report the plan; delete it via ctx.conn only if `self.delete`."""

    def run(
        self, *, season: int, week: int, season_type: str = "REG", force: bool = False
    ) -> RunResult:
        return _execute(
            name=self.name,
            season=season,
            week=week,
            season_type=season_type,
            is_ready=(lambda ctx: True) if force else self.inputs_ready,
            do_work=lambda ctx: self.write(ctx, self.compute(ctx)),
        )


class Grader(ABC):
    """L0: grades locked projections after the games are played. Reads
    `projection_log` (never modifies it) plus scores and lines from `games`, and writes
    only its own grade tables.

    Same contract and `_execute` as `Synthesizer`: inputs_ready(ctx) -> bool | str →
    compute(ctx) -> Any → write(ctx, computed) -> WorkResult.
    """

    name: str

    @abstractmethod
    def inputs_ready(self, ctx: RunContext) -> bool | str:
        """`False` skips as `skipped_fresh`; a string skips as that exact status."""

    @abstractmethod
    def compute(self, ctx: RunContext) -> Any:
        """Read via ctx.conn and build everything to write. No writes."""

    @abstractmethod
    def write(self, ctx: RunContext, computed: Any) -> WorkResult:
        """Write via ctx.conn. Returns WorkResult(rows_written, meta)."""

    def run(
        self, *, season: int, week: int, season_type: str = "REG", force: bool = False
    ) -> RunResult:
        return _execute(
            name=self.name,
            season=season,
            week=week,
            season_type=season_type,
            is_ready=(lambda ctx: True) if force else self.inputs_ready,
            do_work=lambda ctx: self.write(ctx, self.compute(ctx)),
        )
