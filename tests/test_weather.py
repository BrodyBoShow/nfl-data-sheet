import copy
import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from pipeline.collectors.weather import (
    WINDOW_HOURS,
    GameVenue,
    NoForecastData,
    Stadium,
    build_rows,
    game_window,
    parse_forecast,
    resolve_venue,
)
from pipeline.collectors.weather_schedule import compute_game_targets

FIXTURES = Path(__file__).parent / "fixtures"

# The fixture is the real T-48h response for 2026_03_ATL_GB (Lambeau, kickoff
# 2026-09-25T00:15Z), requested with the same params WeatherCollector.fetch sends.
_KICKOFF = datetime(2026, 9, 25, 0, 15, tzinfo=UTC)
_AS_OF = datetime(2026, 9, 23, 0, 21, tzinfo=UTC)


def _body() -> dict:
    return json.loads((FIXTURES / "open_meteo_forecast_sample.json").read_text(encoding="utf-8"))


def _game(**kw) -> GameVenue:
    base = dict(
        game_id="2026_03_ATL_GB",
        season=2026,
        week=3,
        stadium_id="GNB00",
        stadium_name="Lambeau Field",
        roof="outdoors",
    )
    return GameVenue(**{**base, **kw})


def _stadium(**kw) -> Stadium:
    base = dict(
        stadium_id="GNB00",
        known_names=("Lambeau Field",),
        lat=44.50133,
        lon=-88.06222,
        roof_type="open",
    )
    return Stadium(**{**base, **kw})


# --------------------------------------------------------------------------------------
# resolve_venue -- the name guard and roof rule
# --------------------------------------------------------------------------------------


def test_open_venue_with_matching_name_is_fetched():
    d = resolve_venue(_game(), _stadium())
    assert d.fetch and d.skip_reason is None and not d.roof_conflict


def test_mislabeled_game_is_never_fetched_with_the_stored_coords():
    # 2026_05_PHI_JAX: nflverse says JAX00 but "Tottenham Hotspur Stadium"
    game = _game(game_id="2026_05_PHI_JAX", stadium_id="JAX00",
                 stadium_name="Tottenham Hotspur Stadium")  # fmt: skip
    jax = _stadium(stadium_id="JAX00", known_names=("EverBank Stadium", "TIAA Bank Stadium"))
    d = resolve_venue(game, jax)
    assert not d.fetch and d.skip_reason == "name_mismatch"


def test_name_guard_runs_before_the_roof_rule():
    # even a fixed-roof row reports the mismatch, not 'fixed_roof'
    d = resolve_venue(_game(stadium_name="Somewhere Else"), _stadium(roof_type="fixed"))
    assert d.skip_reason == "name_mismatch"


def test_any_known_alias_passes_the_guard():
    hou = _stadium(stadium_id="HOU00", known_names=("NRG Stadium", "Reliant Stadium"),
                   roof_type="retractable")  # fmt: skip
    d = resolve_venue(_game(stadium_id="HOU00", stadium_name="Reliant Stadium", roof=None), hou)
    assert d.fetch


def test_missing_stadium_row_is_unknown_stadium():
    assert resolve_venue(_game(), None).skip_reason == "unknown_stadium"


def test_null_games_stadium_is_a_mismatch_not_a_pass():
    assert resolve_venue(_game(stadium_name=None), _stadium()).skip_reason == "name_mismatch"


def test_fixed_roof_is_skipped():
    d = resolve_venue(_game(roof="dome"), _stadium(roof_type="fixed"))
    assert d.skip_reason == "fixed_roof"


def test_retractable_closed_is_skipped_but_null_or_open_is_fetched():
    retractable = _stadium(roof_type="retractable")
    assert resolve_venue(_game(roof="closed"), retractable).skip_reason == "roof_closed"
    assert resolve_venue(_game(roof=None), retractable).fetch
    assert resolve_venue(_game(roof="open"), retractable).fetch


def test_open_venue_labeled_dome_is_fetched_and_flagged():
    # MEL00 / PAR00 / MUN01: nflverse says dome, the structure is open-air
    d = resolve_venue(_game(roof="dome"), _stadium(roof_type="open"))
    assert d.fetch and d.roof_conflict


# --------------------------------------------------------------------------------------
# game_window / parse_forecast -- against the real fixture
# --------------------------------------------------------------------------------------


def test_game_window_is_kickoff_hour_through_plus_four():
    window = game_window(_KICKOFF)
    assert len(window) == WINDOW_HOURS == 5
    assert window[0] == datetime(2026, 9, 25, 0, 0, tzinfo=UTC)
    assert window[-1] == datetime(2026, 9, 25, 4, 0, tzinfo=UTC)


def test_parse_real_fixture():
    parsed = parse_forecast(_body(), game_window(_KICKOFF))
    assert parsed["grid_lat"] == pytest.approx(44.50524)  # grid cell, not the request
    assert parsed["grid_elevation_m"] == 190.0
    first = parsed["hours"][0]
    assert first["valid_time"] == datetime(2026, 9, 25, 0, 0, tzinfo=UTC)
    assert first["temperature_2m_f"] == 59.0
    assert first["wind_speed_10m_mph"] == 3.2
    assert first["wind_gusts_10m_mph"] == 2.5  # gust < speed: stored as returned
    assert first["wind_direction_10m_deg"] == 141
    assert [h["hour_offset"] for h in parsed["hours"]] == [0, 1, 2, 3, 4]


def test_naive_times_are_read_as_utc():
    parsed = parse_forecast(_body(), game_window(_KICKOFF))
    assert all(h["valid_time"].tzinfo is UTC for h in parsed["hours"])


def test_null_required_variable_is_no_forecast_data_not_filled():
    body = _body()
    body["hourly"]["wind_speed_10m"][2] = None
    with pytest.raises(NoForecastData):
        parse_forecast(body, game_window(_KICKOFF))


def test_null_optional_variable_is_stored_as_null():
    body = _body()
    body["hourly"]["wind_gusts_10m"][4] = None
    parsed = parse_forecast(body, game_window(_KICKOFF))
    assert parsed["hours"][4]["wind_gusts_10m_mph"] is None


def test_unit_change_is_schema_drift():
    body = _body()
    body["hourly_units"]["wind_speed_10m"] = "km/h"
    with pytest.raises(ValueError, match="wind_speed_10m"):
        parse_forecast(body, game_window(_KICKOFF))


def test_unexpected_hours_are_schema_drift():
    with pytest.raises(ValueError, match="hours"):
        parse_forecast(_body(), game_window(_KICKOFF.replace(hour=3)))


def test_missing_variable_is_schema_drift():
    body = copy.deepcopy(_body())
    del body["hourly"]["snowfall"]
    with pytest.raises(ValueError, match="snowfall"):
        parse_forecast(body, game_window(_KICKOFF))


# --------------------------------------------------------------------------------------
# build_rows
# --------------------------------------------------------------------------------------


def _target(target_id: str):
    return next(t for t in compute_game_targets("2026_03_ATL_GB", _KICKOFF)
                if t.target_id == target_id)  # fmt: skip


def test_build_rows_records_actual_lead_and_both_locations():
    parsed = parse_forecast(_body(), game_window(_KICKOFF))
    rows = build_rows(_target("t48"), _game(), _stadium(), parsed, _AS_OF)
    assert len(rows) == 5
    row = rows[0]
    assert row["lead_hours"] == pytest.approx(47.9, abs=0.01)  # actual, not the planned 48
    assert row["requested_lat"] == 44.50133 and row["grid_lat"] == pytest.approx(44.50524)
    assert row["stadium_id"] == "GNB00" and row["target_id"] == "t48"
    assert row["content_hash"]


def test_only_t48_rows_carry_the_model_regime_break_flag():
    parsed = parse_forecast(_body(), game_window(_KICKOFF))
    t48 = build_rows(_target("t48"), _game(), _stadium(), parsed, _AS_OF)
    t36 = build_rows(_target("t36"), _game(), _stadium(), parsed, _AS_OF)
    assert all(r["model_regime_break"] for r in t48)
    assert not any(r["model_regime_break"] for r in t36)
