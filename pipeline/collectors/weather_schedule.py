"""
Job: Compute each upcoming game's weather-snapshot target windows (10 per game, relative
     to kickoff) and decide, from stored target state vs. absolute time, which targets are
     due right now and which have been missed. The scheduling half of the weather
     collector -- pipeline/collectors/weather.py does the venue guard and the fetch.
Reads: games (kickoff times), weather_snapshot_targets
Writes: weather_snapshot_targets
Tier: T1
Phase: P4
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from typing import Literal

import psycopg

from pipeline.core.schedule import kickoff_utc

# (target_id, hours before kickoff), earliest first. Weighted late (docs/phases/P4.md):
# earlier snapshots mostly capture model noise. t48 is kept as the only forecast that
# exists when Sunday cards are drafted Friday, but flagged (MODEL_REGIME_BREAK_TARGETS).
LEADS: tuple[tuple[str, int], ...] = (
    ("t48", 48),
    ("t36", 36),
    ("t24", 24),
    ("t18", 18),
    ("t12", 12),
    ("t6", 6),
    ("t4", 4),
    ("t2", 2),
    ("t1", 1),
    ("t0", 0),
)

# t48 lands past HRRR's reach at US venues (GFS) while t36+ is HRRR -- the t48 -> t36
# change is partly a model switch (docs/sources.md). Flagged by target for every venue.
MODEL_REGIME_BREAK_TARGETS = frozenset({"t48"})

# t0's window: kickoff to kickoff + this. There's no next-later target to close it.
_T0_WINDOW = dt.timedelta(hours=1)

# How far ahead ensure_targets looks for games: the earliest target's lead, plus slack so
# a game enters the table a little before its t48 window opens.
HORIZON = dt.timedelta(hours=LEADS[0][1] + 6)

MissReason = Literal["deadline_passed"]


@dataclass(frozen=True)
class Target:
    game_id: str
    target_id: str
    kickoff: dt.datetime  # UTC
    scheduled_for: dt.datetime  # UTC -- window opens
    deadline: dt.datetime  # UTC -- window closes (exclusive)


@dataclass(frozen=True)
class GameRow:
    game_id: str
    season: int
    week: int
    kickoff: dt.datetime  # UTC
    stadium_id: str | None


@dataclass(frozen=True)
class Decision:
    due: list[Target]
    missed: list[tuple[Target, MissReason]]


def compute_game_targets(game_id: str, kickoff: dt.datetime) -> list[Target]:
    """Pure. One target per lead in LEADS. Each window opens at kickoff - lead and closes
    when the next-later target's window opens (t0: kickoff + _T0_WINDOW), so windows
    never overlap and a late tick can never file a snapshot under the wrong lead."""
    opens = [kickoff - dt.timedelta(hours=hours) for _, hours in LEADS]
    closes = opens[1:] + [kickoff + _T0_WINDOW]
    return [
        Target(game_id, target_id, kickoff, open_at, close_at)
        for (target_id, _), open_at, close_at in zip(LEADS, opens, closes, strict=True)
    ]


def decide(pending: list[Target], now: dt.datetime) -> Decision:
    """Pure. `pending` is every still-pending target whose window has opened
    (scheduled_for <= now). A target past its deadline is missed ('deadline_passed');
    one inside its window is due. Windows don't overlap, so at most one target per game
    is ever due at once -- there is no catch-up collapsing to do (unlike odds): an older
    target whose window closed is simply missed, never fired late."""
    due: list[Target] = []
    missed: list[tuple[Target, MissReason]] = []
    for target in pending:
        if target.scheduled_for > now:
            continue
        if now >= target.deadline:
            missed.append((target, "deadline_passed"))
        else:
            due.append(target)
    return Decision(due, missed)


def upcoming_games(conn: psycopg.Connection, now: dt.datetime) -> list[GameRow]:
    """Games whose kickoff falls in (now - t0 window, now + HORIZON]. Queried by ET
    gameday range first (games has no kickoff timestamp column), then filtered exactly."""
    start_day = (now - dt.timedelta(days=1)).date()
    end_day = (now + HORIZON + dt.timedelta(days=1)).date()
    with conn.cursor() as cur:
        cur.execute(
            "SELECT game_id, season, week, gameday, gametime, stadium_id FROM games "
            "WHERE gameday BETWEEN %s AND %s AND gametime IS NOT NULL",
            (start_day, end_day),
        )
        rows = cur.fetchall()
    games = []
    for game_id, season, week, gameday, gametime, stadium_id in rows:
        kickoff = kickoff_utc(gameday, gametime)
        if now - _T0_WINDOW < kickoff <= now + HORIZON:
            games.append(GameRow(game_id, season, week, kickoff, stadium_id))
    return games


def ensure_targets(conn: psycopg.Connection, games: list[GameRow]) -> int:
    """Inserts every game's 10 targets as 'pending'. On conflict, only a still-pending row
    has its kickoff/window refreshed -- a flexed game (kickoff moved after its targets
    were created) gets correct windows, while captured/missed/skipped rows are never
    touched. A game first seen after some of its windows already closed still gets those
    rows; decide() marks them missed, which is the honest record."""
    rows = [
        (
            t.game_id,
            t.target_id,
            g.season,
            g.week,
            g.stadium_id,
            t.kickoff,
            t.scheduled_for,
            t.deadline,
        )
        for g in games
        for t in compute_game_targets(g.game_id, g.kickoff)
    ]
    if not rows:
        return 0
    with conn.cursor() as cur:
        cur.executemany(
            "INSERT INTO weather_snapshot_targets "
            "(game_id, target_id, season, week, stadium_id, kickoff, scheduled_for, deadline) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s) "
            "ON CONFLICT (game_id, target_id) DO UPDATE SET "
            "kickoff = EXCLUDED.kickoff, scheduled_for = EXCLUDED.scheduled_for, "
            "deadline = EXCLUDED.deadline, stadium_id = EXCLUDED.stadium_id, "
            "updated_at = now() "
            "WHERE weather_snapshot_targets.status = 'pending' AND ("
            "weather_snapshot_targets.kickoff IS DISTINCT FROM EXCLUDED.kickoff OR "
            "weather_snapshot_targets.stadium_id IS DISTINCT FROM EXCLUDED.stadium_id)",
            rows,
        )
    return len(rows)


def load_open_pending(conn: psycopg.Connection, now: dt.datetime) -> list[Target]:
    """Every pending target whose window has opened -- including ones whose deadline
    passed while no tick ran, so decide() can mark them missed."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT game_id, target_id, kickoff, scheduled_for, deadline "
            "FROM weather_snapshot_targets WHERE status = 'pending' AND scheduled_for <= %s",
            (now,),
        )
        return [Target(*row) for row in cur.fetchall()]


def record_missed(
    conn: psycopg.Connection, target: Target, reason: MissReason, now: dt.datetime
) -> None:
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE weather_snapshot_targets SET status = 'missed', missed_reason = %s, "
            "updated_at = %s WHERE game_id = %s AND target_id = %s AND status = 'pending'",
            (reason, now, target.game_id, target.target_id),
        )


def record_captured(conn: psycopg.Connection, target: Target, now: dt.datetime) -> None:
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE weather_snapshot_targets SET status = 'captured', captured_at = %s, "
            "updated_at = %s WHERE game_id = %s AND target_id = %s",
            (now, now, target.game_id, target.target_id),
        )


def record_skipped(conn: psycopg.Connection, target: Target, reason: str, now: dt.datetime) -> None:
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE weather_snapshot_targets SET status = 'skipped', skip_reason = %s, "
            "updated_at = %s WHERE game_id = %s AND target_id = %s",
            (reason, now, target.game_id, target.target_id),
        )
