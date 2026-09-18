"""
Guards the invariant Analyst._delete_stale_signals (pipeline/core/base.py) depends on
for safety: its DELETE is scoped to `sector = %s AND signal = ANY(%s)`, so it can only
ever be safe from clobbering another analyst's rows if every registered analyst's
`sector` is distinct and no two analysts claim the same `signal_names`. Checked against
the dispatcher's actual `_ANALYSTS` registry (not a hardcoded pair) so a future analyst
(market, environment, usage, scheme, ...) is covered automatically the moment it's
registered there, rather than requiring someone to remember to extend this test too.
"""

from itertools import combinations

from pipeline.orchestration.dispatcher import _ANALYSTS


def test_registered_analysts_have_distinct_sectors():
    sectors = [a.sector for a in _ANALYSTS]
    assert len(sectors) == len(set(sectors)), f"duplicate sector among {sectors}"


def test_registered_analysts_have_disjoint_signal_names():
    for a, b in combinations(_ANALYSTS, 2):
        overlap = a.signal_names & b.signal_names
        assert not overlap, f"{a.name!r} and {b.name!r} both claim signal(s) {overlap}"
