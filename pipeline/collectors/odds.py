"""
Job: Fetch NFL odds (h2h/spreads/totals, us region) from The Odds API for whichever
     snapshot target is currently due (pipeline/collectors/odds_schedule.py decides),
     and store one append-only row per (event, bookmaker, poll) plus one derived
     consensus row per (event, poll).
Reads: The Odds API, games, teams, odds_snapshot_targets
Writes: odds_snapshots, odds_consensus, odds_snapshot_targets (capture/miss bookkeeping)
Tier: T1
Phase: P4
"""

from __future__ import annotations

import json
import statistics
from datetime import datetime
from typing import Any

import httpx
import psycopg

from pipeline.collectors import odds_schedule
from pipeline.collectors.odds_schedule import Target
from pipeline.core.base import Collector, RunContext, WorkResult
from pipeline.core.db import filter_changed, upsert_rows
from pipeline.core.hashing import hash_row
from pipeline.core.schedule import resolve_season_week, to_gameday

_ODDS_URL = "https://api.the-odds-api.com/v4/sports/americanfootball_nfl/odds/"

_SNAPSHOT_COLS = [
    "source_event_id",
    "bookmaker",
    "game_id",
    "target_id",
    "season",
    "week",
    "commence_time",
    "home_team",
    "away_team",
    "h2h_home_price",
    "h2h_away_price",
    "spread_home_point",
    "spread_home_price",
    "spread_away_point",
    "spread_away_price",
    "total_point",
    "total_over_price",
    "total_under_price",
    "markets_raw",
    "as_of",
]
_SNAPSHOT_PK = ["source_event_id", "bookmaker", "as_of"]

_CONSENSUS_COLS = [
    "source_event_id",
    "game_id",
    "target_id",
    "season",
    "week",
    "commence_time",
    "home_team",
    "away_team",
    "consensus_spread_point",
    "spread_point_range",
    "spread_book_count",
    "consensus_total_point",
    "total_point_range",
    "total_book_count",
    "as_of",
]
_CONSENSUS_PK = ["source_event_id", "as_of"]


def _parse_commence_time(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _extract_normalized_fields(
    bookmaker: dict[str, Any] | None, home_team_name: str, away_team_name: str
) -> dict[str, Any]:
    """Pure. Flattens one bookmaker's markets into odds_snapshots' normalized columns --
    any market/outcome that bookmaker doesn't carry stays null, never guessed."""
    fields: dict[str, Any] = dict.fromkeys(
        (
            "h2h_home_price",
            "h2h_away_price",
            "spread_home_point",
            "spread_home_price",
            "spread_away_point",
            "spread_away_price",
            "total_point",
            "total_over_price",
            "total_under_price",
        )
    )
    if bookmaker is None:
        return fields

    markets_by_key = {m["key"]: m for m in bookmaker.get("markets") or []}

    h2h = markets_by_key.get("h2h")
    if h2h:
        for outcome in h2h["outcomes"]:
            if outcome["name"] == home_team_name:
                fields["h2h_home_price"] = outcome["price"]
            elif outcome["name"] == away_team_name:
                fields["h2h_away_price"] = outcome["price"]

    spreads = markets_by_key.get("spreads")
    if spreads:
        for outcome in spreads["outcomes"]:
            if outcome["name"] == home_team_name:
                fields["spread_home_point"] = outcome["point"]
                fields["spread_home_price"] = outcome["price"]
            elif outcome["name"] == away_team_name:
                fields["spread_away_point"] = outcome["point"]
                fields["spread_away_price"] = outcome["price"]

    totals = markets_by_key.get("totals")
    if totals:
        for outcome in totals["outcomes"]:
            if outcome["name"] == "Over":
                fields["total_point"] = outcome["point"]
                fields["total_over_price"] = outcome["price"]
            elif outcome["name"] == "Under":
                fields["total_under_price"] = outcome["price"]

    return fields


def _compute_consensus(per_bookmaker_fields: list[dict[str, Any]]) -> dict[str, Any]:
    """Pure. `per_bookmaker_fields` is one event's list of _extract_normalized_fields()
    outputs, one per bookmaker in that poll. consensus_*_point is the median across
    books that carry the market; *_point_range (max-min) is the disagreement signal,
    left null when fewer than 2 books have it -- disagreement isn't measurable from one
    observation. No h2h consensus: median of American moneyline prices isn't a
    meaningful stat without converting to implied probability first, which is real
    Market-analyst work, not this collector's."""
    spread_points = [
        f["spread_home_point"] for f in per_bookmaker_fields if f["spread_home_point"] is not None
    ]
    total_points = [
        f["total_point"] for f in per_bookmaker_fields if f["total_point"] is not None
    ]
    return {
        "consensus_spread_point": statistics.median(spread_points) if spread_points else None,
        "spread_point_range": (
            max(spread_points) - min(spread_points) if len(spread_points) >= 2 else None
        ),
        "spread_book_count": len(spread_points),
        "consensus_total_point": statistics.median(total_points) if total_points else None,
        "total_point_range": (
            max(total_points) - min(total_points) if len(total_points) >= 2 else None
        ),
        "total_book_count": len(total_points),
    }


def _finalize(row: dict[str, Any], now: datetime, hash_fields: list[str]) -> dict[str, Any]:
    row = dict(row)
    row["content_hash"] = hash_row({k: row.get(k) for k in hash_fields})
    row["updated_at"] = now
    return row


def _fetch_team_abbr_by_name(conn: psycopg.Connection) -> dict[str, str]:
    with conn.cursor() as cur:
        cur.execute("SELECT team_name, team_abbr FROM teams WHERE is_active")
        return dict(cur.fetchall())


def _match_game_id(
    conn: psycopg.Connection, home_abbr: str, away_abbr: str, commence_time: datetime
) -> str | None:
    """Matches on gameday + both team abbreviations (order-agnostic, since a mismatch
    between the API's home/away designation and ours is possible but the pairing isn't)
    rather than trusting The Odds API's own opaque event id against nflverse's game_id
    (verified live: they're unrelated identifiers). Uses to_gameday (ET, not raw UTC)
    for the same reason resolve_season_week does -- a prime-time commence_time already
    reads as the next UTC calendar day (verified live: every one of the first week's
    unresolved games was a >=8pm ET kickoff whose raw UTC date never matched
    games.gameday, this week's own MNF game included)."""
    gameday = to_gameday(commence_time)
    with conn.cursor() as cur:
        cur.execute(
            "SELECT game_id FROM games WHERE gameday = %s "
            "AND ((home_team = %s AND away_team = %s) OR (home_team = %s AND away_team = %s))",
            (gameday, home_abbr, away_abbr, away_abbr, home_abbr),
        )
        row = cur.fetchone()
        return row[0] if row else None


class OddsCollector(Collector):
    name = "odds"

    def __init__(self) -> None:
        self._due_target: Target | None = None

    def should_run(self, ctx: RunContext) -> bool:
        targets = odds_schedule.ensure_week_targets(ctx.conn, ctx.season, ctx.week)
        states = odds_schedule.load_states(ctx.conn, ctx.season, ctx.week)
        week_spent = odds_schedule.credits_spent_this_week(ctx.conn, ctx.season, ctx.week)
        month_spent = odds_schedule.credits_spent_this_month(ctx.conn, ctx.season, ctx.now)
        decision = odds_schedule.decide(targets, states, ctx.now, week_spent, month_spent)

        for target_id, reason in decision.missed:
            odds_schedule.record_missed(ctx.conn, ctx.season, ctx.week, target_id, reason, ctx.now)

        self._due_target = decision.fire
        return decision.fire is not None

    def fetch(self, ctx: RunContext) -> dict[str, Any]:
        target = self._due_target
        assert target is not None, "fetch() called without a due target -- should_run() runs first"

        resp = httpx.get(
            _ODDS_URL,
            params={
                "apiKey": ctx.settings.odds_api_key,
                "regions": "us",
                "markets": "h2h,spreads,totals",
                "oddsFormat": "american",
            },
            timeout=30,
        )
        resp.raise_for_status()

        # x-requests-last is this call's actual cost -- verified live to equal 3 for
        # h2h+spreads+totals/us, but read from the response rather than assumed, per
        # docs/sources.md's known trap (plan/params could change what a call costs).
        credits_spent = int(resp.headers.get("x-requests-last", odds_schedule.STANDARD_CREDITS))
        credits_remaining = resp.headers.get("x-requests-remaining")

        return {
            "target_id": target.target_id,
            "events": resp.json(),
            "credits_spent": credits_spent,
            "credits_remaining": int(credits_remaining) if credits_remaining else None,
        }

    def validate(self, raw: dict[str, Any]) -> dict[str, Any]:
        events = raw["events"]
        if not isinstance(events, list):
            raise ValueError("odds response is not a list of events")

        parsed = []
        for event in events:
            required = {"id", "commence_time", "home_team", "away_team", "bookmakers"}
            missing = required - event.keys()
            if missing:
                raise ValueError(f"odds event missing expected keys: {missing}")
            parsed.append(
                {
                    "source_event_id": event["id"],
                    "commence_time": _parse_commence_time(event["commence_time"]),
                    "home_team_name": event["home_team"],
                    "away_team_name": event["away_team"],
                    "bookmakers": event["bookmakers"],
                }
            )

        return {
            "target_id": raw["target_id"],
            "credits_spent": raw["credits_spent"],
            "credits_remaining": raw["credits_remaining"],
            "events": parsed,
        }

    def store(self, ctx: RunContext, validated: dict[str, Any]) -> WorkResult:
        conn = ctx.conn
        target_id = validated["target_id"]
        credits_spent = validated["credits_spent"]

        team_abbr_by_name = _fetch_team_abbr_by_name(conn)

        unresolved_team = 0
        unresolved_game = 0
        snapshot_rows: list[dict[str, Any]] = []
        consensus_rows: list[dict[str, Any]] = []
        for event in validated["events"]:
            home_abbr = team_abbr_by_name.get(event["home_team_name"])
            away_abbr = team_abbr_by_name.get(event["away_team_name"])
            if home_abbr is None or away_abbr is None:
                unresolved_team += 1

            game_id = None
            if home_abbr and away_abbr:
                game_id = _match_game_id(conn, home_abbr, away_abbr, event["commence_time"])
            if game_id is None:
                unresolved_game += 1

            season, week, _season_type = resolve_season_week(conn, event["commence_time"])

            common = {
                "game_id": game_id,
                "target_id": target_id,
                "season": season,
                "week": week,
                "commence_time": event["commence_time"],
                "home_team": home_abbr,
                "away_team": away_abbr,
                "as_of": ctx.now,
            }

            per_bookmaker_fields = []
            for bookmaker in event["bookmakers"]:
                fields = _extract_normalized_fields(
                    bookmaker, event["home_team_name"], event["away_team_name"]
                )
                per_bookmaker_fields.append(fields)
                snapshot_rows.append(
                    {
                        "source_event_id": event["source_event_id"],
                        "bookmaker": bookmaker["key"],
                        **common,
                        **fields,
                        "markets_raw": json.dumps(bookmaker.get("markets") or [], sort_keys=True),
                    }
                )

            consensus_rows.append(
                {
                    "source_event_id": event["source_event_id"],
                    **common,
                    **_compute_consensus(per_bookmaker_fields),
                }
            )

        snapshot_hash_fields = [c for c in _SNAPSHOT_COLS if c not in _SNAPSHOT_PK]
        snapshot_rows = [_finalize(r, ctx.now, snapshot_hash_fields) for r in snapshot_rows]
        # Pass-through, same as injuries (pipeline/collectors/availability.py) -- as_of
        # is unique per run, so every row is "new" by PK; kept for a uniform store()
        # shape across collectors, not because it drops anything here.
        snapshot_rows = filter_changed(conn, "odds_snapshots", _SNAPSHOT_PK, snapshot_rows)
        written = upsert_rows(
            conn,
            "odds_snapshots",
            snapshot_rows,
            conflict_cols=_SNAPSHOT_PK,
            update_cols=snapshot_hash_fields + ["content_hash", "updated_at"],
        )

        consensus_hash_fields = [c for c in _CONSENSUS_COLS if c not in _CONSENSUS_PK]
        consensus_rows = [_finalize(r, ctx.now, consensus_hash_fields) for r in consensus_rows]
        consensus_rows = filter_changed(conn, "odds_consensus", _CONSENSUS_PK, consensus_rows)
        written += upsert_rows(
            conn,
            "odds_consensus",
            consensus_rows,
            conflict_cols=_CONSENSUS_PK,
            update_cols=consensus_hash_fields + ["content_hash", "updated_at"],
        )

        odds_schedule.record_capture(conn, ctx.season, ctx.week, target_id, credits_spent, ctx.now)

        return WorkResult(
            written,
            meta={
                "target_id": target_id,
                "credits_spent": credits_spent,
                "credits_remaining": validated["credits_remaining"],
                "events_written": len(consensus_rows),
                "bookmaker_rows_written": len(snapshot_rows),
                "unresolved_team": unresolved_team,
                "unresolved_game": unresolved_game,
            },
        )
