"""
Job: Delete staged nflverse player-table seasons older than the L2 horizon (keep
     season >= current season - 1). A dry run unless explicitly told to delete.
Reads: games (the current season must be on the spine; ftn's season comes from it),
       the L2 tables' season/week and row counts
Writes: deletes from ngs, ftn, pfr_advstats, player_game_pbp -- only with delete=True
        (`pipeline.run retention --delete`)
Tier: OD
Phase: P7

Policy: docs/phases/P7.md, "Retention policies". Every table the storage design names is
either L2-deleted or exempted here by name with its reason; none is covered by omission.
L4 (collapsing completed seasons of player_usage_week/player_eff_week) is not built:
it's deferred to P7 step 7 because it destroys point-in-time weekly player history.

Guards, all checked before anything is deleted:
- the current season must exist in `games`, so a mistyped `--season` can't move the
  horizon past the data;
- no table may be emptied;
- each table's delete statement must plan: every run, dry or not, EXPLAINs (never
  EXPLAIN ANALYZE) the exact DELETE it would execute, so Postgres parses and plans it
  against the real schema. The dry run's counts come from a different statement
  (`_ROWS_FROM`), so without this the delete SQL would first meet Postgres on the first
  `--delete` run;
- each table's delete must remove exactly the rows the plan counted, inside the run's one
  transaction, or the run fails and `_execute` rolls everything back.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any

import psycopg

from pipeline.core.base import Retention, RunContext, WorkResult
from pipeline.core.db import delete_rows, delete_statement

# The staged player tables P7's L2 policy was written for. L2 deletes each one unless it's
# exempted below.
L2_CANDIDATES = ("player_week", "snaps", "ngs", "ftn", "pfr_advstats", "player_game_pbp")

# Exempt from L2: the inputs Efficiency reads for season - 1 (QB continuity from
# player_week, OL continuity from snaps). Deleting a season of these isn't just a refetch
# cost: it changes what Efficiency computes for the following season. Both paths that
# recompute past seasons -- scripts/backtest.py's r-sensitivity run and
# scripts/backfill_efficiency.py -- would silently diverge from the stored signals, and
# the backtest's parity check compares one week (2021 wk 10), so it can't catch it
# (docs/phases/P5.md, open items 1-2). Decided 2026-09-27; ~13.5 MB/season kept.
L2_EXEMPT_RECOMPUTE_INPUTS = frozenset({"player_week", "snaps"})

L2_TABLES = tuple(t for t in L2_CANDIDATES if t not in L2_EXEMPT_RECOMPUTE_INPUTS)

# Every other table the storage design names, and why this job never deletes from it.
NOT_DELETED: dict[str, str] = {
    "team_week": "tiny, and the backtest reads it from 2018",
    "depth": "latest snapshot only",
    "participation_player_season": (
        "it feeds the display-only _hist values (never a prior), whose window needs the three "
        "seasons before the current one"
    ),
    "player_usage_week": (
        "L4 deferred to P7 step 7: it would destroy point-in-time weekly player history"
    ),
    "player_eff_week": (
        "L4 deferred to P7 step 7: it would destroy point-in-time weekly player history"
    ),
}

# Where each table's rows get season/week. ftn has neither column (0006), so it takes its
# game's from the spine; every other L2 table carries both.
_ROWS_FROM = {"ftn": "ftn JOIN games USING (game_id)"}
_DELETE_WHERE = {"ftn": "game_id IN (SELECT game_id FROM games WHERE season < %s)"}


@dataclass(frozen=True)
class TablePlan:
    table: str
    rows: int  # rows older than the horizon
    total_rows: int
    oldest: str | None  # "2024 wk1"
    newest: str | None
    est_mb: float | None  # the table's bytes/row on disk (incl. indexes) x rows


@dataclass(frozen=True)
class RetentionPlan:
    keep_from_season: int
    tables: list[TablePlan]
    problems: list[str]


def _season_week(code: int | None) -> str | None:
    """Decode min/max(season * 100 + week): weeks never reach 100."""
    return None if code is None else f"{code // 100} wk{code % 100}"


def plan_table(conn: psycopg.Connection, table: str, keep_from: int) -> TablePlan:
    rows_from = _ROWS_FROM.get(table, table)
    with conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT count(*) FILTER (WHERE season < %s),
                   count(*),
                   min(season * 100 + week) FILTER (WHERE season < %s),
                   max(season * 100 + week) FILTER (WHERE season < %s),
                   pg_total_relation_size(%s::regclass)
            FROM {rows_from}
            """,
            (keep_from, keep_from, keep_from, table),
        )
        row = cur.fetchone()
    assert row is not None
    rows, total, oldest, newest, size_bytes = row
    est_mb = size_bytes / total * rows / 1e6 if total else None
    return TablePlan(table, rows, total, _season_week(oldest), _season_week(newest), est_mb)


def _delete_where(table: str) -> str:
    return _DELETE_WHERE.get(table, "season < %s")


def explain_delete(conn: psycopg.Connection, table: str, keep_from: int) -> None:
    """Have Postgres plan, not run, the exact DELETE `write` would execute for `table`. A
    bad table, column, or subquery raises here, on the dry run, instead of on the first
    `--delete` run."""
    sql = delete_statement(table, _delete_where(table))
    with conn.cursor() as cur:
        try:
            cur.execute(f"EXPLAIN {sql}", (keep_from,))
        except psycopg.Error as exc:
            raise ValueError(f"{table}: its delete statement doesn't plan: {exc}") from exc


def build_plan(conn: psycopg.Connection, season: int) -> RetentionPlan:
    keep_from = season - 1
    problems: list[str] = []
    with conn.cursor() as cur:
        cur.execute("SELECT 1 FROM games WHERE season = %s LIMIT 1", (season,))
        if cur.fetchone() is None:
            problems.append(f"season {season} is not in games; refusing to set a horizon")
    for table in L2_TABLES:
        explain_delete(conn, table, keep_from)
    tables = [plan_table(conn, t, keep_from) for t in L2_TABLES]
    problems += [
        f"{p.table}: would delete all {p.total_rows} rows; refusing to empty a table"
        for p in tables
        if p.total_rows and p.rows == p.total_rows
    ]
    return RetentionPlan(keep_from, tables, problems)


def _describe(p: TablePlan, dry_run: bool, keep_from: int) -> str:
    if not p.rows:
        return f"{p.table}: nothing older than {keep_from} ({p.total_rows:,} rows kept)"
    verb = "would delete" if dry_run else "deleting"
    mb = f", ~{p.est_mb:.1f} MB" if p.est_mb is not None else ""
    return f"{p.table}: {verb} {p.rows:,} of {p.total_rows:,} rows, {p.oldest} .. {p.newest}{mb}"


class StagedRetention(Retention):
    name = "retention"

    def inputs_ready(self, ctx: RunContext) -> bool | str:
        return True  # the plan is a handful of aggregates; a dry run always reports

    def compute(self, ctx: RunContext) -> RetentionPlan:
        return build_plan(ctx.conn, ctx.season)

    def write(self, ctx: RunContext, computed: RetentionPlan) -> WorkResult:
        dry_run = not self.delete
        mode = "DRY RUN" if dry_run else "DELETE"
        keep_from = computed.keep_from_season
        print(f"[retention] {mode}: keeping season >= {keep_from}")
        for p in computed.tables:
            print(f"[retention] {mode} {_describe(p, dry_run, keep_from)}")
        for table in sorted(L2_EXEMPT_RECOMPUTE_INPUTS):
            print(f"[retention] exempt {table}: Efficiency's recompute paths read season-1")
        for problem in computed.problems:
            print(f"[retention] REFUSED: {problem}")

        meta: dict[str, Any] = {
            "dry_run": dry_run,
            "keep_from_season": keep_from,
            "tables": [asdict(p) for p in computed.tables],
            "exempt": sorted(L2_EXEMPT_RECOMPUTE_INPUTS),
            "problems": computed.problems,
        }
        if dry_run:
            return WorkResult(0, meta)
        if computed.problems:
            raise ValueError("retention refused: " + "; ".join(computed.problems))

        deleted = 0
        for p in computed.tables:
            if not p.rows:
                continue
            n = delete_rows(ctx.conn, p.table, _delete_where(p.table), [keep_from])
            if n != p.rows:
                raise ValueError(
                    f"{p.table}: deleted {n} rows but the plan counted {p.rows}; "
                    "rolling back the whole run"
                )
            deleted += n
        return WorkResult(deleted, meta)
