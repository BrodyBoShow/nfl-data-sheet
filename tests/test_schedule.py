from datetime import UTC, date, datetime

from pipeline.core.schedule import _pick_week, to_gameday

# Three synthetic weeks: week 1 games Thu 9/4 - Mon 9/8, week 2 Thu 9/11 - Mon 9/15,
# week 3 Thu 9/18 - Mon 9/22 (2025 season, arbitrary but internally consistent).
_GAMES = [
    (date(2025, 9, 4), 2025, 1, "REG"),
    (date(2025, 9, 7), 2025, 1, "REG"),
    (date(2025, 9, 8), 2025, 1, "REG"),
    (date(2025, 9, 11), 2025, 2, "REG"),
    (date(2025, 9, 14), 2025, 2, "REG"),
    (date(2025, 9, 15), 2025, 2, "REG"),
    (date(2025, 9, 18), 2025, 3, "REG"),
    (date(2025, 9, 21), 2025, 3, "REG"),
    (date(2025, 9, 22), 2025, 3, "REG"),
]

# Real 2026 week 2/3 gamedays (verified live 2026-09-18/2026-09-21 via Supabase) --
# used to reproduce the actual timezone bug, not a synthetic stand-in.
_2026_PRIME_TIME_GAMES = [
    (date(2026, 9, 17), 2026, 2, "REG"),  # Thu DET@BUF
    (date(2026, 9, 20), 2026, 2, "REG"),  # Sun slate
    (date(2026, 9, 21), 2026, 2, "REG"),  # Mon MNF: LA@NYG
    (date(2026, 9, 24), 2026, 3, "REG"),  # Thu GB@ATL
    (date(2026, 9, 27), 2026, 3, "REG"),  # Sun slate
    (date(2026, 9, 28), 2026, 3, "REG"),  # Mon PHI@CHI
]


def test_wednesday_before_next_thursday_resolves_to_upcoming_week():
    # Wed 9/17 is one day before week 3's Thursday opener -- inside the 2-day lookahead.
    assert _pick_week(_GAMES, date(2025, 9, 17)) == (2025, 3, "REG")


def test_timestamp_inside_prior_weeks_window_resolves_to_that_week():
    # Sat 9/13 is still within week 2's Tue-Mon window (more than 2 days before week 3).
    assert _pick_week(_GAMES, date(2025, 9, 13)) == (2025, 2, "REG")


def test_end_of_season_falls_back_to_most_recent_past_week():
    # Well past the last scheduled game -- no upcoming week, falls back to week 3.
    assert _pick_week(_GAMES, date(2025, 10, 1)) == (2025, 3, "REG")


def test_empty_games_returns_none():
    assert _pick_week([], date(2025, 9, 17)) is None


# --------------------------------------------------------------------------------------
# to_gameday -- ET vs. raw UTC calendar date (the bug: .date() on a UTC datetime doesn't
# match games.gameday's ET convention for any kickoff at/after ~8pm ET)
# --------------------------------------------------------------------------------------


def test_to_gameday_converts_a_prime_time_utc_timestamp_to_its_et_calendar_date():
    # 2026 week 2 MNF (LA@NYG): kickoff 2026-09-21T20:15 ET == 2026-09-22T00:15Z
    # (verified live). The raw UTC date reads 2026-09-22 -- one day past the actual
    # ET gameday.
    commence_time = datetime(2026, 9, 22, 0, 15, tzinfo=UTC)
    assert commence_time.date() == date(2026, 9, 22)  # the bug, if taken raw
    assert to_gameday(commence_time) == date(2026, 9, 21)  # the fix


def test_to_gameday_passes_plain_dates_through_unchanged():
    assert to_gameday(date(2026, 9, 21)) == date(2026, 9, 21)


def test_mnf_commence_time_resolves_to_its_own_week_not_the_next():
    # Before the fix: _pick_week(_2026_PRIME_TIME_GAMES, date(2026, 9, 22)) picked week 3
    # -- week 2's window is [2026-09-15, 2026-09-21] (2 days before its Thursday opener
    # through its Monday closer), so the raw UTC date fell just outside it and into week
    # 3's window [2026-09-22, 2026-09-28] instead. This is the exact scenario that
    # mislabeled a live odds_snapshots row as week 3.
    commence_time = datetime(2026, 9, 22, 0, 15, tzinfo=UTC)
    assert _pick_week(_2026_PRIME_TIME_GAMES, commence_time.date()) == (2026, 3, "REG")  # bug
    assert _pick_week(_2026_PRIME_TIME_GAMES, to_gameday(commence_time)) == (2026, 2, "REG")


def test_thursday_night_commence_time_resolves_correctly():
    # GB@ATL week 3 TNF: kickoff 2026-09-24T20:15 ET == 2026-09-25T00:15Z.
    commence_time = datetime(2026, 9, 25, 0, 15, tzinfo=UTC)
    assert _pick_week(_2026_PRIME_TIME_GAMES, to_gameday(commence_time)) == (2026, 3, "REG")
