"""P5 grader (pipeline/orchestration/grader.py). The lock under test is built by the
synthesizer's own build_cards, so the card paths the grader reads are the real ones.
Pure tests plus a fake-connection write test -- no DB, no live calls."""

from __future__ import annotations

import datetime as dt
from typing import Any

import numpy as np
import pytest

from pipeline.analysts.market import Capture
from pipeline.core.stats import bootstrap_mean_ci, wilson_ci
from pipeline.orchestration import grader as gr
from pipeline.orchestration.grader import (
    GradeGame,
    build_grades,
    clv,
    grade_game,
    pick_result,
    summarize,
    verdict,
)
from pipeline.synthesis.model import BucketCalibration, ModelFile
from pipeline.synthesis.synthesizer import (
    STATUS_AWAITING_EFFICIENCY,
    STATUS_PROJECTED,
    WindowGame,
    build_cards,
)

UTC = dt.UTC
KICKOFF = dt.datetime(2026, 9, 27, 17, 0, tzinfo=UTC)
LOCKED_AT = KICKOFF - dt.timedelta(hours=2)
NOW = KICKOFF + dt.timedelta(days=1)
GID = "2026_03_KC_BUF"
FP = "fingerprint-ok"


# --- fixtures -----------------------------------------------------------------------------


def _model() -> ModelFile:
    return ModelFile(
        model_version="p5-v1", spec_name="epa_per_play", bases=("epa_per_play",),
        fit_seasons=(2019, 2020, 2021, 2022, 2023, 2024, 2025), efficiency_fingerprint=FP,
        coef={"alpha": 22.0, "beta_off:epa_per_play": 36.0, "beta_def:epa_per_play": 20.0,
              "gamma": 1.5},
        stability_cutpoints=(0.4, 0.7),
        buckets={b: BucketCalibration(13.0, 13.5, False, False) for b in ("low", "mid", "high")},
    )


def _eff(week: int = 3) -> list[dict]:
    values = {"BUF": (0.10, -0.05), "KC": (0.05, 0.02), "MIA": (-0.05, 0.03),
              "NYJ": (-0.10, 0.00)}
    return [
        {"game_id": None, "season": 2026, "week": week, "team": team, "player_id": None,
         "sector": "efficiency", "signal": signal, "value": v, "sample_n": 120,
         "stability": 0.8, "inputs_version": "pbp@x"}
        for team, (off, def_) in values.items()
        for signal, v in (("epa_per_play_off", off), ("epa_per_play_def", def_))
    ]


def _market(spread: float = -2.5, total: float = 47.5, lead: float = 3.0) -> list[dict]:
    rows = [("market_status", 2.0), ("spread_home_current", spread), ("total_current", total),
            ("spread_key_straddle", 0.0), ("spread_book_range", 1.0),
            ("total_book_range", 1.0), ("market_current_lead_hours", lead)]
    return [{"game_id": GID, "season": 2026, "week": 3, "team": None, "player_id": None,
             "sector": "market", "signal": s, "value": v, "sample_n": 8, "stability": None,
             "inputs_version": "odds_current@t"} for s, v in rows]


def _synth(now: dt.datetime, eff: list[dict] | None = None):
    return build_cards(
        games=[WindowGame(GID, 2026, 3, "BUF", "KC", KICKOFF, "Home")],
        efficiency_rows=_eff() if eff is None else eff, market_rows=_market(),
        environment_rows=[], availability_rows=[], model=_model(), live_fingerprint=FP,
        locked={}, now=now,
    )


def _lock() -> dict[str, Any]:
    """The projection_log row the synthesizer would insert at LOCKED_AT, plus its id."""
    result = _synth(LOCKED_AT)
    assert len(result.locks) == 1
    return {"id": 7, **result.locks[0]}


def _game(**overrides: Any) -> GradeGame:
    base: dict[str, Any] = dict(
        game_id=GID, season=2026, week=3, season_type="REG", kickoff=KICKOFF,
        home_team="BUF", away_team="KC", location="Home", div_game=False, home_score=27,
        away_score=20, spread_line=3.0, total_line=48.0,
        updated_at=KICKOFF + dt.timedelta(hours=10),
    )
    base.update(overrides)
    return GradeGame(**base)


def _capture(hours_before: float, spread: float, total: float) -> Capture:
    return Capture(KICKOFF - dt.timedelta(hours=hours_before), "BUF", "KC", spread, 1.0, 8,
                   total, 1.0, 8, ())


_AT_LOCK = _capture(3.0, -2.5, 47.5)
_AFTER_LOCK = _capture(1.0, -3.5, 48.5)


def _grade(game: GradeGame | None = None, *, lock: Any = "default",
           captures: list[Capture] | None = None, card_status: int | None = None,
           card: Any = None) -> dict[str, Any]:
    return grade_game(
        game or _game(), lock=_lock() if lock == "default" else lock,
        card_status=card_status, card=card,
        captures=[_AT_LOCK, _AFTER_LOCK] if captures is None else captures, now=NOW,
    )


# --- pick results and CLV -----------------------------------------------------------------


def test_spread_pick_results():
    # edge < 0: the model's side is home. Home -3, wins by 5 / 3 / 1.
    assert pick_result(-1.0, -3.0, 5.0, "spread") == "win"
    assert pick_result(-1.0, -3.0, 3.0, "spread") == "push"
    assert pick_result(-1.0, -3.0, 1.0, "spread") == "loss"
    # edge > 0: the model's side is away, +3; home wins by 1 -> away covers.
    assert pick_result(1.0, -3.0, 1.0, "spread") == "win"


def test_total_pick_results_and_no_pick():
    assert pick_result(2.0, 44.0, 50.0, "total") == "win"
    assert pick_result(-2.0, 44.0, 50.0, "total") == "loss"
    assert pick_result(0.0, 44.0, 50.0, "total") == "no_pick"
    assert pick_result(2.0, None, 50.0, "total") is None
    assert pick_result(2.0, 44.0, None, "total") is None


def test_clv_sign_convention():
    assert clv(-1.0, -3.0, -4.0) == 1.0   # model likes home, home became a bigger favorite
    assert clv(1.0, -3.0, -4.0) == -1.0   # model likes away, line moved against it
    assert clv(2.0, 44.0, 45.0) == 1.0    # over, total rose
    assert clv(-2.0, 44.0, 45.0) == -1.0  # under, total rose
    assert clv(0.0, 44.0, 45.0) is None
    assert clv(1.0, None, 45.0) is None


# --- grading a lock -----------------------------------------------------------------------


def test_graded_row_from_a_real_synthesizer_lock():
    lock = _lock()
    row = _grade()
    assert row["grade_status"] == "graded"
    assert row["projection_log_id"] == 7
    assert row["efficiency_fingerprint"] == FP
    assert row["lock_line_lead_hours"] == 3.0
    assert row["flag_spread_key_straddle"] is False
    assert row["flag_single_book_market"] is False
    assert row["lock_line_parity"] is True
    # Margin 7, total 47.
    assert row["margin_error"] == pytest.approx(-lock["projected_spread"] - 7)
    assert row["total_error"] == pytest.approx(lock["projected_total"] - 47)
    assert row["in_band_spread"] == (abs(row["margin_error"]) <= 13.0)
    # nflverse spread_line 3.0 is home-positive: -3 in market convention.
    assert row["nflv_close_spread"] == -3.0 and row["nflv_close_total"] == 48.0
    edge = lock["edge_spread"]
    assert row["clv_nflv_spread"] == pytest.approx(np.sign(edge) * (-3.0 - -2.5))
    assert row["own_close_after_lock"] is True
    assert row["own_close_lead_hours"] == pytest.approx(1.0)
    assert row["clv_own_spread"] == pytest.approx(np.sign(edge) * (-3.5 - -2.5))
    assert row["clv_own_total"] == pytest.approx(np.sign(lock["edge_total"]) * (48.5 - 47.5))


def test_clv_own_is_null_when_no_capture_follows_the_lock():
    row = _grade(captures=[_AT_LOCK])
    assert row["own_close_after_lock"] is False
    assert row["own_close_spread"] == -2.5
    assert row["clv_own_spread"] is None and row["clv_own_total"] is None


def test_lock_line_parity_flags_a_mismatch():
    row = _grade(captures=[_capture(3.0, -1.5, 47.5)])
    assert row["lock_line_parity"] is False


def test_capture_at_or_after_kickoff_is_ignored():
    row = _grade(captures=[_AT_LOCK, _capture(0.0, -7.0, 40.0)])
    assert row["own_close_spread"] == -2.5


def test_neutral_site_capture_is_oriented_to_our_home_team():
    flipped = Capture(KICKOFF - dt.timedelta(hours=1), "KC", "BUF", 3.5, 1.0, 8, 48.5, 1.0,
                      8, ())
    row = _grade(captures=[_AT_LOCK, flipped])
    assert row["own_close_spread"] == -3.5


def test_unscored_lock_awaits_result_and_ignores_the_moving_nflverse_line():
    row = _grade(_game(home_score=None, away_score=None))
    assert row["grade_status"] == "awaiting_result"
    assert row["nflv_close_spread"] is None and row["clv_nflv_spread"] is None
    assert row["margin_error"] is None and row["ats_lock"] is None
    assert row["result_first_seen_at"] is None


def test_kickoff_moved_is_flagged():
    row = _grade(_game(kickoff=KICKOFF + dt.timedelta(days=1)))
    assert row["kickoff_moved"] is True


def test_postseason_is_outside_fit_scope():
    assert _grade(_game(season_type="POST"))["outside_fit_scope"] is True


def test_no_lock_row_carries_the_frozen_card_status():
    stale = _synth(KICKOFF - dt.timedelta(days=2), eff=_eff(week=2)).cards[0]
    assert stale["projection_status"] == STATUS_AWAITING_EFFICIENCY
    row = _grade(lock=None, card_status=stale["projection_status"], card=stale["card"])
    assert row["grade_status"] == "no_lock"
    assert row["last_card_status"] == STATUS_AWAITING_EFFICIENCY
    assert row["stability_bucket"] is None
    assert row["projection_log_id"] is None and row["clv_nflv_spread"] is None


def test_no_lock_row_takes_stability_from_a_projected_card():
    card = _synth(KICKOFF - dt.timedelta(days=2)).cards[0]
    assert card["projection_status"] == STATUS_PROJECTED
    row = _grade(lock=None, card_status=STATUS_PROJECTED, card=card["card"])
    assert row["stability_bucket"] == "high"


def test_hash_ignores_the_run_time():
    a = grade_game(_game(), lock=_lock(), card_status=None, card=None, captures=[_AT_LOCK],
                   now=NOW)
    b = grade_game(_game(), lock=_lock(), card_status=None, card=None, captures=[_AT_LOCK],
                   now=NOW + dt.timedelta(hours=5))
    assert a["content_hash"] == b["content_hash"]
    assert a["result_first_seen_at"] != b["result_first_seen_at"]


# --- intervals and verdicts ---------------------------------------------------------------


def test_wilson_worked_example():
    lo, hi = wilson_ci(7, 12)
    assert lo == pytest.approx(0.32, abs=0.01) and hi == pytest.approx(0.81, abs=0.01)
    assert wilson_ci(0, 0) == (None, None)


def test_bootstrap_mean_is_reproducible():
    x = np.arange(20, dtype=float)
    assert bootstrap_mean_ci(x) == bootstrap_mean_ci(x)
    assert bootstrap_mean_ci(np.array([]))["mean"] is None


def test_verdicts():
    kw: dict[str, Any] = dict(null_value=0.0, favorable=1)
    assert verdict(kind="primary", n=12, min_n=50, ci_low=0.1, ci_high=0.2, **kw) == \
        "insufficient_n"
    assert verdict(kind="primary", n=60, min_n=50, ci_low=0.1, ci_high=0.2, **kw) == "supported"
    assert verdict(kind="primary", n=60, min_n=50, ci_low=-0.2, ci_high=-0.1, **kw) == "against"
    assert verdict(kind="primary", n=60, min_n=50, ci_low=-0.1, ci_high=0.1, **kw) == \
        "ci_spans_null"
    assert verdict(kind="primary", n=60, min_n=50, ci_low=-0.2, ci_high=-0.1, null_value=0.0,
                   favorable=-1) == "supported"
    assert verdict(kind="exploratory", n=60, min_n=50, ci_low=0.1, ci_high=0.2, **kw) == \
        "exploratory"


# --- summary ------------------------------------------------------------------------------


def _row(i: int, **overrides: Any) -> dict[str, Any]:
    base = _grade(_game(game_id=f"2026_03_G{i:03d}"))
    base.update(overrides)
    return base


def _metric(summary: list[dict], slice_: str, metric: str) -> dict:
    (row,) = [r for r in summary if r["slice"] == slice_ and r["metric"] == metric]
    return row


def test_a_12_game_58_percent_slice_is_insufficient_n():
    rows = [_row(i, ats_lock="win" if i < 7 else "loss") for i in range(12)]
    m = _metric(summarize(rows, NOW), "all", "ats_lock_win_rate")
    assert m["n"] == 12 and m["estimate"] == pytest.approx(7 / 12)
    assert m["ci_low"] == pytest.approx(0.32, abs=0.01)
    assert m["verdict"] == "insufficient_n"


def test_primary_clv_can_be_supported_but_a_slice_stays_exploratory():
    rows = [_row(i, clv_nflv_spread=0.5 + (i % 5) * 0.25) for i in range(60)]
    summary = summarize(rows, NOW)
    assert _metric(summary, "all", "clv_nflv_spread_mean")["verdict"] == "supported"
    assert _metric(summary, "stability_bucket=high", "clv_nflv_spread_mean")["verdict"] == \
        "exploratory"
    assert _metric(summary, "meta", "tests_reported")["n"] >= 2


def test_lock_rate_by_week_and_bucket_includes_no_lock_games():
    locked = [_row(i) for i in range(3)]
    missed = [_row(10 + i, grade_status="no_lock", stability_bucket=None) for i in range(2)]
    summary = summarize(locked + missed, NOW)
    week = _metric(summary, "season=2026,week=3", "lock_rate")
    assert week["n"] == 5 and week["estimate"] == pytest.approx(0.6)
    assert week["verdict"] == "descriptive"
    unknown = _metric(summary, "stability_bucket=unknown", "lock_rate")
    assert unknown["n"] == 2 and unknown["estimate"] == 0.0


def test_postseason_and_moved_games_are_left_out_of_grades():
    rows = [_row(0), _row(1, outside_fit_scope=True), _row(2, kickoff_moved=True)]
    summary = summarize(rows, NOW)
    assert _metric(summary, "all", "margin_mae_model")["n"] == 1
    assert _metric(summary, "all", "lock_rate")["n"] == 2  # POST excluded, moved kept


def test_build_grades_meta_reports_parity_failures():
    games = [_game()]
    result = build_grades(games, {GID: _lock()}, {}, {GID: [_capture(3.0, -1.5, 47.5)]}, NOW)
    assert result.meta["lock_line_parity_failures"] == [GID]
    assert result.meta["grade_status_counts"] == {"graded": 1}


# --- write --------------------------------------------------------------------------------


class _FakeCursor:
    def __init__(self, log: list[str]) -> None:
        self.log = log

    def __enter__(self) -> _FakeCursor:
        return self

    def __exit__(self, *exc: Any) -> None:
        return None

    def execute(self, sql: str, params: Any = None) -> None:
        self.log.append(sql)

    def executemany(self, sql: str, rows: Any) -> None:
        self.log.append(sql)


class _FakeConn:
    def __init__(self) -> None:
        self.log: list[str] = []

    def cursor(self) -> _FakeCursor:
        return _FakeCursor(self.log)


def test_write_never_touches_projection_log_and_keeps_first_result_time(monkeypatch):
    monkeypatch.setattr(gr, "filter_changed", lambda conn, table, pk, rows: rows)
    result = build_grades([_game()], {GID: _lock()}, {}, {GID: [_AT_LOCK]}, NOW)
    conn = _FakeConn()
    ctx: Any = type("Ctx", (), {"conn": conn})()
    work = gr.ProjectionGrader().write(ctx, result)
    assert work.meta["graded_now"] == [GID]
    assert work.rows_written == 1 + len(result.summary)
    for sql in conn.log:
        assert "projection_log " not in sql and "projection_log(" not in sql
    upsert = next(s for s in conn.log if s.startswith("INSERT INTO projection_grades"))
    assert "COALESCE(projection_grades.result_first_seen_at" in upsert
    assert any(s.startswith("DELETE FROM grade_summary") for s in conn.log)
