from datetime import UTC, date, datetime, timedelta

from pipeline.collectors.odds_schedule import (
    MONTHLY_CREDIT_CEILING,
    WEEKLY_CREDIT_CEILING,
    Target,
    TargetState,
    compute_week_targets,
    decide,
)

# A normal week: Thursday opener, a split Sunday slate (one early, one late kickoff),
# and a Monday closer -- covers every branch of compute_week_targets.
_FULL_WEEK_GAMES = [
    (date(2026, 9, 17), "Thursday", "20:15"),
    (date(2026, 9, 20), "Sunday", "13:00"),
    (date(2026, 9, 20), "Sunday", "16:25"),
    (date(2026, 9, 21), "Monday", "20:15"),
]

# --------------------------------------------------------------------------------------
# compute_week_targets
# --------------------------------------------------------------------------------------


def test_full_week_produces_all_six_targets():
    targets = compute_week_targets(_FULL_WEEK_GAMES)
    ids = {t.target_id for t in targets}
    assert ids == {
        "tue_opener",
        "sat_market_movement",
        "sun_early",
        "sun_late",
        "thu_pre_tnf",
        "mon_pre_mnf",
    }


def test_every_target_scheduled_before_its_own_deadline():
    for target in compute_week_targets(_FULL_WEEK_GAMES):
        assert target.scheduled_for < target.deadline, target.target_id


def test_pre_kickoff_deadlines_match_real_game_times():
    targets = {t.target_id: t for t in compute_week_targets(_FULL_WEEK_GAMES)}
    assert targets["thu_pre_tnf"].deadline == datetime(2026, 9, 18, 0, 15, tzinfo=UTC)
    assert targets["mon_pre_mnf"].deadline == datetime(2026, 9, 22, 0, 15, tzinfo=UTC)
    # 13:00 ET (EDT, UTC-4) on 2026-09-20 == 17:00 UTC
    assert targets["sun_early"].deadline == datetime(2026, 9, 20, 17, 0, tzinfo=UTC)
    # 16:25 ET == 20:25 UTC
    assert targets["sun_late"].deadline == datetime(2026, 9, 20, 20, 25, tzinfo=UTC)


def test_tue_opener_closes_wednesday_morning_not_saturday():
    targets = {t.target_id: t for t in compute_week_targets(_FULL_WEEK_GAMES)}
    # tuesday = 2026-09-15, so "Wednesday 9:00 ET" is 2026-09-16 13:00 UTC (EDT, UTC-4)
    assert targets["tue_opener"].deadline == datetime(2026, 9, 16, 13, 0, tzinfo=UTC)


def test_pre_kickoff_windows_open_four_hours_before_kickoff():
    targets = {t.target_id: t for t in compute_week_targets(_FULL_WEEK_GAMES)}
    for target_id in ("thu_pre_tnf", "sun_late", "mon_pre_mnf"):
        target = targets[target_id]
        assert target.deadline - target.scheduled_for == timedelta(hours=4), target_id


def test_no_thursday_or_monday_game_omits_those_targets():
    sunday_only = [g for g in _FULL_WEEK_GAMES if g[1] == "Sunday"]
    ids = {t.target_id for t in compute_week_targets(sunday_only)}
    assert "thu_pre_tnf" not in ids
    assert "mon_pre_mnf" not in ids
    assert ids == {"tue_opener", "sat_market_movement", "sun_early", "sun_late"}


def test_all_early_sunday_kickoffs_omits_sun_late():
    early_only = [
        (date(2026, 9, 20), "Sunday", "13:00"),
        (date(2026, 9, 20), "Sunday", "13:00"),
    ]
    ids = {t.target_id for t in compute_week_targets(early_only)}
    assert "sun_late" not in ids


def test_no_sunday_game_returns_no_targets():
    assert compute_week_targets([(date(2026, 9, 17), "Thursday", "20:15")]) == []


def test_no_games_returns_no_targets():
    assert compute_week_targets([]) == []


# --------------------------------------------------------------------------------------
# decide
# --------------------------------------------------------------------------------------

_TARGET = Target(
    "sun_early",
    scheduled_for=datetime(2026, 9, 20, 13, 0, tzinfo=UTC),
    deadline=datetime(2026, 9, 20, 17, 0, tzinfo=UTC),
)


def test_fires_when_window_is_open_and_pending():
    decision = decide(
        [_TARGET],
        states={},
        now=datetime(2026, 9, 20, 14, 0, tzinfo=UTC),
        credits_spent_this_week=0,
        credits_spent_this_month=0,
    )
    assert decision.fire == _TARGET
    assert decision.missed == []


def test_does_not_fire_before_window_opens():
    decision = decide(
        [_TARGET],
        states={},
        now=datetime(2026, 9, 20, 12, 0, tzinfo=UTC),
        credits_spent_this_week=0,
        credits_spent_this_month=0,
    )
    assert decision.fire is None
    assert decision.missed == []


def test_already_captured_target_is_neither_fired_nor_missed():
    decision = decide(
        [_TARGET],
        states={"sun_early": TargetState("sun_early", "captured", 3)},
        now=datetime(2026, 9, 20, 14, 0, tzinfo=UTC),
        credits_spent_this_week=3,
        credits_spent_this_month=3,
    )
    assert decision.fire is None
    assert decision.missed == []


def test_deadline_passed_without_capture_is_reported_missed():
    decision = decide(
        [_TARGET],
        states={},
        now=datetime(2026, 9, 20, 18, 0, tzinfo=UTC),
        credits_spent_this_week=0,
        credits_spent_this_month=0,
    )
    assert decision.fire is None
    assert decision.missed == [("sun_early", "deadline_passed")]


def test_catch_up_collapsing_fires_only_the_most_recent_open_target():
    earlier = Target(
        "tue_opener",
        scheduled_for=datetime(2026, 9, 15, 14, 0, tzinfo=UTC),
        deadline=datetime(2026, 9, 20, 12, 0, tzinfo=UTC),
    )
    later = Target(
        "sat_market_movement",
        scheduled_for=datetime(2026, 9, 19, 14, 0, tzinfo=UTC),
        deadline=datetime(2026, 9, 20, 12, 0, tzinfo=UTC),
    )
    decision = decide(
        [earlier, later],
        states={},
        now=datetime(2026, 9, 19, 15, 0, tzinfo=UTC),
        credits_spent_this_week=0,
        credits_spent_this_month=0,
    )
    assert decision.fire == later
    assert decision.missed == [("tue_opener", "superseded")]


def test_weekly_cap_blocks_fire_even_with_open_window():
    decision = decide(
        [_TARGET],
        states={},
        now=datetime(2026, 9, 20, 14, 0, tzinfo=UTC),
        credits_spent_this_week=WEEKLY_CREDIT_CEILING - 1,
        credits_spent_this_month=0,
    )
    assert decision.fire is None
    assert decision.missed == [("sun_early", "weekly_cap")]


def test_monthly_cap_blocks_fire_even_under_weekly_cap():
    decision = decide(
        [_TARGET],
        states={},
        now=datetime(2026, 9, 20, 14, 0, tzinfo=UTC),
        credits_spent_this_week=0,
        credits_spent_this_month=MONTHLY_CREDIT_CEILING - 1,
    )
    assert decision.fire is None
    assert decision.missed == [("sun_early", "monthly_cap")]
