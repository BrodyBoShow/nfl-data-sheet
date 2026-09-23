from pathlib import Path

import polars as pl
import pytest

from pipeline.collectors.id_spine import (
    _GAME_COLS,
    _RETIRED_TEAM_CODES,
    IdSpineCollector,
    _build_sleeper_crosswalk_updates,
)

FIXTURES = Path(__file__).parent / "fixtures"


def _load_raw() -> dict[str, pl.DataFrame]:
    return {
        "schedules": pl.read_parquet(FIXTURES / "nflreadpy_schedules_sample.parquet"),
        "teams": pl.read_parquet(FIXTURES / "nflreadpy_teams_sample.parquet"),
        "players": pl.read_parquet(FIXTURES / "nflreadpy_players_sample.parquet"),
        "ff_playerids": pl.read_parquet(FIXTURES / "nflreadpy_ff_playerids_sample.parquet"),
    }


def test_validate_derives_season_type():
    validated = IdSpineCollector().validate(_load_raw())
    season_types = set(validated["schedules"]["season_type"].to_list())
    assert season_types <= {"REG", "POST"}


def test_game_location_is_stored_and_matches_the_check_constraint():
    # games.location CHECK (migration 0022) allows only 'Home'/'Neutral'. The fixture has
    # a real Neutral row, so this also proves the column survives the _GAME_COLS select.
    validated = IdSpineCollector().validate(_load_raw())
    games = validated["schedules"].select(_GAME_COLS)
    assert set(games["location"].drop_nulls().to_list()) == {"Home", "Neutral"}


def test_validate_drops_null_keys():
    validated = IdSpineCollector().validate(_load_raw())
    assert validated["players"]["gsis_id"].null_count() == 0
    assert validated["teams"]["team_abbr"].null_count() == 0
    assert validated["schedules"]["game_id"].null_count() == 0


def test_retired_team_codes_are_exactly_the_documented_four():
    """Guards against drift from the verified-live 36-row teams table
    (docs/phases/P2.md) -- 32 current codes plus these four retired aliases."""
    assert _RETIRED_TEAM_CODES == frozenset({"OAK", "SD", "STL", "LAR"})


def test_validate_dedupes_ff_playerids_by_gsis_id():
    validated = IdSpineCollector().validate(_load_raw())
    ff = validated["ff_playerids"]
    assert ff["gsis_id"].n_unique() == ff.shape[0]


def test_validate_raises_on_missing_columns():
    raw = _load_raw()
    raw["schedules"] = raw["schedules"].drop("game_id")

    with pytest.raises(ValueError, match="game_id"):
        IdSpineCollector().validate(raw)


# --------------------------------------------------------------------------------------
# Sleeper crosswalk enrichment (_build_sleeper_crosswalk_updates -- pure, no DB)
# --------------------------------------------------------------------------------------


def test_sleeper_enrichment_fills_null_sleeper_id():
    sleeper_players = {"999": {"gsis_id": "00-0012345"}}
    current: dict[str, str | None] = {"00-0012345": None}
    assert _build_sleeper_crosswalk_updates(sleeper_players, current) == [("00-0012345", "999")]


def test_sleeper_enrichment_never_overwrites_existing_sleeper_id():
    sleeper_players = {"999": {"gsis_id": "00-0012345"}}
    current: dict[str, str | None] = {"00-0012345": "111"}  # already has a sleeper_id
    assert _build_sleeper_crosswalk_updates(sleeper_players, current) == []


def test_sleeper_enrichment_skips_players_not_in_crosswalk_at_all():
    sleeper_players = {"999": {"gsis_id": "00-0099999"}}
    current: dict[str, str | None] = {}  # gsis_id not present -- never create a new row
    assert _build_sleeper_crosswalk_updates(sleeper_players, current) == []


def test_sleeper_enrichment_skips_players_without_a_gsis_id():
    sleeper_players = {"999": {"gsis_id": None}, "888": {}}
    current: dict[str, str | None] = {"00-0012345": None}
    assert _build_sleeper_crosswalk_updates(sleeper_players, current) == []
