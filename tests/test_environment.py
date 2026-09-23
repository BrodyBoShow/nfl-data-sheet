from datetime import UTC, datetime, timedelta

import pytest

from pipeline.analysts import environment as env
from pipeline.analysts.environment import (
    Game,
    Snapshot,
    Venue,
    build_rows,
    haversine_miles,
    home_venues,
    in_hrrr_domain,
    in_window,
    pick_snapshot,
    surface_code,
    team_values,
    tz_offset_diff_hours,
    weather_status,
    weather_values,
    wind_components,
    wrap_tz_shift,
)
from pipeline.collectors.stadiums import StadiumsCollector
from pipeline.core.venue import VenueDecision

NOW = datetime(2026, 9, 26, 12, 0, tzinfo=UTC)  # a Saturday
KICKOFF = datetime(2026, 9, 27, 17, 0, tzinfo=UTC)  # Sunday 1pm ET

GNB = Venue("GNB00", ("Lambeau Field",), 44.50133, -88.06222, "open", 179.8, "America/Chicago")
BUF = Venue("BUF00", ("Highmark Stadium",), 42.77304, -78.79219, "open", None, "America/New_York")
DET = Venue("DET00", ("Ford Field",), 42.33993, -83.04557, "fixed", None, "America/Detroit")
ATL = Venue(
    "ATL97", ("Mercedes-Benz Stadium",), 33.75542, -84.40085, "retractable", None,
    "America/New_York",
)  # fmt: skip
SFO = Venue(
    "SFO01", ("Levi's Stadium",), 37.40318, -121.96980, "open", 152.0, "America/Los_Angeles"
)
LAX = Venue("LAX01", ("SoFi Stadium",), 33.95340, -118.33900, "fixed", None, "America/Los_Angeles")
PHO = Venue(
    "PHO00", ("State Farm Stadium",), 33.52778, -112.26267, "retractable", 148.2, "America/Phoenix"
)
MUN = Venue(
    "MUN01", ("FC Bayern Munich Stadium",), 48.21879, 11.62470, "open", None, "Europe/Berlin"
)
MEL = Venue(
    "MEL00", ("Melbourne Cricket Ground",), -37.81998, 144.98344, "open", None,
    "Australia/Melbourne",
)  # fmt: skip
LON = Venue(
    "LON02", ("Tottenham Hotspur Stadium",), 51.60421, -0.06623, "open", None, "Europe/London"
)
NYC = Venue("NYC01", ("MetLife Stadium",), 40.81353, -74.07436, "open", 166.5, "America/New_York")
VENUES = {v.stadium_id: v for v in (GNB, BUF, DET, ATL, SFO, LAX, PHO, MUN, MEL, LON, NYC)}


def _game(**kw) -> Game:
    base = dict(
        game_id="2026_04_ATL_GB",
        season=2026,
        week=4,
        kickoff=KICKOFF,
        home_team="GB",
        away_team="ATL",
        home_rest=7,
        away_rest=10,
        roof="outdoors",
        surface="grass",
        stadium_id="GNB00",
        stadium_name="Lambeau Field",
    )
    return Game(**{**base, **kw})


def _hours(speed: float | list[float] = 5.0, direction=90, **overrides) -> tuple[dict, ...]:
    """Five forecast hours (offsets 0..4). `speed`/`direction` may be a list per hour."""
    speeds = speed if isinstance(speed, list) else [speed] * 5
    dirs = direction if isinstance(direction, list) else [direction] * 5
    hours = []
    for i in range(5):
        h = {
            "hour_offset": i,
            "temperature_2m_f": 60.0 + i,
            "apparent_temperature_f": 58.0 + i,
            "precipitation_in": 0.01,
            "precipitation_probability_pct": 10 * i,
            "snowfall_in": 0.0,
            "wind_speed_10m_mph": speeds[i],
            "wind_gusts_10m_mph": speeds[i] + 5,
            "wind_direction_10m_deg": dirs[i],
        }
        for col, per_hour in overrides.items():
            h[col] = per_hour[i]
        hours.append(h)
    return tuple(hours)


def _snap(as_of=None, lead=20.0, t48=False, hours=None, elev: float | None = 190.0) -> Snapshot:
    as_of = as_of or KICKOFF - timedelta(hours=lead)
    return Snapshot(as_of, lead, t48, elev, hours or _hours())


def _values(snapshot, bearing) -> dict[str, tuple[float, int | None]]:
    return {s: (v, n) for s, v, n in weather_values(snapshot, bearing)}


FETCH = VenueDecision(True)


# --------------------------------------------------------------------------------------
# Window
# --------------------------------------------------------------------------------------


def test_window_spans_dispatcher_weeks_and_keeps_a_day_after_kickoff():
    assert in_window(NOW + timedelta(days=6), NOW)  # next week's Thursday game
    assert not in_window(NOW + timedelta(days=7, minutes=1), NOW)
    assert in_window(NOW - timedelta(hours=23), NOW)
    assert not in_window(NOW - timedelta(hours=24), NOW)


# --------------------------------------------------------------------------------------
# Snapshot choice
# --------------------------------------------------------------------------------------


def test_headline_is_latest_pre_kickoff_snapshot():
    t36, t12 = _snap(lead=36), _snap(lead=12)
    post = _snap(lead=-0.5)  # a t2 capture after kickoff
    assert pick_snapshot([t36, post, t12]) is t12


def test_t48_alone_is_used_and_flagged():
    t48 = _snap(lead=47.5, t48=True)
    assert pick_snapshot([t48]) is t48
    assert _values(t48, None)["weather_model_regime_break"][0] == 1.0


def test_only_post_kickoff_capture_means_no_headline():
    assert pick_snapshot([_snap(lead=-0.2)]) is None


# --------------------------------------------------------------------------------------
# weather_status -- dome vs. missed vs. never tracked are distinguishable
# --------------------------------------------------------------------------------------


def test_status_forecast():
    assert weather_status(FETCH, [("captured", None)], _snap(), KICKOFF, NOW) == 1.0


def test_dome_is_indoor_even_with_no_target_rows():
    decision = VenueDecision(False, "fixed_roof")
    assert weather_status(decision, [], None, KICKOFF - timedelta(days=3), NOW) == 2.0


def test_retractable_closed_via_target_skip_is_indoor():
    targets = [("captured", None), ("skipped", "roof_closed")]
    assert weather_status(FETCH, targets, _snap(), KICKOFF, NOW) == 2.0


def test_awaiting_while_targets_pending_or_beyond_horizon():
    assert weather_status(FETCH, [("pending", None), ("missed", None)], None, KICKOFF, NOW) == 3.0
    assert weather_status(FETCH, [], None, KICKOFF + timedelta(days=4), NOW) == 3.0


def test_missed_when_every_target_closed_uncaptured():
    targets = [("missed", None)] * 5 + [("skipped", "no_forecast_data")] * 2
    assert weather_status(FETCH, targets, None, KICKOFF, NOW) == 4.0


def test_missed_once_kickoff_passes_even_if_t2_still_pending():
    later = KICKOFF + timedelta(minutes=30)
    assert weather_status(FETCH, [("pending", None)], None, KICKOFF, later) == 4.0


def test_venue_unresolved_beats_everything():
    decision = VenueDecision(False, "name_mismatch")
    assert weather_status(decision, [("captured", None)], _snap(), KICKOFF, NOW) == 5.0


def test_not_tracked_when_no_targets_and_kickoff_passed():
    assert weather_status(FETCH, [], None, NOW - timedelta(hours=3), NOW) == 6.0


# --------------------------------------------------------------------------------------
# Wind shapes: speed-only is the base, the split is an add-on
# --------------------------------------------------------------------------------------


def test_no_bearing_is_speed_only_even_in_strong_wind():
    v = _values(_snap(hours=_hours(speed=20.0)), None)
    assert v["wind_direction_mode"][0] == 2.0
    assert v["wind_speed_mph"] == (20.0, 5)
    assert "wind_along_field_mph" not in v and "wind_crosswind_mph" not in v


def test_light_wind_with_bearing_is_speed_only():
    v = _values(_snap(hours=_hours(speed=[2, 3, 5, 7.9, 4])), 179.8)
    assert v["wind_direction_mode"][0] == 1.0
    assert "wind_along_field_mph" not in v


def test_gusts_do_not_lift_an_hour_over_the_threshold():
    hours = _hours(speed=3.0, wind_gusts_10m_mph=[None, 20, 20, 20, 20])
    assert _values(_snap(hours=hours), 0.0)["wind_direction_mode"][0] == 1.0


def test_split_uses_only_qualifying_hours():
    # 3 of 5 hours >= 8 mph, blowing straight across a N-S field.
    hours = _hours(speed=[4, 10, 12, 14, 6], direction=90)
    v = _values(_snap(hours=hours), 0.0)
    assert v["wind_direction_mode"][0] == 3.0
    assert v["wind_crosswind_mph"] == (pytest.approx(12.0), 3)
    assert v["wind_along_field_mph"][0] == pytest.approx(0.0, abs=1e-9)
    assert v["wind_speed_mph"] == (pytest.approx(9.2), 5)  # the base shape is unchanged


def test_components_are_symmetric_along_the_field_axis():
    a1, c1 = wind_components(10, 0, 0)
    a2, c2 = wind_components(10, 180, 0)
    assert (a1, c1) == pytest.approx((a2, c2), abs=1e-9)
    assert a1 == pytest.approx(10)
    a, c = wind_components(10, 45, 0)
    assert a == pytest.approx(c)


# --------------------------------------------------------------------------------------
# Other weather values: nothing from a partial window
# --------------------------------------------------------------------------------------


def test_aggregation_windows():
    v = _values(_snap(), None)
    assert v["temperature_f"] == (pytest.approx(62.0), 5)  # offsets 0..4
    assert v["precip_total_in"] == (pytest.approx(0.04), 4)  # offsets 1..4 only
    assert v["precip_prob_max_pct"] == (40.0, 4)
    assert v["wind_gust_max_mph"] == (10.0, 4)
    assert v["weather_lead_hours"][0] == 20.0
    assert v["venue_elevation_m"][0] == 190.0


def test_null_gusts_everywhere_omits_the_gust_row():
    hours = _hours(wind_gusts_10m_mph=[None] * 5)
    assert "wind_gust_max_mph" not in _values(_snap(hours=hours), None)


def test_partial_precip_nulls_omit_the_sum_but_keep_the_rest():
    hours = _hours(precipitation_in=[0.0, 0.1, None, 0.1, 0.1])
    v = _values(_snap(hours=hours), None)
    assert "precip_total_in" not in v
    assert "snowfall_total_in" in v and "wind_speed_mph" in v


def test_null_precip_before_kickoff_hour_does_not_matter():
    hours = _hours(precipitation_in=[None, 0.1, 0.1, 0.1, 0.1])
    assert _values(_snap(hours=hours), None)["precip_total_in"][0] == pytest.approx(0.4)


def test_missing_elevation_omits_the_row():
    assert "venue_elevation_m" not in _values(_snap(elev=None), None)


# --------------------------------------------------------------------------------------
# HRRR domain -- against every real stadiums row
# --------------------------------------------------------------------------------------


def test_hrrr_domain_matches_us_vs_international_for_every_stadium():
    from pipeline.collectors.stadiums import CSV_PATH

    rows = StadiumsCollector().validate(CSV_PATH.read_text(encoding="utf-8"))["rows"]
    international = {
        "FRA00", "GER00", "LON00", "LON02", "MAD01", "MEL00", "MEX00", "MUN01", "PAR00",
        "RIO00", "SAO00",
    }  # fmt: skip
    for r in rows:
        assert in_hrrr_domain(r.lat, r.lon) == (r.stadium_id not in international), r.stadium_id


def test_hrrr_projection_reproduces_noaa_corner_points():
    # HRRR_conus.domain.txt's mass-point corners sit exactly on the grid's edge: a nudge
    # toward the grid center is inside, a nudge outward is outside.
    sw_lat, sw_lon = 21.13812, -122.7195
    assert in_hrrr_domain(sw_lat + 0.3, sw_lon + 0.3)
    assert not in_hrrr_domain(sw_lat - 0.05, sw_lon)
    ne_lat, ne_lon = 47.84364, -60.90137
    assert in_hrrr_domain(ne_lat - 0.3, ne_lon - 0.3)
    assert not in_hrrr_domain(ne_lat + 0.05, ne_lon)
    assert in_hrrr_domain(38.5, -97.5)


# --------------------------------------------------------------------------------------
# Surface
# --------------------------------------------------------------------------------------


def test_surface_codes():
    assert surface_code("grass") == 1.0
    assert surface_code("a_turf") == 2.0
    assert surface_code("") is None
    assert surface_code(None) is None
    assert surface_code("hybrid_mystery") is None


# --------------------------------------------------------------------------------------
# Rest / travel / timezone
# --------------------------------------------------------------------------------------


def _team(values, team, signal):
    return next(v for t, s, v in values if t == team and s == signal)


HOME = {(2026, "GB"): "GNB00", (2026, "ATL"): "ATL97", (2026, "SF"): "SFO01",
        (2026, "LA"): "LAX01", (2026, "ARI"): "PHO00", (2026, "DET"): "DET00",
        (2026, "NYG"): "NYC01", (2026, "BUF"): "BUF00"}  # fmt: skip


def test_rest_days_and_diff():
    v = team_values(_game(), GNB, HOME, VENUES)
    assert _team(v, "GB", "rest_days") == 7.0
    assert _team(v, "GB", "rest_diff") == -3.0
    assert _team(v, "ATL", "rest_diff") == 3.0


def test_home_team_at_home_has_zero_travel_and_shift():
    v = team_values(_game(), GNB, HOME, VENUES)
    assert _team(v, "GB", "travel_miles") == 0.0
    assert _team(v, "GB", "tz_shift_hours") == 0.0
    assert _team(v, "ATL", "tz_shift_hours") == -1.0  # ET -> CT, traveled west


def test_west_coast_team_at_1pm_et_is_plus_three():
    game = _game(
        home_team="BUF", away_team="SF", stadium_id="BUF00", stadium_name="Highmark Stadium"
    )
    v = team_values(game, BUF, HOME, VENUES)
    assert _team(v, "SF", "tz_shift_hours") == 3.0
    assert _team(v, "SF", "travel_miles") == pytest.approx(
        haversine_miles(SFO.lat, SFO.lon, BUF.lat, BUF.lon)
    )
    assert 2200 < _team(v, "SF", "travel_miles") < 2400


def test_arizona_dst_is_resolved_per_kickoff_date():
    sept = datetime(2026, 9, 27, 20, 0, tzinfo=UTC)
    dec = datetime(2026, 12, 13, 21, 0, tzinfo=UTC)
    assert tz_offset_diff_hours("America/Los_Angeles", "America/Phoenix", sept) == 0.0
    assert tz_offset_diff_hours("America/Los_Angeles", "America/Phoenix", dec) == 1.0


def test_international_trip_is_full_magnitude_for_both_teams():
    # DET's "home" game at Munich: DET travels too. Early November (both zones on
    # standard time): CET +1 vs EST -5 = +6; vs NYG likewise.
    game = _game(
        game_id="2026_09_NYG_DET", kickoff=datetime(2026, 11, 8, 14, 30, tzinfo=UTC),
        home_team="DET", away_team="NYG", stadium_id="MUN01",
        stadium_name="FC Bayern Munich Stadium",
    )  # fmt: skip
    v = team_values(game, MUN, HOME, VENUES)
    assert _team(v, "DET", "tz_shift_hours") == 6.0
    assert _team(v, "NYG", "tz_shift_hours") == 6.0
    assert _team(v, "DET", "travel_miles") > 4000
    assert _team(v, "NYG", "travel_miles") > 3900


def test_london_trip_in_october():
    # Oct 11: BST +1 vs EDT -4.
    game = _game(
        kickoff=datetime(2026, 10, 11, 13, 30, tzinfo=UTC), home_team="NYG", away_team="GB",
        stadium_id="LON02", stadium_name="Tottenham Hotspur Stadium",
    )  # fmt: skip
    v = team_values(game, LON, HOME, VENUES)
    assert _team(v, "NYG", "tz_shift_hours") == 5.0
    assert _team(v, "GB", "tz_shift_hours") == 6.0


def test_melbourne_wraps_to_minus_seven_and_keeps_raw_plus_seventeen():
    # September: AEST +10 (no DST yet) vs PDT -7.
    game = _game(
        kickoff=datetime(2026, 9, 10, 10, 0, tzinfo=UTC), home_team="LA", away_team="SF",
        stadium_id="MEL00", stadium_name="Melbourne Cricket Ground",
    )  # fmt: skip
    v = team_values(game, MEL, HOME, VENUES)
    assert _team(v, "LA", "tz_offset_diff_raw_hours") == 17.0
    assert _team(v, "LA", "tz_shift_hours") == -7.0


def test_wrap_never_clips_magnitude_within_range():
    assert wrap_tz_shift(9.0) == 9.0
    assert wrap_tz_shift(-11.0) == -11.0
    assert wrap_tz_shift(17.0) == -7.0
    assert wrap_tz_shift(-15.0) == 9.0


def test_unresolved_venue_gets_rest_but_no_travel_or_tz():
    v = team_values(_game(), None, HOME, VENUES)
    signals = {s for _, s, _ in v}
    assert signals == {"rest_days", "rest_diff"}


def test_home_venue_is_the_modal_home_stadium_and_ties_are_not_guessed():
    counts = [
        (2026, "DET", "DET00", 8), (2026, "DET", "MUN01", 1),
        (2026, "XXX", "AAA00", 4), (2026, "XXX", "BBB00", 4),
    ]  # fmt: skip
    venues, ties = home_venues(counts)
    assert venues == {(2026, "DET"): "DET00"}
    assert [t["team"] for t in ties] == ["XXX"]


# --------------------------------------------------------------------------------------
# build_rows: per-game scope, the game's own week, weather only with a forecast
# --------------------------------------------------------------------------------------


def _build(games, targets=None, snapshots=None):
    return build_rows(
        games=games,
        venues=VENUES,
        targets=targets or {},
        snapshots=snapshots or {},
        home_venue_ids=HOME,
        now=NOW,
        base_version="schedules@x,stadiums_csv@y",
    )


def _by_game(rows, game_id):
    return {(r["team"], r["signal"]): r for r in rows if r["game_id"] == game_id}


def test_rows_carry_the_games_own_week_not_the_dispatchers():
    thursday = _game(game_id="2026_05_ATL_GB", week=5, kickoff=NOW + timedelta(days=5))
    rows, _ = _build([thursday])
    assert {r["week"] for r in rows} == {5}


def test_forecast_game_gets_weather_rows_with_snapshot_version():
    snap = _snap(lead=31.05)
    rows, meta = _build([_game()], {"2026_04_ATL_GB": [("captured", None)]},
                        {"2026_04_ATL_GB": [snap]})  # fmt: skip
    g = _by_game(rows, "2026_04_ATL_GB")
    assert g[(None, "weather_status")]["value"] == 1.0
    assert g[(None, "wind_direction_mode")]["value"] == 1.0  # Lambeau, 5 mph
    assert g[(None, "weather_forecast_domain")]["value"] == 1.0
    assert g[(None, "venue_roof_code")]["value"] == 4.0
    assert g[(None, "wind_speed_mph")]["inputs_version"].startswith("weather@")
    assert g[(None, "weather_status")]["inputs_version"] == "schedules@x,stadiums_csv@y"
    assert g[("GB", "rest_days")]["game_id"] == "2026_04_ATL_GB"
    assert meta["weather_status_counts"] == {"1": 1}


def test_dome_game_gets_status_and_venue_but_no_weather_rows():
    game = _game(game_id="2026_04_GB_DET", home_team="DET", away_team="GB", stadium_id="DET00",
                 stadium_name="Ford Field", roof="dome", surface="fieldturf")  # fmt: skip
    rows, _ = _build([game])
    g = _by_game(rows, "2026_04_GB_DET")
    assert g[(None, "weather_status")]["value"] == 2.0
    assert g[(None, "venue_roof_code")]["value"] == 1.0
    assert not {s for _, s in g} & env._WEATHER_SIGNALS


def test_retractable_with_null_roof_is_labeled_if_open():
    game = _game(game_id="2026_04_GB_ATL", home_team="ATL", away_team="GB", stadium_id="ATL97",
                 stadium_name="Mercedes-Benz Stadium", roof=None)  # fmt: skip
    rows, _ = _build([game])
    assert _by_game(rows, "2026_04_GB_ATL")[(None, "venue_roof_code")]["value"] == 3.0


def test_name_mismatch_game_is_unresolved_with_no_venue_travel_or_weather():
    game = _game(game_id="2026_05_PHI_JAX", stadium_id="GNB00",
                 stadium_name="Tottenham Hotspur Stadium")  # fmt: skip
    rows, meta = _build([game], snapshots={"2026_05_PHI_JAX": [_snap()]})
    g = _by_game(rows, "2026_05_PHI_JAX")
    assert g[(None, "weather_status")]["value"] == 5.0
    signals = {s for _, s in g}
    assert "venue_roof_code" not in signals and "travel_miles" not in signals
    assert not signals & env._WEATHER_SIGNALS
    assert "surface_code" in signals and "rest_days" in signals
    assert meta["unresolved_venues"] == ["2026_05_PHI_JAX"]


def test_every_emitted_signal_is_registered():
    rows, _ = _build([_game()], {"2026_04_ATL_GB": [("captured", None)]},
                     {"2026_04_ATL_GB": [_snap(hours=_hours(speed=12.0))]})  # fmt: skip
    assert {r["signal"] for r in rows} <= env._SIGNAL_NAMES
    assert all(r["sector"] == "environment" for r in rows)


# --------------------------------------------------------------------------------------
# Stale-row cleanup is scoped by game, not by ctx.week
# --------------------------------------------------------------------------------------


def test_stale_delete_is_scoped_to_this_runs_game_ids(monkeypatch):
    calls = []
    monkeypatch.setattr(env, "delete_rows", lambda conn, table, where, params: calls.append(
        (table, where, params)) or 0)  # fmt: skip
    analyst = env.EnvironmentAnalyst()
    analyst._game_ids = ["2026_04_ATL_GB", "2026_05_ATL_GB"]

    class Ctx:
        conn = None
        season, week = 2026, 3

    analyst._delete_stale_signals(Ctx())  # type: ignore[arg-type]
    ((table, where, params),) = calls
    assert table == "signals"
    assert "game_id = ANY" in where and "week" not in where
    assert params[0] == "environment"
    assert params[2] == ["2026_04_ATL_GB", "2026_05_ATL_GB"]


def test_stale_delete_with_no_games_deletes_nothing(monkeypatch):
    monkeypatch.setattr(env, "delete_rows", lambda *a: pytest.fail("should not delete"))
    assert env.EnvironmentAnalyst()._delete_stale_signals(None) == 0  # type: ignore[arg-type]
