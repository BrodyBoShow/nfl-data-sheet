from datetime import UTC, datetime, timedelta

from pipeline.orchestration.auditor import FreshnessCheck, check_freshness


class _FakeCursor:
    def __init__(self, conn: "_FakeConn") -> None:
        self._conn = conn
        self._last_params: tuple = ()

    def execute(self, query: str, params: tuple = ()) -> None:
        self._last_params = params

    def fetchone(self):
        agent = self._last_params[0]
        value = self._conn.last_success_by_agent.get(agent)
        return (value,) if value is not None else None

    def __enter__(self) -> "_FakeCursor":
        return self

    def __exit__(self, *exc: object) -> bool:
        return False


class _FakeConn:
    def __init__(self, last_success_by_agent: dict[str, datetime]) -> None:
        self.last_success_by_agent = last_success_by_agent

    def cursor(self) -> _FakeCursor:
        return _FakeCursor(self)


def test_check_freshness_flags_a_deliberately_stale_table():
    now = datetime(2026, 9, 16, tzinfo=UTC)
    conn = _FakeConn(
        {
            "stale_agent": now - timedelta(days=10),  # T2 max age is 2 days
            "fresh_agent": now - timedelta(hours=1),
        }
    )

    results = check_freshness(
        conn,  # type: ignore[arg-type]
        [
            FreshnessCheck(agent="stale_agent", tier="T2"),
            FreshnessCheck(agent="fresh_agent", tier="T2"),
        ],
        now=now,
    )

    by_agent = {r.agent: r.status for r in results}
    assert by_agent["stale_agent"] == "stale"
    assert by_agent["fresh_agent"] == "fresh"


def test_check_freshness_flags_never_run():
    conn = _FakeConn({})
    results = check_freshness(
        conn,  # type: ignore[arg-type]
        [FreshnessCheck(agent="ghost_agent", tier="T1")],
    )
    assert results[0].status == "never_run"
