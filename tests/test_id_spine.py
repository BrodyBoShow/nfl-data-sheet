from pathlib import Path

import polars as pl
import pytest

from pipeline.collectors.id_spine import IdSpineCollector

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


def test_validate_drops_null_keys():
    validated = IdSpineCollector().validate(_load_raw())
    assert validated["players"]["gsis_id"].null_count() == 0
    assert validated["teams"]["team_abbr"].null_count() == 0
    assert validated["schedules"]["game_id"].null_count() == 0


def test_validate_dedupes_ff_playerids_by_gsis_id():
    validated = IdSpineCollector().validate(_load_raw())
    ff = validated["ff_playerids"]
    assert ff["gsis_id"].n_unique() == ff.shape[0]


def test_validate_raises_on_missing_columns():
    raw = _load_raw()
    raw["schedules"] = raw["schedules"].drop("game_id")

    with pytest.raises(ValueError, match="game_id"):
        IdSpineCollector().validate(raw)
