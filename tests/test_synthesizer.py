"""P5 matchup synthesizer (pipeline/synthesis/synthesizer.py). Pure build_cards tests on
hand-built rows plus a fake-connection write test -- no DB, no live calls."""

from __future__ import annotations

import datetime as dt
import json
import re
from typing import Any

import pytest

from pipeline.synthesis import synthesizer as syn
from pipeline.synthesis.model import BucketCalibration, ModelFile
from pipeline.synthesis.synthesizer import (
    GAMES_COLUMNS,
    STATUS_AWAITING_EFFICIENCY,
    STATUS_IN_SAMPLE,
    STATUS_INPUT_NULL,
    STATUS_MODEL_STALE,
    STATUS_PROJECTED,
    WindowGame,
    build_cards,
    compute_edge,
    market_view,
    pair_unit_signals,
)

UTC = dt.UTC
KICKOFF = dt.datetime(2026, 9, 27, 17, 0, tzinfo=UTC)
FP = "fingerprint-ok"


def _model(**overrides: Any) -> ModelFile:
    base = dict(
        model_version="p5-v1",
        spec_name="epa_per_play",
        bases=("epa_per_play",),
        fit_seasons=(2019, 2020, 2021, 2022, 2023, 2024, 2025),
        efficiency_fingerprint=FP,
        coef={"alpha": 22.0, "beta_off:epa_per_play": 36.0,
              "beta_def:epa_per_play": 20.0, "gamma": 1.5},
        stability_cutpoints=(0.4, 0.7),
        buckets={b: BucketCalibration(13.0, 13.5, False, False)
                 for b in ("low", "mid", "high")},
    )
    base.update(overrides)
    return ModelFile(**base)  # type: ignore[arg-type]


def _game(**overrides: Any) -> WindowGame:
    base = dict(game_id="2026_03_KC_BUF", season=2026, week=3, home_team="BUF",
                away_team="KC", kickoff=KICKOFF, location="Home")
    base.update(overrides)
    return WindowGame(**base)  # type: ignore[arg-type]


def _eff(season: int = 2026, week: int = 3, stability: float = 0.8) -> list[dict]:
    values = {"BUF": (0.10, -0.05), "KC": (0.05, 0.02), "MIA": (-0.05, 0.03),
              "NYJ": (-0.10, 0.00)}
    rows = []
    for team, (off, def_) in values.items():
        for signal, v in (("epa_per_play_off", off), ("epa_per_play_def", def_),
                          ("success_rate_off", 0.45), ("success_rate_def", 0.44)):
            rows.append({"game_id": None, "season": season, "week": week, "team": team,
                         "player_id": None, "sector": "efficiency", "signal": signal,
                         "value": v, "sample_n": 120, "stability": stability,
                         "inputs_version": "pbp@x"})
    return rows


def _market(game_id: str = "2026_03_KC_BUF", status: float = 2.0, spread: float = -2.5,
            total: float = 47.5, spread_range: float | None = 1.0) -> list[dict]:
    rows = [("market_status", status), ("spread_home_current", spread),
            ("total_current", total), ("spread_key_straddle", 0.0),
            ("total_book_range", 1.0)]
    if spread_range is not None:
        rows.append(("spread_book_range", spread_range))
    return [{"game_id": game_id, "season": 2026, "week": 3, "team": None, "player_id": None,
             "sector": "market", "signal": s, "value": v, "sample_n": 8, "stability": None,
             "inputs_version": "odds_current@t"} for s, v in rows]


def _build(now: dt.datetime, *, games: list[WindowGame] | None = None,
           eff: list[dict] | None = None, market: list[dict] | None = None,
           model: ModelFile | None = None, fingerprint: str = FP,
           locked: dict | None = None) -> syn.SynthesisResult:
    return build_cards(
        games=games if games is not None else [_game()],
        efficiency_rows=_eff() if eff is None else eff,
        market_rows=_market() if market is None else market,
        environment_rows=[],
        availability_rows=[],
        model=model or _model(),
        live_fingerprint=fingerprint,
        locked=locked or {},
        now=now,
    )


def _card(result: syn.SynthesisResult) -> dict:
    assert len(result.cards) == 1
    return json.loads(result.cards[0]["card"])


# --- layer rule --------------------------------------------------------------------------


def test_games_sql_names_only_the_allowed_columns():
    select = re.match(r"SELECT (.*) FROM games", syn._GAMES_SQL)
    assert select is not None
    assert tuple(c.strip() for c in select.group(1).split(",")) == GAMES_COLUMNS
    assert GAMES_COLUMNS == ("game_id", "season", "week", "home_team", "away_team",
                             "gameday", "gametime", "location")
    for forbidden in ("score", "spread_line", "total_line", "result", "season_type"):
        assert forbidden not in syn._GAMES_SQL


# --- projection and status ----------------------------------------------------------------


def test_projects_and_decomposes():
    card = _card(_build(KICKOFF - dt.timedelta(days=2)))
    assert card["projection_status"] == STATUS_PROJECTED
    proj = card["projection"]
    home = proj["decomposition"]["home"]
    assert home["points"] == pytest.approx(
        home["alpha"] + home["hfa"] + sum(t["contribution"] for t in home["terms"])
    )
    assert proj["spread_home"] == pytest.approx(-(proj["pts_home"] - proj["pts_away"]))
    assert home["hfa"] == pytest.approx(0.75)  # gamma * 0.5
    assert card["uncertainty"]["edge_validated"] == {"spread": False, "total": False}
    assert card["uncertainty"]["edge_note"]["spread"] == syn.EDGE_NOT_VALIDATED_NOTE


def test_outcome_noise_is_stated_once_not_as_a_per_game_band():
    card = _card(_build(KICKOFF - dt.timedelta(days=2)))
    unc = card["uncertainty"]
    assert "spread_band" not in unc and "total_band" not in unc
    assert unc["outcome_noise"] == {"margin_rms": 13.0, "total_rms": 13.5,
                                    "note": syn.OUTCOME_NOISE_NOTE}


def test_straddle_flag_carries_the_common_note():
    straddle = [{**r, "value": 1.0} if r["signal"] == "spread_key_straddle" else r
                for r in _market()]
    edge = compute_edge(-4.0, 45.0, market_view(straddle), "BUF", "KC")
    assert edge["flags"]["spread_key_straddle"]
    assert edge["flag_notes"]["spread_key_straddle"] == syn.STRADDLE_COMMON_NOTE
    plain = compute_edge(-4.0, 45.0, market_view(_market()), "BUF", "KC")
    assert plain["flag_notes"]["spread_key_straddle"] is None


def test_neutral_site_has_no_hfa():
    card = _card(_build(KICKOFF - dt.timedelta(days=2), games=[_game(location="Neutral")]))
    assert card["identity"]["neutral"]
    assert card["projection"]["decomposition"]["home"]["hfa"] == 0.0


def test_model_stale_blocks_projection_and_lock():
    result = _build(KICKOFF - dt.timedelta(hours=3), fingerprint="changed")
    card = _card(result)
    assert card["projection_status"] == STATUS_MODEL_STALE
    assert card["projection"] is None
    assert result.locks == []
    assert result.meta["model_stale"]


def test_in_sample_season_never_locks():
    game = _game(game_id="2025_03_KC_BUF", season=2025)
    result = _build(KICKOFF - dt.timedelta(hours=3), games=[game], eff=_eff(season=2025),
                    market=_market("2025_03_KC_BUF"))
    assert _card(result)["projection_status"] == STATUS_IN_SAMPLE
    assert result.locks == []


def test_awaiting_efficiency_has_no_fallback_to_older_weeks():
    result = _build(KICKOFF - dt.timedelta(hours=3), eff=_eff(week=2))
    assert _card(result)["projection_status"] == STATUS_AWAITING_EFFICIENCY
    assert result.locks == []


def test_missing_team_signal_is_input_null():
    eff = [r for r in _eff() if not (r["team"] == "KC" and r["signal"] == "epa_per_play_def")]
    assert _card(_build(KICKOFF - dt.timedelta(days=1), eff=eff))["projection_status"] == (
        STATUS_INPUT_NULL
    )


def test_missing_location_is_input_null():
    card = _card(_build(KICKOFF - dt.timedelta(days=1), games=[_game(location=None)]))
    assert card["projection_status"] == STATUS_INPUT_NULL


def test_postseason_week_is_flagged_outside_fit_scope():
    game = _game(game_id="2026_19_KC_BUF", week=19)
    card = _card(_build(KICKOFF - dt.timedelta(days=1), games=[game], eff=_eff(week=19),
                        market=_market("2026_19_KC_BUF")))
    assert card["identity"]["outside_fit_scope"]
    assert card["projection_status"] == STATUS_PROJECTED


# --- lock window --------------------------------------------------------------------------


def test_locks_exactly_at_t_minus_6h():
    result = _build(KICKOFF - dt.timedelta(hours=6))
    assert [lk["game_id"] for lk in result.locks] == ["2026_03_KC_BUF"]
    lock = result.locks[0]
    assert lock["lock_lead_hours"] == pytest.approx(6.0)
    assert lock["market_spread"] == -2.5
    assert lock["stability_bucket"] == "high"
    assert json.loads(lock["card"]) == _card(result)
    assert _card(result)["lock"]["locked"]


def test_no_lock_just_before_the_window():
    result = _build(KICKOFF - dt.timedelta(hours=6, seconds=1))
    assert result.locks == []
    assert not _card(result)["lock"]["locked"]


def test_nothing_written_at_or_after_kickoff():
    result = _build(KICKOFF)
    assert result.cards == [] and result.locks == []
    assert result.meta["lock_missed"] == ["2026_03_KC_BUF"]
    assert result.meta["frozen_past_kickoff"] == 1


def test_run_after_lock_keeps_the_locked_projection():
    first = _build(KICKOFF - dt.timedelta(hours=5))
    lock = first.locks[0]
    stored = {"2026_03_KC_BUF": {
        "locked_at": lock["locked_at"], "kickoff": lock["kickoff"],
        "lock_lead_hours": lock["lock_lead_hours"], "card": json.loads(lock["card"]),
        "inputs_version": lock["inputs_version"], "edge_spread": lock["edge_spread"],
        "edge_total": lock["edge_total"], "market_spread": lock["market_spread"],
        "market_total": lock["market_total"],
    }}
    # Efficiency and market both changed since the lock.
    changed_eff = [{**r, "value": r["value"] * 3} if r["signal"].startswith("epa") else r
                   for r in _eff()]
    second = _build(KICKOFF - dt.timedelta(hours=1), eff=changed_eff,
                    market=_market(spread=-6.0), locked=stored)
    assert second.locks == []
    card = _card(second)
    assert card["projection"] == json.loads(lock["card"])["projection"]
    assert card["edge"]["at_lock"]["market_spread"] == -2.5
    assert card["edge"]["vs_current"]["market_spread"] == -6.0
    assert card["edge"]["vs_current"]["spread"] == pytest.approx(lock["projected_spread"] + 6.0)


def test_locks_without_a_market():
    result = _build(KICKOFF - dt.timedelta(hours=2), market=[])
    assert len(result.locks) == 1
    lock = result.locks[0]
    assert lock["market_status"] is None
    assert lock["market_spread"] is None and lock["edge_spread"] is None
    assert lock["projected_spread"] is not None


# --- market and edges ---------------------------------------------------------------------


@pytest.mark.parametrize("status,has_edge", [(1.0, True), (2.0, True), (3.0, True),
                                             (4.0, False), (5.0, False)])
def test_edge_by_market_status(status: float, has_edge: bool):
    edge = compute_edge(-4.0, 45.0, market_view(_market(status=status)), "BUF", "KC")
    assert (edge["spread"] is not None) is has_edge
    assert edge["flags"]["market_lookahead_only"] is (status == 3.0)


def test_edge_sign_and_summary():
    edge = compute_edge(-4.0, 45.0, market_view(_market(spread=-2.5)), "BUF", "KC")
    assert edge["spread"] == pytest.approx(-1.5)
    assert edge["summary"] == "model favors BUF by 1.5 more than market"
    edge = compute_edge(1.0, 45.0, market_view(_market(spread=-2.5)), "BUF", "KC")
    assert edge["summary"] == "model favors KC by 3.5 more than market"


def test_book_range_flags():
    inside = compute_edge(-3.0, 47.5, market_view(_market(spread=-2.5, spread_range=1.0)),
                          "BUF", "KC")
    assert inside["flags"]["spread_within_book_range"]
    outside = compute_edge(-4.0, 47.5, market_view(_market(spread=-2.5, spread_range=1.0)),
                           "BUF", "KC")
    assert not outside["flags"]["spread_within_book_range"]
    single = compute_edge(-4.0, 47.5, market_view(_market(spread_range=None)), "BUF", "KC")
    assert single["flags"]["single_book_market"]
    assert not single["flags"]["spread_within_book_range"]


# --- generic pairing (P7 shape) -----------------------------------------------------------


def test_pair_unit_signals_team_subject():
    pairs = pair_unit_signals(_eff(), {"team": "BUF", "player_id": None},
                              {"team": "KC", "player_id": None},
                              exclude_bases=("epa_per_play",))
    assert [p["base"] for p in pairs] == ["success_rate"]
    assert pairs[0]["subject"]["team"] == "BUF"
    assert pairs[0]["opponent"]["signal"] == "success_rate_def"
    assert pairs[0]["opponent"]["team"] == "KC"


def test_pair_unit_signals_player_subject_with_custom_suffix():
    rows = [
        {"team": "KC", "player_id": "00-1", "signal": "pass_epa_per_target_off",
         "value": 0.3},
        {"team": "BUF", "player_id": None, "signal": "pass_epa_per_target_allowed_def",
         "value": 0.1},
        {"team": "KC", "player_id": "00-1", "signal": "rush_gap_a_off", "value": 0.0},
    ]
    pairs = pair_unit_signals(rows, {"team": "KC", "player_id": "00-1"},
                              {"team": "BUF", "player_id": None},
                              opp_suffix="_allowed_def")
    by_base = {p["base"]: p for p in pairs}
    assert by_base["pass_epa_per_target"]["opponent"]["value"] == 0.1
    assert by_base["rush_gap_a"]["opponent"] is None  # no counterpart: not filled


# --- write --------------------------------------------------------------------------------


class _FakeCursor:
    def __init__(self, conflicts: set[str]) -> None:
        self.conflicts = conflicts
        self.rowcount = 0

    def __enter__(self) -> _FakeCursor:
        return self

    def __exit__(self, *exc: Any) -> None:
        return None

    def execute(self, sql: str, params: dict) -> None:
        assert "ON CONFLICT (game_id) DO NOTHING" in sql
        self.rowcount = 0 if params["game_id"] in self.conflicts else 1


class _FakeConn:
    def __init__(self, conflicts: set[str]) -> None:
        self.conflicts = conflicts

    def cursor(self) -> _FakeCursor:
        return _FakeCursor(self.conflicts)


def _write(result: syn.SynthesisResult, conflicts: set[str], monkeypatch) -> tuple:
    written: list[dict] = []
    monkeypatch.setattr(syn, "filter_changed", lambda conn, table, pk, rows: rows)

    def fake_upsert(conn, table, rows, conflict_cols, update_cols):
        written.extend(rows)
        return len(rows)

    monkeypatch.setattr(syn, "upsert_rows", fake_upsert)
    ctx: Any = type("Ctx", (), {"conn": _FakeConn(conflicts)})()
    return syn.MatchupSynthesizer().write(ctx, result), written


def test_write_inserts_lock_and_card(monkeypatch):
    result = _build(KICKOFF - dt.timedelta(hours=2))
    work, written = _write(result, set(), monkeypatch)
    assert work.meta["locks_inserted"] == ["2026_03_KC_BUF"]
    assert [c["game_id"] for c in written] == ["2026_03_KC_BUF"]
    assert work.rows_written == 2


def test_write_skips_card_when_another_run_locked_first(monkeypatch):
    result = _build(KICKOFF - dt.timedelta(hours=2))
    work, written = _write(result, {"2026_03_KC_BUF"}, monkeypatch)
    assert work.meta["locks_lost_to_concurrent_run"] == ["2026_03_KC_BUF"]
    assert written == []


def test_card_hash_ignores_as_of():
    a = _build(KICKOFF - dt.timedelta(days=2)).cards[0]
    b = _build(KICKOFF - dt.timedelta(days=2) + dt.timedelta(minutes=5)).cards[0]
    assert a["content_hash"] == b["content_hash"]
    assert a["as_of"] != b["as_of"]
