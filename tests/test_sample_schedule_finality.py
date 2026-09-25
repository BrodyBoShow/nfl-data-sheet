"""The finality sampler's summary must flag a partial score and pass a clean final.
Synthetic event logs only; the sampler's live half is never called here."""

from __future__ import annotations

from typing import Any

from scripts.sample_schedule_finality import summarize

KICK = "2026-09-27T17:00:00+00:00"


def ev(minutes: float, away: int | None, home: int | None, ot: int | None) -> dict[str, Any]:
    total = None if away is None or home is None else away + home
    result = None if away is None or home is None else home - away
    return {
        "type": "game", "game_id": "2026_03_X_Y", "kickoff": KICK,
        "minutes_since_kickoff": minutes, "away_score": away, "home_score": home,
        "result": result, "total": total, "overtime": ot,
    }


def test_clean_final_is_not_flagged() -> None:
    [g] = summarize([ev(-30, None, None, None), ev(95, None, None, None), ev(205, 17, 24, 0)])
    assert g["first_scored_min"] == 205
    assert g["changes_after_score"] == 0
    assert not g["overtime_null"] and not g["early"]


def test_partial_then_final_is_flagged() -> None:
    [g] = summarize([ev(-30, None, None, None), ev(95, 7, 10, None), ev(205, 17, 24, 0)])
    assert g["first_scored_min"] == 95
    assert g["changes_after_score"] == 1
    assert g["overtime_null"] and g["early"]


def test_score_that_disappears_counts_as_a_change() -> None:
    [g] = summarize([ev(190, 17, 24, 0), ev(200, None, None, None), ev(210, 17, 24, 0)])
    assert g["changes_after_score"] == 2


def test_late_correction_is_flagged_even_when_not_early() -> None:
    [g] = summarize([ev(200, 17, 24, 0), ev(260, 17, 27, 0)])
    assert not g["early"]
    assert g["changes_after_score"] == 1


def test_release_lines_are_ignored() -> None:
    rows = summarize([{"type": "release", "nflverse_ts": "x"}, ev(205, 17, 24, 0)])
    assert len(rows) == 1
