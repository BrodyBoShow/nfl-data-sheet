import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import psycopg
import pytest

from pipeline.core.base import Retention, RunContext, RunResult
from pipeline.orchestration import retention
from pipeline.orchestration.retention import (
    L2_CANDIDATES,
    L2_EXEMPT_RECOMPUTE_INPUTS,
    L2_TABLES,
    NOT_DELETED,
    StagedRetention,
)
from pipeline.run import _JOBS, _parse_args, main

ROOT = Path(__file__).parent.parent


def _migration_columns(table: str) -> set[str]:
    """The table's columns as the migrations define them: its CREATE TABLE plus every
    ALTER TABLE ... ADD COLUMN. Lets the fake reject a column the real table lacks."""
    cols: set[str] = set()
    for path in sorted((ROOT / "db" / "migrations").glob("*.sql")):
        sql = path.read_text()
        create = re.search(rf"CREATE TABLE {table} \((.*?)\n\);", sql, re.S)
        if create:
            cols |= set(re.findall(r"^\s+(\w+)\s", create.group(1), re.M))
        for alter in re.finditer(rf"ALTER TABLE {table}\b(.*?);", sql, re.S):
            cols |= set(re.findall(r"ADD COLUMN (\w+)", alter.group(1)))
    return cols


_COLUMNS = {t: _migration_columns(t) for t in L2_TABLES}


class _FakeCursor:
    def __init__(self, db: "_FakeDb") -> None:
        self.db = db
        self._result: tuple | None = None
        self.rowcount = 0

    def __enter__(self) -> "_FakeCursor":
        return self

    def __exit__(self, *exc: Any) -> None:
        return None

    def execute(self, sql: str, params: Any = ()) -> None:
        self.db.log.append(sql)
        if "FROM games WHERE season = %s" in sql:
            self._result = (1,) if params[0] in self.db.games else None
        elif "pg_total_relation_size" in sql:
            keep_from, table = params[0], params[3]
            rows = self.db.tables[table]
            old = [s * 100 + w for s, w in rows if s < keep_from]
            self._result = (
                len(old),
                len(rows),
                min(old, default=None),
                max(old, default=None),
                self.db.bytes_per_row * len(rows),
            )
        elif sql.startswith("EXPLAIN DELETE FROM"):
            # Plans, never runs. Like Postgres, reject a column the table doesn't have;
            # a subquery's columns belong to its own FROM, so strip it first.
            match = re.match(r"EXPLAIN DELETE FROM (\w+) WHERE (.*)", sql, re.S)
            assert match is not None
            table, where = match.groups()
            outer = re.sub(r"\(SELECT .*?\)", "", where)
            for col in re.findall(r"\b(season|week)\b", outer):
                if col not in _COLUMNS[table]:
                    raise psycopg.errors.UndefinedColumn(f'column "{col}" does not exist')
        elif sql.startswith("DELETE FROM"):
            match = re.match(r"DELETE FROM (\w+)", sql)
            assert match is not None
            table, keep_from = match.group(1), params[0]
            before = self.db.tables[table]
            self.db.tables[table] = [(s, w) for s, w in before if s >= keep_from]
            self.rowcount = len(before) - len(self.db.tables[table])
            self.rowcount += self.db.rowcount_skew.get(table, 0)
        else:
            raise AssertionError(f"unexpected SQL: {sql}")

    def fetchone(self) -> tuple | None:
        return self._result


class _FakeDb:
    """Just enough Postgres for retention: a season/week list per L2 table and the set of
    seasons on the spine. Every statement is logged so a test can see what ran."""

    def __init__(self, tables: dict[str, list[tuple[int, int]]], games: set[int]) -> None:
        self.tables = {t: list(tables.get(t, [])) for t in L2_TABLES}
        self.games = games
        self.bytes_per_row = 1000
        self.log: list[str] = []
        self.rowcount_skew: dict[str, int] = {}

    def cursor(self) -> _FakeCursor:
        return _FakeCursor(self)

    def deletes(self) -> list[str]:
        return [s for s in self.log if s.startswith("DELETE")]

    def explains(self) -> list[str]:
        return [s for s in self.log if s.startswith("EXPLAIN")]


def _weeks(*seasons: int) -> list[tuple[int, int]]:
    return [(s, w) for s in seasons for w in (1, 9, 18)]


def _ctx(db: _FakeDb, season: int = 2026) -> RunContext:
    return RunContext(
        season=season,
        week=3,
        season_type="REG",
        now=datetime(2026, 9, 27, tzinfo=UTC),
        settings=None,  # type: ignore[arg-type]
        conn=db,  # type: ignore[arg-type]
    )


def _db() -> _FakeDb:
    return _FakeDb({t: _weeks(2023, 2024, 2025, 2026) for t in L2_TABLES}, {2025, 2026})


def _run(job: StagedRetention, db: _FakeDb, season: int = 2026):
    ctx = _ctx(db, season)
    return job.write(ctx, job.compute(ctx))


# --- which tables the job may delete from ---------------------------------------------------


def test_every_table_is_either_l2_deleted_or_named_with_a_reason():
    """A new table can't fall out of the policy by omission: every staged player table and
    every table 0028-0032 creates is classified here."""
    created = set()
    for path in sorted((ROOT / "db" / "migrations").glob("00[23][0-9]_*.sql")):
        if "0028" <= path.name[:4] <= "0032":
            created |= set(re.findall(r"CREATE TABLE (\w+)", path.read_text()))
    classified = set(L2_TABLES) | L2_EXEMPT_RECOMPUTE_INPUTS | set(NOT_DELETED)
    assert created <= classified, created - classified
    assert set(L2_CANDIDATES) <= set(L2_TABLES) | L2_EXEMPT_RECOMPUTE_INPUTS
    assert not set(L2_TABLES) & set(NOT_DELETED)
    assert set(L2_TABLES) == {"ngs", "ftn", "pfr_advstats", "player_game_pbp"}


def test_efficiencys_recompute_inputs_are_never_deletable():
    """Read Efficiency's own SQL for the staged tables it queries (it reads each one for
    season and season-1, and scripts/backtest.py + scripts/backfill_efficiency.py recompute
    past seasons through it). None of them may be an L2 table."""
    source = (ROOT / "pipeline" / "analysts" / "efficiency.py").read_text()
    read = set(re.findall(r"FROM (\w+)", source)) & set(L2_CANDIDATES)
    assert read == {"player_week", "snaps"}  # if Efficiency starts reading more, re-decide
    assert read <= L2_EXEMPT_RECOMPUTE_INPUTS
    assert not read & set(L2_TABLES)

    # And operationally: a delete run with old rows everywhere touches neither.
    db = _db()
    _run(StagedRetention(delete=True), db)
    assert not [s for s in db.deletes() if re.search(r"\b(player_week|snaps)\b", s)]


# --- dry run is the default -------------------------------------------------------------------


def test_a_bare_instance_is_a_dry_run_and_deletes_nothing(capsys):
    job = StagedRetention()
    assert job.delete is False
    db = _db()
    before = {t: list(rows) for t, rows in db.tables.items()}
    result = _run(job, db)

    assert db.deletes() == []
    assert db.tables == before
    assert result.rows_written == 0
    assert result.meta["dry_run"] is True
    out = capsys.readouterr().out
    assert "[retention] DRY RUN: keeping season >= 2025" in out
    # table, row count, oldest/newest affected
    assert "[retention] DRY RUN ngs: would delete 6 of 12 rows, 2023 wk1 .. 2024 wk18" in out


def test_a_bare_cli_invocation_is_a_dry_run(monkeypatch):
    seen: list[bool] = []

    def fake_run(self, *, season, week, season_type="REG", force=False):
        seen.append(self.delete)
        return RunResult(self.name, "success", 0, 0.0)

    monkeypatch.setattr(Retention, "run", fake_run)
    registered = _JOBS["retention"]
    assert isinstance(registered, StagedRetention)
    assert registered.delete is False
    parsed = _parse_args(["retention"])
    assert parsed is not None and parsed[6] is False

    assert main(["retention", "--season", "2026", "--week", "3"]) == 0
    assert main(["retention", "--season", "2026", "--week", "3", "--delete"]) == 0
    assert seen == [False, True]
    assert registered.delete is False  # --delete never mutates the registered job


def test_delete_flag_is_rejected_for_other_jobs(capsys):
    # --season/--week given, so a broken check can't reach nflreadpy's network-backed
    # get_current_week() before conftest's live-database block stops it.
    assert main(["efficiency", "--delete", "--season", "2026", "--week", "3"]) == 1
    assert "--delete is only valid for the retention job" in capsys.readouterr().err


# --- deleting --------------------------------------------------------------------------------


def test_delete_removes_only_seasons_before_the_horizon(capsys):
    db = _db()
    result = _run(StagedRetention(delete=True), db)
    for table in L2_TABLES:
        assert {s for s, _ in db.tables[table]} == {2025, 2026}
    assert result.rows_written == 6 * len(L2_TABLES)
    assert "[retention] DELETE ftn: deleting 6 of 12 rows" in capsys.readouterr().out


def test_ftn_deletes_through_its_games_season():
    """ftn has no season column; its rows are counted and deleted by their game's."""
    assert "JOIN games" in retention._ROWS_FROM["ftn"]
    assert "FROM games WHERE season < %s" in retention._DELETE_WHERE["ftn"]


def test_the_fakes_columns_come_from_the_migrations():
    """The EXPLAIN tests below lean on this: ftn has no season/week (0006), the rest do."""
    assert "season" not in _COLUMNS["ftn"] and "game_id" in _COLUMNS["ftn"]
    for table in ("ngs", "pfr_advstats", "player_game_pbp"):
        assert {"season", "week"} <= _COLUMNS[table]


def test_every_dry_run_explains_the_exact_delete_it_would_run():
    """The dry run's counts use _ROWS_FROM, not the delete SQL. EXPLAIN is what puts the
    delete statement itself in front of Postgres before any --delete run."""
    dry = _db()
    _run(StagedRetention(), dry)
    assert dry.deletes() == []
    assert [s.split()[3] for s in dry.explains()] == list(L2_TABLES)  # EXPLAIN DELETE FROM t

    # Same statements, character for character, as the ones a delete run executes.
    wet = _db()
    _run(StagedRetention(delete=True), wet)
    assert dry.explains() == ["EXPLAIN " + s for s in wet.deletes()]


def test_a_delete_statement_postgres_cant_plan_fails_the_dry_run(monkeypatch):
    # ftn's delete as if it had its own season column: what the code did before
    # _DELETE_WHERE. Only the EXPLAIN sees it; the dry run's counts go through the join.
    monkeypatch.setattr(retention, "_DELETE_WHERE", {})
    db = _db()
    with pytest.raises(
        ValueError, match=r'ftn: its delete statement doesn\'t plan: column "season"'
    ):
        _run(StagedRetention(), db)
    assert db.deletes() == []


def test_nothing_to_delete_is_reported_not_skipped(capsys):
    db = _FakeDb({t: _weeks(2025, 2026) for t in L2_TABLES}, {2025, 2026})
    result = _run(StagedRetention(delete=True), db)
    assert result.rows_written == 0
    assert db.deletes() == []
    assert "ngs: nothing older than 2025 (6 rows kept)" in capsys.readouterr().out


# --- guards ----------------------------------------------------------------------------------


def test_refuses_a_season_that_is_not_on_the_spine():
    # games holds 2025-2026. An off-by-one --season 2027 would move the horizon to 2026 and
    # delete all of 2025 -- the season the player priors read -- without emptying any
    # table, so the never-empty guard can't catch it. Only the spine check does.
    db = _db()
    with pytest.raises(ValueError, match="season 2027 is not in games"):
        _run(StagedRetention(delete=True), db, season=2027)
    assert db.deletes() == []
    assert all({s for s, _ in rows} >= {2025} for rows in db.tables.values())

    # The dry run reports the refusal instead of raising.
    result = _run(StagedRetention(), _db(), season=2027)
    assert result.meta["problems"] == ["season 2027 is not in games; refusing to set a horizon"]


def test_refuses_to_empty_any_table_and_deletes_nothing_anywhere():
    tables = {t: _weeks(2023, 2025, 2026) for t in L2_TABLES}
    tables["ngs"] = _weeks(2023)  # only old rows: the horizon would empty it
    db = _FakeDb(tables, {2025, 2026})
    with pytest.raises(ValueError, match="ngs: would delete all 3 rows"):
        _run(StagedRetention(delete=True), db)
    assert db.deletes() == []  # the other tables' deletes never started either


def test_a_delete_that_removes_a_different_count_than_planned_fails_the_run():
    db = _db()
    db.rowcount_skew["pfr_advstats"] = 1
    with pytest.raises(ValueError, match="pfr_advstats: deleted 7 rows but the plan counted 6"):
        _run(StagedRetention(delete=True), db)
