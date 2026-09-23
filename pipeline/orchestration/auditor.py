"""
Job: Check freshness/row-count/null-rate anomalies across pipeline outputs and alert.
Reads: agent_runs, auditor_alerts, plus whichever table each check targets
Writes: auditor_alerts (dedup state); alerts themselves are external (Discord webhook,
        or a printed line as a stub)
Tier: T0
Phase: 1 (skeleton) -- schema-drift detection and the UI-facing staleness feed are not
       built yet; this covers freshness, row-count floors, and null-rate ceilings, the
       three checks that are meaningful with only the ID spine collector running.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import httpx
import psycopg

from pipeline.core.config import get_settings
from pipeline.core.db import get_connection
from pipeline.core.schedule import kickoff_utc, to_gameday

# Max allowed staleness before flagging, matching docs/architecture.md's freshness tiers.
_TIER_MAX_AGE = {
    "T0": timedelta(minutes=15),
    "T1": timedelta(hours=6),
    "T2": timedelta(days=2),
    "T3": timedelta(days=10),
}


@dataclass(frozen=True)
class FreshnessCheck:
    agent: str
    tier: str


@dataclass(frozen=True)
class StalenessResult:
    agent: str
    tier: str
    status: str  # "fresh" | "stale" | "never_run"
    last_success_at: datetime | None
    max_age: timedelta


def check_freshness(
    conn: psycopg.Connection,
    checks: list[FreshnessCheck],
    *,
    now: datetime | None = None,
) -> list[StalenessResult]:
    now = now or datetime.now(UTC)
    results = []
    for check in checks:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT finished_at FROM agent_runs WHERE agent = %s AND status = 'success' "
                "ORDER BY finished_at DESC LIMIT 1",
                (check.agent,),
            )
            row = cur.fetchone()
        max_age = _TIER_MAX_AGE[check.tier]
        if row is None or row[0] is None:
            results.append(StalenessResult(check.agent, check.tier, "never_run", None, max_age))
            continue
        last_success_at = row[0]
        status = "fresh" if (now - last_success_at) <= max_age else "stale"
        results.append(StalenessResult(check.agent, check.tier, status, last_success_at, max_age))
    return results


def check_odds_targets(
    conn: psycopg.Connection, season: int, week: int, now: datetime
) -> list[str]:
    """Odds-specific staleness check, in place of a generic FreshnessCheck (dispatcher.py
    explains why one doesn't fit: odds_schedule.py's targets are legitimately >6h apart
    by design, so a plain elapsed-time-since-last-success check would false-alarm every
    quiet stretch between scheduled windows, not just a real break).

    Scoped to targets whose window has already fully closed (deadline <= now) so a quiet
    stretch before a not-yet-due target (e.g. Wednesday, waiting on Saturday's target)
    never trips this. Returns at most one alert message: an uncaptured target with no
    explained reason ('deadline_passed', or still 'pending' because the dispatcher never
    ticked at all since its deadline passed) is the clear "something's actually broken"
    signal; a target absorbed by catch-up collapsing or a budget cap
    ('superseded'/'weekly_cap'/'monthly_cap') is the scheduler working as designed, not a
    failure by itself, but still surfaced in the same message for visibility. Empty list
    when every due target was captured -- the common, healthy case."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT target_id, status, missed_reason FROM odds_snapshot_targets "
            "WHERE season = %s AND week = %s AND deadline <= %s",
            (season, week, now),
        )
        due = cur.fetchall()

    if not due:
        return []

    captured = [target_id for target_id, status, _ in due if status == "captured"]
    uncaptured = [(target_id, reason) for target_id, status, reason in due if status != "captured"]
    if not uncaptured:
        return []

    explained_reasons = {"superseded", "weekly_cap", "monthly_cap"}
    never_captured = sorted(t for t, reason in uncaptured if reason not in explained_reasons)
    absorbed = sorted(t for t, reason in uncaptured if reason in explained_reasons)

    detail = []
    if never_captured:
        detail.append(f"never captured: {', '.join(never_captured)}")
    if absorbed:
        detail.append(f"missed to catch-up/budget: {', '.join(absorbed)}")

    return [
        f"odds: {len(captured)}/{len(due)} due targets captured this week "
        f"(season {season} week {week}) -- {'; '.join(detail)}"
    ]


def summarize_venue_problems(rows: list[tuple]) -> dict[str, str]:
    """Pure. `rows` are (game_id, stadium_id, games_stadium, known_names | None,
    roof_type | None, games_roof) for every not-yet-played game this season. Returns up
    to two messages keyed 'venue' / 'roof_conflict':

    - venue: games the weather collector's name guard will refuse -- no stadiums row for
      the stadium_id, or games.stadium not among that row's known_names (e.g.
      2026_05_PHI_JAX: JAX00 but "Tottenham Hotspur Stadium"). Fix by correcting the
      upstream row or, after confirming a rename, adding the name to
      reference/stadiums.csv -- never by fetching with the stored coords.
    - roof_conflict: open-air venues where nflverse's games.roof says dome/closed (MCG,
      Stade de France, Munich). Weather is still fetched (structural roof_type wins);
      this is surfaced so the upstream label isn't silently trusted elsewhere."""
    unknown, mismatched, conflicts = [], [], []
    for game_id, stadium_id, games_stadium, known_names, roof_type, games_roof in rows:
        if known_names is None:
            unknown.append(f"{game_id} ({stadium_id})")
        elif games_stadium not in known_names:
            mismatched.append(f"{game_id} ({stadium_id} but '{games_stadium}')")
        elif roof_type == "open" and games_roof in ("dome", "closed"):
            conflicts.append(f"{game_id} ({stadium_id}, games.roof='{games_roof}')")

    out: dict[str, str] = {}
    detail = []
    if mismatched:
        detail.append(f"stadium name not in known_names: {', '.join(sorted(mismatched))}")
    if unknown:
        detail.append(f"stadium_id missing from stadiums: {', '.join(sorted(unknown))}")
    if detail:
        out["venue"] = (
            f"weather: venue guard will skip {len(mismatched) + len(unknown)} "
            f"upcoming game(s) -- {'; '.join(detail)}"
        )
    if conflicts:
        out["roof_conflict"] = (
            "weather: open-air venue labeled dome/closed by nflverse (fetched anyway, "
            f"stadiums.roof_type wins): {', '.join(sorted(conflicts))}"
        )
    return out


def check_venue_problems(conn: psycopg.Connection, season: int, now: datetime) -> dict[str, str]:
    """Runs summarize_venue_problems over every game in `season` from today (ET) on --
    catches a mislabeled game as soon as nflverse publishes it, not only once its
    weather target comes due 48h out."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT g.game_id, g.stadium_id, g.stadium, s.known_names, s.roof_type, g.roof "
            "FROM games g LEFT JOIN stadiums s ON s.stadium_id = g.stadium_id "
            "WHERE g.season = %s AND g.gameday >= %s",
            (season, to_gameday(now)),
        )
        return summarize_venue_problems(cur.fetchall())


def summarize_weather_targets(rows: list[tuple], now: datetime) -> list[str]:
    """Pure. `rows` are per-game aggregates over weather_snapshot_targets: (game_id,
    captured, missed_or_overdue, deliberate_skips, no_forecast_data, last_deadline).

    Individual missed targets are expected and not alerted: GitHub drops many
    scheduled ticks (observed 4.5-5.5h apart), so even the wide late windows get
    missed. The alert is a game that finished its whole schedule (last_deadline
    passed) with zero snapshots and no deliberate skip (fixed/closed roof, or a
    venue-guard skip -- which check_venue_problems already reports) -- i.e. a game
    that simply got no weather. A no_forecast_data skip should
    never happen inside the 48h horizon, so any is reported too."""
    no_weather, no_data = [], []
    for game_id, captured, missed, deliberate, no_forecast, last_deadline in rows:
        if no_forecast:
            no_data.append(game_id)
        if last_deadline <= now and captured == 0 and deliberate == 0 and missed > 0:
            no_weather.append(game_id)
    detail = []
    if no_weather:
        detail.append(f"no snapshot captured at all: {', '.join(sorted(no_weather))}")
    if no_data:
        detail.append(f"Open-Meteo had no data (unexpected <48h out): {', '.join(sorted(no_data))}")
    return [f"weather: {'; '.join(detail)}"] if detail else []


def check_weather_targets(
    conn: psycopg.Connection, season: int, week: int, now: datetime
) -> list[str]:
    """Weather's schedule-aware check, in place of a generic FreshnessCheck -- same
    reasoning as check_odds_targets: runs are deliberately sparse between targets."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT game_id, "
            "count(*) FILTER (WHERE status = 'captured'), "
            "count(*) FILTER (WHERE status = 'missed' OR (status = 'pending' AND deadline <= %s)), "
            "count(*) FILTER (WHERE status = 'skipped' AND skip_reason IN "
            "('fixed_roof', 'roof_closed', 'name_mismatch', 'unknown_stadium')), "
            "count(*) FILTER (WHERE status = 'skipped' AND skip_reason = 'no_forecast_data'), "
            "max(deadline) "
            "FROM weather_snapshot_targets WHERE season = %s AND week = %s GROUP BY game_id",
            (now, season, week),
        )
        return summarize_weather_targets(cur.fetchall(), now)


_LOCK_CHECK_LOOKBACK = timedelta(hours=24)


def summarize_projection_locks(rows: list[tuple], now: datetime) -> list[str]:
    """Pure. `rows` are (game_id, kickoff, locked, projection_status | None) for games
    near now. Alerts on any game with `now - 24h < kickoff <= now` and no projection_log
    row: it kicked off without a locked pre-kickoff claim. The card's last
    projection_status is included as the likely reason (2 awaiting efficiency, 3 an
    input null, 4 model stale; none = no card was ever written). At most one message."""
    missed = sorted(
        f"{game_id} (status {status if status is not None else 'no card'})"
        for game_id, kickoff, locked, status in rows
        if now - _LOCK_CHECK_LOOKBACK < kickoff <= now and not locked
    )
    if not missed:
        return []
    return [
        f"projections: {len(missed)} game(s) kicked off in the last 24h without a locked "
        f"projection -- {', '.join(missed)}"
    ]


def check_projection_locks(conn: psycopg.Connection, now: datetime) -> list[str]:
    """Every game that kicked off in the last 24h must have a projection_log row. The
    synthesizer locks at kickoff - 6h on the first tick it gets; ticks land ~5h apart,
    so a missed lock usually means the synthesizer failed or couldn't project."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT g.game_id, g.gameday, g.gametime, p.game_id IS NOT NULL, "
            "m.projection_status "
            "FROM games g "
            "LEFT JOIN projection_log p ON p.game_id = g.game_id "
            "LEFT JOIN matchup_cards m ON m.game_id = g.game_id "
            "WHERE g.gameday BETWEEN %s AND %s AND g.gametime IS NOT NULL",
            (to_gameday(now - _LOCK_CHECK_LOOKBACK - timedelta(days=1)), to_gameday(now)),
        )
        rows = [
            (game_id, kickoff_utc(gameday, gametime), locked, status)
            for game_id, gameday, gametime, locked, status in cur.fetchall()
        ]
    return summarize_projection_locks(rows, now)


_GRADE_CHECK_DELAY = timedelta(hours=36)
_GRADE_CHECK_LOOKBACK = timedelta(days=7)


def summarize_grades(rows: list[tuple], now: datetime) -> list[str]:
    """Pure. `rows` are (game_id, kickoff, has_score, grade_status | None) for locked
    games. Alerts on any lock that kicked off more than 36h ago (the game plus a day for
    nflverse's post-game schedule) and still isn't graded. The reason tells the two
    failures apart: a final score is stored but the grader hasn't graded it (the grader
    is broken), or no final score has arrived (nflverse lag, or a game not played). At
    most one message."""
    late = sorted(
        f"{game_id} ({'scored, not graded' if has_score else 'no final score yet'}"
        f"{f', {status}' if status else ''})"
        for game_id, kickoff, has_score, status in rows
        if now - _GRADE_CHECK_LOOKBACK < kickoff <= now - _GRADE_CHECK_DELAY
        and status != "graded"
    )
    if not late:
        return []
    return [f"grades: {len(late)} locked game(s) ungraded 36h+ after kickoff -- {', '.join(late)}"]


def check_grades(conn: psycopg.Connection, now: datetime) -> list[str]:
    """Every locked game should be graded within about a day of its final score."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT p.game_id, p.kickoff, g.home_score IS NOT NULL AND g.away_score IS NOT NULL, "
            "pg.grade_status "
            "FROM projection_log p "
            "JOIN games g ON g.game_id = p.game_id "
            "LEFT JOIN projection_grades pg ON pg.game_id = p.game_id "
            "WHERE p.kickoff > %s",
            (now - _GRADE_CHECK_LOOKBACK,),
        )
        rows = cur.fetchall()
    return summarize_grades(rows, now)


def check_availability_outage(conn: psycopg.Connection) -> dict[str, str]:
    """Reads the most recent availability collector run's meta for an outage_guard entry
    -- pipeline/collectors/availability.py skips disappearance detection and records this
    there instead of writing cleared rows when a source's poll returns well under its
    known active roster (pipeline/core/injury_changelog.py's is_source_outage), since an
    outage or truncated response marking the whole roster cleared would otherwise
    silently re-add everyone as "new" first_seen rows on the next good poll.

    Returns {source: message} for every source currently tripped; empty dict (no run yet,
    or a healthy run) is the common case."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT meta FROM agent_runs WHERE agent = 'availability' AND status = 'success' "
            "ORDER BY started_at DESC LIMIT 1"
        )
        row = cur.fetchone()
    if row is None or not row[0]:
        return {}
    outage_guard = row[0].get("outage_guard") or {}
    return {
        source: (
            f"availability: {source} outage guard tripped -- saw {info['seen']}/"
            f"{info['active']} of the known active roster this poll, skipped clearance"
        )
        for source, info in outage_guard.items()
    }


def check_row_count(conn: psycopg.Connection, table: str, min_rows: int) -> bool:
    """True if `table` has at least `min_rows` rows. `table` is always an internal constant."""
    with conn.cursor() as cur:
        cur.execute(f"SELECT count(*) FROM {table}")
        row = cur.fetchone()
        assert row is not None
        return row[0] >= min_rows


def check_null_rate(
    conn: psycopg.Connection, table: str, column: str, max_null_rate: float
) -> bool:
    """True if the fraction of NULLs in `column` is within `max_null_rate`.

    `table`/`column` are always internal constants, never external input.
    """
    with conn.cursor() as cur:
        cur.execute(f"SELECT count(*), count(*) FILTER (WHERE {column} IS NULL) FROM {table}")
        row = cur.fetchone()
        assert row is not None
        total, nulls = row
    if total == 0:
        return True
    return (nulls / total) <= max_null_rate


def send_alert(message: str) -> None:
    """Discord webhook if configured, otherwise a printed stub.

    GitHub-issue alerting (docs/architecture.md's other option) is left to the dispatcher
    workflow itself (`gh issue create`), not to this module -- it would otherwise need
    its own GitHub token wired in just to duplicate what the workflow can do in one line.
    """
    settings = get_settings()
    if settings.discord_webhook_url:
        httpx.post(settings.discord_webhook_url, json={"content": message}, timeout=10)
    else:
        print(f"[auditor] ALERT: {message}")


def _load_alert_message(conn: psycopg.Connection, alert_key: str) -> str | None:
    with conn.cursor() as cur:
        cur.execute("SELECT message FROM auditor_alerts WHERE alert_key = %s", (alert_key,))
        row = cur.fetchone()
        return row[0] if row else None


def _record_alert(conn: psycopg.Connection, alert_key: str, message: str, now: datetime) -> None:
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO auditor_alerts (alert_key, message, last_sent_at) VALUES (%s, %s, %s) "
            "ON CONFLICT (alert_key) DO UPDATE SET message = EXCLUDED.message, "
            "last_sent_at = EXCLUDED.last_sent_at",
            (alert_key, message, now),
        )


def _clear_alert(conn: psycopg.Connection, alert_key: str) -> None:
    with conn.cursor() as cur:
        cur.execute("DELETE FROM auditor_alerts WHERE alert_key = %s", (alert_key,))


def _send_if_new(conn: psycopg.Connection, alert_key: str, message: str, now: datetime) -> bool:
    """Sends `message` under `alert_key` only if it differs from the last message sent
    under that same key -- an unchanged condition (e.g. the same stale table, the same
    uncaptured odds target) stays silent on every later tick instead of re-alerting every
    ~15 minutes until someone fixes it. A key with no live alert this tick is cleared
    (see `_clear_alert` callers below) so a later recurrence, even with identical
    wording, alerts again rather than staying suppressed by a stale row from last time.

    Returns True if a new alert was actually sent (Discord/print), for the caller to
    track whether anything newly fired this run.
    """
    if _load_alert_message(conn, alert_key) == message:
        return False
    send_alert(message)
    _record_alert(conn, alert_key, message, now)
    return True


def audit_and_alert(
    checks: list[FreshnessCheck],
    *,
    odds_season: int | None = None,
    odds_week: int | None = None,
    weather_season: int | None = None,
    weather_week: int | None = None,
) -> bool:
    """Run freshness checks and alert on anything stale or never run, plus the
    odds-specific schedule check (see check_odds_targets) when `odds_season`/
    `odds_week` are given -- odds has no entry in `checks` itself, since a generic
    elapsed-time check doesn't fit its deliberately sparse, calendar-gated cadence.

    Weather gets the same treatment when `weather_season`/`weather_week` are given: the
    venue check (check_venue_problems, every remaining game this season) and the
    schedule-aware target check (check_weather_targets) -- neither fits a generic
    elapsed-time FreshnessCheck.

    Every run also checks projection locks (check_projection_locks): any game that
    kicked off in the last 24h without a projection_log row. And grades (check_grades):
    any lock still ungraded 36h after kickoff.

    Alerts are deduped per check (see `_send_if_new`) so a persistent condition posts
    once, not on every dispatcher tick. Returns True if a *new* alert was sent this run
    (for the dispatcher to decide whether to also file a GitHub issue) -- this is
    deliberately NOT "everything is healthy": auditor findings are alerts, not run
    failures, and must never affect the dispatcher's exit code (see dispatcher.py).
    """
    alerted = False
    now = datetime.now(UTC)
    with get_connection() as conn:
        for result in check_freshness(conn, checks, now=now):
            alert_key = f"freshness:{result.agent}"
            if result.status == "fresh":
                _clear_alert(conn, alert_key)
                continue
            message = (
                f"{result.agent} ({result.tier}) is {result.status} -- "
                f"last success: {result.last_success_at}"
            )
            alerted = _send_if_new(conn, alert_key, message, now) or alerted

        if odds_season is not None and odds_week is not None:
            alert_key = f"odds:{odds_season}:{odds_week}"
            messages = check_odds_targets(conn, odds_season, odds_week, now)
            if not messages:
                _clear_alert(conn, alert_key)
            else:
                # check_odds_targets returns at most one message (see its docstring).
                alerted = _send_if_new(conn, alert_key, messages[0], now) or alerted

        if weather_season is not None and weather_week is not None:
            venue = check_venue_problems(conn, weather_season, now)
            for kind in ("venue", "roof_conflict"):
                alert_key = f"weather_{kind}:{weather_season}"
                if kind not in venue:
                    _clear_alert(conn, alert_key)
                    continue
                alerted = _send_if_new(conn, alert_key, venue[kind], now) or alerted

            alert_key = f"weather_targets:{weather_season}:{weather_week}"
            messages = check_weather_targets(conn, weather_season, weather_week, now)
            if not messages:
                _clear_alert(conn, alert_key)
            else:
                # summarize_weather_targets returns at most one message.
                alerted = _send_if_new(conn, alert_key, messages[0], now) or alerted

        # Rolling 24h window, so one key: the message changes as games enter/leave it.
        messages = check_projection_locks(conn, now)
        if not messages:
            _clear_alert(conn, "projection_locks")
        else:
            alerted = _send_if_new(conn, "projection_locks", messages[0], now) or alerted

        messages = check_grades(conn, now)
        if not messages:
            _clear_alert(conn, "projection_grades")
        else:
            alerted = _send_if_new(conn, "projection_grades", messages[0], now) or alerted

        outages = check_availability_outage(conn)
        for source in ("espn", "sleeper"):
            alert_key = f"availability_outage:{source}"
            if source not in outages:
                _clear_alert(conn, alert_key)
                continue
            alerted = _send_if_new(conn, alert_key, outages[source], now) or alerted
        conn.commit()
    return alerted
