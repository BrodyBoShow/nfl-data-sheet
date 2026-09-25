from datetime import UTC, datetime
from typing import Any

from pipeline.core.injury_changelog import (
    build_cleared_row,
    build_presence_rows,
    decide_injury_row,
    detect_cleared,
    is_source_outage,
    replay_history,
)

# --------------------------------------------------------------------------------------
# decide_injury_row
# --------------------------------------------------------------------------------------


def _state(**overrides: Any) -> dict[str, Any]:
    row: dict[str, Any] = {
        "team": "KC",
        "designation": "Questionable",
        "body_part": "Knee",
        "notes": "n",
    }
    row.update(overrides)
    return row


def test_decide_first_seen_when_no_prior():
    assert decide_injury_row(None, _state()) == "first_seen"


def test_decide_changed_when_designation_differs():
    assert decide_injury_row(_state(designation="Out"), _state()) == "changed"


def test_decide_changed_when_team_differs():
    assert decide_injury_row(_state(team="SF"), _state()) == "changed"


def test_decide_changed_when_body_part_differs():
    assert decide_injury_row(_state(body_part="Ankle"), _state()) == "changed"


def test_decide_changed_when_notes_differs_and_mention_practice():
    new = _state(notes="He was limited in practice Wednesday.")
    assert decide_injury_row(_state(notes="old"), new) == "changed"


def test_decide_practice_match_is_broad_and_case_insensitive():
    # "unable to practice" and "Practiced" both count -- recall over precision.
    texts = ("Unable to practice again.", "He Practiced fully.", "Signed to the practice squad.")
    for text in texts:
        assert decide_injury_row(_state(notes="old"), _state(notes=text)) == "changed", text


def test_decide_skips_notes_only_change_without_practice_language():
    new = _state(notes="Folk missed a 53-yard field goal in the fourth quarter.")
    assert decide_injury_row(_state(notes="old blurb"), new) is None


def test_decide_skips_when_notes_drop_practice_language():
    prior = _state(notes="Limited in practice Wednesday.")
    assert decide_injury_row(prior, _state(notes="Expected to play Sunday.")) is None


def test_decide_changed_when_designation_differs_even_if_notes_lack_practice():
    prior = _state(designation="Out", notes="old")
    assert decide_injury_row(prior, _state(notes="new blurb")) == "changed"


def test_decide_skips_when_nothing_tracked_changed():
    assert decide_injury_row(_state(), _state()) is None


def test_decide_ignores_untracked_fields():
    # raw/season/week/season_type aren't tracked -- only the four classified fields are.
    prior = _state()
    candidate = _state()
    candidate["raw"] = {"details": {"returnDate": "2026-09-27"}}
    assert decide_injury_row(prior, candidate) is None


# --------------------------------------------------------------------------------------
# detect_cleared -- scoped to one source at a time, 2-consecutive-miss clearance
# --------------------------------------------------------------------------------------


def test_detect_cleared_first_miss_does_not_clear():
    active = {"111": {"team": "KC"}, "222": {"team": "SF"}}
    to_clear, updated_counts = detect_cleared(active, present_this_poll={"111"}, prior_counts={})
    assert to_clear == []
    assert updated_counts == {"111": 0, "222": 1}


def test_detect_cleared_second_consecutive_miss_clears():
    active = {"222": {"team": "SF"}}
    to_clear, updated_counts = detect_cleared(
        active, present_this_poll=set(), prior_counts={"222": 1}
    )
    assert to_clear == ["222"]
    assert updated_counts == {}


def test_detect_cleared_reappearance_resets_miss_count_to_zero():
    active = {"222": {"team": "SF"}}
    to_clear, updated_counts = detect_cleared(
        active, present_this_poll={"222"}, prior_counts={"222": 1}
    )
    assert to_clear == []
    assert updated_counts == {"222": 0}


def test_detect_cleared_everyone_still_present_resets_all_to_zero():
    active = {"111": {"team": "KC"}}
    to_clear, updated_counts = detect_cleared(
        active, present_this_poll={"111"}, prior_counts={"111": 0}
    )
    assert to_clear == []
    assert updated_counts == {"111": 0}


def test_detect_cleared_empty_active_set_still_tracks_present_ids():
    # A brand-new (first-seen) player isn't in active_from_db yet, but still needs an
    # injury_presence row created at 0 misses.
    to_clear, updated_counts = detect_cleared({}, present_this_poll={"111"}, prior_counts={})
    assert to_clear == []
    assert updated_counts == {"111": 0}


def test_detect_cleared_miss_threshold_is_configurable():
    active = {"111": {"team": "KC"}}
    to_clear, updated_counts = detect_cleared(
        active, present_this_poll=set(), prior_counts={"111": 2}, miss_threshold=4
    )
    assert to_clear == []
    assert updated_counts == {"111": 3}


# --------------------------------------------------------------------------------------
# build_cleared_row
# --------------------------------------------------------------------------------------


def test_build_cleared_row_nulls_tracked_fields_and_carries_team():
    as_of = datetime(2026, 9, 25, tzinfo=UTC)
    row = build_cleared_row(
        {"team": "KC", "designation": "Questionable"},
        source="espn",
        source_player_id="111",
        player_id="gsis_a",
        season=2026,
        week=3,
        season_type="REG",
        as_of=as_of,
    )
    assert row["is_cleared"] is True
    assert row["designation"] is None
    assert row["body_part"] is None
    assert row["notes"] is None
    assert row["team"] == "KC"
    assert row["raw"]["last_known_designation"] == "Questionable"
    assert row["as_of"] == as_of


# --------------------------------------------------------------------------------------
# build_presence_rows
# --------------------------------------------------------------------------------------


def test_build_presence_rows_first_run_missing_from_presence_state_does_not_raise():
    # injury_presence is empty (e.g. the table was just added, or this source has never
    # been tracked before) but the player is already active in `injuries` and misses
    # this poll -- detect_cleared still puts them in updated_counts (their first tracked
    # miss), even though _fetch_presence_state has no row for them at all. Regression
    # test for the KeyError this caused: 'presence_state[sid]["last_seen_at"]' with no
    # default, on a Sleeper source_player_id absent from presence_state's dict.
    now = datetime(2026, 9, 22, tzinfo=UTC)
    active = {"11394": {"team": "KC"}}
    to_clear, updated_counts = detect_cleared(active, present_this_poll=set(), prior_counts={})
    assert to_clear == []
    assert updated_counts == {"11394": 1}

    rows = build_presence_rows(updated_counts, presence_state={}, now=now)
    assert rows == [{"source_player_id": "11394", "consecutive_misses": 1, "last_seen_at": now}]


def test_build_presence_rows_present_this_poll_uses_now():
    now = datetime(2026, 9, 22, tzinfo=UTC)
    rows = build_presence_rows({"111": 0}, presence_state={}, now=now)
    assert rows == [{"source_player_id": "111", "consecutive_misses": 0, "last_seen_at": now}]


def test_build_presence_rows_surviving_miss_carries_forward_prior_last_seen_at():
    now = datetime(2026, 9, 22, tzinfo=UTC)
    prior_seen = datetime(2026, 9, 20, tzinfo=UTC)
    presence_state = {"222": {"consecutive_misses": 1, "last_seen_at": prior_seen}}
    rows = build_presence_rows({"222": 2}, presence_state, now=now)
    assert rows == [
        {"source_player_id": "222", "consecutive_misses": 2, "last_seen_at": prior_seen}
    ]


# --------------------------------------------------------------------------------------
# is_source_outage
# --------------------------------------------------------------------------------------


def test_outage_when_seen_well_under_half_of_active():
    assert is_source_outage(active_count=800, seen_count=310) is True


def test_no_outage_when_seen_is_close_to_active():
    assert is_source_outage(active_count=800, seen_count=750) is False


def test_no_outage_when_active_count_is_zero():
    assert is_source_outage(active_count=0, seen_count=0) is False


def test_outage_threshold_is_configurable():
    assert is_source_outage(active_count=100, seen_count=60, threshold=0.7) is True
    assert is_source_outage(active_count=100, seen_count=60, threshold=0.5) is False


# --------------------------------------------------------------------------------------
# replay_history -- shared by the verification and backfill scripts
# --------------------------------------------------------------------------------------


def _hist_row(**overrides: Any) -> dict[str, Any]:
    row: dict[str, Any] = {
        "source": "espn",
        "source_player_id": "111",
        "player_id": "gsis_a",
        "season": 2026,
        "week": 3,
        "season_type": "REG",
        "team": "KC",
        "designation": "Questionable",
        "body_part": "Knee",
        "notes": "n",
    }
    row.update(overrides)
    return row


def test_replay_drops_unchanged_repeats_keeps_first_seen_and_real_changes():
    rows = [
        _hist_row(as_of=1),
        _hist_row(as_of=2),  # unchanged repeat -- dropped
        _hist_row(as_of=3, designation="Out"),  # real change -- kept
    ]
    kept, cleared = replay_history(rows)
    assert [r["as_of"] for r in kept] == [1, 3]
    assert cleared == []


def test_replay_single_absence_does_not_clear():
    rows = [
        _hist_row(source_player_id="111", as_of=1),
        _hist_row(source_player_id="222", as_of=1, team="SF", designation="Out"),
        # batch as_of=2: 111 absent once -- one miss, not cleared yet
        _hist_row(source_player_id="222", as_of=2, team="SF", designation="Out"),
        # batch as_of=3: 111 reappears -- miss count resets, never reached the threshold
        _hist_row(source_player_id="111", as_of=3),
        _hist_row(source_player_id="222", as_of=3, team="SF", designation="Out"),
    ]
    kept, cleared = replay_history(rows)
    assert cleared == []
    # 111's as_of=3 row must be kept as a "changed" write (from decide_injury_row's
    # perspective it's the same state as as_of=1 since 111 was never marked cleared, so
    # it's actually a no-op repeat, not a reappearance-change) -- confirms replay never
    # synthesized a clearance for the single miss in between.
    kept_111 = [r for r in kept if r["source_player_id"] == "111"]
    assert [r["as_of"] for r in kept_111] == [1]


def test_replay_two_consecutive_absences_clears_at_the_second():
    rows = [
        _hist_row(source_player_id="111", as_of=1),
        _hist_row(source_player_id="222", as_of=1, team="SF", designation="Out"),
        _hist_row(source_player_id="111", as_of=2),  # still present
        _hist_row(source_player_id="222", as_of=2, team="SF", designation="Out"),
        # batch as_of=3: 111's first miss
        _hist_row(source_player_id="222", as_of=3, team="SF", designation="Out"),
        # batch as_of=4: 111's second consecutive miss -- clears here, not at as_of=3
        _hist_row(source_player_id="222", as_of=4, team="SF", designation="Out"),
    ]
    _, cleared = replay_history(rows)
    assert len(cleared) == 1
    assert cleared[0]["source_player_id"] == "111"
    assert cleared[0]["as_of"] == 4
    assert cleared[0]["is_cleared"] is True
    assert cleared[0]["raw"]["last_known_designation"] == "Questionable"


def test_replay_player_still_present_in_last_batch_is_not_cleared():
    rows = [_hist_row(as_of=1), _hist_row(as_of=2)]
    _, cleared = replay_history(rows)
    assert cleared == []


def test_replay_mid_miss_streak_when_history_runs_out_is_not_cleared():
    rows = [
        _hist_row(source_player_id="111", as_of=1),
        _hist_row(source_player_id="222", as_of=1, team="SF", designation="Out"),
        # batch as_of=2: 111's only miss -- history ends here, one miss short of clearing
        _hist_row(source_player_id="222", as_of=2, team="SF", designation="Out"),
    ]
    _, cleared = replay_history(rows)
    assert cleared == []


def test_replay_reappearance_after_clearance_is_a_change_not_a_repeat():
    rows = [
        _hist_row(source_player_id="111", as_of=1),
        _hist_row(source_player_id="222", team="SF", as_of=1),
        # batch as_of=2: 111's first miss
        _hist_row(source_player_id="222", team="SF", as_of=2),
        # batch as_of=3: 111's second consecutive miss -> cleared
        _hist_row(source_player_id="222", team="SF", as_of=3),
        # batch as_of=4: 111 reappears
        _hist_row(source_player_id="111", as_of=4, designation="Doubtful"),
        _hist_row(source_player_id="222", team="SF", as_of=4),
    ]
    kept, cleared = replay_history(rows)
    assert len(cleared) == 1  # 111's clearance at as_of=3
    assert cleared[0]["as_of"] == 3
    kept_111 = [r for r in kept if r["source_player_id"] == "111"]
    assert [r["as_of"] for r in kept_111] == [1, 4]  # first_seen, then reappearance-change


def test_replay_outage_batch_does_not_count_as_a_miss_even_across_two_batches():
    # active roster of 4; a batch showing only 1 of them trips is_source_outage's
    # default 0.5 threshold (1 < 0.5*4). Two straight outage batches must never add up
    # to a 2-consecutive-miss clearance for the three players the outage didn't see.
    rows = [
        _hist_row(source_player_id="A", as_of=1, team="KC"),
        _hist_row(source_player_id="B", as_of=1, team="KC"),
        _hist_row(source_player_id="C", as_of=1, team="KC"),
        _hist_row(source_player_id="D", as_of=1, team="KC"),
        # batch as_of=2: outage -- only A comes through
        _hist_row(source_player_id="A", as_of=2, team="KC"),
        # batch as_of=3: outage again -- still only A
        _hist_row(source_player_id="A", as_of=3, team="KC"),
        # batch as_of=4: a real poll, everyone's back
        _hist_row(source_player_id="A", as_of=4, team="KC"),
        _hist_row(source_player_id="B", as_of=4, team="KC"),
        _hist_row(source_player_id="C", as_of=4, team="KC"),
        _hist_row(source_player_id="D", as_of=4, team="KC"),
    ]
    _, cleared = replay_history(rows)
    assert cleared == []


def test_replay_sleeper_skip_day_leaves_no_batch_so_it_is_not_a_miss():
    # Sleeper's own <=1/day throttle means a skipped day produces no row at all -- not a
    # batch with zero presence. A gap in as_of values (1 -> 3, nothing at "2") must never
    # be read as a miss, let alone two.
    rows = [
        _hist_row(source="sleeper", source_player_id="111", as_of=1),
        _hist_row(source="sleeper", source_player_id="111", as_of=3),
    ]
    kept, cleared = replay_history(rows)
    assert cleared == []
    assert [r["as_of"] for r in kept] == [1]  # as_of=3 is an unchanged repeat, dropped
