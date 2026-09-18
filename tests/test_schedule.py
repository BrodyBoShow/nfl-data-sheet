from datetime import date

from pipeline.core.schedule import _pick_week

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
