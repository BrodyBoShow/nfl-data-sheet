"""
Job: Manual CLI to run a single named collector/analyst outside the dispatcher's tick,
     for local debugging and backfills (`uv run python -m pipeline.run <job> [flags]`).
Reads: nothing directly (delegates to the named job)
Writes: nothing directly (delegates to the named job)
Tier: n/a
Phase: P1
"""

from __future__ import annotations

import sys

import nflreadpy as nfl

from pipeline.analysts.availability_impact import AvailabilityImpactAnalyst
from pipeline.analysts.efficiency import EfficiencyAnalyst
from pipeline.collectors.availability import AvailabilityCollector
from pipeline.collectors.id_spine import IdSpineCollector
from pipeline.collectors.nflverse_bulk import NflverseBulkCollector
from pipeline.collectors.odds import OddsCollector
from pipeline.core.base import Analyst, Collector

_JOBS: dict[str, Collector | Analyst] = {
    "id_spine": IdSpineCollector(),
    "nflverse_bulk": NflverseBulkCollector(),
    "efficiency": EfficiencyAnalyst(),
    "availability": AvailabilityCollector(),
    "availability_impact": AvailabilityImpactAnalyst(),
    "odds": OddsCollector(),
}

_USAGE = (
    "usage: uv run python -m pipeline.run <job> [--force] [--season N] [--week N] "
    "[--seasons START-END] [--datasets a,b,c]\n"
    "  --seasons: nflverse_bulk or id_spine (id_spine only widens its season-scoped "
    "games/schedules fetch -- teams/players/ff_playerids are unaffected, see "
    "IdSpineCollector.__init__)\n"
    "  --datasets: nflverse_bulk only"
)


_ParsedArgs = tuple[str, bool, "int | None", "int | None", "str | None", "str | None"]


def _parse_args(argv: list[str]) -> _ParsedArgs | None:
    """Returns (job, force, season, week, seasons_range, datasets) or None on bad args."""
    force = False
    season: int | None = None
    week: int | None = None
    seasons_range: str | None = None
    datasets: str | None = None
    positional: list[str] = []

    i = 0
    while i < len(argv):
        arg = argv[i]
        if arg == "--force":
            force = True
            i += 1
        elif arg == "--season" and i + 1 < len(argv):
            season = int(argv[i + 1])
            i += 2
        elif arg == "--week" and i + 1 < len(argv):
            week = int(argv[i + 1])
            i += 2
        elif arg == "--seasons" and i + 1 < len(argv):
            seasons_range = argv[i + 1]
            i += 2
        elif arg == "--datasets" and i + 1 < len(argv):
            datasets = argv[i + 1]
            i += 2
        else:
            positional.append(arg)
            i += 1

    if len(positional) != 1 or positional[0] not in _JOBS:
        return None
    return positional[0], force, season, week, seasons_range, datasets


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    parsed = _parse_args(argv)
    if parsed is None:
        names = ", ".join(sorted(_JOBS))
        print(_USAGE, file=sys.stderr)
        print(f"available jobs: {names}", file=sys.stderr)
        return 1
    job_name, force, season_arg, week_arg, seasons_range, datasets_arg = parsed

    if datasets_arg and job_name != "nflverse_bulk":
        print("--datasets is only valid for the nflverse_bulk job", file=sys.stderr)
        return 1
    if seasons_range and job_name not in ("nflverse_bulk", "id_spine"):
        print("--seasons is only valid for the nflverse_bulk/id_spine jobs", file=sys.stderr)
        return 1

    seasons_override = None
    if seasons_range:
        start, end = seasons_range.split("-")
        seasons_override = list(range(int(start), int(end) + 1))

    job = _JOBS[job_name]
    if isinstance(job, NflverseBulkCollector) and (seasons_range or datasets_arg):
        datasets = set(datasets_arg.split(",")) if datasets_arg else None
        try:
            job = NflverseBulkCollector(seasons_override=seasons_override, datasets=datasets)
        except ValueError as exc:
            print(f"--datasets error: {exc}", file=sys.stderr)
            return 1
    elif isinstance(job, IdSpineCollector) and seasons_range:
        job = IdSpineCollector(seasons_override=seasons_override)

    season = season_arg if season_arg is not None else nfl.get_current_season()
    week = week_arg if week_arg is not None else nfl.get_current_week()
    result = job.run(season=season, week=week, force=force)

    if result.status == "success":
        rows = f"{result.rows_written:,} rows written"
        print(f"{result.name}: success, {rows}, {result.duration_s:.1f}s")
    elif result.status in ("skipped_fresh", "skipped_no_prior", "skipped_no_injuries"):
        print(f"{result.name}: {result.status}")
    else:
        print(f"{result.name}: {result.status} - {result.error}")

    ok_statuses = ("success", "skipped_fresh", "skipped_no_prior", "skipped_no_injuries")
    return 0 if result.status in ok_statuses else 1


if __name__ == "__main__":
    sys.exit(main())
