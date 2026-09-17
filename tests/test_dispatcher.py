from pipeline.core.base import RunResult
from pipeline.orchestration.dispatcher import _run_tick


class _FakeJob:
    """Duck-types Collector/Analyst's shared `run(season, week) -> RunResult` shape --
    _run_tick only ever calls .run() on what it's given, so a fake doesn't need to
    implement the rest of either ABC (fetch/validate/store, compute/write_signals)."""

    def __init__(self, name: str, rows_written: int) -> None:
        self.name = name
        self.rows_written = rows_written
        self.run_calls = 0

    def run(self, *, season: int, week: int) -> RunResult:
        self.run_calls += 1
        return RunResult(self.name, "success", self.rows_written, 0.1)


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
