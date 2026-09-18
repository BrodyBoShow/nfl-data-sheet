"""
Job: Resolve a point in time to (season, week, season_type) using the games table's
     actual schedule -- for sources that are rolling snapshots with no week of their own
     (ESPN injuries, Sleeper players). Never trust a caller-supplied ctx.week for these;
     a Tuesday run, a backfill, or dispatcher season/week drift could file a row under
     the wrong week if it did.
Reads: games
Writes: nothing
Tier: n/a
Phase: P3
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import psycopg

# A game week runs Tue-Mon in practice (Thu/Sun/Mon games, with Tue/Wed as the pre-game
# days before that week's first game). A timestamp up to two days before the next
# upcoming game still belongs to that game's week, not the prior one.
_LOOKAHEAD_DAYS = 2

# games.gameday is nflverse's ET calendar-day convention -- see _to_gameday below.
_ET = ZoneInfo("America/New_York")

_GameRow = tuple[date, int, int, str]  # (gameday, season, week, season_type)


def _pick_week(games: list[_GameRow], at: date) -> tuple[int, int, str] | None:
    """Pure selection logic, no DB access -- groups games into weeks, gives each week a
    window [first_game - _LOOKAHEAD_DAYS, last_game], and picks the week whose window
    contains `at`. If none does (before the season's first window, in a gap, or past the
    last game), falls back to the nearest upcoming week, then the nearest already-started
    one. `None` if `games` is empty."""
    if not games:
        return None

    gamedays_by_week: dict[tuple[int, int, str], list[date]] = {}
    for gameday, season, week, season_type in games:
        gamedays_by_week.setdefault((season, week, season_type), []).append(gameday)

    windows = [
        (key, min(gamedays) - timedelta(days=_LOOKAHEAD_DAYS), max(gamedays))
        for key, gamedays in gamedays_by_week.items()
    ]

    containing = sorted((w for w in windows if w[1] <= at <= w[2]), key=lambda w: w[1])
    if containing:
        return containing[0][0]

    upcoming = sorted((w for w in windows if w[1] > at), key=lambda w: w[1])
    if upcoming:
        return upcoming[0][0]

    past = sorted((w for w in windows if w[2] < at), key=lambda w: w[2], reverse=True)
    if past:
        return past[0][0]

    return None


def to_gameday(at: datetime | date) -> date:
    """games.gameday is nflverse's ET calendar-day convention -- a tz-aware `at` must
    convert to ET before truncating to a date, or a prime-time kickoff at/after ~8pm ET
    (already the next UTC calendar day) silently resolves against the wrong week's
    window. Verified live: the 2026 week 2 MNF game's commence time is
    2026-09-21T20:15 ET, i.e. 2026-09-22T00:15Z -- taking .date() on the raw UTC value
    reads 2026-09-22, which fell outside week 2's window and picked week 3 instead.

    Public (not module-private) because pipeline/collectors/odds.py's _match_game_id
    needs the identical conversion when matching an odds event's commence_time against
    games.gameday directly -- one place to get this right, not two copies to drift."""
    return at.astimezone(_ET).date() if isinstance(at, datetime) else at


def resolve_season_week(conn: psycopg.Connection, at: datetime | date) -> tuple[int, int, str]:
    at_date = to_gameday(at)

    with conn.cursor() as cur:
        cur.execute(
            "SELECT gameday, season, week, season_type FROM games WHERE gameday IS NOT NULL"
        )
        games: list[_GameRow] = cur.fetchall()

    result = _pick_week(games, at_date)
    if result is None:
        raise ValueError(f"no games found to resolve season/week for {at}")
    return result
