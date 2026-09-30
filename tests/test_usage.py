"""Usage and role analyst (pipeline/analysts/usage.py): shares, team denominators,
windows, and its guards. Synthetic frames only, no live calls."""

import polars as pl
import pytest

from pipeline.analysts import usage
from pipeline.core.player_tables import UNHASHED_COLS, finalize_rows


def _snap(player, week, team="KC", off=0, off_pct=0.0, de=0, de_pct=0.0, st=0, st_pct=0.0):
    return {
        "player_id": player,
        "game_id": f"2099_{week:02d}_{team}_X",
        "season": 2099,
        "week": week,
        "season_type": "REG",
        "team": team,
        "offense_snaps": off,
        "offense_pct": off_pct,
        "defense_snaps": de,
        "defense_pct": de_pct,
        "st_snaps": st,
        "st_pct": st_pct,
    }


def _pgp(player, week, team="KC", **counts):
    row = {"player_id": player, "game_id": f"2099_{week:02d}_{team}_X", "week": week, "team": team}
    row.update({c: counts.get(c) for c in usage._PGP_COUNTS})
    return row


def _frames(snaps, pgp, positions=None):
    s = pl.DataFrame(snaps, schema=usage._SNAPS_SCHEMA)
    p = pl.DataFrame(pgp, schema=usage._PGP_SCHEMA)
    pos = pl.DataFrame(
        positions or [{"player_id": r["player_id"], "position_group": "WR"} for r in snaps],
        schema={"player_id": pl.Utf8, "position_group": pl.Utf8},
    )
    return s, p, pos.unique()


def _row(df, player, week):
    return df.filter((pl.col("player_id") == player) & (pl.col("week") == week)).row(0, named=True)


def _two_receivers():
    snaps = [
        _snap("A", 1, off=60, off_pct=1.0),
        _snap("B", 1, off=30, off_pct=0.5),
        _snap("A", 2, off=50, off_pct=1.0),
        _snap("B", 2, off=40, off_pct=0.8),
    ]
    pgp = [
        _pgp("A", 1, targets=6, rec_air_yards_sum=60.0),
        _pgp("B", 1, targets=4, rec_air_yards_sum=-5.0),
        _pgp("A", 2, targets=2, rec_air_yards_sum=10.0),
        _pgp("B", 2, targets=8, rec_air_yards_sum=90.0),
    ]
    return _frames(snaps, pgp)


def test_shares_per_game_and_season_to_date():
    df = usage.build_usage_rows(*_two_receivers())
    a1, a2 = _row(df, "A", 1), _row(df, "A", 2)
    assert a1["target_share_game"] == pytest.approx(0.6)
    assert a2["target_share_game"] == pytest.approx(0.2)
    # _std sums player and team over the player's own games: (6 + 2) / (10 + 10).
    assert a2["target_share_std"] == pytest.approx(0.4)
    assert a2["target_share_wow"] == pytest.approx(-0.4)
    assert a1["target_share_wow"] is None  # first game of the season
    assert a2["targets_per_off_snap_std"] == pytest.approx(8 / 110)
    # Negative air yards count in the team denominator: 60 / (60 - 5).
    assert a1["air_yards_share_game"] == pytest.approx(60 / 55)
    assert a2["off_snap_share_game"] == pytest.approx(1.0)
    assert (a2["usage_games_std"], a2["usage_games_l4"]) == (2, 2)
    assert a2["usage_stability"] == pytest.approx(2 / (2 + usage.K_USAGE))


def test_team_snaps_recovered_from_pfr_pct_median():
    snaps = pl.DataFrame(
        [
            _snap("A", 1, off=64, off_pct=1.0),
            _snap("B", 1, off=45, off_pct=0.70),
            _snap("C", 1, off=20, off_pct=0.31),  # below 50%: not used for recovery
            _snap("D", 1, de=30, de_pct=0.40),  # nobody on defense reaches 50%
        ],
        schema=usage._SNAPS_SCHEMA,
    )
    t = usage.team_snaps(snaps).row(0, named=True)
    assert t["team_off_snaps"] == 64  # median(64/1.0, 45/0.70 = 64.3) = 64.1, rounded
    assert t["team_def_snaps"] is None


def test_snap_without_a_pgp_row_is_a_sourced_zero_not_a_gap():
    snaps = [_snap("A", 1, off=60, off_pct=1.0), _snap("B", 1, off=20, off_pct=0.33)]
    df = usage.build_usage_rows(*_frames(snaps, [_pgp("A", 1, targets=5, carries=0)]))
    b = _row(df, "B", 1)
    assert b["target_share_game"] == 0.0  # the team had targets; B had none
    assert b["carry_share_game"] is None  # the team had no carries: no denominator


def test_player_with_no_snap_gets_no_row():
    snaps = [_snap("A", 1, off=60, off_pct=1.0), _snap("Z", 1)]
    df = usage.build_usage_rows(*_frames(snaps, []))
    assert df["player_id"].to_list() == ["A"]


def test_pct_scale_guard():
    s, p, pos = _two_receivers()
    with pytest.raises(ValueError, match="0-1 fraction"):
        usage.build_usage_rows(s.with_columns(pl.col("offense_pct") * 100), p, pos)


def test_two_games_in_one_week_guard():
    snaps = [_snap("A", 1, off=60, off_pct=1.0), _snap("A", 1, team="NE", off=10, off_pct=0.2)]
    with pytest.raises(ValueError, match="more than one row"):
        usage.build_usage_rows(*_frames(snaps, []))


def test_later_week_never_changes_an_earlier_row():
    s, p, pos = _two_receivers()
    before = usage.build_usage_rows(
        s.filter(pl.col("week") == 1), p.filter(pl.col("week") == 1), pos
    )
    after = usage.build_usage_rows(s, p, pos).filter(pl.col("week") == 1)
    cols = [c for c in before.columns if c.endswith(("_std", "_game", "_l4", "_pct"))]
    assert before.sort("player_id").select(cols).equals(after.sort("player_id").select(cols))


def test_rows_carry_exactly_the_migration_columns():
    df = usage.build_usage_rows(*_two_receivers())
    rows = finalize_rows(
        df, usage.COLUMNS, {"as_of": None, "inputs_version": "x", "updated_at": None}
    )
    assert set(rows[0]) == set(usage.COLUMNS)
    produced = {c for c in usage.COLUMNS if c not in UNHASHED_COLS}
    assert produced <= set(df.columns)
