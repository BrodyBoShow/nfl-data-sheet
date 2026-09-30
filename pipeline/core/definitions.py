"""Version labels for the code that defines what the projection model's fit reads.

`pipeline/synthesis/model.py`'s `efficiency_fingerprint` hashes these, so bumping one
makes every model file fit under the old label stale: the synthesizer refuses to project
until a re-backfill and re-fit ship (docs/phases/P5.md, open item 3).

Each label is pinned to a hash of the source it covers by `tests/test_definitions.py`.
Editing that source fails the test until someone decides:
- the edit changes what Efficiency's signals are built from or how, so bump the label, or
- it doesn't (a comment, a refactor, an unrelated column), so re-pin the hash only.

Data-only changes (new seasons staged, a re-backfill, upstream revisions) don't touch
these. The fit-input hash (`model.fit_inputs_hash`) catches those.
"""

# The collector functions that build the tables Efficiency reads: `_aggregate_team_week`
# and `_garbage_time_expr` (team_week), `_build_player_week` (QB attempts), and
# `_build_snaps` (O-line groups), plus the constants they read.
# 2026-09-30: try plays excluded from team_week (docs/phases/P2.md, open items 1 and 4).
TEAM_INPUTS_DEFINITION = "2026-09-30.try-plays-excluded"

# pipeline/analysts/efficiency.py as a whole: how those tables become signals.
EFFICIENCY_DEFINITION = "2026-09-30"
