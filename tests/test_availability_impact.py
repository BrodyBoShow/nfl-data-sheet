from datetime import UTC, date, datetime

from pipeline.analysts.availability_impact import (
    _CATEGORY_HEALTHY,
    _CATEGORY_INJURY,
    _CATEGORY_NON_INJURY_UNAVAILABLE,
    _SIGNAL_SCHEMA,
    _any_flagged,
    _build_depth_groups,
    _build_flagged_positions,
    _build_redistribution_roster,
    _build_signals_frame,
    _build_trend_sequences,
    _classify_designation,
    _combine_seed_and_window_rows,
    _compute_availability_category,
    _compute_cluster_counts,
    _compute_depth_rank_delta,
    _compute_redistribution,
    _compute_trend_risk,
    _resolve_current_state,
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


# --------------------------------------------------------------------------------------
# _resolve_current_state -- cross-source resolution. An ESPN clearance must never
# override an active Sleeper designation: ESPN's injury report is a weekly-report proxy
# that drops IR/PUP/Reserve players once they're old news, while Sleeper keeps carrying
# them (verified in P3: ~210 Sleeper-only flagged players, mostly IR/PUP/Reserve).
# --------------------------------------------------------------------------------------


def test_resolve_current_state_espn_only_flagged():
    sources = {"espn": {"team": "KC", "designation": "Questionable"}}
    r = _resolve_current_state("p1", sources)
    assert r["designation"] == "Questionable"


def test_resolve_current_state_sleeper_only_flagged():
    sources = {"sleeper": {"team": "DAL", "designation": "IR"}}
    r = _resolve_current_state("p1", sources)
    assert r["designation"] == "IR"


def test_resolve_current_state_espn_cleared_sleeper_still_flagged_stays_flagged():
    # the exact bug: an ESPN clearance must not override an active Sleeper designation
    sources = {
        "espn": {"team": "DAL", "designation": None},
        "sleeper": {"team": "DAL", "designation": "IR"},
    }
    r = _resolve_current_state("p1", sources)
    assert r["designation"] == "IR"
    assert r["team"] == "DAL"


def test_resolve_current_state_both_flagged_prefers_espn_designation():
    sources = {
        "espn": {"team": "KC", "designation": "Doubtful"},
        "sleeper": {"team": "KC", "designation": "Out"},
    }
    r = _resolve_current_state("p1", sources)
    assert r["designation"] == "Doubtful"


def test_resolve_current_state_healthy_only_when_every_source_agrees():
    sources = {
        "espn": {"team": "KC", "designation": None},
        "sleeper": {"team": "KC", "designation": "Active"},
    }
    r = _resolve_current_state("p1", sources)
    assert r["designation"] is None
    assert _classify_designation(r["designation"]) == _CATEGORY_HEALTHY


def test_resolve_current_state_single_source_cleared_is_healthy():
    sources = {"espn": {"team": "KC", "designation": None}}
    r = _resolve_current_state("p1", sources)
    assert r["designation"] is None


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


def test_build_trend_sequences_falls_back_to_more_distinct_days_with_no_driving_source():
    # driving_source_by_player has no entry for p1 -- falls back to the old tie-break
    # (longer sequence wins). Only reachable for a player _resolve_current_state never
    # saw; see test_build_trend_sequences_uses_the_resolved_current_states_driving_source
    # for the real, common path.
    rows = [
        ("p1", "espn", "Questionable", False, datetime(2026, 9, 16, 12, tzinfo=UTC)),
        ("p1", "espn", "Doubtful", False, datetime(2026, 9, 17, 12, tzinfo=UTC)),
        ("p1", "sleeper", "Out", False, datetime(2026, 9, 16, 12, tzinfo=UTC)),
    ]
    poll_days = {"espn": [date(2026, 9, 16), date(2026, 9, 17)], "sleeper": [date(2026, 9, 16)]}
    sequences = _build_trend_sequences(rows, poll_days, {"p1": None})
    assert sequences["p1"] == ["Questionable", "Doubtful"]


def test_build_trend_sequences_collapses_same_day_changes_to_the_last_one():
    # two changes 27 seconds apart on the same day -- verified live, not a real trend point
    rows = [
        ("p1", "espn", "Questionable", False, datetime(2026, 9, 18, 4, 13, 2, tzinfo=UTC)),
        ("p1", "espn", "Doubtful", False, datetime(2026, 9, 18, 4, 13, 29, tzinfo=UTC)),
    ]
    poll_days = {"espn": [date(2026, 9, 18)], "sleeper": []}
    sequences = _build_trend_sequences(rows, poll_days, {"p1": "espn"})
    assert sequences["p1"] == ["Doubtful"]  # one entry, the later same-day change


def test_build_trend_sequences_accepts_plain_dates_too():
    rows = [
        ("p1", "espn", "Questionable", False, date(2026, 9, 16)),
        ("p1", "espn", "Doubtful", False, date(2026, 9, 17)),
    ]
    poll_days = {"espn": [date(2026, 9, 16), date(2026, 9, 17)], "sleeper": []}
    sequences = _build_trend_sequences(rows, poll_days, {"p1": "espn"})
    assert sequences["p1"] == ["Questionable", "Doubtful"]


def test_build_trend_sequences_forward_fills_across_poll_days_with_no_change():
    # only one row all week (first appearance) -- must still fill every poll day, not
    # just the one day it was written, or a stable player would never reach the >=2
    # distinct-day gate _compute_trend_risk requires.
    rows = [("p1", "espn", "Questionable", False, datetime(2026, 9, 16, 12, tzinfo=UTC))]
    poll_days = {"espn": [date(2026, 9, 16), date(2026, 9, 17), date(2026, 9, 18)], "sleeper": []}
    sequences = _build_trend_sequences(rows, poll_days, {"p1": "espn"})
    assert sequences["p1"] == ["Questionable", "Questionable", "Questionable"]


def test_build_trend_sequences_is_cleared_row_registers_as_active_deescalation():
    rows = [
        ("p1", "espn", "Questionable", False, datetime(2026, 9, 16, 12, tzinfo=UTC)),
        ("p1", "espn", None, True, datetime(2026, 9, 17, 12, tzinfo=UTC)),
    ]
    poll_days = {"espn": [date(2026, 9, 16), date(2026, 9, 17)], "sleeper": []}
    sequences = _build_trend_sequences(rows, poll_days, {"p1": "espn"})
    assert sequences["p1"] == ["Questionable", "Active"]
    rows_out = _compute_trend_risk({"p1": sequences["p1"]})
    assert rows_out[0]["value"] == -1.0  # Questionable -> Active is a de-escalation


def test_build_trend_sequences_no_poll_days_for_source_emits_no_sequence():
    rows = [("p1", "sleeper", "Out", False, datetime(2026, 9, 16, 12, tzinfo=UTC))]
    sequences = _build_trend_sequences(rows, {"espn": [], "sleeper": []}, {"p1": "sleeper"})
    assert "p1" not in sequences


def test_build_trend_sequences_uses_the_resolved_current_states_driving_source():
    # The cross-source bug, in the trend instead of the category: ESPN clears the player
    # (IR -> Active, a de-escalation taken alone) while Sleeper still has them on IR the
    # whole time (flat). _resolve_current_state resolved this player as still flagged via
    # Sleeper (an ESPN clearance never overrides an active Sleeper designation) -- the
    # trend sequence must follow that same source, not "whichever has more poll days"
    # (which would pick ESPN here, 2 days vs Sleeper's 2 -- tie, but even untied this must
    # never override the driving source).
    rows = [
        ("p1", "espn", "IR", False, datetime(2026, 9, 16, 12, tzinfo=UTC)),
        ("p1", "espn", None, True, datetime(2026, 9, 17, 12, tzinfo=UTC)),
        ("p1", "sleeper", "IR", False, datetime(2026, 9, 16, 12, tzinfo=UTC)),
        ("p1", "sleeper", "IR", False, datetime(2026, 9, 17, 12, tzinfo=UTC)),
    ]
    poll_days = {
        "espn": [date(2026, 9, 16), date(2026, 9, 17)],
        "sleeper": [date(2026, 9, 16), date(2026, 9, 17)],
    }
    sequences = _build_trend_sequences(rows, poll_days, {"p1": "sleeper"})
    assert sequences["p1"] == ["IR", "IR"]  # Sleeper's flat sequence, not ESPN's de-escalation
    trend_rows = _compute_trend_risk({"p1": sequences["p1"]})
    assert trend_rows[0]["value"] == 0.0  # flat -- "still on IR", not "improving"


# --------------------------------------------------------------------------------------
# _combine_seed_and_window_rows / _any_flagged -- the season/week carryover fix: a
# change-log row is stamped with the week the change happened, not every week the player
# remains in that state, so a player whose last change predates the target week must
# still show up via a seed row, not vanish for lack of a week-stamped row.
# --------------------------------------------------------------------------------------


def test_combine_seed_and_window_rows_merges_and_sorts():
    seed = [("p1", "espn", "Questionable", False, datetime(2026, 9, 10, tzinfo=UTC))]
    window = [("p1", "espn", "Doubtful", False, datetime(2026, 9, 17, tzinfo=UTC))]
    combined = _combine_seed_and_window_rows(seed, window)
    assert combined == [
        ("p1", "espn", "Questionable", False, datetime(2026, 9, 10, tzinfo=UTC)),
        ("p1", "espn", "Doubtful", False, datetime(2026, 9, 17, tzinfo=UTC)),
    ]


def test_two_week_carryover_player_still_flagged_and_in_trend_sequence():
    # Last change was in week 2 (Questionable); no row at all in week 3. Both
    # inputs_ready's flagged check and the trend sequence must still see them going into
    # week 3, forward-filled from the week-2 seed across week 3's poll days.
    week2_change = ("p1", "espn", "Questionable", False, datetime(2026, 9, 10, 12, tzinfo=UTC))

    current_injuries = [{"player_id": "p1", "team": "KC", "designation": "Questionable"}]
    assert _any_flagged(current_injuries) is True  # counts for inputs_ready

    week3_poll_days = {"espn": [date(2026, 9, 17), date(2026, 9, 18)], "sleeper": []}
    combined = _combine_seed_and_window_rows(seed_rows=[week2_change], window_rows=[])
    sequences = _build_trend_sequences(combined, week3_poll_days, {"p1": "espn"})
    assert sequences["p1"] == ["Questionable", "Questionable"]  # flat, not empty
    trend_rows = _compute_trend_risk({"p1": sequences["p1"]})
    assert trend_rows[0]["value"] == 0.0
    assert trend_rows[0]["sample_n"] == 2


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
