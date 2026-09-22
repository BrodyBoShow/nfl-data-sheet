"""One-off script: compact the existing injuries table in place, from "every poll for
every player" to the change-log shape (see pipeline/core/injury_changelog.py) -- deletes
rows that add nothing beyond a repeat of the last stored state, and inserts the
synthetic is_cleared rows that history implies (every "present in poll batch N, absent
from batch N+1" transition, timestamped at batch N+1's as_of). Uses replay_history, the
SAME function scripts/verify_injury_changelog.py uses to prove this rule doesn't change
what availability_impact.py computes -- run that script first and confirm zero diffs
before running this one with --commit.

Defaults to a dry run (prints the row counts this would produce, changes nothing).
--commit is required to actually delete/insert. When committing, first creates
injuries_backup_pre_changelog (a plain data copy, skipped if it already exists) -- kept
until a human explicitly says to drop it; this script never drops it itself.

Not part of the pipeline. Only run --commit after: (1) db/migrations/0016 has been
applied, (2) scripts/verify_injury_changelog.py shows zero diffs, (3) the dispatcher
workflow is disabled for the duration (a live poll writing new rows mid-backfill isn't
itself unsafe -- decide_injury_row's logic is idempotent per-row -- but keeping the
table still during a one-time historical compaction is the simpler, safer thing to ask
for, per this feature's rollout plan).

Usage:
  uv run python scripts/backfill_injuries_changelog.py           # dry run
  uv run python scripts/backfill_injuries_changelog.py --commit  # actually compact
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import psycopg  # noqa: E402

from pipeline.core.db import get_connection, upsert_rows  # noqa: E402
from pipeline.core.hashing import hash_row  # noqa: E402
from pipeline.core.injury_changelog import replay_history  # noqa: E402

_INJURIES_COLUMNS = [
    "player_id",
    "source",
    "source_player_id",
    "season",
    "week",
    "season_type",
    "team",
    "designation",
    "body_part",
    "notes",
    "raw",
    "as_of",
    "is_cleared",
]
_INJURIES_PK = ["source", "source_player_id", "as_of"]
_HASH_FIELDS = [c for c in _INJURIES_COLUMNS if c not in _INJURIES_PK]
_BACKUP_TABLE = "injuries_backup_pre_changelog"


def _fetch_all_injuries_rows(conn: psycopg.Connection) -> list[dict[str, Any]]:
    with conn.cursor() as cur:
        cur.execute(f"SELECT {', '.join(_INJURIES_COLUMNS)} FROM injuries")
        rows = cur.fetchall()
    return [dict(zip(_INJURIES_COLUMNS, r, strict=True)) for r in rows]


def _pk(row: dict[str, Any]) -> tuple[Any, ...]:
    return tuple(row[c] for c in _INJURIES_PK)


def _prep_cleared_row_for_insert(row: dict[str, Any], now: datetime) -> dict[str, Any]:
    row = dict(row)
    row["raw"] = json.dumps(row["raw"], sort_keys=True)
    row["content_hash"] = hash_row({k: row.get(k) for k in _HASH_FIELDS})
    row["updated_at"] = now
    return row


def _backup_exists(conn: psycopg.Connection) -> bool:
    with conn.cursor() as cur:
        cur.execute("SELECT to_regclass(%s)", (f"public.{_BACKUP_TABLE}",))
        row = cur.fetchone()
    return row is not None and row[0] is not None


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--commit", action="store_true", help="Actually delete/insert (default: dry run)"
    )
    args = parser.parse_args()
    now = datetime.now(UTC)

    with get_connection() as conn:
        all_rows = _fetch_all_injuries_rows(conn)
        kept, synthetic_cleared = replay_history(all_rows)
        kept_pks = {_pk(r) for r in kept}
        would_drop = [r for r in all_rows if _pk(r) not in kept_pks]

        print(f"live rows:            {len(all_rows)}")
        print(f"would keep:           {len(kept)}")
        print(f"would drop:           {len(would_drop)}")
        print(f"synthetic cleared:    {len(synthetic_cleared)}")
        print(f"resulting row count:  {len(kept) + len(synthetic_cleared)}")

        if not args.commit:
            print("\nDRY RUN -- no changes made. Re-run with --commit to apply.")
            return

        if _backup_exists(conn):
            print(f"\n{_BACKUP_TABLE} already exists -- leaving it as-is, not overwriting.")
        else:
            print(f"\nCreating {_BACKUP_TABLE} ...")
            with conn.cursor() as cur:
                cur.execute(f"CREATE TABLE {_BACKUP_TABLE} AS TABLE injuries")

        drop_pks = [_pk(r) for r in would_drop]
        if drop_pks:
            # psycopg has no adapter for an anonymous composite type, so a list of
            # (source, source_player_id, as_of) tuples can't go through ANY(%s) as a
            # single bound array -- unnest three parallel typed arrays into a row set
            # instead and join against that.
            with conn.cursor() as cur:
                cur.execute(
                    "DELETE FROM injuries USING unnest(%s::text[], %s::text[], "
                    "%s::timestamptz[]) AS t(source, source_player_id, as_of) "
                    "WHERE injuries.source = t.source "
                    "AND injuries.source_player_id = t.source_player_id "
                    "AND injuries.as_of = t.as_of",
                    (
                        [pk[0] for pk in drop_pks],
                        [pk[1] for pk in drop_pks],
                        [pk[2] for pk in drop_pks],
                    ),
                )
        print(f"Deleted {len(drop_pks)} redundant rows.")

        if synthetic_cleared:
            cleared_rows = [_prep_cleared_row_for_insert(r, now) for r in synthetic_cleared]
            written = upsert_rows(
                conn,
                "injuries",
                cleared_rows,
                conflict_cols=_INJURIES_PK,
                update_cols=[*_HASH_FIELDS, "content_hash", "updated_at"],
            )
            print(f"Inserted {written} synthetic cleared rows.")

        conn.commit()

        with conn.cursor() as cur:
            cur.execute("SELECT count(*), pg_total_relation_size('injuries') FROM injuries")
            count, size_bytes = cur.fetchone()
        print(f"\ninjuries now has {count} rows ({size_bytes / 1024 / 1024:.1f} MB).")


if __name__ == "__main__":
    main()
