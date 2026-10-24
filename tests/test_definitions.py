"""Pins each input-definition label (pipeline/core/definitions.py) to the source it covers.

`efficiency_fingerprint` hashes the labels, not the source, so the synthesizer never goes
stale over a comment edit at runtime. This test is what makes a real definition change
impossible to ship silently: editing covered source fails here until someone decides.
- It changes what Efficiency's signals are built from or how: bump the label. Every model
  file fit under the old label goes stale until a re-backfill and re-fit ship
  (docs/phases/P5.md, open item 3).
- It doesn't (a comment, a refactor, an unrelated column): re-pin the hash only.
"""

from __future__ import annotations

import hashlib
import inspect
import json
from collections.abc import Callable, Mapping
from typing import Any

from pipeline.analysts import efficiency
from pipeline.collectors import nflverse_bulk as nb
from pipeline.core import definitions
from pipeline.core.team_aliases import TEAM_ABBR_ALIASES

# (label, sha256[:16] of the covered source). Update both together, never the hash alone
# without reading the diff that moved it.
PINNED = {
    # Re-pinned 2026-10-15 for TEAM_ABBR_ALIASES' "LAR": "LA" (P8 D19), label kept: snaps,
    # team_week and player_week hold 0 LAR rows across 2018-2026 (measured), and nothing
    # mapped LAR before, so the alias changes nothing the fit reads.
    "TEAM_INPUTS_DEFINITION": ("2026-09-30.try-plays-excluded", "0db77bce93ef8297"),
    # Bumped 2026-10-27 for 2020's combined O-line snap codes in _OL_SNAP_POSITIONS (P7 open
    # item 22, row 3): the 2020-2021 signals change, so the model is re-fit (p5-v3). The
    # label names the deploy date; the code was written 2026-10-19.
    "EFFICIENCY_DEFINITION": ("2026-10-27.ol-combo-codes", "213d581d31f35855"),
}

_TEAM_INPUTS_FUNCTIONS: tuple[Callable[..., Any], ...] = (
    nb._aggregate_team_week,
    nb._garbage_time_expr,
    nb._build_player_week,
    nb._build_snaps,
    nb._derive_season_type,
    nb._normalize_team_abbr,
)
_TEAM_INPUTS_CONSTANTS = (
    "_SEASON_TYPES",
    "_GARBAGE_TIME_WP_LOW",
    "_GARBAGE_TIME_WP_HIGH",
    "_GARBAGE_TIME_Q3_WP_LOW",
    "_GARBAGE_TIME_Q3_WP_HIGH",
    "_EXPLOSIVE_PASS_YARDS",
    "_EXPLOSIVE_RUSH_YARDS",
    "_DRIVE_RESULT_POINTS",
    "_DRIVE_RESULT_KNOWN_ZERO",
    "_TEAM_WEEK_COLS",
    "_TEAM_WEEK_COUNT_COLS",
    "_TEAM_WEEK_SUM_COLS",
)


def _source(obj: Any) -> str:
    # CRLF on a Windows checkout must hash the same as LF on CI.
    return inspect.getsource(obj).replace("\r\n", "\n")


def _digest(sources: list[str], constants: Mapping[str, Any]) -> str:
    text = "\n".join(sources) + json.dumps(
        constants, sort_keys=True, default=lambda v: sorted(v) if isinstance(v, set) else v
    )
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def team_inputs_digest() -> str:
    constants = {name: getattr(nb, name) for name in _TEAM_INPUTS_CONSTANTS}
    constants["TEAM_ABBR_ALIASES"] = TEAM_ABBR_ALIASES
    return _digest([_source(f) for f in _TEAM_INPUTS_FUNCTIONS], constants)


def efficiency_digest() -> str:
    return _digest([_source(efficiency)], {})


def _check(label_name: str, digest: str) -> None:
    label = getattr(definitions, label_name)
    pinned_label, pinned_digest = PINNED[label_name]
    assert (label, digest) == (pinned_label, pinned_digest), (
        f"{label_name}: the source it covers changed (digest {digest}, pinned "
        f"{pinned_digest}), or the label did ({label!r} vs pinned {pinned_label!r}). If the "
        f"change alters what the fit reads, bump {label_name} in "
        "pipeline/core/definitions.py: the live model goes stale until a re-backfill and "
        "re-fit ship. Otherwise re-pin the digest only. Then update PINNED to match."
    )


def test_team_inputs_definition_is_pinned_to_its_source():
    _check("TEAM_INPUTS_DEFINITION", team_inputs_digest())


def test_efficiency_definition_is_pinned_to_its_source():
    _check("EFFICIENCY_DEFINITION", efficiency_digest())
