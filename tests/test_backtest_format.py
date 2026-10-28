"""The backtest report's interval bounds (docs/phases/P7.md, open item 22): a bound that
rounds to zero but isn't zero keeps its sign and a digit, because which side of zero it's
on is what the interval is for."""

import pytest

from scripts.backtest import _fmt_bound, _fmt_ci


@pytest.mark.parametrize(
    ("v", "expected"),
    [
        (-0.0004940895518897146, "-0.0005"),  # p5-v3's high-stability spread bound
        (0.00044696701709777246, "0.0004"),  # p5-v2's, printed "0.000" before 2026-10-28
        (0.0, "0.000"),
        (-0.08, "-0.080"),
        (0.015, "0.015"),
        (1e-9, "1.0e-09"),
        (None, "—"),
    ],
)
def test_fmt_bound(v: float | None, expected: str) -> None:
    assert _fmt_bound(v) == expected


def test_fmt_ci_never_prints_a_signed_or_unsigned_zero_for_a_nonzero_bound() -> None:
    assert _fmt_ci(-0.16419621813533644, -0.0004940895518897146) == "[-0.164, -0.0005]"
