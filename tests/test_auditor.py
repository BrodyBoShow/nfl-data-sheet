from datetime import UTC, datetime, timedelta

from pipeline.orchestration.auditor import (
    FreshnessCheck,
    _clear_alert,
    _send_if_new,
    check_availability_outage,
    check_freshness,
    check_odds_targets,
    summarize_projection_locks,
    summarize_venue_problems,
    summarize_weather_targets,
)


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


# --------------------------------------------------------------------------------------
# check_odds_targets
# --------------------------------------------------------------------------------------


class _FakeTargetsCursor:
    def __init__(self, rows: list[tuple]) -> None:
        self._rows = rows

    def execute(self, query: str, params: tuple = ()) -> None:
        pass

    def fetchall(self) -> list[tuple]:
        return self._rows

    def __enter__(self) -> "_FakeTargetsCursor":
        return self

    def __exit__(self, *exc: object) -> bool:
        return False


class _FakeTargetsConn:
    def __init__(self, rows: list[tuple]) -> None:
        self._rows = rows

    def cursor(self) -> _FakeTargetsCursor:
        return _FakeTargetsCursor(self._rows)


_NOW = datetime(2026, 9, 21, 12, 0, tzinfo=UTC)


def test_no_due_targets_returns_no_alert():
    conn = _FakeTargetsConn([])
    assert check_odds_targets(conn, 2026, 3, _NOW) == []  # type: ignore[arg-type]


def test_all_due_targets_captured_returns_no_alert():
    conn = _FakeTargetsConn(
        [("tue_opener", "captured", None), ("sat_market_movement", "captured", None)]
    )
    assert check_odds_targets(conn, 2026, 3, _NOW) == []  # type: ignore[arg-type]


def test_deadline_passed_uncaptured_target_alerts():
    conn = _FakeTargetsConn(
        [
            ("tue_opener", "captured", None),
            ("sat_market_movement", "missed", "deadline_passed"),
        ]
    )
    alerts = check_odds_targets(conn, 2026, 3, _NOW)  # type: ignore[arg-type]
    assert len(alerts) == 1
    assert "1/2 due targets captured" in alerts[0]
    assert "never captured: sat_market_movement" in alerts[0]


def test_superseded_or_capped_miss_is_reported_separately_from_never_captured():
    conn = _FakeTargetsConn(
        [
            ("tue_opener", "captured", None),
            ("sat_market_movement", "missed", "superseded"),
            ("sun_early", "missed", "weekly_cap"),
        ]
    )
    alerts = check_odds_targets(conn, 2026, 3, _NOW)  # type: ignore[arg-type]
    assert len(alerts) == 1
    assert "1/3 due targets captured" in alerts[0]
    assert "never captured" not in alerts[0]
    assert "missed to catch-up/budget: sat_market_movement, sun_early" in alerts[0]


def test_pending_past_its_own_deadline_counts_as_uncaptured():
    # a target the dispatcher never ticked for since its deadline passed -- still
    # 'pending' in the table, no should_run() ever flipped it to 'missed'
    conn = _FakeTargetsConn([("tue_opener", "pending", None)])
    alerts = check_odds_targets(conn, 2026, 3, _NOW)  # type: ignore[arg-type]
    assert len(alerts) == 1
    assert "0/1 due targets captured" in alerts[0]


# --------------------------------------------------------------------------------------
# check_availability_outage -- reads the most recent availability run's meta for the
# outage_guard entry pipeline/collectors/availability.py records instead of writing
# cleared rows when a source's poll returns well under its known active roster.
# --------------------------------------------------------------------------------------


class _FakeOutageCursor:
    def __init__(self, meta: dict | None) -> None:
        self._meta = meta

    def execute(self, query: str, params: tuple = ()) -> None:
        pass

    def fetchone(self):
        return (self._meta,) if self._meta is not None else None

    def __enter__(self) -> "_FakeOutageCursor":
        return self

    def __exit__(self, *exc: object) -> bool:
        return False


class _FakeOutageConn:
    def __init__(self, meta: dict | None) -> None:
        self._meta = meta

    def cursor(self) -> _FakeOutageCursor:
        return _FakeOutageCursor(self._meta)


def test_no_availability_runs_yet_returns_no_alert():
    conn = _FakeOutageConn(None)
    assert check_availability_outage(conn) == {}  # type: ignore[arg-type]


def test_healthy_run_with_no_outage_guard_returns_no_alert():
    conn = _FakeOutageConn({"first_seen": 3, "changed": 1})
    assert check_availability_outage(conn) == {}  # type: ignore[arg-type]


def test_tripped_outage_guard_alerts_for_that_source():
    conn = _FakeOutageConn({"outage_guard": {"espn": {"active": 800, "seen": 310}}})
    alerts = check_availability_outage(conn)  # type: ignore[arg-type]
    assert set(alerts) == {"espn"}
    assert "310/800" in alerts["espn"]


def test_tripped_outage_guard_alerts_per_source_independently():
    conn = _FakeOutageConn(
        {
            "outage_guard": {
                "espn": {"active": 800, "seen": 310},
                "sleeper": {"active": 50, "seen": 5},
            }
        }
    )
    alerts = check_availability_outage(conn)  # type: ignore[arg-type]
    assert set(alerts) == {"espn", "sleeper"}


# --------------------------------------------------------------------------------------
# alert dedup (_send_if_new / _clear_alert) -- backed by an in-memory auditor_alerts
# --------------------------------------------------------------------------------------


class _FakeAlertCursor:
    def __init__(self, store: dict[str, str]) -> None:
        self._store = store
        self._result: tuple | None = None

    def execute(self, query: str, params: tuple = ()) -> None:
        if query.startswith("SELECT"):
            (alert_key,) = params
            message = self._store.get(alert_key)
            self._result = (message,) if message is not None else None
        elif query.startswith("INSERT"):
            alert_key, message, _now = params
            self._store[alert_key] = message
        elif query.startswith("DELETE"):
            (alert_key,) = params
            self._store.pop(alert_key, None)

    def fetchone(self):
        return self._result

    def __enter__(self) -> "_FakeAlertCursor":
        return self

    def __exit__(self, *exc: object) -> bool:
        return False


class _FakeAlertConn:
    def __init__(self) -> None:
        self.store: dict[str, str] = {}

    def cursor(self) -> _FakeAlertCursor:
        return _FakeAlertCursor(self.store)


def _stub_send_alert(monkeypatch) -> list[str]:
    """Replaces the real send_alert (Discord webhook / print) with a recorder, so these
    dedup tests never depend on -- or fire -- a real DISCORD_WEBHOOK_URL."""
    import pipeline.orchestration.auditor as auditor_mod

    sent: list[str] = []
    monkeypatch.setattr(auditor_mod, "send_alert", sent.append)
    return sent


def test_send_if_new_sends_and_records_a_first_time_alert(monkeypatch):
    sent = _stub_send_alert(monkeypatch)
    conn = _FakeAlertConn()

    result = _send_if_new(conn, "odds:2026:3", "0/1 due targets captured", _NOW)  # type: ignore[arg-type]

    assert result is True
    assert sent == ["0/1 due targets captured"]
    assert conn.store["odds:2026:3"] == "0/1 due targets captured"


def test_send_if_new_suppresses_an_identical_repeat(monkeypatch):
    sent = _stub_send_alert(monkeypatch)
    conn = _FakeAlertConn()
    _send_if_new(conn, "odds:2026:3", "0/1 due targets captured", _NOW)  # type: ignore[arg-type]

    sent_again = _send_if_new(conn, "odds:2026:3", "0/1 due targets captured", _NOW)  # type: ignore[arg-type]

    assert sent_again is False
    assert sent == ["0/1 due targets captured"]  # only the first call actually alerted


def test_send_if_new_resends_when_the_message_changes(monkeypatch):
    _stub_send_alert(monkeypatch)
    conn = _FakeAlertConn()
    _send_if_new(conn, "odds:2026:3", "0/1 due targets captured", _NOW)  # type: ignore[arg-type]

    sent = _send_if_new(conn, "odds:2026:3", "1/2 due targets captured", _NOW)  # type: ignore[arg-type]

    assert sent is True
    assert conn.store["odds:2026:3"] == "1/2 due targets captured"


def test_clear_alert_lets_an_identical_message_resend_later(monkeypatch):
    _stub_send_alert(monkeypatch)
    conn = _FakeAlertConn()
    _send_if_new(conn, "odds:2026:3", "0/1 due targets captured", _NOW)  # type: ignore[arg-type]

    _clear_alert(conn, "odds:2026:3")  # type: ignore[arg-type]
    sent = _send_if_new(conn, "odds:2026:3", "0/1 due targets captured", _NOW)  # type: ignore[arg-type]

    assert sent is True


# --------------------------------------------------------------------------------------
# weather: venue guard + schedule checks
# --------------------------------------------------------------------------------------

_JAX_NAMES = ["EverBank Stadium", "TIAA Bank Stadium"]


def test_venue_all_matching_returns_nothing():
    rows = [("2026_03_NE_JAX", "JAX00", "EverBank Stadium", _JAX_NAMES, "open", "outdoors")]
    assert summarize_venue_problems(rows) == {}


def test_venue_mislabeled_game_alerts():
    rows = [
        ("2026_03_NE_JAX", "JAX00", "EverBank Stadium", _JAX_NAMES, "open", "outdoors"),
        ("2026_05_PHI_JAX", "JAX00", "Tottenham Hotspur Stadium", _JAX_NAMES, "open", "outdoors"),
    ]
    out = summarize_venue_problems(rows)
    assert set(out) == {"venue"}
    assert "2026_05_PHI_JAX" in out["venue"] and "Tottenham Hotspur Stadium" in out["venue"]
    assert "2026_03_NE_JAX" not in out["venue"]


def test_venue_unknown_stadium_id_alerts():
    out = summarize_venue_problems([("2026_20_X_Y", "NEW00", "New Place", None, None, None)])
    assert "stadium_id missing" in out["venue"] and "NEW00" in out["venue"]


def test_open_venue_labeled_dome_is_a_roof_conflict_not_a_venue_problem():
    rows = [("2026_07_PIT_NO", "PAR00", "Stade de France", ["Stade de France"], "open", "dome")]
    out = summarize_venue_problems(rows)
    assert set(out) == {"roof_conflict"} and "PAR00" in out["roof_conflict"]


def test_fixed_roof_dome_label_is_not_a_conflict():
    rows = [("2026_03_NYJ_DET", "DET00", "Ford Field", ["Ford Field"], "fixed", "dome")]
    assert summarize_venue_problems(rows) == {}


# rows: (game_id, captured, missed_or_overdue, deliberate_skips, no_forecast_data, last_deadline)
_PAST = _NOW - timedelta(hours=1)
_FUTURE = _NOW + timedelta(hours=1)


def test_weather_individual_misses_do_not_alert():
    assert summarize_weather_targets([("G", 6, 4, 0, 0, _PAST)], _NOW) == []


def test_weather_game_with_zero_snapshots_alerts_once_its_schedule_is_over():
    assert summarize_weather_targets([("G", 0, 10, 0, 0, _FUTURE)], _NOW) == []
    msgs = summarize_weather_targets([("G", 0, 10, 0, 0, _PAST)], _NOW)
    assert len(msgs) == 1 and "no snapshot captured at all: G" in msgs[0]


def test_weather_deliberately_skipped_game_does_not_alert():
    assert summarize_weather_targets([("DOME", 0, 0, 10, 0, _PAST)], _NOW) == []


def test_weather_no_forecast_data_is_reported():
    msgs = summarize_weather_targets([("G", 9, 0, 0, 1, _FUTURE)], _NOW)
    assert len(msgs) == 1 and "no data" in msgs[0]


# --- projection locks ---------------------------------------------------------------------
# rows: (game_id, kickoff, locked, projection_status | None)


def test_locked_games_do_not_alert():
    assert summarize_projection_locks([("G", _PAST, True, 1)], _NOW) == []


def test_unlocked_game_that_kicked_off_alerts_with_its_card_status():
    msgs = summarize_projection_locks(
        [("A", _PAST, False, 2), ("B", _PAST, False, None), ("C", _PAST, True, 1)], _NOW
    )
    assert len(msgs) == 1
    assert "2 game(s)" in msgs[0]
    assert "A (status 2)" in msgs[0] and "B (status no card)" in msgs[0]
    assert "C" not in msgs[0].split("--")[1]


def test_unlocked_future_game_does_not_alert_yet():
    assert summarize_projection_locks([("G", _FUTURE, False, 1)], _NOW) == []


def test_kickoff_exactly_now_counts_and_older_than_24h_does_not():
    assert summarize_projection_locks([("G", _NOW, False, 1)], _NOW) != []
    old = _NOW - timedelta(hours=24)
    assert summarize_projection_locks([("G", old, False, 1)], _NOW) == []
