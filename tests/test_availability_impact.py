from datetime import UTC, date, datetime

from pipeline.analysts.availability_impact import (
    _CATEGORY_HEALTHY,
    _CATEGORY_INJURY,
    _CATEGORY_NON_INJURY_UNAVAILABLE,
    _SIGNAL_SCHEMA,
    _build_depth_groups,
    _build_flagged_positions,
    _build_redistribution_roster,
    _build_signals_frame,
    _build_trend_sequences,
    _classify_designation,
    _compute_availability_category,
    _compute_cluster_counts,
    _compute_depth_rank_delta,
    _compute_redistribution,
    _compute_trend_risk,
)

# --------------------------------------------------------------------------------------
# Designation classification (healthy / injury / non_injury_unavailable)
# --------------------------------------------------------------------------------------


def test_classify_designation_healthy_values():
    assert _classify_designation(None) == _CATEGORY_HEALTHY
    assert _classify_designation("Active") == _CATEGORY_HEALTHY


def test_classify_designation_injury_values():
    for value in ("Questionable", "Doubtful", "Out", "IR", "Injured Reserve", "PUP"):
        assert _classify_designation(value) == _CATEGORY_INJURY


def test_classify_designation_non_injury_unavailable_values():
    for value in ("NA", "Sus", "COV", "DNR"):
        assert _classify_designation(value) == _CATEGORY_NON_INJURY_UNAVAILABLE


def test_classify_designation_unrecognized_value_defaults_to_injury():
    # err toward flagging -- never silently treat an unrecognized designation as healthy
    assert _classify_designation("SomeBrandNewStatus") == _CATEGORY_INJURY


def test_compute_availability_category_encodes_both_flagged_buckets():
    flagged = [
        {"player_id": "p1", "category": _CATEGORY_INJURY},
        {"player_id": "p2", "category": _CATEGORY_NON_INJURY_UNAVAILABLE},
    ]
    rows = _compute_availability_category(flagged)
    by_player = {r["player_id"]: r["value"] for r in rows}
    assert by_player["p1"] == 1.0
    assert by_player["p2"] == 2.0
    assert all(r["signal"] == "availability_category" for r in rows)


# --------------------------------------------------------------------------------------
# Snap-share redistribution
# --------------------------------------------------------------------------------------


def test_build_redistribution_roster_includes_flagged_and_healthy_teammates():
    flagged = [{"player_id": "wr1", "team": "KC", "designation": "Out"}]
    positions = {
        "wr1": ("KC", "WR"),
        "wr2": ("KC", "WR"),
        "rb1": ("KC", "RB"),  # different position -- not in the WR group
        "wr3": ("SF", "WR"),  # different team -- not relevant
    }
    snap_share = {"wr1": 0.6, "wr2": 0.4}
    roster = _build_redistribution_roster(flagged, positions, snap_share)
    ids = {r["player_id"] for r in roster}
    assert ids == {"wr1", "wr2"}


def test_compute_redistribution_splits_proportionally_by_snap_share():
    roster = [
        {"player_id": "wr1", "team": "KC", "position": "WR", "snap_share": 0.6, "injured": True},
        {"player_id": "wr2", "team": "KC", "position": "WR", "snap_share": 0.3, "injured": False},
        {"player_id": "wr3", "team": "KC", "position": "WR", "snap_share": 0.1, "injured": False},
    ]
    rows = _compute_redistribution(roster)
    by_signal = {(r["player_id"], r["signal"]): r["value"] for r in rows}
    assert by_signal[("wr1", "snap_share_at_risk")] == 0.6
    # wr2 gets 0.6 * (0.3 / 0.4) = 0.45, wr3 gets 0.6 * (0.1 / 0.4) = 0.15
    assert round(by_signal[("wr2", "snap_share_redistribution_gain")], 4) == 0.45
    assert round(by_signal[("wr3", "snap_share_redistribution_gain")], 4) == 0.15


def test_compute_redistribution_sums_gains_from_multiple_injured_teammates():
    roster = [
        {"player_id": "wr1", "team": "KC", "position": "WR", "snap_share": 0.5, "injured": True},
        {"player_id": "wr2", "team": "KC", "position": "WR", "snap_share": 0.5, "injured": True},
        {"player_id": "wr3", "team": "KC", "position": "WR", "snap_share": 1.0, "injured": False},
    ]
    rows = _compute_redistribution(roster)
    gain_rows = [r for r in rows if r["player_id"] == "wr3"]
    assert len(gain_rows) == 1  # one row per (player, signal), not one per injured teammate
    # 0.5 + 0.5, all of it since wr3 is the only healthy one
    assert round(gain_rows[0]["value"], 4) == 1.0


def test_compute_redistribution_no_healthy_teammates_emits_only_at_risk():
    roster = [
        {"player_id": "wr1", "team": "KC", "position": "WR", "snap_share": 0.6, "injured": True},
    ]
    rows = _compute_redistribution(roster)
    assert len(rows) == 1
    assert rows[0]["signal"] == "snap_share_at_risk"


def test_compute_redistribution_missing_snap_share_is_skipped():
    roster = [
        {"player_id": "wr1", "team": "KC", "position": "WR", "snap_share": None, "injured": True},
    ]
    assert _compute_redistribution(roster) == []


# --------------------------------------------------------------------------------------
# Replacement depth-rank delta
# --------------------------------------------------------------------------------------


def test_build_depth_groups_splits_flagged_and_healthy():
    flagged = [{"player_id": "qb1", "team": "KC"}]
    depth = [
        {"team": "KC", "pos_abb": "QB", "pos_rank": 1, "player_id": "qb1"},
        {"team": "KC", "pos_abb": "QB", "pos_rank": 2, "player_id": "qb2"},
    ]
    flagged_depth, healthy_depth = _build_depth_groups(flagged, depth)
    assert flagged_depth == [depth[0]]
    assert healthy_depth == [depth[1]]


def test_compute_depth_rank_delta_finds_lowest_ranked_backup():
    flagged_depth = [{"team": "KC", "pos_abb": "LT", "pos_rank": 1, "player_id": "lt1"}]
    healthy_depth = [
        {"team": "KC", "pos_abb": "LT", "pos_rank": 3, "player_id": "lt3"},
        {"team": "KC", "pos_abb": "LT", "pos_rank": 2, "player_id": "lt2"},
    ]
    rows = _compute_depth_rank_delta(flagged_depth, healthy_depth)
    assert len(rows) == 1
    assert rows[0]["player_id"] == "lt2"  # rank 2, not rank 3
    assert rows[0]["value"] == 1.0  # 2 - 1
    assert rows[0]["signal"] == "replacement_depth_rank_delta"


def test_compute_depth_rank_delta_no_candidate_emits_nothing():
    flagged_depth = [{"team": "KC", "pos_abb": "LT", "pos_rank": 1, "player_id": "lt1"}]
    healthy_depth: list[dict] = []  # no one else at that slot
    assert _compute_depth_rank_delta(flagged_depth, healthy_depth) == []


def test_compute_depth_rank_delta_ignores_higher_ranked_backup():
    # a "backup" ranked ABOVE (lower number than) the flagged starter shouldn't count
    flagged_depth = [{"team": "KC", "pos_abb": "LT", "pos_rank": 2, "player_id": "lt2"}]
    healthy_depth = [{"team": "KC", "pos_abb": "LT", "pos_rank": 1, "player_id": "lt1"}]
    assert _compute_depth_rank_delta(flagged_depth, healthy_depth) == []


# --------------------------------------------------------------------------------------
# OL/secondary cluster counts
# --------------------------------------------------------------------------------------


def test_build_flagged_positions_drops_unknown_position():
    flagged = [{"player_id": "p1", "team": "KC"}, {"player_id": "p2", "team": "KC"}]
    positions = {"p1": ("KC", "T"), "p2": ("KC", None)}
    out = _build_flagged_positions(flagged, positions)
    assert out == [{"team": "KC", "position": "T"}]


def test_compute_cluster_counts_buckets_ol_and_secondary_separately():
    flagged_positions = [
        {"team": "KC", "position": "T"},
        {"team": "KC", "position": "G"},
        {"team": "KC", "position": "CB"},
        {"team": "SF", "position": "S"},
    ]
    rows = _compute_cluster_counts(flagged_positions)
    by_key = {(r["team"], r["signal"]): r["value"] for r in rows}
    assert by_key[("KC", "ol_cluster_count")] == 2.0
    assert by_key[("KC", "secondary_cluster_count")] == 1.0
    assert by_key[("SF", "secondary_cluster_count")] == 1.0
    assert ("SF", "ol_cluster_count") not in by_key


def test_compute_cluster_counts_skips_non_ol_secondary_positions():
    flagged_positions = [{"team": "KC", "position": "QB"}]
    assert _compute_cluster_counts(flagged_positions) == []


# --------------------------------------------------------------------------------------
# Practice-trend risk
# --------------------------------------------------------------------------------------


def test_build_trend_sequences_prefers_source_with_more_distinct_days():
    rows = [
        ("p1", "espn", "Questionable", datetime(2026, 9, 16, 12, tzinfo=UTC)),
        ("p1", "espn", "Doubtful", datetime(2026, 9, 17, 12, tzinfo=UTC)),
        ("p1", "sleeper", "Out", datetime(2026, 9, 16, 12, tzinfo=UTC)),
    ]
    sequences = _build_trend_sequences(rows)
    assert sequences["p1"] == ["Questionable", "Doubtful"]


def test_build_trend_sequences_collapses_same_day_snapshots_to_the_last_one():
    # two runs 27 seconds apart on the same day -- verified live, not a real trend point
    rows = [
        ("p1", "espn", "Questionable", datetime(2026, 9, 18, 4, 13, 2, tzinfo=UTC)),
        ("p1", "espn", "Doubtful", datetime(2026, 9, 18, 4, 13, 29, tzinfo=UTC)),
    ]
    sequences = _build_trend_sequences(rows)
    assert sequences["p1"] == ["Doubtful"]  # one entry, the later same-day observation


def test_build_trend_sequences_accepts_plain_dates_too():
    rows = [
        ("p1", "espn", "Questionable", date(2026, 9, 16)),
        ("p1", "espn", "Doubtful", date(2026, 9, 17)),
    ]
    sequences = _build_trend_sequences(rows)
    assert sequences["p1"] == ["Questionable", "Doubtful"]


def test_compute_trend_risk_scores_escalation_as_positive():
    rows = _compute_trend_risk({"p1": ["Questionable", "Doubtful", "Out"]})
    assert rows[0]["value"] == 2.0
    assert rows[0]["sample_n"] == 3


def test_compute_trend_risk_scores_deescalation_as_negative():
    rows = _compute_trend_risk({"p1": ["Out", "Questionable"]})
    assert rows[0]["value"] == -1.0


def test_compute_trend_risk_flat_is_zero():
    rows = _compute_trend_risk({"p1": ["Questionable", "Questionable"]})
    assert rows[0]["value"] == 0.0


def test_compute_trend_risk_single_snapshot_emits_nothing():
    # a single day's observation can't produce a trend -- emitting a 0 here would be
    # indistinguishable from "confirmed flat" (two-or-more observations, no change), so
    # this player gets no practice_trend_risk row at all rather than a misleading 0.
    assert _compute_trend_risk({"p1": ["Questionable"]}) == []


def test_compute_trend_risk_unrecognized_designation_contributes_no_direction():
    rows = _compute_trend_risk({"p1": ["Questionable", "SomeNewStatus", "Doubtful"]})
    # SomeNewStatus can't be scored against either neighbor -- only recognized steps count
    assert rows[0]["value"] == 0.0


# --------------------------------------------------------------------------------------
# _build_signals_frame -- regression test for the live failure: combining every signal
# type into one frame with pl.DataFrame(rows, schema=_SIGNAL_COLS) (column names only)
# let Polars infer per-column dtypes from the first rows and then raise a ComputeError
# once a later row's type didn't match (e.g. an int cluster count arriving after
# float-valued rows, or the first non-null sample_n arriving after many all-null ones).
# The per-function tests above never combined outputs, so none of them caught it.
# --------------------------------------------------------------------------------------


def test_build_signals_frame_combines_all_seven_signal_types_with_correct_dtypes():
    redistribution_rows = [
        {"player_id": "wr1", "signal": "snap_share_at_risk", "value": 0.6},
        {"player_id": "wr2", "signal": "snap_share_redistribution_gain", "value": 0.45},
    ]
    depth_delta_rows = [
        {"player_id": "lt2", "signal": "replacement_depth_rank_delta", "value": 1.0},
    ]
    cluster_rows = [
        {"team": "KC", "signal": "ol_cluster_count", "value": 2.0},
        {"team": "KC", "signal": "secondary_cluster_count", "value": 1.0},
    ]
    trend_rows = [
        {"player_id": "wr1", "signal": "practice_trend_risk", "value": 2.0, "sample_n": 3},
    ]
    category_rows = [
        {"player_id": "wr1", "signal": "availability_category", "value": 1.0},
        {"player_id": "susp1", "signal": "availability_category", "value": 2.0},
    ]

    df = _build_signals_frame(
        redistribution_rows=redistribution_rows,
        depth_delta_rows=depth_delta_rows,
        cluster_rows=cluster_rows,
        trend_rows=trend_rows,
        category_rows=category_rows,
        season=2026,
        week=3,
        as_of=datetime(2026, 9, 19, tzinfo=UTC),
        inputs_version="snap_counts@x,depth_charts@y",
    )

    assert df.height == 8
    for col, dtype in _SIGNAL_SCHEMA.items():
        assert df.schema[col] == dtype, f"{col}: expected {dtype}, got {df.schema[col]}"

    signals_present = set(df["signal"].to_list())
    assert signals_present == {
        "snap_share_at_risk",
        "snap_share_redistribution_gain",
        "replacement_depth_rank_delta",
        "ol_cluster_count",
        "secondary_cluster_count",
        "practice_trend_risk",
        "availability_category",
    }


def test_build_signals_frame_empty_input_returns_empty_frame_with_correct_schema():
    df = _build_signals_frame(
        redistribution_rows=[],
        depth_delta_rows=[],
        cluster_rows=[],
        trend_rows=[],
        category_rows=[],
        season=2026,
        week=3,
        as_of=datetime(2026, 9, 19, tzinfo=UTC),
        inputs_version="snap_counts@x,depth_charts@y",
    )
    assert df.height == 0
    for col, dtype in _SIGNAL_SCHEMA.items():
        assert df.schema[col] == dtype
