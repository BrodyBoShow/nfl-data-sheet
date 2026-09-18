import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from pipeline.collectors.availability import (
    AvailabilityCollector,
    _diff_transactions,
    _extract_espn_source_player_id,
    _resolve_player_ids,
)

FIXTURES = Path(__file__).parent / "fixtures"


def _load_raw() -> dict:
    espn = json.loads((FIXTURES / "espn_injuries_sample.json").read_text(encoding="utf-8"))
    sleeper = json.loads((FIXTURES / "sleeper_players_sample.json").read_text(encoding="utf-8"))
    return {"espn": espn, "sleeper": sleeper}


# --------------------------------------------------------------------------------------
# validate()
# --------------------------------------------------------------------------------------


def test_validate_extracts_espn_rows_with_designation_and_body_part():
    validated = AvailabilityCollector().validate(_load_raw())
    espn_rows = [r for r in validated["rows"] if r["source"] == "espn"]
    assert espn_rows
    lane = next(r for r in espn_rows if r["source_player_id"] == "4602667")
    assert lane["designation"] == "Questionable"
    assert lane["body_part"] == "Knee" or lane["body_part"] is not None
    assert lane["notes"]


def test_validate_normalizes_wsh_to_was():
    validated = AvailabilityCollector().validate(_load_raw())
    espn_rows = [r for r in validated["rows"] if r["source"] == "espn"]
    assert any(r["team"] == "WAS" for r in espn_rows)
    assert not any(r["team"] == "WSH" for r in espn_rows)


def test_validate_raw_subtree_is_compact_no_logos_or_links():
    validated = AvailabilityCollector().validate(_load_raw())
    espn_rows = [r for r in validated["rows"] if r["source"] == "espn"]
    for row in espn_rows:
        raw_str = json.dumps(row["raw"])
        assert "logos" not in raw_str
        assert "links" not in raw_str
        assert "headshot" not in raw_str


def test_validate_falls_back_to_unmatched_id_when_extraction_fails():
    validated = AvailabilityCollector().validate(_load_raw())
    espn_rows = [r for r in validated["rows"] if r["source"] == "espn"]
    unmatched = [r for r in espn_rows if r["source_player_id"].startswith("unmatched:")]
    assert len(unmatched) == 1
    assert unmatched[0]["source_player_id"] == "unmatched:-9999999"
    assert validated["espn_id_extraction_failed"] == 1


def test_validate_filters_sleeper_to_injured_and_rostered_only():
    validated = AvailabilityCollector().validate(_load_raw())
    sleeper_rows = [r for r in validated["rows"] if r["source"] == "sleeper"]
    # fixture includes one non-injured and one injured-but-no-team player -- both dropped
    assert all(r["designation"] for r in sleeper_rows)
    assert all(r["team"] for r in sleeper_rows)


def test_validate_carries_self_gsis_and_self_espn_for_sleeper_rows():
    validated = AvailabilityCollector().validate(_load_raw())
    sleeper_rows = [r for r in validated["rows"] if r["source"] == "sleeper"]
    assert any(r.get("self_gsis") for r in sleeper_rows)
    assert any(r.get("self_espn") for r in sleeper_rows)
    assert any(not r.get("self_gsis") and not r.get("self_espn") for r in sleeper_rows)


def test_validate_no_sleeper_data_when_not_fetched():
    raw = _load_raw()
    raw["sleeper"] = None
    validated = AvailabilityCollector().validate(raw)
    assert validated["sleeper_fetched"] is False
    assert all(r["source"] == "espn" for r in validated["rows"])


def test_validate_raises_on_missing_espn_keys():
    raw = _load_raw()
    raw["espn"] = {"status": "success"}
    with pytest.raises(ValueError, match="espn"):
        AvailabilityCollector().validate(raw)


def test_validate_raises_on_non_dict_sleeper_payload():
    raw = _load_raw()
    raw["sleeper"] = ["not", "a", "dict"]
    with pytest.raises(ValueError, match="sleeper"):
        AvailabilityCollector().validate(raw)


# --------------------------------------------------------------------------------------
# _extract_espn_source_player_id
# --------------------------------------------------------------------------------------


def test_extract_id_via_links():
    athlete = {"links": [{"href": "https://www.espn.com/nfl/player/_/id/123/some-guy"}]}
    assert _extract_espn_source_player_id(athlete) == ("123", False)


def test_extract_id_falls_back_to_headshot():
    athlete = {
        "links": [{"href": "https://www.espn.com/nfl/player/_/no-id-here/some-guy"}],
        "headshot": {"href": "https://a.espncdn.com/i/headshots/nfl/players/full/456.png"},
    }
    assert _extract_espn_source_player_id(athlete) == ("456", False)


def test_extract_id_reports_failure_when_both_paths_fail():
    athlete = {"links": [], "headshot": {}}
    assert _extract_espn_source_player_id(athlete) == ("", True)


# --------------------------------------------------------------------------------------
# _resolve_player_ids (pure, synthetic data -- no DB)
# --------------------------------------------------------------------------------------


def test_resolve_espn_row_via_crosswalk_espn_id():
    rows = [{"source": "espn", "source_player_id": "111"}]
    resolved = _resolve_player_ids({"111": "gsis_a"}, {}, {}, rows)
    assert resolved[("espn", "111")] == "gsis_a"


def test_resolve_sleeper_row_direct_crosswalk_wins_over_self_ids():
    rows = [
        {
            "source": "sleeper",
            "source_player_id": "222",
            "self_gsis": "gsis_wrong",
            "self_espn": "999",
        }
    ]
    resolved = _resolve_player_ids(
        crosswalk_by_espn_id={"999": "gsis_from_espn_fallback"},
        crosswalk_by_sleeper_id={"222": "gsis_direct"},
        players_by_gsis_id={"gsis_wrong": "gsis_wrong"},
        rows=rows,
    )
    assert resolved[("sleeper", "222")] == "gsis_direct"


def test_resolve_sleeper_row_falls_back_to_self_gsis():
    rows = [
        {"source": "sleeper", "source_player_id": "333", "self_gsis": "gsis_b", "self_espn": None}
    ]
    resolved = _resolve_player_ids({}, {}, {"gsis_b": "gsis_b"}, rows)
    assert resolved[("sleeper", "333")] == "gsis_b"


def test_resolve_sleeper_row_falls_back_to_self_espn():
    rows = [{"source": "sleeper", "source_player_id": "444", "self_gsis": None, "self_espn": "555"}]
    resolved = _resolve_player_ids({"555": "gsis_c"}, {}, {}, rows)
    assert resolved[("sleeper", "444")] == "gsis_c"


def test_resolve_unresolvable_row_stays_none_not_dropped():
    rows = [{"source": "sleeper", "source_player_id": "666", "self_gsis": None, "self_espn": None}]
    resolved = _resolve_player_ids({}, {}, {}, rows)
    assert resolved[("sleeper", "666")] is None


# --------------------------------------------------------------------------------------
# _diff_transactions (pure, synthetic data -- no DB)
# --------------------------------------------------------------------------------------


def _new_row(**overrides):
    row = {
        "player_id": "p1",
        "source": "espn",
        "source_player_id": "111",
        "season": 2026,
        "week": 3,
        "team": "KC",
        "designation": "Questionable",
        "as_of": datetime(2026, 9, 19, tzinfo=UTC),
    }
    row.update(overrides)
    return row


def test_diff_detects_team_change():
    prior = {("espn", "111"): {"team": "SF", "designation": "Questionable"}}
    out = _diff_transactions(prior, [_new_row()])
    assert len(out) == 1
    assert out[0]["transaction_type"] == "team_change"
    assert out[0]["from_value"] == "SF"
    assert out[0]["to_value"] == "KC"


def test_diff_detects_designation_change():
    prior = {("espn", "111"): {"team": "KC", "designation": "Out"}}
    out = _diff_transactions(prior, [_new_row()])
    assert len(out) == 1
    assert out[0]["transaction_type"] == "designation_change"
    assert out[0]["from_value"] == "Out"
    assert out[0]["to_value"] == "Questionable"


def test_diff_no_change_emits_nothing():
    prior = {("espn", "111"): {"team": "KC", "designation": "Questionable"}}
    assert _diff_transactions(prior, [_new_row()]) == []


def test_diff_first_sighting_emits_nothing():
    assert _diff_transactions({}, [_new_row()]) == []
