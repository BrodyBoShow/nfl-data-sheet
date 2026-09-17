"""
Job: Entry point for the GitHub Actions cron -- runs every registered collector/analyst
     and then the auditor.
Reads: nothing directly (delegates to each job's should_run/inputs_ready)
Writes: nothing directly (delegates to each job)
Tier: T0
Phase: 1 (skeleton) -- full calendar-aware triggering (docs/architecture.md's Dispatcher
       calendar, e.g. "don't even try the odds collector outside its windows") is Phase 8
       tuning work. For now every registered job runs on every ~10 min tick, and each
       job's own should_run()/inputs_ready() decides whether there's anything to do --
       cheap for the jobs that exist today (one HTTP freshness check apiece).
"""

from __future__ import annotations

import sys

import nflreadpy as nfl

from pipeline.collectors.id_spine import IdSpineCollector
from pipeline.collectors.nflverse_bulk import NflverseBulkCollector
from pipeline.orchestration.auditor import FreshnessCheck, audit_and_alert

_COLLECTORS = [IdSpineCollector(), NflverseBulkCollector()]

_FRESHNESS_CHECKS = [
    FreshnessCheck(agent="id_spine", tier="T2"),
    FreshnessCheck(agent="nflverse_bulk", tier="T2"),
]


def main() -> int:
    season = nfl.get_current_season()
    week = nfl.get_current_week()

    for collector in _COLLECTORS:
        collector.run(season=season, week=week)

    healthy = audit_and_alert(_FRESHNESS_CHECKS)
    return 0 if healthy else 1


if __name__ == "__main__":
    sys.exit(main())
