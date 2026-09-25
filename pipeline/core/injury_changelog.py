"""
Job: Pure change-log rules for the injuries table -- shared by the availability
     collector, the historical backfill script, and the verification script, so there is
     exactly one implementation of "does this poll write a row" instead of three.
Reads: nothing (no DB access -- plain dicts in, plain dicts/bools out)
Writes: nothing
Tier: n/a
Phase: P3
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any, Literal

_STATE_FIELDS = ("team", "designation", "body_part")

# A notes-only change is written only when the new text mentions practice at all.
# Deliberately broad (user decision, 2026-09-25): measured on 2026-09-18..25, 38% of all
# change-log rows were ESPN rewriting a news blurb with nothing else changed, but those
# blurbs are the only place either source states practice participation ("was limited in
# practice Wednesday"), in prose. A plain substring over-matches ("practice squad") and
# that's the intended trade -- a false positive costs one row, a false negative silently
# loses practice data no structured field carries. See docs/phases/P3.md.
_PRACTICE_NOTES_RE = re.compile(r"practic", re.IGNORECASE)


def decide_injury_row(
    prior: dict[str, Any] | None, candidate: dict[str, Any]
) -> Literal["first_seen", "changed"] | None:
    """prior/candidate: {team, designation, body_part, notes} for one (source,
    source_player_id). None prior -> first appearance, always write. Otherwise write iff
    team/designation/body_part differs from the last stored state, or notes differs AND
    the new notes mention practice (_PRACTICE_NOTES_RE). A notes-only change without
    practice language isn't written, so the stored notes can lag the feed's latest blurb
    until the next written row. Never compares `raw` or `season`/`week`/`season_type` --
    those can shift without the tracked fields changing (e.g. ESPN's
    raw.details.returnDate ticking forward) and aren't what the change-log exists to
    capture."""
    if prior is None:
        return "first_seen"
    if any(prior.get(f) != candidate.get(f) for f in _STATE_FIELDS):
        return "changed"
    new_notes = candidate.get("notes")
    if prior.get("notes") != new_notes and _PRACTICE_NOTES_RE.search(new_notes or ""):
        return "changed"
    return None


def detect_cleared(
    active_from_db: dict[str, dict[str, Any]],
    present_this_poll: set[str],
    prior_counts: dict[str, int],
    *,
    miss_threshold: int = 2,
) -> tuple[list[str], dict[str, int]]:
    """active_from_db: {source_player_id: last-known state} for ONE source, already
    restricted by the caller to is_cleared=false rows. present_this_poll: source_player_ids
    seen in this poll's feed for that SAME source. prior_counts: {source_player_id:
    consecutive_misses} from injury_presence as of before this poll -- absent from the
    dict means 0 (never missed, or never tracked). Never mixes sources: ESPN and Sleeper
    are fetched independently, so calling this once per source is required, not optional
    -- a day Sleeper isn't polled must never be passed through here at all (the caller's
    job, via the sleeper_fetched guard) since an empty present_this_poll would otherwise
    read as "everyone on Sleeper missed a poll".

    A player only clears after `miss_threshold` (default 2) CONSECUTIVE absences from
    this source -- a single missed poll (a blip, a truncated response the outage guard
    didn't catch) must never immediately clear someone. Returns (to_clear,
    updated_counts):

    - to_clear: source_player_ids whose miss streak just reached miss_threshold this
      poll -- the caller writes their is_cleared=true row and deletes their
      injury_presence row.
    - updated_counts: the new consecutive_misses for every id that should remain tracked
      in injury_presence after this poll -- every id in present_this_poll (reset to 0,
      whether previously tracked or brand new) plus every previously-active id that's
      absent but still under miss_threshold (incremented by 1). Ids in to_clear are
      deliberately excluded here; the caller removes their row instead of upserting it.
    """
    updated_counts: dict[str, int] = dict.fromkeys(present_this_poll, 0)
    to_clear: list[str] = []
    for source_player_id in active_from_db:
        if source_player_id in present_this_poll:
            continue
        misses = prior_counts.get(source_player_id, 0) + 1
        if misses >= miss_threshold:
            to_clear.append(source_player_id)
        else:
            updated_counts[source_player_id] = misses
    return to_clear, updated_counts


def build_cleared_row(
    prior_state: dict[str, Any],
    *,
    source: str,
    source_player_id: str,
    player_id: str | None,
    season: int,
    week: int,
    season_type: str,
    as_of: datetime,
) -> dict[str, Any]:
    """Synthesizes the is_cleared=true row for a player who just disappeared from a
    source's feed. Tracked fields go NULL (enforced by the injuries_cleared_fields_null
    CHECK) -- team is carried forward for display, designation/body_part/notes are not,
    since a stale designation string on a "no longer listed" row would misclassify under
    _classify_designation. The prior designation is preserved only inside raw's synthetic
    marker, for audit."""
    return {
        "player_id": player_id,
        "source": source,
        "source_player_id": source_player_id,
        "season": season,
        "week": week,
        "season_type": season_type,
        "team": prior_state.get("team"),
        "designation": None,
        "body_part": None,
        "notes": None,
        "raw": {
            "synthetic": True,
            "reason": "cleared",
            "last_known_designation": prior_state.get("designation"),
        },
        "as_of": as_of,
        "is_cleared": True,
    }


def replay_history(
    rows: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Replays a store-every-poll history through this module's change-log rule, so the
    verification script and the backfill script share one implementation instead of two.
    `rows`: every existing injuries row (any order), each a dict with at least source,
    source_player_id, player_id, team, designation, body_part, notes, as_of, season,
    week, season_type.

    Returns (kept, synthetic_cleared) -- `kept` is the subset of `rows` that would have
    been written under the change-log rule (first appearance or a tracked-field change);
    `synthetic_cleared` is newly-built is_cleared=true rows for every historical
    "absent from TWO consecutive poll batches" transition (see detect_cleared),
    timestamped at the second absent batch's as_of -- never the first miss. A source's
    poll batches are its distinct `as_of` values -- every row in one poll shares the same
    as_of (see pipeline/collectors/availability.py's store()), so grouping by (source,
    as_of) reconstructs the original poll-by-poll history exactly, and a batch only
    exists for a poll that actually happened -- a day Sleeper wasn't polled has no batch
    at all, so it can never count as a miss, matching the live collector's
    sleeper_fetched guard.

    A batch whose feed looks like a source outage (is_source_outage, same threshold the
    collector uses) skips miss-counting and clearing entirely for that batch -- the same
    outage-guard rule the live collector applies, so history and live runs can't diverge
    on it.

    A player still present in the very last batch, or mid-way through a miss streak when
    history runs out, is left active, not cleared -- there is no evidence of
    disappearance to synthesize, only that history runs out."""
    by_source: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        by_source.setdefault(row["source"], []).append(row)

    kept: list[dict[str, Any]] = []
    cleared: list[dict[str, Any]] = []

    for source, source_rows in by_source.items():
        batches: dict[Any, list[dict[str, Any]]] = {}
        for row in source_rows:
            batches.setdefault(row["as_of"], []).append(row)

        last_state: dict[str, dict[str, Any]] = {}
        active: dict[str, dict[str, Any]] = {}
        miss_counts: dict[str, int] = {}

        for as_of in sorted(batches):
            batch_rows = batches[as_of]
            present_ids = {row["source_player_id"] for row in batch_rows}
            active_count_before = len(active)

            for row in batch_rows:
                sid = row["source_player_id"]
                candidate = {
                    "team": row["team"],
                    "designation": row["designation"],
                    "body_part": row["body_part"],
                    "notes": row["notes"],
                }
                if decide_injury_row(last_state.get(sid), candidate) is not None:
                    kept.append(row)
                    last_state[sid] = candidate
                active[sid] = {"player_id": row["player_id"], "team": row["team"],
                                "designation": row["designation"]}

            if is_source_outage(active_count_before, len(present_ids)):
                continue

            to_clear, miss_counts = detect_cleared(active, present_ids, miss_counts)

            for sid in to_clear:
                prior_state = active.pop(sid)
                template = batch_rows[0]  # any row this batch shares this source's
                # resolved season/week/season_type for this poll -- purely informational
                # on a cleared row, never trusted as a validity window on read.
                cleared_row = build_cleared_row(
                    prior_state,
                    source=source,
                    source_player_id=sid,
                    player_id=prior_state["player_id"],
                    season=template["season"],
                    week=template["week"],
                    season_type=template["season_type"],
                    as_of=as_of,
                )
                cleared.append(cleared_row)
                last_state[sid] = {
                    "team": cleared_row["team"],
                    "designation": None,
                    "body_part": None,
                    "notes": None,
                }

    return kept, cleared


def build_presence_rows(
    updated_counts: dict[str, int],
    presence_state: dict[str, dict[str, Any]],
    now: datetime,
) -> list[dict[str, Any]]:
    """Turns detect_cleared's updated_counts into injury_presence upsert rows for ONE
    source (caller adds the `source` column). presence_state: that source's current
    injury_presence rows (see availability.py's _fetch_presence_state), keyed by
    source_player_id.

    updated_counts can contain an id that's missing from presence_state -- e.g. a player
    already active in `injuries` from before injury_presence existed (the first run after
    the table was added), or any id whose first-ever miss is THIS poll. Such an id has no
    prior sighting to carry forward, so its miss starts the clock at `now` instead of
    raising KeyError.

    miss_count == 0 always means the id was present this poll, so `now` is exactly right;
    a surviving miss keeps the prior last_seen_at when presence_state has one, or falls
    back to `now` when this is the id's first tracked miss."""
    rows: list[dict[str, Any]] = []
    for source_player_id, miss_count in updated_counts.items():
        if miss_count == 0:
            last_seen_at = now
        else:
            prior = presence_state.get(source_player_id)
            last_seen_at = prior["last_seen_at"] if prior is not None else now
        rows.append(
            {
                "source_player_id": source_player_id,
                "consecutive_misses": miss_count,
                "last_seen_at": last_seen_at,
            }
        )
    return rows


def is_source_outage(active_count: int, seen_count: int, threshold: float = 0.5) -> bool:
    """True iff this poll's feed for a source is suspiciously small relative to that
    source's currently-active roster -- e.g. an ESPN outage or a truncated response.
    active_count == 0 never trips this (nothing to compare against yet, e.g. the very
    first poll of the season)."""
    if active_count <= 0:
        return False
    return seen_count < threshold * active_count
