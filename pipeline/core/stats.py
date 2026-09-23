"""
Job: Shared interval estimates -- the game bootstrap and the Wilson interval -- so the
     offline backtest (scripts/) and the grader (pipeline/orchestration/grader.py) use
     one implementation.
Reads: nothing
Writes: nothing
Tier: n/a
Phase: P5
"""

from __future__ import annotations

import math

import numpy as np

BOOTSTRAP_N = 2000
BOOTSTRAP_SEED = 20260923
_Z95 = 1.959963984540054


def bootstrap_corr(
    x: np.ndarray, y: np.ndarray, *, n_boot: int = BOOTSTRAP_N, seed: int = BOOTSTRAP_SEED
) -> dict[str, float | int | None]:
    """Pearson r with a game-bootstrap 95% percentile CI (fixed seed, reproducible).
    Undefined (all None) below 3 pairs or when either side has no variance."""
    x = np.asarray(x, dtype=float)
    y = np.asarray(y, dtype=float)
    n = int(x.size)
    if n < 3 or x.std() == 0 or y.std() == 0:
        return {"n": n, "corr": None, "ci_low": None, "ci_high": None}
    corr = float(np.corrcoef(x, y)[0, 1])
    rng = np.random.default_rng(seed)
    idx = rng.integers(0, n, size=(n_boot, n))
    xs, ys = x[idx], y[idx]
    xs = xs - xs.mean(axis=1, keepdims=True)
    ys = ys - ys.mean(axis=1, keepdims=True)
    denom = np.sqrt((xs**2).sum(axis=1) * (ys**2).sum(axis=1))
    boot = (xs * ys).sum(axis=1)[denom > 0] / denom[denom > 0]
    if boot.size == 0:
        return {"n": n, "corr": corr, "ci_low": None, "ci_high": None}
    lo, hi = np.quantile(boot, [0.025, 0.975])
    return {"n": n, "corr": corr, "ci_low": float(lo), "ci_high": float(hi)}


def bootstrap_mean_ci(
    x: np.ndarray, *, n_boot: int = BOOTSTRAP_N, seed: int = BOOTSTRAP_SEED
) -> dict[str, float | int | None]:
    """Mean with a game-bootstrap 95% percentile CI (fixed seed, reproducible). For a
    paired difference, pass the per-game differences."""
    x = np.asarray(x, dtype=float)
    n = int(x.size)
    if n == 0:
        return {"n": 0, "mean": None, "ci_low": None, "ci_high": None}
    mean = float(x.mean())
    if n < 2:
        return {"n": n, "mean": mean, "ci_low": None, "ci_high": None}
    rng = np.random.default_rng(seed)
    boot = x[rng.integers(0, n, size=(n_boot, n))].mean(axis=1)
    lo, hi = np.quantile(boot, [0.025, 0.975])
    return {"n": n, "mean": mean, "ci_low": float(lo), "ci_high": float(hi)}


def wilson_ci(successes: int, n: int, z: float = _Z95) -> tuple[float | None, float | None]:
    """Wilson score interval for a proportion. Unlike the normal approximation it stays
    inside [0, 1] and is honest at small n (7 of 12 -> about [0.32, 0.81])."""
    if n <= 0:
        return None, None
    p = successes / n
    denom = 1 + z * z / n
    center = (p + z * z / (2 * n)) / denom
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denom
    return max(0.0, center - half), min(1.0, center + half)
