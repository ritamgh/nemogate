"""Intervals for the results file: Wilson for rates, a seeded bootstrap for means.

stdlib only. `stats.py` re-exports `wilson`, so it can still be imported from there.
"""

import math
import random
from collections.abc import Sequence

Z = 1.96  # 95% two-sided
DECIMALS = 4
RESAMPLES = 2000
SEED = 20261016  # fixed, so the same runs give the same interval in any order


def wilson(k: int, n: int, z: float = Z) -> tuple[float, float]:
    """Wilson score interval (lo, hi) for k successes in n trials, clamped to [0, 1]."""
    if n < 1:
        raise ValueError(f"n must be at least 1, got {n}")
    if not 0 <= k <= n:
        raise ValueError(f"k must be between 0 and n={n}, got {k}")
    p = k / n
    denom = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / denom
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denom
    return max(0.0, centre - half), min(1.0, centre + half)


def bootstrap_mean(values: Sequence[float]) -> tuple[float, float | None, float | None]:
    """(mean, lo, hi): the mean and its 95% percentile-bootstrap interval, 2000 resamples.

    The values are sorted first and the generator is seeded, so the result never depends on
    input order. One value has no spread to resample: lo and hi are None. ValueError if empty.
    """
    if not values:
        raise ValueError("bootstrap_mean needs at least one value")
    data = sorted(values)
    n = len(data)
    mean = math.fsum(data) / n
    if n < 2:
        return mean, None, None
    rng = random.Random(SEED)
    means = sorted(math.fsum(rng.choices(data, k=n)) / n for _ in range(RESAMPLES))
    return mean, means[int(0.025 * RESAMPLES)], means[math.ceil(0.975 * RESAMPLES) - 1]


def round4(value: float) -> float:
    return round(value, DECIMALS) + 0.0  # + 0.0 turns -0.0 into 0.0


def rate_stat(k: int, n: int) -> dict[str, float]:
    """{"rate", "lo", "hi"}: k of n with its 95% Wilson interval, rounded for the results file."""
    lo, hi = wilson(k, n)
    return {"rate": round4(k / n), "lo": round4(lo), "hi": round4(hi)}
