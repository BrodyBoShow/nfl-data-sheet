"""The projected-primary rule comparison (docs/phases/P9.md, D5) on hand-built rows."""

from __future__ import annotations

import polars as pl

from scripts.compare_primary_rules import (
    actual_primaries,
    project_l4,
    project_last_game,
    score,
)


def _row(team, week, group, pid, game, l4, side="off", season_type="REG"):
    other = "def" if side == "off" else "off"
    return {
        "season": 2025,
        "season_type": season_type,
        "team": team,
        "week": week,
        "position_group": group,
        "player_id": pid,
        f"{side}_snap_share_game": game,
        f"{side}_snap_share_l4": l4,
        f"{other}_snap_share_game": 0.0,
        f"{other}_snap_share_l4": 0.0,
    }


# QB, team A: q1 is the week-1 primary, q2 takes over from week 2.
_QB = [
    _row("A", 1, "QB", "q1", 0.9, 0.9),
    _row("A", 1, "QB", "q2", 0.1, 0.1),
    _row("A", 2, "QB", "q1", 0.4, 0.65),
    _row("A", 2, "QB", "q2", 0.6, 0.35),
    _row("A", 3, "QB", "q2", 1.0, 0.57),
]


def _frame(rows):
    return pl.DataFrame(rows)


def test_actual_primary_is_the_most_snaps_in_the_game():
    got = actual_primaries(_frame(_QB)).sort("week")
    assert got["player_id"].to_list() == ["q1", "q2", "q2"]


def test_last_game_rule_projects_the_previous_games_primary_and_skips_the_first_game():
    got = project_last_game(_frame(_QB)).sort("week")
    assert got["week"].to_list() == [2, 3]
    assert got["player_id"].to_list() == ["q1", "q2"]


def test_l4_rule_uses_each_players_latest_row_before_the_game():
    got = project_l4(_frame(_QB)).sort("week")
    # Week 3: q1's week-2 l4 (0.65) beats q2's (0.35); q2's week-3 row isn't visible yet.
    assert got["player_id"].to_list() == ["q1", "q1"]


def test_l4_rule_ignores_a_player_whose_latest_row_is_another_team():
    rows = _QB + [
        _row("B", 1, "QB", "x", 1.0, 1.0),
        _row("A", 2, "QB", "x", 0.0, 0.99),  # one A row, then traded to B
        _row("B", 2, "QB", "x", 1.0, 0.99),
        _row("B", 3, "QB", "x", 1.0, 0.99),
    ]
    a = project_l4(_frame(rows)).filter(pl.col("team") == "A").sort("week")
    assert "x" not in a["player_id"].to_list()


def test_dl_uses_defensive_snaps_and_ol_is_left_out():
    rows = [
        _row("A", 1, "DL", "d1", 0.8, 0.8, side="def"),
        _row("A", 1, "DL", "d2", 0.7, 0.7, side="def"),
        _row("A", 1, "OL", "o1", 1.0, 1.0),
    ]
    got = actual_primaries(_frame(rows))
    assert got["player_id"].to_list() == ["d1"]


def test_post_season_is_out_and_both_rules_score_the_same_games():
    rows = _QB + [_row("A", 19, "QB", "q2", 1.0, 0.6, season_type="POST")]
    [r] = score(_frame(rows))
    assert r["n"] == 2
    assert r["last_game"][0] == 0.5  # week 2 miss (q1 vs q2), week 3 hit
    assert r["l4"][0] == 0.0  # q1 both weeks
    assert r["diff_l4_minus_last"][0] == -0.5
