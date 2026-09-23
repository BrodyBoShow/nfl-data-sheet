"""
Job: Compute per-game betting-market signals -- open vs. current line, movement, book
     disagreement, key-number crossings, implied team totals, no-vig win probability --
     for every game kicking off in a rolling window around now, from stored odds tables.
Reads: games, odds_snapshot_targets, odds_consensus, odds_snapshots, source_freshness
Writes: signals (sector='market')
Tier: T1
Phase: P4

Descriptive only. There is no handle/ticket/split data, so nothing here says who is
betting or why a line moved -- only where the line was, where it is, and how much the
books agree (docs/signals.md, "Market sector").

Scope is per game, not per dispatcher week, for the same reason as the Environment
analyst: The Odds API returns every upcoming game, so a poll fired for week N's targets
also stores week N+1's lines. Rows carry the game's own season/week from `games` (never
the odds tables' own season/week, which pre-fix rows got wrong), and stale-row cleanup is
scoped to this run's game_ids.

Open vs. current: "open" is the earliest capture fired by one of the game's OWN week's
targets (odds_snapshot_targets.captured_at == the capture's as_of -- odds.py writes ctx.now
to both). A poll from an earlier week's targets ("lookahead") never becomes the open: its
timing depends on the previous week's schedule and its book set is thinner. "Current" is
the latest pre-kickoff capture of any kind.
"""

from __future__ import annotations

import datetime as dt
import statistics
from collections import Counter
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, replace
from typing import Any
from zoneinfo import ZoneInfo

import polars as pl
import psycopg

from pipeline.core.base import Analyst, RunContext, WorkResult
from pipeline.core.db import delete_rows, upsert_rows
from pipeline.core.freshness import get_last_value
from pipeline.core.schedule import kickoff_utc

SECTOR = "market"

_ET = ZoneInfo("America/New_York")

# Same window as the Environment analyst. The lookback lets market_status settle from
# "awaiting" to "missed" after kickoff.
_LOOKBACK = dt.timedelta(hours=24)
_LOOKAHEAD = dt.timedelta(days=7)

# Spread key numbers (the common NFL final margins). Checked symmetrically on the signed
# home spread, so a favorite and an underdog crossing 3 both count.
KEY_NUMBERS = (3.0, 7.0, 10.0, 14.0)
_SIGNED_KEYS = tuple(sorted({s * k for k in KEY_NUMBERS for s in (1.0, -1.0)}))

# tue_opener's window closes Wednesday 09:00 ET (pipeline/collectors/odds_schedule.py).
# The on-time check applies that rule to every row rather than trusting the stored
# deadline, because pre-fix rows carry the old, much wider one (week 2: Sat 04:00Z).
_OPENER_TARGET = "tue_opener"
_OPENER_CLOSE_ET = dt.time(9, 0)

# Numeric codes -- signals.value is numeric-only. Tables mirrored in docs/signals.md.
STATUS_MOVEMENT = 1.0  # own-week open plus a later capture
STATUS_SINGLE_CAPTURE = 2.0  # one own-week capture: current is the open
STATUS_LOOKAHEAD_ONLY = 3.0  # lines exist, none from the game's own week
STATUS_AWAITING = 4.0  # nothing captured, kickoff ahead
STATUS_MISSED = 5.0  # nothing captured, kickoff passed

BASIS_OPENER_ON_TIME = 1.0
BASIS_OPENER_LATE = 2.0  # labeled tue_opener but fired past today's window (pre-fix)
BASIS_LATER_TARGET = 3.0  # no opener line for this game; open is a later own-week target

_SIGNAL_NAMES = frozenset(
    {
        "market_status",
        "market_own_week_captures",
        "market_current_lead_hours",
        "spread_home_current",
        "total_current",
        "spread_book_range",
        "total_book_range",
        "spread_key_straddle",
        "spread_home_open",
        "total_open",
        "market_open_lead_hours",
        "market_open_basis",
        "spread_home_move",
        "total_move",
        "spread_move_per_day",
        "total_move_per_day",
        "spread_key_crossings",
        "spread_book_set_changed",
        "total_book_set_changed",
        "spread_book_count_open",
        "spread_book_count_current",
        "total_book_count_open",
        "total_book_count_current",
        "implied_team_total",
        "win_prob_novig",
    }
)

_SIGNAL_SCHEMA: dict[str, Any] = {
    "game_id": pl.Utf8,
    "season": pl.Int64,
    "week": pl.Int64,
    "team": pl.Utf8,
    "player_id": pl.Utf8,
    "sector": pl.Utf8,
    "signal": pl.Utf8,
    "value": pl.Float64,
    "league_pct": pl.Float32,
    "sample_n": pl.Int64,
    "stability": pl.Float32,
    "as_of": pl.Datetime(time_zone="UTC"),
    "inputs_version": pl.Utf8,
}
_SIGNAL_COLS = list(_SIGNAL_SCHEMA)


@dataclass(frozen=True)
class Game:
    game_id: str
    season: int
    week: int
    kickoff: dt.datetime  # UTC
    home_team: str
    away_team: str


@dataclass(frozen=True)
class BookLine:
    """One bookmaker's line in one capture. Spread is from the capture's home side."""

    bookmaker: str
    spread_home: float | None
    total: float | None
    ml_home: int | None
    ml_away: int | None


@dataclass(frozen=True)
class Capture:
    """One poll's view of one game: the odds_consensus row plus its odds_snapshots rows.
    home_team/away_team are the odds tables' orientation until orient() aligns them."""

    as_of: dt.datetime
    home_team: str | None
    away_team: str | None
    spread_home: float | None
    spread_range: float | None
    spread_books: int
    total: float | None
    total_range: float | None
    total_books: int
    books: tuple[BookLine, ...]


@dataclass(frozen=True)
class CapturedTarget:
    target_id: str
    scheduled_for: dt.datetime
    captured_at: dt.datetime


# --------------------------------------------------------------------------------------
# Pure helpers (no DB access -- unit-tested against synthetic data)
# --------------------------------------------------------------------------------------


def in_window(kickoff: dt.datetime, now: dt.datetime) -> bool:
    return now - _LOOKBACK < kickoff <= now + _LOOKAHEAD


def _neg(v: float | None) -> float | None:
    return -v if v is not None else None


def orient(capture: Capture, game: Game) -> Capture | None:
    """Align a capture to games' home/away. odds.py matches events to games in either team
    order, so at a neutral site the API's home team can be our away team: then spreads are
    negated and moneyline sides swapped. Neither order matching returns None (the capture
    is skipped and reported, never guessed)."""
    if (capture.home_team, capture.away_team) == (game.home_team, game.away_team):
        return capture
    if (capture.home_team, capture.away_team) == (game.away_team, game.home_team):
        return replace(
            capture,
            home_team=game.home_team,
            away_team=game.away_team,
            spread_home=_neg(capture.spread_home),
            books=tuple(
                replace(b, spread_home=_neg(b.spread_home), ml_home=b.ml_away, ml_away=b.ml_home)
                for b in capture.books
            ),
        )
    return None


def opener_on_time(target: CapturedTarget) -> bool:
    """Captured before the Wednesday 09:00 ET that closes today's tue_opener window
    (the day after the window opens, Tuesday 10:00 ET)."""
    opened = target.scheduled_for.astimezone(_ET)
    closes = dt.datetime.combine(opened.date() + dt.timedelta(days=1), _OPENER_CLOSE_ET, _ET)
    return target.captured_at < closes


def select_open(
    captures: Sequence[Capture], own_targets: Sequence[CapturedTarget]
) -> tuple[Capture, float] | None:
    """(open capture, basis) -- the earliest capture fired by one of the game's own
    week's targets, or None if there is none. `captures` must be pre-kickoff only."""
    by_time = {t.captured_at: t for t in own_targets}
    own = sorted((c for c in captures if c.as_of in by_time), key=lambda c: c.as_of)
    if not own:
        return None
    first = own[0]
    target = by_time[first.as_of]
    if target.target_id != _OPENER_TARGET:
        return first, BASIS_LATER_TARGET
    return first, BASIS_OPENER_ON_TIME if opener_on_time(target) else BASIS_OPENER_LATE


def market_status(
    open_: Capture | None, current: Capture | None, kickoff: dt.datetime, now: dt.datetime
) -> float:
    if current is None:
        return STATUS_AWAITING if kickoff > now else STATUS_MISSED
    if open_ is None:
        return STATUS_LOOKAHEAD_ONLY
    if open_.as_of == current.as_of:
        return STATUS_SINGLE_CAPTURE
    return STATUS_MOVEMENT


def key_crossings(open_spread: float, current_spread: float) -> int:
    """Keys the consensus passed strictly through: open and current on opposite sides.
    Landing on a key, or leaving from one, is not a crossing."""
    return sum((open_spread - k) * (current_spread - k) < 0 for k in _SIGNED_KEYS)


def key_straddle(book_spreads: Sequence[float]) -> bool:
    """True if, for any key, the books fall into at least two of {below k, exactly k,
    beyond k} -- e.g. books at 2.5 and 3, or 3 and 3.5. The books don't agree which side
    of the key the line sits on."""
    for k in _SIGNED_KEYS:
        sides = {(s > k) - (s < k) for s in book_spreads}
        if len(sides) >= 2:
            return True
    return False


def american_to_prob(price: int) -> float:
    """Raw (vig-included) implied probability of an American moneyline price."""
    return 100 / (price + 100) if price > 0 else -price / (-price + 100)


def novig_home_prob(books: Iterable[BookLine]) -> tuple[float, int] | None:
    """(median no-vig home win probability, books used). Each book's vig is removed
    proportionally: p_home = r_home / (r_home + r_away). Books missing either side are
    skipped. The away value is 1 - this (the median of 1 - x is 1 - the median of x)."""
    probs = []
    for b in books:
        if b.ml_home is None or b.ml_away is None:
            continue
        r_home, r_away = american_to_prob(b.ml_home), american_to_prob(b.ml_away)
        probs.append(r_home / (r_home + r_away))
    return (statistics.median(probs), len(probs)) if probs else None


def implied_team_totals(spread_home: float, total: float) -> tuple[float, float]:
    """(home, away). A home spread of -3 with a total of 44 gives 23.5 / 20.5."""
    return total / 2 - spread_home / 2, total / 2 + spread_home / 2


def _spread_books(capture: Capture) -> set[str]:
    return {b.bookmaker for b in capture.books if b.spread_home is not None}


def _total_books(capture: Capture) -> set[str]:
    return {b.bookmaker for b in capture.books if b.total is not None}


def _lead_hours(kickoff: dt.datetime, as_of: dt.datetime) -> float:
    return (kickoff - as_of).total_seconds() / 3600


def _iso(t: dt.datetime) -> str:
    return t.astimezone(dt.UTC).isoformat()


def _signal_row(
    game: Game,
    team: str | None,
    signal: str,
    value: float,
    sample_n: int | None,
    as_of: dt.datetime,
    inputs_version: str,
) -> dict[str, Any]:
    return {
        "game_id": game.game_id,
        "season": int(game.season),  # the game's own season/week, never ctx's
        "week": int(game.week),
        "team": team,
        "player_id": None,
        "sector": SECTOR,
        "signal": signal,
        "value": float(value),
        "league_pct": None,
        "sample_n": int(sample_n) if sample_n is not None else None,
        "stability": None,
        "as_of": as_of,
        "inputs_version": inputs_version,
    }


def game_values(
    game: Game, open_: Capture | None, basis: float | None, current: Capture | None
) -> list[tuple[str | None, str, float, int | None]]:
    """(team, signal, value, sample_n) for everything but market_status and
    market_own_week_captures. Current-state rows need a current capture; open, movement,
    crossing, and book-set rows need a distinct open (status 1)."""
    out: list[tuple[str | None, str, float, int | None]] = []
    if current is None:
        return out

    out.append((None, "market_current_lead_hours", _lead_hours(game.kickoff, current.as_of), None))
    if current.spread_home is not None:
        out.append((None, "spread_home_current", current.spread_home, current.spread_books))
    if current.total is not None:
        out.append((None, "total_current", current.total, current.total_books))
    if current.spread_range is not None:
        out.append((None, "spread_book_range", current.spread_range, current.spread_books))
    if current.total_range is not None:
        out.append((None, "total_book_range", current.total_range, current.total_books))

    book_spreads = [b.spread_home for b in current.books if b.spread_home is not None]
    if len(book_spreads) >= 2:
        straddle = 1.0 if key_straddle(book_spreads) else 0.0
        out.append((None, "spread_key_straddle", straddle, len(book_spreads)))

    if current.spread_home is not None and current.total is not None:
        home_tt, away_tt = implied_team_totals(current.spread_home, current.total)
        n = min(current.spread_books, current.total_books)
        out.append((game.home_team, "implied_team_total", home_tt, n))
        out.append((game.away_team, "implied_team_total", away_tt, n))

    novig = novig_home_prob(current.books)
    if novig is not None:
        p_home, n = novig
        out.append((game.home_team, "win_prob_novig", p_home, n))
        out.append((game.away_team, "win_prob_novig", 1 - p_home, n))

    if open_ is None or basis is None or open_.as_of == current.as_of:
        return out

    days = (current.as_of - open_.as_of).total_seconds() / 86400
    out.append((None, "market_open_lead_hours", _lead_hours(game.kickoff, open_.as_of), None))
    out.append((None, "market_open_basis", basis, None))

    if open_.spread_home is not None:
        out.append((None, "spread_home_open", open_.spread_home, open_.spread_books))
    if open_.total is not None:
        out.append((None, "total_open", open_.total, open_.total_books))

    if open_.spread_home is not None and current.spread_home is not None:
        move = current.spread_home - open_.spread_home
        n = min(open_.spread_books, current.spread_books)
        out.append((None, "spread_home_move", move, n))
        out.append((None, "spread_move_per_day", move / days, n))
        crossings = key_crossings(open_.spread_home, current.spread_home)
        out.append((None, "spread_key_crossings", float(crossings), None))
        changed = _spread_books(open_) != _spread_books(current)
        out.append((None, "spread_book_set_changed", 1.0 if changed else 0.0, None))
        out.append((None, "spread_book_count_open", float(open_.spread_books), None))
        out.append((None, "spread_book_count_current", float(current.spread_books), None))

    if open_.total is not None and current.total is not None:
        move = current.total - open_.total
        n = min(open_.total_books, current.total_books)
        out.append((None, "total_move", move, n))
        out.append((None, "total_move_per_day", move / days, n))
        changed = _total_books(open_) != _total_books(current)
        out.append((None, "total_book_set_changed", 1.0 if changed else 0.0, None))
        out.append((None, "total_book_count_open", float(open_.total_books), None))
        out.append((None, "total_book_count_current", float(current.total_books), None))

    return out


def build_rows(
    *,
    games: Sequence[Game],
    captures: dict[str, list[Capture]],
    targets: dict[tuple[int, int], list[CapturedTarget]],
    now: dt.datetime,
    base_version: str,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Every signal row for `games`, plus run meta. Pure."""
    rows: list[dict[str, Any]] = []
    status_counts: Counter[str] = Counter()
    orientation_mismatch: list[dict[str, Any]] = []
    lookahead_only: list[str] = []

    for game in games:
        pre_kickoff: list[Capture] = []
        for raw in captures.get(game.game_id, []):
            if raw.as_of >= game.kickoff:
                continue
            aligned = orient(raw, game)
            if aligned is None:
                orientation_mismatch.append(
                    {
                        "game_id": game.game_id,
                        "as_of": _iso(raw.as_of),
                        "odds_home": raw.home_team,
                        "odds_away": raw.away_team,
                    }
                )
                continue
            pre_kickoff.append(aligned)

        own_targets = targets.get((game.season, game.week), [])
        own_times = {t.captured_at for t in own_targets}
        own_count = sum(c.as_of in own_times for c in pre_kickoff)

        selected = select_open(pre_kickoff, own_targets)
        open_, basis = selected if selected else (None, None)
        current = max(pre_kickoff, key=lambda c: c.as_of) if pre_kickoff else None
        status = market_status(open_, current, game.kickoff, now)
        status_counts[str(int(status))] += 1
        if status == STATUS_LOOKAHEAD_ONLY:
            lookahead_only.append(game.game_id)

        if current is None:
            version = base_version
        elif status == STATUS_MOVEMENT and open_ is not None:
            version = f"odds_open@{_iso(open_.as_of)},odds_current@{_iso(current.as_of)}"
        else:
            version = f"odds_current@{_iso(current.as_of)}"

        rows.append(_signal_row(game, None, "market_status", status, None, now, version))
        rows.append(
            _signal_row(game, None, "market_own_week_captures", own_count, None, now, version)
        )
        for team, signal, value, n in game_values(game, open_, basis, current):
            rows.append(_signal_row(game, team, signal, value, n, now, version))

    meta = {
        "games": len(games),
        "market_status_counts": dict(status_counts),
        "orientation_mismatch": orientation_mismatch,
        "lookahead_only_games": lookahead_only,
    }
    return rows, meta


# --------------------------------------------------------------------------------------
# DB I/O (thin -- feeds the pure functions above)
# --------------------------------------------------------------------------------------


def load_window_games(conn: psycopg.Connection, now: dt.datetime) -> list[Game]:
    """Games with now - _LOOKBACK < kickoff <= now + _LOOKAHEAD. Narrows by ET gameday
    with a day of slack each side, then filters exactly on kickoff_utc (same approach as
    the Environment analyst)."""
    start_day = (now - _LOOKBACK - dt.timedelta(days=1)).date()
    end_day = (now + _LOOKAHEAD + dt.timedelta(days=1)).date()
    with conn.cursor() as cur:
        cur.execute(
            "SELECT game_id, season, week, gameday, gametime, home_team, away_team FROM games "
            "WHERE gameday BETWEEN %s AND %s AND gametime IS NOT NULL ORDER BY game_id",
            (start_day, end_day),
        )
        rows = cur.fetchall()
    games = []
    for r in rows:
        kickoff = kickoff_utc(r[3], r[4])
        if in_window(kickoff, now):
            games.append(Game(r[0], r[1], r[2], kickoff, r[5], r[6]))
    return games


def _load_captures(conn: psycopg.Connection, game_ids: list[str]) -> dict[str, list[Capture]]:
    books: dict[tuple[str, dt.datetime], list[BookLine]] = {}
    with conn.cursor() as cur:
        cur.execute(
            "SELECT game_id, as_of, bookmaker, spread_home_point, total_point, "
            "h2h_home_price, h2h_away_price FROM odds_snapshots WHERE game_id = ANY(%s)",
            (game_ids,),
        )
        for game_id, as_of, bookmaker, spread, total, ml_home, ml_away in cur.fetchall():
            books.setdefault((game_id, as_of), []).append(
                BookLine(bookmaker, spread, total, ml_home, ml_away)
            )
        cur.execute(
            "SELECT game_id, as_of, home_team, away_team, consensus_spread_point, "
            "spread_point_range, spread_book_count, consensus_total_point, total_point_range, "
            "total_book_count FROM odds_consensus WHERE game_id = ANY(%s) "
            "ORDER BY game_id, as_of",
            (game_ids,),
        )
        consensus = cur.fetchall()
    out: dict[str, list[Capture]] = {}
    for game_id, as_of, home, away, spread, s_range, s_n, total, t_range, t_n in consensus:
        capture_books = sorted(books.get((game_id, as_of), []), key=lambda b: b.bookmaker)
        out.setdefault(game_id, []).append(
            Capture(
                as_of, home, away, spread, s_range, s_n, total, t_range, t_n, tuple(capture_books)
            )
        )
    return out


def _load_captured_targets(
    conn: psycopg.Connection, seasons: list[int]
) -> dict[tuple[int, int], list[CapturedTarget]]:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT season, week, target_id, scheduled_for, captured_at "
            "FROM odds_snapshot_targets "
            "WHERE status = 'captured' AND captured_at IS NOT NULL AND season = ANY(%s)",
            (seasons,),
        )
        out: dict[tuple[int, int], list[CapturedTarget]] = {}
        for season, week, target_id, scheduled_for, captured_at in cur.fetchall():
            out.setdefault((season, week), []).append(
                CapturedTarget(target_id, scheduled_for, captured_at)
            )
        return out


def _base_version(conn: psycopg.Connection) -> str:
    return f"schedules@{get_last_value(conn, 'nflverse:schedules') or 'unknown'}"


class MarketAnalyst(Analyst):
    name = "market"
    sector = SECTOR
    signal_names = _SIGNAL_NAMES

    def __init__(self) -> None:
        # The game_ids compute() covered -- _delete_stale_signals scopes to exactly these.
        self._game_ids: list[str] = []
        self._meta: dict[str, Any] = {}

    def inputs_ready(self, ctx: RunContext) -> bool | str:
        # No game in the window is a routine "nothing to do" (skipped_fresh).
        return bool(load_window_games(ctx.conn, ctx.now))

    def compute(self, ctx: RunContext) -> pl.DataFrame:
        conn = ctx.conn
        games = load_window_games(conn, ctx.now)
        self._game_ids = [g.game_id for g in games]
        if not games:
            self._meta = {"games": 0}
            return pl.DataFrame(schema=_SIGNAL_SCHEMA)

        rows, self._meta = build_rows(
            games=games,
            captures=_load_captures(conn, self._game_ids),
            targets=_load_captured_targets(conn, sorted({g.season for g in games})),
            now=ctx.now,
            base_version=_base_version(conn),
        )
        return (
            pl.DataFrame(rows, schema=_SIGNAL_SCHEMA)
            if rows
            else pl.DataFrame(schema=_SIGNAL_SCHEMA)
        )

    def _delete_stale_signals(self, ctx: RunContext) -> int:
        """Scoped to (sector, this run's game_ids) instead of the base class's
        (ctx.season, ctx.week): the window spans dispatcher weeks. Still restricted to this
        analyst's own sector + signal_names."""
        if not self._game_ids:
            return 0
        return delete_rows(
            ctx.conn,
            "signals",
            "sector = %s AND signal = ANY(%s) AND game_id = ANY(%s)",
            (self.sector, sorted(self.signal_names), self._game_ids),
        )

    def write_signals(self, ctx: RunContext, df: pl.DataFrame) -> WorkResult:
        rows = df.to_dicts()
        if not rows:
            return WorkResult(0, self._meta)
        conflict_cols = [
            "season",
            "week",
            "COALESCE(game_id, '')",
            "COALESCE(team, '')",
            "COALESCE(player_id, '')",
            "sector",
            "signal",
        ]
        identity_cols = ("season", "week", "game_id", "team", "player_id", "sector", "signal")
        update_cols = [c for c in _SIGNAL_COLS if c not in identity_cols]
        written = upsert_rows(
            ctx.conn, "signals", rows, conflict_cols=conflict_cols, update_cols=update_cols
        )
        return WorkResult(written, self._meta)
