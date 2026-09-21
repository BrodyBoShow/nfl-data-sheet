import json
from datetime import UTC, date, datetime
from pathlib import Path

from pipeline.collectors.odds import (
    OddsCollector,
    _compute_consensus,
    _extract_normalized_fields,
    _match_game_id,
)
from pipeline.collectors.odds_schedule import Target

FIXTURES = Path(__file__).parent / "fixtures"


def _load_raw() -> dict:
    events = json.loads((FIXTURES / "odds_api_sample.json").read_text(encoding="utf-8"))
    return {
        "target_id": "sun_early",
        "events": events,
        "credits_spent": 3,
        "credits_remaining": 497,
    }


# --------------------------------------------------------------------------------------
# validate()
# --------------------------------------------------------------------------------------


def test_validate_parses_every_event():
    validated = OddsCollector().validate(_load_raw())
    assert len(validated["events"]) == 3
    assert validated["target_id"] == "sun_early"
    assert validated["credits_spent"] == 3


def test_validate_extracts_team_names_and_commence_time():
    validated = OddsCollector().validate(_load_raw())
    falcons_game = next(e for e in validated["events"] if e["home_team_name"] == "Atlanta Falcons")
    assert falcons_game["away_team_name"] == "Carolina Panthers"
    assert falcons_game["commence_time"].isoformat() == "2026-09-20T17:00:00+00:00"


def test_validate_carries_bookmakers_through_unmodified():
    validated = OddsCollector().validate(_load_raw())
    falcons_game = next(e for e in validated["events"] if e["home_team_name"] == "Atlanta Falcons")
    assert len(falcons_game["bookmakers"]) == 8
    assert {b["key"] for b in falcons_game["bookmakers"]} >= {"draftkings", "fanduel"}


def test_validate_raises_on_non_list_response():
    raw = _load_raw()
    raw["events"] = {"error": "bad request"}
    try:
        OddsCollector().validate(raw)
        raise AssertionError("expected ValueError")
    except ValueError as exc:
        assert "list" in str(exc)


def test_validate_raises_on_missing_keys():
    raw = _load_raw()
    raw["events"] = [{"id": "x"}]
    try:
        OddsCollector().validate(raw)
        raise AssertionError("expected ValueError")
    except ValueError as exc:
        assert "missing expected keys" in str(exc)


# --------------------------------------------------------------------------------------
# _extract_normalized_fields (pure, fixture data)
# --------------------------------------------------------------------------------------


def test_extract_normalized_fields_full_markets():
    validated = OddsCollector().validate(_load_raw())
    falcons_game = next(e for e in validated["events"] if e["home_team_name"] == "Atlanta Falcons")
    dk = next(b for b in falcons_game["bookmakers"] if b["key"] == "draftkings")

    fields = _extract_normalized_fields(dk, "Atlanta Falcons", "Carolina Panthers")

    assert fields["h2h_home_price"] == 124
    assert fields["h2h_away_price"] == -148
    assert fields["spread_home_point"] == 2.5
    assert fields["spread_away_point"] == -2.5
    assert fields["total_point"] == 43.5
    assert fields["total_over_price"] == -110
    assert fields["total_under_price"] == -110


def test_extract_normalized_fields_missing_market_stays_null():
    validated = OddsCollector().validate(_load_raw())
    chiefs_game = next(
        e for e in validated["events"] if e["home_team_name"] == "Kansas City Chiefs"
    )
    betmgm = next(b for b in chiefs_game["bookmakers"] if b["key"] == "betmgm")
    assert {m["key"] for m in betmgm["markets"]} == {"h2h", "totals"}

    fields = _extract_normalized_fields(betmgm, "Kansas City Chiefs", "Indianapolis Colts")

    assert fields["h2h_home_price"] is not None
    assert fields["total_point"] is not None
    assert fields["spread_home_point"] is None
    assert fields["spread_home_price"] is None
    assert fields["spread_away_point"] is None
    assert fields["spread_away_price"] is None


def test_extract_normalized_fields_none_bookmaker_returns_all_null():
    fields = _extract_normalized_fields(None, "Atlanta Falcons", "Carolina Panthers")
    assert all(v is None for v in fields.values())


# --------------------------------------------------------------------------------------
# _compute_consensus (pure, fixture data)
# --------------------------------------------------------------------------------------


def _per_bookmaker_fields(event: dict) -> list[dict]:
    return [
        _extract_normalized_fields(b, event["home_team_name"], event["away_team_name"])
        for b in event["bookmakers"]
    ]


def test_consensus_median_and_range_across_full_market_books():
    validated = OddsCollector().validate(_load_raw())
    falcons_game = next(e for e in validated["events"] if e["home_team_name"] == "Atlanta Falcons")

    # spread_home_point across all 8 books: mostly 2.5, one 2.0, one 3.0 -- verified live
    consensus = _compute_consensus(_per_bookmaker_fields(falcons_game))

    assert consensus["consensus_spread_point"] == 2.5
    assert consensus["spread_point_range"] == 1.0  # 3.0 - 2.0
    assert consensus["spread_book_count"] == 8
    assert consensus["consensus_total_point"] == 43.5
    assert consensus["total_point_range"] == 0.0  # every book agreed
    assert consensus["total_book_count"] == 8


def test_consensus_excludes_books_missing_the_market():
    validated = OddsCollector().validate(_load_raw())
    chiefs_game = next(
        e for e in validated["events"] if e["home_team_name"] == "Kansas City Chiefs"
    )
    # betmgm has no spreads market for this game (verified live) -- 7 of 8 books count
    consensus = _compute_consensus(_per_bookmaker_fields(chiefs_game))

    assert consensus["spread_book_count"] == 7
    assert consensus["consensus_spread_point"] == -6.5
    assert consensus["total_book_count"] == 8  # betmgm does have totals


def test_consensus_range_is_null_with_fewer_than_two_books():
    fields = [
        {
            "spread_home_point": -3.5,
            "total_point": 44.0,
            "h2h_home_price": None,
            "h2h_away_price": None,
            "spread_home_price": None,
            "spread_away_point": None,
            "spread_away_price": None,
            "total_over_price": None,
            "total_under_price": None,
        }
    ]
    consensus = _compute_consensus(fields)
    assert consensus["consensus_spread_point"] == -3.5
    assert consensus["spread_point_range"] is None
    assert consensus["spread_book_count"] == 1


def test_consensus_all_null_when_no_book_has_either_market():
    fields = [_extract_normalized_fields(None, "A", "B")]
    consensus = _compute_consensus(fields)
    assert consensus["consensus_spread_point"] is None
    assert consensus["spread_point_range"] is None
    assert consensus["spread_book_count"] == 0
    assert consensus["consensus_total_point"] is None
    assert consensus["total_point_range"] is None
    assert consensus["total_book_count"] == 0


# --------------------------------------------------------------------------------------
# _match_game_id -- must query games.gameday by ET calendar date, not raw UTC
# --------------------------------------------------------------------------------------


class _FakeGamesCursor:
    def __init__(self) -> None:
        self.last_params: tuple = ()

    def execute(self, query: str, params: tuple = ()) -> None:
        self.last_params = params

    def fetchone(self):
        return None

    def __enter__(self) -> "_FakeGamesCursor":
        return self

    def __exit__(self, *exc: object) -> bool:
        return False


class _FakeGamesConn:
    def __init__(self) -> None:
        self.cur = _FakeGamesCursor()

    def cursor(self) -> _FakeGamesCursor:
        return self.cur


def test_match_game_id_queries_by_et_gameday_not_raw_utc_date():
    # 2026 week 2 MNF (LA@NYG): kickoff 2026-09-21T20:15 ET == 2026-09-22T00:15Z
    # (verified live) -- games.gameday for this game is 2026-09-21 (ET), not the raw
    # UTC date 2026-09-22 that a naive .astimezone(UTC).date() would have queried.
    commence_time = datetime(2026, 9, 22, 0, 15, tzinfo=UTC)
    conn = _FakeGamesConn()

    _match_game_id(conn, "LA", "NYG", commence_time)  # type: ignore[arg-type]

    gameday_param = conn.cur.last_params[0]
    assert gameday_param == date(2026, 9, 21)


# --------------------------------------------------------------------------------------
# fetch() -- must fail clearly, not send an empty apiKey to The Odds API
# --------------------------------------------------------------------------------------


class _FakeSettings:
    def __init__(self, odds_api_key: str | None) -> None:
        self.odds_api_key = odds_api_key


class _FakeFetchCtx:
    """Duck-types RunContext's one attribute fetch() reads -- ctx.settings.odds_api_key."""

    def __init__(self, odds_api_key: str | None) -> None:
        self.settings = _FakeSettings(odds_api_key)


def test_fetch_raises_clear_error_when_api_key_missing():
    collector = OddsCollector()
    collector._due_target = Target(
        "sun_early",
        datetime(2026, 9, 20, 12, tzinfo=UTC),
        datetime(2026, 9, 20, 16, tzinfo=UTC),
    )

    try:
        collector.fetch(_FakeFetchCtx(odds_api_key=None))  # type: ignore[arg-type]
        raise AssertionError("expected RuntimeError")
    except RuntimeError as exc:
        assert str(exc) == "ODDS_API_KEY not set"
