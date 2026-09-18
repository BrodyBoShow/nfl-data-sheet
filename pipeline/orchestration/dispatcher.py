"""
Job: Entry point for the GitHub Actions cron -- runs every registered collector, then
     every registered analyst, then the auditor.
Reads: nothing directly (delegates to each job's should_run/inputs_ready)
Writes: nothing directly (delegates to each job)
Tier: T0
Phase: 1 (skeleton) -- full calendar-aware triggering (docs/architecture.md's Dispatcher
       calendar, e.g. "don't even try the odds collector outside its windows") is Phase 8
       tuning work. For now every registered collector runs on every tick, and each
       one's own should_run() decides whether there's anything to do -- cheap for the
       jobs that exist today (one HTTP freshness check apiece). The cron aims for
       roughly every 15 minutes, but GitHub Actions' free-tier scheduled triggers are
       best-effort on shared runners, not a guarantee (.github/workflows/dispatcher.yml
       -- observed live: only 1 of an expected ~18 ticks over 3 hours actually fired) --
       nothing here may assume a tick happened recently, or that ticks are evenly
       spaced, or even that one happens at all in a given window. should_run()/
       inputs_ready() are built accordingly: they decide what's due from stored
       freshness state compared against a live/current value or absolute now, never
       from how much time or how many ticks have passed since the last run. Analysts
       are gated differently: an analyst only re-runs if at least one collector
       actually wrote new/changed rows THIS tick (RunResult.rows_written > 0), not via
       its own inputs_ready() -- a tick where every collector returns skipped_fresh (or
       a 0-row success, e.g. a freshness-gate false alarm that hash-diffed to no real
       changes) skips every analyst entirely, so a quiet tick doesn't recompute every
       signal for no reason -- regardless of how long it's been since the last one.
       Chosen over adding a freshness-marker mechanism to each Analyst because the
       information is already free from this tick's own collector results and needs no
       change to the Analyst contract; the tradeoff is that a fully quiet tick writes no
       agent_runs row at all for any analyst (by design -- nothing was attempted, so
       there's nothing to log), which the collectors' own skipped_fresh/0-row rows
       already explain if that's ever in question.
"""

from __future__ import annotations

import sys
from collections.abc import Sequence
from typing import Protocol

import nflreadpy as nfl

from pipeline.analysts.availability_impact import AvailabilityImpactAnalyst
from pipeline.analysts.efficiency import EfficiencyAnalyst
from pipeline.collectors.availability import AvailabilityCollector
from pipeline.collectors.id_spine import IdSpineCollector
from pipeline.collectors.nflverse_bulk import NflverseBulkCollector
from pipeline.collectors.odds import OddsCollector
from pipeline.core.base import Analyst, Collector, RunResult
from pipeline.orchestration.auditor import FreshnessCheck, audit_and_alert


class _RunnableJob(Protocol):
    """Structural stand-in for Collector/Analyst's shared `run(season, week)` shape, so
    _run_tick (and its tests) don't care which ABC a job actually implements."""

    def run(self, *, season: int, week: int) -> RunResult: ...


_COLLECTORS: list[Collector] = [
    IdSpineCollector(),
    NflverseBulkCollector(),
    AvailabilityCollector(),
    OddsCollector(),
]
_ANALYSTS: list[Analyst] = [EfficiencyAnalyst(), AvailabilityImpactAnalyst()]

_FRESHNESS_CHECKS = [
    FreshnessCheck(agent="id_spine", tier="T2"),
    FreshnessCheck(agent="nflverse_bulk", tier="T2"),
    FreshnessCheck(agent="efficiency", tier="T2"),
    FreshnessCheck(agent="availability", tier="T1"),
    FreshnessCheck(agent="availability_impact", tier="T1"),
    # odds deliberately has no FreshnessCheck here -- check_freshness/_TIER_MAX_AGE
    # assumes a roughly regular per-tier cadence (T1 = flag past 6h since last success),
    # but odds_schedule.py's targets are legitimately >6h apart by design (e.g.
    # mon_pre_mnf to the next tue_opener, or tue_opener to sat_market_movement) -- a
    # plain tier-based check here would false-alarm on every quiet stretch between
    # scheduled windows, not just a real break. It gets its own schedule-aware check
    # instead (auditor.check_odds_targets, wired below via audit_and_alert's
    # odds_season/odds_week), which alerts only on a target whose own deadline passed
    # uncaptured or a week short of its expected capture count.
]


def _run_tick(
    collectors: Sequence[_RunnableJob],
    analysts: Sequence[_RunnableJob],
    *,
    season: int,
    week: int,
) -> None:
    """Run every collector, then every analyst -- but only if at least one collector
    wrote new/changed rows this tick (see module docstring for why)."""
    collector_results = [c.run(season=season, week=week) for c in collectors]
    if any(result.rows_written > 0 for result in collector_results):
        for analyst in analysts:
            analyst.run(season=season, week=week)


def main() -> int:
    season = nfl.get_current_season()
    week = nfl.get_current_week()

    _run_tick(_COLLECTORS, _ANALYSTS, season=season, week=week)

    healthy = audit_and_alert(_FRESHNESS_CHECKS, odds_season=season, odds_week=week)
    return 0 if healthy else 1


if __name__ == "__main__":
    sys.exit(main())
