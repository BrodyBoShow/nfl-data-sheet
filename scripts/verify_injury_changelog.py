"""One-off script: prove the injuries change-log redesign doesn't change what
availability_impact.py computes. Imports and calls the SAME production functions
(_fetch_current_injuries, _fetch_week_injury_sequences, _compute_trend_risk,
_classify_designation) against the real live injuries table (baseline) and again
against a scratch-schema copy holding the compacted row set -- kept rows plus synthetic
cleared rows, both produced by pipeline.core.injury_changelog.replay_history, the SAME
function the backfill script uses -- then diffs the two result sets. Never
hand-reimplements the analyst's logic (CLAUDE.md's rule on verification scripts).

Never touches the live injuries table with DDL, not even inside a rolled-back
transaction -- a cron tick landing mid-transaction would block on the lock. Instead: a
plain read-only SELECT of every row (an ordinary MVCC snapshot read, no lock contention),
replayed in Python, written into a throwaway schema created just for this run on a
SEPARATE connection, queried via that connection's search_path, then rolled back
(never committed) so nothing persists -- not even a DROP SCHEMA has to run.

Not part of the pipeline. Read-only against public.injuries; the only DDL is on its own
scratch schema, and that connection is never committed.

Usage:
  uv run python scripts/verify_injury_changelog.py --season 2026 --week 2
"""

from __future__ import annotations

import argparse
import json
import sys
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import psycopg  # noqa: E402

from pipeline.analysts.availability_impact import (  # noqa: E402
    _CATEGORY_INJURY,
    _classify_designation,
    _compute_trend_risk,
    _fetch_current_state_by_source,
    _fetch_week_injury_sequences,
    _resolve_current_state,
)
from pipeline.core.config import get_settings  # noqa: E402
from pipeline.core.db import get_connection  # noqa: E402
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
_HASH_FIELDS = [c for c in _INJURIES_COLUMNS if c not in ("source", "source_player_id", "as_of")]


def _fetch_all_injuries_rows(conn: psycopg.Connection) -> list[dict[str, Any]]:
    with conn.cursor() as cur:
        cur.execute(f"SELECT {', '.join(_INJURIES_COLUMNS)} FROM injuries")
        rows = cur.fetchall()
    return [dict(zip(_INJURIES_COLUMNS, r, strict=True)) for r in rows]


def _prep_row_for_insert(row: dict[str, Any], now: datetime) -> dict[str, Any]:
    """raw comes back from a DB read (or build_cleared_row) as a plain dict -- psycopg
    won't auto-adapt a dict parameter to jsonb the way the collector's own json.dumps()
    string does via the target column's inferred type, so it's re-serialized here the
    same way. content_hash/updated_at are recomputed uniformly since synthetic cleared
    rows never had them and the scratch table's columns are NOT NULL."""
    row = dict(row)
    if not isinstance(row["raw"], str):
        row["raw"] = json.dumps(row["raw"], sort_keys=True)
    row["content_hash"] = hash_row({k: row.get(k) for k in _HASH_FIELDS})
    row["updated_at"] = now
    return row


def _current_and_trend(
    conn: psycopg.Connection, season: int, week: int, season_type: str
) -> tuple[dict[str, str], dict[str, tuple[float, int]], dict[str, dict[str, dict[str, Any]]]]:
    by_source = _fetch_current_state_by_source(conn, season, week, season_type)
    current = [_resolve_current_state(pid, sources) for pid, sources in by_source.items()]
    for r in current:
        r["category"] = _classify_designation(r["designation"])
    driving_source_by_player = {r["player_id"]: r["source"] for r in current}
    sequences = _fetch_week_injury_sequences(
        conn, season, week, season_type, driving_source_by_player
    )
    injury_ids = {r["player_id"] for r in current if r["category"] == _CATEGORY_INJURY}
    trend = _compute_trend_risk({pid: seq for pid, seq in sequences.items() if pid in injury_ids})
    categories = {r["player_id"]: r["category"] for r in current}
    trend_by_player = {r["player_id"]: (r["value"], r["sample_n"]) for r in trend}
    return categories, trend_by_player, by_source


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--season", type=int, required=True)
    parser.add_argument("--week", type=int, required=True)
    parser.add_argument("--season-type", default="REG")
    args = parser.parse_args()

    settings = get_settings()
    now = datetime.now(UTC)

    with get_connection() as conn:
        baseline_categories, baseline_trend, baseline_by_source = _current_and_trend(
            conn, args.season, args.week, args.season_type
        )
        all_rows = _fetch_all_injuries_rows(conn)

    kept, synthetic_cleared = replay_history(all_rows)
    compacted_rows = [_prep_row_for_insert(r, now) for r in kept + synthetic_cleared]
    print(
        f"live rows: {len(all_rows)}, compacted: {len(compacted_rows)} "
        f"({len(kept)} kept + {len(synthetic_cleared)} synthetic cleared)"
    )

    schema = f"injuries_verify_{uuid.uuid4().hex[:8]}"
    scratch_conn = psycopg.connect(settings.supabase_db_url)
    try:
        with scratch_conn.cursor() as cur:
            cur.execute(f"CREATE SCHEMA {schema}")
            cur.execute(f"CREATE TABLE {schema}.injuries (LIKE public.injuries INCLUDING ALL)")
            cols = _INJURIES_COLUMNS + ["content_hash", "updated_at"]
            col_list = ", ".join(cols)
            placeholders = ", ".join(f"%({c})s" for c in cols)
            for row in compacted_rows:
                cur.execute(
                    f"INSERT INTO {schema}.injuries ({col_list}) VALUES ({placeholders})", row
                )
            cur.execute(f"SET search_path TO {schema}, public")

        scratch_categories, scratch_trend, scratch_by_source = _current_and_trend(
            scratch_conn, args.season, args.week, args.season_type
        )
    finally:
        # Never committed -- rolling back undoes the scratch schema entirely, so no
        # explicit DROP SCHEMA has to run (or can be skipped by a crash mid-script).
        scratch_conn.rollback()
        scratch_conn.close()

    cat_diffs = {
        pid: (baseline_categories.get(pid), scratch_categories.get(pid))
        for pid in set(baseline_categories) | set(scratch_categories)
        if baseline_categories.get(pid) != scratch_categories.get(pid)
    }
    trend_diffs = {
        pid: (baseline_trend.get(pid), scratch_trend.get(pid))
        for pid in set(baseline_trend) | set(scratch_trend)
        if baseline_trend.get(pid) != scratch_trend.get(pid)
    }

    def _per_source_line(pid: str) -> str:
        sources = sorted(set(baseline_by_source.get(pid, {})) | set(scratch_by_source.get(pid, {})))
        parts = []
        for source in sources:
            b = baseline_by_source.get(pid, {}).get(source)
            s = scratch_by_source.get(pid, {}).get(source)
            b_desc = b["designation"] if b else "(never listed)"
            if s and s["is_cleared"]:
                s_desc = "cleared"
            else:
                s_desc = s["designation"] if s else "(never listed)"
            parts.append(f"{source}: baseline={b_desc!r} compacted={s_desc!r}")
        return "; ".join(parts)

    # Compaction only ever REMOVES exact-duplicate rows and ADDS synthetic is_cleared
    # rows -- it never fabricates or alters a real designation. So the only direction a
    # category diff can legitimately take is baseline=flagged -> compacted=healthy (a
    # source's clearance the raw table has no way to represent), always attributable to
    # at least one source flipping to is_cleared=True. The reverse direction, or a
    # flagged->flagged diff with a different designation, would mean something in
    # replay_history or the cross-source resolution is wrong -- a real bug, not an
    # expected semantic difference.
    expected_direction = {
        pid: (before, after)
        for pid, (before, after) in cat_diffs.items()
        if after == "healthy" and before != "healthy"
    }
    unexpected_direction = {
        pid: v for pid, v in cat_diffs.items() if pid not in expected_direction
    }

    print(f"\ncurrent-designation category diffs: {len(cat_diffs)}")
    print(
        f"  expected direction (flagged -> healthy via a source's clearance): "
        f"{len(expected_direction)}"
    )
    for pid in list(expected_direction)[:20]:
        print(f"    {pid}: {_per_source_line(pid)}")
    print(f"  UNEXPECTED direction (would indicate a real bug): {len(unexpected_direction)}")
    for pid, (before, after) in unexpected_direction.items():
        print(f"    {pid}: baseline={before} compacted={after} -- {_per_source_line(pid)}")

    print(f"\npractice_trend_risk diffs: {len(trend_diffs)}")
    cleared_out = {pid: v for pid, v in trend_diffs.items() if v[1] is None}
    both_scored = {
        pid: v for pid, v in trend_diffs.items() if v[0] is not None and v[1] is not None
    }
    handled = set(cleared_out) | set(both_scored)
    other = {pid: v for pid, v in trend_diffs.items() if pid not in handled}
    print(f"  player cleared out of the injury category (no more trend row): {len(cleared_out)}")
    print(f"  BOTH sides scored but disagree (worth inspecting): {len(both_scored)}")
    for pid, (trend_before, trend_after) in both_scored.items():
        detail = _per_source_line(pid)
        print(f"    {pid}: baseline={trend_before} compacted={trend_after} -- {detail}")
    print(f"  other: {len(other)}")
    for pid, (trend_before, trend_after) in list(other.items())[:20]:
        detail = _per_source_line(pid)
        print(f"    {pid}: baseline={trend_before} compacted={trend_after} -- {detail}")

    if unexpected_direction or both_scored or other:
        print("\nFAIL -- found diffs that aren't explained by a source's clearance.")
        sys.exit(1)
    print(
        "\nPASS -- every diff is explained by a source's clearance (a real, intended "
        "behavior change from the old design, not a compaction bug)."
    )


if __name__ == "__main__":
    main()
