"""The per-analyst input gate (pipeline/core/input_gate.py), on fakes only.

docs/phases/P7.md, step 9, the gate spec. What the real engine does with the digest and
lookup statements is checked by scripts/verify_input_gate.py instead.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
from collections.abc import Mapping
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import polars as pl
import psycopg
import pytest

import pipeline.core.base as base
import pipeline.core.input_gate as ig
from pipeline.analysts import player_efficiency, usage
from pipeline.core.base import Readiness, RunContext, RunResult, WorkResult
from pipeline.core.logging import RunStatus
from pipeline.orchestration.auditor import check_gate_inputs, summarize_gate_audit

ROOT = Path(__file__).resolve().parents[1]
GATES = {
    "usage": (usage, usage.INPUT_GATE),
    "player_efficiency": (player_efficiency, player_efficiency.INPUT_GATE),
}
NOW = datetime(2026, 10, 7, 12, tzinfo=UTC)


# --------------------------------------------------------------------------------------
# Fakes
# --------------------------------------------------------------------------------------


class _Cursor:
    def __init__(self, conn: _Conn) -> None:
        self.conn = conn
        self.rows: list[tuple[Any, ...]] = []

    def execute(self, sql: str, params: tuple[Any, ...] = ()) -> None:
        self.conn.statements.append((sql, params))
        self.rows = self.conn.respond(sql, params)

    def fetchone(self) -> tuple[Any, ...] | None:
        return self.rows[0] if self.rows else None

    def fetchall(self) -> list[tuple[Any, ...]]:
        return list(self.rows)

    def __enter__(self) -> _Cursor:
        return self

    def __exit__(self, *exc: object) -> None:
        pass


class _Conn:
    """Answers the gate's three statements, plus what `_execute` and `inputs_ready` send.
    `fail` names the gate statements that raise, as a broken query would."""

    def __init__(
        self,
        gate: ig.InputGate,
        *,
        markers: dict[str, str] | None = None,
        digests: Mapping[str, str | None] | None = None,
        last: tuple[int, dict[str, Any]] | None = None,
        fail: frozenset[str] = frozenset(),
    ) -> None:
        self.gate = gate
        self.markers = markers if markers is not None else {i.marker: "m1" for i in gate.inputs}
        self.digests = digests if digests is not None else {i.table: "d1" for i in gate.inputs}
        self.last = last
        self.fail = fail
        self.statements: list[tuple[str, tuple[Any, ...]]] = []
        self.savepoints = 0
        self.finished: list[tuple[Any, ...]] = []

    def respond(self, sql: str, params: tuple[Any, ...]) -> list[tuple[Any, ...]]:
        if sql.startswith("SELECT source, last_value FROM source_freshness"):
            if "markers" in self.fail:
                raise psycopg.OperationalError("markers boom")
            return [(s, v) for s, v in self.markers.items() if s in params[0]]
        if sql.startswith("SELECT (SELECT coalesce(md5"):
            if "digest" in self.fail:
                raise psycopg.errors.UndefinedColumn("digest boom")
            return [tuple(self.digests.get(i.table) for i in self.gate.inputs)]
        if sql.startswith("SELECT id, meta->'gate' FROM agent_runs"):
            if "lookup" in self.fail:
                raise psycopg.OperationalError("lookup boom")
            return [self.last] if self.last else []
        if sql.startswith("INSERT INTO agent_runs"):
            return [(77,)]
        if "UPDATE agent_runs" in sql:
            self.finished.append(params)
            return []
        if sql.startswith("SELECT count(*) FROM"):
            return [(10,)]
        raise AssertionError(f"unexpected statement: {sql[:80]}")

    def cursor(self) -> _Cursor:
        return _Cursor(self)

    @contextmanager
    def transaction(self):
        self.savepoints += 1
        yield

    def commit(self) -> None:
        pass

    def rollback(self) -> None:
        pass

    def gate_lookups(self) -> int:
        return sum(1 for s, _ in self.statements if "FROM agent_runs" in s and "meta->'gate'" in s)


def _ctx(conn: _Conn, season: int = 2026, week: int = 4) -> RunContext:
    return RunContext(season, week, "REG", NOW, settings=None, conn=conn)  # type: ignore[arg-type]


@pytest.fixture(params=[ig.MARKER, ig.CONTENT])
def branch(request, monkeypatch) -> str:
    monkeypatch.setattr(ig, "GATE_KEY_SOURCE", request.param)
    return request.param


def _first(gate: ig.InputGate, **conn_kw: Any) -> dict[str, Any]:
    """The gate meta of a first run (nothing stored yet) on these inputs."""
    verdict = gate.check(_ctx(_Conn(gate, **conn_kw)), force=False)
    assert verdict.status is None
    return verdict.meta["gate"]


# --------------------------------------------------------------------------------------
# Unselected branch: a hard error at call time
# --------------------------------------------------------------------------------------


def test_the_shipped_branch_is_content():
    """The branch picked on 2026-10-07 (docs/phases/P7.md, step 9, "Branch picked"). The
    unselected-branch tests below set the constant themselves, so they still hold."""
    assert ig.GATE_KEY_SOURCE == ig.CONTENT
    assert ig.selected_branch() == ig.CONTENT


@pytest.mark.parametrize("value", [None, "markers", "both", ""])
def test_unselected_branch_is_a_hard_error_at_call_time(monkeypatch, value):
    monkeypatch.setattr(ig, "GATE_KEY_SOURCE", value)
    gate = usage.INPUT_GATE
    with pytest.raises(ig.GateNotSelected):
        gate.check(_ctx(_Conn(gate)), force=False)
    with pytest.raises(ig.GateNotSelected):
        gate.check(_ctx(_Conn(gate)), force=True)


def _fake_execute(monkeypatch, conn: _Conn) -> None:
    @contextmanager
    def connect():
        yield conn

    monkeypatch.setattr(base, "get_connection", connect)
    monkeypatch.setattr(base, "get_settings", lambda: None)


def _finished(conn: _Conn) -> tuple[str, dict[str, Any], str | None]:
    """(status, meta, error) of the one finish_run UPDATE."""
    assert len(conn.finished) == 1
    _, status, _, _, error, meta, _ = conn.finished[0]
    return status, json.loads(meta), error


@pytest.mark.parametrize("force", [False, True])
def test_unselected_branch_fails_the_run_never_runs_or_skips(monkeypatch, force):
    monkeypatch.setattr(ig, "GATE_KEY_SOURCE", None)
    conn = _Conn(usage.INPUT_GATE)
    _fake_execute(monkeypatch, conn)
    analyst = usage.UsageAnalyst()
    monkeypatch.setattr(analyst, "compute", lambda ctx: pytest.fail("compute ran"))
    result = analyst.run(season=2026, week=4, force=force)
    status, _, error = _finished(conn)
    assert result.status == status == "failed"
    assert error is not None and error.startswith("GateNotSelected")


# --------------------------------------------------------------------------------------
# The decision
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize("name", GATES)
def test_identical_inputs_skip_with_the_matched_run(branch, name):
    gate = GATES[name][1]
    first = _first(gate)
    assert first["decision"] == "run:no_prior_success"
    verdict = gate.check(_ctx(_Conn(gate, last=(41, first))), force=False)
    assert verdict.status == "skipped_unchanged"
    g = verdict.meta["gate"]
    assert (g["decision"], g["matched_run_id"], g["key"]) == ("skip:unchanged", 41, first["key"])
    assert g["parts"] == first["parts"]
    assert g["branch"] == branch


@pytest.mark.parametrize("name", GATES)
def test_key_is_stable_for_identical_inputs_and_moves_with_each_part(branch, name):
    gate = GATES[name][1]
    key = _first(gate)["key"]
    assert _first(gate)["key"] == key
    for i in gate.inputs:
        if branch == ig.MARKER:
            moved = _first(gate, markers={j.marker: "m1" for j in gate.inputs} | {i.marker: "m2"})
        else:
            moved = _first(gate, digests={j.table: "d1" for j in gate.inputs} | {i.table: "d2"})
        assert moved["key"] != key, i.table
        verdict = gate.check(_ctx(_Conn(gate, last=(41, moved))), force=False)
        assert verdict.status is None
        assert verdict.meta["gate"]["decision"] == "run:changed"
        assert verdict.meta["gate"]["changed"] == [i.table]
    other_week = gate.check(_ctx(_Conn(gate), week=5), force=False).meta["gate"]
    assert other_week["key"] != key


def test_code_change_moves_the_key(branch, monkeypatch):
    gate = usage.INPUT_GATE
    first = _first(gate)
    monkeypatch.setattr(ig, "code_fingerprint", lambda files: "f" * 64)
    verdict = gate.check(_ctx(_Conn(gate, last=(41, first))), force=False)
    assert verdict.status is None
    assert verdict.meta["gate"]["changed"] == ["code"]


def test_empty_input_is_decidable_not_unknown(monkeypatch):
    monkeypatch.setattr(ig, "GATE_KEY_SOURCE", ig.CONTENT)
    gate = player_efficiency.INPUT_GATE
    empty = {i.table: ig.EMPTY for i in gate.inputs}
    first = _first(gate, digests=empty)
    assert first["decision"] == "run:no_prior_success"
    verdict = gate.check(_ctx(_Conn(gate, digests=empty, last=(41, first))), force=False)
    assert verdict.status == "skipped_unchanged"


# Each undecidable case runs, and says why (spec item 3).


def test_no_prior_success_runs(branch):
    gate = usage.INPUT_GATE
    conn = _Conn(gate)
    verdict = gate.check(_ctx(conn), force=False)
    assert verdict.status is None
    assert verdict.meta["gate"]["decision"] == "run:no_prior_success"
    _, params = next(s for s in conn.statements if "FROM agent_runs" in s[0])
    assert params == ("usage", "2026", "4")


def test_other_gate_version_runs(branch):
    gate = usage.INPUT_GATE
    stored = _first(gate) | {"version": ig.GATE_VERSION - 1}
    verdict = gate.check(_ctx(_Conn(gate, last=(41, stored))), force=False)
    assert verdict.status is None
    assert verdict.meta["gate"]["decision"] == "run:other_gate_version"


def test_other_branch_runs(branch):
    gate = usage.INPUT_GATE
    other = ig.CONTENT if branch == ig.MARKER else ig.MARKER
    stored = _first(gate) | {"branch": other}
    verdict = gate.check(_ctx(_Conn(gate, last=(41, stored))), force=False)
    assert verdict.status is None
    assert verdict.meta["gate"]["decision"] == "run:other_branch"


def test_prior_success_without_a_key_runs(branch):
    """A success whose own gate errored has no key. It's the newest write, so it can't be
    skipped over to compare against an older success."""
    gate = usage.INPUT_GATE
    stored = {"version": ig.GATE_VERSION, "branch": branch, "decision": "run:gate_error"}
    verdict = gate.check(_ctx(_Conn(gate, last=(41, stored))), force=False)
    assert verdict.status is None
    assert verdict.meta["gate"]["decision"] == "run:prior_has_no_key"


def test_missing_marker_is_unknown_and_runs(monkeypatch):
    monkeypatch.setattr(ig, "GATE_KEY_SOURCE", ig.MARKER)
    gate = player_efficiency.INPUT_GATE
    markers = {i.marker: "m1" for i in gate.inputs if i.table != "players"}
    conn = _Conn(gate, markers=markers)
    verdict = gate.check(_ctx(conn), force=False)
    assert verdict.status is None
    g = verdict.meta["gate"]
    assert g["decision"] == "run:unknown_part"
    assert g["unknown"] == ["players"]
    assert g["parts"]["inputs"]["players"] == ig.UNKNOWN
    assert conn.gate_lookups() == 0


@pytest.mark.parametrize(("chosen", "fail"), [(ig.MARKER, "markers"), (ig.CONTENT, "digest")])
def test_input_query_error_is_unknown_and_runs_inside_a_savepoint(monkeypatch, chosen, fail):
    monkeypatch.setattr(ig, "GATE_KEY_SOURCE", chosen)
    gate = player_efficiency.INPUT_GATE
    conn = _Conn(gate, fail=frozenset({fail}))
    verdict = gate.check(_ctx(conn), force=False)
    assert verdict.status is None
    g = verdict.meta["gate"]
    assert g["decision"] == "run:unknown_part"
    assert g["unknown"] == sorted(i.table for i in gate.inputs)
    assert "boom" in g["errors"]["inputs"]
    # The failed read was inside conn.transaction(), so the run's transaction survives.
    assert conn.savepoints == 1


def test_unknown_never_equals_unknown(branch):
    """A stored key taken with the same unknown part, even an identical key, never
    matches: an unknown part runs before any comparison."""
    gate = usage.INPUT_GATE
    fail = frozenset({"markers" if branch == ig.MARKER else "digest"})
    stored = _first(gate, fail=fail)
    verdict = gate.check(_ctx(_Conn(gate, fail=fail, last=(41, stored))), force=False)
    assert verdict.meta["gate"]["key"] == stored["key"]
    assert verdict.status is None
    assert verdict.meta["gate"]["decision"] == "run:unknown_part"


def test_unreadable_code_is_unknown_and_runs(branch, monkeypatch):
    gate = usage.INPUT_GATE

    def broken(files):
        raise OSError("file gone")

    monkeypatch.setattr(ig, "code_fingerprint", broken)
    verdict = gate.check(_ctx(_Conn(gate)), force=False)
    assert verdict.status is None
    assert verdict.meta["gate"]["unknown"] == ["code"]
    assert "file gone" in verdict.meta["gate"]["errors"]["code"]


def test_lookup_error_runs(branch):
    gate = usage.INPUT_GATE
    verdict = gate.check(_ctx(_Conn(gate, fail=frozenset({"lookup"}))), force=False)
    assert verdict.status is None
    assert verdict.meta["gate"]["decision"] == "run:lookup_error"
    assert "lookup boom" in verdict.meta["gate"]["lookup_error"]


def test_any_other_gate_exception_runs(branch, monkeypatch):
    gate = usage.INPUT_GATE

    def broken(self, *a, **kw):
        raise KeyError("unexpected")

    monkeypatch.setattr(ig.InputGate, "_decide", broken)
    verdict = gate.check(_ctx(_Conn(gate)), force=False)
    assert verdict.status is None
    assert verdict.meta["gate"]["decision"] == "run:gate_error"
    assert verdict.meta["gate"]["season"] == 2026 and verdict.meta["gate"]["week"] == 4


# --------------------------------------------------------------------------------------
# --force, and the gate through _execute
# --------------------------------------------------------------------------------------


def test_force_takes_the_key_without_comparing(branch):
    gate = usage.INPUT_GATE
    first = _first(gate)
    conn = _Conn(gate, last=(41, first))
    verdict = gate.check(_ctx(conn), force=True)
    assert verdict.status is None
    g = verdict.meta["gate"]
    assert g["decision"] == "run:forced"
    assert g["key"] == first["key"] and g["parts"] == first["parts"]
    assert "matched_run_id" not in g and "compared_run_id" not in g
    assert conn.gate_lookups() == 0


def _stub_work(monkeypatch, analyst) -> None:
    monkeypatch.setattr(analyst, "compute", lambda ctx: pl.DataFrame())
    monkeypatch.setattr(analyst, "write_signals", lambda ctx, df: WorkResult(3, {"rows": 3}))


def test_forced_run_skips_the_no_data_check_and_stores_its_key(branch, monkeypatch):
    conn = _Conn(usage.INPUT_GATE)
    _fake_execute(monkeypatch, conn)
    analyst = usage.UsageAnalyst()
    _stub_work(monkeypatch, analyst)
    result = analyst.run(season=2026, week=4, force=True)
    status, meta, _ = _finished(conn)
    assert result.status == status == "success"
    assert meta["gate"]["decision"] == "run:forced" and meta["gate"]["key"]
    assert meta["rows"] == 3
    assert not any(s.startswith("SELECT count(*)") for s, _ in conn.statements)


def test_skip_meta_reaches_agent_runs(branch, monkeypatch):
    gate = usage.INPUT_GATE
    first = _first(gate)
    conn = _Conn(gate, last=(41, first))
    _fake_execute(monkeypatch, conn)
    analyst = usage.UsageAnalyst()
    monkeypatch.setattr(analyst, "compute", lambda ctx: pytest.fail("compute ran on a skip"))
    result = analyst.run(season=2026, week=4)
    status, meta, _ = _finished(conn)
    assert result.status == status == "skipped_unchanged"
    g = meta["gate"]
    assert (g["key"], g["parts"], g["matched_run_id"]) == (first["key"], first["parts"], 41)


def test_gate_exception_runs_the_analyst_never_fails_or_skips(branch, monkeypatch):
    def broken(self, *a, **kw):
        raise ZeroDivisionError("gate bug")

    monkeypatch.setattr(ig.InputGate, "_decide", broken)
    conn = _Conn(usage.INPUT_GATE)
    _fake_execute(monkeypatch, conn)
    analyst = usage.UsageAnalyst()
    _stub_work(monkeypatch, analyst)
    result = analyst.run(season=2026, week=4)
    status, meta, _ = _finished(conn)
    assert result.status == status == "success"
    assert meta["gate"]["decision"] == "run:gate_error"
    assert "gate bug" in meta["gate"]["error"]


def test_failed_run_keeps_its_gate_meta(branch, monkeypatch):
    conn = _Conn(usage.INPUT_GATE)
    _fake_execute(monkeypatch, conn)
    analyst = usage.UsageAnalyst()

    def boom(ctx):
        raise RuntimeError("compute broke")

    monkeypatch.setattr(analyst, "compute", boom)
    result = analyst.run(season=2026, week=4)
    status, meta, _ = _finished(conn)
    assert result.status == status == "failed"
    assert meta["gate"]["decision"] == "run:no_prior_success" and meta["gate"]["key"]


def test_no_current_season_rows_still_skips_first(branch, monkeypatch):
    """inputs_ready's existing check stays first: no data skips as before, and the gate
    never runs."""
    conn = _Conn(usage.INPUT_GATE)
    conn.respond = _no_rows(conn.respond)  # type: ignore[method-assign]
    _fake_execute(monkeypatch, conn)
    result = usage.UsageAnalyst().run(season=2026, week=4)
    status, meta, _ = _finished(conn)
    assert result.status == status == "skipped_fresh"
    assert meta == {}


def _no_rows(respond):
    def wrapped(sql, params):
        return [(0,)] if sql.startswith("SELECT count(*) FROM") else respond(sql, params)

    return wrapped


def test_ungated_readiness_is_unchanged():
    assert base._resolve_skip_status(Readiness(None, {"gate": {}})) is None
    assert base._resolve_skip_status(Readiness("skipped_unchanged")) == "skipped_unchanged"


# --------------------------------------------------------------------------------------
# An undeclared input must be impossible (spec item 5)
# --------------------------------------------------------------------------------------


class _RecordingCursor:
    """Answers every SELECT `_fetch` sends with one row: `player_id` is
    '<table>_pid', every other column NULL. Records each statement."""

    def __init__(self, log: list[tuple[str, str, tuple[Any, ...]]]) -> None:
        self.log = log
        self.description: list[Any] = []
        self.rows: list[tuple[Any, ...]] = []

    def execute(self, sql: str, params: tuple[Any, ...] = ()) -> None:
        tables = re.findall(r"\b(?:FROM|JOIN)\s+([a-z_][a-z0-9_]*)", sql)
        assert len(tables) == 1, sql
        self.log.append((tables[0], sql, params))
        cols = [c.strip() for c in sql.split("SELECT", 1)[1].split(" FROM ", 1)[0].split(",")]
        self.description = [type("Col", (), {"name": c}) for c in cols]
        self.rows = [tuple(f"{tables[0]}_pid" if c == "player_id" else None for c in cols)]

    def fetchall(self) -> list[tuple[Any, ...]]:
        return self.rows

    def __enter__(self) -> _RecordingCursor:
        return self

    def __exit__(self, *exc: object) -> None:
        pass


class _RecordingConn:
    def __init__(self) -> None:
        self.log: list[tuple[str, str, tuple[Any, ...]]] = []

    def cursor(self) -> _RecordingCursor:
        return _RecordingCursor(self.log)


def _undeclared(module: Any, gate: ig.InputGate) -> set[str]:
    conn = _RecordingConn()
    module._fetch(conn, 2026, 4)
    return {table for table, _, _ in conn.log} - {i.table for i in gate.inputs}


@pytest.mark.parametrize("name", GATES)
def test_every_table_fetch_reads_is_a_declared_gate_input(name):
    module, gate = GATES[name]
    assert _undeclared(module, gate) == set()


def test_the_check_catches_an_undeclared_input():
    """The position_group miss (P7 open item 12): a gate without `players` fails."""
    gate = player_efficiency.INPUT_GATE
    without = ig.InputGate(
        gate.agent, gate.module, tuple(i for i in gate.inputs if i.table != "players")
    )
    assert _undeclared(player_efficiency, without) == {"players"}


@pytest.mark.parametrize("name", GATES)
def test_each_digest_filters_exactly_as_fetch_reads(name):
    """Each read's WHERE and parameters are the gate input's own. For `players`, read by
    the id list the other reads returned, the ids are exactly the union of the inputs
    its digest rebuilds them from."""
    module, gate = GATES[name]
    conn = _RecordingConn()
    module._fetch(conn, 2026, 4)
    reads = {table: (sql, params) for table, sql, params in conn.log}
    for i in gate.inputs:
        sql, params = reads[i.table]
        assert sql.endswith(f"WHERE {i.read_where}"), i.table
        if i.ids_from:
            assert set(params[0]) == {f"{s.table}_pid" for s in i.ids_from}
        else:
            assert params == i.digest_filter(2026, 4)[1], i.table


@pytest.mark.parametrize("name", GATES)
def test_markers_are_the_inputs_version_keys_plus_players(name):
    module, gate = GATES[name]
    assert {i.marker for i in gate.inputs} == set(module._INPUTS_VERSION_KEYS) | {
        "nflverse:players"
    }


@pytest.mark.parametrize("name", GATES)
def test_analyst_carries_its_gate(name):
    module, gate = GATES[name]
    analyst = (
        usage.UsageAnalyst() if name == "usage" else player_efficiency.PlayerEfficiencyAnalyst()
    )
    assert analyst.input_gate is gate
    assert gate.agent == analyst.name and gate.module == module.__name__


def test_players_digest_tells_a_null_group_from_a_missing_player():
    players = next(i for i in usage.INPUT_GATE.inputs if i.table == "players")
    assert "quote_nullable(position_group)" in players.row_text()
    assert players.audit is False


# --------------------------------------------------------------------------------------
# The code fingerprint is the import closure, parsed, never a list
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize("name", GATES)
def test_fingerprint_files_are_exactly_what_importing_the_analyst_loads(name):
    """Independent of the parser: a fresh interpreter imports the analyst, and every
    `pipeline` module it loaded must be in the fingerprint, and nothing else is."""
    module, gate = GATES[name]
    probe = (
        "import importlib, sys; importlib.import_module(sys.argv[1]); "
        "print('\\n'.join(m.__file__ for n, m in list(sys.modules.items()) "
        "if n == 'pipeline' or n.startswith('pipeline.')))"
    )
    out = subprocess.run(
        [sys.executable, "-c", probe, module.__name__],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    loaded = {Path(p).resolve() for p in out.split()}
    py_files = {p for p in gate.code_files() if p.suffix == ".py"}
    assert py_files == loaded
    assert module._MIGRATION.resolve() in gate.code_files()


def _write(root: Path, rel: str, text: str) -> Path:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def test_parser_follows_relative_and_module_imports(tmp_path):
    files = {
        "pipeline/__init__.py": "",
        "pipeline/core/__init__.py": "",
        "pipeline/analysts/__init__.py": "",
        "pipeline/analysts/a.py": "import polars\nfrom pipeline.core.b import X\n"
        "from pipeline.core import d\n",
        "pipeline/core/b.py": "from .c import Y\nfrom . import e as ee\nX = 1\n",
        "pipeline/core/c.py": "def f():\n    import pipeline.core.lazy\nY = 2\n",
        "pipeline/core/d.py": "",
        "pipeline/core/e.py": "",
        "pipeline/core/lazy.py": "",
        "pipeline/core/unused.py": "",
    }
    for rel, text in files.items():
        _write(tmp_path, rel, text)
    found = {
        p.relative_to(tmp_path).as_posix() for p in ig.code_files("pipeline.analysts.a", tmp_path)
    }
    assert found == set(files) - {"pipeline/core/unused.py"}


def test_fingerprint_moves_on_any_byte_but_not_on_line_endings(tmp_path):
    a = _write(tmp_path, "pipeline/a.py", "x = 1\ny = 2\n")
    b = _write(tmp_path, "pipeline/b.py", "z = 3\n")
    fp = ig.code_fingerprint([a, b], tmp_path)
    assert ig.code_fingerprint([b, a], tmp_path) == fp
    a.write_bytes(b"x = 1\r\ny = 2\r\n")
    assert ig.code_fingerprint([a, b], tmp_path) == fp
    a.write_text("x = 1\ny = 3\n", encoding="utf-8")
    assert ig.code_fingerprint([a, b], tmp_path) != fp


# --------------------------------------------------------------------------------------
# The status, its migration, and the CLI
# --------------------------------------------------------------------------------------


def test_run_status_matches_the_newest_status_migration():
    newest = None
    for path in sorted((ROOT / "db" / "migrations").glob("*.sql")):
        text = path.read_text(encoding="utf-8")
        if "agent_runs_status_check" in text and "CHECK (status IN" in text:
            newest = text
    assert newest is not None
    allowed = re.findall(r"'([a-z_]+)'", newest.split("CHECK (status IN", 1)[1].split(")", 1)[0])
    assert set(allowed) == set(RunStatus.__args__)  # type: ignore[attr-defined]


def test_cli_exits_zero_on_a_gate_skip(monkeypatch, capsys):
    import pipeline.run as cli

    class Skips:
        def run(self, **kw: Any) -> RunResult:
            return RunResult("usage", "skipped_unchanged", 0, 0.1)

    monkeypatch.setitem(cli._JOBS, "usage", Skips())
    assert cli.main(["usage", "--season", "2026", "--week", "4"]) == 0
    assert "usage: skipped_unchanged" in capsys.readouterr().out


# --------------------------------------------------------------------------------------
# The auditor check: timestamps, never the gate's verdict
# --------------------------------------------------------------------------------------
# Coverage gap, found 2026-10-05: these tests didn't catch check_gate_inputs treating "no
# gated success" as a success at the epoch, because their never-run case has no changed
# inputs. "A never-run analyst must not alert" is tested in tests/test_auditor.py
# (test_gate_audit_of_a_never_run_analyst_is_a_noop and the audit_and_alert tests), which
# count as part of this check's coverage. docs/phases/P7.md, "Coverage gap found by
# break 4".

SUCCESS_AT = datetime(2026, 10, 6, 12, tzinfo=UTC)


class _AuditConn:
    """agent_runs and per-input max(updated_at) for check_gate_inputs."""

    def __init__(
        self,
        success: tuple[int, datetime] | None,
        latest: Mapping[str, datetime | None],
        later: tuple[list[int], int | None, int] = ([], None, 0),
    ) -> None:
        self.success, self.latest, self.later = success, latest, later
        self.statements: list[tuple[str, tuple[Any, ...]]] = []

    def respond(self, sql: str, params: tuple[Any, ...]) -> list[tuple[Any, ...]]:
        if sql.startswith("SELECT id, started_at FROM agent_runs"):
            return [self.success] if self.success else []
        if sql.startswith("SELECT (SELECT max(updated_at)"):
            tables = re.findall(r"FROM ([a-z_]+) WHERE", sql)
            return [tuple(self.latest.get(t) for t in tables)]
        if sql.startswith("SELECT (SELECT coalesce(array_agg"):
            return [self.later]
        raise AssertionError(sql[:80])

    def cursor(self) -> _Cursor:
        return _Cursor(self)  # type: ignore[arg-type]


def _audit(conn: _AuditConn) -> list[str]:
    return check_gate_inputs(conn, player_efficiency.INPUT_GATE, 2026, 4)  # type: ignore[arg-type]


def test_audit_never_reads_players_updated_at():
    conn = _AuditConn((5, SUCCESS_AT), {})
    _audit(conn)
    sql = next(s for s, _ in conn.statements if s.startswith("SELECT (SELECT max"))
    assert "FROM players" not in sql
    assert set(re.findall(r"FROM ([a-z_]+) WHERE", sql)) == {
        i.table for i in player_efficiency.INPUT_GATE.inputs if i.table != "players"
    }


def test_audit_quiet_without_a_change_or_a_success():
    assert _audit(_AuditConn(None, {})) == []
    before = {"snaps": SUCCESS_AT - timedelta(hours=1)}
    assert _audit(_AuditConn((5, SUCCESS_AT), before, ([9], 11, 0))) == []


def test_audit_alerts_on_a_skip_after_the_change():
    changed = {"snaps": SUCCESS_AT + timedelta(hours=1)}
    messages = _audit(_AuditConn((5, SUCCESS_AT), changed, ([9], 11, 1)))
    assert len(messages) == 1
    assert "skipped_unchanged after the change: run(s) [9]" in messages[0]
    assert "snaps" in messages[0] and "run 5" in messages[0]


def test_audit_alerts_on_a_tick_that_ran_without_the_analyst():
    changed = {"pfr_advstats": SUCCESS_AT + timedelta(hours=1)}
    messages = _audit(_AuditConn((5, SUCCESS_AT), changed, ([], 11, 0)))
    assert len(messages) == 1 and "tick ran without it (synthesizer run 11)" in messages[0]


def test_audit_quiet_when_the_analyst_ran_after_the_change():
    """It ran (and failed, or is still running): not the gate's or the tick's hole."""
    changed = {"snaps": SUCCESS_AT + timedelta(hours=1)}
    assert _audit(_AuditConn((5, SUCCESS_AT), changed, ([], 11, 1))) == []
    assert _audit(_AuditConn((5, SUCCESS_AT), changed, ([], None, 0))) == []


def test_audit_window_is_the_change_not_the_success():
    """Skips are counted from the earliest change, not from the success."""
    first = SUCCESS_AT + timedelta(hours=1)
    conn = _AuditConn((5, SUCCESS_AT), {"snaps": first + timedelta(hours=2), "ngs": first})
    _audit(conn)
    _, params = next(s for s in conn.statements if s[0].startswith("SELECT (SELECT coalesce"))
    assert params == ("player_efficiency", first, "2026", "4", first, "player_efficiency", first)


def test_summarize_gate_audit_is_one_message():
    messages = summarize_gate_audit(
        "usage", 2026, 4, (5, SUCCESS_AT), {"snaps": SUCCESS_AT + timedelta(1)}, [9, 10], 11
    )
    assert len(messages) == 1
    assert "[9, 10]" in messages[0] and "synthesizer run 11" in messages[0]
