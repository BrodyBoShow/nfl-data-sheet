from datetime import UTC, datetime, timedelta

import pytest

from pipeline.analysts import market as mkt
from pipeline.analysts.market import (
    BASIS_LATER_TARGET,
    BASIS_OPENER_LATE,
    BASIS_OPENER_ON_TIME,
    STATUS_AWAITING,
    STATUS_LOOKAHEAD_ONLY,
    STATUS_MISSED,
    STATUS_MOVEMENT,
    STATUS_SINGLE_CAPTURE,
    BookLine,
    Capture,
    CapturedTarget,
    Game,
    american_to_prob,
    build_rows,
    implied_team_totals,
    in_window,
    key_crossings,
    key_straddle,
    novig_home_prob,
    opener_on_time,
    orient,
    select_open,
)

# Week 3, 2026, matching the live calendar: tue_opener opened Tue 09-22 14:00Z (10am ET)
# and fired 16:03Z; week 2's lookahead polls hit 09-18 and 09-21.
KICKOFF = datetime(2026, 9, 27, 17, 0, tzinfo=UTC)  # Sunday 1pm ET
NOW = datetime(2026, 9, 26, 12, 0, tzinfo=UTC)
TUE_OPEN = datetime(2026, 9, 22, 14, 0, tzinfo=UTC)
T_LOOKAHEAD = datetime(2026, 9, 21, 22, 20, tzinfo=UTC)
T_OPENER = datetime(2026, 9, 22, 16, 3, tzinfo=UTC)
T_SAT = datetime(2026, 9, 26, 15, 0, tzinfo=UTC)

GAME = Game("2026_03_KC_MIA", 2026, 3, KICKOFF, "MIA", "KC")

OPENER = CapturedTarget("tue_opener", TUE_OPEN, T_OPENER)
SAT = CapturedTarget("sat_market_movement", datetime(2026, 9, 26, 14, 0, tzinfo=UTC), T_SAT)
TARGETS = {(2026, 3): [OPENER, SAT]}


def _book(
    name: str,
    spread: float | None = 11.5,
    total: float | None = 46.0,
    ml_home: int | None = None,
    ml_away: int | None = None,
) -> BookLine:
    return BookLine(name, spread, total, ml_home, ml_away)


def _capture(
    as_of: datetime,
    spread: float | None = 11.5,
    total: float | None = 46.0,
    books: tuple[BookLine, ...] | None = None,
    home: str = "MIA",
    away: str = "KC",
    spread_range: float | None = 1.0,
    total_range: float | None = 0.5,
) -> Capture:
    if books is None:
        books = tuple(_book(f"b{i}", spread, total) for i in range(3))
    return Capture(
        as_of=as_of,
        home_team=home,
        away_team=away,
        spread_home=spread,
        spread_range=spread_range,
        spread_books=sum(b.spread_home is not None for b in books),
        total=total,
        total_range=total_range,
        total_books=sum(b.total is not None for b in books),
        books=books,
    )


def _rows(captures, now=NOW, targets=TARGETS, game=GAME):
    rows, meta = build_rows(
        games=[game],
        captures={game.game_id: captures},
        targets=targets,
        now=now,
        base_version="schedules@x",
    )
    return rows, meta


def _game_signals(rows) -> dict[str, float]:
    return {r["signal"]: r["value"] for r in rows if r["team"] is None}


def _team_signals(rows) -> dict[tuple[str, str], float]:
    return {(r["team"], r["signal"]): r["value"] for r in rows if r["team"] is not None}


# --- window ------------------------------------------------------------------------


def test_window_matches_environment():
    assert in_window(NOW + timedelta(days=7), NOW)
    assert not in_window(NOW + timedelta(days=7, seconds=1), NOW)
    assert in_window(NOW - timedelta(hours=23), NOW)
    assert not in_window(NOW - timedelta(hours=24), NOW)


# --- open selection and basis ------------------------------------------------------


def test_opener_on_time_uses_wednesday_9am_et_not_the_stored_deadline():
    assert opener_on_time(OPENER)
    # Wed 09-23 12:59Z = 8:59 ET, still inside; 13:00Z = 9:00 ET closes it.
    wed = datetime(2026, 9, 23, 13, 0, tzinfo=UTC)
    assert opener_on_time(CapturedTarget("tue_opener", TUE_OPEN, wed - timedelta(minutes=1)))
    assert not opener_on_time(CapturedTarget("tue_opener", TUE_OPEN, wed))


def test_week2_friday_opener_is_late():
    # Live: week 2's opener window opened Tue 09-15 14:00Z and fired Fri 09-18 23:14Z.
    late = CapturedTarget(
        "tue_opener",
        datetime(2026, 9, 15, 14, 0, tzinfo=UTC),
        datetime(2026, 9, 18, 23, 14, tzinfo=UTC),
    )
    assert not opener_on_time(late)
    cap = _capture(late.captured_at)
    assert select_open([cap], [late]) == (cap, BASIS_OPENER_LATE)


def test_open_is_own_week_opener_not_earlier_lookahead():
    look, opener = _capture(T_LOOKAHEAD, spread=8.5), _capture(T_OPENER)
    assert select_open([look, opener], TARGETS[(2026, 3)]) == (opener, BASIS_OPENER_ON_TIME)


def test_missed_opener_falls_back_to_later_own_week_target():
    sat = _capture(T_SAT)
    assert select_open([_capture(T_LOOKAHEAD), sat], [SAT]) == (sat, BASIS_LATER_TARGET)


def test_opener_captured_but_game_absent_from_that_poll_is_later_target():
    sat = _capture(T_SAT)
    assert select_open([sat], TARGETS[(2026, 3)]) == (sat, BASIS_LATER_TARGET)


def test_no_own_week_capture_means_no_open():
    assert select_open([_capture(T_LOOKAHEAD)], TARGETS[(2026, 3)]) is None


# --- status ------------------------------------------------------------------------


def test_status_single_capture_emits_current_only():
    rows, meta = _rows([_capture(T_LOOKAHEAD, spread=8.5), _capture(T_OPENER)], now=T_OPENER)
    sig = _game_signals(rows)
    assert sig["market_status"] == STATUS_SINGLE_CAPTURE
    assert sig["market_own_week_captures"] == 1
    assert sig["spread_home_current"] == 11.5
    for absent in (
        "spread_home_open",
        "spread_home_move",
        "spread_move_per_day",
        "spread_key_crossings",
        "market_open_basis",
        "spread_book_set_changed",
    ):
        assert absent not in sig
    assert meta["market_status_counts"] == {"2": 1}


def test_status_movement():
    rows, _ = _rows([_capture(T_OPENER, spread=8.5), _capture(T_SAT, spread=11.5)])
    sig = _game_signals(rows)
    assert sig["market_status"] == STATUS_MOVEMENT
    assert sig["market_own_week_captures"] == 2
    assert sig["spread_home_open"] == 8.5
    assert sig["spread_home_move"] == 3.0
    assert sig["market_open_basis"] == BASIS_OPENER_ON_TIME
    assert sig["spread_key_crossings"] == 1  # crossed 10


def test_status_lookahead_only():
    rows, meta = _rows([_capture(T_LOOKAHEAD)], now=T_LOOKAHEAD, targets={})
    sig = _game_signals(rows)
    assert sig["market_status"] == STATUS_LOOKAHEAD_ONLY
    assert sig["market_own_week_captures"] == 0
    assert "spread_home_current" in sig
    assert "spread_home_open" not in sig
    assert meta["lookahead_only_games"] == [GAME.game_id]


def test_status_awaiting_and_missed_emit_status_rows_only():
    rows, _ = _rows([])
    assert _game_signals(rows) == {"market_status": STATUS_AWAITING, "market_own_week_captures": 0}
    assert rows[0]["inputs_version"] == "schedules@x"
    rows, _ = _rows([], now=KICKOFF + timedelta(hours=1))
    assert _game_signals(rows)["market_status"] == STATUS_MISSED


def test_post_kickoff_capture_is_ignored():
    rows, _ = _rows([_capture(T_OPENER), _capture(KICKOFF, spread=3.0)], now=KICKOFF)
    sig = _game_signals(rows)
    assert sig["market_status"] == STATUS_SINGLE_CAPTURE
    assert sig["spread_home_current"] == 11.5


# --- movement, velocity, book-set guard --------------------------------------------


def test_velocity_is_move_over_elapsed_days():
    rows, _ = _rows([_capture(T_OPENER, total=44.0), _capture(T_SAT, total=46.0)])
    sig = _game_signals(rows)
    days = (T_SAT - T_OPENER).total_seconds() / 86400
    assert sig["total_move"] == 2.0
    assert sig["total_move_per_day"] == pytest.approx(2.0 / days)
    assert sig["spread_move_per_day"] == 0.0


def test_book_set_change_is_flagged_with_counts_at_both_ends():
    open_books = tuple(_book(n, spread=10.5) for n in ("dk", "fd", "mgm"))
    cur_books = open_books + tuple(_book(n, spread=12.0) for n in ("bov", "betus", "lowvig"))
    rows, _ = _rows([
        _capture(T_OPENER, spread=10.5, books=open_books),
        _capture(T_SAT, spread=11.25, books=cur_books),
    ])  # fmt: skip
    sig = _game_signals(rows)
    assert sig["spread_home_move"] == 0.75  # every original book stayed at 10.5
    assert sig["spread_book_set_changed"] == 1.0
    assert sig["spread_book_count_open"] == 3
    assert sig["spread_book_count_current"] == 6
    move_row = next(r for r in rows if r["signal"] == "spread_home_move")
    assert move_row["sample_n"] == 3


def test_same_count_different_books_is_still_flagged():
    a = tuple(_book(n) for n in ("dk", "fd"))
    b = tuple(_book(n) for n in ("dk", "mgm"))
    rows, _ = _rows([_capture(T_OPENER, books=a), _capture(T_SAT, books=b)])
    sig = _game_signals(rows)
    assert sig["spread_book_set_changed"] == 1.0
    assert sig["total_book_set_changed"] == 1.0


def test_unchanged_book_set_is_zero():
    rows, _ = _rows([_capture(T_OPENER), _capture(T_SAT)])
    sig = _game_signals(rows)
    assert sig["spread_book_set_changed"] == 0.0
    assert sig["total_book_set_changed"] == 0.0


def test_book_dropping_only_the_total_changes_only_the_total_set():
    a = (_book("dk"), _book("fd"))
    b = (_book("dk"), _book("fd", total=None))
    rows, _ = _rows([_capture(T_OPENER, books=a), _capture(T_SAT, books=b)])
    sig = _game_signals(rows)
    assert sig["spread_book_set_changed"] == 0.0
    assert sig["total_book_set_changed"] == 1.0


# --- key numbers -------------------------------------------------------------------


@pytest.mark.parametrize(
    ("open_", "current", "expected"),
    [
        (8.5, 11.5, 1),  # KC_MIA: through 10
        (2.5, 7.0, 1),  # SEA_WAS: through 3, lands on 7
        (-6.0, -3.0, 0),  # TEN_NYG: lands on -3
        (-3.0, -2.5, 0),  # leaves from a key
        (2.25, 3.25, 1),  # quarter-point medians
        (-1.5, 1.5, 0),  # favorite flip, no key between
        (-3.5, 3.5, 2),  # flip through both -3 and 3
        (2.5, 14.5, 4),  # 3, 7, 10, 14
        (6.5, 6.5, 0),
    ],
)
def test_key_crossings(open_, current, expected):
    assert key_crossings(open_, current) == expected


@pytest.mark.parametrize(
    ("spreads", "expected"),
    [
        ([2.5, 3.0], True),
        ([3.0, 3.5], True),
        ([2.5, 3.5], True),
        ([-3.0, -3.0, -3.0], False),
        ([10.5, 11.0, 11.5], False),
        ([-6.5, -6.5, -7.0], True),
        ([4.0, 5.5, 6.5], False),
    ],
)
def test_key_straddle(spreads, expected):
    assert key_straddle(spreads) is expected


def test_straddle_needs_two_books():
    rows, _ = _rows([_capture(T_OPENER, books=(_book("dk", spread=3.0),))])
    assert "spread_key_straddle" not in _game_signals(rows)


# --- disagreement ------------------------------------------------------------------


def test_null_range_emits_no_row_never_zero():
    rows, _ = _rows([_capture(T_OPENER, spread_range=None, total_range=0.0)])
    sig = _game_signals(rows)
    assert "spread_book_range" not in sig
    assert sig["total_book_range"] == 0.0


def test_range_sample_n_is_book_count():
    rows, _ = _rows([_capture(T_OPENER)])
    row = next(r for r in rows if r["signal"] == "spread_book_range")
    assert row["sample_n"] == 3


# --- implied totals and moneyline ---------------------------------------------------


def test_implied_team_totals():
    assert implied_team_totals(-3.0, 44.0) == (23.5, 20.5)
    assert implied_team_totals(11.5, 46.0) == (17.25, 28.75)


def test_implied_totals_are_team_scoped_and_sum_to_total():
    rows, _ = _rows([_capture(T_OPENER)])
    team = _team_signals(rows)
    assert team[("MIA", "implied_team_total")] == 17.25
    assert team[("KC", "implied_team_total")] == 28.75


def test_american_to_prob():
    assert american_to_prob(-110) == pytest.approx(110 / 210)
    assert american_to_prob(150) == pytest.approx(0.4)
    assert american_to_prob(100) == 0.5


def test_novig_is_proportional_and_skips_books_missing_a_side():
    books = [
        _book("a", ml_home=-110, ml_away=-110),
        _book("b", ml_home=None, ml_away=-110),
        _book("c", ml_home=150, ml_away=-180),
    ]
    p, n = novig_home_prob(books)
    assert n == 2
    c = 0.4 / (0.4 + 180 / 280)
    assert p == pytest.approx((0.5 + c) / 2)
    assert novig_home_prob([_book("x")]) is None


def test_win_probs_sum_to_one_and_no_row_without_moneylines():
    books = (_book("dk", ml_home=575, ml_away=-850), _book("fd", ml_home=540, ml_away=-770))
    rows, _ = _rows([_capture(T_OPENER, books=books)])
    team = _team_signals(rows)
    assert team[("MIA", "win_prob_novig")] + team[("KC", "win_prob_novig")] == pytest.approx(1)
    assert team[("KC", "win_prob_novig")] > 0.8
    rows, _ = _rows([_capture(T_OPENER)])
    assert ("MIA", "win_prob_novig") not in _team_signals(rows)


# --- orientation -------------------------------------------------------------------


def test_swapped_orientation_negates_spreads_and_swaps_moneylines():
    books = (_book("dk", spread=-3.0, ml_home=-150, ml_away=130),)
    swapped = _capture(T_OPENER, spread=-3.0, books=books, home="KC", away="MIA")
    aligned = orient(swapped, GAME)
    assert aligned is not None
    assert (aligned.home_team, aligned.away_team) == ("MIA", "KC")
    assert aligned.spread_home == 3.0
    assert aligned.books[0].spread_home == 3.0
    assert (aligned.books[0].ml_home, aligned.books[0].ml_away) == (130, -150)


def test_mismatched_teams_are_skipped_and_reported():
    rows, meta = _rows([_capture(T_OPENER, home="BUF", away="KC")])
    assert _game_signals(rows)["market_status"] == STATUS_AWAITING
    assert meta["orientation_mismatch"][0]["odds_home"] == "BUF"


# --- row shape ---------------------------------------------------------------------


def test_rows_carry_the_games_own_week_and_odds_version():
    rows, _ = _rows([_capture(T_OPENER), _capture(T_SAT)])
    assert {(r["season"], r["week"], r["sector"]) for r in rows} == {(2026, 3, "market")}
    assert rows[0]["inputs_version"] == (
        f"odds_open@{T_OPENER.isoformat()},odds_current@{T_SAT.isoformat()}"
    )


def test_every_emitted_signal_is_registered():
    books = (
        _book("dk", spread=2.5, ml_home=-120, ml_away=100),
        _book("fd", spread=3.0, ml_home=-125, ml_away=105),
    )
    rows, _ = _rows([_capture(T_OPENER, spread=8.5), _capture(T_SAT, spread=2.75, books=books)])
    emitted = {r["signal"] for r in rows}
    assert emitted <= mkt.MarketAnalyst.signal_names
    assert emitted == mkt.MarketAnalyst.signal_names  # this case exercises every signal


def test_stale_delete_is_scoped_to_this_runs_game_ids(monkeypatch):
    calls = []
    monkeypatch.setattr(
        mkt, "delete_rows", lambda conn, table, where, params: calls.append((where, params)) or 0
    )
    analyst = mkt.MarketAnalyst()
    analyst._game_ids = ["2026_03_KC_MIA"]

    class Ctx:
        conn = None

    analyst._delete_stale_signals(Ctx())  # type: ignore[arg-type]
    where, params = calls[0]
    assert "game_id = ANY" in where
    assert params[0] == "market"
    assert params[2] == ["2026_03_KC_MIA"]


def test_stale_delete_with_no_games_deletes_nothing(monkeypatch):
    monkeypatch.setattr(mkt, "delete_rows", lambda *a: pytest.fail("should not delete"))
    assert mkt.MarketAnalyst()._delete_stale_signals(None) == 0  # type: ignore[arg-type]
