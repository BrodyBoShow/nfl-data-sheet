from pipeline.analysts.availability_impact import (
    _build_depth_groups,
    _build_flagged_positions,
    _build_redistribution_roster,
    _build_trend_sequences,
    _compute_cluster_counts,
    _compute_depth_rank_delta,
    _compute_redistribution,
    _compute_trend_risk,
)

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


def test_build_trend_sequences_prefers_source_with_more_snapshots():
    rows = [
        ("p1", "espn", "Questionable", 1),
        ("p1", "espn", "Doubtful", 2),
        ("p1", "sleeper", "Out", 3),
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


def test_compute_trend_risk_single_snapshot_is_zero():
    rows = _compute_trend_risk({"p1": ["Questionable"]})
    assert rows[0]["value"] == 0.0
    assert rows[0]["sample_n"] == 1


def test_compute_trend_risk_unrecognized_designation_contributes_no_direction():
    rows = _compute_trend_risk({"p1": ["Questionable", "SomeNewStatus", "Doubtful"]})
    # SomeNewStatus can't be scored against either neighbor -- only recognized steps count
    assert rows[0]["value"] == 0.0
