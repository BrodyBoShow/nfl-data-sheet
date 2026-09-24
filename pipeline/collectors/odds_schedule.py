"""
Job: Compute this season/week's odds-snapshot target calendar and decide, from stored
     capture/credit-spend state, whether an odds fetch is due right now. The scheduling
     half of the odds collector -- pipeline/collectors/odds.py does the actual
     fetch/validate/store against the (now verified) Odds API; everything decidable
     without touching it -- when a credit should be spent, and the budget guard --
     lives here, complete and tested on its own.
Reads: games (kickoff times for the pre-kickoff targets), odds_snapshot_targets
Writes: odds_snapshot_targets
Tier: T1
Phase: P4
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from typing import Literal
from zoneinfo import ZoneInfo

import psycopg

from pipeline.core.schedule import LOCK_LEAD, kickoff_utc

_ET = ZoneInfo("America/New_York")

# h2h + spreads + totals, us region = 3 credits/call (docs/sources.md, sourced from The
# Odds API's own free-tier pricing page). Every target is costed at this standard call
# for now; a cheaper partial-market call for lower-priority targets is a real future
# option but isn't verified against the live API yet, so it's not modeled here.
STANDARD_CREDITS = 3

# Weekly ceiling: the 6 targets below cost 18 credits in a normal week (every weekday
# anchor present) -- 21 leaves room for exactly one retry after a failed call without
# quietly ballooning past the approved 15-20/week band. Monthly mirrors the Odds API's
# actual 500/mo free-tier reset and is the hard backstop regardless of weekly math.
WEEKLY_CREDIT_CEILING = 21
MONTHLY_CREDIT_CEILING = 500

MissReason = Literal["deadline_passed", "superseded", "weekly_cap", "monthly_cap"]

# How long before its anchor kickoff a pre-kickoff target's window opens (sun_early,
# sun_late, thu_pre_tnf, mon_pre_mnf): the synthesizer's lock lead, LOCK_LEAD (6h). The
# dispatcher runs the odds collector, then the Market analyst, then the synthesizer in
# one tick, so a target opening at the same instant as its slate's lock window means the
# first tick inside that window captures a fresh line and locks against it. The windows
# were 4h wide until 2026 week 3 (first 2h/20min, both too narrow live -- thu_pre_tnf
# closed with zero ticks inside it in week 2), which left 2h at the start of every lock
# window with no target due: week 3's TNF locked at 19:28Z against the Tuesday opener,
# ~51h old, because thu_pre_tnf didn't open until 20:15Z. Opening earlier than the lock
# window would be as bad the other way (a capture that then ages until the lock), so the
# two stay one shared constant rather than two numbers that happen to match.
_PRE_KICKOFF_WINDOW = LOCK_LEAD

# (gameday, weekday, gametime) -- gametime is "HH:MM" 24h ET, games.gametime's format
# (verified live 2026-09-18 against a real week-2 row: DET@BUF Thursday "20:15").
GameRow = tuple[dt.date, str, str]


@dataclass(frozen=True)
class Target:
    target_id: str
    scheduled_for: dt.datetime  # UTC -- window opens here
    deadline: dt.datetime  # UTC -- window closes here; unfired past this point is missed
    credits_estimate: int = STANDARD_CREDITS


@dataclass(frozen=True)
class TargetState:
    target_id: str
    status: Literal["pending", "captured", "missed"] = "pending"
    credits_spent: int = 0


@dataclass(frozen=True)
class Decision:
    fire: Target | None
    missed: list[tuple[str, MissReason]]


def _et(d: dt.date, hour: int, minute: int) -> dt.datetime:
    return dt.datetime(d.year, d.month, d.day, hour, minute, tzinfo=_ET).astimezone(dt.UTC)


def _kickoff(gameday: dt.date, gametime: str) -> dt.datetime:
    return kickoff_utc(gameday, gametime)


def compute_week_targets(games: list[GameRow]) -> list[Target]:
    """Pure -- no DB access. `games` is this season/week's (gameday, weekday, gametime)
    rows. Every deadline is anchored to a real kickoff time from `games` rather than an
    assumed schedule shape (a flexed game, an early international kickoff, or a playoff
    round with a different day layout still gets a correct pre-kickoff deadline instead
    of a guessed one). Window-open times (10:00 ET Tuesday/Saturday, _PRE_KICKOFF_WINDOW
    before the Sun-early/Sun-late/Thu/Mon anchor kickoff) are scheduling policy, not
    sourced facts -- those are the only genuinely chosen numbers here.

    Each pre-kickoff target is anchored to the earliest kickoff of its slate, so it
    opens with that slate's lock window. Later kickoffs in the same slate (a 16:25 after
    a 16:05, or SNF, which counts as sun_late) lock against the anchor's capture, up to
    a few hours old. Windows may overlap (sun_early's with sat_market_movement's last
    hour, sun_late's with sun_early's last ~3h); decide()'s catch-up collapsing fires the
    newest one and marks the other superseded, so an overlap never costs a second call.

    tue_opener closes Wednesday 9:00 ET, not "whenever it eventually fires" -- a window
    left open past that isn't really an opening line any more (live evidence: it once
    ran all the way to Saturday and fired Friday evening, indistinguishable from
    sat_market_movement's own job). A tick-starved week means this target goes
    genuinely uncaptured instead, which auditor.check_odds_targets surfaces -- a real
    miss beats a mislabeled capture.

    Returns up to 6 targets: tue_opener, sat_market_movement, sun_early, sun_late (only
    if a Sunday kickoff falls at/after 15:00 ET that week), thu_pre_tnf (only if a
    Thursday game exists that week), mon_pre_mnf (only if a Monday game exists that
    week). A week with no Sunday game at all (hasn't happened in the modern schedule,
    but not assumed) returns no targets rather than guessing a date."""
    by_weekday: dict[str, list[dt.datetime]] = {}
    for gameday, weekday, gametime in games:
        by_weekday.setdefault(weekday, []).append(_kickoff(gameday, gametime))

    sunday_kickoffs = sorted(by_weekday.get("Sunday", []))
    if not sunday_kickoffs:
        return []
    sunday = sunday_kickoffs[0].astimezone(_ET).date()
    tuesday = sunday - dt.timedelta(days=5)
    wednesday = sunday - dt.timedelta(days=4)
    saturday = sunday - dt.timedelta(days=1)

    targets = [
        Target("tue_opener", _et(tuesday, 10, 0), _et(wednesday, 9, 0)),
        Target("sat_market_movement", _et(saturday, 10, 0), _et(sunday, 8, 0)),
        Target("sun_early", sunday_kickoffs[0] - _PRE_KICKOFF_WINDOW, sunday_kickoffs[0]),
    ]

    late_kickoffs = [k for k in sunday_kickoffs if k.astimezone(_ET).hour >= 15]
    if late_kickoffs:
        earliest_late = min(late_kickoffs)
        targets.append(Target("sun_late", earliest_late - _PRE_KICKOFF_WINDOW, earliest_late))

    for weekday, target_id in (("Thursday", "thu_pre_tnf"), ("Monday", "mon_pre_mnf")):
        kicks = by_weekday.get(weekday)
        if kicks:
            kickoff = min(kicks)
            targets.append(Target(target_id, kickoff - _PRE_KICKOFF_WINDOW, kickoff))

    return targets


def decide(
    targets: list[Target],
    states: dict[str, TargetState],
    now: dt.datetime,
    credits_spent_this_week: int,
    credits_spent_this_month: int,
) -> Decision:
    """Pure. `states` covers only targets already known to the caller (a target with no
    entry is treated as pending -- rows are only inserted into odds_snapshot_targets
    lazily, see ensure_week_targets). Catch-up collapsing: if more than one target's
    window is open at once (a dispatcher gap spanned two windows), only the most
    recently opened one fires -- the other(s) are reported missed as 'superseded' rather
    than each spending a credit on what's now stale history, the same reasoning
    availability_impact.py applies to same-day snapshot reruns not being independent
    data points."""
    missed: list[tuple[str, MissReason]] = []
    open_targets: list[Target] = []
    for target in targets:
        state = states.get(target.target_id, TargetState(target.target_id))
        if state.status != "pending":
            continue
        if now >= target.deadline:
            missed.append((target.target_id, "deadline_passed"))
        elif target.scheduled_for <= now:
            open_targets.append(target)

    if not open_targets:
        return Decision(None, missed)

    due = max(open_targets, key=lambda t: t.scheduled_for)
    for target in open_targets:
        if target.target_id != due.target_id:
            missed.append((target.target_id, "superseded"))

    if credits_spent_this_week + due.credits_estimate > WEEKLY_CREDIT_CEILING:
        missed.append((due.target_id, "weekly_cap"))
        return Decision(None, missed)
    if credits_spent_this_month + due.credits_estimate > MONTHLY_CREDIT_CEILING:
        missed.append((due.target_id, "monthly_cap"))
        return Decision(None, missed)

    return Decision(due, missed)


def _fetch_games(conn: psycopg.Connection, season: int, week: int) -> list[GameRow]:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT gameday, weekday, gametime FROM games "
            "WHERE season = %s AND week = %s AND gameday IS NOT NULL AND gametime IS NOT NULL",
            (season, week),
        )
        return cur.fetchall()


def ensure_week_targets(conn: psycopg.Connection, season: int, week: int) -> list[Target]:
    """Computes this week's target calendar and inserts any not-yet-seen rows as
    'pending'. An existing row is refreshed only while it's still pending, and only its
    window (scheduled_for/deadline -- a calendar-rule change or a flexed kickoff moves
    it); a captured/missed row is never touched, so its stored window stays the one it
    was judged against. Returns the full target list either way, for decide()."""
    targets = compute_week_targets(_fetch_games(conn, season, week))
    if not targets:
        return targets

    rows = [
        (season, week, t.target_id, t.scheduled_for, t.deadline, t.credits_estimate)
        for t in targets
    ]
    with conn.cursor() as cur:
        cur.executemany(
            "INSERT INTO odds_snapshot_targets "
            "(season, week, target_id, scheduled_for, deadline, credits_estimate) "
            "VALUES (%s, %s, %s, %s, %s, %s) "
            "ON CONFLICT (season, week, target_id) DO UPDATE SET "
            "scheduled_for = EXCLUDED.scheduled_for, deadline = EXCLUDED.deadline, "
            "updated_at = now() "
            "WHERE odds_snapshot_targets.status = 'pending' AND ("
            "odds_snapshot_targets.scheduled_for IS DISTINCT FROM EXCLUDED.scheduled_for OR "
            "odds_snapshot_targets.deadline IS DISTINCT FROM EXCLUDED.deadline)",
            rows,
        )
    return targets


def load_states(conn: psycopg.Connection, season: int, week: int) -> dict[str, TargetState]:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT target_id, status, COALESCE(credits_spent, 0) FROM odds_snapshot_targets "
            "WHERE season = %s AND week = %s",
            (season, week),
        )
        return {row[0]: TargetState(row[0], row[1], row[2]) for row in cur.fetchall()}


def credits_spent_this_week(conn: psycopg.Connection, season: int, week: int) -> int:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT COALESCE(SUM(credits_spent), 0) FROM odds_snapshot_targets "
            "WHERE season = %s AND week = %s AND status = 'captured'",
            (season, week),
        )
        row = cur.fetchone()
        assert row is not None  # SUM(...) always returns exactly one row
        return row[0]


def credits_spent_this_month(conn: psycopg.Connection, season: int, now: dt.datetime) -> int:
    """Sums across every week whose captured targets fall in `now`'s calendar month --
    mirrors the Odds API's actual free-tier reset, which is monthly, not per-week/season."""
    month_start = now.astimezone(dt.UTC).replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    with conn.cursor() as cur:
        cur.execute(
            "SELECT COALESCE(SUM(credits_spent), 0) FROM odds_snapshot_targets "
            "WHERE season = %s AND status = 'captured' AND captured_at >= %s",
            (season, month_start),
        )
        row = cur.fetchone()
        assert row is not None  # SUM(...) always returns exactly one row
        return row[0]


def record_capture(
    conn: psycopg.Connection,
    season: int,
    week: int,
    target_id: str,
    credits_spent: int,
    now: dt.datetime,
) -> None:
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE odds_snapshot_targets SET status = 'captured', captured_at = %s, "
            "credits_spent = %s, updated_at = %s "
            "WHERE season = %s AND week = %s AND target_id = %s",
            (now, credits_spent, now, season, week, target_id),
        )


def record_missed(
    conn: psycopg.Connection,
    season: int,
    week: int,
    target_id: str,
    reason: MissReason,
    now: dt.datetime,
) -> None:
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE odds_snapshot_targets SET status = 'missed', missed_reason = %s, "
            "updated_at = %s WHERE season = %s AND week = %s AND target_id = %s",
            (reason, now, season, week, target_id),
        )
