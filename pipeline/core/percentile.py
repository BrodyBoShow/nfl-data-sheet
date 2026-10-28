"""League percentile, one definition for every table that stores one (docs/signals.md).

`round(100 * (below + 0.5 * equal) / population)` of a value within its group, among the
rows in the population with a non-null value. Used by the player tables' `_pct`
(`pipeline/core/player_tables.py`, within a position group) and by Efficiency's team
`league_pct` (within a signal and week), so a team Pct and a player Pct mean the same.

Direction-neutral: a high percentile is a high value. The display decides which way is
good.
"""

from __future__ import annotations

import polars as pl


def league_pct_expr(value: pl.Expr, in_pop: pl.Expr, over: str | list[str]) -> pl.Expr:
    """The percentile of `value` within each `over` group, among rows where `in_pop` holds
    and `value` is non-null. Null outside the population. A whole number, as a float."""
    pop = in_pop.fill_null(False) & value.is_not_null()
    v = pl.when(pop).then(value).otherwise(None)
    return (100.0 * (v.rank("average").over(over) - 0.5) / pop.sum().over(over)).round(0)
