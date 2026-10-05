import json
from contextlib import contextmanager
from typing import Any

import polars as pl
import pytest

import pipeline.core.base as base
import pipeline.core.input_gate as ig
from pipeline.analysts import player_efficiency as eff
from pipeline.analysts import usage
from pipeline.core.base import RunResult
from pipeline.core.db import ChangedUpsert
from pipeline.core.logging import RunStatus
from pipeline.orchestration.dispatcher import (
    _GATED_ANALYSTS,
    _gates,
    _run_tick,
    _set_github_output,
)


class _FakeJob:
    """Duck-types Collector/Analyst's shared `run(season, week) -> RunResult` shape --
    _run_tick only ever calls .run() on what it's given, so a fake doesn't need to
    implement the rest of either ABC (fetch/validate/store, compute/write_signals)."""

    def __init__(self, name: str, rows_written: int, status: RunStatus = "success") -> None:
        self.name = name
        self.rows_written = rows_written
        self.status = status
        self.run_calls = 0

    def run(self, *, season: int, week: int) -> RunResult:
        self.run_calls += 1
        return RunResult(self.name, self.status, self.rows_written, 0.1)


def test_analyst_skipped_when_no_collector_wrote_rows():
    collector = _FakeJob("collector", rows_written=0)
    analyst = _FakeJob("analyst", rows_written=0)

    _run_tick([collector], [analyst], season=2026, week=2)

    assert analyst.run_calls == 0


def test_analyst_runs_when_a_collector_wrote_rows():
    collector = _FakeJob("collector", rows_written=5)
    analyst = _FakeJob("analyst", rows_written=0)

    _run_tick([collector], [analyst], season=2026, week=2)

    assert analyst.run_calls == 1


def test_analyst_runs_if_any_collector_among_several_wrote_rows():
    quiet_collector = _FakeJob("quiet", rows_written=0)
    active_collector = _FakeJob("active", rows_written=41)
    analyst = _FakeJob("analyst", rows_written=0)

    _run_tick([quiet_collector, active_collector], [analyst], season=2026, week=2)

    assert analyst.run_calls == 1


def test_all_collectors_still_run_even_when_analyst_is_skipped():
    collector_a = _FakeJob("a", rows_written=0)
    collector_b = _FakeJob("b", rows_written=0)
    analyst = _FakeJob("analyst", rows_written=0)

    _run_tick([collector_a, collector_b], [analyst], season=2026, week=2)

    assert collector_a.run_calls == 1
    assert collector_b.run_calls == 1


def test_run_tick_returns_every_run_result():
    collector = _FakeJob("collector", rows_written=5)
    analyst = _FakeJob("analyst", rows_written=3)

    results = _run_tick([collector], [analyst], season=2026, week=2)

    assert [r.name for r in results] == ["collector", "analyst"]


def test_run_tick_result_reports_a_collector_failure():
    """A failed collector must be visible in _run_tick's return value -- main() uses
    this (not the auditor) to decide the dispatcher's exit code, per the fix for
    collector/analyst failures silently not failing the GitHub Actions run."""
    collector = _FakeJob("collector", rows_written=0, status="failed")

    results = _run_tick([collector], [], season=2026, week=2)

    assert results[0].status == "failed"


def test_run_tick_result_reports_an_analyst_failure():
    collector = _FakeJob("collector", rows_written=5)
    analyst = _FakeJob("analyst", rows_written=0, status="failed")

    results = _run_tick([collector], [analyst], season=2026, week=2)

    assert any(r.status == "failed" for r in results)


def test_synthesizer_runs_on_a_quiet_tick():
    """Locks are time-triggered, so the synthesizer runs even when no collector wrote
    rows and the analysts were skipped."""
    collector = _FakeJob("collector", rows_written=0)
    analyst = _FakeJob("analyst", rows_written=0)
    synthesizer = _FakeJob("synthesizer", rows_written=0)

    results = _run_tick([collector], [analyst], season=2026, week=2, synthesizers=[synthesizer])

    assert analyst.run_calls == 0
    assert synthesizer.run_calls == 1
    assert [r.name for r in results] == ["collector", "synthesizer"]


def test_synthesizer_runs_after_analysts():
    order: list[str] = []

    class _Ordered(_FakeJob):
        def run(self, *, season: int, week: int) -> RunResult:
            order.append(self.name)
            return super().run(season=season, week=week)

    _run_tick([_Ordered("collector", 3)], [_Ordered("analyst", 0)], season=2026, week=2,
              synthesizers=[_Ordered("synthesizer", 0)])

    assert order == ["collector", "analyst", "synthesizer"]


def test_set_github_output_is_a_noop_without_the_env_var(monkeypatch):
    monkeypatch.delenv("GITHUB_OUTPUT", raising=False)
    _set_github_output("alerted", "true")  # must not raise


def test_set_github_output_appends_name_value_line(tmp_path, monkeypatch):
    output_file = tmp_path / "github_output.txt"
    monkeypatch.setenv("GITHUB_OUTPUT", str(output_file))

    _set_github_output("alerted", "true")

    assert output_file.read_text(encoding="utf-8") == "alerted=true\n"


# --------------------------------------------------------------------------------------
# Gated analysts (_GATED_ANALYSTS): Usage and Player efficiency, the registered instances
# --------------------------------------------------------------------------------------

S = 2099  # the fixture season, as in tests/test_player_efficiency.py


class _RunCursor:
    def __init__(self, conn: "_RunConn") -> None:
        self.conn = conn
        self.rows: list[tuple[Any, ...]] = []

    def execute(self, sql: str, params: tuple[Any, ...] = ()) -> None:
        self.rows = self.conn.respond(sql, params)

    def fetchone(self) -> tuple[Any, ...] | None:
        return self.rows[0] if self.rows else None

    def __enter__(self) -> "_RunCursor":
        return self

    def __exit__(self, *exc: object) -> None:
        pass


class _RunConn:
    """What a gated analyst's run() sends through base._execute, on the content branch:
    agent_runs logging, inputs_ready's count, the gate's digest and last-success lookup
    (none: a first run), and _inputs_version's markers. The tests replace compute's
    reads (`_fetch`) and the player-table write."""

    def __init__(self) -> None:
        self.agents: dict[int, str] = {}
        self.finished: dict[str, tuple[str, dict[str, Any], str | None]] = {}

    def respond(self, sql: str, params: tuple[Any, ...]) -> list[tuple[Any, ...]]:
        if sql.startswith("INSERT INTO agent_runs"):
            run_id = len(self.agents) + 1
            self.agents[run_id] = params[0]
            return [(run_id,)]
        if "UPDATE agent_runs" in sql:
            _, status, _, _, error, meta, run_id = params
            self.finished[self.agents[run_id]] = (status, json.loads(meta), error)
            return []
        if sql.startswith("SELECT count(*) FROM"):
            return [(10,)]
        if sql.startswith("SELECT (SELECT coalesce(md5"):
            return [tuple("d1" for _ in range(sql.count("coalesce(md5")))]
        if sql.startswith("SELECT id, meta->'gate' FROM agent_runs"):
            return []
        if sql.startswith("SELECT last_value FROM source_freshness"):
            return [("m1",)]
        raise AssertionError(f"unexpected statement: {sql[:80]}")

    def cursor(self) -> _RunCursor:
        return _RunCursor(self)

    @contextmanager
    def transaction(self):
        yield

    def commit(self) -> None:
        pass

    def rollback(self) -> None:
        pass


def _connect_to(monkeypatch, conn: _RunConn) -> None:
    @contextmanager
    def connect():
        yield conn

    monkeypatch.setattr(base, "get_connection", connect)
    monkeypatch.setattr(base, "get_settings", lambda: None)


def _registered(name: str):
    return next(a for a in _GATED_ANALYSTS if a.name == name)


def _frame(cols: list[str], rows: list[dict[str, Any]]) -> pl.DataFrame:
    text = {"player_id", "game_id", "team", "season_type", "stat_type", "position_group"}
    schema = {
        c: (pl.Utf8 if c in text else pl.Int64 if c in ("season", "week") else pl.Float64)
        for c in cols
    }
    return pl.DataFrame([{c: r.get(c) for c in cols} for r in rows], schema=schema)


def _defense_inputs() -> eff.EffInputs:
    """Two LB defenders in one game, each with a PFR def row (tackles, a gated source)
    and a player_week row (sacks, ranked). Same shape as
    test_player_efficiency.test_defense_percentiles_are_gated_by_source."""
    base_cols = ["player_id", "season", "week", "season_type"]
    snap_cols = [*base_cols, "game_id", "team", "offense_snaps", "defense_snaps", "st_snaps"]
    snaps = [
        {
            "player_id": p,
            "season": S,
            "week": 1,
            "season_type": "REG",
            "game_id": f"{S}_01_KC_X",
            "team": "KC",
            "offense_snaps": 0,
            "defense_snaps": de,
            "st_snaps": 0,
        }
        for p, de in (("D1", 60), ("D2", 55))
    ]
    pfr = [
        {
            "player_id": p,
            "season": S,
            "week": 1,
            "season_type": "REG",
            "stat_type": "def",
            "def_tackles_combined": t,
        }
        for p, t in (("D1", 6.0), ("D2", 3.0))
    ]
    zeros = dict.fromkeys(eff._PW_DEF_COLS, 0)
    pw = [
        {"player_id": p, "season": S, "week": 1, "season_type": "REG", **zeros, "def_sacks": s}
        for p, s in (("D1", 1.0), ("D2", 0.0))
    ]
    return eff.EffInputs(
        pgp=_frame(eff._PGP_COLS, []),
        snaps=_frame(snap_cols, snaps),
        pfr=_frame([*base_cols, "stat_type", *eff._PFR_COLS], pfr),
        ngs=_frame([*base_cols, "stat_type", *eff._NGS_COLS], []),
        player_week=_frame([*base_cols, *eff._PW_DEF_COLS], pw),
        participation=_frame(["player_id", "season", *eff._PART_COLS], []),
        positions=_frame(
            ["player_id", "position_group"],
            [{"player_id": p, "position_group": "LB"} for p in ("D1", "D2")],
        ),
    )


def test_gated_analysts_are_usage_and_player_efficiency_each_with_its_input_gate():
    """An analyst in _GATED_ANALYSTS without an InputGate would recompute on every tick,
    and _gates would silently leave it out of the auditor's check."""
    assert [a.name for a in _GATED_ANALYSTS] == ["usage", "player_efficiency"]
    assert [g.agent for g in _gates(_GATED_ANALYSTS)] == ["usage", "player_efficiency"]
    assert _registered("usage").input_gate is usage.INPUT_GATE
    assert _registered("player_efficiency").input_gate is eff.INPUT_GATE


def test_gated_analysts_run_on_a_quiet_tick_after_the_analysts_before_synthesizers():
    order: list[str] = []

    class _Ordered(_FakeJob):
        def run(self, *, season: int, week: int) -> RunResult:
            order.append(self.name)
            return super().run(season=season, week=week)

    quiet = _run_tick(
        [_Ordered("collector", 0)],
        [_Ordered("analyst", 0)],
        season=2026,
        week=4,
        gated_analysts=[_Ordered("gated", 0)],
        synthesizers=[_Ordered("synthesizer", 0)],
    )
    assert order == ["collector", "gated", "synthesizer"]  # the collector-write rule skips
    assert [r.name for r in quiet] == order

    order.clear()
    _run_tick(
        [_Ordered("collector", 3)],
        [_Ordered("analyst", 0)],
        season=2026,
        week=4,
        gated_analysts=[_Ordered("gated", 0)],
        synthesizers=[_Ordered("synthesizer", 0)],
    )
    assert order == ["collector", "analyst", "gated", "synthesizer"]


def test_defense_pct_gated_sources_hold_through_a_dispatcher_tick(monkeypatch):
    """The registered Player efficiency, driven by _run_tick on a quiet tick (outside the
    collector-write rule) with its gate on the content branch: the rows it writes keep
    every DEFENSE_PCT_GATED_SOURCES metric's _pct null and rank the player_week ones, and
    the run's meta names the gated sources."""
    monkeypatch.setattr(ig, "GATE_KEY_SOURCE", ig.CONTENT)
    conn = _RunConn()
    _connect_to(monkeypatch, conn)
    monkeypatch.setattr(eff, "_fetch", lambda c, season, week: _defense_inputs())
    written: list[dict[str, Any]] = []

    def write(c, table, season, week, rows):
        written.extend(rows)
        return 0, ChangedUpsert(len(rows), 0, 0)

    monkeypatch.setattr(eff, "write_player_rows", write)

    results = _run_tick(
        [_FakeJob("collector", rows_written=0)],
        [],
        season=S,
        week=1,
        gated_analysts=[_registered("player_efficiency")],
    )

    assert [(r.name, r.status) for r in results] == [
        ("collector", "success"),
        ("player_efficiency", "success"),
    ]
    status, meta, _ = conn.finished["player_efficiency"]
    assert status == "success"
    assert meta["gate"]["decision"] == "run:no_prior_success"
    assert meta["defense_pct_gated_sources"] == sorted(eff.DEFENSE_PCT_GATED_SOURCES)

    d1 = next(r for r in written if r["player_id"] == "D1" and r["week"] == 1)
    defense = [m for m in eff.METRICS if m.family == "defense"]
    gated = [m for m in defense if m.source in eff.DEFENSE_PCT_GATED_SOURCES]
    ranked = [m for m in defense if m.source == "def_pw"]
    assert gated and len(ranked) == 5
    assert all(f"{m.name}_pct" in d1 for m in defense)  # never vacuously absent
    assert d1["tackles_per_snap_game"] == pytest.approx(0.1)  # the gated value is written
    assert all(d1[f"{m.name}_pct"] is None for m in gated)
    assert all(d1[f"{m.name}_pct"] is not None for m in ranked)


@pytest.mark.parametrize("collector_rows", [0, 5])
def test_unselected_gate_fails_each_gated_analyst_and_the_tick_completes(
    monkeypatch, collector_rows
):
    """GATE_KEY_SOURCE unset: a tick reaching either registered analyst logs it failed,
    never skipped and never run, and every later job in the tick still runs."""
    monkeypatch.setattr(ig, "GATE_KEY_SOURCE", None)
    conn = _RunConn()
    _connect_to(monkeypatch, conn)
    for module in (usage, eff):
        monkeypatch.setattr(module, "_fetch", lambda *a: pytest.fail("compute read inputs"))
    analyst = _FakeJob("analyst", rows_written=0)
    synthesizer = _FakeJob("synthesizer", rows_written=0)
    grader = _FakeJob("grader", rows_written=0)

    results = _run_tick(
        [_FakeJob("collector", collector_rows)],
        [analyst],
        season=2026,
        week=4,
        gated_analysts=_GATED_ANALYSTS,
        synthesizers=[synthesizer],
        graders=[grader],
    )

    by_name = {r.name: r for r in results}
    for name in ("usage", "player_efficiency"):
        result = by_name[name]
        assert result.status == "failed", name
        assert result.error is not None and result.error.startswith("GateNotSelected")
        status, _, error = conn.finished[name]
        assert status == "failed" and error is not None and error.startswith("GateNotSelected")
    assert analyst.run_calls == (1 if collector_rows else 0)
    assert synthesizer.run_calls == grader.run_calls == 1
    assert [r.name for r in results][-2:] == ["synthesizer", "grader"]
    # main() exits 1 on any failed job, so the tick isn't green either.
    assert any(r.status == "failed" for r in results)
