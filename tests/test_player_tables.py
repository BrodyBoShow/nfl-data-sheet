"""Shared player-table helpers (pipeline/core/player_tables.py): windows, blend,
percentiles, hashing, the scoped stale delete, and the migration column parser."""

import math
import random
from datetime import UTC, datetime
from pathlib import Path

import polars as pl
import pytest

from pipeline.analysts.efficiency import _blend
from pipeline.core.player_tables import (
    as_of_percentiles,
    blend_exprs,
    check_one_game_per_week,
    column_list,
    delete_stale_player_rows,
    finalize_rows,
    window_frame,
    windowed_sums,
)

_MIGRATIONS = Path(__file__).resolve().parents[1] / "db" / "migrations"


def _played(weeks: dict[str, list[int]]) -> pl.DataFrame:
    return pl.DataFrame([{"player_id": p, "week": w} for p, ws in weeks.items() for w in ws])


def _values(rows: list[tuple[str, int, float]]) -> pl.DataFrame:
    return pl.DataFrame(rows, schema=["player_id", "week", "x"], orient="row")


# --- windows ---------------------------------------------------------------------------


def test_window_frame_counts_games_played_and_starts_l4_three_games_back():
    w = window_frame(_played({"A": [1, 2, 4, 5, 7]})).sort("week")
    assert w["games_std"].to_list() == [1, 2, 3, 4, 5]
    assert w["games_l4"].to_list() == [1, 2, 3, 4, 4]
    # Week 7's last four games played are 2, 4, 5, 7 -- the bye in week 3 doesn't count.
    assert w["l4_from_week"].to_list() == [1, 1, 1, 1, 2]


def test_windowed_sums_game_l4_std():
    windows = window_frame(_played({"A": [1, 2, 4, 5, 7]}))
    values = _values([("A", 1, 1.0), ("A", 2, 2.0), ("A", 4, 4.0), ("A", 5, 5.0), ("A", 7, 7.0)])
    s = windowed_sums(values, windows, ["x"]).sort("week")
    assert s["x_game"].to_list() == [1, 2, 4, 5, 7]
    assert s["x_std"].to_list() == [1, 3, 7, 12, 19]
    assert s["x_l4"].to_list() == [1, 3, 7, 12, 18]  # week 7: 2 + 4 + 5 + 7


def test_windowed_sums_never_reads_a_later_week():
    windows = window_frame(_played({"A": [1, 2, 3], "B": [1, 2, 3]}))
    base = _values([("A", 1, 1.0), ("A", 2, 1.0), ("B", 1, 2.0), ("B", 3, 5.0)])
    before = windowed_sums(base, windows.filter(pl.col("week") <= 2), ["x"]).sort(
        "player_id", "week"
    )
    later = pl.concat([base, _values([("A", 3, 100.0), ("B", 3, 100.0)])])
    after = (
        windowed_sums(later, windows, ["x"]).filter(pl.col("week") <= 2).sort("player_id", "week")
    )
    assert before.equals(after)


def test_l4_window_is_games_played_not_games_with_a_value():
    # A played weeks 1-5 but only has values in weeks 1 and 5. Week 5's last four games
    # played are 2-5, so week 1's value is outside the window.
    windows = window_frame(_played({"A": [1, 2, 3, 4, 5]}))
    s = windowed_sums(_values([("A", 1, 10.0), ("A", 5, 1.0)]), windows, ["x"])
    wk5 = s.filter(pl.col("week") == 5).row(0, named=True)
    assert (wk5["x_l4"], wk5["x_std"]) == (1.0, 11.0)


def test_windowed_sums_keeps_players_apart():
    windows = window_frame(_played({"A": [1, 2], "B": [1, 2]}))
    s = windowed_sums(
        _values([("A", 1, 1.0), ("A", 2, 2.0), ("B", 1, 10.0), ("B", 2, 20.0)]), windows, ["x"]
    )
    got = {(r["player_id"], r["week"]): r["x_std"] for r in s.to_dicts()}
    assert got == {("A", 1): 1, ("A", 2): 3, ("B", 1): 10, ("B", 2): 30}


def test_one_game_per_week_guard():
    check_one_game_per_week(_played({"A": [1, 2]}), "t")
    with pytest.raises(ValueError, match="more than one row"):
        check_one_game_per_week(_played({"A": [1, 1]}), "t")


# --- blend -----------------------------------------------------------------------------


def test_blend_exprs_matches_efficiencys_blend():
    rng = random.Random(7)
    rows = []
    for _ in range(200):
        prior = None if rng.random() < 0.3 else rng.uniform(-1, 1)
        rows.append(
            {
                "cur": rng.uniform(-1, 1),
                "n": float(rng.randint(1, 400)),
                "prior": prior,
                "league": rng.uniform(-0.2, 0.2),
                "k": float(rng.randint(5, 900)),
                "r": rng.random(),
            }
        )
    df = pl.DataFrame(rows)
    value, w_cur, w_prior = blend_exprs(
        cur="cur", n="n", prior="prior", league="league", k=pl.col("k"), r=pl.col("r")
    )
    out = df.with_columns(value.alias("v"), w_cur.alias("wc"), w_prior.alias("wp"))
    for r in out.to_dicts():
        ref = _blend(
            current=r["cur"],
            prior=r["prior"],
            league_avg=r["league"],
            n_cur=r["n"],
            k_metric=r["k"],
            prior_discount=r["r"],
        )
        assert math.isclose(r["v"], ref.value, abs_tol=1e-12)
        assert math.isclose(r["wc"], ref.w_cur, abs_tol=1e-12)
        assert math.isclose(r["wp"], ref.w_prior, abs_tol=1e-12)


def test_blend_is_null_without_a_current_sample():
    df = pl.DataFrame({"cur": [None], "n": [0.0], "prior": [0.3], "league": [0.1]})
    value, _, _ = blend_exprs(cur="cur", n="n", prior="prior", league="league", k=100.0, r=0.8)
    assert df.select(value).item() is None


# --- percentiles -----------------------------------------------------------------------


def _pct_rows() -> pl.DataFrame:
    return pl.DataFrame(
        {
            "player_id": ["A", "B", "C", "A", "B", "D"],
            "week": [1, 1, 1, 2, 2, 2],
            "position_group": ["WR"] * 6,
            "v": [0.1, 0.2, 0.3, 0.4, 0.5, 0.05],
            "ok": [True, True, True, True, True, False],
        }
    )


def test_percentile_formula_and_as_of_population():
    out = as_of_percentiles(_pct_rows(), [("v", "ok", "p")])
    got = {(r["player_id"], r["week"]): r["p"] for r in out.to_dicts()}
    # Week 1: 0.1 / 0.2 / 0.3 among 3 -> 17 / 50 / 83.
    assert (got[("A", 1)], got[("B", 1)], got[("C", 1)]) == (17, 50, 83)
    # Week 2: C didn't play but is in the population through his week-1 row (0.3). D is
    # ineligible: no pct, and not in anyone's population. A 0.4, B 0.5 -> 50, 83.
    assert (got[("A", 2)], got[("B", 2)]) == (50, 83)
    assert got[("D", 2)] is None


def test_percentile_is_per_position_group():
    rows = _pct_rows().with_columns(
        pl.when(pl.col("player_id") == "C")
        .then(pl.lit("TE"))
        .otherwise(pl.col("position_group"))
        .alias("position_group")
    )
    out = as_of_percentiles(rows, [("v", "ok", "p")])
    assert out.filter((pl.col("player_id") == "C") & (pl.col("week") == 1))["p"].item() == 50


# --- rows out --------------------------------------------------------------------------


def test_hash_ignores_as_of_and_inputs_version_and_float_noise():
    cols = [
        "player_id",
        "season",
        "week",
        "x",
        "as_of",
        "inputs_version",
        "content_hash",
        "updated_at",
    ]
    df = pl.DataFrame({"player_id": ["A"], "season": [2026], "week": [1], "x": [0.1 + 0.2]})
    a = finalize_rows(
        df, cols, {"as_of": datetime(2026, 1, 1, tzinfo=UTC), "inputs_version": "pbp@1"}
    )
    b = finalize_rows(
        df.with_columns(pl.col("x") + 1e-12),
        cols,
        {"as_of": datetime(2027, 1, 1, tzinfo=UTC), "inputs_version": "pbp@2"},
    )
    assert a[0]["content_hash"] == b[0]["content_hash"]
    c = finalize_rows(df.with_columns(pl.lit(0.31).alias("x")), cols, {})
    assert c[0]["content_hash"] != a[0]["content_hash"]


class _RecordingConn:
    def __init__(self):
        self.executed: list[tuple[str, tuple]] = []

    def cursor(self):
        conn = self

        class _Cur:
            rowcount = 0

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def execute(self, sql, params=()):
                conn.executed.append((sql, tuple(params)))

        return _Cur()


def test_stale_delete_is_scoped_to_season_and_weeks_through_the_run():
    conn = _RecordingConn()
    delete_stale_player_rows(conn, "player_usage_week", 2026, 3, [("A", 1), ("B", 3)])
    ((sql, params),) = conn.executed
    assert sql.startswith("DELETE FROM player_usage_week WHERE season = %s AND week <= %s ")
    assert params == (2026, 3, ["A", "B"], [1, 3])
    assert "NOT EXISTS" in sql  # only rows this run did NOT produce


def test_column_list_reads_the_create_table_only():
    cols = column_list((_MIGRATIONS / "0031_player_usage_week.sql").read_text(encoding="utf-8"))
    assert cols[:4] == ["player_id", "season", "week", "season_type"]
    assert cols[-2:] == ["content_hash", "updated_at"]
    assert "CREATE" not in cols and len(cols) == len(set(cols))
    eff = column_list((_MIGRATIONS / "0032_player_eff_week.sql").read_text(encoding="utf-8"))
    assert "rec_targets_std" in eff and "epa_per_dropback_vs_zone_hist" in eff
