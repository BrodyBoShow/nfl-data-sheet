from pipeline.core.base import RunResult
from pipeline.core.logging import RunStatus
from pipeline.orchestration.dispatcher import _run_tick, _set_github_output


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


def test_set_github_output_is_a_noop_without_the_env_var(monkeypatch):
    monkeypatch.delenv("GITHUB_OUTPUT", raising=False)
    _set_github_output("alerted", "true")  # must not raise


def test_set_github_output_appends_name_value_line(tmp_path, monkeypatch):
    output_file = tmp_path / "github_output.txt"
    monkeypatch.setenv("GITHUB_OUTPUT", str(output_file))

    _set_github_output("alerted", "true")

    assert output_file.read_text(encoding="utf-8") == "alerted=true\n"
