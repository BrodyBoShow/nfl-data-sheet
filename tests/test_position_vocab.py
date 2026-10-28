"""The analysts' position code sets, pinned against the measured vocabulary
(tests/fixtures/position_vocab.json), with scripts/verify_position_vocab.py's own `check`.
The live run of that script checks the same sets against the database, in CI and after
every dispatcher tick (docs/phases/P7.md, open item 22)."""

import json
from pathlib import Path

import pytest

from scripts.verify_position_vocab import PARTITIONS, check

_FIXTURE = json.loads(
    (Path(__file__).parent / "fixtures" / "position_vocab.json").read_text(encoding="utf-8")
)


@pytest.mark.parametrize("column", sorted(PARTITIONS))
def test_sets_are_exactly_the_measured_vocabulary(column: str) -> None:
    assert check(column, _FIXTURE[column], PARTITIONS[column]) == []


def test_check_fails_on_a_code_no_set_knows() -> None:
    live = {**_FIXTURE["players.position"], "EDGE": 4}
    assert check("players.position", live, PARTITIONS["players.position"]) == [
        "players.position: 'EDGE' (4 rows) is in no set"
    ]


def test_check_fails_on_a_set_code_the_data_lacks() -> None:
    """The sets before 2026-10-28: "T" and "SS" matched no player, and "OT"/"SAF"/"FB"
    were in no set. Both directions fail."""
    old = {
        "ol": {"C", "G", "T", "OL"},
        "secondary": {"CB", "S", "DB", "FS", "SS"},
        "redistribution": {"WR", "RB", "TE"},
        "other": PARTITIONS["players.position"]["availability_impact._OTHER_POSITIONS"],
    }
    problems = check("players.position", _FIXTURE["players.position"], old)
    assert "players.position: 'T' in ol matches no row" in problems
    assert "players.position: 'SS' in secondary matches no row" in problems
    assert "players.position: 'OT' (1696 rows) is in no set" in problems
    assert "players.position: 'SAF' (328 rows) is in no set" in problems
    assert "players.position: 'FB' (209 rows) is in no set" in problems


def test_check_fails_on_null_and_on_overlap() -> None:
    live = {**_FIXTURE["snaps.position"], None: 3}
    sets = {**PARTITIONS["snaps.position"], "dup": {"QB"}}
    problems = check("snaps.position", live, sets)
    assert "snaps.position: 3 rows are NULL" in problems
    assert "snaps.position: 'QB' is in both efficiency._NON_OL_SNAP_POSITIONS and dup" in problems
