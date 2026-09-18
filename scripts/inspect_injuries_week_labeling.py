"""One-off script: check whether any injuries row's stored (season, week) is wrong,
given the timezone bug fixed in pipeline/core/schedule.py's resolve_season_week (it
took .date() on a raw UTC datetime instead of converting to ET first, so a timestamp
between ~8pm and midnight ET -- already the next UTC calendar day -- could resolve
against the wrong week's window).

Calls the SAME production functions the collector uses (pipeline.core.schedule's
to_gameday/resolve_season_week, imported and called here, never reimplemented in
hand-written SQL) -- see CLAUDE.md's note on verification scripts.

Two different rigor levels, because of what's actually retrievable after the fact:

- Sleeper rows: season/week was resolved from ctx.now, which IS the stored `as_of`
  value (pipeline/collectors/availability.py's store()). Fully exact -- recomputing
  resolve_season_week(conn, as_of) with the fix and comparing to the stored
  season/week is a rigorous check, not an approximation.

- ESPN rows: season/week was resolved from ESPN's own feed `timestamp` field
  (`espn_timestamp`), which is NOT stored anywhere on the row (the raw jsonb subtree
  drops it, see availability.py's _espn_raw_subtree) and can't be reconstructed. This
  script instead checks the only thing it CAN verify: whether `as_of` (the fetch time,
  necessarily close to but not identical to espn_timestamp) falls in the danger window
  at all. If it never does, for any distinct as_of on record, that's strong circumstantial
  evidence the bug never actually fired for ESPN rows either -- reported as such, not
  claimed as proof.

Not part of the pipeline. Read-only.

Usage:
  uv run python scripts/inspect_injuries_week_labeling.py
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pipeline.core.db import get_connection  # noqa: E402
from pipeline.core.schedule import resolve_season_week, to_gameday  # noqa: E402


def main() -> None:
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT DISTINCT source, as_of, season, week FROM injuries ORDER BY as_of"
            )
            rows = cur.fetchall()

        distinct_as_of = sorted({as_of for _source, as_of, _season, _week in rows})
        print(f"{len(distinct_as_of)} distinct as_of value(s) across {len(rows)} (source, as_of) "
              f"combinations in injuries.\n")

        any_in_danger_window = False
        any_mismatch = False

        for as_of in distinct_as_of:
            raw_utc_date = as_of.date()
            et_gameday = to_gameday(as_of)
            in_danger_window = raw_utc_date != et_gameday
            any_in_danger_window = any_in_danger_window or in_danger_window

            correct_season, correct_week, _season_type = resolve_season_week(conn, as_of)

            flag = "** IN DANGER WINDOW **" if in_danger_window else "(safe)"
            print(
                f"as_of={as_of.isoformat()}  raw_utc_date={raw_utc_date}  "
                f"et_gameday={et_gameday}  {flag}"
            )
            print(
                f"  fixed resolve_season_week(as_of) -> "
                f"season={correct_season} week={correct_week}"
            )

            for source in ("espn", "sleeper"):
                stored_for_source = {
                    (season, week)
                    for src, row_as_of, season, week in rows
                    if row_as_of == as_of and src == source
                }
                if not stored_for_source:
                    continue
                for season, week in stored_for_source:
                    if source == "sleeper":
                        # exact check -- sleeper's stored season/week came from this
                        # same as_of value
                        if (season, week) != (correct_season, correct_week):
                            any_mismatch = True
                            print(
                                f"  MISMATCH (sleeper, exact): stored season={season} "
                                f"week={week} != correct season={correct_season} "
                                f"week={correct_week}"
                            )
                        else:
                            print(
                                f"  OK (sleeper, exact): stored season={season} "
                                f"week={week} matches"
                            )
                    else:
                        # approximate -- espn's stored season/week came from ESPN's own
                        # espn_timestamp, not as_of; only flag if as_of itself was ever
                        # in the danger window, which is the only thing checkable here
                        if not in_danger_window:
                            note = " -- as_of never in danger window, no evidence of the bug"
                        else:
                            note = (
                                " -- as_of WAS in the danger window, espn_timestamp "
                                "unverifiable, worth a closer look"
                            )
                        print(f"  (espn, approximate): stored season={season} week={week}{note}")
            print()

        print("=" * 72)
        if not any_in_danger_window:
            print("No distinct as_of value ever fell in the ET-8pm-to-midnight danger "
                  "window. Sleeper rows are provably unaffected (their season/week came "
                  "directly from as_of). ESPN rows can't be proven clean -- their "
                  "driving espn_timestamp isn't retrievable -- but nothing observed "
                  "suggests the bug ever fired.")
        if any_mismatch:
            print("MISMATCHES FOUND -- see above. These rows' season/week need re-labeling.")
        else:
            print("No exact mismatches found among sleeper rows (the only rows this "
                  "script can check exactly).")


if __name__ == "__main__":
    main()
