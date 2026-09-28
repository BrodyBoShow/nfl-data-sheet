import polars as pl

from scripts import backtest
from scripts.backtest import PARITY_EXEMPT_WEEKS, PARITY_TOLERANCE, _parity_compare

NAMES = ["epa_per_play_off", "epa_per_play_def"]
WEEKS = [(s, w) for s in (2019, 2021, 2023, 2025) for w in (1, 7, 10)]


def _signals(weeks: list[tuple[int, int]]) -> pl.DataFrame:
    rows = [
        {
            "season": s,
            "week": w,
            "team": t,
            "signal": n,
            "value": 0.01 * w + (0.1 if t == "KC" else -0.1),
            "stability": 0.5,
        }
        for s, w in weeks
        for t in ("KC", "BUF")
        for n in NAMES
    ]
    return pl.DataFrame(
        rows,
        schema={
            "season": pl.Int64,
            "week": pl.Int64,
            "team": pl.Utf8,
            "signal": pl.Utf8,
            "value": pl.Float64,
            "stability": pl.Float64,
        },
    )


def _nudge(df: pl.DataFrame, season: int, week: int, by: float, col: str = "value"):
    hit = (pl.col("season") == season) & (pl.col("week") == week) & (pl.col("team") == "KC")
    return df.with_columns(pl.when(hit).then(pl.col(col) + by).otherwise(pl.col(col)).alias(col))


def test_identical_recompute_passes_and_reports_its_margin():
    stored = _signals(WEEKS)
    result = _parity_compare(stored, stored, NAMES, WEEKS)
    assert result["ok"] and result["failing_weeks"] == []
    assert result["weeks"] == len(WEEKS) and result["rows"] == len(WEEKS) * 4
    assert result["max_value_diff"] == 0.0 and result["worst_value_week"] is not None


def test_one_diverging_week_that_isnt_2021_wk10_is_caught_and_named():
    stored = _signals(WEEKS)
    scratch = _nudge(stored, 2023, 7, 1e-6)
    result = _parity_compare(scratch, stored, NAMES, WEEKS)
    assert not result["ok"]
    assert result["failing_weeks"] == [(2023, 7)]
    assert result["worst_value_week"] == (2023, 7)
    assert abs(result["max_value_diff"] - 1e-6) < 1e-12


def test_the_old_single_week_scope_misses_that_same_divergence():
    """What PARITY_WEEK = (2021, 10) used to check: it recomputed only that week, so the
    same diverged data passes."""
    stored = _signals(WEEKS)
    scratch = _nudge(stored, 2023, 7, 1e-6)
    only_old_week = scratch.filter((pl.col("season") == 2021) & (pl.col("week") == 10))
    assert _parity_compare(only_old_week, stored, NAMES, [(2021, 10)])["ok"]


def test_a_divergence_inside_tolerance_passes_one_past_it_fails():
    stored = _signals(WEEKS)
    assert _parity_compare(_nudge(stored, 2019, 1, PARITY_TOLERANCE / 2), stored, NAMES, WEEKS)[
        "ok"
    ]
    assert not _parity_compare(_nudge(stored, 2019, 1, PARITY_TOLERANCE * 2), stored, NAMES, WEEKS)[
        "ok"
    ]


def test_rows_missing_from_one_side_fail_their_week():
    stored = _signals(WEEKS)
    scratch = stored.filter(
        ~((pl.col("season") == 2025) & (pl.col("week") == 10) & (pl.col("team") == "BUF"))
    )
    result = _parity_compare(scratch, stored, NAMES, WEEKS)
    assert result["failing_weeks"] == [(2025, 10)] and result["unmatched"] == 2


def test_a_week_with_no_rows_on_either_side_fails():
    stored = _signals(WEEKS)
    gone = stored.filter(~((pl.col("season") == 2019) & (pl.col("week") == 7)))
    result = _parity_compare(gone, gone, NAMES, WEEKS)
    assert result["failing_weeks"] == [(2019, 7)]


def test_stability_is_reported_not_gated():
    """signals.stability is float4; stored values carry ~1e-7 rounding."""
    stored = _signals(WEEKS)
    result = _parity_compare(_nudge(stored, 2021, 1, 5e-7, "stability"), stored, NAMES, WEEKS)
    assert result["ok"]
    assert result["worst_stability_week"] == (2021, 1)


def test_exempt_weeks_are_named_skipped_and_reported(monkeypatch):
    stored = _signals(WEEKS)
    computed: list[list[tuple[int, int]]] = []

    def fake_scratch(conn, season_weeks, metrics, *, progress=False):
        computed.append(list(season_weeks))
        # the exempt week would mismatch if it were compared
        return _nudge(stored, 2025, 1, 1.0).join(
            pl.DataFrame(season_weeks, schema={"season": pl.Int64, "week": pl.Int64}, orient="row"),
            on=["season", "week"],
            how="semi",
        )

    monkeypatch.setattr(backtest, "_scratch_efficiency", fake_scratch)
    monkeypatch.setattr(backtest, "PARITY_EXEMPT_WEEKS", {(2025, 1): "live depth fallback"})
    metrics = [m for m in backtest.efficiency._METRIC_CONFIG if m.name == "epa_per_play"]
    result = backtest._parity_check(None, stored, metrics, WEEKS)  # type: ignore[arg-type]
    assert (2025, 1) not in computed[0] and len(computed[0]) == len(WEEKS) - 1
    assert result["ok"] and result["exempt"] == {"2025 wk1": "live depth fallback"}


def test_every_exemption_carries_a_reason():
    assert all(isinstance(why, str) and why.strip() for why in PARITY_EXEMPT_WEEKS.values())
