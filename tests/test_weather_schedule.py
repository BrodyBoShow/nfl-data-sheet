from datetime import UTC, datetime, timedelta

from pipeline.collectors.weather_schedule import (
    LEADS,
    MODEL_REGIME_BREAK_TARGETS,
    compute_game_targets,
    decide,
)

# 2026_03_ATL_GB: Thursday 20:15 ET at Lambeau = 2026-09-25T00:15Z
_KICKOFF = datetime(2026, 9, 25, 0, 15, tzinfo=UTC)


def _targets():
    return {t.target_id: t for t in compute_game_targets("2026_03_ATL_GB", _KICKOFF)}


def test_seven_targets_with_two_wide_late_buckets():
    targets = _targets()
    assert list(targets) == ["t48", "t36", "t24", "t18", "t12", "t6", "t2"]
    assert len(LEADS) == 7
    assert targets["t6"].deadline - targets["t6"].scheduled_for == timedelta(hours=4)
    assert targets["t2"].deadline - targets["t2"].scheduled_for == timedelta(hours=3)


def test_each_window_opens_at_its_lead_before_kickoff():
    targets = _targets()
    for target_id, hours in LEADS:
        assert targets[target_id].scheduled_for == _KICKOFF - timedelta(hours=hours)


def test_windows_tile_without_overlap_and_t2_runs_one_hour_past_kickoff():
    ordered = compute_game_targets("g", _KICKOFF)
    for earlier, later in zip(ordered, ordered[1:], strict=False):
        assert earlier.deadline == later.scheduled_for
    assert ordered[-1].deadline == _KICKOFF + timedelta(hours=1)


def test_only_t48_is_flagged_as_a_model_regime_break():
    assert MODEL_REGIME_BREAK_TARGETS == {"t48"}


def test_decide_fires_the_open_window_and_ignores_unopened_ones():
    targets = _targets()
    now = _KICKOFF - timedelta(hours=20)  # inside t24's window [T-24, T-18)
    decision = decide(list(targets.values()), now)
    assert [t.target_id for t in decision.due] == ["t24"]
    # t48/t36 are past their deadlines; t18 onward haven't opened
    assert sorted(t.target_id for t, _ in decision.missed) == ["t36", "t48"]
    assert all(reason == "deadline_passed" for _, reason in decision.missed)


def test_decide_never_fires_a_closed_window_late():
    targets = _targets()
    # a tick lands 1 minute after t6's window closed (i.e. inside t2's)
    now = _KICKOFF - timedelta(hours=2) + timedelta(minutes=1)
    decision = decide([targets["t6"], targets["t2"]], now)
    assert [t.target_id for t in decision.due] == ["t2"]
    assert [t.target_id for t, _ in decision.missed] == ["t6"]


def test_any_tick_from_t6_open_to_one_hour_past_kickoff_has_a_due_target():
    # Ticks land 4.5-5.5h apart, so the late span must have no gaps: whenever a tick
    # lands between T-6h and kickoff+1h, exactly one late target is due.
    targets = list(_targets().values())
    for minutes in range(-6 * 60, 60, 15):
        now = _KICKOFF + timedelta(minutes=minutes)
        assert len(decide(targets, now).due) == 1, now


def test_window_boundaries_are_open_at_start_and_closed_at_deadline():
    t6 = _targets()["t6"]
    assert [t.target_id for t in decide([t6], t6.scheduled_for).due] == ["t6"]
    at_deadline = decide([t6], t6.deadline)
    assert at_deadline.due == [] and [t.target_id for t, _ in at_deadline.missed] == ["t6"]


def test_decide_across_games_fires_one_target_per_game():
    a = compute_game_targets("A", _KICKOFF)
    b = compute_game_targets("B", _KICKOFF + timedelta(hours=3))
    now = _KICKOFF - timedelta(hours=5)  # A at T-5 -> t6 window; B at T-8 -> t12 window
    decision = decide(a + b, now)
    due = sorted((t.game_id, t.target_id) for t in decision.due)
    assert due == [("A", "t6"), ("B", "t12")]
