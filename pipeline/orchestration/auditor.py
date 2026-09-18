"""
Job: Check freshness/row-count/null-rate anomalies across pipeline outputs and alert.
Reads: agent_runs, plus whichever table each check targets
Writes: nothing (alerts are external: Discord webhook, or a printed line as a stub)
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
    workflow itself (`gh issue create` on a non-zero exit), not to this module -- it
    would otherwise need its own GitHub token wired in just to duplicate what the
    workflow can do in one line.
    """
    settings = get_settings()
    if settings.discord_webhook_url:
        httpx.post(settings.discord_webhook_url, json={"content": message}, timeout=10)
    else:
        print(f"[auditor] ALERT: {message}")


def audit_and_alert(
    checks: list[FreshnessCheck],
    *,
    odds_season: int | None = None,
    odds_week: int | None = None,
) -> bool:
    """Run freshness checks and alert on anything stale or never run, plus the
    odds-specific schedule check (see check_odds_targets) when `odds_season`/
    `odds_week` are given -- odds has no entry in `checks` itself, since a generic
    elapsed-time check doesn't fit its deliberately sparse, calendar-gated cadence.

    Returns True if everything is healthy (for the dispatcher's exit code).
    """
    healthy = True
    now = datetime.now(UTC)
    with get_connection() as conn:
        for result in check_freshness(conn, checks, now=now):
            if result.status != "fresh":
                healthy = False
                send_alert(
                    f"{result.agent} ({result.tier}) is {result.status} -- "
                    f"last success: {result.last_success_at}"
                )
        if odds_season is not None and odds_week is not None:
            for message in check_odds_targets(conn, odds_season, odds_week, now):
                healthy = False
                send_alert(message)
    return healthy
